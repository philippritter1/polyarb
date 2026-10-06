import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arb.client import FeeResolver
from arb.engine import SimClock
from arb.models import Level, OrderBook
from arb.scenarios import ScenarioEngine
from arb.sharp import ODDS_BASE, OddsFeed, SharpStrategy, link, parse_match

NOW = 1_790_000_000.0          # 2026-09-21 14:13 UTC
KICK = "2026-09-21T18:00:00Z"   # kick-off a few hours later


def _event(home="Arsenal", away="Chelsea", odds=(2.0, 3.6, 4.0), when=KICK):
    return {"id": "e1", "home_team": home, "away_team": away, "commence_time": when,
            "bookmakers": [{"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
                {"name": home, "price": odds[0]}, {"name": "Draw", "price": odds[1]}, {"name": away, "price": odds[2]}]}]}]}


class Client:
    def __init__(self, pm_yes=0.40, ask=0.41):
        self.calls = []
        self.fees = FeeResolver({"default_rate": 0.0})
        m = dict(question="Will Arsenal win on 2026-09-21?", clobTokenIds=json.dumps(["AY", "AN"]),
                 outcomePrices=json.dumps([str(pm_yes), str(round(1 - pm_yes, 3))]), enableOrderBook=True,
                 endDate="2026-09-21T20:00:00Z")
        self.pm = [{"id": "ev1", "endDate": "2026-09-21T20:00:00Z", "markets": [m]}]
        self.bk = {"AY": OrderBook("AY", [Level(ask - 0.01, 500)], [Level(ask, 500)]),
                   "AN": OrderBook("AN", [Level(round(1 - ask - 0.01, 3), 500)], [Level(round(1 - ask + 0.01, 3), 500)])}

    def get_json(self, url, params):
        self.calls.append(url)
        if url.endswith("/events"):
            return [{"commence_time": KICK}]
        if url.endswith("/odds"):
            return [_event()]
        raise AssertionError(url)

    def paged(self, path, params, max_items=1000):
        return self.pm if path == "/events" else []

    def books(self, tids):
        return {t: self.bk[t] for t in tids if t in self.bk}

    def token_resolution(self, t):
        return None


def test_parse_and_link():
    m = parse_match(_event(), "soccer_epl")
    assert abs(m["p_home"] + m["p_draw"] + m["p_away"] - 1) < 1e-9 and m["p_home"] > 0.45
    mt, p = link("Will Arsenal FC win on 2026-09-21?", None, [m])
    assert p == m["p_home"]
    assert link("Will Chelsea FC vs. Arsenal end in a draw?", None, [m]) is None  # home/away the other way round
    assert link("Will Arsenal vs. Chelsea end in a draw?", None, [m])[1] == m["p_draw"]
    assert link("Will Arsenal Women win on 2026-09-21?", None, [m]) is None
    assert link("Will Arsenal win on 2026-09-25?", None, [m]) is None  # other day


def test_feed_spends_credits_only_when_a_match_is_near_and_respects_the_daily_budget(tmp_path):
    cl = Client()
    f = OddsFeed(cl.get_json, "k", str(tmp_path / "o.json"), ["soccer_epl", "soccer_germany_bundesliga"],
                 monthly_cap=30, refresh_h=4, lookahead_h=8)  # 30 a month -> 1 a day
    ms = f.matches(NOW)
    assert len(ms) == 1 and f.used(NOW) == 1  # second league: no credit left today
    assert f.stats["Credit-Budget erreicht"] == 1
    cl.calls.clear()
    f.matches(NOW + 600)  # cached: no new odds call, schedule not due either
    assert not any(c.endswith("/odds") for c in cl.calls)
    again = OddsFeed(cl.get_json, "k", str(tmp_path / "o.json"), ["soccer_epl"], monthly_cap=30)
    assert again.used(NOW) == 1  # the budget survives a restart


def test_scenario_buys_when_polymarket_is_cheaper_than_pinnacle(tmp_path, monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "test")
    cfg = {"storage": {"db_path": str(tmp_path / "polyarb.sqlite")},
           "execution": {"latency_ms": 0, "depth_haircut": 1.0, "respect_market_delay": False},
           "scenarios": {"sharp": dict(strategy="sharp", capital_usd=500, min_edge=0.05, max_position_usd=20,
                                       leagues=["soccer_epl"])}}
    # Pinnacle: Arsenal 0.486; Polymarket ask 0.41 -> 7.6 points cheaper: buy YES up to 0.436
    eng = ScenarioEngine("sharp", cfg, Client(pm_yes=0.40, ask=0.41), SimClock(NOW))
    eng.step()
    assert list(eng.pf.positions) == ["AY"]
    note = eng.store.db.execute("SELECT note FROM executions").fetchone()[0]
    assert "Pinnacle 0.486" in note and "Arsenal – Chelsea" in note
    # no gap -> nothing
    eng2 = ScenarioEngine("sharp", dict(cfg, storage={"db_path": str(tmp_path / "b" / "polyarb.sqlite")}),
                          Client(pm_yes=0.48, ask=0.49), SimClock(NOW))
    eng2.step()
    assert not eng2.pf.positions


def test_without_key_nothing_is_called(tmp_path, monkeypatch):
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    cl = Client()
    s = SharpStrategy({"odds_state_path": str(tmp_path / "o.json")}, cl)
    assert s.candidates(NOW) == [] and not cl.calls and "kein ODDS_API_KEY" in next(iter(s.scan))
