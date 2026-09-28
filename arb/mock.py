"""Synthetic market for offline testing of the full pipeline.

NOT a model of real Polymarket profitability. It injects short-lived mispricings
(exponential lifetime) so we can verify detection, sizing, latency misses,
leg failures, unwinds and settlement end-to-end.
"""
from __future__ import annotations

import math
import random
from typing import Dict, List, Optional

from .models import Basket, FeeSpec, Level, OrderBook

CATS = [("politics", 0.04), ("sports", 0.05), ("crypto", 0.07), ("geopolitics", 0.0), ("culture", 0.05)]


class _Mis:
    def __init__(self, until: float, shift: float, depth: float, direction: str):
        self.until, self.shift, self.depth, self.direction = until, shift, depth, direction


class MockClient:
    def __init__(self, clock, n_binary=60, n_events=8, seed=7,
                 mis_rate_per_min=0.4, mean_life_s=1.0, basket_fail_prob=0.01):
        self.clock = clock
        self.rng = random.Random(seed)
        self.mis_rate = mis_rate_per_min / 60.0
        self.mean_life = mean_life_s
        self.fail_p = basket_fail_prob
        self._last_t = clock.now()
        self.fair: Dict[str, float] = {}
        self.partner: Dict[str, List[str]] = {}
        self.mis: Dict[str, _Mis] = {}
        self.baskets: List[Basket] = []
        self.resolved: Dict[str, float] = {}
        now = clock.now()
        for i in range(n_binary):
            cat, rate = self.rng.choice(CATS)
            y, n = f"B{i}Y", f"B{i}N"
            p = self.rng.uniform(0.05, 0.95)
            self.fair[y], self.fair[n] = p, 1 - p
            fee = FeeSpec(rate, 1.0)
            b = Basket(f"bin{i}", "binary", f"Mock binary #{i} ({cat})", [y, n], ["Yes", "No"],
                       [fee, fee], now + self.rng.uniform(5, 90) * 86400, cat)
            self.baskets.append(b)
        for j in range(n_events):
            k = self.rng.randint(3, 8)
            w = [self.rng.random() ** 2 for _ in range(k)]
            s = sum(w)
            toks = [f"E{j}_{m}" for m in range(k)]
            for t, wi in zip(toks, w):
                self.fair[t] = max(0.01, wi / s)
            cat, rate = self.rng.choice(CATS)
            fee = FeeSpec(rate, 1.0)
            b = Basket(f"event:{j}", "negrisk", f"Mock event #{j}: who wins? ({cat})", toks,
                       [f"Cand {m}" for m in range(k)], [fee] * k,
                       now + self.rng.uniform(0.2, 3.0) * 86400, cat)
            self.baskets.append(b)

    # -------------------------------------------------------------- dynamics
    def _evolve(self):
        now = self.clock.now()
        dt = max(0.0, now - self._last_t)
        self._last_t = now
        if dt == 0:
            return
        for b in self.baskets:
            # random walk on fair prices, renormalised so the set sums to 1
            vals = [max(0.01, self.fair[t] * math.exp(self.rng.gauss(0, 0.004 * math.sqrt(dt)))) for t in b.token_ids]
            s = sum(vals)
            for t, v in zip(b.token_ids, vals):
                self.fair[t] = min(0.99, max(0.01, v / s))
            # Poisson arrival of a mispricing
            if b.basket_id not in self.mis and self.rng.random() < 1 - math.exp(-self.mis_rate / len(self.baskets) * dt):
                direction = "buy_all" if (b.kind == "negrisk" or self.rng.random() < 0.75) else "sell_all"
                self.mis[b.basket_id] = _Mis(
                    until=now + self.rng.expovariate(1 / self.mean_life),
                    shift=self.rng.uniform(0.008, 0.035),
                    depth=self.rng.uniform(15, 250),
                    direction=direction)
        for k in [k for k, m in self.mis.items() if m.until < now]:
            del self.mis[k]

    def _book(self, b: Basket, t: str) -> OrderBook:
        p = self.fair[t]
        half = 0.01 if 0.1 < p < 0.9 else 0.005
        tick = 0.01 if 0.04 < p < 0.96 else 0.001
        ask0 = min(0.999, round(p + half, 3))
        bid0 = max(0.001, round(p - half, 3))
        asks = [Level(round(ask0 + i * tick, 3), self.rng.uniform(50, 1500)) for i in range(6)]
        bids = [Level(round(bid0 - i * tick, 3), self.rng.uniform(50, 1500)) for i in range(6) if bid0 - i * tick > 0]
        m = self.mis.get(b.basket_id)
        if m:
            n = len(b.token_ids)
            if m.direction == "buy_all":
                # cheap asks on every leg so that sum(ask) ≈ 1 - shift
                total_ask = sum(min(0.999, self.fair[x] + half) for x in b.token_ids)
                cut = (total_ask - (1 - m.shift)) / n
                px = round(max(0.001, ask0 - cut), 3)
                asks = [Level(px, m.depth * self.rng.uniform(0.6, 1.4))] + [l for l in asks if l.price > px]
                bids = [l for l in bids if l.price < px]
            else:
                total_bid = sum(max(0.001, self.fair[x] - half) for x in b.token_ids)
                lift = ((1 + m.shift) - total_bid) / n
                px = round(min(0.999, bid0 + lift), 3)
                bids = [Level(px, m.depth * self.rng.uniform(0.6, 1.4))] + [l for l in bids if l.price < px]
                asks = [l for l in asks if l.price > px]
        return OrderBook(t, bids, asks, 5.0, tick, self.clock.now())

    # -------------------------------------------------------------- client API
    def binary_baskets(self, *_a, **_k):
        return [b for b in self.baskets if b.kind == "binary"]

    def negrisk_baskets(self, *_a, **_k):
        return [b for b in self.baskets if b.kind == "negrisk"]

    def books(self, token_ids) -> Dict[str, OrderBook]:
        self._evolve()
        want = set(token_ids)
        out = {}
        for b in self.baskets:
            for t in b.token_ids:
                if t in want:
                    out[t] = self._book(b, t)
        return out

    def _resolve_basket(self, b: Basket):
        if b.token_ids[0] in self.resolved:
            return
        weights = [self.fair[t] for t in b.token_ids]
        winner = self.rng.choices(b.token_ids, weights=weights)[0]
        fail = self.rng.random() < self.fail_p
        for t in b.token_ids:
            self.resolved[t] = 0.0 if fail else (1.0 if t == winner else 0.0)

    def basket_resolution(self, token_ids) -> Optional[bool]:
        b = next(x for x in self.baskets if x.token_ids == list(token_ids))
        if b.end_ts and self.clock.now() < b.end_ts:
            return None
        self._resolve_basket(b)
        return any(self.resolved[t] == 1.0 for t in token_ids)

    def token_resolution(self, token_id) -> Optional[float]:
        b = next(x for x in self.baskets if token_id in x.token_ids)
        if b.end_ts and self.clock.now() < b.end_ts:
            return None
        self._resolve_basket(b)
        return self.resolved[token_id]
