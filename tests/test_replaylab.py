import sqlite3
from types import SimpleNamespace as L

from arb import replaylab
from arb.booklog import SCHEMA, pack_levels

T0 = 1_760_000_400  # a block boundary for 1 h blocks


def _booklog(path, minutes=240, crash_at=100, after=0.48, base=0.50, trades=None):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute("INSERT INTO mkt VALUES('c1', 'e', 'Politik', ?, 'Wird X?', 'Y', 'N')", (T0 + 30 * 86400,))
    db.execute("INSERT INTO mkt VALUES('c2', 'e', 'Politik', NULL, 'ohne Rewards', 'Y2', 'N2')")
    for i in range(minutes):
        ts = T0 + 60 * i
        mid = base if i <= crash_at else after  # bad news right after the fill
        bid, ask = round(mid - 0.01, 2), round(mid + 0.01, 2)
        if i == crash_at:
            ask = 0.47  # sellers hit down to 0.47: fills every bid at or above it
        db.execute("INSERT INTO snap VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (ts, "Y", "c1", bid, ask, 100, 100, mid, (trades or {}).get(i),
                    pack_levels([L(price=bid, size=100)], 3), pack_levels([L(price=ask, size=100)], 3)))
        db.execute("INSERT INTO snap VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (ts, "N", "c1", round(1 - ask, 2), round(1 - bid, 2), 100, 100, round(1 - mid, 2), None,
                    pack_levels([L(price=round(1 - ask, 2), size=100)], 3),
                    pack_levels([L(price=round(1 - bid, 2), size=100)], 3)))
    db.commit()
    db.close()


def _scen(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE mm_reward_params(ts REAL, cid TEXT, title TEXT, daily_rate REAL, max_spread_ct REAL, "
               "min_size REAL, volume_24h REAL, programs TEXT)")
    db.execute("INSERT INTO mm_reward_params VALUES(1, 'c1', 'Wird X?', 50, 3.5, 20, 0, '')")
    db.execute("INSERT INTO mm_reward_params VALUES(2, 'c1', 'Wird X?', 100, 4, 20, 0, '')")  # latest wins
    db.commit()
    db.close()


def test_replay_scores_rewards_fills_and_walk_forward(tmp_path):
    bl, sc = str(tmp_path / "2025-10-09.sqlite"), str(tmp_path / "scenario-mm_x.sqlite")
    _booklog(bl)
    _scen(sc)
    assert replaylab.reward_params([sc]) == {"c1": (100.0, 0.04, 20.0)}
    res = replaylab.run([bl], [sc], block_h=1)
    assert res["markets"] == 1 and res["blocks"] == 4 and len(res["walk_forward"]) == 3
    rows = {r["rule"]: r for r in res["rules"]}
    near = rows[replaylab.Rule(0.25, False, 0.05, 0.95, 0.0).name]
    far = rows[replaylab.Rule(0.9, False, 0.05, 0.95, 0.0).name]
    assert near["rewards60"] > 0 and near["fills"] >= 1 and near["m60"] < 0 and near["m60bid"] <= near["m60"]
    assert far["fills"] == 0 and far["net"] > 0                       # 0.46 bid was never reached
    assert rows[replaylab.Rule(0.25, False, 0.05, 0.95, 14.0).name]["quote_h"] > 0   # 30 days left
    paths = replaylab.write_csvs(res, str(tmp_path / "out"))
    assert len(paths) == 5 and "Abstand" in open(paths[0], encoding="utf-8-sig").read()
    split = {(r["rule"], r["part"]): r for r in res["split"] if r["scenario"] == "mm_x"}
    inside = split[(replaylab.BASELINE.name, "bespielt")]
    assert inside["markets"] == 1 and split[(replaylab.BASELINE.name, "übrige")]["markets"] == 0
    assert "bespielt" in open(paths[3], encoding="utf-8-sig").read()
    # held instead of sold an hour later: nobody buys at our sell order, so it is valued at the last bid (0.47)
    assert near["hold"] < 0 and near["sfills"] >= 1 and abs(near["net_hold"] - near["rewards60"] - near["hold"]) < 1e-9
    assert "Halten" in open(paths[2], encoding="utf-8-sig").read()


def test_hold_exit_sells_to_a_trade_above_the_sell_order(tmp_path):
    bl, sc = str(tmp_path / "2025-10-09.sqlite"), str(tmp_path / "scenario-mm_x.sqlite")
    _booklog(bl, after=0.50, trades={130: 0.53})  # crash fills us at 0.49, later someone buys at 0.53
    _scen(sc)
    r = replaylab.Rule(0.25, False, 0.05, 0.95, 0.0)
    row = replaylab.run([bl], [sc], rules=[r], block_h=1)["rules"][0]
    # sell order at mid 0.50 + 1 tick = 0.51: bought at 0.49 (and 0.48 on NO via the same crash), sold at 0.51
    assert row["fills"] >= 1 and row["hold"] > 0 and row["hold"] > row["m60bid"]


def test_pause_after_a_jump_skips_quotes(tmp_path):
    bl, sc = str(tmp_path / "2025-10-09.sqlite"), str(tmp_path / "scenario-mm_x.sqlite")
    _booklog(bl, after=0.44)  # 6 ticks: pause
    _scen(sc)
    res = replaylab.run([bl], [sc], rules=[replaylab.Rule(0.5, False, 0.05, 0.95, 0.0),
                                           replaylab.Rule(0.5, True, 0.05, 0.95, 0.0)], block_h=1)
    off, on = res["rules"]
    assert on["quote_h"] < off["quote_h"]


def test_favourite_only_quotes_one_side_for_fewer_rewards(tmp_path):
    bl, sc = str(tmp_path / "2025-10-09.sqlite"), str(tmp_path / "scenario-mm_x.sqlite")
    _booklog(bl, base=0.60, after=0.60, crash_at=10_000)
    _scen(sc)
    both, fav = replaylab.Rule(0.5, True, 0.05, 0.95, 0.0), replaylab.Rule(0.5, True, 0.05, 0.95, 0.0, fav=True)
    assert fav.name.endswith("nur Favorit") and fav in replaylab.default_rules()
    res = replaylab.run([bl], [sc], rules=[both, fav], block_h=1)
    b, f = res["rules"]
    assert f["quote_h"] == b["quote_h"] and 0 < f["rewards60"] < b["rewards60"] / 2   # one side counts a third


def test_cockpit_shows_the_replay(tmp_path):
    import json
    from arb import cockpit
    bl, sc = str(tmp_path / "2025-10-09.sqlite"), str(tmp_path / "scenario-mm_x.sqlite")
    _booklog(bl)
    _scen(sc)
    (tmp_path / "replaylab.json").write_text(json.dumps(replaylab.run([bl], [sc], block_h=1)))
    out = cockpit.replay_card(cockpit.replay_numbers(tmp_path))
    assert "Replay-Labor" in out and "Walk-forward über 3 Blöcke" in out and "(Vergleich)" in out
    assert cockpit.replay_card(None) == ""
