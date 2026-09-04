# shellcheck shell=bash
# collect.sh - extracts SPT-RESULT-BEGIN/END JSON blocks from a Job's pod
# logs and appends them (with the storageclass context added) to a
# results array file.
# Sourced by bin/storage-perf-tester.sh; do not execute directly.

# Extracts every JSON block between ===SPT-RESULT-BEGIN=== and
# ===SPT-RESULT-END=== markers in $1 (a file with captured pod logs) and
# prints them as a JSON array on stdout. Prints "[]" if none are found.
extract_result_blocks() {
  logfile="$1"
  awk '
    /^===SPT-RESULT-BEGIN===$/ { capture=1; buf=""; next }
    /^===SPT-RESULT-END===$/   { capture=0; print buf; next }
    capture { buf = buf $0 }
  ' "$logfile" | jq -c -s '.'
}

# Appends the result blocks found in $logfile to $results_file (a JSON
# array file), tagging each entry with storageclass/status metadata. If no
# parsable result was found, records a single synthetic error entry so the
# failure is still visible in the final report.
collect_job_result() {
  logfile="$1"; results_file="$2"; storageclass="$3"; workload="$4"
  queue_depth="$5"; repeat="$6"; job_status="$7"

  blocks="$(extract_result_blocks "$logfile" 2>/dev/null || echo '[]')"
  count="$(printf '%s' "$blocks" | jq 'length')"

  if [ "$count" -eq 0 ]; then
    blocks=$(jq -nc --arg workload "$workload" --arg qd "$queue_depth" --arg repeat "$repeat" \
      --arg status "$job_status" \
      '[{workload:$workload, queue_depth:($qd|tonumber), repeat:($repeat|tonumber), engine:"unknown",
         status:"error", error:("no result reported by worker pod (job status: " + $status + ")")}]')
  fi

  tagged="$(printf '%s' "$blocks" | jq -c --arg sc "$storageclass" 'map(. + {storageclass: $sc})')"

  tmp="$(mktemp)"
  jq -c --argjson new "$tagged" '. + $new' "$results_file" >"$tmp" && mv "$tmp" "$results_file"
}
