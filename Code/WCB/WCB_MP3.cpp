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
uint8_t   mp3Volume = 20;   // initial shadow — matches test-sketch default

// Response accumulation state (persistent across loop() calls)
static String _mp3RespBuf  = "";
static bool   _mp3InStatus = false;  // true while accumulating a '=' status string
static bool   _mp3InTrigger = false; // true while accumulating 'M' + 3-byte trigger report
static uint8_t _mp3TriggerBytes = 0;

// ==================== Port-conflict Query ================================

bool isSerialPortUsedForMP3(int port) {
  return mp3Config.configured && (mp3Config.serialPort == (uint8_t)port);
}

// ==================== Low-level Send =====================================

void sendMP3Raw(uint8_t byte1, int8_t byte2) {
  if (!mp3Config.configured || mp3Config.serialPort == 0) {
    Serial.printf("[MP3] Not configured — use %cMP3,S<port>:<baud>\n",
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
  // Expected formats after stripping 'A':
  //   ,PLAY,5   ,PLAYFS,3   ,STOP   ,NEXT   ,PREV
  //   ,VOL,20   ,VOLUP      ,VOLDN
  //   ,COUNT    ,VER

  if (!mp3Config.configured) {
    Serial.printf("[MP3] Not configured — use %cMP3,S<port>:<baud>\n",
                  LocalFunctionIdentifier);
    return;
  }

  // Strip leading 'A' and optional following comma
  String rest = message.substring(1);
  rest.trim();
  if (rest.startsWith(",")) rest = rest.substring(1);

  String restUpper = rest;
  restUpper.toUpperCase();

  // ---- PLAY,n  (by track name order, 1-255) ----------------------------
  if (restUpper.startsWith("PLAY,")) {
    int n = rest.substring(5).toInt();
    if (n < 1 || n > 255) {
      Serial.println("[MP3] PLAY: track number must be 1-255");
      return;
    }
    sendMP3Raw('t', (int8_t)n);
    if (debugEnabled) Serial.printf("[MP3] Play track %d\n", n);
    return;
  }

  // ---- PLAYFS,n  (by filesystem / SD card order, 0-255) ---------------
  if (restUpper.startsWith("PLAYFS,")) {
    int n = rest.substring(7).toInt();
    if (n < 0 || n > 255) {
      Serial.println("[MP3] PLAYFS: index must be 0-255");
      return;
    }
    sendMP3Raw('p', (int8_t)n);
    if (debugEnabled) Serial.printf("[MP3] Play filesystem index %d\n", n);
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

  // ---- VOL,n  (absolute volume: 0=max loud, 255=silent) ---------------
  if (restUpper.startsWith("VOL,")) {
    int n = rest.substring(4).toInt();
    if (n < 0 || n > 255) {
      Serial.println("[MP3] VOL: value must be 0-255 (0=loudest)");
      return;
    }
    mp3Volume = (uint8_t)n;
    sendMP3Raw('v', (int8_t)mp3Volume);
    saveMP3Settings();  // persist volume shadow to NVS
    if (debugEnabled) Serial.printf("[MP3] Volume set to %d\n", mp3Volume);
    return;
  }

  // ---- VOLUP  (louder — decrease byte value by 5, floor 0) ------------
  if (restUpper == "VOLUP") {
    mp3Volume = (mp3Volume <= 5) ? 0 : mp3Volume - 5;
    sendMP3Raw('v', (int8_t)mp3Volume);
    saveMP3Settings();
    Serial.printf("[MP3] Volume up → %d\n", mp3Volume);
    return;
  }

  // ---- VOLDN  (quieter — increase byte value by 5, ceiling 255) -------
  if (restUpper == "VOLDN") {
    mp3Volume = (mp3Volume >= 250) ? 255 : mp3Volume + 5;
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
  Serial.println("  Valid commands: PLAY,n  PLAYFS,n  STOP  NEXT  PREV");
  Serial.println("                  VOL,n   VOLUP     VOLDN");
  Serial.println("                  COUNT   VER");
}

// ==================== Response Reader ====================================
// Call once per loop() tick.  Reads any bytes the MP3 Trigger has sent
// back and interprets them per the SparkFun v2.4 serial protocol.
// The serial task is told to skip this port (isSerialPortUsedForMP3),
// so these bytes are exclusively consumed here.

void processMP3Responses() {
  if (!mp3Config.configured || mp3Config.serialPort == 0) return;

  Stream &s = getSerialStream(mp3Config.serialPort);

  while (s.available()) {
    uint8_t b = s.read();

    // ---- 'M' + 3 bytes: trigger report in quiet mode -------------------
    // Accumulate and discard — quiet mode is not used by default.
    if (_mp3InTrigger) {
      _mp3TriggerBytes++;
      if (_mp3TriggerBytes >= 3) {
        _mp3InTrigger   = false;
        _mp3TriggerBytes = 0;
      }
      continue;
    }

    // ---- '=' + ASCII string + CR: status response ----------------------
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

    // ---- Dispatch single-byte responses --------------------------------
    switch (b) {

      case 'X':   // Track finished normally
        Serial.println("[MP3] Track finished");
        _mp3RespBuf = "";
        if (strlen(mp3Config.onFinCmd) > 0) {
          recallCommandSlot(String(mp3Config.onFinCmd), 0);
        }
        break;

      case 'x':   // Track cancelled (stop during playback)
        Serial.println("[MP3] Track cancelled");
        _mp3RespBuf = "";
        break;

      case 'E':   // Error — track not found or device fault
        Serial.println("[MP3] Error: track not found or device error");
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
        _mp3InTrigger   = true;
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
  mp3Volume            = 20;

  saveMP3Settings();
  Serial.println("[MP3] Configuration cleared");

  if (freedPort > 0) {
    // Re-enable broadcast output on the freed port
    if (!serialBroadcastEnabled[freedPort - 1]) {
      serialBroadcastEnabled[freedPort - 1] = true;
      saveBroadcastSettingsToPreferences();
      Serial.printf("  ✓ Re-enabled broadcast output on S%d\n", freedPort);
    }
    // Re-enable broadcast input on the freed port
    if (blockBroadcastFrom[freedPort - 1]) {
      blockBroadcastFrom[freedPort - 1] = false;
      saveBroadcastBlockSettings();
      Serial.printf("  ✓ Re-enabled broadcast input on S%d\n", freedPort);
    }
    // Reset baud rate
    updateBaudRate(freedPort, 9600);
    Serial.printf("  ✓ Reset S%d baud rate to 9600\n", freedPort);
  }
}

void configureMP3(const String &args) {
  // ?MP3,S<port>:<baud>       — configure on this WCB
  // ?MP3,LIST                 — show current configuration
  // ?MP3,CLEAR                — remove configuration
  // ?MP3,ONFIN,<key>          — set track-finished callback (stored-cmd key)
  // ?MP3,ONFIN,CLEAR          — remove track-finished callback
  // ?MP3,ONERR,<key>          — set error callback (stored-cmd key)
  // ?MP3,ONERR,CLEAR          — remove error callback

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

  // ---- ONFIN,... --------------------------------------------------
  if (aUpper.startsWith("ONFIN,")) {
    String key = args.substring(6);   // preserve original case
    key.trim();
    if (key.equalsIgnoreCase("CLEAR")) {
      memset(mp3Config.onFinCmd, 0, sizeof(mp3Config.onFinCmd));
      saveMP3Settings();
      Serial.println("[MP3] Track-finished callback cleared");
    } else if (key.length() == 0 || key.length() > 23) {
      Serial.println("[MP3] ONFIN: key must be 1-23 characters");
    } else {
      strncpy(mp3Config.onFinCmd, key.c_str(), sizeof(mp3Config.onFinCmd) - 1);
      mp3Config.onFinCmd[sizeof(mp3Config.onFinCmd) - 1] = '\0';
      saveMP3Settings();
      Serial.printf("[MP3] Track-finished callback → ;C%s\n", mp3Config.onFinCmd);
    }
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

  // ---- S<port>:<baud>  (main configuration) -----------------------
  if (!aUpper.startsWith("S")) {
    Serial.printf("[MP3] Invalid format. Use: %cMP3,S<port>:<baud>\n",
                  LocalFunctionIdentifier);
    Serial.printf("  Example: %cMP3,S2:9600\n", LocalFunctionIdentifier);
    return;
  }

  int colonIdx = a.indexOf(':');
  if (colonIdx == -1) {
    Serial.printf("[MP3] Missing baud rate. Use: %cMP3,S<port>:<baud>\n",
                  LocalFunctionIdentifier);
    return;
  }

  int serialPort = a.substring(1, colonIdx).toInt();
  int baudRate   = a.substring(colonIdx + 1).toInt();

  if (serialPort < 1 || serialPort > 5) {
    Serial.println("[MP3] Invalid serial port. Must be S1-S5");
    return;
  }

  if (baudRate != 9600 && baudRate != 38400) {
    Serial.println("[MP3] MP3 Trigger only supports 9600 or 38400 baud");
    return;
  }

  // Software serial ports (S3-S5) can't reliably do 38400
  if (serialPort >= 3 && baudRate > 9600) {
    Serial.println("\n⚠️  =============== WARNING ===============");
    Serial.printf("S%d is SOFTWARE SERIAL\n", serialPort);
    Serial.println("Software serial is unreliable above 9600 baud");
    Serial.println("❌ CONFIGURATION BLOCKED!");
    Serial.println("Options:");
    Serial.printf("  1. Use hardware serial (S1 or S2): %cMP3,S1:%d\n",
                  LocalFunctionIdentifier, baudRate);
    Serial.printf("  2. Use 9600 baud:                  %cMP3,S%d:9600\n",
                  LocalFunctionIdentifier, serialPort);
    Serial.println("=========================================\n");
    return;
  }

  // Update baud rate if it has changed
  if ((uint32_t)baudRate != baudRates[serialPort - 1]) {
    updateBaudRate(serialPort, baudRate);
  }

  // Disable broadcast output on this port (like Maestro)
  if (serialBroadcastEnabled[serialPort - 1]) {
    serialBroadcastEnabled[serialPort - 1] = false;
    saveBroadcastSettingsToPreferences();
    Serial.printf("  ⚠️  Disabled broadcast output on S%d (MP3 Trigger port)\n", serialPort);
  }

  // Disable broadcast input on this port (we own the RX exclusively)
  if (!blockBroadcastFrom[serialPort - 1]) {
    blockBroadcastFrom[serialPort - 1] = true;
    saveBroadcastBlockSettings();
    Serial.printf("  ⚠️  Disabled broadcast input on S%d (MP3 Trigger port)\n", serialPort);
  }

  mp3Config.serialPort = (uint8_t)serialPort;
  mp3Config.baudRate   = (uint32_t)baudRate;
  mp3Config.configured = true;

  saveMP3Settings();

  Serial.printf("[MP3] Configured: S%d at %d baud\n", serialPort, baudRate);
  if (strlen(mp3Config.onFinCmd) > 0)
    Serial.printf("  On track finish → ;C%s\n", mp3Config.onFinCmd);
  if (strlen(mp3Config.onErrCmd) > 0)
    Serial.printf("  On error        → ;C%s\n", mp3Config.onErrCmd);
}

void printMP3Settings() {
  Serial.println("\n--- MP3 Trigger Configuration ---");
  if (!mp3Config.configured) {
    Serial.println("  Not configured.");
    Serial.printf("  Use: %cMP3,S<port>:<baud>  (e.g. %cMP3,S2:9600)\n",
                  LocalFunctionIdentifier, LocalFunctionIdentifier);
    Serial.println("---------------------------------");
    return;
  }
  Serial.printf("  Port      : S%d\n",   mp3Config.serialPort);
  Serial.printf("  Baud      : %lu\n",   (unsigned long)mp3Config.baudRate);
  Serial.printf("  Volume    : %d  (0=loudest, 255=silent)\n", mp3Volume);
  Serial.printf("  On Finish : %s\n",
                strlen(mp3Config.onFinCmd) ? mp3Config.onFinCmd : "(none)");
  Serial.printf("  On Error  : %s\n",
                strlen(mp3Config.onErrCmd) ? mp3Config.onErrCmd : "(none)");
  Serial.println("---------------------------------");
}

void printMP3Backup(String &chainedConfig, String &chainedConfigDefault,
                    char delimiter, bool printToSerial) {
  if (!mp3Config.configured) return;

  // ---- Port / baud config ----
  String cmdSuffix = "MP3,S" + String(mp3Config.serialPort) +
                     ":" + String(mp3Config.baudRate);
  String cmd = String(LocalFunctionIdentifier) + cmdSuffix;
  if (printToSerial) Serial.println(cmd);
  chainedConfig        += String(delimiter) + cmd;
  chainedConfigDefault += "^?" + cmdSuffix;   // factory reset always uses ^?

  // ---- Track-finished callback ----
  if (strlen(mp3Config.onFinCmd) > 0) {
    cmdSuffix = "MP3,ONFIN," + String(mp3Config.onFinCmd);
    cmd = String(LocalFunctionIdentifier) + cmdSuffix;
    if (printToSerial) Serial.println(cmd);
    chainedConfig        += String(delimiter) + cmd;
    chainedConfigDefault += "^?" + cmdSuffix;
  }

  // ---- Error callback ----
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
  preferences.putUChar ("port",  mp3Config.serialPort);
  preferences.putUInt  ("baud",  mp3Config.baudRate);
  preferences.putBool  ("en",    mp3Config.configured);
  preferences.putUChar ("vol",   mp3Volume);
  preferences.putString("onFin", mp3Config.onFinCmd);
  preferences.putString("onErr", mp3Config.onErrCmd);
  preferences.end();
}

void loadMP3Settings() {
  preferences.begin("mp3_cfg", true);
  mp3Config.serialPort = preferences.getUChar ("port",  0);
  mp3Config.baudRate   = preferences.getUInt  ("baud",  9600);
  mp3Config.configured = preferences.getBool  ("en",    false);
  mp3Volume            = preferences.getUChar ("vol",   20);
  String fin           = preferences.getString("onFin", "");
  String err           = preferences.getString("onErr", "");
  preferences.end();

  strncpy(mp3Config.onFinCmd, fin.c_str(), sizeof(mp3Config.onFinCmd) - 1);
  mp3Config.onFinCmd[sizeof(mp3Config.onFinCmd) - 1] = '\0';
  strncpy(mp3Config.onErrCmd, err.c_str(), sizeof(mp3Config.onErrCmd) - 1);
  mp3Config.onErrCmd[sizeof(mp3Config.onErrCmd) - 1] = '\0';

  if (mp3Config.configured) {
    Serial.printf("[MP3] Loaded: S%d at %lu baud  vol=%d\n",
                  mp3Config.serialPort,
                  (unsigned long)mp3Config.baudRate,
                  mp3Volume);
  }
}
