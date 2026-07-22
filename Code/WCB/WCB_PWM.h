#ifndef WCB_PWM_H
#define WCB_PWM_H

#include <Arduino.h>

#define MAX_PWM_MAPPINGS 10
#define MAX_PWM_OUTPUT_PORTS 5

extern String serialPortLabels[5];
extern String getSerialLabel(int port);


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

// PWM stability tracking (one per input port)
struct PWMStabilityTracker {
    unsigned long lastTransmittedValue;
    unsigned long readings[5];
    int readingIndex;
    bool stabilized;
    unsigned long lastTransmitTime;
};

extern PWMStabilityTracker pwmStability[5];  // One tracker per serial port

void initPWM();
void addPWMMapping(const String &config, bool autoReboot = true);
void removePWMMapping(int inputPort);
void listPWMMappings();
void listPWMMappingsBoot();
void clearAllPWMMappings();
void savePWMMappingsToPreferences();
void loadPWMMappingsFromPreferences();
void processPWMPassthrough();
void configureRemotePWMOutput(int serialPort);
bool isSerialPortUsedForPWMInput(int port);
bool canUsePWMOnPort(int port);   // false if the port is reserved (e.g. Kyber) and can't do PWM


extern int pwmOutputPorts[MAX_PWM_OUTPUT_PORTS];  // Ports configured as PWM output only
extern int pwmOutputCount;
// Provenance parallel to pwmOutputPorts[]: 0 = manually configured (never auto-removed);
// >0 = auto-configured by a WDP PWMTARGET advert from that source WCB. Only a WDP-tagged
// port is eligible for self-heal removal when its driver stops advertising it.
extern uint8_t pwmOutputAutoSrc[MAX_PWM_OUTPUT_PORTS];

void addPWMOutputPort(int port, uint8_t wdpAutoSrc = 0);  // wdpAutoSrc>0 tags a WDP self-config
void removePWMOutputPort(int port);
bool isSerialPortPWMOutput(int port);
// Reconcile the WDP-auto output ports tagged to srcWCB against the ports it still
// advertises (wantPorts): clear any this board auto-configured for srcWCB that are no
// longer in its advert — the mapping was removed on the source. Add-only ports become
// self-healing. Never touches a manual (tag 0) port or one owned by another source.
void reconcileWdpAutoPWMOutputs(uint8_t srcWCB, const uint8_t *wantPorts, uint8_t wantCount);
void savePWMOutputPortsToPreferences();
void loadPWMOutputPortsFromPreferences();

#endif