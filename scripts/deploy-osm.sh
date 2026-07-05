#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="osm"
BASE_DIR="/mnt/data/OSM"
DEPLOYMENTS=(postgres tileserver-gl nominatim valhalla status web)
CLEAN=false

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
usage() {
  cat <<EOF
Usage: $0 [--clean]

Options:
  --clean   Remove the existing Kubernetes namespace, all OSM data on disk, and
            perform a clean install from scratch. You will be prompted before any
            data is deleted.
  -h        Show this help text.
EOF
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --clean) CLEAN=true ; shift ;;
    -h|--help) usage ; exit 0 ;;
    *) echo "Unknown option: $1" >&2 ; usage >&2 ; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------
if ! command -v kubectl >/dev/null 2>&1; then
  echo "ERROR: kubectl not found in PATH" >&2
  exit 1
fi

if ! kubectl cluster-info >/dev/null 2>&1; then
  echo "ERROR: kubectl could not reach a Kubernetes cluster" >&2
  exit 1
fi

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  SUDO="sudo"
fi

# ---------------------------------------------------------------------------
# Clean start (optional)
# ---------------------------------------------------------------------------
if [ "$CLEAN" = true ]; then
  echo "========================================"
  echo "  CLEAN START — all existing data will"
  echo "  be permanently deleted!"
  echo "========================================"
  echo ""
  echo "The following will be deleted:"
  echo "  • Kubernetes namespace '${NAMESPACE}' and ALL its resources (pods, services, …)"
  echo "  • All OSM data under ${BASE_DIR}"
  echo ""
  read -r -p "Type 'yes' to continue, anything else to abort: " confirm
  if [ "$confirm" != "yes" ]; then
    echo "Aborted."
    exit 0
  fi

  echo ""
  echo ">>> Deleting Kubernetes namespace '${NAMESPACE}' …"
  kubectl delete namespace "${NAMESPACE}" --ignore-not-found --wait=true
  echo "    Namespace deleted."

  echo ">>> Deleting data directory ${BASE_DIR} …"
  ${SUDO} rm -rf "${BASE_DIR}"
  echo "    Data directory deleted."
  echo ""
fi

# ---------------------------------------------------------------------------
# Create host directories
# ---------------------------------------------------------------------------
echo ">>> Creating host directories under ${BASE_DIR} …"
for dir in \
  "${BASE_DIR}/postgres/data" \
  "${BASE_DIR}/library" \
  "${BASE_DIR}/tileserver" \
  "${BASE_DIR}/nominatim" \
  "${BASE_DIR}/valhalla" \
  "${BASE_DIR}/import" \
  "${BASE_DIR}/cache" \
  "${BASE_DIR}/status"; do
  ${SUDO} mkdir -p "$dir"
done

${SUDO} chown -R 999:999  "${BASE_DIR}/postgres"   2>/dev/null || true
${SUDO} chown -R 1000:1000 "${BASE_DIR}/tileserver" 2>/dev/null || true
${SUDO} chown -R 1000:1000 "${BASE_DIR}/nominatim"  2>/dev/null || true
${SUDO} chown -R 1000:1000 "${BASE_DIR}/valhalla"   2>/dev/null || true
${SUDO} chown -R 1000:1000 "${BASE_DIR}/import"     2>/dev/null || true
${SUDO} chown -R 1000:1000 "${BASE_DIR}/cache"      2>/dev/null || true
${SUDO} chown -R 1000:1000 "${BASE_DIR}/status"     2>/dev/null || true
echo "    Directories ready."

# ---------------------------------------------------------------------------
# Apply Kubernetes manifests
# ---------------------------------------------------------------------------
echo ""
echo ">>> Applying Kubernetes manifests …"
kubectl apply -f "${REPO_ROOT}/k8s/namespace.yaml"
kubectl apply -f "${REPO_ROOT}/k8s/postgres.yaml"
kubectl apply -f "${REPO_ROOT}/k8s/tileserver.yaml"
kubectl apply -f "${REPO_ROOT}/k8s/nominatim.yaml"
kubectl apply -f "${REPO_ROOT}/k8s/valhalla-config.yaml"
kubectl apply -f "${REPO_ROOT}/k8s/valhalla.yaml"
kubectl apply -f "${REPO_ROOT}/k8s/status.yaml"
kubectl apply -f "${REPO_ROOT}/k8s/web.yaml"

# Clean up any stale valhalla import job (e.g., left from a previous run)
kubectl -n "${NAMESPACE}" delete job valhalla-import --ignore-not-found >/dev/null 2>&1 || true

echo "    Manifests applied."

# ---------------------------------------------------------------------------
# Rolling restart (update existing pods)
# ---------------------------------------------------------------------------
echo ""
echo ">>> Triggering rolling restart of all deployments …"
for deployment in "${DEPLOYMENTS[@]}"; do
  kubectl -n "${NAMESPACE}" rollout restart "deployment/${deployment}" >/dev/null 2>&1 || true
done

# ---------------------------------------------------------------------------
# Wait (best-effort) for stateless services that don't depend on OSM data
# ---------------------------------------------------------------------------
echo ""
echo ">>> Waiting for core services (postgres, status, web) …"
for deployment in postgres status web; do
  kubectl -n "${NAMESPACE}" rollout status "deployment/${deployment}" --timeout=120s 2>/dev/null || true
done

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
cat <<EOF

========================================
  localOSM deployment complete
========================================

Services:
  Status dashboard : http://<node-ip>:30083
  Web / Routing UI : http://<node-ip>:30084
  Nominatim        : http://<node-ip>:30081  (starts after OSM data import)
  Valhalla         : http://<node-ip>:30082  (starts after OSM data import)
  TileServer GL    : http://<node-ip>:30085

NOTE: Nominatim and Valhalla will stay in "Init" state until OSM data has
been downloaded and imported. Use the Status dashboard or run:

  bash ${REPO_ROOT}/scripts/run-import.sh --url https://download.geofabrik.de/europe/germany/berlin-latest.osm.pbf

EOF
