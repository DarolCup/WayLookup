"""Hyprland capture and owned dynamic shortcuts (Lua and legacy Hyprlang)."""

import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import threading

from .config import parse_hotkey


def hypr(*args: str) -> str:
    proc = subprocess.run(["hyprctl", *args], capture_output=True, text=True, timeout=5)
    output = proc.stdout.strip()
    if proc.returncode or output.lower().startswith(("error", "unknown", "invalid")):
        raise RuntimeError(proc.stderr.strip() or output or "Ошибка hyprctl")
    return output


def focused_monitor() -> dict:
    monitors = json.loads(hypr("-j", "monitors"))
    if not monitors:
        raise RuntimeError("Hyprland не сообщает о доступных мониторах")
    return next((m for m in monitors if m.get("focused")), monitors[0])


def capture_output(name: str) -> bytes:
    proc = subprocess.run(["grim", "-o", name, "-t", "png", "-"], capture_output=True, timeout=10)
    if proc.returncode:
        raise RuntimeError("grim: " + proc.stderr.decode("utf-8", "replace").strip())
    if not proc.stdout.startswith(b"\x89PNG"):
        raise RuntimeError("grim не вернул PNG")
    return proc.stdout


def launch_command(action: str) -> str:
    # An absolute package path makes source checkouts work regardless of Hyprland's cwd.
    root = str(Path(__file__).resolve().parent.parent)
    return shlex.join(["env", f"PYTHONPATH={root}", sys.executable, "-m", "waylookup", action])


class Hotkey:
    def __init__(self):
        self.current: str | None = None
        version = hypr("version")
        import re

        match = re.search(r"Hyprland\s+(?:v)?(\d+)\.(\d+)", version)
        self.lua = bool(match and (int(match[1]), int(match[2])) >= (0, 55))

    def register(self, value: str) -> None:
        mods, key, mask = parse_hotkey(value)
        if value == self.current and self.is_registered(value):
            return
        binds = json.loads(hypr("-j", "binds"))
        for bind in binds:
            if bind.get("modmask") == mask and str(bind.get("key", "")).upper() == key:
                if bind.get("description") == "WayLookup: capture":
                    continue
                raise RuntimeError(
                    f"{value} уже занят в Hyprland. Выберите другой бинд в настройках."
                )
        previous = self.current
        self.remove()
        try:
            command = launch_command("capture")
            if self.lua:
                code = (
                    "if _G.__waylookup_bind then _G.__waylookup_bind:remove() end; "
                    "_G.__waylookup_bind = hl.bind("
                    + json.dumps(value)
                    + ", hl.dsp.exec_cmd("
                    + json.dumps(command, ensure_ascii=False)
                    + '), {description="WayLookup: capture"})'
                )
                hypr("eval", code)
            else:
                if any(
                    b.get("description") == "WayLookup: capture"
                    and b.get("modmask") == mask
                    and str(b.get("key", "")).upper() == key
                    for b in binds
                ):
                    hypr("keyword", "unbind", f"{mods}, {key}")
                hypr("keyword", "bindd", f"{mods}, {key}, WayLookup: capture, exec, {command}")
            self.current = value
            if not self.is_registered(value):
                raise RuntimeError("Hyprland не зарегистрировал бинд")
        except Exception:
            self.remove()
            if previous and previous != value:
                self.register(previous)
            raise

    def is_registered(self, value: str) -> bool:
        _, key, mask = parse_hotkey(value)
        return any(
            b.get("description") == "WayLookup: capture"
            and b.get("modmask") == mask
            and str(b.get("key", "")).upper() == key
            for b in json.loads(hypr("-j", "binds"))
        )

    def remove(self) -> None:
        if self.current:
            if self.lua:
                hypr(
                    "eval",
                    "if _G.__waylookup_bind then _G.__waylookup_bind:remove(); _G.__waylookup_bind = nil end",
                )
            elif self.is_registered(self.current):
                mods, key, _ = parse_hotkey(self.current)
                # Refuse to unbind if a user added another action to the same combination.
                binds = json.loads(hypr("-j", "binds"))
                if all(
                    b.get("description") == "WayLookup: capture"
                    for b in binds
                    if b.get("modmask") == parse_hotkey(self.current)[2]
                    and str(b.get("key", "")).upper() == key
                ):
                    hypr("keyword", "unbind", f"{mods}, {key}")
            self.current = None


def watch_reload(callback, stop: threading.Event) -> None:
    signature, runtime = (
        os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"),
        os.environ.get("XDG_RUNTIME_DIR"),
    )
    if not signature or not runtime:
        return
    path = str(Path(runtime) / "hypr" / signature / ".socket2.sock")
    while not stop.is_set():
        try:
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(1)
                connection.connect(path)
                pending = b""
                while not stop.is_set():
                    try:
                        chunk = connection.recv(65536)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    pending += chunk
                    while b"\n" in pending:
                        event, pending = pending.split(b"\n", 1)
                        if event.startswith(b"configreloaded>>"):
                            callback()
        except OSError:
            pass
        stop.wait(2)
