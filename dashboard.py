"""Build a self-contained HTML dashboard (no external scripts) from the SQLite log."""
from __future__ import annotations

import csv
import html
import io
import json
import os
import sqlite3
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path


def _q(db, sql, args=()):
    return db.execute(sql, args).fetchall()


def capacity_rows(db_path: str) -> list:
    """Per market day (UTC date of the end): opportunities, what was bought, and what the order books
    offered – up to the buy limit (ask + slippage), up to ask + 2 cents, up to the rule's price limit."""
    if not Path(db_path).exists():
        return []
    db = sqlite3.connect(db_path)
    try:
        rows = db.execute("""SELECT date(c.end_ts, 'unixepoch') d, COUNT(*), SUM(c.depth_slip), SUM(c.depth_2c),
                                    SUM(c.depth_band),
                                    COALESCE(SUM((SELECT SUM(e.locked) FROM executions e
                                                  WHERE e.basket_id LIKE '%:' || c.token AND e.matched_qty > 0)), 0)
                             FROM capacity c WHERE c.end_ts IS NOT NULL GROUP BY d ORDER BY d DESC LIMIT 60""").fetchall()
    except sqlite3.OperationalError:
        rows = []
    db.close()
    return [dict(day=r[0], n=r[1], slip=r[2] or 0, c2=r[3] or 0, band=r[4] or 0, bought=r[5] or 0) for r in rows]


def collect(db_path: str, start_capital: float) -> dict:
    from arb.storage import Store
    Store(db_path).db.close()  # ensure schema exists
    db = sqlite3.connect(db_path)
    eq = _q(db, "SELECT ts, equity, cash, locked, residual, realized_cum, halted FROM equity ORDER BY ts")
    step = max(1, len(eq) // 600)
    # r = start + realized PnL: what the book is worth counting only resolved/merged positions
    curve = [dict(t=r[0], e=round(r[1], 2), l=round(r[3], 2), r=round(start_capital + (r[5] or 0), 2)) for r in eq[::step]]
    if eq and (not curve or curve[-1]["t"] != eq[-1][0]):
        curve.append(dict(t=eq[-1][0], e=round(eq[-1][1], 2), l=round(eq[-1][3], 2),
                          r=round(start_capital + (eq[-1][5] or 0), 2)))

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
    # locked = what was really paid (the capital column held the planned size for partial fills before 02.10.)
    cal = _q(db, """SELECT s.payout > 0, COALESCE(NULLIF(e.locked, 0), e.capital) / e.matched_qty,
                           e.expected_payout / e.matched_qty, s.pnl
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
                 real=last[5] or 0.0, real_ret=((last[5] or 0.0) / start_capital) if start_capital else 0,
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
                  "ladder_buy_all": "Logische Arbitrage", "underdog_buy": "Underdog-Sport", "favorite_buy": "Favorit-Kleinmarkt", "weather_no_buy": "Wetter-NO", "fussball_dog_buy": "Fußball-Außenseiter", "wetter_no_breit_buy": "Wetter-NO breit", "wetter_no_mess_buy": "Wetter-NO + Messwerte", "wetter_no_streng_buy": "Wetter-NO streng", "finanz_dog_buy": "Finanz-Außenseiter", "mlb_spread_buy": "MLB-Spread-Außenseiter", "endgame_buy": "Endspiel-Ernte", "longshot_buy": "Longshot: NO kaufen", "weather_buy": "Wetter-Modell"}
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


def _csv_rows(w, header: list, rows) -> None:
    w.writerow(header)
    for r in rows:
        w.writerow([_num(v) if isinstance(v, float) else ("" if v is None else v) for v in r])


def _csv(header: list, rows: list) -> str:
    """German-Excel CSV: ';' separator, decimal comma."""
    buf = io.StringIO()
    _csv_rows(csv.writer(buf, delimiter=";", lineterminator="\r\n"), header, rows)
    return buf.getvalue()


def _write_csv(path: Path, header: list, rows) -> None:
    """Like _write(path, _csv(...)) but streamed row by row: the raw study tables are too big to hold in memory."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:
        _csv_rows(csv.writer(fh, delimiter=";", lineterminator="\r\n"), header, rows)
    os.replace(tmp, path)


def equity_csv(db_path: str, every_s: float = 300) -> str:
    """Equity curve, one row per `every_s` (the bot logs every few seconds)."""
    rows = []
    if Path(db_path).exists():
        db = sqlite3.connect(db_path)
        last = -1e18
        for r in _q(db, "SELECT ts, equity, cash, locked, residual, realized_cum, halted FROM equity ORDER BY ts"):
            if r[0] - last >= every_s:
                rows.append([datetime.fromtimestamp(r[0]).strftime("%Y-%m-%d %H:%M:%S"), *r[1:6], r[6] or ""])
                last = r[0]
        db.close()
    return _csv(["Zeit", "Equity $", "Cash $", "Gebunden $", "Offene Reste $", "Realisiert kumuliert $", "Pausiert"], rows)


def _write(path: Path, text: str, encoding: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding=encoding, newline="")
    os.replace(tmp, path)  # the web server never sees a half-written file


def build(db_path: str, out: str, start_capital: float, source: str = "auto", title: str = "",
          kind: str = "arb", nav: list | None = None, overview: list | None = None, scan: dict | None = None) -> str:
    data = collect(db_path, float(start_capital))
    if source == "auto":
        source = "mock" if "mock" in Path(db_path).name else "paper"
    cap = capacity_rows(db_path) if kind not in ("arb", "ladder") else []
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    if cap:
        _write(Path(out).with_name("kapazitaet.csv"), _csv(
            ["Markttag (UTC)", "Gelegenheiten", "gekauft $", "im Buch bis Kaufgrenze $", "bis Ask + 2 ct $",
             "bis Preisgrenze der Regel $"], [[c["day"], c["n"], c["bought"], c["slip"], c["c2"], c["band"]] for c in cap]),
            "utf-8-sig")
    data.update(source=source, kind=kind, nav=nav or [], overview=overview or [], scan=scan or {}, capacity=cap,
                title=title or "Polymarket Arbitrage – Paper Trading")
    html = TEMPLATE.replace("/*__DATA__*/null", json.dumps(data, default=float))
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    _write(Path(out).with_name("trades.csv"), trades_csv(db_path), "utf-8-sig")  # BOM: Excel detects UTF-8
    _write(Path(out).with_name("equity.csv"), equity_csv(db_path), "utf-8-sig")
    _write(Path(out), html, "utf-8")
    return out


def _summary(db_path: str, start: float) -> dict:
    """Last equity row of a book: return incl. open positions (at the bid) and realized only."""
    out = dict(start=start, equity=start, real=0.0, ret=0.0, real_ret=0.0, n=0, won=0, open=0)
    if not Path(db_path).exists():
        return out
    db = sqlite3.connect(db_path)
    try:
        row = db.execute("SELECT equity, realized_cum FROM equity ORDER BY ts DESC LIMIT 1").fetchone()
        res = db.execute("SELECT COUNT(*), SUM(payout > 0) FROM settlements WHERE kind IN ('position', 'basket')").fetchone()
        opn = db.execute("""SELECT COUNT(*) FROM executions e WHERE matched_qty > 0
                            AND NOT EXISTS (SELECT 1 FROM settlements s WHERE s.ref = e.basket_id)""").fetchone()
    except sqlite3.OperationalError:
        row = res = opn = None
    db.close()
    if row and start:
        out.update(equity=row[0], real=row[1] or 0.0, ret=row[0] / start - 1, real_ret=(row[1] or 0.0) / start)
    if res:
        out.update(n=res[0] or 0, won=res[1] or 0)
    if opn:
        out.update(open=opn[0] or 0)
    return out


def _cached_stress(study_db: str, stress_test) -> list:
    """The stress test takes about a minute; the study changes every 30 min, the dashboard every 10."""
    import hashlib
    import json
    from arb import backtest
    p = Path(study_db)
    if not p.exists():
        return []
    src = Path(backtest.__file__).read_bytes() + Path(backtest.__file__).with_name("study.py").read_bytes()
    key = f"{p.stat().st_mtime_ns}|{p.stat().st_size}|{hashlib.sha1(src).hexdigest()}"
    cache = p.with_name("stress-cache.json")
    try:
        c = json.loads(cache.read_text("utf-8"))
        if c.get("key") == key:
            return c["stress"]
    except (OSError, ValueError, KeyError):
        pass
    stress = stress_test(study_db, n_boot=500)
    try:
        cache.write_text(json.dumps({"key": key, "stress": stress}), "utf-8")
    except OSError:
        pass
    return stress


def build_all(cfg: dict, out: str) -> list:
    """Arbitrage page at `out`, every enabled scenario at <dir>/<name>/, the market study at <dir>/study/
    and the export section at <dir>/export/ – all linked by one tab bar."""
    root = Path(out).parent
    data_dir = Path(cfg["storage"]["db_path"]).parent
    pages = [dict(key="", label="Arbitrage", db=cfg["storage"]["db_path"], kind="arb",
                  start=float(cfg["portfolio"]["starting_capital_usd"]), out=Path(out),
                  title="Polymarket Arbitrage – Paper Trading")]
    for name, sc in (cfg.get("scenarios") or {}).items():
        if sc.get("enabled"):
            pages.append(dict(key=name, label=sc.get("title", name), db=str(data_dir / f"scenario-{name}.sqlite"),
                              kind=sc.get("strategy", name), start=float(sc.get("capital_usd", 2500)),
                              out=root / name / "index.html", title=f"Szenario: {sc.get('title', name)} – Paper"))
    rets = {p["key"]: _summary(p["db"], p["start"]) for p in pages}
    extra = [("study", "Studie"), ("wxobs", "Wetter-Messwerte"), ("kalshi", "Kalshi"), ("export", "Export")]

    def nav_for(active: str) -> list:
        up = "../" if active else ""  # every page except the arbitrage one lives one folder down
        nav = [dict(label=q["label"], href=up + q["key"] + "/" if q["key"] else up or "./",
                    ret=rets[q["key"]]["ret"], real=rets[q["key"]]["real_ret"], active=q["key"] == active)
               for q in pages]
        return nav + [dict(label=l, href=up + k + "/", ret=None, active=k == active) for k, l in extra]

    overview = [dict(rets[p["key"]], label=p["label"], href=(p["key"] + "/") if p["key"] else "./") for p in pages]
    def scan_of(p: dict) -> dict:
        try:
            return json.loads((data_dir / f"scenario-{p['key']}.scan.json").read_text("utf-8")) if p["key"] else {}
        except (OSError, ValueError):
            return {}

    scans = {p["key"]: scan_of(p) for p in pages}
    built = [build(p["db"], str(p["out"]), p["start"], title=p["title"], kind=p["kind"], nav=nav_for(p["key"]),
                   overview=overview if not p["key"] else None, scan=scans[p["key"]])
             for p in pages]
    for p in pages:  # last scan as CSV next to the trades (export)
        if scans[p["key"]]:
            sc = dict(scans[p["key"]])
            sc["ts"] = datetime.fromtimestamp(sc["ts"]).strftime("%Y-%m-%d %H:%M") if sc.get("ts") else ""
            _write(p["out"].parent / "scan.csv", _csv(["Schritt", "Anzahl"], [[k, v] for k, v in sc.items()]), "utf-8-sig")

    from arb.backtest import stress_test
    from arb.odds import compare
    from arb.study import calibration
    study_db = str(data_dir / "study.sqlite")
    stress = _cached_stress(study_db, stress_test)
    books = compare(study_db)
    (root / "study").mkdir(parents=True, exist_ok=True)
    calib = calibration(study_db)
    _write(root / "study" / "index.html", study_page(calib, nav_for("study"), stress, books), "utf-8")
    built.append(str(root / "study" / "index.html"))

    from arb.kalshi import analysis as kalshi_analysis, csv_rows as kalshi_csv_rows
    kalshi_db = str(data_dir / "kalshi.sqlite")
    kal = kalshi_analysis(kalshi_db)
    (root / "kalshi").mkdir(parents=True, exist_ok=True)
    _write(root / "kalshi" / "index.html", kalshi_page(kal, nav_for("kalshi")), "utf-8")
    built.append(str(root / "kalshi" / "index.html"))

    from arb.wxobs import analysis as wx_analysis, csv_rows as wx_csv_rows
    wx_db = str(data_dir / "wxobs.sqlite")
    (root / "wxobs").mkdir(parents=True, exist_ok=True)
    _write(root / "wxobs" / "index.html", wxobs_page(wx_analysis(wx_db), nav_for("wxobs")), "utf-8")
    built.append(str(root / "wxobs" / "index.html"))

    exp = root / "export"
    exp.mkdir(parents=True, exist_ok=True)
    _write_csv(exp / "kalshi-maerkte.csv", *kalshi_csv_rows(kalshi_db))
    _write_csv(exp / "wetter-messwerte.csv", *wx_csv_rows(wx_db))
    wxa = wx_analysis(wx_db)
    variants = (("Endvolumen < 5k (wie gefunden)", "no_filter"), ("alle Volumen", "no_filter_allvol"),
                ("alle Volumen, Zufalls-Stichprobe", "no_filter_clean"))
    _write(exp / "wetter-no-filter.csv", _csv(
        ["Tabelle", "Gruppe", "Ortszeit", "Käufe", "Ø NO-Preis", "NO gewonnen", "Rendite +2ct", "1. Hälfte", "2. Hälfte",
         "Variante"],
        [[t, r.get("cls") or r.get("name"), r.get("hour", ""), r["n"], r.get("price"), r.get("hit"), r.get("roi"),
          r.get("first"), r.get("second"), vname]
         for vname, vkey in variants
         for t, key in (("Stand", "by_cls"), ("Filter", "filters"), ("Ortszeit", "by_hour"))
         for r in (wxa.get(vkey) or {}).get(key, []) if r.get("n")]), "utf-8-sig")
    from arb.kalshi import diag_rows as kalshi_diag_rows
    from arb.wxobs import station_rows as wx_station_rows
    _write(exp / "kalshi-diagnose.csv", _csv(*kalshi_diag_rows(kalshi_db)), "utf-8-sig")
    _write(exp / "wetter-stationen.csv", _csv(*wx_station_rows(wx_db)), "utf-8-sig")
    _write_csv(exp / "studie-maerkte.csv", STUDY_MARKET_HEADER, study_market_rows(study_db))
    _write_csv(exp / "studie-kalibrierung.csv", ["Zeitpunkt", "Preisbereich", "n", "Ø Preis", "gewonnen", "95% von", "95% bis",
                                                  "Differenz"],
               [[r["cp"], f'{r["lo"]:.2f}-{r["hi"]:.2f}', r["n"], r["price"], r["rate"], r["ci_lo"], r["ci_hi"], r["edge"]]
                for r in calib["rows"]])
    _write(exp / "studie-stresstest.csv", stress_csv(stress), "utf-8-sig")
    _write(exp / "studie-buchmacher.csv", books_csv(study_db), "utf-8-sig")
    files = []  # (group, label, path relative to export/, file on disk, name inside the zip)
    for p in pages:
        folder = p["out"].parent
        slug = p["key"] or "arbitrage"
        rel = "../" + (p["key"] + "/" if p["key"] else "")
        files += [(p["label"], "Alle Trades und Auszahlungen", rel + "trades.csv", folder / "trades.csv", f"{slug}/trades.csv"),
                  (p["label"], "Equity-Verlauf (alle 5 Min.)", rel + "equity.csv", folder / "equity.csv", f"{slug}/equity.csv")]
        if (folder / "kapazitaet.csv").exists():
            files.append((p["label"], "Kapazität je Markttag: Gelegenheiten, gekauft, Geld im Orderbuch", rel + "kapazitaet.csv",
                          folder / "kapazitaet.csv", f"{slug}/kapazitaet.csv"))
        if (folder / "scan.csv").exists():
            files.append((p["label"], "Letzter Scan: wo Märkte am Filter hängen bleiben", rel + "scan.csv",
                          folder / "scan.csv", f"{slug}/scan.csv"))
    files += [("Studie", "Aufgelöste Märkte mit Preisen vor Schluss und Ergebnis", "studie-maerkte.csv",
               exp / "studie-maerkte.csv", "studie/maerkte.csv"),
              ("Studie", "Kalibrierung nach Zeitpunkt und Preisbereich", "studie-kalibrierung.csv",
               exp / "studie-kalibrierung.csv", "studie/kalibrierung.csv"),
              ("Studie", "Stresstest der Strategie-Regeln (Kosten, Glück, Zeit, Drawdown)", "studie-stresstest.csv",
               exp / "studie-stresstest.csv", "studie/stresstest.csv"),
              ("Studie", "Fußball: Polymarket-Preise neben Pinnacle-Quoten (je Markt)", "studie-buchmacher.csv",
               exp / "studie-buchmacher.csv", "studie/buchmacher.csv"),
              ("Kalshi", "Aufgelöste Kalshi-Märkte mit Preis und echtem Ask vor Schluss", "kalshi-maerkte.csv",
               exp / "kalshi-maerkte.csv", "kalshi/maerkte.csv"),
              ("Wetter-Messwerte", "Temperatur-Buckets: ab wann laut Station unmöglich, und Preis danach",
               "wetter-messwerte.csv", exp / "wetter-messwerte.csv", "wetter-messwerte/maerkte.csv"),
              ("Wetter-Messwerte", "Wetter-NO nach Stand der Station beim Kauf (Filter-Varianten)", "wetter-no-filter.csv",
               exp / "wetter-no-filter.csv", "wetter-messwerte/no-filter.csv"),
              ("Wetter-Messwerte", "Stationen je Stadt und was die Marktbeschreibung verlinkt", "wetter-stationen.csv",
               exp / "wetter-stationen.csv", "wetter-messwerte/stationen.csv"),
              ("Kalshi", "Diagnose des letzten Laufs (Anfragen, Fehler, Felder der API)", "kalshi-diagnose.csv",
               exp / "kalshi-diagnose.csv", "kalshi/diagnose.csv")]
    _zip(exp / "polyarb-export.zip", files)
    _zip(exp / "polyarb-kompakt.zip", compact_files(files))
    study_zips = {g: _zip_split(exp, slug, [f for f in files if f[0] == g]) for g, slug in STUDY_ZIPS.items()}
    _write(exp / "index.html", export_page(files, exp / "polyarb-export.zip", nav_for("export"), study_zips), "utf-8")
    built.append(str(exp / "index.html"))
    return built


# ====================================================================== study & export pages (static)
def _nav_html(nav: list) -> str:
    out = []
    for n in nav:
        r = n.get("ret")
        span = "" if r is None else (f'<span class="{"pos" if r > 0.00005 else "neg" if r < -0.00005 else ""}">'
                                     f'{"+" if r >= 0 else ""}{r * 100:.1f} %</span>'.replace(".", ","))
        tip = "" if n.get("real") is None else f' title="realisiert {n["real"] * 100:+.1f} %"'.replace(".", ",")
        out.append(f'<a class="tab{" on" if n["active"] else ""}" href="{n["href"]}"{tip}>{html.escape(n["label"])}{span}</a>')
    return f'<nav class="nav">{"".join(out)}</nav>'


def _page(title: str, nav: list, body: str) -> str:
    style = TEMPLATE.split("<style>", 1)[1].split("</style>", 1)[0]
    return (f'<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title><style>{style}'
            f'.dl{{display:flex;justify-content:space-between;gap:12px;align-items:center;padding:9px 0;border-bottom:1px solid var(--grid)}}'
            f'.dl:last-child{{border-bottom:0}}.dl .m{{color:var(--text2);font-size:12.5px}}</style></head>'
            f'<body><div class="wrap">{_nav_html(nav)}<h1>{html.escape(title)}</h1>{body}</div></body></html>')


def _de(v: float, d: int = 1) -> str:
    return f"{v:.{d}f}".replace(".", ",")


def _calib_svg(rows: list) -> str:
    """Price (x) vs. realized win rate (y) for 1 day and 1 hour before close; diagonal = fair."""
    W, H, m = 520, 300, dict(l=44, r=12, t=10, b=34)
    X = lambda v: m["l"] + v * (W - m["l"] - m["r"])
    Y = lambda v: m["t"] + (1 - v) * (H - m["t"] - m["b"])
    parts = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Kalibrierung">']
    for v in (0, 0.25, 0.5, 0.75, 1):
        parts.append(f'<line x1="{m["l"]}" x2="{W - m["r"]}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="var(--grid)"/>'
                     f'<text x="{m["l"] - 6}" y="{Y(v) + 4:.1f}" text-anchor="end">{int(v * 100)} %</text>'
                     f'<text x="{X(v):.1f}" y="{H - 12}" text-anchor="middle">{int(v * 100)} %</text>')
    parts.append(f'<line x1="{X(0)}" y1="{Y(0)}" x2="{X(1)}" y2="{Y(1)}" stroke="var(--muted)" stroke-dasharray="4 4"/>')
    for cp, col in (("p_1d", "var(--s1)"), ("p_1h", "var(--s2)")):
        pts = [r for r in rows if r["cp"] == cp]
        if pts:
            parts.append('<path d="' + "".join(f'{"L" if i else "M"}{X(r["price"]):.1f},{Y(r["rate"]):.1f}'
                                               for i, r in enumerate(pts)) + f'" fill="none" stroke="{col}" stroke-width="2"/>')
            parts += [f'<circle cx="{X(r["price"]):.1f}" cy="{Y(r["rate"]):.1f}" r="4" fill="{col}">'
                      f'<title>Preis Ø {_de(r["price"] * 100)} % – gewonnen {_de(r["rate"] * 100)} % (n={r["n"]})</title></circle>'
                      for r in pts]
    parts.append("</svg>")
    return "".join(parts)


def stress_csv(stress: list) -> str:
    rows = [[r["name"], r["n"], r.get("games", ""), r.get("price"), r.get("hit"),
             *[r["roi"][k] for k in sorted(r["roi"])], r.get("boot_p5"), r.get("boot_p95"), r.get("p_loss"),
             r.get("roi_first"), r.get("roi_second"), r.get("dd_median"), r.get("dd_p95"), r.get("worst_streak", "")]
            for r in stress if r.get("roi")]
    return _csv(["Regel", "Wetten", "Spiele", "Ø Preis", "Trefferquote", "ROI +0ct", "ROI +1ct", "ROI +2ct", "ROI +3ct",
                 "ROI +4ct", "ROI +5ct", "Bootstrap 5 %", "Bootstrap 95 %", "P(Verlust)", "ROI 1. Hälfte",
                 "ROI 2. Hälfte", "Drawdown Median $", "Drawdown schlechteste 5 % $", "längste Verlustserie"], rows)


def _stress_card(stress: list) -> str:
    done = [r for r in stress if r.get("roi")]
    if not done:
        return ""
    s = done[0]
    def pct(v, sign=True):
        v = round(v * 100) or 0  # no "-0 %"
        return ("+" if sign and v > 0 else "") + _de(v, 0) + " %"
    def cls(v):
        return "pos" if v > 0 else "neg" if v < 0 else ""
    trs = []
    for r in stress:
        if not r.get("roi"):
            trs.append(f'<tr><td>{html.escape(r["name"])}</td><td class="num">{r["n"]}</td><td colspan="8">zu wenige Fälle</td></tr>')
            continue
        rois = "".join(f'<td class="num {cls(r["roi"][k])}">{pct(r["roi"][k])}</td>' for k in ("0.00", "0.02", "0.04"))
        robust = r["boot_p5"] > 0 and r["roi_first"] > 0 and r["roi_second"] > 0
        trs.append(f'<tr><td>{html.escape(r["name"])}{" ✓" if robust else ""}</td><td class="num">{r["n"]} / {r["games"]}</td>'
                   f'<td class="num">{_de(r["price"] * 100)} % → {_de(r["hit"] * 100)} %</td>{rois}'
                   f'<td class="num">{pct(r["boot_p5"])} … {pct(r["boot_p95"])}</td><td class="num">{_de(r["p_loss"] * 100, 0)} %</td>'
                   f'<td class="num">{pct(r["roi_first"])} / {pct(r["roi_second"])}</td>'
                   f'<td class="num">{_de(r["dd_p95"], 0)} $ · {r["worst_streak"]}</td></tr>')
    return (f'<div class="card"><h2>Stresstest der Strategie-Regeln</h2><p class="note">Jede Regel kauft alle Seiten im '
            f'Preisbereich zum jeweiligen Zeitpunkt und hält bis zur Auflösung, inkl. Fee. <b>Kosten:</b> ROI, wenn der echte '
            f'Kaufpreis 0 / 2 / 4 Cent über dem historischen Kurs liegt. <b>Glück:</b> 5–95-%-Bereich des ROI, wenn ganze Spiele '
            f'zufällig neu gezogen werden (bei +{_de(s["stress_slip"] * 100, 0)} ct), und wie oft er dabei negativ ist. '
            f'<b>Zeit:</b> ROI in der ersten und zweiten Hälfte des Zeitraums. <b>Schmerz:</b> schlechteste 5 % Drawdown bei '
            f'{_de(s["stake"], 0)} $ pro Wette und längste Verlustserie. ✓ = besteht Glück und Zeit.</p>'
            '<div class="tblwrap"><table><tr><th>Regel</th><th class="num">Wetten / Spiele</th><th class="num">Preis → gewonnen</th>'
            '<th class="num">ROI +0 ct</th><th class="num">+2 ct</th><th class="num">+4 ct</th><th class="num">Glück 5–95 %</th>'
            '<th class="num">P(Verlust)</th><th class="num">1. / 2. Hälfte</th><th class="num">Drawdown · Serie</th></tr>'
            + "".join(trs) + '</table></div></div>')


def books_csv(study_db: str) -> str:
    rows = []
    if Path(study_db).exists():
        db = sqlite3.connect(study_db)
        try:
            for r in _q(db, """SELECT o.day, o.league, o.home, o.away, o.result, l.role, m.question, m.outcome,
                                      m.p_6h, m.p_1h, l.p_book, o.source, l.score
                               FROM odds_link l JOIN markets m ON m.condition_id = l.condition_id
                               JOIN odds o ON o.key = l.odds_key ORDER BY o.day"""):
                rows.append([r[0], r[1], r[2], r[3], r[4], {"home": "Heimsieg", "away": "Auswärtssieg"}.get(r[5], "Remis"),
                             r[6], "YES" if r[7] else "NO", r[8], r[9], r[10], r[11], r[12]])
        except sqlite3.OperationalError:
            pass
        db.close()
    return _csv(["Tag", "Liga", "Heim", "Auswärts", "Ergebnis (H/D/A)", "Markt", "Polymarket-Frage", "Ausgang",
                 "Polymarket 6 h vorher", "Polymarket 1 h vorher", "Buchmacher (ohne Marge)", "Quelle",
                 "Namens-Übereinstimmung"], rows)


def _books_card(b: dict | None) -> str:
    if not b:
        return ""
    if not b.get("n"):
        return ('<div class="card"><h2>Polymarket gegen Buchmacher (Fußball)</h2><p class="note">Noch keine '
                'zugeordneten Spiele. Die Quoten von football-data.co.uk (inkl. Pinnacle-Schlussquoten) werden mit dem '
                'nächsten Studien-Lauf geladen und den Polymarket-Märkten „Will X win on …?“ zugeordnet.</p></div>')
    better = "Pinnacle" if b["brier_book"] < b["brier_pm"] else "Polymarket"
    trs = "".join(f'<tr><td>{g["label"]}</td><td class="num">{g["n"]}</td><td class="num">{_de(g["price"] * 100)} %</td>'
                  f'<td class="num">{_de(g["rate"] * 100)} %</td><td class="num {"pos" if g["roi"] > 0 else "neg"}">'
                  f'{"+" if g["roi"] > 0 else ""}{_de(g["roi"] * 100, 0)} %</td></tr>' for g in b["groups"])
    return (f'<div class="card"><h2>Polymarket gegen Buchmacher (Fußball)</h2><p class="note">{b["n"]} Polymarket-Märkte '
            f'aus {b["leagues"]} Ligen, zugeordnet zu Spielen mit Pinnacle-Schlussquoten (Marge herausgerechnet). '
            f'<b>Genauigkeit</b> (Brier-Score, kleiner = besser): Polymarket 1 h vorher {_de(b["brier_pm"], 4)} · '
            f'Buchmacher {_de(b["brier_book"], 4)} → genauer: <b>{better}</b>. <b>Sharp-Line-Test:</b> Wenn beide '
            f'abweichen, die Seite auf Polymarket kaufen, die der Buchmacher höher einschätzt (inkl. Fee und '
            f'+{_de(b["slip"] * 100, 0)} Cent Aufschlag).</p><div class="tblwrap"><table><tr><th>Abweichung</th>'
            f'<th class="num">Wetten</th><th class="num">Ø Preis</th><th class="num">gewonnen</th><th class="num">ROI</th>'
            f'</tr>{trs}</table></div></div>')


def study_page(cal: dict, nav: list, stress: list | None = None, books: dict | None = None) -> str:
    from arb.study import CHECKPOINT_NAMES
    if not cal.get("n"):
        body = ('<p class="sub">Noch keine Daten. Der Sammler läuft alle 30 Minuten auf dem Server und holt '
                'aufgelöste Märkte mit Preisverlauf. Die ersten Ergebnisse erscheinen nach dem ersten Lauf.</p>')
        return _page("Markt-Studie: Wie gut sagen Preise den Ausgang voraus?", nav, body)
    rng = (datetime.fromtimestamp(cal["t0"]).strftime("%d.%m.%Y") + " – " +
           datetime.fromtimestamp(cal["t1"]).strftime("%d.%m.%Y")) if cal.get("t0") else ""
    body = [f'<p class="sub">{cal["n"]:,} aufgelöste Märkte · {rng}</p>'.replace(",", "."),
            '<div class="warnbox"><b>So liest du das:</b> Jede Zeile fasst alle Seiten zusammen, die zu diesem Preis '
            'gehandelt wurden. Liegt „tatsächlich gewonnen“ über dem Ø Preis, war der Kauf im Schnitt profitabel '
            '(vor Fees). <b>Grün/rot markiert</b> ist nur, wo der Unterschied statistisch klar ist (95-%-Bereich '
            'schließt den Preis aus). Jeder Markt zählt zweimal: als YES zum Preis p und als NO zu 1−p.</div>',
            '<div class="grid2"><div class="card"><h2>Kalibrierung</h2><p class="note">Punkte auf der gestrichelten '
            'Linie = Preis sagt den Ausgang richtig voraus. <span style="color:var(--s1)">●</span> 1 Tag vorher, '
            '<span style="color:var(--s2)">●</span> 1 h vorher.</p>' + _calib_svg(cal["rows"]) + '</div>']
    body.insert(2, _stress_card(stress or []))
    body.insert(3, _books_card(books))
    cats = cal.get("cats") or []
    rows = "".join(f'<tr><td>{html.escape(c["cat"])}</td><td>{c["group"]}</td><td class="num">{c["n"]}</td>'
                   f'<td class="num">{_de(c["price"] * 100)} %</td><td class="num">{_de(c["rate"] * 100)} %</td>'
                   f'<td class="num {_edge_cls(c)}">{"+" if c["edge"] >= 0 else ""}{_de(c["edge"] * 100)}</td></tr>'
                   for c in cats) or '<tr><td colspan="6">Noch zu wenige Märkte je Kategorie.</td></tr>'
    body.append('<div class="card"><h2>Nach Kategorie (1 Tag vorher)</h2><p class="note">Wo sind Favoriten oder '
                'Außenseiter falsch bepreist? Differenz in Prozentpunkten.</p><div class="tblwrap"><table><tr>'
                '<th>Kategorie</th><th>Gruppe</th><th class="num">n</th><th class="num">Ø Preis</th>'
                f'<th class="num">gewonnen</th><th class="num">Diff.</th></tr>{rows}</table></div></div></div>')
    for cp, name in CHECKPOINT_NAMES.items():
        rs = [r for r in cal["rows"] if r["cp"] == cp]
        if not rs:
            continue
        trs = "".join(f'<tr><td>{_de(r["lo"] * 100, 0)}–{_de(r["hi"] * 100, 0)} %</td><td class="num">{r["n"]}</td>'
                      f'<td class="num">{_de(r["price"] * 100)} %</td><td class="num">{_de(r["rate"] * 100)} %</td>'
                      f'<td class="num">{_de(r["ci_lo"] * 100)}–{_de(r["ci_hi"] * 100)} %</td>'
                      f'<td class="num {_edge_cls(r)}">{"+" if r["edge"] >= 0 else ""}{_de(r["edge"] * 100)}</td></tr>'
                      for r in rs)
        body.append(f'<div class="card"><h2>{name}</h2><div class="tblwrap"><table><tr><th>Preisbereich</th>'
                    '<th class="num">n</th><th class="num">Ø Preis</th><th class="num">tatsächlich gewonnen</th>'
                    f'<th class="num">95-%-Bereich</th><th class="num">Diff. (Pkt.)</th></tr>{trs}</table></div></div>')
    return _page("Markt-Studie: Wie gut sagen Preise den Ausgang voraus?", nav, "".join(body))


def wxobs_page(w: dict, nav: list) -> str:
    title = "Wetter-Messwerte: Was kostet ein Bucket, den die Station schon ausgeschlossen hat?"
    run = ""
    if w.get("last_run"):
        ts, proc, cand, priced = (w["last_run"].split("|") + ["0"] * 4)[:4]
        run = (f'Letzter Lauf {datetime.fromtimestamp(float(ts)).strftime("%d.%m. %H:%M")}: {proc} Märkte geprüft, '
               f'{cand} Kandidaten, {priced} mit Preisverlauf. ')
    reasons = ", ".join(f"{html.escape(str(r))}: {n:,}".replace(",", ".") for r, n in w.get("reasons") or [])
    total = f'{w.get("n", 0):,}'.replace(",", ".")
    status = f'<p class="note">{run}Insgesamt {total} Märkte geprüft' + \
             (f" – ohne Handel: {reasons}" if reasons else "") + ".</p>"
    intro = ('<div class="warnbox"><b>Idee:</b> Das Tagesmaximum kann nur steigen. Hat die Wetterstation schon 25 °C '
             'gemeldet, können „24 °C“ und darunter nicht mehr gewinnen – kosten aber oft noch ein paar Cent. Deren '
             'NO-Seite ist dann fast sicher. <b>Entscheidend ist die Fehlerquote</b>: Fälle, in denen ein laut Station '
             'unmöglicher Bucket trotzdem gewann (Rundung, andere Station, Korrekturen). <b>Rand</b> = so viele Grad '
             'muss die Messung über der Bucket-Grenze liegen. Gekauft wird NO zu 1 − YES-Preis + 1 Cent plus Gebühr, '
             '15 bzw. 60 Minuten nach der Messung.</div>')
    if not w.get("priced"):
        st = _wx_stations(w)
        return _page(title, nav, intro + '<p class="sub">Noch keine Ergebnisse. Die Studie läuft alle 30 Minuten auf '
                     'dem Server und arbeitet die Wetter-Märkte der Polymarket-Studie ab.</p>' + status + st)
    trs = ""
    for r in w["results"]:
        if not r.get("n"):
            trs += f'<tr><td>{r["margin"]}°</td><td>{r["delay"]} min</td><td class="num" colspan="8">keine Fälle</td></tr>'
            continue
        cls = "pos" if r["roi"] > 0 and r["errors"] == 0 else "neg" if r["roi"] < 0 else ""
        ecls = "neg" if r["errors"] else "pos"
        k = lambda v: f"{v:,}".replace(",", ".")  # noqa: E731 – thousands separator only
        trs += (f'<tr><td>{r["margin"]}°</td><td>{r["delay"]} min</td><td class="num">{k(r["n"])}</td>'
                f'<td class="num {ecls}">{r["errors"]} ({_de(r["errors"] / r["n"] * 100, 2)} %)</td>'
                f'<td class="num">{_de(r["yes"] * 100)} ct</td><td class="num">{k(r["ge2"])} / {k(r["ge5"])} / {k(r["ge10"])}</td>'
                f'<td class="num {cls}">{_de(r["roi"] * 100, 2)} %</td><td class="num">{_de(r["hours"], 1)} h</td></tr>')
    body = [intro, status, _wx_no_filter(w),
            '<div class="card"><h2>Ergebnis</h2><p class="note">Jede Zeile: alle Buckets, die laut Station unmöglich '
            'waren und danach noch mindestens 0,5 Cent kosteten. Rendite je Kauf (nicht pro Jahr) – das Geld ist bis '
            'zur Auflösung gebunden (Median-Haltedauer in der letzten Spalte).</p><div class="tblwrap"><table><tr>'
            '<th>Rand</th><th>Reaktion</th><th class="num">Käufe</th><th class="num">Fehler</th>'
            '<th class="num">Ø YES-Preis</th><th class="num">≥ 2 / 5 / 10 ct</th><th class="num">Rendite je Kauf</th>'
            f'<th class="num">Haltedauer</th></tr>{trs}</table></div></div>']
    crs = "".join(f'<tr><td>{html.escape(c["city"])}</td><td>{html.escape(c["station"] or "")}</td><td class="num">{c["n"]}</td>'
                  f'<td class="num {"neg" if c["errors"] else ""}">{c["errors"]}</td><td class="num">{_de(c["yes"] * 100)} ct</td></tr>'
                  for c in w["cities"])
    body.append('<div class="card"><h2>Nach Stadt (Rand 1°, 15 min)</h2><p class="note">Fehler gehäuft in einer Stadt = '
                'dort passt die Station oder die Rundung nicht. Solche Städte würde ein Live-Szenario auslassen.</p>'
                '<div class="tblwrap"><table><tr><th>Stadt</th><th>Station</th><th class="num">Käufe</th>'
                f'<th class="num">Fehler</th><th class="num">Ø YES-Preis</th></tr>{crs}</table></div></div>')
    body.append(_wx_stations(w))
    return _page(title, nav, "".join(body))


def _wx_no_filter(w: dict) -> str:
    nf = w.get("no_filter") or {}
    if not nf.get("n"):
        return ""
    def cells(r):
        if not r.get("n"):
            return '<td class="num" colspan="5">–</td>'
        n = f'{r["n"]:,}'.replace(",", ".")
        return (f'<td class="num">{n}</td><td class="num">{_de(r["price"] * 100)} %</td>'
                f'<td class="num">{_de(r["hit"] * 100)} %</td><td class="num {"pos" if r["roi"] > 0 else "neg"}">'
                f'{_de(r["roi"] * 100)} %</td><td class="num">{_de(r["first"] * 100)} / {_de(r["second"] * 100)} %</td>')
    head = ('<th class="num">Käufe</th><th class="num">Ø NO-Preis</th><th class="num">NO gewonnen</th>'
            '<th class="num">Rendite (+2 ct)</th><th class="num">1. / 2. Hälfte</th>')
    trs = "".join(f'<tr><td>{html.escape(r["cls"])}</td>{cells(r)}</tr>' for r in nf["by_cls"] if r.get("n"))
    frs = "".join(f'<tr><td>{html.escape(r["name"])}</td>{cells(r)}</tr>' for r in nf["filters"])
    hrs = "".join(f'<tr><td>{html.escape(r["cls"])}</td><td>{r["hour"]}</td>{cells(r)}</tr>' for r in nf["by_hour"])
    return ('<div class="card"><h2>Wetter-NO mit Messwerten: Wo stand die Station beim Kauf?</h2><p class="note">Alle '
            'Käufe der Regel Wetter-NO breit in der Studie (NO 55–97 %, Markt unter 5k $, 6 h vor Schluss), eingeteilt danach, '
            'wo das bis dahin gemessene Tagesmaximum lag (Messungen mindestens 15 min alt). Liegt es schon im Bucket, '
            'ist NO riskant; liegt es weit darunter oder ist der Bucket überschritten, sicherer.</p>'
            f'<div class="tblwrap"><table><tr><th>Stand der Station</th>{head}</tr>{trs}</table></div>'
            f'<h2 style="margin-top:14px">Filter-Varianten</h2><div class="tblwrap"><table><tr><th>Regel kauft …</th>{head}</tr>'
            f'{frs}</table></div><h2 style="margin-top:14px">Nach Ortszeit beim Kauf</h2><div class="tblwrap"><table>'
            f'<tr><th>Stand der Station</th><th>Ortszeit</th>{head}</tr>{hrs}</table></div></div>')


def _wx_stations(w: dict) -> str:
    rows = w.get("stations") or []
    if not rows:
        return ""
    trs = "".join(f'<tr><td>{html.escape(c or "")}</td><td>{html.escape(st or "–")}</td><td>{html.escape(tz or "–")}</td>'
                  f'<td>{html.escape(src or "–")}</td><td class="{"neg" if why else ""}">{html.escape(why or "ok")}</td></tr>'
                  for c, st, tz, src, why in rows)
    return ('<div class="card"><h2>Stationen</h2><p class="note">Aus dem Wunderground-Link in der Marktbeschreibung. '
            'Städte ohne Station (z. B. andere Quelle) werden ausgelassen.</p><div class="tblwrap"><table><tr><th>Stadt</th>'
            f'<th>Station</th><th>Zeitzone</th><th>Quelle</th><th>Status</th></tr>{trs}</table></div></div>')


def kalshi_page(k: dict, nav: list) -> str:
    title = "Kalshi-Studie: Gelten die Polymarket-Regeln auch dort?"
    run = ""
    if k.get("last_run"):
        ts, new, skipped, seen = (k["last_run"].split("|") + ["0"] * 4)[:4]
        run = (f'Letzter Lauf {datetime.fromtimestamp(float(ts)).strftime("%d.%m. %H:%M")}: {seen} Märkte gesehen, '
               f'{new} neu, {skipped} übersprungen.')
    skipped = ", ".join(f"{html.escape(str(r))}: {n}" for r, n in k.get("skipped") or [])
    status = (f'<p class="note">{run}{" Übersprungen nach Grund – " + skipped if skipped else ""}</p>')
    if not k.get("n"):
        return _page(title, nav, '<p class="sub">Noch keine Daten. Der Sammler läuft alle 30 Minuten auf dem Server '
                     '(Kalshi-Marktdaten sind öffentlich, kein Konto nötig).</p>' + status)
    span = (datetime.fromtimestamp(k["span"][0]).strftime("%d.%m.%Y") + " – " +
            datetime.fromtimestamp(k["span"][1]).strftime("%d.%m.%Y"))
    body = [f'<p class="sub">{k["n"]:,} aufgelöste Kalshi-Märkte · {span}</p>'.replace(",", "."), status,
            '<div class="warnbox"><b>Der Unterschied zur Polymarket-Studie:</b> Kalshi liefert je Stunde das beste '
            'Angebot (Ask). Gerechnet wird deshalb zum <b>echten Kaufpreis</b> plus Kalshi-Gebühr (7 % · p · (1−p)), '
            'nicht zum letzten Handelspreis. Wo eine Stunde kein Ask hatte, gilt Preis + 2 Cent. Krypto wird nicht '
            'gesammelt (Tausende Stundenmärkte, auf Polymarket ohne Edge). Volumen zählt Kontrakte (je 1 $).</div>']
    def res_cells(r):
        if not r:
            return '<td class="num" colspan="8">zu wenige Wetten</td>'
        cls = "pos" if r["p95"] > 0 and r["p_loss"] <= 0.1 else "neg" if r["p_loss"] >= 0.9 else ""
        return (f'<td class="num">{r["n"]}</td><td class="num">{r["events"]}</td>'
                f'<td class="num">{_de(r["price"] * 100)} % / {_de(r["buy"] * 100)} %</td>'
                f'<td class="num">{_de(r["hit"] * 100)} %</td><td class="num {cls}">{_de(r["roi"] * 100)} %</td>'
                f'<td class="num">{_de(r["roi_1ct"] * 100)} %</td><td class="num">{_de(r["p_loss"] * 100, 0)} %</td>'
                f'<td class="num">{_de(r["first"] * 100)} / {_de(r["second"] * 100)} %</td>')
    head = ('<th class="num">Wetten</th><th class="num">Events</th><th class="num">Ø Preis / Ø Kauf</th>'
            '<th class="num">gewonnen</th><th class="num">Rendite</th><th class="num">+1 Cent</th>'
            '<th class="num">P(Verlust)</th><th class="num">1. / 2. Hälfte</th>')
    rules = "".join(f'<tr><td>{html.escape(r["name"])}</td>{res_cells(r["res"])}</tr>' for r in k["rules"])
    body.append('<div class="card"><h2>Die Polymarket-Regeln auf Kalshi</h2><p class="note">Festgeschrieben wie auf '
                'Polymarket, nicht für Kalshi nachjustiert. Rendite nach Gebühr zum echten Ask; P(Verlust) per '
                'Bootstrap über Events. Grün nur, wenn klar positiv.</p><div class="tblwrap"><table><tr><th>Regel</th>'
                f'{head}</tr>{rules}</table></div></div>')
    cats = "".join(f'<tr><td>{html.escape(c)}</td><td class="num">{v["n"]:,}</td><td class="num">{v["vol"]:,.0f}</td></tr>'
                   .replace(",", ".") for c, v in sorted(k["cats"].items(), key=lambda x: -x[1]["n"]))
    body.append('<div class="card"><h2>Gesammelt nach Kategorie</h2><div class="tblwrap"><table><tr><th>Kategorie</th>'
                f'<th class="num">Märkte</th><th class="num">Volumen (Kontrakte)</th></tr>{cats}</table></div></div>')
    grid = "".join(f'<tr><td>{html.escape(g["cat"])}</td><td>{g["band"]}</td>{res_cells(g["res"])}</tr>' for g in k["grid"])
    body.append('<div class="card"><h2>Edge-Karte (6 h vorher, beide Seiten)</h2><p class="note">Jede Seite, die 6 h vor '
                'Schluss in diesem Preisbereich lag, gekauft zum echten Ask. Viele Zellen = viele Tests: einzelne '
                'grüne Felder können Zufall sein. Ernst zu nehmen ist, was auch in beiden Hälften positiv ist.</p>'
                f'<div class="tblwrap"><table><tr><th>Kategorie</th><th>Preis</th>{head}</tr>'
                f'{grid or "<tr><td colspan=10>Noch zu wenige Daten.</td></tr>"}</table></div></div>')
    return _page(title, nav, "".join(body))


def _edge_cls(r: dict) -> str:
    return "pos" if r["ci_lo"] > r["price"] else "neg" if r["ci_hi"] < r["price"] else ""


STUDY_MARKET_HEADER = ["Ende geplant", "Geschlossen", "Frage", "Kategorie", "Multi-Outcome", "Volumen $", "Ergebnis",
                       "YES 7 Tage vorher", "YES 1 Tag vorher", "YES 6 h vorher", "YES 1 h vorher", "Preispunkte",
                       "Markt-ID", "Stichprobe (0-99)", "Preis unverändert seit h (7 Tage)", "… (1 Tag)", "… (6 h)",
                       "… (1 h)", "Version"]


def _cols(db, table: str, cols: list) -> str:
    """SELECT list that reads NULL for columns an older database does not have yet."""
    have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
    return ", ".join(c if c in have else f"NULL AS {c}" for c in cols)


def study_market_rows(study_db: str):
    """Resolved markets of the study, one CSV row at a time (the table has well over 100k rows)."""
    if not Path(study_db).exists():
        return
    db = sqlite3.connect(study_db)
    try:
        cols = _cols(db, "markets", ["end_ts", "close_ts", "question", "category", "neg_risk", "volume", "outcome",
                                     "p_7d", "p_1d", "p_6h", "p_1h", "n_points", "condition_id", "sample",
                                     "a_7d", "a_1d", "a_6h", "a_1h", "v"])
        cur = db.execute(f"SELECT {cols} FROM markets ORDER BY end_ts")
        for r in cur:
            yield [datetime.fromtimestamp(r[0]).strftime("%Y-%m-%d %H:%M") if r[0] else "",
                   datetime.fromtimestamp(r[1]).strftime("%Y-%m-%d %H:%M") if r[1] else "",
                   r[2], r[3], "ja" if r[4] else "nein", float(r[5] or 0), "YES" if r[6] else "NO",
                   *r[7:11], r[11], r[12], r[13], *r[14:18], r[18] or 1]
    except sqlite3.OperationalError:
        pass
    finally:
        db.close()


# the raw study tables grow without bound; the compact ZIP leaves them out so it stays small enough to upload
RAW_ARCS = {"studie/maerkte.csv", "studie/buchmacher.csv", "kalshi/maerkte.csv", "wetter-messwerte/maerkte.csv"}
COMPACT_MAX_BYTES = 5_000_000


def compact_files(files: list) -> list:
    return [f for f in files if f[4] not in RAW_ARCS
            and not (f[3].exists() and f[3].stat().st_size > COMPACT_MAX_BYTES)]


def _zip(path: Path, files: list) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for *_, disk, arc in files:
            if disk.exists():
                z.write(disk, arc)
    os.replace(tmp, path)


STUDY_ZIPS = {"Studie": "studie", "Wetter-Messwerte": "wetter-messwerte", "Kalshi": "kalshi"}
UPLOAD_LIMIT = 25_000_000  # chat uploads stop at 30 MB


def _zip_split(exp: Path, slug: str, files: list, limit: int = UPLOAD_LIMIT) -> list:
    """One ZIP per study; if it gets too big for an upload, the CSVs are cut into row chunks
    (header repeated) and spread over <slug>-teil1.zip, -teil2.zip, ... each below the limit."""
    for old in exp.glob(f"{slug}-teil*.zip"):
        old.unlink()
    path = exp / f"{slug}.zip"
    _zip(path, files)
    if path.stat().st_size <= limit:
        return [path]
    raw = sum(f[3].stat().st_size for f in files if f[3].exists()) or 1
    target = max(1, int(limit * 0.8 * raw / path.stat().st_size))  # raw bytes per part at the observed ratio
    parts, cur, size = [], [], 0

    def flush():
        nonlocal cur, size
        if cur:
            part = exp / f"{slug}-teil{len(parts) + 1}.zip"
            tmp = part.with_name(part.name + ".tmp")
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
                for arc, data in cur:
                    z.writestr(arc, data)
            os.replace(tmp, part)
            parts.append(part)
        cur, size = [], 0

    for *_, disk, arc in files:
        if not disk.exists():
            continue
        with open(disk, "rb") as fh:
            header = fh.readline()
            chunk, n, k = [header], len(header), 0
            for line in fh:
                if size + n + len(line) > target and len(chunk) > 1:
                    k += 1
                    cur.append((arc.replace(".csv", f"-{k}.csv"), b"".join(chunk)))
                    flush()
                    chunk, n = [header], len(header)
                chunk.append(line)
                n += len(line)
            name = arc if k == 0 else arc.replace(".csv", f"-{k + 1}.csv")
            cur.append((name, b"".join(chunk)))
            size += n
    flush()
    path.unlink()
    return parts


def export_page(files: list, zip_path: Path, nav: list, study_zips: dict | None = None) -> str:
    def meta(p: Path) -> str:
        if not p.exists():
            return "noch keine Daten"
        rows = max(0, p.read_bytes().count(b"\n") - 1) if p.suffix == ".csv" else None
        size = p.stat().st_size
        sz = f"{size / 1e6:.1f} MB" if size > 1e6 else f"{size / 1e3:.0f} KB"
        return (f"{rows:,} Zeilen · ".replace(",", ".") if rows is not None else "") + sz
    groups: dict = {}
    for group, label, href, disk, _ in files:
        groups.setdefault(group, []).append(
            f'<div class="dl"><div><div>{html.escape(label)}</div><div class="m">{meta(disk)}</div></div>'
            f'<a class="btn" href="{href}" download>CSV</a></div>')
    body = [f'<p class="sub">Stand {datetime.now().strftime("%d.%m.%Y %H:%M")} · wird alle 10 Minuten neu erzeugt. '
            'Alle CSVs im Format für deutsches Excel (Semikolon, Dezimalkomma).</p>',
            f'<div class="card"><div class="dl"><div><div><b>Kompakt (zum Hochladen)</b></div><div class="m">Szenarien, '
            f'Zusammenfassungen und Diagnosen, ohne die großen Rohdaten der Studien · '
            f'{meta(zip_path.with_name("polyarb-kompakt.zip"))}</div></div>'
            '<a class="btn" href="polyarb-kompakt.zip" download>ZIP</a></div>'
            f'<div class="dl"><div><div><b>Alles auf einmal</b></div><div class="m">ZIP mit allen '
            f'Dateien unten · {meta(zip_path)}</div></div><a class="btn" href="polyarb-export.zip" download>ZIP</a>'
            '</div></div>']
    if study_zips:
        rows = []
        for g, paths in study_zips.items():
            for i, p in enumerate(paths):
                name = g + (f" · Teil {i + 1} von {len(paths)}" if len(paths) > 1 else "")
                rows.append(f'<div class="dl"><div><div>{html.escape(name)}</div><div class="m">{meta(p)}</div></div>'
                            f'<a class="btn" href="{p.name}" download>ZIP</a></div>')
        body.append('<div class="card"><h2>Studien einzeln</h2><div class="m">Jede Studie als eigenes ZIP, '
                    'große werden in Teile unter 25 MB geschnitten.</div>' + "".join(rows) + '</div>')
    body += [f'<div class="card"><h2>{html.escape(g)}</h2>{"".join(items)}</div>' for g, items in groups.items()]
    return _page("Export", nav, "".join(body))


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
<p class="note" id="scan"></p>
<div id="halt"></div>
<div class="card" id="allcard" style="display:none"><h2>Alle Strategien</h2><p class="note">Equity zählt offene Positionen zum aktuellen Bid mit. <b>Realisiert</b> zählt nur, was aufgelöst (ausgezahlt oder verloren) ist – das ist der tatsächlich erzielte Gewinn. Startkapital je 2.500 $.</p><div class="tblwrap"><table id="alltbl"></table></div></div>
<div class="kpis" id="kpis"></div>
<div class="card" id="capcard" style="display:none"><h2>Kapazität: Wie viel Geld hätte der Markt genommen?</h2><p class="note">Je Markttag: alle Gelegenheiten, die die Regel erfüllten (auch wenn das Budget- oder Tageslimit den Kauf verhindert hat), und wie viel Geld zu dem Zeitpunkt im Orderbuch lag. <b>Bis Kaufgrenze</b> = was die Strategie wirklich kaufen würde (bester Ask + max_slippage des Szenarios, meist 1 Cent). Gezählt wird je Markt das Maximum über alle Scans.</p><div class="tblwrap"><table id="captbl"></table></div></div>
<div class="card"><h2>Equity</h2><p class="note" id="eqnote">Gesamtwert = Cash + gebundene Körbe (zu Kosten) + offene Reste (zum Bid). Startkapital als Referenzlinie.</p><div class="legend"><span><i class="sw" style="background:var(--s1)"></i>Equity (inkl. offener Positionen)</span><span><i class="sw" style="background:var(--s3)"></i>Vermögen realisiert (Start + realisierter PnL)</span></div><div id="eq"></div></div>
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
if(D.kind!=="arb"&&D.kind!=="ladder"){$("eqnote").textContent="Gesamtwert = Cash + offene Positionen (zum Bid). Startkapital als Referenzlinie.";
 $("pnlnote").textContent="Erwartet = Gewinn, wenn jede Position so ausgeht, wie die Strategie annimmt. Realisiert zählt erst bei Auflösung."}
$("nav").innerHTML=D.nav.length>1?D.nav.map(n=>`<a class="tab${n.active?" on":""}" href="${n.href}"${n.real==null?"":` title="realisiert ${(n.real>=0?"+":"")+fmt(n.real*100,1)} %"`}>${n.label}${n.ret==null?"":`<span class="${n.ret>0.00005?"pos":n.ret<-0.00005?"neg":""}">${(n.ret>=0?"+":"")+fmt(n.ret*100,1)} %</span>`}</a>`).join(""):"";
function showTip(e,html){tip.innerHTML=html;tip.style.display="block";const x=Math.min(e.clientX+14,innerWidth-tip.offsetWidth-8);tip.style.left=x+"px";tip.style.top=(e.clientY+14)+"px"}
function hideTip(){tip.style.display="none"}
const NS="http://www.w3.org/2000/svg";
function el(tag,attrs,parent){const n=document.createElementNS(NS,tag);for(const k in attrs)n.setAttribute(k,attrs[k]);parent&&parent.appendChild(n);return n}
function ticks(min,max,n=4){const span=max-min||1,step=Math.pow(10,Math.floor(Math.log10(span/n)));const m=[1,2,2.5,5,10].find(m=>span/(step*m)<=n)*step;const out=[];for(let v=Math.ceil(min/m)*m;v<=max+1e-9;v+=m)out.push(+v.toFixed(10));return out}

$("src").textContent=D.source==="mock"?"SYNTHETISCHE DATEN (Mock)":"LIVE-Orderbücher · Paper";
const K=D.kpi;
$("range").textContent=K.t0?`${dt(K.t0)} – ${dt(K.t1)} · ${K.scans.toLocaleString("de-AT")} Scans · ${K.baskets} Märkte/Körbe · Median-Scan ${fmt(K.med_scan_ms,0)} ms`:"Noch keine Daten";
if(D.scan&&Object.keys(D.scan).length){const S={...D.scan};const t=S.ts;delete S.ts;
 $("scan").innerHTML=`<b>Letzter Scan${t?" "+dt(t):""}:</b> `+Object.entries(S).map(([k,v])=>`${k} ${v}`).join(" · ")}
if(K.halted)$("halt").innerHTML=`<div class="warnbox"><b>Handel pausiert:</b> ${K.halted}</div>`;
if(D.source==="mock")$("halt").innerHTML+=`<div class="warnbox">Diese Zahlen stammen aus dem <b>synthetischen Mock-Markt</b> und testen nur die Pipeline. Sie sagen nichts über reale Profitabilität aus.</div>`;
const tiles=[["Equity",usd(K.equity),`Start ${usd(D.start)} · inkl. offener Positionen`],["Rendite",pct(K.ret),`Max. Drawdown ${pct(K.mdd)}`],
 ["Vermögen realisiert",usd(D.start+K.real),"Start + nur aufgelöste Gewinne/Verluste"],["Rendite realisiert",pct(K.real_ret),`PnL realisiert ${usd(K.real)}`],
 ["PnL",usd(K.realized),`gebunden ${usd(K.locked)} · Reste ${usd(K.residual)}`],["Trefferquote",pct(K.hit),`${K.attempts} Ausführungsversuche`],
 ["Capture",pct(K.capture),"realisiert / erwartet"],["Chancen erkannt",K.opps.toLocaleString("de-AT"),"nach Fees & Mindest-Edge"]];
if(D.kind!=="arb"&&D.kind!=="ladder"){tiles[4][2]=`offene Positionen ${usd(K.locked)} (zu Kosten)`;tiles[6]=["Offen",D.calib.open[0],`gebunden ${usd(D.calib.open[1])}`];tiles[7][2]="Preis unter der Strategie-Grenze"}
if(D.capacity&&D.capacity.length){const C=D.capacity,avg=f=>C.reduce((a,c)=>a+c[f],0)/C.length;
 $("captbl").innerHTML=`<tr><th>Markttag</th><th class="num">Gelegenheiten</th><th class="num">gekauft</th><th class="num">im Buch bis Kaufgrenze</th><th class="num">bis Ask + 2 ct</th><th class="num">bis Regelgrenze</th></tr>`+
  C.map(c=>`<tr><td>${c.day}</td><td class="num">${c.n}</td><td class="num">${usd(c.bought)}</td><td class="num">${usd(c.slip)}</td><td class="num">${usd(c.c2)}</td><td class="num">${usd(c.band)}</td></tr>`).join("")+
  `<tr style="font-weight:600"><td>Ø pro Tag</td><td class="num">${fmt(avg("n"),0)}</td><td class="num">${usd(avg("bought"))}</td><td class="num">${usd(avg("slip"))}</td><td class="num">${usd(avg("c2"))}</td><td class="num">${usd(avg("band"))}</td></tr>`;
 $("capcard").style.display=""}
if(D.overview&&D.overview.length>1){const O=D.overview,sg=v=>v>0.00005?"pos":v<-0.00005?"neg":"";
 const tot=O.reduce((a,o)=>({start:a.start+o.start,equity:a.equity+o.equity,real:a.real+o.real,n:a.n+o.n,won:a.won+o.won,open:a.open+o.open}),{start:0,equity:0,real:0,n:0,won:0,open:0});
 const row=(o,b)=>`<tr${b?' style="font-weight:600"':""}><td>${o.href?`<a href="${o.href}">${o.label}</a>`:o.label}</td><td class="num">${usd(o.equity)}</td><td class="num ${sg(o.equity/o.start-1)}">${pct(o.equity/o.start-1)}</td><td class="num">${usd(o.start+o.real)}</td><td class="num ${sg(o.real)}">${usd(o.real)}</td><td class="num ${sg(o.real)}">${pct(o.real/o.start)}</td><td class="num">${o.won} / ${o.n}</td><td class="num">${o.open}</td></tr>`;
 $("alltbl").innerHTML=`<tr><th>Strategie</th><th class="num">Equity</th><th class="num">Rendite</th><th class="num">Vermögen realisiert</th><th class="num">PnL realisiert</th><th class="num">Rendite realisiert</th><th class="num">Gewonnen / aufgelöst</th><th class="num">Offen</th></tr>`+
  [...O].sort((a,b)=>b.real/b.start-a.real/a.start).map(o=>row(o)).join("")+row(Object.assign(tot,{label:"Gesamt"}),true);
 $("allcard").style.display=""}
$("kpis").innerHTML=tiles.map(([l,v,d])=>`<div class="kpi"><div class="l">${l}</div><div class="v">${v}</div><div class="d">${d}</div></div>`).join("");

// ---------- equity line
(function(){
 const box=$("eq"),W=1100,H=260,m={l:56,r:12,t:10,b:26};const c=D.curve;if(c.length<2){box.textContent="Zu wenig Daten.";return}
 const svg=el("svg",{viewBox:`0 0 ${W} ${H}`,role:"img","aria-label":"Equity-Verlauf"},box);
 const t0=c[0].t,t1=c[c.length-1].t,ys=c.map(p=>p.e).concat(c.map(p=>p.r??p.e),[D.start]);let y0=Math.min(...ys),y1=Math.max(...ys);const pad=(y1-y0)*0.08||5;y0-=pad;y1+=pad;
 const X=t=>m.l+(t-t0)/(t1-t0||1)*(W-m.l-m.r),Y=v=>m.t+(1-(v-y0)/(y1-y0))*(H-m.t-m.b);
 for(const v of ticks(y0,y1)){el("line",{x1:m.l,x2:W-m.r,y1:Y(v),y2:Y(v),stroke:"var(--grid)"},svg);el("text",{x:m.l-8,y:Y(v)+4,"text-anchor":"end"},svg).textContent="$"+fmt(v,0)}
 const nx=6;for(let i=0;i<=nx;i++){const t=t0+(t1-t0)*i/nx;el("text",{x:X(t),y:H-6,"text-anchor":i==0?"start":i==nx?"end":"middle"},svg).textContent=dt(t)}
 el("line",{x1:m.l,x2:W-m.r,y1:Y(D.start),y2:Y(D.start),stroke:"var(--muted)","stroke-dasharray":"4 4"},svg);
 el("path",{d:c.map((p,i)=>(i?"L":"M")+X(p.t).toFixed(1)+","+Y(p.e).toFixed(1)).join(""),fill:"none",stroke:"var(--s1)","stroke-width":2,"stroke-linejoin":"round"},svg);
 el("path",{d:c.map((p,i)=>(i?"L":"M")+X(p.t).toFixed(1)+","+Y(p.r??p.e).toFixed(1)).join(""),fill:"none",stroke:"var(--s3)","stroke-width":2,"stroke-dasharray":"6 4","stroke-linejoin":"round"},svg);
 const cross=el("line",{y1:m.t,y2:H-m.b,stroke:"var(--muted)",visibility:"hidden"},svg),dot=el("circle",{r:4.5,fill:"var(--s1)",stroke:"var(--surface)","stroke-width":2,visibility:"hidden"},svg);
 const hit=el("rect",{x:m.l,y:m.t,width:W-m.l-m.r,height:H-m.t-m.b,fill:"transparent"},svg);
 hit.addEventListener("mousemove",e=>{const r=svg.getBoundingClientRect(),tx=t0+((e.clientX-r.left)*W/r.width-m.l)/(W-m.l-m.r)*(t1-t0);let b=c[0];for(const p of c)if(Math.abs(p.t-tx)<Math.abs(b.t-tx))b=p;
  cross.setAttribute("x1",X(b.t));cross.setAttribute("x2",X(b.t));dot.setAttribute("cx",X(b.t));dot.setAttribute("cy",Y(b.e));cross.setAttribute("visibility","visible");dot.setAttribute("visibility","visible");
  showTip(e,`<b>${dt(b.t)}</b><br>Equity ${usd(b.e)}<br>Vermögen realisiert ${usd(b.r??b.e)}<br>davon gebunden ${usd(b.l)}`)});
 hit.addEventListener("mouseleave",()=>{hideTip();cross.setAttribute("visibility","hidden");dot.setAttribute("visibility","hidden")});
})();

const NAMES={ladder_buy_all:"Logische Arbitrage",underdog_buy:"Underdog-Sport",favorite_buy:"Favorit-Kleinmarkt",weather_no_buy:"Wetter-NO",binary_buy_all:"Binär: YES+NO kaufen",binary_sell_all:"Binär: Split & verkaufen",negrisk_buy_all:"Multi-Outcome-Korb",negrisk_no_buy_all:"Multi-Outcome: alle NO",
 endgame_buy:"Endspiel-Ernte",longshot_buy:"Longshot: NO kaufen",weather_buy:"Wetter-Modell",fussball_dog_buy:"Fußball-Außenseiter",
 wetter_no_breit_buy:"Wetter-NO breit",wetter_no_mess_buy:"Wetter-NO + Messwerte",wetter_no_streng_buy:"Wetter-NO streng",finanz_dog_buy:"Finanz-Außenseiter",mlb_spread_buy:"MLB-Spread"};
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

// ---------- scenarios: calibration instead of the arbitrage histogram (ladders are arbitrage: neither)
if(D.kind==="ladder")$("histcard").style.display="none";
else if(D.kind!=="arb"){const C=D.calib;$("histcard").innerHTML=`<h2>Hat die Strategie einen Edge?</h2>
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
