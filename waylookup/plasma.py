"""Capture the active Plasma output with KDE's trusted screenshot utility."""

import json
import os
from pathlib import Path
import subprocess
import tempfile

from gi.repository import Gio, GLib


def active_output() -> str:
    bus = Gio.bus_get_sync(Gio.BusType.SESSION)
    (name,) = bus.call_sync(
        "org.kde.KWin", "/KWin", "org.kde.KWin", "activeOutputName", None,
        GLib.VariantType.new("(s)"), Gio.DBusCallFlags.NONE, 3000, None,
    ).unpack()
    return name


def capture_screen():
    name = active_output()
    layout = subprocess.run(
        ["kscreen-doctor", "-j"], capture_output=True, text=True, timeout=5, check=True
    )
    for output in json.loads(layout.stdout)["outputs"]:
        if output["name"] == name and output["enabled"] and output["connected"]:
            break
    else:
        raise RuntimeError(f"Plasma не сообщает геометрию монитора {name}")

    # Spectacle exports to a file. Keep it in a private, short-lived runtime directory.
    with tempfile.TemporaryDirectory(
        prefix="waylookup-", dir=os.environ.get("XDG_RUNTIME_DIR")
    ) as directory:
        path = Path(directory) / "screen.png"
        process = subprocess.run(
            [
                "spectacle", "--new-instance", "--background", "--current",
                "--nonotify", "--output", str(path),
            ],
            capture_output=True, text=True, timeout=10,
        )
        if process.returncode or not path.is_file():
            raise RuntimeError("Spectacle: " + (process.stderr.strip() or "не сохранил снимок"))
        png = path.read_bytes()
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("Spectacle не вернул PNG")
    if active_output() != name:
        raise RuntimeError("Активный монитор изменился во время захвата. Повторите захват.")
    return png, output["pos"]
