"""Paper measurement: temperature buckets the station has already ruled out.

The day's high only rises. Once the station is at least `min_margin` whole degrees past a bucket,
that bucket cannot win (same rounding as arb.wxobs). The market may still show a YES price.
This scenario does not place live orders. Every such bucket is logged with the real NO book
(best ask, size at that ask, displayed YES, margin, city, station, time). A paper buy of NO,
at the best ask only, happens when the displayed YES is still at least `min_yes` (default 3 cents)
and the ask has size. Resolution of those paper buys is the realized result; the log is how often
the ask was actually there.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List

from .client import _parse_json_list, _parse_ts, _delay
from .scenarios import Signal, Strategy
from .weather import MONTHS, parse_bucket, parse_title
from .wxobs import LiveObs, parse_question, station_from

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
        self.obs = LiveObs(client.get_json, ttl_s=float(cfg.get("obs_ttl_s", 600)))
        self.snaps: List[dict] = []
        self.scan: dict = {}

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
                if ruled["margin"] + 1e-9 < min_margin:
                    st["noch nicht ausgeschlossen"] = st.get("noch nicht ausgeschlossen", 0) + 1
                    continue
                st["ausgeschlossen"] = st.get("ausgeschlossen", 0) + 1
                snaps.append(self._snap(ev, m, q, ruled, now, min_yes, out))
        self.snaps = snaps
        st["kaufbar"] = sum(1 for s in snaps if s["buyable"])
        st["Signale"] = len(out)
        return out

    def _snap(self, ev: dict, m: dict, question: str, ruled: dict, now: float, min_yes: float,
              out: List[Signal]) -> dict:
        toks = _parse_json_list(m.get("clobTokenIds"))
        prices = _parse_json_list(m.get("outcomePrices"))
        yes_tok = str(toks[0]) if len(toks) == 2 else ""
        no_tok = str(toks[1]) if len(toks) == 2 else ""
        yes_px = float(prices[0]) if len(prices) == 2 else None
        books = self.client.books([t for t in (yes_tok, no_tok) if t]) if no_tok else {}
        nb = books.get(no_tok)
        ask = nb.best_ask if nb and nb.asks else None
        depth = float(nb.asks[0].size) if ask is not None else 0.0
        if yes_px is None and ask is not None:
            yes_px = round(1 - ask, 4)  # displayed price missing: the ask itself is the YES-equivalent
        edge = round(1 - ask, 4) if ask is not None else None
        if ask is None:
            note, buyable = "kein NO-Ask", 0
        elif depth <= 1e-9:
            note, buyable = "keine Tiefe", 0
        elif yes_px is None or yes_px + 1e-12 < min_yes:
            note, buyable = "JA unter 3 ct", 0
        elif not no_tok:
            note, buyable = "kein Token", 0
        else:
            note, buyable = "kaufbar", 1
        row = dict(ts=now, cid=str(m.get("conditionId") or m.get("id") or ""), token=no_tok,
                   question=question[:240], city=ruled["city"], station=ruled["station"],
                   margin=ruled["margin"], unit=ruled["unit"], yes_px=yes_px, no_ask=ask, depth=depth,
                   depth_usd=round(ask * depth, 2) if ask is not None else None, buyable=buyable, edge=edge,
                   note=note)
        if buyable:
            end = _parse_ts(m.get("endDate")) or _parse_ts(ev.get("endDate"))
            fee = self.fees.resolve(m, "weather")
            gid = ev.get("id")
            group = f"event:{gid}" if gid not in (None, "") else row["cid"]
            out.append(Signal(no_tok, group, question[:200], f"NO {ruled['city']}", fair=1.0, max_price=ask,
                              fee=fee, end_ts=end, delay_s=_delay(m),
                              reason=(f"Station {ruled['station']} {ruled['reading']:.0f}°{ruled['unit']} "
                                      f"Abstand {ruled['margin']:.0f}° JA {yes_px:.3f} Ask {ask:.3f} "
                                      f"Tiefe {depth:.0f}"),
                              min_ask=0.0))
        return row


# Keep the synthesized month names on the same list the question parser accepts.
assert set(MONTHS) == {m.lower() for m in _MONTH}
