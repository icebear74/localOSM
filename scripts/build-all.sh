#!/usr/bin/env bash
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="${OSM_NAMESPACE:-osm}"
PARALLEL=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace|-n) NAMESPACE="$2"; shift 2 ;;
    --parallel) PARALLEL=true; shift ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done
services=(tileserver nominatim valhalla photon)
if [[ "$PARALLEL" == true ]]; then
  pids=()
  for svc in "${services[@]}"; do
    bash "$REPO_ROOT/scripts/build-${svc}.sh" --namespace "$NAMESPACE" &
    pids+=("$!")
  done
  rc=0
  for pid in "${pids[@]}"; do wait "$pid" || rc=$?; done
  exit "$rc"
fi
for svc in "${services[@]}"; do
  echo "=== Building $svc ==="
  bash "$REPO_ROOT/scripts/build-${svc}.sh" --namespace "$NAMESPACE"
done
