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
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

USER_AGENT = "dms-meteoswiss-weather (+https://github.com/joshii-h/dms-meteoswiss-weather)"
SWISSTOPO = "https://api3.geo.admin.ch/rest/services/api/MapServer/identify"
FORECAST = "https://app-prod-ws.meteoswiss-app.ch/v1/plzDetail?plz={}"
HOUR_MS = 3600 * 1000

# MeteoSwiss pictograms 1-42 (night variants add 100) to WMO weather codes,
# after the official legend "Wettersymbole: Texte/Übersetzungen".
WMO = [None, 0, 1, 2, 2, 3, 80, 80, 85, 80, 80, 85, 95, 95, 61, 66, 71, 63, 66, 73, 65,
       67, 75, 95, 95, 95, 1, 3, 45, 80, 85, 80, 80, 81, 86, 3, 95, 95, 95, 95, 95, 95, 95]


def wmo(icon):
    if not isinstance(icon, int):
        return None
    index = icon % 100
    return WMO[index] if 0 < index < len(WMO) else None


def get_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    # Two requests must fit in the 30 s Dux Shell gives a plugin.
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def local_iso(ms):
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%dT%H:%M")


def cache_path():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "dms-meteoswiss-weather", "postcodes.json")


def postcode(lat, lon):
    key = f"{lat:.3f},{lon:.3f}"
    path = cache_path()
    try:
        with open(path, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
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
    code = attrs["plz"] * 100 + int(attrs.get("zusziff") or 0) if isinstance(attrs.get("plz"), int) else None

    cache[key] = code
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f)
    if code is None:
        raise SystemExit("MeteoSwiss covers Switzerland only")
    return code


def finite(value):
    return value if isinstance(value, (int, float)) else None


def to_open_meteo(data):
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
    }

    days = data.get("forecast") or []
    sunrise = graph.get("sunrise") or []
    sunset = graph.get("sunset") or []
    probabilities = graph.get("precipitationProbability3h") or []

    def day_probability(i):
        values = [p for p in probabilities[i * 8:i * 8 + 8] if finite(p) is not None]
        return max(values) if len(values) == 8 else None

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
    return {
        "utc_offset_seconds": int(datetime.now().astimezone().utcoffset().total_seconds()),
        "current": {
            "time": local_iso(now_ms),
            "temperature_2m": finite(current.get("temperature")),
            "wind_speed_10m": hourly_value("windSpeed1h", hour),
            "wind_direction_10m": finite(three_hourly("windDirection3h", hour)),
            "weather_code": wmo(icon),
            "is_day": 1 if isinstance(icon, int) and icon < 100 else 0,
        },
        "hourly": hourly,
        "daily": daily,
    }


def main():
    if len(sys.argv) == 3:
        lat, lon = float(sys.argv[1]), float(sys.argv[2])
    elif len(sys.argv) == 1:
        query = json.load(sys.stdin)
        lat, lon = float(query["latitude"]), float(query["longitude"])
    else:
        raise SystemExit(__doc__)
    data = get_json(FORECAST.format(postcode(lat, lon)))
    json.dump(to_open_meteo(data), sys.stdout)


if __name__ == "__main__":
    main()
