#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${OSM_NAMESPACE:-osm}"
DATA_DIR="${OSM_DATA_DIR:-/mnt/data/OSM}"
MANIFEST_DIR="${OSM_MANIFEST_DIR:-/manifests}"
STATE_DIR="${OSM_STATE_DIR:-/state}"
CONFIG_DIR="${OSM_CONFIG_DIR:-/config}"
LOG_FILE="${STATE_DIR}/import-orchestrator.log"
STATE_FILE="${STATE_DIR}/import-orchestrator.json"
HASH_FILE="${STATE_DIR}/import-orchestrator.hash"
REQUEST_FILE="${STATE_DIR}/import-request.json"
LAST_CONFIG_CHECK=0
CONFIG_CHECK_INTERVAL=60

mkdir -p "${STATE_DIR}"

log() {
  local message="$1"
  printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$message" | tee -a "${LOG_FILE}"
}

write_state() {
  local running="$1" phase="$2" progress="$3" message="$4" detail="$5"
  python3 - "$STATE_FILE" "$running" "$phase" "$progress" "$message" "$detail" <<'PY'
import json
import sys
from datetime import datetime, timezone

path, running, phase, progress, message, detail = sys.argv[1:7]
payload = {
    'running': running == 'true',
    'phase': phase,
    'progress': int(progress),
    'message': message,
    'detail': detail,
    'updated_at': datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z'),
}
with open(path, 'w', encoding='utf-8') as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
PY
}

config_hash() {
  if ! find "${CONFIG_DIR}" -type f >/dev/null 2>&1; then
    echo "no-config"
    return 0
  fi
  find "${CONFIG_DIR}" -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum | awk '{print $1}'
}

check_config_change() {
  local now current previous
  now="$(date +%s)"
  if [ $((now - LAST_CONFIG_CHECK)) -lt "${CONFIG_CHECK_INTERVAL}" ] && [ -f "${HASH_FILE}" ]; then
    return 0
  fi
  LAST_CONFIG_CHECK="${now}"
  current="$(config_hash)"
  previous=""
  if [ -f "${HASH_FILE}" ]; then
    previous="$(cat "${HASH_FILE}")"
  fi
  if [ -n "${previous}" ] && [ "${previous}" != "${current}" ]; then
    echo "${current}" > "${HASH_FILE}"
    log "Detected config change; exiting so Kubernetes restarts the orchestrator pod."
    write_state false "idle" 0 "Konfiguration geändert." "Import-Orchestrator beendet sich selbst und wird von k3s neu gestartet."
    exit 0
  fi
  echo "${current}" > "${HASH_FILE}"
}

wait_for_job() {
  local job_name="$1"
  local deadline_seconds="${2:-86400}"
  local start
  start="$(date +%s)"
  while true; do
    check_config_change
    if kubectl -n "${NAMESPACE}" wait --for=condition=complete "job/${job_name}" --timeout=30s >/dev/null 2>&1; then
      return 0
    fi
    if kubectl -n "${NAMESPACE}" get "job/${job_name}" >/dev/null 2>&1; then
      if kubectl -n "${NAMESPACE}" get "job/${job_name}" -o jsonpath='{.status.failed}' 2>/dev/null | grep -q '^[1-9]'; then
        kubectl -n "${NAMESPACE}" logs "job/${job_name}" --tail=200 >&2 || true
        return 1
      fi
    fi
    if [ $(($(date +%s) - start)) -gt "${deadline_seconds}" ]; then
      log "Timed out waiting for ${job_name} after ${deadline_seconds}s."
      return 1
    fi
    sleep 20
  done
}

swap_stage() {
  local service="$1"
  local active_dir="$2"
  local staging_dir="$3"
  local deployment="$4"
  if [ -d "${active_dir}" ]; then
    rm -rf "${active_dir}.old" 2>/dev/null || true
    mv "${active_dir}" "${active_dir}.old" 2>/dev/null || true
  fi
  mv "${staging_dir}" "${active_dir}"
  rm -rf "${active_dir}.old" 2>/dev/null || true
  kubectl -n "${NAMESPACE}" rollout restart "deployment/${deployment}" >/dev/null
  kubectl -n "${NAMESPACE}" rollout status "deployment/${deployment}" --timeout=600s >/dev/null
  log "Swapped ${service} staging data into production."
}

run_step() {
  local service="$1" job_name="$2" manifest="$3" active_dir="$4" staging_dir="$5" deployment="$6"
  check_config_change
  log "Starting ${service} import job."
  write_state true "${service}" 10 "${service} import started." "Submitting ${job_name}."
  kubectl -n "${NAMESPACE}" delete job "${job_name}" --ignore-not-found >/dev/null 2>&1 || true
  kubectl -n "${NAMESPACE}" apply -f "${MANIFEST_DIR}/${manifest}" >/dev/null
  if ! wait_for_job "${job_name}"; then
    write_state false "failed" 0 "${service} import failed." "See job logs for details."
    return 1
  fi
  write_state true "${service}" 90 "${service} import completed." "Promoting staged data."
  swap_stage "${service}" "${active_dir}" "${staging_dir}" "${deployment}"
  write_state false "${service}" 100 "${service} import promoted." "${service} is serving the newly imported data."
}

main() {
  log "Import orchestrator started."
  write_state false "idle" 0 "Import-Orchestrator bereit." "Warte auf einen Import-Request."

  while true; do
    check_config_change
    if [ ! -s "${REQUEST_FILE}" ]; then
      sleep 15
      continue
    fi

    log "Detected import request at ${REQUEST_FILE}."
    write_state true "queued" 5 "Import request received." "Running Nominatim, Valhalla and TileServer sequentially."

    run_step "nominatim" "nominatim-import" "nominatim-import-job.yaml" "${DATA_DIR}/nominatim/active" "${DATA_DIR}/nominatim/staging" "nominatim"
    run_step "valhalla" "valhalla-import" "valhalla-import-job.yaml" "${DATA_DIR}/valhalla/active" "${DATA_DIR}/valhalla/staging" "valhalla"
    run_step "tileserver" "tileserver-import" "tileserver-import-job.yaml" "${DATA_DIR}/tileserver/active" "${DATA_DIR}/tileserver/staging" "tileserver-gl"

    rm -f "${REQUEST_FILE}"
    write_state false "done" 100 "Import erfolgreich abgeschlossen." "Alle Daten wurden sequentiell verarbeitet und aktiviert."
    log "Import request completed successfully."
  done
}

main "$@"
