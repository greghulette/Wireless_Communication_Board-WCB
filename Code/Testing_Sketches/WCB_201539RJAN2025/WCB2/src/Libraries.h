

#ifndef Libraries_h
#define Libraries_h

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

// Used for queing commands so they can be chained without interupting the operations of the WCB
#include "Queue.h"

#include <PololuMaestro.h>

#include "PololuProtocol.h"

#include "../WCB_Preferences.h"

// #include "../StoredSerialCommands.h"

#include "PersistentStringHandler.h"


#endif