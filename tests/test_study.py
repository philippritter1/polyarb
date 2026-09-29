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
        self.calls += 1
        return self.markets if self.calls == 1 else []

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
    markets = (exp / "studie-maerkte.csv").read_text(encoding="utf-8-sig").splitlines()
    assert len(markets) == 21 and markets[0].startswith("Ende geplant;Geschlossen;Frage")
    study = (tmp_path / "www" / "study" / "index.html").read_text(encoding="utf-8")
    assert "20 aufgelöste Märkte" in study and "<svg" in study
