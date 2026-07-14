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
  local timestamp
  timestamp="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  printf '%s %s\n' "$timestamp" "$message" | tee -a "${LOG_FILE}"
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

nominatim_pod_diagnostics() {
  local pod_name status_lines message
  pod_name="$(kubectl -n "${NAMESPACE}" get pod -l app=nominatim -o jsonpath='{.items[-1].metadata.name}' 2>/dev/null || true)"
  if [ -z "${pod_name}" ]; then
    return 1
  fi

  status_lines="$(kubectl -n "${NAMESPACE}" get pod "${pod_name}" -o jsonpath='{range .status.containerStatuses[*]}{.name}{"|"}{.state.waiting.reason}{"|"}{.state.waiting.message}{"|"}{.state.terminated.reason}{"|"}{.state.terminated.message}{"|"}{.lastState.terminated.reason}{"|"}{.lastState.terminated.message}{"|"}{.restartCount}{"\n"}{end}' 2>/dev/null || true)"
  if [ -z "${status_lines}" ]; then
    return 1
  fi

  while IFS='|' read -r container waiting waiting_msg terminated terminated_msg last_reason last_msg restart_count; do
    case "${waiting}" in
      CrashLoopBackOff|ImagePullBackOff|ErrImagePull|CreateContainerConfigError|RunContainerError)
        message="${waiting_msg:-No additional message.}"
        printf 'pod %s: container %s is %s after %s restart(s). %s\n' \
          "${pod_name}" "${container:-nominatim}" "${waiting}" "${restart_count:-0}" "${message}"
        return 0
        ;;
    esac

    if [ "${terminated}" = "Error" ] || [ "${terminated}" = "OOMKilled" ] || [ "${last_reason}" = "Error" ] || [ "${last_reason}" = "OOMKilled" ]; then
      if [[ "${restart_count:-}" =~ ^[0-9]+$ ]] && [ "${restart_count}" -gt 0 ]; then
        message="${terminated_msg:-}"
        if [ -z "${message}" ]; then
          message="${last_msg:-}"
        fi
        if [ -z "${message}" ]; then
          message="No additional message."
        fi
        printf 'pod %s: container %s terminated with %s after %s restart(s). %s\n' \
          "${pod_name}" "${container:-nominatim}" "${terminated:-$last_reason}" "${restart_count:-0}" "${message}"
        return 0
      fi
    fi
  done <<EOF
${status_lines}
EOF

  return 1
}

wait_for_nominatim_rollout() {
  local deadline_seconds="${1:-600}"
  local start elapsed crash_detail
  start="$(date +%s)"
  while true; do
    check_config_change
    if crash_detail="$(nominatim_pod_diagnostics)"; then
      log "Detected a Nominatim startup crash: ${crash_detail}"
      write_state false "failed" 0 "Nominatim failed to start." "The imported database appears to be crash-looping. ${crash_detail}"
      return 1
    fi
    if kubectl -n "${NAMESPACE}" wait --for=condition=available "deployment/nominatim" --timeout=30s >/dev/null 2>&1; then
      return 0
    fi
    elapsed="$(( $(date +%s) - start ))"
    if [ "${elapsed}" -gt "${deadline_seconds}" ]; then
      crash_detail="$(nominatim_pod_diagnostics || true)"
      if [ -n "${crash_detail}" ]; then
        log "Timed out waiting for Nominatim after ${deadline_seconds}s. Latest diagnostics: ${crash_detail}"
        write_state false "failed" 0 "Nominatim failed to start." "${crash_detail}"
      else
        log "Timed out waiting for Nominatim after ${deadline_seconds}s."
        write_state false "failed" 0 "Nominatim failed to start." "Timed out waiting for deployment/nominatim to become available."
      fi
      return 1
    fi
    sleep 10
  done
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
    log "Detected config change in ${CONFIG_DIR}; exiting so Kubernetes restarts the orchestrator pod."
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
    local elapsed
    elapsed="$(($(date +%s) - start))"
    if kubectl -n "${NAMESPACE}" get "job/${job_name}" >/dev/null 2>&1; then
      if kubectl -n "${NAMESPACE}" get "job/${job_name}" -o jsonpath='{.status.failed}' 2>/dev/null | grep -q '^[1-9]'; then
        kubectl -n "${NAMESPACE}" logs "job/${job_name}" --tail=200 >&2 || true
        return 1
      fi
    fi
    if [ "${elapsed}" -gt "${deadline_seconds}" ]; then
      log "Timed out waiting for ${job_name} after ${deadline_seconds}s."
      return 1
    fi
    sleep 20
  done
}

prepare_import_data() {
  local job_name="import-prep"
  log "Starting import preparation job."
  write_state true "download" 20 "Preparing map downloads ..." "Downloading selected extract(s) and merging them."
  kubectl -n "${NAMESPACE}" delete job "${job_name}" --ignore-not-found >/dev/null 2>&1 || true
  kubectl -n "${NAMESPACE}" apply -f - <<EOF >/dev/null
apiVersion: batch/v1
kind: Job
metadata:
  name: import-prep
  namespace: ${NAMESPACE}
spec:
  backoffLimit: 1
  ttlSecondsAfterFinished: 3600
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: import-prep
          image: python:3.12-slim
          command: ["/bin/sh", "-c"]
          args:
            - |
              set -euo pipefail
              export DEBIAN_FRONTEND=noninteractive
              apt-get update >/dev/null
              apt-get install -y --no-install-recommends osmium-tool >/dev/null
              python3 - <<'PY'
              import json
              import os
              import shutil
              import subprocess
              import urllib.request
              from pathlib import Path

              data_dir = Path('/data')
              request_file = data_dir / 'status' / 'import-request.json'
              with request_file.open('r', encoding='utf-8') as handle:
                  request = json.load(handle)
              countries = request.get('countries') or []
              if not countries:
                  raise SystemExit('No countries requested')

              library_dir = data_dir / 'library'
              import_dir = data_dir / 'import'
              library_dir.mkdir(parents=True, exist_ok=True)
              import_dir.mkdir(parents=True, exist_ok=True)

              paths = []
              for country in countries:
                  slug = country['slug']
                  url = country['url']
                  dest = library_dir / f'{slug}.osm.pbf'
                  temp = library_dir / f'{slug}.osm.pbf.part'
                  if not dest.exists() or dest.stat().st_size <= 0:
                      req = urllib.request.Request(url, headers={'User-Agent': 'localosm-import-orchestrator/1.0'})
                      with urllib.request.urlopen(req, timeout=300) as response, temp.open('wb') as handle:
                          shutil.copyfileobj(response, handle)
                      os.replace(temp, dest)
                  paths.append(str(dest))

              merged = import_dir / 'planet.osm.pbf'
              temp_merged = import_dir / 'planet.osm.pbf.tmp'
              if len(paths) == 1:
                  shutil.copyfile(paths[0], temp_merged)
              else:
                  try:
                      subprocess.run(['osmium', 'merge', '--overwrite', '-o', str(temp_merged), '-f', 'pbf', *paths], check=True)
                  except subprocess.CalledProcessError as exc:
                      joined = ', '.join(country['slug'] for country in countries)
                      raise RuntimeError(f'Could not merge selected countries: {joined}') from exc
                  # Country extracts are downloaded independently and can be
                  # generated/cached at different times, so shared border
                  # nodes/ways can end up with different edit versions in each
                  # file. "osmium merge" (the correct tool for this, as
                  # opposed to "osmium cat") keeps every distinct version it
                  # finds instead of dropping one, so the merged file can
                  # still contain the same node/way/relation ID more than
                  # once. osm2pgsql (used by the Nominatim import) is not
                  # history-aware and aborts with "Input data is not ordered:
                  # ... appears more than once" in that case. Collapsing the
                  # merged file with "osmium time-filter" (no explicit
                  # timestamp = current point in time) keeps only the latest
                  # version of each object, guaranteeing a clean,
                  # duplicate-free extract regardless of how stale the
                  # individual cached/downloaded files were relative to each
                  # other.
                  dedup_merged = import_dir / 'planet.osm.pbf.dedup'
                  try:
                      subprocess.run(['osmium', 'time-filter', '--overwrite', '-o', str(dedup_merged), '-f', 'pbf', str(temp_merged)], check=True)
                  except subprocess.CalledProcessError as exc:
                      joined = ', '.join(country['slug'] for country in countries)
                      raise RuntimeError(f'Could not deduplicate merged extract for: {joined}') from exc
                  os.replace(dedup_merged, temp_merged)
              try:
                  subprocess.run(['osmium', 'fileinfo', '-F', 'pbf', str(temp_merged)], check=True)
              except subprocess.CalledProcessError as exc:
                  raise RuntimeError(f'Invalid merged PBF: {temp_merged}') from exc
              os.replace(temp_merged, merged)

              countries_path = data_dir / 'status' / 'countries.json'
              try:
                  with countries_path.open('r', encoding='utf-8') as handle:
                      records = json.load(handle)
              except Exception:
                  records = []
              if isinstance(records, list):
                  slug_set = {country['slug'] for country in countries if country.get('slug')}
                  changed = False
                  for record in records:
                      slug = record.get('slug')
                      if slug in slug_set:
                          dest = library_dir / f'{slug}.osm.pbf'
                          record['status'] = 'ready'
                          record['imported_at'] = request.get('requested_at', '')
                          record['pbf_path'] = str(dest)
                          record['pbf_size_mb'] = round(dest.stat().st_size / (1024 * 1024), 1) if dest.exists() else None
                          record['last_error'] = ''
                          changed = True
                  if changed:
                      with countries_path.open('w', encoding='utf-8') as handle:
                          json.dump(records, handle, indent=2, sort_keys=True)

              meta = import_dir / 'planet.osm.pbf.meta'
              with meta.open('w', encoding='utf-8') as handle:
                  handle.write(f"requested_at={request.get('requested_at', '')}\\n")
                  handle.write(f"countries={','.join(country['slug'] for country in countries)}\\n")
                  handle.write(f"count={len(countries)}\\n")
              PY
          volumeMounts:
            - name: osm-data
              mountPath: /data
      volumes:
        - name: osm-data
          hostPath:
            path: /mnt/data/OSM
            type: DirectoryOrCreate
EOF
  if ! wait_for_job "${job_name}"; then
    write_state false "failed" 0 "Map download failed." "See job logs for details."
    return 1
  fi
  write_state true "download" 55 "Map downloads completed." "Selected country extract(s) were downloaded and merged."
  return 0
}

request_has_countries() {
  python3 - "${REQUEST_FILE}" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, 'r', encoding='utf-8') as handle:
    request = json.load(handle)
sys.exit(0 if request.get('countries') else 1)
PY
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
  if [ "${deployment}" = "nominatim" ]; then
    if ! wait_for_nominatim_rollout 600; then
      return 1
    fi
  else
    if ! kubectl -n "${NAMESPACE}" rollout status "deployment/${deployment}" --timeout=600s >/dev/null; then
      return 1
    fi
  fi
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
  if ! swap_stage "${service}" "${active_dir}" "${staging_dir}" "${deployment}"; then
    write_state false "failed" 0 "${service} promotion failed." "Rollout of deployment/${deployment} did not become ready."
    return 1
  fi
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
    write_state true "queued" 5 "Import request received." "Running TileServer, Nominatim and Valhalla sequentially."

    if request_has_countries; then
      if ! prepare_import_data; then
        continue
      fi
    fi

    if ! run_step "tileserver" "tileserver-import" "tileserver-import-job.yaml" "${DATA_DIR}/tileserver/active" "${DATA_DIR}/tileserver/staging" "tileserver-gl"; then
      continue
    fi
    if ! run_step "nominatim" "nominatim-import" "nominatim-import-job.yaml" "${DATA_DIR}/nominatim/active" "${DATA_DIR}/nominatim/staging" "nominatim"; then
      continue
    fi
    if ! run_step "valhalla" "valhalla-import" "valhalla-import-job.yaml" "${DATA_DIR}/valhalla/active" "${DATA_DIR}/valhalla/staging" "valhalla"; then
      continue
    fi

    rm -f "${REQUEST_FILE}"
    write_state false "done" 100 "Import erfolgreich abgeschlossen." "Alle Daten wurden sequentiell verarbeitet und aktiviert."
    log "Import request completed successfully."
  done
}

main "$@"
