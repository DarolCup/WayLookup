"""GTK3 UI; all GTK calls stay on the main thread."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import logging
from math import ceil, floor
from pathlib import Path
import signal
import threading

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk  # noqa: E402

from .card import TranslationCard  # noqa: E402
from .config import Config  # noqa: E402
from .desktop import APP_ID, CaptureCancelled, capture_screen, current_desktop  # noqa: E402
from .dictionary import Dictionary  # noqa: E402
from .history import save_lookup  # noqa: E402
from .hyprland import Hotkey as HyprlandHotkey, watch_reload  # noqa: E402
from .ocr import png_size, recognize, sentence_for  # noqa: E402
from .translation import LookupResult, Translator  # noqa: E402

LOG = logging.getLogger(__name__)


def label(text="", css=None, wrap=True):
    widget = Gtk.Label(label=text, xalign=0)
    widget.set_line_wrap(wrap)
    widget.set_max_width_chars(85)
    if css:
        widget.get_style_context().add_class(css)
    return widget


def prepare_transparent_window(window):
    window.set_decorated(False)
    window.set_app_paintable(True)
    visual = window.get_screen().get_rgba_visual()
    if visual:
        window.set_visual(visual)
    return visual is not None


def layer_window(shell, window, namespace, monitor=None, fullscreen=False):
    shell.init_for_window(window)
    shell.set_namespace(window, namespace)
    shell.set_layer(window, shell.Layer.OVERLAY)
    shell.set_exclusive_zone(window, -1)  # Cover panels; never reserve desktop space.
    shell.set_keyboard_mode(window, shell.KeyboardMode.EXCLUSIVE)
    if monitor:
        shell.set_monitor(window, monitor)
    if fullscreen:
        for edge in (
            shell.Edge.TOP,
            shell.Edge.BOTTOM,
            shell.Edge.LEFT,
            shell.Edge.RIGHT,
        ):
            shell.set_anchor(window, edge, True)
    return prepare_transparent_window(window)


def find_monitor(display, position):
    count = display.get_n_monitors()
    if count == 1:
        return 0, display.get_monitor(0)
    if position is not None:
        for index in range(count):
            monitor = display.get_monitor(index)
            geometry = monitor.get_geometry()
            if (geometry.x, geometry.y) == (position["x"], position["y"]):
                return index, monitor
    raise RuntimeError("Не удалось определить монитор для оверлея. Повторите захват.")


class Overlay(Gtk.ApplicationWindow):
    def __init__(self, app, png: bytes, monitor: dict | None, words):
        display = Gdk.Display.get_default()
        if app.desktop == "hyprland":
            gdk_monitor = display.get_monitor_at_point(monitor["x"] + 1, monitor["y"] + 1)
        else:
            index, gdk_monitor = find_monitor(display, monitor)
        super().__init__(application=app, title="WayLookup")
        self.app = app
        self.words = []
        self.selected = None
        self.hovered = None
        self.scale = 1.0
        self.offset = (0.0, 0.0)
        self.input_update = 0
        self.png = png
        self.pixbuf = None
        size = png_size(png)
        if size is None:
            raise ValueError("Не удалось определить размер снимка экрана")
        self.image_width, self.image_height = size
        self.windowed_live = app.desktop == "gnome" and display.get_n_monitors() == 1
        self.monitor_geometry = gdk_monitor.get_geometry()
        if app.layer_shell:
            self.supports_transparency = layer_window(
                app.layer_shell, self, "waylookup", gdk_monitor, fullscreen=True
            )
        else:
            self.monitor_index = index
            self.supports_transparency = prepare_transparent_window(self) and self.windowed_live
            if self.supports_transparency:
                # Mutter paints a black background behind transparent fullscreen surfaces.
                self.maximize()
            else:
                self.fullscreen_on_monitor(self.get_screen(), index)
        self.live_background = self.supports_transparency
        self.connect("key-press-event", self.on_key)
        self.connect("destroy", lambda *_: self.app.overlay_closed(self))
        overlay = Gtk.Overlay()
        self.add(overlay)
        self.canvas = Gtk.DrawingArea()
        self.canvas.set_hexpand(True)
        self.canvas.set_vexpand(True)
        self.canvas.set_app_paintable(True)
        self.canvas.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.POINTER_MOTION_MASK)
        self.canvas.connect("draw", self.draw)
        self.canvas.connect("button-press-event", self.click)
        self.canvas.connect("motion-notify-event", self.motion)
        self.canvas.connect("size-allocate", self.schedule_input_update)
        overlay.add(self.canvas)

        self.card = TranslationCard(
            app.config, gdk_monitor.get_geometry(), self.destroy, app.lookup, app.save_result
        )
        self.card.set_no_show_all(True)
        overlay.add_overlay(self.card)

        self.bar = bar = Gtk.Box(spacing=14)
        bar.get_style_context().add_class("capture-bar")
        bar.set_halign(Gtk.Align.CENTER)
        bar.set_valign(Gtk.Align.END)
        bar.set_margin_bottom(30)
        self.status = label("", "status")
        bar.pack_start(self.status, True, True, 0)
        exit_button = Gtk.Button(label="Esc · Закрыть")
        exit_button.connect("clicked", lambda *_: self.destroy())
        bar.pack_start(exit_button, False, False, 0)
        overlay.add_overlay(bar)
        bar.show_all()
        bar.set_no_show_all(True)
        self.connect("map", self.schedule_input_update)
        for widget in (self.card, self.bar):
            for event in ("show", "hide", "size-allocate"):
                widget.connect(event, self.schedule_input_update)
        self.set_words(words)
        self.show_all()
        if not app.layer_shell:
            self.present()
        self.card.hide()

    def set_words(self, words):
        self.words = words
        self.status.set_text(
            f"{len(words)} слов · Нажмите на слово для перевода"
            if words
            else "Текст не найден · Попробуйте другой язык OCR или PSM в настройках"
        )
        self.bar.set_visible(not self.live_background or not words)
        self.schedule_input_update()
        self.canvas.queue_draw()

    def on_key(self, _widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.destroy()
            return True
        if event.keyval == Gdk.KEY_F2 and self.supports_transparency:
            self.live_background = not self.live_background
            if self.windowed_live:
                if self.live_background:
                    self.unfullscreen()
                    self.maximize()
                else:
                    self.fullscreen_on_monitor(self.get_screen(), self.monitor_index)
            self.bar.set_visible(not self.live_background or not self.words)
            self.schedule_input_update()
            self.canvas.queue_draw()
            return True
        return False

    def update_transform(self):
        width = self.canvas.get_allocated_width()
        height = self.canvas.get_allocated_height()
        top_inset = 0
        if self.windowed_live and self.live_background:
            # The standard GNOME top bar is outside the maximized window.
            top_inset = max(0, self.monitor_geometry.height - height)
            width = self.monitor_geometry.width
            height = self.monitor_geometry.height
        self.scale = min(width / self.image_width, height / self.image_height)
        ox = (width - self.image_width * self.scale) / 2
        oy = (height - self.image_height * self.scale) / 2 - top_inset
        self.offset = (ox, oy)

    def schedule_input_update(self, *_):
        if not self.input_update:
            self.input_update = GLib.idle_add(self.update_input_region)

    def update_input_region(self):
        self.input_update = 0
        window = self.get_window()
        if not window:
            return False
        # GDK sends a changed Wayland input region with the next surface commit.
        self.canvas.queue_draw()
        # A click through an ordinary GNOME window can raise the game above it.
        if not self.live_background or self.windowed_live:
            region = cairo.Region(cairo.RectangleInt(
                0, 0, self.get_allocated_width(), self.get_allocated_height()
            ))
            window.input_shape_combine_region(region, 0, 0)
            return False

        self.update_transform()
        region = cairo.Region()
        position = self.canvas.translate_coordinates(self, 0, 0)
        if position is not None:
            x, y = position
            ox, oy = self.offset
            for word in self.words:
                left = floor(x + ox + word.x * self.scale - 3)
                top = floor(y + oy + word.y * self.scale - 3)
                right = ceil(x + ox + (word.x + word.width) * self.scale + 3)
                bottom = ceil(y + oy + (word.y + word.height) * self.scale + 3)
                region.union(cairo.RectangleInt(left, top, right - left, bottom - top))
        for widget in (self.card, self.bar):
            if widget.get_visible():
                position = widget.translate_coordinates(self, 0, 0)
                if position is not None:
                    x, y = position
                    region.union(cairo.RectangleInt(
                        x, y, widget.get_allocated_width(), widget.get_allocated_height()
                    ))
        window.input_shape_combine_region(region, 0, 0)
        return False

    def draw(self, _widget, cr):
        self.update_transform()
        ox, oy = self.offset
        if self.live_background:
            cr.save()
            cr.set_operator(cairo.OPERATOR_CLEAR)
            cr.paint()
            cr.restore()
        else:
            cr.set_source_rgb(0.03, 0.04, 0.06)
            cr.paint()
        cr.save()
        cr.translate(ox, oy)
        cr.scale(self.scale, self.scale)
        if not self.live_background:
            if self.pixbuf is None:
                loader = GdkPixbuf.PixbufLoader.new_with_type("png")
                loader.write(self.png)
                loader.close()
                self.pixbuf = loader.get_pixbuf()
            Gdk.cairo_set_source_pixbuf(cr, self.pixbuf, 0, 0)
            cr.paint()
        for word in self.words:
            selected = word is self.selected
            hover = word is self.hovered
            color = (1, 0.77, 0.3) if selected else (0.31, 0.87, 0.83)
            cr.rectangle(word.x - 2, word.y - 2, word.width + 4, word.height + 4)
            cr.set_source_rgba(*color, 0.25 if selected or hover else 0.06)
            cr.fill_preserve()
            cr.set_source_rgba(*color, 0.95 if selected or hover else 0.45)
            cr.set_line_width((2 if selected or hover else 1) / self.scale)
            cr.stroke()
        cr.restore()
        return False

    def word_at(self, x, y):
        x = (x - self.offset[0]) / self.scale
        y = (y - self.offset[1]) / self.scale
        matches = [w for w in self.words if w.contains(x, y, 3 / self.scale)]
        return min(matches, key=lambda w: w.width * w.height) if matches else None

    def motion(self, widget, event):
        word = self.word_at(event.x, event.y)
        if word is not self.hovered:
            self.hovered = word
            widget.get_window().set_cursor(
                Gdk.Cursor.new_from_name(widget.get_display(), "pointer" if word else "default")
            )
            widget.queue_draw()
        return False

    def click(self, _widget, event):
        if event.button != 1:
            return False
        word = self.word_at(event.x, event.y)
        if word:
            self.selected = word
            self.canvas.queue_draw()
            self.app.lookup(word.lookup, sentence_for(self.words, word))
        return True

    def loading(self, word, sentence):
        self.last_result = None
        self.card.loading(word, sentence)
        self.card.show()

    def show_result(self, result):
        self.last_result = result
        self.card.render(result)


class Settings(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="WayLookup — Настройки")
        self.app = app
        self.get_style_context().add_class("settings-window")
        if app.desktop == "hyprland":
            layer_window(app.layer_shell, self, "waylookup-settings")
        self.set_default_size(650, 720)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, margin=24)
        self.add(root)
        root.pack_start(label("WayLookup", "word"), False, False, 0)
        hint = (
            "Настройте перевод один раз — дальше достаточно бинда."
            if app.desktop != "plasma"
            else "Назначьте команду waylookup capture в системных настройках клавиатуры."
        )
        root.pack_start(label(hint, "muted"), False, False, 0)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        root.pack_start(scroller, True, True, 0)
        grid = Gtk.Grid(column_spacing=18, row_spacing=12)
        scroller.add(grid)
        self.inputs = {}
        row = 0
        specs = []
        if app.desktop != "plasma":
            specs.extend([
                ("hotkey", "Бинд", None),
                ("register_hotkey", "Регистрировать бинд", "bool"),
            ])
        providers = [
            ("mymemory", "MyMemory · онлайн, без ключа"),
            ("google", "Google Cloud · ключ API"),
            ("libretranslate", "LibreTranslate"),
            ("ollama", "Ollama · ИИ"),
        ]
        specs += [
            ("source", "Исходный язык", None),
            ("target", "Язык перевода", None),
            ("ocr_languages", "Языки OCR (eng+rus)", None),
            (
                "ocr_psm",
                "Разметка OCR",
                [
                    (3, "3 · Автоматически"),
                    (6, "6 · Один блок"),
                    (11, "11 · Игры и разрозненный текст"),
                    (12, "12 · Текст с ориентацией"),
                ],
            ),
            ("min_confidence", "Порог уверенности OCR", None),
            (
                "word_mode",
                "Перевод слова",
                [
                    ("dictionary", "Офлайн-словарь"),
                    ("translator", "Переводчик"),
                    ("ai", "ИИ · Ollama"),
                ],
            ),
            (
                "word_provider",
                "Источник машинного перевода слова",
                [("same", "Как для предложения"), *providers],
            ),
            ("dictionary_fallback", "Переводчик, если нет в словаре", "bool"),
            (
                "sentence_provider",
                "Переводчик предложения",
                [*providers, ("off", "Отключить")],
            ),
            ("libretranslate_url", "Адрес LibreTranslate", None),
            ("ollama_url", "Адрес Ollama", None),
            ("ollama_model", "Модель Ollama", None),
            ("network_timeout", "Таймаут сервиса, сек.", None),
            ("history_file", "Файл слов (пусто = words.txt)", None),
        ]
        for key, title, options in specs:
            grid.attach(label(title), 0, row, 1, 1)
            value = getattr(app.config, key)
            if options == "bool":
                widget = Gtk.Switch(active=value, halign=Gtk.Align.START)
            elif options:
                widget = Gtk.ComboBoxText()
                for identifier, text in options:
                    widget.append(str(identifier), text)
                widget.set_active_id(str(value))
            else:
                widget = Gtk.Entry(text=str(value))
                widget.set_hexpand(True)
            grid.attach(widget, 1, row, 1, 1)
            self.inputs[key] = widget
            row += 1
        books = app.dictionary.books()
        text = "Словари: " + (
            ", ".join(f"{book} ({count})" for book, _s, _t, count in books)
            if books
            else "не импортированы"
        )
        root.pack_start(label(text, "muted"), False, False, 0)
        root.pack_start(
            label(
                "MyMemory отправляет выбранное слово и предложение в интернет.\nДля полностью локальной работы: словарь + Ollama либо отключённый перевод предложений.",
                "muted",
            ),
            False,
            False,
            0,
        )
        self.error = label("", "warning")
        root.pack_start(self.error, False, False, 0)
        buttons = Gtk.Box(spacing=12)
        root.pack_start(buttons, False, False, 0)
        import_button = Gtk.Button(label="Импорт словаря…")
        import_button.connect("clicked", self.import_dictionary)
        buttons.pack_start(import_button, False, False, 0)
        if app.desktop == "gnome":
            monitor_button = Gtk.Button(label="Выбрать монитор…")
            monitor_button.connect("clicked", lambda *_: app.capture(choose_monitor=True))
            buttons.pack_start(monitor_button, False, False, 0)
        cancel = Gtk.Button(label="Отмена")
        cancel.connect("clicked", lambda *_: self.destroy())
        buttons.pack_end(cancel, False, False, 0)
        save = Gtk.Button(label="Сохранить")
        save.get_style_context().add_class("suggested-action")
        save.connect("clicked", self.save)
        buttons.pack_end(save, False, False, 0)
        self.connect(
            "key-press-event",
            lambda _w, event: (self.destroy() or True) if event.keyval == Gdk.KEY_Escape else False,
        )
        self.show_all()
        if app.desktop != "hyprland":
            self.present()

    def save(self, *_):
        try:
            values = {}
            for key, widget in self.inputs.items():
                if isinstance(widget, Gtk.Switch):
                    value = widget.get_active()
                elif isinstance(widget, Gtk.ComboBoxText):
                    value = widget.get_active_id()
                else:
                    value = widget.get_text().strip()
                if key in ("ocr_psm", "network_timeout"):
                    value = int(value)
                elif key == "min_confidence":
                    value = float(value)
                values[key] = value
            config = replace(self.app.config, **values).validate()
            self.app.apply_config(config)
            self.destroy()
        except (ValueError, OSError, RuntimeError) as error:
            self.error.set_text(str(error))

    def import_dictionary(self, *_):
        chooser = Gtk.FileChooserNative(
            title="Выберите .ifo StarDict или .json",
            modal=True,
            action=Gtk.FileChooserAction.OPEN,
            accept_label="Импорт",
            cancel_label="Отмена",
        )
        file_filter = Gtk.FileFilter()
        file_filter.set_name("Словари (.ifo, .json)")
        file_filter.add_pattern("*.ifo")
        file_filter.add_pattern("*.json")
        chooser.add_filter(file_filter)
        chooser.connect("response", self.import_selected)
        self.chooser = chooser
        if self.app.desktop == "hyprland":
            # Native/portal dialogs are regular surfaces; hide our covering layer while choosing.
            self.hide()
            self.app.layer_shell.set_keyboard_mode(self, self.app.layer_shell.KeyboardMode.NONE)
        else:
            chooser.set_transient_for(self)
        chooser.show()

    def import_selected(self, chooser, response):
        self.chooser = None
        if self.app.desktop == "hyprland":
            self.show_all()
            self.app.layer_shell.set_keyboard_mode(
                self, self.app.layer_shell.KeyboardMode.EXCLUSIVE
            )
        path = chooser.get_filename()
        chooser.destroy()
        if response != Gtk.ResponseType.ACCEPT or not path:
            return
        self.error.set_text("Импортируем словарь…")
        source = self.inputs["source"].get_text().strip()
        target = self.inputs["target"].get_text().strip()

        def run_import():
            try:
                method = (
                    self.app.dictionary.import_stardict
                    if path.endswith(".ifo")
                    else self.app.dictionary.import_json
                )
                count = method(Path(path), source, target)
                message = f"Импортировано {count} слов ({source} → {target})"
            except Exception as error:
                message = f"Импорт не удался: {error}"
            GLib.idle_add(self.import_finished, message)

        self.app.executor.submit(run_import)

    def import_finished(self, message):
        if not self.in_destruction() and self.get_visible():
            self.error.set_text(message)
        return False


class Application(Gtk.Application):
    def __init__(self):
        self.desktop = current_desktop()
        self.layer_shell = None
        if self.desktop in ("hyprland", "plasma"):
            try:
                gi.require_version("GtkLayerShell", "0.1")
                from gi.repository import GtkLayerShell
            except (ImportError, ValueError):
                if self.desktop == "hyprland":
                    raise
            else:
                if GtkLayerShell.is_supported():
                    self.layer_shell = GtkLayerShell
            if self.desktop == "hyprland" and not self.layer_shell:
                raise RuntimeError("Композитор не поддерживает layer-shell")
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        capture_action = Gio.SimpleAction.new("capture", None)
        capture_action.connect("activate", lambda *_: self.capture())
        self.add_action(capture_action)
        self.config = Config.load()
        self.overlay = None
        self.settings = None
        self.hidden_settings = None
        self.hotkey = None
        self.capturing = False
        self.generation = 0
        self.lookup_id = 0
        self.future = None
        self.stop = threading.Event()
        self.executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="waylookup-translation"
        )
        self.save_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="waylookup-save")
        self.capture_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="waylookup-capture"
        )

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.hold()
        provider = Gtk.CssProvider()
        provider.load_from_path(str(Path(__file__).with_name("style.css")))
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
        self.dictionary = Dictionary()
        self.translator = Translator(self.dictionary)
        self.setup_tray()
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self.quit_signal)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self.quit_signal)
        if self.desktop != "plasma":
            GLib.idle_add(self.setup_hotkey)
        if self.desktop == "hyprland":
            threading.Thread(
                target=watch_reload,
                args=(lambda: GLib.idle_add(self.setup_hotkey), self.stop),
                daemon=True,
            ).start()

    def do_command_line(self, command_line):
        args = command_line.get_arguments()
        command = args[1] if len(args) > 1 else "daemon"
        if command == "capture":
            self.capture()
        elif command == "choose-monitor":
            self.capture(choose_monitor=True)
        elif command == "settings":
            self.open_settings()
        elif command == "quit":
            self.quit()
        elif command != "daemon":
            return 2
        return 0

    def setup_tray(self):
        self.tray_menu = Gtk.Menu()
        for title, callback in [
            ("Перевести экран", self.capture),
            ("Настройки…", self.open_settings),
            ("Открыть words.txt", self.open_history),
            ("Выход", self.quit),
        ]:
            item = Gtk.MenuItem(label=title)
            item.connect("activate", lambda _item, fn=callback: fn())
            self.tray_menu.append(item)
        self.tray_menu.show_all()
        for namespace in ("AyatanaAppIndicator3", "AppIndicator3"):
            try:
                gi.require_version(namespace, "0.1")
                import importlib

                indicator = importlib.import_module("gi.repository." + namespace)
                self.tray = indicator.Indicator.new(
                    "waylookup",
                    "accessories-dictionary",
                    indicator.IndicatorCategory.APPLICATION_STATUS,
                )
                self.tray.set_status(indicator.IndicatorStatus.ACTIVE)
                self.tray.set_title("WayLookup")
                self.tray.set_menu(self.tray_menu)
                return
            except (ValueError, ImportError):
                continue
        if self.desktop != "gnome":
            self.notify(
                "Tray недоступен",
                "Установите AppIndicator и включите tray в панели. Доступны waylookup capture/settings/quit.",
            )

    def notify(self, title, body):
        notification = Gio.Notification.new(title)
        notification.set_body(body)
        notification.set_icon(Gio.ThemedIcon.new("accessories-dictionary"))
        self.send_notification(None, notification)
        LOG.warning("%s: %s", title, body)

    def get_hotkey(self):
        if self.hotkey is None:
            if self.desktop == "gnome":
                from .gnome_shortcut import Hotkey

                self.hotkey = Hotkey()
            else:
                self.hotkey = HyprlandHotkey()
        return self.hotkey

    def setup_hotkey(self):
        try:
            if self.config.register_hotkey:
                self.get_hotkey().register(self.config.hotkey)
            elif self.desktop == "gnome":
                self.get_hotkey().remove()
        except Exception as error:
            self.notify("Бинд WayLookup", str(error))
        return False

    def apply_config(self, config):
        if self.desktop == "plasma":
            config.save()
            self.config = config
            return
        old = self.config
        try:
            if config.register_hotkey:
                self.get_hotkey().register(config.hotkey)
            elif self.hotkey or self.desktop == "gnome":
                self.get_hotkey().remove()
            config.save()
        except Exception:
            if self.hotkey:
                if old.register_hotkey:
                    self.hotkey.register(old.hotkey)
                else:
                    self.hotkey.remove()
            raise
        self.config = config

    def capture(self, choose_monitor=False):
        if self.overlay:
            self.overlay.destroy()
            return
        if self.capturing:
            return
        if self.settings:
            if self.desktop != "gnome" or getattr(self.settings, "chooser", None):
                return
            self.hidden_settings = self.settings
            self.settings.hide()
        self.capturing = True
        self.generation += 1
        generation = self.generation

        if self.hidden_settings:
            GLib.timeout_add(200, self.begin_capture, generation, choose_monitor)
        else:
            self.begin_capture(generation, choose_monitor)

    def begin_capture(self, generation, choose_monitor):
        if self.stop.is_set():
            return False

        config = self.config

        def run_capture():
            shown = False

            def show_primary(words):
                nonlocal shown
                if not self.stop.is_set():
                    shown = True
                    GLib.idle_add(self.show_capture, generation, png, monitor, words)

            try:
                png, monitor = capture_screen(self.desktop, self.stop, choose_monitor)
                if not self.stop.is_set():
                    words = recognize(
                        png, config, on_primary=show_primary,
                        known_words=lambda terms: self.dictionary.existing_words(
                            terms, config.source, config.target
                        ),
                    )
                    if shown:
                        GLib.idle_add(self.refine_capture, generation, words)
                    else:
                        show_primary(words)
            except CaptureCancelled:
                GLib.idle_add(self.capture_failed, generation, None)
            except Exception as error:
                GLib.idle_add(self.capture_failed, generation, str(error))

        self.capture_executor.submit(run_capture)
        return False

    def refine_capture(self, generation, words):
        if generation == self.generation and self.overlay and not self.stop.is_set():
            if words != self.overlay.words:
                self.overlay.set_words(words)
        return False

    def show_capture(self, generation, png, monitor, words):
        if generation != self.generation or self.stop.is_set():
            return False
        self.capturing = False
        if self.settings and self.settings is not self.hidden_settings:
            return False
        try:
            self.overlay = Overlay(self, png, monitor, words)
        except Exception as error:
            self.notify("Ошибка оверлея", str(error))
            self.restore_settings()
            return False
        return False

    def capture_failed(self, generation, error):
        if generation == self.generation:
            self.capturing = False
            if error:
                self.notify("Не удалось захватить экран", error)
            self.restore_settings()
        return False

    def lookup(self, word, sentence):
        if not word or not self.overlay:
            return
        self.lookup_id += 1
        lookup_id, generation, config = self.lookup_id, self.generation, self.config
        if self.future:
            self.future.cancel()
        self.overlay.loading(word, sentence)

        def run_lookup():
            result = LookupResult(word, sentence)
            try:
                result.senses = self.translator.word(word, sentence, config)
            except Exception as error:
                result.errors.append("Слово: " + str(error))
            result.word_complete = True
            # Show local dictionary results while the sentence request is running.
            partial = replace(result, errors=list(result.errors))
            GLib.idle_add(self.lookup_finished, generation, lookup_id, partial)
            try:
                translated = self.translator.translate(sentence, config)
                if translated:
                    result.translated_sentence, result.sentence_source = (
                        translated[0].text,
                        translated[0].source,
                    )
            except Exception as error:
                result.errors.append("Предложение: " + str(error))
            result.complete = True
            GLib.idle_add(self.lookup_finished, generation, lookup_id, result)

        self.future = self.executor.submit(run_lookup)

    def save_result(self, result):
        if not result or not result.complete or result.saved or result.saving or not self.overlay:
            return
        generation, lookup_id, config = self.generation, self.lookup_id, self.config
        self.overlay.show_result(replace(result, saving=True, save_error=""))

        def run_save():
            try:
                saved = save_lookup(config.vocabulary_path, result, config.source, config.target)
                updated = replace(result, saved=saved)
            except OSError:
                updated = replace(result, save_error="Не удалось записать файл слов")
            GLib.idle_add(self.lookup_finished, generation, lookup_id, updated)

        self.save_executor.submit(run_save)

    def lookup_finished(self, generation, lookup_id, result):
        if generation == self.generation and lookup_id == self.lookup_id and self.overlay:
            self.overlay.show_result(result)
        return False

    def overlay_closed(self, window):
        if self.overlay is window:
            self.overlay = None
            self.generation += 1
            if self.future:
                self.future.cancel()
            self.restore_settings()

    def restore_settings(self):
        if self.hidden_settings is not None:
            if self.hidden_settings is self.settings and not self.stop.is_set():
                self.settings.present()
            self.hidden_settings = None

    def open_settings(self):
        if self.overlay:
            self.overlay.destroy()
        if self.settings:
            self.hidden_settings = None
            self.settings.present()
            return
        self.settings = Settings(self)
        self.settings.connect("destroy", lambda *_: setattr(self, "settings", None))

    def open_history(self):
        path = self.config.vocabulary_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)
            Gio.AppInfo.launch_default_for_uri(path.as_uri(), None)
        except Exception as error:
            self.notify("Файл слов", str(error))

    def quit_signal(self):
        self.quit()
        return False

    def do_shutdown(self):
        self.stop.set()
        self.generation += 1
        if self.overlay:
            self.overlay.destroy()
        if self.settings:
            chooser = getattr(self.settings, "chooser", None)
            if chooser:
                chooser.destroy()
            self.settings.destroy()
        try:
            if self.hotkey and self.desktop == "hyprland":
                self.hotkey.remove()
        except Exception as error:
            LOG.warning("Не удалось удалить временный бинд: %s", error)
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.save_executor.shutdown(wait=False)
        self.capture_executor.shutdown(wait=False, cancel_futures=True)
        Gtk.Application.do_shutdown(self)
