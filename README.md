# yt-subtitle-extract

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

### System dependency

**ffmpeg** must be installed and on your `PATH`.

```bash
# Windows (winget)
winget install FFmpeg

# Windows (scoop)
scoop install ffmpeg

# Linux
sudo apt install ffmpeg

# macOS
brew install ffmpeg
```

### Python requirement

`tkinter` must be available in your Python install. It ships with most Windows and macOS Python installers. On Linux, install your distro's `python3-tk` package if missing.

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
