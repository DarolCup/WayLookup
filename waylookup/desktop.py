"""Wayland desktop selection and screen capture dispatch."""

import os


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


def capture_screen(desktop, stop):
    if desktop == "hyprland":
        from .hyprland import capture_output, focused_monitor

        monitor = focused_monitor()
        return capture_output(monitor["name"]), monitor
    if desktop == "plasma":
        from .plasma import capture_screen as capture_plasma

        return capture_plasma()
    if desktop == "gnome":
        from .screencast import capture_screen as capture_gnome

        return capture_gnome(stop)
    raise RuntimeError("Рабочий стол не поддерживается")
