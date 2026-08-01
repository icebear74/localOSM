# Migration vom alten HostPath-Layout zur PVC-basierten Pipeline

## Zielbild

Statt hart codierter `hostPath`-Mounts werden dedizierte PVCs mit der StorageClass `local-path` verwendet. Die gemeinsame Quelle für alle Imports ist künftig:

- `planet-latest.osm.pbf`
- `europa_mitte_final.osm.pbf`
- optionale Zusatzdaten `privat.osm`

## Schritte

1. Basisverzeichnis anlegen: `bash scripts/setup-osm.sh`
2. Namespace und StorageClass anwenden
3. PVCs anwenden und Bound-Status prüfen
4. Neue Deployments aus `k8s/base/deployments/` ausrollen
5. Planet-Update-CronJob aktivieren
6. Osmium-Pipeline testen
7. Import-Jobs pro Service einzeln ausführen

## Hinweise

- Die WebUI ist nur ein Trigger für die vordefinierten Kubernetes-Jobs.
- Die Jobs bleiben vollständig per CLI nutzbar.
- `nominatim-import` verwendet `--flatnode-file`, löscht die Datei nach dem Import wieder und behält den Graceful-Shutdown-Fix bei.
- `tileserver-import` importiert weiterhin zusätzliche OSM-Tags wie `construction`.
