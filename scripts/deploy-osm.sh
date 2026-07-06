#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="osm"
BASE_DIR="/mnt/data/OSM"
DEPLOYMENTS=(postgres tileserver-gl nominatim valhalla status web)
CLEAN=false
NODE_URL=""

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
usage() {
  cat <<EOF
Usage: $0 [--clean] [--node-url <url>]

Options:
  --clean             Remove the existing Kubernetes namespace, all OSM data on
                      disk, and perform a clean install from scratch. You will
                      be prompted before any data is deleted.
  --node-url <url>    Set the node base URL used for service links in the
                      status dashboard (e.g. http://192.168.1.100). If omitted,
                      the IP is auto-detected from the first cluster node.
  -h                  Show this help text.
EOF
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --clean) CLEAN=true ; shift ;;
    --node-url) NODE_URL="$2" ; shift 2 ;;
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
# Safety check: BASE_DIR must be a deep, non-system path
# ---------------------------------------------------------------------------
is_safe_basedir() {
  local dir
  dir="$(realpath -m "$1" 2>/dev/null || echo "$1")"

  # Must be absolute
  [[ "${dir}" == /* ]] || return 1

  # Must not be a known dangerous system prefix
  local unsafe_prefixes=(
    "/"
    "/bin" "/boot" "/dev" "/etc" "/home" "/lib" "/lib64"
    "/media" "/opt" "/proc" "/root" "/run" "/sbin"
    "/srv" "/sys" "/tmp" "/usr" "/var"
  )
  for prefix in "${unsafe_prefixes[@]}"; do
    if [ "${dir}" = "${prefix}" ]; then
      return 1
    fi
  done

  # Must be at least 3 levels deep (e.g. /mnt/data/OSM)
  local depth
  depth="$(echo "${dir}" | tr -cd '/' | wc -c)"
  [ "${depth}" -ge 3 ] || return 1

  return 0
}

if ! is_safe_basedir "${BASE_DIR}"; then
  echo "ERROR: BASE_DIR='${BASE_DIR}' does not look like a safe data directory." >&2
  echo "       It must be an absolute path at least 3 levels deep and must not" >&2
  echo "       point to a system directory (/, /var, /home, …)." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Auto-detect node URL if not provided
# ---------------------------------------------------------------------------
if [ -z "${NODE_URL}" ]; then
  DETECTED_IP="$(kubectl get nodes \
    -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null || true)"
  if [ -n "${DETECTED_IP}" ]; then
    NODE_URL="http://${DETECTED_IP}"
    echo ">>> Auto-detected node IP: ${DETECTED_IP} → using ${NODE_URL}"
  else
    echo "    WARN: Could not auto-detect node IP. Set node URL manually in the status dashboard."
  fi
fi

namespace_exists() {
  kubectl get namespace "${NAMESPACE}" >/dev/null 2>&1
}

wait_for_namespace_deletion() {
  local timeout_secs="$1"
  local waited=0
  while namespace_exists; do
    if [ "$waited" -ge "$timeout_secs" ]; then
      return 1
    fi
    sleep 2
    waited=$((waited + 2))
  done
  return 0
}

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

  if namespace_exists; then
    echo ""
    echo ">>> Cleaning resources in namespace '${NAMESPACE}' before namespace delete …"
    kubectl -n "${NAMESPACE}" delete all --all --ignore-not-found --grace-period=0 --force --timeout=90s >/dev/null 2>&1 || true
    kubectl -n "${NAMESPACE}" delete pvc --all --ignore-not-found --grace-period=0 --force --timeout=90s >/dev/null 2>&1 || true
    kubectl -n "${NAMESPACE}" delete configmap --all --ignore-not-found --timeout=90s >/dev/null 2>&1 || true
    kubectl -n "${NAMESPACE}" delete secret --all --ignore-not-found --timeout=90s >/dev/null 2>&1 || true
  fi

  echo ">>> Deleting Kubernetes namespace '${NAMESPACE}' …"
  kubectl delete namespace "${NAMESPACE}" --ignore-not-found --wait=false >/dev/null 2>&1 || true

  if wait_for_namespace_deletion 120; then
    echo "    Namespace deleted."
  else
    echo "    Namespace deletion is stuck (likely finalizers). Forcing finalizer cleanup …"
    kubectl patch namespace "${NAMESPACE}" --type=merge -p '{"metadata":{"finalizers":[]}}' >/dev/null 2>&1 || true
    kubectl get namespace "${NAMESPACE}" -o json 2>/dev/null \
      | python3 -c 'import json,sys; o=json.load(sys.stdin); o.setdefault("spec",{}); o["spec"]["finalizers"]=[]; print(json.dumps(o))' \
      | kubectl replace --raw "/api/v1/namespaces/${NAMESPACE}/finalize" -f - >/dev/null 2>&1 || true
    if wait_for_namespace_deletion 60; then
      echo "    Namespace force-deleted."
    else
      echo "ERROR: Namespace '${NAMESPACE}' is still terminating after finalizer cleanup." >&2
      echo "Run: kubectl get namespace ${NAMESPACE} -o yaml" >&2
      exit 1
    fi
  fi

  echo ">>> Deleting contents of data directory ${BASE_DIR} …"
  ${SUDO} find "${BASE_DIR:?}" -mindepth 1 -delete
  echo "    Data directory contents deleted."
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
# Write node URL to status config (preserves existing config keys)
# ---------------------------------------------------------------------------
CONFIG_PATH="${BASE_DIR}/status/config.json"
if [ -n "${NODE_URL}" ]; then
  echo ">>> Writing node URL to ${CONFIG_PATH} …"
  EXISTING_CONFIG="{}"
  if [ -f "${CONFIG_PATH}" ]; then
    EXISTING_CONFIG="$(cat "${CONFIG_PATH}")"
  fi
  UPDATED_CONFIG="$(echo "${EXISTING_CONFIG}" | python3 -c "
import json, os, sys
cfg = json.load(sys.stdin)
cfg['node_url'] = os.environ['NODE_URL']
print(json.dumps(cfg, indent=2, sort_keys=True))
" NODE_URL="${NODE_URL}" 2>/dev/null || echo "{\"node_url\": \"${NODE_URL}\"}")"
  echo "${UPDATED_CONFIG}" | ${SUDO} tee "${CONFIG_PATH}" >/dev/null
  echo "    Node URL set to: ${NODE_URL}"
fi

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
NODE_DISPLAY="${NODE_URL:-<node-ip>}"
cat <<EOF

========================================
  localOSM deployment complete
========================================

Services:
  Status dashboard : ${NODE_DISPLAY}:30083
  Web / Routing UI : ${NODE_DISPLAY}:30084
  Nominatim        : ${NODE_DISPLAY}:30081  (starts after OSM data import)
  Valhalla         : ${NODE_DISPLAY}:30082  (starts after OSM data import)
  TileServer GL    : ${NODE_DISPLAY}:30085

NOTE: Nominatim and Valhalla will stay in "Init" state until OSM data has
been downloaded and imported. Use the Status dashboard or run:

  bash ${REPO_ROOT}/scripts/run-import.sh --url https://download.geofabrik.de/europe/germany/berlin-latest.osm.pbf

EOF
