#include "WCB_Maestro.h"
#include "WCB_Storage.h"

extern bool maestroEnabled;
extern int WCB_Number;
extern int Default_WCB_Quantity;
extern bool lastReceivedViaESPNOW;
extern bool debugEnabled;
extern char LocalFunctionIdentifier;
extern unsigned long baudRates[5];
extern void sendESPNowMessage(uint8_t target, const char *message);
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
  
  // ========== STEP 1: Check if configured ==========
  int8_t slot = findSlotByMaestroID(maestroID);
  
  if (slot >= 0) {
    MaestroConfig &config = maestroConfigs[slot];
    
    // LOCAL Maestro
    if (config.serialPort > 0) {
      uint8_t command[] = {0xAA, maestroID, 0x27, scriptNumber};
      Stream &targetSerial = getSerialStream(config.serialPort);
      targetSerial.write(command, sizeof(command));
      targetSerial.flush();
      
      // ALWAYS show local sends
      Serial.printf("→ Maestro %d: Local S%d, Script %d\n", 
                    maestroID, config.serialPort, scriptNumber);
      return;
    }
    
    // REMOTE Maestro
    if (config.remoteWCB > 0) {
      String espnowMsg = ";M" + String(maestroID) + String(scriptNumber);
      sendESPNowMessage(config.remoteWCB, espnowMsg.c_str());
      
      // ALWAYS show unicast sends
      Serial.printf("→ Maestro %d: Unicast to WCB%d, Script %d\n", 
                    maestroID, config.remoteWCB, scriptNumber);
      return;
    }
    
    // No destination
    Serial.printf("⚠️  Maestro %d configured but has no destination\n", maestroID);
    return;
  }
  
  // ========== STEP 2: Broadcast (maestroID == 0) ==========
  if (maestroID == 0) {
    // Send to all LOCAL Maestros
    for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
      if (maestroConfigs[i].configured && maestroConfigs[i].serialPort > 0) {
        uint8_t command[] = {0xAA, maestroConfigs[i].maestroID, 0x27, scriptNumber};
        Stream &targetSerial = getSerialStream(maestroConfigs[i].serialPort);
        targetSerial.write(command, sizeof(command));
        targetSerial.flush();
      }
    }
    
    // LEGACY fallback
    if (!isMaestroConfigured(WCB_Number)) {
      uint8_t command[] = {0xAA, WCB_Number, 0x27, scriptNumber};
      Serial1.write(command, sizeof(command));
      Serial1.flush();
    }
    
    // Broadcast via ESP-NOW
    String espnowMsg = ";M0" + String(scriptNumber);
    sendESPNowMessage(0, espnowMsg.c_str());
    
    // ALWAYS show broadcast
    Serial.printf("→ Maestro Broadcast: Script %d\n", scriptNumber);
    return;
  }
  
  // ========== STEP 3: Legacy Behavior ==========
  if (maestroID == WCB_Number || maestroID == 9) {
    uint8_t command[] = {0xAA, (maestroID == 9) ? WCB_Number : maestroID, 0x27, scriptNumber};
    Serial1.write(command, sizeof(command));
    Serial1.flush();
    
    // ALWAYS show legacy sends
    Serial.printf("→ Maestro %d: Legacy S1, Script %d\n", maestroID, scriptNumber);
    return;
  }
  
  // ========== STEP 4: Unconfigured Fallback ==========
  if (maestroID >= 1 && maestroID <= Default_WCB_Quantity) {
    String espnowMsg = ";M" + String(maestroID) + String(scriptNumber);
    sendESPNowMessage(maestroID, espnowMsg.c_str());
    
    // ALWAYS show fallback unicast
    Serial.printf("→ Maestro %d: Fallback unicast to WCB%d, Script %d\n", 
                  maestroID, maestroID, scriptNumber);
  } else {
    String espnowMsg = ";M" + String(maestroID) + String(scriptNumber);
    sendESPNowMessage(0, espnowMsg.c_str());
    
    // ALWAYS show fallback broadcast
    Serial.printf("→ Maestro %d: Fallback broadcast, Script %d\n", 
                  maestroID, scriptNumber);
  }
}

// ==================== Configuration Functions ====================
void configureMaestro(const String &message) {
  // Format: ?MAESTRO,M<maestroID>:W<wcb>S<port>:<baud>
  // Example: ?MAESTRO,M2:W2S1:57600
  // Chained: ?MAESTRO,M1:W1S2:115200,M2:W2S1:57600,M3:W3S3:115200
  
  // Check if chained (multiple Maestros)
  int firstComma = message.indexOf(',');
  if (firstComma == -1) {
    Serial.printf("Invalid format. Use: %cMAESTRO,M<maestroID>:W<wcb>S<port>:<baud>\n", LocalFunctionIdentifier);
    Serial.printf("Example: %cMAESTRO,M2:W2S1:57600\n", LocalFunctionIdentifier);
    return;
  }
  
  String remaining = message.substring(firstComma + 1);
  remaining.trim();
  
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
    
    // Find or create slot
    int8_t slot = findSlotByMaestroID(maestroID);
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
      
      saveMaestroSettings();
      Serial.printf("✓ Maestro %d: Local S%d at %d baud (slot %d)\n", 
                    maestroID, serialPort, baudRate, slot + 1);
                    
    } else {
      // REMOTE CONFIGURATION
      maestroConfigs[slot].maestroID = maestroID;
      maestroConfigs[slot].serialPort = 0;
      maestroConfigs[slot].remoteWCB = targetWCB;
      maestroConfigs[slot].configured = true;
      
      saveMaestroSettings();
      Serial.printf("✓ Maestro %d: Remote on WCB%d (unicast, slot %d)\n", 
                    maestroID, targetWCB, slot + 1);
    }
    
    startIdx = nextComma + 1;
  }
}

void clearMaestroByID(const String &message) {
  // Format: ?MAESTRO_CLEAR,<maestroID>
  int commaIndex = message.indexOf(',');
  
  if (commaIndex == -1) {
    Serial.printf("Invalid format. Use: %cMAESTRO_CLEAR,<maestroID>\n", LocalFunctionIdentifier);
    Serial.printf("Example: %cMAESTRO_CLEAR,5\n", LocalFunctionIdentifier);
    return;
  }
  
  int maestroID = message.substring(commaIndex + 1).toInt();
  
  if (maestroID < 1 || maestroID > 9) {
    Serial.println("Invalid Maestro ID. Must be 1-9");
    return;
  }
  
  int8_t slot = findSlotByMaestroID(maestroID);
  
  if (slot < 0) {
    Serial.printf("Maestro ID %d is not configured\n", maestroID);
    return;
  }
  
  // **SAVE THE PORT BEFORE CLEARING**
  uint8_t freedPort = maestroConfigs[slot].serialPort;

  maestroConfigs[slot].configured = false;
  maestroConfigs[slot].maestroID = 0;
  maestroConfigs[slot].serialPort = 0;
  maestroConfigs[slot].remoteWCB = 0;

  saveMaestroSettings();
  Serial.printf("Cleared Maestro ID %d (freed slot %d)\n", maestroID, slot + 1);

  // **AUTO RE-ENABLE BROADCAST ON FREED PORT**
  if (freedPort > 0) {
    // Re-enable output
    if (!serialBroadcastEnabled[freedPort - 1]) {
      serialBroadcastEnabled[freedPort - 1] = true;
      saveBroadcastSettingsToPreferences();
      Serial.printf("✓ Re-enabled broadcast output on S%d\n", freedPort);
    }
    
    // Re-enable input
    if (blockBroadcastFrom[freedPort - 1]) {
      blockBroadcastFrom[freedPort - 1] = false;
      saveBroadcastBlockSettings();
      Serial.printf("✓ Re-enabled broadcast input on S%d\n", freedPort);
    }
}
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
  }
  
  saveBroadcastSettingsToPreferences();
  saveBroadcastBlockSettings();
}

