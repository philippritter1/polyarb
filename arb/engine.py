"""Main loop: universe -> books -> scan -> size -> (latency) -> re-fetch -> paper fill -> log."""
from __future__ import annotations

import logging
import os
import time
from typing import Dict, List, Optional

from .models import Basket, OrderBook
from .paper import PaperBroker
from .risk import RiskManager
from .scanner import Scanner, build_opportunity
from .storage import Store

log = logging.getLogger("polyarb")


class RealClock:
    def now(self) -> float:
        return time.time()

    def sleep(self, s: float) -> None:
        time.sleep(s)


class SimClock:
    def __init__(self, start: float):
        self.t = start

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class Engine:
    def __init__(self, cfg, client, clock=None, store: Optional[Store] = None,
                 config_path: Optional[str] = None, persist: bool = False):
        self.cfg = cfg
        self.config_path = config_path
        self._cfg_mtime = os.path.getmtime(config_path) if config_path else None
        self.client = client
        self.clock = clock or RealClock()
        self.scanner = Scanner(cfg["scanner"], cfg["fees"], cfg["universe"])
        self.risk = RiskManager(cfg["risk"])
        self.broker = PaperBroker(cfg["execution"], float(cfg["fees"].get("merge_gas_usd", 0)),
                                  float(cfg["portfolio"]["starting_capital_usd"]))
        self.store = store or Store(cfg["storage"]["db_path"])
        self.latency_s = float(cfg["execution"]["latency_ms"]) / 1000
        self.leg_gap_s = float(cfg["execution"].get("leg_gap_ms", 150)) / 1000
        self.baskets: List[Basket] = []
        self._universe_ts = 0.0
        self.max_trades_per_scan = 5
        self._cooldown: Dict[str, float] = {}
        self.state_path = None
        if persist:
            self.state_path = os.path.join(os.path.dirname(cfg["storage"]["db_path"]) or ".", "portfolio.json")
            if self.broker.load(self.state_path):
                log.info("portfolio restored: cash %.2f, %d baskets, %d residuals", self.broker.pf.cash,
                         len(self.broker.pf.locked), len(self.broker.pf.residuals))

    # ------------------------------------------------------------ live config reload
    def maybe_reload_config(self) -> None:
        if not self.config_path:
            return
        try:
            m = os.path.getmtime(self.config_path)
            if m == self._cfg_mtime:
                return
            from .config import load_config
            new = load_config(self.config_path)
        except Exception:
            log.exception("config reload failed – keeping old config")
            return
        self._cfg_mtime = m
        old_risk = self.risk
        self.cfg = new
        self.scanner = Scanner(new["scanner"], new["fees"], new["universe"])
        self.risk = RiskManager(new["risk"])
        for k in ("halted", "consecutive_leg_failures", "_day", "_day_start_equity", "_halt_until"):
            setattr(self.risk, k, getattr(old_risk, k))
        ex = new["execution"]
        self.broker.haircut = float(ex.get("depth_haircut", 1.0))
        self.broker.unwind = bool(ex.get("unwind_on_leg_failure", True))
        self.broker.sequential = ex.get("leg_mode", "sequential") == "sequential"
        self.latency_s = float(ex["latency_ms"]) / 1000
        self.leg_gap_s = float(ex.get("leg_gap_ms", 150)) / 1000
        self._universe_ts = 0.0  # refresh universe with new filters
        log.info("config reloaded from %s", self.config_path)

    # ------------------------------------------------------------ universe
    def refresh_universe(self) -> None:
        u = self.cfg["universe"]
        baskets: List[Basket] = []
        if u.get("include_binary", True):
            baskets += self.client.binary_baskets(float(u["min_liquidity_usd"]), int(u["max_markets"]))
        if u.get("include_negrisk_baskets", False):
            baskets += self.client.negrisk_baskets(float(u["min_liquidity_usd"]))
        self.baskets = baskets
        self._universe_ts = self.clock.now()
        n_tok = sum(len(b.token_ids) for b in baskets)
        log.info("universe: %d baskets (%d binary, %d negRisk), %d tokens",
                 len(baskets), sum(b.kind == "binary" for b in baskets),
                 sum(b.kind == "negrisk" for b in baskets), n_tok)

    # ------------------------------------------------------------ one cycle
    def step(self) -> None:
        now = self.clock.now()
        if now - self._universe_ts > float(self.cfg["universe"]["refresh_minutes"]) * 60 or not self.baskets:
            self.refresh_universe()

        t0 = time.time()
        tokens = [t for b in self.baskets for t in b.token_ids] + list(self.broker.pf.residuals)
        books = self.client.books(tokens)
        now = self.clock.now()
        raw_before = self.scanner.stats["raw_signals"]
        opps = self.scanner.scan(self.baskets, books, now)

        best_buy = min((s for b in self.baskets if b.kind == "binary"
                        for s in [self.scanner.raw_sum(b, books, "buy_all")] if s is not None), default=None)
        best_sell = max((s for b in self.baskets if b.kind == "binary"
                         for s in [self.scanner.raw_sum(b, books, "sell_all")] if s is not None), default=None)

        self._process_opps(opps, books, now, lambda tids: self.client.books(tids))
        self._housekeeping(books, now, self.scanner.stats["raw_signals"] - raw_before, len(opps),
                           best_buy, best_sell, (time.time() - t0) * 1000)

    # ------------------------------------------------------------ shared pieces
    def _process_opps(self, opps, books, now, get_books) -> int:
        """Size, (paper-)execute and log a ranked list of opportunities. Returns #executions."""
        traded = 0
        for opp in opps:
            bid = opp.basket.basket_id
            if self._cooldown.get(bid, 0) > self.clock.now():
                continue
            if traded >= self.max_trades_per_scan:
                self.store.opportunity(opp, "skipped", "max trades per scan", 0.0)
                continue
            qty, reason = self.risk.size(opp, self.broker.pf.view())
            min_size = max(books[t].min_order_size for t in opp.basket.token_ids)
            if qty < min_size:
                self.store.opportunity(opp, "rejected",
                                       reason if qty <= 0 else f"below min size after sizing ({reason})", qty)
                self._cooldown[bid] = self.clock.now() + 60
                continue
            sized = opp if qty >= opp.qty else build_opportunity(
                opp.basket, books, opp.direction, self.scanner.min_edge_bps, self.scanner.gas,
                max_qty=qty, now=now)
            if not sized or sized.net_profit_usd < self.scanner.min_profit:
                self.store.opportunity(opp, "rejected", f"{reason}; too small after sizing", qty)
                self._cooldown[bid] = self.clock.now() + 60
                continue
            self.store.opportunity(opp, "accepted", reason, sized.qty)

            # --- execution: wait latency, look at the book again, fill against it
            self.clock.sleep(self.latency_s)
            calls = {"n": 0, "last": {}}

            def fetch(tids, _c=calls):
                if _c["n"] > 0:
                    self.clock.sleep(self.leg_gap_s)   # time between first and following legs
                _c["n"] += 1
                bk = get_books(tids)
                _c["last"].update(bk)
                return bk

            res = self.broker.execute(sized, fetch, self.clock.now())
            fresh = self.scanner.raw_sum(sized.basket, calls["last"], sized.direction)
            det = self.scanner.raw_sum(sized.basket, books, sized.direction)
            res.note += f" sum_detect={det:.4f}" if det is not None else ""
            res.note += f" sum_exec={fresh:.4f}" if fresh is not None else " sum_exec=n/a"
            leg_failure = bool(res.unwind_fills) or res.residual_exposure_usd > 0
            self.risk.record_execution(leg_failure, self.clock.now())
            self.store.execution(self.clock.now(), res, self.latency_s * 1000)
            if res.status == "missed":
                self._cooldown[bid] = self.clock.now() + 60
            traded += 1
            log.info("%-9s %-15s %-45.45s qty %.1f/%.1f exp %+.2f real %+.2f | %s",
                     res.status, sized.strategy, sized.basket.title, res.matched_qty, sized.qty,
                     sized.net_profit_usd, res.realized_pnl, res.note)
        return traded

    def _housekeeping(self, books, now, raw, n_opps, best_buy, best_sell, dur_ms) -> None:
        self._settle(books)
        for t, q, pnl in self.broker.unwind_residuals(books):
            self.store.settlement(self.clock.now(), t, "unwind", q, 0.0, pnl)
        self.broker.mark_residuals(books)
        pf = self.broker.pf
        self.risk.update(pf.equity, self.clock.now())
        self.store.scan(now, len(self.baskets), len(books), raw, n_opps, best_buy, best_sell, dur_ms)
        self.store.equity(self.clock.now(), pf.equity, pf.cash, pf.locked_value, pf.residual_value,
                          pf.realized_pnl, self.risk.halted)
        self.store.commit()
        if self.state_path:
            self.broker.save(self.state_path)

    def _settle(self, books: Dict[str, OrderBook]) -> None:
        now = self.clock.now()
        for b in list(self.broker.pf.locked):
            if b.end_ts and now < b.end_ts:
                continue
            paid = self.client.basket_resolution(b.token_ids)
            if paid is None:
                continue
            pnl = self.broker.settle_basket(b, paid)
            self.store.settlement(now, b.basket_id, "basket", b.qty, b.qty if paid else 0, pnl)
            log.info("settled basket %s paid=%s pnl %+.2f", b.title[:50], paid, pnl)
        for t in list(self.broker.pf.residuals):
            final = self.client.token_resolution(t)
            if final is None:
                continue
            r = self.broker.pf.residuals[t]
            pnl = self.broker.settle_residual(t, final)
            self.store.settlement(now, t, "residual", r.qty, r.qty * final, pnl)

    # ------------------------------------------------------------ run
    def _code_mtime(self) -> float:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        files = [os.path.join(base, "run.py")] + [os.path.join(base, "arb", f) for f in os.listdir(os.path.join(base, "arb")) if f.endswith(".py")]
        return max(os.path.getmtime(f) for f in files if os.path.exists(f))

    def run(self, max_steps: Optional[int] = None, duration_s: Optional[float] = None,
            exit_on_code_change: bool = False) -> None:
        start = self.clock.now()
        code_m = self._code_mtime() if exit_on_code_change else None
        fails = 0
        i = 0
        last_check = last_log = start
        while True:
            interval = float(self.cfg["scanner"]["scan_interval_s"])
            t = self.clock.now()
            if t - last_check >= 5:  # cheap file checks, but not every tick
                last_check = t
                if code_m is not None and self._code_mtime() != code_m:
                    log.info("code updated on disk – exiting so the launcher restarts with the new version")
                    return
                self.maybe_reload_config()
            try:
                self.step()
                fails = 0
            except Exception as e:  # keep the bot alive, back off and continue
                fails += 1
                wait = min(900, 30 * 2 ** (fails - 1))
                log.error("step failed (%s: %s) – %d in a row, pausing %ds", type(e).__name__, e, fails, wait)
                if fails == 1:
                    log.exception("details")
                self.clock.sleep(wait)
            i += 1
            if (max_steps and i >= max_steps) or (duration_s and self.clock.now() - start >= duration_s):
                break
            if self.clock.now() - last_log >= 600:
                last_log = self.clock.now()
                pf = self.broker.pf
                log.info("equity %.2f cash %.2f locked %.2f realized %+.2f halted=%s",
                         pf.equity, pf.cash, pf.locked_value, pf.realized_pnl, self.risk.halted)
            self.clock.sleep(max(0.0, interval - (self.clock.now() - t)))


class StreamEngine(Engine):
    """Event-driven engine: order books arrive via WebSocket, only touched baskets are re-checked.

    Paper execution waits `latency_ms` and then fills against the *live* store books at
    that moment – i.e. against whatever the market actually looked like after our delay.
    """

    def __init__(self, cfg, client, store_books=None, pool=None, **kw):
        super().__init__(cfg, client, **kw)
        from .stream import BookStore, StreamPool
        self.books_store = store_books or BookStore()
        from .stream import WS_URL
        scfg = cfg.get("stream") or {}
        self.pool = pool or StreamPool(self.books_store, chunk=int(scfg.get("assets_per_connection", 400)),
                                       url=scfg.get("ws_url") or WS_URL)
        self.token_to_baskets: Dict[str, List[Basket]] = {}
        self._stats_every = float(scfg.get("stats_interval_s", 10))
        self._last_stats = 0.0
        self._raw = 0
        self._n_opps = 0
        self._evals = 0

    def refresh_universe(self) -> None:
        super().refresh_universe()
        m: Dict[str, List[Basket]] = {}
        for b in self.baskets:
            for t in b.token_ids:
                m.setdefault(t, []).append(b)
        self.token_to_baskets = m
        # min order size / tick from REST metadata is not in WS books -> keep defaults unless known
        self.pool.set_assets(list(m) + list(self.broker.pf.residuals))

    def _ready(self, b: Basket) -> bool:
        return all(self.books_store.is_valid(t) for t in b.token_ids)

    def step(self) -> None:
        now = self.clock.now()
        if now - self._universe_ts > float(self.cfg["universe"]["refresh_minutes"]) * 60 or not self.baskets:
            self.refresh_universe()

        dirty = self.books_store.pop_dirty()
        touched: Dict[str, Basket] = {}
        for t in dirty:
            for b in self.token_to_baskets.get(t, ()):
                touched[b.basket_id] = b
        cand = [b for b in touched.values() if self._ready(b)]
        if cand:
            tokens = {t for b in cand for t in b.token_ids}
            books = self.books_store.books(tokens)
            before = self.scanner.stats["raw_signals"]
            opps = self.scanner.scan(cand, books, now)
            self._raw += self.scanner.stats["raw_signals"] - before
            self._n_opps += len(opps)
            self._evals += len(cand)
            if opps:
                self._process_opps(opps, books, now, lambda tids: self.books_store.books(tids))

        if now - self._last_stats >= self._stats_every:
            self._last_stats = now
            ready = [b for b in self.baskets if self._ready(b)]
            books = self.books_store.books({t for b in ready for t in b.token_ids} | set(self.broker.pf.residuals))
            best_buy = min((s for b in ready if b.kind == "binary"
                            for s in [self.scanner.raw_sum(b, books, "buy_all")] if s is not None), default=None)
            best_sell = max((s for b in ready if b.kind == "binary"
                             for s in [self.scanner.raw_sum(b, books, "sell_all")] if s is not None), default=None)
            log.debug("stats: %d/%d baskets live, %d evals, ws %d/%d", len(ready), len(self.baskets),
                      self._evals, self.pool.connected, len(self.pool.streams))
            if not ready and self.baskets and now - self._universe_ts > 120:
                log.warning("no live books after 2 min – websocket connected %d/%d", self.pool.connected,
                            len(self.pool.streams))
            # n_books column = baskets evaluated since last row (event-driven "scan" count)
            self._housekeeping(books, now, self._raw, self._n_opps, best_buy, best_sell, float(self._evals))
            self._raw = self._n_opps = self._evals = 0

    def maybe_reload_config(self) -> None:
        super().maybe_reload_config()
        self.cfg["scanner"] = dict(self.cfg["scanner"], scan_interval_s=0.05)  # event loop tick

    def run(self, max_steps=None, duration_s=None, exit_on_code_change=False) -> None:
        # same outer loop as polling engine, but ticking every 50 ms
        self.cfg["scanner"] = dict(self.cfg["scanner"], scan_interval_s=0.05)
        try:
            super().run(max_steps=max_steps, duration_s=duration_s, exit_on_code_change=exit_on_code_change)
        finally:
            self.pool.stop()
