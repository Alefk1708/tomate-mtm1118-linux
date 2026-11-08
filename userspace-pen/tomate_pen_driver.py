#!/usr/bin/env python3
"""
Driver Userspace da Caneta Digitalizadora e ExpressKeys — Tomate MTM-1118 (Bosto)
=================================================================================
Reconstruído via Engenharia Reversa (REA + Ghidra) de:
  - bostohidrouter.sys (SHA-256: 8fb89c69f833fbe9f9da921aaaab4ac7d00d742cf49041842c34e3b04bee072c)
  - bostomouflt.sys    (SHA-256: f72c2b60bf48c111b773c5d3b786eb4339961df94f3c3c31b0b41491436d81ba)
  - TabletDriver.exe   (SHA-256: 28c5db1dc8143f1c699d7d04cb30a3e669d5b0652e3fa30e46963edcf5996a3c)

Recursos implementados (evidências e limites em README.md):
  1. Inicialização USB HID Feature Report ID 0x02: [0x02, 0x04, 0x03, 0x41] (FUN_140002c80 @ 0x14000502c)
  2. Leitura de pacotes da Caneta (Report ID 0x01, 8 ou 10 bytes) com X (0..17920), Y (0..10240),
     Pressão (0..8192) e Tilt X/Y (-64..64) + Azimuth/Altitude (FUN_00422630)
  3. Leitura das ExpressKeys (Report ID 0x04, byte 4 bitmask), teclado (ID 0x03) e botões
     (FUN_004223e0 & FUN_00422890)
  4. Inversão de Eixo (FlipAxis: X = 17920 - X, Y = 10240 - Y) e Inversão de Tela (FlipScreen)
  5. Homografia Projetiva 3x3 de 4 pontos (Calibration Source -> Target, FUN_140001ca4 / FUN_140001ea4)
     + Captura interativa dos 4 pontos de calibração com suavização exponencial 0.9/0.1 (FUN_00422890)
  6. Curva de Bézier quadrática de pressão (FUN_14000137c) + Escala de Faixa de Pressão (PressureMaxValue,
     FUN_00422890) + Filtro de Caneta Passiva (média móvel de 10 amostras) / Caneta Ativa
     (taxa de 5%, FUN_00422ca0, com adaptação do contato leve para Linux)
  7. Controle de flags do Touch Screen via Feature Report ID 0x0B ([0x0B, flags], FUN_00421d90)
  8. Telemetria em tempo real via /run/tomate-mtm1118/state.json para a GUI (barra de pressão ao vivo,
     status da caneta/touch e calibração interativa de 4 pontos).
"""

import os
import copy
import sys
import time
import json
import math
import fcntl
import struct
import signal
import subprocess
import tempfile
from contextlib import contextmanager
from button_schema import migrate_button_settings, keystroke_tokens
from calibration_protocol import (PROTOCOL as CALIBRATION_PROTOCOL, DISPLAY_STATE_PATH,
                                  fresh_json, target_is_displayed)

try:
    import usb.core
    import usb.util
    HAS_USB = True
except ImportError:
    HAS_USB = False

try:
    import evdev
    from evdev import UInput, AbsInfo, ecodes as e
    HAS_EVDEV = True
except ImportError:
    HAS_EVDEV = False

# ============================================================================
# 1. CONSTANTES DE HARDWARE EXTRAÍDAS DO BINÁRIO (bostohidrouter.sys & TabletDriver.exe)
# ============================================================================
# Tabela de dispositivos digitalizadores em TabletDriver.exe @ 0x005d63d0 e bostohidrouter.inf:
SUPPORTED_PEN_DEVICES = [
    (0x0ED1, 0x7821),  # WinMaxGroup Pen Tablet / Bosto 15-Inch Digitizer (Confirmado via lsusb + INF)
    (0x0ED1, 0x7856),  # Bosto Tilt-Capable Digitizer device (Confirmado via bostohidrouter.inf)
    (0x0B57, 0x9091),  # Bosto / Hanvon 15" & 11.6" Digitizer
    (0x0B57, 0x9092),  # Bosto 13" Digitizer
    (0x0B57, 0x8502),  # Bosto 15" Auxiliary Digitizer
    (0x0B57, 0x8521),  # Bosto 21" Digitizer
    (0x0B57, 0x8021),  # Bosto 21" Digitizer (Alt)
    (0x0B57, 0x8516),  # Bosto 16" Digitizer
    (0x0B57, 0x8513),  # Bosto 13" Digitizer (Alt)
    (0x256C, 0x006E),  # Variante OEM 11.6" (GAOMON / Bosto OEM)
    (0x256C, 0x006D),  # Variante OEM 11.6"
    (0x28BD, 0x0905),  # Variante OEM
    (0x0483, 0x5750),  # Variante STM32 Custom HID
]

# Tabela de Touch Screen em TabletDriver.exe @ 0x005d63c0:
SUPPORTED_TOUCH_DEVICES = [
    (0x6066, 0x0001),  # Bosto 15-Inch Touch Screen
    (0x05AC, 0x0265),  # Bosto Touch Screen (Alt)
]

# Descritor HID de 367 bytes (0x16F) em 0x1400040e0:
MAX_X = 17920         # 0x4600 (179.2 mm @ 100 linhas/mm)
MAX_Y = 10240         # 0x2800 (102.4 mm @ 100 linhas/mm)
MAX_PRESSURE = 8192   # 0x2000 (8192 níveis)
MAX_TILT = 64         # [-64, 64]

# Feature Report de Inicialização (0x14000502c / 0x140005030) e Vendor Init (FUN_00420c90):
FEATURE_ENABLE_PEN = bytes([0x02, 0x04, 0x03, 0x41])
FEATURE_DISABLE_PEN = bytes([0x02, 0x04, 0x03, 0x40])
VENDOR_INIT_REPORT_EE = bytes([0xEE, 0x0A, 0x01, 0x01] + [0x00] * 13)

SETTINGS_PATH = "/etc/tomate-mtm1118/settings.json"
LEGACY_CAL_PATH = "/etc/tomate-mtm1118/calibration.json"
STATE_PATH = "/run/tomate-mtm1118/state.json"
CAL_CMD_PATH = "/run/tomate-mtm1118/calibration-command.json"
INPUT_CMD_PATH = "/run/tomate-mtm1118/input-command.json"
CALIBRATION_LEASE_SECONDS = 12

DEFAULT_SETTINGS = {
    "General": {
        "Language": 1046,
        "AutoStart": True,
        "StartMinimize": False,
        "DiscardSetting": False,
        "TargetDisplay": " Monitor 1"
    },
    "Digitizer": {
        "ButtonLayout": "windows",
        "ButtonReportVersion": 2,
        "TouchFlags": 6,               # Bit 0: DisableAllTouch(1), Bit 1: DisableSingleTouch(2), Bit 2: PenPriority(4)
        "WindowsInkEnabled": True,
        "FlipAxis": False,
        "FlipScreen": False,
        "PressureDamping": 5,          # 0..10 (Mais macio .. Mais firme)
        "PressureDampingMaxValue": 10,
        "PressureMaxValue": 4096,      # 1..8191 (Faixa de pressão)
        "CalibrationValid": False,
        "PenType": 0,                  # 0 = Caneta passiva, 1 = Caneta ativa
        "PressureRatio": 0.6,
        "Calibration": {
            "Source": f"0:0, {MAX_X}:0, {MAX_X}:{MAX_Y}, 0:{MAX_Y}",
            "Target": f"0:0, {MAX_X}:0, {MAX_X}:{MAX_Y}, 0:{MAX_Y}"
        },
        "Buttons": {
            "Button0": {"Action": "Keystroke", "Value": "+Ctrl+Z"},
            "Button1": {"Action": "Keystroke", "Value": "+Ctrl+Shift+Z"},
            "Button2": {"Action": "Keystroke", "Value": "+Ctrl+="},
            "Button3": {"Action": "Keystroke", "Value": "+Ctrl+-"},
            "Button4": {"Action": "Keystroke", "Value": "["},
            "Button5": {"Action": "Keystroke", "Value": "]"},
            "Button6": {"Action": "Keystroke", "Value": "B"},
            "Button7": {"Action": "Keystroke", "Value": "E"},
            "Button8": {"Action": "Middle Click", "Value": ""},
            "Button9": {"Action": "Right Click", "Value": ""},
            "Button10": {"Action": "Disabled", "Value": ""}
        }
    }
}


# ============================================================================
# 2. CURVA DE PRESSÃO BÉZIER E FILTROS (FUN_14000137c, FUN_00422890, FUN_00422ca0)
# ============================================================================
class PressureProcessor:
    """
    Reproduz exatamente o pipeline de pressão de bostohidrouter.sys (FUN_14000137c / FUN_1400025c0)
    e TabletDriver.exe (FUN_004214d0 / FUN_00422890 / FUN_00422ca0):
      1. Escala inicial por PressureMaxValue (slider_pressurerange, 1..8191):
         denom = 8192 - PressureMaxValue
         p_scaled = clamp((raw_p / denom) * 8192, 0, 8192)
      2. Avaliação da curva de Bézier quadrática B(t) em t = p_scaled / 8192.0:
         P0 = (0.0, 0.0)
         P1 = (d, 1.0 - d), onde d = PressureDamping / PressureDampingMaxValue
         P2 = (1.0, 1.0)
         y(t) = 2*(1-t)*t*(1.0 - d) + t^2
      3. Filtro dependente de PenType (FUN_00422ca0):
         - PenType == 0 (Caneta passiva): média móvel circular de até 10 amostras
         - PenType == 1 (Caneta ativa): limite de variação de 5% (409.6/amostra).
           O limiar adicional de 300 do Windows não é aplicado ao contato já validado.
    """
    def __init__(self):
        self.damping = 5
        self.damping_max = 10
        self.pressure_max_value = 4096
        self.pen_type = 0
        self.history = []
        self.prev_active_p = 0

    def update_params(self, damping: int, damping_max: int, pressure_max_val: int, pen_type: int):
        old = self.damping, self.damping_max, self.pressure_max_value, self.pen_type
        self.damping = max(0, min(damping_max, int(damping)))
        self.damping_max = max(1, int(damping_max))
        self.pressure_max_value = max(1, min(8191, int(pressure_max_val)))
        self.pen_type = int(pen_type)
        if old != (self.damping, self.damping_max, self.pressure_max_value, self.pen_type):
            self.reset_stroke()

    def reset_stroke(self):
        self.history.clear()
        self.prev_active_p = 0

    def evaluate(self, raw_p: int, tip_down: bool) -> int:
        # A pressure sample can remain in a hover/barrel-button report. The
        # active-pen smoothing filter must never turn it into a new contact.
        if raw_p <= 0 or not tip_down:
            self.reset_stroke()
            return 0

        # 1. Escala pela faixa de pressão (FUN_00422890: iVar2 = 0x2000 - PressureMaxValue)
        denom = max(1, MAX_PRESSURE - self.pressure_max_value)
        scaled = int((float(raw_p) / float(denom)) * MAX_PRESSURE)
        scaled = max(0, min(MAX_PRESSURE, scaled))

        # 2. Curva de Bézier quadrática avaliada diretamente em t (FUN_14000137c & FUN_00422ca0)
        t = scaled / float(MAX_PRESSURE)
        d = float(self.damping) / float(self.damping_max)
        p1_y = 1.0 - d
        y_t = 2.0 * (1.0 - t) * t * p1_y + (t * t)
        curved = int(round(y_t * MAX_PRESSURE))
        curved = max(0, min(MAX_PRESSURE, curved))

        # 3. Filtro por tipo de caneta (FUN_00422ca0)
        if self.pen_type == 0:
            # Caneta passiva: buffer circular de até 10 amostras
            self.history.append(curved)
            if len(self.history) > 10:
                self.history.pop(0)
            avg_p = int(round(sum(self.history) / len(self.history)))
            return max(1, min(MAX_PRESSURE, avg_p))
        else:
            # Keep the observed 5% slew rate. Contact is already validated by
            # the tip bit + raw force; a second deadzone loses light clicks.
            max_step = int(MAX_PRESSURE * 0.05)
            if abs(self.prev_active_p - curved) > max_step:
                if curved < self.prev_active_p:
                    curved = self.prev_active_p - max_step
                else:
                    curved = self.prev_active_p + max_step
            self.prev_active_p = max(1, curved)
            return min(MAX_PRESSURE, self.prev_active_p)


# ============================================================================
# 3. CALIBRAÇÃO HOMOGRAFIA PROJETIVA DE 4 PONTOS (FUN_140001ca4 / FUN_140001ea4 / FUN_140002134)
# ============================================================================
def parse_point_list(pts_str: str):
    """Converte 'x0:y0, x1:y1, x2:y2, x3:y3' em lista de 4 tuplas [(x0,y0), ...]."""
    pts = []
    for item in pts_str.split(","):
        item = item.strip()
        if ":" in item:
            x_s, y_s = item.split(":", 1)
            pts.append((float(x_s.strip()), float(y_s.strip())))
    if len(pts) != 4:
        return [(0.0, 0.0), (float(MAX_X), 0.0), (float(MAX_X), float(MAX_Y)), (0.0, float(MAX_Y))]
    return pts


class FourPointCalibration:
    """
    Reproduz exatamente FUN_140001ea4 (matriz projetiva canônica -> quadrilátero),
    FUN_140001ca4 (matriz adjunta 3x3) e FUN_140002134 (multiplicação M = M_src_adj * M_dst)
    sem dependência externa de numpy.
    """
    def __init__(self):
        self.enabled = False
        self.matrix = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]

    @staticmethod
    def _quad_to_matrix(pts):
        (x0, y0), (x1, y1), (x2, y2), (x3, y3) = pts
        dx1 = x1 - x2
        dx2 = x3 - x2
        sx = x0 - x1 + x2 - x3
        dy1 = y1 - y2
        dy2 = y3 - y2
        sy = y0 - y1 + y2 - y3

        det = dx1 * dy2 - dx2 * dy1
        if abs(det) < 1e-12:
            return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]

        g = (sx * dy2 - sy * dx2) / det
        h = (dx1 * sy - dy1 * sx) / det

        a = x1 - x0 + g * x1
        b = x3 - x0 + h * x3
        c = x0
        d = y1 - y0 + g * y1
        e_val = y3 - y0 + h * y3
        f = y0

        # Segue a convenção de vetores-linha usada em FUN_140001ea4 / FUN_00422ca0
        return [
            [a, d, g],
            [b, e_val, h],
            [c, f, 1.0]
        ]

    @staticmethod
    def _adjugate_3x3(m):
        """Reproduz exatamente FUN_140001ca4 (adjunta 3x3 usada para homografia projetiva)."""
        return [
            [
                m[1][1] * m[2][2] - m[1][2] * m[2][1],
                m[0][2] * m[2][1] - m[0][1] * m[2][2],
                m[0][1] * m[1][2] - m[0][2] * m[1][1],
            ],
            [
                m[1][2] * m[2][0] - m[1][0] * m[2][2],
                m[0][0] * m[2][2] - m[0][2] * m[2][0],
                m[0][2] * m[1][0] - m[0][0] * m[1][2],
            ],
            [
                m[1][0] * m[2][1] - m[1][1] * m[2][0],
                m[0][1] * m[2][0] - m[0][0] * m[2][1],
                m[0][0] * m[1][1] - m[0][1] * m[1][0],
            ],
        ]

    @staticmethod
    def _mul_3x3(a, b):
        """Reproduz exatamente FUN_140002134 (multiplicação de matrizes 3x3)."""
        out = [[0.0, 0.0, 0.0] for _ in range(3)]
        for i in range(3):
            for j in range(3):
                out[i][j] = a[i][0] * b[0][j] + a[i][1] * b[1][j] + a[i][2] * b[2][j]
        return out

    def update_from_points(self, src_pts, dst_pts, valid: bool):
        self.enabled = bool(valid)
        if not self.enabled:
            self.matrix = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
            return
        for points in (src_pts, dst_pts):
            if len(points) != 4 or any(len(p) != 2 or not all(math.isfinite(v) for v in p) for p in points):
                self.enabled = False
                raise ValueError("Calibração requer quatro pontos finitos.")
            crosses = []
            for i in range(4):
                a, b, c = points[i], points[(i + 1) % 4], points[(i + 2) % 4]
                crosses.append((b[0]-a[0])*(c[1]-b[1]) - (b[1]-a[1])*(c[0]-b[0]))
            if not (all(v > 1e-6 for v in crosses) or all(v < -1e-6 for v in crosses)):
                self.enabled = False
                raise ValueError("Pontos de calibração degenerados ou fora de ordem.")
        m_src = self._quad_to_matrix(src_pts)
        m_dst = self._quad_to_matrix(dst_pts)
        m_src_adj = self._adjugate_3x3(m_src)
        self.matrix = self._mul_3x3(m_src_adj, m_dst)

    def transform(self, x: int, y: int):
        if not self.enabled:
            return x, y
        m = self.matrix
        fx, fy = float(x), float(y)
        rx = fx * m[0][0] + fy * m[1][0] + m[2][0]
        ry = fx * m[0][1] + fy * m[1][1] + m[2][1]
        rw = fx * m[0][2] + fy * m[1][2] + m[2][2]
        if abs(rw) < 1e-12:
            return x, y
        tx = int(round(rx / rw))
        ty = int(round(ry / rw))
        tx = max(0, min(MAX_X, tx))
        ty = max(0, min(MAX_Y, ty))
        return tx, ty


# ============================================================================
# 4. MAPEAMENTO DE TECLAS E BOTÕES (Button0..Button10 de cfgtoolsettings.xml)
# ============================================================================
KEY_MAP = {}
if HAS_EVDEV:
    KEY_MAP = {
        "A": e.KEY_A, "B": e.KEY_B, "C": e.KEY_C, "D": e.KEY_D, "E": e.KEY_E,
        "F": e.KEY_F, "G": e.KEY_G, "H": e.KEY_H, "I": e.KEY_I, "J": e.KEY_J,
        "K": e.KEY_K, "L": e.KEY_L, "M": e.KEY_M, "N": e.KEY_N, "O": e.KEY_O,
        "P": e.KEY_P, "Q": e.KEY_Q, "R": e.KEY_R, "S": e.KEY_S, "T": e.KEY_T,
        "U": e.KEY_U, "V": e.KEY_V, "W": e.KEY_W, "X": e.KEY_X, "Y": e.KEY_Y,
        "Z": e.KEY_Z,
        "0": e.KEY_0, "1": e.KEY_1, "2": e.KEY_2, "3": e.KEY_3, "4": e.KEY_4,
        "5": e.KEY_5, "6": e.KEY_6, "7": e.KEY_7, "8": e.KEY_8, "9": e.KEY_9,
        "F1": e.KEY_F1, "F2": e.KEY_F2, "F3": e.KEY_F3, "F4": e.KEY_F4,
        "F5": e.KEY_F5, "F6": e.KEY_F6, "F7": e.KEY_F7, "F8": e.KEY_F8,
        "F9": e.KEY_F9, "F10": e.KEY_F10, "F11": e.KEY_F11, "F12": e.KEY_F12,
        "[": e.KEY_LEFTBRACE, "]": e.KEY_RIGHTBRACE,
        "-": e.KEY_MINUS, "=": e.KEY_EQUAL, "+": e.KEY_KPPLUS,
        "SPACE": e.KEY_SPACE, "SPACEBAR": e.KEY_SPACE, "BARRA DE ESPAÇO": e.KEY_SPACE,
        "TAB": e.KEY_TAB, "ENTER": e.KEY_ENTER, "RETURN": e.KEY_ENTER,
        "ESC": e.KEY_ESC, "BACKSPACE": e.KEY_BACKSPACE, "DELETE": e.KEY_DELETE,
        "DEL": e.KEY_DELETE, "INS": e.KEY_INSERT, "HOME": e.KEY_HOME, "END": e.KEY_END,
        "PGUP": e.KEY_PAGEUP, "PGDN": e.KEY_PAGEDOWN,
        "UP": e.KEY_UP, "DOWN": e.KEY_DOWN, "LEFT": e.KEY_LEFT, "RIGHT": e.KEY_RIGHT,
        ",": e.KEY_COMMA, ".": e.KEY_DOT, "/": e.KEY_SLASH, "\\": e.KEY_BACKSLASH,
        ";": e.KEY_SEMICOLON, "'": e.KEY_APOSTROPHE, "`": e.KEY_GRAVE,
    }
    KEY_MAP.update({f"F{i}": getattr(e, f"KEY_F{i}") for i in range(13, 25)})


def parse_keystroke_string(val_str: str):
    """Converte strings como '+Ctrl+Shift+Z' ou '[' em lista de keycodes evdev."""
    if not HAS_EVDEV or not val_str:
        return []
    codes = []
    tokens = keystroke_tokens(val_str)
    for tok in tokens:
        u = tok.upper()
        if u in ("CTRL", "CONTROL", "LCTRL"):
            codes.append(e.KEY_LEFTCTRL)
        elif u in ("SHIFT", "LSHIFT"):
            codes.append(e.KEY_LEFTSHIFT)
        elif u in ("ALT", "LALT"):
            codes.append(e.KEY_LEFTALT)
        elif u in ("WIN", "SUPER", "META"):
            codes.append(e.KEY_LEFTMETA)
        elif u in KEY_MAP:
            codes.append(KEY_MAP[u])
    return codes


# Report 0x03 is a keyboard collection in the extracted 367-byte descriptor,
# with five key usages plus one vendor byte. It is not the 0x04 pad mask.
HID_KEY_MAP = {}
HID_MODIFIERS = []
if HAS_EVDEV:
    HID_KEY_MAP.update({4 + i: getattr(e, "KEY_" + letter) for i, letter in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")})
    HID_KEY_MAP.update({30 + i: getattr(e, "KEY_" + number) for i, number in enumerate("1234567890")})
    HID_KEY_MAP.update({58 + i: getattr(e, "KEY_F" + str(i + 1)) for i in range(12)})
    HID_KEY_MAP.update({usage: getattr(e, "KEY_" + name) for usage, name in {
        40: "ENTER", 41: "ESC", 42: "BACKSPACE", 43: "TAB", 44: "SPACE",
        45: "MINUS", 46: "EQUAL", 47: "LEFTBRACE", 48: "RIGHTBRACE", 49: "BACKSLASH",
        51: "SEMICOLON", 52: "APOSTROPHE", 53: "GRAVE", 54: "COMMA", 55: "DOT",
        56: "SLASH", 57: "CAPSLOCK", 70: "SYSRQ", 71: "SCROLLLOCK", 72: "PAUSE",
        73: "INSERT", 74: "HOME", 75: "PAGEUP", 76: "DELETE", 77: "END", 78: "PAGEDOWN",
        79: "RIGHT", 80: "LEFT", 81: "DOWN", 82: "UP", 83: "NUMLOCK",
        84: "KPSLASH", 85: "KPASTERISK", 86: "KPMINUS", 87: "KPPLUS", 88: "KPENTER",
        89: "KP1", 90: "KP2", 91: "KP3", 92: "KP4", 93: "KP5", 94: "KP6",
        95: "KP7", 96: "KP8", 97: "KP9", 98: "KP0", 99: "KPDOT", 100: "102ND"
    }.items()})
    HID_MODIFIERS = [e.KEY_LEFTCTRL, e.KEY_LEFTSHIFT, e.KEY_LEFTALT, e.KEY_LEFTMETA,
                     e.KEY_RIGHTCTRL, e.KEY_RIGHTSHIFT, e.KEY_RIGHTALT, e.KEY_RIGHTMETA]


def decode_keyboard_report(data):
    if len(data) < 8 or data[0] != 3:
        return None
    usages = data[3:8]
    if any(usage in (1, 2, 3) for usage in usages):
        return None  # HID rollover: retain the last valid state.
    keys = {code for bit, code in enumerate(HID_MODIFIERS) if data[1] & (1 << bit)}
    keys.update(HID_KEY_MAP[usage] for usage in usages if usage in HID_KEY_MAP)
    return keys


# ============================================================================
# 5. DAEMON PRINCIPAL DO DIGITALIZADOR E EXPRESSKEYS
# ============================================================================
class TomatePenDaemon:
    def __init__(self):
        self.settings = json.loads(json.dumps(DEFAULT_SETTINGS))
        self.settings_mtime = 0.0
        self.settings_stamp = None
        self.pressure_proc = PressureProcessor()
        self.calibration = FourPointCalibration()
        self.ui_pen = None
        self.ui_keys = None
        self.button_states = [0] * 11  # Button0..7 (Tablet) + Button8..10 (Pen)
        self.active_keycodes = {}
        self.keyboard_pressed = set()
        self.raw_reports = {}
        self.blocked_buttons = set()
        self.last_pen_input = {}
        self.last_input_reset = ''

        # Estado de calibração interativa de 4 pontos (FUN_00422890)
        self.cal_mode = False
        self.cal_session_id = ""
        self.cal_sampling = False
        self.cal_step = 0
        self.cal_waiting_release = False
        self.cal_waiting_press = False
        self.cal_protocol = CALIBRATION_PROTOCOL
        self.cal_phase = 'idle'
        self.cal_error = ''
        self.cal_command_id = ''
        self.cal_renderer = ''
        self.cal_flip_screen = False
        self.cal_feedback = ''
        self.physical_tip_down = None
        self.cal_last_command = 0.0
        self.cal_points = [[0, 0], [0, 0], [0, 0], [0, 0]]

        self.load_settings(force=True)
        if HAS_EVDEV:
            self._setup_uinput()

    def _setup_uinput(self):
        # 1. Dispositivo UInput da Caneta Digitalizadora (Stylus)
        pen_cap = {
            e.EV_KEY: [
                e.BTN_TOUCH,
                e.BTN_TOOL_PEN,
                e.BTN_TOOL_RUBBER,
                e.BTN_STYLUS,
                e.BTN_STYLUS2,
            ],
            e.EV_ABS: [
                (e.ABS_X, AbsInfo(value=0, min=0, max=MAX_X, fuzz=4, flat=0, resolution=100)),
                (e.ABS_Y, AbsInfo(value=0, min=0, max=MAX_Y, fuzz=4, flat=0, resolution=100)),
                (e.ABS_PRESSURE, AbsInfo(value=0, min=0, max=MAX_PRESSURE, fuzz=0, flat=0, resolution=0)),
                (e.ABS_TILT_X, AbsInfo(value=0, min=-MAX_TILT, max=MAX_TILT, fuzz=0, flat=0, resolution=0)),
                (e.ABS_TILT_Y, AbsInfo(value=0, min=-MAX_TILT, max=MAX_TILT, fuzz=0, flat=0, resolution=0)),
            ]
        }
        # INPUT_PROP_DIRECT: informa ao libinput/compositor que a caneta escreve
        # diretamente sobre uma tela (pen display), e não sobre uma mesa opaca.
        try:
            self.ui_pen = UInput(
                pen_cap,
                name="Tomate MTM-1118 Bosto Stylus Pen",
                vendor=0x0ED1,
                product=0x7821,
                version=0x0110,
                input_props=[e.INPUT_PROP_DIRECT],
            )
        except TypeError:
            # python-evdev antigo (< 1.1) sem suporte a input_props
            self.ui_pen = UInput(
                pen_cap,
                name="Tomate MTM-1118 Bosto Stylus Pen",
                vendor=0x0ED1,
                product=0x7821,
                version=0x0110,
            )

        # 2. Dispositivo UInput das ExpressKeys e Ações de Mouse/Teclado
        all_keys = list(set(KEY_MAP.values()) | set(HID_KEY_MAP.values()) | set(HID_MODIFIERS)) + [
            e.KEY_LEFTCTRL, e.KEY_LEFTSHIFT, e.KEY_LEFTALT, e.KEY_LEFTMETA,
            e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE,
        ]
        key_cap = {
            e.EV_KEY: all_keys,
            e.EV_REL: [e.REL_WHEEL],
        }
        self.ui_keys = UInput(
            key_cap,
            name="Tomate MTM-1118 Bosto ExpressKeys",
            vendor=0x0ED1,
            product=0x7821,
            version=0x0110,
        )

    def load_settings(self, force=False):
        stamp = None
        candidate = copy.deepcopy(self.settings)
        try:
            if os.path.exists(SETTINGS_PATH):
                with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                    stat = os.fstat(f.fileno())
                    stamp = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
                    if not force and stamp == getattr(self, 'settings_stamp', None):
                        return False
                    loaded = json.load(f)
                if not isinstance(loaded, dict) or any(not isinstance(loaded.get(sec, {}), dict) for sec in ('General', 'Digitizer')):
                    raise ValueError('As seções General e Digitizer devem ser objetos JSON.')
                # Files predating the Windows slot correction retain their
                # physical button assignments until explicitly migrated.
                if "ButtonLayout" not in loaded.get("Digitizer", {}):
                    loaded.setdefault("Digitizer", {})["ButtonLayout"] = "legacy"
                loaded = migrate_button_settings(loaded)
                # Mescla com DEFAULT_SETTINGS
                for sec in ("General", "Digitizer"):
                    if sec in loaded and isinstance(loaded[sec], dict):
                        candidate[sec].update(loaded[sec])
                if not isinstance(candidate['Digitizer'].get('Buttons', {}), dict):
                    raise ValueError('Buttons deve ser um objeto JSON.')
            elif not force:
                return False
        except Exception as exc:
            print(f"[AVISO] Erro ao carregar {SETTINGS_PATH}: {exc}")
            return False

        old_dig = self.settings['Digitizer']
        new_dig = candidate['Digitizer']
        changed = [i for i in range(11) if (old_dig.get('Buttons', {}).get(f'Button{i}') != new_dig.get('Buttons', {}).get(f'Button{i}')
                   or old_dig.get('ButtonLayout') != new_dig.get('ButtonLayout'))]
        if not hasattr(self, 'blocked_buttons'):
            self.blocked_buttons = set()
        for index in changed:
            if index in self.active_keycodes and self.ui_keys is not None:
                self._set_button_keys(index, [])
            if self.button_states[index]:
                self.blocked_buttons.add(index)  # New action begins on the next physical press.
        self.settings = candidate
        self.settings_stamp = stamp
        self.settings_mtime = stat.st_mtime if stamp is not None else 0.0
        if changed and self.ui_pen is not None:
            right, middle = self._native_pen_buttons()
            self.ui_pen.write(e.EV_KEY, e.BTN_STYLUS, int(right))
            self.ui_pen.write(e.EV_KEY, e.BTN_STYLUS2, int(middle))
            self.ui_pen.syn()
        if changed:
            print(f'[CONFIG] Botões atualizados: {changed}. Botões mantidos devem ser soltos antes da nova ação.')

        dig = self.settings["Digitizer"]
        try:
            self.pressure_proc.update_params(
                int(dig.get("PressureDamping", 5)),
                int(dig.get("PressureDampingMaxValue", 10)),
                int(dig.get("PressureMaxValue", 4096)),
                int(dig.get("PenType", 0)),
            )
        except (TypeError, ValueError, OverflowError):
            print("[PRESSÃO] Valores inválidos; preservando os parâmetros anteriores.")

        cal_cfg = dig.get("Calibration", {})
        try:
            src_pts = parse_point_list(cal_cfg.get("Source", DEFAULT_SETTINGS["Digitizer"]["Calibration"]["Source"]))
            dst_pts = parse_point_list(cal_cfg.get("Target", DEFAULT_SETTINGS["Digitizer"]["Calibration"]["Target"]))
        except (AttributeError, TypeError, ValueError):
            self.calibration.enabled = False
            print("[CALIBRAÇÃO] Formato inválido; usando coordenadas sem calibração.")
            return True
        cal_valid = bool(dig.get("CalibrationValid", False))

        # Compatibilidade com /etc/tomate-mtm1118/calibration.json legado
        if not cal_valid and os.path.exists(LEGACY_CAL_PATH):
            try:
                with open(LEGACY_CAL_PATH, "r", encoding="utf-8") as f:
                    leg = json.load(f)
                if "source_points" in leg and "target_points" in leg:
                    src_pts = [tuple(p) for p in leg["source_points"]]
                    dst_pts = [tuple(p) for p in leg["target_points"]]
                    cal_valid = True
            except Exception:
                pass

        try:
            self.calibration.update_from_points(src_pts, dst_pts, cal_valid)
        except ValueError as exc:
            print(f"[CALIBRAÇÃO] {exc} Usando coordenadas sem calibração.")
        return True

    def check_input_command(self, pen_ok=True):
        path = INPUT_CMD_PATH
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'r') as stream:
                command = json.loads(stream.read(4096))
            os.unlink(path)
            if command.get('action') == 'release':
                self.cal_mode = False
                self.cal_sampling = False
                self.release_input()
                self.last_input_reset = str(command.get('request_id', ''))[:128]
                self.write_state(pen_ok, False)
                print('[INPUT] Teclas, botões e proximidade liberados a pedido do usuário.')
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError) as exc:
            print(f'[INPUT] Comando de recuperação inválido: {exc}')

    def _native_pen_buttons(self):
        dig = self.settings['Digitizer']
        result = [False, False]
        if dig.get('ButtonLayout', 'legacy') == 'windows':
            for index in (8, 9, 10):
                cfg = dig.get('Buttons', {}).get(f'Button{index}', {})
                action = cfg.get('Action', '') if isinstance(cfg, dict) else ''
                down = self.button_states[index] and index not in getattr(self, 'blocked_buttons', set())
                if action in ('Right Click', 'Clique direito'): result[0] |= bool(down)
                if action in ('Middle Click', 'Clique do meio'): result[1] |= bool(down)
        return result

    def abort_calibration(self, reason):
        if not self.cal_mode:
            return
        self.cal_mode = self.cal_sampling = False
        self.cal_waiting_release = self.cal_waiting_press = False
        self.cal_phase, self.cal_error = 'cancelled', reason
        self.release_input()

    def check_calibration_timeout(self, pen_ok=True, touch_ok=False):
        if (self.cal_mode and getattr(self, 'cal_protocol', 1) >= 2
                and time.monotonic() - self.cal_last_command > CALIBRATION_LEASE_SECONDS):
            self.abort_calibration('A janela de calibração deixou de responder. Abra a calibração novamente.')
            self.write_state(pen_ok, touch_ok)

    def check_calibration_command(self, pen_ok=True, touch_ok=False):
        """Verifica se a GUI enviou comando para iniciar/cancelar a calibração interativa de 4 pontos."""
        if not os.path.exists(CAL_CMD_PATH):
            return
        try:
            fd = os.open(CAL_CMD_PATH, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "r", encoding="utf-8") as f:
                cmd = json.loads(f.read(16384))
            os.remove(CAL_CMD_PATH)
            action = cmd.get("action", "")
            if action == "start":
                session = cmd.get('request_id', '')
                protocol = cmd.get('protocol', 1)
                if not isinstance(session, str) or not session or len(session) > 128 or type(protocol) is not int or protocol not in (1, 2, CALIBRATION_PROTOCOL):
                    raise ValueError('Pedido de calibração inválido')
                if self.cal_mode and session == self.cal_session_id and protocol == self.cal_protocol and protocol >= 2:
                    self.cal_last_command = time.monotonic()
                    self.cal_command_id = str(cmd.get('command_id', ''))[:128]
                    self.write_state(pen_ok, touch_ok)
                    return
                self.release_input()
                self.cal_session_id = session
                self.cal_protocol = protocol
                self.cal_renderer = 'usb' if protocol == CALIBRATION_PROTOCOL else 'window'
                self.cal_flip_screen = bool(self.settings['Digitizer'].get('FlipScreen', False))
                self.cal_feedback = ''
                self.cal_sampling = False
                self.cal_mode = True
                self.cal_step = 0
                self.cal_waiting_release = False
                self.cal_waiting_press = protocol == 1
                self.cal_points = [[0, 0], [0, 0], [0, 0], [0, 0]]
                self.cal_phase = 'waiting_target' if protocol >= 2 else 'waiting_touch'
                self.cal_error = ''
                self.cal_last_command = time.monotonic()
                self.cal_command_id = str(cmd.get('command_id', ''))[:128]
                if not pen_ok:
                    self.abort_calibration('A caneta está desconectada. Conecte-a e abra a calibração novamente.')
                print("[CALIBRAÇÃO] Modo interativo de 4 pontos iniciado (pos 0)")
                self.write_state(pen_ok, touch_ok)
            elif action in ('arm', 'heartbeat'):
                if (not self.cal_mode or cmd.get('request_id') != self.cal_session_id
                        or getattr(self, 'cal_protocol', 1) < 2):
                    return
                if action == 'arm':
                    step = cmd.get('step')
                    if type(step) is not int or step != self.cal_step or not 0 <= step < 4:
                        return
                    if self.cal_phase == 'waiting_target':
                        if self.cal_protocol == CALIBRATION_PROTOCOL:
                            displayed = fresh_json(DISPLAY_STATE_PATH)
                            if (not target_is_displayed(displayed, self.cal_session_id, step, self.cal_flip_screen)
                                    or cmd.get('display_frame_id') != displayed.get('cal_frame_id')):
                                return  # No USB completion for this physical target yet.
                        self.cal_phase = 'waiting_lift'
                        self.cal_waiting_release, self.cal_waiting_press = True, False
                        if self.cal_protocol == CALIBRATION_PROTOCOL and self.physical_tip_down is False:
                            self.cal_phase = 'waiting_touch'
                            self.cal_waiting_release, self.cal_waiting_press = False, True
                self.cal_last_command = time.monotonic()
                self.cal_command_id = str(cmd.get('command_id', ''))[:128]
                self.write_state(pen_ok, touch_ok)
            elif action == "cancel":
                if cmd.get("request_id", "") != self.cal_session_id:
                    return
                self.abort_calibration('')
                self.cal_step = 0
                self.cal_command_id = str(cmd.get('command_id', ''))[:128]
                print("[CALIBRAÇÃO] Modo interativo cancelado")
                self.write_state(pen_ok, touch_ok)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            print(f'[CALIBRAÇÃO] Comando inválido: {exc}')

    def _capture_calibration_sample(self, x, y, tip, barrel=False):
        if getattr(self, 'cal_protocol', 1) >= 2:
            if self.cal_phase == 'waiting_target':
                return  # The GUI has not confirmed drawing this target yet.
            if self.cal_waiting_release:
                if not tip:
                    self.cal_waiting_release, self.cal_waiting_press = False, True
                    self.cal_phase = 'waiting_touch'
                return
            if not self.cal_sampling and (not self.cal_waiting_press or barrel):
                return
            if tip and barrel:
                if self.cal_protocol == CALIBRATION_PROTOCOL and self.cal_sampling:
                    self.cal_sampling = False
                    self.cal_waiting_release, self.cal_waiting_press = True, False
                    self.cal_phase = 'waiting_lift'
                return  # Barrel-button reports are not calibration touches.
        if self.cal_step < 4 and tip:
            if not self.cal_sampling:
                self.cal_points[self.cal_step] = [x, y]
                self.cal_sampling = True
                self.cal_waiting_press = False
                self.cal_phase = 'sampling'
            else:
                px, py = self.cal_points[self.cal_step]
                self.cal_points[self.cal_step] = [int(round(px * .9 + x * .1)), int(round(py * .9 + y * .1))]
        elif self.cal_sampling:
            self.cal_sampling = False
            if getattr(self, 'cal_protocol', 1) == CALIBRATION_PROTOCOL:
                x, y = self.cal_points[self.cal_step]
                if any(((x - px) / MAX_X) ** 2 + ((y - py) / MAX_Y) ** 2 < .15 ** 2
                       for px, py in self.cal_points[:self.cal_step]):
                    self.cal_waiting_release, self.cal_waiting_press = True, False
                    self.cal_phase = 'waiting_lift'
                    self.cal_feedback = 'Toque a nova mira; o ponto ficou próximo demais do anterior.'
                    return
            self.cal_feedback = ''
            self.cal_step += 1
            self.cal_mode = self.cal_step < 4
            self.cal_phase = 'waiting_target' if self.cal_mode else 'complete'
            if getattr(self, 'cal_protocol', 1) == 1:
                self.cal_waiting_press = self.cal_mode

    def write_state(self, pen_ok: bool, touch_ok: bool, raw_x=0, raw_y=0, cal_x=0, cal_y=0,
                    pressure=0, tilt_x=0, tilt_y=0, in_range=False, quicktool_req=False):
        """Atualiza telemetria em /run/tomate-mtm1118/state.json para a GUI."""
        if quicktool_req:
            self.quicktool_timestamp = time.time()
        state = {
            "timestamp": time.time(),
            "pen_connected": pen_ok,
            "touch_connected": touch_ok,
            "in_range": in_range,
            "raw_x": raw_x,
            "raw_y": raw_y,
            "cal_x": cal_x,
            "cal_y": cal_y,
            "pressure": pressure,
            "tilt_x": tilt_x,
            "tilt_y": tilt_y,
            "cal_mode": self.cal_mode,
            "cal_session_id": getattr(self, "cal_session_id", ""),
            "cal_step": self.cal_step,
            "cal_points": self.cal_points,
            "cal_protocol": getattr(self, 'cal_protocol', 1),
            "cal_phase": getattr(self, 'cal_phase', 'idle'),
            "cal_error": getattr(self, 'cal_error', ''),
            "cal_command_id": getattr(self, 'cal_command_id', ''),
            "cal_renderer": getattr(self, 'cal_renderer', ''),
            "cal_flip_screen": getattr(self, 'cal_flip_screen', False),
            "cal_feedback": getattr(self, 'cal_feedback', ''),
            "quicktool_trigger": quicktool_req,
            "quicktool_timestamp": getattr(self, 'quicktool_timestamp', 0),
            "raw_reports": self.raw_reports,
            "input": getattr(self, 'last_pen_input', {}),
            "held_keycodes": sorted(set().union(self.keyboard_pressed, *self.active_keycodes.values())),
            "button_states": list(self.button_states),
            "blocked_buttons": sorted(getattr(self, 'blocked_buttons', set())),
            "last_input_reset": getattr(self, 'last_input_reset', ''),
        }
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=os.path.dirname(STATE_PATH), delete=False) as f:
                tmp_file = f.name
                json.dump(state, f)
                os.fchmod(f.fileno(), 0o640)
            os.replace(tmp_file, STATE_PATH)
        except Exception:
            pass

    def _set_button_keys(self, btn_idx, codes):
        before = set().union(self.keyboard_pressed, *self.active_keycodes.values())
        if codes:
            self.active_keycodes[btn_idx] = list(codes)
        else:
            self.active_keycodes.pop(btn_idx, None)
        after = set().union(self.keyboard_pressed, *self.active_keycodes.values())
        for code in sorted(before - after, reverse=True):
            self.ui_keys.write(e.EV_KEY, code, 0)
        for code in sorted(after - before, key=lambda c: (c not in HID_MODIFIERS, c)):
            self.ui_keys.write(e.EV_KEY, code, 1)
        self.ui_keys.syn()

    def execute_button_action(self, btn_idx: int, pressed: bool):
        """
        Executa a ação configurada para Button0..Button7 (ExpressKeys) ou Button8..Button10 (Caneta),
        idêntico ao dispatch de vtable em FUN_004223e0 / FUN_00422890.
        """
        if not HAS_EVDEV or self.ui_keys is None:
            return False
        # Release what this press actually held, even if settings changed.
        if not pressed and btn_idx in self.active_keycodes:
            self._set_button_keys(btn_idx, [])
            return False

        buttons_cfg = self.settings["Digitizer"].get("Buttons", {})
        btn_cfg = buttons_cfg.get(f"Button{btn_idx}", {"Action": "Disabled", "Value": ""})
        if isinstance(btn_cfg, str):
            btn_cfg = {"Action": "Keystroke", "Value": btn_cfg}

        action = btn_cfg.get("Action", "Disabled")
        val = btn_cfg.get("Value", "")

        if action in ("Disabled", "Desativado", ""):
            return False

        if action in ("Keystroke", "Tecla de atalho"):
            self._set_button_keys(btn_idx, parse_keystroke_string(val) if pressed else [])

        elif action in ("Undo", "Desfazer"):
            self._set_button_keys(btn_idx, [e.KEY_LEFTCTRL, e.KEY_Z] if pressed else [])

        elif action in ("Redo", "Refazer"):
            self._set_button_keys(btn_idx, [e.KEY_LEFTCTRL, e.KEY_LEFTSHIFT, e.KEY_Z] if pressed else [])

        elif action in ("Left Click", "Clique esquerdo"):
            self._set_button_keys(btn_idx, [e.BTN_LEFT] if pressed else [])

        elif action in ("Right Click", "Clique direito"):
            self._set_button_keys(btn_idx, [e.BTN_RIGHT] if pressed else [])

        elif action in ("Middle Click", "Clique do meio"):
            self._set_button_keys(btn_idx, [e.BTN_MIDDLE] if pressed else [])

        elif action in ("Double Click", "Clique duplo") and pressed:
            for _ in range(2):
                self.ui_keys.write(e.EV_KEY, e.BTN_LEFT, 1)
                self.ui_keys.syn()
                self.ui_keys.write(e.EV_KEY, e.BTN_LEFT, 0)
                self.ui_keys.syn()

        elif action in ("Wheel Up", "Rolar para cima") and pressed:
            self.ui_keys.write(e.EV_REL, e.REL_WHEEL, 1)
            self.ui_keys.syn()

        elif action in ("Wheel Down", "Rolar para baixo") and pressed:
            self.ui_keys.write(e.EV_REL, e.REL_WHEEL, -1)
            self.ui_keys.syn()

        elif action in ("Run Application", "Executar aplicativo") and pressed and val:
            # Config is user-editable, but USB/uinput require a privileged
            # process. Never execute user commands under that identity.
            print("[BOTÃO] Executar aplicativo deve ser tratado pela sessão do usuário; ação não executada como root.")

        elif action in ("QuickTool", "Ferramenta rápida") and pressed:
            return True

        return False

    def handle_expresskeys_packet(self, data: bytes):
        """
        Processa pacotes de ExpressKeys do tablet (Report ID 0x04, 8 bytes),
        idêntico a FUN_004223e0 (0x4223e0) em TabletDriver.exe.
        """
        if len(data) != 8 or data[0] != 0x04:
            return False
        mask = data[4]
        quicktool_req = False
        for i in range(8):
            is_down = 1 if (mask & (1 << i)) else 0
            blocked = i in getattr(self, 'blocked_buttons', set())
            if not is_down:
                getattr(self, 'blocked_buttons', set()).discard(i)
            if self.button_states[i] != is_down:
                self.button_states[i] = is_down
                if not blocked and self.execute_button_action(i, bool(is_down)):
                    quicktool_req = True
        return quicktool_req

    def handle_keyboard_packet(self, data):
        keys = decode_keyboard_report(data)
        if keys is None or self.ui_keys is None:
            return
        held = set().union(*self.active_keycodes.values())
        for code in sorted(self.keyboard_pressed - keys - held):
            self.ui_keys.write(e.EV_KEY, code, 0)
        for code in sorted(keys - self.keyboard_pressed - held):
            self.ui_keys.write(e.EV_KEY, code, 1)
        self.keyboard_pressed = keys
        self.ui_keys.syn()

    def release_input(self):
        """Release held input after disconnect or shutdown; never leave a tip/key down."""
        if self.ui_keys is not None:
            codes = set(KEY_MAP.values()) | set(HID_KEY_MAP.values()) | set(HID_MODIFIERS)
            codes.update((e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE))
            for code in codes:
                self.ui_keys.write(e.EV_KEY, code, 0)
            self.ui_keys.syn()
        if self.ui_pen is not None:
            self.ui_pen.write(e.EV_ABS, e.ABS_PRESSURE, 0)
            for code in (e.BTN_TOUCH, e.BTN_TOOL_PEN, e.BTN_TOOL_RUBBER, e.BTN_STYLUS, e.BTN_STYLUS2):
                self.ui_pen.write(e.EV_KEY, code, 0)
            self.ui_pen.syn()
        self.button_states = [0] * 11
        self.active_keycodes.clear()
        self.keyboard_pressed.clear()
        self.pressure_proc.reset_stroke()
        getattr(self, 'blocked_buttons', set()).clear()
        self.last_pen_input = {}

    def handle_pen_packet(self, data: bytes, touch_present: bool, has_tilt: bool = False):
        """
        Processa pacotes da caneta (Report ID 0x01, 8 ou 10 bytes),
        idêntico a FUN_00427190, FUN_00422890, FUN_00422630 e FUN_00422ca0 em TabletDriver.exe.
        """
        if len(data) < 8:
            return

        status = data[1]
        raw_x, raw_y, raw_p = struct.unpack("<HHH", data[2:8])
        tilt_x, tilt_y = 0, 0
        if has_tilt and len(data) >= 10:
            tilt_x, tilt_y = struct.unpack("<bb", data[8:10])
            tilt_x = max(-MAX_TILT, min(MAX_TILT, tilt_x))
            tilt_y = max(-MAX_TILT, min(MAX_TILT, tilt_y))

        # This firmware can set bit01 during a barrel press with zero force.
        # Do not generate a drawing contact from that side-button-only report.
        tip = bool(status & 0x01) and raw_p > 0
        self.physical_tip_down = tip  # Includes samples while calibration suppresses normal input.
        barrel1 = bool(status & 0x02)
        barrel2 = bool(status & 0x04)
        invert = bool(status & 0x08)
        in_range = bool(status & 0x10) or tip or barrel1 or barrel2 or bool(status & 0x20)

        dig = self.settings["Digitizer"]
        windows_layout = dig.get("ButtonLayout", "legacy") == "windows"

        # 1. FlipAxis (FUN_00422890: aplicado ANTES da calibração!)
        x, y = raw_x, raw_y
        if dig.get("FlipAxis", False):
            x = MAX_X - x
            y = MAX_Y - y

        # 2. Modo de Calibração Interativa de 4 Pontos (FUN_00422890 @ 0x422a50)
        if self.cal_mode:
            self._capture_calibration_sample(x, y, tip, bool(status & 0x26))
            self.write_state(True, touch_present, x, y, x, y, raw_p, tilt_x, tilt_y, in_range)
            return

        # 3. Curva de Pressão + Faixa de Pressão + Filtro Passiva/Ativa (FUN_00422890 & FUN_00422ca0)
        pressure = self.pressure_proc.evaluate(raw_p, tip)
        # Contact comes from the tip bit AND force, never from the filter.
        # Keep real tip+barrel contact for shortcuts such as Space+drag.
        tip = tip and pressure > 0
        self.last_pen_input = {'status': status, 'raw_pressure': raw_p, 'pressure': pressure,
                               'tip_down': tip, 'in_range': in_range}

        # 4. Homografia de Calibração de 4 Pontos (FUN_00422ca0)
        cal_x, cal_y = self.calibration.transform(x, y)

        # 5. FlipScreen (Inversão de Tela 180°, FUN_004230b0)
        if dig.get("FlipScreen", False):
            cal_x = MAX_X - cal_x
            cal_y = MAX_Y - cal_y

        # Windows FUN_00422890: bit20 -> slot8, bit02 -> slot9,
        # bit04 -> slot10. Tip is independently delivered as tablet contact.
        pen_btns = [bool(status & 0x20), barrel1, barrel2] if windows_layout else [barrel1, tip, barrel2]
        quicktool_req = False
        for idx, b_state in enumerate(pen_btns, start=8):
            b_int = 1 if b_state else 0
            blocked = idx in getattr(self, 'blocked_buttons', set())
            if not b_state:
                getattr(self, 'blocked_buttons', set()).discard(idx)
            if self.button_states[idx] != b_int:
                self.button_states[idx] = b_int
                # Button9 por padrão é Left Click (tratado nativamente por BTN_TOUCH), a menos que customizado
                btn_cfg = dig.get("Buttons", {}).get(f"Button{idx}", {})
                act = btn_cfg.get("Action", "") if isinstance(btn_cfg, dict) else ""
                native = windows_layout and act in ("Right Click", "Clique direito", "Middle Click", "Clique do meio")
                if not blocked and ((not b_state and idx in self.active_keycodes) or
                    (not native and (windows_layout or idx != 9 or act not in ("Left Click", "Clique esquerdo", "")))):
                    quicktool_req |= bool(self.execute_button_action(idx, b_state))
        native_buttons = self._native_pen_buttons()

        # 7. Emissão via UInput para o Linux (X11 / Wayland / libinput)
        if HAS_EVDEV and self.ui_pen is not None:
            # BTN_TOUCH leads the frame, as required by the kernel input ABI.
            self.ui_pen.write(e.EV_KEY, e.BTN_TOUCH, 1 if tip else 0)
            self.ui_pen.write(e.EV_ABS, e.ABS_X, cal_x)
            self.ui_pen.write(e.EV_ABS, e.ABS_Y, cal_y)
            self.ui_pen.write(e.EV_ABS, e.ABS_PRESSURE, pressure)
            self.ui_pen.write(e.EV_ABS, e.ABS_TILT_X, tilt_x)
            self.ui_pen.write(e.EV_ABS, e.ABS_TILT_Y, tilt_y)

            self.ui_pen.write(e.EV_KEY, e.BTN_TOOL_PEN, 1 if (in_range and not invert) else 0)
            self.ui_pen.write(e.EV_KEY, e.BTN_TOOL_RUBBER, 1 if (in_range and invert) else 0)
            # A remapped/disabled button must not also trigger a native click.
            self.ui_pen.write(e.EV_KEY, e.BTN_STYLUS, int(native_buttons[0]))
            self.ui_pen.write(e.EV_KEY, e.BTN_STYLUS2, int(native_buttons[1]))
            self.ui_pen.syn()

        self.write_state(True, touch_present, raw_x, raw_y, cal_x, cal_y, pressure, tilt_x, tilt_y, in_range, quicktool_req)


def find_pen_device():
    for vid, pid in SUPPORTED_PEN_DEVICES:
        dev = usb.core.find(idVendor=vid, idProduct=pid)
        if dev is not None:
            return dev
    return None


def send_feature_enable(dev):
    """
    Envia o Feature Report 02 04 03 41 (FUN_140002c80 @ 0x14000502c em bostohidrouter.sys)
    e o Output Report 0xEE (FUN_00420c90 @ 0x420c90 em TabletDriver.exe) para ativar
    o modo de coordenadas absolutas nativas (17920x10240) e 8192 níveis de pressão.
    """
    for intf_idx in (0,):
        try:
            dev.ctrl_transfer(
                bmRequestType=0x21,
                bRequest=0x09,       # HID SET_REPORT
                wValue=0x0302,       # Feature Report (0x03), Report ID 0x02
                wIndex=intf_idx,
                data_or_wLength=FEATURE_ENABLE_PEN,
                timeout=1000,
            )
            print(f"[OK] Feature Report 02 04 03 41 enviado na interface {intf_idx}.")
            break
        except usb.core.USBError:
            continue

    for intf_idx in (0,):
        try:
            dev.ctrl_transfer(
                bmRequestType=0x21,
                bRequest=0x09,       # HID SET_REPORT
                wValue=0x02EE,       # Output Report (0x02), Report ID 0xEE (FUN_00420c90)
                wIndex=intf_idx,
                data_or_wLength=VENDOR_INIT_REPORT_EE,
                timeout=500,
            )
            break
        except usb.core.USBError:
            continue


def find_touch_device():
    for vid, pid in SUPPORTED_TOUCH_DEVICES:
        tdev = usb.core.find(idVendor=vid, idProduct=pid)
        if tdev is not None:
            return tdev
    return None


def send_touch_flags(dev, touch_flags: int):
    """
    Envia o Feature Report ID 0x0B ([0x0B, flags]) para configurar o Touch Screen,
    idêntico a FUN_00421d90 / FUN_00423c20 em TabletDriver.exe.
    """
    payload = bytes([0x0B, touch_flags & 0x07])
    tdev = find_touch_device()
    if tdev is None:
        return False
    for intf_idx in (0,):
        try:
            tdev.ctrl_transfer(
                bmRequestType=0x21,
                bRequest=0x09,
                wValue=0x030B,       # Feature Report (0x03), Report ID 0x0B
                wIndex=intf_idx,
                data_or_wLength=payload,
                timeout=500,
            )
            return True
        except usb.core.USBError:
            continue
    return False


def acquire_single_instance_lock(lock_name="tomate-pen.lock"):
    lock_path = os.path.join("/run/lock", lock_name)
    if not os.path.exists("/run/lock") or not os.access("/run/lock", os.W_OK):
        lock_path = os.path.join("/tmp", lock_name)

    try:
        f = os.fdopen(os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600), "r+")
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        f.seek(0)
        f.truncate()
        f.write(f"{os.getpid()}\n")
        f.flush()
        return f
    except (BlockingIOError, IOError):
        pid = "desconhecido"
        try:
            with open(lock_path, "r") as lf:
                pid = lf.read().strip()
        except Exception:
            pass
        print("=" * 68)
        print(f" [AVISO] Outra instância do driver da caneta já está ativa (PID: {pid})")
        print(" O serviço 'tomate-pen.service' já está controlando a mesa.")
        print(" Para testar ou executar manualmente no terminal, pare o serviço:")
        print("    sudo systemctl stop tomate-pen")
        print("    python3 /opt/tomate-mtm1118/userspace-pen/tomate_pen_driver.py")
        print("=" * 68)
        sys.exit(0)


@contextmanager
def pen_usb_interface(dev):
    detached = claimed = False
    try:
        try:
            cfg = dev.get_active_configuration()
        except usb.core.USBError:
            dev.set_configuration()
            cfg = dev.get_active_configuration()
        interface = cfg[(0, 0)]
        if interface.bInterfaceClass != 3:
            raise usb.core.USBError("interface 0 não é HID; o driver não irá reivindicá-la")
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
            detached = True
        usb.util.claim_interface(dev, 0)
        claimed = True
        endpoints = [ep for ep in interface
                     if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN
                     and usb.util.endpoint_type(ep.bmAttributes) == usb.util.ENDPOINT_TYPE_INTR]
        if not endpoints:
            raise usb.core.USBError("interface da caneta sem endpoint Interrupt IN")
        yield endpoints
    finally:
        try:
            if claimed:
                usb.util.release_interface(dev, 0)
            if detached:
                dev.attach_kernel_driver(0)
        except usb.core.USBError:
            pass  # A physical unplug can invalidate the device before release.
        finally:
            usb.util.dispose_resources(dev)


def stop_signal(signum, frame):
    raise KeyboardInterrupt


def main():
    lock_file = acquire_single_instance_lock("tomate-pen.lock")
    print("=================================================================")
    print(" Driver Userspace da Caneta e ExpressKeys — Tomate MTM-1118")
    print("=================================================================")
    if not HAS_USB:
        print("[ERRO] Módulo pyusb ausente. Instale com: sudo apt install python3-usb")
        sys.exit(1)
    if not HAS_EVDEV:
        print("[ERRO] Módulo evdev ausente. Instale com: sudo apt install python3-evdev")
        sys.exit(1)
    signal.signal(signal.SIGTERM, stop_signal)
    signal.signal(signal.SIGINT, stop_signal)
    daemon = TomatePenDaemon()
    last_cfg_check = 0.0
    missing_reported = False
    try:
        while True:
            daemon.check_input_command(pen_ok=False)
            dev = find_pen_device()
            if dev is None:
                daemon.check_calibration_command(pen_ok=False)
                daemon.abort_calibration('A caneta foi desconectada. Abra a calibração novamente após reconectar.')
                if not missing_reported:
                    print("[USB] Caneta ausente. No WSL, anexe também 0ed1:7821/7856; anexar só o vídeo não conecta a caneta.")
                    missing_reported = True
                daemon.write_state(False, False)
                time.sleep(1.5)
                continue
            missing_reported = False
            print(f"[*] Digitalizador detectado: VID=0x{dev.idVendor:04X} PID=0x{dev.idProduct:04X}")
            try:
                with pen_usb_interface(dev) as endpoints:
                    send_feature_enable(dev)
                    touch_ok = send_touch_flags(dev, daemon.settings["Digitizer"].get("TouchFlags", 6))
                    daemon.write_state(True, touch_ok)
                    unknown_ids_logged = set()
                    while True:
                        now = time.monotonic()
                        if now - last_cfg_check > 0.5:
                            last_cfg_check = now
                            if daemon.load_settings():
                                send_touch_flags(dev, daemon.settings["Digitizer"].get("TouchFlags", 6))
                            daemon.check_input_command()
                        daemon.check_calibration_command(touch_ok=touch_ok)
                        daemon.check_calibration_timeout(touch_ok=touch_ok)
                        for ep in endpoints:
                            try:
                                data = dev.read(ep.bEndpointAddress, max(16, ep.wMaxPacketSize), timeout=8)
                                if not data:
                                    continue
                                raw = bytes(data)
                                rep_id = raw[0]
                                daemon.raw_reports[f"0x{rep_id:02x}"] = raw.hex(" ")
                                if rep_id == 0x01:
                                    daemon.handle_pen_packet(raw, touch_ok, has_tilt=len(raw) >= 10)
                                elif rep_id == 0x04:
                                    if daemon.handle_expresskeys_packet(raw):
                                        daemon.write_state(True, touch_ok, quicktool_req=True)
                                elif rep_id == 0x03:
                                    daemon.handle_keyboard_packet(raw)
                                elif rep_id not in unknown_ids_logged:
                                    unknown_ids_logged.add(rep_id)
                                    print(f"[DIAG] Report ID desconhecido 0x{rep_id:02X} ({len(raw)} bytes): {raw[:16].hex(' ')}")
                            except usb.core.USBTimeoutError:
                                continue
            except usb.core.USBError as exc:
                print(f"[!] Dispositivo desconectado ou erro USB: {exc}")
                time.sleep(1.0)
            finally:
                daemon.abort_calibration('A conexão da caneta foi interrompida. Abra a calibração novamente.')
                daemon.release_input()
                daemon.raw_reports.clear()
                daemon.write_state(False, False)
    except KeyboardInterrupt:
        print("[*] Encerrando caneta e liberando botões.")
    finally:
        daemon.abort_calibration('O serviço da caneta foi encerrado. Abra a calibração novamente.')
        daemon.release_input()
        daemon.write_state(False, False)
        for device in (daemon.ui_pen, daemon.ui_keys):
            if device is not None:
                device.close()
        lock_file.close()


if __name__ == "__main__":
    main()
