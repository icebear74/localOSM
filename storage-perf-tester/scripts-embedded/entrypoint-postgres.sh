#!/usr/bin/env bash
# entrypoint-postgres.sh - runs a pgbench-based PostgreSQL workload against a
# throwaway PostgreSQL instance whose data directory lives on the tested
# PVC, and prints the result as a single JSON object wrapped in
# machine-parsable markers on stdout.
#
# Required environment variables (set by the Job template):
#   WORKLOAD     - one of: postgres-write-durable, postgres-write-fast,
#                  postgres-read
#   QUEUE_DEPTH  - pgbench -c/-j (clients / worker threads)
#   DURATION     - seconds; pgbench -T runtime
#   REPEAT       - repeat index (1-based), used only for logging
#   MOUNT_PATH   - directory on the PVC to use as PGDATA parent
#   PGBENCH_SCALE - pgbench -s (defaults to a size approximating a
#                  Nominatim-like write-heavy workload; see README)
#
# This is a reproducible synthetic approximation of a Nominatim-style
# import/query load, NOT a full Nominatim import (which is far too slow
# and expensive to run once per StorageClass/queue-depth/repeat
# combination). pgbench's default TPC-B-like schema exercises the same
# fundamentals a Nominatim import stresses: many small indexed
# UPDATE/INSERT transactions with WAL + fsync activity ("write") and mixed
# point/range SELECTs against indexed tables ("read"). See README.md for
# the caveats of this approximation.
#
# The container starts as root (the postgres image's default) so this
# script can fix ownership of the mounted PVC; every actual PostgreSQL
# command is then executed as the unprivileged "postgres" OS user via
# su-exec (the same helper the upstream postgres image uses internally),
# never as root.

set -Eeuo pipefail

WORKLOAD="${WORKLOAD:?WORKLOAD is required}"
QUEUE_DEPTH="${QUEUE_DEPTH:-1}"
DURATION="${DURATION:-30}"
REPEAT="${REPEAT:-1}"
MOUNT_PATH="${MOUNT_PATH:-/data}"
PGBENCH_SCALE="${PGBENCH_SCALE:-25}"

PGDATA_DIR="${MOUNT_PATH}/pgdata-${WORKLOAD}"
PGHOST_DIR="${PGDATA_DIR}/socket"
PGDATABASE="pgbench"

# JSON is built with plain printf rather than jq (avoids adding another
# package-install/root step). WORKLOAD is restricted to a fixed set of
# known values by the Job template and $msg only ever contains fixed
# literal strings below, so minimal escaping (backslashes/quotes) suffices.
json_escape() {
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

emit_result() {
  printf '===SPT-RESULT-BEGIN===\n%s\n===SPT-RESULT-END===\n' "$1"
}

fail_result() {
  local msg="$1"
  local json
  json=$(printf '{"workload":"%s","queue_depth":%s,"repeat":%s,"engine":"pgbench","status":"error","error":"%s"}' \
    "$(json_escape "$WORKLOAD")" "$QUEUE_DEPTH" "$REPEAT" "$(json_escape "$msg")")
  emit_result "$json"
}

pg() {
  # Run a postgres binary as the unprivileged postgres user.
  su-exec postgres "$@"
}

cleanup_pg() {
  pg pg_ctl -D "${PGDATA_DIR}" -m fast stop >&2 2>/dev/null || true
}
trap cleanup_pg EXIT

rm -rf "${PGDATA_DIR}"
mkdir -p "${PGHOST_DIR}"
chown -R postgres:postgres "${PGDATA_DIR}"

echo "[entrypoint-postgres] initdb ..." >&2
if ! pg initdb -D "${PGDATA_DIR}" --auth=trust >&2; then
  fail_result "initdb failed"; exit 0
fi

# fsync/synchronous_commit control the durability profile under test:
#   postgres-write-durable -> fsync=on, synchronous_commit=on (safe default,
#     what a production Nominatim/PostGIS instance would run with)
#   postgres-write-fast    -> fsync=off (NOT crash safe; only ever used
#     against throwaway, disposable PVCs created by this tool, never
#     against real data - see README safety notes)
#   postgres-read          -> durability of the read-only phase is
#     irrelevant, keep fsync on (default)
case "${WORKLOAD}" in
  postgres-write-fast)
    { echo "fsync = off"; echo "synchronous_commit = off"; } >> "${PGDATA_DIR}/postgresql.conf"
    ;;
  *)
    { echo "fsync = on"; echo "synchronous_commit = on"; } >> "${PGDATA_DIR}/postgresql.conf"
    ;;
esac
{
  echo "unix_socket_directories = '${PGHOST_DIR}'"
  echo "listen_addresses = ''"
  echo "max_connections = $(( QUEUE_DEPTH + 10 ))"
} >> "${PGDATA_DIR}/postgresql.conf"
chown postgres:postgres "${PGDATA_DIR}/postgresql.conf"

echo "[entrypoint-postgres] starting postgres ..." >&2
if ! pg pg_ctl -D "${PGDATA_DIR}" -l "${MOUNT_PATH}/postgres-${WORKLOAD}.log" -w start >&2; then
  cat "${MOUNT_PATH}/postgres-${WORKLOAD}.log" >&2 || true
  fail_result "postgres failed to start"; exit 0
fi

if ! pg psql -h "${PGHOST_DIR}" -U postgres -c "CREATE DATABASE ${PGDATABASE};" >&2; then
  fail_result "createdb failed"; exit 0
fi

echo "[entrypoint-postgres] pgbench -i -s ${PGBENCH_SCALE} ..." >&2
if ! pg pgbench -h "${PGHOST_DIR}" -U postgres -i -s "${PGBENCH_SCALE}" --quiet "${PGDATABASE}" >&2; then
  fail_result "pgbench init failed"; exit 0
fi

run_pgbench() {
  # $@ = extra pgbench flags
  pg pgbench -h "${PGHOST_DIR}" -U postgres -c "${QUEUE_DEPTH}" -j "${QUEUE_DEPTH}" \
    -T "${DURATION}" --progress=5 "$@" "${PGDATABASE}"
}

PGLOG="${MOUNT_PATH}/pgbench-${WORKLOAD}.out"
case "${WORKLOAD}" in
  postgres-read)
    echo "[entrypoint-postgres] pgbench read-only ..." >&2
    if ! run_pgbench --select-only >"${PGLOG}" 2>&1; then
      fail_result "pgbench read-only run failed"; exit 0
    fi
    ;;
  postgres-write-durable|postgres-write-fast)
    echo "[entrypoint-postgres] pgbench read-write ..." >&2
    if ! run_pgbench >"${PGLOG}" 2>&1; then
      fail_result "pgbench read-write run failed"; exit 0
    fi
    ;;
  *)
    fail_result "unknown WORKLOAD '${WORKLOAD}'"; exit 0
    ;;
esac

cat "${PGLOG}" >&2

tps=$(grep -E '^tps = ' "${PGLOG}" | tail -n1 | sed -E 's/tps = ([0-9.]+).*/\1/' || echo 0)
lat_avg=$(grep -E '^latency average' "${PGLOG}" | tail -n1 | sed -E 's/[^0-9.]*([0-9.]+).*/\1/' || echo 0)
[ -z "${tps}" ] && tps=0
[ -z "${lat_avg}" ] && lat_avg=0

if [ "${WORKLOAD}" = "postgres-read" ]; then
  iops_read="${tps}"; iops_write=0
else
  iops_read=0; iops_write="${tps}"
fi

json=$(printf '{"workload":"%s","queue_depth":%s,"repeat":%s,"engine":"pgbench","phase":"%s","status":"ok","metrics":{"iops_read":%s,"iops_write":%s,"throughput_mb_read":0,"throughput_mb_write":0,"latency_ms_avg":%s,"latency_ms_p95":%s,"latency_ms_p99":%s}}' \
  "$(json_escape "$WORKLOAD")" "$QUEUE_DEPTH" "$REPEAT" "$(json_escape "$WORKLOAD")" \
  "${iops_read}" "${iops_write}" "${lat_avg}" "${lat_avg}" "${lat_avg}")
emit_result "${json}"
