#ifndef WCB_STORAGE_H
#define WCB_STORAGE_H

#include <Arduino.h>
#include <Preferences.h>

// =============== Global Variables ===============
extern Preferences preferences;

// These are declared elsewhere in the main code, but we need them here as externs
extern uint8_t umac_oct2;
extern uint8_t umac_oct3;
extern int WCB_Number;
extern int Default_WCB_Quantity;
extern char espnowPassword[40];
extern bool debugEnabled;
extern bool serialBroadcastEnabled[5];
extern int baudRates[5];
extern char commandDelimiter;

// For stored commands
#define MAX_STORED_COMMANDS 50
extern String storedCommands[MAX_STORED_COMMANDS];
extern void enqueueCommand(const String &cmd, int sourceID);  // Declare it as an external function
extern void parseCommandsAndEnqueue(const String &data, int sourceID);

// =============== Function Declarations ===============

void saveMACPreferences();
void loadMACPreferences();

void loadWCBNumberFromPreferences();
void saveWCBNumberToPreferences(int wcb_number_f);
void loadWCBQuantitiesFromPreferences();
void saveWCBQuantityPreferences(int quantity);

void updateBaudRate(int port, int baud);
void loadBaudRatesFromPreferences();
void printBaudRates();

void recallCommandSlot(int slot, int sourceID);
void loadStoredCommandsFromPreferences();
void saveStoredCommandsToPreferences();
void clearAllStoredCommands();

void saveBroadcastSettingsToPreferences();
void loadBroadcastSettingsFromPreferences();

void loadLocalFunctionIdentifierAndCommandCharacter();
void saveLocalFunctionIdentifierAndCommandCharacter();

void loadESPNowPasswordFromPreferences();
void setESPNowPassword(const char *password);

void setCommandDelimiter(char delimiter);
void loadCommandDelimiter();

void eraseNVSFlash();

#endif
