@echo off
REM OmniDownloader — one-click venv setup ^& launch (Windows)
cd /d "%~dp0"

set VENV_DIR=.venv

if not exist "%VENV_DIR%" (
    echo Creating virtual environment...
    python -m venv "%VENV_DIR%"
    "%VENV_DIR%\Scripts\pip.exe" install --upgrade pip
    "%VENV_DIR%\Scripts\pip.exe" install -e .
    echo Dependencies installed.
)

"%VENV_DIR%\Scripts\python.exe" -m omnidownloader %*
