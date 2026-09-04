# localOSM

A self-hosted OSM stack on K3s with a read-only status dashboard, a routing web UI, and a sequential import orchestrator.

## Components

- Nominatim (with its bundled, configurably-tuned PostgreSQL/PostGIS)
- Valhalla
- TileServer GL
- Status dashboard
- Routing web UI
- Style-Editor (Maputnik)
- Import orchestrator pod

## Layout

| File | Purpose |
|---|---|
| `k8s/import-orchestrator.yaml` | Orchestrator deployment, RBAC, and config mounts |
| `k8s/nominatim-import-job.yaml` | Nominatim import job |
| `k8s/valhalla-import-job.yaml` | Valhalla import job |
| `k8s/planetiler-import-job.yaml` | TileServer/Planetiler import job on the Longhorn PVCs |
| `k8s/tileserver-import-profile-config.yaml` | Custom Planetiler profile plus the `use_java` toggle for enabling/disabling it during imports |
| `k8s/tileserver-gl-deployment.yaml` | TileServer-GL deployment and service using the `tileserver-gl` PVC |
| `k8s/osm-persistentvolumeclaims.yaml` | Longhorn PVC definitions for `planet`, shared temp PVC `osm-temp`, `tileserver-gl`, `nominatim`, `valhalla`, `photon`, and `osm-status` |
| `k8s/planetiler-rbac.yaml` | ServiceAccount/Role/RoleBinding for the Planetiler import job |
| `k8s/tileserver-init-assets-job.yaml` | One-shot job that copies static TileServer assets from the host bootstrap directory into the PVC |
| `k8s/status.yaml` | Read-only status dashboard |
| `k8s/web.yaml` | Browser routing UI |
| `k8s/style-editor.yaml` | Maputnik style editor (edits the live TileServer-GL style.json via the status dashboard API) |
| `scripts/deploy-osm.sh` | Installs manifests and stages static files on the host |
| `scripts/run-import.sh` | Downloads a `.osm.pbf` and creates an import request |
| `scripts/import-orchestrator.sh` | Sequential import workflow executed inside the orchestrator pod |
| `scripts/localosm-menu.sh` | `dialog`/`whiptail` TUI menu that drives deploy/build/promote/abort and shows live job progress (see below) |

## Host data

Host-side orchestration data still lives under `/mnt/OSM` by default (override
the scratch path with `--temp-dir` / `OSM_TEMP_DIR` when running
`scripts/deploy-osm.sh`). The deploy script keeps templated manifests, the
status dashboard state and the one-time TileServer bootstrap assets on the
host, while the heavy OSM datasets now live on Longhorn PVCs in Kubernetes.

Longhorn PVCs managed by `k8s/osm-persistentvolumeclaims.yaml`:

- `planet` (250Gi, RWO) – contains `/planet/europa.osm.pbf`
- `osm-temp` (700Gi, RWX) – shared import scratch/download volume (`import/` plus per-service work directories)
- `tileserver-gl` (150Gi, RWO) – live TileServer assets (`planet.mbtiles`, fonts, styles, sprites)
- `nominatim` (150Gi, RWX) – production Nominatim/Postgres data promoted by the Nominatim import job itself
- `valhalla` (100Gi, RWX) – active/next/previous Valhalla graph data promoted by the Valhalla import job
- `photon` (100Gi, RWX) – Photon release jar plus active/next/previous Photon index data
- `osm-status` (10Mi, RWX) – shared status/log files

Important host subdirectories under `/mnt/OSM`:

- `library/` – downloaded/cached `.osm.pbf` country extracts, reused across imports
- `manifests/` – static YAML copies used by the orchestrator pod
- `scripts/` – mounted orchestration script
- `status/` – dashboard and orchestrator state files
- `tileserver/bootstrap` – source directory for `tileserver-init-assets-job.yaml`

Important scratch paths under the shared Kubernetes temp PVC mounted inside the pods:

- `import/` – merged/downloaded `planet.osm.pbf` used as the shared input for Nominatim, Valhalla and Photon
- `tileserver/work` – Planetiler work directory, cleaned after the new `planet.mbtiles` has been activated
- `tileserver/downloads` – reusable Planetiler side downloads that may remain between runs
- `nominatim/work` – temporary Postgres/Nominatim import working data, cleaned after promotion into the `nominatim` PVC
- `valhalla/work` – temporary Valhalla graph build directory, cleaned after activation into the `valhalla` PVC
- `photon/work` – temporary Photon index build directory, cleaned after activation into the `photon` PVC

## Deploy

```bash
bash scripts/deploy-osm.sh
```

On a fresh cluster/PVC setup, run the one-shot asset bootstrap once before the
first TileServer import:

```bash
kubectl apply -f /mnt/OSM/manifests/tileserver-init-assets-job.yaml
```

## Import data

```bash
bash scripts/run-import.sh --url https://download.geofabrik.de/europe/germany/berlin-latest.osm.pbf
```

The script downloads the extract and writes an import request. The orchestrator pod processes requests strictly in sequence. Import jobs are intended to be autonomous: the orchestrator only submits them, while each job should manage its own deployment lifecycle (scale/rollout/restart) and data promotion steps internally.

## Control menu (`dialog`/`whiptail`)

`scripts/localosm-menu.sh` is a single, menu-driven entry point for the operations above, so you don't have to remember individual scripts/URLs:

```bash
bash scripts/localosm-menu.sh
```

It requires `dialog` (falls back to `whiptail` if `dialog` isn't installed), `curl`, `python3`, and `kubectl`. Install `dialog` if needed: `sudo apt-get install -y dialog`.

The menu never re-implements any orchestration logic itself: build/promote/abort actions call the status dashboard's existing HTTP API (the same one the web UI uses), so there is exactly one place that owns import-workflow state and locking. From the menu you can:

- Deploy/update the stack (wraps `scripts/deploy-osm.sh`, with prompts for `--clean`/`--preserve-downloads`/`--node-url`)
- Add or queue an extract, check for extract updates, remove a queued extract
- Start a full build, a single build step (tileserver/nominatim/valhalla/photon), or a download-and-merge-only run
- Promote staged data for a service, or abort the currently running import
- Watch **live progress** of the running import: polls `/api/status` into an auto-updating progress bar (phase, message, and a Kubernetes Jobs completions summary), and closes automatically once the workflow finishes
- View a raw Kubernetes overview (deployments/jobs/pods/PVCs in the `osm` namespace) and tail a pod's logs live
- Run cluster maintenance (`scripts/remove-rancher.sh`, `scripts/reset-k3s.sh`), each with an explicit confirmation prompt and a `--dry-run` preview option

By default it autodetects the status dashboard at `http://127.0.0.1:30083` (when run on a cluster node) or via the first node's InternalIP; override with `--url http://<node-ip>:30083` or `--namespace <ns>` if needed.

1. Nominatim
2. Valhalla
3. TileServer

Each step uses a dedicated Kubernetes Job. The shared `osm-temp` PVC keeps the reusable merged/downloaded inputs and per-service work directories, while every import job now copies/promotes its finished output into the active directory itself and then cleans its own work directory.

### Tuning the Nominatim import

The `osm2pgsql` step invoked by `nominatim import` maps its worker/process count 1:1 to the
`THREADS` value it is started with, and its node cache size (`--cache`, in MB) to
`NOMINATIM_OSM2PGSQL_CACHE`. `k8s/nominatim-import-config.yaml` (`ConfigMap
osm-nominatim-import-config`) is the single place to configure both:

- `import_threads` (default `8`) — match this to the CPU cores available to the
  `nominatim-import` Job (see its `resources.limits.cpu` in `k8s/nominatim-import-job.yaml`).
- `import_cache_mb` (default `12000`, i.e. ~12 GB) — match this to the RAM available to the
  `nominatim-import` Job (see its `resources.limits.memory`), up to roughly 75% of that RAM.

Edit the values, then re-apply the ConfigMap:

```bash
kubectl apply -f k8s/nominatim-import-config.yaml
```

before starting the next import. The same ConfigMap also supplies `import_password`, used by both
the import Job and the running `nominatim` deployment, so the two always stay in sync.

### Tuning the Photon search index

Photon imports can now be configured with additional, indexable OSM tags via
`k8s/photon-config.yaml` (`ConfigMap osm-photon-config`). Set
`photon_extra_tags` to either:

- `ALL` to import all available extra tags, or
- a comma-separated list of specific tag keys such as `brand,network,operator`

The value is passed to Photon's `-extra-tags` import flag during
`k8s/photon-import-job.yaml`, so a re-run of the Photon import job will rebuild
the index with the configured tags.

### Tuning the Pelias index

Pelias can also be configured with additional OSM venue tags via
`k8s/pelias-config.yaml` (`ConfigMap osm-pelias-config`). Set
`pelias_extra_tags` to a comma-separated list of tag keys such as
`brand,network,operator`.

Each tag is expanded to `<tag>+name` for the importer, so a re-run of the
Pelias import job will rebuild the index with the configured tags.

For better place enrichment, Pelias now also supports admin lookup with
Who's On First (WOF) data. WOF is enabled by default (`pelias_wof_enabled:
"true"`). The import job automatically downloads the WOF SQLite database via
`pelias download wof` into `pelias_wof_root` and imports it into Elasticsearch
before indexing the OSM data — no manual file provisioning is required.
Without WOF data (set `pelias_wof_enabled: "false"` to disable), Pelias can
still index the OSM data, but the country/region/city enrichment will remain
incomplete. This is the same one-time setup step required for accurate admin
hierarchy data in Pelias.

This setup uses Pelias' built-in Elasticsearch-backed text search. It is a
strong geocoding/full-text search stack, but it is not the same as a separate
LLM-based semantic search layer. For this repository, the relevant setup step is
therefore the extra tag indexing plus the WOF admin lookup, not an external
AI model.

## Root CA certificate injection

All pods include a `ca-injector` init container that can inject a custom Root CA certificate into every container at startup — useful when your cluster sits behind a corporate proxy or a custom PKI.

### Setup

```bash
cp k8s/ca-bundle-config.yaml.example k8s/ca-bundle-config.yaml
```

Edit `k8s/ca-bundle-config.yaml`:

```yaml
data:
  ca-enabled: "true"        # set to "true" to enable injection
  ca.crt: |                 # paste your full PEM certificate(s) here
    -----BEGIN CERTIFICATE-----
    ...
    -----END CERTIFICATE-----
```

`k8s/ca-bundle-config.yaml` is listed in `.gitignore` so the certificate is never committed.

### How it works

1. **`ca-injector`** (Alpine 3.20) always writes a valid cert bundle to `/ca-certs/ca.crt`:
   - Disabled (or empty cert): copies the Alpine system CA bundle — all existing env vars still work.
   - Enabled: appends the custom CA to the system bundle.
2. All containers mount `/ca-certs` read-only and have these env vars set automatically:
   `SSL_CERT_FILE`, `CURL_CA_BUNDLE`, `REQUESTS_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`.
3. Java containers (`eclipse-temurin`, `pelias/elasticsearch`) additionally get a **`java-ca-injector`** that creates a JKS keystore at `/ca-certs/cacerts` and sets `JAVA_TOOL_OPTIONS` to use it.

If `ca-bundle-config.yaml` is absent when `deploy-osm.sh` runs, a disabled placeholder is created automatically from the example file.

## URLs

- Status dashboard: `http://<node-ip>:30083/`
- Web / routing UI: `http://<node-ip>:30084/`
- Nominatim: `http://<node-ip>:30081/`
- Valhalla: `http://<node-ip>:30082/`
- TileServer GL: `http://<node-ip>:30085/`
- Style-Editor (Maputnik): `http://<node-ip>:30086/`

## Style-Editor

The status dashboard's **Style-Editor** card opens Maputnik (pre-loaded with the currently active
TileServer-GL style via `GET /api/style` on the status dashboard). After editing visually, export the
style in Maputnik (Menu ▸ Export style ▸ Download) and upload the exported `style.json` back through
the "Style aktivieren" button on the status dashboard. The status app validates the style, writes it
back into the mounted `tileserver-gl` PVC, and restarts the `tileserver-gl` deployment so the new
style becomes active within seconds — without any manual `scp`/`kubectl` steps.

## Notes

- The status dashboard mainly reports service health, data files, and orchestrator progress; the Style-Editor card is the one place it accepts a write (activating an edited style.json).
- Set `use_java: "false"` in `k8s/tileserver-import-profile-config.yaml` to run the Planetiler import without the custom Java profile for comparison tests.
- The routing web UI remains unchanged.
- The orchestrator exits with code 0 when watched config maps change so Kubernetes restarts it with fresh state.

- The orchestrator uses `alpine/k8s:1.30.0`; keep it aligned with the cluster Kubernetes minor version.

## StorageClass performance testing

`storage-perf-tester/` is a self-contained, optional tool for comparing the
StorageClasses available in a cluster (fio + PostgreSQL/pgbench workloads,
IOPS/MB/s/latency, configurable ranking). It never touches the OSM stack
deployed by this repository; see `storage-perf-tester/README.md` for usage.
