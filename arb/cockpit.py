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
                m60=mk[1] if mk and mk[0] else None, m60_n=mk[0] if mk else 0, start_ts=first[0], filled=filled)


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


def page_body(scen: List[dict], live: Optional[dict]) -> str:
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
    if not parts:
        parts.append("<p class='note'>Noch keine Daten.</p>")
    parts.append(f'<p class="note">Stand {time.strftime("%d.%m. %H:%M")}</p>')
    return "".join(parts)
