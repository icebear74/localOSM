#!/usr/bin/env bash
# switch-tileserver.sh
# Manually switch the TileServer main service to a specific colour (blue|green).
# Usage: bash scripts/switch-tileserver.sh blue|green [--namespace <ns>]
set -euo pipefail

TARGET_COLOR="${1:-}"
NAMESPACE="${TILESERVER_NAMESPACE:-osm}"

shift 1 2>/dev/null || true
while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace|-n) NAMESPACE="$2"; shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

if [[ "$TARGET_COLOR" != "blue" && "$TARGET_COLOR" != "green" ]]; then
  echo "Usage: $0 blue|green [--namespace <ns>]"
  exit 1
fi

if [[ "$TARGET_COLOR" == "blue" ]]; then
  OLD_COLOR=green
else
  OLD_COLOR=blue
fi

echo "Switching TileServer main service to colour: ${TARGET_COLOR} (namespace: ${NAMESPACE})"

# Patch the main service selector
kubectl patch service tileserver-gl -n "$NAMESPACE" \
  --type=json \
  -p="[{\"op\":\"replace\",\"path\":\"/spec/selector/color\",\"value\":\"${TARGET_COLOR}\"}]"

# Update PVC labels
kubectl label pvc "tileserver-${TARGET_COLOR}-pvc" -n "$NAMESPACE" active=true  --overwrite
kubectl label pvc "tileserver-${OLD_COLOR}-pvc"   -n "$NAMESPACE" active=false --overwrite

# Scale deployments
kubectl scale deployment "tileserver-${TARGET_COLOR}" -n "$NAMESPACE" --replicas=1
kubectl scale deployment "tileserver-${OLD_COLOR}"   -n "$NAMESPACE" --replicas=0

echo ""
echo "Done. Active colour: ${TARGET_COLOR}"
echo "Main service (port 31085) now routes to tileserver-${TARGET_COLOR}."
