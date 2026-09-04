#!/usr/bin/env bash
#
# localosm-menu.sh: dialog/whiptail-driven control menu for localOSM.
#
# A single entry point for the operations that are otherwise spread across
# scripts/deploy-osm.sh, scripts/run-import.sh, scripts/remove-rancher.sh,
# scripts/reset-k3s.sh and the status dashboard's HTTP API
# (k8s/status-deployment.yaml, status/app.py). It does not re-implement any
# orchestration logic itself: library/build/promote/abort actions call the
# already-running status dashboard's REST API (same one used by the web UI),
# so there is exactly one place (status/app.py) that owns import-workflow
# state and locking.
#
# Usage: bash scripts/localosm-menu.sh [--url <status-url>] [--namespace <ns>]
#   Environment variables:
#     OSM_STATUS_URL: base URL of the status dashboard (default: autodetect)
#     OSM_NAMESPACE:  Kubernetes namespace of the OSM stack (default: osm)
#
# Requires: dialog or whiptail, curl, python3, kubectl (for cluster/job views
# and for scripts/deploy-osm.sh, scripts/remove-rancher.sh, scripts/reset-k3s.sh).

set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="${OSM_NAMESPACE:-osm}"
STATUS_URL="${OSM_STATUS_URL:-}"
STATUS_PORT="${OSM_STATUS_PORT:-30083}"
DIALOG_BIN=""
DIALOG_HEIGHT=22
DIALOG_WIDTH=78
MENU_HEIGHT=14

TMP_FILES=()
BG_PIDS=()

usage() {
  cat <<EOF
Usage: $0 [--url <status-dashboard-url>] [--namespace <k8s-namespace>]

Options:
  --url <url>         Base URL of the status dashboard, e.g. http://192.168.1.10:30083
                       (default: autodetect via localhost or the first node's InternalIP)
  --namespace <ns>     Kubernetes namespace the OSM stack runs in (default: osm)
  -h, --help           Show this help text
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url) STATUS_URL="${2:-}"; shift 2 ;;
    --namespace) NAMESPACE="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

cleanup() {
  local pid
  for pid in "${BG_PIDS[@]:-}"; do
    if [ -n "$pid" ]; then kill "$pid" >/dev/null 2>&1 || true; fi
  done
  local f
  for f in "${TMP_FILES[@]:-}"; do
    if [ -n "$f" ]; then rm -f "$f" 2>/dev/null || true; fi
  done
}
trap cleanup EXIT INT TERM

mktmp() {
  local f
  f="$(mktemp "${TMPDIR:-/tmp}/localosm-menu.XXXXXX")"
  TMP_FILES+=("$f")
  printf '%s\n' "$f"
}

require_cmd() {
  local cmd="$1" hint="${2:-}"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: required command '${cmd}' not found in PATH.${hint:+ ${hint}}" >&2
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

# Runs $DIALOG_BIN with the given args, always adding --backtitle. The dialog
# result is written to stderr by convention (both dialog and whiptail do
# this), so callers capture it via `3>&1 1>&2 2>&3`.
d() {
  "${DIALOG_BIN}" --backtitle "localOSM control menu (${STATUS_URL:-status URL not detected}, ns=${NAMESPACE})" "$@"
}

msgbox() {
  d --title "${1}" --msgbox "${2}" "${DIALOG_HEIGHT}" "${DIALOG_WIDTH}"
}

yesno() {
  d --title "${1}" --yesno "${2}" "${DIALOG_HEIGHT}" "${DIALOG_WIDTH}"
}

infobox() {
  d --title "${1}" --infobox "${2}" 8 "${DIALOG_WIDTH}"
}

textbox_file() {
  d --title "${1}" --textbox "${2}" "${DIALOG_HEIGHT}" "${DIALOG_WIDTH}"
}

inputbox() {
  local title="$1" prompt="$2" default="${3:-}"
  d --title "${title}" --inputbox "${prompt}" "${DIALOG_HEIGHT}" "${DIALOG_WIDTH}" "${default}" 3>&1 1>&2 2>&3
}

menu() {
  # $1 = title, $2 = prompt, remaining = tag/item pairs
  local title="$1" prompt="$2"
  shift 2
  d --title "${title}" --menu "${prompt}" "${DIALOG_HEIGHT}" "${DIALOG_WIDTH}" "${MENU_HEIGHT}" "$@" 3>&1 1>&2 2>&3
}

# ---------------------------------------------------------------------------
# Status dashboard HTTP API helpers
# ---------------------------------------------------------------------------

detect_status_url() {
  [ -n "${STATUS_URL}" ] && return 0
  if curl -fsS -m 2 "http://127.0.0.1:${STATUS_PORT}/healthz" >/dev/null 2>&1; then
    STATUS_URL="http://127.0.0.1:${STATUS_PORT}"
    return 0
  fi
  local node_ip=""
  if command -v kubectl >/dev/null 2>&1; then
    node_ip="$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null || true)"
  fi
  if [ -n "${node_ip}" ] && curl -fsS -m 2 "http://${node_ip}:${STATUS_PORT}/healthz" >/dev/null 2>&1; then
    STATUS_URL="http://${node_ip}:${STATUS_PORT}"
    return 0
  fi
  # Leave STATUS_URL empty; api_get/api_post will report a clear error.
  return 1
}

# api_get <path>  -> prints response body on stdout, returns curl's exit code
api_get() {
  local path="$1"
  [ -n "${STATUS_URL}" ] || { echo '{"error":"status dashboard URL unknown; use --url"}'; return 1; }
  curl -fsS -m 10 "${STATUS_URL}${path}"
}

# api_post <path> <json-body> -> prints response body on stdout
api_post() {
  local path="$1" body="$2"
  [ -n "${STATUS_URL}" ] || { echo '{"error":"status dashboard URL unknown; use --url"}'; return 1; }
  curl -fsS -m 30 -X POST -H "Content-Type: application/json" -d "${body}" "${STATUS_URL}${path}"
}

# json_get <json> <dotted.path> [default] -> value via python3 (no jq dependency,
# consistent with the rest of the repo's scripts, which already rely on python3).
# The JSON is passed as argv (not stdin) so the heredoc script text itself
# never collides with the data being parsed.
json_get() {
  local json="$1" path="$2" default="${3:-}"
  python3 - "${json}" "${path}" "${default}" <<'PY' 2>/dev/null || printf '%s' "${default}"
import json, sys
raw, path, default = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    data = json.loads(raw)
except Exception:
    print(default)
    sys.exit(0)
cur = data
for part in path.split('.'):
    if isinstance(cur, dict) and part in cur:
        cur = cur[part]
    else:
        print(default)
        sys.exit(0)
if isinstance(cur, bool):
    print("true" if cur else "false")
elif cur is None:
    print(default)
else:
    print(cur)
PY
}

# json_body key=value key2=value2 ... -> builds a JSON object safely via python3
json_body() {
  python3 - "$@" <<'PY'
import json, sys
obj = {}
for arg in sys.argv[1:]:
    key, _, value = arg.partition('=')
    obj[key] = value
print(json.dumps(obj))
PY
}

require_status_url() {
  if [ -z "${STATUS_URL}" ]; then
    if ! detect_status_url; then
      msgbox "Status dashboard not reachable" \
        "Could not reach the status dashboard on http://127.0.0.1:${STATUS_PORT} or any cluster node.\n\nDeploy the stack first, or pass --url http://<node-ip>:${STATUS_PORT}."
      return 1
    fi
  fi
  return 0
}

# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

action_deploy() {
  local clean=off preserve=off node_url tf
  if yesno "Deploy / update stack" "Run scripts/deploy-osm.sh now?\n\nThis (re)applies all Kubernetes manifests. Use --clean only if you explicitly want to wipe the namespace first."; then
    if yesno "Clean deploy?" "Pass --clean (deletes and recreates the 'osm' namespace)?\n\nChoose 'No' for a normal update-in-place deploy."; then
      clean=on
    fi
    if [ "${clean}" = "on" ] && yesno "Preserve downloads?" "Pass --preserve-downloads to keep the extract library across a clean deploy?"; then
      preserve=on
    fi
    node_url="$(inputbox "Node URL (optional)" "Public URL used by the dashboard for links, e.g. http://192.168.1.10 (leave empty to skip):" "")" || return 0
    tf="$(mktmp)"
    {
      echo "Running: bash scripts/deploy-osm.sh $([ "${clean}" = on ] && echo --clean) $([ "${preserve}" = on ] && echo --preserve-downloads) $([ -n "${node_url}" ] && echo "--node-url ${node_url}")"
      echo "---"
      local args=()
      [ "${clean}" = "on" ] && args+=(--clean)
      [ "${preserve}" = "on" ] && args+=(--preserve-downloads)
      [ -n "${node_url}" ] && args+=(--node-url "${node_url}")
      bash "${REPO_ROOT}/scripts/deploy-osm.sh" "${args[@]}"
      echo "---"
      echo "Deploy finished. Press any key to return to the menu."
    } >"${tf}" 2>&1 || true
    textbox_file "Deploy output" "${tf}"
  fi
}

action_library_add() {
  local name url out
  name="$(inputbox "Add & import now" "Custom extract name:")" || return 0
  [ -n "${name}" ] || return 0
  url="$(inputbox "Add & import now" "Extract .osm.pbf URL:")" || return 0
  [ -n "${url}" ] || return 0
  require_status_url || return 0
  out="$(api_post "/api/library/add" "$(json_body name="${name}" url="${url}")" 2>&1)" || true
  msgbox "Add & import" "$(echo "${out}" | python3 -m json.tool 2>/dev/null || echo "${out}")"
}

action_library_queue() {
  local name url out
  name="$(inputbox "Queue extract" "Custom extract name:")" || return 0
  [ -n "${name}" ] || return 0
  url="$(inputbox "Queue extract" "Extract .osm.pbf URL:")" || return 0
  [ -n "${url}" ] || return 0
  require_status_url || return 0
  out="$(api_post "/api/library/queue" "$(json_body name="${name}" url="${url}")" 2>&1)" || true
  msgbox "Queue extract" "$(echo "${out}" | python3 -m json.tool 2>/dev/null || echo "${out}")"
}

action_library_remove() {
  local slug out
  slug="$(inputbox "Remove queued extract" "Slug of the extract to remove (see status overview):")" || return 0
  [ -n "${slug}" ] || return 0
  require_status_url || return 0
  out="$(api_post "/api/library/remove" "$(json_body slug="${slug}")" 2>&1)" || true
  msgbox "Remove extract" "$(echo "${out}" | python3 -m json.tool 2>/dev/null || echo "${out}")"
}

action_check_updates() {
  require_status_url || return 0
  infobox "Check updates" "Checking library extracts for updates ..."
  local out
  out="$(api_post "/api/library/check-updates" "{}" 2>&1)" || true
  msgbox "Check updates" "$(echo "${out}" | python3 -m json.tool 2>/dev/null || echo "${out}")"
}

action_build_full() {
  local auto_promote=true out
  if yesno "Full build" "Run the full pipeline (tileserver, nominatim, valhalla, photon) for all queued/ready extracts?\n\nThis can take hours and consumes significant CPU/RAM/disk I/O."; then
    if ! yesno "Auto-promote?" "Automatically promote each service into production once its build succeeds?\n\nChoose 'No' to build into staging only and promote manually later."; then
      auto_promote=false
    fi
    require_status_url || return 0
    out="$(api_post "/api/library/build" "$(json_body auto_promote="${auto_promote}")" 2>&1)" || true
    msgbox "Full build" "$(echo "${out}" | python3 -m json.tool 2>/dev/null || echo "${out}")"
  fi
}

action_build_step() {
  local step
  step="$(menu "Run single build step" "Choose one pipeline step to (re)build into staging:" \
    tileserver "TileServer / Planetiler import" \
    nominatim "Nominatim import" \
    valhalla "Valhalla import" \
    photon "Photon import")" || return 0
  [ -n "${step}" ] || return 0
  yesno "Run step: ${step}" "Start the '${step}' build step now?\n\nThis builds into staging only; promote it afterwards from the menu." || return 0
  require_status_url || return 0
  local out
  out="$(api_post "/api/library/build-step" "$(json_body step="${step}")" 2>&1)" || true
  msgbox "Build step: ${step}" "$(echo "${out}" | python3 -m json.tool 2>/dev/null || echo "${out}")"
}

action_download_merge() {
  local download_only=false out
  if yesno "Download & merge" "Download and merge all queued extracts into a single planet.osm.pbf, without building any service?"; then
    if yesno "Download only?" "Only download (skip the merge step)?"; then
      download_only=true
    fi
    require_status_url || return 0
    out="$(api_post "/api/library/download-merge" "$(json_body download_only="${download_only}")" 2>&1)" || true
    msgbox "Download & merge" "$(echo "${out}" | python3 -m json.tool 2>/dev/null || echo "${out}")"
  fi
}

action_promote() {
  local service out
  service="$(menu "Promote staged data" "Choose which service's staged build to activate:" \
    tileserver "TileServer / Planetiler" \
    nominatim "Nominatim" \
    valhalla "Valhalla" \
    photon "Photon")" || return 0
  [ -n "${service}" ] || return 0
  yesno "Promote ${service}" "Promote staged ${service} data into production now?\n\nThis restarts the ${service} deployment." || return 0
  require_status_url || return 0
  out="$(api_post "/api/promote/${service}" "{}" 2>&1)" || true
  msgbox "Promote ${service}" "$(echo "${out}" | python3 -m json.tool 2>/dev/null || echo "${out}")"
}

action_abort() {
  yesno "Abort running import" "Abort whatever the import workflow is currently doing?\n\nRunning import Jobs are deleted; already-promoted data is kept." || return 0
  require_status_url || return 0
  local out
  out="$(api_post "/api/import/abort" "{}" 2>&1)" || true
  msgbox "Abort import" "$(echo "${out}" | python3 -m json.tool 2>/dev/null || echo "${out}")"
}

action_raw_status() {
  require_status_url || return 0
  local tf out
  tf="$(mktmp)"
  out="$(api_get "/api/status" 2>&1)" || true
  echo "${out}" | python3 -m json.tool >"${tf}" 2>/dev/null || echo "${out}" >"${tf}"
  textbox_file "Status dashboard: /api/status" "${tf}"
}

# Polls /api/status and shows the import workflow's live progress in a
# dialog gauge until it stops running (or ~30 minutes elapse, whichever is
# first). This is the "query progress of running jobs" entry point.
action_progress_gauge() {
  require_status_url || return 0
  local max_iterations=900 i=0
  {
    while [ "${i}" -lt "${max_iterations}" ]; do
      local status_json running phase message detail updated_at progress jobs_line
      status_json="$(api_get "/api/status" 2>/dev/null || echo '{}')"
      running="$(json_get "${status_json}" workflow.running false)"
      phase="$(json_get "${status_json}" workflow.phase "unknown")"
      message="$(json_get "${status_json}" workflow.message "")"
      detail="$(json_get "${status_json}" workflow.detail "")"
      updated_at="$(json_get "${status_json}" workflow.updated_at "")"
      progress="$(json_get "${status_json}" workflow.progress 0)"
      case "${progress}" in ''|*[!0-9]*) progress=0 ;; esac
      jobs_line=""
      if command -v kubectl >/dev/null 2>&1; then
        jobs_line="$(kubectl -n "${NAMESPACE}" get jobs --no-headers 2>/dev/null | awk '{printf "%s: %s  ", $1, $2}')"
      fi
      printf 'XXX\n%s\nPhase: %s\nMessage: %s\nDetail: %s\nUpdated: %s\n\nKubernetes Jobs (completions):\n%s\nXXX\n' \
        "${progress}" "${phase}" "${message}" "${detail}" "${updated_at}" "${jobs_line:-(none)}"
      if [ "${running}" != "true" ]; then
        printf 'XXX\n100\nWorkflow finished (phase=%s). Closing in 2s ...\nXXX\n' "${phase}"
        sleep 2
        break
      fi
      sleep 3
      i=$((i + 1))
    done
  } | d --title "Live import progress (auto-closes when finished)" --gauge "Polling ${STATUS_URL}/api/status ..." "${DIALOG_HEIGHT}" "${DIALOG_WIDTH}" 0
}

action_k8s_overview() {
  require_cmd kubectl
  local tf
  tf="$(mktmp)"
  {
    echo "Namespace: ${NAMESPACE} (refreshed: $(date '+%Y-%m-%d %H:%M:%S'))"
    echo "=== Deployments ==="
    kubectl -n "${NAMESPACE}" get deployments -o wide 2>&1
    echo
    echo "=== Jobs ==="
    kubectl -n "${NAMESPACE}" get jobs -o wide 2>&1
    echo
    echo "=== Pods ==="
    kubectl -n "${NAMESPACE}" get pods -o wide 2>&1
    echo
    echo "=== PVCs ==="
    kubectl -n "${NAMESPACE}" get pvc 2>&1
  } >"${tf}"
  textbox_file "Kubernetes overview (${NAMESPACE})" "${tf}"
}

action_tail_logs() {
  require_cmd kubectl
  local pods pod tf pid choices=()
  pods="$(kubectl -n "${NAMESPACE}" get pods -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null || true)"
  if [ -z "${pods}" ]; then
    msgbox "Tail pod logs" "No pods found in namespace '${NAMESPACE}'."
    return 0
  fi
  while IFS= read -r p; do
    [ -n "$p" ] && choices+=("$p" "")
  done <<<"${pods}"
  pod="$(menu "Tail pod logs" "Choose a pod to follow (Esc/Cancel to stop):" "${choices[@]}")" || return 0
  [ -n "${pod}" ] || return 0
  tf="$(mktmp)"
  printf 'Following logs for pod/%s in namespace %s ...\n\n' "${pod}" "${NAMESPACE}" >"${tf}"
  kubectl -n "${NAMESPACE}" logs -f "${pod}" --all-containers --tail=200 >>"${tf}" 2>&1 &
  pid=$!
  BG_PIDS+=("${pid}")
  d --title "Logs: ${pod} (live, Esc/Cancel to stop)" --tailbox "${tf}" "${DIALOG_HEIGHT}" 100 || true
  kill "${pid}" >/dev/null 2>&1 || true
  wait "${pid}" 2>/dev/null || true
}

action_maintenance() {
  local choice
  choice="$(menu "Cluster maintenance (destructive)" "Choose an action. Both scripts prompt for --yes below and support --dry-run first." \
    remove_rancher_dry "Preview: remove Rancher/Fleet resources (--dry-run)" \
    remove_rancher "Remove Rancher/Fleet resources (destructive)" \
    reset_k3s_dry "Preview: reset K3s (--dry-run)" \
    reset_k3s "Fully reset K3s (destructive, keeps local data by default)")" || return 0
  [ -n "${choice}" ] || return 0

  case "${choice}" in
    remove_rancher_dry)
      run_and_show "bash scripts/remove-rancher.sh --dry-run" "${REPO_ROOT}/scripts/remove-rancher.sh" --dry-run
      ;;
    remove_rancher)
      yesno "Confirm destructive action" "This deletes Rancher/Fleet resources from the CURRENT cluster context.\n\nContinue?" || return 0
      run_and_show "bash scripts/remove-rancher.sh --yes" "${REPO_ROOT}/scripts/remove-rancher.sh" --yes
      ;;
    reset_k3s_dry)
      run_and_show "bash scripts/reset-k3s.sh --dry-run" "${REPO_ROOT}/scripts/reset-k3s.sh" --dry-run
      ;;
    reset_k3s)
      yesno "Confirm destructive action" "This REMOVES and REINSTALLS K3s on this host, deleting the whole cluster (local data under /var/lib/rancher is kept by default).\n\nContinue?" || return 0
      run_and_show "bash scripts/reset-k3s.sh --yes" "${REPO_ROOT}/scripts/reset-k3s.sh" --yes
      ;;
  esac
}

run_and_show() {
  local label="$1"; shift
  local tf
  tf="$(mktmp)"
  {
    echo "Running: ${label}"
    echo "---"
    "$@"
    echo "---"
    echo "Finished. Press any key to return to the menu."
  } >"${tf}" 2>&1 || true
  textbox_file "${label}" "${tf}"
}

action_settings() {
  local new_url new_ns
  new_url="$(inputbox "Settings" "Status dashboard URL (empty = autodetect):" "${STATUS_URL}")" || return 0
  STATUS_URL="${new_url}"
  new_ns="$(inputbox "Settings" "Kubernetes namespace:" "${NAMESPACE}")" || return 0
  [ -n "${new_ns}" ] && NAMESPACE="${new_ns}"
}

# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------

main_menu() {
  local choice
  choice="$(menu "localOSM control menu" "Choose an action:" \
    deploy        "Deploy / update the stack (scripts/deploy-osm.sh)" \
    lib_add       "Add extract & import now" \
    lib_queue     "Queue extract for later build" \
    lib_remove    "Remove a queued extract" \
    check_updates "Check library extracts for updates" \
    build_full    "Start full build (all queued extracts)" \
    build_step    "Run a single build step (tileserver/nominatim/valhalla/photon)" \
    download_merge "Download & merge extracts only" \
    promote       "Promote staged data for a service" \
    abort         "Abort the running import" \
    progress      "Live progress of the running import (auto-refreshing)" \
    raw_status    "View raw status dashboard JSON" \
    k8s_overview  "Kubernetes overview (deployments/jobs/pods/pvcs)" \
    tail_logs     "Tail logs of a running pod" \
    maintenance   "Cluster maintenance (remove-rancher / reset-k3s)" \
    settings      "Settings (status URL, namespace)" \
    quit          "Exit")"
  echo "${choice}"
}

main() {
  detect_dialog
  require_cmd curl
  require_cmd python3
  detect_status_url || true

  while true; do
    local choice
    if ! choice="$(main_menu)"; then
      break
    fi
    case "${choice}" in
      deploy) action_deploy ;;
      lib_add) action_library_add ;;
      lib_queue) action_library_queue ;;
      lib_remove) action_library_remove ;;
      check_updates) action_check_updates ;;
      build_full) action_build_full ;;
      build_step) action_build_step ;;
      download_merge) action_download_merge ;;
      promote) action_promote ;;
      abort) action_abort ;;
      progress) action_progress_gauge ;;
      raw_status) action_raw_status ;;
      k8s_overview) action_k8s_overview ;;
      tail_logs) action_tail_logs ;;
      maintenance) action_maintenance ;;
      settings) action_settings ;;
      quit|"") break ;;
    esac
  done
  clear
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi