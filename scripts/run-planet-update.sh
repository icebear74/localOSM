#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="${OSM_NAMESPACE:-osm}"
JOB_NAME="planet-update-manual-$(date +%Y%m%d%H%M%S)"

kubectl apply -f "${REPO_ROOT}/k8s/base/cronjobs/planet-update-cronjob.yaml"
kubectl -n "${NAMESPACE}" create job --from=cronjob/planet-update "${JOB_NAME}"
echo "Started ${JOB_NAME} in namespace ${NAMESPACE}."
