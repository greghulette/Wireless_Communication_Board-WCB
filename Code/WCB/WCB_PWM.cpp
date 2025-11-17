#include "WCB_PWM.h"
#include "WCB_Storage.h"
#include "wcb_pin_map.h"
#include <Preferences.h>

extern Preferences preferences;
extern int WCB_Number;
extern void sendESPNowMessage(uint8_t target, const char *message);
extern bool debugEnabled;
extern bool Kyber_Local;
extern bool Kyber_Remote;

PWMMapping pwmMappings[MAX_PWM_MAPPINGS];
int activePWMCount = 0;

// PWM Stability Tracking
PWMStabilityTracker pwmStability[5] = {
    {0, {0}, 0, false, 0},
    {0, {0}, 0, false, 0},
    {0, {0}, 0, false, 0},
    {0, {0}, 0, false, 0},
    {0, {0}, 0, false, 0}
};

const unsigned long PWM_MIN_TRANSMIT_INTERVAL = 15;  // ms between transmits
const int PWM_CHANGE_THRESHOLD = 6;  // μs change to trigger transmission
const int PWM_STABILITY_RANGE = 6;   // μs range for stability

int pwmOutputPorts[MAX_PWM_OUTPUT_PORTS] = {0, 0, 0, 0, 0};
int pwmOutputCount = 0;

// PWM reading variables for each port
volatile unsigned long pwmRiseTime[5] = {0};
volatile unsigned long pwmPulseWidth[5] = {0};
volatile bool pwmNewData[5] = {false};
extern bool espNowInitialized;

// Helper function
bool canSendESPNow() {
    return (millis() > 5000);
}

// Validation function to prevent PWM conflicts with Kyber
bool canUsePWMOnPort(int port) {
    // Serial1 is reserved when Kyber_Local or Kyber_Remote
    if (port == 1 && (Kyber_Local || Kyber_Remote)) {
        Serial.println("❌ Cannot use PWM on Serial1 - reserved for Maestro/Kyber");
        return false;
    }
    
    // Serial2 is reserved when Kyber_Local
    if (port == 2 && Kyber_Local) {
        Serial.println("❌ Cannot use PWM on Serial2 - reserved for Kyber");
        return false;
    }
    
    return true;
}

// ISR handlers for each input port
void IRAM_ATTR pwmISR1() {
    if (digitalRead(SERIAL1_RX_PIN) == HIGH) {
        pwmRiseTime[0] = micros();
    } else {
        pwmPulseWidth[0] = micros() - pwmRiseTime[0];
        pwmNewData[0] = true;
    }
}

void IRAM_ATTR pwmISR2() {
    if (digitalRead(SERIAL2_RX_PIN) == HIGH) {
        pwmRiseTime[1] = micros();
    } else {
        pwmPulseWidth[1] = micros() - pwmRiseTime[1];
        pwmNewData[1] = true;
    }
}

void IRAM_ATTR pwmISR3() {
    if (digitalRead(SERIAL3_RX_PIN) == HIGH) {
        pwmRiseTime[2] = micros();
    } else {
        pwmPulseWidth[2] = micros() - pwmRiseTime[2];
        pwmNewData[2] = true;
    }
}

void IRAM_ATTR pwmISR4() {
    if (digitalRead(SERIAL4_RX_PIN) == HIGH) {
        pwmRiseTime[3] = micros();
    } else {
        pwmPulseWidth[3] = micros() - pwmRiseTime[3];
        pwmNewData[3] = true;
    }
}

void IRAM_ATTR pwmISR5() {
    if (digitalRead(SERIAL5_RX_PIN) == HIGH) {
        pwmRiseTime[4] = micros();
    } else {
        pwmPulseWidth[4] = micros() - pwmRiseTime[4];
        pwmNewData[4] = true;
    }
}

void initPWM() {
    for (int i = 0; i < MAX_PWM_MAPPINGS; i++) {
        pwmMappings[i].active = false;
        pwmMappings[i].inputPort = 0;
        pwmMappings[i].outputCount = 0;
    }
    loadPWMMappingsFromPreferences();
    loadPWMOutputPortsFromPreferences();
}

void attachPWMInterrupt(int port) {
    // Check if port can be used for PWM
    if (!canUsePWMOnPort(port)) {
        return;
    }
    
    int pin = 0;
    void (*isr)() = nullptr;
    
    switch(port) {
        case 1: pin = SERIAL1_RX_PIN; isr = pwmISR1; break;
        case 2: pin = SERIAL2_RX_PIN; isr = pwmISR2; break;
        case 3: pin = SERIAL3_RX_PIN; isr = pwmISR3; break;
        case 4: pin = SERIAL4_RX_PIN; isr = pwmISR4; break;
        case 5: pin = SERIAL5_RX_PIN; isr = pwmISR5; break;
        default: return;
    }
    
    pinMode(pin, INPUT);
    attachInterrupt(digitalPinToInterrupt(pin), isr, CHANGE);
}

void detachPWMInterrupt(int port) {
    int pin = 0;
    switch(port) {
        case 1: pin = SERIAL1_RX_PIN; break;
        case 2: pin = SERIAL2_RX_PIN; break;
        case 3: pin = SERIAL3_RX_PIN; break;
        case 4: pin = SERIAL4_RX_PIN; break;
        case 5: pin = SERIAL5_RX_PIN; break;
        default: return;
    }
    detachInterrupt(digitalPinToInterrupt(pin));
}

void addPWMMapping(const String &config, bool autoReboot) {
    if (!config.startsWith("PMS") && !config.startsWith("pms")) {
        Serial.println("Invalid PWM mapping format");
        return;
    }
    
    String working = config.substring(3);
    int commaPos = working.indexOf(',');
    if (commaPos == -1) {
        Serial.println("Invalid format. Use: ?PMSx,Sy,WySz... where x=input port, S=local serial, Wy=WCB#, Sz=serial port");
        return;
    }
    
    int inputPort = working.substring(0, commaPos).toInt();
    if (inputPort < 1 || inputPort > 5) {
        Serial.println("Input port must be 1-5");
        return;
    }
    
    // Validate input port is not in use by Kyber
    if (!canUsePWMOnPort(inputPort)) {
        Serial.println("PWM mapping blocked - input port in use by Kyber/Maestro");
        return;
    }
    
    int slot = -1;
    for (int i = 0; i < MAX_PWM_MAPPINGS; i++) {
        if (pwmMappings[i].active && pwmMappings[i].inputPort == inputPort) {
            slot = i;
            break;
        }
        if (!pwmMappings[i].active && slot == -1) {
            slot = i;
        }
    }
    
    if (slot == -1) {
        Serial.println("No available PWM mapping slots");
        return;
    }
    
    working = working.substring(commaPos + 1);
    PWMMapping &mapping = pwmMappings[slot];
    mapping.inputPort = inputPort;
    mapping.outputCount = 0;
    
    int startIdx = 0;
    while (startIdx < working.length() && mapping.outputCount < 5) {
        int nextComma = working.indexOf(',', startIdx);
        String output = (nextComma == -1) ? working.substring(startIdx) : working.substring(startIdx, nextComma);
        output.trim();
        
        int wcbNum = 0;
        int serialPort = 0;
        
        if (output.startsWith("S") || output.startsWith("s")) {
            wcbNum = 0;
            serialPort = output.substring(1).toInt();
        } else if (output.startsWith("W") || output.startsWith("w")) {
            output = output.substring(1);
            int sPos = output.indexOf('S');
            if (sPos == -1) sPos = output.indexOf('s');
            
            if (sPos > 0) {
                wcbNum = output.substring(0, sPos).toInt();
                serialPort = output.substring(sPos + 1).toInt();
            }
        }
        
        if (serialPort >= 1 && serialPort <= 5 && wcbNum >= 0 && wcbNum <= 9) {
            // Validate local output ports aren't in use by Kyber
            if (wcbNum == 0 && !canUsePWMOnPort(serialPort)) {
                Serial.printf("⚠️  Skipping output Serial%d - reserved for Kyber\n", serialPort);
            } else {
                mapping.outputs[mapping.outputCount].wcbNumber = wcbNum;
                mapping.outputs[mapping.outputCount].serialPort = serialPort;
                mapping.outputCount++;
            }
        }
        
        if (nextComma == -1) break;
        startIdx = nextComma + 1;
    }
    
    if (mapping.outputCount == 0) {
        Serial.println("No valid outputs specified");
        return;
    }
    
    mapping.active = true;
    attachPWMInterrupt(inputPort);
    
    bool hasRemoteOutputs = false;
    
   for (int i = 0; i < mapping.outputCount; i++) {
    if (mapping.outputs[i].wcbNumber == 0) {
        int txPin = 0;
        switch(mapping.outputs[i].serialPort) {
            case 1: txPin = SERIAL1_TX_PIN; break;
            case 2: txPin = SERIAL2_TX_PIN; break;
            case 3: txPin = SERIAL3_TX_PIN; break;
            case 4: txPin = SERIAL4_TX_PIN; break;
            case 5: txPin = SERIAL5_TX_PIN; break;
        }
        if (txPin > 0) {
            pinMode(txPin, OUTPUT);
            digitalWrite(txPin, LOW);
        }
    } else {
        hasRemoteOutputs = true;  // 👈 ADD THIS
    }
}
    
    savePWMMappingsToPreferences();
    
    Serial.printf("PWM Mapping added: Serial%d -> %d output(s)\n", inputPort, mapping.outputCount);
        
    if (hasRemoteOutputs && canSendESPNow()) {
        for (int i = 0; i < mapping.outputCount; i++) {
            if (mapping.outputs[i].wcbNumber != 0) {
                char remoteCmd[32];
                snprintf(remoteCmd, sizeof(remoteCmd), "?PO%d", mapping.outputs[i].serialPort);
                sendESPNowMessage(mapping.outputs[i].wcbNumber, remoteCmd);
                delay(50);
                if (debugEnabled) {
                    Serial.printf("Sent PWM output config to WCB%d: %s\n", mapping.outputs[i].wcbNumber, remoteCmd);
                }
            }
        }
        delay(100);
        for (int i = 0; i < mapping.outputCount; i++) {
            if (mapping.outputs[i].wcbNumber != 0) {
                sendESPNowMessage(mapping.outputs[i].wcbNumber, "?REBOOT");
                if (debugEnabled) {
                    Serial.printf("Sent reboot command to WCB%d\n", mapping.outputs[i].wcbNumber);
                }
            }
        }
    }
    
    if (autoReboot) {
        Serial.println("Rebooting in 3 seconds to apply PWM configuration...");
        delay(3000);
        ESP.restart();
    }
}

void removePWMMapping(int inputPort) {
    for (int i = 0; i < MAX_PWM_MAPPINGS; i++) {
        if (pwmMappings[i].active && pwmMappings[i].inputPort == inputPort) {
            detachPWMInterrupt(inputPort);
            
            for (int j = 0; j < pwmMappings[i].outputCount; j++) {
                if (pwmMappings[i].outputs[j].wcbNumber == 0) {
                    removePWMOutputPort(pwmMappings[i].outputs[j].serialPort);
                } else {
                    char remoteCmd[32];
                    snprintf(remoteCmd, sizeof(remoteCmd), "?PX%d", pwmMappings[i].outputs[j].serialPort);
                    sendESPNowMessage(pwmMappings[i].outputs[j].wcbNumber, remoteCmd);
                    if (debugEnabled) {
                        Serial.printf("Sent PWM output removal to WCB%d: %s\n", 
                                     pwmMappings[i].outputs[j].wcbNumber, remoteCmd);
                    }
                }
            }
            
            pwmMappings[i].active = false;
            savePWMMappingsToPreferences();
            Serial.printf("Removed PWM mapping for Serial%d\n", inputPort);
            
            Serial.println("Rebooting in 3 seconds to apply changes...");
            delay(3000);
            ESP.restart();
            return;
        }
    }
    Serial.printf("No PWM mapping found for Serial%d\n", inputPort);
}

void listPWMMappings() {
    Serial.println("\n--- PWM Mappings ---");
    
    // Show input-to-output mappings
    bool foundMappings = false;
    for (int i = 0; i < MAX_PWM_MAPPINGS; i++) {
        if (pwmMappings[i].active) {
            foundMappings = true;
            Serial.printf("Input: Serial%d -> Outputs: ", pwmMappings[i].inputPort);
            for (int j = 0; j < pwmMappings[i].outputCount; j++) {
                if (pwmMappings[i].outputs[j].wcbNumber == 0) {
                    Serial.printf("S%d ", pwmMappings[i].outputs[j].serialPort);
                } else {
                    Serial.printf("W%dS%d ", 
                        pwmMappings[i].outputs[j].wcbNumber,
                        pwmMappings[i].outputs[j].serialPort);
                }
            }
            Serial.println();
        }
    }
    if (!foundMappings) Serial.println("No input mappings configured");
    
    // Show output-only ports
    Serial.println("\nPWM Output-Only Ports:");
    if (pwmOutputCount > 0) {
        Serial.print("Configured outputs: ");
        for (int i = 0; i < pwmOutputCount; i++) {
            Serial.printf("S%d ", pwmOutputPorts[i]);
        }
        Serial.println("(receive PWM via ESP-NOW)");
    } else {
        Serial.println("No output-only ports configured");
    }
    
    Serial.println("--- End PWM Mappings ---\n");
}

void clearAllPWMMappings() {
    bool remoteBoards[10] = {false};
    int remotePorts[10][5];
    int remotePortCounts[10] = {0};
    
    for (int i = 0; i < MAX_PWM_MAPPINGS; i++) {
        if (pwmMappings[i].active) {
            detachPWMInterrupt(pwmMappings[i].inputPort);
            
            for (int j = 0; j < pwmMappings[i].outputCount; j++) {
                if (pwmMappings[i].outputs[j].wcbNumber == 0) {
                    removePWMOutputPort(pwmMappings[i].outputs[j].serialPort);
                } else {
                    int wcb = pwmMappings[i].outputs[j].wcbNumber;
                    remoteBoards[wcb] = true;
                    remotePorts[wcb][remotePortCounts[wcb]++] = pwmMappings[i].outputs[j].serialPort;
                }
            }
            
            pwmMappings[i].active = false;
        }
    }
    
    if (canSendESPNow()) {
        for (int wcb = 1; wcb <= 9; wcb++) {
            if (remoteBoards[wcb]) {
                for (int p = 0; p < remotePortCounts[wcb]; p++) {
                    char remoteCmd[32];
                    snprintf(remoteCmd, sizeof(remoteCmd), "?PX%d", remotePorts[wcb][p]);
                    sendESPNowMessage(wcb, remoteCmd);
                    delay(50);
                    if (debugEnabled) {
                        Serial.printf("Sent PWM output removal to WCB%d: %s\n", wcb, remoteCmd);
                    }
                }
                
                delay(50);
                sendESPNowMessage(wcb, "?REBOOT");
                if (debugEnabled) {
                    Serial.printf("Sent reboot command to WCB%d\n", wcb);
                }
            }
        }
    }
    
    preferences.begin("pwm_mappings", false);
    preferences.clear();
    preferences.end();
    
    preferences.begin("pwm_outputs", false);
    preferences.clear();
    preferences.end();
    
    Serial.println("All PWM mappings cleared");
    
    Serial.println("Rebooting in 3 seconds to apply changes...");
    delay(3000);
    ESP.restart();
}

void savePWMMappingsToPreferences() {
    preferences.begin("pwm_mappings", false);
    for (int i = 0; i < MAX_PWM_MAPPINGS; i++) {
        String key = "map" + String(i);
        if (pwmMappings[i].active) {
            String value = String(pwmMappings[i].inputPort) + ",";
            for (int j = 0; j < pwmMappings[i].outputCount; j++) {
                if (pwmMappings[i].outputs[j].wcbNumber == 0) {
                    value += "S" + String(pwmMappings[i].outputs[j].serialPort);
                } else {
                    value += "W" + String(pwmMappings[i].outputs[j].wcbNumber) + 
                             "S" + String(pwmMappings[i].outputs[j].serialPort);
                }
                if (j < pwmMappings[i].outputCount - 1) value += ",";
            }
            preferences.putString(key.c_str(), value);
        } else {
            preferences.remove(key.c_str());
        }
    }
    preferences.end();
}

void loadPWMMappingsFromPreferences() {
    preferences.begin("pwm_mappings", true);
    for (int i = 0; i < MAX_PWM_MAPPINGS; i++) {
        String key = "map" + String(i);
        String value = preferences.getString(key.c_str(), "");
        if (value.length() > 0) {
            addPWMMapping("PMS" + value, false);
        }
    }
    preferences.end();
}

// Stability checking functions
bool checkPWMStabilityForPort(int portIdx, unsigned long currentValue) {
    PWMStabilityTracker &tracker = pwmStability[portIdx];
    
    tracker.readings[tracker.readingIndex] = currentValue;
    tracker.readingIndex = (tracker.readingIndex + 1) % 5;
    
    unsigned long minVal = tracker.readings[0];
    unsigned long maxVal = tracker.readings[0];
    
    for (int i = 1; i < 5; i++) {
        if (tracker.readings[i] < minVal) minVal = tracker.readings[i];
        if (tracker.readings[i] > maxVal) maxVal = tracker.readings[i];
    }
    
    return ((maxVal - minVal) <= PWM_STABILITY_RANGE);
}

bool shouldTransmitPWM_Port(int portIdx, unsigned long currentValue) {
    PWMStabilityTracker &tracker = pwmStability[portIdx];
    unsigned long currentTime = millis();
    
    if (currentTime - tracker.lastTransmitTime < PWM_MIN_TRANSMIT_INTERVAL) {
        return false;
    }
    
    if (tracker.lastTransmittedValue == 0) {
        tracker.lastTransmittedValue = currentValue;
        tracker.lastTransmitTime = currentTime;
        tracker.stabilized = false;
        return true;
    }
    
    unsigned long change = (currentValue > tracker.lastTransmittedValue) ? 
                          (currentValue - tracker.lastTransmittedValue) : 
                          (tracker.lastTransmittedValue - currentValue);
    
    bool significantChange = (change >= PWM_CHANGE_THRESHOLD);
    bool stable = checkPWMStabilityForPort(portIdx, currentValue);
    
    if (significantChange) {
        tracker.lastTransmittedValue = currentValue;
        tracker.lastTransmitTime = currentTime;
        tracker.stabilized = false;
        return true;
    }
    
    if (stable && !tracker.stabilized && change > 0) {
        tracker.lastTransmittedValue = currentValue;
        tracker.lastTransmitTime = currentTime;
        tracker.stabilized = true;
        return true;
    }
    
    return false;
}

void processPWMPassthrough() {
    for (int i = 0; i < MAX_PWM_MAPPINGS; i++) {
        if (!pwmMappings[i].active) continue;
        
        int portIdx = pwmMappings[i].inputPort - 1;
        if (!pwmNewData[portIdx]) continue;
        
        unsigned long pulseWidth = pwmPulseWidth[portIdx];
        pwmNewData[portIdx] = false;
        
        if (pulseWidth < 500 || pulseWidth > 2500) continue;
        
        // Check stability before transmitting
        if (!shouldTransmitPWM_Port(portIdx, pulseWidth)) {
            continue;
        }
        
        if (debugEnabled) {
            Serial.printf("PWM: Serial%d -> %lu μs\n", pwmMappings[i].inputPort, pulseWidth);
        }
        
        for (int j = 0; j < pwmMappings[i].outputCount; j++) {
            int targetWCB = pwmMappings[i].outputs[j].wcbNumber;
            int targetPort = pwmMappings[i].outputs[j].serialPort;
            
            if (targetWCB == 0 || targetWCB == WCB_Number) {
                int txPin = 0;
                switch(targetPort) {
                    case 1: txPin = SERIAL1_TX_PIN; break;
                    case 2: txPin = SERIAL2_TX_PIN; break;
                    case 3: txPin = SERIAL3_TX_PIN; break;
                    case 4: txPin = SERIAL4_TX_PIN; break;
                    case 5: txPin = SERIAL5_TX_PIN; break;
                }
                if (txPin > 0) {
                    digitalWrite(txPin, HIGH);
                    delayMicroseconds(pulseWidth);
                    digitalWrite(txPin, LOW);
                }
            } else {
                char msg[32];
                snprintf(msg, sizeof(msg), ";P%d%lu", targetPort, pulseWidth);
                sendESPNowMessage(targetWCB, msg);
            }
        }
    }
}

bool isSerialPortUsedForPWMInput(int port) {
    for (int i = 0; i < MAX_PWM_MAPPINGS; i++) {
        if (pwmMappings[i].active && pwmMappings[i].inputPort == port) {
            return true;
        }
    }
    return false;
}

void configureRemotePWMOutput(int serialPort) {
    // Check if port can be used for PWM
    if (!canUsePWMOnPort(serialPort)) {
        return;
    }
    
    int txPin = 0;
    switch(serialPort) {
        case 1: txPin = SERIAL1_TX_PIN; break;
        case 2: txPin = SERIAL2_TX_PIN; break;
        case 3: txPin = SERIAL3_TX_PIN; break;
        case 4: txPin = SERIAL4_TX_PIN; break;
        case 5: txPin = SERIAL5_TX_PIN; break;
    }
    
    if (txPin > 0) {
        pinMode(txPin, OUTPUT);
        digitalWrite(txPin, LOW);
        if (debugEnabled) {
            Serial.printf("Configured Serial%d TX pin for PWM output\n", serialPort);
        }
    }
}

bool isSerialPortPWMOutput(int port) {
    for (int i = 0; i < pwmOutputCount; i++) {
        if (pwmOutputPorts[i] == port) {
            return true;
        }
    }
    return false;
}

void addPWMOutputPort(int port) {
    if (port < 1 || port > 5) {
        Serial.println("Invalid port number. Must be 1-5");
        return;
    }
    
    // Check if port can be used for PWM
    if (!canUsePWMOnPort(port)) {
        return;
    }
    
    if (isSerialPortPWMOutput(port)) {
        Serial.printf("Serial%d already configured as PWM output\n", port);
        return;
    }
    
    if (pwmOutputCount >= MAX_PWM_OUTPUT_PORTS) {
        Serial.println("Maximum PWM output ports reached");
        return;
    }
    
    pwmOutputPorts[pwmOutputCount++] = port;
    
    configureRemotePWMOutput(port);
    
    savePWMOutputPortsToPreferences();
    Serial.printf("Serial%d configured as PWM output port\n", port);
}

void removePWMOutputPort(int port) {
    for (int i = 0; i < pwmOutputCount; i++) {
        if (pwmOutputPorts[i] == port) {
            for (int j = i; j < pwmOutputCount - 1; j++) {
                pwmOutputPorts[j] = pwmOutputPorts[j + 1];
            }
            pwmOutputPorts[--pwmOutputCount] = 0;
            savePWMOutputPortsToPreferences();
            Serial.printf("Serial%d removed from PWM output ports\n", port);
            return;
        }
    }
    Serial.printf("Serial%d was not configured as PWM output\n", port);
}

void savePWMOutputPortsToPreferences() {
    preferences.begin("pwm_outputs", false);
    preferences.putInt("count", pwmOutputCount);
    for (int i = 0; i < pwmOutputCount; i++) {
        String key = "port" + String(i);
        preferences.putInt(key.c_str(), pwmOutputPorts[i]);
    }
    preferences.end();
}

void loadPWMOutputPortsFromPreferences() {
    preferences.begin("pwm_outputs", true);
    int savedCount = preferences.getInt("count", 0);
    preferences.end();
    
    if (savedCount == 0) return;
    
    // Load and validate each port
    preferences.begin("pwm_outputs", true);
    pwmOutputCount = 0;  // Reset counter
    bool needsCleanup = false;
    
    for (int i = 0; i < savedCount; i++) {
        String key = "port" + String(i);
        int port = preferences.getInt(key.c_str(), 0);
        
        if (port > 0) {
            // Check if this port conflicts with Kyber
            if (canUsePWMOnPort(port)) {
                pwmOutputPorts[pwmOutputCount++] = port;
                configureRemotePWMOutput(port);
            } else {
                Serial.printf("⚠️  Skipping PWM output port Serial%d - conflicts with Kyber\n", port);
                needsCleanup = true;
            }
        }
    }
    preferences.end();
    
    // If we skipped any ports, clean up NVS
    if (needsCleanup) {
        Serial.println("Cleaning up conflicting PWM output ports from NVS...");
        savePWMOutputPortsToPreferences();
    }
}

