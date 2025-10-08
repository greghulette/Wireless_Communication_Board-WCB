//////////////////////////////// 
// Wireless Communication Board (WCB) Pinout
//////////////////////////////// 

#ifndef WCB_PIN_MAP_H
#define WCB_PIN_MAP_H

// Pin declarations - these are defined in wcb_pin_map.cpp
extern int SERIAL1_TX_PIN;        // Serial 1 Tx Pin
extern int SERIAL1_RX_PIN;        // Serial 1 Rx Pin
extern int SERIAL2_TX_PIN;        // Serial 2 Tx Pin
extern int SERIAL2_RX_PIN;        // Serial 2 Rx Pin
extern int SERIAL3_TX_PIN;        // Serial 3 Tx Pin
extern int SERIAL3_RX_PIN;        // Serial 3 Rx Pin
extern int SERIAL4_TX_PIN;        // Serial 4 Tx Pin 
extern int SERIAL4_RX_PIN;        // Serial 4 Rx Pin
extern int SERIAL5_TX_PIN;        // Serial 5 Tx Pin
extern int SERIAL5_RX_PIN;        // Serial 5 Rx Pin
extern int ONBOARD_LED;           // ESP32 Status LED Pin
extern int STATUS_LED_PIN;        // NeoPixel LED Pin

// Hardware version and debug flag
extern int wcb_hw_version;
extern bool debugEnabled;

// Function declarations
extern void updatePinMap();

#endif // WCB_PIN_MAP_H