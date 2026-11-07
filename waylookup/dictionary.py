"""User-owned JSON / StarDict dictionaries, imported transactionally to SQLite."""

from contextlib import contextmanager
from collections.abc import Callable
from dataclasses import dataclass
import gzip
from html.parser import HTMLParser
import json
from pathlib import Path
import sqlite3
import struct
import unicodedata

from .config import data_dir


@dataclass(frozen=True)
class Sense:
    text: str
    source: str


def normalize(word: str) -> str:
    return unicodedata.normalize("NFKC", word).casefold().replace("’", "'").strip()


def english_base_forms(word: str) -> list[str]:
    word = normalize(word)
    if not word.isascii() or not word.replace("'", "").isalpha() or len(word) < 4:
        return []
    if word.endswith("'s"):
        return [word[:-2]]
    if word.endswith(("ies", "ied")):
        return [word[:-3] + "y", word[:-1]]
    if word.endswith(("ing", "ed")):
        stem = word[:-3] if word.endswith("ing") else word[:-2]
        forms = [stem, stem + "e"]
        if len(stem) >= 3 and stem[-1] == stem[-2] and stem[-1] in "bdgmnprt":
            forms.insert(0, stem[:-1])
        return [form for form in forms if len(form) >= 3]
    if word.endswith(("shes", "ches", "sses", "xes", "zzes", "oes")):
        return [word[:-2], word[:-1]]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return [word[:-1]]
    return []


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in ("br", "p", "div", "li"):
            self.parts.append("\n")


def plain(content: str) -> str:
    parser = PlainText()
    parser.feed(content)
    return "".join(parser.parts).strip()


class Dictionary:
    def __init__(self, path: Path | None = None):
        self.path = path or data_dir() / "dictionaries.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS entries (source TEXT, target TEXT, book TEXT, word TEXT, meaning TEXT, UNIQUE(source,target,book,word,meaning))"
            )
            db.execute("CREATE INDEX IF NOT EXISTS lookup_word ON entries(source,target,word)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("PRAGMA busy_timeout=30000")
            with db:
                yield db
        finally:
            db.close()

    def lookup(self, word: str, source: str, target: str) -> list[Sense]:
        with self.connect() as db:
            return [
                Sense(meaning, book)
                for meaning, book in db.execute(
                    "SELECT meaning,book FROM entries WHERE source=? AND target=? AND word=? ORDER BY book,rowid LIMIT 30",
                    (source, target, normalize(word)),
                )
            ]

    def has_book(self, book: str, source: str, target: str) -> bool:
        with self.connect() as db:
            return db.execute(
                "SELECT 1 FROM entries WHERE source=? AND target=? AND book=? LIMIT 1",
                (source, target, book),
            ).fetchone() is not None

    def books(self) -> list[tuple]:
        with self.connect() as db:
            return db.execute(
                "SELECT book,source,target,count(DISTINCT word) FROM entries GROUP BY book,source,target"
            ).fetchall()

    def import_json(self, path: Path, source: str, target: str, name: str | None = None) -> int:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Словарь JSON должен содержать объект {слово: [значения]}")
        book = name or path.stem
        count = 0
        with self.connect() as db:
            db.execute(
                "DELETE FROM entries WHERE source=? AND target=? AND book=?", (source, target, book)
            )
            for word, meanings in data.items():
                if isinstance(meanings, str):
                    meanings = [meanings]
                if (
                    not word.strip()
                    or not isinstance(meanings, list)
                    or not meanings
                    or not all(isinstance(m, str) and m.strip() for m in meanings)
                ):
                    raise ValueError(f"Некорректные значения для {word!r}")
                for meaning in meanings:
                    db.execute(
                        "INSERT OR IGNORE INTO entries VALUES (?,?,?,?,?)",
                        (source, target, book, normalize(word), meaning.strip()),
                    )
                count += 1
        return count

    def import_stardict(
        self, path: Path, source: str, target: str, name: str | None = None,
        *, definitions_for: Callable[[bytes, str], list[str]] | None = None,
    ) -> int:
        if path.suffix != ".ifo":
            raise ValueError("Укажите файл .ifo словаря StarDict")
        metadata = dict(
            line.split("=", 1)
            for line in path.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
        book = name or metadata.get("bookname", path.stem)
        sequence = metadata.get("sametypesequence", "")
        if any(c not in "mlgtxykwh" for c in sequence):
            raise ValueError("Этот StarDict содержит неподдерживаемые бинарные поля")
        bits = int(metadata.get("idxoffsetbits", "32"))
        if bits not in (32, 64):
            raise ValueError("StarDict: idxoffsetbits должен быть 32 или 64")
        base = path.with_suffix("")
        index_path = Path(str(base) + ".idx")
        if not index_path.exists():
            index_path = Path(str(base) + ".idx.gz")
        dictionary_path = Path(str(base) + ".dict")
        if not dictionary_path.exists():
            dictionary_path = Path(str(base) + ".dict.dz")
        open_index = gzip.open if index_path.suffix == ".gz" else open
        open_dict = gzip.open if dictionary_path.suffix == ".dz" else open
        count = 0
        headwords = []
        # A malformed import rolls back, including deletion of the previous version.
        with (
            open_index(index_path, "rb") as index,
            open_dict(dictionary_path, "rb") as definitions,
            self.connect() as db,
        ):
            db.execute(
                "DELETE FROM entries WHERE source=? AND target=? AND book=?", (source, target, book)
            )
            while True:
                headword = bytearray()
                while True:
                    char = index.read(1)
                    if not char:
                        if headword:
                            raise ValueError("Повреждён индекс StarDict")
                        break
                    if char == b"\0":
                        break
                    headword.extend(char)
                    if len(headword) > 65536:
                        raise ValueError("Слишком длинное слово в StarDict")
                if not char:
                    break
                fmt = ">QI" if bits == 64 else ">II"
                size = struct.calcsize(fmt)
                record = index.read(size)
                if len(record) != size:
                    raise ValueError("Повреждён индекс StarDict")
                offset, length = struct.unpack(fmt, record)
                if length > 16 * 1024 * 1024:
                    raise ValueError("Слишком большая статья StarDict")
                definitions.seek(offset)
                payload = definitions.read(length)
                if len(payload) != length:
                    raise ValueError("Повреждены статьи StarDict")
                meanings = (
                    definitions_for(payload, sequence) if definitions_for
                    else [decode_definition(payload, sequence)]
                )
                word = normalize(headword.decode("utf-8"))
                headwords.append(word)
                db.executemany(
                    "INSERT OR IGNORE INTO entries VALUES (?,?,?,?,?)",
                    ((source, target, book, word, meaning) for meaning in meanings if meaning),
                )
                count += 1
            if metadata.get("wordcount") and count != int(metadata["wordcount"]):
                raise ValueError("Число статей не совпадает с wordcount в .ifo")
            synonyms_path = Path(str(base) + ".syn")
            if synonyms_path.exists():
                with synonyms_path.open("rb") as synonyms:
                    while True:
                        alias = bytearray()
                        while True:
                            char = synonyms.read(1)
                            if not char or char == b"\0":
                                break
                            alias.extend(char)
                            if len(alias) > 65536:
                                raise ValueError("Слишком длинный синоним StarDict")
                        if not char:
                            if alias:
                                raise ValueError("Повреждён файл синонимов StarDict")
                            break
                        record = synonyms.read(4)
                        if len(record) != 4:
                            raise ValueError("Повреждён файл синонимов StarDict")
                        entry = struct.unpack(">I", record)[0]
                        if entry >= len(headwords):
                            raise ValueError("Некорректная ссылка синонима StarDict")
                        db.execute(
                            "INSERT OR IGNORE INTO entries SELECT source,target,book,?,meaning FROM entries WHERE source=? AND target=? AND book=? AND word=?",
                            (
                                normalize(alias.decode("utf-8")),
                                source,
                                target,
                                book,
                                headwords[entry],
                            ),
                        )
        return count


def decode_definition(payload: bytes, sequence: str) -> str:
    pieces = []
    position = 0
    if sequence:
        for i, kind in enumerate(sequence):
            end = len(payload) if i == len(sequence) - 1 else payload.find(b"\0", position)
            if end < 0:
                raise ValueError("Повреждён разделитель статьи StarDict")
            value = payload[position:end].decode("utf-8")
            pieces.append(plain(value) if kind in "hgxk" else value)
            position = end + 1
    else:
        while position < len(payload):
            kind = chr(payload[position])
            position += 1
            if kind.isupper():
                if position + 4 > len(payload):
                    raise ValueError("Повреждено бинарное поле StarDict")
                size = struct.unpack(">I", payload[position : position + 4])[0]
                position += 4 + size
                if position > len(payload):
                    raise ValueError("Повреждено бинарное поле StarDict")
                continue
            end = payload.find(b"\0", position)
            if end < 0:
                raise ValueError("Повреждена статья StarDict")
            value = payload[position:end].decode("utf-8")
            if kind in "mlgtxykwh":
                pieces.append(plain(value) if kind in "hgxk" else value)
            position = end + 1
    return "\n".join(p.strip() for p in pieces if p.strip())
