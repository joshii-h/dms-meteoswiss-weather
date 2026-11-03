#!/usr/bin/env python3
"""MeteoSwiss forecast for Dux Shell, printed as an Open-Meteo response.

Dux Shell sends {"latitude": 46.95, "longitude": 7.45} on stdin. For a test
by hand the coordinates can also be given as arguments:
weather.py <latitude> <longitude>

The forecast comes from the endpoint behind the MeteoSwiss app, keyed by a
six-digit postcode that swisstopo resolves from the coordinates. That endpoint
is not an official API and may change. Outside Switzerland there is no
postcode, so the script fails and Dux Shell moves on to the next provider.
"""

import json
import math
import os
import sys
import time
import urllib.parse
import tempfile
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

USER_AGENT = "dms-meteoswiss-weather (+https://github.com/joshii-h/dms-meteoswiss-weather)"
SWISSTOPO = "https://api3.geo.admin.ch/rest/services/api/MapServer/identify"
FORECAST = "https://app-prod-ws.meteoswiss-app.ch/v1/plzDetail?plz={}"
HOUR_MS = 3600 * 1000
# Dux Shell stops a plugin after 15 s; both requests share this budget.
DEADLINE = time.monotonic() + 12

try:
    ZONE_NAME, ZONE = "Europe/Zurich", ZoneInfo("Europe/Zurich")
except ZoneInfoNotFoundError:
    ZONE_NAME, ZONE = "", None

# MeteoSwiss pictograms 1-42 (night variants add 100) to WMO weather codes,
# after the official legend "Wettersymbole: Texte/Übersetzungen".
WMO = [None, 0, 1, 2, 3, 3, 80, 80, 85, 80, 80, 85, 95, 95, 61, 66, 71, 63, 66, 73, 65,
       67, 75, 95, 95, 95, 1, 3, 45, 80, 85, 80, 80, 81, 86, 3, 95, 95, 95, 95, 95, 95, 95]


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def wmo(icon):
    if not is_int(icon):
        return None
    index = icon % 100
    return WMO[index] if 0 < index < len(WMO) else None


def get_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=max(1, DEADLINE - time.monotonic())) as response:
        return json.load(response)


def local_iso(ms):
    return datetime.fromtimestamp(ms / 1000, ZONE).strftime("%Y-%m-%dT%H:%M")


def cache_path():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "dms-meteoswiss-weather", "postcodes.json")


def read_cache(path):
    try:
        with open(path, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(cache, dict):
        return {}
    return {key: code for key, code in cache.items() if code is None or is_int(code)}


def write_cache(path, cache):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(tmp, path)
    except OSError:
        os.unlink(tmp)


def postcode(lat, lon):
    # Rounded to about 100 m, which is all swisstopo gets to see.
    lat, lon = round(lat, 3), round(lon, 3)
    key = f"{lat:.3f},{lon:.3f}"
    path = cache_path()
    cache = read_cache(path)
    if key in cache:
        if cache[key] is None:
            raise SystemExit("MeteoSwiss covers Switzerland only")
        return cache[key]

    query = urllib.parse.urlencode({
        "geometryType": "esriGeometryPoint",
        "geometry": f"{lon},{lat}",
        "sr": 4326,
        "layers": "all:ch.swisstopo-vd.ortschaftenverzeichnis_plz",
        "tolerance": 0,
        "returnGeometry": "false",
    })
    results = get_json(f"{SWISSTOPO}?{query}").get("results") or []
    attrs = results[0].get("attributes", {}) if results else {}
    # Remember misses too, so a location abroad is not looked up on every refresh.
    code = attrs["plz"] * 100 + int(attrs.get("zusziff") or 0) if is_int(attrs.get("plz")) else None

    cache[key] = code
    write_cache(path, cache)
    if code is None:
        raise SystemExit("MeteoSwiss covers Switzerland only")
    return code


def finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def day_or_night(icon):
    return (0 if icon > 100 else 1) if is_int(icon) else None


def to_open_meteo(data, lat, lon):
    graph = data.get("graph") or {}
    temps = graph.get("temperatureMean1h")
    current = data.get("currentWeather")
    start = graph.get("start")
    if not current or not isinstance(temps, list) or not isinstance(start, (int, float)):
        raise SystemExit("MeteoSwiss returned no forecast")

    def three_hourly(key, i):
        values = graph.get(key) or []
        return values[i // 3] if 0 <= i // 3 < len(values) else None

    def hourly_value(key, i):
        values = graph.get(key) or []
        return finite(values[i]) if 0 <= i < len(values) else None

    # graph.start is local midnight; 1h series are hourly, 3h series cover three hours each.
    hours = range(len(temps))
    hourly = {
        "time": [local_iso(start + i * HOUR_MS) for i in hours],
        "temperature_2m": [finite(t) for t in temps],
        "wind_speed_10m": [hourly_value("windSpeed1h", i) for i in hours],
        "weather_code": [wmo(three_hourly("weatherIcon3h", i)) for i in hours],
        "precipitation_probability": [finite(three_hourly("precipitationProbability3h", i)) for i in hours],
        # Night pictograms are numbered from 101.
        "is_day": [day_or_night(three_hourly("weatherIcon3h", i)) for i in hours],
    }

    days = data.get("forecast") or []
    sunrise = graph.get("sunrise") or []
    sunset = graph.get("sunset") or []
    probabilities = graph.get("precipitationProbability3h") or []

    def day_probability(i):
        values = [p for p in probabilities[i * 8:i * 8 + 8] if finite(p) is not None]
        return max(values) if values else None

    daily = {
        "time": [day.get("dayDate") for day in days],
        "sunrise": [local_iso(sunrise[i]) if i < len(sunrise) else "" for i in range(len(days))],
        "sunset": [local_iso(sunset[i]) if i < len(sunset) else "" for i in range(len(days))],
        "temperature_2m_max": [finite(day.get("temperatureMax")) for day in days],
        "temperature_2m_min": [finite(day.get("temperatureMin")) for day in days],
        "weather_code": [wmo(day.get("iconDay")) for day in days],
        "precipitation_probability_max": [day_probability(i) for i in range(len(days))],
    }

    now_ms = current.get("time") if isinstance(current.get("time"), (int, float)) else time.time() * 1000
    hour = int((now_ms - start) // HOUR_MS)
    icon = current.get("icon")
    offset = datetime.now(ZONE).astimezone(ZONE).utcoffset()
    forecast = {
        "latitude": lat,
        "longitude": lon,
        "utc_offset_seconds": int(offset.total_seconds()),
        "current": {
            "time": local_iso(now_ms),
            "temperature_2m": finite(current.get("temperature")),
            "wind_speed_10m": hourly_value("windSpeed1h", hour),
            "wind_direction_10m": finite(three_hourly("windDirection3h", hour)),
            "weather_code": wmo(icon),
            "is_day": day_or_night(icon),
        },
        "hourly": hourly,
        "daily": daily,
    }
    if ZONE_NAME:
        forecast["timezone"] = ZONE_NAME
    return forecast


def main():
    if len(sys.argv) == 3:
        lat, lon = float(sys.argv[1]), float(sys.argv[2])
    elif len(sys.argv) == 1:
        try:
            query = json.load(sys.stdin)
            lat, lon = float(query["latitude"]), float(query["longitude"])
        except (ValueError, KeyError, TypeError):
            raise SystemExit('expected {"latitude": ..., "longitude": ...} on stdin') from None
    else:
        raise SystemExit(__doc__)
    data = get_json(FORECAST.format(postcode(lat, lon)))
    json.dump(to_open_meteo(data, lat, lon), sys.stdout, allow_nan=False)


if __name__ == "__main__":
    main()
