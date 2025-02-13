// #pragma once
#include "Print.h"
#include <sys/_stdint.h>
#include "PololuProtocol.h"
#include "DebugWCB.h"
// #include "WCB_Preferences.h"

#define MAESTRO_CONTROLLER_SERIAL_PORT Serial1

debugClass Pololu_Debug;


// PololuProtocol::PololuProtocol(HardwareSerial& serial, int rxPin, int txPin)
//     : _serial(MAESTRO_CONTROLLER_SERIAL_PORT), _rxPin(rxPin), _txPin(txPin), bufferIndex(0) {
// }
PololuProtocol::PololuProtocol(HardwareSerial &incomingSerial, HardwareSerial &maestroSerial,
                               uint8_t incomingRxPin, uint8_t incomingTxPin,
                               uint8_t maestroRxPin, uint8_t maestroTxPin)
    : _serial(incomingSerial), _maestroSerial(maestroSerial),
      _incomingRxPin(incomingRxPin), _incomingTxPin(incomingTxPin),
      _maestroRxPin(maestroRxPin), _maestroTxPin(maestroTxPin) {}


void PololuProtocol::begin(uint32_t baudRate) {
    // Initialize the incoming serial port
    _serial.begin(baudRate, SERIAL_8N1, _incomingRxPin, _incomingTxPin);

    // Initialize the Maestro serial port
    _maestroSerial.begin(baudRate, SERIAL_8N1, _maestroRxPin, _maestroTxPin);
}

// void PololuProtocol::begin(uint32_t baudRate) {
//     _serial.begin(baudRate, SERIAL_8N1, _rxPin, _txPin);
// }

void PololuProtocol::processCommands() {
    if (_serial.available() > 0) {
        uint8_t startbit = _serial.read();
        uint8_t LibraryDeviceID = _serial.read();
        uint8_t commandByte = _serial.read();
        maestroCommandByte = commandByte;
        maestroDeviceID = LibraryDeviceID;
        storePololuProtocolData(commandByte);

        switch (commandByte) {
            case 0x84:  // Set Target
                handleSetTarget();
                break;
            case 0x87:  // Set Speed
            case 0x89:  // Set Acceleration
                handleSetSpeedOrAcceleration(commandByte);
                break;
            case 0x90:  // Get Position
                handleGetPosition();
                break;
            case 0x93:  // Get Moving State
                handleGetMovingState();
                break;
            case 0xA1:  // Get Errors
                handleGetErrors();
                break;
            case 0x22:  // Go Home
                handleGoHome();
                break;
            case 0x27:  // Restart Script
                handleRestartScript(LibraryDeviceID);
                break;
            case 0x28:  // Restart Script with Parameter
                handleRestartScriptWithParameter();
                break;
            case 0x24:  // Stop Script
                handleStopScript();
                break;
            case 0x2E:  // Get Script Status
                handleGetScriptStatus();
                break;
            default:
                Pololu_Debug.MAESTRO_DEBUG("Unknown command byte of: 0x%02X\n", commandByte);
                break;
        }
    }
}

void PololuProtocol::storePololuProtocolData(uint8_t data) {
    if (bufferIndex < BUFFER_SIZE) {
        pololuProtocolBuffer[bufferIndex++] = data;
    } else {
        Pololu_Debug.MAESTRO_DEBUG("Buffer overflow. Data not stored.");
    }
}

void PololuProtocol::handleSetTarget() {
    while (_serial.available() < 3) {
        // Wait for all bytes
    }
    uint8_t channel = _serial.read();
    uint16_t target = _serial.read() | (_serial.read() << 8);
    
    storePololuProtocolData(channel);
    storePololuProtocolData(target & 0xFF);
    storePololuProtocolData((target >> 8) & 0xFF);
    maestroChannel = channel;
    maestroTarget = target;
    Pololu_Debug.MAESTRO_DEBUG("Set Target Command: Channel %02X, Target: %02X", channel, target);
    // Serial.print("Set Target Command: Channel ");
    // Serial.print(channel);
    // Serial.print(", Target ");
    // Serial.println(target);
}

void PololuProtocol::handleSetSpeedOrAcceleration(uint8_t command) {
    while (_serial.available() < 3) {
        // Wait for all bytes
    }
    uint8_t channel = _serial.read();
    uint16_t value = _serial.read() | (_serial.read() << 8);
    maestroChannel = channel;
    maestroValue = value;
    storePololuProtocolData(channel);
    storePololuProtocolData(value & 0xFF);
    storePololuProtocolData((value >> 8) & 0xFF);

    if (command == 0x87) {
        Serial.print("Set Speed Command: ");
    } else {
        Serial.print("Set Acceleration Command: ");
    }
    Serial.print("Channel ");
    Serial.print(channel);
    Serial.print(", Value ");
    Serial.println(value);
}

void PololuProtocol::handleGetPosition() {
    while (_serial.available() < 1) {
        // Wait for the byte
    }
    uint8_t channel = _serial.read();
    storePololuProtocolData(channel);

    Serial.print("Get Position Command: Channel ");
    Serial.println(channel);

    uint16_t position = 1500; // Example position
    _serial.write(position & 0xFF);
    _serial.write((position >> 8) & 0xFF);
}

void PololuProtocol::handleGetMovingState() {
    Serial.println("Get Moving State Command");

    uint8_t movingState = 0; // Example moving state
    _serial.write(movingState);
}

void PololuProtocol::handleGetErrors() {
    Serial.println("Get Errors Command");

    uint16_t errors = 0; // Example errors
    _serial.write(errors & 0xFF);
    _serial.write((errors >> 8) & 0xFF);
}

void PololuProtocol::handleGoHome() {
    Serial.println("Go Home Command");
}

void PololuProtocol::handleRestartScript(uint8_t deviceID_L) {
    while (_serial.available() < 1) {
        // Wait for the byte
    }
    uint8_t scriptNumber = _serial.read();
  
    maestroSequence = scriptNumber;
    
    storePololuProtocolData(scriptNumber);
    Serial.print("Device ID: ");
    Serial.println(deviceID_L);
    Serial.print("Restart Script Command: Script Number ");
    Serial.println(scriptNumber);
    commandRecieved = true;

}

void PololuProtocol::handleRestartScriptWithParameter() {
    while (_serial.available() < 3) {
        // Wait for all bytes
    }
    uint8_t scriptNumber = _serial.read();
    uint16_t parameter = _serial.read() | (_serial.read() << 8);
    storePololuProtocolData(scriptNumber);
    storePololuProtocolData(parameter & 0xFF);
    storePololuProtocolData((parameter >> 8) & 0xFF);

    Serial.print("Restart Script with Parameter Command: Script Number ");
    Serial.print(scriptNumber);
    Serial.print(", Parameter ");
    Serial.println(parameter);
  }

void PololuProtocol::handleStopScript() {
    Serial.println("Stop Script Command");
}

void PololuProtocol::handleGetScriptStatus() {
    // Debug: Indicate that we're checking for any running script
    Serial.println("Sending Get Script Status command (0xAE) to Maestro...");

    // Send the 0xAE command to the Maestro
    _maestroSerial.write(0xAE);

    // Wait for a response from the Maestro
    unsigned long startTime = millis();
    while (_maestroSerial.available() < 1) {
        if (millis() - startTime > 1000) {
            Serial.println("Error: Timeout waiting for response from Maestro");
            return;
        }
        delay(1);
    }

    // Read and relay the response
    uint8_t status = _maestroSerial.read();
    _serial.write(status);

    // Debug: Print the status
    Serial.print("Any Script Running Status: ");
    Serial.println(status == 0 ? "Running" : "Not Running");
}


// void PololuProtocol::handleGetScriptStatus() {
//     uint8_t scriptNumber = 1; // Default script number

//     // Wait briefly for a possible script number
//     unsigned long startTime = millis();
//     while (_serial.available() < 1 && millis() - startTime < 100) {
//         delay(1);
//     }

//     if (_serial.available() > 0) {
//         scriptNumber = _serial.read();
//         Serial.print("Script number received: ");
//         Serial.println(scriptNumber);
//     } else {
//         Serial.println("No script number received. Using default value: 0.");
//     }

//     // Debug: Log the command being sent to the Maestro
//     Serial.print("Sending command to Maestro: 0x2E, Script Number: ");
//     Serial.println(scriptNumber);

//     // Forward the command to the Maestro
//     _maestroSerial.write(0x2E);         // Command byte
//     _maestroSerial.write(scriptNumber); // Script number

//     // Wait for a response from the Maestro
//     startTime = millis();
//     while (_maestroSerial.available() < 1) {
//         if (millis() - startTime > 1000) {
//             Serial.println("Error: Timeout waiting for response from Maestro");
//             return;
//         }
//         delay(1);
//     }

//     // Read and relay the response
//     uint8_t status = _maestroSerial.read();
//     _serial.write(status);

//     Serial.print("Script Status: ");
//     Serial.println(status == 1 ? "Running" : "Not Running");
// }

// void PololuProtocol::handleGetScriptStatus() {
//     uint8_t scriptNumber = 0; // Default script number

//     // Check if a script number was sent
//     unsigned long startTime = millis();
//     while (_serial.available() < 1) {
//         if (millis() - startTime > 100) { // Short timeout
//             Serial.println("No script number received. Using default value: 0.");
//             break;
//         }
//         delay(1);
//     }

//     if (_serial.available() > 0) {
//         scriptNumber = _serial.read();
//     }

//     storePololuProtocolData(scriptNumber);

//     // Forward the command to the Maestro
//     _maestroSerial.write(0xA4);         // Command byte
//     _maestroSerial.write(scriptNumber); // Script number

//     // Wait for a response from the Maestro
//     startTime = millis();
//     while (_maestroSerial.available() < 1) {
//         if (millis() - startTime > 1000) {
//             Serial.println("Error: Timeout waiting for response from Maestro");
//             return;
//         }
//         delay(1);
//     }

//     // Read and relay the response
//     uint8_t status = _maestroSerial.read();
//     _serial.write(status);

//     Serial.print("Script Status: ");
//     Serial.println(status == 1 ? "Running" : "Not Running");
// }

// void PololuProtocol::handleGetScriptStatus() {
//     unsigned long startTime = millis();
//     while (_serial.available() < 1) {
//         if (millis() - startTime > 1000) {
//             Serial.println("Error: Timeout waiting for script number on Serial1");
//             return;
//         }
//         delay(1);
//     }

//     // Read the script number from the incoming serial
//     uint8_t scriptNumber = _serial.read();
//     storePololuProtocolData(scriptNumber);

//     Serial.print("Forwarding Get Script Status Command: Script Number ");
//     Serial.println(scriptNumber);

//     // Forward the command to the Maestro
//     _maestroSerial.write(0xA4);         // Command byte
//     _maestroSerial.write(scriptNumber); // Script number

//     // Wait for a response from the Maestro
//     startTime = millis();
//     while (_maestroSerial.available() < 1) {
//         if (millis() - startTime > 1000) {
//             Serial.println("Error: Timeout waiting for response from Maestro");
//             return;
//         }
//         delay(1);
//     }

//     // Read the status byte from the Maestro
//     uint8_t status = _maestroSerial.read();

//     // Send the response back to the source on the incoming serial
//     _serial.write(status);

//     // Debugging output
//     Serial.print("Received Script Status from Maestro: ");
//     Serial.println(status == 1 ? "Running" : "Not Running");
//     Serial.println("Relayed Script Status to Incoming Serial");
// }
// void PololuProtocol::handleGetScriptStatus() {
//     // Ensure we receive the script number byte from Serial1
//     unsigned long startTime = millis();
//     while (_serial.available() < 1) {
//         if (millis() - startTime > 1000) {
//             Serial.println("Error: Timeout waiting for script number on Serial1");
//             return;
//         }
//         delay(1);  // Avoid tight loop
//     }

//     // Read the script number from Serial1
//     uint8_t scriptNumber = _serial.read();
//     storePololuProtocolData(scriptNumber);

//     Serial.print("Forwarding Get Script Status Command: Script Number ");
//     Serial.println(scriptNumber);

//     // Forward the command to the Maestro (Serial2)
//     _maestroSerial.write(0xA4);         // Command byte
//     _maestroSerial.write(scriptNumber); // Script number

//     // Wait for a response from the Maestro
//     startTime = millis();
//     while (_maestroSerial.available() < 1) {
//         if (millis() - startTime > 1000) {
//             Serial.println("Error: Timeout waiting for response from Maestro");
//             return;
//         }
//         delay(1);  // Avoid tight loop
//     }

//     // Read the status byte from the Maestro
//     uint8_t status = _maestroSerial.read();

//     // Send the response back to the source on Serial1
//     _serial.write(status);

//     // Debugging output
//     Serial.print("Received Script Status from Maestro: ");
//     Serial.println(status == 1 ? "Running" : "Not Running");
//     Serial.println("Relayed Script Status to Serial1");
// }

// void PololuProtocol::handleGetScriptStatus() {
//     while (_serial.available() < 1) {}
//     uint8_t scriptNumber = _serial.read();
//     storePololuProtocolData(scriptNumber);

//     Serial.print("Get Script Status Command: Script Number ");
//     Serial.println(scriptNumber);

//     uint8_t status = 0; // Example status (0 = not running, 1 = running)
//     _serial.write(status);
// }
// void PololuProtocol::handleGetScriptStatus() {
//     unsigned long startTime = millis(); // Start timeout timer
//     while (_serial.available() < 1) {   // Wait for the script number byte
//         if (millis() - startTime > 1000) { // Timeout after 1 second
//             Serial.println("Error: Timeout waiting for script number");
//             return;  // Exit the function if timeout occurs
//         }
//         delay(1);  // Avoid tight loop
//     }

//     // Read the script number from the incoming data
//     uint8_t scriptNumber = _serial.read();
//     storePololuProtocolData(scriptNumber);

//     Serial.print("Received Get Script Status Command: Script Number ");
//     Serial.println(scriptNumber);

//     // Simulate a script status response (you can replace this with actual logic)
//     uint8_t status = (scriptNumber == 0) ? 1 : 0; // For example, script 0 is running, others are not

//     // Send the status back as a response
//     _serial.write(status);

//     // Debugging output for status sent
//     Serial.print("Sent Script Status: ");
//     Serial.println(status == 1 ? "Running" : "Not Running");
// }