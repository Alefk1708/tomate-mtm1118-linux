#!/usr/bin/env python3
# Copyright 2026 Alefk1708
# SPDX-License-Identifier: Apache-2.0
# See LICENSE, NOTICE and THIRD_PARTY_NOTICES.md for scope and attribution.
"""
Interface Gráfica de Configuração e Calibração — Tomate MTM-1118 (BostoCfgTool Port)
====================================================================================
Reconstruída via Engenharia Reversa (REA + Ghidra) de:
  - TabletDriver.exe (SHA-256: 28c5db1dc8143f1c699d7d04cb30a3e669d5b0652e3fa30e46963edcf5996a3c)
  - Recursos originais:
    * resources/pt-BR/bostocfgtool.xml      (Janela principal 560x380, 6 abas)
    * resources/pt-BR/bostocalwin.xml       (Janela fullscreen de calibração de 4 pontos)
    * resources/pt-BR/bostokeydlg.xml       (Diálogo de tecla de atalho)
    * resources/pt-BR/bostoquicktoolwin.xml (Janela flutuante QuickTool de 8 atalhos)
    * resources/pt-BR/aboutdialog.xml       (Diálogo Sobre v1.04)
    * tabletlang.xml & cfgtoolsettings.xml  (Traduções pt-BR 1046 e configurações padrão)
"""

import os
import sys
import json
import time
import math
import shutil
import threading
import subprocess
import argparse
import tempfile
import uuid
import fcntl
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "userspace-pen"))
from button_schema import migrate_button_settings, PEN_BUTTON_TITLES, keystroke_tokens
from calibration_protocol import (PROTOCOL as CALIBRATION_PROTOCOL, DISPLAY_STATE_PATH,
                                  calibration_targets, fresh_json, panel_geometry, target_is_displayed)
import tkinter as tk
from tkinter import ttk, messagebox

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import pystray
    HAS_PYSTRAY = True
except ImportError:
    HAS_PYSTRAY = False

MAX_X = 17920
MAX_Y = 10240
MAX_PRESSURE = 8192

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(SCRIPT_DIR, "assets")
SETTINGS_PATH = "/etc/tomate-mtm1118/settings.json"
STATE_PATH = "/run/tomate-mtm1118/state.json"
CAL_CMD_PATH = "/run/tomate-mtm1118/calibration-command.json"
CALIBRATION_STATE_TIMEOUT = 5
CALIBRATION_POINT_TIMEOUT = 90

BUTTON_ACTIONS_PT = [
    ("Clique esquerdo", "Left Click"),
    ("Clique do meio", "Middle Click"),
    ("Clique direito", "Right Click"),
    ("Clique duplo", "Double Click"),
    ("Rolar para cima", "Wheel Up"),
    ("Rolar para baixo", "Wheel Down"),
    ("Tecla de atalho...", "Keystroke"),
    ("Desfazer", "Undo"),
    ("Refazer", "Redo"),
    ("Ferramenta rápida", "QuickTool"),
    ("Desativado", "Disabled"),
]

ACTION_TO_PT = {eng: pt for pt, eng in BUTTON_ACTIONS_PT}
PT_TO_ACTION = {pt: eng for pt, eng in BUTTON_ACTIONS_PT}

DEFAULT_SETTINGS = {
    "General": {
        "Language": 1046,
        "AutoStart": True,
        "StartMinimize": False,
        "DiscardSetting": False,
        "TargetDisplay": " Monitor 1"
    },
    "Digitizer": {
        "TouchFlags": 6,
        "WindowsInkEnabled": True,
        "FlipAxis": False,
        "FlipScreen": False,
        "PressureDamping": 5,
        "PressureDampingMaxValue": 10,
        "PressureMaxValue": 4096,
        "CalibrationValid": False,
        "PenType": 0,
        "ButtonLayout": "windows",
        "ButtonReportVersion": 2,
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


def format_button_label(btn_cfg: dict) -> str:
    act = btn_cfg.get("Action", "Disabled")
    val = btn_cfg.get("Value", "")
    if act == "Keystroke" and val:
        return f"Atalho ({val.lstrip('+')})"
    if act == "Run Application" and val:
        return "Aplicativo (desativado)"
    return ACTION_TO_PT.get(act, act)


def atomic_json(path, value):
    """Publish complete files to the daemon; do not expose partial JSON."""
    folder = os.path.dirname(path)
    with tempfile.NamedTemporaryFile(mode="w", dir=folder, prefix=".tomate-", delete=False,
                                     encoding="utf-8") as stream:
        temporary = stream.name
        try:
            json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
            os.chmod(temporary, 0o660)
        except BaseException:
            os.unlink(temporary)
            raise
    try:
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_calibration_points(points):
    if len(points) != 4 or any(len(point) != 2 for point in points):
        raise ValueError("São necessários quatro pontos da caneta.")
    if any(not math.isfinite(float(v)) for point in points for v in point):
        raise ValueError("Coordenadas de calibração inválidas.")
    if any(not (0 <= x <= MAX_X and 0 <= y <= MAX_Y) for x, y in points):
        raise ValueError("Pontos fora da área do digitalizador.")
    turns = []
    for i in range(4):
        a, b, c = points[i], points[(i + 1) % 4], points[(i + 2) % 4]
        turns.append((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]))
    if not (all(v > 0 for v in turns) or all(v < 0 for v in turns)):
        raise ValueError("Os pontos se cruzam ou estão alinhados. Toque cada mira na ordem indicada.")
    area = abs(sum(points[i][0] * points[(i + 1) % 4][1] -
                   points[(i + 1) % 4][0] * points[i][1] for i in range(4))) / 2
    if area < MAX_X * MAX_Y * .01:
        raise ValueError("Área de calibração muito pequena. Toque as quatro miras da tela.")


def calibration_progress(state, session_id, now):
    """Reject stale telemetry from a previous calibration or daemon instance."""
    if state.get("cal_session_id") != session_id or not 0 <= now - float(state.get("timestamp", 0)) < 3:
        return None
    step = int(state.get("cal_step", 0))
    if not 0 <= step <= 4 or len(state.get("cal_points", [])) != 4:
        return None
    return step, state["cal_points"], bool(state.get("cal_mode", False))


# ============================================================================
# 1. DIÁLOGO DE TECLA DE ATALHO (resources/pt-BR/bostokeydlg.xml)
# ============================================================================
class KeystrokeDialog(tk.Toplevel):
    def __init__(self, parent, initial_value=""):
        super().__init__(parent)
        self.title("Definir tecla de atalho")
        self.geometry("320x250")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result = None

        self.var_ctrl = tk.BooleanVar(value="+Ctrl" in initial_value or "Ctrl+" in initial_value)
        self.var_alt = tk.BooleanVar(value="+Alt" in initial_value or "Alt+" in initial_value)
        self.var_shift = tk.BooleanVar(value="+Shift" in initial_value or "Shift+" in initial_value)
        self.var_win = tk.BooleanVar(value="+Win" in initial_value or "Win+" in initial_value)

        base_key = initial_value
        for mod in ("+Ctrl", "Ctrl+", "+Alt", "Alt+", "+Shift", "Shift+", "+Win", "Win+"):
            base_key = base_key.replace(mod, "")
        base_key = base_key.lstrip("+")

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill=tk.BOTH, expand=True)

        mod_frm = ttk.Frame(frm)
        mod_frm.pack(fill=tk.X, pady=(0, 8))
        ttk.Checkbutton(mod_frm, text="Ctrl", variable=self.var_ctrl).pack(side=tk.LEFT, padx=4)
        ttk.Checkbutton(mod_frm, text="Alt", variable=self.var_alt).pack(side=tk.LEFT, padx=4)
        ttk.Checkbutton(mod_frm, text="Shift", variable=self.var_shift).pack(side=tk.LEFT, padx=4)
        ttk.Checkbutton(mod_frm, text="Win", variable=self.var_win).pack(side=tk.LEFT, padx=4)

        key_frm = ttk.Frame(frm)
        key_frm.pack(fill=tk.X, pady=4)
        ttk.Label(key_frm, text="Chave:").pack(side=tk.LEFT)
        self.ent_key = ttk.Entry(key_frm, width=18)
        self.ent_key.pack(side=tk.LEFT, padx=6)
        self.ent_key.insert(0, base_key)
        self.ent_key.bind("<KeyPress>", self._on_key_press)
        ttk.Button(key_frm, text="Limpar", width=8, command=lambda: self.ent_key.delete(0, tk.END)).pack(side=tk.LEFT)

        spec_frm = ttk.Frame(frm)
        spec_frm.pack(fill=tk.X, pady=8)
        ttk.Button(spec_frm, text="Barra de espaço", command=lambda: self._set_special("Space")).pack(side=tk.LEFT, padx=2)
        ttk.Button(spec_frm, text="Backspace", command=lambda: self._set_special("Backspace")).pack(side=tk.LEFT, padx=2)
        ttk.Button(spec_frm, text="Tab", command=lambda: self._set_special("Tab")).pack(side=tk.LEFT, padx=2)

        btn_frm = ttk.Frame(frm)
        btn_frm.pack(side=tk.BOTTOM, fill=tk.X, pady=(12, 0))
        ttk.Button(btn_frm, text="Cancelar", command=self.destroy).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btn_frm, text="OK", command=self._on_ok).pack(side=tk.RIGHT, padx=4)

    def _set_special(self, name):
        self.ent_key.delete(0, tk.END)
        self.ent_key.insert(0, name)

    def _on_key_press(self, event):
        ks = event.keysym
        if ks in ("Control_L", "Control_R", "Alt_L", "Alt_R", "Shift_L", "Shift_R", "Super_L", "Super_R"):
            return "break"
        for variable, mask in ((self.var_ctrl, 4), (self.var_shift, 1), (self.var_alt, 8), (self.var_win, 64)):
            if event.state & mask:
                variable.set(True)
        self.ent_key.delete(0, tk.END)
        if len(ks) == 1:
            self.ent_key.insert(0, ks.upper())
        else:
            self.ent_key.insert(0, ks)
        return "break"

    def _on_ok(self):
        parts = []
        if self.var_ctrl.get():
            parts.append("Ctrl")
        if self.var_alt.get():
            parts.append("Alt")
        if self.var_shift.get():
            parts.append("Shift")
        if self.var_win.get():
            parts.append("Win")
        k = self.ent_key.get().strip()
        if k:
            parts.append(k)
        value = ("+" + "+".join(parts)) if parts else ""
        if value and not keystroke_tokens(value):
            messagebox.showerror('Atalho não reconhecido', 'Use uma letra, número, F1–F24, Space, Tab, setas ou uma tecla de pontuação suportada.', parent=self)
            return
        self.result = value
        self.destroy()


# ============================================================================
# 2. JANELA FLUTUANTE QUICKTOOL (resources/pt-BR/bostoquicktoolwin.xml)
# ============================================================================
class QuickToolWindow(tk.Toplevel):
    """Reproduz a janela rápida de 8 botões (bostoquicktoolwin.xml + quicktool.xml)."""
    def __init__(self, parent):
        super().__init__(parent)
        self.title("Ferramenta Rápida")
        self.geometry("240x185")
        self.resizable(False, False)
        self.attributes("-topmost", True)

        frm = ttk.Frame(self, padding=8)
        frm.pack(fill=tk.BOTH, expand=True)

        shortcuts = [
            ("Novo (Ctrl+N)", ["ctrl", "n"]),
            ("Salvar (Ctrl+S)", ["ctrl", "s"]),
            ("Desfazer (Ctrl+Z)", ["ctrl", "z"]),
            ("Refazer (Ctrl+Y)", ["ctrl", "y"]),
            ("Zoom + (Ctrl+=)", ["ctrl", "equal"]),
            ("Zoom - (Ctrl+-)", ["ctrl", "minus"]),
            ("Pincel [", ["bracketleft"]),
            ("Pincel ]", ["bracketright"]),
        ]
        for idx, (label, keys) in enumerate(shortcuts):
            r, c = divmod(idx, 2)
            btn = ttk.Button(frm, text=label, width=14, command=lambda k=keys: self._send_keys(k))
            btn.grid(row=r, column=c, padx=4, pady=3, sticky="ew")

        ttk.Button(
            frm,
            text="Configurações do Tablet...",
            command=lambda: (parent.deiconify(), parent.lift()),
        ).grid(row=4, column=0, columnspan=2, pady=(8, 0), sticky="ew")

    @staticmethod
    def _send_keys(keys):
        try:
            subprocess.Popen(["xdotool", "key", "+".join(keys)])
        except Exception:
            pass


# ============================================================================
# 3. JANELA FULLSCREEN DE CALIBRAÇÃO DE 4 PONTOS (resources/pt-BR/bostocalwin.xml)
# ============================================================================
class CalibrationWindow(tk.Toplevel):
    """Controls raw-point capture; crosshairs are rendered by the USB display service."""
    def __init__(self, parent, monitor_geom, on_complete_callback):
        super().__init__(parent)
        self.parent = parent
        self.mon = monitor_geom
        self.on_complete = on_complete_callback
        self.step = 0
        self.captured_sources = [[0, 0], [0, 0], [0, 0], [0, 0]]
        self.cancelled = False
        self.session_id = uuid.uuid4().hex
        self.started = time.monotonic()
        self.acknowledged = False
        self.last_seen = self.last_progress = self.started
        self.last_command_sent = self.started
        self.pending_command = None
        self.arm_scheduled = False
        self.poll_timer = self.arm_timer = None
        self.target_visible = False
        self.status_message = 'Aguardando confirmação da caneta...'
        self.display_frame_id = ''
        self.flip_screen = bool(parent.settings['Digitizer'].get('FlipScreen', False))
        self.display_wait_started = self.started

        self.title('Calibração — mire na tela da mesa Tomate')
        self.geometry('640x250')
        self.resizable(False, False)
        w = self.mon["width"]
        h = self.mon["height"]
        self.configure(bg="#F2F2F2")

        self.canvas = tk.Canvas(self, width=640, height=210, bg="#F2F2F2", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        ttk.Button(self, text='Cancelar calibração', command=self._on_escape).pack(pady=6)

        # The Linux crosshair inset is a UI choice, independent of raw coordinates.
        self.crosshair_screen_pts, self.target_tablet_pts = calibration_targets(
            w, h, self.flip_screen)

        self.bind("<Escape>", self._on_escape)
        self.protocol("WM_DELETE_WINDOW", self._on_escape)

        # Notifica o daemon userspace para iniciar a captura bruta de 4 pontos
        if not self._send_daemon_cmd("start"):
            self.destroy()
            return
        self._draw_current_step()
        self.focus_force()
        self.poll_timer = self.after(50, self._poll_daemon_calibration)

    def _send_daemon_cmd(self, action: str, show_error=True):
        try:
            command_id = uuid.uuid4().hex
            command = {"action": action, "request_id": self.session_id, "timestamp": time.time(),
                       "protocol": CALIBRATION_PROTOCOL, "command_id": command_id}
            if action == 'arm':
                command['step'] = self.step
                command['display_frame_id'] = self.display_frame_id
            atomic_json(CAL_CMD_PATH, command)
            self.last_command_sent = time.monotonic()
            self.pending_command = {'action': action, 'id': command_id, 'sent': self.last_command_sent}
            return True
        except (OSError, ValueError) as exc:
            if show_error:
                self.withdraw()
                messagebox.showerror("Calibração", f"Não foi possível comunicar com o driver:\n{exc}\n"
                                     "Verifique a instalação e o grupo tomate-tablet.", parent=self.parent)
            return False

    def _draw_current_step(self):
        self.canvas.delete("all")
        w, h = 640, 210

        # Texto central idêntico a resources/pt-BR/bostocalwin.xml
        self.canvas.create_text(
            w // 2,
            h // 2 - 15,
            text="Toque a mira na própria tela da mesa Tomate.",
            fill="#0A0A0A",
            font=("Sans", 14, "bold"),
        )
        self.canvas.create_text(
            w // 2,
            h // 2 + 18,
            text=f"Pressione a tecla ESC para sair da tela de calibração. (Ponto {min(self.step + 1, 4)}/4)",
            fill="#404040",
            font=("Sans", 11),
        )
        self.canvas.create_text(w // 2, h // 2 + 48, text=self.status_message, width=600,
                                fill="#404040", font=("Sans", 11))

    def _arm_current_target(self, expected_step):
        self.arm_scheduled = False
        self.arm_timer = None
        if (self.cancelled or not self.winfo_exists() or self.step != expected_step
                or not self.target_visible or self.pending_command is not None):
            return
        displayed = fresh_json(DISPLAY_STATE_PATH)
        if not target_is_displayed(displayed, self.session_id, self.step, self.flip_screen):
            return
        self.display_frame_id = displayed['cal_frame_id']
        if not self._send_daemon_cmd('arm'):
            self._on_escape()

    def _stop_calibration_polling(self):
        for name in ('poll_timer', 'arm_timer'):
            token = self.__dict__.get(name)
            if token is not None:
                try:
                    self.after_cancel(token)
                except tk.TclError:
                    pass
                setattr(self, name, None)
        self.arm_scheduled = False

    def _fail_calibration(self, message):
        self.cancelled = True
        self._stop_calibration_polling()
        self._send_daemon_cmd('cancel', show_error=False)
        self.destroy()
        messagebox.showerror('Calibração interrompida', message, parent=self.parent)

    def _poll_daemon_calibration(self):
        self.poll_timer = None
        if self.cancelled or not self.winfo_exists():
            return
        now = time.monotonic()
        try:
            if os.path.exists(STATE_PATH):
                with open(STATE_PATH, "r", encoding="utf-8") as f:
                    st = json.load(f)
                progress = calibration_progress(st, self.session_id, time.time())
                if progress is not None:
                    if st.get('cal_protocol') != CALIBRATION_PROTOCOL:
                        self._fail_calibration('Atualize o driver e feche/reabra o painel para usar a calibração sincronizada.')
                        return
                    if not self.acknowledged:
                        self.last_progress = now
                    self.acknowledged = True
                    self.last_seen = now
                    if self.pending_command and st.get('cal_command_id') == self.pending_command['id']:
                        self.pending_command = None
                    daemon_step, pts, active = progress
                    if daemon_step < self.step or daemon_step > self.step + 1:
                        self._fail_calibration('O progresso da calibração foi reiniciado ou ficou inconsistente. Abra a calibração novamente.')
                        return
                    if daemon_step > self.step:
                        self.captured_sources = [list(point) for point in pts]
                        self.step = daemon_step
                        self.last_progress = now
                        self.target_visible = False
                        self.display_wait_started = now
                    if self.step >= 4 and not active:
                        self._finish_calibration()
                        return
                    if st.get('cal_error') or not active or not st.get('pen_connected', False):
                        self._fail_calibration(st.get('cal_error') or 'A calibração foi interrompida ou a caneta foi desconectada. Reconecte-a e tente novamente.')
                        return
                    phase = st.get('cal_phase', '')
                    displayed = fresh_json(DISPLAY_STATE_PATH)
                    if not target_is_displayed(displayed, self.session_id, self.step, self.flip_screen):
                        self.target_visible = False
                        self.status_message = 'Aguardando a mira na tela da mesa...'
                        self._draw_current_step()
                        if now - self.display_wait_started > 8:
                            self._fail_calibration('O serviço de imagem não confirmou a mira na mesa. Instale o pacote completo, feche o painel com Sair e reabra-o.')
                            return
                        if self.pending_command is None and now - self.last_command_sent >= 1:
                            self._send_daemon_cmd('heartbeat')
                        self.poll_timer = self.after(50, self._poll_daemon_calibration)
                        return
                    if (displayed['panel_width'], displayed['panel_height']) != (self.mon['width'], self.mon['height']):
                        self._fail_calibration('As dimensões da mesa mudaram durante a calibração. Abra a calibração novamente.')
                        return
                    self.display_wait_started = now
                    message = {'waiting_target': 'Preparando a próxima mira...',
                               'waiting_lift': 'Levante a ponta antes de tocar esta mira.',
                               'waiting_touch': 'Encoste e levante a ponta no centro da mira.',
                               'sampling': 'Ponto em captura. Levante a ponta para confirmar.'}.get(phase, 'Aguardando a caneta...')
                    message = st.get('cal_feedback') or message
                    redraw = message != self.status_message or not self.target_visible
                    self.status_message = message
                    self.target_visible = True
                    if redraw:
                        self._draw_current_step()
                    if phase == 'waiting_target' and self.pending_command is None and not self.arm_scheduled:
                        self.arm_scheduled = True
                        # USB completion, rather than XWayland positioning, authorizes capture.
                        self.arm_timer = self.after_idle(lambda step=self.step: self._arm_current_target(step))
                    elif self.pending_command is None and not self.arm_scheduled and now - self.last_command_sent >= 1:
                        if not self._send_daemon_cmd('heartbeat'):
                            self._on_escape()
                            return
                elif (self.acknowledged and st.get('cal_session_id') != self.session_id
                      and 0 <= time.time() - float(st.get('timestamp', 0)) < 3):
                    self._fail_calibration('O serviço da caneta foi reiniciado ou a sessão foi substituída. Abra a calibração novamente.')
                    return
        except (OSError, ValueError, TypeError, KeyError):
            pass
        if not self.acknowledged and time.monotonic() - self.started > 10:
            self._fail_calibration('O driver não confirmou o início da calibração. Verifique se a caneta está conectada e o serviço está ativo.')
            return
        if self.acknowledged and now - self.last_seen > CALIBRATION_STATE_TIMEOUT:
            self._fail_calibration('A comunicação com a caneta parou. Verifique a conexão e abra a calibração novamente.')
            return
        if self.pending_command and now - self.pending_command['sent'] > CALIBRATION_STATE_TIMEOUT and self.acknowledged:
            self._fail_calibration('O driver não confirmou o preparo da mira. Abra a calibração novamente.')
            return
        if self.acknowledged and now - self.last_progress > CALIBRATION_POINT_TIMEOUT:
            self._fail_calibration('Nenhum ponto foi confirmado por 90 segundos. Encoste e levante a ponta, sem apertar os botões laterais. Abra a calibração para tentar novamente.')
            return
        self.poll_timer = self.after(50, self._poll_daemon_calibration)

    def _on_canvas_click(self, event):
        # Never use an already calibrated screen cursor as a raw source point.
        return "break"

    def _finish_calibration(self):
        self.cancelled = True
        self._stop_calibration_polling()
        try:
            validate_calibration_points(self.captured_sources)
        except (ValueError, TypeError) as exc:
            self._send_daemon_cmd("cancel")
            self.destroy()
            messagebox.showerror("Calibração", str(exc), parent=self.parent)
            return
        self.withdraw()
        # Pergunta de confirmação idêntica a tabletlang.xml ID 27
        ans = messagebox.askyesno(
            "Calibração",
            "Definir dados de calibragem da tela atual?",
            parent=self.parent,
        )
        if ans:
            self.on_complete(self.captured_sources, self.target_tablet_pts)
        self.destroy()

    def _on_escape(self, _event=None):
        self.cancelled = True
        self._stop_calibration_polling()
        self._send_daemon_cmd("cancel", show_error=False)
        self.destroy()


# ============================================================================
# 4. DIÁLOGO SOBRE (resources/pt-BR/aboutdialog.xml)
# ============================================================================
class AboutDialog(tk.Toplevel):
    def __init__(self, parent, logo_img=None):
        super().__init__(parent)
        self.title("Sobre")
        self.minsize(430, 350)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        frm = ttk.Frame(self, padding=15)
        frm.pack(fill=tk.BOTH, expand=True)

        if logo_img is not None:
            ttk.Label(frm, image=logo_img).pack(pady=(0, 8))

        ttk.Label(frm, text="Configuração de Digitalizador de Tablet", font=("Sans", 10, "bold")).pack(anchor="w")
        ttk.Label(frm, text="Linux v2.3.12 — Tomate MTM-1118").pack(anchor="w", pady=2)
        ttk.Label(frm, text="Projeto e port Linux: Alefk1708\nCopyright 2026 Alefk1708", foreground="#333333").pack(anchor="w", pady=6)
        ttk.Label(frm, text="Apache License 2.0 — avisos em LICENSE e NOTICE\nna pasta /opt/tomate-mtm1118.", wraplength=395).pack(anchor="w", pady=4)
        ttk.Label(frm, text="Recursos do painel original:\nCopyright (C) 2016 - 2019, Bosto Co., Ltd.\nTodos os direitos reservados.", foreground="#555555").pack(anchor="w", pady=6)

        ttk.Button(frm, text="OK", width=10, command=self.destroy).pack(side=tk.BOTTOM, pady=(10, 0))


# ============================================================================
# 5. JANELA PRINCIPAL DE CONFIGURAÇÃO (resources/pt-BR/bostocfgtool.xml)
# ============================================================================
class PanelInstance:
    """One editor per user: another launch raises the existing panel."""
    def __init__(self, directory=None):
        self.directory = directory or os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        self.lock_path = os.path.join(self.directory, "tomate-panel.lock")
        self.activate_path = os.path.join(self.directory, "tomate-panel.activate")
        self.fd = None
        self.last_activation = 0

    def acquire(self):
        self.fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            fd = os.open(self.activate_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(str(time.time_ns()))
            return False
        try:
            self.last_activation = os.stat(self.activate_path).st_mtime_ns
        except FileNotFoundError:
            pass
        return True

    def activation_requested(self):
        try:
            timestamp = os.stat(self.activate_path).st_mtime_ns
        except FileNotFoundError:
            return False
        changed = timestamp != self.last_activation
        self.last_activation = timestamp
        return changed


class TomateControlCenter(tk.Tk):
    def __init__(self, start_minimized=False):
        super().__init__()
        self.panel_instance = PanelInstance()
        if not self.panel_instance.acquire():
            self.destroy()
            raise SystemExit(0)
        self.after(300, self._poll_activation)
        self.title("Tablet Digitizer — Tomate MTM-1118")
        self.geometry("760x520")
        self.resizable(False, False)

        self.settings = json.loads(json.dumps(DEFAULT_SETTINGS))
        self.active_settings_file = self._resolve_settings_path()
        self.load_settings()

        self.images = {}
        self._load_assets()
        if "mainmenuicon" in self.images:
            self.iconphoto(True, self.images["mainmenuicon"])

        self.quicktool_win = None
        self.monitors_list = []

        self._build_ui()
        self._refresh_monitors()
        self._sync_ui_from_settings()

        self.protocol("WM_DELETE_WINDOW", self._on_minimize_to_tray)
        self.tray_running = False
        self.last_quicktool_ts = 0.0

        if HAS_PYSTRAY:
            threading.Thread(target=self._run_system_tray, daemon=True).start()

        if start_minimized or self.settings["General"].get("StartMinimize", False):
            # Só esconde a janela se o ícone da bandeja realmente subir; caso contrário
            # (ex.: GNOME/COSMIC sem suporte a AppIndicator) apenas minimiza, para que
            # o painel nunca fique inacessível.
            self.after(1500, self._apply_start_minimized)

        self.after(50, self._poll_live_telemetry)

    def _poll_activation(self):
        if self.panel_instance.activation_requested():
            self.deiconify()
            self.lift()
            self.focus_force()
        self.after(300, self._poll_activation)

    def _resolve_settings_path(self) -> str:
        return SETTINGS_PATH

    def _load_assets(self):
        if not HAS_PIL:
            return
        for name, fname in [
            ("logo", "bosto-logo.png"),
            ("product", "product.png"),
            ("pen_passive", "pen.png"),
            ("pen_active", "pen_1.png"),
            ("tablet_buttons", "tablet-buttons.png"),
            ("plus", "plus.png"),
            ("mainmenuicon", "mainmenuicon.png"),
        ]:
            p = os.path.join(ASSETS_DIR, fname)
            if os.path.exists(p):
                try:
                    img = Image.open(p)
                    if name == "product":
                        img.thumbnail((260, 175))
                    elif name in ("pen_passive", "pen_active"):
                        img.thumbnail((170, 90))
                    elif name == "tablet_buttons":
                        img.thumbnail((220, 190))
                    elif name == "logo":
                        img.thumbnail((120, 32))
                    self.images[name] = ImageTk.PhotoImage(img)
                except Exception:
                    pass

    def load_settings(self):
        for candidate in (SETTINGS_PATH,):
            if os.path.exists(candidate):
                try:
                    with open(candidate, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    data = migrate_button_settings(data)
                    for sec in ("General", "Digitizer"):
                        if sec in data and isinstance(data[sec], dict):
                            self.settings[sec].update(data[sec])
                    if "ButtonLayout" not in data.get("Digitizer", {}):
                        self.settings["Digitizer"]["ButtonLayout"] = "legacy"
                    break
                except Exception:
                    pass

    def save_settings(self):
        if self.settings["General"].get("DiscardSetting", False):
            messagebox.showinfo('Configuração não aplicada', "Desative 'Descartar configurações ao sair' para aplicar e salvar a alteração no driver.", parent=self)
            return False
        try:
            atomic_json(SETTINGS_PATH, self.settings)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Salvar configurações", f"O driver não recebeu as alterações:\n{exc}\n"
                                 "Verifique a instalação e o grupo tomate-tablet.", parent=self)
            return False
        self._update_autostart_desktop()
        return True

    def _update_autostart_desktop(self):
        """Replica FUN_0041ef80 (Run\\BostoCfgTool) criando/removendo .desktop em ~/.config/autostart."""
        autostart_dir = os.path.expanduser("~/.config/autostart")
        desktop_file = os.path.join(autostart_dir, "tomate-mtm1118-tray.desktop")
        enabled = bool(self.settings["General"].get("AutoStart", True))
        if enabled:
            try:
                os.makedirs(autostart_dir, exist_ok=True)
                content = (
                    "[Desktop Entry]\n"
                    "Type=Application\n"
                    "Name=Tomate MTM-1118 Tablet Driver\n"
                    "Comment=Painel e Driver da Mesa Digitalizadora Tomate MTM-1118\n"
                    "Exec=/usr/local/bin/tomate-tablet-settings --minimized\n"
                    "Terminal=false\n"
                    "Categories=Settings;HardwareSettings;\n"
                    "X-GNOME-Autostart-enabled=true\n"
                )
                with open(desktop_file, "w", encoding="utf-8") as f:
                    f.write(content)
            except Exception:
                pass
        else:
            if os.path.exists(desktop_file):
                try:
                    os.remove(desktop_file)
                except Exception:
                    pass

    # ========================================================================
    # CONSTRUÇÃO DO LAYOUT (IDÊNTICO A resources/pt-BR/bostocfgtool.xml)
    # ========================================================================
    def _build_ui(self):
        main_vbox = ttk.Frame(self)
        main_vbox.pack(fill=tk.BOTH, expand=True)

        content_hbox = ttk.Frame(main_vbox, padding=(8, 8, 8, 4))
        content_hbox.pack(fill=tk.BOTH, expand=True)

        # Menu lateral esquerdo (list_pages em bostocfgtool.xml)
        sidebar = ttk.Frame(content_hbox, width=165)
        sidebar.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8))
        sidebar.pack_propagate(False)

        self.page_titles = [
            "Status do dispositivo",
            "Geral",
            "Configurações de desenho",
            "Botões do tablet",
            "Toque",
            "Calibração",
        ]
        self.nav_listbox = tk.Listbox(
            sidebar,
            selectmode=tk.SINGLE,
            font=("Sans", 10),
            activestyle="none",
            bg="#FFFFFF",
            fg="#111111",
            selectbackground="#1E88E5",
            selectforeground="#FFFFFF",
            relief=tk.SOLID,
            bd=1,
            highlightthickness=0,
        )
        for title in self.page_titles:
            self.nav_listbox.insert(tk.END, f"  {title}")
        self.nav_listbox.pack(fill=tk.BOTH, expand=True)
        self.nav_listbox.bind("<<ListboxSelect>>", self._on_nav_select)

        # Container de páginas à direita (tab_pages em bostocfgtool.xml)
        self.pages_container = ttk.Frame(content_hbox, relief=tk.SOLID, borderwidth=1)
        self.pages_container.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.pages = []
        self._build_page_status()
        self._build_page_general()
        self._build_page_drawing()
        self._build_page_buttons()
        self._build_page_touch()
        self._build_page_calibration()

        # Barra inferior (bosto-logo.png, Sobre..., Restaurar padrões, Minimizar, Sair)
        bottom_bar = ttk.Frame(main_vbox, padding=(10, 4, 10, 8))
        bottom_bar.pack(side=tk.BOTTOM, fill=tk.X)

        if "logo" in self.images:
            ttk.Label(bottom_bar, image=self.images["logo"]).pack(side=tk.LEFT, padx=(0, 8))
        else:
            ttk.Label(bottom_bar, text="BOSTO / TOMATE", font=("Sans", 11, "bold")).pack(side=tk.LEFT, padx=(0, 8))

        ttk.Button(bottom_bar, text="Sair", width=8, command=self._on_quit).pack(side=tk.RIGHT, padx=3)
        ttk.Button(bottom_bar, text="Minimizar", width=10, command=self._on_minimize_to_tray).pack(side=tk.RIGHT, padx=3)
        ttk.Button(bottom_bar, text="Restaurar padrões", width=16, command=self._on_restore_defaults).pack(side=tk.RIGHT, padx=3)
        ttk.Button(bottom_bar, text="Sobre...", width=9, command=self._on_about).pack(side=tk.RIGHT, padx=3)

        self.nav_listbox.selection_set(0)
        self._show_page(0)

    def _build_page_status(self):
        """Aba 0: Status do dispositivo (page_status em bostocfgtool.xml)."""
        p = ttk.Frame(self.pages_container, padding=16)
        self.pages.append(p)

        img_lbl = ttk.Label(p)
        if "product" in self.images:
            img_lbl.configure(image=self.images["product"])
        else:
            img_lbl.configure(text="[ Display Interativo Tomate MTM-1118 ]", font=("Sans", 12, "bold"))
        img_lbl.pack(pady=(10, 18))

        st_pen_frm = ttk.Frame(p)
        st_pen_frm.pack(fill=tk.X, pady=4, padx=24)
        ttk.Label(st_pen_frm, text="Status da caneta:", width=22, font=("Sans", 10)).pack(side=tk.LEFT)
        self.lbl_pen_status = ttk.Label(st_pen_frm, text="Apresentado", font=("Sans", 10, "bold"), foreground="#1B5E20")
        self.lbl_pen_status.pack(side=tk.LEFT)

        st_touch_frm = ttk.Frame(p)
        st_touch_frm.pack(fill=tk.X, pady=4, padx=24)
        ttk.Label(st_touch_frm, text="Status do touch screen:", width=22, font=("Sans", 10)).pack(side=tk.LEFT)
        self.lbl_touch_status = ttk.Label(st_touch_frm, text="Não apresentado", font=("Sans", 10))
        self.lbl_touch_status.pack(side=tk.LEFT)

        st_coord_frm = ttk.Frame(p)
        st_coord_frm.pack(fill=tk.X, pady=8, padx=24)
        ttk.Label(st_coord_frm, text="Coordenadas / Tilt:", width=22, font=("Sans", 9)).pack(side=tk.LEFT)
        self.lbl_live_coords = ttk.Label(st_coord_frm, text="X: 0  Y: 0  Pressão: 0", font=("Sans", 9), foreground="#555555")
        self.lbl_live_coords.pack(side=tk.LEFT)
        ttk.Button(p, text='Liberar teclas e reativar caneta', command=self._recover_pen_input).pack(pady=(8, 0))

    def _recover_pen_input(self):
        if getattr(self, 'calibration_window', None) is not None and self.calibration_window.winfo_exists():
            messagebox.showinfo('Caneta', 'Conclua ou cancele a calibração antes de recuperar a entrada.', parent=self)
            return
        try:
            atomic_json('/run/tomate-mtm1118/input-command.json', {'action': 'release', 'request_id': uuid.uuid4().hex})
            messagebox.showinfo('Caneta', 'Pedido enviado. Solte os botões, afaste a caneta e aproxime novamente. A segunda tela continuará funcionando.', parent=self)
        except (OSError, ValueError) as exc:
            messagebox.showerror('Caneta', f'Não foi possível enviar o pedido: {exc}', parent=self)

    def _build_page_general(self):
        """Aba 1: Geral (page_general em bostocfgtool.xml)."""
        p = ttk.Frame(self.pages_container, padding=20)
        self.pages.append(p)

        self.var_autostart = tk.BooleanVar(value=True)
        self.var_discard = tk.BooleanVar(value=False)

        ttk.Checkbutton(
            p,
            text="Executar na inicialização do sistema",
            variable=self.var_autostart,
            command=self._on_general_changed,
        ).pack(anchor="w", pady=6)

        ttk.Checkbutton(
            p,
            text="Descartar configurações ao sair",
            variable=self.var_discard,
            command=self._on_general_changed,
        ).pack(anchor="w", pady=6)

    def _build_page_drawing(self):
        """Aba 2: Configurações de desenho (page_drawing em bostocfgtool.xml)."""
        p = ttk.Frame(self.pages_container, padding=12)
        self.pages.append(p)

        top_frm = ttk.Frame(p)
        top_frm.pack(fill=tk.X, pady=(0, 8))

        # Imagem da caneta + 3 comboboxes dos botões da caneta (Button9=Tip, Button8=Barrel1, Button10=Barrel2)
        self.lbl_pen_img = ttk.Label(top_frm)
        if "pen_passive" in self.images:
            self.lbl_pen_img.configure(image=self.images["pen_passive"])
        self.lbl_pen_img.pack(side=tk.LEFT, padx=(4, 12))

        pen_btns_frm = ttk.Frame(top_frm)
        pen_btns_frm.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.pen_btn_combos = {}
        self.pen_btn_labels = {}
        for btn_idx, title in [(8, "Botão superior:"), (9, "Botão inferior:"), (10, "Auxiliar (não confirmado):")]:
            row = ttk.Frame(pen_btns_frm)
            row.pack(fill=tk.X, pady=2)
            label = ttk.Label(row, text=title, width=23)
            label.pack(side=tk.LEFT)
            self.pen_btn_labels[btn_idx] = label
            cb = ttk.Combobox(row, state="readonly", width=18, values=[pt for pt, _ in BUTTON_ACTIONS_PT])
            cb.pack(side=tk.LEFT, padx=4)
            cb.bind("<<ComboboxSelected>>", lambda ev, idx=btn_idx: self._on_button_combo_selected(idx))
            self.pen_btn_combos[btn_idx] = cb
        ttk.Button(pen_btns_frm, text="Restaurar botões padrão", command=self._restore_pen_buttons).pack(anchor="w", pady=(5, 0))

        # Sensibilidade à pressão (slider_pressuresense 0..10) + Curva Bézier ao vivo
        mid_frm = ttk.Frame(p)
        mid_frm.pack(fill=tk.X, pady=4)

        sliders_col = ttk.Frame(mid_frm)
        sliders_col.pack(side=tk.LEFT, fill=tk.X, expand=True)

        ttk.Label(sliders_col, text="Sensibilidade à pressão:").pack(anchor="w")
        sense_row = ttk.Frame(sliders_col)
        sense_row.pack(fill=tk.X, pady=2)
        ttk.Label(sense_row, text="Mais macio", font=("Sans", 8)).pack(side=tk.LEFT)
        self.var_pressure_sense = tk.IntVar(value=5)
        self.slider_sense = tk.Scale(
            sense_row,
            from_=0,
            to=10,
            orient=tk.HORIZONTAL,
            variable=self.var_pressure_sense,
            showvalue=False,
            length=150,
            command=self._on_drawing_changed,
        )
        self.slider_sense.pack(side=tk.LEFT, padx=6)
        ttk.Label(sense_row, text="Mais firme", font=("Sans", 8)).pack(side=tk.LEFT)

        ttk.Label(sliders_col, text="Faixa de pressão:").pack(anchor="w", pady=(4, 0))
        range_row = ttk.Frame(sliders_col)
        range_row.pack(fill=tk.X, pady=2)
        ttk.Label(range_row, text="Menor", font=("Sans", 8)).pack(side=tk.LEFT)
        self.var_pressure_range = tk.IntVar(value=4096)
        self.slider_range = tk.Scale(
            range_row,
            from_=1,
            to=8191,
            orient=tk.HORIZONTAL,
            variable=self.var_pressure_range,
            showvalue=False,
            length=150,
            command=self._on_drawing_changed,
        )
        self.slider_range.pack(side=tk.LEFT, padx=6)
        ttk.Label(range_row, text="Maior", font=("Sans", 8)).pack(side=tk.LEFT)

        # Mini Canvas da Curva de Bézier (idêntico à visualização da curva de pressão)
        self.curve_canvas = tk.Canvas(mid_frm, width=90, height=90, bg="#FFFFFF", relief=tk.SOLID, bd=1)
        self.curve_canvas.pack(side=tk.RIGHT, padx=(10, 4))

        # Barra de Pressão ao vivo (progress_pressure 0..8192)
        press_row = ttk.Frame(p)
        press_row.pack(fill=tk.X, pady=(6, 4))
        ttk.Label(press_row, text="Pressão:", width=10).pack(side=tk.LEFT)
        self.progress_pressure = ttk.Progressbar(press_row, orient=tk.HORIZONTAL, maximum=MAX_PRESSURE, mode="determinate")
        self.progress_pressure.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        self.lbl_pressure_val = ttk.Label(press_row, text="0 / 8192", width=11)
        self.lbl_pressure_val.pack(side=tk.LEFT)

        # Tipo de caneta (Caneta passiva / Caneta ativa)
        ptype_row = ttk.Frame(p)
        ptype_row.pack(fill=tk.X, pady=4)
        ttk.Label(ptype_row, text="Tipo de caneta:", width=14).pack(side=tk.LEFT)
        self.combo_pentype = ttk.Combobox(ptype_row, state="readonly", width=18, values=["Caneta passiva", "Caneta ativa"])
        self.combo_pentype.pack(side=tk.LEFT, padx=4)
        self.combo_pentype.bind("<<ComboboxSelected>>", self._on_drawing_changed)

    def _build_page_buttons(self):
        """Aba 3: Botões do tablet (page_buttons em bostocfgtool.xml)."""
        p = ttk.Frame(self.pages_container, padding=10)
        self.pages.append(p)

        hbox = ttk.Frame(p)
        hbox.pack(fill=tk.BOTH, expand=True)

        left_col = ttk.Frame(hbox)
        left_col.pack(side=tk.LEFT, fill=tk.Y, padx=(4, 8))

        self.tablet_btn_combos = {}
        for i in range(8):
            row = ttk.Frame(left_col)
            row.pack(fill=tk.X, pady=3)
            ttk.Label(row, text=f"K{i + 1}:", width=4).pack(side=tk.LEFT)
            cb = ttk.Combobox(row, state="readonly", width=19, values=[pt for pt, _ in BUTTON_ACTIONS_PT])
            cb.pack(side=tk.LEFT)
            cb.bind("<<ComboboxSelected>>", lambda ev, idx=i: self._on_button_combo_selected(idx))
            self.tablet_btn_combos[i] = cb

        right_col = ttk.Frame(hbox)
        right_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        if "tablet_buttons" in self.images:
            ttk.Label(right_col, image=self.images["tablet_buttons"]).pack(pady=6)

        ttk.Button(
            right_col,
            text="Abrir Ferramenta Rápida",
            command=self._open_quicktool,
        ).pack(pady=4)

    def _build_page_touch(self):
        """Aba 4: Toque (page_touch em bostocfgtool.xml)."""
        p = ttk.Frame(self.pages_container, padding=20)
        self.pages.append(p)

        self.var_disable_all_touch = tk.BooleanVar(value=False)
        self.var_disable_single_touch = tk.BooleanVar(value=True)
        self.var_pen_priority = tk.BooleanVar(value=True)

        self.chk_disable_all = ttk.Checkbutton(
            p,
            text="Desativar todos os toques",
            variable=self.var_disable_all_touch,
            command=self._on_touch_changed,
        )
        self.chk_disable_all.pack(anchor="w", pady=6)

        self.chk_disable_single = ttk.Checkbutton(
            p,
            text="Desativar toque único",
            variable=self.var_disable_single_touch,
            command=self._on_touch_changed,
        )
        self.chk_disable_single.pack(anchor="w", pady=6)

        self.chk_pen_prio = ttk.Checkbutton(
            p,
            text="Prioridade da caneta",
            variable=self.var_pen_priority,
            command=self._on_touch_changed,
        )
        self.chk_pen_prio.pack(anchor="w", pady=6)

    def _build_page_calibration(self):
        """Aba 5: Calibração (page_calibration em bostocfgtool.xml)."""
        p = ttk.Frame(self.pages_container, padding=18)
        self.pages.append(p)

        ttk.Label(p, text="Tela da mesa (detectada pelo serviço de imagem):").pack(anchor="w")
        disp_row = ttk.Frame(p)
        disp_row.pack(fill=tk.X, pady=(4, 10))

        self.combo_displays = ttk.Combobox(disp_row, state="readonly", width=58)
        self.combo_displays.pack(side=tk.LEFT)
        self.combo_displays.bind("<<ComboboxSelected>>", self._on_display_selected)

        btn_row = ttk.Frame(p)
        btn_row.pack(fill=tk.X, pady=(0, 14))
        ttk.Button(btn_row, text="Atualizar", width=11, command=self._refresh_monitors).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_row, text="Detalhes", width=11, command=self._identify_monitors).pack(side=tk.LEFT, padx=6)
        ttk.Button(btn_row, text="Calibrar...", width=12, command=self._start_calibration).pack(side=tk.LEFT, padx=6)

        self.var_flip_axis = tk.BooleanVar(value=False)
        self.var_flip_screen = tk.BooleanVar(value=False)

        mode_box = ttk.LabelFrame(p, text="Modo de Exibição da Mesa", padding=(10, 6))
        mode_box.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(mode_box, text="Monitor integrado ao desktop. Use as configurações de Tela\n"
                  "do COSMIC para estender, espelhar ou organizar as telas.").pack(anchor='w')
        ttk.Button(mode_box, text="Associar caneta à tela Tomate",
                   command=self._map_cosmic_pen).pack(anchor='w', pady=5)

        ttk.Checkbutton(
            p,
            text="Inverter eixo",
            variable=self.var_flip_axis,
            command=self._on_calibration_flags_changed,
        ).pack(anchor="w", pady=5)

        ttk.Checkbutton(
            p,
            text="Inverter tela",
            variable=self.var_flip_screen,
            command=self._on_calibration_flags_changed,
        ).pack(anchor="w", pady=5)

        self.lbl_cal_state = ttk.Label(p, text="", foreground="#1E88E5", font=("Sans", 9))
        self.lbl_cal_state.pack(anchor="w", pady=(10, 0))

    # ========================================================================
    # EVENTOS E SINCRONIZAÇÃO DE DADOS
    # ========================================================================
    def _on_nav_select(self, _event=None):
        sel = self.nav_listbox.curselection()
        if sel:
            self._show_page(sel[0])

    def _show_page(self, idx: int):
        for i, page in enumerate(self.pages):
            if i == idx:
                page.pack(fill=tk.BOTH, expand=True)
            else:
                page.pack_forget()

    def _draw_bezier_preview(self):
        self.curve_canvas.delete("all")
        w, h = 90, 90
        self.curve_canvas.create_line(0, h, w, 0, fill="#E0E0E0", dash=(2, 2))
        d = float(self.var_pressure_sense.get()) / 10.0
        p1_y = 1.0 - d
        pts = []
        for i in range(21):
            t = i / 20.0
            y = 2.0 * (1.0 - t) * t * p1_y + (t * t)
            sx = int(t * (w - 8)) + 4
            sy = int((1.0 - y) * (h - 8)) + 4
            pts.extend([sx, sy])
        if len(pts) >= 4:
            self.curve_canvas.create_line(*pts, fill="#1E88E5", width=2, smooth=True)

    def _sync_ui_from_settings(self):
        gen = self.settings["General"]
        dig = self.settings["Digitizer"]

        self.var_autostart.set(bool(gen.get("AutoStart", True)))
        self.var_discard.set(bool(gen.get("DiscardSetting", False)))

        self.var_pressure_sense.set(int(dig.get("PressureDamping", 5)))
        self.var_pressure_range.set(int(dig.get("PressureMaxValue", 4096)))
        pen_type = int(dig.get("PenType", 0))
        self.combo_pentype.current(1 if pen_type == 1 else 0)
        if pen_type == 1 and "pen_active" in self.images:
            self.lbl_pen_img.configure(image=self.images["pen_active"])
        elif "pen_passive" in self.images:
            self.lbl_pen_img.configure(image=self.images["pen_passive"])
        self._draw_bezier_preview()

        btns = dig.get("Buttons", {})
        titles = (PEN_BUTTON_TITLES
                  if dig.get("ButtonLayout") == "windows" else
                  {8: "Botão inferior:", 9: "Ponta da caneta:", 10: "Botão superior:"})
        for idx, title in titles.items():
            self.pen_btn_labels[idx].configure(text=title)
        for idx, cb in {**self.tablet_btn_combos, **self.pen_btn_combos}.items():
            bcfg = btns.get(f"Button{idx}", {"Action": "Disabled", "Value": ""})
            if isinstance(bcfg, str):
                bcfg = {"Action": "Keystroke", "Value": bcfg}
            cb.set(format_button_label(bcfg))

        tf = int(dig.get("TouchFlags", 6))
        self.var_disable_all_touch.set(bool(tf & 1))
        self.var_disable_single_touch.set(bool(tf & 2))
        self.var_pen_priority.set(bool(tf & 4))
        self._update_touch_checkbox_states()

        self.var_flip_axis.set(bool(dig.get("FlipAxis", False)))
        self.var_flip_screen.set(bool(dig.get("FlipScreen", False)))
        cal_ok = bool(dig.get("CalibrationValid", False))
        self.lbl_cal_state.configure(
            text="Status da calibração: Calibrado (Homografia 4 pontos ativa)"
            if cal_ok
            else "Status da calibração: Padrão de fábrica (17920x10240)"
        )

    def _on_general_changed(self):
        self.settings["General"]["AutoStart"] = bool(self.var_autostart.get())
        self.settings["General"]["DiscardSetting"] = bool(self.var_discard.get())
        self.save_settings()

    def _on_drawing_changed(self, _event=None):
        dig = self.settings["Digitizer"]
        dig["PressureDamping"] = int(self.var_pressure_sense.get())
        dig["PressureMaxValue"] = int(self.var_pressure_range.get())
        pen_type = 1 if self.combo_pentype.current() == 1 else 0
        dig["PenType"] = pen_type
        if pen_type == 1 and "pen_active" in self.images:
            self.lbl_pen_img.configure(image=self.images["pen_active"])
        elif "pen_passive" in self.images:
            self.lbl_pen_img.configure(image=self.images["pen_passive"])
        self._draw_bezier_preview()
        self.save_settings()

    def _on_button_combo_selected(self, btn_idx: int):
        cb = self.tablet_btn_combos.get(btn_idx) or self.pen_btn_combos.get(btn_idx)
        if cb is None:
            return
        selected_pt = cb.get()
        eng_action = PT_TO_ACTION.get(selected_pt, "Disabled")
        btns = self.settings["Digitizer"].setdefault("Buttons", {})
        cur_cfg = btns.get(f"Button{btn_idx}", {"Action": "Disabled", "Value": ""})
        if isinstance(cur_cfg, str):
            cur_cfg = {"Action": "Keystroke", "Value": cur_cfg}

        if eng_action == "Keystroke":
            dlg = KeystrokeDialog(self, cur_cfg.get("Value", ""))
            self.wait_window(dlg)
            if dlg.result is None:
                cb.set(format_button_label(cur_cfg))
                return
            btns[f"Button{btn_idx}"] = {"Action": "Keystroke", "Value": dlg.result}
        else:
            btns[f"Button{btn_idx}"] = {"Action": eng_action, "Value": ""}

        cb.set(format_button_label(btns[f"Button{btn_idx}"]))
        if self.save_settings() is False:
            btns[f"Button{btn_idx}"] = cur_cfg
            cb.set(format_button_label(cur_cfg))

    def _restore_pen_buttons(self):
        if not messagebox.askyesno("Botões da caneta", "Usar o mapeamento padrão do Windows?\n"
                                  "A ponta continua desenhando. O botão inferior faz clique direito, "
                                  "o superior faz clique do meio e o auxiliar fica desativado.\n"
                                  "As personalizações atuais dos três botões serão substituídas.", parent=self):
            return
        dig = self.settings["Digitizer"]
        dig["ButtonLayout"] = "windows"
        dig["ButtonReportVersion"] = 2
        buttons = dig.setdefault("Buttons", {})
        for idx in (8, 9, 10):
            buttons[f"Button{idx}"] = dict(DEFAULT_SETTINGS["Digitizer"]["Buttons"][f"Button{idx}"])
        self.save_settings()
        self._sync_ui_from_settings()

    def _update_touch_checkbox_states(self):
        if self.var_disable_all_touch.get():
            self.chk_disable_single.configure(state=tk.DISABLED)
            self.chk_pen_prio.configure(state=tk.DISABLED)
        else:
            self.chk_disable_single.configure(state=tk.NORMAL)
            self.chk_pen_prio.configure(state=tk.NORMAL)

    def _on_touch_changed(self):
        """Replica FUN_00423c20: TouchFlags = bit0(DisableAll) | bit1(DisableSingle) | bit2(PenPriority)."""
        flags = 0
        if self.var_disable_all_touch.get():
            flags |= 1
        if self.var_disable_single_touch.get():
            flags |= 2
        if self.var_pen_priority.get():
            flags |= 4
        self.settings["Digitizer"]["TouchFlags"] = flags
        self._update_touch_checkbox_states()
        self.save_settings()

    def _refresh_monitors(self):
        """The one USB panel is stable even when other monitors are reordered/scaled."""
        self.monitors_list = []
        try:
            mon = panel_geometry(fresh_json(DISPLAY_STATE_PATH))
            label = f"Tomate — {mon['width']}×{mon['height']} (imagem {mon['mode_width']}×{mon['mode_height']})"
            self.monitors_list.append((label, 'Tomate USB', mon))
        except ValueError:
            pass
        self.combo_displays["values"] = [item[0] for item in self.monitors_list]
        if self.monitors_list:
            self.combo_displays.current(0)
        else:
            self.combo_displays.set('Mesa sem imagem ativa — conecte e clique Atualizar')

    def _on_display_selected(self, _event=None):
        idx = max(0, self.combo_displays.current())
        if idx < len(self.monitors_list):
            selected = self.monitors_list[idx][1]
            if selected != self.settings["General"].get("TargetDisplay"):
                self.settings["Digitizer"]["CalibrationValid"] = False
            self.settings["General"]["TargetDisplay"] = selected
            # Assim como FUN_004249f0 -> FUN_00422070, trocar de monitor reseta a calibração para o monitor
            self.save_settings()
            self._sync_ui_from_settings()

    def _identify_monitors(self):
        self._refresh_monitors()
        if not self.monitors_list:
            messagebox.showerror('Tela da mesa', 'Não há imagem ativa confirmada pelo serviço da mesa.', parent=self)
            return
        mon = self.monitors_list[0][2]
        messagebox.showinfo('Tela da mesa', f"Tela física: {mon['width']}×{mon['height']}\n"
                            f"Imagem do Linux: {mon['mode_width']}×{mon['mode_height']}, {mon['refresh_rate']} Hz\n\n"
                            'A calibração aparece diretamente na mesa, independentemente da posição dos outros monitores. '
                            'Valores como 1920:280 na versão anterior indicavam posição X:Y, não resolução.', parent=self)

    def _start_calibration(self):
        if self.settings["General"].get("DiscardSetting", False):
            messagebox.showinfo("Calibração", "Desative 'Descartar configurações' para salvar a calibração.", parent=self)
            return
        if getattr(self, "calibration_window", None) is not None and self.calibration_window.winfo_exists():
            self.calibration_window.lift()
            return
        self._refresh_monitors()
        try:
            mon = panel_geometry(fresh_json(DISPLAY_STATE_PATH))
        except ValueError as exc:
            messagebox.showerror('Calibração', str(exc), parent=self)
            return
        self.calibration_window = CalibrationWindow(self, mon, self._on_calibration_completed)

    def _on_calibration_completed(self, src_pts, dst_pts):
        """Salva CalibrationValid=True, Source e Target no formato exato de FUN_00422070."""
        validate_calibration_points(src_pts)
        previous = json.loads(json.dumps(self.settings["Digitizer"]))
        src_str = ", ".join(f"{int(x)}:{int(y)}" for x, y in src_pts)
        dst_str = ", ".join(f"{int(x)}:{int(y)}" for x, y in dst_pts)
        self.settings["Digitizer"]["CalibrationValid"] = True
        self.settings["Digitizer"]["Calibration"] = {
            "Source": src_str,
            "Target": dst_str,
        }
        if not self.save_settings():
            self.settings["Digitizer"] = previous
        self._sync_ui_from_settings()

    def _on_calibration_flags_changed(self):
        """Replica FUN_00423fb0 para FlipAxis e FlipScreen."""
        self.settings["Digitizer"]["FlipAxis"] = bool(self.var_flip_axis.get())
        self.settings["Digitizer"]["FlipScreen"] = bool(self.var_flip_screen.get())
        self.settings["Digitizer"]["CalibrationValid"] = False
        self.save_settings()
        self._sync_ui_from_settings()

    def _map_cosmic_pen(self):
        script = Path(__file__).resolve().parent.parent / 'scripts/map_cosmic_pen.py'
        try:
            result = subprocess.run([sys.executable, str(script), '--wait', '2'],
                                    capture_output=True, text=True, timeout=6)
            text = result.stdout.strip() or result.stderr.strip() or 'Não foi possível aplicar a associação.'
            dialog = messagebox.showinfo if result.returncode == 0 else messagebox.showerror
            dialog('Associação da caneta', text, parent=self)
        except (OSError, subprocess.TimeoutExpired) as exc:
            messagebox.showerror('Associação da caneta', str(exc), parent=self)

    def _open_quicktool(self):
        if self.quicktool_win is not None and self.quicktool_win.winfo_exists():
            self.quicktool_win.lift()
            return
        self.quicktool_win = QuickToolWindow(self)

    def _on_restore_defaults(self):
        """Replica o botão Restaurar padrões (ID 17 em tabletlang.xml)."""
        ans = messagebox.askyesno(
            "Restaurar padrões",
            "Tem certeza de que deseja restaurar as configurações padrão?",
            parent=self,
        )
        if ans:
            self.settings = json.loads(json.dumps(DEFAULT_SETTINGS))
            self._sync_ui_from_settings()
            self.save_settings()

    def _on_about(self):
        AboutDialog(self, self.images.get("logo"))

    def _apply_start_minimized(self):
        if self.tray_running:
            self.withdraw()
        else:
            self.iconify()

    def _on_minimize_to_tray(self):
        if self.tray_running:
            self.withdraw()
        else:
            self.iconify()

    def _on_quit(self):
        self.save_settings()
        self.destroy()
        sys.exit(0)

    def _run_system_tray(self):
        icon_path = os.path.join(ASSETS_DIR, "mainmenuicon.png")
        if not (HAS_PIL and os.path.exists(icon_path)):
            return
        try:
            image = Image.open(icon_path)
            menu = pystray.Menu(
                pystray.MenuItem("Configurações...", lambda: self.after(0, lambda: (self.deiconify(), self.lift())), default=True),
                pystray.MenuItem("Calibrar...", lambda: self.after(0, self._start_calibration)),
                pystray.MenuItem("Ferramenta Rápida", lambda: self.after(0, self._open_quicktool)),
                pystray.MenuItem("Sair", lambda: self.after(0, self._on_quit)),
            )
            tray = pystray.Icon("tomate_mtm1118", image, "Tablet Digitizer — Tomate MTM-1118", menu)

            def _setup(icon):
                icon.visible = True
                self.tray_running = True

            tray.run(setup=_setup)
        except Exception as exc:
            self.tray_running = False
            print(f"[AVISO] Ícone da bandeja indisponível ({exc}); usando janela minimizada.")

    def _poll_live_telemetry(self):
        """Lê a telemetria em /run a 25Hz para atualizar pressão e status."""
        try:
            if os.path.exists(STATE_PATH):
                with open(STATE_PATH, "r", encoding="utf-8") as f:
                    st = json.load(f)
                fresh = (time.time() - float(st.get("timestamp", 0))) < 5.0
                pen_ok = bool(st.get("pen_connected", False)) and fresh
                touch_ok = bool(st.get("touch_connected", False)) and fresh
                p_val = int(st.get("pressure", 0)) if pen_ok else 0

                self.lbl_pen_status.configure(
                    text="Apresentado" if pen_ok else "Desconectado",
                    foreground="#1B5E20" if pen_ok else "#B71C1C",
                )
                self.lbl_touch_status.configure(
                    text="Apresentado" if touch_ok else "Não apresentado",
                )
                self.lbl_live_coords.configure(
                    text=f"X: {st.get('cal_x', 0)}  Y: {st.get('cal_y', 0)}  Pressão: {p_val}  Tilt: ({st.get('tilt_x', 0)}, {st.get('tilt_y', 0)})"
                )
                self.progress_pressure["value"] = p_val
                self.lbl_pressure_val.configure(text=f"{p_val} / {MAX_PRESSURE}")

                if st.get("quicktool_trigger", False) or st.get('quicktool_timestamp', 0):
                    ts = float(st.get('quicktool_timestamp') or st.get("timestamp", 0))
                    if ts > self.last_quicktool_ts and 0 <= time.time() - ts < 5:
                        self.last_quicktool_ts = ts
                        self._open_quicktool()
        except Exception:
            pass
        self.after(40, self._poll_live_telemetry)


def main():
    parser = argparse.ArgumentParser(description="Painel de Controle Tomate MTM-1118 (BostoCfgTool)")
    parser.add_argument("--minimized", action="store_true", help="Iniciar minimizado na bandeja do sistema")
    args = parser.parse_args()

    app = TomateControlCenter(start_minimized=args.minimized)
    app.mainloop()


if __name__ == "__main__":
    main()
