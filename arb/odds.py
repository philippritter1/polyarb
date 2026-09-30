"""Polymarket vs. bookmakers: are Polymarket football prices worse than the sharpest odds?

football-data.co.uk publishes free CSVs of match results with bookmaker odds, including
Pinnacle's closing odds – the market professionals treat as the best public probability.
This module downloads them, removes the bookmaker margin, links every match to the Polymarket
markets of the study ("Will X win on DATE?", "Will A vs. B end in a draw?") and measures:
  accuracy      – Brier score of Polymarket vs. Pinnacle on the same matches
  disagreement  – when the two differ, who was right, and what buying the side Pinnacle rates
                  higher on Polymarket would have returned ("sharp line" backtest)
No API key needed; runs on the server as part of `run.py study`.
"""
from __future__ import annotations

import csv
import difflib
import io
import logging
import re
import sqlite3
import time
import unicodedata
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

log = logging.getLogger("polyarb.odds")
BASE = "https://www.football-data.co.uk"
# season files (mmz4281/<season>/<code>.csv) and all-season files for calendar-year leagues (new/<code>.csv)
MAIN = ["E0", "E1", "E2", "E3", "EC", "SC0", "SC1", "D1", "D2", "I1", "I2", "SP1", "SP2", "F1", "F2", "N1", "B1",
        "P1", "T1", "G1"]
EXTRA = ["ARG", "BRA", "JPN", "MEX", "USA", "AUT", "SWZ", "DNK", "SWE", "NOR", "FIN", "IRL", "POL", "ROU", "CHN"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS odds(
  key TEXT PRIMARY KEY, day TEXT, league TEXT, home TEXT, away TEXT, result TEXT,
  p_home REAL, p_draw REAL, p_away REAL, source TEXT);
CREATE TABLE IF NOT EXISTS odds_link(
  condition_id TEXT PRIMARY KEY, odds_key TEXT, role TEXT, p_book REAL, league TEXT, score REAL);
"""

STOP = {"fc", "cf", "afc", "sc", "ac", "as", "cd", "ss", "us", "sv", "vfl", "vfb", "club", "de", "fk", "nk", "sk",
        "if", "bk", "cp", "ca", "sd", "ud", "rc", "rcd", "ec", "se", "cr", "calcio", "1", "the", "and", "bc", "tsv",
        "ssc", "acf", "ogc", "aj", "rb", "hsc", "sl", "kv", "krc", "kaa", "fsv", "spvgg", "ev", "e", "v"}
# football-data short names that normalisation alone cannot reach
ALIASES = {
    "man united": "manchester united", "man city": "manchester city", "nott m forest": "nottingham forest",
    "sp lisbon": "sporting", "sheffield weds": "sheffield wednesday", "sheffield utd": "sheffield united",
    "wolves": "wolverhampton wanderers", "west brom": "west bromwich albion", "qpr": "queens park rangers",
    "ath madrid": "atletico madrid", "ath bilbao": "athletic", "sociedad": "real sociedad", "betis": "real betis",
    "espanol": "espanyol", "vallecano": "rayo vallecano", "celta": "celta vigo", "m gladbach": "borussia monchengladbach",
    "dortmund": "borussia dortmund", "ein frankfurt": "eintracht frankfurt", "leverkusen": "bayer leverkusen",
    "fc koln": "koln", "inter": "internazionale", "milan": "milan", "az alkmaar": "az", "psv eindhoven": "psv",
    "paris sg": "paris saint germain", "st etienne": "saint etienne", "st pauli": "st pauli", "hertha": "hertha berlin",
}


def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\b\d{4}\b", " ", s)  # founding years: "FC Basel 1893"
    s = " ".join(w for w in s.split() if w not in STOP)
    return ALIASES.get(s, s)


# women's and reserve teams share the club name but not the bookmaker line of the first team
OTHER_TEAM = re.compile(r"\b(wfc|women|womens|femenino|feminino|frauen|ladies|jong|u1\d|u2\d|ii|b team)\b|\(w\)", re.I)


def similar(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if (len(a) >= 4 and a in b.split()) or (len(b) >= 4 and b in a.split()):
        return 0.85  # one side is a single word of the other ("Tottenham" / "Tottenham Hotspur")
    ta, tb = set(a.split()), set(b.split())
    ra, rb = ta - tb, tb - ta
    if ta & tb and ra and rb and difflib.SequenceMatcher(None, " ".join(sorted(ra)), " ".join(sorted(rb))).ratio() < 0.6:
        return 0.6  # shared city, different club: "Manchester United" / "Manchester City"
    overlap = len(ta & tb) / min(len(ta), len(tb))
    return max(difflib.SequenceMatcher(None, a, b).ratio(), overlap * 0.95)


def devig(odds: Iterable[Optional[float]]) -> Optional[Tuple[float, ...]]:
    """Decimal odds -> probabilities with the bookmaker margin removed (proportional method)."""
    o = list(odds)
    if any(x is None or x <= 1.0 for x in o):
        return None
    inv = [1 / x for x in o]
    s = sum(inv)
    return tuple(i / s for i in inv)


def _f(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def _day(s: str) -> Optional[str]:
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s.strip(), fmt).strftime("%Y-%m-%d")
        except (ValueError, AttributeError):
            pass
    return None


def parse_csv(text: str, league: str, since: Optional[str] = None) -> List[dict]:
    """Both layouts: season files (HomeTeam, FTR, PSCH/PSH) and all-season files (Home, Res, PSCH)."""
    out = []
    for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        day = _day(r.get("Date", ""))
        home, away = r.get("HomeTeam") or r.get("Home"), r.get("AwayTeam") or r.get("Away")
        if not day or not home or not away or (since and day < since):
            continue
        probs, source = None, ""
        for cols, src in ((("PSCH", "PSCD", "PSCA"), "Pinnacle Schluss"), (("PSH", "PSD", "PSA"), "Pinnacle"),
                          (("AvgCH", "AvgCD", "AvgCA"), "Schnitt Schluss"), (("AvgH", "AvgD", "AvgA"), "Schnitt")):
            probs = devig(_f(r.get(c)) for c in cols)
            if probs:
                source = src
                break
        if not probs:
            continue
        out.append(dict(key=f"{league}|{day}|{home}|{away}", day=day, league=league, home=home, away=away,
                        result=(r.get("FTR") or r.get("Res") or "").strip(), p_home=probs[0], p_draw=probs[1],
                        p_away=probs[2], source=source))
    return out


def season_code(day: datetime) -> str:
    y = day.year if day.month >= 7 else day.year - 1
    return f"{y % 100:02d}{(y + 1) % 100:02d}"


class OddsSync:
    def __init__(self, db: sqlite3.Connection, fetch=None):
        self.db = db
        self.db.executescript(SCHEMA)
        self.fetch = fetch or self._http

    @staticmethod
    def _http(url: str) -> str:
        import requests
        r = requests.get(url, timeout=30, headers={"User-Agent": "polyarb-study/0.1"})
        r.raise_for_status()
        return r.content.decode("utf-8-sig", errors="replace")

    def download(self, days_back: float = 120, now: Optional[float] = None) -> Dict[str, int]:
        now = time.time() if now is None else now
        since = (datetime.utcfromtimestamp(now) - timedelta(days=days_back)).strftime("%Y-%m-%d")
        seasons = sorted({season_code(datetime.utcfromtimestamp(now - d * 86400)) for d in (0, days_back)})
        urls = [(f"{BASE}/mmz4281/{s}/{c}.csv", c) for s in seasons for c in MAIN] + \
               [(f"{BASE}/new/{c}.csv", c) for c in EXTRA]
        stats = {"files": 0, "failed": 0, "matches": 0}
        for url, league in urls:
            try:
                rows = parse_csv(self.fetch(url), league, since)
            except Exception as e:  # noqa – a missing league file must not stop the others
                stats["failed"] += 1
                log.info("odds: %s not available (%s)", url, type(e).__name__)
                continue
            stats["files"] += 1
            for r in rows:
                self.db.execute("INSERT OR REPLACE INTO odds VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (r["key"], r["day"], r["league"], r["home"], r["away"], r["result"],
                                 r["p_home"], r["p_draw"], r["p_away"], r["source"]))
                stats["matches"] += 1
        self.db.commit()
        return stats

    def link(self, min_score: float = 0.8) -> Dict[str, int]:
        """Link study markets to bookmaker matches: same day (±1 for time zones), matching team names;
        a draw market must match both teams."""
        by_day: Dict[str, list] = {}
        for row in self.db.execute("SELECT key, day, league, home, away, p_home, p_draw, p_away FROM odds"):
            by_day.setdefault(row[1], []).append(row)
        markets = self.db.execute("SELECT condition_id, question, end_ts FROM markets WHERE category = 'Sport'").fetchall()
        stats = {"win": 0, "draw": 0}
        for cid, q, end_ts in markets:
            m_win = re.match(r"^Will (.+) win on (\d{4}-\d{2}-\d{2})\?$", q or "")
            m_draw = re.match(r"^Will (.+) vs\.? (.+) end in a draw\?$", q or "")
            if OTHER_TEAM.search(q or ""):
                continue
            if m_win:
                day, cands = m_win.group(2), []
                for d in _near(day):
                    for row in by_day.get(d, ()):
                        for role, team, p in (("home", row[3], row[5]), ("away", row[4], row[7])):
                            cands.append((similar(m_win.group(1), team), row, role, p))
            elif m_draw and end_ts:
                day = datetime.utcfromtimestamp(end_ts).strftime("%Y-%m-%d")
                cands = [(min(similar(m_draw.group(1), row[3]), similar(m_draw.group(2), row[4])), row, "draw", row[6])
                         for d in _near(day) for row in by_day.get(d, ())]
            else:
                continue
            if not cands:
                continue
            score, row, role, p = max(cands, key=lambda c: c[0])
            if score >= min_score:
                self.db.execute("INSERT OR REPLACE INTO odds_link VALUES(?,?,?,?,?,?)", (cid, row[0], role, p, row[2], score))
                stats["draw" if role == "draw" else "win"] += 1
        self.db.commit()
        return stats


def _near(day: str) -> List[str]:
    d = datetime.strptime(day, "%Y-%m-%d")
    return [(d + timedelta(days=k)).strftime("%Y-%m-%d") for k in (0, -1, 1)]


def sync(db_path: str, days_back: float = 120, every_h: float = 6, fetch=None, now: Optional[float] = None) -> dict:
    """Download (at most every `every_h` hours) and re-link. Called at the end of each study run."""
    now = time.time() if now is None else now
    db = sqlite3.connect(db_path)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value REAL)")
        last = db.execute("SELECT value FROM meta WHERE key='odds_ts'").fetchone()
        s = OddsSync(db, fetch)
        stats = {}
        if not last or now - last[0] > every_h * 3600:
            stats = s.download(days_back, now)
            if stats.get("files"):
                db.execute("INSERT OR REPLACE INTO meta VALUES('odds_ts', ?)", (now,))
        stats.update(s.link())
        db.commit()
        log.info("odds: %s", stats)
        return stats
    finally:
        db.close()


# ====================================================================== analysis
def compare(db_path: str, cp: str = "p_1h", fee_rate: float = 0.05, slip: float = 0.01) -> dict:
    """Polymarket price at `cp` vs. de-vigged bookmaker probability on the linked markets."""
    try:
        db = sqlite3.connect(db_path)
        rows = db.execute(f"""SELECT m.{cp}, l.p_book, m.outcome, l.role, l.league, m.question
                              FROM odds_link l JOIN markets m ON m.condition_id = l.condition_id
                              WHERE m.{cp} IS NOT NULL AND l.p_book IS NOT NULL""").fetchall()
        db.close()
    except sqlite3.OperationalError:
        rows = []
    if not rows:
        return dict(n=0)
    n = len(rows)
    brier_pm = sum((p - y) ** 2 for p, _, y, *_ in rows) / n
    brier_bk = sum((b - y) ** 2 for _, b, y, *_ in rows) / n
    groups = []
    for lo, hi, label in ((0.0, 0.02, "< 2 Punkte"), (0.02, 0.05, "2–5 Punkte"), (0.05, 0.10, "5–10 Punkte"),
                          (0.10, 1.0, "> 10 Punkte")):
        bets = []
        for p, b, y, *_ in rows:
            d = b - p
            if lo <= abs(d) < hi:
                # buy the side the bookmaker rates higher than Polymarket does
                price, won = (p, y) if d > 0 else (1 - p, 1 - y)
                buy = min(price + slip, 0.99)
                bets.append((price, won, buy + fee_rate * buy * (1 - buy)))
        if bets:
            cost = sum(c for *_, c in bets)
            groups.append(dict(label=label, n=len(bets), price=sum(b[0] for b in bets) / len(bets),
                               rate=sum(b[1] for b in bets) / len(bets), roi=(sum(b[1] for b in bets) - cost) / cost))
    return dict(n=n, brier_pm=brier_pm, brier_book=brier_bk, groups=groups, cp=cp, slip=slip,
                leagues=len({r[4] for r in rows}))
