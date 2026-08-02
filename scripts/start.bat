@echo off
REM Set up (if needed) and run the FERPA Evidence Manager locally on Windows.
REM
REM What this does, in order:
REM   1. Creates a Python virtual environment in .venv\ if one doesn't exist.
REM   2. Installs dependencies from pyproject.toml via pip, from PyPI only.
REM   3. Starts the app bound to 127.0.0.1 only, waits until it's actually
REM      accepting connections, and only then opens it in your browser
REM      (Security Phase Step 6) -- opening immediately, before the server
REM      is listening, used to show a connection-refused error instead.
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
echo (Press Ctrl+C to stop.)

REM A hidden background helper polls the port and opens the browser only
REM once the server actually answers -- the server itself still runs in
REM this console, in the foreground, exactly as before, so Ctrl+C and the
REM visible log output behave the same way they always have. By the time
REM uvicorn (run with --factory) accepts a connection, create_app() has
REM already finished running migrations, seeding, and the rest of
REM app/main.py's startup sequence, so a TCP connect is a reliable
REM readiness signal here, not just "the process started."
start /B "" powershell -NoProfile -WindowStyle Hidden -Command ^
  "$deadline = (Get-Date).AddSeconds(30); while ((Get-Date) -lt $deadline) { try { $c = New-Object System.Net.Sockets.TcpClient; $c.Connect('%FERPA_HOST%', %FERPA_PORT%); $c.Close(); Start-Process 'http://%FERPA_HOST%:%FERPA_PORT%'; exit 0 } catch { Start-Sleep -Milliseconds 500 } }; exit 1"

.venv\Scripts\uvicorn app.main:create_app --factory --host %FERPA_HOST% --port %FERPA_PORT%
