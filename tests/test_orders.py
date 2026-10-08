import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arb.orders import Gateway, read_mode, write_mode

NOW = 1_790_000_000.0


def _gw(tmp_path, **lim):
    db = sqlite3.connect(":memory:")
    return Gateway("mm", db, str(tmp_path / "live.json"), str(tmp_path / "status.json"),
                   dict(dict(max_order_usd=30, max_market_usd=60, max_total_usd=50), **lim)), db


def _actions(db):
    return [r[0] for r in db.execute("SELECT action FROM live_orders ORDER BY rowid")]


def test_off_by_default_and_nothing_happens(tmp_path):
    gw, db = _gw(tmp_path)
    assert read_mode(str(tmp_path / "live.json")) == "off"
    gw.sync(NOW, {"A": (0.48, 50, "A JA")})
    assert not gw.orders and _actions(db) == []
    assert json.loads((tmp_path / "status.json").read_text())["mode"] == "aus"


def test_dry_run_places_replaces_cancels_and_enforces_limits(tmp_path):
    gw, db = _gw(tmp_path)
    write_mode(str(tmp_path / "live.json"), "dry")
    gw.sync(NOW, {"A": (0.48, 50, "A JA"), "B": (0.40, 100, "B JA")})  # B = 40 $ > max_order 30
    assert list(gw.orders) == ["A"] and _actions(db) == ["place", "reject"]
    gw.sync(NOW + 2, {"A": (0.48, 50, "A JA")})  # unchanged: nothing
    assert _actions(db) == ["place", "reject"]
    gw.sync(NOW + 4, {"A": (0.47, 50, "A JA"), "C": (0.45, 60, "C JA")})  # price change -> replace; C: 24 + 27 > 50 total
    assert _actions(db) == ["place", "reject", "cancel", "place", "reject"]
    assert gw.counts == {"gesetzt": 2, "storniert": 1, "abgelehnt": 2}
    for i in range(3):  # C still wanted, still too big: re-checked but logged only once
        gw.sync(NOW + 5 + i * 0.1, {"A": (0.47, 50, "A JA"), "C": (0.45, 60, "C JA")})
    assert _actions(db).count("reject") == 2 and gw.counts["abgelehnt"] == 2
    gw.sync(NOW + 5.5, {"C": (0.45, 60, "C JA")})  # A no longer wanted: room for C, placed now
    assert "C" in gw.orders and gw.counts["abgelehnt"] == 2
    gw.sync(NOW + 6, {})  # strategy wants nothing
    assert not gw.orders and _actions(db)[-1] == "cancel"
    # repeated rejections already in the table are pruned on start, the first of each kept
    db.executemany("INSERT INTO live_orders VALUES(?,?,?,?,?,?,?,?,?)",
                   [(NOW, "dry", "reject", "B", "", 0.40, 100, "", "Order > max_order_usd")] * 5)
    Gateway("mm", db, str(tmp_path / "live.json"), str(tmp_path / "s2.json"))
    assert _actions(db).count("reject") == 2
    st = json.loads((tmp_path / "status.json").read_text())
    assert st["mode"] == "Trockenlauf" and st["open_orders"] == 0


def test_emergency_stop_cancels_everything_and_live_never_sends(tmp_path):
    gw, db = _gw(tmp_path)
    write_mode(str(tmp_path / "live.json"), "live")  # not implemented: behaves as dry run, says so
    gw.sync(NOW, {"A": (0.48, 50, "A JA")})
    assert gw.mode == "dry" and gw.orders["A"]["oid"].startswith("dry-")
    assert "noch nicht freigeschaltet" in json.loads((tmp_path / "status.json").read_text())["mode"]
    write_mode(str(tmp_path / "live.json"), "off")
    gw.sync(NOW + 2, {"A": (0.48, 50, "A JA")})
    assert not gw.orders and _actions(db) == ["place", "cancel"]


def test_market_maker_mirrors_quotes_at_pilot_size(tmp_path):
    from arb.engine import SimClock
    from arb.mm import MarketMaker
    from arb.stream import BookStore
    from tests.test_mm import Client, _book, _cfg, _m
    cfg = _cfg(tmp_path, live=dict(quote_usd=12, max_order_usd=30, max_market_usd=60, max_total_usd=300))
    write_mode(str(tmp_path / "live.json"), "dry")
    store = BookStore()
    mm = MarketMaker("mm_rewards", cfg, Client([_m("a", "Will the Fed cut rates?", 0.50)]), SimClock(NOW),
                     books=store, pool=None)
    _book(store, "a", 0.48, 0.52)
    mm.step()
    o = mm.gw.orders
    # pilot size 12 $ / 0.48 = 25 shares, raised to the market's 20-share reward minimum if needed
    assert set(o) == {"aY", "aN"} and o["aY"]["size"] == 25 and o["aY"]["price"] == 0.48
    assert mm.scan["Orders gesetzt"] == 2 and mm.scan["Order-Modul"] == "dry"


def test_settings_page_switches_dry_and_off_but_never_live(tmp_path, monkeypatch):
    import threading
    import urllib.error
    import urllib.parse
    import urllib.request
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy"))
    import admin
    monkeypatch.setattr(admin, "DATA", str(tmp_path))
    srv = admin.serve(port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def post(mode):
        try:
            with urllib.request.urlopen(base + "/admin/live-mode", data=urllib.parse.urlencode({"mode": mode}).encode(),
                                        timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
    try:
        assert post("dry") == 200 and read_mode(str(tmp_path / "live.json")) == "dry"
        assert post("live") == 400 and read_mode(str(tmp_path / "live.json")) == "dry"
        assert post("off") == 200 and read_mode(str(tmp_path / "live.json")) == "off"
        with urllib.request.urlopen(base + "/admin/", timeout=5) as r:
            assert "Order-Modul" in r.read().decode()
    finally:
        srv.shutdown()
