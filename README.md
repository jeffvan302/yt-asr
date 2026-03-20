# Youtube Generated Subtitle Extractor for Transcription training

Download YouTube audio + auto-captions, review timing in a GUI editor, and export ASR training datasets.

## If you are not familiar with Python Environments then follow this guide:
1) Install Guide [documentation/How_to_Install.pdf](documentation/How_to_Install.pdf).
2) Application Quick Usage Guide [documentation/How_to_Use_the_App.pdf](documentation/How_to_Use_the_App.pdf)

## Install

```bash
# Basic install (GUI works but without audio playback or cloud sync)
python -m pip install .

# With audio playback support
python -m pip install ".[audio]"

# With S3-compatible cloud collaboration
python -m pip install ".[cloud]"

# Everything
python -m pip install ".[all]"

# Development / editable install
python -m pip install -e ".[all]"
```

### Windows note if `yt-asr` is not on PATH

If `pip` says the launchers were installed into a folder like:

```text
C:\Users\<you>\AppData\Roaming\Python\Python312\Scripts
```

then the install succeeded, but that Scripts folder is not on `PATH`.

You can:

1. add that Scripts folder to `PATH`
2. install inside an activated virtual environment
3. launch the package directly with Python

```bash
# GUI
py -m yt_asr

# Dataset CLI
py -m yt_asr_dataset "https://www.youtube.com/watch?v=VIDEO_ID" --output ./dataset --language af
```

### Recommended Windows setup with Miniconda

If you are installing from a shared zip on Windows, this is the smoothest setup:

1. Extract the zip to a normal writable folder.
2. Install Miniconda first if you do not already have it.
3. Create and activate a dedicated environment.
4. Run the prerequisite installer from the `install/` folder.
5. Return to the project root and install the app there.

Example:

```powershell
conda create -n yt-asr python=3.13
conda activate yt-asr
cd install
.\win-required.bat
cd ..
python -m pip install .[all]
```

After that, go to the folder where you want to keep your subtitle work and start the app from the same environment.

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

# Windows fallback if yt-asr is not on PATH
py -m yt_asr --workspace ./my_project --language en
```

### CLI dataset builder — `yt-asr-dataset`

```bash
# Build dataset in current directory
yt-asr-dataset "https://www.youtube.com/watch?v=VIDEO_ID"

# Specify output folder and language
yt-asr-dataset "https://www.youtube.com/watch?v=VIDEO_ID" --output ./dataset --language af

# Multiple URLs
yt-asr-dataset URL1 URL2 URL3 --output ./dataset

# Windows fallback if yt-asr-dataset is not on PATH
py -m yt_asr_dataset URL1 URL2 URL3 --output ./dataset
```

### Typical GUI workflow

1. Open the app in the workspace where you want to store your title data.
2. Add a new YouTube video by pasting the link and clicking **Download**, or use **Import Media...** for local files.
3. Select a title from the list on the left to load it into the editor.
4. Edit caption text in the text box, and adjust timing by dragging the red start marker and green end marker on the waveform.
5. Use the playback controls to listen to the current phrase. Enable **Loop** when you want repeated playback.
6. Pan around the waveform with the mouse wheel, Shift+drag, or middle-mouse drag.
7. To split a phrase, place the text cursor where you want the split and click **Split at Cursor**.
8. To merge phrases, select adjacent items in the phrase list and click **Combine Selected**.
9. Save regularly with **Save Progress**.
10. If you are working with a team, use the **Cloud** window to upload new titles or check titles in and out.

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

## Cloud Collaboration (S3 Compatible)

Multiple people can work on the same collection of titles without overwriting each other's work. A **check-out / check-in** model is used: one person downloads a title and places a lock on it; others can see that it is in use and who has it; once the work is done the title is checked back in and the lock is released.

### Installing cloud dependencies

```bash
pip install ".[cloud]"
```

This adds two packages:

- **boto3** — the standard Python client for S3-compatible object storage
- **cryptography** — used to encrypt/decrypt the shareable config file

### Supported providers

The cloud panel now works with **S3-compatible** services and includes presets for:

- **Backblaze B2**
- **Amazon S3**
- **Cloudflare R2**
- **MinIO**
- **Generic S3 Compatible**

Backblaze remains the default preset.

### Opening the Cloud panel

Click the **☁ Cloud** button in the toolbar. The panel opens as a separate, non-modal window so you can keep editing while uploads or downloads run in the background. If valid cloud settings are already saved, the list refreshes automatically when the panel opens.

### Account setup

Fill in the fields in the **Account Settings** section:

| Field | Description |
|---|---|
| Provider | Preset for Backblaze, AWS, R2, MinIO, or a generic S3-compatible service |
| Access Key ID | Your S3 access key / application key ID |
| Secret Access Key | The secret key (hidden by default; tick **Show key** to reveal) |
| Bucket | Bucket name to use |
| Region | Region when the provider uses one |
| Endpoint URL | Custom S3 endpoint URL when required |
| Addressing | S3 bucket addressing style (`virtual`, `path`, or `auto`) |
| Folder prefix | *(optional)* Sub-folder within the bucket, e.g. `team-project/` |
| Your name | Identifies you in lock files so teammates know who has a title checked out |
| User ID | Stable identity used for checkout ownership and audit entries |
| Role | `user` or `admin` for the trusted-team admin workflow |

Click **Save Settings** — credentials are stored locally in `.b2_config.json` inside your workspace folder (the legacy filename is retained for compatibility and is excluded from version control by `.gitignore`).

Click **Test Connection** to verify credentials and see how many titles are currently on the bucket.

#### Bucket layout

The tool stores three object groups in the bucket:

```text
{folder_prefix}titles/{video_id}.asr    # packaged title (audio + captions)
{folder_prefix}locks/{video_id}.lock    # JSON lock while a title is checked out
{folder_prefix}audit/...json            # audit trail for upload / sync / admin actions
```

### Sharing credentials with teammates (encrypted config)

Rather than sending raw API keys over chat, use the encrypted config export:

1. Fill in and save your settings, then click **Export Config…**
2. Choose a password and save the `.b2cfg` file.
3. If the exporting machine is an admin, choose whether the file should be exported as a **user** config or an **admin** config. The default is **user**.
4. Send the `.b2cfg` file to your teammate.
5. Your teammate opens the Cloud panel, clicks **Import Config…**, selects the file, and enters the file password plus their local **name** and **user ID**.

The config is encrypted with AES-128 (Fernet) derived via PBKDF2-HMAC-SHA256 (480 000 iterations). Raw keys are never stored in the exported file. Personal name and user ID are not copied into the exported config.
If the file was exported as a **user** config, the importing machine stays user-only and cannot switch that config to **admin** from the UI.
For imported user-only configs, the shared connection fields are also hidden in Cloud Settings; only personal identity fields remain visible.

> **Note:** If a teammate changes the bucket or keys, they export a new `.b2cfg` and everyone re-imports.

### Cloud Titles panel

After a successful connection, the list refreshes automatically when the panel opens, and you can use **↻ Refresh** any time to pull the latest bucket state manually.

| Colour | Meaning |
|---|---|
| Normal (black) | Available — nobody has it checked out |
| Blue highlight | Checked out **by you** |
| Amber highlight | Checked out by someone else |

The **Title / Video ID** column shows the video title (and channel in brackets) when the title was uploaded with metadata. Older uploads that predate this feature show the raw `video_id` instead and will update automatically the next time they are checked in.

### Workflow

#### Checking out a title

1. Select one or more titles in the list (Ctrl+click for multiple).
2. Click **Check Out**.
3. The `.asr` package is downloaded and unpacked into your workspace, and a lock is written to the bucket.
4. The title appears in your local video list for editing.

Regular users cannot override another person's lock. Admins use **Admin Take Over** or **Admin Force Check In** when they need to recover abandoned cloud copies.

#### Checking in a title

1. Select the title(s) you have finished editing.
2. Click **Check In**.
3. Your local project is packed into a `.asr` file, uploaded to the bucket, and the lock is released.

#### Uploading a new title from your workspace

Use this to push a title that was never on the cloud before:

1. Click **Upload from Workspace**.
2. A dialog lists all local titles. Tick the ones to upload.
3. Choose whether to check them in immediately or leave them checked out by you.
4. Click OK — each selected title is packed and uploaded.

#### Syncing checked-out titles

Use **Sync Checked Out** to upload the latest local changes for titles that are still checked out by you without checking them in. The app also auto-syncs on save for checked-out titles when cloud settings are present.

#### Deleting a title from the cloud

1. Select the title(s) to remove.
2. Click **Delete from Cloud** and confirm the prompt.

If the bucket has object versioning enabled, the tool deletes every version it can find so the title is truly removed. Local workspace copies are not affected.

### Notes

- The `.b2_config.json` file in your workspace contains raw access keys — keep it private and never commit it to version control.
- The cloud backend uses S3-compatible APIs. Backblaze B2 works through its S3 endpoint, not through the old B2-specific SDK.
- Locks are advisory workflow state, but the app now blocks normal users from overriding another user's checked-out title.
- If `boto3` or `cryptography` is not installed, the **☁ Cloud** button shows an error message with the install command rather than crashing the application.

## Notes

- No `torch`, `transformers`, `datasets`, or training packages required
- If `sounddevice` is not installed, Play/Pause/Stop/Loop buttons are disabled with a hint
- If `ffmpeg` is not on `PATH`, downloads work but conversion/export will fail
