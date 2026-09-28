"""Push-Benachrichtigungen über ntfy.sh (kostenlose App, kein Account nötig).

  python deploy/notify.py start     # "Bot läuft" + Dashboard-Link
  python deploy/notify.py watch     # alle 15 min: Alarm bei Stillstand, Kill-Switch, API-Problemen
  python deploy/notify.py daily     # tägliche Zusammenfassung
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import requests

TOPIC = os.environ.get("NTFY_TOPIC", "")
DB = os.environ.get("POLYARB_DB", "/opt/polyarb/data/polyarb.sqlite")
URL = os.environ.get("DASH_URL", "")
USER = os.environ.get("DASH_USER", "")
START = float(os.environ.get("START_CAPITAL", "2500"))
STATE = Path(os.environ.get("NOTIFY_STATE", "/opt/polyarb/data/notify_state.json"))


def push(title: str, msg: str, prio: str = "default", tags: str = "chart_with_upwards_trend"):
    if not TOPIC:
        print(f"[no topic] {title}: {msg}")
        return
    headers = {"Title": title, "Priority": prio, "Tags": tags}
    if URL:
        headers["Click"] = URL
    requests.post(f"https://ntfy.sh/{TOPIC}", data=msg.encode("utf-8"), headers=headers, timeout=10)


def q(sql, args=()):
    if not Path(DB).exists():
        return []
    db = sqlite3.connect(DB)
    try:
        return db.execute(sql, args).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        db.close()


def service_active() -> bool:
    try:
        return subprocess.run(["systemctl", "is-active", "--quiet", "polyarb"]).returncode == 0
    except FileNotFoundError:
        return True


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def save_state(s: dict):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s))


def usd(v: float) -> str:
    return f"{'-' if v < 0 else ''}${abs(v):,.2f}"


def cmd_start():
    try:
        r = requests.get("https://clob.polymarket.com/time", timeout=10)
        api = "erreichbar" if r.ok else f"Fehler HTTP {r.status_code}"
    except Exception as e:  # noqa
        api = f"NICHT erreichbar ({type(e).__name__})"
    push("Polyarb gestartet",
         f"Paper-Trading läuft mit {usd(START)}.\nPolymarket-API: {api}\n"
         f"Dashboard: {URL}\nLogin: {USER} (Passwort aus dem Setup)\n"
         "Du bekommst täglich um 8:00 eine Zusammenfassung und sofort Alarm bei Problemen.",
         tags="rocket")


def cmd_watch():
    st = load_state()
    alerts = []
    now = time.time()
    last = q("SELECT ts, equity, halted FROM equity ORDER BY ts DESC LIMIT 1")
    stale = (not last) or (now - last[0][0] > 15 * 60)
    down = not service_active()

    problem = None
    if down:
        problem = "Der Bot-Prozess läuft nicht (systemd startet ihn automatisch neu)."
    elif stale and (now - st.get("boot", now)) > 20 * 60:
        problem = ("Seit über 15 min keine neuen Daten. Wahrscheinlich ist die Polymarket-API "
                   "nicht erreichbar (Rate-Limit, Ausfall oder Geo-Sperre des Servers).")
    if problem and not st.get("problem"):
        alerts.append(("Polyarb: Problem", problem, "high", "warning"))
    if not problem and st.get("problem"):
        alerts.append(("Polyarb: wieder OK", "Der Bot liefert wieder Daten.", "default", "white_check_mark"))
    st["problem"] = problem

    halted = last[0][2] if last else None
    if halted and halted != st.get("halted"):
        alerts.append(("Polyarb: Handel pausiert", f"Kill-Switch ausgelöst: {halted}", "high", "rotating_light"))
    st["halted"] = halted
    st.setdefault("boot", now)

    # --- trade alerts: every executed paper trade since the last check
    if os.environ.get("NOTIFY_TRADES", "1") == "1":
        last_ts = st.get("last_exec_ts")
        if last_ts is None:
            last_ts = now  # first run: don't replay history
        rows = q("""SELECT ts, strategy, title, status, matched_qty, target_qty, expected_profit, realized_pnl
                    FROM executions WHERE ts > ? ORDER BY ts""", (last_ts,))
        if rows:
            st["last_exec_ts"] = rows[-1][0]
            names = {"binary_buy_all": "YES+NO kaufen", "binary_sell_all": "Split & verkaufen",
                     "negrisk_buy_all": "Korb", "negrisk_no_buy_all": "alle NO"}
            status_de = {"filled": "voll", "partial": "teilweise", "missed": "verpasst"}
            hits = [r for r in rows if r[3] in ("filled", "partial")]
            real = sum(r[7] or 0 for r in rows)
            head = (f"{len(rows)} Trade(s): {len(hits)} gefüllt, {len(rows) - len(hits)} verpasst"
                    f" | realisiert {usd(real)}")
            detail = [f"- {status_de.get(r[3], r[3])}: {names.get(r[1], r[1])} {r[2][:45]} "
                      f"({r[4]:.0f}/{r[5]:.0f}, erw. {usd(r[6] or 0)}, real {usd(r[7] or 0)})" for r in rows[-5:]]
            if len(hits) or os.environ.get("NOTIFY_MISSED", "1") == "1":
                alerts.append(("Polyarb: Trade" if len(rows) == 1 else "Polyarb: Trades",
                               head + "\n" + "\n".join(detail), "default",
                               "moneybag" if hits else "hourglass"))
        else:
            st["last_exec_ts"] = last_ts
    save_state(st)
    for a in alerts:
        push(*a)


def cmd_daily():
    report(24, "Polyarb Tagesbericht")


def cmd_report():
    h = float(sys.argv[2]) if len(sys.argv) > 2 else float(os.environ.get("REPORT_HOURS", "4"))
    report(h, f"Polyarb Update ({h:g}h)")


def report(hours: float, title: str):
    now = time.time()
    since = now - hours * 3600
    eq = q("SELECT ts, equity, locked, residual FROM equity ORDER BY ts DESC LIMIT 1")
    eq_then = q("SELECT equity FROM equity WHERE ts <= ? ORDER BY ts DESC LIMIT 1", (since,))
    if not eq:
        push("Polyarb: noch keine Daten", "Der Bot hat bisher keine Equity-Daten geschrieben.", "high", "warning")
        return
    e = eq[0][1]
    e0 = eq_then[0][0] if eq_then else START
    ex = q("""SELECT status, COUNT(*), SUM(realized_pnl), SUM(expected_profit) FROM executions
              WHERE ts > ? GROUP BY status""", (since,))
    n = {s: c for s, c, _, _ in ex}
    tries = sum(n.values())
    exp = sum(x[3] or 0 for x in ex)
    real = sum(x[2] or 0 for x in ex)
    opps = q("SELECT COUNT(*) FROM opportunities WHERE ts > ?", (since,))[0][0]
    scans = q("SELECT COUNT(*), MIN(best_buy_sum), MAX(best_sell_sum) FROM scans WHERE ts > ?", (since,))[0]
    lines = [
        f"Equity {usd(e)} ({(e / START - 1) * 100:+.2f} % gesamt)",
        f"Letzte {hours:g}h: {usd(e - e0)}",
        f"Chancen: {opps} | Trades: {tries} (voll {n.get('filled', 0)}, teilw. {n.get('partial', 0)}, verpasst {n.get('missed', 0)})",
    ]
    if exp:
        lines.append(f"Capture: {real / exp * 100:.0f} % (realisiert {usd(real)} von {usd(exp)})")
    if scans[0]:
        lines.append(f"Scans: {scans[0]:,} | engste YES+NO-Summe: {scans[1]:.4f}" if scans[1] else f"Scans: {scans[0]:,}")
    if eq[0][2] or eq[0][3]:
        lines.append(f"Gebunden {usd(eq[0][2])} | offene Reste {usd(eq[0][3])}")
    push(title, "\n".join(lines), tags="bar_chart")


def cmd_msg():
    push("Polyarb", " ".join(sys.argv[2:]) or "(leer)", tags="gear")


if __name__ == "__main__":
    {"start": cmd_start, "watch": cmd_watch, "daily": cmd_daily, "report": cmd_report, "msg": cmd_msg}[sys.argv[1] if len(sys.argv) > 1 else "daily"]()
