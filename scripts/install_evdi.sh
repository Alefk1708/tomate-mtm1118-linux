#!/usr/bin/env bash
# Prepare the module and private library for the native DRM monitor.
set -euo pipefail

evdi_require_loaded_version() {
    local expected="$1" sys_root="${2:-/sys}" loaded='' path
    # Upstream exposes the running driver's version as a device attribute.
    # MODULE_VERSION is optional and is absent in the pinned release.
    for path in "$sys_root/devices/evdi/version" "$sys_root/module/evdi/version"; do
        if [ -r "$path" ]; then loaded="$(cat "$path")"; break; fi
    done
    if [ "$loaded" != "$expected" ]; then
        printf 'EVDI carregado: %s; esperado: %s.\n' "${loaded:-versão não identificada}" "$expected" >&2
        echo 'Não será descarregado durante a sessão. Reinicie sem o serviço DisplayLink antigo antes de repetir.' >&2
        return 1
    fi
}

evdi_check_selected_module() {
    local kernel="$1" modules_root="${2:-/lib/modules}" selected directory name matched=false
    selected="$(modinfo -k "$kernel" -F filename evdi)" || return 1
    selected="$(readlink -f -- "$selected")" || return 1
    directory="$(readlink -f -- "$modules_root/$kernel/updates/dkms")" || return 1
    for name in evdi.ko evdi.ko.zst evdi.ko.xz evdi.ko.gz; do
        if [ "$selected" = "$directory/$name" ] && [ -f "$selected" ]; then matched=true; break; fi
    done
    if ! $matched; then
        printf 'O sistema selecionou %s, fora do diretório DKMS esperado: %s.\n' "$selected" "$directory" >&2
        return 1
    fi
    if [ "$(modinfo -k "$kernel" -F vermagic evdi | cut -d' ' -f1)" != "$kernel" ]; then
        echo 'O módulo encontrado não corresponde ao kernel atual.' >&2
        return 1
    fi
    printf 'Módulo EVDI selecionado: %s\n' "$selected"
}

evdi_prepare_for_install() {
    local automatic_dependencies="$1" force_install="$2"
    if [ "$force_install" = true ] || ! python3 scripts/check_environment.py --drm-candidate >/dev/null 2>&1; then
        if [ "$automatic_dependencies" = true ] || [ "$force_install" = true ]; then
            echo 'Preparando os componentes EVDI necessários: módulo e biblioteca libevdi.'
            # Separate process keeps the component installer's EXIT cleanup
            # independent of install.sh's later service recovery trap.
            if ! bash "${BASH_SOURCE[0]}"; then
                echo 'O preparo do EVDI falhou. Os serviços existentes não foram substituídos.' >&2
                return 1
            fi
        else
            python3 scripts/check_environment.py --drm-candidate || true
            echo 'Dependências DRM ausentes. Repita sem --no-deps para prepará-las automaticamente.' >&2
            return 1
        fi
    fi
    if ! python3 scripts/check_environment.py --drm-candidate; then
        echo 'EVDI ainda incompleto. Os serviços existentes não foram substituídos.' >&2
        return 1
    fi
    if ! modprobe evdi; then
        echo 'O módulo EVDI não pôde ser carregado. Confira os logs do kernel e Secure Boot.' >&2
        echo 'Os serviços existentes não foram substituídos.' >&2
        return 1
    fi
}

main() {
version=1.15.1
checksum=b120df303d14be09062157aab17a8598746c0caf2e076ff3388b4091186cf2d9
kernel="$(uname -r)"
if [ "$EUID" -ne 0 ]; then echo 'Execute pelo install.sh com sudo.'; exit 1; fi
if [[ "$kernel" == *microsoft* ]]; then echo 'EVDI requer Linux nativo, não WSL.'; exit 1; fi
if [ -d /sys/module/evdi ]; then
    evdi_require_loaded_version "$version"
fi
apt-get install -y build-essential dkms pkg-config libdrm-dev "linux-headers-$kernel"
build="/lib/modules/$kernel/build"
if [ ! -f "$build/Makefile" ] || [ ! -f "$build/Module.symvers" ]; then
    echo "Headers incompletos para $kernel; modo atual preservado."; exit 1
fi
cache=/var/cache/tomate-mtm1118
install -d -m 0755 "$cache"
archive="$cache/evdi-$version.tar.gz"
if [ ! -f "$archive" ] || ! printf '%s  %s\n' "$checksum" "$archive" | sha256sum -c - >/dev/null 2>&1; then
    python3 - "$archive" "$version" <<'PY'
import sys, urllib.request
url = f'https://codeload.github.com/DisplayLink/evdi/tar.gz/refs/tags/v{sys.argv[2]}'
with urllib.request.urlopen(url, timeout=60) as response, open(sys.argv[1] + '.part', 'wb') as output:
    import shutil
    shutil.copyfileobj(response, output)
import os
os.replace(sys.argv[1] + '.part', sys.argv[1])
PY
fi
printf '%s  %s\n' "$checksum" "$archive" | sha256sum -c -
work="$(mktemp -d /tmp/tomate-evdi.XXXXXXXX)"
trap 'rm -rf -- "$work"' EXIT
python3 - "$archive" "$work" <<'PY'
import sys, tarfile
with tarfile.open(sys.argv[1]) as archive:
    archive.extractall(sys.argv[2], filter='data')
PY
source="$work/evdi-$version"
make -C "$source/library" -j2
dkms_source="/usr/src/evdi-$version"
if [ -L "$dkms_source" ]; then echo 'Diretório DKMS simbólico recusado.'; exit 1; fi
if [ -d "$dkms_source" ]; then
    if ! diff -qr "$source/module" "$dkms_source" >/dev/null; then
        echo "$dkms_source já existe com outra origem; não será sobrescrito."; exit 1
    fi
else
    install -d "$dkms_source"
    cp -a "$source/module/." "$dkms_source/"
fi
if ! dkms status -m evdi -v "$version" | grep -q '^evdi'; then
    dkms add -m evdi -v "$version"
fi
if ! dkms status -m evdi -v "$version" -k "$kernel" | grep -q ': installed'; then
    dkms build -m evdi -v "$version" -k "$kernel"
    dkms install -m evdi -v "$version" -k "$kernel"
fi
depmod -a "$kernel"
evdi_check_selected_module "$kernel"
if ! modprobe evdi; then
    echo 'O EVDI compilou, mas o kernel recusou o carregamento.'
    echo 'Confira journalctl -k, dkms status e o cadastro da chave MOK se Secure Boot estiver ativo.'
    echo 'O instalador não desativa Secure Boot e ainda não parou o desktop da mesa.'
    exit 1
fi
evdi_require_loaded_version "$version"
make -C "$source/library" install LIBDIR=/opt/tomate-mtm1118/evdi-lib
install -m 0644 "$source/library/LICENSE" /opt/tomate-mtm1118/evdi-lib/LICENSE
printf 'EVDI %s preparado para %s. Biblioteca privada instalada.\n' "$version" "$kernel"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
