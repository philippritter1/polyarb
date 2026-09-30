#!/bin/bash
# Pulls the latest code from GitHub; if it changed, re-runs install.sh and restarts the bot.
set -eo pipefail
cd /opt/polyarb
git fetch -q origin main
OLD=$(git rev-parse HEAD); NEW=$(git rev-parse origin/main)
[ "$OLD" = "$NEW" ] && exit 0
git reset -q --hard origin/main
# since 01.10. only a failed update sends a push (the 4-hour report is the regular message)
if bash deploy/install.sh >> data/update.log 2>&1; then
  exit 0
fi
MSG="Update ${NEW:0:7} FEHLGESCHLAGEN - siehe data/update.log"
sudo -u polyarb bash -c "set -a; source /etc/polyarb.env; cd /opt/polyarb && .venv/bin/python deploy/notify.py msg '$MSG'" || true
