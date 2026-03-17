@echo off
:: ============================================================
:: yt-subtitle-extract  --  Windows prerequisite installer
:: Uses Scoop (https://scoop.sh) instead of winget.
:: Run this script once before "pip install ."
:: ============================================================

echo === yt-subtitle-extract: Windows prerequisites (Scoop) ===
echo.

:: ---- Check for scoop ----
where scoop >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] Scoop not found.  Installing Scoop now ...
    echo        This requires PowerShell and an internet connection.
    echo.
    powershell -NoProfile -ExecutionPolicy RemoteSigned -Command ^
        "Set-ExecutionPolicy RemoteSigned -Scope CurrentUser -Force; ^
         Invoke-RestMethod -Uri https://get.scoop.sh | Invoke-Expression"
    if %errorlevel% neq 0 (
        echo [ERROR] Scoop installation failed.
        echo         Visit https://scoop.sh for manual instructions.
        pause
        exit /b 1
    )
    echo [OK] Scoop installed.
    echo.
)

:: ---- ffmpeg ----
echo [1/1] Installing ffmpeg via Scoop ...
scoop install ffmpeg
if %errorlevel% neq 0 (
    echo [WARN] scoop reported an error.  ffmpeg may already be installed.
) else (
    echo [OK]  ffmpeg installed.
)

echo.
echo -------------------------------------------------------
echo  NOTE: tkinter ships with the standard Python installer
echo  from python.org.  If you installed Python via Scoop or
echo  the Microsoft Store, tkinter may be missing -- use
echo  https://www.python.org/downloads/windows/ instead.
echo -------------------------------------------------------
echo.
echo Prerequisites done.  You can now run:
echo   pip install .
echo   pip install ".[audio]"   (adds audio playback support)
echo.
pause
