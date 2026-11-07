"""Tests for the NABEL air quality source. No network: HTTP is mocked.

Run from the repository: python3 -m unittest discover -s tests
"""

import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import nabel  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "nabel-bern.csv")
# 16:50 CET; the fixture's last value is for the hour ending 16:00 CET (15:00 UTC).
NOW = datetime(2026, 11, 7, 15, 50, tzinfo=timezone.utc)
LAST_HOUR = datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)
BERN = (46.95, 7.45)
UA = "test-agent"


def fixture_text():
    with open(FIXTURE, "rb") as f:
        return f.read().decode("latin-1")


def csv(columns, rows, station="Härkingen-A1"):
    """A NABEL answer. rows: (time text, [values]) with None for an empty field."""
    lines = [f"Station: {station}", "Stundenmittelwerte", "MEZ/CET", "Quelle: NABEL",
             "Die Messwerte des laufenden Jahres sind vorläufig und noch nicht abschliessend geprüft.",
             "Datum/Zeit;" + ";".join(f"{c} [ug/m3]" for c in columns)]
    for at, values in rows:
        lines.append(at + ";" + ";".join("" if v is None else str(v) for v in values))
    return "\r\n".join(lines) + "\r\n"


def hourly_rows(values, last_end=LAST_HOUR):
    """Rows with one PM2.5 value each, the last one ending at last_end."""
    rows = []
    for i, value in enumerate(values):
        end = (last_end - timedelta(hours=len(values) - 1 - i)).astimezone(nabel.CET)
        rows.append((end.strftime("%d.%m.%Y %H:%M"), [value]))
    return rows


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class Parsing(unittest.TestCase):
    def test_real_answer(self):
        series = nabel.parse_csv(fixture_text())
        # Bern-Bollwerk measures no SO2, so there is no column and no series.
        self.assertEqual(set(series), {"o3", "no2", "pm10", "pm25"})
        # 48 rows, of which the 8 future ones up to midnight are empty.
        self.assertEqual({len(v) for v in series.values()}, {40})
        self.assertEqual(series["pm25"][-1], (LAST_HOUR, 10.0))
        self.assertEqual(series["o3"][-1], (LAST_HOUR, 27.6))
        self.assertEqual(series["no2"][0], (datetime(2026, 11, 6, 0, 0, tzinfo=timezone.utc), 18.0))

    def test_columns_by_name_not_position(self):
        text = csv(["PM2.5", "SO2", "O3", "PM10", "NO2"], [("07.11.2026 16:00", [1.5, 2.5, 3.5, 4.5, 5.5])])
        series = nabel.parse_csv(text)
        self.assertEqual({p: v[0][1] for p, v in series.items()},
                         {"pm25": 1.5, "so2": 2.5, "o3": 3.5, "pm10": 4.5, "no2": 5.5})

    def test_unknown_columns_and_units_are_skipped(self):
        text = csv(["CO", "PM2.5"], [("07.11.2026 16:00", [400, 8])]).replace(
            "PM2.5 [ug/m3]", "PM2.5 [ppb]")
        self.assertEqual(nabel.parse_csv(text), {})

    def test_empty_bad_and_negative_fields_are_left_out(self):
        text = csv(["PM2.5"], [("07.11.2026 14:00", ["x"]), ("07.11.2026 15:00", [-1]),
                               ("07.11.2026 16:00", [7]), ("07.11.2026 17:00", [None]),
                               ("kaputt", [9]), ("07.11.2026 18:00", ["nan"])])
        self.assertEqual(nabel.parse_csv(text)["pm25"], [(LAST_HOUR, 7.0)])

    def test_latin1_umlauts_in_the_header(self):
        raw = csv(["PM2.5"], [("07.11.2026 16:00", [7])], station="Härkingen-A1").encode("latin-1")
        self.assertIn(b"H\xe4rkingen", raw)
        with mock.patch("urllib.request.urlopen", return_value=Response(raw)):
            text = nabel.fetch_csv(7, NOW, time.monotonic() + 10, UA)
        self.assertIn("Härkingen", text)
        self.assertEqual(nabel.parse_csv(text)["pm25"], [(LAST_HOUR, 7.0)])

    def test_no_table_is_an_error(self):
        for text in ("", "<html>Wartungsarbeiten</html>", "Station: Bern\nQuelle: NABEL\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                nabel.parse_csv(text)


class Times(unittest.TestCase):
    def test_cet_all_year(self):
        # No summer time: 12:00 in July is 11:00 UTC, not 10:00.
        self.assertEqual(nabel.parse_time("07.07.2026 12:00"), datetime(2026, 7, 7, 11, 0, tzinfo=timezone.utc))
        self.assertEqual(nabel.parse_time("07.11.2026 16:00"), LAST_HOUR)

    def test_midnight_as_24_00(self):
        self.assertEqual(nabel.parse_time("07.11.2026 24:00"), nabel.parse_time("08.11.2026 00:00"))
        for text in ("07.11.2026 24:30", "07.11.2026 25:00", "32.11.2026 10:00", "07.11.2026", ""):
            with self.subTest(text=text):
                self.assertIsNone(nabel.parse_time(text))


class Freshness(unittest.TestCase):
    series = staticmethod(lambda: nabel.parse_csv(fixture_text()))

    def test_values_from_the_real_answer(self):
        self.assertEqual(nabel.readings(self.series(), NOW), {
            # NO2 23.0 is the worst: 20 + 13/15 * 20 = 37.3.
            "european_aqi": 37,
            # PM2.5 mean 17.36: 50 + 8.36/26.5 * 50 = 65.8.
            "us_aqi": 66,
            "pm2_5": 10.0,
            "pm10": 12.7,
            "pm2_5_mean_24h": 17.4,
        })

    def test_three_hours_is_the_limit(self):
        # The hour ending 15:00 UTC is still fresh at 18:00 UTC, but not a second later.
        self.assertIsNotNone(nabel.readings(self.series(), LAST_HOUR + timedelta(hours=3)))
        self.assertIsNone(nabel.readings(self.series(), LAST_HOUR + timedelta(hours=3, seconds=1)))

    def test_value_from_the_future_is_ignored(self):
        rows = hourly_rows([5.0, 6.0]) + [("07.11.2026 18:00", [80.0])]
        series = nabel.parse_csv(csv(["PM2.5"], rows))
        self.assertEqual(nabel.readings(series, NOW)["pm2_5"], 6.0)

    def test_stale_pollutant_is_left_out(self):
        rows = [("07.11.2026 11:00", [5.0, 200.0]), ("07.11.2026 16:00", [5.0, None])]
        series = nabel.parse_csv(csv(["PM2.5", "NO2"], rows))
        # NO2 200 from five hours ago would make the index 140.
        self.assertEqual(nabel.readings(series, NOW)["european_aqi"], 20)

    def test_no_pm25_means_no_reading(self):
        # Like Chaumont and Davos, which measure no PM2.5.
        series = nabel.parse_csv(csv(["O3", "NO2", "PM10"], [("07.11.2026 16:00", [40, 7, 10])]))
        self.assertIsNone(nabel.readings(series, NOW))


class Mean(unittest.TestCase):
    def readings(self, values):
        return nabel.readings(nabel.parse_csv(csv(["PM2.5"], hourly_rows(values))), NOW)

    def test_18_of_24_hours(self):
        # Six gaps leave 18 hours: enough for a mean.
        result = self.readings([20.0] * 6 + [None] * 6 + [20.0] * 12)
        self.assertEqual(result["pm2_5_mean_24h"], 20.0)
        # 50 + 11/26.5 * 50 = 70.8.
        self.assertEqual(result["us_aqi"], 71)

    def test_17_hours_fall_back_to_the_latest_value(self):
        result = self.readings([20.0] * 5 + [None] * 7 + [20.0] * 11 + [9.0])
        self.assertIsNone(result["pm2_5_mean_24h"])
        # The latest 9.0 stands in for the mean: exactly 50.
        self.assertEqual(result["us_aqi"], 50)

    def test_window_ends_at_the_latest_hour(self):
        # 30 hours: the 6 oldest of 500 lie outside the 24 hours before the latest.
        result = self.readings([500.0] * 6 + [10.0] * 24)
        self.assertEqual(result["pm2_5_mean_24h"], 10.0)

    def test_window_does_not_end_now(self):
        # The latest hour is almost 3 hours old; its 24 hours count, not the
        # 24 before now, which would hold only 22 of them: 250 / 22 = 11.4.
        series = nabel.parse_csv(csv(["PM2.5"], hourly_rows([40.0] * 3 + [10.0] * 21)))
        result = nabel.readings(series, LAST_HOUR + timedelta(hours=2, minutes=50))
        self.assertEqual(result["pm2_5_mean_24h"], 13.8)


class Stations(unittest.TestCase):
    def test_nearest(self):
        self.assertEqual(nabel.nearest_station(*BERN)[:2], (1, "Bern-Bollwerk"))
        self.assertEqual(nabel.nearest_station(47.37, 8.54)[0], 4)
        self.assertEqual(nabel.nearest_station(46.0, 8.95)[0], 3)

    def test_25_km_cutoff(self):
        lat, lon = 46.950993, 7.440866
        inside, outside = lat + 24 / 111.2, lat + 26 / 111.2
        self.assertAlmostEqual(nabel.distance_km(lat, lon, inside, lon), 24, places=1)
        self.assertEqual(nabel.nearest_station(inside, lon)[0], 1)
        self.assertIsNone(nabel.nearest_station(outside, lon))
        # Upper Engadine, about 35 km from Davos.
        self.assertIsNone(nabel.nearest_station(46.6, 10.2))
        self.assertIsNone(nabel.nearest_station(48.14, 11.58))

    def test_jungfraujoch_is_never_chosen(self):
        # Grindelwald is 9 km from Jungfraujoch and more than 25 km from any other station.
        self.assertIsNone(nabel.nearest_station(46.62, 8.04))
        self.assertIsNone(nabel.nearest_station(46.5475, 7.985))

    def test_every_station_but_jungfraujoch_finds_itself(self):
        for sid, name, lat, lon in nabel.STATIONS:
            with self.subTest(name=name):
                self.assertEqual(nabel.nearest_station(lat, lon), None if sid == 16 else (sid, name, 0.0))


class Request(unittest.TestCase):
    def test_form_and_headers(self):
        calls = []

        def urlopen(request, timeout):
            calls.append((request, timeout))
            return Response(b"")

        # 23:30 UTC is already 8 November in CET.
        now = datetime(2026, 11, 7, 23, 30, tzinfo=timezone.utc)
        with mock.patch("urllib.request.urlopen", urlopen):
            nabel.fetch_csv(4, now, time.monotonic() + 10, UA)
        request, timeout = calls[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("User-agent"), UA)
        form = urllib.parse.parse_qs(request.data.decode())
        self.assertEqual(form["station"], ["4"])
        self.assertEqual(form["schadstoffsliste[]"], ["1", "2", "3", "6", "7"])
        self.assertEqual((form["von"], form["bis"]), (["2026-11-07"], ["2026-11-08"]))
        self.assertEqual((form["datentyp"], form["ausgabe"]), (["stunden"], ["csv"]))
        # Bounded by the time left, with a second to spare.
        self.assertLessEqual(timeout, 9)

    def test_no_request_with_less_than_3_seconds_left(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            with self.assertRaises(TimeoutError):
                nabel.fetch_csv(1, NOW, time.monotonic() + 2.9, UA)
        urlopen.assert_not_called()


class TempCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = nabel.cache_path()

    def lookup(self, text=None, now=NOW, error=None, where=BERN):
        """air_quality with fetch_csv mocked; returns (result, number of requests)."""
        fetch = mock.Mock(return_value=fixture_text() if text is None else text, side_effect=error)
        with mock.patch.object(nabel, "fetch_csv", fetch):
            result = nabel.air_quality(*where, now, time.monotonic() + 10, UA)
        return result, fetch.call_count


class Cache(TempCache):
    def test_at_most_one_request_in_30_minutes(self):
        (fields, note), calls = self.lookup()
        self.assertEqual((fields["pm2_5"], note, calls), (10.0, None, 1))
        self.assertEqual(self.lookup(now=NOW + timedelta(minutes=29))[1], 0)
        self.assertEqual(self.lookup(now=NOW + timedelta(minutes=30))[1], 1)

    def test_cached_values_are_the_same(self):
        first, _ = self.lookup()
        second, calls = self.lookup(now=NOW + timedelta(minutes=5))
        self.assertEqual((first, calls), (second, 0))

    def test_no_fresh_data_is_cached_too(self):
        text = csv(["O3", "PM10"], [("07.11.2026 16:00", [40, 10])])
        (fields, note), calls = self.lookup(text)
        self.assertEqual(fields, nabel.NO_DATA)
        self.assertIn("no PM2.5", note)
        (fields, note), calls = self.lookup(text, now=NOW + timedelta(minutes=10))
        self.assertEqual((fields, calls), (nabel.NO_DATA, 0))
        self.assertIn("no PM2.5", note)

    def test_errors_are_not_cached(self):
        for error in (urllib.error.URLError("down"), TimeoutError("slow"), ValueError("no table")):
            with self.subTest(error=error):
                with self.assertRaises(type(error)):
                    self.lookup(error=error)
                self.assertFalse(os.path.exists(self.path))
        # The next refresh asks again.
        self.assertEqual(self.lookup()[1], 1)

    def test_no_station_no_request(self):
        (fields, note), calls = self.lookup(where=(46.6, 10.2))
        self.assertEqual((fields, note, calls), (nabel.NO_DATA, None, 0))

    def test_per_station(self):
        self.lookup()
        self.assertEqual(self.lookup(where=(47.37, 8.54))[1], 1)
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(set(json.load(f)), {"1", "4"})

    def test_corrupt_cache_is_ignored(self):
        os.makedirs(os.path.dirname(self.path))
        contents = ['{"1": ', "[1, 2]", '{"1": {"at": "x"}}',
                    json.dumps({"1": {"at": NOW.timestamp(), "current": "x"}}),
                    json.dumps({"1": {"at": NOW.timestamp(), "current": {"pm2_5": "10"}}}),
                    '{"1": {"at": %s, "current": {"pm2_5": NaN}}}' % NOW.timestamp(),
                    # From the future, as after the clock was set back.
                    json.dumps({"1": {"at": NOW.timestamp() + 600, "current": None}})]
        for content in contents:
            with self.subTest(content=content):
                with open(self.path, "w", encoding="utf-8") as f:
                    f.write(content)
                (fields, _), calls = self.lookup()
                self.assertEqual((fields["pm2_5"], calls), (10.0, 1))

    def test_written_atomically(self):
        self.lookup()
        with open(self.path, encoding="utf-8") as f:
            old = f.read()
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            self.lookup(now=NOW + timedelta(hours=1))
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), old)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["airquality.json"])

    def test_expired_entries_are_dropped(self):
        self.lookup()
        self.lookup(where=(47.37, 8.54), now=NOW + timedelta(hours=1))
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(set(json.load(f)), {"4"})

    def test_unwritable_cache_is_no_error(self):
        with mock.patch("os.makedirs", side_effect=PermissionError("read-only")):
            (fields, _), calls = self.lookup()
        self.assertEqual((fields["pm2_5"], calls), (10.0, 1))


if __name__ == "__main__":
    unittest.main()
