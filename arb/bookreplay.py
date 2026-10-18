"""Load booklog snapshots and score a quoting rule offline.

This is a stub, not a full matching engine. A rule sees one snapshot and returns a bid price, or None.
The bid is assumed filled when a *later* snapshot of the same token (within `max_wait_s`) carries a
last trade strictly below that bid. Markout is the mid of the first snapshot at least `markout_s` after
that trade, minus the bid, per share. There is no queue and no partial depth walk.

  python -m arb.bookreplay data/booklog/2026-10-18.sqlite --distance 0.02
"""
from __future__ import annotations

import argparse
import math
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence

from .booklog import unpack_levels


@dataclass
class Snap:
    ts: int
    token: str
    cid: str
    bid: Optional[float]
    ask: Optional[float]
    bid_sz: Optional[float]
    ask_sz: Optional[float]
    mid: Optional[float]
    last: Optional[float]
    bids: list = field(default_factory=list)
    asks: list = field(default_factory=list)
    event: str = ""
    category: str = ""
    end_ts: Optional[float] = None
    title: str = ""


def load_snaps(paths: Sequence[str]) -> List[Snap]:
    """Every snapshot in the given day-files, time order."""
    out: List[Snap] = []
    for path in paths:
        db = sqlite3.connect(path)
        try:
            rows = db.execute(
                """SELECT s.ts, s.token, s.cid, s.bid, s.ask, s.bsz, s.asz, s.mid, s.last, s.bids, s.asks,
                          COALESCE(m.event, ''), COALESCE(m.category, ''), m.end_ts, COALESCE(m.title, '')
                   FROM snap s LEFT JOIN mkt m ON m.cid = s.cid
                   ORDER BY s.ts, s.token""")
        except sqlite3.OperationalError:
            db.close()
            continue
        for r in rows:
            out.append(Snap(ts=r[0], token=r[1], cid=r[2] or "", bid=r[3], ask=r[4], bid_sz=r[5], ask_sz=r[6],
                            mid=r[7], last=r[8], bids=unpack_levels(r[9]), asks=unpack_levels(r[10]),
                            event=r[11], category=r[12], end_ts=r[13], title=r[14]))
        db.close()
    out.sort(key=lambda s: (s.ts, s.token))
    return out


def distance_rule(distance: float = 0.02, tick: float = 0.01) -> Callable[[Snap], Optional[float]]:
    """Bid `distance` below the mid, strictly under the ask. A maker quote, not a take."""
    def rule(s: Snap) -> Optional[float]:
        if s.mid is None or s.ask is None:
            return None
        px = math.floor((s.mid - distance) / tick + 1e-9) * tick
        if px < tick or px >= s.ask - 1e-9:
            return None
        return round(px, 4)
    return rule


def replay(snaps: Sequence[Snap], rule: Callable[[Snap], Optional[float]], markout_s: float = 300,
           max_wait_s: float = 3600) -> dict:
    """Quotes, fills and per-share markout. See the module note for what counts as a fill."""
    by: dict = {}
    for s in snaps:
        by.setdefault(s.token, []).append(s)
    quotes = fills = 0
    mark = 0.0
    mark_n = 0
    for series in by.values():
        i, n = 0, len(series)
        while i < n:
            px = rule(series[i])
            hit = None
            if px is not None:
                quotes += 1
                for j in range(i + 1, n):
                    if series[j].ts > series[i].ts + max_wait_s:
                        break
                    if series[j].last is not None and series[j].last < px - 1e-9:
                        hit = j
                        break
            if hit is None:
                i += 1
                continue
            fills += 1
            target = series[hit].ts + markout_s
            for k in range(hit, n):
                if series[k].ts + 1e-9 >= target and series[k].mid is not None:
                    mark += series[k].mid - px
                    mark_n += 1
                    break
            i = hit + 1
    return dict(quotes=quotes, fills=fills, markout=mark, markout_n=mark_n,
                avg_markout=(mark / mark_n) if mark_n else None)


def main(argv: Optional[Sequence[str]] = None) -> dict:
    ap = argparse.ArgumentParser(description="Score a mid-minus-distance bid on booklog snapshots")
    ap.add_argument("paths", nargs="+", help="data/booklog/YYYY-MM-DD.sqlite")
    ap.add_argument("--distance", type=float, default=0.02, help="bid this far below the mid")
    ap.add_argument("--markout-s", type=float, default=300)
    ap.add_argument("--max-wait-s", type=float, default=3600)
    args = ap.parse_args(argv)
    out = replay(load_snaps(args.paths), distance_rule(args.distance), args.markout_s, args.max_wait_s)
    avg = "–" if out["avg_markout"] is None else f"{out['avg_markout']:+.4f}"
    print(f"Gebote {out['quotes']}  Fills {out['fills']}  Markout/Anteil {avg}  (n={out['markout_n']})")
    return out


if __name__ == "__main__":
    main()
