#!/usr/bin/env bash
# Copyright 2026 Alefk1708
# SPDX-License-Identifier: Apache-2.0
# See LICENSE, NOTICE and THIRD_PARTY_NOTICES.md for scope and attribution.
# Tomate MTM-1118: native DRM monitor only.
set -Eeuo pipefail
cd "$(dirname "$(readlink -f "$0")")"
unset PYTHONPATH PYTHONHOME
deps=true
desktop_user="${SUDO_USER:-}"
while [ "$#" -gt 0 ]; do
    case "$1" in
        --user) desktop_user="${2:?Informe o usuário gráfico}"; shift 2 ;;
        --no-deps) deps=false; shift ;;
        --check) exec python3 -B scripts/check_environment.py ;;
        --help) echo 'Uso: sudo ./install.sh [--user USUARIO] [--no-deps]'; echo 'Instala somente a segunda tela DRM, sem Krita.'; exit 0 ;;
        *) echo "Opção não aceita: $1. Use sudo ./install.sh; esta versão instala somente DRM."; exit 2 ;;
    esac
done
if [ "$EUID" -ne 0 ]; then echo 'Execute: sudo ./install.sh, ou abra Instalar.desktop.'; exit 1; fi
if uname -r | grep -qi microsoft; then echo 'Esta versão DRM requer Linux instalado diretamente no computador; não instala no WSL.'; exit 1; fi
if ! command -v apt-get >/dev/null; then echo 'Este instalador requer Pop!_OS/Ubuntu com apt.'; exit 1; fi
if [ -z "$desktop_user" ] || ! id "$desktop_user" >/dev/null 2>&1 || [ "$(id -u "$desktop_user")" -eq 0 ]; then
    echo 'Informe o usuário gráfico com --user USUARIO.'; exit 2
fi
desktop_uid="$(id -u "$desktop_user")"
desktop_home="$(getent passwd "$desktop_user" | cut -d: -f6)"
for path in /opt/tomate-mtm1118 /opt/tomate-mtm1118/userspace-pen /opt/tomate-mtm1118/display-driver \
    /opt/tomate-mtm1118/gui /opt/tomate-mtm1118/scripts /opt/tomate-mtm1118/evdi-lib \
    /opt/tomate-mtm1118/python-libs /etc/tomate-mtm1118 /etc/tomate-mtm1118/settings.json; do
    if [ -L "$path" ]; then echo "Caminho simbólico não aceito: $path"; exit 1; fi
done
files=(LICENSE NOTICE AUTHORS.md THIRD_PARTY_NOTICES.md
    userspace-pen/tomate_pen_driver.py userspace-pen/button_schema.py userspace-pen/calibration_protocol.py gui/tomate_control_center.py
    display-driver/aic_protocol.py display-driver/tomate_display_driver.py display-driver/evdi_capture.py display-driver/calibration_display.py
    scripts/check_environment.py scripts/install_evdi.sh scripts/map_cosmic_pen.py
    systemd/tomate-pen.service systemd/tomate-display-monitor.service
    udev/70-tomate-display.rules udev/99-tomate-mtm1118.quirks xorg/71-tomate-evdev.conf)
for file in "${files[@]}"; do test -f "$file" || { echo "Arquivo ausente: $file"; exit 1; }; done
test -d gui/assets || { echo 'Pasta de imagens do painel ausente.'; exit 1; }
stage="$(mktemp -d /tmp/tomate-install.XXXXXXXX)"
trap 'rm -rf -- "$stage"' EXIT
if $deps; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-usb python3-evdev \
        python3-cryptography python3-pil python3-pil.imagetk python3-tk python3-pystray \
        python3-gi gir1.2-gtk-3.0 libusb-1.0-0 usbutils libinput-tools x11-xserver-utils fonts-dejavu-core
fi
python3 -B - <<'PY'
import ast
import sys
from pathlib import Path
import usb, evdev, cryptography, PIL, tkinter, gi
for directory in ('userspace-pen', 'display-driver', 'gui', 'scripts'):
    for path in Path(directory).glob('*.py'):
        ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
sys.path[:0] = ['userspace-pen', 'display-driver']
import calibration_protocol, calibration_display
assert calibration_protocol.PROTOCOL == 3
calibration_protocol.calibration_targets(1280, 800)
PY
if command -v libinput >/dev/null; then
    install -d "$stage/quirks"
    if [ -d /usr/share/libinput ]; then cp -a /usr/share/libinput/. "$stage/quirks/"; fi
    cp udev/99-tomate-mtm1118.quirks "$stage/quirks/"
    libinput quirks validate --data-dir "$stage/quirks"
fi
source scripts/install_evdi.sh
evdi_prepare_for_install "$deps" false
quirk_changed=true
if cmp -s udev/99-tomate-mtm1118.quirks /usr/share/libinput/99-tomate-mtm1118.quirks; then quirk_changed=false; fi
backup="/var/backups/tomate-mtm1118/final-$(date +%Y%m%d-%H%M%S)-$$"
install -d -m 0700 "$backup"
paths=(/opt/tomate-mtm1118 /etc/tomate-mtm1118
    /etc/systemd/system/tomate-pen.service /etc/systemd/system/tomate-display-monitor.service
    /etc/systemd/system/tomate-desktop.service /etc/systemd/system/tomate-wsl-desktop.service
    /etc/systemd/system/tomate-display.service /etc/systemd/user/tomate-display.service
    /etc/default/tomate-desktop /etc/default/tomate-wsl-desktop
    /usr/local/bin/tomate-tablet-settings /usr/local/bin/tomate-map-pen
    /usr/local/bin/tomate-run /usr/local/bin/tomate-keyboard /usr/local/bin/tomate-display-session
    /usr/local/bin/tomate-pen-diagnose /usr/local/bin/tomate-krita-cpu
    /usr/share/applications/tomate-mtm1118.desktop /usr/share/applications/tomate-keyboard.desktop
    /usr/share/applications/tomate-krita-cpu.desktop /usr/share/libinput/99-tomate-mtm1118.quirks
    /etc/xdg/autostart/tomate-display.desktop /etc/xdg/autostart/tomate-pen-map.desktop
    /etc/xdg/autostart/tomate-mtm1118-tray.desktop
    /etc/udev/rules.d/70-tomate-display.rules /etc/udev/rules.d/99-tomate-mtm1118.rules
    /etc/X11/xorg.conf.d/71-tomate-evdev.conf /etc/tmpfiles.d/tomate-display.conf
    /etc/modules-load.d/tomate-mtm1118.conf
    "$desktop_home/.config/autostart/tomate-mtm1118-tray.desktop"
    "$desktop_home/.config/autostart/tomate-display.desktop")
for path in "${paths[@]}"; do
    if [ -e "$path" ] || [ -L "$path" ]; then cp -a --parents "$path" "$backup/"; fi
done
services=(tomate-display-monitor.service tomate-pen.service tomate-desktop.service tomate-wsl-desktop.service tomate-display.service)
previous_active=()
previous_enabled=()
for service in "${services[@]}"; do
    if systemctl is-active --quiet "$service"; then previous_active+=("$service"); fi
    if systemctl is-enabled --quiet "$service"; then previous_enabled+=("$service"); fi
done
user_display_active=false
if [ -d "/run/user/$desktop_uid" ]; then
    if runuser -u "$desktop_user" -- env XDG_RUNTIME_DIR="/run/user/$desktop_uid" systemctl --user is-active --quiet tomate-display.service 2>/dev/null; then user_display_active=true; fi
fi
installation_failed() {
    trap - ERR
    set +e
    echo "A instalação falhou. Restaurando a instalação anterior. Backup: $backup" >&2
    for service in "${services[@]}"; do systemctl disable --now "$service" 2>/dev/null; done
    for path in "${paths[@]}"; do
        if [ -d "$path" ] && [ ! -L "$path" ]; then rm -rf -- "$path"; else rm -f -- "$path"; fi
        if [ -e "$backup$path" ] || [ -L "$backup$path" ]; then
            mkdir -p -- "$(dirname -- "$path")"
            cp -a -- "$backup$path" "$path"
        fi
    done
    systemctl daemon-reload
    udevadm control --reload-rules
    for service in "${previous_enabled[@]}"; do systemctl enable "$service"; done
    for service in "${previous_active[@]}"; do systemctl start "$service"; done
    if $user_display_active; then
        runuser -u "$desktop_user" -- env XDG_RUNTIME_DIR="/run/user/$desktop_uid" systemctl --user daemon-reload
        runuser -u "$desktop_user" -- env XDG_RUNTIME_DIR="/run/user/$desktop_uid" systemctl --user start tomate-display.service
    fi
    exit 1
}
trap installation_failed ERR
for service in "${services[@]}"; do
    if systemctl cat "$service" >/dev/null 2>&1; then systemctl disable --now "$service"; fi
done
if $user_display_active; then runuser -u "$desktop_user" -- env XDG_RUNTIME_DIR="/run/user/$desktop_uid" systemctl --user stop tomate-display.service; fi
getent group tomate-tablet >/dev/null || groupadd --system tomate-tablet
group_changed=false
if ! id -nG "$desktop_user" | tr ' ' '\n' | grep -qx tomate-tablet; then group_changed=true; fi
usermod -a -G tomate-tablet "$desktop_user"
install -d -m 2770 -o root -g tomate-tablet /etc/tomate-mtm1118 /run/tomate-mtm1118
install -d /opt/tomate-mtm1118/{userspace-pen,display-driver,gui,scripts} \
    /etc/systemd/system /etc/xdg/autostart /etc/udev/rules.d /etc/X11/xorg.conf.d \
    /etc/tmpfiles.d /etc/modules-load.d /usr/local/bin /usr/share/applications /usr/share/libinput
for file in "${files[@]}"; do
    case "$file" in userspace-pen/*|display-driver/*|gui/*|scripts/*) install -m 0644 "$file" "/opt/tomate-mtm1118/$file" ;; esac
done
cp -a gui/assets /opt/tomate-mtm1118/gui/
install -m 0644 LICENSE NOTICE AUTHORS.md THIRD_PARTY_NOTICES.md /opt/tomate-mtm1118/
PYTHONPATH=/opt/tomate-mtm1118/userspace-pen python3 -B - <<'PY'
import copy, json, os, tempfile
from pathlib import Path
from tomate_pen_driver import DEFAULT_SETTINGS
path = Path('/etc/tomate-mtm1118/settings.json')
original = path.read_bytes() if path.exists() else None
settings = json.loads(original) if original is not None else copy.deepcopy(DEFAULT_SETTINGS)
settings.setdefault('Digitizer', {}).pop('BarrelMouseEvents', None)
settings.setdefault('General', {}).pop('DisplayMode', None)
updated = json.dumps(settings, indent=2, ensure_ascii=False).encode('utf-8')
fd, name = tempfile.mkstemp(prefix='.settings-', dir=path.parent)
try:
    with os.fdopen(fd, 'wb') as stream:
        stream.write(updated)
        os.fchmod(stream.fileno(), 0o660)
    if path.is_symlink() or (path.read_bytes() if path.exists() else None) != original:
        raise RuntimeError('A configuração mudou durante a instalação; arquivo preservado.')
    os.replace(name, path)
finally:
    Path(name).unlink(missing_ok=True)
PY
chown root:tomate-tablet /etc/tomate-mtm1118/settings.json
printf 'drm\n' > "$stage/display-mode"
install -m 0644 "$stage/display-mode" /etc/tomate-mtm1118/display-mode
rm -f /run/tomate-mtm1118/input-command.json /run/tomate-mtm1118/calibration-command.json /run/tomate-mtm1118/display-state.json
install -m 0644 systemd/tomate-pen.service systemd/tomate-display-monitor.service /etc/systemd/system/
install -m 0644 udev/70-tomate-display.rules /etc/udev/rules.d/
install -m 0644 udev/99-tomate-mtm1118.quirks /usr/share/libinput/
install -m 0644 xorg/71-tomate-evdev.conf /etc/X11/xorg.conf.d/
printf 'd /run/tomate-mtm1118 2770 root tomate-tablet -\nf /run/lock/tomate-display.lock 0666 root root -\n' > /etc/tmpfiles.d/tomate-display.conf
systemd-tmpfiles --create /etc/tmpfiles.d/tomate-display.conf
printf 'uinput\n' > /etc/modules-load.d/tomate-mtm1118.conf
modprobe uinput
cat > /etc/udev/rules.d/99-tomate-mtm1118.rules <<'RULES'
SUBSYSTEM=="usb", ATTR{idVendor}=="0ed1", ATTR{idProduct}=="7821", MODE="0660", TAG+="systemd", ENV{SYSTEMD_WANTS}+="tomate-pen.service"
SUBSYSTEM=="usb", ATTR{idVendor}=="0ed1", ATTR{idProduct}=="7856", MODE="0660", TAG+="systemd", ENV{SYSTEMD_WANTS}+="tomate-pen.service"
RULES
cat > /usr/local/bin/tomate-tablet-settings <<'LAUNCHER'
#!/usr/bin/env bash
if [ "$(id -u)" -eq 0 ]; then echo 'Abra o painel como usuário comum, sem sudo.'; exit 1; fi
exec /usr/bin/python3 /opt/tomate-mtm1118/gui/tomate_control_center.py "$@"
LAUNCHER
cat > /usr/local/bin/tomate-map-pen <<'LAUNCHER'
#!/usr/bin/env bash
exec /usr/bin/python3 /opt/tomate-mtm1118/scripts/map_cosmic_pen.py "$@"
LAUNCHER
chmod 0755 /usr/local/bin/tomate-tablet-settings /usr/local/bin/tomate-map-pen
cat > /usr/share/applications/tomate-mtm1118.desktop <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=Mesa Tomate MTM-1118
Comment=Calibração, pressão e botões da mesa digitalizadora
Exec=/usr/local/bin/tomate-tablet-settings
Icon=input-tablet
Terminal=false
Categories=Settings;HardwareSettings;Graphics;
DESKTOP
cat > /etc/xdg/autostart/tomate-pen-map.desktop <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=Associação da caneta Tomate
Exec=/usr/local/bin/tomate-map-pen --wait 30
OnlyShowIn=COSMIC;
NoDisplay=true
Terminal=false
DESKTOP
# Retire only this project's old components; keep shared packages and drawings.
rm -f /usr/local/bin/tomate-run /usr/local/bin/tomate-keyboard /usr/local/bin/tomate-display-session \
    /usr/local/bin/tomate-pen-diagnose /usr/local/bin/tomate-krita-cpu \
    /usr/share/applications/tomate-keyboard.desktop /usr/share/applications/tomate-krita-cpu.desktop \
    /etc/default/tomate-desktop /etc/default/tomate-wsl-desktop \
    /etc/systemd/system/tomate-desktop.service /etc/systemd/system/tomate-wsl-desktop.service \
    /etc/systemd/system/tomate-display.service /etc/systemd/user/tomate-display.service \
    /etc/xdg/autostart/tomate-display.desktop /etc/xdg/autostart/tomate-mtm1118-tray.desktop \
    "$desktop_home/.config/autostart/tomate-mtm1118-tray.desktop" "$desktop_home/.config/autostart/tomate-display.desktop"
rm -f /opt/tomate-mtm1118/scripts/{desktop_session.py,keyboard_target.py,run_on_tablet.py,usb-autoattach.ps1,usb_status.sh,krita_cpu.py,pen_diagnostics.py,update_pen.sh} \
    /opt/tomate-mtm1118/gui/{desktop_panel.py,keyboard_bridge.py} /opt/tomate-mtm1118/display-driver/portal_capture.py
udevadm control --reload-rules
systemctl daemon-reload
systemctl enable --now tomate-pen.service
echo 'Preparando a segunda tela. Se necessário, abra Configurações → Telas e ative a mesa.'
if ! systemctl enable --now tomate-display-monitor.service; then
    journalctl -u tomate-display-monitor.service -n 50 --no-pager || true
    installation_failed
fi
if [[ "${XDG_CURRENT_DESKTOP:-}" == *COSMIC* ]] || pgrep -u "$desktop_uid" -x cosmic-comp >/dev/null 2>&1; then
    if ! runuser -u "$desktop_user" -- env -u XDG_CONFIG_HOME -u XDG_STATE_HOME HOME="$desktop_home" \
        XDG_CURRENT_DESKTOP=COSMIC /usr/bin/python3 /opt/tomate-mtm1118/scripts/map_cosmic_pen.py --wait 5; then
        echo 'Imagem ativa; execute tomate-map-pen no COSMIC, sem sudo, para associar a caneta.'
    fi
fi
trap - ERR
printf '\nInstalação DRM concluída. Backup: %s\n' "$backup"
echo 'Caneta e botões restaurados ao comportamento da v2.3.7; vídeo otimizado preservado.'
echo 'Encerre o painel antigo com Sair e reabra Mesa Tomate MTM-1118 (v2.3.12). A instalação não inclui Krita.'
if $quirk_changed || $group_changed; then echo 'Salve o trabalho e entre novamente na sessão uma vez para carregar as permissões e o ajuste de pressão.'; fi
