// #pragma once
#include "Print.h"
#include <sys/_stdint.h>
#include "PololuProtocol.h"
#include "DebugWCB.h"
// #include "WCB_Preferences.h"

#define MAESTRO_CONTROLLER_SERIAL_PORT Serial1

debugClass Pololu_Debug;


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
        Serial.print("StartBit: "); Serial.println(startbit);
        Serial.print("Device ID: "); Serial.println(LibraryDeviceID);
        Serial.print("Command Byte: "); Serial.println(commandByte);
        // Serial.print("StartBit: "); Serial.println(startbit);

        Serial.print("Processing command: 0x");
        Serial.println(commandByte, HEX);

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
                handleRestartScript(maestroDeviceID);
                // startScriptTracking(maestroDeviceID, 20000); // Start tracking with timeout
                // isTrackingScript = true; // Start tracking the script
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
                Serial.print("Unknown command byte: 0x");
                Serial.println(commandByte, HEX);
                break;
        }
    }

    // Poll for script execution status
    // if (isTrackingScript) {
    //     pollScriptExecution();
    // }
}

// void PololuProtocol::processCommands() {
//     if (_serial.available() > 0) {
//         uint8_t startbit = _serial.read();
//         uint8_t LibraryDeviceID = _serial.read();
//         uint8_t commandByte = _serial.read();
//         maestroCommandByte = commandByte;
//         maestroDeviceID = LibraryDeviceID;
//         storePololuProtocolData(commandByte);

//         switch (commandByte) {
//             case 0x84:  // Set Target
//                 handleSetTarget();
//                 break;
//             case 0x87:  // Set Speed
//             case 0x89:  // Set Acceleration
//                 handleSetSpeedOrAcceleration(commandByte);
//                 break;
//             case 0x90:  // Get Position
//                 handleGetPosition();
//                 break;
//             case 0x93:  // Get Moving State
//                 handleGetMovingState();
//                 break;
//             case 0xA1:  // Get Errors
//                 handleGetErrors();
//                 break;
//             case 0x22:  // Go Home
//                 handleGoHome();
//                 break;
//             case 0x27:  // Restart Script
//                 handleRestartScript(maestroDeviceID);
//                 startScriptTracking(maestroDeviceID, 20000); // Default timeout of 10 seconds

//                 break;
//             case 0x28:  // Restart Script with Parameter
//                 handleRestartScriptWithParameter();
//                 break;
//             case 0x24:  // Stop Script
//                 handleStopScript();
//                 break;
//             case 0x2E:  // Get Script Status
//                 handleGetScriptStatus();
//                 break;
//             default:
//                 Pololu_Debug.MAESTRO_DEBUG("Unknown command byte of: 0x%02X\n", commandByte);
//                 break;
//         }
//     }
//     pollScriptExecution();
// }

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

void PololuProtocol::startScriptTracking(uint8_t scriptNumber, unsigned long timeoutMs) {
    _trackedScriptNumber = scriptNumber;
    _trackingTimeout = timeoutMs;
    _trackingStartTime = millis();
    _trackingState = STARTING;

    Serial.print("Starting non-blocking tracking for script ");
    Serial.println(scriptNumber);
    // delay(50);
}

// bool PololuProtocol::pollScriptExecution() {
//     switch (_trackingState) {
//     case IDLE:
//         return true;

//     case STARTING:
//         Serial.println("Sending Restart Script command...");
//         _maestroSerial.write(0xA7); // Restart script
//         _maestroSerial.write(_trackedScriptNumber);
//         _trackingState = POLLING;
//         break;

//     case POLLING:
//         if (millis() - _trackingStartTime > _trackingTimeout) {
//             Serial.println("Error: Script tracking timed out.");
//             _trackingState = IDLE;
//             return false;
//         }

//         // Send "Get Script Status" command
//         _maestroSerial.write(0xAE); 
//         delay(10); // Give the Maestro some time to respond

//         if (_maestroSerial.available() > 0) {
//             uint8_t status = _maestroSerial.read();
//             // Serial.print("Script Running Status: ");
//             // Serial.println(status == 0 ? "Running" : "Not Running");

//             if (status == 0) {
//                 Serial.println("Script is Running.");
//                 _trackingState = POLLING;
//                 return true;
//             } else if (status == 1) {
//                 _trackingState = IDLE;
//                 Serial.println("Script has completed.");
//                 isTrackingScript = false;  // Stop tracking
//                 return false;
//             }
//         } else {
//             Serial.println("Waiting for Maestro response...");
//         }
//         break;
//     }

//     return false;
// }
// bool PololuProtocol::pollScriptExecution() {
//     switch (_trackingState) {
//     case IDLE:
//         // No script is being tracked
//         return false;

//     case STARTING:
//         // Send the "Restart Script" command
//         Serial.println("Sending Restart Script command...");
//         _maestroSerial.write(0xA7); // Restart Script Command
//         _maestroSerial.write(_trackedScriptNumber);

//         _trackingState = POLLING;
//         _trackingStartTime = millis(); // Record start time for timeout handling
//         return true;

//     case POLLING:
//         // Check for timeout
//         if (millis() - _trackingStartTime > _trackingTimeout) {
//             Serial.println("Error: Script tracking timed out.");
//             _trackingState = IDLE;
//             return false;
//         }

//         // Check for response from Maestro
//         if (_maestroSerial.available() > 0) {
//             uint8_t status = _maestroSerial.read();

//             // Process the status
//             if (status == 0) {
//                 Serial.println("Script is running.");
//                 return true; // Continue polling
//             } else if (status == 1) {
//                 Serial.println("Script has completed.");
//                 _trackingState = IDLE;
//                 isTrackingScript = false; // Stop tracking
//                 return false;
//             } else {
//                 Serial.print("Unexpected status: ");
//                 Serial.println(status, HEX);
//             }
//         } else {
//             // No response yet, keep waiting
//             Serial.println("Waiting for Maestro response...");
//         }
//         break;
//     }

//     return false; // Default case
// }
bool PololuProtocol::pollScriptExecution() {
    switch (_trackingState) {
    case IDLE:
        return false; // No script is being tracked

    case STARTING:
        Serial.println("Sending Restart Script command...");
        _maestroSerial.write(0xA7); // Restart Script Command
        _maestroSerial.write(_trackedScriptNumber);

        _trackingState = POLLING;
        _trackingStartTime = millis(); // Record start time for timeout handling
        return true;

    case POLLING:
        // Check for timeout
        if (millis() - _trackingStartTime > _trackingTimeout) {
            Serial.println("Error: Script tracking timed out.");
            _trackingState = IDLE;
            return false;
        }

        // Send "Get Script Status" command
        Serial.println("Sending Get Script Status command...");
        _maestroSerial.write(0xAE); // Get Script Status Command

        // Wait for a response
        if (_maestroSerial.available() > 0) {
            uint8_t status = _maestroSerial.read();
            Serial.print("Received Script Status: ");
            Serial.println(status, HEX);

            if (status == 0) {
                Serial.println("Script is running.");
                return true; // Script is still running
            } else if (status == 1) {
                Serial.println("Script has completed.");
                _trackingState = IDLE;
                isTrackingScript = false; // Stop tracking
                return false;
            } else {
                Serial.print("Unexpected response: ");
                Serial.println(status, HEX);
            }
        } else {
            Serial.println("No response from Maestro. Retrying...");
        }
        break;
    }

    return false; // Default case
}


bool PololuProtocol::isScriptRunning() {
    // Send "Get Script Status" command (0xAE)
    _maestroSerial.write(0xAE);
if (_maestroSerial.available() > 0) {
    while (_maestroSerial.available()) {
        uint8_t byte = _maestroSerial.read();
        Serial.print("Received byte: ");
        Serial.println(byte, HEX);
    }
} else {
    Serial.println("No data received.");
}
    // Wait briefly for a response (non-blocking)
    unsigned long startTime = millis();
    while (!_maestroSerial.available()) {
        if (millis() - startTime > 500) { // Timeout after 10ms
            Serial.println("Error: No response from Maestro when checking script status.");
            return false; // Assume no script is running if there's no response
        }
    }

    // Read the status byte
    uint8_t status = _maestroSerial.read();
Serial.print("Raw status byte: ");
Serial.println(status, HEX);  // Print status as hexadecimal
    // Return true if any script is running (status = 1)
    return status == 1;
}