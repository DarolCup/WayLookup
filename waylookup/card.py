"""Structured, size-bounded translation card for the screenshot overlay."""

import re

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

from .presentation import clean_text, meaning_groups  # noqa: E402
from .translation import LookupResult  # noqa: E402


def text_label(text="", css="", selectable=False):
    widget = Gtk.Label(label=text, xalign=0)
    widget.set_line_wrap(True)
    widget.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
    widget.set_width_chars(1)
    widget.set_max_width_chars(65)
    widget.set_hexpand(True)
    widget.set_selectable(selectable)
    if css:
        widget.get_style_context().add_class(css)
    return widget


def box(css="", spacing=8):
    widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)
    if css:
        widget.get_style_context().add_class(css)
    return widget


def badge(text, css="source-badge"):
    widget = Gtk.Label(label=text, xalign=0)
    widget.set_ellipsize(Pango.EllipsizeMode.END)
    widget.set_max_width_chars(45)
    widget.set_tooltip_text(text)
    widget.set_halign(Gtk.Align.START)
    widget.get_style_context().add_class(css)
    return widget


class TranslationCard(Gtk.Box):
    def __init__(self, config, geometry, close, retry):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.config = config
        self.retry_callback = retry
        self.last_result = None
        self.senses_signature = None
        self.get_style_context().add_class("translation-card")
        self.set_size_request(min(720, max(240, geometry.width - 48)), -1)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.START)
        self.set_margin_top(24)

        heading = box("card-heading", 10)
        self.pack_start(heading, False, False, 0)
        top = Gtk.Box(spacing=10)
        brand = badge("WAYLOOKUP", "brand")
        brand.set_ellipsize(Pango.EllipsizeMode.NONE)
        brand.set_width_chars(12)
        top.pack_start(brand, True, True, 0)
        top.pack_start(
            badge(f"{config.source.upper()} → {config.target.upper()}", "language-badge"),
            False,
            False,
            0,
        )
        close_button = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.BUTTON)
        close_button.get_style_context().add_class("icon-button")
        close_button.set_tooltip_text("Закрыть · Esc")
        close_button.connect("clicked", lambda *_: close())
        top.pack_start(close_button, False, False, 0)
        heading.pack_start(top, False, False, 0)
        self.word_label = text_label("", "selected-word", selectable=True)
        self.word_label.set_max_width_chars(24)
        self.word_label.set_lines(2)
        self.word_label.set_ellipsize(Pango.EllipsizeMode.END)
        heading.pack_start(self.word_label, False, False, 0)

        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroll.set_propagate_natural_height(True)
        self.scroll.set_max_content_height(max(100, min(440, geometry.height - 330)))
        self.pack_start(self.scroll, True, True, 0)
        body = box("card-body", 18)
        self.scroll.add(body)

        word_section = box("", 10)
        body.pack_start(word_section, False, False, 0)
        word_section.pack_start(text_label("ЗНАЧЕНИЯ СЛОВА", "section"), False, False, 0)
        self.word_state = text_label("", "empty-state")
        word_section.pack_start(self.word_state, False, False, 0)
        self.meanings_box = box("", 12)
        word_section.pack_start(self.meanings_box, False, False, 0)

        sentence_section = box("sentence-panel", 10)
        body.pack_start(sentence_section, False, False, 0)
        sentence_section.pack_start(text_label("ПЕРЕВОД ПРЕДЛОЖЕНИЯ", "section"), False, False, 0)
        self.translation_label = text_label("", "sentence-translation", selectable=True)
        sentence_section.pack_start(self.translation_label, False, False, 0)
        self.sentence_source = badge("")
        sentence_section.pack_start(self.sentence_source, False, False, 0)
        sentence_section.pack_start(text_label("ОРИГИНАЛ", "original-caption"), False, False, 0)
        self.context_label = text_label("", "context", selectable=True)
        sentence_section.pack_start(self.context_label, False, False, 0)

        self.error_box = box("error-panel", 6)
        self.error_box.pack_start(
            text_label("Не всё удалось перевести", "error-title"), False, False, 0
        )
        self.error_label = text_label("", "warning")
        self.error_box.pack_start(self.error_label, False, False, 0)
        body.pack_start(self.error_box, False, False, 0)

        footer = Gtk.Box(spacing=10)
        footer.get_style_context().add_class("card-footer")
        self.pack_start(footer, False, False, 0)
        self.spinner = Gtk.Spinner()
        footer.pack_start(self.spinner, False, False, 0)
        self.saved_label = text_label("", "muted")
        footer.pack_start(self.saved_label, True, True, 0)
        self.retry_button = Gtk.Button(label="Повторить")
        self.retry_button.connect("clicked", self.retry)
        footer.pack_start(self.retry_button, False, False, 0)
        self.copy_button = Gtk.Button(label="Копировать")
        self.copy_button.set_tooltip_text("Слово, все значения и предложение")
        self.copy_button.connect("clicked", self.copy_result)
        footer.pack_start(self.copy_button, False, False, 0)
        self.show_all()

    def loading(self, word, sentence):
        self.senses_signature = None
        self.word_label.set_text(clean_text(word, strip_markup=False))
        self.word_label.set_tooltip_text(clean_text(word, strip_markup=False))
        escaped = []
        # Escape every fragment before inserting our own Pango highlight markup.
        for part in re.split(
            f"(?<!\\w)({re.escape(word)})(?!\\w)",
            clean_text(sentence, strip_markup=False),
            flags=re.IGNORECASE,
        ):
            text = GLib.markup_escape_text(part)
            escaped.append(
                f'<span foreground="#8ce8d6" weight="bold">{text}</span>'
                if part.casefold() == word.casefold()
                else text
            )
        self.context_label.set_markup("".join(escaped))
        self.scroll.get_vadjustment().set_value(0)
        self.render(LookupResult(word, sentence))

    def render_meanings(self, senses):
        signature = tuple(senses)
        if signature == self.senses_signature:
            return
        self.senses_signature = signature
        for child in self.meanings_box.get_children():
            child.destroy()
        for group in meaning_groups(senses):
            section = box("meaning-group", 8)
            section.pack_start(badge(group.source), False, False, 0)
            for i, meaning in enumerate(group.meanings, 1):
                row = Gtk.Box(spacing=12)
                number = badge(f"{i:02d}", "meaning-number")
                number.set_valign(Gtk.Align.START)
                row.pack_start(number, False, False, 0)
                content = box("", 6)
                row.pack_start(content, True, True, 0)
                content.pack_start(
                    text_label(meaning.summary, "meaning-text", selectable=True), False, False, 0
                )
                if meaning.article:
                    details = Gtk.Expander(label="Полная словарная статья")
                    article_scroll = Gtk.ScrolledWindow()
                    article_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
                    article_scroll.set_propagate_natural_height(True)
                    article_scroll.set_max_content_height(160)
                    article_scroll.add(text_label(meaning.article, "article-text", selectable=True))
                    details.add(article_scroll)
                    content.pack_start(details, False, False, 0)
                section.pack_start(row, False, False, 0)
            self.meanings_box.pack_start(section, False, False, 0)
        self.meanings_box.show_all()

    def render(self, result):
        self.last_result = result
        self.render_meanings(result.senses)
        has_meanings = bool(meaning_groups(result.senses))
        self.word_state.set_visible(not has_meanings)
        if not result.word_complete and not result.complete:
            self.word_state.set_text("Ищем перевод слова…")
        elif any(error.startswith("Слово:") for error in result.errors):
            self.word_state.set_text("Перевод слова недоступен. Можно повторить запрос.")
        else:
            self.word_state.set_text(
                "Нет значений. Подключите словарь или выберите другой переводчик в настройках."
            )
        sentence = clean_text(result.translated_sentence)
        self.sentence_source.set_text(clean_text(result.sentence_source))
        self.sentence_source.set_tooltip_text(clean_text(result.sentence_source))
        self.sentence_source.set_visible(bool(sentence and result.sentence_source))
        if sentence:
            self.translation_label.set_text(sentence)
        elif self.config.sentence_provider == "off":
            self.translation_label.set_text("Перевод предложения отключён")
        elif not result.complete:
            self.translation_label.set_text("Переводим предложение…")
        else:
            self.translation_label.set_text("Не удалось получить перевод предложения")
        self.error_box.set_visible(bool(result.errors))
        self.error_label.set_text("\n".join(clean_text(error) for error in result.errors))
        busy = not result.complete
        self.spinner.set_visible(busy)
        self.spinner.start() if busy else self.spinner.stop()
        self.retry_button.set_visible(bool(result.complete and result.errors))
        self.copy_button.set_sensitive(bool(result.senses or sentence))
        if result.saved:
            self.saved_label.set_text("✓ Сохранено в " + self.config.vocabulary_path.name)
        elif busy:
            self.saved_label.set_text(
                "Получаем перевод…"
                if not has_meanings
                else "Перевод слова готов · ждём предложение"
            )
        elif has_meanings:
            self.saved_label.set_text("Не удалось сохранить слово")
        else:
            self.saved_label.set_text("Нет перевода для сохранения")

    def retry(self, *_):
        if self.last_result:
            self.retry_callback(self.last_result.word, self.last_result.sentence)

    def copy_result(self, *_):
        result = self.last_result
        if not result:
            return
        lines = [clean_text(result.word), "", "Значения:"]
        for group in meaning_groups(result.senses):
            lines.append(group.source)
            lines.extend(
                f"{i}. {meaning.article or meaning.summary}"
                for i, meaning in enumerate(group.meanings, 1)
            )
        lines.extend(["", "Оригинал:", clean_text(result.sentence, strip_markup=False)])
        if result.translated_sentence:
            lines.extend(["", "Перевод:", clean_text(result.translated_sentence)])
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text("\n".join(lines), -1)
