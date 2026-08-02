#!/usr/bin/env bash
# Set up (if needed) and run the FERPA Evidence Manager locally.
#
# What this does, in order:
#   1. Creates a Python virtual environment in .venv/ if one doesn't exist.
#   2. Installs dependencies from pyproject.toml via pip, from PyPI only --
#      nothing else is downloaded. See docs/PRIVACY_SECURITY.md for the
#      full privacy plan if you want to audit this before running it.
#   3. Starts the app bound to 127.0.0.1 only (never a public address),
#      waits until it's actually accepting connections, and only then
#      opens it in your default browser (Security Phase Step 6) -- a
#      fixed sleep here would either open the browser too early, showing
#      a connection-refused error, or waste time waiting longer than
#      necessary.
#
# Safe to re-run any time -- steps 1-2 are skipped if already done.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [ ! -d .venv ]; then
    echo "Creating virtual environment in .venv/ ..."
    python3 -m venv .venv
fi

echo "Installing dependencies..."
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -e .

HOST="${FERPA_HOST:-127.0.0.1}"
PORT="${FERPA_PORT:-8420}"
READY_TIMEOUT_SECONDS=30

echo "Starting FERPA Evidence Manager at http://${HOST}:${PORT}"
echo "(Press Ctrl+C to stop.)"

.venv/bin/uvicorn app.main:create_app --factory --host "${HOST}" --port "${PORT}" &
SERVER_PID=$!

# Whatever ends this script -- Ctrl+C, a normal exit, an error -- also
# stops the server; nothing is left running in the background afterward.
trap 'kill "${SERVER_PID}" 2>/dev/null || true' EXIT

# Poll the port itself rather than guessing with a fixed sleep. By the
# time uvicorn (run with --factory) accepts a connection, create_app()
# has already finished running migrations, seeding, and the rest of
# app/main.py's startup sequence -- a bare TCP connect is therefore a
# reliable readiness signal here, not just "the process started."
attempts=$((READY_TIMEOUT_SECONDS * 2))
ready=0
for _ in $(seq 1 "${attempts}"); do
    if ! kill -0 "${SERVER_PID}" 2>/dev/null; then
        echo "Server process exited unexpectedly during startup." >&2
        wait "${SERVER_PID}"
        exit $?
    fi
    if (exec 3<>"/dev/tcp/${HOST}/${PORT}") 2>/dev/null; then
        exec 3<&- 3>&-
        ready=1
        break
    fi
    sleep 0.5
done

if [ "${ready}" -eq 1 ]; then
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "http://${HOST}:${PORT}" >/dev/null 2>&1 || true
    elif command -v open >/dev/null 2>&1; then
        open "http://${HOST}:${PORT}" >/dev/null 2>&1 || true
    fi
else
    echo "Warning: server did not become ready within ${READY_TIMEOUT_SECONDS}s -- open http://${HOST}:${PORT} manually." >&2
fi

wait "${SERVER_PID}"
