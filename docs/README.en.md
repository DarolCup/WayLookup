# WayLookup

[Русский README](../README.md)

Click a highlighted word to translate the word and its sentence. WayLookup runs on Wayland with Hyprland on Arch Linux, or KDE Plasma and GNOME on Fedora. The current interface is in Russian and the default translation direction is English → Russian.

![WayLookup overlay](screenshot.png)

Start a capture, click a highlighted word, read its dictionary meanings and sentence translation at the top centre of the screen, then press **Esc**. On Hyprland, the default shortcut is **Alt+Q**. A tray menu provides capture, settings, vocabulary and exit on desktops with a system tray.

This is an early independent project, not affiliated with Lookupper. The screenshot uses a synthetic scene and a local test server; it demonstrates the interface rather than translation quality.

## Fedora KDE Plasma and GNOME

### Quick install and update

Run this in a Wayland session to install or update WayLookup.

```bash
curl -fsSL https://raw.githubusercontent.com/DarolCup/WayLookup/main/install-fedora.sh | bash
```

The installer installs dependencies using `sudo dnf`, starts the user service and enables it at login. Open **WayLookup** from the application menu for settings. Run `~/.local/bin/waylookup doctor` to check dependencies.

- **GNOME.** Press **Alt+Q**. On the first capture, choose a screen, leave **Remember this selection** checked and click **Share**. WayLookup reuses the screen while the portal retains the permission. Use **Choose monitor** in WayLookup settings to switch screens. Choose a different shortcut in settings if Alt+Q is taken.
- **KDE Plasma.** Press **Alt+Q** while WayLookup is running. If the shortcut is taken, choose another one in WayLookup settings. To use a custom Plasma shortcut, disable automatic registration in WayLookup settings and bind `/home/YOUR_USER/.local/bin/waylookup capture` in System Settings.

### Build from source

```bash
git clone https://github.com/DarolCup/WayLookup.git
cd WayLookup
./install-fedora.sh --deps-only
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install build
.venv/bin/python -m build
.venv/bin/python -m pip install dist/waylookup-*.whl
.venv/bin/waylookup doctor
.venv/bin/waylookup daemon
```

For development, run `./scripts/waylookup daemon` from the checkout after installing the dependencies. Outside a graphical session, pass `--desktop plasma` or `--desktop gnome`. Launch the virtual environment build manually.

The quick installer places the launcher in `~/.local/bin`, application files in `~/.local/lib/waylookup/`, and the user unit in `~/.config/systemd/user/waylookup.service`. Settings and capture permission are stored in `~/.config/waylookup/`. Dictionaries and vocabulary are stored in `~/.local/share/waylookup/`. XDG overrides apply to the configuration and data paths.

### Uninstall

Remove the application, launcher, autostart service and managed GNOME shortcut.

```bash
curl -fsSL https://raw.githubusercontent.com/DarolCup/WayLookup/main/install-fedora.sh |
  bash -s -- --uninstall
```

Settings, dictionaries and vocabulary are kept by default. Add `--purge` after `--uninstall` to delete them too. For a source build, run `.venv/bin/waylookup quit` first. On GNOME, also run `.venv/bin/waylookup shortcut --remove`. Then uninstall the wheel with `.venv/bin/python -m pip uninstall waylookup`.

## Install on Arch Linux

Download `waylookup-0.1.1-1-any.pkg.tar.zst` and `SHA256SUMS` from [Releases](https://github.com/DarolCup/WayLookup/releases). In the download directory:

```bash
sha256sum --ignore-missing --check SHA256SUMS && sudo pacman -U ./waylookup-0.1.1-1-any.pkg.tar.zst
waylookup doctor
waylookup daemon
```

`pacman` installs the system dependencies. Run the application as your normal user inside Hyprland. Enable the tray module in your panel. SHA256 checks detect corruption relative to the published list; they are not an author signature. This package is not in the official Arch repositories or AUR.

## Run from source

```bash
sudo pacman -S --needed git python python-gobject python-cairo gtk3 gtk-layer-shell \
  grim tesseract tesseract-data-eng libayatana-appindicator
git clone https://github.com/DarolCup/WayLookup.git
cd WayLookup
python -m waylookup doctor
python -m waylookup daemon
```

You can also use **Code → Download ZIP**, unpack it and run the last two commands inside the extracted directory. `python -m waylookup` requires the project directory unless the package has been installed. To run a checkout from any directory, use the absolute path to `scripts/waylookup`.

The system Python can use Arch's PyGObject packages directly. If using a virtual environment, create it with `--system-site-packages`.

## Translation and dictionaries

- The overlay displays highlights from the first OCR pass while a second pass recovers sparse text. Dense frames with a suitable horizontal gap can run OCR in two parallel strips. Screenshots stay local.
- For game interfaces with scattered labels, choose **11 · Games and sparse text** in OCR settings. The second pass uses local contrast when Pillow is available. WayLookup checks close English OCR fragments against the local dictionary to recover split words.
- StarDict (`.ifo`, `.idx`/`.idx.gz`, `.dict`/`.dict.dz`, optional `.syn`) and JSON dictionaries, indexed locally in SQLite.
- MyMemory is the default sentence provider and has a 500-byte request limit. Word and sentence providers can be selected independently.
- Google Cloud Translation uses `WAYLOOKUP_GOOGLE_API_KEY`. For the user service, put the variable in `~/.config/waylookup/providers.env` and restart `waylookup.service`. [Google Cloud Translation API](https://docs.cloud.google.com/translate/docs/reference/rest/v2/translate)
- LibreTranslate supports a self-hosted server; its optional key is read from `WAYLOOKUP_LIBRETRANSLATE_KEY`.
- Ollama supports local AI translation and contextual word meanings. Install and start Ollama, download your own model, and select its exact name in settings.

The Fedora installer imports English–Russian, Russian–English and German–Russian FreeDict + WikDict dictionaries. For a manual installation, run `waylookup dictionary install-defaults`. For another pair, download a StarDict archive, unpack it and import its `.ifo`. [FreeDict downloads](https://freedict.org/downloads/)

```bash
python -m waylookup dictionary import /path/to/dictionary.ifo --source en --target ru
python -m waylookup dictionary list
```

For a small demo with eleven words:

```bash
python -m waylookup dictionary import examples/en-ru.json --source en --target ru --name Demo
```

The Arch package provides that demo under `/usr/share/waylookup/examples/en-ru.json`. Offline lookup prefers exact matches and checks common English inflections when needed. FreeDict meanings are reordered when their glosses share words with the selected sentence.

For fully offline use, select a local dictionary and disable sentence translation and dictionary fallback, or configure a local Ollama server.

## Configuration and vocabulary

```bash
waylookup settings
waylookup capture
waylookup quit
```

For a checkout, prefix commands with `python -m` while inside the project directory. Configuration is stored in `~/.config/waylookup/config.json`. Click **Save** in the translation card to add a vocabulary entry to `~/.local/share/waylookup/words.txt`. Entries have separate sections for meanings, the original sentence and its translation. The vocabulary path is configurable. XDG directory overrides are supported.

The GUI applies changes immediately. Restart after editing JSON manually. On Hyprland, the WayLookup settings accept shortcuts such as `ALT+Q`, `SUPER+SHIFT+T` and `CTRL+F8`, and report existing shortcut conflicts. On Fedora, the desktop controls the shortcut.

## Autostart

For a Lua Hyprland configuration:

```lua
hl.on("hyprland.start", function()
    hl.exec_cmd("waylookup daemon")
end)
```

For an older Hyprlang configuration:

```ini
exec-once = waylookup daemon
```

For source checkouts, use the absolute path to `scripts/waylookup daemon`. The application registers its own temporary shortcut and restores it after configuration reloads. It does not edit your Hyprland configuration.

A user service is included for sessions that correctly manage `graphical-session.target`, such as a configured UWSM session. Choose either the service or Hyprland autostart, not both. See the main README for environment requirements.

## Limits and validation

The live overlay is supported on Plasma and Hyprland with Layer Shell and on GNOME with one monitor and the standard top bar. Press **F2** to switch to the captured frame if the text has moved. GNOME with multiple monitors uses a frozen frame. OCR accuracy depends on fonts, size, contrast and layout. Sentence boundaries are heuristic.

Tested with Hyprland 0.57.0 and in isolated Fedora 45 sessions with KDE Plasma 6.8.2 and GNOME 51.1. Mixed DPI, HDR and Gamescope still need live testing. Ollama is tested against response contracts. Startup of large models may require a longer timeout.

```bash
python -m unittest discover -s tests -v
uvx ruff check .
python -m build
```

An optional `python tests/live_smoke.py` runs the real overlay temporarily over the focused monitor using isolated configuration, synthetic content and a local HTTP fixture. It does not send text to external translators.

[Architecture](architecture.md) · [Contributing](../CONTRIBUTING.md) · [Changelog](../CHANGELOG.md) · [MIT license](../LICENSE)

The code was written by AI (OpenAI Codex).
