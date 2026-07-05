#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=false
ASSUME_YES=false
TIMEOUT_SECS=180

usage() {
  cat <<'EOF'
Usage: bash scripts/remove-rancher.sh [options]

Removes Rancher/Fleet resources from the current Kubernetes cluster context.

Options:
  --dry-run          Show what would be deleted without changing the cluster
  --yes              Skip confirmation prompt
  --timeout <secs>   Wait timeout per namespace deletion (default: 180)
  -h, --help         Show this help text
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    --yes) ASSUME_YES=true; shift ;;
    --timeout)
      TIMEOUT_SECS="${2:-}"
      if [[ -z "${TIMEOUT_SECS}" || ! "${TIMEOUT_SECS}" =~ ^[0-9]+$ ]]; then
        echo "ERROR: --timeout requires a numeric value in seconds." >&2
        exit 1
      fi
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if ! command -v kubectl >/dev/null 2>&1; then
  echo "ERROR: kubectl not found in PATH." >&2
  exit 1
fi

if ! kubectl cluster-info >/dev/null 2>&1; then
  echo "ERROR: Could not reach Kubernetes cluster with current kubectl context." >&2
  exit 1
fi

run_cmd() {
  if [ "${DRY_RUN}" = true ]; then
    echo "[dry-run] $*"
  else
    echo "+ $*"
    "$@"
  fi
}

wait_for_namespace_gone() {
  local ns="$1"
  local waited=0
  while kubectl get ns "${ns}" >/dev/null 2>&1; do
    if [ "${waited}" -ge "${TIMEOUT_SECS}" ]; then
      return 1
    fi
    sleep 2
    waited=$((waited + 2))
  done
  return 0
}

force_namespace_finalize() {
  local ns="$1"
  if [ "${DRY_RUN}" = true ]; then
    echo "[dry-run] force finalize namespace ${ns}"
    return 0
  fi

  kubectl patch namespace "${ns}" --type=merge -p '{"metadata":{"finalizers":[]}}' >/dev/null 2>&1 || true
  kubectl get namespace "${ns}" -o json 2>/dev/null \
    | python3 -c 'import json,sys;o=json.load(sys.stdin);o.setdefault("spec",{});o["spec"]["finalizers"]=[];print(json.dumps(o))' \
    | kubectl replace --raw "/api/v1/namespaces/${ns}/finalize" -f - >/dev/null 2>&1 || true
}

if [ "${ASSUME_YES}" != true ]; then
  echo "WARNING: This will remove Rancher/Fleet resources from the current cluster context."
  kubectl config current-context 2>/dev/null || true
  read -r -p "Continue? Type 'yes' to proceed: " confirm
  if [ "${confirm}" != "yes" ]; then
    echo "Aborted."
    exit 0
  fi
fi

echo "=== Discovering Rancher-related resources ==="

mapfile -t APISERVICES < <(kubectl get apiservice -o name 2>/dev/null | grep -E '(cattle|rancher|fleet)' || true)
mapfile -t CRDS < <(kubectl get crd -o name 2>/dev/null | grep -E '(cattle|rancher|fleet)' || true)
mapfile -t MUTATING_WEBHOOKS < <(kubectl get mutatingwebhookconfiguration -o name 2>/dev/null | grep -E '(cattle|rancher|fleet)' || true)
mapfile -t VALIDATING_WEBHOOKS < <(kubectl get validatingwebhookconfiguration -o name 2>/dev/null | grep -E '(cattle|rancher|fleet)' || true)
mapfile -t CLUSTER_ROLES < <(kubectl get clusterrole -o name 2>/dev/null | grep -E '(cattle|rancher|fleet)' || true)
mapfile -t CLUSTER_ROLE_BINDINGS < <(kubectl get clusterrolebinding -o name 2>/dev/null | grep -E '(cattle|rancher|fleet)' || true)
mapfile -t NAMESPACES < <(kubectl get ns -o custom-columns=NAME:.metadata.name --no-headers 2>/dev/null | grep -E '^(cattle|fleet)(-|$)|^local$|^rancher-system$' || true)

if command -v helm >/dev/null 2>&1; then
  mapfile -t HELM_RELEASES < <(helm ls -A --short 2>/dev/null | grep -E '(rancher|cattle|fleet)' || true)
else
  HELM_RELEASES=()
fi

echo "Found:"
echo "  Namespaces: ${#NAMESPACES[@]}"
echo "  APIService: ${#APISERVICES[@]}"
echo "  CRDs: ${#CRDS[@]}"
echo "  Webhooks: $(( ${#MUTATING_WEBHOOKS[@]} + ${#VALIDATING_WEBHOOKS[@]} ))"
echo "  ClusterRoles: ${#CLUSTER_ROLES[@]}"
echo "  ClusterRoleBindings: ${#CLUSTER_ROLE_BINDINGS[@]}"
echo "  Helm releases: ${#HELM_RELEASES[@]}"

echo ""
echo "=== Removing Helm releases (if any) ==="
if command -v helm >/dev/null 2>&1; then
  while IFS=$'\t' read -r rel ns; do
    [ -z "${rel}" ] && continue
    run_cmd helm uninstall "${rel}" -n "${ns}"
  done < <(helm ls -A -o json 2>/dev/null \
            | python3 - <<'PY'
import json,sys,re
data = json.load(sys.stdin)
for r in data:
    name = r.get("name","")
    ns = r.get("namespace","")
    if re.search(r"(rancher|cattle|fleet)", name):
        print(f"{name}\t{ns}")
PY
          )
else
  echo "helm not found; skipping Helm release cleanup."
fi

echo ""
echo "=== Removing admission webhooks ==="
for item in "${MUTATING_WEBHOOKS[@]}" "${VALIDATING_WEBHOOKS[@]}"; do
  [ -z "${item}" ] && continue
  run_cmd kubectl delete "${item}" --ignore-not-found
done

echo ""
echo "=== Removing APIService entries ==="
for item in "${APISERVICES[@]}"; do
  [ -z "${item}" ] && continue
  run_cmd kubectl delete "${item}" --ignore-not-found
done

echo ""
echo "=== Removing CRDs ==="
for item in "${CRDS[@]}"; do
  [ -z "${item}" ] && continue
  run_cmd kubectl delete "${item}" --ignore-not-found
done

echo ""
echo "=== Removing ClusterRoles / ClusterRoleBindings ==="
for item in "${CLUSTER_ROLE_BINDINGS[@]}"; do
  [ -z "${item}" ] && continue
  run_cmd kubectl delete "${item}" --ignore-not-found
done
for item in "${CLUSTER_ROLES[@]}"; do
  [ -z "${item}" ] && continue
  run_cmd kubectl delete "${item}" --ignore-not-found
done

echo ""
echo "=== Removing Rancher/Fleet namespaces ==="
for ns in "${NAMESPACES[@]}"; do
  [ -z "${ns}" ] && continue
  run_cmd kubectl delete namespace "${ns}" --ignore-not-found --wait=false
done

if [ "${#NAMESPACES[@]}" -gt 0 ]; then
  echo ""
  echo "=== Waiting for namespace deletion and forcing finalizers when required ==="
  for ns in "${NAMESPACES[@]}"; do
    [ -z "${ns}" ] && continue
    if ! kubectl get ns "${ns}" >/dev/null 2>&1; then
      continue
    fi
    if wait_for_namespace_gone "${ns}"; then
      echo "Namespace ${ns} deleted."
      continue
    fi
    echo "Namespace ${ns} stuck terminating. Forcing finalizer cleanup ..."
    force_namespace_finalize "${ns}"
    if wait_for_namespace_gone "${ns}"; then
      echo "Namespace ${ns} force-deleted."
    else
      echo "WARNING: Namespace ${ns} still terminating. Inspect with: kubectl get ns ${ns} -o yaml" >&2
    fi
  done
fi

echo ""
echo "=== Done ==="
echo "Tip: verify leftovers with"
echo "  kubectl get apiservice | grep -E 'cattle|rancher|fleet' || true"
echo "  kubectl get crd | grep -E 'cattle|rancher|fleet' || true"
echo "  kubectl get ns | grep -E 'cattle|rancher|fleet' || true"
