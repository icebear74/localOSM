#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="${OSM_NAMESPACE:-osm}"

kubectl -n "${NAMESPACE}" delete job osmium-pipeline --ignore-not-found >/dev/null 2>&1 || true
kubectl apply -f "${REPO_ROOT}/k8s/base/jobs/osmium-pipeline-job.yaml"
echo "Started osmium-pipeline in namespace ${NAMESPACE}."
