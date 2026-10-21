# localOSM

A self-hosted OSM stack on K3s with a read-only status dashboard, a routing web UI, and a sequential import orchestrator.

## Components

- Nominatim (with its bundled, configurably-tuned PostgreSQL/PostGIS)
- Valhalla
- TileServer GL
- Pelias (optional, separate comparison API; see [Pelias](#pelias-parallelbetrieb))
- Status dashboard
- Routing web UI
- Style-Editor (Maputnik)
- Import orchestrator pod

## Layout

| File | Purpose |
|---|---|
| `k8s/import-orchestrator.yaml` | Orchestrator deployment, RBAC, and config mounts |
| `k8s/nominatim-import-job.yaml` | Nominatim import job |
| `k8s/valhalla-import-job.yaml` | Valhalla import job |
| `k8s/planetiler-import-job.yaml` | TileServer/Planetiler import job on the Longhorn PVCs |
| `k8s/tileserver-import-profile-config.yaml` | Custom Planetiler profile plus the `use_java` toggle for enabling/disabling it during imports |
| `k8s/tileserver-gl-deployment.yaml` | TileServer-GL deployment and service using the `tileserver-gl` PVC |
| `k8s/osm-persistentvolumeclaims.yaml` | Longhorn PVC definitions for `planet`, shared temp PVC `osm-temp`, `tileserver-gl`, `nominatim`, `valhalla`, `photon`, and `osm-status` |
| `k8s/planetiler-rbac.yaml` | ServiceAccount/Role/RoleBinding for the Planetiler import job |
| `k8s/tileserver-init-assets-job.yaml` | One-shot job that copies static TileServer assets from the host bootstrap directory into the PVC |
| `k8s/status.yaml` | Read-only status dashboard |
| `k8s/web.yaml` | Browser routing UI |
| `k8s/style-editor.yaml` | Maputnik style editor (edits the live TileServer-GL `style_vibrant.json` via the status dashboard API) |
| `scripts/deploy-osm.sh` | Installs manifests and stages static files on the host |
| `scripts/run-import.sh` | Downloads a `.osm.pbf` and creates an import request |
| `scripts/import-orchestrator.sh` | Sequential import workflow executed inside the orchestrator pod |

## Host data

Host-side orchestration data still lives under `/mnt/OSM` by default (override
the scratch path with `--temp-dir` / `OSM_TEMP_DIR` when running
`scripts/deploy-osm.sh`). The deploy script keeps templated manifests, the
status dashboard state and the one-time TileServer bootstrap assets on the
host, while the heavy OSM datasets now live on Longhorn PVCs in Kubernetes.

Longhorn PVCs managed by `k8s/osm-persistentvolumeclaims.yaml`:

- `planet` (250Gi, RWO) – contains `/planet/europa.osm.pbf`
- `osm-temp` (700Gi, RWX) – shared import scratch/download volume (`import/` plus per-service work directories)
- `tileserver-gl` (150Gi, RWO) – live TileServer assets (`planet.mbtiles`, fonts, styles, sprites)
- `nominatim` (150Gi, RWX) – production Nominatim/Postgres data promoted by the Nominatim import job itself
- `valhalla` (100Gi, RWX) – active/next/previous Valhalla graph data promoted by the Valhalla import job
- `photon` (100Gi, RWX) – Photon release jar plus active/next/previous Photon index data
- `osm-status` (10Mi, RWX) – shared status/log files

Important host subdirectories under `/mnt/OSM`:

- `library/` – downloaded/cached `.osm.pbf` country extracts, reused across imports
- `manifests/` – static YAML copies used by the orchestrator pod
- `scripts/` – mounted orchestration script
- `status/` – dashboard and orchestrator state files
- `tileserver/bootstrap` – source directory for `tileserver-init-assets-job.yaml`

Important scratch paths under the shared Kubernetes temp PVC mounted inside the pods:

- `import/` – country-library merge used by parts of the legacy request workflow. This is **not** the authoritative Pelias input: the current Planetiler, Nominatim and Valhalla import jobs read `/planet/europa.osm.pbf` from PVC `planet`.
- `tileserver/work` – Planetiler work directory, cleaned after the new `planet.mbtiles` has been activated
- `tileserver/downloads` – reusable Planetiler side downloads that may remain between runs
- `nominatim/work` – temporary Postgres/Nominatim import working data, cleaned after promotion into the `nominatim` PVC
- `valhalla/work` – temporary Valhalla graph build directory, cleaned after activation into the `valhalla` PVC
- `photon/work` – temporary Photon index build directory, cleaned after activation into the `photon` PVC

## Deploy

```bash
bash scripts/deploy-osm.sh
```

If you only changed TileServer styles/fonts/bootstrap assets, refresh just the
TileServer bootstrap payload without re-running the full deploy:

```bash
bash scripts/prepare-tileserver-bootstrap.sh --apply-configmap
```

Then rerun only the TileServer asset bootstrap job to copy those prepared files
into the live TileServer PVC:

```bash
kubectl -n osm delete job tileserver-init-assets --ignore-not-found
kubectl apply -f /mnt/OSM/manifests/tileserver-init-assets-job.yaml
```

On a fresh cluster/PVC setup, run the one-shot asset bootstrap once before the
first TileServer import:

```bash
kubectl apply -f /mnt/OSM/manifests/tileserver-init-assets-job.yaml
```

## Import data

```bash
bash scripts/run-import.sh --url https://download.geofabrik.de/europe/germany/berlin-latest.osm.pbf
```

## Runtime rollout without reimport

For runtime/config fixes (for example Nominatim warm-up, Photon truststore handling, promotion script hardening), roll out only manifests/config and restart deployments.
Do **not** start any import jobs and do not clear `osm-temp`/service PVC data.

```bash
bash scripts/deploy-osm.sh            # or: bash scripts/deploy-osm.sh --bigmemory
kubectl -n osm rollout restart deployment/nominatim deployment/photon deployment/tileserver-gl
kubectl -n osm rollout status deployment/nominatim --timeout=600s
kubectl -n osm rollout status deployment/photon --timeout=600s
kubectl -n osm rollout status deployment/tileserver-gl --timeout=600s
```

Quick checks after restart:

- Nominatim: `kubectl -n osm logs deployment/nominatim | grep -E 'pg_prewarm|admin --warm'`
- Photon truststore type: `kubectl -n osm logs deployment/photon | grep -E 'Detected truststore type|localosm-custom-root-ca'`
- Promotion job behavior (rollback/copy status): `kubectl -n osm logs job/import-promotion --tail=200`

The script downloads the extract and writes an import request. The orchestrator pod processes requests strictly in sequence.

1. Nominatim
2. Valhalla
3. TileServer

Each step uses a dedicated Kubernetes Job. The shared `osm-temp` PVC keeps the reusable merged/downloaded inputs and per-service work directories.  
After successful build steps, a **central `import-promotion` job** (`k8s/import-promotion-job.yaml`) activates all available outputs in a fixed order (Nominatim, Valhalla, Photon, TileServer), checks free disk space per area first, copies file-by-file with per-file validation, then removes each source file only after successful target write.

### Valhalla traffic closures updater (hourly CronJob)

Closed-road updates are applied by `valhalla-traffic-updater` CronJob (hourly) which writes to:

- `/shared-data/active/traffic.tar`
- `/shared-data/active/roadworks-state.json`

Useful checks:

```bash
kubectl -n osm get cronjob valhalla-traffic-updater
kubectl -n osm get jobs --sort-by=.metadata.creationTimestamp | tail
kubectl -n osm logs job/<latest-valhalla-traffic-updater-job>
kubectl -n osm create job --from=cronjob/valhalla-traffic-updater valhalla-traffic-updater-manual-$(date +%s)
```

### Tuning the Nominatim import

The `osm2pgsql` step invoked by `nominatim import` maps its worker/process count 1:1 to the
`THREADS` value it is started with, and its node cache size (`--cache`, in MB) to
`NOMINATIM_OSM2PGSQL_CACHE`. `k8s/nominatim-import-config.yaml` (`ConfigMap
osm-nominatim-import-config`) is the single place to configure both:

- `import_threads` (default `8`) — match this to the CPU cores available to the
  `nominatim-import` Job (see its `resources.limits.cpu` in `k8s/nominatim-import-job.yaml`).
- `import_cache_mb` (default `12000`, i.e. ~12 GB) — match this to the RAM available to the
  `nominatim-import` Job (see its `resources.limits.memory`), up to roughly 75% of that RAM.

Edit the values, then re-apply the ConfigMap:

```bash
kubectl apply -f k8s/nominatim-import-config.yaml
```

before starting the next import. The same ConfigMap also supplies `import_password`, used by both
the import Job and the running `nominatim` deployment, so the two always stay in sync.

### Tuning the Photon search index

Photon imports can now be configured with additional, indexable OSM tags via
`k8s/photon-config.yaml` (`ConfigMap osm-photon-config`). Set
`photon_extra_tags` to either:

- `ALL` to import all available extra tags, or
- a comma-separated list of specific tag keys such as `brand,network,operator`

The value is passed to Photon's `-extra-tags` import flag during
`k8s/photon-import-job.yaml`, so a re-run of the Photon import job will rebuild
the index with the configured tags.

### Tuning the Pelias index

Pelias can also be configured with additional OSM venue tags via
`k8s/pelias-config.yaml` (`ConfigMap osm-pelias-config`). Set
`pelias_extra_tags` to a comma-separated list of tag keys such as
`brand,network,operator`.

Each tag is expanded to `<tag>+name` for the importer, so a re-run of the
Pelias import job will rebuild the index with the configured tags.

For better place enrichment, Pelias now also supports admin lookup with
Who's On First (WOF) data. WOF is enabled by default (`pelias_wof_enabled:
"true"`). The import job automatically downloads the WOF SQLite database via
`pelias download wof` into `pelias_wof_root` and imports it into Elasticsearch
before indexing the OSM data — no manual file provisioning is required.
Without WOF data (set `pelias_wof_enabled: "false"` to disable), Pelias can
still index the OSM data, but the country/region/city enrichment will remain
incomplete. This is the same one-time setup step required for accurate admin
hierarchy data in Pelias.

This setup uses Pelias' built-in Elasticsearch-backed text search. It is a
strong geocoding/full-text search stack, but it is not the same as a separate
LLM-based semantic search layer. For this repository, the relevant setup step is
therefore the extra tag indexing plus the WOF admin lookup, not an external
AI model.

## Root CA certificate injection

All pods include a `ca-injector` init container that can inject a custom Root CA certificate into every container at startup — useful when your cluster sits behind a corporate proxy or a custom PKI.

### Setup

```bash
cp k8s/ca-bundle-config.yaml.example k8s/ca-bundle-config.yaml
```

Edit `k8s/ca-bundle-config.yaml`:

```yaml
data:
  ca-enabled: "true"        # set to "true" to enable injection
  ca.crt: |                 # paste your full PEM certificate(s) here
    -----BEGIN CERTIFICATE-----
    ...
    -----END CERTIFICATE-----
```

`k8s/ca-bundle-config.yaml` is listed in `.gitignore` so the certificate is never committed.

### How it works

1. **`ca-injector`** (Alpine 3.20) always writes a valid cert bundle to `/ca-certs/ca.crt`:
   - Disabled (or empty cert): copies the Alpine system CA bundle — all existing env vars still work.
   - Enabled: appends the custom CA to the system bundle.
2. All containers mount `/ca-certs` read-only and have these env vars set automatically:
   `SSL_CERT_FILE`, `CURL_CA_BUNDLE`, `REQUESTS_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`.
3. Java containers (`eclipse-temurin`, `pelias/elasticsearch`) additionally get a **`java-ca-injector`** that creates a JKS keystore at `/ca-certs/cacerts` and sets `JAVA_TOOL_OPTIONS` to use it.

For download tools that do not reliably honor environment variables, manifests now pass CA paths explicitly (for example `wget --ca-certificate=/ca-certs/ca.crt`, `curl --cacert /ca-certs/ca.crt`).

## Admin Pod (Midnight Commander)

- Standard storage profile: `k8s/mc-admin.yaml`
- Big-memory storage profile: `k8s/bigmemory/mc-admin.yaml`

Both manifests provide a dedicated `mc-admin` deployment with all relevant PVCs mounted under `/pvc/*`.

## Styles / Routing

- Added new style: `k8s/style_pink.json` (mirrored in `k8s/bigmemory/`).
- Rail/rollercoaster line widths/dash patterns were adjusted for better readability across zoom levels.
- Construction street names are explicitly labeled in all styles (`road-label-construction`).
- Style metadata `metadata.localosm_route_style` defines routing line appearance per style; WebUI reads this and falls back to safe defaults when missing.

If `ca-bundle-config.yaml` is absent when `deploy-osm.sh` runs, a disabled placeholder is created automatically from the example file.

## URLs

- Status dashboard: `http://<node-ip>:30083/`
- Web / routing UI: `http://<node-ip>:30084/`
- Nominatim: `http://<node-ip>:30081/`
- Valhalla: `http://<node-ip>:30082/`
- TileServer GL: `http://<node-ip>:30085/`
- Style-Editor (Maputnik): `http://<node-ip>:30086/`

## Style-Editor

The status dashboard's **Style-Editor** card opens Maputnik (pre-loaded with the currently active
TileServer-GL style via `GET /api/style` on the status dashboard). After editing visually, export the
style in Maputnik (Menu ▸ Export style ▸ Download) and upload the exported style JSON back through
the "Style aktivieren" button on the status dashboard. The status app validates the style, writes it
back into the mounted `tileserver-gl` PVC, and restarts the `tileserver-gl` deployment so the new
style becomes active within seconds — without any manual `scp`/`kubectl` steps.

## Notes

- The status dashboard mainly reports service health, data files, and orchestrator progress; the Style-Editor card is the one place it accepts a write (activating an edited `style_vibrant.json`).
- Set `use_java: "false"` in `k8s/tileserver-import-profile-config.yaml` to run the Planetiler import without the custom Java profile for comparison tests.
- The routing web UI remains unchanged.
- The orchestrator exits with code 0 when watched config maps change so Kubernetes restarts it with fresh state.

- The orchestrator uses `alpine/k8s:1.30.0`; keep it aligned with the cluster Kubernetes minor version.

## StorageClass performance testing

`storage-perf-tester/` is a self-contained, optional tool for comparing the
StorageClasses available in a cluster (fio + PostgreSQL/pgbench workloads,
IOPS/MB/s/latency, configurable ranking). It never touches the OSM stack
deployed by this repository; see `storage-perf-tester/README.md` for usage.

## Pelias-Parallelbetrieb

Pelias ergänzt Photon/Nominatim, ersetzt aber weder deren APIs noch qvadis automatisch.
Die vorhandene Photon-Schnittstelle `/api` ist **nicht** mit Pelias `/v1/*`
austauschbar. qvadis kann später einen eigenen Pelias-Provider für Vergleichstests
ergänzen; Routing bleibt bei Valhalla.

### Verbindliche OSM-Quelle und räumliche Grenzen

Pelias liest ausschließlich **PVC `planet`, `/planet/europa.osm.pbf`**, read-only.
Weder Nominatim-Export noch Country-Library-Datei
`/osm-temp/import/planet.osm.pbf` noch zusätzlicher OSM-Download werden verwendet.
Der Name `europa` ist keine Abdeckungsbeschreibung:

* `k8s/auto-update-geojson-config.yaml` enthält im Standardprofil eine größere
  europäische Polygonmaske; die bigmemory-Variante eine engere Maske um Deutschland.
* Beide konfigurieren zusätzlich eine Japan-Maske und `privat.osm`.
* `auto-update/build_karte.sh` ergänzt aus dem Planet abgeleitete Freizeitparkflächen
  und die Objekte aus `welt_filter.txt`, unter anderem ausgewählte Orte,
  Verwaltungsgrenzen, Gewässer und Autobahnen.

Maßgeblich bleiben die tatsächlich angewandten ConfigMaps, nicht diese Kurzbeschreibung.
Außerhalb der Vollauszugsflächen gibt es nur die gefilterten Objekte, keine
weltweite Hausadressabdeckung. Die Gebietsdefinition wird durch Pelias nicht verändert.
Private Inhalte der PBF gelangen ebenfalls in den Pelias-Index; Zugriff entsprechend
begrenzen.

Der aktualisierte Planet-Producer erzeugt `europa.next.osm.pbf`, berechnet SHA-256
und PBF-Header und veröffentlicht das fertige Ergebnis durch atomisches Umbenennen.
`europa.osm.pbf.meta.json` und `/status/pelias-input.json` enthalten denselben
Fingerprint, Größe, mtime und Veröffentlichungszeit. Gemeinsame `flock`-Sperren
schützen Veröffentlichung und Pelias-Lesephase; Sperrdateien niemals im Betrieb
löschen. Alte Auto-Update-Images müssen vor Nutzung dieses Protokolls neu gebaut
werden. Laufende alte Writer zuerst auslaufen lassen.

Es entsteht keine weitere Pelias-Snapshotkopie der ca. 15-GB-PBF. Der Producer benötigt
während der Erzeugung allerdings Platz für **alte und neue** Ergebnisdatei plus
seine ohnehin benötigten Extraktionsdateien. Ein Leser kann die Veröffentlichung
verzögern; das ist beabsichtigt und kein fehlgeschlagener Download.

### Erstinstallation und Einmalimport

Auf dem OSM-Host den absoluten Checkoutpfad setzen; StorageClasses in
`pelias-persistentvolumeclaims.yaml` vor dem ersten Apply an den Cluster anpassen.
Die Pelias-PVCs werden separat angelegt, bestehende PVCs nicht ersetzt.
Das Standardprofil mit `planet: ReadWriteOnce` setzt einen eindeutig gewählten
OSM-Worker (`osm=true`) voraus. Producer und alle Verbraucher dieses PVCs müssen
auf demselben Node laufen; bei mehreren geeigneten Nodes vor Installation eine
explizite gemeinsame Hostname-Affinität festlegen, nicht auf zufälliges Scheduling
vertrauen. Der Installer prüft das und erlaubt eine gezielte Pelias-Auswahl mit
`--pelias-node <hostname>` bzw. `PELIAS_NODE_HOSTNAME`; diese Auswahl muss zum
Node der vorhandenen Planet-Verbraucher passen. Das bigmemory-Planet-PVC ist im vorhandenen Profil RWX; auch dort
müssen Dateisperren auf der konkreten StorageClass nodeübergreifend funktionieren.

```bash
REPO=/home/runner/work/localOSM/localOSM  # auf dem Zielhost entsprechend setzen
bash "$REPO/scripts/build-pelias-images.sh"
# Bei k3s ohne eigene Registry: auf jeden berechtigten OSM-Worker importieren.
docker save localosm/pelias-runtime:2026-10-21 localosm/pelias-libpostal:2026-10-21 localosm/pelias-elasticsearch:7.17.27 |
  sudo k3s ctr images import -
bash "$REPO/scripts/deploy-osm.sh" --pelias-only --enable-pelias --rollout-management
# Alternativ Ressourcen-/Storageprofil:
# bash "$REPO/scripts/deploy-osm.sh" --bigmemory --pelias-only --enable-pelias --rollout-management
```

`--pelias-only` startet **keine** Import-/Cleanupjobs und skaliert die bestehenden
Such-/Routing-/Tile-Dienste nicht herunter. Die vorhandene Pelias-Policy bleibt bei
wiederholter Installation erhalten; `--enable-pelias` ist eine explizite Freigabe.
`--rollout-management` lädt die aktualisierte Dashboard-/Queue-Integration durch
Neustart **nur** von Status und Orchestrator. Dieser opt-in Pfad verweigert den
Neustart bei laufenden Builds/Jobs; dann erst den laufenden Auftrag abschließen
oder kontrolliert abbrechen. Ohne diesen Schalter werden die Management-Pods
nicht neu gestartet und bereits laufender alter Code bleibt bis zum Rollout aktiv.
Eine eigene Registry ist über `PELIAS_IMAGE_REGISTRY` beim Imagebau möglich;
die Imageverweise in den angewandten Manifesten müssen dann dazu passen.
Die genauen Quellen/Basisimage-Pins stehen in `pelias/images.lock.json`.
Buildzeit-CA, Registry-/Containerd-CA und das Laufzeit-CA-Bundle sind getrennte
Vertrauenskonfigurationen. Keinen TLS-Check abschalten.

Den Producer ebenfalls aus diesem Checkout mit Tag **`pelias-source-v1`** bauen
und in der bereits verwendeten Registry bereitstellen:

```bash
docker build -t 127.0.0.1:5000/planet-update:pelias-source-v1 "$REPO/auto-update"
# bigmemory verwendet stattdessen den in seiner Jobdatei angegebenen Registry-Pfad.
# Nach Bereitstellung des Images den CronJob aktualisieren:
kubectl apply --dry-run=client -f "$REPO/k8s/planet-update-job.yaml" -o json |
  python3 -c 'import json,sys; x=json.load(sys.stdin); print(json.dumps(next(v for v in x["items"] if v["kind"]=="CronJob")))' |
  kubectl apply -f -
```

Für bigmemory die entsprechende Jobdatei aus `"$REPO/k8s/bigmemory"` verwenden.
Das gesamte Multi-Dokument-Manifest würde zusätzlich sofort den manuellen Job
starten; deshalb oben nur den CronJob anwenden. Bereits laufende alte Writer
zuerst beenden/auslaufen lassen. Der erste manuelle Updateauftrag verwendet
anschließend ebenfalls die neue Jobvorlage.

Vor dem ersten Import die JSON-Policy in `osm-pelias-policy` prüfen:
Hilfsdaten-Länderauswahl, immutable Snapshotversion, Mindestdokumentzahlen,
vier datensatzgerechte Smoke-Queries und Aktivierungspolitik.
Landes-/Sprachfilter sind nicht mit räumlicher OSM-Vollabdeckung gleichzusetzen.
Native Pelias-Konfiguration steht ausschließlich in `osm-pelias-config`;
Lifecycle-Schalter gehören in die separate Policy, nicht als erfundene
Pelias-Importoptionen in `pelias.json`.

```bash
kubectl -n osm edit configmap osm-pelias-policy
kubectl apply -f /mnt/OSM/manifests/pelias-aux-job.yaml
kubectl -n osm logs -f job/pelias-aux
kubectl -n osm wait --for=condition=complete job/pelias-aux --timeout=6h
# Vorhandene veröffentlichte PBF verwenden, keine Country-Downloads:
bash "$REPO/scripts/run-import.sh" --pelias-only
kubectl -n osm logs -f job/pelias-import
```

Hilfsdownloads werden explizit vorbereitet und persistent versioniert, nicht bei
jedem OSM-Import erneut geladen. Für ein Hilfsdatenupdate eine **neue**
Snapshotversion konfigurieren, Aux-Job erneut ausführen und anschließend eine neue
OSM-/Suchgeneration bauen. Vor erneutem Ausführen eines abgeschlossenen Jobs
dessen Jobobjekt löschen, nicht seine PVC-Daten.

### Updates, Aktivierung, Rollback und Überwachung

`enabled=true` erlaubt Pelias-Aufträge. `auto_update=true` erlaubt dem
Orchestrator, erfolgreiche Veröffentlichungen aus `/status/pelias-input.json`
dedupliziert aufzunehmen; das gilt für manuelle Planet-Jobs und den wöchentlichen
CronJob, da beide dasselbe Generierungsscript verwenden. Das neue
Planet-Update-Image muss in **beiden** Jobvorlagen ausgerollt sein. Ein erfolgreicher
PBF-Job ist noch kein erfolgreicher Pelias-Build. Fehler werden nicht endlos neu
gestartet: Logs prüfen und gezielt erneut anfordern.

Standardmäßig bleibt `auto_promote=false`: fertige Generation prüfen und im
Dashboard gezielt aktivieren. Für unbeaufsichtigte Aktivierung zusätzlich
`auto_promote=true` freigeben; dieselben Feature-/Backendprüfungen gelten weiterhin.
Die Pelias-Aktivierung ist index-/generationsorientiert, **kein** Teil des
generischen Copy-Jobs `import-promotion`.

Die API jeder Generation verwendet ihren **konkreten** Index und ihre dazugehörigen
Hilfsdaten. Die Elasticsearch-Aliasänderung und der Kubernetes-Servicewechsel sind
zwei getrennte Operationen, keine verteilte atomare Transaktion. Ein Journal erlaubt
die Wiederaufnahme nach Unterbrechung; während der Endpoint-Propagation bleiben
alte und neue Antworten jeweils intern generationskonsistent. Der Alias `pelias`
ist dabei der Lifecycle-Zeiger, nicht ein Anlass, laufende Hilfsdaten umzuschreiben.

```bash
curl -f http://<node-ip>:30083/api/status
curl -f -X POST http://<node-ip>:30083/api/pelias/action \
  -H 'Content-Type: application/json' \
  -d '{"action":"promote","generation":"<geprüfte-generation>"}'
curl -f -X POST http://<node-ip>:30083/api/pelias/action \
  -H 'Content-Type: application/json' \
  -d '{"action":"rollback","generation":"<letzte-gute-generation>"}'
```

Die Dashboard-Abbruchfunktion beendet den laufenden Job; bereits aktiver Suchstand
bleibt erhalten. Nach Abbruch/Fehler gibt es keinen behaupteten Upsert-/Diff-Resume:
ein erneuter Pelias-Build erzeugt einen vollständigen neuen Index. Erfolgreiche
andere Schritte eines fortgesetzten Gesamtauftrags bleiben über die bestehende
Orchestrierung nachverfolgbar. Metadaten einschließlich Quellfingerprint,
Buildphase, Dokumentzahl und aktiver/letzter guter Generation stehen im Dashboard
und auf dem Status-PVC in `pelias.json`; die maßgebliche Lifecycle-Datei liegt auf
dem Pelias-Daten-PVC.

Cleanup ist absichtlich kein automatisch laufender destruktiver CronJob.
Nur eine explizite, nicht aktive/nicht letzte gute/nicht laufende Generation darf
gezielt über `pelias-cleanup` entfernt werden; Retention schützt zusätzlich
erfolgreiche Generationen. Keine Elasticsearch-Dateien von Hand löschen und keine
PVCs zur Fehlerbehebung entfernen.

```bash
GENERATION='<explizit-freigegebene-alte-generation>'
kubectl -n osm delete job pelias-cleanup --ignore-not-found
kubectl create --dry-run=client -f /mnt/OSM/manifests/pelias-cleanup-job.yaml -o json |
  python3 -c 'import json,sys; j=json.load(sys.stdin); c=j["spec"]["template"]["spec"]["containers"][0]; c["env"]=[e for e in c.get("env",[]) if e["name"]!="PELIAS_GENERATION"]+[{"name":"PELIAS_GENERATION","value":sys.argv[1]}]; print(json.dumps(j))' "$GENERATION" |
  kubectl apply -f -
kubectl -n osm logs -f job/pelias-cleanup
```

API-Beispiele:

```bash
curl -fG http://<node-ip>:30088/v1/search --data-urlencode 'text=Clemenshospital Münster'
curl -fG http://<node-ip>:30088/v1/autocomplete --data-urlencode 'text=Clemens'
curl -fG http://<node-ip>:30088/v1/search/structured \
  --data-urlencode 'address=Düesbergweg' --data-urlencode 'locality=Münster'
curl -fG http://<node-ip>:30088/v1/reverse \
  --data-urlencode 'point.lat=51.96' --data-urlencode 'point.lon=7.60'
kubectl -n osm get pods,jobs,pvc | grep -E 'NAME|pelias'
kubectl -n osm logs job/pelias-import --tail=200
kubectl -n osm logs pelias-elasticsearch-0 --tail=100
```

Diese Koordinaten sind nur ein grober Reverse-Testbereich, kein behaupteter
Adresspunkt. Erforderliche ConfigMap-/CA-Änderungen werden von laufenden
Node-Prozessen nicht automatisch neu eingelesen. Für geänderte Importoptionen,
Hilfsdaten, Straßen-/Interpolationsaufbereitung oder Schema eine neue Generation
bauen; für Images/Runtimekorrekturen bestehende Generationen nicht mit neuen
Hilfsdaten überschreiben. Upgrades zunächst als Kandidat prüfen, alte Images und
Generationen für Rollback behalten. Zum Deaktivieren die drei Policy-Schalter
abschalten und den externen API-Zugang schließen; keine PVCs löschen.

### Optionale eigene POIs und Aliasnamen

Es werden keine medizinischen Stammdaten mitgeliefert. Für eigene, rechtmäßig
verwendbare POIs unterstützt der gepinnte offizielle CSV-Importer beispielsweise
die Spalten:

```csv
id,source,layer,name,name_json,lat,lon
```

`name` ist der Hauptname; `name_json` enthält ein JSON-Array mit weiteren Namen,
CSV-konform mit verdoppelten Anführungszeichen escaped. Sprachvarianten heißen
z.B. `name_de` bzw. `name_json_de`, nicht `aliases` oder `alt_name`. Eindeutige IDs,
geeignete Quelle/Layer (z.B. `custom`/`venue`) und echte geprüfte Koordinaten
verwenden; keine geschätzten Krankenhausadressen erfinden.

Eine neue Aux-Version mit einer gepinnten `aux.files`-Datei unter
`csv/eigene-pois.csv` vorbereiten und in der nativen Konfiguration
`imports.csv.files: ["eigene-pois.csv"]` setzen. Der Build importiert diese Datei
vor der Kandidatenprüfung in den **neuen** Index. Eine leere Dateiliste aktiviert
diesen optionalen Import nicht. Die CSV-Felder sind kein Patch auf existierende
OSM-Dokumente: gleiche Kombination aus Quelle/Layer/ID kann ein Dokument ersetzen.
Erwartete eigene Dokumentzahl und Aliassuche zusätzlich als Smoke-Fälle prüfen,
da upstream fehlerhafte CSV-Zeilen teilweise nur protokolliert/überspringt.

### Kapazität, Betrieb und Relevanzabnahme

#### Upstream-Abgleich und Versionsgrenzen

Die Implementierung richtet sich nach den offiziellen Pelias-Repositories,
nicht nach einer fremden Compose-Kopie. Der Abgleich erfolgte insbesondere gegen
[`pelias/docker@3dfa07d`](https://github.com/pelias/docker/tree/3dfa07d580416edd7a27c2d4ff5976c8c1cc6ebc),
[`schema@41e4f40`](https://github.com/pelias/schema/tree/41e4f4009a82f336fc4beef998e35852c2616420),
[`api@1e08f14`](https://github.com/pelias/api/tree/1e08f147d6ed00ffaac6f8442fdfc42a7e56580d),
[`openstreetmap@9668925`](https://github.com/pelias/openstreetmap/tree/9668925dcbb0a447698cf04161da7ffd2773e87a)
und
[`interpolation@142c157`](https://github.com/pelias/interpolation/tree/142c1579bd93bac7ec29f4202a667a9fe26159d4).
`schema.indexName` steuert Schema und Import; **`api.indexName`** steuert die
Suchanfragen. Der Indexwechsel ist unsere Lifecycle-Steuerung, kein behaupteter
upstream Diff-/Löschungsimport. OSM-Änderungen werden vollständig neu aufgebaut.

Der offizielle Compose-Stand verwendet Elasticsearch **7.17.27** mit ICU-Plugin.
Das ist kein Nachweis für OpenSearch- oder Elasticsearch-8/9-Kompatibilität und
keine Zusage über den Security-Support dieses alten Elasticsearch-Zweigs.
Vor Internet-/Mehrmandantenbetrieb den Support- und Vulnerability-Status der
gesamten gepinnten Imagekette separat bewerten; ein Versionspin ersetzt keine
Sicherheitswartung. Backend ohne externe Veröffentlichung betreiben.

PIP lädt die ausgewählten WOF-SQLite-Polygone beim Start; ein erfundenes
separates PIP-Buildkommando gibt es hier nicht. Placeholder benötigt dagegen
Extraktion **und** Datenbankbuild. Interpolation benötigt **street.db und
address.db** einschließlich Vertexberechnung. Libpostal-Modell-Dateien sind
Bestandteil des gepinnten offiziellen Images, kein wiederholter OSM-Importdownload.

`pelias/streets.py` erzeugt die benötigten Straßenpolylinien aus **derselben PBF**:
Osmium exportiert Linien mit einem datenträgergestützten `sparse_file_array`-
Koordinatenindex; der Konverter schreibt benannte Highways im dokumentierten
Polyline6-/NUL-Format. Damit wird der upstream `pbf streets`-Wrapper vermieden,
der wegen speichergebundener Verarbeitung PBFs über 1 GB ablehnt. Der neue Weg
benötigt entsprechend zusätzlichen Scratchplatz und I/O, aber keinen zweiten
Straßendownload oder Valhalla-Neubau. Die große Eingabe bleibt clusterseitig zu
vermessen; der lokale Kleintest ist kein 15-GB-Lastnachweis.
Nicht auflösbare Randgeometrien werden wie beim normalen Osmium-Export ausgelassen;
die zusammengefasste Fehlerzahl steht im Joblog. Eine nicht lesbare PBF, ein
fehlgeschlagener Export oder null benannte Straßen brechen den Build ab.

15 GB Eingabe erlauben keine belastbare Zusage zu Indexgröße oder Importdauer.
Vor dem ersten Großimport auf den gewählten StorageClasses messen:

1. Freien Platz/Inodes, IOPS und Durchsatz auf `planet`, Pelias-Backend und
   Hilfsdaten-/Scratch-PVC prüfen; das vorhandene Storage-Performance-Werkzeug ist
   optional und berührt keine Produktionsdaten.
2. Gleichzeitig aktiven Index, neuen Index, aufbewahrten Rollbackindex,
   Elasticsearch-Segment-Merges/Translog sowie Low-/High-Disk-Watermarks einplanen.
3. Versionierten WOF-/Placeholder-/PIP-Cache, Interpolationsdaten je Generation
   und Import-Scratch zusätzlich messen. Downloads/Entpacken können zeitweise
   komprimierte und entpackte Daten benötigen.
4. Zunächst kleinen echten Import ausführen, dann Dokumentzahl,
   `_cat/indices?v&bytes=b`, `_cluster/health`, Pod-Maximal-RSS, CPU und Laufzeit
   protokollieren. Erst anschließend PVCs und Limits für den großen Ausschnitt
   freigeben. bigmemory ist ein Ressourcenprofil, keine Kapazitätsgarantie.

Backend und Hilfsdienste bleiben ClusterIP/intern. Nur die API ist über NodePort
30088 erreichbar; diesen Port per Host-/Netzwerk-Firewall auf Vergleichsclients
begrenzen. Für externen Betrieb stattdessen den API-Service hinter einen
Ingress mit TLS, Authentisierung, Rate-Limits und konfiguriertem Hostnamen stellen;
NodePort dann schließen bzw. den Service auf ClusterIP umstellen. Keine
Backend-Ports veröffentlichen. Ein ClusterIP allein isoliert nicht gegen andere
Pods; für nicht vertrauenswürdige Mandanten zusätzlich passende NetworkPolicies
für das verwendete CNI setzen.

Manuelle Relevanz-Regression (keine zugesagten Treffer, keine künstlichen Koordinaten):

| Eingabe | Zu prüfendes Verhalten |
|---|---|
| Domagkstraße 16, 48149 Münster | Laut Nutzer existiert Nr. 16 nicht: keinen exakten Hauspunkt behaupten; etwaige Interpolation als Schätzung anzeigen |
| Clemenshospital Münster / clemens hospital münster | POI, Schreibvarianten, Stadtbezug und Dubletten vergleichen |
| ev. Krankenhaus Hattingen / hattingen evan | Krankenhaus gegen Parkplatz-/Haltestellentreffer vergleichen; Adresse nicht erfinden |
| Hindenburgstraße 1, Ostfildern / Nellingen | Gemeinde und Ortsteil in Suchantwort und Hierarchie prüfen |
| Friedrichstraße 38, Ostfildern / Kemnat | Gemeindebezug, Hausnummer und Ortsranking prüfen |

Bei allen Vergleichen `/v1/search`, `/v1/autocomplete`, `/v1/search/structured`
und `/v1/reverse` getrennt prüfen. Exakte OSM-Adresspunkte, POI-Schwerpunkte,
Straßentreffer und interpolierte Positionen sind unterschiedliche Qualitäten;
eine erfolgreiche HTTP-Antwort allein belegt keine brauchbare Geokodierung.

### Reproduzierbare Prüfungen und Grenzen der Abnahme

Die Tests verwenden `unittest`; die Manifesttests benötigen zusätzlich PyYAML
(z.B. das Distributionspaket `python3-yaml`). Kein Planetdownload:

```bash
python3 -m unittest discover -s "$REPO/tests" -v
python3 -m unittest discover -s "$REPO/pelias" -p 'test_*.py' -v
bash -n "$REPO/scripts/deploy-osm.sh" "$REPO/scripts/import-orchestrator.sh" \
  "$REPO/scripts/run-import.sh" "$REPO/auto-update/build_karte.sh"
```

Die Straßentests erzeugen ein winziges neutrales OSM-XML/PBF im Tempverzeichnis,
testen echtes Osmium einschließlich unvollständiger Randreferenzen und enthalten
keine erfundenen medizinischen Einrichtungen. Zusätzlich wurde die offizielle
Pelias-OSM-Testregion **Queens Village** erfolgreich durch den Straßenkonverter
verarbeitet: 2.280.564 Byte, 1.248 benannte Straßenzeilen. Reproduktion ausschließlich
als Test, niemals die Produktions-PBF damit überschreiben:

```bash
mkdir -p /tmp/pelias-fixture
curl -fL https://raw.githubusercontent.com/pelias/openstreetmap/9668925dcbb0a447698cf04161da7ffd2773e87a/test/queens_village_ny.osm.pbf \
  -o /tmp/pelias-fixture/queens.osm.pbf
echo '06a8f23bc9e4a23431ce7d2a563be069aead24d690957e6cced89d5241fc5f4b  /tmp/pelias-fixture/queens.osm.pbf' | sha256sum -c -
python3 "$REPO/pelias/streets.py" /tmp/pelias-fixture/queens.osm.pbf \
  /tmp/pelias-fixture/streets.0sv --scratch /tmp/pelias-fixture/scratch
```

Quelle/Lizenz: offizielles `pelias/openstreetmap`-Testfixture am genannten Commit;
OSM-Daten © OpenStreetMap-Mitwirkende, ODbL. Das historische Fixture ist kein
aktueller Produktionsausschnitt und enthält unvollständige Randgeometrien.

Lokale Unit-/Manifesttests, reale Schemaerstellung, Libpostal-Parsing und
Straßenkonvertierung ersetzen **keinen** vollständigen 15-GB-Import, keine
Speicher-/Lastmessung und keinen Nachweis der genannten Relevanzfälle.
Ohne Zugriff auf den Nutzercluster/PVC wird hier keine erfolgreiche Installation
behauptet. Dort sind Imagebereitstellung, Hilfsdatenvorbereitung, echter Import,
Feature-/Reverse-Prüfung, Aktivierung und Rollback sowie Firewall-/Ingressabnahme
noch auszuführen.
