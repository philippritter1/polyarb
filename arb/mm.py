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

Inventory exit (config `exit:`, off unless set): a one-sided position older than `after_h` gets a resting SELL
order at the mid plus the quote distance (filled strictly by a trade ABOVE it, or a complementary trade below
1 - price); still held after `dump_after_h`, it is sold at the best bid as a taker (fee paid). Without it the
inventory waits for resolution – capital sits idle, possibly for weeks, and earns no rewards.
"""
from __future__ import annotations

import re
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


def _market_text(m: dict) -> str:
    """Question, slug and the event titles/slugs of a Gamma market – what skip_re is matched against."""
    parts = [m.get("question"), m.get("slug"), m.get("groupItemTitle")]
    for e in m.get("events") or []:
        if isinstance(e, dict):
            parts += [e.get("title"), e.get("slug"), e.get("seriesSlug"), e.get("ticker")]
    return " ".join(str(x) for x in parts if x)


def _norm_tag(value) -> str:
    return re.sub(r"[\s_]+", "-", str(value).strip().lower())


def _market_tags(m: dict) -> List[str]:
    """Gamma tag slugs and labels on the market and its event, normalized."""
    out = []
    sources = [m]
    for e in m.get("events") or []:
        if isinstance(e, dict):
            sources.append(e)
    for src in sources:
        for t in src.get("tags") or []:
            if isinstance(t, dict):
                for key in ("slug", "label", "name"):
                    if t.get(key):
                        out.append(_norm_tag(t[key]))
            elif t:
                out.append(_norm_tag(t))
    return out


def _tag_listed(tag: str, skip: set) -> bool:
    """True when the tag is the exclusion itself or one of its hyphen-parts (`culture` hits `pop-culture`)."""
    return tag in skip or bool(set(tag.split("-")) & skip)


def _topic_extra(m: dict, skip_categories: set, skip_tags: set) -> Optional[str]:
    """Gamma's own category/tag fields, on top of categorize(). None when the market is allowed."""
    raw = [m.get("category")]
    for e in m.get("events") or []:
        if isinstance(e, dict):
            raw.append(e.get("category"))
    for c in raw:
        if not c:
            continue
        if str(c) in skip_categories or (skip_tags and _tag_listed(_norm_tag(c), skip_tags)):
            return "Kategorie ausgelassen"
    if skip_tags and any(_tag_listed(t, skip_tags) for t in _market_tags(m)):
        return "Thema ausgelassen"
    return None


def event_key(m: dict, cid: str) -> str:
    """Markets of one Polymarket event (several view-count buckets of the same trailer) share this key."""
    for e in m.get("events") or []:
        if isinstance(e, dict) and e.get("id") not in (None, ""):
            return f"event:{e['id']}"
    for e in m.get("events") or []:
        if isinstance(e, dict) and e.get("slug"):
            return f"event:{e['slug']}"
    return f"m:{cid}"


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
    ask: Optional[dict] = None      # exit: resting sell order {side, price, size, placed_ts}
    inv_ts: Optional[float] = None  # exit: since when the one-sided position is held
    yes_bid: Optional[float] = None  # last YES bid, for marking inventory at the bid
    no_bid: Optional[float] = None
    event: str = ""                  # event_key: exposure cap across the buckets of one event


@dataclass
class MMState:
    cash: float
    markets: Dict[str, MMMarket] = field(default_factory=dict)
    realized: float = 0.0        # merges + resolutions
    rewards: float = 0.0         # estimated liquidity rewards
    rewards_unbooked: float = 0.0
    rebates: float = 0.0
    fills: int = 0
    exit_pnl: float = 0.0        # realized by selling inventory (part of realized)
    exit_usd: float = 0.0        # proceeds of those sales
    ask_rewards: float = 0.0     # reward_asks: part of `rewards` earned by the exit's sell orders

    def inventory_value(self) -> float:
        """What a sale would get: each side at its bid. Last mid, then 0.50, when no bid was seen yet."""
        total = 0.0
        for m in self.markets.values():
            if m.yes_bid is not None:
                y = m.yes_bid
            elif m.mid is not None:
                y = m.mid
            else:
                y = 0.5
            if m.no_bid is not None:
                n = m.no_bid
            elif m.mid is not None:
                n = 1 - m.mid
            else:
                n = 0.5
            total += m.qty_yes * y + m.qty_no * n
        return total

    def inventory_mid(self) -> float:
        """Mark at the mid. The cash buffer still uses this, so switching the report to the bid does not
        change how large the quotes of a running scenario are."""
        return sum(m.qty_yes * (m.mid if m.mid is not None else 0.5)
                   + m.qty_no * (1 - (m.mid if m.mid is not None else 0.5))
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
                           exit_pnl=self.exit_pnl, exit_usd=self.exit_usd, ask_rewards=self.ask_rewards,
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
                 fills=d.get("fills", 0), exit_pnl=d.get("exit_pnl", 0.0), exit_usd=d.get("exit_usd", 0.0),
                 ask_rewards=d.get("ask_rewards", 0.0))
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
                 v: float, min_size: float, ours_ask: Optional[dict] = None) -> float:
    """Our share of the market's reward pool right now. YES bids + NO asks are one side (NO asks at q are YES
    bids at 1 - q), YES asks + NO bids the other. Our quotes are bids on YES and on NO, plus optionally a resting
    sell order {side: yes|no, price, size} from the inventory exit (counts like any ask)."""
    def lv(book_levels, flip=False):
        return [((1 - l.price) if flip else l.price, l.size) for l in book_levels]
    side1 = lv(yes.bids) + lv(no.asks, flip=True)   # buy YES
    side2 = lv(yes.asks) + lv(no.bids, flip=True)   # sell YES
    others = q_min(score(side1, mid, v, True, min_size), score(side2, mid, v, False, min_size), mid)
    l1 = [(ours_yes["price"], ours_yes["size"])] if ours_yes else []
    l2 = [(1 - ours_no["price"], ours_no["size"])] if ours_no else []
    if ours_ask and ours_ask.get("size", 0) > 0:
        if ours_ask["side"] == "yes":
            l2.append((ours_ask["price"], ours_ask["size"]))      # selling YES at p
        else:
            l1.append((1 - ours_ask["price"], ours_ask["size"]))  # selling NO at q = buying YES at 1 - q
    o1 = score(l1, mid, v, True, min_size) if l1 else 0.0
    o2 = score(l2, mid, v, False, min_size) if l2 else 0.0
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
        self._last = dict(select=-1e18, sample=None, equity=-1e18, settle=-1e18, book=-1e18, metrics=self.clock.now())
        # hourly trend of the reward business (does the pool shrink? does competition eat our share?) – only
        # measured, it changes nothing in the trading; dashboard: "Kennzahlen je Tag", report: one line each
        self.store.db.execute("""CREATE TABLE IF NOT EXISTS mm_metrics(ts REAL, pool_selected REAL, pool_quoted REAL,
            rewards_per_day REAL, share_pct REAL, realized REAL, rewards REAL, rebates REAL, inventory REAL,
            markets INTEGER, quotes INTEGER, fills INTEGER)""")
        self._acc = dict(dt=0.0, rew=0.0, pool_dt=0.0)
        # markout (measured only): how far the price moved for us 5 / 60 min after each fill – negative = we were
        # filled by someone who knew better (adverse selection), the cost the rewards have to cover
        self.store.db.execute("""CREATE TABLE IF NOT EXISTS mm_markout(ts REAL, cid TEXT, token TEXT, side TEXT,
            price REAL, qty REAL, m5 REAL, m60 REAL)""")
        have = {r[1] for r in self.store.db.execute("PRAGMA table_info(mm_markout)")}
        for col, typ in (("title", "TEXT"), ("cat", "TEXT"), ("mid", "REAL"), ("days_left", "REAL"), ("usd", "REAL")):
            if col not in have:  # 16.10.: per fill also what it was, for the export (does the move depend on it?)
                self.store.db.execute(f"ALTER TABLE mm_markout ADD COLUMN {col} {typ}")
        self._markout = [dict(rowid=r[0], ts=r[1], token=r[2], price=r[3], qty=r[4], m5=r[5], cid=r[6]) for r in
                         self.store.db.execute("SELECT rowid, ts, token, price, qty, m5, cid FROM mm_markout WHERE m60 IS NULL")]
        # learn (self-learning, 16.10.): per market the estimated rewards against the markout of our fills; a market
        # whose fills cost more than it paid (or that keeps jumping) is blocked for a while, longer on each repeat
        self.store.db.execute("""CREATE TABLE IF NOT EXISTS mm_ledger(cid TEXT PRIMARY KEY, title TEXT, rew REAL, mo REAL,
            fills INTEGER, jumps TEXT, blocked_until REAL, blocks INTEGER, why TEXT)""")
        self._ledger: Dict[str, dict] = {r[0]: dict(title=r[1], rew=r[2], mo=r[3], fills=r[4], jumps=json.loads(r[5] or "[]"),
                                                     blocked_until=r[6] or 0.0, blocks=r[7] or 0, why=r[8] or "")
                                         for r in self.store.db.execute("SELECT * FROM mm_ledger")}
        self._mids: Dict[str, list] = {}    # pause: recent (ts, mid) per market
        self._paused: Dict[str, float] = {}  # pause: market -> no bids until
        self._cool: Dict[tuple, float] = {}  # fill_cooldown_s: (market, side) -> no new bid on that side until
        self._dumps: list = []      # live: fill-and-kill sells of the exit, sent after the next gateway sync
        self._mismatch: dict = {}   # live: account differences seen at the last read (corrected if they persist)
        self.gw = None  # order gateway (arb/orders.py): mirrors the quotes at pilot size, dry run only for now
        if sc.get("live"):
            from .orders import Gateway
            self.gw = Gateway(name, self.store.db, os.path.join(data_dir, "live.json"),
                              os.path.join(data_dir, f"live-status-{name}.json"), sc["live"],
                              secrets_path=os.path.join(data_dir, "secrets.env"))
        self.scan: dict = {}
        self.quoted: Optional[set] = None  # market ids select() chose to quote (None until the first selection)

    def _quote_floor(self, spread: float) -> float:
        """min_quote_frac: closest a bid or an exit ask may stand to the mid, as a share of the reward spread.
        Unset (every scenario but mm_robust) means no extra floor."""
        floor = self.sc.get("min_quote_frac")
        if floor in (None, ""):
            return 0.0
        return max(self.tick, float(spread) * float(floor))

    def _sided_usd(self, mk: MMMarket) -> float:
        """One-sided inventory in dollars at the mid — the same basis as max_inventory_usd."""
        mid = mk.mid if mk.mid is not None else 0.5
        if mk.qty_yes >= mk.qty_no:
            return (mk.qty_yes - mk.qty_no) * mid
        return (mk.qty_no - mk.qty_yes) * (1 - mid)

    def _mark(self, mk: MMMarket, by: Optional[OrderBook], bn: Optional[OrderBook]) -> None:
        """Refresh mid and bid from the books we have. Paper and live, quoted or not — valuation only."""
        if by and by.bids and by.asks:
            mk.mid = (by.best_bid + by.best_ask) / 2
            mk.yes_bid = by.best_bid
            if not (bn and bn.bids):
                mk.no_bid = 1 - by.best_ask
        if bn and bn.bids:
            mk.no_bid = bn.best_bid
            if mk.yes_bid is None and bn.asks:
                mk.yes_bid = 1 - bn.best_ask
            if mk.mid is None and bn.asks:
                mk.mid = 1 - (bn.best_bid + bn.best_ask) / 2

    def _fit_event(self, price: float, size: float, min_size: float, base_ev: float, adding_ev: List[float],
                   cap_ev: float) -> float:
        """Shares still allowed on a bid that would add one-sided exposure in this event.

        Only the larger of the two sides of one market counts (a filled pair merges). 0 means pull the bid.
        """
        other = max(adding_ev) if adding_ev else 0.0
        if price * size <= other + 1e-9:
            return size
        room = cap_ev - base_ev
        if price * size <= room + 1e-9:
            return size
        shrunk = math.floor(max(0.0, room) / price)
        if shrunk + 1e-9 < min_size:
            self.scan["Ereignis-Grenze (Gebot)"] = self.scan.get("Ereignis-Grenze (Gebot)", 0) + 1
            return 0.0
        return float(shrunk)

    def _resting_fits(self, cur: Optional[dict], price: float, size: float, short_this: bool, cap_ev) -> bool:
        """A resting bid may keep its queue place. With an event cap it also has to fit in the remaining room.
        Without a cap this is true whenever the caller already checked that an order is resting."""
        if cap_ev is None or short_this:
            return True
        if not cur or price <= 0:
            return False
        return cur["price"] * cur["size"] <= price * size + 1e-6

    # ------------------------------------------------------------------ market choice
    def select(self, now: float) -> None:
        """Quiet two-way markets that pay rewards: no sport or crypto (prices jump on news), far from the end,
        mid 0.15-0.85, a qualifying spread of at least 2 ticks. The largest pools first – or, with
        rank: share, the largest EXPECTED reward: pool x our share against the orders already in the book.
        max_age_days keeps only markets started within that many days (early, little competition)."""
        sc = self.sc
        min_days = float(sc.get("min_days_to_end", 3))
        skip = set(sc.get("skip_categories", ["Sport", "Krypto"]))
        skip_tags = {_norm_tag(t) for t in sc.get("skip_tags") or [] if str(t).strip()}
        lo, hi = float(sc.get("min_mid", 0.15)), float(sc.get("max_mid", 0.85))
        max_age = sc.get("max_age_days")
        skip_re = re.compile("|".join(f"(?:{r})" for r in sc["skip_re"]), re.I) if sc.get("skip_re") else None
        params = {"active": "true", "closed": "false", "liquidity_num_min": sc.get("min_liquidity", 1000),
                  "order": "volume", "ascending": "false"}
        if max_age:
            params["start_date_min"] = _iso(now - float(max_age) * DAY)
        # Gamma pages only up to offset 2000: for more, several listings in other orders are merged (list_orders)
        markets, seen_ids = [], set()
        for order in sc.get("list_orders") or [params["order"]]:
            asc = order in ("endDate",)
            try:
                page = self.client.paged("/markets", dict(params, order=order, ascending=str(asc).lower()),
                                         max_items=int(sc.get("max_listed", 2000)))
            except Exception as e:  # noqa – one listing failing must not stop the selection
                log.warning("%s: Liste nach %s nicht lesbar (%s)", self.name, order, type(e).__name__)
                continue
            for m in page:
                key = m.get("conditionId") or m.get("id")
                if key not in seen_ids:
                    seen_ids.add(key)
                    markets.append(m)
        from .study import categorize
        st = {"gelistet": len(markets)}
        cands = []
        pools = {}  # measured only: reward pools by time to the end (where do the big pools sit?)
        for m in markets:
            rate, v, size = rewards_of(m)
            toks = _parse_json_list(m.get("clobTokenIds"))
            prices = _parse_json_list(m.get("outcomePrices"))
            end = _parse_ts(m.get("endDate"))
            if rate > 0 and end:
                days = (end - now) / DAY
                b = ("< 3 T" if days < 3 else "3–14 T" if days < 14 else "14–60 T" if days < 60 else "> 60 T")
                n, tot, top = pools.get(b, (0, 0.0, 0.0))
                pools[b] = (n + 1, tot + rate, max(top, rate))
            why = None
            if rate < float(sc.get("min_daily_rate", 5)) or v < 2 * self.tick:
                why = "keine/kleine Rewards"
            elif len(toks) != 2 or len(prices) != 2 or not m.get("enableOrderBook"):
                why = "nicht binär"
            elif not end or end < now + min_days * DAY:
                why = "endet zu bald"
            elif categorize(m) in skip:
                why = "Kategorie ausgelassen"
            elif skip_re and skip_re.search(_market_text(m)):
                why = "Muster ausgelassen"  # skip_re: e.g. sport season markets, price thresholds
            elif _topic_extra(m, skip, skip_tags):
                why = _topic_extra(m, skip, skip_tags)  # gamma category / tags: culture, view-count, ...
            elif self.blocked(str(m.get("conditionId") or m.get("id")), now):
                why = "gelernt: gesperrt"  # learn: this market cost more than it paid
            elif sc.get("max_min_size_usd") and size * max(float(prices[0]), 1 - float(prices[0])) \
                    > float(sc["max_min_size_usd"]):
                why = "Mindestgröße zu groß"  # small quotes would not qualify for rewards there
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
        cap_ev = float(sc["max_event_usd"]) if sc.get("max_event_usd") not in (None, "") else None
        quote_usd = float(sc.get("quote_usd", 50))
        reserved: Dict[str, float] = {}
        if cap_ev is not None:  # inventory already held counts, so a full event is not refilled
            for cid0, mk0 in self.st.markets.items():
                if cid0 in keep:
                    k = mk0.event or f"m:{cid0}"
                    reserved[k] = reserved.get(k, 0.0) + self._sided_usd(mk0)
        chosen = {}
        for rate, m, v, size, end, toks in cands:
            if len(chosen) >= want:
                break
            cid = str(m.get("conditionId") or m.get("id"))
            ev = event_key(m, cid)
            if cap_ev is not None and reserved.get(ev, 0.0) + quote_usd > cap_ev + 1e-9:
                old = self.st.markets.get(cid)
                if old:  # remember the event, so the inventory counts against the right group next time
                    old.event = ev
                st["Ereignis-Grenze"] = st.get("Ereignis-Grenze", 0) + 1
                continue
            fee = self.fees.resolve(m, categorize(m).lower())
            old = self.st.markets.get(cid)
            mk = old or MMMarket(cid, (m.get("question") or "")[:200], str(toks[0]), str(toks[1]), end, rate, v, size,
                                 categorize(m), fee.rate, fee.exponent)
            mk.daily_rate, mk.max_spread, mk.min_size, mk.end_ts = rate, v, size, end
            mk.event = ev
            chosen[cid] = mk
            if cap_ev is not None:
                reserved[ev] = reserved.get(ev, 0.0) + quote_usd
        picked = set(chosen)
        for cid in list(self.st.markets):
            if cid not in chosen:
                mk = self.st.markets[cid]
                mk.bid_yes = mk.bid_no = None  # no longer quoted
                if cid in keep:
                    chosen[cid] = mk  # holds inventory: keep until resolution
        # only the chosen ones are quoted; markets kept for their inventory just wait for resolution (until 10.10.
        # quote() bid in every kept market too, so the book grew past max_markets: mm_neu ~100 markets)
        if picked != (self.quoted or set()):
            log.info("%s: Märkte: %s", self.name, " | ".join(chosen[c].title[:50] for c in picked))
        self.quoted = picked
        self.st.markets = chosen
        st["ausgewählt"] = len(picked)
        st["nur Bestand (keine Gebote)"] = len(chosen) - len(picked)
        st["Reward-Pool $/Tag"] = round(sum(chosen[c].daily_rate for c in picked), 2)
        for b in ("< 3 T", "3–14 T", "14–60 T", "> 60 T"):
            n, tot, top = pools.get(b, (0, 0.0, 0.0))
            st[f"Reward-Töpfe {b}"] = f"{n} Märkte, {tot:.0f} $/Tag, Ø {tot / n if n else 0:.0f}, max {top:.0f}"
        for k in ("Stop-Loss ausgelöst", "Pausen (schnelle Kursbewegung)", "gelernt: Märkte gesperrt", "echte Fills",
                  "Fills ohne Cash (überbucht)", "Gebote ohne Cash (echt)", "Bestand abgestoßen (Bid)", "echte Fills ohne Markt",
                  "Ereignis-Grenze (Gebot)"):
            if k in self.scan:  # running counters: keep them across the half-hourly selection (until 17.10. reset)
                st[k] = self.scan[k]
        self.scan = st
        if self.pool is not None:
            self.pool.set_assets([t for m in chosen.values() for t in (m.yes, m.no)])

    def planned_quotes(self, mid: float, v: float, min_size: float) -> Tuple[dict, dict]:
        """The two bids quote() would place at this mid (see there)."""
        d = max(self.tick, v * float(self.sc.get("quote_frac", 0.5)), self._quote_floor(v))
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
    def _cash_cap(self, q: dict, qty: float) -> float:
        """overcommit: the bids together may exceed the cash, so a fill can find too little cash – then only what
        the cash pays is filled and the rest of that bid is gone (the exchange would refuse it)."""
        if float(self.sc.get("overcommit", 1)) <= 1:
            return qty
        can = math.floor(max(0.0, self.st.cash) / q["price"] * 100) / 100
        if can >= qty:
            return qty
        q["size"] = 0.0
        self.scan["Fills ohne Cash (überbucht)"] = self.scan.get("Fills ohne Cash (überbucht)", 0) + 1
        return can

    def _fill(self, mk: MMMarket, q: dict, side: str, qty: float, now: float) -> None:
        price = q["price"]
        cost = price * qty
        tok = mk.yes if side == "yes" else mk.no
        own_mid = None if mk.mid is None else (mk.mid if side == "yes" else 1 - mk.mid)
        cur = self.store.db.execute(
            "INSERT INTO mm_markout(ts, cid, token, side, price, qty, title, cat, mid, days_left, usd) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (now, mk.cid, tok, side, price, qty, mk.title[:200], mk.category, own_mid,
             round((mk.end_ts - now) / DAY, 2) if mk.end_ts else None, round(cost, 2)))
        self._markout.append(dict(rowid=cur.lastrowid, ts=now, token=tok, price=price, qty=qty, m5=None, cid=mk.cid))
        self.st.cash -= cost
        if side == "yes":
            mk.qty_yes += qty
            mk.cost_yes += cost
        else:
            mk.qty_no += qty
            mk.cost_no += cost
        q["size"] -= qty
        self.st.fills += 1
        if self.sc.get("fill_cooldown_s"):  # the price tends to keep moving after a fill (markout 60 min ≈ 2 x 5 min):
            self._cool[(mk.cid, side)] = now + float(self.sc["fill_cooldown_s"])  # don't buy more into the move
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
        if n <= 0 or (self.gw is not None and self.gw.live):  # live: merging on the chain is not built yet, the
            return  # pair stays in the account (and so in the inventory, else the account check undoes it)
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

    def _sell(self, mk: MMMarket, side: str, qty: float, price: float, now: float, how: str, taker: bool) -> None:
        held = mk.qty_yes if side == "yes" else mk.qty_no
        qty = min(qty, held)
        if qty <= 1e-9:
            return
        cost = (mk.cost_yes if side == "yes" else mk.cost_no) * qty / held
        fee = mk.fee_rate * (price * (1 - price)) ** mk.fee_exp * qty if taker else 0.0
        proceeds = price * qty - fee
        if side == "yes":
            mk.qty_yes -= qty
            mk.cost_yes -= cost
        else:
            mk.qty_no -= qty
            mk.cost_no -= cost
        pnl = proceeds - cost
        self.st.cash += proceeds
        self.st.realized += pnl
        self.st.exit_pnl += pnl
        self.st.exit_usd += proceeds
        self.store.settlement(now, f"{self.name}:{mk.cid}", "exit", qty, proceeds, pnl)
        log.info("%s: %s %s %.2f @ %.3f (%s), PnL %+.2f", self.name, how, side.upper(), qty, price, mk.title[:50], pnl)

    def manage_inventory(self, now: float, books: Dict[str, OrderBook]) -> None:
        """exit: sell one-sided positions – first a resting order at mid + distance, later at the bid."""
        ex = self.sc.get("exit")
        if not ex:
            return
        after, dump = float(ex.get("after_h", 24)) * 3600, float(ex.get("dump_after_h", 96)) * 3600
        self._dumps = []
        frac = float(self.sc.get("quote_frac", 0.5))
        for mk in self.st.markets.values():
            net = mk.qty_yes - mk.qty_no
            if abs(net) < 1e-6:
                mk.ask, mk.inv_ts = None, None
                continue
            side = "yes" if net > 0 else "no"
            if mk.inv_ts is None or (mk.ask and mk.ask["side"] != side):
                mk.inv_ts, mk.ask = now, None
            age = now - mk.inv_ts
            bk = books.get(mk.yes if side == "yes" else mk.no)
            if not bk or not bk.bids or not bk.asks:
                continue
            stop = False
            if ex.get("stop_ticks"):  # stop-loss: the own side's mid fell stop_ticks below the average buy price –
                held = mk.qty_yes if side == "yes" else mk.qty_no  # the move tends to go on, sell at the bid
                avg = (mk.cost_yes if side == "yes" else mk.cost_no) / held if held > 1e-9 else 0.0
                stop = (bk.best_bid + bk.best_ask) / 2 <= avg - float(ex["stop_ticks"]) * self.tick + 1e-9
                if stop:
                    self.scan["Stop-Loss ausgelöst"] = self.scan.get("Stop-Loss ausgelöst", 0) + 1
            if age < after and not stop:
                continue
            if age >= dump or stop:
                q = min(abs(net), bk.bids[0].size)
                if self.gw is not None and self.gw.live:  # real: send a fill-and-kill sell after the resting ask is
                    # cancelled (sync_orders); booked only when the exchange reports the fill (apply_real_fills)
                    mk.ask = None
                    self._dumps.append((mk.yes if side == "yes" else mk.no, bk.best_bid, math.floor(q * 100) / 100,
                                        f"{mk.title[:80]} – {'JA' if side == 'yes' else 'NEIN'}"))
                    continue
                self._sell(mk, side, q, bk.best_bid, now, "abgestoßen zum Bid", taker=True)
                self.scan["Bestand abgestoßen (Bid)"] = self.scan.get("Bestand abgestoßen (Bid)", 0) + 1
                mk.ask = None
                if abs(mk.qty_yes - mk.qty_no) < 1e-6:
                    mk.inv_ts = None
                continue
            mid = (bk.best_bid + bk.best_ask) / 2
            mind = self._quote_floor(mk.max_spread)
            d = max(self.tick, mk.max_spread * float(ex.get("ask_frac", frac)), mind)  # ask_frac: sell closer
            price = round(max(math.ceil((mid + d) / self.tick - 1e-9) * self.tick, bk.best_bid + self.tick), 4)
            if ex.get("decay_h"):  # step the sell order down every hour, from mid + d to the mid itself – an ask
                # scores only between the mid and mid + max_spread, so it never leaves the reward band (and the
                # closer to the mid, the more it scores); never at or below the best bid (post-only).
                # min_quote_frac stops the steps at that distance instead of at the mid.
                done = min(1.0, math.floor((age - after) / 3600) / float(ex["decay_h"]))
                low = max(math.ceil(mid / self.tick - 1e-9) * self.tick, bk.best_bid + self.tick)
                if mind:
                    low = max(low, math.ceil((mid + mind) / self.tick - 1e-9) * self.tick)
                price = round(max(low, math.ceil((price - (price - low) * done) / self.tick - 1e-9) * self.tick), 4)
                top = math.floor((mid + mk.max_spread) / self.tick - 1e-9) * self.tick  # strictly inside mid + v
                if mk.max_spread > 0 and low <= top:
                    price = round(min(price, top), 4)
            if price >= 1:
                continue
            if not mk.ask or abs(mk.ask["price"] - price) > 1e-9 or abs(mk.ask["size"] - abs(net)) > 1e-6:
                mk.ask = dict(side=side, price=price, size=abs(net), placed_ts=now)

    def apply_real_fills(self, now: float) -> int:
        """Live mode: inventory and cash follow the account – every real fill since the switch to live, once.
        (A merge of YES+NO pairs is still booked as in the paper run; on the chain the pair simply stays until
        resolution or until merging is built – same value, the capital is just bound longer.)"""
        by_tok = {}
        for mk in self.st.markets.values():
            by_tok[mk.yes], by_tok[mk.no] = (mk, "yes"), (mk, "no")
        n = 0
        for tok, side, price, size in self.gw.new_fills():
            hit = by_tok.get(tok)
            if not hit:
                log.warning("%s: echter Fill in unbekanntem Token %s (%s %.2f @ %.3f)", self.name, tok[:12], side, size, price)
                self.scan["echte Fills ohne Markt"] = self.scan.get("echte Fills ohne Markt", 0) + 1
                continue
            mk, which = hit
            if side == "BUY":
                q = (mk.bid_yes if which == "yes" else mk.bid_no) or dict(price=price, size=size, placed_ts=now)
                q = dict(q, price=price, size=max(q.get("size", 0), size))
                self._fill(mk, q, which, size, now)
                bid = mk.bid_yes if which == "yes" else mk.bid_no
                if bid:
                    bid["size"] = max(0.0, bid["size"] - size)
            else:
                self._sell(mk, which, size, price, now, "echter Verkauf", taker=False)
                if mk.ask and mk.ask["side"] == which:
                    mk.ask["size"] = max(0.0, mk.ask["size"] - size)
            n += 1
        if n:
            self.scan["echte Fills"] = self.scan.get("echte Fills", 0) + n
        return n

    # ------------------------------------------------------------------ learn
    def _ledger_of(self, cid: str) -> dict:
        e = self._ledger.get(cid)
        if e is None:
            mk = self.st.markets.get(cid)
            e = self._ledger[cid] = dict(title=mk.title if mk else "", rew=0.0, mo=0.0, fills=0, jumps=[],
                                         blocked_until=0.0, blocks=0, why="")
        return e

    def _save_ledger(self, cid: str) -> None:
        e = self._ledger[cid]
        self.store.db.execute("INSERT OR REPLACE INTO mm_ledger VALUES(?,?,?,?,?,?,?,?,?)", (
            cid, e["title"], round(e["rew"], 6), round(e["mo"], 6), e["fills"], json.dumps(e["jumps"]),
            e["blocked_until"], e["blocks"], e["why"]))

    def _learn_check(self, cid: str, now: float) -> None:
        """Block a market whose fills cost more than its rewards (after min_fills fills), or that jumped
        max_jumps times within 24 h. Block for block_h, doubled on every repeat; afterwards it starts afresh."""
        lc, e = self.sc["learn"], self._ledger[cid]
        e["jumps"] = [t for t in e["jumps"] if now - t <= DAY]
        why = ""
        if e["fills"] >= int(lc.get("min_fills", 2)) and e["rew"] + e["mo"] < -float(lc.get("min_loss_usd", 0)):
            why = f"Fills kosteten {-e['mo']:.2f} $, Rewards {e['rew']:.2f} $"
        elif lc.get("max_jumps") and len(e["jumps"]) >= int(lc["max_jumps"]):
            why = f"{len(e['jumps'])} Kurssprünge in 24 h"
        if why and now >= e["blocked_until"]:
            e["blocks"] += 1
            e["blocked_until"] = now + float(lc.get("block_h", 72)) * 3600 * 2 ** (e["blocks"] - 1)
            e["why"] = why
            e["rew"], e["mo"], e["fills"], e["jumps"] = 0.0, 0.0, 0, []  # a fresh start after the block
            self.scan["gelernt: Märkte gesperrt"] = self.scan.get("gelernt: Märkte gesperrt", 0) + 1
            log.info("%s: lernt – %s gesperrt bis %s (%s)", self.name, e["title"][:60],
                     time.strftime("%d.%m. %H:%M", time.gmtime(e["blocked_until"])), why)
        self._save_ledger(cid)

    def blocked(self, cid: str, now: float) -> bool:
        e = self._ledger.get(cid)
        return bool(self.sc.get("learn")) and e is not None and now < e["blocked_until"]

    def update_markouts(self, now: float, books: Dict[str, OrderBook]) -> None:
        """Fill price vs. the token's mid 5 and 60 minutes later, times the shares (+ = the price rose after we
        bought). Books of dropped markets may be missing: such fills are given up after 2 hours."""
        keep = []
        for m in self._markout:
            age = now - m["ts"]
            bk = books.get(m["token"])
            mid = (bk.best_bid + bk.best_ask) / 2 if bk and bk.bids and bk.asks else None
            if mid is not None and m["m5"] is None and age >= 300:
                m["m5"] = round((mid - m["price"]) * m["qty"], 4)
                self.store.db.execute("UPDATE mm_markout SET m5=? WHERE rowid=?", (m["m5"], m["rowid"]))
            if mid is not None and age >= 3600:
                m60 = round((mid - m["price"]) * m["qty"], 4)
                self.store.db.execute("UPDATE mm_markout SET m60=? WHERE rowid=?", (m60, m["rowid"]))
                if self.sc.get("learn") and m.get("cid"):
                    e = self._ledger_of(m["cid"])
                    e["mo"] += m60
                    e["fills"] += 1
                    self._learn_check(m["cid"], now)
                continue
            if age < 7200:
                keep.append(m)
        self._markout = keep

    def reconcile_positions(self, now: float) -> None:
        """Live mode: the account is the truth. Every 5 min the real positions are compared with the inventory;
        a difference that is still there at the next read (a fill may simply not be booked yet) is corrected to
        the account and reported (scan, gateway status -> push alarm). Same for the cash."""
        real = self.gw.real_positions(now)
        if real is None:
            return
        seen, notes = {}, []
        for mk in self.st.markets.values():
            for which, tok in (("yes", mk.yes), ("no", mk.no)):
                have = mk.qty_yes if which == "yes" else mk.qty_no
                r = real.get(tok, {})
                size = float(r.get("size", 0))
                if abs(size - have) <= 0.1:  # rounding of fills (26.00 vs 26.01) is no difference
                    continue
                seen[tok] = size
                if self._mismatch.get(tok) is None or abs(self._mismatch[tok] - size) > 0.01:
                    continue  # first sighting: wait for the next read
                cost = size * float(r.get("avg") or (mk.mid if which == "yes" else 1 - (mk.mid or 0.5)) or 0.5)
                if which == "yes":
                    mk.qty_yes, mk.cost_yes = size, cost
                else:
                    mk.qty_no, mk.cost_no = size, cost
                notes.append(f"{mk.title[:40]} {which.upper()} {have:.2f} -> {size:.2f}")
        known = {t for mk in self.st.markets.values() for t in (mk.yes, mk.no)}
        for tok, r in real.items():  # held in the account, but the bot no longer tracks the market (e.g. dropped
            size = float(r.get("size", 0))  # after a sale it only booked): take it back so the exit sells it
            if tok in known or size <= 0.1 or not r.get("cid") or not r.get("opposite"):
                continue
            seen[tok] = size
            if self._mismatch.get(tok) is None or abs(self._mismatch[tok] - size) > 0.01:
                continue
            first = r.get("index") == 0 if r.get("index") is not None else str(r.get("outcome")).lower() == "yes"
            yes, no = (tok, r["opposite"]) if first else (r["opposite"], tok)
            mk = MMMarket(r["cid"], (r.get("title") or "")[:200], yes, no, _parse_ts(r.get("end")), 0.0, 0.0, 0.0, "")
            if first:
                mk.qty_yes, mk.cost_yes = size, size * float(r.get("avg") or 0)
            else:
                mk.qty_no, mk.cost_no = size, size * float(r.get("avg") or 0)
            self.st.markets[mk.cid] = mk
            notes.append(f"{mk.title[:40]} {'YES' if first else 'NO'} 0.00 -> {size:.2f} (Markt wieder aufgenommen)")
        cash = self.gw.info.get("cash")
        if cash is not None and abs(cash - self.st.cash) > 0.05:
            if self._mismatch.get("cash") is not None and abs(self._mismatch["cash"] - cash) <= 0.05:
                if abs(cash - self.st.cash) > 1.0:  # small drifts are the estimated rewards/rebates booked as
                    notes.append(f"Cash {self.st.cash:.2f} -> {cash:.2f}")  # cash (real ones come next day)
                self.st.cash = float(cash)
            else:
                seen["cash"] = cash
        self._mismatch = seen
        if notes:
            log.warning("%s: Abgleich mit dem Konto korrigiert: %s", self.name, "; ".join(notes))
            self.scan["Abgleich korrigiert"] = self.scan.get("Abgleich korrigiert", 0) + len(notes)
            self.gw.info["abgleich"] = f"{time.strftime('%d.%m. %H:%M')}: " + "; ".join(notes)[:300]

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
            a = mk.ask
            if a and a["size"] > 0 and ts >= a["placed_ts"] + self.latency and (
                    (a["side"] == which and price > a["price"] + 1e-9)
                    or (a["side"] != which and price < 1 - a["price"] - 1e-9)):
                q = min(a["size"], size)
                self._sell(mk, a["side"], q, a["price"], now, "Verkaufsgebot", taker=False)
                a["size"] -= q
                size -= q  # the same trade cannot also fill a bid with these shares
                n += 1
                if size <= 1e-9:
                    continue
            own, other = (mk.bid_yes, mk.bid_no) if which == "yes" else (mk.bid_no, mk.bid_yes)
            own_side, other_side = (which, "no" if which == "yes" else "yes")
            if own and ts >= own["placed_ts"] + self.latency and price < own["price"] - 1e-9 and own["size"] > 0:
                q = self._cash_cap(own, min(own["size"], size))
                if q > 0:
                    self._fill(mk, own, own_side, q, now)
                    n += 1
            elif other and ts >= other["placed_ts"] + self.latency and price > 1 - other["price"] + 1e-9 \
                    and other["size"] > 0:
                q = self._cash_cap(other, min(other["size"], size))
                if q > 0:
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
        cap = sc.get("max_total_inventory_usd")   # all markets together: beyond it only bids that reduce a position
        over = bool(cap) and self.st.inventory_cost() >= float(cap)
        if cap:
            self.scan["Bestandsgrenze erreicht"] = int(over)
        # cash buffer stays on the mid mark, so valuing the report at the bid does not change quote size
        cash_free = self.st.cash * max(1.0, float(sc.get("overcommit", 1))) \
            - float(sc.get("cash_buffer_pct", 0.10)) * (self.st.cash + self.st.inventory_mid())
        hyst = float(sc.get("reprice_ticks", 1))  # move a resting bid only when the target is this many ticks away
        cap_ev = float(sc["max_event_usd"]) if sc.get("max_event_usd") not in (None, "") else None
        if cap_ev is not None:  # how many bids this pass were pulled, not a lifetime counter
            self.scan["Ereignis-Grenze (Gebot)"] = 0
        for mk in self.st.markets.values():  # marks first, including markets we no longer quote
            self._mark(mk, books.get(mk.yes), books.get(mk.no))
        ev_used: Dict[str, float] = {}
        if cap_ev is not None:
            for mk in self.st.markets.values():
                k = mk.event or f"m:{mk.cid}"
                ev_used[k] = ev_used.get(k, 0.0) + self._sided_usd(mk)
        for mk in self.st.markets.values():
            by, bn = books.get(mk.yes), books.get(mk.no)
            if (self.quoted is not None and mk.cid not in self.quoted) or not by or not by.bids or not by.asks \
                    or (mk.end_ts and now > mk.end_ts - stop_h) or mk.daily_rate <= 0 or self.blocked(mk.cid, now):
                mk.bid_yes = mk.bid_no = None
                continue
            mid = mk.mid if mk.mid is not None else (by.best_bid + by.best_ask) / 2
            pz = sc.get("pause")
            if pz:  # the mid jumped by >= ticks within window_s: no bids for pause_s (informed traders are active)
                hist = [x for x in self._mids.get(mk.cid, []) if now - x[0] <= float(pz.get("window_s", 300))]
                hist.append((now, mid))
                self._mids[mk.cid] = hist
                ms = [x[1] for x in hist]
                if max(ms) - min(ms) >= float(pz.get("ticks", 3)) * self.tick - 1e-9:
                    if now >= self._paused.get(mk.cid, 0):
                        self.scan["Pausen (schnelle Kursbewegung)"] = self.scan.get("Pausen (schnelle Kursbewegung)", 0) + 1
                        if sc.get("learn"):
                            self._ledger_of(mk.cid)["jumps"].append(now)
                            self._learn_check(mk.cid, now)
                    self._paused[mk.cid] = now + float(pz.get("pause_s", 600))
                    self._mids[mk.cid] = [(now, mid)]
                if now < self._paused.get(mk.cid, 0):
                    mk.bid_yes = mk.bid_no = None
                    continue
            if by.best_ask - by.best_bid > 2 * mk.max_spread:
                mk.bid_yes = mk.bid_no = None  # book too wide to qualify: nobody to quote against
                continue
            d = max(self.tick, mk.max_spread * frac)
            fy, fn = mid, 1 - mid
            if sc.get("fair") == "micro":  # book imbalance: much more size on the ask = the price is about to fall –
                # each side bids from the lower of mid and microprice (still within the reward band around the mid)
                bs, as_ = by.bids[0].size, by.asks[0].size
                if bs + as_ > 0:
                    micro = (by.best_bid * as_ + by.best_ask * bs) / (bs + as_)
                    fy, fn = min(mid, micro), min(1 - mid, 1 - micro)
            dy = dn = d
            pf = sc.get("pair_frac")
            if pf is not None:  # holding one side: bid the other side closer (a filled pair is worth 1 $ for sure),
                # but never so high that the pair would cost more than 1 $
                if mk.qty_yes > mk.qty_no + 1e-9:
                    dn = max(self.tick, mk.max_spread * float(pf), mk.cost_yes / mk.qty_yes - mid + self.tick)
                elif mk.qty_no > mk.qty_yes + 1e-9:
                    dy = max(self.tick, mk.max_spread * float(pf), mk.cost_no / mk.qty_no - (1 - mid) + self.tick)
            mind = self._quote_floor(mk.max_spread)
            if mind:  # never inside min_quote_frac, even when pair_frac would step closer
                dy, dn = max(dy, mind), max(dn, mind)
            want_yes = math.floor((fy - dy) / self.tick + 1e-9) * self.tick
            want_no = math.floor((fn - dn) / self.tick + 1e-9) * self.tick
            want_yes = min(want_yes, by.best_ask - self.tick)
            if bn and bn.asks:
                want_no = min(want_no, bn.best_ask - self.tick)
            net = (mk.qty_yes - mk.qty_no) * (mid if mk.qty_yes >= mk.qty_no else 1 - mid)
            ev_key = mk.event or f"m:{mk.cid}"
            base_ev = ev_used.get(ev_key, 0.0) if cap_ev is not None else 0.0
            adding_ev: List[float] = []
            for side, price in (("yes", want_yes), ("no", want_no)):
                cur = mk.bid_yes if side == "yes" else mk.bid_no
                long_this = (side == "yes" and mk.qty_yes > mk.qty_no) or (side == "no" and mk.qty_no > mk.qty_yes)
                short_this = (side == "yes" and mk.qty_yes < mk.qty_no) or (side == "no" and mk.qty_no < mk.qty_yes)
                if price < self.tick or (long_this and abs(net) >= max_inv) or (over and not short_this) \
                        or now < self._cool.get((mk.cid, side), 0):
                    new = None  # skew: stop buying the side we already hold too much of (or just got filled on)
                else:
                    size = max(mk.min_size, math.floor(usd / price))
                    if cap_ev is not None and not short_this:
                        size = self._fit_event(price, size, mk.min_size, base_ev, adding_ev, cap_ev)
                    if size <= 0:
                        new = None
                    elif cur and abs(cur["price"] - price) < 1e-9 and cur["size"] > 0 \
                            and self._resting_fits(cur, price, size, short_this, cap_ev):
                        if cap_ev is not None and not short_this:
                            adding_ev.append(cur["price"] * cur["size"])
                        continue  # unchanged: keep the place in the queue
                    elif cur and cur["size"] > 0 and abs(cur["price"] - price) < hyst * self.tick - 1e-9:
                        own_mid = mid if side == "yes" else 1 - mid
                        book = by if side == "yes" else bn
                        if 0 <= own_mid - cur["price"] < mk.max_spread and cur["price"] < book.best_ask - 1e-9 \
                                and self._resting_fits(cur, price, size, short_this, cap_ev):
                            if cap_ev is not None and not short_this:
                                adding_ev.append(cur["price"] * cur["size"])
                            continue  # reprice_ticks: the mid flickers by a tick – keep the order while it qualifies
                        have = (cur["price"] * cur["size"]) if cur else 0.0
                        if price * size > cash_free + have:
                            new = None
                        else:
                            new = dict(price=round(price, 4), size=float(size), placed_ts=now)
                            cash_free -= price * size - have
                    else:
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
                if cap_ev is not None and not short_this and new and new.get("size", 0) > 0:
                    adding_ev.append(new["price"] * new["size"])
            if cap_ev is not None:
                ev_used[ev_key] = base_ev + (max(adding_ev) if adding_ev else 0.0)
        if cap_ev is not None and not self.scan.get("Ereignis-Grenze (Gebot)"):
            self.scan.pop("Ereignis-Grenze (Gebot)", None)

    def sync_orders(self, now: float) -> None:
        """The quotes the paper strategy holds, at pilot size (live.quote_usd, at least the reward minimum),
        handed to the order gateway, which places/cancels/replaces – in dry run only logged."""
        usd = float(self.sc["live"].get("quote_usd", 25))
        scale = usd / float(self.sc.get("quote_usd", 50))
        want = {}
        # live: all resting buys together must be covered by the real cash (the exchange refuses the rest anyway)
        budget = self.st.cash if self.gw.live else float("inf")
        for mk in self.st.markets.values():
            for tok, q, lbl in ((mk.yes, mk.bid_yes, "JA"), (mk.no, mk.bid_no, "NEIN")):
                if q and q["size"] > 0:
                    size = float(max(mk.min_size, math.floor(usd / q["price"])))
                    if q["price"] * size > budget + 1e-9:
                        self.scan["Gebote ohne Cash (echt)"] = self.scan.get("Gebote ohne Cash (echt)", 0) + 1
                        continue
                    budget -= q["price"] * size
                    want[(tok, "BUY")] = (q["price"], size, f"{mk.title[:80]} – {lbl}")
            a = mk.ask
            if a and a["size"] > 0:  # inventory exit: the sell order, scaled like the bids
                size = math.floor(a["size"] * min(1.0, scale) * 100) / 100
                if size >= 5:  # Polymarket's minimum order size
                    want[(mk.yes if a["side"] == "yes" else mk.no, "SELL")] = (
                        a["price"], size, f"{mk.title[:80]} – {'JA' if a['side'] == 'yes' else 'NEIN'}")
        before = sum(self.gw.counts.values())
        self.gw.sync(now, want)
        for tok, price, size, label in getattr(self, "_dumps", []):
            if size >= 5:  # Polymarket's minimum order size
                self.gw.sell_now(now, tok, price, size, label)
        self._dumps = []
        if sum(self.gw.counts.values()) != before:
            self.store.commit()

    # ------------------------------------------------------------------ rewards
    def sample_rewards(self, now: float, books: Dict[str, OrderBook]) -> None:
        last = self._last["sample"]
        self._last["sample"] = now
        if last is None:
            return
        dt = min(now - last, 600.0)
        self._acc["dt"] += dt
        est = {}
        asks = bool(self.sc.get("reward_asks"))  # also count the exit's sell orders (they qualify like bids)
        for mk in self.st.markets.values():
            by, bn = books.get(mk.yes), books.get(mk.no)
            bids = bool(mk.bid_yes or mk.bid_no)
            if mk.ask and mk.max_spread <= 0:  # live: a market taken back from the account has no reward data;
                est[mk.cid] = dict(q=mk.title, rate=0.0, pct=None)  # still record Polymarket's own numbers
                continue
            if not by or not bn or mk.mid is None or not (bids or mk.ask):
                continue
            base = reward_share(by, bn, mk.bid_yes, mk.bid_no, mk.mid, mk.max_spread, mk.min_size) if bids else 0.0
            full = reward_share(by, bn, mk.bid_yes, mk.bid_no, mk.mid, mk.max_spread, mk.min_size, mk.ask) \
                if mk.ask else base
            # Polymarket counts the sell order either way: the live comparison always uses the full share
            est[mk.cid] = dict(q=mk.title, rate=mk.daily_rate, pct=round(full * 100, 4))
            if not (bids or asks):
                continue  # booked estimate as before for scenarios without reward_asks
            share = full if asks else base
            if asks:
                self.st.ask_rewards += mk.daily_rate * (full - base) * dt / DAY
            r = mk.daily_rate * share * dt / DAY
            if self.sc.get("learn"):
                self._ledger_of(mk.cid)["rew"] += r
            self.st.rewards += r
            self.st.rewards_unbooked += r
            self._acc["rew"] += r
            self._acc["pool_dt"] += mk.daily_rate * dt
        if self.gw is not None:  # live: the gateway puts Polymarket's own share next to this estimate
            self.gw.est = est

    def write_metrics(self, now: float) -> None:
        """One row per hour: the pools we quoted in, our estimated reward rate and share of those pools."""
        a, st = self._acc, self.st
        if a["dt"] <= 0:
            return
        pool_q = a["pool_dt"] / a["dt"]
        self.store.db.execute("INSERT INTO mm_metrics VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
            now, float(self.scan.get("Reward-Pool $/Tag", 0) or 0), round(pool_q, 2), round(a["rew"] / a["dt"] * DAY, 2),
            round(a["rew"] / a["pool_dt"] * DAY * 100, 3) if a["pool_dt"] > 0 else None,
            round(st.realized, 2), round(st.rewards, 2), round(st.rebates, 2), round(st.inventory_value(), 2),
            len(st.markets), sum(bool(m.bid_yes) + bool(m.bid_no) for m in st.markets.values()), st.fills))
        self._acc = dict(dt=0.0, rew=0.0, pool_dt=0.0)

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
        live = self.gw is not None and self.gw.live
        if live:
            self.books.trades.clear()  # real orders: what was really filled counts, not the simulation's guess
        else:
            self.process_trades(now)
        self.sample_rewards(now, books)
        self.update_markouts(now, books)
        self.quote(now, books)
        self.manage_inventory(now, books)
        if self.gw is not None:
            self.sync_orders(now)  # in live mode this also fetches the account's fills ...
            if self.gw.live:
                self.apply_real_fills(now)  # ... which then become the inventory
                self.reconcile_positions(now)
        if now - self._last["settle"] >= 1800:
            self._last["settle"] = now
            self.settle(now)
        if now - self._last["equity"] >= float(sc.get("equity_every_s", 300)):
            self._last["equity"] = now
            self.book_rewards(now)
            if now - self._last["metrics"] >= float(sc.get("metrics_every_s", 3600)):
                self._last["metrics"] = now
                self.write_metrics(now)
            st = self.st
            # realized_cum stays the sum, so the 4h report does not jump. trade/rewards/rebates are stored
            # beside it; the dashboard shows those three and calls only the trading part "realisiert".
            self.store.equity(now, st.equity, st.cash, st.inventory_cost(), 0.0,
                              st.realized + st.rewards + st.rebates, None, st.rewards, st.rebates, st.realized)
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
        try:
            n, m5, n60, m60 = self.store.db.execute(
                "SELECT COUNT(m5), SUM(m5), COUNT(m60), SUM(m60) FROM mm_markout").fetchone()
            if n:
                self.scan["Kursbewegung nach Fill 5 Min $"] = f"{m5 or 0:+.2f} ({n} Fills)"
            if n60:
                self.scan["Kursbewegung nach Fill 60 Min $"] = f"{m60 or 0:+.2f} ({n60} Fills)"
        except Exception:  # noqa
            pass
        if self.sc.get("learn"):
            for cid in list(self._ledger):
                self._save_ledger(cid)  # rewards accrue every step: written with the equity row
            now_b = [e for e in self._ledger.values() if now < e["blocked_until"]]
            self.scan["gelernt: jetzt gesperrt"] = "; ".join(
                f"{e['title'][:40]} bis {time.strftime('%d.%m. %H:%M', time.gmtime(e['blocked_until']))} UTC ({e['why']})"
                for e in now_b) or "keiner"
            live = [e for e in self._ledger.values() if e["fills"] or e["rew"]]
            self.scan["gelernt: Bilanz offener Märkte $"] = round(sum(e["rew"] + e["mo"] for e in live), 2)
        scan = dict(self.scan, **{"Gebote aktiv": sum(bool(m.bid_yes) + bool(m.bid_no) for m in st.markets.values()),
                                  "Ausführungen": st.fills, "Spread/Auflösung $": round(st.realized, 2),
                                  "Rewards geschätzt $": round(st.rewards, 2), "Rebates $": round(st.rebates, 2),
                                  **({"Bestand verkauft $": round(st.exit_usd, 2), "davon Ergebnis $": round(st.exit_pnl, 2),
                                      "Verkaufsgebote aktiv": sum(1 for m in st.markets.values() if m.ask)}
                                     if self.sc.get("exit") else {}),
                                  **({"davon Rewards aus Verkaufsgeboten $": round(st.ask_rewards, 2)}
                                     if self.sc.get("reward_asks") else {}),
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
            self.store.commit()  # with the state: the cursor of real fills already applied (else applied twice)
            self.st.save(self.state_path)
            if self.pool is not None:
                self.pool.stop()
