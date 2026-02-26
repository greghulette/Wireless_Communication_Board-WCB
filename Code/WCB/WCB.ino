
/*
____    __    ____  __  .______       _______  __       _______      _______.     _______.                                           
\   \  /  \  /   / |  | |   _  \     |   ____||  |     |   ____|    /       |    /       |                                           
 \   \/    \/   /  |  | |  |_)  |    |  |__   |  |     |  |__      |   (----`   |   (----`                                           
  \            /   |  | |      /     |   __|  |  |     |   __|      \   \        \   \                                               
   \    /\    /    |  | |  |\  \----.|  |____ |  `----.|  |____ .----)   |   .----)   |                                              
    \__/  \__/     |__| | _| `._____||_______||_______||_______||_______/    |_______/                                               
                                                                                                                                     
  ______   ______   .___  ___. .___  ___.  __    __  .__   __.  __    ______      ___      .___________. __    ______   .__   __.    
 /      | /  __  \  |   \/   | |   \/   | |  |  |  | |  \ |  | |  |  /      |    /   \     |           ||  |  /  __  \  |  \ |  |    
|  ,----'|  |  |  | |  \  /  | |  \  /  | |  |  |  | |   \|  | |  | |  ,----'   /  ^  \    `---|  |----`|  | |  |  |  | |   \|  |    
|  |     |  |  |  | |  |\/|  | |  |\/|  | |  |  |  | |  . `  | |  | |  |       /  /_\  \       |  |     |  | |  |  |  | |  . `  |    
|  `----.|  `--'  | |  |  |  | |  |  |  | |  `--'  | |  |\   | |  | |  `----. /  _____  \      |  |     |  | |  `--'  | |  |\   |    
 \______| \______/  |__|  |__| |__|  |__|  \______/  |__| \__| |__|  \______|/__/     \__\     |__|     |__|  \______/  |__| \__|    
                                                                                                                                     
.______     ______        ___      .______       _______          _______    __    ____   ______ .______   ___                       
|   _  \   /  __  \      /   \     |   _  \     |       \        /  /\   \  /  \  /   /  /      ||   _  \  \  \                      
|  |_)  | |  |  |  |    /  ^  \    |  |_)  |    |  .--.  |      |  |  \   \/    \/   /  |  ,----'|  |_)  |  |  |                     
|   _  <  |  |  |  |   /  /_\  \   |      /     |  |  |  |      |  |   \            /   |  |     |   _  <   |  |                     
|  |_)  | |  `--'  |  /  _____  \  |  |\  \----.|  '--'  |      |  |    \    /\    /    |  `----.|  |_)  |  |  |                     
|______/   \______/  /__/     \__\ | _| `._____||_______/       |  |     \__/  \__/      \______||______/   |  |                     
                                                                 \__\                                      /__/                                                  
*/                                                                                                 
                                                                                                                
/////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////
///*****                                                                                                        *****////
///*****                                          Created by Greg Hulette.                                      *****////
///*****                                          Version 6.0_260926RFEB2026                                    *****////
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
///*****                       - PWM Passthrough natively with PWM Mapping Commands                             *****////
///*****                       - Complete Control of broadcast messages                                         *****////
///*****                                                                                                        *****////
///*****                            Full command syntax and description can be found at                         *****////
///*****                      https://github.com/greghulette/Wireless_Communication_Board-WCB/wiki              *****////
///*****                                                                                                        *****////
/////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////

/*
▄▄▄▄  ▄▄▄  ▄▄▄▄   ▄▄▄▄   ▄▄▄▄▄▄▄   ▄▄▄    ▄▄▄ ▄▄▄▄▄ ▄▄▄    ▄▄▄  ▄▄▄▄▄▄▄      ▄▄▄  ▄▄▄  ▄▄▄▄▄▄▄  ▄▄▄▄▄▄▄      ▄▄▄▄▄▄▄  ▄▄▄▄▄▄▄ ▄▄▄▄▄▄▄   ▄▄▄▄▄▄▄  ▄▄▄▄▄▄▄  
▀███  ███  ███▀ ▄██▀▀██▄ ███▀▀███▄ ████▄  ███  ███  ████▄  ███ ███▀▀▀▀▀      ███  ███ █████▀▀▀ ███▀▀▀▀▀     ███▀▀▀▀▀ █████▀▀▀ ███▀▀███▄ ▀▀▀▀████ ▀▀▀▀████ 
 ███  ███  ███  ███  ███ ███▄▄███▀ ███▀██▄███  ███  ███▀██▄███ ███           ███  ███  ▀████▄  ███▄▄        ███▄▄     ▀████▄  ███▄▄███▀   ▄▄██▀     ▄██▀  
 ███▄▄███▄▄███  ███▀▀███ ███▀▀██▄  ███  ▀████  ███  ███  ▀████ ███  ███▀     ███▄▄███    ▀████ ███          ███         ▀████ ███▀▀▀▀       ███▄  ▄███▄▄▄ 
  ▀████▀████▀   ███  ███ ███  ▀███ ███    ███ ▄███▄ ███    ███ ▀██████▀      ▀██████▀ ███████▀ ▀███████     ▀███████ ███████▀ ███       ███████▀ ████████ 
                                                                                                                                                         
▄▄▄▄▄▄▄     ▄▄▄▄▄     ▄▄▄▄   ▄▄▄▄▄▄▄   ▄▄▄▄▄▄       ▄▄▄▄▄▄▄     ▄▄▄▄    ▄▄▄▄▄▄▄ ▄▄▄   ▄▄▄   ▄▄▄▄    ▄▄▄▄▄▄▄   ▄▄▄▄▄▄▄                                     
███▀▀███▄ ▄███████▄ ▄██▀▀██▄ ███▀▀███▄ ███▀▀██▄     ███▀▀███▄ ▄██▀▀██▄ ███▀▀▀▀▀ ███ ▄███▀ ▄██▀▀██▄ ███▀▀▀▀▀  ███▀▀▀▀▀                                     
███▄▄███▀ ███   ███ ███  ███ ███▄▄███▀ ███  ███     ███▄▄███▀ ███  ███ ███      ███████   ███  ███ ███       ███▄▄                                        
███  ███▄ ███▄▄▄███ ███▀▀███ ███▀▀██▄  ███  ███     ███▀▀▀▀   ███▀▀███ ███      ███▀███▄  ███▀▀███ ███  ███▀ ███                                          
████████▀  ▀█████▀  ███  ███ ███  ▀███ ██████▀      ███       ███  ███ ▀███████ ███  ▀███ ███  ███ ▀██████▀  ▀███████                                     
                                                                                                                                                          
▄▄▄▄▄▄▄     ▄▄▄▄▄▄▄     ▄▄▄  ▄▄▄         ▄▄▄▄▄   ▄▄▄    ▄▄▄ ▄▄▄      ▄▄▄   ▄▄▄                                                                            
▀▀▀▀████    ▀▀▀▀████    ███  ███       ▄███████▄ ████▄  ███ ███      ███   ███                                                                            
  ▄▄██▀       ▄▄██▀     ███▄▄███       ███   ███ ███▀██▄███ ███      ▀███▄███▀                                                                            
    ███▄        ███▄     ▀▀▀████       ███▄▄▄███ ███  ▀████ ███        ▀███▀                                                                              
███████▀ ██ ███████▀ ██     ████        ▀█████▀  ███    ███ ████████    ███                                                                               
*/

// ============================= Librarires  =============================
//You must install these into your Arduino IDE
#include <SoftwareSerial.h>                 // ESPSoftwareSerial library by Dirk Kaar, Peter Lerup V8.1.0
#include <Adafruit_NeoPixel.h>              // Adafruit NeoPixel library by Adafruit V1.15.3

//  All of these librarires are included in the ESP32 by Espressif board library V3.3.4
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
#include "WCB_Help.h"

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
bool debugKyber = false;
bool debugPWMEnabled = false;
bool debugPWMPassthrough = false;  // Debug flag for PWM passthrough operations
// WCB Board HW and SW version Variables
int wcb_hw_version = 0;  // Default = 0, Version 1.0 = 1 Version 2.1 = 21, Version 2.3 = 23, Version 2.4 = 24, Version 3.1 = 31, Version 3.2 = 32
String SoftwareVersion = "6.0_260926RFEB2026";

// ESP-NOW Statistics
unsigned long espnowSendAttempts = 0;
unsigned long espnowSendSuccess = 0;
unsigned long espnowSendFailed = 0;

// Regular command messages
unsigned long espnowCommandAttempts = 0;
unsigned long espnowCommandSuccess = 0;
unsigned long espnowCommandFailed = 0;

// PWM passthrough messages
unsigned long espnowPWMAttempts = 0;
unsigned long espnowPWMSuccess = 0;
unsigned long espnowPWMFailed = 0;

// Raw data (Kyber)
unsigned long espnowRawAttempts = 0;
unsigned long espnowRawSuccess = 0;
unsigned long espnowRawFailed = 0;
// ============================= ETM - Ensured Transmission Mode =============================
// ETM Structs
typedef struct __attribute__((packed)) {
  char structPassword[40];
  char structSenderID[4];
  char structTargetID[4];
  uint8_t structCommandIncluded;
  char structCommand[200];
  uint8_t structPacketType;
  uint16_t structSequenceNumber;
} espnow_struct_message_etm;

// ETM Packet Types
#define PACKET_TYPE_COMMAND   0
#define PACKET_TYPE_ACK       1
#define PACKET_TYPE_HEARTBEAT 2

// ETM Settings (NVS stored)
bool debugETM = false;
bool etmEnabled = false;
int etmBootHeartbeatSec = 2;
int etmHeartbeatSec = 10;
int etmMissedHeartbeats = 3;
int etmTimeoutMs = 500;
int etmCharMessageCount = 20;
int etmCharDelayMs = 100;

// ETM Board Status Table
struct BoardStatus {
  bool online;
  unsigned long lastSeenMs;
  unsigned long messagesSent;
  unsigned long messagesAckd;
  unsigned long totalRetries;
  unsigned long totalFailed;
};
BoardStatus boardTable[9];  // sized to max, we only use [0..Default_WCB_Quantity-1]

// ETM Heartbeat timing
unsigned long etmNextHeartbeatMs = 0;  // when to send next heartbeat
bool etmBootHeartbeatSent = false;
uint16_t etmSequenceNumber = 0;        // global sequence counter, Layer 3 uses this

// ETM - Pending Message Table
#define ETM_PENDING_MAX 10
struct PendingMessage {
  bool active;
  uint16_t sequenceNumber;
  char command[200];
  unsigned long lastSentMs;
  int targetWCB;  // ADD THIS - 0 = broadcast, 1-9 = unicast target
  bool expectAckFrom[9];
  bool receivedAckFrom[9];
  uint8_t retryCount[9];
};
PendingMessage etmPendingTable[ETM_PENDING_MAX];

// ETM - Sequence counter
uint16_t etmSequenceCounter = 0;

// ETM - Per-board stats (boardTable already has online/lastSeen from Layer 2)
unsigned long etmStatsSent[9]    = {0};
unsigned long etmStatsAckd[9]    = {0};
unsigned long etmStatsRetries[9] = {0};
unsigned long etmStatsFailed[9]  = {0};

// ETM - Duplicate detection (last received seq per board)
uint16_t etmLastReceivedSeq[9] = {0};
bool etmLastReceivedSeqValid[9] = {false};
// ETM Characterization state
bool etmCharRunning = false;
int etmCharPhase = 0;
int etmCharTargetWCB = 0;  // 0 = not set

// Per-phase, per-board tracking
struct ETMCharBoardResult {
    int sent;
    int acked;
    unsigned long latencyMin;
    unsigned long latencyMax;
    unsigned long latencyAccum;
};

// [phase 0-2][board index 0-7]
ETMCharBoardResult etmCharBoardResults[3][8];
unsigned long etmCharPhaseSentTimes[3][200];  // [phase][msgIndex]
int etmCharPhaseMessageIndex = 0;
unsigned long etmCharPhaseStartTime = 0;
unsigned long etmCharLastSendTime = 0;

// ETM Load test (phase 3 - received ETMLOAD command)
bool etmLoadRunning = false;
unsigned long etmLoadStartTime = 0;
unsigned long etmLoadLastSendTime = 0;
int etmLoadRoundRobinIndex = 0;

// Delivery confirmation tracking
unsigned long espnowCommandDelivered = 0;

// Delivery tracking enable flags (can toggle each independently)
bool trackCommandDelivery = true;

// Serial port monitoring/mirroring
bool serialMonitorEnabled[5] = {false, false, false, false, false};  // Which ports to monitor
bool mirrorToUSB = true;      // Mirror monitored ports to USB Serial
bool mirrorToKyber = false;   // Mirror monitored ports to Kyber serial

// Broadcast blocking
bool blockBroadcastFrom[5] = {false, false, false, false, false};  // Block broadcasts from these ports

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
String serialPortLabels[5] = {"", "", "", "", ""};

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

extern bool serialMonitorEnabled[5];

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
  if (wcb_hw_version == 21 || wcb_hw_version == 23 || wcb_hw_version == 24 || wcb_hw_version == 31 || wcb_hw_version == 32){
    // Add this nullptr check:
    if (statusLED == nullptr) {
      if (debugEnabled) Serial.println("⚠️ statusLED is nullptr, skipping LED operation");
      return;
    }
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
   }else if (wcb_hw_version == 21 ||  wcb_hw_version == 23 || wcb_hw_version == 24 || wcb_hw_version == 32 || wcb_hw_version ==31){
    if (statusLED != nullptr) {  // ADD THIS CHECK
    colorWipeStatus("ES", green, 255);
    }
  }
}  


void   turnOnLEDforBoot(){
if (wcb_hw_version == 1){
  digitalWrite(ONBOARD_LED, HIGH); 
   } else if (wcb_hw_version == 21 ||  wcb_hw_version == 23 || wcb_hw_version == 24 ||wcb_hw_version == 31 || wcb_hw_version == 32){
    if (statusLED != nullptr) {  // ADD THIS CHECK
      colorWipeStatus("ES", red, 255);
    }
  } else {
    Serial.println("No LED yet defined");
  }
};

void turnOnLEDSerial(){
  if (wcb_hw_version == 21 ||  wcb_hw_version == 23 || wcb_hw_version == 24 || wcb_hw_version == 32 || wcb_hw_version ==31){
    if (statusLED != nullptr) {  // ADD THIS CHECK
    colorWipeStatus("ES", red, 255);
    }
  }
}  

void turnOnLEDSerialOut(){
  if (wcb_hw_version == 21 ||  wcb_hw_version == 23 || wcb_hw_version == 24 || wcb_hw_version == 32 || wcb_hw_version ==31){
    if (statusLED != nullptr) {  // ADD THIS CHECK
    colorWipeStatus("ES", orange, 255);
    }
  }
}  

void turnOffLED(){
  if (wcb_hw_version == 1 ){
    digitalWrite(ONBOARD_LED, LOW);   // Turns off the onboard Green LED
   }else if (wcb_hw_version == 21 ||  wcb_hw_version == 23 || wcb_hw_version == 24 || wcb_hw_version == 32 || wcb_hw_version ==31){
    colorWipeStatus("ES", blue, 10);
    
  }
}

// CRC32 calculation for backup verification
uint32_t calculateCRC32(const String &data) {
    const uint8_t *bytes = (const uint8_t *)data.c_str();
    size_t length = data.length();
    uint32_t crc = 0xFFFFFFFF;
    
    for (size_t i = 0; i < length; i++) {
        crc ^= bytes[i];
        for (int j = 0; j < 8; j++) {
            crc = (crc >> 1) ^ (0xEDB88320 & -(crc & 1));
        }
    }
    return ~crc;
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
#define MAX_STORED_COMMANDS 80
// String storedCommands[MAX_STORED_COMMANDS];

// ============================= Forward Declarations =============================
void writeSerialString(Stream &serialPort, String stringData);
void sendESPNowMessage(uint8_t target, const char *message, bool useETM = true);
void enqueueCommand(const String &cmd, int sourceID);
void processPWMPassthrough();
void addPWMOutputPort(int port);
void removePWMOutputPort(int port);
bool isSerialPortPWMOutput(int port);
void initStatusLEDWithRetry(int maxRetries = 10, int delayBetweenMs = 100);
void processIncomingSerial(Stream &serial, int sourceID); 
void etmSendAck(int senderWCB, uint16_t seqNum);
void etmProcessAck(int senderWCB, uint16_t seqNum);
int etmAddToPendingTable(uint16_t seqNum, const char* cmd);




void sendETMHeartbeat() {
  espnow_struct_message_etm hb;
  memset(&hb, 0, sizeof(hb));
  strncpy(hb.structPassword, espnowPassword, sizeof(hb.structPassword) - 1);
  snprintf(hb.structSenderID, sizeof(hb.structSenderID), "%d", WCB_Number);
  snprintf(hb.structTargetID, sizeof(hb.structTargetID), "0");  // broadcast
  hb.structCommandIncluded = false;
  hb.structPacketType = PACKET_TYPE_HEARTBEAT;
  hb.structSequenceNumber = 0;

  esp_now_send(broadcastMACAddress[0], (uint8_t *)&hb, sizeof(hb));
  if (debugETM) {
    Serial.printf("[ETM] Heartbeat sent (WCB%d)\n", WCB_Number);
  }
}

void scheduleNextHeartbeat(bool isBoot) {
  unsigned long intervalMs;
  if (isBoot) {
    // Random 1000ms to etmBootHeartbeatSec*1000ms at millisecond resolution
    intervalMs = (unsigned long)random(1000, etmBootHeartbeatSec * 1000UL);
  } else {
    // Random (etmHeartbeatSec-1)*1000 to (etmHeartbeatSec+1)*1000 at millisecond resolution
    intervalMs = (unsigned long)random((etmHeartbeatSec - 1) * 1000UL, (etmHeartbeatSec + 1) * 1000UL);
  }
  etmNextHeartbeatMs = millis() + intervalMs;
  if (debugETM) {
    Serial.printf("[ETM] Next heartbeat in %lums\n", intervalMs);
  }
}

void processETMHeartbeats() {
  if (!etmEnabled) return;

  // Send heartbeat when timer fires
  if (millis() >= etmNextHeartbeatMs) {
    sendETMHeartbeat();
    scheduleNextHeartbeat(false);
  }

  // Check for boards that have gone offline
  unsigned long offlineThresholdMs = (unsigned long)(etmHeartbeatSec + 1) * etmMissedHeartbeats * 1000UL;
  for (int i = 0; i < Default_WCB_Quantity; i++) {
    if (i + 1 == WCB_Number) continue;  // skip ourselves
    if (boardTable[i].online) {
      if (millis() - boardTable[i].lastSeenMs > offlineThresholdMs) {
        boardTable[i].online = false;
        Serial.printf("[ETM] WCB%d went OFFLINE (no heartbeat for %lus)\n",
                      i + 1, offlineThresholdMs / 1000UL);
      }
    }
  }
}


void etmInitPendingTable() {
  for (int i = 0; i < ETM_PENDING_MAX; i++) {
    etmPendingTable[i].active = false;
  }
}

int etmAddToPendingTable(uint16_t seqNum, const char* cmd, int targetWCB) {
  // Find oldest active slot if full
  int slot = -1;
  for (int i = 0; i < ETM_PENDING_MAX; i++) {
    if (!etmPendingTable[i].active) {
      slot = i;
      break;
    }
  }
  if (slot == -1) {
    if (debugETM) Serial.println("[ETM] Pending table full, evicting oldest entry");
    slot = 0;
    for (int b = 0; b < Default_WCB_Quantity; b++) {
      if (etmPendingTable[slot].expectAckFrom[b] && !etmPendingTable[slot].receivedAckFrom[b]) {
        etmStatsFailed[b]++;
      }
    }
    for (int i = 0; i < ETM_PENDING_MAX - 1; i++) {
      etmPendingTable[i] = etmPendingTable[i + 1];
    }
    etmPendingTable[ETM_PENDING_MAX - 1].active = false;
    slot = ETM_PENDING_MAX - 1;
  }

  PendingMessage &entry = etmPendingTable[slot];
  entry.sequenceNumber = seqNum;
  strncpy(entry.command, cmd, sizeof(entry.command) - 1);
  entry.command[sizeof(entry.command) - 1] = '\0';
  entry.lastSentMs = millis();
  entry.active = true;
  entry.targetWCB = targetWCB;  // store target for retry logic

  for (int b = 0; b < 9; b++) {
    entry.expectAckFrom[b]   = false;
    entry.receivedAckFrom[b] = false;
    entry.retryCount[b]      = 0;
  }

  for (int b = 0; b < Default_WCB_Quantity; b++) {
    int wcbNum = b + 1;
    if (wcbNum == WCB_Number) continue;
    if (!boardTable[b].online) continue;

    // For unicast, only expect ACK from the target board
    if (targetWCB != 0 && wcbNum != targetWCB) continue;

    entry.expectAckFrom[b] = true;
    etmStatsSent[b]++;
  }

  return slot;
}


void etmProcessAck(int senderWCB, uint16_t seqNum) {
  int boardIdx = senderWCB - 1;
  for (int i = 0; i < ETM_PENDING_MAX; i++) {
    if (!etmPendingTable[i].active) continue;
    if (etmPendingTable[i].sequenceNumber != seqNum) continue;

    if (!etmPendingTable[i].receivedAckFrom[boardIdx]) {
      etmPendingTable[i].receivedAckFrom[boardIdx] = true;
      etmStatsAckd[boardIdx]++;
      if (debugETM) {
        Serial.printf("[ETM] ACK received from WCB%d for seq %d\n", senderWCB, seqNum);
      }
      if (debugETM) {
        Serial.printf("[ETM DEBUG] etmProcessAck called: senderWCB=%d, seqNum=%d\n", senderWCB, seqNum);
      }

      // Notify characterization layer if running
      if (etmCharRunning) {
        String originalCmd = String(etmPendingTable[i].command);
        if (originalCmd.startsWith("ETMCHAR_")) {
          int phase = -1;
          if (originalCmd.startsWith("ETMCHAR_P1")) phase = 0;
          else if (originalCmd.startsWith("ETMCHAR_P2")) phase = 1;
          else if (originalCmd.startsWith("ETMCHAR_P3")) phase = 2;

          int mIdx = originalCmd.substring(originalCmd.lastIndexOf('M') + 1).toInt();
          if (phase >= 0 && mIdx >= 0 && mIdx < 200) {
            processETMCharAck(senderWCB, originalCmd, etmCharPhaseSentTimes[phase][mIdx]);
          }
        }
      }
    }

    // Check if all expected ACKs received
    bool allDone = true;
    for (int b = 0; b < Default_WCB_Quantity; b++) {
      if (etmPendingTable[i].expectAckFrom[b] && !etmPendingTable[i].receivedAckFrom[b]) {
        allDone = false;
        break;
      }
    }
    if (allDone) {
      etmPendingTable[i].active = false;
      if (debugETM) Serial.printf("[ETM] Seq %d fully acknowledged\n", seqNum);
    }
    return;
  }
  if (debugETM) Serial.printf("[ETM] ACK for unknown seq %d from WCB%d (already cleared?)\n", seqNum, senderWCB);
}

void processETMAcksAndRetries() {
  if (!etmEnabled) return;

  for (int i = 0; i < ETM_PENDING_MAX; i++) {
    if (!etmPendingTable[i].active) continue;

    PendingMessage &entry = etmPendingTable[i];
    if (millis() - entry.lastSentMs < (unsigned long)etmTimeoutMs) continue;

    bool anyStillPending = false;

    for (int b = 0; b < Default_WCB_Quantity; b++) {
      if (!entry.expectAckFrom[b]) continue;
      if (entry.receivedAckFrom[b]) continue;

      // This board hasn't ACKd yet
      if (entry.retryCount[b] < 3) {
        // Send unicast retry
        int wcbNum = b + 1;
        espnow_struct_message_etm retry;
        memset(&retry, 0, sizeof(retry));

        strncpy(retry.structPassword, espnowPassword, sizeof(retry.structPassword) - 1);
        snprintf(retry.structSenderID, sizeof(retry.structSenderID), "%d", WCB_Number);
        snprintf(retry.structTargetID, sizeof(retry.structTargetID), "%d", wcbNum);
        retry.structCommandIncluded = 1;
        strncpy(retry.structCommand, entry.command, sizeof(retry.structCommand) - 1);
        retry.structPacketType = PACKET_TYPE_COMMAND;
        retry.structSequenceNumber = entry.sequenceNumber;

        uint8_t *mac = WCBMacAddresses[b];
        esp_now_send(mac, (uint8_t *)&retry, sizeof(retry));

        entry.retryCount[b]++;
        etmStatsRetries[b]++;
        anyStillPending = true;

        if (debugETM) {
          Serial.printf("[ETM] Retry %d to WCB%d for seq %d: %s\n",
                        entry.retryCount[b], wcbNum, entry.sequenceNumber, entry.command);
        }
      } else {
        // Exhausted retries for this board
        etmStatsFailed[b]++;
        entry.expectAckFrom[b] = false; // stop retrying this board
        Serial.printf("[ETM] WCB%d failed to ACK seq %d after 3 retries: %s\n",
                      b + 1, entry.sequenceNumber, entry.command);
      }
    }

    entry.lastSentMs = millis(); // reset timer for next retry window

    // Check if all resolved
    bool allResolved = true;
    for (int b = 0; b < Default_WCB_Quantity; b++) {
      if (entry.expectAckFrom[b] && !entry.receivedAckFrom[b]) {
        allResolved = false;
        break;
      }
    }
    if (allResolved) {
      entry.active = false;
      if (debugETM) Serial.printf("[ETM] Seq %d resolved\n", entry.sequenceNumber);
    }
  }
}

void etmSendAck(int senderWCB, uint16_t seqNum) {
  espnow_struct_message_etm ack;
  memset(&ack, 0, sizeof(ack));

  strncpy(ack.structPassword, espnowPassword, sizeof(ack.structPassword) - 1);
  snprintf(ack.structSenderID, sizeof(ack.structSenderID), "%d", WCB_Number);
  snprintf(ack.structTargetID, sizeof(ack.structTargetID), "%d", senderWCB);
  ack.structCommandIncluded = 0;
  ack.structPacketType = PACKET_TYPE_ACK;
  ack.structSequenceNumber = seqNum;

  uint8_t *mac = WCBMacAddresses[senderWCB - 1];
  esp_err_t result = esp_now_send(mac, (uint8_t *)&ack, sizeof(ack));
  if (debugETM) {
    Serial.printf("[ETM] Sent ACK seq %d to WCB%d, result: %d\n", seqNum, senderWCB, result);
  }
}



void startETMChar() {
    if (!etmEnabled) {
        Serial.println("ETM is not enabled. Use ?ETMON first.");
        return;
    }
    if (Default_WCB_Quantity < 2) {
        Serial.println("ETM Char requires at least 2 WCBs in the network (?WCBQ).");
        return;
    }

    etmCharRunning = true;
    etmCharPhase = 1;
    etmCharPhaseMessageIndex = 0;
    etmCharPhaseStartTime = millis();
    etmCharLastSendTime = 0;
    memset(etmCharBoardResults, 0, sizeof(etmCharBoardResults));

    // Init min latency to large value so first real reading wins
    for (int p = 0; p < 3; p++)
        for (int b = 0; b < 8; b++)
            etmCharBoardResults[p][b].latencyMin = 999999;

    Serial.println("\n---------- ETM Network Characterization ----------");
    Serial.printf("Messages per board per phase: %d\n", etmCharMessageCount);
    Serial.printf("Phase 1 - Individual Baseline (unicast, no load)...\n");
}

void processETMChar() {
    if (!etmCharRunning) return;

    unsigned long now = millis();

    int peers[8];
    int peerCount = 0;
    for (int i = 1; i <= Default_WCB_Quantity; i++) {
        if (i != WCB_Number) peers[peerCount++] = i;
    }
    if (peerCount == 0) { etmCharRunning = false; return; }

    int totalMessages = etmCharMessageCount * peerCount;

    if (etmCharPhase == 1 || etmCharPhase == 2) {
        unsigned long interDelay = (etmCharPhase == 1) ? 20UL : (unsigned long)etmCharDelayMs;

        if (etmCharPhaseMessageIndex < totalMessages) {
            if (now - etmCharLastSendTime >= interDelay) {
                int peerIdx = etmCharPhaseMessageIndex % peerCount;
                int targetWCB = peers[peerIdx];
                int boardIdx = targetWCB - 1;

                String payload = "ETMCHAR_P" + String(etmCharPhase) + 
                                 "_M" + String(etmCharPhaseMessageIndex);

                etmCharPhaseSentTimes[etmCharPhase - 1][etmCharPhaseMessageIndex] = now;
                etmCharBoardResults[etmCharPhase - 1][boardIdx].sent++;

                sendESPNowMessage(targetWCB, payload.c_str(), true);
                etmCharPhaseMessageIndex++;
                etmCharLastSendTime = now;
            }
        }

        int totalAcked = 0;
        for (int b = 0; b < 8; b++)
            totalAcked += etmCharBoardResults[etmCharPhase - 1][b].acked;

        unsigned long phaseTimeout = (unsigned long)totalMessages *
                                     (max((unsigned long)etmCharDelayMs, 20UL) + etmTimeoutMs) + 3000;
        bool allDone = (etmCharPhaseMessageIndex >= totalMessages && totalAcked >= totalMessages);
        bool timedOut = (now - etmCharPhaseStartTime > phaseTimeout);

        if (allDone || timedOut) {
            advanceETMCharPhase(peers, peerCount);
        }

    } else if (etmCharPhase == 3) {
        if (etmCharPhaseMessageIndex == 0) {
            Serial.println("Triggering network load on all peers...");
            sendESPNowMessage(0, "ETMLOAD", false);
        }

        if (etmCharPhaseMessageIndex < totalMessages) {
            if (now - etmCharLastSendTime >= (unsigned long)etmCharDelayMs) {
                int msgIdx = etmCharPhaseMessageIndex;
                bool sendBroadcast = (msgIdx % 3 == 2);

                String payload = "ETMCHAR_P3_M" + String(msgIdx);

                if (sendBroadcast) {
                    etmCharPhaseSentTimes[2][msgIdx] = now;
                    for (int b = 0; b < peerCount; b++)
                        etmCharBoardResults[2][peers[b] - 1].sent++;
                    sendESPNowMessage(0, payload.c_str(), true);
                } else {
                    int peerIdx = msgIdx % peerCount;
                    int targetWCB = peers[peerIdx];
                    etmCharPhaseSentTimes[2][msgIdx] = now;
                    etmCharBoardResults[2][targetWCB - 1].sent++;
                    sendESPNowMessage(targetWCB, payload.c_str(), true);
                }

                etmCharPhaseMessageIndex++;
                etmCharLastSendTime = now;
            }
        }

        int totalSent = 0, totalAcked = 0;
        for (int b = 0; b < 8; b++) {
            totalSent += etmCharBoardResults[2][b].sent;
            totalAcked += etmCharBoardResults[2][b].acked;
        }

        unsigned long phaseTimeout = (unsigned long)totalMessages *
                                     ((unsigned long)etmCharDelayMs + etmTimeoutMs) + 5000;
        bool allDone = (etmCharPhaseMessageIndex >= totalMessages && totalAcked >= totalSent);
        bool timedOut = (now - etmCharPhaseStartTime > phaseTimeout);

        if (allDone || timedOut) {
            etmCharRunning = false;
            printETMCharResults(peers, peerCount);
        }
    }
}
void advanceETMCharPhase(int* peers, int peerCount) {
    etmCharPhase++;
    etmCharPhaseMessageIndex = 0;
    etmCharPhaseStartTime = millis();
    etmCharLastSendTime = 0;

    if (etmCharPhase == 2) {
        Serial.printf("Phase 2 - Broadcast (no load)...\n");
        // Phase 2 is actually broadcast to all — re-init results for phase 2
        // We'll send to broadcast but track per-board via ACKs
    } else if (etmCharPhase == 3) {
        Serial.printf("Phase 3 - Loaded Network (all boards transmitting)...\n");
    }
}

void processETMCharAck(int senderWCB, const String &originalCmd, unsigned long sentTime) {
    if (!etmCharRunning) return;
    if (!originalCmd.startsWith("ETMCHAR_")) return;

    int boardIdx = senderWCB - 1;
    if (boardIdx < 0 || boardIdx > 7) return;

    int phase = -1;
    if (originalCmd.startsWith("ETMCHAR_P1")) phase = 0;
    else if (originalCmd.startsWith("ETMCHAR_P2")) phase = 1;
    else if (originalCmd.startsWith("ETMCHAR_P3")) phase = 2;
    if (phase < 0) return;

    unsigned long latency = millis() - sentTime;
    
    // TEMP DEBUG
    Serial.printf("[CHAR DEBUG] ACK: cmd=%s phase=%d board=%d latency=%lums sentTime=%lu now=%lu\n",
                  originalCmd.c_str(), phase, senderWCB, latency, sentTime, millis());

    ETMCharBoardResult &r = etmCharBoardResults[phase][boardIdx];
    r.acked++;
    r.latencyAccum += latency;
    if (latency < r.latencyMin) r.latencyMin = latency;
    if (latency > r.latencyMax) r.latencyMax = latency;
}

void processETMLoad() {
    if (!etmLoadRunning) return;

    unsigned long now = millis();

    // Run for 10 seconds
    if (now - etmLoadStartTime > 10000) {
        etmLoadRunning = false;
        if (debugEnabled) Serial.println("ETM load test complete.");
        return;
    }

    if (now - etmLoadLastSendTime >= (unsigned long)etmCharDelayMs) {
        // Alternate broadcast and unicast round-robin
        static int loadMsgCount = 0;
        bool sendBcast = (loadMsgCount % 3 == 2);

        // Random-ish test payloads of varying length
        const char* payloads[] = {
            "LOAD_TEST_SHORT",
            "LOAD_TEST_MEDIUM_PAYLOAD",
            "LOAD_TEST_THIS_IS_A_LONGER_MESSAGE",
            "LOAD_SHORT2",
            "LOAD_MED_SIZE_MSG",
            "LOAD_TEST_FILLER_DATA_123"
        };
        String payload = payloads[loadMsgCount % 6];

        if (sendBcast) {
            sendESPNowMessage(0, payload.c_str(), false);  // broadcast, not ETM
        } else {
            // Round-robin unicast to peers
            int target = etmLoadRoundRobinIndex + 1;
            if (target == WCB_Number) target++;  // skip self
            if (target > Default_WCB_Quantity) target = 1;
            if (target == WCB_Number) target++;  // skip self again if WCB1
            etmLoadRoundRobinIndex = (etmLoadRoundRobinIndex + 1) % Default_WCB_Quantity;
            sendESPNowMessage(target, payload.c_str(), false);  // not ETM, just load
        }

        loadMsgCount++;
        etmLoadLastSendTime = now;
    }
}

void printETMCharResults(int* peers, int peerCount) {
    const char* phaseNames[] = {
        "Individual Baseline (unicast, no load)",
        "Broadcast (no load)",
        "Loaded Network (all boards transmitting)"
    };

    Serial.println("\n--------------- ETM Network Characterization ---------------");

    unsigned long worstMaxLatency = 0;
    unsigned long worstAvgLatency = 0;
    float worstMissedPct = 0.0f;

    for (int p = 0; p < 3; p++) {
        Serial.printf(" Phase %d - %s:\n", p + 1, phaseNames[p]);
        for (int i = 0; i < peerCount; i++) {
            int b = peers[i] - 1;
            ETMCharBoardResult &r = etmCharBoardResults[p][b];
            if (r.sent == 0) continue;

            int effectiveAcked = min(r.acked, r.sent);
            float missedPct = (float)(r.sent - effectiveAcked) / r.sent * 100.0f;
            unsigned long avgLatency = (effectiveAcked > 0) ? r.latencyAccum / effectiveAcked : 0;
            unsigned long minLat = (effectiveAcked > 0) ? r.latencyMin : 0;
            bool hadRetries = r.latencyMax > (unsigned long)(etmTimeoutMs * 0.9);

            Serial.printf("   WCB%d: Min: %lums, Max: %lums, Avg: %lums, Missed: %.0f%%%s\n",
                peers[i], minLat, r.latencyMax, avgLatency, missedPct,
                hadRetries ? " (retries detected)" : "");

            if (r.latencyMax > worstMaxLatency) worstMaxLatency = r.latencyMax;
            if (avgLatency > worstAvgLatency) worstAvgLatency = avgLatency;
            if (missedPct > worstMissedPct) worstMissedPct = missedPct;
        }
    }

    // Base recommendation on worst average * 5, rounded up to nearest 50ms, floor 150ms
    unsigned long recommendedTimeout = ((worstAvgLatency * 5) / 50 + 1) * 50;
    if (recommendedTimeout < 150) recommendedTimeout = 150;

    Serial.printf("\n Recommended ETM timeout: %lums\n", recommendedTimeout);
    Serial.printf(" Apply with: ?ETMTOUT%lu\n", recommendedTimeout);
    Serial.printf(" (Based on worst avg latency: %lums, worst max: %lums)\n", 
                  worstAvgLatency, worstMaxLatency);
    if (worstMissedPct > 5.0f) {
        Serial.println(" Warning: >5% packet loss detected. Check RF environment.");
    }
    Serial.println("------------------------------------------------------------\n");
}


// String getSerialLabel(int port);
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
  // Check if this is a restore with checksum
  int firstDelim = data.indexOf(commandDelimiter);
  if (firstDelim != -1) {
    // Look for ?CHK command in the chain
    int chkPos = data.indexOf(String(commandDelimiter) + "?CHK");
    if (chkPos == -1) {
      chkPos = data.indexOf(String(commandDelimiter) + "?chk");
    }
    
    if (chkPos != -1) {
      // Extract checksum command
      int chkStart = chkPos + 1; // Skip the delimiter
      int chkEnd = data.indexOf(commandDelimiter, chkStart);
      if (chkEnd == -1) chkEnd = data.length();
      
      String checksumCmd = data.substring(chkStart, chkEnd);
      String providedChecksum = checksumCmd.substring(4); // Skip "?CHK"
      providedChecksum.toUpperCase();
      
      // Get data without checksum for verification
      String dataWithoutChecksum = data.substring(0, chkPos);
      uint32_t calculatedChecksum = calculateCRC32(dataWithoutChecksum);
      String calculatedChecksumStr = String(calculatedChecksum, HEX);
      calculatedChecksumStr.toUpperCase();
      
      if (providedChecksum.equals(calculatedChecksumStr)) {
        Serial.println("✓ Command checksum VERIFIED - Configuration is intact");
        Serial.println("  Provided:   " + providedChecksum);
        Serial.println("  Calculated: " + calculatedChecksumStr);
        // Continue processing without the checksum command
        parseCommandsAndEnqueue(dataWithoutChecksum, sourceID);
        return;
      } else {
        Serial.println("✗ Command checksum FAILED - Configuration may be corrupted!");
        Serial.println("  Provided:   " + providedChecksum);
        Serial.println("  Calculated: " + calculatedChecksumStr);
        Serial.println("  Aborting restore to prevent corruption.");
        return;
      }
    }
  }
  
  // Normal parsing with special handling for ?CS commands
  int startIdx = 0;
  while (startIdx < data.length()) {
    // Check if we're at the start of a ?CS command
    String restOfString = data.substring(startIdx);
    bool isCSCommand = restOfString.startsWith(String(LocalFunctionIdentifier) + "CS") || 
                       restOfString.startsWith(String(LocalFunctionIdentifier) + "cs");
    
    if (isCSCommand) {
      // Find the end of this ?CS command (next ^?CS or end of string)
      int nextCSPos = data.indexOf(String(commandDelimiter) + String(LocalFunctionIdentifier) + "CS", startIdx + 1);
      if (nextCSPos == -1) {
        nextCSPos = data.indexOf(String(commandDelimiter) + String(LocalFunctionIdentifier) + "cs", startIdx + 1);
      }
      
      // Extract the entire ?CS command including all its sub-commands
      int endPos = (nextCSPos == -1) ? data.length() : nextCSPos;
      String csCommand = data.substring(startIdx, endPos);
      csCommand.trim();
      
      if (!csCommand.isEmpty() && !csCommand.startsWith(commentDelimiter)) {
        enqueueCommand(csCommand, sourceID);
      }
      
      // Move past this command
      startIdx = endPos;
      if (startIdx < data.length() && data.charAt(startIdx) == commandDelimiter) {
        startIdx++; // skip the delimiter
      }
      continue;
    }
    
    // Normal command processing (not ?CS)
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
  loadBroadcastBlockSettings();
  // loadBroadcastSettingsFromPreferences();
  Serial.println("--------------- Serial Settings ----------------------");
  printBaudRates();  // Print baud rates
  listSerialMonitorMappings();

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
  printMaestroSettings();  // Show configured Maestros
 if (etmEnabled) {
    Serial.println("--------------- Online WCBs ---------------");
    for (int i = 0; i < Default_WCB_Quantity; i++) {
      if (i + 1 == WCB_Number) {
        Serial.printf("  WCB%d: This board\n", i + 1);
      } else if (boardTable[i].online) {
        unsigned long secsAgo = (millis() - boardTable[i].lastSeenMs) / 1000UL;
        Serial.printf("  WCB%d: Online  (last seen %lus ago)\n", i + 1, secsAgo);
      } else {
        Serial.printf("  WCB%d: OFFLINE\n", i + 1);
      }
    }
}

    Serial.println("--------------- ETM Settings ----------------------");
  Serial.printf("ETM: %s\n", etmEnabled ? "ENABLED ⚠️  All boards must match!" : "Disabled");
  if (etmEnabled) {
    Serial.printf("  Boot heartbeat: 1-%d sec\n", etmBootHeartbeatSec);
    Serial.printf("  Heartbeat interval: %d sec (+/- 1)\n", etmHeartbeatSec);
    Serial.printf("  Offline after: %d missed (%d sec max)\n", etmMissedHeartbeats, (etmHeartbeatSec+1)*etmMissedHeartbeats);
    Serial.printf("  Retry timeout: %d ms\n", etmTimeoutMs);
    Serial.printf("  Char message count: %d, delay: %d ms\n", etmCharMessageCount, etmCharDelayMs);
  }

  Serial.println("--- End of Configuration Info ---\n");
}

void printESPNowStats() {
  Serial.println("\n--- ESP-NOW Statistics (Since Last Reboot) ---");

  if (etmEnabled) {
    unsigned long totalSent = 0, totalAckd = 0, totalRetries = 0, totalFailed = 0;
    for (int b = 0; b < Default_WCB_Quantity; b++) {
      if (b + 1 == WCB_Number) continue;
      totalSent    += etmStatsSent[b];
      totalAckd    += etmStatsAckd[b];
      totalRetries += etmStatsRetries[b];
      totalFailed  += etmStatsFailed[b];
    }
    Serial.println("Unicast Command Messages:");
    Serial.printf("  Transmission Attempts: %lu, Delivered: %lu, Failed: %lu\n",
                  totalSent, totalAckd, totalFailed);
    if (totalSent > 0) {
      Serial.printf("  Delivery Success Rate: %.2f%%\n",
                    (float)totalAckd / totalSent * 100.0);
    }
  } else {
    Serial.println("Unicast Command Messages:");
    Serial.printf("  Transmission Attempts: %lu, Delivered: %lu, Failed: %lu\n",
                  espnowCommandAttempts, espnowCommandSuccess, espnowCommandFailed);
    if (espnowCommandAttempts > 0) {
      Serial.printf("  Delivery Success Rate: %.2f%%\n",
                    (float)espnowCommandSuccess / espnowCommandAttempts * 100.0);
    }
  }

  if (espnowPWMAttempts > 0) {
    Serial.println("PWM Passthrough:");
    Serial.printf("  Attempts: %lu, Success: %lu, Failed: %lu\n",
                  espnowPWMAttempts, espnowPWMSuccess, espnowPWMFailed);
  }

  if (espnowRawAttempts > 0) {
    Serial.println("Raw Data (Kyber Bridging):");
    Serial.printf("  Attempts: %lu, Success: %lu, Failed: %lu\n",
                  espnowRawAttempts, espnowRawSuccess, espnowRawFailed);
  }

  if (etmEnabled) {
    Serial.println("\n--------------- ETM Per-Board Statistics ---------------");
    for (int b = 0; b < Default_WCB_Quantity; b++) {
      int wcbNum = b + 1;
      if (wcbNum == WCB_Number) continue;
      unsigned long ago = (millis() - boardTable[b].lastSeenMs) / 1000;
      Serial.printf("WCB%d: Sent: %lu, ACKd: %lu, Retries: %lu, Failed: %lu, %s\n",
                    wcbNum,
                    etmStatsSent[b], etmStatsAckd[b],
                    etmStatsRetries[b], etmStatsFailed[b],
                    boardTable[b].online
                      ? ("Online (last seen " + String(ago) + "s ago)").c_str()
                      : "OFFLINE");
    }
    Serial.println("--------------------------------------------------------");
  }

  Serial.println("--- End of ESP-NOW Statistics ---\n");
}
//*******************************
/// ESP-NOW Functions
//*******************************
// Send an ESP-NOW message (unicast or broadcast)
void sendESPNowMessage(uint8_t target, const char *message, bool useETM) {
  // Skip broadcast if last was from ESP-NOW
  if (target == 0 && lastReceivedViaESPNOW) {
    if (debugEnabled) { Serial.printf("Skipping ESPNOW broadcast to avoid loops\n"); }
    return;
  }

  // Check if this is a PWM message (starts with ";P") - MUST BE FIRST
  bool isPWMMessage = (message[0] == ';' && (message[1] == 'P' || message[1] == 'p'));

  // ---- ETM SEND PATH ----
  if (etmEnabled && useETM && !isPWMMessage) {
    espnow_struct_message_etm etmMsg;
    memset(&etmMsg, 0, sizeof(etmMsg));

    strncpy(etmMsg.structPassword, espnowPassword, sizeof(etmMsg.structPassword) - 1);
    snprintf(etmMsg.structSenderID, sizeof(etmMsg.structSenderID), "%d", WCB_Number);
    if (target == 0) {
      snprintf(etmMsg.structTargetID, sizeof(etmMsg.structTargetID), "0");
    } else {
      snprintf(etmMsg.structTargetID, sizeof(etmMsg.structTargetID), "%d", target);
    }
    etmMsg.structCommandIncluded = 1;
    strncpy(etmMsg.structCommand, message, sizeof(etmMsg.structCommand) - 1);
    etmMsg.structCommand[sizeof(etmMsg.structCommand) - 1] = '\0';
    etmMsg.structPacketType = PACKET_TYPE_COMMAND;
    etmMsg.structSequenceNumber = ++etmSequenceCounter;

    etmAddToPendingTable(etmMsg.structSequenceNumber, message, target);

    uint8_t *mac = (target == 0) ? broadcastMACAddress[0] : WCBMacAddresses[target - 1];

    espnowCommandAttempts++;
    esp_err_t result = esp_now_send(mac, (uint8_t *)&etmMsg, sizeof(etmMsg));
    if (result == ESP_OK) {
      espnowCommandSuccess++;
      espnowCommandDelivered++;
      if (debugETM) Serial.printf("[ETM] Sent seq %d: %s\n", etmMsg.structSequenceNumber, message);
    } else {
      espnowCommandFailed++;
      Serial.printf("[ETM] Send failed seq %d, error: %d\n", etmMsg.structSequenceNumber, result);
    }
    return;
  }
  // ---- END ETM SEND PATH ----

  // ---- NORMAL SEND PATH ----
  espnow_struct_message msg;
  memset(&msg, 0, sizeof(msg));

  strncpy(msg.structPassword, espnowPassword, sizeof(msg.structPassword) - 1);
  msg.structPassword[sizeof(msg.structPassword) - 1] = '\0';
  snprintf(msg.structSenderID, sizeof(msg.structSenderID), "%d", WCB_Number);
  if (target == 0) {
    snprintf(msg.structTargetID, sizeof(msg.structTargetID), "0");
  } else {
    snprintf(msg.structTargetID, sizeof(msg.structTargetID), "%d", target);
  }
  msg.structCommandIncluded = true;
  strncpy(msg.structCommand, message, sizeof(msg.structCommand) - 1);
  msg.structCommand[sizeof(msg.structCommand) - 1] = '\0';

  uint8_t *mac = (target == 0) ? broadcastMACAddress[0] : WCBMacAddresses[target - 1];

  if (isPWMMessage) {
    espnowPWMAttempts++;
  } else {
    espnowCommandAttempts++;
  }

  esp_err_t result = esp_now_send(mac, (uint8_t *)&msg, sizeof(msg));
  if (result == ESP_OK) {
    if (isPWMMessage) {
      espnowPWMSuccess++;
    } else {
      espnowCommandSuccess++;
      if (debugEnabled) Serial.println("ESP-NOW message sent successfully!");
    }
  } else {
    if (isPWMMessage) {
      espnowPWMFailed++;
    } else {
      espnowCommandFailed++;
    }
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
        espnowRawAttempts++;

        esp_err_t result = esp_now_send(mac, (uint8_t*)&msg, sizeof(msg));
        if (result == ESP_OK) {
            espnowRawSuccess++;
            if (debugEnabled) {
                // Serial.printf("Sent chunk of %d bytes via ESP-NOW\n", (int)chunkSize);
            }
        } else {
            espnowRawFailed++;
            Serial.printf("ESP-NOW send failed! Error code: %d\n", result);
        }
        offset += chunkSize;
    }
}
esp_err_t sendESPNowRawToSpecificWCB(const uint8_t *data, size_t len, uint8_t targetWCB) {
  size_t offset = 0;
  esp_err_t lastResult = ESP_OK;
  
  while (offset < len) {
    size_t chunkSize = len - offset;
    if (chunkSize > 180) chunkSize = 180;

    espnow_struct_message msg;
    memset(&msg, 0, sizeof(msg));

    strncpy(msg.structPassword, espnowPassword, sizeof(msg.structPassword) - 1);
    msg.structPassword[sizeof(msg.structPassword) - 1] = '\0';
    
    snprintf(msg.structSenderID, sizeof(msg.structSenderID), "%d", WCB_Number);
    snprintf(msg.structTargetID, sizeof(msg.structTargetID), "%d", targetWCB);

    msg.structCommandIncluded = true;

    // Store chunk length in first 2 bytes
    msg.structCommand[0] = (uint8_t)(chunkSize & 0xFF);
    msg.structCommand[1] = (uint8_t)((chunkSize >> 8) & 0xFF);

    // Copy chunk
    memcpy(msg.structCommand + 2, data + offset, chunkSize);

    // Send to specific WCB (not broadcast)
    uint8_t *mac = WCBMacAddresses[targetWCB - 1];

    lastResult = esp_now_send(mac, (uint8_t*)&msg, sizeof(msg));
    
    offset += chunkSize;
  }
  
  return lastResult;
}

void sendESPNowRawToPort(const uint8_t *data, size_t len, uint8_t targetWCB, uint8_t targetPort) {
  size_t offset = 0;
  while (offset < len) {
    size_t chunkSize = len - offset;
    if (chunkSize > 180) chunkSize = 180;

    espnow_struct_message msg;
    memset(&msg, 0, sizeof(msg));

    strncpy(msg.structPassword, espnowPassword, sizeof(msg.structPassword) - 1);
    msg.structPassword[sizeof(msg.structPassword) - 1] = '\0';
    
    snprintf(msg.structSenderID, sizeof(msg.structSenderID), "%d", WCB_Number);
    
    // Target ID encodes: K for Kyber with port info
    snprintf(msg.structTargetID, sizeof(msg.structTargetID), "K");

    msg.structCommandIncluded = true;

    // Store: [target_wcb][target_port][chunk_len_low][chunk_len_high][data...]
    msg.structCommand[0] = targetWCB;
    msg.structCommand[1] = targetPort;
    msg.structCommand[2] = (uint8_t)(chunkSize & 0xFF);
    msg.structCommand[3] = (uint8_t)((chunkSize >> 8) & 0xFF);

    memcpy(msg.structCommand + 4, data + offset, chunkSize);

    // Send to specific WCB
    esp_err_t result = esp_now_send(WCBMacAddresses[targetWCB - 1], 
                                    (uint8_t*)&msg, sizeof(msg));
    
    if (debugEnabled && result != ESP_OK) {
      Serial.printf("Kyber targeted send to WCB%d failed\n", targetWCB);
    }

    offset += chunkSize;
  }
}

void sendESPNowRawSerial(const uint8_t *data, size_t len, uint8_t targetWCB, uint8_t targetPort) {
    size_t offset = 0;
    while (offset < len) {
        size_t chunkSize = len - offset;
        if (chunkSize > 180) chunkSize = 180;

        espnow_struct_message msg;
        memset(&msg, 0, sizeof(msg));

        strncpy(msg.structPassword, espnowPassword, sizeof(msg.structPassword) - 1);
        msg.structPassword[sizeof(msg.structPassword) - 1] = '\0';
        
        snprintf(msg.structSenderID, sizeof(msg.structSenderID), "%d", WCB_Number);
        snprintf(msg.structTargetID, sizeof(msg.structTargetID), "8");  // Target ID 8 = raw serial mapping

        msg.structCommandIncluded = true;

        // First byte = target serial port
        msg.structCommand[0] = targetPort;
        // Next 2 bytes = chunk length
        msg.structCommand[1] = (uint8_t)(chunkSize & 0xFF);
        msg.structCommand[2] = (uint8_t)((chunkSize >> 8) & 0xFF);
        // Copy data
        memcpy(msg.structCommand + 3, data + offset, chunkSize);

        uint8_t *mac = (targetWCB == 0) ? broadcastMACAddress[0] : WCBMacAddresses[targetWCB - 1];

        esp_err_t result = esp_now_send(mac, (uint8_t*)&msg, sizeof(msg));
        if (result != ESP_OK && debugEnabled) {
            Serial.printf("ESP-NOW raw serial send failed! Error: %d\n", result);
        }

        offset += chunkSize;
    }
}

void espNowReceiveCallback(const esp_now_recv_info_t *info, const uint8_t *incomingData, int len) {

  // Handle both normal and ETM struct sizes
  bool isETMPacket = false;
  if (len == sizeof(espnow_struct_message_etm)) {
    isETMPacket = true;
  } else if (len != sizeof(espnow_struct_message)) {
    return; // Unknown size - ignore
  }

  // ETM/non-ETM mismatch guards
  if (isETMPacket && !etmEnabled) {
    if (debugETM) Serial.println("[ETM] Received ETM packet but ETM disabled locally, ignoring.");
    return;
  }
  if (!isETMPacket && etmEnabled) {
    espnow_struct_message peek;
    memcpy(&peek, incomingData, sizeof(peek));
    int peekTarget = atoi(peek.structTargetID);
    bool isRawPacket = (peekTarget == 8 || peekTarget == 9 || peek.structTargetID[0] == 'K');
    bool isPWMPacket = (peek.structCommand[0] == ';' && 
                       (peek.structCommand[1] == 'P' || peek.structCommand[1] == 'p'));
    
    if (!isRawPacket && !isPWMPacket) {
      if (debugETM) Serial.println("[ETM] Received normal packet but ETM enabled locally, ignoring.");
      return;
    }
    // Fall through to normal receive path for raw and PWM packets
  }

  // ---- ETM RECEIVE PATH ----
  if (isETMPacket) {
    espnow_struct_message_etm etmReceived;
    memcpy(&etmReceived, incomingData, sizeof(etmReceived));
    etmReceived.structPassword[sizeof(etmReceived.structPassword) - 1] = '\0';

    if (String(etmReceived.structPassword) != String(espnowPassword)) return;
    if (info->src_addr[1] != umac_oct2 || info->src_addr[2] != umac_oct3) return;

    int senderWCB = atoi(etmReceived.structSenderID);
    int targetWCB = atoi(etmReceived.structTargetID);

    if (etmReceived.structPacketType == PACKET_TYPE_HEARTBEAT) {
      int senderIdx = senderWCB - 1;
      if (senderIdx >= 0 && senderIdx < Default_WCB_Quantity && senderWCB != WCB_Number) {
        bool wasOffline = !boardTable[senderIdx].online;
        boardTable[senderIdx].online = true;
        boardTable[senderIdx].lastSeenMs = millis();
        if (wasOffline) {
          Serial.printf("[ETM] WCB%d came ONLINE\n", senderWCB);
        } else if (debugETM) {
          Serial.printf("[ETM] Heartbeat from WCB%d\n", senderWCB);
        }
      }
      return;
    }

    if (etmReceived.structPacketType == PACKET_TYPE_ACK) {
      if (targetWCB == WCB_Number) {
        etmProcessAck(senderWCB, etmReceived.structSequenceNumber);
      }
      return;
    }

    if (etmReceived.structPacketType == PACKET_TYPE_COMMAND) {
        if (targetWCB != 0 && targetWCB != WCB_Number) return;

        // Always ACK - even duplicates (so sender stops retrying)
        etmSendAck(senderWCB, etmReceived.structSequenceNumber);

        // Duplicate detection
        int senderIdx = senderWCB - 1;
        bool isDuplicate = false;
        if (etmLastReceivedSeqValid[senderIdx] &&
            etmLastReceivedSeq[senderIdx] == etmReceived.structSequenceNumber) {
            isDuplicate = true;
        }
        etmLastReceivedSeq[senderIdx] = etmReceived.structSequenceNumber;
        etmLastReceivedSeqValid[senderIdx] = true;

        if (isDuplicate) {
            if (debugETM) {
                Serial.printf("[ETM] Duplicate seq %d from WCB%d - ACKd but not executed\n",
                            etmReceived.structSequenceNumber, senderWCB);
            }
            colorWipeStatus("ES", blue, 10);
            return;
        }

        lastReceivedViaESPNOW = true;
        colorWipeStatus("ES", green, 200);

        if (debugETM) {
            Serial.printf("[ETM] Received seq %d from WCB%d: %s\n",
                        etmReceived.structSequenceNumber, senderWCB, etmReceived.structCommand);
        }

        String etmCmd = String(etmReceived.structCommand);
        if (!etmCmd.startsWith("ETMCHAR_") && !etmCmd.startsWith("ETMLOAD")) {
            enqueueCommand(etmCmd, 0);
        }
        colorWipeStatus("ES", blue, 10);
    }
    return;
  }
  // ---- END ETM RECEIVE PATH ----

  // ---- NORMAL RECEIVE PATH ----
  espnow_struct_message received;
  memcpy(&received, incomingData, sizeof(received));
  received.structPassword[sizeof(received.structPassword) - 1] = '\0';

  if (String(received.structPassword) != String(espnowPassword)) return;

  int senderWCB = atoi(received.structSenderID);
  int targetWCB = atoi(received.structTargetID);

  bool isPWMCommand = (received.structCommand[0] == ';' &&
                       (received.structCommand[1] == 'P' || received.structCommand[1] == 'p'));

  if (debugEnabled && targetWCB != 9 && !isPWMCommand && received.structTargetID[0] != 'K' && targetWCB != 8) {
    Serial.printf("Sender ID: WCB%d, Target ID: WCB%d\n", senderWCB, targetWCB);
  }

  if (info->src_addr[1] != umac_oct2 || info->src_addr[2] != umac_oct3) {
    if (debugEnabled) { Serial.println("Received message from an unrelated WCB group, ignoring."); }
    return;
  }

  // Kyber targeted forwarding
  if (received.structTargetID[0] == 'K') {
    uint8_t destWCB  = received.structCommand[0];
    uint8_t destPort = received.structCommand[1];
    size_t chunkLen  = (uint8_t)received.structCommand[2] | ((uint8_t)received.structCommand[3] << 8);

    if (debugKyber) {
      uint8_t *dataPtr = (uint8_t *)(received.structCommand + 4);
      Serial.printf("Kyber targeted to W%dS%d - chunk (hex): ", destWCB, destPort);
      for (int i = 0; i < (int)chunkLen; i++) {
        if (dataPtr[i] < 0x10) Serial.print("0");
        Serial.print(dataPtr[i], HEX);
        Serial.print(" ");
      }
      Serial.println();
    }

    if (destWCB == WCB_Number && destPort >= 1 && destPort <= 5) {
      Stream &targetSerial = getSerialStream(destPort);
      targetSerial.write((uint8_t *)(received.structCommand + 4), chunkLen);
      targetSerial.flush();
    }
    return;
  }

  // Kyber broadcast (target 9)
  if (targetWCB == 9) {
    size_t chunkLen = (uint8_t)received.structCommand[0] | ((uint8_t)received.structCommand[1] << 8);
    if (chunkLen > 180 || chunkLen == 0) {
      if (debugEnabled) Serial.printf("Invalid bridging chunk length: %d, Ignoring.\n", (int)chunkLen);
      return;
    }

    if (debugKyber) {
      uint8_t *dataPtr = (uint8_t *)(received.structCommand + 2);
      Serial.print("Kyber chunk (hex): ");
      for (int i = 0; i < (int)chunkLen; i++) {
        if (dataPtr[i] < 0x10) Serial.print("0");
        Serial.print(dataPtr[i], HEX);
        Serial.print(" ");
      }
      Serial.println();
    }

    if (Kyber_Local) {
      Serial1.write((uint8_t *)(received.structCommand + 2), chunkLen);
      Serial2.write((uint8_t *)(received.structCommand + 2), chunkLen);
    } else if (Kyber_Remote) {
      Serial1.write((uint8_t *)(received.structCommand + 2), chunkLen);
    }
    return;
  }

  // Raw serial mapping (target 8)
  if (targetWCB == 8) {
    uint8_t targetPort = (uint8_t)received.structCommand[0];
    size_t chunkLen    = (uint8_t)received.structCommand[1] | ((uint8_t)received.structCommand[2] << 8);
    if (chunkLen > 177 || chunkLen == 0 || targetPort < 1 || targetPort > 5) {
      if (debugEnabled) Serial.printf("Invalid raw serial chunk: port=%d, len=%d\n", targetPort, (int)chunkLen);
      return;
    }
    Stream &targetSerial = getSerialStream(targetPort);
    targetSerial.write((uint8_t *)(received.structCommand + 3), chunkLen);
    targetSerial.flush();
    if (debugEnabled) Serial.printf("Raw serial: %d bytes -> Serial%d\n", (int)chunkLen, targetPort);
    return;
  }

  // Normal command for this board
  lastReceivedViaESPNOW = true;
  colorWipeStatus("ES", green, 200);

  if (targetWCB != 0 && targetWCB != WCB_Number) {
    if (debugEnabled) Serial.println("Message not for this WCB, ignoring.");
    colorWipeStatus("ES", blue, 10);
    return;
  }

  String receivedCmd = String(received.structCommand);

  if (receivedCmd.startsWith("ETMLOAD")) {
    etmLoadRunning = true;
    etmLoadStartTime = millis();
    etmLoadLastSendTime = 0;
    etmLoadRoundRobinIndex = 0;
    if (debugEnabled) Serial.println("ETM load test started by remote board.");
    return;
  }
  if (receivedCmd.startsWith("ETMCHAR_")) {
    return;
  }

  enqueueCommand(String(received.structCommand), 0);
  colorWipeStatus("ES", blue, 10);
}
void espNowSendCallback(const wifi_tx_info_t *tx_info, esp_now_send_status_t status) {
    if (status != ESP_NOW_SEND_SUCCESS) {
        if (debugETM || debugEnabled) {
            Serial.printf("[SEND CB] MAC-layer FAILED to: %02X:%02X:%02X:%02X:%02X:%02X\n",
                tx_info->des_addr[0], tx_info->des_addr[1], tx_info->des_addr[2],
                tx_info->des_addr[3], tx_info->des_addr[4], tx_info->des_addr[5]);
        }
    }
}

//*******************************
/// Kyber Serial Forwarding Commands
//*******************************
void forwardDataFromKyber() {
  static uint8_t buffer[64];

  // Check if Serial2 has data
  if (Serial2.available() > 0) {
    int len = Serial2.readBytes((char*)buffer, sizeof(buffer));

    // If targeting is enabled, send to specific targets
    if (kyberUseTargeting) {
      for (int i = 0; i < MAX_KYBER_TARGETS; i++) {
        if (kyberTargets[i].enabled) {
          int targetWCB = kyberTargets[i].targetWCB;
          int targetPort = kyberTargets[i].targetPort;
          
          // If local to this WCB, write to serial port
          if (targetWCB == WCB_Number) {
            Stream &targetSerial = getSerialStream(targetPort);
            targetSerial.write(buffer, len);
            targetSerial.flush();
            if (debugKyber) {
              Serial.printf("Kyber: Forwarded %d bytes to local S%d\n", len, targetPort);
            }
          } 
          // If remote, send via ESP-NOW to specific WCB/port using 'K' target
          else {
            sendESPNowRawToPort(buffer, len, targetWCB, targetPort);
            if (debugKyber) {
              Serial.printf("Kyber: Sent %d bytes to WCB%d S%d\n", len, targetWCB, targetPort);
            }
          }
        }
      }
    } 
    // Broadcast mode - send to all WCBs
    else {
      // Forward to local Serial1 if Maestro is local
      if (Kyber_Local) {
        Serial1.write(buffer, len);
        Serial1.flush();
      }
      
      // Broadcast to all remote WCBs
      sendESPNowRaw(buffer, len);
      
      if (debugKyber) {
        Serial.printf("Kyber: Broadcast %d bytes\n", len);
      }
    }
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


void processLocalCommand(const String &message) {

    // ---- Help: bare ? or HELP ----
  if (message.isEmpty() || message.equals("?") || message.equals("HELP") || message.equals("help")) {
        printCommandHelp("");
        return;
    }

    if (message.endsWith("?")) {  // no space required
        String cmd = message.substring(0, message.length() - 1);
        cmd.trim();
        printCommandHelp(cmd);
        return;
    }

    // ---- Split on first comma to get root command and args ----
    int firstComma = message.indexOf(',');
    String rootCmd = (firstComma == -1) ? message : message.substring(0, firstComma);
    String rootUpper = rootCmd;
    rootUpper.toUpperCase();
    String args = (firstComma == -1) ? "" : message.substring(firstComma + 1);
    String argsUpper = args;
    argsUpper.toUpperCase();

    // ========================================
    // NEW FORMAT COMMANDS
    // ========================================

    // --- ?DEBUG,... ---
    if (rootUpper == "DEBUG") {
        if (argsUpper == "ON") {
            debugEnabled = true;
            Serial.println("Debugging enabled");
        } else if (argsUpper == "OFF") {
            debugEnabled = false;
            Serial.println("Debugging disabled");
        } else if (argsUpper == "KYBER,ON") {
            debugKyber = true;
            Serial.println("Kyber debugging enabled");
        } else if (argsUpper == "KYBER,OFF") {
            debugKyber = false;
            Serial.println("Kyber debugging disabled");
        } else if (argsUpper == "PWM,ON") {
            debugPWMPassthrough = true;
            Serial.println("PWM debugging enabled");
        } else if (argsUpper == "PWM,OFF") {
            debugPWMPassthrough = false;
            Serial.println("PWM debugging disabled");
        } else if (argsUpper == "ETM,ON") {
            debugETM = true;
            Serial.println("ETM debugging enabled");
        } else if (argsUpper == "ETM,OFF") {
            debugETM = false;
            Serial.println("ETM debugging disabled");
        } else {
            Serial.println("Invalid DEBUG command. Use: ?DEBUG ?");
        }
        return;
    }

    // --- ?MAP,... ---
    if (rootUpper == "MAP") {
        int secondComma = args.indexOf(',');
        String mapType = (secondComma == -1) ? args : args.substring(0, secondComma);
        String mapTypeUpper = mapType;
        mapTypeUpper.toUpperCase();
        String mapArgs = (secondComma == -1) ? "" : args.substring(secondComma + 1);
        String mapArgsUpper = mapArgs;
        mapArgsUpper.toUpperCase();

        if (mapTypeUpper == "SERIAL") {
            if (mapArgsUpper == "LIST") {
                listSerialMonitorMappings();
            } else if (mapArgsUpper == "CLEAR,ALL") {
                clearAllSerialMonitorMappings();
            } else if (mapArgsUpper.startsWith("CLEAR,S")) {
                String portPart = mapArgs.substring(mapArgs.indexOf(',') + 1);
                removeSerialMonitorMapping(portPart);
            } else {
                int thirdComma = mapArgs.indexOf(',');
                if (thirdComma == -1) {
                    Serial.println("Invalid format. Use: ?MAP,SERIAL,Sx,dest  or  ?MAP,SERIAL,Sx,R,dest");
                    return;
                }
                String portStr = mapArgs.substring(0, thirdComma);
                String remainder = mapArgs.substring(thirdComma + 1);
                String remainderUpper = remainder;
                remainderUpper.toUpperCase();

                String legacyStr;
                if (remainderUpper.startsWith("R,") || remainderUpper == "R") {
                    String dest = remainder.substring(2);
                    legacyStr = portStr + "R," + dest;
                } else {
                    legacyStr = portStr + "," + remainder;
                }
                addSerialMonitorMapping(legacyStr);
            }
        } else if (mapTypeUpper == "PWM") {
            if (mapArgsUpper == "LIST") {
                listPWMMappings();
            } else if (mapArgsUpper == "CLEAR,ALL") {
                clearAllPWMMappings();
            } else if (mapArgsUpper.startsWith("CLEAR,S")) {
                String portPart = mapArgs.substring(mapArgs.indexOf(',') + 1);
                int port = portPart.substring(1).toInt();
                removePWMMapping(port);
            } else if (mapArgsUpper.startsWith("OUT,S")) {
                String portPart = mapArgs.substring(mapArgs.indexOf(',') + 1);
                int port = portPart.substring(1).toInt();
                addPWMOutputPort(port);
            } else {
                int thirdComma = mapArgs.indexOf(',');
                if (thirdComma == -1) {
                    Serial.println("Invalid format. Use: ?MAP,PWM,Sx,dest");
                    return;
                }
                String portStr = mapArgs.substring(0, thirdComma);
                int port = portStr.substring(1).toInt();
                String dest = mapArgs.substring(thirdComma + 1);
                String pwmConfig = "PMS" + String(port) + "," + dest;
                addPWMMapping(pwmConfig);
            }
        } else if (mapTypeUpper == "CLEAR" && mapArgsUpper == "ALL") {
            clearAllSerialMonitorMappings();
            clearAllPWMMappings();
            Serial.println("All serial and PWM mappings cleared");
        } else {
            Serial.println("Invalid MAP command. Use: ?MAP ?");
        }
        return;
    }

    // --- ?BAUD,Sx,rate ---
    if (rootUpper == "BAUD") {
        int secondComma = args.indexOf(',');
        if (secondComma == -1) {
            Serial.println("Invalid format. Use: ?BAUD,Sx,rate");
            return;
        }
        String portStr = args.substring(0, secondComma);
        portStr.toUpperCase();
        int port = portStr.substring(1).toInt();
        int baud = args.substring(secondComma + 1).toInt();
        updateBaudRate(port, baud);
        return;
    }

    // --- ?LABEL,Sx,text  /  ?LABEL,CLEAR,Sx  /  ?LABEL,CLEAR,ALL ---
    if (rootUpper == "LABEL") {
        int secondComma = args.indexOf(',');
        String labelArg1 = (secondComma == -1) ? args : args.substring(0, secondComma);
        String labelArg1Upper = labelArg1;
        labelArg1Upper.toUpperCase();

        if (labelArg1Upper == "CLEAR") {
            String target = (secondComma == -1) ? "" : args.substring(secondComma + 1);
            target.toUpperCase();
            if (target == "ALL") {
                for (int i = 1; i <= 5; i++) clearSerialLabel(i);
                Serial.println("All serial labels cleared");
            } else if (target.startsWith("S")) {
                clearSerialLabel(target.substring(1).toInt());
            } else {
                Serial.println("Invalid format. Use: ?LABEL,CLEAR,Sx or ?LABEL,CLEAR,ALL");
            }
        } else if (labelArg1Upper.startsWith("S")) {
            int port = labelArg1.substring(1).toInt();
            if (secondComma == -1) {
                Serial.println("Invalid format. Use: ?LABEL,Sx,text");
                return;
            }
            String label = args.substring(secondComma + 1);
            label.trim();
            saveSerialLabelToPreferences(port, label);
        } else {
            Serial.println("Invalid LABEL command. Use: ?LABEL ?");
        }
        return;
    }

    // --- ?BCAST,IN/OUT,Sx,ON/OFF  /  ?BCAST,RESET ---
    if (rootUpper == "BCAST") {
        if (argsUpper == "RESET") {
            resetBroadcastSettingsNamespace();
        } else {
            int secondComma = args.indexOf(',');
            int thirdComma = (secondComma == -1) ? -1 : args.indexOf(',', secondComma + 1);
            if (secondComma == -1 || thirdComma == -1) {
                Serial.println("Invalid format. Use: ?BCAST,IN/OUT,Sx,ON/OFF");
                return;
            }
            String direction = args.substring(0, secondComma);
            direction.toUpperCase();
            String portStr = args.substring(secondComma + 1, thirdComma);
            portStr.toUpperCase();
            String state = args.substring(thirdComma + 1);
            state.toUpperCase();

            int port = portStr.substring(1).toInt();
            if (port < 1 || port > 5) {
                Serial.println("Invalid port. Must be S1-S5");
                return;
            }
            bool enable = (state == "ON");

            if (direction == "IN") {
                blockBroadcastFrom[port - 1] = !enable;
                saveBroadcastBlockSettings();
                Serial.printf("Broadcast INPUT on S%d: %s\n", port, enable ? "Enabled" : "Disabled");
            } else if (direction == "OUT") {
                serialBroadcastEnabled[port - 1] = enable;
                saveBroadcastSettingsToPreferences();
                Serial.printf("Broadcast OUTPUT on S%d: %s\n", port, enable ? "Enabled" : "Disabled");
            } else {
                Serial.println("Invalid direction. Use IN or OUT");
            }
        }
        return;
    }

    // --- ?KYBER,LOCAL/REMOTE/CLEAR/LIST ---
    if (rootUpper == "KYBER") {
        if (argsUpper == "LIST") {
            printKyberList();
        } else if (argsUpper.startsWith("LOCAL") || argsUpper.startsWith("REMOTE") || argsUpper.startsWith("CLEAR")) {
            storeKyberSettings(args);
            printKyberSettings();
        } else {
            Serial.println("Invalid KYBER command. Use: ?KYBER ?");
        }
        return;
    }

    // --- ?ETM,... ---
    if (rootUpper == "ETM") {
        int secondComma = args.indexOf(',');
        String etmCmd = (secondComma == -1) ? args : args.substring(0, secondComma);
        String etmCmdUpper = etmCmd;
        etmCmdUpper.toUpperCase();
        String etmVal = (secondComma == -1) ? "" : args.substring(secondComma + 1);

        if (etmCmdUpper == "ON") {
            etmEnabled = true;
            saveETMSettings();
            Serial.println("ETM enabled");
        } else if (etmCmdUpper == "OFF") {
            etmEnabled = false;
            saveETMSettings();
            Serial.println("ETM disabled");
        } else if (etmCmdUpper == "TIMEOUT") {
            etmTimeoutMs = etmVal.toInt();
            saveETMSettings();
            Serial.printf("ETM timeout set to %d ms\n", etmTimeoutMs);
        } else if (etmCmdUpper == "HB") {
            etmHeartbeatSec = etmVal.toInt();
            saveETMSettings();
            Serial.printf("ETM heartbeat set to %d sec\n", etmHeartbeatSec);
        } else if (etmCmdUpper == "MISS") {
            etmMissedHeartbeats = etmVal.toInt();
            saveETMSettings();
            Serial.printf("ETM missed heartbeats set to %d\n", etmMissedHeartbeats);
        } else if (etmCmdUpper == "BOOT") {
            etmBootHeartbeatSec = etmVal.toInt();
            saveETMSettings();
            Serial.printf("ETM boot window set to %d sec\n", etmBootHeartbeatSec);
        } else if (etmCmdUpper == "COUNT") {
            etmCharMessageCount = constrain(etmVal.toInt(), 10, 200);
            saveETMSettings();
            Serial.printf("ETM char message count set to %d\n", etmCharMessageCount);
        } else if (etmCmdUpper == "DELAY") {
            etmCharDelayMs = etmVal.toInt();
            saveETMSettings();
            Serial.printf("ETM char delay set to %d ms\n", etmCharDelayMs);
        } else if (etmCmdUpper == "CHAR") {
            startETMChar();
        } else {
            Serial.println("Invalid ETM command. Use: ?ETM ?");
        }
        return;
    }

    // --- ?MAC,2/3,xx ---
    if (rootUpper == "MAC") {
        int secondComma = args.indexOf(',');
        if (secondComma == -1) {
            Serial.println("Invalid format. Use: ?MAC,2,xx or ?MAC,3,xx");
            return;
        }
        String octet = args.substring(0, secondComma);
        String hexVal = args.substring(secondComma + 1);
        int newValue = strtoul(hexVal.c_str(), NULL, 16);
        if (octet == "2") {
            umac_oct2 = static_cast<uint8_t>(newValue);
            saveMACPreferences();
            Serial.printf("Updated 2nd MAC octet to 0x%02X\n", umac_oct2);
        } else if (octet == "3") {
            umac_oct3 = static_cast<uint8_t>(newValue);
            saveMACPreferences();
            Serial.printf("Updated 3rd MAC octet to 0x%02X\n", umac_oct3);
        } else {
            Serial.println("Invalid octet. Use 2 or 3");
        }
        return;
    }

    // --- ?EPASS,password ---
    if (rootUpper == "EPASS") {
        if (args.length() == 0) {
            Serial.println("Invalid format. Use: ?EPASS,password");
            return;
        }
        if (args.length() < sizeof(espnowPassword)) {
            setESPNowPassword(args.c_str());
            Serial.printf("ESP-NOW password updated to: %s\n", args.c_str());
        } else {
            Serial.println("Password too long. Max 39 characters.");
        }
        return;
    }

    // --- ?WCB,x ---
    if (rootUpper == "WCB") {
        int newWCB = args.toInt();
        if (newWCB >= 1 && newWCB <= 9) {
            saveWCBNumberToPreferences(newWCB);
        } else {
            Serial.println("Invalid WCB number. Must be 1-9");
        }
        return;
    }

    // --- ?WCBQ,x ---
    if (rootUpper == "WCBQ") {
        int qty = args.toInt();
        saveWCBQuantityPreferences(qty);
        return;
    }

    // --- ?MAESTRO,... ---
    if (rootUpper == "MAESTRO") {
        if (argsUpper == "LIST") {
            printMaestroSettings();
        } else if (argsUpper == "ENABLE") {
            enableMaestroSerialBaudRate();
        } else if (argsUpper == "DISABLE") {
            disableMaestroSerialBaudRate();
        } else if (argsUpper == "CLEAR,ALL") {
            clearAllMaestroConfigs();
        } else if (argsUpper.startsWith("CLEAR,")) {
            int maestroID = args.substring(args.indexOf(',') + 1).toInt();
            clearAllMaestroConfigs();  // use clearAllMaestroConfigs since no per-ID version exists
            Serial.printf("Cleared Maestro config (ID requested: %d)\n", maestroID);
        } else {
            configureMaestro(args);
        }
        return;
    }

    // --- ?SEQ,... ---
    if (rootUpper == "SEQ") {
        int secondComma = args.indexOf(',');
        String seqCmd = (secondComma == -1) ? args : args.substring(0, secondComma);
        String seqCmdUpper = seqCmd;
        seqCmdUpper.toUpperCase();
        String seqArgs = (secondComma == -1) ? "" : args.substring(secondComma + 1);

        if (seqCmdUpper == "LIST") {
            listStoredCommands();
        } else if (seqCmdUpper == "CLEAR") {
            String seqArgsUpper = seqArgs;
            seqArgsUpper.toUpperCase();
            if (seqArgsUpper == "ALL") {
                clearAllStoredCommands();
                Serial.println("All stored sequences cleared");
            } else {
                eraseStoredCommandByName(seqArgs);
            }
        } else if (seqCmdUpper == "SAVE") {
            saveStoredCommandsToPreferences(seqArgs);
        } else {
            Serial.println("Invalid SEQ command. Use: ?SEQ ?");
        }
        return;
    }

    // --- ?STATS,RESET or ?STATS ---
    if (rootUpper == "STATS") {
        if (argsUpper == "RESET") {
            resetESPNowStats();
        } else {
            printESPNowStats();
        }
        return;
    }

    // --- ?TRACK,... ---
    if (rootUpper == "TRACK") {
        if (argsUpper == "ON") {
            trackCommandDelivery = true;
            Serial.println("Delivery tracking enabled");
        } else if (argsUpper == "OFF") {
            trackCommandDelivery = false;
            Serial.println("Delivery tracking disabled");
        } else if (argsUpper == "STATUS") {
            printTrackingStatus();
        } else {
            Serial.println("Invalid TRACK command. Use: ?TRACK ?");
        }
        return;
    }

    // --- ?DELIM,x ---
    if (rootUpper == "DELIM") {
        if (args.length() == 1) {
            setCommandDelimiter(args.charAt(0));
            Serial.printf("Delimiter updated to: '%c'\n", commandDelimiter);
        } else {
            Serial.println("Invalid format. Use: ?DELIM,x where x is one character");
        }
        return;
    }

    // --- ?FUNCCHAR,x ---
    if (rootUpper == "FUNCCHAR") {
        if (args.length() == 1) {
            LocalFunctionIdentifier = args.charAt(0);
            saveLocalFunctionIdentifierAndCommandCharacter();
            Serial.printf("Local function identifier updated to '%c'\n", LocalFunctionIdentifier);
        } else {
            Serial.println("Invalid format. Use: ?FUNCCHAR,x where x is one character");
        }
        return;
    }

    // --- ?CMDCHAR,x ---
    if (rootUpper == "CMDCHAR") {
        if (args.length() == 1) {
            CommandCharacter = args.charAt(0);
            saveLocalFunctionIdentifierAndCommandCharacter();
            Serial.printf("Command character updated to '%c'\n", CommandCharacter);
        } else {
            Serial.println("Invalid format. Use: ?CMDCHAR,x where x is one character");
        }
        return;
    }

    // --- ?ERASE,NVS ---
    if (rootUpper == "ERASE") {
        if (argsUpper == "NVS") {
            eraseNVSFlash();
        } else {
            Serial.println("Invalid format. Use: ?ERASE,NVS");
        }
        return;
    }

    // --- ?HW,x ---
    if (rootUpper == "HW") {
        int temp_hw_version = args.toInt();
        saveHWversion(temp_hw_version);
        return;
    }

    // ========================================
    // LEGACY FORMAT COMMANDS
    // ========================================

    if (message == "don" || message == "DON") {
        debugEnabled = true;
        Serial.println("Debugging enabled");
    } else if (message == "doff" || message == "DOFF") {
        debugEnabled = false;
        Serial.println("Debugging disabled");
    } else if (message.startsWith("dkoff") || message.startsWith("DKOFF")) {
        debugKyber = false;
        Serial.println("Kyber debugging disabled");
    } else if (message.startsWith("dkon") || message.startsWith("DKON")) {
        debugKyber = true;
        Serial.println("Kyber debugging enabled");
    } else if (message.startsWith("dpwmoff") || message.startsWith("DPWMOFF")) {
        debugPWMPassthrough = false;
        Serial.println("PWM debugging disabled");
    } else if (message.startsWith("dpwmon") || message.startsWith("DPWMON")) {
        debugPWMPassthrough = true;
        Serial.println("PWM debugging enabled");
    } else if (message.startsWith("detmoff") || message.startsWith("DETMOFF")) {
        debugETM = false;
        Serial.println("ETM debugging disabled");
    } else if (message.startsWith("detmon") || message.startsWith("DETMON")) {
        debugETM = true;
        Serial.println("ETM debugging enabled");
    } else if (message.startsWith("lf") || message.startsWith("LF")) {
        updateLocalFunctionIdentifier(message);
    } else if (message.equals("cclear") || message.equals("CCLEAR")) {
        clearAllStoredCommands();
        Serial.println("All stored sequences cleared");
    } else if (message.startsWith("cc") || message.startsWith("CC")) {
        updateCommandCharacter(message);
    } else if (message.startsWith("cs") || message.startsWith("CS")) {
        storeCommand(message);
    } else if (message.startsWith("ce") || message.startsWith("CE")) {
        eraseSingleCommand(message);
    } else if (message == "reboot" || message == "REBOOT") {
        reboot();
    } else if (message == "config" || message == "CONFIG") {
        printConfigInfo();
    } else if (message == "backup" || message == "BACKUP") {
        printBackupConfig();
    } else if (message.startsWith("d") || message.startsWith("D")) {
        updateCommandDelimiter(message);
    } else if (message.startsWith("m2") || message.startsWith("M2")) {
        update2ndMACOctet(message);
    } else if (message.startsWith("m3") || message.startsWith("M3")) {
        update3rdMACOctet(message);
    } else if (message == "wcb_erase" || message == "WCB_ERASE") {
        eraseNVSFlash();
    } else if (message.startsWith("wcbq") || message.startsWith("WCBQ")) {
        updateWCBQuantity(message);
    } else if (message.startsWith("wcb") || message.startsWith("WCB")) {
        updateWCBNumber(message);
    } else if (message.startsWith("epass") || message.startsWith("EPASS")) {
        updateESPNowPassword(message);
    } else if (message == "maestro_enable" || message == "MAESTRO_ENABLE") {
        enableMaestroSerialBaudRate();
    } else if (message == "maestro_disable" || message == "MAESTRO_DISABLE") {
        disableMaestroSerialBaudRate();
    } else if (message == "maestro_list" || message == "MAESTRO_LIST") {
        printMaestroSettings();
    } else if (message.startsWith("maestro_clear") || message.startsWith("MAESTRO_CLEAR")) {
        clearAllMaestroConfigs();
    } else if (message == "maestro_default" || message == "MAESTRO_DEFAULT") {
        clearAllMaestroConfigs();
    } else if (message.startsWith("maestro") || message.startsWith("MAESTRO")) {
        configureMaestro(message.substring(7));
    } else if (message.equals("kyber_local") || message.equals("KYBER_LOCAL") ||
               message.startsWith("kyber_local,") || message.startsWith("KYBER_LOCAL,")) {
        int underscorePos = message.indexOf('_');
        String afterLocal = message.substring(underscorePos + 6);
        if (afterLocal.startsWith(",")) afterLocal = afterLocal.substring(1);
        storeKyberSettings(afterLocal.length() > 0 ? "local," + afterLocal : "local");
        printKyberSettings();
    } else if (message.equals("kyber_remote") || message.equals("KYBER_REMOTE")) {
        storeKyberSettings("remote");
        printKyberSettings();
    } else if (message.equals("kyber_clear") || message.equals("KYBER_CLEAR")) {
        storeKyberSettings("clear");
        printKyberSettings();
    } else if (message.equals("kyber_list") || message.equals("KYBER_LIST")) {
        printKyberList();
    } else if (message.startsWith("hw") || message.startsWith("HW")) {
        updateHWVersion(message);
    } else if (message.startsWith("smclear") || message.startsWith("SMCLEAR")) {
        clearAllSerialMonitorMappings();
    } else if (message.startsWith("smlist") || message.startsWith("SMLIST")) {
        listSerialMonitorMappings();
    } else if (message.startsWith("smr") || message.startsWith("SMR")) {
        removeSerialMonitorMapping(message.substring(3));
    } else if (message.startsWith("sm") || message.startsWith("SM")) {
        addSerialMonitorMapping(message.substring(2));
    } else if (message.startsWith("pms") || message.startsWith("PMS")) {
        addPWMMapping("PMS" + message.substring(3));
    } else if (message.startsWith("pclear") || message.startsWith("PCLEAR")) {
        clearAllPWMMappings();
    } else if (message.startsWith("plist") || message.startsWith("PLIST")) {
        listPWMMappings();
    } else if (message.startsWith("prs") || message.startsWith("PRS")) {
        int port = message.substring(3).toInt();
        removePWMMapping(port);
    } else if (message.startsWith("pos") || message.startsWith("POS")) {
        int port = message.substring(3).toInt();
        addPWMOutputPort(port);
    } else if (message.startsWith("sls") || message.startsWith("SLS")) {
        updateSerialLabel(message.substring(3));
    } else if (message.startsWith("slc") || message.startsWith("SLC")) {
        clearSerialLabel(message.substring(4).toInt());
    } else if (message.startsWith("sbi") || message.startsWith("SBI")) {
        updateBroadcastInputSetting(message.substring(3));
    } else if (message.startsWith("sbo") || message.startsWith("SBO")) {
        updateBroadcastOutputSetting(message.substring(3));
    } else if (message == "reset_broadcast" || message == "RESET_BROADCAST") {
        resetBroadcastSettingsNamespace();
    } else if (message.startsWith("statsreset") || message.startsWith("STATSRESET")) {
        resetESPNowStats();
    } else if (message == "stats" || message == "STATS") {
        printESPNowStats();
    } else if (message.startsWith("track_all_on") || message.startsWith("TRACK_ALL_ON")) {
        trackCommandDelivery = true;
        Serial.println("Delivery tracking enabled");
    } else if (message.startsWith("track_all_off") || message.startsWith("TRACK_ALL_OFF")) {
        trackCommandDelivery = false;
        Serial.println("Delivery tracking disabled");
    } else if (message.startsWith("track_status") || message.startsWith("TRACK_STATUS")) {
        printTrackingStatus();
    } else if (message.startsWith("etmcharcount") || message.startsWith("ETMCHARCOUNT")) {
        etmCharMessageCount = constrain(message.substring(12).toInt(), 10, 200);
        saveETMSettings();
        Serial.printf("ETM char count set to %d\n", etmCharMessageCount);
    } else if (message.startsWith("etmchardelay") || message.startsWith("ETMCHARDELAY")) {
        etmCharDelayMs = message.substring(12).toInt();
        saveETMSettings();
        Serial.printf("ETM char delay set to %d ms\n", etmCharDelayMs);
    } else if (message == "etmchar" || message == "ETMCHAR") {
        startETMChar();
    } else if (message.startsWith("etmtimeout") || message.startsWith("ETMTIMEOUT")) {
        etmTimeoutMs = message.substring(10).toInt();
        saveETMSettings();
        Serial.printf("ETM timeout set to %d ms\n", etmTimeoutMs);
    } else if (message.startsWith("etmboot") || message.startsWith("ETMBOOT")) {
        etmBootHeartbeatSec = message.substring(7).toInt();
        saveETMSettings();
        Serial.printf("ETM boot window set to %d sec\n", etmBootHeartbeatSec);
    } else if (message.startsWith("etmhb") || message.startsWith("ETMHB")) {
        etmHeartbeatSec = message.substring(5).toInt();
        saveETMSettings();
        Serial.printf("ETM heartbeat set to %d sec\n", etmHeartbeatSec);
    } else if (message.startsWith("etmmiss") || message.startsWith("ETMMISS")) {
        etmMissedHeartbeats = message.substring(7).toInt();
        saveETMSettings();
        Serial.printf("ETM missed heartbeats set to %d\n", etmMissedHeartbeats);
    } else if (message == "etmon" || message == "ETMON") {
        etmEnabled = true;
        saveETMSettings();
        Serial.println("ETM enabled");
    } else if (message == "etmoff" || message == "ETMOFF") {
        etmEnabled = false;
        saveETMSettings();
        Serial.println("ETM disabled");
    } else if (message.startsWith("bauds") || message.startsWith("BAUDS")) {
        int commaPos = message.indexOf(',');
        if (commaPos != -1) {
            int port = message.substring(5, commaPos).toInt();
            int baud = message.substring(commaPos + 1).toInt();
            updateBaudRate(port, baud);
        }
    } else if (message.startsWith("s") || message.startsWith("S")) {
        updateSerialSettings(message);
    } else {
        Serial.printf("Unknown command: %s\n", message.c_str());
        Serial.println("Type '? ?' for help");
    }
}

//*******************************
/// Help System
//*******************************

void resetESPNowStats() {
    espnowCommandAttempts = 0;
    espnowCommandSuccess = 0;
    espnowCommandFailed = 0;
    espnowCommandDelivered = 0;
    espnowPWMAttempts = 0;
    espnowPWMSuccess = 0;
    espnowPWMFailed = 0;
    espnowRawAttempts = 0;
    espnowRawSuccess = 0;
    espnowRawFailed = 0;
    for (int b = 0; b < 9; b++) {
        etmStatsSent[b] = 0;
        etmStatsAckd[b] = 0;
        etmStatsRetries[b] = 0;
        etmStatsFailed[b] = 0;
    }
    Serial.println("ESP-NOW statistics reset.");
}

void printTrackingStatus() {
    Serial.printf("Command delivery tracking: %s\n",
                  trackCommandDelivery ? "ENABLED" : "DISABLED");
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
  if (message.length() >= 2) {  // <-- Change from == 2 to >= 2
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

void updateSerialLabel(const String &message) {
    // Format: ?SLSx,label text
    if (message.length() < 4) {
        Serial.println("Invalid format. Use: ?SLSx,label where x=1-5");
        return;
    }
    
    // Check for 'S' prefix after SL
    if (!message.startsWith("S") && !message.startsWith("s")) {
        Serial.println("Invalid format. Use: ?SLSx,label where x=1-5");
        return;
    }
    
    int commaPos = message.indexOf(',');
    if (commaPos == -1) {
        Serial.println("Invalid format. Use: ?SLSx,label");
        return;
    }
    
    // Extract port number (between 'S' and ',')
    String portStr = message.substring(3, commaPos);  // Skip "SLS"
    int port = portStr.toInt();
    
    if (port < 1 || port > 5) {
        Serial.println("Invalid port. Must be 1-5");
        return;
    }
    
    String label = message.substring(commaPos + 1);
    label.trim();
    
    if (label.length() > 30) {
        Serial.println("Label too long. Maximum 30 characters.");
        return;
    }
    
    saveSerialLabelToPreferences(port, label);
}

void clearSerialLabelCommand(const String &message) {
    // Format: ?SLCSx where x=1-5
    if (message.length() < 4) {
        Serial.println("Invalid format. Use: ?SLCSx where x=1-5");
        return;
    }
    
    // Check for 'S' prefix after SLC
    String portSection = message.substring(3);  // Everything after "SLC"
    if (!portSection.startsWith("S") && !portSection.startsWith("s")) {
        Serial.println("Invalid format. Use: ?SLCSx where x=1-5");
        return;
    }
    
    int port = portSection.substring(1).toInt();  // Skip the 'S'
    if (port < 1 || port > 5) {
        Serial.println("Invalid port. Must be 1-5");
        return;
    }
    
    clearSerialLabel(port);
}

void updateBroadcastInputSetting(const String &message) {
    // Format: ?SBISxON or ?SBISxOFF where x=1-5
    if (message.length() < 6) {
        Serial.println("Invalid format. Use: ?SBISxON or ?SBISxOFF where x=1-5");
        return;
    }
    
    // Extract everything after "SBI"
    String portSection = message.substring(3);
    
    if (!portSection.startsWith("S") && !portSection.startsWith("s")) {
        Serial.println("Invalid format. Use: ?SBISxON or ?SBISxOFF where x=1-5");
        return;
    }
    
    // Find where ON/OFF starts
    String upperSection = portSection;
    upperSection.toUpperCase();
    int onPos = upperSection.indexOf("ON");
    int offPos = upperSection.indexOf("OFF");
    
    if (onPos == -1 && offPos == -1) {
        Serial.println("Invalid format. Must end with ON or OFF");
        return;
    }
    
    // Extract port number and state
    String portStr;
    bool enable;
    
    if (offPos != -1 && (onPos == -1 || offPos < onPos)) {
        // OFF comes first or ON doesn't exist
        portStr = portSection.substring(1, offPos);
        enable = false;
    } else {
        // ON comes first
        portStr = portSection.substring(1, onPos);
        enable = true;
    }
    
    int port = portStr.toInt();
    
    if (port < 1 || port > 5) {
        Serial.println("Invalid port number. Must be 1-5");
        return;
    }
    
    // INVERTED LOGIC: enable=true means ALLOW (false=not blocked), enable=false means BLOCK (true=blocked)
    blockBroadcastFrom[port - 1] = !enable;
    saveBroadcastBlockSettings();
    
    Serial.printf("Serial%d broadcast input: %s\n", port, enable ? "ENABLED" : "DISABLED");
}

void updateBroadcastOutputSetting(const String &message) {
    // Format: ?SBOSxON or ?SBOSxOFF where x=1-5
    if (message.length() < 6) {
        Serial.println("Invalid format. Use: ?SBOSxON or ?SBOSxOFF where x=1-5");
        return;
    }
    
    // Extract everything after "SBO"
    String portSection = message.substring(3);
    
    if (!portSection.startsWith("S") && !portSection.startsWith("s")) {
        Serial.println("Invalid format. Use: ?SBOSxON or ?SBOSxOFF where x=1-5");
        return;
    }
    
    // Find where ON/OFF starts
    String upperSection = portSection;
    upperSection.toUpperCase();
    int onPos = upperSection.indexOf("ON");
    int offPos = upperSection.indexOf("OFF");
    
    if (onPos == -1 && offPos == -1) {
        Serial.println("Invalid format. Must end with ON or OFF");
        return;
    }
    
    // Extract port number and state
    String portStr;
    bool enable;
    
    if (offPos != -1 && (onPos == -1 || offPos < onPos)) {
        // OFF comes first or ON doesn't exist
        portStr = portSection.substring(1, offPos);
        enable = false;
    } else {
        // ON comes first
        portStr = portSection.substring(1, onPos);
        enable = true;
    }
    
    int port = portStr.toInt();
    
    if (port < 1 || port > 5) {
        Serial.println("Invalid port number. Must be 1-5");
        return;
    }
    
    serialBroadcastEnabled[port - 1] = enable;
    saveBroadcastSettingsToPreferences();
    
    Serial.printf("Serial%d broadcast output: %s\n", port, enable ? "ENABLED" : "DISABLED");
}

void updateSerialSettings(const String &message){
  int port = message.substring(1, 2).toInt();
  int state = message.substring(2, 3).toInt();
  if ((state == 0 || state == 1) && message.length()==3){
    if (port >= 1 && port <= 5) {
      Serial.println("⚠️ WARNING: ?Sx0/1 is being deprecated. Use ?SBOx0/1 for broadcast output control. This command will still function for now but will be removed in future versions.");
      serialBroadcastEnabled[port - 1] = (state == 1);
      saveBroadcastSettingsToPreferences();
      Serial.printf("Serial%d broadcast %s and stored in NVS\n",
          port, serialBroadcastEnabled[port - 1] ? "Enabled" : "Disabled");
    }
  } else if (message.startsWith("baud") || message.startsWith("BAUD")) {
    updateSerialBaudRate(message.substring(4));
    return;
  } else {
    int baud = message.substring(2).toInt();
    if (port >= 1 && port <= 5) {
      Serial.println("⚠️  Command is being deprecated: Use ?BAUDSx,yyyyy instead (e.g., ?BAUDS1,57600).  This command will still function for now but may be removed in future versions.");
      updateBaudRate(port, baud);
      Serial.printf("Updated Serial%d baud rate to %d and stored in NVS\n", port, baud);
    }
  }
}

void updateSerialBaudRate(const String &message) {
  // Format: Sx,yyyyy  (e.g., S1,57600)
  if (!message.startsWith("S") && !message.startsWith("s")) {
    Serial.println("Invalid format. Use ?BAUDSx,yyyyy (e.g., ?BAUDS1,57600)");
    return;
  }

  int commaIndex = message.indexOf(',');
  if (commaIndex == -1) {
    Serial.println("Invalid format. Use ?BAUDSx,yyyyy (e.g., ?BAUDS1,57600)");
    return;
  }

  int port = message.substring(1, commaIndex).toInt();
  int baud = message.substring(commaIndex + 1).toInt();

  if (port < 1 || port > 5) {
    Serial.println("Invalid serial port. Must be S1-S5");
    return;
  }

  updateBaudRate(port, baud);
  Serial.printf("Updated Serial%d baud rate to %d and stored in NVS\n", port, baud);
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

 void printBackupConfig() {
    Serial.println("\n" + commentDelimiter + " ========================================");
    Serial.println(commentDelimiter + " WCB Configuration Backup");
    Serial.println(commentDelimiter + " Copy and paste these commands to restore");
    Serial.println(commentDelimiter + " ========================================\n");

    String chainedConfig = "";
    String chainedConfigDefault = "";
    String cmd;
    char hexBuffer[3];

    // ---- Hardware Version ----
    String hwCmd = "";
    if      (wcb_hw_version == 1)  hwCmd = "?HW,1";
    else if (wcb_hw_version == 21) hwCmd = "?HW,21";
    else if (wcb_hw_version == 23) hwCmd = "?HW,23";
    else if (wcb_hw_version == 24) hwCmd = "?HW,24";
    else if (wcb_hw_version == 31) hwCmd = "?HW,31";
    else if (wcb_hw_version == 32) hwCmd = "?HW,32";
    else {
        Serial.println("WARNING: Hardware version not set! Use ?HW,xx to set version before backup.");
        hwCmd = "?HW,0  *** HARDWARE VERSION NOT SET - SET BEFORE RESTORING! ***";
    }
    Serial.println(hwCmd);
    chainedConfig += hwCmd;
    chainedConfigDefault += hwCmd;

    // ---- Network Identity ----
    sprintf(hexBuffer, "%02X", umac_oct2);
    cmd = "?MAC,2," + String(hexBuffer);
    Serial.println(cmd);
    chainedConfig += String(commandDelimiter) + cmd;
    chainedConfigDefault += "^" + cmd;

    sprintf(hexBuffer, "%02X", umac_oct3);
    cmd = "?MAC,3," + String(hexBuffer);
    Serial.println(cmd);
    chainedConfig += String(commandDelimiter) + cmd;
    chainedConfigDefault += "^" + cmd;

    cmd = "?WCB," + String(WCB_Number);
    Serial.println(cmd);
    chainedConfig += String(commandDelimiter) + cmd;
    chainedConfigDefault += "^" + cmd;

    cmd = "?WCBQ," + String(Default_WCB_Quantity);
    Serial.println(cmd);
    chainedConfig += String(commandDelimiter) + cmd;
    chainedConfigDefault += "^" + cmd;

    cmd = "?EPASS," + String(espnowPassword);
    Serial.println(cmd);
    chainedConfig += String(commandDelimiter) + cmd;
    chainedConfigDefault += "^" + cmd;

    // ---- Command Characters ----
    cmd = "?DELIM," + String(commandDelimiter);
    Serial.println(cmd);
    chainedConfig += String(commandDelimiter) + cmd;
    if (commandDelimiter != '^') {
        chainedConfigDefault += "^" + cmd;
    }

    cmd = "?FUNCCHAR," + String(LocalFunctionIdentifier);
    Serial.println(cmd);
    chainedConfig += String(commandDelimiter) + cmd;
    chainedConfigDefault += "^" + cmd;

    cmd = "?CMDCHAR," + String(CommandCharacter);
    Serial.println(cmd);
    chainedConfig += String(commandDelimiter) + cmd;
    chainedConfigDefault += "^" + cmd;

    // ---- Serial Port Baud Rates ----
    for (int i = 0; i < 5; i++) {
        cmd = "?BAUD,S" + String(i + 1) + "," + String(baudRates[i]);
        Serial.println(cmd);
        chainedConfig += String(commandDelimiter) + cmd;
        chainedConfigDefault += "^" + cmd;
    }

    // ---- Serial Port Labels ----
    for (int i = 0; i < 5; i++) {
        if (serialPortLabels[i].length() > 0) {
            cmd = "?LABEL,S" + String(i + 1) + "," + serialPortLabels[i];
            Serial.println(cmd);
            chainedConfig += String(commandDelimiter) + cmd;
            chainedConfigDefault += "^" + cmd;
        }
    }

    // ---- Broadcast Output Settings ----
    for (int i = 0; i < 5; i++) {
        cmd = "?BCAST,OUT,S" + String(i + 1) + "," + (serialBroadcastEnabled[i] ? "ON" : "OFF");
        Serial.println(cmd);
        chainedConfig += String(commandDelimiter) + cmd;
        chainedConfigDefault += "^" + cmd;
    }

    // ---- Broadcast Input Settings ----
    for (int i = 0; i < 5; i++) {
        cmd = "?BCAST,IN,S" + String(i + 1) + "," + (blockBroadcastFrom[i] ? "OFF" : "ON");
        Serial.println(cmd);
        chainedConfig += String(commandDelimiter) + cmd;
        chainedConfigDefault += "^" + cmd;
    }

    // ---- Serial Monitor Mappings ----
    for (int i = 0; i < MAX_SERIAL_MONITOR_MAPPINGS; i++) {
        if (serialMonitorMappings[i].active) {
            bool isRaw = serialMonitorMappings[i].rawMode;
            cmd = "?MAP,SERIAL,S" + String(serialMonitorMappings[i].inputPort);
            if (isRaw) cmd += ",R";
            for (int j = 0; j < serialMonitorMappings[i].outputCount; j++) {
                cmd += ",";
                if (serialMonitorMappings[i].outputs[j].wcbNumber == 0) {
                    cmd += "S" + String(serialMonitorMappings[i].outputs[j].serialPort);
                } else {
                    cmd += "W" + String(serialMonitorMappings[i].outputs[j].wcbNumber) +
                           "S" + String(serialMonitorMappings[i].outputs[j].serialPort);
                }
            }
            Serial.println(cmd);
            chainedConfig += String(commandDelimiter) + cmd;
            chainedConfigDefault += "^" + cmd;
        }
    }

    // ---- Kyber Settings ----
    if (Kyber_Local)       cmd = "?KYBER,LOCAL";
    else if (Kyber_Remote) cmd = "?KYBER,REMOTE";
    else                   cmd = "?KYBER,CLEAR";
    Serial.println(cmd);
    chainedConfig += String(commandDelimiter) + cmd;
    chainedConfigDefault += "^" + cmd;

    // ---- Maestro Settings ----
    printMaestroBackup(chainedConfig, chainedConfigDefault, commandDelimiter);

    // ---- ETM Settings ----
    cmd = etmEnabled ? "?ETM,ON" : "?ETM,OFF";
    Serial.println(cmd);
    chainedConfig += String(commandDelimiter) + cmd;
    chainedConfigDefault += "^" + cmd;

    cmd = "?ETM,TIMEOUT," + String(etmTimeoutMs);
    Serial.println(cmd);
    chainedConfig += String(commandDelimiter) + cmd;
    chainedConfigDefault += "^" + cmd;

    cmd = "?ETM,HB," + String(etmHeartbeatSec);
    Serial.println(cmd);
    chainedConfig += String(commandDelimiter) + cmd;
    chainedConfigDefault += "^" + cmd;

    cmd = "?ETM,MISS," + String(etmMissedHeartbeats);
    Serial.println(cmd);
    chainedConfig += String(commandDelimiter) + cmd;
    chainedConfigDefault += "^" + cmd;

    cmd = "?ETM,BOOT," + String(etmBootHeartbeatSec);
    Serial.println(cmd);
    chainedConfig += String(commandDelimiter) + cmd;
    chainedConfigDefault += "^" + cmd;

    cmd = "?ETM,COUNT," + String(etmCharMessageCount);
    Serial.println(cmd);
    chainedConfig += String(commandDelimiter) + cmd;
    chainedConfigDefault += "^" + cmd;

    cmd = "?ETM,DELAY," + String(etmCharDelayMs);
    Serial.println(cmd);
    chainedConfig += String(commandDelimiter) + cmd;
    chainedConfigDefault += "^" + cmd;

    // ---- Stored Sequences ----
    preferences.begin("stored_cmds", true);
    String keyList = preferences.getString("key_list", "");
    preferences.end();

    if (keyList.length() > 0) {
        int startIdx = 0;
        while (startIdx < keyList.length()) {
            int commaIndex = keyList.indexOf(',', startIdx);
            if (commaIndex == -1) commaIndex = keyList.length();
            String key = keyList.substring(startIdx, commaIndex);
            key.trim();
            if (key.length() > 0) {
                preferences.begin("stored_cmds", true);
                String value = preferences.getString(key.c_str(), "");
                preferences.end();
                cmd = "?SEQ,SAVE," + key + "," + value;
                Serial.println(cmd);
                chainedConfig += String(commandDelimiter) + cmd;
                chainedConfigDefault += "^" + cmd;
            }
            startIdx = commaIndex + 1;
        }
    }

    // ---- PWM Output Ports (before mappings) ----
    for (int i = 0; i < pwmOutputCount; i++) {
        cmd = "?MAP,PWM,OUT,S" + String(pwmOutputPorts[i]);
        Serial.println(cmd);
        chainedConfig += String(commandDelimiter) + cmd;
        chainedConfigDefault += "^" + cmd;
    }

    // ---- PWM Mappings (LAST - triggers reboot) ----
    for (int i = 0; i < MAX_PWM_MAPPINGS; i++) {
        if (pwmMappings[i].active) {
            cmd = "?MAP,PWM,S" + String(pwmMappings[i].inputPort);
            for (int j = 0; j < pwmMappings[i].outputCount; j++) {
                cmd += ",";
                if (pwmMappings[i].outputs[j].wcbNumber == 0) {
                    cmd += "S" + String(pwmMappings[i].outputs[j].serialPort);
                } else {
                    cmd += "W" + String(pwmMappings[i].outputs[j].wcbNumber) +
                           "S" + String(pwmMappings[i].outputs[j].serialPort);
                }
            }
            Serial.println(cmd);
            chainedConfig += String(commandDelimiter) + cmd;
            chainedConfigDefault += "^" + cmd;
        }
    }

    // ---- Checksums ----
    uint32_t checksum = calculateCRC32(chainedConfig);
    String checksumCmd = "?CHK" + String(checksum, HEX);
    checksumCmd.toUpperCase();

    uint32_t checksumDefault = calculateCRC32(chainedConfigDefault);
    String checksumCmdDefault = "?CHK" + String(checksumDefault, HEX);
    checksumCmdDefault.toUpperCase();

    String chainedWithChecksum = chainedConfig + String(commandDelimiter) + checksumCmd;
    String chainedWithChecksumDefault = chainedConfigDefault + "^" + checksumCmdDefault;

    Serial.println("\n" + commentDelimiter + " === For Configured Boards (Current Delimiter: '" + String(commandDelimiter) + "') ===");
    Serial.println(chainedWithChecksum);
    Serial.println(commentDelimiter + " Checksum: " + checksumCmd);

    Serial.println("\n" + commentDelimiter + " === For Factory Reset/Fresh Boards (Uses Default '^' Delimiter) ===");
    Serial.println(chainedWithChecksumDefault);
    Serial.println(commentDelimiter + " Checksum: " + checksumCmdDefault);

    Serial.println("--------- End of Backup ---------\n");
}

void verifyBackupChecksum(const String &message) {
    // Extract the provided checksum
    String providedChecksum = message.substring(3);
    providedChecksum.toUpperCase();
    
    Serial.println("Checksum verification command received.");
    Serial.println("This checksum is automatically validated when restoring a backup.");
    Serial.println("Provided checksum: " + providedChecksum);
    
    // Note: The actual verification happens during restore processing
    // This command just acknowledges receipt
}
//*******************************
/// Processing Command Character
//*******************************
void processCommandCharcter(const String &message, int sourceID) {
    
     if ((message.startsWith("s") || message.startsWith("S")) && (message.length() > 1 && message.charAt(1) != 'e' && message.charAt(1) != 'E')) {
        processSerialMessage(message);
    } else if (message.startsWith("w") || message.startsWith("W")) {
        processWCBMessage(message);
    } else if (message.startsWith("c") || message.startsWith("C") || message.startsWith(String("SEQ")) || message.startsWith(String("seq"))) {
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
    int target = message.substring(1, 2).toInt();  // Extract Serial port (0-5, where 0=USB)
    if (target < 0 || target > 5) {  // ← CHANGED: was "target < 1"
        Serial.println("Invalid serial port number. Must be between 0 and 5 (0=USB).");  // ← UPDATED MESSAGE
        return;
    }

    // Extract message to send
    String serialMessage = message.substring(2);
    serialMessage.trim();

    // Handle USB (Serial0) separately
    if (target == 0) {
        Serial.println(serialMessage);  // Send to USB
        if (debugEnabled) {
            Serial.printf("Sent to USB: %s\n", serialMessage.c_str());
        }
        return;
    }

    // Send to selected serial port (1-5)
    Stream &targetSerial = getSerialStream(target);
    writeSerialString(targetSerial, serialMessage);
    targetSerial.flush(); // Ensure it's sent immediately

    if (debugEnabled) {
      Serial.printf("Sent to %s: %s\n", getSerialLabel(target).c_str(), serialMessage.c_str());
    }
}
void processWCBMessage(const String &message){
  int targetWCB = message.substring(1, 2).toInt();
  String espnow_message = message.substring(2);
  
  // Check if target is the local WCB
  if (targetWCB == WCB_Number) {
      if (debugEnabled) {
          Serial.printf("Target WCB%d is local - processing command directly: %s\n", 
                       targetWCB, espnow_message.c_str());
      }
      // Process the command locally instead of sending via ESP-NOW
      enqueueCommand(espnow_message, 0);
      return;
  }
  
  // Remote target - send via ESP-NOW
  if (targetWCB >= 1 && targetWCB <= Default_WCB_Quantity) {
      if (debugEnabled) {
          Serial.printf("Sending Unicast ESP-NOW message to WCB%d: %s\n", 
                       targetWCB, espnow_message.c_str());
      }
      sendESPNowMessage(targetWCB, espnow_message.c_str());
  } else {
      Serial.println("Invalid WCB number for unicast.");
  }
}

void recallStoredCommand(const String &message, int sourceID) {
    Serial.print("Recall stored command request received:");
    Serial.println(message);
    if (message.startsWith("SEQ") || message.startsWith("seq")) {
        // Format: SEQkey
        Serial.println("Recalling stored sequence command...");
        String key = message.substring(3);
        key.trim();
        if (key.length() > 0) {
            recallCommandSlot(key, sourceID);
        } else {
            Serial.println("Invalid recall command. Use SEQkey to recall a command.");
        }
    } else {
  String key = message.substring(1);
  key.trim();
  if (key.length() > 0) {
    recallCommandSlot(key, sourceID);
  } else {
    Serial.println("Invalid recall command. Use ;Ckey to recall a command.");
  }
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
    
    if (port < 1 || port > 5 || pulseWidth < 500 || pulseWidth > 2500) {
        if (debugPWMEnabled) Serial.println("Invalid PWM output command");
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
    // Serial.printf("processPWMOutput: port=%d pulse=%lu txPin=%d\n", port, pulseWidth, SERIAL5_TX_PIN);

}


//*******************************
/// Processing Broadcast Function
//*******************************
//*******************************
/// Processing Broadcast Function
//*******************************
void processBroadcastCommand(const String &cmd, int sourceID) {
    if (debugEnabled) {
        Serial.printf("Broadcasting command: %s\n", cmd.c_str());
    }

    // Check if source port has an active serial monitor mapping
    bool hasMappingActive = false;
    int mappingIndex = -1;
    
    if (sourceID >= 1 && sourceID <= 5) {
        for (int i = 0; i < MAX_SERIAL_MONITOR_MAPPINGS; i++) {
            if (serialMonitorMappings[i].active && serialMonitorMappings[i].inputPort == sourceID) {
                hasMappingActive = true;
                mappingIndex = i;
                break;
            }
        }
    }
    
    if (hasMappingActive) {
        // Mapping is active - only send to mapped destinations, NO normal broadcast
        if (debugEnabled) {
            Serial.printf("Serial mapping active on Serial%d - sending only to mapped ports\n", sourceID);
        }
        
        // Send to all mapped outputs
        for (int j = 0; j < serialMonitorMappings[mappingIndex].outputCount; j++) {
            uint8_t destWCB = serialMonitorMappings[mappingIndex].outputs[j].wcbNumber;
            uint8_t destPort = serialMonitorMappings[mappingIndex].outputs[j].serialPort;
            
            if (destWCB == 0) {
                // Local destination
                if (destPort == 0) {
                    Serial.println(cmd);  // USB
                    if (debugEnabled) { Serial.printf("Sent to USB: %s\n", cmd.c_str()); }
                } else {
                    writeSerialString(getSerialStream(destPort), cmd);
                    if (debugEnabled) { Serial.printf("Sent to Serial%d: %s\n", destPort, cmd.c_str()); }
                }
            } else {
                // Remote destination via ESP-NOW
                String espnowCmd = ";S" + String(destPort) + cmd;
                sendESPNowMessage(destWCB, espnowCmd.c_str());
                if (debugEnabled) {
                    Serial.printf("Sent to WCB%d Serial%d via ESP-NOW: %s\n", 
                        destWCB, destPort, cmd.c_str());
                }
            }
        }
        
        return;  // Exit early - mapping replaces all broadcast behavior
    }

    // Check if broadcast is blocked on this port
    if (sourceID >= 1 && sourceID <= 5 && blockBroadcastFrom[sourceID - 1]) {
        Serial.printf("Broadcast blocked from Serial%d (input blocking enabled)\n", sourceID);
        return;
    }

    // Normal broadcast behavior - send to all serial ports except restricted ones
    for (int i = 1; i <= 5; i++) {
        if ((i == 1 && Kyber_Remote) || 
            (i <= 2 && Kyber_Local) || 
            isSerialPortPWMOutput(i) ||
            i == sourceID || !serialBroadcastEnabled[i - 1]) {
            continue;
        }
        
        // Skip if Maestro is on this port
        bool isMaestroPort = false;
        for (int m = 0; m < MAX_MAESTROS_PER_WCB; m++) {
            if (maestroConfigs[m].configured && 
                maestroConfigs[m].serialPort == i) {
                isMaestroPort = true;
                break;
            }
        }
        if (isMaestroPort) {
            if (debugEnabled) {
                Serial.printf("Skipping S%d (Maestro port)\n", i);
            }
            continue;
        }

        writeSerialString(getSerialStream(i), cmd);
        if (debugEnabled) { Serial.printf("Sent to Serial%d: %s\n", i, cmd.c_str()); }
    }

    // Always send via ESP-NOW broadcast (loop prevention is inside sendESPNowMessage)
    sendESPNowMessage(0, cmd.c_str());
    if (debugEnabled) { Serial.printf("Broadcasted via ESP-NOW: %s\n", cmd.c_str()); }
}

// processIncomingSerial for each serial port
void processIncomingSerial(Stream &serial, int sourceID) {
  if (!serial.available()) return;  // Exit if no data available

  static String serialBuffers[6];  // one for each serial port (0 = Serial, 1–5 = Serial1-5)
  String &serialBuffer = serialBuffers[sourceID];
  while (serial.available()) {
    char c = serial.read();
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
      serialBuffer += c;
    }
  }
}

// Helper function to process serial commands
void processSerialCommandHelper(String &data, int sourceID) {
    if (debugEnabled) {
      // Serial.printf("Processing input from %s: %s\n", getSerialLabel(sourceID).c_str(), data.c_str());
    }

    if (data.length() == 0) return;
    
    if (checkForTimerStopRequest(data)) {
        stopTimerSequence();
        return;
    }

    // Handle ?C commands with checksum validation BEFORE storing
    if (data.startsWith(String(LocalFunctionIdentifier) + "C") || 
        data.startsWith(String(LocalFunctionIdentifier) + "c")) {
        
        // Look for delimiter + ?CHK pattern (not just ?CHK)
        String chkPattern = String(commandDelimiter) + "?CHK";
        int chkPos = data.indexOf(chkPattern);
        if (chkPos == -1) {
            chkPattern = String(commandDelimiter) + "?chk";
            chkPos = data.indexOf(chkPattern);
        }
        
        if (chkPos != -1) {
            // Extract the command without checksum (everything before delimiter+?CHK)
            String cmdWithoutChecksum = data.substring(0, chkPos);
            // Extract checksum (skip delimiter + "?CHK")
            String providedChecksum = data.substring(chkPos + chkPattern.length());
            providedChecksum.toUpperCase();
            
            // Calculate checksum for the command WITHOUT the checksum itself
            uint32_t calculatedChecksum = calculateCRC32(cmdWithoutChecksum);
            char calculatedChecksumStr[9];  // 8 hex chars + null terminator
            sprintf(calculatedChecksumStr, "%08X", calculatedChecksum);  // Always 8 chars with leading zeros
            
            if (providedChecksum.equals(calculatedChecksumStr)) {
                Serial.println("✓ Command checksum VERIFIED");
                // Enqueue the command WITHOUT the checksum
                enqueueCommand(cmdWithoutChecksum, sourceID);
            } else {
                Serial.println("✗ Command checksum FAILED!");
                Serial.println("  Provided:   " + providedChecksum);
                Serial.println("  Calculated: " + String(calculatedChecksumStr));
            }
            return;
        } else {
            // No checksum - just process the command as-is
            enqueueCommand(data, sourceID);
            return;
        }
    }

    // Timer-aware parsing: if command contains ;T or ;t, use grouped mode
    if (data.indexOf(";T") != -1 || data.indexOf(";t") != -1) {
        parseCommandGroups(data);
        return;
    }

    // USE parseCommandsAndEnqueue for ALL other cases
    parseCommandsAndEnqueue(data, sourceID);
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
        
        // Process serial ports 3-5 (always safe)
        processIncomingSerial(Serial3, 3);
        processIncomingSerial(Serial4, 4);
        processIncomingSerial(Serial5, 5);

        // Handle Serial1 and Serial2 based on mode
        if (Kyber_Local) {
            // Skip Serial1 and Serial2 - handled by Kyber task
        } else if (Kyber_Remote) {
            // Process Serial2 only if not raw-mapped
            if (!isSerialPortRawMapped(2)) {
                processIncomingSerial(Serial2, 2);
            }
        } else {
            // Normal mode - process if not raw-mapped
            if (!isSerialPortRawMapped(1)) {
                processIncomingSerial(Serial1, 1);
            }
            if (!isSerialPortRawMapped(2)) {
                processIncomingSerial(Serial2, 2);
            }
        }

        vTaskDelay(pdMS_TO_TICKS(5));
    }
}

void PWMTask(void *pvParameters) {
    while (true) {
        processPWMPassthrough();
        vTaskDelay(pdMS_TO_TICKS(5)); // Process every 5ms for ~200Hz update rate
    }
}

void RawSerialForwardingTask(void *pvParameters) {
    static uint8_t buffer[64];
    
    while (true) {
        bool hadData = false;
        
        // Process each active raw serial mapping
        for (int i = 0; i < MAX_SERIAL_MONITOR_MAPPINGS; i++) {
            if (!serialMonitorMappings[i].active || !serialMonitorMappings[i].rawMode) {
                continue;
            }
            
            int inputPort = serialMonitorMappings[i].inputPort;
            Stream &inputSerial = getSerialStream(inputPort);
            
            // Read raw bytes if available - use read() not readBytes()
            if (inputSerial.available() > 0) {
                int len = 0;
                // Read all available bytes (up to buffer size)
                while (inputSerial.available() > 0 && len < sizeof(buffer)) {
                    buffer[len++] = inputSerial.read();
                }
                
                if (len > 0) {
                    hadData = true;
                    
                    // Forward to each output
                    for (int j = 0; j < serialMonitorMappings[i].outputCount; j++) {
                        auto &output = serialMonitorMappings[i].outputs[j];
                        
                        if (output.wcbNumber == 0 || output.wcbNumber == WCB_Number) {
                          // Local output (either explicitly local or targeting self)
                          Stream &outputSerial = getSerialStream(output.serialPort);
                          outputSerial.write(buffer, len);
                          outputSerial.flush();
                          
                          if (debugEnabled && output.wcbNumber == WCB_Number) {
                              Serial.printf("Mapping target W%dS%d is local - forwarding directly\n", 
                                          output.wcbNumber, output.serialPort);
                          }
                      } else {
                          // Remote output via ESP-NOW
                          sendESPNowRawSerial(buffer, len, output.wcbNumber, output.serialPort);
                      }
                    }
                }
            }
        }
        
        // Only delay if we didn't process any data
        if (!hadData) {
            vTaskDelay(pdMS_TO_TICKS(1));  // 1ms when idle
        }
        // No delay if we processed data - loop immediately
    }
}

void initStatusLEDWithRetry(int maxRetries, int delayBetweenMs) {  int attempt = 0;
  bool initialized = false;

  while (attempt < maxRetries && !initialized) {
    if (debugEnabled) Serial.printf("Attempting NeoPixel init (try %d)...\n", attempt + 1);

    // Feed watchdog to prevent timeout
    // esp_task_wdt_reset();
    
    // Prep the pin
    pinMode(STATUS_LED_PIN, OUTPUT);
    digitalWrite(STATUS_LED_PIN, LOW);
    delay(50);  // Reduced from 750ms
    
    // Try to init
    statusLED = new Adafruit_NeoPixel(STATUS_LED_COUNT, STATUS_LED_PIN, NEO_GRB + NEO_KHZ800);
    delay(50);  // Reduced from 750ms
    statusLED->begin();

    // Try setting a test color
    statusLED->setPixelColor(0, statusLED->Color(5, 0, 0));
    statusLED->show();
    delay(100);  // Reduced from 750ms

    // Check if the LED actually changed
    if (statusLED->getPixelColor(0) != 0) {
      initialized = true;
      if (debugEnabled) Serial.printf("✅ NeoPixel initialized on attempt %d\n", attempt + 1);
    } else {
      delete statusLED;
      statusLED = nullptr;
      delay(delayBetweenMs);
    }

    attempt++;
  }

  if (!initialized) {
    Serial.println("⚠️ Failed to initialize NeoPixel after retries - continuing without LED");
    statusLED = nullptr;  // Ensure it's explicitly null
  }
}
// ============================= Setup & Loop =============================

void setup() {

  Serial.begin(115200);
  delay(1000);  // allow USB to stabilize
  while (Serial.available()) Serial.read();  // 🔥 flush startup junk

  loadHWversion();
  loadMaestroSettings();  // Load Maestro configurations from NVS
  loadKyberSettings();
  loadKyberTargets();
  printResetReason();  // Show the exact cause of reset
  loadWCBNumberFromPreferences();
  loadWCBQuantitiesFromPreferences();
  loadMACPreferences();

  // Initialize the NeoPixel status LED
  if (wcb_hw_version == 0){
    Serial.println("No LED was setup.  Define HW version");
  } else if (wcb_hw_version == 1){
    pinMode(ONBOARD_LED, OUTPUT);
  } else if (wcb_hw_version == 21 || wcb_hw_version == 23 || wcb_hw_version == 24 || wcb_hw_version == 31 || wcb_hw_version == 32) {
    // Use safe initialization with retry for ALL NeoPixel versions
    Serial.println("Initializing NeoPixel LED...");
    initStatusLEDWithRetry(5, 100);  // Try 5 times with 100ms between attempts
    
    if (statusLED == nullptr) {
      Serial.println("⚠️ NeoPixel initialization failed - board will function without status LED");
    } else {
      Serial.println("✅ NeoPixel initialized successfully");
    }
  }
  Serial.printf("ETM struct size: %d\n", sizeof(espnow_struct_message_etm));
Serial.printf("Normal struct size: %d\n", sizeof(espnow_struct_message));
  delay(1000);        // I hate using delays but this was added to allow the RMT to stabilize before using LEDs
  turnOnLEDforBoot();
  // Create the command queue
  commandQueue = xQueueCreate(200, sizeof(CommandQueueItem));
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
  // Serial.println("PWM Mappings:");
  loadBaudRatesFromPreferences();
  loadSerialLabelsFromPreferences();  // 
   loadSerialMonitorSettings(); 
  loadBroadcastBlockSettings();  
  loadSerialMonitorMappings();
  loadBroadcastSettingsFromPreferences(); 
  initPWM();  // <-- This loads PWM mappings from preferences
  Serial.println("-------------------------------------------------------");
  printBaudRates();
  listSerialMonitorMappings();
  Serial.println("-------------------------------------------------------");

  if (Kyber_Local) {
      // Kyber Local REQUIRES both Serial1 (Maestro) and Serial2 (Kyber)
      Serial1.begin(baudRates[0], SERIAL_8N1, SERIAL1_RX_PIN, SERIAL1_TX_PIN);
      Serial2.begin(baudRates[1], SERIAL_8N1, SERIAL2_RX_PIN, SERIAL2_TX_PIN);
      Serial.println("Initialized Serial1 & Serial2 for Kyber Local mode");
  } else if (Kyber_Remote) {
      // Kyber Remote REQUIRES Serial1 (Maestro only)
      Serial1.begin(baudRates[0], SERIAL_8N1, SERIAL1_RX_PIN, SERIAL1_TX_PIN);
      Serial.println("Initialized Serial1 for Kyber Remote mode");
      // Serial2 available for PWM or normal serial
      if (!isSerialPortUsedForPWMInput(2) && !isSerialPortPWMOutput(2)) {
          Serial2.begin(baudRates[1], SERIAL_8N1, SERIAL2_RX_PIN, SERIAL2_TX_PIN);
      } else {
          Serial.println("Serial2 reserved for PWM - skipping UART init");
      }
  } else {
      // No Kyber - check PWM usage for both Serial1 and Serial2
      if (!isSerialPortUsedForPWMInput(1) && !isSerialPortPWMOutput(1)) {
          Serial1.begin(baudRates[0], SERIAL_8N1, SERIAL1_RX_PIN, SERIAL1_TX_PIN);
      } else {
          Serial.println("Serial1 reserved for PWM - skipping UART init");
      }
      if (!isSerialPortUsedForPWMInput(2) && !isSerialPortPWMOutput(2)) {
          Serial2.begin(baudRates[1], SERIAL_8N1, SERIAL2_RX_PIN, SERIAL2_TX_PIN);
      } else {
          Serial.println("Serial2 reserved for PWM - skipping UART init");
      }
  }

  // Serial3-5: Only initialize if NOT used for PWM AND baud rate is valid
  if (!isSerialPortUsedForPWMInput(3) && !isSerialPortPWMOutput(3)) {
      if (baudRates[2] > 0) {
          Serial3.begin(baudRates[2], SWSERIAL_8N1, SERIAL3_RX_PIN, SERIAL3_TX_PIN, false, 95);
      } else {
          Serial.println("Serial3 has invalid baud rate (0) - skipping UART init");
      }
  } else {
      Serial.println("Serial3 reserved for PWM - skipping UART init");
  }

  if (!isSerialPortUsedForPWMInput(4) && !isSerialPortPWMOutput(4)) {
      if (baudRates[3] > 0) {
          Serial4.begin(baudRates[3], SWSERIAL_8N1, SERIAL4_RX_PIN, SERIAL4_TX_PIN, false, 95);
      } else {
          Serial.println("Serial4 has invalid baud rate (0) - skipping UART init");
      }
  } else {
      Serial.println("Serial4 reserved for PWM - skipping UART init");
  }

  if (!isSerialPortUsedForPWMInput(5) && !isSerialPortPWMOutput(5)) {
      if (baudRates[4] > 0) {
          Serial5.begin(baudRates[4], SWSERIAL_8N1, SERIAL5_RX_PIN, SERIAL5_TX_PIN, false, 95);
      } else {
          Serial.println("Serial5 has invalid baud rate (0) - skipping UART init");
      }
  } else {
      Serial.println("Serial5 reserved for PWM - skipping UART init");
  }
  listPWMMappingsBoot();
  // ADD MONITORING/BLOCKING STATUS
bool hasMonitoring = false;
bool hasBlocking = false;

for (int i = 0; i < 5; i++) {
    if (serialMonitorEnabled[i]) hasMonitoring = true;
    if (blockBroadcastFrom[i]) hasBlocking = true;
}

if (hasMonitoring) {
    Serial.println("Serial Monitoring Active:");
    for (int i = 0; i < 5; i++) {
        if (serialMonitorEnabled[i]) {
            Serial.printf(" Serial%d monitoring enabled", i + 1);
            if (serialPortLabels[i].length() > 0) {
                Serial.printf(" (%s)", serialPortLabels[i].c_str());
            }
            Serial.println();
        }
    }
    Serial.printf(" Mirror to USB: %s\n", mirrorToUSB ? "YES" : "NO");
    if (Kyber_Local || Kyber_Remote) {
        Serial.printf(" Mirror to Kyber: %s\n", mirrorToKyber ? "YES" : "NO");
    }
}

if (hasBlocking) {
    Serial.println("Broadcast Blocking Active:");
    for (int i = 0; i < 5; i++) {
        if (blockBroadcastFrom[i]) {
            Serial.printf(" Serial%d broadcasts blocked", i + 1);
            if (serialPortLabels[i].length() > 0) {
                Serial.printf(" (%s)", serialPortLabels[i].c_str());
            }
            Serial.println();
        }
    }
}

if (hasMonitoring || hasBlocking) {
    Serial.println("-------------------------------------------------------");
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
  loadETMSettings();

  // Initialize ETM heartbeat timing
  if (etmEnabled) {
    memset(boardTable, 0, sizeof(boardTable));
    scheduleNextHeartbeat(true);  // randomized boot heartbeat
    etmInitPendingTable();
    Serial.printf("[ETM] Heartbeat scheduled (boot window)\n");
  }

  Serial.printf("Delimeter Character: %c\n", commandDelimiter);
  Serial.printf("Local Function Identifier: %c\n", LocalFunctionIdentifier);
  Serial.printf("Command Character: %c\n", CommandCharacter);
  Serial.println("-------------------------------------------------------");
  printKyberSettings();
  esp_now_register_recv_cb(espNowReceiveCallback);
  // Register send callback for delivery tracking
  esp_now_register_send_cb(espNowSendCallback);
  // Serial.println("ESP-NOW send callback registered");
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
    bool hasPWMMappings = false;
    for (int i = 0; i < MAX_PWM_MAPPINGS; i++) {
        if (pwmMappings[i].active) {
            hasPWMMappings = true;
            break;
        }
    }
    
    if (hasPWMMappings) {
        xTaskCreatePinnedToCore(PWMTask, "PWM Task", 2048, NULL, 2, NULL, 0);
        Serial.println("PWM Task Created");
    }

  // Create raw serial forwarding task
  xTaskCreatePinnedToCore(RawSerialForwardingTask, "Raw Serial Task", 4096, NULL, 1, NULL, 1);
  Serial.println("Raw Serial Forwarding Task Created");
  
  delay(150);     
  turnOffLED();
  // Serial.println("------------- Setup Complete-----------------");
}

void loop() {
  processCommandGroups();
  processETMHeartbeats();
  processETMAcksAndRetries();
  processETMChar();
  processETMLoad();
  // Handle queued commands
  if (!commandQueue) return;
  CommandQueueItem inItem;
  while (xQueueReceive(commandQueue, &inItem, 0) == pdTRUE) {
    String commandStr(inItem.cmd);
    free(inItem.cmd);
    handleSingleCommand(commandStr, inItem.sourceID);
  }
}
