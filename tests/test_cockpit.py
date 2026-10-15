import json
import sqlite3

from arb import cockpit
from arb.storage import SCHEMA

T = cockpit.SINCE + 3600


def _db(path, rows, rewards):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute("CREATE TABLE mm_metrics(ts REAL, pool_selected REAL, pool_quoted REAL, rewards_per_day REAL, "
               "share_pct REAL, realized REAL, rewards REAL, rebates REAL, inventory REAL, markets INT, quotes INT, fills INT)")
    for i, eq in enumerate(rows):
        db.execute("INSERT INTO equity VALUES(?,?,0,0,0,0,NULL)", (T + i * 3600, eq))
    db.execute("INSERT INTO settlements VALUES(?, 'x:rewards', 'reward', 0, ?, ?)", (T + 1800, rewards, rewards))
    db.execute("INSERT INTO mm_metrics VALUES(?,0,0,24,3.5,0,0,0,0,0,0,0)", (T + 3600,))
    db.commit()
    db.close()


def test_cockpit_splits_rewards_and_trading_and_renders(tmp_path):
    _db(str(tmp_path / "a.sqlite"), [2500, 2510, 2530], 40.0)  # +30 net, of which 40 rewards -> trading −10
    n = cockpit.scenario_numbers(str(tmp_path / "a.sqlite"), 2500, {"Bestand $": 250})
    assert abs(n["net"] - 30) < 1e-9 and abs(n["rewards"] - 40) < 1e-9 and abs(n["trading"] + 10) < 1e-9
    assert abs(n["trade_per_rew"] + 0.25) < 1e-9 and n["share"] == 3.5 and abs(n["inv_pct"] - 0.1) < 1e-9
    assert cockpit.scenario_numbers(str(tmp_path / "none.sqlite"), 2500, {}) is None
    (tmp_path / "live-status-mm_probe.json").write_text(json.dumps(
        dict(live=True, equity=84.6, cash=81.2, positions=3.4, fills=5, scoring="9/9", rewards_yesterday=0.19)))
    live = cockpit.live_numbers(tmp_path)
    body = cockpit.page_body([dict(key="a", label="MM A", n=n), dict(key="b", label="MM B", n=None)], live)
    assert "Woher kommt das Ergebnis?" in body and "MM A" in body and "MM B" not in body
    assert "84,60 $" in body and "<svg" in body and "−0,25" in body and "var(--s2)" in body
