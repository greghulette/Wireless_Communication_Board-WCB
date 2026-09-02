#include "WCB_RemoteTerm.h"  // Must be first — redirects Serial → WCBDebugSerial
#include "WCB_DFP.h"
#include "WCB_Storage.h"
#include <WcbCmd.h>          // shared ;D translator (DfPlayerCodec) — the same lib NaviCore runs

// ---- Externs provided by WCB.ino / WCB_Storage.cpp ---------------------
extern int          WCB_Number;
extern int          Default_WCB_Quantity;   // for validating a ?DFP,REMOTE,W<n> host
extern bool         debugEnabled;
extern char         LocalFunctionIdentifier;
extern char         commandDelimiter;
extern unsigned long baudRates[5];
extern bool         serialBroadcastEnabled[5];
extern bool         blockBroadcastFrom[5];
extern Preferences  preferences;

extern Stream      &getSerialStream(int port);
extern void         updateBaudRate(int port, int baud);
extern void         saveBroadcastSettingsToPreferences();
extern void         saveBroadcastBlockSettings();
extern void         recallCommandSlot(const String &key, int sourceID);
extern bool         isSerialPortUsedForHCR(int port);   // WCB_HCR.cpp  — port-conflict guard
extern bool         isSerialPortUsedForWLED(int port);  // WCB_WLED.cpp — port-conflict guard
extern bool         isSerialPortUsedForMP3(int port);   // WCB_MP3.cpp  — port-conflict guard

// The DFPlayer's only supported line rate. Not configurable — the module has no
// other mode, and letting a user set 38400 here would just produce silence.
static const uint32_t DFP_BAUD     = 9600;
static const uint8_t  DFP_VOL_CEIL = 30;    // 0 = silent .. 30 = loudest

// ---- Module globals -----------------------------------------------------
DFPConfig dfpConfig = {};
uint8_t   dfpVolume = 20;   // current tracked volume shadow

// ── Shared translator (WcbCmd) — the SAME DfPlayerCodec NaviCore compiles ──────
// Turns the ;D verbs into the DFPlayer's native 10-byte frames. Routing, the ?DFP
// config/NVS, and the "not configured" guard stay here in the firmware.
static DfPlayerCodec dfpCodec;

// Wire the codec's host hooks ONCE (plain function pointers; safe to re-assign).
static void wireDfpCallbacks() {
  dfpCodec.onVolumeChanged = [](uint8_t v){          // VOL / VOLUP / VOLDN moved the shadow
    dfpVolume = v;                                   // mirror it — backup/?DFP read dfpVolume
    saveDFPSettings();
    Serial.printf("[DFP] Volume → %d\n", v);
  };
  dfpCodec.onFinished = [](const char* key){ recallCommandSlot(String(key), 0); };                       // ONFIN
  dfpCodec.onError    = [](){ if (strlen(dfpConfig.onErrCmd) > 0) recallCommandSlot(String(dfpConfig.onErrCmd), 0); };
}

// Bind the codec to the resolved DFPlayer port and push the volume shadow into it
// (no emit). Call at boot AND after every ?DFP reconfigure/clear — otherwise the
// codec keeps writing the OLD port after a port change, or plays a STALE volume
// after ?DFP,…:V<new>. (Same trap the MP3 module documents.)
static void dfpBindCodec() {
  if (dfpConfig.configured && dfpConfig.serialPort > 0)
    dfpCodec.begin(getSerialStream(dfpConfig.serialPort), &Serial);
  dfpCodec.setVolume(dfpVolume);
}

// ==================== Port-conflict Query ================================

bool isSerialPortUsedForDFP(int port) {
  return dfpConfig.configured && (dfpConfig.serialPort == (uint8_t)port);
}

// ==================== Command Dispatcher =================================

void processDFPCommand(const String &message) {
  // `message` starts with 'D' (the CommandCharacter prefix is already stripped).
  // Strip leading 'D' and optional following comma.
  String rest = message.substring(1);
  rest.trim();
  if (rest.startsWith(",")) rest = rest.substring(1);

  if (!dfpConfig.configured) {
    Serial.printf("[DFP] Not configured — use %cDFP,S<port>[:%lu][:V<vol>]\n",
                  LocalFunctionIdentifier, (unsigned long)DFP_BAUD);
    return;
  }

  // handle() stores the ONFIN key internally (rejecting an out-of-range PLAY
  // BEFORE it touches pending, so a bad command can't clobber an in-flight
  // track's callback) and returns false only on an unknown/out-of-range verb.
  if (!dfpCodec.handle(rest.c_str())) {
    Serial.printf("[DFP] Unknown or invalid command: %s\n", rest.c_str());
    Serial.println("  Valid: PLAY,n  FOLDER,f,t  MP3FOLDER,n   (each +[,ONFIN,key])");
    Serial.println("         STOP NEXT PREV PAUSE RESUME RANDOM RESET STATUS");
    Serial.println("         VOL,n (0=silent..30=loudest)  VOLUP  VOLDN");
    Serial.println("         LOOP,n  LOOPALL,0|1  LOOPFOLDER,f  EQ,0-5  DEVICE,1-5");
  }
}

// ==================== Response Reader ====================================
// Non-blocking. Call once per loop() tick.
// The serial task is told to skip this port (isSerialPortUsedForDFP), so these
// bytes are exclusively consumed here.

void processDFPResponses() {
  if (!dfpConfig.configured || dfpConfig.serialPort == 0) return;
  dfpCodec.poll();   // fires onFinished (0x3D → ONFIN) + onError (0x40)
}

// ==================== Configuration (?DFP,...) ===========================

void clearDFPConfig() {
  uint8_t freedPort = dfpConfig.serialPort;
  // ?DFP,CLEAR removes the LOCAL host only; the auto-learned remote route is a
  // separate axis (?DFP,REMOTE,OFF / factory reset). Preserve it so a Wizard
  // full-push CLEAR can't wipe a route it never observed. (See clearMP3Config.)
  uint8_t savedRemote = dfpConfig.remoteWCB;

  memset(&dfpConfig, 0, sizeof(dfpConfig));
  dfpConfig.configured = false;
  dfpConfig.baudRate   = DFP_BAUD;
  dfpConfig.volume     = 20;
  dfpConfig.remoteWCB  = savedRemote;
  dfpVolume            = 20;

  saveDFPSettings();
  dfpBindCodec();   // resync the codec's volume shadow to the default (port released)
  Serial.println("[DFP] Local configuration cleared");
  if (dfpConfig.remoteWCB > 0)
    Serial.printf("  (still routing ;D to WCB%d — %cDFP,REMOTE,OFF to stop)\n",
                  dfpConfig.remoteWCB, LocalFunctionIdentifier);

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
    Serial.printf("  ✓ Reset S%d baud rate to 9600\n", freedPort);
    saveSerialLabelToPreferences(freedPort, "");
  }
}

void configureDFP(const String &args) {
  // ?DFP,S<port>[:<baud>][:V<vol>]  — configure on this WCB
  // ?DFP,LIST                       — show current configuration
  // ?DFP,CLEAR                      — remove configuration
  // ?DFP,REMOTE,W<n> | REMOTE,OFF   — route ;D to another board
  // ?DFP,ONERR,<key> | ONERR,CLEAR  — error callback (stored-cmd key)

  String a = args;
  a.trim();
  String aUpper = a;
  aUpper.toUpperCase();

  if (aUpper == "LIST")  { printDFPSettings(); return; }
  if (aUpper == "CLEAR") { clearDFPConfig();   return; }

  // ---- REMOTE,W<n> | REMOTE,OFF — route ;D to the DFPlayer on another board ----
  if (aUpper.startsWith("REMOTE")) {
    String v = (a.indexOf(',') >= 0) ? a.substring(a.indexOf(',') + 1) : "";
    v.trim(); String vU = v; vU.toUpperCase();
    if (vU == "" || vU == "OFF" || vU == "0") {
      dfpConfig.remoteWCB = 0;
      saveDFPSettings();
      Serial.println("[DFP] Remote routing cleared");
      return;
    }
    int wIdx = vU.indexOf('W');
    int host = (wIdx >= 0) ? v.substring(wIdx + 1).toInt() : v.toInt();
    // Bound matches auto-learn + routing (1..MAX_WCB_COUNT, incl. learned peers
    // above the WCBQ floor) so a persisted route always round-trips through a backup.
    if (host < 1 || host > MAX_WCB_COUNT || host == WCB_Number) {
      Serial.printf("[DFP] Invalid host. Use %cDFP,REMOTE,W<n> (1-%d, not this board)\n",
                    LocalFunctionIdentifier, MAX_WCB_COUNT);
      return;
    }
    if (dfpConfig.configured) clearDFPConfig();   // was a local host — release the port
    dfpConfig.remoteWCB = (uint8_t)host;
    saveDFPSettings();
    Serial.printf("[DFP] Routing ;D to WCB%d\n", host);
    return;
  }

  // ---- ONERR,... --------------------------------------------------
  if (aUpper.startsWith("ONERR,")) {
    String key = args.substring(6);
    key.trim();
    if (key.equalsIgnoreCase("CLEAR")) {
      memset(dfpConfig.onErrCmd, 0, sizeof(dfpConfig.onErrCmd));
      saveDFPSettings();
      Serial.println("[DFP] Error callback cleared");
    } else if (key.length() == 0 || key.length() > 23) {
      Serial.println("[DFP] ONERR: key must be 1-23 characters");
    } else {
      strncpy(dfpConfig.onErrCmd, key.c_str(), sizeof(dfpConfig.onErrCmd) - 1);
      dfpConfig.onErrCmd[sizeof(dfpConfig.onErrCmd) - 1] = '\0';
      saveDFPSettings();
      Serial.printf("[DFP] Error callback → ;C%s\n", dfpConfig.onErrCmd);
    }
    return;
  }

  // ---- S<port>[:<baud>][:V<vol>]  (main configuration) ------------
  // Baud is optional because the DFPlayer only has one. It is still accepted in
  // the same S:baud:V shape as ?MP3 so a backup line reads the same and the
  // Wizard's generic parser needs no special case.
  if (!aUpper.startsWith("S")) {
    Serial.printf("[DFP] Invalid format. Use: %cDFP,S<port>[:%lu][:V<vol>]\n",
                  LocalFunctionIdentifier, (unsigned long)DFP_BAUD);
    Serial.printf("  Example: %cDFP,S2:9600:V20   (or just %cDFP,S2)\n",
                  LocalFunctionIdentifier, LocalFunctionIdentifier);
    return;
  }

  int firstColon  = a.indexOf(':');
  int secondColon = (firstColon != -1) ? a.indexOf(':', firstColon + 1) : -1;

  int serialPort = (firstColon != -1) ? a.substring(1, firstColon).toInt()
                                      : a.substring(1).toInt();
  int baudRate   = (int)DFP_BAUD;
  int volume     = 20;

  // Middle field: a baud, OR the volume when the baud was skipped ("S2:V20").
  if (firstColon != -1) {
    String mid = (secondColon != -1) ? a.substring(firstColon + 1, secondColon)
                                     : a.substring(firstColon + 1);
    mid.trim();
    if (mid.startsWith("V") || mid.startsWith("v")) volume   = mid.substring(1).toInt();
    else if (mid.length())                          baudRate = mid.toInt();
  }
  if (secondColon != -1) {
    String volStr = a.substring(secondColon + 1);
    volStr.trim();
    if (!volStr.startsWith("V") && !volStr.startsWith("v")) {
      Serial.printf("[DFP] Volume must be given as :V<0-%d> (e.g. :V20)\n", DFP_VOL_CEIL);
      return;
    }
    volume = volStr.substring(1).toInt();
  }

  // ---- Validate ---------------------------------------------------
  if (serialPort < 1 || serialPort > 5) {
    Serial.println("[DFP] Invalid serial port. Must be S1-S5");
    return;
  }

  if ((uint32_t)baudRate != DFP_BAUD) {
    Serial.printf("[DFP] A DFPlayer Mini only runs at %lu baud (module-fixed)\n",
                  (unsigned long)DFP_BAUD);
    return;
  }

  // 9600 is safe on the SoftwareSerial ports (S3-S5), so unlike ?MP3 there is no
  // software-serial speed block here — the only supported baud is already the
  // slow one.

  if (volume < 0 || volume > DFP_VOL_CEIL) {
    Serial.printf("[DFP] Volume must be 0-%d (0=silent, %d=loudest)\n",
                  DFP_VOL_CEIL, DFP_VOL_CEIL);
    return;
  }

  // ---- Reject a port already claimed by PWM / Kyber / HCR / MP3 / WLED ----
  // Does NOT check DFP itself, so re-configuring the DFPlayer on its own port
  // (e.g. just changing volume) is still allowed.
  if (isSerialPortPWMOutput(serialPort) || isSerialPortUsedForPWMInput(serialPort) ||
      isSerialPortUsedForHCR(serialPort) || isSerialPortUsedForWLED(serialPort) ||
      isSerialPortUsedForMP3(serialPort) ||
      (serialPort == 1 && (Kyber_Local || Maestro_Remote)) ||
      (serialPort == 2 && Kyber_Local)) {
    Serial.printf("[DFP] S%d already in use by PWM/Kyber/HCR/MP3/WLED - config blocked\n", serialPort);
    return;
  }

  // ---- Release old port if moving to a different one --------------
  if (dfpConfig.configured && dfpConfig.serialPort > 0 &&
      dfpConfig.serialPort != (uint8_t)serialPort) {
    uint8_t oldPort = dfpConfig.serialPort;
    if (!serialBroadcastEnabled[oldPort - 1]) {
      serialBroadcastEnabled[oldPort - 1] = true;
      saveBroadcastSettingsToPreferences();
      Serial.printf("  ✓ Re-enabled broadcast output on S%d (old DFPlayer port)\n", oldPort);
    }
    if (blockBroadcastFrom[oldPort - 1]) {
      blockBroadcastFrom[oldPort - 1] = false;
      saveBroadcastBlockSettings();
      Serial.printf("  ✓ Re-enabled broadcast input on S%d (old DFPlayer port)\n", oldPort);
    }
    saveSerialLabelToPreferences(oldPort, "");
    Serial.printf("  ✓ Released S%d (old DFPlayer port)\n", oldPort);
  }

  // ---- Apply ------------------------------------------------------
  if ((uint32_t)baudRate != baudRates[serialPort - 1]) {
    updateBaudRate(serialPort, baudRate);
  }

  if (serialBroadcastEnabled[serialPort - 1]) {
    serialBroadcastEnabled[serialPort - 1] = false;
    saveBroadcastSettingsToPreferences();
    Serial.printf("  ⚠️  Disabled broadcast output on S%d (DFPlayer port)\n", serialPort);
  }

  if (!blockBroadcastFrom[serialPort - 1]) {
    blockBroadcastFrom[serialPort - 1] = true;
    saveBroadcastBlockSettings();
    Serial.printf("  ⚠️  Disabled broadcast input on S%d (DFPlayer port)\n", serialPort);
  }

  dfpConfig.serialPort = (uint8_t)serialPort;
  dfpConfig.baudRate   = (uint32_t)baudRate;
  dfpConfig.volume     = (uint8_t)volume;
  dfpConfig.configured = true;
  dfpConfig.remoteWCB  = 0;   // we host it now — not a client of another board
  dfpVolume            = (uint8_t)volume;

  saveDFPSettings();
  saveSerialLabelToPreferences(serialPort, "DFPlayer");
  dfpBindCodec();   // rebind codec to the new port + push the new volume (no emit)

  Serial.printf("[DFP] Configured: S%d at %d baud  default volume=%d\n",
                serialPort, baudRate, volume);
  Serial.println("  Note: a DFPlayer needs ~1.5-3s after power-on before it accepts commands.");
  if (strlen(dfpConfig.onErrCmd) > 0)
    Serial.printf("  On error → ;C%s\n", dfpConfig.onErrCmd);
}

void printDFPSettings() {
  Serial.println("\n--- DFPlayer Mini Configuration ---");
  if (!dfpConfig.configured) {
    if (dfpConfig.remoteWCB > 0) {
      Serial.printf("  Routes ;D to WCB%d (remote host)\n", dfpConfig.remoteWCB);
    } else {
      Serial.println("  Not configured.");
      Serial.printf("  Use: %cDFP,S<port>[:%lu][:V<vol>]  (e.g. %cDFP,S2:9600:V20)\n",
                    LocalFunctionIdentifier, (unsigned long)DFP_BAUD, LocalFunctionIdentifier);
    }
    Serial.println("-----------------------------------");
    return;
  }
  Serial.printf("  Port          : S%d\n",  dfpConfig.serialPort);
  Serial.printf("  Baud          : %lu  (module-fixed)\n", (unsigned long)dfpConfig.baudRate);
  Serial.printf("  Default Volume: %d  (0=silent, %d=loudest)\n", dfpConfig.volume, DFP_VOL_CEIL);
  Serial.printf("  Current Volume: %d\n",   dfpVolume);
  Serial.printf("  On Error      : %s\n",
                strlen(dfpConfig.onErrCmd) ? dfpConfig.onErrCmd : "(none)");
  Serial.println("-----------------------------------");
}

void printDFPBackup(String &chainedConfig, String &chainedConfigDefault,
                    char delimiter, bool printToSerial,
                    const String &defSep, const String &defFunc) {
  // Client board (no local DFPlayer, routes ;D to a remote host): persist the route.
  if (!dfpConfig.configured) {
    if (dfpConfig.remoteWCB > 0) {
      String suffix = "DFP,REMOTE,W" + String(dfpConfig.remoteWCB);
      String cmd = String(LocalFunctionIdentifier) + suffix;
      if (printToSerial) Serial.println(cmd);
      chainedConfig        += String(delimiter) + cmd;
      chainedConfigDefault += defSep + defFunc + suffix;
    }
    return;
  }

  // Main config line — includes volume
  String cmdSuffix = "DFP,S" + String(dfpConfig.serialPort) +
                     ":" + String(dfpConfig.baudRate) +
                     ":V" + String(dfpConfig.volume);
  String cmd = String(LocalFunctionIdentifier) + cmdSuffix;
  if (printToSerial) Serial.println(cmd);
  chainedConfig        += String(delimiter) + cmd;
  chainedConfigDefault += defSep + defFunc + cmdSuffix;

  // Error callback
  if (strlen(dfpConfig.onErrCmd) > 0) {
    cmdSuffix = "DFP,ONERR," + String(dfpConfig.onErrCmd);
    cmd = String(LocalFunctionIdentifier) + cmdSuffix;
    if (printToSerial) Serial.println(cmd);
    chainedConfig        += String(delimiter) + cmd;
    chainedConfigDefault += defSep + defFunc + cmdSuffix;
  }
}

// ==================== NVS Storage ========================================

void saveDFPSettings() {
  preferences.begin("dfp_cfg", false);
  preferences.putUChar ("port",   dfpConfig.serialPort);
  preferences.putUInt  ("baud",   dfpConfig.baudRate);
  preferences.putBool  ("en",     dfpConfig.configured);
  preferences.putUChar ("vol",    dfpVolume);           // current tracked volume
  preferences.putUChar ("defvol", dfpConfig.volume);    // configured default
  preferences.putString("onErr",  dfpConfig.onErrCmd);
  preferences.putUChar ("rwcb",   dfpConfig.remoteWCB);
  preferences.end();
}

void loadDFPSettings() {
  preferences.begin("dfp_cfg", true);
  dfpConfig.serialPort = preferences.getUChar ("port",   0);
  dfpConfig.baudRate   = preferences.getUInt  ("baud",   DFP_BAUD);
  dfpConfig.configured = preferences.getBool  ("en",     false);
  dfpConfig.volume     = preferences.getUChar ("defvol", 20);
  dfpVolume            = preferences.getUChar ("vol",    20);
  String err           = preferences.getString("onErr",  "");
  dfpConfig.remoteWCB  = preferences.getUChar ("rwcb",   0);
  preferences.end();

  strncpy(dfpConfig.onErrCmd, err.c_str(), sizeof(dfpConfig.onErrCmd) - 1);
  dfpConfig.onErrCmd[sizeof(dfpConfig.onErrCmd) - 1] = '\0';

  if (dfpConfig.configured) {
    Serial.printf("[DFP] Loaded: S%d at %lu baud  default vol=%d  current vol=%d\n",
                  dfpConfig.serialPort,
                  (unsigned long)dfpConfig.baudRate,
                  dfpConfig.volume,
                  dfpVolume);
  } else if (dfpConfig.remoteWCB > 0) {
    Serial.printf("[DFP] Loaded: routes ;D to WCB%d\n", dfpConfig.remoteWCB);
  }

  wireDfpCallbacks();   // one-time host hooks (volume mirror, ONFIN, onError)
  dfpBindCodec();       // bind the codec to the resolved port + sync the volume shadow
}

// Auto-learn the DFPlayer host from a WDP advert. First-host-wins + persisted:
// never override a local host or an already-stored host. Returns true if newly stored.
bool dfpAutoAddRemote(uint8_t hostWCB) {
  if (hostWCB == 0 || hostWCB == WCB_Number) return false;
  if (dfpConfig.configured)                  return false;   // we host it locally
  if (dfpConfig.remoteWCB != 0)              return false;   // already have a host
  dfpConfig.remoteWCB = hostWCB;
  saveDFPSettings();
  Serial.printf("[WDP] DFPlayer host learned — routing ;D to WCB%d\n", hostWCB);
  return true;
}
