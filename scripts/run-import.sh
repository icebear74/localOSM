#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_DIR="/mnt/data/OSM"
TEMP_DIR="${OSM_TEMP_DIR:-/mnt/data/OSM/TempDir}"
DEST="${TEMP_DIR}/import/planet.osm.pbf"
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
      echo "Usage: $0 --url <https://...osm.pbf> [--dest /mnt/data/OSM/TempDir/import/planet.osm.pbf]"
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
    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    fh.write(f"downloaded_at={timestamp}\n")
    fh.write(f"source={url}\n")
    fh.write(f"path={dest}\n")
PY

echo "Downloaded $(du -sh "$DEST" | cut -f1) → $DEST"

REQUEST_FILE="${BASE_DIR}/status/import-request.json"
cat > "$REQUEST_FILE" <<EOF
{
  "requested_at": "$(date -u '+%Y-%m-%dT%H:%M:%SZ')",
  "source": "${URL}",
  "pbf": "${DEST}"
}
EOF

echo ""
echo "Import request written to ${REQUEST_FILE}."
echo "Start the orchestrator pod if it is not already running; it will pick up the request automatically."
echo ""
