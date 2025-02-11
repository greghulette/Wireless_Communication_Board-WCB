// #pragma once

// #include <Arduino.h>
// #include <esp_now.h>
// #include <WiFi.h>
// #include <Preferences.h>

// // ============================= External Variables =============================
// extern bool debugEnabled;
// extern char LocalFunctionIdentifier;
// extern char CommandCharacter;
// extern int WCB_Number;
// extern int Default_WCB_Quantity;
// extern bool serialBroadcastEnabled[5];
// extern int baudRates[5];
// extern char espnowPassword[40];
// extern char commandDelimiter;
// extern Preferences preferences;
// extern bool lastReceivedViaESPNOW;

// // ============================= Function Declarations =============================
// // Serial Processing
// void processIncomingSerial(Stream &serial, int sourceID);
// void processBroadcastCommand(const String &cmd, int sourceID);
// void writeSerialString(Stream &serialPort, String stringData);
// Stream &getSerialStream(int port);


// void handleSingleCommand(String cmd, int sourceID);

// void processLocalCommand(const String &message);
// void updateLocalFunctionIdentifier(const String &message);
// void updateCommandCharacter(const String &message);
// void storeCommand(const String &message);
// void updateCommandDelimiter(const String &message);
// void update2ndMACOctet(const String &message);
// void update3rdMACOctet(const String &message);
// void updateWCBQuantity(const String &message);
// void updateWCBNumber(const String &message);
// void updateESPNowPassword(const String &message);
// void updateSerialSettings(const String &message);
// void enableMaestroSerialBaudRate();
// void disableMaestroSerialBaudRate();
// void updateHWVersion(const String &message);

// void processCommandCharcter(const String &message, int sourceID);
// void processSerialMessage(const String &message);
// void processMaestroCommand(const String &message);
// void recallStoredCommand(const String &message, int sourceID);
// void processMaestroCommand(const String &message);

// void processBroadcastCommand(const String &cmd, int sourceID);

// // WCB Communication
// void sendESPNowMessage(uint8_t target, const char *message);
// void sendESPNowRaw(const uint8_t *data, size_t len);
// void espNowReceiveCallback(const esp_now_recv_info_t *info, const uint8_t *incomingData, int len);

// // Command Handling
// void handleSingleCommand(String cmd, int sourceID);
// void processLocalCommand(const String &message);
// void processCommandCharacter(const String &message, int sourceID);

// // Utility Functions

// void enqueueCommand(const String &cmd, int sourceID);
// void reboot();
// void colorWipeStatus(String statusled, uint32_t c, int brightness);
