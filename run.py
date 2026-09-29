#!/usr/bin/env python3
"""Polymarket arbitrage bot – CLI.

  python run.py scan                 # one live scan, prints opportunities + how close markets are
  python run.py paper --hours 24     # live paper trading against real order books
  python run.py mock  --hours 6      # offline simulation with a synthetic market (pipeline test)
  python run.py dashboard            # build dashboard.html from the SQLite log
"""
from __future__ import annotations

import argparse
import logging
import time

from arb.config import load_config


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["scan", "paper", "mock", "dashboard", "scenario"])
    ap.add_argument("name", nargs="?", help="scenario name (for `scenario`)")
    ap.add_argument("--all", action="store_true", help="dashboard: also build every enabled scenario tab")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--hours", type=float, default=None)
    ap.add_argument("--db", default=None, help="override storage.db_path")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="dashboard.html", help="dashboard output path")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    cfg = load_config(args.config)
    if args.db:
        cfg["storage"]["db_path"] = args.db

    if args.cmd == "dashboard":
        from dashboard import build, build_all
        if args.all:
            for out in build_all(cfg, args.out):
                print(f"wrote {out}")
            return
        out = build(cfg["storage"]["db_path"], args.out, cfg["portfolio"]["starting_capital_usd"])
        print(f"wrote {out}")
        return

    from arb.engine import Engine, RealClock, SimClock

    if args.cmd == "mock":
        from arb.mock import MockClient
        clock = SimClock(time.time())
        client = MockClient(clock, seed=args.seed)
        eng = Engine(cfg, client, clock)
        eng.run(duration_s=(args.hours or 6) * 3600)
        pf = eng.broker.pf
        print(f"\nMOCK done: equity {pf.equity:.2f}  realized {pf.realized_pnl:+.2f}  locked {pf.locked_value:.2f}")
        return

    from arb.client import PolymarketClient
    client = PolymarketClient(cfg["api"], cfg["fees"])

    if args.cmd == "scenario":
        from arb.scenarios import ScenarioEngine
        if not args.name or args.name not in (cfg.get("scenarios") or {}):
            ap.error(f"unknown scenario {args.name!r} – defined: {', '.join(cfg.get('scenarios') or {})}")
        ScenarioEngine(args.name, cfg, client).run(duration_s=args.hours * 3600 if args.hours else None,
                                                    exit_on_code_change=True)
        return

    if args.cmd == "scan":
        from arb.scanner import Scanner
        eng = Engine(cfg, client, RealClock())
        eng.refresh_universe()
        tokens = [t for b in eng.baskets for t in b.token_ids]
        books = client.books(tokens)
        sc = eng.scanner
        rows = []
        for b in eng.baskets:
            for d in sc.directions(b):
                s = sc.raw_sum(b, books, d)
                if s is not None:
                    # distance to the set payout: < 0 means an edge before fees
                    rows.append((s - b.payout if d == "buy_all" else 1 - s, s, d, b))
        rows.sort(key=lambda r: r[0])
        print(f"\n{len(books)} books loaded. Closest buy_all sets (sum of best asks / payout):")
        for _, s, d, b in [r for r in rows if r[2] == "buy_all"][:15]:
            print(f"  {s:.4f}/{b.payout:.0f}  {b.kind:10s} {b.title[:80]}")
        print("\nClosest sell_all sets (sum of best bids):")
        for _, s, d, b in [r for r in rows if r[2] == "sell_all"][:10]:
            print(f"  {s:.4f}  {b.kind:10s} {b.title[:80]}")
        opps = sc.scan(eng.baskets, books)
        print(f"\n{len(opps)} opportunities after fees & thresholds:")
        for o in opps[:20]:
            ann = f"{o.annualized:.0%}" if o.annualized is not None else "instant"
            print(f"  {o.strategy:19s} qty {o.qty:8.1f} cap ${o.capital_usd:8.2f} net ${o.net_profit_usd:7.2f} "
                  f"({o.edge_bps:5.0f} bps, {ann})  {o.basket.title[:60]}")
        return

    if args.cmd == "paper":
        try:
            from arb import netcheck
            netcheck.run()
        except ImportError:
            pass
        from arb.engine import StreamEngine
        cls = StreamEngine if cfg.get("data_source", "polling") == "websocket" else Engine
        logging.getLogger().info("data source: %s", cfg.get("data_source", "polling"))
        eng = cls(cfg, client, clock=RealClock(), config_path=args.config, persist=True)
        eng.run(duration_s=args.hours * 3600 if args.hours else None, exit_on_code_change=True)


if __name__ == "__main__":
    main()
