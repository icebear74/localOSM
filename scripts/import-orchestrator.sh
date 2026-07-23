#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${OSM_NAMESPACE:-osm}"
DATA_DIR="${OSM_DATA_DIR:-/mnt/data/OSM}"
TEMP_DIR="${OSM_TEMP_DIR:-/mnt/data/OSM/TempDir}"
MANIFEST_DIR="${OSM_MANIFEST_DIR:-/manifests}"
STATE_DIR="${OSM_STATE_DIR:-/state}"
CONFIG_DIR="${OSM_CONFIG_DIR:-/config}"
LOG_FILE="${STATE_DIR}/import-orchestrator.log"
STATE_FILE="${STATE_DIR}/import-orchestrator.json"
HASH_FILE="${STATE_DIR}/import-orchestrator.hash"
REQUEST_FILE="${STATE_DIR}/import-request.json"
ABORT_FILE="${STATE_DIR}/import-abort.flag"
COMPLETED_STEPS_FILE="${STATE_DIR}/import-completed-steps"
LAST_CONFIG_CHECK=0
CONFIG_CHECK_INTERVAL=60
MV_SUPPORTS_T=false
POD_TERMINATION_TIMEOUT_SECONDS=180

if mv --help 2>&1 | grep -q -- ' -T'; then
  MV_SUPPORTS_T=true
fi

mkdir -p "${STATE_DIR}"

# Removes everything *inside* a scratch directory on the (user-provided,
# fast/SSD-backed) temp-dir mount without ever removing the directory (mount
# point) itself, so temporary import data never lingers once a step is done.
cleanup_temp_dir() {
  local dir="$1"
  [ -d "${dir}" ] || return 0
  find "${dir}" -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null || true
}

log() {
  local message="$1"
  local timestamp
  timestamp="$(date '+%Y-%m-%dT%H:%M:%S%z')"
  printf '%s %s\n' "$timestamp" "$message" | tee -a "${LOG_FILE}"
}

abort_requested() {
  [ -f "${ABORT_FILE}" ]
}

clear_abort_flag() {
  rm -f "${ABORT_FILE}" 2>/dev/null || true
}

# Tracks which import steps (tileserver/nominatim/valhalla) have already been
# completed for the *current* request, so that a container/pod restart in the
# middle of a multi-step import (e.g. after Valhalla finished but before the
# loop reached its end) resumes instead of redoing already-promoted steps.
request_fingerprint() {
  sha256sum "${REQUEST_FILE}" 2>/dev/null | awk '{print $1}'
}

# Ensures the completed-steps tracker matches the request currently being
# processed. If the request file changed (new/edited request) since the
# tracker was last written, the tracker is reset so no stale step is skipped.
sync_completed_steps() {
  local current_fp stored_fp
  current_fp="$(request_fingerprint)"
  stored_fp=""
  if [ -f "${COMPLETED_STEPS_FILE}" ]; then
    stored_fp="$(head -n1 "${COMPLETED_STEPS_FILE}" 2>/dev/null || true)"
  fi
  if [ "${stored_fp}" != "${current_fp}" ]; then
    printf '%s\n' "${current_fp}" > "${COMPLETED_STEPS_FILE}"
  fi
}

step_already_done() {
  local step="$1"
  [ -f "${COMPLETED_STEPS_FILE}" ] || return 1
  tail -n +2 "${COMPLETED_STEPS_FILE}" | grep -qxF "${step}"
}

mark_step_done() {
  local step="$1"
  echo "${step}" >> "${COMPLETED_STEPS_FILE}"
}

clear_completed_steps() {
  rm -f "${COMPLETED_STEPS_FILE}" 2>/dev/null || true
}

write_state() {
  local running="$1" phase="$2" progress="$3" message="$4" detail="$5"
  python3 - "$STATE_FILE" "$running" "$phase" "$progress" "$message" "$detail" <<'PY'
import json
import sys
from datetime import datetime

path, running, phase, progress, message, detail = sys.argv[1:7]
payload = {
    'running': running == 'true',
    'phase': phase,
    'progress': int(progress),
    'message': message,
    'detail': detail,
    'updated_at': datetime.now().astimezone().replace(microsecond=0).isoformat(),
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
  local deadline_seconds="${2:-252200}"
  local start
  start="$(date +%s)"
  while true; do
    check_config_change
    if abort_requested; then
      log "Abort requested; deleting job ${job_name} and its pods."
      kubectl -n "${NAMESPACE}" delete job "${job_name}" --ignore-not-found --wait=false >/dev/null 2>&1 || true
      return 2
    fi
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

deployment_selector() {
  local deployment="$1"
  local selector
  selector="$(kubectl -n "${NAMESPACE}" get "deployment/${deployment}" -o jsonpath='{range $k,$v := .spec.selector.matchLabels}{printf "%s=%s," $k $v}{end}' 2>/dev/null || true)"
  selector="${selector%,}"
  if [ -z "${selector}" ]; then
    selector="app=${deployment}"
  fi
  echo "${selector}"
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
          resources:
            requests:
              cpu: "1"
              memory: "2Gi"
            limits:
              cpu: "6"
              memory: "10Gi"
          command: ["/bin/sh", "-c"]
          args:
            - |
              set -euo pipefail
              export DEBIAN_FRONTEND=noninteractive
              echo "Installing osmium-tool ..."
              apt-get update >/dev/null
              apt-get install -y --no-install-recommends osmium-tool >/dev/null
              echo "osmium-tool installed."
              # -u: unbuffered stdout so progress prints show up live in kubectl logs.
              python3 -u - <<'PY'
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

              # Countries whose cached extract must be re-downloaded even if a
              # local copy already exists (set by the status dashboard's
              # "Download & Merge"/"Nur Download" actions after comparing the
              # remote Last-Modified header against the last download).
              force_slugs = set(request.get('force_slugs') or [])
              # Whether the merged planet.osm.pbf should be (re)built at all;
              # "Nur Download" auto-update/manual runs set this to false so
              # only the per-country cache is refreshed.
              merge_requested = bool(request.get('merge_requested', True))

              library_dir = data_dir / 'library'
              import_dir = data_dir / 'import'
              library_dir.mkdir(parents=True, exist_ok=True)
              import_dir.mkdir(parents=True, exist_ok=True)

              paths = []
              any_downloaded = False
              downloaded_slugs = set()
              for country in countries:
                  slug = country['slug']
                  url = country['url']
                  dest = library_dir / f'{slug}.osm.pbf'
                  temp = library_dir / f'{slug}.osm.pbf.part'
                  needs_download = not dest.exists() or dest.stat().st_size <= 0 or slug in force_slugs
                  if needs_download:
                      print(f'Downloading {slug} from {url} ...')
                      req = urllib.request.Request(url, headers={'User-Agent': 'localosm-import-orchestrator/1.0'})
                      with urllib.request.urlopen(req, timeout=300) as response, temp.open('wb') as handle:
                          shutil.copyfileobj(response, handle)
                      os.replace(temp, dest)
                      any_downloaded = True
                      downloaded_slugs.add(slug)
                      print(f'Downloaded {slug}.')
                  else:
                      print(f'Using cached extract for {slug}.')
                  paths.append(str(dest))

              merged = import_dir / 'planet.osm.pbf'
              temp_merged = import_dir / 'planet.osm.pbf.tmp'
              merge_performed = False
              if not merge_requested:
                  print('Merge not requested; skipping merge step (download-only run).')
              elif not any_downloaded and merged.exists() and merged.stat().st_size > 0:
                  # Requirement: only (re)merge when at least one extract was
                  # actually refreshed; otherwise reuse the existing merged
                  # file untouched to avoid needless work.
                  print('No country extract changed since the last merge; reusing existing planet.osm.pbf.')
              else:
                  if len(paths) == 1:
                      print('Only one extract selected; copying it directly.')
                      shutil.copyfile(paths[0], temp_merged)
                  else:
                      print(f'Merging {len(paths)} extracts ...')
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
                      dedup_output_path = import_dir / 'planet.osm.pbf.dedup'
                      print('Deduplicating merged extract ...')
                      try:
                          subprocess.run(['osmium', 'time-filter', '--overwrite', '-o', str(dedup_output_path), '-f', 'pbf', '-F', 'pbf', str(temp_merged)], check=True)
                      except subprocess.CalledProcessError as exc:
                          joined = ', '.join(country['slug'] for country in countries)
                          raise RuntimeError(f'Could not deduplicate merged extract for: {joined}') from exc
                      os.replace(dedup_output_path, temp_merged)
                  print('Validating merged extract ...')
                  try:
                      subprocess.run(['osmium', 'fileinfo', '-F', 'pbf', str(temp_merged)], check=True)
                  except subprocess.CalledProcessError as exc:
                      raise RuntimeError(f'Invalid merged PBF: {temp_merged}') from exc
                  os.replace(temp_merged, merged)
                  merge_performed = True
                  print('Merged extract ready.')

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
                          if slug in downloaded_slugs:
                              record['downloaded_at'] = request.get('requested_at', '')
                              record['update_available'] = False
                          changed = True
              if isinstance(records, list) and changed:
                  with countries_path.open('w', encoding='utf-8') as handle:
                      json.dump(records, handle, indent=2, sort_keys=True)

              if merge_performed:
                  meta = import_dir / 'planet.osm.pbf.meta'
                  with meta.open('w', encoding='utf-8') as handle:
                      handle.write(f"requested_at={request.get('requested_at', '')}\\n")
                      handle.write(f"countries={','.join(country['slug'] for country in countries)}\\n")
                      handle.write(f"count={len(countries)}\\n")
              PY

          volumeMounts:
            - name: osm-data
              mountPath: /data
            - name: temp-data
              mountPath: /data/import
      volumes:
        - name: osm-data
          hostPath:
            path: ${DATA_DIR}
            type: DirectoryOrCreate
        - name: temp-data
          hostPath:
            path: ${TEMP_DIR}/import
            type: DirectoryOrCreate
EOF
  local rc=0
  wait_for_job "${job_name}" || rc=$?
  if [ "${rc}" -eq 2 ]; then
    write_state false "aborted" 0 "Map download aborted." "Import wurde vom Benutzer abgebrochen."
    return 2
  elif [ "${rc}" -ne 0 ]; then
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

# Space-separated list of steps (subset of "tileserver nominatim valhalla")
# that should be executed for the current request. Missing/empty "steps"
# means "run all three", which keeps older/manual requests (e.g. written by
# scripts/run-import.sh) working unchanged.
request_steps() {
  python3 - "${REQUEST_FILE}" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, 'r', encoding='utf-8') as handle:
    request = json.load(handle)
# Missing "steps" key means "run all three" (back-compat with older/manual
# requests); an explicitly empty list means "run no build steps at all"
# (used by the dedicated Download & Merge / download-only workflows) and
# must NOT fall back to the default here.
if 'steps' in request:
    steps = request.get('steps') or []
else:
    steps = ['tileserver', 'nominatim', 'valhalla']
print(' '.join(steps))
PY
}

# Whether a successfully imported step should be swapped into production
# automatically. Missing "auto_promote" defaults to true for backward
# compatibility with older/manual requests.
request_auto_promote() {
  python3 - "${REQUEST_FILE}" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, 'r', encoding='utf-8') as handle:
    request = json.load(handle)
print('true' if request.get('auto_promote', True) else 'false')
PY
}

step_requested() {
  local step="$1"
  case " ${REQUESTED_STEPS} " in
    *" ${step} "*) return 0 ;;
    *) return 1 ;;
  esac
}

swap_stage() {
  local service="$1"
  local active_dir="$2"
  local staging_dir="$3"
  local deployment="$4"
  local backup_dir="${active_dir}.old"
  local mv_err
  local wait_start
  local selector

  if [ ! -d "${staging_dir}" ]; then
    log "Staging directory ${staging_dir} not found; cannot promote ${service}."
    return 1
  fi

  if abort_requested; then
    log "Abort requested before promoting ${service}; skipping swap."
    return 2
  fi

  log "Scaling deployment/${deployment} down to 0 before promoting ${service} to release file locks."
  if ! kubectl -n "${NAMESPACE}" scale "deployment/${deployment}" --replicas=0 >/dev/null; then
    log "Failed to scale deployment/${deployment} down before promoting ${service}."
    return 1
  fi

  wait_start="$(date +%s)"
  selector="$(deployment_selector "${deployment}")"
  while kubectl -n "${NAMESPACE}" get pods -l "${selector}" --no-headers 2>/dev/null | grep -q .; do
    check_config_change
    if abort_requested; then
      log "Abort requested while waiting for deployment/${deployment} pods to terminate."
      kubectl -n "${NAMESPACE}" scale "deployment/${deployment}" --replicas=1 >/dev/null 2>&1 || true
      return 2
    fi
    if [ "$(( $(date +%s) - wait_start ))" -gt "${POD_TERMINATION_TIMEOUT_SECONDS}" ]; then
      log "Timed out waiting for deployment/${deployment} pods to terminate before promoting ${service}."
      return 1
    fi
    sleep 3
  done

  rm -rf "${backup_dir}" 2>/dev/null || true
  if [ -d "${active_dir}" ]; then
    if ! mv_err="$(mv "${active_dir}" "${backup_dir}" 2>&1)"; then
      log "Failed to back up existing ${active_dir} before promoting ${service}: ${mv_err}"
      return 1
    fi
  fi

  local mv_cmd=(mv -T "${staging_dir}" "${active_dir}")
  if [ "${MV_SUPPORTS_T}" != "true" ]; then
    if [ -e "${active_dir}" ]; then
      log "Fallback mv requested but ${active_dir} still exists; refusing non-atomic promote for ${service}."
      return 1
    fi
    mv_cmd=(mv "${staging_dir}" "${active_dir}")
  fi
  if ! mv_err="$("${mv_cmd[@]}" 2>&1)"; then
    log "Failed to move staged ${service} data from ${staging_dir} to ${active_dir}: ${mv_err}"
    # Restore the previous good data so the service keeps serving it instead
    # of being left without any active data at all.
    if [ -d "${backup_dir}" ]; then
      rm -rf "${active_dir}" 2>/dev/null || true
      mv "${backup_dir}" "${active_dir}" 2>/dev/null || true
    fi
    return 1
  fi

  if [ "${service}" = "nominatim" ] && [ ! -f "${active_dir}/import-finished" ]; then
    log "import-finished marker missing after promoting ${service}; recreating ${active_dir}/import-finished."
    touch "${active_dir}/import-finished"
  fi

  rm -rf "${backup_dir}" 2>/dev/null || true
  # The temp dir is scratch space only; once the promoted data has been moved
  # into OSM/<service>/active, nothing must remain behind on the temp mount.
  cleanup_temp_dir "$(dirname "${staging_dir}")"

  log "Scaling deployment/${deployment} back to 1 after promoting ${service}."
  if ! kubectl -n "${NAMESPACE}" scale "deployment/${deployment}" --replicas=1 >/dev/null; then
    log "Failed to scale deployment/${deployment} back up after promoting ${service}."
    return 1
  fi
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
  return 0
}

run_step() {
  local service="$1" job_name="$2" manifest="$3" active_dir="$4" staging_dir="$5" deployment="$6"
  local auto_promote="${7:-true}"
  check_config_change
  # Start from a clean temp-dir scratch area in case a previous run crashed
  # before it could clean up after itself.
  cleanup_temp_dir "$(dirname "${staging_dir}")"
  log "Starting ${service} import job."
  write_state true "${service}" 10 "${service} import started." "Submitting ${job_name}."
  kubectl -n "${NAMESPACE}" delete job "${job_name}" --ignore-not-found >/dev/null 2>&1 || true
  kubectl -n "${NAMESPACE}" apply -f "${MANIFEST_DIR}/${manifest}" >/dev/null
  local rc=0
  wait_for_job "${job_name}" || rc=$?
  if [ "${rc}" -eq 2 ]; then
    write_state false "aborted" 0 "${service} import aborted." "Import wurde vom Benutzer abgebrochen."
    cleanup_temp_dir "$(dirname "${staging_dir}")"
    return 2
  elif [ "${rc}" -ne 0 ]; then
    write_state false "failed" 0 "${service} import failed." "See job logs for details."
    cleanup_temp_dir "$(dirname "${staging_dir}")"
    return 1
  fi
  if [ "${auto_promote}" != "true" ]; then
    log "${service} import staged; auto-promote disabled, leaving deployment/${deployment} untouched."
    write_state false "${service}" 100 "${service} import staged." "Staged data is ready; promote it manually when you are ready."
    return 0
  fi
  write_state true "${service}" 90 "${service} import completed." "Promoting staged data."
  local prc=0
  swap_stage "${service}" "${active_dir}" "${staging_dir}" "${deployment}" || prc=$?
  if [ "${prc}" -eq 2 ]; then
    write_state false "aborted" 0 "${service} promotion aborted." "Import wurde vom Benutzer abgebrochen."
    cleanup_temp_dir "$(dirname "${staging_dir}")"
    return 2
  elif [ "${prc}" -ne 0 ]; then
    write_state false "failed" 0 "${service} promotion failed." "Rollout of deployment/${deployment} did not become ready."
    cleanup_temp_dir "$(dirname "${staging_dir}")"
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
      # No request pending: any leftover abort flag from a previous run is
      # stale and must not affect the next request.
      clear_abort_flag
      sleep 15
      continue
    fi

    log "Detected import request at ${REQUEST_FILE}."
    write_state true "queued" 5 "Import request received." "Running TileServer, Nominatim and Valhalla sequentially."
    # Reconcile the completed-steps tracker with this request so that a
    # container/pod restart in the middle of the sequential pipeline resumes
    # at the next pending step instead of redoing already-promoted ones.
    sync_completed_steps
    REQUESTED_STEPS="$(request_steps)"
    AUTO_PROMOTE="$(request_auto_promote)"
    log "Requested steps: ${REQUESTED_STEPS} (auto_promote=${AUTO_PROMOTE})."

    if request_has_countries; then
      local prc=0
      prepare_import_data || prc=$?
      if [ "${prc}" -ne 0 ]; then
        if [ "${prc}" -eq 2 ]; then
          log "Import aborted by user during preparation; discarding import request."
        else
          log "Import preparation failed; discarding import request to avoid retrying the same failing request in a loop."
        fi
        cleanup_temp_dir "${TEMP_DIR}/import"
        rm -f "${REQUEST_FILE}"
        clear_abort_flag
        clear_completed_steps
        continue
      fi
    fi

    local src=0
    if ! step_requested "tileserver"; then
      log "TileServer step not requested for this import request; skipping."
    elif step_already_done "tileserver"; then
      log "TileServer already promoted for this request (resumed after restart); skipping re-import."
    else
      run_step "tileserver" "tileserver-import" "tileserver-import-job.yaml" "${DATA_DIR}/tileserver/active" "${TEMP_DIR}/tileserver/staging" "tileserver-gl" "${AUTO_PROMOTE}" || src=$?
      if [ "${src}" -ne 0 ]; then
        if [ "${src}" -eq 2 ]; then
          log "Import aborted by user during TileServer import; discarding import request."
        else
          log "TileServer import failed; discarding import request to avoid retrying the same failing request in a loop."
        fi
        cleanup_temp_dir "${TEMP_DIR}/import"
        rm -f "${REQUEST_FILE}"
        clear_abort_flag
        clear_completed_steps
        continue
      fi
      mark_step_done "tileserver"
    fi
    local nrc=0
    if ! step_requested "nominatim"; then
      log "Nominatim step not requested for this import request; skipping."
    elif step_already_done "nominatim"; then
      log "Nominatim already promoted for this request (resumed after restart); skipping re-import."
    else
      run_step "nominatim" "nominatim-import" "nominatim-import-job.yaml" "${DATA_DIR}/nominatim/active" "${TEMP_DIR}/nominatim/staging" "nominatim" "${AUTO_PROMOTE}" || nrc=$?
      if [ "${nrc}" -ne 0 ]; then
        if [ "${nrc}" -eq 2 ]; then
          log "Import aborted by user during Nominatim import; discarding import request."
        else
          log "Nominatim import failed; discarding import request to avoid retrying the same failing request in a loop."
        fi
        cleanup_temp_dir "${TEMP_DIR}/import"
        rm -f "${REQUEST_FILE}"
        clear_abort_flag
        clear_completed_steps
        continue
      fi
      mark_step_done "nominatim"
    fi
    local vrc=0
    if ! step_requested "valhalla"; then
      log "Valhalla step not requested for this import request; skipping."
    elif step_already_done "valhalla"; then
      log "Valhalla already promoted for this request (resumed after restart); skipping re-import."
    else
      run_step "valhalla" "valhalla-import" "valhalla-import-job.yaml" "${DATA_DIR}/valhalla/active" "${TEMP_DIR}/valhalla/staging" "valhalla" "${AUTO_PROMOTE}" || vrc=$?
      if [ "${vrc}" -ne 0 ]; then
        if [ "${vrc}" -eq 2 ]; then
          log "Import aborted by user during Valhalla import; discarding import request."
        else
          log "Valhalla import failed; discarding import request to avoid retrying the same failing request in a loop."
        fi
        cleanup_temp_dir "${TEMP_DIR}/import"
        rm -f "${REQUEST_FILE}"
        clear_abort_flag
        clear_completed_steps
        continue
      fi
      mark_step_done "valhalla"
    fi

    # The shared merged-extract scratch data is only needed while the three
    # import steps run; once TileServer, Nominatim and Valhalla have all
    # consumed it, the temp dir must be cleared again.
    cleanup_temp_dir "${TEMP_DIR}/import"
    rm -f "${REQUEST_FILE}"
    clear_abort_flag
    clear_completed_steps
    write_state false "done" 100 "Import erfolgreich abgeschlossen." "Alle Daten wurden sequentiell verarbeitet und aktiviert."
    log "Import request completed successfully."
  done
}

main "$@"
