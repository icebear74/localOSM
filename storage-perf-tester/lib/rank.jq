# rank.jq - computes per-workload and overall StorageClass rankings from
# collected storage-perf-tester results.
#
# Input (via --argjson / --slurpfile, see lib/rank.sh):
#   .results          : flat array of normalized result objects, one per
#                       (storageclass, workload, queue_depth, repeat, phase)
#   .workload_weights : {workload_id: number, ...}  (see config/weights.default.conf)
#   .metric_weights   : {iops: number, throughput: number, latency: number}
#
# Output: {generated_at, workloads: {<workload>: [{storageclass, score, rank, ...}]},
#          overall: [{storageclass, score, rank}], errors: [...]}

def norm01(vals; v):
  # min-max normalize v within vals; if all values are equal, everyone
  # gets 1.0 so a tie never looks like a penalty.
  (vals | min) as $min | (vals | max) as $max |
  if ($max - $min) == 0 then 1.0 else ((v - $min) / ($max - $min)) end;

# 1) Merge phases (e.g. large-files write+read) that share the same
#    (storageclass, workload, queue_depth, repeat): sum throughput/iops,
#    average latency, OR the status.
def merge_phases:
  group_by([.storageclass, .workload, .queue_depth, .repeat])
  | map({
      storageclass: .[0].storageclass,
      workload: .[0].workload,
      queue_depth: .[0].queue_depth,
      repeat: .[0].repeat,
      status: (if any(.[]; .status != "ok") then "error" else "ok" end),
      error: ([.[] | select(.status != "ok") | .error] | join("; ")),
      iops: ([.[] | (.metrics.iops_read // 0) + (.metrics.iops_write // 0)] | add),
      throughput_mb: ([.[] | (.metrics.throughput_mb_read // 0) + (.metrics.throughput_mb_write // 0)] | add),
      latency_ms: (([.[] | .metrics.latency_ms_avg // 0] | add) / (length))
    });

# 2) Average repeats for the same (storageclass, workload, queue_depth).
def merge_repeats:
  group_by([.storageclass, .workload, .queue_depth])
  | map({
      storageclass: .[0].storageclass,
      workload: .[0].workload,
      queue_depth: .[0].queue_depth,
      status: (if any(.[]; .status != "ok") then "error" else "ok" end),
      error: ([.[] | select(.status != "ok") | .error] | join("; ")),
      iops: (([.[] | select(.status == "ok") | .iops] | add // 0) / (([.[] | select(.status == "ok")] | length) | if . == 0 then 1 else . end)),
      throughput_mb: (([.[] | select(.status == "ok") | .throughput_mb] | add // 0) / (([.[] | select(.status == "ok")] | length) | if . == 0 then 1 else . end)),
      latency_ms: (([.[] | select(.status == "ok") | .latency_ms] | add // 0) / (([.[] | select(.status == "ok")] | length) | if . == 0 then 1 else . end))
    });

# 3) Normalize per (workload, queue_depth) group across storageclasses,
#    then combine into one weighted sub-score per row using metric_weights.
def score_rows($metric_weights):
  ($metric_weights.iops + $metric_weights.throughput + $metric_weights.latency) as $mw_sum |
  group_by([.workload, .queue_depth])
  | map(
      (map(.iops) ) as $iops_vals |
      (map(.throughput_mb)) as $tp_vals |
      (map(.latency_ms)) as $lat_vals |
      map(. + {
        norm_iops: norm01($iops_vals; .iops),
        norm_throughput: norm01($tp_vals; .throughput_mb),
        # lower latency is better -> invert after normalizing
        norm_latency: (1 - norm01($lat_vals; .latency_ms))
      })
      | map(. + {
          score: (
            (.norm_iops * $metric_weights.iops
             + .norm_throughput * $metric_weights.throughput
             + .norm_latency * $metric_weights.latency) / $mw_sum
          )
        })
    )
  | flatten;

# 4) Average the per-queue-depth scores into one score per
#    (storageclass, workload), then rank storageclasses within it.
def workload_rankings:
  group_by([.storageclass, .workload])
  | map({
      storageclass: .[0].storageclass,
      workload: .[0].workload,
      status: (if any(.[]; .status != "ok") then "partial-error" else "ok" end),
      score: (([.[] | select(.status == "ok") | .score] | add // 0) / (([.[] | select(.status == "ok")] | length) | if . == 0 then 1 else . end)),
      queue_depths_ok: ([.[] | select(.status == "ok") | .queue_depth]),
      errors: ([.[] | select(.status != "ok") | .error] | join("; "))
    })
  | group_by(.workload)
  | map({
      key: .[0].workload,
      value: (
        sort_by(-.score)
        | to_entries
        | map(.value + {rank: (.key + 1)})
      )
    })
  | from_entries;

def overall_ranking($workload_weights; $workload_scores):
  ($workload_scores | map(.storageclass) | unique) as $scs |
  ($workload_weights | to_entries | map(select(.value > 0))) as $weights |
  $scs
  | map(. as $sc |
      ($workload_scores | map(select(.storageclass == $sc and .status == "ok"))) as $rows |
      ($weights | map(select(.key as $w | $rows | any(.workload == $w)))) as $applicable |
      ($applicable | map(.value) | add // 0) as $wsum |
      {
        storageclass: $sc,
        score: (
          if $wsum == 0 then 0 else
            ($applicable | map(
              . as $w |
              ($rows | map(select(.workload == $w.key)) | .[0].score) as $s |
              $s * $w.value
            ) | add) / $wsum
          end
        ),
        workloads_included: ($applicable | map(.key)),
        workloads_missing: (($weights | map(.key)) - ($applicable | map(.key)))
      }
    )
  | sort_by(-.score)
  | to_entries
  | map(.value + {rank: (.key + 1)});

. as $input |
($input.results // []) as $all |
($all | map(select(.status != "ok"))) as $raw_errors |
($all | merge_phases) as $merged |
($merged | merge_repeats) as $per_qd |
($per_qd | score_rows($input.metric_weights)) as $scored |
($scored | workload_rankings) as $wr |
($wr | [to_entries[] | .value[]]) as $flat_workload_scores |
{
  generated_at: (now | todate),
  workloads: $wr,
  overall: overall_ranking($input.workload_weights; $flat_workload_scores),
  errors: $raw_errors
}
