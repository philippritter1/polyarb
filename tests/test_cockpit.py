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


def test_calibration_card_compares_per_filled_dollar():
    from arb.cockpit import calibration_card
    rows = [dict(key="mm_sofort", label="sofort", n=dict(filled=1500.0, rewards=900.0, trading=-170.0, m60=-110.0, days=2.5))]
    live = dict(filled=121.0, pnl_total=-23.7, paid_total=2.67, today=2.84)
    out = calibration_card(rows, live)
    real_rate = (-23.7 - 5.51) / 121.0
    assert "Echt (Generalprobe)" in out and "sofort" in out
    assert f"{(900 + 1500 * real_rate) / 2.5:+.1f}".replace(".", ",") in out


def test_calibration_uses_the_probes_final_result_when_it_is_off():
    from arb.cockpit import calibration_card
    rows = [dict(key="mm_sofort", label="sofort", n=dict(filled=1000.0, rewards=500.0, trading=-60.0, m60=-50.0, days=2.0))]
    out = calibration_card(rows, dict(filled=None, pnl_total=None))
    assert "Echt (Generalprobe)" in out and f"{(500 + 1000 * -29.2 / 121) / 2:+.1f}".replace(".", ",") in out


def test_decision_needs_both_halves_over_the_threshold(tmp_path):
    from arb.cockpit import decision_card, decision_rows
    db = str(tmp_path / "a.sqlite")
    _db(db, [2500, 2510, 2530, 2540, 2560], 0.0)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE mm_markout(ts REAL, cid TEXT, token TEXT, side TEXT, price REAL, qty REAL, m5 REAL, m60 REAL)")
    con.execute("INSERT INTO mm_markout VALUES(?, 'c', 't', 'yes', 0.5, 200, 0, 0)", (T + 1800,))       # 100 $, 1st half
    con.execute("INSERT INTO mm_markout VALUES(?, 'c', 't', 'yes', 0.5, 200, 0, 0)", (T + 3 * 3600,))   # 100 $, 2nd half
    con.execute("INSERT INTO settlements VALUES(?, 'x', 'reward', 0, 100, 100)", (T + 3 * 3600,))       # 2nd half only
    con.commit()
    con.close()
    n = cockpit.scenario_numbers(db, 2500, {})
    assert len(n["halves"]) == 2 and n["halves"][0]["filled"] == 100 and n["halves"][1]["rewards"] == 100
    rows = [dict(key="a", label="A", n=n)]
    d = decision_rows(rows)[0]
    assert d["h1"] < 0.25 < d["h2"] and d["verdict"] != "ja"   # one good half is not enough
    assert "Kein Szenario besteht beide Hälften" in decision_card(rows, None) or d["verdict"] == "zu wenig Daten"
    n["halves"][0]["rewards"] = 100
    assert decision_rows(rows)[0]["verdict"] == "zu wenig Daten"  # 20.10.: 200 $ filled is no basis for a "ja"
    assert "Noch kein Urteil" in decision_card(rows, None)
    n["filled"] = 1200.0
    n["rewards"] = 600.0
    assert decision_rows(rows)[0]["verdict"] == "ja"
