#ifndef BOARDS_L
#define BOARDS_L

#if defined(TEENSYDUINO) 
    //  --------------- Teensy -----------------
    #if defined(__AVR_ATmega32U4__)
        #define BOARD "Teensy 2.0"
    #elif defined(__AVR_AT90USB1286__)       
        #define BOARD "Teensy++ 2.0"
    #elif defined(__MK20DX128__)       
        #define BOARD "Teensy 3.0"
    #elif defined(__MK20DX256__)       
        #define BOARD "Teensy 3.2" // and Teensy 3.1 (obsolete)
    #elif defined(__MKL26Z64__)       
        #define BOARD "Teensy LC"
    #elif defined(__MK64FX512__)
        #define BOARD "Teensy 3.5"
    #elif defined(__MK66FX1M0__)
        #define BOARD "Teensy 3.6"
    #endif
#elif defined(ESP32)
    #define BOARD "ESP32"
#else
// --------------- Arduino ------------------
    #if defined(ARDUINO_AVR_ADK)       
        #define BOARD "Mega Adk"
    #elif defined(ARDUINO_AVR_BT)    // Bluetooth
        #define BOARD "BT"
    #elif defined(ARDUINO_AVR_DUEMILANOVE)       
        #define BOARD "Duemilanove"
    #elif defined(ARDUINO_AVR_ESPLORA)       
        #define BOARD "Esplora"
    #elif defined(ARDUINO_AVR_ETHERNET)       
        #define BOARD "Ethernet"
    #elif defined(ARDUINO_AVR_FIO)       
        #define BOARD "Fio"
    #elif defined(ARDUINO_AVR_GEMMA)
        #define BOARD "Gemma"
    #elif defined(ARDUINO_AVR_LEONARDO)       
        #define BOARD "Leonardo"
    #elif defined(ARDUINO_AVR_LILYPAD)
        #define BOARD "Lilypad"
    #elif defined(ARDUINO_AVR_LILYPAD_USB)
        #define BOARD "Lilypad Usb"
    #elif defined(ARDUINO_AVR_MEGA)       
        #define BOARD "Mega"
    #elif defined(ARDUINO_AVR_MEGA2560)       
        #define BOARD "Mega 2560"
    #elif defined(ARDUINO_AVR_MICRO)       
        #define BOARD "Micro"
    #elif defined(ARDUINO_AVR_MINI)       
        #define BOARD "Mini"
    #elif defined(ARDUINO_AVR_NANO)       
        #define BOARD "Nano"
    #elif defined(ARDUINO_AVR_NG)       
        #define BOARD "NG"
    #elif defined(ARDUINO_AVR_PRO)       
        #define BOARD "Pro"
    #elif defined(ARDUINO_AVR_ROBOT_CONTROL)       
        #define BOARD "Robot Ctrl"
    #elif defined(ARDUINO_AVR_ROBOT_MOTOR)       
        #define BOARD "Robot Motor"
    #elif defined(ARDUINO_AVR_UNO)       
        #define BOARD "Uno"
    #elif defined(ARDUINO_AVR_YUN)       
        #define BOARD "Yun"
    #elif defined(ARDUINO_SAM_DUE)       
        #define BOARD "Due"
    #elif defined(ARDUINO_SAMD_ZERO)       
        #define BOARD "Zero"
    #elif defined(ARDUINO_ARC32_TOOLS)       
        #define BOARD "101"
    #elif defined(ARDUINO_PORTENTA_H7_M7) || defined(ARDUINO_PORTENTA_H7_M4) || defined(ARDUINO_PORTENTA_X8)
        #define BOARD "Portenta H7"
    #endif
#endif

#endif

/*
ARDUINO_AVR_ADK
ARDUINO_AVR_BT
ARDUINO_AVR_CIRCUITPLAY
ARDUINO_AVR_DUEMILANOVE
ARDUINO_AVR_ESPLORA
ARDUINO_AVR_ETHERNET
ARDUINO_AVR_FIO
ARDUINO_AVR_GEMMA
ARDUINO_AVR_INDUSTRIAL101
ARDUINO_AVR_LEONARDO
ARDUINO_AVR_LEONARDO_ETH
ARDUINO_AVR_LILYPAD
ARDUINO_AVR_LILYPAD_USB
ARDUINO_AVR_LININO_ONE
ARDUINO_AVR_MEGA2560
ARDUINO_AVR_MICRO
ARDUINO_AVR_MINI
ARDUINO_AVR_NANO
ARDUINO_AVR_NG
ARDUINO_AVR_PRO
ARDUINO_AVR_ROBOT_CONTROL
ARDUINO_AVR_ROBOT_MOTOR
ARDUINO_AVR_UNO
ARDUINO_AVR_UNO
ARDUINO_AVR_UNO_WIFI_DEV_ED
ARDUINO_AVR_YUN
ARDUINO_AVR_YUNMINI

NANO_RP2040_CONNECT
ARDUINO_NANO33BLE

PORTENTA_H7_M7
PORTENTA_H7_M4
PORTENTA_X8
*/