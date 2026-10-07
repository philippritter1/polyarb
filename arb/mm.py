"""Paper market making with Polymarket liquidity rewards.

Instead of guessing which side wins, quote both sides of quiet markets that pay liquidity rewards and earn
  spread    – a YES bid and a NO bid below the mid; a filled YES + a filled NO merge into $1
  rewards   – Polymarket pays a daily pool per market to resting orders near the mid (quadratic in the
              distance, share of all qualifying orders); estimated here from the live books
  rebates   – makers pay no fee and get a share of the taker fee back (25 %, sports 15 %, crypto 20 %)
and lose whatever informed traders take from the quotes (adverse selection) – that trade-off is the test.

Fills (strict, independent of how the feed labels the trade side): a resting bid at price b is filled by a
trade on its token BELOW b, or by a trade on the complementary token ABOVE 1 - b – that seller/buyer would
have met our order first. A trade AT our price is not counted (the queue in front may have taken it).
Only trades after the order rested for `latency_s` count; each order is filled at most by the trade's size.

Reward estimate per sample (Polymarket's published scoring): an order of size x at distance s < v from the
mid scores ((v - s) / v)^2 * x. Side one = YES bids + NO asks, side two = YES asks + NO bids;
Q = max(min(Q1, Q2), max(Q1, Q2) / 3) when 0.10 <= mid <= 0.90, else min(Q1, Q2). Our share is
Q_ours / (Q_ours + Q_others), the others read from the book. Per sample: daily_rate * share * dt / 1 day.
The real payout depends on everyone's orders over the day, so this is an estimate, booked separately.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

from .client import FeeResolver, _parse_json_list, _parse_ts
from .scenarios import _iso
from .models import Basket, ExecutionResult, FeeSpec, Fill, Leg, Opportunity, OrderBook
from .storage import Store

log = logging.getLogger("polyarb.mm")
DAY = 86_400
REBATE_SHARE = {"Sport": 0.15, "Krypto": 0.20}  # rest: 0.25


@dataclass
class Quote:
    token: str
    price: float
    size: float          # shares still open
    placed_ts: float


@dataclass
class MMMarket:
    cid: str
    title: str
    yes: str
    no: str
    end_ts: Optional[float]
    daily_rate: float    # reward pool $/day
    max_spread: float    # qualifying distance from the mid, in price units (3.5 cents -> 0.035)
    min_size: float      # qualifying order size, shares
    category: str
    fee_rate: float = 0.0
    fee_exp: float = 1.0
    bid_yes: Optional[dict] = None
    bid_no: Optional[dict] = None
    qty_yes: float = 0.0
    qty_no: float = 0.0
    cost_yes: float = 0.0
    cost_no: float = 0.0
    mid: Optional[float] = None


@dataclass
class MMState:
    cash: float
    markets: Dict[str, MMMarket] = field(default_factory=dict)
    realized: float = 0.0        # merges + resolutions
    rewards: float = 0.0         # estimated liquidity rewards
    rewards_unbooked: float = 0.0
    rebates: float = 0.0
    fills: int = 0

    def inventory_value(self) -> float:
        return sum(m.qty_yes * (m.mid if m.mid is not None else 0.5) + m.qty_no * (1 - (m.mid if m.mid is not None else 0.5))
                   for m in self.markets.values())

    def inventory_cost(self) -> float:
        return sum(m.cost_yes + m.cost_no for m in self.markets.values())

    def reserved(self) -> float:
        return sum(q["price"] * q["size"] for m in self.markets.values() for q in (m.bid_yes, m.bid_no) if q)

    @property
    def equity(self) -> float:
        return self.cash + self.inventory_value()

    def save(self, path: str) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dict(cash=self.cash, realized=self.realized, rewards=self.rewards,
                           rewards_unbooked=self.rewards_unbooked, rebates=self.rebates, fills=self.fills,
                           markets=[asdict(m) for m in self.markets.values()]), f)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str, cash: float) -> "MMState":
        if not os.path.exists(path):
            return cls(cash=cash)
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        st = cls(cash=d["cash"], realized=d.get("realized", 0.0), rewards=d.get("rewards", 0.0),
                 rewards_unbooked=d.get("rewards_unbooked", 0.0), rebates=d.get("rebates", 0.0),
                 fills=d.get("fills", 0))
        for m in d.get("markets", []):
            st.markets[m["cid"]] = MMMarket(**m)
        return st


def score(levels: List[Tuple[float, float]], mid: float, v: float, buy: bool, min_size: float = 0.0) -> float:
    """Reward score of book levels (price, size) on one side: ((v - s) / v)^2 * size within v of the mid."""
    out = 0.0
    for p, x in levels:
        s = (mid - p) if buy else (p - mid)
        if 0 <= s < v and x >= min_size:
            out += ((v - s) / v) ** 2 * x
    return out


def q_min(q1: float, q2: float, mid: float, c: float = 3.0) -> float:
    if 0.10 <= mid <= 0.90:
        return max(min(q1, q2), max(q1, q2) / c)
    return min(q1, q2)


def reward_share(yes: OrderBook, no: OrderBook, ours_yes: Optional[dict], ours_no: Optional[dict], mid: float,
                 v: float, min_size: float) -> float:
    """Our share of the market's reward pool right now. YES bids + NO asks are one side (NO asks at q are YES
    bids at 1 - q), YES asks + NO bids the other. Our quotes are bids on YES and on NO."""
    def lv(book_levels, flip=False):
        return [((1 - l.price) if flip else l.price, l.size) for l in book_levels]
    side1 = lv(yes.bids) + lv(no.asks, flip=True)   # buy YES
    side2 = lv(yes.asks) + lv(no.bids, flip=True)   # sell YES
    others = q_min(score(side1, mid, v, True, min_size), score(side2, mid, v, False, min_size), mid)
    o1 = score([(ours_yes["price"], ours_yes["size"])], mid, v, True, min_size) if ours_yes else 0.0
    o2 = score([(1 - ours_no["price"], ours_no["size"])], mid, v, False, min_size) if ours_no else 0.0
    ours = q_min(o1, o2, mid)
    return ours / (ours + others) if ours > 0 else 0.0


def rewards_of(m: dict) -> Tuple[float, float, float]:
    """(daily pool $, max spread as price, min size) from a Gamma market; 0 pool if it pays none."""
    rate = 0.0
    for r in m.get("clobRewards") or []:
        try:
            rate += float(r.get("rewardsDailyRate") or 0)
        except (TypeError, ValueError):
            pass
    try:
        v = float(m.get("rewardsMaxSpread") or 0) / 100  # cents
        size = float(m.get("rewardsMinSize") or 0)
    except (TypeError, ValueError):
        v, size = 0.0, 0.0
    return rate, v, size


class MarketMaker:
    def __init__(self, name: str, cfg: dict, client, clock=None, books=None, pool=None):
        from .engine import RealClock
        from .scenarios import prepare_scenario_dir
        self.name, self.cfg, self.client = name, cfg, client
        self.sc = sc = cfg["scenarios"][name]
        self.clock = clock or RealClock()
        self.start_capital = float(sc.get("capital_usd", 2500))
        data_dir = os.path.dirname(cfg["storage"]["db_path"]) or "."
        prepare_scenario_dir(data_dir, name, self.start_capital, str(sc.get("reset", "")))
        self.store = Store(os.path.join(data_dir, f"scenario-{name}.sqlite"))
        self.state_path = os.path.join(data_dir, f"scenario-{name}.json")
        self.st = MMState.load(self.state_path, self.start_capital)
        self.fees = getattr(client, "fees", None) or FeeResolver({})
        if books is None:
            from .stream import BookStore, StreamPool
            books = BookStore()
            pool = pool or StreamPool(books)
        self.books, self.pool = books, pool
        self.latency = float(sc.get("latency_s", 1.0))
        self.tick = float(sc.get("tick", 0.01))
        self._last = dict(select=-1e18, sample=None, equity=-1e18, settle=-1e18, book=-1e18)
        self.gw = None  # order gateway (arb/orders.py): mirrors the quotes at pilot size, dry run only for now
        if sc.get("live"):
            from .orders import Gateway
            self.gw = Gateway(name, self.store.db, os.path.join(data_dir, "live.json"),
                              os.path.join(data_dir, f"live-status-{name}.json"), sc["live"])
        self.scan: dict = {}

    # ------------------------------------------------------------------ market choice
    def select(self, now: float) -> None:
        """Quiet two-way markets that pay rewards: no sport or crypto (prices jump on news), far from the end,
        mid 0.15-0.85, a qualifying spread of at least 2 ticks. The largest pools first – or, with
        rank: share, the largest EXPECTED reward: pool x our share against the orders already in the book.
        max_age_days keeps only markets started within that many days (early, little competition)."""
        sc = self.sc
        min_days = float(sc.get("min_days_to_end", 3))
        skip = set(sc.get("skip_categories", ["Sport", "Krypto"]))
        lo, hi = float(sc.get("min_mid", 0.15)), float(sc.get("max_mid", 0.85))
        max_age = sc.get("max_age_days")
        params = {"active": "true", "closed": "false", "liquidity_num_min": sc.get("min_liquidity", 1000),
                  "order": "volume", "ascending": "false"}
        if max_age:
            params["start_date_min"] = _iso(now - float(max_age) * DAY)
        markets = self.client.paged("/markets", params, max_items=int(sc.get("max_listed", 2000)))
        from .study import categorize
        st = {"gelistet": len(markets)}
        cands = []
        for m in markets:
            rate, v, size = rewards_of(m)
            toks = _parse_json_list(m.get("clobTokenIds"))
            prices = _parse_json_list(m.get("outcomePrices"))
            end = _parse_ts(m.get("endDate"))
            why = None
            if rate < float(sc.get("min_daily_rate", 5)) or v < 2 * self.tick:
                why = "keine/kleine Rewards"
            elif len(toks) != 2 or len(prices) != 2 or not m.get("enableOrderBook"):
                why = "nicht binär"
            elif not end or end < now + min_days * DAY:
                why = "endet zu bald"
            elif categorize(m) in skip:
                why = "Kategorie ausgelassen"
            elif not lo <= float(prices[0]) <= hi:
                why = "Preis zu extrem"
            elif max_age and (_parse_ts(m.get("startDate") or m.get("createdAt")) or 0) < now - float(max_age) * DAY:
                why = "älter als max_age_days"
            if why:
                st[why] = st.get(why, 0) + 1
                continue
            cands.append((rate, m, v, size, end, toks))
        cands.sort(key=lambda x: -x[0])
        if sc.get("rank") == "share" and cands:
            cands = self._rank_by_share(cands[:int(sc.get("prefilter", 60))], st)
        want = int(sc.get("max_markets", 15))
        keep = {cid for cid, mk in self.st.markets.items() if mk.qty_yes > 1e-9 or mk.qty_no > 1e-9}
        chosen = {}
        for rate, m, v, size, end, toks in cands:
            if len(chosen) >= want:
                break
            cid = str(m.get("conditionId") or m.get("id"))
            fee = self.fees.resolve(m, categorize(m).lower())
            old = self.st.markets.get(cid)
            mk = old or MMMarket(cid, (m.get("question") or "")[:200], str(toks[0]), str(toks[1]), end, rate, v, size,
                                 categorize(m), fee.rate, fee.exponent)
            mk.daily_rate, mk.max_spread, mk.min_size, mk.end_ts = rate, v, size, end
            chosen[cid] = mk
        for cid in list(self.st.markets):
            if cid not in chosen:
                mk = self.st.markets[cid]
                mk.bid_yes = mk.bid_no = None  # no longer quoted
                if cid in keep:
                    chosen[cid] = mk  # holds inventory: keep until resolution
        self.st.markets = chosen
        st["ausgewählt"] = len(chosen)
        st["Reward-Pool $/Tag"] = round(sum(m.daily_rate for m in chosen.values()), 2)
        self.scan = st
        if self.pool is not None:
            self.pool.set_assets([t for m in chosen.values() for t in (m.yes, m.no)])

    def planned_quotes(self, mid: float, v: float, min_size: float) -> Tuple[dict, dict]:
        """The two bids quote() would place at this mid (see there)."""
        d = max(self.tick, v * float(self.sc.get("quote_frac", 0.5)))
        usd = float(self.sc.get("quote_usd", 50))
        out = []
        for price in (math.floor((mid - d) / self.tick + 1e-9) * self.tick,
                      math.floor((1 - mid - d) / self.tick + 1e-9) * self.tick):
            out.append(dict(price=round(price, 4), size=float(max(min_size, math.floor(usd / price)))) if price >= self.tick
                       else None)
        return out[0], out[1]

    def _rank_by_share(self, cands: list, st: dict) -> list:
        """Expected reward $/day = pool x our share of the score against the orders already resting (REST books)."""
        books = self.client.books([t for c in cands for t in c[5]])
        scored = []
        for c in cands:
            rate, m, v, size, end, toks = c
            by, bn = books.get(str(toks[0])), books.get(str(toks[1]))
            if not by or not bn or not by.bids or not by.asks:
                st["kein Buch"] = st.get("kein Buch", 0) + 1
                continue
            if by.best_ask - by.best_bid > 2 * v:
                st["Buch zu breit"] = st.get("Buch zu breit", 0) + 1  # quote() would not place bids there
                continue
            mid = (by.best_bid + by.best_ask) / 2
            qy, qn = self.planned_quotes(mid, v, size)
            scored.append((rate * reward_share(by, bn, qy, qn, mid, v, size), c))
        scored.sort(key=lambda x: -x[0])
        want = int(self.sc.get("max_markets", 15))
        st["erwarteter Reward $/Tag"] = round(sum(e for e, _ in scored[:want]), 2)
        return [c for _, c in scored]

    # ------------------------------------------------------------------ fills
    def _fill(self, mk: MMMarket, q: dict, side: str, qty: float, now: float) -> None:
        price = q["price"]
        cost = price * qty
        self.st.cash -= cost
        if side == "yes":
            mk.qty_yes += qty
            mk.cost_yes += cost
        else:
            mk.qty_no += qty
            mk.cost_no += cost
        q["size"] -= qty
        self.st.fills += 1
        taker_fee = mk.fee_rate * (price * (1 - price)) ** mk.fee_exp * qty
        rebate = taker_fee * REBATE_SHARE.get(mk.category, 0.25)
        self.st.rebates += rebate
        self.st.cash += rebate
        tok = mk.yes if side == "yes" else mk.no
        basket = Basket(f"{self.name}:{mk.cid}", self.name, mk.title, [tok], [side.upper()], [FeeSpec()],
                        end_ts=mk.end_ts, category="", delay_s=0.0)
        leg = Leg(tok, side.upper(), "BUY", price, qty, price, 0.0)
        opp = Opportunity(basket, "mm", qty, [leg], gross_usd=cost, fees_usd=0.0, net_profit_usd=0.0, capital_usd=cost,
                          edge_bps=0.0, lockup_days=0.0, detected_ts=q["placed_ts"])
        res = ExecutionResult(opp, "filled", qty, [Fill(tok, "BUY", qty, price, 0.0)], locked_capital=cost,
                              expected_payout=0.0,
                              note=f"maker Gebot {side.upper()} @ {price:.3f}, Mitte {mk.mid or 0:.3f}, Rebate {rebate:.4f} $")
        self.store.execution(now, res, 0.0)
        self._merge(mk, now)

    def _merge(self, mk: MMMarket, now: float) -> None:
        """A YES and a NO share together are worth $1 at once (merge): book the spread earned."""
        n = math.floor(min(mk.qty_yes, mk.qty_no) * 100) / 100
        if n <= 0:
            return
        c_yes = mk.cost_yes * n / mk.qty_yes
        c_no = mk.cost_no * n / mk.qty_no
        pnl = n - c_yes - c_no
        mk.qty_yes -= n
        mk.qty_no -= n
        mk.cost_yes -= c_yes
        mk.cost_no -= c_no
        self.st.cash += n
        self.st.realized += pnl
        self.store.settlement(now, f"{self.name}:{mk.cid}", "merge", n, n, pnl)

    def process_trades(self, now: float) -> int:
        """Fill resting quotes from the trades seen since the last step (see module doc)."""
        by_tok = {}
        for mk in self.st.markets.values():
            by_tok[mk.yes] = (mk, "yes")
            by_tok[mk.no] = (mk, "no")
        n = 0
        trades = self.books.trades
        while trades:
            tok, price, size, ts = trades.popleft()
            hit = by_tok.get(tok)
            if not hit:
                continue
            mk, which = hit
            own, other = (mk.bid_yes, mk.bid_no) if which == "yes" else (mk.bid_no, mk.bid_yes)
            own_side, other_side = (which, "no" if which == "yes" else "yes")
            if own and ts >= own["placed_ts"] + self.latency and price < own["price"] - 1e-9 and own["size"] > 0:
                q = min(own["size"], size)
                self._fill(mk, own, own_side, q, now)
                n += 1
            elif other and ts >= other["placed_ts"] + self.latency and price > 1 - other["price"] + 1e-9 \
                    and other["size"] > 0:
                q = min(other["size"], size)
                self._fill(mk, other, other_side, q, now)
                n += 1
        return n

    # ------------------------------------------------------------------ quoting
    def quote(self, now: float, books: Dict[str, OrderBook]) -> None:
        sc = self.sc
        frac = float(sc.get("quote_frac", 0.5))      # distance from the mid as a share of the qualifying spread
        usd = float(sc.get("quote_usd", 50))          # per side
        max_inv = float(sc.get("max_inventory_usd", 150))
        stop_h = float(sc.get("stop_hours_before_end", 24)) * 3600
        cash_free = self.st.cash - float(sc.get("cash_buffer_pct", 0.10)) * self.st.equity
        for mk in self.st.markets.values():
            by, bn = books.get(mk.yes), books.get(mk.no)
            if not by or not by.bids or not by.asks or (mk.end_ts and now > mk.end_ts - stop_h) or mk.daily_rate <= 0:
                mk.bid_yes = mk.bid_no = None
                continue
            mid = (by.best_bid + by.best_ask) / 2
            mk.mid = mid
            if by.best_ask - by.best_bid > 2 * mk.max_spread:
                mk.bid_yes = mk.bid_no = None  # book too wide to qualify: nobody to quote against
                continue
            d = max(self.tick, mk.max_spread * frac)
            want_yes = math.floor((mid - d) / self.tick + 1e-9) * self.tick
            want_no = math.floor((1 - mid - d) / self.tick + 1e-9) * self.tick
            want_yes = min(want_yes, by.best_ask - self.tick)
            if bn and bn.asks:
                want_no = min(want_no, bn.best_ask - self.tick)
            net = (mk.qty_yes - mk.qty_no) * (mid if mk.qty_yes >= mk.qty_no else 1 - mid)
            for side, price in (("yes", want_yes), ("no", want_no)):
                cur = mk.bid_yes if side == "yes" else mk.bid_no
                long_this = (side == "yes" and mk.qty_yes > mk.qty_no) or (side == "no" and mk.qty_no > mk.qty_yes)
                if price < self.tick or (long_this and abs(net) >= max_inv):
                    new = None  # skew: stop buying the side we already hold too much of
                else:
                    size = max(mk.min_size, math.floor(usd / price))
                    if cur and abs(cur["price"] - price) < 1e-9 and cur["size"] > 0:
                        continue  # unchanged: keep the place in the queue
                    have = (cur["price"] * cur["size"]) if cur else 0.0
                    if price * size > cash_free + have:
                        new = None
                    else:
                        new = dict(price=round(price, 4), size=float(size), placed_ts=now)
                        cash_free -= price * size - have
                if side == "yes":
                    mk.bid_yes = new
                else:
                    mk.bid_no = new

    def sync_orders(self, now: float) -> None:
        """The quotes the paper strategy holds, at pilot size (live.quote_usd, at least the reward minimum),
        handed to the order gateway, which places/cancels/replaces – in dry run only logged."""
        usd = float(self.sc["live"].get("quote_usd", 25))
        want = {}
        for mk in self.st.markets.values():
            for tok, q, lbl in ((mk.yes, mk.bid_yes, "JA"), (mk.no, mk.bid_no, "NEIN")):
                if q and q["size"] > 0:
                    size = float(max(mk.min_size, math.floor(usd / q["price"])))
                    want[tok] = (q["price"], size, f"{mk.title[:80]} – {lbl}")
        before = sum(self.gw.counts.values())
        self.gw.sync(now, want)
        if sum(self.gw.counts.values()) != before:
            self.store.commit()

    # ------------------------------------------------------------------ rewards
    def sample_rewards(self, now: float, books: Dict[str, OrderBook]) -> None:
        last = self._last["sample"]
        self._last["sample"] = now
        if last is None:
            return
        dt = min(now - last, 600.0)
        for mk in self.st.markets.values():
            by, bn = books.get(mk.yes), books.get(mk.no)
            if not by or not bn or mk.mid is None or not (mk.bid_yes or mk.bid_no):
                continue
            share = reward_share(by, bn, mk.bid_yes, mk.bid_no, mk.mid, mk.max_spread, mk.min_size)
            r = mk.daily_rate * share * dt / DAY
            self.st.rewards += r
            self.st.rewards_unbooked += r

    def book_rewards(self, now: float) -> None:
        if self.st.rewards_unbooked > 0:
            r = self.st.rewards_unbooked
            self.st.cash += r
            self.st.rewards_unbooked = 0.0
            self.store.settlement(now, f"{self.name}:rewards", "reward", 0, r, r)

    # ------------------------------------------------------------------ resolution
    def settle(self, now: float) -> None:
        for cid, mk in list(self.st.markets.items()):
            if not (mk.qty_yes > 1e-9 or mk.qty_no > 1e-9) or (mk.end_ts and now < mk.end_ts):
                continue
            final = self.client.token_resolution(mk.yes)
            if final is None:
                continue
            payout = mk.qty_yes * final + mk.qty_no * (1 - final)
            pnl = payout - mk.cost_yes - mk.cost_no
            self.st.cash += payout
            self.st.realized += pnl
            self.store.settlement(now, f"{self.name}:{mk.cid}", "position", mk.qty_yes + mk.qty_no, payout, pnl)
            del self.st.markets[cid]

    # ------------------------------------------------------------------ loop
    def step(self) -> None:
        now = self.clock.now()
        sc = self.sc
        if now - self._last["select"] >= float(sc.get("select_every_min", 30)) * 60:
            self._last["select"] = now
            try:
                self.select(now)
            except Exception as e:  # noqa
                log.error("%s: market selection failed (%s: %s)", self.name, type(e).__name__, e)
        tokens = [t for m in self.st.markets.values() for t in (m.yes, m.no)]
        books = self.books.books(tokens)
        if self.pool is not None and len(books) < len(tokens) and now - self._last["book"] > 60:
            self._last["book"] = now  # websocket not (yet) delivering: fall back to REST once a minute
            books = dict(self.client.books(tokens), **books)
        self.process_trades(now)
        self.sample_rewards(now, books)
        self.quote(now, books)
        if self.gw is not None:
            self.sync_orders(now)
        if now - self._last["settle"] >= 1800:
            self._last["settle"] = now
            self.settle(now)
        if now - self._last["equity"] >= float(sc.get("equity_every_s", 300)):
            self._last["equity"] = now
            self.book_rewards(now)
            st = self.st
            self.store.equity(now, st.equity, st.cash, st.inventory_cost(), 0.0, st.realized + st.rewards + st.rebates, None)
            self.store.commit()
            st.save(self.state_path)
            self._write_scan(now)
            log.info("%s: %d markets, %d quotes, %d fills, equity %.2f (spread/resolution %+.2f, rewards %+.2f, "
                     "rebates %+.2f)", self.name, len(st.markets),
                     sum(bool(m.bid_yes) + bool(m.bid_no) for m in st.markets.values()), st.fills, st.equity,
                     st.realized, st.rewards, st.rebates)

    def _write_scan(self, now: float) -> None:
        st = self.st
        if self.gw is not None:
            self.scan.update({"Order-Modul": self.gw.mode, "Orders gesetzt": self.gw.counts["gesetzt"],
                              "Orders storniert": self.gw.counts["storniert"],
                              "Orders abgelehnt (Limits)": self.gw.counts["abgelehnt"],
                              "Orders offen $": round(self.gw.open_usd(), 2)})
        scan = dict(self.scan, **{"Gebote aktiv": sum(bool(m.bid_yes) + bool(m.bid_no) for m in st.markets.values()),
                                  "Ausführungen": st.fills, "Spread/Auflösung $": round(st.realized, 2),
                                  "Rewards geschätzt $": round(st.rewards, 2), "Rebates $": round(st.rebates, 2),
                                  "Bestand $": round(st.inventory_value(), 2), "ts": now})
        try:
            with open(self.state_path.replace(".json", ".scan.json"), "w", encoding="utf-8") as f:
                json.dump(scan, f)
        except OSError:
            pass

    def run(self, duration_s: Optional[float] = None, exit_on_code_change: bool = False) -> None:
        from .engine import Engine
        code_m = Engine._code_mtime(self) if exit_on_code_change else None
        start = self.clock.now()
        every = float(self.sc.get("step_s", 2))
        try:
            while True:
                try:
                    self.step()
                except Exception:  # noqa – keep running
                    log.exception("%s: step failed", self.name)
                if duration_s and self.clock.now() - start >= duration_s:
                    return
                self.clock.sleep(every)
                if code_m is not None and Engine._code_mtime(self) != code_m:
                    log.info("%s: code updated on disk – exiting for restart", self.name)
                    return
        finally:
            self.st.save(self.state_path)
            if self.pool is not None:
                self.pool.stop()
