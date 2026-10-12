#!/usr/bin/env bash
# Run Glimpse as a systemd user service (starts at boot, restarts on failure).
# Remove with: ./scripts/unix/install-linux.sh --uninstall
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
UNIT="$HOME/.config/systemd/user/glimpse.service"

if [ "${1:-}" = "--uninstall" ]; then
  systemctl --user disable --now glimpse.service 2>/dev/null || true
  rm -f "$UNIT"
  systemctl --user daemon-reload
  echo "Removed the Glimpse service."
  exit 0
fi

[ -x "$ROOT/.venv/bin/glimpse" ] || { echo "Run scripts/unix/setup.sh first." >&2; exit 1; }
mkdir -p "$(dirname "$UNIT")"
cat > "$UNIT" <<EOF
[Unit]
Description=Glimpse calendar sharing
After=network-online.target

[Service]
WorkingDirectory=$ROOT
ExecStart=$ROOT/.venv/bin/glimpse -c $ROOT/config.yaml serve
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now glimpse.service
loginctl enable-linger "$USER" 2>/dev/null || echo "Note: run 'sudo loginctl enable-linger $USER' so it starts before you log in."
sleep 3
curl -fsS http://127.0.0.1:8000/healthz && echo && echo "Glimpse is running (logs: journalctl --user -u glimpse)."
