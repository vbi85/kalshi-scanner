#!/usr/bin/env bash
# Give this server read-only access to the private GitHub repo.  Type:  kalshi-github-key
set -euo pipefail
mkdir -p /root/.ssh && chmod 700 /root/.ssh
[ -f /root/.ssh/id_ed25519 ] || ssh-keygen -t ed25519 -N "" -f /root/.ssh/id_ed25519 -q -C "kalshi-droplet"
echo
echo "Copy the ONE line below (starts with ssh-ed25519) into:"
echo "GitHub > kalshi-scanner > Settings > Deploy keys > Add deploy key   (leave write access OFF)"
echo
cat /root/.ssh/id_ed25519.pub
echo
echo "After adding it, type:  kalshi-update"
