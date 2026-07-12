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

write_metadata_file() {
  local url="$1"
  local dest="$2"
  local python_bin=""
  if command -v python3 >/dev/null 2>&1; then
    python_bin="python3"
  elif command -v python >/dev/null 2>&1; then
    python_bin="python"
  else
    echo "Neither python3 nor python found" >&2
    return 1
  fi

  "$python_bin" - "$url" "$dest" <<'PY'
import datetime
import sys

url, dest = sys.argv[1], sys.argv[2]
timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds")
with open(dest + ".meta", "w", encoding="utf-8") as fh:
    fh.write(f"downloaded_at={timestamp}\n")
    fh.write(f"source={url}\n")
    fh.write(f"path={dest}\n")
PY
}

mkdir -p "$(dirname "$DEST")"

echo "=== localOSM – Download OSM PBF ==="
echo "Source : $URL"
echo "Dest   : $DEST"
echo ""

# Prefer wget for progress display, fall back to Python
if command -v wget >/dev/null 2>&1; then
  wget --progress=bar:force:noscroll -O "$DEST" "$URL"
else
  PYTHON_BIN=""
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
  else
    echo "Neither wget nor python found" >&2
    exit 1
  fi

  "${PYTHON_BIN}" - <<'PY' "$URL" "$DEST"
import os
import sys
import urllib.request

url  = sys.argv[1]
dest = sys.argv[2]

def _progress(block_count, block_size, total_size):
    downloaded = block_count * block_size
    if total_size > 0:
        pct = min(100, downloaded * 100 // total_size)
        mb_done  = downloaded / (1024 * 1024)
        mb_total = total_size / (1024 * 1024)
        bar = "#" * (pct // 5) + "." * (20 - pct // 5)
        print(f"\r  [{bar}] {pct:3d}%  {mb_done:.1f}/{mb_total:.1f} MB", end="", flush=True)
    else:
        mb = downloaded / (1024 * 1024)
        print(f"\r  {mb:.1f} MB downloaded", end="", flush=True)

print(f"Downloading {url}")
urllib.request.urlretrieve(url, dest, _progress)
print()
PY
fi

# Write metadata file next to the PBF
write_metadata_file "$URL" "$DEST"

PBF_SIZE=$(du -sh "$DEST" | cut -f1)
echo "Downloaded ${PBF_SIZE} → ${DEST}"

if command -v kubectl >/dev/null 2>&1 && kubectl cluster-info >/dev/null 2>&1; then
  echo ""
  echo "=== Starting Valhalla import job ==="
  kubectl -n osm apply -f "${REPO_ROOT}/k8s/valhalla-import-config.yaml" >/dev/null
  kubectl -n osm delete job valhalla-import --ignore-not-found >/dev/null 2>&1 || true
  kubectl -n osm apply -f "${REPO_ROOT}/k8s/valhalla-import-job.yaml" >/dev/null
  echo "Import job started. Monitor progress with:"
  echo "  kubectl -n osm logs -f job/valhalla-import"
  echo ""
  echo "Check overall status with:"
  echo "  kubectl -n osm get pods"
  echo "  http://<node-ip>:30083/    (Status-Dashboard)"
else
  echo ""
  echo "kubectl not available or cluster unreachable."
  echo "Start the import job manually later with:"
  echo "  kubectl -n osm apply -f ${REPO_ROOT}/k8s/valhalla-import-config.yaml"
  echo "  kubectl -n osm apply -f ${REPO_ROOT}/k8s/valhalla-import-job.yaml"
fi
