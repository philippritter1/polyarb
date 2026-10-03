import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arb.study import Study, calibration, categorize, outcome_yes, price_at

END = 1_790_000_000.0


def test_helpers():
    assert outcome_yes({"outcomePrices": json.dumps(["1", "0"])}) == 1
    assert outcome_yes({"outcomePrices": json.dumps(["0", "1"])}) == 0
    assert outcome_yes({"outcomePrices": json.dumps(["0.5", "0.5"])}) is None
    hist = [{"t": 100, "p": 0.4}, {"t": 200, "p": 0.6}, {"t": 300, "p": 0.9}]
    assert price_at(hist, 250, 100) == 0.6
    assert price_at(hist, 50, 100) is None
    assert price_at(hist, 1000, 100) is None  # last point too old
    assert categorize({"question": "Highest temperature in Paris on May 3?"}) == "Wetter"
    assert categorize({"question": "Will Bitcoin reach $150k?"}) == "Krypto"
    assert categorize({"question": "Lakers vs. Celtics", "gameStartTime": "x"}) == "Sport"


class StudyClient:
    """Resolved markets: favourites at 0.95 one day before that won 19 of 20 times."""

    clob = "https://clob"

    def __init__(self, n=20):
        self.markets = []
        for i in range(n):
            won = i != 0
            self.markets.append(dict(conditionId=f"c{i}", question=f"Will thing {i} happen?",
                                     clobTokenIds=json.dumps([f"Y{i}", f"N{i}"]),
                                     outcomePrices=json.dumps(["1", "0"] if won else ["0", "1"]),
                                     endDate="2026-09-21T13:33:20Z", volumeNum=5000))
        self.markets.append(dict(conditionId="bad", question="?", clobTokenIds=json.dumps(["a", "b"]),
                                 outcomePrices=json.dumps(["0.5", "0.5"]), endDate="2026-09-21T13:33:20Z"))
        self.calls = 0

    def paged(self, path, params, max_items=1000):
        if path == "/events":
            return self.events if params.get("end_date_max", "") > "2026-09-20" else []
        self.calls += 1
        return self.markets if self.calls == 1 else []

    events = []

    def get_json(self, url, params):
        end = params["endTs"]
        return {"history": [{"t": end - 7 * 86400, "p": 0.80}, {"t": end - 86400 - 60, "p": 0.95},
                            {"t": end - 3600, "p": 0.99}]}


def test_collect_and_calibrate(tmp_path):
    db = str(tmp_path / "study.sqlite")
    cl = StudyClient()
    st = Study(cl, db)
    stats = st.collect(days_back=4, now=END + 3600)
    assert stats["new"] == 20 and stats["skipped"] == 1
    # second run: nothing new, nothing refetched
    cl.calls = 0
    assert Study(cl, db).collect(days_back=4, now=END + 3600)["new"] == 0
    cal = calibration(db)
    fav = [r for r in cal["rows"] if r["cp"] == "p_1d" and r["lo"] == 0.95][0]
    assert fav["n"] == 20 and abs(fav["price"] - 0.95) < 1e-9 and abs(fav["rate"] - 0.95) < 1e-9
    longs = [r for r in cal["rows"] if r["cp"] == "p_1d" and r["hi"] == 0.10][0]
    assert longs["n"] == 20 and abs(longs["rate"] - 0.05) < 1e-9


def test_export_section(tmp_path):
    from dashboard import build_all
    data = tmp_path / "data"
    Study(StudyClient(), str(data / "study.sqlite")).collect(days_back=4, now=END + 3600)
    cfg = {"storage": {"db_path": str(data / "polyarb.sqlite")}, "portfolio": {"starting_capital_usd": 2500},
           "scenarios": {}}
    out = tmp_path / "www" / "index.html"
    build_all(cfg, str(out))
    exp = tmp_path / "www" / "export"
    page = (exp / "index.html").read_text(encoding="utf-8")
    assert 'href="polyarb-export.zip"' in page and 'href="../trades.csv"' in page
    with zipfile.ZipFile(exp / "polyarb-export.zip") as z:
        names = set(z.namelist())
    assert {"arbitrage/trades.csv", "arbitrage/equity.csv", "studie/maerkte.csv", "studie/kalibrierung.csv"} <= names
    assert 'href="polyarb-kompakt.zip"' in page
    with zipfile.ZipFile(exp / "polyarb-kompakt.zip") as z:
        small = set(z.namelist())
    assert {"arbitrage/trades.csv", "studie/kalibrierung.csv"} <= small and "studie/maerkte.csv" not in small
    markets = (exp / "studie-maerkte.csv").read_text(encoding="utf-8-sig").splitlines()
    assert len(markets) == 21 and markets[0].startswith("Ende geplant;Geschlossen;Frage")
    study = (tmp_path / "www" / "study" / "index.html").read_text(encoding="utf-8")
    assert "20 aufgelöste Märkte" in study and "<svg" in study


def test_weather_buckets_below_main_volume_floor(tmp_path):
    cl = StudyClient(n=0)
    cl.events = [dict(id=1, title="Highest temperature in Paris on September 21?", endDate="2026-09-21T13:33:20Z",
                      markets=[dict(conditionId=f"w{i}", question=f"Will the highest temperature in Paris be {20 + i}°C?",
                                    clobTokenIds=json.dumps([f"WY{i}", f"WN{i}"]), volumeNum=v,
                                    outcomePrices=json.dumps(["1", "0"] if i == 1 else ["0", "1"]))
                               for i, v in enumerate([300, 120, 20])]),
                 dict(id=2, title="Will it snow in Paris?", markets=[])]
    stats = Study(cl, str(tmp_path / "s.sqlite")).collect(days_back=4, window_days=2, now=END + 3600)
    assert stats["weather"] == 2 and stats["new"] == 2  # the 20 $ bucket stays below the floor
    cats = calibration(str(tmp_path / "s.sqlite"))
    assert cats["n"] == 2


class WindowClient(StudyClient):
    """Counts market listings per window; every window holds one resolved market."""

    def __init__(self):
        super().__init__(n=0)
        self.listed = []

    def paged(self, path, params, max_items=1000):
        if path == "/events":
            return []
        self.listed.append(params["end_date_max"])
        i = len(self.listed)
        return [dict(conditionId=f"{params['end_date_max']}", question=f"Will thing {i} happen?",
                     clobTokenIds=json.dumps([f"Y{i}", f"N{i}"]), outcomePrices=json.dumps(["1", "0"]),
                     endDate=params["end_date_max"], volumeNum=5000)]


def test_bookmark_keeps_frequent_runs_cheap(tmp_path):
    db = str(tmp_path / "s.sqlite")
    cl = WindowClient()
    first = Study(cl, db).collect(days_back=20, window_days=2, recent_days=4, now=END)
    assert first["windows"] == 10 and first["backfill_days_left"] == 0
    cl.listed = []
    second = Study(cl, db).collect(days_back=20, window_days=2, recent_days=4, now=END + 1800)
    assert second["windows"] == 2  # only the recent days are listed again, the backfill is done


def test_backfill_resumes_after_max_new(tmp_path):
    db = str(tmp_path / "s.sqlite")
    cl = WindowClient()
    a = Study(cl, db).collect(days_back=20, window_days=2, recent_days=4, max_new=5, now=END)
    assert a["new"] == 5 and a["backfill_days_left"] > 0
    b = Study(cl, db).collect(days_back=20, window_days=2, recent_days=4, max_new=100, now=END)
    assert b["backfill_days_left"] == 0
    import sqlite3
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM markets").fetchone()[0] == 10


def test_stress_test_rules():
    from arb.backtest import RULES, run_rule
    rule = {r["key"]: r for r in RULES}
    # 200 sport games: underdog side at 5 % wins 10 % of the time; favourites at 97 % win 95 %
    rows = []
    for i in range(200):
        won_underdog = 1 if i % 10 == 0 else 0
        rows.append((f"Team {i} vs. Team {i + 1}: winner", "Sport", won_underdog, float(i), 8000.0,
                     None, None, 0.05, 0.05))
    under = run_rule(rows, rule["underdog_6h"], n_boot=200)
    assert under["n"] == 200 and abs(under["hit"] - 0.10) < 1e-9 and under["roi"]["0.00"] > 0.8
    assert under["roi"]["0.05"] < under["roi"]["0.00"]  # every cent of surcharge costs
    assert under["games"] == 200 and 0 <= under["p_loss"] <= 1 and under["dd_p95"] > 0
    fav = run_rule(rows, rule["endgame_1h"], n_boot=200)  # the same markets seen from the 95 % side
    assert fav["roi"]["0.00"] < 0
    # category and volume windows: sport rows never count for the weather rule
    assert run_rule(rows, rule["weather_no_6h"], n_boot=50)["n"] == 0


def test_weather_with_game_start_time_is_not_sport(tmp_path):
    import sqlite3
    from arb.study import SCHEMA, fix_categories
    from arb.backtest import game_key
    wx = {"question": "Will the highest temperature in Paris be 24°C on September 3?", "gameStartTime": "2026-09-03"}
    assert categorize(wx) == "Wetter"
    assert categorize({"question": "Bitcoin Up or Down - September 3, 4PM ET", "gameStartTime": "x"}) == "Krypto"
    assert categorize({"question": "Lakers vs. Celtics", "gameStartTime": "x"}) == "Sport"
    db = sqlite3.connect(str(tmp_path / "s.sqlite"))
    db.executescript(SCHEMA)
    for cid, q in (("a", wx["question"]), ("b", "Lakers vs. Celtics")):
        db.execute("INSERT INTO markets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (cid, q, "Sport", 0, 1, 100, 0, 0, 0, None, None, 0.5, 0.5, 5, 0))
    assert fix_categories(db) == 1
    assert dict(db.execute("SELECT condition_id, category FROM markets")) == {"a": "Wetter", "b": "Sport"}
    # all buckets of one city and day are one event for the bootstrap
    assert game_key(wx["question"]) == game_key("Will the highest temperature in Paris be 26°C or higher on September 3?")
    assert game_key(wx["question"]) != game_key("Will the highest temperature in Paris be 24°C on September 4?")


def test_categories_v2_whole_words_finance_and_social(tmp_path):
    import sqlite3
    from arb.study import SCHEMA, fix_categories, market_type, sport_kind
    gs = {"gameStartTime": "x"}
    # whole words only: "rain" in Ukraine / Rainbow Six, "eth" in Elizabeth
    assert categorize(dict(gs, question="Hungary vs. Ukraine: O/U 2.5")) == "Sport"
    assert categorize({"question": "Rainbow Six Siege: FaZe Clan vs LOS (BO3) - Playoffs"}) == "Sport"
    assert categorize(dict(gs, question="ITF Granby: Elizabeth Mandlik vs Anna Kalinskaya")) == "Sport"
    assert categorize({"question": "Will it rain in NYC on July 4?"}) == "Wetter"
    # finance and tweet counts carry a gameStartTime but are no sport
    assert categorize(dict(gs, question="WTI Crude Oil (WTI) closes above $94 on September 30?")) == "Finanz"
    assert categorize(dict(gs, question="Will Google (GOOGL) close above $340 end of September?")) == "Finanz"
    assert categorize({"question": "S&P 500 (SPX) Up or Down on September 24?"}) == "Finanz"
    assert categorize({"question": "Bitcoin Up or Down - September 3, 4PM ET"}) == "Krypto"
    assert categorize(dict(gs, question="Will Elon Musk post 40-64 tweets from September 28 to September 30?")) == "Social"
    # brackets alone are no ticker
    assert categorize(dict(gs, question="ITF MEN: M15 Champaign, IL (USA), hard: A Farzam vs M Arseneault")) == "Sport"
    assert sport_kind("Counter-Strike: Leo Team vs mellren - Map 4 Winner") == "Esports"
    assert sport_kind("W35 Kyoto: Kisa Yoshioka vs Jiangxue Han") == "Tennis"
    assert sport_kind("Spread: New York Yankees (-1.5)") == "US"
    assert sport_kind("AS Roma vs. FC Barcelona: O/U 1.5") == "Fussball"
    assert market_type("AS Roma vs. FC Barcelona: O/U 1.5") == "Ueber/Unter"
    assert market_type("Will AS Roma vs. FC Barcelona end in a draw?") == "Remis"
    assert market_type("Will FC Barcelona win on 2026-09-30?") == "Sieg"
    assert market_type("Exact Score: AS Roma 2 - 3 FC Barcelona?") == "Exact Score"
    assert market_type("Will Robinhood (HOOD) beat quarterly earnings?") == "Quartalszahlen"
    # stored rows are re-filed once per categories version
    db = sqlite3.connect(str(tmp_path / "s.sqlite"))
    db.executescript(SCHEMA)
    for cid, q, cat, sp in (("a", "WTI Crude Oil (WTI) closes above $94 on September 30?", "Sport", 1),
                            ("b", "Rainbow Six Siege: M80 vs Wildcard Gaming (BO3)", "Wetter", 0),
                            ("c", "Lakers vs. Celtics", "Sport", 1)):
        db.execute("INSERT INTO markets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (cid, q, cat, 0, sp, 100, 0, 0, 0, None, None, 0.5, 0.5, 5, 0))
    assert fix_categories(db) == 2
    assert dict(db.execute("SELECT condition_id, category FROM markets")) == {"a": "Finanz", "b": "Sport", "c": "Sport"}
    assert fix_categories(db) == 0


def test_backtest_rules_with_types_and_no_side():
    from arb.backtest import RULES, run_rule
    rule = {r["key"]: r for r in RULES}
    rows = []
    for i in range(40):  # O/U underdogs at 0.10 that win 1 in 4, plain "who wins" underdogs that never win
        rows.append((f"Team{i} FC vs. Club{i}: O/U 2.5", "Sport", int(i % 4 == 0), i, 5000, None, None, 0.10, None))
        rows.append((f"Will Team{i} FC win on 2026-09-{i % 28 + 1:02d}?", "Sport", 0, i, 5000, None, None, 0.10, None))
    r = run_rule(rows, rule["fussball_dog_6h"], n_boot=50)
    assert r["n"] == 40 and r["hit"] == 0.25  # only the O/U side, the win markets are filtered out
    wx = [(f"Will the highest temperature in City{i} be 20°C on July {i % 28 + 1}?", "Wetter", 0, i, 800,
           None, None, 0.30, None) for i in range(40)]
    r = run_rule(wx, rule["wetter_no_breit_6h"], n_boot=50)
    assert r["n"] == 40 and abs(r["price"] - 0.70) < 1e-9 and r["hit"] == 1.0  # NO side at 0.70 only, never the YES


def test_crypto_is_not_collected_and_listing_v2_rewalks(tmp_path):
    from arb.study import LISTING_VERSION
    db = str(tmp_path / "s.sqlite")
    st = Study(StudyClient(n=0), db)
    st._meta("backfill_until", 123.0)
    st.db.execute("UPDATE meta SET value=1 WHERE key='listing_version'")
    st.db.commit()
    st2 = Study(StudyClient(n=0), db)  # older listing version: the history is walked again
    assert st2._meta("backfill_until") is None and st2._meta("listing_version") == LISTING_VERSION
    m = dict(conditionId="c1", question="Bitcoin Up or Down - September 21, 4PM ET", clobTokenIds=json.dumps(["Y", "N"]),
             outcomePrices=json.dumps(["1", "0"]), endDate="2026-09-21T13:33:20Z", volumeNum=50000)
    stats = {"new": 0, "skipped": 0, "seen": 0, "weather": 0, "windows": 0}

    class One(StudyClient):
        def paged(self, path, params, max_items=1000):
            return [m] if path == "/markets" else []

    st2.client = One(n=0)
    st2._window(0, 1, set(), stats, 10, 1000, 0, END)
    assert stats["skipped"] == 1 and stats["new"] == 0
    assert st2.db.execute("SELECT reason FROM skipped").fetchone()[0] == "Krypto (nicht gesammelt)"
