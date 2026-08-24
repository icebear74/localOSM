#!/usr/bin/env python3
"""
route_lookup.py – Standalone routing distance validator.

Reads a semicolon-delimited CSV of origin/destination addresses, geocodes
them via Photon or Nominatim, queries Valhalla for the route distance, and
writes a result CSV comparing the original distance with the Valhalla distance.

Optionally tries multiple routing-parameter combinations to find the variant
closest to the stored original distance (optimization mode).

Usage
-----
  python route_lookup.py --input example_input.csv --output results.csv
  python route_lookup.py --input example_input.csv --output results.csv \\
      --config config.json --geocoder nominatim

Run  python route_lookup.py --help  for all options.
"""

import argparse
import copy
import csv
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    level=logging.INFO,
    stream=sys.stderr,
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default configuration
# ---------------------------------------------------------------------------
DEFAULT_CONFIG: Dict[str, Any] = {
    "photon_base_url": "https://photon.optadata.io",
    "nominatim_base_url": "https://nominatim.optadata.io",
    "valhalla_base_url": "https://valhalla.optadata.io",
    "geocoder": "photon",
    "photon": {
        "limit": 1,
        "lang": "de",
    },
    "nominatim": {
        "format": "json",
        "limit": 1,
        "countrycodes": "de",
        "addressdetails": 0,
    },
    "valhalla": {
        "costing": "auto",
        "costing_options": {},
        "directions_type": "none",
        "units": "km",
    },
    "optimize": {
        "enabled": True,
        "costing_variants": ["auto", "truck"],
        "units_variants": ["km"],
        "costing_options_variants": [
            {},
            {"auto": {"use_highways": 1.0, "use_tolls": 0.5}},
            {"auto": {"use_highways": 0.5, "use_tolls": 0.0}},
        ],
    },
    "timeout_seconds": 10,
    "input_encoding": "utf-8",
    "output_encoding": "utf-8",
}


# ---------------------------------------------------------------------------
# Address parsing
# ---------------------------------------------------------------------------
def parse_address(raw: str) -> str:
    """
    Convert a semicolon-encoded address field to a plain query string.

    The CSV stores addresses as ``"D;42551;Velbert;Blumenstr. 4"``.
    We drop the country code prefix and join the remaining parts with a comma.
    """
    raw = raw.strip().strip('"')
    parts = [p.strip() for p in raw.split(";")]
    if len(parts) >= 4:
        # Format: country_code ; postcode ; city ; street
        return f"{parts[3]}, {parts[2]}, {parts[1]}, {parts[0]}"
    if len(parts) == 3:
        return f"{parts[2]}, {parts[1]}, {parts[0]}"
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------
def _http_get(url: str, timeout: int) -> Any:
    """Perform a GET request and return the parsed JSON response."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_post(url: str, body: Dict, timeout: int) -> Any:
    """Perform a POST request with a JSON body and return the parsed JSON response."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------
def geocode_photon(
    address: str,
    base_url: str,
    params: Dict,
    timeout: int,
) -> Tuple[Optional[float], Optional[float], float]:
    """
    Geocode *address* using Photon.

    Returns ``(lon, lat, elapsed_seconds)``.  Either coordinate is ``None``
    on failure.
    """
    q = {"q": address, **params}
    url = f"{base_url.rstrip('/')}/api?" + urllib.parse.urlencode(q)
    t0 = time.perf_counter()
    try:
        data = _http_get(url, timeout)
        elapsed = time.perf_counter() - t0
        features = data.get("features", [])
        if features:
            coords = features[0]["geometry"]["coordinates"]
            return coords[0], coords[1], elapsed
        log.warning("Photon: no result for %r", address)
        return None, None, elapsed
    except Exception as exc:  # pylint: disable=broad-except
        elapsed = time.perf_counter() - t0
        log.error("Photon geocoding failed for %r: %s", address, exc)
        return None, None, elapsed


def geocode_nominatim(
    address: str,
    base_url: str,
    params: Dict,
    timeout: int,
) -> Tuple[Optional[float], Optional[float], float]:
    """
    Geocode *address* using Nominatim.

    Returns ``(lon, lat, elapsed_seconds)``.
    """
    q = {"q": address, **params}
    url = f"{base_url.rstrip('/')}/search?" + urllib.parse.urlencode(q)
    t0 = time.perf_counter()
    try:
        data = _http_get(url, timeout)
        elapsed = time.perf_counter() - t0
        if data:
            return float(data[0]["lon"]), float(data[0]["lat"]), elapsed
        log.warning("Nominatim: no result for %r", address)
        return None, None, elapsed
    except Exception as exc:  # pylint: disable=broad-except
        elapsed = time.perf_counter() - t0
        log.error("Nominatim geocoding failed for %r: %s", address, exc)
        return None, None, elapsed


def geocode(
    address: str,
    config: Dict,
) -> Tuple[Optional[float], Optional[float], float, str]:
    """
    Geocode *address* using the geocoder selected in *config*.

    Returns ``(lon, lat, elapsed_seconds, geocoder_name)``.
    """
    geocoder = config.get("geocoder", "photon").lower()
    timeout = config.get("timeout_seconds", 10)

    if geocoder == "nominatim":
        lon, lat, elapsed = geocode_nominatim(
            address,
            config.get("nominatim_base_url", DEFAULT_CONFIG["nominatim_base_url"]),
            config.get("nominatim", {}),
            timeout,
        )
        return lon, lat, elapsed, "nominatim"

    # default: photon
    lon, lat, elapsed = geocode_photon(
        address,
        config.get("photon_base_url", DEFAULT_CONFIG["photon_base_url"]),
        config.get("photon", {}),
        timeout,
    )
    return lon, lat, elapsed, "photon"


# ---------------------------------------------------------------------------
# Valhalla routing
# ---------------------------------------------------------------------------
def valhalla_route(
    origin_lon: float,
    origin_lat: float,
    dest_lon: float,
    dest_lat: float,
    base_url: str,
    valhalla_params: Dict,
    timeout: int,
) -> Tuple[Optional[float], float]:
    """
    Query Valhalla for a route and return ``(distance_meters, elapsed_seconds)``.

    Distance is ``None`` on failure.
    """
    body: Dict[str, Any] = {
        "locations": [
            {"lon": origin_lon, "lat": origin_lat},
            {"lon": dest_lon, "lat": dest_lat},
        ],
        "costing": valhalla_params.get("costing", "auto"),
        "directions_type": valhalla_params.get("directions_type", "none"),
        "units": valhalla_params.get("units", "km"),
    }
    costing_options = valhalla_params.get("costing_options")
    if costing_options:
        body["costing_options"] = costing_options

    url = f"{base_url.rstrip('/')}/route"
    t0 = time.perf_counter()
    try:
        data = _http_post(url, body, timeout)
        elapsed = time.perf_counter() - t0
        legs = data["trip"]["legs"]
        total_length: float = sum(leg["summary"]["length"] for leg in legs)
        # Valhalla returns length in the requested units; normalise to meters.
        units = valhalla_params.get("units", "km")
        if units == "miles":
            total_meters = total_length * 1609.344
        else:  # "km" (default) or anything else treated as km
            total_meters = total_length * 1000.0
        return total_meters, elapsed
    except Exception as exc:  # pylint: disable=broad-except
        elapsed = time.perf_counter() - t0
        log.error("Valhalla routing failed: %s", exc)
        return None, elapsed


# ---------------------------------------------------------------------------
# Optimization helpers
# ---------------------------------------------------------------------------
def _build_param_combinations(config: Dict) -> List[Dict]:
    """
    Return a list of Valhalla parameter dicts to try.

    When ``optimize.enabled`` is ``False``, returns a single entry with the
    base valhalla config.
    """
    opt = config.get("optimize", {})
    if not opt.get("enabled", False):
        return [config.get("valhalla", {})]

    base = copy.deepcopy(config.get("valhalla", {}))
    costings = opt.get("costing_variants") or [base.get("costing", "auto")]
    units_list = opt.get("units_variants") or [base.get("units", "km")]
    costing_opts_list = opt.get("costing_options_variants") or [{}]

    combos: List[Dict] = []
    for costing in costings:
        for units in units_list:
            for co in costing_opts_list:
                params = copy.deepcopy(base)
                params["costing"] = costing
                params["units"] = units
                params["costing_options"] = copy.deepcopy(co)
                combos.append(params)
    return combos


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------
def process_row(
    row: Dict[str, str],
    config: Dict,
    timing_totals: Dict[str, float],
) -> Dict[str, Any]:
    """
    Process a single CSV row and return a result dict.

    *timing_totals* is updated in-place with accumulated component times.
    """
    row_start = time.perf_counter()

    origin_raw = row.get("origin_ascertained") or row.get("origin_entry", "")
    dest_raw = row.get("destination_ascertained") or row.get("destination_entry", "")
    original_meters = float(row.get("distance", 0) or 0)

    origin_addr = parse_address(origin_raw)
    dest_addr = parse_address(dest_raw)

    # Geocode origin
    o_lon, o_lat, geo_o_t, geocoder_name = geocode(origin_addr, config)
    timing_totals[geocoder_name] = timing_totals.get(geocoder_name, 0.0) + geo_o_t

    # Geocode destination
    d_lon, d_lat, geo_d_t, geocoder_name_d = geocode(dest_addr, config)
    timing_totals[geocoder_name_d] = timing_totals.get(geocoder_name_d, 0.0) + geo_d_t

    if o_lon is None or d_lon is None:
        return {
            "id": row.get("id", ""),
            "origin_address": origin_addr,
            "destination_address": dest_addr,
            "original_meters": original_meters,
            "valhalla_meters": "",
            "difference_meters": "",
            "best_costing": "",
            "error": "geocoding failed",
        }

    # Valhalla – try parameter combinations
    valhalla_base = config.get("valhalla_base_url", DEFAULT_CONFIG["valhalla_base_url"])
    timeout = config.get("timeout_seconds", 10)
    combos = _build_param_combinations(config)

    best_meters: Optional[float] = None
    best_diff = float("inf")
    best_costing = ""
    valhalla_elapsed = 0.0

    for params in combos:
        meters, v_t = valhalla_route(
            o_lon, o_lat, d_lon, d_lat, valhalla_base, params, timeout
        )
        valhalla_elapsed += v_t
        if meters is None:
            continue
        diff = abs(meters - original_meters)
        if diff < best_diff:
            best_diff = diff
            best_meters = meters
            best_costing = params.get("costing", "")

    timing_totals["valhalla"] = timing_totals.get("valhalla", 0.0) + valhalla_elapsed

    row_elapsed = time.perf_counter() - row_start
    timing_totals["total"] = timing_totals.get("total", 0.0) + row_elapsed

    return {
        "id": row.get("id", ""),
        "origin_address": origin_addr,
        "destination_address": dest_addr,
        "original_meters": original_meters,
        "valhalla_meters": round(best_meters, 1) if best_meters is not None else "",
        "difference_meters": (
            round(best_meters - original_meters, 1)
            if best_meters is not None
            else ""
        ),
        "best_costing": best_costing,
        "error": "",
    }


# ---------------------------------------------------------------------------
# CSV I/O
# ---------------------------------------------------------------------------
def read_input_csv(path: str, encoding: str) -> List[Dict[str, str]]:
    rows = []
    with open(path, newline="", encoding=encoding) as fh:
        reader = csv.DictReader(fh, delimiter=";")
        for row in reader:
            rows.append(dict(row))
    return rows


OUTPUT_FIELDS = [
    "id",
    "origin_address",
    "destination_address",
    "original_meters",
    "valhalla_meters",
    "difference_meters",
    "best_costing",
    "error",
]


def write_output_csv(results: List[Dict], path: str, encoding: str) -> None:
    with open(path, "w", newline="", encoding=encoding) as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=OUTPUT_FIELDS,
            delimiter=";",
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(results)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Validate stored route distances against Valhalla.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--input", "-i", required=True, help="Path to input CSV file.")
    p.add_argument("--output", "-o", required=True, help="Path to output CSV file.")
    p.add_argument(
        "--config",
        "-c",
        default=os.path.join(os.path.dirname(__file__), "config.json"),
        help="Path to JSON config file (default: config.json next to this script).",
    )
    p.add_argument(
        "--geocoder",
        choices=["photon", "nominatim"],
        default=None,
        help="Override geocoder selection from config.",
    )
    p.add_argument(
        "--photon-url",
        default=None,
        help="Override Photon base URL.",
    )
    p.add_argument(
        "--nominatim-url",
        default=None,
        help="Override Nominatim base URL.",
    )
    p.add_argument(
        "--valhalla-url",
        default=None,
        help="Override Valhalla base URL.",
    )
    p.add_argument(
        "--no-optimize",
        action="store_true",
        help="Disable parameter-range optimization; use only base config.",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging.",
    )
    return p


def _deep_merge(base: Dict, override: Dict) -> Dict:
    """Recursively merge *override* into a copy of *base*."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_config(path: str) -> Dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            file_cfg = json.load(fh)
        # Deep-merge so that nested keys (valhalla, photon, optimize, …) are
        # updated individually rather than replaced wholesale.
        cfg = _deep_merge(cfg, file_cfg)
    else:
        log.warning("Config file not found: %s – using defaults.", path)
    return cfg


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    config = load_config(args.config)

    # CLI overrides
    if args.geocoder:
        config["geocoder"] = args.geocoder
    if args.photon_url:
        config["photon_base_url"] = args.photon_url
    if args.nominatim_url:
        config["nominatim_base_url"] = args.nominatim_url
    if args.valhalla_url:
        config["valhalla_base_url"] = args.valhalla_url
    if args.no_optimize:
        config.setdefault("optimize", {})["enabled"] = False

    log.info("Reading input: %s", args.input)
    rows = read_input_csv(args.input, config.get("input_encoding", "utf-8"))
    log.info("Rows to process: %d", len(rows))

    results: List[Dict] = []
    timing_totals: Dict[str, float] = {}

    for idx, row in enumerate(rows, start=1):
        log.info("[%d/%d] Processing row id=%s", idx, len(rows), row.get("id", "?"))
        result = process_row(row, config, timing_totals)
        results.append(result)
        log.info(
            "  origin=%-40s  dest=%-40s  orig=%8s m  valhalla=%8s m  diff=%s m",
            result["origin_address"][:40],
            result["destination_address"][:40],
            result["original_meters"],
            result["valhalla_meters"],
            result["difference_meters"],
        )

    log.info("Writing output: %s", args.output)
    write_output_csv(results, args.output, config.get("output_encoding", "utf-8"))

    # Timing summary
    log.info("--- Timing summary ---")
    geocoder_keys = [k for k in timing_totals if k not in ("valhalla", "total")]
    for k in sorted(geocoder_keys):
        log.info("  %-15s %.3f s", k, timing_totals[k])
    log.info("  %-15s %.3f s", "valhalla", timing_totals.get("valhalla", 0.0))
    log.info("  %-15s %.3f s", "total", timing_totals.get("total", 0.0))
    log.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
