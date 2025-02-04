#include "WCB_Maestro.h"

extern bool maestroEnabled;
extern int WCB_Number;
extern void sendESPNowMessage(uint8_t target, const char *message);


void sendMaestroCommand(uint8_t maestroID, uint8_t scriptNumber) {
  // 0xAA, <maestroID>, 0x27, <scriptNumber>
  if (maestroID == WCB_Number || maestroID == 9){
    uint8_t command[] = {0xAA, WCB_Number, 0x27, scriptNumber};
    Serial1.write(command, sizeof(command));
    Serial1.flush();
    Serial.printf("Sent Maestro Command to ID %d, Script %d\n", maestroID, scriptNumber);
  return;
  } else if (maestroID == 0){
    uint8_t command[] = {0xAA, WCB_Number, 0x27, scriptNumber};
    Serial1.write(command, sizeof(command));
    Serial1.flush();
    Serial.printf("Sent Maestro Command to ID %d, Script %d\n", maestroID, scriptNumber);
    Serial.println("Setup ESPNOW Broadcast");
    String temp_espnowmessage = ";M" + String(9) + String(scriptNumber);
    Serial.println(temp_espnowmessage);
    sendESPNowMessage(0,temp_espnowmessage.c_str());
  return; 
  } else {
    String temp_espnowmessage = ";M" + String(maestroID) + String(scriptNumber);
    Serial.println(temp_espnowmessage);
    sendESPNowMessage(0,temp_espnowmessage.c_str());
  }
}

