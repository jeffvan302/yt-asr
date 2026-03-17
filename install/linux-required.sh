#!/usr/bin/env bash
# ============================================================
# yt-subtitle-extract  --  Linux prerequisite installer
# Installs ffmpeg and python3-tk via apt (Debian / Ubuntu).
# Run once before:  pip install .
# ============================================================

set -euo pipefail

echo "=== yt-subtitle-extract: Linux prerequisites ==="
echo

# ---- Detect package manager ----
if command -v apt-get &>/dev/null; then
    PKG_MGR="apt"
elif command -v dnf &>/dev/null; then
    PKG_MGR="dnf"
elif command -v pacman &>/dev/null; then
    PKG_MGR="pacman"
else
    echo "[ERROR] Could not detect a supported package manager (apt / dnf / pacman)."
    echo "        Install ffmpeg and python3-tkinter manually, then re-run pip install ."
    exit 1
fi

echo "[INFO] Detected package manager: $PKG_MGR"
echo

install_apt() {
    echo "[1/2] Updating package lists ..."
    sudo apt-get update -qq

    echo "[2/2] Installing ffmpeg and python3-tk ..."
    sudo apt-get install -y ffmpeg python3-tk

    echo
    echo "[OK] ffmpeg and python3-tk installed."
}

install_dnf() {
    echo "[1/1] Installing ffmpeg and python3-tkinter ..."
    # ffmpeg lives in rpmfusion-free on Fedora/RHEL; guide the user if it fails
    if ! sudo dnf install -y ffmpeg python3-tkinter 2>/dev/null; then
        echo
        echo "[WARN] ffmpeg may not be in the default repos."
        echo "       Enable RPM Fusion first:"
        echo "  sudo dnf install https://download1.rpmfusion.org/free/fedora/rpmfusion-free-release-\$(rpm -E %fedora).noarch.rpm"
        echo "       Then re-run this script."
        exit 1
    fi
    echo "[OK] ffmpeg and python3-tkinter installed."
}

install_pacman() {
    echo "[1/1] Installing ffmpeg and tk ..."
    sudo pacman -Sy --noconfirm ffmpeg tk
    echo "[OK] ffmpeg and tk installed."
}

case "$PKG_MGR" in
    apt)    install_apt    ;;
    dnf)    install_dnf    ;;
    pacman) install_pacman ;;
esac

echo
echo "-------------------------------------------------------"
echo " Prerequisites done.  You can now run:"
echo "   pip install ."
echo "   pip install '.[audio]'   # adds audio playback support"
echo "-------------------------------------------------------"
