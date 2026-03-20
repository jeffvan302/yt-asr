# Windows Bootstrap Launcher

This folder contains a native Windows bootstrap launcher for `yt-asr`.

## What it does

When `yt-asr-launcher.exe` starts, it:

1. Locates the app root by searching upward for `pyproject.toml` and `yt_subtitle_extract/gui.py`.
2. Creates local folders under the app root:
   - `runtime/python`
   - `runtime/python-base`
   - `runtime/ffmpeg`
   - `runtime/cache`
   - `runtime/logs`
   - `workdata`
3. Builds a local Python environment in `runtime/python`.
4. If a registered Python install already exists on the machine and it supports both `venv` and `tkinter`, the launcher prefers that as the base interpreter.
5. The launcher prefers Python `3.13`, but can fall back to another local Python `>=3.10` if that is the only local interpreter with GUI support.
6. If no suitable base interpreter exists, it downloads and installs a bundled Python `3.13.12` base runtime into `runtime/python-base`, then creates the local environment from that.
7. Downloads and installs local `ffmpeg.exe` and `ffprobe.exe` into `runtime/ffmpeg/bin` if they are missing.
8. Uses the local Python runtime to:
   - bootstrap `pip`, `setuptools`, and `wheel`
   - install or refresh the local app dependencies from `.[all]`
   - update `yt-dlp` on every launch
9. Launches the GUI with:
   - working directory set to `workdata`
   - `--workspace <app-root>\workdata`
10. Shows a native Windows startup window with:
   - the current bootstrap step
   - a progress bar
   - a live text log of the setup/startup flow

The launcher also writes:

- `runtime/logs/bootstrap.log`
- `runtime/logs/python-installer.log` when the Python installer runs
- `runtime/state.json`

## Build

### Visual Studio / CMake

```powershell
cd win-launcher
cmake -S . -B build
cmake --build build --config Release
```

### One-step batch build

On another Windows machine, you can also run:

```bat
win-launcher\build-launcher.bat
```

The batch file will:

- find a Visual Studio C++ toolchain
- load the compiler environment
- find CMake
- configure and build the launcher
- print the official Microsoft download page if the required compiler tools are missing

If Visual Studio is not installed yet, install either Visual Studio Community or Visual Studio Build Tools from Microsoft's official pages:

- Build Tools for Visual Studio: [visualstudio.microsoft.com/downloads](https://visualstudio.microsoft.com/downloads/)
- Visual C++ tools overview: [visualstudio.microsoft.com/vs/cplusplus](https://visualstudio.microsoft.com/vs/cplusplus/)

On the downloads page, use the `Build Tools for Visual Studio` download under `Tools for Visual Studio`.

Install it with:

- `Desktop development with C++`
- `MSVC v143 or newer C++ x64/x86 build tools`
- `Windows 11 SDK` or `Windows 10 SDK`
- `C++ CMake tools for Windows`

The resulting executable will be named:

```text
win-launcher/build/Release/yt-asr-launcher.exe
```

The build now also copies the fresh `yt-asr-launcher.exe` into the application root automatically, next to `pyproject.toml`.

## Runtime Notes

- The launcher keeps a single-instance mutex for the full app lifetime, so a second launcher instance will not bootstrap over a running app.
- `ffmpeg` is provided locally and prepended to `PATH` for the child process.
- `PYTHONNOUSERSITE=1` is set so the local runtime does not depend on globally installed user packages.
- The launcher currently downloads:
  - Python from `python.org`
  - FFmpeg Essentials from `gyan.dev`

## Current defaults

- Python version: `3.13.12`
- App install command: `python -m pip install --upgrade .[all]`
- Per-launch refresh command: `python -m pip install --upgrade yt-dlp`
