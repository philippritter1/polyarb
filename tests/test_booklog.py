"""Book snapshots stay out of the quoting path, and the replay stub reads them back."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arb.booklog import BookLog, open_log
from arb.bookreplay import distance_rule, load_snaps, main, replay
from arb.engine import SimClock
from arb.mm import MarketMaker
from arb.models import Level, OrderBook
from arb.stream import BookStore, handle_message
from tests.test_mm import NOW, _book, _cfg, _m, Client


def _ob(tid="T"):
    return OrderBook(tid, [Level(0.48, 100), Level(0.47, 40), Level(0.46, 10), Level(0.40, 5)],
                     [Level(0.52, 80), Level(0.53, 20)])


def test_record_is_throttled_compact_and_pruned(tmp_path):
    old = tmp_path / "booklog" / "2020-01-01.sqlite"
    old.parent.mkdir()
    old.write_bytes(b"")
    log = BookLog(enabled=True, directory=str(tmp_path / "booklog"), every_s=60, depth=3, keep_days=7)
    info = dict(cid="c1", event="event:9", category="Wetter", end_ts=NOW + 86400, title="Bucket", yes="Y", no="N")
    books = {"Y": _ob("Y")}
    assert log.record(NOW, books, {"Y": info}, {"Y": (0.47, 12, NOW)}) == 1
    assert log.record(NOW + 30, books, {"Y": info}, None) == 0  # same minute
    assert log.record(NOW + 60, books, {"Y": info}, None) == 1
    assert not old.exists()
    snaps = load_snaps([str(p) for p in (tmp_path / "booklog").glob("*.sqlite")])
    assert [s.ts for s in snaps] == [int(NOW // 60) * 60, int(NOW // 60) * 60 + 60]
    assert snaps[0].bid == 0.48 and snaps[0].ask == 0.52 and abs(snaps[0].mid - 0.50) < 1e-9
    assert snaps[0].last == 0.47 and snaps[0].category == "Wetter" and snaps[0].event == "event:9"
    assert [round(p, 4) for p, _ in snaps[0].bids] == [0.48, 0.47, 0.46]  # fourth level dropped
    assert abs(snaps[0].asks[0][0] - 0.52) < 1e-5 and snaps[0].asks[0][1] == 80.0
    assert open_log({"storage": {"db_path": "x"}}).enabled is False
    summary = main([str(p) for p in (tmp_path / "booklog").glob("*.sqlite")])
    assert summary["quotes"] >= 1 and summary["fills"] == 0  # the stored last trade is on the same snapshot


def test_replay_fills_when_a_later_trade_is_through_the_bid():
    def snap(ts, last, mid):
        from arb.bookreplay import Snap
        return Snap(ts=ts, token="Y", cid="c", bid=0.48, ask=0.52, bid_sz=10, ask_sz=10, mid=mid, last=last)
    rows = [snap(0, None, 0.50), snap(60, 0.45, 0.47), snap(400, None, 0.46)]
    out = replay(rows, distance_rule(0.02), markout_s=300, max_wait_s=3600)
    assert out["quotes"] == 2 and out["fills"] == 1  # the snapshot after the fill may quote again
    assert abs(out["avg_markout"] - (0.46 - 0.48)) < 1e-9
    assert replay(rows, lambda s: None)["quotes"] == 0


def test_stream_remembers_the_last_trade_after_the_queue_is_read():
    st = BookStore()
    handle_message(st, json.dumps({"event_type": "last_trade_price", "asset_id": "A", "price": "0.41",
                                   "size": "5", "timestamp": "1790000000000"}))
    st.trades.clear()
    assert st.last["A"][0] == 0.41


def test_mm_quotes_are_unchanged_when_the_log_is_on(tmp_path):
    store = BookStore()
    _book(store, "a", 0.48, 0.52)
    store.last["aY"] = (0.47, 10, NOW)
    cfg = _cfg(tmp_path)
    cfg["booklog"] = dict(enabled=True, dir=str(tmp_path / "booklog"), every_s=60, depth=3, keep_days=7)
    mm = MarketMaker("mm_rewards", cfg, Client([_m("a", "Will the Fed cut rates?", 0.50)]),
                     SimClock(NOW), books=store, pool=None)
    mm.step()
    mk = mm.st.markets["a"]
    assert mk.bid_yes["price"] == 0.48 and mk.bid_no["price"] == 0.48  # same quote as with the log off
    snaps = load_snaps([str(p) for p in (tmp_path / "booklog").glob("*.sqlite")])
    by = {s.token: s for s in snaps}
    assert by["aY"].bid == 0.48 and by["aY"].last == 0.47 and by["aY"].cid == "a"
    assert by["aN"].ask is not None
    mm.step()
    assert len(load_snaps([str(p) for p in (tmp_path / "booklog").glob("*.sqlite")])) == 2


def test_sniper_candidates_are_logged_including_ones_not_bought(tmp_path):
    from arb.scenarios import ScenarioEngine
    from tests.test_wxsniper import DESC, WxClient, _bucket
    from tests.test_scenarios import _cfg as scfg, ob
    ev = dict(id="tor", title="Highest temperature in Toronto on September 21?", description=DESC,
              endDate="2026-09-22T03:00:00Z", markets=[
                  _bucket("hot", "18°C", 0.08, ["Yhot", "Nhot"],
                          "Will the highest temperature in Toronto be 18°C on September 21?"),
                  _bucket("cheap", "16°C or below", 0.01, ["Yc", "Nc"],
                          "Will the highest temperature in Toronto be 16°C or below on September 21?"),
                  _bucket("thin", "15°C", 0.10, ["Yt", "Nt"],
                          "Will the highest temperature in Toronto be 15°C on September 21?"),
              ])
    cfg = scfg(tmp_path, "wetter_sniper", strategy="wxsniper", capital_usd=500, max_position_usd=25,
               max_slippage=0, min_margin=1, min_yes=0.03, obs_delay_min=15)
    cfg["booklog"] = dict(enabled=True, dir=str(tmp_path / "booklog"), every_s=60, depth=2, keep_days=7)
    eng = ScenarioEngine("wetter_sniper", cfg, WxClient(
        events=[ev], books={"Nhot": ob("Nhot", [(0.90, 80)], [(0.91, 80)]),
                            "Nc": ob("Nc", [(0.96, 40)], [(0.97, 40)])}), SimClock(NOW))
    eng.step()
    toks = {s.token for s in load_snaps([str(p) for p in (tmp_path / "booklog").glob("*.sqlite")])}
    assert toks == {"Nhot", "Nc"}  # thin had no book; the cheap NO is logged even though it is not bought
    assert eng.pf.positions["Nhot"].qty > 0
