#!/usr/bin/env bash
# Uses only already-built images and fixtures from their pinned upstream sources.
# OSM: pelias/openstreetmap test/vancouver_canada.osm.pbf (OSM contributors, ODbL).
# WOF: pelias/whosonfirst test/resources/france.json and its MIT-licensed test helper.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
python3 -c 'import yaml' || {
  echo "PyYAML is required by the existing manifest test suite." >&2
  exit 1
}
for image in \
  localosm/pelias-runtime:2026-10-21 \
  localosm/pelias-libpostal:2026-10-21 \
  localosm/pelias-elasticsearch:7.17.27; do
  docker image inspect "$image" >/dev/null || {
    echo "Build local images first: scripts/build-pelias-images.sh" >&2
    exit 1
  }
done
export PELIAS_REAL_SMOKE=1 PYTHONDONTWRITEBYTECODE=1
exec python3 -m unittest discover -s tests -p test_pelias_manifests.py -v
