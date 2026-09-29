"""Logic ladders: related markets whose prices must respect an order.

Inside one Polymarket event the markets often form a ladder:
  thresholds  "Bitcoin above 100k / 105k / 110k on Oct 1?"  -> above 110k implies above 100k
  touch       "What price will Bitcoin hit?" ↑ 120k, ↑ 130k    -> hitting 130k implies hitting 120k
  deadlines   "X by October 31 / by December 31?"            -> by October implies by December

For a "narrow" market N that implies a "broad" market B, P(N) <= P(B). The set
[YES B, NO N] therefore pays at least $1 in every outcome (and $2 when B happens but N
does not), so buying it below $1 locks in a profit – held until both markets resolve.
Risk that remains: the two markets resolve by slightly different rules.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from itertools import combinations
from typing import Callable, List, Optional, Tuple

from .models import Basket, FeeSpec

NUM_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*([kmb])?\b", re.I)
RANGE_RE = re.compile(r"\d\s*[-–]\s*\$?\d|between|\bto\b", re.I)
MONTH_RE = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", re.I)
DOWN = ("↓", "below", "under", "less than", "dip", "fall", "drop", "lower than", "<")
UP = ("↑", "above", "over", "higher", "greater", "more than", "at least", "reach", "hit", "exceed", ">")
MULT = {"k": 1e3, "m": 1e6, "b": 1e9}


def _list(v) -> list:
    if isinstance(v, list):
        return v
    try:
        return json.loads(v or "[]")
    except (TypeError, ValueError):
        return []


def _ts(s: Optional[str]) -> Optional[float]:
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def threshold(label: str, question: str) -> Optional[Tuple[str, float]]:
    """('up' | 'down', value) for '↑ 120,000', '$110k', 'above 4.5%' ...; None for ranges/buckets."""
    if RANGE_RE.search(label) or MONTH_RE.search(label):  # buckets and dates are not thresholds
        return None
    m = NUM_RE.search(label.replace("$", ""))
    if not m:
        return None
    value = float(m.group(1).replace(",", "")) * MULT.get((m.group(2) or "").lower(), 1)
    text = f"{label} {question}".lower()
    if "↑" in text:
        return "up", value
    if "↓" in text:
        return "down", value
    if any(w in text for w in DOWN):
        return "down", value
    if any(w in text for w in UP):
        return "up", value
    return None


def ladders_from_event(ev: dict, fee_of: Callable[[dict, str], FeeSpec],
                       delay_of: Callable[[dict], float] = lambda m: 0.0, max_pairs: int = 60) -> List[Basket]:
    """[YES broad, NO narrow] baskets for every ordered pair of markets in one event."""
    if ev.get("negRisk"):  # mutually exclusive outcomes, not a ladder
        return []
    rungs = []
    for m in ev.get("markets") or []:
        toks = _list(m.get("clobTokenIds"))
        if len(toks) != 2 or m.get("closed") or not m.get("enableOrderBook") or not m.get("acceptingOrders", True):
            continue
        rungs.append((m, [str(t) for t in toks]))
    if len(rungs) < 2:
        return []
    cat = ev.get("category") or ""
    groups: dict = {}
    # thresholds: every market has a number and a direction; group per direction (↑ and ↓ share events)
    th = [(threshold(m.get("groupItemTitle") or "", m.get("question") or ""), m, t) for m, t in rungs]
    if all(x[0] for x in th):
        for (d, v), m, t in th:
            # broad = easier to reach: lower threshold going up, higher threshold going down;
            # only markets with the same deadline imply each other
            groups.setdefault((d, m.get("endDate")), []).append((v if d == "down" else -v, m, t))
    elif all(" by " in f" {(m.get('question') or '').lower()} " for m, _ in rungs):
        ends = [(_ts(m.get("endDate")), m, t) for m, t in rungs]
        if all(e for e, _, _ in ends) and len({e for e, _, _ in ends}) == len(ends):
            groups["by"] = ends  # broad = later deadline
    out: List[Basket] = []
    for kind, items in groups.items():
        items.sort(key=lambda x: x[0], reverse=True)  # broadest first
        for (kb, mb, tb), (kn, mn, tn) in list(combinations(items, 2))[:max_pairs]:
            if kb == kn:
                continue
            lb, ln = mb.get("groupItemTitle") or mb.get("question", "")[:30], mn.get("groupItemTitle") or mn.get("question", "")[:30]
            ends = [e for e in (_ts(mb.get("endDate")), _ts(mn.get("endDate"))) if e]
            out.append(Basket(
                basket_id=f"ladder:{ev.get('id')}:{mb.get('id') or tb[:10]}:{mn.get('id') or tn[:10]}",
                kind="ladder", title=f"{ev.get('title', '')} [{lb} ⊇ {ln}]",
                token_ids=[tb[0], tn[1]], labels=[f"YES {lb}", f"NO {ln}"],
                fees=[fee_of(mb, cat), fee_of(mn, cat)], end_ts=max(ends) if ends else None,
                category=cat, payout=1.0, delay_s=max(delay_of(mb), delay_of(mn))))
    return out
