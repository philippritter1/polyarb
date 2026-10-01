"""Weather study with real observations: how much did a temperature bucket still cost once the
station had already made it impossible?

The day's high can only go up. Once the station has reported, say, 25 °C, the buckets "24 °C" and
"23 °C or below" cannot win any more – the market may still price them at a few cents, because
most traders watch forecasts, not the station. Buying their NO side is then (almost) riskless.
"Almost": Polymarket resolves on the Wunderground page of one station (rounding, late corrections),
so the study also counts every case where an "impossible" bucket still won. That error rate
decides whether this is an edge at all.

Per market of the Polymarket study (data/study.sqlite, category Wetter):
  1. station  – the ICAO code in the Wunderground link of the market description (once per city),
                time zone from the Open-Meteo geocoder
  2. readings – METAR archive of the Iowa Environmental Mesonet (free), cached per station and day
  3. dead     – first reading of the local day that puts the running max (min) beyond the bucket by
                more than `margin` degrees (0, 1, 2 are all evaluated)
  4. price    – YES price from the CLOB history 15 and 60 minutes after that reading
Only buckets that still cost >= 2 cents at one of the study checkpoints get the price history.

  python run.py wxobs        # one run (every 30 min via polyarb-wxobs.timer), continues where it stopped
"""
from __future__ import annotations

import csv
import io
import logging
import math
import re
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .weather import GEOCODE_URL, MONTHS, parse_bucket

log = logging.getLogger(__name__)

IEM_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
MARGINS = (0, 1, 2)
DELAYS = (15, 60)  # minutes after the reading: METAR publication plus our reaction
FEE_RATE = 0.05
QUESTION_RE = re.compile(r"will the (highest|lowest) temperature in (.+?) be (.+?) on ([a-z]+) (\d{1,2})(?:,? (\d{4}))?\s*\??$", re.I)
STATION_RE = re.compile(r"wunderground\.com/history/(?:daily|hourly)/[^\s\"')]*?/([A-Z0-9]{4})(?=[/?\s\"').,]|$)")

SCHEMA = """
CREATE TABLE IF NOT EXISTS wx_city(city TEXT PRIMARY KEY, station TEXT, tz TEXT, source TEXT, reason TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS wx_obs(station TEXT, ts REAL, tmpf REAL, PRIMARY KEY(station, ts));
CREATE TABLE IF NOT EXISTS wx_obs_day(station TEXT, day TEXT, PRIMARY KEY(station, day));
CREATE TABLE IF NOT EXISTS wx_markets(
  condition_id TEXT PRIMARY KEY, question TEXT, city TEXT, station TEXT, kind TEXT, lo REAL, hi REAL, unit TEXT,
  local_date TEXT, close_ts REAL, outcome INT, p_max REAL,
  dead0_ts REAL, dead1_ts REAL, dead2_ts REAL,
  yes0_15 REAL, yes0_60 REAL, yes1_15 REAL, yes1_60 REAL, yes2_15 REAL, yes2_60 REAL,
  reason TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS wx_meta(key TEXT PRIMARY KEY, value TEXT);
"""


def parse_question(q: str, year: int) -> Optional[Tuple[str, str, Tuple[float, float, str], date]]:
    """'Will the highest temperature in Paris be 24°C on September 3?' -> ('max', 'Paris', (24, 24, 'c'), date)."""
    m = QUESTION_RE.match((q or "").strip())
    if not m or m.group(4).lower() not in MONTHS:
        return None
    b = parse_bucket(m.group(3))
    if not b:
        return None
    try:
        d = date(int(m.group(6) or year), MONTHS[m.group(4).lower()], int(m.group(5)))
    except ValueError:
        return None
    return ("max" if m.group(1).lower() == "highest" else "min"), m.group(2).strip(), b, d


def station_from(text: str) -> Optional[str]:
    m = STATION_RE.search(text or "")
    return m.group(1).upper() if m else None


def to_unit(tmpf: float, unit: str) -> int:
    """A reading as the whole degree a station page shows (half up)."""
    v = tmpf if unit == "f" else (tmpf - 32) * 5 / 9
    return int(math.floor(v + 0.5 + 1e-9))  # 76.1 °F is 24.5 °C, not 24.4999...


def dead_time(readings: List[Tuple[float, float]], kind: str, lo: float, hi: float, unit: str,
              margin: int) -> Optional[float]:
    """First reading (of the local day, sorted) after which the bucket [lo, hi] can no longer win."""
    ext = None
    for ts, tmpf in readings:
        v = to_unit(tmpf, unit)
        ext = v if ext is None else (max(ext, v) if kind == "max" else min(ext, v))
        if kind == "max" and hi != math.inf and ext > hi + margin:
            return ts
        if kind == "min" and lo != -math.inf and ext < lo - margin:
            return ts
    return None


def price_after(hist: List[dict], ts: float, end: float) -> Optional[float]:
    """First traded price at or after ts (and before the market closed)."""
    for h in hist:
        t = float(h.get("t") or 0)
        if ts <= t <= end:
            return float(h["p"])
    return None


class WxObsStudy:
    def __init__(self, client, db_path: str, study_db: str, fetch_text: Optional[Callable[[str, dict], str]] = None):
        self.client = client
        self.study_db = study_db
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path)
        self.db.executescript(SCHEMA)
        self.fetch_text = fetch_text or _fetch_text

    # ------------------------------------------------------------------ helpers
    def _json(self, url: str, params: dict):
        try:
            return self.client.get_json(url, params)
        except Exception as e:  # noqa – recorded as a reason by the caller
            log.debug("wxobs %s: %s", url, e)
            return None

    def _gamma(self, cids: List[str]) -> Dict[str, dict]:
        out = {}
        for i in range(0, len(cids), 20):
            data = self._json(f"{self.client.gamma}/markets",
                              {"condition_ids": cids[i:i + 20], "closed": "true", "limit": 50})
            for m in data or []:
                out[str(m.get("conditionId"))] = m
        return out

    def _city(self, city: str, cid: str) -> Optional[Tuple[str, str]]:
        row = self.db.execute("SELECT station, tz FROM wx_city WHERE city=?", (city,)).fetchone()
        if row:
            return (row[0], row[1]) if row[0] and row[1] else None
        m = self._gamma([cid]).get(cid) or {}
        text = f"{m.get('description') or ''} {m.get('resolutionSource') or ''}"
        station = station_from(text)
        src = re.search(r"https?://([^/\s]+)", text)
        geo = (self._json(GEOCODE_URL, {"name": re.sub(r"\s*\(.*?\)", "", city), "count": 1, "language": "en"}) or {})
        res = geo.get("results") or []
        tz = res[0].get("timezone") if res else None
        reason = None if station and tz else ("keine Wunderground-Station" if not station else "Zeitzone unbekannt")
        if not m:
            reason = "Markt nicht gefunden"
        self.db.execute("INSERT OR REPLACE INTO wx_city VALUES(?,?,?,?,?,?)",
                        (city, station, tz, src.group(1) if src else None, reason, time.time()))
        self.db.commit()
        return (station, tz) if not reason else None

    def _readings(self, station: str, day: date, tz: str) -> List[Tuple[float, float]]:
        """Readings of the local calendar day `day` at the station, fetched once per station and day."""
        from zoneinfo import ZoneInfo
        z = ZoneInfo(tz)
        start = datetime(day.year, day.month, day.day, tzinfo=z).timestamp()
        end = (datetime(day.year, day.month, day.day, tzinfo=z) + timedelta(days=1)).timestamp()
        if not self.db.execute("SELECT 1 FROM wx_obs_day WHERE station=? AND day=?", (station, day.isoformat())).fetchone():
            self._fetch_obs(station, day)
        return self.db.execute("SELECT ts, tmpf FROM wx_obs WHERE station=? AND ts>=? AND ts<? ORDER BY ts",
                               (station, start, end)).fetchall()

    def _fetch_obs(self, station: str, day: date) -> None:
        """One IEM request covers up to a month of UTC days from the day before (time zones up to +-14 h).
        Days are only marked as fetched once the next UTC day is in the request and fully past."""
        today = datetime.now(timezone.utc).date()
        a = day - timedelta(days=1)
        b = max(day + timedelta(days=2), min(day + timedelta(days=30), today))
        rows = []
        for sid in dict.fromkeys([station, station[1:] if len(station) == 4 and station[0] == "K" else station]):
            params = {"station": sid, "data": "tmpf", "year1": a.year, "month1": a.month, "day1": a.day,
                      "year2": b.year, "month2": b.month, "day2": b.day, "tz": "Etc/UTC", "format": "onlycomma",
                      "latlon": "no", "missing": "M", "trace": "T", "direct": "no", "report_type": [3, 4]}
            try:
                text = self.fetch_text(IEM_URL, params)
            except Exception as e:  # noqa
                log.warning("wxobs: IEM %s failed: %s", sid, e)
                return  # not marked as fetched: try again next run
            for r in csv.DictReader(io.StringIO(text or "")):
                try:
                    ts = datetime.strptime(r["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp()
                    rows.append((station, ts, float(r["tmpf"])))
                except (KeyError, ValueError, TypeError):
                    continue
            if rows:
                break
        self.db.executemany("INSERT OR IGNORE INTO wx_obs VALUES(?,?,?)", rows)
        d = day
        while d + timedelta(days=1) < b and d < today - timedelta(days=1):
            self.db.execute("INSERT OR IGNORE INTO wx_obs_day VALUES(?,?)", (station, d.isoformat()))
            d += timedelta(days=1)
        self.db.commit()

    def _history(self, token: str, start: float, end: float) -> List[dict]:
        for fidelity in (10, 60):
            data = self._json(f"{self.client.clob}/prices-history",
                              {"market": token, "startTs": int(start), "endTs": int(end), "fidelity": fidelity})
            hist = sorted((data or {}).get("history") or [], key=lambda h: h.get("t", 0))
            if hist:
                return hist
        return []

    # ------------------------------------------------------------------ run
    def collect(self, max_markets: int = 3000, min_price: float = 0.02, now: Optional[float] = None) -> Dict[str, int]:
        now = time.time() if now is None else now
        if not Path(self.study_db).exists():
            return {"error": "keine Studie"}
        sdb = sqlite3.connect(self.study_db)
        try:
            rows = sdb.execute("""SELECT condition_id, question, end_ts, close_ts, outcome, p_1d, p_6h, p_1h
                                  FROM markets WHERE category='Wetter' ORDER BY COALESCE(close_ts, end_ts) DESC""").fetchall()
        finally:
            sdb.close()
        done = {r[0] for r in self.db.execute("SELECT condition_id FROM wx_markets")}
        stats = {"processed": 0, "candidates": 0, "dead": 0, "priced": 0, "skipped": 0}
        todo = []
        for cid, q, end, close, out, *ps in rows:
            if cid in done:
                continue
            if len(todo) >= max_markets:
                break
            todo.append((cid, q, end, close, out, ps))
        cand = []
        for cid, q, end, close, out, ps in todo:
            stats["processed"] += 1
            year = datetime.fromtimestamp(end or close or now, tz=timezone.utc).year
            p = parse_question(q, year)
            base = [cid, q, None, None, None, None, None, None, None, close, out, max([x for x in ps if x is not None], default=None)]
            if not p:
                self._store(base, [None] * 3, [None] * 6, "Frage nicht lesbar")
                continue
            kind, city, (lo, hi, unit), day = p
            base[2:9] = [city, None, kind, lo, hi, unit, day.isoformat()]
            loc = self._city(city, cid)
            if not loc:
                self._store(base, [None] * 3, [None] * 6, "Station/Zeitzone fehlt")
                continue
            station, tz = loc
            base[3] = station
            readings = self._readings(station, day, tz)
            if not readings:
                self._store(base, [None] * 3, [None] * 6, "keine Messwerte")
                continue
            # only readings published before the market closed count
            readings = [r for r in readings if not close or r[0] <= close]
            deads = [dead_time(readings, kind, lo, hi, unit, m) for m in MARGINS]
            if deads[0] is None:
                self._store(base, deads, [None] * 6, "nie unmöglich")
                continue
            stats["dead"] += 1
            if base[11] is None or base[11] < min_price:
                self._store(base, deads, [None] * 6, "schon vorher billig")
                continue
            cand.append((base, deads))
        stats["candidates"] = len(cand)
        tokens = {}
        for i in range(0, len(cand), 100):
            g = self._gamma([c[0][0] for c in cand[i:i + 100]])
            for cid, m in g.items():
                toks = m.get("clobTokenIds")
                if isinstance(toks, str):
                    import json
                    try:
                        toks = json.loads(toks)
                    except ValueError:
                        toks = None
                if toks:
                    tokens[cid] = str(toks[0])
        for base, deads in cand:
            tok = tokens.get(base[0])
            if not tok:
                self._store(base, deads, [None] * 6, "Token fehlt")
                continue
            close = base[9] or (deads[0] + 2 * 86400)
            hist = self._history(tok, deads[0] - 3600, close)
            if not hist:
                self._store(base, deads, [None] * 6, "kein Preisverlauf")
                continue
            prices = []
            for d in deads:
                for delay in DELAYS:
                    prices.append(price_after(hist, d + delay * 60, close) if d else None)
            stats["priced"] += 1
            self._store(base, deads, prices, None)
        stats["skipped"] = self.db.execute("SELECT COUNT(*) FROM wx_markets WHERE reason IS NOT NULL").fetchone()[0]
        self.db.execute("INSERT OR REPLACE INTO wx_meta VALUES('last_run', ?)",
                        (f"{now:.0f}|{stats['processed']}|{stats['candidates']}|{stats['priced']}",))
        self.db.commit()
        log.info("wxobs: %s", stats)
        return stats

    def _store(self, base: list, deads: list, prices: list, reason: Optional[str]) -> None:
        self.db.execute(f"INSERT OR REPLACE INTO wx_markets VALUES({','.join('?' * 23)})",
                        (*base, *deads, *prices, reason, time.time()))


def _fetch_text(url: str, params: dict) -> str:
    import requests
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=60)
            if r.status_code == 200:
                return r.text
            if r.status_code < 500:
                raise RuntimeError(f"HTTP {r.status_code}")
        except requests.RequestException:
            pass
        time.sleep(5 * (attempt + 1))
    raise RuntimeError("IEM nicht erreichbar")


# ====================================================================== analysis
def analysis(db_path: str, slip: float = 0.01) -> dict:
    """Per margin and delay: trades, errors (an "impossible" bucket that won) and the return of
    buying NO at 1 - YES price + `slip` plus fee."""
    out = dict(n=0, priced=0, results=[], cities=[], reasons=[], last_run=None, stations=[])
    if not Path(db_path).exists():
        return out
    db = sqlite3.connect(db_path)
    try:
        rows = db.execute("""SELECT city, station, outcome, close_ts, dead0_ts, dead1_ts, dead2_ts,
                                    yes0_15, yes0_60, yes1_15, yes1_60, yes2_15, yes2_60 FROM wx_markets
                             WHERE reason IS NULL""").fetchall()
        out["n"] = db.execute("SELECT COUNT(*) FROM wx_markets").fetchone()[0]
        out["reasons"] = db.execute("SELECT reason, COUNT(*) FROM wx_markets WHERE reason IS NOT NULL GROUP BY 1 ORDER BY 2 DESC").fetchall()
        out["stations"] = db.execute("SELECT city, station, tz, source, reason FROM wx_city ORDER BY city").fetchall()
        r = db.execute("SELECT value FROM wx_meta WHERE key='last_run'").fetchone()
        out["last_run"] = r[0] if r else None
    except sqlite3.OperationalError:
        rows = []
    db.close()
    out["priced"] = len(rows)
    for mi, margin in enumerate(MARGINS):
        for di, delay in enumerate(DELAYS):
            trades = []
            for city, st, res, close, *rest in rows:
                dead, yes = rest[mi], rest[3 + 2 * mi + di]
                if dead is None or yes is None or yes < 0.005:
                    continue
                buy = min(1 - yes + slip, 0.999)
                cost = buy * (1 + FEE_RATE * (1 - buy))
                trades.append(dict(city=city, yes=yes, won=1 - res, cost=cost,
                                   hours=((close or dead) - dead) / 3600))
            if not trades:
                out["results"].append(dict(margin=margin, delay=delay, n=0))
                continue
            cost = sum(t["cost"] for t in trades)
            pay = sum(t["won"] for t in trades)
            out["results"].append(dict(
                margin=margin, delay=delay, n=len(trades), errors=sum(1 - t["won"] for t in trades),
                yes=sum(t["yes"] for t in trades) / len(trades),
                ge2=sum(t["yes"] >= 0.02 for t in trades), ge5=sum(t["yes"] >= 0.05 for t in trades),
                ge10=sum(t["yes"] >= 0.10 for t in trades), roi=(pay - cost) / cost,
                profit_per_100=(pay - cost) / cost * 100, hours=sorted(t["hours"] for t in trades)[len(trades) // 2]))
    # per city, at margin 1 / 15 min: where do the errors come from?
    by = {}
    for city, st, res, close, *rest in rows:
        dead, yes = rest[1], rest[3 + 2]
        if dead is None or yes is None or yes < 0.005:
            continue
        c = by.setdefault(city, dict(city=city, station=st, n=0, errors=0, yes=0.0))
        c["n"] += 1
        c["errors"] += res
        c["yes"] += yes
    out["cities"] = sorted(({**c, "yes": c["yes"] / c["n"]} for c in by.values()), key=lambda c: (-c["errors"], -c["n"]))
    return out


def csv_rows(db_path: str) -> tuple:
    header = ["Markt-ID", "Frage", "Stadt", "Station", "Art", "Bucket von", "Bucket bis", "Einheit", "Messtag",
              "Geschlossen", "Ergebnis", "max. Preis Studie", "unmöglich ab (Rand 0)", "unmöglich ab (Rand 1)",
              "unmöglich ab (Rand 2)", "YES Rand 0 +15 min", "YES Rand 0 +60 min", "YES Rand 1 +15 min",
              "YES Rand 1 +60 min", "YES Rand 2 +15 min", "YES Rand 2 +60 min", "Grund"]
    if not Path(db_path).exists():
        return header, []
    db = sqlite3.connect(db_path)
    try:
        rows = db.execute("SELECT * FROM wx_markets ORDER BY close_ts").fetchall()
    except sqlite3.OperationalError:
        rows = []
    db.close()
    fmt = lambda t: datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") if t else ""  # noqa: E731
    out = []
    for r in rows:
        r = list(r[:22])
        for i in (9, 12, 13, 14):
            r[i] = fmt(r[i])
        r[10] = "" if r[10] is None else ("YES" if r[10] else "NO")
        for i in (5, 6):
            r[i] = "" if r[i] in (math.inf, -math.inf) else r[i]
        out.append(r)
    return header, out
