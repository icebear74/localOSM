#!/usr/bin/env bash
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="osm"
BASE_DIR="/mnt/OSM"
HOSTPATH_BASE="/mnt/k8s"
CLEAN=false
YES=false
usage(){ cat <<EOF
Usage: $0 [--clean] [--yes] [--namespace <ns>] [--base-dir <path>] [--hostpath-base <path>]
EOF
}
if ! command -v kubectl >/dev/null 2>&1; then echo "ERROR: kubectl not found" >&2; exit 1; fi
if ! kubectl cluster-info >/dev/null 2>&1; then echo "ERROR: kubectl could not reach a Kubernetes cluster" >&2; exit 1; fi
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
is_safe_basedir(){ local dir; dir="$(realpath -m "$1" 2>/dev/null || echo "$1")"; [[ "$dir" == /* ]] || return 1; local unsafe=(/ /bin /boot /dev /etc /home /lib /lib64 /media /opt /proc /root /run /sbin /srv /sys /tmp /usr /var); for p in "${unsafe[@]}"; do [ "$dir" = "$p" ] && return 1; done; local depth; depth="$(echo "$dir" | tr -cd '/' | wc -c)"; [ "$depth" -ge 2 ] || return 1; }
namespace_exists(){ kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; }
wait_for_namespace_deletion(){ local timeout="$1" waited=0; while namespace_exists; do [ "$waited" -ge "$timeout" ] && return 1; sleep 2; waited=$((waited+2)); done; }
while [[ $# -gt 0 ]]; do case "$1" in --clean) CLEAN=true; shift ;; --yes) YES=true; shift ;; --namespace|-n) NAMESPACE="$2"; shift 2 ;; --base-dir) BASE_DIR="$2"; shift 2 ;; --hostpath-base) HOSTPATH_BASE="$2"; shift 2 ;; -h|--help) usage; exit 0 ;; *) echo "Unknown option: $1" >&2; exit 1 ;; esac; done
is_safe_basedir "$BASE_DIR" || { echo "ERROR: unsafe BASE_DIR '$BASE_DIR'" >&2; exit 1; }
is_safe_basedir "$HOSTPATH_BASE" || { echo "ERROR: unsafe HOSTPATH_BASE '$HOSTPATH_BASE'" >&2; exit 1; }
if [[ "$CLEAN" == true ]]; then
  echo "========================================"
  echo "  CLEAN START — all blue/green data will"
  echo "  be permanently deleted!"
  echo "========================================"
  if [[ "$YES" != true ]]; then read -r -p "Type 'yes' to continue, anything else to abort: " confirm; [[ "$confirm" == "yes" ]] || exit 0; fi
  if namespace_exists; then
    kubectl -n "$NAMESPACE" delete all --all --ignore-not-found --grace-period=0 --force --timeout=90s >/dev/null 2>&1 || true
    kubectl -n "$NAMESPACE" delete pvc --all --ignore-not-found --grace-period=0 --force --timeout=90s >/dev/null 2>&1 || true
    kubectl -n "$NAMESPACE" delete configmap --all --ignore-not-found --timeout=90s >/dev/null 2>&1 || true
    kubectl -n "$NAMESPACE" delete secret --all --ignore-not-found --timeout=90s >/dev/null 2>&1 || true
  fi
  for pv in $(kubectl get pv -o jsonpath='{range .items[?(@.spec.claimRef.namespace=="osm")]}{.metadata.name}{"
"}{end}' 2>/dev/null); do kubectl delete pv "$pv" --ignore-not-found >/dev/null 2>&1 || true; done
  kubectl delete namespace "$NAMESPACE" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  if ! wait_for_namespace_deletion 120; then
    kubectl patch namespace "$NAMESPACE" --type=merge -p '{"metadata":{"finalizers":[]}}' >/dev/null 2>&1 || true
    kubectl get namespace "$NAMESPACE" -o json 2>/dev/null | python3 -c 'import json,sys; o=json.load(sys.stdin); o.setdefault("spec",{}); o["spec"]["finalizers"]=[]; print(json.dumps(o))' | kubectl replace --raw "/api/v1/namespaces/'"$NAMESPACE"'/finalize" -f - >/dev/null 2>&1 || true
  fi
  for dir in "$BASE_DIR/tileserver" "$BASE_DIR/nominatim" "$BASE_DIR/valhalla" "$BASE_DIR/photon"; do [ -d "$dir" ] && $SUDO find "$dir" -mindepth 1 -maxdepth 1 -exec rm -rf {} +; done
fi
for dir in "$BASE_DIR/tileserver/blue" "$BASE_DIR/tileserver/green" "$BASE_DIR/nominatim/blue" "$BASE_DIR/nominatim/green" "$BASE_DIR/valhalla/blue" "$BASE_DIR/valhalla/green" "$BASE_DIR/photon/blue" "$BASE_DIR/photon/green" "$BASE_DIR/photon/bin"; do $SUDO mkdir -p "$dir"; done
$SUDO chown -R 1000:1000 "$BASE_DIR/tileserver" 2>/dev/null || true
$SUDO chown -R 100:100 "$BASE_DIR/nominatim" 2>/dev/null || true
$SUDO chown -R 1000:1000 "$BASE_DIR/valhalla" 2>/dev/null || true
$SUDO chown -R 0:0 "$BASE_DIR/photon" 2>/dev/null || true
kubectl apply -f "$REPO_ROOT/k8s/namespace.yaml"
kubectl apply -f "$REPO_ROOT/k8s/base/storage/local-path-storage-class.yaml"
kubectl apply -f "$REPO_ROOT/k8s/base/storage/hostpath-config.yaml"
kubectl apply -f "$REPO_ROOT/k8s/nominatim-import-config.yaml"
kubectl apply -f "$REPO_ROOT/k8s/nominatim-postgres-tuning-config.yaml"
kubectl apply -f "$REPO_ROOT/k8s/valhalla-config.yaml"
kubectl apply -f "$REPO_ROOT/k8s/valhalla-import-config.yaml"
kubectl apply -f "$REPO_ROOT/k8s/photon-config.yaml"
if [ -f "$REPO_ROOT/k8s/style.json" ]; then kubectl -n "$NAMESPACE" create configmap tileserver-style --from-file=style.json="$REPO_ROOT/k8s/style.json" --dry-run=client -o yaml | kubectl apply -f -; fi
for file in   k8s/base/pvcs/planet-storage-pvc.yaml   k8s/base/pvcs/tileserver-blue-pvc.yaml k8s/base/pvcs/tileserver-green-pvc.yaml   k8s/base/pvcs/nominatim-blue-pvc.yaml k8s/base/pvcs/nominatim-green-pvc.yaml   k8s/base/pvcs/valhalla-blue-pvc.yaml k8s/base/pvcs/valhalla-green-pvc.yaml   k8s/base/pvcs/photon-blue-pvc.yaml k8s/base/pvcs/photon-green-pvc.yaml   k8s/base/rbac/tileserver-orchestrator-rbac.yaml   k8s/base/deployments/tileserver-blue.yaml k8s/base/deployments/tileserver-green.yaml   k8s/base/deployments/nominatim-blue.yaml k8s/base/deployments/nominatim-green.yaml   k8s/base/deployments/valhalla-blue.yaml k8s/base/deployments/valhalla-green.yaml   k8s/base/deployments/photon-blue.yaml k8s/base/deployments/photon-green.yaml   k8s/base/services/tileserver-service.yaml k8s/base/services/tileserver-blue-service.yaml k8s/base/services/tileserver-green-service.yaml   k8s/base/services/nominatim-service.yaml k8s/base/services/nominatim-blue-service.yaml k8s/base/services/nominatim-green-service.yaml   k8s/base/services/valhalla-service.yaml k8s/base/services/valhalla-blue-service.yaml k8s/base/services/valhalla-green-service.yaml   k8s/base/services/photon-service.yaml k8s/base/services/photon-blue-service.yaml k8s/base/services/photon-green-service.yaml   k8s/base/configmaps/tileserver-import-manifest.yaml k8s/base/configmaps/nominatim-import-manifest.yaml k8s/base/configmaps/valhalla-import-manifest.yaml k8s/base/configmaps/photon-import-manifest.yaml   k8s/base/jobs/tileserver-import-orchestrator.yaml k8s/base/jobs/nominatim-import-orchestrator.yaml k8s/base/jobs/valhalla-import-orchestrator.yaml k8s/base/jobs/photon-import-orchestrator.yaml; do
  kubectl apply -f "$REPO_ROOT/$file"
done
echo "Blue/Green deployment complete."
