from arb.client import PaginationEnd
from arb.kalshi import BASE, KalshiStudy, _bets, analysis, at, csv_rows, evaluate

NOW = 1_790_000_000.0
CLOSE = NOW - 2 * 86400


def _candles(close, p_cents, bid, ask, dollars=False):
    """Hourly candles up to the close with a constant book."""
    out = []
    for h in range(30, -1, -1):
        t = int(close - h * 3600)
        if dollars:
            out.append({"end_period_ts": t, "price": {"close_dollars": f"{p_cents / 100:.4f}"},
                        "yes_bid": {"close_dollars": f"{bid / 100:.4f}"}, "yes_ask": {"close_dollars": f"{ask / 100:.4f}"}})
        else:
            out.append({"end_period_ts": t, "price": {"close": p_cents}, "yes_bid": {"close": bid}, "yes_ask": {"close": ask}})
    return out


class KalshiClient:
    def __init__(self):
        iso = "2026-09-24T04:00:00Z"
        self.markets = [
            dict(ticker="KXHIGHNY-26SEP23-B78", event_ticker="KXHIGHNY-26SEP23", market_type="binary",
                 title="Highest temperature in NYC on Sep 23?", yes_sub_title="78° to 79°", result="no",
                 volume=3200, close_time=CLOSE),
            dict(ticker="KXHIGHCHI-26SEP23-B70", event_ticker="KXHIGHCHI-26SEP23", market_type="binary",
                 title="Highest temperature in Chicago on Sep 23?", result="yes", volume_fp="900.00", close_time=iso),
            dict(ticker="KXMLB-26SEP23-NYY", event_ticker="KXMLB-26SEP23", market_type="binary",
                 title="Yankees win?", result="", volume=50000, close_time=CLOSE),          # not settled yet
            dict(ticker="KXBTC-26SEP23-B100", event_ticker="KXBTC-26SEP23", market_type="binary",
                 title="Bitcoin price range", result="no", volume=90000, close_time=CLOSE),  # crypto: skipped
            dict(ticker="KXHIGHMIA-26SEP23-B90", event_ticker="KXHIGHMIA-26SEP23", market_type="binary",
                 title="Highest temperature in Miami on Sep 23?", result="no", volume=20, close_time=CLOSE),  # too small
        ]
        self.calls = []

    def get_json(self, url, params):
        path = url[len(BASE):]
        self.calls.append(path)
        if path == "/markets":
            if params.get("cursor") == "p2":
                return {"markets": self.markets[2:], "cursor": ""}
            return {"markets": self.markets[:2], "cursor": "p2"}
        if path == "/series":
            return {"series": [{"ticker": "KXHIGHNY", "category": "Climate and Weather"},
                               {"ticker": "KXHIGHCHI", "category": "Climate and Weather"},
                               {"ticker": "KXMLB", "category": "Sports"}, {"ticker": "KXBTC", "category": "Crypto"},
                               {"ticker": "KXHIGHMIA", "category": "Climate and Weather"}]}
        if path == "/series/KXHIGHNY/markets/KXHIGHNY-26SEP23-B78/candlesticks":
            return {"candlesticks": _candles(CLOSE, 20, 19, 21)}
        if path == "/series/KXHIGHCHI/markets/KXHIGHCHI-26SEP23-B70/candlesticks":
            raise PaginationEnd("404")  # old market: only under /historical
        if path == "/historical/markets/KXHIGHCHI-26SEP23-B70/candlesticks":
            from arb.kalshi import _ts
            return {"candlesticks": _candles(_ts("2026-09-24T04:00:00Z"), 40, 38, 42, dollars=True)}
        raise AssertionError(path)


def test_collect_reads_cents_dollars_and_historical(tmp_path):
    db = str(tmp_path / "kalshi.sqlite")
    st = KalshiStudy(KalshiClient(), db)
    stats = st.collect(days_back=3, recent_days=3, window_days=3, min_volume=100, now=NOW)
    assert stats["new"] == 2 and stats["skipped"] == 1  # NY + Chicago; the unsettled MLB market is skipped
    rows = {r[0]: r for r in st.db.execute("SELECT ticker, category, result, p_6h, ask_yes_6h, ask_no_6h FROM kalshi_markets")}
    ny = rows["KXHIGHNY-26SEP23-B78"]
    assert ny[1] == "Wetter" and ny[2] == 0
    assert abs(ny[3] - 0.20) < 1e-9 and abs(ny[4] - 0.21) < 1e-9 and abs(ny[5] - 0.81) < 1e-9  # NO ask = 1 - YES bid
    chi = rows["KXHIGHCHI-26SEP23-B70"]
    assert chi[2] == 1 and abs(chi[3] - 0.40) < 1e-9 and abs(chi[5] - 0.62) < 1e-9
    assert st.db.execute("SELECT reason FROM kalshi_skipped").fetchone()[0] == "result ''"
    # second run: nothing new, the bitcoin market was never fetched
    assert st.collect(days_back=3, recent_days=3, window_days=3, now=NOW)["new"] == 0
    assert not any("KXBTC" in c for c in st.client.calls)
    header, out = csv_rows(db)
    assert len(out) == 2 and header[0] == "Ticker" and out[0][9] in ("YES", "NO")
    from arb.kalshi import diag_rows
    d = dict(diag_rows(db)[1])
    assert "ticker" in d["market_keys"] and "übersprungen: result ''" in d  # Chicago (volume_fp only) was collected above


def test_at_respects_max_age():
    cs = _candles(CLOSE, 20, 19, 21)
    assert at(cs, CLOSE - 3600, 1800)["ask_yes"] == 0.21
    assert at(cs, CLOSE - 40 * 3600, 3600) is None


def test_rules_buy_at_the_real_ask(tmp_path):
    # weather NO at a 0.80 price but a 0.83 ask, winning 90 %: the ask decides the result
    rows = [(f"T{i}", f"E{i}", "Wetter", 1000, CLOSE + i, int(i % 10 == 0), None, 0.20, None,
             None, None, 0.21, 0.83, None, None) for i in range(100)]
    bets = _bets(rows, "6h", 0.55, 0.97, "Wetter", side="no")
    assert len(bets) == 100 and all(b["at_ask"] and b["buy"] == 0.83 for b in bets)
    r = evaluate(bets, n_boot=50)
    assert abs(r["hit"] - 0.9) < 1e-9 and r["roi"] < 0.09  # (0.90 - 0.83 - fee) / cost
    # bigger markets are left out of the 5k rule
    big = [tuple(list(x[:3]) + [9000] + list(x[4:])) for x in rows]
    assert _bets(big, "6h", 0.55, 0.97, "Wetter", side="no", max_volume=5000) == []


def test_analysis_without_and_with_data(tmp_path):
    db = str(tmp_path / "kalshi.sqlite")
    assert analysis(db)["n"] == 0
    st = KalshiStudy(KalshiClient(), db)
    st.collect(days_back=3, recent_days=3, window_days=3, now=NOW)
    a = analysis(db)
    assert a["n"] == 2 and a["cats"]["Wetter"]["n"] == 2 and a["last_run"]
    assert {r["key"] for r in a["rules"]} >= {"k_wetter_no_breit", "k_sport_dog"}
