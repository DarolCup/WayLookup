"""Tesseract TSV parsing, coordinates and sentence reconstruction without UI dependencies."""

import csv
from dataclasses import dataclass
import io
import re
import subprocess

from .config import Config


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


def recognize(png: bytes, config: Config) -> list[Word]:
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
        input=png,
        capture_output=True,
        timeout=60,
    )
    if process.returncode:
        raise RuntimeError("Tesseract: " + process.stderr.decode("utf-8", "replace").strip()[:700])
    return parse_tsv(process.stdout.decode("utf-8", "replace"), config.min_confidence)


ABBREVIATIONS = {"mr.", "mrs.", "ms.", "dr.", "prof.", "st.", "vs.", "e.g.", "i.e.", "etc."}


def ends_sentence(word: str) -> bool:
    word = word.rstrip("\"'”’»)]}")
    return (
        bool(re.search(r"[.!?。！？]$", word))
        and word.lower() not in ABBREVIATIONS
        and not re.fullmatch(r"[A-Za-z]\.", word)
    )


def sentence_for(words: list[Word], selected: Word) -> str:
    # Tesseract's block/paragraph boundaries prevent joining unrelated UI panels.
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
    text = " ".join(w.text for w in group[start : end + 1])
    return re.sub(r"\s+([,.;:!?。！？])", r"\1", text)
