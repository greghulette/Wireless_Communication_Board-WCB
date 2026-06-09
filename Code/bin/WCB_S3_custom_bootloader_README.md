# WCB ESP32-S3 custom bootloader

**File:** `WCB_S3_custom_bootloader_16MB_wdt3s.bin`

A drop-in replacement for the stock Arduino-ESP32 3.3.4 S3 bootloader, identical
to it **except** the RTC watchdog timeout is shortened from 9000 ms → **3000 ms**.
Purpose: if a cold/marginal board stalls in the pre-app boot window, it
auto-resets and retries in ~3 s instead of sitting dark until someone presses
reset. The RTC WDT runs off the internal RC oscillator, so it still fires even
when a cold crystal is what's stalling the boot.

## Header (must match the stock Arduino bootloader)
- Flash mode: **DIO** (header) — QIO + auto-detect at runtime
- Flash freq: **80 MHz**
- Flash size: **16 MB**   ← critical
- RTC watchdog: **3000 ms** (stock is 9000 ms)

## ⚠️ The 4MB trap — do not repeat
The first build of this bootloader declared **4 MB** flash (the partition table
only uses the first 4 MB, so 4 MB seemed right). On the 16 MB chip that
misconfigures the flash/MMU setup: the running app's NVS **writes commit but
reads come back stale**, so all saved config (HW version, baud, ESP-NOW, stored
sequences, variables) silently fails to persist across reboot — while esptool
dumps still show the data. The fix was to declare the real **16 MB** and mirror
Arduino's full bootloader sdkconfig (not IDF defaults). Any board flashed with
the old 4 MB bootloader has silently-broken NVS and must be re-flashed with this
one (or an Arduino IDE upload).

## Flash it — use the ESP Flasher Companion app (recommended)
The **ESP Flasher Companion** (separate repo: `GitHub/ESP-Flasher-Companion`)
has a guarded **Restore Bootloader** button per board. It detects the chip +
real flash size first and **refuses to flash unless the board is ESP32-S3 /
16MB**, then writes only 0x0. This makes the flash-size trap impossible and
works for both the WCB 3.x boards and the NaviCore (same S3/16MB board).

Manual equivalent (bootloader only — leaves app / partition table / NVS intact):
```
python -m esptool --chip esp32s3 -p COM## write_flash 0x0 WCB_S3_custom_bootloader_16MB_wdt3s.bin
```

## Workflow note — the Arduino IDE wipes this every upload
The IDE writes the STOCK bootloader at 0x0 on EVERY upload, so after any IDE
upload the custom bootloader is gone. Two ways to keep it:
- **After an IDE upload:** press **Restore Bootloader** in the Companion app
  (app / config untouched). ~2 seconds.
- **Skip the IDE for uploads:** use the Companion's **Build + Flash** — it
  compiles and writes only 0x10000, so it never disturbs the bootloader.

A custom bootloader cannot be delivered over OTA — it requires a USB flash.

## Rebuild
Source project: `PlatformIO/Projects/WCB_Bootloader_S3` (framework=espidf).
`sdkconfig.defaults` there mirrors the Arduino sdkconfig from
`esp32-arduino-libs/.../esp32s3/sdkconfig`; only `CONFIG_BOOTLOADER_WDT_TIME_MS`
is changed. Build: `pio run -e esp32-s3-devkitc-1`, harvest
`.pio/build/esp32-s3-devkitc-1/bootloader.bin`, and verify the header with
`esptool image-info` before trusting it.
