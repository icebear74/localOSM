#!/usr/bin/env bash
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="${VALHALLA_NAMESPACE:-osm}"
SUFFIX=$(date +%Y%m%d%H%M%S)
JOB_NAME="valhalla-import-orchestrator-${SUFFIX}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace|-n) NAMESPACE="$2"; shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done
MANIFEST_PATH="$REPO_ROOT/k8s/base/jobs/valhalla-import-orchestrator.yaml"
TMP_MANIFEST="$(mktemp)"
trap 'rm -f "$TMP_MANIFEST"' EXIT
sed -e "s|^  name: .*|  name: ${JOB_NAME}|" \
    -e "s|^  namespace: .*|  namespace: ${NAMESPACE}|" \
    "$MANIFEST_PATH" > "$TMP_MANIFEST"
echo "Creating valhalla orchestrator job '${JOB_NAME}' in namespace '${NAMESPACE}' ..."
kubectl apply -f "$TMP_MANIFEST"
echo
echo "Job created. Follow logs with:"
echo "  kubectl logs -f job/${JOB_NAME} -n ${NAMESPACE}"
