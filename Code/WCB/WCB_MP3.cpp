#include "WCB_RemoteTerm.h"  // Must be first — redirects Serial → WCBDebugSerial
#include "WCB_MP3.h"
#include "WCB_Storage.h"
#include <WcbCmd.h>          // shared ;A translator (Mp3Codec) — byte-identical to inline; same lib NaviCore runs

// ---- Externs provided by WCB.ino / WCB_Storage.cpp ---------------------
extern int          WCB_Number;
extern int          Default_WCB_Quantity;   // for validating a ?MP3,REMOTE,W<n> host
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
extern bool         isSerialPortUsedForHCR(int port);  // WCB_HCR.cpp — for the port-conflict guard
extern bool         isSerialPortUsedForWLED(int port); // WCB_WLED.cpp — for the port-conflict guard
// PWM/MP3 predicates + Kyber_Local/Maestro_Remote come from WCB_Storage.h.

// ---- Module globals -----------------------------------------------------
MP3Config mp3Config = {};
uint8_t   mp3Volume = 20;   // current tracked volume shadow

// Per-play callback — runtime only, not persisted.
// NOTE: with the shared Mp3Codec (below) the codec now OWNS the pending-ONFIN key
// internally; this String is retained only for printMP3Settings' "Pending ONFIN"
// display (now always empty) and is otherwise vestigial. The codec fires onFinished
// (→ recallCommandSlot) directly from poll(), matching the old behavior.
static String _mp3PendingCallback = "";

// ── Shared translator (WcbCmd) — the SAME Mp3Codec NaviCore compiles ────────────
// Turns the ;A verbs into the MP3 Trigger's native serial bytes, byte-identical to
// the old inline sendMP3Raw path. Routing, the ?MP3 config/NVS, and the
// "not configured" guard stay here in the firmware.
static Mp3Codec mp3Codec;

// Wire the codec's host hooks ONCE (plain function pointers; safe to re-assign).
static void wireMp3Callbacks() {
  mp3Codec.onVolumeChanged = [](uint8_t v){          // VOL / VOLUP / VOLDN moved the shadow
    mp3Volume = v;                                   // mirror it — backup/?MP3 read mp3Volume
    saveMP3Settings();
    Serial.printf("[MP3] Volume → %d\n", v);         // user-facing feedback (was "up/down → n")
  };
  mp3Codec.onFinished = [](const char* key){ recallCommandSlot(String(key), 0); };                       // ONFIN
  mp3Codec.onError    = [](){ if (strlen(mp3Config.onErrCmd) > 0) recallCommandSlot(String(mp3Config.onErrCmd), 0); };
}

// Bind the codec to the resolved MP3 port and push the volume shadow into it (no
// emit). Call at boot AND after every ?MP3 reconfigure/clear — otherwise the codec
// would play a STALE volume after ?MP3,…:V<new> or write the OLD port after a port
// change (the byte/target divergences the guide's load-only snippet missed).
static void mp3BindCodec() {
  if (mp3Config.configured && mp3Config.serialPort > 0)
    mp3Codec.begin(getSerialStream(mp3Config.serialPort), &Serial);
  mp3Codec.setVolume(mp3Volume);
}

// (RX accumulation state moved into the shared Mp3Codec — see processMP3Responses.)

// ==================== Port-conflict Query ================================

bool isSerialPortUsedForMP3(int port) {
  return mp3Config.configured && (mp3Config.serialPort == (uint8_t)port);
}

// ==================== Low-level Send =====================================

void sendMP3Raw(uint8_t byte1, int byte2) {
  if (!mp3Config.configured || mp3Config.serialPort == 0) {
    Serial.printf("[MP3] Not configured — use %cMP3,S<port>:<baud>:V<vol>\n",
                  LocalFunctionIdentifier);
    return;
  }
  Stream &s = getSerialStream(mp3Config.serialPort);
  s.write(byte1);
  // byte2 is an int with sentinel -1 = "no second byte". A plain int8_t here (and
  // (int8_t) casts at the call sites) made tracks 128-255 negative, so >=0 failed
  // and the track byte was dropped. int preserves the full 0-255 range.
  if (byte2 >= 0) s.write((uint8_t)byte2);
  // No flush() — write() queues into the UART TX buffer and it drains async
  // (mirror WCB_Maestro). Flushing blocked loop() ~1-4ms per audio command for
  // no benefit (the MP3 Trigger doesn't require the caller to wait).
}

// ==================== Audio Command Dispatcher ===========================

void processMP3AudioCommand(const String &message) {
  // `message` starts with 'A' (the CommandCharacter prefix is already stripped).
  // Strip leading 'A' and optional following comma.
  String rest = message.substring(1);
  rest.trim();
  if (rest.startsWith(",")) rest = rest.substring(1);

  String restUpper = rest;
  restUpper.toUpperCase();

  if (!mp3Config.configured) {
    Serial.printf("[MP3] Not configured — use %cMP3,S<port>:<baud>:V<vol>\n",
                  LocalFunctionIdentifier);
    return;
  }

  // ── Wire bytes + volume shadow + ONFIN pending → the shared Mp3Codec ────────────
  // Byte-identical to the old inline sendMP3Raw path: 'v'+shadow then 't'/'p', the
  // VOLUP/VOLDN ±5 step math with the 0/64 clamps, COUNT/VER — the SAME codec
  // NaviCore runs. handle() stores the ONFIN key internally (rejecting an
  // out-of-range PLAY BEFORE it touches pending, exactly like the old code) and
  // returns false only on an unknown/out-of-range verb. `rest` is already
  // 'A'-stripped + trimmed, so there's no trailing-whitespace mismatch.
  //
  // Console note: the per-verb debug logs (Play/Next/…) and the specific
  // range-reject strings are consolidated into the one help line below; volume
  // feedback now comes from onVolumeChanged ("[MP3] Volume → n"). Wire bytes and
  // the RX-side console are unchanged.
  if (!mp3Codec.handle(rest.c_str())) {
    Serial.printf("[MP3] Unknown or invalid audio command: %s\n", rest.c_str());
    Serial.println("  Valid: PLAY,n[,ONFIN,key]  PLAY,n[,key]  PLAYFS,n[,ONFIN,key]");
    Serial.println("         STOP  NEXT  PREV  VOL,n (0-64)  VOLUP  VOLDN  COUNT  VER");
  }
}

// ==================== Response Reader ====================================
// Non-blocking. Call once per loop() tick.
// The serial task is told to skip this port (isSerialPortUsedForMP3),
// so these bytes are exclusively consumed here.

void processMP3Responses() {
  if (!mp3Config.configured || mp3Config.serialPort == 0) return;
  // RX pump moved into the shared codec — identical X/x/E/=/M handling and identical
  // "[MP3] Track finished / cancelled / Error / Status" console output (verified
  // word-for-word), and it fires onFinished (ONFIN → recallCommandSlot) + onError.
  mp3Codec.poll();
}

// ==================== Configuration (?MP3,...) ===========================

void clearMP3Config() {
  uint8_t freedPort = mp3Config.serialPort;
  // ?MP3,CLEAR removes the LOCAL host only; the auto-learned remote route is a
  // separate axis (?MP3,REMOTE,OFF / factory reset). Preserve it so a Wizard
  // full-push CLEAR can't wipe a route it never observed. (See clearHCRConfig.)
  uint8_t savedRemote = mp3Config.remoteWCB;

  memset(&mp3Config, 0, sizeof(mp3Config));
  mp3Config.configured = false;
  mp3Config.baudRate   = 9600;
  mp3Config.volume     = 20;
  mp3Config.remoteWCB  = savedRemote;
  mp3Volume            = 20;
  _mp3PendingCallback  = "";

  saveMP3Settings();
  mp3BindCodec();   // resync the codec's volume shadow to the default (port released)
  Serial.println("[MP3] Local configuration cleared");
  if (mp3Config.remoteWCB > 0)
    Serial.printf("  (still routing ;A to WCB%d — %cMP3,REMOTE,OFF to stop)\n",
                  mp3Config.remoteWCB, LocalFunctionIdentifier);

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

void configureMP3(const String &args) {
  // ?MP3,S<port>:<baud>:V<vol>   — configure on this WCB (volume required)
  // ?MP3,LIST                    — show current configuration
  // ?MP3,CLEAR                   — remove configuration
  // ?MP3,ONERR,<key>             — set error callback (stored-cmd key)
  // ?MP3,ONERR,CLEAR             — remove error callback

  String a = args;
  a.trim();
  String aUpper = a;
  aUpper.toUpperCase();

  // ---- LIST -------------------------------------------------------
  if (aUpper == "LIST") {
    printMP3Settings();
    return;
  }

  // ---- CLEAR ------------------------------------------------------
  if (aUpper == "CLEAR") {
    clearMP3Config();
    return;
  }

  // ---- REMOTE,W<n> | REMOTE,OFF — route ;A to the MP3 on another board ----
  // Persisted routing (auto-learned from WDP, set by hand, or restored). A board
  // that HOSTS an MP3 Trigger locally never needs this.
  if (aUpper.startsWith("REMOTE")) {
    String v = (a.indexOf(',') >= 0) ? a.substring(a.indexOf(',') + 1) : "";
    v.trim(); String vU = v; vU.toUpperCase();
    if (vU == "" || vU == "OFF" || vU == "0") {
      mp3Config.remoteWCB = 0;
      saveMP3Settings();
      Serial.println("[MP3] Remote routing cleared");
      return;
    }
    int wIdx = vU.indexOf('W');
    int host = (wIdx >= 0) ? v.substring(wIdx + 1).toInt() : v.toInt();
    // Bound matches auto-learn + routing (1..MAX_WCB_COUNT, incl. learned peers
    // above the WCBQ floor) so a persisted route always round-trips through a backup.
    if (host < 1 || host > MAX_WCB_COUNT || host == WCB_Number) {
      Serial.printf("[MP3] Invalid host. Use %cMP3,REMOTE,W<n> (1-%d, not this board)\n",
                    LocalFunctionIdentifier, MAX_WCB_COUNT);
      return;
    }
    if (mp3Config.configured) clearMP3Config();   // was a local host — release the port
    mp3Config.remoteWCB = (uint8_t)host;
    saveMP3Settings();
    Serial.printf("[MP3] Routing ;A to WCB%d\n", host);
    return;
  }

  // ---- ONERR,... --------------------------------------------------
  if (aUpper.startsWith("ONERR,")) {
    String key = args.substring(6);
    key.trim();
    if (key.equalsIgnoreCase("CLEAR")) {
      memset(mp3Config.onErrCmd, 0, sizeof(mp3Config.onErrCmd));
      saveMP3Settings();
      Serial.println("[MP3] Error callback cleared");
    } else if (key.length() == 0 || key.length() > 23) {
      Serial.println("[MP3] ONERR: key must be 1-23 characters");
    } else {
      strncpy(mp3Config.onErrCmd, key.c_str(), sizeof(mp3Config.onErrCmd) - 1);
      mp3Config.onErrCmd[sizeof(mp3Config.onErrCmd) - 1] = '\0';
      saveMP3Settings();
      Serial.printf("[MP3] Error callback → ;C%s\n", mp3Config.onErrCmd);
    }
    return;
  }

  // ---- S<port>:<baud>:V<vol>  (main configuration) ----------------
  if (!aUpper.startsWith("S")) {
    Serial.printf("[MP3] Invalid format. Use: %cMP3,S<port>:<baud>:V<vol>\n",
                  LocalFunctionIdentifier);
    Serial.printf("  Example: %cMP3,S2:9600:V25\n", LocalFunctionIdentifier);
    return;
  }

  int firstColon  = a.indexOf(':');
  int secondColon = (firstColon != -1) ? a.indexOf(':', firstColon + 1) : -1;

  if (firstColon == -1 || secondColon == -1) {
    Serial.printf("[MP3] Incomplete config. Use: %cMP3,S<port>:<baud>:V<vol>\n",
                  LocalFunctionIdentifier);
    Serial.printf("  Example: %cMP3,S2:9600:V25\n", LocalFunctionIdentifier);
    return;
  }

  int    serialPort = a.substring(1, firstColon).toInt();
  int    baudRate   = a.substring(firstColon + 1, secondColon).toInt();
  String volStr     = a.substring(secondColon + 1);  // "V25"

  if (!volStr.startsWith("V") && !volStr.startsWith("v")) {
    Serial.println("[MP3] Volume required. Use :V<0-64> at the end (e.g. :V25)");
    return;
  }
  int volume = volStr.substring(1).toInt();

  // ---- Validate ---------------------------------------------------
  if (serialPort < 1 || serialPort > 5) {
    Serial.println("[MP3] Invalid serial port. Must be S1-S5");
    return;
  }

  if (baudRate != 9600 && baudRate != 38400) {
    Serial.println("[MP3] MP3 Trigger only supports 9600 or 38400 baud");
    return;
  }

  if (serialPort >= 3 && baudRate > 9600) {
    Serial.println("\n⚠️  =============== WARNING ===============");
    Serial.printf("S%d is SOFTWARE SERIAL\n", serialPort);
    Serial.println("Software serial is unreliable above 9600 baud");
    Serial.println("❌ CONFIGURATION BLOCKED!");
    Serial.printf("  Use hardware serial (S1 or S2): %cMP3,S1:%d:V%d\n",
                  LocalFunctionIdentifier, baudRate, volume);
    Serial.printf("  Or use 9600 baud:               %cMP3,S%d:9600:V%d\n",
                  LocalFunctionIdentifier, serialPort, volume);
    Serial.println("=========================================\n");
    return;
  }

  if (volume < 0 || volume > 64) {
    Serial.println("[MP3] Volume must be 0-64 (0=loudest, 64=inaudible)");
    return;
  }

  // ---- Reject a port already claimed by PWM / Kyber / HCR ----------
  // Mirrors canUsePWMOnPort (WCB_PWM.cpp) so two subsystems can't silently
  // share one UART. Does NOT check MP3 itself, so re-configuring MP3 on its
  // own port (e.g. just changing baud/volume) is still allowed.
  if (isSerialPortPWMOutput(serialPort) || isSerialPortUsedForPWMInput(serialPort) ||
      isSerialPortUsedForHCR(serialPort) || isSerialPortUsedForWLED(serialPort) ||
      (serialPort == 1 && (Kyber_Local || Maestro_Remote)) ||
      (serialPort == 2 && Kyber_Local)) {
    Serial.printf("[MP3] S%d already in use by PWM/Kyber/HCR/WLED - config blocked\n", serialPort);
    return;
  }

  // ---- Release old port if moving to a different one --------------
  if (mp3Config.configured && mp3Config.serialPort > 0 &&
      mp3Config.serialPort != (uint8_t)serialPort) {
    uint8_t oldPort = mp3Config.serialPort;
    if (!serialBroadcastEnabled[oldPort - 1]) {
      serialBroadcastEnabled[oldPort - 1] = true;
      saveBroadcastSettingsToPreferences();
      Serial.printf("  ✓ Re-enabled broadcast output on S%d (old MP3 port)\n", oldPort);
    }
    if (blockBroadcastFrom[oldPort - 1]) {
      blockBroadcastFrom[oldPort - 1] = false;
      saveBroadcastBlockSettings();
      Serial.printf("  ✓ Re-enabled broadcast input on S%d (old MP3 port)\n", oldPort);
    }
    saveSerialLabelToPreferences(oldPort, "");
    Serial.printf("  ✓ Released S%d (old MP3 port)\n", oldPort);
  }

  // ---- Apply ------------------------------------------------------
  if ((uint32_t)baudRate != baudRates[serialPort - 1]) {
    updateBaudRate(serialPort, baudRate);
  }

  if (serialBroadcastEnabled[serialPort - 1]) {
    serialBroadcastEnabled[serialPort - 1] = false;
    saveBroadcastSettingsToPreferences();
    Serial.printf("  ⚠️  Disabled broadcast output on S%d (MP3 Trigger port)\n", serialPort);
  }

  if (!blockBroadcastFrom[serialPort - 1]) {
    blockBroadcastFrom[serialPort - 1] = true;
    saveBroadcastBlockSettings();
    Serial.printf("  ⚠️  Disabled broadcast input on S%d (MP3 Trigger port)\n", serialPort);
  }

  mp3Config.serialPort = (uint8_t)serialPort;
  mp3Config.baudRate   = (uint32_t)baudRate;
  mp3Config.volume     = (uint8_t)volume;
  mp3Config.configured = true;
  mp3Config.remoteWCB  = 0;   // we host it now — not a client of another board
  mp3Volume            = (uint8_t)volume;

  saveMP3Settings();
  saveSerialLabelToPreferences(serialPort, "MP3 Trigger");
  mp3BindCodec();   // rebind codec to the new port + push the new volume (no emit)

  Serial.printf("[MP3] Configured: S%d at %d baud  default volume=%d\n",
                serialPort, baudRate, volume);
  if (strlen(mp3Config.onErrCmd) > 0)
    Serial.printf("  On error → ;C%s\n", mp3Config.onErrCmd);
}

void printMP3Settings() {
  Serial.println("\n--- MP3 Trigger Configuration ---");
  if (!mp3Config.configured) {
    if (mp3Config.remoteWCB > 0) {
      Serial.printf("  Routes ;A to WCB%d (remote host)\n", mp3Config.remoteWCB);
    } else {
      Serial.println("  Not configured.");
      Serial.printf("  Use: %cMP3,S<port>:<baud>:V<vol>  (e.g. %cMP3,S2:9600:V25)\n",
                    LocalFunctionIdentifier, LocalFunctionIdentifier);
    }
    Serial.println("---------------------------------");
    return;
  }
  Serial.printf("  Port          : S%d\n",  mp3Config.serialPort);
  Serial.printf("  Baud          : %lu\n",  (unsigned long)mp3Config.baudRate);
  Serial.printf("  Default Volume: %d  (0=loudest, 64=inaudible ceiling)\n", mp3Config.volume);
  Serial.printf("  Current Volume: %d\n",   mp3Volume);
  Serial.printf("  On Error      : %s\n",
                strlen(mp3Config.onErrCmd) ? mp3Config.onErrCmd : "(none)");
  if (_mp3PendingCallback.length() > 0)
    Serial.printf("  Pending ONFIN : %s\n", _mp3PendingCallback.c_str());
  Serial.println("---------------------------------");
}

void printMP3Backup(String &chainedConfig, String &chainedConfigDefault,
                    char delimiter, bool printToSerial,
                    const String &defSep, const String &defFunc) {
  // Client board (no local MP3, routes ;A to a remote host): persist the route.
  if (!mp3Config.configured) {
    if (mp3Config.remoteWCB > 0) {
      String suffix = "MP3,REMOTE,W" + String(mp3Config.remoteWCB);
      String cmd = String(LocalFunctionIdentifier) + suffix;
      if (printToSerial) Serial.println(cmd);
      chainedConfig        += String(delimiter) + cmd;
      chainedConfigDefault += defSep + defFunc + suffix;
    }
    return;
  }

  // Main config line — includes volume
  String cmdSuffix = "MP3,S" + String(mp3Config.serialPort) +
                     ":" + String(mp3Config.baudRate) +
                     ":V" + String(mp3Config.volume);
  String cmd = String(LocalFunctionIdentifier) + cmdSuffix;
  if (printToSerial) Serial.println(cmd);
  chainedConfig        += String(delimiter) + cmd;
  chainedConfigDefault += defSep + defFunc + cmdSuffix;

  // Error callback
  if (strlen(mp3Config.onErrCmd) > 0) {
    cmdSuffix = "MP3,ONERR," + String(mp3Config.onErrCmd);
    cmd = String(LocalFunctionIdentifier) + cmdSuffix;
    if (printToSerial) Serial.println(cmd);
    chainedConfig        += String(delimiter) + cmd;
    chainedConfigDefault += defSep + defFunc + cmdSuffix;
  }
}

// ==================== NVS Storage ========================================

void saveMP3Settings() {
  preferences.begin("mp3_cfg", false);
  preferences.putUChar ("port",   mp3Config.serialPort);
  preferences.putUInt  ("baud",   mp3Config.baudRate);
  preferences.putBool  ("en",     mp3Config.configured);
  preferences.putUChar ("vol",    mp3Volume);           // save current tracked volume
  preferences.putUChar ("defvol", mp3Config.volume);    // save configured default
  preferences.putString("onErr",  mp3Config.onErrCmd);
  preferences.putUChar ("rwcb",   mp3Config.remoteWCB);
  preferences.end();
}

void loadMP3Settings() {
  preferences.begin("mp3_cfg", true);
  mp3Config.serialPort = preferences.getUChar ("port",   0);
  mp3Config.baudRate   = preferences.getUInt  ("baud",   9600);
  mp3Config.configured = preferences.getBool  ("en",     false);
  mp3Config.volume     = preferences.getUChar ("defvol", 20);
  mp3Volume            = preferences.getUChar ("vol",    20);
  String err           = preferences.getString("onErr",  "");
  mp3Config.remoteWCB  = preferences.getUChar ("rwcb",   0);
  preferences.end();

  strncpy(mp3Config.onErrCmd, err.c_str(), sizeof(mp3Config.onErrCmd) - 1);
  mp3Config.onErrCmd[sizeof(mp3Config.onErrCmd) - 1] = '\0';

  if (mp3Config.configured) {
    Serial.printf("[MP3] Loaded: S%d at %lu baud  default vol=%d  current vol=%d\n",
                  mp3Config.serialPort,
                  (unsigned long)mp3Config.baudRate,
                  mp3Config.volume,
                  mp3Volume);
  } else if (mp3Config.remoteWCB > 0) {
    Serial.printf("[MP3] Loaded: routes ;A to WCB%d\n", mp3Config.remoteWCB);
  }

  wireMp3Callbacks();   // one-time host hooks (volume mirror, ONFIN, onError)
  mp3BindCodec();       // bind the codec to the resolved port + sync the volume shadow
}

// Auto-learn the MP3 host from a WDP advert. First-host-wins + persisted: never
// override a local host or an already-stored host. Returns true if newly stored.
bool mp3AutoAddRemote(uint8_t hostWCB) {
  if (hostWCB == 0 || hostWCB == WCB_Number) return false;
  if (mp3Config.configured)                  return false;   // we host it locally
  if (mp3Config.remoteWCB != 0)              return false;   // already have a host
  mp3Config.remoteWCB = hostWCB;
  saveMP3Settings();
  Serial.printf("[WDP] MP3 host learned — routing ;A to WCB%d\n", hostWCB);
  return true;
}
