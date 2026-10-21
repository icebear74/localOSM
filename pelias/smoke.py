#!/usr/bin/env python3
"""Feature-based checks shared by candidate and operational Pelias probes."""

import json
import math
import urllib.parse
import urllib.request


ENDPOINTS = frozenset(("search", "autocomplete", "search/structured", "reverse"))


def validate_cases(cases):
    if not isinstance(cases, list):
        raise ValueError("smoke must be a list of feature-based cases")
    covered = set()
    for case in cases:
        endpoint = case.get("endpoint")
        if endpoint not in ENDPOINTS or not isinstance(case.get("params"), dict):
            raise ValueError("each smoke case needs a supported endpoint and params")
        expect = case.get("expect", {})
        minimum = expect.get("min_features", 1) if isinstance(expect, dict) else None
        if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 1:
            raise ValueError("smoke checks must require at least one real feature")
        covered.add(endpoint)
    if covered != ENDPOINTS:
        raise ValueError("smoke must cover search, autocomplete, search/structured and reverse")


def run(base_url, cases, timeout=30):
    validate_cases(cases)
    results = []
    for case in cases:
        url = "%s/v1/%s?%s" % (
            base_url.rstrip("/"), case["endpoint"],
            urllib.parse.urlencode(case["params"], doseq=True),
        )
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = json.load(response)
        features = body.get("features", [])
        expect = case.get("expect", {})
        if body.get("type") != "FeatureCollection" or not isinstance(features, list):
            raise RuntimeError("smoke response is not a GeoJSON FeatureCollection: " + url)
        if len(features) < expect.get("min_features", 1):
            raise RuntimeError("smoke returned too few features: " + url)
        for feature in features:
            geometry = feature.get("geometry") or {}
            properties = feature.get("properties") or {}
            coordinates = geometry.get("coordinates")
            if (
                feature.get("type") != "Feature" or geometry.get("type") != "Point"
                or not isinstance(coordinates, list) or len(coordinates) != 2
                or not all(
                    isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)
                    for value in coordinates
                )
                or not -180 <= coordinates[0] <= 180 or not -90 <= coordinates[1] <= 90
            ):
                raise RuntimeError("smoke feature has no point geometry: " + url)
            if not properties.get("label") or not properties.get("layer"):
                raise RuntimeError("smoke feature has no label/layer: " + url)
        if expect.get("label_contains") and not any(
            expect["label_contains"].casefold() in f["properties"]["label"].casefold()
            for f in features
        ):
            raise RuntimeError("smoke label did not match: " + url)
        for key in ("layer", "source"):
            if expect.get(key) and not any(
                f["properties"].get(key) == expect[key] for f in features
            ):
                raise RuntimeError("smoke %s did not match: %s" % (key, url))
        results.append({"endpoint": case["endpoint"], "features": len(features)})
    return results
