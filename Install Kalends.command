#!/usr/bin/env bash
# macOS: double-click to install or update Kalends. (First time: right-click > Open.)
cd "$(dirname "$0")" && ./scripts/unix/install.sh
echo
read -r -p "Press Return to close this window." _
