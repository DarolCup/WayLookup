"""Download verified FreeDict StarDict releases and import them for offline lookup."""

from hashlib import sha512
from html.parser import HTMLParser
from pathlib import Path
import shutil
import tarfile
from tempfile import TemporaryDirectory
from urllib.request import urlopen

from .config import data_dir
from .dictionary import Dictionary, decode_definition

VERSION = "2025.11.23"
RELEASES = (
    (
        "eng-rus", "en", "ru",
        "ffbfbbe9817dc9a2a84fb8556d99ba73b5bc572eab18f91988029adfc2a791761"
        "6dd20d13c94a99871857a047ce2a47a3538bde545f97065eb7e531d7a6ef4b4",
    ),
    (
        "rus-eng", "ru", "en",
        "5ee14615b10463ce54efbe23f9998676c34be92a295351947471b8aa18077f0589"
        "7b6ed924dbe37bed941f8de04a276e48d598974d1b531b96c20567f72bf625",
    ),
    (
        "deu-rus", "de", "ru",
        "fe63a117ebdea9a38f4817c2ddc2ab31bde9d240f2632f16d76a846fe7251e3c75"
        "d04d2f48cc5f05d8a00f9b7c9ada2283b2a2d0dcdc558dfdc59b673f5cf6d1",
    ),
)


class WikDictArticle(HTMLParser):
    """Keep the translations and their explanatory gloss together."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.divs = []
        self.list_depth = 0
        self.group = 0
        self.groups = [([], [])]
        self.in_grammar = False
        self.after_grammar = False

    def handle_starttag(self, tag, attrs):
        if tag == "div":
            if self.divs:
                self.divs[-1][1] = True
            self.divs.append([[], False, False])
        elif tag in ("ol", "ul"):
            self.list_depth += 1
        elif tag == "li" and self.list_depth == 1:
            self.group = len(self.groups)
            self.groups.append(([], []))
        elif tag == "font" and dict(attrs).get("class") == "grammar":
            self.in_grammar = True
            if self.divs:
                self.divs[-1][2] = True

    def handle_endtag(self, tag):
        if tag == "div" and self.divs:
            parts, nested, grammar = self.divs.pop()
            if self.divs and not nested and not grammar:
                text = " ".join("".join(parts).split())
                if text:
                    self.groups[self.group][0].append(text)
        elif tag in ("ol", "ul"):
            self.list_depth -= 1
        elif tag == "li" and self.list_depth == 1:
            self.group = 0
        elif tag == "font" and self.in_grammar:
            self.in_grammar = False
            self.after_grammar = True

    def handle_data(self, data):
        if len(self.divs) > 1:
            self.divs[-1][0].append(data)
        elif self.after_grammar and not self.in_grammar:
            self.groups[self.group][1].append(data)


def wikdict_definitions(payload: bytes, sequence: str) -> list[str]:
    if sequence != "h":
        return [decode_definition(payload, sequence)]
    article = WikDictArticle()
    article.feed(payload.decode("utf-8"))
    result = []
    for terms, gloss_parts in article.groups:
        if terms:
            translation = ", ".join(dict.fromkeys(terms))
            gloss = " ".join(" ".join(gloss_parts).split()).strip(". ")
            result.append(translation + ("\n\n" + gloss if gloss else ""))
    return result or [decode_definition(payload, sequence)]


def install_defaults(dictionary: Dictionary) -> int:
    licenses = data_dir() / "dictionary-licenses"
    failed = False
    for pair, source, target, digest in RELEASES:
        book = f"FreeDict + WikDict ({source} → {target})"
        license_file = licenses / f"{pair}-{VERSION}.txt"
        if license_file.is_file() and dictionary.has_book(book, source, target):
            continue
        filename = f"freedict-{pair}-{VERSION}.stardict.tar.xz"
        url = f"https://download.freedict.org/dictionaries/{pair}/{VERSION}/{filename}"
        print(f"Загружаем {book}…", flush=True)
        try:
            with TemporaryDirectory(prefix="waylookup-dictionary-") as directory:
                directory = Path(directory)
                archive = directory / "download.tar.xz"
                with urlopen(url, timeout=20) as response, archive.open("wb") as stream:
                    shutil.copyfileobj(response, stream)
                if sha512(archive.read_bytes()).hexdigest() != digest:
                    raise ValueError("контрольная сумма архива не совпала")
                filenames = (f"{pair}.ifo", f"{pair}.idx.gz", f"{pair}.dict", "COPYING")
                with tarfile.open(archive, "r:xz") as source_archive:
                    for filename in filenames:
                        member = source_archive.getmember(f"{pair}/{filename}")
                        if not member.isfile():
                            raise ValueError(f"ожидался файл {filename}")
                        with (
                            source_archive.extractfile(member) as file,
                            (directory / filename).open("wb") as stream,
                        ):
                            shutil.copyfileobj(file, stream)
                count = dictionary.import_stardict(
                    directory / f"{pair}.ifo", source, target, book,
                    definitions_for=wikdict_definitions,
                )
                licenses.mkdir(parents=True, exist_ok=True)
                attribution = (directory / f"{pair}.ifo").read_text(encoding="utf-8")
                license_file.write_text(
                    attribution + "\n" + (directory / "COPYING").read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
                print(f"{book} · {count} слов")
        except (OSError, ValueError, KeyError, tarfile.TarError) as error:
            failed = True
            print(f"Не удалось установить {book}: {error}")
    return int(failed)
