#include <sys/_types.h>
#include "WCB_Storage.h"
#include <Preferences.h>
#include "WCB_PWM.h"
#include "WCB_Maestro.h"  // For MAX_MAESTROS_PER_WCB and maestroConfigs
// Declare the external variables that are defined in the main sketch
extern Preferences preferences;
extern unsigned long baudRates[5];
extern bool serialBroadcastEnabled[5];
extern char LocalFunctionIdentifier;
extern char CommandCharacter;
extern char commandDelimiter;
extern String commentDelimiter;
extern int Default_WCB_Quantity;
extern int WCB_Number;
extern uint8_t umac_oct2;
extern uint8_t umac_oct3;
extern char espnowPassword[40];
extern String storedCommands[MAX_STORED_COMMANDS];
extern int wcb_hw_version;
extern char espnowPassword[40];
extern void updatePinMap();
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
  } else {
    Serial.println("No valid HW version identified.");
  }
  // updatePinMap();
}

void loadHWversion(){
preferences.begin("hw_version", true);
wcb_hw_version =  preferences.getInt("hw_version", 0);
// Serial.printf("Pin out version %d\n", wcb_hw_version);
preferences.end();
updatePinMap();
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
  if (!(baud == 110 || baud == 300 || baud == 600 || baud == 1200 ||
        baud == 2400 || baud == 9600 || baud == 14400 || baud == 19200 || baud == 38400 ||
        baud == 57600 || baud == 115200 || baud == 128000 || baud == 256000)) {
    Serial.println("Invalid baud rate");
    return;
  }

  // Hardware vs Software serial
  if (port == 1) {
    Serial1.updateBaudRate(baud);
  } else if (port == 2) {
    Serial2.updateBaudRate(baud);
  } else {
    // Serial.printf("Invalid serial port: %d\n", port);
    // return;
  }

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
    Serial.printf("Changed WCB Number to: %d\nPlease reboot to take effect\n", wcb_number_f);
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

void recallBaudRatefromSerial(int ser){
  preferences.begin("serial_baud", true);
  String key = "Serial" + String(ser);
  storedBaudRate[ser] = preferences.getInt(key.c_str(), storedBaudRate[ser]);
  preferences.end();
}

void setBaudRateForSerial(int ser){
  preferences.begin("serial_baud", false);
  String key = "Serial" + String(ser);
  storedBaudRate[ser] = preferences.putInt(key.c_str(), storedBaudRate[ser]);
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
        // Auto-detected labels for Kyber/Maestro if no user label
        else if (i == 0 && (Kyber_Local || Kyber_Remote)) {
            bool hasMaestroOnS1 = false;
            for (int m = 0; m < MAX_MAESTROS_PER_WCB; m++) {
                if (maestroConfigs[m].configured && maestroConfigs[m].serialPort == 1) {
                    hasMaestroOnS1 = true;
                    break;
                }
            }
            if (hasMaestroOnS1) Serial.print(" (Maestro)");
        }
        else if (i == 1 && Kyber_Local) {
            Serial.print(" (Kyber)");
        }

        Serial.println();
    }
}

void saveBroadcastSettingsToPreferences() {
    preferences.begin("bdcst_set", false);
    Serial.println("DEBUG: Saving Broadcast Setting:");
    for (int i = 0; i < 5; i++) {
        String key = "S" + String(i + 1);
        int value = serialBroadcastEnabled[i] ? 1 : 0;
        size_t result = preferences.putInt(key.c_str(), value);
        // Serial.printf("Broadcast setting for Serial%d saved as %d\n", i + 1, value);
        // Serial.printf("  %s = %d (bytes written: %d)\n", key.c_str(), value, result);
    }
    preferences.end();
}

void loadBroadcastSettingsFromPreferences() {
    preferences.begin("bdcst_set", true);
    for (int i = 0; i < 5; i++) {
        String key = "S" + String(i + 1);
        int value = preferences.getInt(key.c_str(), 1);  // default = 1 (enabled)
        serialBroadcastEnabled[i] = (value == 1);
    }
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
    Serial.println("Done. Please reboot.");
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

// Save the WCB quantity to preferences
void saveWCBQuantityPreferences(int quantity) {
    preferences.begin("wcb_config", false);
    preferences.putInt("wcb_quantity", quantity);
    preferences.end();
    Default_WCB_Quantity = quantity;
    Serial.printf("Saved new WCB Quantities to: %d.  Please reboot to take effect\n", Default_WCB_Quantity);
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
// Recall a Stored Command
void recallCommandSlot(const String &key, int sourceID) {
    preferences.begin("stored_cmds", true);
    String recalledCommand = preferences.getString(key.c_str(), "");
    preferences.end();

    if (recalledCommand.isEmpty()) {
        Serial.printf("No command stored under key: '%s'\n", key.c_str());
        return;
    }

    Serial.printf("Recalling command for key '%s': %s\n", key.c_str(), recalledCommand.c_str());

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
        Serial.println("Sequence contained only comments — nothing to execute.");
        return;
    }

    // Enqueue for execution
    if (isTimerCommand(stripped)) {
        parseCommandGroups(stripped);
    } else {
        parseCommandsAndEnqueue(stripped, sourceID);
    }

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
  key.trim();
  value.trim();

  if (key.length() == 0 || value.length() == 0) {
    Serial.println("Key or value cannot be empty.");
    return;
  }

  preferences.begin("stored_cmds", false);
  preferences.putString(key.c_str(), value);

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
  Serial.printf("Stored: Key='%s', Value='%s'\n", key.c_str(), value.c_str());
}


void eraseStoredCommandByName(const String &name) {
    if (name.length() == 0) {
        Serial.println("Command name cannot be empty.");
        return;
    }

    preferences.begin("stored_cmds", false);

    // Step 1: Remove the key from NVS
    bool removed = preferences.remove(name.c_str());

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

// Clear all stored commands
void clearAllStoredCommands() {
  preferences.begin("stored_cmds", false);
    preferences.clear();
    preferences.end();   
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
    
    clearAllPWMMappings();

    Serial.println("NVS cleared. Restarting...");
    delay(2000);
    ESP.restart();
}

void storeKyberSettings(const String &message) {
  int firstComma = message.indexOf(',');
  String baseCommand;
  String params = "";
  int kyberPort = 0;
  
  if (firstComma == -1) {
    baseCommand = message;
    baseCommand.toLowerCase();
    kyberUseTargeting = false;
    kyberPort = 2;
    
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
  kyberPort = portStr.toInt();
  kyberUseTargeting = true;
} else {
  Serial.println("Invalid format. Use: ?KYBER,LOCAL,Sx or ?KYBER,LOCAL,Sx,M1:W1S1:57600");
  return;
}
  } 
  if (baseCommand.equals("local") && kyberUseTargeting && (kyberPort < 1 || kyberPort > 5)) {
    Serial.println("Invalid Kyber port. Must be S1-S5");
    return;
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
    Kyber_Local = true;
    Kyber_Remote = false;
    Kyber_Location = "local";
    Serial.printf("Kyber is LOCAL on Serial%d\n", kyberPort);
    
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
    Kyber_Local = false;
    Kyber_Remote = true;
    Kyber_Location = "remote";
    Serial.println("Kyber is REMOTE (on another WCB)");
    
  } else if (baseCommand.equals("clear")) {
    Kyber_Location = " ";
    Kyber_Local = false;
    Kyber_Remote = false;
    kyberUseTargeting = false;
    
    if (kyberPort > 0) {
      updateBaudRate(kyberPort, 9600);
      Serial.printf("✓ Reset S%d baud rate to 9600 (Kyber port)\n", kyberPort);
    }
    
    preferences.begin("kyber_settings", false);
    preferences.putString("K_Location", Kyber_Location);
    preferences.end();

    if (!serialBroadcastEnabled[kyberPort - 1]) {
      serialBroadcastEnabled[kyberPort - 1] = true;
      saveBroadcastSettingsToPreferences();
      Serial.printf("✓ Re-enabled broadcast output on S%d\n", kyberPort);
    }
    if (blockBroadcastFrom[kyberPort - 1]) {
      blockBroadcastFrom[kyberPort - 1] = false;
      saveBroadcastBlockSettings();
      Serial.printf("✓ Re-enabled broadcast input on S%d\n", kyberPort);
    }
    saveKyberTargets();
    Serial.println("Kyber cleared. Run ?MAESTRO_DEFAULT to clear Maestro configs.");
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
          
          if (maestroID >= 1 && maestroID <= 9 &&
              wcbNum >= 1 && wcbNum <= Default_WCB_Quantity &&
              portNum >= 1 && portNum <= 5 &&
              (baudRate == 9600 || baudRate == 14400 || baudRate == 19200 || 
               baudRate == 38400 || baudRate == 57600 || baudRate == 115200)) {
            
            kyberTargets[targetIndex].maestroID = maestroID;
            kyberTargets[targetIndex].targetWCB = wcbNum;
            kyberTargets[targetIndex].targetPort = portNum;
            kyberTargets[targetIndex].enabled = true;
            
            Serial.printf("Kyber target %d: Maestro %d → WCB%d S%d (%d baud)\n",
                          targetIndex + 1, maestroID, wcbNum, portNum, baudRate);
            
            // Auto-configure Maestro
            int8_t maestroSlot = findSlotByMaestroID(maestroID);
            if (maestroSlot < 0) {
              maestroSlot = findEmptySlot();
            }
            
            if (maestroSlot >= 0) {
              maestroConfigs[maestroSlot].maestroID = maestroID;
              maestroConfigs[maestroSlot].configured = true;
              
              if (wcbNum == WCB_Number) {
                maestroConfigs[maestroSlot].serialPort = portNum;
                maestroConfigs[maestroSlot].remoteWCB = 0;
                
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
                Serial.printf("✓ Maestro %d: Remote on WCB%d\n", maestroID, wcbNum);
              }
            }
            
            targetIndex++;
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
        
        String command = "";
        if (needsKyberRemote) {
          command += "?" + String("KYBER_REMOTE^?");
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
  
  preferences.begin("kyber_settings", false);
  preferences.putString("K_Location", Kyber_Location);
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
  preferences.end();

  if (Kyber_Location == "local"){
      Kyber_Local = true;
      Kyber_Remote = false;
  } else if (Kyber_Location == "remote"){
      Kyber_Local = false;
      Kyber_Remote = true;
  } else if (Kyber_Location == ""){
      Kyber_Local = false;
      Kyber_Remote = false;
  }
};


void printKyberSettings() {
  String temp;
  
  if (Kyber_Location == "local") {
    temp = "Local";
  } else if (Kyber_Location == "remote") {
    temp = "Remote";
  } else {
    temp = "Not used";
  }
  
  Serial.printf("Kyber is %s\n", temp.c_str());
  
  if (Kyber_Local || Kyber_Remote) {
    if (kyberUseTargeting) {
      Serial.println("Kyber targeting mode: Enabled");
      Serial.println("Kyber targets:");
      
      bool anyTargets = false;
      for (int i = 0; i < MAX_KYBER_TARGETS; i++) {
        if (kyberTargets[i].enabled) {
          Serial.printf("  Maestro %d → WCB%d S%d\n",
                        kyberTargets[i].maestroID,
                        kyberTargets[i].targetWCB,
                        kyberTargets[i].targetPort);
          anyTargets = true;
        }
      }
      
      if (!anyTargets) {
        Serial.println("  (No targets configured)");
      }
    } else {
      Serial.println("Kyber targeting mode: Disabled (Broadcast Mode)");
    }
  }
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
    Serial.println("DEBUG: Saving Broadcast Setting:");
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

    // Find or create mapping for this input port
    SerialMonitorMapping *mapping = nullptr;
    for (int i = 0; i < MAX_SERIAL_MONITOR_MAPPINGS; i++) {
        if (serialMonitorMappings[i].active && serialMonitorMappings[i].inputPort == inputPort) {
            mapping = &serialMonitorMappings[i];
            break;
        }
    }

    if (!mapping) {
        for (int i = 0; i < MAX_SERIAL_MONITOR_MAPPINGS; i++) {
            if (!serialMonitorMappings[i].active) {
                mapping = &serialMonitorMappings[i];
                mapping->active = true;
                mapping->inputPort = inputPort;
                mapping->outputCount = 0;
                mapping->rawMode = inputRawMode;
                break;
            }
        }
    }

    if (!mapping) {
        Serial.println("Maximum serial mappings reached");
        return;
    }

    // Update raw mode if different
    if (mapping->rawMode != inputRawMode) {
        mapping->rawMode = inputRawMode;
    }

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

            if (wcbNum > 9 || serialPort < 0 || serialPort > 5) {
                Serial.printf("Invalid destination: WCB %d Serial %d\n", wcbNum, serialPort);
                continue;
            }

            // Check for duplicate before adding
            if (mappingDestinationExists(mapping, wcbNum, serialPort)) {
                Serial.printf("Mapping already exists: Serial%d -> %s%d - skipping duplicate\n",
                             inputPort,
                             wcbNum == 0 ? "S" : ("W" + String(wcbNum) + "S").c_str(),
                             serialPort);
                continue;  // Skip this destination
            }

        // Add output
        if (mapping->outputCount < 10) {
            mapping->outputs[mapping->outputCount].wcbNumber = wcbNum;
            mapping->outputs[mapping->outputCount].serialPort = serialPort;
            mapping->outputCount++;
            outputsAdded++;

            // Automatically disable BOTH broadcast input and output for this port
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
        }
    }

    if (outputsAdded > 0) {
        saveSerialMonitorMappings();
        Serial.printf("Serial mapping updated: Serial%d%s -> %d new destination(s) added (total: %d)\n",
                      inputPort, inputRawMode ? " (RAW)" : "", outputsAdded, mapping->outputCount);
    } else {
        Serial.println("No new destinations added (all were duplicates or invalid)");
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
            
            preferences.putUChar(keyInput.c_str(), serialMonitorMappings[i].inputPort);
            preferences.putUChar(keyCount.c_str(), serialMonitorMappings[i].outputCount);
            preferences.putBool(keyRaw.c_str(), serialMonitorMappings[i].rawMode);
            
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
            
            // Re-enable broadcast input for this port
            if (blockBroadcastFrom[port - 1]) {
                blockBroadcastFrom[port - 1] = false;
                saveBroadcastBlockSettings();
                Serial.printf("Auto-disabled broadcast input blocking on Serial%d\n", port);
            }
            
            // Re-enable broadcast output for this port
            if (!serialBroadcastEnabled[port - 1]) {
                serialBroadcastEnabled[port - 1] = true;
                saveBroadcastSettingsToPreferences();
                Serial.printf("Auto-enabled broadcast output on Serial%d\n", port);
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
            
            // Re-enable broadcast for each port
            if (blockBroadcastFrom[port - 1]) {
                blockBroadcastFrom[port - 1] = false;
            }
        }
        
        serialMonitorMappings[i].active = false;
        serialMonitorMappings[i].inputPort = 0;
        serialMonitorMappings[i].outputCount = 0;
        serialMonitorMappings[i].rawMode = false;
    }
    
    saveBroadcastBlockSettings();
    saveSerialMonitorMappings();
    Serial.println("All serial mappings cleared");
}

void listSerialMonitorMappings() {
    bool hasAny = false;
    
    Serial.println("\n--- Serial Mappings ---");
    
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
        Serial.println("No serial mappings configured");
    }
    
    Serial.println("--- End of Mappings ---");
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
}

void printMaestroSettings() {
  Serial.println("\n--- Maestro Configuration ---");
  
  bool anyConfigured = false;
  int localCount = 0;
  int remoteCount = 0;
  String chainedCommands = "";
  
  // Build universal config string
  for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
    if (maestroConfigs[i].configured) {
      anyConfigured = true;
      
      if (chainedCommands.length() > 0) {
        chainedCommands += String(commandDelimiter);
      }
      
      if (maestroConfigs[i].serialPort > 0) {
        // Local Maestro
        chainedCommands += String(LocalFunctionIdentifier) + "MAESTRO,M" + 
                          String(maestroConfigs[i].maestroID) + 
                          ":W" + String(WCB_Number) + 
                          "S" + String(maestroConfigs[i].serialPort) + 
                          ":" + String(maestroConfigs[i].baudRate);
        // Add label if set
        String portLabel = serialPortLabels[maestroConfigs[i].serialPort - 1];
        if (portLabel.length() > 0) {
          chainedCommands += String(commandDelimiter) + 
                             String(LocalFunctionIdentifier) + "SLS" + 
                             String(maestroConfigs[i].serialPort) + "," + portLabel;
        }
        localCount++;
      } else if (maestroConfigs[i].remoteWCB > 0) {
        // Remote Maestro
        chainedCommands += String(LocalFunctionIdentifier) + "MAESTRO,M" + 
                          String(maestroConfigs[i].maestroID) + 
                          ":W" + String(maestroConfigs[i].remoteWCB) + 
                          "S1:" + String(maestroConfigs[i].baudRate);
        remoteCount++;
      }
    }
  }
  
  if (!anyConfigured) {
    Serial.println("No Maestros configured");
    Serial.println("Using legacy routing (Maestro ID = WCB Number on S1)");
  } else {
    Serial.println("Copy/paste this to configure all WCBs:");
    Serial.println("");
    Serial.println(chainedCommands);
    Serial.println("");
    Serial.printf("Total: %d configured (%d local, %d remote)\n", 
                  localCount + remoteCount, localCount, remoteCount);
    
    // Individual breakdown
    Serial.println("");
    Serial.println("Breakdown:");
    for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
    if (maestroConfigs[i].configured) {
        if (maestroConfigs[i].serialPort > 0) {
        String portLabel = serialPortLabels[maestroConfigs[i].serialPort - 1];
        Serial.printf("  Maestro %d: Local S%d at %lu baud%s", 
                        maestroConfigs[i].maestroID,
                        maestroConfigs[i].serialPort,
                        baudRates[maestroConfigs[i].serialPort - 1],
                        portLabel.length() > 0 ? (" (" + portLabel + ")").c_str() : "");
        
        // **SHOW BROADCAST STATUS**
        bool outputDisabled = !serialBroadcastEnabled[maestroConfigs[i].serialPort - 1];
        bool inputDisabled = blockBroadcastFrom[maestroConfigs[i].serialPort - 1];

        if (outputDisabled && inputDisabled) {
        Serial.print(" [Broadcast I/O: Disabled]");
        } else if (!outputDisabled && !inputDisabled) {
        Serial.print(" [⚠️  Broadcast I/O: Enabled - May interfere!]");
        } else {
        Serial.print(" [⚠️  Broadcast partial: ");
        if (!outputDisabled) Serial.print("Output ON ");
        if (!inputDisabled) Serial.print("Input ON");
        Serial.print("]");
        }
        Serial.println();
        } else if (maestroConfigs[i].remoteWCB > 0) {
        Serial.printf("  Maestro %d: Remote on WCB%d (unicast)\n", 
                        maestroConfigs[i].maestroID,
                        maestroConfigs[i].remoteWCB);
        }
    }
    }
  }
  
  Serial.println("-----------------------------\n");
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
        cmd += "?" + String("KYBER_REMOTE^?");
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
    etmMissedHeartbeats = preferences.getInt("etmMiss", 3);
    etmTimeoutMs        = preferences.getInt("etmTimeout", 500);
    etmCharMessageCount = preferences.getInt("etmCharCount", 20);
    etmCharDelayMs      = preferences.getInt("etmCharDelay", 100);
    etmChecksumEnabled = preferences.getBool("etmChksm", false);
    preferences.end();
    if (etmEnabled) {
        Serial.println("ETM: ENABLED ⚠️  Ensure all boards match firmware!");
    }
}
