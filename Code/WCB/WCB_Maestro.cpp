#include "WCB_Maestro.h"

extern bool maestroEnabled;
extern int WCB_Number;
extern bool lastReceivedViaESPNOW;
extern bool debugEnabled;
extern void sendESPNowMessage(uint8_t target, const char *message);

// Optimized Maestro command function with better performance and validation
void sendMaestroCommand(uint8_t maestroID, uint8_t scriptNumber) {
  if (debugEnabled) {
    Serial.printf("Maestro command: ID=%d, Script=%d\n", maestroID, scriptNumber);
  }
  
  // Validate input parameters
  if (scriptNumber == 0) {
    Serial.println("Warning: Script number 0 may not be valid for Maestro");
  }
  
  // Pre-compute command bytes for performance
  uint8_t command[4] = {0xAA, 0, 0x27, scriptNumber};
  
  // Handle different maestroID cases efficiently
  switch (maestroID) {
    case 0:  // Broadcast to all Maestros
      command[1] = (uint8_t)WCB_Number;
      Serial1.write(command, sizeof(command));
      Serial1.flush();
      if (debugEnabled) {
        Serial.printf("Sent local Maestro command: ID=%d, Script=%d\n", WCB_Number, scriptNumber);
      }
      
      // Also send via ESP-NOW broadcast - optimized message format
      {
        String espnow_message;
        espnow_message.reserve(16);  // Pre-reserve for performance
        espnow_message = ";M9";
        espnow_message += String(scriptNumber);
        
        if (debugEnabled) {
          Serial.printf("Broadcasting ESP-NOW: %s\n", espnow_message.c_str());
        }
        sendESPNowMessage(0, espnow_message.c_str());
      }
      break;
      
    case 9:  // Local maestro (same as WCB_Number)
      command[1] = (uint8_t)WCB_Number;
      Serial1.write(command, sizeof(command));
      Serial1.flush();
      if (debugEnabled) {
        Serial.printf("Sent local Maestro command: ID=%d, Script=%d\n", maestroID, scriptNumber);
      }
      break;
      
    default:  // Specific WCB or local WCB
      if (maestroID == WCB_Number) {
        // Send to local Maestro
        command[1] = (uint8_t)WCB_Number;
        Serial1.write(command, sizeof(command));
        Serial1.flush();
        if (debugEnabled) {
          Serial.printf("Sent local Maestro command: ID=%d, Script=%d\n", maestroID, scriptNumber);
        }
      } else if (maestroID >= 1 && maestroID <= 8) {
        // Send to specific remote WCB - optimized message format
        String espnow_message;
        espnow_message.reserve(16);  // Pre-reserve for performance
        espnow_message = ";M";
        espnow_message += String(maestroID);
        espnow_message += String(scriptNumber);
        
        if (debugEnabled) {
          Serial.printf("Sending ESP-NOW to WCB%d: %s\n", maestroID, espnow_message.c_str());
        }
        sendESPNowMessage(maestroID, espnow_message.c_str());
      } else {
        Serial.printf("Invalid Maestro ID: %d (valid range: 0-9)\n", maestroID);
      }
      break;
  }
  
  // Performance tracking
  #ifdef PERFORMANCE_MODE_ENABLED
  static int maestroCommandsSent = 0;
  maestroCommandsSent++;
  if (debugEnabled && (maestroCommandsSent % 10 == 0)) {
    Serial.printf("Maestro commands sent: %d\n", maestroCommandsSent);
  }
  #endif
}