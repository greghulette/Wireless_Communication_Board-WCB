#include "WCB_RemoteTerm.h"   // Must be first — redirects Serial → WCBDebugSerial
#include "WCB_OTA.h"
#include <esp_ota_ops.h>
#include <esp_partition.h>
#include <mbedtls/base64.h>
#include <esp_now.h>          // P2: relay OTA over ESP-NOW
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>   // P2: defer OTA flash writes out of the WiFi callback

// Externs provided by WCB.ino
extern int     WCB_Number;
extern bool    debugEnabled;
extern String  SoftwareVersion;
extern char    espnowPassword[40];               // P2: shared-secret gate
extern uint8_t WCBMacAddresses[MAX_WCB_COUNT][6]; // P2: per-WCB MAC table
extern void    otaRelayPrint(const char *line);  // defer relay-ACK Serial output to loop() (cross-core safe)

// Struct sizes feed the size-based ESP-NOW router in WCB.ino — they MUST stay
// distinct from every other packet ({43,204,226,230,249,252}). Lock them here.
static_assert(sizeof(espnow_struct_ota_ctrl) == 55,  "ota_ctrl size changed — re-check router uniqueness");
static_assert(sizeof(espnow_struct_ota_data) == 243, "ota_data size changed — re-check router uniqueness");

// ── Single in-flight OTA session ────────────────────────────────────────────
// Only one OTA can run at a time (one esp_ota handle). This matches the single
// MgmtSession model used for remote config push.
static struct {
  bool                   active;
  uint16_t               sessionId;
  uint32_t               imageSize;
  uint32_t               written;        // bytes committed == next expected offset
  uint8_t                chipFamily;
  esp_ota_handle_t       handle;
  const esp_partition_t *part;
  unsigned long          lastActivityMs;
  uint32_t               lastProgressPrint;
} ota = {0};

#define OTA_TIMEOUT_MS        30000UL   // long-lived (NOT the 15 s MGMT reaper)
#define OTA_LOCAL_SESSION     1         // fixed session id for the local USB path
#define OTA_LOCAL_MAX_CHUNK   1024      // max decoded bytes per ?OTALOCAL,DATA line
#define OTA_PROGRESS_STEP     65536UL   // print a progress line every 64 KB

uint8_t otaLocalChipFamily() {
#if   defined(CONFIG_IDF_TARGET_ESP32S3)
  return 1;   // ESP32-S3
#elif defined(CONFIG_IDF_TARGET_ESP32)
  return 0;   // classic ESP32
#else
  return 0xFF;
#endif
}

uint32_t otaWrittenOffset() { return ota.active ? ota.written  : 0; }
uint16_t otaActiveSession() { return ota.active ? ota.sessionId : 0; }

// Free the handle and clear state WITHOUT switching the boot partition.
static void otaTeardown() {
  if (ota.active && ota.handle) {
    esp_ota_abort(ota.handle);   // safe to call on an in-progress handle
  }
  ota.active = false;
  ota.handle = 0;
  ota.part   = nullptr;
  ota.imageSize = 0;
  ota.written   = 0;
  ota.sessionId = 0;
  ota.lastProgressPrint = 0;
}

void otaAbortSession(const char *reason) {
  if (ota.active) {
    Serial.printf("[OTA] aborted: %s (current app intact)\n", reason ? reason : "");
  }
  otaTeardown();
}

void checkOtaTimeout() {
  if (ota.active && (millis() - ota.lastActivityMs > OTA_TIMEOUT_MS)) {
    otaAbortSession("session timed out");
  }
}

// ── Core ────────────────────────────────────────────────────────────────────
bool otaBegin(uint16_t sessionId, uint32_t imageSize, uint8_t chipFamily) {
  if (ota.active) otaTeardown();   // supersede any stale session

  uint8_t mine = otaLocalChipFamily();
  if (chipFamily != mine) {
    Serial.printf("[OTA] BEGIN rejected: image chip family %u != this board %u (brick guard)\n",
                  chipFamily, mine);
    return false;
  }

  const esp_partition_t *part = esp_ota_get_next_update_partition(NULL);
  if (!part) {
    Serial.println("[OTA] BEGIN rejected: no inactive OTA partition available");
    return false;
  }
  if (imageSize == 0 || imageSize > part->size) {
    Serial.printf("[OTA] BEGIN rejected: image %u B exceeds partition '%s' (%u B)\n",
                  imageSize, part->label, part->size);
    return false;
  }

  esp_err_t e = esp_ota_begin(part, imageSize, &ota.handle);
  if (e != ESP_OK) {
    Serial.printf("[OTA] esp_ota_begin failed: %s\n", esp_err_to_name(e));
    ota.handle = 0;
    return false;
  }

  ota.active            = true;
  ota.sessionId         = sessionId;
  ota.imageSize         = imageSize;
  ota.written           = 0;
  ota.chipFamily        = chipFamily;
  ota.part              = part;
  ota.lastActivityMs    = millis();
  ota.lastProgressPrint = 0;
  Serial.printf("[OTA] BEGIN ok: session %u, %u B → partition '%s' @0x%06x (%u B)\n",
                sessionId, imageSize, part->label, part->address, part->size);
  return true;
}

bool otaWrite(uint16_t sessionId, uint32_t offset, const uint8_t *data, uint16_t len) {
  if (!ota.active || sessionId != ota.sessionId) return false;
  if (offset != ota.written) return false;   // gap/dup → caller rewinds to ota.written
  if (ota.written + len > ota.imageSize) {
    Serial.printf("[OTA] write overruns image (%u + %u > %u) — aborting\n",
                  ota.written, len, ota.imageSize);
    otaTeardown();
    return false;
  }

  esp_err_t e = esp_ota_write(ota.handle, data, len);
  if (e != ESP_OK) {
    Serial.printf("[OTA] esp_ota_write failed @%u: %s — aborting\n", offset, esp_err_to_name(e));
    otaTeardown();
    return false;
  }
  ota.written       += len;
  ota.lastActivityMs = millis();

  if (ota.written - ota.lastProgressPrint >= OTA_PROGRESS_STEP || ota.written == ota.imageSize) {
    ota.lastProgressPrint = ota.written;
    Serial.printf("[OTA] %u / %u B (%u%%)\n", ota.written, ota.imageSize,
                  (uint32_t)((uint64_t)ota.written * 100 / ota.imageSize));
  }
  return true;
}

bool otaEnd(uint16_t sessionId) {
  if (!ota.active || sessionId != ota.sessionId) {
    Serial.println("[OTA] END: no matching active session");
    return false;
  }
  if (ota.written != ota.imageSize) {
    Serial.printf("[OTA] END rejected: incomplete %u / %u B\n", ota.written, ota.imageSize);
    otaTeardown();
    return false;
  }

  esp_err_t e = esp_ota_end(ota.handle);   // validates magic + SHA; consumes the handle
  ota.handle = 0;
  if (e != ESP_OK) {
    Serial.printf("[OTA] END verify FAILED: %s (image rejected, current app intact)\n",
                  esp_err_to_name(e));
    ota.active = false; ota.part = nullptr;
    return false;
  }

  const esp_partition_t *part = ota.part;
  e = esp_ota_set_boot_partition(part);
  ota.active = false; ota.part = nullptr;
  if (e != ESP_OK) {
    Serial.printf("[OTA] END set_boot_partition failed: %s\n", esp_err_to_name(e));
    return false;
  }
  Serial.printf("[OTA] END ok: verified %u B → next boot '%s'\n", ota.imageSize, part->label);
  return true;   // caller reboots
}

// ── Status helper ───────────────────────────────────────────────────────────
static void otaPrintStatus() {
  const esp_partition_t *run  = esp_ota_get_running_partition();
  const esp_partition_t *next = esp_ota_get_next_update_partition(NULL);
  Serial.println("---------- OTA Status ----------");
  Serial.printf("Chip:        %s (family %u)\n", ESP.getChipModel(), otaLocalChipFamily());
  Serial.printf("Firmware:    %s\n", SoftwareVersion.c_str());
  if (run)  Serial.printf("Running:     '%s' @0x%06x (%u B)\n", run->label,  run->address,  run->size);
  if (next) Serial.printf("Next (OTA):  '%s' @0x%06x (%u B)\n", next->label, next->address, next->size);
  else      Serial.println("Next (OTA):  none — partition table has no spare OTA slot!");
  if (ota.active)
    Serial.printf("Session:     ACTIVE id=%u  %u / %u B\n", ota.sessionId, ota.written, ota.imageSize);
  else
    Serial.println("Session:     idle");
  Serial.println("--------------------------------");
}

// ── Local USB command driver ────────────────────────────────────────────────
// Reusable scratch buffer for one decoded DATA chunk (avoids per-call malloc).
static uint8_t s_otaChunk[OTA_LOCAL_MAX_CHUNK];

void processOtaLocalCommand(const String &args) {
  // args = "<SUB>[,<params...>]"
  int c1 = args.indexOf(',');
  String sub  = (c1 < 0) ? args : args.substring(0, c1);
  String rest = (c1 < 0) ? ""   : args.substring(c1 + 1);
  sub.trim(); sub.toUpperCase();

  if (sub == "STATUS" || sub.isEmpty()) { otaPrintStatus(); return; }

  if (sub == "BEGIN") {
    // rest = "<imageSize>,<chipFamily>"
    int p = rest.indexOf(',');
    if (p < 0) { Serial.println("[OTA] BEGIN usage: ?OTALOCAL,BEGIN,<imageSize>,<family 0|1>"); return; }
    uint32_t size   = (uint32_t) rest.substring(0, p).toInt();
    uint8_t  family = (uint8_t)  rest.substring(p + 1).toInt();
    bool ok = otaBegin(OTA_LOCAL_SESSION, size, family);   // logs its own detail
    Serial.printf("[OTA:BEGIN,%s,%u]\n", ok ? "OK" : "ERR", otaWrittenOffset());
    return;
  }

  if (sub == "DATA") {
    // rest = "<offset>,<base64>"
    int p = rest.indexOf(',');
    if (p < 0) { Serial.println("[OTA] DATA usage: ?OTALOCAL,DATA,<offset>,<base64>"); return; }
    uint32_t offset = (uint32_t) rest.substring(0, p).toInt();
    String   b64    = rest.substring(p + 1);
    b64.trim();
    size_t outLen = 0;
    int rc = mbedtls_base64_decode(s_otaChunk, sizeof(s_otaChunk), &outLen,
                                   (const unsigned char *)b64.c_str(), b64.length());
    if (rc != 0) {
      Serial.printf("[OTA] DATA base64 error %d (chunk too big? max %u B decoded)\n",
                    rc, (unsigned)sizeof(s_otaChunk));
      return;
    }
    if (!otaWrite(OTA_LOCAL_SESSION, offset, s_otaChunk, (uint16_t)outLen)) {
      // Over reliable USB this should only happen on a real gap — report the cursor
      Serial.printf("[OTA] DATA rejected at offset %u (write cursor at %u)\n",
                    offset, otaWrittenOffset());
      Serial.printf("[OTA:NAK,%u]\n", otaWrittenOffset());   // machine-readable resync point
      return;
    }
    // Machine-readable per-chunk ACK = flow control. The browser waits for this
    // before sending the next chunk so the ESP32 RX buffer can't overflow while
    // a flash write is in progress (USB has no other backpressure here).
    Serial.printf("[OTA:ACK,%u]\n", otaWrittenOffset());
    return;
  }

  if (sub == "END") {
    if (otaEnd(OTA_LOCAL_SESSION)) {
      Serial.println("[OTA:END,OK]");
      Serial.println("[OTA] rebooting into new firmware in 2s…");
      delay(2000);
      ESP.restart();
    } else {
      Serial.println("[OTA:END,ERR]");
    }
    return;
  }

  if (sub == "ABORT") { otaAbortSession("local abort command"); return; }

  Serial.printf("[OTA] unknown subcommand '%s' (use STATUS|BEGIN|DATA|END|ABORT)\n", sub.c_str());
}

// ════════════════════════════════════════════════════════════════════════════
//  PHASE 2 — ESP-NOW RELAY OTA
//  Target-side handlers (drive the SAME core above) + relay-side forward/ACK.
// ════════════════════════════════════════════════════════════════════════════

// Build + unicast an OTA_ACK ctrl packet back to the relay.
static void sendOtaAck(uint8_t relayWCB, uint16_t sessionId, uint8_t status, uint32_t ackedOffset) {
  if (relayWCB < 1 || relayWCB > MAX_WCB_COUNT) return;
  espnow_struct_ota_ctrl ack;
  memset(&ack, 0, sizeof(ack));
  strncpy(ack.structPassword, espnowPassword, sizeof(ack.structPassword) - 1);
  ack.packetType  = PACKET_TYPE_OTA_ACK;
  ack.targetWCB   = relayWCB;             // addressed to the relay
  ack.sourceWCB   = (uint8_t)WCB_Number;  // us (the target)
  ack.status      = status;
  ack.sessionId   = sessionId;
  ack.ackedOffset = ackedOffset;
  esp_now_send(WCBMacAddresses[relayWCB - 1], (const uint8_t *)&ack, sizeof(ack));
}

// Password + addressed-to-us gate shared by the target-side handlers.
static bool otaPktAuth(const char *pw, uint8_t targetWCB) {
  return (String(pw) == String(espnowPassword)) && (targetWCB == (uint8_t)WCB_Number);
}

// ── Target side (reuses otaBegin/otaWrite/otaEnd) ───────────────────────────
void handleOtaBeginPacket(const uint8_t *raw) {
  espnow_struct_ota_ctrl pkt;
  memcpy(&pkt, raw, sizeof(pkt));
  pkt.structPassword[sizeof(pkt.structPassword) - 1] = '\0';
  if (!otaPktAuth(pkt.structPassword, pkt.targetWCB)) return;
  bool ok = otaBegin(pkt.sessionId, pkt.imageSize, pkt.chipFamily);
  sendOtaAck(pkt.sourceWCB, pkt.sessionId, ok ? OTA_ST_OK : OTA_ST_ERR, otaWrittenOffset());
}

void handleOtaDataPacket(const uint8_t *raw) {
  espnow_struct_ota_data pkt;
  memcpy(&pkt, raw, sizeof(pkt));
  pkt.structPassword[sizeof(pkt.structPassword) - 1] = '\0';
  if (!otaPktAuth(pkt.structPassword, pkt.targetWCB)) return;
  uint16_t len = pkt.dataLen > OTA_ESPNOW_PAYLOAD ? OTA_ESPNOW_PAYLOAD : pkt.dataLen;
  // Any in-session frame — even a dup from a lost-ACK resend — proves the browser
  // is still streaming, so keep the session alive. Dups are write no-ops, so
  // otaWrite() won't refresh lastActivityMs; without this a lost-ACK retry storm
  // could trip the idle timeout and abort a transfer that was still live.
  if (ota.active && pkt.sessionId == ota.sessionId) ota.lastActivityMs = millis();
  // Write (no-op on out-of-order/dup), then ALWAYS ack the current cursor so the
  // browser learns where we are — covers a lost DATA (cursor stalls → resend
  // from cursor) and a lost ACK (re-acked on the resent DATA).
  otaWrite(pkt.sessionId, pkt.fragOffset, pkt.data, len);
  sendOtaAck(pkt.sourceWCB, pkt.sessionId, OTA_ST_OK, otaWrittenOffset());
}

void handleOtaEndPacket(const uint8_t *raw) {
  espnow_struct_ota_ctrl pkt;
  memcpy(&pkt, raw, sizeof(pkt));
  pkt.structPassword[sizeof(pkt.structPassword) - 1] = '\0';
  if (!otaPktAuth(pkt.structPassword, pkt.targetWCB)) return;
  bool ok = otaEnd(pkt.sessionId);
  sendOtaAck(pkt.sourceWCB, pkt.sessionId, ok ? OTA_ST_OK : OTA_ST_ERR, otaWrittenOffset());
  if (ok) {
    Serial.println("[OTA] remote update verified — rebooting into new firmware…");
    delay(300);   // let the ACK actually transmit before the radio drops
    ESP.restart();
  }
}

void handleOtaAbortPacket(const uint8_t *raw) {
  espnow_struct_ota_ctrl pkt;
  memcpy(&pkt, raw, sizeof(pkt));
  pkt.structPassword[sizeof(pkt.structPassword) - 1] = '\0';
  if (!otaPktAuth(pkt.structPassword, pkt.targetWCB)) return;
  otaAbortSession("remote abort");
  sendOtaAck(pkt.sourceWCB, pkt.sessionId, OTA_ST_OK, 0);
}

// ── Relay side ──────────────────────────────────────────────────────────────
// A relay received the target's OTA_ACK — surface it to USB for the browser.
void handleOtaAckRelay(const uint8_t *raw) {
  espnow_struct_ota_ctrl pkt;
  memcpy(&pkt, raw, sizeof(pkt));
  pkt.structPassword[sizeof(pkt.structPassword) - 1] = '\0';
  if (String(pkt.structPassword) != String(espnowPassword)) return;
  if (pkt.targetWCB != (uint8_t)WCB_Number) return;   // ACK addressed to us (the relay)
  // This runs in the ESP-NOW receive callback (WiFi task). ESP32 Serial isn't
  // atomic across cores, and this [OTA:ACK,...] line is the browser's flow-control
  // token — a direct write here can interleave with Core-1 output and garble it.
  // Defer the print to loop() via the shared relay queue.
  char line[64];
  snprintf(line, sizeof(line), "[OTA:ACK,%u,%u,%lu,%u]",
           pkt.sourceWCB, pkt.sessionId, (unsigned long)pkt.ackedOffset, pkt.status);
  otaRelayPrint(line);
}

// "?OTA,<SUB>,<target>,<session>,…" — build the OTA packet and unicast to target.
//   BEGIN,<target>,<session>,<size>,<family>
//   DATA,<target>,<session>,<offset>,<base64(<=192 B)>
//   END,<target>,<session>      ABORT,<target>,<session>
void processOtaRelayCommand(const String &args) {
  int c1 = args.indexOf(',');
  String sub  = (c1 < 0) ? args : args.substring(0, c1);
  String rest = (c1 < 0) ? ""   : args.substring(c1 + 1);
  sub.trim(); sub.toUpperCase();

  // common prefix: <target>,<session>,…
  int c2 = rest.indexOf(',');
  uint8_t  target  = (uint8_t)((c2 < 0 ? rest : rest.substring(0, c2)).toInt());
  String   r2      = (c2 < 0) ? "" : rest.substring(c2 + 1);
  int c3 = r2.indexOf(',');
  uint16_t session = (uint16_t)((c3 < 0 ? r2 : r2.substring(0, c3)).toInt());
  String   r3      = (c3 < 0) ? "" : r2.substring(c3 + 1);

  if (target < 1 || target > MAX_WCB_COUNT) {
    Serial.printf("[OTA] relay: invalid target %u\n", target);
    return;
  }
  const uint8_t *destMac = WCBMacAddresses[target - 1];

  if (sub == "BEGIN") {
    int p = r3.indexOf(',');
    uint32_t size   = (uint32_t)((p < 0 ? r3 : r3.substring(0, p)).toInt());
    uint8_t  family = (uint8_t) (p < 0 ? 0 : r3.substring(p + 1).toInt());
    espnow_struct_ota_ctrl pkt; memset(&pkt, 0, sizeof(pkt));
    strncpy(pkt.structPassword, espnowPassword, sizeof(pkt.structPassword) - 1);
    pkt.packetType = PACKET_TYPE_OTA_BEGIN;
    pkt.targetWCB  = target;
    pkt.sourceWCB  = (uint8_t)WCB_Number;
    pkt.chipFamily = family;
    pkt.sessionId  = session;
    pkt.imageSize  = size;
    esp_now_send(destMac, (const uint8_t *)&pkt, sizeof(pkt));
    return;
  }

  if (sub == "DATA") {
    int p = r3.indexOf(',');
    if (p < 0) { Serial.println("[OTA] relay DATA: ?OTA,DATA,<t>,<s>,<offset>,<b64>"); return; }
    uint32_t offset = (uint32_t)r3.substring(0, p).toInt();
    String   b64    = r3.substring(p + 1); b64.trim();
    espnow_struct_ota_data pkt; memset(&pkt, 0, sizeof(pkt));
    strncpy(pkt.structPassword, espnowPassword, sizeof(pkt.structPassword) - 1);
    pkt.packetType = PACKET_TYPE_OTA_DATA;
    pkt.targetWCB  = target;
    pkt.sourceWCB  = (uint8_t)WCB_Number;
    pkt.sessionId  = session;
    pkt.fragOffset = offset;
    size_t outLen = 0;
    int rc = mbedtls_base64_decode(pkt.data, sizeof(pkt.data), &outLen,
                                   (const unsigned char *)b64.c_str(), b64.length());
    if (rc != 0) { Serial.printf("[OTA] relay DATA base64 error %d\n", rc); return; }
    pkt.dataLen = (uint16_t)outLen;
    esp_now_send(destMac, (const uint8_t *)&pkt, sizeof(pkt));
    return;
  }

  if (sub == "END" || sub == "ABORT") {
    espnow_struct_ota_ctrl pkt; memset(&pkt, 0, sizeof(pkt));
    strncpy(pkt.structPassword, espnowPassword, sizeof(pkt.structPassword) - 1);
    pkt.packetType = (sub == "END") ? PACKET_TYPE_OTA_END : PACKET_TYPE_OTA_ABORT;
    pkt.targetWCB  = target;
    pkt.sourceWCB  = (uint8_t)WCB_Number;
    pkt.sessionId  = session;
    esp_now_send(destMac, (const uint8_t *)&pkt, sizeof(pkt));
    return;
  }

  Serial.printf("[OTA] relay: unknown subcommand '%s'\n", sub.c_str());
}

// ── Deferred processing: keep esp_ota flash writes OUT of the WiFi callback ──
// esp_ota_begin (erases ~1.2 MB) / esp_ota_write / esp_ota_end all block and
// must not run in the ESP-NOW receive callback — doing so stalls the WiFi task
// and the OTA session times out mid-stream. The callback copies the packet into
// this queue; drainOtaPackets() (loop context) runs the handlers safely. This
// mirrors how the command queue defers ESP-NOW-received commands to loop().
typedef struct {
  uint8_t  buf[244];   // largest OTA packet (ota_data = 243 B)
  uint16_t len;
} OtaPktSlot;
static QueueHandle_t otaPktQueue = nullptr;

void enqueueOtaPacket(const uint8_t *raw, uint16_t len) {
  if (len == 0 || len > sizeof(((OtaPktSlot *)0)->buf)) return;
  if (!otaPktQueue) {
    otaPktQueue = xQueueCreate(12, sizeof(OtaPktSlot));
    if (!otaPktQueue) return;
  }
  OtaPktSlot slot;
  memcpy(slot.buf, raw, len);
  slot.len = len;
  xQueueSend(otaPktQueue, &slot, 0);   // drop if full — the browser resends from its cursor
}

void drainOtaPackets() {
  if (!otaPktQueue) return;
  OtaPktSlot slot;
  while (xQueueReceive(otaPktQueue, &slot, 0) == pdTRUE) {
    if (slot.len == sizeof(espnow_struct_ota_data)) {
      handleOtaDataPacket(slot.buf);
    } else if (slot.len == sizeof(espnow_struct_ota_ctrl)) {
      uint8_t pt = ((espnow_struct_ota_ctrl *)slot.buf)->packetType;
      if      (pt == PACKET_TYPE_OTA_BEGIN) handleOtaBeginPacket(slot.buf);
      else if (pt == PACKET_TYPE_OTA_END)   handleOtaEndPacket(slot.buf);
      else if (pt == PACKET_TYPE_OTA_ABORT) handleOtaAbortPacket(slot.buf);
    }
  }
}
