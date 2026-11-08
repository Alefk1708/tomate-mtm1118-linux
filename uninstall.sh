#!/usr/bin/env bash
# Copyright 2026 Alefk1708
# SPDX-License-Identifier: Apache-2.0
# See LICENSE, NOTICE and THIRD_PARTY_NOTICES.md for scope and attribution.
# Remove installed programs and services; preserve configuration and drawings.
set -euo pipefail
if [ "$EUID" -ne 0 ]; then echo 'Execute: sudo bash uninstall.sh'; exit 1; fi
if [ "$#" -ne 0 ]; then echo 'Este desinstalador preserva configurações; não aceita opções.'; exit 2; fi
if [ -L /opt/tomate-mtm1118 ]; then echo 'Recusando remover diretório de programa simbólico.'; exit 1; fi
backup="/var/backups/tomate-mtm1118/uninstall-$(date +%Y%m%d-%H%M%S)"
install -d -m 0700 "$backup"
for path in /etc/tomate-mtm1118 /etc/default/tomate-wsl-desktop /etc/default/tomate-desktop; do
    if [ -e "$path" ]; then cp -a --parents "$path" "$backup/"; fi
done
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
    desktop_uid="$(id -u "$SUDO_USER")"
    runuser -u "$SUDO_USER" -- env XDG_RUNTIME_DIR="/run/user/$desktop_uid" \
        systemctl --user stop tomate-display.service 2>/dev/null || true
fi
for service in tomate-desktop tomate-wsl-desktop tomate-display-monitor tomate-display tomate-pen; do
    if systemctl cat "$service.service" >/dev/null 2>&1; then
        systemctl disable --now "$service.service" || true
    fi
done
for name in tomate-desktop tomate-wsl-desktop tomate-display-monitor tomate-display tomate-pen; do
    rm -f "/etc/systemd/system/$name.service"
done
for name in tomate-desktop tomate-wsl-desktop tomate-display-monitor tomate-display tomate-pen; do
    for directory in /etc/systemd/system/multi-user.target.wants /etc/systemd/system/graphical.target.wants; do
        rm -f "$directory/$name.service"
    done
done
rm -f /etc/systemd/user/tomate-display.service /etc/xdg/autostart/tomate-display.desktop \
    /etc/xdg/autostart/tomate-mtm1118-tray.desktop /usr/share/applications/tomate-mtm1118.desktop \
    /usr/local/bin/tomate-tablet-settings /usr/local/bin/tomate-display-session /usr/local/bin/tomate-run /usr/local/bin/tomate-keyboard \
    /etc/udev/rules.d/70-tomate-display.rules /etc/udev/rules.d/99-tomate-mtm1118.rules \
    /etc/modules-load.d/tomate-mtm1118.conf /etc/tmpfiles.d/tomate-display.conf /etc/tmpfiles.d/tomate-pen.conf
rm -f /etc/X11/xorg.conf.d/71-tomate-evdev.conf
rm -f /usr/share/applications/tomate-keyboard.desktop
rm -f /usr/local/bin/tomate-map-pen /usr/local/bin/tomate-krita-cpu /etc/xdg/autostart/tomate-pen-map.desktop
rm -f /usr/local/bin/tomate-pen-diagnose /usr/share/applications/tomate-krita-cpu.desktop /usr/share/libinput/99-tomate-mtm1118.quirks
rm -f /etc/udev/hwdb.d/99-tomate-mtm1118.hwdb /etc/profile.d/99-tomate-mtm1118-xhost.sh
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
    user_home="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
    rm -f "$user_home/.config/autostart/tomate-mtm1118-tray.desktop"
fi
for old in /etc/systemd/system/tomate-display.service.before-aic-fix; do
    if [ -f "$old" ]; then cp -a "$old" "$backup/"; rm -f "$old"; fi
done
rm -rf -- /opt/tomate-mtm1118
rm -rf -- /run/tomate-mtm1118 /run/tomate-wsl-desktop /run/tomate-desktop
rm -f /run/lock/tomate-display.lock /run/lock/tomate-pen.lock /etc/default/tomate-wsl-desktop /etc/default/tomate-desktop
systemctl daemon-reload
udevadm control --reload-rules
echo "Backup da configuração: $backup"
echo 'Programas e serviços removidos. Configurações em /etc/tomate-mtm1118 e desenhos pessoais preservados.'
echo 'Backups em /var/backups/tomate-mtm1118 preservados; pacotes do sistema/EVDI não foram removidos.'
