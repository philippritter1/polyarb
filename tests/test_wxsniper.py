"""Station-ruled-out weather sniper: log every case, paper-buy NO only at a real ask."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arb.engine import SimClock
from arb.scenarios import ScenarioEngine
from tests.test_scenarios import NOW, FakeClient, _cfg, ob

DESC = ("Resolves on the highest temperature at Toronto Pearson. "
        "https://www.wunderground.com/history/daily/ca/toronto/CYYZ")


class WxClient(FakeClient):
    def get_json(self, url, params):
        if "stationinfo" in url:
            return [{"lat": 43.68, "lon": -79.63}]
        if "open-meteo.com/v1/forecast" in url:
            return {"timezone": "America/Toronto"}
        if "metar" in url:
            return [{"obsTime": NOW - 7200, "temp": 22}]  # 22°C, already published
        return super().get_json(url, params)


def _bucket(cid, label, yes, toks, question=""):
    return dict(conditionId=cid, groupItemTitle=label, question=question,
                outcomePrices=json.dumps([str(yes), str(round(1 - yes, 4))]),
                clobTokenIds=json.dumps(toks), outcomes=json.dumps(["Yes", "No"]),
                enableOrderBook=True, description=DESC, endDate="2026-09-22T03:00:00Z")


def test_sniper_logs_ruled_out_buckets_and_buys_no_only_at_the_ask(tmp_path):
    ev = dict(id="tor", title="Highest temperature in Toronto on September 21?", description=DESC,
              endDate="2026-09-22T03:00:00Z", markets=[
                  _bucket("hot", "18°C", 0.08, ["Yhot", "Nhot"],
                          "Will the highest temperature in Toronto be 18°C on September 21?"),
                  _bucket("inb", "22°C", 0.40, ["Yin", "Nin"],
                          "Will the highest temperature in Toronto be 22°C on September 21?"),
                  _bucket("cheap", "16°C or below", 0.01, ["Yc", "Nc"],
                          "Will the highest temperature in Toronto be 16°C or below on September 21?"),
                  _bucket("thin", "15°C", 0.10, ["Yt", "Nt"],
                          "Will the highest temperature in Toronto be 15°C on September 21?"),
              ])
    books = {"Nhot": ob("Nhot", [(0.90, 80)], [(0.91, 80)]),
             "Nc": ob("Nc", [(0.96, 40)], [(0.97, 40)])}
    cl = WxClient(events=[ev], books=books, resolutions={"Nhot": 1.0})
    clock = SimClock(NOW)
    eng = ScenarioEngine("wetter_sniper", _cfg(
        tmp_path, "wetter_sniper", strategy="wxsniper", capital_usd=500, max_position_usd=25,
        max_event_usd=75, max_slippage=0, min_margin=1, min_yes=0.03, obs_delay_min=15), cl, clock)
    eng.step()
    assert list(eng.pf.positions) == ["Nhot"]
    assert abs(eng.pf.positions["Nhot"].qty * 0.91 - eng.pf.positions["Nhot"].cost) < 0.02  # paid the ask
    rows = eng.store.db.execute(
        "SELECT cid, margin, yes_px, no_ask, depth, buyable, note FROM wx_snap ORDER BY cid").fetchall()
    by = {r[0]: r for r in rows}
    assert set(by) == {"cheap", "hot", "thin"}          # 22°C is still possible, so it is not an opportunity
    assert by["hot"][1] == 4 and by["hot"][5] == 1 and by["hot"][3] == 0.91 and by["hot"][4] == 80
    assert by["cheap"][5] == 0 and by["cheap"][6] == "JA unter 3 ct"
    assert by["thin"][5] == 0 and by["thin"][6] == "kein NO-Ask"
    eng.step()
    assert eng.store.db.execute("SELECT COUNT(*) FROM executions WHERE matched_qty>0").fetchone()[0] == 1
    clock.t = NOW + 86_400
    eng.step()
    pnl = eng.store.db.execute("SELECT pnl FROM settlements WHERE kind='position'").fetchone()[0]
    assert pnl > 0

    from dashboard import build
    page = build(str(tmp_path / "scenario-wetter_sniper.sqlite"), str(tmp_path / "www" / "index.html"),
                 500, title="Wetter-Sniper", kind="wxsniper")
    html = open(page, encoding="utf-8").read()
    data = json.loads(html.split("const D=", 1)[1].split(";\n", 1)[0])
    s = data["sniper"]
    assert s["markets"] == 3 and s["buyable_markets"] == 1 and s["bought"] == 1
    assert s["settled"] == 1 and s["wins"] == 1 and s["pnl"] > 0 and s["avg_edge"] == 0.09
    assert "Kaufbar am Ask" in html and "Wetter-Sniper" in html
    csv = (tmp_path / "www" / "snaps.csv").read_text(encoding="utf-8")
    assert "CYYZ" in csv and "kaufbar" in csv


def test_next_local_day_in_cape_town_does_not_rule_out_yesterdays_bucket(tmp_path):
    """19 Oct 08:21 in Cape Town must not use that morning's reading against the 18 Oct market.

    FACT is UTC+2. 01:00 on the 19th is still the 18th in UTC, and 08:00 is unambiguously the 19th.
    Neither reading may create a signal. A reading on the 18th local afternoon still does.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from arb.wxobs import LiveObs

    cape = ZoneInfo("Africa/Johannesburg")
    q = "Will the highest temperature in Cape Town be 16°C on October 18?"
    desc = ("Highest temperature at Cape Town International. "
            "https://www.wunderground.com/history/daily/za/cape-town/FACT")
    now = datetime(2026, 10, 19, 8, 21, tzinfo=cape).timestamp()
    next_morning = datetime(2026, 10, 19, 8, 0, tzinfo=cape).timestamp()
    still_utc_18 = datetime(2026, 10, 19, 1, 0, tzinfo=cape).timestamp()  # 23:00 UTC on the 18th

    class Cape(WxClient):
        def get_json(self, url, params):
            if "stationinfo" in url:
                return [{"lat": -33.97, "lon": 18.60}]
            if "open-meteo.com/v1/forecast" in url:
                return {"timezone": "Africa/Johannesburg"}
            if "metar" in url:
                return [{"obsTime": next_morning, "temp": 19}, {"obsTime": still_utc_18, "temp": 19}]
            return None

    assert LiveObs(Cape().get_json, ttl_s=0).ruling(q, desc, now, delay_s=0) is None
    ev = dict(id="cpt", title="Highest temperature in Cape Town on October 18?", description=desc,
              markets=[_bucket("c16", "16°C", 0.984, ["Yc", "Nc"], q)])
    books = {"Nc": ob("Nc", [(0.01, 80)], [(0.016, 80)])}
    eng = ScenarioEngine("wetter_sniper", _cfg(
        tmp_path, "wetter_sniper", strategy="wxsniper", capital_usd=500, max_position_usd=25,
        max_slippage=0, min_margin=1, min_yes=0.03, obs_delay_min=0), Cape(events=[ev], books=books),
        SimClock(now))
    eng.step()
    assert eng.pf.positions == {} and eng.strategy.snaps == []
    assert eng.strategy.scan.get("keine Messung", 0) >= 1

    same_day = datetime(2026, 10, 18, 15, 0, tzinfo=cape).timestamp()

    class CapeSame(Cape):
        def get_json(self, url, params):
            if "metar" in url:
                return [{"obsTime": same_day, "temp": 19}]
            return super().get_json(url, params)

    ruled = LiveObs(CapeSame().get_json, ttl_s=0).ruling(
        q, desc, datetime(2026, 10, 18, 16, 0, tzinfo=cape).timestamp(), delay_s=0)
    assert ruled["margin"] == 3 and ruled["day"] == "2026-10-18" and ruled["station"] == "FACT"


def test_buyable_needs_the_haircut_depth_to_cover_the_minimum(tmp_path):
    ev = dict(id="tor", title="Highest temperature in Toronto on September 21?", description=DESC,
              markets=[_bucket("small", "18°C", 0.10, ["Ys", "Ns"],
                               "Will the highest temperature in Toronto be 18°C on September 21?")])
    books = {"Ns": ob("Ns", [(0.88, 6)], [(0.90, 6)])}  # 6 × haircut 0.5 = 3 < min order 5
    eng = ScenarioEngine("wetter_sniper", _cfg(
        tmp_path, "wetter_sniper", strategy="wxsniper", capital_usd=500, max_position_usd=25,
        max_slippage=0, min_margin=1, min_yes=0.03, obs_delay_min=15), WxClient(events=[ev], books=books),
        SimClock(NOW))
    eng.step()
    assert eng.pf.positions == {}
    row = eng.store.db.execute("SELECT buyable, note, depth FROM wx_snap").fetchone()
    assert row[0] == 0 and row[1] == "unter Mindestgröße" and row[2] == 6
