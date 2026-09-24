#include "WCB_RemoteTerm.h"   // Must be first — redirects Serial -> WCBDebugSerial
#include "WCB_WDP.h"
#include "WCB_Storage.h"      // MAX_WCB_COUNT, etmEnabled, WCB_Number externs
#include "WCB_HCR.h"          // hcrConfig
#include "WCB_MP3.h"          // mp3Config
#include "WCB_DFP.h"          // dfpConfig
#include "WCB_WLED.h"         // wledConfig
#include "WCB_Maestro.h"      // maestroConfigs[], MAX_MAESTROS_PER_WCB
#include "WCB_PWM.h"          // pwmOutputCount, pwmMappings[]
#include <Preferences.h>
#include <strings.h>          // strcasecmp (?WDP,DA,FORGET type match)

// ---- Externs provided by WCB.ino ----------------------------------------
extern String      wcb_alias;         // board alias ("" = unset, sanitized, <=24)
extern int         wcb_hw_version;    // 0/1/21/23/24/31/32
extern String      SoftwareVersion;   // firmware version string
extern bool        Kyber_Local;
extern bool        Maestro_Remote;
extern bool        specialPeerEnabled;
extern uint8_t     WCB_SPECIAL_PEER_ID;   // controller/special peer id — excluded from learned auto-join
extern bool        debugEnabled;
extern bool        debugMGMT;         // remote-management debug level (?DEBUG,MGMT,ON) — WDP rides here
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
// Same envelope, PACKET_TYPE_WDP_DA: one frame of this board's WDP-DA device list.
extern void wdpDaBroadcast(const uint8_t *payload, int len);
// Auto-config action: enable this board's controller (special) peer, live.
extern void enableControllerPeer(uint8_t id);
// Dynamic peer membership (WCB.ino): learn regular WCBs from their adverts.
extern bool    addActivePeer(uint8_t id, bool learned);
extern bool    addTemporaryPeer(uint8_t id);   // TEMPORARY peer: live but not persisted (see WCB.ino)
extern void    removeActivePeer(uint8_t id);
extern void    clearAllLearnedPeers();
extern int     activePeerCount();
extern bool    wcbPeerActive[MAX_WCB_COUNT];
extern bool    wcbPeerLearned[MAX_WCB_COUNT];
extern bool    wcbPeerTemporary[MAX_WCB_COUNT];
extern uint8_t wcbPeerAdvertCount[MAX_WCB_COUNT];
extern bool    wcbPeerOnline(uint8_t id);   // registered peer AND currently ETM-online (for cap routing)

// ---- Module state --------------------------------------------------------
bool wdpEnabled  = true;
bool wdpAutoJoin = true;   // learn regular WCBs from their adverts (default ON)
static WdpNeighbor wdpNeighbors[MAX_WCB_COUNT];

static const unsigned long WDP_PERIOD_MS = 60000UL;    // periodic backstop advert
static const unsigned long WDP_TTL_MS    = 180000UL;   // facts go stale after ~3 missed

static uint8_t       wdpBootLeft     = 0;
static unsigned long wdpNextBootMs   = 0;
static unsigned long wdpNextAdvertMs = 0;
// On-change re-announce: hash of the last-SENT advert payload + when to next check
// whether the payload changed. Lets a config edit (device added/cleared, alias/label
// changed, capability flipped) propagate in ~½s instead of the ~60s periodic backstop,
// with NO need to instrument every config path — we detect any content change directly.
static uint32_t      wdpLastAdvertHash   = 0;
static unsigned long wdpNextDirtyCheckMs = 0;

// WDP-DA pieces the advert path uses (defined in the WDP-DA section below).
static void wdpDaLabel(int port, char *out, size_t cap);
static void wdpDaStartSend();
static void wdpDaCheckChanged(unsigned long now);
static void wdpDaTxTick(unsigned long now);

// ---- TLV registry (payload rides structCommand[200]) --------------------
#define WDP_MAGIC        'W'
#define WDP_TLV_END      0x00
#define WDP_TLV_ALIAS    0x01
#define WDP_TLV_FWVER    0x03
#define WDP_TLV_HWVER    0x04
#define WDP_TLV_CAPFLAGS 0x05
#define WDP_TLV_MAESTRO  0x06
#define WDP_TLV_MAESTRO_CFG 0x0E // [id(1)][baudCode(1)] records — rich Maestro id+baud for auto-config
#define WDP_TLV_WLED_CFG    0x0F // [id(1)][baudCode(1)] records — WLED id+baud for auto-config
#define WDP_TLV_PORTLABEL 0x09   // [port(1)][label bytes] — one TLV per labeled serial port
#define WDP_TLV_CTRLID   0x0A    // [id(1)] — controller (special-peer) ID; sent only when linked
// ---- Device-identity TLVs (shared with WDP-DA + the WCB_Client library) ----
// A WCB_Client device advertises these instead of the WCB-specific TLVs above.
// Presence of DEVTYPE marks the sender as a client device, not a WCB.
#define WDP_TLV_DEVTYPE  0x0B    // string — canonical device type name (from the vocabulary)
#define WDP_TLV_HWREV    0x0C    // string — hardware revision (distinct from numeric HWVER)
#define WDP_TLV_CAPTAGS  0x0D    // string — space-separated capability tags (optional)
#define WDP_TLV_PWMTARGET 0x10   // [targetWCB(1)][port(1)] pairs — this board's REMOTE PWM
                                 // outputs. Each named board self-configures that output port
                                 // (receiver-side), so a target that was powered off when the
                                 // mapping was made still sets itself up on its next advert.
#define WDP_TLV_SOLICIT  0x11    // len 0 — a "please advertise now" request (?WDP,POLL). Carries
                                 // no facts; a receiver schedules a prompt advert and does NOT
                                 // record the solicit as a neighbor (it would wipe real facts).
#define WDP_TLV_SEQHASH  0x13    // [hash:4 LE] — FNV-1a over this board's stored-sequence
                                 // key_list. A change means "my sequence inventory moved,
                                 // re-pull it with ?MGMT,SEQ,<n>". Names themselves are NOT
                                 // advertised: ~16 B each would evict the port labels from
                                 // the 200 B payload. 4 bytes buys the same freshness signal.
                                 // NOTE: draft ids 0x02/0x07/0x08 are burned (never shipped,
                                 // see docs/WDP_DESIGN.md §10) — do not recycle them.
#define WDP_TLV_FLAGS    0x12    // [flags:1] — advert flags bitmap (below). A device sets bits to
                                 // change how remotes treat it. Absent = all-zero (default).
#define WDP_ADVFLAG_TEMPORARY 0x01  // "I'm TEMPORARY" — adopt me as a live-but-not-persisted peer
                                    // (gone on reboot, evicted on silence), never a permanent one.
                                    // For occasional devices like a management relay.

// ==================== Capability / Maestro helpers =======================

static uint16_t wdpCapFlags() {
  uint16_t f = 0;
  if (hcrConfig.configured)  f |= WDP_CAP_HCR;
  if (mp3Config.configured)  f |= WDP_CAP_MP3;
  if (dfpConfig.configured)  f |= WDP_CAP_DFPLAYER;
  if (Kyber_Local)           f |= WDP_CAP_KYBER_LOCAL;
  if (Maestro_Remote)        f |= WDP_CAP_MAESTRO_REM;
  // activePWMCount used to stand in for 'has an input mapping' - it was declared, read here,
  // and assigned nowhere, so a board whose PWM is input mappings only never advertised the
  // capability. Ask the mappings directly; there is nothing to keep in step.
  bool anyPwmMapping = false;
  for (int i = 0; i < MAX_PWM_MAPPINGS; i++) {
    if (pwmMappings[i].active) { anyPwmMapping = true; break; }
  }
  if (pwmOutputCount > 0 || anyPwmMapping) f |= WDP_CAP_PWM;
  if (specialPeerEnabled)    f |= WDP_CAP_CONTROLLER;
  for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
    if (maestroConfigs[i].configured && maestroConfigs[i].remoteWCB == 0 &&
        maestroConfigs[i].serialPort > 0) { f |= WDP_CAP_MAESTRO_LOC; break; }
  }
  for (int i = 0; i < MAX_WLED_PER_WCB; i++) {   // WDP_CAP_WLED = this board hosts a WLED
    if (wledConfigs[i].configured && wledConfigs[i].remoteWCB == 0 &&
        wledConfigs[i].serialPort > 0) { f |= WDP_CAP_WLED; break; }
  }
  return f;
}

// Ordered, APPEND-ONLY Maestro baud table. The array index IS the WDP wire code
// carried in WDP_TLV_MAESTRO_CFG — never reorder or remove entries or the codes
// drift between firmware versions. Mirrors the baud whitelist validated in
// configureMaestro() (WCB_Maestro.cpp); keep the two in sync.
static const uint32_t WDP_BAUD_TABLE[] = {
  0, 110, 300, 600, 1200, 2400, 9600, 14400,
  19200, 38400, 57600, 115200, 128000, 256000
};
static const uint8_t WDP_BAUD_N = sizeof(WDP_BAUD_TABLE) / sizeof(WDP_BAUD_TABLE[0]);
static uint8_t  wdpBaudToCode(uint32_t b) {
  for (uint8_t i = 0; i < WDP_BAUD_N; i++) if (WDP_BAUD_TABLE[i] == b) return i;
  return 0xFF;   // unrecognized baud → advertise "unknown"; receivers won't auto-config it
}
static uint32_t wdpCodeToBaud(uint8_t c) { return c < WDP_BAUD_N ? WDP_BAUD_TABLE[c] : 0; }

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

// Same selection as wdpLocalMaestroIds, but also emits each Maestro's baud as a
// wire code — feeds the rich WDP_TLV_MAESTRO_CFG so neighbors can auto-configure
// a remote proxy (id + host + baud) without a manual ?MAESTRO.
static int wdpLocalMaestroCfg(uint8_t *ids, uint8_t *codes) {
  int n = 0;
  for (int i = 0; i < MAX_MAESTROS_PER_WCB && n < WDP_MAX_MAESTRO; i++) {
    if (maestroConfigs[i].configured && maestroConfigs[i].remoteWCB == 0 &&
        maestroConfigs[i].serialPort > 0) {
      ids[n]   = maestroConfigs[i].maestroID;
      codes[n] = wdpBaudToCode(maestroConfigs[i].baudRate);
      n++;
    }
  }
  return n;
}

// This board's PHYSICALLY-attached WLED IDs + baud codes — feeds WDP_TLV_WLED_CFG
// so neighbors can auto-configure a remote WLED proxy (id + host + baud). Mirrors
// wdpLocalMaestroCfg; remote proxies (serialPort==0) are NOT re-advertised.
static int wdpLocalWLEDCfg(uint8_t *ids, uint8_t *codes) {
  int n = 0;
  for (int i = 0; i < MAX_WLED_PER_WCB && n < WDP_MAX_WLED; i++) {
    if (wledConfigs[i].configured && wledConfigs[i].remoteWCB == 0 &&
        wledConfigs[i].serialPort > 0) {
      ids[n]   = wledConfigs[i].wledID;
      codes[n] = wdpBaudToCode(wledConfigs[i].baudRate);
      n++;
    }
  }
  return n;
}

// This board's PWM REMOTE-output targets: the (WCB,port) pairs its active PWM
// mappings drive on OTHER boards. Local outputs (wcbNumber 0 or self) are excluded
// — the local board configures those itself. Fills [targetWCB][port] records for
// WDP_TLV_PWMTARGET so each targeted board self-configures its output port even if
// it was powered off when the mapping was created. Deduped; capped at
// WDP_MAX_PWMTARGET pairs. Unlike Maestro/WLED (which advertise INCOMING routes for
// others to proxy TO this board), this advertises OUTGOING config for the target
// board to apply to itself.
static int wdpLocalPwmTargets(uint8_t *rec) {
  int n = 0;
  for (int i = 0; i < MAX_PWM_MAPPINGS && n < WDP_MAX_PWMTARGET; i++) {
    if (!pwmMappings[i].active) continue;
    for (int j = 0; j < pwmMappings[i].outputCount && n < WDP_MAX_PWMTARGET; j++) {
      int wcb  = pwmMappings[i].outputs[j].wcbNumber;
      int port = pwmMappings[i].outputs[j].serialPort;
      if (wcb <= 0 || wcb == WCB_Number) continue;   // local output — no advert needed
      if (port < 1 || port > 5)          continue;
      bool dup = false;                              // collapse duplicate (wcb,port)
      for (int k = 0; k < n; k++)
        if (rec[k * 2] == wcb && rec[k * 2 + 1] == port) { dup = true; break; }
      if (dup) continue;
      rec[n * 2]     = (uint8_t)wcb;
      rec[n * 2 + 1] = (uint8_t)port;
      n++;
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
    uint8_t ids[WDP_MAX_MAESTRO], codes[WDP_MAX_MAESTRO];
    int n = wdpLocalMaestroCfg(ids, codes);
    if (n > 0) {
      o = putTLV(buf, o, max, WDP_TLV_MAESTRO, ids, n);   // legacy id-only (old receivers + display)
      uint8_t rec[WDP_MAX_MAESTRO * 2];                    // [id][baudCode] pairs
      for (int i = 0; i < n; i++) { rec[i * 2] = ids[i]; rec[i * 2 + 1] = codes[i]; }
      o = putTLV(buf, o, max, WDP_TLV_MAESTRO_CFG, rec, n * 2);
    }
  }
  {
    uint8_t ids[WDP_MAX_WLED], codes[WDP_MAX_WLED];
    int n = wdpLocalWLEDCfg(ids, codes);
    if (n > 0) {
      uint8_t rec[WDP_MAX_WLED * 2];                       // [id][baudCode] pairs
      for (int i = 0; i < n; i++) { rec[i * 2] = ids[i]; rec[i * 2 + 1] = codes[i]; }
      o = putTLV(buf, o, max, WDP_TLV_WLED_CFG, rec, n * 2);
    }
  }
  // PWM remote-output targets (see wdpLocalPwmTargets): tell every board this one
  // drives to reserve its output port, so the target self-configures even if it was
  // powered off when the mapping was created. Advertised BEFORE the (cosmetic) port
  // labels so this functional config wins the payload budget. Only sent when this
  // board actually has remote PWM outputs.
  {
    uint8_t rec[WDP_MAX_PWMTARGET * 2];
    int n = wdpLocalPwmTargets(rec);
    if (n > 0) o = putTLV(buf, o, max, WDP_TLV_PWMTARGET, rec, n * 2);
  }
  // Stored-sequence inventory fingerprint. 6 bytes on the wire, and it goes in
  // BEFORE the port labels for the same reason PWMTARGET does: it drives a
  // consumer's re-pull decision, whereas labels are cosmetic. Always advertised
  // (an empty set has a well-defined hash), so a peer can distinguish "no
  // sequences" from "old firmware that never sends this".
  {
    uint32_t h = sequenceInventoryHash();
    uint8_t v[4] = { (uint8_t)(h & 0xFF), (uint8_t)((h >> 8) & 0xFF),
                     (uint8_t)((h >> 16) & 0xFF), (uint8_t)((h >> 24) & 0xFF) };  // little-endian
    o = putTLV(buf, o, max, WDP_TLV_SEQHASH, v, 4);
  }
  // Per-port interface labels (the ?LABEL / device names) — one TLV per labeled
  // port: [port][label]. Advertised last so the core identity always fits; a
  // label set that would overflow the 200 B payload is dropped gracefully.
  for (int p = 1; p <= 5; p++) {
    String lbl = serialPortLabels[p - 1];   // raw label, not the "Serial<N> (...)" form
    if (lbl.length() == 0) {                 // unlabeled → fall back to a WDP-DA detected device type
      char t[25]; wdpDaLabel(p, t, sizeof(t)); lbl = t;
    }
    if (lbl.length() == 0) continue;         // still nothing — don't advertise
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

// Cheap FNV-1a over the advert bytes — a content fingerprint, not security.
static uint32_t wdpFnv1a(const uint8_t *d, int n) {
  uint32_t h = 2166136261u;
  for (int i = 0; i < n; i++) { h ^= d[i]; h *= 16777619u; }
  return h;
}

static void wdpSendAdvert() {
  uint8_t payload[200];
  memset(payload, 0, sizeof(payload));
  int len = wdpBuildPayload(payload, sizeof(payload));
  wdpBroadcast(payload, len);
  wdpLastAdvertHash = wdpFnv1a(payload, len);   // remember what we just advertised
  if (debugMGMT) Serial.printf("[WDP] advert sent (%d B)\n", len);   // mesh-mgmt chatter — under ?DEBUG,MGMT
  wdpDaStartSend();   // the WDP-DA device list follows every advert, on its own packet type
}

// Broadcast a bare SOLICIT — "everyone advertise now" — so a fresh/renamed board or an
// operator opening the Wizard can populate the whole mesh in ~1 s instead of waiting up
// to the 60 s backstop. Carries no facts (see WDP_TLV_SOLICIT).
static void wdpSendSolicit() {
  uint8_t p[8];
  int o = 0;
  p[o++] = WDP_MAGIC;
  p[o++] = WDP_PROTO_VERSION;
  p[o++] = WDP_TLV_SOLICIT;
  p[o++] = 0;                // len 0
  p[o++] = WDP_TLV_END;
  wdpBroadcast(p, o);
  if (debugMGMT) Serial.println("[WDP] solicit broadcast");
}

// A solicit was heard — arm ONE prompt advert, staggered by board number so a fleet
// doesn't answer in lockstep (a broadcast storm). Reuses the boot-burst timer: arm it
// if idle, or pull an already-pending burst earlier; never lengthens a larger burst.
static void wdpArmSolicitedAdvert() {
  unsigned long now    = millis();
  unsigned long jitter = (unsigned long)((WCB_Number % 16) * 40);   // 0..600 ms stagger
  if (wdpBootLeft < 1) { wdpBootLeft = 1; wdpNextBootMs = now + jitter; }
  else if ((long)((now + jitter) - wdpNextBootMs) < 0) wdpNextBootMs = now + jitter;
  if (debugMGMT) Serial.println("[WDP] solicited — advertising shortly");
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
  // On-change re-announce: if the advert content changed since we last sent one (a
  // device was configured/cleared, alias/label edited, a capability flipped), send
  // now instead of waiting for the ~60s backstop. Hash the payload ~2x/sec — a burst
  // of config commands collapses into one or two adverts. No config path needs to know
  // about WDP; any change to what wdpBuildPayload emits is detected here.
  if ((long)(now - wdpNextDirtyCheckMs) >= 0) {
    wdpNextDirtyCheckMs = now + 500;
    uint8_t buf[200]; memset(buf, 0, sizeof(buf));
    int len = wdpBuildPayload(buf, sizeof(buf));
    uint32_t h = wdpFnv1a(buf, len);
    if (wdpLastAdvertHash == 0) {
      wdpLastAdvertHash = h;              // first check — establish the baseline, don't advert
    } else if (h != wdpLastAdvertHash) {
      wdpSendAdvert();                    // content changed — announce now (updates the hash)
      if (wdpBootLeft < 1) { wdpBootLeft = 1; wdpNextBootMs = now + 800; }  // 1 re-send for reliability
    }
    wdpDaCheckChanged(now);               // the device list changed on its own (appeared, quiet, forgotten)
  }
  wdpDaTxTick(now);                       // next device-list frame, and its armed re-send
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

// In-place replace bytes that would corrupt the comma/bracket-delimited
// ?WDP,DUMP record (',' ']' and control chars) with '_'. Applied to
// wire-sourced neighbor strings after decode.
static void wdpScrub(char *s) {
  for (; *s; ++s)
    if (*s == ',' || *s == ']' || (uint8_t)*s < 0x20) *s = '_';
}

// A device type whose announce should auto-enable this board's controller
// (special) peer. Case-insensitive; \xC3\xA9 is the UTF-8 'é' so both the
// accented ("Sabé") and plain ("Sabe") spellings match.
static bool wdpIsControllerType(const char *type) {
  static const char *CONTROLLERS[] = { "NaviCore", "Sab\xC3\xA9", "Sabe" };
  for (unsigned i = 0; i < sizeof(CONTROLLERS) / sizeof(CONTROLLERS[0]); i++)
    if (String(type).equalsIgnoreCase(CONTROLLERS[i])) return true;
  return false;
}

// Does any WCB neighbor advertise the given capability bit? (WCB fact — client
// devices carry capabilities as text tags, not in this bitmap, so they're skipped.)
static bool wdpMeshHasCap(uint16_t capBit) {
  for (int i = 0; i < MAX_WCB_COUNT; i++) {
    if (!wdpNeighbors[i].valid || wdpNeighbors[i].isClient) continue;
    if (wdpNeighbors[i].capFlags & capBit) return true;
  }
  return false;
}

// Owner of a capability = the LOWEST-numbered board that advertises it AND is a
// reachable, ETM-ONLINE peer — INCLUDING this board (self is judged by the same
// wdpCapFlags() predicate that built our own advert; self is always reachable).
// Returns 0 if nobody — not even self — owns it. Client devices are skipped
// (their caps are free-text tags, not this bitmap); a client can ISSUE a trigger
// but is never a routing owner.
//
// A REMOTE owner is gated on wcbPeerOnline() (registered peer + ETM-online),
// NOT on WDP's nb.confirmed: WDP's TTL (~180 s) is far longer than the ETM
// offline window (~33 s), so a just-powered-off board stays WDP-confirmed and a
// routed trigger sent to it would black-hole. Gating on ETM-online skips it and
// lets election fall to the next-lowest live owner (or local).
int wdpCapOwner(uint16_t capBit) {
  int owner = (wdpCapFlags() & capBit) ? WCB_Number : 0;   // self: always reachable locally
  for (int i = 0; i < MAX_WCB_COUNT; i++) {
    WdpNeighbor &nb = wdpNeighbors[i];
    if (!nb.valid || nb.isClient)     continue;
    if (!(nb.capFlags & capBit))      continue;
    if (!wcbPeerOnline(nb.wcbNumber)) continue;   // reachable + ETM-online only
    if (owner == 0 || nb.wcbNumber < owner) owner = nb.wcbNumber;
  }
  return owner;
}

// Is a controller-type client (NaviCore / Sabé) present on the mesh?
static bool wdpMeshHasControllerClient() {
  for (int i = 0; i < MAX_WCB_COUNT; i++)
    if (wdpNeighbors[i].valid && wdpNeighbors[i].isClient &&
        wdpIsControllerType(wdpNeighbors[i].alias))
      return true;
  return false;
}

// Auto-configure Maestro-remote from whatever raw-Maestro SOURCE is on the mesh.
// Two rulesets, because the sources address Maestros differently:
//   • NaviCore/Sabé present → ANY local Maestro qualifies (the controller streams
//     raw packets to every Maestro).
//   • Kyber-local present    → only a local Maestro with ID 1 or 2 (the R-series
//     Kyber brain only addresses 1 and 2; IDs 3+ are ;m-driven and never remote).
//   • Both present           → the controller (more open) ruleset wins.
// Sticky + idempotent: only ever ENABLES, and is a no-op once we're already remote
// or if this board is itself the Kyber host. storeKyberSettings persists the flag
// live, but the receive relay task only spawns at boot, so we note "reboot to apply."
static void wdpEvaluateMaestroRemote() {
  if (Kyber_Local || Maestro_Remote) return;      // we're the source, or already remote
  uint8_t ids[WDP_MAX_MAESTRO];
  int n = wdpLocalMaestroIds(ids);
  if (n == 0) return;                              // no local Maestro to drive

  const char *why = nullptr;
  if (wdpMeshHasControllerClient()) {
    why = "controller (NaviCore/Sabé) on mesh";   // NaviCore ruleset: any Maestro
  } else if (wdpMeshHasCap(WDP_CAP_KYBER_LOCAL)) {
    for (int i = 0; i < n; i++)                    // Kyber ruleset: only Maestro 1 or 2
      if (ids[i] == 1 || ids[i] == 2) { why = "Kyber-local on mesh + local Maestro 1/2"; break; }
  }
  if (why) {
    Serial.printf("[WDP] %s — enabling Maestro remote (reboot to fully apply)\n", why);
    storeKyberSettings("remote");
  }
}

void wdpOnAdvertReceived(int senderWCB, const uint8_t *cmd) {
  if (!wdpEnabled) return;
  if (senderWCB < 1 || senderWCB > MAX_WCB_COUNT) return;
  if (cmd[0] != (uint8_t)WDP_MAGIC || cmd[1] != WDP_PROTO_VERSION) return;

  // Solicitation check FIRST — a bare SOLICIT carries no facts, so decoding it as an
  // advert (memset + rebuild below) would erase the sender's real neighbor record. If
  // this packet contains a SOLICIT TLV, arm a prompt advert and return without touching
  // the table. (Auto-join isn't triggered here either — a solicit isn't an advert.)
  {
    int s = 2;
    while (s + 2 <= 200) {
      uint8_t ty = cmd[s];
      if (ty == WDP_TLV_END) break;
      int ln = cmd[s + 1];
      if (s + 2 + ln > 200) break;
      if (ty == WDP_TLV_SOLICIT) { wdpArmSolicitedAdvert(); return; }
      s += 2 + ln;
    }
  }

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
      case WDP_TLV_FLAGS:   // advert flags bitmap — currently just the "temporary" bit
        if (len >= 1) nb.temporary = (val[0] & WDP_ADVFLAG_TEMPORARY) != 0;
        break;
      case WDP_TLV_SEQHASH:  // stored-sequence inventory fingerprint (little-endian)
        if (len >= 4) nb.seqHash = (uint32_t)val[0]        | ((uint32_t)val[1] << 8) |
                                   ((uint32_t)val[2] << 16) | ((uint32_t)val[3] << 24);
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
        memcpy(nb.maestroIds, val, L); nb.maestroCount = (uint8_t)L;
        for (int i = 0; i < L; i++) nb.maestroBaudCode[i] = 0xFF;  // unknown unless MAESTRO_CFG overrides
        break;
      }
      case WDP_TLV_MAESTRO_CFG: {   // [id][baudCode] pairs — supersedes the id-only TLV above
        int recs = len / 2; if (recs > WDP_MAX_MAESTRO) recs = WDP_MAX_MAESTRO;
        for (int i = 0; i < recs; i++) {
          nb.maestroIds[i]      = val[i * 2];
          nb.maestroBaudCode[i] = val[i * 2 + 1];
        }
        nb.maestroCount = (uint8_t)recs;
        break;
      }
      case WDP_TLV_WLED_CFG: {      // [id][baudCode] pairs — this board's local WLED nodes
        int recs = len / 2; if (recs > WDP_MAX_WLED) recs = WDP_MAX_WLED;
        for (int i = 0; i < recs; i++) {
          nb.wledIds[i]      = val[i * 2];
          nb.wledBaudCode[i] = val[i * 2 + 1];
        }
        nb.wledCount = (uint8_t)recs;
        break;
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
      case WDP_TLV_PWMTARGET: {   // [targetWCB][port] pairs — the sender's remote PWM outputs.
        // Keep only the ports that name THIS board; we self-configure them below.
        int pairs = len / 2;
        for (int i = 0; i < pairs; i++) {
          uint8_t tgt = val[i * 2];
          uint8_t prt = val[i * 2 + 1];
          if (tgt != (uint8_t)WCB_Number || prt < 1 || prt > 5) continue;
          bool dup = false;
          for (int k = 0; k < nb.pwmSelfCount; k++)
            if (nb.pwmSelfPorts[k] == prt) { dup = true; break; }
          if (!dup && nb.pwmSelfCount < 5) nb.pwmSelfPorts[nb.pwmSelfCount++] = prt;
        }
        break;
      }
      default: break;                     // unknown TLV — skipped by its length (forward-compatible)
    }
    o += 2 + len;
  }
  // Neutralize bytes that would corrupt the comma/bracket-delimited ?WDP,DUMP
  // line — wire strings are unsanitized here (unlike locally-set aliases, which
  // saveWCBAlias strips at the source).
  wdpScrub(nb.alias); wdpScrub(nb.fwVer); wdpScrub(nb.hwRev); wdpScrub(nb.capTags);
  for (int _p = 0; _p < 5; _p++) wdpScrub(nb.portLabels[_p]);

  if (!wasValid)
    Serial.printf("[WDP] learned WCB%d%s%s\n", senderWCB, nb.alias[0] ? " " : "", nb.alias);

  // ---- Auto-adopt the controller peer on hearing a controller device --------
  // Gated on !wasValid so it fires once per learn and never re-fights a manual
  // change (the neighbor stays 'valid' across periodic re-adverts). Only adopt
  // when NO controller is configured yet: never clobber a manually-pinned (or
  // already-adopted) controller just because a different one is heard on the
  // mesh. It only registers a peer + tracks heartbeats, so it's safe.
  if (!wasValid && nb.isClient && wdpIsControllerType(nb.alias) && !specialPeerEnabled) {
    Serial.printf("[WDP] heard controller \"%s\" (WCB%d) — auto-enabling controller peer\n",
                  nb.alias, senderWCB);
    enableControllerPeer((uint8_t)senderWCB);
  }

  // ---- Auto-config Maestro-remote from the mesh's raw-Maestro source --------
  // Decoupled from the controller adopt above: Maestro-remote is about a
  // Kyber-local OR a NaviCore/Sabé being present as a stream source, with
  // different Maestro-ID rules per source (see wdpEvaluateMaestroRemote).
  // Evaluated every advert; a no-op once we're already remote / the Kyber host.
  wdpEvaluateMaestroRemote();

  // ---- Auto-join (real WCBs AND client devices) -----------------------------
  // Every WCB and every WCB_Client forces its STA MAC to the derived scheme
  // 02:oct2:oct3:00:00:<id>, so a learned peer's MAC is always known from its id
  // — a client registers exactly like a WCB. We store ANY device we hear (so we
  // can reach it) and persist it across reboots; the peer table is large enough
  // that transient devices aren't a concern, and ?WDP,FORGET / ?WDP,CLEAR clean
  // it up. A learned peer that never ACKs (a passive monitor) is harmless: it is
  // skipped from ensured-broadcast completion until it reciprocates (see
  // etmAddToPendingTable, wcbPeerReciprocated). The sender id was bound to its
  // source MAC in the receive callback, so senderWCB is trustworthy.
  //
  // EXCEPTION: the controller / special peer is registered via the controller
  // path above (enableControllerPeer), not as a learned peer — addActivePeer()
  // rejects it anyway, so skip it here while it holds that role.
  {
    int idx = senderWCB - 1;
    if (wcbPeerAdvertCount[idx] < 255) wcbPeerAdvertCount[idx]++;
    bool isSpecialPeer = specialPeerEnabled && senderWCB == WCB_SPECIAL_PEER_ID;
    bool vetted = wcbPeerAdvertCount[idx] >= 2;   // >=2 adverts so one stray/echoed packet can't inject
    if (wdpAutoJoin && !isSpecialPeer) {
      if (nb.temporary) {
        // Device asked to be TEMPORARY (e.g. a management relay): adopt as an temporary
        // peer — live so we can reach it, but never persisted, and evicted on silence.
        // Also downgrades a peer that was permanently learned and now advertises
        // temporary. addTemporaryPeer is a no-op once it's already temporary, so gate on
        // !wcbPeerTemporary so we only act (and log) on the transition.
        if (vetted && !wcbPeerTemporary[idx]) {
          bool wasLearned = wcbPeerLearned[idx];
          if (addTemporaryPeer((uint8_t)senderWCB))
            Serial.printf("[WDP] %s WCB%d%s%s (temporary)\n",
                          wasLearned ? "downgraded to temporary" : "temporarily joined",
                          senderWCB, nb.alias[0] ? " " : "", nb.alias);
        }
      } else if (!wcbPeerActive[idx] && vetted) {
        // Default: permanent auto-join (persisted learned peer), unchanged.
        if (addActivePeer((uint8_t)senderWCB, true))
          Serial.printf("[WDP] auto-joined WCB%d%s%s (%s)\n", senderWCB,
                        nb.alias[0] ? " " : "", nb.alias,
                        nb.isClient ? "client" : "board");
      }
    }
  }

  // ---- Auto-config remote Maestros (persisted per-device routing table) ------
  // Every Maestro a PEER neighbor physically hosts becomes a remote proxy slot on
  // this board (id -> senderWCB @ baud), identical to a manual ?MAESTRO,<id>:W..,
  // so ;M<id> / raw-Maestro traffic routes to the right board with no hand config.
  // Per-HOST (not first-host-wins) + idempotent + persisted (survives reboot; warm
  // table on boot) — a device number hosted by several boards is legal and each gets
  // its own proxy slot, because the mesh Maestro id is a MULTICAST address.
  // Runs AFTER auto-join and is gated on wcbPeerActive: we only auto-config a
  // Maestro on a board we can actually REACH (a proxy to a non-peer is unroutable),
  // which also inherits auto-join's >=2-advert vetting for learned peers so one
  // stray/echoed advert can't inject a persisted proxy. Admits the CONTROLLER (which
  // does host Maestros) but no other client, and skips any Maestro whose baud didn't
  // arrive as a usable value —
  // old id-only advert (0xFF), a garbled out-of-range code, or baud 0 all map to
  // wdpCodeToBaud()==0, and there's nothing safe to configure a proxy with.
  // EXCEPTION: the CONTROLLER (special peer — a NaviCore) is the one client device that
  // DOES host Maestros, and it advertises them with the same MAESTRO/MAESTRO_CFG TLVs a
  // board does. Both normal conjuncts block it: it always sets nb.isClient (it emits
  // DEVTYPE), and it is never wcbPeerActive[] (addActivePeer rejects it by design). Admit
  // ONLY the pinned controller id — every other client still needs !isClient &&
  // wcbPeerActive. senderWCB was bound to the source MAC on the receive path, so the id
  // can't be spoofed into this branch.
  bool isControllerPeer = (specialPeerEnabled && senderWCB == WCB_SPECIAL_PEER_ID);
  if (wdpAutoJoin && !wcbPeerTemporary[senderWCB - 1] &&
      (isControllerPeer || (!nb.isClient && wcbPeerActive[senderWCB - 1]))) {   // never derive PERSISTED config from a temp peer
    bool maestroChanged = false;
    for (int i = 0; i < nb.maestroCount && i < WDP_MAX_MAESTRO; i++) {
      uint32_t baud = wdpCodeToBaud(nb.maestroBaudCode[i]);
      if (baud == 0) continue;   // unknown / garbled / baud-0 — nothing to configure
      if (maestroAutoAddRemote(nb.maestroIds[i], (uint8_t)senderWCB, baud))
        maestroChanged = true;
    }
    // If THIS board is the Kyber host forwarding by target, a newly auto-learned
    // remote Maestro must land in kyberTargets[] too — otherwise forwardDataFromKyber
    // never broadcasts the sabre stream to the just-discovered board until a manual
    // ?KYBER,LOCAL re-issue. Add-only so a manual remote-port stays put; broadcast
    // mode (kyberUseTargeting==false) already reaches remote Maestros, so skip it.
    if (maestroChanged && Kyber_Local && kyberUseTargeting) {
      if (reconcileKyberTargetsFromMaestroConfigs() > 0) {
        saveKyberTargets();
        if (debugEnabled)
          Serial.println("[WDP] Kyber targets updated for auto-learned remote Maestro");
      }
    }
    for (int i = 0; i < nb.wledCount && i < WDP_MAX_WLED; i++) {   // same rules for WLED
      uint32_t baud = wdpCodeToBaud(nb.wledBaudCode[i]);
      if (baud == 0) continue;
      wledAutoAddRemote(nb.wledIds[i], (uint8_t)senderWCB, baud);
    }
    // HCR / MP3 / DFPlayer are single-owner capabilities (no id): learn + persist the
    // host so ;H / ;A / ;D route there across reboots. First-host-wins (see
    // hcr/mp3/dfpAutoAddRemote).
    if (nb.capFlags & WDP_CAP_HCR)      hcrAutoAddRemote((uint8_t)senderWCB);
    if (nb.capFlags & WDP_CAP_MP3)      mp3AutoAddRemote((uint8_t)senderWCB);
    if (nb.capFlags & WDP_CAP_DFPLAYER) dfpAutoAddRemote((uint8_t)senderWCB);

    // Receiver-side PWM output auto-config: the sender named THIS board as a remote
    // PWM output target (decoded into nb.pwmSelfPorts). Reserve/configure each such
    // port locally if we haven't already — this is the fix for "a board powered off
    // when the mapping was made never got configured." addPWMOutputPort is idempotent
    // (no-op if the port is already an output) and applies LIVE — pin config + NVS,
    // NO reboot (unlike a mapping add or a port removal).
    for (int i = 0; i < nb.pwmSelfCount; i++) {
      uint8_t prt = nb.pwmSelfPorts[i];
      if (isSerialPortPWMOutput(prt)) continue;   // already an output — nothing to do
      if (!canUsePWMOnPort(prt, true)) continue;  // reserved (the Kyber's own port, a REMOTE board's
                                                  // S1, a device or local-Maestro port): skip SILENTLY
                                                  // so we don't re-log every 60 s advert. It can flip
                                                  // (a Kyber move, a device clear); the next advert
                                                  // then retries. A Kyber LOCAL board's other hardware
                                                  // port is NOT reserved (tracker #73 D4).
      Serial.printf("[WDP] WCB%d drives our S%d — auto-configuring PWM output\n",
                    senderWCB, prt);
      addPWMOutputPort(prt, (uint8_t)senderWCB);  // tag the source so it can self-heal on removal
    }
    // Self-heal: if the sender previously drove one of our ports but its FRESH advert
    // no longer names it, the mapping was deleted — clear the stale output config we
    // auto-added for it (add-only ports become self-healing). Runs on this live advert,
    // so the source is definitively present; a stale/offline source never triggers a
    // clear. Only clears ports tagged to THIS source — manual outputs are never touched.
    // Clears the list + NVS + broadcast flags now; the pin fully returns to serial on the
    // next reboot (same as the manual ;MAP,PWM,CLEAR,OUT, minus its forced reboot — we
    // must not auto-reboot just because a peer dropped a mapping).
    reconcileWdpAutoPWMOutputs((uint8_t)senderWCB, nb.pwmSelfPorts, nb.pwmSelfCount);
  }
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

// ==================== WDP-DA: serial-attached device announces ============
// A device wired to a serial port self-identifies with "@WDP1 {json}" (see
// docs/WDP_DEVICE_ANNOUNCE.md). processIncomingSerial routes any line starting
// with "@WDP" here. One record per (port, type), up to WDP_DA_PER_PORT per port.
// A record is confirmed by its device's second announce, the way auto-join waits for a
// second advert: boards chained on one port can garble each other into a line that
// still parses, and a record that is kept for good must not come from one such line.
// Unconfirmed records live in RAM only and are dropped if not heard again. Confirmed
// ones persist (NVS wdp_da) and are kept when their device goes quiet; only a forget
// removes one. They feed the advert port label, ?WDP,DUMP here, and every neighbor
// through the device-list frames below.

static WdpDaDevice wdpDaDevices[5][WDP_DA_PER_PORT];
static uint32_t    wdpDaNextFirstHeard = 1;
static const unsigned long WDP_DA_LIVE_MS = 90000UL;   // ~3 missed 25-30 s announces = quiet
static bool          wdpDaDirty    = false;   // list changed since the last NVS save
static unsigned long wdpDaSaveAtMs = 0;       // when loop() saves it: a burst of changes is one write

#define WDP_DA_MAGIC       'D'
#define WDP_DA_VERSION     0x01    // device-list frame format
#define WDP_DA_NVS_VERSION 0x01    // saved-list blob format
#define WDP_DA_FLAG_LIVE   0x01    // record flag: heard within WDP_DA_LIVE_MS
#define WDP_DA_REC_MAX     (6 + 24 + 27 + 15 + 48)   // one encoded record, all fields at full length
#define WDP_DA_BLOB_MAX    (3 + 5 * WDP_DA_PER_PORT * WDP_DA_REC_MAX)

// The serial task (which adds and refreshes records) and loop() (which reads them, marks
// them quiet and forgets them) are both priority 1 on core 1 and time-slice. Suspending
// the scheduler makes a table access atomic against the other task WITHOUT masking
// interrupts: a critical section would also hold off the level-3 GPIO ISR that
// soft-serial RX timestamps edges with (CLAUDE.md rule 13). It only works because both
// tasks share core 1. Nothing between lock and unlock may block, print, allocate or
// touch NVS.
static inline void wdpDaLock()   { vTaskSuspendAll(); }
static inline void wdpDaUnlock() { xTaskResumeAll(); }

// Call with the lock held. The NVS write itself happens later, in loop() (wdpDaTick).
static void wdpDaMarkDirty() {
  wdpDaDirty    = true;
  wdpDaSaveAtMs = millis() + 1000;
}

// Pull a "key":"value" string out of a flat JSON object. out is always
// NUL-terminated (possibly empty). No escape handling — identity strings don't
// contain quotes.
static bool wdpDaExtractStr(const String &json, const char *key, char *out, int outSize) {
  out[0] = '\0';
  String pat = String("\"") + key + "\"";
  int k = json.indexOf(pat);            if (k < 0) return false;
  int colon = json.indexOf(':', k + pat.length()); if (colon < 0) return false;
  int q1 = json.indexOf('"', colon + 1);            if (q1 < 0) return false;
  int q2 = json.indexOf('"', q1 + 1);               if (q2 < 0) return false;
  int L = q2 - q1 - 1; if (L > outSize - 1) L = outSize - 1;
  for (int i = 0; i < L; i++) out[i] = json[q1 + 1 + i];
  out[L] = '\0';
  return true;
}

// Pull "caps":[ "a","b" ] into out as space-separated tags.
static void wdpDaExtractCaps(const String &json, char *out, int outSize) {
  out[0] = '\0';
  int k = json.indexOf("\"caps\""); if (k < 0) return;
  int lb = json.indexOf('[', k);    if (lb < 0) return;
  int rb = json.indexOf(']', lb);   if (rb < 0) return;
  int o = 0; bool inTok = false;
  for (int i = lb + 1; i < rb && o < outSize - 1; i++) {
    char c = json[i];
    if (c == '"') { if (inTok && o < outSize - 1) out[o++] = ' '; inTok = !inTok; continue; }
    if (inTok && o < outSize - 1) out[o++] = c;
  }
  while (o > 0 && out[o - 1] == ' ') o--;
  out[o] = '\0';
}

// One record, the same bytes in NVS and in a device-list frame:
//   [port][flags][tLen][type][fLen][fw][hLen][hwRev][cLen][capTags]
// Each length is clamped to its field, so a record is at most WDP_DA_REC_MAX bytes.
// Returns the offset past it, or -1 if it doesn't fit before max.
static int wdpDaPutStr(uint8_t *buf, int o, const char *s, int cap) {
  int L = (int)strnlen(s, cap);
  buf[o++] = (uint8_t)L;
  memcpy(buf + o, s, L);
  return o + L;
}
static int wdpDaPutRecord(uint8_t *buf, int o, int max, uint8_t port, uint8_t flags,
                          const char *type, const char *fw, const char *hwRev, const char *capTags) {
  int need = 6 + (int)strnlen(type, 24) + (int)strnlen(fw, 27) + (int)strnlen(hwRev, 15) +
             (int)strnlen(capTags, 48);
  if (o + need > max) return -1;
  buf[o++] = port;
  buf[o++] = flags;
  o = wdpDaPutStr(buf, o, type, 24);
  o = wdpDaPutStr(buf, o, fw, 27);
  o = wdpDaPutStr(buf, o, hwRev, 15);
  return wdpDaPutStr(buf, o, capTags, 48);
}

// Decode the record at buf[o]: port, flags and the four strings (scrubbed, NUL-terminated;
// type non-empty). Returns the offset past it, or -1 if it is malformed or runs past max.
static int wdpDaGetStr(const uint8_t *buf, int o, int max, char *out, int cap) {
  if (o >= max) return -1;
  int L = buf[o++];
  if (L > cap || o + L > max) return -1;
  memcpy(out, buf + o, L);
  out[L] = '\0';
  return o + L;
}
static int wdpDaGetRecord(const uint8_t *buf, int o, int max, uint8_t *port, uint8_t *flags,
                          char *type, char *fw, char *hwRev, char *capTags) {
  if (o + 2 > max) return -1;
  *port  = buf[o++];
  *flags = buf[o++];
  if (*port < 1 || *port > 5) return -1;
  if ((o = wdpDaGetStr(buf, o, max, type, 24)) < 0 || !type[0]) return -1;
  if ((o = wdpDaGetStr(buf, o, max, fw, 27))      < 0) return -1;
  if ((o = wdpDaGetStr(buf, o, max, hwRev, 15))   < 0) return -1;
  if ((o = wdpDaGetStr(buf, o, max, capTags, 48)) < 0) return -1;
  wdpScrub(type); wdpScrub(fw); wdpScrub(hwRev); wdpScrub(capTags);
  return o;
}

// The port's (1-5) records in the order they were first heard, as slot indices into
// idx[]; returns the count. With confirmedOnly, just the confirmed ones — what is saved,
// advertised and dumped; idx[0] of those labels the port. Call with the lock held, or
// from setup before the serial task exists.
static int wdpDaOrder(int port, int idx[WDP_DA_PER_PORT], bool confirmedOnly) {
  const WdpDaDevice *row = wdpDaDevices[port - 1];
  int n = 0;
  for (int k = 0; k < WDP_DA_PER_PORT; k++) {
    if (!row[k].used || (confirmedOnly && !row[k].confirmed)) continue;
    int j = n++;
    while (j > 0 && row[idx[j - 1]].firstHeard > row[k].firstHeard) { idx[j] = idx[j - 1]; j--; }
    idx[j] = k;
  }
  return n;
}

// Copy the port's records, first-heard first, into out[]; returns the count. Takes the lock.
static int wdpDaSnapshotPort(int port, WdpDaDevice out[WDP_DA_PER_PORT], bool confirmedOnly) {
  int idx[WDP_DA_PER_PORT];
  wdpDaLock();
  int c = wdpDaOrder(port, idx, confirmedOnly);
  for (int j = 0; j < c; j++) out[j] = wdpDaDevices[port - 1][idx[j]];
  wdpDaUnlock();
  return c;
}

// The port's WDP-DA label: the type of its first-heard confirmed record, heard or not —
// a quiet device keeps naming its port. "" when the port has none.
static void wdpDaLabel(int port, char *out, size_t cap) {
  out[0] = '\0';
  if (port < 1 || port > 5) return;
  int idx[WDP_DA_PER_PORT];
  wdpDaLock();
  if (wdpDaOrder(port, idx, true) > 0) {
    strncpy(out, wdpDaDevices[port - 1][idx[0]].type, cap - 1);
    out[cap - 1] = '\0';
  }
  wdpDaUnlock();
}

// On a full port, the record a new type may replace: the unconfirmed one heard from least
// recently, or -1 when every record is confirmed — a saved device is never pushed out
// automatically. Call with the lock held.
static int wdpDaStalestUnconfirmed(const WdpDaDevice *row, unsigned long now) {
  int best = -1;
  unsigned long bestAge = 0;
  for (int k = 0; k < WDP_DA_PER_PORT; k++) {
    if (!row[k].used || row[k].confirmed) continue;
    unsigned long age = now - row[k].lastSeenMs;
    if (best < 0 || age > bestAge) { best = k; bestAge = age; }
  }
  return best;
}

void wdpDaHandleLine(int port, const char *line) {
  if (port < 1 || port > 5 || !line) return;
  String s = line; s.trim();
  if (!s.startsWith("@WDP1")) return;          // only protocol version 1
  // One whole announce per line. Boards chained on one port share its RX wire, and two
  // announcing at once garble each other: a line cut short, or run into the next one,
  // would otherwise become a phantom record — and records are kept until forgotten.
  if (!s.endsWith("}") || s.indexOf("@WDP", 1) >= 0) return;
  int brace = s.indexOf('{'); if (brace < 0) return;
  String json = s.substring(brace);

  // Build the record aside, then copy it in under the lock (see wdpDaLock). Wire strings
  // are unsanitized — scrub bytes that would corrupt the port label / ?WDP,DUMP (matches
  // the neighbor-decode scrub). The type is scrubbed before matching, so it compares equal
  // to the stored key.
  WdpDaDevice nd;
  memset(&nd, 0, sizeof(nd));
  if (!wdpDaExtractStr(json, "type", nd.type, sizeof(nd.type)) || !nd.type[0]) return;  // type required
  wdpDaExtractStr(json, "fw", nd.fw,    sizeof(nd.fw));
  wdpDaExtractStr(json, "hw", nd.hwRev, sizeof(nd.hwRev));
  wdpDaExtractCaps(json,      nd.capTags, sizeof(nd.capTags));
  wdpScrub(nd.type); wdpScrub(nd.fw); wdpScrub(nd.hwRev); wdpScrub(nd.capTags);
  nd.used = nd.heard = nd.live = true;
  nd.lastSeenMs = millis();

  char dropped[sizeof(nd.type)] = "";
  bool isNew = false, confirmedNow = false, wasLive = true, refused = false;
  wdpDaLock();
  WdpDaDevice *row = wdpDaDevices[port - 1];
  int slot = -1, freeSlot = -1;
  for (int k = 0; k < WDP_DA_PER_PORT; k++) {
    if (row[k].used && strcmp(row[k].type, nd.type) == 0) { slot = k; break; }
    if (!row[k].used && freeSlot < 0) freeSlot = k;
  }
  if (slot >= 0) {                             // known device: refresh, keep its place
    WdpDaDevice &d = row[slot];
    wasLive      = d.live;
    confirmedNow = !d.confirmed;               // its second announce confirms it
    if (confirmedNow || strcmp(d.fw, nd.fw) || strcmp(d.hwRev, nd.hwRev) || strcmp(d.capTags, nd.capTags))
      wdpDaMarkDirty();                        // newly confirmed, or saved facts changed (a reflash)
    nd.firstHeard = d.firstHeard;
    nd.confirmed  = true;
    d = nd;
  } else {                                     // new device: unconfirmed until it announces again
    slot = (freeSlot >= 0) ? freeSlot : wdpDaStalestUnconfirmed(row, nd.lastSeenMs);
    if (slot < 0) {
      refused = true;                          // every slot holds a saved device
    } else {
      isNew = true;
      if (freeSlot < 0) memcpy(dropped, row[slot].type, sizeof(dropped));
      nd.firstHeard = wdpDaNextFirstHeard++;
      row[slot] = nd;                          // confirmed = false: not saved, advertised or a label yet
    }
  }
  wdpDaUnlock();

  if (refused) {
    // Once per newcomer per port: a device that keeps announcing would otherwise repeat it every 30 s.
    static char lastRefused[5][sizeof(nd.type)];
    if (strcmp(lastRefused[port - 1], nd.type) != 0) {
      memcpy(lastRefused[port - 1], nd.type, sizeof(nd.type));
      Serial.printf("[WDP-DA] S%d: %s not added: port full (%d saved devices; %cWDP,DA,FORGET one first)\n",
                    port, nd.type, WDP_DA_PER_PORT, LocalFunctionIdentifier);
    }
    return;
  }
  if (dropped[0])
    Serial.printf("[WDP-DA] S%d: %s dropped, port full (%d devices)\n", port, dropped, WDP_DA_PER_PORT);
  if (isNew || (!wasLive && !confirmedNow))
    Serial.printf("[WDP-DA] S%d: %s%s%s%s\n", port, nd.type, nd.fw[0] ? " fw " : "", nd.fw,
                  isNew ? "" : " (heard again)");
  if (confirmedNow)
    Serial.printf("[WDP-DA] S%d: %s saved\n", port, nd.type);
}

// Save the list to NVS (wdp_da/list) port by port, first-heard first, so the reloaded
// list keeps each port's order and label. loop() only: an NVS write can stall for tens
// of ms, and the serial task's job is to keep the soft-serial RX buffers drained.
static void wdpDaSave() {
  uint8_t *blob = (uint8_t *)malloc(WDP_DA_BLOB_MAX);
  if (!blob) return;                           // still dirty: the next tick retries
  int o = 3, n = 0;
  blob[0] = WDP_DA_MAGIC;
  blob[1] = WDP_DA_NVS_VERSION;
  wdpDaLock();
  for (int p = 1; p <= 5; p++) {
    int idx[WDP_DA_PER_PORT];
    int c = wdpDaOrder(p, idx, true);         // confirmed records only
    for (int j = 0; j < c; j++) {
      const WdpDaDevice &d = wdpDaDevices[p - 1][idx[j]];
      int next = wdpDaPutRecord(blob, o, WDP_DA_BLOB_MAX, (uint8_t)p, 0, d.type, d.fw, d.hwRev, d.capTags);
      if (next < 0) break;
      o = next;
      n++;
    }
  }
  wdpDaDirty = false;
  wdpDaUnlock();
  blob[2] = (uint8_t)n;
  preferences.begin("wdp_da", false);
  size_t wrote = preferences.putBytes("list", blob, o);
  preferences.end();
  free(blob);
  if (wrote != (size_t)o) {
    Serial.println("[WDP-DA] could not save the device list to NVS (full?) - retrying in 60 s");
    wdpDaLock();
    wdpDaDirty    = true;
    wdpDaSaveAtMs = millis() + 60000;
    wdpDaUnlock();
  }
}

// Reload the saved list. Called from wdpBegin (setup), before the serial task exists.
// Every record starts quiet — "not heard since boot" — until its device announces again.
static void wdpDaLoad() {
  memset(wdpDaDevices, 0, sizeof(wdpDaDevices));
  wdpDaNextFirstHeard = 1;
  preferences.begin("wdp_da", true);
  size_t len = preferences.getBytesLength("list");
  uint8_t *blob = (len >= 3 && len <= WDP_DA_BLOB_MAX) ? (uint8_t *)malloc(len) : nullptr;
  if (blob && preferences.getBytes("list", blob, len) != len) { free(blob); blob = nullptr; }
  preferences.end();
  if (!blob) return;
  int n = 0;
  if (blob[0] == WDP_DA_MAGIC && blob[1] == WDP_DA_NVS_VERSION) {
    int o = 3;
    for (int r = 0; r < blob[2]; r++) {
      WdpDaDevice d;
      memset(&d, 0, sizeof(d));
      uint8_t port, flags;
      int next = wdpDaGetRecord(blob, o, (int)len, &port, &flags, d.type, d.fw, d.hwRev, d.capTags);
      if (next < 0) break;                     // corrupt tail: keep what parsed
      o = next;
      WdpDaDevice *row = wdpDaDevices[port - 1];
      for (int k = 0; k < WDP_DA_PER_PORT; k++) {
        if (row[k].used) {
          if (strcmp(row[k].type, d.type) == 0) break;   // duplicate: keep the first
          continue;
        }
        d.used = d.confirmed = true;
        d.firstHeard = wdpDaNextFirstHeard++;
        row[k] = d;
        n++;
        break;
      }
    }
  }
  free(blob);
  if (n) Serial.printf("[WDP-DA] %d serial-attached device%s remembered (%cWDP,DA)\n",
                       n, n == 1 ? "" : "s", LocalFunctionIdentifier);
}

void wdpDaTick() {
  static unsigned long nextScanMs = 0;
  unsigned long now = millis();
  if ((long)(now - nextScanMs) >= 0) {
    nextScanMs = now + 250;
    for (int p = 0; p < 5; p++) {
      for (int k = 0; k < WDP_DA_PER_PORT; k++) {
        WdpDaDevice &d = wdpDaDevices[p][k];
        // Checked unlocked first (almost always nothing to do), then again under the lock.
        // Signed: the serial task can refresh a record after `now` was read, leaving
        // lastSeenMs a millisecond ahead. Unsigned, that wraps huge and marks quiet a
        // device that has just announced.
        if (!(d.used && d.live && (long)(now - d.lastSeenMs) > (long)WDP_DA_LIVE_MS)) continue;
        char quiet[sizeof(d.type)] = "";
        bool once = false;
        wdpDaLock();
        if (d.used && d.live && (long)(now - d.lastSeenMs) > (long)WDP_DA_LIVE_MS) {
          memcpy(quiet, d.type, sizeof(quiet));
          once = !d.confirmed;
          if (once) d.used = false;            // heard only once: a stray line, never saved
          else      d.live = false;            // kept, and still naming its port: just quiet
        }
        wdpDaUnlock();
        if (quiet[0]) Serial.printf("[WDP-DA] S%d: %s %s\n", p + 1, quiet,
                                    once ? "heard only once, dropped" : "stopped announcing");
      }
    }
  }
  if (wdpDaDirty && (long)(now - wdpDaSaveAtMs) >= 0) wdpDaSave();
}

// Seconds since the record was last heard. Clamped at 0: the serial task can refresh it
// after the caller read millis().
static unsigned long wdpDaAgeS(const WdpDaDevice &d, unsigned long now) {
  return (long)(now - d.lastSeenMs) > 0 ? (now - d.lastSeenMs) / 1000 : 0;
}

void wdpDaPrint() {
  Serial.println();
  Serial.println("Serial-attached devices (WDP-DA announces):");
  unsigned long now = millis();
  int n = 0;
  for (int p = 1; p <= 5; p++) {
    WdpDaDevice recs[WDP_DA_PER_PORT];
    int c = wdpDaSnapshotPort(p, recs, false);  // unconfirmed too, marked
    for (int j = 0; j < c; j++) {
      const WdpDaDevice &d = recs[j];
      char when[40];
      if (!d.heard)          strcpy(when, "not heard since boot");
      else if (!d.confirmed) snprintf(when, sizeof(when), "%lus ago, heard once", wdpDaAgeS(d, now));
      else                   snprintf(when, sizeof(when), "%lus ago", wdpDaAgeS(d, now));
      n++;
      Serial.printf("  S%d  %-24.24s  fw %-14.14s  %s\n", p, d.type, d.fw[0] ? d.fw : "?", when);
      if (d.hwRev[0])   Serial.printf("       hw %s\n", d.hwRev);
      if (d.capTags[0]) Serial.printf("       caps: %s\n", d.capTags);
    }
  }
  if (n == 0)
    Serial.println("  (none — a wired device self-identifies by sending @WDP1)");
  else
    Serial.printf("  Saved on a device's second announce, kept until forgotten:\n"
                  "  %cWDP,DA,FORGET,S<n>[,<type>]  or  %cWDP,DA,CLEAR\n",
                  LocalFunctionIdentifier, LocalFunctionIdentifier);
  Serial.println();
}

// ?WDP,DUMP: one [WDPDA:...] line per confirmed record of THIS board, port by port,
// first-heard first — the same set its neighbors receive. SEEN=1 while heard within
// WDP_DA_LIVE_MS; AGE is seconds since the last announce, "-" when not heard since boot.
// Its own record type, not fields on [WDPIF:...], so older Wizards ignore it. Neighbors'
// devices come from wdpDaDumpRemote.
static void wdpDaDump(int selfNum) {
  unsigned long now = millis();
  for (int p = 1; p <= 5; p++) {
    WdpDaDevice recs[WDP_DA_PER_PORT];
    int c = wdpDaSnapshotPort(p, recs, true);
    for (int j = 0; j < c; j++) {
      const WdpDaDevice &d = recs[j];
      char age[12];
      if (d.heard) snprintf(age, sizeof(age), "%lu", wdpDaAgeS(d, now));
      else         strcpy(age, "-");
      Serial.printf("[WDPDA:N=%d,S=%d,TYPE=%s,FW=%s,HW=%s,CAPS=%s,SEEN=%d,AGE=%s]\n",
                    selfNum, p, d.type, d.fw, d.hwRev, d.capTags, d.live ? 1 : 0, age);
    }
  }
}

// ?WDP,DA,FORGET,S<n>[,<type>]  and  ?WDP,DA,CLEAR. A device that is still announcing
// comes straight back on its next announce: forget is for hardware that was removed or
// replaced. The type match ignores case, since a person types it.
static void wdpDaCommand(const String &rest) {
  String r = rest; r.trim();
  String ru = r; ru.toUpperCase();
  if (ru == "CLEAR") {
    int n = 0;
    wdpDaLock();
    for (int p = 0; p < 5; p++)
      for (int k = 0; k < WDP_DA_PER_PORT; k++)
        if (wdpDaDevices[p][k].used) { wdpDaDevices[p][k].used = false; n++; }
    if (n) wdpDaMarkDirty();
    wdpDaUnlock();
    Serial.printf("[WDP-DA] forgot %d device%s\n", n, n == 1 ? "" : "s");
    return;
  }
  if (ru.startsWith("FORGET,")) {
    String t = r.substring(7); t.trim();
    int c = t.indexOf(',');
    String portTok = (c >= 0) ? t.substring(0, c) : t;
    String type    = (c >= 0) ? t.substring(c + 1) : String("");
    portTok.trim(); type.trim();
    if (portTok.startsWith("S") || portTok.startsWith("s")) portTok = portTok.substring(1);
    int port = portTok.toInt();
    if (port >= 1 && port <= 5) {
      char gone[WDP_DA_PER_PORT][25];
      int n = 0;
      wdpDaLock();
      for (int k = 0; k < WDP_DA_PER_PORT; k++) {
        WdpDaDevice &d = wdpDaDevices[port - 1][k];
        if (!d.used) continue;
        if (type.length() && strcasecmp(type.c_str(), d.type) != 0) continue;
        memcpy(gone[n++], d.type, sizeof(gone[0]));
        d.used = false;
      }
      if (n) wdpDaMarkDirty();
      wdpDaUnlock();
      if (n == 0 && type.length())
        Serial.printf("[WDP-DA] S%d: no device \"%s\" (see %cWDP,DA)\n", port, type.c_str(), LocalFunctionIdentifier);
      else if (n == 0)
        Serial.printf("[WDP-DA] S%d: nothing to forget\n", port);
      for (int i = 0; i < n; i++) Serial.printf("[WDP-DA] S%d: %s forgotten\n", port, gone[i]);
      return;
    }
  }
  Serial.printf("[WDP] usage: %cWDP,DA[,FORGET,S<n>[,<type>]|,CLEAR]\n", LocalFunctionIdentifier);
}

// ---- The device list on the mesh -------------------------------------------------------
// The advert's 200 B can't hold up to 20 full records, so the list rides its OWN packet
// type, PACKET_TYPE_WDP_DA (17), in as many frames as it needs:
//   [0]'D' [1]version [2..5]listHash (LE) [6]frameIdx [7]frameCount [8]recordCount
//   then records (wdpDaPutRecord) until a 0 port byte or the end of the 200 B.
// A new packet type, not a TLV on PACKET_TYPE_WDP: released firmware and WCB_Client drop an
// ETM packet type they don't know, whereas a frame on the advert's type would reach an
// older board's advert decoder as an EMPTY advert and blank that sender's neighbor record
// (the SOLICIT trap, docs/WDP_DESIGN.md §3). A receiver shows a list only once it holds
// every frame of one listHash, so a lost frame leaves the previous list in place, never
// half of a new one. Sent after every advert (boot burst, 60 s backstop, solicited, advert
// change) and when the list itself changes (a device appears, goes quiet or is forgotten),
// with one re-send.
#define WDP_DA_HDR        9
#define WDP_DA_MAX_FRAMES (5 * WDP_DA_PER_PORT)   // a record always fits one frame

// Build frame frameIdx of the current list into buf[200]: records packed greedily in list
// order (port by port, first-heard first). Also returns the list hash — FNV-1a over every
// record's bytes, live flag included, then the record count — and the frame and record
// counts. Takes the lock. Returns the frame's length, 0 if frameIdx is past the last frame.
static int wdpDaBuildFrame(int frameIdx, uint8_t *buf, uint32_t *hash, int *frames, int *records) {
  uint8_t rec[WDP_DA_REC_MAX];
  uint32_t h = 2166136261u;
  int nFrames = 1, used = WDP_DA_HDR, len = 0, n = 0;
  memset(buf, 0, 200);
  wdpDaLock();
  for (int p = 1; p <= 5; p++) {
    int idx[WDP_DA_PER_PORT];
    int c = wdpDaOrder(p, idx, true);         // confirmed records only
    for (int j = 0; j < c; j++) {
      const WdpDaDevice &d = wdpDaDevices[p - 1][idx[j]];
      int rl = wdpDaPutRecord(rec, 0, sizeof(rec), (uint8_t)p, d.live ? WDP_DA_FLAG_LIVE : 0,
                              d.type, d.fw, d.hwRev, d.capTags);
      for (int i = 0; i < rl; i++) { h ^= rec[i]; h *= 16777619u; }
      if (used + rl > 200 - 1) { nFrames++; used = WDP_DA_HDR; }   // keep a byte for the 0 terminator
      if (nFrames - 1 == frameIdx) { memcpy(buf + used, rec, rl); len = used + rl; }
      used += rl;
      n++;
    }
  }
  wdpDaUnlock();
  h ^= (uint8_t)n; h *= 16777619u;
  *hash = h; *frames = nFrames; *records = n;
  if (frameIdx >= nFrames) return 0;
  if (len == 0) len = WDP_DA_HDR;               // an empty list is one header-only frame
  buf[0] = WDP_DA_MAGIC;
  buf[1] = WDP_DA_VERSION;
  buf[2] = (uint8_t)(h & 0xFF);         buf[3] = (uint8_t)((h >> 8) & 0xFF);
  buf[4] = (uint8_t)((h >> 16) & 0xFF); buf[5] = (uint8_t)((h >> 24) & 0xFF);
  buf[6] = (uint8_t)frameIdx;
  buf[7] = (uint8_t)nFrames;
  buf[8] = (uint8_t)n;
  return len;
}

static bool          wdpDaTxActive   = false;   // a send is in progress
static int           wdpDaTxNext     = 0;       // ...next frame index
static uint32_t      wdpDaTxHash     = 0;       // ...and the list hash its frames carry
static unsigned long wdpDaTxAtMs     = 0;
static bool          wdpDaTxSentAny  = false;   // a whole list has gone out since boot
static uint32_t      wdpDaTxSentHash = 0;       // ...its hash: the on-change baseline
static unsigned long wdpDaResendAtMs = 0;       // one re-send after a change (0 = none armed)
static const unsigned long WDP_DA_FRAME_GAP_MS = 25;

static void wdpDaStartSend() {
  wdpDaTxActive = true;
  wdpDaTxNext   = 0;
  wdpDaTxAtMs   = millis();
}

// One frame per call, WDP_DA_FRAME_GAP_MS apart, so a long list never floods the ESP-NOW
// TX queue. If the list changes mid-send, start over: every frame of a send carries one hash.
static void wdpDaSendStep(unsigned long now) {
  if (!wdpDaTxActive || (long)(now - wdpDaTxAtMs) < 0) return;
  uint8_t buf[200];
  uint32_t h;
  int frames, records;
  int len = wdpDaBuildFrame(wdpDaTxNext, buf, &h, &frames, &records);
  if (wdpDaTxNext > 0 && h != wdpDaTxHash) {
    wdpDaTxNext = 0;
    len = wdpDaBuildFrame(0, buf, &h, &frames, &records);
  }
  wdpDaTxHash = h;
  if (len > 0) wdpDaBroadcast(buf, len);
  wdpDaTxNext++;
  wdpDaTxAtMs = now + WDP_DA_FRAME_GAP_MS;
  if (wdpDaTxNext >= frames) {
    wdpDaTxActive   = false;
    wdpDaTxSentAny  = true;
    wdpDaTxSentHash = h;
    if (debugMGMT) Serial.printf("[WDP] device list sent (%d device%s, %d frame%s)\n",
                                 records, records == 1 ? "" : "s", frames, frames == 1 ? "" : "s");
  }
}

// Called from wdpTick's half-second change check: the list changed since it last went out
// in full (a device appeared, went quiet or was forgotten), so send it now, plus one re-send
// 800 ms later, as the advert does. The first full send, in the boot burst, sets the baseline.
static void wdpDaCheckChanged(unsigned long now) {
  if (!wdpDaTxSentAny || wdpDaTxActive) return;
  uint8_t buf[200];
  uint32_t h;
  int frames, records;
  wdpDaBuildFrame(0, buf, &h, &frames, &records);
  if (h != wdpDaTxSentHash) {
    wdpDaStartSend();
    wdpDaResendAtMs = now + 800;
  }
}

// Called every wdpTick: the armed re-send, then the next frame of a send in progress.
static void wdpDaTxTick(unsigned long now) {
  if (wdpDaResendAtMs && !wdpDaTxActive && (long)(now - wdpDaResendAtMs) >= 0) {
    wdpDaResendAtMs = 0;
    wdpDaStartSend();
  }
  wdpDaSendStep(now);
}

// ---- Neighbors' device lists (received) -------------------------------------------------
// A shared pool rather than a per-neighbor array: most boards announce a handful of
// devices, and 20 neighbors x 20 full records would cost ~50 KB. Only loop() touches it
// (frames arrive through the loop-drained WDP queue), so it needs no lock.
#define WDP_DA_POOL 32
struct WdpDaRemote {
  uint8_t  wcb;          // owning neighbor (1..MAX_WCB_COUNT); 0 = free slot
  bool     pending;      // part of a list still being assembled: not shown yet
  uint16_t pos;          // position in the sender's list: frame index * 32 + index in frame
  uint8_t  port;         // 1-5
  uint8_t  flags;        // WDP_DA_FLAG_*
  char     type[25];
  char     fw[28];
  char     hwRev[16];
  char     capTags[49];
};
static WdpDaRemote wdpDaRemote[WDP_DA_POOL];

struct WdpDaPeer {
  bool     have;         // a complete list from this neighbor is committed (shown)
  uint32_t hash;         // ...and its listHash
  bool     assembling;   // frames of a different list are arriving
  uint32_t asmHash;
  uint8_t  asmFrames;    // frames expected
  uint8_t  asmRecords;   // records expected
  uint32_t asmMask;      // one bit per frame index received
  bool     warned;       // "doesn't fit" already printed...
  uint32_t warnedHash;   // ...for this list, so the 60 s re-sends don't repeat it
};
static WdpDaPeer wdpDaPeers[MAX_WCB_COUNT];

// Free one neighbor's pool slots: its partial (pending) list, or its committed one.
static void wdpDaFreeRemote(uint8_t wcb, bool pending) {
  for (int i = 0; i < WDP_DA_POOL; i++)
    if (wdpDaRemote[i].wcb == wcb && wdpDaRemote[i].pending == pending) wdpDaRemote[i].wcb = 0;
}

// Forget everything received from a neighbor (it left the WDP table).
static void wdpDaDropPeer(uint8_t wcb) {
  if (wcb < 1 || wcb > MAX_WCB_COUNT) return;
  for (int i = 0; i < WDP_DA_POOL; i++)
    if (wdpDaRemote[i].wcb == wcb) wdpDaRemote[i].wcb = 0;
  memset(&wdpDaPeers[wcb - 1], 0, sizeof(WdpDaPeer));
}

// Give up on a neighbor's partial list; the one already shown stays.
static void wdpDaAbortAssembly(uint8_t wcb) {
  wdpDaFreeRemote(wcb, true);
  wdpDaPeers[wcb - 1].assembling = false;
}

void wdpDaOnFrameReceived(int senderWCB, const uint8_t *cmd) {
  if (!wdpEnabled) return;
  if (senderWCB < 1 || senderWCB > MAX_WCB_COUNT) return;
  if (cmd[0] != (uint8_t)WDP_DA_MAGIC || cmd[1] != WDP_DA_VERSION) return;
  uint32_t hash = (uint32_t)cmd[2] | ((uint32_t)cmd[3] << 8) |
                  ((uint32_t)cmd[4] << 16) | ((uint32_t)cmd[5] << 24);
  uint8_t idx = cmd[6], frames = cmd[7], records = cmd[8];
  if (frames == 0 || frames > WDP_DA_MAX_FRAMES || idx >= frames) return;
  if (records > 5 * WDP_DA_PER_PORT) return;
  uint8_t wcb = (uint8_t)senderWCB;
  WdpDaPeer &pe = wdpDaPeers[wcb - 1];
  if (pe.have && pe.hash == hash) return;       // already showing this list (a routine re-send)

  if (!pe.assembling || pe.asmHash != hash || pe.asmFrames != frames || pe.asmRecords != records) {
    wdpDaFreeRemote(wcb, true);                 // a newer list: drop any older partial one
    pe.assembling = true;
    pe.asmHash    = hash;
    pe.asmFrames  = frames;
    pe.asmRecords = records;
    pe.asmMask    = 0;
  }
  if (pe.asmMask & (1UL << idx)) return;        // duplicate frame

  int o = WDP_DA_HDR, n = 0;
  while (o < 200 && cmd[o] != 0) {
    WdpDaRemote r;
    memset(&r, 0, sizeof(r));
    int next = wdpDaGetRecord(cmd, o, 200, &r.port, &r.flags, r.type, r.fw, r.hwRev, r.capTags);
    if (next < 0) { wdpDaAbortAssembly(wcb); return; }   // malformed: never show part of it
    o = next;
    int slot = -1;
    for (int i = 0; i < WDP_DA_POOL; i++) if (wdpDaRemote[i].wcb == 0) { slot = i; break; }
    if (slot < 0) {
      if (!(pe.warned && pe.warnedHash == hash))
        Serial.printf("[WDP] WCB%d's device list doesn't fit (%d devices max across all neighbors)\n",
                      wcb, WDP_DA_POOL);
      pe.warned     = true;
      pe.warnedHash = hash;
      wdpDaAbortAssembly(wcb);
      return;
    }
    r.wcb     = wcb;
    r.pending = true;
    r.pos     = (uint16_t)(idx * 32 + n);
    wdpDaRemote[slot] = r;
    n++;
  }
  pe.asmMask |= (1UL << idx);
  if (pe.asmMask != ((1UL << frames) - 1)) return;      // more frames to come

  int got = 0;
  for (int i = 0; i < WDP_DA_POOL; i++)
    if (wdpDaRemote[i].wcb == wcb && wdpDaRemote[i].pending) got++;
  if (got != records) { wdpDaAbortAssembly(wcb); return; }
  wdpDaFreeRemote(wcb, false);                          // the list it replaces
  for (int i = 0; i < WDP_DA_POOL; i++)
    if (wdpDaRemote[i].wcb == wcb) wdpDaRemote[i].pending = false;
  pe.have       = true;
  pe.hash       = hash;
  pe.assembling = false;
}

// A neighbor's committed list as pool indices in list order; returns the count.
static int wdpDaRemoteList(uint8_t wcb, int *idx, int max) {
  int n = 0;
  for (int i = 0; i < WDP_DA_POOL; i++) {
    const WdpDaRemote &r = wdpDaRemote[i];
    if (r.wcb != wcb || r.pending || n >= max) continue;
    int j = n++;
    while (j > 0 && wdpDaRemote[idx[j - 1]].pos > r.pos) { idx[j] = idx[j - 1]; j--; }
    idx[j] = i;
  }
  return n;
}

// ?WDP,DUMP: a neighbor's devices, same record as wdpDaDump. AGE is "-": a receiver only
// knows whether the sender currently hears each device, not when it last did.
static void wdpDaDumpRemote(uint8_t wcb) {
  int idx[WDP_DA_POOL];
  int n = wdpDaRemoteList(wcb, idx, WDP_DA_POOL);
  for (int j = 0; j < n; j++) {
    const WdpDaRemote &r = wdpDaRemote[idx[j]];
    Serial.printf("[WDPDA:N=%d,S=%d,TYPE=%s,FW=%s,HW=%s,CAPS=%s,SEEN=%d,AGE=-]\n",
                  wcb, r.port, r.type, r.fw, r.hwRev, r.capTags, (r.flags & WDP_DA_FLAG_LIVE) ? 1 : 0);
  }
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
    { WDP_CAP_DFPLAYER, 'D' },
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
    { WDP_CAP_DFPLAYER, "DFPlayer" },
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

// Dot-joined "<id>@<baud>" list for the machine dump ("-" when none). Falls back to
// a bare id when the baud code is unknown (0xFF — a legacy id-only advert). Used for
// the supplementary [WDPX:...] line so the Wizard can show remote WLED nodes and the
// per-device baud that the terse MAESTRO= field omits. Values contain no ',' or ']'.
static void wdpIdBaudStr(const uint8_t *ids, const uint8_t *codes, uint8_t count,
                         int maxItems, char *out, int max) {
  if (count == 0) { if (max > 1) { out[0] = '-'; out[1] = '\0'; } return; }
  int o = 0; out[0] = '\0';
  for (int m = 0; m < count && m < maxItems; m++) {
    char t[16];
    if (codes[m] != 0xFF)
      snprintf(t, sizeof(t), "%s%d@%lu", o ? "." : "", ids[m],
               (unsigned long)wdpCodeToBaud(codes[m]));
    else
      snprintf(t, sizeof(t), "%s%d", o ? "." : "", ids[m]);
    int need = strlen(t);
    if (o + need >= max - 1) break;
    strcpy(out + o, t); o += need;
  }
  if (o == 0 && max > 1) { out[0] = '-'; out[1] = '\0'; }
}

// Emit the supplementary [WDPX:...] dump line for one record — the per-device baud
// the terse MAESTRO= field drops, plus the full WLED list (invisible in the main
// record entirely). Skipped when the board hosts neither Maestro nor WLED, so a
// plain WCB adds no extra line. A separate line (not new fields on [WDP:...]) so an
// older Wizard simply ignores it instead of failing to parse the whole record.
static void wdpEmitDumpX(const WdpNeighbor &nb) {
  if (nb.maestroCount == 0 && nb.wledCount == 0) return;
  char mb[80]; wdpIdBaudStr(nb.maestroIds, nb.maestroBaudCode, nb.maestroCount,
                            WDP_MAX_MAESTRO, mb, sizeof(mb));
  char wl[80]; wdpIdBaudStr(nb.wledIds, nb.wledBaudCode, nb.wledCount,
                            WDP_MAX_WLED, wl, sizeof(wl));
  Serial.printf("[WDPX:N=%d,MB=%s,WL=%s]\n", nb.wcbNumber, mb, wl);
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
  Serial.printf("==== WCB %d  \"%s\" ====\n", nb.wcbNumber, nb.alias[0] ? nb.alias : "(no alias)");
  Serial.printf("  Platform    : %s (hw %d)\n", wdpHwName(nb.hwVer), nb.hwVer);
  Serial.printf("  Firmware    : %s\n", nb.fwVer[0] ? nb.fwVer : "?");
  if ((nb.capFlags & WDP_CAP_CONTROLLER) && nb.ctrlId > 0)
    Serial.printf("  Capabilities: %s  (controller ID %d)\n", caps, nb.ctrlId);
  else
    Serial.printf("  Capabilities: %s\n", caps);
  if (nb.maestroCount == 0) {
    Serial.println("  Maestros    : -");
  } else {
    Serial.printf("  Maestros    :");
    for (int m = 0; m < nb.maestroCount && m < WDP_MAX_MAESTRO; m++) {
      if (nb.maestroBaudCode[m] != 0xFF)   // rich advert — show id@baud
        Serial.printf(" %d@%lu", nb.maestroIds[m], (unsigned long)wdpCodeToBaud(nb.maestroBaudCode[m]));
      else                                  // legacy id-only advert
        Serial.printf(" %d", nb.maestroIds[m]);
    }
    Serial.println();
  }
  if (nb.wledCount == 0) {
    Serial.println("  WLED        : -");
  } else {
    Serial.printf("  WLED        :");
    for (int w = 0; w < nb.wledCount && w < WDP_MAX_WLED; w++) {
      if (nb.wledBaudCode[w] != 0xFF)
        Serial.printf(" %d@%lu", nb.wledIds[w], (unsigned long)wdpCodeToBaud(nb.wledBaudCode[w]));
      else
        Serial.printf(" %d", nb.wledIds[w]);
    }
    Serial.println();
  }
  if (nb.seqHash == 0)
    Serial.println("  Sequences   : (not advertised)");
  else
    Serial.printf("  Sequences   : hash %08X   (%cMGMT,SEQ,%d to list)\n",
                  nb.seqHash, LocalFunctionIdentifier, nb.wcbNumber);
  Serial.printf("  Last advert : %lus ago  (%s)\n", (millis() - nb.lastAdvertMs) / 1000,
                nb.confirmed ? "live" : "stale");
  // Each port's label, then the devices announcing on it (from the neighbor's WDP-DA
  // device-list frames — only firmware that sends them has any).
  Serial.println("  Interfaces  :");
  int ridx[WDP_DA_POOL];
  int rn = wdpDaRemoteList(nb.wcbNumber, ridx, WDP_DA_POOL);
  bool any = false;
  for (int p = 0; p < 5; p++) {
    bool hasDev = false;
    for (int j = 0; j < rn; j++) if (wdpDaRemote[ridx[j]].port == p + 1) { hasDev = true; break; }
    if (!nb.portLabels[p][0] && !hasDev) continue;
    any = true;
    Serial.printf("    S%d  %s\n", p + 1, nb.portLabels[p][0] ? nb.portLabels[p] : "(no label)");
    for (int j = 0; j < rn; j++) {
      const WdpDaRemote &r = wdpDaRemote[ridx[j]];
      if (r.port != p + 1) continue;
      Serial.printf("          %-24.24s  fw %-14.14s%s\n", r.type, r.fw[0] ? r.fw : "?",
                    (r.flags & WDP_DA_FLAG_LIVE) ? "" : "  (not heard)");
      if (r.hwRev[0])   Serial.printf("            hw %s\n", r.hwRev);
      if (r.capTags[0]) Serial.printf("            caps: %s\n", r.capTags);
    }
  }
  if (!any) Serial.println("    (none advertised)");
  Serial.println();
}

// Drop one neighbor from the WDP table so ?WDP,DUMP stops emitting it. Called when a
// TEMPORARY peer is evicted after its TTL (ephemeral — it must fully vanish, not linger
// with an ever-growing AGE), and by ?WDP,FORGET. Floor/learned peers use ?WDP,FORGET.
void wdpForgetNeighbor(uint8_t id) {
  if (id >= 1 && id <= MAX_WCB_COUNT) wdpNeighbors[id - 1].valid = false;
  wdpDaDropPeer(id);                     // and the device list it sent
}

// Machine-readable dump (for the config tool / scripts).
static void printWdpDump() {
  int count = 0;
  unsigned long now = millis();

  // SELF row first — a WDP neighbor table never includes the board itself, so the
  // mesh view was missing the very board you're plugged into. Emit this board's own
  // identity in the same record format (reusing the neighbor emit path via a temp),
  // flagged PEER=3 so the Wizard renders it as "this board". Not counted in `count`.
  {
    WdpNeighbor self;
    memset(&self, 0, sizeof(self));
    self.wcbNumber = (uint8_t)WCB_Number;
    strncpy(self.alias, wcb_alias.c_str(),      sizeof(self.alias) - 1);
    strncpy(self.fwVer, SoftwareVersion.c_str(), sizeof(self.fwVer) - 1);
    self.hwVer    = (uint8_t)wcb_hw_version;
    self.capFlags = wdpCapFlags();
    self.ctrlId   = specialPeerEnabled ? WCB_SPECIAL_PEER_ID : 0;
    uint8_t sids[WDP_MAX_MAESTRO], scodes[WDP_MAX_MAESTRO];
    int sn = wdpLocalMaestroCfg(sids, scodes);
    for (int m = 0; m < sn; m++) { self.maestroIds[m] = sids[m]; self.maestroBaudCode[m] = scodes[m]; }
    self.maestroCount = (uint8_t)sn;
    uint8_t swids[WDP_MAX_WLED], swcodes[WDP_MAX_WLED];
    int swn = wdpLocalWLEDCfg(swids, swcodes);
    for (int w = 0; w < swn; w++) { self.wledIds[w] = swids[w]; self.wledBaudCode[w] = swcodes[w]; }
    self.wledCount = (uint8_t)swn;
    for (int p = 0; p < 5; p++) {
      String lbl = serialPortLabels[p];
      if (lbl.length() == 0) { char t[25]; wdpDaLabel(p + 1, t, sizeof(t)); lbl = t; }
      strncpy(self.portLabels[p], lbl.c_str(), sizeof(self.portLabels[p]) - 1);
    }
    wdpScrub(self.alias); wdpScrub(self.fwVer);
    for (int p = 0; p < 5; p++) wdpScrub(self.portLabels[p]);

    char smaestro[48]; wdpMaestroStr(self, smaestro, sizeof(smaestro));
    Serial.printf("[WDP:N=%d,CLIENT=0,ALIAS=%s,HW=%d,HWREV=%s,FW=%s,CAP=%04X,CTRL=%d,CAPTAGS=%s,MAESTRO=%s,AGE=0,SEEN=1,PEER=3]\n",
                  self.wcbNumber, self.alias, self.hwVer, self.hwRev, self.fwVer,
                  self.capFlags, self.ctrlId, self.capTags, smaestro);
    for (int p = 0; p < 5; p++)
      if (self.portLabels[p][0])
        Serial.printf("[WDPIF:N=%d,S=%d,DEV=%s]\n", self.wcbNumber, p + 1, self.portLabels[p]);
    wdpDaDump(self.wcbNumber);               // every announcing device, several per port
    wdpEmitDumpX(self);                      // MB=/WL= (Maestro+WLED id@baud)
    // Sequence-inventory fingerprint as its OWN record, not a new field on the
    // [WDP:...] line — that line's field order is load-bearing for older Wizard
    // regexes (see the PEER= note below).
    Serial.printf("[WDPSEQ:N=%d,HASH=%08X]\n", (int)self.wcbNumber, sequenceInventoryHash());
    // This board's OUTGOING remote-PWM targets — authoritative, straight from the
    // live mappings. Each is an edge "SELF drives WCB<DST> S<port>". Neighbor rows
    // below emit the reverse direction (a neighbor driving one of OUR ports).
    {
      uint8_t prec[WDP_MAX_PWMTARGET * 2];
      int pn = wdpLocalPwmTargets(prec);
      for (int k = 0; k < pn; k++)
        Serial.printf("[WDPPWM:N=%d,DST=%d,S=%d]\n",
                      (int)self.wcbNumber, (int)prec[k * 2], (int)prec[k * 2 + 1]);
    }
  }

  for (int i = 0; i < MAX_WCB_COUNT; i++) {
    WdpNeighbor &nb = wdpNeighbors[i];
    if (!nb.valid) continue;
    count++;
    char maestro[48]; wdpMaestroStr(nb, maestro, sizeof(maestro));
    // PEER: this board's membership relationship to the neighbor —
    // 0 = not a mesh peer (the controller/special peer, or a not-yet-joined
    // device), 1 = WCBQ-floor member, 2 = auto-joined/learned member, 4 = TEMPORARY
    // (temporary) peer — live but not persisted, evicted on silence. Clients CAN be
    // learned peers (auto-join stores them like WCBs), so this is not forced to 0 for
    // clients. Appended last-but-SEEN so older Wizard regexes (anchored on SEEN) fail
    // soft rather than mis-parse.
    int peerFlag = wcbPeerTemporary[i] ? 4 : (wcbPeerLearned[i] ? 2 : (wcbPeerActive[i] ? 1 : 0));
    Serial.printf("[WDP:N=%d,CLIENT=%d,ALIAS=%s,HW=%d,HWREV=%s,FW=%s,CAP=%04X,CTRL=%d,CAPTAGS=%s,MAESTRO=%s,AGE=%lu,SEEN=%d,PEER=%d]\n",
                  nb.wcbNumber, nb.isClient ? 1 : 0, nb.alias, nb.hwVer, nb.hwRev, nb.fwVer,
                  nb.capFlags, nb.ctrlId, nb.capTags, maestro,
                  (now - nb.lastAdvertMs) / 1000, nb.confirmed ? 1 : 0, peerFlag);
    // Per-port interface devices (advertised port labels, incl. WDP-DA detected
    // serial devices). DEV is the last field so a comma inside a label can't
    // shift parsing; labels are already scrubbed of ']'.
    for (int p = 0; p < 5; p++)
      if (nb.portLabels[p][0])
        Serial.printf("[WDPIF:N=%d,S=%d,DEV=%s]\n", nb.wcbNumber, p + 1, nb.portLabels[p]);
    wdpDaDumpRemote(nb.wcbNumber);           // the devices announcing on its ports
    wdpEmitDumpX(nb);                        // MB=/WL= (Maestro+WLED id@baud)
    // Sequence-inventory fingerprint (own record — see the SELF row above).
    // Omitted entirely when the neighbor advertised no hash, so a consumer can tell
    // "old firmware" from "no sequences" (the empty set hashes to 811C9DC5).
    if (nb.seqHash != 0)
      Serial.printf("[WDPSEQ:N=%d,HASH=%08X]\n", (int)nb.wcbNumber, nb.seqHash);
    // Remote-PWM edges terminating on THIS board: ports of ours the neighbor drives
    // (decoded into pwmSelfPorts). Edge "neighbor drives WCB<self> S<port>". The
    // sender's targets aimed at OTHER boards aren't in our table — only edges
    // touching this board are reconstructable from a single dump (by design).
    for (int p = 0; p < nb.pwmSelfCount && p < 5; p++)
      Serial.printf("[WDPPWM:N=%d,DST=%d,S=%d]\n",
                    (int)nb.wcbNumber, (int)WCB_Number, (int)nb.pwmSelfPorts[p]);
  }
  // Config summary for the tool: WDP enabled, auto-join state, live peer count.
  // EN lets the Wizard distinguish "WDP disabled here" from "mesh just empty".
  Serial.printf("[WDPCFG:EN=%d,AUTOJOIN=%d,PEERS=%d]\n",
                wdpEnabled ? 1 : 0, wdpAutoJoin ? 1 : 0, activePeerCount());
  Serial.printf("[WDP:END,count=%d]\n", count);
}

static void printWdpStatus() {
  int n = 0;
  for (int i = 0; i < MAX_WCB_COUNT; i++) if (wdpNeighbors[i].valid) n++;
  Serial.printf("[WDP:en=%d,autojoin=%d,proto=%d,neighbors=%d,peers=%d]\n",
                wdpEnabled ? 1 : 0, wdpAutoJoin ? 1 : 0, WDP_PROTO_VERSION, n, activePeerCount());
}

void processWdpCommand(const String &args) {
  String a = args; a.trim();
  String au = a; au.toUpperCase();

  if (au == "" || au == "LIST") { printWdpList();   return; }
  if (au == "STATUS")           { printWdpStatus(); return; }
  if (au == "DUMP")             { printWdpDump();    return; }
  if (au == "DA")               { wdpDaPrint();      return; }   // serial-attached devices (@WDP1)
  if (au.startsWith("DA,"))     { wdpDaCommand(a.substring(3)); return; }   // DA,FORGET,... / DA,CLEAR
  // ?WDP,POLL — force convergence now: advertise ourselves + broadcast a SOLICIT so
  // every other board advertises within ~1 s instead of waiting for the 60 s backstop.
  if (au == "POLL") {
    if (!wdpEnabled || !etmEnabled) { Serial.println("[WDP] POLL needs WDP + ETM enabled"); return; }
    wdpSendAdvert();
    wdpSendSolicit();
    Serial.println("[WDP] polled: advertised + solicited the mesh");
    return;
  }
  if (au == "ON")  { wdpEnabled = true;  saveWdpSettings(); Serial.println("[WDP] enabled");  return; }
  if (au == "OFF") { wdpEnabled = false; saveWdpSettings(); Serial.println("[WDP] disabled"); return; }

  // ?WDP,AUTOJOIN         → report state (query never mutates)
  // ?WDP,AUTOJOIN,ON|OFF  → set it
  if (au.startsWith("AUTOJOIN")) {
    int c = a.indexOf(',');
    String arg = (c >= 0) ? a.substring(c + 1) : "";
    arg.trim(); arg.toUpperCase();
    if      (arg == "")    Serial.printf("[WDP] auto-join is %s\n", wdpAutoJoin ? "ON" : "OFF");
    else if (arg == "ON")  { wdpAutoJoin = true;  saveWdpSettings(); Serial.println("[WDP] auto-join enabled");  }
    else if (arg == "OFF") { wdpAutoJoin = false; saveWdpSettings(); Serial.println("[WDP] auto-join disabled"); }
    else                   Serial.println("[WDP] usage: ?WDP,AUTOJOIN[,ON|,OFF]");
    return;
  }

  // ?WDP,ADD,<id> — manually register a WCB as a (learned, persisted) peer
  if (au.startsWith("ADD")) {
    int c = a.indexOf(',');
    int id = (c >= 0) ? a.substring(c + 1).toInt() : 0;
    if (id >= 1 && id <= MAX_WCB_COUNT)
      Serial.printf(addActivePeer((uint8_t)id, true) ? "[WDP] added WCB%d as a peer\n"
                                                     : "[WDP] could not add WCB%d\n", id);
    else Serial.println("[WDP] usage: ?WDP,ADD,<id>");
    return;
  }

  // ?WDP,FORGET,<id> — drop one learned peer (floor peers are unaffected)
  if (au.startsWith("FORGET")) {
    int c = a.indexOf(',');
    int id = (c >= 0) ? a.substring(c + 1).toInt() : 0;
    if (id >= 1 && id <= MAX_WCB_COUNT) {
      removeActivePeer((uint8_t)id);
      wdpForgetNeighbor((uint8_t)id);
      Serial.printf("[WDP] forgot WCB%d\n", id);
    } else Serial.println("[WDP] usage: ?WDP,FORGET,<id>");
    return;
  }

  // ?WDP,CLEAR — drop ALL learned peers AND wipe the neighbor table
  if (au == "CLEAR") {
    clearAllLearnedPeers();
    memset(wdpNeighbors, 0, sizeof(wdpNeighbors));
    memset(wdpDaRemote, 0, sizeof(wdpDaRemote));   // neighbors' device lists go with them;
    memset(wdpDaPeers, 0, sizeof(wdpDaPeers));     // this board's own (?WDP,DA) stays
    Serial.println("[WDP] neighbor table + learned peers cleared");
    return;
  }

  // ?WDP,DETAIL,<n>  or  ?WDP,<n>  → drill into one neighbor
  if (au.startsWith("DETAIL")) {
    int c = a.indexOf(',');
    printWdpDetail(c >= 0 ? a.substring(c + 1).toInt() : 0);
    return;
  }
  int n = a.toInt();
  if (n > 0) { printWdpDetail(n); return; }

  Serial.printf("[WDP] unknown subcommand '%s' (LIST | <n> | DETAIL,n | STATUS | DUMP | DA | "
                "DA,FORGET,S<n>[,<type>] | DA,CLEAR | POLL | ON | OFF | AUTOJOIN[,ON|,OFF] | "
                "ADD,<id> | FORGET,<id> | CLEAR)\n", a.c_str());
}

// ==================== NVS + lifecycle ====================================

void loadWdpSettings() {
  preferences.begin("wdp_cfg", true);
  wdpEnabled  = preferences.getBool("en", true);        // default ON
  wdpAutoJoin = preferences.getBool("autojoin", true);  // default ON
  preferences.end();
}

void saveWdpSettings() {
  preferences.begin("wdp_cfg", false);
  preferences.putBool("en", wdpEnabled);
  preferences.putBool("autojoin", wdpAutoJoin);
  preferences.end();
}

void wdpBegin() {
  loadWdpSettings();
  memset(wdpNeighbors, 0, sizeof(wdpNeighbors));
  memset(wdpDaRemote, 0, sizeof(wdpDaRemote));
  memset(wdpDaPeers, 0, sizeof(wdpDaPeers));
  wdpDaLoad();                       // this board's saved WDP-DA devices (all "not heard since boot")
  wdpBootLeft   = 3;
  wdpNextBootMs = millis() + 1600;   // just after the ETM boot announce (1500)
  // Per-board phase stagger so periodic backstops from boards that booted
  // together don't all fire in the same tick.
  wdpNextAdvertMs = millis() + WDP_PERIOD_MS + (unsigned long)((WCB_Number % 16) * 500);
  if (wdpEnabled) Serial.println("[WDP] discovery enabled");
}
