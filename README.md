# localOSM

This repository contains a Kubernetes deployment scaffold for a local OSM stack on K3s.

## What is included

The deployment now covers the main OSM building blocks:

- `k8s/postgres.yaml` – PostgreSQL + PostGIS for the OSM data backend
- `k8s/tileserver.yaml` – TileServer GL for raster tiles
- `k8s/nominatim.yaml` – Nominatim for address lookup / reverse geocoding
- `k8s/valhalla.yaml` – Valhalla for routing / distance API
- `scripts/deploy-osm.sh` – creates the required host directories under `/mnt/data/OSM` and applies the manifests

## Host paths used

The deployment stores persistent data under:

- `/mnt/data/OSM/postgres/data`
- `/mnt/data/OSM/tileserver`
- `/mnt/data/OSM/nominatim`
- `/mnt/data/OSM/valhalla`

## Prerequisites

- A working Kubernetes cluster (for example K3s)
- `kubectl` configured to talk to it

## Deploy

Run:

```bash
bash scripts/deploy-osm.sh
```

## Verify

```bash
kubectl -n osm get all
kubectl -n osm get svc
```

## Access

Once deployed, the services are exposed as NodePorts:

- TileServer GL: `http://<node-ip>:30080`
- Nominatim: `http://<node-ip>:30081`
- Valhalla: `http://<node-ip>:30082`

## Notes

This is a deployment scaffold. The actual OSM data import and tile generation still require importing a real `.osm.pbf` file (or a regional extract) into PostGIS / Nominatim / Valhalla. The manifests here provide the runtime containers and storage layout so you can plug in the actual import workflow next.
