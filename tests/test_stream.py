import asyncio
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arb.stream import BookStore, handle_message


def test_book_snapshot_and_price_change():
    st = BookStore()
    handle_message(st, json.dumps([{"event_type": "book", "asset_id": "A", "timestamp": "1790000000000",
                                    "bids": [{"price": "0.40", "size": "100"}],
                                    "asks": [{"price": "0.42", "size": "50"}, {"price": "0.45", "size": "10"}]}]))
    ob = st.books(["A"])["A"]
    assert ob.best_bid == 0.40 and ob.best_ask == 0.42
    handle_message(st, json.dumps({"event_type": "price_change", "market": "m", "timestamp": "1790000000100",
                                   "price_changes": [{"asset_id": "A", "price": "0.42", "size": "0", "side": "SELL"},
                                                     {"asset_id": "A", "price": "0.41", "size": "30", "side": "BUY"}]}))
    ob = st.books(["A"])["A"]
    assert ob.best_ask == 0.45 and ob.best_bid == 0.41
    assert st.pop_dirty() == {"A"} and st.pop_dirty() == set()


def test_changes_before_snapshot_ignored_and_invalidate():
    st = BookStore()
    handle_message(st, json.dumps({"event_type": "price_change",
                                   "price_changes": [{"asset_id": "X", "price": "0.5", "size": "1", "side": "BUY"}]}))
    assert st.books(["X"]) == {}
    handle_message(st, json.dumps({"event_type": "book", "asset_id": "X", "bids": [], "asks": []}))
    assert "X" in st.books(["X"])
    st.invalidate(["X"])
    assert st.books(["X"]) == {}
    handle_message(st, "PONG")  # keep-alive answers are ignored


# ---------------------------------------------------------------- end-to-end with a local fake WS
def _fake_server(port, ready, stop):
    import websockets

    async def handler(ws):
        sub = json.loads(await ws.recv())
        assets = sub["assets_ids"]
        await ws.send(json.dumps([
            {"event_type": "book", "asset_id": a, "bids": [{"price": "0.47", "size": "500"}],
             "asks": [{"price": "0.53", "size": "500"}]} for a in assets]))
        await asyncio.sleep(1.0)
        # open an arb: both asks drop to 0.45 with 200 shares -> sum 0.90
        await ws.send(json.dumps({"event_type": "price_change", "price_changes": [
            {"asset_id": a, "price": "0.45", "size": "200", "side": "SELL"} for a in assets]}))
        while not stop.is_set():
            try:
                m = await asyncio.wait_for(ws.recv(), 0.5)
                if m == "PING":
                    await ws.send("PONG")
            except asyncio.TimeoutError:
                pass
            except Exception:
                break

    async def main():
        async with websockets.serve(handler, "127.0.0.1", port):
            ready.set()
            while not stop.is_set():
                await asyncio.sleep(0.1)

    asyncio.run(main())


class _Client:
    def __init__(self, baskets):
        self.b = baskets

    def binary_baskets(self, *a, **k):
        return self.b

    def negrisk_baskets(self, *a, **k):
        return []

    def basket_resolution(self, *a):
        return None

    def token_resolution(self, *a):
        return None


def test_stream_engine_end_to_end(tmp_path):
    import os
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
    from arb.config import load_config
    from arb.engine import StreamEngine
    from arb.models import Basket, FeeSpec

    port = 8765
    ready, stop = threading.Event(), threading.Event()
    th = threading.Thread(target=_fake_server, args=(port, ready, stop), daemon=True)
    th.start()
    assert ready.wait(5)

    cfg = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
    cfg["storage"]["db_path"] = str(tmp_path / "t.sqlite")
    cfg["stream"] = {"ws_url": f"ws://127.0.0.1:{port}", "stats_interval_s": 1}
    cfg["execution"]["latency_ms"] = 50
    fee = FeeSpec(0.0)
    b = Basket("m1", "binary", "Fake market", ["Y1", "N1"], ["Yes", "No"], [fee, fee])
    eng = StreamEngine(cfg, _Client([b]))
    eng.run(duration_s=4)
    stop.set()
    pf = eng.broker.pf
    # 200 shares available, haircut 0.5 -> ~100 sets at 0.90 -> ~+10 $, capped by risk (75 $ leg cap)
    assert pf.realized_pnl > 5, pf.realized_pnl
    rows = eng.store.db.execute("SELECT status, note FROM executions").fetchall()
    assert rows and rows[0][0] in ("filled", "partial")
