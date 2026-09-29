import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arb.models import Basket, FeeSpec, Level, OrderBook
from arb.paper import PaperBroker
from arb.risk import PortfolioView, RiskManager
from arb.scanner import Scanner, build_opportunity, walk_depth

NOFEE = FeeSpec(0.0)
RISK = dict(max_trade_pct=0.10, max_market_pct=0.20, max_locked_pct=0.60, max_unhedged_usd=75,
            daily_loss_limit_pct=0.02, max_consecutive_leg_failures=5, kelly_fraction=0.5,
            basket_failure_prob=0.01, cash_buffer_pct=0.10)


def ob(tid, bids, asks):
    return OrderBook(tid, [Level(p, s) for p, s in bids], [Level(p, s) for p, s in asks])


def binary(fee=NOFEE):
    return Basket("m1", "binary", "Test", ["Y", "N"], ["Yes", "No"], [fee, fee])


def test_fee_formula():
    f = FeeSpec(0.04)
    assert math.isclose(f.per_share(0.5), 0.01)
    assert math.isclose(f.per_share(0.9), 0.04 * 0.09)
    assert FeeSpec(0.0).per_share(0.5) == 0


def test_walk_depth_stops_when_marginal_edge_gone():
    books = {"Y": ob("Y", [], [(0.45, 100), (0.50, 100)]),
             "N": ob("N", [], [(0.50, 60), (0.53, 100)])}
    w = walk_depth(binary(), books, "buy_all", 0.999)
    # 60 @ .95, 40 @ .98 (.45+.53), then .50+.53=1.03 -> stop
    assert math.isclose(w["qty"], 100)
    assert math.isclose(w["notional"][0], 45)
    assert math.isclose(w["notional"][1], 60 * 0.50 + 40 * 0.53)


def test_no_arb_when_fees_eat_edge():
    fee = FeeSpec(0.04)  # at p=.49/.49 fee ~ 0.02 per set
    books = {"Y": ob("Y", [], [(0.49, 100)]), "N": ob("N", [], [(0.50, 100)])}
    assert build_opportunity(binary(), books, "buy_all", 50) is not None
    assert build_opportunity(binary(fee), books, "buy_all", 50) is None


def test_buy_opportunity_math():
    books = {"Y": ob("Y", [], [(0.40, 50)]), "N": ob("N", [], [(0.55, 80)])}
    o = build_opportunity(binary(), books, "buy_all", 50)
    assert o.qty == 50
    assert math.isclose(o.capital_usd, 50 * 0.95)
    assert math.isclose(o.net_profit_usd, 50 * 0.05)


def test_sell_opportunity():
    books = {"Y": ob("Y", [(0.52, 30)], []), "N": ob("N", [(0.51, 100)], [])}
    o = build_opportunity(binary(), books, "sell_all", 50)
    assert o.qty == 30 and math.isclose(o.net_profit_usd, 30 * 0.03)


def test_scanner_filters_min_profit():
    sc = Scanner(dict(min_edge_bps=50, min_profit_usd=5, min_annualized_return=0), dict(merge_gas_usd=0), {})
    books = {"Y": ob("Y", [], [(0.40, 50)]), "N": ob("N", [], [(0.55, 80)])}
    assert sc.scan([binary()], books) == []  # profit 2.5 < 5


def test_risk_caps_trade_and_legging():
    rm = RiskManager(RISK)
    books = {"Y": ob("Y", [], [(0.40, 5000)]), "N": ob("N", [], [(0.55, 5000)])}
    o = build_opportunity(binary(), books, "buy_all", 50)
    qty, reason = rm.size(o, PortfolioView(2500, 2500, 0, 0))
    # trade cap 250$ -> 263 sets; legging cap 75/0.55 -> 136 sets -> legging binds
    assert math.isclose(qty, 75 / 0.55)
    assert "leg_unhedged" in reason


def test_kill_switch():
    rm = RiskManager(RISK)
    rm.update(2500, now=0)
    rm.update(2440, now=10)
    assert rm.halted


def test_paper_full_fill_binary_merge():
    br = PaperBroker(dict(depth_haircut=1.0, unwind_on_leg_failure=True), 0.0, 2500)
    books = {"Y": ob("Y", [(0.38, 500)], [(0.40, 50)]), "N": ob("N", [(0.53, 500)], [(0.55, 80)])}
    o = build_opportunity(binary(), books, "buy_all", 50)
    r = br.execute(o, lambda t: books, 0)
    assert r.status == "filled"
    assert math.isclose(r.realized_pnl, 2.5)
    assert math.isclose(br.pf.cash, 2502.5)


def test_paper_leg_failure_unwinds_parallel():
    br = PaperBroker(dict(depth_haircut=1.0, unwind_on_leg_failure=True, leg_mode="parallel"), 0.0, 2500)
    detect = {"Y": ob("Y", [(0.38, 500)], [(0.40, 50)]), "N": ob("N", [(0.53, 500)], [(0.55, 80)])}
    o = build_opportunity(binary(), detect, "buy_all", 50)
    # after latency the cheap NO ask is gone -> only YES fills, then gets dumped at 0.38
    fresh = {"Y": ob("Y", [(0.38, 500)], [(0.40, 50)]), "N": ob("N", [(0.53, 500)], [(0.58, 80)])}
    r = br.execute(o, lambda t: fresh, 0)
    assert r.status == "missed" and r.matched_qty == 0
    assert math.isclose(r.realized_pnl, 50 * (0.38 - 0.40))
    assert r.unwind_fills


def test_paper_negrisk_lock_and_settle():
    f = NOFEE
    b = Basket("event:1", "negrisk", "E", ["A", "B", "C"], ["a", "b", "c"], [f, f, f], end_ts=86400 * 10)
    books = {"A": ob("A", [], [(0.30, 100)]), "B": ob("B", [], [(0.30, 100)]), "C": ob("C", [], [(0.35, 100)])}
    o = build_opportunity(b, books, "buy_all", 50, now=0)
    assert o.lockup_days == 10 and o.annualized > 1
    br = PaperBroker(dict(depth_haircut=1.0), 0.0, 2500)
    r = br.execute(o, lambda t: books, 0)
    assert math.isclose(r.locked_capital, 95) and math.isclose(br.pf.equity, 2500)
    pnl = br.settle_basket(br.pf.locked[0], True)
    assert math.isclose(pnl, 5) and math.isclose(br.pf.cash, 2505)


def test_paper_sell_all_partial():
    br = PaperBroker(dict(depth_haircut=1.0, unwind_on_leg_failure=True, leg_mode="parallel"), 0.0, 2500)
    books = {"Y": ob("Y", [(0.52, 30), (0.45, 500)], []), "N": ob("N", [(0.51, 100)], [])}
    o = build_opportunity(binary(), books, "sell_all", 50)
    fresh = {"Y": ob("Y", [(0.52, 10), (0.45, 500)], []), "N": ob("N", [(0.51, 100)], [])}
    r = br.execute(o, lambda t: fresh, 0)
    # split 30; sold 10 YES@.52, 30 NO@.51; merge 0; dump 20 YES @ .45
    assert math.isclose(r.matched_qty, 10)
    assert math.isclose(br.pf.cash, 2500 - 30 + 10 * .52 + 30 * .51 + 20 * .45)


def test_sequential_first_leg_miss_costs_nothing():
    br = PaperBroker(dict(depth_haircut=1.0, unwind_on_leg_failure=True, leg_mode="sequential"), 0.0, 2500)
    detect = {"Y": ob("Y", [(0.38, 500)], [(0.40, 50)]), "N": ob("N", [(0.53, 500)], [(0.55, 80)])}
    o = build_opportunity(binary(), detect, "buy_all", 50)
    fresh = {"Y": ob("Y", [(0.38, 500)], [(0.40, 50)]), "N": ob("N", [(0.53, 500)], [(0.58, 80)])}
    r = br.execute(o, lambda t: fresh, 0)
    assert r.status == "missed" and r.realized_pnl == 0 and br.pf.cash == 2500


def test_sequential_scales_second_leg():
    br = PaperBroker(dict(depth_haircut=1.0, leg_mode="sequential"), 0.0, 2500)
    detect = {"Y": ob("Y", [], [(0.40, 50)]), "N": ob("N", [], [(0.55, 80)])}
    o = build_opportunity(binary(), detect, "buy_all", 50)
    fresh = {"Y": ob("Y", [], [(0.40, 20)]), "N": ob("N", [], [(0.55, 80)])}
    r = br.execute(o, lambda t: fresh, 0)
    assert r.status == "partial" and math.isclose(r.matched_qty, 20) and not r.unwind_fills
    assert math.isclose(r.realized_pnl, 20 * 0.05)


def test_leg_failure_cooldown_resets():
    rm = RiskManager(dict(RISK, leg_failure_cooldown_min=1))
    rm.update(2500, now=0)
    for _ in range(5):
        rm.record_execution(True, now=0)
    assert rm.halted
    rm.update(2500, now=61)
    assert rm.halted is None


def test_limit_slack_keeps_min_edge():
    books = {"Y": ob("Y", [], [(0.40, 50)]), "N": ob("N", [], [(0.55, 80)])}
    o = build_opportunity(binary(), books, "buy_all", 50)
    # room = 1/1.005 - 0.95 = 0.045 -> 80% / 2 legs -> 0.018 -> floored to 1 tick
    assert [l.limit_price for l in o.legs] == [0.41, 0.56]
    assert sum(l.limit_price for l in o.legs) <= 1 / 1.005


# ---------------------------------------------------------------- negRisk NO sets
def no_basket(fee=NOFEE):
    return Basket("event:1:no", "negrisk_no", "E", ["nA", "nB", "nC"], ["a", "b", "c"], [fee] * 3,
                  end_ts=86400 * 10, payout=2.0)


def test_negrisk_no_opportunity_math():
    # 3 outcomes -> exactly 2 NO pay out; asks sum 1.95 < 2
    books = {"nA": ob("nA", [], [(0.60, 100)]), "nB": ob("nB", [], [(0.65, 100)]),
             "nC": ob("nC", [], [(0.70, 100)])}
    o = build_opportunity(no_basket(), books, "buy_all", 50, now=0)
    assert o.qty == 100 and o.lockup_days == 0 and o.annualized is None
    assert math.isclose(o.capital_usd, 195) and math.isclose(o.net_profit_usd, 5)
    # no edge when the NO asks add up to the payout
    books["nC"] = ob("nC", [], [(0.75, 100)])
    assert build_opportunity(no_basket(), books, "buy_all", 50, now=0) is None


def test_negrisk_no_converts_instantly():
    books = {"nA": ob("nA", [], [(0.60, 100)]), "nB": ob("nB", [], [(0.65, 100)]),
             "nC": ob("nC", [], [(0.70, 100)])}
    o = build_opportunity(no_basket(), books, "buy_all", 50, now=0)
    br = PaperBroker(dict(depth_haircut=1.0), 0.0, 2500)
    r = br.execute(o, lambda t: books, 0)
    assert r.status == "filled" and r.locked_capital == 0 and not br.pf.locked
    assert math.isclose(r.realized_pnl, 5) and math.isclose(br.pf.cash, 2505)


# ---------------------------------------------------------------- phantom-edge guards
def test_crossed_books_are_skipped():
    sc = Scanner(dict(min_edge_bps=50, min_profit_usd=0, min_annualized_return=0), dict(merge_gas_usd=0), {})
    ok = {"Y": ob("Y", [(0.38, 50)], [(0.40, 50)]), "N": ob("N", [(0.50, 50)], [(0.55, 80)])}
    assert len(sc.scan([binary()], ok)) == 1
    # YES bid 0.42 above its ask 0.40: stale copy -> no trade
    crossed = {"Y": ob("Y", [(0.42, 50)], [(0.40, 50)]), "N": ob("N", [(0.50, 50)], [(0.55, 80)])}
    assert sc.scan([binary()], crossed) == [] and sc.stats["crossed"] == 1


def test_consumed_liquidity_not_filled_twice():
    from arb.paper import ConsumedLiquidity
    books = {"Y": ob("Y", [(0.38, 500)], [(0.40, 50)]), "N": ob("N", [(0.53, 500)], [(0.55, 80)])}
    br = PaperBroker(dict(depth_haircut=0.5), 0.0, 2500)
    led = ConsumedLiquidity(ttl_s=60)
    o = build_opportunity(binary(), books, "buy_all", 50)
    r = br.execute(o, lambda t: led.apply(books, 0), 0)
    assert math.isclose(r.matched_qty, 25)
    led.add(r.consumed, 0)
    # the live feed still shows the same book, but the YES level we hit is gone for us
    after = led.apply(books, 1)
    assert after["Y"].asks == [] and books["Y"].asks[0].size == 50
    assert math.isclose(after["N"].asks[0].size, 80 - 50)
    assert build_opportunity(binary(), after, "buy_all", 50) is None
    # once the TTL passes (makers re-quote) the level counts again
    assert led.apply(books, 61)["Y"].asks[0].size == 50


class _StaticClient:
    """Returns the same order book on every call – like a live feed that never sees our paper orders."""

    def __init__(self, baskets, books):
        self.b, self.bk = baskets, books

    def binary_baskets(self, *a, **k):
        return self.b

    def negrisk_baskets(self, *a, **k):
        return []

    def books(self, tids):
        import copy
        return {t: copy.deepcopy(self.bk[t]) for t in tids if t in self.bk}

    def basket_resolution(self, *a):
        return None

    def token_resolution(self, *a):
        return None


def _cfg(tmp_path):
    from arb.config import load_config
    cfg = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
    cfg["storage"]["db_path"] = str(tmp_path / "t.sqlite")
    return cfg


def test_engine_does_not_refill_same_orders(tmp_path):
    from arb.engine import Engine, SimClock
    books = {"Y": ob("Y", [(0.38, 500)], [(0.40, 200)]), "N": ob("N", [(0.53, 500)], [(0.55, 200)])}
    eng = Engine(_cfg(tmp_path), _StaticClient([binary()], books), SimClock(1_000_000))
    for _ in range(3):
        eng.step()
    rows = eng.store.db.execute("SELECT status FROM executions").fetchall()
    assert len(rows) == 1 and rows[0][0] in ("filled", "partial")


def test_stream_engine_confirms_with_rest(tmp_path):
    from arb.engine import StreamEngine
    rest = {"Y": ob("Y", [(0.38, 500)], [(0.45, 200)]), "N": ob("N", [(0.53, 500)], [(0.58, 200)])}
    eng = StreamEngine(_cfg(tmp_path), _StaticClient([binary()], rest))
    got = eng._exec_books(["Y", "N"])
    assert got["Y"].asks[0].price == 0.45  # REST snapshot, not the (empty) local store
    eng.confirm_rest = False
    assert eng._exec_books(["Y", "N"]) == {}


def test_trades_csv_export(tmp_path):
    from arb.engine import Engine, SimClock
    from dashboard import build
    books = {"Y": ob("Y", [(0.38, 500)], [(0.40, 200)]), "N": ob("N", [(0.53, 500)], [(0.55, 200)])}
    cfg = _cfg(tmp_path)
    eng = Engine(cfg, _StaticClient([binary()], books), SimClock(1_000_000))
    eng.step()
    build(cfg["storage"]["db_path"], str(tmp_path / "index.html"), 2500)
    raw = (tmp_path / "trades.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM so Excel reads UTF-8
    lines = raw.decode("utf-8-sig").splitlines()
    assert lines[0].startswith("Zeit;Strategie;Strategie-Code;Markt")
    row = lines[1].split(";")
    assert row[1] == "Binär: YES+NO kaufen" and row[2] == "binary_buy_all" and row[5] in ("voll", "teilweise")
    assert "," in row[8] and "." not in row[8]  # decimal comma
    assert 'href="trades.csv"' in (tmp_path / "index.html").read_text(encoding="utf-8")
