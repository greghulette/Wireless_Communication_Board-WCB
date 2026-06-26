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

// ─── PHASE 2: ESP-NOW relay OTA (firmware update over the mesh) ──────────────
//
// A USB-connected "relay" WCB forwards a firmware stream to a TARGET WCB over
// ESP-NOW; the target writes its inactive slot via the SAME core above and
// reboots. Browser drives flow control (ACK-paced), so the relay is stateless.
//
//   Browser ──USB──> Relay WCB ──ESP-NOW(OTA_DATA)──> Target WCB ─> esp_ota_write
//   Browser <──USB── Relay WCB <──ESP-NOW(OTA_ACK)─── Target WCB
//
// Reliability: the target ALWAYS acks its current contiguous write cursor (even
// on a duplicate/gap), and only advances on an in-order write — so a lost DATA
// stalls the cursor and the browser resends from there; a lost ACK is re-sent on
// the next (resent) DATA. esp_ota verifies before switching → never bricks.

#ifndef MAX_WCB_COUNT
#define MAX_WCB_COUNT 20   // mirror of WCB.ino (hardware peer limit) for this TU
#endif

// Packet types — local to the two OTA struct sizes below (so they can't collide
// with the existing 5/7 values that live in differently-sized structs).
#define PACKET_TYPE_OTA_BEGIN  20   // relay → target: start (size, chip family)
#define PACKET_TYPE_OTA_DATA   21   // relay → target: one firmware fragment
#define PACKET_TYPE_OTA_ACK    22   // target → relay: write cursor + status
#define PACKET_TYPE_OTA_END    23   // relay → target: finalize, verify, reboot
#define PACKET_TYPE_OTA_ABORT  24   // relay → target: tear down

#define OTA_ESPNOW_PAYLOAD 192      // firmware bytes per OTA_DATA packet

// OTA status codes (in OTA_ACK / END results)
#define OTA_ST_OK         0
#define OTA_ST_ERR        1         // generic reject (chip/partition/begin/write/verify)

// Control packet — BEGIN / END / ABORT / ACK (packed, 55 B → unique size).
typedef struct __attribute__((packed)) {
  char     structPassword[40];
  uint8_t  packetType;     // OTA_BEGIN / OTA_END / OTA_ABORT / OTA_ACK
  uint8_t  targetWCB;      // board this packet is addressed to
  uint8_t  sourceWCB;      // sender (relay for BEGIN/…; target for ACK)
  uint8_t  chipFamily;     // BEGIN: 0=ESP32, 1=ESP32-S3
  uint8_t  status;         // ACK/END: OTA_ST_*
  uint16_t sessionId;      // ties BEGIN/DATA/END/ACK together
  uint32_t imageSize;      // BEGIN
  uint32_t ackedOffset;    // ACK: highest contiguous byte written
} espnow_struct_ota_ctrl;

// Data packet — one firmware fragment (packed, 243 B → unique size).
typedef struct __attribute__((packed)) {
  char     structPassword[40];
  uint8_t  packetType;     // OTA_DATA
  uint8_t  targetWCB;
  uint8_t  sourceWCB;      // relay (where the target sends its ACK)
  uint16_t sessionId;      // must match the active session on the target
  uint16_t dataLen;        // valid bytes in data[]
  uint32_t fragOffset;     // ABSOLUTE byte offset into the image
  uint8_t  data[OTA_ESPNOW_PAYLOAD];
} espnow_struct_ota_data;

// ── Target-side ESP-NOW handlers (call the transport-agnostic core above) ───
void handleOtaBeginPacket(const uint8_t *raw);
void handleOtaDataPacket(const uint8_t *raw);
void handleOtaEndPacket(const uint8_t *raw);
void handleOtaAbortPacket(const uint8_t *raw);

// ── Relay-side ──
// handleOtaAckRelay: a relay received an OTA_ACK from a target — print it to USB
//                    as "[OTA:ACK,<src>,<session>,<offset>,<status>]" for the browser.
// processOtaRelayCommand: parse "?OTA,<sub>,…" and unicast the OTA packet to the target.
void handleOtaAckRelay(const uint8_t *raw);
void processOtaRelayCommand(const String &args);

#endif // WCB_OTA_H
