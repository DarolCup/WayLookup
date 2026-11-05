"""Human-readable UTF-8 vocabulary, cross-process locked and deduplicated."""

from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path

from .translation import LookupResult


def one_line(text: str) -> str:
    return " ".join(text.split())


def save_lookup(path: Path, result: LookupResult, source: str, target: str) -> bool:
    if not result.senses:
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
    # The ID is embedded in the same append, avoiding a second file that can diverge.
    marker = f"[id:{fingerprint}]"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        if any(marker in line for line in stream):
            return True
        lines = [
            f"{datetime.now().astimezone().isoformat(timespec='seconds')}  {source} → {target}  {marker}",
            f"Слово: {one_line(result.word)}",
        ]
        lines.extend(
            f"  {i}. {one_line(s.text)} [{one_line(s.source)}]"
            for i, s in enumerate(result.senses, 1)
        )
        lines.append(f"Контекст: {one_line(result.sentence)}")
        if result.translated_sentence:
            lines.append(f"Перевод: {one_line(result.translated_sentence)}")
        stream.write("\n".join(lines) + "\n\n")
        stream.flush()
        os.fsync(stream.fileno())
    return True
