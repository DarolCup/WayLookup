import unittest

from waylookup.layout import (
    Rect, ease_out_cubic, phrase_ranges, place_card, selection_range,
)
from waylookup.ocr import Word, merge_words


def line(*texts, line_number=1, paragraph=1):
    words, x = [], 0
    for text in texts:
        words.append(Word(text, x, 20 * line_number, 10 * len(text), 16, 90, (1, 1), paragraph,
                          line_number))
        x += 10 * len(text) + 6
    return words


class PlacementTests(unittest.TestCase):
    AREA = (1920, 1080)

    def place(self, anchor, prefer=None):
        return place_card(anchor, 560, 300, *self.AREA, prefer=prefer)

    def test_card_goes_below_and_stays_on_screen_at_right_edge(self):
        x, y, side = self.place(Rect(1880, 100, 30, 18))
        self.assertEqual(side, "below")
        self.assertEqual(y, 130)
        self.assertEqual(x, 1920 - 16 - 560)

    def test_card_goes_above_word_at_bottom(self):
        x, y, side = self.place(Rect(900, 1000, 60, 18))
        self.assertEqual(side, "above")
        self.assertEqual(y, 1000 - 12 - 300)
        self.assertEqual(x, round(930 - 280))

    def test_card_goes_left_of_word_when_no_vertical_room(self):
        x, y, side = place_card(Rect(1700, 500, 60, 18), 560, 700, *self.AREA)
        self.assertEqual(side, "left")
        self.assertEqual(x, 1700 - 12 - 560)
        self.assertTrue(16 <= y <= 1080 - 16 - 700)

    def test_previous_side_is_kept_while_it_fits(self):
        anchor = Rect(900, 500, 60, 18)
        self.assertEqual(self.place(anchor)[2], "below")
        self.assertEqual(self.place(anchor, prefer="above")[2], "above")

    def test_huge_card_uses_roomier_side_and_is_clamped(self):
        x, y, side = place_card(Rect(900, 200, 60, 18), 1900, 2000, *self.AREA)
        self.assertEqual(side, "below")
        self.assertEqual((x, y), (16, 16))

    def test_easing_is_monotonic_and_bounded(self):
        values = [ease_out_cubic(step / 10) for step in range(-1, 12)]
        self.assertEqual(values[0], 0)
        self.assertEqual(values[-1], 1)
        self.assertEqual(values, sorted(values))


class PhraseTests(unittest.TestCase):
    def test_phrase_candidates_contain_clicked_word_longest_first(self):
        words = line("Oh,", "by", "the", "way", "hello")
        ranges = phrase_ranges(words, 2)
        texts = [text for *_span, text in ranges]
        self.assertIn("by the way", texts)
        self.assertNotIn("Oh by the", texts)  # The comma ends the phrase.
        self.assertEqual(len(ranges[0][2].split()), 4)
        self.assertTrue(all(first <= 2 <= last for first, last, _text in ranges))

    def test_phrase_does_not_cross_lines(self):
        words = line("look") + line("up", line_number=2)
        self.assertEqual(phrase_ranges(words, 0), [])

    def test_drag_selection_stays_in_paragraph_and_is_limited(self):
        words = line("a", "b", "c") + line("d", line_number=2) + line("e", paragraph=2)
        self.assertEqual(selection_range(words, 2, 0), (0, 2))
        self.assertEqual(selection_range(words, 1, 3), (1, 3))
        self.assertIsNone(selection_range(words, 0, 4))
        many = line(*"abcdefghij")
        self.assertIsNone(selection_range(many, 0, 9))


class MergeTests(unittest.TestCase):
    def test_shifted_duplicate_from_sparse_pass_is_dropped(self):
        hello = Word("hello", 100, 100, 50, 16, 90, (1, 1), 1, 1)
        shifted = Word("Hello", 101, 109, 50, 16, 80, (1, 1), 1, 1)  # 44% overlap
        taller = Word("hel1o", 99, 96, 52, 40, 80, (1, 1), 1, 1)
        below = Word("world", 100, 140, 50, 16, 80, (1, 1), 1, 1)
        merged = merge_words([hello], [shifted, taller, below])
        self.assertEqual([word.text for word in merged], ["hello", "world"])


if __name__ == "__main__":
    unittest.main()
