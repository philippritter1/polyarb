"""wetter_sniper2 options: lowest bucket skipped, data gaps, won YES, evening and day-over NO."""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arb.engine import SimClock
from arb.scenarios import ScenarioEngine
from tests.test_scenarios import FakeClient, _cfg, ob
from tests.test_wxsniper import DESC, _bucket

TOR = ZoneInfo("America/Toronto")


def _t(day, hour, minute=0):
    return datetime(2026, 9, day, hour, minute, tzinfo=TOR).timestamp()


def _q(label):
    return f"Will the highest temperature in Toronto be {label} on September 21?"


class Client(FakeClient):
    """Hourly METAR on 21 Sep local, rising to `high` at 15:00; forecast `fc` °C for every hour."""

    def __init__(self, high=22, gap=False, fc=20.0, **kw):
        super().__init__(**kw)
        self.high, self.gap, self.fc = high, gap, fc

    def get_json(self, url, params):
        if "stationinfo" in url:
            return [{"lat": 43.68, "lon": -79.63}]
        if "open-meteo.com/v1/forecast" in url:
            if "hourly" in params:
                start = datetime(2026, 9, 21, 0, tzinfo=ZoneInfo("UTC"))
                times = [(start.replace(hour=0).timestamp() + 3600 * i) for i in range(48)]
                return {"hourly": {"time": [datetime.fromtimestamp(t, ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M")
                                            for t in times], "temperature_2m": [self.fc] * 48}}
            return {"timezone": "America/Toronto"}
        if "metar" in url:
            hours = [h for h in range(24) if not (self.gap and 3 <= h <= 8)]
            return [{"obsTime": _t(21, h, 50), "temp": min(self.high, 12 + h)} for h in hours]
        return super().get_json(url, params)


def _eng(tmp_path, client, now, name="wetter_sniper2", **opts):
    base = dict(strategy="wxsniper", capital_usd=500, max_position_usd=25, max_event_usd=75, max_slippage=0,
                min_margin=1, min_yes=0.03, obs_delay_min=0, obs_hours=48)
    return ScenarioEngine(name, _cfg(tmp_path, name, **dict(base, **opts)), client, SimClock(now))


SNIPER2 = dict(skip_lowest=True, max_gap_h=2, won_yes_margin=2, late_hour=17, late_margin=2, final_margin=2,
               lazy_books=True)


def _event(*buckets):
    return dict(id="tor", title="Highest temperature in Toronto on September 21?", description=DESC,
                markets=[_bucket(cid, label, yes, [f"Y{cid}", f"N{cid}"], _q(label)) for cid, label, yes in buckets])


def test_lowest_bucket_is_skipped_and_won_open_bucket_buys_yes(tmp_path):
    ev = _event(("low", "16°C or below", 0.10), ("top", "21°C or higher", 0.85))
    books = {"Nlow": ob("Nlow", [(0.88, 80)], [(0.90, 80)]), "Ytop": ob("Ytop", [(0.86, 80)], [(0.88, 80)])}
    eng = _eng(tmp_path, Client(events=[ev], books=books), _t(21, 16), **SNIPER2)
    eng.step()
    assert list(eng.pf.positions) == ["Ytop"]                 # YES on the bucket the high already won
    assert eng.strategy.scan["niedrigstes Bucket ausgelassen"] == 1 and eng.strategy.scan["gewonnen"] == 1
    note = eng.store.db.execute("SELECT note FROM executions WHERE matched_qty>0").fetchone()[0]
    assert "gewonnen: Station CYYZ 22" in note


def test_original_sniper_is_unchanged(tmp_path):
    ev = _event(("low", "16°C or below", 0.10), ("top", "21°C or higher", 0.85))
    books = {"Nlow": ob("Nlow", [(0.88, 80)], [(0.90, 80)]), "Ytop": ob("Ytop", [(0.86, 80)], [(0.88, 80)])}
    eng = _eng(tmp_path, Client(events=[ev], books=books), _t(21, 16), name="wetter_sniper")
    eng.step()
    assert list(eng.pf.positions) == ["Nlow"]                 # the old rules: lowest NO yes, no YES buys


def test_data_gap_blocks_buys(tmp_path):
    ev = _event(("top", "21°C or higher", 0.85))
    books = {"Ytop": ob("Ytop", [(0.86, 80)], [(0.88, 80)])}
    eng = _eng(tmp_path, Client(gap=True, events=[ev], books=books), _t(21, 16), **SNIPER2)
    eng.step()
    assert eng.pf.positions == {} and eng.strategy.scan["Datenlücke"] == 1


def test_evening_rule_needs_a_flat_forecast(tmp_path):
    ev = _event(("b24", "24°C", 0.10))
    books = {"Nb24": ob("Nb24", [(0.88, 80)], [(0.90, 80)])}
    eng = _eng(tmp_path, Client(fc=20.0, events=[ev], books=books), _t(21, 18), **SNIPER2)
    eng.step()
    assert list(eng.pf.positions) == ["Nb24"] and eng.strategy.scan["spät ausgeschlossen"] == 1
    warm = _eng(tmp_path / "w", Client(fc=24.0, events=[ev], books=books), _t(21, 18), **SNIPER2)
    warm.step()
    assert warm.pf.positions == {}
    early = _eng(tmp_path / "e", Client(fc=20.0, events=[ev], books=books), _t(21, 15, 55), **SNIPER2)
    early.step()
    assert early.pf.positions == {}                            # before 17:00 the high can still move


def test_day_over_rules_out_buckets_beyond_the_final_high(tmp_path):
    ev = _event(("b24", "24°C", 0.05), ("b23", "23°C", 0.10))
    books = {"Nb24": ob("Nb24", [(0.92, 80)], [(0.94, 80)]), "Nb23": ob("Nb23", [(0.88, 80)], [(0.90, 80)])}
    eng = _eng(tmp_path, Client(events=[ev], books=books), _t(22, 2), late_hour=None,
               **{k: v for k, v in SNIPER2.items() if k != "late_hour"})
    eng.step()
    assert list(eng.pf.positions) == ["Nb24"]                  # 23°C is only 1 above the final 22: kept out
    assert eng.strategy.scan["Tag vorbei"] == 1
