#include "WCB_RemoteTerm.h"  // Must be first — redirects Serial → WCBDebugSerial
#include "WCB_MP3.h"
#include "WCB_Storage.h"

// ---- Externs provided by WCB.ino / WCB_Storage.cpp ---------------------
extern int          WCB_Number;
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

// ---- Module globals -----------------------------------------------------
MP3Config mp3Config = {};
uint8_t   mp3Volume = 20;   // current tracked volume shadow

// Per-play callback — runtime only, not persisted
static String _mp3PendingCallback = "";

// Response accumulation state (persistent across loop() calls)
static String  _mp3RespBuf   = "";
static bool    _mp3InStatus  = false;  // accumulating a '=' status string
static bool    _mp3InTrigger = false;  // accumulating 'M' + 3-byte trigger report
static uint8_t _mp3TriggerBytes = 0;

// ==================== Port-conflict Query ================================

bool isSerialPortUsedForMP3(int port) {
  return mp3Config.configured && (mp3Config.serialPort == (uint8_t)port);
}

// ==================== Low-level Send =====================================

void sendMP3Raw(uint8_t byte1, int8_t byte2) {
  if (!mp3Config.configured || mp3Config.serialPort == 0) {
    Serial.printf("[MP3] Not configured — use %cMP3,S<port>:<baud>:V<vol>\n",
                  LocalFunctionIdentifier);
    return;
  }
  Stream &s = getSerialStream(mp3Config.serialPort);
  s.write(byte1);
  if (byte2 >= 0) s.write((uint8_t)byte2);
  s.flush();
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

  // ---- PLAY,n[,ONFIN,key]  or  PLAY,n[,key]  --------------------------
  // ---- (sends volume first, then play command) -------------------------
  if (restUpper.startsWith("PLAY,")) {
    String playArgs = rest.substring(5);  // everything after "PLAY,"

    // Split track number from optional callback
    int commaIdx = playArgs.indexOf(',');
    int trackNum;
    String callbackKey = "";

    if (commaIdx == -1) {
      // No callback — just a track number
      trackNum = playArgs.toInt();
    } else {
      trackNum = playArgs.substring(0, commaIdx).toInt();
      String afterTrack = playArgs.substring(commaIdx + 1);  // "ONFIN,key" or "key"
      String afterUpper = afterTrack;
      afterUpper.toUpperCase();

      if (afterUpper.startsWith("ONFIN,")) {
        callbackKey = afterTrack.substring(6);  // explicit: strip "ONFIN,"
      } else {
        callbackKey = afterTrack;               // implicit: the whole thing is the key
      }
      callbackKey.trim();
    }

    if (trackNum < 1 || trackNum > 255) {
      Serial.println("[MP3] PLAY: track number must be 1-255");
      return;
    }

    // Store callback (overwrites any previous pending callback)
    _mp3PendingCallback = callbackKey;

    // Send volume first, then play
    sendMP3Raw('v', (int8_t)mp3Volume);
    sendMP3Raw('t', (int8_t)trackNum);

    if (debugEnabled) {
      Serial.printf("[MP3] Play track %d  vol=%d", trackNum, mp3Volume);
      if (callbackKey.length() > 0) Serial.printf("  → callback: %s", callbackKey.c_str());
      Serial.println();
    }
    return;
  }

  // ---- PLAYFS,n[,ONFIN,key]  or  PLAYFS,n[,key]  ----------------------
  if (restUpper.startsWith("PLAYFS,")) {
    String playArgs = rest.substring(7);  // everything after "PLAYFS,"

    int commaIdx = playArgs.indexOf(',');
    int trackIdx;
    String callbackKey = "";

    if (commaIdx == -1) {
      trackIdx = playArgs.toInt();
    } else {
      trackIdx = playArgs.substring(0, commaIdx).toInt();
      String afterTrack = playArgs.substring(commaIdx + 1);
      String afterUpper = afterTrack;
      afterUpper.toUpperCase();

      if (afterUpper.startsWith("ONFIN,")) {
        callbackKey = afterTrack.substring(6);
      } else {
        callbackKey = afterTrack;
      }
      callbackKey.trim();
    }

    if (trackIdx < 0 || trackIdx > 255) {
      Serial.println("[MP3] PLAYFS: index must be 0-255");
      return;
    }

    _mp3PendingCallback = callbackKey;

    // Send volume first, then play
    sendMP3Raw('v', (int8_t)mp3Volume);
    sendMP3Raw('p', (int8_t)trackIdx);

    if (debugEnabled) {
      Serial.printf("[MP3] Play FS index %d  vol=%d", trackIdx, mp3Volume);
      if (callbackKey.length() > 0) Serial.printf("  → callback: %s", callbackKey.c_str());
      Serial.println();
    }
    return;
  }

  // ---- STOP  (start/stop toggle) ---------------------------------------
  if (restUpper == "STOP") {
    sendMP3Raw('O');
    if (debugEnabled) Serial.println("[MP3] Start/Stop toggle");
    return;
  }

  // ---- NEXT ------------------------------------------------------------
  if (restUpper == "NEXT") {
    sendMP3Raw('F');
    if (debugEnabled) Serial.println("[MP3] Next track");
    return;
  }

  // ---- PREV ------------------------------------------------------------
  if (restUpper == "PREV") {
    sendMP3Raw('R');
    if (debugEnabled) Serial.println("[MP3] Previous track");
    return;
  }

  // ---- VOL,n  (0=loudest, 64=inaudible ceiling) ------------------------
  if (restUpper.startsWith("VOL,")) {
    int n = rest.substring(4).toInt();
    if (n < 0 || n > 64) {
      Serial.println("[MP3] VOL: value must be 0-64 (0=loudest, 64=inaudible)");
      return;
    }
    mp3Volume = (uint8_t)n;
    sendMP3Raw('v', (int8_t)mp3Volume);
    saveMP3Settings();
    if (debugEnabled) Serial.printf("[MP3] Volume set to %d\n", mp3Volume);
    return;
  }

  // ---- VOLUP  (louder — decrease value, floor 0) -----------------------
  if (restUpper == "VOLUP") {
    mp3Volume = (mp3Volume <= 5) ? 0 : mp3Volume - 5;
    sendMP3Raw('v', (int8_t)mp3Volume);
    saveMP3Settings();
    Serial.printf("[MP3] Volume up → %d\n", mp3Volume);
    return;
  }

  // ---- VOLDN  (quieter — increase value, ceiling 64) -------------------
  if (restUpper == "VOLDN") {
    mp3Volume = (mp3Volume >= 59) ? 64 : mp3Volume + 5;
    sendMP3Raw('v', (int8_t)mp3Volume);
    saveMP3Settings();
    Serial.printf("[MP3] Volume down → %d\n", mp3Volume);
    return;
  }

  // ---- COUNT  (request total track count) ------------------------------
  if (restUpper == "COUNT") {
    sendMP3Raw('S', (int8_t)'1');
    if (debugEnabled) Serial.println("[MP3] Requesting track count...");
    return;
  }

  // ---- VER  (request firmware version string) --------------------------
  if (restUpper == "VER") {
    sendMP3Raw('S', (int8_t)'0');
    if (debugEnabled) Serial.println("[MP3] Requesting firmware version...");
    return;
  }

  // ---- Unknown ---------------------------------------------------------
  Serial.printf("[MP3] Unknown audio command: %s\n", rest.c_str());
  Serial.println("  Valid: PLAY,n[,ONFIN,key]  PLAY,n[,key]  PLAYFS,n[,ONFIN,key]");
  Serial.println("         STOP  NEXT  PREV");
  Serial.println("         VOL,n (0-64)  VOLUP  VOLDN");
  Serial.println("         COUNT  VER");
}

// ==================== Response Reader ====================================
// Non-blocking. Call once per loop() tick.
// The serial task is told to skip this port (isSerialPortUsedForMP3),
// so these bytes are exclusively consumed here.

void processMP3Responses() {
  if (!mp3Config.configured || mp3Config.serialPort == 0) return;

  Stream &s = getSerialStream(mp3Config.serialPort);

  while (s.available()) {
    uint8_t b = s.read();

    // ---- 'M' + 3 bytes: quiet-mode trigger report — silently discard ---
    if (_mp3InTrigger) {
      _mp3TriggerBytes++;
      if (_mp3TriggerBytes >= 3) {
        _mp3InTrigger    = false;
        _mp3TriggerBytes = 0;
      }
      continue;
    }

    // ---- '=' + ASCII + CR: status response (version / track count) -----
    if (_mp3InStatus) {
      if (b == '\r' || b == '\n') {
        Serial.printf("[MP3] Status: %s\n", _mp3RespBuf.c_str());
        _mp3RespBuf  = "";
        _mp3InStatus = false;
      } else {
        _mp3RespBuf += (char)b;
      }
      continue;
    }

    // ---- Single-byte responses -----------------------------------------
    switch (b) {

      case 'X':   // Track finished normally
        Serial.println("[MP3] Track finished");
        if (_mp3PendingCallback.length() > 0) {
          recallCommandSlot(_mp3PendingCallback, 0);
          _mp3PendingCallback = "";
        }
        _mp3RespBuf = "";
        break;

      case 'x':   // Track cancelled (stop during playback)
        Serial.println("[MP3] Track cancelled");
        _mp3PendingCallback = "";  // clear — don't fire on cancel
        _mp3RespBuf = "";
        break;

      case 'E':   // Error — track not found or device fault
        Serial.println("[MP3] Error: track not found or device error");
        _mp3PendingCallback = "";  // clear — don't fire ONFIN on error
        _mp3RespBuf = "";
        if (strlen(mp3Config.onErrCmd) > 0) {
          recallCommandSlot(String(mp3Config.onErrCmd), 0);
        }
        break;

      case '=':   // Start of status string
        _mp3RespBuf  = "";
        _mp3InStatus = true;
        break;

      case 'M':   // Start of quiet-mode trigger report (M + 3 bytes)
        _mp3InTrigger    = true;
        _mp3TriggerBytes = 0;
        break;

      default:    // Unknown byte — silently ignore
        break;
    }
  }
}

// ==================== Configuration (?MP3,...) ===========================

void clearMP3Config() {
  uint8_t freedPort = mp3Config.serialPort;

  memset(&mp3Config, 0, sizeof(mp3Config));
  mp3Config.configured = false;
  mp3Config.baudRate   = 9600;
  mp3Config.volume     = 20;
  mp3Volume            = 20;
  _mp3PendingCallback  = "";

  saveMP3Settings();
  Serial.println("[MP3] Configuration cleared");

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
  mp3Volume            = (uint8_t)volume;

  saveMP3Settings();

  Serial.printf("[MP3] Configured: S%d at %d baud  default volume=%d\n",
                serialPort, baudRate, volume);
  if (strlen(mp3Config.onErrCmd) > 0)
    Serial.printf("  On error → ;C%s\n", mp3Config.onErrCmd);
}

void printMP3Settings() {
  Serial.println("\n--- MP3 Trigger Configuration ---");
  if (!mp3Config.configured) {
    Serial.println("  Not configured.");
    Serial.printf("  Use: %cMP3,S<port>:<baud>:V<vol>  (e.g. %cMP3,S2:9600:V25)\n",
                  LocalFunctionIdentifier, LocalFunctionIdentifier);
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
                    char delimiter, bool printToSerial) {
  if (!mp3Config.configured) return;

  // Main config line — includes volume
  String cmdSuffix = "MP3,S" + String(mp3Config.serialPort) +
                     ":" + String(mp3Config.baudRate) +
                     ":V" + String(mp3Config.volume);
  String cmd = String(LocalFunctionIdentifier) + cmdSuffix;
  if (printToSerial) Serial.println(cmd);
  chainedConfig        += String(delimiter) + cmd;
  chainedConfigDefault += "^?" + cmdSuffix;

  // Error callback
  if (strlen(mp3Config.onErrCmd) > 0) {
    cmdSuffix = "MP3,ONERR," + String(mp3Config.onErrCmd);
    cmd = String(LocalFunctionIdentifier) + cmdSuffix;
    if (printToSerial) Serial.println(cmd);
    chainedConfig        += String(delimiter) + cmd;
    chainedConfigDefault += "^?" + cmdSuffix;
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
  preferences.end();

  strncpy(mp3Config.onErrCmd, err.c_str(), sizeof(mp3Config.onErrCmd) - 1);
  mp3Config.onErrCmd[sizeof(mp3Config.onErrCmd) - 1] = '\0';

  if (mp3Config.configured) {
    Serial.printf("[MP3] Loaded: S%d at %lu baud  default vol=%d  current vol=%d\n",
                  mp3Config.serialPort,
                  (unsigned long)mp3Config.baudRate,
                  mp3Config.volume,
                  mp3Volume);
  }
}
