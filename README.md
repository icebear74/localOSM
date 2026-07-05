# localOSM

This repository contains a Kubernetes deployment scaffold for a local OSM stack on K3s with a built-in status service.

## What is included

The deployment now covers the main OSM building blocks:

- `k8s/postgres.yaml` – PostgreSQL + PostGIS for the OSM data backend
- `k8s/tileserver.yaml` – TileServer GL for raster tiles
- `k8s/nominatim.yaml` – Nominatim for address lookup / reverse geocoding
- `k8s/valhalla.yaml` – Valhalla for routing / distance API
- `k8s/status.yaml` – a small Python-based status container with test endpoints
- `scripts/deploy-osm.sh` – creates the required host directories under `/mnt/data/OSM` and applies the manifests
- `scripts/run-import.sh` – downloads an OSM `.pbf` file into the local import directory

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

## Status container and tests

The status container is available as a NodePort on port `30083`:

- `http://<node-ip>:30083/`
- `http://<node-ip>:30083/api/status`
- `http://<node-ip>:30083/test/postgres`
- `http://<node-ip>:30083/test/tileserver`
- `http://<node-ip>:30083/test/nominatim`
- `http://<node-ip>:30083/test/valhalla`

## Import a local OSM extract

Download a `.pbf` file into the local import directory:

```bash
bash scripts/run-import.sh --url https://download.geofabrik.de/europe/germany/berlin-latest.osm.pbf
```

The file will be stored under:

- `/mnt/data/OSM/import/planet.osm.pbf`

## Notes

This is a complete deployment and test workflow scaffold for a local OSM stack. The base services are deployed, the host directories are created, and the status container gives you simple test endpoints. The actual import into Nominatim / Valhalla still needs a region-specific import step if you want a fully populated geocoder and routing graph.
