# shellcheck shell=bash
# common.sh - shared logging/helper functions for storage-perf-tester.
# Sourced by bin/storage-perf-tester.sh; do not execute directly.

log_info()  { printf '[INFO]  %s\n' "$*" >&2; }
log_warn()  { printf '[WARN]  %s\n' "$*" >&2; }
log_error() { printf '[ERROR] %s\n' "$*" >&2; }
die()       { log_error "$*"; exit 1; }

require_cmd() {
  for c in "$@"; do
    command -v "$c" >/dev/null 2>&1 || die "Required command '$c' not found in PATH. See README.md prerequisites."
  done
}

# Reads a simple KEY=VALUE config file (comments '#' and blank lines
# ignored) and converts it into a compact JSON object on stdout.
conf_to_json() {
  file="$1"
  [ -f "$file" ] || die "Config file not found: $file"
  jq -Rn '
    [inputs
     | select(length > 0)
     | select(startswith("#") | not)
     | select(test("^[A-Za-z_][A-Za-z0-9_]*="))
     | capture("^(?<k>[A-Za-z_][A-Za-z0-9_]*)=(?<v>.*)$")]
    | map({(.k): (.v | tonumber? // .)}) | add // {}
  ' <"$file"
}

# Splits a comma separated list into newline separated entries, trimming
# whitespace and dropping empty entries.
split_csv() {
  printf '%s' "$1" | tr ',' '\n' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' | grep -v '^$' || true
}

# Runs "$@" while holding an exclusive lock on $LOCK_TARGET (a file path).
# Uses flock(1) when available (fast, no busy-waiting); otherwise falls
# back to a portable mkdir-based spinlock so the tool still works on hosts
# without util-linux.
with_lock() {
  target="$1"; shift
  if command -v flock >/dev/null 2>&1; then
    (
      flock -x 200
      "$@"
    ) 200>"${target}.lock"
  else
    lockdir="${target}.lockdir"
    until mkdir "$lockdir" 2>/dev/null; do sleep 0.1; done
    trap 'rmdir "$lockdir" 2>/dev/null || true' RETURN
    "$@"
  fi
}

# Sanitizes an arbitrary string into a lowercase, DNS-label-safe token
# suitable for use in Kubernetes object names/labels.
k8s_safe_name() {
  printf '%s' "$1" | tr '[:upper:]_' '[:lower:]-' | tr -cs 'a-z0-9-' '-' | sed 's/^-*//; s/-*$//'
}
