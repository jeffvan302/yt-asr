@echo off
:: ============================================================
:: yt-subtitle-extract  --  Windows prerequisite installer
:: Installs ffmpeg using winget (built into Windows 10/11).
:: Run this script once before "pip install ."
:: ============================================================

echo === yt-subtitle-extract: Windows prerequisites ===
echo.

:: ---- Check for winget ----
where winget >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] winget not found.
    echo         winget ships with Windows 10 1709+ and Windows 11.
    echo         If missing, install "App Installer" from the Microsoft Store,
    echo         or use the scoop installer instead: install\win-required-scoop.bat
    pause
    exit /b 1
)

:: ---- ffmpeg ----
echo [1/1] Installing ffmpeg ...
winget install --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
if %errorlevel% neq 0 (
    echo.
    echo [WARN] winget reported an error. ffmpeg may already be installed,
    echo        or you may need to restart your terminal to pick up PATH changes.
) else (
    echo [OK]  ffmpeg installed.
)

echo.
echo -------------------------------------------------------
echo  NOTE: tkinter ships with the standard Python installer
echo  from python.org.  If you installed Python via the
echo  Microsoft Store, tkinter may be missing -- reinstall
echo  from https://www.python.org/downloads/windows/
echo -------------------------------------------------------
echo.
echo Prerequisites done.  You can now run:
echo   pip install .
echo   pip install ".[audio]"   (adds audio playback support)
echo.
pause
