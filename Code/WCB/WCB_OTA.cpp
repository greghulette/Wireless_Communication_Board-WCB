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
#define OTA_LOCAL_DEFAULT_BAUD 115200UL // USB serial baud outside an OTA transfer

// A local USB OTA can temporarily raise the USB serial baud for the byte transfer
// (the 115200 terminal rate is the throughput bottleneck — base64 image @115200 is
// minutes). 0 = at the default; >0 = bumped, so teardown knows to restore it. The
// host drives the switch via ?OTALOCAL,BAUD (see processOtaLocalCommand).
static uint32_t s_otaXferBaud = 0;

// Return the USB serial to the default baud if a transfer bumped it. Idempotent.
// Called from otaAbortSession (covers ABORT + the 30 s idle timeout); the END path
// reboots instead, which resets the baud in setup().
static void otaRestoreLocalBaud() {
  if (s_otaXferBaud) {
    Serial.flush();                                // drain anything still queued
    Serial.updateBaudRate(OTA_LOCAL_DEFAULT_BAUD);
    s_otaXferBaud = 0;
  }
}

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
  otaRestoreLocalBaud();   // if a transfer bumped the USB baud, return it to 115200
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
    otaRestoreLocalBaud();   // this failure ENDS the session — don't strand the USB at a bumped baud
    return false;
  }

  esp_err_t e = esp_ota_write(ota.handle, data, len);
  if (e != ESP_OK) {
    Serial.printf("[OTA] esp_ota_write failed @%u: %s — aborting\n", offset, esp_err_to_name(e));
    otaTeardown();
    otaRestoreLocalBaud();   // this failure ENDS the session — don't strand the USB at a bumped baud
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
    otaRestoreLocalBaud();   // this failure ENDS the session — don't strand the USB at a bumped baud
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

  if (sub == "BAUD") {
    // rest = "<baud>" — temporarily raise THIS board's USB serial rate for the transfer.
    // Handshake: ACK at the CURRENT baud + flush so the host reads it, THEN switch our
    // UART; the host switches its side after the ACK. Auto-restored to 115200 on reboot
    // (END OK), on END failure, or via otaAbortSession (ABORT/timeout).
    //
    // REQUIRE an active session: the only restore paths are tied to the session
    // (END reboot / END-fail restore / otaAbortSession from ABORT or the ota.active-gated
    // idle timeout). A bump with no session in flight would have nothing to undo it and
    // could strand the baud, so reject it. The host always bumps AFTER BEGIN.
    if (!ota.active) {
      Serial.printf("[OTA:BAUD,ERR,%lu]\n", (unsigned long)OTA_LOCAL_DEFAULT_BAUD);
      return;
    }
    uint32_t baud = (uint32_t) rest.toInt();
    if (baud != 115200 && baud != 230400 && baud != 460800 &&
        baud != 921600 && baud != 1000000) {
      Serial.printf("[OTA:BAUD,ERR,%lu]\n", (unsigned long)OTA_LOCAL_DEFAULT_BAUD);
      return;
    }
    Serial.printf("[OTA:BAUD,OK,%lu]\n", (unsigned long)baud);   // ack at the OLD baud
    Serial.flush();                                              // ensure it's on the wire
    delay(20);
    Serial.updateBaudRate(baud);
    s_otaXferBaud = (baud == OTA_LOCAL_DEFAULT_BAUD) ? 0 : baud;
    return;
  }

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
      // A failed finalize does NOT reboot and has already cleared the session, so the
      // ota.active-gated idle timeout can't restore the baud. Restore it here (flush
      // sends [OTA:END,ERR] at the high baud first) so a bumped transfer that fails
      // verify — the documented too-fast-bridge outcome — can't strand the USB rate.
      otaRestoreLocalBaud();
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

// esp_now_send() only reaches a REGISTERED peer. Normal unicasts register the target via
// addActivePeer(); the OTA send paths did not — so a target (or relay) that wasn't already
// mutually peered couldn't unicast the OTA ACK / frame, the send returned
// ESP_ERR_ESPNOW_NOT_FOUND and was silently lost, and the Wizard saw "no response … via
// relay". Register the peer on-demand (best-effort; the MAC is deterministic from the WCB
// number) so relay OTA works even when the two boards weren't peered beforehand.
static void otaEnsurePeer(uint8_t wcbNum) {
  if (wcbNum < 1 || wcbNum > MAX_WCB_COUNT) return;
  const uint8_t *mac = WCBMacAddresses[wcbNum - 1];
  if (esp_now_is_peer_exist(mac)) return;
  esp_now_peer_info_t p = {};
  memcpy(p.peer_addr, mac, 6);
  p.channel = 0; p.encrypt = false;
  esp_now_add_peer(&p);   // best-effort; if the peer table is truly full the send just fails as before
}

// Register the peer if needed, unicast, and LOG on failure instead of silently dropping. The
// one remaining hard case is a FULL 20-slot ESP-NOW peer table: otaEnsurePeer's add can't
// succeed, esp_now_send returns ESP_ERR_ESPNOW_NOT_FOUND (12390/0x3066), and the frame would
// vanish — surfacing it turns a silent "no response via relay" into a diagnosable line that
// points at the real limit (clear a peer, or raise the cap). Used by BOTH OTA send legs.
static void otaUnicast(uint8_t destWcb, const void *buf, size_t len, const char *label) {
  if (destWcb < 1 || destWcb > MAX_WCB_COUNT) return;
  otaEnsurePeer(destWcb);
  esp_err_t r = esp_now_send(WCBMacAddresses[destWcb - 1], (const uint8_t *)buf, len);
  if (r != ESP_OK)
    Serial.printf("[OTA] %s -> WCB%u send failed rc=%d (ESP-NOW peer table full? cap 20)\n",
                  label, destWcb, (int)r);
}

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
  otaUnicast(relayWCB, &ack, sizeof(ack), "ACK");   // ensures the peer, unicasts, logs a full-table drop
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
  const bool inSession = (ota.active && pkt.sessionId == ota.sessionId);
  if (inSession) ota.lastActivityMs = millis();
  // Write (no-op on out-of-order/dup), then ack the current cursor so the browser
  // learns where we are — covers a lost DATA (cursor stalls → resend from cursor)
  // and a lost ACK (re-acked on the resent DATA).
  otaWrite(pkt.sessionId, pkt.fragOffset, pkt.data, len);
  // Report the session's REAL state. If a failed write already tore the session down
  // (image overrun / esp_ota_write error / idle abort), otaWrittenOffset() reads 0 and
  // an OK+0 ACK is indistinguishable from a stale duplicate: the browser ignores it,
  // times out, rewinds, resends from 0, is re-answered OK+0 — a genuine infinite hang
  // that looks like success with a frozen progress bar. OTA_ST_ERR on a no-longer-active
  // session makes it fail loudly instead. A frame for a DIFFERENT session gets its own id
  // back, which the browser filters out. (Matches navicore_ota.h, which already carries this.)
  sendOtaAck(pkt.sourceWCB, pkt.sessionId, inSession ? OTA_ST_OK : OTA_ST_ERR,
             otaWrittenOffset());
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
    otaUnicast(target, &pkt, sizeof(pkt), sub.c_str());
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
    otaUnicast(target, &pkt, sizeof(pkt), sub.c_str());
    return;
  }

  if (sub == "END" || sub == "ABORT") {
    espnow_struct_ota_ctrl pkt; memset(&pkt, 0, sizeof(pkt));
    strncpy(pkt.structPassword, espnowPassword, sizeof(pkt.structPassword) - 1);
    pkt.packetType = (sub == "END") ? PACKET_TYPE_OTA_END : PACKET_TYPE_OTA_ABORT;
    pkt.targetWCB  = target;
    pkt.sourceWCB  = (uint8_t)WCB_Number;
    pkt.sessionId  = session;
    otaUnicast(target, &pkt, sizeof(pkt), sub.c_str());
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
