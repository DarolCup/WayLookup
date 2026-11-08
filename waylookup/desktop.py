"""Wayland desktop selection and screen capture dispatch."""

import os
from pathlib import Path
import shlex
import sys


APP_ID = "io.github.waylookup.WayLookup"


def capture_command() -> str:
    launcher = Path.home() / ".local/bin/waylookup"
    if launcher.is_symlink() and launcher.resolve() == Path(sys.argv[0]).resolve():
        fallback = [str(launcher), "capture"]
    else:
        from .hyprland import launch_command

        fallback = shlex.split(launch_command("capture"))
    action = f'gapplication action {APP_ID} capture >/dev/null 2>&1 || exec "$@"'
    return shlex.join(["sh", "-c", action, "waylookup", *fallback])


class CaptureCancelled(Exception):
    """The user or the application cancelled the capture."""


def current_desktop() -> str | None:
    if not os.environ.get("WAYLAND_DISPLAY"):
        return None
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return "hyprland"
    names = os.environ.get("XDG_CURRENT_DESKTOP", "").lower().split(":")
    if "kde" in names:
        return "plasma"
    if "gnome" in names:
        return "gnome"
    return None


def capture_screen(desktop, stop, choose_monitor=False):
    if desktop == "hyprland":
        from .hyprland import capture_output, focused_monitor

        monitor = focused_monitor()
        return capture_output(monitor["name"]), monitor
    if desktop == "plasma":
        from .plasma import capture_screen as capture_plasma

        return capture_plasma()
    if desktop == "gnome":
        from .screencast import capture_screen as capture_gnome

        return capture_gnome(stop, choose_monitor)
    raise RuntimeError("Рабочий стол не поддерживается")
