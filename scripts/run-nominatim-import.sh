#!/usr/bin/env bash
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="${NOMINATIM_NAMESPACE:-osm}"
SUFFIX=$(date +%Y%m%d%H%M%S)
JOB_NAME="nominatim-import-orchestrator-${SUFFIX}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace|-n) NAMESPACE="$2"; shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done
MANIFEST_PATH="$REPO_ROOT/k8s/base/jobs/nominatim-import-orchestrator.yaml"
TMP_MANIFEST="$(mktemp)"
trap 'rm -f "$TMP_MANIFEST"' EXIT
python3 - "$MANIFEST_PATH" "$JOB_NAME" "$NAMESPACE" "$TMP_MANIFEST" <<'PY'
import pathlib
import sys

source_path, job_name, namespace, output_path = sys.argv[1:]
lines = pathlib.Path(source_path).read_text().splitlines()
modified = False
in_metadata = False
for idx, line in enumerate(lines):
    if line == "metadata:":
        in_metadata = True
        continue
    if in_metadata and line.startswith("  name:"):
        lines[idx] = f"  name: {job_name}"
        modified = True
        in_metadata = False
        continue
    if in_metadata and line.startswith("  namespace:"):
        lines[idx] = f"  namespace: {namespace}"
        modified = True
        in_metadata = False
        continue
    if in_metadata and line and not line.startswith(" "):
        in_metadata = False

if not modified:
    raise SystemExit(f"Unable to locate metadata.name/metadata.namespace in {source_path}")
pathlib.Path(output_path).write_text("\n".join(lines) + "\n")
PY
echo "Creating nominatim orchestrator job '${JOB_NAME}' in namespace '${NAMESPACE}' ..."
kubectl apply -f "$TMP_MANIFEST"
echo
echo "Job created. Follow logs with:"
echo "  kubectl logs -f job/${JOB_NAME} -n ${NAMESPACE}"
