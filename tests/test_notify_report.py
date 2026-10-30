"""Short 4-h report (31.10.): running scenarios one line each, equity at 60 % rewards; winding-down books summed."""
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy"))


def _book(path, rows):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE equity(ts REAL, equity REAL, realized_cum REAL, trade_cum REAL, rewards_cum REAL, "
               "rebates_cum REAL)")
    db.executemany("INSERT INTO equity VALUES(?,?,0,0,?,0)", rows)
    db.commit()
    db.close()


def test_short_report_lists_running_books_and_sums_the_winding_down(tmp_path, monkeypatch):
    import notify
    now = time.time()
    a, b, c = (str(tmp_path / f"scenario-{n}.sqlite") for n in ("a", "b", "c"))
    _book(a, [(now - 3 * 86400, 2500, 0), (now - 86400 - 60, 2550, 50), (now - 4 * 3600 - 60, 2600, 100),
              (now - 10, 2700, 200)])                      # 60 %: 2620 now, 2560 4 h ago, 2530 a day ago
    _book(b, [(now - 86400 - 60, 2480, 0), (now - 10, 2470, 0)])
    _book(c, [(now - 86400 - 60, 2400, 0), (now - 10, 2300, 0)])
    monkeypatch.setattr(notify, "books", lambda: [
        dict(label="Market Making (Schutz + lernend)", db=a, start=2500.0),
        dict(label="Wetter-Sniper", db=b, start=2500.0),
        dict(label="Market Making (Rewards)", db=c, start=2500.0, wind_down=True)])
    sent = []
    monkeypatch.setattr(notify, "push", lambda title, msg, *a, **k: sent.append((title, msg)))
    notify.overview(4, "Polyarb 4h-Bericht")
    msg = sent[0][1]
    lines = msg.splitlines()
    assert lines[1] == "▲ MM Schutz + lernend: $120.00 | $60.00 | $90.00"
    assert lines[2] == "▼ Wetter-Sniper: -$30.00 | -$10.00 | -$10.00"
    assert lines[3].startswith("Auslaufend (1): -$200.00 gesamt, -$100.00 in 24h")
    assert "Rebates" not in msg and "Gesamt realisiert" not in msg and len(lines) == 4
