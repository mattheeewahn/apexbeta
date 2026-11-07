#!/usr/bin/env bash
# Raspberry Pi / Linux launcher for APEX.
#   First run : creates a virtualenv (.venv-pi) and installs requirements.
#   Every run : starts the server with gunicorn on port 5000 (override with PORT=xxxx).
# Settings are read from the .env file in this folder.
set -e
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "[!] .env file not found in $(pwd). Copy it from your PC or create one (see .env.example)."
  exit 1
fi

if [ ! -x .venv-pi/bin/python ]; then
  echo "[*] Creating virtual environment (.venv-pi)..."
  python3 -m venv .venv-pi
fi

# Install / update packages only when requirements.txt changed
if [ ! -f .venv-pi/.installed ] || [ requirements.txt -nt .venv-pi/.installed ]; then
  echo "[*] Installing packages (first time can take a few minutes on a Pi)..."
  .venv-pi/bin/pip install --upgrade pip
  .venv-pi/bin/pip install -r requirements.txt
  touch .venv-pi/.installed
fi

PORT="${PORT:-5000}"
echo "[*] Starting APEX on http://0.0.0.0:${PORT}  (Ctrl+C to stop)"
# Socket.IO needs exactly ONE worker process; concurrency comes from threads.
exec .venv-pi/bin/gunicorn -k gthread -w 1 --threads 100 --timeout 120 -b "0.0.0.0:${PORT}" server:app
