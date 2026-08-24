# routing_lookup

A standalone Python utility that reads a semicolon-separated CSV of origin/destination
addresses, geocodes them via **Photon** or **Nominatim**, queries **Valhalla** for route
distances, and writes a result CSV comparing the stored distance with the Valhalla
distance.

## Requirements

- Python 3.8+ (standard library only – no additional packages required)
- A reachable Photon, Nominatim, and/or Valhalla instance

## Files

| File | Purpose |
|---|---|
| `route_lookup.py` | Main script |
| `config.json` | Default configuration (edit service URLs, routing parameters, …) |
| `example_input.csv` | Sample input file matching the expected CSV format |

## Quick start

```bash
# Positional shortcut (quickest):
python route_lookup.py QVadis.csv results.csv

# Or with named flags:
python route_lookup.py --input QVadis.csv --output results.csv

# Use Nominatim instead of Photon for geocoding
python route_lookup.py QVadis.csv results.csv --geocoder nominatim

# Override service URLs on the command line
python route_lookup.py QVadis.csv results.csv \
    --photon-url https://photon.example.com \
    --nominatim-url https://nominatim.example.com \
    --valhalla-url https://valhalla.example.com

# Use a custom config file
python route_lookup.py QVadis.csv results.csv --config myconfig.json

# Disable optimization (single-pass, base config only)
python route_lookup.py QVadis.csv results.csv --no-optimize

# Evaluate each setting globally over all rows and save best one
python route_lookup.py QVadis.csv results.csv --optimize-global --optimal-config-out optimal.json

# Enable verbose / debug logging
python route_lookup.py QVadis.csv results.csv --verbose
```

## Input CSV format

Semicolon-delimited, UTF-8, with a header row:

```
id;destination_ascertained;destination_entry;distance;origin_ascertained;origin_entry;saved_date;is_used
```

Addresses are stored as `"<country>;<postcode>;<city>;<street>"` (the script parses
this format automatically).

When using `geocoder=photon`, the script can automatically fall back to
Nominatim if Photon returns no coordinates (`fallback_to_nominatim` in
`config.json`, enabled by default).

## Output CSV format

Also semicolon-delimited:

| Column | Description |
|---|---|
| `id` | Row identifier from the input |
| `origin_address` | Geocoded query string for the origin |
| `destination_address` | Geocoded query string for the destination |
| `original_meters` | Distance stored in the input file |
| `valhalla_meters` | Distance returned by Valhalla (best match when optimizing) |
| `difference_meters` | `valhalla_meters − original_meters` |
| `best_costing` | Valhalla costing profile that produced the best match |
| `error` | Non-empty if geocoding or routing failed |

## Configuration (`config.json`)

```json
{
  "photon_base_url":    "https://photon.optadata.io",
  "nominatim_base_url": "https://nominatim.optadata.io",
  "valhalla_base_url":  "https://valhalla.optadata.io",

  "geocoder": "photon",

  "photon":    { "limit": 1, "lang": "de" },
  "nominatim": { "format": "json", "limit": 1, "countrycodes": "de" },

  "valhalla": {
    "costing": "auto",
    "costing_options": {},
    "directions_type": "none",
    "units": "km"
  },

  "optimize": {
    "enabled": true,
    "costing_variants":         ["auto", "truck"],
    "units_variants":           ["km"],
    "costing_options_variants": [
      {},
      {"auto": {"use_highways": 1.0, "use_tolls": 0.5}},
      {"auto": {"use_highways": 0.5, "use_tolls": 0.0}}
    ]
  },

  "timeout_seconds": 10
}
```

### Optimization

When `optimize.enabled` is `true` the script tries every combination of
`costing_variants × units_variants × costing_options_variants` for each row and
keeps the result closest to the stored original distance.  Disable it with
`--no-optimize` or by setting `"enabled": false` in the config.

### Global optimization mode

With `--optimize-global` the script evaluates **one setting over all rows**,
then the next setting, and so on. After each setting it prints:

- per-request step logs (`route <n> step <m>/<total_steps>`)
- routed/failed/geocoding-failed counts
- elapsed time for the setting
- overall absolute difference to stored distances in percent
- overall signed difference in percent

The best overall setting is written to `optimal.json` by default, or to the
path passed via `--optimal-config-out`.

#### Value ranges (from/to/step)

Any scalar in `costing_options_variants` can be replaced with a range dict so
the script sweeps that parameter automatically:

```json
{
  "auto": {
    "use_highways": { "from": 0.0, "to": 1.0, "step": 0.25 },
    "use_tolls": 0.5
  }
}
```

This generates 5 combinations (0.0 / 0.25 / 0.5 / 0.75 / 1.0) for
`use_highways`, each paired with `use_tolls = 0.5`.  Ranges from
multiple keys are cross-multiplied.

#### All available Valhalla costing parameters

The `_all_valhalla_costing_options` section in `config.json` documents every
known Valhalla option for `auto`, `truck`, `pedestrian`, `bicycle`,
`motorcycle`, and `motor_scooter` profiles.  Copy any of these into
`costing_options_variants` to include them in the optimization sweep.

Key parameters for car routing (`auto` / `truck`):

| Parameter | Range | Meaning |
|---|---|---|
| `use_highways` | 0.0–1.0 | 0 = avoid motorways, 1 = prefer motorways |
| `use_tolls` | 0.0–1.0 | 0 = avoid tolls, 1 = accept tolls freely |
| `use_ferry` | 0.0–1.0 | 0 = avoid ferries, 1 = prefer ferries |
| `top_speed` | km/h | Assumed top speed (affects time-based routing) |
| `shortest` | bool | true = minimize distance instead of time |
| `maneuver_penalty` | seconds | Penalty applied at every turn/manoeuvre |
| `country_crossing_cost` | seconds | Extra time for crossing a border |
| `gate_cost` / `gate_penalty` | seconds | Cost for passing a gate |
| `private_access_penalty` | seconds | Penalty for roads with private access |

## Timing

Per-component timing (Photon/Nominatim, Valhalla, total) is printed to stderr at
the end of each run:

```
2026-08-24 12:00:01  INFO      --- Timing summary ---
2026-08-24 12:00:01  INFO        photon          2.134 s
2026-08-24 12:00:01  INFO        valhalla        1.876 s
2026-08-24 12:00:01  INFO        total           4.121 s
```
