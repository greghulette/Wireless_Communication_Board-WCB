#ifndef WCB_PWM_H
#define WCB_PWM_H

#include <Arduino.h>

#define MAX_PWM_MAPPINGS 10
#define MAX_PWM_OUTPUT_PORTS 5
struct PWMMapping {
    int inputPort;           // 1-5: which serial port RX pin to read PWM from
    int outputCount;         // How many outputs
    struct {
        int wcbNumber;       // WCB to send to (0 = local)
        int serialPort;      // Serial port TX pin to output on
    } outputs[5];           // Max 5 outputs per input
    bool active;
};

extern PWMMapping pwmMappings[MAX_PWM_MAPPINGS];
extern int activePWMCount;

void initPWM();
void addPWMMapping(const String &config, bool autoReboot = true);
void removePWMMapping(int inputPort);
void listPWMMappings();
void clearAllPWMMappings();
void savePWMMappingsToPreferences();
void loadPWMMappingsFromPreferences();
void processPWMPassthrough();
void configureRemotePWMOutput(int serialPort);
bool isSerialPortUsedForPWMInput(int port);


extern int pwmOutputPorts[MAX_PWM_OUTPUT_PORTS];  // Ports configured as PWM output only
extern int pwmOutputCount;

void addPWMOutputPort(int port);
void removePWMOutputPort(int port);
bool isSerialPortPWMOutput(int port);
void savePWMOutputPortsToPreferences();
void loadPWMOutputPortsFromPreferences();

#endif