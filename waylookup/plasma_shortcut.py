"""Register a session shortcut with Plasma's KGlobalAccel service."""

from gi.repository import Gio, GLib

from .config import parse_hotkey
from .desktop import APP_ID

SERVICE = "org.kde.kglobalaccel"
INTERFACE = "org.kde.KGlobalAccel"
# KDE excludes actions with this prefix from persistent shortcut settings.
ACTION = "_k_session:capture"
ACTION_ID = [APP_ID, ACTION, "WayLookup", "Перевести экран"]
SET_PRESENT = 2
NO_AUTOLOADING = 4

# Qt::KeyboardModifier and Qt::Key values used on KGlobalAccel's D-Bus API.
MODIFIERS = {"SHIFT": 0x02000000, "CTRL": 0x04000000, "ALT": 0x08000000, "SUPER": 0x10000000}
SPECIAL_KEYS = {
    "ESC": 0x01000000, "ESCAPE": 0x01000000, "TAB": 0x01000001,
    "BACKSPACE": 0x01000003, "RETURN": 0x01000004, "ENTER": 0x01000004,
    "INSERT": 0x01000006, "DELETE": 0x01000007, "PAUSE": 0x01000008, "PRINT": 0x01000009,
    "HOME": 0x01000010, "END": 0x01000011, "LEFT": 0x01000012, "UP": 0x01000013,
    "RIGHT": 0x01000014, "DOWN": 0x01000015, "PAGE_UP": 0x01000016, "PAGE_DOWN": 0x01000017,
    "SPACE": 0x20,
}


def qt_key(value: str) -> int:
    modifiers, key, _ = parse_hotkey(value)
    if len(key) == 1 and key.isalnum():
        code = ord(key)
    elif key.startswith("F") and key[1:].isdigit() and 1 <= int(key[1:]) <= 35:
        code = 0x0100002F + int(key[1:])
    elif key in SPECIAL_KEYS:
        code = SPECIAL_KEYS[key]
    else:
        raise ValueError(f"Plasma не распознаёт клавишу {key}")
    return code | sum(MODIFIERS[name] for name in modifiers.split())


class Hotkey:
    def __init__(self, capture):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        self.capture = capture
        self.subscription = 0
        self.registered = False

    def call(self, method, signature, *args):
        return self.bus.call_sync(
            SERVICE, "/kglobalaccel", INTERFACE, method, GLib.Variant(signature, args),
            None, Gio.DBusCallFlags.NO_AUTO_START, 3000, None,
        ).unpack()

    def register(self, value):
        key = qt_key(value)
        sequence = ([key, 0, 0, 0],)
        (current,) = self.call("shortcutKeys", "(as)", ACTION_ID)
        if sequence not in current:
            (available,) = self.call("globalShortcutAvailable", "((ai)s)", sequence, APP_ID)
            if not available:
                raise RuntimeError(f"{value} уже используется в Plasma")

        was_registered = self.registered
        self.call("doRegister", "(as)", ACTION_ID)
        self.registered = True
        try:
            if not self.subscription:
                (path,) = self.call("getComponent", "(s)", APP_ID)
                self.subscription = self.bus.signal_subscribe(
                    SERVICE, "org.kde.kglobalaccel.Component", "globalShortcutPressed", path,
                    APP_ID, Gio.DBusSignalFlags.NONE, self.on_pressed,
                )
            (assigned,) = self.call(
                "setShortcutKeys", "(asa(ai)u)", ACTION_ID, [sequence],
                SET_PRESENT | NO_AUTOLOADING,
            )
            if sequence not in assigned:
                raise RuntimeError(f"Plasma не удалось назначить {value}")
        except Exception:
            if not was_registered:
                self.remove()
            raise

    def on_pressed(self, _bus, _sender, _path, _interface, _signal, parameters):
        if parameters.unpack()[:2] == (APP_ID, ACTION):
            self.capture()

    def remove(self):
        if self.registered:
            self.call("unregister", "(ss)", APP_ID, ACTION)
            self.registered = False
        if self.subscription:
            self.bus.signal_unsubscribe(self.subscription)
            self.subscription = 0
