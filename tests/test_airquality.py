"""Tests for the air quality indices, taken over from dms-openaq-air-quality.

Run from the repository: python3 -m unittest discover -s tests
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import airquality  # noqa: E402

NOW = datetime(2026, 11, 7, 14, 20, tzinfo=timezone.utc)


class EuropeanIndex(unittest.TestCase):
    def test_every_band_boundary(self):
        for pollutant, thresholds in airquality.EAQI_THRESHOLDS.items():
            for step, concentration in enumerate(thresholds):
                with self.subTest(pollutant=pollutant, concentration=concentration):
                    self.assertAlmostEqual(airquality.eaqi(pollutant, concentration), step * 20)

    def test_interpolates_inside_a_band(self):
        # PM2.5 15-50 is "moderate", 40-60.
        self.assertAlmostEqual(airquality.eaqi("pm25", 32.5), 50)
        self.assertAlmostEqual(airquality.eaqi("pm10", 7.5), 10)
        # Just below a boundary stays in the lower band.
        self.assertLess(airquality.eaqi("pm25", 4.99), 20)

    def test_extrapolates_above_the_top(self):
        # Last PM2.5 step is 90-140, 50 µg/m³ per 20 points.
        self.assertAlmostEqual(airquality.eaqi("pm25", 190), 120)
        self.assertAlmostEqual(airquality.eaqi("o3", 200), 120)

    def test_missing_or_negative_is_none(self):
        for value in (None, -1, float("nan"), float("inf"), "12", True):
            with self.subTest(value=value):
                self.assertIsNone(airquality.eaqi("pm25", value))
        self.assertIsNone(airquality.eaqi("co", 5))

    def test_worst_pollutant_wins(self):
        # PM2.5 10 → 30, PM10 20 → 26.7, NO2 70 → 65.
        index = airquality.european_aqi({"pm25": 10, "pm10": 20, "no2": 70})
        self.assertAlmostEqual(index, 65)
        self.assertAlmostEqual(airquality.european_aqi({"pm25": 10, "pm10": None}), 30)
        self.assertIsNone(airquality.european_aqi({}))
        self.assertIsNone(airquality.european_aqi({"pm25": None}))


class USIndex(unittest.TestCase):
    STEPS = [0, 50, 100, 150, 200, 300, 500]

    def test_every_breakpoint(self):
        for pollutant, thresholds in airquality.US_THRESHOLDS.items():
            for concentration, index in zip(thresholds, self.STEPS):
                with self.subTest(pollutant=pollutant, concentration=concentration):
                    self.assertAlmostEqual(airquality.us_index(pollutant, concentration), index)

    def test_2024_pm25_breakpoints(self):
        # The revision moved "good" from 12 to 9 µg/m³.
        self.assertAlmostEqual(airquality.us_index("pm25", 9), 50)
        self.assertGreater(airquality.us_index("pm25", 12), 50)

    def test_interpolates_and_extrapolates(self):
        self.assertAlmostEqual(airquality.us_index("pm25", 4.5), 25)
        # 225.5-325.5 spans 300-500, so 275.5 is 400.
        self.assertAlmostEqual(airquality.us_index("pm25", 275.5), 400)
        # The scale ends at 500: the top breakpoint and anything above it.
        self.assertAlmostEqual(airquality.us_index("pm25", 325.4), 499.8)
        self.assertEqual(airquality.us_index("pm25", 325.5), 500)
        self.assertEqual(airquality.us_index("pm25", 425.5), 500)
        self.assertEqual(airquality.us_index("pm10", 900), 500)
        self.assertAlmostEqual(airquality.us_index("pm10", 105), 75)

    def test_worse_of_pm25_and_pm10(self):
        self.assertAlmostEqual(airquality.us_aqi(9, 155), 100)
        self.assertAlmostEqual(airquality.us_aqi(35.5, 0), 100)
        self.assertAlmostEqual(airquality.us_aqi(None, 55), 50)
        self.assertIsNone(airquality.us_aqi(None, None))
        self.assertIsNone(airquality.us_aqi(-3, None))


class Rounding(unittest.TestCase):
    def test_indices_round_half_up(self):
        self.assertEqual(airquality.round_half_up(50.5), 51)
        self.assertEqual(airquality.round_half_up(52.5), 53)
        self.assertEqual(airquality.round_half_up(50.49), 50)
        self.assertEqual(airquality.round_half_up(0), 0)
        self.assertIsNone(airquality.round_half_up(None))
        self.assertIsInstance(airquality.round_half_up(7.5), int)


class Mean24h(unittest.TestCase):
    def series(self, count, value=10.0):
        return [(NOW.replace(minute=0) - timedelta(hours=i), value) for i in range(count)]

    def test_needs_18_hours(self):
        self.assertEqual(airquality.mean_24h(self.series(18), NOW), 10.0)
        self.assertIsNone(airquality.mean_24h(self.series(17), NOW))

    def test_ignores_hours_outside_the_window(self):
        old = [(NOW - timedelta(hours=30 + i), 500.0) for i in range(10)]
        future = [(NOW + timedelta(hours=1), 500.0)]
        self.assertEqual(airquality.mean_24h(self.series(24) + old + future, NOW), 10.0)
        # 24 hours back exactly is outside, the hour ending now is inside.
        self.assertIsNone(airquality.mean_24h([(NOW - timedelta(hours=24), 1.0)] + self.series(17), NOW))

    def test_rounds_and_skips_bad_values(self):
        values = [(NOW.replace(minute=0) - timedelta(hours=i), v)
                  for i, v in enumerate([1, 2] * 9 + [None, -2, float("nan")])]
        self.assertEqual(airquality.mean_24h(values, NOW), 1.5)

    def test_duplicate_hours_count_once(self):
        self.assertIsNone(airquality.mean_24h(self.series(10) + self.series(10), NOW))


if __name__ == "__main__":
    unittest.main()
