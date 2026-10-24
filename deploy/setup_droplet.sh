#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu 24.04 DigitalOcean droplet.
# From inside the cloned repo, run as root:   bash deploy/setup_droplet.sh
set -euo pipefail
APP=/opt/kalshi-scanner
apt-get update -y && apt-get install -y python3-venv python3-pip git ufw
id -u scanner >/dev/null 2>&1 || useradd -r -m -d "$APP" scanner
mkdir -p "$APP" && cp -r . "$APP" && cd "$APP"
python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
[ -f .env ] || cp .env.example .env
mkdir -p data && chown -R scanner:scanner "$APP" && chmod 600 .env
cp deploy/kalshi-engine.service deploy/kalshi-dashboard.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now kalshi-engine kalshi-dashboard
ufw allow OpenSSH && ufw allow 8080/tcp && ufw --force enable
echo "Done. Edit $APP/.env (set DASH_PASSWORD!), then: systemctl restart kalshi-engine kalshi-dashboard"
echo "Dashboard: http://$(curl -s ifconfig.me):8080"
