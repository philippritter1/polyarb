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

    def reward_live(self, day=None):
        return {"c1": {"pct": 2.0, "earned": 0.12}}


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
    gw.sync(NOW + 63, {("A", "BUY"): (0.48, 50, "A JA")})  # retried only after a minute
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
    # 14.10.: only the dress rehearsal is released (owner's decision); 15.10. owner raised it to ~60 $
    assert released in ([], ["mm_probe"])
    for n in released:
        lim = cfg["scenarios"][n]["live"]
        assert lim["max_total_usd"] <= 60 and lim["max_daily_loss_usd"] <= 8 and lim["max_order_usd"] <= 12
    pilot = cfg["scenarios"]["mm_pilot"]
    assert pilot["live"]["quote_usd"] == pilot["quote_usd"] and pilot["live"]["max_daily_loss_usd"] <= 50


def test_connection_test_places_below_the_bid_and_always_cancels():
    from arb.exchange import connection_test, test_passed

    class Ex(FakeExchange):
        c = object()

        def best(self, token):  # "OVER" is a finished match: no bids left
            if token == "OVER":
                return dict(bid=None, ask=0.01, tick=0.01, min_size=5)
            return dict(bid=0.55, ask=0.57, tick=0.01, min_size=5)
    find = lambda: [dict(token="OVER", question="Finished match"), dict(token="T", question="Will it rain?")]  # noqa
    ex = Ex(cash=10)
    steps = connection_test(ex, find=find, wait=lambda s: None)
    assert all(s["ok"] for s in steps) and test_passed(steps), steps
    assert ("place", "T", "BUY", 0.52, 5.0) in ex.calls and ex.open == {}  # 3 ticks under the bid, gone again
    assert "Will it rain?" in steps[2]["info"]  # the one-sided book was skipped
    # only one-sided books: a failed step, and not a pass (the page claimed success on 14.10.)
    steps = connection_test(Ex(cash=10), find=lambda: [dict(token="OVER", question="x")], wait=lambda s: None)
    assert not steps[-1]["ok"] and "beiden Seiten" in steps[-1]["info"] and not test_passed(steps)
    # stopped early with every shown step green is no pass either
    assert not test_passed([{"step": "Zugang", "ok": True}])
    # the scoring check breaks after the order was placed: it is cancelled all the same
    ex2 = Ex(cash=10)
    ex2.scoring = lambda ids: (_ for _ in ()).throw(RuntimeError("down"))
    steps = connection_test(ex2, find=find, wait=lambda s: None)
    assert [s["ok"] for s in steps if "Rewards" in s["step"]] == [False] and ex2.open == {}
    assert any(c[0] == "cancel" for c in ex2.calls)
    # no money for this signature type: the other types are asked, the one holding the money is named
    ex0 = Ex(cash=0)
    ex0.wallet = {"sig_type": 1}
    ex0.balance_usd = lambda t=None: {None: 0.0, 0: 0.0, 2: 11.0}[t]
    steps = connection_test(ex0, find=find, wait=lambda s: None)
    info = {x["step"]: x["info"] for x in steps}
    assert "Typ 2: 11.0" in info["Guthaben je Signaturtyp"] and "Signaturtyp 2" in steps[-1]["info"]
    assert "Blockchain direkt" in info  # reported (here without a key: as a failed diagnostic step)
    assert not any(c[0] == "place" for c in ex0.calls)
    # not enough money: nothing is placed
    ex3 = Ex(cash=1)
    steps = connection_test(ex3, find=find, wait=lambda s: None)
    assert not any(c[0] == "place" for c in ex3.calls) and "zu wenig Guthaben" in steps[-1]["info"]
    # the exchange refuses (e.g. wrong signature type): reported, nothing to cancel
    ex4 = Ex(cash=10, fail="invalid signature")
    steps = connection_test(ex4, find=find, wait=lambda s: None)
    assert "invalid signature" in steps[-1]["info"] and not any(c[0] == "cancel" for c in ex4.calls)


def test_chain_balances_reads_both_usdc_tokens_and_contract_code():
    from arb.exchange import TOKENS, chain_balances
    calls = []

    class R:
        def __init__(self, d):
            self.d = d

        def json(self):
            return self.d

    def post(url, json, timeout):
        calls.append(url)
        if json["method"] == "eth_getCode":
            return R({"result": "0x6080"})
        tok = json["params"][0]["to"]
        return R({"result": hex(11_000_000 if tok == TOKENS["USDC"] else 0)})
    out = chain_balances({"Funder": "0x" + "1" * 40, "kaputt": "nope"}, post=post)
    assert out == {"Funder": "pUSD 0.00, USDC.e 0.00, USDC 11.00, Vertrag"}


def test_reward_test_quotes_inside_the_reward_distance_and_reports_scoring_and_fills():
    from arb.exchange import reward_test, reward_test_passed

    class Ex(FakeExchange):
        c = object()

        def __init__(self, cash=30.0, score_after=2, fills=()):
            super().__init__(cash)
            self.n_score, self.score_after, self.fill_list = 0, score_after, list(fills)

        def best(self, token):
            return {"WIDE": dict(bid=0.30, ask=0.50, tick=0.01, min_size=5),
                    "R": dict(bid=0.48, ask=0.50, tick=0.01, min_size=5)}[token]

        def scoring(self, ids):
            self.n_score += 1
            return {i: self.n_score >= self.score_after for i in ids}

        def trades(self, after_ts):
            return self.fill_list
    markets = lambda: [dict(token="WIDE", question="wide book", rate=100, v=0.035, min_size=20),  # noqa: E731
                       dict(token="R", question="Will X happen?", rate=50, v=0.035, min_size=20)]
    ex = Ex()
    steps = reward_test(ex, find=markets, wait=lambda s: None)
    # wide book skipped; bid at min(best bid 0.48, mid 0.49 - 1.75 ct -> 0.47) = 0.47, 20 shares (reward minimum)
    assert ("place", "R", "BUY", 0.47, 20.0) in ex.calls and ex.open == {} and reward_test_passed(steps), steps
    assert any("nach 40 s" in str(x["info"]) for x in steps)
    # never scores: not passed, still cancelled
    ex2 = Ex(score_after=99)
    steps = reward_test(ex2, find=markets, wait=lambda s: None)
    assert not reward_test_passed(steps) and ex2.open == {} and any(c[0] == "cancel" for c in ex2.calls)
    # filled while waiting: reported, the shares stay
    ex3 = Ex(fills=[dict(id="t", token="R", side="BUY", price=0.47, size=20, ts=0, role="MAKER")])
    steps = reward_test(ex3, find=markets, wait=lambda s: None)
    assert steps[-1]["step"] == "Teilweise ausgeführt" and "20 Anteile" in steps[-1]["info"]
    # not enough money for the reward minimum: nothing placed
    ex4 = Ex(cash=5)
    steps = reward_test(ex4, find=markets, wait=lambda s: None)
    assert not any(c[0] == "place" for c in ex4.calls) and "bräuchte 9.40 $" in steps[-1]["info"]


def test_own_fills_reads_our_side_of_maker_trades():
    from arb.exchange import own_fills
    me = "0xAbC"
    trades = [
        # we took liquidity: the trade's own fields are ours
        dict(id="t1", trader_side="TAKER", asset_id="Y", side="BUY", price="0.5", size="10", match_time="100"),
        # our YES bid was hit by a YES seller: same token -> we BOUGHT, at our price and matched amount
        dict(id="t2", trader_side="MAKER", asset_id="Y", side="SELL", price="0.47", size="30", match_time="101",
             maker_orders=[dict(order_id="o1", maker_address="0xabc", asset_id="Y", price="0.48", matched_amount="20"),
                           dict(order_id="o2", maker_address="0xother", asset_id="Y", price="0.48", matched_amount="10")]),
        # our NO bid met a YES buyer (minting): complementary token -> we BOUGHT NO
        dict(id="t3", trader_side="MAKER", asset_id="Y", side="BUY", price="0.53", size="15", match_time="102",
             maker_orders=[dict(order_id="o3", maker_address="0xABC", asset_id="N", price="0.47", matched_amount="15")]),
        # explicit side on the maker order wins
        dict(id="t4", trader_side="MAKER", asset_id="Y", side="BUY", price="0.6", size="5", match_time="103",
             maker_orders=[dict(order_id="o4", maker_address="0xabc", asset_id="Y", price="0.6", matched_amount="5",
                                side="SELL")]),
    ]
    out = {f["id"]: (f["token"], f["side"], f["price"], f["size"]) for f in own_fills(trades, me)}
    assert out == {"t1": ("Y", "BUY", 0.5, 10.0), "t2:o1": ("Y", "BUY", 0.48, 20.0), "t3:o3": ("N", "BUY", 0.47, 15.0),
                   "t4:o4": ("Y", "SELL", 0.6, 5.0)}


def test_real_fills_drive_the_market_maker_in_live_mode(tmp_path):
    from arb.engine import SimClock
    from arb.mm import MarketMaker
    from arb.stream import BookStore
    from tests.test_mm import Client, _book, _cfg, _m
    cfg = _cfg(tmp_path, live=dict(quote_usd=20, max_order_usd=30, max_market_usd=60, max_total_usd=300,
                                   allow_real=True, max_daily_loss_usd=50))
    write_mode(str(tmp_path / "live.json"), "live")
    store = BookStore()
    mm = MarketMaker("mm_rewards", cfg, Client([_m("a", "Will the Fed cut rates?", 0.50)]), SimClock(NOW),
                     books=store, pool=None)
    ex = FakeExchange(cash=300)
    mm.gw._ex, mm.gw._geo_fn, mm.gw._value_fn = ex, (lambda: {"blocked": False, "country": "FI"}), (lambda u: 0.0)
    _book(store, "a", 0.48, 0.52)
    mm.step()
    assert mm.gw.live and any(c[0] == "place" for c in ex.calls)
    # a simulated trade below our bid no longer counts in live mode ...
    store.trades.append(("aY", 0.40, 50, NOW + 5))
    # ... but the real fill from the account does (fills from before going live never do)
    ex.fills = [dict(id="old", token="aY", side="BUY", price=0.48, size=99, ts=NOW - 3600, role="MAKER"),
                dict(id="f1", token="aY", side="BUY", price=0.48, size=12, ts=NOW + 6, role="MAKER")]
    mm.clock.sleep(100)
    mm.step()
    mk = mm.st.markets["a"]
    assert mk.qty_yes == 12 and mm.st.fills == 1 and mm.scan["echte Fills"] == 1
    # handed over once: the next sync does not book it again; a real sale reduces the position
    ex.fills.append(dict(id="f2", token="aY", side="SELL", price=0.52, size=5, ts=NOW + 150, role="MAKER"))
    mm.clock.sleep(100)
    mm.step()
    assert mk.qty_yes == 7 and abs(mm.st.exit_pnl - 5 * 0.04) < 1e-9
    # a fill in a token the strategy does not know is reported, not booked
    ex.fills.append(dict(id="f3", token="ZZ", side="BUY", price=0.3, size=5, ts=NOW + 250, role="MAKER"))
    mm.clock.sleep(100)
    mm.step()
    assert mm.scan["echte Fills ohne Markt"] == 1 and mk.qty_yes == 7


def test_reward_share_is_stored_next_to_the_estimate_and_shown(tmp_path):
    import dashboard
    ex = FakeExchange()
    gw, db = _live(tmp_path, ex)
    gw.est = {"c1": dict(q="Markt 1", rate=100.0, pct=2.5), "c2": dict(q="Markt 2", rate=50.0, pct=1.0)}
    gw.sync(NOW, {("A", "BUY"): (0.48, 50, "A JA")})
    gw.sync(NOW + 100, {("A", "BUY"): (0.48, 50, "A JA")})
    rows = db.execute("SELECT cid, rate, pm_pct, est_pct, pm_earned FROM live_reward_share ORDER BY cid").fetchall()
    assert rows == [("c1", 100.0, 2.0, 2.5, 0.12), ("c2", 50.0, None, 1.0, None)]
    st = json.loads((tmp_path / "live-status-pilot.json").read_text())
    assert st["reward_share"][0]["pm_day"] == 2.0 and st["reward_share"][0]["est_day"] == 2.5
    assert st["reward_share"][1]["pm_day"] is None
    db.commit()
    path = str(tmp_path / "x.db")
    disk = sqlite3.connect(path)
    db.backup(disk)
    disk.close()
    day, est, pm, ratio, earned, n = dashboard.reward_share_daily(path)[0]
    assert (est, pm, ratio, earned, n) == (3.0, 2.0, 67, 0.12, 1)


def test_reward_live_reads_polymarkets_share_and_earnings():
    from arb.exchange import PolyExchange

    class C:
        def get_reward_percentages(self):
            return {"c1": 1.5, "c2": "x"}

        def get_earnings_for_user_for_day(self, day):
            return [dict(condition_id="c1", earnings=0.1), dict(condition_id="c1", earnings="0.05"),
                    dict(condition_id="c3", earnings=0.2)]
    ex = PolyExchange.__new__(PolyExchange)
    ex._c = C()
    assert ex.reward_live("2026-10-14") == {"c1": {"pct": 1.5, "earned": 0.15}, "c3": {"earned": 0.2}}


def test_exchange_refusal_is_not_retried_for_a_minute(tmp_path):
    ex = FakeExchange(fail="not enough balance / allowance")
    gw, _ = _live(tmp_path, ex)
    for i in range(10):  # every 2 s, like the strategy loop
        gw.sync(NOW + 2 * i, {("A", "BUY"): (0.31, 20, "A JA")})
    assert sum(c[0] == "place" for c in ex.calls) == 1
    ex.fail = None
    gw.sync(NOW + 61, {("A", "BUY"): (0.31, 20, "A JA")})
    assert sum(c[0] == "place" for c in ex.calls) == 2 and ("A", "BUY") in gw.orders


def test_live_bids_are_limited_to_the_real_cash():
    from types import SimpleNamespace
    from arb.mm import MMMarket, MarketMaker
    seen = {}
    gw = SimpleNamespace(live=True, counts={"gesetzt": 0}, sync=lambda now, want: seen.update(want))
    mk = MMMarket("c", "Markt", "Y", "N", None, 100.0, 0.05, 20.0, "x",
                  bid_yes=dict(price=0.30, size=20, placed_ts=0), bid_no=dict(price=0.31, size=20, placed_ts=0))
    me = SimpleNamespace(sc=dict(quote_usd=5, live=dict(quote_usd=5)), gw=gw, scan={}, store=None,
                         st=SimpleNamespace(cash=7.0, markets={"c": mk}))
    MarketMaker.sync_orders(me, NOW)
    assert list(seen) == [("Y", "BUY")] and me.scan["Gebote ohne Cash (echt)"] == 1  # 6 $ fit, the next 6.2 $ not
    gw.live = False  # dry run: no cash limit, both bids mirrored
    seen.clear()
    MarketMaker.sync_orders(me, NOW)
    assert set(seen) == {("Y", "BUY"), ("N", "BUY")}


def test_sell_now_only_live_and_at_most_once_a_minute(tmp_path):
    ex = FakeExchange()
    ex.sell_now = lambda tok, price, size: ex.calls.append(("sell_now", tok, price, size)) or "0xs"
    gw, db = _live(tmp_path, ex)
    gw.sync(NOW, {})
    assert gw.sell_now(NOW, "A", 0.48, 20, "A JA") and ("sell_now", "A", 0.48, 20) in ex.calls
    assert not gw.sell_now(NOW + 30, "A", 0.48, 20, "A JA")  # once a minute
    assert gw.sell_now(NOW + 61, "A", 0.48, 20, "A JA")
    assert db.execute("SELECT COUNT(*) FROM live_orders WHERE action='dump'").fetchone()[0] == 2
    write_mode(str(tmp_path / "live.json"), "dry")
    gw.sync(NOW + 62, {})
    assert not gw.sell_now(NOW + 200, "A", 0.48, 20, "A JA")


def test_positions_and_fill_and_kill_sell_without_network(monkeypatch):
    import sys
    import types
    from arb.exchange import PolyExchange, positions
    if "py_clob_client_v2" not in sys.modules:  # the live library is optional: stand in for its two modules
        ct = types.SimpleNamespace(OrderArgsV2=lambda **k: types.SimpleNamespace(**k),
                                   OrderType=types.SimpleNamespace(GTC="GTC", FAK="FAK"))
        monkeypatch.setitem(sys.modules, "py_clob_client_v2", types.ModuleType("py_clob_client_v2"))
        monkeypatch.setitem(sys.modules, "py_clob_client_v2.clob_types", ct)
        monkeypatch.setitem(sys.modules, "py_clob_client_v2.order_builder", types.ModuleType("ob"))
        monkeypatch.setitem(sys.modules, "py_clob_client_v2.order_builder.constants",
                            types.SimpleNamespace(BUY="BUY", SELL="SELL"))
    pos = positions("0x1", lambda *a, **k: [dict(asset="t1", size="20", avgPrice="0.32", conditionId="c",
                                                  oppositeAsset="t2", outcomeIndex=1), dict(x=1)])
    assert list(pos) == ["t1"] and pos["t1"]["size"] == 20.0 and pos["t1"]["avg"] == 0.32
    assert pos["t1"]["opposite"] == "t2" and pos["t1"]["index"] == 1
    assert positions("0x1", lambda *a, **k: {"error": "x"}) is None

    class C:
        def create_order(self, args):
            return args

        def post_order(self, order, order_type, post_only=False):
            self.sent = (order.side, order.price, order.size, order_type, post_only)
            return {"success": True, "orderID": "0xf"}
    ex = PolyExchange.__new__(PolyExchange)
    ex._c = C()
    assert ex.sell_now("t1", 0.48, 20) == "0xf" and ex._c.sent == ("SELL", 0.48, 20.0, "FAK", False)
