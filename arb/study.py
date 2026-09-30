"""Market study: how well do Polymarket prices predict outcomes?

Collects resolved binary markets with their price history (CLOB /prices-history) and stores, per
market, the YES price 7 days / 1 day / 6 hours / 1 hour before the market closed plus the actual
outcome. From that the dashboard builds calibration tables: if 95-cent favourites win 97 % of the
time, buying them has an edge; if they win 93 %, it does not. Thousands of past markets answer in
days what paper trading answers in months.

Runs on the server (`python run.py study`, timer every 30 min), incremental: known markets are skipped
and a bookmark remembers how far back the collection already got.
"""
from __future__ import annotations

import logging
import math
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .client import PaginationEnd, _parse_json_list, _parse_ts

log = logging.getLogger("polyarb.study")
DAY = 86_400
CHECKPOINTS = {"p_7d": 7 * DAY, "p_1d": DAY, "p_6h": 6 * 3600, "p_1h": 3600}
CHECKPOINT_NAMES = {"p_7d": "7 Tage vorher", "p_1d": "1 Tag vorher", "p_6h": "6 h vorher", "p_1h": "1 h vorher"}
BUCKETS = [0.0, 0.02, 0.05, 0.10, 0.20, 0.35, 0.50, 0.65, 0.80, 0.90, 0.95, 0.98, 1.0001]

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets(
  condition_id TEXT PRIMARY KEY, question TEXT, category TEXT, neg_risk INT, sports INT,
  volume REAL, end_ts REAL, close_ts REAL, outcome INT,
  p_7d REAL, p_1d REAL, p_6h REAL, p_1h REAL, n_points INT, collected_ts REAL);
CREATE TABLE IF NOT EXISTS skipped(condition_id TEXT PRIMARY KEY, reason TEXT, ts REAL);
"""

# checked BEFORE the sport signals: Polymarket gives weather and crypto markets a gameStartTime too
NON_SPORT = [
    ("Wetter", ("temperature", "rain", "snow", "hurricane", "weather")),
    ("Krypto", ("up or down", "bitcoin", "btc", "ethereum", "eth ", "solana", "xrp", "crypto", "doge")),
]
CATEGORY_WORDS = [
    *NON_SPORT,
    ("Krypto", ("token", "fdv")),
    ("Politik", ("election", "president", "senate", "prime minister", "parliament", "trump", "vote", "governor",
                 "minister", "party", "nominee")),
    ("Wirtschaft", ("fed ", "interest rate", "inflation", "cpi", "gdp", "unemployment", "recession", "stock",
                    "close above", "s&p", "nasdaq", "earnings")),
    ("Tech/KI", (" ai ", "openai", "google", "apple", "nvidia", "model", "gpt", "llm", "tesla")),
    ("Kultur", ("oscar", "grammy", "movie", "album", "box office", "song", "tweet", "youtube", "mrbeast")),
]


def categorize(m: dict) -> str:
    q = f" {(m.get('question') or '').lower()} "
    for name, words in NON_SPORT:
        if any(w in q for w in words):
            return name
    if m.get("gameStartTime") or m.get("sportsMarketType"):
        return "Sport"
    if any(w in q for w in (" vs. ", " vs ", " win on ", "o/u", "spread:", "exact score", "moneyline")):
        return "Sport"
    for name, words in CATEGORY_WORDS:
        if any(w in q for w in words):
            return name
    return "Sonstiges"


def fix_categories(db: sqlite3.Connection) -> int:
    """Rows collected before 30.09. filed weather/crypto markets with a gameStartTime under "Sport"."""
    n = 0
    for name, words in NON_SPORT:
        like = " OR ".join("lower(question) LIKE ?" for _ in words)
        n += db.execute(f"UPDATE markets SET category = ?, sports = 0 WHERE category != ? AND ({like})",
                        (name, name, *[f"%{w}%" for w in words])).rowcount
    db.commit()
    return n


def outcome_yes(m: dict) -> Optional[int]:
    """1 if YES won, 0 if NO won, None for unresolved / 50-50 / anything else."""
    prices = _parse_json_list(m.get("outcomePrices"))
    if len(prices) != 2:
        return None
    try:
        a, b = float(prices[0]), float(prices[1])
    except (TypeError, ValueError):
        return None
    if a >= 0.99 and b <= 0.01:
        return 1
    if b >= 0.99 and a <= 0.01:
        return 0
    return None


def price_at(history: List[dict], ts: float, max_age: float) -> Optional[float]:
    """Last traded price at or before ts, if it is not older than max_age."""
    best = None
    for h in history:
        t = float(h.get("t", 0))
        if t <= ts:
            best = h
        else:
            break
    if best is None or ts - float(best["t"]) > max_age:
        return None
    return float(best["p"])


def wilson(k: int, n: int, z: float = 1.96) -> tuple:
    if n == 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (c - r) / d), min(1.0, (c + r) / d)


class Study:
    def __init__(self, client, db_path: str):
        self.client = client
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path)
        self.db.executescript(SCHEMA)
        fix_categories(self.db)

    def _known(self) -> set:
        return {r[0] for r in self.db.execute("SELECT condition_id FROM markets UNION SELECT condition_id FROM skipped")}

    def _history(self, token: str, start: float, end: float) -> List[dict]:
        """Hourly price history; resolved markets sometimes only serve coarser data -> fall back to 12 h."""
        for fidelity in (60, 720):
            try:
                data = self.client.get_json(f"{self.client.clob}/prices-history", {
                    "market": token, "startTs": int(start), "endTs": int(end), "fidelity": fidelity})
            except PaginationEnd:
                data = None
            hist = sorted((data or {}).get("history") or [], key=lambda h: h.get("t", 0))
            if hist:
                return hist
        return []

    def _weather_markets(self, t_lo: float, t_hi: float, min_volume: float) -> List[dict]:
        """Temperature buckets trade little each (often < 1,000 $), so the volume filter of the main
        listing drops them. Fetch closed weather events instead and keep their buckets from min_volume."""
        events = self.client.paged("/events", {
            "closed": "true", "tag_slug": "weather", "end_date_min": _iso(t_lo), "end_date_max": _iso(t_hi)},
            max_items=2000)
        out = []
        for ev in events:
            if "temperature in" not in (ev.get("title") or "").lower():
                continue
            for m in ev.get("markets") or []:
                if float(m.get("volumeNum") or m.get("volume") or 0) >= min_volume:
                    out.append(dict(m, endDate=m.get("endDate") or ev.get("endDate")))
        return out

    def _meta(self, key: str, value: Optional[float] = None) -> Optional[float]:
        self.db.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value REAL)")
        if value is not None:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, value))
            return value
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def collect(self, days_back: float = 120, max_new: int = 3000, window_days: float = 2,
                min_volume: float = 1000, weather_min_volume: float = 50, recent_days: float = 7,
                now: Optional[float] = None) -> Dict[str, int]:
        """One run: first the last `recent_days` (markets that just closed, or resolved late after a
        dispute), then the backfill continues where the previous run stopped (bookmark in `meta`).
        Cheap enough to run every 30 minutes once the backfill is done."""
        now = time.time() if now is None else now
        known = self._known()
        stats = {"new": 0, "skipped": 0, "seen": 0, "weather": 0, "windows": 0}
        oldest = now - days_back * DAY
        t_hi = now
        while t_hi > now - recent_days * DAY and stats["new"] < max_new:
            self._window(t_hi - window_days * DAY, t_hi, known, stats, max_new, min_volume, weather_min_volume, now)
            t_hi -= window_days * DAY
        t_hi = min(t_hi, self._meta("backfill_until") or t_hi)
        while t_hi > oldest and stats["new"] < max_new:
            t_lo = t_hi - window_days * DAY
            if self._window(t_lo, t_hi, known, stats, max_new, min_volume, weather_min_volume, now):
                self._meta("backfill_until", t_lo)  # this window is complete, never list it again
                self.db.commit()
            t_hi = t_lo
        stats["backfill_days_left"] = round(max(0.0, ((self._meta("backfill_until") or now) - oldest) / DAY), 1)
        log.info("study: %s", stats)
        return stats

    def _window(self, t_lo: float, t_hi: float, known: set, stats: dict, max_new: int, min_volume: float,
                weather_min_volume: float, now: float) -> bool:
        """Collect one window of closed markets. False if max_new stopped it midway."""
        stats["windows"] += 1
        markets = self.client.paged("/markets", {
            "closed": "true", "end_date_min": _iso(t_lo), "end_date_max": _iso(t_hi),
            "volume_num_min": min_volume, "order": "volume", "ascending": "false"}, max_items=2000)
        weather = self._weather_markets(t_lo, t_hi, weather_min_volume) if weather_min_volume else []
        stats["weather"] += len(weather)
        for m in markets + weather:
            cid = str(m.get("conditionId") or m.get("id"))
            stats["seen"] += 1
            if cid in known:
                continue
            if stats["new"] >= max_new:
                self.db.commit()
                return False
            known.add(cid)
            row = self._row(m, cid, now)
            if isinstance(row, str):
                self.db.execute("INSERT OR REPLACE INTO skipped VALUES(?,?,?)", (cid, row, now))
                stats["skipped"] += 1
            else:
                self.db.execute("INSERT OR REPLACE INTO markets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
                stats["new"] += 1
            if (stats["new"] + stats["skipped"]) % 50 == 0:
                self.db.commit()
        self.db.commit()
        return True

    def _row(self, m: dict, cid: str, now: float):
        toks = _parse_json_list(m.get("clobTokenIds"))
        if len(toks) != 2:
            return "not binary"
        out = outcome_yes(m)
        if out is None:
            return "unresolved"
        end = _parse_ts(m.get("endDate"))
        closed = _parse_ts(m.get("closedTime")) or end
        ref = min(t for t in (end, closed) if t) if (end or closed) else None
        if not ref:
            return "no dates"
        hist = self._history(str(toks[0]), ref - 8 * DAY, ref)
        if not hist:
            return "no history"
        prices = {k: price_at(hist, ref - dt, max_age=max(dt / 2, 12 * 3600)) for k, dt in CHECKPOINTS.items()}
        if all(v is None for v in prices.values()):
            return "no prices"
        return (cid, (m.get("question") or "")[:300], categorize(m), int(bool(m.get("negRisk"))),
                int(categorize(m) == "Sport"), float(m.get("volumeNum") or m.get("volume") or 0), end, closed, out,
                prices["p_7d"], prices["p_1d"], prices["p_6h"], prices["p_1h"], len(hist), now)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ====================================================================== analysis (dashboard, CSV)
def calibration(db_path: str) -> dict:
    """Per checkpoint and price bucket: how often did YES win vs. what the price said.

    Each market counts twice, as YES at p and as NO at 1-p, so favourites and long shots are
    both visible no matter which side the question is phrased on.
    """
    if not Path(db_path).exists():
        return dict(n=0, rows=[], cats=[])
    db = sqlite3.connect(db_path)
    try:
        rows = db.execute("SELECT category, outcome, p_7d, p_1d, p_6h, p_1h, end_ts FROM markets").fetchall()
    except sqlite3.OperationalError:
        rows = []
    db.close()
    out, cats = [], []
    for ci, key in enumerate(CHECKPOINTS):
        pts = []
        for r in rows:
            p = r[2 + ci]
            if p is not None:
                pts += [(r[0], p, r[1]), (r[0], 1 - p, 1 - r[1])]
        for lo, hi in zip(BUCKETS, BUCKETS[1:]):
            sel = [x for x in pts if lo <= x[1] < hi]
            if not sel:
                continue
            k, n = sum(x[2] for x in sel), len(sel)
            price = sum(x[1] for x in sel) / n
            clo, chi = wilson(k, n)
            out.append(dict(cp=key, lo=lo, hi=min(hi, 1.0), n=n, price=price, rate=k / n, ci_lo=clo, ci_hi=chi,
                            edge=k / n - price))
        if key == "p_1d":
            for cat in sorted({x[0] for x in pts}):
                for lo, hi, label in ((0.90, 1.0001, "Favoriten ≥ 90 %"), (0.0, 0.10, "Außenseiter < 10 %")):
                    sel = [x for x in pts if x[0] == cat and lo <= x[1] < hi]
                    if len(sel) >= 10:
                        k, n = sum(x[2] for x in sel), len(sel)
                        price = sum(x[1] for x in sel) / n
                        clo, chi = wilson(k, n)
                        cats.append(dict(cat=cat, group=label, n=n, price=price, rate=k / n, ci_lo=clo, ci_hi=chi,
                                         edge=k / n - price))
    ends = [r[6] for r in rows if r[6]]
    return dict(n=len(rows), rows=out, cats=cats, t0=min(ends) if ends else 0, t1=max(ends) if ends else 0)
