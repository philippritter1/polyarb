"""Sharp line: buy on Polymarket when it is clearly cheaper than the bookmaker professionals use.

Pinnacle's de-vigged pre-match odds are the best public estimate of a football result. The study
(Polymarket vs. Pinnacle closing odds, 9,800 markets) found Polymarket as accurate overall, but the rare
large gaps (> 10 points) paid +25-39 % – with closing odds, which are not known before kick-off, and with
Polymarket prices that may have been stale. This scenario tests the idea live: current Pinnacle odds from
The Odds API, the real Polymarket ask, buy before kick-off when the ask lies at least `min_edge` below
the bookmaker probability.

The Odds API (https://the-odds-api.com): the key comes from ODDS_API_KEY in /etc/polyarb.env or from
data/secrets.env, which the settings page of the dashboard (/admin/, deploy/admin.py) writes – read again on
every step, so a new key works without a restart. The free plan has
500 credits a month. /sports/{league}/events is free; /sports/{league}/odds costs 1 credit per region and
market (here bookmakers=pinnacle, markets=h2h -> 1 credit), an empty answer costs nothing. Odds are fetched
per league only when one of its matches starts within `odds_lookahead_h`, at most every `odds_refresh_h`
hours, and never beyond a daily share of the monthly cap – the budget lasts the whole month.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .client import _parse_json_list, _parse_ts
from .odds import OTHER_TEAM, devig, similar
from .scenarios import Signal, Strategy, _iso

log = logging.getLogger("polyarb.sharp")
ODDS_BASE = "https://api.the-odds-api.com/v4"
LEAGUES = ["soccer_epl", "soccer_germany_bundesliga", "soccer_spain_la_liga", "soccer_italy_serie_a",
           "soccer_france_ligue_one", "soccer_uefa_champs_league", "soccer_netherlands_eredivisie",
           "soccer_portugal_primeira_liga"]
WIN_RE = re.compile(r"^Will (.+) win on (\d{4}-\d{2}-\d{2})\?$")
DRAW_RE = re.compile(r"^Will (.+) vs\.? (.+) end in a draw\?$")


class OddsFeed:
    """Pinnacle h2h probabilities per upcoming match, cached on disk with the credit budget."""

    def __init__(self, get_json, key: str, state_path: str, leagues: List[str], monthly_cap: int = 450,
                 refresh_h: float = 4, lookahead_h: float = 8, events_every_min: float = 30):
        self.get_json, self.key, self.path = get_json, key, state_path
        self.leagues, self.cap = leagues, int(monthly_cap)
        self.refresh, self.lookahead, self.events_every = refresh_h * 3600, lookahead_h * 3600, events_every_min * 60
        self.state = {"used": {}, "leagues": {}}
        try:
            with open(state_path, encoding="utf-8") as f:
                self.state.update(json.load(f))
        except (OSError, ValueError):
            pass
        self.stats: Dict[str, int] = {}

    def _save(self) -> None:
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def used(self, now: float, day: bool = False) -> int:
        fmt = "%Y-%m-%d" if day else "%Y-%m"
        k = datetime.fromtimestamp(now, tz=timezone.utc).strftime(fmt)
        return int(self.state["used"].get(k, 0))

    def _spend(self, now: float, n: int) -> None:
        for fmt in ("%Y-%m", "%Y-%m-%d"):
            k = datetime.fromtimestamp(now, tz=timezone.utc).strftime(fmt)
            self.state["used"][k] = self.state["used"].get(k, 0) + n

    def _budget_left(self, now: float) -> bool:
        daily = max(1, self.cap // 30)
        return self.used(now) < self.cap and self.used(now, day=True) < daily

    def matches(self, now: float) -> List[dict]:
        self.stats = {}
        out = []
        for lg in self.leagues:
            st = self.state["leagues"].setdefault(lg, {"events_ts": 0, "next": [], "odds_ts": 0, "matches": []})
            if now - st["events_ts"] >= self.events_every:  # free: which matches start soon
                try:
                    evs = self.get_json(f"{ODDS_BASE}/sports/{lg}/events", {"apiKey": self.key}) or []
                    st["next"] = sorted(_parse_ts(e.get("commence_time")) or 0 for e in evs if isinstance(e, dict))
                    st["events_ts"] = now
                except Exception as e:  # noqa – unknown league, network: try again later
                    self.stats["Fehler Spielplan"] = self.stats.get("Fehler Spielplan", 0) + 1
                    log.debug("odds events %s: %s", lg, e)
            soon = [t for t in st["next"] if now < t <= now + self.lookahead]
            if soon and now - st["odds_ts"] >= self.refresh:
                if not self._budget_left(now):
                    self.stats["Credit-Budget erreicht"] = self.stats.get("Credit-Budget erreicht", 0) + 1
                else:
                    try:
                        data = self.get_json(f"{ODDS_BASE}/sports/{lg}/odds", {
                            "apiKey": self.key, "bookmakers": "pinnacle", "markets": "h2h", "oddsFormat": "decimal",
                            "commenceTimeFrom": _iso(now), "commenceTimeTo": _iso(now + self.lookahead)}) or []
                        if data:
                            self._spend(now, 1)
                        st["matches"] = [m for m in (parse_match(e, lg) for e in data) if m]
                        st["odds_ts"] = now
                        self.stats["Quoten abgefragt"] = self.stats.get("Quoten abgefragt", 0) + 1
                    except Exception as e:  # noqa
                        self.stats["Fehler Quoten"] = self.stats.get("Fehler Quoten", 0) + 1
                        log.warning("odds %s: %s", lg, e)
            out += [dict(m, odds_ts=st["odds_ts"]) for m in st["matches"] if m["start"] > now]
        self.stats["Credits Monat"] = self.used(now)
        self.stats["Credits heute"] = self.used(now, day=True)
        self._save()
        return out


def parse_match(ev: dict, league: str) -> Optional[dict]:
    """One Odds API event -> {home, away, start, p_home, p_draw, p_away} from Pinnacle's h2h line."""
    home, away = ev.get("home_team"), ev.get("away_team")
    start = _parse_ts(ev.get("commence_time"))
    for bk in ev.get("bookmakers") or []:
        if bk.get("key") != "pinnacle":
            continue
        for mk in bk.get("markets") or []:
            if mk.get("key") != "h2h":
                continue
            price = {o.get("name"): o.get("price") for o in mk.get("outcomes") or []}
            probs = devig([price.get(home), price.get("Draw"), price.get(away)])
            if probs and start:
                return dict(league=league, home=home, away=away, start=start,
                            p_home=probs[0], p_draw=probs[1], p_away=probs[2])
    return None


def link(question: str, end_ts: Optional[float], matches: List[dict], min_score: float = 0.85):
    """(match, probability that YES wins) for "Will X win on DATE?" / "Will A vs. B end in a draw?"."""
    if OTHER_TEAM.search(question or ""):
        return None
    m_win, m_draw = WIN_RE.match(question or ""), DRAW_RE.match(question or "")
    best = None
    for mt in matches:
        day = datetime.fromtimestamp(mt["start"], tz=timezone.utc).strftime("%Y-%m-%d")
        if m_win:
            if abs((datetime.strptime(m_win.group(2), "%Y-%m-%d") - datetime.strptime(day, "%Y-%m-%d")).days) > 1:
                continue
            for team, p in ((mt["home"], mt["p_home"]), (mt["away"], mt["p_away"])):
                s = similar(m_win.group(1), team)
                if s >= min_score and (best is None or s > best[0]):
                    best = (s, mt, p)
        elif m_draw:
            if end_ts and abs(end_ts - mt["start"]) > 2 * 86400:
                continue
            s = min(similar(m_draw.group(1), mt["home"]), similar(m_draw.group(2), mt["away"]))
            if s >= min_score and (best is None or s > best[0]):
                best = (s, mt, mt["p_draw"])
    return (best[1], best[2]) if best else None


def read_key(secrets_path: str, env_name: str = "ODDS_API_KEY") -> str:
    """The Odds API key: environment first, else the KEY=value file the settings page writes."""
    key = os.environ.get(env_name, "")
    if key:
        return key
    try:
        with open(secrets_path, encoding="utf-8") as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k == env_name and v:
                    return v.strip()
    except OSError:
        pass
    return ""


class SharpStrategy(Strategy):
    """Buys the Polymarket side whose ask is at least `min_edge` below Pinnacle's probability, before kick-off."""

    def __init__(self, cfg: dict, client, feed: Optional[OddsFeed] = None):
        super().__init__(cfg, client)
        self.state_path = cfg.get("odds_state_path") or os.path.join("data", "odds-api.json")
        self.secrets_path = cfg.get("secrets_path") or os.path.join(os.path.dirname(self.state_path) or ".",
                                                                     "secrets.env")
        self.feed, self._injected = feed, feed is not None
        self.scan: dict = {}

    def _feed(self) -> Optional[OddsFeed]:
        if self._injected:
            return self.feed
        key = read_key(self.secrets_path, self.cfg.get("odds_key_env", "ODDS_API_KEY"))
        if not key:
            return None
        if self.feed is None:
            c = self.cfg
            self.feed = OddsFeed(self.client.get_json, key, self.state_path, list(c.get("leagues") or LEAGUES),
                                 int(c.get("odds_monthly_cap", 450)), float(c.get("odds_refresh_h", 4)),
                                 float(c.get("odds_lookahead_h", 8)))
        self.feed.key = key  # a new key from the settings page takes effect at once
        return self.feed

    def candidates(self, now: float) -> List[Signal]:
        c = self.cfg
        if self._feed() is None:
            self.scan = {"kein Odds-API-Key (Dashboard -> /admin/ eintragen)": 1}
            return []
        matches = self.feed.matches(now)
        st = self.scan = dict(self.feed.stats, **{"Spiele mit Quote": len(matches)})
        min_start = float(c.get("min_hours_to_start", 0.25)) * 3600
        max_age = float(c.get("max_odds_age_h", 6)) * 3600
        matches = [m for m in matches if m["start"] - now >= min_start and now - m["odds_ts"] <= max_age]
        if not matches:
            return []
        edge = float(c.get("min_edge", 0.05))
        last = max(m["start"] for m in matches)
        evs = self.client.paged("/events", {"active": "true", "closed": "false", "tag_slug": c.get("tag_slug", "soccer"),
                                            "end_date_min": _iso(now), "end_date_max": _iso(last + 3 * 86400)},
                                max_items=2000)
        st["Polymarket-Events"] = len(evs)
        out = []
        for ev in evs:
            for m in ev.get("markets") or []:
                if m.get("closed") or not m.get("enableOrderBook") or not m.get("acceptingOrders", True):
                    continue
                hit = link(m.get("question") or "", _parse_ts(m.get("endDate") or ev.get("endDate")), matches)
                if not hit:
                    continue
                st["verknüpft"] = st.get("verknüpft", 0) + 1
                mt, p_yes = hit
                toks, prices = _parse_json_list(m.get("clobTokenIds")), _parse_json_list(m.get("outcomePrices"))
                if len(toks) != 2 or len(prices) != 2:
                    continue
                for i, fair in ((0, p_yes), (1, 1 - p_yes)):
                    price = float(prices[i])
                    if fair - price < edge - 0.01:  # the ask is checked by the engine; skip the hopeless ones
                        continue
                    st["Abweichung ≥ Schwelle"] = st.get("Abweichung ≥ Schwelle", 0) + 1
                    out.append(Signal(str(toks[i]), f"event:{ev.get('id')}", m.get("question", ""),
                                      ["Yes", "No"][i], fair=fair, max_price=round(fair - edge, 4),
                                      fee=self.fees.resolve(m, "sports"), end_ts=mt["start"],
                                      max_edge=float(c.get("max_gap", 0.25)), min_ask=0.03,
                                      max_spread=float(c.get("max_spread", 0.03)),
                                      reason=f"Pinnacle {fair:.3f} vs Polymarket {price:.3f} "
                                             f"({mt['home']} – {mt['away']}, {mt['league']})"))
        return out
