#ifndef WCB_OTA_H
#define WCB_OTA_H
#include <Arduino.h>

// ─── ESP-NOW relay OTA — firmware update over the mesh ───────────────────────
//
// PHASE 1 (this file): a transport-agnostic OTA-write core + a local USB command
// driver (?OTALOCAL,*). The ESP-NOW relay transport (Phase 2) will reuse the
// SAME core via otaBegin/otaWrite/otaEnd below — only the framing differs.
//
// Brick-safety is structural: all writes go to the INACTIVE OTA partition
// (esp_ota_get_next_update_partition), the image is SHA-verified by esp_ota_end
// BEFORE the boot partition is switched, and any failure/timeout calls
// esp_ota_abort (which never switches). A failed or interrupted transfer always
// leaves the board running its current firmware.
// ────────────────────────────────────────────────────────────────────────────

// Local USB command driver. `args` is the text AFTER "OTALOCAL," — i.e. the
// subcommand and its parameters. Subcommands:
//   ?OTALOCAL,STATUS                       → print partitions / running ver / session
//   ?OTALOCAL,BEGIN,<imageSize>,<family>   → start (family: 0=ESP32, 1=ESP32-S3)
//   ?OTALOCAL,DATA,<offset>,<base64>       → one in-order chunk (offset = byte cursor)
//   ?OTALOCAL,END                          → verify, set boot partition, reboot
//   ?OTALOCAL,ABORT                        → tear down (no boot switch)
void processOtaLocalCommand(const String &args);

// Periodic timeout reaper — call from loop(). Aborts a stalled session so a
// dropped transfer frees the esp_ota handle (current app untouched).
void checkOtaTimeout();

// ── Transport-agnostic core (Phase 2 ESP-NOW handlers call these too) ───────
// otaBegin: validate chip family + partition size, esp_ota_begin on the inactive
//           slot. Tears down any stale session first.
// otaWrite: write ONE in-order chunk — `offset` MUST equal the running write
//           cursor (otaWrittenOffset()); out-of-order/duplicate returns false so
//           the sender can rewind to the cursor.
// otaEnd:   require the full image, esp_ota_end (SHA verify) + set_boot_partition.
//           Returns true if the caller should reboot into the new image.
bool     otaBegin(uint16_t sessionId, uint32_t imageSize, uint8_t chipFamily);
bool     otaWrite(uint16_t sessionId, uint32_t offset, const uint8_t *data, uint16_t len);
bool     otaEnd(uint16_t sessionId);
void     otaAbortSession(const char *reason);

uint32_t otaWrittenOffset();    // bytes committed so far (the resume / ACK cursor)
uint16_t otaActiveSession();    // current session id, or 0 if idle
uint8_t  otaLocalChipFamily();  // 0 = ESP32, 1 = ESP32-S3, 0xFF = unsupported

#endif // WCB_OTA_H
