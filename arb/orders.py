"""Order gateway between a strategy and the exchange: off, dry run, (later) live.

The strategy says which resting orders it wants right now; `sync` compares that with the orders the gateway
holds and derives the actions – place, cancel, replace (price changed). In dry run every action is checked
against hard limits and written to the scenario database (table live_orders) and data/live-status.json,
nothing is sent. Live trading is not implemented: mode "live" is treated as dry run and says so.

Mode: data/live.json {"mode": "off" | "dry" | "live"}, set on the settings page (/admin/). Switching to "off"
is the emergency stop: every held order is cancelled (logged) and nothing new is placed.

Hard limits (per scenario, config `live:`), independent of the strategy:
  max_order_usd    one order (price x size)
  max_market_usd   all orders of one token
  max_total_usd    all open orders together
An order beyond a limit is rejected and logged, the strategy keeps running.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Dict, Optional, Tuple

log = logging.getLogger("polyarb.orders")
MODES = ("off", "dry", "live")


def read_mode(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            m = json.load(f).get("mode", "off")
        return m if m in MODES else "off"
    except (OSError, ValueError, AttributeError):
        return "off"


def write_mode(path: str, mode: str) -> None:
    if mode not in MODES:
        raise ValueError(mode)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"mode": mode, "ts": time.time()}, f)
    os.replace(tmp, path)


class Gateway:
    def __init__(self, name: str, db, mode_path: str, status_path: str, limits: Optional[dict] = None):
        self.name, self.db = name, db
        self.mode_path, self.status_path = mode_path, status_path
        lim = limits or {}
        self.max_order = float(lim.get("max_order_usd", 30))
        self.max_market = float(lim.get("max_market_usd", 60))
        self.max_total = float(lim.get("max_total_usd", 300))
        self.orders: Dict[str, dict] = {}   # key (token) -> {price, size, oid, ts}
        self.counts = {"gesetzt": 0, "storniert": 0, "abgelehnt": 0}
        self._seq = 0
        self.mode = "off"
        db.execute("""CREATE TABLE IF NOT EXISTS live_orders(ts REAL, mode TEXT, action TEXT, token TEXT, label TEXT,
                      price REAL, size REAL, oid TEXT, note TEXT)""")

    # ------------------------------------------------------------------ helpers
    def _log(self, now: float, action: str, token: str, label: str, price: float, size: float, oid: str, note: str = ""):
        self.db.execute("INSERT INTO live_orders VALUES(?,?,?,?,?,?,?,?,?)",
                        (now, self.mode, action, token, label[:120], price, size, oid, note))

    def open_usd(self) -> float:
        return sum(o["price"] * o["size"] for o in self.orders.values())

    def _cancel(self, now: float, token: str, why: str) -> None:
        o = self.orders.pop(token)
        self.counts["storniert"] += 1
        self._log(now, "cancel", token, o.get("label", ""), o["price"], o["size"], o["oid"], why)

    def _place(self, now: float, token: str, label: str, price: float, size: float) -> None:
        usd = price * size
        why = ("Order > max_order_usd" if usd > self.max_order + 1e-9 else
               "Markt > max_market_usd" if usd > self.max_market + 1e-9 else
               "gesamt > max_total_usd" if self.open_usd() + usd > self.max_total + 1e-9 else None)
        if why:
            self.counts["abgelehnt"] += 1
            self._log(now, "reject", token, label, price, size, "", why)
            return
        self._seq += 1
        oid = f"{self.mode}-{self.name}-{self._seq}"
        self.orders[token] = dict(price=price, size=size, oid=oid, ts=now, label=label)
        self.counts["gesetzt"] += 1
        self._log(now, "place", token, label, price, size, oid)

    # ------------------------------------------------------------------ main
    def sync(self, now: float, want: Dict[str, Tuple[float, float, str]]) -> str:
        """want: token -> (price, size, label) of every order the strategy wants resting now."""
        mode = read_mode(self.mode_path)
        if mode == "off":
            if self.orders:  # emergency stop / switched off: cancel everything we hold
                for t in list(self.orders):
                    self._cancel(now, t, "ausgeschaltet")
            self.mode = mode
            self._status(now, "aus")
            return mode
        self.mode = "dry"  # live is not implemented: never send anything
        for t in list(self.orders):
            w = want.get(t)
            if w is None:
                self._cancel(now, t, "nicht mehr gewollt")
            elif abs(w[0] - self.orders[t]["price"]) > 1e-9:
                self._cancel(now, t, f"Preis {self.orders[t]['price']:.3f} -> {w[0]:.3f}")
        for t, (price, size, label) in want.items():
            if t not in self.orders and size > 0:
                self._place(now, t, label, price, size)
        self._status(now, "Trockenlauf" + (" (echt ist noch nicht freigeschaltet)" if mode == "live" else ""))
        return mode

    def _status(self, now: float, mode_text: str) -> None:
        try:
            tmp = self.status_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(dict(scenario=self.name, mode=mode_text, ts=now, open_orders=len(self.orders),
                               open_usd=round(self.open_usd(), 2), limits=dict(max_order_usd=self.max_order,
                               max_market_usd=self.max_market, max_total_usd=self.max_total), **self.counts), f)
            os.replace(tmp, self.status_path)
        except OSError:
            pass
