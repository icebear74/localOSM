# localOSM

This repository contains a complete Kubernetes workflow for a local OSM stack on K3s with a built-in status service and a simple routing web UI.

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

- TileServer GL: `http://<node-ip>:30080`
- Nominatim: `http://<node-ip>:30081`
- Valhalla: `http://<node-ip>:30082`
- Status UI: `http://<node-ip>:30083/`
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
