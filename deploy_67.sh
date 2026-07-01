#!/usr/bin/env bash
# Upload the 67 GIF + its full-screen page onto Pepper's own web server so the
# tablet serves them locally, OFFLINE, same-origin (the reliable way to show it).
# The teleop script auto-deploys on startup; run this manually if that fails.
#
# Usage:  ./deploy_67.sh <PEPPER_IP> [app_name]
# SSH login is nao@<ip> (password: pepper).
set -euo pipefail

IP="${1:-}"
APP="${2:-followme}"
if [ -z "$IP" ]; then
  echo "usage: ./deploy_67.sh <PEPPER_IP> [app_name]"; exit 1
fi

DIR="$(cd "$(dirname "$0")" && pwd)/assets"
DEST_DIR=".local/share/PackageManager/apps/${APP}/html"
URL="http://${IP}/apps/${APP}/67.html"

if [ ! -f "$DIR/67.gif" ] || [ ! -f "$DIR/67.html" ]; then
  echo "ERROR: missing $DIR/67.gif or $DIR/67.html"; exit 1
fi

echo "Deploying 67.gif + 67.html -> nao@${IP}:~/${DEST_DIR}/"
ssh "nao@${IP}" "mkdir -p ~/${DEST_DIR}"
scp "$DIR/67.gif" "$DIR/67.html" "nao@${IP}:${DEST_DIR}/"

echo
echo "Deployed. The GIF now lives on the robot at:"
echo "    $URL"
echo "The 67 emote will show it on the tablet automatically."
