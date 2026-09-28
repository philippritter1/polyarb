"""Arbitrage detection by walking order-book depth.

Idea: a *complete set* (all outcomes of a market/event) always pays exactly $1.
  buy_all : sum(ask_i + fee_i) < 1  -> buy one of each, then merge (binary) or hold to resolution (negRisk)
  sell_all: sum(bid_i - fee_i) > 1  -> split $1 into a set (binary), sell every leg

We walk the books level by level and only keep units whose *marginal* edge
clears the threshold, so the size reflects executable depth, not just top-of-book.
"""
from __future__ import annotations

import math
import time
from typing import Dict, List, Optional

from .models import Basket, Leg, Opportunity, OrderBook

SECONDS_PER_DAY = 86_400


def _floor2(x: float) -> float:
    return math.floor(x * 100 + 1e-9) / 100


def walk_depth(
    basket: Basket,
    books: Dict[str, OrderBook],
    direction: str,
    max_marginal_cost: float,
    max_qty: Optional[float] = None,
) -> Optional[dict]:
    """Walk all legs simultaneously. Returns per-leg qty/notional/fees or None.

    buy_all : accept units while sum(ask+fee) <= max_marginal_cost
    sell_all: accept units while sum(bid-fee) >= max_marginal_cost  (here: min revenue)
    """
    side_levels = []
    for tid in basket.token_ids:
        ob = books.get(tid)
        if ob is None:
            return None
        lv = ob.asks if direction == "buy_all" else ob.bids
        if not lv:
            return None
        side_levels.append(lv)

    n = len(side_levels)
    idx = [0] * n
    rem = [side_levels[i][0].size for i in range(n)]
    qty = 0.0
    notional = [0.0] * n
    fees = [0.0] * n
    worst = [0.0] * n

    while True:
        if any(idx[i] >= len(side_levels[i]) for i in range(n)):
            break
        prices = [side_levels[i][idx[i]].price for i in range(n)]
        unit_fees = [basket.fees[i].per_share(prices[i]) for i in range(n)]
        if direction == "buy_all":
            unit = sum(prices) + sum(unit_fees)
            if unit > max_marginal_cost:
                break
        else:
            unit = sum(prices) - sum(unit_fees)
            if unit < max_marginal_cost:
                break
        step = min(rem)
        if max_qty is not None:
            step = min(step, max_qty - qty)
        if step <= 1e-9:
            break
        qty += step
        for i in range(n):
            notional[i] += step * prices[i]
            fees[i] += step * unit_fees[i]
            worst[i] = prices[i]
            rem[i] -= step
            if rem[i] <= 1e-9:
                idx[i] += 1
                if idx[i] < len(side_levels[i]):
                    rem[i] = side_levels[i][idx[i]].size
        if max_qty is not None and qty >= max_qty - 1e-9:
            break

    if qty <= 0:
        return None
    return {"qty": qty, "notional": notional, "fees": fees, "worst": worst}


def build_opportunity(
    basket: Basket,
    books: Dict[str, OrderBook],
    direction: str,
    min_edge_bps: float,
    gas_usd: float = 0.0,
    max_qty: Optional[float] = None,
    now: Optional[float] = None,
) -> Optional[Opportunity]:
    now = time.time() if now is None else now
    edge = min_edge_bps / 10_000
    if direction == "buy_all":
        threshold = 1.0 / (1.0 + edge)          # cost per set such that (1-c)/c >= edge
    else:
        threshold = 1.0 + edge                  # revenue per $1 set
    w = walk_depth(basket, books, direction, threshold, max_qty)
    if not w:
        return None

    min_size = max(books[t].min_order_size for t in basket.token_ids)
    qty = _floor2(w["qty"])
    if qty < min_size:
        return None
    if qty != w["qty"]:  # re-walk with rounded qty for exact numbers
        w = walk_depth(basket, books, direction, threshold, qty)
        if not w:
            return None

    fees_usd = sum(w["fees"])
    gross = sum(w["notional"])
    side = "BUY" if direction == "buy_all" else "SELL"
    # Price slack: let each leg's limit move by an equal share of the remaining edge at the
    # worst filled level, so small re-quotes between detection and execution still fill
    # while the whole set stays at or above the minimum edge.
    n = len(basket.token_ids)
    worst_unit = sum(w["worst"][i] + (1 if direction == "buy_all" else -1) * basket.fees[i].per_share(w["worst"][i])
                     for i in range(n))
    room = (threshold - worst_unit) if direction == "buy_all" else (worst_unit - threshold)
    limits = []
    for i, t in enumerate(basket.token_ids):
        tick = books[t].tick_size or 0.01
        slack = math.floor(max(0.0, room) * 0.8 / n / tick + 1e-9) * tick
        lp = w["worst"][i] + slack if direction == "buy_all" else w["worst"][i] - slack
        limits.append(round(min(max(lp, 0.001), 0.999), 4))
    legs = [
        Leg(token_id=t, label=basket.labels[i], side=side, limit_price=limits[i],
            qty=qty, avg_price=w["notional"][i] / qty, fee_usd=w["fees"][i])
        for i, t in enumerate(basket.token_ids)
    ]
    if direction == "buy_all":
        capital = gross + fees_usd + gas_usd
        net = qty * 1.0 - capital
    else:
        capital = qty * 1.0 + gas_usd            # USDC needed to split
        net = gross - fees_usd - qty * 1.0 - gas_usd

    lockup_days = 0.0
    annualized = None
    if basket.kind == "negrisk":
        if basket.end_ts:
            lockup_days = max((basket.end_ts - now) / SECONDS_PER_DAY, 0.5)
        else:
            lockup_days = 365.0  # unknown -> punish
        annualized = (net / capital) * 365.0 / lockup_days if capital > 0 else None

    return Opportunity(
        basket=basket, direction=direction, qty=qty, legs=legs,
        gross_usd=gross, fees_usd=fees_usd, net_profit_usd=net, capital_usd=capital,
        edge_bps=(net / capital) * 10_000 if capital > 0 else 0.0,
        lockup_days=lockup_days, annualized=annualized, detected_ts=now,
    )


class Scanner:
    def __init__(self, scan_cfg: dict, fee_cfg: dict, universe_cfg: dict):
        self.min_edge_bps = float(scan_cfg["min_edge_bps"])
        self.min_profit = float(scan_cfg["min_profit_usd"])
        self.min_ann = float(scan_cfg.get("min_annualized_return", 0.0))
        self.gas = float(fee_cfg.get("merge_gas_usd", 0.0))
        self.max_days = float(universe_cfg.get("max_days_to_resolution", 3650))
        self.stats = {"baskets_scanned": 0, "raw_signals": 0}

    def directions(self, basket: Basket) -> List[str]:
        # sell_all requires splitting a full set; for negRisk that is not a single
        # atomic op, so we only trade buy_all there.
        return ["buy_all", "sell_all"] if basket.kind == "binary" else ["buy_all"]

    def raw_sum(self, basket: Basket, books: Dict[str, OrderBook], direction: str) -> Optional[float]:
        """Top-of-book sum (before fees) – useful for monitoring how close markets are."""
        vals = []
        for t in basket.token_ids:
            ob = books.get(t)
            if not ob:
                return None
            p = ob.best_ask if direction == "buy_all" else ob.best_bid
            if p is None:
                return None
            vals.append(p)
        return sum(vals)

    def scan(self, baskets: List[Basket], books: Dict[str, OrderBook], now: Optional[float] = None) -> List[Opportunity]:
        now = time.time() if now is None else now
        opps: List[Opportunity] = []
        for b in baskets:
            self.stats["baskets_scanned"] += 1
            if b.kind == "negrisk" and b.end_ts and (b.end_ts - now) / SECONDS_PER_DAY > self.max_days:
                continue
            for d in self.directions(b):
                s = self.raw_sum(b, books, d)
                if s is None:
                    continue
                if (d == "buy_all" and s >= 1.0) or (d == "sell_all" and s <= 1.0):
                    continue  # cheap pre-filter
                self.stats["raw_signals"] += 1
                opp = build_opportunity(b, books, d, self.min_edge_bps, self.gas, now=now)
                if not opp or opp.net_profit_usd < self.min_profit:
                    continue
                if opp.annualized is not None and opp.annualized < self.min_ann:
                    continue
                opps.append(opp)
        # rank by absolute profit; for lock-up trades by annualized return
        opps.sort(key=lambda o: (o.annualized if o.annualized is not None else 1e9, o.net_profit_usd), reverse=True)
        return opps
