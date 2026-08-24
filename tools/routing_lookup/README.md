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
# Run with the example input and default config (writes results.csv)
python route_lookup.py --input example_input.csv --output results.csv

# Use Nominatim instead of Photon for geocoding
python route_lookup.py --input example_input.csv --output results.csv --geocoder nominatim

# Override service URLs on the command line
python route_lookup.py --input example_input.csv --output results.csv \
    --photon-url https://photon.example.com \
    --nominatim-url https://nominatim.example.com \
    --valhalla-url https://valhalla.example.com

# Disable optimization (single-pass, base config only)
python route_lookup.py --input example_input.csv --output results.csv --no-optimize

# Enable verbose / debug logging
python route_lookup.py --input example_input.csv --output results.csv --verbose
```

## Input CSV format

Semicolon-delimited, UTF-8, with a header row:

```
id;destination_ascertained;destination_entry;distance;origin_ascertained;origin_entry;saved_date;is_used
```

Addresses are stored as `"<country>;<postcode>;<city>;<street>"` (the script parses
this format automatically).

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

## Timing

Per-component timing (Photon/Nominatim, Valhalla, total) is printed to stderr at
the end of each run:

```
2026-08-24 12:00:01  INFO      --- Timing summary ---
2026-08-24 12:00:01  INFO        photon          2.134 s
2026-08-24 12:00:01  INFO        valhalla        1.876 s
2026-08-24 12:00:01  INFO        total           4.121 s
```
