#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="osm"
BASE_DIR="/mnt/data/OSM"

if ! command -v kubectl >/dev/null 2>&1; then
  echo "kubectl not found in PATH" >&2
  exit 1
fi

if ! kubectl cluster-info >/dev/null 2>&1; then
  echo "kubectl could not reach a Kubernetes cluster" >&2
  exit 1
fi

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  SUDO="sudo"
fi

echo "Creating host directories under ${BASE_DIR}"
for dir in "${BASE_DIR}/postgres/data" "${BASE_DIR}/tileserver"; do
  ${SUDO} mkdir -p "$dir"
done

${SUDO} chown -R 999:999 "${BASE_DIR}/postgres" 2>/dev/null || true
${SUDO} chown -R 1000:1000 "${BASE_DIR}/tileserver" 2>/dev/null || true

kubectl apply -f "${REPO_ROOT}/k8s/namespace.yaml"
kubectl apply -f "${REPO_ROOT}/k8s/postgres.yaml"
kubectl apply -f "${REPO_ROOT}/k8s/tileserver.yaml"

kubectl -n "${NAMESPACE}" rollout status deployment/postgres --timeout=180s >/dev/null 2>&1 || true
kubectl -n "${NAMESPACE}" rollout status deployment/tileserver-gl --timeout=180s >/dev/null 2>&1 || true

echo "Deployment finished."
echo "Check resources with: kubectl -n ${NAMESPACE} get all"
echo "Postgres: postgres.${NAMESPACE}.svc.cluster.local:5432"
echo "Tileserver: http://<service-ip-or-nodeport>"
