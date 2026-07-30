@echo off
REM Set up (if needed) and run the FERPA Evidence Manager locally on Windows.
REM
REM What this does, in order:
REM   1. Creates a Python virtual environment in .venv\ if one doesn't exist.
REM   2. Installs dependencies from pyproject.toml via pip, from PyPI only.
REM   3. Starts the app bound to 127.0.0.1 only and opens it in your browser.
REM
REM Safe to double-click and re-run any time.
setlocal
cd /d "%~dp0\.."

if not exist .venv (
    echo Creating virtual environment in .venv\ ...
    python -m venv .venv
)

echo Installing dependencies...
.venv\Scripts\pip install --quiet --upgrade pip
.venv\Scripts\pip install --quiet -e .

if "%FERPA_HOST%"=="" set FERPA_HOST=127.0.0.1
if "%FERPA_PORT%"=="" set FERPA_PORT=8420

echo Starting FERPA Evidence Manager at http://%FERPA_HOST%:%FERPA_PORT%
start "" "http://%FERPA_HOST%:%FERPA_PORT%"

.venv\Scripts\uvicorn app.main:create_app --factory --host %FERPA_HOST% --port %FERPA_PORT%
