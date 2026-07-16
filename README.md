# localOSM

A self-hosted OSM stack on K3s with a read-only status dashboard, a routing web UI, and a sequential import orchestrator.

## Components

- PostgreSQL + PostGIS
- Nominatim
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
| `k8s/tileserver-import-job.yaml` | TileServer/Planetiler import job |
| `k8s/status.yaml` | Read-only status dashboard |
| `k8s/web.yaml` | Browser routing UI |
| `k8s/style-editor.yaml` | Maputnik style editor (edits the live TileServer-GL style.json via the status dashboard API) |
| `scripts/deploy-osm.sh` | Installs manifests and stages static files on the host |
| `scripts/run-import.sh` | Downloads a `.osm.pbf` and creates an import request |
| `scripts/import-orchestrator.sh` | Sequential import workflow executed inside the orchestrator pod |

## Host data

Persistent (final) data lives under `/mnt/data/OSM`. All temporary/scratch
data produced while an import is running lives under a separate directory,
`/mnt/data/OSMTemp` (override with `OSM_TEMP_DIR`). Create and mount
`OSMTemp` on fast storage (e.g. an SSD) yourself — the scripts only manage
its *contents*, never the directory/mount point itself, and they clear those
contents as soon as an import step has finished (successfully or not) so
nothing lingers on the fast disk.

Important subdirectories under `/mnt/data/OSM` (final data only):

- `library/` – downloaded/cached `.osm.pbf` country extracts, reused across imports
- `nominatim/active`
- `valhalla/active`
- `tileserver/active`
- `manifests/` – static YAML copies used by the orchestrator pod
- `scripts/` – mounted orchestration script
- `status/` – dashboard and orchestrator state files

Important subdirectories under `/mnt/data/OSMTemp` (scratch data only, cleared after each import step):

- `import/` – merged/downloaded `planet.osm.pbf` used as the shared input for the TileServer, Nominatim and Valhalla import jobs
- `nominatim/staging` – osm2pgsql/PostgreSQL working data while Nominatim import runs
- `valhalla/staging` – routing graph/tile build working data
- `tileserver/staging` – Planetiler working data (mbtiles output, downloaded source files)

## Deploy

```bash
bash scripts/deploy-osm.sh
```

## Import data

```bash
bash scripts/run-import.sh --url https://download.geofabrik.de/europe/germany/berlin-latest.osm.pbf
```

The script downloads the extract and writes an import request. The orchestrator pod processes requests strictly in sequence:

1. Nominatim
2. Valhalla
3. TileServer

Each step uses a dedicated Kubernetes Job and only promotes staged data after the job succeeds.

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
to the same host path TileServer-GL serves from, and restarts the `tileserver-gl` deployment so the
new style becomes active within seconds — without any manual `scp`/`kubectl` steps.

## Notes

- The status dashboard mainly reports service health, data files, and orchestrator progress; the Style-Editor card is the one place it accepts a write (activating an edited style.json).
- The routing web UI remains unchanged.
- The orchestrator exits with code 0 when watched config maps change so Kubernetes restarts it with fresh state.

- The orchestrator uses `alpine/k8s:1.30.0`; keep it aligned with the cluster Kubernetes minor version.
