# Youtube Generated Subtitle Extractor for Transcription training

Download YouTube audio + auto-captions, review timing in a GUI editor, and export ASR training datasets.

## Install

```bash
# Basic install (GUI works but without audio playback)
pip install .

# With audio playback support
pip install ".[audio]"

# Development / editable install
pip install -e ".[audio]"
```

### System dependencies

Two non-Python tools are required before running `pip install`:

- **ffmpeg** — used for audio conversion and clip extraction. Must be on your `PATH`.
- **tkinter** — the GUI toolkit. Bundled with most Python installers; may need a separate package on Linux.

#### Quick install — use the scripts in `install/`

The `install/` folder contains ready-to-run scripts that handle everything automatically for each platform. Double-click or run from a terminal — no manual steps needed.

| Platform | Script | How to run | What it does |
|---|---|---|---|
| Windows 10 / 11 | `install\win-required.bat` | Double-click or run in CMD | Installs ffmpeg via **winget** (built into Windows). Notes tkinter requirement. |
| Windows (any) | `install\win-required-scoop.bat` | Double-click or run in CMD | Installs **Scoop** package manager if missing, then installs ffmpeg via Scoop. |
| Linux | `install/linux-required.sh` | `bash install/linux-required.sh` | Auto-detects **apt** (Ubuntu/Debian), **dnf** (Fedora/RHEL), or **pacman** (Arch) and installs both ffmpeg and python3-tk. |
| macOS | `install/macos-required.sh` | `bash install/macos-required.sh` | Installs **Homebrew** if missing, then installs ffmpeg via Homebrew. Notes the tkinter/Homebrew-Python caveat. |

> **Windows users:** `win-required.bat` is the recommended starting point. If winget is not available on your machine (older Windows 10), use `win-required-scoop.bat` instead.

> **Linux users:** The script installs both `ffmpeg` and `python3-tk` in one step and works across the most common distros without any configuration.

#### Manual install (if you prefer)

```bash
# Windows (winget)
winget install Gyan.FFmpeg

# Windows (scoop)
scoop install ffmpeg

# Linux (Debian / Ubuntu)
sudo apt install ffmpeg python3-tk

# macOS
brew install ffmpeg
```

**tkinter** ships with the standard Python installer from [python.org](https://www.python.org/downloads/).
On Linux it may need a separate package (`python3-tk` / `python3-tkinter` / `tk` depending on distro).
On macOS, if you installed Python via Homebrew, also run `brew install python-tk`.

## Usage

### GUI editor — `yt-asr`

```bash
# Launch in current directory as workspace (creates subdirectories for downloads, captions, etc.)
yt-asr

# Specify a workspace folder
yt-asr --workspace ./my_project

# Set default caption language
yt-asr --language en
```

### CLI dataset builder — `yt-asr-dataset`

```bash
# Build dataset in current directory
yt-asr-dataset "https://www.youtube.com/watch?v=VIDEO_ID"

# Specify output folder and language
yt-asr-dataset "https://www.youtube.com/watch?v=VIDEO_ID" --output ./dataset --language af

# Multiple URLs
yt-asr-dataset URL1 URL2 URL3 --output ./dataset
```

## Features

- Download audio and auto-captions from YouTube URLs
- **Language picker** — Choose the caption language from a dropdown; click ↻ to probe a URL and see all available auto-caption languages
- Multiple videos in one workspace
- Interactive waveform view with draggable start/end markers
- **Waveform panning** — Scroll wheel or Shift+drag to pan left/right
- **Audio playback** — Play, Pause, Stop with loop mode for fine-tuning timing
- **Live playback update** — Dragging markers automatically updates playback boundaries
- **Split at Cursor** — Place cursor in caption text and split a phrase into two
- **Combine Selected** — Ctrl+click adjacent phrases and merge them (time ranges + text)
- Enable/disable individual phrases for export
- Export reviewed clips and a training TSV manifest

## Notes

- No `torch`, `transformers`, `datasets`, or training packages required
- If `sounddevice` is not installed, Play/Pause/Stop/Loop buttons are disabled with a hint
- If `ffmpeg` is not on `PATH`, downloads work but conversion/export will fail
