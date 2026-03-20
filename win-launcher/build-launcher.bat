@echo off
setlocal EnableExtensions

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
for %%I in ("%SCRIPT_DIR%\..") do set "APP_ROOT=%%~fI"
set "BUILD_DIR=%SCRIPT_DIR%\build"
set "CONFIG=Release"
set "ARCH=x64"
set "VSWHERE_EXE="
set "VS_INSTALL="
set "CMAKE_EXE="

if /I "%~1"=="clean" (
    echo Removing existing build directory...
    if exist "%BUILD_DIR%" rmdir /S /Q "%BUILD_DIR%"
)

echo === yt-asr launcher build ===
echo Launcher folder: %SCRIPT_DIR%
echo App root: %APP_ROOT%
echo.

echo [1/4] Locating Visual Studio C++ build tools...
call :find_vswhere
if not defined VSWHERE_EXE (
    call :missing_compiler
    exit /b 1
)

for /f "usebackq delims=" %%I in (`"%VSWHERE_EXE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do (
    if not defined VS_INSTALL set "VS_INSTALL=%%~I"
)

if not defined VS_INSTALL (
    call :missing_compiler
    exit /b 1
)

if exist "%VS_INSTALL%\Common7\Tools\VsDevCmd.bat" (
    call "%VS_INSTALL%\Common7\Tools\VsDevCmd.bat" -arch=%ARCH% -host_arch=%ARCH% >nul
    if errorlevel 1 (
        call :vs_env_failed
        exit /b 1
    )
) else if exist "%VS_INSTALL%\VC\Auxiliary\Build\vcvars64.bat" (
    call "%VS_INSTALL%\VC\Auxiliary\Build\vcvars64.bat" >nul
    if errorlevel 1 (
        call :vs_env_failed
        exit /b 1
    )
) else (
    call :vs_env_failed
    exit /b 1
)

echo Found Visual Studio tools in:
echo   %VS_INSTALL%
echo.

echo [2/4] Locating CMake...
call :find_cmake
if not defined CMAKE_EXE (
    call :missing_cmake
    exit /b 1
)
echo Using CMake:
echo   %CMAKE_EXE%
echo.

echo [3/4] Configuring the launcher build...
set "CMAKE_ARCH_ARGS="
if not exist "%BUILD_DIR%\CMakeCache.txt" set "CMAKE_ARCH_ARGS=-A %ARCH%"
"%CMAKE_EXE%" -S "%SCRIPT_DIR%" -B "%BUILD_DIR%" %CMAKE_ARCH_ARGS%
if errorlevel 1 (
    call :cmake_failed
    exit /b 1
)
echo.

echo [4/4] Building yt-asr-launcher.exe...
"%CMAKE_EXE%" --build "%BUILD_DIR%" --config %CONFIG%
if errorlevel 1 (
    call :build_failed
    exit /b 1
)
echo.

if exist "%APP_ROOT%\yt-asr-launcher.exe" (
    echo Build completed successfully.
    echo Root launcher:
    echo   %APP_ROOT%\yt-asr-launcher.exe
    echo Build output:
    echo   %BUILD_DIR%\%CONFIG%\yt-asr-launcher.exe
    exit /b 0
)

if exist "%BUILD_DIR%\%CONFIG%\yt-asr-launcher.exe" (
    echo Build completed successfully.
    echo Build output:
    echo   %BUILD_DIR%\%CONFIG%\yt-asr-launcher.exe
    exit /b 0
)

echo Build finished, but the expected launcher executable was not found.
exit /b 1

:find_vswhere
if exist "%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe" (
    set "VSWHERE_EXE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
    exit /b 0
)
if exist "%ProgramFiles%\Microsoft Visual Studio\Installer\vswhere.exe" (
    set "VSWHERE_EXE=%ProgramFiles%\Microsoft Visual Studio\Installer\vswhere.exe"
    exit /b 0
)
exit /b 0

:find_cmake
for /f "delims=" %%I in ('where cmake.exe 2^>nul') do (
    if not defined CMAKE_EXE set "CMAKE_EXE=%%~fI"
)
if defined CMAKE_EXE exit /b 0

if defined VS_INSTALL if exist "%VS_INSTALL%\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe" (
    set "CMAKE_EXE=%VS_INSTALL%\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
)
exit /b 0

:missing_compiler
echo.
echo No Visual Studio C++ compiler environment was found.
echo Install one of these from the official Microsoft downloads page:
echo   https://visualstudio.microsoft.com/downloads/
echo.
echo Either of these is fine:
echo   - Visual Studio Community 2022 or newer
echo   - Visual Studio Build Tools 2022 or newer
echo.
echo Required workload:
echo   - Desktop development with C++
echo.
echo Recommended components:
echo   - MSVC v143 or newer C++ x64/x86 build tools
echo   - Windows 11 SDK or Windows 10 SDK
echo   - C++ CMake tools for Windows
exit /b 1

:missing_cmake
echo.
echo CMake was not found after loading the Visual Studio build environment.
echo Modify the Visual Studio installation and add:
echo   - C++ CMake tools for Windows
echo.
echo Official Microsoft download page:
echo   https://visualstudio.microsoft.com/downloads/
exit /b 1

:vs_env_failed
echo.
echo Visual Studio was found, but the compiler environment could not be initialized.
echo Visual Studio location:
echo   %VS_INSTALL%
echo.
echo Try repairing that installation from:
echo   https://visualstudio.microsoft.com/downloads/
exit /b 1

:cmake_failed
echo.
echo CMake configuration failed.
echo Review the output above for the first actual error.
exit /b 1

:build_failed
echo.
echo The launcher build failed.
echo Review the compiler output above for the first actual error.
exit /b 1
