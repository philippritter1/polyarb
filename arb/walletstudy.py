"""Wallet study: what do the wallets that really earn on Polymarket do? (20.10., read only, no orders)

Everything comes from Polymarket's public data API – trades, activity and positions of any wallet are public.

Market makers ("wallet-maker.csv"): the reward markets our scenarios tracked (`mm_reward_params`) are the
hunting ground. Their recent trades name the wallets that trade there; a wallet active in many of these markets,
on both sides, is a market maker. For the most active ones: rewards paid (activity type REWARD), trading volume,
merges, open positions and their profit, categories and days to the end of what they hold. The question: how much
reward per traded dollar do they get (we: ~4.5 % real, the simulation ~50 %), and in which markets?

Traders ("wallet-trader.csv"): the profit leaderboard. For each: closed/open positions – win rate, entry price
(endgame ≥ 0.90, longshot ≤ 0.20), categories, and how many hours before the end they bought.

Endpoints and response fields are logged in "wallet-diagnose.csv": the API is not documented in every detail, so the
first runs show what came back (and which leaderboard URL answered).

  python -m arb.walletstudy            # on the server (polyarb-wallets.timer); writes data/wallet-*.csv
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sqlite3
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime
from typing import Callable, Dict, Iterable, List, Optional, Sequence

from .memtrim import release_memory
from .study import categorize

DATA = "https://data-api.polymarket.com"
LEADERBOARDS = [  # tried in this order, the first that answers with a list is used
    (f"{DATA}/v1/leaderboard", {"timePeriod": "month", "orderBy": "PNL", "limit": 50, "category": "overall"}),
    ("https://lb-api.polymarket.com/profit", {"window": "1m", "limit": 50}),
]


def _ts(v) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        x = float(v)
        return x / 1000 if x > 1e12 else x
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _wallet(d: dict) -> str:
    for k in ("proxyWallet", "proxy_wallet", "wallet", "address", "user"):
        if d.get(k):
            return str(d[k]).lower()
    return ""


class Api:
    """GET with a diagnosis per endpoint: status, rows, the fields of the first row."""

    def __init__(self, get: Callable[[str, dict], object]):
        self.get = get
        self.diag: Dict[str, dict] = {}

    def list(self, url: str, params: dict) -> List[dict]:
        key = url.split("polymarket.com")[-1]
        d = self.diag.setdefault(key, dict(calls=0, errors=0, rows=0, fields="", error=""))
        d["calls"] += 1
        try:
            r = self.get(url, params)
        except Exception as e:  # noqa – one endpoint failing must not stop the study
            d["errors"] += 1
            d["error"] = f"{type(e).__name__}: {str(e)[:120]}"
            return []
        if isinstance(r, dict):  # some endpoints wrap the list
            r = next((v for v in r.values() if isinstance(v, list)), [])
        rows = [x for x in (r or []) if isinstance(x, dict)]
        d["rows"] += len(rows)
        if rows and not d["fields"]:
            d["fields"] = ",".join(sorted(rows[0].keys()))[:400]
        return rows


# ------------------------------------------------------------------------------------------------- makers
def reward_markets(db_paths: Iterable[str], limit: int = 150) -> List[str]:
    """Condition ids of the reward markets the scenarios logged, the most recently quoted first."""
    seen: Dict[str, float] = {}
    for p in db_paths:
        try:
            db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            for cid, ts in db.execute("SELECT cid, MAX(ts) FROM mm_reward_params GROUP BY cid"):
                if cid and ts > seen.get(cid, 0):
                    seen[cid] = ts
            db.close()
        except sqlite3.Error:
            continue
    return [c for c, _ in sorted(seen.items(), key=lambda kv: -kv[1])][:limit]


def maker_candidates(api: Api, cids: Sequence[str], exclude: set) -> List[dict]:
    """Wallets trading in the reward markets: markets, trades, $ volume, share of trades on both outcomes."""
    by: Dict[str, dict] = defaultdict(lambda: dict(markets=set(), trades=0, usd=0.0, outcomes=defaultdict(set), name=""))
    for cid in cids:
        for t in api.list(f"{DATA}/trades", {"market": cid, "limit": 500, "takerOnly": "false"}):
            w = _wallet(t)
            if not w or w in exclude:
                continue
            e = by[w]
            e["markets"].add(cid)
            e["trades"] += 1
            e["usd"] += _f(t.get("size")) * _f(t.get("price"))
            e["outcomes"][cid].add(str(t.get("outcome") or t.get("outcomeIndex") or ""))
            e["name"] = e["name"] or str(t.get("name") or t.get("pseudonym") or "")
    out = []
    for w, e in by.items():
        both = sum(1 for o in e["outcomes"].values() if len(o) > 1)
        out.append(dict(wallet=w, name=e["name"], markets=len(e["markets"]), trades=e["trades"], usd=e["usd"],
                        both_sides=both / len(e["markets"]) if e["markets"] else 0.0))
    out.sort(key=lambda d: (-d["markets"], -d["usd"]))
    return out


def _positions(api: Api, w: str, now: float) -> dict:
    pos = api.list(f"{DATA}/positions", {"user": w, "limit": 500, "sizeThreshold": 1})
    cats, dte = Counter(), []
    value = pnl = 0.0
    for p in pos:
        value += _f(p.get("currentValue"))
        pnl += _f(p.get("cashPnl"))
        cats[categorize({"question": p.get("title") or ""})] += _f(p.get("currentValue"))
        end = _ts(p.get("endDate"))
        if end:
            dte.append((end - now) / 86400)
    return dict(n=len(pos), value=value, cash_pnl=pnl, cats=cats, dte=statistics.median(dte) if dte else None)


def maker_profile(api: Api, w: str, now: float) -> dict:
    acts = api.list(f"{DATA}/activity", {"user": w, "limit": 500})
    rew = api.list(f"{DATA}/activity", {"user": w, "limit": 500, "type": "REWARD"})
    types = Counter(str(a.get("type") or "?").upper() for a in acts)
    trade_usd = sum(_f(a.get("usdcSize")) for a in acts if str(a.get("type")).upper() == "TRADE")
    tts = [t for t in (_ts(a.get("timestamp")) for a in acts) if t]
    span = max(1 / 24, (max(tts) - min(tts)) / 86400) if len(tts) > 1 else None
    rts = [t for t in (_ts(a.get("timestamp")) for a in rew) if t]
    rew_usd = sum(_f(a.get("usdcSize")) for a in rew)
    rew_days = max(1.0, (now - min(rts)) / 86400) if rts else None
    pos = _positions(api, w, now)
    return dict(types=types, trade_usd=trade_usd, trade_days=span, rewards=rew_usd, reward_days=rew_days,
                rewards_day=rew_usd / rew_days if rew_days else None,
                trade_day=trade_usd / span if span else None, **pos)


# ------------------------------------------------------------------------------------------------- traders
def leaderboard(api: Api) -> List[dict]:
    for url, params in LEADERBOARDS:
        rows = api.list(url, params)
        out = []
        for r in rows:
            w = _wallet(r)
            if w:
                out.append(dict(wallet=w, name=str(r.get("userName") or r.get("name") or r.get("pseudonym") or ""),
                                pnl=_f(r.get("pnl") or r.get("amount") or r.get("profit")),
                                volume=_f(r.get("vol") or r.get("volume"))))
        if out:
            return out
    return []


def trader_profile(api: Api, w: str, now: float) -> dict:
    closed = []  # 20.10.: the endpoint returns at most 50 per page and sorts by profit by default – the first page
    for off in range(0, 500, 50):  # alone gave a 100 % win rate; newest first, paged, is a fair sample
        page = api.list(f"{DATA}/closed-positions", {"user": w, "limit": 50, "offset": off,
                                                     "sortBy": "TIMESTAMP", "sortDirection": "DESC"})
        closed += page
        if len(page) < 50:
            break
    pos = closed or api.list(f"{DATA}/positions", {"user": w, "limit": 500, "sizeThreshold": 1})
    acts = api.list(f"{DATA}/activity", {"user": w, "limit": 500, "type": "TRADE"})
    first_buy: Dict[str, float] = {}
    for a in acts:
        if str(a.get("side") or "").upper() == "BUY":
            cid, t = str(a.get("conditionId") or ""), _ts(a.get("timestamp"))
            if cid and t and (cid not in first_buy or t < first_buy[cid]):
                first_buy[cid] = t
    wins = n = 0
    prices, hours, cats = [], [], defaultdict(float)
    for p in pos:
        pnl = _f(p.get("realizedPnl")) if closed else _f(p.get("cashPnl"))
        n += 1
        wins += pnl > 0
        avg = _f(p.get("avgPrice"))
        if avg:
            prices.append(avg)
        cats[categorize({"question": p.get("title") or ""})] += pnl
        end, t0 = _ts(p.get("endDate")), first_buy.get(str(p.get("conditionId") or ""))
        if end and t0 and end > t0:
            hours.append((end - t0) / 3600)
    return dict(n=n, closed=bool(closed), win_rate=wins / n if n else None,
                med_price=statistics.median(prices) if prices else None,
                endgame=sum(1 for x in prices if x >= 0.9) / len(prices) if prices else None,
                longshot=sum(1 for x in prices if x <= 0.2) / len(prices) if prices else None,
                med_hours=statistics.median(hours) if hours else None, cats=cats)


# ------------------------------------------------------------------------------------------------- run + output
def _de(v, d=2) -> str:
    return "" if v is None else f"{v:.{d}f}".replace(".", ",")


def _top(c: dict, k: int = 3) -> str:
    return ", ".join(f"{name} {_de(v, 0)}" for name, v in sorted(c.items(), key=lambda kv: -abs(kv[1]))[:k])


def run(get: Callable[[str, dict], object], db_paths: Sequence[str], out_dir: str, exclude: Iterable[str] = (),
        n_markets: int = 150, n_makers: int = 30, n_traders: int = 30, now: Optional[float] = None) -> dict:
    now = now or time.time()
    api = Api(get)
    excl = {e.lower() for e in exclude if e}
    cids = reward_markets(db_paths, n_markets)
    cands = maker_candidates(api, cids, excl)
    release_memory()
    makers = []
    for c in cands[:n_makers]:
        makers.append(dict(c, **maker_profile(api, c["wallet"], now)))
    traders = []
    for t in leaderboard(api)[:n_traders]:
        if t["wallet"] not in excl:
            traders.append(dict(t, **trader_profile(api, t["wallet"], now)))
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "wallet-maker.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Wallet", "Name", "Reward-Märkte (von unseren)", "Trades dort", "Volumen dort $", "Märkte beidseitig %",
                    "Rewards gezahlt $ (letzte 500)", "Rewards $/Tag", "Handel $/Tag", "Rewards je 1 $ Handel %",
                    "Zusammengelegt (MERGE)", "Einlösungen (REDEEM)", "offene Positionen", "Positionswert $",
                    "Positionen Gewinn/Verlust $", "Tage bis Ende (Median)", "Kategorien (Wert $)"])
        for m in makers:
            ratio = m["rewards_day"] / m["trade_day"] * 100 if m["rewards_day"] and m["trade_day"] else None
            w.writerow([m["wallet"], m["name"], m["markets"], m["trades"], _de(m["usd"], 0), _de(m["both_sides"] * 100, 0),
                        _de(m["rewards"]), _de(m["rewards_day"]), _de(m["trade_day"], 0), _de(ratio, 1),
                        m["types"].get("MERGE", 0), m["types"].get("REDEEM", 0), m["n"], _de(m["value"], 0),
                        _de(m["cash_pnl"], 0), _de(m["dte"], 0), _top(m["cats"])])
    with open(os.path.join(out_dir, "wallet-trader.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Wallet", "Name", "Gewinn laut Rangliste $", "Volumen $", "Positionen ausgewertet", "geschlossene?",
                    "Trefferquote %", "Einstiegspreis Median", "Anteil Einstieg ≥ 0,90 %", "Anteil Einstieg ≤ 0,20 %",
                    "Stunden vor Ende beim ersten Kauf (Median)", "Gewinn nach Kategorie $"])
        for t in traders:
            w.writerow([t["wallet"], t["name"], _de(t["pnl"], 0), _de(t["volume"], 0), t["n"], "ja" if t["closed"] else "nein",
                        _de(t["win_rate"] * 100 if t["win_rate"] is not None else None, 0), _de(t["med_price"]),
                        _de(t["endgame"] * 100 if t["endgame"] is not None else None, 0),
                        _de(t["longshot"] * 100 if t["longshot"] is not None else None, 0), _de(t["med_hours"], 0),
                        _top(t["cats"])])
    with open(os.path.join(out_dir, "wallet-diagnose.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Endpunkt", "Abfragen", "Fehler", "Zeilen", "Felder der ersten Zeile", "letzter Fehler"])
        for k, d in sorted(api.diag.items()):
            w.writerow([k, d["calls"], d["errors"], d["rows"], d["fields"], d["error"]])
    res = dict(ts=now, markets=len(cids), candidates=len(cands), makers=len(makers), traders=len(traders),
               calls=sum(d["calls"] for d in api.diag.values()))
    tmp = os.path.join(out_dir, "walletstudy.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(res, f)
    os.replace(tmp, os.path.join(out_dir, "walletstudy.json"))
    return res


def main(argv: Optional[Sequence[str]] = None) -> dict:
    ap = argparse.ArgumentParser(description="What do profitable Polymarket wallets do? (public data API, read only)")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--makers", type=int, default=30)
    ap.add_argument("--traders", type=int, default=30)
    args = ap.parse_args(argv)
    from .client import PolymarketClient
    from .config import load_config
    from .exchange import read_wallet
    cfg = load_config(args.config)
    data_dir = os.path.dirname((cfg.get("storage") or {}).get("db_path") or "data/x") or "."
    client = PolymarketClient(cfg["api"], cfg["fees"])
    own = []
    try:  # our own wallet is not a "profitable trader" to learn from
        w = read_wallet(os.path.join(data_dir, "secrets.env")) or {}
        own = [w.get("funder") or ""]
    except Exception:  # noqa
        pass
    res = run(client.get_json, sorted(glob.glob(os.path.join(data_dir, "scenario-mm_*.sqlite"))), data_dir, own,
              n_makers=args.makers, n_traders=args.traders)
    print(f"Wallet-Studie: {res['markets']} Reward-Märkte, {res['candidates']} Wallets dort, {res['makers']} Maker und "
          f"{res['traders']} Trader ausgewertet, {res['calls']} Abfragen")
    return res


if __name__ == "__main__":
    main()
