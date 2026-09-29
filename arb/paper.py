"""Paper broker: simulates execution against a *fresh* order book fetched after
the configured latency, so edges that vanish in the meantime are counted as misses.

Accounting is cash-based:
  cash       – free USDC
  locked     – negRisk baskets held until resolution (valued at cost)
  residuals  – naked leftovers from failed legs (valued at best bid)
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .models import HELD_KINDS, ExecutionResult, FeeSpec, Fill, Level, Opportunity, OrderBook
from .risk import PortfolioView


@dataclass
class LockedBasket:
    basket_id: str
    title: str
    token_ids: List[str]
    qty: float
    cost: float
    end_ts: Optional[float]
    opened_ts: float


@dataclass
class Residual:
    token_id: str
    qty: float
    cost: float
    basket_id: str
    fee: Optional[FeeSpec] = None


@dataclass
class PaperPortfolio:
    cash: float
    locked: List[LockedBasket] = field(default_factory=list)
    residuals: Dict[str, Residual] = field(default_factory=dict)
    realized_pnl: float = 0.0
    residual_marks: Dict[str, float] = field(default_factory=dict)

    @property
    def locked_value(self) -> float:
        return sum(b.cost for b in self.locked)

    @property
    def residual_value(self) -> float:
        return sum(r.qty * self.residual_marks.get(t, 0.0) for t, r in self.residuals.items())

    @property
    def unhedged_cost(self) -> float:
        return sum(r.cost for r in self.residuals.values())

    @property
    def equity(self) -> float:
        return self.cash + self.locked_value + self.residual_value

    def view(self) -> PortfolioView:
        exp: Dict[str, float] = {}
        for b in self.locked:
            exp[b.basket_id] = exp.get(b.basket_id, 0.0) + b.cost
        for r in self.residuals.values():
            exp[r.basket_id] = exp.get(r.basket_id, 0.0) + r.cost
        return PortfolioView(self.equity, self.cash, self.locked_value, self.unhedged_cost, exp)


def _ioc(levels: List[Level], limit: Optional[float], qty: float, side: str, haircut: float
         ) -> Tuple[float, List[Tuple[float, float]]]:
    """Consume `levels` in place. Returns (filled, [(price, size), ...])."""
    filled, parts = 0.0, []
    for lv in levels:
        if filled >= qty - 1e-9:
            break
        if limit is not None:
            if side == "BUY" and lv.price > limit + 1e-9:
                break
            if side == "SELL" and lv.price < limit - 1e-9:
                break
        avail = lv.size * haircut
        take = min(avail, qty - filled)
        if take <= 0:
            continue
        parts.append((lv.price, take))
        lv.size -= take
        filled += take
    return filled, parts


class ConsumedLiquidity:
    """Liquidity our paper fills took out of the market.

    Simulated orders never reach Polymarket, so the live books keep showing the orders we
    'filled'. Without this ledger the next update would find – and fill – the same resting
    orders again. Consumed size is subtracted from a price level until the level disappears
    from the real book (the orders are gone anyway) or `ttl_s` passes (makers re-quote).
    """

    def __init__(self, ttl_s: float = 60.0):
        self.ttl = ttl_s
        self._m: Dict[str, Dict[Tuple[str, float], Tuple[float, float]]] = {}

    def add(self, items, now: float) -> None:
        for tid, side, price, size in items:
            d = self._m.setdefault(tid, {})
            key = (side, round(price, 6))
            q = d.get(key, (0.0, 0.0))[0]
            d[key] = (q + size, now + self.ttl)

    def apply(self, books: Dict[str, OrderBook], now: float) -> Dict[str, OrderBook]:
        if not self._m:
            return books
        out = dict(books)
        for tid in list(self._m):
            d = self._m[tid]
            for k in [k for k, (_, exp) in d.items() if exp <= now]:
                del d[k]
            ob = books.get(tid)
            if ob is not None:
                new = copy.copy(ob)
                for side in ("bids", "asks"):
                    live = {round(lv.price, 6) for lv in getattr(ob, side)}
                    for k in [k for k in d if k[0] == side and k[1] not in live]:
                        del d[k]
                    levels = []
                    for lv in getattr(ob, side):
                        left = lv.size - d.get((side, round(lv.price, 6)), (0.0, 0.0))[0]
                        if left > 1e-9:
                            levels.append(Level(lv.price, left))
                    setattr(new, side, levels)
                out[tid] = new
            if not d:
                del self._m[tid]
        return out


def _cost_first(parts: List[Tuple[float, float]], m: float) -> float:
    """Notional of the first m shares of a fill (fills walk best-first)."""
    tot, left = 0.0, m
    for p, s in parts:
        t = min(s, left)
        tot += p * t
        left -= t
        if left <= 1e-12:
            break
    return tot


class PaperBroker:
    def __init__(self, cfg_exec: dict, gas_usd: float, starting_cash: float):
        self.haircut = float(cfg_exec.get("depth_haircut", 1.0))
        self.unwind = bool(cfg_exec.get("unwind_on_leg_failure", True))
        self.sequential = cfg_exec.get("leg_mode", "sequential") == "sequential"
        self.repair = bool(cfg_exec.get("repair_legs", True))
        self.gas = gas_usd
        self.pf = PaperPortfolio(cash=starting_cash)
        self._consumed: List[tuple] = []

    # -------------------------------------------------------------- helpers
    def _fee(self, opp: Opportunity, i: int, parts) -> float:
        spec = opp.basket.fees[i]
        return sum(s * spec.per_share(p) for p, s in parts)

    def _mark_consumed(self, token_id: str, side: str, parts, haircut: float) -> None:
        # With a haircut we only get `haircut` of each level because faster bots take the rest,
        # so the level we touched is gone as a whole: record take / haircut.
        for p, sz in parts:
            self._consumed.append((token_id, side, p, sz / haircut if haircut > 0 else sz))

    def _sell_unwind(self, opp, i, books, token_id, qty) -> Tuple[float, float, Fill]:
        """Market-sell qty into the (already consumed) bid book. Returns (sold, proceeds_net, fill)."""
        ob = books.get(token_id)
        if not ob or qty <= 0:
            return 0.0, 0.0, Fill(token_id, "SELL", 0.0, 0.0, 0.0)
        sold, parts = _ioc(ob.bids, None, qty, "SELL", 1.0)
        self._mark_consumed(token_id, "bids", parts, 1.0)
        fee = self._fee(opp, i, parts)
        notional = sum(p * s for p, s in parts)
        return sold, notional - fee, Fill(token_id, "SELL", sold, notional / sold if sold else 0.0, fee)

    def _add_residual(self, token_id, qty, cost, basket_id, fee: Optional[FeeSpec] = None):
        r = self.pf.residuals.get(token_id)
        if r:
            r.qty += qty
            r.cost += cost
        else:
            self.pf.residuals[token_id] = Residual(token_id, qty, cost, basket_id, fee)

    def unwind_residuals(self, books: Dict[str, OrderBook]) -> List[Tuple[str, float, float]]:
        """Retry flattening naked leftovers each cycle. Returns [(token, qty_sold, pnl)]."""
        out = []
        if not self.unwind:
            return out
        for t, r in list(self.pf.residuals.items()):
            ob = books.get(t)
            if not ob or not ob.bids:
                continue
            bids = copy.deepcopy(ob.bids)
            sold, parts = _ioc(bids, None, r.qty, "SELL", 1.0)
            if sold <= 1e-9:
                continue
            fee = sum(sz * (r.fee.per_share(p) if r.fee else 0.0) for p, sz in parts)
            proceeds = sum(p * sz for p, sz in parts) - fee
            cost_part = r.cost * sold / r.qty
            self.pf.cash += proceeds
            pnl = proceeds - cost_part
            self.pf.realized_pnl += pnl
            r.qty -= sold
            r.cost -= cost_part
            if r.qty <= 1e-6:
                del self.pf.residuals[t]
                self.pf.residual_marks.pop(t, None)
            out.append((t, sold, pnl))
        return out

    # -------------------------------------------------------------- execute
    def execute(self, opp: Opportunity, fetch: Callable[[List[str]], Dict[str, OrderBook]], now: float
                ) -> ExecutionResult:
        """`fetch(token_ids)` returns FRESH books (the engine waits the latency before calling).

        sequential mode: fire the scarcest leg first (IOC), then size the other legs
        to what actually filled – a failed first leg costs nothing.
        parallel mode:   all legs fire at once on the same snapshot (faster, more legging risk).
        """
        tids = opp.basket.token_ids
        books1 = copy.deepcopy(fetch(tids))
        if any(t not in books1 for t in tids):
            return ExecutionResult(opp, "missed", 0.0, note="book unavailable")
        if not self.sequential:
            order = list(range(len(opp.legs)))
            return self._run(opp, books1, books1, order, now, fetch)
        order = sorted(range(len(opp.legs)), key=lambda i: self._depth_ratio(opp, i, books1))
        books2 = copy.deepcopy(fetch([t for j, t in enumerate(tids) if j != order[0]]))
        books2[tids[order[0]]] = books1[tids[order[0]]]
        if any(t not in books2 for t in tids):
            books2 = books1
        return self._run(opp, books1, books2, order, now, fetch)

    def _depth_ratio(self, opp: Opportunity, i: int, books) -> float:
        leg = opp.legs[i]
        ob = books[leg.token_id]
        lv = ob.asks if leg.side == "BUY" else ob.bids
        ok = [l.size for l in lv if (l.price <= leg.limit_price + 1e-9 if leg.side == "BUY"
                                     else l.price >= leg.limit_price - 1e-9)]
        return sum(ok) * self.haircut / leg.qty

    def _repair(self, opp, latest, done, parts_by_leg, fills, fetch) -> float:
        """Some legs came up short: try to buy the missing shares instead of dumping the rest.

        Dumping keeps min(done) sets and sells every other leg's excess into its bid; completing
        keeps max(done) sets. Completing is worth it while its cost stays below
            budget = (max - min) * payout - proceeds_of_dumping,
        so a repair never ends worse than the dump would have. Returns shares bought.
        """
        n, hi, lo = len(done), max(done), min(done)
        dump = 0.0
        for i in range(n):
            ob = latest.get(opp.legs[i].token_id)
            if done[i] - lo > 1e-9 and ob:
                _, parts = _ioc(copy.deepcopy(ob.bids), None, done[i] - lo, "SELL", 1.0)
                dump += sum(p * s for p, s in parts) - self._fee(opp, i, parts)
        budget = (hi - lo) * opp.basket.payout - dump
        short = sorted((i for i in range(n) if done[i] < hi - 1e-9), key=lambda i: done[i])
        if budget <= 0 or not short:
            return 0.0
        # the repair orders go out one round later -> fresh books, minus what we just took
        books = copy.deepcopy(fetch([opp.legs[i].token_id for i in short]))
        taken = ConsumedLiquidity(ttl_s=1e9)
        taken.add(self._consumed, 0.0)
        books = taken.apply(books, 0.0)
        bought = 0.0
        for k, i in enumerate(short):
            leg, spec = opp.legs[i], opp.basket.fees[i]
            ob = books.get(leg.token_id)
            if ob is None:
                continue
            # split what is left of the budget over the remaining short legs by their detected cost
            ref = {j: opp.legs[j].avg_price + opp.legs[j].fee_usd / opp.qty for j in short[k:]}
            weight = sum((hi - done[j]) * ref[j] for j in short[k:])
            allin = budget * ref[i] / weight if weight > 0 else 0.0
            ok = [lv.price for lv in ob.asks if lv.price + spec.per_share(lv.price) <= allin + 1e-12]
            if not ok:
                continue
            q, parts = _ioc(ob.asks, max(ok), hi - done[i], "BUY", self.haircut)
            if q <= 1e-9:
                continue
            self._mark_consumed(leg.token_id, "asks", parts, self.haircut)
            fee = self._fee(opp, i, parts)
            notional = sum(p * s for p, s in parts)
            self.pf.cash -= notional + fee
            budget -= notional + fee
            old = fills[i]
            tot_q = old.qty + q
            fills[i] = Fill(leg.token_id, "BUY", tot_q, (old.avg_price * old.qty + notional) / tot_q,
                            old.fee_usd + fee)
            parts_by_leg[i] = parts_by_leg[i] + parts
            done[i] += q
            bought += q
        return bought

    def _run(self, opp, books1, books2, order, now, fetch=None) -> ExecutionResult:
        buy = opp.direction == "buy_all"
        cash0 = self.pf.cash
        self._consumed: List[tuple] = []
        n = len(opp.legs)
        Q = opp.qty
        if not buy:
            self.pf.cash -= Q * 1.0 + self.gas               # split USDC -> full sets
        fills: List[Optional[Fill]] = [None] * n
        parts_by_leg: List[list] = [[] for _ in range(n)]
        done = [0.0] * n
        target = Q
        for k, i in enumerate(order):
            leg = opp.legs[i]
            bk = books1 if k == 0 else books2
            ob = bk[leg.token_id]
            q, parts = _ioc(ob.asks if buy else ob.bids, leg.limit_price, target, leg.side, self.haircut)
            self._mark_consumed(leg.token_id, "asks" if buy else "bids", parts, self.haircut)
            fee = self._fee(opp, i, parts)
            notional = sum(p * s for p, s in parts)
            self.pf.cash += (-(notional + fee)) if buy else (notional - fee)
            fills[i] = Fill(leg.token_id, leg.side, q, notional / q if q else 0.0, fee)
            parts_by_leg[i], done[i] = parts, q
            if self.sequential and k == 0:
                target = q                                     # size the rest to what filled
                if q <= 1e-9:
                    break                                      # first leg missed -> nothing else sent
        latest = books2
        repaired = 0.0
        if buy and self.repair and fetch is not None and all(f is not None for f in fills) \
                and max(done) - min(done) > 1e-9:
            repaired = self._repair(opp, latest, done, parts_by_leg, fills, fetch)
        fills_final = [f for f in fills if f is not None]

        unwind_fills, residual_usd = [], 0.0
        locked = payout = 0.0
        if buy:
            matched = min(done)
            min_size = max(latest[t].min_order_size for t in opp.basket.token_ids)
            if 0 < matched < min_size and opp.basket.kind in HELD_KINDS:
                matched = 0.0
            matched_cost = 0.0
            for i in range(n):
                if done[i]:
                    matched_cost += _cost_first(parts_by_leg[i], matched) + fills[i].fee_usd * matched / done[i]
            for i, leg in enumerate(opp.legs):
                excess = done[i] - matched
                if excess <= 1e-9:
                    continue
                total_cost = sum(p * s for p, s in parts_by_leg[i]) + fills[i].fee_usd
                excess_cost = total_cost - (_cost_first(parts_by_leg[i], matched) + fills[i].fee_usd * matched / done[i])
                left = excess
                if self.unwind:
                    sold, proceeds, uf = self._sell_unwind(opp, i, latest, leg.token_id, excess)
                    self.pf.cash += proceeds
                    unwind_fills.append(uf)
                    left = excess - sold
                if left > 1e-9:
                    c = excess_cost * left / excess
                    self._add_residual(leg.token_id, left, c, opp.basket.basket_id, opp.basket.fees[i])
                    residual_usd += c
            if matched > 0:
                if opp.basket.kind in ("binary", "negrisk_no"):
                    # binary: merge YES+NO -> $1; negRisk NO set: convert n NO -> $n-1, both instantly
                    self.pf.cash += matched * opp.basket.payout - self.gas
                else:
                    locked = matched_cost + self.gas
                    self.pf.cash -= self.gas
                    payout = matched
                    self.pf.locked.append(LockedBasket(opp.basket.basket_id, opp.basket.title,
                                                       opp.basket.token_ids, matched, locked,
                                                       opp.basket.end_ts, now))
        else:
            matched = min(done)
            leftovers = [Q - d for d in done]
            m = min(leftovers)
            if m > 1e-9:
                self.pf.cash += m                                  # merge unsold pairs back
            for i, leg in enumerate(opp.legs):
                extra = leftovers[i] - m
                if extra <= 1e-9:
                    continue
                left = extra
                if self.unwind:
                    s2, proceeds, uf = self._sell_unwind(opp, i, latest, leg.token_id, extra)
                    self.pf.cash += proceeds
                    unwind_fills.append(uf)
                    left = extra - s2
                if left > 1e-9:
                    mark = latest[leg.token_id].best_bid or 0.0
                    self._add_residual(leg.token_id, left, left * mark, opp.basket.basket_id, opp.basket.fees[i])
                    residual_usd += left * mark

        # binary: locked profit minus unwind slippage; negRisk: only unwind slippage
        # (locked baskets and residuals are still held and valued separately)
        realized = (self.pf.cash - cash0) + locked + residual_usd
        self.pf.realized_pnl += realized
        status = "filled" if matched >= Q - 1e-6 else ("partial" if matched > 0 else "missed")
        return ExecutionResult(opp, status, matched, fills_final, unwind_fills,
                               realized_pnl=realized, locked_capital=locked, expected_payout=payout,
                               residual_exposure_usd=residual_usd,
                               note=f"legs={[round(d, 2) for d in done]} order={order}"
                                    + (f" repaired={repaired:.2f}" if repaired else ""),
                               consumed=self._consumed)

    # -------------------------------------------------------------- persistence
    def save(self, path: str) -> None:
        import dataclasses, json, os
        pf = self.pf
        data = {
            "cash": pf.cash, "realized_pnl": pf.realized_pnl, "residual_marks": pf.residual_marks,
            "locked": [dataclasses.asdict(b) for b in pf.locked],
            "residuals": {t: {**dataclasses.asdict(r), "fee": dataclasses.asdict(r.fee) if r.fee else None}
                          for t, r in pf.residuals.items()},
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)

    def load(self, path: str) -> bool:
        import json, os
        if not os.path.exists(path):
            return False
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        self.pf = PaperPortfolio(cash=d["cash"], realized_pnl=d["realized_pnl"],
                                 residual_marks=d.get("residual_marks", {}))
        self.pf.locked = [LockedBasket(**b) for b in d.get("locked", [])]
        for t, r in d.get("residuals", {}).items():
            fee = FeeSpec(**r.pop("fee")) if r.get("fee") else None
            r.pop("fee", None)
            self.pf.residuals[t] = Residual(**r, fee=fee)
        return True

    # -------------------------------------------------------------- lifecycle
    def mark_residuals(self, books: Dict[str, OrderBook]) -> None:
        for t in self.pf.residuals:
            ob = books.get(t)
            if ob and ob.best_bid is not None:
                self.pf.residual_marks[t] = ob.best_bid

    def settle_basket(self, b: LockedBasket, paid_out) -> float:
        """paid_out: True/False for exactly-one-pays sets, or USDC per set (a ladder pays 1 or 2)."""
        payout = b.qty * float(paid_out)
        self.pf.cash += payout
        pnl = payout - b.cost
        self.pf.realized_pnl += pnl
        self.pf.locked.remove(b)
        return pnl

    def settle_residual(self, token_id: str, final_price: float) -> float:
        r = self.pf.residuals.pop(token_id)
        self.pf.residual_marks.pop(token_id, None)
        payout = r.qty * final_price
        self.pf.cash += payout
        pnl = payout - r.cost
        self.pf.realized_pnl += pnl
        return pnl
