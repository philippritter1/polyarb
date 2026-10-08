#!/bin/bash
# Idempotent server setup/upgrade. Runs as root from /opt/polyarb (a git checkout).
set -euxo pipefail
source /etc/polyarb.env
cd /opt/polyarb
timedatectl set-timezone Europe/Vienna || true
id polyarb || useradd -r -m -d /home/polyarb -s /usr/sbin/nologin polyarb
# the diagnostics page (/admin/diag, read-only) shows the polyarb service logs: journal read access
usermod -aG systemd-journal polyarb || true
chgrp polyarb /etc/polyarb.env && chmod 640 /etc/polyarb.env
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q --disable-pip-version-check -r requirements.txt
mkdir -p data /var/www/polyarb
# Paper reset: a new value in deploy/RESET_ID archives the paper data once and the bot starts fresh
# with its starting capital. Nothing is deleted – old data moves to data/archive-<timestamp>/.
if [ -f deploy/RESET_ID ] && [ "$(cat deploy/RESET_ID)" != "$(cat data/.reset_id 2>/dev/null || true)" ]; then
  systemctl stop polyarb.service 'polyarb-scenario@*.service' || true
  ARCH="data/archive-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$ARCH"
  for f in data/polyarb.sqlite data/polyarb.sqlite-journal data/polyarb.sqlite-wal data/portfolio.json data/scenario-*; do
    if [ -e "$f" ]; then mv "$f" "$ARCH/"; fi
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
# v2 (06.10.): /admin* goes to the settings page (deploy/admin.py, 127.0.0.1:8787), same password as the dashboard.
# The new file is validated first; if caddy rejects it, the old one stays and the dashboard keeps running.
if [ ! -f /etc/caddy/.polyarb2 ]; then
  HOST=${DASH_URL#https://}
  HASH=$(caddy hash-password --plaintext "$DASH_PASS")
  printf '%s {\n  basicauth {\n    %s %s\n  }\n  handle /admin* {\n    reverse_proxy 127.0.0.1:8787\n  }\n  handle {\n    root * /var/www/polyarb\n    file_server\n  }\n}\n' "$HOST" "$DASH_USER" "$HASH" > /etc/caddy/Caddyfile.new
  if caddy validate --config /etc/caddy/Caddyfile.new --adapter caddyfile; then
    [ -f /etc/caddy/Caddyfile ] && cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak
    mv /etc/caddy/Caddyfile.new /etc/caddy/Caddyfile
    touch /etc/caddy/.polyarb /etc/caddy/.polyarb2
    systemctl restart caddy
  else
    echo "Caddyfile v2 ungültig – alte Konfiguration bleibt" >&2
  fi
fi
systemctl daemon-reload
systemctl enable polyarb-admin.service
systemctl restart polyarb-admin.service
systemctl enable polyarb.service polyarb-dash.timer polyarb-watch.timer polyarb-report.timer polyarb-update.timer polyarb-study.timer polyarb-kalshi.timer polyarb-wxobs.timer
systemctl start polyarb-dash.timer polyarb-watch.timer polyarb-report.timer polyarb-update.timer polyarb-study.timer polyarb-kalshi.timer polyarb-wxobs.timer
# seit 01.10.: nur noch der 4-h-Bericht (08 Uhr gehört jetzt dazu), kein eigener Tagesbericht
systemctl disable --now polyarb-daily.timer || true
systemctl restart polyarb-report.timer  # neuer Zeitplan
# a study run started with the old code would go on for hours: stop it, the timers start the new code
systemctl stop polyarb-kalshi.service polyarb-wxobs.service || true
systemctl restart polyarb.service
# paper scenarios from config.yaml: one polyarb-scenario@<name> per enabled entry, the rest stopped
SCEN=$(.venv/bin/python -c "from arb.config import load_config; c=load_config('config.yaml'); print(' '.join(k for k, v in (c.get('scenarios') or {}).items() if v.get('enabled')))")
for u in $(ls /etc/systemd/system/multi-user.target.wants/ 2>/dev/null | grep '^polyarb-scenario@' || true); do
  n=${u#polyarb-scenario@}; n=${n%.service}
  case " $SCEN " in *" $n "*) ;; *) systemctl disable --now "$u" || true ;; esac
done
for n in $SCEN; do
  systemctl enable "polyarb-scenario@$n.service"
  systemctl restart "polyarb-scenario@$n.service"
done
if [ "$FIRST" = 1 ]; then
  sleep 5
  sudo -u polyarb bash -c 'set -a; source /etc/polyarb.env; cd /opt/polyarb && .venv/bin/python deploy/notify.py start' || true
fi
