"""Native DRM monitor bridge using the public libevdi 1.14/1.15 ABI.

Requires a compatible evdi kernel module. It does not capture the desktop:
the compositor renders into this connector, and libevdi supplies the pixels.
API: https://github.com/DisplayLink/evdi/blob/main/library/evdi_lib.h
"""
import ctypes as C
import ctypes.util
import select
import time
from pathlib import Path


# DRM fourcc names describe packed little-endian words, not byte order.
# The physical USB panel is opaque, so the alpha/unused byte is discarded.
# https://github.com/torvalds/linux/blob/master/include/uapi/drm/drm_fourcc.h
DRM_FORMATS_32 = {
    0x34325258: ('XRGB8888', 'BGRX'),
    0x34325241: ('ARGB8888', 'BGRX'),
    0x34324258: ('XBGR8888', 'RGBX'),
    0x34324241: ('ABGR8888', 'RGBX'),
}


class EvdiError(RuntimeError):
    pass


class Version(C.Structure):
    _fields_ = [(name, C.c_int) for name in ("major", "minor", "patch")]


class Mode(C.Structure):
    _fields_ = [("width", C.c_int), ("height", C.c_int), ("refresh_rate", C.c_int),
                ("bits_per_pixel", C.c_int), ("pixel_format", C.c_uint)]


class Rect(C.Structure):
    _fields_ = [(name, C.c_int) for name in ("x1", "y1", "x2", "y2")]


class Buffer(C.Structure):
    _fields_ = [("id", C.c_int), ("buffer", C.c_void_p), ("width", C.c_int),
                ("height", C.c_int), ("stride", C.c_int),
                ("rects", C.POINTER(Rect)), ("rect_count", C.c_int)]


class CursorSet(C.Structure):
    _fields_ = [("hot_x", C.c_int32), ("hot_y", C.c_int32), ("width", C.c_uint32),
                ("height", C.c_uint32), ("enabled", C.c_uint8), ("buffer_length", C.c_uint32),
                ("buffer", C.POINTER(C.c_uint32)), ("pixel_format", C.c_uint32), ("stride", C.c_uint32)]


class CursorMove(C.Structure):
    _fields_ = [("x", C.c_int32), ("y", C.c_int32)]


class Ddcci(C.Structure):
    _fields_ = [("address", C.c_uint16), ("flags", C.c_uint16),
                ("buffer_length", C.c_uint32), ("buffer", C.POINTER(C.c_uint8))]


IntCallback = C.CFUNCTYPE(None, C.c_int, C.c_void_p)
ModeCallback = C.CFUNCTYPE(None, Mode, C.c_void_p)
CursorSetCallback = C.CFUNCTYPE(None, CursorSet, C.c_void_p)
CursorMoveCallback = C.CFUNCTYPE(None, CursorMove, C.c_void_p)
DdcciCallback = C.CFUNCTYPE(None, Ddcci, C.c_void_p)


class EventContext(C.Structure):
    _fields_ = [("dpms", IntCallback), ("mode_changed", ModeCallback),
                ("update_ready", IntCallback), ("crtc", IntCallback),
                ("cursor_set", CursorSetCallback), ("cursor_move", CursorMoveCallback),
                ("ddcci", DdcciCallback), ("user_data", C.c_void_p)]


def panel_edid(info):
    edid = info.raw[16:144]
    if len(edid) != 128 or edid[:8] != bytes.fromhex("00ffffffffffff00") or sum(edid) % 256:
        raise EvdiError("EDID base inválido na resposta USB; monitor DRM não será criado")
    if edid[126]:
        raise EvdiError("o EDID anuncia extensões não disponíveis na resposta USB de 160 bytes")
    return edid


def load_library():
    private = Path('/opt/tomate-mtm1118/evdi-lib/libevdi.so.1')
    name = str(private) if private.is_file() else ctypes.util.find_library("evdi")
    if not name:
        raise EvdiError("libevdi ausente; instale a biblioteca EVDI compatível com o módulo de kernel")
    return C.CDLL(name)


class EvdiCapture:
    def __init__(self, info, device_index=None):
        edid = panel_edid(info)
        if not Path("/sys/module/evdi").exists():
            raise EvdiError("módulo evdi não carregado; instale uma versão compatível com seu kernel e execute sudo modprobe evdi")
        self.lib = load_library()
        self.handle, self.connected = None, False
        self.memory, self.buffer = None, None
        self.mode, self.error = None, None
        self.pending = self.ready = False
        self.dpms_on, self.crtc_on = True, True
        self.buffer_id = 0
        self.max_area = info.width * info.height
        self._bind()
        version = Version()
        self.lib.evdi_get_lib_version(C.byref(version))
        if version.major != 1 or version.minor not in (14, 15):
            raise EvdiError(f"ABI libevdi {version.major}.{version.minor} ainda não auditada; esperado 1.14 ou 1.15")
        self.callbacks = EventContext(
            IntCallback(self._dpms), ModeCallback(self._safe_mode), IntCallback(self._update),
            IntCallback(self._crtc), CursorSetCallback(lambda *args: None),
            CursorMoveCallback(lambda *args: None), DdcciCallback(self._ddcci), None)
        try:
            # Explicit indices are checked to prevent opening a non-EVDI card.
            if device_index is None:
                if hasattr(self.lib, "evdi_open_attached_to_fixed"):
                    self.handle = self.lib.evdi_open_attached_to_fixed(None, 0)
                else:
                    self.handle = self.lib.evdi_open_attached_to(None)
            else:
                if self.lib.evdi_check_device(device_index) != 0:
                    raise EvdiError("o índice DRM indicado não é um dispositivo EVDI disponível")
                self.handle = self.lib.evdi_open(device_index)
            if not self.handle:
                raise EvdiError("não foi possível abrir/criar um dispositivo EVDI; confira permissões DRM e o módulo")
            self.fd = self.lib.evdi_get_event_ready(self.handle)
            if self.fd < 0:
                raise EvdiError("EVDI não devolveu um descritor de eventos válido")
            self.edid = (C.c_ubyte * len(edid)).from_buffer_copy(edid)
            self.lib.evdi_connect2(self.handle, self.edid, len(edid), self.max_area, self.max_area * 60)
            self.connected = True
            self.lib.evdi_enable_cursor_events(self.handle, False)
            print("[DRM] EDID apresentado ao EVDI. Ative o novo monitor nas configurações de tela do Linux.", flush=True)
        except BaseException:
            self.close()
            raise

    def _bind(self):
        signatures = {
            "evdi_get_lib_version": ([C.POINTER(Version)], None),
            "evdi_open_attached_to": ([C.c_char_p], C.c_void_p),
            "evdi_check_device": ([C.c_int], C.c_int),
            "evdi_open": ([C.c_int], C.c_void_p),
            "evdi_close": ([C.c_void_p], None),
            "evdi_get_event_ready": ([C.c_void_p], C.c_int),
            "evdi_connect2": ([C.c_void_p, C.POINTER(C.c_ubyte), C.c_uint, C.c_uint32, C.c_uint32], None),
            "evdi_disconnect": ([C.c_void_p], None),
            "evdi_enable_cursor_events": ([C.c_void_p, C.c_bool], None),
            "evdi_register_buffer": ([C.c_void_p, Buffer], None),
            "evdi_unregister_buffer": ([C.c_void_p, C.c_int], None),
            "evdi_request_update": ([C.c_void_p, C.c_int], C.c_bool),
            "evdi_grab_pixels": ([C.c_void_p, C.POINTER(Rect), C.POINTER(C.c_int)], None),
            "evdi_handle_events": ([C.c_void_p, C.POINTER(EventContext)], None),
            "evdi_ddcci_response": ([C.c_void_p, C.POINTER(C.c_ubyte), C.c_uint32, C.c_bool], None),
        }
        if hasattr(self.lib, "evdi_open_attached_to_fixed"):
            signatures["evdi_open_attached_to_fixed"] = ([C.c_char_p, C.c_size_t], C.c_void_p)
        for name, (arguments, result) in signatures.items():
            function = getattr(self.lib, name)
            function.argtypes, function.restype = arguments, result

    def wait_connected(self, timeout=3):
        """libevdi connect2 returns void; confirm kernel connector state too."""
        card = Path(f'/proc/self/fd/{self.fd}').resolve().name
        if not card.startswith('card') or not card[4:].isdigit():
            raise EvdiError('O descritor EVDI não corresponde a uma placa DRM.')
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for connector in Path('/sys/class/drm').glob(f'{card}-*'):
                try:
                    if (connector / 'status').read_text().strip() == 'connected':
                        return
                except OSError:
                    continue
            time.sleep(.05)
        raise EvdiError('O kernel não confirmou a conexão do monitor EVDI.')

    def _dpms(self, mode, unused):
        self.dpms_on = mode == 0

    def _crtc(self, state, unused):
        self.crtc_on = bool(state)

    def _update(self, buffer_id, unused):
        if self.buffer is not None and buffer_id == self.buffer.id:
            self.ready = True

    def _ddcci(self, data, unused):
        # No hardware brightness/DDC protocol has been established for this panel.
        self.lib.evdi_ddcci_response(self.handle, None, 0, False)

    def _safe_mode(self, mode, unused):
        try:
            self._mode_changed(mode)
        except Exception as exc:
            self.error = exc  # ctypes callbacks cannot propagate Python exceptions.

    def _mode_changed(self, mode):
        if not (16 <= mode.width <= 8192 and 16 <= mode.height <= 8192) or mode.width * mode.height > self.max_area:
            raise EvdiError("o compositor selecionou um modo fora da capacidade anunciada do painel")
        if mode.bits_per_pixel != 32 or mode.pixel_format not in DRM_FORMATS_32:
            raise EvdiError(f"formato DRM não suportado: bpp={mode.bits_per_pixel}, fourcc=0x{mode.pixel_format:x}")
        if self.buffer is not None:
            self.lib.evdi_unregister_buffer(self.handle, self.buffer.id)
            self.buffer = None
        self.memory = C.create_string_buffer(mode.width * mode.height * 4)
        self.buffer_id += 1
        self.buffer = Buffer(self.buffer_id, C.cast(self.memory, C.c_void_p), mode.width, mode.height,
                             mode.width * 4, None, 0)
        self.lib.evdi_register_buffer(self.handle, self.buffer)
        self.mode, self.pending, self.ready = mode, False, False
        print(f"[DRM] Modo {mode.width}x{mode.height}, {mode.refresh_rate} Hz, buffer {self.buffer.id}, "
              f"formato {DRM_FORMATS_32[mode.pixel_format][0]}.", flush=True)

    def grab(self, monitor=None):
        from PIL import Image
        # Drain pending mode/update events without adding a fixed 20 ms wait
        # to every frame. The outer loop already controls the frame rate.
        if select.select([self.fd], [], [], 0)[0]:
            self.lib.evdi_handle_events(self.handle, C.byref(self.callbacks))
        if self.error:
            raise EvdiError(str(self.error))
        if self.buffer is None or not self.dpms_on or not self.crtc_on:
            return None
        if not self.pending:
            self.ready = bool(self.lib.evdi_request_update(self.handle, self.buffer.id))
            self.pending = True
        if not self.ready:
            return None
        rects, count = (Rect * 16)(), C.c_int(0)
        self.lib.evdi_grab_pixels(self.handle, rects, C.byref(count))
        self.pending = self.ready = False
        # Arm the next damage notification while JPEG encoding/USB are busy.
        # A single registered buffer is kept: no queue of obsolete frames.
        self.ready = bool(self.lib.evdi_request_update(self.handle, self.buffer.id))
        self.pending = True
        if count.value <= 0:
            return None
        return Image.frombytes("RGB", (self.buffer.width, self.buffer.height), bytes(self.memory),
                               "raw", DRM_FORMATS_32[self.mode.pixel_format][1], self.buffer.stride, 1)

    def close(self):
        if not self.handle:
            return
        try:
            if self.connected:
                self.lib.evdi_disconnect(self.handle)
                self.connected = False
            if self.buffer is not None:
                self.lib.evdi_unregister_buffer(self.handle, self.buffer.id)
                self.buffer = None
        finally:
            self.lib.evdi_close(self.handle)
            self.handle = None
            self.memory = None
