#ifndef WCB_STORAGE_H
#define WCB_STORAGE_H

#include <Arduino.h>
#include <Preferences.h>

// Declare the preferences object
extern Preferences preferences;

// Declare global variables used in preferences
extern uint8_t umac_oct2;
extern uint8_t umac_oct3;
extern int Default_WCB_Quantity;
extern char espnowPassword[40];
extern int baudRates[5];
extern bool serialBroadcastEnabled[5];
extern char commandDelimiter;
// extern String storedCommands[];
// extern int MAX_STORED_COMMANDS;
#define MAX_STORED_COMMANDS 50

extern String storedCommands[MAX_STORED_COMMANDS];

// Function declarations
void loadMACPreferences();
void saveMACPreferences();
void loadWCBNumberFromPreferences();
void saveWCBQuantityPreferences(int wcbq);
void loadWCBQuantitiesFromPreferences();
void loadBaudRatesFromPreferences();
void loadBroadcastSettingsFromPreferences();
void saveBroadcastSettingsToPreferences();
void loadESPNowPasswordFromPreferences();
void setESPNowPassword(const char *password);
void loadCommandDelimiter();
void setCommandDelimiter(char c);
void loadStoredCommandsFromPreferences();
void saveStoredCommandsToPreferences();
void clearAllStoredCommands();
void eraseNVSFlash();

#endif  // WCB_STORAGE_H
