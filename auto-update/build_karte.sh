#!/bin/bash
set -Eeuo pipefail

# ==============================================================================
# Pipeline: Planet-OSM aktualisieren & Europa + Freizeitparks extrahieren
# ==============================================================================

# Eingabedateien & Pfade (können per Umgebungsvariable überschrieben werden)
WORK_DIR="${WORK_DIR:-/work}"
CONF_DIR="${CONF_DIR:-/conf}"
STATUS_DIR="${STATUS_DIR:-/status}"

PLANET_FILE="${WORK_DIR}/planet-latest.osm.pbf"
PLANET_DOWNLOAD_URL="${PLANET_DOWNLOAD_URL:-https://planet.openstreetmap.org/pbf/planet-latest.osm.pbf}"

EUROPA_GEOJSON="${CONF_DIR}/mein_europa.geojson"
JAPAN_GEOJSON="${CONF_DIR}/japan.geojson"
PRIVAT_OSM="${CONF_DIR}/privat.osm"
WELT_FILTER="${CONF_DIR}/welt_filter.txt"

OUTPUT_PBF="${WORK_DIR}/europa_und_parks.osm.pbf"
STATUS_FILE="${STATUS_DIR}/planet-update.json"
LOCK_FILE="${WORK_DIR}/.planet-update.lock"

# Temporäre Hilfsdateien (alle im WORK_DIR)
TEMP_PARK_PBF="${WORK_DIR}/hilf_park_grenzen.osm.pbf"
TEMP_PARK_GEOJSON="${WORK_DIR}/hilf_parks.geojson"
TEMP_HILF_WELT="${WORK_DIR}/hilf_welt.osm.pbf"
TEMP_MASKE_RAW="${WORK_DIR}/kombinierte_maske_raw.geojson"
TEMP_MASKE="${WORK_DIR}/kombinierte_maske.geojson"
TEMP_EXTRACT_PBF="${WORK_DIR}/hilf_extract_temp.osm.pbf"

SCRIPT_START_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# ------------------------------------------------------------------------------
# Hilfsfunktionen
# ------------------------------------------------------------------------------
log() {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"
}

cleanup_file() {
  local f="$1"
  if [ -f "${f}" ]; then
    if rm -f "${f}"; then
      log "  Gelöscht: ${f}"
    else
      log "  WARNUNG: Konnte ${f} nicht löschen."
    fi
  fi
}

# ------------------------------------------------------------------------------
# 0. Aufräum-Funktion (wird bei Skriptende ODER Fehler aufgerufen)
# ------------------------------------------------------------------------------
cleanup() {
  local exit_code=$?
  log ""
  log "--> Räume ALLE temporären Dateien auf..."
  cleanup_file "${TEMP_PARK_PBF}"
  cleanup_file "${TEMP_PARK_GEOJSON}"
  cleanup_file "${TEMP_MASKE_RAW}"
  cleanup_file "${TEMP_MASKE}"
  cleanup_file "${TEMP_EXTRACT_PBF}"
  cleanup_file "${TEMP_HILF_WELT}"
  # Lockfile freigeben
  if [ -f "${LOCK_FILE}" ]; then
    rm -f "${LOCK_FILE}" && log "  Lockfile freigegeben." || log "  WARNUNG: Lockfile konnte nicht freigegeben werden."
  fi

  local now
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  mkdir -p "${STATUS_DIR}"

  if [ "${exit_code}" -ne 0 ]; then
    log "❌ Skript fehlgeschlagen (Exit-Code: ${exit_code})!"
    # Status-Datei: Fehler
    python3 -c "
import json, os
path = '${STATUS_FILE}'
try:
    with open(path) as fh:
        data = json.load(fh)
except Exception:
    data = {}
data['running'] = False
data['last_run_at'] = '${now}'
data['last_error'] = 'Script failed with exit code ${exit_code}'
output_path = '${OUTPUT_PBF}'
if os.path.exists(output_path):
    data['output_file_mtime'] = '$(python3 -c "import os,datetime; p='${OUTPUT_PBF}'; print(datetime.datetime.utcfromtimestamp(os.path.getmtime(p)).strftime('%Y-%m-%dT%H:%M:%SZ') if os.path.exists(p) else '')" 2>/dev/null || true)'
with open(path, 'w') as fh:
    json.dump(data, fh, indent=2)
" 2>/dev/null || true
  else
    log "🧹 Aufräumen abgeschlossen."
    # Status-Datei: Erfolg
    local output_mtime=""
    if [ -f "${OUTPUT_PBF}" ]; then
      output_mtime="$(python3 -c "import os,datetime; print(datetime.datetime.utcfromtimestamp(os.path.getmtime('${OUTPUT_PBF}')).strftime('%Y-%m-%dT%H:%M:%SZ'))" 2>/dev/null || true)"
    fi
    python3 -c "
import json
path = '${STATUS_FILE}'
try:
    with open(path) as fh:
        data = json.load(fh)
except Exception:
    data = {}
data['running'] = False
data['last_run_at'] = '${now}'
data['last_success_at'] = '${now}'
data['last_error'] = ''
data['output_file_mtime'] = '${output_mtime}'
with open(path, 'w') as fh:
    json.dump(data, fh, indent=2)
" 2>/dev/null || true
  fi
}
trap cleanup EXIT ERR INT TERM

# ------------------------------------------------------------------------------
# 1. Lockfile – verhindert doppelte gleichzeitige Ausführung
# ------------------------------------------------------------------------------
mkdir -p "${WORK_DIR}" "${STATUS_DIR}"
if [ -f "${LOCK_FILE}" ]; then
  log "❌ Fehler: Eine andere Instanz läuft bereits (Lockfile: ${LOCK_FILE}). Abbruch." >&2
  exit 1
fi
echo "$$" > "${LOCK_FILE}"
log "Lockfile gesetzt (PID $$)."

# Status-Datei: läuft jetzt
python3 -c "
import json
path = '${STATUS_FILE}'
try:
    with open(path) as fh:
        data = json.load(fh)
except Exception:
    data = {}
data['running'] = True
data['last_run_at'] = '${SCRIPT_START_TIME}'
with open(path, 'w') as fh:
    json.dump(data, fh, indent=2)
" 2>/dev/null || true

# ------------------------------------------------------------------------------
# 2. Prüfungen auf Voraussetzungen
# ------------------------------------------------------------------------------
log "Prüfe Voraussetzungen..."

for cmd in pyosmium-up-to-date osmium mapshaper python3; do
  if ! command -v "${cmd}" &> /dev/null; then
    log "❌ Fehler: '${cmd}' ist nicht installiert oder nicht im PATH." >&2
    exit 1
  fi
done

for file in "${EUROPA_GEOJSON}" "${PRIVAT_OSM}" "${WELT_FILTER}"; do
  if [ ! -f "${file}" ]; then
    log "❌ Fehler: Benötigte Konfigurationsdatei '${file}' wurde nicht gefunden!" >&2
    exit 1
  fi
done

# ------------------------------------------------------------------------------
# 3. Planet-Datei herunterladen, falls nicht vorhanden
# ------------------------------------------------------------------------------
if [ ! -f "${PLANET_FILE}" ]; then
  log "[0/7] planet-latest.osm.pbf nicht gefunden – starte Erstdownload von ${PLANET_DOWNLOAD_URL} ..."
  log "      HINWEIS: Das kann mehrere Stunden dauern (ca. 75 GB)."
  PLANET_PART="${PLANET_FILE}.part"
  for attempt in 1 2 3; do
    log "      Download-Versuch ${attempt}/3 ..."
    if wget --progress=dot:giga -c -O "${PLANET_PART}" "${PLANET_DOWNLOAD_URL}"; then
      mv "${PLANET_PART}" "${PLANET_FILE}"
      log "      ✅ Planet-Datei heruntergeladen."
      break
    else
      log "      ⚠️ Download-Versuch ${attempt} fehlgeschlagen."
      if [ "${attempt}" -eq 3 ]; then
        rm -f "${PLANET_PART}"
        log "❌ Planet-Datei konnte nach 3 Versuchen nicht heruntergeladen werden." >&2
        exit 1
      fi
      sleep 30
    fi
  done
fi

# ------------------------------------------------------------------------------
# 4. Hauptprozess
# ------------------------------------------------------------------------------
log "----------------------------------------------------------------------"
log "[1/7] Aktualisiere planet-latest.osm.pbf via pyosmium-up-to-date..."
pyosmium-up-to-date -vv "${PLANET_FILE}"

log "[2/7] Extrahiere optimierte Weltkarte"
osmium tags-filter "${PLANET_FILE}" -e "${WELT_FILTER}" -o "${TEMP_HILF_WELT}" --overwrite

log "[3/7] Filtere Freizeitpark-Grenzlinien aus der optimierten Weltkarte..."
osmium tags-filter "${TEMP_HILF_WELT}" wr/tourism=theme_park wr/construction=theme_park -o "${TEMP_PARK_PBF}" --overwrite

log "[4/7] Konvertiere Freizeitparks in ein flaches GeoJSON..."
osmium export "${TEMP_PARK_PBF}" --geometry-types=polygon -o "${TEMP_PARK_GEOJSON}" --overwrite

log "[5/7] Vereine ${EUROPA_GEOJSON}, ${JAPAN_GEOJSON} und ${TEMP_PARK_GEOJSON}..."
mapshaper -i "${EUROPA_GEOJSON}" "${JAPAN_GEOJSON}" "${TEMP_PARK_GEOJSON}" combine-files \
  -drop fields=* \
  -merge-layers \
  -dissolve \
  -o "${TEMP_MASKE_RAW}" force

# Python-Fix: Konvertierung in ein valides Einzel-Feature für Osmium
python3 -c "
import json, sys
try:
    with open('${TEMP_MASKE_RAW}') as f:
        data = json.load(f)

    if 'geometries' in data:
        geom = data['geometries'][0]
    elif 'features' in data and len(data['features']) > 0:
        geom = data['features'][0]['geometry']
    else:
        geom = data

    out = {'type': 'Feature', 'properties': {}, 'geometry': geom}

    with open('${TEMP_MASKE}', 'w') as f:
        json.dump(out, f)
except Exception as e:
    sys.stderr.write(f'Fehler bei der GeoJSON-Konvertierung: {e}\n')
    sys.exit(1)
"

log "[6/7] Schneide Europa und alle Weltweit-Parks aus..."
osmium extract -p "${TEMP_MASKE}" "${PLANET_FILE}" -o "${TEMP_EXTRACT_PBF}" --overwrite

log "[7/7] Merge extrahierte Daten mit deiner privat.osm..."
osmium merge "${TEMP_EXTRACT_PBF}" "${PRIVAT_OSM}" "${TEMP_HILF_WELT}" -o "${OUTPUT_PBF}" --overwrite

log "----------------------------------------------------------------------"
log "✅ FERTIG! Die Datei '${OUTPUT_PBF}' wurde erfolgreich erstellt."
log "----------------------------------------------------------------------"
