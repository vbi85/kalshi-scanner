#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu 24.04 DigitalOcean droplet.
# From inside the cloned repo, run as root:   bash deploy/setup_droplet.sh
#
# Gives you:  https://<your-ip-with-dashes>.sslip.io   (free HTTPS, works as a phone app)
set -euo pipefail
APP=/opt/kalshi-scanner
IP=$(curl -s -4 ifconfig.me)
HOST="${DASH_HOST:-${IP//./-}.sslip.io}"

echo "==> Installing system packages"
apt-get update -y
apt-get install -y python3-venv python3-pip git ufw debian-keyring debian-archive-keyring apt-transport-https curl gnupg

echo "==> Installing Caddy (automatic HTTPS)"
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y && apt-get install -y caddy
fi

echo "==> Installing the bot"
id -u scanner >/dev/null 2>&1 || useradd -r -m -d "$APP" scanner
mkdir -p "$APP" && cp -r . "$APP" && cd "$APP"
python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
if [ ! -f .env ]; then
  cp .env.example .env
  PW=$(openssl rand -base64 12 | tr -d '/+=')
  sed -i "s/^DASH_PASSWORD=.*/DASH_PASSWORD=$PW/" .env
  NEW_PW=1
fi
mkdir -p data && chown -R scanner:scanner "$APP" && chmod 600 .env

echo "==> Starting services"
cp deploy/kalshi-engine.service deploy/kalshi-dashboard.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now kalshi-engine kalshi-dashboard

cat > /etc/caddy/Caddyfile <<EOF
$HOST {
    encode gzip
    reverse_proxy 127.0.0.1:8080
}
EOF
systemctl restart caddy

echo "==> Firewall: only SSH + HTTPS open"
ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp
ufw delete allow 8080/tcp >/dev/null 2>&1 || true
ufw --force enable

echo
echo "=============================================================="
echo " Dashboard (open on your phone):  https://$HOST"
echo " Username: $(grep ^DASH_USER= .env | cut -d= -f2)"
[ "${NEW_PW:-}" = 1 ] && echo " Password: $PW      <-- save this now"
echo " Next: edit $APP/.env for text alerts, then:"
echo "       systemctl restart kalshi-engine kalshi-dashboard"
echo "=============================================================="
