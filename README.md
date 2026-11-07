# MeteoSwiss Weather

A [Dux Shell](https://github.com/duxlabs-dev/dux-shell) plugin that adds
MeteoSwiss as a weather provider. It uses the same local forecast as the
MeteoSwiss app, which comes from MeteoSwiss's high-resolution models for the
Alps (ICON-CH, INCA).

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
3. Prints it in the Open-Meteo response format that Dux Shell expects.

Test it by hand:

```sh
echo '{"latitude": 46.948, "longitude": 7.4474}' | ./weather.py | python3 -m json.tool | head
```

`./weather.py 46.948 7.4474` works as well.

## Limits

- **Unofficial endpoint.** The app endpoint is not a documented API and can
  change without notice. If it breaks, Dux Shell falls back to the next
  provider until the plugin is fixed. The official MeteoSwiss open data
  (CC BY 4.0) is only offered as large files covering all of Switzerland,
  about 30 MB per parameter, which does not suit a widget that refreshes
  every 15 minutes.
- **Switzerland only.**
- **Privacy.** The coordinates, rounded to about 100 m, go to swisstopo once
  per location, and the postcode goes to MeteoSwiss on each refresh.

Weather data: Source: MeteoSwiss. This plugin is not affiliated with or
endorsed by MeteoSwiss.

## License

MIT
