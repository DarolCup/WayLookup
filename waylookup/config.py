"""Validated XDG configuration. No credentials are written to the config."""

from dataclasses import asdict, dataclass, fields
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit


def config_path() -> Path:
    return (
        Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "waylookup/config.json"
    )


def data_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "waylookup"


MODIFIERS = {"SHIFT": 1, "CTRL": 4, "ALT": 8, "SUPER": 64}


def parse_hotkey(value: str) -> tuple[str, str, int]:
    parts = [p.strip() for p in value.upper().split("+")]
    if len(parts) < 2 or not all(p in MODIFIERS for p in parts[:-1]):
        raise ValueError("Бинд: ALT+Q, SUPER+SHIFT+T или CTRL+F8")
    if len(set(parts[:-1])) != len(parts[:-1]) or not re.fullmatch(r"[A-Z0-9_]+", parts[-1]):
        raise ValueError("Некорректная клавиша или повторяющийся модификатор")
    return " ".join(parts[:-1]), parts[-1], sum(MODIFIERS[p] for p in parts[:-1])


@dataclass(frozen=True)
class Config:
    hotkey: str = "ALT+Q"
    source: str = "en"
    target: str = "ru"
    ocr_languages: str = "eng"
    ocr_psm: int = 3
    min_confidence: float = 35.0
    word_mode: str = "dictionary"
    sentence_provider: str = "mymemory"
    dictionary_fallback: bool = True
    libretranslate_url: str = "http://127.0.0.1:5000"
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:8b"
    network_timeout: int = 20
    history_file: str = ""
    register_hotkey: bool = True

    @property
    def vocabulary_path(self) -> Path:
        return (
            Path(self.history_file).expanduser().absolute()
            if self.history_file
            else data_dir() / "words.txt"
        )

    def validate(self) -> "Config":
        defaults = Config()
        for field in fields(self):
            value, default = getattr(self, field.name), getattr(defaults, field.name)
            if isinstance(default, float):
                valid = type(value) in (int, float)
            else:
                valid = type(value) is type(default)
            if not valid:
                raise ValueError(f"Некорректный тип настройки: {field.name}")
        parse_hotkey(self.hotkey)
        for code in (self.source, self.target):
            if not re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z]{2,4})?", code):
                raise ValueError("Укажите код языка, например en или ru; auto не поддерживается")
        if not re.fullmatch(r"[A-Za-z0-9_]+(?:\+[A-Za-z0-9_]+)*", self.ocr_languages):
            raise ValueError("Языки Tesseract: eng или eng+rus")
        if self.ocr_psm not in (3, 6, 11, 12) or not 0 <= self.min_confidence <= 100:
            raise ValueError("PSM: 3, 6, 11, 12; уверенность OCR: 0–100")
        if self.word_mode not in ("dictionary", "translator", "ai"):
            raise ValueError("Неизвестный режим слова")
        if self.sentence_provider not in ("mymemory", "libretranslate", "ollama", "off"):
            raise ValueError("Неизвестный переводчик предложения")
        if not 1 <= self.network_timeout <= 180:
            raise ValueError("Таймаут: от 1 до 180 секунд")
        for url in (self.libretranslate_url, self.ollama_url):
            parsed = urlsplit(url)
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError("Адрес сервиса: http(s)://host:port без пароля в URL")
        if not self.ollama_model.strip():
            raise ValueError("Укажите модель Ollama")
        return self

    @classmethod
    def load(cls) -> "Config":
        path = config_path()
        if not path.exists():
            return cls()
        content = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(content, dict):
            raise ValueError("Конфигурация должна быть JSON-объектом")
        unknown = set(content) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"Неизвестные настройки: {', '.join(sorted(unknown))}")
        return cls(**content).validate()

    def save(self) -> None:
        self.validate()
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=path.parent, prefix=".config-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(asdict(self), stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
