"""Read-only Polymarket data client (Gamma API for metadata, CLOB API for order books).

No keys needed – all endpoints used here are public.
"""
from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime
from typing import Dict, Iterable, List, Optional

import requests

from .models import Basket, FeeSpec, Level, OrderBook

log = logging.getLogger(__name__)


def _parse_json_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return []


def _parse_ts(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class PaginationEnd(Exception):
    """4xx from the API, e.g. offset beyond what Gamma allows (422)."""


MAX_OFFSET = 2000  # Gamma rejects deeper pagination
BACKOFF = [2, 5, 15, 30]
PAGE = 100         # Gamma returns at most 100 items per page


class FeeResolver:
    def __init__(self, fee_cfg: dict):
        self.default_rate = float(fee_cfg.get("default_rate", 0.05))
        self.exponent = float(fee_cfg.get("exponent", 1.0))
        self.cat_rates = {k.lower(): float(v) for k, v in (fee_cfg.get("category_rates") or {}).items()}

    def resolve(self, market: dict, category: str = "") -> FeeSpec:
        if market.get("feesEnabled") is False:
            return FeeSpec(0.0, self.exponent)
        sched = market.get("feeSchedule")
        if isinstance(sched, dict) and sched.get("rate") is not None:
            rate = float(sched["rate"])
            if rate > 1:  # delivered in bps
                rate /= 10_000
            return FeeSpec(rate, float(sched.get("exponent") or self.exponent))
        cat = (category or market.get("category") or "").lower()
        for key, rate in self.cat_rates.items():
            if key and key in cat:
                return FeeSpec(rate, self.exponent)
        # conservative fallback: assume fees apply
        return FeeSpec(self.default_rate, self.exponent)


class PolymarketClient:
    def __init__(self, api_cfg: dict, fee_cfg: dict):
        self.gamma = api_cfg["gamma_url"].rstrip("/")
        self.clob = api_cfg["clob_url"].rstrip("/")
        self.batch = int(api_cfg.get("books_batch_size", 100))
        self.timeout = float(api_cfg.get("timeout_s", 10))
        self.min_interval = float(api_cfg.get("min_request_interval_s", 0.12))
        self.fees = FeeResolver(fee_cfg)
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "polyarb-paper/0.1"
        self._last = 0.0

    # -------------------------------------------------------------- http
    def _reset_session(self):
        self.s.close()
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "polyarb-paper/0.1"

    def _throttle(self):
        wait = self.min_interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()

    def _get(self, url: str, params: dict | None = None):
        for attempt in range(4):
            self._throttle()
            try:
                r = self.s.get(url, params=params, timeout=self.timeout)
                if r.status_code == 429:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                if 400 <= r.status_code < 500:
                    raise PaginationEnd(f"{r.status_code} for {r.url}")  # client error: retrying won't help
                r.raise_for_status()
                return r.json()
            except requests.RequestException as e:
                log.warning("GET %s failed (%s), retry %d", url, type(e).__name__, attempt + 1)
                time.sleep(BACKOFF[attempt] + random.random())
                self._reset_session()
        raise RuntimeError(f"GET {url} failed repeatedly")

    def _post(self, url: str, payload):
        for attempt in range(4):
            self._throttle()
            try:
                r = self.s.post(url, json=payload, timeout=self.timeout)
                if r.status_code == 429:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                r.raise_for_status()
                return r.json()
            except requests.RequestException as e:
                log.warning("POST %s failed (%s), retry %d", url, type(e).__name__, attempt + 1)
                time.sleep(BACKOFF[attempt] + random.random())
                self._reset_session()
        raise RuntimeError(f"POST {url} failed repeatedly")

    # -------------------------------------------------------------- universe
    def binary_baskets(self, min_liquidity: float, max_markets: int) -> List[Basket]:
        out: List[Basket] = []
        offset = 0
        while len(out) < max_markets and offset <= MAX_OFFSET:
            try:
                page = self._get(f"{self.gamma}/markets", {
                    "active": "true", "closed": "false", "limit": PAGE, "offset": offset,
                    "liquidity_num_min": min_liquidity, "order": "volume24hr", "ascending": "false",
                })
            except PaginationEnd as e:
                log.info("markets pagination stopped: %s", e)
                break
            if not page:
                break
            for m in page:
                b = self._binary_from_market(m)
                if b:
                    out.append(b)
            offset += len(page)
            if len(page) < PAGE:
                break
        return out[:max_markets]

    def _binary_from_market(self, m: dict) -> Optional[Basket]:
        if not m.get("enableOrderBook") or not m.get("acceptingOrders", True):
            return None
        tokens = _parse_json_list(m.get("clobTokenIds"))
        outcomes = _parse_json_list(m.get("outcomes"))
        if len(tokens) != 2:
            return None
        fee = self.fees.resolve(m)
        return Basket(
            basket_id=m.get("conditionId") or str(m.get("id")),
            kind="binary",
            title=m.get("question", ""),
            token_ids=[str(t) for t in tokens],
            labels=[str(o) for o in (outcomes or ["YES", "NO"])],
            fees=[fee, fee],
            end_ts=_parse_ts(m.get("endDate")),
            category=m.get("category") or "",
        )

    def negrisk_baskets(self, min_liquidity: float, max_events: int = 300) -> List[Basket]:
        out: List[Basket] = []
        offset = 0
        ordered = True
        n_events = 0
        while n_events < max_events and offset <= MAX_OFFSET:
            params = {"active": "true", "closed": "false", "limit": PAGE, "offset": offset,
                      "liquidity_min": min_liquidity}
            if ordered:
                params.update(order="volume24hr", ascending="false")
            try:
                page = self._get(f"{self.gamma}/events", params)
            except PaginationEnd as e:
                if offset == 0 and ordered:
                    ordered = False  # fall back to unordered listing
                    continue
                log.info("events pagination stopped: %s", e)
                break
            if not page:
                break
            for ev in page:
                bs = self._baskets_from_event(ev)
                if bs and n_events < max_events:
                    out.extend(bs)
                    n_events += 1
            offset += len(page)
            if len(page) < PAGE:
                break
        return out

    def _baskets_from_event(self, ev: dict) -> List[Basket]:
        """YES basket (exactly one YES pays $1) and NO basket (all NO pay $n-1) of a negRisk event.

        Only complete, non-augmented events: every outcome must be listed and tradable.
        """
        if not ev.get("negRisk") or ev.get("negRiskAugmented"):
            return []
        markets = ev.get("markets") or []
        if len(markets) < 3:
            return []
        yes, no, labels, fees = [], [], [], []
        cat = ev.get("category") or ""
        for m in markets:
            if m.get("closed") or not m.get("active", True) or not m.get("enableOrderBook"):
                return []  # incomplete basket -> not riskless
            t = _parse_json_list(m.get("clobTokenIds"))
            if len(t) != 2:
                return []
            yes.append(str(t[0]))
            no.append(str(t[1]))
            labels.append(m.get("groupItemTitle") or m.get("question", "")[:40])
            fees.append(self.fees.resolve(m, cat))
        common = dict(title=ev.get("title", ""), labels=labels, fees=fees,
                      end_ts=_parse_ts(ev.get("endDate")), category=cat)
        return [
            Basket(basket_id=f"event:{ev.get('id')}", kind="negrisk", token_ids=yes, **common),
            Basket(basket_id=f"event:{ev.get('id')}:no", kind="negrisk_no", token_ids=no,
                   payout=float(len(no) - 1), **common),
        ]

    # -------------------------------------------------------------- books
    def books(self, token_ids: Iterable[str]) -> Dict[str, OrderBook]:
        ids = list(dict.fromkeys(token_ids))
        out: Dict[str, OrderBook] = {}
        for i in range(0, len(ids), self.batch):
            chunk = ids[i:i + self.batch]
            data = self._post(f"{self.clob}/books", [{"token_id": t} for t in chunk])
            for raw in data or []:
                ob = parse_book(raw)
                if ob:
                    out[ob.token_id] = ob
        return out

    def market_resolution(self, token_id: str) -> Optional[float]:
        """Returns final price (1.0/0.0) of a token once its market is resolved, else None."""
        data = self._get(f"{self.gamma}/markets", {"clob_token_ids": token_id, "closed": "true"})
        if not data:
            return None
        m = data[0]
        tokens = _parse_json_list(m.get("clobTokenIds"))
        prices = _parse_json_list(m.get("outcomePrices"))
        if token_id in tokens and prices:
            p = float(prices[tokens.index(token_id)])
            if p in (0.0, 1.0):
                return p
        return None

    def token_resolution(self, token_id: str) -> Optional[float]:
        return self.market_resolution(token_id)

    def basket_resolution(self, token_ids: List[str]) -> Optional[bool]:
        """True if exactly-one-pays basket paid out, False if none did, None if still pending."""
        finals = [self.market_resolution(t) for t in token_ids]
        if any(f is None for f in finals):
            return None
        return any(f == 1.0 for f in finals)


def parse_book(raw: dict) -> Optional[OrderBook]:
    tid = raw.get("asset_id") or raw.get("token_id")
    if not tid:
        return None
    bids = sorted((Level(float(x["price"]), float(x["size"])) for x in raw.get("bids", [])),
                  key=lambda l: -l.price)
    asks = sorted((Level(float(x["price"]), float(x["size"])) for x in raw.get("asks", [])),
                  key=lambda l: l.price)
    ts = raw.get("timestamp")
    return OrderBook(
        token_id=str(tid), bids=bids, asks=asks,
        min_order_size=float(raw.get("min_order_size") or 5),
        tick_size=float(raw.get("tick_size") or 0.01),
        ts=float(ts) / 1000 if ts else time.time(),
    )
