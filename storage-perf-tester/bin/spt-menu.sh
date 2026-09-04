#!/usr/bin/env bash
# spt-menu.sh - dialog/whiptail control menu for storage-perf-tester.
#
# A single, menu-driven entry point for storage-perf-tester so you don't
# have to remember individual bin/storage-perf-tester.sh flags. It never
# re-implements any discovery/testing/ranking logic itself: every action
# just invokes bin/storage-perf-tester.sh (the same script documented in
# ../README.md), either synchronously (discover/plan/cleanup) or in the
# background (run), so there is exactly one place that owns the actual
# Kubernetes/testing logic.
#
# From the menu you can:
#   - configure and start a performance run in the background
#   - watch a "status dashboard": live progress of the running run (cases
#     completed vs total, per-case log tail, Kubernetes Jobs/Pods overview)
#   - browse a "performance dashboard": the ranking/results of the most
#     recent (or any previous) run
#   - discover StorageClasses, render a dry-run plan, and clean up
#
# Usage: bash storage-perf-tester/bin/spt-menu.sh [--namespace <ns>] [--output-dir <path>]
#
# Requires: dialog or whiptail, bash, kubectl, jq (same prerequisites as
# bin/storage-perf-tester.sh; see ../README.md).

set -Eeuo pipefail

SPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SPT_BIN="${SPT_DIR}/bin/storage-perf-tester.sh"

DIALOG_BIN=""
DIALOG_HEIGHT=23
DIALOG_WIDTH=86
MENU_HEIGHT=15

# Menu-level defaults; every one of these maps 1:1 to a
# bin/storage-perf-tester.sh flag and can be overridden from the "Configure
# run" screen. Pre-filled from config/default.conf so the menu shows the
# same conservative defaults as the CLI.
NAMESPACE="storage-perf-test"
STORAGECLASSES=""
EXCLUDE_STORAGECLASSES=""
PVC_SIZE="5Gi"
DURATION="30"
FILE_SIZE="1Gi"
QUEUE_DEPTHS="1,4,8"
REPEATS="1"
MAX_PARALLEL="1"
WORKLOADS=""
CLEANUP_AFTER_RUN="true"
OUTPUT_DIR="${SPT_DIR}/results"
OUTPUT_FORMAT="both"
WEIGHTS_FILE=""

# State of the run launched from this menu (one at a time, tracked via a
# small state file so "status dashboard" keeps working even if the menu is
# closed and reopened while a run is still in progress).
STATE_DIR="${SPT_DIR}/results/.menu-state"
ACTIVE_RUN_FILE="${STATE_DIR}/active-run"

TMP_FILES=()
BG_PIDS=()

cleanup_tmp() {
  local pid f
  for pid in "${BG_PIDS[@]:-}"; do
    if [ -n "$pid" ]; then kill "$pid" >/dev/null 2>&1 || true; fi
  done
  for f in "${TMP_FILES[@]:-}"; do
    if [ -n "$f" ]; then rm -f "$f" 2>/dev/null || true; fi
  done
}
trap cleanup_tmp EXIT INT TERM

mktmp() {
  local f
  f="$(mktemp "${TMPDIR:-/tmp}/spt-menu.XXXXXX")"
  TMP_FILES+=("$f")
  printf '%s\n' "$f"
}

require_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: required command '${cmd}' not found in PATH." >&2
    exit 1
  fi
}

detect_dialog() {
  if command -v dialog >/dev/null 2>&1; then
    DIALOG_BIN="dialog"
  elif command -v whiptail >/dev/null 2>&1; then
    DIALOG_BIN="whiptail"
  else
    echo "ERROR: neither 'dialog' nor 'whiptail' is installed." >&2
    echo "Install one of them, e.g.: sudo apt-get install -y dialog" >&2
    exit 1
  fi
}

# Loads config/default.conf into plain (non ": \${VAR:=}" -mangled)
# variables purely to pre-fill the "Configure run" dialog fields; the
# actual run always re-reads the same file via bin/storage-perf-tester.sh
# itself, so this is display-only and never a second source of truth.
load_defaults() {
  local key val
  while IFS='=' read -r key val; do
    case "$key" in
      ''|'#'*) continue ;;
    esac
    val="${val%\"}"; val="${val#\"}"
    case "$key" in
      NAMESPACE) NAMESPACE="$val" ;;
      PVC_SIZE) PVC_SIZE="$val" ;;
      DURATION) DURATION="$val" ;;
      FILE_SIZE) FILE_SIZE="$val" ;;
      QUEUE_DEPTHS) QUEUE_DEPTHS="$val" ;;
      REPEATS) REPEATS="$val" ;;
      MAX_PARALLEL) MAX_PARALLEL="$val" ;;
      CLEANUP) CLEANUP_AFTER_RUN="$val" ;;
      OUTPUT_DIR) OUTPUT_DIR="${SPT_DIR}/${val#./}" ;;
      OUTPUT_FORMAT) OUTPUT_FORMAT="$val" ;;
    esac
  done < <(sed -n 's/^: "\${\([A-Za-z_]*\):=\(.*\)}"$/\1=\2/p' "${SPT_DIR}/config/default.conf")
}

d() {
  "${DIALOG_BIN}" --backtitle "storage-perf-tester control menu (namespace=${NAMESPACE})" "$@"
}

msgbox() { d --title "${1}" --msgbox "${2}" "${DIALOG_HEIGHT}" "${DIALOG_WIDTH}"; }
yesno()  { d --title "${1}" --yesno "${2}" "${DIALOG_HEIGHT}" "${DIALOG_WIDTH}"; }
infobox() { d --title "${1}" --infobox "${2}" 8 "${DIALOG_WIDTH}"; }
textbox_file() { d --title "${1}" --textbox "${2}" "${DIALOG_HEIGHT}" "${DIALOG_WIDTH}"; }
inputbox() {
  local title="$1" prompt="$2" default="${3:-}"
  d --title "${title}" --inputbox "${prompt}" "${DIALOG_HEIGHT}" "${DIALOG_WIDTH}" "${default}" 3>&1 1>&2 2>&3
}
menu() {
  local title="$1" prompt="$2"
  shift 2
  d --title "${title}" --menu "${prompt}" "${DIALOG_HEIGHT}" "${DIALOG_WIDTH}" "${MENU_HEIGHT}" "$@" 3>&1 1>&2 2>&3
}

# ---------------------------------------------------------------------------
# Run-state helpers (background run tracking)
# ---------------------------------------------------------------------------

# active-run state file format (KEY=VALUE, one per line): run_id, pid,
# namespace, out_dir, log_file, total_cases, workloads (space-joined),
# storageclass_count, queue_depth_count, repeats.
save_active_run() {
  mkdir -p "${STATE_DIR}"
  {
    echo "run_id=${1}"
    echo "pid=${2}"
    echo "namespace=${3}"
    echo "out_dir=${4}"
    echo "log_file=${5}"
    echo "total_cases=${6}"
  } >"${ACTIVE_RUN_FILE}"
}

read_active_run_field() {
  local field="$1"
  [ -f "${ACTIVE_RUN_FILE}" ] || return 1
  sed -n "s/^${field}=//p" "${ACTIVE_RUN_FILE}" | tail -n1
}

active_run_pid_alive() {
  local pid
  pid="$(read_active_run_field pid 2>/dev/null || true)"
  [ -n "$pid" ] && kill -0 "$pid" >/dev/null 2>&1
}

# ---------------------------------------------------------------------------
# Actions: discover / plan / cleanup (thin, synchronous CLI wrappers)
# ---------------------------------------------------------------------------

action_discover() {
  local tf
  tf="$(mktmp)"
  {
    echo "Running: storage-perf-tester.sh discover --namespace ${NAMESPACE}"
    echo "---"
    "${SPT_BIN}" discover --namespace "${NAMESPACE}" \
      ${STORAGECLASSES:+--storageclasses "${STORAGECLASSES}"} \
      ${EXCLUDE_STORAGECLASSES:+--exclude-storageclasses "${EXCLUDE_STORAGECLASSES}"}
  } >"${tf}" 2>&1 || true
  textbox_file "Discover StorageClasses" "${tf}"
}

action_plan() {
  local tf
  tf="$(mktmp)"
  {
    echo "Running: storage-perf-tester.sh plan (dry-run, no cluster changes)"
    echo "---"
    build_common_args
    "${SPT_BIN}" plan "${COMMON_ARGS[@]}"
  } >"${tf}" 2>&1 || true
  textbox_file "Plan (dry-run manifest render)" "${tf}"
}

action_cleanup() {
  local run_id scope
  scope="$(menu "Cleanup" "Choose cleanup scope:" \
    this_run "Only the run tracked by this menu (if any)" \
    specific "A specific --run-id" \
    all "ALL storage-perf-tester resources in the namespace")" || return 0
  [ -n "${scope}" ] || return 0
  case "${scope}" in
    this_run)
      run_id="$(read_active_run_field run_id 2>/dev/null || true)"
      if [ -z "${run_id}" ]; then
        msgbox "Cleanup" "No run is currently tracked by this menu."
        return 0
      fi
      ;;
    specific)
      run_id="$(inputbox "Cleanup" "run-id to delete (see status dashboard / results dir names):")" || return 0
      [ -n "${run_id}" ] || return 0
      ;;
    all)
      run_id=""
      ;;
  esac
  if [ -n "${run_id}" ]; then
    yesno "Confirm cleanup" "Delete all storage-perf-tester resources for run-id '${run_id}' in namespace '${NAMESPACE}'?" || return 0
  else
    yesno "Confirm cleanup" "Delete ALL storage-perf-tester resources in namespace '${NAMESPACE}'?\n\nThis affects every run, not just ones started from this menu." || return 0
  fi
  local tf
  tf="$(mktmp)"
  {
    echo "Running: storage-perf-tester.sh cleanup --namespace ${NAMESPACE} ${run_id:+--run-id ${run_id}}"
    echo "---"
    "${SPT_BIN}" cleanup --namespace "${NAMESPACE}" ${run_id:+--run-id "${run_id}"}
  } >"${tf}" 2>&1 || true
  textbox_file "Cleanup" "${tf}"
}

# ---------------------------------------------------------------------------
# Action: configure & start a run (background)
# ---------------------------------------------------------------------------

COMMON_ARGS=()
build_common_args() {
  COMMON_ARGS=(
    --namespace "${NAMESPACE}"
    --pvc-size "${PVC_SIZE}"
    --duration "${DURATION}"
    --file-size "${FILE_SIZE}"
    --queue-depths "${QUEUE_DEPTHS}"
    --repeats "${REPEATS}"
    --max-parallel "${MAX_PARALLEL}"
    --output-dir "${OUTPUT_DIR}"
    --output-format "${OUTPUT_FORMAT}"
  )
  [ -n "${STORAGECLASSES}" ] && COMMON_ARGS+=(--storageclasses "${STORAGECLASSES}")
  [ -n "${EXCLUDE_STORAGECLASSES}" ] && COMMON_ARGS+=(--exclude-storageclasses "${EXCLUDE_STORAGECLASSES}")
  [ -n "${WORKLOADS}" ] && COMMON_ARGS+=(--workloads "${WORKLOADS}")
  [ -n "${WEIGHTS_FILE}" ] && COMMON_ARGS+=(--weights-file "${WEIGHTS_FILE}")
  if [ "${CLEANUP_AFTER_RUN}" = "true" ]; then
    COMMON_ARGS+=(--cleanup)
  else
    COMMON_ARGS+=(--no-cleanup)
  fi
}

action_configure_run() {
  local val
  val="$(inputbox "Configure run" "Namespace:" "${NAMESPACE}")" || return 0
  [ -n "$val" ] && NAMESPACE="$val"
  val="$(inputbox "Configure run" "StorageClasses (comma separated, empty = auto-discover all):" "${STORAGECLASSES}")" || return 0
  STORAGECLASSES="$val"
  val="$(inputbox "Configure run" "Exclude StorageClasses (comma separated):" "${EXCLUDE_STORAGECLASSES}")" || return 0
  EXCLUDE_STORAGECLASSES="$val"
  val="$(inputbox "Configure run" "Workloads (comma separated, empty = all 7):\npostgres-write-durable,postgres-write-fast,postgres-read,\nsmall-files,large-files,mixed,jenkins-checkout" "${WORKLOADS}")" || return 0
  WORKLOADS="$val"
  val="$(inputbox "Configure run" "PVC size:" "${PVC_SIZE}")" || return 0
  [ -n "$val" ] && PVC_SIZE="$val"
  val="$(inputbox "Configure run" "Duration per phase (seconds):" "${DURATION}")" || return 0
  [ -n "$val" ] && DURATION="$val"
  val="$(inputbox "Configure run" "File size (large-files/mixed workloads):" "${FILE_SIZE}")" || return 0
  [ -n "$val" ] && FILE_SIZE="$val"
  val="$(inputbox "Configure run" "Queue depths / threads (comma separated):" "${QUEUE_DEPTHS}")" || return 0
  [ -n "$val" ] && QUEUE_DEPTHS="$val"
  val="$(inputbox "Configure run" "Repeats per (workload, queue-depth):" "${REPEATS}")" || return 0
  [ -n "$val" ] && REPEATS="$val"
  val="$(inputbox "Configure run" "Max parallel StorageClass pipelines (keep low on shared clusters):" "${MAX_PARALLEL}")" || return 0
  [ -n "$val" ] && MAX_PARALLEL="$val"
  if yesno "Configure run" "Clean up PVCs/Jobs automatically after the run?\n\nChoose 'No' to inspect resources manually afterwards (remember to run Cleanup later)."; then
    CLEANUP_AFTER_RUN="true"
  else
    CLEANUP_AFTER_RUN="false"
  fi
  val="$(inputbox "Configure run" "Output directory:" "${OUTPUT_DIR}")" || return 0
  [ -n "$val" ] && OUTPUT_DIR="$val"
}

# Rough total-case estimate shown before starting a run: storageclass count
# (from discover, or the explicit --storageclasses list) * workload count *
# queue-depth count * repeats. This mirrors iterate_cases() in
# bin/storage-perf-tester.sh without duplicating its logic (it is only used
# for the confirmation prompt and the progress bar denominator).
estimate_total_cases() {
  local sc_count wl_count qd_count
  if [ -n "${STORAGECLASSES}" ]; then
    sc_count="$(printf '%s' "${STORAGECLASSES}" | tr ',' '\n' | grep -vc '^$' || true)"
  else
    sc_count="$("${SPT_BIN}" discover --namespace "${NAMESPACE}" \
      ${EXCLUDE_STORAGECLASSES:+--exclude-storageclasses "${EXCLUDE_STORAGECLASSES}"} 2>/dev/null \
      | grep -c '^  - ' || true)"
  fi
  if [ -n "${WORKLOADS}" ]; then
    wl_count="$(printf '%s' "${WORKLOADS}" | tr ',' '\n' | grep -vc '^$' || true)"
  else
    wl_count=7
  fi
  qd_count="$(printf '%s' "${QUEUE_DEPTHS}" | tr ',' '\n' | grep -vc '^$' || true)"
  echo $(( sc_count * wl_count * qd_count * REPEATS ))
}

action_start_run() {
  if active_run_pid_alive; then
    msgbox "Run already active" "A run (run-id=$(read_active_run_field run_id)) is already tracked as running.\n\nOpen the status dashboard, or stop it first."
    return 0
  fi
  build_common_args
  local total
  total="$(estimate_total_cases 2>/dev/null || echo '?')"
  yesno "Start run" "Start a performance run now?\n\nNamespace: ${NAMESPACE}\nEstimated test cases: ${total} (storageclasses x workloads x queue-depths x repeats)\nOutput dir: ${OUTPUT_DIR}\n\nThis creates PVCs/Jobs in the cluster and can take a long time depending on duration/repeats/max-parallel." || return 0

  mkdir -p "${STATE_DIR}"
  local run_id log_file
  run_id="$(date -u +%Y%m%d-%H%M%S)"
  log_file="${STATE_DIR}/run-${run_id}.log"

  ( "${SPT_BIN}" run --run-id "${run_id}" "${COMMON_ARGS[@]}" >"${log_file}" 2>&1 ) &
  local pid=$!
  disown "${pid}" 2>/dev/null || true
  save_active_run "${run_id}" "${pid}" "${NAMESPACE}" "${OUTPUT_DIR%/}/${run_id}" "${log_file}" "${total}"
  msgbox "Run started" "Started run-id=${run_id} in the background (pid ${pid}).\n\nLog file: ${log_file}\nOpen 'Status dashboard' to watch live progress."
}

action_stop_run() {
  if ! active_run_pid_alive; then
    msgbox "Stop run" "No run is currently tracked as running by this menu."
    return 0
  fi
  local run_id pid
  run_id="$(read_active_run_field run_id)"
  pid="$(read_active_run_field pid)"
  yesno "Stop run" "Stop the background process for run-id=${run_id} (pid ${pid})?\n\nThis does NOT delete already-created cluster resources; use Cleanup afterwards." || return 0
  kill "${pid}" >/dev/null 2>&1 || true
  msgbox "Stop run" "Sent stop signal to pid ${pid}.\n\nRemember to run Cleanup for run-id=${run_id} if resources should be removed."
}

# ---------------------------------------------------------------------------
# Action: status dashboard (live progress of the tracked run)
# ---------------------------------------------------------------------------

action_status_dashboard() {
  if [ ! -f "${ACTIVE_RUN_FILE}" ]; then
    msgbox "Status dashboard" "No run has been started from this menu yet."
    return 0
  fi
  local run_id namespace out_dir log_file total_cases
  run_id="$(read_active_run_field run_id)"
  namespace="$(read_active_run_field namespace)"
  out_dir="$(read_active_run_field out_dir)"
  log_file="$(read_active_run_field log_file)"
  total_cases="$(read_active_run_field total_cases)"
  case "${total_cases}" in ''|*[!0-9]*) total_cases=0 ;; esac

  local max_iterations=2000 i=0
  {
    while [ "${i}" -lt "${max_iterations}" ]; do
      local done_cases pct jobs_line last_lines running
      done_cases=0
      if [ -f "${out_dir}/results.json" ]; then
        done_cases="$(jq 'length' "${out_dir}/results.json" 2>/dev/null || echo 0)"
      fi
      case "${done_cases}" in ''|*[!0-9]*) done_cases=0 ;; esac
      if [ "${total_cases}" -gt 0 ]; then
        pct=$(( done_cases * 100 / total_cases ))
      else
        pct=0
      fi
      [ "${pct}" -gt 100 ] && pct=100
      jobs_line=""
      if command -v kubectl >/dev/null 2>&1; then
        jobs_line="$(kubectl -n "${namespace}" get jobs -l "spt-run-id=${run_id}" --no-headers 2>/dev/null | awk '{printf "%s: %s  ", $1, $2}')"
      fi
      last_lines="$(tail -n 6 "${log_file}" 2>/dev/null | tr '\n' ' | ')"
      running="alive"
      active_run_pid_alive || running="finished/stopped"
      printf 'XXX\n%s\nRun: %s (%s)\nCompleted cases: %s / %s\nJobs: %s\nLast log lines: %s\nXXX\n' \
        "${pct}" "${run_id}" "${running}" "${done_cases}" "${total_cases}" "${jobs_line:-(none)}" "${last_lines:-(no output yet)}"
      if ! active_run_pid_alive; then
        printf 'XXX\n100\nRun finished (or was stopped). Closing in 2s ...\nXXX\n'
        sleep 2
        break
      fi
      sleep 3
      i=$((i + 1))
    done
  } | d --title "Status dashboard: run ${run_id} (auto-closes when finished)" --gauge "Watching ${out_dir}/results.json ..." "${DIALOG_HEIGHT}" "${DIALOG_WIDTH}" 0
}

action_tail_log() {
  if [ ! -f "${ACTIVE_RUN_FILE}" ]; then
    msgbox "Tail run log" "No run has been started from this menu yet."
    return 0
  fi
  local log_file
  log_file="$(read_active_run_field log_file)"
  [ -f "${log_file}" ] || { msgbox "Tail run log" "Log file not found: ${log_file}"; return 0; }
  d --title "Run log: ${log_file} (Esc/Cancel to stop)" --tailbox "${log_file}" "${DIALOG_HEIGHT}" 100 || true
}

action_k8s_overview() {
  require_cmd kubectl
  local namespace tf
  namespace="${NAMESPACE}"
  if [ -f "${ACTIVE_RUN_FILE}" ]; then
    namespace="$(read_active_run_field namespace)"
  fi
  tf="$(mktmp)"
  {
    echo "Namespace: ${namespace} (refreshed: $(date '+%Y-%m-%d %H:%M:%S'))"
    echo "=== Jobs ==="
    kubectl -n "${namespace}" get jobs -o wide 2>&1
    echo
    echo "=== Pods ==="
    kubectl -n "${namespace}" get pods -o wide 2>&1
    echo
    echo "=== PVCs ==="
    kubectl -n "${namespace}" get pvc 2>&1
  } >"${tf}"
  textbox_file "Kubernetes overview (${namespace})" "${tf}"
}

# ---------------------------------------------------------------------------
# Action: performance dashboard (browse ranking.json / results.json)
# ---------------------------------------------------------------------------

list_run_dirs() {
  # Prints "<run_id>\t<path>" for every completed/in-progress run under
  # OUTPUT_DIR, newest first.
  [ -d "${OUTPUT_DIR}" ] || return 0
  find "${OUTPUT_DIR}" -mindepth 1 -maxdepth 1 -type d -name '[0-9]*-[0-9]*' 2>/dev/null \
    | sort -r \
    | while IFS= read -r d; do printf '%s\t%s\n' "$(basename "$d")" "$d"; done
}

action_performance_dashboard() {
  local choices=() run_dirs run_id run_dir
  run_dirs="$(list_run_dirs)"
  if [ -z "${run_dirs}" ]; then
    msgbox "Performance dashboard" "No runs found under ${OUTPUT_DIR}.\n\nStart a run first."
    return 0
  fi
  while IFS=$'\t' read -r rid rdir; do
    [ -n "$rid" ] || continue
    local label="run"
    [ -f "${rdir}/ranking.json" ] && label="run (ranking available)"
    choices+=("$rid" "$label")
  done <<<"${run_dirs}"
  run_id="$(menu "Performance dashboard" "Choose a run to inspect:" "${choices[@]}")" || return 0
  [ -n "${run_id}" ] || return 0
  run_dir="${OUTPUT_DIR%/}/${run_id}"

  local section
  section="$(menu "Performance dashboard: ${run_id}" "Choose a view:" \
    overall "Overall ranking" \
    per_workload "Per-workload ranking" \
    raw_results "Raw results (IOPS/throughput/latency/errors)" \
    csv "Path to CSV export (if generated)")" || return 0
  [ -n "${section}" ] || return 0

  local tf
  tf="$(mktmp)"
  case "${section}" in
    overall)
      if [ ! -f "${run_dir}/ranking.json" ]; then
        msgbox "Performance dashboard" "No ranking.json found for ${run_id} yet (run still in progress?)."
        return 0
      fi
      jq -r '
        "Overall ranking (higher score = better, normalized 0-1 per workload/queue-depth):\n",
        (.overall[] | "\(.rank)) \(.storageclass)  score=\(.score | (.*1000|round)/1000)  (workloads included: \(.workloads_included | join(", ")))")
      ' "${run_dir}/ranking.json" >"${tf}" 2>&1 || cat "${run_dir}/ranking.json" >"${tf}"
      ;;
    per_workload)
      if [ ! -f "${run_dir}/ranking.json" ]; then
        msgbox "Performance dashboard" "No ranking.json found for ${run_id} yet (run still in progress?)."
        return 0
      fi
      jq -r '
        .workloads | to_entries[] |
        "=== \(.key) ===",
        (.value[] | "  \(.rank)) \(.storageclass)  score=\(.score | (.*1000|round)/1000)  status=\(.status)"),
        ""
      ' "${run_dir}/ranking.json" >"${tf}" 2>&1 || cat "${run_dir}/ranking.json" >"${tf}"
      ;;
    raw_results)
      if [ ! -f "${run_dir}/results.json" ]; then
        msgbox "Performance dashboard" "No results.json found for ${run_id} yet."
        return 0
      fi
      jq -r '
        .[] | "\(.storageclass // "?")  \(.workload // "?")  qd=\(.queue_depth // "?")  repeat=\(.repeat // "?")  status=\(.status // "?")" +
        (if .metrics then
          "\n    IOPS read/write: \(.metrics.iops_read // "-")/\(.metrics.iops_write // "-")" +
          "\n    MB/s read/write: \(.metrics.throughput_mb_read // "-")/\(.metrics.throughput_mb_write // "-")" +
          "\n    latency avg/p95/p99 ms: \(.metrics.latency_ms_avg // "-")/\(.metrics.latency_ms_p95 // "-")/\(.metrics.latency_ms_p99 // "-")"
        else "" end) +
        (if .error then "\n    error: \(.error)" else "" end)
      ' "${run_dir}/results.json" >"${tf}" 2>&1 || cat "${run_dir}/results.json" >"${tf}"
      ;;
    csv)
      if [ -f "${run_dir}/results.csv" ]; then
        printf 'CSV export: %s\n\n' "${run_dir}/results.csv" >"${tf}"
        head -n 50 "${run_dir}/results.csv" >>"${tf}" 2>&1
      else
        printf 'No CSV export found for %s (OUTPUT_FORMAT was likely "json").\n' "${run_id}" >"${tf}"
      fi
      ;;
  esac
  textbox_file "Performance dashboard: ${run_id} / ${section}" "${tf}"
}

# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------

main_menu() {
  menu "storage-perf-tester control menu" "Choose an action:" \
    configure     "Configure run (namespace, storageclasses, workloads, sizes, ...)" \
    start_run     "Start a performance run (background)" \
    stop_run      "Stop the tracked run" \
    status        "Status dashboard (live progress of the tracked run)" \
    tail_log      "Tail the tracked run's raw log output" \
    k8s_overview  "Kubernetes overview (jobs/pods/pvcs)" \
    perf_dash     "Performance dashboard (ranking / raw results of a run)" \
    discover      "Discover StorageClasses" \
    plan          "Plan (dry-run manifest render)" \
    cleanup       "Cleanup cluster resources" \
    quit          "Exit"
}

main() {
  detect_dialog
  require_cmd jq
  [ -x "${SPT_BIN}" ] || require_cmd bash
  load_defaults

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --namespace) NAMESPACE="$2"; shift 2 ;;
      --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
      -h|--help)
        echo "Usage: $0 [--namespace <ns>] [--output-dir <path>]"
        exit 0
        ;;
      *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
  done

  while true; do
    local choice
    if ! choice="$(main_menu)"; then
      break
    fi
    case "${choice}" in
      configure) action_configure_run ;;
      start_run) action_start_run ;;
      stop_run) action_stop_run ;;
      status) action_status_dashboard ;;
      tail_log) action_tail_log ;;
      k8s_overview) action_k8s_overview ;;
      perf_dash) action_performance_dashboard ;;
      discover) action_discover ;;
      plan) action_plan ;;
      cleanup) action_cleanup ;;
      quit|"") break ;;
    esac
  done
  clear
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
