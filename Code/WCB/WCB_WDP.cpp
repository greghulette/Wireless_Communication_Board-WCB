#include "WCB_RemoteTerm.h"   // Must be first — redirects Serial -> WCBDebugSerial
#include "WCB_WDP.h"
#include "WCB_Storage.h"      // MAX_WCB_COUNT, etmEnabled, WCB_Number externs
#include "WCB_HCR.h"          // hcrConfig
#include "WCB_MP3.h"          // mp3Config
#include "WCB_WLED.h"         // wledConfig
#include "WCB_Maestro.h"      // maestroConfigs[], MAX_MAESTROS_PER_WCB
#include "WCB_PWM.h"          // pwmOutputCount, activePWMCount
#include <Preferences.h>

// ---- Externs provided by WCB.ino ----------------------------------------
extern String      wcb_alias;         // board alias ("" = unset, sanitized, <=24)
extern int         wcb_hw_version;    // 0/1/21/23/24/31/32
extern String      SoftwareVersion;   // firmware version string
extern bool        Kyber_Local;
extern bool        Maestro_Remote;
extern bool        specialPeerEnabled;
extern bool        debugEnabled;
extern Preferences preferences;
// (MAX_WCB_COUNT, etmEnabled, WCB_Number come from WCB_Storage.h)

// The ETM wire struct lives in WCB.ino, so the envelope build + broadcast do
// too — this module just hands it a ready TLV payload.
extern void wdpBroadcast(const uint8_t *payload, int len);

// ---- Module state --------------------------------------------------------
bool wdpEnabled = true;
static WdpNeighbor wdpNeighbors[MAX_WCB_COUNT];

static const unsigned long WDP_PERIOD_MS = 60000UL;    // periodic backstop advert
static const unsigned long WDP_TTL_MS    = 180000UL;   // facts go stale after ~3 missed

static uint8_t       wdpBootLeft     = 0;
static unsigned long wdpNextBootMs   = 0;
static unsigned long wdpNextAdvertMs = 0;

// ---- TLV registry (payload rides structCommand[200]) --------------------
#define WDP_MAGIC        'W'
#define WDP_TLV_END      0x00
#define WDP_TLV_ALIAS    0x01
#define WDP_TLV_FWVER    0x03
#define WDP_TLV_HWVER    0x04
#define WDP_TLV_CAPFLAGS 0x05
#define WDP_TLV_MAESTRO  0x06

// ==================== Capability / Maestro helpers =======================

static uint16_t wdpCapFlags() {
  uint16_t f = 0;
  if (hcrConfig.configured)  f |= WDP_CAP_HCR;
  if (mp3Config.configured)  f |= WDP_CAP_MP3;
  if (wledConfig.configured) f |= WDP_CAP_WLED;
  if (Kyber_Local)           f |= WDP_CAP_KYBER_LOCAL;
  if (Maestro_Remote)        f |= WDP_CAP_MAESTRO_REM;
  if (pwmOutputCount > 0 || activePWMCount > 0) f |= WDP_CAP_PWM;
  if (specialPeerEnabled)    f |= WDP_CAP_CONTROLLER;
  for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
    if (maestroConfigs[i].configured && maestroConfigs[i].remoteWCB == 0 &&
        maestroConfigs[i].serialPort > 0) { f |= WDP_CAP_MAESTRO_LOC; break; }
  }
  return f;
}

// This board's PHYSICALLY-attached Maestro IDs (remoteWCB==0). Remote proxies
// (serialPort==0) are NOT advertised as local capabilities.
static int wdpLocalMaestroIds(uint8_t *out) {
  int n = 0;
  for (int i = 0; i < MAX_MAESTROS_PER_WCB && n < WDP_MAX_MAESTRO; i++) {
    if (maestroConfigs[i].configured && maestroConfigs[i].remoteWCB == 0 &&
        maestroConfigs[i].serialPort > 0) {
      out[n++] = maestroConfigs[i].maestroID;
    }
  }
  return n;
}

// ==================== TLV encode (our own advert) ========================

static int putTLV(uint8_t *buf, int o, int max, uint8_t type, const uint8_t *val, int len) {
  if (len < 0)   len = 0;
  if (len > 255) len = 255;
  if (o + 2 + len > max) return o;   // no room — skip this TLV
  buf[o++] = type;
  buf[o++] = (uint8_t)len;
  for (int i = 0; i < len; i++) buf[o++] = val[i];
  return o;
}

static int wdpBuildPayload(uint8_t *buf, int max) {
  int o = 0;
  if (max < 3) return 0;
  buf[o++] = WDP_MAGIC;
  buf[o++] = WDP_PROTO_VERSION;

  if (wcb_alias.length() > 0) {
    int L = wcb_alias.length(); if (L > 24) L = 24;
    o = putTLV(buf, o, max, WDP_TLV_ALIAS, (const uint8_t *)wcb_alias.c_str(), L);
  }
  {
    int L = SoftwareVersion.length(); if (L > 27) L = 27;
    o = putTLV(buf, o, max, WDP_TLV_FWVER, (const uint8_t *)SoftwareVersion.c_str(), L);
  }
  {
    uint8_t hv = (uint8_t)wcb_hw_version;
    o = putTLV(buf, o, max, WDP_TLV_HWVER, &hv, 1);
  }
  {
    uint16_t cf = wdpCapFlags();
    uint8_t v[2] = { (uint8_t)(cf & 0xFF), (uint8_t)(cf >> 8) };   // little-endian
    o = putTLV(buf, o, max, WDP_TLV_CAPFLAGS, v, 2);
  }
  {
    uint8_t ids[WDP_MAX_MAESTRO];
    int n = wdpLocalMaestroIds(ids);
    if (n > 0) o = putTLV(buf, o, max, WDP_TLV_MAESTRO, ids, n);
  }

  if (o < max) buf[o++] = WDP_TLV_END;   // explicit terminator (tail is 0 anyway)
  return o;
}

// ==================== Advert send (cadence) ==============================

static void wdpSendAdvert() {
  uint8_t payload[200];
  memset(payload, 0, sizeof(payload));
  int len = wdpBuildPayload(payload, sizeof(payload));
  wdpBroadcast(payload, len);
  if (debugEnabled) Serial.printf("[WDP] advert sent (%d B)\n", len);
}

void wdpTick() {
  if (!wdpEnabled || !etmEnabled) return;   // WDP rides the ETM broadcast layer
  unsigned long now = millis();

  if (wdpBootLeft > 0 && now >= wdpNextBootMs) {
    wdpSendAdvert();
    wdpBootLeft--;
    wdpNextBootMs = now + 1300;
  }
  if ((long)(now - wdpNextAdvertMs) >= 0) {
    wdpSendAdvert();
    wdpNextAdvertMs = now + WDP_PERIOD_MS;
  }
  // Age descriptive facts stale (keep the slot as topology memory).
  for (int i = 0; i < MAX_WCB_COUNT; i++) {
    if (wdpNeighbors[i].valid && wdpNeighbors[i].confirmed &&
        (now - wdpNeighbors[i].lastAdvertMs) > WDP_TTL_MS) {
      wdpNeighbors[i].confirmed = false;
    }
  }
}

// ==================== Advert receive (decode into table) =================
// Called from drainWdpPackets() in WCB.ino (loop context — Serial is safe here).

void wdpOnAdvertReceived(int senderWCB, const uint8_t *cmd) {
  if (!wdpEnabled) return;
  if (senderWCB < 1 || senderWCB > MAX_WCB_COUNT) return;
  if (cmd[0] != (uint8_t)WDP_MAGIC || cmd[1] != WDP_PROTO_VERSION) return;

  WdpNeighbor &nb = wdpNeighbors[senderWCB - 1];
  bool wasValid = nb.valid;
  memset(&nb, 0, sizeof(nb));            // replace this board's facts wholesale
  nb.valid        = true;
  nb.confirmed    = true;
  nb.wcbNumber    = (uint8_t)senderWCB;
  nb.lastAdvertMs = millis();

  int o = 2;                              // skip magic + version
  while (o + 2 <= 200) {
    uint8_t type = cmd[o];
    if (type == WDP_TLV_END) break;
    int len = cmd[o + 1];
    const uint8_t *val = &cmd[o + 2];
    if (o + 2 + len > 200) break;         // truncated / malformed
    switch (type) {
      case WDP_TLV_ALIAS: {
        int L = len > 24 ? 24 : len; memcpy(nb.alias, val, L); nb.alias[L] = '\0'; break;
      }
      case WDP_TLV_FWVER: {
        int L = len > 27 ? 27 : len; memcpy(nb.fwVer, val, L); nb.fwVer[L] = '\0'; break;
      }
      case WDP_TLV_HWVER:
        if (len >= 1) nb.hwVer = val[0];
        break;
      case WDP_TLV_CAPFLAGS:
        if (len >= 2) nb.capFlags = (uint16_t)val[0] | ((uint16_t)val[1] << 8);
        break;
      case WDP_TLV_MAESTRO: {
        int L = len > WDP_MAX_MAESTRO ? WDP_MAX_MAESTRO : len;
        memcpy(nb.maestroIds, val, L); nb.maestroCount = (uint8_t)L; break;
      }
      default: break;                     // unknown TLV — skipped by its length (forward-compatible)
    }
    o += 2 + len;
  }
  if (!wasValid)
    Serial.printf("[WDP] learned WCB%d%s%s\n", senderWCB, nb.alias[0] ? " " : "", nb.alias);
}

// ==================== Command / query ====================================

static void printWdpList() {
  Serial.println("---- WDP Neighbors ----");
  int count = 0;
  unsigned long now = millis();
  for (int i = 0; i < MAX_WCB_COUNT; i++) {
    WdpNeighbor &nb = wdpNeighbors[i];
    if (!nb.valid) continue;
    count++;
    char maestro[48] = "";
    for (int m = 0; m < nb.maestroCount && m < WDP_MAX_MAESTRO; m++) {
      char t[8]; snprintf(t, sizeof(t), "%s%d", m ? "." : "", nb.maestroIds[m]);
      strncat(maestro, t, sizeof(maestro) - strlen(maestro) - 1);
    }
    Serial.printf("[WDP:N=%d,ALIAS=%s,HW=%d,FW=%s,CAP=%04X,MAESTRO=%s,AGE=%lu,SEEN=%d]\n",
                  nb.wcbNumber, nb.alias, nb.hwVer, nb.fwVer, nb.capFlags,
                  maestro, (now - nb.lastAdvertMs) / 1000, nb.confirmed ? 1 : 0);
  }
  Serial.printf("---- End WDP (%d neighbor%s) ----\n", count, count == 1 ? "" : "s");
}

static void printWdpStatus() {
  int n = 0;
  for (int i = 0; i < MAX_WCB_COUNT; i++) if (wdpNeighbors[i].valid) n++;
  Serial.printf("[WDP:en=%d,proto=%d,neighbors=%d]\n", wdpEnabled ? 1 : 0, WDP_PROTO_VERSION, n);
}

void processWdpCommand(const String &args) {
  String a = args; a.trim();
  String au = a; au.toUpperCase();

  if (au == "" || au == "LIST") { printWdpList();   return; }
  if (au == "STATUS")           { printWdpStatus(); return; }
  if (au == "ON")  { wdpEnabled = true;  saveWdpSettings(); Serial.println("[WDP] enabled");  return; }
  if (au == "OFF") { wdpEnabled = false; saveWdpSettings(); Serial.println("[WDP] disabled"); return; }
  if (au == "CLEAR") { memset(wdpNeighbors, 0, sizeof(wdpNeighbors)); Serial.println("[WDP] neighbor table cleared"); return; }

  Serial.printf("[WDP] unknown subcommand '%s' (LIST/STATUS/ON/OFF/CLEAR)\n", a.c_str());
}

// ==================== NVS + lifecycle ====================================

void loadWdpSettings() {
  preferences.begin("wdp_cfg", true);
  wdpEnabled = preferences.getBool("en", true);   // default ON
  preferences.end();
}

void saveWdpSettings() {
  preferences.begin("wdp_cfg", false);
  preferences.putBool("en", wdpEnabled);
  preferences.end();
}

void wdpBegin() {
  loadWdpSettings();
  memset(wdpNeighbors, 0, sizeof(wdpNeighbors));
  wdpBootLeft   = 3;
  wdpNextBootMs = millis() + 1600;   // just after the ETM boot announce (1500)
  // Per-board phase stagger so periodic backstops from boards that booted
  // together don't all fire in the same tick.
  wdpNextAdvertMs = millis() + WDP_PERIOD_MS + (unsigned long)((WCB_Number % 16) * 500);
  if (wdpEnabled) Serial.println("[WDP] discovery enabled");
}
