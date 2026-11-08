"""ArtInChip wire protocol reconstructed from the shipped Windows DLL.

See ../README.md for addresses, evidence, and remaining unknowns.
This module has no dependency on Linux, PyUSB, or a desktop session.
"""

import secrets
import struct
from dataclasses import dataclass

FORMAT_RGB565 = 0x0000
FORMAT_BGRA32 = 0x0001
FORMAT_JPEG = 0x0010
FORMAT_H264 = 0x0011
FORMAT_NAMES = {0: "rgb565", 1: "raw32", 0x10: "jpeg", 0x11: "h264"}
MAGIC_FRAME = 0xA1C62B01
MAGIC_AUTH_HOST = 0xA1C62B10
MAGIC_AUTH_DEVICE = 0xA1C62B11
ALPHANUMERIC = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAybdtvB1uNA4XICh+xJi1
KJWO0GYal4lNiW69zSMIJFGzb2wkiFBX2txFaH5ZYh0TYdwmjzBqinzTsWhIasW3
rl9QN5cv73zFalO3J4hADXz1g7hlHVB0BKDD280NUKUGAbwDv+KMHTprs+B/T4QU
a0s4RBNnN4fMPk2H0UAWU1jKAvMYjh/YR+MLYbl04ZCLlOfX9zQjRBVan7aLARQg
v5QRahAlAoBsYK864VrBKq91lRCXt4XP5d/sDtZM7kGcpLi2i4xHtRct37M+bkZv
Lf/3aVpAVsqZy5P2NXEe6HMv4Q+YP6QKz2wuk3xWYHWFn+88ydjv394tN28rjl56
hwIDAQAB
-----END PUBLIC KEY-----"""


class ProtocolError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeviceInfo:
    raw: bytes
    revision: int
    state: int
    pixel_format: int
    width: int
    height: int
    flags: int

    @classmethod
    def parse(cls, raw):
        raw = bytes(raw)
        if len(raw) != 0xA0:
            raise ProtocolError(f"resposta 0x00 incompleta: {len(raw)}/160 bytes")
        revision, state, fmt = struct.unpack_from("<HHH", raw)
        width, height = struct.unpack_from("<HH", raw, 10)
        if state not in (0x80, 0x81):
            raise ProtocolError(f"estado de vídeo não aceito pelo driver original: 0x{state:04x}")
        if not (16 <= width <= 8192 and 16 <= height <= 8192):
            raise ProtocolError(f"dimensões inválidas no firmware: {width}x{height}")
        if fmt not in FORMAT_NAMES:
            raise ProtocolError(f"formato desconhecido no firmware: 0x{fmt:04x}")
        return cls(raw, revision, state, fmt, width, height, raw[0x90])

    @property
    def supports_mode_command(self):
        return self.revision >= 3 and bool(self.flags & 1)


def frame_header(payload_size, pixel_format, orientation=0):
    if not 0 < payload_size <= 0xFFFFFFFF:
        raise ProtocolError("tamanho de quadro inválido")
    if pixel_format not in FORMAT_NAMES or not 0 <= orientation <= 3:
        raise ProtocolError("formato ou orientação de quadro inválido")
    return struct.pack("<IIHHII", MAGIC_FRAME, payload_size, orientation, pixel_format, 0, MAGIC_FRAME)


def recover_device_challenge(public_key, ciphertext):
    """Equivalent of RSA_public_decrypt with RSA_PKCS1_PADDING (type 1)."""
    numbers = public_key.public_numbers()
    size = (numbers.n.bit_length() + 7) // 8
    if len(ciphertext) != size or int.from_bytes(ciphertext, "big") >= numbers.n:
        raise ProtocolError("desafio RSA com tamanho ou valor inválido")
    block = pow(int.from_bytes(ciphertext, "big"), numbers.e, numbers.n).to_bytes(size, "big")
    separator = block.find(b"\0", 2)
    if block[:2] != b"\0\1" or separator < 10 or block[2:separator] != b"\xff" * (separator - 2):
        raise ProtocolError("padding RSA tipo 1 inválido no desafio do dispositivo")
    response = block[separator + 1:]
    if not 1 <= len(response) <= size - 11:
        raise ProtocolError("resposta RSA vazia ou longa demais")
    return response


class AicProtocol:
    def __init__(self, device, ep_out, ep_in, log=print):
        self.device, self.ep_out, self.ep_in = device, ep_out, ep_in
        self.log = log
        self.info = None
        self.authenticated = False
        self.powered = False
        self.pixel_format = None
        self.frames = 0
        self.payload_bytes = 0

    def write(self, payload, timeout=3000):
        sent = self.ep_out.write(payload, timeout=timeout)
        if sent != len(payload):
            self.authenticated = False
            raise ProtocolError(f"Bulk OUT incompleto: {sent}/{len(payload)} bytes")

    def control(self, request, value=0, index=0):
        result = self.device.ctrl_transfer(0x40, request, value, index, b"", timeout=3000)
        if result != 0:
            raise ProtocolError(f"controle 0x{request:02x}: resposta inesperada {result}")

    def read_info(self):
        """Read capabilities without resetting the stream or changing power."""
        raw = self.device.ctrl_transfer(0xC0, 0, 0, 0, 0xA0, timeout=3000)
        self.info = DeviceInfo.parse(raw)
        self.log(f"USB info: rev={self.info.revision}, state=0x{self.info.state:04x}, "
                 f"{self.info.width}x{self.info.height}, formato={FORMAT_NAMES[self.info.pixel_format]}, "
                 f"flags=0x{self.info.flags:02x}; raw={self.info.raw.hex()}")
        return self.info

    def initialize(self, pixel_format=None):
        self.authenticated = False
        self.read_info()
        self.control(0x03)
        chosen = self.info.pixel_format if pixel_format is None else pixel_format
        if chosen not in FORMAT_NAMES:
            raise ProtocolError("formato solicitado inválido")
        if chosen != self.info.pixel_format and not self.info.supports_mode_command:
            raise ProtocolError("firmware não anuncia o comando para alterar o formato")
        if self.info.supports_mode_command:
            # FUN_1800f3df0 passes info[4:6] in EDX and 1 in R8D.
            self.control(0x04, chosen, 1)
        self.pixel_format = chosen
        if self.info.revision >= 2:
            # D0Entry callback: FUN_1800f3d40. D0Exit sends value 3.
            self.control(0x05, 4)
            self.powered = True
        return self.info

    def authenticate(self, public_key=None):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        self.authenticated = False
        key = public_key or serialization.load_pem_public_key(PUBLIC_KEY_PEM)
        if key.key_size != 2048:
            raise ProtocolError("a autenticação exige uma chave RSA de 2048 bits")
        challenge = bytes(secrets.choice(ALPHANUMERIC) for _ in range(245))
        self.write(struct.pack("<IIQI", MAGIC_AUTH_HOST, 256, 0, MAGIC_AUTH_HOST))
        self.write(key.encrypt(challenge, padding.PKCS1v15()))
        echo = bytes(self.ep_in.read(256, timeout=3000))
        if echo != challenge:
            raise ProtocolError(f"eco RSA divergente: recebidos {len(echo)}/245 bytes")
        self.write(struct.pack("<IIQI", MAGIC_AUTH_DEVICE, 256, 0, MAGIC_AUTH_DEVICE))
        response = recover_device_challenge(key, bytes(self.ep_in.read(256, timeout=3000)))
        self.write(response)
        self.authenticated = True
        self.log("Handshake RSA: eco de 245 bytes validado; resposta ao desafio enviada.")

    def send_frame(self, payload, orientation=0):
        if not self.authenticated:
            raise ProtocolError("envio antes da autenticação")
        # Keep the short header transfer separate, as in FUN_1800f5cc4.
        self.write(frame_header(len(payload), self.pixel_format, orientation))
        self.write(payload)
        self.frames += 1
        self.payload_bytes += len(payload)

    def power_off(self):
        self.authenticated = False
        if self.powered:
            self.control(0x05, 3)
            self.powered = False
