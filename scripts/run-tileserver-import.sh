#!/usr/bin/env bash
# run-tileserver-import.sh
# Trigger a TileServer Blue/Green import by creating an orchestrator Job.
# Usage: bash scripts/run-tileserver-import.sh [--namespace <ns>]
set -euo pipefail

NAMESPACE="${TILESERVER_NAMESPACE:-osm}"
SUFFIX=$(date +%Y%m%d%H%M%S)
JOB_NAME="tileserver-import-orchestrator-${SUFFIX}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace|-n) NAMESPACE="$2"; shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

echo "Creating TileServer orchestrator job '${JOB_NAME}' in namespace '${NAMESPACE}' ..."
kubectl create job "$JOB_NAME" \
  --from=job/tileserver-import-orchestrator \
  -n "$NAMESPACE"

echo ""
echo "Job created. Follow logs with:"
echo "  kubectl logs -f job/${JOB_NAME} -n ${NAMESPACE}"
