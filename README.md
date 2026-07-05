# localOSM

A self-hosted OSM stack running on Kubernetes (K3s) with routing, geocoding, map tiles, a visual status dashboard, and a browser-based routing UI.

## What is included

| File | Purpose |
|---|---|
| `k8s/namespace.yaml` | Kubernetes namespace `osm` |
| `k8s/postgres.yaml` | PostgreSQL + PostGIS |
| `k8s/tileserver.yaml` | TileServer GL – raster/vector tiles |
| `k8s/nominatim.yaml` | Nominatim – address search & geocoding |
| `k8s/valhalla-config.yaml` | Valhalla service configuration (full 3.x format) |
| `k8s/valhalla.yaml` | Valhalla – routing engine |
| `k8s/valhalla-import-job.yaml` | Batch job to build the Valhalla routing graph from a `.osm.pbf` file |
| `k8s/status.yaml` | Auto-refreshing status dashboard (service health, import files, tile size) |
| `k8s/web.yaml` | Browser routing UI with Leaflet map, geocoding, distance & route calculation |
| `scripts/deploy-osm.sh` | Create host directories and install or update all manifests |
| `scripts/run-import.sh` | Download an OSM PBF file and start the Valhalla import job |
| `scripts/remove-rancher.sh` | Helper to remove Rancher/Fleet leftovers from a cluster |
| `scripts/reset-k3s.sh` | Fully uninstall and reinstall K3s on the host |
| `status/app.py` | Source for the status container |

## Host directories

Persistent data is stored under `/mnt/data/OSM/`:

```
/mnt/data/OSM/
  postgres/data   – PostgreSQL data
  library/        – downloaded country extracts used for merged rebuilds
  tileserver/     – TileServer GL styles & tiles
  nominatim/      – Nominatim PostgreSQL data (pg15)
  valhalla/       – Valhalla routing graph (tiles/)
  import/         – Downloaded .osm.pbf files
  cache/          – Scratch space
  status/         – Status metadata
```

## Prerequisites

- Kubernetes cluster (K3s recommended)
- `kubectl` configured and pointing at the cluster
- Outbound internet access from the node (to pull images and download PBF)

## Quick start

### 1 – Deploy the stack

```bash
bash scripts/deploy-osm.sh
```

This creates the host directories, sets permissions, and installs the stack. Re-running it updates an existing installation.

### 2 – Load OSM data

```bash
bash scripts/run-import.sh --url https://download.geofabrik.de/europe/germany/berlin-latest.osm.pbf
```

The script downloads the PBF file to `/mnt/data/OSM/import/planet.osm.pbf` with a live progress bar, then starts the Valhalla import job automatically.

Monitor the import:

```bash
kubectl -n osm logs -f job/valhalla-import
```

### 3 – Open the UI

| Service | URL | NodePort |
|---|---|---|
| **Routing UI** (Leaflet map + routing) | `http://<node-ip>:30084/` | 30084 |
| **Status dashboard** (auto-refresh + country library manager) | `http://<node-ip>:30083/` | 30083 |
| TileServer GL | `http://<node-ip>:30085/` | 30085 |
| Nominatim | `http://<node-ip>:30081/` | 30081 |
| Valhalla API | `http://<node-ip>:30082/` | 30082 |

## Using the routing UI

Open `http://<node-ip>:30084/`:

- Enter a start and destination address in the search boxes and press 🔍 to geocode them (requires Nominatim with imported data)
- Or enter coordinates directly / click on the map (first click = start, second click = destination)
- Choose vehicle type (Auto, Zu Fuß, Fahrrad, LKW) and units
- Click **Route berechnen** to draw the route on the map and show distance + travel time
- Click **Nur Distanz** for a quick distance/time answer without a map drawing

> **Note:** Routing requires the Valhalla import job to have completed successfully (see Status dashboard → "Valhalla Tiles" card).

## Status dashboard

Open `http://<node-ip>:30083/`:

- Colour-coded health indicators for all services (green = reachable, red = down)
- Country library dropdown for adding predefined countries such as the Netherlands, Germany, Belgium, and more
- Optional custom country name + `.osm.pbf` URL fields for extra extracts
- Live workflow progress while the dashboard downloads, merges, rebuilds Valhalla, and refreshes Nominatim
- Added-country cards that show which countries are already part of the merged library
- Import directory listing with file sizes and timestamps
- Valhalla tile graph size – shows "Routing bereit" once tiles exist
- Auto-refreshes every 5 seconds

## Verify the deployment

```bash
kubectl -n osm get all
kubectl -n osm get svc
kubectl -n osm logs job/valhalla-import
```

## Optional: remove Rancher leftovers

If namespace deletion is stuck because of stale `ext.cattle.io` or other Rancher APIs:

```bash
bash scripts/remove-rancher.sh --dry-run
bash scripts/remove-rancher.sh --yes
```

The script removes Rancher/Fleet namespaces, CRDs, APIService entries, webhooks, and related cluster roles, then forces namespace finalizer cleanup if needed.

## Optional: reset K3s completely

If you want a full clean restart of K3s (and therefore remove Rancher leftovers with it):

```bash
bash scripts/reset-k3s.sh --dry-run
bash scripts/reset-k3s.sh --yes
```

Optional flags:

- `--channel stable` (default)
- `--version v1.31.1+k3s1` (installs exact version)
- `--keep-data` (skip local data directory cleanup)

## Notes

- **Nominatim** uses its own internal PostgreSQL 15 instance. The `PBF_PATH` env var triggers data import on first start – this can take a long time for large extracts.
- **TileServer GL** needs `.mbtiles` or `.pmtiles` files placed in `/mnt/data/OSM/tileserver/` and a `config.json` style definition to serve tiles.
- The routing UI falls back to OSM tile CDN for the map background if no local TileServer tiles are configured.


## What is included

The deployment now covers the main OSM building blocks:

- `k8s/postgres.yaml` – PostgreSQL + PostGIS for the OSM data backend
- `k8s/tileserver.yaml` – TileServer GL for raster tiles
- `k8s/nominatim.yaml` – Nominatim for address lookup / reverse geocoding
- `k8s/valhalla.yaml` – Valhalla for routing / distance API
- `k8s/valhalla-config.yaml` – a basic Valhalla config file for the routing service
- `k8s/valhalla-import-job.yaml` – a batch job that builds a Valhalla routing graph from a local `.osm.pbf` file
- `k8s/status.yaml` – a Python-based status container with basic test endpoints
- `k8s/web.yaml` – a Python-based web UI that allows distance and routing requests via the local Valhalla service
- `scripts/deploy-osm.sh` – creates the required host directories under `/mnt/data/OSM` and applies the manifests
- `scripts/run-import.sh` – downloads an OSM `.pbf` file into the local import directory and starts the Valhalla import job

## Host paths used

The deployment stores persistent data under:

- `/mnt/data/OSM/postgres/data`
- `/mnt/data/OSM/library`
- `/mnt/data/OSM/tileserver`
- `/mnt/data/OSM/nominatim`
- `/mnt/data/OSM/valhalla`
- `/mnt/data/OSM/import`
- `/mnt/data/OSM/cache`
- `/mnt/data/OSM/status`

## Prerequisites

- A working Kubernetes cluster (for example K3s)
- `kubectl` configured to talk to it

## Deploy the stack

Run:

```bash
bash scripts/deploy-osm.sh
```

## Verify

```bash
kubectl -n osm get all
kubectl -n osm get svc
```

## Access the services

Once deployed, the services are exposed as NodePorts:

- TileServer GL: `http://<node-ip>:30085`
- Nominatim: `http://<node-ip>:30081`
- Valhalla: `http://<node-ip>:30082`
- Status UI + country library manager: `http://<node-ip>:30083/`
- Web UI / routing UI: `http://<node-ip>:30084/`

## Import OSM data

Download an OSM extract and import it into Valhalla:

```bash
bash scripts/run-import.sh --url https://download.geofabrik.de/europe/germany/berlin-latest.osm.pbf
```

The file will be stored under:

- `/mnt/data/OSM/import/planet.osm.pbf`

The import job will start automatically and build the Valhalla graph. You can inspect logs with:

```bash
kubectl -n osm logs job/valhalla-import
```

## Using the web UI

Open the routing UI at `http://<node-ip>:30084/` and enter coordinates. The web UI uses the local Valhalla service to compute routes and distances.

## Notes

This is a full deployment and test workflow scaffold for a local OSM stack. It provides the runtime services, persistent storage, a Python-based status page, and a routing web UI. The actual OSM import into Nominatim / PostGIS still requires a more detailed import workflow if you want a fully populated geocoder and huge-area routing graph, but the stack is now usable for local testing and route calculation once a suitable `.osm.pbf` file is available.
