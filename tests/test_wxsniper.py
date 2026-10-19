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
