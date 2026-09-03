#!/usr/bin/env bash
# storage-perf-tester.sh - configurable Kubernetes StorageClass performance
# tester (see ../README.md for full documentation).
set -Eeuo pipefail

SPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../lib/common.sh
source "${SPT_DIR}/lib/common.sh"
# shellcheck source=../lib/k8s.sh
source "${SPT_DIR}/lib/k8s.sh"
# shellcheck source=../lib/collect.sh
source "${SPT_DIR}/lib/collect.sh"
# shellcheck source=../lib/rank.sh
source "${SPT_DIR}/lib/rank.sh"

ALL_WORKLOADS="postgres-write-durable postgres-write-fast postgres-read small-files large-files mixed jenkins-checkout"
POSTGRES_WORKLOADS="postgres-write-durable postgres-write-fast postgres-read"

usage() {
  cat <<'EOF'
Usage: storage-perf-tester.sh <command> [options]

Commands:
  discover   Detect and print the StorageClasses that would be tested.
  plan       Render every Kubernetes manifest that "run" would apply,
             without contacting the cluster (dry-run).
  run        Discover StorageClasses, create test resources, execute all
             workloads (bounded by --max-parallel), collect results, write
             JSON/CSV and a ranking report. Cleans up afterwards unless
             --no-cleanup is given.
  rank       Recompute results.json + ranking.json for an existing
             --output-dir without touching the cluster.
  cleanup    Delete storage-perf-tester resources from the cluster.
             With --run-id, only that run's resources are removed;
             otherwise every storage-perf-tester resource in --namespace
             is removed.

Options (all have defaults in config/default.conf, see README.md):
  --config FILE                 Additional config file sourced after the
                                 built-in defaults (same KEY=VALUE syntax).
  --namespace NAME
  --storageclasses a,b,c        Explicit StorageClass allow-list.
  --exclude-storageclasses a,b  StorageClasses to always skip.
  --pvc-size SIZE               e.g. 5Gi
  --duration SECONDS            fio/pgbench runtime per phase
  --file-size SIZE              e.g. 1Gi (large-files/mixed workloads)
  --queue-depths 1,4,8          Threads/clients tested per workload
  --repeats N
  --max-parallel N              Concurrent StorageClass pipelines
  --workloads a,b,c             Subset of workloads to run (see below)
  --cleanup / --no-cleanup
  --output-dir PATH
  --output-format json|csv|both
  --weights-file FILE           Ranking weights (see config/weights.default.conf)
  --cpu-request / --cpu-limit
  --memory-request / --memory-limit
  --pvc-timeout SECONDS
  --job-timeout SECONDS
  --fio-image IMAGE
  --postgres-image IMAGE
  --run-id ID                   Reuse/target a specific run id (auto-generated otherwise)
  --results-file FILE           Input for the "rank" command (default: <output-dir>/results.json)
  -h, --help

Available workload ids: postgres-write-durable postgres-write-fast
  postgres-read small-files large-files mixed jenkins-checkout

Examples:
  storage-perf-tester.sh discover
  storage-perf-tester.sh plan --namespace storage-perf-test
  storage-perf-tester.sh run --queue-depths 1,4 --repeats 1 --no-cleanup
  storage-perf-tester.sh cleanup --run-id 20240101-120000
EOF
}

# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------
CONFIG_FILE=""
RUN_ID=""
RESULTS_FILE_OVERRIDE=""

load_config() {
  set -a
  # shellcheck source=config/default.conf
  source "${SPT_DIR}/config/default.conf"
  set +a
  if [ -n "$CONFIG_FILE" ]; then
    [ -f "$CONFIG_FILE" ] || die "Config file not found: $CONFIG_FILE"
    set -a
    # shellcheck source=/dev/null
    source "$CONFIG_FILE"
    set +a
  fi
}

parse_common_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --config) CONFIG_FILE="$2"; shift 2 ;;
      --namespace) NAMESPACE="$2"; shift 2 ;;
      --storageclasses) STORAGECLASSES="$2"; shift 2 ;;
      --exclude-storageclasses) EXCLUDE_STORAGECLASSES="$2"; shift 2 ;;
      --pvc-size) PVC_SIZE="$2"; shift 2 ;;
      --duration) DURATION="$2"; shift 2 ;;
      --file-size) FILE_SIZE="$2"; shift 2 ;;
      --queue-depths) QUEUE_DEPTHS="$2"; shift 2 ;;
      --repeats) REPEATS="$2"; shift 2 ;;
      --max-parallel) MAX_PARALLEL="$2"; shift 2 ;;
      --workloads) WORKLOADS="$2"; shift 2 ;;
      --cleanup) CLEANUP="true"; shift ;;
      --no-cleanup) CLEANUP="false"; shift ;;
      --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
      --output-format) OUTPUT_FORMAT="$2"; shift 2 ;;
      --weights-file) WEIGHTS_FILE="$2"; shift 2 ;;
      --cpu-request) CPU_REQUEST="$2"; shift 2 ;;
      --cpu-limit) CPU_LIMIT="$2"; shift 2 ;;
      --memory-request) MEMORY_REQUEST="$2"; shift 2 ;;
      --memory-limit) MEMORY_LIMIT="$2"; shift 2 ;;
      --pvc-timeout) PVC_TIMEOUT="$2"; shift 2 ;;
      --job-timeout) JOB_TIMEOUT="$2"; shift 2 ;;
      --fio-image) FIO_IMAGE="$2"; shift 2 ;;
      --postgres-image) POSTGRES_IMAGE="$2"; shift 2 ;;
      --run-id) RUN_ID="$2"; shift 2 ;;
      --results-file) RESULTS_FILE_OVERRIDE="$2"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die "Unknown option: $1 (see --help)" ;;
    esac
  done
  [ -n "${WEIGHTS_FILE}" ] || WEIGHTS_FILE="${SPT_DIR}/config/weights.default.conf"
}

resolved_storageclasses() {
  discovered="$(discover_storageclasses)"
  if [ -z "$discovered" ] && [ -z "$STORAGECLASSES" ]; then
    die "No StorageClasses found in the cluster and none given via --storageclasses."
  fi
  filter_storageclasses "$discovered" "$STORAGECLASSES" "$EXCLUDE_STORAGECLASSES"
}

resolved_workloads() {
  if [ -n "$WORKLOADS" ]; then
    split_csv "$WORKLOADS"
  else
    printf '%s\n' "$ALL_WORKLOADS" | tr ' ' '\n'
  fi
}

is_postgres_workload() {
  case " ${POSTGRES_WORKLOADS} " in *" $1 "*) return 0 ;; *) return 1 ;; esac
}

# ---------------------------------------------------------------------------
# Manifest generation (shared by "plan" and "run")
# ---------------------------------------------------------------------------

# Prints the PVC/Job name for a given (run_id, storageclass, workload,
# queue_depth, repeat) combination. Names are deterministic and unique: a
# short checksum suffix guards against collisions introduced by truncating
# long storageclass/workload names down to the 63-character Kubernetes
# object name limit.
name_hash() { printf '%s' "$1" | cksum | awk '{print $1}'; }

pvc_name_for() {
  run_id="$1"; sc="$2"; workload="$3"; qd="$4"; repeat="$5"
  h="$(name_hash "${run_id}-${sc}-${workload}-${qd}-${repeat}")"
  trimmed="$(printf 'spt-pvc-%s-%s-qd%s-r%s' "$(k8s_safe_name "$sc")" "$(k8s_safe_name "$workload")" "$qd" "$repeat" | cut -c1-50 | sed 's/-*$//')"
  printf '%s-h%s\n' "$trimmed" "$h"
}
job_name_for() {
  run_id="$1"; sc="$2"; workload="$3"; qd="$4"; repeat="$5"
  h="$(name_hash "${run_id}-${sc}-${workload}-${qd}-${repeat}")"
  trimmed="$(printf 'spt-job-%s-%s-qd%s-r%s' "$(k8s_safe_name "$sc")" "$(k8s_safe_name "$workload")" "$qd" "$repeat" | cut -c1-50 | sed 's/-*$//')"
  printf '%s-h%s\n' "$trimmed" "$h"
}

# Renders the full manifest set (namespace + configmap + pvc + job) for one
# (storageclass, workload, queue_depth, repeat) combination.
render_case() {
  sc="$1"; workload="$2"; qd="$3"; repeat="$4"; run_id="$5"
  sc_label="$(k8s_safe_name "$sc")"
  pvc="$(pvc_name_for "$run_id" "$sc" "$workload" "$qd" "$repeat")"
  job="$(job_name_for "$run_id" "$sc" "$workload" "$qd" "$repeat")"
  cm="spt-scripts-${run_id}"

  echo "---"
  render_template pvc.yaml.tpl \
    "PVC_NAME=${pvc}" "NAMESPACE=${NAMESPACE}" "STORAGECLASS=${sc}" \
    "PVC_SIZE=${PVC_SIZE}" "RUN_ID=${run_id}" "STORAGECLASS_LABEL=${sc_label}"
  echo "---"
  if is_postgres_workload "$workload"; then
    render_template postgres-job.yaml.tpl \
      "JOB_NAME=${job}" "NAMESPACE=${NAMESPACE}" "STORAGECLASS_LABEL=${sc_label}" \
      "RUN_ID=${run_id}" "WORKLOAD=${workload}" "QUEUE_DEPTH=${qd}" "REPEAT=${repeat}" \
      "JOB_TIMEOUT=${JOB_TIMEOUT}" "TTL_SECONDS=3600" "POSTGRES_IMAGE=${POSTGRES_IMAGE}" \
      "DURATION=${DURATION}" "MOUNT_PATH=/data" "PGBENCH_SCALE=25" \
      "CPU_REQUEST=${CPU_REQUEST}" "CPU_LIMIT=${CPU_LIMIT}" \
      "MEMORY_REQUEST=${MEMORY_REQUEST}" "MEMORY_LIMIT=${MEMORY_LIMIT}" \
      "CONFIGMAP_NAME=${cm}" "PVC_NAME=${pvc}"
  else
    render_template fio-job.yaml.tpl \
      "JOB_NAME=${job}" "NAMESPACE=${NAMESPACE}" "STORAGECLASS_LABEL=${sc_label}" \
      "RUN_ID=${run_id}" "WORKLOAD=${workload}" "QUEUE_DEPTH=${qd}" "REPEAT=${repeat}" \
      "JOB_TIMEOUT=${JOB_TIMEOUT}" "TTL_SECONDS=3600" "FIO_IMAGE=${FIO_IMAGE}" \
      "DURATION=${DURATION}" "FILE_SIZE=${FILE_SIZE}" "MOUNT_PATH=/data" \
      "SMALL_FILES_COUNT=2000" \
      "CPU_REQUEST=${CPU_REQUEST}" "CPU_LIMIT=${CPU_LIMIT}" \
      "MEMORY_REQUEST=${MEMORY_REQUEST}" "MEMORY_LIMIT=${MEMORY_LIMIT}" \
      "CONFIGMAP_NAME=${cm}" "PVC_NAME=${pvc}"
  fi
}

iterate_cases() {
  # Calls "$1" (a function name) once per (storageclass, workload,
  # queue_depth, repeat) combination with those four values as arguments.
  callback="$1"; run_id="$2"
  scs="$(resolved_storageclasses)"
  workloads="$(resolved_workloads)"
  qds="$(split_csv "$QUEUE_DEPTHS")"
  [ -n "$scs" ] || die "No StorageClasses selected."
  [ -n "$workloads" ] || die "No workloads selected."
  [ -n "$qds" ] || die "No queue depths configured."
  while IFS= read -r sc; do
    [ -n "$sc" ] || continue
    while IFS= read -r workload; do
      [ -n "$workload" ] || continue
      while IFS= read -r qd; do
        [ -n "$qd" ] || continue
        r=1
        while [ "$r" -le "$REPEATS" ]; do
          "$callback" "$sc" "$workload" "$qd" "$r" "$run_id"
          r=$((r + 1))
        done
      done <<<"$qds"
    done <<<"$workloads"
  done <<<"$scs"
}

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

cmd_discover() {
  require_cmd kubectl
  scs="$(resolved_storageclasses)"
  [ -n "$scs" ] || die "No StorageClasses matched the current selection."
  echo "StorageClasses to be tested (namespace: ${NAMESPACE}):"
  printf '%s\n' "$scs" | sed 's/^/  - /'
}

cmd_plan() {
  require_cmd kubectl envsubst
  run_id="${RUN_ID:-$(date -u +%Y%m%d-%H%M%S)}"
  log_info "Plan for run-id=${run_id} (dry-run, no cluster changes made)"
  render_template namespace.yaml.tpl "NAMESPACE=${NAMESPACE}"
  iterate_cases render_case "$run_id"
}

cmd_run() {
  require_cmd kubectl envsubst jq
  run_id="${RUN_ID:-$(date -u +%Y%m%d-%H%M%S)}"
  out_dir="${OUTPUT_DIR%/}/${run_id}"
  mkdir -p "$out_dir"
  results_file="${out_dir}/results.json"
  echo "[]" >"$results_file"

  log_info "Starting storage-perf-tester run ${run_id} in namespace ${NAMESPACE}"
  ensure_namespace "$NAMESPACE" >/dev/null
  cm="spt-scripts-${run_id}"
  apply_scripts_configmap "$cm" "$NAMESPACE" >/dev/null

  # Build the flat list of cases to execute so we can bound parallelism.
  cases_file="$(mktemp)"
  collect_case() { printf '%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "$5" >>"$cases_file"; }
  iterate_cases collect_case "$run_id"
  total_cases="$(wc -l <"$cases_file" | tr -d ' ')"
  log_info "Executing ${total_cases} test case(s) with max-parallel=${MAX_PARALLEL}"

  run_one_case() {
    sc="$1"; workload="$2"; qd="$3"; repeat="$4"
    pvc="$(pvc_name_for "$run_id" "$sc" "$workload" "$qd" "$repeat")"
    job="$(job_name_for "$run_id" "$sc" "$workload" "$qd" "$repeat")"
    log_info "[${sc}/${workload} qd=${qd} repeat=${repeat}] applying PVC+Job (${job})"
    render_case "$sc" "$workload" "$qd" "$repeat" "$run_id" | kubectl apply -f - >/dev/null

    pvc_status="$(wait_for_pvc_ready "$pvc" "$NAMESPACE" "$sc" "$PVC_TIMEOUT")"
    log_info "[${sc}/${workload} qd=${qd} repeat=${repeat}] PVC status: ${pvc_status}"

    if [ "$pvc_status" = "Timeout" ]; then
      status="PVCTimeout"
      log_info "[${sc}/${workload} qd=${qd} repeat=${repeat}] PVC never bound within ${PVC_TIMEOUT}s, skipping job wait"
    else
      status="$(wait_for_job "$job" "$NAMESPACE" "$JOB_TIMEOUT")"
      log_info "[${sc}/${workload} qd=${qd} repeat=${repeat}] job status: ${status}"
    fi

    logfile="$(mktemp)"
    job_logs "$job" "$NAMESPACE" >"$logfile" || true
    with_lock "$results_file" collect_job_result "$logfile" "$results_file" "$sc" "$workload" "$qd" "$repeat" "$status"
    rm -f "$logfile"

    if [ "${CLEANUP}" = "true" ]; then
      kubectl delete job "$job" -n "$NAMESPACE" --ignore-not-found=true --wait=false >/dev/null 2>&1 || true
      kubectl delete pvc "$pvc" -n "$NAMESPACE" --ignore-not-found=true --wait=false >/dev/null 2>&1 || true
    fi
  }

  # Bound parallelism natively with bash job control (no GNU parallel/xargs
  # dependency): keep at most MAX_PARALLEL run_one_case backgrounded at a
  # time, since real parallel PVC/Job creation consumes cluster CPU/RAM/IO.
  running=0
  while IFS=$'\t' read -r sc workload qd repeat _run_id; do
    [ -n "$sc" ] || continue
    run_one_case "$sc" "$workload" "$qd" "$repeat" &
    running=$((running + 1))
    if [ "$running" -ge "$MAX_PARALLEL" ]; then
      wait -n || true
      running=$((running - 1))
    fi
  done <"$cases_file"
  wait
  rm -f "$cases_file" "${results_file}.lock"

  if [ "${CLEANUP}" = "true" ]; then
    kubectl delete configmap "$cm" -n "$NAMESPACE" --ignore-not-found=true --wait=false >/dev/null 2>&1 || true
  fi

  write_reports "$results_file" "$out_dir"
  log_info "Run ${run_id} complete. Results in ${out_dir}"
}

write_reports() {
  results_file="$1"; out_dir="$2"
  ranking_file="${out_dir}/ranking.json"
  compute_ranking "$results_file" "$WEIGHTS_FILE" "$ranking_file"

  case "$OUTPUT_FORMAT" in
    csv|both)
      results_to_csv "$results_file" >"${out_dir}/results.csv"
      ;;
  esac
  case "$OUTPUT_FORMAT" in
    json|both) : ;; # results.json / ranking.json already written
  esac

  echo
  echo "=== Overall ranking (${out_dir}/ranking.json) ==="
  jq -r '.overall[] | "\(.rank)) \(.storageclass)  score=\(.score | (.*1000|round)/1000)"' "$ranking_file" 2>/dev/null || true
  echo
  echo "Per-workload rankings and raw metrics: ${out_dir}/ranking.json / results.json"
  [ -f "${out_dir}/results.csv" ] && echo "CSV export: ${out_dir}/results.csv"
}

cmd_rank() {
  require_cmd jq
  results_file="${RESULTS_FILE_OVERRIDE:-${OUTPUT_DIR%/}/results.json}"
  [ -f "$results_file" ] || die "Results file not found: $results_file"
  out_dir="$(dirname "$results_file")"
  write_reports "$results_file" "$out_dir"
}

cmd_cleanup() {
  require_cmd kubectl
  if [ -n "$RUN_ID" ]; then
    log_info "Deleting storage-perf-tester resources for run-id=${RUN_ID} in namespace ${NAMESPACE}"
    delete_by_label "$NAMESPACE" "spt-run-id=${RUN_ID}"
  else
    log_info "Deleting ALL storage-perf-tester resources in namespace ${NAMESPACE}"
    delete_by_label "$NAMESPACE" "app.kubernetes.io/managed-by=storage-perf-tester"
  fi
  log_info "Cleanup requested (asynchronous; resources terminate in the background)."
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

main() {
  [ $# -ge 1 ] || { usage; exit 1; }
  command="$1"; shift
  load_config
  parse_common_args "$@"

  case "$command" in
    discover) cmd_discover ;;
    plan) cmd_plan ;;
    run) cmd_run ;;
    rank) cmd_rank ;;
    cleanup) cmd_cleanup ;;
    -h|--help) usage ;;
    *) usage; die "Unknown command: $command" ;;
  esac
}

main "$@"
