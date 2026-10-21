import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arb.client import FeeResolver
from arb.engine import SimClock
from arb.mm import MMMarket, MarketMaker, q_min, reward_share, rewards_of, score
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


def test_wind_down_pulls_bids_and_still_exits_inventory(tmp_path):
    store = BookStore()
    mm = MarketMaker("mm_rewards", _cfg(tmp_path, exit={"after_h": 0, "dump_after_h": 96}),
                     Client([_m("a", "Will the Fed cut rates?", 0.50)]), SimClock(NOW), books=store, pool=None)
    _book(store, "a", 0.48, 0.52)
    mm.step()
    mk = mm.st.markets["a"]
    assert mk.bid_yes and mk.bid_no
    mk.qty_yes, mk.cost_yes = 10.0, 4.8
    mm.sc["wind_down"] = True
    mm._last["select"] = -1e18
    mm.clock.sleep(2)
    mm.step()
    assert "a" in mm.st.markets and mk.bid_yes is None and mk.bid_no is None and mk.qty_yes == 10
    assert mk.ask and mk.ask["side"] == "yes" and mm.scan["wind_down"] == "keine neuen Gebote"
    mk.qty_yes = mk.cost_yes = 0
    mm._last["select"] = -1e18
    mm.clock.sleep(2)
    mm.step()
    assert "a" not in mm.st.markets


def test_aggressive_mm_scenarios_wind_down_and_the_others_do_not():
    import yaml
    sc = yaml.safe_load(open(Path(__file__).resolve().parents[1] / "config.yaml", encoding="utf-8"))["scenarios"]
    titles = {
        "mm_sofort": "sofort verkaufen",
        "mm_nah": "nah an der Mitte",
        "mm_ueberbucht": "überbucht",
        "mm_treppe": "Abbau-Treppe",
        "mm_ruhig": "Pause bei Sprüngen",
    }
    for name, bit in titles.items():
        s = sc[name]
        assert s["wind_down"] is True and s["unwind"] == "active" and s["enabled"] is True
        assert bit in s["title"] and "live" not in s
    for name in ("mm_rewards", "mm_robust", "mm_robust_quotealter", "mm_konservativ", "mm_neu_vorsichtig",
                 "mm_probe", "mm_breit", "mm_schutz", "mm_schutz_neu", "mm_stop"):
        assert not sc[name].get("wind_down") and sc[name].get("unwind") != "active"


def _held(cid="a", qty=80, cost=40, end=None):
    return MMMarket(cid=cid, title="Will the Fed cut rates?", yes=f"{cid}Y", no=f"{cid}N",
                    end_ts=NOW + 30 * 86400 if end is None else end, daily_rate=50, max_spread=0.04,
                    min_size=20, category="", qty_yes=qty, cost_yes=cost)


def test_active_unwind_quotes_nothing_and_sells_one_haircut_slice(tmp_path, caplog):
    import logging
    store = BookStore()
    cfg = _cfg(tmp_path, wind_down=True, unwind="active", exit={"after_h": 0, "dump_after_h": 1})
    cfg["execution"] = {"depth_haircut": 0.5}
    mm = MarketMaker("mm_rewards", cfg, Client([_m("a", "Will the Fed cut rates?", 0.50)]),
                     SimClock(NOW), books=store, pool=None)
    _book(store, "a", 0.50, 0.54, size=100)
    store.change("aY", 0.47, 200, "BUY", NOW)  # 3 ct under the touch: outside the 2 ct floor
    mm.step()
    assert mm.st.markets == {}  # wind-down selects nothing, so there is no new quote
    mm.st.markets["a"] = _held()
    mm._last["select"] = -1e18
    mm.clock.sleep(2)
    with caplog.at_level(logging.INFO, logger="polyarb.mm"):
        mm.step()
    mk = mm.st.markets["a"]
    assert mk.bid_yes is None and mk.bid_no is None and mk.ask is None
    assert mk.qty_yes == 30  # 100 * 0.5 at 0.50, not the 0.47 level
    qty, vwap, bid, pnl, vs = mm.store.db.execute(
        "SELECT qty, vwap, bid, pnl, vs_bid FROM mm_unwind").fetchone()
    assert qty == 50 and vwap == 0.50 and bid == 0.50 and abs(vs) < 1e-9 and abs(pnl) < 1e-9
    assert any("Unwind JA 50.00 @ 0.500" in r.message and "vs Bid" in r.message for r in caplog.records)
    # the book is read again: the next slice follows the new bid, it does not keep walking the old one
    store.snapshot("aY", [{"price": 0.40, "size": 100}], [{"price": 0.44, "size": 100}], NOW + 3)
    mm._last["select"] = -1e18
    mm.clock.sleep(2)
    mm.step()
    assert mk.qty_yes == 0
    bids = [r[0] for r in mm.store.db.execute("SELECT bid FROM mm_unwind ORDER BY ts")]
    assert bids == [0.50, 0.40]


def test_unwind_does_not_sell_the_same_bid_twice(tmp_path):
    store = BookStore()
    cfg = _cfg(tmp_path, wind_down=True, unwind="active")
    cfg["execution"] = {"depth_haircut": 0.5, "consumed_ttl_s": 60}
    mm = MarketMaker("mm_rewards", cfg, Client([]), SimClock(NOW), books=store, pool=None)
    store.snapshot("zY", [{"price": 0.45, "size": 100}], [{"price": 0.50, "size": 100}], NOW)
    mm.st.markets["z"] = _held("z", qty=80, cost=36)
    mm.unwind(NOW, store.books(["zY"]))
    assert mm.st.markets["z"].qty_yes == 30  # 100 * haircut 0.5
    mm.unwind(NOW, store.books(["zY"]))
    assert mm.st.markets["z"].qty_yes == 30  # that bid is already consumed
    mm.unwind(NOW + 61, store.books(["zY"]))
    assert mm.st.markets["z"].qty_yes == 0  # makers re-quoted after consumed_ttl_s


def test_unwind_respects_min_size_slippage_and_skips_resolved_markets(tmp_path):
    store = BookStore()
    cfg = _cfg(tmp_path, wind_down=True, unwind="active", max_unwind_slippage=0.02)
    cfg["execution"] = {"depth_haircut": 0.5}
    mm = MarketMaker("mm_rewards", cfg, Client([]), SimClock(NOW), books=store, pool=None)
    # 10 + 10 shares visible, half of each is inside the 2 ct band; 0.47 is not
    store.snapshot("aY", [{"price": 0.50, "size": 10}, {"price": 0.48, "size": 10}, {"price": 0.47, "size": 100}],
                   [{"price": 0.55, "size": 10}], NOW)
    mm.st.markets["a"] = _held(qty=100, cost=50)
    mm.unwind(NOW, store.books(["aY", "aN"]))
    mk = mm.st.markets["a"]
    assert mk.qty_yes == 90
    vwap, vs = mm.store.db.execute("SELECT vwap, vs_bid FROM mm_unwind").fetchone()
    assert abs(vwap - 0.49) < 1e-9 and abs(vs - (5 * 0.50 + 5 * 0.48 - 10 * 0.50)) < 1e-9
    # too little size after the haircut: stay, and say why
    store.snapshot("bY", [{"price": 0.50, "size": 8}], [{"price": 0.55, "size": 8}], NOW)
    mm.st.markets["b"] = _held("b", qty=40, cost=20)
    mm.unwind(NOW, {"bY": store.books(["bY"])["bY"]})
    assert mm.st.markets["b"].qty_yes == 40
    assert "unter Mindestgröße" in mm._unwind_why[("b", "yes")]
    # no bid
    store.snapshot("cY", [], [{"price": 0.55, "size": 50}], NOW)
    mm.st.markets["c"] = _held("c", qty=20, cost=10)
    mm.unwind(NOW, {"cY": store.books(["cY"])["cY"]})
    assert mm.st.markets["c"].qty_yes == 20 and mm._unwind_why[("c", "yes")] == "kein Bid"
    # already closed, and already resolved: neither is sold
    mm.st.markets["d"] = _held("d", qty=12, cost=6, end=NOW - 10)
    mm.client.res["eY"] = 1.0
    mm.st.markets["e"] = _held("e", qty=12, cost=6)
    store.snapshot("dY", [{"price": 0.40, "size": 100}], [{"price": 0.45, "size": 100}], NOW)
    store.snapshot("eY", [{"price": 0.40, "size": 100}], [{"price": 0.45, "size": 100}], NOW)
    mm.unwind(NOW, store.books(["dY", "eY"]))
    assert mm.st.markets["d"].qty_yes == 12 and mm.st.markets["e"].qty_yes == 12
    assert mm.scan["Unwind Auflösung"] == 2
    mm.client.res["dY"] = 1.0
    mm.settle(NOW)
    assert "d" not in mm.st.markets and "e" in mm.st.markets  # e has not reached its end


def test_unwind_without_the_flag_still_quotes(tmp_path):
    store = BookStore()
    mm = MarketMaker("mm_rewards", _cfg(tmp_path, unwind="active"),
                     Client([_m("a", "Will the Fed cut rates?", 0.50)]), SimClock(NOW), books=store, pool=None)
    _book(store, "a", 0.48, 0.52)
    mm.step()
    mk = mm.st.markets["a"]
    assert mk.bid_yes and mk.bid_no and mk.qty_yes == 0
    assert mm.store.db.execute("SELECT COUNT(*) FROM mm_unwind").fetchone()[0] == 0


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



def test_skip_re_drops_season_and_price_markets_also_via_the_event(tmp_path):
    import yaml
    probe = yaml.safe_load(open(Path(__file__).resolve().parents[1] / "config.yaml"))["scenarios"]["mm_probe"]
    ms = [_m("a", "Will the Fed cut rates in December?", 0.40),
          _m("b", "Will the Florida Panthers record 100+ points?", 0.40),
          _m("c", "Will the Ornn A100 Index be between $0.75 and $1.00 on November 30?", 0.40),
          _m("d", "Will Smith finish top 3?", 0.40, events=[{"title": "NHL Awards", "slug": "nhl-awards"}]),
          _m("e", "Will Anthropic's IPO occur in November?", 0.40)]
    mm = MarketMaker("mm_rewards", _cfg(tmp_path, skip_re=probe["skip_re"]), Client(ms), SimClock(NOW),
                     books=BookStore(), pool=None)
    mm.select(NOW)
    assert sorted(mm.st.markets) == ["a", "e"] and mm.scan["Muster ausgelassen"] == 3

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
                   "smallY": OrderBook("smallY", [Level(0.47, 10)], [Level(0.53, 10)]),
                   "smallN": OrderBook("smallN", [Level(0.47, 10)], [Level(0.53, 10)]),
                   "oldY": OrderBook("oldY", [Level(0.10, 10)], [Level(0.90, 10)])}
            return {t: out[t] for t in tids if t in out}

    mm = MarketMaker("mm_rewards", _cfg(tmp_path, max_age_days=3, rank="share", max_markets=1), C(ms), SimClock(NOW),
                     books=BookStore(), pool=None)
    mm.select(NOW)
    assert list(mm.st.markets) == ["small"]
    assert mm.scan["älter als max_age_days"] == 1 and mm.scan["erwarteter Reward $/Tag"] > 10
    # a book far wider than the qualifying spread gets no quotes, so it cannot rank first
    ms.append(_m("wide", "Will D happen?", 0.50, rate=1000, startDate=started))

    class W(C):
        def books(self, tids):
            out = C.books(self, [t for t in tids if not t.startswith("wide")])
            out.update({t: OrderBook(t, [Level(0.20, 10)], [Level(0.80, 10)]) for t in tids if t.startswith("wide")})
            return out

    mm2 = MarketMaker("mm_rewards", _cfg(tmp_path / "w", max_age_days=3, rank="share", max_markets=1), W(ms),
                      SimClock(NOW), books=BookStore(), pool=None)
    mm2.select(NOW)
    assert list(mm2.st.markets) == ["small"] and mm2.scan["Buch zu breit"] == 1


def test_hourly_metrics_feed_the_daily_trend_and_report(tmp_path):
    """Pool, our share and the estimated reward rate are recorded; dashboard and report show the trend."""
    store = BookStore()
    mm = MarketMaker("mm_rewards", _cfg(tmp_path, metrics_every_s=600), Client([_m("a", "Will the Fed cut rates?", 0.50)]),
                     SimClock(NOW), books=store, pool=None)
    _book(store, "a", 0.48, 0.52)
    for _ in range(8):
        mm.clock.sleep(400)
        mm.step()
    rows = mm.store.db.execute("SELECT pool_selected, pool_quoted, rewards_per_day, share_pct FROM mm_metrics").fetchall()
    assert len(rows) >= 2
    pool, pool_q, per_day, share = rows[-1]
    assert pool == 50 and pool_q == 50 and 0 < share < 100 and abs(per_day - 50 * share / 100) < 0.05
    mm.store.commit()
    from dashboard import mm_daily
    db = str(tmp_path / "scenario-mm_rewards.sqlite")
    day = mm_daily(db)
    assert len(day) == 1 and day[0][1] == 50 and 0 < day[0][4] < 100 and day[0][11] == len(rows)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy"))
    import notify
    line = notify.mm_trend(dict(label="MM", db=db), now=mm.clock.now())
    assert line.startswith("MM MM: Pool $50.00/Tag") and "Anteil" in line
    assert notify.mm_trend(dict(label="x", db=str(tmp_path / "scenario-none.sqlite"))) is None


def test_markets_kept_for_inventory_are_not_quoted(tmp_path):
    """max_markets limits the quoted markets; a deselected market with inventory only waits for resolution."""
    store = BookStore()
    cl = Client([_m("a", "Will the Fed cut rates?", 0.50, rate=50)])
    mm = MarketMaker("mm_rewards", _cfg(tmp_path, max_markets=1), cl, SimClock(NOW), books=store, pool=None)
    _book(store, "a", 0.48, 0.52)
    _book(store, "b", 0.48, 0.52)
    mm.step()
    store.trades.append(("aY", 0.47, 10, NOW + 5))  # inventory in a
    mm.clock.sleep(10)
    mm.step()
    assert mm.st.markets["a"].qty_yes == 10
    cl.markets = [_m("b", "Will B happen?", 0.50, rate=500)]  # b now pays more: a is dropped from the quotes
    mm._last["select"] = -1e18
    for _ in range(3):
        mm.clock.sleep(10)
        mm.step()
    a, b = mm.st.markets["a"], mm.st.markets["b"]
    assert a.bid_yes is None and a.bid_no is None and a.qty_yes == 10  # kept, not quoted
    assert b.bid_yes and b.bid_no
    assert mm.scan["ausgewählt"] == 1 and mm.scan["nur Bestand (keine Gebote)"] == 1


def test_breit_skips_large_min_sizes_and_caps_total_inventory(tmp_path):
    """mm_breit: no market whose reward minimum is too big for small quotes; past the total inventory cap only
    bids that reduce a position."""
    store = BookStore()
    ms = [_m("a", "Will A happen?", 0.50), dict(_m("b", "Will B happen?", 0.50), rewardsMinSize=200)]
    mm = MarketMaker("mm_rewards", _cfg(tmp_path, max_min_size_usd=25, max_total_inventory_usd=5), Client(ms),
                     SimClock(NOW), books=store, pool=None)
    _book(store, "a", 0.48, 0.52)
    mm.step()
    assert list(mm.st.markets) == ["a"] and mm.scan["Mindestgröße zu groß"] == 1
    store.trades.append(("aY", 0.47, 20, NOW + 5))  # 20 YES at 0.48 = 9.60 $ > cap 5
    mm.clock.sleep(10)
    mm.step()
    mk = mm.st.markets["a"]
    assert mk.qty_yes == 20 and mk.bid_yes is None and mk.bid_no is not None  # only the side that merges it away
    assert mm.scan["Bestandsgrenze erreicht"] == 1


def test_exit_sells_old_inventory_first_resting_then_at_the_bid(tmp_path):
    """exit: a one-sided position gets a sell order after after_h (filled only by a trade above it); what is
    still held after dump_after_h is sold at the bid with the taker fee."""
    store = BookStore()
    mm = MarketMaker("mm_rewards", _cfg(tmp_path, exit=dict(after_h=1, dump_after_h=3)),
                     Client([_m("a", "Will the Fed cut rates?", 0.50)]), SimClock(NOW), books=store, pool=None)
    _book(store, "a", 0.48, 0.52)
    mm.step()
    store.trades.append(("aY", 0.47, 30, NOW + 5))  # 30 YES at 0.48
    mm.clock.sleep(10)
    mm.step()
    mk = mm.st.markets["a"]
    assert mk.qty_yes == 30 and mk.ask is None  # too young to sell
    mm.clock.sleep(3600)
    mm.step()
    assert mk.ask and mk.ask["side"] == "yes" and mk.ask["price"] == 0.52 and mk.ask["size"] == 30
    store.trades.append(("aY", 0.52, 10, mm.clock.now()))  # at our price: not filled
    store.trades.append(("aY", 0.53, 10, mm.clock.now() + 2))  # above: 10 sold at 0.52
    mm.clock.sleep(10)
    mm.step()
    assert abs(mk.qty_yes - 20) < 1e-9 and abs(mm.st.exit_pnl - 10 * (0.52 - 0.48)) < 1e-9
    assert mm.st.markets["a"].bid_no is not None  # the NO bid stays (merging also reduces the position)
    mm.clock.sleep(2 * 3600)
    mm.step()  # older than 3 h: the rest goes at the bid (0.48, fee 0 here)
    assert mk.qty_yes == 0 and mk.ask is None and mm.scan["Bestand abgestoßen (Bid)"] == 1
    assert abs(mm.st.exit_usd - (10 * 0.52 + 20 * 0.48)) < 1e-9
    kinds = [r[0] for r in mm.store.db.execute("SELECT kind FROM settlements")]
    assert kinds.count("exit") == 2
    mm.st.save(mm.state_path)
    again = MarketMaker("mm_rewards", _cfg(tmp_path, exit=dict(after_h=1, dump_after_h=3)), Client([]), SimClock(NOW),
                        books=BookStore(), pool=None)
    assert abs(again.st.exit_usd - mm.st.exit_usd) < 1e-9


def test_real_rewards_next_to_the_estimate(tmp_path):
    import sqlite3
    from datetime import datetime, timezone
    from dashboard import rewards_compare
    db_path = tmp_path / "scenario-mm_pilot.sqlite"
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE settlements(ts REAL, ref TEXT, kind TEXT, qty REAL, payout REAL, pnl REAL)")
    db.execute("CREATE TABLE live_rewards(day TEXT PRIMARY KEY, usd REAL, ts REAL)")
    day_ts = datetime(2026, 11, 2, 12, tzinfo=timezone.utc).timestamp()
    db.executemany("INSERT INTO settlements VALUES(?,?,?,?,?,?)",
                   [(day_ts, "r", "reward", 0, 2.0, 2.0), (day_ts + 60, "r", "reward", 0, 2.0, 2.0),
                    (day_ts, "m", "merge", 1, 1.0, 0.04)])
    db.execute("INSERT INTO live_rewards VALUES('2026-11-02', 3.0, 0)")
    db.commit()
    assert rewards_compare(str(db_path)) == [["2026-11-02", 4.0, 3.0, 75]]
    (tmp_path / "live-status-mm_pilot.json").write_text(json.dumps({"live": True, "scoring": "8/10", "equity": 301.5}))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy"))
    import notify
    line = notify.live_line(dict(label="Pilot", db=str(db_path)))
    assert "gezahlt $3.00 (geschätzt $4.00, 75 %)" in line and "zählen jetzt 8/10" in line and "Konto $301.50" in line
    assert rewards_compare(str(tmp_path / "none.sqlite")) == []


def test_exit_ask_counts_for_rewards_and_sells_at_once(tmp_path):
    from arb.mm import reward_share
    store = BookStore()
    _book(store, "a", 0.48, 0.52)
    bk = store.books(["aY", "aN"])
    by, bn = bk["aY"], bk["aN"]
    # a YES sell at 0.52 is on the same side as a NO bid at 0.48: one-sided bid + ask = both sides
    bid = dict(price=0.48, size=100)
    one = reward_share(by, bn, bid, None, 0.50, 0.035, 20)
    both = reward_share(by, bn, bid, None, 0.50, 0.035, 20, dict(side="yes", price=0.52, size=100))
    assert both > one > 0
    assert reward_share(by, bn, None, None, 0.50, 0.035, 20, dict(side="no", price=0.52, size=100)) > 0
    mm = MarketMaker("mm_rewards", _cfg(tmp_path, exit=dict(after_h=0, dump_after_h=96), reward_asks=True),
                     Client([_m("a", "Will the Fed cut rates?", 0.50)]), SimClock(NOW), books=store, pool=None)
    mm.step()
    store.trades.append(("aY", 0.47, 30, NOW + 5))  # 30 YES at 0.48
    mm.clock.sleep(10)
    mm.step()
    mk = mm.st.markets["a"]
    mm.clock.sleep(2)
    mm.step()
    assert mk.qty_yes == 30 and mk.ask and mk.ask["side"] == "yes"  # sell order right away
    mm.clock.sleep(60)
    mm.step()
    assert mm.st.ask_rewards > 0 and mm.st.rewards > mm.st.ask_rewards


class FakeGW:
    """Live gateway stand-in: the account's fills and positions are set by the test."""
    def __init__(self):
        self.live, self.mode, self.counts, self.info, self.est = True, "live", dict(gesetzt=0, storniert=0, abgelehnt=0), {}, {}
        self.fills, self.sold, self.want, self.pos = [], [], {}, None

    def sync(self, now, want):
        self.want = dict(want)

    def new_fills(self):
        f, self.fills = self.fills, []
        return f

    def sell_now(self, now, tok, price, size, label=""):
        self.sold.append((tok, price, size))
        return True

    def real_positions(self, now):
        return self.pos

    def open_usd(self):
        return 0.0


def test_live_exit_sells_for_real_and_books_only_the_reported_fill(tmp_path):
    store = BookStore()
    mm = MarketMaker("mm_rewards", _cfg(tmp_path, exit=dict(after_h=1, dump_after_h=3), live=dict(quote_usd=20)),
                     Client([_m("a", "Will the Fed cut rates?", 0.50)]), SimClock(NOW), books=store, pool=None)
    mm.gw = gw = FakeGW()
    _book(store, "a", 0.48, 0.52)
    mm.step()
    gw.fills = [("aY", "BUY", 0.48, 30.0)]
    mm.clock.sleep(10)
    mm.step()
    mk = mm.st.markets["a"]
    assert mk.qty_yes == 30
    mm.clock.sleep(2)
    mm.step()  # the exit's clock starts at the next step
    mm.clock.sleep(3600)
    mm.step()
    assert ("aY", "SELL") in gw.want  # resting exit ask (limit, earns rewards)
    mm.clock.sleep(2)
    mm.step()
    assert gw.est["a"]["pct"] > 0  # the live comparison counts the sell order too
    mm.clock.sleep(2 * 3600)
    mm.step()
    assert gw.sold == [("aY", 0.48, 30.0)] and ("aY", "SELL") not in gw.want  # ask withdrawn, sold at the bid
    assert mk.qty_yes == 30 and mm.st.exit_usd == 0  # not booked before the exchange reports the fill
    gw.fills = [("aY", "SELL", 0.48, 30.0)]
    mm.clock.sleep(2)
    mm.step()
    assert mk.qty_yes == 0 and abs(mm.st.exit_usd - 30 * 0.48) < 1e-9


def test_live_inventory_follows_the_account_when_a_difference_persists(tmp_path):
    store = BookStore()
    mm = MarketMaker("mm_rewards", _cfg(tmp_path, live=dict(quote_usd=20)),
                     Client([_m("a", "Will the Fed cut rates?", 0.50)]), SimClock(NOW), books=store, pool=None)
    mm.gw = gw = FakeGW()
    _book(store, "a", 0.48, 0.52)
    mm.step()
    mk = mm.st.markets["a"]
    gw.pos, gw.info["cash"] = {"aY": dict(size=20.0, avg=0.32)}, mm.st.cash - 6.4
    mm.clock.sleep(2)
    mm.step()
    assert mk.qty_yes == 0 and "abgleich" not in gw.info  # first sighting: maybe just not booked yet
    mm.clock.sleep(2)
    mm.step()
    assert mk.qty_yes == 20 and abs(mk.cost_yes - 6.4) < 1e-9 and abs(mm.st.cash - gw.info["cash"]) < 1e-2
    assert "YES 0.00 -> 20.00" in gw.info["abgleich"] and "Cash" in gw.info["abgleich"]
    gw.pos = None  # unreadable: nothing changes
    mm.clock.sleep(2)
    mm.step()
    assert mk.qty_yes == 20


def test_live_takes_back_a_held_position_of_a_dropped_market(tmp_path):
    store = BookStore()
    mm = MarketMaker("mm_rewards", _cfg(tmp_path, exit=dict(after_h=0, dump_after_h=2), live=dict(quote_usd=20)),
                     Client([_m("a", "Will the Fed cut rates?", 0.50)]), SimClock(NOW), books=store, pool=None)
    mm.gw = gw = FakeGW()
    _book(store, "a", 0.48, 0.52)
    _book(store, "z", 0.30, 0.34)
    mm.step()
    gw.pos = {"zN": dict(size=20.0, avg=0.32, cid="z", title="CT-02", opposite="zY", index=1, end=None)}
    mm.clock.sleep(2)
    mm.step()
    assert "z" not in mm.st.markets
    mm.clock.sleep(2)
    mm.step()
    mk = mm.st.markets["z"]
    assert (mk.yes, mk.no, mk.qty_no, mk.qty_yes) == ("zY", "zN", 20.0, 0.0) and abs(mk.cost_no - 6.4) < 1e-9
    assert "wieder aufgenommen" in gw.info["abgleich"]
    mm.clock.sleep(2)
    mm.step()
    mm.clock.sleep(2)
    mm.step()
    assert ("zN", "SELL") in gw.want and ("zY", "BUY") not in gw.want and ("zN", "BUY") not in gw.want


def test_reprice_ticks_keeps_a_bid_while_the_mid_flickers(tmp_path):
    store = BookStore()
    mm = MarketMaker("mm_rewards", _cfg(tmp_path, reprice_ticks=2), Client([_m("a", "Will the Fed cut rates?", 0.50)]),
                     SimClock(NOW), books=store, pool=None)
    _book(store, "a", 0.48, 0.52)
    mm.step()
    mk = mm.st.markets["a"]
    first = mk.bid_yes["price"]
    _book(store, "a", 0.49, 0.53)  # mid +1 tick: keep the resting bid (still within the reward distance)
    mm.clock.sleep(2)
    mm.step()
    assert mk.bid_yes["price"] == first
    _book(store, "a", 0.51, 0.55)  # mid +3 ticks: move
    mm.clock.sleep(2)
    mm.step()
    assert mk.bid_yes["price"] > first


def test_overcommit_bids_beyond_cash_but_fills_only_what_the_cash_pays(tmp_path):
    store = BookStore()
    mm = MarketMaker("mm_rewards", _cfg(tmp_path, overcommit=2, capital_usd=30, quote_usd=20, cash_buffer_pct=0,
                                        exit=dict(after_h=0, ask_frac=0.25, dump_after_h=96)),
                     Client([_m("a", "Will the Fed cut rates?", 0.50)]), SimClock(NOW), books=store, pool=None)
    _book(store, "a", 0.48, 0.52)
    mm.step()
    mk = mm.st.markets["a"]
    assert mk.bid_yes and mk.bid_no  # 2 x ~20 $ resting with 30 $ cash
    assert mk.bid_yes["price"] * mk.bid_yes["size"] + mk.bid_no["price"] * mk.bid_no["size"] > mm.st.cash
    store.trades.append(("aY", 0.47, 100, NOW + 5))  # takes the whole YES bid
    store.trades.append(("aN", 0.47, 100, NOW + 6))  # and the NO bid – but the cash is short
    mm.clock.sleep(10)
    mm.step()
    assert mm.st.cash >= -1e-9 and mm.scan["Fills ohne Cash (überbucht)"] == 1
    mm.clock.sleep(2)
    mm.step()
    if mk.ask:  # the exit sells a quarter of the reward distance above the mid, not half
        assert mk.ask["price"] <= 0.50 + 0.04 * 0.25 + 0.01 + 1e-9


def test_exit_decay_steps_the_sell_order_down_every_hour(tmp_path):
    store = BookStore()
    mm = MarketMaker("mm_rewards", _cfg(tmp_path, exit=dict(after_h=0, decay_h=4, dump_after_h=96)),
                     Client([_m("a", "Will the Fed cut rates?", 0.50)]), SimClock(NOW), books=store, pool=None)
    _book(store, "a", 0.46, 0.54)
    mm.step()
    mk = mm.st.markets["a"]
    mk.qty_yes, mk.cost_yes = 20.0, 9.0
    prices = []
    for _ in range(6):
        mm.step()
        prices.append(mk.ask["price"])
        mm.clock.sleep(3600)
    # from mid + 2 ct down to the mid itself – always inside the reward band (mid .. mid + 4 ct), never below the mid
    assert prices[0] == 0.52 and prices == sorted(prices, reverse=True) and prices[-1] == 0.50
    assert all(0.50 <= p < 0.54 for p in prices)


def test_markout_measures_the_price_move_after_a_fill(tmp_path):
    store = BookStore()
    mm = MarketMaker("mm_rewards", _cfg(tmp_path), Client([_m("a", "Will the Fed cut rates?", 0.50)]), SimClock(NOW),
                     books=store, pool=None)
    _book(store, "a", 0.48, 0.52)
    mm.step()
    store.trades.append(("aY", 0.47, 30, NOW + 5))  # 30 YES bought at 0.48
    mm.clock.sleep(10)
    mm.step()
    _book(store, "a", 0.40, 0.44)  # the price falls after our fill: adverse selection
    mm.clock.sleep(301)
    mm.step()
    m5 = mm.store.db.execute("SELECT m5 FROM mm_markout").fetchone()[0]
    assert abs(m5 - 30 * (0.42 - 0.48)) < 1e-9
    mm.clock.sleep(3600)
    mm.step()
    assert mm.store.db.execute("SELECT m60 FROM mm_markout").fetchone()[0] is not None and not mm._markout


def test_pause_pulls_the_bids_after_a_fast_move(tmp_path):
    store = BookStore()
    mm = MarketMaker("mm_rewards", _cfg(tmp_path, pause=dict(ticks=3, window_s=300, pause_s=600)),
                     Client([_m("a", "Will the Fed cut rates?", 0.50)]), SimClock(NOW), books=store, pool=None)
    _book(store, "a", 0.48, 0.52)
    mm.step()
    mk = mm.st.markets["a"]
    assert mk.bid_yes
    _book(store, "a", 0.52, 0.56)  # mid +4 ticks within a minute
    mm.clock.sleep(60)
    mm.step()
    assert mk.bid_yes is None and mk.bid_no is None and mm.scan["Pausen (schnelle Kursbewegung)"] == 1
    mm.clock.sleep(601)
    mm.step()
    assert mk.bid_yes  # quiet again: back in the book


def test_learn_blocks_a_market_whose_fills_cost_more_than_its_rewards(tmp_path):
    store = BookStore()
    mm = MarketMaker("mm_rewards", _cfg(tmp_path, learn=dict(min_fills=1, block_h=72)),
                     Client([_m("a", "Will the Fed cut rates?", 0.50)]), SimClock(NOW), books=store, pool=None)
    _book(store, "a", 0.48, 0.52)
    mm.step()
    store.trades.append(("aY", 0.47, 30, NOW + 5))  # 30 YES bought at 0.48
    mm.clock.sleep(10)
    mm.step()
    _book(store, "a", 0.40, 0.44)  # the price falls: −1.80 $ markout, far more than the rewards of an hour
    mm.clock.sleep(3601)
    mm.step()
    assert mm.blocked("a", mm.clock.now()) and "Fills kosteten" in mm._ledger["a"]["why"]
    mk = mm.st.markets["a"]
    assert mk.bid_yes is None and mk.bid_no is None and mm._ledger["a"]["fills"] == 0  # fresh start afterwards
    mm.select(mm.clock.now())
    assert mm.scan["gelernt: gesperrt"] == 1 and "a" not in mm.quoted
    # remembered across a restart, and the next block lasts twice as long
    mm2 = MarketMaker("mm_rewards", _cfg(tmp_path, learn=dict(min_fills=1, block_h=72)),
                      Client([_m("a", "Will the Fed cut rates?", 0.50)]), SimClock(mm.clock.now()), books=store, pool=None)
    assert mm2.blocked("a", mm.clock.now()) and mm2._ledger["a"]["blocks"] == 1


def test_learn_blocks_a_market_that_keeps_jumping(tmp_path):
    store = BookStore()
    mm = MarketMaker("mm_rewards", _cfg(tmp_path, pause=dict(ticks=3, window_s=300, pause_s=60),
                                        learn=dict(max_jumps=2, block_h=24)),
                     Client([_m("a", "Will the Fed cut rates?", 0.50)]), SimClock(NOW), books=store, pool=None)
    for i, (b, a) in enumerate([(0.48, 0.52), (0.52, 0.56), (0.48, 0.52), (0.52, 0.56)]):
        _book(store, "a", b, a)
        mm.step()
        mm.clock.sleep(120)
    assert mm.blocked("a", mm.clock.now()) and "Kurssprünge" in mm._ledger["a"]["why"]


def test_microprice_and_pair_frac_shift_the_bids(tmp_path):
    store = BookStore()
    mm = MarketMaker("mm_rewards", _cfg(tmp_path, fair="micro", pair_frac=0.25),
                     Client([_m("a", "Will the Fed cut rates?", 0.50)]), SimClock(NOW), books=store, pool=None)
    store.snapshot("aY", [{"price": 0.48, "size": 50}], [{"price": 0.52, "size": 950}], NOW)  # sellers dominate
    store.snapshot("aN", [{"price": 0.48, "size": 950}], [{"price": 0.52, "size": 50}], NOW)
    mm.step()
    mk = mm.st.markets["a"]
    assert mk.bid_yes["price"] < 0.48 and mk.bid_no["price"] == 0.48  # YES bid moved down, NO stays at the mid's
    _book(store, "a", 0.48, 0.52)
    mk.qty_yes, mk.cost_yes = 30.0, 30 * 0.48  # holding YES: the NO bid comes closer, the pair stays below 1 $
    mm.clock.sleep(10)
    mm.step()
    assert mk.bid_no["price"] == 0.49 and mk.bid_no["price"] + 0.48 < 1


def test_list_orders_merge_several_listings(tmp_path):
    class C(Client):
        def paged(self, path, params, max_items=1000):
            return [m for m in self.markets if m["order"] == params["order"]]
    a = dict(_m("a", "Will the Fed cut rates?", 0.40), order="volume")
    b = dict(_m("b", "Will the ECB cut rates?", 0.40), order="liquidity")
    mm = MarketMaker("mm_rewards", _cfg(tmp_path, list_orders=["volume", "liquidity"]), C([a, b, dict(a, order="liquidity")]),
                     SimClock(NOW), books=BookStore(), pool=None)
    mm.select(NOW)
    assert mm.scan["gelistet"] == 2 and sorted(mm.st.markets) == ["a", "b"]


def test_fill_cooldown_stops_buying_into_the_move(tmp_path):
    store = BookStore()
    mm = MarketMaker("mm_rewards", _cfg(tmp_path, fill_cooldown_s=3600), Client([_m("a", "Will the Fed cut rates?", 0.50)]),
                     SimClock(NOW), books=store, pool=None)
    _book(store, "a", 0.48, 0.52)
    mm.step()
    store.trades.append(("aY", 0.47, 500, NOW + 5))  # our YES bid filled completely
    mm.clock.sleep(10)
    mm.step()
    mk = mm.st.markets["a"]
    assert mk.qty_yes > 0 and mk.bid_yes is None and mk.bid_no  # no new YES bid, NO side still quoted
    mm.clock.sleep(3601)
    mm.step()
    assert mk.bid_yes  # an hour later: back in the book


def test_stop_loss_sells_at_the_bid_and_fills_are_exported(tmp_path):
    store = BookStore()
    mm = MarketMaker("mm_rewards", _cfg(tmp_path, exit=dict(after_h=24, stop_ticks=3, dump_after_h=96)),
                     Client([_m("a", "Will the Fed cut rates?", 0.50)]), SimClock(NOW), books=store, pool=None)
    _book(store, "a", 0.48, 0.52)
    mm.step()
    store.trades.append(("aY", 0.47, 30, NOW + 5))  # 30 YES bought at 0.48
    mm.clock.sleep(10)
    mm.step()
    mk = mm.st.markets["a"]
    assert abs(mk.qty_yes - 30) < 1e-9
    _book(store, "a", 0.46, 0.50)  # mid 0.48: not 3 ticks below the 0.48 entry yet
    mm.clock.sleep(10)
    mm.step()
    assert mk.qty_yes > 0
    _book(store, "a", 0.43, 0.47)  # mid 0.45 = 3 ticks below: sold at the bid at once (well before after_h)
    mm.clock.sleep(10)
    mm.step()
    assert mk.qty_yes < 1e-9 and mm.scan["Stop-Loss ausgelöst"] >= 1
    mm.store.commit()
    import dashboard
    rows = dashboard.markout_rows(str(tmp_path / "scenario-mm_rewards.sqlite"))
    assert rows and rows[0][2] == "Will the Fed cut rates?" and rows[0][4] == "JA" and rows[0][9] == 2.0


def test_dropped_market_is_marked_at_the_current_bid(tmp_path):
    """A position we no longer quote used to keep the last mid. It is marked at the bid, and the bid moves."""
    store = BookStore()
    mm = MarketMaker("mm_rewards", _cfg(tmp_path), Client([_m("a", "Will the Fed cut rates?", 0.50)]),
                     SimClock(NOW), books=store, pool=None)
    _book(store, "a", 0.48, 0.52)
    mm.step()
    store.trades.append(("aY", 0.47, 30, NOW + 5))
    mm.clock.sleep(10)
    mm.step()
    mk = mm.st.markets["a"]
    assert abs(mk.qty_yes - 30) < 1e-9
    assert abs(mm.st.inventory_value() - 30 * 0.48) < 1e-6  # bid, not the 0.50 mid
    assert abs(mm.st.inventory_mid() - 30 * 0.50) < 1e-6
    mm.quoted = set()  # dropped from the quote set; the position stays
    _book(store, "a", 0.30, 0.34)
    mm.clock.sleep(10)
    mm.step()
    assert mk.bid_yes is None and abs(mk.yes_bid - 0.30) < 1e-9
    assert abs(mm.st.inventory_value() - 30 * 0.30) < 1e-6


def test_min_quote_frac_never_steps_inside_half_the_spread(tmp_path):
    store = BookStore()
    mm = MarketMaker("mm_rewards", _cfg(tmp_path, min_quote_frac=0.5, pair_frac=0.25,
                                        exit=dict(after_h=0, decay_h=4, dump_after_h=96)),
                     Client([_m("a", "Will the Fed cut rates?", 0.50)]), SimClock(NOW), books=store, pool=None)
    _book(store, "a", 0.48, 0.52)  # mid 0.50, reward spread 4 ct, half of it is 2 ct
    mm.step()
    mk = mm.st.markets["a"]
    assert mk.bid_yes["price"] == 0.48 and mk.bid_no["price"] == 0.48
    mk.qty_yes, mk.cost_yes = 30.0, 30 * 0.48  # pair_frac would pull the NO bid to 0.49; the floor holds it
    mm.clock.sleep(10)
    mm.step()
    assert mk.bid_no["price"] == 0.48
    prices = []
    for _ in range(5):
        mm.step()
        prices.append(mk.ask["price"])
        mm.clock.sleep(3600)
    assert prices and min(prices) >= 0.52 - 1e-9  # exit ask decays toward the mid, stops at half the spread


def test_topic_filters_and_event_cap(tmp_path):
    soon = "2026-09-30T00:00:00Z"  # nine days after NOW
    ev = {"id": "ev1", "title": "Two buckets"}
    ms = [_m("b1", "Will bucket one happen?", 0.40, events=[ev]),
          _m("b2", "Will bucket two happen?", 0.40, events=[ev]),
          _m("ok", "Will the Fed cut rates in December?", 0.40),
          _m("oscar", "Will the movie win an Oscar?", 0.40),
          _m("views", "Will the GTA VI trailer get 40-60 million views?", 0.40),
          _m("tag", "Will something quiet happen?", 0.40, tags=[{"slug": "pop-culture", "label": "Pop Culture"}]),
          _m("soon", "Will a near event happen?", 0.40, end=soon)]
    mm = MarketMaker("mm_rewards", _cfg(
        tmp_path, min_days_to_end=14, max_event_usd=25, quote_usd=20, max_markets=10,
        skip_categories=["Sport", "Krypto", "Kultur", "Social"], skip_tags=["pop-culture", "culture"],
        skip_re=[r"\bviews?\b", r"\bgta\s*(vi|6)\b"]), Client(ms), SimClock(NOW), books=BookStore(), pool=None)
    mm.select(NOW)
    assert sorted(mm.st.markets) == ["b1", "ok"]
    assert mm.scan["Ereignis-Grenze"] == 1 and mm.scan["Kategorie ausgelassen"] >= 1
    assert mm.scan["Muster ausgelassen"] == 1 and mm.scan["Thema ausgelassen"] == 1 and mm.scan["endet zu bald"] >= 1
    assert mm.st.markets["b1"].event == "event:ev1"
    # quote-time cap: both buckets are already chosen, the second bid must not add another 20 $
    store = mm.books
    mm.st.markets["b2"] = mm.st.markets["b1"].__class__(
        **{**mm.st.markets["b1"].__dict__, "cid": "b2", "yes": "b2Y", "no": "b2N", "title": "bucket two"})
    mm.quoted = {"b1", "b2", "ok"}
    mm.sc["max_event_usd"] = 25
    _book(store, "b1", 0.48, 0.52)
    _book(store, "b2", 0.48, 0.52)
    _book(store, "ok", 0.48, 0.52)
    mm._last["select"] = mm.clock.now()
    mm.quote(mm.clock.now(), store.books(["b1Y", "b1N", "b2Y", "b2N", "okY", "okN"]))
    assert mm.st.markets["b1"].bid_yes and mm.st.markets["ok"].bid_yes
    assert mm.st.markets["b2"].bid_yes is None and mm.st.markets["b2"].bid_no is None
    assert mm.scan["Ereignis-Grenze (Gebot)"] >= 1


def test_dashboard_splits_trading_rewards_and_rebates(tmp_path):
    import json
    import re
    from dashboard import build, mm_equity_parts
    store = BookStore()
    m = _m("a", "Will the Fed cut rates?", 0.50, feeSchedule={"rate": 0.05})
    mm = MarketMaker("mm_rewards", _cfg(tmp_path), Client([m]), SimClock(NOW), books=store, pool=None)
    _book(store, "a", 0.48, 0.52)
    mm.step()
    store.trades.extend([("aY", 0.47, 10, NOW + 5), ("aY", 0.53, 30, NOW + 6)])
    mm.clock.sleep(400)
    mm.step()
    mm.store.commit()
    db = str(tmp_path / "scenario-mm_rewards.sqlite")
    parts = mm_equity_parts(db)
    row = mm.store.db.execute(
        "SELECT realized_cum, trade_cum, rewards_cum, rebates_cum FROM equity ORDER BY ts DESC LIMIT 1").fetchone()
    assert parts["rewards"] > 0 and parts["rebates"] > 0
    assert abs(parts["trade"] - mm.st.realized) < 1e-6
    assert abs(row[0] - (row[1] + row[2] + row[3])) < 1e-6  # the stored total is still the sum
    assert abs(parts["trade"] + parts["rewards"] + parts["rebates"] - row[0]) < 0.05
    html = open(build(db, str(tmp_path / "www" / "index.html"), 1000, title="MM", kind="mm"), encoding="utf-8").read()
    data = json.loads(re.search(r"const D=(.*);\nconst \$", html).group(1))
    assert data["split"]["rewards"] > 0 and abs(data["split"]["trade"] - round(mm.st.realized, 2)) < 1e-9
    assert "Handel realisiert" in html and "Rewards geschätzt" in html and "Rebates" in html
    assert abs(data["kpi"]["real"] - data["split"]["trade"]) < 1e-9
    last = data["curve"][-1]
    assert abs(last["r"] - (1000 + data["split"]["trade"])) < 1e-6 and last["rw"] == data["split"]["rewards"]
    assert last["h"] == round(last["e"] - 0.4 * parts["rewards"], 2)
    assert "Equity bei 60 % Rewards" in html and "60 % der geschätzten Rewards" in html
    mm._write_scan(mm.clock.now())
    written = json.loads(Path(mm.state_path.replace(".json", ".scan.json")).read_text(encoding="utf-8"))
    assert written["Equity bei 60 % Rewards $"] == round(mm.st.equity - 0.4 * mm.st.rewards, 2)


def test_mm_robust_is_a_separate_scenario_and_leaves_the_others_alone():
    import yaml
    sc = yaml.safe_load(open(Path(__file__).resolve().parents[1] / "config.yaml", encoding="utf-8"))["scenarios"]
    r = sc["mm_robust"]
    assert r["enabled"] is True and r["strategy"] == "mm" and "live" not in r
    assert r["min_days_to_end"] >= 14 and r["min_quote_frac"] >= 0.5 and r["quote_frac"] >= r["min_quote_frac"]
    assert r["max_event_usd"] > 0 and "Kultur" in r["skip_categories"] and r["skip_tags"] and r["skip_re"]
    assert sc["mm_sofort"]["min_days_to_end"] == 3 and "max_event_usd" not in sc["mm_sofort"]
    assert sc["mm_sofort"]["enabled"] is True and "min_share" not in (sc["mm_sofort"].get("exit") or {})
    assert sc["mm_nah"]["quote_frac"] == 0.25 and "min_quote_frac" not in sc["mm_nah"] and sc["mm_nah"]["enabled"] is True
    assert sc["mm_treppe"]["enabled"] is True and sc["mm_ueberbucht"]["enabled"] is True
    assert sc["mm_ruhig"]["enabled"] is True and sc["mm_stop"]["enabled"] is True and sc["mm_rewards"]["enabled"] is True
    assert "Kultur" not in sc["mm_rewards"]["skip_categories"]
    k, n = sc["mm_konservativ"], sc["mm_neu_vorsichtig"]
    # 19.10. owner: mm_konservativ (0,03 $/day expected) and mm_neu_vorsichtig (10k volume filter: 5 markets) archived
    assert k["enabled"] is False and k["archive"] is True and n["enabled"] is False and n["archive"] is True
    assert "live" not in k and k["min_days_to_end"] >= 30 and k["quote_frac"] >= 0.8
    assert k["min_quote_frac"] >= k["quote_frac"] and k["quote_usd"] <= 20 and k["pause"] and k["exit"]["stop_ticks"]
    assert "live" not in n and n["max_age_days"] <= 3 and "overcommit" not in n
    assert n["exit"]["min_share"] > 0 and n["max_inventory_usd"] <= 50 and n["quote_frac"] == 0.5
    assert n["min_days_to_end"] >= 14 and n["rewards_active"] is True and n["min_event_volume_24h"] >= 10000
    assert "Kultur" in n["skip_categories"] and n["skip_re"] and n["pause"]["ticks"] >= 3
    assert n["exit"]["stop_ticks"] >= 3 and "skew" not in n
    n2 = sc["mm_neu_vorsichtig2"]  # the same, but 24-h volume from 1,000 $
    assert n2["enabled"] is True and "live" not in n2 and n2["min_event_volume_24h"] == 1000
    assert {k_: v for k_, v in n2.items() if k_ not in ("enabled", "title", "min_event_volume_24h")} == \
        {k_: v for k_, v in n.items() if k_ not in ("enabled", "archive", "title", "min_event_volume_24h")}
    q = sc["mm_robust_quotealter"]
    assert q["enabled"] is False and q["archive"] is True and "live" not in q and q["strategy"] == "mm"
    assert q["max_quote_age_s"] == 30 and q["requote_ticks"] == 1
    assert q["skew"]["fav_frac"] == r["skew"]["fav_frac"] >= 0.5
    assert "max_quote_age_s" not in r and "requote_ticks" not in r
    for name in ("mm_robust", "mm_robust_quotealter", "mm_konservativ", "mm_neu_vorsichtig"):
        s = sc[name]
        assert s["max_min_size_usd"] == s["quote_usd"] and "live" not in s
    w = sc["wetter_sniper"]
    assert w["enabled"] is True and w["strategy"] == "wxsniper" and "live" not in w
    assert w["min_margin"] >= 1 and w["min_yes"] >= 0.03 and w["max_slippage"] == 0


def test_min_size_above_quote_usd_is_not_quoted(tmp_path):
    """A reward minimum that costs more than quote_usd is not quoted, and a quote that does fit stays within it."""
    store = BookStore()
    ms = [_m("ok", "Will the small minimum qualify?", 0.50),
          dict(_m("big", "Will the minimum exceed the quote?", 0.50), rewardsMinSize=80)]
    mm = MarketMaker("mm_rewards", _cfg(tmp_path, quote_usd=15, max_min_size_usd=15), Client(ms),
                     SimClock(NOW), books=store, pool=None)
    _book(store, "ok", 0.48, 0.52)
    _book(store, "big", 0.48, 0.52)
    mm.step()
    assert list(mm.st.markets) == ["ok"] and mm.scan["Mindestgröße zu groß"] == 1
    assert "big" not in mm.st.markets
    mk = mm.st.markets["ok"]
    assert mk.bid_yes and mk.bid_no
    for q in (mk.bid_yes, mk.bid_no):
        assert q["price"] * q["size"] <= 15 + 1e-6


def test_min_share_pulls_quotes_and_sells_inventory_only_when_set(tmp_path):
    store = BookStore()
    _book(store, "a", 0.48, 0.52, size=20000)  # our ~40 shares are a tiny share of this book
    plain = MarketMaker("mm_rewards", _cfg(tmp_path / "plain"), Client([_m("a", "Will the Fed cut rates?", 0.50)]),
                        SimClock(NOW), books=store, pool=None)
    plain.step()
    assert plain.st.markets["a"].bid_yes is not None  # without min_share the crowd does not stop the quote
    crowded = MarketMaker("mm_rewards", _cfg(tmp_path / "exit", exit={"after_h": 24, "dump_after_h": 96, "min_share": 0.5}),
                          Client([_m("a", "Will the Fed cut rates?", 0.50)]), SimClock(NOW), books=BookStore(), pool=None)
    _book(crowded.books, "a", 0.48, 0.52, size=20000)
    crowded.select(NOW)
    mk = crowded.st.markets["a"]
    mk.qty_yes, mk.cost_yes = 30.0, 15.0
    crowded._last["select"] = NOW
    crowded.step()
    assert mk.bid_yes is None and mk.bid_no is None and mk.qty_yes == 0
    assert crowded.scan.get("Reward-Anteil unter Schwelle", 0) >= 1
    assert crowded.scan.get("Bestand abgestoßen (Bid)", 0) >= 1


SKEW = dict(band_lo=0.30, band_hi=0.70, min_mid=0.10, max_mid=0.90, fav_frac=0.25, long_frac=0.75, long_usd=15)


def test_reward_program_window_and_event_volume_are_opt_in(tmp_path):
    from arb.mm import volume_24h
    ended = "2020-01-01T00:00:00Z"
    later = "2027-06-01T00:00:00Z"
    assert volume_24h({"events": [{"volume24hr": 12000}], "volume24hr": 100}) == 12000
    assert volume_24h({"volume24hrClob": 8000}) == 8000
    stale = {"clobRewards": [{"rewardsDailyRate": 40, "endDate": ended}], "rewardsMaxSpread": 4, "rewardsMinSize": 20}
    assert rewards_of(stale)[0] == 40  # legacy: the listed rate, dates ignored
    assert rewards_of(stale, NOW, True)[0] == 0
    assert rewards_of({"clobRewards": [{"rewardsDailyRate": 9}], "rewardsMaxSpread": 4, "rewardsMinSize": 20},
                      NOW, True)[0] == 9  # no dates: treated as running
    def dated(cid, question, when, key):
        m = _m(cid, question, 0.40, rate=40, events=[{"id": cid, "volume24hr": 50000}])
        m["clobRewards"] = [{"rewardsDailyRate": 40, key: when}]
        return m
    ms = [_m("a", "Big event market", 0.40, events=[{"id": "e", "volume24hr": 20000}]),
          _m("b", "Small event market", 0.40, events=[{"id": "s", "volume24hr": 1000}]),
          dated("c", "Rewards already over", ended, "endDate"),
          dated("d", "Rewards not started", later, "startDate")]
    plain = MarketMaker("mm_rewards", _cfg(tmp_path / "plain"), Client(ms), SimClock(NOW), books=BookStore(), pool=None)
    plain.select(NOW)
    assert set(plain.st.markets) == {"a", "b", "c", "d"}
    flt = MarketMaker("mm_rewards", _cfg(tmp_path / "flt", rewards_active=True, min_event_volume_24h=10000),
                      Client(ms), SimClock(NOW), books=BookStore(), pool=None)
    flt.select(NOW)
    assert list(flt.st.markets) == ["a"]
    assert flt.scan["Ereignis-Volumen 24h zu klein"] == 1 and flt.scan["Reward-Programm nicht aktiv"] == 2
    row = flt.store.db.execute(
        "SELECT daily_rate, max_spread_ct, min_size, volume_24h, programs FROM mm_reward_params").fetchone()
    assert row[0] == 50 and row[1] == 4 and row[2] == 20 and row[3] == 20000 and "ohne Datum" in row[4]


def test_skew_leans_to_the_favourite_and_quotes_the_middle_symmetrically(tmp_path):
    store = BookStore()
    mm = MarketMaker("mm_rewards", _cfg(tmp_path, skew=dict(SKEW), min_mid=0.10, max_mid=0.90),
                     Client([_m("a", "Will the favourite win?", 0.80), _m("b", "Will it be close?", 0.50)]),
                     SimClock(NOW), books=store, pool=None)
    _book(store, "a", 0.78, 0.82)
    _book(store, "b", 0.48, 0.52)
    mm.step()
    fav, midm = mm.st.markets["a"], mm.st.markets["b"]
    assert fav.bid_yes["price"] == 0.79 and fav.bid_no["price"] == 0.17  # closer / further than half the 4 ct band
    assert fav.bid_no["price"] * fav.bid_no["size"] <= 15 + 1e-6
    assert fav.bid_yes["size"] >= 20 and midm.bid_yes["price"] == midm.bid_no["price"] == 0.48
    y, n = mm.planned_quotes(0.80, 0.04, 20)
    mm.sc["skew"] = dict(SKEW, long_usd=0)
    y_off, n_off = mm.planned_quotes(0.80, 0.04, 20)
    assert y_off and n_off is None and y and n
    mm.sc["skew"] = dict(SKEW, long_usd=1)  # 1 $ at ~0.17 is 5 shares, under rewardsMinSize 20: side stays off
    y_small, n_small = mm.planned_quotes(0.80, 0.04, 20)
    assert y_small and n_small is None
    mm.step()
    assert mm.st.markets["a"].bid_no is None and mm.scan.get("Außenseiter unter Reward-Mindestgröße", 0) >= 1
    full = MarketMaker("mm_rewards", _cfg(tmp_path / "full", quote_usd=20), Client([]), SimClock(NOW),
                       books=BookStore(), pool=None)
    assert full._sized(0.50, 20, 50) == 50  # the full quote is still raised to the reward minimum
    book = OrderBook("Y", [Level(0.78, 100)], [Level(0.82, 100)])
    other = OrderBook("N", [Level(0.18, 100)], [Level(0.22, 100)])
    assert reward_share(book, other, y_off, None, 0.80, 0.04, 20) < reward_share(book, other, y, n, 0.80, 0.04, 20)
    _book(store, "b", 0.94, 0.96)  # live mid above 0.90: both bids pulled
    mm.clock.sleep(2)
    mm._last["select"] = mm.clock.now()
    mm.step()
    assert mm.st.markets["b"].bid_yes is None and mm.st.markets["b"].bid_no is None
    assert mm.scan["Mitte zu extrem (Gebot)"] >= 1


def test_fill_records_quote_age_band_and_markouts(tmp_path):
    store = BookStore()
    mm = MarketMaker("mm_rewards", _cfg(tmp_path), Client([_m("a", "Will the Fed cut rates?", 0.50)], res={"aY": 0}),
                     SimClock(NOW), books=store, pool=None)
    _book(store, "a", 0.48, 0.52)
    mm.step()
    store.trades.append(("aY", 0.47, 30, NOW + 5))
    mm.clock.sleep(10)
    mm.step()
    age, dist, band, price = mm.store.db.execute("SELECT age_s, dist, band, price FROM mm_markout").fetchone()
    assert band == "mitte" and abs(dist - 0.02) < 1e-9 and 9 <= age <= 11 and price == 0.48
    _book(store, "a", 0.46, 0.50)
    mm.clock.sleep(2)
    mm.update_markouts(mm.clock.now(), store.books(["aY", "aN"]))
    m1s, m10s = mm.store.db.execute("SELECT m1s, m10s FROM mm_markout").fetchone()
    assert m1s == round((0.48 - 0.48) * 30, 4) and m10s is None  # first book at least 1 s later; step is 2 s
    _book(store, "a", 0.44, 0.48)
    mm.clock.sleep(10)
    mm.update_markouts(mm.clock.now(), store.books(["aY", "aN"]))
    m10s = mm.store.db.execute("SELECT m10s FROM mm_markout").fetchone()[0]
    assert m10s == round((0.46 - 0.48) * 30, 4)
    _book(store, "a", 0.40, 0.44)
    mm.clock.sleep(3600)
    mm.update_markouts(mm.clock.now(), store.books(["aY", "aN"]))
    m1, m5, m60 = mm.store.db.execute("SELECT m1, m5, m60 FROM mm_markout").fetchone()
    assert m1 == m5 == m60 == round((0.42 - 0.48) * 30, 4)
    mm.st.markets["a"].end_ts = mm.clock.now() - 1
    mm.settle(mm.clock.now())
    assert mm.store.db.execute("SELECT m_res FROM mm_markout").fetchone()[0] == round((0 - 0.48) * 30, 4)
    import dashboard
    mm.store.commit()
    db = str(tmp_path / "scenario-mm_rewards.sqlite")
    rows = dashboard.markout_rows(db)
    assert rows[0][9] == 2.0 and rows[0][14] == age and rows[0][15] == "Mitte" and rows[0][16] == m1
    assert rows[0][18] == m1s and rows[0][19] == m10s
    mm._write_scan(mm.clock.now())
    assert "Mitte" in mm.scan["Markout nach Seite je 1 $"] and "60 Min" in mm.scan["Markout nach Seite je 1 $"]
    ages = dashboard.markout_age(db)
    hit = [r for r in ages if r[0] == "< 30 s" and r[1] == "Mitte"]
    assert hit and hit[0][2] == 1 and abs(hit[0][6] - (m60 / (0.48 * 30))) < 1e-3
    page = tmp_path / "dash" / "index.html"
    dashboard.build(db, str(page), 1000, kind="mm", title="MM")
    text = page.read_text(encoding="utf-8")
    assert "Gebotsalter und Favorit" in text and "< 30 s" in text
    fills = (page.parent / "fills.csv").read_text(encoding="utf-8")
    assert "Gebotsalter s" in fills and "Kursbewegung 1 s $" in fills and "Kursbewegung 10 s $" in fills
    assert (page.parent / "gebotsalter.csv").exists()


def test_quote_refresh_options_do_nothing_unless_set(tmp_path):
    store = BookStore()
    mm = MarketMaker("mm_rewards", _cfg(tmp_path / "off", reprice_ticks=5),
                     Client([_m("a", "Will the Fed cut rates?", 0.50)]), SimClock(NOW), books=store, pool=None)
    _book(store, "a", 0.48, 0.52)
    mm.step()
    ts, px = mm.st.markets["a"].bid_yes["placed_ts"], mm.st.markets["a"].bid_yes["price"]
    mm.clock.sleep(120)
    mm._last["select"] = mm.clock.now()
    _book(store, "a", 0.49, 0.53)  # one tick: inside reprice_ticks 5, and no age limit
    mm.step()
    assert mm.st.markets["a"].bid_yes["placed_ts"] == ts and mm.st.markets["a"].bid_yes["price"] == px
    assert "mid" not in mm.st.markets["a"].bid_yes

    aged = BookStore()
    mm2 = MarketMaker("mm_rewards", _cfg(tmp_path / "age", max_quote_age_s=30, reprice_ticks=5),
                      Client([_m("a", "Will the Fed cut rates?", 0.50)]), SimClock(NOW), books=aged, pool=None)
    _book(aged, "a", 0.48, 0.52)
    mm2.step()
    ts = mm2.st.markets["a"].bid_yes["placed_ts"]
    mm2.clock.sleep(10)
    mm2._last["select"] = mm2.clock.now()
    mm2.step()
    assert mm2.st.markets["a"].bid_yes["placed_ts"] == ts
    mm2.clock.sleep(40)
    mm2._last["select"] = mm2.clock.now()
    mm2.step()
    assert mm2.st.markets["a"].bid_yes["placed_ts"] > ts
    assert mm2.scan.get("Gebote wegen Alter erneuert", 0) >= 1

    moved = BookStore()
    mm3 = MarketMaker("mm_rewards", _cfg(tmp_path / "rq", requote_ticks=1, reprice_ticks=5),
                      Client([_m("a", "Will the Fed cut rates?", 0.50)]), SimClock(NOW), books=moved, pool=None)
    _book(moved, "a", 0.48, 0.52)
    mm3.step()
    ts = mm3.st.markets["a"].bid_yes["placed_ts"]
    assert mm3.st.markets["a"].bid_yes.get("mid") == 0.5
    mm3.clock.sleep(5)
    mm3._last["select"] = mm3.clock.now()
    _book(moved, "a", 0.49, 0.53)
    mm3.step()
    assert mm3.st.markets["a"].bid_yes["placed_ts"] > ts and mm3.st.markets["a"].bid_yes["price"] == 0.49


def test_ended_markets_are_not_polled_for_resolution_every_pass(tmp_path):
    calls = []

    class Counting(Client):
        def token_resolution(self, t):
            calls.append(t)
            if t == "boom":
                raise RuntimeError("gamma down")
            return self.res.get(t)

    mm = MarketMaker("mm_rewards", _cfg(tmp_path), Counting([]), SimClock(NOW), books=BookStore(), pool=None)
    mm.store.db.execute(
        "INSERT INTO mm_markout(ts, cid, token, side, price, qty, ytok, days_left) VALUES(?,?,?,?,?,?,?,?)",
        (NOW - 86400, "gone", "goneY", "yes", 0.40, 10, "goneY", 0.0))
    mm.store.db.execute(
        "INSERT INTO mm_markout(ts, cid, token, side, price, qty, ytok, days_left) VALUES(?,?,?,?,?,?,?,?)",
        (NOW - 86400, "err", "boom", "yes", 0.40, 10, "boom", 0.0))
    mm._close_markouts(NOW)
    mm._close_markouts(NOW + 60)
    assert calls.count("goneY") == 1 and calls.count("boom") == 1
    mm._close_markouts(NOW + 601)
    assert calls.count("goneY") == 2 and calls.count("boom") == 2
    mm.store.db.execute(
        "INSERT INTO mm_markout(ts, cid, token, side, price, qty, ytok, days_left) VALUES(?,?,?,?,?,?,?,?)",
        (NOW, "known", "knownY", "no", 0.30, 10, "knownY", 30))
    mm._close_markouts(NOW + 601, "known", 1.0)  # the settle path already knows the price and does not wait
    assert mm.store.db.execute("SELECT m_res FROM mm_markout WHERE cid='known'").fetchone()[0] == round((0 - 0.30) * 10, 4)
    assert calls.count("knownY") == 0


def test_running_scenarios_do_not_pick_up_the_new_filters():
    import yaml
    cfg = yaml.safe_load(open(Path(__file__).resolve().parents[1] / "config.yaml"))
    assert cfg["booklog"]["enabled"] is True and cfg["booklog"]["every_s"] == 60 and cfg["booklog"]["keep_days"] == 7
    sc = cfg["scenarios"]
    for name in ("mm_robust", "mm_konservativ", "mm_robust_quotealter"):
        s = sc[name]
        assert (s["enabled"] is True or name in ("mm_konservativ", "mm_robust_quotealter")) and "live" not in s
        assert s["rewards_active"] is True and s["min_event_volume_24h"] >= 10000
        assert s["min_mid"] <= 0.10 and s["max_mid"] >= 0.90
        assert s["skew"]["long_usd"] > 0 and s["skew"]["fav_frac"] >= 0.5
    assert "max_quote_age_s" not in sc["mm_robust"] and "requote_ticks" not in sc["mm_robust"]
    assert sc["mm_robust_quotealter"]["max_quote_age_s"] == 30 and sc["mm_robust_quotealter"]["requote_ticks"] == 1
    for name, s in sc.items():
        if name in ("mm_robust", "mm_konservativ", "mm_robust_quotealter", "mm_neu_vorsichtig", "mm_neu_vorsichtig2"):
            continue
        assert "skew" not in s and "rewards_active" not in s and "min_event_volume_24h" not in s
        assert "max_quote_age_s" not in s and "requote_ticks" not in s


def test_jump_confirm_counts_only_jumps_that_stay(tmp_path):
    store = BookStore()
    mm = MarketMaker("mm_rewards", _cfg(tmp_path, pause=dict(ticks=3, window_s=300, pause_s=60),
                                        learn=dict(max_jumps=2, block_h=24, jump_confirm_s=300)),
                     Client([_m("a", "Will the Fed cut rates?", 0.50)]), SimClock(NOW), books=store, pool=None)
    # flicker: up 4 ticks and straight back, twice – no jump counts
    for b, a in [(0.48, 0.52), (0.52, 0.56), (0.48, 0.52), (0.48, 0.52), (0.52, 0.56), (0.48, 0.52), (0.48, 0.52)]:
        _book(store, "a", b, a)
        mm.step()
        mm.clock.sleep(120)
    assert not mm.blocked("a", mm.clock.now()) and mm.scan.get("Sprünge nur Flackern", 0) >= 1
    assert not mm._ledger.get("a") or not mm._ledger["a"]["jumps"]
    # a real move that stays: counted after 5 minutes (twice -> blocked)
    for b, a in [(0.52, 0.56), (0.52, 0.56), (0.52, 0.56), (0.56, 0.60), (0.56, 0.60), (0.56, 0.60), (0.56, 0.60)]:
        _book(store, "a", b, a)
        mm.step()
        mm.clock.sleep(120)
    assert mm.blocked("a", mm.clock.now()) and "Kurssprünge" in mm._ledger["a"]["why"]


def test_pull_ticks_keeps_the_bid_out_of_the_way(tmp_path):
    store = BookStore()
    mm = MarketMaker("mm_rewards", _cfg(tmp_path, quote_frac=0.5, pull_ticks=1, pull_if_best=True),
                     Client([_m("a", "Will the Fed cut rates?", 0.50)]), SimClock(NOW), books=store, pool=None)
    _book(store, "a", 0.49, 0.51)  # others bid 0.49; ours at 0.50 − 0.02 = 0.48, behind them
    mm.step()
    mk = mm.st.markets["a"]
    assert mk.bid_yes and mk.bid_yes["price"] == 0.48
    _book(store, "a", 0.46, 0.52)  # the others step back: ours (0.49 − 0.02 = 0.47) would be the best bid → pulled
    mm.clock.sleep(10)
    mm.step()
    assert mk.bid_yes is None and mk.bid_no is None and mm.scan["Gebote gezogen (Fill-Gefahr)"] >= 2
