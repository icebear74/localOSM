#!/bin/bash
#
# download-fonts.sh: Download and validate OpenMapTiles Noto Sans Regular fonts
# for MapLibre GL text rendering. This script should be run during deployment
# to ensure fonts are available before TileServer-GL starts.
#
# Usage: download-fonts.sh [FONT_DEST_PATH]
#   FONT_DEST_PATH: Directory to store fonts (default: /mnt/data/OSM/tileserver/fonts)
#
# The script provides detailed error output during deployment so font download
# issues are visible immediately, rather than causing silent rendering failures.

set -e

FONT_DEST="${1:-/mnt/data/OSM/tileserver/fonts}"
FONT_NAME="NotoSansRegular"
FONT_PATH="$FONT_DEST/$FONT_NAME"
DOWNLOAD_URL="https://github.com/openmaptiles/fonts/archive/refs/heads/master.zip"
MAX_RETRIES=3
RETRY_DELAY=5

echo "=========================================="
echo "MapLibre Font Downloader"
echo "=========================================="
echo "Destination: $FONT_PATH"
echo "Source: $DOWNLOAD_URL"
echo "Max retries: $MAX_RETRIES"
echo ""

# Ensure font destination directory exists
mkdir -p "$FONT_DEST"
echo "[✓] Font destination directory created: $FONT_DEST"

# Check if fonts already exist and are valid
if [ -d "$FONT_PATH" ]; then
    FONT_COUNT=$(find "$FONT_PATH" -name "*.pbf" 2>/dev/null | wc -l)
    if [ "$FONT_COUNT" -gt 0 ]; then
        echo "[✓] Fonts already present ($FONT_COUNT .pbf files found)"
        echo "    Skipping download (fonts are cached on host)"
        exit 0
    else
        echo "[!] Font directory exists but is empty or corrupted"
        echo "    Removing and re-downloading..."
        rm -rf "$FONT_PATH"
    fi
fi

# Attempt to download and extract fonts with retries
DOWNLOAD_SUCCESS=0
RETRY_COUNT=0

while [ $RETRY_COUNT -lt $MAX_RETRIES ] && [ $DOWNLOAD_SUCCESS -eq 0 ]; do
    RETRY_COUNT=$((RETRY_COUNT + 1))
    echo ""
    echo "Download attempt $RETRY_COUNT of $MAX_RETRIES..."
    
    # Create temporary working directory
    TEMP_DIR=$(mktemp -d)
    trap "rm -rf $TEMP_DIR" EXIT
    
    # Check if wget is available
    if ! command -v wget &> /dev/null; then
        echo "[✗] wget not found. Attempting to install..."
        if command -v apt-get &> /dev/null; then
            apt-get update -qq
            apt-get install -y -qq wget unzip 2>&1 | grep -v "^Get:" | grep -v "^Reading" || true
        elif command -v apk &> /dev/null; then
            apk add --no-cache wget unzip
        else
            echo "[✗] FAILED: Cannot install wget (no apt-get or apk available)"
            [ $RETRY_COUNT -lt $MAX_RETRIES ] && echo "    Retrying in ${RETRY_DELAY}s..." && sleep $RETRY_DELAY && continue || exit 1
        fi
    fi
    
    # Download fonts archive
    echo "  → Downloading from $DOWNLOAD_URL..."
    if ! wget -q --timeout=30 -O "$TEMP_DIR/fonts.zip" "$DOWNLOAD_URL" 2>&1; then
        echo "[✗] Download failed"
        [ $RETRY_COUNT -lt $MAX_RETRIES ] && echo "    Retrying in ${RETRY_DELAY}s..." && sleep $RETRY_DELAY && continue || exit 1
    fi
    echo "  ✓ Downloaded successfully ($(du -h $TEMP_DIR/fonts.zip | cut -f1))"
    
    # Extract fonts archive
    echo "  → Extracting fonts..."
    if ! unzip -q "$TEMP_DIR/fonts.zip" \
        "fonts-master/noto-sans/Noto Sans Regular/*" \
        -d "$TEMP_DIR/extract" 2>&1; then
        echo "[✗] Extraction failed"
        [ $RETRY_COUNT -lt $MAX_RETRIES ] && echo "    Retrying in ${RETRY_DELAY}s..." && sleep $RETRY_DELAY && continue || exit 1
    fi
    
    # Verify extraction and copy to destination
    echo "  → Validating and installing fonts..."
    EXTRACTED_FONT_DIR="$TEMP_DIR/extract/fonts-master/noto-sans/Noto Sans Regular"
    
    if [ ! -d "$EXTRACTED_FONT_DIR" ]; then
        echo "[✗] Extracted font directory not found"
        [ $RETRY_COUNT -lt $MAX_RETRIES ] && echo "    Retrying in ${RETRY_DELAY}s..." && sleep $RETRY_DELAY && continue || exit 1
    fi
    
    # Copy fonts to destination
    mkdir -p "$FONT_PATH"
    if ! cp -r "$EXTRACTED_FONT_DIR"/* "$FONT_PATH/" 2>&1; then
        echo "[✗] Failed to copy fonts to destination"
        [ $RETRY_COUNT -lt $MAX_RETRIES ] && echo "    Retrying in ${RETRY_DELAY}s..." && sleep $RETRY_DELAY && continue || exit 1
    fi
    
    # Validate installed fonts
    FONT_COUNT=$(find "$FONT_PATH" -name "*.pbf" 2>/dev/null | wc -l)
    if [ "$FONT_COUNT" -gt 0 ]; then
        echo "  ✓ Fonts installed successfully ($FONT_COUNT .pbf files)"
        DOWNLOAD_SUCCESS=1
    else
        echo "[✗] Font directory is empty after installation"
        rm -rf "$FONT_PATH"
        [ $RETRY_COUNT -lt $MAX_RETRIES ] && echo "    Retrying in ${RETRY_DELAY}s..." && sleep $RETRY_DELAY && continue || exit 1
    fi
done

if [ $DOWNLOAD_SUCCESS -eq 0 ]; then
    echo ""
    echo "=========================================="
    echo "[✗] FONT DOWNLOAD FAILED"
    echo "=========================================="
    echo ""
    echo "ERROR: Unable to download fonts after $MAX_RETRIES attempts."
    echo ""
    echo "IMPACT:"
    echo "  - Map will render WITHOUT text labels (roads, cities, etc)"
    echo "  - Map features (roads, water, buildings) will still be visible"
    echo "  - Routing and navigation will work normally"
    echo ""
    echo "RESOLUTION:"
    echo "  1. Check internet connectivity and firewall rules"
    echo "  2. Manually download fonts from:"
    echo "     $DOWNLOAD_URL"
    echo "  3. Extract to: $FONT_PATH/NotoSansRegular/"
    echo "  4. Restart TileServer-GL pod"
    echo ""
    exit 1
fi

echo ""
echo "=========================================="
echo "[✓] Fonts successfully installed"
echo "=========================================="
echo "Location: $FONT_PATH"
echo "Files: $FONT_COUNT .pbf glyph files"
echo ""
echo "Map will render with full text labels for:"
echo "  - Water names, roads (minor/secondary/primary)"
echo "  - Places (country, state, city, town, village, suburb, hamlet)"
echo ""
