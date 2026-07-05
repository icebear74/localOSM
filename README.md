# localOSM

This repository contains a minimal Kubernetes deployment for a local OSM-like stack on K3s.

## What is included

- `k8s/namespace.yaml` – creates the `osm` namespace
- `k8s/postgres.yaml` – deploys PostgreSQL for the OSM stack
- `k8s/tileserver.yaml` – deploys a local tile server container
- `scripts/deploy-osm.sh` – creates `/mnt/data/OSM/...` directories and applies the manifests

## Host paths used

The deployment uses hostPath mounts so data stays on the node under:

- `/mnt/data/OSM/postgres/data`
- `/mnt/data/OSM/tileserver`

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
kubectl -n osm get pvc
```

## Notes

- The PostgreSQL password is currently set to `osm` for simplicity.
- The tile server image is a generic placeholder for a local OSM tileserver setup; replace it with your preferred image if needed.
