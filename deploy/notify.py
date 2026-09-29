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
URL = os.environ.get("DASH_URL", "")
USER = os.environ.get("DASH_USER", "")


def _target() -> dict:
    """Which book the alerts and reports follow: `notify: scenario:` in config.yaml, else the arbitrage bot."""
    base = Path(__file__).resolve().parents[1]
    t = dict(db=str(base / "data" / "polyarb.sqlite"), start=2500.0, service="polyarb", label="",
             stale_min=15.0, url=URL, state=str(base / "data" / "notify_state.json"))
    try:
        import yaml
        cfg = yaml.safe_load((base / "config.yaml").read_text(encoding="utf-8")) or {}
    except Exception:  # noqa – never let a config problem silence the watchdog
        cfg = {}
    data = Path(((cfg.get("storage") or {}).get("db_path")) or "data/polyarb.sqlite")
    data = data if data.is_absolute() else base / data
    t.update(db=str(data), start=float((cfg.get("portfolio") or {}).get("starting_capital_usd", 2500)),
             state=str(data.parent / "notify_state.json"))
    name = (cfg.get("notify") or {}).get("scenario") or ""
    sc = (cfg.get("scenarios") or {}).get(name)
    if sc and sc.get("enabled"):
        t.update(db=str(data.parent / f"scenario-{name}.sqlite"), start=float(sc.get("capital_usd", 2500)),
                 service=f"polyarb-scenario@{name}", label=sc.get("title", name),
                 stale_min=max(15.0, 2.5 * float(sc.get("interval_min", 10))),
                 url=f"{URL.rstrip('/')}/{name}/" if URL else "",
                 state=str(data.parent / f"notify_state-{name}.json"))
    return t


T = _target()
DB = os.environ.get("POLYARB_DB", T["db"])
START = float(os.environ.get("START_CAPITAL", T["start"]))
STATE = Path(os.environ.get("NOTIFY_STATE", T["state"]))
NAME = f"Polyarb {T['label']}" if T["label"] else "Polyarb"
STRATEGY_NAMES = {"binary_buy_all": "YES+NO kaufen", "binary_sell_all": "Split & verkaufen",
                  "negrisk_buy_all": "Korb", "negrisk_no_buy_all": "alle NO", "ladder_buy_all": "Leiter",
                  "underdog_buy": "Underdog", "endgame_buy": "Endspiel", "longshot_buy": "Longshot-NO",
                  "weather_buy": "Wetter"}


def push(title: str, msg: str, prio: str = "default", tags: str = "chart_with_upwards_trend"):
    if not TOPIC:
        print(f"[no topic] {title}: {msg}")
        return
    headers = {"Title": title, "Priority": prio, "Tags": tags}
    if T["url"]:
        headers["Click"] = T["url"]
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
    """The followed book's process and the arbitrage bot (which also feeds the dashboard)."""
    try:
        return all(subprocess.run(["systemctl", "is-active", "--quiet", s]).returncode == 0
                   for s in dict.fromkeys([T["service"], "polyarb"]))
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
    stale = (not last) or (now - last[0][0] > T["stale_min"] * 60)
    down = not service_active()

    problem = None
    if down:
        problem = "Ein Bot-Prozess läuft nicht (systemd startet ihn automatisch neu)."
    elif stale and (now - st.get("boot", now)) > (T["stale_min"] + 5) * 60:
        problem = (f"Seit über {T['stale_min']:.0f} min keine neuen Daten. Wahrscheinlich ist die Polymarket-API "
                   "nicht erreichbar (Rate-Limit, Ausfall oder Geo-Sperre des Servers).")
    if problem and not st.get("problem"):
        alerts.append((f"{NAME}: Problem", problem, "high", "warning"))
    if not problem and st.get("problem"):
        alerts.append((f"{NAME}: wieder OK", "Der Bot liefert wieder Daten.", "default", "white_check_mark"))
    st["problem"] = problem

    halted = last[0][2] if last else None
    if halted and halted != st.get("halted"):
        alerts.append((f"{NAME}: Handel pausiert", f"Kill-Switch ausgelöst: {halted}", "high", "rotating_light"))
    st["halted"] = halted
    st.setdefault("boot", now)

    # --- trade alerts: every executed paper trade since the last check
    if os.environ.get("NOTIFY_TRADES", "1") == "1":
        last_ts = st.get("last_exec_ts")
        if last_ts is None:
            last_ts = now  # first run: don't replay history
        rows = q("""SELECT ts, strategy, title, status, matched_qty, target_qty, expected_profit, realized_pnl, locked
                    FROM executions WHERE ts > ? ORDER BY ts""", (last_ts,))
        if rows:
            st["last_exec_ts"] = rows[-1][0]
            status_de = {"filled": "voll", "partial": "teilweise", "missed": "verpasst"}
            hits = [r for r in rows if r[3] in ("filled", "partial")]
            if T["label"]:  # scenario: a buy books nothing yet – say what was bought
                head = (f"{len(hits)} Kauf/Käufe für {usd(sum(r[8] or 0 for r in hits))}"
                        f"{f', {len(rows) - len(hits)} verpasst' if len(rows) > len(hits) else ''}")
                detail = [f"- {r[2][:50]}: {r[4]:.0f} Stk. für {usd(r[8] or 0)}"
                          f" (Ø {(r[8] or 0) / r[4]:.3f})" if r[4] else f"- verpasst: {r[2][:50]}" for r in rows[-5:]]
            else:
                real = sum(r[7] or 0 for r in rows)
                head = (f"{len(rows)} Trade(s): {len(hits)} gefüllt, {len(rows) - len(hits)} verpasst"
                        f" | realisiert {usd(real)}")
                detail = [f"- {status_de.get(r[3], r[3])}: {STRATEGY_NAMES.get(r[1], r[1])} {r[2][:45]} "
                          f"({r[4]:.0f}/{r[5]:.0f}, erw. {usd(r[6] or 0)}, real {usd(r[7] or 0)})" for r in rows[-5:]]
            if len(hits) or os.environ.get("NOTIFY_MISSED", "1") == "1":
                alerts.append((f"{NAME}: Trade" if len(rows) == 1 else f"{NAME}: Trades",
                               head + "\n" + "\n".join(detail), "default",
                               "moneybag" if hits else "hourglass"))
        else:
            st["last_exec_ts"] = last_ts

    # --- payouts: positions and baskets that resolved since the last check
    if os.environ.get("NOTIFY_PAYOUTS", "1") == "1":
        last_s = st.get("last_settle_ts")
        if last_s is None:
            last_s = now  # first run: don't replay history
        rows = q("""SELECT s.ts, s.payout, s.pnl, COALESCE(e.title, s.ref) FROM settlements s
                    LEFT JOIN (SELECT basket_id, MAX(title) title FROM executions GROUP BY basket_id) e
                    ON e.basket_id = s.ref WHERE s.ts > ? AND s.kind IN ('position', 'basket') ORDER BY s.ts""",
                 (last_s,))
        st["last_settle_ts"] = rows[-1][0] if rows else last_s
        if rows:
            won = [r for r in rows if (r[1] or 0) > 0]
            pnl = sum(r[2] or 0 for r in rows)
            head = f"{len(won)} gewonnen, {len(rows) - len(won)} verloren | {usd(pnl)}"
            detail = [f"- {'✓' if (r[1] or 0) > 0 else '✗'} {r[3][:50]}: {usd(r[2] or 0)}"
                      for r in sorted(rows, key=lambda r: -abs(r[2] or 0))[:5]]
            alerts.append((f"{NAME}: Auszahlung" if len(rows) == 1 else f"{NAME}: Auszahlungen",
                           head + "\n" + "\n".join(detail), "default", "trophy" if pnl > 0 else "chart_with_downwards_trend"))
    save_state(st)
    for a in alerts:
        push(*a)


def cmd_daily():
    report(24, f"{NAME} Tagesbericht")


def cmd_report():
    h = float(sys.argv[2]) if len(sys.argv) > 2 else float(os.environ.get("REPORT_HOURS", "4"))
    report(h, f"{NAME} Update ({h:g}h)")


def report(hours: float, title: str):
    now = time.time()
    since = now - hours * 3600
    eq = q("SELECT ts, equity, locked, residual FROM equity ORDER BY ts DESC LIMIT 1")
    eq_then = q("SELECT equity FROM equity WHERE ts <= ? ORDER BY ts DESC LIMIT 1", (since,))
    if not eq:
        push(f"{NAME}: noch keine Daten", "Der Bot hat bisher keine Equity-Daten geschrieben.", "high", "warning")
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
    res = q("""SELECT COUNT(*), SUM(payout > 0), SUM(pnl) FROM settlements
               WHERE ts > ? AND kind IN ('position', 'basket')""", (since,))[0]
    if res[0]:
        lines.append(f"Aufgelöst: {res[0]} ({res[1] or 0} gewonnen, {res[0] - (res[1] or 0)} verloren) | {usd(res[2] or 0)}")
    if scans[0] and not T["label"]:
        lines.append(f"Scans: {scans[0]:,} | engste YES+NO-Summe: {scans[1]:.4f}" if scans[1] else f"Scans: {scans[0]:,}")
    if eq[0][2] or eq[0][3]:
        lines.append(f"Gebunden {usd(eq[0][2])} | offene Reste {usd(eq[0][3])}")
    push(title, "\n".join(lines), tags="bar_chart")


def cmd_msg():
    push("Polyarb", " ".join(sys.argv[2:]) or "(leer)", tags="gear")


if __name__ == "__main__":
    {"start": cmd_start, "watch": cmd_watch, "daily": cmd_daily, "report": cmd_report, "msg": cmd_msg}[sys.argv[1] if len(sys.argv) > 1 else "daily"]()
