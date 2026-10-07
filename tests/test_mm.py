import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arb.client import FeeResolver
from arb.engine import SimClock
from arb.mm import MarketMaker, q_min, reward_share, rewards_of, score
from arb.models import Level, OrderBook
from arb.stream import BookStore

NOW = 1_790_000_000.0
END = "2026-12-31T00:00:00Z"


def _m(cid, q, yes_price, rate=50, end=END, **kw):
    return dict(conditionId=cid, question=q, clobTokenIds=json.dumps([f"{cid}Y", f"{cid}N"]),
                outcomePrices=json.dumps([str(yes_price), str(round(1 - yes_price, 3))]), endDate=end,
                enableOrderBook=True, clobRewards=[{"rewardsDailyRate": rate}], rewardsMaxSpread=4,
                rewardsMinSize=20, **kw)


class Client:
    def __init__(self, markets, res=None):
        self.markets, self.res = markets, res or {}
        self.fees = FeeResolver({"default_rate": 0.0})

    def paged(self, path, params, max_items=1000):
        return self.markets

    def books(self, tids):
        return {}

    def token_resolution(self, t):
        return self.res.get(t)


def _cfg(tmp_path, **sc):
    return {"storage": {"db_path": str(tmp_path / "polyarb.sqlite")},
            "scenarios": {"mm_rewards": dict(dict(strategy="mm", capital_usd=1000, quote_usd=20, latency_s=1,
                                                  max_markets=5, equity_every_s=0), **sc)}}


def _book(store, cid, bid, ask, size=200):
    store.snapshot(f"{cid}Y", [{"price": bid, "size": size}], [{"price": ask, "size": size}], NOW)
    store.snapshot(f"{cid}N", [{"price": round(1 - ask, 3), "size": size}], [{"price": round(1 - bid, 3), "size": size}], NOW)


def test_reward_scoring():
    assert abs(score([(0.48, 100)], 0.50, 0.04, True) - 25) < 1e-9
    assert score([(0.45, 100)], 0.50, 0.04, True) == 0.0  # outside the qualifying spread
    assert q_min(100, 0, 0.5) == 100 / 3 and q_min(100, 0, 0.95) == 0  # one-sided quotes count a third
    yes = OrderBook("Y", [Level(0.49, 100)], [Level(0.51, 100)])
    no = OrderBook("N", [Level(0.49, 100)], [Level(0.51, 100)])
    s = reward_share(yes, no, dict(price=0.49, size=100), dict(price=0.49, size=100), 0.50, 0.04, 20)
    assert 0.3 < s < 0.4  # same distance and size as the book's own two sides on each side: one third
    assert rewards_of({"clobRewards": [{"rewardsDailyRate": "10"}, {"rewardsDailyRate": 5}], "rewardsMaxSpread": 3.5,
                       "rewardsMinSize": "50"}) == (15.0, 0.035, 50.0)


def test_selection_skips_sport_short_extreme_and_unrewarded(tmp_path):
    ms = [_m("a", "Will the Fed cut rates in December?", 0.40),
          _m("b", "Lakers vs. Celtics", 0.50, gameStartTime="x"),            # sport: prices jump on news
          _m("c", "Will X happen by Friday?", 0.40, end="2026-09-22T00:00:00Z"),  # ends too soon
          _m("d", "Will Y happen?", 0.97),                                      # too extreme
          _m("e", "Will Z happen?", 0.40, rate=0)]                              # no rewards
    mm = MarketMaker("mm_rewards", _cfg(tmp_path), Client(ms), SimClock(NOW), books=BookStore(), pool=None)
    mm.select(NOW)
    assert list(mm.st.markets) == ["a"]
    assert mm.scan["Kategorie ausgelassen"] == 1 and mm.scan["endet zu bald"] == 1


def test_quotes_fill_strictly_merge_and_earn_rewards(tmp_path):
    store = BookStore()
    mm = MarketMaker("mm_rewards", _cfg(tmp_path), Client([_m("a", "Will the Fed cut rates?", 0.50)]),
                     SimClock(NOW), books=store, pool=None)
    _book(store, "a", 0.48, 0.52)
    mm.step()
    mk = mm.st.markets["a"]
    # mid 0.50, qualifying spread 4 ct, half of it -> bids at 0.48 on YES and on NO (YES ask 0.52)
    assert mk.bid_yes["price"] == 0.48 and mk.bid_no["price"] == 0.48
    assert mk.bid_yes["size"] == 41  # 20 $ / 0.48, at least the 20-share reward minimum
    # a trade AT our price: the queue may be in front – no fill
    store.trades.append(("aY", 0.48, 10, NOW + 5))
    mm.clock.sleep(10)
    mm.step()
    assert mk.qty_yes == 0
    # a trade below our YES bid fills it; a YES trade above 1 - NO bid fills the NO bid
    store.trades.append(("aY", 0.47, 10, NOW + 15))
    store.trades.append(("aY", 0.53, 30, NOW + 16))
    mm.clock.sleep(10)
    mm.step()
    assert mm.st.fills == 2
    # 10 YES + 30 NO bought at 0.48 each: 10 pairs merged into $1 -> 10 * (1 - 0.96) spread earned
    assert abs(mm.st.realized - 0.4) < 1e-9 and abs(mk.qty_no - 20) < 1e-9 and mk.qty_yes == 0
    kinds = [r[0] for r in mm.store.db.execute("SELECT kind FROM settlements")]
    assert "merge" in kinds
    # rewards accrue while quoting, booked into cash with the equity row
    assert mm.st.rewards > 0
    assert mm.store.db.execute("SELECT COUNT(*) FROM equity").fetchone()[0] >= 2
    # state survives a restart
    mm.st.save(mm.state_path)
    again = MarketMaker("mm_rewards", _cfg(tmp_path), Client([]), SimClock(NOW), books=BookStore(), pool=None)
    assert abs(again.st.markets["a"].qty_no - 20) < 1e-9 and again.st.fills == 2


def test_inventory_cap_stops_buying_the_long_side_and_resolution_pays(tmp_path):
    store = BookStore()
    mm = MarketMaker("mm_rewards", _cfg(tmp_path, max_inventory_usd=5),
                     Client([_m("a", "Will the Fed cut rates?", 0.50)], res={"aY": 1.0}), SimClock(NOW),
                     books=store, pool=None)
    _book(store, "a", 0.48, 0.52)
    mm.step()
    store.trades.append(("aY", 0.47, 41, NOW + 5))  # our whole YES bid is filled: 41 YES held
    mm.clock.sleep(10)
    mm.step()
    mk = mm.st.markets["a"]
    assert mk.qty_yes == 41 and mk.bid_yes is None and mk.bid_no is not None  # only the other side is quoted
    cash = mm.st.cash
    mk.end_ts = NOW  # market ended, resolved YES
    mm._last["settle"] = -1e18
    mm.clock.sleep(10)
    mm.step()
    assert "a" not in mm.st.markets and abs(mm.st.cash - cash - 41) < 0.01  # + a few estimated reward cents
    assert abs(mm.st.realized - 41 * (1 - 0.48)) < 1e-6


def test_dashboard_builds_with_merges_and_rewards(tmp_path):
    """A reward settlement belongs to no execution: the trades CSV must still build (broke the dashboard)."""
    from dashboard import build, trades_csv
    store = BookStore()
    mm = MarketMaker("mm_rewards", _cfg(tmp_path), Client([_m("a", "Will the Fed cut rates?", 0.50)]),
                     SimClock(NOW), books=store, pool=None)
    _book(store, "a", 0.48, 0.52)
    mm.step()
    store.trades.extend([("aY", 0.47, 10, NOW + 5), ("aY", 0.53, 30, NOW + 6)])
    for _ in range(2):
        mm.clock.sleep(400)
        mm.step()
    db = str(tmp_path / "scenario-mm_rewards.sqlite")
    lines = trades_csv(db).splitlines()
    assert any("Liquidity Rewards" in l for l in lines) and any("zusammengelegt" in l for l in lines)
    build(db, str(tmp_path / "www" / "index.html"), 1000, title="MM", kind="mm")


def test_new_markets_ranked_by_expected_share(tmp_path):
    """mm_neu: only recently started markets; a smaller pool with an empty book beats a big crowded one."""
    started = "2026-09-20T12:00:00Z"   # a day before NOW
    ms = [_m("big", "Will A happen?", 0.50, rate=100, startDate=started),
          _m("small", "Will B happen?", 0.50, rate=30, startDate=started),
          _m("old", "Will C happen?", 0.50, rate=500, startDate="2026-08-01T00:00:00Z")]

    class C(Client):
        def books(self, tids):
            crowd = [Level(0.49, 5000)]  # big pool: lots of size already resting near the mid
            out = {"bigY": OrderBook("bigY", crowd, [Level(0.51, 5000)]), "bigN": OrderBook("bigN", crowd, [Level(0.51, 5000)]),
                   "smallY": OrderBook("smallY", [Level(0.45, 10)], [Level(0.55, 10)]),
                   "smallN": OrderBook("smallN", [Level(0.45, 10)], [Level(0.55, 10)])}
            return {t: out[t] for t in tids if t in out}

    mm = MarketMaker("mm_rewards", _cfg(tmp_path, max_age_days=3, rank="share", max_markets=1), C(ms), SimClock(NOW),
                     books=BookStore(), pool=None)
    mm.select(NOW)
    assert list(mm.st.markets) == ["small"]
    assert mm.scan["älter als max_age_days"] == 1 and mm.scan["erwarteter Reward $/Tag"] > 10
