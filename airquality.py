"""Air quality indices, the same as in dms-openaq-air-quality and Open-Meteo.

European index: EEA bands as revised in 2024, from hourly concentrations,
20 points per band, extrapolated above 100, the worst pollutant wins.
US index: EPA breakpoints as revised in 2024, from 24-hour means, capped
at 500. All concentrations in µg/m³.
"""

import math
from datetime import timedelta

# The EEA's 75 % of 24 hours for a daily mean, as in dux-go.
MIN_HOURS_FOR_MEAN = 18

# European Air Quality Index, bands as revised by the EEA in 2024, for hourly
# concentrations in µg/m³. The same tables and interpolation as Open-Meteo, so
# the index reads the same as the one it fills in.
EAQI_THRESHOLDS = {
    "pm25": [0, 5, 15, 50, 90, 140],
    "pm10": [0, 15, 45, 120, 195, 270],
    "no2": [0, 10, 25, 60, 100, 150],
    "o3": [0, 60, 100, 120, 160, 180],
    "so2": [0, 20, 40, 125, 190, 275],
}

# US AQI, the EPA's breakpoints for 24-hour means in µg/m³, PM2.5 as revised
# in 2024. Index steps 0, 50, 100, 150, 200, 300, 500.
US_THRESHOLDS = {
    "pm25": [0, 9, 35.5, 55.5, 125.5, 225.5, 325.5],
    "pm10": [0, 55, 155, 255, 355, 425, 605],
}
US_MAX = 500


def finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def position(thresholds, value):
    """Where value falls in thresholds, interpolated linearly between two of
    them and extrapolated with the last step's slope above the top."""
    for i in range(1, len(thresholds)):
        if value < thresholds[i]:
            low, high = thresholds[i - 1], thresholds[i]
            return i - 1 + (value - low) / (high - low)
    low, high = thresholds[-2], thresholds[-1]
    return len(thresholds) - 2 + (value - low) / (high - low)


def eaqi(pollutant, concentration):
    """European index, 0 to 100 and above, for one hourly concentration in
    µg/m³. None for a missing or negative concentration."""
    concentration = finite(concentration)
    if concentration is None or concentration < 0 or pollutant not in EAQI_THRESHOLDS:
        return None
    return position(EAQI_THRESHOLDS[pollutant], concentration) * 20


def european_aqi(concentrations):
    """The worst European index of the pollutants given, None without any."""
    indices = [eaqi(p, c) for p, c in concentrations.items()]
    indices = [i for i in indices if i is not None]
    return max(indices) if indices else None


def us_scale(step):
    # Steps 0-4 are 50 index points each, step 5 is 100 and step 6 is 200.
    # The EPA's scale ends at 500, "hazardous" covers everything above.
    if step <= 4:
        return step * 50
    if step <= 5:
        return step * 100 - 200
    return min(step * 200 - 700, US_MAX)


def us_index(pollutant, mean):
    """US index, 0 to 500, for one 24-hour mean in µg/m³, None when missing
    or negative."""
    mean = finite(mean)
    if mean is None or mean < 0 or pollutant not in US_THRESHOLDS:
        return None
    return us_scale(position(US_THRESHOLDS[pollutant], mean))


def us_aqi(pm25, pm10):
    indices = [i for i in (us_index("pm25", pm25), us_index("pm10", pm10)) if i is not None]
    return max(indices) if indices else None


def mean_24h(hours, now):
    """Mean of the hourly values that ended in the 24 hours before now, rounded
    to 0.1. hours is a list of (end of hour, value). None with fewer than
    MIN_HOURS_FOR_MEAN of those hours. Negative values are sensor noise and
    count as missing."""
    by_hour = {}
    for end, value in hours:
        value = finite(value)
        if end is None or value is None or value < 0 or not now - timedelta(hours=24) < end <= now:
            continue
        by_hour.setdefault(end.replace(minute=0, second=0, microsecond=0), value)
    if len(by_hour) < MIN_HOURS_FOR_MEAN:
        return None
    return round(sum(by_hour.values()) / len(by_hour), 1)


def round_half_up(value):
    """An index rounded to a whole number, .5 up as the EPA and Open-Meteo do;
    round() would take 50.5 down to 50. None stays None."""
    return None if value is None else math.floor(value + 0.5)
