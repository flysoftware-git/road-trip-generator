@echo off
if "%1"=="" (
    echo You must specify a program name
    echo Example: run-trip.bat SW2026
    pause
    exit /b
)

REM Activate venv
call C:\dev\road-trip-runtime\road-trip-generator\venv\Scripts\activate.bat

REM Output root
set OUTPUT_ROOT=C:\Users\bryan\OneDrive\RoadTripOutput

REM Program name from argument
set PROGRAM=%1
set OUTPUT_DIR=%OUTPUT_ROOT%\%PROGRAM%

REM Ensure folder exists
if not exist "%OUTPUT_DIR%" mkdir "%OUTPUT_DIR%"

REM Version stamp
echo Run date: %DATE% %TIME% > "%OUTPUT_DIR%\version.txt"

REM Manifest naming convention: <program>_manifest.yaml
python -m generator.main --manifest "%PROGRAM%_manifest.yaml" --output "%OUTPUT_DIR%"

REM Log completion
echo Completed at %DATE% %TIME% >> "%OUTPUT_DIR%\run-log.txt"

pause
