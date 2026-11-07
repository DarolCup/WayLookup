"""Tesseract TSV parsing, coordinates and sentence reconstruction without UI dependencies."""

import csv
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


def split_image(png: bytes, config: Config) -> tuple[bytes, bytes, int] | None:
    size = png_size(png)
    if not size or config.ocr_psm not in (3, 11) or len(os.sched_getaffinity(0)) < 2:
        return None
    width, height = size
    if width * height < 2_000_000 or height < 720:
        return None
    try:
        from PIL import Image, ImageFilter
    except ImportError:
        return None
    with Image.open(io.BytesIO(png)) as image:
        gray = image.convert("L")
    edges = gray.filter(ImageFilter.FIND_EDGES).crop((1, 1, width - 1, height - 1))
    rows = edges.resize((1, height - 2), Image.Resampling.BOX).tobytes()
    # Parallelize only dense frames with a quiet horizontal gutter near the middle.
    for half in (rows[:height // 2], rows[height // 2:]):
        if sum(value >= 3 for value in half) < len(half) // 4:
            return None
    middle = height // 2
    for cut in sorted(range(height * 2 // 5, height * 3 // 5), key=lambda row: abs(row - middle)):
        if max(rows[cut - 5:cut + 5]) <= 1:
            break
    else:
        return None
    parts = []
    for top, bottom in ((0, cut + STRIP_OVERLAP), (cut - STRIP_OVERLAP, height)):
        output = io.BytesIO()
        gray.crop((0, top, width, bottom)).save(output, "PPM")
        parts.append(output.getvalue())
    return parts[0], parts[1], cut


def recognize_primary(png: bytes, config: Config) -> list[Word]:
    parts = split_image(png, config)
    if parts is None:
        return recognize_image(png, config)
    top, bottom, cut = parts
    with ThreadPoolExecutor(max_workers=2) as workers:
        upper = workers.submit(recognize_image, top, config, True)
        lower = recognize_image(bottom, config, True)
    lower_offset = cut - STRIP_OVERLAP
    return [word for word in upper.result() if word.y + word.height / 2 < cut] + [
        replace(word, y=word.y + lower_offset, block=(word.block[0] + 1, word.block[1]))
        for word in lower if word.y + word.height / 2 >= STRIP_OVERLAP
    ]


def recognize_sparse(png: bytes, config: Config) -> list[Word]:
    sparse = replace(config, ocr_psm=11)
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return recognize_image(png, sparse)
    with Image.open(io.BytesIO(png)) as image:
        gray = image.convert("L")
    if sum(gray.histogram()[:128]) * 2 > gray.width * gray.height:
        gray = ImageOps.invert(gray)
    output = io.BytesIO()
    gray.save(output, "PPM")
    return recognize_image(output.getvalue(), sparse, adaptive_threshold=True)


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


def recognize(png: bytes, config: Config) -> list[Word]:
    words = recognize_primary(png, config)
    size = png_size(png)
    if not size or config.ocr_psm not in (3, 11):
        return words
    width, height = size
    # Sparse screens benefit from a second segmentation mode. Keep dense screens fast
    # unless the user explicitly selected the sparse-text mode.
    if config.ocr_psm == 3 and len(words) >= max(24, width * height // 35_000):
        return words
    try:
        extra = recognize_sparse(png, config)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        LOG.warning("Дополнительный OCR: %s", error)
        return words
    return merge_words(words, extra)


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
