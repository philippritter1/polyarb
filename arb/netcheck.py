"""Startup network diagnostics – logs which request variants reach Polymarket."""
from __future__ import annotations

import logging
import time
import urllib.request

import requests

log = logging.getLogger("polyarb.netcheck")

TESTS = [
    ("clob /time", "https://clob.polymarket.com/time"),
    ("gamma limit=1", "https://gamma-api.polymarket.com/markets?limit=1"),
    ("gamma limit=100", "https://gamma-api.polymarket.com/markets?limit=100&active=true&closed=false"),
]


def run() -> None:
    for name, url in TESTS:
        for variant in ("requests-botUA", "requests-defaultUA", "urllib"):
            t = time.time()
            try:
                if variant == "urllib":
                    with urllib.request.urlopen(url, timeout=15) as r:
                        n = len(r.read())
                        code = r.status
                else:
                    h = {"User-Agent": "polyarb-paper/0.1"} if variant == "requests-botUA" else {}
                    r = requests.get(url, headers=h, timeout=15)
                    n, code = len(r.content), r.status_code
                log.info("NETCHECK %-16s %-19s OK %s %d bytes %.1fs", name, variant, code, n, time.time() - t)
            except Exception as e:  # noqa
                log.info("NETCHECK %-16s %-19s FAIL %s: %s (%.1fs)", name, variant, type(e).__name__,
                         str(e)[:120], time.time() - t)
            time.sleep(1)
