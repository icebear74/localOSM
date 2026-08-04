#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${DATA_DIR:-/mnt/OSM}"

mkdir -p "${DATA_DIR}"
echo "[✓] Ensured OSM base directory exists: ${DATA_DIR}"
