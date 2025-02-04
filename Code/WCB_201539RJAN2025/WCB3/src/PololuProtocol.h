#include <sys/_stdint.h>
#ifndef POLOLU_PROTOCOL_H
#define POLOLU_PROTOCOL_H


// #pragma once

#include <Arduino.h>
// #include "WCB_Preferences.h"


#define BUFFER_SIZE 64

class PololuProtocol {
public:
    // PololuProtocol(HardwareSerial& serial, int rxPin, int txPin);
    // PololuProtocol(HardwareSerial &incomingSerial, HardwareSerial &maestroSerial, uint8_t rxPin, uint8_t txPin);
    PololuProtocol(HardwareSerial &incomingSerial, HardwareSerial &maestroSerial,
                   uint8_t incomingRxPin, uint8_t incomingTxPin,
                   uint8_t maestroRxPin, uint8_t maestroTxPin);

    void begin(uint32_t baudRate = 9600);
    void processCommands();
    uint8_t maestroDeviceID;
    uint8_t maestroSequence;
    uint8_t maestroCommandByte;
    uint8_t maestroChannel;
    uint16_t maestroTarget;
    uint16_t maestroValue;
    uint8_t maestraoStartBit = 0xAA;
    bool commandRecieved = false;

private:
    // HardwareSerial& _serial;
    // uint8_t _rxPin;
    // uint8_t _txPin;
    // HardwareSerial &_serial;       // Serial interface for incoming commands
    // HardwareSerial &_maestroSerial; // Serial interface to the Maestro
    // uint8_t _rxPin, _txPin;        // Pins for Serial communication
    HardwareSerial &_serial;       // Serial interface for incoming commands
    HardwareSerial &_maestroSerial; // Serial interface to the Maestro
    uint8_t _incomingRxPin, _incomingTxPin; // Pins for incoming serial
    uint8_t _maestroRxPin, _maestroTxPin;   // Pins for Maestro connection
    
    uint8_t pololuProtocolBuffer[BUFFER_SIZE];
    uint8_t bufferIndex;

    void storePololuProtocolData(uint8_t data);
    void handleSetTarget();
    void handleSetSpeedOrAcceleration(uint8_t command);
    void handleGetPosition();
    void handleGetMovingState();
    void handleGetErrors();
    void handleGoHome();
    void handleRestartScript(uint8_t maestroDeviceID);
    void handleRestartScriptWithParameter();
    void handleStopScript();
    void handleGetScriptStatus();

};

#endif
