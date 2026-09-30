import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arb.odds import OddsSync, compare, devig, parse_csv, similar, sync
from arb.study import SCHEMA

# real Polymarket names (from the study) vs. football-data.co.uk spellings
SAME = [("Manchester United FC", "Man United"), ("Nottingham Forest FC", "Nott'm Forest"),
        ("Wolverhampton Wanderers FC", "Wolves"), ("Tottenham Hotspur FC", "Tottenham"),
        ("Brighton & Hove Albion FC", "Brighton"), ("FC Bayern München", "Bayern Munich"),
        ("Bayer 04 Leverkusen", "Leverkusen"), ("BV Borussia 09 Dortmund", "Dortmund"),
        ("Borussia Mönchengladbach", "M'gladbach"), ("1. FC Köln", "FC Koln"), ("FC Internazionale Milano", "Inter"),
        ("SSC Napoli", "Napoli"), ("Club Atlético de Madrid", "Ath Madrid"), ("Real Betis Balompié", "Betis"),
        ("Sporting CP", "Sp Lisbon"), ("Sport Lisboa e Benfica", "Benfica"), ("PSV", "PSV Eindhoven"),
        ("Paris Saint-Germain FC", "Paris SG"), ("Queens Park Rangers FC", "QPR"), ("Galatasaray SK", "Galatasaray")]
DIFFERENT = [("Manchester United FC", "Man City"), ("Real Madrid CF", "Sociedad"), ("Inter Miami CF", "Inter"),
             ("Aston Villa FC", "Villarreal")]


def test_team_names():
    for pm, fd in SAME:
        assert similar(pm, fd) >= 0.8, (pm, fd, similar(pm, fd))
    for pm, fd in DIFFERENT:
        assert similar(pm, fd) < 0.8, (pm, fd, similar(pm, fd))
    assert similar("Paris FC", "Paris FC") > similar("Paris FC", "Paris SG")


def test_devig_and_both_csv_layouts():
    p = devig([2.0, 3.5, 4.0])
    assert abs(sum(p) - 1) < 1e-12 and p[0] > p[1] > p[2]
    season = ("Div,Date,HomeTeam,AwayTeam,FTR,PSH,PSD,PSA,PSCH,PSCD,PSCA\n"
              "E0,20/09/2026,Man United,Chelsea,H,2.1,3.4,3.6,2.0,3.5,3.9\n"
              "E0,20/09/2026,Wolves,Brighton,A,,,,,,\n")
    rows = parse_csv(season, "E0")
    assert len(rows) == 1 and rows[0]["source"] == "Pinnacle Schluss" and rows[0]["result"] == "H"
    extra = ("Country,League,Season,Date,Time,Home,Away,HG,AG,Res,PSCH,PSCD,PSCA,AvgCH,AvgCD,AvgCA\n"
             "Brazil,Serie A,2026,21/09/2026,20:00,Palmeiras,Santos,1,1,D,,,,1.8,3.6,4.8\n")
    rows = parse_csv(extra, "BRA")
    assert rows[0]["home"] == "Palmeiras" and rows[0]["source"] == "Schnitt Schluss"


def _study(tmp_path):
    db = sqlite3.connect(str(tmp_path / "study.sqlite"))
    db.executescript(SCHEMA)
    from datetime import datetime, timezone
    end = datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc).timestamp()
    q = [("w1", "Will Manchester United FC win on 2026-09-20?", 1, 0.40),
         ("w2", "Will Chelsea FC win on 2026-09-20?", 0, 0.38),
         ("d1", "Will Manchester United FC vs. Chelsea FC end in a draw?", 0, 0.22),
         ("w3", "Will Manchester City WFC win on 2026-09-20?", 1, 0.70),   # women's team: never linked
         ("w4", "Will Brighton & Hove Albion FC win on 2026-09-20?", 0, 0.30)]
    for cid, question, out, p in q:
        db.execute("INSERT INTO markets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (cid, question, "Sport", 1, 1, 9000, end, end, out, None, p, p, p, 50, 0))
    db.commit()
    return db


CSV = ("Div,Date,HomeTeam,AwayTeam,FTR,PSCH,PSCD,PSCA\n"
       "E0,20/09/2026,Man United,Chelsea,H,2.0,3.5,3.9\n"
       "E0,20/09/2026,Man City,Brighton,H,1.5,4.5,6.5\n")


def test_link_and_compare(tmp_path):
    db = _study(tmp_path)
    s = OddsSync(db, fetch=lambda url: CSV if url.endswith("/2627/E0.csv") else (_ for _ in ()).throw(IOError()))
    stats = s.download(days_back=30, now=1_790_300_000.0)
    assert stats["matches"] == 2 and stats["failed"] > 0
    assert s.link() == {"win": 3, "draw": 1}
    roles = dict(db.execute("SELECT condition_id, role FROM odds_link").fetchall())
    assert roles == {"w1": "home", "w2": "away", "d1": "draw", "w4": "away"}
    db.close()
    c = compare(str(tmp_path / "study.sqlite"), cp="p_1h")
    assert c["n"] == 4 and c["leagues"] == 1 and 0 < c["brier_book"] < 1
    assert sum(g["n"] for g in c["groups"]) == 4


def test_sync_downloads_at_most_every_6h(tmp_path):
    _study(tmp_path).close()
    calls = []
    fetch = lambda url: calls.append(url) or CSV
    sync(str(tmp_path / "study.sqlite"), days_back=30, fetch=fetch, now=1_790_300_000.0)
    n = len(calls)
    sync(str(tmp_path / "study.sqlite"), days_back=30, fetch=fetch, now=1_790_300_000.0 + 3600)
    assert n > 0 and len(calls) == n  # second run within 6 h: only re-linking
