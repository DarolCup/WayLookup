"""Regression coverage for malformed provider responses and oversized dictionary articles."""

import unittest
from unittest.mock import patch

from waylookup.config import Config
from waylookup.dictionary import Sense
from waylookup.presentation import clean_text, meaning_groups
from waylookup.translation import TranslationError, Translator, required_text


class PresentationTests(unittest.TestCase):
    def test_html_entities_control_characters_and_paragraphs(self):
        self.assertEqual(
            clean_text("<p>меч&nbsp; &amp; щит</p>\r\n\n\n<b>клинок</b>\x00"), "меч & щит\n\nклинок"
        )
        self.assertEqual(clean_text("x < y; a > b"), "x < y; a > b")

    def test_original_screen_text_keeps_literal_markup(self):
        self.assertEqual(clean_text("<b>word</b> &amp;", strip_markup=False), "<b>word</b> &amp;")

    def test_sources_grouped_duplicates_removed_without_merging_dictionaries(self):
        groups = meaning_groups(
            [
                Sense("меч", "A"),
                Sense(" МЕЧ ", "A"),
                Sense("клинок", "A"),
                Sense("меч", "B"),
                Sense(" ", "A"),
            ]
        )
        self.assertEqual([g.source for g in groups], ["A", "B"])
        self.assertEqual([m.summary for m in groups[0].meanings], ["меч", "клинок"])
        self.assertEqual(len(groups[1].meanings), 1)

    def test_long_articles_have_bounded_preview_and_complete_original(self):
        article = "Толкование и примеры употребления. " * 500
        meaning = meaning_groups([Sense(article, "Dictionary")])[0].meanings[0]
        self.assertLessEqual(len(meaning.summary), 241)
        self.assertTrue(meaning.summary.endswith("…"))
        self.assertEqual(meaning.article, article.strip())

    def test_provider_text_is_clean_and_empty_html_is_not_a_translation(self):
        self.assertEqual(required_text("<p>Возьми меч.</p>", "test"), "Возьми меч.")
        with self.assertRaises(TranslationError):
            required_text("<p> </p>", "test")

    @patch("waylookup.translation.request_json")
    def test_mymemory_formatting_group_is_removed_from_word_translation(self, request):
        for translated in (
            '<g id="Documents.NoBold">Английский язык</g>',
            '&lt;g id=&quot;Documents.NoBold&quot;&gt;Английский язык&lt;/g&gt;',
        ):
            with self.subTest(translated=translated):
                request.return_value = {
                    "responseStatus": 200,
                    "responseData": {"translatedText": translated},
                }
                senses = Translator(None).translate("English", Config())
                self.assertEqual(senses, [Sense("Английский язык", "MyMemory · переводчик")])

    @patch("waylookup.translation.request_json")
    def test_malformed_mymemory_envelope_becomes_readable_error(self, request):
        request.return_value = {"responseStatus": 200, "responseData": None}
        with self.assertRaisesRegex(TranslationError, "некорректный ответ"):
            Translator(None).translate("word", Config())
