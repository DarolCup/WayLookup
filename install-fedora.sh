#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "Usage: bash install-fedora.sh [--desktop plasma|gnome] [--ref BRANCH_OR_TAG] [--deps-only]"
    echo "       bash install-fedora.sh --uninstall [--purge]"
}

fail() {
    printf 'WayLookup: %s\n' "$*" >&2
    exit 1
}

desktop=
ref=main
deps_only=false
uninstall=false
purge=false
while (( $# )); do
    case "$1" in
        --desktop) desktop=${2:?--desktop requires plasma or gnome}; shift 2 ;;
        --ref) ref=${2:?--ref requires a branch or tag}; shift 2 ;;
        --deps-only) deps_only=true; shift ;;
        --uninstall) uninstall=true; shift ;;
        --purge) purge=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; fail "Unknown option: $1" ;;
    esac
done

(( EUID != 0 )) || fail "Run as your normal user. The installer uses sudo for DNF."
source /etc/os-release
[[ ${ID:-} == fedora ]] || fail "This installer requires Fedora."
if "$purge" && ! "$uninstall"; then
    fail "--purge requires --uninstall."
fi

install_root="$HOME/.local/lib/waylookup"
bin_dir="$HOME/.local/bin"
launcher="$bin_dir/waylookup"
config_root="${XDG_CONFIG_HOME:-$HOME/.config}"
data_root="${XDG_DATA_HOME:-$HOME/.local/share}"
unit_dir="$config_root/systemd/user"
applications="$data_root/applications"
desktop_file="$applications/io.github.waylookup.WayLookup.desktop"

app_is_running() {
    [[ $(gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
        --method org.freedesktop.DBus.NameHasOwner io.github.waylookup.WayLookup 2>/dev/null) == "(true,)" ]]
}

stop_running_app() {
    if app_is_running; then
        "$launcher" quit
        for (( attempt = 0; attempt < 30; attempt++ )); do
            if ! app_is_running; then
                return
            fi
            sleep 0.2
        done
        fail "WayLookup is still shutting down. Try again shortly."
    fi
}

check_launcher() {
    if [[ -e $launcher || -L $launcher ]]; then
        [[ -L $launcher && $(readlink -- "$launcher") == "$install_root"/release.*/scripts/waylookup ]] ||
            fail "An existing launcher is in the way: $launcher"
    fi
}

if "$uninstall"; then
    ! "$deps_only" || fail "--deps-only and --uninstall are mutually exclusive."
    check_launcher
    user_manager=false
    if systemctl --user show-environment >/dev/null 2>&1; then
        user_manager=true
        if [[ -f $unit_dir/waylookup.service ]]; then
            systemctl --user daemon-reload
            loaded_unit=$(systemctl --user show --property=FragmentPath --value waylookup.service)
            if [[ -n $loaded_unit && $loaded_unit -ef $unit_dir/waylookup.service ]]; then
                systemctl --user stop waylookup.service
            fi
        fi
    fi
    if [[ -x $launcher ]]; then
        stop_running_app
        if gsettings list-schemas | grep -Fx org.gnome.settings-daemon.plugins.media-keys >/dev/null &&
            "$launcher" --help | grep -w shortcut >/dev/null; then
            "$launcher" shortcut --remove
        fi
    fi
    rm -f -- "$launcher" "$desktop_file" "$unit_dir/waylookup.service" \
        "$unit_dir/graphical-session.target.wants/waylookup.service"
    for installed in "$install_root"/release.*; do
        if [[ ! -L $installed && -f $installed/scripts/waylookup ]]; then
            rm -rf -- "$installed"
        fi
    done
    rmdir -- "$install_root" 2>/dev/null || true
    if "$user_manager"; then
        systemctl --user daemon-reload
    fi
    if "$purge"; then
        rm -rf -- "$config_root/waylookup" "$data_root/waylookup"
    fi
    printf 'WayLookup removed.\n'
    exit 0
fi

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
    gnome) packages+=(gnome-settings-daemon xdg-desktop-portal xdg-desktop-portal-gnome pipewire-gstreamer
                     gstreamer1-plugins-base gstreamer1-plugins-good) ;;
    *) fail "Choose a desktop with --desktop plasma or --desktop gnome." ;;
esac
if "$deps_only"; then
    sudo dnf install -y "${packages[@]}"
    exit 0
fi

check_launcher

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

mkdir -p -- "$unit_dir/graphical-session.target.wants" "$applications"
install -m 644 -- "$source_dir/packaging/waylookup-fedora.service" "$unit_dir/waylookup.service"
ln -sfn -- ../waylookup.service "$unit_dir/graphical-session.target.wants/waylookup.service"

# Exec uses desktop-entry quoting, which escapes backslashes both inside and outside quotes.
desktop_exec=${launcher//\\/\\\\}
desktop_exec=${desktop_exec//\"/\\\"}
desktop_exec=${desktop_exec//\`/\\\`}
desktop_exec=${desktop_exec//\$/\\\$}
desktop_exec=${desktop_exec//%/%%}
desktop_exec=${desktop_exec//\\/\\\\}
while IFS= read -r line; do
    case "$line" in
        "Exec=waylookup daemon") printf 'Exec="%s" settings\n' "$desktop_exec" ;;
        "Exec=waylookup "*) printf 'Exec="%s" %s\n' "$desktop_exec" "${line#Exec=waylookup }" ;;
        *) printf '%s\n' "$line" ;;
    esac
done < "$source_dir/packaging/io.github.waylookup.WayLookup.desktop" > "$work_dir/waylookup.desktop"
install -m 644 -- "$work_dir/waylookup.desktop" "$desktop_file"

link_dir=$(mktemp -d "$bin_dir/.waylookup.XXXXXXXX")
ln -s -- "$release/scripts/waylookup" "$link_dir/waylookup"
# Publish only once the full application is in place. Running instances keep their old files.
mv -Tf -- "$link_dir/waylookup" "$launcher"
release=
"$launcher" config --init >/dev/null

if [[ -n ${WAYLAND_DISPLAY:-} && -n ${XDG_CURRENT_DESKTOP:-} ]]; then
    systemctl --user daemon-reload
    systemctl --user import-environment WAYLAND_DISPLAY XDG_CURRENT_DESKTOP
    systemctl --user stop waylookup.service
    stop_running_app
    systemctl --user start waylookup.service
    if [[ $desktop == gnome ]] && ! "$launcher" shortcut; then
        printf 'Open WayLookup in the application menu to choose a shortcut.\n' >&2
    fi
    printf 'WayLookup started. It will also start at your next Wayland login.\n'
else
    printf 'WayLookup will start when you log into a Wayland session.\n'
fi
printf 'Open WayLookup in the application menu. Diagnostic command: "%s" doctor\n' "$launcher"
