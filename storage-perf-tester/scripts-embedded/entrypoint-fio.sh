#!/bin/sh
# entrypoint-fio.sh - runs one fio-based (or synthetic file-system) workload
# inside the worker Job pod and prints the result as a single JSON object
# wrapped in machine-parsable markers on stdout.
#
# Required environment variables (set by the Job template):
#   WORKLOAD     - one of: large-files, mixed, small-files, jenkins-checkout
#   QUEUE_DEPTH  - parallelism (fio numjobs / parallel worker processes)
#   DURATION     - seconds; time-based fio phases run for this long
#   FILE_SIZE    - size per fio job for large-files/mixed (e.g. 1Gi)
#   REPEAT       - repeat index (1-based), used only for logging
#   MOUNT_PATH   - directory on the PVC to use as working directory
#
# Portability note: fio's io_uring/libaio engines require O_DIRECT and
# kernel AIO support that is not guaranteed on every CSI/network
# filesystem. To stay portable across all StorageClasses this script uses
# fio's default synchronous engine (psync) and expresses "queue depth" as
# the number of parallel fio jobs (numjobs), not iodepth.

set -eu

echo "[entrypoint-fio] installing fio + jq ..." >&2
apk add --no-cache fio jq coreutils >/dev/null

WORKLOAD="${WORKLOAD:?WORKLOAD is required}"
QUEUE_DEPTH="${QUEUE_DEPTH:-1}"
DURATION="${DURATION:-30}"
FILE_SIZE="${FILE_SIZE:-1Gi}"
REPEAT="${REPEAT:-1}"
MOUNT_PATH="${MOUNT_PATH:-/data}"
SMALL_FILES_COUNT="${SMALL_FILES_COUNT:-2000}"

WORKDIR="${MOUNT_PATH}/spt-${WORKLOAD}"
mkdir -p "${WORKDIR}"

emit_result() {
  # $1 = compact JSON object (already built by caller)
  printf '===SPT-RESULT-BEGIN===\n%s\n===SPT-RESULT-END===\n' "$1"
}

fail_result() {
  # $1 = human readable error message
  msg="$1"
  json=$(jq -nc --arg workload "$WORKLOAD" --arg qd "$QUEUE_DEPTH" --arg repeat "$REPEAT" \
    --arg err "$msg" \
    '{workload:$workload, queue_depth:($qd|tonumber), repeat:($repeat|tonumber), engine:"fio", status:"error", error:$err}')
  emit_result "$json"
}

# Convert a fio JSON report (single job, group_reporting) into our
# normalized metrics envelope.
normalize_fio() {
  # $1 = path to fio json output, $2 = phase label (e.g. write/read/mixed)
  fio_json="$1"
  jq -c --arg workload "$WORKLOAD" --arg qd "$QUEUE_DEPTH" --arg repeat "$REPEAT" --arg phase "$2" '
    (.jobs[0]) as $j |
    {
      workload: $workload,
      queue_depth: ($qd|tonumber),
      repeat: ($repeat|tonumber),
      engine: "fio",
      phase: $phase,
      status: "ok",
      metrics: {
        iops_read: ($j.read.iops // 0),
        iops_write: ($j.write.iops // 0),
        throughput_mb_read: (($j.read.bw // 0) / 1024),
        throughput_mb_write: (($j.write.bw // 0) / 1024),
        latency_ms_avg: (
          (([$j.read.clat_ns.mean // 0, $j.write.clat_ns.mean // 0] | add) / 1000000)
        ),
        latency_ms_p95: (
          ([($j.read.clat_ns.percentile["95.000000"] // 0), ($j.write.clat_ns.percentile["95.000000"] // 0)] | max) / 1000000
        ),
        latency_ms_p99: (
          ([($j.read.clat_ns.percentile["99.000000"] // 0), ($j.write.clat_ns.percentile["99.000000"] // 0)] | max) / 1000000
        )
      }
    }
  ' "$fio_json"
}

run_fio() {
  # $1 = name, remaining args = extra fio flags, phase label passed via $FIO_PHASE
  name="$1"; shift
  out="${WORKDIR}/${name}.json"
  if ! fio --name="${name}" --directory="${WORKDIR}" --group_reporting \
       --output-format=json --output="${out}" "$@" >&2; then
    return 1
  fi
  normalize_fio "${out}" "${name}"
}

case "${WORKLOAD}" in
  large-files)
    if ! WOUT=$(run_fio large-write --rw=write --bs=1m --size="${FILE_SIZE}" \
        --numjobs="${QUEUE_DEPTH}" --ioengine=psync --fallocate=none \
        --filename_format='large.$jobnum.$filenum'); then
      fail_result "fio sequential write phase failed"; exit 0
    fi
    if ! ROUT=$(run_fio large-read --rw=read --bs=1m --size="${FILE_SIZE}" \
        --numjobs="${QUEUE_DEPTH}" --ioengine=psync \
        --filename_format='large.$jobnum.$filenum'); then
      fail_result "fio sequential read phase failed"; exit 0
    fi
    emit_result "$WOUT"
    emit_result "$ROUT"
    ;;

  mixed)
    if ! OUT=$(run_fio mixed --rw=randrw --rwmixread=70 --bs=4k \
        --size="${FILE_SIZE}" --numjobs="${QUEUE_DEPTH}" --ioengine=psync \
        --time_based=1 --runtime="${DURATION}"); then
      fail_result "fio mixed random/sequential phase failed"; exit 0
    fi
    emit_result "$OUT"
    ;;

  small-files)
    if ! OUT=$(run_fio smallfiles --rw=randrw --rwmixread=50 --bs=4k \
        --file_service_type=random --nrfiles="${SMALL_FILES_COUNT}" \
        --filesize=4k-64k --numjobs="${QUEUE_DEPTH}" --ioengine=psync \
        --time_based=1 --runtime="${DURATION}" --create_on_open=1 --unlink=1); then
      fail_result "fio small-files phase failed"; exit 0
    fi
    emit_result "$OUT"
    ;;

  jenkins-checkout)
    # Synthetic "many files + metadata operations" workload approximating a
    # Jenkins workspace / git checkout: create a nested tree of small text
    # files, stat everything, copy the tree (second checkout), rename it
    # (branch switch) and finally remove both trees. All phases are timed
    # and reported as ops/sec.
    total="${SMALL_FILES_COUNT}"
    src="${WORKDIR}/checkout-src"
    dst="${WORKDIR}/checkout-copy"
    rm -rf "${src}" "${dst}"
    mkdir -p "${src}"

    # Timestamps use whole seconds (busybox `date` has no sub-second
    # resolution). With SMALL_FILES_COUNT in the thousands each phase runs
    # for several seconds, so integer precision is acceptable here.
    t0=$(date +%s)
    i=1
    while [ "$i" -le "$total" ]; do echo "$i"; i=$((i + 1)); done | \
      xargs -P "${QUEUE_DEPTH}" -n 1 sh -c '
        i="$0"; d="'"${src}"'/dir-$(( i % 20 ))"
        mkdir -p "$d"
        dd if=/dev/urandom of="$d/file-$i.txt" bs=1024 count="$(( (i % 16) + 1 ))" status=none
      '
    t1=$(date +%s)

    find "${src}" -type f -print0 | xargs -0 -P "${QUEUE_DEPTH}" -n 32 stat >/dev/null
    t2=$(date +%s)

    cp -r "${src}" "${dst}"
    t3=$(date +%s)

    mv "${dst}" "${dst}-renamed"
    t4=$(date +%s)

    rm -rf "${src}" "${dst}-renamed"
    t5=$(date +%s)

    json=$(jq -nc --arg workload "$WORKLOAD" --arg qd "$QUEUE_DEPTH" --arg repeat "$REPEAT" \
      --arg total "$total" \
      --argjson create_s "$(( t1 - t0 ))" \
      --argjson stat_s "$(( t2 - t1 ))" \
      --argjson copy_s "$(( t3 - t2 ))" \
      --argjson rename_s "$(( t4 - t3 ))" \
      --argjson delete_s "$(( t5 - t4 ))" \
      '
      ($total|tonumber) as $n |
      $create_s as $create | $stat_s as $stat |
      $copy_s as $copy | $rename_s as $rename |
      $delete_s as $delete |
      {
        workload: $workload, queue_depth: ($qd|tonumber), repeat: ($repeat|tonumber),
        engine: "synthetic", phase: "jenkins-checkout", status: "ok",
        metrics: {
          iops_read: (if $stat > 0 then ($n / $stat) else 0 end),
          iops_write: (if ($create+$copy+$rename+$delete) > 0 then ($n / ($create+$copy+$rename+$delete)) else 0 end),
          throughput_mb_read: 0,
          throughput_mb_write: 0,
          latency_ms_avg: (if $n > 0 then (($create+$copy) * 1000 / $n) else 0 end),
          latency_ms_p95: (if $n > 0 then (($create+$copy) * 1000 / $n) else 0 end),
          latency_ms_p99: (if $n > 0 then (($create+$copy) * 1000 / $n) else 0 end)
        },
        phases_seconds: { create: $create, stat: $stat, copy: $copy, rename: $rename, delete: $delete }
      }')
    emit_result "$json"
    ;;

  *)
    fail_result "unknown WORKLOAD '${WORKLOAD}'"
    exit 0
    ;;
esac
