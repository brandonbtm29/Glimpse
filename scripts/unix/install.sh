#!/usr/bin/env bash
# Install or update Glimpse on macOS or Linux. On a Mac, double-click "Install Glimpse.command".
# No sudo needed. Safe to run again (that's how you update); your settings are never overwritten.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
say() { printf '  %s\n' "$*"; }
step() { printf '\n[%s/5] %s\n' "$1" "$2"; }

printf '\n  Glimpse installer\n  Folder: %s\n' "$ROOT"

step 1 "Checking for Python 3.11 or newer"
PY=""
for cand in "${PYTHON:-}" python3.13 python3.12 python3.11 python3; do
  [ -n "$cand" ] || continue
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$(command -v "$cand")"; break
  fi
done
if [ -z "$PY" ]; then
  say "Python 3.11 or newer is needed."
  if [ "$(uname)" = "Darwin" ]; then
    say "Install it from python.org (or 'brew install python@3.12'), then run this again."
    open "https://www.python.org/downloads/macos/" 2>/dev/null || true
  else
    say "Install it with your package manager (e.g. 'sudo apt install python3.12 python3.12-venv'), then run this again."
  fi
  exit 1
fi
say "Using $PY"

step 2 "Installing Glimpse"
if [ -x .venv/bin/glimpse ]; then
  # Stop a running copy from this folder so it can be updated (auto-start brings it back).
  pkill -f "$ROOT/.venv/bin/" 2>/dev/null || true
fi
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip --quiet --disable-pip-version-check
.venv/bin/python -m pip install -e "$ROOT" --quiet --disable-pip-version-check
say "Installed."

step 3 "Settings"
if [ -f config.yaml ]; then
  say "Keeping your existing config.yaml and .env."
else
  .venv/bin/glimpse -c config.yaml init --setup-later >/dev/null
  say "Created config.yaml and .env. You'll choose a password in the browser."
fi
chmod 600 .env 2>/dev/null || true

step 4 "Starting automatically when you log in"
.venv/bin/glimpse -c config.yaml autostart on

step 5 "Starting Glimpse"
.venv/bin/glimpse -c config.yaml open

printf '\n  Done. Glimpse is running in the background.\n'
say "Settings: http://127.0.0.1:8001   (or run: .venv/bin/glimpse open)"
say "Logs:     $ROOT/logs/glimpse.log"
