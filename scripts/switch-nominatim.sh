#!/usr/bin/env bash
set -euo pipefail
TARGET_COLOR="${1:-}"
NAMESPACE="${NOMINATIM_NAMESPACE:-osm}"
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
if [[ "$TARGET_COLOR" == "blue" ]]; then OLD_COLOR=green; else OLD_COLOR=blue; fi
echo "Switching nominatim main service to colour: ${TARGET_COLOR} (namespace: ${NAMESPACE})"
kubectl patch service nominatim -n "$NAMESPACE" --type=json -p="[{"op":"replace","path":"/spec/selector/color","value":"${TARGET_COLOR}"},{"op":"replace","path":"/spec/selector/active","value":"true"}]"
kubectl patch service nominatim-postgres -n "$NAMESPACE" --type=json -p="[{"op":"replace","path":"/spec/selector/color","value":"${TARGET_COLOR}"},{"op":"replace","path":"/spec/selector/active","value":"true"}]"
kubectl label pvc "nominatim-${TARGET_COLOR}-pvc" -n "$NAMESPACE" active=true --overwrite
kubectl label pvc "nominatim-${OLD_COLOR}-pvc" -n "$NAMESPACE" active=false --overwrite
kubectl patch deployment "nominatim-${TARGET_COLOR}" -n "$NAMESPACE" --type=json -p="[{"op":"replace","path":"/spec/template/metadata/labels/active","value":"true"}]"
kubectl patch deployment "nominatim-${OLD_COLOR}" -n "$NAMESPACE" --type=json -p="[{"op":"replace","path":"/spec/template/metadata/labels/active","value":"false"}]" || true
kubectl scale deployment "nominatim-${TARGET_COLOR}" -n "$NAMESPACE" --replicas=1
kubectl scale deployment "nominatim-${OLD_COLOR}" -n "$NAMESPACE" --replicas=0
echo
echo "Done. Active colour: ${TARGET_COLOR}"
