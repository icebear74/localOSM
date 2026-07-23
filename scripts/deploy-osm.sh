#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="osm"
BASE_DIR="/mnt/data/OSM"
TEMP_BASE_DIR="${OSM_TEMP_DIR:-${BASE_DIR}/TempDir}"
DEPLOYMENTS=(tileserver-gl nominatim valhalla import-orchestrator status web)
PRESERVE_PATHS=("${BASE_DIR}/library" "${BASE_DIR}/status")
CLEAN=false
PRESERVE_DOWNLOADS=false
NODE_URL=""

usage() {
  cat <<EOF
Usage: $0 [--clean] [--preserve-downloads] [--node-url <url>] [--temp-dir <path>]
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --clean) CLEAN=true; shift ;;
    --preserve-downloads) PRESERVE_DOWNLOADS=true; shift ;;
    --node-url) NODE_URL="$2"; shift 2 ;;
    --temp-dir) TEMP_BASE_DIR="$2"; shift 2 ;;
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

if ! is_safe_basedir "${TEMP_BASE_DIR}"; then
  echo "ERROR: TEMP_BASE_DIR='${TEMP_BASE_DIR}' does not look like a safe data directory." >&2
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

# The temp dir only ever holds scratch/staging data for an in-progress
# import. On a clean start its contents can normally be discarded outright. Only the
# contents are removed — the directory (mount point) itself, which the user
# creates and mounts, is left untouched. When --preserve-downloads is set,
# the shared merged extract at TEMP_BASE_DIR/import/planet.osm.pbf (+ its
# .meta sidecar) is kept too, so a "clean" redeploy does not force every
# subsequent Build to re-download and re-merge the country extracts (only
# per-service staging dirs and stray intermediate merge artifacts are
# cleared).
clean_temp_directory() {
  echo ">>> Deleting contents of temporary scratch directory ${TEMP_BASE_DIR} …"
  if [ -d "${TEMP_BASE_DIR}" ]; then
    if [ "$PRESERVE_DOWNLOADS" = true ]; then
      shopt -s nullglob dotglob
      for entry in "${TEMP_BASE_DIR}"/*; do
        if [ "$(basename "$entry")" = "import" ]; then
          ${SUDO} find "${entry}" -mindepth 1 -maxdepth 1 ! -name 'planet.osm.pbf' ! -name 'planet.osm.pbf.meta' -exec rm -rf {} + 2>/dev/null || true
        else
          ${SUDO} rm -rf -- "$entry"
        fi
      done
    else
      ${SUDO} find "${TEMP_BASE_DIR:?}" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
    fi
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
  clean_temp_directory
fi

echo ">>> Creating host directories under ${BASE_DIR} …"
for dir in \
  "${BASE_DIR}/library" \
  "${BASE_DIR}/tileserver/active" \
  "${BASE_DIR}/tileserver/fonts" \
  "${BASE_DIR}/nominatim/active" \
  "${BASE_DIR}/valhalla/active" \
  "${BASE_DIR}/cache" \
  "${BASE_DIR}/status" \
  "${BASE_DIR}/manifests" \
  "${BASE_DIR}/scripts"; do
  ${SUDO} mkdir -p "$dir"
done

echo ">>> Creating temporary scratch directories under ${TEMP_BASE_DIR} …"
for dir in \
  "${TEMP_BASE_DIR}/import" \
  "${TEMP_BASE_DIR}/tileserver/staging" \
  "${TEMP_BASE_DIR}/nominatim/staging" \
  "${TEMP_BASE_DIR}/valhalla/staging"; do
  ${SUDO} mkdir -p "$dir"
done

terminate_existing_pods() {
  for deployment in "${DEPLOYMENTS[@]}"; do
    kubectl -n "${NAMESPACE}" scale "deployment/${deployment}" --replicas=0 >/dev/null 2>&1 || true
  done

  for deployment in "${DEPLOYMENTS[@]}"; do
    for _ in $(seq 1 60); do
      if ! kubectl -n "${NAMESPACE}" get pods -l "app=${deployment}" --no-headers 2>/dev/null | grep -q .; then
        break
      fi
      sleep 2
    done
  done
}

${SUDO} chown -R 1000:1000 "${BASE_DIR}/tileserver" 2>/dev/null || true
${SUDO} chown -R 100:100   "${BASE_DIR}/nominatim"  2>/dev/null || true
${SUDO} chown -R 1000:1000 "${BASE_DIR}/valhalla"   2>/dev/null || true
${SUDO} chown -R 1000:1000 "${BASE_DIR}/cache"      2>/dev/null || true
${SUDO} chown -R 1000:1000 "${BASE_DIR}/status"     2>/dev/null || true
${SUDO} chown -R 1000:1000 "${BASE_DIR}/manifests"  2>/dev/null || true
${SUDO} chown -R 1000:1000 "${BASE_DIR}/scripts"     2>/dev/null || true

${SUDO} chown -R 1000:1000 "${TEMP_BASE_DIR}/import"              2>/dev/null || true
${SUDO} chown -R 1000:1000 "${TEMP_BASE_DIR}/tileserver"          2>/dev/null || true
${SUDO} chown -R 100:100   "${TEMP_BASE_DIR}/nominatim"           2>/dev/null || true
${SUDO} chown -R 1000:1000 "${TEMP_BASE_DIR}/valhalla"            2>/dev/null || true

echo ">>> Copying static manifests and orchestrator script …"
# Manifests that hardcode the OSM_TEMP_DIR mount use the placeholder
# __OSM_TEMP_DIR__ instead of a literal path, so that a custom
# --temp-dir/OSM_TEMP_DIR value is honored by the deployed hostPaths/env vars
# too, not just by the directories this script creates below.
for manifest in \
  namespace.yaml \
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
  nominatim-postgres-tuning-config.yaml \
  tileserver-import-config.yaml \
  tileserver-import-job.yaml \
  import-orchestrator.yaml \
  web.yaml \
  style-editor.yaml; do
  sed "s|__OSM_TEMP_DIR__|${TEMP_BASE_DIR}|g" "${REPO_ROOT}/k8s/${manifest}" | ${SUDO} tee "${BASE_DIR}/manifests/${manifest}" >/dev/null
done
${SUDO} cp "${REPO_ROOT}/scripts/import-orchestrator.sh" "${BASE_DIR}/scripts/import-orchestrator.sh"
${SUDO} chmod +x "${BASE_DIR}/scripts/import-orchestrator.sh"

if [ -d "${REPO_ROOT}/fonts" ]; then
  ${SUDO} find "${BASE_DIR}/tileserver/fonts" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  ${SUDO} cp -a "${REPO_ROOT}/fonts/." "${BASE_DIR}/tileserver/fonts/"
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
kubectl apply -f "${BASE_DIR}/manifests/namespace.yaml"

if [ -f "${REPO_ROOT}/k8s/style.json" ]; then
  kubectl -n "${NAMESPACE}" create configmap tileserver-style \
    --from-file=style.json="${REPO_ROOT}/k8s/style.json" \
    --dry-run=client -o yaml | kubectl apply -f -
fi

for manifest in tileserver.yaml nominatim.yaml nominatim-postgres-tuning-config.yaml valhalla-config.yaml valhalla.yaml valhalla-import-config.yaml status-config.yaml status.yaml nominatim-import-config.yaml tileserver-import-config.yaml import-orchestrator.yaml web.yaml style-editor.yaml; do
  kubectl apply -f "${BASE_DIR}/manifests/${manifest}"
done

echo ">>> Waiting for core services (status, web, import orchestrator) …"
for deployment in status web import-orchestrator; do
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
Style-Editor     : ${NODE_DISPLAY}:30086
EOF
