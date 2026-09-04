# storage-perf-tester

A self-contained, configurable Kubernetes StorageClass performance tester.
It discovers the StorageClasses available in your cluster (or a subset you
name explicitly), spins up disposable PVCs and Jobs per StorageClass, runs
a matrix of I/O workloads against them with `fio` and a PostgreSQL/`pgbench`
benchmark, collects IOPS/throughput/latency, and produces a transparent,
configurably-weighted ranking - both per workload and overall.

This tool is fully self-contained in this directory. It never touches the
OSM stack, its namespaces, or any other resource elsewhere in this
repository; it creates and manages its own namespace (`storage-perf-test`
by default) and cleans up after itself.

## Table of contents

- [Prerequisites](#prerequisites)
- [Safety and cost notes](#safety-and-cost-notes)
- [Quick start](#quick-start)
- [Usage](#usage)
- [Control menu (`spt-menu.sh`)](#control-menu-spt-menush)
- [Configuration](#configuration)
- [Workloads](#workloads)
- [Results and ranking](#results-and-ranking)
- [Interpreting results](#interpreting-results)
- [Troubleshooting / error handling](#troubleshooting--error-handling)
- [Layout](#layout)

## Prerequisites

On the machine you run the tool from (NOT inside the cluster):

- `bash` >= 4.3 (uses `wait -n`)
- `kubectl`, configured with a context that can create Namespaces,
  PersistentVolumeClaims, Jobs and ConfigMaps (and read StorageClasses)
- `jq` (result parsing/ranking)
- `envsubst` (part of GNU gettext; used for manifest templating)
- `flock` (optional; a portable `mkdir`-based lock is used if missing)

Inside the cluster:

- Outbound network access from the nodes that run the fio worker Jobs, so
  `apk add fio jq` can install packages inside the (otherwise plain)
  `alpine:3.20` image at container start. If your cluster has no outbound
  access, build `images/fio.Dockerfile` / `images/postgres.Dockerfile`
  once, push them to a registry your cluster can reach, and pass
  `--fio-image` / `--postgres-image` (or set `FIO_IMAGE`/`POSTGRES_IMAGE`).
- At least one StorageClass with dynamic provisioning.

## Safety and cost notes

- **Resources are dedicated and disposable.** Everything the tool creates
  lives in its own namespace and is labeled
  `app.kubernetes.io/managed-by=storage-perf-tester`; nothing outside that
  label selector and namespace is ever touched, so existing OSM (or any
  other) workloads are never affected.
- **Storage and compute cost.** Every (StorageClass × workload × queue
  depth × repeat) combination creates its own PVC (`--pvc-size`, default
  `5Gi`) and a Job with its own CPU/memory request/limit (defaults:
  `250m`/`2` CPU, `256Mi`/`1Gi` memory). With the shipped defaults (7
  workloads × 3 queue depths × 1 repeat = 21 cases per StorageClass) a run
  against N StorageClasses will transiently provision up to `21 * N * 5Gi`
  of storage and run up to `MAX_PARALLEL` Jobs concurrently. Raise
  `--pvc-size`, `--file-size`, `--duration`, `--queue-depths` or
  `--repeats` deliberately, and only after checking cluster headroom.
  Real parallel PVC/Job tests can stress node CPU/RAM/IO and starve other
  workloads - `--max-parallel` (default `1`) exists specifically to bound
  this; raise it only if the cluster has capacity to spare.
- **`postgres-write-fast` is intentionally not crash-safe** (`fsync=off`,
  `synchronous_commit=off`). It only ever runs against the throwaway PVCs
  this tool creates for the duration of a single Job, never against real
  data, so this is safe here, but do not reuse the container/config for
  anything else.
- **No secrets or cluster credentials are stored or committed.** The
  Postgres worker uses local, trust-authenticated, ephemeral instances
  (`--auth=trust`) that only exist inside a single Job's Pod and are
  destroyed together with the PVC.
- **The default configuration is conservative** for a shared/test cluster
  (small PVCs, short durations, `--max-parallel 1`). Review and raise the
  relevant `--pvc-size`/`--duration`/`--file-size`/`--queue-depths` values
  before running a "serious" benchmark.

## Quick start

```bash
cd storage-perf-tester

# 1. See which StorageClasses would be tested.
./bin/storage-perf-tester.sh discover

# 2. Dry-run: render every manifest that "run" would apply, without
#    touching the cluster.
./bin/storage-perf-tester.sh plan

# 3. Run a small, fast smoke test (single queue depth, single repeat,
#    only the fio-based workloads) and keep resources afterwards for
#    inspection.
./bin/storage-perf-tester.sh run \
  --workloads large-files,mixed,small-files \
  --queue-depths 1 --repeats 1 --no-cleanup

# 4. Full run with the shipped conservative defaults (all workloads,
#    queue depths 1/4/8, cleans up automatically afterwards):
./bin/storage-perf-tester.sh run

# 5. Recompute the JSON/CSV/ranking report for a previous run without
#    touching the cluster (e.g. after editing weights):
./bin/storage-perf-tester.sh rank --output-dir ./results/<run-id>

# 6. Remove everything the tool created for a given run (or all of it).
./bin/storage-perf-tester.sh cleanup --run-id <run-id>
./bin/storage-perf-tester.sh cleanup   # removes everything in the namespace
```

## Usage

```
storage-perf-tester.sh <command> [options]

Commands:
  discover   Detect and print the StorageClasses that would be tested.
  plan       Render every Kubernetes manifest that "run" would apply,
             without contacting the cluster (dry-run).
  run        Discover StorageClasses, create test resources, execute all
             workloads (bounded by --max-parallel), collect results, write
             JSON/CSV and a ranking report. Cleans up afterwards unless
             --no-cleanup is given.
  rank       Recompute results.json + ranking.json for an existing
             --output-dir without touching the cluster.
  cleanup    Delete storage-perf-tester resources from the cluster.
```

Run `./bin/storage-perf-tester.sh --help` for the full option reference.

## Control menu (`spt-menu.sh`)

For interactive use, `bin/spt-menu.sh` provides a `dialog`/`whiptail`
menu on top of `bin/storage-perf-tester.sh`. It never re-implements any
discovery/testing/ranking logic itself - every menu action just invokes
`bin/storage-perf-tester.sh` (either synchronously, or in the background
for `run`), so there is exactly one place that owns the Kubernetes/testing
logic and the CLI documented above always works standalone.

Requires `dialog` (preferred) or `whiptail`, plus the same prerequisites
as the CLI (`kubectl`, `jq`, `bash`; `fio`/`psql` only matter inside the
worker images, not on the machine running the menu).

```bash
cd storage-perf-tester
./bin/spt-menu.sh
# or: bash bin/spt-menu.sh --namespace my-ns --output-dir ./results
```

Menu actions:

| Action | What it does |
|---|---|
| Configure run | Interactively set namespace, StorageClass selection/exclusion, workloads, PVC size, duration/file size, queue depths, repeats, `--max-parallel`, cleanup behavior, and output directory. Pre-filled from `config/default.conf`. |
| Start a performance run | Launches `storage-perf-tester.sh run` in the background with the configured options and a fresh `--run-id`, so the menu stays responsive. Only one tracked run at a time; shows an estimated case count before confirming. |
| Stop the tracked run | Sends a stop signal to the background run process. Does **not** delete already-created cluster resources - use Cleanup afterwards. |
| Status dashboard | Live progress gauge for the tracked run: completed vs. estimated total test cases (read from `results.json`), a `kubectl get jobs -l spt-run-id=<id>` summary, and the last log lines. Auto-closes once the run finishes. |
| Tail the tracked run's raw log output | Live `tail -f`-style view of the background run's full stdout/stderr. |
| Kubernetes overview | Read-only snapshot of Jobs/Pods/PVCs in the target namespace. |
| Performance dashboard | Browse any previous (or the current) run's results: overall ranking, per-workload ranking, raw per-case IOPS/throughput/latency/errors, or the path to the CSV export. |
| Discover StorageClasses | Runs `storage-perf-tester.sh discover` and shows the output. |
| Plan (dry-run manifest render) | Runs `storage-perf-tester.sh plan` and shows the rendered manifests, without touching the cluster. |
| Cleanup cluster resources | Runs `storage-perf-tester.sh cleanup`, scoped to the tracked run, a specific `--run-id`, or the whole namespace. Always asks for confirmation first. |

The menu keeps a small state file under `results/.menu-state/` to track
the currently running background job (run-id, PID, log file path) so the
status dashboard keeps working even if you close and reopen the menu
while a run is still in progress. It is git-ignored along with the rest
of `results/`.

## Configuration

Defaults live in [`config/default.conf`](config/default.conf) (every value
documented inline). Precedence, highest wins:

1. CLI flags (e.g. `--pvc-size 20Gi`)
2. `--config <file>` (a second KEY=VALUE file sourced after the defaults)
3. Environment variables exported before invoking the script (e.g.
   `NAMESPACE=my-ns ./bin/storage-perf-tester.sh run`)
4. Built-in defaults in `config/default.conf`

Key configurable knobs:

| Variable / flag | Meaning | Default |
|---|---|---|
| `NAMESPACE` / `--namespace` | Namespace created/used for all test resources | `storage-perf-test` |
| `STORAGECLASSES` / `--storageclasses` | Comma list to restrict testing to; empty = auto-discover all | (empty) |
| `EXCLUDE_STORAGECLASSES` / `--exclude-storageclasses` | Comma list to always skip | (empty) |
| `PVC_SIZE` / `--pvc-size` | PVC size per test case | `5Gi` |
| `DURATION` / `--duration` | Seconds per fio/pgbench measurement phase | `30` |
| `FILE_SIZE` / `--file-size` | File size for large-files/mixed workloads | `1Gi` |
| `QUEUE_DEPTHS` / `--queue-depths` | Comma list of thread/client counts tested | `1,4,8` |
| `REPEATS` / `--repeats` | Repetitions per (workload, queue depth) | `1` |
| `MAX_PARALLEL` / `--max-parallel` | Concurrent StorageClass test pipelines | `1` |
| `WORKLOADS` / `--workloads` | Subset of workload ids to run; empty = all | (empty = all) |
| `CLEANUP` / `--cleanup`/`--no-cleanup` | Delete resources automatically after collecting results | `true` |
| `OUTPUT_DIR` / `--output-dir` | Base directory for `<run-id>/{results.json,results.csv,ranking.json}` | `./results` |
| `OUTPUT_FORMAT` / `--output-format` | `json`, `csv` or `both` | `both` |
| `PVC_TIMEOUT` / `--pvc-timeout` | Seconds to wait for PVC Bound (Immediate binding mode only) | `300` |
| `JOB_TIMEOUT` / `--job-timeout` | Seconds to wait for a Job to finish | `900` |
| `FIO_IMAGE` / `--fio-image` | fio worker image | `alpine:3.20` |
| `POSTGRES_IMAGE` / `--postgres-image` | Postgres worker image | `postgres:16-alpine` |
| `CPU_REQUEST`/`CPU_LIMIT`/`MEMORY_REQUEST`/`MEMORY_LIMIT` | Worker Job resources | `250m`/`2`/`256Mi`/`1Gi` |
| `WEIGHTS_FILE` / `--weights-file` | Ranking weights file | `config/weights.default.conf` |

Ranking weights (how much each workload and each metric contributes to the
scores) live in [`config/weights.default.conf`](config/weights.default.conf).
Copy it, adjust it (e.g. weigh `postgres_write_durable`/`postgres_read`
higher to reflect an OSM/Nominatim-heavy cluster) and pass
`--weights-file your-weights.conf`.

### `WaitForFirstConsumer`, RWO and single-node PVCs

Many CSI drivers use `volumeBindingMode: WaitForFirstConsumer`, meaning a
PVC only binds once a Pod that uses it is scheduled - the tool detects
this per StorageClass and does not treat a still-`Pending` PVC as an error
in that case; it waits for the Job's Pod (bounded by `--job-timeout`)
instead. PVCs are always `ReadWriteOnce`, so a given test case's PVC (and
thus its Pod) can only ever run on a single node at a time - this is
expected and does not prevent testing multiple StorageClasses or queue
depths, since each test case gets its own PVC/Pod pair.

## Workloads

| id | Engine | What it measures |
|---|---|---|
| `postgres-write-durable` | pgbench (TPC-B-like) | Read/write PostgreSQL transactions with `fsync=on`, `synchronous_commit=on` (safe, production-like durability) |
| `postgres-write-fast` | pgbench | Same, but `fsync=off` (throughput ceiling without sync overhead; **not** crash-safe, disposable PVC only) |
| `postgres-read` | pgbench `--select-only` | Read-only PostgreSQL query throughput/latency |
| `small-files` | fio | Many small files (4k-64k) created/read/deleted concurrently (`nrfiles`, `file_service_type=random`, `unlink=1`) |
| `large-files` | fio | Sequential write then sequential read of large files (`--file-size`, default `1Gi`) |
| `mixed` | fio | 70/30 random read/write mix at 4k block size |
| `jenkins-checkout` | shell (create/stat/copy/rename/delete) | Many-small-files + metadata-heavy workload approximating a Jenkins workspace / git checkout |

fio's `libaio`/`io_uring` engines require O_DIRECT and kernel AIO support
that is not guaranteed on every CSI/network filesystem, so all fio
workloads use the portable `psync` engine and express "queue depth" /
"threads" as the number of parallel fio jobs (`numjobs`), which works
identically across every StorageClass/filesystem.

### About the PostgreSQL/Nominatim approximation

A full Nominatim import can run for many hours and is far too slow/costly
to repeat once per StorageClass × queue depth × repeat combination. The
`postgres-*` workloads instead run pgbench's standard TPC-B-like schema/
transaction mix inside a throwaway, single-purpose PostgreSQL instance
whose data directory lives on the PVC under test:

- `postgres-write-durable`/`postgres-write-fast` exercise the same
  fundamentals a Nominatim `osm2pgsql` import stresses: many small,
  indexed `UPDATE`/`INSERT` transactions with WAL + fsync activity.
- `postgres-read` exercises indexed point/range `SELECT`s, similar in
  spirit to Nominatim/geocoding query patterns.

This is a **reproducible synthetic approximation**, not a full Nominatim
import; it is a good relative comparison between StorageClasses for
"how does this storage behave under many small transactional PostgreSQL
writes/reads", but do not treat its absolute TPS numbers as a prediction
of real Nominatim import duration.

## Results and ranking

Each `run`/`rank` invocation writes, under `<output-dir>/<run-id>/`:

- `results.json` - flat array of every collected measurement (one entry
  per storageclass/workload/queue_depth/repeat/phase) with `iops_read`,
  `iops_write`, `throughput_mb_read`, `throughput_mb_write`,
  `latency_ms_avg`, `latency_ms_p95`, `latency_ms_p99`, and `status`/
  `error` for failed cases.
- `results.csv` - the same data flattened to CSV (when `--output-format`
  is `csv` or `both`).
- `ranking.json` - the computed ranking (see below), plus the list of raw
  error entries.

### How the ranking is computed

1. Phases that belong together (e.g. `large-files`' write and read phase)
   are merged: IOPS/throughput are summed, latency is averaged.
2. Repeats of the same (storageclass, workload, queue depth) are averaged.
3. **Per (workload, queue depth)**, IOPS, throughput and latency are each
   min-max normalized *across StorageClasses* into a 0-1 range (so e.g.
   MB/s from a large-file test is never mixed with IOPS from a small-file
   test - normalization always happens within one metric/workload/queue
   depth group first). Latency is inverted after normalization (lower
   latency -> higher normalized score) so all three sub-scores share the
   same "higher is better" direction before being combined using
   `METRIC_WEIGHT_IOPS`/`METRIC_WEIGHT_THROUGHPUT`/`METRIC_WEIGHT_LATENCY`.
4. The resulting per-queue-depth scores are averaged into one score per
   (storageclass, workload) - this is the **per-workload ranking**.
5. The overall ranking is a weighted average of the per-workload scores
   using `WORKLOAD_WEIGHT_*` from the weights file, normalized by the sum
   of the weights of the workloads that actually produced usable data for
   that StorageClass (so a StorageClass missing one workload's data is
   still ranked fairly on the workloads it does have).

Because everything is normalized to unitless 0-1 sub-scores before being
combined, the resulting score is a relative ranking aid, not an absolute
physical unit - always look at `results.json`/`results.csv` for the raw
IOPS/MB/s/latency numbers.

## Interpreting results

- A StorageClass ranked #1 overall performed comparatively best across the
  workloads you weighted; check `workloads_included`/`workloads_missing`
  in `ranking.json` to see whether that ranking is based on complete data.
- Look at the per-workload ranking (`ranking.json` -> `.workloads.<id>`)
  to see which StorageClass wins for the workload category you care about
  most (e.g. `postgres_write_durable` for an OSM/Nominatim-heavy cluster).
- `status: "partial-error"` or `"error"` entries mean at least one
  repeat/queue-depth failed for that (storageclass, workload); check
  `errors`/`results.json`'s `error` field for the failure reason before
  trusting a low score.
- Raw `results.csv`/`results.json` always contain the real IOPS/MB/s/
  latency numbers if you want to compare absolute performance rather than
  the normalized ranking.

## Troubleshooting / error handling

- **Missing `kubectl`/`jq`/`envsubst`**: the tool checks for required
  commands up front and exits with a clear `[ERROR]` message naming the
  missing command.
- **No StorageClasses found**: `discover`/`plan`/`run` fail fast with an
  explicit error if the cluster reports no StorageClasses and none were
  given via `--storageclasses`.
- **PVC stuck Pending**: for `Immediate` binding mode StorageClasses, the
  tool waits up to `--pvc-timeout` seconds and reports `PVCTimeout` for
  that case (skipping the Job wait) instead of hanging until
  `--job-timeout`. For `WaitForFirstConsumer` StorageClasses this is
  expected until the Job's Pod is scheduled and is not treated as an error
  by itself.
- **Job timeout / failure**: recorded as `status: "error"` with a
  descriptive `error` message in `results.json`/`ranking.json`'s `errors`
  array; the rest of the matrix continues to run.
- **Unsupported/unknown workload id**: rejected immediately by the worker
  script with a clear error result rather than hanging.
- **Incomplete results**: if a Job produced no parsable result (crashed
  before printing one), a synthetic `error` entry is recorded so the gap
  is visible instead of silently missing from the report.

## Layout

| Path | Purpose |
|---|---|
| `bin/storage-perf-tester.sh` | Main entry point (all commands) |
| `bin/spt-menu.sh` | Optional `dialog`/`whiptail` control menu wrapping the CLI |
| `lib/common.sh` | Logging, config parsing, locking helpers |
| `lib/k8s.sh` | kubectl wrappers: discovery, templating, wait/collect helpers |
| `lib/collect.sh` | Extracts worker Job results from Pod logs |
| `lib/rank.sh` / `lib/rank.jq` | Builds the ranking input and the jq ranking program |
| `templates/*.yaml.tpl` | Namespace/PVC/Job manifest templates (rendered with `envsubst`) |
| `scripts-embedded/entrypoint-fio.sh` | fio/synthetic worker entrypoint (mounted via ConfigMap) |
| `scripts-embedded/entrypoint-postgres.sh` | pgbench worker entrypoint (mounted via ConfigMap) |
| `config/default.conf` | All configurable defaults, documented inline |
| `config/weights.default.conf` | Ranking weights, documented inline |
| `images/*.Dockerfile` | Optional offline/pinned worker images for air-gapped clusters |
| `results/` | Default output directory (git-ignored) |
