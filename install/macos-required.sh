#!/usr/bin/env bash
# ============================================================
# yt-subtitle-extract  --  macOS prerequisite installer
# Installs Homebrew (if missing) then ffmpeg.
# tkinter is bundled with the python.org macOS installer.
# Run once before:  pip install .
# ============================================================

set -euo pipefail

echo "=== yt-subtitle-extract: macOS prerequisites ==="
echo

# ---- Homebrew ----
if ! command -v brew &>/dev/null; then
    echo "[INFO] Homebrew not found.  Installing Homebrew ..."
    echo "       This may prompt for your password (sudo)."
    echo
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

    # Add brew to PATH for Apple-silicon Macs (installs to /opt/homebrew)
    if [[ -f /opt/homebrew/bin/brew ]]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    fi

    echo "[OK] Homebrew installed."
    echo
else
    echo "[INFO] Homebrew already installed."
fi

# ---- ffmpeg ----
echo "[1/1] Installing ffmpeg ..."
brew install ffmpeg
echo "[OK] ffmpeg installed."

echo
echo "-------------------------------------------------------"
echo " NOTE: tkinter ships with the Python installer from"
echo " https://www.python.org/downloads/macos/"
echo " If you installed Python via Homebrew, install the"
echo " tk formula as well:"
echo "   brew install python-tk"
echo "-------------------------------------------------------"
echo
echo " Prerequisites done.  You can now run:"
echo "   pip install ."
echo "   pip install '.[audio]'   # adds audio playback support"
echo "-------------------------------------------------------"
