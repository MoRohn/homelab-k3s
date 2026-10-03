"""Exact weather from Open-Meteo (no key): current conditions and the daily forecast for a named place.

`lookup(text, now, target)` finds the place in the question ("weather in Boise, ID tomorrow"),
geocodes it, and returns the current conditions plus the day(s) asked about. Only the place name and
coordinates leave the box. Returns None when no place is named (web search answers instead).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import httpx

from lif.common import config, log

LOG = log.get("lif.web.weather")
GEO = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST = "https://api.open-meteo.com/v1/forecast"

_STATES = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
           "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
           "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
           "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
           "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
           "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
           "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
           "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
           "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
           "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia"}
_WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
        51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
        61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
        71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers",
        82: "heavy showers", 85: "snow showers", 86: "heavy snow showers", 95: "thunderstorms",
        96: "thunderstorms with hail", 99: "thunderstorms with heavy hail"}
_PLACE = re.compile(r"\b(?:in|for|at|near|around|over)\s+((?:[A-Z][\w'.-]*)(?:(?:\s+|,\s*)(?:[A-Z][\w'.-]*)){0,3})")
_PLACE_BEFORE = re.compile(r"\b((?:[A-Z][\w'.-]*\s+){0,2}[A-Z][\w'.-]*)(?:,\s*([A-Z]{2}))?(?:'s)?\s+(?:weather|forecast)\b")
_NOT_PLACE = {"What", "Whats", "What's", "The", "Today", "Tomorrow", "Tonight", "Is", "Will", "How", "Weather",
              "Forecast", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "This",
              "Next", "January", "February", "March", "April", "May", "June", "July", "August", "September",
              "October", "November", "December", "Celsius", "Fahrenheit", "I", "Me"}


@dataclass
class WeatherResult:
    lines: list[str]
    sources: list[dict]                 # parallel to lines
    place: str


def place_of(text: str) -> tuple[str, str | None] | None:
    """(name, region hint) from the question, e.g. ("Boise", "Idaho"). None when no place is named."""
    for rx in (_PLACE, _PLACE_BEFORE):
        m = rx.search(text)
        if not m:
            continue
        raw = m.group(1).strip(" ,.?")
        parts = [p.strip() for p in re.split(r",", raw) if p.strip()]
        words = [w for w in parts[0].split() if w not in _NOT_PLACE]
        if not words:
            continue
        name = " ".join(words)
        region = parts[1] if len(parts) > 1 else (m.group(2) if rx is _PLACE_BEFORE and m.lastindex >= 2 else None)
        if region is None and len(words) > 1 and words[-1].upper() in _STATES and len(words[-1]) == 2:
            name, region = " ".join(words[:-1]), words[-1]
        if region:
            region = _STATES.get(region.upper(), region)
        return name, region
    return None


class WeatherFeed:
    def __init__(self, client: httpx.AsyncClient | None = None, timeout: float = 2.0):
        self.client = client or httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "lif-web/1"})
        self.timeout = timeout
        self._geo: dict[str, tuple[float, dict | None]] = {}

    async def _geocode(self, name: str, region: str | None) -> dict | None:
        key = f"{name}|{region}".lower()
        hit = self._geo.get(key)
        if hit and time.time() - hit[0] < 7 * 86400:
            return hit[1]
        r = await self.client.get(GEO, params={"name": name, "count": 10, "language": "en", "format": "json"},
                                  timeout=self.timeout)
        r.raise_for_status()
        results = r.json().get("results") or []
        if region:
            rl = region.lower()
            results = [x for x in results if rl in (str(x.get("admin1", "")).lower(), str(x.get("country", "")).lower(),
                                                    str(x.get("country_code", "")).lower())] or results
        best = max(results, key=lambda x: x.get("population") or 0) if results else None
        self._geo[key] = (time.time(), best)
        return best

    async def lookup(self, text: str, now: datetime, target: date | None = None,
                     span: tuple[date, date] | None = None) -> WeatherResult | None:
        p = place_of(text) or _default_place()
        if p is None:
            return None
        loc = await self._geocode(*p)
        if not loc:
            return None
        us = loc.get("country_code") == "US"
        params = {"latitude": loc["latitude"], "longitude": loc["longitude"], "timezone": "auto", "forecast_days": 8,
                  "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m",
                  "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                  "temperature_unit": "fahrenheit" if us else "celsius", "wind_speed_unit": "mph" if us else "kmh"}
        r = await self.client.get(FORECAST, params=params, timeout=self.timeout)
        r.raise_for_status()
        d = r.json()
        unit, wind = ("°F", "mph") if us else ("°C", "km/h")
        place = ", ".join(x for x in (loc.get("name"), loc.get("admin1"), loc.get("country_code")) if x)
        lines = []
        cur = d.get("current") or {}
        if cur and (target is None or target == now.date()):
            lines.append(f"CURRENT ({place}, {cur.get('time', '').replace('T', ' ')} local): "
                         f"{cur.get('temperature_2m')}{unit} (feels like {cur.get('apparent_temperature')}{unit}), "
                         f"{_WMO.get(cur.get('weather_code'), 'conditions unknown')}, wind {cur.get('wind_speed_10m')} {wind}, "
                         f"humidity {cur.get('relative_humidity_2m')}%.")
        daily = d.get("daily") or {}
        days = daily.get("time") or []
        want = set()
        if span:
            want = {span[0] + timedelta(days=i) for i in range((span[1] - span[0]).days + 1)}
        elif target:
            want = {target}
        else:
            want = {now.date() + timedelta(days=i) for i in range(3)}
        for i, ds in enumerate(days):
            day = date.fromisoformat(ds)
            if day not in want:
                continue
            pp = (daily.get("precipitation_probability_max") or [None] * len(days))[i]
            lines.append(f"FORECAST {day:%a} {day:%B} {day.day}, {day.year} ({place}): high "
                         f"{daily['temperature_2m_max'][i]}{unit}, low {daily['temperature_2m_min'][i]}{unit}, "
                         f"{_WMO.get(daily['weather_code'][i], 'conditions unknown')}"
                         + (f", {pp}% chance of precipitation." if pp is not None else "."))
        if not lines:
            return None
        url = f"https://open-meteo.com/en/docs#latitude={loc['latitude']}&longitude={loc['longitude']}"
        src = {"title": f"Open-Meteo forecast: {place}", "url": url}
        return WeatherResult(lines, [src] * len(lines), place)


def _default_place() -> tuple[str, str | None] | None:
    """web.default_location (e.g. "Boise, Idaho") for "what's the weather?" with no place named."""
    v = config.get("web.default_location")
    if not v:
        return None
    parts = [p.strip() for p in str(v).split(",")]
    return parts[0], (parts[1] if len(parts) > 1 else None)
