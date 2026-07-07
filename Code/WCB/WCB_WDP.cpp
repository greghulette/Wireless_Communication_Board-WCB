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
extern char        LocalFunctionIdentifier;   // the '?' function-command prefix
extern String      serialPortLabels[5];       // RAW per-port labels ("" = unlabeled).
                                              // NOT getSerialLabel() — that decorates as
                                              // "Serial<N> (<label>)", which would eat the
                                              // advert's 24-char budget and clip long names.
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
#define WDP_TLV_PORTLABEL 0x09   // [port(1)][label bytes] — one TLV per labeled serial port
#define WDP_TLV_CTRLID   0x0A    // [id(1)] — controller (special-peer) ID; sent only when linked
// ---- Device-identity TLVs (shared with WDP-DA + the WCB_Client library) ----
// A WCB_Client device advertises these instead of the WCB-specific TLVs above.
// Presence of DEVTYPE marks the sender as a client device, not a WCB.
#define WDP_TLV_DEVTYPE  0x0B    // string — canonical device type name (from the vocabulary)
#define WDP_TLV_HWREV    0x0C    // string — hardware revision (distinct from numeric HWVER)
#define WDP_TLV_CAPTAGS  0x0D    // string — space-separated capability tags (optional)

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
  if (specialPeerEnabled) {                     // which controller this board links to
    uint8_t id = WCB_SPECIAL_PEER_ID;
    o = putTLV(buf, o, max, WDP_TLV_CTRLID, &id, 1);
  }
  {
    uint8_t ids[WDP_MAX_MAESTRO];
    int n = wdpLocalMaestroIds(ids);
    if (n > 0) o = putTLV(buf, o, max, WDP_TLV_MAESTRO, ids, n);
  }
  // Per-port interface labels (the ?LABEL / device names) — one TLV per labeled
  // port: [port][label]. Advertised last so the core identity always fits; a
  // label set that would overflow the 200 B payload is dropped gracefully.
  for (int p = 1; p <= 5; p++) {
    String lbl = serialPortLabels[p - 1];   // raw label, not the "Serial<N> (...)" form
    if (lbl.length() == 0) continue;         // unlabeled port — don't advertise
    int L = lbl.length(); if (L > 24) L = 24;
    uint8_t v[25];
    v[0] = (uint8_t)p;
    memcpy(v + 1, lbl.c_str(), L);
    o = putTLV(buf, o, max, WDP_TLV_PORTLABEL, v, 1 + L);
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
      case WDP_TLV_CTRLID:
        if (len >= 1) nb.ctrlId = val[0];
        break;
      // ---- Client-device identity (WCB_Client) ----------------------------
      case WDP_TLV_DEVTYPE: {   // device type name doubles as the neighbor's alias
        int L = len > 24 ? 24 : len; memcpy(nb.alias, val, L); nb.alias[L] = '\0';
        nb.isClient = true; break;
      }
      case WDP_TLV_HWREV: {
        int L = len > 15 ? 15 : len; memcpy(nb.hwRev, val, L); nb.hwRev[L] = '\0'; break;
      }
      case WDP_TLV_CAPTAGS: {
        int L = len > 48 ? 48 : len; memcpy(nb.capTags, val, L); nb.capTags[L] = '\0'; break;
      }
      case WDP_TLV_MAESTRO: {
        int L = len > WDP_MAX_MAESTRO ? WDP_MAX_MAESTRO : len;
        memcpy(nb.maestroIds, val, L); nb.maestroCount = (uint8_t)L; break;
      }
      case WDP_TLV_PORTLABEL: {
        if (len >= 1) {
          int port = val[0];
          if (port >= 1 && port <= 5) {
            int L = len - 1; if (L > 24) L = 24;
            memcpy(nb.portLabels[port - 1], val + 1, L);
            nb.portLabels[port - 1][L] = '\0';
          }
        }
        break;
      }
      default: break;                     // unknown TLV — skipped by its length (forward-compatible)
    }
    o += 2 + len;
  }
  if (!wasValid)
    Serial.printf("[WDP] learned WCB%d%s%s\n", senderWCB, nb.alias[0] ? " " : "", nb.alias);
}

// ==================== Alias resolution ===================================
// Case-insensitive alias -> WCB number over the RAM neighbor table. Returns the
// number on a UNIQUE match, 0 if no board advertises that alias, or -1 if more
// than one does (ambiguous — the caller tells the user to use the number). The
// local board is intentionally excluded; the caller matches wcb_alias first.
int wdpResolveAlias(const char *alias) {
  if (!alias || !alias[0]) return 0;
  String want = alias;
  int number = 0, matches = 0;
  for (int i = 0; i < MAX_WCB_COUNT; i++) {
    const WdpNeighbor &nb = wdpNeighbors[i];
    if (!nb.valid || nb.alias[0] == '\0') continue;
    if (want.equalsIgnoreCase(nb.alias)) { matches++; number = nb.wcbNumber; }
  }
  if (matches == 0) return 0;
  if (matches > 1)  return -1;
  return number;
}

// ==================== Command / query ====================================

// Chip family name from the HW version number.
static const char *wdpHwName(uint8_t hw) {
  if (hw >= 31) return "ESP32-S3";
  if (hw >  0)  return "ESP32";
  return "?";
}

// Compact single-letter capability codes for the summary table (e.g. "M C R").
static void wdpCapCodes(uint16_t cf, char *out, int max) {
  static const struct { uint16_t bit; char code; } CODES[] = {
    { WDP_CAP_MAESTRO_LOC, 'M' }, { WDP_CAP_MAESTRO_REM, 'R' },
    { WDP_CAP_KYBER_LOCAL, 'K' }, { WDP_CAP_HCR, 'H' }, { WDP_CAP_MP3, '3' },
    { WDP_CAP_WLED, 'W' }, { WDP_CAP_PWM, 'P' }, { WDP_CAP_CONTROLLER, 'C' },
  };
  int o = 0;
  for (unsigned i = 0; i < sizeof(CODES) / sizeof(CODES[0]); i++) {
    if (cf & CODES[i].bit) {
      if (o && o < max - 1) out[o++] = ' ';
      if (o < max - 1)      out[o++] = CODES[i].code;
    }
  }
  if (o == 0 && max > 1) out[o++] = '-';
  out[o] = '\0';
}

// Spelled-out capability names for the detail view.
static void wdpCapNames(uint16_t cf, char *out, int max) {
  static const struct { uint16_t bit; const char *name; } NAMES[] = {
    { WDP_CAP_MAESTRO_LOC, "Maestro host" }, { WDP_CAP_MAESTRO_REM, "Maestro remote" },
    { WDP_CAP_KYBER_LOCAL, "Kyber local" }, { WDP_CAP_HCR, "HCR" }, { WDP_CAP_MP3, "MP3" },
    { WDP_CAP_WLED, "WLED" }, { WDP_CAP_PWM, "PWM" }, { WDP_CAP_CONTROLLER, "Controller link" },
  };
  int o = 0; out[0] = '\0';
  for (unsigned i = 0; i < sizeof(NAMES) / sizeof(NAMES[0]); i++) {
    if (!(cf & NAMES[i].bit)) continue;
    int need = (int)strlen(NAMES[i].name) + (o ? 2 : 0);
    if (o + need >= max - 1) break;
    if (o) { out[o++] = ','; out[o++] = ' '; }
    strcpy(out + o, NAMES[i].name); o += strlen(NAMES[i].name);
  }
  if (o == 0 && max > 5) strcpy(out, "none");
}

// Dot-joined local Maestro IDs ("-" when none).
static void wdpMaestroStr(const WdpNeighbor &nb, char *out, int max) {
  if (nb.maestroCount == 0) { if (max > 1) { out[0] = '-'; out[1] = '\0'; } return; }
  int o = 0; out[0] = '\0';
  for (int m = 0; m < nb.maestroCount && m < WDP_MAX_MAESTRO; m++) {
    char t[8]; snprintf(t, sizeof(t), "%s%d", m ? "." : "", nb.maestroIds[m]);
    int need = strlen(t);
    if (o + need >= max - 1) break;
    strcpy(out + o, t); o += need;
  }
}

// Summary table — `show cdp neighbors` for the WCB mesh.
static void printWdpList() {
  Serial.println();
  Serial.println("Capability codes: M=Maestro host  R=Maestro remote  K=Kyber  H=HCR  3=MP3  W=WLED  P=PWM  C=Controller link");
  Serial.println();
  Serial.printf("%-4s  %-16s  %-10s  %-12s  %-10s  %-5s  %-5s\n",
                "WCB", "Alias", "Platform", "Cap", "Maestros", "Age", "State");
  Serial.printf("%-4s  %-16s  %-10s  %-12s  %-10s  %-5s  %-5s\n",
                "----", "----------------", "----------", "------------", "----------", "-----", "-----");
  int count = 0;
  unsigned long now = millis();
  for (int i = 0; i < MAX_WCB_COUNT; i++) {
    WdpNeighbor &nb = wdpNeighbors[i];
    if (!nb.valid) continue;
    count++;
    char cap[24]; char maestro[24];
    const char *platform;
    if (nb.isClient) {                       // client device — no WCB serial-port facts
      platform = "client";
      strcpy(cap, "-"); strcpy(maestro, "-");
    } else {
      platform = wdpHwName(nb.hwVer);
      wdpCapCodes(nb.capFlags, cap, sizeof(cap));
      wdpMaestroStr(nb, maestro, sizeof(maestro));
    }
    char ageStr[12];  snprintf(ageStr, sizeof(ageStr), "%lus", (now - nb.lastAdvertMs) / 1000);
    Serial.printf("%-4d  %-16.16s  %-10.10s  %-12.12s  %-10.10s  %-5s  %-5s\n",
                  nb.wcbNumber, nb.alias[0] ? nb.alias : "-", platform,
                  cap, maestro, ageStr, nb.confirmed ? "live" : "stale");
  }
  if (count == 0) Serial.println("(no neighbors discovered yet)");
  Serial.printf("\nTotal WDP neighbors: %d   (%cWDP,<n> for detail)\n", count, LocalFunctionIdentifier);
}

// Detail view — `show cdp neighbors detail` for one board.
static void printWdpDetail(int wcbNum) {
  if (wcbNum < 1 || wcbNum > MAX_WCB_COUNT || !wdpNeighbors[wcbNum - 1].valid) {
    Serial.printf("[WDP] no neighbor WCB%d — see %cWDP,LIST\n", wcbNum, LocalFunctionIdentifier);
    return;
  }
  WdpNeighbor &nb = wdpNeighbors[wcbNum - 1];
  Serial.println();

  // ---- Client device (WCB_Client) — device-style detail --------------------
  if (nb.isClient) {
    Serial.printf("==== Device %d  \"%s\" ====\n", nb.wcbNumber, nb.alias[0] ? nb.alias : "(unnamed)");
    Serial.printf("  Type        : %s\n", nb.alias[0] ? nb.alias : "?");
    Serial.printf("  Firmware    : %s\n", nb.fwVer[0] ? nb.fwVer : "?");
    if (nb.hwRev[0])   Serial.printf("  Hardware    : %s\n", nb.hwRev);
    if (nb.capTags[0]) Serial.printf("  Capabilities: %s\n", nb.capTags);
    Serial.printf("  Last advert : %lus ago  (%s)\n", (millis() - nb.lastAdvertMs) / 1000,
                  nb.confirmed ? "live" : "stale");
    Serial.println();
    return;
  }

  // ---- WCB neighbor --------------------------------------------------------
  char caps[128];    wdpCapNames(nb.capFlags, caps, sizeof(caps));
  char maestro[24];  wdpMaestroStr(nb, maestro, sizeof(maestro));
  Serial.printf("==== WCB %d  \"%s\" ====\n", nb.wcbNumber, nb.alias[0] ? nb.alias : "(no alias)");
  Serial.printf("  Platform    : %s (hw %d)\n", wdpHwName(nb.hwVer), nb.hwVer);
  Serial.printf("  Firmware    : %s\n", nb.fwVer[0] ? nb.fwVer : "?");
  if ((nb.capFlags & WDP_CAP_CONTROLLER) && nb.ctrlId > 0)
    Serial.printf("  Capabilities: %s  (controller ID %d)\n", caps, nb.ctrlId);
  else
    Serial.printf("  Capabilities: %s\n", caps);
  Serial.printf("  Maestros    : %s\n", maestro);
  Serial.printf("  Last advert : %lus ago  (%s)\n", (millis() - nb.lastAdvertMs) / 1000,
                nb.confirmed ? "live" : "stale");
  Serial.println("  Interfaces  :");
  bool any = false;
  for (int p = 0; p < 5; p++) {
    if (nb.portLabels[p][0]) { Serial.printf("    S%d  %s\n", p + 1, nb.portLabels[p]); any = true; }
  }
  if (!any) Serial.println("    (none advertised)");
  Serial.println();
}

// Machine-readable dump (for the config tool / scripts).
static void printWdpDump() {
  int count = 0;
  unsigned long now = millis();
  for (int i = 0; i < MAX_WCB_COUNT; i++) {
    WdpNeighbor &nb = wdpNeighbors[i];
    if (!nb.valid) continue;
    count++;
    char maestro[48]; wdpMaestroStr(nb, maestro, sizeof(maestro));
    Serial.printf("[WDP:N=%d,CLIENT=%d,ALIAS=%s,HW=%d,HWREV=%s,FW=%s,CAP=%04X,CTRL=%d,CAPTAGS=%s,MAESTRO=%s,AGE=%lu,SEEN=%d]\n",
                  nb.wcbNumber, nb.isClient ? 1 : 0, nb.alias, nb.hwVer, nb.hwRev, nb.fwVer,
                  nb.capFlags, nb.ctrlId, nb.capTags, maestro,
                  (now - nb.lastAdvertMs) / 1000, nb.confirmed ? 1 : 0);
  }
  Serial.printf("[WDP:END,count=%d]\n", count);
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
  if (au == "DUMP")             { printWdpDump();    return; }
  if (au == "ON")  { wdpEnabled = true;  saveWdpSettings(); Serial.println("[WDP] enabled");  return; }
  if (au == "OFF") { wdpEnabled = false; saveWdpSettings(); Serial.println("[WDP] disabled"); return; }
  if (au == "CLEAR") { memset(wdpNeighbors, 0, sizeof(wdpNeighbors)); Serial.println("[WDP] neighbor table cleared"); return; }

  // ?WDP,DETAIL,<n>  or  ?WDP,<n>  → drill into one neighbor
  if (au.startsWith("DETAIL")) {
    int c = a.indexOf(',');
    printWdpDetail(c >= 0 ? a.substring(c + 1).toInt() : 0);
    return;
  }
  int n = a.toInt();
  if (n > 0) { printWdpDetail(n); return; }

  Serial.printf("[WDP] unknown subcommand '%s' (LIST | <n> | DETAIL,n | STATUS | DUMP | ON | OFF | CLEAR)\n", a.c_str());
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
