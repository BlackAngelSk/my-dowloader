@echo off
setlocal EnableExtensions
REM OmniDownloader - one-click setup and launch (Windows)
REM Creates the virtual environment on first run and (re)installs the packages
REM listed in requirements.txt whenever that file changes, so a fresh clone or a
REM pulled update never fails with "ModuleNotFoundError".
cd /d "%~dp0"

set "VENV_DIR=.venv"
set "VPY=%VENV_DIR%\Scripts\python.exe"
set "STAMP=%VENV_DIR%\.requirements.synced"
set "PY="

REM --- locate a Python interpreter ----------------------------------------
where py >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if not defined PY (
    where python >nul 2>&1
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo [ERROR] Python 3.11 or newer was not found on PATH.
    echo         Install it from https://www.python.org/downloads/ and tick
    echo         "Add python.exe to PATH", then run start.bat again.
    pause
    exit /b 1
)

REM --- create the virtual environment -------------------------------------
if not exist "%VPY%" (
    echo Creating virtual environment...
    %PY% -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERROR] Could not create the virtual environment using "%PY%".
        pause
        exit /b 1
    )
)

REM --- decide whether dependencies have to be (re)installed ----------------
set "NEED_INSTALL=0"
if not exist "%STAMP%" set "NEED_INSTALL=1"
if not exist "%VENV_DIR%\Scripts\pip.exe" set "NEED_INSTALL=1"
if "%NEED_INSTALL%"=="0" (
    fc "requirements.txt" "%STAMP%" >nul 2>&1 || set "NEED_INSTALL=1"
)

if "%NEED_INSTALL%"=="1" (
    echo Installing dependencies from requirements.txt. This can take a few minutes...
    "%VPY%" -m pip install --upgrade pip --quiet
    "%VPY%" -m pip install -r "requirements.txt"
    if errorlevel 1 (
        echo.
        echo [ERROR] Installing requirements.txt failed. Check your internet
        echo         connection ^(or proxy^) and run start.bat again.
        pause
        exit /b 1
    )
    copy /y "requirements.txt" "%STAMP%" >nul
    echo Dependencies installed.
)

REM --- launch --------------------------------------------------------------
"%VPY%" -m omnidownloader %*
if errorlevel 1 pause
endlocal
