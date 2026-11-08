# Copyright 2026 Alefk1708
# SPDX-License-Identifier: Apache-2.0
# See LICENSE, NOTICE and THIRD_PARTY_NOTICES.md for scope and attribution.
"""Calibration coordinates belong to the USB panel, never the desktop layout."""
import json
import math
import time
from pathlib import Path

PROTOCOL = 3
DISPLAY_STATE_PATH = Path('/run/tomate-mtm1118/display-state.json')
MAX_X, MAX_Y = 17920, 10240


def calibration_targets(width, height, flip_screen=False):
    if type(width) is not int or type(height) is not int or not (128 <= width <= 8192 and 128 <= height <= 8192):
        raise ValueError('Resolução de monitor inválida para calibração.')
    screen = [(32, 32), (width - 32, 32), (width - 32, height - 32), (32, height - 32)]
    target = [(round(x * MAX_X / width), round(y * MAX_Y / height)) for x, y in screen]
    if flip_screen:
        target = [(MAX_X - x, MAX_Y - y) for x, y in target]
    return screen, target


def fresh_json(path, max_age=4, now=None):
    try:
        with open(path, encoding='utf-8') as stream:
            state = json.loads(stream.read(65536))
        timestamp = float(state.get('timestamp', 0))
        age = (time.time() if now is None else now) - timestamp
        return state if math.isfinite(age) and 0 <= age < max_age else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def panel_geometry(state):
    """Reject stale/invalid data; never replace the panel with a whole-desktop rectangle."""
    if not state or state.get('protocol') != PROTOCOL or not state.get('active'):
        raise ValueError('O serviço de imagem não confirmou uma tela Tomate ativa. Atualize o pacote completo e reconecte a mesa.')
    width, height = state.get('panel_width'), state.get('panel_height')
    calibration_targets(width, height)
    return {'left': 0, 'top': 0, 'width': width, 'height': height,
            'mode_width': state.get('mode_width'), 'mode_height': state.get('mode_height'),
            'refresh_rate': state.get('refresh_rate')}


def target_is_displayed(state, session, step, flip, now=None):
    """USB completion acknowledges a particular physical crosshair, not a Tk repaint."""
    try:
        panel_geometry(state)
        age = (time.time() if now is None else now) - float(state['timestamp'])
        return (math.isfinite(age) and 0 <= age < 4
                and state.get('cal_session_id') == session and state.get('cal_step') == step
                and state.get('cal_flip_screen') is bool(flip)
                and isinstance(state.get('cal_frame_id'), str) and bool(state['cal_frame_id']))
    except (ValueError, TypeError, KeyError):
        return False
