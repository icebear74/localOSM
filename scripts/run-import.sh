#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_DIR="${OSM_DATA_DIR:-/mnt/OSM}"
TEMP_DIR="${OSM_TEMP_DIR:-/mnt/OSM/TempDir}"
DEST="${TEMP_DIR}/import/planet.osm.pbf"
URL=""
PELIAS_ONLY=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pelias-only)
      PELIAS_ONLY=true
      shift
      ;;
    --url)
      URL="$2"
      shift 2
      ;;
    --dest)
      DEST="$2"
      shift 2
      ;;
    -h|--help)
      echo "Usage: $0 --url <https://...osm.pbf> [--dest path] | --pelias-only"
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

if [ "${PELIAS_ONLY}" = true ]; then
  [ -z "${URL}" ] || { echo "--pelias-only uses the existing /planet/europa.osm.pbf; do not specify --url" >&2; exit 1; }
  mkdir -p "${BASE_DIR}/status"
  python3 - "${BASE_DIR}/status" <<'PY'
import datetime, fcntl, json, os, sys
root = sys.argv[1]
with open(os.path.join(root, "import-request.lock"), "a+") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    path = os.path.join(root, "import-request-queue.json")
    try:
        with open(path) as f:
            queue = json.load(f)
    except FileNotFoundError:
        queue = []
    if any(r.get("steps") == ["pelias"] and r.get("action", "build") == "build" for r in queue):
        raise SystemExit("A Pelias build is already queued.")
    queue.append({"requested_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  "steps": ["pelias"], "countries": [], "count": 0,
                  "merge_requested": False, "auto_promote": False})
    with open(path + ".new", "w") as f:
        json.dump(queue, f)
    os.replace(path + ".new", path)
PY
  echo "Queued Pelias-only build using the existing published PBF (no download or merge)."
  exit 0
fi

if [ -z "$URL" ]; then
  echo "Missing --url" >&2
  exit 1
fi

mkdir -p "$(dirname "$DEST")" "${BASE_DIR}/status"

echo "=== localOSM – Download OSM PBF ==="
echo "Source : $URL"
echo "Dest   : $DEST"
echo ""

if command -v wget >/dev/null 2>&1; then
  wget --progress=bar:force:noscroll -O "$DEST" "$URL"
else
  python3 - "$URL" "$DEST" <<'PY'
import sys, urllib.request
url, dest = sys.argv[1], sys.argv[2]
def _progress(block_count, block_size, total_size):
    downloaded = block_count * block_size
    if total_size > 0:
        pct = min(100, downloaded * 100 // total_size)
        mb_done = downloaded / (1024 * 1024)
        mb_total = total_size / (1024 * 1024)
        bar = '#' * (pct // 5) + '.' * (20 - pct // 5)
        print(f"\r  [{bar}] {pct:3d}%  {mb_done:.1f}/{mb_total:.1f} MB", end='', flush=True)
    else:
        print(f"\r  {downloaded / (1024 * 1024):.1f} MB downloaded", end='', flush=True)
print(f"Downloading {url}")
urllib.request.urlretrieve(url, dest, _progress)
print()
PY
fi

python3 - "$URL" "$DEST" <<'PY'
import datetime, sys
url, dest = sys.argv[1], sys.argv[2]
with open(dest + '.meta', 'w', encoding='utf-8') as fh:
    timestamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    fh.write(f"downloaded_at={timestamp}\n")
    fh.write(f"source={url}\n")
    fh.write(f"path={dest}\n")
PY

echo "Downloaded $(du -sh "$DEST" | cut -f1) → $DEST"

REQUEST_FILE="${BASE_DIR}/status/import-request.json"
cat > "$REQUEST_FILE" <<EOF
{
  "requested_at": "$(date '+%Y-%m-%dT%H:%M:%S%z')",
  "source": "${URL}",
  "pbf": "${DEST}"
}
EOF

echo ""
echo "Import request written to ${REQUEST_FILE}."
echo "Start the orchestrator pod if it is not already running; it will pick up the request automatically."
echo ""
