#include <Arduino.h>
#include <esp_wifi.h>
#include <esp_now.h>
#include <WiFi.h>
#include <HardwareSerial.h>
#include <SoftwareSerial.h>
#include <Adafruit_NeoPixel.h>
#include "WCB_Storage.h"
#include "WCB_Maestro.h"
#include "wcb_pin_map.h"
//hello
// Flag to track if last received message was via ESP-NOW
bool lastReceivedViaESPNOW = false;

// Debugging flag (default: off)
bool debugEnabled = false;

// Characters for local functions and commands
char LocalFunctionIdentifier = '?';
char CommandCharacter = ';';
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>


#define SERIAL_TIMEOUT 10
#define BOARD1 // Change to BOARD1 or BOARD2
// #define BOARD2 // Change to BOARD1 or BOARD2



// WCB Board HW and SW version Variables
int wcb_hw_version = 0;  // Default = 0, Version 2.1 = 21, Version 2.3 = 23, Version 2.4 = 24
String SoftwareVersion = "5.0";

// Uncomment only the board that you are loading this sketch onto.
#define WCB1
// #define WCB2
// #define WCB3
// #define WCB4
// #define WCB5
// #define WCB6
// #define WCB7
// #define WCB8
// #define WCB9

int WCB_Number = 1;  // Default to WCB1

bool maestroEnabled = false;
bool Kyber = true;

Preferences preferences;  // Allows you to store information that persists after reboot and after reloading of sketch

#define STATUS_LED_PIN 19 // ESP32 Status NeoPixel Pin
#define STATUS_LED_COUNT 1

const uint32_t red     = 0xFF0000;
const uint32_t orange  = 0xFF8000;
const uint32_t yellow  = 0xFFFF00;
const uint32_t green   = 0x00FF00;
const uint32_t cyan    = 0x00FFFF;
const uint32_t blue    = 0x0000FF;
const uint32_t magenta = 0xFF00FF;
const uint32_t white   = 0xFFFFFF;
const uint32_t off     = 0x000000;

const uint32_t basicColors[9] = {off, red, yellow, green, cyan, blue, magenta, orange, white};

Adafruit_NeoPixel statusLED(STATUS_LED_COUNT, STATUS_LED_PIN, NEO_GRB + NEO_KHZ800);

// Serial port pin assignments
#define SERIAL1_TX_PIN 8
#define SERIAL1_RX_PIN 21
#define SERIAL2_TX_PIN 20
#define SERIAL2_RX_PIN 7
#define SERIAL3_TX_PIN 25
#define SERIAL3_RX_PIN 4
#define SERIAL4_TX_PIN 14
#define SERIAL4_RX_PIN 27
#define SERIAL5_TX_PIN 13
#define SERIAL5_RX_PIN 26

// Default baud rates
#define SERIAL1_DEFAULT_BAUD_RATE 9600
#define SERIAL2_DEFAULT_BAUD_RATE 9600
#define SERIAL3_DEFAULT_BAUD_RATE 9600
#define SERIAL4_DEFAULT_BAUD_RATE 9600
#define SERIAL5_DEFAULT_BAUD_RATE 9600

// Broadcast enabled settings for each serial port (modifiable)
bool serialBroadcastEnabled[5] = {true, true, true, true, true};

// Current baud rates (modifiable)
int baudRates[5] = {
  SERIAL1_DEFAULT_BAUD_RATE,
  SERIAL2_DEFAULT_BAUD_RATE,
  SERIAL3_DEFAULT_BAUD_RATE,
  SERIAL4_DEFAULT_BAUD_RATE,
  SERIAL5_DEFAULT_BAUD_RATE
};

// Number of WCB boards in the system
int Default_WCB_Quantity = 1; // User-defined

// MAC Octets
uint8_t umac_oct2 = 0x00;
uint8_t umac_oct3 = 0x00;

// Simple reboot helper
void reboot(){
  Serial.println("Rebooting in 2 seconds");
  delay(2000);
  ESP.restart();
}

// Define WCB MAC addresses
uint8_t WCBMacAddresses[9][6];
uint8_t broadcastMACAddress[1][6]; // Will be updated dynamically

// ESP-NOW message struct
typedef struct espnow_struct_message {
  char structPassword[40];
  char structSenderID[4];
  char structTargetID[4];
  bool structCommandIncluded;
  char structCommand[200];
} espnow_struct_message;

// ESP-NOW messages
espnow_struct_message commandsToSend[10]; // Includes 1-9 and broadcast
espnow_struct_message commandsToReceive[10];

// SoftwareSerial objects
SoftwareSerial Serial3(SERIAL3_RX_PIN, SERIAL3_TX_PIN);
SoftwareSerial Serial4(SERIAL4_RX_PIN, SERIAL4_TX_PIN);
SoftwareSerial Serial5(SERIAL5_RX_PIN, SERIAL5_TX_PIN);

// User-defined ESP-NOW password
char espnowPassword[40] = "CHANGE_ME_PLEASE_OR_RISK_TAKEOVER";

// Delimiter character (default '^')
char commandDelimiter = '^';

// ============================= FreeRTOS Queue Setup =============================
typedef struct {
  char *cmd;     // Dynamically allocated
  int sourceID;  // 1..5 for serial, or 0 if we want to designate ESP-NOW or WCBX
} CommandQueueItem;

static QueueHandle_t commandQueue = nullptr;

// ============================= Stored Commands =============================
#define MAX_STORED_COMMANDS 50
String storedCommands[MAX_STORED_COMMANDS];

// ============================= Forward Declarations =============================
void writeSerialString(Stream &serialPort, String stringData);
void sendESPNowMessage(uint8_t target, const char *message);
void handleSingleCommand(const String &cmd, int sourceID);
void enqueueCommand(const String &cmd, int sourceID);

// ============================= Setup & Loop =============================

// Helper to set a color on the status LED
void setLEDColor(const String &colorName) {
  static const uint32_t off=0x000000,red=0xFF0000,orange=0xFF8000,yellow=0xFFFF00,green=0x00FF00,cyan=0x00FFFF,blue=0x0000FF,magenta=0xFF00FF,white=0xFFFFFF;
  uint32_t color = off;

  if (colorName.equalsIgnoreCase("red")) color = red;
  else if (colorName.equalsIgnoreCase("orange")) color = orange;
  else if (colorName.equalsIgnoreCase("yellow")) color = yellow;
  else if (colorName.equalsIgnoreCase("green")) color = green;
  else if (colorName.equalsIgnoreCase("cyan")) color = cyan;
  else if (colorName.equalsIgnoreCase("blue")) color = blue;
  else if (colorName.equalsIgnoreCase("magenta")) color = magenta;
  else if (colorName.equalsIgnoreCase("white")) color = white;

  statusLED.fill(color);
  statusLED.show();
}

// Write a string + `\r` to a given Stream
void writeSerialString(Stream &serialPort, String stringData) {
  String completeString = stringData + '\r';
  for (int i = 0; i < completeString.length(); i++) {
    serialPort.write(completeString[i]);
  }
}

Stream &getSerialStream(int port) {
  switch (port) {
    case 1: return Serial1;
    case 2: return Serial2;
    case 3: return Serial3;
    case 4: return Serial4;
    case 5: return Serial5;
    default: return Serial; // fallback
  }
}

// Enqueue commands for asynchronous processing
void enqueueCommand(const String &cmd, int sourceID) {
  if (!commandQueue) return;

  CommandQueueItem item;
  item.sourceID = sourceID;

  // Allocate enough space for the command (including null terminator)
  int length = cmd.length() + 1;
  item.cmd = (char *)malloc(length);
  if (!item.cmd) {
    Serial.println("Out of memory while enqueuing command!");
    return;
  }

  strncpy(item.cmd, cmd.c_str(), length);
  item.cmd[length - 1] = '\0';

  // Attempt to enqueue
  if (xQueueSend(commandQueue, &item, 0) != pdTRUE) {
    // If queue is full, free memory
    Serial.println("Command queue is full! Discarding command.");
    free(item.cmd);
  }
}

// parseCommandsAndEnqueue used by recallCommandSlot
void parseCommandsAndEnqueue(const String &data, int sourceID) {
  int startIdx = 0;
  while (true) {
    int delimPos = data.indexOf(commandDelimiter, startIdx);
    if (delimPos == -1) {
      String singleCmd = data.substring(startIdx);
      singleCmd.trim();
      if (!singleCmd.isEmpty()) {
        enqueueCommand(singleCmd, sourceID);
      }
      break;
    } else {
      String singleCmd = data.substring(startIdx, delimPos);
      singleCmd.trim();
      if (!singleCmd.isEmpty()) {
        enqueueCommand(singleCmd, sourceID);
      }
      startIdx = delimPos + 1;
    }
  }
}

String getBoardHostname() {
  return "Wireless Communication Board " + String(WCB_Number) + " (W" + String(WCB_Number) + ")";
}

// Print all config info
void printConfigInfo() {
  String hostname = getBoardHostname();
  Serial.println("\n\n----------- Configuration Info ------------\n");
  Serial.printf("Hostname: %s\n", hostname.c_str());
  printHWversion();
  Serial.printf("Software Version %s\n", SoftwareVersion);

  Serial.println("Serial Baud Rates and Broadcast Settings:");
  for (int i = 0; i < 5; i++) {
    Serial.printf("  Serial%d Baud Rate: %d,  Broadcast: %s\n",
                  i + 1, baudRates[i], serialBroadcastEnabled[i] ? "Enabled" : "Disabled");
  }
  Serial.printf("LocalFunctionIdentifier: '%c'\n", LocalFunctionIdentifier);
  Serial.printf("CommandCharacter: '%c'\n", CommandCharacter);

  // Print ESP-NOW password
  Serial.printf("ESP-NOW Password: %s\n", espnowPassword);

  // Print MAC address used for ESP-NOW (station MAC)
  uint8_t baseMac[6];
  esp_wifi_get_mac(WIFI_IF_STA, baseMac);
  Serial.printf("ESP-NOW (STA) MAC Address: %02X:%02X:%02X:%02X:%02X:%02X\n",
                baseMac[0], baseMac[1], baseMac[2], baseMac[3], baseMac[4], baseMac[5]);

  // Print all stored commands
  Serial.println("Stored Commands:");
  for (int i = 0; i < MAX_STORED_COMMANDS; i++) {
    if (storedCommands[i].length() > 0) {
      Serial.printf("  Stored Sequence: %d: %s\n", i + 1, storedCommands[i].c_str());
    }
  }
  Serial.println("--- End of Configuration Info ---\n");
}

// Send an ESP-NOW message (unicast or broadcast)
void sendESPNowMessage(uint8_t target, const char *message) {
    // Skip broadcast if last was from ESP-NOW
    if (target == 0 && lastReceivedViaESPNOW) {
        return;
    }
    lastReceivedViaESPNOW = false; // Reset after check

    // Prepare the struct
    espnow_struct_message msg;
    memset(&msg, 0, sizeof(msg));

    // Fill struct fields
    strncpy(msg.structPassword, espnowPassword, sizeof(msg.structPassword) - 1);
    msg.structPassword[sizeof(msg.structPassword) - 1] = '\0';

    // Send only the **WCB number** instead of "WCBx"
    snprintf(msg.structSenderID, sizeof(msg.structSenderID), "%d", WCB_Number);

    if (target == 0) {
        snprintf(msg.structTargetID, sizeof(msg.structTargetID), "0"); // Use "0" for broadcast
    } else {
        snprintf(msg.structTargetID, sizeof(msg.structTargetID), "%d", target);
    }

    msg.structCommandIncluded = true;
    strncpy(msg.structCommand, message, sizeof(msg.structCommand) - 1);
    msg.structCommand[sizeof(msg.structCommand) - 1] = '\0';

    // Select MAC address
    uint8_t *mac = (target == 0) ? broadcastMACAddress[0] : WCBMacAddresses[target - 1];

    // Debug Output
    Serial.printf("Sending ESP-NOW message to MAC: %02X:%02X:%02X:%02X:%02X:%02X\n",
                  mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    Serial.printf("Sender ID: WCB%s, Target ID: WCB%s, Message: %s\n",
                  msg.structSenderID, msg.structTargetID, msg.structCommand);

    // Send ESP-NOW message
    esp_err_t result = esp_now_send(mac, (uint8_t *)&msg, sizeof(msg));
    if (result == ESP_OK) {
        Serial.println("ESP-NOW message sent successfully!");
    } else {
        Serial.printf("ESP-NOW send failed! Error code: %d\n", result);
    }
}
void sendESPNowRaw(const uint8_t *data, size_t len) {
    size_t offset = 0;
    while (offset < len) {
        size_t chunkSize = len - offset;
        if (chunkSize > 180) chunkSize = 180; // Prevent overflow in structCommand

        // Prepare ESP-NOW message
        espnow_struct_message msg;
        memset(&msg, 0, sizeof(msg));

        strncpy(msg.structPassword, espnowPassword, sizeof(msg.structPassword) - 1);
        msg.structPassword[sizeof(msg.structPassword) - 1] = '\0';
        
        // Mark sender ID as this WCB number
        snprintf(msg.structSenderID, sizeof(msg.structSenderID), "%d", WCB_Number);
        
        // Set Target ID to "0" -> Means raw bridging data
        snprintf(msg.structTargetID, sizeof(msg.structTargetID), "9");

        msg.structCommandIncluded = true;

        // **Store chunk length in first 2 bytes**
        msg.structCommand[0] = (uint8_t)(chunkSize & 0xFF);
        msg.structCommand[1] = (uint8_t)((chunkSize >> 8) & 0xFF);

        // Copy the chunk into structCommand
        memcpy(msg.structCommand + 2, data + offset, chunkSize);

        // Send via ESP-NOW broadcast
        uint8_t *mac = broadcastMACAddress[0];

        esp_err_t result = esp_now_send(mac, (uint8_t*)&msg, sizeof(msg));
        if (result == ESP_OK) {
            if (debugEnabled) {
                // Serial.printf("Sent chunk of %d bytes via ESP-NOW\n", (int)chunkSize);
            }
        } else {
            Serial.printf("ESP-NOW send failed! Error code: %d\n", result);
        }

        offset += chunkSize;
    }
}


void readMaestroComms() {
  while (Serial2.available()) {
    uint8_t d = Serial2.read();
    if (d >= 0) {
      Serial1.write((uint8_t)d);
      // Serial.println(d);
      // delay(2);
    }
  }
}

// We'll read from Serial2 & forward to Serial1 + bridging
void forwardSerial2Data() {
  static uint8_t buffer[64];

  // **Check if Serial2 has data**
  if (Serial2.available() > 0) {
    int len = Serial2.readBytes((char*)buffer, sizeof(buffer));

    // **Forward data to Serial1**
    Serial1.write(buffer, len);
    Serial1.flush(); // Ensure immediate transmission

    // **Forward via ESP-NOW broadcast**
    sendESPNowRaw(buffer, len);
  }
}



// Received messages via ESP-NOW
// void espNowReceiveCallback(const esp_now_recv_info_t *info, const uint8_t *incomingData, int len) {
//     if (len != sizeof(espnow_struct_message)) {
//         if (debugEnabled) {
//             Serial.printf("Received unexpected size: %d (expected %d)\n", len, (int)sizeof(espnow_struct_message));
//         }
//         return;
//     }

//     espnow_struct_message received;
//     memcpy(&received, incomingData, sizeof(received));

//     // Check if it's a bridging message (structTargetID == "0")
//     if (strcmp(received.structTargetID, "9") == 0) {
//         // Extract chunk length from first 2 bytes of structCommand
//         size_t chunkLen = (uint8_t)received.structCommand[0] | ((uint8_t)received.structCommand[1] << 8);

//         // Safety check to prevent invalid length
//         if (chunkLen > 180 || chunkLen == 0) {
//             if (debugEnabled) {
//                 Serial.printf("Invalid bridging chunk length: %d, Ignoring.\n", (int)chunkLen);
//             }
//             return;
//         }

//         // Forward valid chunk data to Serial1
//         Serial1.write((uint8_t*)(received.structCommand + 2), chunkLen);

//         if (debugEnabled) {
//             Serial.printf("Bridging chunk of %d bytes received from WCB%s\n", (int)chunkLen, received.structSenderID);
//         }
//         return;
//     }
//     lastReceivedViaESPNOW = true; // Prevent loopback issues

//     // // Handle normal WCB messages as usual
//     // if (received.structCommandIncluded) {
//     //     String cmd(received.structCommand);
//     //     cmd.trim();
//     //     if (!cmd.isEmpty()) {
//     //         enqueueCommand(cmd, 0); // Process as normal WCB command
//     //     }
//     // }
// }




void espNowReceiveCallback(const esp_now_recv_info_t *info, const uint8_t *incomingData, int len) {
    lastReceivedViaESPNOW = true; // Prevent loopback issues

  if (len != sizeof(espnow_struct_message)) {
      Serial.printf("Received unexpected size: %d (expected %d)\n", len, (int)sizeof(espnow_struct_message));
      return;
  }

  espnow_struct_message received;
  memcpy(&received, incomingData, len);
  String temp_espnowpassword = received.structPassword;
  if (temp_espnowpassword == espnowPassword){
    // Serial.printf("Received ESP-NOW Message from MAC: %02X:%02X:%02X:%02X:%02X:%02X\n",
    //             info->src_addr[0], info->src_addr[1], info->src_addr[2],
    //             info->src_addr[3], info->src_addr[4], info->src_addr[5]);

    // Serial.printf("Received Command: %s\n", received.structCommand);

    // Convert sender and target ID to integers
    int senderWCB = atoi(received.structSenderID);
    int targetWCB = atoi(received.structTargetID);

    // Serial.printf("Sender ID: WCB%d, Target ID: WCB%d\n", senderWCB, targetWCB);

    // Ensure message is from a WCB in the same group
    if (info->src_addr[1] != umac_oct2 || info->src_addr[2] != umac_oct3) {
        Serial.println("Received message from an unrelated WCB group, ignoring.");
        return;
    }
    if (targetWCB == 9) {
        // Extract chunk length from first 2 bytes of structCommand
        size_t chunkLen = (uint8_t)received.structCommand[0] | ((uint8_t)received.structCommand[1] << 8);

        // Safety check to prevent invalid length
        if (chunkLen > 180 || chunkLen == 0) {
            if (debugEnabled) {
                Serial.printf("Invalid bridging chunk length: %d, Ignoring.\n", (int)chunkLen);
            }
            return;
        }

        // Forward valid chunk data to Serial1
        Serial1.write((uint8_t*)(received.structCommand + 2), chunkLen);

        if (debugEnabled) {
            // Serial.printf("Bridging chunk of %d bytes received from WCB%s\n", (int)chunkLen, received.structSenderID);
        }
        return;
    }
    // Check if this message is meant for this WCB
    if (targetWCB != 0 && targetWCB != WCB_Number ) {
        Serial.println("Message not for this WCB, ignoring.");
        return;
    }

    // If valid, enqueue the command
    enqueueCommand(String(received.structCommand), 0);
  } else {
    Serial.println("ESPNOW password does not match local password!");
  }
}


// The main command processor
void handleSingleCommand(const String &cmd, int sourceID) {
  // 1) LocalFunctionIdentifier-based
  if (cmd.startsWith(String(LocalFunctionIdentifier))) {
    String message = cmd.substring(1);
    if (message.equals("DON")) {
      debugEnabled = true;
      Serial.println("Debugging enabled");
      return;
    } else if (message.equals("DOFF")) {
      debugEnabled = false;
      Serial.println("Debugging disabled");
      return;
    } else if (message.startsWith("LF")) {
      if (message.length() >= 3) {
        LocalFunctionIdentifier = message.charAt(2);
        saveLocalFunctionIdentifierAndCommandCharacter();
        Serial.printf("LocalFunctionIdentifier updated to '%c'\n", LocalFunctionIdentifier);
      } else {
        Serial.println("Invalid LocalFunctionIdentifier update command");
      }
    } else if (message.startsWith("CC")) {
      if (message.length() >= 3) {
        CommandCharacter = message.charAt(2);
        saveLocalFunctionIdentifierAndCommandCharacter();
        Serial.printf("CommandCharacter updated to '%c'\n", CommandCharacter);
      } else {
        Serial.println("Invalid CommandCharacter update command");
      }
    } else if (message.equals("REBOOT")){
      reboot();
    } else if (message.startsWith("CONFIG")) {
      Serial.println("Processing ?CONFIG command");
      printConfigInfo();
      return;
    } else if (message.startsWith("C")) {
      int slot = message.substring(1, 3).toInt();
      String commandData = message.substring(3);
      if (slot >= 1 && slot <= MAX_STORED_COMMANDS) {
        // store in slot
        storedCommands[slot - 1] = commandData;
        saveStoredCommandsToPreferences();
        Serial.printf("Stored command in slot %d: %s\n", slot, commandData.c_str());
      } else {
        Serial.println("Invalid command slot. Please enter a number between 1 and 50.");
      }
      return;
    } else if (message.startsWith("D")) {
      if (message.length() == 2) {
        char newDelimiter = message.charAt(1);
        setCommandDelimiter(newDelimiter);
        Serial.printf("Command delimiter updated to: '%c'\n", newDelimiter);
      } else {
        Serial.println("Invalid command delimiter update. Use ?Dx where x is the new delimiter.");
      }
      return;
    } else if (message.startsWith("CCLEAR")) {
      clearAllStoredCommands();
    } else if (message.startsWith("M2")) {
      String hexValue = message.substring(2, 4);
      int newValue = strtoul(hexValue.c_str(), NULL, 16);
      umac_oct2 = static_cast<uint8_t>(newValue);
      saveMACPreferences();
      Serial.printf("Updated the 2nd Octet to 0x%02X\n", umac_oct2);
      return;
    } else if (message.startsWith("M3")) {
      String hexValue = message.substring(2, 4);
      int newValue = strtoul(hexValue.c_str(), NULL, 16);
      umac_oct3 = static_cast<uint8_t>(newValue);
      saveMACPreferences();
      Serial.printf("Updated the 3rd Octet to 0x%02X\n", umac_oct3);
      return;
    } else if (message.equals("WCB_ERASE")) {
      eraseNVSFlash();
    } else if (message.startsWith("WCBQ")) {
      int wcbQty = message.substring(4).toInt();
      saveWCBQuantityPreferences(wcbQty);
      return;
    } else if (message.startsWith("WCB")) {
        int newWCB = message.substring(3).toInt();
        if (newWCB >= 1 && newWCB <= 9) {
        saveWCBNumberToPreferences(newWCB);
      } else {
        Serial.println("Invalid WCB number. Please enter a number between 1 and 9.");
      }
      return;
    } else if (message.startsWith("EPASS")) {
      String newPassword = message.substring(5);
      if (newPassword.length() > 0 && newPassword.length() < sizeof(espnowPassword)) {
        setESPNowPassword(newPassword.c_str());
        Serial.printf("ESP-NOW Password updated to: %s\n", newPassword.c_str());
      } else {
        Serial.println("Invalid ESP-NOW password length. Must be between 1 and 39 characters.");
      }
      return;
    } else if (message.startsWith("S")) { 
      // Handle enabling/disabling broadcast on serial ports
      int port = message.substring(1, 2).toInt();
      int state = message.substring(2, 3).toInt();
      if ((state == 0 || state == 1) && message.length()==3){
        if (port >= 1 && port <= 5) {
          serialBroadcastEnabled[port - 1] = (state == 1);
          saveBroadcastSettingsToPreferences();
          Serial.printf("Serial%d broadcast %s and stored in NVS\n",
              port, serialBroadcastEnabled[port - 1] ? "Enabled" : "Disabled");
        }
      } else {
        int baud = message.substring(2).toInt();
        if (port >= 1 && port <= 5) {
          updateBaudRate(port, baud);
          Serial.printf("Updated Serial%d baud rate to %d and stored in NVS\n", port, baud);
        }
      }
      return;
    } else if (message.startsWith("MAESTRO")) {
      updateBaudRate(1,57600);
      updateBaudRate(2,57600);
      return;
    } else if (message.startsWith("HW")){
      int temp_hw_version = message.substring(2).toInt();
      saveHWversion(temp_hw_version);
      return;
    }else {
      Serial.println("No valid command was give in the local menu section");
  }

  // 2) If command starts with CommandCharacter
  } else if (cmd.startsWith(String(CommandCharacter))) {
    String message = cmd.substring(1);

    // Example: ;S1Hello
    if (message.startsWith("S") || message.startsWith("s")) {
      int target = cmd.substring(2, 3).toInt();
      String serialMessage = message.substring(2);
      if (target >= 1 && target <= 5) {
        writeSerialString(getSerialStream(target), serialMessage);
        getSerialStream(target).flush();
        if (debugEnabled) {Serial.printf("Sent to Serial%d: Message: %s\n", target, serialMessage.c_str());}
      }
    }
    // Unicast to a specific WCB
    else if (message.startsWith("W") || message.startsWith("w")) {
      int targetWCB = message.substring(1, 2).toInt();
      String espnow_message = message.substring(2);
      if (targetWCB >= 1 && targetWCB <= Default_WCB_Quantity) {
        Serial.printf("Sending Unicast ESP-NOW message to WCB%d: %s\n", targetWCB, espnow_message.c_str());
        sendESPNowMessage(targetWCB, espnow_message.c_str());
      } else {
        Serial.println("Invalid WCB number for unicast.");
      }
      return;
    }
    // Recall a stored command
    else if (message.startsWith("C") || message.startsWith("c")) {
      int slot = message.substring(1, 3).toInt();
      if (slot >= 1 && slot <= MAX_STORED_COMMANDS) {
        recallCommandSlot(slot, sourceID);
      } else {
        Serial.printf("Invalid Sequence number %d. Please enter a number between 1 and 50.\n", slot);
      }
      return;
    } else if (message.startsWith("M") || message.startsWith("m")){
      int message_maestroID = message.substring(1,2).toInt();
      int message_maestroSeq = message.substring(2).toInt();
      sendMaestroCommand(message_maestroID,message_maestroSeq);
      return;
    } else {
      Serial.println("No valid Ch=nmmand Charcter function was chosen");
    }

  // 3) If no recognized prefix, we broadcast the command
  } else {
    if (debugEnabled) {
      Serial.printf("Broadcasting command: %s\n", cmd.c_str());
    }
    // Send to all serial ports
    for (int i = 1; i <= 5; i++) {
      if (i == sourceID || !serialBroadcastEnabled[i - 1]) continue;
      writeSerialString(getSerialStream(i), cmd);
      if (debugEnabled) {Serial.printf("Sent to Serial%d: %s\n", i, cmd.c_str());}
    }
    // Also send via ESP-NOW broadcast
    sendESPNowMessage(0, cmd.c_str());
    if (debugEnabled) {Serial.printf("Broadcasted via ESP-NOW: %s\n", cmd.c_str());}
  }
}

// processIncomingSerial for each serial port
void processIncomingSerial(Stream &serial, int sourceID) {
  if (!serial.available()) return;
  String data = serial.readStringUntil('\r');
  // Because we read from Serial, we reset lastReceivedViaESPNOW
  lastReceivedViaESPNOW = false;

  data.trim();
  if (debugEnabled) {
    Serial.printf("Processing input from Serial%d: %s\n", sourceID, data.c_str());
  }
  if (data.length() == 0) return;

  // If command starts with "?C", just enqueue it directly
  if (data.startsWith("?C")) {
    enqueueCommand(data, sourceID);
    return;
  }

  // Otherwise, parse by the commandDelimiter
  int startIdx = 0;
  while (true) {
    int delimPos = data.indexOf(commandDelimiter, startIdx);
    if (delimPos == -1) {
      String singleCmd = data.substring(startIdx);
      singleCmd.trim();
      if (!singleCmd.isEmpty()) {
        enqueueCommand(singleCmd, sourceID);
      }
      break;
    } else {
      String singleCmd = data.substring(startIdx, delimPos);
      singleCmd.trim();
      if (!singleCmd.isEmpty()) {
        enqueueCommand(singleCmd, sourceID);
      }
      startIdx = delimPos + 1;
    }
  }
}

// ============================= Setup & Loop =============================

void setup() {
  Serial.begin(115200);
  // loadHWversion();
  // Serial.print("Hardware Version: ");Serial.println(wcb_hw_version);
  loadWCBNumberFromPreferences();
  loadWCBQuantitiesFromPreferences();
  loadMACPreferences();

  // Start Serial


  // Create the command queue
  commandQueue = xQueueCreate(20, sizeof(CommandQueueItem));
  if (!commandQueue) {
    Serial.println("Failed to create command queue!");
  }

  Serial.println("\n\n-------------------------------------------------------");
  String hostname = getBoardHostname();
  Serial.printf("Booting up the %s\n", hostname.c_str());
  // printHWversion();
  Serial.printf("Software Version: %s\n", SoftwareVersion);
  Serial.println("-------------------------------------------------------");
  Serial.printf("Number of WCBs in the system: %d\n", Default_WCB_Quantity);

  loadBaudRatesFromPreferences();
  printBaudRates();

  // Initialize hardware serial
  Serial1.begin(baudRates[0], SERIAL_8N1, SERIAL1_RX_PIN, SERIAL1_TX_PIN);
  Serial2.begin(baudRates[1], SERIAL_8N1, SERIAL2_RX_PIN, SERIAL2_TX_PIN);

  // Initialize software serial
  Serial3.begin(baudRates[2]);
  Serial4.begin(baudRates[3]);
  Serial5.begin(baudRates[4]);

  // Initialize the NeoPixel status LED
  statusLED.begin();
  statusLED.setBrightness(10);
  statusLED.fill(blue);
  statusLED.show();

  // Initialize Wi-Fi
  WiFi.mode(WIFI_STA);

  // Dynamically update MAC addresses based on stored umac_oct2 & umac_oct3
  for (int i = 0; i < 9; i++) {
    WCBMacAddresses[i][0] = 0x02;
    WCBMacAddresses[i][1] = umac_oct2;
    WCBMacAddresses[i][2] = umac_oct3;
    WCBMacAddresses[i][3] = 0x00;
    WCBMacAddresses[i][4] = 0x00;
    WCBMacAddresses[i][5] = i + 1;
  }
   for (int i = 0; i < 1; i++) {
    broadcastMACAddress[i][0] = 0xFF;
    broadcastMACAddress[i][1] = umac_oct2;
    broadcastMACAddress[i][2] = umac_oct3;
    broadcastMACAddress[i][3] = 0xFF;
    broadcastMACAddress[i][4] = 0xFF;
    broadcastMACAddress[i][5] = 0xFF;
  }
  // Set our own ESP-NOW MAC
  esp_wifi_set_mac(WIFI_IF_STA, WCBMacAddresses[WCB_Number - 1]);

  // Print final MAC
  uint8_t baseMac[6];
  esp_wifi_get_mac(WIFI_IF_STA, baseMac);
  Serial.printf("ESP-NOW MAC Address: %02X:%02X:%02X:%02X:%02X:%02X\n",
                baseMac[0], baseMac[1], baseMac[2], baseMac[3], baseMac[4], baseMac[5]);

  // Load additional config
  loadESPNowPasswordFromPreferences();
  loadCommandDelimiter();
  loadLocalFunctionIdentifierAndCommandCharacter();
  loadStoredCommandsFromPreferences();

  // Init ESP-NOW
  if (esp_now_init() != ESP_OK) {
    Serial.println("Error initializing ESP-NOW");
    return;
  }

  // Add peers
  for (int i = 0; i < Default_WCB_Quantity; i++) {
    if (i + 1 == WCB_Number) continue; // skip ourselves
    esp_now_peer_info_t peerInfo = {};
    memcpy(peerInfo.peer_addr, WCBMacAddresses[i], 6);
    peerInfo.channel = 0;
    peerInfo.encrypt = false;
    if (esp_now_add_peer(&peerInfo) == ESP_OK) {
      Serial.printf("Added ESP-NOW peer: %02X:%02X:%02X:%02X:%02X:%02X\n",
                    WCBMacAddresses[i][0], WCBMacAddresses[i][1], WCBMacAddresses[i][2],
                    WCBMacAddresses[i][3], WCBMacAddresses[i][4], WCBMacAddresses[i][5]);
    } else {
      Serial.printf("Failed to add peer %d\n", i + 1);
    }
  }

  // Add broadcast peer
  esp_now_peer_info_t broadcastPeer = {};
  memcpy(broadcastPeer.peer_addr, broadcastMACAddress, 6);
  broadcastPeer.channel = 0;
  broadcastPeer.encrypt = false;
  if (esp_now_add_peer(&broadcastPeer) == ESP_OK) {
    Serial.println("Added ESP-NOW broadcast peer.");
  //   uint8_t broadmac[6];
  //  broadmac = broadcastMACAddress[];
  //   Serial.printf("ESP-NOW Broadcast MAC Address: %02X:%02X:%02X:%02X:%02X:%02X\n",
  //               broadmac[0], broadmac[1], broadmac[2], broadmac[3], broadmac[4], broadmac[5]);

  } else {
    Serial.println("Failed to add ESP-NOW broadcast peer!");
  }

  esp_now_register_recv_cb(espNowReceiveCallback);
}

void loop() {
  // Process USB Serial
  processIncomingSerial(Serial, 0);

  // Process Serial1..5
  if (Kyber){forwardSerial2Data();} else{processIncomingSerial(Serial1, 1);processIncomingSerial(Serial2, 2);};
  // if (Kyber){readMaestroComms();} else{processIncomingSerial(Serial2, 2);};
  processIncomingSerial(Serial3, 3);
  processIncomingSerial(Serial4, 4);
  processIncomingSerial(Serial5, 5);

  // Handle all queued commands
  if (!commandQueue) return;
  CommandQueueItem inItem;
  while (xQueueReceive(commandQueue, &inItem, 0) == pdTRUE) {
    String commandStr(inItem.cmd);
    free(inItem.cmd); // release dynamic memory
    handleSingleCommand(commandStr, inItem.sourceID);
  }
}
