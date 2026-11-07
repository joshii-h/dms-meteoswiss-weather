# MeteoSwiss Weather

A [Dux Shell](https://github.com/duxlabs-dev/dux-shell) plugin that adds
MeteoSwiss as a weather provider. It uses the same local forecast as the
MeteoSwiss app, which comes from MeteoSwiss's high-resolution models for the
Alps (ICON-CH, INCA). Air quality comes from the nearest station of NABEL,
the Swiss national air pollution monitoring network.

The plugin has no widget of its own. It feeds the built-in weather (bar,
dashboard, lock screen) and sits in the weather provider list next to
Open-Meteo and MET Norway.

## Requirements

- Dux Shell 2.1.1 or newer, the first release whose dms runs weather provider
  plugins (the `weather.plugins` capability).
- `python3` (standard library only)
- A weather location in Switzerland. Elsewhere the plugin reports an error
  and Dux Shell moves on to the next provider.

## Installation

```sh
cd ~/.config/dms/plugins
git clone https://github.com/joshii-h/dms-meteoswiss-weather meteoswissWeather
```

Then:

1. Enable **MeteoSwiss Weather** in Settings → Plugins.
2. In Settings → Date, time & region → Weather → Weather providers, switch on
   **MeteoSwiss** and drag it to the top.
3. Keep Open-Meteo and/or MET Norway switched on below it. They take over when
   MeteoSwiss does not answer, and they fill in the readings MeteoSwiss does
   not provide.

## What it provides

| Reading | MeteoSwiss |
|---|---|
| Temperature, weather symbol | ✓ (current, hourly for 6 days, daily) |
| Wind speed and direction | ✓ |
| Precipitation probability | ✓ (3-hour steps) |
| Sunrise, sunset | ✓ |
| Air quality (European and US index, PM2.5, PM10) | ✓ from the nearest NABEL station, see [Air quality](#air-quality) |
| Feels like, humidity, pressure, dew point, UV, visibility | – (filled in by the next enabled provider) |

MeteoSwiss weather symbols are mapped to WMO weather codes following the
official legend "Wettersymbole: Texte/Übersetzungen".

## How it works

About every 15 minutes Dux Shell runs `weather.py` with the coordinates as
JSON on stdin (`{"latitude": 46.948, "longitude": 7.4474}`). The script:

1. Looks up the postcode for the coordinates with the swisstopo API
   (official directory of localities). The result is cached in
   `~/.cache/dms-meteoswiss-weather/postcodes.json`, so this happens once per
   location.
2. Fetches the forecast for that postcode from the endpoint behind the
   MeteoSwiss app.
3. Adds the air quality of the nearest NABEL station (see below).
4. Prints it in the Open-Meteo response format that Dux Shell expects.

Test it by hand:

```sh
echo '{"latitude": 46.948, "longitude": 7.4474}' | ./weather.py | python3 -m json.tool | head
```

`./weather.py 46.948 7.4474` works as well.

## Air quality

The readings come from NABEL, the national air pollution monitoring network
run by the Federal Office for the Environment (BAFU) and Empa: 16 stations
across Switzerland, measured every hour.

- **Station.** The nearest station within 25 km of the weather location.
  Jungfraujoch (3580 m) is never used, it measures the background air of the
  Alps. The other mountain and rural stations (Rigi-Seebodenalp, Chaumont,
  Davos) count like any other, so the nearest station wins. Without a station
  within 25 km, for example in the Engadine or the Upper Valais, the plugin
  delivers no air quality.
- **Values.** Latest hourly PM2.5 and PM10 in µg/m³, the PM2.5 mean of the
  24 hours up to the latest hour (from at least 18 of them), the European Air
  Quality Index (EEA bands of 2024, from the latest hourly PM2.5, PM10, NO2,
  O3 and SO2, the worst pollutant counts) and the US AQI (EPA breakpoints of
  2024, from the 24-hour means of PM2.5 and PM10; without a mean the latest
  value stands in). The indices are calculated the same way as Open-Meteo's,
  so they read the same.
- **Freshness.** A pollutant whose latest value is older than 3 hours is left
  out. A station without a fresh PM2.5 value delivers nothing; Chaumont and
  Davos do not measure PM2.5 at all.
- **Updates.** NABEL publishes the hourly values with a delay of about an
  hour. The result is cached per station for 30 minutes in
  `~/.cache/dms-meteoswiss-weather/airquality.json`, so NABEL is asked at most
  twice an hour.
- **Fallback.** Whatever the plugin does not deliver, Open-Meteo fills in with
  its modelled air quality, if it is switched on. Values from this plugin
  take precedence over Open-Meteo's.
- **Dux Shell version.** The air quality only shows with a dms release that
  includes air quality support. Older releases ignore the extra readings.

The values come from the backend of BAFU's public query form, which is not a
documented API. If it changes, the air quality stays empty and Open-Meteo
fills in; the forecast is never affected. A failing NABEL request only leaves
a note on stderr.

Quelle: NABEL, BAFU — vorläufige Daten. The values of the current year are
provisional and not yet fully validated.

## Tests

```sh
python3 -m unittest discover -s tests
```

The tests use no network.

## Limits

- **Unofficial endpoint.** The app endpoint is not a documented API and can
  change without notice. If it breaks, Dux Shell falls back to the next
  provider until the plugin is fixed. The official MeteoSwiss open data
  (CC BY 4.0) is only offered as large files covering all of Switzerland,
  about 30 MB per parameter, which does not suit a widget that refreshes
  every 15 minutes.
- **Switzerland only.**
- **Privacy.** The coordinates, rounded to about 100 m, go to swisstopo once
  per location, and the postcode goes to MeteoSwiss on each refresh. NABEL
  only learns the number of the nearest station; the station is picked
  locally.

Weather data: Source: MeteoSwiss. Air quality: Quelle: NABEL, BAFU. This plugin is not affiliated with or
endorsed by MeteoSwiss or BAFU.

## License

MIT
