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
FONTS_URL="https://github.com/openmaptiles/fonts/archive/refs/heads/master.zip"
FONTS_MAX_RETRIES=3
FONTS_RETRY_DELAY=5
MIN_FONT_FILES=5  # Minimum number of font files required for successful installation
MIN_VALID_PBF_SIZE=10  # Minimum file size (bytes) for valid PBF files (excludes empty stubs)

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

# Get file size in bytes (works with both BSD and GNU stat)
get_file_size() {
    local file="$1"
    stat -f%z "$file" 2>/dev/null || stat -c%s "$file" 2>/dev/null
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
    font_count=$(find "$FONTS_PATH" -type f \( -name "*.ttf" -o -name "*.otf" \) 2>/dev/null | wc -l)
    
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
        font_count=$(find "$FONTS_PATH" -type f \( -name "*.ttf" -o -name "*.otf" \) 2>/dev/null | wc -l)
        log_success "Fonts already present ($font_count font files)"
        return 0
    fi
    
    # Clean up any partial/corrupted fonts
    if [ -d "$FONTS_PATH" ]; then
        log_debug "Removing partial/corrupted fonts from $FONTS_PATH"
        rm -rf "$FONTS_PATH"
    fi
    
    local retry_count=0
    local download_success=0
    local temp_dir=""
    
    # Set trap to clean up temp directory on exit
    trap "[ -n \"$temp_dir\" ] && rm -rf \"$temp_dir\"" EXIT
    
    while [ $retry_count -lt $FONTS_MAX_RETRIES ] && [ $download_success -eq 0 ]; do
        retry_count=$((retry_count + 1))
        echo ""
        log_info "Download attempt $retry_count of $FONTS_MAX_RETRIES..."
        
        # Create temporary working directory
        temp_dir=$(mktemp -d) || abort "Failed to create temporary directory"
        
        # Ensure wget and unzip are available
        if ! command -v wget &> /dev/null || ! command -v unzip &> /dev/null; then
            log_debug "wget/unzip not found, attempting to install..."
            if command -v apt-get &> /dev/null; then
                apt-get update -qq 2>&1 | grep -v "^Get:" | grep -v "^Reading" || true
                apt-get install -y -qq wget unzip 2>&1 | tail -1
            elif command -v apk &> /dev/null; then
                apk add --no-cache wget unzip 2>&1 | tail -1
            else
                log_error "Cannot install wget/unzip (no apt-get or apk available)"
                [ $retry_count -lt $FONTS_MAX_RETRIES ] && {
                    log_info "Retrying in ${FONTS_RETRY_DELAY}s..."
                    sleep "$FONTS_RETRY_DELAY"
                    continue
                } || abort "Failed to install wget/unzip"
            fi
        fi
        
        # Download fonts archive
        log_info "  → Downloading fonts archive..."
        if ! wget -q --timeout=30 -O "$temp_dir/fonts.zip" "$FONTS_URL" 2>&1; then
            log_error "  Download failed"
            [ $retry_count -lt $FONTS_MAX_RETRIES ] && {
                log_info "Retrying in ${FONTS_RETRY_DELAY}s..."
                sleep "$FONTS_RETRY_DELAY"
                continue
            } || abort "Font download failed after $FONTS_MAX_RETRIES attempts"
        fi
        
        local zip_size
        zip_size=$(du -h "$temp_dir/fonts.zip" | cut -f1)
        log_success "  Downloaded ($zip_size)"
        
        # Extract fonts archive - extract the entire noto-sans directory
        log_info "  → Extracting fonts..."
        if ! unzip -q "$temp_dir/fonts.zip" \
            "fonts-master/noto-sans/*" \
            -d "$temp_dir/extract" >/dev/null 2>&1; then
            log_error "  Extraction failed"
            [ $retry_count -lt $FONTS_MAX_RETRIES ] && {
                log_info "Retrying in ${FONTS_RETRY_DELAY}s..."
                sleep "$FONTS_RETRY_DELAY"
                continue
            } || abort "Font extraction failed after $FONTS_MAX_RETRIES attempts"
        fi
        
        # Verify extraction - the TTF files should be directly in fonts-master/noto-sans/
        local extracted_source_dir="$temp_dir/extract/fonts-master/noto-sans"
        if [ ! -d "$extracted_source_dir" ]; then
            log_error "  Extracted source directory not found"
            [ $retry_count -lt $FONTS_MAX_RETRIES ] && {
                log_info "Retrying in ${FONTS_RETRY_DELAY}s..."
                sleep "$FONTS_RETRY_DELAY"
                continue
            } || abort "Font extraction directory structure invalid after $FONTS_MAX_RETRIES attempts"
        fi
        
        # Verify that the directory contains font files (TTF/OTF)
        local font_count
        font_count=$(find "$extracted_source_dir" -type f \( -name "*.ttf" -o -name "*.otf" \) 2>/dev/null | wc -l)
        if [ "$font_count" -lt "$MIN_FONT_FILES" ]; then
            log_error "  Insufficient font files found ($font_count files, minimum: $MIN_FONT_FILES)"
            [ $retry_count -lt $FONTS_MAX_RETRIES ] && {
                log_info "Retrying in ${FONTS_RETRY_DELAY}s..."
                sleep "$FONTS_RETRY_DELAY"
                continue
            } || abort "Font directory has insufficient font files after $FONTS_MAX_RETRIES attempts"
        fi
        
        # Copy fonts to destination
        log_info "  → Installing fonts..."
        mkdir -p "$FONTS_PATH" || abort "Failed to create fonts destination directory"
        
        # Copy the TTF/OTF font files to the destination using find for robustness
        # This ensures proper handling regardless of which file types are present
        if ! find "$extracted_source_dir" -type f \( -name "*.ttf" -o -name "*.otf" \) -exec cp {} "$FONTS_PATH/" \; 2>/dev/null; then
            log_error "  Installation failed"
            rm -rf "$FONTS_PATH"
            [ $retry_count -lt $FONTS_MAX_RETRIES ] && {
                log_info "Retrying in ${FONTS_RETRY_DELAY}s..."
                sleep "$FONTS_RETRY_DELAY"
                continue
            } || abort "Font installation failed after $FONTS_MAX_RETRIES attempts"
        fi
        
        # Validate installed fonts
        local installed_font_count
        installed_font_count=$(find "$FONTS_PATH" -type f \( -name "*.ttf" -o -name "*.otf" \) 2>/dev/null | wc -l)
        
        if [ "$installed_font_count" -gt 0 ]; then
            log_success "  Installed successfully ($installed_font_count font files)"
            download_success=1
        else
            log_error "  Font installation verification failed (no files found)"
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

# Generate glyph PBF files from TTF fonts using fontnik
# fontnik is maintained by Mapbox and provides the build-glyphs CLI tool
generate_glyph_pbf_files() {
    log_info "Generating glyph PBF files from TTF fonts..."
    
    local pbf_dir="${FONTS_DIR}/NotoSansRegular"
    
    # Check if we already have valid PBF files
    if [ -d "$pbf_dir" ]; then
        local pbf_count=$(find "$pbf_dir" -name "*.pbf" -type f 2>/dev/null | wc -l)
        if [ "$pbf_count" -gt 0 ]; then
            # Validate that PBF files are substantial (not empty stubs)
            local valid_pbf_count=0
            while IFS= read -r pbf_file; do
                if [ -f "$pbf_file" ]; then
                    local file_size=$(get_file_size "$pbf_file")
                    if [ "$file_size" -gt "$MIN_VALID_PBF_SIZE" ]; then
                        valid_pbf_count=$((valid_pbf_count + 1))
                    fi
                fi
            done < <(find "$pbf_dir" -name "*.pbf" -type f)
            
            if [ "$valid_pbf_count" -gt 0 ]; then
                log_info "  Valid PBF glyph files found ($valid_pbf_count files)"
                return 0
            fi
        fi
    fi
    
    # Check if we have TTF/OTF files
    local ttf_count=$(find "$FONTS_PATH" -type f \( -name "*.ttf" -o -name "*.otf" \) 2>/dev/null | wc -l)
    if [ "$ttf_count" -eq 0 ]; then
        log_error "No TTF/OTF font files found to generate glyphs from"
        return 1
    fi
    
    # Ensure Node.js is available for running fontnik
    if ! command -v node &> /dev/null; then
        log_info "  Installing Node.js..."
        if command -v apt-get &> /dev/null; then
            apt-get update -qq 2>&1 | grep -v "^Get:" | grep -v "^Reading" || true
            apt-get install -y -qq nodejs npm 2>&1 | tail -1
        elif command -v apk &> /dev/null; then
            apk add --no-cache nodejs npm 2>&1 | tail -1
        else
            log_error "Cannot install Node.js (no apt-get or apk available)"
            return 1
        fi
    fi
    
    # Ensure npm is available
    if ! command -v npm &> /dev/null; then
        log_error "npm is not available in PATH"
        return 1
    fi
    
    # Install fontnik globally if not already installed
    if ! command -v build-glyphs &> /dev/null; then
        log_info "  Installing fontnik (Mapbox's PBF glyph generator)..."
        if ! npm install -g fontnik@0.7.7 2>&1 | tail -3; then
            log_error "fontnik installation failed"
            return 1
        fi
    fi
    
    # Verify fontnik installation (need to also clear shell command cache)
    hash -r 2>/dev/null || true  # Clear shell command cache (bash/dash)
    if ! command -v build-glyphs &> /dev/null; then
        log_error "fontnik installation failed or build-glyphs not available in PATH"
        return 1
    fi
    
    log_info "  Using fontnik build-glyphs to generate PBF files..."
    
    # Create output directory
    mkdir -p "$pbf_dir" || {
        log_error "Failed to create PBF directory"
        return 1
    }
    
    # Get the first TTF or OTF file
    local ttf_file=$(find "$FONTS_PATH" -name "*.ttf" | head -1)
    if [ -z "$ttf_file" ]; then
        # Try OTF as fallback
        ttf_file=$(find "$FONTS_PATH" -name "*.otf" | head -1)
    fi
    if [ -z "$ttf_file" ]; then
        log_error "No TTF or OTF file found for glyph generation"
        return 1
    fi
    
    log_debug "Using font file: $ttf_file"
    
    # Generate PBF glyphs using fontnik build-glyphs
    # Syntax: build-glyphs <fontstack path> <output dir>
    local glyphs_error
    glyphs_error=$(build-glyphs "$ttf_file" "$pbf_dir" 2>&1)
    if [ $? -ne 0 ]; then
        log_error "fontnik build-glyphs generation failed: $glyphs_error"
        return 1
    fi
    
    # Verify that PBF files were generated
    local generated_count
    generated_count=$(find "$pbf_dir" -name "*.pbf" -type f 2>/dev/null | wc -l)
    
    if [ "$generated_count" -eq 0 ]; then
        log_error "Failed to generate any PBF files from TTF font"
        return 1
    fi
    
    log_success "Generated $generated_count glyph PBF files using fontnik build-glyphs"
    
    return 0
}

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
    
    # Step 2: Generate glyph PBF files from TTF fonts using fontnik
    generate_glyph_pbf_files || abort "Glyph PBF generation failed - map will not display text labels"
    
    # Summary
    echo ""
    log_success "=========================================="
    log_success "Initial setup completed successfully"
    log_success "=========================================="
    echo ""
    echo "Ready for Kubernetes deployment:"
    echo "  ✓ Directory structure verified"
    echo "  ✓ Fonts downloaded and validated"
    echo "  ✓ Glyph PBF files generated"
    echo ""
}

# Run main function
main "$@"
