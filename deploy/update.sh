#!/usr/bin/env bash
# Pull the latest code from GitHub and restart. Run as root:
#   bash ~/kalshi-scanner/deploy/update.sh
# Your settings (.env), trade history (data/) and Kalshi key (keys/) are never touched.
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/kalshi-scanner
cd "$SRC"
git pull --ff-only
rsync -a --delete \
  --exclude '.git' --exclude '.env' --exclude 'data/' --exclude 'keys/' --exclude '.venv/' \
  "$SRC"/ "$APP"/
cd "$APP"
.venv/bin/pip install -q -r requirements.txt
cp deploy/kalshi-engine.service deploy/kalshi-dashboard.service /etc/systemd/system/
systemctl daemon-reload
chown -R scanner:scanner "$APP"
systemctl restart kalshi-engine kalshi-dashboard
echo "Updated to $(git -C "$SRC" log -1 --format='%h %s') and restarted."
