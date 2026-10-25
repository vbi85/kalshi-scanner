#!/usr/bin/env bash
# Pull the latest code from GitHub and restart.  After the first run you can just type:  kalshi-update
# Your settings (.env), trade history (data/) and Kalshi key (keys/) are never touched.
set -euo pipefail
SRC=/root/kalshi-scanner
APP=/opt/kalshi-scanner
cd "$SRC"

# Private repo: use the server's GitHub deploy key once it has been added (see: kalshi-github-key)
if [ -f /root/.ssh/id_ed25519 ] && git remote get-url origin | grep -q '^https://'; then
  if ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes -T git@github.com 2>&1 | grep -q "successfully authenticated"; then
    git remote set-url origin git@github.com:vbi85/kalshi-scanner.git
  fi
fi

if ! git pull --ff-only; then
  echo
  echo "Could not download from GitHub. The repo is private and this server has no access yet."
  echo "Fix once:  kalshi-github-key   (then add the printed key to GitHub > Settings > Deploy keys)"
  exit 1
fi

rsync -a --delete \
  --exclude '.git' --exclude '.env' --exclude 'data/' --exclude 'keys/' --exclude '.venv/' \
  "$SRC"/ "$APP"/
cd "$APP"
.venv/bin/pip install -q -r requirements.txt
cp deploy/kalshi-engine.service deploy/kalshi-dashboard.service /etc/systemd/system/
systemctl daemon-reload
chown -R scanner:scanner "$APP"
chmod +x "$SRC"/deploy/*.sh

# One-word shortcuts
ln -sf "$SRC/deploy/update.sh"       /usr/local/bin/kalshi-update
ln -sf "$SRC/deploy/set_password.sh" /usr/local/bin/kalshi-password
ln -sf "$SRC/deploy/github_key.sh"   /usr/local/bin/kalshi-github-key

systemctl restart kalshi-engine kalshi-dashboard
echo
echo "Updated to: $(git -C "$SRC" log -1 --format='%h %s')"
echo "Bot restarted. Shortcuts: kalshi-update | kalshi-password | kalshi-github-key"
