"""A persistent GNOME custom shortcut for WayLookup."""

from pathlib import Path
import shlex

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, Gtk  # noqa: E402

from .config import parse_hotkey  # noqa: E402
from .desktop import capture_command  # noqa: E402

MEDIA_KEYS = "org.gnome.settings-daemon.plugins.media-keys"
CUSTOM = MEDIA_KEYS + ".custom-keybinding"
LIST_KEY = "custom-keybindings"
PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/waylookup/"
SYSTEM_SCHEMAS = (
    MEDIA_KEYS,
    "org.gnome.desktop.wm.keybindings",
    "org.gnome.shell.keybindings",
    "org.gnome.mutter.keybindings",
    "org.gnome.mutter.wayland.keybindings",
)
MODIFIERS = {"SHIFT": "Shift", "CTRL": "Control", "ALT": "Alt", "SUPER": "Super"}


def existing_waylookup_command(command):
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    return len(parts) == 2 and Path(parts[0]).name == "waylookup" and parts[1] == "capture"


class Hotkey:
    def __init__(self):
        self.schemas = Gio.SettingsSchemaSource.get_default()
        self.media = self.settings(MEDIA_KEYS)
        if self.media is None or not self.schemas.lookup(CUSTOM, True):
            raise RuntimeError("Не найдены системные настройки сочетаний GNOME")

    def settings(self, name):
        schema = self.schemas.lookup(name, True) if self.schemas else None
        return Gio.Settings.new_full(schema, None, None) if schema else None

    def register(self, value):
        modifiers, key, _ = parse_hotkey(value)
        accelerator = "".join(f"<{MODIFIERS[mod]}>" for mod in modifiers.split())
        accelerator += key.lower() if len(key) == 1 else key
        binding = Gtk.accelerator_parse(accelerator)
        if not binding.accelerator_key:
            raise ValueError(f"GNOME не распознаёт сочетание {value}")

        paths = self.media.get_strv(LIST_KEY)
        migrate = []
        for path in paths:
            if path == PATH:
                continue
            other = Gio.Settings.new_with_path(CUSTOM, path)
            if Gtk.accelerator_parse(other.get_string("binding")) == binding:
                if not existing_waylookup_command(other.get_string("command")):
                    raise RuntimeError(f"{value} уже используется в GNOME")
                migrate.append(path)
        for schema_name in SYSTEM_SCHEMAS:
            settings = self.settings(schema_name)
            if settings is None:
                continue
            for name in settings.props.settings_schema.list_keys():
                if name == LIST_KEY:
                    continue
                shortcut = settings.get_value(name)
                kind = shortcut.get_type_string()
                bindings = shortcut.unpack() if kind == "as" else []
                if kind == "s":
                    bindings = [shortcut.unpack()]
                if any(Gtk.accelerator_parse(key) == binding for key in bindings):
                    raise RuntimeError(f"{value} уже используется в GNOME")

        shortcut = Gio.Settings.new_with_path(CUSTOM, PATH)
        shortcut.delay()
        if not all((
            shortcut.set_string("name", "WayLookup"),
            shortcut.set_string("command", capture_command()),
            shortcut.set_string("binding", accelerator),
        )):
            shortcut.revert()
            raise RuntimeError("GNOME заблокировал изменение сочетания")
        shortcut.apply()
        updated = [path for path in paths if path not in migrate]
        if PATH not in updated:
            updated.append(PATH)
        if updated != paths and not self.media.set_strv(LIST_KEY, updated):
            raise RuntimeError("GNOME заблокировал изменение списка сочетаний")
        Gio.Settings.sync()

    def remove(self):
        paths = self.media.get_strv(LIST_KEY)
        if PATH in paths:
            if not self.media.set_strv(LIST_KEY, [path for path in paths if path != PATH]):
                raise RuntimeError("GNOME заблокировал изменение списка сочетаний")
            shortcut = Gio.Settings.new_with_path(CUSTOM, PATH)
            for key in ("name", "command", "binding"):
                shortcut.reset(key)
            Gio.Settings.sync()
