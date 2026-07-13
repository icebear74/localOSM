#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="osm"
BASE_DIR="/mnt/data/OSM"
DEPLOYMENTS=(postgres tileserver-gl nominatim valhalla import-orchestrator status web)
PRESERVE_PATHS=("${BASE_DIR}/import" "${BASE_DIR}/library" "${BASE_DIR}/status")
CLEAN=false
PRESERVE_DOWNLOADS=false
NODE_URL=""

usage() {
  cat <<EOF
Usage: $0 [--clean] [--preserve-downloads] [--node-url <url>]
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --clean) CLEAN=true; shift ;;
    --preserve-downloads) PRESERVE_DOWNLOADS=true; shift ;;
    --node-url) NODE_URL="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

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

is_safe_basedir() {
  local dir
  dir="$(realpath -m "$1" 2>/dev/null || echo "$1")"
  [[ "${dir}" == /* ]] || return 1
  local unsafe_prefixes=("/" "/bin" "/boot" "/dev" "/etc" "/home" "/lib" "/lib64" "/media" "/opt" "/proc" "/root" "/run" "/sbin" "/srv" "/sys" "/tmp" "/usr" "/var")
  for prefix in "${unsafe_prefixes[@]}"; do
    [ "${dir}" = "${prefix}" ] && return 1
  done
  local depth
  depth="$(echo "${dir}" | tr -cd '/' | wc -c)"
  [ "${depth}" -ge 3 ] || return 1
}

if ! is_safe_basedir "${BASE_DIR}"; then
  echo "ERROR: BASE_DIR='${BASE_DIR}' does not look like a safe data directory." >&2
  exit 1
fi

namespace_exists() {
  kubectl get namespace "${NAMESPACE}" >/dev/null 2>&1
}

wait_for_namespace_deletion() {
  local timeout_secs="$1" waited=0
  while namespace_exists; do
    [ "$waited" -ge "$timeout_secs" ] && return 1
    sleep 2
    waited=$((waited + 2))
  done
  return 0
}

clean_data_directory() {
  echo ">>> Deleting contents of data directory ${BASE_DIR} …"
  if [ "$PRESERVE_DOWNLOADS" = true ]; then
    shopt -s nullglob dotglob
    for entry in "${BASE_DIR}"/*; do
      keep=false
      for preserved in "${PRESERVE_PATHS[@]}"; do
        if [ "$entry" = "$preserved" ]; then
          keep=true
          break
        fi
      done
      if [ "$keep" = false ]; then
        ${SUDO} rm -rf -- "$entry"
      fi
    done
  else
    ${SUDO} find "${BASE_DIR:?}" -mindepth 1 -maxdepth 10 -delete
  fi
}

if [ "$CLEAN" = true ]; then
  echo "========================================"
  echo "  CLEAN START — all existing data will"
  echo "  be permanently deleted!"
  echo "========================================"
  read -r -p "Type 'yes' to continue, anything else to abort: " confirm
  [ "$confirm" = "yes" ] || exit 0
  if namespace_exists; then
    kubectl -n "${NAMESPACE}" delete all --all --ignore-not-found --grace-period=0 --force --timeout=90s >/dev/null 2>&1 || true
    kubectl -n "${NAMESPACE}" delete pvc --all --ignore-not-found --grace-period=0 --force --timeout=90s >/dev/null 2>&1 || true
    kubectl -n "${NAMESPACE}" delete configmap --all --ignore-not-found --timeout=90s >/dev/null 2>&1 || true
    kubectl -n "${NAMESPACE}" delete secret --all --ignore-not-found --timeout=90s >/dev/null 2>&1 || true
  fi
  kubectl delete namespace "${NAMESPACE}" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  if ! wait_for_namespace_deletion 120; then
    kubectl patch namespace "${NAMESPACE}" --type=merge -p '{"metadata":{"finalizers":[]}}' >/dev/null 2>&1 || true
    kubectl get namespace "${NAMESPACE}" -o json 2>/dev/null | python3 -c 'import json,sys; o=json.load(sys.stdin); o.setdefault("spec",{}); o["spec"]["finalizers"]=[]; print(json.dumps(o))' | kubectl replace --raw "/api/v1/namespaces/${NAMESPACE}/finalize" -f - >/dev/null 2>&1 || true
  fi
  clean_data_directory
fi

echo ">>> Creating host directories under ${BASE_DIR} …"
for dir in \
  "${BASE_DIR}/postgres/data" \
  "${BASE_DIR}/library" \
  "${BASE_DIR}/tileserver/active" \
  "${BASE_DIR}/tileserver/staging" \
  "${BASE_DIR}/tileserver/fonts" \
  "${BASE_DIR}/nominatim/active" \
  "${BASE_DIR}/nominatim/staging" \
  "${BASE_DIR}/valhalla/active" \
  "${BASE_DIR}/valhalla/staging" \
  "${BASE_DIR}/import" \
  "${BASE_DIR}/cache" \
  "${BASE_DIR}/status" \
  "${BASE_DIR}/manifests" \
  "${BASE_DIR}/scripts"; do
  ${SUDO} mkdir -p "$dir"
done

terminate_existing_pods() {
  for deployment in "${DEPLOYMENTS[@]}"; do
    kubectl -n "${NAMESPACE}" delete pod -l "app=${deployment}" --ignore-not-found --wait=true >/dev/null 2>&1 || true
  done
}

${SUDO} chown -R 999:999  "${BASE_DIR}/postgres"   2>/dev/null || true
${SUDO} chown -R 1000:1000 "${BASE_DIR}/tileserver" 2>/dev/null || true
${SUDO} chown -R 100:100   "${BASE_DIR}/nominatim"  2>/dev/null || true
${SUDO} chown -R 1000:1000 "${BASE_DIR}/valhalla"   2>/dev/null || true
${SUDO} chown -R 1000:1000 "${BASE_DIR}/import"     2>/dev/null || true
${SUDO} chown -R 1000:1000 "${BASE_DIR}/cache"      2>/dev/null || true
${SUDO} chown -R 1000:1000 "${BASE_DIR}/status"     2>/dev/null || true
${SUDO} chown -R 1000:1000 "${BASE_DIR}/manifests"  2>/dev/null || true
${SUDO} chown -R 1000:1000 "${BASE_DIR}/scripts"     2>/dev/null || true

echo ">>> Copying static manifests and orchestrator script …"
for manifest in \
  namespace.yaml \
  postgres.yaml \
  tileserver.yaml \
  nominatim.yaml \
  valhalla.yaml \
  valhalla-config.yaml \
  valhalla-import-config.yaml \
  valhalla-import-job.yaml \
  status.yaml \
  status-config.yaml \
  nominatim-import-config.yaml \
  nominatim-import-job.yaml \
  tileserver-import-config.yaml \
  tileserver-import-job.yaml \
  import-orchestrator.yaml \
  web.yaml; do
  ${SUDO} cp "${REPO_ROOT}/k8s/${manifest}" "${BASE_DIR}/manifests/${manifest}"
done
${SUDO} cp "${REPO_ROOT}/scripts/import-orchestrator.sh" "${BASE_DIR}/scripts/import-orchestrator.sh"
${SUDO} chmod +x "${BASE_DIR}/scripts/import-orchestrator.sh"

if [ -f "${REPO_ROOT}/k8s/style.json" ]; then
  ${SUDO} cp "${REPO_ROOT}/k8s/style.json" "${BASE_DIR}/tileserver/style.template.json"
  ${SUDO} chown 1000:1000 "${BASE_DIR}/tileserver/style.template.json" 2>/dev/null || true
fi

CONFIG_PATH="${BASE_DIR}/status/config.json"
if [ ! -f "${CONFIG_PATH}" ]; then
  cat > "${CONFIG_PATH}" <<'EOF'
{
  "node_url": "",
  "auto_update_enabled": false,
  "auto_update_time": "03:00",
  "routing_costing_models": {
    "car": {"enabled": true},
    "foot": {"enabled": true},
    "bicycle": {"enabled": true}
  },
  "routing_speeds": {
    "car": 120,
    "foot": 5,
    "bicycle": 25
  },
  "routing_advanced": {
    "car": {"toll_factor": 1.0, "unpaved_factor": 1.0, "ferry_factor": 1.0},
    "foot": {"hill_factor": 1.0, "unpaved_factor": 1.0},
    "bicycle": {"hill_factor": 1.0, "unpaved_factor": 1.0}
  }
}
EOF
fi

if [ -n "${NODE_URL}" ]; then
  python3 - "${CONFIG_PATH}" "${NODE_URL}" <<'PY'
import json, sys
path, node_url = sys.argv[1:3]
with open(path, encoding='utf-8') as handle:
    cfg = json.load(handle)
cfg['node_url'] = node_url
with open(path, 'w', encoding='utf-8') as handle:
    json.dump(cfg, handle, indent=2, sort_keys=True)
PY
fi

echo ">>> Applying Kubernetes manifests …"
terminate_existing_pods
for manifest in namespace.yaml postgres.yaml tileserver.yaml nominatim.yaml valhalla-config.yaml valhalla.yaml valhalla-import-config.yaml status-config.yaml status.yaml nominatim-import-config.yaml tileserver-import-config.yaml import-orchestrator.yaml web.yaml; do
  kubectl apply -f "${BASE_DIR}/manifests/${manifest}"
done

echo ">>> Waiting for core services (postgres, status, web, import orchestrator) …"
for deployment in postgres status web import-orchestrator; do
  kubectl -n "${NAMESPACE}" rollout status "deployment/${deployment}" --timeout=120s 2>/dev/null || true
done

NODE_DISPLAY="${NODE_URL:-<node-ip>}"
cat <<EOF
localOSM deployment complete
Status dashboard : ${NODE_DISPLAY}:30083
Web / Routing UI : ${NODE_DISPLAY}:30084
Nominatim        : ${NODE_DISPLAY}:30081
Valhalla         : ${NODE_DISPLAY}:30082
TileServer GL    : ${NODE_DISPLAY}:30085
EOF
