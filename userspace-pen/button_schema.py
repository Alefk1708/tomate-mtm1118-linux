"""Physical MTM-1118 mapping verified from captured report 01 button presses."""
import copy


KEY_ALIASES = {
    'CONTROL': 'CTRL', 'LCTRL': 'CTRL', 'LSHIFT': 'SHIFT', 'LALT': 'ALT',
    'SUPER': 'WIN', 'META': 'WIN', 'SPACEBAR': 'SPACE', 'BARRA DE ESPAÇO': 'SPACE',
    'RETURN': 'ENTER', 'ESCAPE': 'ESC', 'DEL': 'DELETE', 'INSERT': 'INS',
    'PRIOR': 'PGUP', 'PAGEUP': 'PGUP', 'NEXT': 'PGDN', 'PAGEDOWN': 'PGDN',
    'BRACKETLEFT': '[', 'BRACKETRIGHT': ']', 'MINUS': '-', 'EQUAL': '=',
    'PLUS': '+', 'KP_ADD': '+', 'COMMA': ',', 'PERIOD': '.',
    'SLASH': '/', 'BACKSLASH': '\\', 'SEMICOLON': ';', 'APOSTROPHE': "'", 'GRAVE': '`',
}
KEY_NAMES = set('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') | {
    'CTRL', 'SHIFT', 'ALT', 'WIN', 'SPACE', 'TAB', 'ENTER', 'ESC', 'BACKSPACE',
    'DELETE', 'INS', 'HOME', 'END', 'PGUP', 'PGDN', 'UP', 'DOWN', 'LEFT', 'RIGHT',
    '[', ']', '-', '=', '+', ',', '.', '/', '\\', ';', "'", '`',
} | {f'F{i}' for i in range(1, 25)}


def keystroke_tokens(value):
    """One parser for Tk key names and driver shortcuts; reject partial matches."""
    if not isinstance(value, str):
        return []
    value = value.strip()
    tokens = [token.strip().upper() for token in value.split('+') if token.strip()]
    if value == '+' or value.endswith('++'):
        tokens.append('+')
    tokens = [KEY_ALIASES.get(token, token) for token in tokens]
    if any(token not in KEY_NAMES for token in tokens):
        return []
    return list(dict.fromkeys(tokens))

BUTTON_REPORT_VERSION = 2
# Windows logical slots remain unchanged; their physical labels are corrected.
PEN_BUTTON_TITLES = {8: "Botão superior:", 9: "Botão inferior:", 10: "Auxiliar (não confirmado):"}


def migrate_button_settings(settings):
    """Move the old 'upper' assignment from slot10 to the observed slot8.

    Only the previous Windows profile schema had this labeling error. Legacy
    profiles stay explicitly legacy until the user restores their defaults.
    """
    result = copy.deepcopy(settings)
    digitizer = result.setdefault('Digitizer', {})
    if digitizer.get('ButtonLayout') == 'windows' and digitizer.get('ButtonReportVersion', 1) < BUTTON_REPORT_VERSION:
        buttons = digitizer.setdefault('Buttons', {})
        upper = buttons.get('Button10', {'Action': 'Middle Click', 'Value': ''})
        auxiliary = buttons.get('Button8', {'Action': 'Disabled', 'Value': ''})
        buttons['Button8'], buttons['Button10'] = upper, auxiliary
        digitizer['ButtonReportVersion'] = BUTTON_REPORT_VERSION
    return result
