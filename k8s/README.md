# Kubernetes Architektur (StorageClass + Planet Pipeline)

## Überblick

Die neue Basisstruktur unter `k8s/base/` bereitet LocalOSM auf spätere Blue/Green-Deployments vor:

- `storage/` – `local-path` StorageClass mit konfigurierbarem Basispfad
- `pvcs/` – dedizierte PVCs für Nominatim, Valhalla, TileServer, Photon und Planet-Daten
- `configmaps/` – editierbare GeoJSON- und Filter-Konfigurationen
- `cronjobs/` – stündliches Planet-Update via `pyosmium-up-to-date`
- `jobs/` – Osmium-Pipeline und manuell triggerbare Import-Jobs
- `deployments/` – Services ohne direkte `hostPath`-Mounts

## Datenfluss

1. `planet-update` aktualisiert `/mnt/OSM/planet-latest.osm.pbf`
2. `osmium-pipeline` erzeugt daraus `/mnt/OSM/europa_mitte_final.osm.pbf`
3. Die Import-Jobs lesen ausschließlich diese finale PBF-Datei
4. Die WebUI triggert nur diese vorgefertigten Jobs; dieselben YAMLs sind auch direkt per `kubectl apply -f ...` nutzbar

## Rollout

```bash
bash scripts/setup-osm.sh
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/base/storage/
kubectl apply -f k8s/base/pvcs/
kubectl apply -f k8s/base/configmaps/
kubectl apply -f k8s/base/deployments/
kubectl apply -f k8s/base/cronjobs/planet-update-cronjob.yaml
```

## Manuelle Ausführung

```bash
bash scripts/run-planet-update.sh
bash scripts/run-osmium-pipeline.sh
kubectl apply -f k8s/base/jobs/nominatim-import.yaml
kubectl apply -f k8s/base/jobs/valhalla-import.yaml
kubectl apply -f k8s/base/jobs/tileserver-import.yaml
kubectl apply -f k8s/base/jobs/photon-import.yaml
```

## WebUI/API

Die Status-WebUI ergänzt dafür folgende Endpunkte:

- `POST /api/config/upload/geojson`
- `POST /api/config/upload/filter`
- `POST /api/jobs/planet-update/trigger`
- `POST /api/jobs/osmium-pipeline/trigger`
- `POST /api/jobs/{service}-import/trigger`
- `GET /api/jobs/{jobname}/status`
- `GET /api/jobs/{jobname}/logs`
