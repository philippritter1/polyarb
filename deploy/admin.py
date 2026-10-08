"""Settings page of the dashboard: enter secrets from the phone instead of an SSH console.

Runs as polyarb on 127.0.0.1:8787 (deploy/systemd/polyarb-admin.service); Caddy forwards /admin* to it behind the
dashboard's basic auth. It can do exactly two things – store the Odds API key in data/secrets.env (mode 600,
read by arb/sharp.py on every step, no restart), and switch the order gateway (arb/orders.py) between
"off" (emergency stop) and "dry" (dry run) in data/live.json. Live trading cannot be switched on here. It never
shows a stored key (only its last 4 characters) and accepts nothing else. Read-only: /admin/diag (and
/admin/diag.json) shows service state, memory, disk and redacted log tails (deploy/diag.py – fixed read-only
queries only, nothing can be started, stopped or changed). A new key is first checked against The Odds API's free /sports
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import diag  # noqa: E402

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


def live_section() -> str:
    import glob
    try:
        with open(os.path.join(DATA, "live.json"), encoding="utf-8") as f:
            mode = json.load(f).get("mode", "off")
    except (OSError, ValueError):
        mode = "off"
    rows = []
    for path in sorted(glob.glob(os.path.join(DATA, "live-status-*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                st = json.load(f)
        except (OSError, ValueError):
            continue
        rows.append(f"<li><b>{html.escape(str(st.get('scenario')))}</b>: {html.escape(str(st.get('mode')))} · "
                    f"offen {st.get('open_orders', 0)} Orders / {st.get('open_usd', 0)} $ · gesetzt {st.get('gesetzt', 0)}"
                    f" · storniert {st.get('storniert', 0)} · abgelehnt {st.get('abgelehnt', 0)}</li>")
    label = {"off": "Aus (Not-Aus)", "dry": "Trockenlauf", "live": "Echt"}.get(mode, mode)
    radios = "".join(
        f'<label style="display:block;margin:6px 0"><input type="radio" name="mode" value="{v}"'
        f'{" checked" if mode == v else ""}{" disabled" if v == "live" else ""}> {t}</label>'
        for v, t in (("off", "Aus – Not-Aus, alle Orders stornieren"), ("dry", "Trockenlauf – Orders nur protokollieren"),
                     ("live", "Echt – noch nicht verfügbar")))
    return (f"<h2>Order-Modul (Market Making)</h2><p>Modus: <b>{label}</b></p>"
            f"<ul>{''.join(rows) or '<li>noch kein Lauf</li>'}</ul>"
            f'<form method="post" action="/admin/live-mode">{radios}<button type="submit">Übernehmen</button></form>')


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
{live_section()}
<p><a href="/admin/diag">→ Server-Diagnose</a> · <a href="/sharp_fussball/">→ Tab Fußball vs. Pinnacle</a> ·
<a href="/">→ Dashboard</a></p>
</body></html>"""


STYLE = """<style>body{font-family:system-ui,sans-serif;max-width:1000px;margin:24px auto;padding:0 16px;color:#111}
table{border-collapse:collapse;font-size:14px;width:100%} td,th{border-bottom:1px solid #ddd;padding:4px 6px;text-align:left}
pre{background:#f4f4f4;padding:8px;overflow-x:auto;font-size:12px;white-space:pre-wrap;word-break:break-all}
.bad{color:#b00020;font-weight:bold} a{color:#0645ad}
@media (prefers-color-scheme: dark){body{background:#111;color:#eee} pre{background:#222} a{color:#8ab4f8}
td,th{border-color:#333}}</style>"""


def _head(title: str) -> str:
    return (f'<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>{title}</title>{STYLE}</head><body>')


def diag_page(d: "diag.Diag") -> str:
    s = d.summary()
    e = html.escape
    sy = s["system"]
    mem = sy["mem_mb"]
    disk = " · ".join(f"{k}: {v['free_gb']} GB frei ({v['used_pct']} % belegt)" for k, v in sy["disk"].items())
    rows = "".join(
        f"<tr><td><a href=\"/admin/diag/log?unit={urllib.parse.quote(u['unit'])}\">{e(u['unit'])}</a></td>"
        f"<td class=\"{'bad' if u['unit'] in s['failed'] else ''}\">{e(u['active'])}/{e(u['sub'])}</td>"
        f"<td>{e(str(u.get('result', '')))}</td><td>{e(str(u.get('restarts', '')))}</td>"
        f"<td>{e(str(u.get('mem_mb') if u.get('mem_mb') is not None else ''))}</td><td>{e(str(u.get('since', '')))}</td></tr>"
        for u in s["units"])
    files = "".join(f"<tr><td>{e(f['file'])}</td><td>{f['mb']}</td><td>{f['age_min']}</td></tr>" for f in s["files"])
    failed = (f'<p class="bad">Fehlgeschlagen: {e(", ".join(s["failed"]))}</p>' if s["failed"] else "<p>Keine Unit fehlgeschlagen.</p>")
    return (_head("polyarb – Diagnose") + f"<h1>Server-Diagnose</h1><p>Stand {e(s['ts'])} · nur lesend · "
            f'<a href="/admin/diag.json">JSON</a> · <a href="/admin/">← Einstellungen</a></p>'
            f"<p>RAM verfügbar {mem.get('MemAvailable', '?')} von {mem.get('MemTotal', '?')} MB · Swap frei "
            f"{mem.get('SwapFree', '?')} MB · Last {e(str(sy['load']))} ({sy['cpus']} CPUs) · läuft seit "
            f"{sy['uptime_h']} h<br>Platte {e(disk)}</p>{failed}"
            f"<h2>Dienste</h2><table><tr><th>Unit (→ Log)</th><th>Status</th><th>Ergebnis</th><th>Neustarts</th>"
            f"<th>RAM MB</th><th>aktiv seit</th></tr>{rows}</table>"
            f"<h2>Warnungen und Fehler (24 h)</h2><pre>{e(s['problems']) or '–'}</pre>"
            f"<h2>Timer</h2><pre>{e(s['timers'])}</pre>"
            f"<h2>Größte Dateien in data/</h2><table><tr><th>Datei</th><th>MB</th><th>geändert vor min</th></tr>{files}</table>"
            "</body></html>")


def log_page(d: "diag.Diag", query: str) -> str:
    q = urllib.parse.parse_qs(query)
    unit = (q.get("unit") or [""])[0]
    try:
        n = int((q.get("n") or ["80"])[0])
    except ValueError:
        n = 80
    warn = (q.get("warn") or [""])[0] == "1"
    e, qu = html.escape, urllib.parse.quote(unit)
    return (_head("polyarb – Log") + f"<h1>Log {e(unit)}</h1><p>"
            f'<a href="/admin/diag/log?unit={qu}&n=80">80 Zeilen</a> · <a href="/admin/diag/log?unit={qu}&n=300">300</a> · '
            f'<a href="/admin/diag/log?unit={qu}&n=300&warn=1">nur Warnungen</a> · <a href="/admin/diag">← Diagnose</a>'
            f"</p><pre>{e(d.logs(unit, n, warn))}</pre></body></html>")


class Handler(BaseHTTPRequestHandler):
    server_version = "polyarb-admin"
    fetch = None  # tests: replaces the key check
    run = None    # tests: replaces systemctl/journalctl

    def _send(self, body: str, code: int = 200, ctype: str = "text/html") -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        path, _, query = self.path.partition("?")
        path = path.rstrip("/")
        if path in ("/admin", ""):
            return self._send(page())
        if path == "/admin/diag":
            return self._send(diag_page(self.diag()))
        if path == "/admin/diag.json":
            return self._send(json.dumps(self.diag().summary(), ensure_ascii=False, indent=1), ctype="application/json")
        if path == "/admin/diag/log":
            return self._send(log_page(self.diag(), query))
        self._send("not found", 404)

    def diag(self) -> "diag.Diag":
        return diag.Diag(DATA, run=self.run) if self.run else diag.Diag(DATA)

    def do_POST(self):  # noqa: N802
        path = self.path.rstrip("/")
        if path not in ("/admin/odds-key", "/admin/live-mode"):
            return self._send("not found", 404)
        n = int(self.headers.get("Content-Length") or 0)
        if n > 4096:
            return self._send(page("Zu viele Daten.", False), 413)
        form = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
        if path == "/admin/live-mode":
            mode = (form.get("mode") or [""])[0]
            if mode not in ("off", "dry"):  # "live" cannot be switched on from here
                return self._send(page("Dieser Modus ist nicht erlaubt.", False), 400)
            tmp = os.path.join(DATA, "live.json.tmp")
            os.makedirs(DATA, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"mode": mode, "ts": time.time()}, f)
            os.replace(tmp, os.path.join(DATA, "live.json"))
            return self._send(page("Order-Modul: " + ("aus – alle Orders werden storniert." if mode == "off"
                                                       else "Trockenlauf – Orders werden nur protokolliert.")))
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
