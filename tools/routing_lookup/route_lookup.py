#!/usr/bin/env python3
"""
route_lookup.py – Standalone routing distance validator.

Reads a semicolon-delimited CSV of origin/destination addresses, geocodes
them via Photon or Nominatim, queries Valhalla for the route distance, and
writes a result CSV comparing the original distance with the Valhalla distance.

Optionally tries multiple routing-parameter combinations (including float
value ranges) to find the variant closest to the stored original distance.

Usage
-----
  # positional – quickest way to call it:
  python route_lookup.py input.csv output.csv

  # named flags (same result):
  python route_lookup.py --input input.csv --output output.csv

  # use Nominatim instead of Photon for geocoding:
  python route_lookup.py input.csv output.csv --geocoder nominatim

  # use a custom config file:
  python route_lookup.py input.csv output.csv --config myconfig.json

Run  python route_lookup.py --help  for all options.
"""

import argparse
import copy
import concurrent.futures
import csv
import datetime
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
    "fallback_to_nominatim": True,
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
        "costing_variants": ["auto"],
        "units_variants": ["km"],
        "costing_options_variants": [
            {"auto": {"use_highways": {"from": 0.0, "to": 0.9, "step": 0.1}}},
        ],
    },
    "timeout_seconds": 10,
    "worker_threads": 10,
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

    # default: photon, optionally fallback to Nominatim
    lon, lat, elapsed = geocode_photon(
        address,
        config.get("photon_base_url", DEFAULT_CONFIG["photon_base_url"]),
        config.get("photon", {}),
        timeout,
    )
    if lon is not None and lat is not None:
        return lon, lat, elapsed, "photon"

    if config.get("fallback_to_nominatim", True):
        log.info("Photon failed for %r, falling back to Nominatim.", address)
        n_lon, n_lat, n_elapsed = geocode_nominatim(
            address,
            config.get("nominatim_base_url", DEFAULT_CONFIG["nominatim_base_url"]),
            config.get("nominatim", {}),
            timeout,
        )
        return n_lon, n_lat, elapsed + n_elapsed, "photon+nominatim"

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
) -> Tuple[Optional[float], float, Optional[float]]:
    """
    Query Valhalla for a route and return
    ``(distance_meters, elapsed_seconds, routing_cost)``.

    Distance and routing_cost are ``None`` on failure.
    routing_cost comes from ``trip.summary.cost`` when present.
    """
    body: Dict[str, Any] = {
        "locations": [
            {"lon": origin_lon, "lat": origin_lat},
            {"lon": dest_lon, "lat": dest_lat},
        ],
    }
    for key in ("costing", "directions_type", "units"):
        value = valhalla_params.get(key)
        if value not in (None, ""):
            body[key] = value
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
        # Routing cost: prefer trip-level summary, fall back to sum of legs.
        trip_summary = data["trip"].get("summary", {})
        routing_cost: Optional[float] = trip_summary.get("cost")
        if routing_cost is None:
            leg_costs = [leg["summary"].get("cost") for leg in legs]
            if all(c is not None for c in leg_costs):
                routing_cost = sum(leg_costs)  # type: ignore[arg-type]
        return total_meters, elapsed, routing_cost
    except Exception as exc:  # pylint: disable=broad-except
        elapsed = time.perf_counter() - t0
        log.error("Valhalla routing failed: %s", exc)
        return None, elapsed, None


def _expand_range(value: Any) -> List[Any]:
    """
    If *value* is a range dict ``{"from": f, "to": t, "step": s}`` return a
    list of float values from f to t (inclusive) with step s.
    Otherwise return ``[value]``.
    """
    if isinstance(value, dict) and "from" in value and "to" in value:
        start = float(value["from"])
        stop = float(value["to"])
        step = float(value.get("step", 0.1))
        if step <= 0 or stop < start:
            return [start]
        result: List[float] = []
        i = 0
        while True:
            current = start + i * step
            if current > stop + 1e-9:
                break
            rounded = round(current, 10)
            if not result or abs(result[-1] - rounded) > 1e-9:
                result.append(rounded)
            i += 1
        if not result:
            result = [start]
        if result[-1] < stop - 1e-9:
            result.append(round(stop, 10))
        return result
    return [value]


def _expand_costing_options(co_template: Dict) -> List[Dict]:
    """
    Expand a costing-options dict that may contain range values into a list of
    fully resolved dicts.

    Example input::

        {"auto": {"use_highways": {"from": 0.0, "to": 1.0, "step": 0.5},
                  "use_tolls": 0.5}}

    Yields three dicts where ``use_highways`` is 0.0, 0.5, 1.0.
    """
    # Flatten: collect (profile_key, option_key, expanded_values) triples
    expanded: List[Dict] = [{}]
    for profile, opts in co_template.items():
        # Skip documentation-only keys
        if profile.startswith("_"):
            continue
        if not isinstance(opts, dict):
            # Scalar value – keep as-is
            for d in expanded:
                d[profile] = opts
            continue
        for opt_key, opt_val in opts.items():
            if opt_key.startswith("_"):
                continue
            values = _expand_range(opt_val)
            new_expanded: List[Dict] = []
            for existing in expanded:
                for v in values:
                    entry = copy.deepcopy(existing)
                    entry.setdefault(profile, {})[opt_key] = v
                    new_expanded.append(entry)
            expanded = new_expanded
    return expanded


# ---------------------------------------------------------------------------
# Optimization helpers
# ---------------------------------------------------------------------------
def _build_param_combinations(config: Dict) -> List[Dict]:
    """
    Return a list of Valhalla parameter dicts to try.

    When ``optimize.enabled`` is ``False``, returns a single entry with the
    base valhalla config.

    Each entry in ``costing_options_variants`` may contain range dicts of the
    form ``{"from": 0.0, "to": 1.0, "step": 0.25}`` which are expanded into
    individual float values before building the cross-product of combinations.
    """
    opt = config.get("optimize", {})
    if not opt.get("enabled", False):
        return [config.get("valhalla", {})]

    base = copy.deepcopy(config.get("valhalla", {}))
    costings = opt.get("costing_variants") or [base.get("costing", "auto")]
    units_list = opt.get("units_variants") or [base.get("units", "km")]
    raw_co_list = opt.get("costing_options_variants") or [{}]

    # Expand each costing-options template (may contain range dicts)
    expanded_co_list: List[Dict] = []
    for co_template in raw_co_list:
        expanded_co_list.extend(_expand_costing_options(co_template))

    combos: List[Dict] = []
    for costing in costings:
        for units in units_list:
            for co in expanded_co_list:
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

    if o_lon is None or o_lat is None or d_lon is None or d_lat is None:
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
    best_params: Dict[str, Any] = {}
    best_cost: Optional[float] = None
    min_meters: Optional[float] = None
    max_meters: Optional[float] = None
    valhalla_elapsed = 0.0

    for combo_idx, params in enumerate(combos, start=1):
        if len(combos) > 1:
            log.info("route %s step %d/%d", row.get("id", "?"), combo_idx, len(combos))
        meters, v_t, cost = valhalla_route(
            o_lon, o_lat, d_lon, d_lat, valhalla_base, params, timeout
        )
        valhalla_elapsed += v_t
        if meters is None:
            continue
        if min_meters is None or meters < min_meters:
            min_meters = meters
        if max_meters is None or meters > max_meters:
            max_meters = meters
        diff = abs(meters - original_meters)
        if diff < best_diff:
            best_diff = diff
            best_meters = meters
            best_params = copy.deepcopy(params)
            best_cost = cost

    timing_totals["valhalla"] = timing_totals.get("valhalla", 0.0) + valhalla_elapsed

    row_elapsed = time.perf_counter() - row_start
    timing_totals["total"] = timing_totals.get("total", 0.0) + row_elapsed

    return {
        "id": row.get("id", ""),
        "origin_address": origin_addr,
        "destination_address": dest_addr,
        "original_meters": original_meters,
        "valhalla_meters": round(best_meters, 1) if best_meters is not None else "",
        "valhalla_km_min": round(min_meters / 1000.0, 3) if min_meters is not None else "",
        "valhalla_km_max": round(max_meters / 1000.0, 3) if max_meters is not None else "",
        "routing_cost": round(best_cost, 3) if best_cost is not None else "",
        "difference_meters": (
            round(best_meters - original_meters, 1)
            if best_meters is not None
            else ""
        ),
        "best_costing": (
            json.dumps(
                {
                    "costing": best_params.get("costing", ""),
                    "units": best_params.get("units", ""),
                    "costing_options": best_params.get("costing_options", {}),
                },
                ensure_ascii=False,
            )
            if best_meters is not None
            else ""
        ),
        "error": "",
    }


def process_global_optimization(
    rows: List[Dict[str, str]],
    config: Dict,
    timing_totals: Dict[str, float],
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]], Dict[str, Any], List[Dict[str, Any]]]:
    """Evaluate each parameter set across all rows and keep the globally best one.

    Returns ``(results, best_params, best_metrics, prepared_rows)`` so the caller
    can reuse the already-geocoded row data for the opt-output pass.
    """
    prepared_rows: List[Dict[str, Any]] = []
    row_start_total = time.perf_counter()

    for idx, row in enumerate(rows, start=1):
        log.info("[prep %d/%d] Geocoding row id=%s", idx, len(rows), row.get("id", "?"))
        origin_raw = row.get("origin_ascertained") or row.get("origin_entry", "")
        dest_raw = row.get("destination_ascertained") or row.get("destination_entry", "")
        original_meters = float(row.get("distance", 0) or 0)
        origin_addr = parse_address(origin_raw)
        dest_addr = parse_address(dest_raw)

        o_lon, o_lat, geo_o_t, geocoder_name = geocode(origin_addr, config)
        timing_totals[geocoder_name] = timing_totals.get(geocoder_name, 0.0) + geo_o_t

        d_lon, d_lat, geo_d_t, geocoder_name_d = geocode(dest_addr, config)
        timing_totals[geocoder_name_d] = timing_totals.get(geocoder_name_d, 0.0) + geo_d_t

        prepared_rows.append(
            {
                "row": row,
                "id": row.get("id", ""),
                "origin_address": origin_addr,
                "destination_address": dest_addr,
                "original_meters": original_meters,
                "o_lon": o_lon,
                "o_lat": o_lat,
                "d_lon": d_lon,
                "d_lat": d_lat,
            }
        )

    combos = _build_param_combinations(config)
    valhalla_base = config.get("valhalla_base_url", DEFAULT_CONFIG["valhalla_base_url"])
    timeout = config.get("timeout_seconds", 10)
    worker_threads = max(1, int(config.get("worker_threads", 10)))

    best_combo_params: Optional[Dict[str, Any]] = None
    best_combo_pct = float("inf")
    best_combo_row_meters: Dict[int, Optional[float]] = {}
    best_combo_row_costs: Dict[int, Optional[float]] = {}
    best_combo_metrics: Dict[str, Any] = {}
    # Per-row min/max across all combos
    row_min_meters: Dict[int, Optional[float]] = {}
    row_max_meters: Dict[int, Optional[float]] = {}

    for combo_idx, params in enumerate(combos, start=1):
        combo_start = time.perf_counter()
        sum_original = 0.0
        sum_abs_diff = 0.0
        sum_signed_diff = 0.0
        routed_count = 0
        failed_routing = 0
        geocode_failed = 0
        row_meters: Dict[int, Optional[float]] = {}
        row_costs: Dict[int, Optional[float]] = {}
        valhalla_elapsed_combo = 0.0

        runnable_rows: List[Tuple[int, Dict[str, Any]]] = []
        for row_idx, p in enumerate(prepared_rows):
            if p["o_lon"] is None or p["o_lat"] is None or p["d_lon"] is None or p["d_lat"] is None:
                geocode_failed += 1
                row_meters[row_idx] = None
                continue
            runnable_rows.append((row_idx, p))

        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_threads) as executor:
            futures = {
                executor.submit(
                    valhalla_route,
                    p["o_lon"],
                    p["o_lat"],
                    p["d_lon"],
                    p["d_lat"],
                    valhalla_base,
                    params,
                    timeout,
                ): row_idx
                for row_idx, p in runnable_rows
            }

            for future in concurrent.futures.as_completed(futures):
                row_idx = futures[future]
                log.info("route %d/%d step %d/%d", row_idx + 1, len(prepared_rows), combo_idx, len(combos))
                try:
                    meters, v_t, cost = future.result()
                except Exception as exc:  # pylint: disable=broad-except
                    log.error("Valhalla worker failed for row %d: %s", row_idx + 1, exc)
                    meters, v_t, cost = None, 0.0, None
                valhalla_elapsed_combo += v_t

                if meters is None:
                    failed_routing += 1
                    row_meters[row_idx] = None
                    row_costs[row_idx] = None
                    continue

                # Track global min/max per row
                prev_min = row_min_meters.get(row_idx)
                prev_max = row_max_meters.get(row_idx)
                row_min_meters[row_idx] = meters if prev_min is None else min(prev_min, meters)
                row_max_meters[row_idx] = meters if prev_max is None else max(prev_max, meters)

                original = prepared_rows[row_idx]["original_meters"]
                diff = meters - original
                sum_original += original
                sum_abs_diff += abs(diff)
                sum_signed_diff += diff
                routed_count += 1
                row_meters[row_idx] = meters
                row_costs[row_idx] = cost
        timing_totals["valhalla"] = timing_totals.get("valhalla", 0.0) + valhalla_elapsed_combo

        abs_pct = (sum_abs_diff / sum_original * 100.0) if sum_original else float("inf")
        signed_pct = (sum_signed_diff / sum_original * 100.0) if sum_original else float("inf")
        combo_elapsed = time.perf_counter() - combo_start
        log.info(
            (
                "[combo %d/%d] routed=%d failed=%d geocode_failed=%d "
                "abs_diff=%.1f m abs_diff_pct=%.4f%% signed_diff_pct=%.4f%% "
                "elapsed=%.2fs threads=%d best_so_far=%.4f%%"
            ),
            combo_idx,
            len(combos),
            routed_count,
            failed_routing,
            geocode_failed,
            sum_abs_diff,
            abs_pct,
            signed_pct,
            combo_elapsed,
            worker_threads,
            min(best_combo_pct, abs_pct),
        )

        if best_combo_params is None or abs_pct < best_combo_pct:
            best_combo_pct = abs_pct
            best_combo_params = copy.deepcopy(params)
            best_combo_row_meters = row_meters
            best_combo_row_costs = row_costs
            best_combo_metrics = {
                "combo_index": combo_idx,
                "combo_count": len(combos),
                "routed_count": routed_count,
                "failed_routing": failed_routing,
                "geocode_failed": geocode_failed,
                "sum_original_meters": round(sum_original, 3),
                "sum_abs_diff_meters": round(sum_abs_diff, 3),
                "sum_signed_diff_meters": round(sum_signed_diff, 3),
                "abs_diff_percent": round(abs_pct, 6),
                "signed_diff_percent": round(signed_pct, 6),
                "elapsed_seconds": round(combo_elapsed, 3),
            }

    results: List[Dict[str, Any]] = []
    for row_idx, p in enumerate(prepared_rows):
        best_meters = best_combo_row_meters.get(row_idx)
        best_cost = best_combo_row_costs.get(row_idx)
        min_m = row_min_meters.get(row_idx)
        max_m = row_max_meters.get(row_idx)
        error = ""
        if p["o_lon"] is None or p["o_lat"] is None or p["d_lon"] is None or p["d_lat"] is None:
            error = "geocoding failed"
        elif best_meters is None:
            error = "routing failed"
        results.append(
            {
                "id": p["id"],
                "origin_address": p["origin_address"],
                "destination_address": p["destination_address"],
                "original_meters": p["original_meters"],
                "valhalla_meters": round(best_meters, 1) if best_meters is not None else "",
                "valhalla_km_min": round(min_m / 1000.0, 3) if min_m is not None else "",
                "valhalla_km_max": round(max_m / 1000.0, 3) if max_m is not None else "",
                "routing_cost": round(best_cost, 3) if best_cost is not None else "",
                "difference_meters": (
                    round(best_meters - p["original_meters"], 1)
                    if best_meters is not None
                    else ""
                ),
                "best_costing": (
                    json.dumps(
                        {
                            "costing": (best_combo_params or {}).get("costing", ""),
                            "units": (best_combo_params or {}).get("units", ""),
                            "costing_options": (best_combo_params or {}).get("costing_options", {}),
                        },
                        ensure_ascii=False,
                    )
                    if best_meters is not None
                    else ""
                ),
                "error": error,
            }
        )

    timing_totals["total"] = timing_totals.get("total", 0.0) + (time.perf_counter() - row_start_total)
    return results, best_combo_params, best_combo_metrics, prepared_rows


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
    "valhalla_km_min",
    "valhalla_km_max",
    "routing_cost",
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


def _opt_output_path(output_path: str) -> str:
    """Derive the '-opt' companion output path from the main output path.

    ``results.csv`` → ``results-opt.csv``
    ``results``     → ``results-opt``
    """
    base, ext = os.path.splitext(output_path)
    return f"{base}-opt{ext}"


def run_with_fixed_params(
    prepared_rows: List[Dict[str, Any]],
    params: Dict[str, Any],
    valhalla_base: str,
    timeout: int,
    worker_threads: int,
    timing_totals: Dict[str, float],
) -> List[Dict[str, Any]]:
    """Route all *prepared_rows* with a single fixed *params* set.

    Returns a result list suitable for writing to a CSV via ``write_output_csv``.
    *prepared_rows* must be the list produced by the geocoding phase of
    ``process_global_optimization`` (each entry has the standard geocoded fields).
    """
    runnable: List[Tuple[int, Dict[str, Any]]] = [
        (i, p) for i, p in enumerate(prepared_rows)
        if p["o_lon"] is not None and p["d_lon"] is not None
    ]
    row_meters: Dict[int, Optional[float]] = {}
    row_costs: Dict[int, Optional[float]] = {}
    valhalla_elapsed = 0.0

    best_costing_json = json.dumps(
        {
            "costing": params.get("costing", ""),
            "units": params.get("units", ""),
            "costing_options": params.get("costing_options", {}),
        },
        ensure_ascii=False,
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_threads) as executor:
        futures = {
            executor.submit(
                valhalla_route,
                p["o_lon"], p["o_lat"], p["d_lon"], p["d_lat"],
                valhalla_base, params, timeout,
            ): row_idx
            for row_idx, p in runnable
        }
        for future in concurrent.futures.as_completed(futures):
            row_idx = futures[future]
            try:
                meters, v_t, cost = future.result()
            except Exception as exc:  # pylint: disable=broad-except
                log.error("Valhalla worker (opt pass) failed for row %d: %s", row_idx + 1, exc)
                meters, v_t, cost = None, 0.0, None
            valhalla_elapsed += v_t
            row_meters[row_idx] = meters
            row_costs[row_idx] = cost

    timing_totals["valhalla"] = timing_totals.get("valhalla", 0.0) + valhalla_elapsed

    results: List[Dict[str, Any]] = []
    for row_idx, p in enumerate(prepared_rows):
        meters = row_meters.get(row_idx)
        cost = row_costs.get(row_idx)
        if p["o_lon"] is None or p["d_lon"] is None:
            error = "geocoding failed"
        elif meters is None:
            error = "routing failed"
        else:
            error = ""
        results.append(
            {
                "id": p["id"],
                "origin_address": p["origin_address"],
                "destination_address": p["destination_address"],
                "original_meters": p["original_meters"],
                "valhalla_meters": round(meters, 1) if meters is not None else "",
                "valhalla_km_min": "",
                "valhalla_km_max": "",
                "routing_cost": round(cost, 3) if cost is not None else "",
                "difference_meters": (
                    round(meters - p["original_meters"], 1) if meters is not None else ""
                ),
                "best_costing": best_costing_json if meters is not None else "",
                "error": error,
            }
        )
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Validate stored route distances against Valhalla.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    # Positional shortcuts: route_lookup.py input.csv output.csv
    p.add_argument(
        "pos_input",
        nargs="?",
        metavar="INPUT",
        help="Input CSV file (positional shortcut for --input).",
    )
    p.add_argument(
        "pos_output",
        nargs="?",
        metavar="OUTPUT",
        help="Output CSV file (positional shortcut for --output).",
    )
    p.add_argument("--input", "-i", default=None, help="Path to input CSV file.")
    p.add_argument("--output", "-o", default=None, help="Path to output CSV file.")
    p.add_argument(
        "--config",
        "-c",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"),
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
        "--optimize-global",
        action="store_true",
        help="Evaluate each parameter set across all rows and choose one best overall set.",
    )
    p.add_argument(
        "--optimal-config-out",
        default=None,
        help="Path to write best overall parameter set (used with --optimize-global).",
    )
    p.add_argument(
        "--threads",
        type=int,
        default=None,
        help="Worker threads for global optimization routing requests.",
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


def _collect_cli_overrides(args: argparse.Namespace) -> Dict[str, Any]:
    overrides: Dict[str, Any] = {}
    for key in (
        "geocoder",
        "photon_url",
        "nominatim_url",
        "valhalla_url",
        "threads",
        "optimal_config_out",
    ):
        value = getattr(args, key, None)
        if value is not None:
            overrides[key] = value
    if getattr(args, "no_optimize", False):
        overrides["no_optimize"] = True
    if getattr(args, "optimize_global", False):
        overrides["optimize_global"] = True
    return overrides


def save_optimal_config(
    path: str,
    valhalla_params: Dict[str, Any],
    metrics: Dict[str, Any],
    config: Dict[str, Any],
    cli_overrides: Dict[str, Any],
) -> None:
    payload = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "valhalla": valhalla_params,
        "passed_parameters": {
            "service_urls": {
                "photon_base_url": config.get("photon_base_url"),
                "nominatim_base_url": config.get("nominatim_base_url"),
                "valhalla_base_url": config.get("valhalla_base_url"),
            },
            "geocoder": config.get("geocoder"),
            "fallback_to_nominatim": config.get("fallback_to_nominatim"),
            "timeout_seconds": config.get("timeout_seconds"),
            "worker_threads": config.get("worker_threads"),
            "valhalla": valhalla_params,
            "optimize": config.get("optimize", {}),
        },
        "effective_config": config,
        "cli_overrides": cli_overrides,
        "metrics": metrics,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    # Resolve input/output: positional args take precedence if named args absent
    input_path = args.input or args.pos_input
    output_path = args.output or args.pos_output

    if not input_path:
        parser.error("input CSV is required (positional or --input)")
    if not output_path:
        parser.error("output CSV is required (positional or --output)")

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    config = load_config(args.config)
    cli_overrides = _collect_cli_overrides(args)

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
    if args.threads is not None:
        config["worker_threads"] = max(1, int(args.threads))

    log.info("Reading input: %s", input_path)
    rows = read_input_csv(input_path, config.get("input_encoding", "utf-8"))
    log.info("Rows to process: %d", len(rows))

    results: List[Dict] = []
    timing_totals: Dict[str, float] = {}
    prepared_rows_cache: List[Dict[str, Any]] = []
    best_params_global: Optional[Dict[str, Any]] = None

    if args.optimize_global:
        log.info("Global optimization mode enabled: evaluating each parameter set over all rows.")
        results, best_params_global, best_metrics, prepared_rows_cache = process_global_optimization(
            rows, config, timing_totals
        )
        if best_params_global is not None:
            out_path = args.optimal_config_out or os.path.join(os.getcwd(), "optimal.json")
            save_optimal_config(out_path, best_params_global, best_metrics, config, cli_overrides)
            log.info(
                "Best overall combination: [%d/%d] abs_diff_pct=%.4f%% -> %s",
                best_metrics.get("combo_index", 0),
                best_metrics.get("combo_count", 0),
                best_metrics.get("abs_diff_percent", float("inf")),
                out_path,
            )
    else:
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

    log.info("Writing output: %s", output_path)
    write_output_csv(results, output_path, config.get("output_encoding", "utf-8"))

    # Write the -opt companion file when a globally best parameter set is available.
    if best_params_global is not None and prepared_rows_cache:
        opt_path = _opt_output_path(output_path)
        log.info(
            "Writing opt output with best params (costing_options=%s): %s",
            best_params_global.get("costing_options", {}),
            opt_path,
        )
        valhalla_base = config.get("valhalla_base_url", DEFAULT_CONFIG["valhalla_base_url"])
        timeout = config.get("timeout_seconds", 10)
        worker_threads = max(1, int(config.get("worker_threads", 10)))
        opt_results = run_with_fixed_params(
            prepared_rows_cache,
            best_params_global,
            valhalla_base,
            timeout,
            worker_threads,
            timing_totals,
        )
        write_output_csv(opt_results, opt_path, config.get("output_encoding", "utf-8"))

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
