#!/usr/bin/env bash
set -euo pipefail
NAMESPACE="${NOMINATIM_NAMESPACE:-osm}"
SUFFIX=$(date +%Y%m%d%H%M%S)
JOB_NAME="nominatim-import-orchestrator-${SUFFIX}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace|-n) NAMESPACE="$2"; shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done
echo "Creating nominatim orchestrator job '${JOB_NAME}' in namespace '${NAMESPACE}' ..."
kubectl create job "$JOB_NAME" --from=job/nominatim-import-orchestrator -n "$NAMESPACE"
echo
echo "Job created. Follow logs with:"
echo "  kubectl logs -f job/${JOB_NAME} -n ${NAMESPACE}"
