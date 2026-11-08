# Copyright 2026 Alefk1708
# SPDX-License-Identifier: Apache-2.0
# See LICENSE, NOTICE and THIRD_PARTY_NOTICES.md for scope and attribution.
"""Read-only Linux deployment preflight; never loads a module or opens a GPU."""
import argparse
import ctypes
import ctypes.util
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys


def command(*args):
    if not shutil.which(args[0]):
        return None
    try:
        result = subprocess.run(args, text=True, capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def drm_connectors():
    result = []
    for connector in sorted(Path('/sys/class/drm').glob('card*-*')):
        item = {'name': connector.name}
        for field in ('status', 'enabled', 'modes'):
            try:
                content = (connector / field).read_text().strip()
                item[field] = content.splitlines() if field == 'modes' else content
            except OSError:
                item[field] = None
        result.append(item)
    return result


def drm_validation_errors(kernel, module, library, library_version, library_error=None):
    errors = []
    if not module:
        errors.append(f'Módulo EVDI ausente para o kernel {kernel}.')
    elif module.split()[0] != kernel:
        errors.append(f'O módulo EVDI encontrado não corresponde ao kernel {kernel}.')
    if not library:
        errors.append('Biblioteca libevdi ausente; instalar somente o módulo DKMS não fornece essa biblioteca.')
    elif not library_version:
        errors.append(f'Não foi possível carregar/consultar a biblioteca libevdi: {library_error or library}.')
    elif library_version[0] != 1 or library_version[1] not in (14, 15):
        errors.append(f'ABI libevdi não suportada: {library_version}; esperado 1.14 ou 1.15.')
    return errors


def inspect():
    kernel = platform.release()
    module = command("modinfo", "-F", "vermagic", "evdi")
    loaded_version = None
    for path in (Path('/sys/devices/evdi/version'), Path('/sys/module/evdi/version')):
        try:
            loaded_version = path.read_text().strip()
            break
        except OSError:
            continue
    library_version, library_error = None, None
    private = Path('/opt/tomate-mtm1118/evdi-lib/libevdi.so.1')
    library = str(private) if private.is_file() else ctypes.util.find_library("evdi")
    if library:
        try:
            version = (ctypes.c_int * 3)()
            lib = ctypes.CDLL(library)
            lib.evdi_get_lib_version.argtypes = [ctypes.c_void_p]
            lib.evdi_get_lib_version.restype = None
            lib.evdi_get_lib_version(ctypes.byref(version))
            library_version = list(version)
        except (AttributeError, OSError) as exc:
            library_error = str(exc)
    build = Path("/lib/modules") / kernel / "build"
    header_files = {name: (build / name).is_file() for name in ("Makefile", "Module.symvers")}
    imports = {name: importlib.util.find_spec(name) is not None
               for name in ("usb", "evdev", "PIL", "cryptography", "tkinter", "gi")}
    wsl = "microsoft" in kernel.lower()
    drm_errors = drm_validation_errors(kernel, module, library, library_version, library_error)
    return {
        "kernel": kernel, "wsl": wsl,
        "os_release": Path("/etc/os-release").read_text() if Path("/etc/os-release").is_file() else None,
        "session": {key: os.getenv(key) for key in ("XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP", "DISPLAY", "WAYLAND_DISPLAY")},
        "kernel_build": str(build), "kernel_build_files": header_files,
        "evdi_module_vermagic": module, "evdi_library_version": library_version,
        "evdi_module_filename": command("modinfo", "-F", "filename", "evdi"),
        "evdi_loaded_version": loaded_version,
        "evdi_library_path": library,
        "evdi_library_error": library_error,
        "evdi_errors": drm_errors,
        "evdi_candidate": not drm_errors,
        "evdi_loaded": Path("/sys/module/evdi").exists(),
        "drm_connectors": drm_connectors(),
        "drm_service": command("systemctl", "is-active", "tomate-display-monitor.service"),
        "python_modules": imports,
        "secure_boot": command("mokutil", "--sb-state"),
        "usb": command("lsusb"),
        "note": "Esta versão instala somente DRM no Linux nativo. EVDI e sua biblioteca são necessários. Este diagnóstico não carrega módulos."
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drm-candidate", action="store_true")
    args = parser.parse_args()
    report = inspect()
    if args.drm_candidate:
        for error in report['evdi_errors']:
            print(f'[EVDI] {error}', file=sys.stderr)
        raise SystemExit(0 if report["evdi_candidate"] else 1)
    print(json.dumps(report, indent=2, ensure_ascii=False))
