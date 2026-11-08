"""Tesseract TSV parsing, coordinates and sentence reconstruction without UI dependencies."""

import csv
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
import io
import logging
import os
import re
import struct
import subprocess
import unicodedata

from .config import Config

STRIP_OVERLAP = 48
# Tesseract is single-threaded per strip; more strips than this mostly adds model loads.
MAX_STRIPS = 4
MIN_STRIP_HEIGHT = 240
MIN_PARALLEL_PIXELS = 1_000_000
# The background sparse pass upscales small text up to this many pixels.
SPARSE_MAX_PIXELS = 12_000_000
SMALL_TEXT_HEIGHT = 18
LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Word:
    text: str
    x: int
    y: int
    width: int
    height: int
    confidence: float
    block: tuple[int, int]
    paragraph: int
    line: int

    @property
    def lookup(self) -> str:
        # Preserve apostrophes and internal hyphens, remove surrounding OCR punctuation.
        return re.sub(r"^[\W_]+|[\W_]+$", "", self.text, flags=re.UNICODE)

    def contains(self, x: float, y: float, padding: float = 3) -> bool:
        return (
            self.x - padding <= x <= self.x + self.width + padding
            and self.y - padding <= y <= self.y + self.height + padding
        )


def parse_tsv(content: str, confidence: float = 35) -> list[Word]:
    result = []
    for row in csv.DictReader(io.StringIO(content), delimiter="\t", quoting=csv.QUOTE_NONE):
        if row.get("level") != "5":
            continue
        try:
            text = row["text"].strip()
            score = float(row["conf"])
            word = Word(
                text,
                int(row["left"]),
                int(row["top"]),
                int(row["width"]),
                int(row["height"]),
                score,
                (int(row["page_num"]), int(row["block_num"])),
                int(row["par_num"]),
                int(row["line_num"]),
            )
            if (
                score >= confidence
                and word.width > 0
                and word.height > 0
                and any(c.isalnum() for c in text)
            ):
                result.append(word)
        except (KeyError, TypeError, ValueError):
            continue
    return result


def recognize_image(
    image: bytes, config: Config, single_thread=False, *, adaptive_threshold=False,
) -> list[Word]:
    options = {"env": {**os.environ, "OMP_THREAD_LIMIT": "1"}} if single_thread else {}
    threshold = ["-c", "thresholding_method=2"] if adaptive_threshold else []
    process = subprocess.run(
        [
            "tesseract",
            "stdin",
            "stdout",
            "-l",
            config.ocr_languages,
            "--psm",
            str(config.ocr_psm),
            *threshold,
            # Use the setting directly so installations without the tsv config file work.
            "-c",
            "tessedit_create_tsv=1",
        ],
        input=image,
        capture_output=True,
        timeout=60,
        **options,
    )
    if process.returncode:
        raise RuntimeError("Tesseract: " + process.stderr.decode("utf-8", "replace").strip()[:700])
    return parse_tsv(process.stdout.decode("utf-8", "replace"), config.min_confidence)


def png_size(png: bytes) -> tuple[int, int] | None:
    if len(png) >= 24 and png[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack_from(">II", png, 16)
    return None


def strip_count(width: int, height: int) -> int:
    if width * height < MIN_PARALLEL_PIXELS:
        return 1
    cpus = len(os.sched_getaffinity(0))
    return max(1, min(MAX_STRIPS, cpus, height // MIN_STRIP_HEIGHT))


def find_cuts(gray, count: int) -> list[int]:
    """Pick horizontal cut rows near equal fractions, preferring rows between text lines."""
    from PIL import Image, ImageFilter

    width, height = gray.size
    edges = gray.filter(ImageFilter.FIND_EDGES).crop((1, 1, width - 1, height - 1))
    # rows[index] is the mean edge strength of image row index + 1.
    rows = edges.resize((1, height - 2), Image.Resampling.BOX).tobytes()
    reach = max(1, height // (count * 4))
    cuts = []
    for index in range(1, count):
        target = height * index // count
        low = max(2, target - reach)
        high = min(height - 3, target + reach)
        # A cut through a text line is still safe: every strip overlaps its neighbours and
        # keeps only words whose centre lies inside it. Quiet rows simply keep layouts intact.
        cuts.append(min(
            range(low, high + 1),
            key=lambda row: (max(rows[row - 2:row + 1]), abs(row - target)),
        ))
    return cuts


def split_image(png: bytes, config: Config) -> list[tuple[bytes, int, int, int]] | None:
    """Return (image, offset, keep_from, keep_to) strips for parallel Tesseract runs."""
    gray = grayscale(png)
    if gray is None:
        return None
    return split_gray(gray, config)


def grayscale(png: bytes):
    if not png_size(png):
        return None
    try:
        from PIL import Image
    except ImportError:
        return None
    with Image.open(io.BytesIO(png)) as image:
        return image.convert("L")


def split_gray(gray, config: Config) -> list[tuple[bytes, int, int, int]] | None:
    if config.ocr_psm not in (3, 11):
        return None
    width, height = gray.size
    count = strip_count(width, height)
    if count < 2:
        return None
    bounds = [0, *find_cuts(gray, count), height]
    strips = []
    for keep_from, keep_to in zip(bounds, bounds[1:]):
        top = max(0, keep_from - STRIP_OVERLAP)
        bottom = min(height, keep_to + STRIP_OVERLAP)
        output = io.BytesIO()
        gray.crop((0, top, width, bottom)).save(output, "PPM")
        strips.append((output.getvalue(), top, keep_from, keep_to))
    return strips


def recognize_strips(
    strips: list[tuple[bytes, int, int, int]], config: Config, **options,
) -> list[Word]:
    with ThreadPoolExecutor(max_workers=len(strips) - 1) as workers:
        futures = [
            workers.submit(recognize_image, image, config, True, **options)
            for image, *_ in strips[:-1]
        ]
        last = recognize_image(strips[-1][0], config, True, **options)
        results = [future.result() for future in futures] + [last]
    words = []
    for index, ((_image, offset, keep_from, keep_to), found) in enumerate(zip(strips, results)):
        for word in found:
            centre = offset + word.y + word.height / 2
            if keep_from <= centre < keep_to:
                words.append(replace(
                    word, y=word.y + offset, block=(word.block[0] + index, word.block[1])
                ))
    return words


def recognize_primary(png: bytes, config: Config) -> list[Word]:
    strips = split_image(png, config)
    if not strips:
        return recognize_image(png, config)
    return recognize_strips(strips, config)


def sparse_scale(width: int, height: int, words: list[Word]) -> float:
    """Tesseract reads best around 20-30 px letters; enlarge screens with small text."""
    heights = sorted(word.height for word in words)
    if heights and heights[len(heights) // 4] >= SMALL_TEXT_HEIGHT:
        return 1.0
    scale = min(2.0, (SPARSE_MAX_PIXELS / max(1, width * height)) ** 0.5)
    return scale if scale >= 1.2 else 1.0


def unscale(words: list[Word], scale: float) -> list[Word]:
    if scale == 1:
        return words
    return [
        replace(
            word,
            x=round(word.x / scale),
            y=round(word.y / scale),
            width=max(1, round(word.width / scale)),
            height=max(1, round(word.height / scale)),
        )
        for word in words
    ]


def recognize_sparse(png: bytes, config: Config, scale: float = 1.0) -> list[Word]:
    sparse = replace(config, ocr_psm=11)
    gray = grayscale(png)
    if gray is None:
        return recognize_image(png, sparse)
    if sum(gray.histogram()[:128]) * 2 > gray.width * gray.height:
        from PIL import ImageOps

        gray = ImageOps.invert(gray)
    if scale != 1:
        from PIL import Image

        gray = gray.resize(
            (round(gray.width * scale), round(gray.height * scale)), Image.Resampling.LANCZOS
        )
    strips = split_gray(gray, sparse)
    if strips:
        return unscale(recognize_strips(strips, sparse, adaptive_threshold=True), scale)
    output = io.BytesIO()
    gray.save(output, "PPM")
    return unscale(recognize_image(output.getvalue(), sparse, adaptive_threshold=True), scale)


def line_key(word: Word) -> tuple:
    return word.block, word.paragraph, word.line


def merge_words(primary: list[Word], extra: list[Word]) -> list[Word]:
    """Add missed words while keeping the primary pass's paragraph and line order."""
    if not extra:
        return primary
    lines: dict[tuple, list[Word]] = {}
    for word in primary:
        lines.setdefault(line_key(word), []).append(word)
    primary_keys = set(lines)
    page_offset = max((word.block[0] for word in primary), default=0)
    affected = set()
    accepted = list(primary)
    for word in extra:
        anchor = None
        best_gap = float("inf")
        for other in accepted:
            vertical = min(word.y + word.height, other.y + other.height) - max(word.y, other.y)
            if vertical <= 0:
                continue
            horizontal = min(word.x + word.width, other.x + other.width) - max(word.x, other.x)
            area = min(word.width * word.height, other.width * other.height)
            if horizontal > 0 and horizontal * vertical >= area * 0.5:
                break
            if (
                vertical >= 0.6 * min(word.height, other.height)
                and max(word.height, other.height) <= 1.5 * min(word.height, other.height)
                and abs(horizontal) < min(best_gap, 1.25 * max(word.height, other.height))
            ):
                anchor, best_gap = other, abs(horizontal)
        else:
            if anchor:
                word = replace(word, block=anchor.block, paragraph=anchor.paragraph, line=anchor.line)
            else:
                word = replace(word, block=(word.block[0] + page_offset, word.block[1]))
            key = line_key(word)
            lines.setdefault(key, []).append(word)
            if key in primary_keys:
                affected.add(key)
            accepted.append(word)
    for key in affected:
        line = lines[key]
        direction = next(
            (
                kind for char in line[0].text
                if (kind := unicodedata.bidirectional(char)) in ("L", "R", "AL")
            ),
            "L",
        )
        line.sort(key=lambda word: word.x, reverse=direction != "L")
    return [word for line in lines.values() for word in line]


def split_candidate(left: Word, right: Word) -> tuple[str, str] | None:
    if line_key(left) != line_key(right):
        return None
    first, second = left.lookup, right.lookup
    if (
        len(first) + len(second) < 5
        or not first.isascii() or not second.isascii()
        or not first.isalpha() or not second.isalpha()
        or not left.text.endswith(first) or not right.text.startswith(second)
    ):
        return None
    gap = right.x - (left.x + left.width)
    height = min(left.height, right.height)
    overlap = min(left.y + left.height, right.y + right.height) - max(left.y, right.y)
    letter_width = (left.width + right.width) / (len(first) + len(second))
    if (
        not -1 <= gap <= max(2, letter_width * 1.05)
        or overlap < height * 0.55
        or max(left.height, right.height) > height * 1.6
    ):
        return None
    return first.casefold(), second.casefold()


def line_spacing(words: list[Word]) -> dict[tuple, float]:
    """Median space between words of each line with at least two real gaps."""
    gaps: dict[tuple, list[int]] = {}
    for left, right in zip(words, words[1:]):
        gap = right.x - (left.x + left.width)
        if line_key(left) == line_key(right) and gap > 0:
            gaps.setdefault(line_key(left), []).append(gap)
    return {key: sorted(values)[len(values) // 2] for key, values in gaps.items() if len(values) >= 2}


def should_join(
    pair: tuple[str, str], known: set[str], left: Word, right: Word, spacing: float | None,
) -> bool:
    gap = right.x - (left.x + left.width)
    if "".join(pair) in known:
        # "some" + "thing": both parts are words, so require a gap much tighter than a space.
        return not all(part in known for part in pair) or (
            spacing is not None and gap <= spacing * 0.45
        )
    # Names and jargon are missing from dictionaries: join only touching fragments.
    return (
        spacing is not None
        and gap <= max(1, spacing * 0.2)
        and not any(part in known for part in pair)
    )


def repair_split_words(
    words: list[Word], known_words: Callable[[set[str]], set[str]],
) -> list[Word]:
    candidates = {}
    terms = set()
    for index, (left, right) in enumerate(zip(words, words[1:])):
        pair = split_candidate(left, right)
        if pair:
            first, second = pair
            candidates[index] = (first, second)
            terms.update((first, second, first + second))
    if not candidates:
        return words
    known = known_words(terms)
    spacing = line_spacing(words)
    result = []
    skip = False
    for index, left in enumerate(words):
        if skip:
            skip = False
            continue
        pair = candidates.get(index)
        right = words[index + 1] if pair else None
        if pair and should_join(pair, known, left, right, spacing.get(line_key(left))):
            top = min(left.y, right.y)
            result.append(replace(
                left,
                text=left.text + right.text,
                y=top,
                width=right.x + right.width - left.x,
                height=max(left.y + left.height, right.y + right.height) - top,
                confidence=min(left.confidence, right.confidence),
            ))
            skip = True
        else:
            result.append(left)
    return result


def recognize(
    png: bytes, config: Config, *,
    on_primary: Callable[[list[Word]], None] | None = None,
    known_words: Callable[[set[str]], set[str]] | None = None,
) -> list[Word]:
    words = recognize_primary(png, config)
    if known_words and config.source == "en":
        words = repair_split_words(words, known_words)
    size = png_size(png)
    if not size or config.ocr_psm not in (3, 11):
        return words
    width, height = size
    if words and on_primary:
        on_primary(words)
    # The primary boxes are already visible. A sparse pass, enlarged for small text, then finds
    # headings, captions and other font sizes the page layout pass skipped.
    try:
        extra = recognize_sparse(png, config, scale=sparse_scale(width, height, words))
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        LOG.warning("Дополнительный OCR: %s", error)
        return words
    merged = merge_words(words, extra)
    if known_words and config.source == "en" and merged is not words:
        return repair_split_words(merged, known_words)
    return merged


ABBREVIATIONS = {
    "mr.", "mrs.", "ms.", "dr.", "prof.", "st.", "vs.", "e.g.", "i.e.", "etc.",
    "г.", "ул.", "рис.", "стр.", "им.", "т.", "д.", "т.д.", "т.п.",
}


def ends_sentence(word: str) -> bool:
    word = word.rstrip("\"'”’»)]}")
    return (
        bool(re.search(r"[.!?。！？]$", word))
        and word.lower() not in ABBREVIATIONS
        and not re.fullmatch(r"(?:[A-Za-zА-Яа-яЁё]\.)+", word)
    )


def adjacent_line(anchor, lines, before):
    x = min(word.x for word in anchor)
    right = max(word.x + word.width for word in anchor)
    top = min(word.y for word in anchor)
    bottom = max(word.y + word.height for word in anchor)
    height = bottom - top
    best = None
    for line in lines:
        other_top = min(word.y for word in line)
        other_bottom = max(word.y + word.height for word in line)
        gap = top - other_bottom if before else other_top - bottom
        if not 0 <= gap <= height:
            continue
        other_height = other_bottom - other_top
        if not height * 0.65 <= other_height <= height * 1.5:
            continue
        other_x = min(word.x for word in line)
        other_right = max(word.x + word.width for word in line)
        overlap = min(right, other_right) - max(x, other_x)
        alignment = min(abs(x - other_x), abs(x + right - other_x - other_right) / 2)
        if overlap < height or alignment > height:
            continue
        if best is None or gap < best[0]:
            best = gap, line
    return best[1] if best else []


def sentence_for(words: list[Word], selected: Word) -> str:
    group = [w for w in words if w.block == selected.block and w.paragraph == selected.paragraph]
    try:
        index = next(i for i, word in enumerate(group) if word is selected)
    except StopIteration:
        return selected.text
    start = index
    while start > 0 and not ends_sentence(group[start - 1].text):
        start -= 1
    end = index
    while end < len(group) - 1 and not ends_sentence(group[end].text):
        end += 1
    sentence = group[start:end + 1]
    if start == 0 or end == len(group) - 1:
        # OCR sometimes splits a wrapped sentence across paragraphs or screen strips.
        neighbors = {}
        for word in words:
            if (word.block, word.paragraph) != (selected.block, selected.paragraph):
                neighbors.setdefault((word.block, word.paragraph, word.line), []).append(word)
        if start == 0:
            anchor = [word for word in group if word.line == group[0].line]
            previous = adjacent_line(anchor, neighbors.values(), before=True)
            boundary = next(
                (i for i in range(len(previous) - 1, -1, -1) if ends_sentence(previous[i].text)),
                -1,
            )
            sentence = previous[boundary + 1:] + sentence
        if end == len(group) - 1 and not ends_sentence(group[end].text):
            anchor = [word for word in group if word.line == group[-1].line]
            following = adjacent_line(anchor, neighbors.values(), before=False)
            boundary = next(
                (i for i, word in enumerate(following) if ends_sentence(word.text)), len(following)
            )
            sentence += following[:boundary + 1]
    text = " ".join(w.text for w in sentence)
    text = re.sub(r"(?<=\w)(-|­)\s+(?=\w)", lambda match: "-" if match[1] == "-" else "", text)
    return re.sub(r"\s+([,.;:!?。！？])", r"\1", text)
