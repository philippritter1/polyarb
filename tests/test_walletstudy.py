import csv
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arb.walletstudy import DATA, run

NOW = 1_792_000_000.0
MAKER, TRADER, OWN = "0xmaker", "0xtrader", "0xown"


def _db(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE mm_reward_params(ts REAL, cid TEXT, title TEXT, daily_rate REAL, max_spread_ct REAL,"
               " min_size REAL, volume_24h REAL, programs TEXT)")
    for i, cid in enumerate(("c1", "c2", "c3")):
        db.execute("INSERT INTO mm_reward_params VALUES(?,?,?,?,?,?,?,?)", (NOW - i, cid, "t", 50, 3, 20, 1000, ""))
    db.commit()
    db.close()


def fake_get(url, params):
    if url == f"{DATA}/trades":
        cid = params["market"]
        rows = [dict(proxyWallet=MAKER, outcome="Yes", size=100, price=0.5, name="mm"),
                dict(proxyWallet=MAKER, outcome="No", size=100, price=0.5, name="mm"),
                dict(proxyWallet=OWN, outcome="Yes", size=10, price=0.5)]
        if cid == "c1":
            rows.append(dict(proxyWallet="0xone", outcome="Yes", size=5, price=0.4))
        return rows
    if url == f"{DATA}/activity":
        if params.get("type") == "REWARD":
            return [dict(type="REWARD", usdcSize=30, timestamp=NOW - 10 * 86400), dict(type="REWARD", usdcSize=20, timestamp=NOW - 86400)]
        if params.get("type") == "TRADE":
            return [dict(type="TRADE", side="BUY", conditionId="w1", timestamp=NOW - 86400 * 2)]
        return [dict(type="TRADE", usdcSize=500, timestamp=NOW - 5 * 86400), dict(type="TRADE", usdcSize=500, timestamp=NOW),
                dict(type="MERGE", usdcSize=10, timestamp=NOW - 86400)]
    if url == f"{DATA}/positions":
        return [dict(currentValue=40, cashPnl=-5, title="Will the Fed cut rates?", endDate="2026-12-31T00:00:00Z")]
    if url.endswith("/v1/leaderboard"):
        raise RuntimeError("404")  # the first leaderboard URL fails: the second one is used
    if url.endswith("/profit"):
        return [dict(proxyWallet=TRADER, amount=1234, name="pro"), dict(proxyWallet=OWN, amount=5)]
    if url == f"{DATA}/closed-positions":
        return [dict(realizedPnl=10, avgPrice=0.93, title="Will X happen?", conditionId="w1", endDate=NOW + 86400 * 1),
                dict(realizedPnl=-3, avgPrice=0.15, title="Will Bitcoin hit 100k?", conditionId="w2")]
    raise AssertionError(url)


def test_wallet_study_finds_makers_and_traders_and_writes_csvs(tmp_path):
    db = str(tmp_path / "scenario-mm_x.sqlite")
    _db(db)
    res = run(fake_get, [db], str(tmp_path), exclude=[OWN], n_makers=5, n_traders=5, now=NOW)
    assert res["markets"] == 3 and res["makers"] == 2 and res["traders"] == 1  # own wallet left out
    rows = list(csv.reader(open(tmp_path / "wallet-maker.csv", encoding="utf-8-sig"), delimiter=";"))
    top = rows[1]
    assert top[0] == MAKER and top[2] == "3" and top[5] == "100"   # all 3 markets, both outcomes everywhere
    assert top[6] == "50,00" and top[7] == "5,00"                   # 50 $ rewards over 10 days
    assert top[8] == "200" and top[9] == "2,5" and top[10] == "1"   # 1000 $ over 5 days; rewards per 1 $ traded; 1 merge
    tr = list(csv.reader(open(tmp_path / "wallet-trader.csv", encoding="utf-8-sig"), delimiter=";"))[1]
    assert tr[0] == TRADER and tr[2] == "1234" and tr[5] == "ja" and tr[6] == "50" and tr[8] == "50" and tr[9] == "50"
    assert tr[10] == "72"                                            # bought 72 h before the end
    diag = open(tmp_path / "wallet-diagnose.csv", encoding="utf-8-sig").read()
    assert "/v1/leaderboard" in diag and "RuntimeError" in diag and "/trades" in diag
