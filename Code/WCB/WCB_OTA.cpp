#include "WCB_RemoteTerm.h"   // Must be first — redirects Serial → WCBDebugSerial
#include "WCB_OTA.h"
#include <esp_ota_ops.h>
#include <esp_partition.h>
#include <mbedtls/base64.h>

// Externs provided by WCB.ino
extern int    WCB_Number;
extern bool   debugEnabled;
extern String SoftwareVersion;

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
    otaBegin(OTA_LOCAL_SESSION, size, family);   // logs its own result
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
    }
    return;
  }

  if (sub == "END") {
    if (otaEnd(OTA_LOCAL_SESSION)) {
      Serial.println("[OTA] rebooting into new firmware in 2s…");
      delay(2000);
      ESP.restart();
    }
    return;
  }

  if (sub == "ABORT") { otaAbortSession("local abort command"); return; }

  Serial.printf("[OTA] unknown subcommand '%s' (use STATUS|BEGIN|DATA|END|ABORT)\n", sub.c_str());
}
