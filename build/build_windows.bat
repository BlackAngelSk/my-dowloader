@echo off
REM OmniDownloader — One-click Windows build
REM Requires: Python 3.11+, PyInstaller, Inno Setup (iscc)
setlocal enabledelayedexpansion
cd /d "%~dp0\.."

echo ============================================================
echo  OmniDownloader Windows Build
echo ============================================================
echo.

REM ── Step 1: Install build dependencies ──────────────────────
echo [1/4] Installing build dependencies...
pip install pyinstaller packaging >nul 2>&1
pip install -e . >nul 2>&1

REM ── Step 2: PyInstaller bundle ──────────────────────────────
echo [2/4] Building PyInstaller bundle...
pyinstaller build\windows.spec --noconfirm --clean --log-level WARNING
if errorlevel 1 (
    echo ERROR: PyInstaller build failed.
    exit /b 1
)

REM ── Step 3: Read version from pyproject.toml ────────────────
for /f "tokens=2 delims==" %%A in ('findstr /B "version" pyproject.toml') do (
    set VERSION=%%~A
)
set VERSION=%VERSION: =%
set VERSION=%VERSION:"=%
echo Current version: %VERSION%

REM ── Step 4: Inno Setup installer ────────────────────────────
echo [3/4] Building installer with Inno Setup...
where iscc >nul 2>&1
if errorlevel 1 (
    echo WARNING: Inno Setup (iscc) not found in PATH.
    echo Skipping installer creation. Install Inno Setup from:
    echo   https://jrsoftware.org/isinfo.php
    echo.
    echo PyInstaller bundle is ready at: dist\OmniDownloader\
    goto :done
)

iscc /DAPP_VERSION=%VERSION% build\installer.iss
if errorlevel 1 (
    echo ERROR: Inno Setup build failed.
    exit /b 1
)

echo.
echo [4/4] Build complete!
echo   Bundle:   dist\OmniDownloader\
echo   Installer: dist\OmniDownloader-Setup-%VERSION%.exe

:done
echo.
echo Done.
endlocal
