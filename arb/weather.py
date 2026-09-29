"""Temperature markets: parse Polymarket weather events and price their buckets from ensemble forecasts.

Polymarket lists events like "Highest temperature in Toronto on September 29?" with one market per
bucket ("72-73°F", "80°F or higher", "21°C", ...). The event resolves on the whole-degree value a
weather station reports, so a bucket [lo, hi] covers the continuous range [lo - 0.5, hi + 0.5).

Model: every member of the Open-Meteo ensemble forecasts (GFS, ECMWF, ICON, ...) is one scenario for
the day's max/min. Each member is smeared with a normal error `sigma` (grid point vs. station,
model bias) and the bucket probability is the average over all members. Free API, no key.
"""
from __future__ import annotations

import math
import re
import time
from datetime import date
from typing import Callable, Dict, List, Optional, Tuple

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], start=1)}
TITLE_RE = re.compile(r"^(highest|lowest) temperature in (.+?) on ([a-z]+) (\d{1,2})(?:,? (\d{4}))?\s*\??$", re.I)
RANGE_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*°?\s*[fc]?\s*(?:-|to)\s*(-?\d+(?:\.\d+)?)\s*°?\s*([fc])\b")
SINGLE_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*°?\s*([fc])\b")


def parse_title(title: str, today: date) -> Optional[Tuple[str, str, date]]:
    """'Highest temperature in Toronto on September 29?' -> ('max', 'Toronto', date(2026, 9, 29))."""
    m = TITLE_RE.match(title.strip())
    if not m or m.group(3).lower() not in MONTHS:
        return None
    month, day = MONTHS[m.group(3).lower()], int(m.group(4))
    year = int(m.group(5)) if m.group(5) else today.year
    try:
        d = date(year, month, day)
        if not m.group(5) and (d - today).days < -180:  # "January 2" seen in late December
            d = date(year + 1, month, day)
    except ValueError:
        return None
    return ("max" if m.group(1).lower() == "highest" else "min"), m.group(2).strip(), d


def parse_bucket(label: str) -> Optional[Tuple[float, float, str]]:
    """'72-73°F' -> (72, 73, 'f'); '80°F or higher' -> (80, inf, 'f'); '≤16°C' -> (-inf, 16, 'c')."""
    s = label.strip().lower().replace("–", "-").replace("—", "-").replace("º", "°")
    m = RANGE_RE.search(s)
    if m:
        lo, hi = sorted((float(m.group(1)), float(m.group(2))))
        return lo, hi, m.group(3)
    m = SINGLE_RE.search(s)
    if not m:
        return None
    v, unit = float(m.group(1)), m.group(2)
    if any(w in s for w in ("higher", "above", "more", "≥", ">=")) or s.endswith("+"):
        return v, math.inf, unit
    if any(w in s for w in ("lower", "below", "less", "≤", "<=")):
        return -math.inf, v, unit
    if ">" in s:
        return v + 1, math.inf, unit
    if "<" in s:
        return -math.inf, v - 1, unit
    return v, v, unit


def _cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bucket_prob(members: List[float], lo: float, hi: float, sigma: float) -> float:
    """Probability that the station reports a whole-degree value in [lo, hi]."""
    if not members:
        return 0.0
    a, b = lo - 0.5, hi + 0.5
    total = 0.0
    for v in members:
        upper = 1.0 if b == math.inf else _cdf((b - v) / sigma)
        lower = 0.0 if a == -math.inf else _cdf((a - v) / sigma)
        total += upper - lower
    return total / len(members)


class WeatherModel:
    """Ensemble members for a city/day from Open-Meteo, cached so one event costs one request."""

    def __init__(self, get_json: Callable[[str, dict], dict], models: str,
                 coords: Optional[Dict[str, List[float]]] = None, ttl_s: float = 1800):
        self.get_json = get_json
        self.models = models
        self.coords = {k.lower(): tuple(v) for k, v in (coords or {}).items()}
        self.ttl = ttl_s
        self._geo: Dict[str, Optional[Tuple[float, float]]] = {}
        self._fc: Dict[tuple, Tuple[float, dict]] = {}

    def locate(self, city: str) -> Optional[Tuple[float, float]]:
        key = city.lower()
        if key in self.coords:
            return self.coords[key]
        if key not in self._geo:
            res = (self.get_json(GEOCODE_URL, {"name": city, "count": 1, "language": "en"}) or {}).get("results")
            self._geo[key] = (float(res[0]["latitude"]), float(res[0]["longitude"])) if res else None
        return self._geo[key]

    def members(self, city: str, day: date, kind: str, unit: str) -> List[float]:
        loc = self.locate(city)
        if not loc:
            return []
        key = (loc, unit)
        hit = self._fc.get(key)
        if not hit or time.time() - hit[0] > self.ttl:
            data = self.get_json(ENSEMBLE_URL, {
                "latitude": loc[0], "longitude": loc[1], "daily": "temperature_2m_max,temperature_2m_min",
                "models": self.models, "timezone": "auto", "forecast_days": 4,
                "temperature_unit": "fahrenheit" if unit == "f" else "celsius"})
            hit = (time.time(), (data or {}).get("daily") or {})
            self._fc[key] = hit
        daily = hit[1]
        days = daily.get("time") or []
        if day.isoformat() not in days:
            return []
        i = days.index(day.isoformat())
        # every ensemble member arrives as its own column: temperature_2m_max_member07_<model>
        prefix = f"temperature_2m_{kind}"
        return [float(col[i]) for k, col in daily.items()
                if k.startswith(prefix) and isinstance(col, list) and i < len(col) and col[i] is not None]
