# localOSM

A self-hosted OSM stack on K3s with a read-only status dashboard, a routing web UI, and a sequential import orchestrator.

## Components

- PostgreSQL + PostGIS
- Nominatim
- Valhalla
- TileServer GL
- Status dashboard
- Routing web UI
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
| `scripts/deploy-osm.sh` | Installs manifests and stages static files on the host |
| `scripts/run-import.sh` | Downloads a `.osm.pbf` and creates an import request |
| `scripts/import-orchestrator.sh` | Sequential import workflow executed inside the orchestrator pod |

## Host data

Persistent data lives under `/mnt/data/OSM`.

Important subdirectories:

- `import/` – downloaded `.osm.pbf` files
- `nominatim/active` / `nominatim/staging`
- `valhalla/active` / `valhalla/staging`
- `tileserver/active` / `tileserver/staging`
- `manifests/` – static YAML copies used by the orchestrator pod
- `scripts/` – mounted orchestration script
- `status/` – dashboard and orchestrator state files

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

## URLs

- Status dashboard: `http://<node-ip>:30083/`
- Web / routing UI: `http://<node-ip>:30084/`
- Nominatim: `http://<node-ip>:30081/`
- Valhalla: `http://<node-ip>:30082/`
- TileServer GL: `http://<node-ip>:30085/`

## Notes

- The status dashboard is read-only and only reports service health, data files, and orchestrator progress.
- The routing web UI remains unchanged.
- The orchestrator exits with code 0 when watched config maps change so Kubernetes restarts it with fresh state.

- The orchestrator uses `alpine/k8s:1.30.0`; keep it aligned with the cluster Kubernetes minor version.
