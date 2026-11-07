"""Human-readable UTF-8 vocabulary, cross-process locked and deduplicated."""

from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path

from .presentation import clean_text, meaning_groups
from .translation import LookupResult


def format_lookup(result: LookupResult) -> str:
    sections = [clean_text(result.word, strip_markup=False)]
    groups = meaning_groups(result.senses)
    if groups:
        meanings = ["Значения"]
        for group in groups:
            meanings.append(f"  {group.source}")
            for number, meaning in enumerate(group.meanings, 1):
                lines = (meaning.article or meaning.summary).splitlines()
                meanings.append(f"    {number}. {lines[0]}")
                meanings.extend(f"       {line}" if line else "" for line in lines[1:])
        sections.append("\n".join(meanings))
    if result.sentence:
        sections.append("Оригинал\n" + clean_text(result.sentence, strip_markup=False))
    if result.translated_sentence:
        source = clean_text(result.sentence_source)
        heading = f"Перевод предложения · {source}" if source else "Перевод предложения"
        sections.append(f"{heading}\n{clean_text(result.translated_sentence)}")
    return "\n\n".join(sections)


def save_lookup(path: Path, result: LookupResult, source: str, target: str) -> bool:
    if not result.senses and not result.translated_sentence:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fingerprint = hashlib.sha256(
        json.dumps(
            [
                result.word.casefold(),
                source,
                target,
                result.sentence,
                [(s.text, s.source) for s in result.senses],
            ],
            ensure_ascii=False,
        ).encode()
    ).hexdigest()[:24]
    # Keep recognizing fingerprints written by older WayLookup versions.
    marker = f"[id:{fingerprint}]"
    body = format_lookup(result)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        history = stream.read()
        if marker in history or f"\n\n{body}\n\n" in history:
            return True
        date = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
        stream.write(
            f"{'─' * 48}\n{date} · {source.upper()} → {target.upper()}\n\n"
            f"{body}\n\n"
        )
        stream.flush()
        os.fsync(stream.fileno())
    return True
