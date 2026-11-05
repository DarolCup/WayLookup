"""CLI entry points; dictionary tools and doctor work without a running display."""

import argparse
import importlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys

from . import __version__
from .config import Config, config_path


def doctor() -> int:
    failures = 0
    checks = []
    for program in ("hyprctl", "grim", "tesseract"):
        checks.append(
            (bool(shutil.which(program)), program, shutil.which(program) or "не установлен")
        )
    for namespace, version in (
        ("Gtk", "3.0"),
        ("GtkLayerShell", "0.1"),
        ("AyatanaAppIndicator3", "0.1"),
    ):
        try:
            import gi

            gi.require_version(namespace, version)
            importlib.import_module("gi.repository." + namespace)
            checks.append((True, namespace, version))
        except (ImportError, ValueError) as error:
            checks.append((False, namespace, str(error)))
    checks.append(
        (
            bool(os.environ.get("WAYLAND_DISPLAY")),
            "Wayland",
            os.environ.get("WAYLAND_DISPLAY", "нет сессии"),
        )
    )
    checks.append(
        (
            bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")),
            "Hyprland",
            "сессия" if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") else "нет сессии",
        )
    )
    try:
        config = Config.load()
        if shutil.which("tesseract"):
            process = subprocess.run(
                ["tesseract", "--list-langs"], capture_output=True, text=True, timeout=5
            )
            languages = set(process.stdout.splitlines()[1:])
            missing = set(config.ocr_languages.split("+")) - languages
            checks.append(
                (
                    not missing,
                    "OCR-языки",
                    ", ".join(sorted(missing)) + " отсутствуют"
                    if missing
                    else config.ocr_languages,
                )
            )
        checks.append((True, "Конфигурация", str(config_path())))
        checks.append((True, "Файл слов", str(config.vocabulary_path)))
    except (ValueError, OSError) as error:
        checks.append((False, "Конфигурация", str(error)))
    for success, name, detail in checks:
        print(f"{'OK' if success else 'FAIL'}  {name}: {detail}")
        failures += not success
    return int(bool(failures))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="WayLookup — перевод слов на экране под Hyprland")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command")
    for command, help_text in (
        ("daemon", "Запустить демон с tray"),
        ("capture", "Открыть/закрыть перевод экрана"),
        ("settings", "Открыть настройки"),
        ("quit", "Завершить демон"),
        ("doctor", "Проверить зависимости"),
    ):
        commands.add_parser(command, help=help_text)
    config_parser = commands.add_parser(
        "config", help="Показать конфигурацию или создать файл по умолчанию"
    )
    config_parser.add_argument("--init", action="store_true")
    dictionary_parser = commands.add_parser(
        "dictionary", help="Импортировать или перечислить офлайн-словари"
    )
    dictionary_commands = dictionary_parser.add_subparsers(dest="dictionary_command", required=True)
    dictionary_commands.add_parser("list")
    importer = dictionary_commands.add_parser("import")
    importer.add_argument("file", type=Path, help=".ifo StarDict или .json")
    importer.add_argument("--source", default="en")
    importer.add_argument("--target", default="ru")
    importer.add_argument("--name")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        if args.command == "doctor":
            return doctor()
        if args.command == "config":
            config = Config.load()
            if args.init and not config_path().exists():
                config.save()
            from dataclasses import asdict

            print(json.dumps(asdict(config), ensure_ascii=False, indent=2))
            print(f"\nФайл: {config_path()}")
            return 0
        if args.command == "dictionary":
            from .dictionary import Dictionary

            dictionary = Dictionary()
            if args.dictionary_command == "list":
                for book, source, target, count in dictionary.books():
                    print(f"{book}\t{source} → {target}\t{count} слов")
            else:
                Config(source=args.source, target=args.target).validate()
                method = (
                    dictionary.import_stardict
                    if args.file.suffix == ".ifo"
                    else dictionary.import_json
                )
                count = method(args.file, args.source, args.target, args.name)
                print(f"Импортировано {count} слов")
            return 0
        os.environ.setdefault("GDK_BACKEND", "wayland")
        if not os.environ.get("WAYLAND_DISPLAY"):
            parser.error("Нужна Wayland-сессия Hyprland. Проверьте waylookup doctor")
        from .ui import Application, GtkLayerShell

        if not GtkLayerShell.is_supported():
            parser.error("Композитор не поддерживает layer-shell")
        app = Application()
        return app.run(["waylookup", args.command or "daemon"])
    except (OSError, ValueError, RuntimeError, ImportError) as error:
        print(f"WayLookup: {error}", file=sys.stderr)
        return 1
