# Copyright 2026 Alefk1708
# SPDX-License-Identifier: Apache-2.0
# See LICENSE, NOTICE and THIRD_PARTY_NOTICES.md for scope and attribution.
"""Render calibration directly into the panel stream, independent of XWayland outputs."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'userspace-pen'))
from calibration_protocol import PROTOCOL, DISPLAY_STATE_PATH, calibration_targets, fresh_json

PEN_STATE_PATH = Path('/run/tomate-mtm1118/state.json')


class CalibrationDisplay:
    def __init__(self, width, height):
        self.width, self.height = width, height
        self.key = self.image = self.encoded = None
        self.frame_id = ''
        self.shown = None
        self.last_publish = 0
        self.was_calibrating = False
        self.read_stamp = self.state = None
        self.publish_error = False

    def _pen_state(self):
        try:
            stamp = PEN_STATE_PATH.stat().st_mtime_ns
        except OSError:
            self.state = None
            return None
        if stamp != self.read_stamp:
            self.read_stamp = stamp
            self.state = fresh_json(PEN_STATE_PATH)
        state = self.state
        if not state or not 0 <= time.time() - float(state.get('timestamp', 0)) < 4:
            return None
        if (state.get('cal_protocol') != PROTOCOL or not state.get('cal_mode')
                or state.get('cal_renderer') != 'usb' or not state.get('pen_connected')):
            return None
        step = state.get('cal_step')
        session = state.get('cal_session_id')
        if type(step) is not int or not 0 <= step < 4 or not isinstance(session, str) or not session:
            return None
        return state

    def payload(self, encode):
        """Return a cached overlay. The caller must acknowledge only after USB send succeeds."""
        state = self._pen_state()
        if state is None:
            self.key = self.image = self.encoded = None
            self.shown = None
            return None
        key = (state['cal_session_id'], state['cal_step'], bool(state.get('cal_flip_screen')))
        if key != self.key:
            from PIL import Image, ImageDraw, ImageFont
            image = Image.new('RGB', (self.width, self.height), '#f2f2f2')
            draw = ImageDraw.Draw(image)
            try:
                font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 20)
            except OSError:
                font = ImageFont.load_default()
            x, y = calibration_targets(self.width, self.height)[0][key[1]]
            draw.line((x - 16, y, x + 16, y), fill='#cf0000', width=3)
            draw.line((x, y - 16, x, y + 16), fill='#cf0000', width=3)
            draw.ellipse((x - 5, y - 5, x + 5, y + 5), outline='black', width=2)
            labels = ('TOMATE - Calibracao', f'Ponto {key[1] + 1}/4',
                      'Levante a ponta, toque a mira e levante novamente.',
                      'Aguarde a proxima mira. Nao pressione os botoes.', 'ESC no painel para cancelar.')
            for i, label in enumerate(labels):
                draw.text((self.width // 2, self.height // 2 - 75 + i * 32), label,
                          font=font, fill='#151515', anchor='mm')
            self.key, self.image = key, image
            self.encoded = encode(image)
            self.frame_id = uuid.uuid4().hex
            self.shown = None  # A failed USB send must never acknowledge a new target.
        return self.encoded

    def sent(self):
        if self.key is not None:
            self.shown = (self.key, self.frame_id)

    def publish(self, capture, force=False, active=None):
        now = time.monotonic()
        if not force and now - self.last_publish < .5:
            return
        mode = getattr(capture, 'mode', None)
        enabled = bool(capture.dpms_on and capture.crtc_on) if active is None else active
        state = {'protocol': PROTOCOL, 'timestamp': time.time(), 'active': enabled,
                 'panel_width': self.width, 'panel_height': self.height,
                 'mode_width': getattr(mode, 'width', None), 'mode_height': getattr(mode, 'height', None),
                 'refresh_rate': getattr(mode, 'refresh_rate', None),
                 'cal_session_id': '', 'cal_step': None, 'cal_frame_id': ''}
        if enabled and self.shown is not None:
            (session, step, flip), frame = self.shown
            state.update(cal_session_id=session, cal_step=step, cal_flip_screen=flip, cal_frame_id=frame)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=DISPLAY_STATE_PATH.parent,
                                             prefix='.display-', delete=False) as stream:
                temporary = stream.name
                json.dump(state, stream, allow_nan=False)
                os.fchmod(stream.fileno(), 0o644)
            os.replace(temporary, DISPLAY_STATE_PATH)
            self.publish_error = False
        except OSError as exc:
            if not self.publish_error:
                print(f'[CALIBRAÇÃO] Não foi possível publicar a confirmação da imagem: {exc}', flush=True)
            self.publish_error = True
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
        self.last_publish = now
