"""GTK3 UI; all GTK calls stay on the main thread."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import logging
from math import ceil, floor, pi
from pathlib import Path
import signal
import threading
from time import monotonic

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk  # noqa: E402

from .card import TranslationCard  # noqa: E402
from .config import Config  # noqa: E402
from .desktop import APP_ID, CaptureCancelled, capture_screen, current_desktop  # noqa: E402
from .dictionary import Dictionary, normalize  # noqa: E402
from .history import save_lookup  # noqa: E402
from .hyprland import Hotkey as HyprlandHotkey, watch_reload  # noqa: E402
from .layout import (  # noqa: E402
    Rect, ease_out_cubic, line_of, phrase_ranges, place_card, selection_range,
)
from .ocr import png_size, recognize, sentence_for  # noqa: E402
from .translation import LookupResult, Translator  # noqa: E402

LOG = logging.getLogger(__name__)

# Highlight palette: calm blue for found words, warm amber for the selection.
ACCENT = (0.541, 0.706, 1.0)  # #8AB4FF
SELECTED = (1.0, 0.784, 0.341)  # #FFC857
WORD_FADE = 0.18
HOVER_FADE = 0.12
CARD_MOTION = 0.17
CARD_SLIDE = 10


def rounded_rectangle(cr, x, y, width, height, radius):
    radius = max(0.0, min(radius, width / 2, height / 2))
    cr.new_sub_path()
    cr.arc(x + width - radius, y + radius, radius, -pi / 2, 0)
    cr.arc(x + width - radius, y + height - radius, radius, 0, pi / 2)
    cr.arc(x + radius, y + height - radius, radius, pi / 2, pi)
    cr.arc(x + radius, y + radius, radius, pi, 3 * pi / 2)
    cr.close_path()


def approach(value, target, step):
    return min(target, value + step) if value < target else max(target, value - step)


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
        self.selection = []
        self.selection_auto = False
        self.hovered = None
        self.drag = None
        self.presence = {}
        self.hover_level = {}
        self.tick_id = 0
        self.last_frame = None
        self.card_side = None
        self.card_from = None
        self.card_to = None
        self.card_state = (0.0, 0.0, 0.0)
        self.card_progress = 1.0
        self.card_layout = 0
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
        self.canvas.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self.canvas.connect("draw", self.draw)
        self.canvas.connect("button-press-event", self.press)
        self.canvas.connect("button-release-event", self.release)
        self.canvas.connect("motion-notify-event", self.motion)
        self.canvas.connect("size-allocate", self.schedule_input_update)
        overlay.add(self.canvas)

        self.card = TranslationCard(
            app.config, gdk_monitor.get_geometry(), self.destroy, app.lookup, app.save_result
        )
        self.card.set_no_show_all(True)
        self.card.connect("size-allocate", self.card_resized)
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
        # The refining OCR pass returns new objects; equal words keep their state.
        known = set(words)
        self.selection = [word for word in self.selection if word in known]
        if self.hovered not in known:
            self.hovered = None
        self.presence = {word: self.presence.get(word, 0.0) for word in words}
        self.hover_level = {w: v for w, v in self.hover_level.items() if w in known}
        self.words = words
        self.animate()
        self.status.set_text(
            f"{len(words)} слов · Клик — слово или фраза · Протяните по словам — своя фраза"
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
            # Dim the frozen screenshot slightly so highlights stand out.
            cr.set_source_rgba(0.02, 0.03, 0.05, 0.22)
            cr.paint()
        pixel = 1 / self.scale
        pad = 2.5 * pixel
        radius = 5 * pixel
        selected = set(self.selection)
        for word in self.words:
            if word in selected:
                continue
            presence = ease_out_cubic(self.presence.get(word, 1.0))
            hover = ease_out_cubic(self.hover_level.get(word, 0.0))
            rounded_rectangle(
                cr, word.x - pad, word.y - pad, word.width + 2 * pad, word.height + 2 * pad, radius
            )
            cr.set_source_rgba(*ACCENT, (0.10 + 0.16 * hover) * presence)
            cr.fill_preserve()
            cr.set_source_rgba(*ACCENT, (0.42 + 0.53 * hover) * presence)
            cr.set_line_width((1 + 0.6 * hover) * pixel)
            cr.stroke()
        for left, top, right, bottom in self.selection_boxes():
            glow = 3 * pixel
            rounded_rectangle(
                cr, left - pad - glow, top - pad - glow,
                right - left + 2 * (pad + glow), bottom - top + 2 * (pad + glow), radius + glow,
            )
            cr.set_source_rgba(*SELECTED, 0.16)
            cr.fill()
            rounded_rectangle(
                cr, left - pad, top - pad, right - left + 2 * pad, bottom - top + 2 * pad, radius
            )
            cr.set_source_rgba(*SELECTED, 0.30)
            cr.fill_preserve()
            cr.set_source_rgba(*SELECTED, 0.95)
            cr.set_line_width(1.8 * pixel)
            cr.stroke()
        cr.restore()
        return False

    def selection_boxes(self):
        """One rounded box per line of the selection in image coordinates."""
        boxes = {}
        for word in self.selection:
            box = boxes.get(line_of(word))
            edges = (word.x, word.y, word.x + word.width, word.y + word.height)
            boxes[line_of(word)] = edges if box is None else (
                min(box[0], edges[0]), min(box[1], edges[1]),
                max(box[2], edges[2]), max(box[3], edges[3]),
            )
        return list(boxes.values())

    # ------------------------------------------------------------ animation
    def animate(self):
        if not self.tick_id:
            self.last_frame = None
            self.tick_id = self.add_tick_callback(self.on_tick)

    def on_tick(self, _widget, clock):
        now = clock.get_frame_time() / 1_000_000
        step = 0.0 if self.last_frame is None else min(0.05, now - self.last_frame)
        self.last_frame = now
        busy = False
        for word, value in self.presence.items():
            if value < 1:
                self.presence[word] = min(1.0, value + step / WORD_FADE)
                busy = True
        for word in set(self.hover_level) | ({self.hovered} - {None}):
            value = self.hover_level.get(word, 0.0)
            target = 1.0 if word is self.hovered else 0.0
            value = approach(value, target, step / HOVER_FADE)
            if value != target:
                busy = True
            if value <= 0:
                self.hover_level.pop(word, None)
            else:
                self.hover_level[word] = value
        if self.card_progress < 1 and self.card_to is not None:
            self.card_progress = min(1.0, self.card_progress + step / CARD_MOTION)
            busy = busy or self.card_progress < 1
            self.apply_card_state()
        self.canvas.queue_draw()
        if busy:
            return True
        self.tick_id = 0
        return False

    def apply_card_state(self):
        eased = ease_out_cubic(self.card_progress)
        state = tuple(a + (b - a) * eased for a, b in zip(self.card_from, self.card_to))
        self.card_state = state
        x, y, opacity = state
        self.card.set_margin_start(max(0, round(x)))
        self.card.set_margin_top(max(0, round(y)))
        self.card.set_opacity(opacity)
        if self.card_progress >= 1 and self.card_to[2] <= 0:
            self.card.hide()

    def move_card(self, target, start=None):
        self.card_from = start if start is not None else self.card_state
        self.card_to = target
        self.card_progress = 0.0
        self.apply_card_state()
        self.animate()

    # -------------------------------------------------------- card placement
    def schedule_card_layout(self, *_):
        if not self.card_layout:
            self.card_layout = GLib.idle_add(self.layout_card)

    def card_resized(self, _widget, allocation):
        size = (allocation.width, allocation.height)
        if size != getattr(self, "card_size", None):
            self.card_size = size
            self.schedule_card_layout()

    def layout_card(self, appear=False):
        self.card_layout = 0
        if not self.card.get_visible() or not self.selection:
            return False
        self.update_transform()
        ox, oy = self.offset
        left, top, right, bottom = (
            min(edge[i] for edge in self.selection_boxes()) if i < 2
            else max(edge[i] for edge in self.selection_boxes())
            for i in range(4)
        )
        anchor = Rect(
            ox + left * self.scale, oy + top * self.scale,
            (right - left) * self.scale, (bottom - top) * self.scale,
        )
        width = max(self.card.get_size_request()[0], self.card.get_preferred_width()[0])
        height = self.card.get_preferred_height_for_width(width)[1]
        x, y, side = place_card(
            anchor, width, height,
            self.canvas.get_allocated_width(), self.canvas.get_allocated_height(),
            prefer=self.card_side,
        )
        if not appear and (x, y, 1.0) == self.card_to and side == self.card_side:
            return False
        self.card_side = side
        if appear:
            # Fade in while sliding a few pixels away from the word.
            dx = {"right": -CARD_SLIDE, "left": CARD_SLIDE}.get(side, 0)
            dy = {"below": -CARD_SLIDE, "above": CARD_SLIDE}.get(side, 0)
            self.move_card((x, y, 1.0), start=(x + dx, y + dy, 0.0))
        else:
            self.move_card((x, y, 1.0))
        return False

    def hide_card(self):
        if self.card.get_visible():
            x, y, _opacity = self.card_state
            self.move_card((x, y, 0.0))

    # ----------------------------------------------------------- selection
    def word_at(self, x, y):
        x = (x - self.offset[0]) / self.scale
        y = (y - self.offset[1]) / self.scale
        matches = [w for w in self.words if w.contains(x, y, 3 / self.scale)]
        return min(matches, key=lambda w: w.width * w.height) if matches else None

    def index_of(self, word):
        return next(index for index, item in enumerate(self.words) if item is word)

    def motion(self, widget, event):
        word = self.word_at(event.x, event.y)
        if word is not self.hovered:
            self.hovered = word
            widget.get_window().set_cursor(
                Gdk.Cursor.new_from_name(widget.get_display(), "pointer" if word else "default")
            )
            self.animate()
        if self.drag and word and event.state & Gdk.ModifierType.BUTTON1_MASK:
            span = selection_range(self.words, self.drag[0], self.index_of(word))
            if span and (span[1] > span[0] or self.drag[2]):
                self.drag[2] = True
                self.selection = self.words[span[0]:span[1] + 1]
                widget.queue_draw()
        return False

    def press(self, _widget, event):
        if event.button != 1:
            return False
        if event.type != Gdk.EventType.BUTTON_PRESS:
            return True  # Double and triple clicks repeat the first press.
        word = self.word_at(event.x, event.y)
        if not word:
            self.selection = []
            self.hide_card()
            self.canvas.queue_draw()
            return True
        modifiers = Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.CONTROL_MASK
        # [anchor index, single word requested, dragged]
        self.drag = [self.index_of(word), bool(event.state & modifiers), False]
        return True

    def release(self, _widget, event):
        if event.button != 1 or not self.drag:
            return False
        index, single, dragged = self.drag
        self.drag = None
        if dragged and len(self.selection) > 1:
            self.lookup_selection(self.selection, auto=False)
            return True
        word = self.words[index]
        # A second click on a word of a found phrase translates only that word.
        if self.selection_auto and len(self.selection) > 1 and word in self.selection:
            single = True
        first, last = (index, index) if single else self.phrase_span(index)
        self.lookup_selection(self.words[first:last + 1], auto=last > first)
        return True

    def phrase_span(self, index):
        ranges = phrase_ranges(self.words, index)
        if not ranges:
            return index, index
        config = self.app.config
        try:
            known = self.app.dictionary.existing_words(
                {text for *_span, text in ranges}, config.source, config.target
            )
        except Exception:  # noqa: BLE001 - phrase search is optional
            LOG.debug("Поиск фраз недоступен", exc_info=True)
            return index, index
        for first, last, text in ranges:
            if normalize(text) in known:
                return first, last
        return index, index

    def lookup_selection(self, words, auto):
        self.selection = list(words)
        self.selection_auto = auto
        self.canvas.queue_draw()
        text = " ".join(word.lookup for word in words)
        self.app.lookup(text, sentence_for(self.words, words[0]))

    def loading(self, word, sentence):
        self.last_result = None
        appear = not self.card.get_visible() or self.card_state[2] < 0.5
        self.card.loading(word, sentence)
        if self.selection_auto:
            self.card.set_hint("Найдена фраза · нажмите на слово ещё раз, чтобы перевести только его")
        if appear:
            self.card_side = None
            self.card.set_opacity(0)
        self.card.show()
        if self.card_layout:
            GLib.source_remove(self.card_layout)
            self.card_layout = 0
        self.layout_card(appear=appear)

    def show_result(self, result):
        self.last_result = result
        self.card.render(result)
        self.schedule_card_layout()


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
            started = monotonic()

            def show_primary(words):
                nonlocal shown
                if not self.stop.is_set():
                    shown = True
                    LOG.info(
                        "Первые рамки: %d мс (%d слов)",
                        (monotonic() - started) * 1000, len(words),
                    )
                    GLib.idle_add(self.show_capture, generation, png, monitor, words)

            try:
                png, monitor = capture_screen(self.desktop, self.stop, choose_monitor)
                LOG.info("Захват экрана: %d мс", (monotonic() - started) * 1000)
                if not self.stop.is_set():
                    words = recognize(
                        png, config, on_primary=show_primary,
                        known_words=lambda terms: self.dictionary.existing_words(
                            terms, config.source, config.target
                        ),
                    )
                    if shown:
                        LOG.info("Полный OCR: %d мс", (monotonic() - started) * 1000)
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
