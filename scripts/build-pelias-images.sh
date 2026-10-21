#!/usr/bin/env bash
# Build the runtime, parser and ICU-enabled ES from the immutable source lock.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGISTRY="${PELIAS_IMAGE_REGISTRY:-localosm}"
TAG="${PELIAS_IMAGE_TAG:-2026-10-21}"
PUSH=false
case "${1:-}" in
  "") ;;
  --push) PUSH=true ;;
  *) echo "Usage: $0 [--push] (PELIAS_IMAGE_REGISTRY, PELIAS_IMAGE_TAG)" >&2; exit 2 ;;
esac
mapfile -t LOCK < <(python3 - "$ROOT/pelias/images.lock.json" <<'PY'
import json
import re
import sys

lock = json.load(open(sys.argv[1]))
for name, source in lock["repositories"].items():
    if not re.fullmatch(r"[a-f0-9]{40}", source["commit"]):
        raise SystemExit("unlocked source: " + name)
for name in ("node", "go", "libpostal", "elasticsearch"):
    image = lock["bases"][name]
    if not re.search(r"@sha256:[a-f0-9]{64}$", image):
        raise SystemExit("unlocked base: " + name)
    print(image)
print(lock["repositories"]["libpostal-server"]["commit"])
print(lock["elasticsearch"])
PY
)
test "${#LOCK[@]}" = 6 || { echo "Invalid Pelias image lock" >&2; exit 1; }
docker build --file "$ROOT/pelias/Dockerfile" \
  --build-arg "NODE_IMAGE=${LOCK[0]}" \
  --tag "$REGISTRY/pelias-runtime:$TAG" "$ROOT"
docker build --file "$ROOT/pelias/Dockerfile.libpostal" \
  --build-arg "GO_IMAGE=${LOCK[1]}" \
  --build-arg "LIBPOSTAL_IMAGE=${LOCK[2]}" \
  --build-arg "LIBPOSTAL_SERVER_COMMIT=${LOCK[4]}" \
  --tag "$REGISTRY/pelias-libpostal:$TAG" "$ROOT"
docker build --file "$ROOT/pelias/Dockerfile.elasticsearch" \
  --build-arg "ELASTICSEARCH_IMAGE=${LOCK[3]}" \
  --tag "$REGISTRY/pelias-elasticsearch:${LOCK[5]}" "$ROOT"
if "$PUSH"; then
  docker push "$REGISTRY/pelias-runtime:$TAG"
  docker push "$REGISTRY/pelias-libpostal:$TAG"
  docker push "$REGISTRY/pelias-elasticsearch:${LOCK[5]}"
fi
printf 'Built %s/pelias-{runtime,libpostal}:%s\n' "$REGISTRY" "$TAG"
printf 'Built %s/pelias-elasticsearch:%s (analysis-icu enabled)\n' "$REGISTRY" "${LOCK[5]}"
printf '%s\n' 'Import all three images into eligible Kubernetes nodes, or push and set the manifest image registry.'
