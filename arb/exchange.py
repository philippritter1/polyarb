"""Real Polymarket access for the order gateway (arb/orders.py) – only used in live mode.

Wraps the official client (py-clob-client, imported lazily: the paper bot and the tests do not need it):
sign and post GTC limit orders as post-only (a maker order that would cross the book is rejected instead of
taking), cancel, list open orders and own trades, read the USDC balance, ask whether orders score for
liquidity rewards, and read the rewards actually earned. Plus the public checks the gateway needs before it
sends anything: the geoblock answer for the server's IP and the account value from the data API.

Credentials come from data/secrets.env (entered on /admin/, mode 600) or the environment:
  POLY_PRIVATE_KEY   key of the wallet that signs (a wallet used only for the bot, holding only the pilot money)
  POLY_FUNDER        address that holds the funds (the Polymarket proxy wallet shown on polymarket.com)
  POLY_SIG_TYPE      0 = plain wallet (EOA), 1 = email/Magic account, 2 = browser-wallet proxy
The API key/secret/passphrase are derived from the private key on first use; nothing secret is logged.
"""
from __future__ import annotations

import logging
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
    if w.get("sig_type") in (1, 2) and not ADDR_RE.match(w.get("funder") or ""):
        return "Funder-Adresse fehlt (Proxy-Wallet von polymarket.com)"
    if w.get("sig_type") not in (0, 1, 2):
        return "Signaturtyp muss 0, 1 oder 2 sein"
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
TOKENS = {"USDC.e": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", "USDC": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"}


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

    @property
    def c(self):
        if self._c is None:
            from py_clob_client.client import ClobClient
            w = self.wallet
            key = w["key"] if w["key"].startswith("0x") else "0x" + w["key"]
            c = ClobClient(self.host, chain_id=CHAIN_ID, key=key, signature_type=w["sig_type"],
                           funder=w.get("funder") or None)
            c.set_api_creds(c.create_or_derive_api_creds())
            self._c = c
        return self._c

    @property
    def user(self) -> str:
        """The address that holds funds and positions (proxy wallet, or the signer for sig type 0)."""
        return self.wallet.get("funder") or address_of(self.wallet["key"])

    def best(self, token: str) -> dict:
        """{bid, ask, tick, min_size} of a token's book."""
        b = self.c.get_order_book(str(token))
        bids = sorted((float(x.price) for x in b.bids or []), reverse=True)
        asks = sorted(float(x.price) for x in b.asks or [])
        return dict(bid=bids[0] if bids else None, ask=asks[0] if asks else None, tick=float(b.tick_size or 0.01),
                    min_size=float(b.min_order_size or 5))

    # ------------------------------------------------------------------ orders
    def place(self, token: str, side: str, price: float, size: float) -> str:
        """Post-only GTC limit order; returns the order id or raises with the exchange's reason."""
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL
        order = self.c.create_order(OrderArgs(token_id=str(token), price=float(price), size=float(size),
                                              side=BUY if side == "BUY" else SELL))
        resp = self.c.post_order(order, OrderType.GTC, post_only=True) or {}
        oid = resp.get("orderID") or resp.get("orderId") or ""
        if not resp.get("success", bool(oid)) or not oid:
            raise RuntimeError(resp.get("errorMsg") or resp.get("error") or "Order abgelehnt")
        return oid

    def cancel(self, order_id: str) -> None:
        self.c.cancel(order_id)

    def cancel_all(self) -> None:
        self.c.cancel_all()

    def open_orders(self) -> Dict[str, dict]:
        """order id -> {token, side, price, size, matched}."""
        from py_clob_client.clob_types import OpenOrderParams
        out = {}
        for o in self.c.get_orders(OpenOrderParams()) or []:
            out[o.get("id")] = dict(token=str(o.get("asset_id")), side=str(o.get("side", "")).upper(),
                                    price=float(o.get("price") or 0), size=float(o.get("original_size") or 0),
                                    matched=float(o.get("size_matched") or 0))
        return out

    def trades(self, after_ts: int) -> List[dict]:
        """Own fills since `after_ts` (unix s): {id, token, side, price, size, ts, role}."""
        from py_clob_client.clob_types import TradeParams
        out = []
        for t in self.c.get_trades(TradeParams(after=int(after_ts))) or []:
            out.append(dict(id=t.get("id"), token=str(t.get("asset_id")), side=str(t.get("side", "")).upper(),
                            price=float(t.get("price") or 0), size=float(t.get("size") or 0),
                            ts=float(t.get("match_time") or 0), role=t.get("trader_side", "")))
        return out

    def balance_usd(self, sig_type: Optional[int] = None) -> Optional[float]:
        """USDC available for orders; `sig_type` asks for another signature type than the configured one."""
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
        sig = -1 if sig_type is None else int(sig_type)
        try:  # the exchange caches the balance: refresh it from the chain first (a fresh deposit showed 0)
            self.c.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=sig))
        except Exception as e:  # noqa
            log.warning("balance refresh: %s", type(e).__name__)
        d = self.c.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=sig)) or {}
        try:
            return float(d.get("balance", 0)) / 1e6  # USDC has 6 decimals
        except (TypeError, ValueError):
            return None

    def scoring(self, order_ids: List[str]) -> Dict[str, bool]:
        """Does each order count for liquidity rewards right now (Polymarket's own check)?"""
        if not order_ids:
            return {}
        from py_clob_client.clob_types import OrdersScoringParams
        d = self.c.are_orders_scoring(OrdersScoringParams(orderIds=list(order_ids))) or {}
        return {k: bool(v) for k, v in d.items()}

    def rewards(self, day: Optional[str] = None) -> Optional[float]:
        """Liquidity rewards earned on `day` (YYYY-MM-DD, UTC; default yesterday) in USDC; None if the endpoint
        does not answer (not in py-clob-client – checked against the real account in the pilot)."""
        from py_clob_client.clob_types import RequestArgs
        from py_clob_client.headers.headers import create_level_2_headers
        from py_clob_client.http_helpers.helpers import get
        day = day or datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() - 86400, timezone.utc).strftime("%Y-%m-%d")
        path = "/rewards/user/total"
        try:
            headers = create_level_2_headers(self.c.signer, self.c.creds, RequestArgs(method="GET", request_path=path))
            d = get(f"{self.host}{path}?date={day}&signature_type={self.wallet['sig_type']}", headers=headers)
            rows = d if isinstance(d, list) else (d.get("data") or [d])
            return round(sum(float(r.get("earnings", 0) or 0) for r in rows if isinstance(r, dict)), 6)
        except Exception as e:  # noqa
            log.warning("rewards %s: %s", day, type(e).__name__)
            return None


def liquid_token(get: Callable = None) -> Optional[dict]:
    """A busy, two-sided market for the connection test: {token, question}."""
    import json as _json
    r = (get or requests.get)("https://gamma-api.polymarket.com/markets", params={
        "active": "true", "closed": "false", "order": "volume24hr", "ascending": "false", "limit": 30}, timeout=20)
    for m in (r.json() if hasattr(r, "json") else r) or []:
        try:
            toks, prices = _json.loads(m.get("clobTokenIds") or "[]"), _json.loads(m.get("outcomePrices") or "[]")
            if m.get("enableOrderBook") and len(toks) == 2 and 0.2 <= float(prices[0]) <= 0.8:
                return dict(token=str(toks[0]), question=m.get("question", ""))
        except (ValueError, TypeError, IndexError):
            continue
    return None


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
    bal = step("Guthaben (USDC)", ex.balance_usd)
    m = step("Markt für den Test", lambda: (find or liquid_token)() or (_ for _ in ()).throw(RuntimeError("keiner gefunden")))
    if not m:
        return steps
    bk = step("Orderbuch", lambda: ex.best(m["token"]))
    if not bk or bk["bid"] is None:
        return steps
    price = round(max(bk["tick"], bk["bid"] - 3 * bk["tick"]), 4)
    size = max(bk["min_size"], 5.0)
    if bal is not None and price * size > bal:
        hint = ""
        if bal == 0:  # usually the wrong signature type: look where the money is
            found = {}
            for t in (0, 1, 2):
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
