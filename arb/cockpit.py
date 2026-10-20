"""Cockpit page: all market-making scenarios on one page – where the money comes from (rewards vs. trading),
return curves as small multiples, ratios, and the real-money rehearsal next to its estimate.

Everything is drawn server-side as inline SVG (no script, no library): the static dashboard is rebuilt every
few minutes on a 1-CPU server. Colors are the dashboard's tokens (--s1 blue = rewards, --s2 orange = trading),
so light/dark mode follow the rest of the site.
"""
from __future__ import annotations

import html
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

SINCE = datetime(2026, 10, 11).timestamp()  # before: inflated by the max_markets bug (10.10.)


def _de(v: Optional[float], d: int = 2, sign: bool = False) -> str:
    if v is None:
        return "–"
    s = f"{v:+,.{d}f}" if sign else f"{v:,.{d}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".").replace("-", "−")


def scenario_numbers(db_path: str, start: float, scan: dict) -> Optional[dict]:
    """Since 11.10. (or the scenario's start): estimated rewards, trading result (realized without rewards plus
    the change in value of the held inventory), net, per day, plus an hourly return curve and ratios."""
    if not Path(db_path).exists():
        return None
    db = sqlite3.connect(db_path)
    try:
        first = db.execute("SELECT ts, equity, realized_cum FROM equity WHERE ts >= ? ORDER BY ts LIMIT 1",
                           (SINCE,)).fetchone()
        last = db.execute("SELECT ts, equity, realized_cum FROM equity ORDER BY ts DESC LIMIT 1").fetchone()
        if not first or not last or last[0] - first[0] < 3600:
            return None
        rew = db.execute("SELECT COALESCE(SUM(pnl), 0) FROM settlements WHERE kind='reward' AND ts >= ?",
                         (first[0],)).fetchone()[0]
        curve = db.execute("""SELECT MAX(ts), equity FROM equity WHERE ts >= ? GROUP BY CAST(ts / 3600 AS INT)
                              ORDER BY 1""", (first[0],)).fetchall()
        met = db.execute("""SELECT AVG(rewards_per_day), AVG(share_pct) FROM mm_metrics WHERE ts >= ?""",
                         (last[0] - 3 * 86400,)).fetchone()
        try:
            mk = db.execute("SELECT COUNT(m60), SUM(m60), COUNT(m5), SUM(m5) FROM mm_markout").fetchone()
            filled = db.execute("SELECT COALESCE(SUM(price * qty), 0) FROM mm_markout WHERE ts >= ?",
                                (first[0],)).fetchone()[0]
        except sqlite3.OperationalError:
            mk, filled = (0, None, 0, None), 0.0
        fills = db.execute("SELECT COUNT(*) FROM executions WHERE ts >= ? AND matched_qty > 0", (first[0],)).fetchone()[0]
        halves = _halves(db, first[0], last[0])
    except sqlite3.OperationalError:
        return None
    finally:
        db.close()
    days = max((last[0] - first[0]) / 86400, 1 / 24)
    base = first[1]
    net = last[1] - base
    trading = net - rew
    inv = float(scan.get("Bestand $") or 0)
    return dict(days=days, rewards=rew, trading=trading, net=net, per_day=net / days, rew_day=rew / days,
                ret=net / start if start else 0.0, curve=[(t, (e - base) / start if start else 0) for t, e in curve],
                est_rew_day=met[0] if met else None, share=met[1] if met else None, fills=fills,
                trade_per_rew=(trading / rew) if rew > 0 else None, inv=inv, inv_pct=inv / start if start else None,
                m60=mk[1] if mk and mk[0] else None, m60_n=mk[0] if mk else 0, start_ts=first[0], filled=filled,
                halves=halves, start=start)


def _halves(db: sqlite3.Connection, t0: float, t1: float) -> List[dict]:
    """The window cut in two by time: rewards, filled $ and trading (equity change minus rewards) per half. A rule
    that only works in one half is luck or a phase, not an edge – the same test as walk-forward, on live data."""
    def eq(t):
        r = db.execute("SELECT equity FROM equity WHERE ts <= ? ORDER BY ts DESC LIMIT 1", (t,)).fetchone()
        return r[0] if r else None
    try:  # fills are only recorded since 15.10. (mm_markout): rewards before that have no fills to divide by
        first_fill = db.execute("SELECT MIN(ts) FROM mm_markout").fetchone()[0]
    except sqlite3.OperationalError:
        first_fill = None
    if first_fill and first_fill > t0:
        t0 = min(first_fill - 1, t1)  # the first fill itself belongs to the first half
    mid = (t0 + t1) / 2
    out = []
    for a, b in ((t0, mid), (mid, t1)):
        rew = db.execute("SELECT COALESCE(SUM(pnl), 0) FROM settlements WHERE kind='reward' AND ts > ? AND ts <= ?",
                         (a, b)).fetchone()[0]
        try:
            filled = db.execute("SELECT COALESCE(SUM(price * qty), 0) FROM mm_markout WHERE ts > ? AND ts <= ?",
                                (a, b)).fetchone()[0]
        except sqlite3.OperationalError:
            filled = 0.0
        ea, eb = eq(a), eq(b)
        out.append(dict(rewards=rew, filled=filled, trading=None if ea is None or eb is None else eb - ea - rew,
                        days=(b - a) / 86400))
    return out


def live_numbers(data_dir: Path) -> Optional[dict]:
    try:
        st = json.loads((data_dir / "live-status-mm_probe.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None
    out = dict(live=st.get("live"), equity=st.get("equity"), cash=st.get("cash"), positions=st.get("positions"),
               fills=st.get("fills"), scoring=st.get("scoring"), yesterday=st.get("rewards_yesterday"),
               today=st.get("earned_today"), pnl_total=st.get("pnl_total"), deposits=st.get("deposits"))
    db_path = data_dir / "scenario-mm_probe.sqlite"
    if db_path.exists():
        db = sqlite3.connect(str(db_path))
        try:
            ts = db.execute("SELECT MAX(ts) FROM live_reward_share").fetchone()[0]
            if ts:
                r = db.execute("""SELECT SUM(pm_earned), SUM(rate * pm_pct / 100), SUM(rate * est_pct / 100)
                                  FROM live_reward_share WHERE ts = ?""", (ts,)).fetchone()
                out.update(pm_day=r[1], est_day=r[2])
                if out.get("today") is None:  # older status files: only the markets quoted right now
                    out["today"] = r[0]
            out["paid"] = db.execute("SELECT day, usd FROM live_rewards ORDER BY day DESC LIMIT 7").fetchall()
            out["paid_total"] = db.execute("SELECT COALESCE(SUM(usd), 0) FROM live_rewards").fetchone()[0]
            out["filled"] = db.execute("SELECT COALESCE(SUM(price * qty), 0) FROM mm_markout").fetchone()[0]
        except sqlite3.OperationalError:
            pass
        finally:
            db.close()
    return out


# ---------------------------------------------------------------------------------------------------- drawing
def _tiles(items: List[tuple]) -> str:
    return '<div class="kpis">' + "".join(
        f'<div class="kpi"><div class="l">{html.escape(l)}</div><div class="v {cls}">{v}</div>'
        f'<div class="d">{html.escape(d)}</div></div>' for l, v, d, cls in items) + "</div>"


def _bars(rows: List[dict]) -> str:
    """Diverging horizontal bars per scenario: rewards (blue, always >= 0) and trading (orange, + or −) from a
    shared zero line; the net as text at the end. Plain HTML (percent widths), so text stays readable on a phone."""
    if not rows:
        return "<p class='note'>Noch zu wenig Daten.</p>"
    lo = min(0.0, min(r["trading"] for r in rows))
    hi = max(0.0, max(r["rewards"] + max(r["trading"], 0) for r in rows))
    span = (hi - lo) or 1
    P = lambda v: abs(v) / span * 100  # noqa: E731
    z = P(lo)
    out = ['<div style="display:grid;grid-template-columns:minmax(70px,26%) 1fr auto;gap:8px 10px;align-items:center">']
    for r in rows:
        name = html.escape(r["label"])
        segs = (f'<div title="Rewards (geschätzt) {_de(r["rewards"])} $" style="position:absolute;left:{z:.2f}%;'
                f'width:{P(r["rewards"]):.2f}%;top:0;bottom:0;background:var(--s1);border-radius:3px"></div>')
        if r["trading"] >= 0:
            segs += (f'<div title="Handel {_de(r["trading"], sign=True)} $" style="position:absolute;left:calc({z + P(r["rewards"]):.2f}% + 2px);'
                     f'width:{P(r["trading"]):.2f}%;top:0;bottom:0;background:var(--s2);border-radius:3px"></div>')
        else:
            segs += (f'<div title="Handel {_de(r["trading"], sign=True)} $" style="position:absolute;left:{z - P(r["trading"]):.2f}%;'
                     f'width:calc({P(r["trading"]):.2f}% - 2px);top:0;bottom:0;background:var(--s2);border-radius:3px"></div>')
        cls = "pos" if r["net"] > 0 else "neg" if r["net"] < 0 else ""
        out.append(f'<div style="text-align:right;font-size:12.5px;color:var(--text2)">{name}</div>'
                   f'<div style="position:relative;height:16px;border-left:1px solid transparent">{segs}'
                   f'<div style="position:absolute;left:{z:.2f}%;top:-4px;bottom:-4px;border-left:1px solid var(--muted)"></div></div>'
                   f'<div class="{cls}" style="font-weight:600;font-variant-numeric:tabular-nums;white-space:nowrap">'
                   f'{_de(r["net"], 0, sign=True)} $</div>')
    out.append("</div>")
    return "".join(out)


def _spark(curve: List[tuple], ret: float) -> str:
    if len(curve) < 2:
        return ""
    W, H, p = 240, 56, 4
    t0, t1 = curve[0][0], curve[-1][0]
    vs = [v for _, v in curve] + [0.0]
    lo, hi = min(vs), max(vs)
    span = (hi - lo) or 0.01
    X = lambda t: p + (t - t0) / ((t1 - t0) or 1) * (W - 2 * p)  # noqa: E731
    Y = lambda v: p + (1 - (v - lo) / span) * (H - 2 * p)  # noqa: E731
    d = "".join(("L" if i else "M") + f"{X(t):.1f},{Y(v):.1f}" for i, (t, v) in enumerate(curve))
    col = "var(--good)" if ret >= 0 else "var(--bad)"
    return (f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Rendite-Verlauf">'
            f'<line x1="{p}" x2="{W - p}" y1="{Y(0):.1f}" y2="{Y(0):.1f}" stroke="var(--muted)" stroke-dasharray="3 3"/>'
            f'<path d="{d}" fill="none" stroke="var(--s1)" stroke-width="2" stroke-linejoin="round"/>'
            f'<circle cx="{X(t1):.1f}" cy="{Y(curve[-1][1]):.1f}" r="4" fill="{col}" stroke="var(--surface)" stroke-width="2"/></svg>')


def short(label: str) -> str:
    """"Market Making (breit)" -> "breit": the page is all market making anyway."""
    return label[len("Market Making ("):-1] if label.startswith("Market Making (") and label.endswith(")") else label


# Generalprobe 14.–16.10. (Not-Aus): −23,73 $ Kontowert − Einzahlungen, ~5,50 $ echte Rewards, ~121 $ gefüllt
PROBE_RESULT = dict(filled=121.0, rew=5.5, trade=-29.2, trade_rate=-29.2 / 121.0)


def calibration_card(rows: List[dict], live: Optional[dict]) -> str:
    """Everything per 1 $ that was filled – comparable between the simulation and the real probe. "kalibriert":
    the simulation's rewards, but each filled $ costing as much as it really did in the probe (16.10.)."""
    real = None
    if live and (live.get("filled") or 0) > 20 and live.get("pnl_total") is not None:
        real_rew = float(live.get("paid_total") or 0) + float(live.get("today") or 0)
        real = dict(filled=live["filled"], rew=real_rew, trade=live["pnl_total"] - real_rew)
        real["trade_rate"] = real["trade"] / real["filled"]
    elif live is not None:  # probe switched off: the account is no longer read – use its final result (16.10.)
        real = dict(PROBE_RESULT)
    sim = [r for r in rows if (r["n"].get("filled") or 0) > 20 and r["key"] != "mm_probe"]
    if not sim and not real:
        return ""
    th = ("<tr><th>Szenario</th><th class='num'>gefüllt $</th><th class='num'>Rewards je 1 $ gefüllt</th>"
          "<th class='num'>Handel je 1 $ gefüllt</th><th class='num'>Kurs 60 Min. je 1 $</th>"
          "<th class='num'>netto $/Tag kalibriert</th></tr>")
    trs = ""
    if real:
        trs += (f"<tr><td><b>Echt (Generalprobe)</b></td><td class='num'>{_de(real['filled'], 0)}</td>"
                f"<td class='num'>{_de(real['rew'] / real['filled'] * 100, 1)} %</td>"
                f"<td class='num neg'>{_de(real['trade_rate'] * 100, 1, sign=True)} %</td><td class='num'>–</td>"
                f"<td class='num'>–</td></tr>")
    for r in sorted(sim, key=lambda r: -(r["n"]["rewards"] / r["n"]["filled"])):
        n = r["n"]
        cal = (n["rewards"] + n["filled"] * real["trade_rate"]) / n["days"] if real else None
        trs += (f"<tr><td><a href='../{r['key']}/'>{html.escape(r['label'])}</a></td><td class='num'>{_de(n['filled'], 0)}</td>"
                f"<td class='num'>{_de(n['rewards'] / n['filled'] * 100, 1)} %</td>"
                f"<td class='num {'neg' if n['trading'] < 0 else 'pos'}'>{_de(n['trading'] / n['filled'] * 100, 1, sign=True)} %</td>"
                f"<td class='num'>{'–' if n['m60'] is None else _de(n['m60'] / n['filled'] * 100, 1, sign=True) + ' %'}</td>"
                f"<td class='num {'' if cal is None else 'pos' if cal > 0 else 'neg'}'>"
                f"{'–' if cal is None else _de(cal, 1, sign=True)}</td></tr>")
    return ('<div class="card"><h2>Vergleich je 1 $ gefüllt – Simulation an echt kalibriert</h2><p class="note">'
            'Alles je Dollar, der ausgeführt wurde – so sind kleine echte und große simulierte Gebote vergleichbar. '
            '„Handel“ = realisiert + Wertänderung des Bestands. „netto kalibriert“: Rewards der Simulation, aber jeder '
            'gefüllte Dollar kostet so viel wie echt in der Generalprobe. Positiv = trägt auch unter echten Bedingungen.</p>'
            f'<div class="tblwrap"><table>{th}{trs}</table></div></div>')


REAL_REWARD_FACTOR = 0.6  # echte Rewards ≈ 60 % der Schätzung (Generalprobe 14.–16.10.)
STAGE1_THRESHOLD = 0.25   # Plan 18.10.: Stufe 2 nur, wenn echte Rewards je 1 $ gefüllt > ~25 %
STAGE1_CAPITAL = 150.0    # Stufe 1: 100–150 $ echt
STAGE1_MIN_FILLED = 1000.0  # 20.10.: unter 1.000 $ gefüllt (≈ 20–40 Fills) ist ein „ja“ Zufall – erst „zu wenig Daten“


def _real_trade_rate(live: Optional[dict]) -> float:
    if live and (live.get("filled") or 0) > 20 and live.get("pnl_total") is not None:
        real_rew = float(live.get("paid_total") or 0) + float(live.get("today") or 0)
        return (live["pnl_total"] - real_rew) / live["filled"]
    return PROBE_RESULT["trade_rate"]


def decision_rows(rows: List[dict], live: Optional[dict] = None) -> List[dict]:
    """Per scenario, everything per 1 $ filled: rewards at 60 % of the estimate, in each half of the window and in
    total; net with the real trading cost of the probe; what that would be per day at 150 $ (Stufe 1); a verdict.
    "ja" only if both halves clear the threshold and net is positive – one good half is not enough."""
    trade = _real_trade_rate(live)
    out = []
    for r in rows:
        n = r.get("n") or {}
        filled = n.get("filled") or 0
        if filled <= 20 or r.get("key") == "mm_probe":
            continue
        rew = REAL_REWARD_FACTOR * n["rewards"] / filled
        hs = [REAL_REWARD_FACTOR * h["rewards"] / h["filled"] if h["filled"] > 5 else None for h in n.get("halves") or []]
        net = rew + trade
        cap = n.get("start") or 0
        per_day = filled / n["days"] / cap * STAGE1_CAPITAL * net if cap else None
        ok_h = len(hs) == 2 and all(h is not None and h > STAGE1_THRESHOLD for h in hs)
        verdict = "ja" if ok_h and net > 0 else "knapp" if rew > STAGE1_THRESHOLD or (net > 0 and any(
            h is not None and h > STAGE1_THRESHOLD for h in hs)) else "nein"
        if verdict != "nein" and filled < STAGE1_MIN_FILLED:
            verdict = "zu wenig Daten"
        out.append(dict(key=r["key"], label=r["label"], filled=filled, rew=rew, h1=hs[0] if hs else None,
                        h2=hs[1] if len(hs) > 1 else None, sim_trade=n["trading"] / filled, net=net,
                        per_day150=per_day, verdict=verdict, days=n["days"]))
    order = {"ja": 0, "knapp": 1, "zu wenig Daten": 2, "nein": 3}
    out.sort(key=lambda d: (order[d["verdict"]], -d["net"]))
    return out


def decision_card(rows: List[dict], live: Optional[dict]) -> str:
    dec = decision_rows(rows, live)
    if not dec:
        return ""
    trade = _real_trade_rate(live)
    pct = lambda v: "–" if v is None else _de(v * 100, 1) + " %"  # noqa: E731
    th = ("<tr><th>Szenario</th><th class='num'>gefüllt $</th><th class='num'>Rewards×0,6 je 1 $ – 1. Hälfte</th>"
          "<th class='num'>2. Hälfte</th><th class='num'>gesamt</th><th class='num'>Handel je 1 $ (Sim)</th>"
          f"<th class='num'>netto je 1 $ (Handel echt {_de(trade * 100, 0)} %)</th>"
          f"<th class='num'>bei {_de(STAGE1_CAPITAL, 0)} $: $/Tag</th><th>Stufe 1?</th></tr>")
    trs = "".join(
        f"<tr><td><a href='../{d['key']}/'>{html.escape(d['label'])}</a></td><td class='num'>{_de(d['filled'], 0)}</td>"
        f"<td class='num'>{pct(d['h1'])}</td><td class='num'>{pct(d['h2'])}</td><td class='num'><b>{pct(d['rew'])}</b></td>"
        f"<td class='num'>{_de(d['sim_trade'] * 100, 1, sign=True)} %</td>"
        f"<td class='num {'pos' if d['net'] > 0 else 'neg'}'>{_de(d['net'] * 100, 1, sign=True)} %</td>"
        f"<td class='num'>{'–' if d['per_day150'] is None else _de(d['per_day150'], 2, sign=True)}</td>"
        f"<td class='{'pos' if d['verdict'] == 'ja' else 'neg' if d['verdict'] == 'nein' else ''}'><b>{d['verdict']}</b></td></tr>"
        for d in dec)
    best = dec[0]
    if best["verdict"] == "ja":
        head = (f"Kandidat für Stufe 1: <b>{html.escape(best['label'])}</b> – Rewards×0,6 je 1 $ gefüllt in beiden "
                f"Hälften über {_de(STAGE1_THRESHOLD * 100, 0)} %, netto {_de(best['net'] * 100, 1, sign=True)} % je 1 $ gefüllt.")
    elif best["verdict"] == "zu wenig Daten":
        head = (f"Noch kein Urteil: <b>{html.escape(best['label'])}</b> sieht gut aus, hat aber erst "
                f"{_de(best['filled'], 0)} $ gefüllt (nötig ≥ {_de(STAGE1_MIN_FILLED, 0)} $) – ein paar Fills entscheiden "
                "hier noch alles.")
    else:
        head = (f"Kein Szenario besteht beide Hälften – Stufe 1 nur als Messung (Rewards echt je 1 $ gefüllt), nicht "
                f"als Gewinnerwartung. Am nächsten: <b>{html.escape(best['label'])}</b> ({best['verdict']}).")
    return ('<div class="card"><h2>Entscheidungshilfe Stufe 1 (Echt-Test 100–150 $)</h2>'
            f'<p>{head}</p><p class="note">Regel aus dem Plan vom 18.10.: echte Rewards je 1 $ gefüllt müssen die echten '
            f'Handelskosten (Generalprobe {_de(trade * 100, 0)} % je 1 $) schlagen, Schwelle ~{_de(STAGE1_THRESHOLD * 100, 0)} %. '
            f'Rewards hier = Schätzung × {_de(REAL_REWARD_FACTOR, 1)} (echt war ~60 % der Schätzung). Zeitraum je Szenario '
            'seit 11.10. bzw. Start, in zwei gleich lange Hälften geteilt: „ja“ nur, wenn beide Hälften über der Schwelle '
            'liegen (Stabilität statt Glückstreffer). „bei 150 $“ = gefüllte $ je $ Kapital und Tag aus der Simulation × '
            '150 $ × netto je 1 $ – grob, Rewards und Füllrate skalieren nicht linear.</p>'
            f'<div class="tblwrap"><table>{th}{trs}</table></div></div>')


def replay_numbers(data_dir: Path) -> Optional[dict]:
    try:
        return json.loads((data_dir / "replaylab.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None


def replay_card(res: Optional[dict]) -> str:
    """Nightly replay (arb/replaylab.py): the rule grid on the order-book recording and the walk-forward check."""
    if not res or not res.get("rules"):
        return ""
    top = sorted(res["rules"], key=lambda d: -d["net"])[:6]
    base = next((r for r in res["rules"] if r["rule"] == res.get("baseline")), None)
    if base and base not in top:
        top.append(base)
    th = ("<tr><th>Regel</th><th class='num'>netto $</th><th class='num'>Rewards×0,6 $</th><th class='num'>Bid 60 Min. $</th>"
          "<th class='num'>gefüllt $</th><th class='num'>netto je 1 $ gefüllt</th><th class='num'>Blöcke positiv</th></tr>")
    trs = "".join(
        f"<tr><td>{html.escape(r['rule'])}{' (Vergleich)' if base and r is base else ''}</td>"
        f"<td class='num {'pos' if r['net'] > 0 else 'neg'}'>{_de(r['net'], 2, sign=True)}</td>"
        f"<td class='num'>{_de(r['rewards60'])}</td><td class='num'>{_de(r['m60bid'], 2, sign=True)}</td>"
        f"<td class='num'>{_de(r['filled'], 0)}</td>"
        f"<td class='num'>{'–' if not r['filled'] else _de(r['net'] / r['filled'] * 100, 1, sign=True) + ' %'}</td>"
        f"<td class='num'>{r['blocks_pos']}/{r['blocks']}</td></tr>" for r in top)
    wf = res.get("walk_forward") or []
    if wf:
        n = len(wf)
        verdict = (f"Walk-forward über {n} {'Block' if n == 1 else 'Blöcke'} à {_de(res['block_h'], 0)} h: gewählte Regeln "
                   f"<b class='{'pos' if res['wf_test'] > 0 else 'neg'}'>{_de(res['wf_test'], 2, sign=True)} $</b> außerhalb "
                   f"der Stichprobe (davor im Schnitt {_de(res['wf_train_best'] / n, 2, sign=True)} $ je Block), "
                   f"Vergleich {html.escape(res['baseline'])} {_de(res['wf_base'], 2, sign=True)} $.")
        if n < 6:
            verdict += " Noch zu wenige Blöcke für ein Urteil – ab ~6 (3 Tage) aussagekräftiger."
    else:
        verdict = "Noch kein Walk-forward – dafür braucht es mindestens zwei Blöcke Mitschnitt."
    when = time.strftime("%d.%m. %H:%M", time.localtime(res.get("ts") or 0))
    return ('<div class="card"><h2>Replay-Labor – Regeln auf dem Orderbuch-Mitschnitt</h2>'
            f'<p>{verdict}</p><p class="note">{res["markets"]} Reward-Märkte, Gebot {_de(res["size_usd"], 0)} $ je Seite. '
            'netto = Rewards × 0,6 (Obergrenze: der Mitschnitt hat nur 3 Level je Seite, die Konkurrenz ist größer) + '
            'Verkauf zum Bid 60 Min. nach dem Fill. Fill = Ask fällt auf unser Gebot oder ein neuer Handel darunter, '
            f'ohne Warteschlange. Stand {when}; alle Regeln in replay-regeln.csv (Export).</p>'
            f'<div class="tblwrap"><table>{th}{trs}</table></div></div>')


def page_body(scen: List[dict], live: Optional[dict], replay: Optional[dict] = None) -> str:
    parts = []
    scen = [dict(r, label=short(r["label"])) for r in scen]
    if live:
        paid = live.get("paid") or []
        ratio = (live["pm_day"] / live["est_day"]) if live.get("pm_day") and live.get("est_day") else None
        parts.append('<h2 style="margin-top:6px">Echtgeld – Generalprobe</h2><p class="note">Echte Orders mit deinem '
                     'Polymarket-Konto. Rewards laut Polymarket; „Anteil echt/geschätzt“ = Polymarkets eigener Anteil am '
                     'Topf gegen unsere Schätzung (über 100 % = wir unterschätzen).</p>')
        parts.append(_tiles([
            ("Kontowert", f'{_de(live.get("equity"))} $', f'Cash {_de(live.get("cash"))} $ + Positionen {_de(live.get("positions"))} $', ""),
            ("Handel seit Start (echt)", f'{_de(live.get("pnl_total"), sign=True)} $',
             f'Kontowert − Einzahlungen {_de(live.get("deposits"))} $, ohne noch nicht gezahlte Rewards',
             "pos" if (live.get("pnl_total") or 0) > 0 else "neg" if (live.get("pnl_total") or 0) < 0 else ""),
            ("Rewards heute (echt)", f'{_de(live.get("today"), 3)} $', "alle Märkte des Tages, laut Polymarket; Auszahlung ab 1 $/Tag", ""),
            ("Rewards gestern (echt)", f'{_de(live.get("yesterday"), 3)} $', " · ".join(f"{d[5:]}: {_de(u, 2)}" for d, u in paid[:4]), ""),
            ("Anteil echt / geschätzt", "–" if ratio is None else f"{ratio * 100:.0f} %",
             f'≈ {_de(live.get("pm_day"))} vs. {_de(live.get("est_day"))} $/Tag', "pos" if ratio and ratio >= 1 else ""),
            ("Orders zählen für Rewards", str(live.get("scoring") or "–"), f'{live.get("fills") or 0} echte Ausführungen', ""),
        ]))
    rows = sorted([r for r in scen if r.get("n")], key=lambda r: -r["n"]["net"])
    if rows:
        tot_r = sum(r["n"]["rewards"] for r in rows)
        tot_t = sum(r["n"]["trading"] for r in rows)
        best = rows[0]
        parts.append('<h2 style="margin-top:18px">Simulation – alle Market-Making-Szenarien</h2><p class="note">'
                     'Seit 11.10. (davor durch den Auswahl-Fehler aufgebläht) bzw. seit Start. Rewards sind geschätzt – wie '
                     'nah sie an der Wirklichkeit liegen, zeigt oben „Anteil echt / geschätzt“ (schwankt noch stark). Handel = Spread, Auflösungen, Verkäufe und '
                     'die Wertänderung des gehaltenen Bestands.</p>')
        parts.append(_tiles([
            ("Rewards (geschätzt), alle", f"{_de(tot_r, 0)} $", f"{len(rows)} Szenarien", "pos"),
            ("Handel, alle", f"{_de(tot_t, 0, sign=True)} $", "Verluste an Informierte + Bestand", "pos" if tot_t >= 0 else "neg"),
            ("Handel je 1 $ Rewards", f"{_de(tot_t / tot_r, 2, sign=True)} $" if tot_r else "–",
             "−1,00 = Handel frisst alle Rewards", "neg" if tot_r and tot_t < 0 else "pos"),
            ("Bestes Szenario", html.escape(best["label"]), f'{_de(best["n"]["per_day"], 1, sign=True)} $/Tag netto', ""),
        ]))
        parts.append('<div class="card"><h2>Woher kommt das Ergebnis?</h2><p class="note">'
                     '<span style="color:var(--s1)">■</span> Rewards (geschätzt) · <span style="color:var(--s2)">■</span> '
                     'Handel (+/−) · rechts: netto. Sortiert nach netto.</p>'
                     + _bars([dict(label=r["label"], **r["n"]) for r in rows]) + "</div>")
        cards = "".join(
            f'<div class="card" style="margin:0"><div style="display:flex;justify-content:space-between;gap:8px">'
            f'<a href="../{r["key"]}/" style="font-weight:600">{html.escape(r["label"])}</a>'
            f'<span class="{"pos" if r["n"]["ret"] >= 0 else "neg"}" style="font-weight:600;white-space:nowrap">'
            f'{_de(r["n"]["ret"] * 100, 1, sign=True)} %</span></div>{_spark(r["n"]["curve"], r["n"]["ret"])}'
            f'<div class="note" style="margin:4px 0 0">{_de(r["n"]["per_day"], 1, sign=True)} $/Tag · '
            f'{_de(r["n"]["days"], 1)} Tage</div></div>' for r in rows)
        parts.append('<h2 style="margin-top:18px">Rendite-Verlauf</h2><p class="note">Je Szenario seit 11.10. bzw. '
                     'Start, in % des Startkapitals; gestrichelt = 0.</p>'
                     f'<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px;'
                     f'margin-bottom:16px">{cards}</div>')
        th = ("<tr><th>Szenario</th><th class='num'>Rendite</th><th class='num'>netto $/Tag</th>"
              "<th class='num'>Rewards $/Tag</th><th class='num'>Anteil am Topf</th><th class='num'>Handel je 1 $ Rewards</th>"
              "<th class='num'>Ausführungen</th><th class='num'>Bestand</th><th class='num'>Kurs 60 Min. nach Fill</th></tr>")
        trs = "".join(
            f"<tr><td><a href='../{r['key']}/'>{html.escape(r['label'])}</a></td>"
            f"<td class='num {'pos' if n['ret'] >= 0 else 'neg'}'>{_de(n['ret'] * 100, 1, sign=True)} %</td>"
            f"<td class='num'>{_de(n['per_day'], 1, sign=True)}</td><td class='num'>{_de(n['rew_day'], 1)}</td>"
            f"<td class='num'>{'–' if n['share'] is None else _de(n['share'], 1) + ' %'}</td>"
            f"<td class='num {'neg' if (n['trade_per_rew'] or 0) < -0.5 else ''}'>{'–' if n['trade_per_rew'] is None else _de(n['trade_per_rew'], 2, sign=True)}</td>"
            f"<td class='num'>{n['fills']}</td>"
            f"<td class='num'>{_de(n['inv'], 0)} $ ({'–' if n['inv_pct'] is None else _de(n['inv_pct'] * 100, 0)} %)</td>"
            f"<td class='num {'neg' if (n['m60'] or 0) < 0 else 'pos' if (n['m60'] or 0) > 0 else ''}'>"
            f"{'–' if n['m60'] is None else _de(n['m60'], 2, sign=True) + ' $ (' + str(n['m60_n']) + ')'}</td></tr>"
            for r in rows for n in [r["n"]])
        parts.append('<div class="card"><h2>Kennzahlen</h2><p class="note">Anteil am Topf = Ø der letzten 3 Tage. '
                     '„Handel je 1 $ Rewards“: wie viel des Reward-Ertrags der Handel kostet (−0,50 = die Hälfte). '
                     '„Kurs 60 Min. nach Fill“: negativ = von Besserinformierten getroffen (Messung ab 15.10.).</p>'
                     f'<div class="tblwrap"><table>{th}{trs}</table></div></div>')
    parts.append(calibration_card(rows, live))
    parts.append(replay_card(replay))
    if rows:
        parts.insert(2 if live else 0, decision_card(rows, live))
    if not parts:
        parts.append("<p class='note'>Noch keine Daten.</p>")
    parts.append(f'<p class="note">Stand {time.strftime("%d.%m. %H:%M")}</p>')
    return "".join(parts)
