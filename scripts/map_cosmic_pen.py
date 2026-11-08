"""Associate this port's stylus with the Tomate EVDI output in COSMIC.

COSMIC can discard tablet input on external-only systems without map_to_output.
See https://github.com/pop-os/cosmic-comp/issues/2687.
Only this stylus's state/map_to_output fields are edited; other RON is retained.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

PEN_NAME = 'Tomate MTM-1118 Bosto Stylus Pen'
PANEL_ID = bytes.fromhex('05230e01')  # EDID manufacturer/product, not serial number.


class MappingError(RuntimeError):
    pass


def tokens(text):
    """Token spans for a lossless, narrowly scoped edit of COSMIC's RON map."""
    result, i = [], 0
    while i < len(text):
        if text[i].isspace():
            i += 1
            continue
        if text.startswith('//', i):
            end = text.find('\n', i)
            i = len(text) if end < 0 else end + 1
            continue
        if text.startswith('/*', i):
            depth, i = 1, i + 2
            while depth and i < len(text):
                if text.startswith('/*', i):
                    depth, i = depth + 1, i + 2
                elif text.startswith('*/', i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            if depth:
                raise MappingError('Comentário RON incompleto; configuração preservada.')
            continue
        start = i
        raw = re.match(r'r(#+)?"', text[i:])
        if raw:
            delimiter = '"' + (raw[1] or '')
            end = text.find(delimiter, i + raw.end())
            if end < 0:
                raise MappingError('String RON incompleta; configuração preservada.')
            i = end + len(delimiter)
        elif text[i] in ('"', "'"):
            quote, i = text[i], i + 1
            while i < len(text) and text[i] != quote:
                i += 2 if text[i] == '\\' else 1
            if i >= len(text):
                raise MappingError('String RON incompleta; configuração preservada.')
            i += 1
        elif text[i] in '{}[]():,#!':
            i += 1
        else:
            while i < len(text) and not text[i].isspace() and text[i] not in '{}[]():,#!"\'':
                if text.startswith(('//', '/*'), i):
                    break
                i += 1
            if i == start:
                raise MappingError('Sintaxe RON não reconhecida; configuração preservada.')
        result.append((text[start:i], start, i))
    return result


def structure(ts):
    stack, pairs = [], {}
    for i, (value, _, _) in enumerate(ts):
        if value in ('{', '[', '('):
            stack.append(i)
        elif value in ('}', ']', ')'):
            if not stack or ts[stack[-1]][0] != {'}': '{', ']': '[', ')': '('}[value]:
                raise MappingError('Estrutura RON inválida; configuração preservada.')
            opening = stack.pop()
            pairs[opening] = i
    if stack:
        raise MappingError('Estrutura RON incompleta; configuração preservada.')
    return pairs


def members(ts, pairs, opening):
    """Return key/value token spans at one nesting level, skipping child values."""
    result, closing = [], pairs[opening]
    i = opening + 1
    while i < closing:
        key = i
        if i + 2 >= closing or ts[i + 1][0] != ':':
            raise MappingError('Campos RON não reconhecidos; configuração preservada.')
        first = i = i + 2
        while i < closing and ts[i][0] != ',':
            i = pairs[i] + 1 if i in pairs else i + 1
        if i == first:
            raise MappingError('Valor RON ausente; configuração preservada.')
        result.append((key, first, i))
        if i < closing:
            i += 1
    return result


def string_value(value):
    raw = re.fullmatch(r'r(#+)?"(.*)"\1', value, re.S) if value.startswith('r#') else None
    if raw:
        return raw[2]
    if value.startswith('r"') and value.endswith('"'):
        return value[2:-1]
    try:
        value = re.sub(r'\\u\{([0-9a-fA-F]+)\}',
                       lambda m: json.dumps(chr(int(m[1], 16)))[1:-1], value)
        return json.loads(value)
    except (ValueError, TypeError):
        raise MappingError('Nome de dispositivo RON não reconhecido; configuração preservada.')


def merge_mapping(text, output):
    ts = tokens(text)
    pairs = structure(ts)
    root = 0
    # Retain optional RON file extension attributes before the map.
    while root + 2 < len(ts) and [t[0] for t in ts[root:root + 3]] == ['#', '!', '[']:
        root = pairs[root + 2] + 1
    if root >= len(ts) or ts[root][0] != '{' or pairs[root] != len(ts) - 1:
        raise MappingError('Esperava um mapa RON do COSMIC; configuração preservada.')
    entries = members(ts, pairs, root)
    own = [entry for entry in entries if string_value(ts[entry[0]][0]) == PEN_NAME]
    if len(own) > 1:
        raise MappingError('Entrada duplicada da caneta no COSMIC; configuração preservada.')
    target = 'Some(' + json.dumps(output, ensure_ascii=False) + ')'
    if not own:
        close = pairs[root]
        separator = ',' if entries and ts[close - 1][0] != ',' else ''
        addition = (separator + '\n    ' + json.dumps(PEN_NAME) + ': (\n'
                    '        state: Enabled,\n        map_to_output: ' + target + ',\n    ),\n')
        pos = ts[close][1]
        return text[:pos] + addition + text[pos:]
    _, first, last = own[0]
    opening = first
    if ts[opening][0] == 'InputConfig':
        opening += 1
    if ts[opening][0] != '(' or pairs.get(opening) != last - 1:
        raise MappingError('Configuração da caneta não reconhecida; arquivo preservado.')
    fields = members(ts, pairs, opening)
    edits, missing = [], []
    for name, value in (('state', 'Enabled'), ('map_to_output', target)):
        found = [field for field in fields if ts[field[0]][0] == name]
        if len(found) > 1:
            raise MappingError('Campo duplicado na configuração da caneta; arquivo preservado.')
        if found:
            _, start, end = found[0]
            edits.append((ts[start][1], ts[end - 1][2], value))
        else:
            missing.append(f'        {name}: {value},\n')
    if missing:
        close = pairs[opening]
        separator = ',' if fields and ts[close - 1][0] != ',' else ''
        edits.append((ts[close][1], ts[close][1], separator + '\n' + ''.join(missing)))
    for start, end, value in sorted(edits, reverse=True):
        text = text[:start] + value + text[end:]
    return text


def detect_target(drm_root=Path('/sys/class/drm'), input_root=Path('/sys/class/input'), output=None):
    found_pen = False
    for entry in input_root.glob('event*'):
        try:
            found_pen |= (entry / 'device/name').read_text().strip() == PEN_NAME
        except OSError:
            continue
    if not found_pen:
        raise MappingError('Dispositivo virtual da caneta ausente; confira tomate-pen.service.')
    candidates = []
    for connector in sorted(drm_root.glob('card*-*')):
        try:
            if not any(part.startswith('evdi.') for part in connector.resolve().parts):
                continue
            if (connector / 'status').read_text().strip() != 'connected':
                continue
            name = re.sub(r'^card[0-9]+-', '', connector.name)
            if output is not None:
                if name == output:
                    candidates.append(name)
                continue
            edid = (connector / 'edid').read_bytes()[:128]
            if (len(edid) == 128 and edid[:8] == bytes.fromhex('00ffffffffffff00')
                    and edid[8:12] == PANEL_ID and sum(edid) % 256 == 0):
                candidates.append(name)
        except OSError:
            continue
    if len(candidates) != 1:
        raise MappingError('Não foi possível identificar uma única tela Tomate EVDI conectada. '
                           'Ative a mesa em Telas; para seleção explícita use --output DVI-I-1.')
    return candidates[0]


def apply_mapping(config, output, backup_dir):
    if config.is_symlink():
        raise MappingError('O arquivo input_devices é simbólico; configuração preservada.')
    exists = config.exists()
    original = config.read_bytes() if exists else None
    if exists and not original.strip():
        raise MappingError('Arquivo input_devices vazio; tente novamente após o COSMIC terminar de gravar.')
    updated = merge_mapping(original.decode('utf-8') if exists else '{}\n', output).encode('utf-8')
    if updated == original:
        return False, None
    config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = None
    if exists:
        backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(dir=backup_dir, prefix='input_devices-', suffix='.ron', delete=False) as stream:
            stream.write(original)
            backup = Path(stream.name)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=config.parent, prefix='.tomate-input-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(updated)
            os.fchmod(stream.fileno(), (config.stat().st_mode & 0o666) if exists else 0o644)
            stream.flush()
            os.fsync(stream.fileno())
        if config.is_symlink() or (config.read_bytes() if config.exists() else None) != original:
            raise MappingError('O COSMIC alterou a configuração durante a operação; tente novamente.')
        os.replace(temporary, config)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True, backup


def cosmic_session():
    if 'COSMIC' in os.getenv('XDG_CURRENT_DESKTOP', '').upper():
        return True
    return bool(shutil.which('pgrep') and subprocess.run(
        ['pgrep', '-u', str(os.getuid()), '-x', 'cosmic-comp'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', help='nome explícito do conector EVDI, como DVI-I-1')
    parser.add_argument('--wait', type=float, default=0, help='aguardar até N segundos pela mesa')
    parser.add_argument('--check', action='store_true', help='diagnóstico sem alterações')
    args = parser.parse_args()
    if not 0 <= args.wait <= 60:
        parser.error('--wait deve estar entre 0 e 60')
    if not args.check and (os.geteuid() == 0 or not cosmic_session()):
        parser.error('Execute no terminal do COSMIC como usuário comum, sem sudo.')
    home = Path.home()
    config = Path(os.getenv('XDG_CONFIG_HOME', home / '.config')) / 'cosmic/com.system76.CosmicComp/v1/input_devices'
    backups = Path(os.getenv('XDG_STATE_HOME', home / '.local/state')) / 'tomate-mtm1118/backups'
    try:
        deadline = time.monotonic() + args.wait
        while True:
            try:
                output = detect_target(output=args.output)
                break
            except MappingError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.5)
        if args.check:
            original = config.read_text() if config.exists() else '{}\n'
            print(json.dumps({'pen': PEN_NAME, 'output': output, 'config': str(config),
                              'configured': config.exists() and merge_mapping(original, output) == original}, indent=2))
        else:
            changed, backup = apply_mapping(config, output, backups)
            print(f'Caneta associada a {output} no COSMIC.' + (' Configuração já estava correta.' if not changed else ''))
            if backup:
                print(f'Backup: {backup}')
            print('Afaste a caneta da mesa e aproxime novamente para testar.')
    except (MappingError, OSError, UnicodeError) as exc:
        print(f'[ASSOCIAÇÃO] {exc}')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
