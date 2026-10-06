"""Settings page of the dashboard: enter secrets from the phone instead of an SSH console.

Runs as polyarb on 127.0.0.1:8787 (deploy/systemd/polyarb-admin.service); Caddy forwards /admin* to it behind the
dashboard's basic auth. It can do exactly one thing – store the Odds API key in data/secrets.env (mode 600,
read by arb/sharp.py on every step, no restart). It never shows a stored key (only its last 4 characters),
runs no commands and accepts nothing else. A new key is first checked against The Odds API's free /sports
endpoint (costs no credit).
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA = os.environ.get("POLYARB_DATA", "data")
SECRETS = os.path.join(DATA, "secrets.env")
KEY_RE = re.compile(r"^[A-Za-z0-9]{16,64}$")
ODDS_CHECK = "https://api.the-odds-api.com/v4/sports"


def read_secrets(path: str = None) -> dict:
    out = {}
    try:
        with open(path or SECRETS, encoding="utf-8") as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k and v:
                    out[k] = v
    except OSError:
        pass
    return out


def write_secret(name: str, value: str, path: str = None) -> None:
    path = path or SECRETS
    cur = read_secrets(path)
    cur[name] = value
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("".join(f"{k}={v}\n" for k, v in cur.items()))
    os.replace(tmp, path)


def check_key(key: str, fetch=None) -> str:
    """'' if The Odds API accepts the key, else a short reason. /sports is free."""
    url = ODDS_CHECK + "?" + urllib.parse.urlencode({"apiKey": key})
    try:
        if fetch:
            fetch(url)
        else:
            with urllib.request.urlopen(url, timeout=15) as r:
                r.read(1)
        return ""
    except urllib.error.HTTPError as e:
        return "Key abgelehnt (401)" if e.code == 401 else f"Odds API antwortet {e.code}"
    except Exception as e:  # noqa – network: store anyway, the scenario retries
        return f"nicht prüfbar ({type(e).__name__})"


def page(msg: str = "", ok: bool = True) -> str:
    sec = read_secrets()
    key = sec.get("ODDS_API_KEY", "")
    status = f"gespeichert (…{html.escape(key[-4:])})" if key else "noch keiner"
    used = ""
    try:
        with open(os.path.join(DATA, "odds-api.json"), encoding="utf-8") as f:
            st = json.load(f)
        month = time.strftime("%Y-%m", time.gmtime())
        used = f"<p>Verbrauchte Credits diesen Monat: <b>{st.get('used', {}).get(month, 0)}</b> von 450 (Gratis-Plan 500)</p>"
    except (OSError, ValueError):
        pass
    note = f'<p class="{"ok" if ok else "err"}">{html.escape(msg)}</p>' if msg else ""
    return f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>polyarb – Einstellungen</title>
<style>body{{font-family:system-ui,sans-serif;max-width:560px;margin:24px auto;padding:0 16px;color:#111}}
input{{width:100%;font-size:16px;padding:10px;box-sizing:border-box;margin:8px 0}}
button{{font-size:16px;padding:10px 16px}} .ok{{color:#0a7a2f}} .err{{color:#b00020}} a{{color:#0645ad}}
@media (prefers-color-scheme: dark){{body{{background:#111;color:#eee}} a{{color:#8ab4f8}}}}</style></head><body>
<h1>Einstellungen</h1>{note}
<h2>Odds API (Fußball vs. Pinnacle)</h2>
<p>Key: <b>{status}</b></p>{used}
<form method="post" action="/admin/odds-key">
<label>Neuer Key von <a href="https://the-odds-api.com" target="_blank" rel="noopener">the-odds-api.com</a>
<input name="key" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="Key einfügen"></label>
<button type="submit">Prüfen und speichern</button></form>
<p>Wirkt ohne Neustart beim nächsten Durchlauf (alle 10 Minuten). Der Key wird nie angezeigt.</p>
<p><a href="/sharp_fussball/">→ Tab Fußball vs. Pinnacle</a> · <a href="/">→ Dashboard</a></p>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "polyarb-admin"
    fetch = None  # tests: replaces the key check

    def _send(self, body: str, code: int = 200) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path.split("?")[0].rstrip("/") in ("/admin", ""):
            return self._send(page())
        self._send("not found", 404)

    def do_POST(self):  # noqa: N802
        if self.path.rstrip("/") != "/admin/odds-key":
            return self._send("not found", 404)
        n = int(self.headers.get("Content-Length") or 0)
        if n > 4096:
            return self._send(page("Zu viele Daten.", False), 413)
        form = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
        key = (form.get("key") or [""])[0].strip()
        if not KEY_RE.match(key):
            return self._send(page("Das sieht nicht wie ein Odds-API-Key aus (16–64 Buchstaben/Ziffern).", False), 400)
        why = check_key(key, self.fetch)
        if why.startswith("Key abgelehnt"):
            return self._send(page(f"{why} – nicht gespeichert.", False), 400)
        write_secret("ODDS_API_KEY", key)
        self._send(page("Gespeichert." + (f" Hinweis: {why}." if why else " Key von der Odds API bestätigt.")))

    def log_message(self, fmt, *args):  # no request lines (they would contain nothing secret, but keep it quiet)
        pass


def serve(host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


if __name__ == "__main__":
    serve(port=int(sys.argv[1]) if len(sys.argv) > 1 else 8787).serve_forever()
