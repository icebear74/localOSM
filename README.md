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
| `k8s/base/storage/local-path-storage-class.yaml` | `local-path` StorageClass + konfigurierbarer Basis-Pfad |
| `k8s/base/pvcs/*.yaml` | PVCs für Nominatim, Valhalla, TileServer, Photon und Planet-Daten |
| `k8s/base/configmaps/*.yaml` | Editierbare GeoJSON- und Osmium-Filter-Konfigurationen |
| `k8s/base/cronjobs/planet-update-cronjob.yaml` | Stündliches Planet-Update via `pyosmium-up-to-date` |
| `k8s/base/jobs/osmium-pipeline-job.yaml` | Extract → Filter → Merge Pipeline für `europa_mitte_final.osm.pbf` |
| `k8s/base/jobs/*-import.yaml` | Manuell und per WebUI triggerbare Service-Import-Jobs |
| `k8s/base/deployments/*.yaml` | PVC-basierte Service-Deployments ohne direkte `hostPath`-Mounts |
| `k8s/status.yaml` | Status-Dashboard mit Job-/ConfigMap-Triggern |
| `scripts/setup-osm.sh` | Legt nur noch das Basisverzeichnis `/mnt/OSM` an |
| `scripts/run-planet-update.sh` | Manueller Trigger für den Planet-Update-Job |
| `scripts/run-osmium-pipeline.sh` | Manueller Trigger für die Osmium-Daten-Pipeline |

## Host data

Persistent data is now provisioned through the `local-path` StorageClass and dedicated PVCs. The common data source for all imports lives under `/mnt/OSM` inside the `planet-storage-pvc` volume and contains `planet-latest.osm.pbf`, optional `privat.osm`, and the generated `europa_mitte_final.osm.pbf`.

Important subdirectories under `/mnt/data/OSM` (final data only):

- `library/` – downloaded/cached `.osm.pbf` country extracts, reused across imports
- `nominatim/active`
- `valhalla/active`
- `tileserver/active`
- `manifests/` – static YAML copies used by the orchestrator pod
- `scripts/` – mounted orchestration script
- `status/` – dashboard and orchestrator state files

Important subdirectories under `/mnt/data/OSM/TempDir` (scratch data only, cleared after each import step):

- `import/` – merged/downloaded `planet.osm.pbf` used as the shared input for the TileServer, Nominatim and Valhalla import jobs
- `nominatim/staging` – osm2pgsql/PostgreSQL working data while Nominatim import runs
- `valhalla/staging` – routing graph/tile build working data
- `tileserver/staging` – Planetiler working data (mbtiles output, downloaded source files)

## Deploy

```bash
bash scripts/setup-osm.sh
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/base/storage/
kubectl apply -f k8s/base/pvcs/
kubectl apply -f k8s/base/configmaps/
kubectl apply -f k8s/base/deployments/
```

## Pipeline & Imports

```bash
bash scripts/run-planet-update.sh
bash scripts/run-osmium-pipeline.sh
kubectl apply -f k8s/base/jobs/nominatim-import.yaml
kubectl apply -f k8s/base/jobs/valhalla-import.yaml
kubectl apply -f k8s/base/jobs/tileserver-import.yaml
kubectl apply -f k8s/base/jobs/photon-import.yaml
```

Alle Service-Imports lesen dieselbe Datei `europa_mitte_final.osm.pbf`, die aus `planet-latest.osm.pbf` via GeoJSON-Extract, Tag-Filter und Merge erzeugt wird.

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

## PVC Migration & WebUI

- Die neue WebUI kann GeoJSON- und Filter-ConfigMaps direkt aktualisieren.
- Vorgefertigte Jobs lassen sich über `/api/jobs/.../trigger` starten, ohne dass der Webservice eigene Importlogik ausführt.
- `nominatim-import` nutzt zusätzlich `--flatnode-file`, was große Imports typischerweise spürbar beschleunigt.
