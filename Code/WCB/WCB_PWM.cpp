#include "WCB_RemoteTerm.h"  // Must be first — redirects Serial → WCBDebugSerial
#include "WCB_PWM.h"
#include "WCB_Storage.h"
#include "wcb_pin_map.h"
#include <Preferences.h>
#include <sdkconfig.h>
#if CONFIG_IDF_TARGET_ESP32
#include "hal/gpio_ll.h"   // level-emulated PWM input edges (erratum GPIO-3.14) - see pwmEdge()
#endif

extern Preferences preferences;
extern int WCB_Number;
// NO default here on purpose: WCB.ino:977 defaults useETM to true, and a second default in this
// file silently won the 2-argument calls below - clearAllPWMMappings' remote commands went out
// as plain packets that every ETM receiver drops (WCB.ino:4203-4225), while this board still
// printed success. State it at every call site instead.
extern void sendESPNowMessage(uint8_t target, const char *message, bool useETM);
extern bool debugEnabled;
extern char LocalFunctionIdentifier;
extern bool serialBroadcastEnabled[5];
extern bool blockBroadcastFrom[5];
extern void saveBroadcastSettingsToPreferences();
extern void saveBroadcastBlockSettings();
extern bool Kyber_Local;
extern bool Maestro_Remote;
extern bool debugPWMPassthrough;
extern bool isSerialPortUsedForHCR(int port);   // WCB_HCR.cpp  — serial-device port reservation
extern bool isSerialPortUsedForWLED(int port);  // WCB_WLED.cpp — serial-device port reservation
extern bool isSerialPortUsedForMaestro(int port);  // WCB_Maestro.cpp — local Maestro slots only
extern bool isSerialPortUsedForDFP(int port);      // WCB_DFP.cpp    — serial-device port reservation
// isSerialPortUsedForMP3 comes from WCB_Storage.h (included above).

PWMMapping pwmMappings[MAX_PWM_MAPPINGS];
volatile bool pwmRebootPending = false;

// PWM Stability Tracking
PWMStabilityTracker pwmStability[5] = {
    {0, {0}, 0, false, 0},
    {0, {0}, 0, false, 0},
    {0, {0}, 0, false, 0},
    {0, {0}, 0, false, 0},
    {0, {0}, 0, false, 0}
};

const unsigned long PWM_MIN_TRANSMIT_INTERVAL = 1;  // ms between transmits
const int PWM_CHANGE_THRESHOLD = 6;  // μs change to trigger transmission
const int PWM_STABILITY_RANGE = 6;   // μs range for stability

int pwmOutputPorts[MAX_PWM_OUTPUT_PORTS] = {0, 0, 0, 0, 0};
int pwmOutputCount = 0;
uint8_t pwmOutputAutoSrc[MAX_PWM_OUTPUT_PORTS] = {0, 0, 0, 0, 0};  // see WCB_PWM.h
bool pwmOutputPrevBcstOut[MAX_PWM_OUTPUT_PORTS] = {true, true, true, true, true};   // see WCB_PWM.h
bool pwmOutputPrevBlockIn[MAX_PWM_OUTPUT_PORTS] = {false, false, false, false, false};

// PWM reading variables for each port
volatile unsigned long pwmRiseTime[5] = {0};
volatile unsigned long pwmPulseWidth[5] = {0};
volatile bool pwmNewData[5] = {false};
#if CONFIG_IDF_TARGET_ESP32
static volatile uint8_t pwmLastLevel[5];   // pad level pwmEdge() last recorded per input port (level emulation)
#endif
extern bool espNowInitialized;

// Remote PWM configuration may go out once ESP-NOW is running. This used to be "uptime > 5 s", but nothing at boot
// sends remote PWM config — only commands do — so the gate only ever bit a command that arrived in the first five
// seconds after a reboot (a config push, a test): it cleared or changed the local mapping, saved it and printed
// success, while the remote board was never told. setup() registers the peers right after esp_now_init(), and an
// ETM send to a peer not yet seen online still goes out and retries.
bool canSendESPNow() {
    if (espNowInitialized) return true;
    Serial.println("⚠️  ESP-NOW is not running — remote PWM configuration was NOT sent");
    return false;
}

// Validation function to prevent PWM conflicts with Kyber
// quiet=true suppresses the refusal lines for callers that skip a reserved port as a matter of
// course - the WDP auto-config loop sees the same neighbour advert every 60 s forever, and its
// comment already claimed the skip was silent.
bool canUsePWMOnPort(int port, bool quiet) {
    // Only the port a Kyber mode owns: the Kyber's own port on Kyber LOCAL, S1 on Maestro REMOTE
    // (kyberModeReservesPort, WCB_Storage.cpp - see there for why). The other hardware port of a Kyber
    // LOCAL board is an ordinary port; reserving it too dropped a PWM output saved there from NVS at the
    // next boot (loadPWMOutputPortsFromPreferences) while ;P kept driving the pin (tracker #73 D4).
    // Every caller comes through here: ?MAP,PWM, both boot loaders and WDP PWMTARGET auto-config.
    // The REMOTE S1 text is pinned by HIL pwm.out_port_lines_and_guards / pwm.map_validation.
    if (kyberModeReservesPort(port)) {
        if (!quiet) Serial.printf("❌ Cannot use PWM on Serial%d - reserved for %s\n", port,
                                  Maestro_Remote ? "Maestro/Kyber" : "Kyber");
        return false;
    }

    // Reject ports already claimed by a serial-device module (HCR/MP3/WLED).
    // Symmetric with those modules' own guards, which reject PWM ports — so two
    // subsystems can never silently drive the same UART.
    // Maestro added 2026-09-20: a local Maestro slot owns its port just as firmly as an HCR or
    // WLED does, and ;P had no term for it at all (WCB.ino processPWMOutput).
    if (isSerialPortUsedForHCR(port) || isSerialPortUsedForMP3(port) || isSerialPortUsedForWLED(port) ||
        isSerialPortUsedForMaestro(port) || isSerialPortUsedForDFP(port)) {
        if (!quiet) Serial.printf("❌ Cannot use PWM on Serial%d - reserved for HCR/MP3/WLED/Maestro/DFP\n", port);
        return false;
    }

    return true;
}

// ISR handlers for each input port.
//
// Classic ESP32: LEVEL-emulated edges (tracker #78). ESP32 erratum GPIO-3.14 loses an EDGE interrupt on GPIO0-31
// when the GPIO ISR's STATUS / W1TC handling of another pin lands on it - measured on a WCB's soft-serial RX pins
// (HIL softrx.erratum_pairs, 2026-09-23), and a PWM input shares the same interrupt status register with them, so a
// lost edge here is a lost or doubled pulse width. The pin is armed for the level it is NOT at (attachPWMInterrupt);
// each entry re-arms the opposite level FIRST, records a rise or fall only when the level differs from the one last
// recorded (the same level is the erratum's spurious entry: no edge), then re-reads the pad and re-arms if it moved,
// so it rarely returns armed for a level the line already has; the guard exit and the tail before the IDF's
// post-handler W1TC rely on the level status re-asserting while its condition holds, which re-fires the ISR at once.
// The same scheme as the vendored EspSoftwareSerial's rxBitISR.
// The int_type write is a read-modify-write of GPIO_PINn_REG from core 1's GPIO ISR: attach/detach these pins only
// from core 1 (setup / loop command handlers) - never from PWMTask or any other core-0 task (CLAUDE.md rule 13).
// ESP32-S3: the stock CHANGE-interrupt body, unchanged (no such erratum).
static inline void IRAM_ATTR pwmEdge(int idx, int pin) {
#if CONFIG_IDF_TARGET_ESP32
    bool high = gpio_ll_get_level(&GPIO, pin);
    for (int guard = 0; ; ++guard) {
        gpio_ll_set_intr_type(&GPIO, pin, high ? GPIO_INTR_LOW_LEVEL : GPIO_INTR_HIGH_LEVEL);
        if (high != (bool)pwmLastLevel[idx]) {
            pwmLastLevel[idx] = high;
            if (high) {
                pwmRiseTime[idx] = micros();
            } else {
                pwmPulseWidth[idx] = micros() - pwmRiseTime[idx];
                pwmNewData[idx] = true;
            }
        }
        const bool now = gpio_ll_get_level(&GPIO, pin);
        if (now == high || guard >= 3) break;   // cap: a glitch train must not hold the ISR
        high = now;
    }
#else
    if (digitalRead(pin) == HIGH) {
        pwmRiseTime[idx] = micros();
    } else {
        pwmPulseWidth[idx] = micros() - pwmRiseTime[idx];
        pwmNewData[idx] = true;
    }
#endif
}

void IRAM_ATTR pwmISR1() { pwmEdge(0, SERIAL1_RX_PIN); }
void IRAM_ATTR pwmISR2() { pwmEdge(1, SERIAL2_RX_PIN); }
void IRAM_ATTR pwmISR3() { pwmEdge(2, SERIAL3_RX_PIN); }
void IRAM_ATTR pwmISR4() { pwmEdge(3, SERIAL4_RX_PIN); }
void IRAM_ATTR pwmISR5() { pwmEdge(4, SERIAL5_RX_PIN); }

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
#if CONFIG_IDF_TARGET_ESP32
    // Level-emulated edges (see pwmEdge): arm for the level the line is NOT at; the ISR flips it after every edge.
    const bool high = gpio_ll_get_level(&GPIO, pin);
    pwmLastLevel[port - 1] = high;
    attachInterrupt(digitalPinToInterrupt(pin), isr, high ? ONLOW : ONHIGH);
#else
    attachInterrupt(digitalPinToInterrupt(pin), isr, CHANGE);
#endif
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

    // Snapshot existing remote outputs so we can detect removals after the new mapping is built
    struct { int wcbNumber; int serialPort; } oldRemote[5];
    int oldRemoteCount = 0;
    if (pwmMappings[slot].active) {
        for (int i = 0; i < pwmMappings[slot].outputCount; i++) {
            if (pwmMappings[slot].outputs[i].wcbNumber != 0) {
                oldRemote[oldRemoteCount].wcbNumber  = pwmMappings[slot].outputs[i].wcbNumber;
                oldRemote[oldRemoteCount].serialPort = pwmMappings[slot].outputs[i].serialPort;
                oldRemoteCount++;
            }
        }
    }

    working = working.substring(commaPos + 1);
    // Staged, not live. This used to take a reference to the slot and zero its outputCount before
    // parsing a single destination, so an invalid re-map ("No valid outputs specified") left the slot
    // ACTIVE with zero outputs: passthrough drove nothing, ?MAP,PWM,LIST printed an input with an
    // empty output list, and ?backup emitted a bare "?MAP,PWM,S3" that cannot be replayed - while NVS
    // still held the good list, so a reboot resurrected it. Parse into a local, commit on success.
    PWMMapping cand = {};
    cand.inputPort   = inputPort;
    cand.outputCount = 0;
    cand.active      = false;
    PWMMapping &mapping = cand;
    
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
        
        // This board's own number IS local. Left as W<n>, the passthrough treated it as local
        // (so it skipped the UART init) while only wcbNumber==0 outputs got pinMode(OUTPUT) -
        // the pin never drove anything, and ?backup emitted a self-addressed token.
        if (wcbNum == WCB_Number) wcbNum = 0;
        if (serialPort >= 1 && serialPort <= 5 && wcbNum >= 0 && wcbNum <= MAX_WCB_COUNT) {
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
        return;                 // slot untouched: the previous mapping still stands
    }
    
    mapping.active = true;
    pwmMappings[slot] = cand;   // commit: everything below reads the same values either way
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

    // Send CLEAR to any remote outputs that were present before but are no longer in the new mapping
    if (oldRemoteCount > 0 && canSendESPNow()) {
        for (int i = 0; i < oldRemoteCount; i++) {
            bool stillPresent = false;
            for (int j = 0; j < mapping.outputCount; j++) {
                if (mapping.outputs[j].wcbNumber  == oldRemote[i].wcbNumber &&
                    mapping.outputs[j].serialPort == oldRemote[i].serialPort) {
                    stillPresent = true;
                    break;
                }
            }
            if (!stillPresent) {
                char clearCmd[40];
                snprintf(clearCmd, sizeof(clearCmd), "%cMAP,PWM,CLEAR,OUT,S%d",
                         LocalFunctionIdentifier, oldRemote[i].serialPort);
                sendESPNowMessage(oldRemote[i].wcbNumber, clearCmd, true);
                delay(50);
                if (debugEnabled) {
                    Serial.printf("Sent PWM output clear to WCB%d: %s\n", oldRemote[i].wcbNumber, clearCmd);
                }
            }
        }
    }

    // Serial.printf("PWM Mapping added: Serial%d -> %d output(s)\n", inputPort, mapping.outputCount);

    if (hasRemoteOutputs && canSendESPNow()) {
        for (int i = 0; i < mapping.outputCount; i++) {
            if (mapping.outputs[i].wcbNumber != 0) {
                char remoteCmd[40];
                snprintf(remoteCmd, sizeof(remoteCmd), "%cMAP,PWM,OUT,S%d",
                         LocalFunctionIdentifier, mapping.outputs[i].serialPort);
                sendESPNowMessage(mapping.outputs[i].wcbNumber, remoteCmd, true);
                delay(50);
                if (debugEnabled) {
                    Serial.printf("Sent PWM output config to WCB%d: %s\n", mapping.outputs[i].wcbNumber, remoteCmd);
                }
            }
        }
    }
    
    if (autoReboot) {
        // DEFER the restart instead of taking it here. A config push arrives as a stream of
        // commands, so rebooting inside one of them destroys the commands still queued behind
        // it — and the pusher cannot tell, because the boot banner satisfies its "did the board
        // answer" test, so no retry fires and the push is scored as fully ACKed. Setting the
        // flag lets the rest of the queue drain first; loop() restarts once it is empty.
        pwmRebootPending = true;
        Serial.println("PWM configuration stored — rebooting once the command queue drains.");
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
                    char remoteCmd[40];
                    snprintf(remoteCmd, sizeof(remoteCmd), "%cMAP,PWM,CLEAR,OUT,S%d",
                             LocalFunctionIdentifier, pwmMappings[i].outputs[j].serialPort);
                    sendESPNowMessage(pwmMappings[i].outputs[j].wcbNumber, remoteCmd, true);
                    if (debugEnabled) {
                        Serial.printf("Sent PWM output removal to WCB%d: %s\n",
                                     pwmMappings[i].outputs[j].wcbNumber, remoteCmd);
                    }
                }
            }
            
            pwmMappings[i].active = false;
            savePWMMappingsToPreferences();
            Serial.printf("Removed PWM mapping for Serial%d\n", inputPort);
            
            // Deferred, like addPWMMapping — see pwmRebootPending in WCB_PWM.h. Restarting
            // inline here would swallow every command still queued behind this one.
            pwmRebootPending = true;
            Serial.println("Mapping removed — rebooting once the command queue drains.");
            return;
        }
    }
    Serial.printf("No PWM mapping found for Serial%d\n", inputPort);
}

void listPWMMappings() {
    Serial.println("PWM Mappings:");
    
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
    

    if (pwmOutputCount > 0) {    // Show output-only ports
        Serial.println("PWM Output-Only Ports:");
        Serial.print("Configured outputs: ");
        for (int i = 0; i < pwmOutputCount; i++) {
            Serial.printf("S%d ", pwmOutputPorts[i]);
        }
        Serial.println();
    } else {
        // Serial.println("No output-only ports configured");
    }
    
    Serial.println("--------------- End PWM Mappings ---------------");
}

void listPWMMappingsBoot() {
    Serial.println("PWM Mappings:");
    
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
    if (pwmOutputCount > 0) {
        Serial.println("PWM Output-Only Ports:");
        Serial.print("Configured outputs: ");
        for (int i = 0; i < pwmOutputCount; i++) {
            Serial.printf("S%d ", pwmOutputPorts[i]);
        }
        Serial.println();
    } else {
        // Serial.println("No output-only ports configured");
    }
    
}

void clearAllPWMMappings(bool autoReboot) {
    bool remoteBoards[MAX_WCB_COUNT + 1] = {false};   // indexed by WCB number (0 = local), sized to the full peer range
    int remotePorts[MAX_WCB_COUNT + 1][5];
    int remotePortCounts[MAX_WCB_COUNT + 1] = {0};
    
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
        for (int wcb = 1; wcb <= MAX_WCB_COUNT; wcb++) {
            if (remoteBoards[wcb]) {
                for (int p = 0; p < remotePortCounts[wcb]; p++) {
                    char remoteCmd[40];
                    snprintf(remoteCmd, sizeof(remoteCmd), "%cMAP,PWM,CLEAR,OUT,S%d",
                             LocalFunctionIdentifier, remotePorts[wcb][p]);
                    sendESPNowMessage(wcb, remoteCmd, true);   // ETM: a plain packet is dropped
                    delay(50);
                    if (debugEnabled) {
                        Serial.printf("Sent PWM output removal to WCB%d: %s\n", wcb, remoteCmd);
                    }
                }
                
                // Only reboot the remote board when this is a real "clear my PWM mappings"
                // action. A local factory reset must not restart the rest of the fleet.
                if (autoReboot) {
                    delay(50);
                    char rebootCmd[16];
                    snprintf(rebootCmd, sizeof(rebootCmd), "%cREBOOT", LocalFunctionIdentifier);
                    sendESPNowMessage(wcb, rebootCmd, true);   // ETM: a plain packet is dropped
                    if (debugEnabled) {
                        Serial.printf("Sent reboot command to WCB%d\n", wcb);
                    }
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
    
    // Deferred, like addPWMMapping — see pwmRebootPending in WCB_PWM.h. Guarded on
    // autoReboot for the same reason as the remote ?REBOOT above: eraseNVSFlash() calls
    // this with false and performs its own restart afterwards.
    if (autoReboot) {
        pwmRebootPending = true;
        Serial.println("Rebooting once the command queue drains.");
    }
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
            // Parse the stored mapping directly (silent restore)
            int commaPos = value.indexOf(',');
            if (commaPos == -1) continue;
            
            int inputPort = value.substring(0, commaPos).toInt();
            if (inputPort < 1 || inputPort > 5 || !canUsePWMOnPort(inputPort)) continue;
            
            // Fill the mapping structure
            PWMMapping &mapping = pwmMappings[i];
            mapping.inputPort = inputPort;
            mapping.outputCount = 0;
            mapping.active = false;  // Set true only if we have valid outputs
            
            String outputs = value.substring(commaPos + 1);
            int startIdx = 0;
            
            while (startIdx < outputs.length() && mapping.outputCount < 5) {
                int nextComma = outputs.indexOf(',', startIdx);
                String output = (nextComma == -1) ? outputs.substring(startIdx) : outputs.substring(startIdx, nextComma);
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
                
                if (wcbNum == WCB_Number) wcbNum = 0;   // same normalization as the parse path
                if (serialPort >= 1 && serialPort <= 5 && wcbNum >= 0 && wcbNum <= MAX_WCB_COUNT) {
                    if (wcbNum == 0 && !canUsePWMOnPort(serialPort)) {
                        // Skip local outputs that conflict with Kyber
                    } else {
                        mapping.outputs[mapping.outputCount].wcbNumber = wcbNum;
                        mapping.outputs[mapping.outputCount].serialPort = serialPort;
                        mapping.outputCount++;
                    }
                }
                
                if (nextComma == -1) break;
                startIdx = nextComma + 1;
            }
            
            // Only activate if we got valid outputs
            if (mapping.outputCount > 0) {
                mapping.active = true;
                attachPWMInterrupt(inputPort);
                
                // Configure local output pins
                for (int j = 0; j < mapping.outputCount; j++) {
                    if (mapping.outputs[j].wcbNumber == 0) {
                        int txPin = 0;
                        switch(mapping.outputs[j].serialPort) {
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
                    }
                }
            }
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
        
        if (!shouldTransmitPWM_Port(portIdx, pulseWidth)) {
            continue;
        }
        
        if (debugPWMPassthrough) {
            Serial.printf("[PWM] Input S%d: %lu μs -> %d output(s)\n", 
                         pwmMappings[i].inputPort, pulseWidth, pwmMappings[i].outputCount);
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
                    if (debugPWMPassthrough) {
                        Serial.printf("[PWM] Local output -> S%d pin %d: %lu μs\n", targetPort, txPin, pulseWidth);
                    }
                }
            } else {
                char msg[32];
                snprintf(msg, sizeof(msg), ";P%d%lu", targetPort, pulseWidth);
                sendESPNowMessage(targetWCB, msg, false);  // false = bypass ETM for PWM
                if (debugPWMPassthrough) {
                    Serial.printf("[PWM] Remote output -> WCB%d S%d: %s\n", targetWCB, targetPort, msg);
                }
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
    // Check explicit output-only ports list
    for (int i = 0; i < pwmOutputCount; i++) {
        if (pwmOutputPorts[i] == port) {
            return true;
        }
    }
    
    // Check if port is used as an output in any PWM mapping
    for (int i = 0; i < MAX_PWM_MAPPINGS; i++) {
        if (pwmMappings[i].active) {
            for (int j = 0; j < pwmMappings[i].outputCount; j++) {
                // Check local outputs (wcbNumber == 0 or == WCB_Number)
                if ((pwmMappings[i].outputs[j].wcbNumber == 0 || 
                     pwmMappings[i].outputs[j].wcbNumber == WCB_Number) &&
                    pwmMappings[i].outputs[j].serialPort == port) {
                    return true;
                }
            }
        }
    }
    
    return false;
}

void addPWMOutputPort(int port, uint8_t wdpAutoSrc) {
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
        return;   // already an output — DON'T retag (a manual port stays manual so it
                  // can never be auto-removed, and the first WDP source keeps ownership)
    }

    if (pwmOutputCount >= MAX_PWM_OUTPUT_PORTS) {
        Serial.println("Maximum PWM output ports reached");
        return;
    }

    pwmOutputAutoSrc[pwmOutputCount]     = wdpAutoSrc;   // 0 = manual, >0 = WDP self-config source
    pwmOutputPrevBcstOut[pwmOutputCount] = serialBroadcastEnabled[port - 1];  // restore these on release
    pwmOutputPrevBlockIn[pwmOutputCount] = blockBroadcastFrom[port - 1];
    pwmOutputPorts[pwmOutputCount++]     = port;

    configureRemotePWMOutput(port);

    savePWMOutputPortsToPreferences();
    Serial.printf("Serial%d configured as PWM output port\n", port);
}

void reconcileWdpAutoPWMOutputs(uint8_t srcWCB, const uint8_t *wantPorts, uint8_t wantCount) {
    if (srcWCB == 0) return;
    // KNOWN LIMITATION (misconfiguration only): a port is cleared purely on the tagging
    // source dropping it. If TWO boards drive the same receiver port (a config error —
    // their ;P streams already fight one pin), and the tag-owner drops it while the other
    // still wants it, the port is briefly cleared (broadcasts re-enable on it) until the
    // other board's next advert re-adds it (<=60 s backstop). Not hardened deliberately:
    // checking "does any other neighbor still name this port" would pull the WDP neighbor
    // table into WCB_PWM, and two sources on one output port is itself the real bug to fix.
    // removePWMOutputPort() compacts the arrays, so don't blindly advance the index.
    for (int i = 0; i < pwmOutputCount; ) {
        if (pwmOutputAutoSrc[i] == srcWCB) {
            int p = pwmOutputPorts[i];
            bool wanted = false;
            for (int k = 0; k < wantCount; k++) if (wantPorts[k] == p) { wanted = true; break; }
            if (!wanted) {
                Serial.printf("[WDP] WCB%d no longer drives our S%d - clearing auto-configured PWM output\n",
                              srcWCB, p);
                removePWMOutputPort(p);   // shuffles this slot out; re-examine same index
                continue;
            }
        }
        i++;
    }
}

bool removePWMOutputPort(int port) {
    for (int i = 0; i < pwmOutputCount; i++) {
        if (pwmOutputPorts[i] == port) {
            // Read the recorded flags BEFORE the shuffle below moves them.
            const bool prevOut = pwmOutputPrevBcstOut[i];
            const bool prevIn  = pwmOutputPrevBlockIn[i];
            for (int j = i; j < pwmOutputCount - 1; j++) {
                pwmOutputPorts[j] = pwmOutputPorts[j + 1];
                pwmOutputAutoSrc[j]     = pwmOutputAutoSrc[j + 1];
                pwmOutputPrevBcstOut[j] = pwmOutputPrevBcstOut[j + 1];
                pwmOutputPrevBlockIn[j] = pwmOutputPrevBlockIn[j + 1];
            }
            pwmOutputPorts[--pwmOutputCount] = 0;
            pwmOutputAutoSrc[pwmOutputCount]     = 0;
            pwmOutputPrevBcstOut[pwmOutputCount] = true;
            pwmOutputPrevBlockIn[pwmOutputCount] = false;
            savePWMOutputPortsToPreferences();
            // Put the port's broadcast flags back the way they were when PWM claimed it. This
            // used to force ON/unblocked, which wiped a deliberate OFF and persisted the wipe -
            // and WDP self-heal reaches this path with no user action at all.
            if (port >= 1 && port <= 5) {
                serialBroadcastEnabled[port - 1] = prevOut;
                saveBroadcastSettingsToPreferences();
                blockBroadcastFrom[port - 1] = prevIn;
                saveBroadcastBlockSettings();
            }
            Serial.printf("Serial%d removed from PWM output ports; broadcasts re-enabled\n", port);
            return true;
        }
    }
    Serial.printf("Serial%d was not configured as PWM output\n", port);
    return false;   // nothing changed — callers must not reboot on a miss (WCB_PWM.h)
}

void savePWMOutputPortsToPreferences() {
    preferences.begin("pwm_outputs", false);
    preferences.putInt("count", pwmOutputCount);
    for (int i = 0; i < pwmOutputCount; i++) {
        String key = "port" + String(i);
        preferences.putInt(key.c_str(), pwmOutputPorts[i]);
        String akey = "auto" + String(i);         // WDP self-config source (0 = manual)
        preferences.putUChar(akey.c_str(), pwmOutputAutoSrc[i]);
        preferences.putBool(("pbo" + String(i)).c_str(), pwmOutputPrevBcstOut[i]);
        preferences.putBool(("pbi" + String(i)).c_str(), pwmOutputPrevBlockIn[i]);
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
        String akey = "auto" + String(i);
        uint8_t autoSrc = preferences.getUChar(akey.c_str(), 0);  // 0 for pre-tag NVS = manual

        if (port > 0) {
            // Check if this port conflicts with Kyber
            if (canUsePWMOnPort(port)) {
                pwmOutputAutoSrc[pwmOutputCount]     = autoSrc;   // keep provenance aligned to the
                pwmOutputPrevBcstOut[pwmOutputCount] = preferences.getBool(("pbo" + String(i)).c_str(), true);
                pwmOutputPrevBlockIn[pwmOutputCount] = preferences.getBool(("pbi" + String(i)).c_str(), false);
                pwmOutputPorts[pwmOutputCount++]     = port;      // compacted (Kyber-skipped) list
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

