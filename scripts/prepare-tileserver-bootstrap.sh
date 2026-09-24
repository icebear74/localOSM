#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_DIR="/mnt/OSM"
NAMESPACE="osm"
APPLY_CONFIGMAP=false

usage() {
  cat <<EOF
Usage: $0 [--base-dir <path>] [--namespace <name>] [--apply-configmap]
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-dir) BASE_DIR="$2"; shift 2 ;;
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --apply-configmap) APPLY_CONFIGMAP=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

is_safe_basedir() {
  local dir
  dir="$(realpath -m "$1" 2>/dev/null || echo "$1")"
  [[ "${dir}" == /* ]] || return 1
  local unsafe_prefixes=("/" "/bin" "/boot" "/dev" "/etc" "/home" "/lib" "/lib64" "/media" "/opt" "/proc" "/root" "/run" "/sbin" "/srv" "/sys" "/tmp" "/usr" "/var")
  for prefix in "${unsafe_prefixes[@]}"; do
    [ "${dir}" = "${prefix}" ] && return 1
    [[ "${dir}" == "${prefix}/"* ]] && return 1
  done
  local depth
  depth="$(echo "${dir}" | tr -cd '/' | wc -c)"
  [ "${depth}" -ge 2 ] || return 1
}

if ! is_safe_basedir "${BASE_DIR}"; then
  echo "ERROR: BASE_DIR='${BASE_DIR}' does not look like a safe data directory." >&2
  exit 1
fi

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  SUDO="sudo"
fi

BOOTSTRAP_DIR="${BASE_DIR}/tileserver/bootstrap"
${SUDO} mkdir -p "${BOOTSTRAP_DIR}" "${BOOTSTRAP_DIR}/fonts" "${BOOTSTRAP_DIR}/sprites" "${BOOTSTRAP_DIR}/icons"

if [ -d "${REPO_ROOT}/fonts" ]; then
  ${SUDO} find "${BOOTSTRAP_DIR}/fonts" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  ${SUDO} cp -a "${REPO_ROOT}/fonts/." "${BOOTSTRAP_DIR}/fonts/"
fi

if [ -d "${REPO_ROOT}/sprites" ]; then
  ${SUDO} find "${BOOTSTRAP_DIR}/sprites" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  ${SUDO} cp -a "${REPO_ROOT}/sprites/." "${BOOTSTRAP_DIR}/sprites/"
fi

if [ -d "${REPO_ROOT}/icons" ]; then
  ${SUDO} find "${BOOTSTRAP_DIR}/icons" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  ${SUDO} cp -a "${REPO_ROOT}/icons/." "${BOOTSTRAP_DIR}/icons/"
fi

${SUDO} find "${BOOTSTRAP_DIR}" -maxdepth 1 -type f -name 'style_*.json' -delete
for source_style in "${REPO_ROOT}"/k8s/style_*.json; do
  [ -f "${source_style}" ] || continue
  ${SUDO} cp "${source_style}" "${BOOTSTRAP_DIR}/$(basename "${source_style}")"
done

if [ ! -f "${BOOTSTRAP_DIR}/style_vibrant.json" ]; then
  printf '%s\n' '{"version":8,"name":"LocalOSM","sources":{"openmaptiles":{"type":"vector","url":"mbtiles://{v3}"}},"glyphs":"{fontstack}/{range}.pbf","layers":[{"id":"background","type":"background","paint":{"background-color":"#f2efe9"}}]}' | ${SUDO} tee "${BOOTSTRAP_DIR}/style_vibrant.json" >/dev/null
fi

styles_json=''
while IFS= read -r style_path; do
  style_file="$(basename "${style_path}")"
  style_id="${style_file%.json}"
  style_entry="\"${style_id}\":{\"style\":\"/data/${style_file}\"}"
  if [ -n "${styles_json}" ]; then
    styles_json="${styles_json},${style_entry}"
  else
    styles_json="${style_entry}"
  fi
done < <(find "${BOOTSTRAP_DIR}" -maxdepth 1 -type f -name 'style_*.json' | sort)
if [ -z "${styles_json}" ]; then
  styles_json='"style_vibrant":{"style":"/data/style_vibrant.json"}'
fi
printf '%s\n' "{\"options\":{\"paths\":{\"root\":\"/data\",\"fonts\":\"fonts\",\"sprites\":\"sprites\",\"icons\":\"icons\"},\"serveAllFonts\":true,\"cors\":true},\"styles\":{${styles_json}},\"data\":{\"v3\":{\"mbtiles\":\"planet.mbtiles\"}}}" | ${SUDO} tee "${BOOTSTRAP_DIR}/config.json" >/dev/null

if [ "${APPLY_CONFIGMAP}" = true ]; then
  if ! command -v kubectl >/dev/null 2>&1; then
    echo "ERROR: kubectl not found in PATH" >&2
    exit 1
  fi
  if ! kubectl cluster-info >/dev/null 2>&1; then
    echo "ERROR: kubectl could not reach a Kubernetes cluster" >&2
    exit 1
  fi
  style_files=()
  for source_style in "${REPO_ROOT}"/k8s/style_*.json; do
    [ -f "${source_style}" ] || continue
    style_files+=("--from-file=$(basename "${source_style}")=${source_style}")
  done
  if [ "${#style_files[@]}" -gt 0 ]; then
    kubectl -n "${NAMESPACE}" create configmap tileserver-style "${style_files[@]}" --dry-run=client -o yaml | kubectl apply -f -
  fi
fi

echo "Prepared TileServer bootstrap assets in ${BOOTSTRAP_DIR}"
