#include "WCB_RemoteTerm.h"  // Must be first — redirects Serial → WCBDebugSerial
#include <sys/_types.h>
#include "esp_heap_caps.h"   // byte-addressable heap for the out-of-memory message
#include "WCB_Storage.h"
#include <Preferences.h>
#include "WCB_PWM.h"
#include "WCB_Maestro.h"  // For MAX_MAESTROS_PER_WCB and maestroConfigs
// Declare the external variables that are defined in the main sketch
extern bool inSequenceBody;   // WCB.ino — nested-recall mesh-fanout suppression flag
extern Preferences preferences;
extern unsigned long baudRates[5];
extern bool serialBroadcastEnabled[5];
extern char LocalFunctionIdentifier;
extern char CommandCharacter;
extern char commandDelimiter;
extern String commentDelimiter;
extern int Default_WCB_Quantity;
extern int WCB_Number;
extern bool specialPeerEnabled;
extern bool wcbPeerActive[];   // dynamic peer membership (WCB.ino): floor ∪ learned
extern uint8_t umac_oct2;
extern uint8_t umac_oct3;
extern char espnowPassword[40];
extern String storedCommands[MAX_STORED_COMMANDS];
extern int wcb_hw_version;
extern char espnowPassword[40];
extern void updatePinMap();
extern void applyLiveBaud(int port, uint32_t baud);  // defined in WCB.ino — live re-init incl. SW serial S3-S5
extern volatile bool rebootPending;                 // defined in WCB.ino — loop() takes the restart
extern bool isSerialPortUsedForHCR(int port);   // WCB_HCR.cpp  — serial-device port reservation
extern bool isSerialPortUsedForWLED(int port);  // WCB_WLED.cpp — serial-device port reservation
extern bool isSerialPortUsedForDFP(int port);   // WCB_DFP.cpp  — serial-device port reservation
extern bool isSerialPortUsedForMaestro(int port);  // WCB_Maestro.cpp — local Maestro slots only
// isSerialPortUsedForMP3 + isSerialPortPWMOutput/Input come from WCB_Storage.h / WCB_PWM.h.
extern int SERIAL1_TX_PIN;        //  // Serial 1 Tx Pin
extern int SERIAL1_RX_PIN;       //  // Serial 1 Rx Pin
extern int SERIAL2_TX_PIN;       //  // Serial 2 Tx Pin
extern int SERIAL2_RX_PIN;	     //  // Serial 2 Rx Pin
extern int SERIAL3_TX_PIN;       //  // Serial 3 Tx Pin
extern int SERIAL3_RX_PIN;       //  // Serial 3 Rx Pin
extern int SERIAL4_TX_PIN;      //  // Serial 4 Tx Pin 
extern int SERIAL4_RX_PIN;      //  // Serial 4 Rx Pin
extern int SERIAL5_TX_PIN;	     //  // Serial 5 Tx Pin
extern int SERIAL5_RX_PIN;	     //  // Serial 5 Rx Pin
extern int ONBOARD_LED;       //  // ESP32 Status NeoPixel Pin
extern int STATUS_LED_PIN;       //  // Not used on this board but defining it to match version 2.1 board

extern bool etmEnabled;
extern int etmBootHeartbeatSec;
extern int etmHeartbeatSec;
extern int etmMissedHeartbeats;
extern int etmTimeoutMs;
extern int etmCharMessageCount;
extern int etmCharDelayMs;

SerialMonitorMapping serialMonitorMappings[MAX_SERIAL_MONITOR_MAPPINGS];

KyberTarget kyberTargets[MAX_KYBER_TARGETS];
bool kyberUseTargeting = false;

bool mappingDestinationExists(SerialMonitorMapping *mapping, uint8_t wcbNum, uint8_t serialPort) {
    for (int i = 0; i < mapping->outputCount; i++) {
        if (mapping->outputs[i].wcbNumber == wcbNum && 
            mapping->outputs[i].serialPort == serialPort) {
            return true;
        }
    }
    return false;
}

// ==================== Load & Save Functions ====================

void saveHWversion(int wcb_hw_version_f){
  // Validate BEFORE writing. The old order wrote the value first, so an
  // invalid ?HW,0 (e.g. a config push from a Wizard with no HW selected)
  // clobbered a previously-saved hardware version in NVS and the board came
  // up with no pin map after the next reboot.
  if (wcb_hw_version_f != 1  && wcb_hw_version_f != 21 && wcb_hw_version_f != 23 &&
      wcb_hw_version_f != 24 && wcb_hw_version_f != 31 && wcb_hw_version_f != 32) {
    Serial.printf("No valid HW version identified (%d) — stored HW version unchanged.\n",
                  wcb_hw_version_f);
    return;
  }
  preferences.begin("hw_version", false);
  preferences.putInt("hw_version", wcb_hw_version_f);
  preferences.end();
  if (wcb_hw_version_f == 1){
    Serial.println("Saved HW Ver: 1.0 to NVS.  Reboot to take effect!");
  } else if (wcb_hw_version_f == 21){
    Serial.println("Saved HW Ver: 2.1 to NVS.  Reboot to take effect!");
  } else if (wcb_hw_version_f == 23){
    Serial.println("Saved HW Ver: 2.3 to NVS.  Reboot to take effect!");
  } else if (wcb_hw_version_f == 24){
    Serial.println("Saved HW Ver: 2.4 to NVS.  Reboot to take effect!");
  } else if (wcb_hw_version_f == 31){
    Serial.println("Saved HW Ver: 3.1 to NVS.  Reboot to take effect!");
  } else if (wcb_hw_version_f == 32){
    Serial.println("Saved HW Ver: 3.2 to NVS.  Reboot to take effect!");
  }
  // (invalid values are rejected before the NVS write above)
  // updatePinMap();
}

void loadHWversion(){
preferences.begin("hw_version", true);
wcb_hw_version =  preferences.getInt("hw_version", 0);
// Serial.printf("Pin out version %d\n", wcb_hw_version);
preferences.end();
updatePinMap();
}

void saveStatusLEDPin(int pin) {
    preferences.begin("led_config", false);
    preferences.putInt("led_pin", pin);
    preferences.end();
    Serial.printf("LED pin GPIO%d saved to NVS.\n", pin);
}

void loadStatusLEDPin() {
    preferences.begin("led_config", true);
    int savedPin = preferences.getInt("led_pin", -1);
    preferences.end();
    if (savedPin >= 0) {
        STATUS_LED_PIN = savedPin;
        Serial.printf("LED pin override from NVS: GPIO%d\n", savedPin);
    }
}

void printHWversion(){

    if (wcb_hw_version == 1){
    Serial.println("HW Version: 1.0");
  } else if (wcb_hw_version == 21){
    Serial.println("HW Version: 2.1");
  } else if (wcb_hw_version == 23){
    Serial.println("HW Version: 2.3");
  } else if (wcb_hw_version == 24){
    Serial.println("HW Version: 2.4");
  } else if (wcb_hw_version == 31){
    Serial.println("HW Version: 3.1");
  } else if (wcb_hw_version == 31){
    Serial.println("HW Version: 3.1");
  } else if (wcb_hw_version == 32){
    Serial.println("HW Version: 3.2");
  } else {
    Serial.println("SET YOUR HARDWARE VERSION BEFORE PROCEEDING!!");
  }
}

void updateBaudRate(int port, int baud) {
  // Reject out-of-range ports up front. Without this guard, a parse failure
  // upstream (e.g. ?BAUD,SX,57600 → port=0) would write a "Serial0" NVS key
  // into the serial_baud namespace, polluting storage and overlapping with
  // any future legitimate use of port 0.
  if (port < 1 || port > 5) {
    Serial.printf("Invalid serial port %d (must be 1-5)\n", port);
    return;
  }
  if (!(baud == 110 || baud == 300 || baud == 600 || baud == 1200 ||
        baud == 2400 || baud == 9600 || baud == 14400 || baud == 19200 || baud == 38400 ||
        baud == 57600 || baud == 115200 || baud == 128000 || baud == 256000)) {
    Serial.println("Invalid baud rate");
    return;
  }
  // S3-S5 have no UART: RX is EspSoftwareSerial (a GPIO ISR: level-emulated on the classic ESP32, tracker #78), TX an RMT channel
  // (WCB_SoftSerial.h). The library caps the supported speed at 115200 - 128000 and 256000 were
  // accepted, applied and confirmed on a port that cannot carry them. With RMT TX and the GPIO
  // ISR at level 3 the bench measures RX exact through 57600 (20/20 lines at 19200, 38400 and
  // 57600, three runs each) and 0/20 at 115200, so warn only for 115200.
  if (port >= 3 && baud > 115200) {
    Serial.printf("S%d is a software UART - %d baud is above the 115200 it supports. Use S1 or S2 for this device.\n", port, baud);
    return;
  }
  if (port >= 3 && baud > 57600) {
    Serial.printf("Warning: S%d is a software UART - input at %d baud is not received reliably (measured exact through 57600, none at 115200). Output is fine.\n", port, baud);
  }

  // Apply to the live port immediately — no reboot required. S1/S2 reprogram
  // the UART divisor; S3-S5 (software serial) are torn down and re-begun.
  // Previously only S1/S2 were handled here, so S3-S5 baud changes silently
  // did nothing until the next reboot reloaded NVS in setup().
  applyLiveBaud(port, (uint32_t)baud);

  // Sync the in-memory baud table. Without this, the running config and the
  // config backup (which read baudRates[]) keep reporting the OLD value until
  // the next reboot reloads NVS — so the web tool's verify-pull sees the stale
  // value, never advances its baseline, and re-pushes the same change forever.
  baudRates[port - 1] = baud;

  // Save to preferences
  preferences.begin("serial_baud", false);
  String key = String("Serial") + String(port);
  preferences.putInt(key.c_str(), baud);
  preferences.end();

  Serial.printf("Baud rate for Serial%d updated to %d\n", port, baud);
}

void loadWCBNumberFromPreferences() {
    preferences.begin("wcb_config", true);
    WCB_Number = preferences.getInt("WCB_Number", 1);
    preferences.end();
    // Serial.printf("Loaded WCB Number: %d\n", WCB_Number);
}

void saveWCBNumberToPreferences(int wcb_number_f) {
    preferences.begin("wcb_config", false);
    preferences.putInt("WCB_Number", wcb_number_f);
    preferences.end();
    WCB_Number = wcb_number_f;   // take effect immediately so MAESTRO/KYBER commands
                                  // processed in the same push use the correct WCB number;
                                  // full network re-init (peers, MAC, ETM) still requires reboot
    Serial.printf("Changed WCB Number to: %d\nPlease reboot to take full effect\n", wcb_number_f);
}

// Per-WCB friendly alias (e.g. "Body" / "Dome"). Stored in the same
// wcb_config NVS namespace as WCB_Number for simplicity. Capped at 24
// chars on save; empty string = unset (treated as no alias).
void loadWCBAlias() {
    preferences.begin("wcb_config", true);
    wcb_alias = preferences.getString("alias", "");
    preferences.end();
    if (wcb_alias.length() > 0) {
        Serial.printf("Loaded WCB alias: %s\n", wcb_alias.c_str());
    }
}

void saveWCBAlias(const String &alias) {
    String a = alias;
    a.trim();
    // Strip characters that would corrupt the backup chain on restore:
    //   ^   delimiter between chained commands
    //   ,   field separator within a command
    //   ;   command-character (would be parsed as a runtime verb)
    //   ?   local-function-identifier (would be parsed as config command)
    //   \r \n  literal newlines split the chain
    // Replace with '_' so the user's intent is still readable instead of
    // silently dropping characters.
    for (unsigned i = 0; i < a.length(); i++) {
        char c = a[i];
        if (c == '^' || c == ',' || c == ';' || c == '?' ||
            c == '\r' || c == '\n') {
            a.setCharAt(i, '_');
        }
    }
    // 24-char cap, but clamp on a UTF-8 codepoint boundary so we never split
    // a multi-byte sequence (would leave invalid UTF-8 in NVS / the banner).
    if (a.length() > 24) {
        int cut = 24;
        // back up while the byte at [cut] is a UTF-8 continuation (10xxxxxx)
        while (cut > 0 && ((uint8_t)a[cut] & 0xC0) == 0x80) cut--;
        a = a.substring(0, cut);
    }
    preferences.begin("wcb_config", false);
    preferences.putString("alias", a);
    preferences.end();
    wcb_alias = a;
    if (a.length() == 0) Serial.println("WCB alias cleared");
    else                 Serial.printf("WCB alias set to: %s\n", a.c_str());
}

// Load baud rates from preferences
void loadBaudRatesFromPreferences() {
    preferences.begin("serial_baud", true);
    for (int i = 0; i < 5; i++) {
        String key = "Serial" + String(i + 1);
        baudRates[i] = preferences.getInt(key.c_str(), baudRates[i]); // FIX: Use `.c_str()`
    }
    preferences.end();
}



// Save baud rates to preferences
void saveBaudRatesToPreferences() {
    preferences.begin("serial_baud", false);
    for (int i = 0; i < 5; i++) {
        String key = "Serial" + String(i + 1);
        preferences.putInt(key.c_str(), baudRates[i]); // FIX: Use `.c_str()`
    }
    preferences.end();
}

void printBaudRates() {
    Serial.println("Serial Baud Rates and Broadcast Settings:");
    for (int i = 0; i < 5; i++) {
        int txPin, rxPin;
        switch (i + 1) {
            case 1: txPin = SERIAL1_TX_PIN; rxPin = SERIAL1_RX_PIN; break;
            case 2: txPin = SERIAL2_TX_PIN; rxPin = SERIAL2_RX_PIN; break;
            case 3: txPin = SERIAL3_TX_PIN; rxPin = SERIAL3_RX_PIN; break;
            case 4: txPin = SERIAL4_TX_PIN; rxPin = SERIAL4_RX_PIN; break;
            case 5: txPin = SERIAL5_TX_PIN; rxPin = SERIAL5_RX_PIN; break;
            default: txPin = 0; rxPin = 0; break;
        }

        // Check if port is reserved for PWM output
        if (isSerialPortPWMOutput(i + 1)) {
            Serial.printf("  Serial%d: Reserved for PWM Output  Pins: Tx:%d Rx:%d", i + 1, txPin, rxPin);
            if (serialPortLabels[i].length() > 0) {
                Serial.printf(" (%s)", serialPortLabels[i].c_str());
            }
            Serial.println();
            continue;
        }

        // Check if port is reserved for PWM input
        if (isSerialPortUsedForPWMInput(i + 1)) {
            Serial.printf("  Serial%d: Reserved for PWM Input  Pins: Tx:%d Rx:%d", i + 1, txPin, rxPin);
            if (serialPortLabels[i].length() > 0) {
                Serial.printf(" (%s)", serialPortLabels[i].c_str());
            }
            Serial.println();
            continue;
        }

        // Normal serial port
        String inputStatus  = blockBroadcastFrom[i]    ? "Disabled" : "Enabled";
        String outputStatus = serialBroadcastEnabled[i] ? "Enabled"  : "Disabled";

        Serial.printf("  Serial%d Baud: %lu, Broadcast Input: %s, Broadcast Output: %s  Pins: Tx:%d Rx:%d",
                      i + 1, baudRates[i], inputStatus.c_str(), outputStatus.c_str(), txPin, rxPin);

        // Label: user-defined takes priority
        if (serialPortLabels[i].length() > 0) {
            Serial.printf(" (%s)", serialPortLabels[i].c_str());
        }
        // No user label: name what owns the port. These were hard-wired to S1 = Maestro / S2 = Kyber, so
        // with the Kyber on S1 the S2 row said "(Kyber)" (tracker #73 D8).
        else if (Kyber_Local && (i + 1) == kyberLocalPort) {
            Serial.print(" (Kyber)");
        }
        else if (isSerialPortUsedForMaestro(i + 1)) {   // local slots only
            Serial.print(" (Maestro)");
        }

        Serial.println();
    }
}

void saveBroadcastSettingsToPreferences() {
    preferences.begin("bdcst_set", false);
    for (int i = 0; i < 5; i++) {
        String key = "S" + String(i + 1);
        preferences.putInt(key.c_str(), serialBroadcastEnabled[i] ? 1 : 0);
    }
    preferences.putInt("S0", broadcastToS0 ? 1 : 0);   // S0/USB broadcast output (opt-in)
    preferences.end();
}

void loadBroadcastSettingsFromPreferences() {
    preferences.begin("bdcst_set", true);
    for (int i = 0; i < 5; i++) {
        String key = "S" + String(i + 1);
        int value = preferences.getInt(key.c_str(), 1);  // default = 1 (enabled)
        serialBroadcastEnabled[i] = (value == 1);
    }
    broadcastToS0 = (preferences.getInt("S0", 0) == 1);   // S0/USB output defaults OFF
    preferences.end();
}

void resetBroadcastSettingsNamespace() {
    Serial.println("Clearing broadcast_settings namespace...");
    preferences.begin("bdcst_set", false);
    preferences.clear();
    preferences.end();
    
    delay(100);
    
    Serial.println("Recreating broadcast_settings with defaults...");
    preferences.begin("bdcst_set", false);
    for (int i = 0; i < 5; i++) {
        String key = "S" + String(i + 1);
        int result = preferences.putInt(key.c_str(), true);
        Serial.printf("  Created %s = true (result: %s)\n", 
                      key.c_str(), 
                      result ? "SUCCESS" : "FAILED");
    }
    preferences.end();

    // Bring the LIVE globals back in step with what was just written. Without this the RAM copies
    // kept the pre-reset values, so ?BCAST,RESET appeared to work while the very next unrelated
    // save (saveBroadcastSettingsToPreferences, called from ?MAESTRO,CLEAR,ALL among others)
    // re-persisted the old settings straight back over the defaults.
    for (int i = 0; i < 5; i++) {
        serialBroadcastEnabled[i] = true;
        blockBroadcastFrom[i]     = false;   // input blocking was never reset at all
    }
    broadcastToS0 = false;                   // matches the load default at :348

    // RESET means every port: S1-S5 all come back open, device-owned ports included (Greg's call,
    // 2026-09-21). A port an HCR / MP3 / DFPlayer / WLED / Maestro / PWM / Kyber owns will now
    // receive broadcasts and feed its device's output to the command parser until its flags are
    // set again - re-issue that device's config (or ?BCAST,OUT/IN,Sx,OFF) to put them back.

    // Persist BOTH namespaces. Only bdcst_set was written before, so the cleared input blocking
    // lived in RAM alone and ?config (or the next reboot) reloaded bcast_block over the top.
    saveBroadcastSettingsToPreferences();
    saveBroadcastBlockSettings();

    Serial.println("Done. Broadcast output re-enabled and input blocking cleared on S1-S5 (device ports included), S0 echo off.");
}

// Load MAC address preferences
void loadMACPreferences() {
    preferences.begin("mac_config", true);
    umac_oct2 = preferences.getUChar("umac_oct2", 0x00);
    umac_oct3 = preferences.getUChar("umac_oct3", 0x00);
    preferences.end();
}

// Save MAC address preferences
void saveMACPreferences() {
    preferences.begin("mac_config", false);
    preferences.putUChar("umac_oct2", umac_oct2);
    preferences.putUChar("umac_oct3", umac_oct3);
    preferences.end();
}

// Load the WCB quantity from preferences
void loadWCBQuantitiesFromPreferences() {
    preferences.begin("wcb_config", true);
    Default_WCB_Quantity = preferences.getInt("wcb_quantity", Default_WCB_Quantity);
    preferences.end();
}

// Load special peer (ID 20) enabled state from preferences
void loadSpecialPeerPreferences() {
    preferences.begin("wcb_config", true);
    specialPeerEnabled = preferences.getBool("special_peer", false);
    preferences.end();
}

// Save special peer (ID 20) enabled state to preferences.
// Takes effect after reboot (peer registration runs during setup).
void saveSpecialPeerPreferences(bool enabled) {
    preferences.begin("wcb_config", false);
    preferences.putBool("special_peer", enabled);
    preferences.end();
    specialPeerEnabled = enabled;
    Serial.printf("Controller peer (ID %d) %s.\n",
                  WCB_SPECIAL_PEER_ID, enabled ? "ENABLED" : "DISABLED");
}

// Load the special peer ID from preferences (default 20).
void loadSpecialPeerIDFromPreferences() {
    preferences.begin("wcb_config", true);
    WCB_SPECIAL_PEER_ID = preferences.getUChar("special_peer_id", 20);
    preferences.end();
}

// Save the special peer ID to preferences. Takes effect after reboot
// (peer registration runs during setup).
void saveSpecialPeerIDToPreferences(uint8_t id) {
    if (id < 1 || id > 20) {
        Serial.printf("Invalid special peer ID %d. Valid range: 1-20.\n", id);
        return;
    }
    preferences.begin("wcb_config", false);
    preferences.putUChar("special_peer_id", id);
    preferences.end();
    WCB_SPECIAL_PEER_ID = id;
    Serial.printf("Controller peer ID set to %d.\n", id);
}

// Save the WCB quantity to preferences
void saveWCBQuantityPreferences(int quantity) {
    if (quantity < 1 || quantity > MAX_WCB_COUNT) {
        Serial.printf("Invalid WCB quantity %d. Valid range: 1-%d.\n", quantity, MAX_WCB_COUNT);
        return;
    }
    preferences.begin("wcb_config", false);
    preferences.putInt("wcb_quantity", quantity);
    preferences.end();
    Default_WCB_Quantity = quantity;
    // The ?WCBQ callers reconcile peer registrations live (rebuildActivePeers +
    // syncActivePeerRegistrations) right after this, so no reboot is required.
    Serial.printf("Saved WCB quantity: %d. Peer registrations reconciled live (no reboot needed).\n", Default_WCB_Quantity);
}

// Load the ESP-NOW mesh channel from preferences (default WCB_MESH_CHANNEL_DEFAULT).
// A stale or out-of-range value falls back to the default so a corrupt NVS blob
// can't strand the board off-channel.
void loadMeshChannelFromPreferences() {
    preferences.begin("wcb_config", true);
    meshChannel = preferences.getUChar("mesh_channel", WCB_MESH_CHANNEL_DEFAULT);
    preferences.end();
    if (meshChannel < 1 || meshChannel > 11) meshChannel = WCB_MESH_CHANNEL_DEFAULT;
}

// Save the ESP-NOW mesh channel to preferences. Applied on the NEXT REBOOT, NOT
// live: switching the radio immediately would drop this board off the mesh mid-
// configuration — fatal when the ?WCBCH arrived over ESP-NOW (Wizard relay / client
// unicast), because the sender stays on the old channel and can no longer reach
// this board to confirm or retry. Deferring keeps the whole fleet reachable on the
// old channel until a coordinated reboot moves everyone at once. The boot path
// (loadMeshChannelFromPreferences + esp_wifi_set_channel) applies it.
void saveMeshChannelToPreferences(uint8_t channel) {
    if (channel < 1 || channel > 11) {
        Serial.printf("Invalid mesh channel %d. Valid range: 1-11.\n", channel);
        return;
    }
    preferences.begin("wcb_config", false);
    preferences.putUChar("mesh_channel", channel);
    preferences.end();
    meshChannel = channel;
    Serial.printf("Mesh channel saved as %d — reboot to apply. Move ALL WCBs + clients "
                  "to this channel together (one radio = one channel).\n", meshChannel);
}

// Load ESP-NOW password from preferences
void loadESPNowPasswordFromPreferences() {
    preferences.begin("espnow_config", true); // Open in read mode

    // Use the external `espnowPassword` as the default value
    preferences.getString("password", espnowPassword, sizeof(espnowPassword));

    preferences.end();
    Serial.printf("ESP-NOW Password: %s\n", espnowPassword);
}



// Save ESP-NOW password to preferences
void setESPNowPassword(const char* newPassword) {
    preferences.begin("espnow_config", false);
    preferences.putString("password", newPassword);
    preferences.end();
    strncpy(espnowPassword, newPassword, sizeof(espnowPassword) - 1);
    espnowPassword[sizeof(espnowPassword) - 1] = '\0';
}

// Load function identifiers from preferences
void loadLocalFunctionIdentifierAndCommandCharacter() {
    preferences.begin("command_config", true);
    String localFunc = preferences.getString("LocalFunc", String(LocalFunctionIdentifier));
    String cmdChar   = preferences.getString("CmdChar", String(CommandCharacter));

    if (localFunc.length() > 0) {
        LocalFunctionIdentifier = localFunc.charAt(0);
    }
    if (cmdChar.length() > 0) {
        CommandCharacter = cmdChar.charAt(0);
    }
    preferences.end();
}

// Save function identifiers to preferences
void saveLocalFunctionIdentifierAndCommandCharacter() {
    preferences.begin("command_config", false);
    preferences.putString("LocalFunc", String(LocalFunctionIdentifier));
    preferences.putString("CmdChar", String(CommandCharacter));
    preferences.end();
}

// Load command delimiter from preferences
void loadCommandDelimiter() {
    preferences.begin("command_config", true);
    String delim = preferences.getString("delimiter", String(commandDelimiter));
    if (delim.length() > 0) {
        commandDelimiter = delim.charAt(0);
    }
    preferences.end();
}

// Save command delimiter to preferences
void setCommandDelimiter(char c) {
    preferences.begin("command_config", false);
    preferences.putString("delimiter", String(c));
    preferences.end();
    commandDelimiter = c;
}

// // Load stored commands from preferences
// void loadStoredCommandsFromPreferences() {
//     preferences.begin("stored_commands", true);
//     for (int i = 0; i < MAX_STORED_COMMANDS; i++) {
//         char storedCommandBuffer[1800] = {0};
//         String key = "CMD" + String(i + 1);
//         preferences.getString(key.c_str(), storedCommandBuffer, sizeof(storedCommandBuffer));
//         storedCommands[i] = String(storedCommandBuffer);
//     }
//     preferences.end();

// }
// FNV-1a 32 of the EXACT key bytes - a sequence lineage entry (SeqPath, WCB_Storage.h). Case-sensitive like the
// NVS lookup below; a collision can only make the cycle guard over-refuse, never miss a cycle.
uint32_t seqKeyHash(const String &key) {
    uint32_t h = 2166136261u;
    for (unsigned i = 0; i < key.length(); i++) { h ^= (uint8_t)key[i]; h *= 16777619u; }
    return h;
}

// Recall a Stored Command
void recallCommandSlot(const String &key, int sourceID) {
    if (key.length() > SEQ_KEY_MAX_LEN) {   // never stored; NVS would match the shorter key's first 15 characters
        Serial.printf("No command stored under key: '%s'\n", key.c_str());
        return;
    }
    preferences.begin("stored_cmds", true);
    String recalledCommand = preferences.getString(key.c_str(), "");
    preferences.end();

    if (recalledCommand.isEmpty()) {
        Serial.printf("No command stored under key: '%s'\n", key.c_str());
        return;
    }

    // "Recalling command for key ..." is printed below, once the queue reserve has accepted the expansion: a refused
    // nested recall must print ONE short line, not echo its whole (up to 2000-character) body to UART0 as well.

    // Strip inline comments (everything from commentDelimiter onward in each
    // delimiter-separated part) so stored annotations are not executed.
    String stripped = "";
    String remaining = recalledCommand;
    while (remaining.length() > 0) {
        int delimPos = remaining.indexOf(commandDelimiter);
        String part;
        if (delimPos == -1) {
            part = remaining;
            remaining = "";
        } else {
            part = remaining.substring(0, delimPos);
            remaining = remaining.substring(delimPos + 1);
        }
        int commentPos = part.indexOf(commentDelimiter);
        if (commentPos >= 0) part = part.substring(0, commentPos);
        part.trim();
        if (part.length() > 0) {
            if (stripped.length() > 0) stripped += commandDelimiter;
            stripped += part;
        }
    }

    if (stripped.isEmpty()) {
        Serial.printf("Recalling command for key '%s': %s\n", key.c_str(), recalledCommand.c_str());
        Serial.println("Sequence contained only comments — nothing to execute.");
        return;
    }

    // A recalled body may itself be a ?SEQ,SAVE / ?CS / ?MGMT chain whose value holds ';t'.
    const bool viaTimer = isTimerCommand(stripped) && !chainCarriesValueVerb(stripped);
    const bool onLoop   = seqOnLoopTask();

    // A NESTED recall (a ;C inside a running body) must not partly fill the command queue. Reusing a sub-sequence
    // back-to-back expands every call before any leaf drains (FIFO), and a full queue then printed one "Command
    // queue is full! Discarding command." line per dropped token on UART0 - which has no TX buffer - stalling
    // loop() for tens of seconds, dropping console commands (?reboot included) and silently cutting bodies short
    // (tracker #47). Refuse the whole expansion, once, instead: a refused ;C enqueues nothing, so the cascade
    // shrinks the queue rather than growing it. The token count is an upper bound (IF gating and value verbs only
    // make the real count smaller). Top-level recalls (depth 0, incl. ONFIN/ONERR) and timer-chain bodies, which
    // enqueue one group at a time, are unchanged.
    if (onLoop && seqCurPath.depth > 0 && !viaTimer) {
        unsigned need = 1;
        for (unsigned i = 0; i < stripped.length(); i++) if (stripped[i] == commandDelimiter) need++;
        if (commandQueueSpaces() < need + SEQ_QUEUE_RESERVE) {
            Serial.printf("Command queue nearly full — not expanding nested sequence '%s'. "
                          "Space repeated calls with ;T.\n", key.c_str());
            return;
        }
    }

    Serial.printf("Recalling command for key '%s': %s\n", key.c_str(), recalledCommand.c_str());

    // Enqueue for execution.
    //
    // A stored sequence's commands are authored locally on THIS board and are
    // meant to fan out to peers. They must NOT inherit the "received via ESP-NOW"
    // flag from the trigger: when a PEER triggers the recall, that flag is set
    // (loop-prevention), and without this every command in the sequence that
    // needs to go back out over ESP-NOW to the other WCBs would be silently
    // suppressed. Force local origin for the enqueue/parse; the per-item snapshot
    // in enqueueCommand (and commandGroupsEspnowOrigin for timer sequences)
    // carries it through to dispatch. Restore afterward so the rest of the queue
    // drain is unaffected.
    // Also mark these as INSIDE a sequence body: a nested `;C`/`;SEQ` recall in this body
    // must run locally only, NOT re-fan the trigger out to the mesh (see recallStoredCommand).
    // Carried per-item exactly like the origin flag above.
    bool _savedEspNowOrigin = lastReceivedViaESPNOW;
    bool _savedSeqBody       = inSequenceBody;
    lastReceivedViaESPNOW = false;
    inSequenceBody        = true;
    // Push this key onto the lineage the body's items (and its timer chain) carry; recallStoredCommand has already
    // refused a repeat or depth 8 (tracker #47). An MP3/DFPlayer ONFIN/ONERR recall lands here directly, from
    // loop() outside any dispatch, so it starts a fresh lineage. Off the loop task there is no lineage to push.
    SeqPath _savedPath = {};
    if (onLoop) {
        _savedPath = seqCurPath;
        if (seqCurPath.depth < SEQ_MAX_NESTING) seqCurPath.key[seqCurPath.depth++] = seqKeyHash(key);
    }
    if (viaTimer) {
        parseCommandGroups(stripped);
    } else {
        parseCommandsAndEnqueue(stripped, sourceID);
    }
    lastReceivedViaESPNOW = _savedEspNowOrigin;
    inSequenceBody        = _savedSeqBody;
    if (onLoop) seqCurPath = _savedPath;
}

// Save stored commands to preferences
void saveStoredCommandsToPreferences(const String &message) {
  int commaIndex = message.indexOf(',');
  if (commaIndex == -1 || commaIndex == 0) {
    Serial.println("Invalid format. Use ?Ckey,value");
    return;
  }

  String key = message.substring(0, commaIndex);
  String value = message.substring(commaIndex + 1);
  // An Arduino String whose allocation fails comes back EMPTY, not as an error - so a long value
  // on a busy heap fell through to 'Key or value cannot be empty', naming the wrong problem.
  if (value.length() == 0 && message.length() > (unsigned)commaIndex + 1) {
    Serial.printf("Out of memory: could not copy a %u-character value (largest free block %u bytes). Not stored.\n",
                  (unsigned)(message.length() - commaIndex - 1),
                  (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT));   // not ESP.getMaxAllocHeap(): see ?STATS
    return;
  }
  key.trim();
  value.trim();

  if (key.length() == 0 || value.length() == 0) {
    Serial.println("Key or value cannot be empty.");
    return;
  }

  // An ESP32 NVS key is capped at 15 characters. A longer one makes putString fail while the name
  // was still appended to key_list below — so the sequence was listed, reported as "Stored:", and
  // advertised to peers, but recalling it found nothing. Both shipping clients already enforce 15
  // (the Wizard's maxlength and WCB_Client's wcbSeqKeyValid); this closes the hand-typed path.
  if (key.length() > SEQ_KEY_MAX_LEN) {
    Serial.printf("Sequence key '%s' is %u characters — the limit is 15. Not stored.\n",
                  key.c_str(), (unsigned)key.length());
    return;
  }

  preferences.begin("stored_cmds", false);
  if (!preferences.putString(key.c_str(), value)) {
    preferences.end();
    Serial.printf("Failed to store sequence '%s' (NVS write rejected). Not added to the list.\n",
                  key.c_str());
    return;
  }

  String existingKeys = preferences.getString("key_list", "");
  bool alreadyExists = false;

  // Check against variations to catch duplicates
  if (existingKeys == key + "," ||
      existingKeys.startsWith(key + ",") ||
      existingKeys.endsWith("," + key + ",") ||
      existingKeys.indexOf("," + key + ",") != -1) {
    alreadyExists = true;
  }

  if (!alreadyExists) {
    existingKeys += key + ",";
    preferences.putString("key_list", existingKeys);
  }

  preferences.end();
  invalidateSequenceInventoryHash();   // WDP re-advertises the new fingerprint within ~500 ms
  Serial.printf("Stored: Key='%s', Value='%s'\n", key.c_str(), value.c_str());
}


void eraseStoredCommandByName(const String &name) {
    if (name.length() == 0) {
        Serial.println("Command name cannot be empty.");
        return;
    }
    preferences.begin("stored_cmds", false);

    // Step 1: Remove the key from NVS — but never by a key longer than SEQ_KEY_MAX_LEN. No such key can be stored,
    // and NVS compares only its first 15 characters, so the remove would delete the shorter key's sequence. The
    // key_list rewrite below still runs: firmware before d83042e listed an over-long key it had failed to store, and
    // erasing by that name is the only way to drop the phantom from the list.
    bool removed = (name.length() <= SEQ_KEY_MAX_LEN) && preferences.remove(name.c_str());

    // Step 2: Retrieve and split key_list safely
    String existingKeys = preferences.getString("key_list", "");
    existingKeys.trim();

    // Split into an array
    std::vector<String> keys;
    int start = 0;
    while (start < existingKeys.length()) {
        int comma = existingKeys.indexOf(',', start);
        if (comma == -1) break;
        String k = existingKeys.substring(start, comma);
        if (k != name) {
            keys.push_back(k);  // only keep keys not matching `name`
        }
        start = comma + 1;
    }

    // Rebuild the key_list
    String updatedList = "";
    for (const auto& k : keys) {
        updatedList += k + ",";
    }

    preferences.putString("key_list", updatedList);
    preferences.end();
    invalidateSequenceInventoryHash();

    if (removed) {
        Serial.printf("Deleted stored command key: '%s'\n", name.c_str());
    } else {
        Serial.printf("No stored value found for key: '%s', but removed from list if present.\n", name.c_str());
    }
}



void listStoredCommands() {
    preferences.begin("stored_cmds", true); // Open in read mode
    String keyList = preferences.getString("key_list", ""); // Retrieve stored keys
    preferences.end();

    if (keyList.length() == 0) {
        Serial.println("No stored commands.");
        return;
    }

    Serial.println("\n--- Stored Commands ---");
    int startIdx = 0;
while (startIdx < keyList.length()) {
    int commaIndex = keyList.indexOf(',', startIdx);
    if (commaIndex == -1) commaIndex = keyList.length();

    String key = keyList.substring(startIdx, commaIndex);
    key.trim();

    if (key.length() > 0) {
        preferences.begin("stored_cmds", true);
        String value = preferences.getString(key.c_str(), "");
        preferences.end();
        Serial.printf("Key: '%s' -> Value: '%s'\n", key.c_str(), value.c_str());
    }

    startIdx = commaIndex + 1;
}


    Serial.println("--- End of Stored Commands ---");
}

// ── Stored-sequence inventory (names only) ─────────────────────────────────
// See WCB_Storage.h for the format and why names-only exists as a separate path
// from ?SEQ,LIST / the config pull.

// Cached inventory hash. Recomputed lazily, and ONLY after a write — the WDP
// dirty-check rebuilds the whole advert payload twice a second (WCB_WDP.cpp), and
// hashing values there uncached would mean N NVS reads at 2 Hz forever.
static uint32_t seqInvHashCache  = 0;
static bool     seqInvHashValid  = false;

void invalidateSequenceInventoryHash() { seqInvHashValid = false; }

uint32_t sequenceInventoryHash() {
    if (seqInvHashValid) return seqInvHashCache;

    preferences.begin("stored_cmds", true);
    String keyList = preferences.getString("key_list", "");
    preferences.end();

    uint32_t h = 2166136261u;                     // FNV-1a 32-bit offset basis
    auto feed = [&h](const String &s) {
        for (unsigned i = 0; i < s.length(); i++) { h ^= (uint8_t)s[i]; h *= 16777619u; }
        h ^= 0xFF; h *= 16777619u;                // record separator — "ab"+"c" != "a"+"bc"
    };

    feed(keyList);

    // Hash the VALUES too, not just the names. Editing a sequence in place doesn't
    // touch key_list, so a keys-only hash would leave every peer believing its
    // cached copy was still current — the exact failure this fingerprint exists to
    // prevent. Walked in key_list order so the result is stable.
    int startIdx = 0;
    while (startIdx < (int)keyList.length()) {
        int commaIndex = keyList.indexOf(',', startIdx);
        if (commaIndex == -1) commaIndex = keyList.length();
        String key = keyList.substring(startIdx, commaIndex);
        key.trim();
        if (key.length() > 0) {
            preferences.begin("stored_cmds", true);
            String value = preferences.getString(key.c_str(), "");
            preferences.end();
            feed(value);
        }
        startIdx = commaIndex + 1;
    }

    seqInvHashCache = h;
    seqInvHashValid = true;
    return h;
}

String buildSequenceNamesString() {
    preferences.begin("stored_cmds", true);
    String keyList = preferences.getString("key_list", "");
    preferences.end();

    // Same cached hash the WDP_TLV_SEQHASH advert carries — a consumer compares
    // the two directly, so they must never be computed two different ways.
    uint32_t h = sequenceInventoryHash();

    // Walk key_list exactly as listStoredCommands does — trailing comma, possible
    // empty entries from a legacy erase — but emit only the names.
    String names = "";
    int    count = 0;
    int    startIdx = 0;
    while (startIdx < (int)keyList.length()) {
        int commaIndex = keyList.indexOf(',', startIdx);
        if (commaIndex == -1) commaIndex = keyList.length();

        String key = keyList.substring(startIdx, commaIndex);
        key.trim();
        if (key.length() > 0) {
            names += ",";
            names += key;
            count++;
        }
        startIdx = commaIndex + 1;
    }

    char hdr[16];
    snprintf(hdr, sizeof(hdr), "%08X,%d", h, count);
    return String(hdr) + names;
}

// Clear all stored commands
void clearAllStoredCommands() {
  preferences.begin("stored_cmds", false);
    preferences.clear();
    // clear() also removes seq_mig_done, which lives in THIS namespace — so without re-setting it
    // the legacy migration re-armed and re-imported every CMD1..CMD80 from "stored_commands" on
    // the next boot, resurrecting the sequences the user just deleted. Re-stamp it: this board has
    // already migrated, and deleting sequences is not a request to import the old ones back.
    preferences.putBool("seq_mig_done", true);
    preferences.end();
    invalidateSequenceInventoryHash();
}

// Normalise legacy ^*** inline-comment markers in a stored sequence value.
//
// DroidNet / 5.0 convention:
//   ^***text   — inline comment, appended to the preceding command's display line
//   ^^***text  — standalone comment, displayed on its own line
//
// In the current firmware the delimiter before *** is redundant because the recall
// path already splits on ^ and then strips anything from *** onward in each part.
// Stripping the leading ^ from ^*** turns it into a true inline comment stored on
// the same chunk as the preceding command, matching how the wizard now displays and
// saves sequences.  ^^*** (standalone) is left untouched.
static String normaliseSeqComments(const String &value) {
    char   d   = commandDelimiter;          // typically '^'
    String dl  = String(d);                 // "^"
    String ddl = dl + dl;                   // "^^"
    String cmt = "***";

    // Use a non-printable placeholder to protect ^^*** during the replacement.
    String placeholder = "\x01***";

    String result = value;
    result.replace(ddl + cmt, placeholder);  // protect ^^***
    result.replace(dl  + cmt, cmt);          // ^***  →  ***
    result.replace(placeholder, ddl + cmt);  // restore ^^***
    return result;
}

// One-time migration: recover sequences stored in legacy formats and normalise
// legacy ^*** comment markers.
// Covers two cases:
//   1. "stored_commands" namespace  — CMD1..CMDn keys from the oldest firmware
//   2. "stored_cmds" namespace      — CMD1..CMDn keys written without a key_list entry
//      (firmware versions that used stored_cmds but hadn't yet added key_list tracking)
// Runs once per board; a migration flag is stored in "stored_cmds" so it is skipped
// on every subsequent boot.
void migrateOldStoredCommands() {
    // Skip if already done
    preferences.begin("stored_cmds", true);
    bool already = preferences.getBool("seq_mig_done", false);
    String keyList = preferences.getString("key_list", "");
    preferences.end();
    if (already) return;

    int recovered = 0;

    // ── Case 1: old "stored_commands" namespace (CMD1..CMDn) ──────────────
    for (int i = 1; i <= MAX_STORED_COMMANDS; i++) {
        String key = "CMD" + String(i);
        preferences.begin("stored_commands", true);
        String value = preferences.getString(key.c_str(), "");
        preferences.end();
        if (value.length() == 0) continue;

        value = normaliseSeqComments(value);
        // Register in the current store (saveStoredCommandsToPreferences handles
        // duplicate-key checks against key_list automatically)
        saveStoredCommandsToPreferences(key + "," + value);
        recovered++;
        Serial.printf("[MIGRATION] Recovered from 'stored_commands': key='%s'\n", key.c_str());
    }

    // ── Case 2: "stored_cmds" namespace, CMD# keys missing from key_list ──
    for (int i = 1; i <= MAX_STORED_COMMANDS; i++) {
        String key = "CMD" + String(i);

        // Check if already in key_list (re-read it in case Case 1 added entries)
        preferences.begin("stored_cmds", true);
        keyList = preferences.getString("key_list", "");
        String value = preferences.getString(key.c_str(), "");
        preferences.end();

        if (value.length() == 0) continue;

        bool inList = (keyList == key + "," ||
                       keyList.startsWith(key + ",") ||
                       keyList.endsWith("," + key + ",") ||
                       keyList.indexOf("," + key + ",") != -1);
        if (inList) continue;

        value = normaliseSeqComments(value);
        saveStoredCommandsToPreferences(key + "," + value);
        recovered++;
        Serial.printf("[MIGRATION] Recovered orphaned key in 'stored_cmds': key='%s'\n", key.c_str());
    }

    // Mark done so this never runs again
    preferences.begin("stored_cmds", false);
    preferences.putBool("seq_mig_done", true);
    preferences.end();
    // Migration rewrites values (comment-marker normalisation), so the fingerprint
    // must not survive it. Runs before the first advert anyway — this is belt-and-
    // braces against a future reorder of setup().
    invalidateSequenceInventoryHash();

    if (recovered > 0) {
        Serial.printf("[MIGRATION] Sequence recovery complete — %d sequence(s) restored.\n", recovered);
    } else {
        Serial.println("[MIGRATION] No legacy sequences found — nothing to recover.");
    }
}

// Erase all NVS preferences
void eraseNVSFlash() {
    preferences.begin("serial_baud", false);
    preferences.clear();
    preferences.end();

    preferences.begin("bdcst_set", false);
    preferences.clear();
    preferences.end();

    preferences.begin("mac_config", false);
    preferences.clear();
    preferences.end();

    preferences.begin("wcb_config", false);
    preferences.clear();
    preferences.end();

    preferences.begin("espnow_config", false);
    preferences.clear();
    preferences.end();

    preferences.begin("command_config", false);
    preferences.clear();
    preferences.end();

    preferences.begin("stored_cmds", false);
    preferences.clear();
    preferences.end();

    preferences.begin("kyber_settings", false);
    preferences.clear();
    preferences.end();

    preferences.begin("kyber_targets", false);
    preferences.clear();
    preferences.end();

    preferences.begin("hw_version", false);
    preferences.clear();
    preferences.end();

    preferences.begin("serial_labels", false);
    preferences.clear();
    preferences.end();

    preferences.begin("serial_map", false); 
    preferences.clear();
    preferences.end();

    preferences.begin("serial_monitor", false); 
    preferences.clear();
    preferences.end();

    preferences.begin("bcast_block", false);
    preferences.clear();
    preferences.end();

    preferences.begin("maestro_cfg", false);
    preferences.clear();
    preferences.end();

    preferences.begin("etm_config", false);
    preferences.clear();
    preferences.end();

    preferences.begin("mp3_cfg", false);
    preferences.clear();
    preferences.end();

    preferences.begin("led_config", false);
    preferences.clear();
    preferences.end();

    preferences.begin("wdp_cfg", false);
    preferences.clear();
    preferences.end();

    preferences.begin("learned_peers", false);
    preferences.clear();
    preferences.end();

    // Previously MISSED by factory reset — HCR, WLED, and user-variable configs
    // survived an erase and re-seized their ports / reappeared after reboot.
    preferences.begin("hcr_cfg", false);   preferences.clear(); preferences.end();
    preferences.begin("wled_cfg", false);  preferences.clear(); preferences.end();
    preferences.begin("wcb_vars", false);  preferences.clear(); preferences.end();
    // ...and DFPlayer, missed the same way: a configured DFP re-claimed its UART after the
    // erase (isSerialPortUsedForDFP makes processIncomingSerial skip that port), so a board
    // the user believed was blank silently ignored commands on it.
    preferences.begin("dfp_cfg", false);   preferences.clear(); preferences.end();
    // The LEGACY sequence namespace. "stored_cmds" (cleared above) holds the seq_mig_done flag,
    // so clearing that alone re-armed migrateOldStoredCommands() while leaving the old CMD1..CMD80
    // values here intact — every deleted sequence reappeared on the next boot.
    preferences.begin("stored_commands", false); preferences.clear(); preferences.end();

    // false = do not restart this board and do not broadcast ?REBOOT. clearAllPWMMappings()
    // ended in an inline ESP.restart(), so eraseNVSFlash() never reached its own confirmation
    // or restart below — and it rebooted every other WCB in the fleet as a side effect of one
    // board being erased. Both are now behind autoReboot, and the local restart is deferred to
    // loop() via pwmRebootPending rather than taken inside the command.
    clearAllPWMMappings(false);

    // hw_version is cleared above, which is deliberate and documented (?HELP,ERASE and the
    // Wizard's factory-reset modal both say so). Say what to do about it: until ?HW is set the
    // board comes up on the hw 0 pin map, where no serial port is usable.
    Serial.println("NVS cleared — set ?HW,xx before use (serial ports are inactive until then).");
    // Deferred like every other restart (CLAUDE.md rule 11): ?ERASE,NVS can arrive mid-chain, and
    // an inline restart drops whatever was queued behind it while the sender counts it delivered.
    Serial.println("Restart queued - restarting once the command queue is quiet");
    rebootPending = true;
}

// Add-only reconcile of kyberTargets[] from the current maestroConfigs[]. Unlike
// the ?KYBER,LOCAL,Sx empty-params auto-populate (which wipes and rebuilds), this
// PRESERVES every existing enabled target — including a manually-documented remote
// port — and only adds a new enabled slot for a configured Maestro that has no
// target yet. Remote proxies (serialPort==0) get targetPort=1, which is unused at
// runtime for remote targets (forwardDataFromKyber broadcasts; the remote board
// owns its port). Returns the number of NEW targets added. Does NOT persist —
// callers save when the return is > 0.
int reconcileKyberTargetsFromMaestroConfigs() {
  int added = 0;
  for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
    if (!maestroConfigs[i].configured) continue;
    uint8_t id = maestroConfigs[i].maestroID;

    bool have = false;
    for (int j = 0; j < MAX_KYBER_TARGETS; j++) {
      if (kyberTargets[j].enabled && kyberTargets[j].maestroID == id) { have = true; break; }
    }
    if (have) continue;

    int slot = -1;
    for (int j = 0; j < MAX_KYBER_TARGETS; j++) {
      if (!kyberTargets[j].enabled) { slot = j; break; }
    }
    if (slot < 0) break;   // table full — nothing more we can add

    kyberTargets[slot].maestroID  = id;
    kyberTargets[slot].targetWCB  = maestroConfigs[i].remoteWCB > 0
                                    ? maestroConfigs[i].remoteWCB : WCB_Number;
    kyberTargets[slot].targetPort = maestroConfigs[i].serialPort > 0
                                    ? maestroConfigs[i].serialPort : 1;
    kyberTargets[slot].enabled    = true;
    added++;
  }
  return added;
}

// The one serial port a Kyber mode takes from every other subsystem: the HCR/MP3/DFP/WLED guards,
// canUsePWMOnPort (so ?MAP,PWM, the boot-time PWM load and WDP PWMTARGET auto-config) and ;P.
// Kyber LOCAL owns only kyberLocalPort - the other hardware port has been a normal command port since #14,
// and reserving it too dropped a PWM output saved there from NVS at the next boot (tracker #73 D4, HIL
// kyber.local_free_port_takes_pwm). Maestro REMOTE keeps S1: the parser never reads it, broadcasts skip it,
// and the receive path's legacy fallback writes Maestro bytes there (WCB.ino). Reads the live globals, as
// KyberLocalTask does, so a runtime ?KYBER,LOCAL,S<n> move is seen by every guard at once.
bool kyberModeReservesPort(int port) {
  if (port < 1 || port > 5) return false;
  if (Kyber_Local) return port == kyberLocalPort;
  return Maestro_Remote && port == 1;
}

// ?KYBER,LOCAL refusals, checked BEFORE storeKyberSettings touches any live state. They used to run
// after kyberUseTargeting was set and the targets were auto-populated (or, on the bare form, wiped),
// so a refused ?KYBER,LOCAL,S3 still switched a broadcast-mode board to targeting - #6's leak again,
// through the #28 refusal (HIL kyber.config_negatives / kyber.local_rejected_port_keeps_forwarding).
static bool kyberPortOwnedByOther(int port) {
  return isSerialPortUsedForWLED(port) || isSerialPortUsedForHCR(port) ||
         isSerialPortUsedForMP3(port)  || isSerialPortPWMOutput(port) ||
         isSerialPortUsedForDFP(port)  || isSerialPortUsedForPWMInput(port);
}

// The hardware ports a refused ?KYBER,LOCAL can honestly suggest: neither owned by another subsystem
// nor hosting a local Maestro. Printed as the "use ..." hint, or a note that neither is free.
static void kyberPrintFreeHwPorts() {
  const bool f1 = !kyberPortOwnedByOther(1) && !isSerialPortUsedForMaestro(1);
  const bool f2 = !kyberPortOwnedByOther(2) && !isSerialPortUsedForMaestro(2);
  if (f1 && f2)  Serial.println("   Use \"?KYBER,LOCAL,S1\" or \"?KYBER,LOCAL,S2\".");
  else if (f1)   Serial.println("   Use \"?KYBER,LOCAL,S1\" - S2 is taken.");
  else if (f2)   Serial.println("   Use \"?KYBER,LOCAL,S2\" - S1 is taken.");
  else           Serial.println("   Neither hardware port (S1, S2) is free - move a device off one first.");
}

// The Maestro id an explicit ?KYBER,LOCAL target list would put LOCALLY on `port` (the Kyber's own
// port), or 0. Parses each "M<id>:W<wcb>S<port>:<baud>" token exactly as the target loop below does.
// The target loop creates a local slot for such a token - after kyberLocalPortRefused has passed,
// because no slot exists yet - and re-bauds the Kyber port under it. That is the form ?backup and the
// Wizard emit, so without this the local-Maestro refusal was bypassed by every config push.
static int kyberTargetOnKyberPort(const String &params, int port) {
  int startIdx = 0;
  while (startIdx < (int)params.length()) {
    int nextComma = params.indexOf(',', startIdx);
    if (nextComma == -1) nextComma = params.length();
    String t = params.substring(startIdx, nextComma);
    startIdx = nextComma + 1;
    t.trim();
    t.toUpperCase();
    const int c1 = t.indexOf(':'), c2 = t.indexOf(':', c1 + 1);
    if (c1 == -1 || c2 == -1 || !t.startsWith("M")) continue;
    const String wp = t.substring(c1 + 1, c2);
    const int wIdx = wp.indexOf('W'), sIdx = wp.indexOf('S');
    if (wIdx == -1 || sIdx == -1) continue;
    if (wp.substring(wIdx + 1, sIdx).toInt() == WCB_Number && wp.substring(sIdx + 1).toInt() == port)
      return t.substring(1, c1).toInt();
  }
  return 0;
}

static bool kyberLocalPortRefused(int port) {
  // Don't seize a UART another subsystem already owns. Symmetric with the
  // HCR/MP3/WLED/PWM guards so two subsystems can't silently share one port.
  // (Config-time only — loadKyberSettings() reads NVS directly, never replays
  // this, so a saved config is never rejected at boot.)
  if (port >= 1 && port <= 5 && kyberPortOwnedByOther(port)) {
    Serial.printf("❌ Cannot set Kyber LOCAL on Serial%d - reserved by HCR/MP3/WLED/PWM/DFP\n", port);
    return true;
  }
  // The Kyber's rate is fixed at 115200 and S3-S5 receive in software, so a soft-serial Kyber port
  // has no working configuration - configureMaestro refuses the same ports for the same
  // reason. This used to call updateBaudRate(port, 115200) and report success.
  if (port >= 3 && port <= 5) {
    Serial.printf("❌ S%d is SOFTWARE SERIAL - the Kyber runs at 115200, which needs a hardware port.\n", port);
    kyberPrintFreeHwPorts();
    return true;
  }
  // A local Maestro on the port (Greg, 2026-09-22): accepting it moved the Maestro's port to 115200,
  // and KyberLocalTask's forwardMaestroDataToLocalKyber then wrote the Maestro's bytes back into the
  // same port, echoing it into itself. Local slots only - a remote proxy owns no local port.
  if (port >= 1 && port <= 2 && isSerialPortUsedForMaestro(port)) {
    Serial.printf("❌ Cannot set Kyber LOCAL on Serial%d - a local Maestro is configured there. "
                  "Clear it (?MAESTRO,CLEAR,M<id>:W%dS%d) or use the other hardware port.\n", port, WCB_Number, port);
    kyberPrintFreeHwPorts();
    return true;
  }
  return false;
}

// Give a port back when the Kyber leaves it: 9600 baud, ?BCAST output and input on - the reverse of what the
// LOCAL branch took (115200, both flags off). ?KYBER,CLEAR, a ?KYBER,LOCAL port move and ?MAESTRO,REMOTE
// (?KYBER,REMOTE) all release through here (tracker #73 D5/D9 - the move and REMOTE used to leave the old port
// at 115200 with broadcasts off). A port a local Maestro or another subsystem uses NOW keeps its settings:
// a swap target list puts a Maestro on the port the Kyber just left, and resetting it would re-baud that
// Maestro. Call only once kyberLocalPort no longer names the port, so KyberLocalTask is off it before
// applyLiveBaud re-begins it.
static void kyberReleasePort(int port) {
  if (port < 1 || port > 5) return;
  if (kyberPortOwnedByOther(port) || isSerialPortUsedForMaestro(port)) {
    Serial.printf("S%d keeps its settings - a Maestro or another device is configured there (was the Kyber port)\n", port);
    return;
  }
  updateBaudRate(port, 9600);
  Serial.printf("✓ Reset S%d baud rate to 9600 (was the Kyber port)\n", port);
  if (!serialBroadcastEnabled[port - 1]) {
    serialBroadcastEnabled[port - 1] = true;
    saveBroadcastSettingsToPreferences();
    Serial.printf("✓ Re-enabled broadcast output on S%d\n", port);
  }
  if (blockBroadcastFrom[port - 1]) {
    blockBroadcastFrom[port - 1] = false;
    saveBroadcastBlockSettings();
    Serial.printf("✓ Re-enabled broadcast input on S%d\n", port);
  }
}

void storeKyberSettings(const String &message) {
  int firstComma = message.indexOf(',');
  String baseCommand;
  String params = "";
  int kyberPort = 0;
  int releasePort = 0;   // the port the Kyber gives up; released after the target loop

  if (firstComma == -1) {
    baseCommand = message;
    baseCommand.toLowerCase();
    // Bare LOCAL keeps a local Kyber where it is and only switches to broadcast mode; S2 is the default for a
    // board that is not Kyber local yet. Hard-coding S2 re-pointed a Kyber on S1 at S2 (refused outright with a
    // Maestro there) and left S1 claimed (#73 D5). S1/S2 only: a legacy S3-S5 Kyber moves to S2 and its old port
    // is released. REMOTE and CLEAR keep 2, so their stored K_Port bytes are unchanged.
    kyberPort = (baseCommand.equals("local") && Kyber_Local && kyberLocalPort >= 1 && kyberLocalPort <= 2)
                ? kyberLocalPort : 2;
    if (baseCommand.equals("local") && kyberLocalPortRefused(kyberPort)) return;   // before the target wipe
    kyberUseTargeting = false;

    for (int i = 0; i < MAX_KYBER_TARGETS; i++) {
      kyberTargets[i].enabled = false;
      kyberTargets[i].maestroID = 0;
      kyberTargets[i].targetWCB = 0;
      kyberTargets[i].targetPort = 0;
    }
  } else {
    baseCommand = message.substring(0, firstComma);
    baseCommand.toLowerCase();
    params = message.substring(firstComma + 1);
    
    int secondComma = params.indexOf(',');
if (params.startsWith("S") || params.startsWith("s")) {
  String portStr;
  if (secondComma != -1) {
    portStr = params.substring(1, secondComma);
    params = params.substring(secondComma + 1);
  } else {
    portStr = params.substring(1);  // port only, no maestro targets
    params = "";
  }
  // Validate BEFORE touching live state. kyberUseTargeting used to be set here with the port
  // range checked afterwards, so a refused ?KYBER,LOCAL,S6 left targeting ENABLED on a board that
  // had been broadcasting: forwardDataFromKyber then took the targeted branch and walked a
  // kyberTargets[] the rejected command never populated, dropping every sabre byte with no
  // message. The check covers REMOTE too now - it was gated on "local", so ?KYBER,REMOTE,S9
  // skipped it entirely.
  const int wantPort = portStr.toInt();
  if (wantPort < 1 || wantPort > 5) {
    Serial.println("Invalid Kyber port. Must be S1-S5");
    return;
  }
  if (baseCommand.equals("local") && kyberLocalPortRefused(wantPort)) return;
  if (baseCommand.equals("local")) {
    const int onKyberPort = kyberTargetOnKyberPort(params, wantPort);
    if (onKyberPort) {
      Serial.printf("❌ Cannot set Kyber LOCAL on Serial%d - target M%d puts a local Maestro on the Kyber port.\n",
                    wantPort, onKyberPort);
      kyberPrintFreeHwPorts();
      return;
    }
  }
  kyberPort = wantPort;
  kyberUseTargeting = true;
} else {
  Serial.println("Invalid format. Use: ?KYBER,LOCAL,Sx or ?KYBER,LOCAL,Sx,M1:W1S1:57600");
  return;
}
  } 
   // If port specified but no targets provided, auto-populate from existing maestro configs
  if (kyberUseTargeting && params.length() == 0) {
      int targetIndex = 0;
      for (int i = 0; i < MAX_KYBER_TARGETS; i++) {
          kyberTargets[i].enabled = false;
      }
      for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
          if (maestroConfigs[i].configured && targetIndex < MAX_KYBER_TARGETS) {
              kyberTargets[targetIndex].maestroID  = maestroConfigs[i].maestroID;
              kyberTargets[targetIndex].targetWCB  = maestroConfigs[i].remoteWCB > 0 
                                                     ? maestroConfigs[i].remoteWCB 
                                                     : WCB_Number;
              kyberTargets[targetIndex].targetPort = maestroConfigs[i].serialPort > 0
                                                     ? maestroConfigs[i].serialPort
                                                     : 1;
              kyberTargets[targetIndex].enabled    = true;
              targetIndex++;
          }
      }
      if (targetIndex > 0) {
          Serial.printf("Auto-populated %d Kyber targets from existing Maestro configs\n", targetIndex);
      } else {
          Serial.println("Warning: No Maestro configs found to auto-populate Kyber targets");
          Serial.println("Configure Maestros first with ?MAESTRO,M<id>:W<wcb>S<port>:<baud>");
      }
  }
  
  if (baseCommand.equals("local")) {
    // Port refusals ran above, before any live state changed (kyberLocalPortRefused).
    // A move to the other port releases the old one after the target loop below (it may put a Maestro there).
    // Re-pointing kyberLocalPort first moves a running KyberLocalTask off the old port on its next pass.
    if (kyberLocalPort >= 1 && kyberLocalPort <= 5 && kyberLocalPort != kyberPort) releasePort = kyberLocalPort;
    Kyber_Local = true;
    Maestro_Remote = false;
    Kyber_Location = "local";
    kyberLocalPort = kyberPort;   // store globally so forwarding functions use correct port
    Serial.printf("Kyber is LOCAL on Serial%d\n", kyberPort);
    // KyberLocalTask / KyberRemoteTask are created ONLY at boot (WCB.ino setup()), so flipping the
    // mode at runtime leaves the newly-owned ports with no reader at all until a restart. Say so
    // rather than letting the board look configured-but-deaf.
    Serial.println("⚠️  Reboot required — the Kyber forwarding task is only started at boot.");
    
    if (kyberPort > 0 && kyberPort <= 5) {
      updateBaudRate(kyberPort, 115200);
      Serial.printf("Set Serial%d to 115200 baud (Kyber standard)\n", kyberPort);

      if (serialBroadcastEnabled[kyberPort - 1]) {
        serialBroadcastEnabled[kyberPort - 1] = false;
        saveBroadcastSettingsToPreferences();
        Serial.printf("✓ Disabled broadcast output on S%d (Kyber port)\n", kyberPort);
      }
      if (!blockBroadcastFrom[kyberPort - 1]) {
        blockBroadcastFrom[kyberPort - 1] = true;
        saveBroadcastBlockSettings();
        Serial.printf("✓ Disabled broadcast input on S%d (Kyber port)\n", kyberPort);
      }
    }
    
  } else if (baseCommand.equals("remote")) {
    // Leaving Kyber LOCAL gives the Kyber port up, as ?KYBER,CLEAR does (#73 D9). KyberLocalTask is boot-only and
    // gates on kyberLocalPort alone (forwardDataFromKyber / forwardMaestroDataToLocalKyber, WCB.ino), so leaving it
    // set kept a Kyber_Local-booted board bridging the old port - beside the Maestro_Remote parser branch, which
    // reads S2 - and echoing its Maestro ports into it until reboot. Zero means the task idles and
    // maestroPortOwnedByKyberBridge hands the ports back.
    if (kyberLocalPort >= 1 && kyberLocalPort <= 5) releasePort = kyberLocalPort;
    kyberLocalPort = 0;
    Kyber_Local = false;
    Maestro_Remote = true;
    Kyber_Location = "remote";
    Serial.println("Kyber is REMOTE (on another WCB)");
    Serial.println("⚠️  Reboot required — the Maestro-remote forwarding task is only started at boot.");
    
  } else if (baseCommand.equals("clear")) {
    // Act on the port the Kyber was ACTUALLY on, and only if it was configured at all.
    //
    // This used to operate on `kyberPort`, which the bare `?KYBER,CLEAR` path hard-codes to 2.
    // Because collectConfigCommands emits ?KYBER,CLEAR in EVERY non-Kyber board's config chain,
    // every full push to every plain board reset Serial2 to 9600 and re-enabled its broadcast
    // flags — silently undoing settings the same push had just applied a few commands earlier.
    const int clearPort = (kyberLocalPort >= 1 && kyberLocalPort <= 5) ? kyberLocalPort : 0;
    // Only a board that WAS in a Kyber mode needs the reboot notice below: collectConfigCommands and
    // the Wizard send ?KYBER,CLEAR in every plain board's config push, which must stay quiet.
    const bool wasKyberMode = Kyber_Local || Maestro_Remote;

    Kyber_Location = " ";
    Kyber_Local = false;
    Maestro_Remote = false;
    kyberLocalPort = 0;
    kyberUseTargeting = false;

    preferences.begin("kyber_settings", false);
    preferences.putString("K_Location", Kyber_Location);
    preferences.end();

    // Only the port the Kyber actually held (0 = none). Indexing with the hard-coded 2 also meant
    // `?KYBER,CLEAR,S0` wrote serialBroadcastEnabled[-1].
    kyberReleasePort(clearPort);
    saveKyberTargets();
    Serial.println("Kyber cleared. Run ?MAESTRO_DEFAULT to clear Maestro configs.");
    if (wasKyberMode)   // same wording as the LOCAL/REMOTE branches; the S1/S2 setup and the Kyber
                        // tasks are chosen at boot (the bridge task idles until then - WCB.ino)
      Serial.println("⚠️  Reboot required — Serial1/Serial2 setup and the Kyber forwarding tasks are only chosen at boot.");
    return; 
  }
  
  // Parse Maestro targets
  if (kyberUseTargeting && params.length() > 0) {
    int targetIndex = 0;
    
    for (int i = 0; i < MAX_KYBER_TARGETS; i++) {
      kyberTargets[i].enabled = false;
    }
    
    int startIdx = 0;
    
    while (startIdx < params.length() && targetIndex < MAX_KYBER_TARGETS) {
      int nextComma = params.indexOf(',', startIdx);
      if (nextComma == -1) nextComma = params.length();
      
      String targetStr = params.substring(startIdx, nextComma);
      targetStr.trim();
      targetStr.toUpperCase();
      
      int firstColon = targetStr.indexOf(':');
      int secondColon = targetStr.indexOf(':', firstColon + 1);
      
      if (firstColon != -1 && secondColon != -1 && targetStr.startsWith("M")) {
        int maestroID = targetStr.substring(1, firstColon).toInt();
        String wcbPortStr = targetStr.substring(firstColon + 1, secondColon);
        int baudRate = targetStr.substring(secondColon + 1).toInt();
        
        int wIdx = wcbPortStr.indexOf('W');
        int sIdx = wcbPortStr.indexOf('S');
        
        if (wIdx != -1 && sIdx != -1) {
          int wcbNum = wcbPortStr.substring(wIdx + 1, sIdx).toInt();
          int portNum = wcbPortStr.substring(sIdx + 1).toInt();
          
          // 1-8, not 1-9: id 9 is the reserved "all local Maestros" routing target and must never
          // become a slot (CLAUDE.md rule 5). configureMaestro and WDP auto-add both refuse it; this
          // path did not, and an id-9 slot then matched ;M9 in the config loop and returned before
          // the fan-out, writing a literal device-9 frame instead of re-addressing each Maestro.
          if (maestroID >= 1 && maestroID <= 8 &&
              wcbNum >= 1 && wcbNum <= MAX_WCB_COUNT &&
              portNum >= 1 && portNum <= 5 &&
              // Same 13-rate list configureMaestro() validates against (WCB_Maestro.cpp:610).
              // This used to accept only six, which meant a Maestro baud that ?MAESTRO took
              // happily was rejected by ?KYBER,LOCAL for the SAME physical device — and the
              // Wizard computes its Kyber targets from the Maestro rows, so a legal 2400-baud
              // Maestro made the whole target line get skipped. Widening is safe: the accept
              // path calls updateBaudRate() below, which validates the same 13.
              (baudRate == 110 || baudRate == 300 || baudRate == 600 ||
               baudRate == 1200 || baudRate == 2400 || baudRate == 9600 ||
               baudRate == 14400 || baudRate == 19200 || baudRate == 38400 ||
               baudRate == 57600 || baudRate == 115200 || baudRate == 128000 ||
               baudRate == 256000)) {
            
            kyberTargets[targetIndex].maestroID = maestroID;
            kyberTargets[targetIndex].targetWCB = wcbNum;
            kyberTargets[targetIndex].targetPort = portNum;
            kyberTargets[targetIndex].enabled = true;
            
            Serial.printf("Kyber target %d: Maestro %d → WCB%d S%d (%d baud)\n",
                          targetIndex + 1, maestroID, wcbNum, portNum, baudRate);
            // Warn only if the target board isn't a current mesh peer (floor OR
            // WDP-learned). With auto-join on, a board above WCBQ may already be a
            // member — don't cry wolf, and don't tell anyone to reboot.
            if (wcbNum >= 1 && wcbNum <= MAX_WCB_COUNT && !wcbPeerActive[wcbNum - 1] &&
                !(specialPeerEnabled && wcbNum == WCB_Number))
              Serial.printf("⚠️  Warning: WCB%d is not a current mesh peer (WCBQ floor is %d, "
                            "and it hasn't been auto-joined). Add it with ?WDP,ADD,%d (or raise "
                            "?WCBQ), or this target won't be reachable.\n",
                            wcbNum, Default_WCB_Quantity, wcbNum);
            
            // Auto-configure Maestro. Use the SAME slot key as the ?MAESTRO
            // handler — (maestroID, serialPort, remoteWCB) — so the two config
            // paths can never disagree and create duplicate slots for the same
            // physical Maestro. Local: (id, portNum, 0); remote: (id, 0, wcb).
            uint8_t kKeyRemote = (wcbNum == WCB_Number) ? 0 : (uint8_t)wcbNum;
            uint8_t kKeyPort   = (wcbNum == WCB_Number) ? (uint8_t)portNum : 0;
            int8_t maestroSlot = findSlotByMaestroIDPortTarget(maestroID, kKeyPort, kKeyRemote);
            if (maestroSlot < 0) {
              maestroSlot = findEmptySlot();
            }
            
            if (maestroSlot >= 0) {
              maestroConfigs[maestroSlot].maestroID = maestroID;
              maestroConfigs[maestroSlot].configured = true;
              
              if (wcbNum == WCB_Number) {
                maestroConfigs[maestroSlot].serialPort = portNum;
                maestroConfigs[maestroSlot].remoteWCB = 0;
                // Record the baud on the slot itself. Without this the slot
                // kept a stale/default baud (e.g. 115200) while the port was
                // actually set to baudRate — making the config backup emit a
                // self-contradictory ?MAESTRO line that never matched the UI,
                // so the web tool re-pushed ?MAESTRO on every single save.
                maestroConfigs[maestroSlot].baudRate = baudRate;

                updateBaudRate(portNum, baudRate);
                
                if (serialBroadcastEnabled[portNum - 1]) {
                  serialBroadcastEnabled[portNum - 1] = false;
                  saveBroadcastSettingsToPreferences();
                  Serial.printf("⚠️  Disabled broadcast output on S%d (Maestro port)\n", portNum);
                }
                if (!blockBroadcastFrom[portNum - 1]) {
                  blockBroadcastFrom[portNum - 1] = true;
                  saveBroadcastBlockSettings();
                  Serial.printf("⚠️  Disabled broadcast input on S%d (Maestro port)\n", portNum);
                }
                
                Serial.printf("✓ Maestro %d: Local S%d at %d baud (slot %d)\n", 
                              maestroID, portNum, baudRate, maestroSlot + 1);
              } else {
                maestroConfigs[maestroSlot].serialPort = 0;
                maestroConfigs[maestroSlot].remoteWCB = wcbNum;
                maestroConfigs[maestroSlot].baudRate = baudRate;  // keep slot baud consistent (matches ?MAESTRO handler)
                Serial.printf("✓ Maestro %d: Remote on WCB%d\n", maestroID, wcbNum);
              }
            }
            
            targetIndex++;
          } else {
            // Print a clear reason so the user knows why a target was rejected
            if (maestroID < 1 || maestroID > 8)
              Serial.printf("⚠️  Skipping target '%s': Maestro ID must be 1-8 (9 = all local Maestros)\n", targetStr.c_str());
            else if (wcbNum < 1 || wcbNum > MAX_WCB_COUNT)
              Serial.printf("⚠️  Skipping target '%s': WCB number out of range (1-%d)\n", targetStr.c_str(), MAX_WCB_COUNT);
            else if (portNum < 1 || portNum > 5)
              Serial.printf("⚠️  Skipping target '%s': port must be S1-S5\n", targetStr.c_str());
            else
              Serial.printf("⚠️  Skipping target '%s': baud rate must be one of 110/300/600/1200/2400/9600/14400/19200/38400/57600/115200/128000/256000\n", targetStr.c_str());
          }
        }
      }

      startIdx = nextComma + 1;
    }
    
    saveMaestroSettings();
    
    // Generate copy-paste commands for other WCBs
    if (baseCommand.equals("local")) {
      Serial.println("\n─────────────────────────────────────────────────────");
      Serial.println("📋 COPY-PASTE SETUP COMMANDS FOR OTHER WCBs:");
      Serial.println("─────────────────────────────────────────────────────\n");
      
      for (int wcb = 1; wcb <= Default_WCB_Quantity; wcb++) {
        if (wcb == WCB_Number) continue;
        
        // Only add MAESTRO_REMOTE (legacy alias: KYBER_REMOTE) if this WCB has Maestro ID 1 or 2 physically on it
        bool needsKyberRemote = false;
        for (int i = 0; i < MAX_KYBER_TARGETS; i++) {
          if (kyberTargets[i].enabled && 
              kyberTargets[i].targetWCB == wcb &&
              kyberTargets[i].maestroID <= 2) {
            needsKyberRemote = true;
            break;
          }
        }
        
        String command = "";
        if (needsKyberRemote) {
          command += "?" + String("MAESTRO_REMOTE^?");
        } else {
          command += "?";
        }
        
        command += "MAESTRO";
        
        // Add all Maestro mappings
        int cmdStartIdx = 0;
        while (cmdStartIdx < params.length()) {
          int cmdNextComma = params.indexOf(',', cmdStartIdx);
          if (cmdNextComma == -1) cmdNextComma = params.length();
          
          String cmdTargetStr = params.substring(cmdStartIdx, cmdNextComma);
          cmdTargetStr.trim();
          cmdTargetStr.toUpperCase();
          
          int cmdFirstColon = cmdTargetStr.indexOf(':');
          int cmdSecondColon = cmdTargetStr.indexOf(':', cmdFirstColon + 1);
          
          if (cmdFirstColon != -1 && cmdSecondColon != -1 && cmdTargetStr.startsWith("M")) {
            int cmdMaestroID = cmdTargetStr.substring(1, cmdFirstColon).toInt();
            String cmdWcbPortStr = cmdTargetStr.substring(cmdFirstColon + 1, cmdSecondColon);
            int cmdBaudRate = cmdTargetStr.substring(cmdSecondColon + 1).toInt();
            
            command += ",M" + String(cmdMaestroID) + ":" + cmdWcbPortStr + ":" + String(cmdBaudRate);
          }
          
          cmdStartIdx = cmdNextComma + 1;
        }
        
        // Add label commands only for Maestros local to this WCB
        cmdStartIdx = 0;
        while (cmdStartIdx < params.length()) {
          int cmdNextComma = params.indexOf(',', cmdStartIdx);
          if (cmdNextComma == -1) cmdNextComma = params.length();
          
          String cmdTargetStr = params.substring(cmdStartIdx, cmdNextComma);
          cmdTargetStr.trim();
          cmdTargetStr.toUpperCase();
          
          int cmdFirstColon = cmdTargetStr.indexOf(':');
          int cmdSecondColon = cmdTargetStr.indexOf(':', cmdFirstColon + 1);
          
          if (cmdFirstColon != -1 && cmdSecondColon != -1 && cmdTargetStr.startsWith("M")) {
            int cmdMaestroID = cmdTargetStr.substring(1, cmdFirstColon).toInt();
            String cmdWcbPortStr = cmdTargetStr.substring(cmdFirstColon + 1, cmdSecondColon);
            
            int wIdx = cmdWcbPortStr.indexOf('W');
            int sIdx = cmdWcbPortStr.indexOf('S');
            
            if (wIdx != -1 && sIdx != -1) {
              int targetWCB = cmdWcbPortStr.substring(wIdx + 1, sIdx).toInt();
              int targetPort = cmdWcbPortStr.substring(sIdx + 1).toInt();
              
              if (targetWCB == wcb) {
                command += "^?" + String("SLS") + String(targetPort) + ",Maestro " + String(cmdMaestroID);
              }
            }
          }
          
          cmdStartIdx = cmdNextComma + 1;
        }
        
        command += "^?REBOOT";
        Serial.printf("WCB%d - Run this command:\n%s\n\n", wcb, command.c_str());
      }
      
      Serial.println("─────────────────────────────────────────────────────\n");
    }
  }

  // After the target loop: a target it just put on the released port makes that port a Maestro's, and
  // kyberReleasePort then leaves it alone.
  kyberReleasePort(releasePort);   // no-op for 0

  preferences.begin("kyber_settings", false);
  preferences.putString("K_Location", Kyber_Location);
  preferences.putInt("K_Port", kyberPort);   // persist so kyberLocalPort is correct after reboot
  preferences.end();

  saveKyberTargets();
  
  if (kyberUseTargeting) {
    Serial.printf("Kyber %s with targeted forwarding configured\n", Kyber_Location.c_str());
  } else {
    Serial.printf("Kyber %s with broadcast mode\n", Kyber_Location.c_str());
  }
}


void loadKyberSettings(){
  preferences.begin("kyber_settings", true);
  Kyber_Location = preferences.getString("K_Location", "");
  int storedPort = preferences.getInt("K_Port", 0);
  preferences.end();

  if (Kyber_Location == "local"){
      Kyber_Local = true;
      Maestro_Remote = false;
      // Fall back to port 2 (legacy default) if K_Port was never saved to NVS
      kyberLocalPort = (storedPort > 0 && storedPort <= 5) ? storedPort : 2;
  } else if (Kyber_Location == "remote"){
      Kyber_Local = false;
      Maestro_Remote = true;
      kyberLocalPort = 0;
  } else {
      Kyber_Local = false;
      Maestro_Remote = false;
      kyberLocalPort = 0;
  }
};


void printKyberSettings() {
  Serial.println("--------Kyber Settings ----------------------------------");

  String temp;
  if (Kyber_Location == "local")       temp = "Local";
  else if (Kyber_Location == "remote") temp = "Remote";
  else                                  temp = "Not used";

  Serial.printf("Kyber is %s\n", temp.c_str());

  // The actual ports. This used to name "Serial1 & Serial2" whatever kyberLocalPort was, and said
  // "Initialized" after every ?KYBER command too, where nothing changes until the reboot (tracker #73 D8).
  if (Kyber_Local) {
    if (kyberLocalPort >= 1 && kyberLocalPort <= 5)
      Serial.printf("Kyber port: Serial%d (%lu baud)\n", kyberLocalPort, baudRates[kyberLocalPort - 1]);
    else
      Serial.println("Kyber port: not set");
  } else if (Maestro_Remote) {
    // Mirrors the target-98 receive path (WCB.ino espNowReceiveCallback): every local Maestro port, else S1.
    String ports;
    for (int p = 1; p <= 5; p++)
      if (isSerialPortUsedForMaestro(p)) ports += String(ports.length() ? ", S" : "S") + String(p);
    Serial.printf("Maestro data from the mesh goes to: %s\n",
                  ports.length() ? ports.c_str() : "S1 (no local Maestro configured - legacy default)");
  }

  printMaestroSettings();
}

void loadSerialLabelsFromPreferences() {
    preferences.begin("serial_labels", true);
    for (int i = 0; i < 5; i++) {
        String key = "label" + String(i + 1);
        serialPortLabels[i] = preferences.getString(key.c_str(), "");
    }
    preferences.end();
}

void saveSerialLabelToPreferences(int port, const String &label) {
    if (port < 1 || port > 5) return;
    
    preferences.begin("serial_labels", false);
    String key = "label" + String(port);
    if (label.length() > 0) {
        preferences.putString(key.c_str(), label);
    } else {
        preferences.remove(key.c_str());
    }
    preferences.end();
    
    serialPortLabels[port - 1] = label;
    Serial.printf("Serial%d label set to: '%s'\n", port, label.c_str());
}

void clearSerialLabel(int port) {
    if (port < 1 || port > 5) return;
    saveSerialLabelToPreferences(port, "");
    Serial.printf("Serial%d label cleared\n", port);
}


String getSerialLabel(int port) {
  if (port == 0) {
    return "Serial0 (USB)";
  }
  if (port < 1 || port > 5) return "Serial" + String(port);
  
  String label = "Serial" + String(port);
  if (serialPortLabels[port - 1].length() > 0) {
      label += " (" + serialPortLabels[port - 1] + ")";
  }
  return label;
}

void loadSerialMonitorMappings() {
    preferences.begin("serial_map", true);
    
    for (int i = 0; i < MAX_SERIAL_MONITOR_MAPPINGS; i++) {
        String keyActive = "sm" + String(i) + "_act";
        serialMonitorMappings[i].active = preferences.getBool(keyActive.c_str(), false);
        
        if (serialMonitorMappings[i].active) {
            String keyInput = "sm" + String(i) + "_in";
            String keyCount = "sm" + String(i) + "_cnt";
            String keyRaw = "sm" + String(i) + "_raw";
            
            serialMonitorMappings[i].inputPort = preferences.getUChar(keyInput.c_str(), 0);
            serialMonitorMappings[i].outputCount = preferences.getUChar(keyCount.c_str(), 0);
            serialMonitorMappings[i].rawMode = preferences.getBool(keyRaw.c_str(), false);
            // Defaults ON/unblocked: a mapping stored before these keys existed restores the
            // way it always did, so upgrading changes nothing for an existing config.
            serialMonitorMappings[i].prevBroadcastOut =
                preferences.getBool(("sm" + String(i) + "_pbo").c_str(), true);
            serialMonitorMappings[i].prevBlockIn =
                preferences.getBool(("sm" + String(i) + "_pbi").c_str(), false);
            
            for (int j = 0; j < serialMonitorMappings[i].outputCount; j++) {
                String keyWCB = "sm" + String(i) + "_" + String(j) + "w";
                String keyPort = "sm" + String(i) + "_" + String(j) + "p";
                serialMonitorMappings[i].outputs[j].wcbNumber = preferences.getUChar(keyWCB.c_str(), 0);
                serialMonitorMappings[i].outputs[j].serialPort = preferences.getUChar(keyPort.c_str(), 0);
            }
        }
    }
    
    preferences.end();
}

void saveSerialMonitorSettings() {
    preferences.begin("serial_monitor", false);
    for (int i = 0; i < 5; i++) {
        String key = "mon" + String(i + 1);
        preferences.putBool(key.c_str(), serialMonitorEnabled[i]);
    }
    preferences.putBool("mirror_usb", mirrorToUSB);
    preferences.putBool("mirror_kyber", mirrorToKyber);
    preferences.end();
}

void loadBroadcastBlockSettings() {
    preferences.begin("bcast_block", true);
    for (int i = 0; i < 5; i++) {
        String key = "blk" + String(i + 1);
        blockBroadcastFrom[i] = preferences.getBool(key.c_str(), false);
    }
    preferences.end();
}

void saveBroadcastBlockSettings() {
    preferences.begin("bcast_block", false);
    for (int i = 0; i < 5; i++) {
        String key = "blk" + String(i + 1);
        preferences.putBool(key.c_str(), blockBroadcastFrom[i]);
    }
    preferences.end();
}


void addSerialMonitorMapping(const String &message) {
    // Format: ?SMSxR,dest1,dest2  or ?SMSx,dest1R,dest2
    // Where R indicates raw binary mode for that port
    
    int firstComma = message.indexOf(',');
    if (firstComma == -1) {
        Serial.println("Invalid format. Use: ?SMSx[R],dest1[R],dest2[R],...");
        return;
    }

    // Parse input port
    String sourceStr = message.substring(0, firstComma);
    sourceStr.trim();

    if (!sourceStr.startsWith("S") && !sourceStr.startsWith("s")) {
        Serial.println("Invalid format. Source must be Sx or SxR (e.g., S1, S3R)");
        return;
    }

    // Check for 'R' suffix on input
    bool inputRawMode = false;
    if (sourceStr.endsWith("R") || sourceStr.endsWith("r")) {
        inputRawMode = true;
        sourceStr = sourceStr.substring(0, sourceStr.length() - 1);  // Remove 'R'
    }

    int inputPort = sourceStr.substring(1).toInt();
    if (inputPort < 1 || inputPort > 5) {
        Serial.println("Invalid input port. Must be 1-5");
        return;
    }

    // Check for conflicts
    if (isSerialPortUsedForPWMInput(inputPort)) {
        Serial.println("Cannot create mapping: Port is used for PWM input");
        return;
    }
    if (isSerialPortPWMOutput(inputPort)) {
        Serial.println("Cannot create mapping: Port is configured as PWM output");
        return;
    }

    // Find or create mapping for this input port.
    //
    // A ?MAP,SERIAL command carries the COMPLETE destination list the caller wants for this input
    // port, so it REPLACES the previous list rather than appending to it. Appending meant a
    // destination the user removed or re-pointed in the Wizard stayed live on the board forever —
    // the config was pushed, reported success, and the old route kept forwarding.
    // Staged, not live. This used to zero the existing mapping's outputCount (or allocate a slot
    // with active=true) BEFORE parsing a single destination, while the commit below only saves when
    // outputsAdded > 0 - so a command whose destinations were all invalid left an ACTIVE mapping
    // holding zero outputs. In text mode that silently swallowed every line arriving on the port;
    // in raw mode serialCommandTask stopped reading the port at all, so it stopped executing
    // commands, and ?backup emitted a token that could not be replayed. Parse into a local and
    // replace the slot only once something valid came out of it.
    SerialMonitorMapping *slot = nullptr;
    for (int i = 0; i < MAX_SERIAL_MONITOR_MAPPINGS; i++) {
        if (serialMonitorMappings[i].active && serialMonitorMappings[i].inputPort == inputPort) {
            slot = &serialMonitorMappings[i];
            break;
        }
    }
    if (!slot) {
        for (int i = 0; i < MAX_SERIAL_MONITOR_MAPPINGS; i++) {
            if (!serialMonitorMappings[i].active) { slot = &serialMonitorMappings[i]; break; }
        }
    }
    if (!slot) {
        Serial.println("Maximum serial mappings reached");
        return;
    }

    SerialMonitorMapping staged = {};
    staged.active      = true;
    staged.inputPort   = inputPort;
    staged.rawMode     = inputRawMode;
    staged.outputCount = 0;

    // Parse outputs
    String remaining = message.substring(firstComma + 1);
    int outputsAdded = 0;

    while (remaining.length() > 0) {
        int nextComma = remaining.indexOf(',');
        String dest = (nextComma == -1) ? remaining : remaining.substring(0, nextComma);
        dest.trim();

        // CRITICAL: Advance 'remaining' BEFORE processing to prevent infinite loops
        if (nextComma == -1) {
            remaining = "";  // Last item - clear remaining
        } else {
            remaining = remaining.substring(nextComma + 1);
        }

        // Now process the destination
        if (dest.length() == 0) {
            continue;  // Skip empty destinations
        }

        uint8_t wcbNum = 0;
        uint8_t serialPort = 0;
        bool outputRawMode = false;

        // Check for 'R' suffix on output
        if (dest.endsWith("R") || dest.endsWith("r")) {
            outputRawMode = true;
            dest = dest.substring(0, dest.length() - 1);
        }

        // Parse destination
        if (dest.startsWith("W") || dest.startsWith("w")) {
            int sPos = dest.indexOf('S', 1);
            if (sPos == -1) sPos = dest.indexOf('s', 1);
            if (sPos != -1) {
                wcbNum = dest.substring(1, sPos).toInt();
                serialPort = dest.substring(sPos + 1).toInt();
            } else {
                Serial.printf("Invalid destination format: %s\n", dest.c_str());
                continue;
            }
        } else if (dest.startsWith("S") || dest.startsWith("s")) {
            wcbNum = 0;
            serialPort = dest.substring(1).toInt();
        } else {
            Serial.printf("Invalid destination format: %s\n", dest.c_str());
            continue;
        }

            if (wcbNum > MAX_WCB_COUNT || serialPort > 5) {
                Serial.printf("Invalid destination: WCB %d Serial %d\n", wcbNum, serialPort);
                continue;
            }
            // A remote S0 can never receive a raw chunk: the receiving board drops every
            // WCB_TARGET_RAW_SERIAL frame whose port is < 1, so the mapping would be accepted,
            // reported as set, and silently deliver nothing. (S0 is fine for a TEXT mapping -
            // that is the remote board's USB console.)
            if (inputRawMode && wcbNum != 0 && serialPort == 0) {
                Serial.printf("Invalid destination: raw mappings cannot target a remote S0 (W%dS0)\n", wcbNum);
                continue;
            }

            // Check for duplicate before adding
            if (mappingDestinationExists(&staged, wcbNum, serialPort)) {
                Serial.printf("Mapping already exists: Serial%d -> %s%d - skipping duplicate\n",
                             inputPort,
                             wcbNum == 0 ? "S" : ("W" + String(wcbNum) + "S").c_str(),
                             serialPort);
                continue;  // Skip this destination
            }

        // Add to the STAGED copy. The port's broadcast flags are only touched at commit, so a
        // mapping that parses to nothing cannot leave them flipped on its way to changing nothing.
        if (staged.outputCount < 10) {
            staged.outputs[staged.outputCount].wcbNumber = wcbNum;
            staged.outputs[staged.outputCount].serialPort = serialPort;
            staged.outputCount++;
            outputsAdded++;
        }
    }

    if (outputsAdded > 0) {
        // Record the port's pre-mapping flags ONLY when this claims the slot. A re-issue of an
        // existing mapping (idempotent by design - a Wizard push re-sends it) finds the flags
        // already disabled BY the mapping; recording those made CLEAR restore OFF/blocked and
        // leave the port dark. Keep the values captured when the mapping first appeared.
        const bool wasMapped = slot->active && slot->inputPort == inputPort;
        const bool keptOut   = slot->prevBroadcastOut;
        const bool keptIn    = slot->prevBlockIn;
        *slot = staged;          // replace, don't append - see the staging comment above
        if (wasMapped) {
            slot->prevBroadcastOut = keptOut;
            slot->prevBlockIn      = keptIn;
        } else {
            slot->prevBroadcastOut = serialBroadcastEnabled[inputPort - 1];
            slot->prevBlockIn      = blockBroadcastFrom[inputPort - 1];
        }

        // Auto-disable both broadcast directions for the port, now that the mapping is real.
        if (!blockBroadcastFrom[inputPort - 1]) {
            blockBroadcastFrom[inputPort - 1] = true;
            saveBroadcastBlockSettings();
            Serial.printf("Auto-enabled broadcast input blocking on Serial%d\n", inputPort);
        }
        if (serialBroadcastEnabled[inputPort - 1]) {
            serialBroadcastEnabled[inputPort - 1] = false;
            saveBroadcastSettingsToPreferences();
            Serial.printf("Auto-disabled broadcast output on Serial%d\n", inputPort);
        }

        saveSerialMonitorMappings();
        // The list is REPLACED, not appended, so outputsAdded == the full destination count.
        // This also persists a raw-mode toggle on an otherwise-unchanged mapping, which used to
        // change only the RAM copy and silently revert on the next reboot.
        Serial.printf("Serial mapping set: Serial%d%s -> %d destination(s)\n",
                      inputPort, inputRawMode ? " (RAW)" : "", staged.outputCount);
    } else {
        // Nothing valid parsed: the existing mapping (if any) is untouched, and no slot was taken.
        Serial.printf("No valid destinations - Serial%d mapping left unchanged\n", inputPort);
    }
}

void loadSerialMonitorSettings() {
    preferences.begin("serial_monitor", true);
    for (int i = 0; i < 5; i++) {
        String key = "mon" + String(i + 1);
        serialMonitorEnabled[i] = preferences.getBool(key.c_str(), false);
    }
    mirrorToUSB = preferences.getBool("mirror_usb", true);
    mirrorToKyber = preferences.getBool("mirror_kyber", false);
    preferences.end();
}

// void loadSerialMonitorMappings() {
//     preferences.begin("serial_monitor", true);
    
//     for (int i = 0; i < MAX_SERIAL_MONITOR_MAPPINGS; i++) {
//         String keyActive = "sm" + String(i) + "_act";
//         serialMonitorMappings[i].active = preferences.getBool(keyActive.c_str(), false);
        
//         if (serialMonitorMappings[i].active) {
//             String keyInput = "sm" + String(i) + "_in";
//             String keyCount = "sm" + String(i) + "_cnt";
            
//             serialMonitorMappings[i].inputPort = preferences.getUChar(keyInput.c_str(), 0);
//             serialMonitorMappings[i].outputCount = preferences.getUChar(keyCount.c_str(), 0);
            
//             for (int j = 0; j < serialMonitorMappings[i].outputCount; j++) {
//                 String keyWCB = "sm" + String(i) + "_" + String(j) + "w";
//                 String keyPort = "sm" + String(i) + "_" + String(j) + "p";
//                 serialMonitorMappings[i].outputs[j].wcbNumber = preferences.getUChar(keyWCB.c_str(), 0);
//                 serialMonitorMappings[i].outputs[j].serialPort = preferences.getUChar(keyPort.c_str(), 0);
//             }
//         }
//     }
    
//     preferences.end();
// }

bool isSerialPortMonitored(int port) {
    for (int i = 0; i < MAX_SERIAL_MONITOR_MAPPINGS; i++) {
        if (serialMonitorMappings[i].active && serialMonitorMappings[i].inputPort == port) {
            return true;
        }
    }
    return false;
}

bool isSerialPortRawMapped(int port) {
    for (int i = 0; i < MAX_SERIAL_MONITOR_MAPPINGS; i++) {
        if (serialMonitorMappings[i].active && 
            serialMonitorMappings[i].inputPort == port && 
            serialMonitorMappings[i].rawMode) {
            return true;
        }
    }
    return false;
}

void setSerialMappingRawMode(int inputPort, bool raw) {
    for (int i = 0; i < MAX_SERIAL_MONITOR_MAPPINGS; i++) {
        if (serialMonitorMappings[i].active && serialMonitorMappings[i].inputPort == inputPort) {
            serialMonitorMappings[i].rawMode = raw;
            saveSerialMonitorMappings();
            Serial.printf("Serial%d raw mode set to: %s\n", inputPort, raw ? "ENABLED" : "DISABLED");
            return;
        }
    }
    Serial.printf("No mapping found for Serial%d\n", inputPort);
}

void saveSerialMonitorMappings() {
    preferences.begin("serial_map", false);
    
    for (int i = 0; i < MAX_SERIAL_MONITOR_MAPPINGS; i++) {
        String keyActive = "sm" + String(i) + "_act";
        preferences.putBool(keyActive.c_str(), serialMonitorMappings[i].active);
        
        if (serialMonitorMappings[i].active) {
            String keyInput = "sm" + String(i) + "_in";
            String keyCount = "sm" + String(i) + "_cnt";
            String keyRaw = "sm" + String(i) + "_raw";
            String keyPrO = "sm" + String(i) + "_pbo";   // broadcast OUT before the mapping
            String keyPrI = "sm" + String(i) + "_pbi";   // input blocking before the mapping
            
            preferences.putUChar(keyInput.c_str(), serialMonitorMappings[i].inputPort);
            preferences.putUChar(keyCount.c_str(), serialMonitorMappings[i].outputCount);
            preferences.putBool(keyRaw.c_str(), serialMonitorMappings[i].rawMode);
            preferences.putBool(keyPrO.c_str(), serialMonitorMappings[i].prevBroadcastOut);
            preferences.putBool(keyPrI.c_str(), serialMonitorMappings[i].prevBlockIn);
            
            for (int j = 0; j < serialMonitorMappings[i].outputCount; j++) {
                String keyWCB = "sm" + String(i) + "_" + String(j) + "w";
                String keyPort = "sm" + String(i) + "_" + String(j) + "p";
                preferences.putUChar(keyWCB.c_str(), serialMonitorMappings[i].outputs[j].wcbNumber);
                preferences.putUChar(keyPort.c_str(), serialMonitorMappings[i].outputs[j].serialPort);
            }
        }
    }
    
    preferences.end();
}

void removeSerialMonitorMapping(const String &portStr) {
    if (!portStr.startsWith("S") && !portStr.startsWith("s")) {
        Serial.println("Invalid format. Use: ?SMRSx where x=1-5");
        return;
    }
    
    int port = portStr.substring(1).toInt();
    if (port < 1 || port > 5) {
        Serial.println("Invalid port number. Must be 1-5");
        return;
    }
    
    bool found = false;
    for (int i = 0; i < MAX_SERIAL_MONITOR_MAPPINGS; i++) {
        if (serialMonitorMappings[i].active && serialMonitorMappings[i].inputPort == port) {
            serialMonitorMappings[i].active = false;
            serialMonitorMappings[i].inputPort = 0;
            serialMonitorMappings[i].outputCount = 0;
            serialMonitorMappings[i].rawMode = false;
            
            // Clear the output array
            for (int j = 0; j < 10; j++) {
                serialMonitorMappings[i].outputs[j].wcbNumber = 0;
                serialMonitorMappings[i].outputs[j].serialPort = 0;
            }
            
            found = true;
            
            // Put the flags back the way the port had them, not the way the defaults say.
            const bool wantBlockIn = serialMonitorMappings[i].prevBlockIn;
            const bool wantBcstOut = serialMonitorMappings[i].prevBroadcastOut;
            if (blockBroadcastFrom[port - 1] != wantBlockIn) {
                blockBroadcastFrom[port - 1] = wantBlockIn;
                saveBroadcastBlockSettings();
                Serial.printf("Restored broadcast input blocking on Serial%d: %s\n",
                              port, wantBlockIn ? "blocked" : "allowed");
            }
            if (serialBroadcastEnabled[port - 1] != wantBcstOut) {
                serialBroadcastEnabled[port - 1] = wantBcstOut;
                saveBroadcastSettingsToPreferences();
                Serial.printf("Restored broadcast output on Serial%d: %s\n",
                              port, wantBcstOut ? "enabled" : "disabled");
            }
            
            break;
        }
    }
    
    if (found) {
        saveSerialMonitorMappings();
        Serial.printf("Serial mapping removed for Serial%d\n", port);
    } else {
        Serial.printf("No mapping found for Serial%d\n", port);
    }
}

void clearAllSerialMonitorMappings() {
    for (int i = 0; i < MAX_SERIAL_MONITOR_MAPPINGS; i++) {
        if (serialMonitorMappings[i].active) {
            int port = serialMonitorMappings[i].inputPort;

            // Same restore as the single-port CLEAR: put back the flags the mapping overrode,
            // both directions. This used to clear input blocking only, so every port it
            // released stayed silent on broadcast OUTPUT until someone re-enabled it by hand.
            if (port >= 1 && port <= 5) {
                const bool wantBlockIn = serialMonitorMappings[i].prevBlockIn;
                const bool wantBcstOut = serialMonitorMappings[i].prevBroadcastOut;
                if (blockBroadcastFrom[port - 1] != wantBlockIn) {
                    blockBroadcastFrom[port - 1] = wantBlockIn;
                    Serial.printf("Restored broadcast input blocking on Serial%d: %s\n",
                                  port, wantBlockIn ? "blocked" : "allowed");
                }
                if (serialBroadcastEnabled[port - 1] != wantBcstOut) {
                    serialBroadcastEnabled[port - 1] = wantBcstOut;
                    Serial.printf("Restored broadcast output on Serial%d: %s\n",
                                  port, wantBcstOut ? "enabled" : "disabled");
                }
            }
        }
        
        serialMonitorMappings[i].active = false;
        serialMonitorMappings[i].inputPort = 0;
        serialMonitorMappings[i].outputCount = 0;
        serialMonitorMappings[i].rawMode = false;
    }
    
    saveBroadcastBlockSettings();
    saveBroadcastSettingsToPreferences();
    saveSerialMonitorMappings();
    Serial.println("All serial mappings cleared");
}

void listSerialMonitorMappings() {
    bool hasAny = false;
    
    for (int i = 0; i < MAX_SERIAL_MONITOR_MAPPINGS; i++) {
        if (serialMonitorMappings[i].active) {
            hasAny = true;
            
            Serial.printf("Mapping %d: Serial%d%s -> ", 
                i + 1, 
                serialMonitorMappings[i].inputPort,
                serialMonitorMappings[i].rawMode ? " (RAW)" : "");
            
            for (int j = 0; j < serialMonitorMappings[i].outputCount; j++) {
                if (j > 0) Serial.print(", ");
                
                if (serialMonitorMappings[i].outputs[j].wcbNumber == 0) {
                    Serial.printf("S%d", serialMonitorMappings[i].outputs[j].serialPort);
                } else {
                    Serial.printf("W%dS%d", 
                        serialMonitorMappings[i].outputs[j].wcbNumber,
                        serialMonitorMappings[i].outputs[j].serialPort);
                }
            }
            Serial.println();
        }
    }
    
    if (!hasAny) {
        Serial.println("  No serial mappings configured");
    }
}



// ==================== Maestro Configuration Functions ====================

void saveMaestroSettings() {
  preferences.begin("maestro_cfg", false);
  
  for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
    String keyID   = "m" + String(i) + "_id";
    String keyPort = "m" + String(i) + "_port";
    String keyWCB  = "m" + String(i) + "_wcb";
    String keyEn   = "m" + String(i) + "_en";
    String keyBaud = "m" + String(i) + "_baud";
    
    preferences.putUChar(keyID.c_str(),   maestroConfigs[i].maestroID);
    preferences.putUChar(keyPort.c_str(), maestroConfigs[i].serialPort);
    preferences.putUChar(keyWCB.c_str(),  maestroConfigs[i].remoteWCB);
    preferences.putBool(keyEn.c_str(),    maestroConfigs[i].configured);
    preferences.putUInt(keyBaud.c_str(),  maestroConfigs[i].baudRate);
  }
  
  preferences.end();
}

void loadMaestroSettings() {
  preferences.begin("maestro_cfg", true);
  
  for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
    String keyID   = "m" + String(i) + "_id";
    String keyPort = "m" + String(i) + "_port";
    String keyWCB  = "m" + String(i) + "_wcb";
    String keyEn   = "m" + String(i) + "_en";
    String keyBaud = "m" + String(i) + "_baud";
    
    maestroConfigs[i].maestroID  = preferences.getUChar(keyID.c_str(),   0);
    maestroConfigs[i].serialPort = preferences.getUChar(keyPort.c_str(), 0);
    maestroConfigs[i].remoteWCB  = preferences.getUChar(keyWCB.c_str(),  0);
    maestroConfigs[i].configured = preferences.getBool(keyEn.c_str(),    false);
    maestroConfigs[i].baudRate   = preferences.getUInt(keyBaud.c_str(),  9600);
  }

  preferences.end();
  // NOTE: cannot normalize remote-to-self slots here — this runs before
  // loadWCBNumberFromPreferences() at boot, so WCB_Number is still the default.
  // normalizeMaestroSelfSlots() is called from setup() once WCB_Number is known.
}

// Repair a legacy "remote-to-self" Maestro slot (remoteWCB == this board's own
// WCB number). No current config path can create one, but older firmware could
// persist it — and it breaks the Wizard's CLEAR-then-re-add cycle: the backup
// emits it as a local M:W<self>S1, but its stored key is {serialPort=0,
// remoteWCB=self} while CLEAR searches for {serialPort=1, remoteWCB=0}, so CLEAR
// never matches and every push stacks another duplicate Maestro. Collapse it to a
// plain local slot so it obeys the same invariant configure/clear enforce.
// MUST be called AFTER loadWCBNumberFromPreferences(). Persists the one-time repair.
void normalizeMaestroSelfSlots() {
  bool changed = false;
  for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
    if (maestroConfigs[i].configured && maestroConfigs[i].remoteWCB == WCB_Number) {
      maestroConfigs[i].serialPort = maestroConfigs[i].serialPort ? maestroConfigs[i].serialPort : 1;
      maestroConfigs[i].remoteWCB  = 0;
      changed = true;
      Serial.printf("[MAESTRO] repaired legacy remote-to-self slot: M%d → local S%d\n",
                    maestroConfigs[i].maestroID, maestroConfigs[i].serialPort);
    }
  }
  if (changed) saveMaestroSettings();
}

void printMaestroSettings() {
  Serial.println("------- Maestro Settings ----------------------------------");

  bool anyConfigured = false;
  for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
    if (!maestroConfigs[i].configured) continue;
    anyConfigured = true;
    if (maestroConfigs[i].serialPort > 0) {
      // Local Maestro
      Serial.printf("  Maestro %d → Local S%d\n",
                    maestroConfigs[i].maestroID,
                    maestroConfigs[i].serialPort);
    } else if (maestroConfigs[i].remoteWCB > 0) {
      // Remote Maestro — look up port from kyberTargets
      int port = 0;
      for (int j = 0; j < MAX_KYBER_TARGETS; j++) {
        if (kyberTargets[j].enabled && kyberTargets[j].maestroID == maestroConfigs[i].maestroID) {
          port = kyberTargets[j].targetPort;
          break;
        }
      }
      if (port > 0) {
        Serial.printf("  Maestro %d → WCB%d S%d\n",
                      maestroConfigs[i].maestroID,
                      maestroConfigs[i].remoteWCB,
                      port);
      } else {
        Serial.printf("  Maestro %d → WCB%d\n",
                      maestroConfigs[i].maestroID,
                      maestroConfigs[i].remoteWCB);
      }
    }
  }

  if (!anyConfigured) {
    Serial.println("  No Maestros configured");
  }
}


void saveKyberTargets() {
  preferences.begin("kyber_targets", false);
  
  preferences.putBool("use_target", kyberUseTargeting);
  
  for (int i = 0; i < MAX_KYBER_TARGETS; i++) {
    String keyID = "kt" + String(i) + "_id";
    String keyWCB = "kt" + String(i) + "_wcb";
    String keyPort = "kt" + String(i) + "_port";
    String keyEn = "kt" + String(i) + "_en";
    
    preferences.putUChar(keyID.c_str(), kyberTargets[i].maestroID);
    preferences.putUChar(keyWCB.c_str(), kyberTargets[i].targetWCB);
    preferences.putUChar(keyPort.c_str(), kyberTargets[i].targetPort);
    preferences.putBool(keyEn.c_str(), kyberTargets[i].enabled);
  }
  
  preferences.end();
}

void loadKyberTargets() {
  preferences.begin("kyber_targets", true);
  
  kyberUseTargeting = preferences.getBool("use_target", false);
  
  for (int i = 0; i < MAX_KYBER_TARGETS; i++) {
    String keyID = "kt" + String(i) + "_id";
    String keyWCB = "kt" + String(i) + "_wcb";
    String keyPort = "kt" + String(i) + "_port";
    String keyEn = "kt" + String(i) + "_en";
    
    kyberTargets[i].maestroID = preferences.getUChar(keyID.c_str(), 0);
    kyberTargets[i].targetWCB = preferences.getUChar(keyWCB.c_str(), 0);
    kyberTargets[i].targetPort = preferences.getUChar(keyPort.c_str(), 0);
    kyberTargets[i].enabled = preferences.getBool(keyEn.c_str(), false);
  }
  
  preferences.end();
}
void printKyberList() {
  Serial.println("\n--- Kyber Configuration ---");

  if (Kyber_Location != "local" && Kyber_Location != "remote") {
    Serial.println("Kyber is not configured.");
    Serial.println("-----------------------------\n");
    return;
  }

  Serial.printf("Kyber is %s\n", Kyber_Location == "local" ? "Local" : "Remote");
  Serial.printf("Targeting mode: %s\n", kyberUseTargeting ? "Enabled" : "Disabled (Broadcast Mode)");

  if (kyberUseTargeting) {
    Serial.println("\nKyber Targets:");
    for (int i = 0; i < MAX_KYBER_TARGETS; i++) {
      if (kyberTargets[i].enabled) {
        Serial.printf("  Maestro %d → WCB%d S%d\n",
                      kyberTargets[i].maestroID,
                      kyberTargets[i].targetWCB,
                      kyberTargets[i].targetPort);
      }
    }
  }

  if (Kyber_Location == "local" && kyberUseTargeting) {
    Serial.println("\n─────────────────────────────────────────────────────");
    Serial.println("📋 COPY-PASTE SETUP COMMANDS:");
    Serial.println("─────────────────────────────────────────────────────");

    // This WCB's command
    preferences.begin("kyber_settings", true);
    int storedPort = preferences.getInt("K_Port", 2);
    preferences.end();
    
    String thisCmd = "?" + String("KYBER_LOCAL,S") + String(storedPort);
    for (int i = 0; i < MAX_KYBER_TARGETS; i++) {
      if (kyberTargets[i].enabled) {
        thisCmd += ",M" + String(kyberTargets[i].maestroID) +
                   ":W" + String(kyberTargets[i].targetWCB) +
                   "S" + String(kyberTargets[i].targetPort) +
                   ":" + String(baudRates[kyberTargets[i].targetPort - 1]);
      }
    }
    Serial.printf("\nWCB%d (this board):\n%s\n\n", WCB_Number, thisCmd.c_str());

    // Other WCBs
    for (int wcb = 1; wcb <= Default_WCB_Quantity; wcb++) {
      if (wcb == WCB_Number) continue;

      // Only add KYBER_REMOTE if this WCB has Maestro ID 1 or 2 physically on it
      bool needsKyberRemote = false;
      for (int i = 0; i < MAX_KYBER_TARGETS; i++) {
        if (kyberTargets[i].enabled && 
            kyberTargets[i].targetWCB == wcb &&
            kyberTargets[i].maestroID <= 2) {
          needsKyberRemote = true;
          break;
        }
      }

      String cmd = "";
      if (needsKyberRemote) {
        cmd += "?" + String("MAESTRO_REMOTE^?");
      } else {
        cmd += "?";
      }
      cmd += "MAESTRO";

      // Add all Maestro mappings
      for (int i = 0; i < MAX_KYBER_TARGETS; i++) {
        if (kyberTargets[i].enabled) {
          cmd += ",M" + String(kyberTargets[i].maestroID) +
                 ":W" + String(kyberTargets[i].targetWCB) +
                 "S" + String(kyberTargets[i].targetPort) +
                 ":" + String(baudRates[kyberTargets[i].targetPort - 1]);
        }
      }

      // Add label commands only for Maestros local to this WCB
      for (int i = 0; i < MAX_KYBER_TARGETS; i++) {
        if (kyberTargets[i].enabled && kyberTargets[i].targetWCB == wcb) {
          String label = serialPortLabels[kyberTargets[i].targetPort - 1];
          if (label.length() > 0) {
            cmd += "^?" + String("SLS") + String(kyberTargets[i].targetPort) + "," + label;
          }
        }
      }

      cmd += "^?REBOOT";
      Serial.printf("WCB%d:\n%s\n\n", wcb, cmd.c_str());
    }
    Serial.println("─────────────────────────────────────────────────────\n");
  }

  Serial.println("-----------------------------\n");
}

void saveETMSettings() {
    preferences.begin("etm_config", false);
    preferences.putBool("etmEnabled", etmEnabled);
    preferences.putInt("etmBoot", etmBootHeartbeatSec);
    preferences.putInt("etmHB", etmHeartbeatSec);
    preferences.putInt("etmMiss", etmMissedHeartbeats);
    preferences.putInt("etmTimeout", etmTimeoutMs);
    preferences.putInt("etmCharCount", etmCharMessageCount);
    preferences.putInt("etmCharDelay", etmCharDelayMs);
    preferences.putBool("etmChksm", etmChecksumEnabled);
    preferences.end();
}

void loadETMSettings() {
    preferences.begin("etm_config", true);
    etmEnabled          = preferences.getBool("etmEnabled", true);
    etmBootHeartbeatSec = preferences.getInt("etmBoot", 2);
    etmHeartbeatSec     = preferences.getInt("etmHB", 10);
    // Default MUST match the compile-time initialiser in WCB.ino (5). It was 3, so every board
    // that had never explicitly run ?ETM,MISS silently reverted the deliberately-widened 55 s
    // offline window back to 33 s at every boot — the comment on the initialiser described
    // behaviour that never shipped. (The Wizard defaults were corrected to match.)
    etmMissedHeartbeats = preferences.getInt("etmMiss", 5);
    etmTimeoutMs        = preferences.getInt("etmTimeout", 500);
    etmCharMessageCount = preferences.getInt("etmCharCount", 20);
    etmCharDelayMs      = preferences.getInt("etmCharDelay", 100);
    etmChecksumEnabled = preferences.getBool("etmChksm", true);
    preferences.end();

    // Heal a board that already holds a value written before the setters were range-checked:
    // a bare ?ETM,TIMEOUT / ?ETM,BOOT / ?ETMMISS used to persist 0, and 0 here means no retry
    // window, no boot window, and every peer permanently offline. Same belt-and-braces the
    // heartbeat path already carries for a value restored from an older NVS blob.
    if (etmTimeoutMs        < 50 || etmTimeoutMs        > 10000) etmTimeoutMs        = 500;
    if (etmBootHeartbeatSec < 1  || etmBootHeartbeatSec > 30)    etmBootHeartbeatSec = 2;
    if (etmMissedHeartbeats < 1  || etmMissedHeartbeats > 100)   etmMissedHeartbeats = 5;
    if (etmCharDelayMs      < 0  || etmCharDelayMs      > 5000)  etmCharDelayMs      = 100;
    if (etmEnabled) {
        Serial.println("ETM: ENABLED ⚠️  Ensure all boards match firmware!");
    }
}
