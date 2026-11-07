"""Air quality from the nearest station of NABEL, the Swiss national air
pollution monitoring network run by BAFU and Empa.

The data comes from the backend of BAFU's public query form, run by
Meteotest. It is not a documented API and may change. Every failure here
only leaves the air quality readings empty; the forecast never depends on it.
"""

import contextlib
import json
import math
import os
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from airquality import FIELDS, european_aqi, finite, mean_24h, round_half_up, us_aqi

APP = "dms-meteoswiss-weather"
URL = "https://bafu.meteotest.ch/nabel/index.php/ausgabe/index/german?webgrab=no"
# Ids of the query form: O3, NO2, SO2, PM10, PM2.5.
POLLUTANT_IDS = (1, 2, 3, 6, 7)
# Column names in the CSV header, without the unit.
COLUMNS = {"O3": "o3", "NO2": "no2", "SO2": "so2", "PM10": "pm10", "PM2.5": "pm25"}
UNITS = ("ug/m3", "µg/m3", "µg/m³")

# The NABEL stations: names and coordinates from the geo.admin layer
# ch.bafu.nabelstationen, ids from the station list of the query form.
# (id, name, latitude, longitude)
STATIONS = (
    (1, "Bern-Bollwerk", 46.950993, 7.440866),
    (2, "Lausanne-César-Roux", 46.522018, 6.639701),
    (3, "Lugano-Università", 46.011117, 8.957165),
    (4, "Zürich-Kaserne", 47.377586, 8.530406),
    (5, "Basel-Binningen", 47.541081, 7.583264),
    (6, "Dübendorf-Empa", 47.404842, 8.608474),
    (7, "Härkingen-A1", 47.311911, 7.8205),
    (8, "Sion-Aéroport-A9", 46.220201, 7.341966),
    (9, "Magadino-Cadenazzo", 46.160376, 8.933939),
    (10, "Payerne", 46.813057, 6.944473),
    (11, "Tänikon", 47.479771, 8.904686),
    (12, "Beromünster", 47.189614, 8.175434),
    (13, "Chaumont", 47.049465, 6.979204),
    (14, "Rigi-Seebodenalp", 47.06741, 8.46333),
    (15, "Davos-Seehornwald", 46.815199, 9.855859),
    (16, "Jungfraujoch", 46.547499, 7.985071),
)
# Jungfraujoch, at 3580 m, measures the background air of the Alps and says
# nothing about the air where anyone lives.
EXCLUDED = {16}
RADIUS_KM = 25

# NABEL times are the end of the hourly interval in CET, all year.
CET = timezone(timedelta(hours=1))
# A pollutant whose last value is older than this is left out.
STALE_AFTER = timedelta(hours=3)
# Dux Shell asks about every 15 minutes; NABEL is asked at most once in this
# time per station, the rest is answered from the cache.
RESULT_TTL = 30 * 60
# No request is started with less time than this left before the deadline.
MIN_TIME = 3

NO_DATA = dict.fromkeys(FIELDS)


def distance_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 6371 * 2 * math.asin(min(1, math.sqrt(a)))


def nearest_station(lat, lon):
    """(id, name, distance in km) of the nearest station within RADIUS_KM,
    None without one."""
    found = [(distance_km(lat, lon, slat, slon), sid, name)
             for sid, name, slat, slon in STATIONS if sid not in EXCLUDED]
    distance, sid, name = min(found)
    return (sid, name, round(distance, 1)) if distance <= RADIUS_KM else None


def parse_time(text):
    """End of the hourly interval as an aware datetime, None if unreadable.
    The last hour of a day is written as 24:00 or as 00:00 of the next."""
    try:
        day, clock = text.strip().split(" ")
        hour, minute = clock.split(":")
        base = datetime.strptime(day, "%d.%m.%Y").replace(tzinfo=CET)
        hour, minute = int(hour), int(minute)
    except ValueError:
        return None
    if not (0 <= hour <= 24 and 0 <= minute < 60) or (hour == 24 and minute):
        return None
    return base + timedelta(hours=hour, minutes=minute)


def parse_csv(text):
    """Hourly values by pollutant, each a list of (end of hour, value), oldest
    first; empty fields are left out. Columns are found by their header name,
    since a station only lists the pollutants it measures. ValueError when
    there is no data table."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("Datum/Zeit"):
            break
    else:
        raise ValueError("no data table in the NABEL answer")
    columns = {}
    for index, name in enumerate(lines[i].split(";")):
        label, _, unit = name.strip().partition(" [")
        if label in COLUMNS and unit.rstrip("]").strip() in UNITS:
            columns[index] = COLUMNS[label]
    series = {pollutant: [] for pollutant in columns.values()}
    for line in lines[i + 1:]:
        fields = line.split(";")
        end = parse_time(fields[0])
        if end is None:
            continue
        for index, pollutant in columns.items():
            if index >= len(fields) or not fields[index].strip():
                continue
            try:
                value = float(fields[index].strip().replace(",", "."))
            except ValueError:
                continue
            if math.isfinite(value) and value >= 0:
                series[pollutant].append((end, value))
    return series


def latest(hours, now):
    """(end, value) of the last hour up to now, None if older than STALE_AFTER."""
    # A little leeway for clocks that run behind.
    past = [(end, value) for end, value in hours if end <= now + timedelta(minutes=10)]
    if not past:
        return None
    end, value = max(past, key=lambda item: item[0])
    return (end, value) if end >= now - STALE_AFTER else None


def readings(series, now):
    """The air quality fields from the parsed series, None without a fresh
    PM2.5 value."""
    fresh = {p: latest(hours, now) for p, hours in series.items()}
    fresh = {p: item for p, item in fresh.items() if item}
    if "pm25" not in fresh:
        return None

    def mean(pollutant):
        # The 24 hours that end with the pollutant's latest hour.
        return mean_24h(series[pollutant], fresh[pollutant][0]) if pollutant in fresh else None

    pm25 = fresh["pm25"][1]
    pm10 = fresh["pm10"][1] if "pm10" in fresh else None
    pm25_mean, pm10_mean = mean("pm25"), mean("pm10")
    # The EPA's index is for 24-hour means; without one the latest value stands in.
    us = us_aqi(pm25_mean if pm25_mean is not None else pm25,
                pm10_mean if pm10_mean is not None else pm10)
    return {
        "european_aqi": round_half_up(european_aqi({p: item[1] for p, item in fresh.items()})),
        "us_aqi": round_half_up(us),
        "pm2_5": round(pm25, 1),
        "pm10": None if pm10 is None else round(pm10, 1),
        "pm2_5_mean_24h": pm25_mean,
    }


def fetch_csv(station_id, now, deadline, user_agent):
    """The station's hourly values of yesterday and today, as text."""
    remaining = deadline - time.monotonic()
    if remaining < MIN_TIME:
        raise TimeoutError("too little time left before Dux Shell stops the plugin")
    today = now.astimezone(CET).date()
    form = [("nach", "station"), ("abfrageflag", "true"), ("station", station_id)]
    form += [("schadstoffsliste[]", pid) for pid in POLLUTANT_IDS]
    form += [("datentyp", "stunden"), ("zeitraum", "frei"),
             ("von", (today - timedelta(days=1)).isoformat()), ("bis", today.isoformat()),
             ("ausgabe", "csv")]
    request = urllib.request.Request(URL, data=urllib.parse.urlencode(form).encode("ascii"),
                                     headers={"User-Agent": user_agent})
    # Leave a second for printing the forecast.
    with urllib.request.urlopen(request, timeout=max(1, remaining - 1)) as response:
        # ISO-8859-1 by the server's word; latin-1 decodes any byte, so an odd
        # encoding garbles at most a station name, never the numbers.
        return response.read(1_000_000).decode("latin-1")


def cache_path():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, APP, "airquality.json")


def valid_entry(entry, now_s):
    if not isinstance(entry, dict) or finite(entry.get("at")) is None or not 0 <= now_s - entry["at"] < RESULT_TTL:
        return False
    current = entry.get("current")
    if current is None:
        return True
    return isinstance(current, dict) and all(
        current.get(f) is None or finite(current.get(f)) is not None for f in FIELDS)


def read_cache(path):
    try:
        with open(path, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        return {}
    return cache if isinstance(cache, dict) else {}


def write_cache(path, cache, now_s):
    # Drop what has expired, so the file holds only live entries.
    cache = {k: v for k, v in cache.items() if valid_entry(v, now_s)}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    except OSError:
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cache, f, allow_nan=False)
        os.replace(tmp, path)
    except (OSError, ValueError):
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def air_quality(lat, lon, now, deadline, user_agent):
    """(fields, note): the air quality fields of the nearest station, all None
    when there is none or it has no fresh PM2.5, and a note for stderr or
    None. Results and the lack of fresh data are cached for RESULT_TTL;
    errors raise and are not cached."""
    station = nearest_station(lat, lon)
    if station is None:
        return dict(NO_DATA), None
    sid, name, _ = station
    now_s = now.timestamp()
    path = cache_path()
    cache = read_cache(path)
    entry = cache.get(str(sid))
    if valid_entry(entry, now_s):
        current = entry["current"]
    else:
        current = readings(parse_csv(fetch_csv(sid, now, deadline, user_agent)), now)
        cache[str(sid)] = {"at": now_s, "current": current}
        write_cache(path, cache, now_s)
    if current is None:
        return dict(NO_DATA), f"NABEL {name}: no PM2.5 in the last {int(STALE_AFTER.total_seconds() // 3600)} hours"
    return {f: current.get(f) for f in FIELDS}, None
