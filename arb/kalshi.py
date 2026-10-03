"""Kalshi market study: settled Kalshi markets with prices 1 day / 6 h / 1 h before the close.

Same question as the Polymarket study (arb/study.py), one advantage: Kalshi candlesticks carry the
best YES bid and ask of each hour, so a rule can be tested at the price one would really have paid
(the ask) instead of the last trade. Market data is public, no account or key needed.

  python run.py kalshi                 # one collection run (every 30 min via polyarb-kalshi.timer)

Public API v2: GET /markets (status=settled, min/max_close_ts, cursor paging), GET /series (category
per series), GET /series/{s}/markets/{t}/candlesticks (hourly bid/ask/price). Markets that settled
long ago may only be served under /historical/...; both are tried. Prices arrive in cents (ints) or,
in newer responses, as dollar strings (`*_dollars`) – both are read.
Not reachable from the development sandbox: verified against the documented format only. Every
skipped market keeps its reason, the Kalshi tab shows them, so the first server run tells what the
live API really returns.
"""
from __future__ import annotations

import logging
import random
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

BASE = "https://api.elections.kalshi.com/trade-api/v2"
DAY = 86400.0
CHECKPOINTS = {"1d": DAY, "6h": 6 * 3600, "1h": 3600}
VERSION = 4  # v2: millisecond candle times, detailed reasons, retries; v3: no parlays ("Exotics");
# v4: random sample below the volume filter, volume before each checkpoint, rows collected anew
ROW_VERSION = 2
SAMPLE_PCT = 20  # share of markets kept regardless of volume: the final volume depends on the outcome
FEE_RATE = 0.07  # Kalshi taker fee: 7 % * p * (1 - p) per contract (rounded up to the cent per order)
MAX_LISTED = 20000  # markets listed per window at most (20 pages)

# Kalshi series categories -> the German categories of the Polymarket study
CATEGORY_MAP = {
    "climate and weather": "Wetter", "weather": "Wetter", "climate": "Wetter",
    "sports": "Sport", "financials": "Finanz", "economics": "Wirtschaft", "politics": "Politik",
    "elections": "Politik", "crypto": "Krypto", "entertainment": "Kultur", "mentions": "Social",
    "science and technology": "Tech/KI", "companies": "Finanz", "world": "Sonstiges", "health": "Sonstiges",
    "transportation": "Sonstiges", "social": "Social", "commodities": "Finanz",
    "exotics": "Kombiwetten",  # multi-leg parlays (KXMVE...): thousands a day, crowd out everything else
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS kalshi_markets(
  ticker TEXT PRIMARY KEY, event_ticker TEXT, series TEXT, title TEXT, subtitle TEXT, category TEXT,
  kalshi_category TEXT, volume REAL, close_ts REAL, result INT,
  p_1d REAL, p_6h REAL, p_1h REAL,
  ask_yes_1d REAL, ask_no_1d REAL, ask_yes_6h REAL, ask_no_6h REAL, ask_yes_1h REAL, ask_no_1h REAL,
  collected_ts REAL);
CREATE TABLE IF NOT EXISTS kalshi_skipped(ticker TEXT PRIMARY KEY, reason TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS kalshi_meta(key TEXT PRIMARY KEY, value TEXT);
"""
# v2 columns (03.10.), added to older databases on start:
#   sample  0..99 from a hash of the ticker: rows with sample < SAMPLE_PCT are a random sample of ALL markets
#   vol_*   contracts traded in the 24 h before the checkpoint (known at that time, unlike the final volume)
#   exp_ts  expected expiration (close_ts can be early when the outcome was settled early)
V2_COLUMNS = [("sample", "INT"), ("vol_1d", "REAL"), ("vol_6h", "REAL"), ("vol_1h", "REAL"), ("exp_ts", "REAL"),
              ("v", "INT")]
COLUMNS = ["ticker", "event_ticker", "series", "title", "subtitle", "category", "kalshi_category", "volume",
           "close_ts", "result", "p_1d", "p_6h", "p_1h", "ask_yes_1d", "ask_no_1d", "ask_yes_6h", "ask_no_6h",
           "ask_yes_1h", "ask_no_1h", "collected_ts"] + [c for c, _ in V2_COLUMNS]


def sample_bucket(key: str) -> int:
    import hashlib
    return int(hashlib.sha1(str(key).encode()).hexdigest()[:8], 16) % 100


def _ts(v) -> Optional[float]:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _price(obj, key: str) -> Optional[float]:
    """A price in dollars (0..1) from `key` (cents) or `key_dollars` (dollar string), else None."""
    if not isinstance(obj, dict):
        return None
    d = obj.get(f"{key}_dollars")
    if d not in (None, ""):
        try:
            return float(d)
        except (TypeError, ValueError):
            pass
    c = obj.get(key)
    if c in (None, ""):
        return None
    try:
        c = float(c)
    except (TypeError, ValueError):
        return None
    return c / 100.0 if c > 1.0 else c


def candle_prices(candle: dict) -> dict:
    """Close of the hour: last trade (or mean), best YES ask, and the NO ask (= 1 - best YES bid)."""
    price = candle.get("price") or {}
    p = _price(price, "close")
    if p is None:
        p = _price(price, "mean")
    ask = _price(candle.get("yes_ask") or {}, "close")
    bid = _price(candle.get("yes_bid") or {}, "close")
    ask = ask if ask is not None and 0 < ask < 1 else None
    bid = bid if bid is not None and 0 < bid < 1 else None
    if p is None and ask is not None and bid is not None:
        p = (ask + bid) / 2  # no trade that hour: the middle of the book
    return dict(p=p, ask_yes=ask, ask_no=(1 - bid) if bid is not None else None)


def candle_volume(c: dict) -> float:
    for k in ("volume", "volume_fp"):
        v = c.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 0.0


def volume_before(candles: List[dict], ts: float, span: float = DAY) -> Optional[float]:
    """Contracts traded in (ts - span, ts]; None if the candles do not reach back that far."""
    if not candles or candle_ts(candles[0]) > ts - span + 3600:
        return None
    return sum(candle_volume(c) for c in candles if ts - span < candle_ts(c) <= ts)


def candle_ts(c: dict) -> float:
    """End of the candle's period in seconds (milliseconds are converted)."""
    t = float(c.get("end_period_ts") or c.get("ts") or 0)
    return t / 1000 if t > 1e12 else t


def at(candles: List[dict], ts: float, max_age: float) -> Optional[dict]:
    best = None
    for c in candles:
        if candle_ts(c) <= ts:
            best = c
        else:
            break
    if best is None or ts - candle_ts(best) > max_age:
        return None
    return candle_prices(best)


def _gap_reason(cs: List[dict], ref: float) -> str:
    """Why candles gave no price at the checkpoints – grouped, so the reasons stay countable."""
    last, first = candle_ts(cs[-1]), candle_ts(cs[0])
    if not any(candle_prices(c)["p"] is not None for c in cs):
        return "no prices: Kerzen ohne Preisfelder"
    if first > ref - 3600:
        return "no prices: Kerzen erst in der letzten Stunde"
    gap = (ref - last) / 3600
    return "no prices: letzte Kerze " + ("< 3 h" if gap < 3 else "3-12 h" if gap < 12 else "12-48 h" if gap < 48
                                         else "> 48 h") + " vor Schluss"


def result_yes(m: dict) -> Optional[int]:
    r = str(m.get("result") or "").lower()
    return 1 if r == "yes" else 0 if r == "no" else None


def volume_of(m: dict) -> float:
    """Traded contracts; newer API versions send counts as fixed-point strings (`volume_fp`)."""
    for k in ("volume", "volume_fp", "volume_contracts"):
        v = m.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 0.0


def series_of(m: dict) -> str:
    return str(m.get("series_ticker") or str(m.get("event_ticker") or m.get("ticker") or "").split("-")[0])


class KalshiStudy:
    def __init__(self, client, db_path: str, base: str = BASE):
        self.client, self.base = client, base.rstrip("/")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path)
        self.db.executescript(SCHEMA)
        have = {r[1] for r in self.db.execute("PRAGMA table_info(kalshi_markets)")}
        for name, typ in V2_COLUMNS:
            if name not in have:
                self.db.execute(f"ALTER TABLE kalshi_markets ADD COLUMN {name} {typ}")
        if "sample" not in have:
            self.db.executemany("UPDATE kalshi_markets SET sample=? WHERE ticker=?", [
                (sample_bucket(t), t) for (t,) in self.db.execute("SELECT ticker FROM kalshi_markets").fetchall()])
        self._series: Optional[Dict[str, str]] = None
        row = self.db.execute("SELECT value FROM kalshi_meta WHERE key='version'").fetchone()
        if not row or int(row[0]) < VERSION:  # candle problems of older versions: list and try those markets again
            self.db.execute("DELETE FROM kalshi_skipped WHERE reason LIKE 'no candles%' OR reason LIKE 'no prices%'")
            self.db.execute("DELETE FROM kalshi_meta WHERE key='backfill_until'")
            self.db.execute("DELETE FROM kalshi_markets WHERE lower(kalshi_category)='exotics' OR series LIKE 'KXMVE%'")
            self.db.execute("INSERT OR REPLACE INTO kalshi_meta VALUES('version', ?)", (str(VERSION),))
            self.db.commit()
        self.diag: Dict[str, object] = {"errors": [], "requests": 0}

    # ------------------------------------------------------------------ API
    def _get(self, path: str, params: dict) -> Optional[dict]:
        self.diag["requests"] = int(self.diag["requests"]) + 1
        try:
            return self.client.get_json(f"{self.base}{path}", params)
        except Exception as e:  # noqa – 4xx (PaginationEnd) or repeated failures: the caller records why
            log.debug("kalshi %s: %s", path, e)
            errs = self.diag["errors"]
            if len(errs) < 8:
                errs.append(f"{path.split('/markets/')[0][:60]}: {str(e)[:160]}")
            return None

    def _paged(self, path: str, key: str, params: dict, max_items: int) -> List[dict]:
        out, cursor = [], None
        while len(out) < max_items:
            data = self._get(path, dict(params, cursor=cursor) if cursor else params)
            if not data:
                break
            out += data.get(key) or []
            cursor = data.get("cursor")
            if not cursor or not data.get(key):
                break
        return out

    def series_categories(self) -> Dict[str, str]:
        if self._series is None:
            self._series = {str(s.get("ticker")): str(s.get("category") or "")
                            for s in self._paged("/series", "series", {}, 50000)}
        return self._series

    def candles(self, m: dict, start: float, end: float) -> List[dict]:
        params = {"start_ts": int(start), "end_ts": int(end), "period_interval": 60}
        for path in (f"/series/{series_of(m)}/markets/{m['ticker']}/candlesticks",
                     f"/historical/markets/{m['ticker']}/candlesticks"):
            data = self._get(path, params)
            cs = (data or {}).get("candlesticks") or []
            if cs:
                if "candle_sample" not in self.diag:  # one raw answer for the export
                    import json
                    self.diag["candle_sample"] = json.dumps({"keys": sorted(data), "n": len(cs), "first": cs[0],
                                                             "last": cs[-1], "path": path.split("/candlesticks")[0][-60:]},
                                                            default=str)[:1500]
                return sorted(cs, key=candle_ts)
        return []

    # ------------------------------------------------------------------ meta
    def _meta(self, key: str, value=None):
        if value is not None:
            self.db.execute("INSERT OR REPLACE INTO kalshi_meta VALUES(?,?)", (key, str(value)))
            return value
        row = self.db.execute("SELECT value FROM kalshi_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------ collect
    def collect(self, days_back: float = 90, max_new: int = 2000, window_days: float = 0.25,
                min_volume: float = 100, recent_days: float = 3,
                skip_categories: tuple = ("Krypto", "Kombiwetten"), now: Optional[float] = None,
                max_runtime_s: float = 1200) -> Dict[str, int]:
        """Like the Polymarket study: the last `recent_days` first, then the backfill continues where
        the previous run stopped. Crypto is skipped by default: thousands of hourly price ranges a day
        would crowd out everything else, and the Polymarket study found nothing there."""
        now = time.time() if now is None else now
        started = time.monotonic()
        out_of_time = lambda: time.monotonic() - started > max_runtime_s  # noqa: E731 – the timer starts the next run
        known = {r[0] for r in self.db.execute("SELECT ticker FROM kalshi_markets WHERE v >= ? UNION "
                                                "SELECT ticker FROM kalshi_skipped", (ROW_VERSION,))}
        stats = {"new": 0, "skipped": 0, "seen": 0, "windows": 0, "low_volume": 0, "category_skipped": 0,
                 "capped_windows": 0, "out_of_time": False}
        oldest = now - days_back * DAY
        t_hi = now
        while t_hi > now - recent_days * DAY and stats["new"] < max_new and not out_of_time():
            self._window(t_hi - window_days * DAY, t_hi, known, stats, max_new, min_volume, skip_categories, now)
            self._progress(stats, now, oldest)
            t_hi -= window_days * DAY
        bookmark = self._meta("backfill_until")
        t_hi = min(t_hi, float(bookmark)) if bookmark else t_hi
        while t_hi > oldest and stats["new"] < max_new and not out_of_time():
            t_lo = t_hi - window_days * DAY
            if self._window(t_lo, t_hi, known, stats, max_new, min_volume, skip_categories, now):
                self._meta("backfill_until", t_lo)
            self._progress(stats, now, oldest)
            t_hi = t_lo
        stats["out_of_time"] = out_of_time()
        self._progress(stats, now, oldest)
        log.info("kalshi: %s", stats)
        return stats

    def _progress(self, stats: dict, now: float, oldest: float) -> None:
        """Counts and diagnostics after every window: an interrupted run still shows where it was."""
        import json
        bookmark = self._meta("backfill_until")
        stats["backfill_days_left"] = round(max(0.0, ((float(bookmark) if bookmark else now) - oldest) / DAY), 1)
        self._meta("last_run", f"{now:.0f}|{stats['new']}|{stats['skipped']}|{stats['seen']}")
        self._meta("diag", json.dumps(dict(self.diag, stats=stats, ts=now), default=str))
        self.db.commit()

    def _window(self, t_lo, t_hi, known, stats, max_new, min_volume, skip_categories, now) -> bool:
        stats["windows"] += 1
        params = {"status": "settled", "min_close_ts": int(t_lo), "max_close_ts": int(t_hi), "limit": 1000}
        if self.diag.get("mve_filter") != "unsupported":
            params["mve_filter"] = "exclude"  # no parlays: listing them alone took hours
        markets = self._paged("/markets", "markets", params, MAX_LISTED)
        if not markets and "mve_filter" in params and self.diag.get("mve_filter") is None:
            markets = self._paged("/markets", "markets", {k: v for k, v in params.items() if k != "mve_filter"}, MAX_LISTED)
            self.diag["mve_filter"] = "unsupported" if markets else None
        elif markets and "mve_filter" in params:
            self.diag["mve_filter"] = "ok"
        if len(markets) >= MAX_LISTED:
            stats["capped_windows"] += 1  # accepted: the window counts as done, the rest of it is lost
        cats = self.series_categories() if markets else {}
        if markets and "market_keys" not in self.diag:  # what the live API really sends (shown in the export)
            m0 = markets[0]
            self.diag["market_keys"] = ",".join(sorted(m0))[:600]
            self.diag["market_sample"] = {k: m0.get(k) for k in ("ticker", "status", "result", "volume", "volume_fp",
                                                                 "close_time", "last_price", "last_price_dollars")}
            self.diag["series"] = len(cats)
        todo = []
        for m in markets:
            stats["seen"] += 1
            t = str(m.get("ticker") or "")
            if not t or t in known:
                continue
            if volume_of(m) < min_volume and sample_bucket(t) >= SAMPLE_PCT:
                stats["low_volume"] += 1
                continue
            opened, closed = _ts(m.get("open_time")), _ts(m.get("close_time"))
            if opened and closed and closed - opened < 2 * 3600:  # 15-minute gold/oil markets: no "1 h before"
                stats["too_short"] = stats.get("too_short", 0) + 1
                continue
            kcat = cats.get(series_of(m)) or str(m.get("category") or "")
            cat = CATEGORY_MAP.get(kcat.lower(), "Sonstiges")
            if series_of(m).startswith("KXMVE"):
                cat = "Kombiwetten"
            if cat in skip_categories:
                stats["category_skipped"] += 1
                continue
            todo.append((m, kcat, cat))
        todo.sort(key=lambda x: -volume_of(x[0]))  # the most traded first
        for m, kcat, cat in todo:
            if stats["new"] >= max_new:
                self.db.commit()
                return False
            known.add(m["ticker"])
            row = self._row(m, kcat, cat, now)
            if isinstance(row, str):
                self.db.execute("INSERT OR REPLACE INTO kalshi_skipped VALUES(?,?,?)", (m["ticker"], row, now))
                stats["skipped"] += 1
            else:
                self.db.execute(f"INSERT OR REPLACE INTO kalshi_markets({','.join(COLUMNS)}) "
                                f"VALUES({','.join('?' * len(COLUMNS))})", row)
                stats["new"] += 1
            if (stats["new"] + stats["skipped"]) % 50 == 0:
                self.db.commit()
        self.db.commit()
        return True

    def _row(self, m: dict, kcat: str, cat: str, now: float):
        if str(m.get("market_type") or "binary") != "binary":
            return "not binary"
        out = result_yes(m)
        if out is None:
            return f"result {m.get('result')!r}"
        close = _ts(m.get("close_time"))
        exp = _ts(m.get("expected_expiration_time"))
        ref = min(t for t in (close, exp) if t) if (close or exp) else None
        if not ref:
            return "no close time"
        cs = self.candles(m, ref - 54 * 3600, ref)  # 30 h for the prices, 24 h more for the volume before 1 d
        if not cs:
            if "no_candles_sample" not in self.diag:
                self.diag["no_candles_sample"] = {"ticker": m.get("ticker"), "series": series_of(m),
                                                  "close_time": m.get("close_time"), "ref": ref,
                                                  "expected_expiration_time": m.get("expected_expiration_time")}
            return "no candles"
        pts = {k: at(cs, ref - dt, max_age=max(dt / 2, 3 * 3600)) for k, dt in CHECKPOINTS.items()}
        if all(v is None for v in pts.values()):
            if "no_prices_sample" not in self.diag:
                self.diag["no_prices_sample"] = {"ticker": m.get("ticker"), "ref": ref, "n": len(cs),
                                                 "first_ts": candle_ts(cs[0]), "last_ts": candle_ts(cs[-1]),
                                                 "close_time": m.get("close_time")}
            return _gap_reason(cs, ref)
        g = lambda k, f: (pts[k] or {}).get(f)  # noqa: E731
        return (m["ticker"], m.get("event_ticker"), series_of(m), (m.get("title") or "")[:300],
                (m.get("yes_sub_title") or m.get("subtitle") or "")[:200], cat, kcat,
                volume_of(m), ref, out,
                g("1d", "p"), g("6h", "p"), g("1h", "p"),
                g("1d", "ask_yes"), g("1d", "ask_no"), g("6h", "ask_yes"), g("6h", "ask_no"),
                g("1h", "ask_yes"), g("1h", "ask_no"), now,
                sample_bucket(m["ticker"]), *(volume_before(cs, ref - dt) for dt in CHECKPOINTS.values()), exp, ROW_VERSION)


# ====================================================================== analysis
BANDS = [(0.03, 0.10), (0.10, 0.25), (0.25, 0.45), (0.45, 0.55), (0.55, 0.75), (0.75, 0.90), (0.90, 0.97)]
RULES = [
    dict(key="k_wetter_no_breit", name="Wetter-NO 55–97 % bis 5k Kontrakte (6 h vorher)", cp="6h", lo=0.55,
         hi=0.97, cat="Wetter", side="no", max_volume=5000),
    dict(key="k_wetter_no_alle", name="Wetter-NO 55–97 %, alle Volumen (6 h vorher)", cp="6h", lo=0.55, hi=0.97,
         cat="Wetter", side="no"),
    dict(key="k_sport_dog", name="Sport-Außenseiter 3–25 % (6 h vorher)", cp="6h", lo=0.03, hi=0.25, cat="Sport"),
    dict(key="k_finanz_dog", name="Finanz-Außenseiter 10–25 % (6 h vorher)", cp="6h", lo=0.10, hi=0.25, cat="Finanz"),
]


def _rows(db_path: str) -> list:
    if not Path(db_path).exists():
        return []
    db = sqlite3.connect(db_path)
    try:
        return db.execute("""SELECT ticker, event_ticker, category, volume, close_ts, result, p_1d, p_6h, p_1h,
                                    ask_yes_1d, ask_no_1d, ask_yes_6h, ask_no_6h, ask_yes_1h, ask_no_1h
                             FROM kalshi_markets""").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        db.close()


def _bets(rows, cp: str, lo: float, hi: float, cat: Optional[str] = None, side: str = "both",
          max_volume: Optional[float] = None) -> List[dict]:
    """Every side priced lo..hi at the checkpoint. Bought at the real ask when the candle had one,
    else at the price + 2 cents (`at_ask` tells which)."""
    i = {"1d": 0, "6h": 1, "1h": 2}[cp]
    out = []
    for (tk, ev, c, vol, t, res, *rest) in rows:
        p, asks = rest[i], rest[3 + 2 * i: 5 + 2 * i]
        if p is None or (cat and c != cat) or (max_volume is not None and (vol or 0) >= max_volume):
            continue
        sides = [(1 - p, 1 - res, asks[1])] if side == "no" else [(p, res, asks[0]), (1 - p, 1 - res, asks[1])]
        for price, won, ask in sides:
            if lo <= price < hi:
                use_ask = ask is not None and ask >= price - 0.05
                buy = ask if use_ask else min(price + 0.02, 0.99)
                out.append(dict(g=ev or tk, t=t or 0, price=price, buy=buy, won=won, at_ask=use_ask))
    return out


def _roi(bets: List[dict], extra: float = 0.0) -> float:
    cost = sum(min(b["buy"] + extra, 0.995) * (1 + FEE_RATE * (1 - min(b["buy"] + extra, 0.995))) for b in bets)
    return (sum(b["won"] for b in bets) - cost) / cost if cost else 0.0


def evaluate(bets: List[dict], n_boot: int = 300, seed: int = 1) -> Optional[dict]:
    if len(bets) < 20:
        return None
    rnd = random.Random(seed)
    games: Dict[str, list] = {}
    for b in bets:
        games.setdefault(b["g"], []).append(b)
    keys = list(games)
    boot = sorted(_roi([b for k in (rnd.choice(keys) for _ in keys) for b in games[k]]) for _ in range(n_boot))
    by_t = sorted(bets, key=lambda b: b["t"])
    half = len(by_t) // 2
    return dict(n=len(bets), events=len(keys), price=sum(b["price"] for b in bets) / len(bets),
                buy=sum(b["buy"] for b in bets) / len(bets), hit=sum(b["won"] for b in bets) / len(bets),
                at_ask=sum(b["at_ask"] for b in bets) / len(bets),
                roi=_roi(bets), roi_1ct=_roi(bets, 0.01), p_loss=sum(r < 0 for r in boot) / n_boot,
                p5=boot[int(0.05 * n_boot)], p95=boot[int(0.95 * n_boot)],
                first=_roi(by_t[:half]), second=_roi(by_t[half:]))


def analysis(db_path: str) -> dict:
    """Overview for the Kalshi tab: what was collected, the fixed rules, and an edge map
    (category x price band at 6 h) at the real ask."""
    rows = _rows(db_path)
    out = dict(n=len(rows), cats={}, rules=[], grid=[], skipped=[], last_run=None, span=None)
    if Path(db_path).exists():
        db = sqlite3.connect(db_path)
        try:
            out["skipped"] = db.execute("SELECT reason, COUNT(*) FROM kalshi_skipped GROUP BY 1 ORDER BY 2 DESC LIMIT 8").fetchall()
            r = db.execute("SELECT value FROM kalshi_meta WHERE key='last_run'").fetchone()
            out["last_run"] = r[0] if r else None
        except sqlite3.OperationalError:
            pass
        db.close()
    if not rows:
        return out
    out["span"] = (min(r[4] for r in rows), max(r[4] for r in rows))
    for r in rows:
        c = out["cats"].setdefault(r[2], dict(n=0, vol=0.0))
        c["n"] += 1
        c["vol"] += r[3] or 0
    for rule in RULES:
        res = evaluate(_bets(rows, rule["cp"], rule["lo"], rule["hi"], rule["cat"], rule.get("side", "both"),
                             rule.get("max_volume")))
        out["rules"].append(dict(rule, res=res))
    for cat in sorted(out["cats"], key=lambda c: -out["cats"][c]["n"]):
        for lo, hi in BANDS:
            res = evaluate(_bets(rows, "6h", lo, hi, cat), n_boot=200)
            if res and res["n"] >= 50:
                out["grid"].append(dict(cat=cat, band=f"{lo * 100:.0f}–{hi * 100:.0f} %", res=res))
    return out


def diag_rows(db_path: str) -> tuple:
    """Last run as key/value rows for the export: counts, API errors, the fields the API sent."""
    import json
    header = ["Schlüssel", "Wert"]
    if not Path(db_path).exists():
        return header, [["Status", "noch kein Lauf"]]
    db = sqlite3.connect(db_path)
    try:
        r = db.execute("SELECT value FROM kalshi_meta WHERE key='diag'").fetchone()
        skipped = db.execute("SELECT reason, COUNT(*) FROM kalshi_skipped GROUP BY 1 ORDER BY 2 DESC").fetchall()
    except sqlite3.OperationalError:
        r, skipped = None, []
    db.close()
    d = json.loads(r[0]) if r else {}
    rows = [[k, json.dumps(v, default=str) if isinstance(v, (dict, list)) else v] for k, v in d.items()]
    rows += [[f"übersprungen: {reason}", n] for reason, n in skipped]
    if not r:  # the database exists, but the last run was an older version without diagnostics
        rows.insert(0, ["Status", "Datenbank vorhanden, Diagnose erst ab dem nächsten Lauf"])
    return header, rows


def csv_rows(db_path: str) -> tuple:
    header = ["Ticker", "Event", "Serie", "Frage", "Bucket/Untertitel", "Kategorie", "Kalshi-Kategorie",
              "Volumen (Kontrakte)", "Schluss", "Ergebnis", "Preis 1 Tag vorher", "Preis 6 h vorher",
              "Preis 1 h vorher", "Ask YES 1 Tag", "Ask NO 1 Tag", "Ask YES 6 h", "Ask NO 6 h", "Ask YES 1 h",
              "Ask NO 1 h", "Stichprobe (0-99)", "Kontrakte 24 h vor 1 Tag", "… vor 6 h", "… vor 1 h",
              "Ablauf erwartet", "Version"]
    if not Path(db_path).exists():
        return header, []
    db = sqlite3.connect(db_path)
    try:
        have = {r[1] for r in db.execute("PRAGMA table_info(kalshi_markets)")}
        cols = [c for c in COLUMNS if c != "collected_ts"]
        sel = ", ".join(c if c in have else f"NULL AS {c}" for c in cols)
        rows = db.execute(f"SELECT {sel} FROM kalshi_markets ORDER BY close_ts").fetchall()
    except sqlite3.OperationalError:
        rows = []
    db.close()
    out = []
    for r in rows:
        r = list(r)
        r[8] = datetime.fromtimestamp(r[8]).strftime("%Y-%m-%d %H:%M") if r[8] else ""
        r[9] = "YES" if r[9] == 1 else "NO"
        r[23] = datetime.fromtimestamp(r[23]).strftime("%Y-%m-%d %H:%M") if r[23] else ""
        r[24] = r[24] or 1
        out.append(r)
    return header, out

