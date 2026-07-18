@echo off
setlocal enabledelayedexpansion

REM ────────────────────────────────────────────────────────────────
REM Correct repo root (match actual casing exactly)
REM ────────────────────────────────────────────────────────────────
set REPO_ROOT=C:\Dev\road-trip-generator

REM Path to the directory where this BAT file lives (scripts/)
set SCRIPT_ROOT=%~dp0

REM Program name from argument
if "%1"=="" (
    echo You must specify a program name
    echo Example: run-trip.bat SW2026 dev
    pause
    exit /b
)
set PROGRAM=%1

REM Optional environment (dev/test/prod)
set ENVIRONMENT=%2

REM Output root
set OUTPUT_ROOT=C:\Users\bryan\OneDrive\RoadTripOutput
set OUTPUT_DIR=%OUTPUT_ROOT%\%PROGRAM%

if not exist "%OUTPUT_DIR%" mkdir "%OUTPUT_DIR%"

echo Run date: %DATE% %TIME% > "%OUTPUT_DIR%\version.txt"

REM Validate repo root
if not exist "%REPO_ROOT%" (
    echo [ERROR] Repo root not found: %REPO_ROOT%
    pause
    exit /b 1
)

REM Validate venv
if not exist "%REPO_ROOT%\venv\Scripts\activate.bat" (
    echo [ERROR] venv not found at: %REPO_ROOT%\venv\Scripts\activate.bat
    pause
    exit /b 1
)

REM Validate manifest in scripts directory
set MANIFEST_PATH=%SCRIPT_ROOT%%PROGRAM%_manifest.yaml
if not exist "%MANIFEST_PATH%" (
    echo [ERROR] Manifest not found: %MANIFEST_PATH%
    pause
    exit /b 1
)

REM Validate .env in scripts directory
set ENV_PATH=%SCRIPT_ROOT%.env
if not exist "%ENV_PATH%" (
    echo [WARN] .env not found in: %ENV_PATH%
)

REM Activate venv
call "%REPO_ROOT%\venv\Scripts\activate.bat"

REM Move into repo root so Python can import generator
cd /d "%REPO_ROOT%"

REM Build CLI flags
set CLI_ENV_FLAG=
if not "%ENVIRONMENT%"=="" (
    set CLI_ENV_FLAG=--environment %ENVIRONMENT%
)

set CLI_ENVFILE_FLAG=
if exist "%ENV_PATH%" (
    set CLI_ENVFILE_FLAG=--env-file "%ENV_PATH%"
)

REM Run generator
python -m generator.main ^
    --manifest "%MANIFEST_PATH%" ^
    --output "%OUTPUT_DIR%" ^
    %CLI_ENV_FLAG% ^
    %CLI_ENVFILE_FLAG%

echo Completed at %DATE% %TIME% >> "%OUTPUT_DIR%\run-log.txt"

pause
endlocal
