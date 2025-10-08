/////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////
///*****                                                                                                        *****////
///*****                                          Created by Greg Hulette.                                      *****////
///*****                                          Version 5.2_250951RSEP25                                     *****////
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
///*****                                                                                                        *****////
///*****                            Full command syntax and description can be found at                         *****////
///*****                      https://github.com/greghulette/Wireless_Communication_Board-WCB                   *****////
///*****                                                                                                        *****////
/////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////

// ============================= Libraries  =============================
//You must install these into your Arduino IDE
#include <SoftwareSerial.h>                 // ESPSoftwareSerial library by Dirk Kaar, Peter Lerup V8.1.0
#include <Adafruit_NeoPixel.h>              // Adafruit NeoPixel library by Adafruit V1.12.5

//  All of these libraries are included in the ESP32 by Espressif board library V3.1.1
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
#include <vector>

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
bool lastReceivedViaESPNOW = false;

// Debugging flag (default: off)
bool debugEnabled = false;

// WCB Board HW and SW version Variables
int wcb_hw_version = 0;  // Default = 0, Version 1.0 = 1 Version 2.1 = 21, Version 2.3 = 23, Version 2.4 = 24, Version 3.0 = 30
String SoftwareVersion = "5.0_091116RJUL25";

Preferences preferences;  // Allows you to store information that persists after reboot and after reloading of sketch

// Serial port pin assignments
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

// ============================= String Optimization & Performance Monitoring =============================
// Pre-allocated string buffers to avoid repeated allocation/deallocation
String reusableBuffer1;
String reusableBuffer2;
String reusableBuffer3;

// Performance tracking variables
static unsigned long lastPerfCheck = 0;
static uint32_t commandCount = 0;
static uint32_t espnowOutCount = 0;
static uint32_t espnowInCount = 0;
static unsigned long totalCommandTime = 0;

// SoftwareSerial objects - MUST BE DECLARED EARLY
SoftwareSerial Serial3(SERIAL3_RX_PIN, SERIAL3_TX_PIN);
SoftwareSerial Serial4(SERIAL4_RX_PIN, SERIAL4_TX_PIN);
SoftwareSerial Serial5(SERIAL5_RX_PIN, SERIAL5_TX_PIN);

void initializeStringBuffers() {
  reusableBuffer1.reserve(1200);  // 1000 char limit + safety margin
  reusableBuffer2.reserve(1200);
  reusableBuffer3.reserve(1200);
}

// ============================= PHASE 3: Optimized Serial Operations (EARLY DEFINITION) =============================
// Write a string + `\r` to a given Stream - NON-BLOCKING
void writeSerialStringOptimized(Stream &serialPort, const String &stringData) {
  // Use pre-allocated buffer to avoid string concatenation overhead
  size_t msgLen = stringData.length();
  if (msgLen > 1198) msgLen = 1198; // Safety limit for 1200 char buffer
  
  reusableBuffer1 = stringData;
  reusableBuffer1 += '\r';
  
  // Write all at once - more efficient than character by character
  serialPort.write((uint8_t*)reusableBuffer1.c_str(), reusableBuffer1.length());
  // NO flush() - let hardware buffer handle it for non-blocking operation
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

// ============================= Performance Monitoring Functions =============================
void updatePerformanceCounters(unsigned long commandTime) {
  commandCount++;
  totalCommandTime += commandTime;
}

void printPerformanceReport() {
  unsigned long currentTime = millis();
  unsigned long elapsed = currentTime - lastPerfCheck;
  
  if (elapsed >= 30000) {  // Every 30 seconds
    unsigned long avgCommandTime = (commandCount > 0) ? (totalCommandTime / commandCount) : 0;
    
    Serial.printf("Performance: %lu cmds/30s, %lu ESP-NOW out, %lu ESP-NOW in, avg %lu µs/cmd, free heap: %d bytes\n",
                  commandCount, espnowOutCount, espnowInCount, avgCommandTime, ESP.getFreeHeap());
    
    // Reset counters
    commandCount = 0;
    espnowOutCount = 0;
    espnowInCount = 0;
    totalCommandTime = 0;
    lastPerfCheck = currentTime;
  }
}

// ============================= PHASE 5: Asynchronous Serial Broadcasting =============================
// Serial message queue structures
typedef struct {
  char *message;              // Dynamically allocated message
} SerialQueueItem;

// One queue for each serial port
static QueueHandle_t serialQueues[5] = {nullptr};

// Queue a message to a specific serial port - NON-BLOCKING
void queueSerialMessage(int serialPort, const String &message) {
  if (serialPort < 1 || serialPort > 5 || !serialQueues[serialPort-1]) return;
  
  SerialQueueItem item;
  int length = message.length() + 1;
  item.message = (char *)malloc(length);
  if (!item.message) {
    if (debugEnabled) Serial.println("Out of memory for serial queue!");
    return;
  }
  
  strncpy(item.message, message.c_str(), length);
  item.message[length - 1] = '\0';
  
  // Non-blocking queue send
  if (xQueueSend(serialQueues[serialPort-1], &item, 0) != pdTRUE) {
    free(item.message); // Free if queue full
    if (debugEnabled) {
      Serial.printf("Serial%d queue full, dropping message\n", serialPort);
    }
  }
}

// Asynchronous serial broadcasting task
void serialBroadcastTask(void *pvParameters) {
  SerialQueueItem item;
  
  while (true) {
    // Check each serial port queue
    for (int i = 0; i < 5; i++) {
      if (xQueueReceive(serialQueues[i], &item, 0) == pdTRUE) {
        // Send to the appropriate serial port
        Stream &targetSerial = getSerialStream(i + 1);
        writeSerialStringOptimized(targetSerial, String(item.message));
        
        if (debugEnabled) {
          Serial.printf("Async sent to Serial%d: %s\n", i + 1, item.message);
        }
        
        free(item.message); // Free allocated memory
      }
    }
    
    // Small delay to prevent task starvation
    vTaskDelay(pdMS_TO_TICKS(1));
  }
}

// ============================= PHASE 2: Asynchronous ESP-NOW =============================
// ESP-NOW message queue structure
typedef struct {
  uint8_t targetWCB;              // 0 for broadcast, 1-9 for specific WCB
  char message[200];              // Message to send
  bool isRawData;                 // True for raw data bridging
  uint8_t rawData[180];           // Raw data for bridging
  size_t rawDataLength;           // Length of raw data
} ESPNowQueueItem;

static QueueHandle_t espnowQueue = nullptr;

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
      if (debugEnabled){ }
      Serial.printf("No LED was chosen \n");
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

void turnOnLEDforBoot(){
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
void writeSerialStringOptimized(Stream &serialPort, const String &stringData);
void queueESPNowMessage(uint8_t target, const char *message);
void queueESPNowRaw(const uint8_t *data, size_t len);
void queueSerialMessage(int serialPort, const String &message);
void enqueueCommand(const String &cmd, int sourceID);
void runPerformanceBenchmark();
void updatePerformanceCounters(unsigned long commandTime);
void printPerformanceReport();

// ============================= PHASE 2: Asynchronous ESP-NOW Task =============================
// Non-blocking ESP-NOW queue function
void queueESPNowMessage(uint8_t target, const char *message) {
  if (!espnowQueue) return;
  
  ESPNowQueueItem item;
  item.targetWCB = target;
  item.isRawData = false;
  strncpy(item.message, message, sizeof(item.message) - 1);
  item.message[sizeof(item.message) - 1] = '\0';
  
  // Non-blocking queue send
  if (xQueueSend(espnowQueue, &item, 0) != pdTRUE) {
    if (debugEnabled) {
      Serial.println("ESP-NOW queue full, dropping message");
    }
  } else {
    espnowOutCount++;  // Track outgoing messages
  }
}

void queueESPNowRaw(const uint8_t *data, size_t len) {
  if (!espnowQueue || len > sizeof(ESPNowQueueItem::rawData)) return;
  
  ESPNowQueueItem item;
  item.targetWCB = 0; // Broadcast for raw data
  item.isRawData = true;
  item.rawDataLength = len;
  memcpy(item.rawData, data, len);
  
  // Non-blocking queue send
  if (xQueueSend(espnowQueue, &item, 0) != pdTRUE) {
    if (debugEnabled) {
      Serial.println("ESP-NOW raw queue full, dropping data");
    }
  }
}

// Asynchronous ESP-NOW transmission task
void espnowTransmissionTask(void *pvParameters) {
  ESPNowQueueItem item;
  
  while (true) {
    // Wait for ESP-NOW messages to send
    if (xQueueReceive(espnowQueue, &item, portMAX_DELAY) == pdTRUE) {
      
      if (item.isRawData) {
        // Handle raw data transmission
        size_t offset = 0;
        while (offset < item.rawDataLength) {
          size_t chunkSize = item.rawDataLength - offset;
          if (chunkSize > 180) chunkSize = 180;
          
          espnow_struct_message msg;
          memset(&msg, 0, sizeof(msg));
          
          strncpy(msg.structPassword, espnowPassword, sizeof(msg.structPassword) - 1);
          snprintf(msg.structSenderID, sizeof(msg.structSenderID), "%d", WCB_Number);
          snprintf(msg.structTargetID, sizeof(msg.structTargetID), "9");
          msg.structCommandIncluded = true;
          
          msg.structCommand[0] = (uint8_t)(chunkSize & 0xFF);
          msg.structCommand[1] = (uint8_t)((chunkSize >> 8) & 0xFF);
          memcpy(msg.structCommand + 2, item.rawData + offset, chunkSize);
          
          esp_now_send(broadcastMACAddress[0], (uint8_t*)&msg, sizeof(msg));
          offset += chunkSize;
        }
      } else {
        // Handle regular message transmission
        // Skip broadcast if last was from ESP-NOW
        if (item.targetWCB == 0 && lastReceivedViaESPNOW) {
          continue;
        }
        
        espnow_struct_message msg;
        memset(&msg, 0, sizeof(msg));
        
        strncpy(msg.structPassword, espnowPassword, sizeof(msg.structPassword) - 1);
        snprintf(msg.structSenderID, sizeof(msg.structSenderID), "%d", WCB_Number);
        
        if (item.targetWCB == 0) {
          snprintf(msg.structTargetID, sizeof(msg.structTargetID), "0");
        } else {
          snprintf(msg.structTargetID, sizeof(msg.structTargetID), "%d", item.targetWCB);
        }
        
        msg.structCommandIncluded = true;
        strncpy(msg.structCommand, item.message, sizeof(msg.structCommand) - 1);
        
        uint8_t *mac = (item.targetWCB == 0) ? broadcastMACAddress[0] : WCBMacAddresses[item.targetWCB - 1];
        
        esp_err_t result = esp_now_send(mac, (uint8_t *)&msg, sizeof(msg));
        if (debugEnabled && result != ESP_OK) {
          Serial.printf("ESP-NOW async send failed! Error: %d\n", result);
        }
      }
    }
    
    // Small delay to prevent task starvation
    vTaskDelay(pdMS_TO_TICKS(1));
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
      reusableBuffer2 = data.substring(startIdx);
      reusableBuffer2.trim();
      if (!reusableBuffer2.isEmpty()) {
        if (!reusableBuffer2.startsWith(commentDelimiter)) {
          enqueueCommand(reusableBuffer2, sourceID);
        } else {
          if (debugEnabled) Serial.printf("Ignored chain command: %s\n", reusableBuffer2.c_str());
        }
      }
      break;
    } else {
      reusableBuffer2 = data.substring(startIdx, delimPos);
      reusableBuffer2.trim();
      if (!reusableBuffer2.isEmpty()) {
        if (!reusableBuffer2.startsWith(commentDelimiter)) {
          enqueueCommand(reusableBuffer2, sourceID);
        } else {
          if (debugEnabled) Serial.printf("Ignored chain command: %s\n", reusableBuffer2.c_str());
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
  Serial.printf("Software Version %s\n", SoftwareVersion);
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
  Serial.println("--- End of Configuration Info ---\n");
}

//*******************************
/// ESP-NOW Functions - Now calling async queue
//*******************************
void sendESPNowMessage(uint8_t target, const char *message) {
  queueESPNowMessage(target, message);
}

void sendESPNowRaw(const uint8_t *data, size_t len) {
  queueESPNowRaw(data, len);
}

void espNowReceiveCallback(const esp_now_recv_info_t *info, const uint8_t *incomingData, int len) {
  if (len != sizeof(espnow_struct_message)) {
      return;
  }

  espnow_struct_message received;
  memcpy(&received, incomingData, sizeof(received));
  received.structPassword[sizeof(received.structPassword) - 1] = '\0';

  reusableBuffer3 = String(received.structPassword);
  if (reusableBuffer3 == espnowPassword){
    Serial.printf("Received Command: %s\n", received.structCommand);

    int senderWCB = atoi(received.structSenderID);
    int targetWCB = atoi(received.structTargetID);

    Serial.printf("Sender ID: WCB%d, Target ID: WCB%d\n", senderWCB, targetWCB);

    if (info->src_addr[1] != umac_oct2 || info->src_addr[2] != umac_oct3) {
        if (debugEnabled){Serial.println("Received message from an unrelated WCB group, ignoring."); } 
        return;
    }
    if (targetWCB == 9) {
        size_t chunkLen = (uint8_t)received.structCommand[0] | ((uint8_t)received.structCommand[1] << 8);

        if (chunkLen > 180 || chunkLen == 0) {
            if (debugEnabled) {
                Serial.printf("Invalid bridging chunk length: %d, Ignoring.\n", (int)chunkLen);
            }
            return;
        }

        if (Kyber_Local){ 
          Serial1.write((uint8_t*)(received.structCommand + 2), chunkLen);
          Serial2.write((uint8_t*)(received.structCommand + 2), chunkLen); 
          }  
        else if (Kyber_Remote) {
          Serial1.write((uint8_t*)(received.structCommand + 2), chunkLen);
        }      

        if (debugEnabled) {
        }
        return;
    }

     lastReceivedViaESPNOW = true;
      colorWipeStatus("ES", green, 200);
    if (targetWCB != 0 && targetWCB != WCB_Number ) {
        Serial.println("Message not for this WCB, ignoring.");
        return;
    }

    enqueueCommand(String(received.structCommand), 0);
    espnowInCount++;  // Track incoming messages
  }
    colorWipeStatus("ES", blue, 10);
}

//*******************************
/// Kyber Serial Forwarding Commands
//*******************************
void forwardDataFromKyber() {
  static uint8_t buffer[64];

  if (Serial2.available() > 0) {
    int len = Serial2.readBytes((char*)buffer, sizeof(buffer));
    Serial1.write(buffer, len);
    // No flush() - let hardware handle it
    queueESPNowRaw(buffer, len);
  }
}

void forwardMaestroDataToLocalKyber() {
  static uint8_t buffer[64];

  if (Serial1.available() > 0) {
    int len = Serial1.readBytes((char*)buffer, sizeof(buffer));
    Serial2.write(buffer, len);
    // No flush() - let hardware handle it
    if (Kyber_Remote){queueESPNowRaw(buffer, len);}
  }
}

void forwardMaestroDataToRemoteKyber() {
  static uint8_t buffer[64];

  if (Serial1.available() > 0) {
    int len = Serial1.readBytes((char*)buffer, sizeof(buffer));
    queueESPNowRaw(buffer, len);
  }
}

// ============================= Performance Benchmark Function =============================
void runPerformanceBenchmark() {
  Serial.println("Running performance benchmark for variable message lengths...");
  
  // Create test messages of varying lengths
  String testMessages[180];
  for (int i = 0; i < 180; i++) {
    // Create messages of varying complexity and length
    switch (i % 6) {
      case 0: testMessages[i] = "short_cmd"; break;
      case 1: testMessages[i] = "medium_length_command_test"; break;
      case 2: testMessages[i] = "very_long_command_with_multiple_parameters_and_data"; break;
      case 3: testMessages[i] = "testing231"; break;
      case 4: testMessages[i] = ";S1test_serial_message"; break;
      case 5: testMessages[i] = "broadcast_message_to_all_devices_with_some_extra_data_" + String(i); break;
    }
  }
  
  // Measure parsing time
  unsigned long startTime = micros();
  
  for (int i = 0; i < 180; i++) {
    // Simulate the parsing that would happen
    parseCommandsAndEnqueue(testMessages[i], 0);
  }
  
  unsigned long totalTime = micros() - startTime;
  unsigned long avgTime = totalTime / 180;
  
  Serial.printf("Variable message parsing: %lu µs for 180 messages (%lu µs avg)\n", totalTime, avgTime);
  Serial.println("Benchmark complete - system optimized for variable message lengths");
}

//*******************************
/// Processing Input Functions - PHASE 1: WITH PROFILING
//*******************************
void handleSingleCommand(const String &cmd, int sourceID) {
    // PHASE 1: Detailed profiling
    unsigned long start = micros();
    unsigned long checkpoint;
    
    if (debugEnabled) {
        Serial.printf("🔍 CMD START: '%s' (src:%d) ", cmd.c_str(), sourceID);
    }

    // 1) LocalFunctionIdentifier-based commands (e.g., `?commands`)
    if (cmd.startsWith(String(LocalFunctionIdentifier))) {
        checkpoint = micros();
        processLocalCommand(cmd.substring(1));
        if (debugEnabled) {
            Serial.printf("Local:%luμs ", micros() - checkpoint);
        }
    } 
    // 2) CommandCharacter-based commands (e.g., `;commands`)
    else if (cmd.startsWith(String(CommandCharacter))) {
        checkpoint = micros();
        processCommandCharcter(cmd.substring(1), sourceID);
        if (debugEnabled) {
            Serial.printf("Command:%luμs ", micros() - checkpoint);
        }
    } 
    // 3) Default to broadcasting if not a recognized prefix
    else {
        checkpoint = micros();
        processBroadcastCommand(cmd, sourceID);
        if (debugEnabled) {
            Serial.printf("Broadcast:%luμs ", micros() - checkpoint);
        }
    }
    
    // Total timing and performance tracking
    unsigned long total = micros() - start;
    updatePerformanceCounters(total);
    
    if (debugEnabled) {
        Serial.printf("TOTAL:%luμs", total);
        if (total > 50000) {  // > 50ms
            Serial.print(" ⚠️SLOW⚠️");
        }
        Serial.println();
    }
}

//*******************************
/// Processing Local Function Identifier 
//*******************************
void processLocalCommand(const String &message) {
    if (message == "don" || message == "DON") {
        debugEnabled = true;
        Serial.println("Debugging enabled");
        return;
    } else if (message == "doff" || message == "DOFF") {
        debugEnabled = false;
        Serial.println("Debugging disabled");
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
    } else if (message.equals("benchmark") || message.equals("BENCHMARK")) {
        runPerformanceBenchmark();
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
    String hexValue = message.substring(2, 4);
    int newValue = strtoul(hexValue.c_str(), NULL, 16);
    umac_oct2 = static_cast<uint8_t>(newValue);
    saveMACPreferences();
    Serial.printf("Updated the 2nd Octet to 0x%02X\n", umac_oct2);
}

void update3rdMACOctet(const String &message){
  String hexValue = message.substring(2, 4);
  int newValue = strtoul(hexValue.c_str(), NULL, 16);
  umac_oct3 = static_cast<uint8_t>(newValue);
  saveMACPreferences();
  Serial.printf("Updated the 3rd Octet to 0x%02X\n", umac_oct3);
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
  reusableBuffer3 = message.substring(5);
  if (reusableBuffer3.length() > 0 && reusableBuffer3.length() < sizeof(espnowPassword)) {
    setESPNowPassword(reusableBuffer3.c_str());
    Serial.printf("ESP-NOW Password updated to: %s\n", reusableBuffer3.c_str());
  } else {
    Serial.println("Invalid ESP-NOW password length. Must be between 1 and 39 characters.");
  }
}

void enableMaestroSerialBaudRate(){
    recallBaudRatefromSerial(1);
    Serial.printf("Saved original baud rate of %d for Serial 1\n, Updating to 57600 to support Maestro\n", storedBaudRate[1], 1);
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

    int target = message.substring(1, 2).toInt();
    if (target < 1 || target > 5) {
        Serial.println("Invalid serial port number. Must be between 1 and 5.");
        return;
    }

    reusableBuffer3 = message.substring(2);
    reusableBuffer3.trim();

    // Use async queue for serial transmission - NON-BLOCKING
    queueSerialMessage(target, reusableBuffer3);

    if (debugEnabled) {
        Serial.printf("Queued to Serial%d: %s\n", target, reusableBuffer3.c_str());
    }
}

void processWCBMessage(const String &message){
  int targetWCB = message.substring(1, 2).toInt();
        reusableBuffer3 = message.substring(2);
        if (targetWCB >= 1 && targetWCB <= Default_WCB_Quantity) {
          Serial.printf("Sending Unicast ESP-NOW message to WCB%d: %s\n", targetWCB, reusableBuffer3.c_str());
          queueESPNowMessage(targetWCB, reusableBuffer3.c_str());
        } else {
          Serial.println("Invalid WCB number for unicast.");
        }
}

void recallStoredCommand(const String &message, int sourceID) {
  reusableBuffer3 = message.substring(1);
  reusableBuffer3.trim();
  if (reusableBuffer3.length() > 0) {
    recallCommandSlot(reusableBuffer3, sourceID);
  } else {
    Serial.println("Invalid recall command. Use ;Ckey to recall a command.");
  }
}

void processMaestroCommand(const String &message){
  int message_maestroID = message.substring(1,2).toInt();
  int message_maestroSeq = message.substring(2).toInt();
  sendMaestroCommand(message_maestroID,message_maestroSeq);
}

//*******************************
/// Processing Broadcast Function - PHASE 5: FULLY ASYNCHRONOUS
//*******************************
void processBroadcastCommand(const String &cmd, int sourceID) {
    if (debugEnabled) {
        Serial.printf("Broadcasting command: %s\n", cmd.c_str());
    }

    // Queue to all serial ports except restricted ones - FULLY NON-BLOCKING
    for (int i = 1; i <= 5; i++) {
        if ((i == 1 && Kyber_Remote) || 
            (i <= 2 && Kyber_Local) || 
            i == sourceID || !serialBroadcastEnabled[i - 1]) {
            continue;
        }

        queueSerialMessage(i, cmd);  // Fully asynchronous!
        if (debugEnabled) { 
          Serial.printf("Queued to Serial%d: %s\n", i, cmd.c_str()); 
        }
    }

    // Always send via ESP-NOW broadcast - ASYNCHRONOUS
    queueESPNowMessage(0, cmd.c_str());
    if (debugEnabled) { 
      Serial.printf("Queued ESP-NOW broadcast: %s\n", cmd.c_str()); 
    }
}

// processIncomingSerial for each serial port
void processIncomingSerial(Stream &serial, int sourceID) {
  if (!serial.available()) return;

  static String serialBuffers[6];
  String &serialBuffer = serialBuffers[sourceID];
  while (serial.available()) {
    char c = serial.read();
    if (c == '\r' || c == '\n') {
      if (!serialBuffer.isEmpty()) {
          serialBuffer.trim();
          if (debugEnabled){Serial.printf("Processing input from Serial%d: %s\n", sourceID, serialBuffer.c_str());} 

          lastReceivedViaESPNOW = false;

          processSerialCommandHelper(serialBuffer, sourceID);

          serialBuffer = "";
      }
    } else {
      serialBuffer += c;
    }
  }
}

// Helper function to process serial commands
void processSerialCommandHelper(const String &data, int sourceID) {
    if (debugEnabled) {
        Serial.printf("Processing command from Serial%d: %s\n", sourceID, data.c_str());
    }

    if (data.length() == 0) return;

    if (data.startsWith(String(LocalFunctionIdentifier) + "C") || 
        data.startsWith(String(LocalFunctionIdentifier) + "c")) {
        enqueueCommand(data, sourceID);
        return;
    }

    // Parse command using the stored delimiter
    int startIdx = 0;
    while (true) {
        int delimPos = data.indexOf(commandDelimiter, startIdx);
        if (delimPos == -1) {
            reusableBuffer1 = data.substring(startIdx);
            reusableBuffer1.trim();
            if (!reusableBuffer1.isEmpty()) {
                enqueueCommand(reusableBuffer1, sourceID);
            }
            break;
        } else {
            reusableBuffer1 = data.substring(startIdx, delimPos);
            reusableBuffer1.trim();
            if (!reusableBuffer1.isEmpty()) {
                enqueueCommand(reusableBuffer1, sourceID);
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

/// Task Definition for multi threading. Act as separate loops within the main loop.
void KyberLocalTask(void *pvParameters) {
    while (true) {
        forwardMaestroDataToLocalKyber();
        forwardDataFromKyber();
        vTaskDelay(pdMS_TO_TICKS(1));
    }
}

void KyberRemoteTask(void *pvParameters) {
    while (true) {
        forwardMaestroDataToRemoteKyber();
        vTaskDelay(pdMS_TO_TICKS(1));
    }
}

void serialCommandTask(void *pvParameters) {
  vTaskDelay(pdMS_TO_TICKS(500));
    while (true) {
        processIncomingSerial(Serial, 0);
        processIncomingSerial(Serial3, 3);
        processIncomingSerial(Serial4, 4);
        processIncomingSerial(Serial5, 5);

        if (Kyber_Local) {
        } else if (Kyber_Remote){
            processIncomingSerial(Serial2, 2);
        } else {
          processIncomingSerial(Serial1, 1);
          processIncomingSerial(Serial2, 2);
        }

        vTaskDelay(pdMS_TO_TICKS(5));
    }
}

void initStatusLEDWithRetry(int maxRetries = 10, int delayBetweenMs = 200) {
  int attempt = 0;
  bool initialized = false;

  while (attempt < maxRetries && !initialized) {
    if (debugEnabled) Serial.printf("Attempting NeoPixel init (try %d)...\n", attempt + 1);

    pinMode(STATUS_LED_PIN, OUTPUT);
    digitalWrite(STATUS_LED_PIN, LOW);
    delay(750);
    statusLED = new Adafruit_NeoPixel(STATUS_LED_COUNT, STATUS_LED_PIN, NEO_GRB + NEO_KHZ800);
    delay(750);
    statusLED->begin();

    statusLED->setPixelColor(0, statusLED->Color(0, 0, 5));
    statusLED->show();
    delay(750);

    if (statusLED->getPixelColor(0) != 0) {
      initialized = true;
    } else {
      delete statusLED;
      statusLED = nullptr;
      delay(delayBetweenMs);
    }

    attempt++;
  }

  if (!initialized) {
    Serial.println("⚠ Failed to initialize NeoPixel after retries.");
  }
}

// ============================= Setup & Loop =============================

void setup() {

  Serial.begin(115200);
  delay(1000);
  while (Serial.available()) Serial.read();
  
  // PHASE 4: Initialize optimized string buffers
  initializeStringBuffers();
  
  loadHWversion();
  loadKyberSettings();
  printResetReason();
  loadWCBNumberFromPreferences();
  loadWCBQuantitiesFromPreferences();
  loadMACPreferences();

  if (wcb_hw_version == 0){
    Serial.println("No LED was setup.  Define HW version");
  } else if (wcb_hw_version == 1){
    pinMode(ONBOARD_LED, OUTPUT);
  } else if (wcb_hw_version == 21 ||  wcb_hw_version == 23 || wcb_hw_version == 24) {
    statusLED = new  Adafruit_NeoPixel(STATUS_LED_COUNT, STATUS_LED_PIN, NEO_GRB + NEO_KHZ800);
    statusLED->begin();
    statusLED->show();
  } else if (wcb_hw_version == 31){
      initStatusLEDWithRetry(10, 200);
  } 

  delay(1000);
  turnOnLEDforBoot();
  
  // Create the command queue - OPTIMIZED SIZE
  commandQueue = xQueueCreate(500, sizeof(CommandQueueItem));
  if (!commandQueue) {
    Serial.println("Failed to create command queue!");
  }
  
  // PHASE 2: Create ESP-NOW async transmission queue
  espnowQueue = xQueueCreate(100, sizeof(ESPNowQueueItem));
  if (!espnowQueue) {
    Serial.println("Failed to create ESP-NOW queue!");
  }
  
  // PHASE 5: Create serial port queues
  for (int i = 0; i < 5; i++) {
    serialQueues[i] = xQueueCreate(50, sizeof(SerialQueueItem)); // 50 messages per serial port
    if (!serialQueues[i]) {
      Serial.printf("Failed to create Serial%d queue!\n", i + 1);
    }
  }

  Serial.println("\n\n-------------------------------------------------------");
  String hostname = getBoardHostname();
  Serial.printf("Booting up the %s\n", hostname.c_str());
  printHWversion();
  Serial.printf("Software Version: %s\n", SoftwareVersion.c_str());
  Serial.printf("Number of WCBs in the system: %d\n", Default_WCB_Quantity);
  Serial.println("-------------------------------------------------------");
  loadBaudRatesFromPreferences();
  printBaudRates();
  Serial.println("-------------------------------------------------------");

  Serial1.begin(baudRates[0], SERIAL_8N1, SERIAL1_RX_PIN, SERIAL1_TX_PIN);
  Serial2.begin(baudRates[1], SERIAL_8N1, SERIAL2_RX_PIN, SERIAL2_TX_PIN);
  Serial3.begin(baudRates[2], SWSERIAL_8N1, SERIAL3_RX_PIN, SERIAL3_TX_PIN, false, 95);
  Serial4.begin(baudRates[3], SWSERIAL_8N1, SERIAL4_RX_PIN, SERIAL4_TX_PIN, false, 95);
  Serial5.begin(baudRates[4], SWSERIAL_8N1, SERIAL5_RX_PIN, SERIAL5_TX_PIN, false, 95);

  WiFi.mode(WIFI_STA);

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
  esp_wifi_set_mac(WIFI_IF_STA, WCBMacAddresses[WCB_Number - 1]);
  
  loadESPNowPasswordFromPreferences();

  uint8_t baseMac[6];
  esp_wifi_get_mac(WIFI_IF_STA, baseMac);
  Serial.printf("ESP-NOW MAC Address: %02X:%02X:%02X:%02X:%02X:%02X\n",
                baseMac[0], baseMac[1], baseMac[2], baseMac[3], baseMac[4], baseMac[5]);

  if (esp_now_init() != ESP_OK) {
    Serial.println("Error initializing ESP-NOW");
    return;
  }

  for (int i = 0; i < Default_WCB_Quantity; i++) {
    if (i + 1 == WCB_Number) continue;
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

  loadCommandDelimiter();
  loadLocalFunctionIdentifierAndCommandCharacter();
  loadStoredCommandsFromPreferences();

  Serial.printf("Delimeter Character: %c\n", commandDelimiter);
  Serial.printf("Local Function Identifier: %c\n", LocalFunctionIdentifier);
  Serial.printf("Command Character: %c\n", CommandCharacter);
  Serial.println("-------------------------------------------------------");
  printKyberSettings();
  esp_now_register_recv_cb(espNowReceiveCallback);
  
  Serial.println("Performance Optimizations Active:");
  Serial.println("- Asynchronous ESP-NOW transmission");
  Serial.println("- Asynchronous serial port broadcasting");
  Serial.println("- Non-blocking serial operations");
  Serial.println("- Pre-allocated string buffers");
  Serial.println("- Detailed command profiling");
  Serial.println("Use ?benchmark to run performance test");
  Serial.println("-------------------------------------------------------");

  // Create FreeRTOS Tasks
  xTaskCreatePinnedToCore(serialCommandTask, "Serial Command Task", 4096, NULL, 1, NULL, 1);
  
  // PHASE 2: Create asynchronous ESP-NOW transmission task
  xTaskCreatePinnedToCore(espnowTransmissionTask, "ESP-NOW Transmission Task", 4096, NULL, 2, NULL, 0);
  Serial.println("ESP-NOW Async Task Created");
  
  // PHASE 5: Create asynchronous serial broadcasting task
  xTaskCreatePinnedToCore(serialBroadcastTask, "Serial Broadcast Task", 4096, NULL, 1, NULL, 1);
  Serial.println("Serial Async Broadcast Task Created");

  if (Kyber_Local) {
      xTaskCreatePinnedToCore(KyberLocalTask, "Kyber Local Task", 4096, NULL, 1, NULL, 1);
      Serial.println("Kyber_Local Task Created");
  }    
  if (Kyber_Remote) {
      xTaskCreatePinnedToCore(KyberRemoteTask, "Kyber Remote Task", 4096, NULL, 1, NULL, 1);
      Serial.println("Kyber_Remote Task Created");
  }

  delay(150);     
  turnOffLED();
  
  // Initialize performance monitoring
  lastPerfCheck = millis();
}

void loop() {

  // Handle all queued commands
  if (!commandQueue) return;
  CommandQueueItem inItem;
  while (xQueueReceive(commandQueue, &inItem, 0) == pdTRUE) {
    // PHASE 4: Use const reference to avoid string copy
    const String commandStr(inItem.cmd);
    free(inItem.cmd); // release dynamic memory
    handleSingleCommand(commandStr, inItem.sourceID);
  }
  
  // Print performance report every 30 seconds
  printPerformanceReport();
}
