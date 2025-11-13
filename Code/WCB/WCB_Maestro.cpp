#include "WCB_Maestro.h"

extern bool maestroEnabled;
extern int WCB_Number;
 extern bool lastReceivedViaESPNOW;
extern void sendESPNowMessage(uint8_t target, const char *message);


void sendMaestroCommand(uint8_t maestroID, uint8_t scriptNumber) {
  Serial.println("Sending Maestro Triggered");
  // 0xAA, <maestroID>, 0x27, <scriptNumber>
  if (maestroID == WCB_Number || maestroID == 9){
    uint8_t command[] = {0xAA, WCB_Number, 0x27, scriptNumber};
    Serial1.write(command, sizeof(command));
    Serial1.flush();
    if (debugEnabled){Serial.printf("Sent Maestro Command to ID %d, Script %d\n", maestroID, scriptNumber);}
  // return;
  } else if (maestroID == 0){
    uint8_t command[] = {0xAA, WCB_Number, 0x27, scriptNumber};
    Serial1.write(command, sizeof(command));
    Serial1.flush();
    if (debugEnabled){Serial.printf("Sent Maestro Command to ID %d, Script %d\n", maestroID, scriptNumber);}
    if (debugEnabled){Serial.println("Setup ESPNOW Broadcast");}
    String temp_espnowmessage = ";M" + String(9) + String(scriptNumber);
    Serial.println(temp_espnowmessage);
    sendESPNowMessage(0,temp_espnowmessage.c_str());
  // return; 
  } else if (maestroID == 1 ) {
    String temp_espnowmessage = ";M" + String(maestroID) + String(scriptNumber);
    Serial.println(temp_espnowmessage);
    if (debugEnabled){Serial.printf("Sent Maestro Command to ID %d, Script %d\n", maestroID, scriptNumber);}
    // lastReceivedViaESPNOW = false;
    sendESPNowMessage(1,temp_espnowmessage.c_str());
  } else if (maestroID == 2 ) {
    String temp_espnowmessage = ";M" + String(maestroID) + String(scriptNumber);
    Serial.println(temp_espnowmessage);
    if (debugEnabled){Serial.printf("Sent Maestro Command to ID %d, Script %d\n", maestroID, scriptNumber);}
    // lastReceivedViaESPNOW = false;
    sendESPNowMessage(2,temp_espnowmessage.c_str());
  } else if (maestroID == 3 ) {
    String temp_espnowmessage = ";M" + String(maestroID) + String(scriptNumber);
    Serial.println(temp_espnowmessage);
    if (debugEnabled){Serial.printf("Sent Maestro Command to ID %d, Script %d\n", maestroID, scriptNumber);}
    // lastReceivedViaESPNOW = false;
    sendESPNowMessage(3,temp_espnowmessage.c_str());
  } else if (maestroID == 4 ) {
    String temp_espnowmessage = ";M" + String(maestroID) + String(scriptNumber);
    Serial.println(temp_espnowmessage);
    if (debugEnabled){Serial.printf("Sent Maestro Command to ID %d, Script %d\n", maestroID, scriptNumber);}
    // lastReceivedViaESPNOW = false;
    sendESPNowMessage(4,temp_espnowmessage.c_str());
  } else if (maestroID == 5 ) {
    String temp_espnowmessage = ";M" + String(maestroID) + String(scriptNumber);
    Serial.println(temp_espnowmessage);
    if (debugEnabled){Serial.printf("Sent Maestro Command to ID %d, Script %d\n", maestroID, scriptNumber);}
    // lastReceivedViaESPNOW = false;
    sendESPNowMessage(5,temp_espnowmessage.c_str());
  } else if (maestroID == 6 ) {
    String temp_espnowmessage = ";M" + String(maestroID) + String(scriptNumber);
    Serial.println(temp_espnowmessage);
    if (debugEnabled){Serial.printf("Sent Maestro Command to ID %d, Script %d\n", maestroID, scriptNumber);}
    // lastReceivedViaESPNOW = false;
    sendESPNowMessage(6,temp_espnowmessage.c_str());
  } else if (maestroID == 7 ) {
    String temp_espnowmessage = ";M" + String(maestroID) + String(scriptNumber);
    Serial.println(temp_espnowmessage);
    if (debugEnabled){Serial.printf("Sent Maestro Command to ID %d, Script %d\n", maestroID, scriptNumber);}
    // lastReceivedViaESPNOW = false;
    sendESPNowMessage(7,temp_espnowmessage.c_str());
  } else if (maestroID == 8 ) {
    String temp_espnowmessage = ";M" + String(maestroID) + String(scriptNumber);
    Serial.println(temp_espnowmessage);
    if (debugEnabled){Serial.printf("Sent Maestro Command to ID %d, Script %d\n", maestroID, scriptNumber);}
    // lastReceivedViaESPNOW = false;
    sendESPNowMessage(8,temp_espnowmessage.c_str());
  }
}

