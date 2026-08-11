@echo off
REM OmniDownloader — One-click Windows build
REM Requires: Python 3.11+, PyInstaller, Inno Setup (iscc)
setlocal enabledelayedexpansion
cd /d "%~dp0\.."

echo ============================================================
echo  OmniDownloader Windows Build
echo ============================================================
echo.

REM ── Step 0: Activate project venv ───────────────────────────
set VENV_DIR=.venv
if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo Creating virtual environment...
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment.
        echo Make sure Python 3.11+ is installed and in your PATH.
        pause
        exit /b 1
    )
)
echo Activating venv...
call "%VENV_DIR%\Scripts\activate.bat"

REM ── Step 1: Install build dependencies ──────────────────────
echo [1/4] Installing build dependencies...
pip install --upgrade pip
pip install pyinstaller packaging
if errorlevel 1 (
    echo ERROR: Failed to install build dependencies.
    pause
    exit /b 1
)
pip install -e .
if errorlevel 1 (
    echo ERROR: Failed to install project dependencies.
    pause
    exit /b 1
)
echo.

REM ── Step 2: PyInstaller bundle ──────────────────────────────
echo [2/4] Building PyInstaller bundle...
pyinstaller build\windows.spec --noconfirm --clean --log-level WARNING
if errorlevel 1 (
    echo ERROR: PyInstaller build failed.
    pause
    exit /b 1
)
echo.

REM ── Step 3: Read version from pyproject.toml ────────────────
echo [3/4] Reading version...
set "VERSION=0.1.0"
for /f "tokens=2 delims==" %%A in ('findstr /B "version" pyproject.toml 2^>nul') do (
    set "RAW=%%~A"
    set "VERSION=!RAW: =!"
    set "VERSION=!VERSION:"=!"
)
echo Current version: %VERSION%
echo.

REM ── Step 4: Inno Setup installer ────────────────────────────
echo [4/4] Building installer with Inno Setup...
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
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  BUILD COMPLETE!
echo ============================================================
echo   Bundle:    dist\OmniDownloader\
echo   Installer: dist\OmniDownloader-Setup-%VERSION%.exe
echo ============================================================

:done
echo.
pause
endlocal

