# ASR GUI App — Detailed Requirements & Feature Specification

This file covers the complete requirements and feature set for the `yt-subtitle-extract` package.

**Source files:**

- `yt_subtitle_extract/gui.py` — GUI editor (`yt-asr` command)
- `yt_subtitle_extract/dataset.py` — CLI pipeline (`yt-asr-dataset` command)
- `yt_subtitle_extract/cloud.py` — S3-compatible cloud integration
- `pyproject.toml` — Package metadata and optional dependency groups
- `install/` — Platform-specific scripts for non-Python dependencies

---

## Python Version

- Python **3.10** or newer (uses `match`, `|` union types, `Path.unlink(missing_ok=True)`)
- `tkinter` must be available (standard on Windows/macOS; requires `python3-tk` on Linux)

---

## Python Package Dependencies

### Required (installed automatically by `pip install .`)

| Package | Purpose |
|---|---|
| `yt-dlp` | YouTube download, metadata extraction, and caption retrieval |

### Optional — audio playback (`pip install ".[audio]"`)

| Package | Min version | Purpose |
|---|---|---|
| `sounddevice` | 0.4.6 | Play/Pause/Stop/Loop segment audio directly in the app |

### Optional — cloud collaboration (`pip install ".[cloud]"`)

| Package | Min version | Purpose |
|---|---|---|
| `boto3` | 1.34 | S3-compatible bucket operations (upload, download, list, delete) |
| `cryptography` | 42.0 | Fernet AES-128 encryption for shareable `.b2cfg` config files |

### Install everything

```bash
pip install ".[all]"        # runtime: audio + cloud
pip install -e ".[all]"     # editable / development install
```

---

## System Dependencies

`ffmpeg` must be installed and available on `PATH`.  Used for audio conversion and clip extraction.

`tkinter` must be available.  Ships with the standard Python installer on Windows and macOS.  On Linux it is a separate package (`python3-tk` / `python3-tkinter` / `tk` depending on distro).

### Quick-install scripts (`install/` folder)

| Platform | Script | Method | What it installs |
|---|---|---|---|
| Windows 10 / 11 | `install\win-required.bat` | winget (built in) | ffmpeg — notes tkinter requirement |
| Windows (any) | `install\win-required-scoop.bat` | Scoop (auto-installed if missing) | ffmpeg via Scoop |
| Linux | `install/linux-required.sh` | apt / dnf / pacman (auto-detected) | ffmpeg + python3-tk |
| macOS | `install/macos-required.sh` | Homebrew (auto-installed if missing) | ffmpeg — notes python-tk caveat |

---

## Running the App

```bash
# GUI editor — uses current directory as workspace
yt-asr

# GUI with explicit workspace and default caption language
yt-asr --workspace ./my_project --language en

# CLI dataset builder
yt-asr-dataset "https://www.youtube.com/watch?v=VIDEO_ID" --output ./dataset --language af

# Multiple URLs in one CLI run
yt-asr-dataset URL1 URL2 URL3 --output ./dataset
```

If the generated `yt-asr.exe` / `yt-asr-dataset.exe` launchers are installed into a per-user Scripts directory that is not on `PATH`, the package can still be started directly with Python:

```bash
py -m yt_asr --workspace ./my_project --language en
py -m yt_asr_dataset URL1 URL2 URL3 --output ./dataset
```

---

## Workspace Layout

The workspace is a directory that holds all projects.  Sub-directories are created automatically on first download.

```
workspace/
  downloads/          raw audio files (.m4a / .webm)
  working_audio/      converted mono WAV files for the waveform view
  captions/           downloaded caption files (.json3)
  projects/           one JSON project file per video
  exports/            clipped WAV segments and TSV manifests
```

All paths stored inside project JSON files are **relative** to the workspace root.  This keeps projects portable and safe to move or share.

---

## Project File Format

Each video is described by a JSON project file in `projects/`.

```json
{
  "version": 1,
  "video_id": "...",
  "title": "...",
  "channel": "...",
  "webpage_url": "...",
  "duration": 123.4,
  "caption_language": "en",
  "caption_file": "captions/VIDEO_ID.json3",
  "audio_file": "downloads/VIDEO_ID.m4a",
  "working_audio_file": "working_audio/VIDEO_ID.wav",
  "created_at": 1700000000.0,
  "updated_at": 1700000000.0,
  "segments": [
    {
      "start": 0.0,
      "end":   2.5,
      "text":  "Hello world.",
      "enabled": true,
      "reviewed": false
    }
  ]
}
```

All file paths are stored relative to `workspace/` using forward slashes.  On load, paths are resolved back to absolute using the workspace root.

---

## `.asr` Package Format

`.asr` files are standard ZIP archives with a `.asr` extension.  They are used to export a set of titles for sharing or cloud storage.

### Archive layout

```
manifest.json               list of included video_ids and ASR_FORMAT_VERSION
{video_id}/project.json     project data (paths rewritten to flat names)
{video_id}/caption.json3    original caption file
{video_id}/working_audio.wav  mono WAV used by the waveform editor
```

Intra-package paths use flat filenames (no absolute or relative path separators) so the archive is portable across operating systems.

### `manifest.json`

```json
{
  "asr_format_version": 1,
  "titles": [
    { "video_id": "...", "title": "...", "channel": "..." }
  ]
}
```

### Title selection dialogs

**Export (pack):** A scrollable checklist dialog (`TitleSelectDialog`) lets the user choose which local titles to include.  All titles are pre-selected by default.

**Import (unpack):** The same dialog shows the titles found inside the `.asr` file.  No titles are pre-selected, so the user deliberately chooses what to import.

---

## Local Media Import

The GUI can also import a local media file directly into the workspace.

### Supported subtitle sources

- Embedded subtitle tracks discovered from the media file with `ffprobe`
- A separate subtitle file selected from disk

### Import window

The **Import Media...** action opens a dedicated modal window with:

- Media file picker
- Title field (defaults to the file name)
- Optional channel/source field
- Caption language field
- Embedded subtitle track list for files that contain multiple subtitle languages
- Separate subtitle file picker for `.vtt`, `.srt`, `.json3`, `.srv3`, `.json`, or convertible text subtitle formats

### Import workflow

The app:

1. extracts or copies the chosen subtitle source into `captions/`
2. converts the selected media file to `working_audio/{video_id}.wav`
3. builds editable phrase segments from the chosen subtitle file
4. writes normal workspace `metadata/` and `projects/` files

After import, local media titles behave the same as YouTube titles in the workspace.

---

## GUI Layout

The GUI is divided into three vertical panes (proportional weights 2 : 6 : 3):

- **Left pane** — Video list (all downloaded titles in the workspace)
- **Centre pane** — Waveform view, time entry fields, caption text editor
- **Right pane** — Caption phrases list with timing and review state

### Toolbar

Two rows of controls separated by a visual divider:

**Row 0:** URL input field, Language dropdown, ↻ probe button, Download button, ▶ Import .asr, ◀ Export .asr, ☁ Cloud, separator, Save Progress

**Row 1:** ▶ Play, ⏸ Pause, ⏹ Stop, 🔁 Loop, separator, ✂ Split, ⊕ Combine, separator, Enable / Disable phrase

---

## Caption Phrases Panel

Located in the right pane.  Displays one row per segment.

### Columns

| Column | Content |
|---|---|
| Start | Segment start time (MM:SS.mmm) |
| End | Segment end time |
| En | ✓ (enabled) or ✗ (disabled) — phrase will be included in export |
| Reviewed | ☑ (reviewed) or ☐ (not reviewed) |

### Colour coding

| Tag | Condition | Colour |
|---|---|---|
| `normal` | Enabled, not reviewed | Default |
| `reviewed` | Enabled, reviewed | Green background |
| `disabled` | Disabled, not reviewed | Grey/muted text |
| `disabled_reviewed` | Disabled, reviewed | Grey + green background |

### Clickable Reviewed column

Clicking directly on the **Reviewed** column cell (column `#4`) toggles the `reviewed` flag for that phrase without changing the selection.

### Auto-marking as reviewed

A phrase is automatically marked reviewed when any of the following occurs:

- The **Play** button is clicked for that phrase
- The caption text is edited and committed (Enter / focus-out) and the text has changed
- The **Apply** button is clicked to save manually edited time entries
- A waveform marker is dragged and released for that phrase

Auto-marking is a one-way operation — it sets `reviewed = True` but never clears it.  The user must manually untick the checkbox (or click the Reviewed column) to un-review a phrase.

---

## Data Persistence

### `reviewed` field

The `reviewed` field is stored in each segment object inside the project JSON.  On load, older project files that lack the field default to `reviewed = False` (backward compatible).

### `.asr` packages

The `reviewed` field is included in the segment data written into `.asr` packages.  It is preserved across pack/unpack round-trips.

### Path storage

All three path fields in a project JSON (`caption_file`, `audio_file`, `working_audio_file`) are stored as paths **relative to the workspace root**.  A helper `_to_relative(p, root)` resolves both paths and calls `Path.relative_to()`.  If the path cannot be made relative (e.g. it is on a different drive on Windows), the absolute string is used as a fallback.

---

## Cloud Collaboration - S3 Compatible

Multiple users can work on the same set of titles concurrently using a check-out / check-in model backed by any S3-compatible object store. Presets are built in for Backblaze B2, Amazon S3, Cloudflare R2, MinIO, and a generic S3-compatible endpoint.

### Cloud module (`cloud.py`)

#### `B2Config` dataclass

The class name is retained for backward compatibility, but it now represents generic S3-compatible cloud settings.

| Field | Type | Description |
|---|---|---|
| `provider` | str | Provider preset key such as `backblaze_b2`, `aws_s3`, `cloudflare_r2`, `minio`, or `generic_s3` |
| `key_id` | str | S3 access key / application key ID |
| `application_key` | str | S3 secret key / application key secret |
| `bucket_name` | str | Target bucket |
| `endpoint_url` | str | Explicit S3 endpoint URL when the provider requires one |
| `region_name` | str | Region used by the provider; Backblaze can derive its S3 endpoint from this |
| `addressing_style` | str | S3 bucket addressing style: `virtual`, `path`, or `auto` |
| `folder_prefix` | str | Optional sub-folder within the bucket, for example `"team/"` |
| `display_name` | str | Human name shown in lock files and audit records |
| `user_id` | str | Stable user identity used for lock ownership and audit history |
| `role` | str | Trusted-team role (`user` or `admin`) |

Important helper methods:

- `is_valid()` requires access key, secret, bucket, and any provider-specific endpoint details needed to connect.
- `effective_endpoint_url()` uses `endpoint_url` when provided and derives the Backblaze S3 endpoint from `region_name` when needed.
- `normalized_provider()` and `normalized_addressing_style()` coerce older or missing config values onto supported presets.

Config is persisted to `.b2_config.json` in the workspace root. The legacy filename is retained for compatibility and is listed in `.gitignore`.

#### `B2CloudStore` class

The class name is also retained for compatibility. Internally it now builds a `boto3` S3 client and uses standard S3 APIs for all network operations.

**Key methods:**

| Method | Description |
|---|---|
| `list_titles()` | Returns metadata dicts for every `.asr` file in `titles/`, including `title` and `channel` from S3 object metadata; if missing, falls back to reading the uploaded `.asr` archive |
| `list_locks()` | Downloads and parses every `.lock` file in `locks/`; returns `{video_id: lock_info}` |
| `upload_asr(video_id, bytes, title, channel)` | Uploads `.asr` bytes and stores `title` / `channel` as S3 object metadata |
| `download_asr(video_id)` | Downloads and returns `.asr` bytes |
| `delete_asr(video_id)` | Deletes the `.asr` object and releases its lock |
| `create_lock(video_id)` | Uploads a JSON lock file with user identity, role, checkout token, hostname, and timestamp |
| `release_lock(video_id)` | Deletes the lock file |
| `get_lock(video_id)` | Downloads and returns a single lock dict, or `None` |
| `write_audit(event_type, video_id, details)` | Writes an audit JSON record under `audit/` for uploads, syncs, check-ins, and admin actions |

**Deletion detail:** if the bucket has object versioning enabled, `_delete_file` enumerates all object versions and delete markers for the exact key and removes them all. If version APIs are unavailable or the bucket is not versioned, it falls back to a normal `delete_object` call.

#### Bucket layout

```text
{folder_prefix}titles/{video_id}.asr     # packaged title
{folder_prefix}locks/{video_id}.lock     # advisory lock while checked out
{folder_prefix}audit/<timestamp>_...json # audit trail for sync and admin actions
```

### Encrypted config sharing (`.b2cfg` files)

Allows a team lead to distribute S3-compatible bucket credentials without sharing raw keys in chat.

**Encryption:** Fernet (AES-128-CBC + HMAC-SHA256) with a key derived from the user-chosen password via PBKDF2-HMAC-SHA256 (480000 iterations, 16-byte random salt).

**Export flow:** fill in settings -> Export Config... -> choose password -> if the current machine is an admin, choose whether the export should be a `user` or `admin` config -> save `.b2cfg`.

**Import flow:** Import Config... -> select `.b2cfg` -> enter the file password plus a local **name** and **user ID** -> config loads into the form and is saved locally. Wrong passwords raise `ValueError("Incorrect password or corrupted file.")`.

The exported file contains shared connection settings (`provider`, keys, bucket, endpoint, region, addressing style, folder prefix, and role) but does not copy the exporting machine's personal `display_name` or `user_id`.
If a config is exported as `user`, the imported local config persists that restriction and the Role field stays locked to `user` in the UI.
Imported user-only configs also hide the shared connection fields (`provider`, keys, bucket, region, endpoint, addressing, folder prefix, and role) so the receiving user only sees their local identity fields.

**`.b2cfg` file format:**

```json
{
  "v": 1,
  "salt": "<base64 16-byte salt>",
  "data": "<Fernet ciphertext of JSON-serialised config data>"
}
```

### Lock system

Locks are advisory JSON objects stored as `.lock` files in the bucket.

**Lock file format:**

```json
{
  "user": "Alice",
  "user_id": "alice01",
  "role": "user",
  "checkout_token": "7f5f6d...",
  "hostname": "alice-workstation",
  "locked_at": 1700000000.0,
  "locked_at_str": "2024-01-01 12:00:00"
}
```

The Cloud Titles panel colour-codes rows: blue = locked by you, amber = locked by someone else. Normal users cannot override another user's lock. Admin-only actions provide controlled recovery for abandoned work:

- `Admin Force Check In` releases another user's lock and makes the current cloud copy available again.
- `Admin Take Over` replaces the lock so the admin becomes the current editor.

### `CloudPanel` window (`cloud.py`)

Non-modal `tk.Toplevel` window. Can remain open while editing. If valid settings are already saved, opening the panel auto-refreshes the cloud list.

**Account Settings section:** form with Provider, Access Key ID, Secret Access Key (masked), Bucket, Region, Endpoint URL, Addressing, Folder prefix, Your name, User ID, and Role; Show key toggle; Test Connection; Save Settings; Export Config...; Import Config...

**Cloud Titles section:** `ttk.Treeview` with columns Title/Video ID, Size, Uploaded, Status. The list uses S3 object metadata for title and channel, with `.asr` archive fallback for older uploads.

**Action buttons:** Refresh, Check Out, Check In, Sync Checked Out, Upload from Workspace, Delete from Cloud, Admin Force Check In, Admin Take Over

**Background worker pattern:** all cloud operations run in a `threading.Thread`; progress updates and results are posted to a `queue.Queue`; the main thread polls via `after(150, _poll_queue)`.

**Graceful degradation:** if `boto3` or `cryptography` is not installed, the Cloud button shows an install dialog instead of crashing.

### Workflows

**Check Out:** download `.asr` -> unpack into the workspace -> create or refresh the lock -> refresh the cloud list -> call `on_import_done` so the local video list updates.

**Check In:** pack the local project into `.asr` -> upload with title / channel metadata -> release the lock -> write an audit record -> refresh the list.

**Upload from Workspace:** open `TitleSelectDialog` -> show local titles that are not already on the cloud -> pack each selected title -> upload it -> optionally check it in immediately, or keep it checked out by the uploading user.

**Sync Checked Out:** upload local changes for titles that are still checked out by the current user without releasing the lock. The app also performs a best-effort auto-sync on save for checked-out local titles.

**Delete from Cloud:** confirm dialog -> delete the `.asr` object and lock file -> remove all object versions when possible -> refresh the list.

---

## CLI Dataset Builder (`dataset.py`)

### `process_url(url, dirs, language)`

Downloads audio and captions for a single URL.  Calls `build_manifest_rows` with `root=dirs["root"]` so that the output manifest uses workspace-relative paths.

### `build_manifest_rows(project, root)`

Accepts an optional `root: Path` parameter.  When provided, all file paths in the manifest rows are written relative to `root`.

### `write_metadata(path, project)`

Stores all three path fields (`caption_file`, `audio_file`, `working_audio_file`) as paths relative to the workspace root using an inline `_rel(p)` helper.

---

## Notes

- No `torch`, `transformers`, `datasets`, or training packages are required.
- If `sounddevice` is not installed, Play/Pause/Stop/Loop buttons are disabled with a tooltip hint.
- If `ffmpeg` is not on `PATH`, downloads complete but audio conversion and segment export will fail.
- If `boto3` or `cryptography` is not installed, the Cloud button shows an install prompt.
- `.b2_config.json` contains raw API credentials — never commit it to version control.  It is listed in `.gitignore`.
- The cloud backend uses S3-compatible APIs. Backblaze B2 works through its S3 endpoint, not through the old B2-specific SDK.
- Locks are advisory workflow state, but the app blocks normal users from overriding another user's active checkout.
