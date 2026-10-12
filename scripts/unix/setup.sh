#!/usr/bin/env bash
# One-time setup of Glimpse on macOS or Linux.
# Creates .venv, installs Glimpse, and (first run only) creates config.yaml + .env,
# asking for a settings-panel password. Safe to run again.
#
#   ./scripts/unix/setup.sh
set -euo pipefail
cd "$(dirname "$0")/../.."

PY="${PYTHON:-python3}"
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
  echo "Python 3.11+ is required (macOS: brew install python@3.12)." >&2
  exit 1
fi

[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install --upgrade pip --quiet
.venv/bin/pip install -e . --quiet
echo "Installed Glimpse into .venv"

if [ ! -f config.yaml ]; then
  .venv/bin/glimpse init
else
  echo "config.yaml already exists; keeping it."
fi
chmod 600 .env 2>/dev/null || true

cat <<'EOF'

Next steps:
  1. Tailscale (public links + private settings panel):
       tailscale funnel --bg 8000
       tailscale serve --bg --https=8443 http://127.0.0.1:8001
       .venv/bin/glimpse set-base-url https://<this-machine>.<tailnet>.ts.net
  2. Start automatically:
       macOS:  ./scripts/unix/install-macos.sh
       Linux:  ./scripts/unix/install-linux.sh
  3. Open http://127.0.0.1:8001 and add your calendars.
EOF
