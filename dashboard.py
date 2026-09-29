"""Build a self-contained HTML dashboard (no external scripts) from the SQLite log."""
from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path


def _q(db, sql, args=()):
    return db.execute(sql, args).fetchall()


def collect(db_path: str, start_capital: float) -> dict:
    from arb.storage import Store
    Store(db_path).db.close()  # ensure schema exists
    db = sqlite3.connect(db_path)
    eq = _q(db, "SELECT ts, equity, cash, locked, residual, realized_cum, halted FROM equity ORDER BY ts")
    step = max(1, len(eq) // 600)
    curve = [dict(t=r[0], e=round(r[1], 2), l=round(r[3], 2)) for r in eq[::step]]
    if eq and (not curve or curve[-1]["t"] != eq[-1][0]):
        curve.append(dict(t=eq[-1][0], e=round(eq[-1][1], 2), l=round(eq[-1][3], 2)))

    peak, mdd = start_capital, 0.0
    for r in eq:
        peak = max(peak, r[1])
        mdd = max(mdd, (peak - r[1]) / peak if peak else 0)

    by_strat = {}
    for s, st, n, real, exp in _q(db, """SELECT strategy, status, COUNT(*), SUM(realized_pnl), SUM(expected_profit)
                                        FROM executions GROUP BY 1,2"""):
        d = by_strat.setdefault(s, dict(filled=0, partial=0, missed=0, realized=0.0, expected=0.0))
        d[st] = n
        d["realized"] += real or 0
        d["expected"] += exp or 0
    settle = {k: dict(n=n, pnl=p) for k, n, p in _q(db, "SELECT kind, COUNT(*), SUM(pnl) FROM settlements GROUP BY 1")}
    # payouts at resolution (baskets, scenario positions) count for the strategy that opened them
    for s, pnl in _q(db, """SELECT e.strategy, SUM(s.pnl) FROM settlements s
                            JOIN (SELECT DISTINCT basket_id, strategy FROM executions) e ON s.ref = e.basket_id
                            WHERE s.kind IN ('basket', 'position') GROUP BY 1"""):
        if s in by_strat:
            by_strat[s]["realized"] += pnl or 0

    # scenarios: did positions win as often as the entry price / the strategy's model said?
    cal = _q(db, """SELECT s.payout > 0, e.capital / e.matched_qty, e.expected_payout / e.matched_qty, s.pnl
                    FROM settlements s JOIN executions e ON s.ref = e.basket_id
                    WHERE s.kind = 'position' AND e.matched_qty > 0""")
    calib = dict(n=len(cal), wins=sum(1 for r in cal if r[0]),
                 price=sum(r[1] for r in cal) / len(cal) if cal else 0,
                 model=sum(r[2] for r in cal) / len(cal) if cal else 0,
                 pnl=sum(r[3] or 0 for r in cal),
                 open=_q(db, """SELECT COUNT(*), COALESCE(SUM(locked), 0) FROM executions e
                                WHERE matched_qty > 0 AND NOT EXISTS (SELECT 1 FROM settlements s WHERE s.ref = e.basket_id)""")[0])

    reasons = Counter()
    for dec, reason, n in _q(db, "SELECT decision, reason, COUNT(*) FROM opportunities GROUP BY 1,2"):
        if dec == "accepted":
            key = "accepted – " + ("full size" if reason == "full size" else reason.replace("sized down by ", "sized: "))
        else:
            r = reason.split(";")[0].split("(")[0].strip()
            key = "rejected – " + r.replace("halted: ", "halt: ")[:48]
        reasons[key] += n

    scans = _q(db, "SELECT best_buy_sum, best_sell_sum, n_baskets, duration_ms FROM scans")
    buys = [r[0] for r in scans if r[0] is not None]
    bins = [0.97, 0.98, 0.99, 1.00, 1.005, 1.01, 1.02, 1.03, 1.05, 9]
    labels = ["<0.97", "0.97–0.98", "0.98–0.99", "0.99–1.00", "1.000–1.005", "1.005–1.01", "1.01–1.02", "1.02–1.03", "1.03–1.05", ">1.05"]
    hist = [0] * len(labels)
    for v in buys:
        for i, b in enumerate(bins):
            if v < b:
                hist[i] += 1
                break
        else:
            hist[-1] += 1

    execs = _q(db, """SELECT ts, strategy, title, status, target_qty, matched_qty, capital, expected_profit,
                             realized_pnl, locked, residual FROM executions ORDER BY ts DESC LIMIT 25""")
    n_opps = _q(db, "SELECT COUNT(*) FROM opportunities")[0][0]
    attempts = sum(d["filled"] + d["partial"] + d["missed"] for d in by_strat.values())
    hits = sum(d["filled"] + d["partial"] for d in by_strat.values())
    exp_total = sum(d["expected"] for d in by_strat.values())
    last = eq[-1] if eq else (0, start_capital, start_capital, 0, 0, 0, None)
    return dict(
        start=start_capital, curve=curve,
        kpi=dict(equity=last[1], cash=last[2], locked=last[3], residual=last[4], realized=last[1] - start_capital,
                 ret=(last[1] / start_capital - 1) if start_capital else 0, mdd=mdd,
                 attempts=attempts, hit=(hits / attempts) if attempts else 0,
                 capture=(sum(d["realized"] for d in by_strat.values()) / exp_total) if exp_total else 0,
                 opps=n_opps, scans=len(scans), halted=last[6],
                 t0=eq[0][0] if eq else 0, t1=last[0] if eq else 0,
                 med_scan_ms=sorted(r[3] for r in scans)[len(scans) // 2] if scans else 0,
                 baskets=scans[-1][2] if scans else 0),
        strat=by_strat, reasons=reasons.most_common(10),
        hist=dict(labels=labels, counts=hist, n=len(buys)),
        settle=settle, calib=calib,
        execs=[dict(t=r[0], s=r[1], title=r[2], st=r[3], tq=r[4], mq=r[5], cap=r[6], exp=r[7],
                    real=r[8], lock=r[9], res=r[10]) for r in execs],
    )


STRATEGY_NAMES = {"binary_buy_all": "Binär: YES+NO kaufen", "binary_sell_all": "Binär: Split & verkaufen",
                  "negrisk_buy_all": "Multi-Outcome-Korb", "negrisk_no_buy_all": "Multi-Outcome: alle NO",
                  "endgame_buy": "Endspiel-Ernte", "longshot_buy": "Longshot: NO kaufen", "weather_buy": "Wetter-Modell"}
STATUS_NAMES = {"filled": "voll", "partial": "teilweise", "missed": "verpasst"}
CSV_COLUMNS = [
    ("Zeit", "ts"), ("Typ", "typ"), ("Strategie", "strategy_name"), ("Strategie-Code", "strategy"),
    ("Markt", "title"), ("Markt-ID", "basket_id"), ("Status", "status"), ("Menge Ziel", "target_qty"),
    ("Menge gefüllt", "matched_qty"), ("Kapital $", "capital"), ("Erwartet $", "expected_profit"),
    ("Realisiert $", "realized_pnl"), ("Gebunden $", "locked"), ("Erw. Auszahlung $", "expected_payout"),
    ("Auszahlung $", "payout"), ("Offene Reste $", "residual"), ("Latenz ms", "latency_ms"),
    ("Notiz", "note"), ("Fills (JSON)", "fills"),
]
SETTLE_TYPES = {"basket": "Auszahlung Korb", "residual": "Rest aufgelöst", "unwind": "Rest verkauft",
                "position": "Auszahlung Position"}
EXEC_FIELDS = ["ts", "basket_id", "title", "strategy", "status", "target_qty", "matched_qty", "capital",
               "expected_profit", "realized_pnl", "locked", "expected_payout", "residual", "latency_ms",
               "note", "fills"]


def _num(v) -> str:
    return "" if v is None else f"{v:.6f}".rstrip("0").rstrip(".").replace(".", ",")


def trades_csv(db_path: str) -> str:
    """All executions plus their later settlements (basket payouts, leftover sales) in one CSV,
    sorted by time, for German Excel: ';' separator, decimal comma, local time.

    Summing "Realisiert $" over all rows gives the booked PnL: an execution books merge profit
    and unwind losses, a basket books its profit only when the event resolves.
    """
    db = sqlite3.connect(db_path)
    rows = [dict(zip(EXEC_FIELDS, r), typ="Ausführung")
            for r in _q(db, f"SELECT {', '.join(EXEC_FIELDS)} FROM executions")]
    by_basket = {r["basket_id"]: r for r in rows}
    for ts, ref, kind, qty, payout, pnl in _q(db, "SELECT ts, ref, kind, qty, payout, pnl FROM settlements"):
        # basket settlements reference the event id, leftovers the token id (found in the fills)
        src = by_basket.get(ref) or next((r for r in rows if ref and ref in (r["fills"] or "")), {})
        rows.append(dict(ts=ts, typ=SETTLE_TYPES.get(kind, kind), basket_id=src.get("basket_id", ref),
                         title=src.get("title", ""), strategy=src.get("strategy", ""), matched_qty=qty,
                         realized_pnl=pnl, payout=None if kind == "unwind" else payout,
                         note="" if src.get("basket_id") == ref else f"Token {ref}"))
    db.close()
    rows.sort(key=lambda r: r["ts"])

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow([h for h, _ in CSV_COLUMNS])
    for r in rows:
        r["strategy_name"] = STRATEGY_NAMES.get(r.get("strategy"), r.get("strategy"))
        r["status"] = STATUS_NAMES.get(r.get("status"), r.get("status"))
        r["ts"] = datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M:%S")
        out = []
        for _, col in CSV_COLUMNS:
            v = r.get(col)
            out.append(_num(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else ("" if v is None else v))
        w.writerow(out)
    return buf.getvalue()


def _write(path: Path, text: str, encoding: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding=encoding, newline="")
    os.replace(tmp, path)  # the web server never sees a half-written file


def build(db_path: str, out: str, start_capital: float, source: str = "auto", title: str = "",
          kind: str = "arb", nav: list | None = None) -> str:
    data = collect(db_path, float(start_capital))
    if source == "auto":
        source = "mock" if "mock" in Path(db_path).name else "paper"
    data.update(source=source, kind=kind, nav=nav or [],
                title=title or "Polymarket Arbitrage – Paper Trading")
    html = TEMPLATE.replace("/*__DATA__*/null", json.dumps(data, default=float))
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    _write(Path(out).with_name("trades.csv"), trades_csv(db_path), "utf-8-sig")  # BOM: Excel detects UTF-8
    _write(Path(out), html, "utf-8")
    return out


def _summary(db_path: str, start: float) -> float:
    """Return since start from the last equity row (0 if the scenario has no data yet)."""
    if not Path(db_path).exists():
        return 0.0
    db = sqlite3.connect(db_path)
    try:
        row = db.execute("SELECT equity FROM equity ORDER BY ts DESC LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        row = None
    db.close()
    return (row[0] / start - 1) if row and start else 0.0


def build_all(cfg: dict, out: str) -> list:
    """Arbitrage page at `out`, every enabled scenario at <dir>/<name>/index.html, linked by a tab bar."""
    root = Path(out).parent
    data_dir = Path(cfg["storage"]["db_path"]).parent
    pages = [dict(key="", label="Arbitrage", db=cfg["storage"]["db_path"], kind="arb",
                  start=float(cfg["portfolio"]["starting_capital_usd"]), out=Path(out),
                  title="Polymarket Arbitrage – Paper Trading")]
    for name, sc in (cfg.get("scenarios") or {}).items():
        if sc.get("enabled"):
            pages.append(dict(key=name, label=sc.get("title", name), db=str(data_dir / f"scenario-{name}.sqlite"),
                              kind=sc.get("strategy", name), start=float(sc.get("capital_usd", 500)),
                              out=root / name / "index.html", title=f"Szenario: {sc.get('title', name)} – Paper"))
    rets = {p["key"]: _summary(p["db"], p["start"]) for p in pages}
    built = []
    for p in pages:
        up = "../" if p["key"] else ""  # scenario pages live one folder below the arbitrage page
        nav = [dict(label=q["label"], href=up + q["key"] + "/" if q["key"] else up or "./",
                    ret=rets[q["key"]], active=q is p) for q in pages]
        built.append(build(p["db"], str(p["out"]), p["start"], title=p["title"], kind=p["kind"], nav=nav))
    return built


TEMPLATE = r"""<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Polyarb Dashboard</title>
<style>
:root{color-scheme:light;
 --bg:#f6f5f2;--surface:#fcfcfb;--border:#e4e2dc;--grid:#ecebe7;
 --text:#0b0b0b;--text2:#52514e;--muted:#8a8984;
 --s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;
 --good:#008300;--bad:#e34948;--warn:#c98500;--badge:#fff4e0;}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;
 --bg:#121211;--surface:#1a1a19;--border:#2e2e2b;--grid:#262624;
 --text:#fff;--text2:#c3c2b7;--muted:#8a8984;
 --s1:#3987e5;--s2:#d95926;--s3:#199e70;--good:#3fb950;--bad:#e66767;--warn:#e0a526;--badge:#3a2e12;}}
:root[data-theme="dark"]{color-scheme:dark;
 --bg:#121211;--surface:#1a1a19;--border:#2e2e2b;--grid:#262624;
 --text:#fff;--text2:#c3c2b7;--muted:#8a8984;
 --s1:#3987e5;--s2:#d95926;--s3:#199e70;--good:#3fb950;--bad:#e66767;--warn:#e0a526;--badge:#3a2e12;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:15px;margin:0 0 2px}
.sub{color:var(--text2);margin:0 0 18px}
.badge{display:inline-block;font-size:12px;font-weight:600;padding:2px 8px;border-radius:999px;background:var(--badge);color:var(--text);margin-left:8px;vertical-align:3px}
.note{color:var(--text2);font-size:12.5px;margin:2px 0 12px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:16px}
.kpi,.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:14px 16px}
.kpi .l{color:var(--text2);font-size:12.5px}.kpi .v{font-size:24px;font-weight:600;font-variant-numeric:tabular-nums;margin-top:2px}
.kpi .d{color:var(--muted);font-size:12px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
@media (max-width:820px){.grid2{grid-template-columns:1fr}}
.card{margin-bottom:16px;position:relative;min-width:0}
svg{display:block;width:100%;height:auto;overflow:visible}
svg text{fill:var(--text2);font-size:11px}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12.5px;color:var(--text2);margin:6px 0 4px}
.sw{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:-1px}
.tip{position:fixed;pointer-events:none;background:var(--surface);border:1px solid var(--border);border-radius:8px;
 padding:7px 10px;font-size:12.5px;box-shadow:0 4px 16px rgba(0,0,0,.14);display:none;z-index:9;max-width:320px;color:var(--text)}
.tblwrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:12.5px;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--grid);white-space:nowrap}
th{color:var(--text2);font-weight:600}td.num,th.num{text-align:right}
td.t{max-width:300px;overflow:hidden;text-overflow:ellipsis}
.pos{color:var(--good)}.neg{color:var(--bad)}
.st{font-size:11.5px;padding:1px 7px;border-radius:999px;border:1px solid var(--border)}
.head{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
.btn{font-size:12.5px;font-weight:600;color:var(--s1);text-decoration:none;border:1px solid var(--border);border-radius:8px;padding:4px 10px}
.btn:hover{border-color:var(--s1)}
.nav{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 16px}
.tab{font-size:13px;font-weight:600;color:var(--text2);text-decoration:none;border:1px solid var(--border);border-radius:999px;padding:5px 12px;background:var(--surface)}
.tab.on{color:var(--text);border-color:var(--s1)}.tab span{font-weight:500;margin-left:6px}
.warnbox{border-left:3px solid var(--warn);padding:8px 12px;background:var(--surface);border-radius:6px;margin-bottom:16px;font-size:13px}
</style></head><body><div class="wrap">
<nav class="nav" id="nav"></nav>
<h1><span id="ttl"></span> <span class="badge" id="src"></span></h1>
<p class="sub" id="range"></p>
<div id="halt"></div>
<div class="kpis" id="kpis"></div>
<div class="card"><h2>Equity</h2><p class="note" id="eqnote">Gesamtwert = Cash + gebundene Körbe (zu Kosten) + offene Reste (zum Bid). Startkapital als Referenzlinie.</p><div id="eq"></div></div>
<div class="grid2">
 <div class="card"><h2>Erwarteter vs. realisierter Gewinn je Strategie</h2><p class="note" id="pnlnote">Die Lücke ist, was Latenz, Konkurrenz und Leg-Failures kosten.</p>
  <div class="legend"><span><i class="sw" style="background:var(--s1)"></i>Erwartet bei Erkennung</span><span><i class="sw" style="background:var(--s2)"></i>Realisiert</span></div><div id="pnl"></div></div>
 <div class="card"><h2>Ausführung je Strategie</h2><p class="note">Anteil der Versuche, die voll, teilweise oder gar nicht gefüllt wurden.</p>
  <div class="legend"><span><i class="sw" style="background:var(--s1)"></i>Voll</span><span><i class="sw" style="background:var(--s3)"></i>Teilweise</span><span><i class="sw" style="background:var(--s2)"></i>Verpasst</span></div><div id="exec"></div></div>
</div>
<div class="grid2">
 <div class="card" id="histcard"><h2>Wie nah ist der Markt an Arbitrage?</h2><p class="note">Pro Scan: niedrigste Summe YES-Ask + NO-Ask über alle Binärmärkte (vor Fees). Unter 1,00 = Rohsignal.</p><div id="hist"></div></div>
 <div class="card"><h2>Risk-Engine: Entscheidungen</h2><p class="note">Warum Chancen angenommen, verkleinert oder abgelehnt wurden.</p><div id="rsn"></div></div>
</div>
<div class="card"><div class="head"><h2>Letzte Ausführungen</h2><a class="btn" href="trades.csv" download>CSV-Export aller Trades</a></div>
 <p class="note">Hier die letzten 25. Der Export enthält alle Ausführungen und späteren Auszahlungen, wird mit dem Dashboard alle 10 Minuten aktualisiert und öffnet direkt in Excel.</p><div class="tblwrap"><table id="tbl"></table></div></div>
</div><div class="tip" id="tip"></div>
<script>
const D=/*__DATA__*/null;
const $=id=>document.getElementById(id);
const fmt=(v,d=2)=>v==null?"–":Number(v).toLocaleString("de-AT",{minimumFractionDigits:d,maximumFractionDigits:d});
const usd=v=>(v<0?"−":"")+"$"+fmt(Math.abs(v));
const pct=v=>fmt(v*100,1)+" %";
const dt=t=>new Date(t*1000).toLocaleString("de-AT",{day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"});
const tip=$("tip");
$("ttl").textContent=D.title;document.title=D.title;
if(D.kind!=="arb"){$("eqnote").textContent="Gesamtwert = Cash + offene Positionen (zum Bid). Startkapital als Referenzlinie.";
 $("pnlnote").textContent="Erwartet = Gewinn, wenn jede Position so ausgeht, wie die Strategie annimmt. Realisiert zählt erst bei Auflösung."}
$("nav").innerHTML=D.nav.length>1?D.nav.map(n=>`<a class="tab${n.active?" on":""}" href="${n.href}">${n.label}<span class="${n.ret>0.00005?"pos":n.ret<-0.00005?"neg":""}">${(n.ret>=0?"+":"")+fmt(n.ret*100,1)} %</span></a>`).join(""):"";
function showTip(e,html){tip.innerHTML=html;tip.style.display="block";const x=Math.min(e.clientX+14,innerWidth-tip.offsetWidth-8);tip.style.left=x+"px";tip.style.top=(e.clientY+14)+"px"}
function hideTip(){tip.style.display="none"}
const NS="http://www.w3.org/2000/svg";
function el(tag,attrs,parent){const n=document.createElementNS(NS,tag);for(const k in attrs)n.setAttribute(k,attrs[k]);parent&&parent.appendChild(n);return n}
function ticks(min,max,n=4){const span=max-min||1,step=Math.pow(10,Math.floor(Math.log10(span/n)));const m=[1,2,2.5,5,10].find(m=>span/(step*m)<=n)*step;const out=[];for(let v=Math.ceil(min/m)*m;v<=max+1e-9;v+=m)out.push(+v.toFixed(10));return out}

$("src").textContent=D.source==="mock"?"SYNTHETISCHE DATEN (Mock)":"LIVE-Orderbücher · Paper";
const K=D.kpi;
$("range").textContent=K.t0?`${dt(K.t0)} – ${dt(K.t1)} · ${K.scans.toLocaleString("de-AT")} Scans · ${K.baskets} Märkte/Körbe · Median-Scan ${fmt(K.med_scan_ms,0)} ms`:"Noch keine Daten";
if(K.halted)$("halt").innerHTML=`<div class="warnbox"><b>Handel pausiert:</b> ${K.halted}</div>`;
if(D.source==="mock")$("halt").innerHTML+=`<div class="warnbox">Diese Zahlen stammen aus dem <b>synthetischen Mock-Markt</b> und testen nur die Pipeline. Sie sagen nichts über reale Profitabilität aus.</div>`;
const tiles=[["Equity",usd(K.equity),`Start ${usd(D.start)}`],["Rendite",pct(K.ret),`Max. Drawdown ${pct(K.mdd)}`],
 ["PnL",usd(K.realized),`gebunden ${usd(K.locked)} · Reste ${usd(K.residual)}`],["Trefferquote",pct(K.hit),`${K.attempts} Ausführungsversuche`],
 ["Capture",pct(K.capture),"realisiert / erwartet"],["Chancen erkannt",K.opps.toLocaleString("de-AT"),"nach Fees & Mindest-Edge"]];
if(D.kind!=="arb"){tiles[2][2]=`offene Positionen ${usd(K.locked)} (zu Kosten)`;tiles[4]=["Offen",D.calib.open[0],`gebunden ${usd(D.calib.open[1])}`];tiles[5][2]="Preis unter der Strategie-Grenze"}
$("kpis").innerHTML=tiles.map(([l,v,d])=>`<div class="kpi"><div class="l">${l}</div><div class="v">${v}</div><div class="d">${d}</div></div>`).join("");

// ---------- equity line
(function(){
 const box=$("eq"),W=1100,H=260,m={l:56,r:12,t:10,b:26};const c=D.curve;if(c.length<2){box.textContent="Zu wenig Daten.";return}
 const svg=el("svg",{viewBox:`0 0 ${W} ${H}`,role:"img","aria-label":"Equity-Verlauf"},box);
 const t0=c[0].t,t1=c[c.length-1].t,ys=c.map(p=>p.e).concat([D.start]);let y0=Math.min(...ys),y1=Math.max(...ys);const pad=(y1-y0)*0.08||5;y0-=pad;y1+=pad;
 const X=t=>m.l+(t-t0)/(t1-t0||1)*(W-m.l-m.r),Y=v=>m.t+(1-(v-y0)/(y1-y0))*(H-m.t-m.b);
 for(const v of ticks(y0,y1)){el("line",{x1:m.l,x2:W-m.r,y1:Y(v),y2:Y(v),stroke:"var(--grid)"},svg);el("text",{x:m.l-8,y:Y(v)+4,"text-anchor":"end"},svg).textContent="$"+fmt(v,0)}
 const nx=6;for(let i=0;i<=nx;i++){const t=t0+(t1-t0)*i/nx;el("text",{x:X(t),y:H-6,"text-anchor":i==0?"start":i==nx?"end":"middle"},svg).textContent=dt(t)}
 el("line",{x1:m.l,x2:W-m.r,y1:Y(D.start),y2:Y(D.start),stroke:"var(--muted)","stroke-dasharray":"4 4"},svg);
 el("path",{d:c.map((p,i)=>(i?"L":"M")+X(p.t).toFixed(1)+","+Y(p.e).toFixed(1)).join(""),fill:"none",stroke:"var(--s1)","stroke-width":2,"stroke-linejoin":"round"},svg);
 const cross=el("line",{y1:m.t,y2:H-m.b,stroke:"var(--muted)",visibility:"hidden"},svg),dot=el("circle",{r:4.5,fill:"var(--s1)",stroke:"var(--surface)","stroke-width":2,visibility:"hidden"},svg);
 const hit=el("rect",{x:m.l,y:m.t,width:W-m.l-m.r,height:H-m.t-m.b,fill:"transparent"},svg);
 hit.addEventListener("mousemove",e=>{const r=svg.getBoundingClientRect(),tx=t0+((e.clientX-r.left)*W/r.width-m.l)/(W-m.l-m.r)*(t1-t0);let b=c[0];for(const p of c)if(Math.abs(p.t-tx)<Math.abs(b.t-tx))b=p;
  cross.setAttribute("x1",X(b.t));cross.setAttribute("x2",X(b.t));dot.setAttribute("cx",X(b.t));dot.setAttribute("cy",Y(b.e));cross.setAttribute("visibility","visible");dot.setAttribute("visibility","visible");
  showTip(e,`<b>${dt(b.t)}</b><br>Equity ${usd(b.e)}<br>davon gebunden ${usd(b.l)}`)});
 hit.addEventListener("mouseleave",()=>{hideTip();cross.setAttribute("visibility","hidden");dot.setAttribute("visibility","hidden")});
})();

const NAMES={binary_buy_all:"Binär: YES+NO kaufen",binary_sell_all:"Binär: Split & verkaufen",negrisk_buy_all:"Multi-Outcome-Korb",negrisk_no_buy_all:"Multi-Outcome: alle NO",
 endgame_buy:"Endspiel-Ernte",longshot_buy:"Longshot: NO kaufen",weather_buy:"Wetter-Modell"};
// ---------- grouped bars: expected vs realized
(function(){
 const S=Object.entries(D.strat);const box=$("pnl");if(!S.length){box.textContent="Noch keine Trades.";return}
 const W=520,H=240,m={l:56,r:8,t:10,b:40};const svg=el("svg",{viewBox:`0 0 ${W} ${H}`,role:"img","aria-label":"PnL je Strategie"},box);
 const vals=S.flatMap(([,d])=>[d.expected,d.realized]);const y0=Math.min(0,...vals),y1=Math.max(0,...vals)*1.08||1;
 const Y=v=>m.t+(1-(v-y0)/(y1-y0))*(H-m.t-m.b);
 for(const v of ticks(y0,y1)){el("line",{x1:m.l,x2:W-m.r,y1:Y(v),y2:Y(v),stroke:"var(--grid)"},svg);el("text",{x:m.l-8,y:Y(v)+4,"text-anchor":"end"},svg).textContent="$"+fmt(v,0)}
 const gw=(W-m.l-m.r)/S.length,bw=Math.min(34,gw/3);
 S.forEach(([k,d],i)=>{const cx=m.l+gw*(i+.5);
  [["expected","var(--s1)",-1],["realized","var(--s2)",1]].forEach(([f,col,s])=>{const v=d[f],x=cx+(s<0?-bw-1:1),y=Math.min(Y(v),Y(0)),h=Math.max(1,Math.abs(Y(v)-Y(0)));
   const r=el("rect",{x,y,width:bw,height:h,rx:4,fill:col},svg);
   r.addEventListener("mousemove",e=>showTip(e,`<b>${NAMES[k]||k}</b><br>${f==="expected"?"Erwartet":"Realisiert"}: ${usd(v)}<br>Capture: ${d.expected?pct(d.realized/d.expected):"–"}`));r.addEventListener("mouseleave",hideTip)});
  el("text",{x:cx,y:H-22,"text-anchor":"middle"},svg).textContent=(NAMES[k]||k).split(":")[0];
  el("text",{x:cx,y:H-8,"text-anchor":"middle"},svg).textContent=(NAMES[k]||k).split(":")[1]||"";
 });
 el("line",{x1:m.l,x2:W-m.r,y1:Y(0),y2:Y(0),stroke:"var(--muted)"},svg);
})();

// ---------- 100% stacked horizontal bars: execution outcomes
(function(){
 const S=Object.entries(D.strat);const box=$("exec");if(!S.length){box.textContent="Noch keine Trades.";return}
 const W=520,rowH=46,m={l:150,r:10,t:6},H=m.t+rowH*S.length;const svg=el("svg",{viewBox:`0 0 ${W} ${H}`,role:"img","aria-label":"Ausführungsqualität"},box);
 const segs=[["filled","var(--s1)","Voll"],["partial","var(--s3)","Teilweise"],["missed","var(--s2)","Verpasst"]];
 S.forEach(([k,d],i)=>{const tot=d.filled+d.partial+d.missed||1;let x=m.l;const y=m.t+i*rowH+8,h=22,w=W-m.l-m.r;
  el("text",{x:m.l-10,y:y+15,"text-anchor":"end"},svg).textContent=NAMES[k]||k;
  segs.forEach(([f,col,lab],j)=>{const sw=d[f]/tot*w;if(sw<=0)return;const r=el("rect",{x:x+(j?1:0),y,width:Math.max(0,sw-(j<2?2:0)),height:h,rx:4,fill:col},svg);
   r.addEventListener("mousemove",e=>showTip(e,`<b>${NAMES[k]||k}</b><br>${lab}: ${d[f]} von ${tot} (${pct(d[f]/tot)})`));r.addEventListener("mouseleave",hideTip);x+=sw});
  el("text",{x:W-m.r,y:y+h+14,"text-anchor":"end"},svg).textContent=`${tot} Versuche · ${pct((d.filled+d.partial)/tot)} gefüllt`;
 });
})();

// ---------- scenarios: calibration instead of the arbitrage histogram
if(D.kind!=="arb"){const C=D.calib;$("histcard").innerHTML=`<h2>Hat die Strategie einen Edge?</h2>
 <p class="note">Aufgelöste Positionen: Gewinnt die Strategie öfter, als der Einstiegspreis sagt? Nur dann bleibt nach vielen Trades Gewinn übrig. Aussagekräftig erst ab etwa 30 Auflösungen.</p>
 <div class="kpis">${[["Aufgelöst",C.n,`offen: ${C.open[0]} (${usd(C.open[1])})`],["Gewinnquote",C.n?pct(C.wins/C.n):"–",`${C.wins} von ${C.n}`],
 ["Preis sagte",C.n?pct(C.price):"–","Ø Einstiegspreis"],["PnL aufgelöst",usd(C.pnl),D.kind==="weather"?`Modell sagte ${C.n?pct(C.model):"–"}`:"nach Fees"]]
 .map(([l,v,d])=>`<div class="kpi"><div class="l">${l}</div><div class="v">${v}</div><div class="d">${d}</div></div>`).join("")}</div>`}
// ---------- histogram of closest buy sums
(function(){if(D.kind!=="arb")return;
 const Hh=D.hist,box=$("hist");if(!Hh.n){box.textContent="Noch keine Scans.";return}
 const W=520,H=240,m={l:44,r:8,t:10,b:44};const svg=el("svg",{viewBox:`0 0 ${W} ${H}`,role:"img","aria-label":"Verteilung der Ask-Summen"},box);
 const mx=Math.max(...Hh.counts)*1.08||1,Y=v=>m.t+(1-v/mx)*(H-m.t-m.b),bw=(W-m.l-m.r)/Hh.counts.length;
 for(const v of ticks(0,mx)){el("line",{x1:m.l,x2:W-m.r,y1:Y(v),y2:Y(v),stroke:"var(--grid)"},svg);el("text",{x:m.l-8,y:Y(v)+4,"text-anchor":"end"},svg).textContent=v.toLocaleString("de-AT")}
 Hh.counts.forEach((n,i)=>{const x=m.l+i*bw+1,arb=i<4;const r=el("rect",{x,y:Y(n),width:bw-2,height:Math.max(0,Y(0)-Y(n)),rx:4,fill:arb?"var(--s2)":"var(--s1)"},svg);
  r.addEventListener("mousemove",e=>showTip(e,`<b>Summe ${Hh.labels[i]}</b><br>${n.toLocaleString("de-AT")} Scans (${pct(n/Hh.n)})${arb?"<br>Rohsignal vor Fees":""}`));r.addEventListener("mouseleave",hideTip);
  const t=el("text",{x:x+bw/2,y:H-m.b+14,"text-anchor":"end",transform:`rotate(-35 ${x+bw/2} ${H-m.b+14})`},svg);t.textContent=Hh.labels[i]});
 el("line",{x1:m.l+4*bw,x2:m.l+4*bw,y1:m.t,y2:H-m.b,stroke:"var(--muted)","stroke-dasharray":"3 3"},svg);
 el("text",{x:m.l+4*bw+4,y:m.t+10},svg).textContent="1,00";
})();

// ---------- decisions bar
(function(){
 const R=D.reasons,box=$("rsn");if(!R.length){box.textContent="Noch keine Entscheidungen.";return}
 const W=520,rowH=24,m={l:250,r:48,t:4},H=m.t+rowH*R.length;const svg=el("svg",{viewBox:`0 0 ${W} ${H}`,role:"img","aria-label":"Risk-Entscheidungen"},box);
 const mx=Math.max(...R.map(r=>r[1]));
 R.forEach(([k,n],i)=>{const y=m.t+i*rowH,w=(W-m.l-m.r)*n/mx;const acc=k.startsWith("accepted");
  el("text",{x:m.l-8,y:y+15,"text-anchor":"end"},svg).textContent=k.length>40?k.slice(0,39)+"…":k;
  const r=el("rect",{x:m.l,y:y+4,width:Math.max(2,w),height:15,rx:4,fill:acc?"var(--s1)":"var(--s2)"},svg);
  r.addEventListener("mousemove",e=>showTip(e,`<b>${k}</b><br>${n.toLocaleString("de-AT")}×`));r.addEventListener("mouseleave",hideTip);
  el("text",{x:m.l+Math.max(2,w)+6,y:y+15},svg).textContent=n.toLocaleString("de-AT")});
})();

// ---------- table
(function(){
 const E=D.execs;const cls=v=>v>0.005?"pos":v<-0.005?"neg":"";
 const ST={filled:"voll",partial:"teilweise",missed:"verpasst"};
 $("tbl").innerHTML=`<tr><th>Zeit</th><th>Strategie</th><th>Markt</th><th>Status</th><th class="num">Menge</th><th class="num">Kapital</th><th class="num">Erwartet</th><th class="num">Realisiert</th><th class="num">Gebunden</th></tr>`+
 (E.length?E.map(r=>`<tr><td>${dt(r.t)}</td><td>${(NAMES[r.s]||r.s)}</td><td class="t" title="${r.title.replace(/"/g,"&quot;")}">${r.title}</td><td><span class="st">${ST[r.st]||r.st}</span></td>
 <td class="num">${fmt(r.mq,1)} / ${fmt(r.tq,1)}</td><td class="num">${usd(r.cap)}</td><td class="num">${usd(r.exp)}</td><td class="num ${cls(r.real)}">${usd(r.real)}</td><td class="num">${r.lock?usd(r.lock):"–"}</td></tr>`).join(""):`<tr><td colspan="9">Noch keine Ausführungen.</td></tr>`);
})();
</script></body></html>"""


if __name__ == "__main__":
    import sys
    db = sys.argv[1] if len(sys.argv) > 1 else "data/polyarb.sqlite"
    print(build(db, sys.argv[2] if len(sys.argv) > 2 else "dashboard.html", 2500))
