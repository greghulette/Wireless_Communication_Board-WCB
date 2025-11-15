//////////////////////////////// 
// Wireless Communincation Board (WCB) Pinout
//////////////////////////////// 
#include <Arduino.h>
#include "wcb_pin_map.h"



extern void updatePinMap(){
 if (wcb_hw_version == 0){
    //////////////////////////////// 
    // This pinout is for a Version 1.0 board (LilyGo T7 V1.5 ESP32)
    ////////////////////////////////
    SERIAL1_TX_PIN = 5;   //  // Serial 1 Tx Pin
    SERIAL1_RX_PIN = 5;   //  // Serial 1 Rx Pin
    SERIAL2_TX_PIN = 5;   //  // Serial 2 Tx Pin
    SERIAL2_RX_PIN = 5;   //  // Serial 2 Rx Pin
    SERIAL3_TX_PIN = 5;   //  // Serial 3 Tx Pin
    SERIAL3_RX_PIN = 5;   //  // Serial 3 Rx Pin
    SERIAL4_TX_PIN = 5;   //  // Serial 4 Tx Pin 
    SERIAL4_RX_PIN = 5;   //  // Serial 4 Rx Pin
    SERIAL5_TX_PIN = 5;   //  // Serial 5 Tx Pin
    SERIAL5_RX_PIN = 5;   //  // Serial 5 Rx Pin
    ONBOARD_LED    = 19;     //  // ESP32 Status NeoPixel Pin
    STATUS_LED_PIN = 19;  //  // Not used on this board but defining it to match   
    // Serial.println("Loaded Default Pinout");
 } else if (wcb_hw_version == 1){
    //////////////////////////////// 
    // This pinout is for a Version 1.0 board (LilyGo T7 V1.5 ESP32)
    ////////////////////////////////
     SERIAL1_TX_PIN = 5;  //  // Serial 1 Tx Pin
     SERIAL1_RX_PIN = 32; //  // Serial 1 Rx Pin
     SERIAL2_TX_PIN = 26; //  // Serial 2 Tx Pin
     SERIAL2_RX_PIN = 18; //  // Serial 2 Rx Pin
     SERIAL3_TX_PIN = 27; //  // Serial 3 Tx Pin
     SERIAL3_RX_PIN = 25; //  // Serial 3 Rx Pin
     SERIAL4_TX_PIN = 4;  //  // Serial 4 Tx Pin 
     SERIAL4_RX_PIN = 21; //  // Serial 4 Rx Pin
     SERIAL5_TX_PIN = 22; //  // Serial 5 Tx Pin
     SERIAL5_RX_PIN = 23; //  // Serial 5 Rx Pin
     ONBOARD_LED    = 19; //  // ESP32 Status NeoPixel Pin
     STATUS_LED_PIN = 12; //  // Not used on this board but defining it to match version 2.1 board
    
    // Serial.println("Loaded HW Version 1.0 Pinout");

 } else if (wcb_hw_version == 21){
    //////////////////////////////// 
    // This pinout is for a Version 2.1 (Custom Board)
    ////////////////////////////////
     SERIAL1_TX_PIN = 8;  //  // Serial 1 Tx Pin
     SERIAL1_RX_PIN = 5;  //  // Serial 1 Rx Pin
     SERIAL2_TX_PIN = 20; //  // Serial 2 Tx Pin
     SERIAL2_RX_PIN = 7;  //  // Serial 2 Rx Pin
     SERIAL3_TX_PIN = 15; //  // Serial 3 Tx Pin
     SERIAL3_RX_PIN = 4;  //  // Serial 3 Rx Pin
     SERIAL4_TX_PIN = 14; //  // Serial 4 Tx Pin
     SERIAL4_RX_PIN = 27; //  // Serial 4 Rx Pin
     SERIAL5_TX_PIN = 13; //  // Serial 5 Tx Pin
     SERIAL5_RX_PIN = 12; //  // Serial 5 Rx Pin
     ONBOARD_LED    = 25; //  // Not Used in this board but defined to match the Version 1.0 board's onboard Green LED
     STATUS_LED_PIN = 19; //  // ESP32 Status NeoPixel Pin
    // Serial.println("Loaded HW Version 2.1 Pinout");
 } else if (wcb_hw_version == 23){
      //////////////////////////////// 
    // This pinout is for a Version 2.3 (Custom Board)
    ////////////////////////////////
     SERIAL1_TX_PIN = 8;  //  // Serial 1 Tx Pin
     SERIAL1_RX_PIN = 21; //  // Serial 1 Rx Pin
     SERIAL2_TX_PIN = 20; //  // Serial 2 Tx Pin
     SERIAL2_RX_PIN = 7;  //  // Serial 2 Rx Pin
     SERIAL3_TX_PIN = 25; //  // Serial 3 Tx Pin
     SERIAL3_RX_PIN = 4;  //  // Serial 3 Rx Pin
     SERIAL4_TX_PIN = 14; //  // Serial 4 Tx Pin
     SERIAL4_RX_PIN = 27; //  // Serial 4 Rx Pin
     SERIAL5_TX_PIN = 13; //  // Serial 5 Tx Pin
     SERIAL5_RX_PIN = 26; //  // Serial 5 Rx Pin
     ONBOARD_LED    = 32; //  // Not Used in this board but defined to match the Version 1.0 board's onboard Green LED
     STATUS_LED_PIN = 19; //  // ESP32 on board NeoPixel Pin
    // Serial.println("Loaded HW Version 2.3 Pinout");
 } else if (wcb_hw_version == 24){
    //////////////////////////////// 
    // This pinout is for a Version 2.4 (Custom Board)
    ////////////////////////////////
    SERIAL1_TX_PIN = 8;  //  // Serial 1 Tx Pin
    SERIAL1_RX_PIN = 21; //  // Serial 1 Rx Pin
    SERIAL2_TX_PIN = 20; //  // Serial 2 Tx Pin
    SERIAL2_RX_PIN = 7;  //  // Serial 2 Rx Pin
    SERIAL3_TX_PIN = 25; //  // Serial 3 Tx Pin
    SERIAL3_RX_PIN = 4;  //  // Serial 3 Rx Pin
    SERIAL4_TX_PIN = 14; //  // Serial 4 Tx Pin 
    SERIAL4_RX_PIN = 27; //  // Serial 4 Rx Pin
    SERIAL5_TX_PIN = 13; //  // Serial 5 Tx Pin
    SERIAL5_RX_PIN = 26; //  // Serial 5 Rx Pin
    ONBOARD_LED    = 32; //  // Not Used in this board but defined to match the Version 1.0 board's onboard Green LED
    STATUS_LED_PIN = 19; //  // ESP32 on board NeoPixel Pin
    // Serial.println("Loaded HW Version 2.4 Pinout");
 }else if (wcb_hw_version == 32 || wcb_hw_version == 31){
    //////////////////////////////// 
    // This pinout is for a Version 3.0 (DIY Version)
    ////////////////////////////////
    SERIAL1_TX_PIN  = 4;  //  // Serial 1 Tx Pin
    SERIAL1_RX_PIN  = 5; //  // Serial 1 Rx Pin
    SERIAL2_TX_PIN  = 6; //  // Serial 2 Tx Pin
    SERIAL2_RX_PIN	= 7;  //  // Serial 2 Rx Pin
    SERIAL3_TX_PIN  = 15; //  // Serial 3 Tx Pin
    SERIAL3_RX_PIN  = 16;  //  // Serial 3 Rx Pin
    SERIAL4_TX_PIN  = 17; //  // Serial 4 Tx Pin
    SERIAL4_RX_PIN  = 18; //  // Serial 4 Rx Pin
    SERIAL5_TX_PIN  = 9; //  // Serial 5 Tx Pin
    SERIAL5_RX_PIN  = 10; //  // Serial 5 Rx Pin
    ONBOARD_LED     = 32; //  // Not Used in this board but defined to match the Version 1.0 board's onboard Green LED
    STATUS_LED_PIN  = 38; //  // ESP32 on board NeoPixel Pin
    // Serial.println("Loaded HW Version 3.0 Pinout");
 }
}


