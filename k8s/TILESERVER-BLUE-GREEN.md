# TileServer Blue/Green Deployment

## Überblick

Dieses Dokument beschreibt die Blue/Green-Deployment-Strategie für TileServer GL.
Der Alt-Stack (`tileserver-gl`, Port 30085) läuft unverändert weiter.
Der Neu-Stack verwendet Ports 31085 (Main/Switch), 31086 (Blue direkt), 31087 (Green direkt).

## Architektur

```
User/WebUI
    │
    ▼
:31085  tileserver-gl (Service, Selector: app=tileserver, active=true)
    ├── → tileserver-green  (replicas=1, active=true)   ← Initialzustand
    └── → tileserver-blue   (replicas=0, active=false)

:31086  tileserver-blue  (immer Blue, für Testing)
:31087  tileserver-green (immer Green, für Testing)
```

## Verzeichnisstruktur

```
k8s/base/
├── pvcs/
│   ├── tileserver-blue-pvc.yaml    (200Gi, active=false)
│   └── tileserver-green-pvc.yaml   (200Gi, active=true)
├── deployments/
│   ├── tileserver-blue.yaml        (replicas=0, wartet auf ersten Import)
│   └── tileserver-green.yaml       (replicas=1, startet mit leeren Daten)
├── services/
│   ├── tileserver-service.yaml     (Port 31085, Selector switcht)
│   ├── tileserver-blue-service.yaml  (Port 31086, immer Blue)
│   └── tileserver-green-service.yaml (Port 31087, immer Green)
├── configmaps/
│   └── tileserver-import-manifest.yaml  (Job2-Template für Orchestrator)
├── rbac/
│   └── tileserver-orchestrator-rbac.yaml
├── jobs/
│   └── tileserver-import-orchestrator.yaml  (Job1, Orchestrator)
└── storage/
    └── hostpath-config.yaml        (HostPath-Konfiguration)

scripts/
├── run-tileserver-import.sh    (Import auslösen)
└── switch-tileserver.sh        (Manuell Blue/Green switchen)

k8s/old/
└── tileserver.yaml             (Backup des originalen Deployments)
```

## Initialzustand

| Deployment       | Replicas | PVC                  | active |
|------------------|----------|----------------------|--------|
| tileserver-green | 1        | tileserver-green-pvc | true   |
| tileserver-blue  | 0        | tileserver-blue-pvc  | false  |

## Import-Workflow

### Erster Import (beide PVCs leer)

```
1. Green läuft mit leeren Daten (replicas=1, active=true)
2. Blue wartet (replicas=0, active=false)
3. User startet: bash scripts/run-tileserver-import.sh
4. Orchestrator-Job erkennt: Green aktiv → Target = Blue
5. Import in Blue-PVC (map.mbtiles)
6. Orchestrator: Scale Blue → 1, warte auf Ready
7. Service-Selector wechselt: color=blue
8. PVC-Labels aktualisiert: blue.active=true, green.active=false
9. Green wird skaliert auf 0
```

### Zweiter Import

```
1. Blue läuft mit Daten (replicas=1, active=true)
2. Orchestrator-Target = Green
3. Import in Green-PVC
4. Service-Selector wechselt: color=green
5. Blue wird skaliert auf 0
```

## Rollout

```bash
# 1. Verzeichnisse auf Node anlegen
bash k8s/base/storage/hostpath-config.yaml   # enthält setup.sh
# oder manuell:
mkdir -p /mnt/OSM/tileserver/blue /mnt/OSM/tileserver/green

# 2. HostPath-Config aktualisieren (falls abweichend von /mnt/k8s)
kubectl apply -f k8s/base/storage/hostpath-config.yaml

# 3. PVCs anlegen
kubectl apply -f k8s/base/pvcs/tileserver-blue-pvc.yaml
kubectl apply -f k8s/base/pvcs/tileserver-green-pvc.yaml

# 4. Deployments anlegen
kubectl apply -f k8s/base/deployments/tileserver-blue.yaml
kubectl apply -f k8s/base/deployments/tileserver-green.yaml

# 5. Services anlegen
kubectl apply -f k8s/base/services/tileserver-service.yaml
kubectl apply -f k8s/base/services/tileserver-blue-service.yaml
kubectl apply -f k8s/base/services/tileserver-green-service.yaml

# 6. RBAC anlegen
kubectl apply -f k8s/base/rbac/tileserver-orchestrator-rbac.yaml

# 7. ConfigMap (Importer-Template) anlegen
kubectl apply -f k8s/base/configmaps/tileserver-import-manifest.yaml

# 8. Orchestrator-Job-Template anlegen
kubectl apply -f k8s/base/jobs/tileserver-import-orchestrator.yaml

# 9. Erster Import starten
bash scripts/run-tileserver-import.sh

# 10. Logs verfolgen
kubectl logs -f job/tileserver-import-orchestrator-<SUFFIX> -n osm
```

## Manueller Switch

```bash
# Zu Blue wechseln
bash scripts/switch-tileserver.sh blue

# Zu Green wechseln
bash scripts/switch-tileserver.sh green
```

## WebUI

Neue Endpunkte:

| Methode | Pfad                              | Beschreibung                          |
|---------|-----------------------------------|---------------------------------------|
| POST    | `/api/jobs/tileserver/import`     | Startet Orchestrator-Job              |
| GET     | `/api/jobs/tileserver/import/status` | Status des letzten Orchestrator-Jobs |
| GET     | `/api/services/tileserver/state`  | Blue/Green Status (Farbe, Replicas)   |
| POST    | `/api/services/tileserver/switch` | Manueller Blue/Green Switch           |

## Bekannte Einschränkungen

- Alt-Stack (Port 30085) läuft parallel weiter und wird nicht beeinflusst.
- Rollback erfolgt manuell via `scripts/switch-tileserver.sh`.
- Der Service-Selector muss `active=true` als Label auf den Pod-Templates haben –
  der Orchestrator patcht dies beim Switch automatisch.

## Konfigurierbare Parameter

Umgebungsvariablen für `scripts/run-tileserver-import.sh` und `scripts/switch-tileserver.sh`:

| Variable              | Default | Beschreibung          |
|-----------------------|---------|-----------------------|
| `TILESERVER_NAMESPACE` | `osm`  | Kubernetes Namespace  |

HostPath-Pfade in `k8s/base/storage/hostpath-config.yaml`:

| Variable        | Default   | Beschreibung                         |
|-----------------|-----------|--------------------------------------|
| `HOSTPATH_BASE` | `/mnt/k8s`| Basis-Verzeichnis für local-path PVCs|
| `OSM_DATA_DIR`  | `/mnt/OSM`| OSM-Datenverzeichnis auf dem Node    |
