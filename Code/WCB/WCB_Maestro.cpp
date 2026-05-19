#include "WCB_RemoteTerm.h"  // Must be first — redirects Serial → WCBDebugSerial
#include "WCB_Maestro.h"
#include "WCB_Storage.h"

extern bool maestroEnabled;
extern int WCB_Number;
extern int Default_WCB_Quantity;
extern bool lastReceivedViaESPNOW;
extern bool debugEnabled;
extern char LocalFunctionIdentifier;
extern char CommandCharacter;
extern unsigned long baudRates[5];
extern void sendESPNowMessage(uint8_t target, const char *message, bool useETM = true);
extern Stream &getSerialStream(int port);
extern void updateBaudRate(int port, int baud);

MaestroConfig maestroConfigs[MAX_MAESTROS_PER_WCB];

// ==================== Helper Functions ====================

int8_t findSlotByMaestroID(uint8_t maestroID) {
  for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
    if (maestroConfigs[i].configured && maestroConfigs[i].maestroID == maestroID) {
      return i;
    }
  }
  return -1;  // Not found
}

// Find a slot matching BOTH maestroID and target (remoteWCB == 0 means local).
// Used during configuration so that two entries with the same ID but different
// targets (e.g. M2:W2 local and M2:W3 remote) each get their own slot rather
// than the second overwriting the first.
int8_t findSlotByMaestroIDAndTarget(uint8_t maestroID, uint8_t remoteWCB) {
  for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
    if (maestroConfigs[i].configured &&
        maestroConfigs[i].maestroID == maestroID &&
        maestroConfigs[i].remoteWCB == remoteWCB) {
      return i;
    }
  }
  return -1;
}

// Slot identity = (maestroID, serialPort, remoteWCB). Baud is intentionally
// excluded: configuring the same Maestro on the same port with only a new
// baud rate must UPDATE the existing slot, not create a duplicate. Same ID
// on a different port, or the same ID targeting a different WCB, still gets
// its own slot. For local maestros serialPort is the S-port and remoteWCB
// is 0; for remote maestros serialPort is 0 and remoteWCB is the target.
int8_t findSlotByMaestroIDPortTarget(uint8_t maestroID, uint8_t serialPort, uint8_t remoteWCB) {
  for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
    if (maestroConfigs[i].configured &&
        maestroConfigs[i].maestroID  == maestroID &&
        maestroConfigs[i].serialPort == serialPort &&
        maestroConfigs[i].remoteWCB  == remoteWCB) {
      return i;
    }
  }
  return -1;
}

int8_t findEmptySlot() {
  for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
    if (!maestroConfigs[i].configured) {
      return i;
    }
  }
  return -1;  // No empty slots
}

bool isMaestroConfigured(uint8_t maestroID) {
  return findSlotByMaestroID(maestroID) >= 0;
}

// ==================== Command Sending ====================

void sendMaestroCommand(uint8_t maestroID, uint8_t scriptNumber) {

  // Iterate ALL slots with this ID — the same maestroID can appear on multiple
  // targets (e.g. M2 local on this board AND M2 remote on WCB3), and every
  // matching slot must receive the command.
  bool handled = false;
  for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
    if (!maestroConfigs[i].configured || maestroConfigs[i].maestroID != maestroID) continue;
    MaestroConfig &config = maestroConfigs[i];

    // LOCAL Maestro
    if (config.serialPort > 0) {
      uint8_t command[] = {0xAA, maestroID, 0x27, scriptNumber};
      Stream &targetSerial = getSerialStream(config.serialPort);
      // write() queues bytes into the hardware UART TX buffer (256 bytes).
      // flush() would block ~347–694 µs waiting for the 4 bytes to transmit —
      // unnecessary and a direct source of jitter. The UART drains asynchronously.
      targetSerial.write(command, sizeof(command));
      Serial.printf("→ Maestro %d: Local S%d, Script %d\n",
                    maestroID, config.serialPort, scriptNumber);
      handled = true;
    }

    // REMOTE Maestro — use ETM so the receiving board ACKs the command.
    // ;m commands are processed commands, not raw passthrough, so delivery
    // confirmation matters.  Raw Kyber Maestro port bytes bypass this path entirely.
    if (config.remoteWCB > 0 && !lastReceivedViaESPNOW) {
      String espnowMsg = String(CommandCharacter) + "M" + String(maestroID) + String(scriptNumber);
      sendESPNowMessage(config.remoteWCB, espnowMsg.c_str());
      if (debugEnabled) {
        Serial.printf("→ Maestro %d: Unicast to WCB%d, Script %d\n",
                      maestroID, config.remoteWCB, scriptNumber);
      }
      handled = true;
    }
  }

  if (handled) return;

  // Broadcast (maestroID == 0)
  if (maestroID == 0) {
    for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
      if (maestroConfigs[i].configured && maestroConfigs[i].serialPort > 0) {
        uint8_t command[] = {0xAA, maestroConfigs[i].maestroID, 0x27, scriptNumber};
        Stream &targetSerial = getSerialStream(maestroConfigs[i].serialPort);
        targetSerial.write(command, sizeof(command)); // no flush — UART drains async
      }
    }
    
    if (!isMaestroConfigured(WCB_Number)) {
      uint8_t command[] = {0xAA, WCB_Number, 0x27, scriptNumber};
      Serial1.write(command, sizeof(command)); // no flush — UART drains async
    }
    
    if (!lastReceivedViaESPNOW) {
      String espnowMsg = String(CommandCharacter) + "M0" + String(scriptNumber);
      sendESPNowMessage(0, espnowMsg.c_str(), false);
    }
    
    if (debugEnabled) {
      Serial.printf("→ Maestro Broadcast: Script %d\n", scriptNumber);
    }
    return;
  }
  
  // Legacy behavior
  if (maestroID == WCB_Number || maestroID == 9) {
    uint8_t command[] = {0xAA, (maestroID == 9) ? WCB_Number : maestroID, 0x27, scriptNumber};
    Serial1.write(command, sizeof(command)); // no flush — UART drains async
    Serial.printf("→ Maestro %d: Legacy S1, Script %d\n", maestroID, scriptNumber);
    return;
  }
  
  // Unconfigured fallback
  if (maestroID >= 1 && maestroID <= Default_WCB_Quantity) {
    if (!lastReceivedViaESPNOW) {
      String espnowMsg = String(CommandCharacter) + "M" + String(maestroID) + String(scriptNumber);
      sendESPNowMessage(maestroID, espnowMsg.c_str(), false);
      if (debugEnabled) {
        Serial.printf("→ Maestro %d: Fallback unicast to WCB%d, Script %d\n",
                      maestroID, maestroID, scriptNumber);
      }
    }
  } else {
    if (!lastReceivedViaESPNOW) {
      String espnowMsg = String(CommandCharacter) + "M" + String(maestroID) + String(scriptNumber);
      sendESPNowMessage(0, espnowMsg.c_str(), false);
      Serial.printf("→ Maestro %d: Fallback broadcast, Script %d\n",
                    maestroID, scriptNumber);
    }
  }
}

// ==================== Configuration Functions ====================
void configureMaestro(const String &message) {
  // Format: ?MAESTRO,M<maestroID>:W<wcb>S<port>:<baud>
  // Example: ?MAESTRO,M2:W2S1:57600
  // Chained: ?MAESTRO,M1:W1S2:115200,M2:W2S1:57600,M3:W3S3:115200
  
String remaining = message;
  remaining.trim();
  if (remaining.isEmpty() || !remaining.startsWith("M") && !remaining.startsWith("m")) {
    Serial.printf("Invalid format. Use: %cMAESTRO,M<maestroID>:W<wcb>S<port>:<baud>\n", LocalFunctionIdentifier);
    Serial.printf("Example: %cMAESTRO,M2:W2S1:57600\n", LocalFunctionIdentifier);
    return;
  }
  
  // Process each Maestro config (split by comma)
  int startIdx = 0;
  while (startIdx < remaining.length()) {
    int nextComma = remaining.indexOf(',', startIdx);
    if (nextComma == -1) nextComma = remaining.length();
    
    String singleConfig = remaining.substring(startIdx, nextComma);
    singleConfig.trim();
    singleConfig.toUpperCase();
    
    // Must start with 'M'
    if (!singleConfig.startsWith("M")) {
      Serial.printf("Invalid Maestro config: %s (must start with M)\n", singleConfig.c_str());
      Serial.printf("Format should be: M<maestroID>:W<wcb>S<port>:<baud>\n");
      startIdx = nextComma + 1;
      continue;
    }
    
    // Parse: M2:W2S1:57600
    int firstColon = singleConfig.indexOf(':');
    int secondColon = singleConfig.indexOf(':', firstColon + 1);
    
    if (firstColon == -1 || secondColon == -1) {
      Serial.printf("Invalid Maestro config: %s\n", singleConfig.c_str());
      Serial.printf("Format should be: M<maestroID>:W<wcb>S<port>:<baud>\n");
      startIdx = nextComma + 1;
      continue;
    }
    
    int maestroID = singleConfig.substring(1, firstColon).toInt();  // Skip 'M'
    String destStr = singleConfig.substring(firstColon + 1, secondColon);
    int baudRate = singleConfig.substring(secondColon + 1).toInt();
    
    // Validate Maestro ID
    if (maestroID < 1 || maestroID > 9) {
      Serial.println("Invalid Maestro ID. Must be 1-9");
      startIdx = nextComma + 1;
      continue;
    }
    
    // Parse W<wcb>S<port> format
    int wIndex = destStr.indexOf('W');
    int sIndex = destStr.indexOf('S');
    
    if (wIndex == -1 || sIndex == -1 || sIndex <= wIndex) {
      Serial.println("Destination must be in format W<wcb>S<port> (e.g., W2S1)");
      startIdx = nextComma + 1;
      continue;
    }
    
    int targetWCB = destStr.substring(wIndex + 1, sIndex).toInt();
    int serialPort = destStr.substring(sIndex + 1).toInt();
    
    if (targetWCB < 1 || targetWCB > Default_WCB_Quantity) {
      Serial.printf("Invalid WCB number. Must be W1-W%d\n", Default_WCB_Quantity);
      startIdx = nextComma + 1;
      continue;
    }
    
    if (serialPort < 1 || serialPort > 5) {
      Serial.println("Invalid serial port. Must be S1-S5");
      startIdx = nextComma + 1;
      continue;
    }
    
    // Validate baud rate
    if (!(baudRate == 0 || baudRate == 110 || baudRate == 300 || 
          baudRate == 600 || baudRate == 1200 || baudRate == 2400 || 
          baudRate == 9600 || baudRate == 14400 || baudRate == 19200 || 
          baudRate == 38400 || baudRate == 57600 || baudRate == 115200 || 
          baudRate == 128000 || baudRate == 256000)) {
      Serial.println("Invalid baud rate");
      startIdx = nextComma + 1;
      continue;
    }
    
    // Find or create slot — keyed on (maestroID, serialPort, target). A repeat
    // config of the same Maestro on the same port (e.g. only the baud rate
    // changed) updates the existing slot instead of adding a duplicate. Same
    // ID on a different port, or targeting a different WCB, still gets its own
    // slot. The slot stores serialPort=0 for remote maestros, so the key uses
    // 0 for the port in the remote case to match how the slot is written below.
    uint8_t effectiveRemoteWCB = (targetWCB == WCB_Number) ? 0 : targetWCB;
    uint8_t keyPort            = (targetWCB == WCB_Number) ? (uint8_t)serialPort : 0;
    int8_t slot = findSlotByMaestroIDPortTarget(maestroID, keyPort, effectiveRemoteWCB);
    if (slot < 0) {
      slot = findEmptySlot();
      if (slot < 0) {
        Serial.printf("No available slots. Maximum %d Maestros.\n", MAX_MAESTROS_PER_WCB);
        startIdx = nextComma + 1;
        continue;
      }
    }
    
    // DETERMINE IF LOCAL OR REMOTE
    if (targetWCB == WCB_Number) {
      // LOCAL CONFIGURATION
      
      // Software serial warning
      if (serialPort >= 3 && serialPort <= 5 && baudRate > 57600) {
        Serial.println("\n⚠️  =============== WARNING ===============");
        Serial.printf("S%d is SOFTWARE SERIAL\n", serialPort);
        Serial.printf("Baud rate (%d) is TOO HIGH for software serial\n", baudRate);
        Serial.println("❌ CONFIGURATION BLOCKED!");
        Serial.println("Options:");
        Serial.printf("  1. Use hardware serial: %cMAESTRO,M%d:W%dS1:%d\n", 
                      LocalFunctionIdentifier, maestroID, WCB_Number, baudRate);
        Serial.printf("  2. Lower baud rate: %cMAESTRO,M%d:W%dS%d:57600\n", 
                      LocalFunctionIdentifier, maestroID, WCB_Number, serialPort);
        Serial.println("========================================\n");
        startIdx = nextComma + 1;
        continue;
      }
      
      // Update baud rate if needed
      if (baudRate != baudRates[serialPort - 1]) {
        updateBaudRate(serialPort, baudRate);
      }

      // **DISABLE BROADCAST OUTPUT ON THIS PORT**
      if (serialBroadcastEnabled[serialPort - 1]) {
        serialBroadcastEnabled[serialPort - 1] = false;
        saveBroadcastSettingsToPreferences();
        Serial.printf("⚠️  Disabled broadcast output on S%d (Maestro port)\n", serialPort);
      }

      // **DISABLE BROADCAST INPUT ON THIS PORT**
      if (!blockBroadcastFrom[serialPort - 1]) {
        blockBroadcastFrom[serialPort - 1] = true;
        saveBroadcastBlockSettings();
        Serial.printf("⚠️  Disabled broadcast input on S%d (Maestro port)\n", serialPort);
      }
      
      maestroConfigs[slot].maestroID = maestroID;
      maestroConfigs[slot].serialPort = serialPort;
      maestroConfigs[slot].remoteWCB = 0;
      maestroConfigs[slot].configured = true;
      maestroConfigs[slot].baudRate = baudRate;
      
      saveMaestroSettings();
      Serial.printf("✓ Maestro %d: Local S%d at %d baud (slot %d)\n", 
                    maestroID, serialPort, baudRate, slot + 1);
                    
    } else {
      // REMOTE CONFIGURATION
      maestroConfigs[slot].maestroID = maestroID;
      maestroConfigs[slot].serialPort = 0;
      maestroConfigs[slot].remoteWCB = targetWCB;
      maestroConfigs[slot].configured = true;
      maestroConfigs[slot].baudRate = baudRate;
      
      saveMaestroSettings();
      Serial.printf("✓ Maestro %d: Remote on WCB%d (unicast, slot %d)\n", 
                    maestroID, targetWCB, slot + 1);
    }
    
    startIdx = nextComma + 1;
  }
}

// Clear one slot and do the port housekeeping (broadcast re-enable, baud
// reset). Does NOT call saveMaestroSettings() — the caller saves once after
// clearing one or more slots.
static void _clearMaestroSlot(int8_t slot) {
  if (slot < 0) return;

  uint8_t freedPort = maestroConfigs[slot].serialPort;

  maestroConfigs[slot].configured = false;
  maestroConfigs[slot].maestroID  = 0;
  maestroConfigs[slot].serialPort = 0;
  maestroConfigs[slot].remoteWCB  = 0;
  maestroConfigs[slot].baudRate   = 0;

  // Auto re-enable broadcast on freed port (only if no OTHER slot still uses it)
  if (freedPort > 0) {
    bool portStillUsed = false;
    for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
      if (maestroConfigs[i].configured && maestroConfigs[i].serialPort == freedPort) {
        portStillUsed = true;
        break;
      }
    }
    if (!portStillUsed) {
      if (!serialBroadcastEnabled[freedPort - 1]) {
        serialBroadcastEnabled[freedPort - 1] = true;
        saveBroadcastSettingsToPreferences();
        Serial.printf("✓ Re-enabled broadcast output on S%d\n", freedPort);
      }
      if (blockBroadcastFrom[freedPort - 1]) {
        blockBroadcastFrom[freedPort - 1] = false;
        saveBroadcastBlockSettings();
        Serial.printf("✓ Re-enabled broadcast input on S%d\n", freedPort);
      }
      updateBaudRate(freedPort, 9600);
      Serial.printf("✓ Reset S%d baud rate to 9600\n", freedPort);
    }
  }
}

void clearMaestroByID(const String &message) {
  // Caller strips "CLEAR," and passes the remainder. Accepts:
  //   "M5" / "5"             → clear ALL slots with that Maestro ID
  //   "M1:W1S2" / "1:W1S2"   → clear ONLY that specific (id, port, target)
  //                            slot. Required now that the same ID can occupy
  //                            multiple slots (e.g. M1 moved S2→S1); a bare-ID
  //                            clear could otherwise nuke the wrong one.

  String s = message;
  s.trim();
  if (s.startsWith("M") || s.startsWith("m")) s = s.substring(1);

  int colon = s.indexOf(':');

  // ---- Bare ID: clear every slot with this ID ----
  if (colon < 0) {
    int maestroID = s.toInt();
    if (maestroID < 1 || maestroID > 9) {
      Serial.printf("Invalid Maestro ID. Must be M1-M9\n");
      return;
    }
    int cleared = 0;
    for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
      if (maestroConfigs[i].configured && maestroConfigs[i].maestroID == maestroID) {
        _clearMaestroSlot(i);
        cleared++;
      }
    }
    if (cleared == 0) {
      Serial.printf("Maestro ID %d is not configured\n", maestroID);
    } else {
      saveMaestroSettings();
      Serial.printf("Cleared Maestro ID %d (%d slot%s)\n",
                    maestroID, cleared, cleared == 1 ? "" : "s");
    }
    return;
  }

  // ---- Targeted: M<id>:W<wcb>S<port> (clear exactly one slot) ----
  int maestroID = s.substring(0, colon).toInt();
  String tgt = s.substring(colon + 1);
  tgt.toUpperCase();
  int wi = tgt.indexOf('W');
  int si = tgt.indexOf('S');
  if (maestroID < 1 || maestroID > 9 || wi < 0 || si < 0 || si <= wi) {
    Serial.printf("Invalid CLEAR target '%s'. Use M<id>:W<wcb>S<port> or M<id>\n",
                  message.c_str());
    return;
  }
  int wcb  = tgt.substring(wi + 1, si).toInt();
  int port = tgt.substring(si + 1).toInt();   // toInt() stops at any trailing ':baud'

  // Mirror how configureMaestro keys slots: local → (id, port, 0);
  // remote → (id, 0, wcb).
  uint8_t keyRemote = (wcb == WCB_Number) ? 0 : (uint8_t)wcb;
  uint8_t keyPort   = (wcb == WCB_Number) ? (uint8_t)port : 0;
  int8_t slot = findSlotByMaestroIDPortTarget(maestroID, keyPort, keyRemote);

  if (slot < 0) {
    Serial.printf("Maestro M%d:W%dS%d is not configured\n", maestroID, wcb, port);
    return;
  }
  _clearMaestroSlot(slot);
  saveMaestroSettings();
  Serial.printf("Cleared Maestro M%d:W%dS%d (freed slot %d)\n",
                maestroID, wcb, port, slot + 1);
}


void clearAllMaestroConfigs() {
  // Format: ?MAESTRO_DEFAULT
  
  // Track which ports need broadcast re-enabling
  bool portsUsed[5] = {false, false, false, false, false};
  
  // Identify which ports were being used
  for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
    if (maestroConfigs[i].configured && maestroConfigs[i].serialPort > 0) {
      portsUsed[maestroConfigs[i].serialPort - 1] = true;
    }
  }
  
  // Clear all configs
  for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
    maestroConfigs[i].configured = false;
    maestroConfigs[i].maestroID = 0;
    maestroConfigs[i].serialPort = 0;
    maestroConfigs[i].remoteWCB = 0;
  }
  
  saveMaestroSettings();
  Serial.println("All Maestro configurations cleared");
  Serial.println("Reverted to legacy routing (Maestro ID = WCB Number on S1)");
  
  // **AUTO RE-ENABLE BROADCAST ON ALL FREED PORTS**
  Serial.println("\nRe-enabling broadcasts on freed ports:");
  for (int i = 0; i < 5; i++) {
    if (portsUsed[i]) {
      // Re-enable output
      if (!serialBroadcastEnabled[i]) {
        serialBroadcastEnabled[i] = true;
        Serial.printf("  ✓ S%d output enabled\n", i + 1);
      }
      
      // Re-enable input
      if (blockBroadcastFrom[i]) {
        blockBroadcastFrom[i] = false;
        Serial.printf("  ✓ S%d input enabled\n", i + 1);
      }
    }
    // **RESET BAUD RATES ON ALL FREED PORTS**
    Serial.println("Resetting baud rates on freed ports:");
    for (int i = 0; i < 5; i++) {
        if (portsUsed[i]) {
            updateBaudRate(i + 1, 9600);
            Serial.printf("  ✓ S%d baud rate reset to 9600\n", i + 1);
        }
    }
  }
  
  saveBroadcastSettingsToPreferences();
  saveBroadcastBlockSettings();
}

void printMaestroBackup(String &chainedConfig, String &chainedConfigDefault, char delimiter, bool printToSerial) {
    bool anyActive = false;
    for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
        if (maestroConfigs[i].configured) {
            anyActive = true;
            break;
        }
    }
    if (!anyActive) return;

    for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
        if (!maestroConfigs[i].configured) continue;

        String cmd = "?MAESTRO,M" + String(maestroConfigs[i].maestroID);

        if (maestroConfigs[i].serialPort > 0) {
            cmd += ":W" + String(WCB_Number) +
                   "S" + String(maestroConfigs[i].serialPort) +
                   ":" + String(maestroConfigs[i].baudRate);
        } else {
            // Remote — look up actual port from kyberTargets
            int targetPort = 1;  // fallback
            for (int j = 0; j < MAX_KYBER_TARGETS; j++) {
                if (kyberTargets[j].enabled &&
                    kyberTargets[j].maestroID == maestroConfigs[i].maestroID) {
                    targetPort = kyberTargets[j].targetPort;
                    break;
                }
            }
            cmd += ":W" + String(maestroConfigs[i].remoteWCB) +
                   "S" + String(targetPort) +
                   ":" + String(maestroConfigs[i].baudRate);
        }

        if (printToSerial) Serial.println(cmd);
        chainedConfig += String(delimiter) + cmd;
        chainedConfigDefault += "^" + cmd;
    }
}




