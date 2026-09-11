# shellcheck shell=bash
# k8s.sh - kubectl helpers for storage-perf-tester.
# Sourced by bin/storage-perf-tester.sh; do not execute directly.

SPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE_DIR="${SPT_DIR}/templates"
SCRIPTS_EMBEDDED_DIR="${SPT_DIR}/scripts-embedded"

# Renders a template file, substituting only the explicitly given
# NAME=VALUE pairs (never arbitrary variables from the caller's
# environment), and prints the result to stdout.
#   render_template <template-file> NAME=VALUE [NAME=VALUE ...]
render_template() {
  tpl="$1"; shift
  varlist=""
  for kv in "$@"; do
    name="${kv%%=*}"
    varlist="${varlist}\${${name}} "
  done
  # shellcheck disable=SC2016
  env -i "PATH=${PATH}" "$@" envsubst "$(printf '%s' "$varlist")" <"${TEMPLATE_DIR}/${tpl}"
}

kubectl_apply_stdin() {
  kubectl apply -f -
}

# Discovers StorageClass names available in the cluster.
discover_storageclasses() {
  require_cmd kubectl
  kubectl get storageclass -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null || true
}

# Filters a newline separated list of discovered StorageClasses against an
# optional comma separated include list and an optional comma separated
# exclude list.
filter_storageclasses() {
  all="$1"
  include="$2"
  exclude="$3"
  if [ -n "$include" ]; then
    candidates="$(split_csv "$include")"
  else
    candidates="$all"
  fi
  if [ -n "$exclude" ]; then
    excl_file="$(mktemp)"
    split_csv "$exclude" >"$excl_file"
    printf '%s\n' "$candidates" | grep -v -x -F -f "$excl_file" || true
    rm -f "$excl_file"
  else
    printf '%s\n' "$candidates"
  fi
}

# Creates/updates the ConfigMap holding the embedded worker scripts.
apply_scripts_configmap() {
  name="$1"; namespace="$2"
  kubectl create configmap "$name" -n "$namespace" \
    --from-file="${SCRIPTS_EMBEDDED_DIR}/entrypoint-fio.sh" \
    --from-file="${SCRIPTS_EMBEDDED_DIR}/entrypoint-postgres.sh" \
    --dry-run=client -o yaml | kubectl apply -f -
}

ensure_namespace() {
  ns="$1"
  render_template namespace.yaml.tpl "NAMESPACE=${ns}" | kubectl_apply_stdin
}

# Reports the volumeBindingMode of a StorageClass (defaults to
# "Immediate" if it cannot be determined, matching the Kubernetes default).
storageclass_binding_mode() {
  sc="$1"
  mode="$(kubectl get storageclass "$sc" -o jsonpath='{.volumeBindingMode}' 2>/dev/null || true)"
  [ -n "$mode" ] && echo "$mode" || echo "Immediate"
}

# Waits for a freshly created PVC to become usable.
#   - StorageClasses with volumeBindingMode=WaitForFirstConsumer only bind
#     once a consuming Pod is scheduled; the Job created alongside the PVC
#     provides that consumer, so we don't block here (the Job wait covers
#     the rest) - prints "DeferredToConsumer".
#   - Otherwise, waits up to $timeout seconds for phase=Bound, printing
#     "Bound" or "Timeout".
wait_for_pvc_ready() {
  pvc="$1"; namespace="$2"; storageclass="$3"; timeout="$4"
  if [ "$(storageclass_binding_mode "$storageclass")" = "WaitForFirstConsumer" ]; then
    echo "DeferredToConsumer"; return 0
  fi
  if kubectl wait --for=jsonpath='{.status.phase}=Bound' "pvc/${pvc}" -n "$namespace" --timeout="${timeout}s" >/dev/null 2>&1; then
    echo "Bound"
  else
    echo "Timeout"
  fi
}

# Waits for a Job to finish (Complete or Failed condition), up to
# $timeout seconds. Prints "Complete", "Failed" or "Timeout" on stdout.
wait_for_job() {
  job="$1"; namespace="$2"; timeout="$3"
  if kubectl wait --for=condition=complete "job/${job}" -n "$namespace" --timeout="${timeout}s" >/dev/null 2>&1; then
    echo "Complete"; return 0
  fi
  if kubectl get job "$job" -n "$namespace" -o jsonpath='{.status.conditions[?(@.type=="Failed")].status}' 2>/dev/null | grep -q True; then
    echo "Failed"; return 0
  fi
  echo "Timeout"
}

job_pod_name() {
  job="$1"; namespace="$2"
  kubectl get pods -n "$namespace" -l "job-name=${job}" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true
}

job_logs() {
  job="$1"; namespace="$2"
  pod="$(job_pod_name "$job" "$namespace")"
  [ -n "$pod" ] || return 1
  kubectl logs "$pod" -n "$namespace" --container=worker 2>/dev/null || true
}

# Deletes every storage-perf-tester resource matching the given label
# selector inside the namespace (Jobs, Pods, ConfigMaps, PVCs). Never
# touches resources outside of $namespace or without the selector match,
# so pre-existing OSM workloads in other namespaces are never affected.
delete_by_label() {
  namespace="$1"; selector="$2"
  for kind in job pod configmap pvc; do
    kubectl delete "$kind" -n "$namespace" -l "$selector" --ignore-not-found=true --wait=false >/dev/null 2>&1 || true
  done
}
