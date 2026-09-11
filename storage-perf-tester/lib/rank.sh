# shellcheck shell=bash
# rank.sh - builds the jq input document (results + weights) and runs
# lib/rank.jq to produce the ranking report.
# Sourced by bin/storage-perf-tester.sh; do not execute directly.

RANK_JQ="${SPT_DIR}/lib/rank.jq"

# $1 = results.json (array), $2 = weights file (KEY=VALUE), $3 = output path
compute_ranking() {
  results_file="$1"; weights_file="$2"; out_file="$3"

  weights_json="$(conf_to_json "$weights_file")"
  workload_weights="$(printf '%s' "$weights_json" | jq -c '
    to_entries
    | map(select(.key | startswith("WORKLOAD_WEIGHT_")))
    | map({key: (.key | sub("^WORKLOAD_WEIGHT_"; "") | gsub("_"; "-")), value: .value})
    | from_entries
  ')"
  metric_weights="$(printf '%s' "$weights_json" | jq -c '
    {
      iops: (.METRIC_WEIGHT_IOPS // 1),
      throughput: (.METRIC_WEIGHT_THROUGHPUT // 1),
      latency: (.METRIC_WEIGHT_LATENCY // 1)
    }
  ')"

  jq -n --slurpfile results "$results_file" \
        --argjson workload_weights "$workload_weights" \
        --argjson metric_weights "$metric_weights" \
        '{results: $results[0], workload_weights: $workload_weights, metric_weights: $metric_weights}' \
    | jq -f "$RANK_JQ" >"$out_file"
}

# Renders results.json as CSV (one row per storageclass/workload/queue_depth/repeat).
results_to_csv() {
  results_file="$1"
  echo "storageclass,workload,queue_depth,repeat,phase,status,iops_read,iops_write,throughput_mb_read,throughput_mb_write,latency_ms_avg,latency_ms_p95,latency_ms_p99,error"
  jq -r '
    .[] | [
      (.storageclass // ""), (.workload // ""), (.queue_depth // ""), (.repeat // ""),
      (.phase // ""), (.status // ""),
      (.metrics.iops_read // 0), (.metrics.iops_write // 0),
      (.metrics.throughput_mb_read // 0), (.metrics.throughput_mb_write // 0),
      (.metrics.latency_ms_avg // 0), (.metrics.latency_ms_p95 // 0), (.metrics.latency_ms_p99 // 0),
      ((.error // "") | gsub("[\r\n,]"; " "))
    ] | @csv
  ' "$results_file"
}
