// #include <sys/_types.h>
// #pragma once

// #include <Arduino.h>
// #include <Adafruit_NeoPixel.h>
// // #include <SoftwareSerial.h>

// #include <esp_now.h>
// #include <WiFi.h>
// #include <Preferences.h>
// #include <esp_wifi.h>
// #include "WCB_Storage.h"


// // // ============================= External Variables =============================
// extern int STATUS_LED_COUNT;
// extern bool debugEnabled;
// extern int wcb_hw_version;
// extern int ONBOARD_LED;

// const uint32_t red     = 0xFF0000;
// const uint32_t orange  = 0xFF8000;
// const uint32_t yellow  = 0xFFFF00;
// const uint32_t green   = 0x00FF00;
// const uint32_t cyan    = 0x00FFFF;
// const uint32_t blue    = 0x0000FF;
// const uint32_t magenta = 0xFF00FF;
// const uint32_t white   = 0xFFFFFF;
// const uint32_t off     = 0x000000;

// const uint32_t basicColors[9] = {off, red, yellow, green, cyan, blue, magenta, orange, white};

// extern int SERIAL1_TX_PIN;        //  // Serial 1 Tx Pin
// extern int SERIAL1_RX_PIN;       //  // Serial 1 Rx Pin
// extern int SERIAL2_TX_PIN;       //  // Serial 2 Tx Pin
// extern int SERIAL2_RX_PIN;	     //  // Serial 2 Rx Pin
// extern int SERIAL3_TX_PIN;       //  // Serial 3 Tx Pin
// extern int SERIAL3_RX_PIN;       //  // Serial 3 Rx Pin
// extern int SERIAL4_TX_PIN;      //  // Serial 4 Tx Pin 
// extern int SERIAL4_RX_PIN;      //  // Serial 4 Rx Pin
// extern int SERIAL5_TX_PIN;	     //  // Serial 5 Tx Pin
// extern int SERIAL5_RX_PIN;	     //  // Serial 5 Rx Pin
// extern int ONBOARD_LED;       //  // ESP32 Status NeoPixel Pin
// extern int STATUS_LED_PIN;       //  // Not used on this board but defining it to match version 2.1 

// // extern SoftwareSerial Serial3(SERIAL3_RX_PIN, SERIAL3_TX_PIN);
// // extern SoftwareSerial Serial4(SERIAL4_RX_PIN, SERIAL4_TX_PIN);
// // extern SoftwareSerial Serial5(SERIAL5_RX_PIN, SERIAL5_TX_PIN);
// extern char LocalFunctionIdentifier;
// extern char CommandCharacter;
// extern int WCB_Number;
// extern int Default_WCB_Quantity;
// extern bool serialBroadcastEnabled[5];
// // extern int baudRates[5];
// extern char espnowPassword[40];
// extern char commandDelimiter;
// extern Preferences preferences;
// extern bool lastReceivedViaESPNOW;
// extern String SoftwareVersion;


// extern Adafruit_NeoPixel *statusLED;


// // Helper Functions
// void reboot();


// //LED Functions
// void colorWipeStatus(String statusled1, uint32_t c, int brightness);
// void turnOnLEDESPNOW();
// void turnOnLEDforBoot();
// void turnOnLEDSerialOut();
// void turnOffLED();
// String getBoardHostname();
// void printConfigInfo();


// // // ============================= Function Declarations =============================
// // // Serial Processing
// // void processIncomingSerial(Stream &serial, int sourceID);
// // void processBroadcastCommand(const String &cmd, int sourceID);
// void writeSerialString(Stream &serialPort, String stringData);
// Stream &getSerialStream(int port);


// // void handleSingleCommand(String cmd, int sourceID);

// // void processLocalCommand(const String &message);
// // void updateLocalFunctionIdentifier(const String &message);
// // void updateCommandCharacter(const String &message);
// // void storeCommand(const String &message);
// // void updateCommandDelimiter(const String &message);
// // void update2ndMACOctet(const String &message);
// // void update3rdMACOctet(const String &message);
// // void updateWCBQuantity(const String &message);
// // void updateWCBNumber(const String &message);
// // void updateESPNowPassword(const String &message);
// // void updateSerialSettings(const String &message);
// // void enableMaestroSerialBaudRate();
// // void disableMaestroSerialBaudRate();
// // void updateHWVersion(const String &message);

// // void processCommandCharcter(const String &message, int sourceID);
// // void processSerialMessage(const String &message);
// // void processMaestroCommand(const String &message);
// // void recallStoredCommand(const String &message, int sourceID);
// // void processMaestroCommand(const String &message);

// // void processBroadcastCommand(const String &cmd, int sourceID);

// // // WCB Communication
// // void sendESPNowMessage(uint8_t target, const char *message);
// // void sendESPNowRaw(const uint8_t *data, size_t len);
// // void espNowReceiveCallback(const esp_now_recv_info_t *info, const uint8_t *incomingData, int len);

// // // Command Handling
// // void handleSingleCommand(String cmd, int sourceID);
// // void processLocalCommand(const String &message);
// // void processCommandCharacter(const String &message, int sourceID);

// // // Utility Functions

// // void enqueueCommand(const String &cmd, int sourceID);
// // void reboot();
// // void colorWipeStatus(String statusled, uint32_t c, int brightness);
