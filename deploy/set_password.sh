#!/usr/bin/env bash
# Change the dashboard password.  Type:  kalshi-password
# The new password is shown as you type so you can check it.
set -euo pipefail
ENV=/opt/kalshi-scanner/.env
while true; do
  read -r -p "New dashboard password (at least 10 characters, shown as you type): " PW
  if [ ${#PW} -lt 10 ]; then echo "Too short. Try again."; continue; fi
  case "$PW" in *" "*|*"#"*|*"="*|*"/"*|*"\\"*|*"&"*|*"'"*|*'"'*) echo "Avoid spaces and these symbols: # = / \\ & ' \"  Try again."; continue;; esac
  read -r -p "Type it again to confirm: " PW2
  [ "$PW" = "$PW2" ] && break
  echo "They don't match. Try again."
done
sed -i "s/^DASH_PASSWORD=.*/DASH_PASSWORD=$PW/" "$ENV"
systemctl restart kalshi-dashboard
clear
echo "Password changed. Sign in on your phone with the new password."
