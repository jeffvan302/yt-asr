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
MARKER_HITBOX_PX = 10


@dataclass
class EditableSegment:
    index: int
    text: str
    start_s: float
    end_s: float
    original_start_s: float
    original_end_s: float
    enabled: bool = True


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

    for path in sorted(dirs["metadata"].glob("*.json")):
        summary = video_summary_from_metadata(path)
        by_id[summary["video_id"]] = summary

    for path in sorted(dirs["projects"].glob("*.json")):
        summary = video_summary_from_project(path)
        by_id[summary["video_id"]] = summary

    return sorted(by_id.values(), key=lambda item: item["title"].lower())


def segment_from_payload(payload: dict[str, Any]) -> EditableSegment:
    return EditableSegment(
        index=int(payload["index"]),
        text=payload["text"],
        start_s=float(payload["start_s"]),
        end_s=float(payload["end_s"]),
        original_start_s=float(payload.get("original_start_s", payload["start_s"])),
        original_end_s=float(payload.get("original_end_s", payload["end_s"])),
        enabled=bool(payload.get("enabled", True)),
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
    caption_language, caption_entry = select_caption_track(info, language)
    caption_ext = (caption_entry.get("ext") or "json3").lower()
    caption_file = dirs["captions"] / (
        f"{sanitize_filename(video_id)}.{caption_language}.{caption_ext}"
    )
    fetch_caption_file(caption_entry["url"], caption_file)

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

        self.video_summaries: list[dict[str, Any]] = []
        self.current_project: VideoProject | None = None
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
        self.enabled_var = tk.BooleanVar(value=True)

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

        toolbar = ttk.Frame(self.root, padding=(10, 10, 10, 6))
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.columnconfigure(1, weight=1)
        toolbar.columnconfigure(9, weight=1)

        ttk.Label(toolbar, text="Workspace").grid(row=0, column=0, sticky="w")
        ttk.Entry(toolbar, textvariable=self.workspace_var).grid(
            row=0, column=1, sticky="ew", padx=(6, 6)
        )
        ttk.Button(toolbar, text="Browse", command=self._choose_workspace).grid(
            row=0, column=2, padx=(0, 6)
        )
        ttk.Button(toolbar, text="Reload", command=self.refresh_video_list).grid(
            row=0, column=3, padx=(0, 10)
        )
        ttk.Button(
            toolbar,
            text="Save Progress",
            command=lambda: self._save_current_project(show_feedback=True),
        ).grid(row=0, column=4, padx=(0, 10), sticky="n")

        ttk.Label(toolbar, text="Language").grid(row=0, column=5, sticky="nw")
        self.language_combo = ttk.Combobox(
            toolbar, textvariable=self.language_var, width=8,
            values=["af", "en", "zu", "xh", "st", "tn", "ts", "ss", "nr", "ve",
                    "nso", "nl", "de", "fr", "es", "pt", "it", "ru", "ja", "ko",
                    "zh", "ar", "hi"],
        )
        self.language_combo.grid(row=0, column=6, padx=(6, 2), sticky="nw")
        ttk.Button(
            toolbar, text="\u21bb", width=3, command=self._fetch_available_languages,
        ).grid(row=0, column=7, padx=(0, 6), sticky="nw")
        ttk.Label(toolbar, text="YouTube URL(s)").grid(row=0, column=8, sticky="nw")
        self.url_input = tk.Text(toolbar, height=3, wrap="word")
        self.url_input.grid(row=0, column=9, sticky="ew", padx=(6, 6))
        self.url_input.bind("<Control-Return>", lambda _event: self._download_urls())
        ttk.Button(toolbar, text="Download", command=self._download_urls).grid(
            row=0, column=10, padx=(0, 6), sticky="n"
        )
        ttk.Button(
            toolbar, text="Export Current", command=self._export_current_project
        ).grid(row=0, column=11, padx=(0, 6), sticky="n")
        ttk.Button(toolbar, text="Export All", command=self._export_all_projects).grid(
            row=0, column=12, sticky="n"
        )
        ttk.Button(toolbar, text="Pack .asr", command=self._pack_asr).grid(
            row=0, column=13, padx=(10, 6), sticky="n"
        )
        ttk.Button(toolbar, text="Import .asr", command=self._unpack_asr).grid(
            row=0, column=14, sticky="n"
        )

        status = ttk.Label(
            self.root,
            textvariable=self.status_var,
            anchor="w",
            relief="groove",
            padding=(10, 5),
        )
        status.grid(row=2, column=0, sticky="ew")

        main = ttk.Panedwindow(self.root, orient="horizontal")
        main.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 8))

        videos_frame = ttk.Frame(main, padding=8)
        editor_frame = ttk.Frame(main, padding=8)
        segments_frame = ttk.Frame(main, padding=8)
        main.add(videos_frame, weight=2)
        main.add(editor_frame, weight=5)
        main.add(segments_frame, weight=4)

        videos_frame.columnconfigure(0, weight=1)
        videos_frame.rowconfigure(1, weight=1)
        ttk.Label(videos_frame, text="Downloaded Videos").grid(
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
        self.video_tree.column("state", width=80, stretch=False, anchor="center")
        self.video_tree.column("segments", width=70, stretch=False, anchor="e")
        self.video_tree.grid(row=1, column=0, sticky="nsew")
        self.video_tree.bind("<<TreeviewSelect>>", self._on_video_selected)
        video_scroll = ttk.Scrollbar(
            videos_frame, orient="vertical", command=self.video_tree.yview
        )
        video_scroll.grid(row=1, column=1, sticky="ns")
        self.video_tree.configure(yscrollcommand=video_scroll.set)

        editor_frame.columnconfigure(0, weight=1)
        editor_frame.rowconfigure(3, weight=1)
        ttk.Label(
            editor_frame,
            textvariable=self.video_label_var,
            anchor="w",
            font=("Segoe UI", 12, "bold"),
        ).grid(row=0, column=0, sticky="ew", pady=(0, 8))

        ttk.Label(editor_frame, text="Caption Text (editable)").grid(
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

        self._play_btn = ttk.Button(playback_frame, text="Play", width=6,
                                     command=self._playback_play)
        self._play_btn.pack(side="left", padx=(0, 4))
        self._pause_btn = ttk.Button(playback_frame, text="Pause", width=6,
                                      command=self._playback_pause)
        self._pause_btn.pack(side="left", padx=(0, 4))
        self._stop_btn = ttk.Button(playback_frame, text="Stop", width=6,
                                     command=self._playback_stop)
        self._stop_btn.pack(side="left", padx=(0, 10))
        self._loop_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(playback_frame, text="Loop", variable=self._loop_var,
                         command=self._on_loop_toggled).pack(side="left", padx=(0, 10))
        self._playback_status_var = tk.StringVar(value="")
        ttk.Label(playback_frame, textvariable=self._playback_status_var,
                  foreground="#4b5563").pack(side="left")

        if not _HAS_SOUNDDEVICE:
            self._play_btn.state(["disabled"])
            self._pause_btn.state(["disabled"])
            self._stop_btn.state(["disabled"])
            self._playback_status_var.set("pip install sounddevice to enable playback")

        # --- Existing controls ---
        controls = ttk.Frame(editor_frame, padding=(0, 10, 0, 0))
        controls.grid(row=5, column=0, sticky="ew")
        for index in range(12):
            controls.columnconfigure(index, weight=0)
        controls.columnconfigure(11, weight=1)

        ttk.Label(controls, textvariable=self.segment_label_var).grid(
            row=0, column=0, columnspan=12, sticky="w", pady=(0, 8)
        )
        ttk.Button(controls, text="Previous", command=self._select_previous_segment).grid(
            row=1, column=0, padx=(0, 6)
        )
        ttk.Button(controls, text="Next", command=self._select_next_segment).grid(
            row=1, column=1, padx=(0, 12)
        )
        ttk.Button(controls, text="Zoom In", command=lambda: self._zoom(0.7)).grid(
            row=1, column=2, padx=(0, 6)
        )
        ttk.Button(controls, text="Zoom Out", command=lambda: self._zoom(1.4)).grid(
            row=1, column=3, padx=(0, 12)
        )
        ttk.Label(controls, text="Start (s)").grid(row=1, column=4, sticky="w")
        start_entry = ttk.Entry(controls, textvariable=self.start_var, width=10)
        start_entry.grid(row=1, column=5, padx=(6, 10))
        start_entry.bind("<Return>", lambda _event: self._apply_time_entries())
        ttk.Label(controls, text="End (s)").grid(row=1, column=6, sticky="w")
        end_entry = ttk.Entry(controls, textvariable=self.end_var, width=10)
        end_entry.grid(row=1, column=7, padx=(6, 10))
        end_entry.bind("<Return>", lambda _event: self._apply_time_entries())
        ttk.Button(controls, text="Apply", command=self._apply_time_entries).grid(
            row=1, column=8, padx=(0, 6)
        )
        ttk.Button(controls, text="Reset Segment", command=self._reset_current_segment).grid(
            row=1, column=9, padx=(0, 10)
        )
        ttk.Checkbutton(
            controls,
            text="Include in export",
            variable=self.enabled_var,
            command=self._toggle_current_segment_enabled,
        ).grid(row=1, column=10, sticky="w")

        ttk.Button(controls, text="Split at Cursor", command=self._split_at_cursor).grid(
            row=2, column=0, columnspan=2, padx=(0, 6), pady=(8, 0), sticky="w"
        )
        ttk.Button(controls, text="Combine Selected", command=self._combine_segments).grid(
            row=2, column=2, columnspan=2, padx=(0, 12), pady=(8, 0), sticky="w"
        )
        help_text = (
            "Drag markers or scroll to pan. "
            "Split: place cursor in text, click Split. "
            "Combine: select adjacent phrases on the right, click Combine."
        )
        ttk.Label(controls, text=help_text, foreground="#4b5563").grid(
            row=2, column=4, columnspan=8, sticky="w", pady=(8, 0)
        )

        segments_frame.columnconfigure(0, weight=1)
        segments_frame.rowconfigure(1, weight=1)
        ttk.Label(segments_frame, text="Caption Phrases").grid(
            row=0, column=0, sticky="w", pady=(0, 6)
        )
        self.segment_tree = ttk.Treeview(
            segments_frame,
            columns=("start", "end", "enabled"),
            show="tree headings",
            selectmode="extended",
        )
        self.segment_tree.heading("#0", text="Sentence")
        self.segment_tree.heading("start", text="Start")
        self.segment_tree.heading("end", text="End")
        self.segment_tree.heading("enabled", text="Use")
        self.segment_tree.column("#0", width=360, stretch=True)
        self.segment_tree.column("start", width=72, stretch=False, anchor="e")
        self.segment_tree.column("end", width=72, stretch=False, anchor="e")
        self.segment_tree.column("enabled", width=50, stretch=False, anchor="center")
        self.segment_tree.tag_configure("disabled", foreground="#6b7280")
        self.segment_tree.grid(row=1, column=0, sticky="nsew")
        self.segment_tree.bind("<<TreeviewSelect>>", self._on_segment_selected)
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

    def _choose_workspace(self) -> None:
        self._save_current_project()
        selected = filedialog.askdirectory(initialdir=str(self.workspace))
        if not selected:
            return
        self.workspace = Path(selected).resolve()
        self.workspace_var.set(str(self.workspace))
        self.dirs = ensure_app_dirs(self.workspace)
        self.current_project = None
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

    def refresh_video_list(
        self, select_video_id: str | None = None, auto_open: bool = False
    ) -> None:
        previous = select_video_id
        if previous is None:
            selected = self.video_tree.selection()
            previous = selected[0] if selected else None

        self.video_summaries = discover_videos(self.workspace)
        existing_ids = {item["video_id"] for item in self.video_summaries}

        for item in self.video_tree.get_children():
            self.video_tree.delete(item)

        for summary in self.video_summaries:
            state = "Reviewed" if summary["source"] == "project" else "Downloaded"
            segment_count = summary["segment_count"]
            self.video_tree.insert(
                "",
                "end",
                iid=summary["video_id"],
                text=summary["title"],
                values=(
                    summary["channel"],
                    state,
                    segment_count if segment_count is not None else "-",
                ),
            )

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
            self.current_segment_index = None
            self.video_label_var.set("No video loaded.")
            self.segment_label_var.set("No phrase selected.")
            self._set_text_preview("")
            self._populate_segment_tree()
            self._redraw_waveform()

    def _unsuspend_video_select(self) -> None:
        self._suspend_video_select = False

    def _on_video_selected(self, _event: Any = None) -> None:
        if self._suspend_video_select:
            return
        selection = self.video_tree.selection()
        if selection:
            self._open_video_by_id(selection[0])

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
        self.view_span_s = DEFAULT_VIEW_SPAN_S
        self.view_start_s = 0.0
        self.text_dirty = False
        self.video_label_var.set(
            f"{project.title}  |  {project.channel or 'Unknown channel'}  |  "
            f"{len(project.segments)} phrases"
        )
        logger.debug("_display_project: populating segment tree")
        self._populate_segment_tree()
        if project.segments:
            logger.debug("_display_project: selecting first segment")
            self._select_segment_by_index(0)
        else:
            self.current_segment_index = None
            self.segment_label_var.set("No usable phrases were found for this video.")
            self._set_text_preview("")
            self._redraw_waveform()
        self._set_status(f"Loaded {project.title}")

    def _populate_segment_tree(self) -> None:
        for item in self.segment_tree.get_children():
            self.segment_tree.delete(item)

        if not self.current_project:
            return

        for index, segment in enumerate(self.current_project.segments):
            tags = ("disabled",) if not segment.enabled else ()
            self.segment_tree.insert(
                "",
                "end",
                iid=str(index),
                text=segment.text,
                values=(
                    self._format_s(segment.start_s),
                    self._format_s(segment.end_s),
                    "Yes" if segment.enabled else "No",
                ),
                tags=tags,
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
        self.preview_text.delete("1.0", "end")
        self.preview_text.insert("1.0", text)
        self.preview_text.edit_modified(False)
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
        self._save_current_project()
        self._refresh_playback_data()
        self._sync_segment_ui()
        self._focus_current_segment()
        self._redraw_waveform()

    def _reset_current_segment(self) -> None:
        if not self.current_project or self.current_segment_index is None:
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
        self._commit_current_segment_text()
        segment = self.current_project.segments[self.current_segment_index]
        segment.enabled = self.enabled_var.get()
        self._save_current_project()
        self._sync_segment_ui()

    def _save_current_project(self, show_feedback: bool = False) -> bool:
        if not self.current_project:
            return False
        self._commit_current_segment_text()
        self._persist_project(show_feedback=show_feedback)
        return True

    def _persist_project(self, show_feedback: bool = False) -> None:
        if not self.current_project:
            return
        save_project(
            project_path(self.workspace, self.current_project.video_id),
            self.current_project,
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
            tags = ("disabled",) if not segment.enabled else ()
            self.segment_tree.item(
                item_id,
                text=segment.text,
                values=(
                    self._format_s(segment.start_s),
                    self._format_s(segment.end_s),
                    "Yes" if segment.enabled else "No",
                ),
                tags=tags,
            )
        self.segment_label_var.set(
            f"Phrase {self.current_segment_index + 1}/{len(self.current_project.segments)}  |  "
            f"{segment.end_s - segment.start_s:.2f}s"
        )

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
        # If paused, resume
        if self._playback_paused and self._playback_stream is not None:
            self._playback_paused = False
            self._playback_status_var.set("Playing...")
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
        self._playback_status_var.set("Playing..." + (" [Loop]" if self._playback_loop else ""))

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
                samplerate=rate,
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
        self._playback_paused = not self._playback_paused
        self._playback_status_var.set(
            "Paused" if self._playback_paused
            else "Playing..." + (" [Loop]" if self._playback_loop else "")
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
        self._playback_status_var.set("Finished")
        self._redraw_waveform()

    def _on_loop_toggled(self) -> None:
        self._playback_loop = self._loop_var.get()
        if self._playback_stream is not None and not self._playback_paused:
            self._playback_status_var.set(
                "Playing..." + (" [Loop]" if self._playback_loop else "")
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
