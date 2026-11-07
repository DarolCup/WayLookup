import gzip
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from waylookup.config import Config, parse_hotkey
from waylookup.dictionary import Dictionary, Sense
from waylookup.default_dictionaries import wikdict_definitions
from waylookup.history import save_lookup
from waylookup.hyprland import Hotkey
from waylookup.ocr import STRIP_OVERLAP, Word, parse_tsv, recognize, sentence_for
from waylookup.translation import LookupResult, TranslationError, Translator, request_json


class OCRTests(unittest.TestCase):
    @patch("waylookup.ocr.subprocess.run")
    def test_recognize_requests_tsv_without_a_config_file(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout=b"level\ttext\n")
        self.assertEqual(recognize(b"image", Config()), [])
        self.assertEqual(run.call_args.args[0][-2:], ["-c", "tessedit_create_tsv=1"])

    def test_tsv_keeps_quotes_and_excludes_noise(self):
        header = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        rows = [
            f"5\t1\t2\t1\t1\t1\t10\t20\t30\t15\t{score}\t{text}\n"
            for score, text in [(94, '"Hello'), (10, "noise"), (98, "..."), (90, 'world!"')]
        ]
        words = parse_tsv(header + "".join(rows))
        self.assertEqual([w.text for w in words], ['"Hello', 'world!"'])
        self.assertEqual(words[0].lookup, "Hello")
        self.assertTrue(words[0].contains(12, 22))
        self.assertFalse(words[0].contains(200, 22))

    def test_sentence_wrap_abbreviation_and_blocks(self):
        words = [
            Word(text, i * 20, 20, 18, 12, 95, (1, 1), 1, 1 + i // 5)
            for i, text in enumerate(
                ["Hello.", "Dr.", "Stone", "found", "a", "rare", "sword!", "Leave", "now."]
            )
        ]
        words.append(Word("Unrelated", 0, 0, 20, 20, 95, (1, 2), 1, 1))
        self.assertEqual(sentence_for(words, words[5]), "Dr. Stone found a rare sword!")
        self.assertEqual(sentence_for(words, words[0]), "Hello.")
        self.assertEqual(sentence_for(words, words[8]), "Leave now.")

    def test_sentence_rejoins_wrap_without_joining_other_columns(self):
        first = Word("Cross", 30, 100, 50, 16, 95, (1, 1), 1, 1)
        selected = Word("the", 87, 100, 26, 16, 95, (1, 1), 1, 1)
        wrapped = Word("river.", 30, 122, 69, 16, 95, (2, 1), 1, 1)
        unrelated = Word("Settings", 270, 120, 80, 16, 95, (1, 2), 1, 1)
        self.assertEqual(sentence_for([first, selected, unrelated, wrapped], selected), "Cross the river.")
        self.assertEqual(sentence_for([first, selected, unrelated, wrapped], wrapped), "Cross the river.")

    @patch("waylookup.ocr.split_image", return_value=(b"top", b"bottom", 200))
    @patch("waylookup.ocr.recognize_image")
    def test_parallel_ocr_keeps_one_copy_and_restores_monitor_coordinates(self, ocr, _split):
        def recognize_strip(image, _config, _single_thread):
            if image == b"top":
                return [
                    Word("top", 50, 120, 30, 10, 95, (1, 1), 1, 1),
                    Word("bottom", 50, 210, 30, 10, 95, (1, 1), 1, 2),
                ]
            return [Word("bottom", 50, STRIP_OVERLAP + 10, 30, 10, 95, (1, 1), 1, 1)]

        ocr.side_effect = recognize_strip
        words = recognize(b"image", Config())
        self.assertEqual([(word.text, word.y) for word in words], [("top", 120), ("bottom", 210)])
        self.assertNotEqual(words[0].block, words[1].block)


class ConfigTests(unittest.TestCase):
    def test_valid_and_invalid_hotkeys(self):
        self.assertEqual(parse_hotkey("SUPER+SHIFT+T"), ("SUPER SHIFT", "T", 65))
        for hotkey in ["Q", "ALT+ALT+Q", "ALT+Q;exec", "bad+Q", "ALT+"]:
            with self.subTest(hotkey=hotkey), self.assertRaises(ValueError):
                parse_hotkey(hotkey)

    def test_invalid_settings_rejected(self):
        for values in [
            {"min_confidence": float("nan")},
            {"source": "en;sh"},
            {"register_hotkey": "false"},
            {"network_timeout": 0},
            {"ollama_url": "file:///etc/passwd"},
        ]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                Config(**values).validate()

    def test_atomic_roundtrip(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict("os.environ", {"XDG_CONFIG_HOME": temporary}),
        ):
            Config(hotkey="CTRL+F8").save()
            self.assertEqual(Config.load().hotkey, "CTRL+F8")


class DictionaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.dictionary = Dictionary(self.root / "dict.sqlite3")

    def tearDown(self):
        self.temporary.cleanup()

    def test_multiple_meanings_normalization_and_rollback(self):
        path = self.root / "test.json"
        path.write_text(json.dumps({"Sword": ["меч", "клинок"], "don't": "не делай"}))
        self.assertEqual(self.dictionary.import_json(path, "en", "ru"), 2)
        self.assertEqual(len(self.dictionary.lookup("SWORD", "en", "ru")), 2)
        self.assertEqual(self.dictionary.lookup("don’t", "en", "ru")[0].text, "не делай")
        self.assertEqual(self.dictionary.lookup("sword", "en", "de"), [])
        path.write_text('{"new": "новое", "invalid": 1}')
        with self.assertRaises(ValueError):
            self.dictionary.import_json(path, "en", "ru")
        self.assertEqual(len(self.dictionary.lookup("sword", "en", "ru")), 2)
        self.assertEqual(self.dictionary.lookup("new", "en", "ru"), [])

    def test_stardict_compressed_and_64bit_offsets(self):
        for bits in (32, 64):
            base = self.root / f"test{bits}"
            definition = "<b>меч</b><br>клинок".encode()
            base.with_suffix(".ifo").write_text(
                f"StarDict's dict ifo file\nversion=3.0.0\nbookname=Test{bits}\nwordcount=1\nidxoffsetbits={bits}\nsametypesequence=h\n"
            )
            with gzip.open(str(base) + ".dict.dz", "wb") as stream:
                stream.write(definition)
            with gzip.open(str(base) + ".idx.gz", "wb") as stream:
                stream.write(
                    b"sword\0" + struct.pack(">QI" if bits == 64 else ">II", 0, len(definition))
                )
            Path(str(base) + ".syn").write_bytes(b"swords\0" + struct.pack(">I", 0))
            self.assertEqual(
                self.dictionary.import_stardict(base.with_suffix(".ifo"), "en", "ru"), 1
            )
        self.assertEqual(
            [s.text for s in self.dictionary.lookup("sword", "en", "ru")], ["меч\nклинок"] * 2
        )
        self.assertEqual(
            [s.text for s in self.dictionary.lookup("swords", "en", "ru")], ["меч\nклинок"] * 2
        )


class HistoryTests(unittest.TestCase):
    def test_concurrent_dedup_and_failed_translations(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "words.txt"
            result = LookupResult("sword", "Take the sword.", [Sense("меч", "Test")], "Возьми меч.")
            workers = [
                threading.Thread(target=save_lookup, args=(path, result, "en", "ru"))
                for _ in range(6)
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            content = path.read_text()
            self.assertEqual(content.count("\n\nsword\n\n"), 1)
            self.assertIn("Значения\n  Test\n    1. меч\n\nОригинал\nTake the sword.", content)
            self.assertIn("\n\nПеревод предложения\nВозьми меч.\n", content)
            self.assertNotIn("[id:", content)
            self.assertFalse(save_lookup(path, LookupResult("bad", "bad"), "en", "ru"))

    def test_can_save_sentence_without_dictionary_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "words.txt"
            result = LookupResult("hello", "Hello there.", translated_sentence="Привет.")
            self.assertTrue(save_lookup(path, result, "en", "ru"))
            self.assertIn("Перевод предложения\nПривет.", path.read_text())


class TranslationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.translator = Translator(Dictionary(Path(self.temporary.name) / "dict.db"))

    def tearDown(self):
        self.temporary.cleanup()

    @patch("waylookup.translation.request_json")
    def test_mymemory_cache_and_quota(self, request):
        request.return_value = {
            "responseStatus": 200,
            "responseData": {"translatedText": "меч &amp; щит"},
        }
        self.assertEqual(self.translator.translate("sword", Config())[0].text, "меч & щит")
        self.translator.translate("sword", Config())
        self.assertEqual(request.call_count, 1)
        request.return_value = {"responseStatus": 429}
        with self.assertRaises(TranslationError):
            self.translator.translate("shield", Config())
        with self.assertRaises(TranslationError):
            self.translator.translate("я" * 251, Config())

    @patch("waylookup.translation.request_json")
    def test_libretranslate_and_ollama(self, request):
        request.return_value = {"translatedText": "Возьми меч."}
        self.translator.translate("Take the sword.", Config(sentence_provider="libretranslate"))
        self.assertEqual(request.call_args.args[1]["q"], "Take the sword.")
        request.return_value = {"message": {"content": '{"translation": "Возьми меч."}'}}
        result = self.translator.translate("Take the sword.", Config(sentence_provider="ollama"))
        self.assertEqual(result[0].text, "Возьми меч.")
        request.return_value = {"message": {"content": '{"meanings": ["меч", "клинок"]}'}}
        result = self.translator.word("sword", "Take the sword.", Config(word_mode="ai"))
        self.assertEqual(len(result), 2)
        request.return_value = {"message": {"content": "not JSON"}}
        with self.assertRaises(TranslationError):
            self.translator.ai_word("x", "x", Config())

    @patch("waylookup.translation.request_json")
    def test_explicit_google_word_provider_and_missing_ollama_do_not_fall_back(self, request):
        google = Config(word_mode="translator", word_provider="google", sentence_provider="off")
        request.return_value = {"data": {"translations": [{"translatedText": "меч"}]}}
        with patch.dict("os.environ", {"WAYLOOKUP_GOOGLE_API_KEY": "test-key"}):
            self.assertEqual(self.translator.word("sword", "Take the sword.", google)[0].text, "меч")
        self.assertEqual(request.call_args.args[1]["format"], "text")
        self.assertIn("translation.googleapis.com", request.call_args.args[0])
        ollama = Config(sentence_provider="ollama")
        request.return_value = {"message": {"content": '{"translation": "меч"}'}}
        self.translator.translate("sword", ollama)
        request.reset_mock()
        request.side_effect = TranslationError("Сервис недоступен")
        with self.assertRaisesRegex(TranslationError, "Ollama"):
            self.translator.translate("sword", ollama)
        self.assertEqual(request.call_count, 1)
        self.assertTrue(request.call_args.args[0].endswith("/api/chat"))

    def test_dictionary_forms_and_context_use_local_senses(self):
        payload = (
            '<div>/<font color="gray">bæŋk</font>/<br><div><font class="grammar">noun</font></div>'
            '<ol><li>A financial institution.<div>банк</div></li>'
            '<li>An edge of a river.<ol><li><div>берег</div></li><li><div>брег</div></li></ol>'
            '</li></ol></div>'
        ).encode()
        entries = wikdict_definitions(payload, "h")
        self.assertEqual(entries, ["банк\n\nA financial institution", "берег, брег\n\nAn edge of a river"])
        path = Path(self.temporary.name) / "dictionary.json"
        path.write_text(json.dumps({"bank": entries}), encoding="utf-8")
        self.translator.dictionary.import_json(path, "en", "ru", "FreeDict + WikDict (en → ru)")
        result = self.translator.word("banks", "Flooding covered both banks of the river.", Config())
        self.assertTrue(result[0].text.startswith("берег, брег\n"))
        self.assertTrue(result[0].source.endswith(" · bank"))

    @patch("waylookup.translation.request_json")
    def test_offline_does_not_send_requests(self, request):
        config = Config(sentence_provider="off", dictionary_fallback=False)
        self.assertEqual(self.translator.word("unknown", "unknown", config), [])
        self.assertEqual(self.translator.translate("unknown", config), [])
        request.assert_not_called()

    @patch("waylookup.translation.urlopen", side_effect=TimeoutError)
    def test_network_error_does_not_leak_url(self, _request):
        with self.assertRaises(TranslationError) as error:
            request_json("https://secret.example/?api_key=hidden")
        self.assertNotIn("hidden", str(error.exception))


class HotkeyTests(unittest.TestCase):
    @patch("waylookup.hyprland.hypr")
    def test_conflicts_never_remove_existing_user_bind(self, hypr):
        hypr.side_effect = lambda *args: (
            "Hyprland 0.57.0"
            if args == ("version",)
            else json.dumps([{"modmask": 8, "key": "Q", "description": "User action"}])
        )
        hotkey = Hotkey()
        with self.assertRaises(RuntimeError):
            hotkey.register("ALT+Q")
        self.assertFalse(
            any("eval" in call.args or "keyword" in call.args for call in hypr.call_args_list)
        )


if __name__ == "__main__":
    unittest.main()
