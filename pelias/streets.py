#!/usr/bin/env python3
"""Stream OSM street geometry to Pelias polyline6/NUL-separated rows.

Unlike upstream's memory-bound `pbf streets` wrapper, osmium's location index
is disk-backed. The format is specified in pelias/polylines README.md.
"""

import argparse
import json
import math
from pathlib import Path
import subprocess
import tempfile


def encode_polyline(coordinates):
    previous = (0, 0)
    encoded = []
    for longitude, latitude, *_ in coordinates:
        if not (math.isfinite(longitude) and math.isfinite(latitude)
                and -180 <= longitude <= 180 and -90 <= latitude <= 90):
            raise ValueError("invalid street coordinate")
        current = tuple(math.floor(value * 1_000_000 + 0.5)
                        for value in (latitude, longitude))
        for value, old in zip(current, previous):
            delta = value - old
            number = ~(delta << 1) if delta < 0 else delta << 1
            while number >= 32:
                encoded.append(chr((32 | (number & 31)) + 63))
                number >>= 5
            encoded.append(chr(number + 63))
        previous = current
    return "".join(encoded)


def street_rows(feature):
    tags = feature.get("properties") or {}
    if not tags.get("highway"):
        return
    names = []
    for key, value in tags.items():
        if key in ("name", "alt_name", "official_name", "short_name") or key.startswith("name:"):
            for name in str(value).split(";"):
                clean = " ".join(name.replace("\0", " ").split())
                if clean and clean not in names:
                    names.append(clean)
    if not names:
        return
    geometry = feature.get("geometry") or {}
    if geometry.get("type") == "LineString":
        lines = [geometry["coordinates"]]
    elif geometry.get("type") == "MultiLineString":
        lines = geometry["coordinates"]
    else:
        return
    for coordinates in lines:
        if len(coordinates) >= 2:
            yield ("\0".join([encode_polyline(coordinates)] + names) + "\n").encode("utf-8")


def extract(source, destination, scratch):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    pending = destination.with_name(destination.name + ".next")
    count = 0
    try:
        with tempfile.TemporaryDirectory(prefix="streets-", dir=scratch) as temporary:
            configuration = Path(temporary) / "export.json"
            configuration.write_text(json.dumps({"linear_tags": True, "area_tags": False}))
            command = [
                "osmium", "export", str(source), "--geometry-types=linestring",
                "--output-format=geojsonseq", "--stop-on-error",
                "--index-type=sparse_file_array," + str(Path(temporary) / "locations.idx"),
                "--config=" + str(configuration),
            ]
            process = subprocess.Popen(command, stdout=subprocess.PIPE)
            try:
                with pending.open("wb") as output:
                    for line in process.stdout:
                        record = line.lstrip(b"\x1e").strip()
                        if not record:
                            continue
                        feature = json.loads(record)
                        for row in street_rows(feature):
                            output.write(row)
                            count += 1
                if process.wait():
                    raise RuntimeError("osmium street export failed")
                if not count:
                    raise RuntimeError("source contains no named highway geometry")
            finally:
                process.stdout.close()
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
        pending.replace(destination)
    finally:
        pending.unlink(missing_ok=True)
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("destination")
    parser.add_argument("--scratch", required=True)
    args = parser.parse_args()
    print(json.dumps({"street_rows": extract(args.source, args.destination, args.scratch)}))


if __name__ == "__main__":
    main()
