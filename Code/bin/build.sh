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
#   - HumanCyborgRelationsAPI (HCRVocalizer) is bundled in the sketch at
#     Code/WCB/src/HumanCyborgRelationsAPI — no separate install needed
#
# Output (only written when BOTH targets compile successfully):
#   Code/bin/WCB_<version>_<branch>_ESP32.bin    (v1.0, v2.1, v2.3, v2.4)
#   Code/bin/WCB_<version>_<branch>_ESP32S3.bin  (v3.1, v3.2)
#   Wizard/bin/WCB_ESP32.bin                     (stable name for wizard auto-flash)
#   Wizard/bin/WCB_ESP32S3.bin                   (stable name for wizard auto-flash)
#
# IMPORTANT — failure semantics:
#   This script compiles BOTH targets into a temp dir FIRST and only then,
#   if BOTH succeeded, replaces the committed binaries. If ANY build fails
#   it touches nothing and exits NON-ZERO.
#
#   This is deliberate. The GitHub Actions workflow commits Code/bin & Wizard/bin
#   after this step. If this script exits non-zero, Actions skips the commit step,
#   so a failed compile can never delete or stale the published binaries — and the
#   run shows a real red ✗ instead of a misleading green ✓. (A previous version
#   deleted the bins up front and always exited 0, so a failed CI build silently
#   wiped Code/bin while reporting success.)
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKETCH_PATH="$SCRIPT_DIR/../WCB"
OUTPUT_DIR="$SCRIPT_DIR"
WIZARD_BIN="$SCRIPT_DIR/../../Wizard/bin"
VERSION=$(grep 'String SoftwareVersion =' "$SKETCH_PATH/WCB.ino" | head -1 | grep -o '"[^"]*"' | head -1 | tr -d '"')

# Branch name for the output filename. In GitHub Actions the checkout can be in
# a detached HEAD, where `rev-parse --abbrev-ref HEAD` returns the literal
# "HEAD"; fall back to the GITHUB_REF_NAME env var the workflow provides so the
# filename still carries the real branch.
BRANCH=$(git -C "$SKETCH_PATH" rev-parse --abbrev-ref HEAD 2>/dev/null)
if [ "$BRANCH" = "HEAD" ] || [ -z "$BRANCH" ]; then
    BRANCH="${GITHUB_REF_NAME:-unknown}"
fi

TMP_ESP32="/tmp/wcb_esp32"
TMP_S3="/tmp/wcb_s3"

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
rm -rf "$TMP_ESP32" "$TMP_S3"

ESP32_OK=1
S3_OK=1

# ----------------------------------------
# Build 1 - ESP32 (v1.0, v2.1, v2.3, v2.4)
# ----------------------------------------
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
echo ""
echo "Building ESP32 binary (v1.0, v2.1, v2.3, v2.4)..."
arduino-cli compile \
    --fqbn esp32:esp32:esp32:PartitionScheme=min_spiffs \
    --output-dir "$TMP_ESP32" \
    "$SKETCH_PATH"
if [ $? -eq 0 ]; then
    ESP32_OK=0
    echo "✓ Compiled: ESP32"
else
    echo "✗ Failed: ESP32 build"
fi

# ----------------------------------------
# Build 2 - ESP32-S3 (v3.1, v3.2)
# ----------------------------------------
echo ""
echo "Building ESP32-S3 binary (v3.1, v3.2)..."
arduino-cli compile \
    --fqbn esp32:esp32:esp32s3:PartitionScheme=min_spiffs \
    --output-dir "$TMP_S3" \
    "$SKETCH_PATH"
if [ $? -eq 0 ]; then
    S3_OK=0
    echo "✓ Compiled: ESP32-S3"
else
    echo "✗ Failed: ESP32-S3 build"
fi

# ----------------------------------------
# Publish — ONLY if BOTH targets compiled. Touch nothing on failure so the
# committed binaries are never deleted/staled by a broken build.
# ----------------------------------------
if [ $ESP32_OK -ne 0 ] || [ $S3_OK -ne 0 ]; then
    echo ""
    echo "========================================"
    echo " ✗ Build FAILED — committed binaries left untouched."
    echo "   ESP32:   $([ $ESP32_OK -eq 0 ] && echo OK || echo FAILED)"
    echo "   ESP32-S3:$([ $S3_OK   -eq 0 ] && echo OK || echo FAILED)"
    echo "========================================"
    rm -rf "$TMP_ESP32" "$TMP_S3"
    exit 1
fi

# Both succeeded — now it's safe to clear stale version-named files and copy.
# Three separate files per target flashed at their correct addresses:
#   ESP32:   bootloader → 0x1000   partitions → 0x8000   app → 0x10000
#   ESP32S3: bootloader → 0x0      partitions → 0x8000   app → 0x10000
# NVS at 0x9000 is never written — preserved on existing boards.

# ── S3 ships the CUSTOM short-watchdog bootloader, PER FLASH SIZE ───────────
# The S3 _boot artifacts are deliberately NOT the stock compiled bootloader:
# they're the hand-verified custom ones (RTC WDT 9000→3000 ms) so every board
# flashed from the web config tool gets the cold-boot auto-recovery fix.
# Identical to stock Arduino 3.3.4 otherwise (QIO-autodetect/80m — see
# WCB_S3_custom_bootloader_README.md; built from the Arduino sdkconfig).
#
# TWO variants because S3 boards carry different flash chips (3.1 = 8MB,
# 3.2 = 8MB or 16MB module): a bootloader whose header declares the WRONG
# flash size silently corrupts NVS (writes commit, reads come back stale —
# the documented "4MB trap"). The web flasher detects the chip's REAL flash
# size at flash time and picks the matching _boot_<size> artifact; it must
# NEVER write a mismatched one.
# Classic ESP32 (PICO) boards keep the stock compiled bootloader — they don't
# exhibit the cold-boot stall.
CUSTOM_S3_BOOT_16MB="$OUTPUT_DIR/WCB_S3_custom_bootloader_16MB_wdt3s.bin"
CUSTOM_S3_BOOT_8MB="$OUTPUT_DIR/WCB_S3_custom_bootloader_8MB_wdt3s.bin"
PY_BIN="$(command -v python3 || command -v python)"

# check_boot <file> <size-nibble> <label>: exists + magic 0xE9 + declared size
check_boot() {
    if [ ! -f "$1" ]; then
        echo "✗ ERROR: custom S3 bootloader missing: $1"
        echo "  Refusing to publish — S3 releases must carry BOTH short-WDT"
        echo "  bootloader variants (8MB and 16MB)."
        rm -rf "$TMP_ESP32" "$TMP_S3"
        exit 1
    fi
    if ! "$PY_BIN" - "$1" "$2" <<'PYEOF'
import sys
h = open(sys.argv[1], "rb").read(4)
sys.exit(0 if (h[0] == 0xE9 and (h[3] >> 4) == int(sys.argv[2])) else 1)
PYEOF
    then
        echo "✗ ERROR: $1 failed header sanity check (magic 0xE9 / $3 size nibble $2)."
        rm -rf "$TMP_ESP32" "$TMP_S3"
        exit 1
    fi
}
# ESP image header byte3 hi-nibble: 3 = 8MB, 4 = 16MB
check_boot "$CUSTOM_S3_BOOT_16MB" 4 "16MB"
check_boot "$CUSTOM_S3_BOOT_8MB"  3 "8MB"

# Only remove the version-named files THIS script owns (WCB_*_ESP32*.bin) —
# a bare *.bin wipe would also delete the custom bootloader above.
rm -f "$OUTPUT_DIR"/WCB_*_ESP32*.bin

cp "$TMP_ESP32/WCB.ino.bin"            "$OUTPUT_DIR/WCB_${VERSION}_${BRANCH}_ESP32.bin"
cp "$TMP_ESP32/WCB.ino.bootloader.bin" "$OUTPUT_DIR/WCB_${VERSION}_${BRANCH}_ESP32_boot.bin"
cp "$TMP_ESP32/WCB.ino.partitions.bin" "$OUTPUT_DIR/WCB_${VERSION}_${BRANCH}_ESP32_part.bin"
cp "$TMP_ESP32/WCB.ino.bin"            "$WIZARD_BIN/WCB_ESP32.bin"
cp "$TMP_ESP32/WCB.ino.bootloader.bin" "$WIZARD_BIN/WCB_ESP32_boot.bin"
cp "$TMP_ESP32/WCB.ino.partitions.bin" "$WIZARD_BIN/WCB_ESP32_part.bin"

cp "$TMP_S3/WCB.ino.bin"            "$OUTPUT_DIR/WCB_${VERSION}_${BRANCH}_ESP32S3.bin"
cp "$TMP_S3/WCB.ino.partitions.bin" "$OUTPUT_DIR/WCB_${VERSION}_${BRANCH}_ESP32S3_part.bin"
cp "$TMP_S3/WCB.ino.bin"            "$WIZARD_BIN/WCB_ESP32S3.bin"
cp "$TMP_S3/WCB.ino.partitions.bin" "$WIZARD_BIN/WCB_ESP32S3_part.bin"
# Per-flash-size bootloaders — the flasher picks by DETECTED chip flash size.
cp "$CUSTOM_S3_BOOT_16MB" "$OUTPUT_DIR/WCB_${VERSION}_${BRANCH}_ESP32S3_boot_16MB.bin"
cp "$CUSTOM_S3_BOOT_8MB"  "$OUTPUT_DIR/WCB_${VERSION}_${BRANCH}_ESP32S3_boot_8MB.bin"
cp "$CUSTOM_S3_BOOT_16MB" "$WIZARD_BIN/WCB_ESP32S3_boot_16MB.bin"
cp "$CUSTOM_S3_BOOT_8MB"  "$WIZARD_BIN/WCB_ESP32S3_boot_8MB.bin"
# Legacy single-name artifact (16MB) kept so an older cached Wizard / direct
# downloaders don't 404; the current flasher prefers the sized names.
cp "$CUSTOM_S3_BOOT_16MB" "$OUTPUT_DIR/WCB_${VERSION}_${BRANCH}_ESP32S3_boot.bin"
cp "$CUSTOM_S3_BOOT_16MB" "$WIZARD_BIN/WCB_ESP32S3_boot.bin"
echo "✓ S3 _boot artifacts = CUSTOM short-WDT bootloaders (8MB + 16MB variants)"

rm -rf "$TMP_ESP32" "$TMP_S3"

echo ""
echo "========================================"
echo " ✓ Build complete — both targets published"
echo " Binaries:"
ls -lh "$OUTPUT_DIR/"*.bin 2>/dev/null || echo " No bin files found"
echo "========================================"
exit 0
