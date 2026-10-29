"""Replay lab: score a grid of market-making rules on the booklog and check them walk-forward.

Two-sided bids around the mid, as the mm scenarios quote, one market at a time from the 60 s snapshots:
- Rewards: Polymarket's formula (`score`, `q_min` from arb/mm.py) with the reward parameters the scenarios
  logged (`mm_reward_params`), our bid against the book's levels, rate × share × time. The booklog keeps only
  3 levels per side, so the others' score is too small and our share too big – reported at 60 % like the
  cockpit (real rewards were ~60 % of the estimate), still an upper bound.
- Fills: the next snapshot's ask at or below our bid, or a new last trade strictly below it. No queue.
- Trading: per filled share the side's mid 5 and 60 min later minus our price ("m60"), and the side's bid
  60 min later minus our price ("m60 Bid", as if sold to the bid – the pessimistic exit). After a fill the side
  rests `cooldown_s`, like `fill_cooldown_s` in mm_schutz_neu.

Two more ways to count the same fills (28./29.10., the replay said +2 % per $ filled at 0.9, the scenarios ≈ 0):
- "Halten": the scenarios do not sell to the bid after 60 min. They put a sell order on the inventory right away at
  own mid + the quote distance and step it down every hour to the mid over 6 h (exit after_h 0, decay_h 6); after
  96 h the rest goes to the bid. Filled when the side's bid reaches it or a new trade prints above it. Still open at
  the end of the data: valued at the last bid.
- "streng": only fills where the ask went strictly below our bid or a trade printed below it – a touch at our
  price leaves us behind the queue that was there first.

Walk-forward: the window is cut into blocks of `block_h` hours. For each block from the second on, the rule with
the best result on all earlier blocks is picked and its result in the block counts as out-of-sample. If the
picked rules do much worse out-of-sample than in-sample, the grid only finds noise.

Nothing here touches the running scenarios. Run nightly (polyarb-replay.timer) or by hand:
  python -m arb.replaylab              # booklog + scenario databases from config.yaml, writes data/replay-*.csv
"""
from __future__ import annotations

import argparse
import bisect
import csv
import glob
import json
import math
import os
import sqlite3
import time
from collections import deque
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .booklog import unpack_levels
from .mm import q_min, score

REAL_REWARD_FACTOR = 0.6
TICK = 0.01


@dataclass(frozen=True)
class Rule:
    frac: float           # bid at mid − frac × reward spread (at least one tick)
    pause: bool           # mid jumps ≥ 3 ticks within 5 min → no bids for 10 min (mm_ruhig)
    lo: float             # quote only while lo ≤ mid ≤ hi
    hi: float
    min_dte: float        # days to the market's end at least this
    fav: bool = False     # bid only on the favourite's token (mid ≥ 0.5): the cheaper token lost ~7 % per $ filled
                          # 60 min after the fill in the scenarios (19.–27.10.), the favourite's ~1 %

    @property
    def name(self) -> str:
        return (f"Abstand {self.frac:g}" + (" · Pause" if self.pause else "")
                + (f" · Mitte {self.lo:g}–{self.hi:g}" if (self.lo, self.hi) != (0.05, 0.95) else "")
                + (f" · ≥{self.min_dte:g} Tage" if self.min_dte else "") + (" · nur Favorit" if self.fav else ""))


def default_rules() -> List[Rule]:
    grid = [Rule(f, p, lo, hi, d) for f in (0.25, 0.5, 0.7, 0.9) for p in (False, True)
            for lo, hi in ((0.05, 0.95), (0.2, 0.8)) for d in (0.0, 14.0)]
    # 27.10.: one-sided on the favourite – fewer rewards (q_min counts one side a third), fewer bad fills?
    return grid + [Rule(f, True, lo, hi, d, fav=True) for f in (0.5, 0.7, 0.8, 0.9)  # 0.8: 28.10. Nutzer
                   for lo, hi in ((0.05, 0.95), (0.2, 0.8)) for d in (0.0, 14.0)]


BASELINE = Rule(0.7, True, 0.05, 0.95, 0.0)  # closest to mm_schutz_neu


# ------------------------------------------------------------------------------------------------- loading
def reward_params(db_paths: Iterable[str]) -> Dict[str, Tuple[float, float, float]]:
    """cid -> (daily rate $, max spread in price units, min size) – the latest the scenarios logged."""
    out: Dict[str, Tuple[float, float, float, float]] = {}
    for path in db_paths:
        try:
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            rows = db.execute("""SELECT cid, MAX(ts), daily_rate, max_spread_ct, min_size FROM mm_reward_params
                                 GROUP BY cid""").fetchall()
            db.close()
        except sqlite3.Error:
            continue
        for cid, ts, rate, ct, size in rows:
            if cid and (cid not in out or ts > out[cid][0]):
                out[cid] = (ts, float(rate or 0), float(ct or 0) / 100, float(size or 0))
    return {c: v[1:] for c, v in out.items() if v[1] > 0 and v[2] > 0}


def markets(paths: Sequence[str]) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for path in paths:
        try:
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            for cid, cat, end, title, yes, no in db.execute("SELECT cid, category, end_ts, title, yes, no FROM mkt"):
                if cid and yes and no:
                    out[cid] = dict(cid=cid, category=cat or "", end_ts=end, title=title or "", yes=yes, no=no)
            db.close()
        except sqlite3.Error:
            continue
    return out


def series(paths: Sequence[str], token: str) -> Dict[int, tuple]:
    """ts -> (bid, ask, mid, last, bids, asks) for one token over all day files."""
    out: Dict[int, tuple] = {}
    for path in paths:
        try:
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            for ts, bid, ask, mid, last, bids, asks in db.execute(
                    "SELECT ts, bid, ask, mid, last, bids, asks FROM snap WHERE token = ? ORDER BY ts", (token,)):
                out[ts] = (bid, ask, mid, last, bids, asks)
            db.close()
        except sqlite3.Error:
            continue
    return out


# ------------------------------------------------------------------------------------------------- simulation
def _floor(p: float) -> float:
    return round(math.floor(p / TICK + 1e-9) * TICK, 4)


def _new_acc() -> dict:
    return dict(quote_min=0.0, rewards=0.0, fills=0, filled=0.0, m5=0.0, m60=0.0, m60bid=0.0,
                hold=0.0, sfills=0, sfilled=0.0, shold=0.0)


HOLD_DECAY_H = 6
HOLD_DUMP_H = 96


def replay_market(mk: dict, yes: Dict[int, tuple], no: Dict[int, tuple], params: Tuple[float, float, float],
                  rules: Sequence[Rule], block_s: int, size_usd: float = 50.0, cooldown_s: float = 3600,
                  max_gap_s: float = 180) -> Dict[Tuple[int, int], dict]:
    """(rule index, block start) -> accumulators for one market."""
    rate, v, min_size = params
    ts_list = sorted(t for t in yes if yes[t][2] is not None)
    if len(ts_list) < 3:
        return {}
    yes_mid = {t: yes[t][2] for t in ts_list}
    no_mid = {t: (no[t][2] if t in no and no[t][2] is not None else 1 - yes_mid[t]) for t in ts_list}
    no_bid = {t: (no[t][0] if t in no and no[t][0] is not None
                  else (1 - yes[t][1] if yes[t][1] is not None else None)) for t in ts_list}

    def later(t: float, which: str, side: str) -> Optional[float]:
        """The side's mid (or bid) at the first snapshot at least t; None if the data ends before."""
        k = bisect.bisect_left(ts_list, t)
        if k >= len(ts_list) or ts_list[k] - t > 900:
            return None
        u = ts_list[k]
        if which == "mid":
            return yes_mid[u] if side == "yes" else no_mid[u]
        return yes[u][0] if side == "yes" else no_bid[u]

    def side_at(u: int, side: str) -> tuple:
        """(bid, mid, last trade) of one side at a snapshot."""
        if side == "yes":
            return yes[u][0], yes_mid[u], yes[u][3]
        n = no.get(u)
        return no_bid[u], no_mid[u], (n[3] if n else None)

    held: Dict[tuple, float] = {}

    def hold_exit(side: str, t0: float, px: float, d: float) -> float:
        """Exit price of the scenarios' sell order for a fill at t0 (see the module docstring), cached per fill."""
        key = (side, t0, round(px, 4), round(d, 4))
        if key in held:
            return held[key]
        k = bisect.bisect_left(ts_list, t0)
        ask = None
        last_bid, prev_last, res = px, None, None
        for u in ts_list[k:]:
            bid, m, last = side_at(u, side)
            if ask is not None and ((bid is not None and bid >= ask - 1e-9)
                                    or (last is not None and last != prev_last and last > ask + 1e-9)):
                res = ask
                break
            if bid is not None:
                last_bid = bid
            prev_last = last
            age = u - t0
            if age >= HOLD_DUMP_H * 3600:
                res = last_bid
                break
            done = min(1.0, math.floor(age / 3600) / HOLD_DECAY_H)
            ask = math.ceil((m + d * (1 - done)) / TICK - 1e-9) * TICK
        held[key] = res if res is not None else last_bid
        return held[key]

    out: Dict[Tuple[int, int], dict] = {}
    cool = [[0.0, 0.0] for _ in rules]
    paused_until = [0.0] * len(rules)
    hist: deque = deque()
    for i, t in enumerate(ts_list[:-1]):
        t2 = ts_list[i + 1]
        dt = t2 - t
        if dt > max_gap_s:
            continue
        y = yes[t]
        n = no.get(t)
        mid = y[2]
        hist.append((t, mid))
        while hist and hist[0][0] < t - 300:
            hist.popleft()
        jumped = any(abs(mid - m) >= 3 * TICK - 1e-9 for _, m in hist)
        y_ask = y[1]
        n_ask = n[1] if n and n[1] is not None else (1 - y[0] if y[0] is not None else None)
        if y_ask is None or y[0] is None or y_ask - y[0] > 2 * v:
            continue  # empty side or spread wider than the mm scenarios accept
        dte = (mk["end_ts"] - t) / 86400 if mk.get("end_ts") else None
        side1 = [(p, s) for p, s in unpack_levels(y[4])]
        side2 = [(p, s) for p, s in unpack_levels(y[5])]
        if n:
            side1 += [(1 - p, s) for p, s in unpack_levels(n[5])]
            side2 += [(1 - p, s) for p, s in unpack_levels(n[4])]
        others = q_min(score(side1, mid, v, True, min_size), score(side2, mid, v, False, min_size), mid)
        y2, n2 = yes[t2], no.get(t2)
        block = int(t // block_s) * block_s
        cache: Dict[float, tuple] = {}
        for ri, r in enumerate(rules):
            if r.pause and jumped:
                paused_until[ri] = t + 600
            if t < paused_until[ri] or not (r.lo <= mid <= r.hi) or (r.min_dte and dte is not None and dte < r.min_dte):
                continue
            if r.frac not in cache:
                d = max(TICK, v * r.frac)
                py, pn = _floor(mid - d), _floor((1 - mid) - d)
                py = py if TICK <= py < y_ask - 1e-9 else None
                pn = pn if n_ask is not None and TICK <= pn < n_ask - 1e-9 else None
                sy = max(min_size, size_usd / py) if py else 0.0
                sn = max(min_size, size_usd / pn) if pn else 0.0
                cache[r.frac] = (py, pn, sy, sn)
            py, pn, sy, sn = cache[r.frac]
            if r.fav:  # the cheaper token's bid goes: YES below 0.5, NO above
                py = py if mid >= 0.5 - 1e-9 else None
                pn = pn if mid <= 0.5 + 1e-9 else None
            if py and t < cool[ri][0]:
                py = None
            if pn and t < cool[ri][1]:
                pn = None
            if not py and not pn:
                continue
            o1 = score([(py, sy)], mid, v, True, min_size) if py else 0.0
            o2 = score([(1 - pn, sn)], mid, v, False, min_size) if pn else 0.0
            ours = q_min(o1, o2, mid)
            acc = out.get((ri, block))
            if acc is None:
                acc = out[(ri, block)] = _new_acc()
            acc["quote_min"] += dt / 60
            if ours > 0:
                acc["rewards"] += rate * ours / (ours + others) * dt / 86400
            for side, px, sz, nxt, cur in (("yes", py, sy, y2, y), ("no", pn, sn, n2, n)):
                if not px or nxt is None:
                    continue
                hit = (nxt[1] is not None and nxt[1] <= px + 1e-9) or (
                    nxt[3] is not None and nxt[3] < px - 1e-9 and (cur is None or nxt[3] != cur[3]))
                if not hit:
                    continue
                m5, m60, b60 = later(t2 + 300, "mid", side), later(t2 + 3600, "mid", side), later(t2 + 3600, "bid", side)
                if m60 is None:
                    continue  # no data an hour later: cannot score it, leave it out on every rule alike
                acc["fills"] += 1
                acc["filled"] += px * sz
                acc["m5"] += ((m5 if m5 is not None else m60) - px) * sz
                acc["m60"] += (m60 - px) * sz
                acc["m60bid"] += ((b60 if b60 is not None else m60) - px) * sz
                hold = (hold_exit(side, t2, px, max(TICK, v * r.frac)) - px) * sz
                acc["hold"] += hold
                if (nxt[1] is not None and nxt[1] < px - 1e-9) or (
                        nxt[3] is not None and nxt[3] < px - 1e-9 and (cur is None or nxt[3] != cur[3])):
                    acc["sfills"] += 1
                    acc["sfilled"] += px * sz
                    acc["shold"] += hold
                cool[ri][0 if side == "yes" else 1] = t2 + cooldown_s
    return out


def net(acc: dict) -> float:
    """Rewards at 60 % plus the pessimistic exit (sold to the bid an hour later)."""
    return REAL_REWARD_FACTOR * acc["rewards"] + acc["m60bid"]


def net_hold(acc: dict) -> float:
    """Rewards at 60 % plus the scenarios' exit (sell order stepping down to the mid)."""
    return REAL_REWARD_FACTOR * acc["rewards"] + acc["hold"]


def net_strict(acc: dict) -> float:
    """As net_hold, but only the fills that traded through our bid."""
    return REAL_REWARD_FACTOR * acc["rewards"] + acc["shold"]


def _sum(accs: Iterable[dict]) -> dict:
    tot = _new_acc()
    for a in accs:
        for k in tot:
            tot[k] += a[k]
    return tot


def walk_forward(by_rule_block: Dict[Tuple[int, int], dict], n_rules: int, baseline: int, net=net) -> List[dict]:
    blocks = sorted({b for _, b in by_rule_block})
    rows = []
    for k in range(1, len(blocks)):
        train = [_sum(a for (ri, b), a in by_rule_block.items() if ri == r and b < blocks[k]) for r in range(n_rules)]
        best = max(range(n_rules), key=lambda r: net(train[r]))
        test = by_rule_block.get((best, blocks[k])) or _new_acc()
        base = by_rule_block.get((baseline, blocks[k])) or _new_acc()
        rows.append(dict(block=blocks[k], rule=best, train_net=net(train[best]) / k, test_net=net(test),
                         test_rewards=REAL_REWARD_FACTOR * test["rewards"], test_m60bid=test["m60bid"],
                         test_filled=test["filled"], base_net=net(base)))
    return rows


def run(booklog_paths: Sequence[str], scenario_dbs: Sequence[str], rules: Optional[Sequence[Rule]] = None,
        block_h: float = 12, size_usd: float = 50.0) -> dict:
    rules = list(rules or default_rules())
    block_s = int(block_h * 3600)
    params = reward_params(scenario_dbs)
    mkts = {c: m for c, m in markets(booklog_paths).items() if c in params}
    total: Dict[Tuple[int, int], dict] = {}
    by_cat: Dict[Tuple[int, str], dict] = {}
    for cid, mk in mkts.items():
        res = replay_market(mk, series(booklog_paths, mk["yes"]), series(booklog_paths, mk["no"]), params[cid],
                            rules, block_s, size_usd)
        for key, acc in res.items():
            total[key] = _sum([total.get(key) or _new_acc(), acc])
            ck = (key[0], mk["category"] or "?")
            by_cat[ck] = _sum([by_cat.get(ck) or _new_acc(), acc])
    blocks = sorted({b for _, b in total})
    base = rules.index(BASELINE) if BASELINE in rules else 0
    rule_rows = []
    for ri, r in enumerate(rules):
        accs = [total[(ri, b)] for b in blocks if (ri, b) in total]
        tot = _sum(accs)
        rule_rows.append(dict(rule=r.name, idx=ri, net=net(tot), rewards60=REAL_REWARD_FACTOR * tot["rewards"],
                              m60=tot["m60"], m60bid=tot["m60bid"], m5=tot["m5"], fills=tot["fills"], filled=tot["filled"],
                              quote_h=tot["quote_min"] / 60, blocks=len(accs),
                              blocks_pos=sum(1 for a in accs if net(a) > 0),
                              net_hold=net_hold(tot), hold=tot["hold"], hold_pos=sum(1 for a in accs if net_hold(a) > 0),
                              net_strict=net_strict(tot), sfills=tot["sfills"], sfilled=tot["sfilled"],
                              strict_pos=sum(1 for a in accs if net_strict(a) > 0)))
    wf = walk_forward(total, len(rules), base)
    wf_hold = walk_forward(total, len(rules), base, net_hold)
    wf_strict = walk_forward(total, len(rules), base, net_strict)
    best = max(rule_rows, key=lambda d: d["net"]) if rule_rows else None
    cats = [dict(rule=rules[ri].name, category=c, net=net(a), rewards60=REAL_REWARD_FACTOR * a["rewards"],
                 m60bid=a["m60bid"], fills=a["fills"], filled=a["filled"])
            for (ri, c), a in sorted(by_cat.items()) if best and ri in (best["idx"], base)]
    return dict(ts=time.time(), markets=len(mkts), files=len(booklog_paths), block_h=block_h, size_usd=size_usd,
                blocks=len(blocks), first=blocks[0] if blocks else None, rules=rule_rows, walk_forward=wf,
                wf_test=sum(w["test_net"] for w in wf), wf_train_best=sum(w["train_net"] for w in wf),
                wf_base=sum(w["base_net"] for w in wf), baseline=rules[base].name, categories=cats,
                walk_forward_hold=wf_hold, wf_hold=sum(w["test_net"] for w in wf_hold),
                walk_forward_strict=wf_strict, wf_strict=sum(w["test_net"] for w in wf_strict),
                rule_names=[r.name for r in rules])


# ------------------------------------------------------------------------------------------------- output
def _de(v, d=2) -> str:
    return "" if v is None else f"{v:.{d}f}".replace(".", ",")


def write_csvs(res: dict, out_dir: str) -> List[str]:
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    p = os.path.join(out_dir, "replay-regeln.csv")
    with open(p, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Regel", "netto $ (Rewards×0,6 + Verkauf zum Bid nach 60 Min.)", "Rewards×0,6 $", "Kurs 60 Min. $",
                    "Bid 60 Min. $", "Kurs 5 Min. $", "Fills", "gefüllt $", "netto je 1 $ gefüllt", "Gebots-Stunden",
                    "Blöcke", "Blöcke positiv", "netto Halten $ (Verkaufsgebot wie Szenario)", "Halten Handel $",
                    "Blöcke positiv Halten", "netto streng + Halten $", "Fills streng", "gefüllt streng $",
                    "Blöcke positiv streng"])
        for r in sorted(res["rules"], key=lambda d: -d["net"]):
            w.writerow([r["rule"], _de(r["net"]), _de(r["rewards60"]), _de(r["m60"]), _de(r["m60bid"]), _de(r["m5"]),
                        r["fills"], _de(r["filled"]), _de(r["net"] / r["filled"], 4) if r["filled"] else "",
                        _de(r["quote_h"], 1), r["blocks"], r["blocks_pos"], _de(r.get("net_hold")), _de(r.get("hold")),
                        r.get("hold_pos", ""), _de(r.get("net_strict")), r.get("sfills", ""), _de(r.get("sfilled")),
                        r.get("strict_pos", "")])
    paths.append(p)
    p = os.path.join(out_dir, "replay-walkforward.csv")
    with open(p, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Block (UTC)", "gewählte Regel (beste bis dahin)", "netto davor je Block $", "netto im Block $",
                    "Rewards×0,6 $", "Bid 60 Min. $", "gefüllt $", f"Vergleich {res['baseline']} $"])
        for r in res["walk_forward"]:
            w.writerow([time.strftime("%Y-%m-%d %H:%M", time.gmtime(r["block"])), res["rule_names"][r["rule"]],
                        _de(r["train_net"]), _de(r["test_net"]), _de(r["test_rewards"]), _de(r["test_m60bid"]),
                        _de(r["test_filled"]), _de(r["base_net"])])
    paths.append(p)
    p = os.path.join(out_dir, "replay-walkforward-halten.csv")
    with open(p, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Zählweise", "Block (UTC)", "gewählte Regel (beste bis dahin)", "netto davor je Block $",
                    "netto im Block $", f"Vergleich {res['baseline']} $"])
        for label, key in (("Halten", "walk_forward_hold"), ("streng + Halten", "walk_forward_strict")):
            for r in res.get(key) or []:
                w.writerow([label, time.strftime("%Y-%m-%d %H:%M", time.gmtime(r["block"])),
                            res["rule_names"][r["rule"]], _de(r["train_net"]), _de(r["test_net"]), _de(r["base_net"])])
    paths.append(p)
    p = os.path.join(out_dir, "replay-kategorien.csv")
    with open(p, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Regel", "Kategorie", "netto $", "Rewards×0,6 $", "Bid 60 Min. $", "Fills", "gefüllt $"])
        for r in res["categories"]:
            w.writerow([r["rule"], r["category"], _de(r["net"]), _de(r["rewards60"]), _de(r["m60bid"]), r["fills"],
                        _de(r["filled"])])
    paths.append(p)
    return paths


def main(argv: Optional[Sequence[str]] = None) -> dict:
    ap = argparse.ArgumentParser(description="Walk-forward replay of market-making rules on the booklog")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--block-h", type=float, default=12)
    ap.add_argument("--size-usd", type=float, default=50.0)
    args = ap.parse_args(argv)
    from .config import load_config
    cfg = load_config(args.config)
    data_dir = os.path.dirname((cfg.get("storage") or {}).get("db_path") or "data/x") or "."
    bdir = ((cfg.get("booklog") or {}).get("dir")) or os.path.join(data_dir, "booklog")
    paths = sorted(glob.glob(os.path.join(bdir, "*.sqlite")))
    dbs = sorted(glob.glob(os.path.join(data_dir, "scenario-mm_*.sqlite")))
    res = run(paths, dbs, block_h=args.block_h, size_usd=args.size_usd)
    write_csvs(res, data_dir)
    tmp = os.path.join(data_dir, "replaylab.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(res, f)
    os.replace(tmp, os.path.join(data_dir, "replaylab.json"))
    print(f"{res['markets']} Märkte, {res['blocks']} Blöcke à {args.block_h:g} h; walk-forward netto "
          f"{res['wf_test']:+.2f} $ (davor je Block {res['wf_train_best']:+.2f} $, Vergleich {res['wf_base']:+.2f} $); "
          f"Halten {res['wf_hold']:+.2f} $, streng + Halten {res['wf_strict']:+.2f} $")
    return res


if __name__ == "__main__":
    main()
