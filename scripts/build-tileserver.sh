#!/usr/bin/env bash
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="${TILESERVER_NAMESPACE:-osm}"
JOB_NAME=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace|-n) NAMESPACE="$2"; shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done
output=$(bash "$REPO_ROOT/scripts/run-tileserver-import.sh" --namespace "$NAMESPACE")
printf '%s
' "$output"
JOB_NAME=$(printf '%s
' "$output" | sed -n "s/Creating tileserver orchestrator job '\([^']*\)'.*//p")
[ -n "$JOB_NAME" ] || { echo "Could not determine created job name" >&2; exit 1; }
echo "Waiting for $JOB_NAME ..."
kubectl wait --for=condition=complete "job/$JOB_NAME" -n "$NAMESPACE" --timeout=21600s || {
  kubectl logs "job/$JOB_NAME" -n "$NAMESPACE" --tail=200 || true
  exit 1
}
kubectl logs "job/$JOB_NAME" -n "$NAMESPACE" --tail=200 || true
