"""Paper measurement: temperature buckets the station has already ruled out.

The day's high only rises. Once the station is at least `min_margin` whole degrees past a bucket,
that bucket cannot win (same rounding as arb.wxobs). The market may still show a YES price.
This scenario does not place live orders. Every such bucket is logged with the real NO book
(best ask, size at that ask, displayed YES, margin, city, station, time). A paper buy of NO,
at the best ask only, happens when the displayed YES is still at least `min_yes` (default 3 cents)
and the size at the best ask, after the execution depth haircut, is at least the book's minimum
order. Resolution of those paper buys is the realized result; the log is how often the ask was
actually there. A bucket is judged only on station readings from its own local calendar day.

wetter_sniper2 (24.10.) adds options, all off by default (wetter_sniper unchanged):
  skip_lowest      no NO on the lowest bucket ("X or below"): a day without data resolves to it
                   (Istanbul 20.10.: station at 15 °C, "13 °C or below" won, -25 $)
  max_gap_h        no buy when the market day has a stretch longer than this without a reading
  won_yes_margin   YES on an open bucket already won: the high at least this many degrees into
                   "X or higher", the low into "X or below"
  late_hour        after this local hour on the market day: NO on high buckets starting at least
                   late_margin degrees above the day's high, if the forecast for the rest of the
                   day stays at or below the high
  final_margin     after the local day: NO on buckets at least this many degrees beyond the final
                   extreme (above the high, below the low)
  lazy_books       no order book fetch when the side to buy is already shown above 1 - min_yes
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, timezone
from typing import List

from .client import _parse_json_list, _parse_ts, _delay
from .memtrim import release_memory
from .scenarios import Signal, Strategy
from .weather import MONTHS, parse_bucket, parse_title
from .wxobs import LiveObs, parse_question, station_from, to_unit

log = logging.getLogger("polyarb.wxsniper")

_MONTH = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]


def bucket_question(ev: dict, m: dict, today) -> str:
    """Full 'Will the highest temperature…' question, or one built from the event title and the bucket."""
    q = (m.get("question") or "").strip()
    if parse_question(q, today.year):
        return q
    parsed = parse_title(ev.get("title") or "", today)
    label = (m.get("groupItemTitle") or "").strip()
    if not parsed or not parse_bucket(label) or parsed[2].month not in range(1, 13):
        return ""
    kind, city, day = parsed
    word = "highest" if kind == "max" else "lowest"
    return f"Will the {word} temperature in {city} be {label} on {_MONTH[day.month - 1]} {day.day}?"


def _description(ev: dict, m: dict) -> str:
    parts = [m.get("description"), m.get("resolutionSource"), ev.get("description"), ev.get("resolutionSource")]
    return "\n".join(p for p in parts if p)


class WxSniperStrategy(Strategy):
    """Candidates are buyable NOs. Every ruled-out bucket, bought or not, is appended to `snaps`."""

    def __init__(self, cfg: dict, client):
        super().__init__(cfg, client)
        self.obs = LiveObs(client.get_json, ttl_s=float(cfg.get("obs_ttl_s", 600)),
                           hours=int(cfg.get("obs_hours", 30)))
        self.snaps: List[dict] = []
        self.scan: dict = {}
        self.haircut = 1.0  # ScenarioEngine overwrites this with execution.depth_haircut

    def candidates(self, now: float) -> List[Signal]:
        c = self.cfg
        today = datetime.fromtimestamp(now, tz=timezone.utc).date()
        delay = float(c.get("obs_delay_min", 15)) * 60
        min_margin = float(c.get("min_margin", 1))
        min_yes = float(c.get("min_yes", 0.03))
        tag = c.get("tag_slug", "weather")
        events = self.client.paged("/events", {"active": "true", "closed": "false", "tag_slug": tag},
                                   max_items=int(c.get("max_events", 400)))
        st = self.scan = {"Events": len(events)}
        out, seen = [], set()
        snaps: List[dict] = []
        for ev in events:
            for m in ev.get("markets") or []:
                if m.get("closed") or not m.get("enableOrderBook", True):
                    continue
                cid = str(m.get("conditionId") or m.get("id") or "")
                if not cid or cid in seen:
                    continue
                seen.add(cid)
                q = bucket_question(ev, m, today)
                if not q:
                    st["kein Temperatur-Bucket"] = st.get("kein Temperatur-Bucket", 0) + 1
                    continue
                desc = _description(ev, m)
                if not station_from(desc):
                    st["keine Station"] = st.get("keine Station", 0) + 1
                    continue
                ruled = self.obs.ruling(q, desc, now, delay)
                if ruled is None:
                    st["keine Messung"] = st.get("keine Messung", 0) + 1
                    continue
                if ruled["margin"] + 1e-9 >= min_margin:
                    rule = "ausgeschlossen"
                else:
                    rule = self._later_rule(ruled, now)
                if rule is None:
                    st["noch nicht ausgeschlossen"] = st.get("noch nicht ausgeschlossen", 0) + 1
                    continue
                buy_yes = rule == "gewonnen"
                if not buy_yes and c.get("skip_lowest") and ruled["lo"] == -math.inf:
                    st["niedrigstes Bucket ausgelassen"] = st.get("niedrigstes Bucket ausgelassen", 0) + 1
                    continue
                if c.get("max_gap_h") is not None and self.obs.max_gap_h(
                        ruled["station"], ruled["tz"], date.fromisoformat(ruled["day"]), now, delay) \
                        > float(c["max_gap_h"]) + 1e-9:
                    st["Datenlücke"] = st.get("Datenlücke", 0) + 1
                    continue
                st[rule] = st.get(rule, 0) + 1
                snaps.append(self._snap(ev, m, q, ruled, now, min_yes, out, buy_yes=buy_yes, rule=rule))
        self.snaps = snaps
        del events
        release_memory()  # the event listing (~400 events with all buckets) is no longer needed (31.10., RAM)
        st["kaufbar"] = sum(1 for s in snaps if s["buyable"])
        st["Signale"] = len(out)
        return out

    def _later_rule(self, r: dict, now: float):
        """wetter_sniper2: a bucket decided without the station being past it yet – "gewonnen" (YES),
        "spät ausgeschlossen" (evening, forecast flat) or "Tag vorbei" (final extreme). None otherwise."""
        c = self.cfg
        kind, lo, hi, v = r["kind"], r["lo"], r["hi"], r["reading"]
        wm = c.get("won_yes_margin")
        if wm is not None:
            if kind == "max" and hi == math.inf and v >= lo + float(wm) - 1:
                return "gewonnen"
            if kind == "min" and lo == -math.inf and v <= hi - float(wm) + 1:
                return "gewonnen"
        from zoneinfo import ZoneInfo
        local = datetime.fromtimestamp(now, tz=ZoneInfo(r["tz"]))
        day = date.fromisoformat(r["day"])
        fm = c.get("final_margin")
        if fm is not None and local.date() > day:
            if kind == "max" and lo != -math.inf and lo >= v + float(fm):
                return "Tag vorbei"
            if kind == "min" and hi != math.inf and hi <= v - float(fm):
                return "Tag vorbei"
        lh = c.get("late_hour")
        if lh is not None and kind == "max" and local.date() == day and local.hour >= int(lh) \
                and lo != -math.inf and lo >= v + float(c.get("late_margin", 2)):
            end = datetime(day.year, day.month, day.day, tzinfo=ZoneInfo(r["tz"])).timestamp() + 86400
            rest = [f for t, f in self.obs.forecast(r["station"], now) if now - 3600 <= t < end]
            if rest and max(to_unit(f, r["unit"]) for f in rest) <= v:
                return "spät ausgeschlossen"
        return None

    def _snap(self, ev: dict, m: dict, question: str, ruled: dict, now: float, min_yes: float,
              out: List[Signal], buy_yes: bool = False, rule: str = "ausgeschlossen") -> dict:
        toks = _parse_json_list(m.get("clobTokenIds"))
        prices = _parse_json_list(m.get("outcomePrices"))
        yes_tok = str(toks[0]) if len(toks) == 2 else ""
        no_tok = str(toks[1]) if len(toks) == 2 else ""
        yes_px = float(prices[0]) if len(prices) == 2 else None
        buy_tok = yes_tok if buy_yes else no_tok
        other = (None if yes_px is None else round(1 - yes_px, 4)) if buy_yes else yes_px  # the losing side
        lazy = bool(self.cfg.get("lazy_books")) and other is not None and other + 1e-12 < min_yes
        books = self.client.books([t for t in (yes_tok, no_tok) if t]) if no_tok and not lazy else {}
        nb = books.get(buy_tok)
        ask = nb.best_ask if nb and nb.asks else None
        depth = float(nb.asks[0].size) if ask is not None else 0.0
        if other is None and ask is not None:
            other = round(1 - ask, 4)  # displayed price missing: the ask itself is the equivalent
        if yes_px is None and other is not None:
            yes_px = round(1 - other, 4) if buy_yes else other
        edge = round(1 - ask, 4) if ask is not None else None
        hair = float(getattr(self, "haircut", 1.0) or 0.0)
        minimum = float(nb.min_order_size) if nb is not None else 0.0
        side = "JA" if buy_yes else "NO"
        if lazy:
            note, buyable = ("NEIN" if buy_yes else "JA") + " unter 3 ct", 0
        elif ask is None:
            note, buyable = f"kein {side}-Ask", 0
        elif depth <= 1e-9:
            note, buyable = "keine Tiefe", 0
        elif other is None or other + 1e-12 < min_yes:
            note, buyable = ("NEIN" if buy_yes else "JA") + " unter 3 ct", 0
        elif not no_tok:
            note, buyable = "kein Token", 0
        elif depth * hair + 1e-9 < minimum:
            note, buyable = "unter Mindestgröße", 0
        else:
            note, buyable = "kaufbar", 1
        row = dict(ts=now, cid=str(m.get("conditionId") or m.get("id") or ""), token=buy_tok,
                   question=question[:240], city=ruled["city"], station=ruled["station"],
                   margin=ruled["margin"], unit=ruled["unit"], yes_px=yes_px, no_ask=ask, depth=depth,
                   depth_usd=round(ask * depth, 2) if ask is not None else None, buyable=buyable, edge=edge,
                   note=note)
        end = _parse_ts(m.get("endDate")) or _parse_ts(ev.get("endDate"))
        gid = ev.get("id")
        group = f"event:{gid}" if gid not in (None, "") else row["cid"]
        blog = getattr(self, "booklog", None)
        if blog is not None and books:
            from .booklog import meta_for
            blog.record(now, books, meta_for(
                [yes_tok, no_tok],
                dict(cid=row["cid"], event=group, category="Wetter", end_ts=end, title=question[:160],
                     yes=yes_tok, no=no_tok)), None)
        if buyable:
            fee = self.fees.resolve(m, "weather")
            prefix = "" if rule == "ausgeschlossen" else f"{rule}: "
            out.append(Signal(buy_tok, group, question[:200], f"{side} {ruled['city']}", fair=1.0, max_price=ask,
                              fee=fee, end_ts=end, delay_s=_delay(m),
                              reason=(f"{prefix}Station {ruled['station']} {ruled['reading']:.0f}°{ruled['unit']} "
                                      f"Abstand {ruled['margin']:.0f}° JA {yes_px:.3f} Ask {ask:.3f} "
                                      f"Tiefe {depth:.0f}"),
                              min_ask=0.0))
        return row


# Keep the synthesized month names on the same list the question parser accepts.
assert set(MONTHS) == {m.lower() for m in _MONTH}
