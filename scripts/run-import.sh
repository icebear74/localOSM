#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_DIR="/mnt/data/OSM"
DEST="${BASE_DIR}/import/planet.osm.pbf"
URL=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url)
      URL="$2"
      shift 2
      ;;
    --dest)
      DEST="$2"
      shift 2
      ;;
    -h|--help)
      echo "Usage: $0 --url <https://...osm.pbf> [--dest /mnt/data/OSM/import/planet.osm.pbf]"
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

if [ -z "$URL" ]; then
  echo "Missing --url" >&2
  exit 1
fi

mkdir -p "$(dirname "$DEST")"

PYTHON_BIN=""
if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
else
  echo "python not found" >&2
  exit 1
fi

"${PYTHON_BIN}" - <<'PY' "$URL" "$DEST"
import os
import sys
import urllib.request
import datetime

url = sys.argv[1]
dest = sys.argv[2]

print(f"Downloading OSM PBF from {url}")
urllib.request.urlretrieve(url, dest)

with open(dest + ".meta", "w", encoding="utf-8") as handle:
    handle.write(f"downloaded_at={datetime.datetime.utcnow().isoformat()}Z\n")
    handle.write(f"source={url}\n")
    handle.write(f"path={dest}\n")

print(f"Saved to {dest}")
PY

echo "Downloaded data."
if command -v kubectl >/dev/null 2>&1 && kubectl cluster-info >/dev/null 2>&1; then
  echo "Starting Valhalla import job..."
  kubectl -n osm delete job valhalla-import --ignore-not-found >/dev/null 2>&1 || true
  kubectl -n osm apply -f "${REPO_ROOT}/k8s/valhalla-import-job.yaml" >/dev/null
  echo "The import job has been started. Check logs with: kubectl -n osm logs job/valhalla-import"
else
  echo "kubectl is not available or the cluster is not reachable."
  echo "You can run the import manually later with: kubectl -n osm apply -f ${REPO_ROOT}/k8s/valhalla-import-job.yaml"
fi
