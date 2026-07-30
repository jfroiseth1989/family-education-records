#!/usr/bin/env bash
# Set up (if needed) and run the FERPA Evidence Manager locally.
#
# What this does, in order:
#   1. Creates a Python virtual environment in .venv/ if one doesn't exist.
#   2. Installs dependencies from pyproject.toml via pip, from PyPI only --
#      nothing else is downloaded. See docs/PRIVACY_SECURITY.md for the
#      full privacy plan if you want to audit this before running it.
#   3. Starts the app bound to 127.0.0.1 only (never a public address) and
#      opens it in your default browser.
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

echo "Starting FERPA Evidence Manager at http://${HOST}:${PORT}"
echo "(Press Ctrl+C to stop.)"

(
  sleep 1.5
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "http://${HOST}:${PORT}" >/dev/null 2>&1 || true
  elif command -v open >/dev/null 2>&1; then
    open "http://${HOST}:${PORT}" >/dev/null 2>&1 || true
  fi
) &

exec .venv/bin/uvicorn app.main:create_app --factory --host "${HOST}" --port "${PORT}"
