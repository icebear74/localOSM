#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="osm"
BASE_DIR="/mnt/data/OSM"
DEPLOYMENTS=(postgres tileserver-gl nominatim valhalla status web)

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
for dir in \
  "${BASE_DIR}/postgres/data" \
  "${BASE_DIR}/library" \
  "${BASE_DIR}/tileserver" \
  "${BASE_DIR}/nominatim" \
  "${BASE_DIR}/valhalla/tiles" \
  "${BASE_DIR}/import" \
  "${BASE_DIR}/cache" \
  "${BASE_DIR}/status"; do
  ${SUDO} mkdir -p "$dir"
done

${SUDO} chown -R 999:999 "${BASE_DIR}/postgres" 2>/dev/null || true
${SUDO} chown -R 1000:1000 "${BASE_DIR}/tileserver" 2>/dev/null || true
${SUDO} chown -R 1000:1000 "${BASE_DIR}/nominatim" 2>/dev/null || true
${SUDO} chown -R 1000:1000 "${BASE_DIR}/valhalla" 2>/dev/null || true
${SUDO} chown -R 1000:1000 "${BASE_DIR}/import" 2>/dev/null || true
${SUDO} chown -R 1000:1000 "${BASE_DIR}/cache" 2>/dev/null || true
${SUDO} chown -R 1000:1000 "${BASE_DIR}/status" 2>/dev/null || true

EXISTING_DEPLOYMENTS=()
for deployment in "${DEPLOYMENTS[@]}"; do
  if kubectl -n "${NAMESPACE}" get deployment "${deployment}" >/dev/null 2>&1; then
    EXISTING_DEPLOYMENTS+=("${deployment}")
  fi
done

if [ "${#EXISTING_DEPLOYMENTS[@]}" -gt 0 ]; then
  echo "Existing installation detected in namespace ${NAMESPACE}; applying updates."
else
  echo "No existing installation detected; performing first-time install."
fi

kubectl apply -f "${REPO_ROOT}/k8s/namespace.yaml"
kubectl apply -f "${REPO_ROOT}/k8s/postgres.yaml"
kubectl apply -f "${REPO_ROOT}/k8s/tileserver.yaml"
kubectl apply -f "${REPO_ROOT}/k8s/nominatim.yaml"
kubectl apply -f "${REPO_ROOT}/k8s/valhalla-config.yaml"
kubectl apply -f "${REPO_ROOT}/k8s/valhalla.yaml"
kubectl apply -f "${REPO_ROOT}/k8s/status.yaml"
kubectl apply -f "${REPO_ROOT}/k8s/web.yaml"

if [ "${#EXISTING_DEPLOYMENTS[@]}" -gt 0 ]; then
  for deployment in "${EXISTING_DEPLOYMENTS[@]}"; do
    kubectl -n "${NAMESPACE}" rollout restart "deployment/${deployment}" >/dev/null 2>&1 || true
  done
fi

for deployment in "${DEPLOYMENTS[@]}"; do
  kubectl -n "${NAMESPACE}" rollout status "deployment/${deployment}" --timeout=180s >/dev/null 2>&1 || true
done

echo "Deployment finished."
echo "Services:"
echo "  - TileServer GL: http://<node-ip>:30085"
echo "  - Nominatim: http://<node-ip>:30081"
echo "  - Valhalla: http://<node-ip>:30082"
echo "  - Status UI: http://<node-ip>:30083"
echo "  - OSM Web UI: http://<node-ip>:30084"
echo ""
echo "Download OSM data with:"
echo "  bash ${REPO_ROOT}/scripts/run-import.sh --url https://download.geofabrik.de/europe/germany/berlin-latest.osm.pbf"
