#include "WCB_RemoteTerm.h"  // Must be first — redirects Serial → WCBDebugSerial
#include "WCB_WLED.h"
#include "WCB_Storage.h"
#include <Preferences.h>
#include <SoftwareSerial.h>

// ---- Externs provided by WCB.ino / WCB_Storage.cpp ---------------------
extern int           WCB_Number;
extern bool          debugEnabled;
extern char          LocalFunctionIdentifier;
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

// ---- Module globals -----------------------------------------------------
WLEDConfig wledConfig = {};

static Stream *_wledPort = nullptr;   // concrete port stream (bound in beginWLED)

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

// Send one JSON document to WLED (newline-terminated, per its serial parser).
static void wledSend(const String &json) {
  if (!_wledPort) return;
  _wledPort->print(json);
  _wledPort->print('\n');
  if (debugEnabled) Serial.printf("[WLED-DBG] TX -> %s\n", json.c_str());
}

// ==================== Port-conflict Query ================================

bool isSerialPortUsedForWLED(int port) {
  return wledConfig.configured && wledConfig.serialPort == (uint8_t)port;
}

// ==================== Lifecycle =========================================

void beginWLED() {
  _wledPort = nullptr;
  if (!wledConfig.configured || wledConfig.serialPort < 1 || wledConfig.serialPort > 5) {
    return;
  }
  _wledPort = &getSerialStream(wledConfig.serialPort);
  Serial.printf("[WLED] Active on S%d at %lu baud\n",
                wledConfig.serialPort, (unsigned long)wledConfig.baudRate);
}

// ==================== Runtime (;L,...) ==================================

void processWLEDRuntimeCommand(const String &message) {
  if (!wledConfig.configured || !_wledPort) {
    Serial.println("[WLED] Not configured — use ?WLED,PORT,Sx:115200 first");
    return;
  }

  // Strip the leading "L" / "L," (from ;L routing).
  String body = message;
  body.trim();
  if (body.length() >= 1 && (body[0] == 'L' || body[0] == 'l')) {
    body = (body.length() >= 2 && body[1] == ',') ? body.substring(2)
                                                  : body.substring(1);
  }
  body.trim();
  if (body.length() == 0) { Serial.println("[WLED] Empty ;L command"); return; }

  String verb = wledField(body, 0);
  String vU   = verb; vU.toUpperCase();

  // ---- JSON: raw passthrough — everything after "JSON," verbatim ---------
  // (may contain commas / braces, so do NOT field-split it). Escape hatch for
  // anything the compact verbs don't cover.
  if (vU == "JSON") {
    int firstComma = body.indexOf(',');
    String payload = (firstComma < 0) ? "" : body.substring(firstComma + 1);
    payload.trim();
    if (payload.length() == 0) { Serial.println("[WLED] JSON needs a payload"); return; }
    wledSend(payload);
    return;
  }

  if (vU == "ON")     { wledSend("{\"on\":true}");  return; }
  if (vU == "OFF")    { wledSend("{\"on\":false}"); return; }
  if (vU == "TOGGLE") { wledSend("{\"on\":\"t\"}"); return; }

  if (vU == "BRI") {
    int bri = constrain(wledField(body, 1).toInt(), 0, 255);
    wledSend("{\"bri\":" + String(bri) + "}");
    return;
  }

  if (vU == "PS") {            // recall preset (primary workflow)
    String n = wledField(body, 1);
    if (n.length() == 0) { Serial.println("[WLED] Usage: ;L,PS,<preset#>"); return; }
    wledSend("{\"ps\":" + String(n.toInt()) + "}");
    return;
  }

  if (vU == "COL") {           // solid colour on segment 0: RRGGBB or RRGGBBWW
    String hex = wledField(body, 1);
    if (hex.startsWith("#")) hex = hex.substring(1);
    if (hex.length() != 6 && hex.length() != 8) {
      Serial.println("[WLED] Usage: ;L,COL,RRGGBB  (or RRGGBBWW for RGBW)");
      return;
    }
    unsigned long v = strtoul(hex.c_str(), nullptr, 16);
    int r, g, b, w = -1;
    if (hex.length() == 8) { r = (v >> 24) & 0xFF; g = (v >> 16) & 0xFF; b = (v >> 8) & 0xFF; w = v & 0xFF; }
    else                   { r = (v >> 16) & 0xFF; g = (v >> 8)  & 0xFF; b = v & 0xFF; }
    String col = "[" + String(r) + "," + String(g) + "," + String(b);
    if (w >= 0) col += "," + String(w);
    col += "]";
    wledSend("{\"seg\":[{\"col\":[" + col + "]}]}");
    return;
  }

  if (vU == "FX") {            // effect by index, optional speed + intensity
    String n = wledField(body, 1);
    if (n.length() == 0) { Serial.println("[WLED] Usage: ;L,FX,<index>[,<speed>,<intensity>]"); return; }
    String seg = "{\"fx\":" + String(n.toInt());
    String sx = wledField(body, 2);
    String ix = wledField(body, 3);
    if (sx.length()) seg += ",\"sx\":" + String(constrain(sx.toInt(), 0, 255));
    if (ix.length()) seg += ",\"ix\":" + String(constrain(ix.toInt(), 0, 255));
    seg += "}";
    wledSend("{\"seg\":[" + seg + "]}");
    return;
  }

  if (vU == "PAL") {           // palette by index
    String n = wledField(body, 1);
    if (n.length() == 0) { Serial.println("[WLED] Usage: ;L,PAL,<index>"); return; }
    wledSend("{\"seg\":[{\"pal\":" + String(n.toInt()) + "}]}");
    return;
  }

  Serial.printf("[WLED] Unknown ;L command: %s  (ON/OFF/TOGGLE/BRI/PS/COL/FX/PAL/JSON)\n",
                verb.c_str());
}

// ==================== Configuration (?WLED,...) =========================

void clearWLEDConfig() {
  uint8_t freedPort = wledConfig.serialPort;

  _wledPort = nullptr;
  memset(&wledConfig, 0, sizeof(wledConfig));
  wledConfig.configured = false;
  wledConfig.baudRate   = 115200;

  saveWLEDSettings();
  Serial.println("[WLED] Configuration cleared");

  if (freedPort > 0) {
    if (!serialBroadcastEnabled[freedPort - 1]) {
      serialBroadcastEnabled[freedPort - 1] = true;
      saveBroadcastSettingsToPreferences();
      Serial.printf("  ✓ Re-enabled broadcast output on S%d\n", freedPort);
    }
    if (blockBroadcastFrom[freedPort - 1]) {
      blockBroadcastFrom[freedPort - 1] = false;
      saveBroadcastBlockSettings();
      Serial.printf("  ✓ Re-enabled broadcast input on S%d\n", freedPort);
    }
    updateBaudRate(freedPort, 9600);
    saveSerialLabelToPreferences(freedPort, "");
    Serial.printf("  ✓ Released S%d (old WLED port)\n", freedPort);
  }
}

static void wledReservePort(int serialPort, int baudRate) {
  // Release old port if moving.
  if (wledConfig.configured && wledConfig.serialPort > 0 &&
      wledConfig.serialPort != (uint8_t)serialPort) {
    uint8_t oldPort = wledConfig.serialPort;
    if (!serialBroadcastEnabled[oldPort - 1]) {
      serialBroadcastEnabled[oldPort - 1] = true;
      saveBroadcastSettingsToPreferences();
    }
    if (blockBroadcastFrom[oldPort - 1]) {
      blockBroadcastFrom[oldPort - 1] = false;
      saveBroadcastBlockSettings();
    }
    saveSerialLabelToPreferences(oldPort, "");
    Serial.printf("  ✓ Released S%d (old WLED port)\n", oldPort);
  }

  // Apply baud live (handles S1/S2 divisor + S3-5 software re-init).
  if ((uint32_t)baudRate != baudRates[serialPort - 1]) {
    updateBaudRate(serialPort, baudRate);
  } else {
    applyLiveBaud(serialPort, (uint32_t)baudRate);
  }

  // Reserve the port: the WCB drives WLED on it, so disable broadcast both ways
  // (don't feed mesh traffic to WLED, don't re-broadcast any WLED replies).
  if (serialBroadcastEnabled[serialPort - 1]) {
    serialBroadcastEnabled[serialPort - 1] = false;
    saveBroadcastSettingsToPreferences();
    Serial.printf("  ⚠️  Disabled broadcast output on S%d (WLED port)\n", serialPort);
  }
  if (!blockBroadcastFrom[serialPort - 1]) {
    blockBroadcastFrom[serialPort - 1] = true;
    saveBroadcastBlockSettings();
    Serial.printf("  ⚠️  Disabled broadcast input on S%d (WLED port)\n", serialPort);
  }
  saveSerialLabelToPreferences(serialPort, "WLED");

  wledConfig.serialPort = (uint8_t)serialPort;
  wledConfig.baudRate   = (uint32_t)baudRate;
  wledConfig.configured = true;

  saveWLEDSettings();
  beginWLED();
  Serial.printf("[WLED] Configured on S%d at %d baud\n", serialPort, baudRate);
}

void configureWLED(const String &args) {
  String a = args; a.trim();
  String aU = a;   aU.toUpperCase();

  if (aU == "LIST")   { printWLEDSettings(); return; }
  if (aU == "CLEAR")  { clearWLEDConfig();   return; }
  if (aU == "STATUS") { printWLEDStatus();   return; }

  // ---- PORT,S<port>:<baud> -------------------------------------------
  String spec = a;
  if (aU.startsWith("PORT,")) spec = a.substring(5);   // tolerate ?WLED,PORT,S1:115200
  spec.trim();
  String specU = spec; specU.toUpperCase();

  if (!specU.startsWith("S")) {
    Serial.printf("[WLED] Invalid. Use: %cWLED,PORT,S<port>:<baud>  (e.g. S1:115200)\n",
                  LocalFunctionIdentifier);
    return;
  }
  int colon = spec.indexOf(':');
  if (colon < 0) {
    Serial.printf("[WLED] Missing baud. Use: %cWLED,PORT,S<port>:<baud>\n",
                  LocalFunctionIdentifier);
    return;
  }
  int serialPort = spec.substring(1, colon).toInt();
  int baudRate   = spec.substring(colon + 1).toInt();

  if (serialPort < 1 || serialPort > 5) {
    Serial.println("[WLED] Invalid serial port. Must be S1-S5"); return;
  }
  if (baudRate != 9600 && baudRate != 19200 && baudRate != 38400 &&
      baudRate != 57600 && baudRate != 115200) {
    Serial.println("[WLED] Baud must be 9600/19200/38400/57600/115200 (WLED default 115200)"); return;
  }
  if (serialPort >= 3 && baudRate > 9600) {
    Serial.println("\n⚠️  =============== WARNING ===============");
    Serial.printf("S%d is SOFTWARE SERIAL — unreliable above 9600 baud\n", serialPort);
    Serial.println("WLED wants 115200 — use a HARDWARE port (S1/S2).");
    Serial.println("❌ CONFIGURATION BLOCKED!");
    Serial.printf("  Use: %cWLED,PORT,S1:115200   or   S2:115200\n", LocalFunctionIdentifier);
    Serial.println("=========================================\n");
    return;
  }

  // ---- Reject a port already claimed by PWM / Kyber / MP3 / HCR --------
  // Mirrors the HCR/MP3 guards so two subsystems can't silently share a UART.
  // Does NOT check WLED itself, so re-configuring WLED on its own port is fine.
  if (isSerialPortPWMOutput(serialPort) || isSerialPortUsedForPWMInput(serialPort) ||
      isSerialPortUsedForMP3(serialPort) || isSerialPortUsedForHCR(serialPort) ||
      (serialPort == 1 && (Kyber_Local || Maestro_Remote)) ||
      (serialPort == 2 && Kyber_Local)) {
    Serial.printf("[WLED] S%d already in use by PWM/Kyber/MP3/HCR - config blocked\n", serialPort);
    return;
  }

  wledReservePort(serialPort, baudRate);
}

// ==================== Status / List =====================================

void printWLEDSettings() {
  Serial.println("---- WLED Configuration ----");
  if (!wledConfig.configured) {
    Serial.println("  Not configured.  Use ?WLED,PORT,S<port>:115200");
    return;
  }
  Serial.printf("  Port:  S%d\n", wledConfig.serialPort);
  Serial.printf("  Baud:  %lu\n", (unsigned long)wledConfig.baudRate);
  Serial.printf("  Link:  %s\n", _wledPort ? "active" : "inactive");
}

void printWLEDStatus() {
  if (!wledConfig.configured || !_wledPort) {
    Serial.println("[WLED:cfg=0]");
    return;
  }
  Serial.printf("[WLED:cfg=1,port=%d,baud=%lu]\n",
                wledConfig.serialPort, (unsigned long)wledConfig.baudRate);
}

void printWLEDBackup(String &chainedConfig, String &chainedConfigDefault,
                     char delimiter, bool printToSerial,
                     const String &defSep, const String &defFunc) {
  if (!wledConfig.configured) return;

  String suffix = "WLED,PORT,S" + String(wledConfig.serialPort) +
                  ":" + String(wledConfig.baudRate);
  String cmd = String(LocalFunctionIdentifier) + suffix;
  if (printToSerial) Serial.println(cmd);
  chainedConfig        += String(delimiter) + cmd;
  chainedConfigDefault += defSep + defFunc + suffix;
}

// ==================== NVS storage =======================================

void saveWLEDSettings() {
  preferences.begin("wled_cfg", false);
  preferences.putUChar("port", wledConfig.serialPort);
  preferences.putUInt ("baud", wledConfig.baudRate);
  preferences.putBool ("en",   wledConfig.configured);
  preferences.end();
}

void loadWLEDSettings() {
  preferences.begin("wled_cfg", true);
  wledConfig.serialPort = preferences.getUChar("port", 0);
  wledConfig.baudRate   = preferences.getUInt ("baud", 115200);
  wledConfig.configured = preferences.getBool ("en",   false);
  preferences.end();

  if (wledConfig.configured) {
    Serial.printf("[WLED] Loaded: S%d at %lu baud\n",
                  wledConfig.serialPort, (unsigned long)wledConfig.baudRate);
  }
}
