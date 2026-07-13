#include "WCB_RemoteTerm.h"  // Must be first — redirects Serial → WCBDebugSerial
#include "WCB_WLED.h"
#include "WCB_Storage.h"
#include <Preferences.h>
#include <SoftwareSerial.h>

// ---- Externs provided by WCB.ino / WCB_Storage.cpp ---------------------
extern int           WCB_Number;
extern int           Default_WCB_Quantity;
extern bool          debugEnabled;
extern char          LocalFunctionIdentifier;
extern char          CommandCharacter;          // ';' — for reconstructing a routed ;L<id>
extern bool          lastReceivedViaESPNOW;      // one-hop routing cap
extern unsigned long baudRates[5];
extern bool          serialBroadcastEnabled[5];
extern bool          blockBroadcastFrom[5];
extern Preferences   preferences;

// updateBaudRate / saveBroadcastSettingsToPreferences / saveBroadcastBlockSettings
// / saveSerialLabelToPreferences are declared by WCB_Storage.h (included above).
// PWM/MP3 predicates + Kyber_Local / Maestro_Remote also come from WCB_Storage.h.
extern Stream &getSerialStream(int port);          // write target for the configured port
extern void    applyLiveBaud(int port, uint32_t baud);
extern bool    isSerialPortUsedForHCR(int port);   // WCB_HCR.cpp — for the conflict guard
extern void    sendESPNowMessage(uint8_t target, const char *message, bool useETM = true);

// ---- Module globals -----------------------------------------------------
WLEDConfig wledConfigs[MAX_WLED_PER_WCB] = {};

// Valid WLED serial baud rates (WLED default 115200).
static bool wledBaudValid(int b) {
  return b == 9600 || b == 19200 || b == 38400 || b == 57600 || b == 115200;
}

// Split a comma list, return field idx (0-based), trimmed. "" if absent.
static String wledField(const String &s, int idx) {
  int start = 0;
  for (int i = 0; i < idx; i++) {
    int c = s.indexOf(',', start);
    if (c < 0) return "";
    start = c + 1;
  }
  int end = s.indexOf(',', start);
  String f = (end < 0) ? s.substring(start) : s.substring(start, end);
  f.trim();
  return f;
}

// ==================== Slot helpers (mirror the Maestro model) ============

int8_t findWLEDSlotByID(uint8_t wledID) {
  for (int i = 0; i < MAX_WLED_PER_WCB; i++)
    if (wledConfigs[i].configured && wledConfigs[i].wledID == wledID) return (int8_t)i;
  return -1;
}

int8_t findEmptyWLEDSlot() {
  for (int i = 0; i < MAX_WLED_PER_WCB; i++)
    if (!wledConfigs[i].configured) return (int8_t)i;
  return -1;
}

bool isWLEDConfigured(uint8_t wledID) {
  return findWLEDSlotByID(wledID) >= 0;
}

// True if a LOCAL WLED other than exceptSlot occupies `port`.
static bool wledPortUsedByOtherLocal(int port, int exceptSlot) {
  for (int i = 0; i < MAX_WLED_PER_WCB; i++)
    if (i != exceptSlot && wledConfigs[i].configured &&
        wledConfigs[i].remoteWCB == 0 && wledConfigs[i].serialPort == (uint8_t)port)
      return true;
  return false;
}

// ==================== Port-conflict Query ================================

bool isSerialPortUsedForWLED(int port) {
  for (int i = 0; i < MAX_WLED_PER_WCB; i++)
    if (wledConfigs[i].configured && wledConfigs[i].remoteWCB == 0 &&
        wledConfigs[i].serialPort == (uint8_t)port)
      return true;
  return false;
}

// ==================== Lifecycle =========================================

void beginWLED() {
  for (int i = 0; i < MAX_WLED_PER_WCB; i++) {
    WLEDConfig &c = wledConfigs[i];
    if (c.configured && c.remoteWCB == 0 && c.serialPort >= 1 && c.serialPort <= 5)
      Serial.printf("[WLED] %d active on S%d at %lu baud\n",
                    c.wledID, c.serialPort, (unsigned long)c.baudRate);
  }
}

// ==================== Runtime (;L[id],...) ==============================

// Send one JSON document to a WLED port (newline-terminated, per its parser).
static void wledSend(Stream &out, const String &json) {
  out.print(json);
  out.print('\n');
  if (debugEnabled) Serial.printf("[WLED-DBG] TX -> %s\n", json.c_str());
}

// Translate one ;L verb (ON/OFF/BRI/PS/…) to WLED JSON and write it to `port`.
// `cmd` is the verb + args WITHOUT the leading L / id (e.g. "PS,2", "JSON,{...}").
static void wledDispatchLocal(int port, const String &cmd) {
  String body = cmd; body.trim();
  if (body.length() == 0) { Serial.println("[WLED] Empty ;L command"); return; }

  String verb = wledField(body, 0);
  String vU   = verb; vU.toUpperCase();
  Stream &out = getSerialStream(port);

  // ---- JSON: raw passthrough — everything after "JSON," verbatim ---------
  // (may contain commas / braces, so do NOT field-split it). Escape hatch for
  // anything the compact verbs don't cover.
  if (vU == "JSON") {
    int firstComma = body.indexOf(',');
    String payload = (firstComma < 0) ? "" : body.substring(firstComma + 1);
    payload.trim();
    if (payload.length() == 0) { Serial.println("[WLED] JSON needs a payload"); return; }
    wledSend(out, payload);
    return;
  }

  if (vU == "ON")     { wledSend(out, "{\"on\":true}");  return; }
  if (vU == "OFF")    { wledSend(out, "{\"on\":false}"); return; }
  if (vU == "TOGGLE") { wledSend(out, "{\"on\":\"t\"}"); return; }

  if (vU == "BRI") {
    int bri = constrain(wledField(body, 1).toInt(), 0, 255);
    wledSend(out, "{\"bri\":" + String(bri) + "}");
    return;
  }

  if (vU == "PS") {            // recall preset (primary workflow)
    String n = wledField(body, 1);
    if (n.length() == 0) { Serial.println("[WLED] Usage: ;L[id],PS,<preset#>"); return; }
    wledSend(out, "{\"ps\":" + String(n.toInt()) + "}");
    return;
  }

  if (vU == "COL") {           // solid colour on segment 0: RRGGBB or RRGGBBWW
    String hex = wledField(body, 1);
    if (hex.startsWith("#")) hex = hex.substring(1);
    if (hex.length() != 6 && hex.length() != 8) {
      Serial.println("[WLED] Usage: ;L[id],COL,RRGGBB  (or RRGGBBWW for RGBW)");
      return;
    }
    unsigned long v = strtoul(hex.c_str(), nullptr, 16);
    int r, g, b, w = -1;
    if (hex.length() == 8) { r = (v >> 24) & 0xFF; g = (v >> 16) & 0xFF; b = (v >> 8) & 0xFF; w = v & 0xFF; }
    else                   { r = (v >> 16) & 0xFF; g = (v >> 8)  & 0xFF; b = v & 0xFF; }
    String col = "[" + String(r) + "," + String(g) + "," + String(b);
    if (w >= 0) col += "," + String(w);
    col += "]";
    wledSend(out, "{\"seg\":[{\"col\":[" + col + "]}]}");
    return;
  }

  if (vU == "FX") {            // effect by index, optional speed + intensity
    String n = wledField(body, 1);
    if (n.length() == 0) { Serial.println("[WLED] Usage: ;L[id],FX,<index>[,<speed>,<intensity>]"); return; }
    String seg = "{\"fx\":" + String(n.toInt());
    String sx = wledField(body, 2);
    String ix = wledField(body, 3);
    if (sx.length()) seg += ",\"sx\":" + String(constrain(sx.toInt(), 0, 255));
    if (ix.length()) seg += ",\"ix\":" + String(constrain(ix.toInt(), 0, 255));
    seg += "}";
    wledSend(out, "{\"seg\":[" + seg + "]}");
    return;
  }

  if (vU == "PAL") {           // palette by index
    String n = wledField(body, 1);
    if (n.length() == 0) { Serial.println("[WLED] Usage: ;L[id],PAL,<index>"); return; }
    wledSend(out, "{\"seg\":[{\"pal\":" + String(n.toInt()) + "}]}");
    return;
  }

  Serial.printf("[WLED] Unknown ;L command: %s  (ON/OFF/TOGGLE/BRI/PS/COL/FX/PAL/JSON)\n",
                verb.c_str());
}

void processWLEDRuntimeCommand(const String &message) {
  // message is the command WITHOUT the leading command char, starting with L/l:
  //   "L,PS,2"   "L3,PS,2"   "L,ON"   "L2,JSON,{...}"
  String body = message; body.trim();
  if (body.length() && (body[0] == 'L' || body[0] == 'l')) body = body.substring(1);

  // Parse an optional leading numeric ID (";L3,…" -> 3; ";L,…" -> 0 = bare/local).
  int i = 0;
  while (i < (int)body.length() && isDigit(body[i])) i++;
  uint8_t id = (i > 0) ? (uint8_t)body.substring(0, i).toInt() : 0;
  String rest = body.substring(i);
  rest.trim();
  if (rest.startsWith(",")) rest = rest.substring(1);
  rest.trim();

  // ---- Bare ;L — act on THIS board's own (lowest-id) LOCAL WLED ----------
  if (id == 0) {
    int8_t slot = -1;
    for (int s = 0; s < MAX_WLED_PER_WCB; s++)
      if (wledConfigs[s].configured && wledConfigs[s].remoteWCB == 0 && wledConfigs[s].serialPort > 0)
        if (slot < 0 || wledConfigs[s].wledID < wledConfigs[slot].wledID) slot = (int8_t)s;
    if (slot < 0) {
      Serial.printf("[WLED] No local WLED — use %cWLED,<id>:W%dS<port>:115200\n",
                    LocalFunctionIdentifier, WCB_Number);
      return;
    }
    wledDispatchLocal(wledConfigs[slot].serialPort, rest);
    return;
  }

  // ---- ;L<id> — the WLED with that ID, local or routed to its host -------
  int8_t slot = findWLEDSlotByID(id);
  if (slot < 0) { Serial.printf("[WLED] WLED %d not configured\n", id); return; }
  WLEDConfig &cfg = wledConfigs[slot];

  if (cfg.remoteWCB == 0 && cfg.serialPort >= 1 && cfg.serialPort <= 5) {
    wledDispatchLocal(cfg.serialPort, rest);        // local device
  } else if (cfg.remoteWCB > 0) {
    // Remote proxy — forward the original ;L<id>,rest to the host (ETM-ACKed).
    // The one-hop cap: a command that ARRIVED over the mesh was already routed to
    // us; we only hold a proxy, not the physical device, so never re-forward.
    if (!lastReceivedViaESPNOW) {
      String fwd = String(CommandCharacter) + "L" + String(id) +
                   (rest.length() ? ("," + rest) : "");
      sendESPNowMessage(cfg.remoteWCB, fwd.c_str());
      if (debugEnabled) Serial.printf("[WLED] %d -> WCB%d: %s\n", id, cfg.remoteWCB, rest.c_str());
    }
  }
}

// ==================== Port reservation ==================================

// Reserve a local port for WLED: apply baud, disable broadcast both ways (don't
// feed mesh traffic to WLED, don't re-broadcast WLED bytes), label it "WLED".
static void wledReserveLocalPort(int port, uint32_t baud) {
  if (baud != baudRates[port - 1]) updateBaudRate(port, baud);
  else                             applyLiveBaud(port, baud);

  if (serialBroadcastEnabled[port - 1]) {
    serialBroadcastEnabled[port - 1] = false;
    saveBroadcastSettingsToPreferences();
    Serial.printf("  ⚠️  Disabled broadcast output on S%d (WLED port)\n", port);
  }
  if (!blockBroadcastFrom[port - 1]) {
    blockBroadcastFrom[port - 1] = true;
    saveBroadcastBlockSettings();
    Serial.printf("  ⚠️  Disabled broadcast input on S%d (WLED port)\n", port);
  }
  saveSerialLabelToPreferences(port, "WLED");
}

// Release a local port no WLED uses anymore: re-enable broadcast, reset baud,
// clear the label.
static void wledReleaseLocalPort(int port) {
  if (!serialBroadcastEnabled[port - 1]) {
    serialBroadcastEnabled[port - 1] = true;
    saveBroadcastSettingsToPreferences();
  }
  if (blockBroadcastFrom[port - 1]) {
    blockBroadcastFrom[port - 1] = false;
    saveBroadcastBlockSettings();
  }
  updateBaudRate(port, 9600);
  saveSerialLabelToPreferences(port, "");
  Serial.printf("  ✓ Released S%d (old WLED port)\n", port);
}

// ==================== Configuration (?WLED,...) =========================

void clearWLEDConfig() {
  bool portUsed[6] = { false };
  for (int i = 0; i < MAX_WLED_PER_WCB; i++)
    if (wledConfigs[i].configured && wledConfigs[i].remoteWCB == 0 &&
        wledConfigs[i].serialPort >= 1 && wledConfigs[i].serialPort <= 5)
      portUsed[wledConfigs[i].serialPort] = true;

  memset(wledConfigs, 0, sizeof(wledConfigs));
  for (int i = 0; i < MAX_WLED_PER_WCB; i++) wledConfigs[i].baudRate = 115200;
  saveWLEDSettings();

  for (int p = 1; p <= 5; p++) if (portUsed[p]) wledReleaseLocalPort(p);
  Serial.println("[WLED] All WLED configuration cleared");
}

void clearWLEDByID(const String &idArg) {
  int id = idArg.toInt();
  if (id < 1 || id > 9) { Serial.printf("[WLED] Usage: %cWLED,CLEAR,<id 1-9>\n", LocalFunctionIdentifier); return; }
  int8_t slot = findWLEDSlotByID((uint8_t)id);
  if (slot < 0) { Serial.printf("[WLED] WLED %d not configured\n", id); return; }

  uint8_t port = (wledConfigs[slot].remoteWCB == 0) ? wledConfigs[slot].serialPort : 0;
  memset(&wledConfigs[slot], 0, sizeof(WLEDConfig));
  wledConfigs[slot].baudRate = 115200;
  saveWLEDSettings();

  if (port >= 1 && port <= 5 && !wledPortUsedByOtherLocal(port, slot)) wledReleaseLocalPort(port);
  Serial.printf("[WLED] WLED %d cleared\n", id);
}

void configureWLED(const String &args) {
  String a = args; a.trim();
  String aU = a;   aU.toUpperCase();

  if (aU == "LIST")   { printWLEDSettings(); return; }
  if (aU == "STATUS") { printWLEDStatus();   return; }
  if (aU == "CLEAR")  { clearWLEDConfig();   return; }
  if (aU.startsWith("CLEAR,")) { clearWLEDByID(a.substring(6)); return; }

  int    wledID;
  String dest;                 // "W<wcb>S<port>"
  int    baudRate;

  if (aU.startsWith("PORT,")) {
    // Legacy: ?WLED,PORT,S<port>:<baud>  ->  WLED id 1, local to this board.
    String spec = a.substring(5); spec.trim();     // "S1:115200"
    String specU = spec; specU.toUpperCase();
    int colon = spec.indexOf(':');
    if (!specU.startsWith("S") || colon < 0) {
      Serial.printf("[WLED] Legacy usage: %cWLED,PORT,S<port>:<baud>\n", LocalFunctionIdentifier);
      return;
    }
    wledID   = 1;
    dest     = "W" + String(WCB_Number) + spec.substring(0, colon);   // "W<self>S<port>"
    baudRate = spec.substring(colon + 1).toInt();
  } else {
    // New: ?WLED,<id>:W<wcb>S<port>:<baud>   (e.g. 1:W2S1:115200)
    int c1 = a.indexOf(':');
    if (c1 < 0) {
      Serial.printf("[WLED] Usage: %cWLED,<id>:W<wcb>S<port>:<baud>  (e.g. 1:W%dS1:115200)\n",
                    LocalFunctionIdentifier, WCB_Number);
      return;
    }
    wledID = a.substring(0, c1).toInt();
    String rest = a.substring(c1 + 1);                // "W<wcb>S<port>:<baud>"
    int c2 = rest.indexOf(':');
    if (c2 < 0) { Serial.printf("[WLED] Missing baud. Use ...S<port>:<baud>\n"); return; }
    dest     = rest.substring(0, c2);                 // "W<wcb>S<port>"
    baudRate = rest.substring(c2 + 1).toInt();
  }

  if (wledID < 1 || wledID > 9) { Serial.println("[WLED] Invalid WLED ID. Must be 1-9"); return; }

  String destU = dest; destU.toUpperCase();
  int wIdx = destU.indexOf('W');
  int sIdx = destU.indexOf('S');
  if (wIdx < 0 || sIdx < 0 || sIdx <= wIdx) {
    Serial.println("[WLED] Destination must be W<wcb>S<port> (e.g. W2S1)");
    return;
  }
  int targetWCB  = dest.substring(wIdx + 1, sIdx).toInt();
  int serialPort = dest.substring(sIdx + 1).toInt();

  if (!wledBaudValid(baudRate)) {
    Serial.println("[WLED] Baud must be 9600/19200/38400/57600/115200 (WLED default 115200)");
    return;
  }

  // ---- REMOTE (hosted on another board): store a proxy, no local port ----
  if (targetWCB != WCB_Number) {
    if (targetWCB < 1 || targetWCB > Default_WCB_Quantity) {
      Serial.printf("[WLED] Invalid WCB number. Must be W1-W%d\n", Default_WCB_Quantity);
      return;
    }
    // One slot per id — reuse the id's existing slot (incl. converting a former
    // local WLED into a remote proxy) instead of leaving a duplicate.
    int8_t  slot    = findWLEDSlotByID((uint8_t)wledID);
    uint8_t oldPort = (slot >= 0 && wledConfigs[slot].remoteWCB == 0) ? wledConfigs[slot].serialPort : 0;
    if (slot < 0) slot = findEmptyWLEDSlot();
    if (slot < 0) { Serial.printf("[WLED] No free slots (max %d)\n", MAX_WLED_PER_WCB); return; }
    wledConfigs[slot] = { (uint8_t)wledID, 0, (uint8_t)targetWCB, true, (uint32_t)baudRate };
    saveWLEDSettings();
    // Was local, now remote — free the old local port if nothing else uses it.
    if (oldPort >= 1 && oldPort <= 5 && !wledPortUsedByOtherLocal(oldPort, slot))
      wledReleaseLocalPort(oldPort);
    Serial.printf("[WLED] WLED %d: remote on WCB%d (slot %d)\n", wledID, targetWCB, slot + 1);
    return;
  }

  // ---- LOCAL (hosted on THIS board) --------------------------------------
  if (serialPort < 1 || serialPort > 5) { Serial.println("[WLED] Invalid serial port. Must be S1-S5"); return; }
  if (serialPort >= 3 && baudRate > 9600) {
    Serial.println("\n⚠️  =============== WARNING ===============");
    Serial.printf("S%d is SOFTWARE SERIAL — unreliable above 9600 baud\n", serialPort);
    Serial.println("WLED wants 115200 — use a HARDWARE port (S1/S2).");
    Serial.println("❌ CONFIGURATION BLOCKED!");
    Serial.printf("  Use: %cWLED,%d:W%dS1:115200   or   S2:115200\n",
                  LocalFunctionIdentifier, wledID, WCB_Number);
    Serial.println("=========================================\n");
    return;
  }

  // Reject a port already claimed by PWM / Kyber / MP3 / HCR. Mirrors the HCR/MP3
  // guards so two subsystems can't silently share a UART. Does NOT check WLED
  // itself, so re-configuring a WLED on its own port is fine.
  if (isSerialPortPWMOutput(serialPort) || isSerialPortUsedForPWMInput(serialPort) ||
      isSerialPortUsedForMP3(serialPort) || isSerialPortUsedForHCR(serialPort) ||
      (serialPort == 1 && (Kyber_Local || Maestro_Remote)) ||
      (serialPort == 2 && Kyber_Local)) {
    Serial.printf("[WLED] S%d already in use by PWM/Kyber/MP3/HCR - config blocked\n", serialPort);
    return;
  }

  // A WLED ID is ONE physical device — exactly one slot per id. REUSE the id's
  // existing slot (moving port, changing baud, or re-homing a remote proxy to
  // local all land here) so we never leave a duplicate that findWLEDSlotByID
  // would resolve to the wrong/stale target.
  int8_t  slot    = findWLEDSlotByID((uint8_t)wledID);
  uint8_t oldPort = (slot >= 0 && wledConfigs[slot].remoteWCB == 0) ? wledConfigs[slot].serialPort : 0;
  if (slot < 0) slot = findEmptyWLEDSlot();
  if (slot < 0) { Serial.printf("[WLED] No free slots (max %d)\n", MAX_WLED_PER_WCB); return; }

  wledReserveLocalPort(serialPort, (uint32_t)baudRate);
  wledConfigs[slot] = { (uint8_t)wledID, (uint8_t)serialPort, 0, true, (uint32_t)baudRate };
  saveWLEDSettings();

  // Free the old local port if this id moved off it and no OTHER local WLED uses it.
  if (oldPort >= 1 && oldPort <= 5 && oldPort != (uint8_t)serialPort &&
      !wledPortUsedByOtherLocal(oldPort, slot))
    wledReleaseLocalPort(oldPort);

  Serial.printf("[WLED] WLED %d: local S%d at %d baud (slot %d)\n", wledID, serialPort, baudRate, slot + 1);
}

// ==================== Status / List =====================================

void printWLEDSettings() {
  Serial.println("---- WLED Configuration ----");
  bool any = false;
  for (int i = 0; i < MAX_WLED_PER_WCB; i++) {
    WLEDConfig &c = wledConfigs[i];
    if (!c.configured) continue;
    any = true;
    if (c.remoteWCB == 0)
      Serial.printf("  WLED %d : local S%d @ %lu baud\n", c.wledID, c.serialPort, (unsigned long)c.baudRate);
    else
      Serial.printf("  WLED %d : remote on WCB%d @ %lu baud\n", c.wledID, c.remoteWCB, (unsigned long)c.baudRate);
  }
  if (!any) Serial.printf("  Not configured.  Use %cWLED,<id>:W%dS<port>:115200\n",
                          LocalFunctionIdentifier, WCB_Number);
}

void printWLEDStatus() {
  int n = 0;
  for (int i = 0; i < MAX_WLED_PER_WCB; i++) if (wledConfigs[i].configured) n++;
  Serial.printf("[WLED:cnt=%d]\n", n);
  for (int i = 0; i < MAX_WLED_PER_WCB; i++) {
    WLEDConfig &c = wledConfigs[i];
    if (!c.configured) continue;
    if (c.remoteWCB == 0)
      Serial.printf("[WLED:id=%d,port=%d,baud=%lu]\n", c.wledID, c.serialPort, (unsigned long)c.baudRate);
    else
      Serial.printf("[WLED:id=%d,wcb=%d,baud=%lu]\n", c.wledID, c.remoteWCB, (unsigned long)c.baudRate);
  }
}

void printWLEDBackup(String &chainedConfig, String &chainedConfigDefault,
                     char delimiter, bool printToSerial,
                     const String &defSep, const String &defFunc) {
  for (int i = 0; i < MAX_WLED_PER_WCB; i++) {
    WLEDConfig &c = wledConfigs[i];
    if (!c.configured) continue;

    // Local: W<self>S<port>. Remote proxy: W<host>S0 (the port is the host's
    // business; the receiver ignores S for a non-self target). Round-trips through
    // configureWLED either way.
    String suffix;
    if (c.remoteWCB == 0)
      suffix = "WLED," + String(c.wledID) + ":W" + String(WCB_Number) + "S" + String(c.serialPort) +
               ":" + String(c.baudRate);
    else
      suffix = "WLED," + String(c.wledID) + ":W" + String(c.remoteWCB) + "S0" +
               ":" + String(c.baudRate);

    String cmd = String(LocalFunctionIdentifier) + suffix;
    if (printToSerial) Serial.println(cmd);
    chainedConfig        += String(delimiter) + cmd;
    chainedConfigDefault += defSep + defFunc + suffix;
  }
}

// ==================== WDP auto-config ===================================

// Auto-add (or baud-refresh) a REMOTE WLED proxy learned from a WDP advert.
// Mirrors maestroAutoAddRemote: first-host-wins, idempotent, persisted. Returns
// true iff a slot was added or its baud changed.
bool wledAutoAddRemote(uint8_t wledID, uint8_t hostWCB, uint32_t baud) {
  if (wledID < 1 || wledID > 9)            return false;
  if (hostWCB == 0 || hostWCB == WCB_Number) return false;

  // One slot per id. If the id is already configured: refresh baud only when it's
  // a proxy to THIS same host; never shadow a local WLED or re-home a proxy that
  // points at a different host (first-host-wins).
  int8_t slot = findWLEDSlotByID(wledID);
  if (slot >= 0) {
    if (wledConfigs[slot].remoteWCB == hostWCB && baud != 0 &&
        wledConfigs[slot].baudRate != baud) {
      wledConfigs[slot].baudRate = baud;
      saveWLEDSettings();
      Serial.printf("[WDP] WLED %d @ WCB%d baud updated to %lu\n", wledID, hostWCB, (unsigned long)baud);
      return true;
    }
    return false;
  }

  slot = findEmptyWLEDSlot();
  if (slot < 0) {
    if (debugEnabled)
      Serial.printf("[WDP] WLED %d @ WCB%d heard but no free slot (max %d)\n", wledID, hostWCB, MAX_WLED_PER_WCB);
    return false;
  }
  wledConfigs[slot] = { wledID, 0, hostWCB, true, baud };
  saveWLEDSettings();
  Serial.printf("[WDP] auto-added remote WLED %d on WCB%d @ %lu baud (slot %d)\n",
                wledID, hostWCB, (unsigned long)baud, slot + 1);
  return true;
}

// ==================== NVS storage =======================================

void saveWLEDSettings() {
  preferences.begin("wled_cfg", false);
  for (int i = 0; i < MAX_WLED_PER_WCB; i++) {
    String kID = "w" + String(i) + "_id", kPort = "w" + String(i) + "_port",
           kWCB = "w" + String(i) + "_wcb", kEn = "w" + String(i) + "_en",
           kBaud = "w" + String(i) + "_baud";
    preferences.putUChar(kID.c_str(),   wledConfigs[i].wledID);
    preferences.putUChar(kPort.c_str(), wledConfigs[i].serialPort);
    preferences.putUChar(kWCB.c_str(),  wledConfigs[i].remoteWCB);
    preferences.putBool (kEn.c_str(),   wledConfigs[i].configured);
    preferences.putUInt (kBaud.c_str(), wledConfigs[i].baudRate);
  }
  // Purge the legacy singular keys so a later CLEAR can't resurrect the old
  // config via migration (see loadWLEDSettings).
  preferences.remove("port");
  preferences.remove("baud");
  preferences.remove("en");
  preferences.end();
}

void loadWLEDSettings() {
  preferences.begin("wled_cfg", true);
  bool anyNew = false;
  for (int i = 0; i < MAX_WLED_PER_WCB; i++) {
    String kID = "w" + String(i) + "_id", kPort = "w" + String(i) + "_port",
           kWCB = "w" + String(i) + "_wcb", kEn = "w" + String(i) + "_en",
           kBaud = "w" + String(i) + "_baud";
    wledConfigs[i].wledID     = preferences.getUChar(kID.c_str(),   0);
    wledConfigs[i].serialPort = preferences.getUChar(kPort.c_str(), 0);
    wledConfigs[i].remoteWCB  = preferences.getUChar(kWCB.c_str(),  0);
    wledConfigs[i].configured = preferences.getBool (kEn.c_str(),   false);
    wledConfigs[i].baudRate   = preferences.getUInt (kBaud.c_str(), 115200);
    if (wledConfigs[i].configured) anyNew = true;
  }
  // Legacy singular scheme: bare "port"/"baud"/"en". Migrate once to WLED id 1.
  bool    legacyEn   = preferences.getBool ("en",   false);
  uint8_t legacyPort = preferences.getUChar("port", 0);
  uint32_t legacyBaud = preferences.getUInt("baud", 115200);
  preferences.end();

  if (!anyNew && legacyEn && legacyPort >= 1 && legacyPort <= 5) {
    wledConfigs[0] = { 1, legacyPort, 0, true, legacyBaud };
    saveWLEDSettings();   // rewrites in the new format AND purges the legacy keys
    Serial.printf("[WLED] migrated legacy config -> WLED 1 local S%d @ %lu\n",
                  legacyPort, (unsigned long)legacyBaud);
  }

  for (int i = 0; i < MAX_WLED_PER_WCB; i++) {
    WLEDConfig &c = wledConfigs[i];
    if (!c.configured) continue;
    if (c.remoteWCB == 0)
      Serial.printf("[WLED] Loaded: WLED %d -> local S%d @ %lu baud\n", c.wledID, c.serialPort, (unsigned long)c.baudRate);
    else
      Serial.printf("[WLED] Loaded: WLED %d -> remote WCB%d @ %lu baud\n", c.wledID, c.remoteWCB, (unsigned long)c.baudRate);
  }
}
