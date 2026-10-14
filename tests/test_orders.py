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
    assert list(gw.orders) == [("A", "BUY")] and _actions(db) == ["place", "reject"]
    gw.sync(NOW + 2, {"A": (0.48, 50, "A JA")})  # unchanged: nothing
    assert _actions(db) == ["place", "reject"]
    gw.sync(NOW + 4, {"A": (0.47, 50, "A JA"), "C": (0.45, 60, "C JA")})  # price change -> replace; C: 24 + 27 > 50 total
    assert _actions(db) == ["place", "reject", "cancel", "place", "reject"]
    assert gw.counts == {"gesetzt": 2, "storniert": 1, "abgelehnt": 2}
    for i in range(3):  # C still wanted, still too big: re-checked but logged only once
        gw.sync(NOW + 5 + i * 0.1, {"A": (0.47, 50, "A JA"), "C": (0.45, 60, "C JA")})
    assert _actions(db).count("reject") == 2 and gw.counts["abgelehnt"] == 2
    gw.sync(NOW + 5.5, {"C": (0.45, 60, "C JA")})  # A no longer wanted: room for C, placed now
    assert ("C", "BUY") in gw.orders and gw.counts["abgelehnt"] == 2
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
    assert gw.mode == "dry" and gw.orders[("A", "BUY")]["oid"].startswith("dry-")
    assert "nicht freigegeben" in json.loads((tmp_path / "status.json").read_text())["mode"]
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
    assert set(o) == {("aY", "BUY"), ("aN", "BUY")} and o[("aY", "BUY")]["size"] == 25 and o[("aY", "BUY")]["price"] == 0.48
    assert mm.scan["Orders gesetzt"] == 2 and mm.scan["Order-Modul"] == "dry"


def test_settings_page_switches_dry_and_off_but_never_live(tmp_path, monkeypatch):
    import threading
    import urllib.error
    import urllib.parse
    import urllib.request
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy"))
    import admin
    monkeypatch.setattr(admin, "DATA", str(tmp_path))
    monkeypatch.setattr(admin, "geocheck", lambda: {"blocked": None, "country": ""})
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


class FakeExchange:
    """Stands in for arb/exchange.PolyExchange: records calls, never touches the network."""
    user = "0xabc"

    def __init__(self, cash=300.0, fail=None):
        self.calls, self.open, self.cash, self.fail, self._n = [], {}, cash, fail, 0
        self.fills = []

    def place(self, token, side, price, size):
        self.calls.append(("place", token, side, price, size))
        if self.fail:
            raise RuntimeError(self.fail)
        self._n += 1
        oid = f"0x{self._n}"
        self.open[oid] = dict(token=token, side=side, price=price, size=size, matched=0)
        return oid

    def cancel(self, oid):
        self.calls.append(("cancel", oid))
        self.open.pop(oid, None)

    def cancel_all(self):
        self.calls.append(("cancel_all",))
        self.open.clear()

    def open_orders(self):
        return dict(self.open)

    def trades(self, after_ts):
        return list(self.fills)

    def balance_usd(self):
        return self.cash

    def scoring(self, ids):
        return {i: True for i in ids}

    def rewards(self, day):
        return 1.23


def _live(tmp_path, ex=None, geo=None, value=0.0, **lim):
    db = sqlite3.connect(":memory:")
    gw = Gateway("pilot", db, str(tmp_path / "live.json"), str(tmp_path / "live-status-pilot.json"),
                 dict(dict(max_order_usd=30, max_market_usd=60, max_total_usd=50, allow_real=True,
                           max_daily_loss_usd=20), **lim),
                 exchange=ex, geo=lambda: (geo or {"blocked": False, "country": "DE"}), value=lambda user: value)
    write_mode(str(tmp_path / "live.json"), "live")
    return gw, db


def test_live_needs_every_key(tmp_path):
    # allowed on /admin/ but not for this scenario -> dry run, nothing reaches the exchange
    ex = FakeExchange()
    gw, _ = _live(tmp_path, ex, allow_real=False)
    gw.sync(NOW, {("A", "BUY"): (0.48, 50, "A")})
    assert not gw.live and ex.calls == [] and "nicht freigegeben" in gw.why
    # geoblocked or unknown -> dry run
    for geo, why in (({"blocked": True, "country": "XX"}, "sperrt"), ({"blocked": None}, "nicht erreichbar")):
        ex = FakeExchange()
        gw, _ = _live(tmp_path, ex, geo=geo)
        gw.sync(NOW, {("A", "BUY"): (0.48, 50, "A")})
        assert not gw.live and ex.calls == [] and why in gw.why
    # no wallet -> dry run
    gw, _ = _live(tmp_path, None)
    gw.secrets_path = str(tmp_path / "none.env")
    gw.sync(NOW, {("A", "BUY"): (0.48, 50, "A")})
    assert not gw.live and "Wallet" in gw.why


def test_live_places_replaces_reconciles_and_stops(tmp_path):
    ex = FakeExchange()
    gw, db = _live(tmp_path, ex)
    gw.sync(NOW, {("A", "BUY"): (0.48, 50, "A JA"), ("B", "SELL"): (0.70, 80, "B NEIN")})
    assert gw.live and ex.calls[0] == ("cancel_all",)  # clean start at the exchange
    assert ("place", "A", "BUY", 0.48, 50) in ex.calls and ("place", "B", "SELL", 0.70, 80) in ex.calls
    # a sell needs no cash: 56 $ would break max_total for a buy, but not for the sell
    assert gw.open_usd() == 24
    gw.sync(NOW + 2, {("A", "BUY"): (0.47, 50, "A JA"), ("B", "SELL"): (0.70, 80, "B NEIN")})
    assert ex.calls[-2][0] == "cancel" and ex.calls[-1] == ("place", "A", "BUY", 0.47, 50)
    # A was filled at the exchange: reconcile drops it, the next sync places it again; fills are stored
    oid = gw.orders[("A", "BUY")]["oid"]
    ex.open.pop(oid)
    ex.fills = [dict(id="t1", token="A", side="BUY", price=0.47, size=50, ts=NOW + 3, role="MAKER")]
    gw.sync(NOW + 100, {("A", "BUY"): (0.47, 50, "A JA"), ("B", "SELL"): (0.70, 80, "B NEIN")})
    assert db.execute("SELECT COUNT(*) FROM live_fills").fetchone()[0] == 1
    assert gw.orders[("A", "BUY")]["oid"] != oid
    st = json.loads((tmp_path / "live-status-pilot.json").read_text())
    assert st["mode"] == "ECHT" and st["live"] and st["fills"] == 1 and st["scoring"] == "1/1"  # checked before A was placed again
    # emergency stop: everything cancelled, also at the exchange
    write_mode(str(tmp_path / "live.json"), "off")
    gw.sync(NOW + 102, {("A", "BUY"): (0.47, 50, "A JA")})
    assert not gw.orders and ex.calls[-1] == ("cancel_all",) and ex.open == {}


def test_exchange_rejection_is_logged_once_and_daily_loss_switches_everything_off(tmp_path):
    ex = FakeExchange(fail="order crosses book")
    gw, db = _live(tmp_path, ex)
    for i in range(3):
        gw.sync(NOW + i, {("A", "BUY"): (0.48, 50, "A JA")})
    rej = db.execute("SELECT note FROM live_orders WHERE action='reject'").fetchall()
    assert rej == [("Börse: order crosses book",)] and gw.counts["abgelehnt"] == 1
    ex.fail = None
    gw.sync(NOW + 3, {("A", "BUY"): (0.48, 50, "A JA")})
    assert ("A", "BUY") in gw.orders
    # cash falls by 25 $ (> 20 $ limit) within the day: off for everyone, all cancelled at the exchange
    ex.cash = 275.0
    gw.sync(NOW + 400, {("A", "BUY"): (0.48, 50, "A JA")})
    assert read_mode(str(tmp_path / "live.json")) == "off" and not gw.orders and ("cancel_all",) in ex.calls
    assert "Tagesverlust" in json.loads((tmp_path / "live.json").read_text())["why"]


def test_exchange_helpers_without_network(tmp_path):
    from arb.exchange import account_value, geoblock, read_wallet, wallet_problem
    (tmp_path / "s.env").write_text("POLY_PRIVATE_KEY=" + "ab" * 32 + "\nPOLY_FUNDER=0x" + "1" * 40 + "\nPOLY_SIG_TYPE=2\n")
    w = read_wallet(str(tmp_path / "s.env"))
    assert w["sig_type"] == 2 and wallet_problem(w) == ""
    assert wallet_problem(dict(w, key="xyz")) and wallet_problem(dict(w, funder=""))
    assert geoblock(lambda *a, **k: {"blocked": False, "country": "DE"}) == {"blocked": False, "country": "DE", "region": ""}

    def boom(*a, **k):
        raise OSError
    assert geoblock(boom)["blocked"] is None and account_value("0x1", boom) is None
    assert account_value("0x1", lambda *a, **k: [{"user": "0x1", "value": 12.5}]) == 12.5


def test_no_scenario_is_released_for_real_orders_by_accident():
    """allow_real is the owner's decision after the pilot review – changing it must change this test too."""
    import yaml
    cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text(encoding="utf-8"))
    released = [n for n, sc in cfg["scenarios"].items() if (sc.get("live") or {}).get("allow_real")]
    assert released == []
    pilot = cfg["scenarios"]["mm_pilot"]
    assert pilot["live"]["quote_usd"] == pilot["quote_usd"] and pilot["live"]["max_daily_loss_usd"] <= 50
