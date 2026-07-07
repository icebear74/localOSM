#!/usr/bin/env bash
set -euo pipefail

ASSUME_YES=false
DRY_RUN=false
CHANNEL="stable"
VERSION=""
CLEAN_DATA=true

usage() {
  cat <<'EOF'
Usage: bash scripts/reset-k3s.sh [options]

Fully removes K3s (including Rancher/Kubernetes leftovers) and installs K3s again.

Options:
  --yes                 Skip confirmation prompt
  --dry-run             Print actions without executing them
  --channel <name>      K3s channel (default: stable)
  --version <vX.Y.Z>    Install explicit K3s version (overrides --channel)
  --keep-data           Keep local data dirs (skip cleanup of /var/lib/rancher etc.)
  -h, --help            Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes) ASSUME_YES=true; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    --channel) CHANNEL="${2:-}"; shift 2 ;;
    --version) VERSION="${2:-}"; shift 2 ;;
    --keep-data) CLEAN_DATA=false; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if [[ -z "${CHANNEL}" ]]; then
  echo "ERROR: --channel requires a value." >&2
  exit 1
fi

if [[ -n "${VERSION}" && ! "${VERSION}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+ ]]; then
  echo "ERROR: --version must look like v1.31.1 (optionally with suffix)." >&2
  exit 1
fi

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  SUDO="sudo"
fi

run_cmd() {
  if [ "${DRY_RUN}" = true ]; then
    echo "[dry-run] $*"
  else
    echo "+ $*"
    "$@"
  fi
}

echo "WARNING: This will reset K3s on the current host."
echo "It removes running cluster resources and can delete local cluster state."
echo "Current context (if any):"
kubectl config current-context 2>/dev/null || true
echo ""

if [ "${ASSUME_YES}" != true ]; then
  read -r -p "Type 'yes' to continue: " confirm
  if [ "${confirm}" != "yes" ]; then
    echo "Aborted."
    exit 0
  fi
fi

echo "=== Stopping and uninstalling existing K3s ==="
if [ -x /usr/local/bin/k3s-uninstall.sh ]; then
  run_cmd ${SUDO} /usr/local/bin/k3s-uninstall.sh
else
  echo "k3s-uninstall.sh not found; continuing."
fi

if [ -x /usr/local/bin/k3s-agent-uninstall.sh ]; then
  run_cmd ${SUDO} /usr/local/bin/k3s-agent-uninstall.sh
else
  echo "k3s-agent-uninstall.sh not found; continuing."
fi

echo "=== Removing systemd/service leftovers ==="
run_cmd ${SUDO} systemctl disable --now k3s.service || true
run_cmd ${SUDO} systemctl disable --now k3s-agent.service || true
run_cmd ${SUDO} rm -f /etc/systemd/system/k3s.service /etc/systemd/system/k3s-agent.service || true
run_cmd ${SUDO} rm -f /etc/systemd/system/multi-user.target.wants/k3s.service || true
run_cmd ${SUDO} rm -f /etc/systemd/system/multi-user.target.wants/k3s-agent.service || true
run_cmd ${SUDO} systemctl daemon-reload || true

if [ "${CLEAN_DATA}" = true ]; then
  echo "=== Removing local K3s/Kubernetes data ==="
  run_cmd ${SUDO} rm -rf /etc/rancher /var/lib/rancher /var/lib/kubelet /var/lib/cni /etc/cni /run/k3s || true
  run_cmd ${SUDO} rm -f /usr/local/bin/k3s /usr/local/bin/kubectl /usr/local/bin/crictl /usr/local/bin/ctr || true
else
  echo "Skipping data cleanup (--keep-data)."
fi

echo "=== Installing fresh K3s ==="
if [ "${DRY_RUN}" = true ]; then
  if [ -n "${VERSION}" ]; then
    echo "[dry-run] INSTALL_K3S_VERSION=${VERSION} curl -sfL https://get.k3s.io | sh -"
  else
    echo "[dry-run] INSTALL_K3S_CHANNEL=${CHANNEL} curl -sfL https://get.k3s.io | sh -"
  fi
else
  if [ -n "${VERSION}" ]; then
    curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="${VERSION}" sh -
  else
    curl -sfL https://get.k3s.io | INSTALL_K3S_CHANNEL="${CHANNEL}" sh -
  fi
fi

echo "=== Waiting for K3s readiness ==="
if [ "${DRY_RUN}" = false ]; then
  if ! command -v kubectl >/dev/null 2>&1; then
    echo "kubectl missing in PATH; trying K3s bundled kubectl."
    if [ -x /usr/local/bin/kubectl ]; then
      export PATH="/usr/local/bin:${PATH}"
    fi
  fi
  for _ in $(seq 1 60); do
    if kubectl get nodes >/dev/null 2>&1; then
      break
    fi
    sleep 2
  done
  kubectl get nodes || true
fi

echo ""
echo "=== Done ==="
echo "K3s was reinstalled."
echo "Next step:"
echo "  bash scripts/deploy-osm.sh"
