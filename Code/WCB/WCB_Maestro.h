#ifndef WCB_MAESTRO_H
#define WCB_MAESTRO_H
#include <Arduino.h>

// Maestro configuration structure
struct MaestroConfig {
  uint8_t maestroID;
  uint8_t serialPort;
  uint8_t remoteWCB;
  bool configured;
  uint32_t baudRate;    // ADD THIS
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
void printMaestroBackup(String &chainedConfig, String &chainedConfigDefault, char delimiter);
#endif