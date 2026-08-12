#!/bin/bash
set -euo pipefail

# ==============================================================================
# Pipeline: Planet-OSM aktualisieren & Europa + Freizeitparks extrahieren
# ==============================================================================

# Eingabedateien & Pfade
PLANET_FILE="planet-latest.osm.pbf"
EUROPA_GEOJSON="conf/mein_europa.geojson"
PRIVAT_OSM="conf/privat.osm"
WELT_FILTER="conf/welt_filter.txt"

# Endprodukt
OUTPUT_PBF="europa_und_parks.osm.pbf"

# Temporäre Hilfsdateien
TEMP_PARK_PBF="hilf_park_grenzen.osm.pbf"
TEMP_PARK_GEOJSON="hilf_parks.geojson"
TEMP_HILF_WELT="hilf_welt.osm.pbf"
TEMP_MASKE_RAW="kombinierte_maske_raw.geojson"
TEMP_MASKE="kombinierte_maske.geojson"
TEMP_EXTRACT_PBF="hilf_extract_temp.osm.pbf"

# ------------------------------------------------------------------------------
# 0. Aufräum-Funktion (wird bei Skriptende ODER Fehler aufgerufen)
# ------------------------------------------------------------------------------
cleanup() {
  local exit_code=$?
  echo ""
  echo "--> Räume ALLE temporären Dateien auf..."
  rm -f "$TEMP_PARK_PBF" "$TEMP_PARK_GEOJSON" "$TEMP_MASKE_RAW" "$TEMP_MASKE" "$TEMP_EXTRACT_PBF" "$TEMP_HILF_WELT"
  
  if [ $exit_code -ne 0 ]; then
    echo "❌ Skript fehlgeschlagen (Exit-Code: $exit_code)!"
  else
    echo "🧹 Aufräumen abgeschlossen."
  fi
}
trap cleanup EXIT

# ------------------------------------------------------------------------------
# 1. Prüfungen auf Voraussetzungen
# ------------------------------------------------------------------------------
echo "Prüfe Voraussetzungen..."

for cmd in pyosmium-up-to-date osmium mapshaper python3; do
  if ! command -v "$cmd" &> /dev/null; then
    echo "❌ Fehler: '$cmd' ist nicht installiert oder nicht im PATH." >&2
    exit 1
  fi
done

for file in "$PLANET_FILE" "$EUROPA_GEOJSON" "$PRIVAT_OSM" "$WELT_FILTER"; do
  if [ ! -f "$file" ]; then
    echo "❌ Fehler: Benötigte Datei '$file' wurde nicht gefunden!" >&2
    exit 1
  fi
done

# ------------------------------------------------------------------------------
# 2. Hauptprozess
# ------------------------------------------------------------------------------
echo "----------------------------------------------------------------------"
echo "[1/7] Aktualisiere planet-latest.osm.pbf via pyosmium-up-to-date..."
pyosmium-up-to-date -vv "$PLANET_FILE"

echo "[2/7] Extrahiere optimierte Weltkarte"
osmium tags-filter "$PLANET_FILE" -e "$WELT_FILTER" -o "$TEMP_HILF_WELT" --overwrite

echo "[3/7] Filtere Freizeitpark-Grenzlinien aus der optimierten Weltkarte..."
#osmium tags-filter "$TEMP_HILF_WELT" wr/tourism=theme_park -o "$TEMP_PARK_PBF" --overwrite
osmium tags-filter "$TEMP_HILF_WELT" wr/tourism=theme_park wr/construction=theme_park -o "$TEMP_PARK_PBF" --overwrite

echo "[4/7] Konvertiere Freizeitparks in ein flaches GeoJSON..."
osmium export "$TEMP_PARK_PBF" --geometry-types=polygon -o "$TEMP_PARK_GEOJSON" --overwrite

echo "[5/7] vereine $EUROPA_GEOJSON und $TEMP_PARK_GEOJSON"
mapshaper -i "$EUROPA_GEOJSON" "conf/japan.geojson" "$TEMP_PARK_GEOJSON" combine-files \
  -drop fields=* \
  -merge-layers \
  -dissolve \
  -o "$TEMP_MASKE_RAW" force

# Python-Fix: Konvertierung in ein valides Einzel-Feature für Osmium
python3 -c "
import json, sys
try:
    with open('$TEMP_MASKE_RAW') as f:
        data = json.load(f)

    if 'geometries' in data:
        geom = data['geometries'][0]
    elif 'features' in data and len(data['features']) > 0:
        geom = data['features'][0]['geometry']
    else:
        geom = data

    out = {'type': 'Feature', 'properties': {}, 'geometry': geom}

    with open('$TEMP_MASKE', 'w') as f:
        json.dump(out, f)
except Exception as e:
    sys.stderr.write(f'Fehler bei der GeoJSON-Konvertierung: {e}\n')
    sys.exit(1)
"

echo "[6/7] Schneide Europa und alle Weltweit-Parks aus..."
osmium extract -p "$TEMP_MASKE" "$PLANET_FILE" -o "$TEMP_EXTRACT_PBF" --overwrite

echo "[7/7] Merge extrahierte Daten mit deiner privat.osm..."
osmium merge "$TEMP_EXTRACT_PBF" "$PRIVAT_OSM" "$TEMP_HILF_WELT" -o "$OUTPUT_PBF" --overwrite

echo "----------------------------------------------------------------------"
echo "✅ FERTIG! Die Datei '$OUTPUT_PBF' wurde erfolgreich erstellt."
echo "----------------------------------------------------------------------"
