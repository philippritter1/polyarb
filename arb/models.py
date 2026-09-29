"""Core data structures."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


# baskets held until the markets resolve (no merge/convert): negRisk YES sets and logic ladders
HELD_KINDS = ("negrisk", "ladder")


@dataclass
class Level:
    price: float
    size: float


@dataclass
class OrderBook:
    token_id: str
    bids: List[Level]  # sorted best (highest) first
    asks: List[Level]  # sorted best (lowest) first
    min_order_size: float = 5.0
    tick_size: float = 0.01
    ts: float = 0.0

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.bids and self.asks:
            return (self.bids[0].price + self.asks[0].price) / 2
        return self.best_bid or self.best_ask


@dataclass
class FeeSpec:
    rate: float = 0.0
    exponent: float = 1.0

    def per_share(self, price: float) -> float:
        """Polymarket taker fee per share in USDC: rate * (p*(1-p))^exponent."""
        if self.rate <= 0:
            return 0.0
        return self.rate * (price * (1.0 - price)) ** self.exponent


@dataclass
class Basket:
    """A set of tokens that together always pay out `payout` USDC.

    - binary market: [YES, NO]              -> pays 1, can be MERGED instantly (no lock-up)
    - negRisk event: [YES_1, ..., YES_n]    -> pays 1, held until resolution (lock-up)
    - negRisk NO set: [NO_1, ..., NO_n]     -> pays n-1, CONVERTED instantly via the
      NegRiskAdapter (n NO -> n-1 USDC), kind "negrisk_no"
    - logic ladder: [YES broad, NO narrow]  -> pays 1 or 2 (narrow implies broad), held, kind "ladder"
    """
    basket_id: str            # conditionId (binary) or event id (negRisk)
    kind: str                 # "binary" | "negrisk" | "negrisk_no" | "ladder"
    title: str
    token_ids: List[str]
    labels: List[str]
    fees: List[FeeSpec]
    end_ts: Optional[float] = None   # unix ts of expected resolution
    category: str = ""
    payout: float = 1.0              # USDC one complete set is worth
    delay_s: float = 0.0             # Polymarket holds marketable orders this long (live sports)


@dataclass
class Leg:
    token_id: str
    label: str
    side: str                 # "BUY" | "SELL"
    limit_price: float        # worst price we accept
    qty: float
    avg_price: float
    fee_usd: float


@dataclass
class Opportunity:
    basket: Basket
    direction: str            # "buy_all" | "sell_all"
    qty: float                # number of complete sets
    legs: List[Leg]
    gross_usd: float          # cash out (buy) or cash in (sell) excl. fees
    fees_usd: float
    net_profit_usd: float
    capital_usd: float        # capital that must be put up
    edge_bps: float           # net_profit / capital * 1e4
    lockup_days: float = 0.0
    annualized: Optional[float] = None
    detected_ts: float = 0.0

    @property
    def strategy(self) -> str:
        return f"{self.basket.kind}_{self.direction}"


@dataclass
class Fill:
    token_id: str
    side: str
    qty: float
    avg_price: float
    fee_usd: float


@dataclass
class ExecutionResult:
    opp: Opportunity
    status: str               # "filled" | "partial" | "missed" | "rejected"
    matched_qty: float
    fills: List[Fill] = field(default_factory=list)
    unwind_fills: List[Fill] = field(default_factory=list)
    realized_pnl: float = 0.0     # booked now (binary merge / unwind losses)
    locked_capital: float = 0.0   # for negRisk baskets held to resolution
    expected_payout: float = 0.0
    residual_exposure_usd: float = 0.0
    note: str = ""
    # liquidity our simulated orders took: (token_id, "bids"|"asks", price, size)
    consumed: List[tuple] = field(default_factory=list)
