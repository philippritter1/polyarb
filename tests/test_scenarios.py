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
    def __init__(self, markets=(), events=(), books=None, resolutions=None, forecast=None):
        self.data = {"/markets": list(markets), "/events": list(events)}
        self.bk = books or {}
        self.res = resolutions or {}
        self.forecast = forecast or {}
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
        return {"daily": self.forecast}


def _cfg(tmp_path, name, **sc):
    return {"storage": {"db_path": str(tmp_path / "polyarb.sqlite")},
            "portfolio": {"starting_capital_usd": 2500},
            "execution": {"latency_ms": 350, "depth_haircut": 0.5, "respect_market_delay": True},
            "scenarios": {name: dict(enabled=True, strategy=name, capital_usd=500, title=name.title(), **sc)}}


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


def _weather_event():
    buckets = ["69°F or below", "70-71°F", "72-73°F", "74-75°F", "76°F or higher"]
    return dict(id=9, title="Highest temperature in Toronto on September 21?", endDate="2026-09-22T04:00:00Z",
                markets=[dict(groupItemTitle=b, clobTokenIds=json.dumps([f"Y{i}", f"N{i}"]), enableOrderBook=True)
                         for i, b in enumerate(buckets)])


def test_weather_buys_underpriced_bucket(tmp_path):
    # 30 members at 72.2-72.8°F, sigma 1°F -> "72-73°F" ~ 0.67, neighbours ~ 0.18; market sells 72-73 at 0.40
    forecast = {"time": ["2026-09-21", "2026-09-22"]}
    for k in range(30):
        forecast[f"temperature_2m_max_member{k:02d}_gfs"] = [72.5 + (k % 3 - 1) * 0.3, 60.0]
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
    assert [n["href"] for n in data["nav"]] == ["../", "../endgame/"] and data["nav"][1]["active"]
    assert data["calib"]["n"] == 1 and data["calib"]["wins"] == 1 and data["kind"] == "endgame"
    assert '"href": "endgame/"' in root
    assert (tmp_path / "www" / "endgame" / "trades.csv").exists()
