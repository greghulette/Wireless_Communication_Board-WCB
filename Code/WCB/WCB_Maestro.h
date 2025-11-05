#ifndef WCB_MAESTRO_H
#define WCB_MAESTRO_H
#include "WCB_Storage.h"
#include <Arduino.h>

extern bool maestroEnabled;
extern volatile bool lastReceivedViaESPNOW;

void sendMaestroCommand(uint8_t maestroID, uint8_t scriptNumber);



#endif