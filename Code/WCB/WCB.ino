/////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////
///*****                                                                                                        *****////
///*****                                          Created by Greg Hulette.                                      *****////
///*****                                          Version 5.3_050816RNOV2025                                    *****////
///*****                                                                                                        *****////
///*****                                 So exactly what does this all do.....?                                 *****////
///*****                       - Receives commands via Serial or ESP-NOW                                        *****////
///*****                       - Sends Serial commands to other locally connected devices                       *****////
///*****                       - Sends Serial commands to other remotely connected devices                      *****////
///*****                       - Serial Commands sent ends with a Carriage Return "\r"                          *****////     
///*****                       - Controls Maestro Servo Controller via Serial Commands                          *****////  
///*****                       - Serial Commands sent ends with a Carriage Return "\r"                          *****////
///*****                       - Controls Maestro Servo Controller via Kyber                                    *****//// 
///*****                       - Store commands and recall them for later processing                            *****////
///*****                       - Servo passthrough from RC Receiver to remote servo                             *****////
///*****                                                                                                        *****////
///*****                            Full command syntax and description can be found at                         *****////
///*****                      https://github.com/greghulette/Wireless_Communication_Board-WCB                   *****////
///*****                                                                                                        *****////
/////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////

// ============================= Librarires  =============================
//You must install these into your Arduino IDE
#include <SoftwareSerial.h>                 // ESPSoftwareSerial library by Dirk Kaar, Peter Lerup V8.1.0
#include <Adafruit_NeoPixel.h>              // Adafruit NeoPixel library by Adafruit V1.12.5

//  All of these librarires are included in the ESP32 by Espressif board library V3.1.1
#include <Arduino.h>
#include <esp_wifi.h>
#include <esp_now.h>
#include <WiFi.h>
#include <HardwareSerial.h>
#include "WCB_Storage.h"
#include "WCB_Maestro.h"
#include "wcb_pin_map.h"
#include "command_timer_queue.h"
#include "esp_task_wdt.h"
#include "esp_system.h"
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include "WCB_PWM.h"


/////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////
///*****                                                                                                        *****////
///*****                                    DO NOT CHANGE ANYTHING IN THIS FILE                                 *****////
///*****                             All variables should be changed via the command line                       *****////
///*****                                                                                                        *****////
/////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////



// ============================= Global Variables =============================
// Number of WCB boards in the system
int WCB_Number = 1;                                                 // Default to WCB1.  Change to match your setup here or via command line
int Default_WCB_Quantity = 1;                                       // Default setting.  Change to match your setup here or via command line

// MAC Octets
uint8_t umac_oct2 = 0x00;                                           // Default setting.  Change to match your setup here or via command line
uint8_t umac_oct3 = 0x00;                                           // Default setting.  Change to match your setup here or via command line

// User-defined ESP-NOW password
char espnowPassword[40] = "change_me_or_risk_takeover";      // Default setting. Change to match your setup here or via command line.  Lower case characters only!

// Delimiter character (default '^')
char commandDelimiter = '^';                                        // Default setting.  Change to match your setup here or via command line
String commentDelimiter = "***";                                  // Default setting.  Change to match your setup here or via command line

// Characters for local functions and commands
char LocalFunctionIdentifier = '?';                                 // Default setting.  Change to match your setup here or via command line
char CommandCharacter = ';';                                        // Default setting.  Change to match your setup here or via command line

bool maestroEnabled = false;
bool Kyber_Local = false;    // this tracks if the Kyber is plugged into this board directly
bool Kyber_Remote = false;  // this tracks if the Kyber is plugged into this board directly
String Kyber_Location;

// Flag to track if last received message was via ESP-NOW
volatile bool lastReceivedViaESPNOW = false;
// Debugging flag (default: off)
bool debugEnabled = false;
bool debugKyber = false;
// WCB Board HW and SW version Variables
int wcb_hw_version = 0;  // Default = 0, Version 1.0 = 1 Version 2.1 = 21, Version 2.3 = 23, Version 2.4 = 24, Version 3.0 = 30
String SoftwareVersion = "5.3_050816RNOV2025";


Preferences preferences;  // Allows you to store information that persists after reboot and after reloading of sketch

// // Serial port pin assignments
int SERIAL1_TX_PIN;         //  // Serial 1 Tx Pin
int SERIAL1_RX_PIN;         //  // Serial 1 Rx Pin
int SERIAL2_TX_PIN;         //  // Serial 2 Tx Pin
int SERIAL2_RX_PIN;	        //  // Serial 2 Rx Pin
int SERIAL3_TX_PIN;         //  // Serial 3 Tx Pin
int SERIAL3_RX_PIN;         //  // Serial 3 Rx Pin
int SERIAL4_TX_PIN;         //  // Serial 4 Tx Pin 
int SERIAL4_RX_PIN;         //  // Serial 4 Rx Pin
int SERIAL5_TX_PIN;	        //  // Serial 5 Tx Pin
int SERIAL5_RX_PIN;	        //  // Serial 5 Rx Pin
int ONBOARD_LED;            //  // Pin for the onboard LED on version 1.0
int STATUS_LED_PIN;         //  // Pin for the onboard NeoPixel LED on version 2.x boards
  
// Default baud rates
#define SERIAL1_DEFAULT_BAUD_RATE 9600
#define SERIAL2_DEFAULT_BAUD_RATE 9600
#define SERIAL3_DEFAULT_BAUD_RATE 9600
#define SERIAL4_DEFAULT_BAUD_RATE 9600
#define SERIAL5_DEFAULT_BAUD_RATE 9600

// Broadcast enabled settings for each serial port (modifiable)
bool serialBroadcastEnabled[5] = {true, true, true, true, true};

// Current baud rates (modifiable)
unsigned long baudRates[5] = {
  SERIAL1_DEFAULT_BAUD_RATE,
  SERIAL2_DEFAULT_BAUD_RATE,
  SERIAL3_DEFAULT_BAUD_RATE,
  SERIAL4_DEFAULT_BAUD_RATE,
  SERIAL5_DEFAULT_BAUD_RATE
};

int storedBaudRate[6] = {
  0,
  SERIAL1_DEFAULT_BAUD_RATE,
  SERIAL2_DEFAULT_BAUD_RATE,
  SERIAL3_DEFAULT_BAUD_RATE,
  SERIAL4_DEFAULT_BAUD_RATE,
  SERIAL5_DEFAULT_BAUD_RATE
};

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
Adafruit_NeoPixel* statusLED = nullptr;

void colorWipeStatus(String statusled1, uint32_t c, int brightness) {
  if (wcb_hw_version == 21 || wcb_hw_version == 23 || wcb_hw_version == 24 || wcb_hw_version == 31){
    if (statusled1 == "ES"){
    statusLED->setBrightness(brightness);
    for (int i = 0; i<STATUS_LED_COUNT; i++){
      statusLED->setPixelColor(i, c);
      statusLED->show();
    } 
    } 
    else {
      if (debugEnabled){Serial.printf("No LED was chosen \n"); }
    };
  };
}

void turnOnLEDESPNOW(){
if (wcb_hw_version == 1){
  digitalWrite(ONBOARD_LED, HIGH); 
   }else if (wcb_hw_version == 21 ||  wcb_hw_version == 23 || wcb_hw_version == 24 || wcb_hw_version == 31){
    colorWipeStatus("ES", green, 255);
  }
}  


void   turnOnLEDforBoot(){
if (wcb_hw_version == 1){
  digitalWrite(ONBOARD_LED, HIGH); 
   } else if (wcb_hw_version == 21 ||  wcb_hw_version == 23 || wcb_hw_version == 24 || wcb_hw_version == 31){
    colorWipeStatus("ES", red, 255);
  } else {
    Serial.println("No LED yet defined");
  }

};

void turnOnLEDSerial(){
  if (wcb_hw_version == 21 ||  wcb_hw_version == 23 || wcb_hw_version == 24 || wcb_hw_version == 31){
    colorWipeStatus("ES", red, 255);
  }
}  

void turnOnLEDSerialOut(){
  if (wcb_hw_version == 21 ||  wcb_hw_version == 23 || wcb_hw_version == 24 || wcb_hw_version == 31){
    colorWipeStatus("ES", orange, 255);
  }
}  

void turnOffLED(){
  if (wcb_hw_version == 1 ){
    digitalWrite(ONBOARD_LED, LOW);   // Turns off the onboard Green LED
   }else if (wcb_hw_version == 21 ||  wcb_hw_version == 23 || wcb_hw_version == 24 || wcb_hw_version == 31){
    colorWipeStatus("ES", blue, 10);
    
  }
}

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
typedef struct __attribute__((packed)) {
  char structPassword[40];
  char structSenderID[4];
  char structTargetID[4];
  uint8_t structCommandIncluded;
  char structCommand[200];
} espnow_struct_message;

// ESP-NOW messages
espnow_struct_message commandsToSend[10]; // Includes 1-9 and broadcast
espnow_struct_message commandsToReceive[10];

// SoftwareSerial objects
SoftwareSerial Serial3(SERIAL3_RX_PIN, SERIAL3_TX_PIN);
SoftwareSerial Serial4(SERIAL4_RX_PIN, SERIAL4_TX_PIN);
SoftwareSerial Serial5(SERIAL5_RX_PIN, SERIAL5_TX_PIN);

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
void enqueueCommand(const String &cmd, int sourceID);
void processPWMPassthrough();
void addPWMOutputPort(int port);
void removePWMOutputPort(int port);
bool isSerialPortPWMOutput(int port);

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

void parseCommandsAndEnqueue(const String &data, int sourceID) {
  int startIdx = 0;
  while (true) {
    int delimPos = data.indexOf(commandDelimiter, startIdx);
    if (delimPos == -1) {
      String singleCmd = data.substring(startIdx);
      singleCmd.trim();
      if (!singleCmd.isEmpty()) {
        if (!singleCmd.startsWith(commentDelimiter)) {
          enqueueCommand(singleCmd, sourceID);
        } else {
          Serial.printf("Ignored chain command: %s\n", singleCmd.c_str());
        }
      }
      break;
    } else {
      String singleCmd = data.substring(startIdx, delimPos);
      singleCmd.trim();
      if (!singleCmd.isEmpty()) {
        if (!singleCmd.startsWith(commentDelimiter)) {
          enqueueCommand(singleCmd, sourceID);
        } else {
          Serial.printf("Ignored chain command: %s\n", singleCmd.c_str());
        }
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
  Serial.printf("Software Version %s\n", SoftwareVersion.c_str());
  loadBaudRatesFromPreferences();
  Serial.println("--------------- Serial Settings ----------------------");
  for (int i = 0; i < 5; i++) {
    Serial.printf("Serial%d Baud Rate: %d,  Broadcast: %s\n",
                  i + 1, baudRates[i], serialBroadcastEnabled[i] ? "Enabled" : "Disabled");
  }
  Serial.printf("Serial1 TX Pin: %d, RX Pin: %d\n", SERIAL1_TX_PIN, SERIAL1_RX_PIN);
  Serial.printf("Serial2 TX Pin: %d, RX Pin: %d\n", SERIAL2_TX_PIN, SERIAL2_RX_PIN);
  Serial.printf("Serial3 TX Pin: %d, RX Pin: %d\n", SERIAL3_TX_PIN, SERIAL3_RX_PIN);
  Serial.printf("Serial4 TX Pin: %d, RX Pin: %d\n", SERIAL4_TX_PIN, SERIAL4_RX_PIN);
  Serial.printf("Serial5 TX Pin: %d, RX Pin: %d\n", SERIAL5_TX_PIN, SERIAL5_RX_PIN);

  Serial.println("--------------- ESPNOW Settings ----------------------");
  // Print ESP-NOW password
  Serial.printf("ESP-NOW Password: %s\n", espnowPassword);
  // Print MAC address used for ESP-NOW (station MAC)
  uint8_t baseMac[6];
  esp_wifi_get_mac(WIFI_IF_STA, baseMac);
  Serial.printf("ESP-NOW (STA) MAC Address: %02X:%02X:%02X:%02X:%02X:%02X\n",
                baseMac[0], baseMac[1], baseMac[2], baseMac[3], baseMac[4], baseMac[5]);

  Serial.println("--------------- ESP Command Settings ----------------------");
  Serial.printf("Delimeter Character: %c\n", commandDelimiter);
  Serial.printf("Local Function Identifier: %c\n", LocalFunctionIdentifier);
  Serial.printf("Command Character: %c\n", CommandCharacter);

  // Print all stored commands
  listStoredCommands(); // List stored commands
  // Print PWM mappings
  listPWMMappings();  // Print PWM mappings

  Serial.println("--- End of Configuration Info ---\n");
}


//*******************************
/// ESP-NOW Functions
//*******************************
// Send an ESP-NOW message (unicast or broadcast)
void sendESPNowMessage(uint8_t target, const char *message) {
    // Skip broadcast if last was from ESP-NOW
    if (target == 0 && lastReceivedViaESPNOW) {
            if (debugEnabled) {Serial.printf("Skipping ESPNOW broadcast to avoid loops\n");};
        return;
    }
      // turnOnLEDESPNOW();

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
    // Serial.printf("Sending ESP-NOW message to MAC: %02X:%02X:%02X:%02X:%02X:%02X\n",
    //               mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    // Serial.printf("Sender ID: WCB%s, Target ID: WCB%s, Message: %s\n",
    //               msg.structSenderID, msg.structTargetID, msg.structCommand);

    // Send ESP-NOW message
    esp_err_t result = esp_now_send(mac, (uint8_t *)&msg, sizeof(msg));
    if (result == ESP_OK) {
        // Check if this is a PWM message (starts with ";P")
        bool isPWMMessage = (message[0] == ';' && (message[1] == 'P' || message[1] == 'p'));
        
        // Only print success if debug is on AND it's not a PWM message
        if (debugEnabled && !isPWMMessage) {
            Serial.println("ESP-NOW message sent successfully!");
        }
    } else {
        Serial.printf("ESP-NOW send failed! Error code: %d\n", result);
    }
  // turnOffLED();
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
        snprintf(msg.structTargetID, sizeof(msg.structTargetID), "0");

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

void espNowReceiveCallback(const esp_now_recv_info_t *info, const uint8_t *incomingData, int len) {

  if (len != sizeof(espnow_struct_message)) {
      // Serial.printf("Received unexpected size: %d (expected %d)\n", len, (int)sizeof(espnow_struct_message));
      return;
  }

  espnow_struct_message received;
  memcpy(&received, incomingData, sizeof(received));
  received.structPassword[sizeof(received.structPassword) - 1] = '\0';

  // memcpy(&received, incomingData, len);
  String temp_espnowpassword = String(received.structPassword);
  if (temp_espnowpassword == espnowPassword){
    // Serial.printf("Received ESP-NOW Message from MAC: %02X:%02X:%02X:%02X:%02X:%02X\n",
    //             info->src_addr[0], info->src_addr[1], info->src_addr[2],
    //             info->src_addr[3], info->src_addr[4], info->src_addr[5]);

    // if (debugEnabled){.printf("Received Command: %s\n", received.structCommand);}

    // Convert sender and target ID to integers
    int senderWCB = atoi(received.structSenderID);
    int targetWCB = atoi(received.structTargetID);

    if (debugEnabled) { Serial.printf("Sender ID: WCB%d, Target ID: WCB%d\n", senderWCB, targetWCB); }

    // Ensure message is from a WCB in the same group
    if (info->src_addr[1] != umac_oct2 || info->src_addr[2] != umac_oct3) {
        if (debugEnabled){Serial.println("Received message from an unrelated WCB group, ignoring."); } 
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
        if (Kyber_Local){ 
          Serial1.write((uint8_t*)(received.structCommand + 2), chunkLen);
          Serial2.write((uint8_t*)(received.structCommand + 2), chunkLen); 
          }  
        else if (Kyber_Remote) {
          Serial1.write((uint8_t*)(received.structCommand + 2), chunkLen);
          uint8_t* dataPtr = (uint8_t*)(received.structCommand + 2);

        if (debugKyber){
          Serial.print("Chunk (hex): ");
          for (int i = 0; i < chunkLen; i++) {
            if (dataPtr[i] < 0x10) Serial.print("0"); // leading zero
            Serial.print(dataPtr[i], HEX);
            Serial.print(" ");
          }
          Serial.println();
        } 
      }      

        if (debugEnabled) {
            // Serial.printf("Bridging chunk of %d bytes received from WCB%s\n", (int)chunkLen, received.structSenderID);
        }
        return;
    }

     lastReceivedViaESPNOW = true; // Prevent loopback issues
      colorWipeStatus("ES", green, 200);
    // Check if this message is meant for this WCB
    if (targetWCB != 0 && targetWCB != WCB_Number ) {
        if (debugEnabled) { Serial.println("Message not for this WCB, ignoring."); }
        return;
    }

    // If valid, enqueue the command
    enqueueCommand(String(received.structCommand), 0);
  } else {
    // Serial.println("ESPNOW password does not match local password!");
  }
    colorWipeStatus("ES", blue, 10);
}

//*******************************
/// Kyber Serial Forwarding Commands
//*******************************
void forwardDataFromKyber() {
  static uint8_t espnowBurst[64];   // tune if you want
  static size_t  burstLen = 0;

  bool gotAny = false;

  while (Serial2.available()) {
    int b = Serial2.read();
    if (b < 0) break;

    // Forward immediately to Serial1
    Serial1.write((uint8_t)b);

    // Debug (hex)
    if(debugKyber){Serial.printf("%02X ", (uint8_t)b);}
    

    // Accumulate for ESP-NOW
    if (burstLen < sizeof(espnowBurst)) {
      espnowBurst[burstLen++] = (uint8_t)b;
    } else {
      // buffer full -> flush now
      sendESPNowRaw(espnowBurst, burstLen);
      burstLen = 0;
      espnowBurst[burstLen++] = (uint8_t)b;
    }

    gotAny = true;
  }

  Serial1.flush();

  // If this call consumed a burst and UART is now idle, flush once to ESP-NOW
  if (gotAny && burstLen > 0 && Serial2.available() == 0) {
    sendESPNowRaw(espnowBurst, burstLen);
    burstLen = 0;
    Serial.println();  // newline after hex dump
  }
}

void forwardMaestroDataToLocalKyber() {
  static uint8_t buffer[64];

  // **Check if Serial2 has data**
  if (Serial1.available() > 0) {
    int len = Serial1.readBytes((char*)buffer, sizeof(buffer));

    // **Forward data to Serial1**
    Serial2.write(buffer, len);
    Serial2.flush(); // Ensure immediate transmission
    // **Forward via ESP-NOW broadcast**
    if (Kyber_Remote){sendESPNowRaw(buffer, len);}

  }
}

void forwardMaestroDataToRemoteKyber() {
  static uint8_t buffer[64];

  // **Check if Serial2 has data**
  if (Serial1.available() > 0) {
    int len = Serial1.readBytes((char*)buffer, sizeof(buffer));
  sendESPNowRaw(buffer, len);
  }
}



//*******************************
/// Processing Input Functions
//*******************************
void handleSingleCommand(String cmd, int sourceID) {
    // Serial.printf("handleSingleCommand called with: [%s] from source %d\n", cmd.c_str(), sourceID);

    // 1) LocalFunctionIdentifier-based commands (e.g., `?commands`)
    if (cmd.startsWith(String(LocalFunctionIdentifier))) {
        // cmd.toLowerCase();
        processLocalCommand(cmd.substring(1)); // Process local function
    } 
    // 2) CommandCharacter-based commands (e.g., `;commands`)
    else if (cmd.startsWith(String(CommandCharacter))) {
        processCommandCharcter(cmd.substring(1), sourceID); // Process serial-specific command
    } 
    // 3) Default to broadcasting if not a recognized prefix
    else {
        processBroadcastCommand(cmd, sourceID);
    }
}

//*******************************
/// Processing Local Function Identifier 
//*******************************
void processLocalCommand(const String &message) {
  // Serial.printf("processLocalCommand called with: [%s]\n", message.c_str());
    if (message == "don" || message == "DON") {
        debugEnabled = true;
        Serial.println("Debugging enabled");
        return;
    } else if (message == "doff" || message == "DOFF") {
        debugEnabled = false;
        Serial.println("Debugging disabled");
        return;
            } else if (message == "dkoff" || message == "DKOFF") {
        debugKyber = false;
        Serial.println("Kyber Debugging disabled");
        return;
    } else if (message == "dkon" || message == "DKON") {
        debugKyber = true;
        Serial.println("Kyber Debugging enabled");
        return;
    } else if (message.startsWith("lf") || message.startsWith("LF")) {
        updateLocalFunctionIdentifier(message);
        return;
    } else if (message.equals("cclear") || message.equals("CCLEAR")) {
        clearAllStoredCommands();
        return;
    } else if (message.startsWith("cc") || message.startsWith("CC")) {
        updateCommandCharacter(message);
        return;
    } else if (message.startsWith("cs") || message.startsWith("CS")) {
        storeCommand(message);
        return;
    } else if (message.startsWith("ce") || message.startsWith("CE")) {
        eraseSingleCommand(message);
        return;
    } else if (message == "reboot" || message == "REBOOT") {
        reboot();
    } else if (message == "config" || message == "CONFIG") {
        printConfigInfo();
        return;
    } else if (message.startsWith("d") || message.startsWith("D")) {
        updateCommandDelimiter(message);
        return;
    }  else if (message.startsWith("m2") || message.startsWith("M2")){
        update2ndMACOctet(message);
        return;
    } else if (message.startsWith("m3") || message.startsWith("M3")) {
        update3rdMACOctet(message);
        return;
    } else if (message == "wcb_erase" || message == "WCB_ERASE") {
        eraseNVSFlash();
    } else if (message.startsWith("wcbq") || message.startsWith("WCBQ")) {
        updateWCBQuantity(message);
        return;
    } else if (message.startsWith("wcb") || message.startsWith("WCB")) {
        updateWCBNumber(message);
        return;
    } else if (message.startsWith("epass") || message.startsWith("EPASS")) {
        updateESPNowPassword(message);
        return;
    } else if (message.startsWith("s") || message.startsWith("S")) {
        updateSerialSettings(message);
        return;
    } else if (message.startsWith("maestro_enable") || message.startsWith("MAESTRO_ENABLE")) {
        enableMaestroSerialBaudRate();
        return;
    }else if (message.startsWith("maestro_disable") || message.startsWith("MAESTRO_DISABLE")) {
        disableMaestroSerialBaudRate();
        return;
    } else if (message.equals("kyber_local") || message.equals("KYBER_LOCAL")){
        storeKyberSettings("local");
        printKyberSettings();
        return;
    } else if (message.equals("kyber_remote") || message.equals("KYBER_REMOTE")){
        storeKyberSettings("remote");
        printKyberSettings();
        return;
    } else if (message.equals("kyber_clear") || message.equals("KYBER_CLEAR")){
        storeKyberSettings("clear");
        printKyberSettings();
        return;
    } else if (message.startsWith("hw") || message.startsWith("HW")) {
        updateHWVersion(message);
        return;
    } else if (message.startsWith("pms") || message.startsWith("PMS")) {
      addPWMMapping(message);
      return;
    } else if (message.startsWith("prs") || message.startsWith("PRS")) {
        String portStr = message.substring(3);
        int port = portStr.toInt();
        removePWMMapping(port);
        return;
    } else if (message.equals("plist") || message.equals("PLIST")) {
        listPWMMappings();
        return;
    } else if (message.equals("pclear") || message.equals("PCLEAR")) {
      
        clearAllPWMMappings();
    return;
    } else if (message.startsWith("po") || message.startsWith("PO")) {
    int port = message.substring(2).toInt();
    addPWMOutputPort(port);
    return;
    } else if (message.startsWith("px") || message.startsWith("PX")) {
        int port = message.substring(2).toInt();
        removePWMOutputPort(port);
        return;
    } else {
        Serial.println("Invalid Local Command");
    }
}

//*******************************
/// Processing Local Function Identifier Functions
//*******************************
void updateLocalFunctionIdentifier(const String &message){
  if (message.length() >= 3) {
    LocalFunctionIdentifier = message.charAt(2);
    saveLocalFunctionIdentifierAndCommandCharacter();
    Serial.printf("LocalFunctionIdentifier updated to '%c'\n", LocalFunctionIdentifier);
  } else {
    Serial.println("Invalid LocalFunctionIdentifier update command");
  }
}

void updateCommandCharacter(const String &message){
  if (message.length() >= 3) {
    CommandCharacter = message.charAt(2);
    saveLocalFunctionIdentifierAndCommandCharacter();
    Serial.printf("CommandCharacter updated to '%c'\n", CommandCharacter);
  } else {
    Serial.println("Invalid CommandCharacter update command");
  }
}

void storeCommand(const String &message){
  saveStoredCommandsToPreferences(message.substring(2));
}

void eraseSingleCommand(const String &message){
  eraseStoredCommandByName(message.substring(2));
}

void updateCommandDelimiter(const String &message){
  if (message.length() == 2) {
    char newDelimiter = message.charAt(1);
    setCommandDelimiter(newDelimiter);
    Serial.printf("Command delimiter updated to: '%c'\n", newDelimiter);
  } else {
    Serial.println("Invalid command delimiter update. Use ?Dx where x is the new delimiter.");
  }
}

void update2ndMACOctet(const String &message){
  if (message.length() >= 4) {
    String hexValue = message.substring(2, 4);
    char *endptr = NULL;
    long newValue = strtol(hexValue.c_str(), &endptr, 16);
    if (endptr != NULL && *endptr == '\0' && newValue >= 0 && newValue <= 0xFF) {
      umac_oct2 = static_cast<uint8_t>(newValue);
      saveMACPreferences();
      Serial.printf("Updated the 2nd Octet to 0x%02X\n", umac_oct2);
    } else {
      Serial.println("Invalid hex value for 2nd MAC octet. Use two hex digits (00-FF).");
    }
  } else {
    Serial.println("Invalid command. Use ?M2xx where xx is two hex digits.");
  }
}

void update3rdMACOctet(const String &message){
  if (message.length() >= 4) {
    String hexValue = message.substring(2, 4);
    char *endptr = NULL;
    long newValue = strtol(hexValue.c_str(), &endptr, 16);
    if (endptr != NULL && *endptr == '\0' && newValue >= 0 && newValue <= 0xFF) {
      umac_oct3 = static_cast<uint8_t>(newValue);
      saveMACPreferences();
      Serial.printf("Updated the 3rd Octet to 0x%02X\n", umac_oct3);
    } else {
      Serial.println("Invalid hex value for 3rd MAC octet. Use two hex digits (00-FF).");
    }
  } else {
    Serial.println("Invalid command. Use ?M3xx where xx is two hex digits.");
  }
}

void updateWCBQuantity(const String &message){
  int wcbQty = message.substring(4).toInt();
  saveWCBQuantityPreferences(wcbQty);
}

void updateSerialSettings(const String &message){
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
}

void updateWCBNumber(const String &message){
  int newWCB = message.substring(3).toInt();
  if (newWCB >= 1 && newWCB <= 9) {
    saveWCBNumberToPreferences(newWCB);
  }
}

void updateESPNowPassword(const String &message){
  String newPassword = message.substring(5);
  if (newPassword.length() > 0 && newPassword.length() < sizeof(espnowPassword)) {
    setESPNowPassword(newPassword.c_str());
    Serial.printf("ESP-NOW Password updated to: %s\n", newPassword.c_str());
  } else {
    Serial.println("Invalid ESP-NOW password length. Must be between 1 and 39 characters.");
  }
}

void enableMaestroSerialBaudRate(){
    recallBaudRatefromSerial(1);  
    // Print the saved baud rate and then update to Maestro-compatible baud
  Serial.printf("Saved original baud rate of %d for Serial 1, updating to 57600 to support Maestro\n", storedBaudRate[1]);
    updateBaudRate(1, 57600);
}

void disableMaestroSerialBaudRate(){
    updateBaudRate(1, storedBaudRate[1]);    
    Serial.printf("Saved original baud rate of %d for Serial 1\n", storedBaudRate[1]);
}

void updateHWVersion(const String &message) {
  int temp_hw_version = message.substring(2).toInt();
  saveHWversion(temp_hw_version);
}

//*******************************
/// Processing Command Character
//*******************************
void processCommandCharcter(const String &message, int sourceID) {
    if (message.startsWith("s") || message.startsWith("S")) {
        processSerialMessage(message);
    } else if (message.startsWith("w") || message.startsWith("W")) {
        processWCBMessage(message);
    } else if (message.startsWith("c") || message.startsWith("C")) {
        recallStoredCommand(message, sourceID);
    } else if (message.startsWith("m") || message.startsWith("M")) {
        processMaestroCommand(message);
    } else if (message.startsWith("p") || message.startsWith("P")) {
      processPWMOutput(message);
    } else {
        Serial.println("Invalid Serial Command");
    }
}

//*******************************
/// Processing Command Character Functions
//*******************************
void processSerialMessage(const String &message) {
    if (message.length() < 2) {
        Serial.println("Invalid serial message format.");
        return;
    }

    // Extract serial port number
    int target = message.substring(1, 2).toInt();  // Extract Serial port (1-5)
    if (target < 1 || target > 5) {
        Serial.println("Invalid serial port number. Must be between 1 and 5.");
        return;
    }

    // Extract message to send
    String serialMessage = message.substring(2);
    serialMessage.trim();

    // Send to selected serial port
    Stream &targetSerial = getSerialStream(target);
    writeSerialString(targetSerial, serialMessage);
    targetSerial.flush(); // Ensure it's sent immediately

    if (debugEnabled) {
        Serial.printf("Sent to Serial%d: %s\n", target, serialMessage.c_str());
    }
}

void processWCBMessage(const String &message){
  int targetWCB = message.substring(1, 2).toInt();
        String espnow_message = message.substring(2);
        if (targetWCB >= 1 && targetWCB <= Default_WCB_Quantity) {
          Serial.printf("Sending Unicast ESP-NOW message to WCB%d: %s\n", targetWCB, espnow_message.c_str());
          sendESPNowMessage(targetWCB, espnow_message.c_str());
        } else {
          Serial.println("Invalid WCB number for unicast.");
        }
}

void recallStoredCommand(const String &message, int sourceID) {
  String key = message.substring(1);
  key.trim();
  if (key.length() > 0) {
    recallCommandSlot(key, sourceID);
  } else {
    Serial.println("Invalid recall command. Use ;Ckey to recall a command.");
  }
}

void processMaestroCommand(const String &message){
  int message_maestroID = message.substring(1,2).toInt();
  int message_maestroSeq = message.substring(2).toInt();
  sendMaestroCommand(message_maestroID,message_maestroSeq);
}

void processPWMOutput(const String &message) {
    // Format: ;Pxnnnn where x=port, nnnn=pulse width in microseconds
    int port = message.substring(1, 2).toInt();
    unsigned long pulseWidth = message.substring(2).toInt();
    
    if (port < 1 || port > 5 || pulseWidth < 800 || pulseWidth > 2200) {
        if (debugEnabled) Serial.println("Invalid PWM output command");
        return;
    }
    
    int txPin = 0;
    switch(port) {
        case 1: txPin = SERIAL1_TX_PIN; break;
        case 2: txPin = SERIAL2_TX_PIN; break;
        case 3: txPin = SERIAL3_TX_PIN; break;
        case 4: txPin = SERIAL4_TX_PIN; break;
        case 5: txPin = SERIAL5_TX_PIN; break;
    }
    
    if (txPin > 0) {
        pinMode(txPin, OUTPUT);
        digitalWrite(txPin, HIGH);
        delayMicroseconds(pulseWidth);
        digitalWrite(txPin, LOW);
    }
}


//*******************************
/// Processing Broadcast Function
//*******************************
void processBroadcastCommand(const String &cmd, int sourceID) {
    if (debugEnabled) {
        Serial.printf("Broadcasting command: %s\n", cmd.c_str());
    }

    // Send to all serial ports except restricted ones
    for (int i = 1; i <= 5; i++) {
        // Skip Serial1 if Kyber_Remote is enabled
        if ((i == 1 && Kyber_Remote) || 
            (i <= 2 && Kyber_Local) || 
            i == sourceID || !serialBroadcastEnabled[i - 1]) {
            continue;
        }

        writeSerialString(getSerialStream(i), cmd);
        if (debugEnabled) { Serial.printf("Sent to Serial%d: %s\n", i, cmd.c_str()); }
    }

    // Always send via ESP-NOW broadcast
    if (!lastReceivedViaESPNOW){
      sendESPNowMessage(0, cmd.c_str());
      if (debugEnabled) { Serial.printf("Broadcasted via ESP-NOW: %s\n", cmd.c_str()); }
    }
  }

// processIncomingSerial for each serial port
void processIncomingSerial(Stream &serial, int sourceID) {
  if (!serial.available()) return;  // Exit if no data available

  // static String serialBuffer = "";  // Buffer for incoming serial data
  static String serialBuffers[6];  // one for each serial port (0 = Serial, 1–5 = Serial1-5)
  String &serialBuffer = serialBuffers[sourceID];
  while (serial.available()) {
    char c = serial.read();
  // Serial.printf("Serial%d read: '%c' (0x%02X)\n", sourceID, c, (uint8_t)c);
    if (c == '\r' || c == '\n') {  // End of command
      if (!serialBuffer.isEmpty()) {
          serialBuffer.trim();  // Remove leading/trailing spaces
          if (debugEnabled){Serial.printf("Processing input from Serial%d: %s\n", sourceID, serialBuffer.c_str());} 

          // Reset last received flag since we are reading from Serial
          lastReceivedViaESPNOW = false;

          // Process the command
          processSerialCommandHelper(serialBuffer, sourceID);

            // Clear buffer for next command
          serialBuffer = "";
      }
    } else {
      serialBuffer += c;  // Append new characters to buffer
    }
  }
}

// Helper function to process serial commands
void processSerialCommandHelper(String &data, int sourceID) {
    if (debugEnabled) {
        Serial.printf("Processing command from Serial%d: %s\n", sourceID, data.c_str());
    }

    if (data.length() == 0) return;
          if (checkForTimerStopRequest(data)) {
          stopTimerSequence();
          return;
      }

    // Direct enqueue if command starts with "?C"
    if (data.startsWith(String(LocalFunctionIdentifier) + "C") || 
        data.startsWith(String(LocalFunctionIdentifier) + "c")) {
        enqueueCommand(data, sourceID);
        return;
    }

    // Timer-aware parsing: if command contains ;T or ;t, use grouped mode
    if (data.indexOf(";T") != -1 || data.indexOf(";t") != -1) {
        parseCommandGroups(data);  // Uses non-blocking delay between command groups
        return;
    }

    // Otherwise, parse normally
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

void printResetReason() {
    esp_reset_reason_t reason = esp_reset_reason();
    Serial.printf("Reset reason: %d - ", reason);
    switch (reason) {
        case ESP_RST_POWERON: Serial.println("Power-on Reset"); break;
        case ESP_RST_EXT: Serial.println("External Reset"); break;
        case ESP_RST_SW: Serial.println("Software Reset"); break;
        case ESP_RST_PANIC: Serial.println("Exception/Panic Reset"); break;
        case ESP_RST_INT_WDT: Serial.println("Interrupt Watchdog Reset"); break;
        case ESP_RST_TASK_WDT: Serial.println("Task Watchdog Reset"); break;
        case ESP_RST_WDT: Serial.println("Other Watchdog Reset"); break;
        case ESP_RST_DEEPSLEEP: Serial.println("Deep Sleep Reset"); break;
        case ESP_RST_BROWNOUT: Serial.println("Brownout Reset"); break;
        case ESP_RST_SDIO: Serial.println("SDIO Reset"); break;
        default: Serial.println("Unknown Reset"); break;
    }
}

/// Task Definition for multi threading.  Act as separate loops within the main loop.
void KyberLocalTask(void *pvParameters) {
    while (true) {
        forwardMaestroDataToLocalKyber();
        forwardDataFromKyber();
        vTaskDelay(pdMS_TO_TICKS(5)); // Reduce CPU usage while maintaining speed
    }
}

void KyberRemoteTask(void *pvParameters) {
    while (true) {
        forwardMaestroDataToRemoteKyber();
        vTaskDelay(pdMS_TO_TICKS(5)); // Reduce CPU usage while maintaining speed
    }
}

void serialCommandTask(void *pvParameters) {
  vTaskDelay(pdMS_TO_TICKS(500));
    while (true) {
        processIncomingSerial(Serial, 0);
        // Only process Serial3-5 if not used for PWM
        if (!isSerialPortUsedForPWMInput(3) && !isSerialPortPWMOutput(3)) {
            processIncomingSerial(Serial3, 3);
        }
        if (!isSerialPortUsedForPWMInput(4) && !isSerialPortPWMOutput(4)) {
            processIncomingSerial(Serial4, 4);
        }
        if (!isSerialPortUsedForPWMInput(5) && !isSerialPortPWMOutput(5)) {
            processIncomingSerial(Serial5, 5);
        }

        if (Kyber_Local) {
          // don't add serial 1 or 2 for processing strings
        } else if (Kyber_Remote){
            processIncomingSerial(Serial2, 2);
        } else {
          if (!isSerialPortUsedForPWMInput(1) && !isSerialPortPWMOutput(1)) {
              processIncomingSerial(Serial1, 1);
          }
          if (!isSerialPortUsedForPWMInput(2) && !isSerialPortPWMOutput(2)) {
              processIncomingSerial(Serial2, 2);
          }
        }

        vTaskDelay(pdMS_TO_TICKS(5));
    }
}

// void serialCommandTask(void *pvParameters) {
//   vTaskDelay(pdMS_TO_TICKS(500));  // at top of task to enable proper startup timing
//     while (true) {
//         processIncomingSerial(Serial, 0);  // USB Serial
//         processIncomingSerial(Serial3, 3);
//         processIncomingSerial(Serial4, 4);
//         processIncomingSerial(Serial5, 5);

//         if (Kyber_Local) {
//           // don't add serial 1 or 2 for processing strings
//         } else if (Kyber_Remote){
//             processIncomingSerial(Serial2, 2);
//           // add only serial 2 for processing strings
//         } else {
//           processIncomingSerial(Serial1, 1);
//           processIncomingSerial(Serial2, 2);
//         }

//         vTaskDelay(pdMS_TO_TICKS(5)); // Allow time for other tasks
//     }
// }

void PWMTask(void *pvParameters) {
    while (true) {
        processPWMPassthrough();
        vTaskDelay(pdMS_TO_TICKS(5)); // Process every 5ms for ~200Hz update rate
    }
}

void initStatusLEDWithRetry(int maxRetries = 10, int delayBetweenMs = 200) {
  int attempt = 0;
  bool initialized = false;

  while (attempt < maxRetries && !initialized) {
    if (debugEnabled) Serial.printf("Attempting NeoPixel init (try %d)...\n", attempt + 1);

    // Optional: prep the pin to reduce false starts
    pinMode(STATUS_LED_PIN, OUTPUT);
    digitalWrite(STATUS_LED_PIN, LOW);
    delay(1000);  // let internal structures stabilize
    // Try to init
    statusLED = new Adafruit_NeoPixel(STATUS_LED_COUNT, STATUS_LED_PIN, NEO_GRB + NEO_KHZ800);
    delay(750);  // let internal structures stabilize
    statusLED->begin();

    // Try setting a test color
    statusLED->setPixelColor(0, statusLED->Color(0, 0, 5));
    statusLED->show();
    delay(750);

    // Check if the LED actually changed (simple test)
    if (statusLED->getPixelColor(0) != 0) {
      initialized = true;
    } else {
      delete statusLED;
      statusLED = nullptr;
      delay(delayBetweenMs);  // wait before next attempt
    }

    attempt++;
  }

  if (!initialized) {
    Serial.println("❌ Failed to initialize NeoPixel after retries.");
  } else {
    // Serial.printf("✅ NeoPixel initialized successfully after %d attempt(s).\n", attempt);
  }
}

// ============================= Setup & Loop =============================

void setup() {

  Serial.begin(115200);
  delay(1000);  // allow USB to stabilize
  while (Serial.available()) Serial.read();  // 🔥 flush startup junk
  loadHWversion();
  loadKyberSettings();
  printResetReason();  // Show the exact cause of reset
  loadWCBNumberFromPreferences();
  loadWCBQuantitiesFromPreferences();
  loadMACPreferences();
// Initialize the NeoPixel status LED
  if (wcb_hw_version == 0){
    Serial.println("No LED was setup.  Define HW version");
  } else if (wcb_hw_version == 1){
    pinMode(ONBOARD_LED, OUTPUT);
  } else if (wcb_hw_version == 21 ||  wcb_hw_version == 23 || wcb_hw_version == 24) {
    statusLED = new  Adafruit_NeoPixel(STATUS_LED_COUNT, STATUS_LED_PIN, NEO_GRB + NEO_KHZ800);
    statusLED->begin();
    statusLED->show();
  } else if (wcb_hw_version == 31){
      initStatusLEDWithRetry(10, 200);  // Up to 10 tries with 200ms delay between
  } 

  delay(1000);        // I hate using delays but this was added to allow the RMT to stabilize before using LEDs
  turnOnLEDforBoot();
  // Create the command queue
  commandQueue = xQueueCreate(20, sizeof(CommandQueueItem));
  if (!commandQueue) {
    Serial.println("Failed to create command queue!");
  }

  Serial.println("\n\n-------------------------------------------------------");
  String hostname = getBoardHostname();
  Serial.printf("Booting up the %s\n", hostname.c_str());
  printHWversion();
  Serial.printf("Software Version: %s\n", SoftwareVersion.c_str());
  Serial.printf("Number of WCBs in the system: %d\n", Default_WCB_Quantity);
  Serial.println("-------------------------------------------------------");
  loadBaudRatesFromPreferences();
  initPWM();  // <-- This loads PWM mappings from preferences

  printBaudRates();
  Serial.println("-------------------------------------------------------");

  // Initialize hardware serial
  // Serial1.begin(baudRates[0], SERIAL_8N1, SERIAL1_RX_PIN, SERIAL1_TX_PIN);
  // Serial2.begin(baudRates[1], SERIAL_8N1, SERIAL2_RX_PIN, SERIAL2_TX_PIN);
  // Serial3.begin(baudRates[2], SWSERIAL_8N1, SERIAL3_RX_PIN, SERIAL3_TX_PIN, false, 95);
  // Serial4.begin(baudRates[3], SWSERIAL_8N1, SERIAL4_RX_PIN, SERIAL4_TX_PIN, false, 95);
  // Serial5.begin(baudRates[4], SWSERIAL_8N1, SERIAL5_RX_PIN, SERIAL5_TX_PIN, false, 95);
  

  // Initialize hardware serial - but skip ports used for PWM input
 // Initialize hardware serial - but skip ports used for PWM or Kyber
if (!Kyber_Local && !Kyber_Remote && !isSerialPortUsedForPWMInput(1) && !isSerialPortPWMOutput(1)) {
    Serial1.begin(baudRates[0], SERIAL_8N1, SERIAL1_RX_PIN, SERIAL1_TX_PIN);
}
if (!Kyber_Local && !isSerialPortUsedForPWMInput(2) && !isSerialPortPWMOutput(2)) {
    Serial2.begin(baudRates[1], SERIAL_8N1, SERIAL2_RX_PIN, SERIAL2_TX_PIN);
}
if (!isSerialPortUsedForPWMInput(3) && !isSerialPortPWMOutput(3)) {
    Serial3.begin(baudRates[2], SWSERIAL_8N1, SERIAL3_RX_PIN, SERIAL3_TX_PIN, false, 95);
}
if (!isSerialPortUsedForPWMInput(4) && !isSerialPortPWMOutput(4)) {
    Serial4.begin(baudRates[3], SWSERIAL_8N1, SERIAL4_RX_PIN, SERIAL4_TX_PIN, false, 95);
}
if (!isSerialPortUsedForPWMInput(5) && !isSerialPortPWMOutput(5)) {
    Serial5.begin(baudRates[4], SWSERIAL_8N1, SERIAL5_RX_PIN, SERIAL5_TX_PIN, false, 95);
}

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
  
  loadESPNowPasswordFromPreferences();

  // Print final MAC
  uint8_t baseMac[6];
  esp_wifi_get_mac(WIFI_IF_STA, baseMac);
  Serial.printf("ESP-NOW MAC Address: %02X:%02X:%02X:%02X:%02X:%02X\n",
                baseMac[0], baseMac[1], baseMac[2], baseMac[3], baseMac[4], baseMac[5]);


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
    Serial.printf("Added ESP-NOW broadcast peer: %02X:%02X:%02X:%02X:%02X:%02X\n",
                broadcastMACAddress[0][0], broadcastMACAddress[0][1], broadcastMACAddress[0][2], broadcastMACAddress[0][3], broadcastMACAddress[0][4], broadcastMACAddress[0][5]);

  } else {
    Serial.println("Failed to add ESP-NOW broadcast peer!");
  }
  Serial.println("-------------------------------------------------------");

  // Load additional config
  loadCommandDelimiter();
  loadLocalFunctionIdentifierAndCommandCharacter();
  loadStoredCommandsFromPreferences();

  Serial.printf("Delimeter Character: %c\n", commandDelimiter);
  Serial.printf("Local Function Identifier: %c\n", LocalFunctionIdentifier);
  Serial.printf("Command Character: %c\n", CommandCharacter);
  Serial.println("-------------------------------------------------------");
  printKyberSettings();
  esp_now_register_recv_cb(espNowReceiveCallback);

  // Create FreeRTOS Tasks-
  xTaskCreatePinnedToCore(serialCommandTask, "Serial Command Task", 4096, NULL, 1, NULL, 1);

  // If bridging is enabled, create the bridging task
  if (Kyber_Local) {
      xTaskCreatePinnedToCore(KyberLocalTask, "Kyber Local Task", 4096, NULL, 1, NULL, 1);
      Serial.println("Kyber_Local Task Created");
  }    
  if (Kyber_Remote) {
      xTaskCreatePinnedToCore(KyberRemoteTask, "Kyber Remote Task", 4096, NULL, 1, NULL, 1);
      Serial.println("Kyber_Remote Task Created");
  }

  xTaskCreatePinnedToCore(PWMTask, "PWM Task", 2048, NULL, 2, NULL, 0); 

  delay(150);     
  turnOffLED();
  // Serial.println("------------- Setup Complete-----------------");
}

void loop() {

  // Handle queued commands
  if (!commandQueue) return;
  CommandQueueItem inItem;
  while (xQueueReceive(commandQueue, &inItem, 0) == pdTRUE) {
    String commandStr(inItem.cmd);
    free(inItem.cmd);
    handleSingleCommand(commandStr, inItem.sourceID);
  }
}
