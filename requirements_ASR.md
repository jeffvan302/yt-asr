# ASR GUI App Requirements

This file covers the requirements for the `yt-subtitle-extract` package:

- `yt_subtitle_extract/gui.py` — GUI editor (`yt-asr` command)
- `yt_subtitle_extract/dataset.py` — CLI pipeline (`yt-asr-dataset` command)

## Install

The recommended way to install is via pip from the project root:

```bash
# With audio playback
pip install ".[audio]"

# Or editable (for development)
pip install -e ".[audio]"
```

This installs the `yt-asr` and `yt-asr-dataset` commands globally.

## Python

- Python 3.10 or newer
- `tkinter` must be available (standard on Windows/macOS; `python3-tk` on Linux)

## Python Packages

Installed automatically by `pip install .`:

- `yt-dlp` — YouTube download and metadata extraction

Optional (installed with `pip install ".[audio]"`):

- `sounddevice>=0.4.6` — Segment audio playback (Play/Pause/Stop/Loop)

## System Dependency

`ffmpeg` must be installed and available on `PATH`.

```bash
ffmpeg -version
```

## Run

```bash
# GUI editor — uses current directory as workspace by default
yt-asr

# GUI with explicit workspace and language
yt-asr --workspace ./youtube_asr_dataset --language af

# CLI dataset builder
yt-asr-dataset "https://www.youtube.com/watch?v=VIDEO_ID" --output ./dataset
```

## Features

- Download audio and auto-captions from YouTube URLs
- **Language picker** — Choose the caption language from a dropdown before downloading; click the ↻ button to probe a URL and see all available auto-caption languages
- Keep multiple downloaded videos in one workspace
- Video list on the left, caption phrases on the right
- Interactive waveform view with draggable start/end markers
- **Waveform panning** — Scroll wheel or Shift+drag to pan left/right; middle-mouse drag also works
- Edit start/end times manually or by dragging markers on the waveform
- **Audio playback** — Play, Pause, Stop the current segment's audio directly in the app
- **Loop mode** — Continuously loop segment audio for fine-tuning timing
- **Live playback update** — Dragging markers, editing times, or resetting a segment automatically updates the playing audio to match the new boundaries
- **Split at Cursor** — Place the cursor in the caption text and click Split to divide a phrase into two, with a proportional time estimate for the split point
- **Combine Selected** — Select two or more adjacent phrases (Ctrl+click or Shift+click) and click Combine to merge them into one phrase with the combined time range and concatenated text
- Enable/disable individual phrases for export
- Export reviewed clips and a training TSV manifest

## Notes

- No `torch`, `transformers`, `datasets`, or training packages are required.
- If `tkinter` is missing on Linux, install your distro's `python3-tk` package.
- If `ffmpeg` is not on `PATH`, downloads may finish but project creation/export will fail.
- If `sounddevice` is not installed, the Play/Pause/Stop/Loop buttons will be disabled with a hint.
