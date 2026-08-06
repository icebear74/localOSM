# Blue/Green Deployment für localOSM

Dieses Dokument verallgemeinert das TileServer-Muster auf TileServer, Nominatim, Valhalla und Photon.

## Betroffene Services

Blue/Green wird nur für datenintensive Rebuild-Services verwendet:

- TileServer
- Nominatim
- Valhalla
- Photon

Nicht betroffen sind Status, Web, Style-Editor und sonstige Steuer-Komponenten.

## Struktur

Für jeden Service gibt es jetzt:

- zwei PVCs (`-blue`, `-green`)
- zwei Deployments (`-blue`, `-green`)
- einen Haupt-Service mit `active=true`
- zwei Direkt-Services für Blue/Green-Tests
- ein Importer-Template als ConfigMap
- einen Orchestrator-Job
- `run-<service>-import.sh`
- `switch-<service>.sh`
- `build-<service>.sh`

Zusätzlich gibt es:

- `scripts/build-all.sh`
- `scripts/deploy-bluegreen.sh`

## Ports

- TileServer: 31085 / 31086 / 31087
- Nominatim: 31081 / 31082 / 31083
- Valhalla: 31088 / 31089 / 31090
- Photon: 31091 / 31092 / 31093

## Build-Semantik

"Build" bedeutet hier: Import/Neuerstellung der Servicedaten und anschließender Blue/Green-Swap.

- `build-<service>.sh` startet den jeweiligen Orchestrator und wartet bis zum Ende.
- `build-all.sh` baut standardmäßig sequenziell, um RAM-Spitzen zu vermeiden.
- `build-all.sh --parallel` startet alle vier Builds parallel.

## Deployment

```bash
bash scripts/deploy-bluegreen.sh
```

Mit Clean-Start:

```bash
bash scripts/deploy-bluegreen.sh --clean
```

Der Clean-Modus:

- löscht Ressourcen im Namespace `osm`
- löscht den Namespace selbst
- wartet, bis der Namespace vollständig terminiert ist (mit wiederholten Versuchen, hängende Finalizer zu entfernen), bevor Ressourcen neu angelegt werden
- löscht zusätzlich gebundene PVs des Namespace
- leert die Blue/Green-Datenverzeichnisse unter `/mnt/OSM`

Mit Soft-Reset (Pods/Jobs/Deployments neu erstellen, aber Daten behalten):

```bash
bash scripts/deploy-bluegreen.sh --soft-reset
```

Der Soft-Reset-Modus:

- löscht alle Workload-Ressourcen im Namespace `osm` (Deployments, Jobs, CronJobs, ConfigMaps, Secrets, Services)
- lässt die PVCs (und damit die importierten Daten) unangetastet
- behält den Namespace selbst bei
- eignet sich, wenn die WebUI/Jobs nach einem Redeploy nicht sauber wieder anlaufen, aber kein Datenverlust gewünscht ist

## Manueller Import / Switch

```bash
bash scripts/run-nominatim-import.sh
bash scripts/run-valhalla-import.sh
bash scripts/run-photon-import.sh
bash scripts/run-tileserver-import.sh

bash scripts/switch-nominatim.sh blue
bash scripts/switch-valhalla.sh green
bash scripts/switch-photon.sh blue
bash scripts/switch-tileserver.sh green
```

## Wichtige Entscheidung

Die vorhandenen Ressourcenwerte aus den Legacy-Manifests wurden für die neuen Blue/Green-Varianten unverändert übernommen. Dadurch bleiben die bereits abgestimmten Speicher-/CPU-Werte und JVM-Parameter erhalten.
