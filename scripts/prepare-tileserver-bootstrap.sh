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

for style_file in style.json dark_style.json blue_style.json futuristic_style.json; do
  if [ -f "${REPO_ROOT}/k8s/${style_file}" ]; then
    ${SUDO} cp "${REPO_ROOT}/k8s/${style_file}" "${BOOTSTRAP_DIR}/${style_file}"
  else
    ${SUDO} rm -f "${BOOTSTRAP_DIR:?}/${style_file}"
  fi
done

styles_json='"osm":{"style":"/data/style.json"}'
if [ -f "${BOOTSTRAP_DIR}/dark_style.json" ]; then
  styles_json="${styles_json},\"osm-dark\":{\"style\":\"/data/dark_style.json\"}"
fi
if [ -f "${BOOTSTRAP_DIR}/blue_style.json" ]; then
  styles_json="${styles_json},\"osm-blue\":{\"style\":\"/data/blue_style.json\"}"
fi
if [ -f "${BOOTSTRAP_DIR}/futuristic_style.json" ]; then
  styles_json="${styles_json},\"osm-futuristic\":{\"style\":\"/data/futuristic_style.json\"}"
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
  if [ -f "${REPO_ROOT}/k8s/style.json" ]; then
    style_files=("--from-file=style.json=${REPO_ROOT}/k8s/style.json")
    if [ -f "${REPO_ROOT}/k8s/dark_style.json" ]; then
      style_files+=("--from-file=dark_style.json=${REPO_ROOT}/k8s/dark_style.json")
    fi
    if [ -f "${REPO_ROOT}/k8s/blue_style.json" ]; then
      style_files+=("--from-file=blue_style.json=${REPO_ROOT}/k8s/blue_style.json")
    fi
    if [ -f "${REPO_ROOT}/k8s/futuristic_style.json" ]; then
      style_files+=("--from-file=futuristic_style.json=${REPO_ROOT}/k8s/futuristic_style.json")
    fi
    kubectl -n "${NAMESPACE}" create configmap tileserver-style "${style_files[@]}" --dry-run=client -o yaml | kubectl apply -f -
  fi
fi

echo "Prepared TileServer bootstrap assets in ${BOOTSTRAP_DIR}"
