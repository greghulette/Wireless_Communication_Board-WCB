#include <esp_now.h>
#include <WiFi.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <SoftwareSerial.h>
#include "WCB_Preferences.h"
#include "src/Libraries.h"

// Queue for communication between tasks
QueueHandle_t espNowQueue;


#ifdef HWVERSION_1
bool BoardVer1 = true;
bool BoardVer2_1 = false;
bool Boardver2_3 = false;
bool Boardver2_4 = false;

#elif defined HWVERSION_2_1
bool BoardVer1 = false;
bool BoardVer2_1 = true;
bool Boardver2_3 = false;
bool Boardver2_4 = false;

#elif defined HWVERSION_2_3
bool BoardVer1 = false;
bool BoardVer2_1 = false;
bool Boardver2_3 = true;
bool Boardver2_4 = false;

#elif defined HWVERSION_2_4
bool BoardVer1 = false;
bool BoardVer2_1 = false;
bool Boardver2_3 = false;
bool Boardver2_4 = true;
#endif

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


 
  String umac_oct2_String ;      // Must match the unique Mac Address "umac_oct2" variable withouth the "0x"
  char umac_oct2_CharArray[2];      // Must match the unique Mac Address "umac_oct2" variable withouth the "0x"

  String umac_oct3_String ;      // Must match the unique Mac Address "umac_oct2" variable withouth the "0x"
  char umac_oct3_CharArray[2];      // Must match the unique Mac Address "umac_oct2" variable withouth the "0x"


// ESP-NOW broadcast address (example MAC address)
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
const uint8_t broadcastMACAddress[] =  {0xFF, umac_oct2, umac_oct3, 0xFF, 0xFF, 0xFF};
const uint8_t publicMACAddress[] =  {0xFF, 0xFD, 0xFF, 0xFF, 0xFF, 0xFF};

  String ESPNOWPASSWORD;

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
  String broadcastMACAddressString;

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

  

// SoftwareSerial ports for Serial3, Serial4, Serial5
SoftwareSerial Serial3; // RX, TX pins
SoftwareSerial Serial4; // RX, TX pins
SoftwareSerial Serial5; // RX, TX pins



  unsigned long SERIAL1_BROADCAST_ENABLE; 
  unsigned long SERIAL2_BROADCAST_ENABLE;
  unsigned long SERIAL3_BROADCAST_ENABLE;
  unsigned long SERIAL4_BROADCAST_ENABLE;
  unsigned long SERIAL5_BROADCAST_ENABLE;


Preferences preferences;
  debugClass Debug;
  String debugInputIdentifier ="";


void onDataReceive(const esp_now_recv_info *info, const uint8_t *data, int len) {
    // Convert sender's MAC address to a string
    char macStr[18];
    snprintf(macStr, sizeof(macStr), "%02X:%02X:%02X:%02X:%02X:%02X",
             info->src_addr[0], info->src_addr[1], info->src_addr[2],
             info->src_addr[3], info->src_addr[4], info->src_addr[5]);
    String incomingMacAddress = String(macStr); // Use Arduino String if std::string isn't supported

    // Print debug info
    Serial.printf("Received data from MAC: %s, length: %d\n", macStr, len);

    // Process incoming data
    if (incomingMacAddress == WCB1MacAddressString) {
        processIncomingData(data, sizeof(commandsToReceiveFromWCB1), incomingMacAddress);
    } else if (incomingMacAddress == WCB2MacAddressString) {
        processIncomingData(data, sizeof(commandsToReceiveFromWCB2), incomingMacAddress);
    } else if (incomingMacAddress == WCB3MacAddressString) {
        processIncomingData(data, sizeof(commandsToReceiveFromWCB3), incomingMacAddress);
    } else if (incomingMacAddress == WCB4MacAddressString) {
        processIncomingData(data, sizeof(commandsToReceiveFromWCB4), incomingMacAddress);
    } else if (incomingMacAddress == WCB5MacAddressString) {
        processIncomingData(data, sizeof(commandsToReceiveFromWCB5), incomingMacAddress);
    } else if (incomingMacAddress == WCB6MacAddressString) {
        processIncomingData(data, sizeof(commandsToReceiveFromWCB6), incomingMacAddress);
    } else if (incomingMacAddress == WCB7MacAddressString) {
        processIncomingData(data, sizeof(commandsToReceiveFromWCB7), incomingMacAddress);
    } else if (incomingMacAddress == WCB8MacAddressString) {
        processIncomingData(data, sizeof(commandsToReceiveFromWCB8), incomingMacAddress);
    } else if (incomingMacAddress == WCB9MacAddressString) {
        processIncomingData(data, sizeof(commandsToReceiveFromWCB9), incomingMacAddress);
    } else if (incomingMacAddress == broadcastMACAddressString) {
        processIncomingData(data, sizeof(commandsToReceiveFromBroadcast), incomingMacAddress);
    } else {
        Serial.println("Unknown MAC address, message ignored.");
        // turnOffLED();
    }
}
    // const char* namespaceName = "storage";

void processIncomingData(const uint8_t *incomingData, size_t dataSize, const String &macAddress) {
  if (macAddress == WCB1MacAddressString) {
    memcpy(&commandsToReceiveFromWCB1, incomingData, dataSize);
    incomingPassword = commandsToReceiveFromWCB1.structPassword;
  } else if (macAddress == WCB2MacAddressString) {
    memcpy(&commandsToReceiveFromWCB2, incomingData, dataSize);
    incomingPassword = commandsToReceiveFromWCB2.structPassword;
  } else if (macAddress == WCB3MacAddressString) {
    memcpy(&commandsToReceiveFromWCB3, incomingData, dataSize);
    incomingPassword = commandsToReceiveFromWCB3.structPassword;
  } else if (macAddress == WCB4MacAddressString) {
    memcpy(&commandsToReceiveFromWCB4, incomingData, dataSize);
    incomingPassword = commandsToReceiveFromWCB4.structPassword;
  } else if (macAddress == WCB5MacAddressString) {
    memcpy(&commandsToReceiveFromWCB5, incomingData, dataSize);
    incomingPassword = commandsToReceiveFromWCB5.structPassword;
  } else if (macAddress == WCB6MacAddressString) {
    memcpy(&commandsToReceiveFromWCB6, incomingData, dataSize);
    incomingPassword = commandsToReceiveFromWCB6.structPassword;
  } else if (macAddress == WCB7MacAddressString) {
    memcpy(&commandsToReceiveFromWCB7, incomingData, dataSize);
    incomingPassword = commandsToReceiveFromWCB7.structPassword;
  } else if (macAddress == WCB8MacAddressString) {
    memcpy(&commandsToReceiveFromWCB8, incomingData, dataSize);
    incomingPassword = commandsToReceiveFromWCB8.structPassword;
  } else if (macAddress == WCB9MacAddressString) {
    memcpy(&commandsToReceiveFromWCB9, incomingData, dataSize);
    incomingPassword = commandsToReceiveFromWCB9.structPassword;
  } else if (macAddress == broadcastMACAddressString) {
    memcpy(&commandsToReceiveFromBroadcast, incomingData, dataSize);
    incomingPassword = commandsToReceiveFromBroadcast.structPassword;
  } else {
    // turnOffLED();
    return; // Unknown MAC address, stop processing
  }

  // Validate the ESP-NOW password
  if (incomingPassword != ESPNOWPASSWORD) {
    Debug.ESPNOW("Wrong ESP-NOW Password from %s. Message Ignored\n", macAddress.c_str());
    return;
  }

  // Process valid data
  incomingSenderID = commandsToReceiveFromWCB1.structSenderID;
  incomingTargetID = commandsToReceiveFromWCB1.structTargetID;
  incomingCommandIncluded = commandsToReceiveFromWCB1.structCommandIncluded;
  incomingCommand = commandsToReceiveFromWCB1.structCommand;

  Debug.ESPNOW("ESP-NOW Message Received from %s\n", incomingSenderID.c_str());
  processESPNOWIncomingMessage();
}

//////////////////////////////////////////////////////////////////////
///*****             ESP-NOW Send Functions                   *****///
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

void OnDataSent(const uint8_t *mac_addr, esp_now_send_status_t status) {
  // if (status ==0){SuccessCounter ++;} else {FailureCounter ++;};
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

void processESPNOWIncomingMessage(){
  Debug.ESPNOW("incoming target: %s\n", incomingTargetID.c_str());
  Debug.ESPNOW("incoming sender: %s\n", incomingSenderID.c_str());
  Debug.ESPNOW("incoming command included: %d\n", incomingCommandIncluded);
  Debug.ESPNOW("incoming command: %s\n", incomingCommand.c_str());
  if (incomingTargetID == ESPNOW_SenderID){
      // ESPNOWBroadcastCommand = false;
      // queue.push(incomingCommand);
      Debug.ESPNOW("Recieved command from %s \n", incomingSenderID);
    }
    if (incomingTargetID == "BR"){
      // ESPNOWBroadcastCommand = true;
      // queue.push(incomingCommand);
      Debug.ESPNOW("Recieved command from %s \n", incomingSenderID);
    }
  // turnOffLED();
}

// Function prototypes
void serialTask(void *parameter);
void espNowTask(void *parameter);
void sendEspNowMessage(String message);
void processCommand(String command, String source);

void setup() {
    // Initialize Serial ports
    Serial.begin(115200);
    preferences.begin("serial-baud", false);
    unsigned long SERIAL1_BAUD_RATE = preferences.getInt("S1BAUD", SERIAL1_DEFAULT_BAUD_RATE);
    unsigned long SERIAL2_BAUD_RATE = preferences.getInt("S2BAUD", SERIAL2_DEFAULT_BAUD_RATE);
    unsigned long SERIAL3_BAUD_RATE = preferences.getInt("S3BAUD", SERIAL3_DEFAULT_BAUD_RATE);
    unsigned long SERIAL4_BAUD_RATE = preferences.getInt("S4BAUD", SERIAL4_DEFAULT_BAUD_RATE);
    unsigned long SERIAL5_BAUD_RATE = preferences.getInt("S5BAUD", SERIAL5_DEFAULT_BAUD_RATE);
    SERIAL1_BROADCAST_ENABLE = preferences.getInt("S1BDCST", SERIAL1_BROADCAST_DEFAULT);
    SERIAL2_BROADCAST_ENABLE = preferences.getInt("S2BDCST", SERIAL2_BROADCAST_DEFAULT);
    SERIAL3_BROADCAST_ENABLE = preferences.getInt("S3BDCST", SERIAL3_BROADCAST_DEFAULT);
    SERIAL4_BROADCAST_ENABLE = preferences.getInt("S4BDCST", SERIAL4_BROADCAST_DEFAULT);
    SERIAL5_BROADCAST_ENABLE = preferences.getInt("S5BDCST", SERIAL5_BROADCAST_DEFAULT);
  preferences.end();

  preferences.begin("ESP-Quantity", false);
    int32_t WCB_Quantity = preferences.getInt( "WCBQuantity", Default_WCB_Quantity);          
  preferences.end();

    preferences.begin("Password", false);
    ESPNOWPASSWORD = preferences.getString( "DPASS", DEFAULT_ESPNOWPASSWORD);          
  preferences.end(); 
  Serial1.begin(SERIAL1_BAUD_RATE,SERIAL_8N1,SERIAL1_RX_PIN,SERIAL1_TX_PIN);
  Serial2.begin(SERIAL2_BAUD_RATE,SERIAL_8N1,SERIAL2_RX_PIN,SERIAL2_TX_PIN);  
  Serial3.begin(SERIAL3_BAUD_RATE,SWSERIAL_8N1,SERIAL3_RX_PIN,SERIAL3_TX_PIN,false,95);  
  Serial4.begin(SERIAL4_BAUD_RATE,SWSERIAL_8N1,SERIAL4_RX_PIN,SERIAL4_TX_PIN,false,95);  
  Serial5.begin(SERIAL5_BAUD_RATE,SWSERIAL_8N1,SERIAL5_RX_PIN,SERIAL5_TX_PIN,false,95); 
  // chainSerial.begin(CHAIN_DEFAULT_BAUD_RATE,SWSERIAL_8N1,CHAIN_RX_PIN,CHAIN_TX_PIN,false,

    // Serial1.begin(57600, SERIAL_8N1, 21, 8); // Example RX and TX pins for Serial1
    // Serial3.begin(57600); // Initialize SoftwareSerial Serial3
    // Serial4.begin(57600); // Initialize SoftwareSerial Serial4
    // Serial5.begin(57600); // Initialize SoftwareSerial Serial5

    // Initialize Wi-Fi in station mode (required for ESP-NOW)
    // WiFi.mode(WIFI_STA);
    // if (esp_now_init() != ESP_OK) {
    //     Serial.println("ESP-NOW Initialization Failed!");
    //     return;
    // }
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
  // esp_now_register_recv_cb(OnDataRecv);
  esp_now_register_recv_cb(onDataReceive);
  
    // Create a queue to hold messages
    espNowQueue = xQueueCreate(10, sizeof(String));
    if (espNowQueue == NULL) {
        Serial.println("Failed to create queue!");
        return;
    }

    // Create tasks
    xTaskCreatePinnedToCore(serialTask, "Serial Task", 8192, NULL, 1, NULL, 0);
    xTaskCreatePinnedToCore(espNowTask, "ESP-NOW Task", 4096, NULL, 1, NULL, 1);
}

// Task to listen to Serial1, Serial3, Serial4, Serial5 and enqueue messages
void serialTask(void *parameter) {
    while (true) {
        // Check Serial1
        if (Serial1.available()) {
            String message = Serial1.readStringUntil('\n'); // Read until newline
            if (!message.isEmpty()) {
                processCommand(message, "Serial1");
            }
        }

        // Check Serial3
        if (Serial3.available()) {
            String message = Serial3.readStringUntil('\n'); // Read until newline
            if (!message.isEmpty()) {
                processCommand(message, "Serial3");
            }
        }

        // Check Serial4
        if (Serial4.available()) {
            String message = Serial4.readStringUntil('\n'); // Read until newline
            if (!message.isEmpty()) {
                processCommand(message, "Serial4");
            }
        }

        // Check Serial5
        if (Serial5.available()) {
            String message = Serial5.readStringUntil('\n'); // Read until newline
            if (!message.isEmpty()) {
                processCommand(message, "Serial5");
            }
        }

        vTaskDelay(10 / portTICK_PERIOD_MS); // Small delay to yield CPU time
    }
}

// Task to send messages via ESP-NOW
void espNowTask(void *parameter) {
    while (true) {
        String message;
        if (xQueueReceive(espNowQueue, &message, portMAX_DELAY)) {
            Serial.println("Sending ESP-NOW message: " + message);
            sendEspNowMessage(message);
        }
        vTaskDelay(10 / portTICK_PERIOD_MS); // Yield CPU time
    }
}

// Function to send a message via ESP-NOW
void sendEspNowMessage(String message) {
    esp_err_t result = esp_now_send(broadcastMACAddress, (uint8_t *)message.c_str(), message.length());

    if (result == ESP_OK) {
        Serial.println("ESP-NOW message sent successfully");
    } else {
        Serial.println("Error sending ESP-NOW message");
    }
}

// Function to process commands received from serial ports
void processCommand(String command, String source) {
    command.trim(); // Remove whitespace
    Serial.println("Processing command from " + source + ": " + command);

    if (command.startsWith("RESET")) {
        Serial.println("Resetting device...");
        ESP.restart();
    } else if (command.startsWith("STATUS")) {
        Serial.println("Device status: All systems operational.");
    } else if (command.startsWith("SEND:")) {
        // Extract message to send via ESP-NOW
        String message = command.substring(5);
        if (!message.isEmpty()) {
            xQueueSend(espNowQueue, &message, portMAX_DELAY);
        }
    } else {
        Serial.println("Unknown command: " + command);
    }
}

// Remove the existing loop() function, as tasks handle the program flow
void loop() {
    // Nothing to do here
}
