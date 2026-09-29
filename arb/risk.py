"""Position sizing & risk limits.

Two risk types matter for complete-set arbitrage:
  1. Execution / legging risk  – one leg fills, the other doesn't -> naked position.
     Bounded by capping the notional of any single leg at the remaining
     `max_unhedged_usd` budget (worst case = one full leg left naked).
  2. Settlement risk (negRisk baskets only) – capital locked until resolution,
     small chance the basket doesn't pay (dispute, augmented outcome, rule edge).
     Sized with fractional Kelly on a binary bet: win `edge`, lose the stake.

Plus portfolio-level guards: per-trade %, per-market %, lock-up %, cash buffer,
daily loss kill-switch, consecutive-leg-failure kill-switch.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from .models import HELD_KINDS, Opportunity


@dataclass
class PortfolioView:
    equity: float
    cash: float
    locked: float
    unhedged: float
    exposure_by_basket: Dict[str, float] = field(default_factory=dict)


class RiskManager:
    def __init__(self, risk_cfg: dict):
        c = risk_cfg
        self.max_trade_pct = float(c["max_trade_pct"])
        self.max_market_pct = float(c["max_market_pct"])
        self.max_locked_pct = float(c["max_locked_pct"])
        self.max_unhedged = float(c["max_unhedged_usd"])
        self.daily_loss_pct = float(c["daily_loss_limit_pct"])
        self.max_leg_fail = int(c["max_consecutive_leg_failures"])
        self.kelly_frac = float(c["kelly_fraction"])
        self.p_fail = float(c["basket_failure_prob"])
        self.cash_buffer = float(c["cash_buffer_pct"])
        self.cooldown_s = float(c.get("leg_failure_cooldown_min", 30)) * 60
        self._halt_until: Optional[float] = None

        self.halted: Optional[str] = None
        self.consecutive_leg_failures = 0
        self._day: Optional[str] = None
        self._day_start_equity: Optional[float] = None

    # ------------------------------------------------------------ kill switch
    def update(self, equity: float, now: Optional[float] = None) -> None:
        day = datetime.fromtimestamp(time.time() if now is None else now, tz=timezone.utc).strftime("%Y-%m-%d")
        if day != self._day:
            self._day, self._day_start_equity = day, equity
            if self.halted and self.halted.startswith("daily"):
                self.halted = None  # new day, reset daily stop
        t = time.time() if now is None else now
        if self._halt_until and t >= self._halt_until and self.halted and "leg failures" in self.halted:
            self.halted, self._halt_until, self.consecutive_leg_failures = None, None, 0
        if self._day_start_equity and equity < self._day_start_equity * (1 - self.daily_loss_pct):
            self.halted = f"daily loss limit hit ({equity:.2f} < {self._day_start_equity:.2f})"

    def record_execution(self, leg_failure: bool, now: Optional[float] = None) -> None:
        self.consecutive_leg_failures = self.consecutive_leg_failures + 1 if leg_failure else 0
        if self.consecutive_leg_failures >= self.max_leg_fail and not self.halted:
            t = time.time() if now is None else now
            self._halt_until = t + self.cooldown_s
            self.halted = (f"{self.consecutive_leg_failures} consecutive leg failures – "
                           f"cooldown {self.cooldown_s / 60:.0f} min")

    # ------------------------------------------------------------ sizing
    def kelly_capital(self, opp: Opportunity, equity: float) -> float:
        """Fractional Kelly for a basket: win b = net/capital with p=1-p_fail, lose stake with p_fail."""
        if opp.capital_usd <= 0:
            return 0.0
        b = opp.net_profit_usd / opp.capital_usd
        if b <= 0:
            return 0.0
        p, q = 1 - self.p_fail, self.p_fail
        f_star = (p * b - q) / b
        return max(0.0, f_star * self.kelly_frac * equity)

    def size(self, opp: Opportunity, pf: PortfolioView) -> Tuple[float, str]:
        """Returns (max_qty, reason). max_qty == 0 -> rejected."""
        if self.halted:
            return 0.0, f"halted: {self.halted}"
        if pf.unhedged >= self.max_unhedged:
            return 0.0, "unhedged exposure limit reached"

        cap_per_set = opp.capital_usd / opp.qty
        caps = {
            "trade_pct": self.max_trade_pct * pf.equity,
            "cash": pf.cash - self.cash_buffer * pf.equity,
            "market_pct": self.max_market_pct * pf.equity - pf.exposure_by_basket.get(opp.basket.basket_id, 0.0),
        }
        if opp.basket.kind in HELD_KINDS:
            caps["locked_pct"] = self.max_locked_pct * pf.equity - pf.locked
            caps["kelly"] = self.kelly_capital(opp, pf.equity)

        # legging cap: biggest single leg must fit into remaining unhedged budget
        max_leg_px = max(l.avg_price for l in opp.legs) or 1e-9
        leg_cap_qty = (self.max_unhedged - pf.unhedged) / max_leg_px

        binding = min(caps, key=caps.get)
        cap_capital = caps[binding]
        qty = min(opp.qty, cap_capital / cap_per_set, leg_cap_qty)
        if leg_cap_qty < min(opp.qty, cap_capital / cap_per_set):
            binding = "leg_unhedged"
        if qty <= 0:
            return 0.0, f"no capacity ({binding})"
        reason = "full size" if qty >= opp.qty - 1e-9 else f"sized down by {binding}"
        return qty, reason
