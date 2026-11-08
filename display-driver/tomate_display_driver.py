#!/usr/bin/env python3
"""Linux userspace display transport for Tomate MTM-1118 / ArtInChip.

Protocol evidence and validation limits: ../README.md.
This edition exposes only the native DRM monitor.
"""

import argparse
import fcntl
import io
import json
import os
import signal
import socket
import sys
import time
from array import array
from pathlib import Path
from calibration_display import CalibrationDisplay

from aic_protocol import (AicProtocol, ProtocolError, FORMAT_NAMES, FORMAT_RGB565,
                          FORMAT_BGRA32, FORMAT_JPEG, FORMAT_H264)

SUPPORTED_DEVICES = ((0x33C3, 0x0E02), (0x33C3, 0x0E01))
SETTINGS_PATH = Path("/etc/tomate-mtm1118/settings.json")
EXIT_DRM_SETUP = 78


class DrmSetupError(RuntimeError):
    """A compositor/setup failure must not trigger repeated USB power cycles."""


def prepare_drm(info, device_index=None, timeout=30):
    """Obtain the first real desktop frame before initializing/powering USB."""
    capture = None
    try:
        from evdi_capture import EvdiCapture
        capture = EvdiCapture(info, device_index)
        capture.wait_connected(timeout=10)
        print('[DRM] Conector conectado; aguardando o primeiro quadro do compositor. '
              'Ative a mesa nas configurações de Telas.', flush=True)
        start = time.monotonic()
        next_status = start + 5
        while time.monotonic() - start < timeout:
            image = capture.grab()
            if image is not None:
                return capture, image
            now = time.monotonic()
            if now >= next_status:
                print(f'[DRM] Sem quadro após {now - start:.0f}s; '
                      f'modo recebido={capture.mode is not None}, '
                      f'DPMS ativo={capture.dpms_on}, CRTC ativo={capture.crtc_on}.', flush=True)
                next_status = now + 5
            time.sleep(.01)
        raise DrmSetupError('O compositor não forneceu imagem à mesa dentro do prazo. '
                            'O serviço será interrompido; consulte os logs DRM/COSMIC.')
    except BaseException as exc:
        if capture is not None:
            try:
                capture.close()
            except Exception as cleanup_error:
                print(f'[DRM] Liberação após falha: {cleanup_error}', flush=True)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise DrmSetupError(str(exc)) from exc


class DrmFrameCache:
    """Repeat the last real frame on USB during unchanged, active DRM output."""
    def __init__(self, interval=.25):
        self.interval = interval
        self.image = self.encoded = None
        self.flip, self.sent_at = None, 0

    def next_payload(self, image, encode, now, flip=False, active=True):
        if not active:
            self.image = self.encoded = None
            return None
        if image is not None:
            self.image, self.encoded = image, encode(image)
        elif self.image is None:
            return None
        elif flip != self.flip:
            self.encoded = encode(self.image)
        elif now - self.sent_at < self.interval:
            return None
        self.flip, self.sent_at = flip, now
        return self.encoded


class FrameTimings:
    """Windowed driver costs, not an estimate of physical panel latency."""
    def __init__(self):
        self.samples = []

    def add(self, capture, encode, usb):
        self.samples.append((capture, encode, usb))

    def summary(self):
        if not self.samples:
            return ''
        count = len(self.samples)
        means = [sum(sample[i] for sample in self.samples) * 1000 / count for i in range(3)]
        self.samples.clear()
        return f' captura={means[0]:.1f} ms, JPEG/conversão={means[1]:.1f} ms, USB={means[2]:.1f} ms;'


def frame_interval(fps, capture=None):
    refresh = getattr(getattr(capture, 'mode', None), 'refresh_rate', 0)
    return 1 / (min(fps, refresh) if refresh > 0 else fps)


def notify_ready():
    """DRM service is ready only after its first real frame has reached USB."""
    address = os.environ.get('NOTIFY_SOCKET')
    if address:
        if address.startswith('@'):
            address = '\0' + address[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
            channel.connect(address)
            channel.sendall(b'READY=1')


def single_instance():
    # One shared lock across root and desktop sessions; USB busnum/devnum also
    # have exclusive claims. No per-user /tmp fallback that hides a root owner.
    path = "/run/lock/tomate-display.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o666)
    handle = os.fdopen(fd, "r+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise ProtocolError("outra instância controla o vídeo; pare tomate-display.service antes de executar manualmente")
    return handle


class UsbConnection:
    def __init__(self, bus=None, address=None):
        self.device = None
        self.detached = False
        self.claimed = False
        self.bus, self.address = bus, address

    def open(self):
        import usb.core
        import usb.util
        devices = [device for vid, pid in SUPPORTED_DEVICES
                   for device in usb.core.find(find_all=True, idVendor=vid, idProduct=pid)
                   if (self.bus is None or device.bus == self.bus)
                   and (self.address is None or device.address == self.address)]
        if not devices:
            raise ProtocolError("ArtInChip 33c3:0e02/0e01 ausente; confirme lsusb dentro deste Linux")
        if len(devices) != 1:
            raise ProtocolError("mais de um display encontrado; escolha --bus e --address")
        self.device = device = devices[0]
        try:
            try:
                cfg = device.get_active_configuration()
            except usb.core.USBError:
                device.set_configuration()
                cfg = device.get_active_configuration()
            # The INF binds MI_00. Do not detach other composite interfaces.
            interface = cfg[(0, 0)]
            if device.is_kernel_driver_active(0):
                device.detach_kernel_driver(0)
                self.detached = True
            usb.util.claim_interface(device, 0)
            self.claimed = True
            out = usb.util.find_descriptor(interface, custom_match=lambda ep:
                usb.util.endpoint_type(ep.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK
                and usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_OUT)
            incoming = usb.util.find_descriptor(interface, custom_match=lambda ep:
                usb.util.endpoint_type(ep.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK
                and usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN)
            if out is None or incoming is None:
                raise ProtocolError("interface 0 sem o par Bulk IN/OUT exigido pela autenticação")
            print(f"USB {device.bus:03d}:{device.address:03d} {device.idVendor:04x}:{device.idProduct:04x}; "
                  f"interface=0, OUT=0x{out.bEndpointAddress:02x}, IN=0x{incoming.bEndpointAddress:02x}", flush=True)
            return AicProtocol(device, out, incoming)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.device is None:
            return
        import usb.util
        try:
            if self.claimed:
                usb.util.release_interface(self.device, 0)
            if self.detached:
                self.device.attach_kernel_driver(0)
        except Exception as exc:
            print(f"Liberação USB: {exc}", flush=True)
        finally:
            usb.util.dispose_resources(self.device)
            self.device = None
            self.claimed = self.detached = False


def encode_frame(image, info, pixel_format, quality=85, flip=False):
    from PIL import Image
    image = image.convert("RGB")
    if image.size != (info.width, info.height):
        image = image.resize((info.width, info.height), Image.Resampling.BILINEAR)
    if flip:
        image = image.transpose(Image.Transpose.ROTATE_180)
    if pixel_format == FORMAT_JPEG:
        output = io.BytesIO()
        # Newer firmware's JPEG path explicitly sets all sampling factors to 1.
        subsampling = 0 if info.supports_mode_command else 2
        image.save(output, "JPEG", quality=quality, subsampling=subsampling, progressive=False)
        return output.getvalue()
    if pixel_format == FORMAT_BGRA32:
        return image.convert("RGBA").tobytes("raw", "BGRA")
    if pixel_format == FORMAT_RGB565:
        try:
            import numpy as np
            rgb = np.asarray(image, dtype=np.uint16)
            words = ((rgb[:, :, 0] >> 3) << 11) | ((rgb[:, :, 1] >> 2) << 5) | (rgb[:, :, 2] >> 3)
            return words.astype("<u2").tobytes()
        except ImportError:
            words = array("H", (((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
                                for r, g, b in image.getdata()))
            if sys.byteorder != "little":
                words.byteswap()
            return words.tobytes()
    raise ProtocolError("H.264 está presente no binário, mas o encoder Linux ainda não foi implementado")


def settings():
    try:
        cfg = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return 1, bool(cfg.get("Digitizer", {}).get("FlipScreen", False)), "drm"
    except (OSError, ValueError, TypeError):
        return 1, False, "drm"


def stop_signal(signum, frame):
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("drm",), default="drm", help=argparse.SUPPRESS)
    parser.add_argument("--evdi-device", type=int, default=None, help="índice DRM EVDI (padrão: criar/abrir pelo libevdi)")
    parser.add_argument("--drm-startup-timeout", type=float, default=30)
    parser.add_argument("--format", choices=("auto", "jpeg", "rgb565", "raw32"), default="auto")
    parser.add_argument("--width", type=int, default=0)
    parser.add_argument("--height", type=int, default=0)
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--quality", type=int, default=85)
    parser.add_argument("--bus", type=int)
    parser.add_argument("--address", type=int)
    parser.add_argument("--duration", type=float, default=0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if (not 1 <= args.fps <= 60 or not 1 <= args.quality <= 100 or args.duration < 0
            or not 1 <= args.drm_startup_timeout <= 300):
        parser.error("fps deve ser 1..60, qualidade 1..100, duração >= 0 e prazo DRM 1..300")
    signal.signal(signal.SIGTERM, stop_signal)
    signal.signal(signal.SIGINT, stop_signal)
    try:
        import usb.core
        from PIL import Image
        lock = single_instance()
    except (ImportError, OSError, ProtocolError) as exc:
        print(f"[ERRO] {exc}", flush=True)
        return 1
    requested = next((value for value, name in FORMAT_NAMES.items() if name == args.format), None)
    warned_missing = False
    try:
        while True:
            connection, protocol, capture = UsbConnection(args.bus, args.address), None, None
            calibration_display = None
            try:
                protocol = connection.open()
                warned_missing = False
                probed_info = protocol.read_info()
                capture, first_image = prepare_drm(probed_info, args.evdi_device, args.drm_startup_timeout)
                info = protocol.initialize(requested)
                if ((info.width, info.height, info.raw[16:144]) !=
                        (probed_info.width, probed_info.height, probed_info.raw[16:144])):
                    raise DrmSetupError('As capacidades USB mudaram durante a criação do monitor; reinicie o serviço.')
                if (args.width and args.width != info.width) or (args.height and args.height != info.height):
                    raise ProtocolError("as dimensões pedidas diferem do firmware")
                if protocol.pixel_format == FORMAT_H264:
                    raise ProtocolError("firmware pede H.264; encoder Linux não implementado")
                protocol.authenticate()
                flip = settings()[1]
                start = last_stats = time.monotonic()
                timings = FrameTimings()
                stats_frames = protocol.frames
                drm_cache = DrmFrameCache()
                latest_desktop = first_image
                calibration_display = CalibrationDisplay(info.width, info.height)
                payload = drm_cache.next_payload(first_image, lambda image: encode_frame(
                    image, info, protocol.pixel_format, args.quality, flip), start, flip)
                protocol.send_frame(payload)
                calibration_display.publish(capture, force=True)
                notify_ready()
                print('[DRM] Primeiro quadro real enviado por USB; fluxo de imagem iniciado.', flush=True)
                while not args.duration or time.monotonic() - start < args.duration:
                    tick = time.monotonic()
                    flip = settings()[1]
                    try:
                        image = capture.grab()
                    except Exception as exc:
                        raise DrmSetupError(str(exc)) from exc
                    captured_at = time.monotonic()
                    if image is not None:
                        latest_desktop = image
                    active = capture.dpms_on and capture.crtc_on
                    overlay = calibration_display.payload(lambda frame: encode_frame(
                        frame, info, protocol.pixel_format, args.quality, False)) if active else None
                    if overlay is not None:
                        payload = overlay
                        calibration_display.was_calibrating = True
                    else:
                        # Resume the newest desktop frame even when it has no new damage.
                        if calibration_display.was_calibrating:
                            image = latest_desktop
                            calibration_display.was_calibrating = False
                        payload = drm_cache.next_payload(image, lambda frame: encode_frame(
                            frame, info, protocol.pixel_format, args.quality, flip), tick, flip, active=active)
                    calibration_display.publish(capture)
                    if payload is None:
                        time.sleep(max(0, frame_interval(args.fps, capture) - (time.monotonic() - tick)))
                        continue
                    encoded_at = time.monotonic()
                    protocol.send_frame(payload)
                    if overlay is not None:
                        first_target_frame = calibration_display.shown is None
                        calibration_display.sent()
                        calibration_display.publish(capture, force=first_target_frame)
                    sent_at = time.monotonic()
                    timings.add(captured_at - tick, encoded_at - captured_at, sent_at - encoded_at)
                    if time.monotonic() - last_stats >= 5:
                        elapsed = time.monotonic() - start
                        recent_fps = (protocol.frames - stats_frames) / max(sent_at - last_stats, .001)
                        print(f"[USB] {protocol.frames} quadros, {protocol.payload_bytes} bytes, "
                              f"{protocol.frames / max(elapsed, .001):.1f} fps médios; "
                              f"{recent_fps:.1f} fps recentes;{timings.summary()} "
                              "transferências concluídas (imagem visível requer confirmação na mesa)", flush=True)
                        last_stats, stats_frames = sent_at, protocol.frames
                    time.sleep(max(0, frame_interval(args.fps, capture) - (time.monotonic() - tick)))
                return 0
            except KeyboardInterrupt:
                return 0
            except DrmSetupError as exc:
                print(f'[DRM ERRO] {exc} Sem reinício automático por falha de configuração.', flush=True)
                return EXIT_DRM_SETUP
            except Exception as exc:
                if not warned_missing:
                    print(f"[ERRO] {type(exc).__name__}: {exc}", flush=True)
                    warned_missing = True
                if args.once:
                    return 1
            finally:
                if calibration_display is not None and capture is not None:
                    calibration_display.publish(capture, force=True, active=False)
                if capture is not None:
                    capture.close()
                if protocol is not None:
                    try:
                        protocol.power_off()
                    except Exception as exc:
                        print(f"Desligamento do fluxo: {exc}", flush=True)
                connection.close()
            time.sleep(2)
    except KeyboardInterrupt:
        return 0
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
