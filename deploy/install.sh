#!/bin/bash
# Idempotent server setup/upgrade. Runs as root from /opt/polyarb (a git checkout).
set -euxo pipefail
source /etc/polyarb.env
cd /opt/polyarb
timedatectl set-timezone Europe/Vienna || true
id polyarb || useradd -r -m -d /home/polyarb -s /usr/sbin/nologin polyarb
chgrp polyarb /etc/polyarb.env && chmod 640 /etc/polyarb.env
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q --disable-pip-version-check -r requirements.txt
mkdir -p data /var/www/polyarb
# Paper reset: a new value in deploy/RESET_ID archives the paper data once and the bot starts fresh
# with its starting capital. Nothing is deleted – old data moves to data/archive-<timestamp>/.
if [ -f deploy/RESET_ID ] && [ "$(cat deploy/RESET_ID)" != "$(cat data/.reset_id 2>/dev/null || true)" ]; then
  systemctl stop polyarb.service || true
  ARCH="data/archive-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$ARCH"
  for f in polyarb.sqlite polyarb.sqlite-journal polyarb.sqlite-wal portfolio.json; do
    if [ -e "data/$f" ]; then mv "data/$f" "$ARCH/"; fi
  done
  cp deploy/RESET_ID data/.reset_id
fi
[ -f /var/www/polyarb/index.html ] || echo '<meta http-equiv="refresh" content="60"><p style="font-family:sans-serif">Bot startet – Dashboard erscheint in ~10 Minuten.</p>' > /var/www/polyarb/index.html
chown -R polyarb:polyarb data /var/www/polyarb
cp deploy/systemd/* /etc/systemd/system/

FIRST=0
if ! grep -q '^DASH_URL=' /etc/polyarb.env; then
  FIRST=1
  IP=$(curl -s4 --max-time 10 https://api.ipify.org || hostname -I | awk '{print $1}')
  echo "DASH_URL=https://${IP//./-}.sslip.io" >> /etc/polyarb.env
fi
source /etc/polyarb.env
if [ ! -f /etc/caddy/.polyarb ]; then
  HOST=${DASH_URL#https://}
  HASH=$(caddy hash-password --plaintext "$DASH_PASS")
  printf '%s {\n  basicauth {\n    %s %s\n  }\n  root * /var/www/polyarb\n  file_server\n}\n' "$HOST" "$DASH_USER" "$HASH" > /etc/caddy/Caddyfile
  touch /etc/caddy/.polyarb
  systemctl restart caddy
fi
systemctl daemon-reload
systemctl enable polyarb.service polyarb-dash.timer polyarb-watch.timer polyarb-daily.timer polyarb-report.timer polyarb-update.timer
systemctl start polyarb-dash.timer polyarb-watch.timer polyarb-daily.timer polyarb-report.timer polyarb-update.timer
systemctl restart polyarb.service
if [ "$FIRST" = 1 ]; then
  sleep 5
  sudo -u polyarb bash -c 'set -a; source /etc/polyarb.env; cd /opt/polyarb && .venv/bin/python deploy/notify.py start' || true
fi
