#!/usr/bin/env bash
# Pull the latest code from GitHub and restart. Run as root.
set -euo pipefail
cd /opt/kalshi-scanner
sudo -u scanner git pull
.venv/bin/pip install -q -r requirements.txt
systemctl restart kalshi-engine kalshi-dashboard
echo "Updated and restarted."
