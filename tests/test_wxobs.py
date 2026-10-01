import math
import sqlite3
from datetime import date, datetime, timezone

from arb.study import SCHEMA as STUDY_SCHEMA
from arb.wxobs import WxObsStudy, analysis, csv_rows, dead_time, parse_question, station_from, to_unit

UTC = timezone.utc
END = datetime(2026, 9, 3, 14, tzinfo=UTC).timestamp()      # Polymarket "end": 14:00 UTC on the day
CLOSE = datetime(2026, 9, 4, 10, tzinfo=UTC).timestamp()    # resolved the next morning
DESC = ("This market will resolve to the temperature range that contains the highest temperature recorded at "
        "the London City Airport Station in degrees Celsius on 3 Sep '26. Source: "
        "https://www.wunderground.com/history/daily/gb/london/EGLC.")


def _temp_c(ts):
    """Sep 3 in London (UTC+1): 15 °C at night, peak 25 °C at 15:00 local; other days 18 °C."""
    local = datetime.fromtimestamp(ts + 3600, tz=UTC)
    if local.date() != date(2026, 9, 3):
        return 18
    return max(15, 25 - abs(local.hour - 15))


def _iem_csv():
    lines = ["station,valid,tmpf"]
    t = datetime(2026, 9, 2, tzinfo=UTC).timestamp()
    while t < datetime(2026, 9, 5, tzinfo=UTC).timestamp():
        lines.append(f"EGLC,{datetime.fromtimestamp(t, tz=UTC):%Y-%m-%d %H:%M},{_temp_c(t) * 9 / 5 + 32:.2f}")
        t += 1800
    lines.append("EGLC,2026-09-03 05:20,M")  # missing value: ignored
    return "\n".join(lines)


class Client:
    gamma, clob = "https://gamma", "https://clob"

    def __init__(self):
        self.calls = []

    def get_json(self, url, params):
        self.calls.append(url)
        if "stationinfo" in url:
            assert params["ids"] == "EGLC"
            return [{"icaoId": "EGLC", "lat": 51.5, "lon": 0.05}]
        if "open-meteo.com/v1/forecast" in url:
            assert params["latitude"] == 51.5 and params["timezone"] == "auto"
            return {"timezone": "Europe/London"}
        if url.endswith("/markets"):
            return [{"conditionId": c, "description": DESC, "clobTokenIds": f'["Y{c}", "N{c}"]'}
                    for c in params["condition_ids"]]
        if url.endswith("/prices-history"):
            price = {"Ya": 0.08, "Yd": 0.05}[params["market"]]
            t = params["startTs"]
            return {"history": [{"t": t + k * 600, "p": price} for k in range(200)]}
        raise AssertionError(url)


def _study(tmp_path):
    db = sqlite3.connect(str(tmp_path / "study.sqlite"))
    db.executescript(STUDY_SCHEMA)
    rows = [("a", "Will the highest temperature in London be 23°C on September 3?", 0, 0.10),  # dies at 24 °C
            ("b", "Will the highest temperature in London be 25°C on September 3?", 1, 0.30),  # the winner
            ("c", "Will the highest temperature in London be 22°C or below on September 3?", 0, 0.01),  # cheap
            ("d", "Will the highest temperature in London be 24°C on September 3?", 1, 0.20),  # station says dead, won anyway
            ("e", "Will it rain in London on September 3?", 0, 0.5)]
    for cid, q, out, p in rows:
        db.execute("INSERT INTO markets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (cid, q, "Wetter", 1, 0, 800, END, CLOSE, out, None, p, p, p, 20, 0))
    db.commit()
    return str(tmp_path / "study.sqlite")


def test_helpers():
    k, city, b, d = parse_question("Will the highest temperature in Seoul (Incheon) be between 66-67°F on September 23?", 2026)
    assert (k, city, b, d) == ("max", "Seoul (Incheon)", (66, 67, "f"), date(2026, 9, 23))
    assert parse_question("Will the lowest temperature in Austin be 67°F or below on August 30?", 2026)[2] == (-math.inf, 67, "f")
    assert parse_question("Will it rain?", 2026) is None
    assert station_from(DESC) == "EGLC"
    assert station_from("see https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA") == "KLGA"
    assert station_from("Hong Kong Observatory https://www.hko.gov.hk/") is None
    assert station_from("here: https://www.wunderground.com/history/daily/KMIA/date/2026-9-23.") == "KMIA"
    assert station_from("https://www.wunderground.com/history/daily/tw/taipei/RCSS.") == "RCSS"
    assert station_from("see https://weather.gov/wrh/timeseries?site=KAUS and https://www.wunderground.com/history/daily/us/tx/austin/KAUS") == "KAUS"
    assert station_from("recorded at Toronto Pearson (CYYZ) by Environment Canada") == "CYYZ"
    assert to_unit(71.96, "f") == 72 and to_unit(75.2, "c") == 24 and to_unit(76.1, "c") == 25
    r = [(1, 64.4), (2, 59.0), (3, 57.2)]  # 18, 15, 14 °C
    assert dead_time(r, "min", 15, 15, "c", 0) == 3 and dead_time(r, "min", 15, 15, "c", 1) is None
    assert dead_time(r, "max", 16, 17, "c", 0) == 1 and dead_time(r, "max", 18, math.inf, "c", 0) is None


def test_collect_and_analysis(tmp_path):
    wx = str(tmp_path / "wxobs.sqlite")
    calls = []

    def fetch(url, params):
        calls.append(params["station"])
        return _iem_csv()

    cl = Client()
    st = WxObsStudy(cl, wx, _study(tmp_path), fetch_text=fetch)
    stats = st.collect(now=CLOSE + 86400)
    assert stats["processed"] == 5 and stats["candidates"] == 2 and stats["priced"] == 2
    assert calls == ["EGLC"]  # one archive request serves the whole city/day
    reasons = dict(st.db.execute("SELECT condition_id, reason FROM wx_markets"))
    assert reasons == {"a": None, "b": "nie unmöglich", "c": "schon vorher billig", "d": None, "e": "Frage nicht lesbar"}
    a = st.db.execute("SELECT dead0_ts, dead1_ts, yes0_15 FROM wx_markets WHERE condition_id='a'").fetchone()
    assert a[0] == datetime(2026, 9, 3, 13, tzinfo=UTC).timestamp()  # first 24 °C reading: 14:00 local
    assert a[1] == datetime(2026, 9, 3, 14, tzinfo=UTC).timestamp()  # with 1° margin: the 25 °C reading
    assert a[2] == 0.08
    # second run: nothing left, no new requests
    n_calls = len(cl.calls)
    assert st.collect(now=CLOSE + 86400)["processed"] == 0 and len(cl.calls) == n_calls

    res = {(r["margin"], r["delay"]): r for r in analysis(wx)["results"]}
    r0 = res[(0, 15)]
    assert r0["n"] == 2 and r0["errors"] == 1  # bucket d "died" at 25 °C but won: an error
    r1 = res[(1, 15)]
    assert r1["n"] == 1 and r1["errors"] == 0 and r1["roi"] > 0  # the margin removes the error
    header, rows = csv_rows(wx)
    assert len(rows) == 5 and len(rows[0]) == len(header)


def test_city_without_station_is_skipped(tmp_path):
    class NoStation(Client):
        def get_json(self, url, params):
            if url.endswith("/markets"):
                return [{"conditionId": params["condition_ids"][0], "description": "Source: https://www.hko.gov.hk/"}]
            return super().get_json(url, params)

    st = WxObsStudy(NoStation(), str(tmp_path / "wx.sqlite"), _study(tmp_path), fetch_text=lambda u, p: "")
    st.collect(now=CLOSE)
    row = st.db.execute("SELECT station, source, reason FROM wx_city").fetchone()
    assert row == (None, "www.hko.gov.hk", "keine Station im Beschreibungstext")
    assert analysis(str(tmp_path / "wx.sqlite"))["priced"] == 0
    # failures are not final: after RETRY_AFTER the city and its markets are tried again
    from arb.wxobs import RETRY_AFTER
    st.client = Client()
    stats = st.collect(now=CLOSE + RETRY_AFTER + 60)
    assert stats["processed"] == 4  # a-d again (e stays "Frage nicht lesbar")
    assert st.db.execute("SELECT station, reason FROM wx_city").fetchone() == ("EGLC", None)
