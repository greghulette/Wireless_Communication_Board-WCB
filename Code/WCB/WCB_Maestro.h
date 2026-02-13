#ifndef WCB_MAESTRO_H
#define WCB_MAESTRO_H
#include <Arduino.h>

// Maestro configuration structure
struct MaestroConfig {
  uint8_t maestroID;      // The ID of this Maestro (1-9, 0 = unused slot)
  uint8_t serialPort;     // Local serial port (1-5, 0 = not local)
  uint8_t remoteWCB;      // Remote WCB number (1-9, 0 = local only)
  bool configured;        // TRUE = in use, FALSE = unused
};

#define MAX_MAESTROS_PER_WCB 9  // Can track all 9 Maestros in the system

extern MaestroConfig maestroConfigs[MAX_MAESTROS_PER_WCB];
extern bool maestroEnabled;
extern bool lastReceivedViaESPNOW;

// Core Maestro functions
void sendMaestroCommand(uint8_t maestroID, uint8_t scriptNumber);

// Configuration functions
void configureMaestro(const String &message);
void clearMaestroByID(const String &message);
void clearAllMaestroConfigs();

// Helper functions
int8_t findSlotByMaestroID(uint8_t maestroID);
int8_t findEmptySlot();
bool isMaestroConfigured(uint8_t maestroID);

#endif