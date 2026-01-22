#include <sys/_types.h>
#ifndef WCB_STORAGE_H
#define WCB_STORAGE_H

#include <Arduino.h>
#include <Preferences.h>
#include "command_timer_queue.h"

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
extern unsigned long baudRates[5];
extern char CommandCharacter;
extern char commandDelimiter;
extern char LocalFunctionIdentifier;
extern int wcb_hw_version;
extern int storedBaudRate[6];
extern bool Kyber_Remote;
extern bool Kyber_Local;
extern String Kyber_Location;
extern String serialPortLabels[5];
String getSerialLabel(int port);


// For stored commands
#define MAX_STORED_COMMANDS 80
#define MAX_SERIAL_MONITOR_MAPPINGS 5  // One per input port

extern String storedCommands[MAX_STORED_COMMANDS];

extern void enqueueCommand(const String &cmd, int sourceID);  // Declare it as an external function
extern void parseCommandsAndEnqueue(const String &data, int sourceID);

struct SerialMonitorOutput {
    uint8_t wcbNumber;    // 0 = local, 1-9 = remote WCB
    uint8_t serialPort;   // 0 = USB, 1-5 = Serial ports
};

struct SerialMonitorMapping {
    bool active;
    uint8_t inputPort;    // 1-5 (which local serial port to monitor)
    uint8_t outputCount;
    SerialMonitorOutput outputs[10];  // Up to 10 output destinations
};

extern SerialMonitorMapping serialMonitorMappings[MAX_SERIAL_MONITOR_MAPPINGS];

// =============== Function Declarations ===============
void saveHWversion(int wcb_hw_version_f);
void loadHWversion();
void printHWversion();
void saveMACPreferences();
void loadMACPreferences();

void loadWCBNumberFromPreferences();
void saveWCBNumberToPreferences(int wcb_number_f);
void loadWCBQuantitiesFromPreferences();
void saveWCBQuantityPreferences(int quantity);

void updateBaudRate(int port, int baud);
void loadBaudRatesFromPreferences();
void resetBroadcastSettingsNamespace();
void printBaudRates();
void recallBaudRatefromSerial(int ser);
void setBaudRateForSerial(int ser);
bool isTimerCommand(const String &input);

void recallCommandSlot(const String &key, int sourceID);
void loadStoredCommandsFromPreferences();
void saveStoredCommandsToPreferences(const String &message);
void listStoredCommands();
void eraseStoredCommandByName(const String &name);

void clearAllStoredCommands();

void storeKyberSettings(const String &message);
void loadKyberSettings();
void printKyberSettings();

void saveBroadcastSettingsToPreferences();
void loadBroadcastSettingsFromPreferences();

void loadLocalFunctionIdentifierAndCommandCharacter();
void saveLocalFunctionIdentifierAndCommandCharacter();

void loadESPNowPasswordFromPreferences();
void setESPNowPassword(const char *password);

void setCommandDelimiter(char delimiter);
void loadCommandDelimiter();

void eraseNVSFlash();

extern bool isSerialPortUsedForPWMInput(int port);
extern bool isSerialPortPWMOutput(int port);

void loadSerialLabelsFromPreferences();
void saveSerialLabelToPreferences(int port, const String &label);
void clearSerialLabel(int port);

// Serial monitoring
extern bool serialMonitorEnabled[5];
extern bool mirrorToUSB;
extern bool mirrorToKyber;

// Broadcast blocking
extern bool blockBroadcastFrom[5];

void loadSerialMonitorSettings();
void saveSerialMonitorSettings();
void loadBroadcastBlockSettings();
void saveBroadcastBlockSettings();

void addSerialMonitorMapping(const String &message);
void removeSerialMonitorMapping(int port);
void clearAllSerialMonitorMappings();
void listSerialMonitorMappings();
void saveSerialMonitorMappings();
void loadSerialMonitorMappings();
bool isSerialPortMonitored(int port); 

#endif