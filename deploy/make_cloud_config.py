"""Erzeugt die cloud-config ("User Data") für einen frischen Ubuntu-24.04-Server.

Der Server klont den Code von GitHub und aktualisiert sich danach alle 10 Minuten selbst
(deploy/update.sh). Zugangsdaten stehen in deploy/SECRETS.txt (nicht im Repo).

  python deploy/make_cloud_config.py https://github.com/<user>/polyarb.git
"""
from __future__ import annotations

import secrets
import string
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEC = ROOT / "deploy" / "SECRETS.txt"


def load_or_create_secrets():
    if SEC.exists():
        t = SEC.read_text()
        pw = t.split("Dashboard-Login: ")[1].split("\n")[0].split(" / ")[1].strip()
        topic = t.split("abonnieren): ")[1].split()[0].strip()
    else:
        pw = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))
        topic = "polyarb-" + secrets.token_hex(6)
        SEC.write_text(f"Dashboard-Login: philipp / {pw}\nntfy-Topic (in der ntfy-App abonnieren): {topic}\n")
    return "philipp", pw, topic


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: make_cloud_config.py <git-repo-url>")
    repo = sys.argv[1]
    user, pw, topic = load_or_create_secrets()
    env = (f"NTFY_TOPIC={topic}\nDASH_USER={user}\nDASH_PASS={pw}\nSTART_CAPITAL=2500\n"
           f"PYTHONUNBUFFERED=1\nREPO_URL={repo}\n")
    cc = "#cloud-config\npackage_update: true\npackages: [python3-venv, caddy, git, curl]\nwrite_files:\n"
    cc += "  - path: /etc/polyarb.env\n    permissions: '0640'\n    content: |\n"
    cc += "".join(f"      {l}\n" for l in env.splitlines())
    cc += ("runcmd:\n"
           "  - [bash, -c, 'source /etc/polyarb.env && git clone \"$REPO_URL\" /opt/polyarb "
           "&& bash /opt/polyarb/deploy/install.sh > /var/log/polyarb-install.log 2>&1']\n")
    out = ROOT / "deploy" / "cloud-config.yaml"
    out.write_text(cc)
    print(f"{out} ({len(cc)} bytes)")


if __name__ == "__main__":
    main()
