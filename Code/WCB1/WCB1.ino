/////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////
///*****                                                                                                        *****////
///*****                                          Created by Greg Hulette.                                      *****////
///*****                                                Version 2.2                                             *****////
///*****                                                                                                        *****////
///*****                                 So exactly what does this all do.....?                                 *****////
///*****                       - Receives commands via Serial or ESP-NOW                                        *****////
///*****                       - Sends Serial commands to other locally connected devices                       *****////
///*****                       - Sends Serial commands to other remotely connected devices                      *****////
///*****                       - Serial Commands sent ends with a Carriage Return "\r"                          *****//// 
///*****                                                                                                        *****////
///*****                                                                                                        *****//// 
///*****                       Wireless Command Syntax:                                                         *****////
///*****                       :W(x):S(y)(zzz...)                                                               *****////
///*****                       x: 1 Digit Identifier for the destination  (i.e. W1 - W9, BR)                    *****////
///*****                          x: 1 = WCB1                                                                   *****//// 
///*****                          x: 2 = WCB2                                                                   *****//// 
///*****                          x: 3 = WCB3                                                                   *****//// 
///*****                          x: 4 = WCB4                                                                   *****//// 
///*****                          x: 5 = WCB5                                                                   *****//// 
///*****                          x: 6 = WCB6                                                                   *****//// 
///*****                          x: 7 = WCB7                                                                   *****//// 
///*****                          x: 8 = WCB8                                                                   *****//// 
///*****                          x: 9 = WCB9                                                                   *****//// 
///*****                          x: B = Broadcast                                                              *****//// 
///*****                       y :Target's Serial Port (i.e. S1-S5)                                             *****////
///*****                          y: 1 = Serial 1 Port                                                          *****//// 
///*****                          y: 2 = Serial 2 Port                                                          *****//// 
///*****                          y: 3 = Serial 3 Port                                                          *****//// 
///*****                          y: 4 = Serial 4 Port                                                          *****//// 
///*****                          y: 5 = Serial 5 Port                                                          *****//// 
///*****                       zzz...: String to send out the destination serial port                           *****////
///*****                          zzz...: any string of characters up to 90 characters long                     *****//// 
///*****                                                                                                        *****////
///*****          Example1: :W2:S2:PP100  (Sends to WCB2's Serial 2 port and sends string ":PP100" + "\r")      *****////
///*****          Example2: :W3:S3:R01    (Sends to WCB3's Serial 3 port and sends string ":R01" + "\r")        *****////
///*****          Example3: :W5:S4MD904   (Sends to WCB5's Serial 4 port and sends string "MD904" + "\r")       *****////
///*****                                                                                                        *****////
///*****                                                                                                        *****////
///*****                       Local Serial Command Syntax:                                                     *****////                        
///*****                       :S(y)(zzz...)                                                                    *****////
///*****                       y :Target Serial Port (i.e. S1-S5)                                               *****////
///*****                          y: 1 = Serial 1 Port                                                          *****//// 
///*****                          y: 2 = Serial 2 Port                                                          *****//// 
///*****                          y: 3 = Serial 3 Port                                                          *****//// 
///*****                          y: 4 = Serial 4 Port                                                          *****//// 
///*****                          y: 5 = Serial 5 Port                                                          *****//// 
///*****                       zzz...: String to send out the destination serial port                           *****////
///*****                          zzz...: any string of characters up to 90 characters long                     *****//// 
///*****                                                                                                        *****////
///*****          Example1: :S2:PP100  (Sends to local Serial 2 port and sends string ":PP100" + "\r")          *****////
///*****          Example2: :S3:R01    (Sends to local Serial 3 port and sends string ":R01" + "\r")            *****////
///*****          Example3: :S4MD904   (Sends to local Serial 4 port and sends string "MD904" + "\r")           *****////
///*****                                                                                                        *****////
/////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////
/////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////

//////////////////////////////////////////////////////////////////////
///*****        Libraries used in this sketch                 *****///
//////////////////////////////////////////////////////////////////////

// Standard Arduino library
#include <Arduino.h>

//Used for ESP-NOW
#include <WiFi.h>
#include "esp_wifi.h"
#include <esp_now.h>

//pin definitions
#include "wcb_pin_map.h"

// Debuging Functions  - Using my own library for this.  This should be in the same folder as this sketch.
#include "DebugWCB.h" 

// Used for Software Serial to allow more serial port capacity
#include <SoftwareSerial.h>                 //EspSoftwareSerial by Dirk Kaar, Peter Lerup Library

// Used to store parameters after reboot/power loss
#include <Preferences.h>

//Used for the Status LED on Board version 2.1
#include <Adafruit_NeoPixel.h>                 // Adafruit NeoPixel by Adafruit Library

#include "Queue.h"

//////////////////////////////////////////////////////////////////////
///*****        Command Varaiables, Containers & Flags        *****///
//////////////////////////////////////////////////////////////////////
  
  char inputBuffer[300];
  String inputString;         // a string to hold incoming data
  
  volatile boolean stringComplete  = false;      // whether the serial string is complete
  String autoInputString;         // a string to hold incoming data
  volatile boolean autoComplete    = false;    // whether an Auto command is setA
  
  String umac_oct2_String ;      // Must match the unique Mac Address "umac_oct2" variable withouth the "0x"
  char umac_oct2_CharArray[2];      // Must match the unique Mac Address "umac_oct2" variable withouth the "0x"

  String umac_oct3_String ;      // Must match the unique Mac Address "umac_oct2" variable withouth the "0x"
  char umac_oct3_CharArray[2];      // Must match the unique Mac Address "umac_oct2" variable withouth the "0x"
  int commandLength;

  String serialStringCommand;
  String serialPort;
  String serialSubStringCommand;
  int serialicomingport = 0;
  String serialBroadcastCommand;
  String serialBroadcastSubCommand;  //not sure if I'm going to need this but creating in case for now
  bool ESPNOWBroadcastCommand;
  bool serialCommandisTrue;

  uint32_t Local_Command[6]  = {0,0,0,0,0,0};
  int localCommandFunction     = 0;

  String ESPNOWStringCommand;
  String ESPNOWTarget;
  String ESPNOWSubStringCommand;
  String ESPNOWPASSWORD;
  uint32_t SuccessCounter = 0;
  uint32_t FailureCounter = 0;
  int qcount;
  int lqcount = -1;
  bool haveCommands;
  String currentCommand;
  String previousCommand;
  unsigned long previousCommandMillis;

  String peekAt;
  debugClass Debug;
  String debugInputIdentifier ="";

  #ifdef WCB1
    String ESPNOW_SenderID = "W1";
    String HOSTNAME = "Wireless Communication Board 1 (W1)";
  #elif defined (WCB2)
    String ESPNOW_SenderID = "W2";
    String HOSTNAME = "Wireless Communication Board 2 (W2)";
  #elif defined (WCB3)
    String ESPNOW_SenderID = "W3";
    String HOSTNAME = "Wireless Communication Board 3 (W3)";
  #elif defined (WCB4)
    String ESPNOW_SenderID = "W4";
    String HOSTNAME = "Wireless Communication Board 4 (W4)";
  #elif defined (WCB5)
    String ESPNOW_SenderID = "W5";
    String HOSTNAME = "Wireless Communication Board 5 (W5)";
  #elif defined (WCB6)
    String ESPNOW_SenderID = "W6";
    String HOSTNAME = "Wireless Communication Board 6 (W6)";
  #elif defined (WCB7)
    String ESPNOW_SenderID = "W7";
    String HOSTNAME = "Wireless Communication Board 7 (W7)";
  #elif defined (WCB8)
    String ESPNOW_SenderID = "W8";
    String HOSTNAME = "Wireless Communication Board 8 (W8)";
  #elif defined (WCB9)
    String ESPNOW_SenderID = "W9";
    String HOSTNAME = "Wireless Communication Board 9 (W9)";
  #endif

  Preferences preferences;

Queue<String> queue = Queue<String>();

//////////////////////////////////////////////////////////////////////
///*****       Startup and Loop Variables                     *****///
//////////////////////////////////////////////////////////////////////
  
  boolean startUp = true;
  boolean isStartUp = true;
  
  //Main Loop Timers
  unsigned long mainLoopTime; 
  unsigned long MLMillis;
  byte mainLoopDelayVar = 5;
  String version = "V2.2";


#ifdef HWVERSION_1
bool BoardVer1 = true;
bool BoardVer2_1 = false;
bool Boardver2_2 = false;

#elif defined HWVERSION_2_1
bool BoardVer1 = false;
bool BoardVer2_1 = true;
bool Boardver2_2 = false;

#elif defined HWVERSION_2_2
bool BoardVer1 = false;
bool BoardVer2_1 = false;
bool Boardver2_2 = true;

#endif
 
//////////////////////////////////////////////////////////////////
///******       Serial Ports Definitions                  *****///
//////////////////////////////////////////////////////////////////


  #define s1Serial Serial1
  #define s2Serial Serial2
  SoftwareSerial s3Serial;
  SoftwareSerial s4Serial;
  SoftwareSerial s5Serial;
  

//////////////////////////////////////////////////////////////////////
///*****            Status LED Variables and settings       *****///
//////////////////////////////////////////////////////////////////////
  
// -------------------------------------------------
// Define some constants to help reference objects,
// pins, leds, colors etc by name instead of numbers
// -------------------------------------------------
//    CAMERA LENS LED VARIABLES
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

  #define STATUS_LED_COUNT 1

  Adafruit_NeoPixel ESP_LED = Adafruit_NeoPixel(STATUS_LED_COUNT, STATUS_LED_PIN, NEO_GRB + NEO_KHZ800);

/////////////////////////////////////////////////////////////////////////
///*****                  ESP NOW Set Up                         *****///
/////////////////////////////////////////////////////////////////////////
  
  //  ESP-NOW MAC Addresses used in the Droid. 
  const uint8_t WCB1MacAddress[] =  {0x02, umac_oct2, umac_oct3, 0x00, 0x00, 0x01};
  const uint8_t WCB2MacAddress[] =  {0x02, umac_oct2, umac_oct3, 0x00, 0x00, 0x02};
  const uint8_t WCB3MacAddress[] =  {0x02, umac_oct2, umac_oct3, 0x00, 0x00, 0x03};
  const uint8_t WCB4MacAddress[]=   {0x02, umac_oct2, umac_oct3, 0x00, 0x00, 0x04};
  const uint8_t WCB5MacAddress[] =  {0x02, umac_oct2, umac_oct3, 0x00, 0x00, 0x05};
  const uint8_t WCB6MacAddress[] =  {0x02, umac_oct2, umac_oct3, 0x00, 0x00, 0x06};
  const uint8_t WCB7MacAddress[] =  {0x02, umac_oct2, umac_oct3, 0x00, 0x00, 0x07};
  const uint8_t WCB8MacAddress[] =  {0x02, umac_oct2, umac_oct3, 0x00, 0x00, 0x08};
  const uint8_t WCB9MacAddress[] =  {0x02, umac_oct2, umac_oct3, 0x00, 0x00, 0x09};
  const uint8_t broadcastMACAddress[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

  // Uses these Strings for comparators
  String WCB1MacAddressString;
  String WCB2MacAddressString; 
  String WCB3MacAddressString; 
  String WCB4MacAddressString; 
  String WCB5MacAddressString; 
  String WCB6MacAddressString;
  String WCB7MacAddressString;
  String WCB8MacAddressString;
  String WCB9MacAddressString;
  String broadcastMACAddressString =  "FF:FF:FF:FF:FF:FF";

  // Define variables to store commands to be sent
  String  senderID;
  String  targetID;
  bool    commandIncluded;
  String  command;

  // Define variables to store incoming commands
  String  incomingPassword;
  String  incomingTargetID;  
  String  incomingSenderID;
  bool    incomingCommandIncluded;
  String  incomingCommand;
  
  // Variable to store if sending data was successful
  String success;

  //Structure example to send data
  //Must match the receiver structure
  typedef struct espnow_struct_message {
        char structPassword[40];
        char structSenderID[4];
        char structTargetID[4];
        bool structCommandIncluded;
        char structCommand[200];
    } espnow_struct_message;


  // Create a struct_message calledcommandsTosend to hold variables that will be sent
  espnow_struct_message commandsToSendtoWCB1;
  espnow_struct_message commandsToSendtoWCB2;
  espnow_struct_message commandsToSendtoWCB3;
  espnow_struct_message commandsToSendtoWCB4;
  espnow_struct_message commandsToSendtoWCB5;
  espnow_struct_message commandsToSendtoWCB6;
  espnow_struct_message commandsToSendtoWCB7;
  espnow_struct_message commandsToSendtoWCB8;
  espnow_struct_message commandsToSendtoWCB9;
  espnow_struct_message commandsToSendtoBroadcast;


  // Create a espnow_struct_message to hold variables that will be received
  espnow_struct_message commandsToReceiveFromWCB1;
  espnow_struct_message commandsToReceiveFromWCB2;
  espnow_struct_message commandsToReceiveFromWCB3;
  espnow_struct_message commandsToReceiveFromWCB4;
  espnow_struct_message commandsToReceiveFromWCB5;
  espnow_struct_message commandsToReceiveFromWCB6;
  espnow_struct_message commandsToReceiveFromWCB7;
  espnow_struct_message commandsToReceiveFromWCB8;
  espnow_struct_message commandsToReceiveFromWCB9;
  espnow_struct_message commandsToReceiveFromBroadcast;


  esp_now_peer_info_t peerInfo;

  // Callback when data is sent
void OnDataSent(const uint8_t *mac_addr, esp_now_send_status_t status) {
  if (status ==0){SuccessCounter ++;} else {FailureCounter ++;};
  if (Debug.debugflag_espnow == 1){
    Serial.print("\r\nLast Packet Send Status:\t");
    Serial.println(status == ESP_NOW_SEND_SUCCESS ? "Delivery Success" : "Delivery Fail");
    if (status ==0){
      success = "Delivery Success :)";
    }
    else{
      success = "Delivery Fail :(";
 
    }
  }
}

// Callback when data is received
void OnDataRecv(const uint8_t * mac, const uint8_t *incomingData, int len) {
  turnOnLEDESPNOW();
  char macStr[18];
  snprintf(macStr, sizeof(macStr), "%02X:%02X:%02X:%02X:%02X:%02X",
            mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
  String IncomingMacAddress(macStr);
  if (IncomingMacAddress == WCB1MacAddressString) {
    memcpy(&commandsToReceiveFromWCB1, incomingData, sizeof(commandsToReceiveFromWCB1));
    incomingPassword = commandsToReceiveFromWCB1.structPassword;
    if (incomingPassword != ESPNOWPASSWORD){
    Debug.ESPNOW("Wrong ESP-NOW Password was sent.  Message Ignored\n");  
    } else {
      incomingSenderID = commandsToReceiveFromWCB1.structSenderID;
      incomingTargetID = commandsToReceiveFromWCB1.structTargetID;
      incomingCommandIncluded = commandsToReceiveFromWCB1.structCommandIncluded;
      incomingCommand = commandsToReceiveFromWCB1.structCommand;
        Debug.ESPNOW("ESP-NOW Message Received from %s\n", incomingSenderID);
      processESPNOWIncomingMessage();
    } 
  } else if (IncomingMacAddress == WCB2MacAddressString){
    memcpy(&commandsToReceiveFromWCB2, incomingData, sizeof(commandsToReceiveFromWCB2));
    incomingPassword = commandsToReceiveFromWCB2.structPassword;
    if (incomingPassword != ESPNOWPASSWORD){
        Debug.ESPNOW("Wrong ESP-NOW Password of %s was sent.  Message Ignored\n", incomingPassword.c_str());  
      } else {
        incomingSenderID = commandsToReceiveFromWCB2.structSenderID;
        incomingTargetID = commandsToReceiveFromWCB2.structTargetID;
        incomingCommandIncluded = commandsToReceiveFromWCB2.structCommandIncluded;
        incomingCommand = commandsToReceiveFromWCB2.structCommand;
        Debug.ESPNOW("ESP-NOW Message Received from %s\n", incomingSenderID);
        processESPNOWIncomingMessage();
      }
  } else if (IncomingMacAddress == WCB3MacAddressString){
    memcpy(&commandsToReceiveFromWCB3, incomingData, sizeof(commandsToReceiveFromWCB3));
    incomingPassword = commandsToReceiveFromWCB3.structPassword;
    if (incomingPassword != ESPNOWPASSWORD){
        Debug.ESPNOW("Wrong ESP-NOW Password of %s was sent.  Message Ignored\n", incomingPassword.c_str());  
      } else {
        incomingSenderID = commandsToReceiveFromWCB3.structSenderID;
        incomingTargetID = commandsToReceiveFromWCB3.structTargetID;
        incomingCommandIncluded = commandsToReceiveFromWCB3.structCommandIncluded;
        incomingCommand = commandsToReceiveFromWCB3.structCommand;
        Debug.ESPNOW("ESP-NOW Message Received from %s\n", incomingSenderID);
        processESPNOWIncomingMessage();
      }
  } else if (IncomingMacAddress == WCB4MacAddressString){
    memcpy(&commandsToReceiveFromWCB4, incomingData, sizeof(commandsToReceiveFromWCB4));
    incomingPassword = commandsToReceiveFromWCB4.structPassword;
    if (incomingPassword != ESPNOWPASSWORD){
        Debug.ESPNOW("Wrong ESP-NOW Password of %s was sent.  Message Ignored\n", incomingPassword.c_str());  
      } else {
        incomingSenderID = commandsToReceiveFromWCB4.structSenderID;
        incomingTargetID = commandsToReceiveFromWCB4.structTargetID;
        incomingCommandIncluded = commandsToReceiveFromWCB4.structCommandIncluded;
        incomingCommand = commandsToReceiveFromWCB4.structCommand;
        Debug.ESPNOW("ESP-NOW Message Received from %s\n", incomingSenderID);
        processESPNOWIncomingMessage();
      }
  } else if (IncomingMacAddress == WCB5MacAddressString){
    memcpy(&commandsToReceiveFromWCB5, incomingData, sizeof(commandsToReceiveFromWCB5));
    incomingPassword = commandsToReceiveFromWCB4.structPassword;
    if (incomingPassword != ESPNOWPASSWORD){
        Debug.ESPNOW("Wrong ESP-NOW Password of %s was sent.  Message Ignored\n", incomingPassword.c_str());  
      } else {
        incomingSenderID = commandsToReceiveFromWCB5.structSenderID;
        incomingTargetID = commandsToReceiveFromWCB5.structTargetID;
        incomingCommandIncluded = commandsToReceiveFromWCB5.structCommandIncluded;
        incomingCommand = commandsToReceiveFromWCB5.structCommand;
        Debug.ESPNOW("ESP-NOW Message Received from %s\n", incomingSenderID);
        processESPNOWIncomingMessage();
      }
  } else if (IncomingMacAddress == WCB6MacAddressString){
    memcpy(&commandsToReceiveFromWCB5, incomingData, sizeof(commandsToReceiveFromWCB5));
    incomingPassword = commandsToReceiveFromWCB5.structPassword;
    if (incomingPassword != ESPNOWPASSWORD){
        Debug.ESPNOW("Wrong ESP-NOW Password of %s was sent.  Message Ignored\n", incomingPassword.c_str());  
      } else {
        incomingSenderID = commandsToReceiveFromWCB5.structSenderID;
        incomingTargetID = commandsToReceiveFromWCB5.structTargetID;
        incomingCommandIncluded = commandsToReceiveFromWCB5.structCommandIncluded;
        incomingCommand = commandsToReceiveFromWCB5.structCommand;
        Debug.ESPNOW("ESP-NOW Message Received from %s\n", incomingSenderID);
        processESPNOWIncomingMessage();
      }
  } else if (IncomingMacAddress == WCB6MacAddressString){
    memcpy(&commandsToReceiveFromWCB6, incomingData, sizeof(commandsToReceiveFromWCB6));
    incomingPassword = commandsToReceiveFromWCB6.structPassword;
    if (incomingPassword != ESPNOWPASSWORD){
        Debug.ESPNOW("Wrong ESP-NOW Password of %s was sent.  Message Ignored\n", incomingPassword.c_str());  
      } else {
        incomingSenderID = commandsToReceiveFromWCB6.structSenderID;
        incomingTargetID = commandsToReceiveFromWCB6.structTargetID;
        incomingCommandIncluded = commandsToReceiveFromWCB6.structCommandIncluded;
        incomingCommand = commandsToReceiveFromWCB6.structCommand;
        Debug.ESPNOW("ESP-NOW Message Received from %s\n", incomingSenderID);
        processESPNOWIncomingMessage();
      }
  } else if (IncomingMacAddress == WCB7MacAddressString){
    memcpy(&commandsToReceiveFromWCB7, incomingData, sizeof(commandsToReceiveFromWCB7));
    incomingPassword = commandsToReceiveFromWCB7.structPassword;
    if (incomingPassword != ESPNOWPASSWORD){
        Debug.ESPNOW("Wrong ESP-NOW Password of %s was sent.  Message Ignored\n", incomingPassword.c_str());  
      } else {
        incomingSenderID = commandsToReceiveFromWCB7.structSenderID;
        incomingTargetID = commandsToReceiveFromWCB7.structTargetID;
        incomingCommandIncluded = commandsToReceiveFromWCB7.structCommandIncluded;
        incomingCommand = commandsToReceiveFromWCB7.structCommand;
        Debug.ESPNOW("ESP-NOW Message Received from %s\n", incomingSenderID);
        processESPNOWIncomingMessage();
      }
  } else if (IncomingMacAddress == WCB8MacAddressString){
    memcpy(&commandsToReceiveFromWCB8, incomingData, sizeof(commandsToReceiveFromWCB8));
    incomingPassword = commandsToReceiveFromWCB8.structPassword;
    if (incomingPassword != ESPNOWPASSWORD){
        Debug.ESPNOW("Wrong ESP-NOW Password of %s was sent.  Message Ignored\n", incomingPassword.c_str());  
      } else {
        incomingSenderID = commandsToReceiveFromWCB8.structSenderID;
        incomingTargetID = commandsToReceiveFromWCB8.structTargetID;
        incomingCommandIncluded = commandsToReceiveFromWCB8.structCommandIncluded;
        incomingCommand = commandsToReceiveFromWCB8.structCommand;
        Debug.ESPNOW("ESP-NOW Message Received from %s\n", incomingSenderID);
        processESPNOWIncomingMessage();
      }
  } else if (IncomingMacAddress == WCB9MacAddressString){
    memcpy(&commandsToReceiveFromWCB9, incomingData, sizeof(commandsToReceiveFromWCB9));
    incomingPassword = commandsToReceiveFromWCB9.structPassword;
    if (incomingPassword != ESPNOWPASSWORD){
        Debug.ESPNOW("Wrong ESP-NOW Password of %s was sent.  Message Ignored\n", incomingPassword.c_str());  
      } else {
        incomingSenderID = commandsToReceiveFromWCB9.structSenderID;
        incomingTargetID = commandsToReceiveFromWCB9.structTargetID;
        incomingCommandIncluded = commandsToReceiveFromWCB9.structCommandIncluded;
        incomingCommand = commandsToReceiveFromWCB9.structCommand;
        Debug.ESPNOW("ESP-NOW Message Received from %s\n", incomingSenderID);
        processESPNOWIncomingMessage();
      }
  } else if (IncomingMacAddress == broadcastMACAddressString) {
  memcpy(&commandsToReceiveFromBroadcast, incomingData, sizeof(commandsToReceiveFromBroadcast));
  incomingPassword = commandsToReceiveFromBroadcast.structPassword;
  if (incomingPassword != ESPNOWPASSWORD){
    Debug.ESPNOW("Wrong ESP-NOW Password was sent.  Message Ignored\n");  
  } else {
      incomingSenderID = commandsToReceiveFromBroadcast.structSenderID;
      incomingTargetID = commandsToReceiveFromBroadcast.structTargetID;
      incomingCommandIncluded = commandsToReceiveFromBroadcast.structCommandIncluded;
      incomingCommand = commandsToReceiveFromBroadcast.structCommand;
        Debug.ESPNOW("ESP-NOW Message Received from %s\n", incomingSenderID);
      processESPNOWIncomingMessage();
    }
  } 
  else {Debug.ESPNOW("ESP-NOW Mesage ignored \n");}  
  IncomingMacAddress ="";  

    
    } 

void processESPNOWIncomingMessage(){
  Debug.ESPNOW("incoming target: %s\n", incomingTargetID.c_str());
  Debug.ESPNOW("incoming sender: %s\n", incomingSenderID.c_str());
  Debug.ESPNOW("incoming command included: %d\n", incomingCommandIncluded);
  Debug.ESPNOW("incoming command: %s\n", incomingCommand.c_str());
  if (incomingTargetID == ESPNOW_SenderID || incomingTargetID == "BR"){
    ESPNOWBroadcastCommand = true;
    queue.push(incomingCommand);
    // enqueueCommand(incomingCommand);

    Debug.ESPNOW("Recieved command from %s \n", incomingSenderID);
  }
  turnOffLED();
}

/////////////////////////////////////////////////////////////////////////////////////////////////////////
/////////////////////////////////////////////////////////////////////////////////////////////////////////
/////////                                                                                       /////////     
/////////                             Start OF FUNCTIONS                                        /////////
/////////                                                                                       /////////     
/////////////////////////////////////////////////////////////////////////////////////////////////////////
/////////////////////////////////////////////////////////////////////////////////////////////////////////


/////////////////////////////////////////////////////////////////////////////////////////////////////////
/////////////////////////////////////////////////////////////////////////////////////////////////////////
/////                              Communication Functions                                          /////
/////////////////////////////////////////////////////////////////////////////////////////////////////////
/////////////////////////////////////////////////////////////////////////////////////////////////////////

/////////////////////////////////////////////////////////
///*****          Serial Write Function          *****///
/////////////////////////////////////////////////////////

void writeSerialString(String stringData){
  String completeString = stringData + '\r';
  for (int i=0; i<completeString.length(); i++)
  {
    Serial.write(completeString[i]);
  }
  Debug.SERIAL_EVENT("Sent Command: %s out Serial port USB\n", completeString.c_str());
}

void writes1SerialString(String stringData){
  String completeString = stringData + '\r';
  for (int i=0; i<completeString.length(); i++)
  {
    s1Serial.write(completeString[i]);
  }
    Debug.SERIAL_EVENT("Sent Command: %s out Serial port 1\n", completeString.c_str());

}

void writes2SerialString(String stringData){
  String completeString = stringData + '\r';
  for (int i=0; i<completeString.length(); i++)
  {
    s2Serial.write(completeString[i]);
  }
    Debug.SERIAL_EVENT("Sent Command: %s out Serial port 2\n", completeString.c_str());

}

void writes3SerialString(String stringData){
  String completeString = stringData + '\r';
  for (int i=0; i<completeString.length(); i++)
  {
    s3Serial.write(completeString[i]);
  }
      Debug.SERIAL_EVENT("Sent Command: %s out Serial port 3\n", completeString.c_str());

}

void writes4SerialString(String stringData){
  String completeString = stringData + '\r';
  for (int i=0; i<completeString.length(); i++)
  {
    s4Serial.write(completeString[i]);
  }
    Debug.SERIAL_EVENT("Sent Command: %s out Serial port 4\n", completeString.c_str());

}

void writes5SerialString(String stringData){
  String completeString = stringData + '\r';
  for (int i=0; i<completeString.length(); i++)
  {
    s5Serial.write(completeString[i]);
  }
    Debug.SERIAL_EVENT("Sent Command: %s out Serial port 5\n", completeString.c_str());

}

/////////////////////////////////////////////////////////
///*****          Serial Event Function          *****///
/////////////////////////////////////////////////////////

void serialEvent() {
  while (Serial.available() > 0) {
    char inChar = (char)Serial.read();
    inputString += inChar;
      if (inChar == '\r') {               // if the incoming character is a carriage return (\r)
        Debug.SERIAL_EVENT("USB Serial Input: %s \n",inputString.c_str());
        serialicomingport = 0;
        serialCommandisTrue  = true;
        // inputString += DELIMITER;
        processSerial(inputString);
      }
  }
}

void s1SerialEvent() {
  while (s1Serial.available() > 0) {
    char inChar = (char)s1Serial.read();
    inputString += inChar;
    if (inChar == '\r') {               // if the incoming character is a carriage return (\r)
      Debug.SERIAL_EVENT("Serial 1 Input: %s \n",inputString.c_str());
      serialicomingport = 1;
      serialCommandisTrue  = true;
      processSerial(inputString);
    }
  }
}

void s2SerialEvent() {
  while (s2Serial.available() > 0) {
    char inChar = (char)s2Serial.read();
    inputString += inChar;
    if (inChar == '\r') {               // if the incoming character is a carriage return (\r)
      Debug.SERIAL_EVENT("Serial 2 Input: %s \n", inputString.c_str());
      serialicomingport = 2;
      serialCommandisTrue  = true;
      processSerial(inputString);
    }
  }
}

void s3SerialEvent() {
  while (s3Serial.available() > 0) {
    char inChar = (char)s3Serial.read();
    inputString += inChar;
    if (inChar == '\r') {               // if the incoming character is a carriage return (\r)
      Debug.SERIAL_EVENT("Serial 3 Input: %s \n", inputString.c_str());
      serialicomingport = 3;
      serialCommandisTrue  = true;
      processSerial(inputString);
    }
  }
}
void s4SerialEvent() {
  while (s4Serial.available() > 0) {
    char inChar = (char)s4Serial.read();
    inputString += inChar;
    if (inChar == '\r') {               // if the incoming character is a carriage return (\r)
      Debug.SERIAL_EVENT("Serial 4 Input: %s \n", inputString.c_str());
      serialicomingport = 4;
      serialCommandisTrue  = true;
      processSerial(inputString);
    }
  }
}
void s5SerialEvent() {
  while (s5Serial.available() > 0) {
    char inChar = (char)s5Serial.read();
    inputString += inChar;
    if (inChar == '\r') {               // if the incoming character is a carriage return (\r)
      Debug.SERIAL_EVENT("Serial 5 Input: %s \n", inputString.c_str());
      serialicomingport = 5;
      serialCommandisTrue  = true;
      // inputString += DELIMITER;
      processSerial(inputString);
    }
  }
}


long resetSerialNumberMillis;
uint16_t resetInterval = 150;


void resetserialnumber(){
if (millis() - resetSerialNumberMillis >= resetInterval)
serialicomingport = 0;
}

void resetSerialCommand(){
  if ( millis() - previousCommandMillis > resetInterval){
    previousCommand="nothingheretosee"; }  //resets the previous command after 50ms

}
void processSerial(String incomingSerialCommand){
  turnOnLEDSerial();
  incomingSerialCommand += DELIMITER;               // add the deliimiter to the end so that next part knows when to end the splicing of commands
  int saArrayLength = MAX_QUEUE_DEPTH;
  String sa[saArrayLength];  int r = 0;
  int  t =0;
  resetSerialNumberMillis = millis();

  for (int i=0; i < incomingSerialCommand.length(); i++){ 
    if(incomingSerialCommand.charAt(i) == DELIMITER){ 
      sa[t] = incomingSerialCommand.substring(r, i);
      // enqueueCommand(sa[t]);
      queue.push(sa[t]);
      Debug.SERIAL_EVENT("Serial Chain Command %i: %s \n", t+1 , sa[t].c_str());
      r=(i+1); 
      t++; 
    }
  }
  turnOffLED();
}

//////////////////////////////////////////////////////////////////////
///*****             ESP-NOW Functions                        *****///
//////////////////////////////////////////////////////////////////////

void setupSendStruct(espnow_struct_message* msg, String pass, String sender, String targetID, bool hascommand, String cmd)
{
    snprintf(msg->structPassword, sizeof(msg->structPassword), "%s", pass.c_str());
    snprintf(msg->structSenderID, sizeof(msg->structSenderID), "%s", sender.c_str());
    snprintf(msg->structTargetID, sizeof(msg->structTargetID), "%s", targetID.c_str());
    msg->structCommandIncluded = hascommand;
    snprintf(msg->structCommand, sizeof(msg->structCommand), "%s", cmd.c_str());
};

void sendESPNOWCommand(String starget, String scomm){
  String scommEval = "";
  bool hasCommand;
  if (scommEval == scomm){
    hasCommand = 0;
  } else {hasCommand = 1;};

  if (starget == "W1"){
    setupSendStruct(&commandsToSendtoWCB1, ESPNOWPASSWORD, ESPNOW_SenderID, starget, hasCommand, scomm);
    esp_err_t result = esp_now_send(WCB1MacAddress, (uint8_t *) &commandsToSendtoWCB1, sizeof(commandsToSendtoWCB1));
    if (result == ESP_OK) {Debug.ESPNOW("Sent the command: %s to ESP-NOW WCB1 Neighbor\n", scomm.c_str());
    }else {Debug.ESPNOW("Error sending the data to ESP-NOW WCB1 Neighbor \n");}
  }  else if (starget == "W2"){
    setupSendStruct(&commandsToSendtoWCB2, ESPNOWPASSWORD, ESPNOW_SenderID, starget, hasCommand, scomm);
    esp_err_t result = esp_now_send(WCB2MacAddress, (uint8_t *) &commandsToSendtoWCB2, sizeof(commandsToSendtoWCB2));
    if (result == ESP_OK) {Debug.ESPNOW("Sent the command: %s to ESP-NOW WCB2 Neighbor\n", scomm.c_str());
    }else {Debug.ESPNOW("Error sending the data to ESP-NOW WCB2 Neighbor\n");}
  } else if (starget == "W3"){
    setupSendStruct(&commandsToSendtoWCB3, ESPNOWPASSWORD, ESPNOW_SenderID, starget, hasCommand, scomm);
    esp_err_t result = esp_now_send(WCB3MacAddress, (uint8_t *) &commandsToSendtoWCB3, sizeof(commandsToSendtoWCB3));
    if (result == ESP_OK) {Debug.ESPNOW("Sent the command: %s to ESP-NOW WCB3 Neighbor\n", scomm.c_str());
    }else {Debug.ESPNOW("Error sending the data to ESP-NOW WCB3 Neighbor\n");}
  } else if (starget == "W4"){
    setupSendStruct(&commandsToSendtoWCB4, ESPNOWPASSWORD, ESPNOW_SenderID, starget, hasCommand, scomm);
    esp_err_t result = esp_now_send(WCB4MacAddress, (uint8_t *) &commandsToSendtoWCB4, sizeof(commandsToSendtoWCB4));
    if (result == ESP_OK) {Debug.ESPNOW("Sent the command: %s to ESP-NOW WCB4 Neighbor\n", scomm.c_str());
    }else {Debug.ESPNOW("Error sending the data to ESP-NOW WCB4 Neighbor\n");}
  } else if (starget == "W5"){
    setupSendStruct(&commandsToSendtoWCB5, ESPNOWPASSWORD, ESPNOW_SenderID, starget, hasCommand, scomm);
    esp_err_t result = esp_now_send(WCB5MacAddress, (uint8_t *) &commandsToSendtoWCB5, sizeof(commandsToSendtoWCB5));
    if (result == ESP_OK) {Debug.ESPNOW("Sent the command: %s to ESP-NOW WCB5 Neighbor\n", scomm.c_str());
    }else {Debug.ESPNOW("Error sending the data to ESP-NOW WCB5 Neighbor\n");}
  } else if (starget == "W6"){
    setupSendStruct(&commandsToSendtoWCB6, ESPNOWPASSWORD, ESPNOW_SenderID, starget, hasCommand, scomm);
    esp_err_t result = esp_now_send(WCB6MacAddress, (uint8_t *) &commandsToSendtoWCB6, sizeof(commandsToSendtoWCB6));
    if (result == ESP_OK) {Debug.ESPNOW("Sent the command: %s to ESP-NOW WCB6 Neighbor\n", scomm.c_str());
    }else {Debug.ESPNOW("Error sending the data to ESP-NOW WCB6 Neighbor\n");}
  } else if (starget == "W7"){
    setupSendStruct(&commandsToSendtoWCB7, ESPNOWPASSWORD, ESPNOW_SenderID, starget, hasCommand, scomm);
    esp_err_t result = esp_now_send(WCB7MacAddress, (uint8_t *) &commandsToSendtoWCB7, sizeof(commandsToSendtoWCB7));
    if (result == ESP_OK) {Debug.ESPNOW("Sent the command: %s to ESP-NOW WCB7 Neighbor\n", scomm.c_str());
    }else {Debug.ESPNOW("Error sending the datato ESP-NOW WCB7 Neighbor\n");}
  } else if (starget == "W8"){
    setupSendStruct(&commandsToSendtoWCB8, ESPNOWPASSWORD, ESPNOW_SenderID, starget, hasCommand, scomm);
    esp_err_t result = esp_now_send(WCB8MacAddress, (uint8_t *) &commandsToSendtoWCB8, sizeof(commandsToSendtoWCB8));
    if (result == ESP_OK) {Debug.ESPNOW("Sent the command: %s to ESP-NOW WCB8 Neighbor\n", scomm.c_str());
    }else {Debug.ESPNOW("Error sending the data to ESP-NOW WCB8 Neighbor\n");}
  } else if (starget == "W9"){
    setupSendStruct(&commandsToSendtoWCB9, ESPNOWPASSWORD, ESPNOW_SenderID, starget, hasCommand, scomm);
    esp_err_t result = esp_now_send(WCB9MacAddress, (uint8_t *) &commandsToSendtoWCB9, sizeof(commandsToSendtoWCB9));
    if (result == ESP_OK) {Debug.ESPNOW("Sent the command: %s to ESP-NOW WCB9 Neighbor\n", scomm.c_str());
    }else {Debug.ESPNOW("Error sending the data to ESP-NOW WCB9 Neighbor\n");}
  } else if (starget == "BR"){
    setupSendStruct(&commandsToSendtoBroadcast, ESPNOWPASSWORD, ESPNOW_SenderID, starget, hasCommand, scomm);
       esp_err_t result = esp_now_send(broadcastMACAddress, (uint8_t *) &commandsToSendtoBroadcast, sizeof(commandsToSendtoBroadcast));
    if (result == ESP_OK) {Debug.ESPNOW("Sent the command: %s to ESP-NOW Neighbors\n", scomm.c_str());
    }else {Debug.ESPNOW("Error sending the data\n");}
  } else {Debug.ESPNOW("No valid destination \n");}
};


/////////////////////////////////////////////////////////////////////////////////////////////////////////
/////////////////////////////////////////////////////////////////////////////////////////////////////////
/////                              Miscellaneous Functions                                          /////
/////////////////////////////////////////////////////////////////////////////////////////////////////////
/////////////////////////////////////////////////////////////////////////////////////////////////////////
// const char *str = '"ES",blue,20';
/////////////////////////////////////////////////////////
///*****          On-Board LED Function          *****///
/////////////////////////////////////////////////////////

void turnOnLEDESPNOW(){
if (BoardVer1){
  digitalWrite(ONBOARD_LED, HIGH); 
  } else if (BoardVer2_1 || Boardver2_2){
    colorWipeStatus("ES", green, 255);
  }
}  // Turns om the onboard Green LED

void turnOnLEDSerial(){
  if (BoardVer2_1 || Boardver2_2){
    colorWipeStatus("ES", red, 255);
  }
}  

void turnOnLEDSerialOut(){
  if (BoardVer2_1 ||  Boardver2_2){
    colorWipeStatus("ES", orange, 255);
  }
}  

void turnOffLED(){
  if (BoardVer1 ){
    digitalWrite(ONBOARD_LED, LOW);   // Turns off the onboard Green LED
  } else if (BoardVer2_1 ||  Boardver2_2){
    colorWipeStatus("ES", blue, 10);
  }
}


//////////////////////////////////////////////////////////////////////
///*****   ColorWipe Function for Status LED                  *****///
//////////////////////////////////////////////////////////////////////
void colorWipeStatus(String statusled, uint32_t c, int brightness) {
  if(statusled == "ES"){
    ESP_LED.setBrightness(brightness);
    ESP_LED.setPixelColor(0, c);
    ESP_LED.show();
  } 
  else{Debug.DBG("No LED was chosen \n");}
};

/////////////////////////////////////////////////////////
///*****          Baud Rate Functions.           *****///
/////////////////////////////////////////////////////////
 void saveBaud(const char* Port, int32_t Baud)
 {
  if (Baud == 110     ||     // checks to make sure a valid baudrate is used
      Baud == 300     ||
      Baud == 600     ||
      Baud == 1200    ||
      Baud == 2400    ||
      Baud == 9600    ||
      Baud == 14400   ||
      Baud == 19200   ||
      Baud == 38400   ||
      Baud == 57600   ||
      Baud == 115200  ||
      Baud == 128000  ||
      Baud == 256000
      ){
        preferences.begin("serial-baud", false);
        preferences.putInt(Port, Baud);
        preferences.end();
        Serial.printf("\n\nThe Serial Port Baud Rate has changed and the system will reboot in 3 seconds\n\n");
        delay(3000);
        ESP.restart();
      } else {Serial.printf("Wrong Baudrate given");}
}

 void clearBaud(){
  preferences.begin("serial-baud", false);
  preferences.clear();
  preferences.end();
  Serial.printf("\n\nThe Serial Port Baud Rates have been cleared and the system will reboot in 3 seconds\n\n");
  delay(3000);
  ESP.restart();
 }

void saveQuantity(int32_t quant ){
  preferences.begin("ESP-Quantity", false);
    preferences.putInt("WCBQuantity", quant);          
  preferences.end();  
    Serial.printf("\n\nThe WCB Quanity has changed to %i and the system will reboot in 3 seconds\n\n", quant);
  delay(3000);
  ESP.restart();
}

void clearQuantity(){
   preferences.begin("ESP-Quantity", false);
    preferences.clear();         
  preferences.end();
  Serial.printf("\n\nThe WCB Quantity has been cleard and the system will reboot in 3 seconds\n\n");
  delay(3000);
  ESP.restart();  
}

  void savePassword(String pass ){
  preferences.begin("Password", false);
   preferences.putString("DPASS", pass);          
  preferences.end(); 
    Serial.printf("\n\nThe ESP-NOW password has changed and the system will reboot in 3 seconds\n\n");
  delay(3000);
  ESP.restart();
  }

void clearPassword(){
   preferences.begin("Password", false);
    preferences.clear();         
  preferences.end();  
    Serial.printf("\n\nThe ESP-NOW password has been cleared and the system will reboot in 3 seconds\n\n");
  delay(3000);
  ESP.restart();
}

//////////////////////////////////////////////////////////////////////
///*****              Command Queueing Functions              *****///
///*****                                                      *****///
///*****    Function to store commands in a queue before      *****///
///*****    processing to ensure commands aren't lost while   *****///
///*****    processing other commands                         *****///
///*****                                                      *****///
///*****  A huge thanks to Mimir for this section of code!!   *****///
///*****                                                      *****///
//////////////////////////////////////////////////////////////////////

// template<class T, int maxitems>
// class Queue {
//   private:
//     int _front = 0, _back = 0, _count = 0;
//     T _data[maxitems + 1];
//     int _maxitems = maxitems;
//   public:
//     inline int count() { return _count; }
//     inline int front() { return _front; }
//     inline int back()  { return _back;  }

//     void push(const T &item) {
//       if(_count < _maxitems) { // Drops out when full
//         _data[_back++]=item;
//         ++_count;
//         // Check wrap around
//         if (_back > _maxitems)
//           _back -= (_maxitems + 1);
//       }
//     }

//     T peek() {
//       return (_count <= 0) ? T() : _data[_front];
//     }

//     T pop() {
//       if (_count <= 0)
//         return T(); // Returns empty

//       T result = _data[_front];
//       _front++;
//       --_count;
//       // Check wrap around
//       if (_front > _maxitems) 
//         _front -= (_maxitems + 1);
//       return result; 
//     }

//     void clear() {
//       _front = _back;
//       _count = 0;
//     }
// };

// template <int maxitems = MAX_QUEUE_DEPTH>
// using CommandQueue = Queue<String, maxitems>;

// ////////////////////////////////////////////////////


// CommandQueue<> commandQueue;

// bool havePendingCommands(){return (commandQueue.count() > 0);}

// String getNextCommand(){return commandQueue.pop();}

// void enqueueCommand(String command){commandQueue.push(command);}

// String peekAtCommand(){return commandQueue.peek();}


/////////////////////////////////////////////////////////////////////////////////////////////////////////
/////////////////////////////////////////////////////////////////////////////////////////////////////////
/////////                                                                                       /////////     
/////////                             END OF FUNCTIONS                                          /////////
/////////                                                                                       /////////     
/////////////////////////////////////////////////////////////////////////////////////////////////////////
/////////////////////////////////////////////////////////////////////////////////////////////////////////

void setup(){
  // initializes the 5 serial ports
  Serial.begin(115200);

  preferences.begin("serial-baud", false);
 unsigned long SERIAL1_BAUD_RATE = preferences.getInt("S1BAUD", SERIAL1_DEFAULT_BAUD_RATE);
 unsigned long SERIAL2_BAUD_RATE = preferences.getInt("S2BAUD", SERIAL2_DEFAULT_BAUD_RATE);
 unsigned long SERIAL3_BAUD_RATE = preferences.getInt("S3BAUD", SERIAL3_DEFAULT_BAUD_RATE);
 unsigned long SERIAL4_BAUD_RATE = preferences.getInt("S4BAUD", SERIAL4_DEFAULT_BAUD_RATE);
 unsigned long SERIAL5_BAUD_RATE = preferences.getInt("S5BAUD", SERIAL5_DEFAULT_BAUD_RATE);
  preferences.end();

  preferences.begin("ESP-Quantity", false);
  int32_t WCB_Quantity = preferences.getInt( "WCBQuantity", Default_WCB_Quantity);          
  preferences.end(); 

  preferences.begin("Password", false);
   ESPNOWPASSWORD = preferences.getString( "DPASS", DEFAULT_ESPNOWPASSWORD);          
  preferences.end(); 

  s1Serial.begin(SERIAL1_BAUD_RATE,SERIAL_8N1,SERIAL1_RX_PIN,SERIAL1_TX_PIN);
  s2Serial.begin(SERIAL2_BAUD_RATE,SERIAL_8N1,SERIAL2_RX_PIN,SERIAL2_TX_PIN);  
  s3Serial.begin(SERIAL3_BAUD_RATE,SWSERIAL_8N1,SERIAL3_RX_PIN,SERIAL3_TX_PIN,false,95);  
  s4Serial.begin(SERIAL4_BAUD_RATE,SWSERIAL_8N1,SERIAL4_RX_PIN,SERIAL4_TX_PIN,false,95);  
  s5Serial.begin(SERIAL5_BAUD_RATE,SWSERIAL_8N1,SERIAL5_RX_PIN,SERIAL5_TX_PIN,false,95);  

  // prints out a bootup message of the local hostname
  Serial.println("\n\n----------------------------------------");
  Serial.print("Booting up the ");Serial.println(HOSTNAME);
  Serial.print("FW Version: "); Serial.println(version);
  #ifdef HWVERSION_1
  Serial.println("HW Version 1.0");
  #elif defined HWVERSION_2
  Serial.println("HW Version 2.1");
  #endif
  Serial.println("----------------------------------------");
  Serial.printf("Serial 1 Baudrate: %i \nSerial 2 Baudrate: %i\nSerial 3 Baudrate: %i \nSerial 4 Baudrate: %i \nSerial 5 Baudrate: %i \n", SERIAL1_BAUD_RATE, SERIAL2_BAUD_RATE, SERIAL3_BAUD_RATE, SERIAL4_BAUD_RATE, SERIAL5_BAUD_RATE );
  
  // Takes the variables in the WCB_Preference.h file and converts them to strings, then assigns them to larger strings which the callback for ESP-NOW uses.  
  sprintf(umac_oct2_CharArray, "%02x", umac_oct2);
  umac_oct2_String = umac_oct2_CharArray;
  umac_oct2_String.toUpperCase();
  sprintf(umac_oct3_CharArray, "%02x", umac_oct3);
  umac_oct3_String = umac_oct3_CharArray;
  umac_oct3_String.toUpperCase();
  WCB1MacAddressString = "02:" + umac_oct2_String + ":" + umac_oct3_String + ":00:00:01";
  WCB2MacAddressString = "02:" + umac_oct2_String + ":" + umac_oct3_String + ":00:00:02";
  WCB3MacAddressString = "02:" + umac_oct2_String + ":" + umac_oct3_String + ":00:00:03";
  WCB4MacAddressString = "02:" + umac_oct2_String + ":" + umac_oct3_String + ":00:00:04";
  WCB5MacAddressString = "02:" + umac_oct2_String + ":" + umac_oct3_String + ":00:00:05";
  WCB6MacAddressString = "02:" + umac_oct2_String + ":" + umac_oct3_String + ":00:00:06";
  WCB7MacAddressString = "02:" + umac_oct2_String + ":" + umac_oct3_String + ":00:00:07";
  WCB8MacAddressString = "02:" + umac_oct2_String + ":" + umac_oct3_String + ":00:00:08";
  WCB9MacAddressString = "02:" + umac_oct2_String + ":" + umac_oct3_String + ":00:00:09";  

  Serial.printf("ESPNOW Password: %s \nQuantity of WCB's in system: %i \n2nd Octet: 0x%s \n3rd Octet: 0x%s\n", ESPNOWPASSWORD.c_str(), WCB_Quantity, umac_oct2_String, umac_oct3_String);
  

  //Reserve the memory for inputStrings
  inputString.reserve(300);                                                              // Reserve 100 bytes for the inputString:
  autoInputString.reserve(300);

  // Onboard LED setup
  pinMode(ONBOARD_LED, OUTPUT);

  // NeoPixel Setup
  ESP_LED.begin();
  ESP_LED.show();
  colorWipeStatus("ES",red,10);

    // Tasks_Init();


  // Tasks_Start();

  //initialize WiFi for ESP-NOW
  WiFi.mode(WIFI_STA);

  // Sets the local Mac Address for ESP-NOW 
  #ifdef WCB1
    esp_wifi_set_mac(WIFI_IF_STA, &WCB1MacAddress[0]);
  #elif defined (WCB2)
    esp_wifi_set_mac(WIFI_IF_STA, &WCB2MacAddress[0]);
  #elif defined (WCB3)
    esp_wifi_set_mac(WIFI_IF_STA, &WCB3MacAddress[0]);
  #elif defined (WCB4)
    esp_wifi_set_mac(WIFI_IF_STA, &WCB4MacAddress[0]);
  #elif defined (WCB5)
    esp_wifi_set_mac(WIFI_IF_STA, &WCB5MacAddress[0]);
  #elif defined (WCB6)
    esp_wifi_set_mac(WIFI_IF_STA, &WCB6MacAddress[0]);
  #elif defined (WCB7)
    esp_wifi_set_mac(WIFI_IF_STA, &WCB7MacAddress[0]);
  #elif defined (WCB8)
    esp_wifi_set_mac(WIFI_IF_STA, &WCB8MacAddress[0]);
  #elif defined (WCB9)
    esp_wifi_set_mac(WIFI_IF_STA, &WCB9MacAddress[0]);
  #endif

  //Prints out local Mac on bootup
  Serial.print("Local MAC address = ");
  Serial.println(WiFi.macAddress());

  
  //Initialize ESP-NOW
  if (esp_now_init() != ESP_OK) {
    Serial.println("Error initializing ESP-NOW");
  return;
  }
  
  // Once ESPNow is successfully Init, we will register for Send CB to
  // get the status of Trasnmitted packet
  esp_now_register_send_cb(OnDataSent);

  // Register peer configuration
  peerInfo.channel = 0;  
  peerInfo.encrypt = false;

  // Add ESP-NOW peers  
  
  // Broadcast
  memcpy(peerInfo.peer_addr, broadcastMACAddress, 6);
  if (esp_now_add_peer(&peerInfo) != ESP_OK){
    Serial.println("Failed to add Broadcast ESP-NOW peer");
    return;
  }
if (WCB_Quantity >= 1 ){
    // WCB1 ESP-NOW Peer
    #ifndef WCB1
      memcpy(peerInfo.peer_addr, WCB1MacAddress, 6);
      if (esp_now_add_peer(&peerInfo) != ESP_OK){
        Serial.println("Failed to add WCB1 ESP-NOW peer");
        return;
    }  
    #endif
}
if (WCB_Quantity >= 2 ){
  // WCB2 ESP-NOW Peer
  #ifndef WCB2
    memcpy(peerInfo.peer_addr, WCB2MacAddress, 6);
    if (esp_now_add_peer(&peerInfo) != ESP_OK){
      Serial.println("Failed to add WCB2 ESP-NOW peer");
      return;
    }  
  #endif
}
  
if (WCB_Quantity >= 3 ){
  // WCB3 ESP-NOW Peer
  #ifndef WCB3
    memcpy(peerInfo.peer_addr, WCB3MacAddress, 6);
    if (esp_now_add_peer(&peerInfo) != ESP_OK){
      Serial.println("Failed to add WCB3 ESP-NOW peer");
      return;
    }  
  #endif
}
if (WCB_Quantity >= 4 ){
  // WCB4 ESP-NOW Peer
  #ifndef WCB4
  memcpy(peerInfo.peer_addr, WCB4MacAddress, 6);
  if (esp_now_add_peer(&peerInfo) != ESP_OK){
    Serial.println("Failed to add WCB4 ESP-NOW peer");
    return;
  }  
  #endif
}

if (WCB_Quantity >= 5 ){
  // WCB5 ESP-NOW Peer
  #ifndef WCB5
  memcpy(peerInfo.peer_addr, WCB5MacAddress, 6);
  if (esp_now_add_peer(&peerInfo) != ESP_OK){
    Serial.println("Failed to add WCB5 ESP-NOW peer");
    return;
  }  
  #endif
}

if (WCB_Quantity >= 6 ){
  // WCB6 ESP-NOW Peer
  #ifndef WCB6
  memcpy(peerInfo.peer_addr, WCB6MacAddress, 6);
  if (esp_now_add_peer(&peerInfo) != ESP_OK){
    Serial.println("Failed to add WCB6 ESP-NOW peer");
    return;
  }  
  #endif
}

if (WCB_Quantity >= 7 ){

  // WCB7 ESP-NOW Peer
  #ifndef WCB7
  memcpy(peerInfo.peer_addr, WCB7MacAddress, 6);
  if (esp_now_add_peer(&peerInfo) != ESP_OK){
    Serial.println("Failed to add WCB7 ESP-NOW peer");
    return;
  }  
  #endif
}
if (WCB_Quantity >= 8 ){
    // WCB8 ESP-NOW Peer
    #ifndef WCB8
    memcpy(peerInfo.peer_addr, WCB8MacAddress, 6);
    if (esp_now_add_peer(&peerInfo) != ESP_OK){
      Serial.println("Failed to add WCB8 ESP-NOW peer");
      return;
    }  
    #endif
}
if (WCB_Quantity >= 9 ){
  // WCB9 ESP-NOW Peer
    #ifndef WCB9
    memcpy(peerInfo.peer_addr, WCB9MacAddress, 6);
    if (esp_now_add_peer(&peerInfo) != ESP_OK){
      Serial.println("Failed to add WCB9 ESP-NOW peer");
      return;
    } 
    #endif
}

  // Register for a callback function that will be called when data is received
  esp_now_register_recv_cb(OnDataRecv);
  


}   // end of setup


void loop(){
    if(Serial.available()){serialEvent();}
    if(s1Serial.available()){s1SerialEvent();}
    if(s2Serial.available()){s2SerialEvent();}
    if(s3Serial.available()){s3SerialEvent();}
    if(s4Serial.available()){s4SerialEvent();}
    if(s5Serial.available()){s5SerialEvent();}
    resetserialnumber();
    resetSerialCommand();
  if (millis() - MLMillis >= mainLoopDelayVar){
    MLMillis = millis();
    if(startUp) {
        colorWipeStatus("ES",blue,10);
      startUp = false;
      Serial.print("Startup complete\nStarting main loop\n\n\n");
    }

    // looks for new serial commands (Needed because ESP's do not have an onSerialEvent function)
   

    // if (havePendingCommands()) {autoComplete=false;}
    // if (havePendingCommands() || autoComplete) {
    // if(havePendingCommands()) { 
    if (queue.count()>0) {autoComplete=false;}
    if (queue.count()>0 || autoComplete) {
    if(queue.count()>0) {
      Debug.SERIAL_EVENT("New Queue String: %s\nQueue Count %i\n", queue.peek().c_str(),queue.count());
      haveCommands = queue.count()>0; 
      currentCommand = queue.peek();
      inputString = queue.pop(); 
      Debug.LOOP("Comamand Accepted into Loop: %s \n", inputString.c_str());
      inputString.toCharArray(inputBuffer, 300);inputString="";}

    else if (autoComplete) {autoInputString.toCharArray(inputBuffer, 300);autoInputString="";}
      if (inputBuffer[0] == '#'){
        if (
            inputBuffer[1]=='D' ||          // Command for debugging
            inputBuffer[1]=='d' ||          // Command for debugging
            inputBuffer[1]=='L' ||          // Command designator for internal functions
            inputBuffer[1]=='l' ||          // Command designator for internal functions
            inputBuffer[1]=='E' ||          // Command designator for storing ESPNOW Password
            inputBuffer[1]=='e' ||          // Command designator for storing ESPNOW Password
            inputBuffer[1]=='Q' ||          // Command designator for storing Quantity of WCBs
            inputBuffer[1]=='q' ||          // Command designator for storing Quantity of WCBs
            inputBuffer[1]=='S' ||          // Command designator for changing Serial Baud Rates
            inputBuffer[1]=='s'             // Command designator for changing Serial Baud Rates
          ){commandLength = strlen(inputBuffer); 
            if (inputBuffer[1]=='D' || inputBuffer[1]=='d'){
              debugInputIdentifier = "";                            // flush the string
              for (int i=2; i<=commandLength-2; i++){
                char inCharRead = inputBuffer[i];
                debugInputIdentifier += inCharRead;                   // add it to the inputString:
              }
              debugInputIdentifier.toUpperCase();
              Debug.toggle(debugInputIdentifier);
              debugInputIdentifier = "";                             // flush the string
              } else if (inputBuffer[1]=='L' || inputBuffer[1]=='l') {
                localCommandFunction = (inputBuffer[2]-'0')*10+(inputBuffer[3]-'0');
                Local_Command[0]   = '\0';                                                            // Flushes Array
                Local_Command[0] = localCommandFunction;
              Debug.LOOP("Entered the Local Command Structure /n");
              } else if (inputBuffer[1]=='S' || inputBuffer[1]=='s'){
                for (int i=1; i<commandLength;i++ ){
                char inCharRead = inputBuffer[i];
                serialStringCommand += inCharRead;  // add it to the inputString:
              }
              serialPort = serialStringCommand.substring(0,2);
              serialSubStringCommand = serialStringCommand.substring(2,commandLength);
              Debug.LOOP("Serial Baudrate: %s on Serial Port: %s\n", serialSubStringCommand.c_str(), serialPort); 
              int tempBaud = serialSubStringCommand.toInt();
              if (serialPort == "S1"|| serialPort == "s1"){
                  saveBaud("S1BAUD", tempBaud);
              } else if (serialPort == "S2" || serialPort == "s2"){
                  saveBaud("S2BAUD", tempBaud);
              } else if (serialPort == "S3" || serialPort == "s3"){
                  saveBaud("S3BAUD", tempBaud);
              }else if (serialPort == "S4" || serialPort == "s4"){
                  saveBaud("S4BAUD", tempBaud);
              } else if (serialPort == "S5" || serialPort == "s5"){
                  saveBaud("S5BAUD", tempBaud);
              }else if (serialPort == "SC" || serialPort == "sc"){
                  clearBaud();
              }else {Debug.LOOP("No valid serial port given \n");}
              
              serialStringCommand = "";
              serialPort = "";
              } else if (inputBuffer[1]=='Q' || inputBuffer[1]=='q'){
                int WCBQuantity = inputBuffer[2]-'0';
                if (WCBQuantity == 0){
                  clearQuantity();
                } else {saveQuantity(WCBQuantity);}
                
              }else if (inputBuffer[1]=='E' || inputBuffer[1]=='e'){
                String tempESPNOWPASSWORD;                            // flush the string
                for (int i=2; i<=commandLength-2; i++){
                  char inCharRead = inputBuffer[i];
                  tempESPNOWPASSWORD += inCharRead;                   // add it to the inputString:
              }
                if (tempESPNOWPASSWORD == "CLEAR"){
                  clearPassword();
                } else{savePassword(tempESPNOWPASSWORD); }
              }
              else {Debug.LOOP("No valid command entered /n");}
          }
              if(Local_Command[0]){
                switch (Local_Command[0]){
                  case 1: Serial.println(HOSTNAME);
                        Local_Command[0]   = '\0';                                                           break;
                  case 2: Serial.println("Resetting the ESP in 3 Seconds");
                        //  DelayCall::schedule([] {ESP.restart();}, 3000);
                        ESP.restart();
                        Local_Command[0]   = '\0';                                                           break;
                  case 3: break;  //reserved for future use
                  case 4: ; break;  //reserved for future use
                  case 5: printf("ESP-NOW Success Count: %i \nESP-NOW Failure Count %i \n", SuccessCounter, FailureCounter);
                        Local_Command[0]   = '\0';
                         break;  //prints out failure rate of ESPNOW
                  case 6: ; break;  //reserved for future use
                  case 7: ; break;  //reserved for future use
                  case 8: ; break;  //reserved for future use                                                         break;  //reserved for future use
                  case 9: ; break;  //reserved for future use

                }
              }

      }else if (inputBuffer[0] == CommandCharacter){
        if( inputBuffer[1]=='W' ||        // Command for Sending ESP-NOW Messages
            inputBuffer[1]=='w' ||        // Command for Sending ESP-NOW Messages
            inputBuffer[1]=='S' ||        // Command for sending Serial Strings out Serial ports
            inputBuffer[1]=='s'           // Command for sending Serial Strings out Serial ports
        ){commandLength = strlen(inputBuffer);                     //  Determines length of command character array.
          Debug.DBG("Command Length is: %i\n" , commandLength);
          if(commandLength >= 3) {
            if(inputBuffer[1]=='W' || inputBuffer[1]=='w') {
              for (int i=1; i<=commandLength; i++){
                char inCharRead = inputBuffer[i];
                ESPNOWStringCommand += inCharRead;                   // add it to the inputString:
              }
              Debug.LOOP("\nFull Command Recieved: %s \n",ESPNOWStringCommand.c_str());
              ESPNOWTarget = ESPNOWStringCommand.substring(0,2);
              ESPNOWTarget.toUpperCase();
              Debug.LOOP("ESP NOW Target: %s\n", ESPNOWTarget.c_str());
              ESPNOWSubStringCommand = ESPNOWStringCommand.substring(2,commandLength+1);
              Debug.LOOP("Command to Forward: %s\n", ESPNOWSubStringCommand.c_str());
              sendESPNOWCommand(ESPNOWTarget, ESPNOWSubStringCommand);
              // reset ESP-NOW Variables
              ESPNOWStringCommand = "";
              ESPNOWSubStringCommand = "";
              ESPNOWTarget = "";  
              }  
            if(inputBuffer[1]=='S' || inputBuffer[1]=='s') {
              for (int i=1; i<commandLength;i++ ){
                char inCharRead = inputBuffer[i];
                serialStringCommand += inCharRead;  // add it to the inputString:
              }
              serialPort = serialStringCommand.substring(0,2);

              serialSubStringCommand = serialStringCommand.substring(2,commandLength);
              Debug.LOOP("Serial Command: %s to Serial Port: %s\n", serialSubStringCommand.c_str(), serialPort);  
              if (serialPort == "S1" || serialPort == "s1"){
                writes1SerialString(serialSubStringCommand);
              } else if (serialPort == "S2" || serialPort == "s2"){
                writes2SerialString(serialSubStringCommand);
              } else if (serialPort == "S3" || serialPort == "s3"){
                writes3SerialString(serialSubStringCommand);
              }else if (serialPort == "S4" || serialPort == "s4"){
                writes4SerialString(serialSubStringCommand);
              } else if (serialPort == "S5" || serialPort == "s5"){
                writes5SerialString(serialSubStringCommand);
              }else {Debug.LOOP("No valid serial port given \n");}
              serialStringCommand = "";
              serialPort = "";
            } 
          }
        }
      } else if (haveCommands == true & currentCommand != previousCommand){
        previousCommand = currentCommand;
      previousCommandMillis = millis();

        // qcount  > 0  & qcount != lqcount
        // Debug.SERIAL_EVENT("qcount in if function: %i\nlqcount in if function %i\n", qcount,lqcount);
        // lqcount = qcount;
      // } else { 
    
        // Serial.println("Entered other stuctures");
      commandLength = strlen(inputBuffer);
      // Serial.println(commandLength);
      for (int i=0; i<commandLength;i++ ){
                char inCharRead = inputBuffer[i];
                serialBroadcastCommand += inCharRead;  // add it to the inputString:
              }
              // Serial.println(serialBroadcastCommand);
              Debug.DBG_2("Broadcast Command: %s", serialBroadcastCommand.c_str());
              if (serialicomingport != 1){writes1SerialString(serialBroadcastCommand);}
              if (serialicomingport != 2){writes2SerialString(serialBroadcastCommand);}
              if (serialicomingport != 3){writes3SerialString(serialBroadcastCommand);}
              if (serialicomingport != 4){writes4SerialString(serialBroadcastCommand);}
              if (serialicomingport != 5){writes5SerialString(serialBroadcastCommand);}
              if (ESPNOWBroadcastCommand == false){sendESPNOWCommand("BR", serialBroadcastCommand);}
              ESPNOWBroadcastCommand = false;

              serialCommandisTrue  = false; 
      }
      // }
      ///***  Clear States and Reset for next command.  ***///
      stringComplete =false;
      autoComplete = false;
      inputBuffer[0] = '\0';
      inputBuffer[1] = '\0'; 
      serialBroadcastCommand = "";
    
      // reset Local ESP Command Variables
      int espCommandFunction;

      Debug.LOOP("command Proccessed\n");
    
    }  
    if(isStartUp) {
      isStartUp = false;
      delay(500);
    }
  }
}