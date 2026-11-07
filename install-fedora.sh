#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "Usage: bash install-fedora.sh [--desktop plasma|gnome] [--ref BRANCH_OR_TAG]"
}

fail() {
    printf 'WayLookup: %s\n' "$*" >&2
    exit 1
}

desktop=
ref=main
while (( $# )); do
    case "$1" in
        --desktop) desktop=${2:?--desktop requires plasma or gnome}; shift 2 ;;
        --ref) ref=${2:?--ref requires a branch or tag}; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; fail "Unknown option: $1" ;;
    esac
done

(( EUID != 0 )) || fail "Run as your normal user. The installer uses sudo for DNF."
source /etc/os-release
[[ ${ID:-} == fedora ]] || fail "This installer requires Fedora."
command -v dnf >/dev/null || fail "This installer requires Fedora with DNF."
[[ $ref =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] || fail "Invalid GitHub ref: $ref"

if [[ -z $desktop ]]; then
    desktop_names=${XDG_CURRENT_DESKTOP:-}
    case ":${desktop_names,,}:" in
        *:kde:*) desktop=plasma ;;
        *:gnome:*) desktop=gnome ;;
        ::)
            case ${VARIANT_ID:-} in
                kde) desktop=plasma ;;
                workstation) desktop=gnome ;;
            esac
            ;;
    esac
fi

packages=(python3 python3-gobject python3-cairo gtk3 tesseract tesseract-langpack-eng)
case "$desktop" in
    plasma) packages+=(spectacle libkscreen libappindicator-gtk3) ;;
    gnome) packages+=(xdg-desktop-portal xdg-desktop-portal-gnome pipewire-gstreamer
                     gstreamer1-plugins-base gstreamer1-plugins-good) ;;
    *) fail "Choose a desktop with --desktop plasma or --desktop gnome." ;;
esac

install_root="$HOME/.local/lib/waylookup"
bin_dir="$HOME/.local/bin"
launcher="$bin_dir/waylookup"
if [[ -e $launcher || -L $launcher ]]; then
    [[ -L $launcher && $(readlink -- "$launcher") == "$install_root"/release.*/scripts/waylookup ]] ||
        fail "An existing launcher is in the way: $launcher"
fi

work_dir=$(mktemp -d)
release=
link_dir=
cleanup() {
    rm -rf -- "$work_dir"
    if [[ -n $link_dir ]]; then
        rm -rf -- "$link_dir"
    fi
    if [[ -n $release && $(readlink -- "$launcher" || true) != "$release/scripts/waylookup" ]]; then
        rm -rf -- "$release"
    fi
}
trap cleanup EXIT

source_dir=
if [[ -n ${BASH_SOURCE[0]:-} ]]; then
    source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
fi
if [[ -z $source_dir || ! -f $source_dir/waylookup/desktop.py ]]; then
    command -v curl >/dev/null || fail "Install curl or run this script from a Git checkout."
    curl --fail --silent --show-error --location --retry 3 \
        "https://codeload.github.com/DarolCup/WayLookup/tar.gz/$ref" -o "$work_dir/source.tar.gz"
    mkdir -- "$work_dir/source"
    tar -xzf "$work_dir/source.tar.gz" -C "$work_dir/source" --strip-components=1
    source_dir="$work_dir/source"
fi
[[ -f $source_dir/waylookup/desktop.py && -f $source_dir/scripts/waylookup ]] ||
    fail "The source archive does not contain Fedora support. Check the ref."

sudo dnf install -y "${packages[@]}"

mkdir -p -- "$install_root" "$bin_dir"
release=$(mktemp -d "$install_root/release.XXXXXXXX")
mkdir -- "$release/waylookup" "$release/scripts"
install -m 644 -- "$source_dir"/waylookup/*.py "$source_dir/waylookup/style.css" "$release/waylookup"
install -m 644 -- "$source_dir/LICENSE" "$release/LICENSE"
install -m 755 -- "$source_dir/scripts/waylookup" "$release/scripts/waylookup"
"$release/scripts/waylookup" --version >/dev/null

link_dir=$(mktemp -d "$bin_dir/.waylookup.XXXXXXXX")
ln -s -- "$release/scripts/waylookup" "$link_dir/waylookup"
# Publish only once the full application is in place. Running instances keep their old files.
mv -Tf -- "$link_dir/waylookup" "$launcher"
release=

printf 'WayLookup installed for %s.\nRun: "%s" doctor\n     "%s" settings\n' \
    "$desktop" "$launcher" "$launcher"
