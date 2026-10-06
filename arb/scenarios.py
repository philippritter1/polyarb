"""Directional paper scenarios, each with its own budget, database, portfolio and dashboard tab.

Unlike the arbitrage bot these trades CAN lose: a strategy says which token it believes is
underpriced (`Signal.fair` = its probability that the token pays $1) and the worst price it
accepts. The engine buys against a fresh order book after latency + market order delay (same
haircut and fees as the arbitrage simulation), holds the position to resolution and books the
payout. The dashboard compares the realized win rate with the entry prices and the strategy's
own probabilities – that comparison is what tells whether a strategy has an edge.

Strategies
  endgame  – favorites at 0.95-0.99 shortly before / after the scheduled end ("almost decided")
  longshot – NO on multi-outcome candidates priced 2-8 % (favorite-longshot bias)
  weather  – temperature buckets priced from ensemble forecasts (arb/weather.py)
  ladder   – logic arbitrage between related markets (arb/ladder.py), run by the arbitrage engine
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .client import FeeResolver, _parse_json_list, _parse_ts, _delay
from .models import Basket, ExecutionResult, FeeSpec, Fill, Leg, Opportunity, OrderBook
from .paper import _ioc
from .storage import Store
from .weather import WeatherModel, bucket_prob, parse_bucket, parse_title

log = logging.getLogger("polyarb.scenario")
DAY = 86_400


@dataclass
class Signal:
    token_id: str
    group: str                # event/market id – exposure is capped per group
    title: str
    label: str
    fair: float               # strategy's probability that the token pays $1
    max_price: float          # worst ask we accept
    fee: FeeSpec
    end_ts: Optional[float]
    delay_s: float = 0.0
    kelly: bool = False       # size by fractional Kelly on `fair` instead of a flat stake
    reason: str = ""
    # sanity limits vs. the market (checked against the live ask): a model that disagrees with the
    # market by more than max_edge, or buys what the market prices near zero, is usually just wrong
    max_edge: Optional[float] = None
    min_ask: float = 0.0
    safest_first: bool = False  # rank by price (most certain first) instead of by edge
    max_spread: Optional[float] = None  # skip books whose bid-ask spread is wider (edge dies in the spread)


@dataclass
class Position:
    token_id: str
    group: str
    title: str
    label: str
    qty: float
    cost: float
    fair: float
    opened_ts: float
    end_ts: Optional[float]
    last_check: float = 0.0


@dataclass
class ScenarioPortfolio:
    cash: float
    positions: Dict[str, Position] = field(default_factory=dict)
    realized_pnl: float = 0.0
    marks: Dict[str, float] = field(default_factory=dict)
    seen: List[str] = field(default_factory=list)   # tokens already traded once
    # maker mode: resting limit buys, token -> {group, title, label, price, qty, fair, end_ts, placed_ts, reason}
    orders: Dict[str, dict] = field(default_factory=dict)

    @property
    def cost(self) -> float:
        return sum(p.cost for p in self.positions.values())

    @property
    def reserved(self) -> float:
        """Cash promised to resting orders (a fill pays price x qty, makers pay no fee)."""
        return sum(o["price"] * o["qty"] for o in self.orders.values())

    @property
    def value(self) -> float:
        return sum(p.qty * self.marks.get(t, p.cost / p.qty) for t, p in self.positions.items())

    @property
    def equity(self) -> float:
        return self.cash + self.value

    def exposure(self, group: str) -> float:
        return (sum(p.cost for p in self.positions.values() if p.group == group)
                + sum(o["price"] * o["qty"] for o in self.orders.values() if o["group"] == group))

    def save(self, path: str) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"cash": self.cash, "realized_pnl": self.realized_pnl, "marks": self.marks,
                       "seen": self.seen, "positions": [asdict(p) for p in self.positions.values()],
                       "orders": self.orders}, f)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str, cash: float) -> "ScenarioPortfolio":
        if not os.path.exists(path):
            return cls(cash=cash)
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        pf = cls(cash=d["cash"], realized_pnl=d.get("realized_pnl", 0.0), marks=d.get("marks", {}),
                 seen=d.get("seen", []), orders=d.get("orders", {}))
        for p in d.get("positions", []):
            pf.positions[p["token_id"]] = Position(**p)
        return pf


# ====================================================================== strategies
class Strategy:
    def __init__(self, cfg: dict, client):
        self.cfg, self.client = cfg, client
        self.fees = getattr(client, "fees", None) or FeeResolver({})

    def candidates(self, now: float) -> List[Signal]:
        raise NotImplementedError


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class EndgameStrategy(Strategy):
    """Favorite of a binary market at min_price..max_price shortly before or after its scheduled end.

    The bet: once an outcome is practically decided, the last cents are still paid for waiting
    (and for dispute risk). Wins small often; one wrong call costs a whole stake. Games only count
    once they are over (gameStartTime + game_hours): before that a 0.93 favorite is just a bet.
    """

    def candidates(self, now: float) -> List[Signal]:
        c = self.cfg
        lo, hi = float(c.get("min_price", 0.95)), float(c.get("max_price", 0.99))
        ahead, behind = float(c.get("max_hours_to_end", 12)) * 3600, float(c.get("max_hours_after_end", 72)) * 3600
        game_s = float(c.get("game_hours", 3)) * 3600
        markets = self.client.paged("/markets", {
            "active": "true", "closed": "false", "liquidity_num_min": c.get("min_liquidity", 1000),
            "end_date_min": _iso(now - behind), "end_date_max": _iso(now + ahead)},
            max_items=int(c.get("max_markets", 1000)))
        out = []
        for m in markets:
            end = _parse_ts(m.get("endDate"))
            if not end or not (now - behind <= end <= now + ahead) or not m.get("enableOrderBook"):
                continue
            if not m.get("acceptingOrders", True):
                continue
            start = _parse_ts(m.get("gameStartTime"))
            if start and now < start + game_s:
                continue  # game not over yet: nothing is decided
            toks, prices = _parse_json_list(m.get("clobTokenIds")), _parse_json_list(m.get("outcomePrices"))
            outs = _parse_json_list(m.get("outcomes")) or ["Yes", "No"]
            if len(toks) != 2 or len(prices) != 2:
                continue
            i = 0 if float(prices[0]) >= float(prices[1]) else 1
            if not lo - 0.02 <= float(prices[i]) <= hi:
                continue
            evs = m.get("events") or [{}]
            group = f"event:{evs[0].get('id')}" if evs[0].get("id") else str(m.get("conditionId") or m.get("id"))
            out.append(Signal(str(toks[i]), group, m.get("question", ""),
                              str(outs[i]), fair=1.0, max_price=hi, fee=self.fees.resolve(m),
                              end_ts=end, delay_s=_delay(m), reason=f"favorite {float(prices[i]):.3f}",
                              min_ask=lo, safest_first=True))
        return out


class LongshotStrategy(Strategy):
    """NO on multi-outcome candidates whose YES trades at yes_min..yes_max.

    Prediction markets overprice long shots (people like cheap lottery tickets). Buying the NO of
    many small candidates wins most of the time; a candidate that does win costs the whole stake.
    """

    def candidates(self, now: float) -> List[Signal]:
        c = self.cfg
        ymin, ymax = float(c.get("yes_min", 0.02)), float(c.get("yes_max", 0.08))
        horizon = float(c.get("max_days_to_end", 30)) * DAY
        events = self.client.paged("/events", {"active": "true", "closed": "false",
                                               "liquidity_min": c.get("min_liquidity", 1000)},
                                   max_items=int(c.get("max_events", 1000)))
        out = []
        for ev in events:
            if not ev.get("negRisk"):
                continue
            end = _parse_ts(ev.get("endDate"))
            if not end or not now < end <= now + horizon:
                continue
            for m in ev.get("markets") or []:
                if m.get("closed") or not m.get("enableOrderBook"):
                    continue
                toks, prices = _parse_json_list(m.get("clobTokenIds")), _parse_json_list(m.get("outcomePrices"))
                if len(toks) != 2 or len(prices) != 2:
                    continue
                yes = float(prices[0])
                if not ymin <= yes <= ymax:
                    continue
                out.append(Signal(str(toks[1]), f"event:{ev.get('id')}", ev.get("title", ""),
                                  f"NO {m.get('groupItemTitle') or m.get('question', '')[:40]}",
                                  fair=1.0, max_price=min(0.99, 1 - yes + float(c.get("slack", 0.01))),
                                  fee=self.fees.resolve(m, ev.get("category") or ""), end_ts=end,
                                  delay_s=_delay(m), reason=f"yes {yes:.3f}"))
        return out


class WeatherStrategy(Strategy):
    """Temperature buckets where the ensemble forecast disagrees with the price by >= min_edge."""

    def __init__(self, cfg: dict, client, model: Optional[WeatherModel] = None):
        super().__init__(cfg, client)
        self.model = model or WeatherModel(client.get_json, cfg.get("models", "gfs_seamless,ecmwf_ifs025,icon_seamless"),
                                           coords=cfg.get("coords"))
        self.skipped: Dict[str, int] = {}

    def _events(self) -> List[dict]:
        base = {"active": "true", "closed": "false"}
        evs = self.client.paged("/events", dict(base, tag_slug=self.cfg.get("tag_slug", "weather")),
                                max_items=int(self.cfg.get("max_events", 500)))
        if not any("temperature" in (e.get("title") or "").lower() for e in evs):
            evs = self.client.paged("/events", base, max_items=2000)  # tag unknown -> scan everything
        return [e for e in evs if "temperature in" in (e.get("title") or "").lower()]

    def candidates(self, now: float) -> List[Signal]:
        c = self.cfg
        min_edge, days = float(c.get("min_edge", 0.08)), int(c.get("max_days_ahead", 2))
        min_ahead = int(c.get("min_days_ahead", 1))
        sig = {"f": float(c.get("sigma_f", 2.5)), "c": float(c.get("sigma_c", 1.5))}
        max_edge, min_ask = c.get("max_edge", 0.30), float(c.get("min_ask", 0.03))
        today = datetime.fromtimestamp(now, tz=timezone.utc).date()
        out = []
        for ev in self._events():
            parsed = parse_title(ev.get("title", ""), today)
            if not parsed:
                self.skipped["title"] = self.skipped.get("title", 0) + 1
                continue
            kind, city, day = parsed
            if not 0 <= (day - today).days <= days:
                continue
            buckets = []
            for m in ev.get("markets") or []:
                b = parse_bucket(m.get("groupItemTitle") or "")
                toks = _parse_json_list(m.get("clobTokenIds"))
                if b is None or len(toks) != 2 or m.get("closed"):
                    self.skipped["bucket"] = self.skipped.get("bucket", 0) + 1
                    continue
                buckets.append((m, b, toks))
            if not buckets:
                continue
            unit = buckets[0][1][2]
            try:
                members = self.model.members(city, day, kind, unit)
            except Exception as e:  # noqa – one city failing must not stop the scan
                log.warning("forecast %s failed: %s", city, e)
                continue
            if len(members) < 5:
                self.skipped["forecast"] = self.skipped.get("forecast", 0) + 1
                continue
            # never trade a day that has already started where the city is: the market then already
            # sees the measured value, the model only its forecast
            off = self.model.utc_offset.get(city.lower())
            local_today = datetime.fromtimestamp(now + (off or 0), tz=timezone.utc).date()
            if off is None or (day - local_today).days < min_ahead:
                self.skipped["started"] = self.skipped.get("started", 0) + 1
                continue
            end = _parse_ts(ev.get("endDate"))
            for m, (lo, hi, _), toks in buckets:
                p = min(max(bucket_prob(members, lo, hi, sig[unit]), 0.0), 1.0)
                fee = self.fees.resolve(m, ev.get("category") or "weather")
                label = m.get("groupItemTitle") or ""
                why = f"model {p:.3f} ({len(members)} members)"
                for tok, fair, side in ((toks[0], p, "YES"), (toks[1], 1 - p, "NO")):
                    mx = fair - min_edge
                    if mx >= 0.01 and fair >= float(c.get("min_prob", 0.05)):
                        out.append(Signal(str(tok), f"event:{ev.get('id')}", ev.get("title", ""),
                                          f"{side} {label}", fair=fair, max_price=round(mx, 4), fee=fee,
                                          end_ts=end, delay_s=_delay(m), kelly=True, reason=why,
                                          max_edge=None if max_edge is None else float(max_edge), min_ask=min_ask))
        return out


class PriceBandStrategy(Strategy):
    """Sides priced min_price..max_price a few hours before the scheduled end, in markets of one
    `category` whose volume at the time of the trade lies in min_volume..max_volume.

    Market study (69,510 markets, 01.08.-30.09., categories corrected on 30.09. – weather markets carry
    a gameStartTime and had been filed as sport):
      underdog   real sport, 3-10 % sides won ~10 % instead of ~6 %, in both halves of the period
      weather_no weather buckets priced 3-10 % in markets under 5k $ won ~1 %: their NO side at
                 90-97 % won ~99 %, +2.5-3 % per bet after fee and 2 cents, in both halves
    Buys at the real ask and skips wide spreads.

    Optional filters (study 01.10., confirmed or refuted on 11,624 markets the rules were not found on):
      kinds       sport sub-kinds to keep (Fussball, Esports, Tennis, US) – sport underdogs lost in esports
      types       bet types to keep (Ueber/Unter, Spread, Remis, Halbzeit, Sieg, Match-Sieger, ...) –
                  underdogs won on totals/spreads/draws but lost on plain "who wins" markets
      exclude_types  bet types to skip (e.g. Quartalszahlen for finance)
      outcome     only this side, e.g. "No" (weather: every YES bucket was overpriced)
    """

    def candidates(self, now: float) -> List[Signal]:
        from .study import categorize, market_type, sport_kind
        c = self.cfg
        kinds, types = c.get("kinds"), c.get("types")
        exclude_types = set(c.get("exclude_types") or ())
        want_outcome = str(c.get("outcome") or "").lower()
        obs_skip = set(c.get("obs_skip") or ())  # weather: station classes not to buy (see arb.wxobs.gap_class)
        if obs_skip and getattr(self, "_obs", None) is None:
            from .wxobs import LiveObs
            self._obs = LiveObs(self.client.get_json, ttl_s=float(c.get("obs_ttl_s", 600)))
        lo, hi = float(c.get("min_price", 0.03)), float(c.get("max_price", 0.10))
        h_min, h_max = float(c.get("min_hours_to_end", 0.5)) * 3600, float(c.get("max_hours_to_end", 8)) * 3600
        v_min, v_max = float(c.get("min_volume", 0)), float(c.get("max_volume") or 1e18)
        category = c.get("category", "Sport")
        cap = int(c.get("max_markets", 2000))
        markets = self.client.paged("/markets", {
            "active": "true", "closed": "false", "liquidity_num_min": c.get("min_liquidity", 500),
            "end_date_min": _iso(now + h_min), "end_date_max": _iso(now + h_max)}, max_items=cap)
        st = self.scan = {"gelistet": len(markets), "Liste voll": int(len(markets) >= cap)}
        # thousands of 5-minute crypto markets end in any window and can fill the list before the rest:
        # events of the configured topic tags are fetched as well (their markets carry the event's end)
        for tag in c.get("tag_slugs") or ():
            evs = self.client.paged("/events", {"active": "true", "closed": "false", "tag_slug": tag,
                                                "end_date_min": _iso(now + h_min), "end_date_max": _iso(now + h_max)},
                                    max_items=1000)
            extra = [dict(m, endDate=m.get("endDate") or ev.get("endDate"), events=[{"id": ev.get("id")}])
                     for ev in evs for m in ev.get("markets") or [] if not m.get("closed")]
            st[f"Tag {tag}"] = len(extra)
            markets = markets + extra
        out, seen_ids = [], set()

        def drop(why: str) -> None:
            st[why] = st.get(why, 0) + 1

        for m in markets:
            mid = str(m.get("conditionId") or m.get("id"))
            if mid in seen_ids:
                continue
            seen_ids.add(mid)
            end = _parse_ts(m.get("endDate"))
            if not end or not now + h_min <= end <= now + h_max:
                drop("außerhalb Zeitfenster")
                continue
            if not m.get("enableOrderBook") or not m.get("acceptingOrders", True):
                drop("kein Handel")
                continue
            if categorize(m) != category:
                drop("andere Kategorie")
                continue
            q = m.get("question") or ""
            if kinds and sport_kind(q) not in kinds:
                drop("andere Sportart")
                continue
            mtype = market_type(q)
            if (types and mtype not in types) or mtype in exclude_types:
                drop("anderer Wett-Typ")
                continue
            vol = float(m.get("volumeNum") or m.get("volume") or 0)
            if not v_min <= vol < v_max:
                drop("Volumen außerhalb")
                continue
            toks, prices = _parse_json_list(m.get("clobTokenIds")), _parse_json_list(m.get("outcomePrices"))
            outs = _parse_json_list(m.get("outcomes")) or ["Yes", "No"]
            if len(toks) != 2 or len(prices) != 2:
                drop("nicht binär")
                continue
            drop("passend (Kategorie, Typ, Volumen)")
            evs = m.get("events") or [{}]
            group = f"event:{evs[0].get('id')}" if evs[0].get("id") else str(m.get("conditionId") or m.get("id"))
            for i in (0, 1):
                p = float(prices[i])
                if want_outcome and str(outs[i]).lower() != want_outcome:
                    continue
                if lo - 0.01 <= p <= hi:
                    drop("Seite im Preisband")
                    obs_note = ""
                    if obs_skip:  # where today's station reading stands relative to this bucket
                        cls = self._obs.gap(q, m.get("description") or m.get("resolutionSource") or "", now,
                                            float(c.get("obs_delay_min", 15)) * 60)
                        if cls in obs_skip:
                            drop(f"Station: {cls} (ausgelassen)")
                            continue
                        drop(f"Station: {cls}")
                        obs_note = f" | {cls}"
                    out.append(Signal(str(toks[i]), group, m.get("question", ""), str(outs[i]),
                                      fair=min(0.99, p + float(c.get("assumed_edge", 0.03))), max_price=hi,
                                      fee=self.fees.resolve(m, FEE_CATEGORY.get(category, category.lower())),
                                      end_ts=end, delay_s=_delay(m),
                                      reason=f"{c.get('side', 'side')} {p:.3f} {mtype} vol {vol:,.0f}{obs_note}", min_ask=lo,
                                      max_spread=float(c.get("max_spread", 0.03))))
        return out


FEE_CATEGORY = {"Sport": "sports", "Wetter": "weather", "Krypto": "crypto", "Finanz": "finance",
                "Social": "mentions", "Politik": "politics", "Wirtschaft": "economics", "Kultur": "culture",
                "Tech/KI": "tech"}

STRATEGIES = {"endgame": EndgameStrategy, "longshot": LongshotStrategy, "weather": WeatherStrategy,
              "underdog": PriceBandStrategy, "favorite": PriceBandStrategy, "band": PriceBandStrategy}


def prepare_scenario_dir(data_dir: str, name: str, capital: float, reset: str = "") -> None:
    """Start a scenario fresh when its budget (or its `reset` value) changed: move its old files to
    data/archive-<ts>-<name>/. Returns in % are only comparable if a run started with the configured
    capital and one version of the strategy.
    """
    marker = os.path.join(data_dir, f"scenario-{name}.capital")
    files = [os.path.join(data_dir, f"scenario-{name}{ext}") for ext in (".sqlite", ".json")]
    old = None
    if os.path.exists(marker):
        with open(marker, encoding="utf-8") as f:
            old = f.read().strip()
    want = f"{capital:g}" + (f"|{reset}" if reset else "")
    if old != want and any(os.path.exists(p) for p in files):
        arch = os.path.join(data_dir, f"archive-{time.strftime('%Y%m%d-%H%M%S')}-{name}")
        os.makedirs(arch, exist_ok=True)
        for p in files:
            if os.path.exists(p):
                os.replace(p, os.path.join(arch, os.path.basename(p)))
        log.info("%s: budget/reset changed (%s -> %s) – old data archived in %s", name, old, want, arch)
    os.makedirs(data_dir, exist_ok=True)
    with open(marker, "w", encoding="utf-8") as f:
        f.write(want)


def ladder_engine(name: str, cfg: dict, client, clock=None):
    """Logic-ladder arbitrage as a scenario: the multi-leg arbitrage engine (sequential legs, leg
    repair, REST fills) on ladder baskets only, with its own budget, database and portfolio."""
    import copy
    from .engine import Engine
    sc = cfg["scenarios"][name]
    data_dir = os.path.dirname(cfg["storage"]["db_path"]) or "."
    capital = float(sc.get("capital_usd", 2500))
    prepare_scenario_dir(data_dir, name, capital, str(sc.get("reset", "")))
    c = copy.deepcopy(dict(cfg))
    c["portfolio"] = dict(c["portfolio"], starting_capital_usd=capital)
    c["storage"] = dict(c["storage"], db_path=os.path.join(data_dir, f"scenario-{name}.sqlite"))
    c["universe"] = dict(c["universe"], include_binary=False, include_negrisk_baskets=False, include_negrisk_no=False,
                         include_ladders=True, max_ladder_events=int(sc.get("max_events", 300)),
                         max_days_to_resolution=float(sc.get("max_days_to_resolution", 90)))
    c["scanner"] = dict(c["scanner"], **{k: sc[k] for k in ("min_edge_bps", "max_edge_bps", "min_profit_usd",
                                                              "min_annualized_return", "scan_interval_s") if k in sc})
    if "risk" in sc:
        c["risk"] = dict(c["risk"], **sc["risk"])
    return Engine(c, client, clock=clock, persist=True,
                  state_path=os.path.join(data_dir, f"scenario-{name}.json"))


# ====================================================================== engine
class ScenarioEngine:
    def __init__(self, name: str, cfg: dict, client, clock=None, strategy: Optional[Strategy] = None):
        from .engine import RealClock
        self.name, self.cfg, self.client = name, cfg, client
        self.sc = cfg["scenarios"][name]
        self.clock = clock or RealClock()
        kind = self.sc.get("strategy", name)
        if strategy is None and kind == "sharp":  # bookmaker comparison (arb/sharp.py), imported on demand
            from .sharp import SharpStrategy
            data_dir = os.path.dirname(cfg["storage"]["db_path"]) or "."
            strategy = SharpStrategy(dict(self.sc, odds_state_path=self.sc.get(
                "odds_state_path", os.path.join(data_dir, "odds-api.json"))), client)
        self.strategy = strategy or STRATEGIES[kind](self.sc, client)
        self.start_capital = float(self.sc.get("capital_usd", 2500))
        data_dir = os.path.dirname(cfg["storage"]["db_path"]) or "."
        prepare_scenario_dir(data_dir, name, self.start_capital, str(self.sc.get("reset", "")))
        self.store = Store(os.path.join(data_dir, f"scenario-{name}.sqlite"))
        self.store.db.execute("""CREATE TABLE IF NOT EXISTS capacity(token TEXT PRIMARY KEY, title TEXT, end_ts REAL,
            first_ts REAL, last_ts REAL, n INT, best_ask REAL, depth_slip REAL, depth_2c REAL, depth_band REAL)""")
        self.state_path = os.path.join(data_dir, f"scenario-{name}.json")
        self.pf = ScenarioPortfolio.load(self.state_path, self.start_capital)
        ex = cfg["execution"]
        self.latency_s = float(ex.get("latency_ms", 350)) / 1000
        self.haircut = float(ex.get("depth_haircut", 1.0))
        self.market_delay = bool(ex.get("respect_market_delay", True))
        self.max_slippage = float(self.sc.get("max_slippage", 0.03))
        self._cooldown: Dict[str, float] = {}
        self.guarded = 0

    # ------------------------------------------------------------ sizing
    def _budget(self, s: Signal, ask: float) -> tuple:
        sc, eq = self.sc, self.pf.equity
        caps = {"position": float(sc.get("max_position_usd", 50)),
                "cash": self.pf.cash - self.pf.reserved - float(sc.get("cash_buffer_pct", 0.10)) * eq,
                "event": float(sc.get("max_event_usd", 100)) - self.pf.exposure(s.group)}
        if sc.get("max_day_usd") and s.end_ts:  # all markets ending on one (UTC) day: one weather day, one risk
            day = int(s.end_ts // DAY)
            caps["day"] = (float(sc["max_day_usd"]) - sum(p.cost for p in self.pf.positions.values()
                                                          if p.end_ts and int(p.end_ts // DAY) == day)
                           - sum(o["price"] * o["qty"] for o in self.pf.orders.values()
                                 if o.get("end_ts") and int(o["end_ts"] // DAY) == day))
        if s.kelly:  # binary bet at price a with win prob p: f* = (p - a) / (1 - a)
            caps["kelly"] = max(0.0, (s.fair - ask) / (1 - ask)) * float(sc.get("kelly_fraction", 0.25)) * eq
        binding = min(caps, key=caps.get)
        return caps[binding], binding

    # ------------------------------------------------------------ one cycle
    def step(self) -> None:
        now = self.clock.now()
        try:
            signals = self.strategy.candidates(now)
        except Exception as e:  # noqa
            log.error("%s: candidates failed (%s: %s)", self.name, type(e).__name__, e)
            signals = []
        maker = bool(self.sc.get("maker"))
        live_ids = {s.token_id for s in signals}  # maker: an order whose market left the rule is cancelled
        seen = set(self.pf.seen) | set(self.pf.orders)
        signals = [s for s in signals if s.token_id not in seen and self._cooldown.get(s.token_id, 0) <= now]
        tokens = list(dict.fromkeys([s.token_id for s in signals] + list(self.pf.positions) + list(self.pf.orders)))
        books = self.client.books(tokens) if tokens else {}
        n_open = int(self.sc.get("max_open_positions", 20))
        n_new = int(self.sc.get("max_new_per_step", 3))  # spread entries over time, not all in one scan
        # best edge first (fair value minus ask); "safest first" strategies by price
        ranked = sorted((s for s in signals if books.get(s.token_id) and books[s.token_id].asks),
                        key=lambda s: books[s.token_id].best_ask if s.safest_first else s.fair - books[s.token_id].best_ask,
                        reverse=True)
        opened = 0
        n_opps = 0
        full = False
        if maker:
            opened += self._maker_fills(books, live_ids, now)
        for s in ranked:
            ob = books[s.token_id]
            if ob.best_ask > s.max_price + 1e-9:
                continue
            if s.max_spread is not None and (ob.best_bid is None or ob.best_ask - ob.best_bid > s.max_spread + 1e-9):
                self.guarded += 1  # no bid or a wide spread: the edge would be paid away on entry
                continue
            if ob.best_ask < s.min_ask - 1e-9 or (s.max_edge is not None and s.fair - ob.best_ask > s.max_edge):
                self.guarded += 1  # "too good to be true": the market knows something the strategy doesn't
                continue
            # never walk far up a thin book: at most max_slippage above the best ask
            band_max = s.max_price
            s.max_price = min(s.max_price, round(ob.best_ask + self.max_slippage, 4))
            n_opps += 1
            self._capacity(s, ob, band_max, now)  # also when a limit stops the trade: what the market offered
            full = full or len(self.pf.positions) + len(self.pf.orders) >= n_open or opened >= n_new
            if full:
                continue
            if maker:
                opened += self._place(s, ob, now)
                continue
            before = len(self.pf.positions)
            self._trade(s, ob, now)
            opened += len(self.pf.positions) > before
        self._settle(now)
        self._mark(books)
        pf = self.pf
        self.store.scan(now, len(signals), len(books), len(signals), n_opps, None, None, 0.0)
        self.store.equity(self.clock.now(), pf.equity, pf.cash, pf.cost, 0.0, pf.realized_pnl, None)
        self.store.commit()
        pf.save(self.state_path)
        scan = getattr(self.strategy, "scan", None)
        if scan is not None:  # where the markets of the last scan dropped out – for the tab and the export
            scan = dict(scan, Signale=len(signals), **{"unter Max.-Preis": n_opps, "neu gekauft": opened}, ts=now)
            try:
                with open(self.state_path.replace(".json", ".scan.json"), "w", encoding="utf-8") as f:
                    json.dump(scan, f)
            except OSError:
                pass
        skipped = getattr(self.strategy, "skipped", None)
        log.info("%s: %d signals, %d below max price, %d open, equity %.2f, guarded %d%s", self.name, len(signals),
                 n_opps, len(pf.positions), pf.equity, self.guarded, f", skipped {skipped}" if skipped else "")

    def _capacity(self, s: Signal, ob: OrderBook, band_max: float, now: float) -> None:
        """Dollars on the ask side up to the scenario's buy limit (ask + max_slippage), up to ask + 2 cents and
        up to the rule's price limit – once per token, the largest seen. Sums per day = what the strategy
        could really put to work, independent of its own budget."""
        def depth(limit: float) -> float:
            return sum(lv.price * lv.size for lv in ob.asks if lv.price <= limit + 1e-9)
        self.store.db.execute(
            """INSERT INTO capacity VALUES(?,?,?,?,?,1,?,?,?,?)
               ON CONFLICT(token) DO UPDATE SET last_ts=excluded.last_ts, n=n+1,
                 depth_slip=MAX(depth_slip, excluded.depth_slip), depth_2c=MAX(depth_2c, excluded.depth_2c),
                 depth_band=MAX(depth_band, excluded.depth_band)""",
            (s.token_id, s.title[:200], s.end_ts, now, now, ob.best_ask, depth(s.max_price),
             depth(ob.best_ask + 0.02), depth(band_max)))

    def _trade(self, s: Signal, ob: OrderBook, now: float) -> None:
        budget, binding = self._budget(s, ob.best_ask)
        # how many shares fit the budget at the visible asks up to max_price
        qty = spend = 0.0
        for lv in ob.asks:
            if lv.price > s.max_price + 1e-9 or spend >= budget:
                break
            per = lv.price + s.fee.per_share(lv.price)
            take = min(lv.size * self.haircut, (budget - spend) / per)
            qty, spend = qty + take, spend + take * per
        qty = math.floor(qty * 100) / 100
        basket = Basket(f"{self.name}:{s.token_id}", self.name, s.title, [s.token_id], [s.label], [s.fee],
                        end_ts=s.end_ts, category="", delay_s=s.delay_s)
        avg = spend / qty if qty else ob.best_ask
        leg = Leg(s.token_id, s.label, "BUY", s.max_price, qty, avg, s.fee.per_share(avg) * qty)
        opp = Opportunity(basket, "buy", qty, [leg], gross_usd=avg * qty, fees_usd=leg.fee_usd,
                          net_profit_usd=qty * s.fair - spend, capital_usd=spend,
                          edge_bps=(qty * s.fair - spend) / spend * 1e4 if spend else 0.0,
                          lockup_days=max(((s.end_ts or now) - now) / DAY, 0.0), detected_ts=now)
        if qty < ob.min_order_size:
            self.store.opportunity(opp, "rejected", f"no capacity ({binding})", qty)
            self._cooldown[s.token_id] = now + 1800
            return
        self.store.opportunity(opp, "accepted", "full size" if binding == "position" else f"sized down by {binding}", qty)

        # --- execution: latency (+ market order delay), fresh book, IOC up to max_price
        delay = s.delay_s if self.market_delay else 0.0
        self.clock.sleep(self.latency_s + delay)
        fresh = self.client.books([s.token_id]).get(s.token_id)
        got, parts = _ioc(fresh.asks, s.max_price, qty, "BUY", self.haircut) if fresh else (0.0, [])
        notional = sum(p * z for p, z in parts)
        fee = sum(z * s.fee.per_share(p) for p, z in parts)
        cost = notional + fee
        status = "missed" if got <= 1e-9 else ("filled" if got >= qty - 1e-6 else "partial")
        note = f"ask={ob.best_ask:.3f} max={s.max_price:.3f} fair={s.fair:.3f} {s.reason}" + (
            f" delay={delay:g}s" if delay else "")
        fills = [Fill(s.token_id, "BUY", got, notional / got if got else 0.0, fee)]
        if got > 1e-9 and status == "partial":  # log what was really spent, not the planned size
            import dataclasses
            opp = dataclasses.replace(opp, capital_usd=cost, net_profit_usd=got * s.fair - cost)
        res = ExecutionResult(opp, status, got, fills, locked_capital=cost, expected_payout=got * s.fair, note=note)
        self.store.execution(self.clock.now(), res, (self.latency_s + delay) * 1000)
        if got <= 1e-9:
            self._cooldown[s.token_id] = now + 1800
            return
        self.pf.cash -= cost
        self.pf.positions[s.token_id] = Position(s.token_id, s.group, s.title, s.label, got, cost, s.fair,
                                                 self.clock.now(), s.end_ts)
        self.pf.seen.append(s.token_id)
        log.info("%s: %s %.1f x %s @ %.3f (fair %.3f) %s", self.name, status, got, s.label[:30],
                 notional / got, s.fair, s.title[:50])

    # ------------------------------------------------------------ maker mode
    def _place(self, s: Signal, ob: OrderBook, now: float) -> int:
        """Maker mode: instead of paying the ask, rest a limit buy one tick above the best bid (or at the bid
        when the spread is one tick). No fill now – see _maker_fills."""
        if ob.best_bid is None:
            return 0
        tick = float(self.sc.get("tick", 0.01))
        price = round(ob.best_bid + tick, 4) if ob.best_ask - ob.best_bid > tick + 1e-9 else ob.best_bid
        if not s.min_ask - 1e-9 <= price <= s.max_price + 1e-9:
            return 0
        budget, binding = self._budget(s, price)
        qty = math.floor(budget / price * 100) / 100 if price > 0 else 0.0
        if qty < max(ob.min_order_size, 1.0):
            self._cooldown[s.token_id] = now + 1800
            return 0
        self.pf.orders[s.token_id] = dict(group=s.group, title=s.title, label=s.label, price=price, qty=qty,
                                          fair=s.fair, end_ts=s.end_ts, placed_ts=now, ask=ob.best_ask,
                                          bid=ob.best_bid, reason=s.reason)
        log.info("%s: limit buy %.1f x %s @ %.3f (bid %.3f ask %.3f) %s", self.name, qty, s.label[:30], price,
                 ob.best_bid, ob.best_ask, s.title[:50])
        return 1

    def _maker_fills(self, books: Dict[str, OrderBook], live_ids: set, now: float) -> int:
        """Strict fill rule for resting orders: filled only when the ask has dropped BELOW the limit – a seller
        crossed every order at our price, ours included (at the limit itself the queue in front might take
        it all). Filled size = what the book shows up to the limit, at the limit price, no maker fee.
        Cancelled when the market no longer passes the rule (price band, station reading), when it ends
        within min_hours_to_end, or after order_ttl_h."""
        filled = 0
        h_min = float(self.sc.get("min_hours_to_end", 2)) * 3600
        ttl = float(self.sc.get("order_ttl_h", 24)) * 3600
        tick = float(self.sc.get("tick", 0.01))
        for t, o in list(self.pf.orders.items()):
            ob = books.get(t)
            if ob and ob.asks and ob.best_ask <= o["price"] - tick + 1e-9:
                avail = sum(lv.size * self.haircut for lv in ob.asks if lv.price <= o["price"] + 1e-9)
                got = math.floor(min(o["qty"], avail) * 100) / 100
                if got > 0:
                    self._book_maker_fill(t, o, got, now)
                    filled += 1
                    continue
            why = ("Markt fällt aus der Regel" if t not in live_ids else
                   "kurz vor Schluss" if o.get("end_ts") and now > o["end_ts"] - h_min else
                   "zu alt" if now - o["placed_ts"] > ttl else None)
            if why:
                del self.pf.orders[t]
                self.pf.seen.append(t)  # one chance per market, like the taker scenarios
                log.info("%s: limit order cancelled (%s): %s", self.name, why, o["title"][:50])
        return filled

    def _book_maker_fill(self, t: str, o: dict, got: float, now: float) -> None:
        cost = got * o["price"]
        basket = Basket(f"{self.name}:{t}", self.name, o["title"], [t], [o["label"]], [FeeSpec()],
                        end_ts=o.get("end_ts"), category="", delay_s=0.0)
        leg = Leg(t, o["label"], "BUY", o["price"], got, o["price"], 0.0)
        opp = Opportunity(basket, "buy", got, [leg], gross_usd=cost, fees_usd=0.0,
                          net_profit_usd=got * o["fair"] - cost, capital_usd=cost,
                          edge_bps=(got * o["fair"] - cost) / cost * 1e4 if cost else 0.0,
                          lockup_days=max(((o.get("end_ts") or now) - now) / DAY, 0.0), detected_ts=o["placed_ts"])
        status = "filled" if got >= o["qty"] - 1e-6 else "partial"
        waited = (now - o["placed_ts"]) / 60
        note = (f"maker limit={o['price']:.3f} (bid {o['bid']:.3f} ask {o['ask']:.3f} beim Setzen) "
                f"gefüllt nach {waited:.0f} min fair={o['fair']:.3f} {o['reason']}")
        res = ExecutionResult(opp, status, got, [Fill(t, "BUY", got, o["price"], 0.0)], locked_capital=cost,
                              expected_payout=got * o["fair"], note=note)
        self.store.execution(now, res, 0.0)
        del self.pf.orders[t]
        self.pf.cash -= cost
        self.pf.positions[t] = Position(t, o["group"], o["title"], o["label"], got, cost, o["fair"], now, o.get("end_ts"))
        self.pf.seen.append(t)
        log.info("%s: maker %s %.1f x %s @ %.3f after %.0f min %s", self.name, status, got, o["label"][:30],
                 o["price"], waited, o["title"][:50])

    def _settle(self, now: float) -> None:
        every = float(self.sc.get("resolution_check_min", 30)) * 60
        for t, p in list(self.pf.positions.items()):
            if (p.end_ts and now < p.end_ts) or now - p.last_check < every:
                continue
            p.last_check = now
            final = self.client.token_resolution(t)
            if final is None:
                continue
            payout = p.qty * final
            pnl = payout - p.cost
            self.pf.cash += payout
            self.pf.realized_pnl += pnl
            del self.pf.positions[t]
            self.pf.marks.pop(t, None)
            self.store.settlement(now, f"{self.name}:{t}", "position", p.qty, payout, pnl)
            log.info("%s: settled %s %s -> %.0f, pnl %+.2f", self.name, p.label[:30], p.title[:40], final, pnl)

    def _mark(self, books: Dict[str, OrderBook]) -> None:
        for t in self.pf.positions:
            ob = books.get(t)
            if ob and ob.best_bid is not None:
                self.pf.marks[t] = ob.best_bid

    # ------------------------------------------------------------ run
    def run(self, duration_s: Optional[float] = None, exit_on_code_change: bool = False) -> None:
        from .engine import Engine
        code_m = Engine._code_mtime(self) if exit_on_code_change else None
        start = self.clock.now()
        interval = float(self.sc.get("interval_min", 10)) * 60
        while True:
            t = self.clock.now()
            try:
                self.step()
            except Exception:  # noqa – keep the scenario alive
                log.exception("%s: step failed", self.name)
            if duration_s and self.clock.now() - start >= duration_s:
                return
            while self.clock.now() - t < interval:
                self.clock.sleep(min(5.0, interval))
                if code_m is not None and Engine._code_mtime(self) != code_m:
                    log.info("%s: code updated on disk – exiting for restart", self.name)
                    return
