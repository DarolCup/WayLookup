"""Overlay geometry, phrase spans and easing without GTK dependencies."""

from dataclasses import dataclass

MAX_PHRASE_WORDS = 4
MAX_SELECTION_WORDS = 8


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height


def clamp(value: float, low: float, high: float) -> float:
    return low if high < low else max(low, min(value, high))


def place_card(
    anchor: Rect, width: float, height: float, area_width: float, area_height: float,
    prefer: str | None = None, gap: float = 12, margin: float = 16,
) -> tuple[int, int, str]:
    """Place a card beside the selection without covering it.

    Below and above keep the card centred on the word and slide it sideways to stay on
    screen. If neither fits, the card goes to the right or left of the word. The previous
    side is kept while it still fits, so a growing card does not jump around.
    """
    centre_x = clamp(anchor.x + anchor.width / 2 - width / 2, margin, area_width - margin - width)
    centre_y = clamp(
        anchor.y + anchor.height / 2 - height / 2, margin, area_height - margin - height
    )
    room = {
        "below": area_height - margin - anchor.bottom - gap,
        "above": anchor.y - margin - gap,
        "right": area_width - margin - anchor.right - gap,
        "left": anchor.x - margin - gap,
    }
    positions = {
        "below": (centre_x, anchor.bottom + gap),
        "above": (centre_x, anchor.y - gap - height),
        "right": (anchor.right + gap, centre_y),
        "left": (anchor.x - gap - width, centre_y),
    }
    needed = {"below": height, "above": height, "right": width, "left": width}
    order = ["below", "above", "right", "left"]
    if prefer in order:
        order.remove(prefer)
        order.insert(0, prefer)
    for side in order:
        if needed[side] <= room[side]:
            x, y = positions[side]
            return round(x), round(y), side
    # Nothing fits completely: use the roomier vertical side, the card body scrolls.
    side = "below" if room["below"] >= room["above"] else "above"
    x, y = positions[side]
    return round(x), round(clamp(y, margin, area_height - margin - height)), side


def ease_out_cubic(progress: float) -> float:
    progress = clamp(progress, 0.0, 1.0)
    return 1 - (1 - progress) ** 3


def line_of(word) -> tuple:
    return word.block, word.paragraph, word.line


def joinable(left, right) -> bool:
    """Adjacent words of one line without punctuation between them."""
    return (
        line_of(left) == line_of(right)
        and bool(left.lookup) and bool(right.lookup)
        and left.text.endswith(left.lookup)
        and right.text.startswith(right.lookup)
    )


def phrase_ranges(words, index: int, max_words: int = MAX_PHRASE_WORDS):
    """Candidate multi-word expressions containing words[index], longest first."""
    start = index
    while start > 0 and index - start < max_words - 1 and joinable(words[start - 1], words[start]):
        start -= 1
    end = index
    while (
        end + 1 < len(words)
        and end - index < max_words - 1
        and joinable(words[end], words[end + 1])
    ):
        end += 1
    ranges = []
    for first in range(start, index + 1):
        for last in range(max(index, first + 1), min(end, first + max_words - 1) + 1):
            ranges.append((first, last, " ".join(w.lookup for w in words[first:last + 1])))
    ranges.sort(key=lambda item: (item[0] - item[1], item[0]))
    return ranges


def selection_range(words, anchor: int, current: int) -> tuple[int, int] | None:
    """Words between two dragged words of one paragraph, in reading order."""
    first, last = sorted((anchor, current))
    if last - first + 1 > MAX_SELECTION_WORDS:
        return None
    group = words[anchor].block, words[anchor].paragraph
    if any((word.block, word.paragraph) != group for word in words[first:last + 1]):
        return None
    return first, last
