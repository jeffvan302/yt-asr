"""
youtube_asr_review_gui.py
-------------------------
Desktop editor for reviewing and adjusting YouTube Afrikaans auto-caption timing.

Features:
  - Download audio + auto-captions from YouTube URLs
  - Keep multiple downloaded videos in one workspace
  - Show video list on the left, caption phrases on the right
  - Jump the waveform view to the selected phrase
  - Drag start/end bars to correct timings
  - Export reviewed clips and a training TSV

Example:
  python youtube_asr_review_gui.py --workspace ./youtube_asr_dataset
"""

from __future__ import annotations

import argparse
import array
import csv
import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
import traceback
import wave
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import sounddevice as sd
    _HAS_SOUNDDEVICE = True
except ImportError:
    sd = None  # type: ignore[assignment]
    _HAS_SOUNDDEVICE = False

from .dataset import (
    available_caption_languages,
    collapse_segments,
    cut_clip,
    download_audio,
    ensure_dirs,
    fetch_caption_file,
    load_segments,
    sanitize_filename,
    select_caption_track,
    yt_info,
)
try:
    from .cloud import (
        B2CloudStore,
        CloudPanel,
        _HAS_B2,
        _HAS_CRYPTO,
        actor_name_for_config,
        load_b2_config,
        load_cloud_state,
        lock_belongs_to,
        remove_cloud_state_entry,
        update_cloud_state_entry,
    )
    _HAS_CLOUD = True
except ImportError:
    _HAS_CLOUD = False
    _HAS_B2 = False
    _HAS_CRYPTO = False
    B2CloudStore = None  # type: ignore[assignment]

    def load_cloud_state(_workspace: Path) -> dict[str, dict[str, Any]]:
        return {}

    def load_b2_config(_workspace: Path) -> Any:
        return None

    def update_cloud_state_entry(_workspace: Path, _video_id: str, **_updates: Any) -> dict[str, dict[str, Any]]:
        return {}

    def remove_cloud_state_entry(_workspace: Path, _video_id: str) -> dict[str, dict[str, Any]]:
        return {}

    def actor_name_for_config(_config: Any) -> str:
        return ""

    def lock_belongs_to(_lock: Any, _config: Any) -> bool:
        return False


logger = logging.getLogger(__name__)

DEFAULT_WORKSPACE = "."
PROJECT_VERSION = 1
WAVEFORM_HEIGHT = 280
MIN_SEGMENT_LEN_S = 0.05
DEFAULT_VIEW_SPAN_S = 14.0
DEFAULT_EXPORT_ACCURATE = 0.8
REVIEW_MERGE_GAP = 0.35
REVIEW_MIN_DURATION_S = 0.35
REVIEW_MAX_DURATION_S = 20.0
REVIEW_MIN_CHARS = 2
DEFAULT_MANUAL_SEGMENT_TEXT = "<Sentence>"
DEFAULT_MANUAL_SEGMENT_LEN_S = 2.0
DEFAULT_PLAYBACK_SPEED = 1.0
MIN_PLAYBACK_SPEED = 0.5
MAX_PLAYBACK_SPEED = 1.5
MARKER_HITBOX_PX = 10
AUTO_SYNC_IDLE_TIMEOUT_S = 0.35
IMAGE_SUBTITLE_CODECS = {
    "dvd_subtitle",
    "dvb_subtitle",
    "hdmv_pgs_subtitle",
    "xsub",
}
DIRECT_SUBTITLE_SUFFIXES = {".json", ".json3", ".srv3", ".vtt", ".srt"}
MEDIA_FILETYPES = [
    ("Media files", "*.mp4 *.mkv *.mov *.avi *.m4v *.webm *.mp3 *.m4a *.wav *.flac *.aac *.ogg"),
    ("All files", "*.*"),
]
SUBTITLE_FILETYPES = [
    ("Subtitle files", "*.vtt *.srt *.json3 *.srv3 *.json *.ass *.ssa *.ttml *.xml"),
    ("All files", "*.*"),
]


@dataclass
class EditableSegment:
    index: int
    text: str
    start_s: float
    end_s: float
    original_start_s: float
    original_end_s: float
    enabled: bool = True
    reviewed: bool = False


@dataclass
class VideoProject:
    version: int
    video_id: str
    title: str
    channel: str
    webpage_url: str
    duration: float
    caption_language: str
    caption_file: str
    audio_file: str
    working_audio_file: str
    created_at: float
    updated_at: float
    segments: list[EditableSegment]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Launch the YouTube ASR timing review GUI."
    )
    parser.add_argument(
        "--workspace",
        default=DEFAULT_WORKSPACE,
        help=f"Workspace folder for downloads and projects (default: {DEFAULT_WORKSPACE}).",
    )
    parser.add_argument(
        "--language",
        default="af",
        help="Caption language to request when downloading new videos (default: af).",
    )
    return parser.parse_args()


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def ffprobe_path() -> str | None:
    return shutil.which("ffprobe")


def ensure_app_dirs(root: Path) -> dict[str, Path]:
    dirs = ensure_dirs(root)
    dirs["projects"] = root / "projects"
    dirs["working_audio"] = root / "working_audio"
    dirs["exports"] = root / "exports"
    dirs["export_clips"] = dirs["exports"] / "clips"
    for key in ("projects", "working_audio", "exports", "export_clips"):
        dirs[key].mkdir(parents=True, exist_ok=True)
    return dirs


def project_path(root: Path, video_id: str) -> Path:
    return root / "projects" / f"{video_id}.json"


def metadata_path(root: Path, video_id: str) -> Path:
    return root / "metadata" / f"{video_id}.json"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_empty_caption_file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"events": []}, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def clamp_playback_speed(speed: Any) -> float:
    try:
        value = float(speed)
    except (TypeError, ValueError):
        value = DEFAULT_PLAYBACK_SPEED
    return max(MIN_PLAYBACK_SPEED, min(MAX_PLAYBACK_SPEED, round(value, 2)))


def playback_speed_text(speed: Any) -> str:
    return f"{clamp_playback_speed(speed):.2f}x"


def playback_status_text(state: str, speed: Any, *, loop: bool = False) -> str:
    text = f"{state} @ {playback_speed_text(speed)}"
    if loop:
        text += " [Loop]"
    return text


def effective_playback_samplerate(rate: int, speed: Any) -> int:
    return max(1, int(round(rate * clamp_playback_speed(speed))))


def _to_relative(p: Path, root: Path) -> Path:
    """Return *p* relative to *root* when possible, otherwise return *p* unchanged."""
    try:
        return p.resolve().relative_to(root.resolve())
    except ValueError:
        return p.resolve()


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


def convert_audio_to_wav(input_audio: Path, output_wav: Path) -> Path:
    ffmpeg = ffmpeg_path()
    if ffmpeg is None:
        raise RuntimeError("ffmpeg was not found on PATH.")

    if output_wav.exists():
        return output_wav

    command = [
        ffmpeg,
        "-y",
        "-v",
        "error",
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
    try:
        result = subprocess.run(command, check=False, timeout=600, **_subprocess_kwargs())
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"ffmpeg conversion timed out after 10 minutes for {input_audio.name}"
        )
    if result.returncode != 0:
        stderr_text = (result.stderr or b"").decode(errors="replace").strip()
        raise RuntimeError(f"ffmpeg conversion failed: {stderr_text or 'unknown error'}")
    return output_wav


def subtitle_tracks_from_probe(payload: dict[str, Any]) -> list[dict[str, Any]]:
    tracks: list[dict[str, Any]] = []
    subtitle_number = 0
    for stream in payload.get("streams") or []:
        if not isinstance(stream, dict):
            continue
        if str(stream.get("codec_type") or "").lower() != "subtitle":
            continue
        subtitle_number += 1
        tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
        disposition = (
            stream.get("disposition")
            if isinstance(stream.get("disposition"), dict)
            else {}
        )
        codec = str(stream.get("codec_name") or "").strip().lower()
        language = str(
            tags.get("language")
            or tags.get("LANGUAGE")
            or tags.get("lang")
            or ""
        ).strip()
        title = str(
            tags.get("title")
            or tags.get("handler_name")
            or tags.get("HANDLER_NAME")
            or ""
        ).strip()
        supported = codec not in IMAGE_SUBTITLE_CODECS
        display_parts = [
            language or "und",
            title or f"Subtitle {subtitle_number}",
            codec or "unknown codec",
        ]
        if disposition.get("default"):
            display_parts.append("default")
        if not supported:
            display_parts.append("unsupported")
        tracks.append(
            {
                "stream_index": int(stream.get("index", subtitle_number - 1)),
                "subtitle_number": subtitle_number,
                "language": language,
                "title": title,
                "codec": codec,
                "supported": supported,
                "default": bool(disposition.get("default")),
                "display": " | ".join(display_parts),
            }
        )
    return tracks


def list_embedded_subtitle_tracks(media_path: Path) -> list[dict[str, Any]]:
    ffprobe = ffprobe_path()
    if ffprobe is None:
        raise RuntimeError("ffprobe was not found on PATH.")

    command = [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        str(media_path),
    ]
    kwargs = _subprocess_kwargs()
    kwargs["stdout"] = subprocess.PIPE
    result = subprocess.run(command, check=False, timeout=120, **kwargs)
    if result.returncode != 0:
        stderr_text = (result.stderr or b"").decode(errors="replace").strip()
        raise RuntimeError(f"ffprobe failed: {stderr_text or 'unknown error'}")
    try:
        payload = json.loads((result.stdout or b"{}").decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not parse ffprobe output: {exc}") from exc
    return subtitle_tracks_from_probe(payload)


def convert_subtitle_to_vtt(source_path: Path, dest_vtt: Path) -> Path:
    ffmpeg = ffmpeg_path()
    if ffmpeg is None:
        raise RuntimeError("ffmpeg was not found on PATH.")
    command = [
        ffmpeg,
        "-y",
        "-v",
        "error",
        "-i",
        str(source_path),
        "-c:s",
        "webvtt",
        str(dest_vtt),
    ]
    result = subprocess.run(command, check=False, timeout=600, **_subprocess_kwargs())
    if result.returncode != 0:
        stderr_text = (result.stderr or b"").decode(errors="replace").strip()
        raise RuntimeError(
            f"Subtitle conversion failed: {stderr_text or 'unknown error'}"
        )
    return dest_vtt


def extract_embedded_subtitle_to_vtt(
    media_path: Path,
    stream_index: int,
    dest_vtt: Path,
) -> Path:
    ffmpeg = ffmpeg_path()
    if ffmpeg is None:
        raise RuntimeError("ffmpeg was not found on PATH.")
    command = [
        ffmpeg,
        "-y",
        "-v",
        "error",
        "-i",
        str(media_path),
        "-map",
        f"0:{stream_index}",
        "-c:s",
        "webvtt",
        str(dest_vtt),
    ]
    result = subprocess.run(command, check=False, timeout=600, **_subprocess_kwargs())
    if result.returncode != 0:
        stderr_text = (result.stderr or b"").decode(errors="replace").strip()
        raise RuntimeError(
            f"Subtitle extraction failed: {stderr_text or 'unknown error'}"
        )
    return dest_vtt


def make_unique_local_video_id(root: Path, preferred_name: str) -> str:
    base = sanitize_filename(preferred_name) or "local_media"
    candidate = base
    suffix = 2
    while project_path(root, candidate).exists() or metadata_path(root, candidate).exists():
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def copy_or_prepare_local_subtitle(
    source_path: Path,
    dest_dir: Path,
    video_id: str,
    caption_language: str,
) -> Path:
    lang_slug = sanitize_filename(caption_language or "und")
    suffix = source_path.suffix.lower()
    if suffix in DIRECT_SUBTITLE_SUFFIXES:
        dest_path = dest_dir / f"{sanitize_filename(video_id)}.{lang_slug}{suffix}"
        if source_path.resolve() != dest_path.resolve():
            shutil.copy2(source_path, dest_path)
        return dest_path

    dest_path = dest_dir / f"{sanitize_filename(video_id)}.{lang_slug}.vtt"
    return convert_subtitle_to_vtt(source_path, dest_path)


def create_project_from_local_media(
    root: Path,
    media_path: Path,
    *,
    title: str,
    channel: str,
    caption_language: str,
    subtitle_mode: str,
    subtitle_stream_index: int | None = None,
    subtitle_file: Path | None = None,
    status_hook: Callable[[str], None] | None = None,
) -> VideoProject:
    def _status(message: str) -> None:
        if status_hook is not None:
            status_hook(message)

    dirs = ensure_app_dirs(root)
    resolved_media = media_path.resolve()
    if not resolved_media.exists():
        raise RuntimeError(f"Media file does not exist: {resolved_media}")

    final_title = title.strip() or resolved_media.stem
    final_channel = channel.strip()
    final_language = caption_language.strip() or "und"
    video_id = make_unique_local_video_id(root, final_title or resolved_media.stem)

    caption_dest_dir = dirs["captions"]
    audio_dest = dirs["working_audio"] / f"{sanitize_filename(video_id)}.wav"

    _status("Preparing subtitle file...")
    if subtitle_mode == "embedded":
        if subtitle_stream_index is None:
            raise RuntimeError("Select an embedded subtitle track to import.")
        caption_path = caption_dest_dir / (
            f"{sanitize_filename(video_id)}.{sanitize_filename(final_language)}.vtt"
        )
        extract_embedded_subtitle_to_vtt(
            resolved_media,
            subtitle_stream_index,
            caption_path,
        )
    elif subtitle_mode == "external":
        if subtitle_file is None:
            raise RuntimeError("Select a subtitle file to import.")
        caption_path = copy_or_prepare_local_subtitle(
            subtitle_file.resolve(),
            caption_dest_dir,
            video_id,
            final_language,
        )
    else:
        raise RuntimeError(f"Unsupported subtitle mode: {subtitle_mode}")

    _status("Extracting audio...")
    working_audio = convert_audio_to_wav(resolved_media, audio_dest)
    duration = wave_duration_s(working_audio)
    segments = build_segments_from_caption(caption_path)
    now = time.time()

    project = VideoProject(
        version=PROJECT_VERSION,
        video_id=video_id,
        title=final_title,
        channel=final_channel,
        webpage_url=resolved_media.as_uri(),
        duration=duration,
        caption_language=final_language,
        caption_file=str(_to_relative(caption_path, root)),
        audio_file=str(_to_relative(working_audio, root)),
        working_audio_file=str(_to_relative(working_audio, root)),
        created_at=now,
        updated_at=now,
        segments=segments,
    )
    save_project(project_path(root, video_id), project)
    write_json(
        metadata_path(root, video_id),
        {
            "id": video_id,
            "title": final_title,
            "channel": final_channel,
            "webpage_url": resolved_media.as_uri(),
            "duration": duration,
            "caption_language": final_language,
            "caption_file": str(_to_relative(caption_path, root)),
            "audio_file": str(_to_relative(working_audio, root)),
        },
    )
    return project


def _path_is_within_workspace(path: Path, workspace: Path) -> bool:
    try:
        path.resolve().relative_to(workspace.resolve())
        return True
    except ValueError:
        return False


def delete_local_title_data(root: Path, video_id: str) -> list[Path]:
    tracked_files: set[Path] = set()
    removed: list[Path] = []

    project_file = project_path(root, video_id)
    metadata_file = metadata_path(root, video_id)
    for manifest_path in (project_file, metadata_file):
        if not manifest_path.exists():
            continue
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        for key in ("caption_file", "audio_file", "working_audio_file"):
            raw_path = payload.get(key)
            if not raw_path:
                continue
            resolved = resolve_path(str(raw_path), root)
            if _path_is_within_workspace(resolved, root):
                tracked_files.add(resolved)

    for manifest_path in (project_file, metadata_file):
        if manifest_path.exists():
            tracked_files.add(manifest_path.resolve())

    for path in sorted(tracked_files, key=lambda item: len(str(item)), reverse=True):
        if not path.exists() or not path.is_file():
            continue
        path.unlink(missing_ok=True)
        removed.append(path)

    clip_dir = root / "exports" / "clips" / sanitize_filename(video_id)
    if clip_dir.exists() and clip_dir.is_dir() and _path_is_within_workspace(clip_dir, root):
        shutil.rmtree(clip_dir, ignore_errors=True)
        removed.append(clip_dir)

    return removed


def wave_duration_s(wav_path: Path) -> float:
    with wave.open(str(wav_path), "rb") as wav_file:
        frames = wav_file.getnframes()
        rate = wav_file.getframerate()
        if rate <= 0:
            return 0.0
        return frames / float(rate)


def video_summary_from_project(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        "video_id": payload["video_id"],
        "title": payload.get("title") or payload["video_id"],
        "channel": payload.get("channel") or "",
        "source": "project",
        "project_path": path,
        "metadata_path": metadata_path(path.parent.parent, payload["video_id"]),
        "segment_count": len(payload.get("segments") or []),
    }


def video_summary_from_metadata(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        "video_id": payload["id"],
        "title": payload.get("title") or payload["id"],
        "channel": payload.get("channel") or "",
        "source": "metadata",
        "project_path": project_path(path.parent.parent, payload["id"]),
        "metadata_path": path,
        "segment_count": None,
    }


def discover_videos(root: Path) -> list[dict[str, Any]]:
    dirs = ensure_app_dirs(root)
    by_id: dict[str, dict[str, Any]] = {}
    cloud_state = load_cloud_state(root)

    for path in sorted(dirs["metadata"].glob("*.json")):
        summary = video_summary_from_metadata(path)
        by_id[summary["video_id"]] = summary

    for path in sorted(dirs["projects"].glob("*.json")):
        summary = video_summary_from_project(path)
        by_id[summary["video_id"]] = summary

    summaries: list[dict[str, Any]] = []
    for video_id, summary in by_id.items():
        cloud_entry = cloud_state.get(video_id, {})
        cloud_status = str(cloud_entry.get("cloud_state") or "").strip()
        lock_user = str(cloud_entry.get("lock_user") or "").strip()

        match cloud_status:
            case "checked_out_self":
                state_label = "Checked Out"
                sort_group = 0
                read_only = False
            case "checked_in":
                state_label = "Checked In"
                sort_group = 1
                read_only = True
            case "checked_out_other":
                state_label = f"Locked: {lock_user}" if lock_user else "Locked"
                sort_group = 1
                read_only = True
            case _:
                state_label = "Reviewed" if summary["source"] == "project" else "Downloaded"
                sort_group = 1
                read_only = False

        merged = dict(summary)
        merged["cloud_state"] = cloud_status
        merged["lock_user"] = lock_user
        merged["state_label"] = state_label
        merged["read_only"] = read_only
        merged["_sort_group"] = sort_group
        summaries.append(merged)

    return sorted(
        summaries,
        key=lambda item: (int(item.get("_sort_group", 1)), item["title"].lower()),
    )


def segment_from_payload(payload: dict[str, Any]) -> EditableSegment:
    return EditableSegment(
        index=int(payload["index"]),
        text=payload["text"],
        start_s=float(payload["start_s"]),
        end_s=float(payload["end_s"]),
        original_start_s=float(payload.get("original_start_s", payload["start_s"])),
        original_end_s=float(payload.get("original_end_s", payload["end_s"])),
        enabled=bool(payload.get("enabled", True)),
        reviewed=bool(payload.get("reviewed", False)),
    )


def load_project(path: Path) -> VideoProject:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return VideoProject(
        version=int(payload.get("version", PROJECT_VERSION)),
        video_id=payload["video_id"],
        title=payload.get("title") or payload["video_id"],
        channel=payload.get("channel") or "",
        webpage_url=payload.get("webpage_url") or "",
        duration=float(payload.get("duration", 0.0)),
        caption_language=payload.get("caption_language") or "af",
        caption_file=payload["caption_file"],
        audio_file=payload["audio_file"],
        working_audio_file=payload["working_audio_file"],
        created_at=float(payload.get("created_at", time.time())),
        updated_at=float(payload.get("updated_at", time.time())),
        segments=[segment_from_payload(item) for item in payload.get("segments", [])],
    )


def save_project(path: Path, project: VideoProject) -> None:
    project.updated_at = time.time()
    root = path.parent.parent  # workspace root (projects/ is one level down)
    def _rel(p_str: str) -> str:
        return str(_to_relative(Path(p_str), root))

    payload = {
        "version": project.version,
        "video_id": project.video_id,
        "title": project.title,
        "channel": project.channel,
        "webpage_url": project.webpage_url,
        "duration": project.duration,
        "caption_language": project.caption_language,
        "caption_file": _rel(project.caption_file),
        "audio_file": _rel(project.audio_file),
        "working_audio_file": _rel(project.working_audio_file),
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        "segments": [asdict(segment) for segment in project.segments],
    }
    write_json(path, payload)


def resolve_path(path_value: str, root: Path) -> Path:
    path = Path(path_value)
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def build_segments_from_caption(caption_path: Path) -> list[EditableSegment]:
    raw_segments = load_segments(caption_path)
    collapsed = collapse_segments(
        raw_segments,
        merge_gap=REVIEW_MERGE_GAP,
        min_duration=REVIEW_MIN_DURATION_S,
        max_duration=REVIEW_MAX_DURATION_S,
        min_chars=REVIEW_MIN_CHARS,
    )
    return [
        EditableSegment(
            index=index,
            text=segment.text,
            start_s=segment.start_s,
            end_s=segment.end_s,
            original_start_s=segment.start_s,
            original_end_s=segment.end_s,
            enabled=True,
        )
        for index, segment in enumerate(collapsed, start=1)
    ]


def build_manual_segment(
    project: VideoProject,
    *,
    after_index: int | None = None,
    text: str = DEFAULT_MANUAL_SEGMENT_TEXT,
) -> EditableSegment:
    duration = max(float(project.duration or 0.0), 0.0)
    span = DEFAULT_MANUAL_SEGMENT_LEN_S
    if duration > 0:
        span = min(span, duration)
    span = max(MIN_SEGMENT_LEN_S, span)

    start_s = 0.0
    if project.segments:
        if after_index is None or after_index < 0 or after_index >= len(project.segments):
            anchor = project.segments[-1]
        else:
            anchor = project.segments[after_index]
        start_s = max(0.0, float(anchor.end_s))

    if duration > 0:
        max_start = max(0.0, duration - span)
        start_s = min(start_s, max_start)
        end_s = min(duration, start_s + span)
        if end_s - start_s < MIN_SEGMENT_LEN_S:
            start_s = max(0.0, duration - MIN_SEGMENT_LEN_S)
            end_s = duration
    else:
        end_s = start_s + span

    return EditableSegment(
        index=0,
        text=text,
        start_s=round(start_s, 3),
        end_s=round(end_s, 3),
        original_start_s=round(start_s, 3),
        original_end_s=round(end_s, 3),
        enabled=True,
        reviewed=False,
    )


def metadata_to_project(root: Path, payload: dict[str, Any]) -> VideoProject:
    video_id = payload.get("id") or payload.get("video_id")
    if not video_id:
        raise RuntimeError("Metadata did not contain a video id.")

    audio_file = resolve_path(payload["audio_file"], root)
    caption_file = resolve_path(payload["caption_file"], root)
    if not audio_file.exists():
        raise RuntimeError(f"Audio file does not exist: {audio_file}")
    if not caption_file.exists():
        raise RuntimeError(f"Caption file does not exist: {caption_file}")

    working_audio = root / "working_audio" / f"{sanitize_filename(video_id)}.wav"
    if audio_file.suffix.lower() == ".wav":
        working_audio = audio_file
    else:
        convert_audio_to_wav(audio_file, working_audio)

    duration = float(payload.get("duration") or 0.0)
    if duration <= 0:
        duration = wave_duration_s(working_audio)

    now = time.time()
    return VideoProject(
        version=PROJECT_VERSION,
        video_id=video_id,
        title=payload.get("title") or video_id,
        channel=payload.get("channel") or "",
        webpage_url=payload.get("webpage_url") or "",
        duration=duration,
        caption_language=payload.get("caption_language") or "af",
        caption_file=str(_to_relative(caption_file, root)),
        audio_file=str(_to_relative(audio_file, root)),
        working_audio_file=str(_to_relative(working_audio, root)),
        created_at=now,
        updated_at=now,
        segments=build_segments_from_caption(caption_file),
    )


def ensure_project_for_video(root: Path, video_id: str) -> VideoProject:
    path = project_path(root, video_id)
    if path.exists():
        project = load_project(path)
        working_audio = resolve_path(project.working_audio_file, root)
        caption_file = resolve_path(project.caption_file, root)
        if working_audio.exists() and caption_file.exists():
            return project

    meta_path = metadata_path(root, video_id)
    if not meta_path.exists():
        raise RuntimeError(f"No metadata found for video '{video_id}'.")

    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    project = metadata_to_project(root, payload)
    save_project(path, project)
    return project


def create_project_from_url(
    root: Path,
    url: str,
    language: str,
    progress_hook: Any = None,
    status_hook: Any = None,
) -> VideoProject:
    def _status(msg: str) -> None:
        if status_hook is not None:
            status_hook(msg)

    dirs = ensure_app_dirs(root)
    _status("Fetching video info...")
    info = yt_info(url, None)
    video_id = info.get("id")
    if not video_id:
        raise RuntimeError(f"Unable to determine a video id for {url}")

    path = project_path(root, video_id)
    if path.exists():
        return load_project(path)

    _status("Downloading audio...")
    audio_file = download_audio(url, video_id, dirs["downloads"], None, progress_hook=progress_hook)
    _status("Fetching captions...")
    caption_language = language or "und"
    caption_ext = "json3"
    caption_file = dirs["captions"] / (
        f"{sanitize_filename(video_id)}.{caption_language}.{caption_ext}"
    )
    try:
        caption_language, caption_entry = select_caption_track(info, language)
        caption_ext = (caption_entry.get("ext") or "json3").lower()
        caption_file = dirs["captions"] / (
            f"{sanitize_filename(video_id)}.{caption_language}.{caption_ext}"
        )
        fetch_caption_file(
            caption_entry["url"],
            caption_file,
            video_url=url,
            caption_language=caption_language,
            caption_format=caption_ext,
        )
    except Exception as exc:
        logger.warning(
            "Could not fetch captions for %s. Creating empty project for manual sentences: %s",
            video_id,
            exc,
        )
        caption_ext = "json3"
        caption_file = dirs["captions"] / (
            f"{sanitize_filename(video_id)}.{caption_language}.{caption_ext}"
        )
        caption_file.unlink(missing_ok=True)
        write_empty_caption_file(caption_file)
        _status("No usable captions were available. Creating an empty project for manual sentence entry...")

    metadata = {
        "id": video_id,
        "title": info.get("title") or video_id,
        "channel": info.get("channel") or "",
        "webpage_url": info.get("webpage_url") or url,
        "duration": info.get("duration") or 0,
        "caption_language": caption_language,
        "caption_file": str(caption_file.resolve()),
        "audio_file": str(audio_file.resolve()),
    }
    write_json(metadata_path(root, video_id), metadata)

    _status("Converting audio to WAV and building segments...")
    project = metadata_to_project(root, metadata)
    save_project(path, project)
    return project


# ---------------------------------------------------------------------------
# .asr package support (ZIP archive with .asr extension)
# ---------------------------------------------------------------------------

ASR_FORMAT_VERSION = 1


def list_asr_contents(asr_path: Path) -> list[dict[str, Any]]:
    """Return the manifest entries from an .asr package without extracting files."""
    with zipfile.ZipFile(asr_path, "r") as zf:
        with zf.open("manifest.json") as fh:
            manifest = json.loads(fh.read().decode("utf-8"))
    return manifest.get("projects", [])


def pack_asr(
    workspace: Path,
    projects: list[VideoProject],
    dest_path: Path,
    *,
    status_hook: Callable[[str], None] | None = None,
) -> int:
    """
    Pack *projects* from *workspace* into an .asr ZIP archive at *dest_path*.
    Returns the number of projects successfully packed.

    Archive layout::

        manifest.json
        {video_id}/
            project.json       # segments + intra-package relative paths
            metadata.json      # video metadata
            working_audio.wav  # WAV used for review
            caption.{ext}      # caption file (.json3 or .vtt)
    """
    manifest_projects: list[dict[str, Any]] = []
    packed = 0

    with zipfile.ZipFile(dest_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for project in projects:
            slug = sanitize_filename(project.video_id)
            if status_hook:
                status_hook(f"Packing {project.title}…")

            working_audio = resolve_path(project.working_audio_file, workspace)
            caption_file  = resolve_path(project.caption_file, workspace)

            if not working_audio.exists():
                logger.warning("Skipping %s: working audio missing (%s)", project.video_id, working_audio)
                continue

            # Intra-package names (flat inside the project folder)
            intra_audio   = "working_audio.wav"
            intra_caption = f"caption{caption_file.suffix}"

            # Project payload with package-relative paths
            pkg_project: dict[str, Any] = {
                "version":          project.version,
                "video_id":         project.video_id,
                "title":            project.title,
                "channel":          project.channel,
                "webpage_url":      project.webpage_url,
                "duration":         project.duration,
                "caption_language": project.caption_language,
                "caption_file":     intra_caption,
                "audio_file":       intra_audio,
                "working_audio_file": intra_audio,
                "created_at":       project.created_at,
                "updated_at":       project.updated_at,
                "segments":         [asdict(seg) for seg in project.segments],
            }

            # Metadata (load from workspace if available, otherwise synthesise)
            meta_p = metadata_path(workspace, project.video_id)
            if meta_p.exists():
                raw_meta: dict[str, Any] = json.loads(meta_p.read_text(encoding="utf-8"))
            else:
                raw_meta = {
                    "id":               project.video_id,
                    "title":            project.title,
                    "channel":          project.channel,
                    "webpage_url":      project.webpage_url,
                    "duration":         project.duration,
                    "caption_language": project.caption_language,
                }
            raw_meta["caption_file"] = intra_caption
            raw_meta["audio_file"]   = intra_audio

            # Write into archive
            zf.writestr(f"{slug}/project.json",  json.dumps(pkg_project, ensure_ascii=False, indent=2))
            zf.writestr(f"{slug}/metadata.json", json.dumps(raw_meta,    ensure_ascii=False, indent=2))
            zf.write(working_audio, f"{slug}/{intra_audio}")
            if caption_file.exists():
                zf.write(caption_file, f"{slug}/{intra_caption}")

            manifest_projects.append({
                "video_id": project.video_id,
                "title":    project.title,
                "channel":  project.channel,
                "folder":   slug,
            })
            packed += 1

        zf.writestr(
            "manifest.json",
            json.dumps(
                {"version": ASR_FORMAT_VERSION, "projects": manifest_projects},
                ensure_ascii=False,
                indent=2,
            ),
        )

    return packed


def unpack_asr(
    asr_path: Path,
    workspace: Path,
    video_ids: list[str],
    *,
    status_hook: Callable[[str], None] | None = None,
) -> list[str]:
    """
    Extract the projects listed in *video_ids* from *asr_path* into *workspace*.
    Returns the list of video IDs that were successfully imported.
    """
    dirs = ensure_app_dirs(workspace)
    imported: list[str] = []

    with zipfile.ZipFile(asr_path, "r") as zf:
        arc_names = set(zf.namelist())

        with zf.open("manifest.json") as fh:
            manifest = json.loads(fh.read().decode("utf-8"))

        for entry in manifest.get("projects", []):
            vid_id = entry["video_id"]
            if vid_id not in video_ids:
                continue

            folder = entry["folder"]
            if status_hook:
                status_hook(f"Importing {entry.get('title', vid_id)}…")

            # Read project payload from archive
            pkg_project: dict[str, Any] = json.loads(
                zf.read(f"{folder}/project.json").decode("utf-8")
            )

            intra_audio   = pkg_project.get("working_audio_file", "working_audio.wav")
            intra_caption = pkg_project.get("caption_file", "caption.json3")
            caption_ext   = Path(intra_caption).suffix  # e.g. ".json3" or ".vtt"

            # Destination paths inside the workspace
            slug         = sanitize_filename(vid_id)
            dest_audio   = dirs["working_audio"] / f"{slug}.wav"
            dest_caption = dirs["captions"] / f"{slug}{caption_ext}"

            # Extract audio
            with zf.open(f"{folder}/{intra_audio}") as src, open(dest_audio, "wb") as dst:
                dst.write(src.read())

            # Extract caption (may be absent in very old packages)
            caption_arc = f"{folder}/{intra_caption}"
            if caption_arc in arc_names:
                with zf.open(caption_arc) as src, open(dest_caption, "wb") as dst:
                    dst.write(src.read())

            # Rewrite paths to workspace-relative values
            pkg_project["working_audio_file"] = str(dest_audio.relative_to(workspace))
            pkg_project["audio_file"]         = str(dest_audio.relative_to(workspace))
            pkg_project["caption_file"]       = str(dest_caption.relative_to(workspace))

            # Write project.json
            proj_p = project_path(workspace, vid_id)
            proj_p.parent.mkdir(parents=True, exist_ok=True)
            proj_p.write_text(json.dumps(pkg_project, ensure_ascii=False, indent=2), encoding="utf-8")

            # Write metadata.json (if present)
            meta_arc = f"{folder}/metadata.json"
            if meta_arc in arc_names:
                raw_meta = json.loads(zf.read(meta_arc).decode("utf-8"))
                raw_meta["caption_file"] = str(dest_caption.relative_to(workspace))
                raw_meta["audio_file"]   = str(dest_audio.relative_to(workspace))
                meta_p = metadata_path(workspace, vid_id)
                meta_p.parent.mkdir(parents=True, exist_ok=True)
                meta_p.write_text(json.dumps(raw_meta, ensure_ascii=False, indent=2), encoding="utf-8")

            imported.append(vid_id)

    return imported


def export_projects(
    root: Path,
    projects: list[VideoProject],
    accurate: float = DEFAULT_EXPORT_ACCURATE,
) -> dict[str, Any]:
    dirs = ensure_app_dirs(root)
    manifest_path = dirs["exports"] / "reviewed_dataset.tsv"
    rows: list[dict[str, Any]] = []
    clip_count = 0

    for project in projects:
        source_audio = resolve_path(project.working_audio_file, root)
        if not source_audio.exists():
            raise RuntimeError(
                f"Working audio for '{project.title}' is missing: {source_audio}"
            )

        clip_root = dirs["export_clips"] / sanitize_filename(project.video_id)
        clip_root.mkdir(parents=True, exist_ok=True)

        segment_number = 1
        for segment in project.segments:
            if not segment.enabled:
                continue

            start_s = max(0.0, segment.start_s)
            end_s = min(project.duration, segment.end_s) if project.duration else segment.end_s
            if end_s - start_s < MIN_SEGMENT_LEN_S:
                continue

            clip_path = clip_root / f"{segment_number:06d}.wav"
            cut_clip(source_audio, clip_path, start_s, end_s)
            clip_count += 1
            rows.append(
                {
                    "audio_path": str(clip_path.resolve()),
                    "sentence": segment.text,
                    "source": "youtube_asr_reviewed",
                    "original_split": "reviewed",
                    "is_noise": 0,
                    "up_votes": 0,
                    "down_votes": 0,
                    "accurate": f"{accurate:.3f}".rstrip("0").rstrip("."),
                    "duration_s": f"{(end_s - start_s):.3f}".rstrip("0").rstrip("."),
                }
            )
            segment_number += 1

    if not rows:
        raise RuntimeError("No enabled segments were available to export.")

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
    with open(manifest_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)

    return {
        "manifest_path": manifest_path,
        "clip_count": clip_count,
        "project_count": len(projects),
    }


# ---------------------------------------------------------------------------
# Title-selection dialog (shared by pack and import flows)
# ---------------------------------------------------------------------------

class TitleSelectDialog(tk.Toplevel):
    """Modal checklist that lets the user pick a subset of projects by title."""

    def __init__(
        self,
        parent: tk.Misc,
        dialog_title: str,
        summaries: list[dict[str, Any]],
        *,
        default_all: bool = True,
    ) -> None:
        super().__init__(parent)
        self.title(dialog_title)
        self.resizable(True, True)
        self.transient(parent)
        self.grab_set()

        self._vars: list[tuple[tk.BooleanVar, str]] = []
        self.result: list[str] | None = None  # None → cancelled

        self._build(summaries, default_all)
        self.update_idletasks()

        # Centre over the parent window
        w = 480
        h = min(480, 100 + len(summaries) * 26 + 60)
        px = parent.winfo_rootx() + parent.winfo_width()  // 2
        py = parent.winfo_rooty() + parent.winfo_height() // 2
        self.geometry(f"{w}x{h}+{px - w // 2}+{py - h // 2}")
        self.minsize(320, 160)

        self.wait_window()

    # ------------------------------------------------------------------
    def _build(self, summaries: list[dict[str, Any]], default_all: bool) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        # --- select-all / select-none row ---
        top = ttk.Frame(self, padding=(8, 8, 8, 4))
        top.grid(row=0, column=0, sticky="ew")
        ttk.Button(top, text="Select All",  command=lambda: self._set_all(True)).pack(side="left", padx=(0, 6))
        ttk.Button(top, text="Select None", command=lambda: self._set_all(False)).pack(side="left")

        # --- scrollable checklist ---
        list_frame = ttk.Frame(self, padding=(8, 0, 8, 4))
        list_frame.grid(row=1, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)

        canvas = tk.Canvas(list_frame, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        canvas.configure(yscrollcommand=scrollbar.set)
        inner.bind(
            "<Configure>",
            lambda _e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        for summary in summaries:
            var = tk.BooleanVar(value=default_all)
            vid_id = summary["video_id"]
            label  = summary.get("title") or vid_id
            ttk.Checkbutton(inner, text=label, variable=var).pack(anchor="w", padx=4, pady=2)
            self._vars.append((var, vid_id))

        # --- OK / Cancel ---
        btn_row = ttk.Frame(self, padding=(8, 4, 8, 10))
        btn_row.grid(row=2, column=0)
        ttk.Button(btn_row, text="OK",     command=self._ok,     width=10).pack(side="left", padx=6)
        ttk.Button(btn_row, text="Cancel", command=self._cancel, width=10).pack(side="left")

    # ------------------------------------------------------------------
    def _set_all(self, value: bool) -> None:
        for var, _ in self._vars:
            var.set(value)

    def _ok(self) -> None:
        self.result = [vid_id for var, vid_id in self._vars if var.get()]
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


class LocalMediaImportDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, default_language: str) -> None:
        super().__init__(parent)
        self.title("Import Local Media")
        self.resizable(True, True)
        self.transient(parent)
        self.grab_set()

        self.result: dict[str, Any] | None = None
        self._embedded_tracks: list[dict[str, Any]] = []

        self._media_path_var = tk.StringVar()
        self._subtitle_file_var = tk.StringVar()
        self._title_var = tk.StringVar()
        self._channel_var = tk.StringVar()
        self._caption_language_var = tk.StringVar(value=default_language or "und")
        self._subtitle_mode_var = tk.StringVar(value="embedded")
        self._track_note_var = tk.StringVar(value="Choose a media file to inspect subtitle tracks.")

        self._build()
        self.update_idletasks()

        w = 760
        h = 540
        px = parent.winfo_rootx() + parent.winfo_width() // 2
        py = parent.winfo_rooty() + parent.winfo_height() // 2
        self.geometry(f"{w}x{h}+{px - w // 2}+{py - h // 2}")
        self.minsize(620, 440)

        self.wait_window()

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        form = ttk.Frame(self, padding=12)
        form.grid(row=0, column=0, sticky="nsew")
        form.columnconfigure(1, weight=1)
        form.columnconfigure(2, weight=0)

        ttk.Label(form, text="Media file:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 6))
        ttk.Entry(form, textvariable=self._media_path_var).grid(row=0, column=1, sticky="ew", pady=(0, 6))
        ttk.Button(form, text="Browse", command=self._browse_media).grid(row=0, column=2, padx=(6, 0), pady=(0, 6))

        ttk.Label(form, text="Title:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(0, 6))
        ttk.Entry(form, textvariable=self._title_var).grid(row=1, column=1, columnspan=2, sticky="ew", pady=(0, 6))

        ttk.Label(form, text="Channel:").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(0, 6))
        ttk.Entry(form, textvariable=self._channel_var).grid(row=2, column=1, columnspan=2, sticky="ew", pady=(0, 6))

        ttk.Label(form, text="Caption language:").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=(0, 6))
        ttk.Entry(form, textvariable=self._caption_language_var, width=14).grid(
            row=3, column=1, sticky="w", pady=(0, 6)
        )
        ttk.Label(form, text="Use codes like en, af, en-US", foreground="#6b7280").grid(
            row=3, column=2, sticky="w", padx=(6, 0), pady=(0, 6)
        )

        source_lf = ttk.LabelFrame(self, text="Subtitle Source", padding=12)
        source_lf.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 8))
        source_lf.columnconfigure(0, weight=1)
        source_lf.rowconfigure(2, weight=1)

        ttk.Radiobutton(
            source_lf,
            text="Use an embedded subtitle track from the media file",
            variable=self._subtitle_mode_var,
            value="embedded",
            command=self._update_source_mode,
        ).grid(row=0, column=0, sticky="w")

        embedded_frame = ttk.Frame(source_lf)
        embedded_frame.grid(row=1, column=0, sticky="nsew", pady=(8, 10))
        embedded_frame.columnconfigure(0, weight=1)
        embedded_frame.rowconfigure(0, weight=1)

        self._embedded_list = tk.Listbox(embedded_frame, height=8, exportselection=False)
        self._embedded_list.grid(row=0, column=0, sticky="nsew")
        self._embedded_list.bind("<<ListboxSelect>>", self._on_track_selected)
        embedded_scroll = ttk.Scrollbar(
            embedded_frame,
            orient="vertical",
            command=self._embedded_list.yview,
        )
        embedded_scroll.grid(row=0, column=1, sticky="ns")
        self._embedded_list.configure(yscrollcommand=embedded_scroll.set)
        ttk.Label(
            source_lf,
            textvariable=self._track_note_var,
            foreground="#6b7280",
            wraplength=680,
        ).grid(row=2, column=0, sticky="ew", pady=(0, 10))

        ttk.Radiobutton(
            source_lf,
            text="Use a separate subtitle file",
            variable=self._subtitle_mode_var,
            value="external",
            command=self._update_source_mode,
        ).grid(row=3, column=0, sticky="w")

        external_row = ttk.Frame(source_lf)
        external_row.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        external_row.columnconfigure(0, weight=1)
        self._subtitle_entry = ttk.Entry(external_row, textvariable=self._subtitle_file_var)
        self._subtitle_entry.grid(row=0, column=0, sticky="ew")
        self._subtitle_browse_btn = ttk.Button(
            external_row,
            text="Browse",
            command=self._browse_subtitle_file,
        )
        self._subtitle_browse_btn.grid(row=0, column=1, padx=(6, 0))

        btn_row = ttk.Frame(self, padding=(12, 0, 12, 12))
        btn_row.grid(row=2, column=0, sticky="e")
        ttk.Button(btn_row, text="Cancel", command=self._cancel, width=12).pack(side="right")
        ttk.Button(btn_row, text="Import", command=self._ok, width=12).pack(side="right", padx=(0, 8))

        self._update_source_mode()

    def _browse_media(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self,
            title="Select media file",
            filetypes=MEDIA_FILETYPES,
        )
        if not selected:
            return
        media_path = Path(selected)
        self._media_path_var.set(str(media_path))
        if not self._title_var.get().strip():
            self._title_var.set(media_path.stem)
        self._scan_embedded_tracks(media_path)

    def _browse_subtitle_file(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self,
            title="Select subtitle file",
            filetypes=SUBTITLE_FILETYPES,
        )
        if not selected:
            return
        self._subtitle_file_var.set(selected)
        self._subtitle_mode_var.set("external")
        self._update_source_mode()

    def _scan_embedded_tracks(self, media_path: Path) -> None:
        self._embedded_tracks = []
        self._embedded_list.delete(0, "end")
        try:
            tracks = list_embedded_subtitle_tracks(media_path)
        except Exception as exc:
            self._track_note_var.set(f"Could not inspect embedded subtitles: {exc}")
            return

        self._embedded_tracks = tracks
        if not tracks:
            self._track_note_var.set("No embedded subtitle tracks were found. Choose a separate subtitle file instead.")
            self._subtitle_mode_var.set("external")
            self._update_source_mode()
            return

        for track in tracks:
            self._embedded_list.insert("end", track["display"])

        preferred_index = 0
        for idx, track in enumerate(tracks):
            if track.get("default"):
                preferred_index = idx
                break
        self._embedded_list.selection_clear(0, "end")
        self._embedded_list.selection_set(preferred_index)
        self._embedded_list.activate(preferred_index)
        self._embedded_list.see(preferred_index)
        self._subtitle_mode_var.set("embedded")
        self._update_source_mode()
        self._on_track_selected()

        supported = sum(1 for track in tracks if track.get("supported"))
        unsupported = len(tracks) - supported
        note = f"Found {len(tracks)} embedded subtitle track(s)."
        if unsupported:
            note += f" {unsupported} track(s) appear image-based and may not import."
        self._track_note_var.set(note)

    def _update_source_mode(self) -> None:
        embedded_mode = self._subtitle_mode_var.get() == "embedded"
        self._embedded_list.configure(state="normal" if embedded_mode else "disabled")
        self._subtitle_entry.state(["disabled"] if embedded_mode else ["!disabled"])
        self._subtitle_browse_btn.state(["disabled"] if embedded_mode else ["!disabled"])

    def _selected_track(self) -> dict[str, Any] | None:
        selection = self._embedded_list.curselection()
        if not selection:
            return None
        index = int(selection[0])
        if 0 <= index < len(self._embedded_tracks):
            return self._embedded_tracks[index]
        return None

    def _on_track_selected(self, _event: Any = None) -> None:
        track = self._selected_track()
        if not track:
            return
        language = str(track.get("language") or "").strip()
        if language:
            self._caption_language_var.set(language)

    def _ok(self) -> None:
        media_path = Path(self._media_path_var.get().strip()) if self._media_path_var.get().strip() else None
        if media_path is None or not media_path.exists():
            messagebox.showwarning("Missing media", "Choose a media file to import.", parent=self)
            return

        title = self._title_var.get().strip() or media_path.stem
        caption_language = self._caption_language_var.get().strip() or "und"
        subtitle_mode = self._subtitle_mode_var.get().strip() or "embedded"

        subtitle_file: Path | None = None
        subtitle_stream_index: int | None = None
        if subtitle_mode == "embedded":
            track = self._selected_track()
            if not track:
                messagebox.showwarning("No subtitle track", "Select an embedded subtitle track first.", parent=self)
                return
            if not track.get("supported"):
                messagebox.showwarning(
                    "Unsupported subtitle track",
                    "That embedded subtitle track looks image-based. Choose a different track or use a separate subtitle file.",
                    parent=self,
                )
                return
            subtitle_stream_index = int(track["stream_index"])
            if not self._caption_language_var.get().strip():
                self._caption_language_var.set(str(track.get("language") or "und"))
                caption_language = self._caption_language_var.get().strip() or "und"
        else:
            subtitle_raw = self._subtitle_file_var.get().strip()
            subtitle_file = Path(subtitle_raw) if subtitle_raw else None
            if subtitle_file is None or not subtitle_file.exists():
                messagebox.showwarning("Missing subtitle file", "Choose a subtitle file to import.", parent=self)
                return

        self.result = {
            "media_path": media_path.resolve(),
            "title": title,
            "channel": self._channel_var.get().strip(),
            "caption_language": caption_language,
            "subtitle_mode": subtitle_mode,
            "subtitle_stream_index": subtitle_stream_index,
            "subtitle_file": subtitle_file.resolve() if subtitle_file else None,
        }
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


class ASRReviewApp:
    def __init__(self, root: tk.Tk, workspace: Path, language: str) -> None:
        self.root = root
        self.workspace = workspace.resolve()
        self.language = language
        self.dirs = ensure_app_dirs(self.workspace)

        self.worker_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self.worker_done_callback: Any = None
        self.active_job_key: str | None = None
        self._auto_sync_queue: queue.Queue[tuple[Path, str, float]] = queue.Queue()
        self._auto_sync_result_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._auto_sync_thread: threading.Thread | None = None
        self._auto_sync_lock = threading.Lock()

        self.video_summaries: list[dict[str, Any]] = []
        self._video_summary_by_id: dict[str, dict[str, Any]] = {}
        self.current_project: VideoProject | None = None
        self.current_video_summary: dict[str, Any] | None = None
        self.current_read_only = False
        self.current_segment_index: int | None = None
        self.view_start_s = 0.0
        self.view_span_s = DEFAULT_VIEW_SPAN_S
        self.drag_target: str | None = None
        self._pan_start_x: int | None = None
        self._pan_start_view: float = 0.0
        self._suspend_segment_select = False
        self._suspend_video_select = False
        self._setting_text = False
        self.text_dirty = False

        self._last_segment_selection: tuple[str, ...] = ()

        # Audio playback state
        self._playback_stream: Any = None
        self._playback_data: bytes = b""
        self._playback_offset = 0
        self._playback_loop = False
        self._playback_paused = False
        self._playback_rate = 16000
        self._playback_channels = 1
        self._playback_sampwidth = 2
        self._playback_segment_start_s = 0.0
        self._playback_segment_end_s = 0.0
        self._playback_indicator_job: str | None = None

        self.workspace_var = tk.StringVar(value=str(self.workspace))
        self.url_var = tk.StringVar()
        self.language_var = tk.StringVar(value=self.language)
        self.status_var = tk.StringVar(value="Ready.")
        self.video_label_var = tk.StringVar(value="No video loaded.")
        self.segment_label_var = tk.StringVar(value="No phrase selected.")
        self.start_var = tk.StringVar()
        self.end_var = tk.StringVar()
        self._playback_speed_var = tk.DoubleVar(value=DEFAULT_PLAYBACK_SPEED)
        self._playback_speed_label_var = tk.StringVar(
            value=playback_speed_text(DEFAULT_PLAYBACK_SPEED)
        )
        self.enabled_var  = tk.BooleanVar(value=True)
        self.reviewed_var = tk.BooleanVar(value=False)

        self.root.title("YouTube ASR Review")
        self.root.geometry("1680x960")
        self.root.minsize(1280, 780)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        if ffmpeg_path() is None:
            self._set_status(
                "ffmpeg was not found on PATH. Download/export and some project creation steps will fail until it is installed."
            )
        self.root.after(150, self._poll_worker_queue)
        self.root.after(0, lambda: self.refresh_video_list(auto_open=False))

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        # ── Toolbar ──────────────────────────────────────────────────────────
        # Two logical rows inside a single outer frame, separated by thin
        # vertical separators between functional groups.
        toolbar = ttk.Frame(self.root, padding=(10, 8, 10, 6))
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.columnconfigure(0, weight=1)

        # ---- Row 0: workspace path + file/export/package actions ----
        row0 = ttk.Frame(toolbar)
        row0.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        row0.columnconfigure(1, weight=1)   # workspace Entry stretches

        ttk.Label(row0, text="Workspace:").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Entry(row0, textvariable=self.workspace_var).grid(
            row=0, column=1, sticky="ew", padx=(0, 4)
        )
        ttk.Button(row0, text="Browse", command=self._choose_workspace).grid(
            row=0, column=2, padx=(0, 4)
        )
        ttk.Button(row0, text="Reload", command=self.refresh_video_list).grid(
            row=0, column=3, padx=(0, 4)
        )
        self._save_progress_btn = ttk.Button(
            row0, text="Save Progress",
            command=lambda: self._save_current_project(show_feedback=True),
        )
        self._save_progress_btn.grid(row=0, column=4, padx=(0, 0))

        ttk.Separator(row0, orient="vertical").grid(
            row=0, column=5, sticky="ns", padx=(12, 12)
        )

        ttk.Button(row0, text="Export Current",
                   command=self._export_current_project).grid(row=0, column=6, padx=(0, 4))
        ttk.Button(row0, text="Export All",
                   command=self._export_all_projects).grid(row=0, column=7, padx=(0, 0))

        ttk.Separator(row0, orient="vertical").grid(
            row=0, column=8, sticky="ns", padx=(12, 12)
        )

        ttk.Button(row0, text="Pack .asr",
                   command=self._pack_asr).grid(row=0, column=9, padx=(0, 4))
        ttk.Button(row0, text="Import .asr",
                   command=self._unpack_asr).grid(row=0, column=10)

        ttk.Separator(row0, orient="vertical").grid(
            row=0, column=11, sticky="ns", padx=(12, 12)
        )
        ttk.Button(row0, text="☁  Cloud",
                   command=self._open_cloud_panel).grid(row=0, column=12)

        # ---- thin horizontal rule between the two toolbar rows ----
        ttk.Separator(toolbar, orient="horizontal").grid(
            row=1, column=0, sticky="ew", pady=(0, 6)
        )

        # ---- Row 1: download strip ----
        row1 = ttk.Frame(toolbar)
        row1.grid(row=2, column=0, sticky="ew")
        row1.columnconfigure(4, weight=1)   # URL Text widget stretches

        ttk.Label(row1, text="Language:").grid(row=0, column=0, sticky="w", padx=(0, 4))
        self.language_combo = ttk.Combobox(
            row1, textvariable=self.language_var, width=8,
            values=["af", "en", "zu", "xh", "st", "tn", "ts", "ss", "nr", "ve",
                    "nso", "nl", "de", "fr", "es", "pt", "it", "ru", "ja", "ko",
                    "zh", "ar", "hi"],
        )
        self.language_combo.grid(row=0, column=1, padx=(0, 2))
        ttk.Button(
            row1, text="\u21bb", width=3, command=self._fetch_available_languages,
        ).grid(row=0, column=2, padx=(0, 0))

        ttk.Separator(row1, orient="vertical").grid(
            row=0, column=3, sticky="ns", padx=(10, 10)
        )

        ttk.Label(row1, text="YouTube URL(s):").grid(row=0, column=4, sticky="w", padx=(0, 6))
        # Re-assign column weight so the URL entry (col 5) stretches
        row1.columnconfigure(4, weight=0)
        row1.columnconfigure(5, weight=1)
        self.url_input = tk.Text(row1, height=2, wrap="word")
        self.url_input.grid(row=0, column=5, sticky="ew", padx=(0, 6))
        self.url_input.bind("<Control-Return>", lambda _event: self._download_urls())
        ttk.Button(row1, text="Download", command=self._download_urls).grid(
            row=0, column=6, sticky="n"
        )
        ttk.Button(row1, text="Import Media...", command=self._import_local_media).grid(
            row=0, column=7, sticky="n", padx=(6, 0)
        )

        status = ttk.Label(
            self.root,
            textvariable=self.status_var,
            anchor="w",
            relief="flat",
            background="#f1f5f9",
            foreground="#374151",
            padding=(10, 4),
        )
        status.grid(row=2, column=0, sticky="ew")

        main = ttk.Panedwindow(self.root, orient="horizontal")
        main.grid(row=1, column=0, sticky="nsew", padx=10, pady=(4, 4))

        videos_frame   = ttk.Frame(main, padding=8)
        editor_frame   = ttk.Frame(main, padding=8)
        segments_frame = ttk.Frame(main, padding=8)
        main.add(videos_frame,   weight=2)
        main.add(editor_frame,   weight=6)
        main.add(segments_frame, weight=3)

        videos_frame.columnconfigure(0, weight=1)
        videos_frame.rowconfigure(1, weight=1)
        ttk.Label(videos_frame, text="Workspace Titles").grid(
            row=0, column=0, sticky="w", pady=(0, 6)
        )
        self.video_tree = ttk.Treeview(
            videos_frame,
            columns=("channel", "state", "segments"),
            show="tree headings",
            selectmode="browse",
        )
        self.video_tree.heading("#0", text="Title")
        self.video_tree.heading("channel", text="Channel")
        self.video_tree.heading("state", text="State")
        self.video_tree.heading("segments", text="Phrases")
        self.video_tree.column("#0", width=240, stretch=True)
        self.video_tree.column("channel", width=120, stretch=True)
        self.video_tree.column("state", width=120, stretch=False, anchor="center")
        self.video_tree.column("segments", width=70, stretch=False, anchor="e")
        self.video_tree.tag_configure("checked_out", foreground="#1d4ed8")
        self.video_tree.tag_configure("checked_in", foreground="#6b7280")
        self.video_tree.tag_configure("locked", foreground="#b45309")
        self.video_tree.grid(row=1, column=0, sticky="nsew")
        self.video_tree.bind("<<TreeviewSelect>>", self._on_video_selected)
        video_scroll = ttk.Scrollbar(
            videos_frame, orient="vertical", command=self.video_tree.yview
        )
        video_scroll.grid(row=1, column=1, sticky="ns")
        self.video_tree.configure(yscrollcommand=video_scroll.set)

        video_actions = ttk.Frame(videos_frame, padding=(0, 8, 0, 0))
        video_actions.grid(row=2, column=0, sticky="ew")
        self._delete_local_btn = ttk.Button(
            video_actions,
            text="Delete Local Copy",
            command=self._delete_local_copy,
        )
        self._delete_local_btn.pack(side="left")

        editor_frame.columnconfigure(0, weight=1)
        editor_frame.rowconfigure(3, weight=1)
        ttk.Label(
            editor_frame,
            textvariable=self.video_label_var,
            anchor="w",
            font=("Segoe UI", 12, "bold"),
        ).grid(row=0, column=0, sticky="ew", pady=(0, 8))

        self.caption_text_label_var = tk.StringVar(value="Caption Text (editable)")
        ttk.Label(editor_frame, textvariable=self.caption_text_label_var).grid(
            row=1, column=0, sticky="w", pady=(0, 4)
        )
        self.preview_text = tk.Text(
            editor_frame,
            height=5,
            wrap="word",
            relief="solid",
            borderwidth=1,
        )
        self.preview_text.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self.preview_text.bind("<<Modified>>", self._on_text_modified)

        self.waveform_canvas = tk.Canvas(
            editor_frame,
            height=WAVEFORM_HEIGHT,
            background="#0f172a",
            highlightthickness=0,
        )
        self.waveform_canvas.grid(row=3, column=0, sticky="nsew")
        self.waveform_canvas.bind("<Configure>", lambda _event: self._redraw_waveform())
        self.waveform_canvas.bind("<ButtonPress-1>", self._on_waveform_press)
        self.waveform_canvas.bind("<B1-Motion>", self._on_waveform_drag)
        self.waveform_canvas.bind("<ButtonRelease-1>", self._on_waveform_release)
        # Pan: scroll wheel to pan left/right
        self.waveform_canvas.bind("<MouseWheel>", self._on_waveform_scroll)
        # Pan: middle-mouse-button drag
        self.waveform_canvas.bind("<ButtonPress-2>", self._on_pan_press)
        self.waveform_canvas.bind("<B2-Motion>", self._on_pan_drag)
        # Shift+left-click drag also pans (more natural on laptops)
        self.waveform_canvas.bind("<Shift-ButtonPress-1>", self._on_pan_press)
        self.waveform_canvas.bind("<Shift-B1-Motion>", self._on_pan_drag)

        # --- Playback controls row ---
        playback_frame = ttk.Frame(editor_frame, padding=(0, 6, 0, 0))
        playback_frame.grid(row=4, column=0, sticky="ew")
        playback_frame.columnconfigure(0, weight=1)

        playback_buttons = ttk.Frame(playback_frame)
        playback_buttons.grid(row=0, column=0, sticky="w")

        self._play_btn = ttk.Button(playback_buttons, text="Play", width=6,
                                    command=self._playback_play)
        self._play_btn.pack(side="left", padx=(0, 4))
        self._pause_btn = ttk.Button(playback_buttons, text="Pause", width=6,
                                     command=self._playback_pause)
        self._pause_btn.pack(side="left", padx=(0, 4))
        self._stop_btn = ttk.Button(playback_buttons, text="Stop", width=6,
                                    command=self._playback_stop)
        self._stop_btn.pack(side="left", padx=(0, 10))
        self._loop_var = tk.BooleanVar(value=False)
        self._loop_btn = ttk.Checkbutton(
            playback_buttons,
            text="Loop",
            variable=self._loop_var,
            command=self._on_loop_toggled,
        )
        self._loop_btn.pack(side="left", padx=(0, 10))
        playback_speed_frame = ttk.Frame(playback_frame)
        playback_speed_frame.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ttk.Label(playback_speed_frame, text="Speed").pack(side="left", padx=(0, 8))
        self._playback_speed_scale = ttk.Scale(
            playback_speed_frame,
            from_=MIN_PLAYBACK_SPEED,
            to=MAX_PLAYBACK_SPEED,
            variable=self._playback_speed_var,
            orient="horizontal",
            length=180,
            command=self._on_playback_speed_drag,
        )
        self._playback_speed_scale.pack(side="left", fill="x")
        self._playback_speed_scale.bind("<ButtonRelease-1>", self._on_playback_speed_commit)
        self._playback_speed_scale.bind("<KeyRelease>", self._on_playback_speed_commit)
        ttk.Label(
            playback_speed_frame,
            textvariable=self._playback_speed_label_var,
            width=6,
            anchor="e",
        ).pack(side="left", padx=(8, 0))
        self._playback_status_var = tk.StringVar(value="")
        ttk.Label(
            playback_frame,
            textvariable=self._playback_status_var,
            foreground="#4b5563",
        ).grid(row=2, column=0, sticky="ew", pady=(4, 0))

        if not _HAS_SOUNDDEVICE:
            self._play_btn.state(["disabled"])
            self._pause_btn.state(["disabled"])
            self._stop_btn.state(["disabled"])
            self._playback_speed_scale.state(["disabled"])
            self._playback_status_var.set("pip install sounddevice to enable playback")

        # --- Existing controls ---
        controls = ttk.Frame(editor_frame, padding=(0, 10, 0, 0))
        controls.grid(row=5, column=0, sticky="ew")
        controls.columnconfigure(0, weight=1)
        controls.bind(
            "<Configure>",
            lambda event: self._controls_help_label.configure(
                wraplength=max(int(event.width) - 20, 260)
            ),
        )

        ttk.Label(controls, textvariable=self.segment_label_var).grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )

        navigation_frame = ttk.Frame(controls)
        navigation_frame.grid(row=1, column=0, sticky="w")
        self._previous_btn = ttk.Button(navigation_frame, text="Previous", command=self._select_previous_segment)
        self._previous_btn.pack(side="left", padx=(0, 6))
        self._next_btn = ttk.Button(navigation_frame, text="Next", command=self._select_next_segment)
        self._next_btn.pack(side="left", padx=(0, 12))
        self._zoom_in_btn = ttk.Button(navigation_frame, text="Zoom In", command=lambda: self._zoom(0.7))
        self._zoom_in_btn.pack(side="left", padx=(0, 6))
        self._zoom_out_btn = ttk.Button(navigation_frame, text="Zoom Out", command=lambda: self._zoom(1.4))
        self._zoom_out_btn.pack(side="left")

        timing_frame = ttk.Frame(controls)
        timing_frame.grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Label(timing_frame, text="Start (s)").pack(side="left")
        self._start_entry = ttk.Entry(timing_frame, textvariable=self.start_var, width=10)
        self._start_entry.pack(side="left", padx=(6, 12))
        self._start_entry.bind("<Return>", lambda _event: self._apply_time_entries())
        ttk.Label(timing_frame, text="End (s)").pack(side="left")
        self._end_entry = ttk.Entry(timing_frame, textvariable=self.end_var, width=10)
        self._end_entry.pack(side="left", padx=(6, 0))
        self._end_entry.bind("<Return>", lambda _event: self._apply_time_entries())

        timing_actions = ttk.Frame(controls)
        timing_actions.grid(row=3, column=0, sticky="w", pady=(6, 0))
        self._apply_btn = ttk.Button(timing_actions, text="Apply", command=self._apply_time_entries)
        self._apply_btn.pack(side="left", padx=(0, 6))
        self._reset_btn = ttk.Button(timing_actions, text="Reset Segment", command=self._reset_current_segment)
        self._reset_btn.pack(side="left")

        flags_frame = ttk.Frame(controls)
        flags_frame.grid(row=4, column=0, sticky="w", pady=(8, 0))
        self._enabled_check = ttk.Checkbutton(
            flags_frame,
            text="Include in export",
            variable=self.enabled_var,
            command=self._toggle_current_segment_enabled,
        )
        self._enabled_check.pack(side="left", padx=(0, 10))
        self._reviewed_check = ttk.Checkbutton(
            flags_frame,
            text="Reviewed",
            variable=self.reviewed_var,
            command=self._toggle_current_segment_reviewed,
        )
        self._reviewed_check.pack(side="left")

        edit_tools_frame = ttk.Frame(controls)
        edit_tools_frame.grid(row=5, column=0, sticky="w", pady=(8, 0))
        self._add_sentence_btn = ttk.Button(edit_tools_frame, text="Add Sentence", command=self._add_sentence)
        self._add_sentence_btn.pack(side="left", padx=(0, 6))
        self._split_btn = ttk.Button(edit_tools_frame, text="Split at Cursor", command=self._split_at_cursor)
        self._split_btn.pack(side="left", padx=(0, 6))
        self._combine_btn = ttk.Button(edit_tools_frame, text="Combine Selected", command=self._combine_segments)
        self._combine_btn.pack(side="left")
        help_text = (
            "Add Sentence creates a new editable phrase. "
            "Drag markers or scroll to pan. "
            "Split: place cursor in text, click Split. "
            "Combine: select adjacent phrases on the right, click Combine."
        )
        self._controls_help_label = ttk.Label(
            controls,
            text=help_text,
            foreground="#4b5563",
            justify="left",
            wraplength=700,
        )
        self._controls_help_label.grid(
            row=6, column=0, sticky="ew", pady=(8, 0)
        )

        segments_frame.columnconfigure(0, weight=1)
        segments_frame.rowconfigure(1, weight=1)
        ttk.Label(segments_frame, text="Caption Phrases").grid(
            row=0, column=0, sticky="w", pady=(0, 6)
        )
        self.segment_tree = ttk.Treeview(
            segments_frame,
            columns=("start", "end", "enabled", "reviewed"),
            show="tree headings",
            selectmode="extended",
        )
        self.segment_tree.heading("#0", text="Sentence")
        self.segment_tree.heading("start", text="Start")
        self.segment_tree.heading("end", text="End")
        self.segment_tree.heading("enabled", text="Use")
        self.segment_tree.heading("reviewed", text="Reviewed")
        self.segment_tree.column("#0", width=320, stretch=True)
        self.segment_tree.column("start", width=72, stretch=False, anchor="e")
        self.segment_tree.column("end", width=72, stretch=False, anchor="e")
        self.segment_tree.column("enabled", width=50, stretch=False, anchor="center")
        self.segment_tree.column("reviewed", width=70, stretch=False, anchor="center")
        # Row colour tags — four combinations of enabled/reviewed state
        self.segment_tree.tag_configure("normal",            foreground="#111827", background="#ffffff")
        self.segment_tree.tag_configure("reviewed",          foreground="#111827", background="#d1fae5")
        self.segment_tree.tag_configure("disabled",          foreground="#9ca3af", background="#ffffff")
        self.segment_tree.tag_configure("disabled_reviewed", foreground="#9ca3af", background="#d1fae5")
        self.segment_tree.grid(row=1, column=0, sticky="nsew")
        self.segment_tree.bind("<<TreeviewSelect>>", self._on_segment_selected)
        self.segment_tree.bind("<ButtonRelease-1>", self._on_segment_tree_click)
        seg_scroll = ttk.Scrollbar(
            segments_frame, orient="vertical", command=self.segment_tree.yview
        )
        seg_scroll.grid(row=1, column=1, sticky="ns")
        self.segment_tree.configure(yscrollcommand=seg_scroll.set)

        self.root.bind("<Left>", self._on_arrow_left)
        self.root.bind("<Right>", self._on_arrow_right)
        self.root.bind(
            "<Control-s>", lambda _event: self._save_current_project(show_feedback=True)
        )

    def _on_arrow_left(self, event: tk.Event[Any]) -> None:
        if self.root.focus_get() is self.preview_text:
            return  # let the text widget handle cursor movement
        self._select_previous_segment()

    def _on_arrow_right(self, event: tk.Event[Any]) -> None:
        if self.root.focus_get() is self.preview_text:
            return  # let the text widget handle cursor movement
        self._select_next_segment()

    def _set_status(self, message: str) -> None:
        self.status_var.set(message)

    def _current_cloud_state(self) -> str:
        if not self.current_video_summary:
            return ""
        return str(self.current_video_summary.get("cloud_state") or "").strip()

    def _current_lock_user(self) -> str:
        if not self.current_video_summary:
            return ""
        return str(self.current_video_summary.get("lock_user") or "").strip()

    def _apply_editor_access_state(self) -> None:
        cloud_state = self._current_cloud_state()
        lock_user = self._current_lock_user()
        self.current_read_only = bool(
            self.current_video_summary and self.current_video_summary.get("read_only")
        )

        state_suffix = ""
        if cloud_state == "checked_in":
            state_suffix = "  |  Checked in (read-only)"
        elif cloud_state == "checked_out_self":
            state_suffix = "  |  Checked out"
        elif cloud_state == "checked_out_other":
            locker = lock_user or "someone else"
            state_suffix = f"  |  Locked by {locker} (read-only)"

        if self.current_project:
            self.video_label_var.set(
                f"{self.current_project.title}  |  "
                f"{self.current_project.channel or 'Unknown channel'}  |  "
                f"{len(self.current_project.segments)} phrases"
                f"{state_suffix}"
            )
        self.caption_text_label_var.set(
            "Caption Text (read-only)" if self.current_read_only else "Caption Text (editable)"
        )

        text_state = "disabled" if self.current_read_only else "normal"
        self.preview_text.config(state="normal")
        if self.current_read_only:
            self.preview_text.config(state="disabled")

        button_state = ["disabled"] if self.current_read_only else ["!disabled"]
        check_state = ["disabled"] if self.current_read_only else ["!disabled"]

        self._start_entry.state(["disabled"] if self.current_read_only else ["!disabled"])
        self._end_entry.state(["disabled"] if self.current_read_only else ["!disabled"])
        self._apply_btn.state(button_state)
        self._reset_btn.state(button_state)
        self._enabled_check.state(check_state)
        self._reviewed_check.state(check_state)
        self._add_sentence_btn.state(button_state)
        self._split_btn.state(button_state)
        self._combine_btn.state(button_state)
        if hasattr(self, "_save_progress_btn"):
            self._save_progress_btn.state(button_state)

    def _ensure_project_editable(self, action: str = "edit this title") -> bool:
        if not self.current_read_only:
            return True
        cloud_state = self._current_cloud_state()
        if cloud_state == "checked_in":
            detail = "It is checked in and read-only locally."
        elif cloud_state == "checked_out_other":
            locker = self._current_lock_user() or "someone else"
            detail = f"It is checked out by {locker} and read-only locally."
        else:
            detail = "It is read-only right now."
        self._set_status(f"Cannot {action}. {detail}")
        return False

    def _choose_workspace(self) -> None:
        self._save_current_project()
        selected = filedialog.askdirectory(initialdir=str(self.workspace))
        if not selected:
            return
        self.workspace = Path(selected).resolve()
        self.workspace_var.set(str(self.workspace))
        self.dirs = ensure_app_dirs(self.workspace)
        self.current_project = None
        self.current_video_summary = None
        self.current_read_only = False
        self.current_segment_index = None
        self.view_start_s = 0.0
        self.view_span_s = DEFAULT_VIEW_SPAN_S
        self.refresh_video_list(auto_open=False)
        self._set_status(f"Loaded workspace: {self.workspace}")

    def _run_in_worker(
        self,
        start_message: str,
        worker: Any,
        on_done: Any,
        *,
        job_key: str | None = None,
        allow_duplicate_job: bool = False,
    ) -> bool:
        if self.worker_thread and self.worker_thread.is_alive():
            if (
                not allow_duplicate_job
                and job_key is not None
                and job_key == self.active_job_key
            ):
                return False
            messagebox.showinfo("Busy", "Another background task is still running.")
            return False

        self.worker_done_callback = on_done
        self.active_job_key = job_key
        self._set_status(start_message)

        def runner() -> None:
            try:
                result = worker()
            except Exception as exc:
                logger.exception("Worker thread failed")
                self.worker_queue.put(("error", str(exc)))
            else:
                self.worker_queue.put(("done", result))

        self.worker_thread = threading.Thread(target=runner, daemon=True)
        self.worker_thread.start()
        return True

    def _poll_worker_queue(self) -> None:
        self._poll_auto_sync_results()
        try:
            while True:
                kind, payload = self.worker_queue.get_nowait()
                if kind == "status":
                    self._set_status(str(payload))
                elif kind == "error":
                    self.worker_thread = None
                    self.active_job_key = None
                    self._set_status(str(payload))
                    self.worker_done_callback = None
                    messagebox.showerror("Action Failed", str(payload))
                elif kind == "done":
                    self.worker_thread = None
                    self.active_job_key = None
                    callback = self.worker_done_callback
                    self.worker_done_callback = None
                    if callback is not None:
                        try:
                            callback(payload)
                        except Exception:
                            logger.exception("Callback failed after worker completed")
                            self._set_status("Internal error — see log.")
                            messagebox.showerror(
                                "Internal Error",
                                f"An error occurred while loading results:\n\n"
                                f"{traceback.format_exc()}",
                            )
        except queue.Empty:
            pass
        finally:
            try:
                if self.root.winfo_exists():
                    self.root.after(150, self._poll_worker_queue)
            except tk.TclError:
                return

    def _poll_auto_sync_results(self) -> None:
        try:
            while True:
                self._handle_auto_sync_result(self._auto_sync_result_queue.get_nowait())
        except queue.Empty:
            return

    def _queue_auto_sync(self, workspace: Path, video_id: str, updated_at: float) -> None:
        if not (_HAS_CLOUD and _HAS_B2):
            return
        self._auto_sync_queue.put((workspace.resolve(), video_id, float(updated_at or 0.0)))
        with self._auto_sync_lock:
            if self._auto_sync_thread and self._auto_sync_thread.is_alive():
                return
            self._auto_sync_thread = threading.Thread(
                target=self._auto_sync_loop,
                name="cloud-auto-sync",
                daemon=True,
            )
            self._auto_sync_thread.start()

    def _auto_sync_loop(self) -> None:
        while True:
            batch = self._dequeue_auto_sync_batch()
            if not batch:
                with self._auto_sync_lock:
                    if self._auto_sync_queue.empty():
                        self._auto_sync_thread = None
                        return
                continue

            for workspace, video_id, target_updated_at in batch:
                self._auto_sync_result_queue.put(
                    self._auto_sync_one(workspace, video_id, target_updated_at)
                )

    def _dequeue_auto_sync_batch(self) -> list[tuple[Path, str, float]]:
        try:
            first = self._auto_sync_queue.get(timeout=AUTO_SYNC_IDLE_TIMEOUT_S)
        except queue.Empty:
            return []

        pending: dict[tuple[str, str], tuple[Path, str, float]] = {
            (str(first[0]), first[1]): first,
        }
        while True:
            try:
                workspace, video_id, updated_at = self._auto_sync_queue.get_nowait()
            except queue.Empty:
                break
            key = (str(workspace), video_id)
            previous = pending.get(key)
            if previous is None or float(updated_at or 0.0) >= previous[2]:
                pending[key] = (workspace, video_id, float(updated_at or 0.0))
        return list(pending.values())

    def _auto_sync_one(
        self,
        workspace: Path,
        video_id: str,
        target_updated_at: float,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "workspace": str(workspace),
            "video_id": video_id,
            "target_updated_at": float(target_updated_at or 0.0),
            "outcome": "skipped",
            "reason": "",
        }

        try:
            config = load_b2_config(workspace)
            if config is None or not getattr(config, "is_valid", lambda: False)():
                result["reason"] = "cloud settings are incomplete"
                return result
            if not getattr(config, "has_identity", lambda: False)():
                result["reason"] = "cloud user id is missing"
                return result

            local_project_path = project_path(workspace, video_id)
            if not local_project_path.exists():
                result["reason"] = "local project file is missing"
                return result

            project = load_project(local_project_path)
            result["title"] = project.title
            result["channel"] = project.channel
            project_updated_at = float(getattr(project, "updated_at", 0.0) or 0.0)
            result["updated_at"] = project_updated_at

            state = load_cloud_state(workspace)
            last_synced_updated_at = float(
                state.get(video_id, {}).get("last_synced_updated_at") or 0.0
            )
            if project_updated_at <= last_synced_updated_at + 1e-6:
                result["outcome"] = "unchanged"
                return result

            store = B2CloudStore(config)
            lock = store.get_lock(video_id)
            if not lock:
                result["outcome"] = "checked_in"
                return result
            if not lock_belongs_to(lock, config):
                result["outcome"] = "lock_lost"
                result["lock_user"] = str(lock.get("user") or "").strip()
                result["lock_user_id"] = str(lock.get("user_id") or "").strip()
                result["lock_role"] = str(lock.get("role") or "").strip()
                return result

            import tempfile

            with tempfile.NamedTemporaryFile(suffix=".asr", delete=False) as tf:
                tmp_path = Path(tf.name)
            try:
                packed = pack_asr(workspace, [project], tmp_path)
                if packed == 0:
                    result["reason"] = "project could not be packed"
                    return result
                store.upload_asr(
                    video_id,
                    tmp_path.read_bytes(),
                    title=project.title,
                    channel=project.channel,
                )
            finally:
                tmp_path.unlink(missing_ok=True)

            store.write_audit_safe(
                "auto_sync",
                video_id,
                details={"updated_at": project_updated_at},
            )
            result["outcome"] = "synced"
            result["lock_user"] = actor_name_for_config(config)
            result["lock_user_id"] = config.user_id.strip()
            result["lock_role"] = config.normalized_role()
            return result
        except Exception as exc:
            logger.exception("Cloud auto-sync failed for %s", video_id)
            result["outcome"] = "error"
            result["reason"] = str(exc)
            return result

    def _handle_auto_sync_result(self, result: dict[str, Any]) -> None:
        workspace = Path(str(result.get("workspace") or self.workspace)).resolve()
        video_id = str(result.get("video_id") or "").strip()
        if not video_id:
            return

        outcome = str(result.get("outcome") or "").strip()
        current_workspace = workspace == self.workspace
        title = str(result.get("title") or "").strip() or video_id
        needs_refresh = False

        if outcome == "synced":
            update_cloud_state_entry(
                workspace,
                video_id,
                cloud_state="checked_out_self",
                lock_user=str(result.get("lock_user") or ""),
                lock_user_id=str(result.get("lock_user_id") or ""),
                lock_role=str(result.get("lock_role") or ""),
                title=str(result.get("title") or ""),
                channel=str(result.get("channel") or ""),
                last_synced_updated_at=float(result.get("updated_at") or 0.0),
                uploaded_at=time.time(),
            )
            if current_workspace:
                self._set_status(f"Auto-synced {title} at {time.strftime('%H:%M:%S')}.")
            return

        if outcome == "lock_lost":
            update_cloud_state_entry(
                workspace,
                video_id,
                cloud_state="checked_out_other",
                lock_user=str(result.get("lock_user") or ""),
                lock_user_id=str(result.get("lock_user_id") or ""),
                lock_role=str(result.get("lock_role") or ""),
            )
            needs_refresh = True
            if current_workspace:
                locker = str(result.get("lock_user") or "").strip() or "another user"
                self._set_status(
                    f"Cloud lock changed for {title}. It is now read-only locally ({locker})."
                )
        elif outcome == "checked_in":
            update_cloud_state_entry(
                workspace,
                video_id,
                cloud_state="checked_in",
                lock_user="",
                lock_user_id="",
                lock_role="",
            )
            needs_refresh = True
            if current_workspace:
                self._set_status(
                    f"{title} is checked in on cloud. The local copy is now read-only."
                )
        elif outcome == "error" and current_workspace:
            self._set_status(f"Auto-sync failed for {title}: {result.get('reason') or 'unknown error'}")

        if needs_refresh and current_workspace:
            selected_id = self.current_project.video_id if self.current_project else None
            self.refresh_video_list(select_video_id=selected_id, auto_open=False)

    def refresh_video_list(
        self, select_video_id: str | None = None, auto_open: bool = False
    ) -> None:
        previous = select_video_id
        if previous is None:
            selected = self.video_tree.selection()
            previous = selected[0] if selected else None

        self.video_summaries = discover_videos(self.workspace)
        self._video_summary_by_id = {
            item["video_id"]: item for item in self.video_summaries
        }
        existing_ids = {item["video_id"] for item in self.video_summaries}

        for item in self.video_tree.get_children():
            self.video_tree.delete(item)

        for summary in self.video_summaries:
            segment_count = summary["segment_count"]
            row_tag = ""
            match summary.get("cloud_state"):
                case "checked_out_self":
                    row_tag = "checked_out"
                case "checked_in":
                    row_tag = "checked_in"
                case "checked_out_other":
                    row_tag = "locked"
            self.video_tree.insert(
                "",
                "end",
                iid=summary["video_id"],
                text=summary["title"],
                values=(
                    summary["channel"],
                    summary.get("state_label", ""),
                    segment_count if segment_count is not None else "-",
                ),
                tags=(row_tag,) if row_tag else (),
            )

        if self.current_project:
            self.current_video_summary = self._video_summary_by_id.get(self.current_project.video_id)
            self._apply_editor_access_state()

        target_id: str | None = None
        if previous and previous in existing_ids:
            target_id = previous
        elif self.video_summaries:
            target_id = self.video_summaries[0]["video_id"]

        if target_id:
            self._suspend_video_select = True
            self.video_tree.selection_set(target_id)
            self.video_tree.focus(target_id)
            self.video_tree.see(target_id)
            # Defer flag reset so the queued <<TreeviewSelect>> event is still suppressed
            self.root.after_idle(self._unsuspend_video_select)
            if auto_open:
                self.root.after_idle(lambda: self._open_video_by_id(target_id))
        else:
            self.current_project = None
            self.current_video_summary = None
            self.current_read_only = False
            self.current_segment_index = None
            self.video_label_var.set("No video loaded.")
            self.segment_label_var.set("No phrase selected.")
            self._set_text_preview("")
            self._populate_segment_tree()
            self._redraw_waveform()
            self._apply_editor_access_state()

    def _unsuspend_video_select(self) -> None:
        self._suspend_video_select = False

    def _on_video_selected(self, _event: Any = None) -> None:
        if self._suspend_video_select:
            return
        selection = self.video_tree.selection()
        if selection:
            self._open_video_by_id(selection[0])

    def _delete_local_copy(self) -> None:
        selection = self.video_tree.selection()
        if not selection:
            messagebox.showinfo("No Video Selected", "Select a title to delete locally.")
            return

        video_id = selection[0]
        summary = self._video_summary_by_id.get(video_id)
        if not summary:
            messagebox.showerror("Missing title", f"Could not find local details for {video_id}.")
            return

        cloud_state = str(summary.get("cloud_state") or "").strip()
        if cloud_state in {"checked_out_self", "checked_out_other"}:
            if cloud_state == "checked_out_self":
                detail = "It is currently checked out by you. Check it in or delete it from cloud first."
            else:
                locker = str(summary.get("lock_user") or "").strip() or "someone else"
                detail = f"It is currently checked out by {locker}."
            messagebox.showwarning("Cannot delete local copy", detail, parent=self.root)
            return

        title = str(summary.get("title") or video_id)
        if cloud_state == "checked_in":
            prompt = (
                f"Delete the local copy of '{title}' from this computer?\n\n"
                "The checked-in cloud copy will remain available."
            )
        else:
            prompt = (
                f"Delete the local title '{title}' from this computer?\n\n"
                "This title is not checked in on cloud."
            )
        if not messagebox.askyesno("Delete Local Copy", prompt, parent=self.root):
            return

        fallback_id: str | None = None
        ids = [item["video_id"] for item in self.video_summaries]
        if video_id in ids:
            index = ids.index(video_id)
            if index + 1 < len(ids):
                fallback_id = ids[index + 1]
            elif index > 0:
                fallback_id = ids[index - 1]

        if self.current_project and self.current_project.video_id == video_id:
            self._playback_stop_internal()
            self.current_project = None
            self.current_video_summary = None
            self.current_segment_index = None
            self.current_read_only = False

        removed = delete_local_title_data(self.workspace, video_id)
        remove_cloud_state_entry(self.workspace, video_id)
        self.refresh_video_list(select_video_id=fallback_id, auto_open=bool(fallback_id))
        self._set_status(f"Deleted local copy of {title}. Removed {len(removed)} item(s).")

    def _open_video_by_id(self, video_id: str) -> None:
        self._save_current_project()
        if self.current_project and self.current_project.video_id == video_id:
            logger.debug("Skipping re-display of already-loaded project %s", video_id)
            return

        def worker() -> VideoProject:
            logger.debug("Worker: opening %s", video_id)
            self.worker_queue.put(("status", f"Opening {video_id}..."))
            project = ensure_project_for_video(self.workspace, video_id)
            logger.debug("Worker: project loaded, %d segments", len(project.segments))
            return project

        def on_done(project: VideoProject) -> None:
            self._display_project(project)

        self._run_in_worker(
            f"Opening {video_id}...",
            worker,
            on_done,
            job_key=f"open:{video_id}",
        )

    def _display_project(self, project: VideoProject) -> None:
        if getattr(self, '_displaying', False):
            logger.debug("_display_project: skipping re-entrant call for %s", project.video_id)
            return
        self._displaying = True
        try:
            self._display_project_inner(project)
        finally:
            self._displaying = False

    def _display_project_inner(self, project: VideoProject) -> None:
        logger.debug("_display_project: %s (%d segments)", project.video_id, len(project.segments))
        self.current_project = project
        self.current_video_summary = self._video_summary_by_id.get(project.video_id, {})
        self.view_span_s = DEFAULT_VIEW_SPAN_S
        self.view_start_s = 0.0
        self.text_dirty = False
        logger.debug("_display_project: populating segment tree")
        self._populate_segment_tree()
        if project.segments:
            logger.debug("_display_project: selecting first segment")
            self._select_segment_by_index(0)
        else:
            self.current_segment_index = None
            self.segment_label_var.set("No phrases yet. Click Add Sentence to create one.")
            self.start_var.set("")
            self.end_var.set("")
            self.enabled_var.set(True)
            self.reviewed_var.set(False)
            self._set_text_preview("")
            self._redraw_waveform()
        self._apply_editor_access_state()
        state_label = str((self.current_video_summary or {}).get("state_label") or "").strip()
        if state_label:
            self._set_status(f"Loaded {project.title} [{state_label}]")
        else:
            self._set_status(f"Loaded {project.title}")

    def _segment_row_tag(self, segment: "EditableSegment") -> str:
        """Return the single treeview tag that encodes both enabled and reviewed state."""
        if segment.enabled and segment.reviewed:
            return "reviewed"
        if not segment.enabled and segment.reviewed:
            return "disabled_reviewed"
        if not segment.enabled:
            return "disabled"
        return "normal"

    def _populate_segment_tree(self) -> None:
        for item in self.segment_tree.get_children():
            self.segment_tree.delete(item)

        if not self.current_project:
            return

        for index, segment in enumerate(self.current_project.segments):
            self.segment_tree.insert(
                "",
                "end",
                iid=str(index),
                text=segment.text,
                values=(
                    self._format_s(segment.start_s),
                    self._format_s(segment.end_s),
                    "✓" if segment.enabled  else "✗",
                    "✓" if segment.reviewed else "☐",
                ),
                tags=(self._segment_row_tag(segment),),
            )

    def _unsuspend_segment_select(self) -> None:
        self._suspend_segment_select = False

    def _on_segment_selected(self, _event: Any = None) -> None:
        if self._suspend_segment_select:
            return
        selection = self.segment_tree.selection()
        logger.debug("_on_segment_selected: selection=%s, current=%s", selection, self.current_segment_index)
        if not selection:
            return
        # Stash selection so Combine can read it even after focus moves away
        self._last_segment_selection = selection
        # Multi-select: update waveform to first item but don't reset selection
        if len(selection) > 1:
            logger.debug("_on_segment_selected: multi-select (%d items), preserving", len(selection))
            first = int(selection[0])
            if first != self.current_segment_index:
                self._select_segment_by_index_no_sync(first)
            return
        # Single select: normal navigation
        first = int(selection[0])
        if first != self.current_segment_index:
            self._select_segment_by_index(first)

    def _on_segment_tree_click(self, event: tk.Event[Any]) -> None:
        """Toggle the Reviewed flag when the Reviewed column cell is clicked."""
        col = self.segment_tree.identify_column(event.x)   # e.g. "#4"
        row = self.segment_tree.identify_row(event.y)       # iid string, or ""
        if not row or col != "#4":   # #4 = 4th data column = "reviewed"
            return
        if not self.current_project:
            return
        if not self._ensure_project_editable("change the reviewed state"):
            return
        index = int(row)
        if index < 0 or index >= len(self.current_project.segments):
            return
        segment = self.current_project.segments[index]
        segment.reviewed = not segment.reviewed
        # Sync the checkbox widget if this is also the currently selected segment
        if index == self.current_segment_index:
            self.reviewed_var.set(segment.reviewed)
        # Refresh just this row and persist
        item_id = str(index)
        if self.segment_tree.exists(item_id):
            values = self.segment_tree.item(item_id, "values")
            self.segment_tree.item(
                item_id,
                values=(
                    values[0],
                    values[1],
                    values[2],
                    "✓" if segment.reviewed else "☐",
                ),
                tags=(self._segment_row_tag(segment),),
            )
        self._save_current_project()

    def _select_segment_by_index(self, index: int) -> None:
        self._select_segment_by_index_no_sync(index)
        self._sync_segment_selection()

    def _select_segment_by_index_no_sync(self, index: int) -> None:
        """Select a segment and update the UI, but don't touch the tree selection."""
        if not self.current_project:
            return
        if index < 0 or index >= len(self.current_project.segments):
            return
        if self.current_segment_index is not None and index != self.current_segment_index:
            text_changed = self._commit_current_segment_text()
            if text_changed:
                self._save_current_project()

        self._playback_stop()
        self.current_segment_index = index
        segment = self.current_project.segments[index]
        self.start_var.set(f"{segment.start_s:.3f}")
        self.end_var.set(f"{segment.end_s:.3f}")
        self.enabled_var.set(segment.enabled)
        self.reviewed_var.set(segment.reviewed)
        self.segment_label_var.set(
            f"Phrase {index + 1}/{len(self.current_project.segments)}  |  "
            f"{segment.end_s - segment.start_s:.2f}s"
        )
        self._set_text_preview(segment.text)
        self._focus_current_segment()
        self._redraw_waveform()

    def _sync_segment_selection(self) -> None:
        if self.current_segment_index is None:
            return
        segment_id = str(self.current_segment_index)
        if not self.segment_tree.exists(segment_id):
            return
        self._suspend_segment_select = True
        self.segment_tree.selection_set(segment_id)
        self.segment_tree.focus(segment_id)
        self.segment_tree.see(segment_id)
        # Defer flag reset so the queued <<TreeviewSelect>> event is still suppressed
        self.root.after_idle(self._unsuspend_segment_select)

    def _focus_current_segment(self) -> None:
        if not self.current_project or self.current_segment_index is None:
            return

        segment = self.current_project.segments[self.current_segment_index]
        duration = max(self.current_project.duration, segment.end_s)
        segment_span = max(MIN_SEGMENT_LEN_S, segment.end_s - segment.start_s)
        padding = max(1.25, min(3.5, segment_span * 0.8))
        self.view_span_s = max(self.view_span_s, segment_span + padding * 2)
        if duration > 0:
            self.view_span_s = min(self.view_span_s, max(2.0, duration))
        self.view_start_s = max(0.0, segment.start_s - padding)
        if duration > 0 and self.view_start_s + self.view_span_s > duration:
            self.view_start_s = max(0.0, duration - self.view_span_s)

    def _set_text_preview(self, text: str) -> None:
        self._setting_text = True
        was_disabled = str(self.preview_text.cget("state")) == "disabled"
        if was_disabled:
            self.preview_text.config(state="normal")
        self.preview_text.delete("1.0", "end")
        self.preview_text.insert("1.0", text)
        self.preview_text.edit_modified(False)
        if was_disabled:
            self.preview_text.config(state="disabled")
        self._setting_text = False

    def _normalize_sentence_text(self, text: str) -> str:
        return " ".join(text.replace("\r", " ").replace("\n", " ").split())

    def _on_text_modified(self, _event: Any = None) -> None:
        if self._setting_text:
            self.preview_text.edit_modified(False)
            return
        if not self.preview_text.edit_modified():
            return
        self.text_dirty = True
        self.preview_text.edit_modified(False)
        if self.current_project and self.current_segment_index is not None:
            self._set_status("Text edited. Click Save Progress or switch phrases to persist the spelling correction.")

    def _commit_current_segment_text(self) -> bool:
        if not self.current_project or self.current_segment_index is None:
            return False
        if self.current_read_only:
            self.text_dirty = False
            return False
        segment = self.current_project.segments[self.current_segment_index]
        new_text = self._normalize_sentence_text(self.preview_text.get("1.0", "end"))
        if not new_text:
            self._set_text_preview(segment.text)
            self.text_dirty = False
            self._set_status("Empty sentence text is not allowed. The previous text was restored.")
            return False
        if new_text == segment.text:
            self.text_dirty = False
            return False
        segment.text = new_text
        self.text_dirty = False
        self._mark_current_reviewed()
        self._sync_segment_ui()
        return True

    def _select_previous_segment(self) -> None:
        if self.current_segment_index is not None:
            self._select_segment_by_index(self.current_segment_index - 1)

    def _select_next_segment(self) -> None:
        if self.current_project and self.current_segment_index is not None:
            self._select_segment_by_index(self.current_segment_index + 1)

    def _zoom(self, factor: float) -> None:
        if not self.current_project:
            return
        duration = max(self.current_project.duration, 2.0)
        range_start, range_end = self._visible_range()
        center = (range_start + range_end) / 2.0
        new_span = max(2.0, min(duration, self.view_span_s * factor))
        self.view_span_s = new_span
        self.view_start_s = max(0.0, center - new_span / 2.0)
        if self.view_start_s + new_span > duration:
            self.view_start_s = max(0.0, duration - new_span)
        self._redraw_waveform()

    def _apply_time_entries(self) -> None:
        if not self.current_project or self.current_segment_index is None:
            return
        if not self._ensure_project_editable("change timings"):
            return
        self._commit_current_segment_text()
        try:
            start_s = float(self.start_var.get().strip())
            end_s = float(self.end_var.get().strip())
        except ValueError:
            messagebox.showerror("Invalid Time", "Start and end times must be numbers.")
            return

        duration = max(self.current_project.duration, 0.0)
        start_s = max(0.0, start_s)
        if duration > 0:
            start_s = min(start_s, max(0.0, duration - MIN_SEGMENT_LEN_S))
        end_limit = duration if duration > 0 else max(start_s + MIN_SEGMENT_LEN_S, end_s)
        end_s = min(max(start_s + MIN_SEGMENT_LEN_S, end_s), end_limit)

        segment = self.current_project.segments[self.current_segment_index]
        segment.start_s = start_s
        segment.end_s = end_s
        self.start_var.set(f"{segment.start_s:.3f}")
        self.end_var.set(f"{segment.end_s:.3f}")
        self._mark_current_reviewed()
        self._save_current_project()
        self._refresh_playback_data()
        self._sync_segment_ui()
        self._focus_current_segment()
        self._redraw_waveform()

    def _reset_current_segment(self) -> None:
        if not self.current_project or self.current_segment_index is None:
            return
        if not self._ensure_project_editable("reset this segment"):
            return
        self._commit_current_segment_text()
        segment = self.current_project.segments[self.current_segment_index]
        segment.start_s = segment.original_start_s
        segment.end_s = segment.original_end_s
        self.start_var.set(f"{segment.start_s:.3f}")
        self.end_var.set(f"{segment.end_s:.3f}")
        self._save_current_project()
        self._refresh_playback_data()
        self._sync_segment_ui()
        self._focus_current_segment()
        self._redraw_waveform()

    def _toggle_current_segment_enabled(self) -> None:
        if not self.current_project or self.current_segment_index is None:
            return
        if not self._ensure_project_editable("change export inclusion"):
            return
        self._commit_current_segment_text()
        segment = self.current_project.segments[self.current_segment_index]
        segment.enabled = self.enabled_var.get()
        self._save_current_project()
        self._sync_segment_ui()

    def _toggle_current_segment_reviewed(self) -> None:
        """Manual toggle of the Reviewed checkbox."""
        if not self.current_project or self.current_segment_index is None:
            return
        if not self._ensure_project_editable("change the reviewed state"):
            return
        segment = self.current_project.segments[self.current_segment_index]
        segment.reviewed = self.reviewed_var.get()
        self._save_current_project()
        self._sync_segment_ui()

    def _mark_current_reviewed(self) -> None:
        """Auto-mark the current segment as reviewed (called on play / edit)."""
        if not self.current_project or self.current_segment_index is None:
            return
        if self.current_read_only:
            return
        segment = self.current_project.segments[self.current_segment_index]
        if segment.reviewed:
            return  # already reviewed — nothing to do
        segment.reviewed = True
        self.reviewed_var.set(True)
        self._sync_segment_ui()

    def _save_current_project(self, show_feedback: bool = False) -> bool:
        if not self.current_project:
            return False
        if self.current_read_only:
            return False
        self._commit_current_segment_text()
        self._persist_project(show_feedback=show_feedback)
        return True

    def _persist_project(self, show_feedback: bool = False) -> None:
        if not self.current_project:
            return
        if self.current_read_only:
            return
        save_project(
            project_path(self.workspace, self.current_project.video_id),
            self.current_project,
        )
        if self._current_cloud_state() == "checked_out_self":
            self._queue_auto_sync(
                self.workspace,
                self.current_project.video_id,
                float(self.current_project.updated_at or 0.0),
            )
        if show_feedback:
            self._set_status(
                f"Saved progress for {self.current_project.title} at {time.strftime('%H:%M:%S')}."
            )

    def _sync_segment_ui(self) -> None:
        if not self.current_project or self.current_segment_index is None:
            return
        segment = self.current_project.segments[self.current_segment_index]
        item_id = str(self.current_segment_index)
        if self.segment_tree.exists(item_id):
            self.segment_tree.item(
                item_id,
                text=segment.text,
                values=(
                    self._format_s(segment.start_s),
                    self._format_s(segment.end_s),
                    "✓" if segment.enabled  else "✗",
                    "✓" if segment.reviewed else "☐",
                ),
            tags=(self._segment_row_tag(segment),),
        )

    def _add_sentence(self) -> None:
        if not self.current_project:
            self._set_status("Load a title first.")
            return
        if not self._ensure_project_editable("add a sentence"):
            return

        insert_after = self.current_segment_index
        insert_at = len(self.current_project.segments)
        if insert_after is not None and 0 <= insert_after < len(self.current_project.segments):
            insert_at = insert_after + 1

        segment = build_manual_segment(
            self.current_project,
            after_index=insert_after,
        )
        self.current_project.segments.insert(insert_at, segment)
        self._reindex_segments()
        self._persist_project()
        self._populate_segment_tree()
        self._select_segment_by_index(insert_at)
        self._set_status("Added a new sentence. Edit the text and adjust the timing as needed.")

    def _visible_range(self) -> tuple[float, float]:
        if not self.current_project:
            return 0.0, DEFAULT_VIEW_SPAN_S
        duration = max(self.current_project.duration, DEFAULT_VIEW_SPAN_S)
        span = max(2.0, min(self.view_span_s, duration))
        start = max(0.0, min(self.view_start_s, max(0.0, duration - span)))
        end = start + span
        return start, end

    def _time_to_x(self, time_s: float) -> float:
        start_s, end_s = self._visible_range()
        width = max(1, self.waveform_canvas.winfo_width())
        if end_s <= start_s:
            return 0.0
        return (time_s - start_s) / (end_s - start_s) * width

    def _x_to_time(self, x: float) -> float:
        start_s, end_s = self._visible_range()
        width = max(1, self.waveform_canvas.winfo_width())
        ratio = min(1.0, max(0.0, x / width))
        return start_s + ratio * (end_s - start_s)

    def _redraw_waveform(self) -> None:
        self.waveform_canvas.delete("all")

        width = self.waveform_canvas.winfo_width()
        height = self.waveform_canvas.winfo_height() or WAVEFORM_HEIGHT
        if width <= 4 or height <= 4:
            return

        self.waveform_canvas.create_rectangle(
            0, 0, width, height, fill="#0f172a", outline=""
        )
        self.waveform_canvas.create_line(
            0, height / 2, width, height / 2, fill="#1f2937"
        )

        if not self.current_project:
            self.waveform_canvas.create_text(
                width / 2,
                height / 2,
                text="Select or download a video to review.",
                fill="#cbd5e1",
                font=("Segoe UI", 12),
            )
            return

        working_audio = resolve_path(self.current_project.working_audio_file, self.workspace)
        if not working_audio.exists():
            self.waveform_canvas.create_text(
                width / 2,
                height / 2,
                text="Working WAV is missing. Re-open the video after installing ffmpeg.",
                fill="#fca5a5",
                font=("Segoe UI", 12),
            )
            return

        range_start, range_end = self._visible_range()
        peaks = self._read_waveform_peaks(working_audio, range_start, range_end, width)
        center_y = height / 2.0
        amplitude_height = (height / 2.0) - 28
        for x, peak in enumerate(peaks):
            line_height = max(1.0, peak * amplitude_height)
            self.waveform_canvas.create_line(
                x,
                center_y - line_height,
                x,
                center_y + line_height,
                fill="#7dd3fc",
            )

        self.waveform_canvas.create_text(
            10,
            height - 10,
            anchor="sw",
            text=f"{range_start:.2f}s",
            fill="#cbd5e1",
        )
        self.waveform_canvas.create_text(
            width - 10,
            height - 10,
            anchor="se",
            text=f"{range_end:.2f}s",
            fill="#cbd5e1",
        )

        if self.current_segment_index is None:
            return

        segment = self.current_project.segments[self.current_segment_index]
        start_x = self._time_to_x(segment.start_s)
        end_x = self._time_to_x(segment.end_s)
        original_start_x = self._time_to_x(segment.original_start_s)
        original_end_x = self._time_to_x(segment.original_end_s)

        region_left = max(0, min(width, start_x))
        region_right = max(0, min(width, end_x))
        self.waveform_canvas.create_rectangle(
            region_left,
            18,
            region_right,
            height - 18,
            fill="#1e3a5f",
            stipple="gray25",
            outline="",
        )

        for x in (original_start_x, original_end_x):
            self.waveform_canvas.create_line(
                x,
                0,
                x,
                height,
                fill="#f59e0b",
                dash=(4, 4),
            )

        self.waveform_canvas.create_line(
            start_x, 0, start_x, height, fill="#ef4444", width=3
        )
        self.waveform_canvas.create_line(
            end_x, 0, end_x, height, fill="#22c55e", width=3
        )
        self.waveform_canvas.create_text(
            10,
            10,
            anchor="nw",
            text=f"Start {segment.start_s:.3f}s   End {segment.end_s:.3f}s   "
            f"Original {segment.original_start_s:.3f}s - {segment.original_end_s:.3f}s",
            fill="#e2e8f0",
            font=("Segoe UI", 10, "bold"),
        )
        self._draw_playback_indicator()

    def _read_waveform_peaks(
        self, wav_path: Path, start_s: float, end_s: float, pixel_count: int
    ) -> list[float]:
        pixel_count = max(1, pixel_count)
        with wave.open(str(wav_path), "rb") as wav_file:
            frame_rate = wav_file.getframerate()
            frame_count = wav_file.getnframes()
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            if frame_rate <= 0 or frame_count <= 0:
                return [0.0] * pixel_count

            start_frame = max(0, min(frame_count - 1, int(start_s * frame_rate)))
            end_frame = max(start_frame + 1, min(frame_count, int(end_s * frame_rate)))
            wav_file.setpos(start_frame)
            raw = wav_file.readframes(end_frame - start_frame)

        if sample_width == 2:
            samples = array.array("h")
            samples.frombytes(raw)
            max_abs = 32768.0
        elif sample_width == 1:
            values = [sample - 128 for sample in raw]
            samples = array.array("h", values)
            max_abs = 128.0
        else:
            return [0.0] * pixel_count

        if channels > 1:
            samples = samples[::channels]
        if not samples:
            return [0.0] * pixel_count

        bucket_size = max(1, len(samples) // pixel_count)
        peaks: list[float] = []
        for pixel in range(pixel_count):
            start_index = pixel * bucket_size
            end_index = min(len(samples), start_index + bucket_size)
            if start_index >= len(samples):
                peaks.append(0.0)
                continue
            peak = max(abs(value) for value in samples[start_index:end_index]) / max_abs
            peaks.append(min(1.0, peak))
        return peaks

    # ---- Audio playback ----

    def _current_playback_speed(self) -> float:
        speed = clamp_playback_speed(self._playback_speed_var.get())
        self._playback_speed_label_var.set(playback_speed_text(speed))
        return speed

    def _on_playback_speed_drag(self, _value: str) -> None:
        self._current_playback_speed()

    def _on_playback_speed_commit(self, _event: tk.Event[Any]) -> None:
        raw_speed = float(self._playback_speed_var.get())
        speed = self._current_playback_speed()
        if abs(raw_speed - speed) > 1e-6:
            self._playback_speed_var.set(speed)
        if self._playback_stream is None:
            return
        was_paused = self._playback_paused
        self._playback_stop_internal()
        if was_paused:
            self._playback_status_var.set(
                playback_status_text("Ready", speed, loop=self._loop_var.get())
            )
            self._redraw_waveform()
            return
        self._playback_play()

    def _current_playback_time_s(self) -> float | None:
        if self._playback_stream is None:
            return None
        bytes_per_frame = self._playback_channels * self._playback_sampwidth
        if bytes_per_frame <= 0 or self._playback_rate <= 0:
            return None
        offset = max(0, min(self._playback_offset, len(self._playback_data)))
        frames_played = offset / float(bytes_per_frame)
        current_time_s = self._playback_segment_start_s + (frames_played / float(self._playback_rate))
        return min(self._playback_segment_end_s, current_time_s)

    def _draw_playback_indicator(self) -> None:
        self.waveform_canvas.delete("playhead")
        current_time_s = self._current_playback_time_s()
        if current_time_s is None:
            return
        x = self._time_to_x(current_time_s)
        height = self.waveform_canvas.winfo_height() or WAVEFORM_HEIGHT
        self.waveform_canvas.create_line(
            x, 0, x, height, fill="#fbbf24", width=2, tags="playhead"
        )
        self.waveform_canvas.create_polygon(
            x - 6,
            0,
            x + 6,
            0,
            x,
            10,
            fill="#fbbf24",
            outline="",
            tags="playhead",
        )

    def _clear_playback_indicator(self) -> None:
        if self._playback_indicator_job is not None:
            try:
                self.root.after_cancel(self._playback_indicator_job)
            except tk.TclError:
                pass
            self._playback_indicator_job = None
        self.waveform_canvas.delete("playhead")

    def _schedule_playback_indicator(self) -> None:
        self._clear_playback_indicator()
        self._update_playback_indicator()

    def _update_playback_indicator(self) -> None:
        self._draw_playback_indicator()
        if self._playback_stream is not None and self.root.winfo_exists():
            self._playback_indicator_job = self.root.after(40, self._update_playback_indicator)
        else:
            self._playback_indicator_job = None

    def _read_segment_audio(self) -> tuple[bytes, int, int, int] | None:
        """Read raw PCM bytes for the current segment from the working WAV."""
        if not self.current_project or self.current_segment_index is None:
            return None
        segment = self.current_project.segments[self.current_segment_index]
        working_audio = resolve_path(self.current_project.working_audio_file, self.workspace)
        if not working_audio.exists():
            return None
        with wave.open(str(working_audio), "rb") as wf:
            rate = wf.getframerate()
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            total_frames = wf.getnframes()
            start_frame = max(0, int(segment.start_s * rate))
            end_frame = min(total_frames, int(segment.end_s * rate))
            if end_frame <= start_frame:
                return None
            wf.setpos(start_frame)
            data = wf.readframes(end_frame - start_frame)
        return data, rate, channels, sampwidth

    def _playback_play(self) -> None:
        if not _HAS_SOUNDDEVICE:
            return
        speed = self._current_playback_speed()
        # If paused, resume
        if self._playback_paused and self._playback_stream is not None:
            self._playback_paused = False
            self._playback_status_var.set(
                playback_status_text("Playing...", speed, loop=self._playback_loop)
            )
            self._schedule_playback_indicator()
            return
        # Stop any existing playback
        self._playback_stop_internal()
        result = self._read_segment_audio()
        if result is None:
            self._playback_status_var.set("No segment to play")
            return
        if not self.current_project or self.current_segment_index is None:
            return
        self._mark_current_reviewed()
        segment = self.current_project.segments[self.current_segment_index]
        data, rate, channels, sampwidth = result
        self._playback_data = data
        self._playback_offset = 0
        self._playback_paused = False
        self._playback_loop = self._loop_var.get()
        self._playback_rate = rate
        self._playback_channels = channels
        self._playback_sampwidth = sampwidth
        self._playback_segment_start_s = segment.start_s
        self._playback_segment_end_s = segment.end_s
        self._playback_status_var.set(
            playback_status_text("Playing...", speed, loop=self._playback_loop)
        )

        bytes_per_frame = channels * sampwidth

        def _audio_callback(outdata, frames, _time_info, status):
            needed = frames * bytes_per_frame
            if self._playback_paused:
                outdata[:] = b"\x00" * needed
                return
            chunk = b""
            while len(chunk) < needed:
                remaining = self._playback_data[self._playback_offset:]
                take = needed - len(chunk)
                if len(remaining) >= take:
                    chunk += remaining[:take]
                    self._playback_offset += take
                else:
                    chunk += remaining
                    self._playback_offset = 0
                    if not self._playback_loop:
                        # Pad with silence and signal done
                        chunk += b"\x00" * (needed - len(chunk))
                        self.root.after_idle(self._playback_finished)
                        break
            outdata[:] = chunk

        try:
            dtype = "int16" if sampwidth == 2 else "int8" if sampwidth == 1 else "int16"
            self._playback_stream = sd.RawOutputStream(
                samplerate=effective_playback_samplerate(rate, speed),
                channels=channels,
                dtype=dtype,
                blocksize=1024,
                callback=_audio_callback,
            )
            self._playback_stream.start()
            self._schedule_playback_indicator()
        except Exception as exc:
            logger.exception("Playback failed")
            self._playback_stop_internal()
            self._playback_status_var.set(f"Playback error: {exc}")

    def _playback_pause(self) -> None:
        if self._playback_stream is None:
            return
        speed = self._current_playback_speed()
        self._playback_paused = not self._playback_paused
        self._playback_status_var.set(
            playback_status_text("Paused", speed, loop=self._playback_loop)
            if self._playback_paused
            else playback_status_text("Playing...", speed, loop=self._playback_loop)
        )
        if self._playback_paused:
            self._draw_playback_indicator()
        else:
            self._schedule_playback_indicator()

    def _playback_stop(self) -> None:
        self._playback_stop_internal()
        self._playback_status_var.set("")
        self._redraw_waveform()

    def _playback_stop_internal(self) -> None:
        if self._playback_stream is not None:
            try:
                self._playback_stream.stop()
                self._playback_stream.close()
            except Exception:
                pass
            self._playback_stream = None
        self._playback_paused = False
        self._playback_offset = 0
        self._clear_playback_indicator()

    def _playback_finished(self) -> None:
        """Called from audio callback when non-looping playback ends."""
        self._playback_stop_internal()
        self._playback_status_var.set(
            playback_status_text("Finished", self._current_playback_speed())
        )
        self._redraw_waveform()

    def _on_loop_toggled(self) -> None:
        self._playback_loop = self._loop_var.get()
        if self._playback_stream is not None and not self._playback_paused:
            self._playback_status_var.set(
                playback_status_text(
                    "Playing...",
                    self._current_playback_speed(),
                    loop=self._playback_loop,
                )
            )

    def _refresh_playback_data(self) -> None:
        """Re-read segment audio for updated start/end while stream is active."""
        if self._playback_stream is None:
            return
        result = self._read_segment_audio()
        if result is None:
            return
        data, _rate, _channels, _sampwidth = result
        self._playback_data = data
        # Reset offset so playback restarts from the new start boundary
        self._playback_offset = 0
        if self.current_project and self.current_segment_index is not None:
            segment = self.current_project.segments[self.current_segment_index]
            self._playback_segment_start_s = segment.start_s
            self._playback_segment_end_s = segment.end_s
        self._draw_playback_indicator()

    def _on_waveform_press(self, event: tk.Event[Any]) -> None:
        if not self.current_project or self.current_segment_index is None:
            return
        if self.current_read_only:
            return
        # Shift+click is used for panning, don't start marker drag
        if event.state & 0x0001:  # Shift modifier
            return
        segment = self.current_project.segments[self.current_segment_index]
        start_x = self._time_to_x(segment.start_s)
        end_x = self._time_to_x(segment.end_s)
        if abs(event.x - start_x) <= MARKER_HITBOX_PX:
            self.drag_target = "start"
        elif abs(event.x - end_x) <= MARKER_HITBOX_PX:
            self.drag_target = "end"
        else:
            self.drag_target = None

    def _on_waveform_drag(self, event: tk.Event[Any]) -> None:
        if (
            not self.current_project
            or self.current_segment_index is None
            or self.drag_target is None
        ):
            return
        if self.current_read_only:
            return

        segment = self.current_project.segments[self.current_segment_index]
        duration = max(self.current_project.duration, 0.0)
        new_time = max(0.0, self._x_to_time(event.x))
        if duration > 0:
            new_time = min(new_time, duration)

        if self.drag_target == "start":
            segment.start_s = min(new_time, segment.end_s - MIN_SEGMENT_LEN_S)
        elif self.drag_target == "end":
            segment.end_s = max(new_time, segment.start_s + MIN_SEGMENT_LEN_S)

        self.start_var.set(f"{segment.start_s:.3f}")
        self.end_var.set(f"{segment.end_s:.3f}")
        self._sync_segment_ui()
        self._redraw_waveform()

    def _on_waveform_release(self, _event: tk.Event[Any]) -> None:
        if self.drag_target is not None:
            if self.current_read_only:
                self.drag_target = None
                self._redraw_waveform()
                return
            self._mark_current_reviewed()
            self._sync_segment_ui()
            self._save_current_project()
            self._refresh_playback_data()
        self.drag_target = None

    # ---- Waveform panning ----

    def _on_waveform_scroll(self, event: tk.Event[Any]) -> None:
        """Scroll wheel pans the waveform left/right."""
        if not self.current_project:
            return
        duration = max(self.current_project.duration, 2.0)
        # event.delta is typically +-120 on Windows per notch
        step = self.view_span_s * 0.15 * (-1 if event.delta > 0 else 1)
        self.view_start_s = max(0.0, min(self.view_start_s + step, duration - self.view_span_s))
        self._redraw_waveform()

    def _on_pan_press(self, event: tk.Event[Any]) -> None:
        """Start panning with middle-mouse or shift+left-click."""
        self._pan_start_x = event.x
        self._pan_start_view = self.view_start_s

    def _on_pan_drag(self, event: tk.Event[Any]) -> None:
        """Drag to pan the waveform view."""
        if self._pan_start_x is None or not self.current_project:
            return
        duration = max(self.current_project.duration, 2.0)
        canvas_width = self.waveform_canvas.winfo_width()
        if canvas_width <= 0:
            return
        dx_pixels = event.x - self._pan_start_x
        dx_seconds = -(dx_pixels / canvas_width) * self.view_span_s
        new_start = self._pan_start_view + dx_seconds
        self.view_start_s = max(0.0, min(new_start, duration - self.view_span_s))
        self._redraw_waveform()

    # ---- Split and Combine ----

    def _split_at_cursor(self) -> None:
        """Split the current segment at the text cursor position."""
        if not self.current_project or self.current_segment_index is None:
            return
        if not self._ensure_project_editable("split this segment"):
            return
        self._commit_current_segment_text()
        segment = self.current_project.segments[self.current_segment_index]
        text = segment.text
        if not text.strip():
            return

        # Get cursor position in the text widget (character offset)
        cursor_pos = self.preview_text.index("insert")
        # cursor_pos is "line.col" — convert to character offset in the full string
        line, col = (int(x) for x in cursor_pos.split("."))
        lines = text.split("\n")
        char_offset = sum(len(lines[i]) + 1 for i in range(line - 1)) + col

        if char_offset <= 0 or char_offset >= len(text):
            messagebox.showinfo(
                "Cannot Split",
                "Place the cursor inside the text (not at the very start or end) to split.",
            )
            return

        left_text = text[:char_offset].strip()
        right_text = text[char_offset:].strip()
        if not left_text or not right_text:
            messagebox.showinfo(
                "Cannot Split",
                "Both sides of the split must contain text.",
            )
            return

        # Estimate split time proportionally by character position
        ratio = len(left_text) / len(text.strip())
        split_time = segment.start_s + (segment.end_s - segment.start_s) * ratio

        # Create two new segments replacing the original
        seg_left = EditableSegment(
            index=0,
            text=left_text,
            start_s=segment.start_s,
            end_s=round(split_time, 3),
            original_start_s=segment.original_start_s,
            original_end_s=round(split_time, 3),
            enabled=segment.enabled,
        )
        seg_right = EditableSegment(
            index=0,
            text=right_text,
            start_s=round(split_time, 3),
            end_s=segment.end_s,
            original_start_s=round(split_time, 3),
            original_end_s=segment.original_end_s,
            enabled=segment.enabled,
        )

        idx = self.current_segment_index
        self.current_project.segments[idx:idx + 1] = [seg_left, seg_right]
        self._reindex_segments()
        self.current_segment_index = None
        self._persist_project()
        self._suspend_segment_select = True
        try:
            self._populate_segment_tree()
            self._select_segment_by_index_no_sync(idx)
            self.segment_tree.selection_set(str(idx))
            self.segment_tree.see(str(idx))
            self._last_segment_selection = (str(idx),)
        finally:
            self.root.after_idle(self._unsuspend_segment_select)

    def _combine_segments(self) -> None:
        """Combine selected adjacent segments into one."""
        if not self.current_project:
            return
        if not self._ensure_project_editable("combine these segments"):
            return
        # Use stashed selection — tree selection may be lost when button got focus
        selection = self._last_segment_selection
        logger.debug("_combine_segments: selection=%s", selection)
        if len(selection) < 2:
            messagebox.showinfo(
                "Select Segments",
                "Select two or more adjacent phrases (Ctrl+click or Shift+click) to combine.",
            )
            return

        indices = sorted(int(s) for s in selection)

        # Verify they are consecutive
        for i in range(1, len(indices)):
            if indices[i] != indices[i - 1] + 1:
                messagebox.showwarning(
                    "Not Adjacent",
                    "Only adjacent (consecutive) phrases can be combined.",
                )
                return

        self._commit_current_segment_text()
        segments = self.current_project.segments
        to_merge = [segments[i] for i in indices]

        for i, seg in enumerate(to_merge):
            logger.debug("_combine_segments: seg[%d] text=%r", indices[i], seg.text)

        combined_text = " ".join(seg.text.strip() for seg in to_merge if seg.text.strip())
        combined = EditableSegment(
            index=0,
            text=combined_text,
            start_s=to_merge[0].start_s,
            end_s=to_merge[-1].end_s,
            original_start_s=to_merge[0].original_start_s,
            original_end_s=to_merge[-1].original_end_s,
            enabled=all(seg.enabled for seg in to_merge),
        )
        logger.debug("_combine_segments: combined text=%r", combined_text)

        first_idx = indices[0]
        self.current_project.segments[first_idx:indices[-1] + 1] = [combined]
        self._reindex_segments()
        self.current_segment_index = None
        self._persist_project()
        # Suspend selection events during tree rebuild
        self._suspend_segment_select = True
        try:
            self._populate_segment_tree()
            self._select_segment_by_index_no_sync(first_idx)
            self.segment_tree.selection_set(str(first_idx))
            self.segment_tree.see(str(first_idx))
            self._last_segment_selection = (str(first_idx),)
        finally:
            self.root.after_idle(self._unsuspend_segment_select)
        logger.debug("_combine_segments: done, now displaying segment %d text=%r",
                      first_idx, self.current_project.segments[first_idx].text)

    def _reindex_segments(self) -> None:
        """Re-number segment indices after split/combine."""
        for i, seg in enumerate(self.current_project.segments):
            seg.index = i

    def _fetch_available_languages(self) -> None:
        """Probe the first URL in the URL box and populate the language dropdown."""
        raw = self.url_input.get("1.0", "end").strip()
        url = raw.split()[0] if raw.split() else ""
        if not url:
            messagebox.showinfo("No URL", "Enter a YouTube URL first, then click the refresh button to see available caption languages.")
            return

        def worker():
            info = yt_info(url, cookies=None)
            langs = available_caption_languages(info)
            return langs

        def on_done(langs):
            if not langs:
                messagebox.showinfo("No Captions", f"No automatic captions found for this video.")
                return
            self.language_combo["values"] = langs
            # If the current language is in the list, keep it; otherwise select first
            if self.language_var.get() not in langs:
                self.language_var.set(langs[0])
            self.status_var.set(f"Found {len(langs)} caption language(s). Select one from the dropdown.")

        self._run_in_worker("Fetching available languages...", worker, on_done)

    def _download_urls(self) -> None:
        raw_value = self.url_input.get("1.0", "end").strip()
        urls = [item.strip() for item in raw_value.split() if item.strip()]
        if not urls:
            messagebox.showinfo(
                "No URLs",
                "Enter one or more YouTube URLs separated by spaces or new lines.",
            )
            return

        def worker() -> dict[str, Any]:
            created_ids: list[str] = []
            projects: list[VideoProject] = []

            def _progress_hook(info: dict[str, Any]) -> None:
                status = info.get("status", "")
                if status == "downloading":
                    pct = info.get("_percent_str", "").strip()
                    speed = info.get("_speed_str", "").strip()
                    eta = info.get("_eta_str", "").strip()
                    parts = [p for p in [pct, speed, f"ETA {eta}" if eta else ""] if p]
                    self.worker_queue.put(("status", f"Downloading: {' | '.join(parts) or '...'}"))
                elif status == "finished":
                    self.worker_queue.put(("status", "Download finished, processing audio..."))

            def _status_hook(msg: str) -> None:
                self.worker_queue.put(("status", msg))

            for index, url in enumerate(urls, start=1):
                self.worker_queue.put(("status", f"Fetching info {index}/{len(urls)}: {url}"))
                project = create_project_from_url(
                    self.workspace, url, self.language_var.get() or self.language,
                    progress_hook=_progress_hook, status_hook=_status_hook,
                )
                created_ids.append(project.video_id)
                projects.append(project)
            return {"video_ids": created_ids, "projects": projects}

        def on_done(result: dict[str, Any]) -> None:
            created_ids = result.get("video_ids") or []
            projects = result.get("projects") or []
            self.url_input.delete("1.0", "end")
            selected_id = created_ids[-1] if created_ids else None
            if projects:
                last_project = projects[-1]
                # Display the project first — this sets self.current_project.
                # refresh_video_list will select the tree item with the flag
                # suppressed, so the deferred <<TreeviewSelect>> event is
                # ignored and _open_video_by_id's guard prevents a re-display.
                self._display_project(last_project)
            self.refresh_video_list(select_video_id=selected_id, auto_open=False)
            if created_ids:
                self._set_status(f"Downloaded {len(created_ids)} video(s) into {self.workspace}")
            else:
                self._set_status("Nothing new was downloaded.")

        self._run_in_worker("Starting download...", worker, on_done)

    def _import_local_media(self) -> None:
        dialog = LocalMediaImportDialog(
            self.root,
            self.language_var.get() or self.language,
        )
        if not dialog.result:
            return
        options = dialog.result

        def worker() -> dict[str, Any]:
            def _status_hook(message: str) -> None:
                self.worker_queue.put(("status", message))

            project = create_project_from_local_media(
                self.workspace,
                Path(options["media_path"]),
                title=str(options.get("title") or ""),
                channel=str(options.get("channel") or ""),
                caption_language=str(options.get("caption_language") or ""),
                subtitle_mode=str(options.get("subtitle_mode") or "embedded"),
                subtitle_stream_index=options.get("subtitle_stream_index"),
                subtitle_file=Path(options["subtitle_file"]) if options.get("subtitle_file") else None,
                status_hook=_status_hook,
            )
            return {"project": project}

        def on_done(result: dict[str, Any]) -> None:
            project = result["project"]
            self._display_project(project)
            self.refresh_video_list(select_video_id=project.video_id, auto_open=False)
            self._set_status(f"Imported local media as {project.title}")

        self._run_in_worker("Importing local media...", worker, on_done)

    def _export_current_project(self) -> None:
        if not self.current_project:
            messagebox.showinfo("No Video Selected", "Select a video before exporting.")
            return
        self._save_current_project()
        project = self.current_project

        def worker() -> dict[str, Any]:
            self.worker_queue.put(("status", f"Exporting clips for {project.title}"))
            return export_projects(self.workspace, [project])

        def on_done(result: dict[str, Any]) -> None:
            manifest_path = result["manifest_path"]
            self._set_status(
                f"Exported {result['clip_count']} clip(s) from {project.title} to {manifest_path}"
            )
            messagebox.showinfo(
                "Export Complete",
                f"Exported {result['clip_count']} clip(s).\nManifest:\n{manifest_path}",
            )

        self._run_in_worker("Starting export...", worker, on_done)

    def _export_all_projects(self) -> None:
        summaries = discover_videos(self.workspace)
        if not summaries:
            messagebox.showinfo("No Videos", "Download at least one video first.")
            return
        self._save_current_project()

        def worker() -> dict[str, Any]:
            projects: list[VideoProject] = []
            for index, summary in enumerate(summaries, start=1):
                self.worker_queue.put(
                    (
                        "status",
                        f"Preparing {index}/{len(summaries)}: {summary['title']}",
                    )
                )
                projects.append(ensure_project_for_video(self.workspace, summary["video_id"]))
            self.worker_queue.put(("status", "Exporting reviewed clips..."))
            return export_projects(self.workspace, projects)

        def on_done(result: dict[str, Any]) -> None:
            manifest_path = result["manifest_path"]
            selected_id = self.current_project.video_id if self.current_project else None
            self.refresh_video_list(select_video_id=selected_id)
            self._set_status(
                f"Exported {result['clip_count']} clip(s) from {result['project_count']} video(s)."
            )
            messagebox.showinfo(
                "Export Complete",
                f"Exported {result['clip_count']} clip(s) from "
                f"{result['project_count']} video(s).\nManifest:\n{manifest_path}",
            )

        self._run_in_worker("Starting export...", worker, on_done)

    # ------------------------------------------------------------------
    # .asr package — pack (export)
    # ------------------------------------------------------------------

    def _pack_asr(self) -> None:
        summaries = discover_videos(self.workspace)
        if not summaries:
            messagebox.showinfo("No Videos", "Download at least one video first.")
            return

        dlg = TitleSelectDialog(
            self.root,
            "Pack .asr — Select Titles to Export",
            summaries,
            default_all=True,
        )
        selected_ids = dlg.result
        if not selected_ids:
            return

        dest = filedialog.asksaveasfilename(
            title="Save .asr package",
            defaultextension=".asr",
            filetypes=[("ASR package", "*.asr"), ("All files", "*.*")],
        )
        if not dest:
            return

        self._save_current_project()
        dest_path    = Path(dest)
        selected_set = set(selected_ids)

        def worker() -> dict[str, Any]:
            projects: list[VideoProject] = []
            for summary in summaries:
                if summary["video_id"] not in selected_set:
                    continue
                self.worker_queue.put(("status", f"Preparing {summary['title']}…"))
                projects.append(ensure_project_for_video(self.workspace, summary["video_id"]))

            def status_hook(msg: str) -> None:
                self.worker_queue.put(("status", msg))

            packed = pack_asr(self.workspace, projects, dest_path, status_hook=status_hook)
            return {"packed": packed, "dest": str(dest_path)}

        def on_done(result: dict[str, Any]) -> None:
            packed = result["packed"]
            self._set_status(f"Packed {packed} project(s) → {result['dest']}")
            messagebox.showinfo(
                "Pack Complete",
                f"Packed {packed} project(s) to:\n{result['dest']}",
            )

        self._run_in_worker("Packing .asr…", worker, on_done)

    # ------------------------------------------------------------------
    # .asr package — unpack (import)
    # ------------------------------------------------------------------

    def _unpack_asr(self) -> None:
        src = filedialog.askopenfilename(
            title="Open .asr package",
            filetypes=[("ASR package", "*.asr"), ("All files", "*.*")],
        )
        if not src:
            return

        asr_path = Path(src)
        try:
            entries = list_asr_contents(asr_path)
        except Exception as exc:
            messagebox.showerror("Invalid Package", f"Could not read package:\n{exc}")
            return

        if not entries:
            messagebox.showinfo("Empty Package", "This package contains no projects.")
            return

        summaries = [
            {"video_id": e["video_id"], "title": e.get("title") or e["video_id"]}
            for e in entries
        ]

        dlg = TitleSelectDialog(
            self.root,
            "Import .asr — Select Titles to Import",
            summaries,
            default_all=True,
        )
        selected_ids = dlg.result
        if not selected_ids:
            return

        def worker() -> dict[str, Any]:
            def status_hook(msg: str) -> None:
                self.worker_queue.put(("status", msg))
            imported = unpack_asr(asr_path, self.workspace, selected_ids, status_hook=status_hook)
            return {"imported": imported}

        def on_done(result: dict[str, Any]) -> None:
            imported = result["imported"]
            selected_id = self.current_project.video_id if self.current_project else None
            self.refresh_video_list(select_video_id=selected_id)
            self._set_status(f"Imported {len(imported)} project(s) into {self.workspace}")
            messagebox.showinfo(
                "Import Complete",
                f"Imported {len(imported)} project(s) into workspace.",
            )

        self._run_in_worker("Importing .asr…", worker, on_done)

    # ------------------------------------------------------------------
    # Cloud panel
    # ------------------------------------------------------------------

    def _open_cloud_panel(self) -> None:
        if not _HAS_CLOUD:
            messagebox.showerror(
                "Missing dependencies",
                "Cloud features require extra packages.\n\n"
                "Run:  pip install \".[cloud]\"\n\n"
                "This installs boto3 (for S3-compatible cloud services)\n"
                "and cryptography (for encrypted config files).",
            )
            return

        # Reuse existing panel window if still open
        existing = getattr(self, "_cloud_panel", None)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.lift()
                    existing.focus_set()
                    return
            except tk.TclError:
                pass

        self._cloud_panel = CloudPanel(
            self.root,
            workspace        = self.workspace,
            pack_fn          = pack_asr,
            unpack_fn        = unpack_asr,
            list_contents_fn = list_asr_contents,
            get_project_fn   = lambda vid: ensure_project_for_video(self.workspace, vid),
            discover_fn      = lambda: discover_videos(self.workspace),
            prepare_local_fn = lambda: self._save_current_project(),
            on_cloud_change  = lambda: self.refresh_video_list(auto_open=False),
        )

    # ------------------------------------------------------------------

    def _format_s(self, value: float) -> str:
        return f"{value:.2f}"

    def _on_close(self) -> None:
        self._playback_stop_internal()
        self._save_current_project()
        self.root.destroy()


def main() -> int:
    args = parse_args()
    workspace = Path(args.workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(workspace / "gui_debug.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )

    root = tk.Tk()
    ASRReviewApp(root, workspace, args.language)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
