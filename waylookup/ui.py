"""GTK3 + layer-shell UI; all GTK calls stay on the main thread."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import logging
from pathlib import Path
import signal
import threading

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, GtkLayerShell  # noqa: E402

from .card import TranslationCard  # noqa: E402
from .config import Config  # noqa: E402
from .dictionary import Dictionary  # noqa: E402
from .history import save_lookup  # noqa: E402
from .hyprland import Hotkey, capture_output, focused_monitor, watch_reload  # noqa: E402
from .ocr import recognize, sentence_for  # noqa: E402
from .translation import LookupResult, Translator  # noqa: E402

LOG = logging.getLogger(__name__)


def label(text="", css=None, wrap=True):
    widget = Gtk.Label(label=text, xalign=0)
    widget.set_line_wrap(wrap)
    widget.set_max_width_chars(85)
    if css:
        widget.get_style_context().add_class(css)
    return widget


def layer_window(window, namespace, monitor=None, fullscreen=False):
    GtkLayerShell.init_for_window(window)
    GtkLayerShell.set_namespace(window, namespace)
    GtkLayerShell.set_layer(window, GtkLayerShell.Layer.OVERLAY)
    GtkLayerShell.set_exclusive_zone(window, -1)  # Cover panels; never reserve desktop space.
    GtkLayerShell.set_keyboard_mode(window, GtkLayerShell.KeyboardMode.EXCLUSIVE)
    if monitor:
        GtkLayerShell.set_monitor(window, monitor)
    if fullscreen:
        for edge in (
            GtkLayerShell.Edge.TOP,
            GtkLayerShell.Edge.BOTTOM,
            GtkLayerShell.Edge.LEFT,
            GtkLayerShell.Edge.RIGHT,
        ):
            GtkLayerShell.set_anchor(window, edge, True)
    window.set_decorated(False)
    window.set_app_paintable(True)
    visual = window.get_screen().get_rgba_visual()
    if visual:
        window.set_visual(visual)


class Overlay(Gtk.ApplicationWindow):
    def __init__(self, app, png: bytes, monitor: dict):
        super().__init__(application=app, title="WayLookup")
        self.app = app
        self.words = []
        self.selected = None
        self.hovered = None
        self.scale = 1.0
        self.offset = (0.0, 0.0)
        loader = GdkPixbuf.PixbufLoader.new_with_type("png")
        loader.write(png)
        loader.close()
        self.pixbuf = loader.get_pixbuf()
        display = Gdk.Display.get_default()
        # GTK and Hyprland both report logical layout coordinates, including fractional scale.
        gdk_monitor = display.get_monitor_at_point(monitor["x"] + 1, monitor["y"] + 1)
        layer_window(self, "waylookup", gdk_monitor, fullscreen=True)
        self.connect("key-press-event", self.on_key)
        self.connect("destroy", lambda *_: self.app.overlay_closed(self))
        overlay = Gtk.Overlay()
        self.add(overlay)
        self.canvas = Gtk.DrawingArea()
        self.canvas.set_hexpand(True)
        self.canvas.set_vexpand(True)
        self.canvas.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.POINTER_MOTION_MASK)
        self.canvas.connect("draw", self.draw)
        self.canvas.connect("button-press-event", self.click)
        self.canvas.connect("motion-notify-event", self.motion)
        overlay.add(self.canvas)

        self.card = TranslationCard(
            app.config, gdk_monitor.get_geometry(), self.destroy, app.lookup
        )
        self.card.set_no_show_all(True)
        overlay.add_overlay(self.card)

        bar = Gtk.Box(spacing=14)
        bar.get_style_context().add_class("capture-bar")
        bar.set_halign(Gtk.Align.CENTER)
        bar.set_valign(Gtk.Align.END)
        bar.set_margin_bottom(30)
        self.status = label("Распознавание текста…", "status")
        bar.pack_start(self.status, True, True, 0)
        exit_button = Gtk.Button(label="Esc · Закрыть")
        exit_button.connect("clicked", lambda *_: self.destroy())
        bar.pack_start(exit_button, False, False, 0)
        overlay.add_overlay(bar)
        self.show_all()
        self.card.hide()

    def set_words(self, words):
        self.words = words
        self.status.set_text(
            f"{len(words)} слов · Нажмите на слово для перевода"
            if words
            else "Текст не найден · Попробуйте другой язык OCR или PSM в настройках"
        )
        self.canvas.queue_draw()

    def on_key(self, _widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.destroy()
            return True
        return False

    def draw(self, widget, cr):
        width, height = widget.get_allocated_width(), widget.get_allocated_height()
        self.scale = min(width / self.pixbuf.get_width(), height / self.pixbuf.get_height())
        ox = (width - self.pixbuf.get_width() * self.scale) / 2
        oy = (height - self.pixbuf.get_height() * self.scale) / 2
        self.offset = (ox, oy)
        cr.set_source_rgb(0.03, 0.04, 0.06)
        cr.paint()
        cr.save()
        cr.translate(ox, oy)
        cr.scale(self.scale, self.scale)
        Gdk.cairo_set_source_pixbuf(cr, self.pixbuf, 0, 0)
        cr.paint()
        cr.set_source_rgba(0.02, 0.04, 0.08, 0.12)
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
        layer_window(self, "waylookup-settings")
        self.set_default_size(650, 720)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, margin=24)
        self.add(root)
        root.pack_start(label("WayLookup", "word"), False, False, 0)
        root.pack_start(
            label("Настройте перевод один раз — дальше достаточно бинда.", "muted"), False, False, 0
        )
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        root.pack_start(scroller, True, True, 0)
        grid = Gtk.Grid(column_spacing=18, row_spacing=12)
        scroller.add(grid)
        self.inputs = {}
        row = 0
        specs = [
            ("hotkey", "Бинд", None),
            ("register_hotkey", "Регистрировать бинд", "bool"),
            ("source", "Исходный язык", None),
            ("target", "Язык перевода", None),
            ("ocr_languages", "Языки OCR (eng+rus)", None),
            (
                "ocr_psm",
                "Разметка OCR",
                [
                    (3, "3 · Автоматически"),
                    (6, "6 · Один блок"),
                    (11, "11 · Разрозненный текст"),
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
            ("dictionary_fallback", "Переводчик, если нет в словаре", "bool"),
            (
                "sentence_provider",
                "Переводчик предложения",
                [
                    ("mymemory", "MyMemory · онлайн, без ключа"),
                    ("libretranslate", "LibreTranslate"),
                    ("ollama", "Ollama · ИИ"),
                    ("off", "Отключить"),
                ],
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
        # Native/portal dialogs are regular surfaces; hide our covering layer while choosing.
        self.hide()
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.NONE)
        chooser.show()

    def import_selected(self, chooser, response):
        self.chooser = None
        self.show_all()
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.EXCLUSIVE)
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
        super().__init__(
            application_id="io.github.waylookup.WayLookup",
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self.config = Config.load()
        self.overlay = None
        self.settings = None
        self.hotkey = None
        self.capturing = False
        self.generation = 0
        self.lookup_id = 0
        self.future = None
        self.stop = threading.Event()
        self.executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="waylookup-translation"
        )
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
        GLib.idle_add(self.setup_hotkey)
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
        self.notify(
            "Tray недоступен",
            "Установите libayatana-appindicator и включите tray в панели. Доступны waylookup capture/settings/quit.",
        )

    def notify(self, title, body):
        notification = Gio.Notification.new(title)
        notification.set_body(body)
        notification.set_icon(Gio.ThemedIcon.new("accessories-dictionary"))
        self.send_notification(None, notification)
        LOG.warning("%s: %s", title, body)

    def setup_hotkey(self):
        try:
            if self.config.register_hotkey:
                if self.hotkey is None:
                    self.hotkey = Hotkey()
                self.hotkey.register(self.config.hotkey)
        except Exception as error:
            self.notify("Бинд WayLookup", str(error))
        return False

    def apply_config(self, config):
        old = self.config
        try:
            if config.register_hotkey:
                if self.hotkey is None:
                    self.hotkey = Hotkey()
                self.hotkey.register(config.hotkey)
            elif self.hotkey:
                self.hotkey.remove()
            config.save()
        except Exception:
            if self.hotkey:
                if old.register_hotkey:
                    self.hotkey.register(old.hotkey)
                else:
                    self.hotkey.remove()
            raise
        self.config = config

    def capture(self):
        if self.overlay:
            self.overlay.destroy()
            return
        if self.capturing or self.settings:
            return
        self.capturing = True
        self.generation += 1
        generation = self.generation

        def run_capture():
            try:
                monitor = focused_monitor()
                png = capture_output(monitor["name"])
                GLib.idle_add(self.show_capture, generation, png, monitor)
            except Exception as error:
                GLib.idle_add(self.capture_failed, generation, str(error))

        self.capture_executor.submit(run_capture)

    def show_capture(self, generation, png, monitor):
        if generation != self.generation or self.stop.is_set():
            return False
        self.capturing = False
        try:
            self.overlay = Overlay(self, png, monitor)
        except Exception as error:
            self.notify("Ошибка оверлея", str(error))
            return False
        config = self.config

        def run_ocr():
            try:
                words = recognize(png, config)
                GLib.idle_add(self.ocr_finished, generation, words, None)
            except Exception as error:
                GLib.idle_add(self.ocr_finished, generation, [], str(error))

        self.capture_executor.submit(run_ocr)
        return False

    def capture_failed(self, generation, error):
        if generation == self.generation:
            self.capturing = False
            self.notify("Не удалось захватить экран", error)
        return False

    def ocr_finished(self, generation, words, error):
        if generation == self.generation and self.overlay:
            if error:
                self.overlay.status.set_text(error)
            else:
                self.overlay.set_words(words)
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
            try:
                result.saved = save_lookup(
                    config.vocabulary_path, result, config.source, config.target
                )
            except OSError:
                result.errors.append("Не удалось сохранить слово: проверьте путь и права на файл")
            result.complete = True
            GLib.idle_add(self.lookup_finished, generation, lookup_id, result)

        self.future = self.executor.submit(run_lookup)

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

    def open_settings(self):
        if self.overlay:
            self.overlay.destroy()
        if self.settings:
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
            if self.hotkey:
                self.hotkey.remove()
        except Exception as error:
            LOG.warning("Не удалось удалить временный бинд: %s", error)
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.capture_executor.shutdown(wait=False, cancel_futures=True)
        Gtk.Application.do_shutdown(self)
