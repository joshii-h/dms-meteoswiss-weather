"""Tests for weather.py: the forecast mapping and that air quality can never
stop the forecast. No network: requests are mocked.

Run from the repository: python3 -m unittest discover -s tests
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from datetime import datetime
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
import nabel  # noqa: E402
import weather  # noqa: E402

AQ_FIELDS = ("european_aqi", "us_aqi", "pm2_5", "pm10", "pm2_5_mean_24h")
# Local midnight of 7 November 2026 in Zurich (CET), in ms.
START = int(datetime(2026, 11, 7, 0, 0, tzinfo=weather.ZONE).timestamp() * 1000)
HOUR = weather.HOUR_MS


def meteoswiss():
    """A small answer of the MeteoSwiss app endpoint: one day, 24 hours."""
    return {
        "currentWeather": {"time": START + 14 * HOUR + 20 * 60000, "icon": 5, "temperature": 9.5},
        "graph": {
            "start": START,
            "temperatureMean1h": [float(i) for i in range(24)],
            "windSpeed1h": [3.0] * 24,
            # 3-hour steps: night, night, sun, cloud, cloud, rain, night, night.
            "weatherIcon3h": [101, 101, 1, 5, 5, 17, 101, 101],
            "windDirection3h": [200] * 8,
            "precipitationProbability3h": [0, 0, 10, 20, 30, 80, 10, 0],
            "sunrise": [START + 7 * HOUR + 15 * 60000],
            "sunset": [START + 17 * HOUR],
        },
        "forecast": [{"dayDate": "2026-11-07", "iconDay": 5, "temperatureMax": 11, "temperatureMin": 2}],
    }


class Mapping(unittest.TestCase):
    def test_open_meteo_shape(self):
        forecast = weather.to_open_meteo(meteoswiss(), 46.95, 7.45)
        hourly, daily, current = forecast["hourly"], forecast["daily"], forecast["current"]
        self.assertEqual(hourly["time"][0], "2026-11-07T00:00")
        self.assertEqual(len(hourly["time"]), 24)
        self.assertEqual(hourly["weather_code"][6], 0)
        self.assertEqual(hourly["weather_code"][15], 63)
        self.assertEqual(hourly["is_day"][:7], [0] * 6 + [1])
        self.assertEqual(hourly["precipitation_probability"][16], 80)
        self.assertEqual(daily["precipitation_probability_max"], [80])
        self.assertEqual(daily["sunrise"], ["2026-11-07T07:15"])
        self.assertEqual(current["time"], "2026-11-07T14:20")
        self.assertEqual((current["weather_code"], current["is_day"], current["wind_direction_10m"]), (3, 1, 200))

    def test_bad_values_are_none(self):
        data = meteoswiss()
        data["graph"]["temperatureMean1h"][0] = None
        data["currentWeather"]["icon"] = 99
        forecast = weather.to_open_meteo(data, 46.95, 7.45)
        self.assertIsNone(forecast["hourly"]["temperature_2m"][0])
        self.assertIsNone(forecast["current"]["weather_code"])


class PostcodeCache(unittest.TestCase):
    def test_unwritable_cache_does_not_cost_the_postcode(self):
        import tempfile
        with tempfile.TemporaryDirectory() as home:
            # A file where the cache directory should be: makedirs fails.
            blocker = os.path.join(home, "dms-meteoswiss-weather")
            open(blocker, "w").close()
            answer = {"results": [{"attributes": {"plz": 3000, "zusziff": 0}}]}
            with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": home}), \
                    mock.patch.object(weather, "get_json", return_value=answer):
                self.assertEqual(weather.postcode(46.95, 7.45), 300000)


class Main(unittest.TestCase):
    def setUp(self):
        patches = [mock.patch.object(weather, "DEADLINE", time.monotonic() + 12),
                   mock.patch.object(weather, "postcode", return_value=300100),
                   mock.patch.object(weather, "get_json", side_effect=lambda url: meteoswiss()),
                   mock.patch.object(sys, "argv", ["weather.py", "46.95", "7.45"])]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def run_main(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            weather.main()
        return json.loads(out.getvalue()), err.getvalue()

    def test_air_quality_lands_in_current(self):
        values = {"european_aqi": 37, "us_aqi": 66, "pm2_5": 10.0, "pm10": 12.7, "pm2_5_mean_24h": 17.4}
        with mock.patch.object(nabel, "air_quality", return_value=(values, None)) as aq:
            forecast, err = self.run_main()
        self.assertEqual({f: forecast["current"][f] for f in AQ_FIELDS}, values)
        self.assertEqual(forecast["current"]["temperature_2m"], 9.5)
        self.assertEqual(err, "")
        lat, lon, _, deadline, agent = aq.call_args.args
        self.assertEqual((lat, lon, deadline, agent), (46.95, 7.45, weather.DEADLINE, weather.USER_AGENT))

    def test_every_nabel_failure_leaves_only_air_quality_empty(self):
        for error in (RuntimeError("boom"), OSError("unreachable"), ValueError("no table"),
                      TimeoutError("slow"), SystemExit("stop"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(nabel, "air_quality", side_effect=error):
                    forecast, err = self.run_main()
                self.assertEqual(forecast["current"]["temperature_2m"], 9.5)
                self.assertEqual([forecast["current"][f] for f in AQ_FIELDS], [None] * 5)
                self.assertIn("air quality unavailable", err)
                self.assertNotIn("took too long", err)
                self.assertIn(str(error), err)

    def test_real_nabel_code_with_a_failing_request(self):
        import urllib.error
        with mock.patch.object(nabel, "fetch_csv", side_effect=urllib.error.URLError("down")), \
                mock.patch.dict(os.environ, {"XDG_CACHE_HOME": os.devnull + "/x"}):
            forecast, err = self.run_main()
        self.assertEqual([forecast["current"][f] for f in AQ_FIELDS], [None] * 5)
        self.assertIn("down", err)

    def test_slow_nabel_is_given_up_at_the_deadline(self):
        release = threading.Event()
        self.addCleanup(release.set)
        weather.DEADLINE = time.monotonic() + 0.3
        with mock.patch.object(nabel, "air_quality", side_effect=lambda *a: release.wait(30)):
            started = time.monotonic()
            forecast, err = self.run_main()
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual([forecast["current"][f] for f in AQ_FIELDS], [None] * 5)
        self.assertIn("took too long", err)

    def test_missing_module_is_no_error(self):
        with mock.patch.object(weather, "nabel", None), \
                mock.patch.object(weather, "NABEL_IMPORT_ERROR", ImportError("no nabel"), create=True):
            forecast, err = self.run_main()
        self.assertEqual([forecast["current"][f] for f in AQ_FIELDS], [None] * 5)
        self.assertIn("no nabel", err)

    def test_failing_forecast_still_fails(self):
        # Without a forecast Dux Shell must move on to the next provider.
        weather.get_json.side_effect = SystemExit("MeteoSwiss returned no forecast")
        with mock.patch.object(nabel, "air_quality") as aq, self.assertRaises(SystemExit):
            self.run_main()
        aq.assert_not_called()


class Isolated(unittest.TestCase):
    def test_broken_module_is_no_import_error(self):
        import shutil
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copy(os.path.join(ROOT, "weather.py"), tmp)
            with open(os.path.join(tmp, "nabel.py"), "w", encoding="utf-8") as f:
                f.write("raise ImportError('broken')\n")
            code = ("import runpy, sys; g = runpy.run_path(sys.argv[1]); "
                    "sys.exit(0 if g['nabel'] is None and 'broken' in str(g['NABEL_IMPORT_ERROR']) else 1)")
            result = subprocess.run([sys.executable, "-I", "-c", code, os.path.join(tmp, "weather.py")],
                                    cwd="/", capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_modules_found_under_python_I(self):
        # -I leaves the script's directory off sys.path; weather.py adds it.
        code = ("import runpy, sys; g = runpy.run_path(sys.argv[1]); "
                "sys.exit(0 if g['nabel'] is not None else 1)")
        result = subprocess.run([sys.executable, "-I", "-c", code, os.path.join(ROOT, "weather.py")],
                                cwd="/", capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
