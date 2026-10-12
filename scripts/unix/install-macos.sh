#!/usr/bin/env bash
# Start Glimpse at login on macOS (launchd), restart it if it stops.
# Logs: logs/glimpse.log.  Remove with: ./scripts/unix/install-macos.sh --uninstall
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LABEL="io.github.glimpse"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ "${1:-}" = "--uninstall" ]; then
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed Glimpse from login items."
  exit 0
fi

[ -x "$ROOT/.venv/bin/glimpse" ] || { echo "Run scripts/unix/setup.sh first." >&2; exit 1; }
mkdir -p "$ROOT/logs" "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$ROOT/.venv/bin/glimpse</string>
    <string>-c</string><string>$ROOT/config.yaml</string>
    <string>serve</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$ROOT/logs/glimpse.log</string>
  <key>StandardErrorPath</key><string>$ROOT/logs/glimpse.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
sleep 3
curl -fsS http://127.0.0.1:8000/healthz && echo && echo "Glimpse is running and will start at login."
