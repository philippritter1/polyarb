#!/bin/bash
# Stellt einen bestehenden Polyarb-Server (alte Installation ohne Git) auf die GitHub-Version um.
# Aufruf auf dem Server als root:
#   curl -sL https://raw.githubusercontent.com/philippritter1/polyarb/main/deploy/migrate.sh | bash
set -euxo pipefail
REPO=https://github.com/philippritter1/polyarb.git
command -v git >/dev/null || (apt-get update -q && apt-get install -y -q git)
systemctl stop polyarb || true
if [ -d /opt/polyarb ] && [ ! -d /opt/polyarb/.git ]; then
  rm -rf /opt/polyarb-old
  mv /opt/polyarb /opt/polyarb-old
fi
[ -d /opt/polyarb/.git ] || git clone "$REPO" /opt/polyarb
mkdir -p /opt/polyarb/data
if [ -d /opt/polyarb-old/data ]; then cp -an /opt/polyarb-old/data/. /opt/polyarb/data/; fi
grep -q '^REPO_URL=' /etc/polyarb.env || echo "REPO_URL=$REPO" >> /etc/polyarb.env
bash /opt/polyarb/deploy/install.sh
sudo -u polyarb bash -c 'set -a; source /etc/polyarb.env; cd /opt/polyarb && .venv/bin/python deploy/notify.py msg "Server auf GitHub-Version umgestellt - Auto-Update aktiv"' || true
echo "FERTIG - Polyarb laeuft jetzt aus GitHub und aktualisiert sich alle 10 Minuten."
