"""SQLite persistence for opportunities, executions, equity curve and scan stats."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import ExecutionResult, Opportunity

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans(
  ts REAL, n_baskets INT, n_books INT, raw_signals INT, n_opps INT,
  best_buy_sum REAL, best_sell_sum REAL, duration_ms REAL);
CREATE TABLE IF NOT EXISTS opportunities(
  ts REAL, basket_id TEXT, kind TEXT, direction TEXT, title TEXT, qty REAL,
  capital REAL, net_profit REAL, fees REAL, edge_bps REAL, annualized REAL,
  lockup_days REAL, decision TEXT, reason TEXT, sized_qty REAL);
CREATE TABLE IF NOT EXISTS executions(
  ts REAL, basket_id TEXT, title TEXT, strategy TEXT, status TEXT,
  target_qty REAL, matched_qty REAL, capital REAL, expected_profit REAL,
  realized_pnl REAL, locked REAL, expected_payout REAL, residual REAL,
  latency_ms REAL, note TEXT, fills TEXT);
CREATE TABLE IF NOT EXISTS equity(
  ts REAL, equity REAL, cash REAL, locked REAL, residual REAL,
  realized_cum REAL, halted TEXT);
CREATE TABLE IF NOT EXISTS settlements(
  ts REAL, ref TEXT, kind TEXT, qty REAL, payout REAL, pnl REAL);
"""


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def scan(self, ts, n_baskets, n_books, raw, n_opps, best_buy, best_sell, dur_ms):
        self.db.execute("INSERT INTO scans VALUES(?,?,?,?,?,?,?,?)",
                        (ts, n_baskets, n_books, raw, n_opps, best_buy, best_sell, dur_ms))

    def opportunity(self, o: Opportunity, decision: str, reason: str, sized_qty: float):
        self.db.execute(
            "INSERT INTO opportunities VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (o.detected_ts, o.basket.basket_id, o.basket.kind, o.direction, o.basket.title[:200],
             o.qty, o.capital_usd, o.net_profit_usd, o.fees_usd, o.edge_bps, o.annualized,
             o.lockup_days, decision, reason, sized_qty))

    def execution(self, ts: float, r: ExecutionResult, latency_ms: float):
        o = r.opp
        fills = [f.__dict__ for f in r.fills] + [dict(f.__dict__, unwind=True) for f in r.unwind_fills]
        self.db.execute(
            "INSERT INTO executions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, o.basket.basket_id, o.basket.title[:200], o.strategy, r.status, o.qty, r.matched_qty,
             o.capital_usd, o.net_profit_usd, r.realized_pnl, r.locked_capital, r.expected_payout,
             r.residual_exposure_usd, latency_ms, r.note, json.dumps(fills)))

    def equity(self, ts, equity, cash, locked, residual, realized, halted):
        self.db.execute("INSERT INTO equity VALUES(?,?,?,?,?,?,?)",
                        (ts, equity, cash, locked, residual, realized, halted))

    def settlement(self, ts, ref, kind, qty, payout, pnl):
        self.db.execute("INSERT INTO settlements VALUES(?,?,?,?,?,?)", (ts, ref, kind, qty, payout, pnl))

    def commit(self):
        self.db.commit()
