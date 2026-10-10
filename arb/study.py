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

import hashlib
import logging
import math
import re
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
# v2 (03.10.) columns, added to older databases by migrate():
#   sample  0..99 from a hash of the market id: rows with sample < SAMPLE_PCT are a random sample of ALL
#           markets, independent of the final volume (which depends on the outcome: upsets draw late trading)
#   a_*     hours since the price last changed at the checkpoint (0.50 that never moved = no real trade)
#   v       row version; rows of older versions are collected again when the backfill passes them
V2_COLUMNS = [("sample", "INT"), ("a_7d", "REAL"), ("a_1d", "REAL"), ("a_6h", "REAL"), ("a_1h", "REAL"), ("v", "INT")]
COLUMNS = ["condition_id", "question", "category", "neg_risk", "sports", "volume", "end_ts", "close_ts", "outcome",
           "p_7d", "p_1d", "p_6h", "p_1h", "n_points", "collected_ts"] + [c for c, _ in V2_COLUMNS]
ROW_VERSION = 2
SAMPLE_PCT = 20          # share of markets kept regardless of volume (random sample)
SAMPLE_MIN_VOLUME = 10   # below that nothing ever traded: not even listed


def sample_bucket(key: str) -> int:
    """Stable 0..99 from the market id – the same market always lands in the same bucket."""
    return int(hashlib.sha1(str(key).encode()).hexdigest()[:8], 16) % 100


def migrate(db: sqlite3.Connection) -> None:
    have = {r[1] for r in db.execute("PRAGMA table_info(markets)")}
    for name, typ in V2_COLUMNS:
        if name not in have:
            db.execute(f"ALTER TABLE markets ADD COLUMN {name} {typ}")
    if "sample" not in have:
        db.executemany("UPDATE markets SET sample=? WHERE condition_id=?",
                       [(sample_bucket(cid), cid) for (cid,) in db.execute("SELECT condition_id FROM markets").fetchall()])
    db.commit()

# checked BEFORE the sport signals: Polymarket gives weather, crypto, finance and "tweet count" markets a
# gameStartTime too. Words match whole words only ("rain" must not hit Ukraine or Rainbow Six, "eth" not Elizabeth).
NON_SPORT = [
    ("Wetter", ("temperature", "rain", "rainfall", "snow", "snowfall", "hurricane", "weather", "precipitation")),
    ("Krypto", ("bitcoin", "btc", "ethereum", "eth", "solana", "xrp", "crypto", "doge", "dogecoin", "bnb")),
]
SOCIAL_RE = re.compile(r"\bpost\b.*\b(tweets?|posts|truth social)\b|\btweets?\b", re.I)
# a ticker in brackets plus a price verb ("Google (GOOGL) close above", "S&P 500 (SPX) Up or Down"),
# commodities and indices; the brackets alone would also match "(USA)" in tennis or "(BO3)" in esports
FINANCE_RE = re.compile(
    r"\([A-Za-z]{1,6}\)[^?]*\b(close[sd]?|closes|finish(es)?|hit|up or down|opens?|beat|earnings|revenue|"
    r"operating margin|market cap)\b|\b(crude oil|natural gas|s&p 500|nasdaq|dow jones|treasury yield)\b|"
    r"\b(gold|silver) \((xauusd|xagusd)\)", re.I)
CATEGORY_WORDS = [
    ("Krypto", ("token", "fdv")),
    ("Politik", ("election", "president", "senate", "prime minister", "parliament", "trump", "vote", "governor",
                 "minister", "party", "nominee")),
    ("Wirtschaft", ("fed", "interest rate", "interest rates", "inflation", "cpi", "gdp", "unemployment",
                    "recession", "stock", "earnings")),
    ("Tech/KI", ("ai", "openai", "google", "apple", "nvidia", "model", "gpt", "llm", "tesla")),
    ("Kultur", ("oscar", "grammy", "movie", "album", "box office", "song", "youtube", "mrbeast")),
]
_WORDS_RE: Dict[tuple, re.Pattern] = {}


def _has(q: str, words: tuple) -> bool:
    rx = _WORDS_RE.get(words)
    if rx is None:
        rx = _WORDS_RE[words] = re.compile(r"(?<![\w])(" + "|".join(re.escape(w) for w in words) + r")(?![\w])")
    return bool(rx.search(q))


def categorize(m: dict) -> str:
    q = (m.get("question") or "").lower()
    if SOCIAL_RE.search(q):
        return "Social"
    for name, words in NON_SPORT:
        if _has(q, words):
            return name
    if FINANCE_RE.search(q):
        return "Finanz"
    if "up or down" in q:
        return "Krypto"
    if m.get("gameStartTime") or m.get("sportsMarketType"):
        return "Sport"
    if any(w in f" {q} " for w in (" vs. ", " vs ", " win on ", "o/u", "spread:", "exact score", "moneyline")):
        return "Sport"
    for name, words in CATEGORY_WORDS:
        if _has(q, words):
            return name
    return "Sonstiges"


ESPORTS_RE = re.compile(r"^(valorant|dota 2|lol|league of legends|counter-strike|cs2|rainbow six|overwatch|"
                        r"call of duty|rocket league|mobile legends|honor of kings|starcraft)\b|\bmap \d|"
                        r"game \d winner|total kills|first blood", re.I)
TENNIS_RE = re.compile(r"^(itf|atp|wta|m\d\d|w\d\d|us open|wimbledon|roland|australian open)\b|challenger|"
                       r"qualification atp|main draw|set \d winner", re.I)
US_RE = re.compile(r"run scored|inning|strikeouts|yankees|dodgers|padres|red sox|mets|cubs|guardians|mariners|"
                   r"phillies|brewers|orioles|marlins|royals|nationals|blue jays|astros|braves|pirates|rockies|"
                   r"angels|white sox|rays|cardinals|diamondbacks|\bnfl\b|\bnba\b|\bwnba\b|\bnhl\b|"
                   r"touchdown|yards|rebounds", re.I)


def sport_kind(question: str) -> str:
    """Rough sub-kind of a sport market (~95 % right on the study data): Esports, Tennis, US (mostly MLB)
    or Fussball (football and the rest)."""
    q = question or ""
    if ESPORTS_RE.search(q):
        return "Esports"
    if TENNIS_RE.search(q):
        return "Tennis"
    if US_RE.search(q):
        return "US"
    return "Fussball"


MARKET_TYPES = [("Exact Score", "exact score"), ("Remis", "end in a draw"), ("Halbzeit", "leading at halftime"),
                ("Beide treffen", "both teams"), ("Ueber/Unter", "o/u|over/under"), ("Spread", "spread|handicap"),
                ("Sieg", r"win on \d"), ("Satz/Map/Game", r"set \d|map \d|game \d"), ("Run 1. Inning", "run scored"),
                ("Quartalszahlen", "beat quarterly earnings|earnings|revenue|operating margin")]


def market_type(question: str) -> str:
    """Kind of bet: sport underdogs won on totals/spreads/draws but lost on plain "who wins" markets."""
    q = (question or "").lower()
    for name, rx in MARKET_TYPES:
        if re.search(rx, q):
            return name
    return "Match-Sieger" if re.search(r"\bvs\.?\b", q) else "Andere"


CATEGORIES_VERSION = 2
LISTING_VERSION = 3


def fix_categories(db: sqlite3.Connection) -> int:
    """Re-file stored rows with the current categorize() (v2, 01.10.: whole words, Finanz and Social).
    Runs once per CATEGORIES_VERSION. Without the stored gameStartTime the old `sports` flag stands in."""
    db.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value REAL)")
    row = db.execute("SELECT value FROM meta WHERE key='categories_version'").fetchone()
    if row and row[0] >= CATEGORIES_VERSION:
        return 0
    n = 0
    for cid, q, cat, sports in db.execute("SELECT condition_id, question, category, sports FROM markets").fetchall():
        new = categorize({"question": q, "gameStartTime": "x" if sports or cat == "Sport" else None})
        if new != cat:
            db.execute("UPDATE markets SET category = ?, sports = ? WHERE condition_id = ?",
                       (new, int(new == "Sport"), cid))
            n += 1
    db.execute("INSERT OR REPLACE INTO meta VALUES('categories_version', ?)", (CATEGORIES_VERSION,))
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
    return price_age_at(history, ts, max_age)[0]


def price_age_at(history: List[dict], ts: float, max_age: float) -> tuple:
    """(price, hours since the price last changed) at ts; (None, None) if there is no point within max_age."""
    best = None
    for i, h in enumerate(history):
        if float(h.get("t", 0)) <= ts:
            best = i
        else:
            break
    if best is None or ts - float(history[best]["t"]) > max_age:
        return None, None
    p = float(history[best]["p"])
    since = float(history[best]["t"])
    for h in reversed(history[:best]):
        if abs(float(h["p"]) - p) > 1e-9:
            break
        since = float(h["t"])
    return p, (ts - since) / 3600


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
        # WAL: the dashboard export reads for minutes while this run writes ("database is locked", 10.10.)
        self.db = sqlite3.connect(db_path, timeout=300)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        migrate(self.db)
        fix_categories(self.db)
        if (self._meta("listing_version") or 0) < LISTING_VERSION:
            # v2 (02.10.): 12-hour windows instead of 2 days (a window lists at most 2,000 markets, so busy
            # days were cut off) – walk the whole history once more; known markets cost no price request
            # v3 (03.10.): random sample below the volume filter, checkpoints from the planned end, price age:
            # walk again, rows of version < 2 are collected anew
            self.db.execute("DELETE FROM meta WHERE key='backfill_until'")
            self.db.execute("DELETE FROM skipped WHERE reason IN ('no prices', 'no history')")
            self._meta("listing_version", LISTING_VERSION)
            self.db.commit()

    def _known(self) -> set:
        return {r[0] for r in self.db.execute("SELECT condition_id FROM markets WHERE v >= ? UNION "
                                              "SELECT condition_id FROM skipped", (ROW_VERSION,))}

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

    def collect(self, days_back: float = 240, max_new: int = 3000, window_days: float = 0.5,
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

    def _listing(self, t_lo: float, t_hi: float, min_volume: float, stats: dict, cap: int = 2000) -> List[dict]:
        """Closed markets ending in [t_lo, t_hi]. Gamma lists at most ~2,000 per query (ordered by volume), so a
        full window is split in halves until it fits – otherwise the smallest markets would always be cut."""
        markets = self.client.paged("/markets", {
            "closed": "true", "end_date_min": _iso(t_lo), "end_date_max": _iso(t_hi),
            "volume_num_min": min_volume, "order": "volume", "ascending": "false"}, max_items=cap)
        if len(markets) >= cap and t_hi - t_lo > 1800:
            stats["split_windows"] = stats.get("split_windows", 0) + 1
            mid = (t_lo + t_hi) / 2
            return self._listing(t_lo, mid, min_volume, stats, cap) + self._listing(mid, t_hi, min_volume, stats, cap)
        if len(markets) >= cap:
            stats["full_windows"] = stats.get("full_windows", 0) + 1
        return markets

    def _window(self, t_lo: float, t_hi: float, known: set, stats: dict, max_new: int, min_volume: float,
                weather_min_volume: float, now: float) -> bool:
        """Collect one window of closed markets. False if max_new stopped it midway.
        Kept: every market from min_volume (as before) plus a random SAMPLE_PCT % of the smaller ones."""
        stats["windows"] += 1
        listed = self._listing(t_lo, t_hi, min(min_volume, SAMPLE_MIN_VOLUME), stats)
        markets = [m for m in listed if float(m.get("volumeNum") or m.get("volume") or 0) >= min_volume
                   or sample_bucket(str(m.get("conditionId") or m.get("id"))) < SAMPLE_PCT]
        stats["sample_small"] = stats.get("sample_small", 0) + sum(
            float(m.get("volumeNum") or m.get("volume") or 0) < min_volume for m in markets)
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
            if categorize(m) == "Krypto":  # no edge there (study 01.10.), and every market costs a price request
                self.db.execute("INSERT OR REPLACE INTO skipped VALUES(?,?,?)", (cid, "Krypto (nicht gesammelt)", now))
                stats["skipped"] += 1
                continue
            row = self._row(m, cid, now)
            if isinstance(row, str):
                self.db.execute("INSERT OR REPLACE INTO skipped VALUES(?,?,?)", (cid, row, now))
                stats["skipped"] += 1
            else:
                self.db.execute(f"INSERT OR REPLACE INTO markets({','.join(COLUMNS)}) VALUES({','.join('?' * len(COLUMNS))})", row)
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
        # checkpoints count back from the PLANNED end (what the live scenarios use), not from the actual close:
        # "Will X hit $Y?" closes the moment it hits, so "6 h before the close" would mean "6 h before the hit".
        # A checkpoint after the actual close is left empty – the market could no longer be bought then.
        ref = end or closed
        if not ref:
            return "no dates"
        last = min(ref, closed) if closed else ref
        hist = self._history(str(toks[0]), ref - 8 * DAY, last)
        if not hist:
            return "no history"
        pa = {k: price_age_at(hist, ref - dt, max_age=max(dt / 2, 12 * 3600)) if ref - dt < last else (None, None)
              for k, dt in CHECKPOINTS.items()}
        prices = {k: v[0] for k, v in pa.items()}
        if all(v is None for v in prices.values()):
            return "no prices"
        return (cid, (m.get("question") or "")[:300], categorize(m), int(bool(m.get("negRisk"))),
                int(categorize(m) == "Sport"), float(m.get("volumeNum") or m.get("volume") or 0), end, closed, out,
                prices["p_7d"], prices["p_1d"], prices["p_6h"], prices["p_1h"], len(hist), now,
                sample_bucket(cid), pa["p_7d"][1], pa["p_1d"][1], pa["p_6h"][1], pa["p_1h"][1], ROW_VERSION)


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
