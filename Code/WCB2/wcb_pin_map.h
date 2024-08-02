//////////////////////////////// 
// Wireless Communincation Board (WCB) Pinout
//////////////////////////////// 
#include "WCB_Preferences.h"

#ifndef wcb_pin_map.h
#define wcb_pin_map.h
#endif

#ifdef HWVERSION_1
//////////////////////////////// 
// This pinout is for a Version 1.0 board (LilyGo T7 V1.5 ESP32)
////////////////////////////////
#define SERIAL1_TX_PIN      5  //  // Serial 1 Tx Pin
#define SERIAL1_RX_PIN      32 //  // Serial 1 Rx Pin
#define SERIAL2_TX_PIN      26 //  // Serial 4 Tx Pin
#define SERIAL2_RX_PIN	    18 //  // Serial 2 Rx Pin
#define SERIAL3_TX_PIN      27 //  // Serial 4 Rx Pin
#define SERIAL3_RX_PIN      25 //  // Serial 5 Tx Pin
#define SERIAL4_TX_PIN	    4  //  // Serial 5 Rx Pin 
#define SERIAL4_RX_PIN      21 //  // Serial 3 Rx Pin
#define SERIAL5_TX_PIN	    22 //  // Serial 2 Tx Pin
#define SERIAL5_RX_PIN	    23 //  // Serial 3 Tx Pin
#define ONBOARD_LED         19 //  // ESP32 Status NeoPixel Pin
#define STATUS_LED_PIN      12 //  // Not used on this board but defining it to match version 2.1 board

 #elif defined HWVERSION_2_1
//////////////////////////////// 
// This pinout is for a Version 2.0 (Custom Board)
////////////////////////////////
#define SERIAL1_TX_PIN      8  //  // Serial 1 Tx Pin
#define SERIAL1_RX_PIN      5  //  // Serial 1 Rx Pin
#define SERIAL2_TX_PIN      20 //  // Serial 4 Tx Pin
#define SERIAL2_RX_PIN	    7  //  // Serial 2 Rx Pin
#define SERIAL3_TX_PIN      15 //  // Serial 4 Rx Pin
#define SERIAL3_RX_PIN      4  //  // Serial 5 Tx Pin
#define SERIAL4_TX_PIN	    14 //  // Serial 5 Rx Pin 
#define SERIAL4_RX_PIN      27 //  // Serial 3 Rx Pin
#define SERIAL5_TX_PIN	    13 //  // Serial 2 Tx Pin
#define SERIAL5_RX_PIN	    12 //  // Serial 3 Tx Pin
#define ONBOARD_LED         25 //  // Not Used in this board but there to match the Version 1.0 board's onboard Green LED
#define STATUS_LED_PIN      19 //  // ESP32 Status NeoPixel Pin

 #elif defined HWVERSION_2_3
//////////////////////////////// 
// This pinout is for a Version 2.0 (Custom Board)
////////////////////////////////
#define SERIAL1_TX_PIN      8  //  // Serial 1 Tx Pin
#define SERIAL1_RX_PIN      21  //  // Serial 1 Rx Pin
#define SERIAL2_TX_PIN      20 //  // Serial 4 Tx Pin
#define SERIAL2_RX_PIN	    7  //  // Serial 2 Rx Pin
#define SERIAL3_TX_PIN      25 //  // Serial 4 Rx Pin
#define SERIAL3_RX_PIN      4  //  // Serial 5 Tx Pin
#define SERIAL4_TX_PIN	    14 //  // Serial 5 Rx Pin 
#define SERIAL4_RX_PIN      27 //  // Serial 3 Rx Pin
#define SERIAL5_TX_PIN	    13 //  // Serial 2 Tx Pin
#define SERIAL5_RX_PIN	    26 //  // Serial 3 Tx Pin
#define ONBOARD_LED         22 //  // Not Used in this board but there to match the Version 1.0 board's onboard Green LED
#define STATUS_LED_PIN      19 //  // ESP32 Status NeoPixel Pin

#endif
