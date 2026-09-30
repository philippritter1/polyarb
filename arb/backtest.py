"""Stress test of simple price-band rules on the market study (data/study.sqlite).

Rule: at a checkpoint before close, buy every side whose price lies in [lo, hi) (optionally one
category only), hold to resolution. The test asks what could make a good-looking result fake:
  costs      – taker fee plus a surcharge of 0..5 cents over the historical (traded) price, since
               the real ask of a long shot can sit well above it
  luck       – bootstrap over whole games (markets of one match move together), P(loss)
  regime     – first half vs. second half of the sample in time
  pain       – drawdown and losing streak for flat stakes, over random orderings of the bets
"""
from __future__ import annotations

import random
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

RULES = [
    dict(key="underdog_5k", name="Underdog-Sport ab 5k $ (6 h vorher, 3–10 %)", cp="p_6h", lo=0.03, hi=0.10,
         cat="Sport", vol=(5e3, None)),
    dict(key="favorite_small", name="Favorit-Kleinmarkt 1–5k $ (6 h vorher, 90–97 %)", cp="p_6h", lo=0.90, hi=0.97,
         cat="Sport", vol=(1e3, 5e3)),
    dict(key="underdog_6h", name="Underdog-Sport alle (6 h vorher, 3–10 %)", cp="p_6h", lo=0.03, hi=0.10, cat="Sport"),
    dict(key="endgame_1h", name="Endspiel-Ernte (1 h vorher, 95–99 %)", cp="p_1h", lo=0.95, hi=0.99, cat=None),
    dict(key="longshot_1d", name="Longshot-NO (1 Tag vorher, 92–98 %)", cp="p_1d", lo=0.92, hi=0.98, cat=None),
]
# Note: the study stores the FINAL volume; a live scenario can only filter on the volume at purchase.
SLIPS = (0.0, 0.01, 0.02, 0.03, 0.04, 0.05)


def game_key(question: str) -> str:
    """Cluster markets of one match ('Army vs. Temple: O/U 50.5' -> 'army vs. temple')."""
    return re.split(r":| - | O/U| Spread| \(", question or "")[0].strip().lower()


def _bets(rows, cp_idx: int, lo: float, hi: float, cat: Optional[str], slip: float, fee_rate: float,
          vol: Optional[tuple] = None) -> List[dict]:
    out = []
    v_lo, v_hi = (vol[0] or 0, vol[1] or float("inf")) if vol else (0, float("inf"))
    for q, c, outcome, t, volume, *prices in rows:
        p = prices[cp_idx]
        if p is None or (cat and c != cat) or not v_lo <= (volume or 0) < v_hi:
            continue
        for price, won in ((p, outcome), (1 - p, 1 - outcome)):
            if lo <= price < hi:
                buy = min(price + slip, 0.99)
                out.append(dict(g=game_key(q), t=t or 0, cost=buy + fee_rate * buy * (1 - buy), pay=won, price=price))
    return out


def _roi(bets: List[dict]) -> float:
    cost = sum(b["cost"] for b in bets)
    return (sum(b["pay"] for b in bets) - cost) / cost if cost else 0.0


def run_rule(rows, rule: dict, fee_rate: float = 0.05, stress_slip: float = 0.02, stake: float = 10.0,
             n_boot: int = 1000, seed: int = 1) -> Optional[dict]:
    cp_idx = ["p_7d", "p_1d", "p_6h", "p_1h"].index(rule["cp"])
    base = _bets(rows, cp_idx, rule["lo"], rule["hi"], rule["cat"], 0.0, fee_rate, rule.get("vol"))
    if len(base) < 20:
        return dict(rule, n=len(base))
    stressed = _bets(rows, cp_idx, rule["lo"], rule["hi"], rule["cat"], stress_slip, fee_rate, rule.get("vol"))
    rnd = random.Random(seed)
    games: Dict[str, list] = {}
    for b in stressed:
        games.setdefault(b["g"], []).append(b)
    keys = list(games)
    boot = sorted(_roi([b for k in (rnd.choice(keys) for _ in keys) for b in games[k]]) for _ in range(n_boot))
    by_time = sorted(stressed, key=lambda b: b["t"])
    half = len(by_time) // 2
    dds, streak, worst = [], 0, 0
    for b in by_time:
        streak = streak + 1 if not b["pay"] else 0
        worst = max(worst, streak)
    for _ in range(n_boot):
        seq = stressed[:]
        rnd.shuffle(seq)
        eq = peak = dd = 0.0
        for b in seq:
            eq += stake * (b["pay"] / b["cost"] - 1)
            peak, dd = max(peak, eq), max(dd, peak - eq)
        dds.append(dd)
    dds.sort()
    return dict(rule, n=len(base), games=len(keys), hit=sum(b["pay"] for b in base) / len(base),
                price=sum(b["price"] for b in base) / len(base),
                roi={f"{s:.2f}": _roi(_bets(rows, cp_idx, rule["lo"], rule["hi"], rule["cat"], s, fee_rate, rule.get("vol"))) for s in SLIPS},
                boot_p5=boot[int(0.05 * n_boot)], boot_p95=boot[int(0.95 * n_boot)],
                p_loss=sum(r < 0 for r in boot) / n_boot,
                roi_first=_roi(by_time[:half]), roi_second=_roi(by_time[half:]),
                dd_median=dds[n_boot // 2], dd_p95=dds[int(0.95 * n_boot)], worst_streak=worst,
                stress_slip=stress_slip, stake=stake)


def stress_test(db_path: str, **kw) -> List[dict]:
    if not Path(db_path).exists():
        return []
    db = sqlite3.connect(db_path)
    try:
        rows = db.execute("SELECT question, category, outcome, COALESCE(close_ts, end_ts), volume, "
                          "p_7d, p_1d, p_6h, p_1h FROM markets").fetchall()
    except sqlite3.OperationalError:
        rows = []
    db.close()
    return [run_rule(rows, r, **kw) for r in RULES] if rows else []
