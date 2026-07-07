#!/bin/bash
#
# setup-osm.sh: Initial setup for localOSM deployment
#
# This script handles ALL pre-download and initialization tasks that must
# complete successfully before any services start. It runs during initial
# deployment setup to ensure all required resources are available.
#
# The script provides comprehensive error reporting and ABORTS on ANY failure
# so deployment issues are visible immediately rather than causing silent
# failures or degraded functionality.
#
# Called by: scripts/deploy-osm.sh
# Usage: setup-osm.sh
#   Environment variables:
#     DATA_DIR: Base data directory (default: /mnt/data/OSM)
#     VERBOSE: Enable verbose output (set to 1 for debug output)
#
# Exit codes:
#   0 = All setup tasks completed successfully
#   1 = One or more setup tasks failed

set -o pipefail

# Configuration
DATA_DIR="${DATA_DIR:-/mnt/data/OSM}"
TILESERVER_DIR="${DATA_DIR}/tileserver"
FONTS_DIR="${TILESERVER_DIR}/fonts"
FONTS_NAME="NotoSansRegular"
FONTS_PATH="${FONTS_DIR}/${FONTS_NAME}"
VERBOSE="${VERBOSE:-0}"

# Font download configuration
# Use @geops/mapbox-gl-fonts which has pre-built PBF fonts (not TTF source files)
FONTS_URL="https://registry.npmjs.org/@geops/mapbox-gl-fonts/-/mapbox-gl-fonts-3.0.2.tgz"
FONTS_MAX_RETRIES=3
FONTS_RETRY_DELAY=5

# Logging functions
log_info() {
    echo "[INFO] $*" >&2
}

log_success() {
    echo "[✓] $*" >&2
}

log_error() {
    echo "[✗] ERROR: $*" >&2
}

log_warn() {
    echo "[!] WARNING: $*" >&2
}

log_debug() {
    if [ "$VERBOSE" = "1" ]; then
        echo "[DEBUG] $*" >&2
    fi
}

# Report error and exit with failure
abort() {
    local msg="$1"
    local code="${2:-1}"
    
    echo ""
    echo "=========================================="
    log_error "$msg"
    echo "=========================================="
    echo ""
    exit "$code"
}

# Check if fonts are already installed
fonts_already_available() {
    if [ ! -d "$FONTS_PATH" ]; then
        return 1
    fi
    
    local font_count
    font_count=$(find "$FONTS_PATH" -name "*.pbf" 2>/dev/null | wc -l)
    
    if [ "$font_count" -gt 0 ]; then
        log_debug "Found $font_count existing font files in $FONTS_PATH"
        return 0
    fi
    
    return 1
}

# Download fonts with retries and validation
download_fonts() {
    log_info "========================================"
    log_info "Downloading MapLibre Fonts"
    log_info "========================================"
    log_info "Source: $FONTS_URL"
    log_info "Destination: $FONTS_PATH"
    log_info "Max retries: $FONTS_MAX_RETRIES"
    echo ""
    
    # Check if fonts are already available
    if fonts_already_available; then
        local font_count
        font_count=$(find "$FONTS_PATH" -name "*.pbf" 2>/dev/null | wc -l)
        log_success "Fonts already present ($font_count .pbf files)"
        return 0
    fi
    
    # Clean up any partial/corrupted fonts
    if [ -d "$FONTS_PATH" ]; then
        log_debug "Removing partial/corrupted fonts from $FONTS_PATH"
        rm -rf "$FONTS_PATH"
    fi
    
    local retry_count=0
    local download_success=0
    
    while [ $retry_count -lt $FONTS_MAX_RETRIES ] && [ $download_success -eq 0 ]; do
        retry_count=$((retry_count + 1))
        echo ""
        log_info "Download attempt $retry_count of $FONTS_MAX_RETRIES..."
        
        # Create temporary working directory
        local temp_dir
        temp_dir=$(mktemp -d) || abort "Failed to create temporary directory"
        trap "rm -rf $temp_dir" RETURN
        
        # Ensure wget and tar are available
        if ! command -v wget &> /dev/null || ! command -v tar &> /dev/null; then
            log_debug "wget/tar not found, attempting to install..."
            if command -v apt-get &> /dev/null; then
                apt-get update -qq 2>&1 | grep -v "^Get:" | grep -v "^Reading" || true
                apt-get install -y -qq wget tar 2>&1 | tail -1
            elif command -v apk &> /dev/null; then
                apk add --no-cache wget tar 2>&1 | tail -1
            else
                log_error "Cannot install wget/tar (no apt-get or apk available)"
                [ $retry_count -lt $FONTS_MAX_RETRIES ] && {
                    log_info "Retrying in ${FONTS_RETRY_DELAY}s..."
                    sleep "$FONTS_RETRY_DELAY"
                    continue
                } || abort "Failed to install wget/tar"
            fi
        fi
        
        # Download fonts archive
        log_info "  → Downloading fonts archive..."
        if ! wget -q --timeout=30 -O "$temp_dir/fonts.tar.gz" "$FONTS_URL" 2>&1; then
            log_error "  Download failed"
            [ $retry_count -lt $FONTS_MAX_RETRIES ] && {
                log_info "Retrying in ${FONTS_RETRY_DELAY}s..."
                sleep "$FONTS_RETRY_DELAY"
                continue
            } || abort "Font download failed after $FONTS_MAX_RETRIES attempts"
        fi
        
        local tar_size
        tar_size=$(du -h "$temp_dir/fonts.tar.gz" | cut -f1)
        log_success "  Downloaded ($tar_size)"
        
        # Extract fonts archive
        log_info "  → Extracting fonts..."
        # Extract the tar.gz package (NPM package format)
        if ! tar -xzf "$temp_dir/fonts.tar.gz" -C "$temp_dir/extract" 2>&1; then
            log_error "  Extraction failed"
            [ $retry_count -lt $FONTS_MAX_RETRIES ] && {
                log_info "Retrying in ${FONTS_RETRY_DELAY}s..."
                sleep "$FONTS_RETRY_DELAY"
                continue
            } || abort "Font extraction failed after $FONTS_MAX_RETRIES attempts"
        fi
        
        # Verify extraction and find the fonts directory
        # The NPM package extracts to "package/fonts/NotoSansRegular"
        local extracted_font_dir="$temp_dir/extract/package/fonts/NotoSansRegular"
        
        # Verify the directory exists and has .pbf files
        if [ ! -d "$extracted_font_dir" ]; then
            log_error "  Extracted font directory not found at $extracted_font_dir"
            log_debug "  Directory contents: $(ls -la "$temp_dir/extract/package/fonts/" 2>/dev/null || echo 'not found')"
            [ $retry_count -lt $FONTS_MAX_RETRIES ] && {
                log_info "Retrying in ${FONTS_RETRY_DELAY}s..."
                sleep "$FONTS_RETRY_DELAY"
                continue
            } || abort "Font extraction directory structure invalid after $FONTS_MAX_RETRIES attempts"
        fi
        
        # Verify that the directory contains .pbf files
        local pbf_count
        pbf_count=$(find "$extracted_font_dir" -name "*.pbf" 2>/dev/null | wc -l)
        if [ "$pbf_count" -eq 0 ]; then
            log_error "  No .pbf files found in extracted directory"
            [ $retry_count -lt $FONTS_MAX_RETRIES ] && {
                log_info "Retrying in ${FONTS_RETRY_DELAY}s..."
                sleep "$FONTS_RETRY_DELAY"
                continue
            } || abort "Font directory contains no .pbf files after $FONTS_MAX_RETRIES attempts"
        fi
        
        # Copy fonts to destination
        log_info "  → Installing fonts..."
        mkdir -p "$FONTS_PATH" || abort "Failed to create fonts destination directory"
        
        if ! cp -r "$extracted_font_dir"/* "$FONTS_PATH/" 2>&1; then
            log_error "  Installation failed"
            rm -rf "$FONTS_PATH"
            [ $retry_count -lt $FONTS_MAX_RETRIES ] && {
                log_info "Retrying in ${FONTS_RETRY_DELAY}s..."
                sleep "$FONTS_RETRY_DELAY"
                continue
            } || abort "Font installation failed after $FONTS_MAX_RETRIES attempts"
        fi
        
        # Validate installed fonts
        local font_count
        font_count=$(find "$FONTS_PATH" -name "*.pbf" 2>/dev/null | wc -l)
        
        if [ "$font_count" -gt 0 ]; then
            log_success "  Installed successfully ($font_count .pbf files)"
            download_success=1
        else
            log_error "  Font directory is empty after installation"
            rm -rf "$FONTS_PATH"
            [ $retry_count -lt $FONTS_MAX_RETRIES ] && {
                log_info "Retrying in ${FONTS_RETRY_DELAY}s..."
                sleep "$FONTS_RETRY_DELAY"
                continue
            } || abort "Font validation failed after $FONTS_MAX_RETRIES attempts (directory empty)"
        fi
    done
    
    if [ $download_success -eq 0 ]; then
        abort "Font download failed after $FONTS_MAX_RETRIES attempts"
    fi
    
    return 0
}

# Main setup flow
main() {
    echo ""
    log_info "=========================================="
    log_info "localOSM Initial Setup"
    log_info "=========================================="
    log_info "Data directory: $DATA_DIR"
    log_info "Fonts path: $FONTS_PATH"
    echo ""
    
    # Check if tileserver directory is accessible
    if [ ! -d "$TILESERVER_DIR" ]; then
        abort "Tileserver directory does not exist: $TILESERVER_DIR (should be created by deploy-osm.sh)"
    fi
    
    # Step 1: Download and validate fonts
    download_fonts || abort "Font setup failed"
    
    # Summary
    echo ""
    log_success "=========================================="
    log_success "Initial setup completed successfully"
    log_success "=========================================="
    echo ""
    echo "Ready for Kubernetes deployment:"
    echo "  ✓ Directory structure verified"
    echo "  ✓ Fonts downloaded and validated"
    echo ""
}

# Run main function
main "$@"
