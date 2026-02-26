#!/bin/bash

# ============================================================
# WCB Firmware Build Script
# Produces bin files for all WCB hardware versions
#
# Usage: run from anywhere, script locates itself
#   ./build.sh
#   or
#   Code/bin/build.sh
#
# Requirements:
#   - Arduino CLI installed and in PATH
#   - ESP32 platform installed at v3.3.4
#   - EspSoftwareSerial and Adafruit NeoPixel libraries installed
#
# Output:
#   Code/bin/WCB_<version>_<branch>_ESP32.bin    (v1.0, v2.1, v2.3, v2.4)
#   Code/bin/WCB_<version>_<branch>_ESP32S3.bin  (v3.1, v3.2)
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKETCH_PATH="$SCRIPT_DIR/../WCB"
OUTPUT_DIR="$SCRIPT_DIR"
VERSION=$(grep 'String SoftwareVersion =' "$SKETCH_PATH/WCB.ino" | head -1 | grep -o '"[^"]*"' | head -1 | tr -d '"')
BRANCH=$(git -C "$SKETCH_PATH" rev-parse --abbrev-ref HEAD)

echo ""
echo "========================================"
echo " WCB Firmware Build"
echo " Version : $VERSION"
echo " Branch  : $BRANCH"
echo " Sketch  : $SKETCH_PATH"
echo " Output  : $OUTPUT_DIR"
echo "========================================"

mkdir -p "$OUTPUT_DIR"

PASS=0
FAIL=0

# ----------------------------------------
# Build 1 - ESP32 (v1.0, v2.1, v2.3, v2.4)
# ----------------------------------------
echo ""
echo "Building ESP32 binary (v1.0, v2.1, v2.3, v2.4)..."

arduino-cli compile \
    --fqbn esp32:esp32:esp32:PartitionScheme=default \
    --output-dir "/tmp/wcb_esp32" \
    "$SKETCH_PATH"

if [ $? -eq 0 ]; then
    OUTFILE="$OUTPUT_DIR/WCB_${VERSION}_${BRANCH}_ESP32.bin"
    cp "/tmp/wcb_esp32/WCB.ino.bin" "$OUTFILE"
    echo "✓ Built: $(basename $OUTFILE)"
    PASS=$((PASS + 1))
else
    echo "✗ Failed: ESP32 build"
    FAIL=$((FAIL + 1))
fi

rm -rf "/tmp/wcb_esp32"

# ----------------------------------------
# Build 2 - ESP32-S3 (v3.1, v3.2)
# ----------------------------------------
echo ""
echo "Building ESP32-S3 binary (v3.1, v3.2)..."

arduino-cli compile \
    --fqbn esp32:esp32:esp32s3:PartitionScheme=default \
    --output-dir "/tmp/wcb_s3" \
    "$SKETCH_PATH"

if [ $? -eq 0 ]; then
    OUTFILE="$OUTPUT_DIR/WCB_${VERSION}_${BRANCH}_ESP32S3.bin"
    cp "/tmp/wcb_s3/WCB.ino.bin" "$OUTFILE"
    echo "✓ Built: $(basename $OUTFILE)"
    PASS=$((PASS + 1))
else
    echo "✗ Failed: ESP32-S3 build"
    FAIL=$((FAIL + 1))
fi

rm -rf "/tmp/wcb_s3"

# ----------------------------------------
echo ""
echo "========================================"
echo " Build complete: $PASS passed, $FAIL failed"
echo " Binaries:"
ls -lh "$OUTPUT_DIR/"*.bin 2>/dev/null || echo " No bin files found"
echo "========================================"