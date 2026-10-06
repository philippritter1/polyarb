"""Real-time order books via Polymarket's market WebSocket.

wss://ws-subscriptions-clob.polymarket.com/ws/market
  -> subscribe {"assets_ids": [...], "type": "market"}
  <- "book"          full snapshot per asset (sent on subscribe and after trades)
  <- "price_change"  level updates: price_changes[{asset_id, price, size, side}] (size = new total, 0 = removed)
  <- "last_trade_price"  a trade: {asset_id, price, size, side, fee_rate_bps, timestamp}
Keep-alive: send text "PING" every 10 s.

BookStore keeps the local books; every update marks the touched tokens dirty so the
engine re-checks only the affected baskets – within milliseconds instead of a 10 s poll.
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections import deque
from typing import Callable, Dict, Iterable, List, Optional, Set

from .models import Level, OrderBook

log = logging.getLogger("polyarb.stream")

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class BookStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._bids: Dict[str, Dict[float, float]] = {}
        self._asks: Dict[str, Dict[float, float]] = {}
        self._ts: Dict[str, float] = {}
        self._valid: Set[str] = set()
        self._dirty: Set[str] = set()
        self.meta: Dict[str, tuple] = {}   # token -> (min_order_size, tick_size)
        self.updates = 0
        # trades ("last_trade_price"): (asset_id, price, size, ts) – read by the market-making simulation
        self.trades: deque = deque(maxlen=50_000)

    # ---------------------------------------------------------------- writes (WS threads)
    def snapshot(self, tid: str, bids, asks, ts: Optional[float] = None) -> None:
        with self._lock:
            self._bids[tid] = {float(x["price"]): float(x["size"]) for x in bids if float(x["size"]) > 0}
            self._asks[tid] = {float(x["price"]): float(x["size"]) for x in asks if float(x["size"]) > 0}
            self._ts[tid] = ts or time.time()
            self._valid.add(tid)
            self._dirty.add(tid)
            self.updates += 1

    def change(self, tid: str, price: float, size: float, side: str, ts: Optional[float] = None) -> None:
        with self._lock:
            if tid not in self._valid:
                return  # no snapshot yet – ignore deltas
            book = self._bids[tid] if side.upper() in ("BUY", "BID") else self._asks[tid]
            if size <= 0:
                book.pop(price, None)
            else:
                book[price] = size
            self._ts[tid] = ts or time.time()
            self._dirty.add(tid)
            self.updates += 1

    def invalidate(self, tids: Iterable[str]) -> None:
        with self._lock:
            for t in tids:
                self._valid.discard(t)

    # ---------------------------------------------------------------- reads (engine thread)
    def pop_dirty(self) -> Set[str]:
        with self._lock:
            d, self._dirty = self._dirty, set()
            return d

    def is_valid(self, tid: str) -> bool:
        return tid in self._valid

    def books(self, tids: Iterable[str]) -> Dict[str, OrderBook]:
        out: Dict[str, OrderBook] = {}
        with self._lock:
            for t in tids:
                if t not in self._valid:
                    continue
                mn, tick = self.meta.get(t, (5.0, 0.01))
                out[t] = OrderBook(
                    token_id=t,
                    bids=[Level(p, s) for p, s in sorted(self._bids[t].items(), key=lambda x: -x[0])],
                    asks=[Level(p, s) for p, s in sorted(self._asks[t].items(), key=lambda x: x[0])],
                    min_order_size=mn, tick_size=tick, ts=self._ts.get(t, 0.0))
        return out

    @property
    def n_valid(self) -> int:
        return len(self._valid)


def handle_message(store: BookStore, raw: str) -> None:
    if raw in ("PONG", "PING", ""):
        return
    try:
        data = json.loads(raw)
    except ValueError:
        return
    events = data if isinstance(data, list) else [data]
    for ev in events:
        et = ev.get("event_type") or ev.get("type")
        ts = float(ev["timestamp"]) / 1000 if ev.get("timestamp") else None
        if et == "book":
            store.snapshot(str(ev["asset_id"]), ev.get("bids") or ev.get("buys") or [],
                           ev.get("asks") or ev.get("sells") or [], ts)
        elif et == "price_change":
            changes = ev.get("price_changes") or ev.get("changes") or []
            for ch in changes:
                tid = str(ch.get("asset_id") or ev.get("asset_id"))
                store.change(tid, float(ch["price"]), float(ch["size"]), ch.get("side", ""), ts)
        elif et == "last_trade_price":
            try:
                store.trades.append((str(ev["asset_id"]), float(ev["price"]), float(ev["size"]), ts or time.time()))
            except (KeyError, TypeError, ValueError):
                pass
        elif et == "tick_size_change":
            tid = str(ev.get("asset_id"))
            mn, _ = store.meta.get(tid, (5.0, 0.01))
            store.meta[tid] = (mn, float(ev.get("new_tick_size") or 0.01))


class MarketStream:
    """One WebSocket connection for a chunk of assets, with ping + auto-reconnect."""

    def __init__(self, store: BookStore, asset_ids: List[str], url: str = WS_URL, name: str = "ws"):
        self.store, self.assets, self.url, self.name = store, list(asset_ids), url, name
        self._stop = threading.Event()
        self._ws = None
        self.connected = False
        self.reconnects = 0
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    def _run(self):
        import websocket  # websocket-client

        backoff = 1.0
        while not self._stop.is_set():
            try:
                ws = websocket.create_connection(self.url, timeout=30, enable_multithread=True)
                self._ws = ws
                ws.send(json.dumps({"assets_ids": self.assets, "type": "market"}))
                self.connected, backoff = True, 1.0
                log.info("%s connected (%d assets)", self.name, len(self.assets))
                last_ping = time.time()
                ws.settimeout(5)
                while not self._stop.is_set():
                    if time.time() - last_ping >= 10:
                        ws.send("PING")
                        last_ping = time.time()
                    try:
                        msg = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if msg is None or msg == "":
                        if not ws.connected:
                            raise ConnectionError("closed by server")
                        continue
                    handle_message(self.store, msg)
            except Exception as e:  # noqa
                if self._stop.is_set():
                    break
                log.warning("%s disconnected (%s: %s) – reconnect in %.0fs", self.name, type(e).__name__, e, backoff)
            finally:
                self.connected = False
                self.store.invalidate(self.assets)  # books stale until fresh snapshot arrives
                try:
                    if self._ws:
                        self._ws.close()
                except Exception:
                    pass
            if self._stop.is_set():
                break
            self.reconnects += 1
            time.sleep(backoff + random.random())
            backoff = min(backoff * 2, 60)


class StreamPool:
    def __init__(self, store: BookStore, chunk: int = 400, url: str = WS_URL):
        self.store, self.chunk, self.url = store, chunk, url
        self.streams: List[MarketStream] = []
        self.assets: Set[str] = set()

    def set_assets(self, assets: Iterable[str]) -> None:
        new = set(assets)
        if new == self.assets and self.streams:
            return
        self.stop()
        self.assets = new
        ids = sorted(new)
        for i in range(0, len(ids), self.chunk):
            self.streams.append(MarketStream(self.store, ids[i:i + self.chunk], self.url, f"ws{i // self.chunk}").start())
        log.info("stream pool: %d assets over %d connections", len(ids), len(self.streams))

    def stop(self) -> None:
        for s in self.streams:
            s.stop()
        self.streams = []

    @property
    def connected(self) -> int:
        return sum(s.connected for s in self.streams)
