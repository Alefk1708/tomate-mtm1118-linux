#!/usr/bin/env bash
# Copyright 2026 Alefk1708
# SPDX-License-Identifier: Apache-2.0
# See LICENSE, NOTICE and THIRD_PARTY_NOTICES.md for scope and attribution.
# Opened by the portable desktop launcher in a terminal.
set -uo pipefail
project="$(dirname "$(readlink -f "$0")")"
echo 'Instalação da Mesa Tomate MTM-1118 — segunda tela DRM'
echo 'Salve seus documentos antes de atualizar. Será solicitada a senha de administrador.'
if [ "$EUID" -eq 0 ]; then
    bash "$project/install.sh"
    result=$?
elif command -v pkexec >/dev/null 2>&1; then
    pkexec /bin/bash "$project/install.sh" --user "$(id -un)"
    result=$?
elif command -v sudo >/dev/null 2>&1; then
    sudo /bin/bash "$project/install.sh" --user "$(id -un)"
    result=$?
else
    echo 'Abra um terminal nesta pasta e execute: sudo ./install.sh'
    result=1
fi
if [ "$result" -eq 0 ]; then
    echo 'Instalação concluída.'
else
    echo "A instalação não foi concluída (código $result). Confira a mensagem acima."
fi
if [ -t 0 ]; then read -r -p 'Pressione Enter para fechar...' || true; fi
exit "$result"
