"""Tesseract TSV parsing, coordinates and sentence reconstruction without UI dependencies."""

import csv
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
import io
import os
import re
import struct
import subprocess

from .config import Config

STRIP_OVERLAP = 48


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


def recognize_image(image: bytes, config: Config, single_thread=False) -> list[Word]:
    options = {"env": {**os.environ, "OMP_THREAD_LIMIT": "1"}} if single_thread else {}
    process = subprocess.run(
        [
            "tesseract",
            "stdin",
            "stdout",
            "-l",
            config.ocr_languages,
            "--psm",
            str(config.ocr_psm),
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


def split_image(png: bytes, config: Config) -> tuple[bytes, bytes, int] | None:
    if (
        config.ocr_psm not in (3, 11) or len(os.sched_getaffinity(0)) < 2
        or len(png) < 24 or png[:8] != b"\x89PNG\r\n\x1a\n"
    ):
        return None
    width, height = struct.unpack_from(">II", png, 16)
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


def recognize(png: bytes, config: Config) -> list[Word]:
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
