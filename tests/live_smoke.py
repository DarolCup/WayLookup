"""Opt-in real Hyprland smoke test. Temporarily covers the focused monitor.

Run: python tests/live_smoke.py [--screenshot /tmp/waylookup-preview.png]
Uses isolated XDG directories, synthetic text and a local HTTP fixture. No external translations.
"""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--screenshot", type=Path)
parser.add_argument("--stress-screenshot", type=Path)
args = parser.parse_args()

temporary = tempfile.TemporaryDirectory(prefix="waylookup-smoke-")
os.environ["XDG_CONFIG_HOME"] = temporary.name + "/config"
os.environ["XDG_DATA_HOME"] = temporary.name + "/data"
os.environ["GDK_BACKEND"] = "wayland"

import cairo  # noqa: E402
from waylookup.config import Config  # noqa: E402
from waylookup.dictionary import Dictionary, Sense  # noqa: E402
from waylookup.hyprland import capture_output, focused_monitor  # noqa: E402
from waylookup.translation import LookupResult  # noqa: E402
from waylookup.ui import Application, Gdk, GLib, Gtk  # noqa: E402


class Server(BaseHTTPRequestHandler):
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert self.path == "/translate"
        assert payload["q"] == "Take the sword and follow the river."
        body = json.dumps({"translatedText": "Возьми меч и следуй вдоль реки."}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
threading.Thread(target=server.serve_forever, daemon=True).start()
Config(
    register_hotkey=False,
    sentence_provider="libretranslate",
    libretranslate_url=f"http://127.0.0.1:{server.server_port}",
).save()
source = Path(temporary.name) / "fixture.json"
source.write_text(
    json.dumps({"sword": ["меч — холодное оружие", "клинок", "военная сила (перен.)"]})
)
Dictionary().import_json(source, "en", "ru", "Smoke Dictionary")

monitor = focused_monitor()
# Exercise real screencopy without persisting the user's screen.
assert capture_output(monitor["name"]).startswith(b"\x89PNG")
surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1920, 1080)
cr = cairo.Context(surface)
cr.set_source_rgb(0.055, 0.085, 0.12)
cr.paint()
# A stylised game scene with known text at subtitle position.
cr.set_source_rgb(0.08, 0.16, 0.19)
cr.move_to(0, 660)
cr.line_to(420, 330)
cr.line_to(850, 700)
cr.line_to(1390, 260)
cr.line_to(1920, 690)
cr.line_to(1920, 1080)
cr.line_to(0, 1080)
cr.close_path()
cr.fill()
cr.set_source_rgb(0.15, 0.31, 0.34)
cr.move_to(1120, 600)
cr.line_to(1450, 1080)
cr.line_to(700, 1080)
cr.close_path()
cr.fill()
cr.set_source_rgb(0.035, 0.045, 0.065)
cr.rectangle(460, 785, 1050, 110)
cr.fill()
cr.set_source_rgb(0.9, 0.95, 1)
cr.select_font_face("DejaVu Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
cr.set_font_size(38)
cr.move_to(120, 150)
cr.show_text("THE NORTHERN PASS")
cr.set_font_size(34)
cr.move_to(540, 855)
cr.show_text("Take the sword and follow the river.")
output = io.BytesIO()
surface.write_to_png(output)
png = output.getvalue()


class Smoke(Application):
    def __init__(self):
        super().__init__()
        self.set_application_id("io.github.waylookup.Smoke")
        self.stage = 0
        self.failure = None
        self.deadline = time.monotonic() + 20

    def do_command_line(self, _command_line):
        self.generation += 1
        self.show_capture(self.generation, png, monitor)
        GLib.timeout_add(100, self.check)
        return 0

    def check(self):
        try:
            assert time.monotonic() < self.deadline, f"Smoke timeout at stage {self.stage}"
            if self.stage == 0 and self.overlay.words:
                word = next(w for w in self.overlay.words if w.lookup == "sword")
                x = (word.x + word.width / 2) * self.overlay.scale + self.overlay.offset[0]
                y = (word.y + word.height / 2) * self.overlay.scale + self.overlay.offset[1]
                self.overlay.click(self.overlay.canvas, SimpleNamespace(button=1, x=x, y=y))
                self.stage = 1
            elif self.stage == 1:
                result = getattr(self.overlay, "last_result", None)
                if result and result.saved:
                    assert len(result.senses) == 3, result
                    assert result.sentence == "Take the sword and follow the river.", result
                    assert result.translated_sentence == "Возьми меч и следуй вдоль реки.", result
                    assert not result.errors, result.errors
                    assert "Слово: sword" in self.config.vocabulary_path.read_text()
                    self.stage = 2
                    GLib.timeout_add(650, self.finish_overlay)
            return True
        except Exception as error:
            self.failure = error
            self.quit()
            return False

    def finish_overlay(self):
        try:
            if args.screenshot:
                args.screenshot.write_bytes(capture_output(monitor["name"]))
            self.overlay.loading("unbroken" * 100, "A <unknown> & a sword in this sentence.")
            article = "A very long article with definitions and examples.\n" * 600
            result = LookupResult(
                "unbroken" * 100,
                "A <unknown> & a sword in this sentence.",
                [Sense(article, "A dictionary with a very long title " * 30)],
                "Очень длинный перевод без пробелов " + "длинный" * 300,
                "Источник " * 100,
                complete=True,
                word_complete=True,
            )
            self.overlay.show_result(result)
            self.original_stress_result = result
            GLib.timeout_add(180, self.check_long_card)
        except Exception as error:
            self.failure = error
            self.quit()
        return False

    def check_long_card(self):
        try:
            card = self.overlay.card
            assert card.get_allocated_width() <= 720, card.get_allocated_width()
            assert card.get_allocated_height() <= 850, card.get_allocated_height()
            assert card.context_label.get_text() == "A <unknown> & a sword in this sentence."
            expanders = []

            def find_expanders(widget):
                if isinstance(widget, Gtk.Expander):
                    expanders.append(widget)
                if isinstance(widget, Gtk.Container):
                    for child in widget.get_children():
                        find_expanders(child)

            find_expanders(card.meanings_box)
            assert len(expanders) == 1
            expanders[0].set_expanded(True)
            GLib.timeout_add(180, self.check_expanded_card)
        except Exception as error:
            self.failure = error
            self.quit()
        return False

    def check_expanded_card(self):
        try:
            card = self.overlay.card
            assert card.get_allocated_width() <= 720, card.get_allocated_width()
            assert card.get_allocated_height() <= 850, card.get_allocated_height()
            if args.stress_screenshot:
                args.stress_screenshot.write_bytes(capture_output(monitor["name"]))
            error_result = LookupResult(
                "failed",
                "Network failure.",
                errors=["Слово: Сервис недоступен", "Предложение: Сервис недоступен"],
                complete=True,
                word_complete=True,
            )
            self.overlay.loading("failed", "Network failure.")
            self.overlay.show_result(error_result)
            assert card.retry_button.get_visible()
            assert card.error_box.get_visible()
            assert not card.spinner.get_visible()
            assert not card.copy_button.get_sensitive()
            assert not card.meanings_box.get_children()
            self.overlay.loading("new", "A new choice.")
            assert not card.retry_button.get_visible()
            assert not card.error_box.get_visible()
            assert card.spinner.get_visible()
            current = card.last_result
            self.lookup_finished(self.generation - 1, self.lookup_id, self.original_stress_result)
            assert card.last_result is current, "Stale session replaced the new choice"
            self.lookup_finished(self.generation, self.lookup_id - 1, self.original_stress_result)
            assert card.last_result is current, "Stale word replaced the new choice"
            self.overlay.on_key(None, SimpleNamespace(keyval=Gdk.KEY_Escape))
            assert self.overlay is None
            self.open_settings()
            assert self.settings.get_visible()
            GLib.timeout_add(200, self.finish_settings)
        except Exception as error:
            self.failure = error
            self.quit()
        return False

    def finish_settings(self):
        try:
            self.settings.destroy()
            assert self.settings is None
        except Exception as error:
            self.failure = error
        self.quit()
        return False


app = Smoke()
try:
    status = app.run(["waylookup-smoke", "daemon"])
    if app.failure:
        raise app.failure
    assert app.stage == 2
    print(
        "PASS: live screencopy, OCR, click hit-test, dictionary senses, HTTP translation, vocabulary, long/expanded article layout, error/retry/loading states, stale replies, Esc, settings"
    )
finally:
    server.shutdown()
    temporary.cleanup()
