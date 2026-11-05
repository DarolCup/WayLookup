"""Display formatting independent of GTK. Original translations remain available for export."""

from dataclasses import dataclass, field
import html
import re
import unicodedata

from .dictionary import Sense

# Strip recognised formatting only: comparisons such as "x < y" must stay readable.
HTML_TAG = re.compile(
    r"</?(?:html|body|div|p|br|span|b|strong|i|em|ul|ol|li|font|a|sup|sub)(?:\s[^>]*?)?\s*/?>",
    re.IGNORECASE,
)
BLOCK_TAG = re.compile(r"</?(?:div|p|br|li)\b[^>]*>", re.IGNORECASE)


def clean_text(value: str, strip_markup: bool = True) -> str:
    if strip_markup:
        value = HTML_TAG.sub("", BLOCK_TAG.sub("\n", html.unescape(value)))
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = "".join(c for c in value if c in "\n\t" or unicodedata.category(c) not in ("Cc", "Cs"))
    lines = [re.sub(r"[^\S\n]+", " ", line).strip() for line in value.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


@dataclass(frozen=True)
class DisplayMeaning:
    summary: str
    article: str = ""


@dataclass
class MeaningGroup:
    source: str
    meanings: list[DisplayMeaning] = field(default_factory=list)


def meaning_groups(senses: list[Sense]) -> list[MeaningGroup]:
    groups: dict[str, MeaningGroup] = {}
    seen: set[tuple[str, str]] = set()
    for sense in senses:
        text = clean_text(sense.text)
        source = " ".join(clean_text(sense.source).split()) or "Источник не указан"
        key = (source, " ".join(text.casefold().split()))
        if not text or key in seen:
            continue
        seen.add(key)
        # Long dictionary articles get an honest preview and a disclosure with the complete text.
        if len(text) > 280 or len(text.splitlines()) > 4:
            summary = text[:240]
            if len(text) > 240:
                boundary = summary.rfind(" ")
                if boundary > 160:
                    summary = summary[:boundary]
                summary = summary.rstrip() + "…"
            meaning = DisplayMeaning(summary, text)
        else:
            meaning = DisplayMeaning(text)
        groups.setdefault(source, MeaningGroup(source)).meanings.append(meaning)
    return list(groups.values())
