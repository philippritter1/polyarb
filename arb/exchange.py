"""Real Polymarket access for the order gateway (arb/orders.py) – only used in live mode.

Wraps the official client (py-clob-client-v2 – Polymarket switched to CLOB V2 with pUSD collateral on 28.04.2026,
the old py-clob-client no longer works against production; imported lazily, the paper bot and the tests do not need it):
sign and post GTC limit orders as post-only (a maker order that would cross the book is rejected instead of
taking), cancel, list open orders and own trades, read the USDC balance, ask whether orders score for
liquidity rewards, and read the rewards actually earned. Plus the public checks the gateway needs before it
sends anything: the geoblock answer for the server's IP and the account value from the data API.

Credentials come from data/secrets.env (entered on /admin/, mode 600) or the environment:
  POLY_PRIVATE_KEY   key of the wallet that signs (a wallet used only for the bot, holding only the pilot money)
  POLY_FUNDER        address that holds the funds (the Polymarket proxy wallet shown on polymarket.com)
  POLY_SIG_TYPE      0 = plain wallet (EOA), 1 = Polymarket proxy (email), 2 = Gnosis safe (browser wallet),
                     3 = smart-contract wallet (EIP-1271)
The API key/secret/passphrase are derived from the private key on first use; nothing secret is logged.
"""
from __future__ import annotations

import logging
import math
import os
import re
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

import requests

log = logging.getLogger("polyarb.exchange")
CLOB = "https://clob.polymarket.com"
GEOBLOCK = "https://polymarket.com/api/geoblock"
DATA_API = "https://data-api.polymarket.com"
CHAIN_ID = 137  # Polygon
KEY_RE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def read_wallet(secrets_path: str) -> dict:
    """{key, funder, sig_type} from the environment or data/secrets.env; missing values are empty."""
    vals = {}
    try:
        with open(secrets_path, encoding="utf-8") as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k and v:
                    vals[k] = v
    except OSError:
        pass
    get = lambda n: os.environ.get(n) or vals.get(n, "")  # noqa: E731
    try:
        sig = int(get("POLY_SIG_TYPE") or 1)
    except ValueError:
        sig = 1
    return dict(key=get("POLY_PRIVATE_KEY"), funder=get("POLY_FUNDER"), sig_type=sig)


def wallet_problem(w: dict) -> str:
    """'' if the wallet settings look usable, else what is missing."""
    if not KEY_RE.match(w.get("key") or ""):
        return "kein gültiger Private Key"
    if w.get("sig_type") in (1, 2, 3) and not ADDR_RE.match(w.get("funder") or ""):
        return "Funder-Adresse fehlt (Proxy-Wallet von polymarket.com)"
    if w.get("sig_type") not in (0, 1, 2, 3):
        return "Signaturtyp muss 0, 1, 2 oder 3 sein"
    return ""


def address_of(key: str) -> str:
    """Public address of a private key (shown on /admin/ instead of the key)."""
    from eth_account import Account
    return Account.from_key(key if key.startswith("0x") else "0x" + key).address


def geoblock(get: Callable = None) -> dict:
    """Polymarket's own answer for this server's IP: {blocked, country, ...}; blocked=None if unknown."""
    try:
        r = (get or requests.get)(GEOBLOCK, timeout=15)
        d = r.json() if hasattr(r, "json") else r
        return dict(blocked=bool(d.get("blocked")), country=d.get("country", ""), region=d.get("region", ""))
    except Exception as e:  # noqa – unknown counts as "do not trade"
        return dict(blocked=None, country="", error=type(e).__name__)


POLYGON_RPC = ("https://polygon-bor-rpc.publicnode.com", "https://polygon-rpc.com", "https://1rpc.io/matic")
TOKENS = {"pUSD": "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB",   # Polymarket's collateral since CLOB V2
          "USDC.e": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", "USDC": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"}


def chain_balances(addresses: Dict[str, str], post: Callable = None) -> Dict[str, str]:
    """What the blockchain itself says (public Polygon RPC): USDC.e / USDC per address and whether the address is
    a contract (Polymarket proxy/safe wallets are). Diagnostic only – for when the exchange reports 0."""
    def rpc(method, params):
        for url in POLYGON_RPC:
            try:
                r = (post or requests.post)(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                            timeout=10)
                d = r.json() if hasattr(r, "json") else r
                if "result" in d:
                    return d["result"]
            except Exception:  # noqa – next endpoint
                continue
        raise RuntimeError("kein Polygon-Knoten erreichbar")
    out = {}
    for name, addr in addresses.items():
        if not ADDR_RE.match(addr or ""):
            continue
        parts = []
        for sym, tok in TOKENS.items():
            raw = rpc("eth_call", [{"to": tok, "data": "0x70a08231" + addr[2:].lower().rjust(64, "0")}, "latest"])
            parts.append(f"{sym} {int(raw, 16) / 1e6:.2f}")
        code = rpc("eth_getCode", [addr, "latest"])
        parts.append("Vertrag" if code not in ("0x", "0x0", None) else "normale Adresse")
        out[name] = ", ".join(parts)
    return out


def own_fills(trades: List[dict], user: str) -> List[dict]:
    """Our side of each trade. As TAKER the trade's own fields are ours. As MAKER they describe the taker's order –
    our fill is the entry in maker_orders with our address: its asset, price and matched amount. Its side is
    given, or follows from the matching rule: same token = the opposite side of the taker, complementary token
    (YES bid met NO bid by minting / two asks by merging) = the same side."""
    me, out = (user or "").lower(), []
    for t in trades:
        ts = float(t.get("match_time") or 0)
        if str(t.get("trader_side", "")).upper() != "MAKER":
            out.append(dict(id=str(t.get("id")), token=str(t.get("asset_id")), side=str(t.get("side", "")).upper(),
                            price=float(t.get("price") or 0), size=float(t.get("size") or 0), ts=ts, role="TAKER"))
            continue
        taker_side, taker_tok = str(t.get("side", "")).upper(), str(t.get("asset_id"))
        for mo in t.get("maker_orders") or []:
            if str(mo.get("maker_address", "")).lower() != me:
                continue
            tok = str(mo.get("asset_id"))
            side = str(mo.get("side") or "").upper() or (
                ("SELL" if taker_side == "BUY" else "BUY") if tok == taker_tok else taker_side)
            out.append(dict(id=f"{t.get('id')}:{mo.get('order_id')}", token=tok, side=side,
                            price=float(mo.get("price") or 0), size=float(mo.get("matched_amount") or 0), ts=ts,
                            role="MAKER"))
    return out


def account_value(user: str, get: Callable = None) -> Optional[float]:
    """Current value of all positions of `user` (data API, public); None if unavailable."""
    try:
        r = (get or requests.get)(f"{DATA_API}/value", params={"user": user}, timeout=15)
        d = r.json() if hasattr(r, "json") else r
        if isinstance(d, list):
            d = d[0] if d else {}
        return float(d.get("value", 0) or 0)
    except Exception:  # noqa
        return None


class PolyExchange:
    """Thin layer over py-clob-client with the calls the gateway needs. `client` may be injected (tests)."""

    def __init__(self, wallet: dict, client=None, host: str = CLOB):
        self.wallet = wallet
        self._c = client
        self.host = host

    def _client(self, sig_type: int, creds=None):
        from py_clob_client_v2.client import ClobClient
        w = self.wallet
        key = w["key"] if w["key"].startswith("0x") else "0x" + w["key"]
        c = ClobClient(self.host, chain_id=CHAIN_ID, key=key, signature_type=sig_type, funder=w.get("funder") or None)
        c.set_api_creds(creds or c.create_or_derive_api_key())
        return c

    @property
    def c(self):
        if self._c is None:
            self._c = self._client(self.wallet["sig_type"])
        return self._c

    @property
    def user(self) -> str:
        """The address that holds funds and positions (proxy wallet, or the signer for sig type 0)."""
        return self.wallet.get("funder") or address_of(self.wallet["key"])

    def best(self, token: str) -> dict:
        """{bid, ask, tick, min_size} of a token's book."""
        b = self.c.get_order_book(str(token)) or {}
        bids = sorted((float(x["price"]) for x in b.get("bids") or []), reverse=True)
        asks = sorted(float(x["price"]) for x in b.get("asks") or [])
        return dict(bid=bids[0] if bids else None, ask=asks[0] if asks else None,
                    tick=float(b.get("tick_size") or 0.01), min_size=float(b.get("min_order_size") or 5))

    # ------------------------------------------------------------------ orders
    def place(self, token: str, side: str, price: float, size: float) -> str:
        """Post-only GTC limit order; returns the order id or raises with the exchange's reason."""
        from py_clob_client_v2.clob_types import OrderArgsV2, OrderType
        from py_clob_client_v2.order_builder.constants import BUY, SELL
        order = self.c.create_order(OrderArgsV2(token_id=str(token), price=float(price), size=float(size),
                                                side=BUY if side == "BUY" else SELL))
        resp = self.c.post_order(order, OrderType.GTC, post_only=True) or {}
        oid = resp.get("orderID") or resp.get("orderId") or ""
        if not resp.get("success", bool(oid)) or not oid:
            raise RuntimeError(resp.get("errorMsg") or resp.get("error") or "Order abgelehnt")
        return oid

    def cancel(self, order_id: str) -> None:
        from py_clob_client_v2.clob_types import OrderPayload
        self.c.cancel_order(OrderPayload(orderID=order_id))

    def cancel_all(self) -> None:
        self.c.cancel_all()

    def open_orders(self) -> Dict[str, dict]:
        """order id -> {token, side, price, size, matched}."""
        from py_clob_client_v2.clob_types import OpenOrderParams
        out = {}
        for o in self.c.get_open_orders(OpenOrderParams()) or []:
            out[o.get("id")] = dict(token=str(o.get("asset_id")), side=str(o.get("side", "")).upper(),
                                    price=float(o.get("price") or 0), size=float(o.get("original_size") or 0),
                                    matched=float(o.get("size_matched") or 0))
        return out

    def trades(self, after_ts: int) -> List[dict]:
        """OUR fills since `after_ts` (unix s): {id, token, side, price, size, ts, role}."""
        from py_clob_client_v2.clob_types import TradeParams
        return own_fills(self.c.get_trades(TradeParams(after=int(after_ts))) or [], self.user)

    def balance_usd(self, sig_type: Optional[int] = None) -> Optional[float]:
        """pUSD available for orders; `sig_type` asks as another signature type than the configured one."""
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams
        c = self.c if sig_type is None or sig_type == self.wallet["sig_type"] else self._client(sig_type, self.c.creds)
        p = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        try:  # the exchange caches the balance: refresh it from the chain first (a fresh deposit showed 0)
            c.update_balance_allowance(p)
        except Exception as e:  # noqa
            log.warning("balance refresh: %s", type(e).__name__)
        d = c.get_balance_allowance(p) or {}
        try:
            return float(d.get("balance", 0)) / 1e6  # 6 decimals
        except (TypeError, ValueError):
            return None

    def scoring(self, order_ids: List[str]) -> Dict[str, bool]:
        """Does each order count for liquidity rewards right now (Polymarket's own check)?"""
        if not order_ids:
            return {}
        from py_clob_client_v2.clob_types import OrdersScoringParams
        d = self.c.are_orders_scoring(OrdersScoringParams(orderIds=list(order_ids))) or {}
        return {k: bool(v) for k, v in d.items()}

    def reward_live(self, day: Optional[str] = None) -> Dict[str, dict]:
        """Polymarket's own view of our reward business, also below the $1 payout minimum:
        {condition id: {"pct": our share of the market's pool right now in %, "earned": $ earned on `day` so far}}
        (day YYYY-MM-DD UTC, default today). Either part may be missing if its endpoint fails."""
        day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        out: Dict[str, dict] = {}
        try:
            d = self.c.get_reward_percentages() or {}
            d = d.get("data", d) if isinstance(d, dict) else {}
            for cid, v in (d.items() if isinstance(d, dict) else []):
                try:
                    out.setdefault(str(cid), {})["pct"] = float(v)
                except (TypeError, ValueError):
                    pass
        except Exception as e:  # noqa
            log.warning("reward percentages: %s", type(e).__name__)
        try:
            for r in self.c.get_earnings_for_user_for_day(day) or []:
                if isinstance(r, dict) and r.get("condition_id"):
                    e = out.setdefault(str(r["condition_id"]), {})
                    e["earned"] = round(e.get("earned", 0.0) + float(r.get("earnings", 0) or 0), 6)
        except Exception as e:  # noqa
            log.warning("reward earnings %s: %s", day, type(e).__name__)
        log.info("reward_live %s: Anteil für %d Märkte, Verdienst für %d", day,
                 sum("pct" in v for v in out.values()), sum("earned" in v for v in out.values()))
        return out

    def rewards(self, day: Optional[str] = None) -> Optional[float]:
        """Liquidity rewards earned on `day` (YYYY-MM-DD, UTC; default yesterday); None if unavailable."""
        day = day or datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() - 86400, timezone.utc).strftime("%Y-%m-%d")
        try:
            d = self.c.get_total_earnings_for_user_for_day(day)
            rows = d if isinstance(d, list) else (d.get("data") or [d]) if isinstance(d, dict) else []
            return round(sum(float(r.get("earnings", 0) or 0) for r in rows if isinstance(r, dict)), 6)
        except Exception as e:  # noqa
            log.warning("rewards %s: %s", day, type(e).__name__)
            return None


def liquid_tokens(get: Callable = None, n: int = 8) -> List[dict]:
    """Busy markets for the connection test, most traded first: [{token, question}] (books checked by the caller)."""
    import json as _json
    r = (get or requests.get)("https://gamma-api.polymarket.com/markets", params={
        "active": "true", "closed": "false", "order": "volume24hr", "ascending": "false", "limit": 40}, timeout=20)
    out = []
    for m in (r.json() if hasattr(r, "json") else r) or []:
        try:
            toks, prices = _json.loads(m.get("clobTokenIds") or "[]"), _json.loads(m.get("outcomePrices") or "[]")
            if m.get("enableOrderBook") and m.get("acceptingOrders", True) and len(toks) == 2 \
                    and 0.2 <= float(prices[0]) <= 0.8:
                out.append(dict(token=str(toks[0]), question=m.get("question", "")))
        except (ValueError, TypeError, IndexError):
            continue
    return out[:n]


def connection_test(ex: "PolyExchange", find=None, wait=None) -> List[dict]:
    """One small, controlled round trip with real money but (almost) no risk: read the balance, place ONE post-only
    buy of the minimum size a few ticks BELOW the best bid of a busy market (so it is practically never filled),
    see it among the open orders, ask whether it scores, cancel it and check it is gone. Every step is reported;
    the order is cancelled whatever happens after it was placed."""
    import time as _t
    steps, oid = [], ""

    def step(name, fn):
        try:
            v = fn()
            steps.append(dict(step=name, ok=True, info=v))
            return v
        except Exception as e:  # noqa
            steps.append(dict(step=name, ok=False, info=f"{type(e).__name__}: {str(e)[:160]}"))
            return None
    if step("Zugang (API-Schlüssel aus dem Private Key)", lambda: bool(ex.c) and "ok") is None:
        return steps
    bal = step("Guthaben (pUSD)", ex.balance_usd)
    def two_sided():  # the most traded market whose book has a bid AND an ask (a finished match has neither)
        for c in (find or liquid_tokens)():
            b = ex.best(c["token"])
            if b["bid"] is not None and b["ask"] is not None and b["ask"] - b["bid"] <= 0.05:
                return dict(c, book=b)
        raise RuntimeError("kein Markt mit Geboten auf beiden Seiten gefunden")
    m = step("Markt für den Test", two_sided)
    if not m:
        return steps
    bk = m.pop("book")
    steps[-1]["info"] = f"{m['question'][:80]} – bestes Gebot {bk['bid']:.2f}, bester Ask {bk['ask']:.2f}"
    price = round(max(bk["tick"], bk["bid"] - 3 * bk["tick"]), 4)
    size = max(bk["min_size"], 5.0)
    if bal is not None and price * size > bal:
        hint = ""
        if bal == 0:  # usually the wrong signature type: look where the money is
            found = {}
            for t in (0, 1, 2, 3):
                if t != ex.wallet.get("sig_type"):
                    try:
                        found[t] = ex.balance_usd(t)
                    except Exception:  # noqa
                        found[t] = None
            steps.append(dict(step="Guthaben je Signaturtyp", ok=True,
                              info=", ".join(f"Typ {t}: {v if v is not None else '?'}" for t, v in found.items())))
            try:
                from_chain = chain_balances({"Funder": ex.wallet.get("funder") or "",
                                             "Signer": address_of(ex.wallet["key"])})
                steps.append(dict(step="Blockchain direkt", ok=True, info=" | ".join(f"{k}: {v}" for k, v in from_chain.items())))
            except Exception as e:  # noqa
                steps.append(dict(step="Blockchain direkt", ok=False, info=f"{type(e).__name__}: {str(e)[:120]}"))
            rich = [t for t, v in found.items() if v]
            hint = (f" – das Geld liegt bei Signaturtyp {rich[0]}: auf /admin/ Signaturtyp {rich[0]} speichern "
                    "(Key-Feld leer lassen) und erneut testen" if rich else
                    " – bei keinem Signaturtyp Guthaben sichtbar: Funder-Adresse prüfen oder die Einzahlung abwarten")
        steps.append(dict(step="Testorder", ok=False, info=f"zu wenig Guthaben: braucht {price * size:.2f} ${hint}"))
        return steps
    try:
        oid = step(f"Testorder setzen: KAUF {size:g} @ {price:.2f} (bestes Gebot {bk['bid']:.2f}) – {m['question'][:60]}",
                   lambda: ex.place(m["token"], "BUY", price, size))
        if not oid:
            return steps
        (wait or _t.sleep)(2)
        step("Order bei Polymarket offen", lambda: oid in ex.open_orders() or (_ for _ in ()).throw(RuntimeError("nicht gefunden")))
        step("Zählt für Rewards (Polymarkets Prüfung)", lambda: ex.scoring([oid]).get(oid))
    finally:
        if oid:
            step("Order stornieren", lambda: ex.cancel(oid) or "ok")
            (wait or _t.sleep)(2)
            step("Order weg", lambda: oid not in ex.open_orders() or (_ for _ in ()).throw(RuntimeError("noch offen!")))
    return steps


def test_passed(steps: List[dict]) -> bool:
    """Only a test that went all the way – order placed, found, cancelled and gone – counts as passed."""
    return bool(steps) and all(x.get("ok") for x in steps) and any(x.get("step") == "Order weg" for x in steps)


def reward_markets(get: Callable = None, n: int = 300) -> List[dict]:
    """Markets that pay liquidity rewards, both outcome tokens each: [{token, question, rate, v, min_size, price}].
    Same reading of Polymarket's reward fields as the paper market maker (arb/mm.rewards_of)."""
    import json as _json
    from .mm import rewards_of
    r = (get or requests.get)("https://gamma-api.polymarket.com/markets", params={
        "active": "true", "closed": "false", "order": "volume24hr", "ascending": "false", "limit": 500,
        "liquidity_num_min": 500}, timeout=30)
    out = []
    for m in (r.json() if hasattr(r, "json") else r) or []:
        try:
            rate, v, size = rewards_of(m)
            toks, prices = _json.loads(m.get("clobTokenIds") or "[]"), _json.loads(m.get("outcomePrices") or "[]")
            if rate > 0 and v >= 0.02 and m.get("enableOrderBook") and m.get("acceptingOrders", True) \
                    and len(toks) == 2 and 0.1 <= float(prices[0]) <= 0.9:
                for i in (0, 1):  # bid on either side: the cheaper one needs less money for the reward minimum
                    out.append(dict(token=str(toks[i]), question=m.get("question", "") + (" – NEIN" if i else " – JA"),
                                    rate=rate, v=v, min_size=size, price=float(prices[i])))
        except (ValueError, TypeError, IndexError):
            continue
    return out[:n]


def reward_test(ex: "PolyExchange", find=None, wait=None, checks: int = 4, every_s: float = 20) -> List[dict]:
    """Does a real quote of ours count for liquidity rewards? In a market WITH a reward pool: one post-only buy of
    the reward minimum size, inside the qualifying distance from the mid but never above the best bid (so it does not
    cross), asked `checks` times every `every_s` seconds whether it scores, then cancelled. Closer to the mid than the
    connection test, so it CAN be filled in that minute – a fill is reported (the shares then stay in the account)."""
    import time as _t
    pause = wait or _t.sleep
    steps, oid = [], ""

    def step(name, fn):
        try:
            v = fn()
            steps.append(dict(step=name, ok=True, info=v))
            return v
        except Exception as e:  # noqa
            steps.append(dict(step=name, ok=False, info=f"{type(e).__name__}: {str(e)[:160]}"))
            return None
    bal = step("Guthaben (pUSD)", ex.balance_usd)
    if bal is None:
        return steps

    def pick():
        cands = list((find or reward_markets)())
        # cheapest reward minimum first (fits the balance), then the bigger pool
        cands.sort(key=lambda c: (c["min_size"] * c.get("price", 0.5) > bal * 0.95, c["min_size"] * c.get("price", 0.5),
                                  -c["rate"]))
        cheapest = None
        for c in cands[:40]:
            b = ex.best(c["token"])
            if b["bid"] is None or b["ask"] is None or b["ask"] - b["bid"] > c["v"]:
                continue  # one-sided or too wide to quote inside the reward distance
            mid, tick = (b["bid"] + b["ask"]) / 2, b["tick"]
            price = round(min(b["bid"], math.floor((mid - c["v"] / 2) / tick + 1e-9) * tick), 4)
            size = max(c["min_size"], b["min_size"], 5.0)
            if price <= 0 or mid - price >= c["v"]:
                continue
            cheapest = min(cheapest or 1e9, price * size)
            if price * size > bal:
                continue
            return dict(c, price=price, size=size, mid=mid, bid=b["bid"])
        raise RuntimeError(f"{len(cands)} Reward-Angebote geprüft, keins passt ins Guthaben ({bal:.2f} $)"
                           + (f"; das günstigste bräuchte {cheapest:.2f} $" if cheapest else ""))
    m = step("Reward-Markt", pick)
    if not m:
        return steps
    steps[-1]["info"] = (f"{m['question'][:70]} – Pool {m['rate']:.0f} $/Tag, Abstand max. {m['v'] * 100:.1f} ct, "
                         f"Mindestgröße {m['min_size']:g}")
    t0 = int(_t.time()) - 5
    try:
        oid = step(f"Gebot setzen: KAUF {m['size']:g} @ {m['price']:.2f} (Mitte {m['mid']:.3f}, bestes Gebot {m['bid']:.2f})",
                   lambda: ex.place(m["token"], "BUY", m["price"], m["size"]))
        if not oid:
            return steps
        scored = False
        for i in range(checks):
            pause(every_s)
            s = ex.scoring([oid]).get(oid)
            scored = scored or bool(s)
            if scored:
                break
        steps.append(dict(step="Zählt für Rewards (Polymarkets Prüfung)", ok=scored,
                          info=f"ja (nach {(i + 1) * every_s:.0f} s)" if scored else f"nein nach {checks * every_s:.0f} s"))
    finally:
        if oid:
            step("Order stornieren", lambda: ex.cancel(oid) or "ok")
            pause(2)
            step("Order weg", lambda: oid not in ex.open_orders() or (_ for _ in ()).throw(RuntimeError("noch offen!")))
            try:  # fully filled orders are no longer open: look at our own trades instead
                filled = sum(t["size"] for t in ex.trades(t0) if t["token"] == m["token"] and t["side"] == "BUY")
            except Exception:  # noqa
                filled = 0
            if filled:
                steps.append(dict(step="Teilweise ausgeführt", ok=True,
                                  info=f"{filled:g} Anteile gekauft – liegen jetzt im Konto (später verkaufen)"))
    return steps


def reward_test_passed(steps: List[dict]) -> bool:
    return any(x.get("step", "").startswith("Zählt für Rewards") and x.get("ok") for x in steps) \
        and any(x.get("step") == "Order weg" and x.get("ok") for x in steps)
