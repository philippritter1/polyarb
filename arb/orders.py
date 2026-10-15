"""Order gateway between a strategy and the exchange: off, dry run, live.

The strategy says which resting orders it wants right now; `sync` compares that with the orders the gateway
holds and derives the actions – place, cancel, replace (price changed). Every action is checked against hard
limits and written to the scenario database (table live_orders) and data/live-status-<name>.json.

Mode: data/live.json {"mode": "off" | "dry" | "live"}, set on the settings page (/admin/). "off" is the
emergency stop: every held order is cancelled (at the exchange too, if live) and nothing new is placed.

Real orders are sent only if ALL of these hold – otherwise the gateway runs as dry run and says why:
  - mode "live" on /admin/                         (the owner's switch)
  - `allow_real: true` in this scenario's `live:`  (a reviewed commit; the other scenarios stay dry)
  - a usable wallet in data/secrets.env            (POLY_PRIVATE_KEY, POLY_FUNDER, POLY_SIG_TYPE – arb/exchange.py)
  - Polymarket's geoblock check for the server's IP says not blocked (checked hourly; unknown = not allowed)
  - the daily loss limit is not hit (cash + positions vs. the day's start, `max_daily_loss_usd`); hitting it
    switches the mode to "off" for everyone and cancels all orders at the exchange
On every switch into live all orders of the account are cancelled first, so the gateway's view starts clean.

Hard limits (per scenario, config `live:`), independent of the strategy:
  max_order_usd    one order (price x size)
  max_market_usd   all buy orders of one token
  max_total_usd    all open buy orders together
Sell orders (inventory exit) only need the shares, which the exchange checks. A rejection is logged and counted
once per (order, price, size, reason): sync runs every few seconds and would otherwise re-log it each time.

Live bookkeeping: open orders are reconciled with the exchange every minute (an order that is gone was filled or
cancelled there), own fills go to live_fills, whether the orders score for rewards is sampled, and the rewards
Polymarket paid are read once a day into live_rewards – next to the paper estimate, that is what the pilot checks.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

log = logging.getLogger("polyarb.orders")
MODES = ("off", "dry", "live")
Key = Tuple[str, str]  # (token, "BUY" | "SELL")


def read_mode(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            m = json.load(f).get("mode", "off")
        return m if m in MODES else "off"
    except (OSError, ValueError, AttributeError):
        return "off"


def write_mode(path: str, mode: str, why: str = "") -> None:
    if mode not in MODES:
        raise ValueError(mode)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"mode": mode, "ts": time.time(), **({"why": why} if why else {})}, f)
    os.replace(tmp, path)


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


class Gateway:
    def __init__(self, name: str, db, mode_path: str, status_path: str, limits: Optional[dict] = None,
                 exchange=None, secrets_path: str = "", geo=None, value=None):
        self.name, self.db = name, db
        self.mode_path, self.status_path = mode_path, status_path
        lim = limits or {}
        self.max_order = float(lim.get("max_order_usd", 30))
        self.max_market = float(lim.get("max_market_usd", 60))
        self.max_total = float(lim.get("max_total_usd", 300))
        self.allow_real = bool(lim.get("allow_real", False))
        self.max_loss = float(lim.get("max_daily_loss_usd", 50))
        # all money paid in so far (config, kept up to date by hand): a deposit must not look like a profit that
        # hides the day's losses from the limit below, and the result since the start is equity - deposits
        self.deposits = float(lim.get("deposits_usd", 0) or 0)
        self.orders: Dict[Key, dict] = {}   # (token, side) -> {price, size, oid, ts, label}
        self.counts = {"gesetzt": 0, "storniert": 0, "abgelehnt": 0}
        self.rejected: Dict[Key, tuple] = {}  # last logged rejection per order
        self.retry_s = float(lim.get("retry_after_reject_s", 60))
        self._ex_reject: Dict[Key, float] = {}  # when the exchange last refused this order: no retry before retry_s
        self._sold: Dict[str, float] = {}  # token -> when sell_now last sent an order (at most one per retry_s)
        self._seq = 0
        self.mode = "off"
        self.live = False
        self.why = ""
        self._ex, self.secrets_path = exchange, secrets_path
        from .exchange import account_value, geoblock
        self._geo_fn, self._value_fn = geo or geoblock, value or account_value
        self._geo: Optional[dict] = None
        self._last = dict(geo=-1e18, reconcile=-1e18, equity=-1e18, scoring=-1e18, rewards="")
        self.info: dict = {}
        self.est: Dict[str, dict] = {}  # set by the strategy: condition id -> {q, rate, pct} (our estimated share)
        self.day_path = status_path.replace("live-status-", "live-day-")
        db.execute("""CREATE TABLE IF NOT EXISTS live_orders(ts REAL, mode TEXT, action TEXT, token TEXT, label TEXT,
                      price REAL, size REAL, oid TEXT, note TEXT)""")
        db.execute("""CREATE TABLE IF NOT EXISTS live_fills(id TEXT PRIMARY KEY, ts REAL, token TEXT, side TEXT,
                      price REAL, size REAL, role TEXT)""")
        db.execute("CREATE TABLE IF NOT EXISTS live_rewards(day TEXT PRIMARY KEY, usd REAL, ts REAL)")
        db.execute("CREATE TABLE IF NOT EXISTS live_meta(k TEXT PRIMARY KEY, v TEXT)")
        # every 10 min: Polymarket's own share of each pool we quote in, next to our estimate (counts also below
        # the $1 payout minimum) – does our reward estimate, on which the whole case rests, match reality?
        db.execute("""CREATE TABLE IF NOT EXISTS live_reward_share(ts REAL, cid TEXT, question TEXT, rate REAL,
                      pm_pct REAL, est_pct REAL, pm_earned REAL)""")
        # drop the repeated rejections logged before they were deduplicated (keeps the first of each)
        db.execute("""DELETE FROM live_orders WHERE action='reject' AND rowid NOT IN
                      (SELECT MIN(rowid) FROM live_orders WHERE action='reject' GROUP BY token, price, size, note)""")

    # ------------------------------------------------------------------ helpers
    def _log(self, now: float, action: str, key: Key, label: str, price: float, size: float, oid: str, note: str = ""):
        self.db.execute("INSERT INTO live_orders VALUES(?,?,?,?,?,?,?,?,?)",
                        (now, self.mode, action, key[0], (label + ("" if key[1] == "BUY" else " (Verkauf)"))[:120],
                         price, size, oid, note))

    def open_usd(self) -> float:
        return sum(o["price"] * o["size"] for k, o in self.orders.items() if k[1] == "BUY")

    @property
    def exchange(self):
        if self._ex is None:
            from .exchange import PolyExchange, read_wallet, wallet_problem
            w = read_wallet(self.secrets_path)
            if wallet_problem(w):
                return None
            self._ex = PolyExchange(w)
        return self._ex

    def _cancel(self, now: float, key: Key, why: str) -> None:
        o = self.orders.pop(key)
        if self.live and not o["oid"].startswith("dry-"):
            try:
                self.exchange.cancel(o["oid"])
            except Exception as e:  # noqa – gone already (filled) or a hiccup: reconcile sorts it out
                why += f" (Börse: {type(e).__name__})"
        self.counts["storniert"] += 1
        self._log(now, "cancel", key, o.get("label", ""), o["price"], o["size"], o["oid"], why)

    def _reject(self, now: float, key: Key, label: str, price: float, size: float, why: str) -> None:
        if self.rejected.get(key) != (price, size, why):
            self.rejected[key] = (price, size, why)
            self.counts["abgelehnt"] += 1
            self._log(now, "reject", key, label, price, size, "", why)

    def _place(self, now: float, key: Key, label: str, price: float, size: float) -> None:
        usd = price * size
        why = None
        if key[1] == "BUY":
            same = sum(o["price"] * o["size"] for k, o in self.orders.items() if k == key)
            why = ("Order > max_order_usd" if usd > self.max_order + 1e-9 else
                   "Markt > max_market_usd" if same + usd > self.max_market + 1e-9 else
                   "gesamt > max_total_usd" if self.open_usd() + usd > self.max_total + 1e-9 else None)
        if why:
            return self._reject(now, key, label, price, size, why)
        if self.live:
            if now - self._ex_reject.get(key, -1e18) < self.retry_s:
                return  # refused a moment ago (balance, post-only would cross, ...): do not hammer the exchange
            try:
                oid = self.exchange.place(key[0], key[1], price, size)
            except Exception as e:  # noqa – the exchange said no (post-only would cross, balance, tick, ...)
                self._ex_reject[key] = now
                return self._reject(now, key, label, price, size, f"Börse: {str(e)[:100] or type(e).__name__}")
            self._ex_reject.pop(key, None)
        else:
            self._seq += 1
            oid = f"dry-{self.name}-{self._seq}"
        self.rejected.pop(key, None)
        self.orders[key] = dict(price=price, size=size, oid=oid, ts=now, label=label)
        self.counts["gesetzt"] += 1
        self._log(now, "place", key, label, price, size, oid)

    def sell_now(self, now: float, token: str, price: float, size: float, label: str = "") -> bool:
        """Inventory exit: sell `size` at once at `price` (the bid), fill-and-kill – nothing rests. Only in live
        mode (dry: the simulation sells by itself); at most once per retry_s per token. The fill comes back through new_fills()."""
        key = (token, "SELL")
        if not self.live:
            return False
        if now - self._sold.get(token, -1e18) < self.retry_s:
            return False
        self._sold[token] = now
        try:
            oid = self.exchange.sell_now(token, price, size)
        except Exception as e:  # noqa
            self._reject(now, key, label, price, size, f"Sofortverkauf: {str(e)[:100] or type(e).__name__}")
            return False
        self.counts["gesetzt"] += 1
        self._log(now, "dump", key, label, price, size, oid, "Sofortverkauf zum Bid")
        return True

    def real_positions(self, now: float) -> Optional[Dict[str, dict]]:
        """The account's positions per token, read at most every 5 min (None between reads or if unreadable)."""
        if not self.live or now - self._last.get("positions", -1e18) < 300:
            return None
        self._last["positions"] = now
        from .exchange import positions
        return positions(self.exchange.user)

    # ------------------------------------------------------------------ safety
    def _live_allowed(self, now: float, mode: str) -> str:
        """'' if real orders may be sent, else why not."""
        if mode != "live":
            return "Modus Trockenlauf"
        if not self.allow_real:
            return "für dieses Szenario nicht freigegeben (allow_real)"
        if self.exchange is None:
            return "keine gültige Wallet in /admin/"
        if now - self._last["geo"] > 3600 or self._geo is None:
            self._last["geo"] = now
            self._geo = self._geo_fn()
        if self._geo.get("blocked") is None:
            return "Geoblock-Prüfung nicht erreichbar"
        if self._geo.get("blocked"):
            return f"Polymarket sperrt diesen Server ({self._geo.get('country', '?')})"
        return ""

    def _equity_check(self, now: float) -> bool:
        """Daily loss limit on cash + positions. False (and everything off) when it is hit."""
        if now - self._last["equity"] < 300:
            return True
        self._last["equity"] = now
        ex = self.exchange
        try:
            cash, pos = ex.balance_usd(), self._value_fn(ex.user)
        except Exception as e:  # noqa
            log.warning("%s: Kontostand nicht lesbar (%s)", self.name, type(e).__name__)
            return True
        if cash is None or pos is None:
            return True
        eq = cash + pos
        day = _day(now)
        try:
            with open(self.day_path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        if d.get("day") != day or d.get("dep") != self.deposits:  # new day, or money was paid in/out: new base
            d = dict(day=day, start=eq, dep=self.deposits)
            tmp = self.day_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f)
            os.replace(tmp, self.day_path)
        self.info.update(cash=round(cash, 2), positions=round(pos, 2), equity=round(eq, 2), day_start=round(d["start"], 2),
                         deposits=self.deposits, pnl_total=round(eq - self.deposits, 2) if self.deposits else None)
        if eq < d["start"] - self.max_loss:
            why = f"Tagesverlust {d['start'] - eq:.2f} $ > {self.max_loss:.0f} $"
            log.error("%s: %s – Not-Aus", self.name, why)
            write_mode(self.mode_path, "off", why)
            self._stop_all(now, why)
            return False
        return True

    def _stop_all(self, now: float, why: str) -> None:
        for k in list(self.orders):
            self._cancel(now, k, why)
        try:
            self.exchange.cancel_all()
        except Exception as e:  # noqa
            log.error("%s: cancel_all fehlgeschlagen (%s)", self.name, type(e).__name__)

    def _meta(self, k: str, default=None):
        r = self.db.execute("SELECT v FROM live_meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def _set_meta(self, k: str, v) -> None:
        self.db.execute("INSERT OR REPLACE INTO live_meta VALUES(?,?)", (k, str(v)))

    def new_fills(self) -> list:
        """Real fills not yet handed to the strategy (each exactly once, also across restarts):
        [(token, side, price, size)]. Fills from before the switch to live are never handed over."""
        cur = int(self._meta("fill_cursor", 0))
        rows = self.db.execute("SELECT rowid, token, side, price, size FROM live_fills WHERE rowid > ? AND ts >= ? "
                               "ORDER BY rowid", (cur, float(self._meta("live_since", 9e18)))).fetchall()
        last = self.db.execute("SELECT MAX(rowid) FROM live_fills").fetchone()[0] or cur
        if last != cur:
            self._set_meta("fill_cursor", last)
        return [(t, sd, p, q) for _, t, sd, p, q in rows]

    # ------------------------------------------------------------------ live bookkeeping
    def _reconcile(self, now: float) -> None:
        if now - self._last["reconcile"] < 60:
            return
        self._last["reconcile"] = now
        ex = self.exchange
        try:
            open_ids = set(ex.open_orders())
            for k, o in list(self.orders.items()):
                if o["oid"] not in open_ids:  # filled (or cancelled by the exchange): place anew if still wanted
                    self.orders.pop(k)
                    self._log(now, "gone", k, o.get("label", ""), o["price"], o["size"], o["oid"], "nicht mehr offen")
            last = self.db.execute("SELECT MAX(ts) FROM live_fills").fetchone()[0] \
                or float(self._meta("live_since", now))
            for t in ex.trades(int(last) - 60):
                self.db.execute("INSERT OR IGNORE INTO live_fills VALUES(?,?,?,?,?,?,?)",
                                (t["id"], t["ts"], t["token"], t["side"], t["price"], t["size"], t["role"]))
            self.info["fills"] = self.db.execute("SELECT COUNT(*) FROM live_fills").fetchone()[0]
        except Exception as e:  # noqa
            log.warning("%s: Abgleich fehlgeschlagen (%s: %s)", self.name, type(e).__name__, str(e)[:120])
        if now - self._last["scoring"] >= 600 and self.orders:
            self._last["scoring"] = now
            try:
                sc = ex.scoring([o["oid"] for o in self.orders.values()])
                self.info["scoring"] = f"{sum(sc.values())}/{len(sc)}"
            except Exception as e:  # noqa
                self.info["scoring"] = f"? ({type(e).__name__})"
            self._reward_share(now)
        day = _day(now - 86400)
        if self._last["rewards"] != day and datetime.fromtimestamp(now, timezone.utc).hour >= 1:
            self._last["rewards"] = day
            usd = ex.rewards(day)
            if usd is not None:
                self.db.execute("INSERT OR REPLACE INTO live_rewards VALUES(?,?,?)", (day, usd, now))
            self.info["rewards_yesterday"] = usd

    def _reward_share(self, now: float) -> None:
        fn = getattr(self.exchange, "reward_live", None)
        if fn is None or not self.est:
            return
        try:
            pm = fn()
        except Exception as e:  # noqa
            log.warning("%s: Reward-Anteil nicht abrufbar (%s)", self.name, type(e).__name__)
            return
        # all markets of the day, also the ones we left meanwhile (the bot only compares the current ones below)
        self.info["earned_today"] = round(sum(float(v.get("earned") or 0) for v in pm.values()), 4)
        self.info["earned_ts"] = now
        rows = []
        for cid, e in self.est.items():
            p = pm.get(cid, {})
            row = (now, cid, str(e.get("q", ""))[:120], float(e.get("rate") or 0), p.get("pct"), e.get("pct"),
                   p.get("earned"))
            self.db.execute("INSERT INTO live_reward_share VALUES(?,?,?,?,?,?,?)", row)
            rows.append(dict(q=row[2][:60], rate=row[3], pm_pct=row[4], est_pct=row[5], earned_today=row[6],
                             pm_day=round(row[3] * row[4] / 100, 2) if row[4] is not None else None,
                             est_day=round(row[3] * row[5] / 100, 2) if row[5] is not None else None))
        self.db.commit()
        self.info["reward_share"] = rows[:10]

    # ------------------------------------------------------------------ main
    def sync(self, now: float, want: Dict) -> str:
        """want: (token, "BUY"|"SELL") -> (price, size, label) of every order the strategy wants resting now
        (a plain token key means a buy)."""
        want = {(k if isinstance(k, tuple) else (k, "BUY")): v for k, v in want.items()}
        mode = read_mode(self.mode_path)
        if mode == "off":
            if self.orders or self.live:  # emergency stop / switched off: cancel everything we hold
                for k in list(self.orders):
                    self._cancel(now, k, "ausgeschaltet")
                if self.live:
                    self._stop_all(now, "ausgeschaltet")
            self.mode, self.live, self.why = mode, False, "ausgeschaltet"
            self._status(now, "aus")
            return mode
        why = self._live_allowed(now, mode)
        live = not why
        if live != self.live:  # switching between dry and real: start from a clean book on both sides
            for k in list(self.orders):
                self._cancel(now, k, "Moduswechsel")
            if live:
                try:
                    self.exchange.cancel_all()
                    if self._meta("live_since") is None:
                        self._set_meta("live_since", now - 5)  # fills from here on belong to this strategy
                except Exception as e:  # noqa
                    why, live = f"Börse nicht erreichbar ({type(e).__name__})", False
        self.live, self.why = live, why
        self.mode = "live" if live else "dry"
        if live and not self._equity_check(now):
            self.mode, self.live = "off", False
            self._status(now, "aus (Tagesverlust)")
            return "off"
        if live:
            self._reconcile(now)
        for k in list(self.orders):
            w = want.get(k)
            if w is None:
                self._cancel(now, k, "nicht mehr gewollt")
            elif abs(w[0] - self.orders[k]["price"]) > 1e-9:
                self._cancel(now, k, f"Preis {self.orders[k]['price']:.3f} -> {w[0]:.3f}")
        for k in [k for k in self.rejected if k not in want]:
            del self.rejected[k]
        for k, (price, size, label) in want.items():
            if k not in self.orders and size > 0:
                self._place(now, k, label, price, size)
        self._status(now, "ECHT" if live else "Trockenlauf" + (f" – {why}" if mode == "live" else ""))
        return mode

    def _status(self, now: float, mode_text: str) -> None:
        try:
            tmp = self.status_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(dict(scenario=self.name, mode=mode_text, live=self.live, ts=now, open_orders=len(self.orders),
                               open_usd=round(self.open_usd(), 2), geoblock=self._geo,
                               limits=dict(max_order_usd=self.max_order, max_market_usd=self.max_market,
                                           max_total_usd=self.max_total, max_daily_loss_usd=self.max_loss,
                                           allow_real=self.allow_real), **self.info, **self.counts), f)
            os.replace(tmp, self.status_path)
        except OSError:
            pass
