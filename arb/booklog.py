"""Compact order-book snapshots for offline replay. Off unless `booklog.enabled` is true.

One SQLite file per UTC day, `data/booklog/YYYY-MM-DD.sqlite` (next to the main database). A row is one
token at one sample: best bid/ask and size, mid, last trade, and up to `depth` levels per side. Market
metadata (category, end, event id, title, YES/NO tokens) sits in a side table, once per market per day.
Two processes writing the same token in the same `every_s` bucket insert once (`INSERT OR IGNORE`).

Nothing here is read by quoting or by the paper fill. A disk error is logged and swallowed.

Size, roughly: one row is about 400 bytes with a long token id and 3 levels per side. At `every_s: 60`,
1 000 distinct tokens are about 100 MB/day, 7 days about 700 MB. The running market-making set is smaller
than that if tokens overlap (the bucket is shared): plan for on the order of 100–200 MB/day and under
1.5 GB at `keep_days: 7`. The arbitrage universe, if that service is on as well, is hundreds of extra
tokens and dominates the file. Raise `every_s` or lower `keep_days` when the disk is tight. Files older
than `keep_days` are deleted, including their WAL.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from array import array
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, Optional

log = logging.getLogger("polyarb.booklog")

SCHEMA = """
CREATE TABLE IF NOT EXISTS snap(
  ts INTEGER NOT NULL,
  token TEXT NOT NULL,
  cid TEXT,
  bid REAL, ask REAL, bsz REAL, asz REAL, mid REAL, last REAL,
  bids BLOB, asks BLOB,
  PRIMARY KEY(token, ts));
CREATE TABLE IF NOT EXISTS mkt(
  cid TEXT PRIMARY KEY,
  event TEXT, category TEXT, end_ts REAL, title TEXT, yes TEXT, no TEXT);
"""


def pack_levels(levels, depth: int) -> Optional[bytes]:
    """First `depth` (price, size) pairs as float32. The best level is the first pair."""
    if not levels or depth <= 0:
        return None
    out = array("f")
    for lv in list(levels)[:depth]:
        out.append(float(lv.price))
        out.append(float(lv.size))
    return out.tobytes()


def unpack_levels(blob: Optional[bytes]) -> list:
    if not blob:
        return []
    raw = array("f")
    raw.frombytes(blob)
    return [(raw[i], raw[i + 1]) for i in range(0, len(raw) - 1, 2)]


class BookLog:
    def __init__(self, enabled: bool = False, directory: str = "", every_s: float = 60,
                 depth: int = 3, keep_days: int = 7):
        self.enabled = bool(enabled)
        self.directory = directory
        self.every = max(1, int(every_s))
        self.depth = max(1, int(depth))
        self.keep_days = max(1, int(keep_days))
        self._bucket: Dict[str, int] = {}
        self._con: Optional[sqlite3.Connection] = None
        self._day = None
        self._pruned = None

    def record(self, now: float, books: dict, meta_by_token: Dict[str, dict],
               last: Optional[dict] = None) -> int:
        """Write tokens we already have a book and a metadata row for. Returns rows attempted."""
        if not self.enabled or not books or not meta_by_token:
            return 0
        try:
            return self._record(now, books, meta_by_token, last or {})
        except Exception:  # noqa – the log must not stop a quote cycle
            log.exception("booklog write failed")
            return 0

    def _record(self, now: float, books: dict, meta_by_token: dict, last: dict) -> int:
        bucket = int(now // self.every) * self.every
        rows, mkts = [], {}
        for token, ob in books.items():
            info = meta_by_token.get(token)
            if not info or ob is None:
                continue
            if self._bucket.get(token) == bucket:
                continue
            bid, ask = ob.best_bid, ob.best_ask
            bsz = ob.bids[0].size if ob.bids else None
            asz = ob.asks[0].size if ob.asks else None
            traded = last.get(token)
            rows.append((bucket, token, info.get("cid") or "", bid, ask, bsz, asz, ob.mid,
                         None if not traded else float(traded[0]),
                         pack_levels(ob.bids, self.depth), pack_levels(ob.asks, self.depth)))
            cid = info.get("cid")
            if cid:
                mkts[cid] = info
            self._bucket[token] = bucket
        if not rows:
            return 0
        db = self._db(bucket)
        db.executemany(
            "INSERT OR IGNORE INTO snap(ts, token, cid, bid, ask, bsz, asz, mid, last, bids, asks) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
        for cid, info in mkts.items():
            db.execute(
                """INSERT INTO mkt(cid, event, category, end_ts, title, yes, no) VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(cid) DO UPDATE SET event=excluded.event, category=excluded.category,
                     end_ts=excluded.end_ts, title=excluded.title, yes=excluded.yes, no=excluded.no""",
                (cid, info.get("event") or "", info.get("category") or "", info.get("end_ts"),
                 (info.get("title") or "")[:160], info.get("yes") or "", info.get("no") or ""))
        db.commit()
        return len(rows)

    def _db(self, bucket: int) -> sqlite3.Connection:
        day = datetime.fromtimestamp(bucket, tz=timezone.utc).date()
        if self._con is not None and self._day == day:
            return self._con
        if self._con is not None:
            self._con.close()
        os.makedirs(self.directory, exist_ok=True)
        path = os.path.join(self.directory, f"{day.isoformat()}.sqlite")
        con = sqlite3.connect(path, timeout=10)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.executescript(SCHEMA)
        self._con, self._day = con, day
        self._prune(day)
        return con

    def _prune(self, today) -> None:
        if self._pruned == today:
            return
        self._pruned = today
        cutoff = today - timedelta(days=self.keep_days)
        for name in os.listdir(self.directory):
            stem = name.split(".")[0]
            try:
                d = datetime.strptime(stem, "%Y-%m-%d").date()
            except ValueError:
                continue
            if d >= cutoff:
                continue
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(os.path.join(self.directory, stem + ".sqlite" + suffix))
                except OSError:
                    pass

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None


def open_log(cfg: dict) -> BookLog:
    """Shared recorder. Disabled unless booklog.enabled is true — then it does not create a directory."""
    c = (cfg or {}).get("booklog") or {}
    if not c.get("enabled"):
        return BookLog(enabled=False)
    base = os.path.dirname((cfg.get("storage") or {}).get("db_path") or "") or "."
    directory = c.get("dir") or os.path.join(base, "booklog")
    return BookLog(enabled=True, directory=directory, every_s=float(c.get("every_s", 60)),
                   depth=int(c.get("depth", 3)), keep_days=int(c.get("keep_days", 7)))


def meta_for(tokens: Iterable[str], info: dict) -> Dict[str, dict]:
    return {t: info for t in tokens if t}
