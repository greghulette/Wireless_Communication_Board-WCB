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
#   Wizard/bin/WCB_ESP32.bin                     (stable name for wizard auto-flash)
#   Wizard/bin/WCB_ESP32S3.bin                   (stable name for wizard auto-flash)
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKETCH_PATH="$SCRIPT_DIR/../WCB"
OUTPUT_DIR="$SCRIPT_DIR"
WIZARD_BIN="$SCRIPT_DIR/../../Wizard/bin"
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
mkdir -p "$WIZARD_BIN"

PASS=0
FAIL=0
rm -f "$OUTPUT_DIR"/*.bin

# ----------------------------------------
# Build 1 - ESP32 (v1.0, v2.1, v2.3, v2.4)
# ----------------------------------------
echo ""
echo "Building ESP32 binary (v1.0, v2.1, v2.3, v2.4)..."

arduino-cli compile \
    --fqbn esp32:esp32:esp32:PartitionScheme=min_spiffs \
    --output-dir "/tmp/wcb_esp32" \
    "$SKETCH_PATH"

if [ $? -eq 0 ]; then
    # PartitionScheme=min_spiffs: 2x ~1.9MB OTA app slots + 128KB spiffs.
    # The WCB uses no filesystem, so the default scheme's ~1.4MB SPIFFS was
    # dead space capping the app at 1.25MB. min_spiffs keeps OTA (2 slots)
    # and raises the app ceiling to ~1.9MB. NVS (0x9000/0x5000) and otadata
    # (0xe000) offsets are IDENTICAL to the old default scheme, so saved
    # config survives the transition as long as NVS is not erased.
    #
    # NOTE: switching schemes changes the partition TABLE. Boards still on
    # the old table MUST be full-flashed (boot+part+app) once — an app-only
    # update would overrun the old 1.25MB slot. The flasher detects a table
    # mismatch and forces a full (NVS-preserving) flash automatically.
    #
    # Three separate files flashed at their correct addresses:
    #   bootloader → 0x1000   (recovers blank/bricked boards)
    #   partitions → 0x8000
    #   app        → 0x10000
    # NVS at 0x9000 is never written — preserved on existing boards
    cp "/tmp/wcb_esp32/WCB.ino.bin"            "$OUTPUT_DIR/WCB_${VERSION}_${BRANCH}_ESP32.bin"
    cp "/tmp/wcb_esp32/WCB.ino.bootloader.bin" "$OUTPUT_DIR/WCB_${VERSION}_${BRANCH}_ESP32_boot.bin"
    cp "/tmp/wcb_esp32/WCB.ino.partitions.bin" "$OUTPUT_DIR/WCB_${VERSION}_${BRANCH}_ESP32_part.bin"
    cp "/tmp/wcb_esp32/WCB.ino.bin"            "$WIZARD_BIN/WCB_ESP32.bin"
    cp "/tmp/wcb_esp32/WCB.ino.bootloader.bin" "$WIZARD_BIN/WCB_ESP32_boot.bin"
    cp "/tmp/wcb_esp32/WCB.ino.partitions.bin" "$WIZARD_BIN/WCB_ESP32_part.bin"
    echo "✓ Built: WCB_${VERSION}_${BRANCH}_ESP32 (.bin / _boot.bin / _part.bin)"
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
    --fqbn esp32:esp32:esp32s3:PartitionScheme=min_spiffs \
    --output-dir "/tmp/wcb_s3" \
    "$SKETCH_PATH"

if [ $? -eq 0 ]; then
    # Three separate files — ESP32-S3 bootloader lives at 0x0 (not 0x1000)
    cp "/tmp/wcb_s3/WCB.ino.bin"            "$OUTPUT_DIR/WCB_${VERSION}_${BRANCH}_ESP32S3.bin"
    cp "/tmp/wcb_s3/WCB.ino.bootloader.bin" "$OUTPUT_DIR/WCB_${VERSION}_${BRANCH}_ESP32S3_boot.bin"
    cp "/tmp/wcb_s3/WCB.ino.partitions.bin" "$OUTPUT_DIR/WCB_${VERSION}_${BRANCH}_ESP32S3_part.bin"
    cp "/tmp/wcb_s3/WCB.ino.bin"            "$WIZARD_BIN/WCB_ESP32S3.bin"
    cp "/tmp/wcb_s3/WCB.ino.bootloader.bin" "$WIZARD_BIN/WCB_ESP32S3_boot.bin"
    cp "/tmp/wcb_s3/WCB.ino.partitions.bin" "$WIZARD_BIN/WCB_ESP32S3_part.bin"
    echo "✓ Built: WCB_${VERSION}_${BRANCH}_ESP32S3 (.bin / _boot.bin / _part.bin)"
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