"""
youtube_asr_to_dataset.py
-------------------------
Build a pseudo-labeled ASR dataset from YouTube automatic captions.

Given one or more YouTube video URLs, this script:
  1. downloads the best available audio track
  2. fetches the Afrikaans automatic captions
  3. collapses rolling caption updates into phrase-like segments
  4. cuts one 16 kHz mono WAV clip per segment with ffmpeg
  5. writes a TSV compatible with train_whisper_af.py

Example:
  python youtube_asr_to_dataset.py ^
      "https://www.youtube.com/watch?v=VIDEO_ID" ^
      --output ./youtube_asr_dataset

Notes:
  - Use only videos you are allowed to download and use for training.
  - YouTube auto captions are noisy pseudo-labels. The default `accurate`
    score is intentionally lower than fully curated datasets.
  - Requires ffmpeg on PATH and the Python package `yt-dlp`.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import html
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/132.0.0.0 Safari/537.36"
)

TIMECODE_RE = re.compile(
    r"^(?P<start>\d{2}:)?\d{2}:\d{2}\.\d{3}\s+-->\s+(?P<end>(\d{2}:)?\d{2}:\d{2}\.\d{3})"
)
TAG_RE = re.compile(r"<[^>]+>")
MUSIC_ONLY_RE = re.compile(
    r"^[\s\W_]*(music|applause|laughter|laughing|cheering)[\s\W_]*$",
    re.IGNORECASE,
)
BRACKET_STAGE_RE = re.compile(r"^\s*[\[(].*?[\])]\s*$")
WHITESPACE_RE = re.compile(r"\s+")
TEXT_SUFFIXES = {".json", ".json3", ".srv3", ".ttml", ".vtt"}


@dataclass
class Segment:
    start_s: float
    end_s: float
    text: str

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end_s - self.start_s)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a training TSV from YouTube Afrikaans auto captions."
    )
    parser.add_argument(
        "urls",
        nargs="+",
        help="One or more YouTube video URLs.",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Output folder for audio, captions, clips, and manifest.",
    )
    parser.add_argument(
        "--language",
        default="af",
        help="Caption language to request (default: af).",
    )
    parser.add_argument(
        "--accurate",
        type=float,
        default=0.7,
        help="Accuracy weight to assign to pseudo-labels (default: 0.7).",
    )
    parser.add_argument(
        "--min-duration",
        type=float,
        default=0.8,
        help="Skip segments shorter than this many seconds (default: 0.8).",
    )
    parser.add_argument(
        "--max-duration",
        type=float,
        default=15.0,
        help="Skip segments longer than this many seconds (default: 15.0).",
    )
    parser.add_argument(
        "--min-chars",
        type=int,
        default=3,
        help="Skip captions with fewer than this many characters (default: 3).",
    )
    parser.add_argument(
        "--merge-gap",
        type=float,
        default=0.35,
        help="Merge nearby rolling caption updates within this many seconds (default: 0.35).",
    )
    parser.add_argument(
        "--cookies",
        default=None,
        help="Optional path to a Netscape cookies.txt file for restricted videos.",
    )
    parser.add_argument(
        "--source",
        default="youtube_asr",
        help="Value to write into the TSV source column (default: youtube_asr).",
    )
    parser.add_argument(
        "--split",
        default="pseudo",
        help="Value to write into the TSV original_split column (default: pseudo).",
    )
    return parser.parse_args()


def _windows_startupinfo() -> subprocess.STARTUPINFO | None:
    if os.name != "nt":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    return si


def _subprocess_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.PIPE,
    }
    si = _windows_startupinfo()
    if si is not None:
        kwargs["startupinfo"] = si
    return kwargs


@contextlib.contextmanager
def _suppress_windows_console():
    """Temporarily patch subprocess.Popen so every child process (including
    those spawned internally by yt-dlp's ffmpeg post-processors) gets a
    hidden window and stdin=DEVNULL on Windows.  No-op on other OSes."""
    if os.name != "nt":
        yield
        return

    _original_popen = subprocess.Popen

    class _SilentPopen(_original_popen):  # type: ignore[misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            if "startupinfo" not in kwargs:
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = 0
                kwargs["startupinfo"] = si
            if "stdin" not in kwargs:
                kwargs["stdin"] = subprocess.DEVNULL
            super().__init__(*args, **kwargs)

    subprocess.Popen = _SilentPopen  # type: ignore[misc]
    try:
        yield
    finally:
        subprocess.Popen = _original_popen  # type: ignore[misc]


def require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg was not found on PATH.")


def load_yt_dlp():
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError(
            "yt-dlp is not installed. Run: pip install yt-dlp"
        ) from exc
    return yt_dlp


def ensure_dirs(root: Path) -> dict[str, Path]:
    dirs = {
        "root": root,
        "downloads": root / "downloads",
        "captions": root / "captions",
        "clips": root / "clips",
        "metadata": root / "metadata",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def sanitize_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "item"


def yt_info(url: str, cookies: str | None) -> dict[str, Any]:
    yt_dlp = load_yt_dlp()
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 30,
    }
    if cookies:
        opts["cookiefile"] = cookies
    with _suppress_windows_console(), yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def download_audio(
    url: str,
    video_id: str,
    downloads_dir: Path,
    cookies: str | None,
    progress_hook: Any = None,
) -> Path:
    existing = [p for p in downloads_dir.glob(f"{video_id}.*") if p.is_file()]
    existing = [p for p in existing if p.suffix.lower() not in TEXT_SUFFIXES]
    if existing:
        return sorted(existing)[0]

    yt_dlp = load_yt_dlp()
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "format": "bestaudio/best",
        "outtmpl": str(downloads_dir / "%(id)s.%(ext)s"),
    }
    if cookies:
        opts["cookiefile"] = cookies
    if progress_hook is not None:
        opts["progress_hooks"] = [progress_hook]

    with _suppress_windows_console(), yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

    downloaded = [p for p in downloads_dir.glob(f"{video_id}.*") if p.is_file()]
    downloaded = [p for p in downloaded if p.suffix.lower() not in TEXT_SUFFIXES]
    if not downloaded:
        raise RuntimeError(f"Unable to find downloaded audio for video {video_id}.")
    return sorted(downloaded)[0]


def score_language_key(requested: str, candidate: str) -> int | None:
    req = requested.lower()
    cand = candidate.lower()
    req_base = req.split("-")[0]
    cand_base = cand.split("-")[0]

    if cand == req:
        return 0
    if cand_base == req_base:
        return 1
    if cand.startswith(req_base):
        return 2
    return None


def available_caption_languages(info: dict[str, Any]) -> list[str]:
    """Return sorted list of automatic-caption language codes from video info."""
    auto = info.get("automatic_captions") or {}
    return sorted(auto.keys())


def select_caption_track(info: dict[str, Any], language: str) -> tuple[str, dict[str, Any]]:
    auto = info.get("automatic_captions") or {}
    ranked: list[tuple[int, str, dict[str, Any]]] = []

    for lang_key, entries in auto.items():
        lang_score = score_language_key(language, lang_key)
        if lang_score is None:
            continue
        for entry in entries or []:
            ext = (entry.get("ext") or "").lower()
            if ext == "json3":
                format_score = 0
            elif ext == "vtt":
                format_score = 1
            else:
                continue
            ranked.append((lang_score * 10 + format_score, lang_key, entry))

    if not ranked:
        available = ", ".join(sorted(auto.keys())) or "none"
        raise RuntimeError(
            f"No automatic caption track found for language '{language}'. Available auto-caption languages: {available}"
        )

    ranked.sort(key=lambda item: item[0])
    _, lang_key, entry = ranked[0]
    return lang_key, entry


def fetch_caption_file(url: str, dest: Path) -> Path:
    if dest.exists():
        return dest

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        dest.write_bytes(response.read())
    return dest


def normalize_text(text: str) -> str:
    text = text.replace("\ufeff", "")
    text = html.unescape(text)
    text = text.replace("\n", " ")
    text = text.replace("\r", " ")
    text = text.replace("\u200b", "")
    text = WHITESPACE_RE.sub(" ", text).strip()
    return text


def clean_caption_text(text: str) -> str:
    text = normalize_text(text)
    text = TAG_RE.sub("", text)
    text = normalize_text(text)

    if not text:
        return ""
    if BRACKET_STAGE_RE.match(text) and MUSIC_ONLY_RE.search(text):
        return ""
    if MUSIC_ONLY_RE.match(text):
        return ""
    if text.count("♪") >= max(1, len(text) // 3):
        return ""

    return text


def parse_timecode(value: str) -> float:
    parts = value.split(":")
    if len(parts) == 2:
        minutes = int(parts[0])
        seconds = float(parts[1])
        return minutes * 60 + seconds
    if len(parts) == 3:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = float(parts[2])
        return hours * 3600 + minutes * 60 + seconds
    raise ValueError(f"Unsupported timecode: {value}")


def parse_vtt(path: Path) -> list[Segment]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    cues: list[Segment] = []
    idx = 0
    while idx < len(lines):
        line = lines[idx].strip()
        idx += 1

        if not line or line == "WEBVTT":
            continue
        if line.startswith("NOTE") or line.startswith("STYLE") or line.startswith("REGION"):
            while idx < len(lines) and lines[idx].strip():
                idx += 1
            continue

        timing = line
        if "-->" not in timing and idx < len(lines):
            timing = lines[idx].strip()
            idx += 1
        if "-->" not in timing:
            continue

        if not TIMECODE_RE.match(timing):
            continue

        start_raw, end_raw = [part.strip() for part in timing.split("-->", 1)]
        start_s = parse_timecode(start_raw.split()[0])
        end_s = parse_timecode(end_raw.split()[0])

        text_lines: list[str] = []
        while idx < len(lines) and lines[idx].strip():
            text_lines.append(lines[idx].strip())
            idx += 1

        text = clean_caption_text(" ".join(text_lines))
        if text:
            cues.append(Segment(start_s=start_s, end_s=end_s, text=text))

    return cues


def parse_json3(path: Path) -> list[Segment]:
    payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    cues: list[Segment] = []

    for event in payload.get("events", []):
        segs = event.get("segs") or []
        if not segs:
            continue

        text = clean_caption_text("".join(seg.get("utf8", "") for seg in segs))
        if not text:
            continue

        start_ms = event.get("tStartMs")
        duration_ms = event.get("dDurationMs")
        if start_ms is None or duration_ms is None:
            continue

        start_s = float(start_ms) / 1000.0
        end_s = start_s + (float(duration_ms) / 1000.0)
        cues.append(Segment(start_s=start_s, end_s=end_s, text=text))

    return cues


def load_segments(caption_path: Path) -> list[Segment]:
    suffix = caption_path.suffix.lower()
    if suffix == ".json3":
        return parse_json3(caption_path)
    if suffix == ".vtt":
        return parse_vtt(caption_path)
    raise RuntimeError(f"Unsupported caption format: {caption_path.suffix}")


def collapse_segments(
    segments: Sequence[Segment],
    merge_gap: float,
    min_duration: float,
    max_duration: float,
    min_chars: int,
) -> list[Segment]:
    if not segments:
        return []

    collapsed: list[Segment] = []

    for current in segments:
        if current.duration_s <= 0:
            continue

        if collapsed:
            prev = collapsed[-1]
            is_close = current.start_s <= prev.end_s + merge_gap
            progressive = current.text.startswith(prev.text) or prev.text.startswith(current.text)

            if is_close and current.text == prev.text:
                prev.end_s = max(prev.end_s, current.end_s)
                continue

            if is_close and progressive:
                prev.start_s = min(prev.start_s, current.start_s)
                prev.end_s = max(prev.end_s, current.end_s)
                if len(current.text) > len(prev.text):
                    prev.text = current.text
                continue

        collapsed.append(Segment(current.start_s, current.end_s, current.text))

    filtered: list[Segment] = []
    last_text = None
    for segment in collapsed:
        segment.text = clean_caption_text(segment.text)
        if not segment.text:
            continue
        if len(segment.text) < min_chars:
            continue
        if segment.duration_s < min_duration or segment.duration_s > max_duration:
            continue
        if last_text == segment.text:
            continue
        filtered.append(segment)
        last_text = segment.text

    return filtered


def cut_clip(input_audio: Path, output_wav: Path, start_s: float, end_s: float) -> None:
    command = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-ss",
        f"{start_s:.3f}",
        "-to",
        f"{end_s:.3f}",
        "-i",
        str(input_audio),
        "-ar",
        "16000",
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        str(output_wav),
    ]
    result = subprocess.run(command, check=False, timeout=120, **_subprocess_kwargs())
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, command, output=None, stderr=result.stderr
        )


def write_metadata(path: Path, info: dict[str, Any], caption_lang: str, caption_file: Path, audio_file: Path) -> None:
    payload = {
        "id": info.get("id"),
        "title": info.get("title"),
        "channel": info.get("channel"),
        "webpage_url": info.get("webpage_url"),
        "duration": info.get("duration"),
        "caption_language": caption_lang,
        "caption_file": str(caption_file.resolve()),
        "audio_file": str(audio_file.resolve()),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def build_manifest_rows(
    source_audio: Path,
    clip_root: Path,
    segments: Sequence[Segment],
    source_name: str,
    split_name: str,
    accurate: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    for idx, segment in enumerate(segments, start=1):
        clip_path = clip_root / f"{idx:06d}.wav"
        cut_clip(source_audio, clip_path, segment.start_s, segment.end_s)
        rows.append(
            {
                "audio_path": str(clip_path.resolve()),
                "sentence": segment.text,
                "source": source_name,
                "original_split": split_name,
                "is_noise": 0,
                "up_votes": 0,
                "down_votes": 0,
                "accurate": f"{accurate:.3f}".rstrip("0").rstrip("."),
                "duration_s": f"{segment.duration_s:.3f}".rstrip("0").rstrip("."),
            }
        )

    return rows


def write_manifest(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    fieldnames = [
        "audio_path",
        "sentence",
        "source",
        "original_split",
        "is_noise",
        "up_votes",
        "down_votes",
        "accurate",
        "duration_s",
    ]
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def process_url(url: str, args: argparse.Namespace, dirs: dict[str, Path]) -> list[dict[str, Any]]:
    info = yt_info(url, args.cookies)
    video_id = info.get("id")
    if not video_id:
        raise RuntimeError(f"Unable to determine video id for {url}")

    video_slug = sanitize_filename(video_id)
    audio_path = download_audio(url, video_id, dirs["downloads"], args.cookies)

    caption_lang, caption_entry = select_caption_track(info, args.language)
    caption_ext = (caption_entry.get("ext") or "json3").lower()
    caption_path = dirs["captions"] / f"{video_slug}.{caption_lang}.{caption_ext}"
    fetch_caption_file(caption_entry["url"], caption_path)

    raw_segments = load_segments(caption_path)
    segments = collapse_segments(
        raw_segments,
        merge_gap=args.merge_gap,
        min_duration=args.min_duration,
        max_duration=args.max_duration,
        min_chars=args.min_chars,
    )
    if not segments:
        raise RuntimeError(f"No usable segments found for {url}")

    clip_root = dirs["clips"] / video_slug
    clip_root.mkdir(parents=True, exist_ok=True)

    write_metadata(
        dirs["metadata"] / f"{video_slug}.json",
        info=info,
        caption_lang=caption_lang,
        caption_file=caption_path,
        audio_file=audio_path,
    )

    return build_manifest_rows(
        source_audio=audio_path,
        clip_root=clip_root,
        segments=segments,
        source_name=args.source,
        split_name=args.split,
        accurate=args.accurate,
    )


def main() -> int:
    args = parse_args()
    require_ffmpeg()

    output_root = Path(args.output).resolve()
    dirs = ensure_dirs(output_root)

    all_rows: list[dict[str, Any]] = []
    failures: list[str] = []

    for url in args.urls:
        print(f"Processing: {url}")
        try:
            rows = process_url(url, args, dirs)
        except Exception as exc:
            failures.append(f"{url}: {exc}")
            print(f"  FAILED: {exc}")
            continue

        all_rows.extend(rows)
        print(f"  Added {len(rows)} clips")

    if not all_rows:
        print("No clips were created.", file=sys.stderr)
        if failures:
            print("\nFailures:", file=sys.stderr)
            for failure in failures:
                print(f"  - {failure}", file=sys.stderr)
        return 1

    manifest_path = output_root / "youtube_asr_dataset.tsv"
    write_manifest(manifest_path, all_rows)

    print(f"\nWrote manifest: {manifest_path}")
    print(f"Total clips: {len(all_rows)}")

    if failures:
        print("\nCompleted with some failures:")
        for failure in failures:
            print(f"  - {failure}")
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
