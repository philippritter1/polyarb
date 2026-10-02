import copy
import json
import math
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arb.client import FeeResolver
from arb.engine import SimClock
from arb.models import Level, OrderBook
from arb.scenarios import ScenarioEngine
from arb.weather import bucket_prob, parse_bucket, parse_title

NOW = 1_790_000_000.0  # 2026-09-21


def ob(tid, bids, asks):
    return OrderBook(tid, [Level(p, s) for p, s in bids], [Level(p, s) for p, s in asks])


# ---------------------------------------------------------------- weather parsing & model
def test_parse_weather_title():
    today = date(2026, 9, 28)
    assert parse_title("Highest temperature in Toronto on September 29?", today) == ("max", "Toronto", date(2026, 9, 29))
    assert parse_title("Lowest temperature in New York City on October 2, 2026?", today) == \
        ("min", "New York City", date(2026, 10, 2))
    assert parse_title("Will it rain in Paris?", today) is None


def test_parse_buckets():
    assert parse_bucket("72-73°F") == (72, 73, "f")
    assert parse_bucket("72–73°F") == (72, 73, "f")
    assert parse_bucket("80°F or higher") == (80, math.inf, "f")
    assert parse_bucket("65°F or below") == (-math.inf, 65, "f")
    assert parse_bucket("21°C") == (21, 21, "c")
    assert parse_bucket("≤16°C") == (-math.inf, 16, "c")
    assert parse_bucket("Other") is None


def test_bucket_probs_sum_to_one():
    members = [70.2, 71.8, 72.4, 73.1, 74.9, 72.0]
    edges = [(-math.inf, 69), (70, 71), (72, 73), (74, 75), (76, math.inf)]
    total = sum(bucket_prob(members, lo, hi, 1.8) for lo, hi in edges)
    assert math.isclose(total, 1.0, abs_tol=1e-9)
    assert bucket_prob(members, 72, 73, 1.8) > bucket_prob(members, 76, math.inf, 1.8)


# ---------------------------------------------------------------- fake Polymarket + Open-Meteo
class FakeClient:
    def __init__(self, markets=(), events=(), books=None, resolutions=None, forecast=None, utc_offset=0):
        self.data = {"/markets": list(markets), "/events": list(events)}
        self.bk = books or {}
        self.res = resolutions or {}
        self.forecast = forecast or {}
        self.utc_offset = utc_offset
        self.fees = FeeResolver({"default_rate": 0.0})

    def paged(self, path, params, max_items=1000):
        return self.data[path]

    def books(self, tids):
        return {t: copy.deepcopy(self.bk[t]) for t in tids if t in self.bk}

    def token_resolution(self, t):
        return self.res.get(t)

    def get_json(self, url, params):
        if "geocoding" in url:
            return {"results": [{"latitude": 43.7, "longitude": -79.4}]}
        return {"daily": self.forecast, "utc_offset_seconds": self.utc_offset}


def _cfg(tmp_path, name, **sc):
    return {"storage": {"db_path": str(tmp_path / "polyarb.sqlite")},
            "portfolio": {"starting_capital_usd": 2500},
            "execution": {"latency_ms": 350, "depth_haircut": 0.5, "respect_market_delay": True},
            "scenarios": {name: dict(dict(enabled=True, strategy=name, capital_usd=500, title=name.title()), **sc)}}


def _market(cid, q, prices, toks, end="2026-09-21T20:00:00Z", **kw):
    return dict(conditionId=cid, question=q, outcomePrices=json.dumps(prices), clobTokenIds=json.dumps(toks),
                outcomes=json.dumps(["Yes", "No"]), endDate=end, enableOrderBook=True, **kw)


def test_endgame_buys_favorite_and_books_payout(tmp_path):
    m = _market("c1", "Will X happen by Sep 21?", ["0.97", "0.03"], ["Y1", "N1"])
    cl = FakeClient(markets=[m], books={"Y1": ob("Y1", [(0.96, 500)], [(0.97, 200)])})
    clock = SimClock(NOW)
    eng = ScenarioEngine("endgame", _cfg(tmp_path, "endgame", max_position_usd=40, max_event_usd=40), cl, clock)
    eng.step()
    pos = eng.pf.positions["Y1"]
    assert math.isclose(pos.cost, 40, abs_tol=0.01) and pos.label == "Yes"
    # no second buy of the same token, even though the book still shows it
    eng.step()
    assert eng.store.db.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 1
    cl.res["Y1"] = 1.0
    clock.t = NOW + 86_400
    eng.step()
    assert not eng.pf.positions and math.isclose(eng.pf.realized_pnl, pos.qty - pos.cost)
    assert math.isclose(eng.pf.equity, 500 + pos.qty - pos.cost)


def test_endgame_loss_costs_the_stake(tmp_path):
    m = _market("c1", "Will X happen?", ["0.96", "0.04"], ["Y1", "N1"])
    cl = FakeClient(markets=[m], books={"Y1": ob("Y1", [(0.95, 500)], [(0.96, 200)])}, resolutions={"Y1": 0.0})
    clock = SimClock(NOW)
    eng = ScenarioEngine("endgame", _cfg(tmp_path, "endgame", max_position_usd=40), cl, clock)
    eng.step()
    clock.t = NOW + 86_400
    eng.step()
    assert math.isclose(eng.pf.realized_pnl, -40, abs_tol=0.01)


def test_longshot_buys_no_on_small_candidates(tmp_path):
    ev = dict(id=7, title="Who wins?", negRisk=True, endDate="2026-10-01T00:00:00Z", markets=[
        dict(groupItemTitle="Big favorite", outcomePrices=json.dumps(["0.80", "0.20"]),
             clobTokenIds=json.dumps(["YA", "NA"]), enableOrderBook=True),
        dict(groupItemTitle="Outsider", outcomePrices=json.dumps(["0.04", "0.96"]),
             clobTokenIds=json.dumps(["YB", "NB"]), enableOrderBook=True)])
    cl = FakeClient(events=[ev], books={"NB": ob("NB", [(0.95, 500)], [(0.96, 500)]),
                                        "NA": ob("NA", [(0.19, 500)], [(0.20, 500)])})
    eng = ScenarioEngine("longshot", _cfg(tmp_path, "longshot", max_position_usd=20), cl, SimClock(NOW))
    eng.step()
    assert list(eng.pf.positions) == ["NB"] and eng.pf.positions["NB"].label == "NO Outsider"


def _weather_event(day=22):
    buckets = ["69°F or below", "70-71°F", "72-73°F", "74-75°F", "76°F or higher"]
    return dict(id=9, title=f"Highest temperature in Toronto on September {day}?", endDate="2026-09-23T04:00:00Z",
                markets=[dict(groupItemTitle=b, clobTokenIds=json.dumps([f"Y{i}", f"N{i}"]), enableOrderBook=True)
                         for i, b in enumerate(buckets)])


def test_weather_buys_underpriced_bucket(tmp_path):
    # 30 members at 72.2-72.8°F, sigma 1°F -> "72-73°F" ~ 0.67, neighbours ~ 0.18; market sells 72-73 at 0.40
    forecast = {"time": ["2026-09-21", "2026-09-22"]}
    for k in range(30):
        forecast[f"temperature_2m_max_member{k:02d}_gfs"] = [60.0, 72.5 + (k % 3 - 1) * 0.3]
    books = {f"Y{i}": ob(f"Y{i}", [(0.01, 100)], [(0.05, 100)]) for i in (0, 4)}
    books.update({f"Y{i}": ob(f"Y{i}", [(0.20, 100)], [(0.22, 100)]) for i in (1, 3)})
    books.update({f"N{i}": ob(f"N{i}", [(0.90, 100)], [(0.95, 100)]) for i in range(5)})
    books["Y2"] = ob("Y2", [(0.38, 300)], [(0.40, 300)])
    books["N2"] = ob("N2", [(0.58, 300)], [(0.60, 300)])
    cl = FakeClient(events=[_weather_event()], books=books, forecast=forecast)
    eng = ScenarioEngine("weather", _cfg(tmp_path, "weather", min_edge=0.08, sigma_f=1.0, max_position_usd=40,
                                          max_event_usd=80, kelly_fraction=0.25), cl, SimClock(NOW))
    eng.step()
    assert list(eng.pf.positions) == ["Y2"]
    p = eng.pf.positions["Y2"]
    assert 0.6 < p.fair < 0.7 and p.label == "YES 72-73°F"
    # Kelly: (0.67 - 0.40) / 0.60 x 0.25 x 500 ~ 56 $ -> capped by max_position_usd
    assert math.isclose(p.cost, 40, abs_tol=0.5)
    note = eng.store.db.execute("SELECT note FROM executions WHERE basket_id='weather:Y2'").fetchone()[0]
    assert "model" in note and "30 members" in note
    assert "N2" not in eng.pf.positions  # NO at 0.60 while the model says ~0.2 -> no edge


def test_dashboard_tabs_and_calibration(tmp_path):
    from dashboard import build_all
    m = _market("c1", "Will X happen?", ["0.97", "0.03"], ["Y1", "N1"])
    cl = FakeClient(markets=[m], books={"Y1": ob("Y1", [(0.96, 500)], [(0.97, 200)])}, resolutions={"Y1": 1.0})
    cfg = _cfg(tmp_path, "endgame", max_position_usd=40)
    clock = SimClock(NOW)
    eng = ScenarioEngine("endgame", cfg, cl, clock)
    eng.step()
    clock.t = NOW + 86_400
    eng.step()
    out = tmp_path / "www" / "index.html"
    built = build_all(cfg, str(out))
    assert Path(built[1]) == tmp_path / "www" / "endgame" / "index.html"
    root = out.read_text(encoding="utf-8")
    page = (tmp_path / "www" / "endgame" / "index.html").read_text(encoding="utf-8")
    data = json.loads(page.split("const D=", 1)[1].split(";\n", 1)[0])
    assert [n["href"] for n in data["nav"]] == ["../", "../endgame/", "../study/", "../wxobs/", "../kalshi/", "../export/"]
    assert data["nav"][1]["active"] and data["nav"][2]["ret"] is None
    assert data["calib"]["n"] == 1 and data["calib"]["wins"] == 1 and data["kind"] == "endgame"
    assert '"href": "endgame/"' in root
    assert (tmp_path / "www" / "endgame" / "trades.csv").exists()


# ---------------------------------------------------------------- logic ladders
def _lm(mid, label, q, toks, end="2026-10-01T00:00:00Z"):
    return dict(id=mid, groupItemTitle=label, question=q, clobTokenIds=json.dumps(toks), endDate=end,
                enableOrderBook=True, acceptingOrders=True)


def _fee(m, cat=""):
    from arb.models import FeeSpec
    return FeeSpec(0.0)


def test_ladder_thresholds_up_and_down():
    from arb.ladder import ladders_from_event
    ev = dict(id=1, title="What price will Bitcoin hit in October?", markets=[
        _lm(1, "↑ 120,000", "Will Bitcoin reach $120,000?", ["Y120", "N120"]),
        _lm(2, "↑ 130,000", "Will Bitcoin reach $130,000?", ["Y130", "N130"]),
        _lm(3, "↓ 100,000", "Will Bitcoin dip to $100,000?", ["Y100", "N100"]),
        _lm(4, "↓ 90,000", "Will Bitcoin dip to $90,000?", ["Y90", "N90"])])
    got = {tuple(b.token_ids) for b in ladders_from_event(ev, _fee)}
    # hitting 130k implies 120k; dipping to 90k implies dipping to 100k
    assert got == {("Y120", "N130"), ("Y100", "N90")}


def test_ladder_deadlines_and_rejects():
    from arb.ladder import ladders_from_event
    ev = dict(id=2, title="Fed cut by ...?", markets=[
        _lm(1, "October", "Will the Fed cut by October 31?", ["YO", "NO_"], end="2026-10-31T00:00:00Z"),
        _lm(2, "December", "Will the Fed cut by December 31?", ["YD", "ND"], end="2026-12-31T00:00:00Z")])
    assert [b.token_ids for b in ladders_from_event(ev, _fee)] == [["YD", "NO_"]]
    buckets = dict(id=3, title="BTC range?", markets=[_lm(1, "100-105k", "above?", ["a", "b"]),
                                                      _lm(2, "105-110k", "above?", ["c", "d"])])
    assert ladders_from_event(buckets, _fee) == []
    assert ladders_from_event(dict(ev, negRisk=True), _fee) == []
    # same threshold ladder but different days: "above 110k on Oct 2" does not imply "above 100k on Oct 1"
    days = dict(id=4, title="BTC above?", markets=[
        _lm(1, "100k", "Will BTC be above $100k on Oct 1?", ["A", "a"], end="2026-10-01T00:00:00Z"),
        _lm(2, "110k", "Will BTC be above $110k on Oct 2?", ["B", "b"], end="2026-10-02T00:00:00Z")])
    assert ladders_from_event(days, _fee) == []


class LadderClient(FakeClient):
    def ladder_baskets(self, min_liq, max_events=300):
        from arb.ladder import ladders_from_event
        return [b for ev in self.data["/events"] for b in ladders_from_event(ev, _fee)]

    def basket_payout(self, token_ids):
        finals = [self.res.get(t) for t in token_ids]
        return None if None in finals else float(sum(finals))


def test_ladder_scenario_trades_and_settles(tmp_path):
    from arb.scenarios import ladder_engine
    ev = dict(id=5, title="Bitcoin above ___ on Oct 1?", markets=[
        _lm(1, "100k", "Will BTC be above $100k on Oct 1?", ["Y100", "N100"]),
        _lm(2, "110k", "Will BTC be above $110k on Oct 1?", ["Y110", "N110"])])
    # market says above 110k (0.55) is likelier than above 100k (0.50): YES 100k 0.50 + NO 110k 0.45 = 0.95
    books = {"Y100": ob("Y100", [(0.49, 400)], [(0.50, 400)]), "N100": ob("N100", [(0.49, 400)], [(0.51, 400)]),
             "Y110": ob("Y110", [(0.54, 400)], [(0.56, 400)]), "N110": ob("N110", [(0.44, 400)], [(0.45, 400)])}
    cl = LadderClient(events=[ev], books=books)
    cfg = _cfg(tmp_path, "ladder", capital_usd=2500)
    cfg.update(portfolio={"starting_capital_usd": 2500}, fees={"merge_gas_usd": 0.0},
               universe={"refresh_minutes": 30, "min_liquidity_usd": 1000, "max_markets": 800},
               scanner={"scan_interval_s": 8, "min_edge_bps": 50, "min_profit_usd": 0.5, "min_annualized_return": 0.0},
               risk=dict(max_trade_pct=0.10, max_market_pct=0.20, max_locked_pct=0.60, max_unhedged_usd=75,
                         daily_loss_limit_pct=0.02, max_consecutive_leg_failures=5, kelly_fraction=0.5,
                         basket_failure_prob=0.01, cash_buffer_pct=0.10))
    cfg["execution"].update(leg_mode="sequential", leg_gap_ms=150)
    clock = SimClock(NOW)
    eng = ladder_engine("ladder", cfg, cl, clock)
    assert eng.store.db.execute("PRAGMA database_list").fetchone()[2].endswith("scenario-ladder.sqlite")
    eng.step()
    locked = eng.broker.pf.locked
    assert len(locked) == 1 and locked[0].basket_id.startswith("ladder:")
    b = locked[0]
    # BTC ends at 105k: above 100k yes, above 110k no -> both legs pay, 2 $ per set
    cl.res.update({"Y100": 1.0, "N110": 1.0})
    clock.t = NOW + 30 * 86_400
    eng.step()
    assert not eng.broker.pf.locked
    row = eng.store.db.execute("SELECT qty, payout, pnl FROM settlements").fetchone()
    assert math.isclose(row[1], 2 * b.qty) and math.isclose(row[2], 2 * b.qty - b.cost)


def test_budget_change_archives_scenario(tmp_path):
    from arb.scenarios import prepare_scenario_dir
    d = tmp_path / "data"
    d.mkdir()
    (d / "scenario-endgame.sqlite").write_text("old")
    (d / "scenario-endgame.json").write_text("{}")
    prepare_scenario_dir(str(d), "endgame", 2500)  # no marker yet -> started with the old budget
    assert not (d / "scenario-endgame.sqlite").exists()
    arch = [p for p in d.iterdir() if p.name.startswith("archive-")]
    assert len(arch) == 1 and (arch[0] / "scenario-endgame.sqlite").read_text() == "old"
    (d / "scenario-endgame.sqlite").write_text("new")
    prepare_scenario_dir(str(d), "endgame", 2500)  # same budget -> untouched
    assert (d / "scenario-endgame.sqlite").read_text() == "new"


def _loss_case_books():
    # what happened on 29.09.: the day is (nearly) over, the market knows the bucket (YES 0.999),
    # the overconfident model puts it at ~1 % -> it wanted NO for up to 0.91 while the NO ask was 0.001
    books = {f"Y{i}": ob(f"Y{i}", [(0.001, 100)], [(0.002, 100)]) for i in range(5)}
    books.update({f"N{i}": ob(f"N{i}", [(0.998, 100)], [(0.999, 100)]) for i in range(5)})
    books["Y4"] = ob("Y4", [(0.998, 500)], [(0.999, 500)])
    books["N4"] = ob("N4", [(0.0, 0)], [(0.001, 20), (0.05, 200), (0.40, 300), (0.90, 500)])
    forecast = {"time": ["2026-09-21", "2026-09-22"]}
    for k in range(30):
        forecast[f"temperature_2m_max_member{k:02d}_gfs"] = [72.0 + (k % 3 - 1) * 0.3] * 2
    return books, forecast


def test_weather_skips_day_already_started_in_city(tmp_path):
    books, forecast = _loss_case_books()
    cl = FakeClient(events=[_weather_event(day=21)], books=books, forecast=forecast, utc_offset=8 * 3600)
    eng = ScenarioEngine("weather", _cfg(tmp_path, "weather", sigma_f=1.0, min_edge=0.08), cl, SimClock(NOW))
    eng.step()
    assert not eng.pf.positions and eng.strategy.skipped.get("started") == 1


def test_weather_guard_when_market_knows_better(tmp_path):
    books, forecast = _loss_case_books()
    cl = FakeClient(events=[_weather_event(day=22)], books=books, forecast=forecast)
    eng = ScenarioEngine("weather", _cfg(tmp_path, "weather", sigma_f=1.0, min_edge=0.08), cl, SimClock(NOW))
    eng.step()
    assert not eng.pf.positions and eng.guarded >= 1  # NO at 0.001 vs model 0.99 -> not traded


def test_scenario_never_walks_far_up_the_book(tmp_path):
    m = _market("c1", "Will X happen?", ["0.95", "0.05"], ["Y1", "N1"])
    cl = FakeClient(markets=[m], books={"Y1": ob("Y1", [(0.94, 500)], [(0.95, 10), (0.97, 50), (0.99, 500)])})
    eng = ScenarioEngine("endgame", _cfg(tmp_path, "endgame", max_position_usd=200, max_slippage=0.03), cl,
                         SimClock(NOW))
    eng.step()
    p = eng.pf.positions["Y1"]
    # haircut 0.5: 5 @ 0.95 + 25 @ 0.97; the 0.99 level is more than 3 cents above the best ask
    assert math.isclose(p.qty, 30) and p.cost / p.qty < 0.97


def test_reset_value_restarts_scenario(tmp_path):
    from arb.scenarios import prepare_scenario_dir
    d = tmp_path / "data"
    d.mkdir()
    prepare_scenario_dir(str(d), "weather", 2500)
    (d / "scenario-weather.sqlite").write_text("v1")
    prepare_scenario_dir(str(d), "weather", 2500, "2")
    assert not (d / "scenario-weather.sqlite").exists()
    assert (d / "scenario-weather.capital").read_text() == "2500|2"


# ---------------------------------------------------------------- endgame v2 (after 29.09.)
def test_endgame_enforces_min_price_and_finished_games(tmp_path):
    cheap = _market("c1", "Exact Score: A 2 - 0 B?", ["0.07", "0.93"], ["Y1", "N1"])       # ask 0.934 < 0.95
    pregame = _market("c2", "Will A win?", ["0.96", "0.04"], ["Y2", "N2"],
                      gameStartTime="2026-09-21T13:00:00Z")                               # kick-off 33 min ago
    done = _market("c3", "Will C win?", ["0.97", "0.03"], ["Y3", "N3"], gameStartTime="2026-09-21T08:00:00Z")
    cl = FakeClient(markets=[cheap, pregame, done],
                    books={"N1": ob("N1", [(0.93, 500)], [(0.934, 500)]), "Y2": ob("Y2", [(0.95, 500)], [(0.96, 500)]),
                           "Y3": ob("Y3", [(0.96, 500)], [(0.97, 500)])})
    eng = ScenarioEngine("endgame", _cfg(tmp_path, "endgame", max_position_usd=100), cl, SimClock(NOW))
    eng.step()
    assert list(eng.pf.positions) == ["Y3"]


def test_endgame_spreads_entries_and_caps_per_event(tmp_path):
    ms = [_market(f"c{i}", f"Exact Score {i}?", ["0.03", "0.97"], [f"Y{i}", f"N{i}"], events=[{"id": 77}])
          for i in range(3)]
    ms += [_market(f"d{i}", f"Other {i}?", ["0.97", "0.03"], [f"A{i}", f"B{i}"]) for i in range(4)]
    books = {f"N{i}": ob(f"N{i}", [(0.96, 500)], [(0.97, 500)]) for i in range(3)}
    books.update({f"A{i}": ob(f"A{i}", [(0.96, 500)], [(0.97 + i * 0.005, 500)]) for i in range(4)})
    cl = FakeClient(markets=ms, books=books)
    eng = ScenarioEngine("endgame", _cfg(tmp_path, "endgame", max_position_usd=100, max_event_usd=100,
                                          max_new_per_step=2), cl, SimClock(NOW))
    eng.step()
    assert len(eng.pf.positions) == 2
    assert {"A3", "A2"} == set(eng.pf.positions)  # safest (highest price) first
    for _ in range(5):
        eng.step()
    event_77 = [t for t in eng.pf.positions if t.startswith("N")]
    assert len(event_77) == 1  # one match = one 100 $ stake, not three


def test_underdog_buys_real_ask_and_skips_wide_spreads(tmp_path):
    kw = dict(gameStartTime="2026-09-21T16:00:00Z", end="2026-09-21T18:00:00Z")  # ends in ~4.5 h
    ok = _market("s1", "Lakers vs. Celtics", ["0.94", "0.06"], ["L1", "C1"], **kw)
    wide = _market("s2", "Knicks vs. Bulls", ["0.93", "0.07"], ["K2", "B2"], **kw)
    books = {"C1": ob("C1", [(0.05, 500)], [(0.06, 500)]),     # 1 cent spread -> buy
             "B2": ob("B2", [(0.03, 500)], [(0.08, 500)]),     # 5 cent spread -> the edge is gone
             "L1": ob("L1", [(0.93, 500)], [(0.94, 500)]), "K2": ob("K2", [(0.92, 500)], [(0.93, 500)])}
    cl = FakeClient(markets=[ok, wide], books=books)
    eng = ScenarioEngine("underdog", _cfg(tmp_path, "underdog", max_position_usd=10, max_spread=0.03,
                                          max_slippage=0.01), cl, SimClock(NOW))
    eng.step()
    assert list(eng.pf.positions) == ["C1"]
    p = eng.pf.positions["C1"]
    assert math.isclose(p.cost, 10, abs_tol=0.05) and p.cost / p.qty <= 0.061
    assert eng.guarded == 1


def test_notifications_follow_the_configured_scenario(tmp_path):
    import os
    import subprocess
    kw = dict(gameStartTime="2026-09-21T16:00:00Z", end="2026-09-21T18:00:00Z")
    m = _market("s1", "Lakers vs. Celtics", ["0.94", "0.06"], ["L1", "C1"], **kw)
    cl = FakeClient(markets=[m], books={"C1": ob("C1", [(0.05, 500)], [(0.06, 500)]),
                                        "L1": ob("L1", [(0.93, 500)], [(0.94, 500)])}, resolutions={"C1": 1.0})
    clock = SimClock(NOW)
    eng = ScenarioEngine("underdog", _cfg(tmp_path, "underdog", max_position_usd=10), cl, clock)
    eng.step()
    clock.t = NOW + 86_400
    eng.step()
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"last_exec_ts": 0, "last_settle_ts": 0, "boot": 0}))
    env = dict(os.environ, NTFY_TOPIC="", POLYARB_DB=str(tmp_path / "data" / "scenario-underdog.sqlite")
               if (tmp_path / "data").exists() else str(tmp_path / "scenario-underdog.sqlite"),
               NOTIFY_STATE=str(state), START_CAPITAL="500")
    root = Path(__file__).resolve().parents[1]
    import yaml
    conf = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    label = conf["scenarios"][conf["notify"]["scenario"]]["title"]  # the book the alerts follow
    # since 01.10. single trade/payout pushes are off by default: only problems and the 4-hour report
    out = subprocess.run([sys.executable, "deploy/notify.py", "watch"], cwd=root, env=env,
                         capture_output=True, text=True).stdout
    assert "Trade" not in out and "Auszahlung" not in out
    state.write_text(json.dumps({"last_exec_ts": 0, "last_settle_ts": 0, "boot": 0}))
    out = subprocess.run([sys.executable, "deploy/notify.py", "watch"], cwd=root,
                         env=dict(env, NOTIFY_TRADES="1", NOTIFY_PAYOUTS="1"), capture_output=True, text=True).stdout
    assert f"Polyarb {label}: Trade" in out and "Stk. für $10" in out
    assert f"Polyarb {label}: Auszahlung" in out and "1 gewonnen, 0 verloren" in out
    pnl = eng.pf.realized_pnl
    rep = subprocess.run([sys.executable, "deploy/notify.py", "report", "4"], cwd=root, env=env,
                         capture_output=True, text=True).stdout
    assert pnl > 0 and "Polyarb 4h-Bericht" in rep  # the simulated trades lie outside the last 4 h ...
    assert f"Gesamt realisiert ${pnl:,.2f} ({pnl / 500 * 100:+.2f} % auf $500.00)" in rep
    assert f"▲ {label}: ${pnl:,.2f} (+{pnl / 500 * 100:.2f} %) | 4h $0.00" in rep
    rep = subprocess.run([sys.executable, "deploy/notify.py", "report", "100000"], cwd=root, env=env,
                         capture_output=True, text=True).stdout
    assert f"| 100000h ${pnl:,.2f} · 1✓ 0✗" in rep  # ... but inside a long window


def test_sport_band_filters_on_volume_at_purchase(tmp_path):
    kw = dict(gameStartTime="2026-09-21T16:00:00Z", end="2026-09-21T18:00:00Z")
    big = _market("s1", "Lakers vs. Celtics", ["0.94", "0.06"], ["L1", "C1"], volumeNum=12000, **kw)
    small = _market("s2", "Knicks vs. Bulls", ["0.94", "0.06"], ["K2", "B2"], volumeNum=2500, **kw)
    books = {"C1": ob("C1", [(0.05, 500)], [(0.06, 500)]), "B2": ob("B2", [(0.05, 500)], [(0.06, 500)]),
             "L1": ob("L1", [(0.93, 500)], [(0.94, 500)]), "K2": ob("K2", [(0.93, 500)], [(0.94, 500)])}
    under = ScenarioEngine("underdog", _cfg(tmp_path / "u", "underdog", min_volume=5000, max_position_usd=10),
                           FakeClient(markets=[big, small], books=books), SimClock(NOW))
    under.step()
    assert list(under.pf.positions) == ["C1"]  # only the underdog of the 12k market
    fav = ScenarioEngine("favorite", _cfg(tmp_path / "f", "favorite", min_price=0.90, max_price=0.97,
                                          min_volume=1000, max_volume=5000, max_spread=0.02,
                                          max_position_usd=25), FakeClient(markets=[big, small], books=books),
                         SimClock(NOW))
    fav.step()
    assert list(fav.pf.positions) == ["K2"]  # only the favourite of the 2.5k market
    assert fav.store.db.execute("SELECT strategy FROM executions").fetchone()[0] == "favorite_buy"


def test_weather_no_band_buys_the_no_side_of_cheap_buckets(tmp_path):
    kw = dict(gameStartTime="2026-09-21T00:00:00Z", end="2026-09-22T04:00:00Z", volumeNum=1800)
    wx = _market("w1", "Will the highest temperature in Paris be 24°C on September 21?", ["0.06", "0.94"],
                 ["Y24", "N24"], **kw)
    game = _market("g1", "Lakers vs. Celtics", ["0.06", "0.94"], ["LY", "LN"], **kw)
    books = {"N24": ob("N24", [(0.94, 500)], [(0.95, 500)]), "Y24": ob("Y24", [(0.05, 500)], [(0.06, 500)]),
             "LN": ob("LN", [(0.94, 500)], [(0.95, 500)]), "LY": ob("LY", [(0.05, 500)], [(0.06, 500)])}
    cfg = _cfg(tmp_path, "weather_no", strategy="band", category="Wetter", min_price=0.90, max_price=0.97,
               max_volume=5000, max_hours_to_end=30, max_spread=0.02, max_position_usd=25)
    eng = ScenarioEngine("weather_no", cfg, FakeClient(markets=[wx, game], books=books), SimClock(NOW))
    eng.step()
    assert list(eng.pf.positions) == ["N24"]  # the weather NO, not the sport favourite
    # and the underdog scenario no longer buys temperature buckets
    under = ScenarioEngine("underdog", _cfg(tmp_path / "u", "underdog", min_volume=1000, max_position_usd=10,
                                          max_hours_to_end=40),
                           FakeClient(markets=[wx, game], books=books), SimClock(NOW))
    under.step()
    assert list(under.pf.positions) == ["LY"]


def test_band_filters_kind_type_and_outcome(tmp_path):
    kw = dict(gameStartTime="2026-09-21T16:00:00Z", end="2026-09-21T18:00:00Z", volumeNum=5000)
    ou = _market("f1", "AS Roma vs. FC Barcelona: O/U 3.5", ["0.20", "0.80"], ["OV", "UN"], **kw)
    win = _market("f2", "Will AS Roma win on 2026-09-21?", ["0.20", "0.80"], ["RW", "RN"], **kw)
    esp = _market("e1", "Counter-Strike: Leo Team vs mellren - Map 4 Winner", ["0.20", "0.80"], ["LT", "ML"], **kw)
    books = {t: ob(t, [(p - 0.01, 500)], [(p, 500)]) for t, p in
             (("OV", 0.20), ("UN", 0.80), ("RW", 0.20), ("RN", 0.80), ("LT", 0.20), ("ML", 0.80))}
    cfg = _cfg(tmp_path, "fussball_dog", strategy="band", category="Sport", kinds=["Fussball"],
               types=["Ueber/Unter", "Spread", "Remis", "Halbzeit"], min_price=0.03, max_price=0.25,
               min_volume=1000, max_position_usd=10)
    eng = ScenarioEngine("fussball_dog", cfg, FakeClient(markets=[ou, win, esp], books=books), SimClock(NOW))
    eng.step()
    assert list(eng.pf.positions) == ["OV"]  # not the "who wins" market, not esports
    assert eng.store.db.execute("SELECT strategy FROM executions").fetchone()[0] == "fussball_dog_buy"
    # weather: only the NO side, even when the YES side is in the band too
    wkw = dict(end="2026-09-21T20:00:00Z", volumeNum=1800)
    a = _market("w1", "Will the highest temperature in Paris be 24°C on September 21?", ["0.40", "0.60"],
                ["Y24", "N24"], **wkw)
    b = _market("w2", "Will the highest temperature in Paris be 25°C on September 21?", ["0.60", "0.40"],
                ["Y25", "N25"], **wkw)
    wbooks = {t: ob(t, [(p - 0.01, 500)], [(p, 500)]) for t, p in
              (("Y24", 0.40), ("N24", 0.60), ("Y25", 0.60), ("N25", 0.40))}
    wcfg = _cfg(tmp_path / "w", "wetter_no_breit", strategy="band", category="Wetter", outcome="No",
                min_price=0.55, max_price=0.97, max_volume=5000, max_hours_to_end=12, max_spread=0.02,
                max_position_usd=25)
    weng = ScenarioEngine("wetter_no_breit", wcfg, FakeClient(markets=[a, b], books=wbooks), SimClock(NOW))
    weng.step()
    assert list(weng.pf.positions) == ["N24"]
    # finance: quarterly earnings are skipped
    fkw = dict(gameStartTime="2026-09-21T16:00:00Z", end="2026-09-21T20:00:00Z", volumeNum=5000)
    px = _market("p1", "WTI Crude Oil (WTI) closes above $94 on September 21?", ["0.15", "0.85"], ["WY", "WN"], **fkw)
    er = _market("p2", "Will Robinhood (HOOD) beat quarterly earnings?", ["0.15", "0.85"], ["HY", "HN"], **fkw)
    fbooks = {t: ob(t, [(p - 0.01, 500)], [(p, 500)]) for t, p in
              (("WY", 0.15), ("WN", 0.85), ("HY", 0.15), ("HN", 0.85))}
    fcfg = _cfg(tmp_path / "f", "finanz_dog", strategy="band", category="Finanz", exclude_types=["Quartalszahlen"],
                min_price=0.10, max_price=0.25, min_volume=1000, max_hours_to_end=24, max_position_usd=10)
    feng = ScenarioEngine("finanz_dog", fcfg, FakeClient(markets=[px, er], books=fbooks), SimClock(NOW))
    feng.step()
    assert list(feng.pf.positions) == ["WY"]


def test_partial_fill_logs_what_was_really_spent(tmp_path):
    kw = dict(gameStartTime="2026-09-21T16:00:00Z", end="2026-09-21T18:00:00Z")
    m = _market("s1", "Lakers vs. Celtics", ["0.94", "0.06"], ["L1", "C1"], **kw)
    books = {"C1": ob("C1", [(0.05, 500)], [(0.06, 500)]), "L1": ob("L1", [(0.93, 500)], [(0.94, 500)])}

    class Thinning(FakeClient):  # the book loses depth between sizing and the order
        n = 0

        def books(self, tids):
            out = super().books(tids)
            if "C1" in out and "C1" in tids and len(tids) == 1:
                Thinning.n += 1
                if Thinning.n > 1:
                    out["C1"] = ob("C1", [(0.05, 500)], [(0.06, 40)])
            return out

    eng = ScenarioEngine("underdog", _cfg(tmp_path, "underdog", max_position_usd=10, confirm_with_rest=False),
                         Thinning(markets=[m], books=books), SimClock(NOW))
    eng.step()
    status, capital, locked, qty = eng.store.db.execute(
        "SELECT status, capital, locked, matched_qty FROM executions").fetchone()
    assert status == "partial" and qty < 100
    assert math.isclose(capital, locked) and math.isclose(capital, eng.pf.positions["C1"].cost)


def test_max_day_usd_caps_one_end_date(tmp_path):
    kw = dict(end="2026-09-21T20:00:00Z", volumeNum=1800)
    ms = [_market(f"w{i}", f"Will the highest temperature in City{i} be 24°C on September 21?", ["0.20", "0.80"],
                  [f"Y{i}", f"N{i}"], **kw) for i in range(6)]
    books = {}
    for i in range(6):
        books[f"N{i}"] = ob(f"N{i}", [(0.79, 500)], [(0.80, 500)])
        books[f"Y{i}"] = ob(f"Y{i}", [(0.19, 500)], [(0.20, 500)])
    cfg = _cfg(tmp_path, "wetter_no_breit", strategy="band", category="Wetter", outcome="No", min_price=0.55,
               max_price=0.97, max_volume=5000, max_hours_to_end=12, max_spread=0.02, max_position_usd=25,
               max_day_usd=60, max_new_per_step=10)
    eng = ScenarioEngine("wetter_no_breit", cfg, FakeClient(markets=ms, books=books), SimClock(NOW))
    eng.step()
    spent = sum(p.cost for p in eng.pf.positions.values())
    assert len(eng.pf.positions) == 3 and spent <= 60 + 1e-6  # 25 + 25 + the 10 that is left for the day
    reasons = [r[0] for r in eng.store.db.execute("SELECT reason FROM opportunities")]
    assert "sized down by day" in reasons and "no capacity (day)" in reasons


def test_band_scan_stats_and_topic_tags(tmp_path):
    fkw = dict(end="2026-09-21T20:00:00Z", volumeNum=5000)
    btc = _market("k1", "Bitcoin Up or Down - September 21, 4:00PM-4:05PM ET", ["0.50", "0.50"], ["BU", "BD"], **fkw)
    wti = _market("p1", "WTI Crude Oil (WTI) closes above $94 on September 21?", ["0.15", "0.85"], ["WY", "WN"], **fkw)
    cl = FakeClient(markets=[btc], events=[{"id": 7, "endDate": "2026-09-21T20:00:00Z", "markets": [wti]}],
                    books={t: ob(t, [(p - 0.01, 500)], [(p, 500)]) for t, p in (("WY", 0.15), ("WN", 0.85))})
    cfg = _cfg(tmp_path, "finanz_dog", strategy="band", category="Finanz", min_price=0.10, max_price=0.25,
               min_volume=1000, max_hours_to_end=24, max_position_usd=10, max_markets=1, tag_slugs=["finance"])
    eng = ScenarioEngine("finanz_dog", cfg, cl, SimClock(NOW))
    eng.step()
    assert list(eng.pf.positions) == ["WY"]  # found through the topic tag although the listing was full
    scan = json.loads(Path(eng.state_path.replace(".json", ".scan.json")).read_text())
    assert scan["Liste voll"] == 1 and scan["Tag finance"] == 1 and scan["andere Kategorie"] == 1
    assert scan["Seite im Preisband"] == 1 and scan["neu gekauft"] == 1


def test_capacity_counts_what_the_books_offered(tmp_path):
    from dashboard import capacity_rows
    kw = dict(end="2026-09-21T20:00:00Z", volumeNum=1800)
    ms = [_market(f"w{i}", f"Will the highest temperature in City{i} be 24°C on September 21?", ["0.20", "0.80"],
                  [f"Y{i}", f"N{i}"], **kw) for i in range(6)]
    books = {}
    for i in range(6):  # 500 shares at 0.80 and 1000 more at 0.83 (beyond the 1-cent buy limit)
        books[f"N{i}"] = ob(f"N{i}", [(0.79, 500)], [(0.80, 500), (0.83, 1000)])
        books[f"Y{i}"] = ob(f"Y{i}", [(0.19, 500)], [(0.20, 500)])
    cfg = _cfg(tmp_path, "wetter_no_breit", strategy="band", category="Wetter", outcome="No", min_price=0.55,
               max_price=0.97, max_volume=5000, max_hours_to_end=12, max_spread=0.02, max_position_usd=25,
               max_day_usd=60, max_new_per_step=10, max_slippage=0.01)
    eng = ScenarioEngine("wetter_no_breit", cfg, FakeClient(markets=ms, books=books), SimClock(NOW))
    eng.step()
    eng.step()  # a second scan of the same markets does not count them twice
    assert len(eng.pf.positions) == 3
    day = capacity_rows(str(tmp_path / "scenario-wetter_no_breit.sqlite"))[0]
    assert day["day"] == "2026-09-21" and day["n"] == 6  # also the three the day limit stopped
    assert math.isclose(day["slip"], 6 * 400) and math.isclose(day["c2"], 6 * 400)
    assert math.isclose(day["band"], 6 * (400 + 830)) and 55 < day["bought"] <= 60 + 1e-6


def test_weather_no_skips_buckets_the_station_already_reached(tmp_path):
    """Paris, day 2026-09-21 (UTC+2): the station reported 24 °C this morning. NO on '24 °C' is skipped
    (the max sits in the bucket), NO on '27 °C' is bought (3 buckets above)."""
    desc = "Resolution: https://www.weather.gov/wrh/timeseries?site=lfpb"
    kw = dict(end="2026-09-21T20:00:00Z", volumeNum=1800, description=desc)
    a = _market("w1", "Will the highest temperature in Paris be 24°C on September 21?", ["0.30", "0.70"], ["Y24", "N24"], **kw)
    b = _market("w2", "Will the highest temperature in Paris be 27°C on September 21?", ["0.30", "0.70"], ["Y27", "N27"], **kw)
    books = {t: ob(t, [(p - 0.01, 500)], [(p, 500)]) for t, p in
             (("Y24", 0.30), ("N24", 0.70), ("Y27", 0.30), ("N27", 0.70))}

    class Obs(FakeClient):
        def get_json(self, url, params):
            if "stationinfo" in url:
                return [{"icaoId": "LFPB", "lat": 48.97, "lon": 2.44}]
            if "open-meteo.com/v1/forecast" in url:
                return {"timezone": "Europe/Paris"}
            if "metar" in url:
                assert params["ids"] == "LFPB"
                return [{"obsTime": int(NOW) - 3600 * k, "temp": t} for k, t in ((1, 24.0), (3, 21.0), (40, 30.0))]
            return super().get_json(url, params)

    cfg = _cfg(tmp_path, "wetter_no_mess", strategy="band", category="Wetter", outcome="No", min_price=0.55,
               max_price=0.97, max_volume=5000, max_hours_to_end=12, max_spread=0.02, max_position_usd=25,
               obs_skip=["Maximum liegt im Bucket"])
    eng = ScenarioEngine("wetter_no_mess", cfg, Obs(markets=[a, b], books=books), SimClock(NOW))
    eng.step()
    assert list(eng.pf.positions) == ["N27"]
    scan = json.loads(Path(eng.state_path.replace(".json", ".scan.json")).read_text())
    assert scan["Station: Maximum liegt im Bucket (ausgelassen)"] == 1 and scan["Station: 3–4 Buckets darunter"] == 1
    note = eng.store.db.execute("SELECT note FROM executions").fetchone()[0]
    assert "3–4 Buckets darunter" in note


def test_strict_weather_no_needs_a_reading(tmp_path):
    kw = dict(end="2026-09-21T20:00:00Z", volumeNum=1800, description="no station link here")
    m = _market("w1", "Will the highest temperature in Paris be 27°C on September 21?", ["0.30", "0.70"], ["Y27", "N27"], **kw)
    books = {"Y27": ob("Y27", [(0.29, 500)], [(0.30, 500)]), "N27": ob("N27", [(0.69, 500)], [(0.70, 500)])}
    for skip, bought in ((["Maximum liegt im Bucket"], ["N27"]),
                         (["Maximum liegt im Bucket", "1 Bucket darunter", "keine Messung"], [])):
        cfg = _cfg(tmp_path / str(len(skip)), "wetter_no_streng", strategy="band", category="Wetter", outcome="No",
                   min_price=0.55, max_price=0.97, max_volume=5000, max_hours_to_end=12, max_spread=0.02,
                   max_position_usd=50, obs_skip=skip)
        eng = ScenarioEngine("wetter_no_streng", cfg, FakeClient(markets=[m], books=books), SimClock(NOW))
        eng.step()
        assert list(eng.pf.positions) == bought
