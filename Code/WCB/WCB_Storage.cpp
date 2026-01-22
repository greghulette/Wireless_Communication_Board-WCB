#include <sys/_types.h>
#include "WCB_Storage.h"
#include <Preferences.h>
#include "WCB_PWM.h"
// Declare the external variables that are defined in the main sketch
extern Preferences preferences;
extern unsigned long baudRates[5];
extern bool serialBroadcastEnabled[5];
extern char LocalFunctionIdentifier;
extern char CommandCharacter;
extern char commandDelimiter;
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
SerialMonitorMapping serialMonitorMappings[MAX_SERIAL_MONITOR_MAPPINGS];

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
        // Check if port is reserved for PWM output
        if (isSerialPortPWMOutput(i + 1)) {
            Serial.printf(" Serial%d: Reserved for PWM Output", i + 1);
            // Add label if it exists
            if (serialPortLabels[i].length() > 0) {
                Serial.printf(" (%s)", serialPortLabels[i].c_str());
            }
            Serial.println();
            continue;
        }
        
        // Check if port is reserved for PWM input
        if (isSerialPortUsedForPWMInput(i + 1)) {
            Serial.printf(" Serial%d: Reserved for PWM Input", i + 1);
            // Add label if it exists
            if (serialPortLabels[i].length() > 0) {
                Serial.printf(" (%s)", serialPortLabels[i].c_str());
            }
            Serial.println();
            continue;
        }
        
        // Normal serial port - show baud and broadcast settings
        // Determine broadcast input status (blocked or enabled)
        String inputStatus = blockBroadcastFrom[i] ? "Disabled" : "Enabled";
        
        // Determine broadcast output status
        String outputStatus = serialBroadcastEnabled[i] ? "Enabled" : "Disabled";
        
        Serial.printf(" Serial%d Baud Rate: %lu, Broadcast Input: %s, Broadcast Output: %s",
                      i + 1, baudRates[i], inputStatus.c_str(), outputStatus.c_str());
        
        // Add label if it exists
        if (serialPortLabels[i].length() > 0) {
            Serial.printf(" (%s)", serialPortLabels[i].c_str());
        }
        // Add auto-detected labels for Kyber
        else if ((i == 0 && (Kyber_Local || Kyber_Remote))) {
            Serial.print(" (Maestro)");
        } else if (i == 1 && Kyber_Local) {
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

// Load stored commands from preferences
void loadStoredCommandsFromPreferences() {
    preferences.begin("stored_commands", true);
    for (int i = 0; i < MAX_STORED_COMMANDS; i++) {
        char storedCommandBuffer[1800] = {0};
        String key = "CMD" + String(i + 1);
        preferences.getString(key.c_str(), storedCommandBuffer, sizeof(storedCommandBuffer));
        storedCommands[i] = String(storedCommandBuffer);
    }
    preferences.end();

}
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

    // Enqueue for execution
    // parseCommandsAndEnqueue(recalledCommand, sourceID);
    if (isTimerCommand(recalledCommand)) {
  parseCommandGroups(recalledCommand);
} else {
  parseCommandsAndEnqueue(recalledCommand, sourceID);
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

    preferences.begin("stored_commands", false);
    preferences.clear();
    preferences.end();
      
    preferences.begin("kyber_settings", false);
    preferences.clear();
    preferences.end();

    preferences.begin("hw_version", false);
    preferences.clear();
    preferences.end();

    preferences.begin("serial_labels", false);
    preferences.clear();
    preferences.end();

    preferences.begin("serial_monitor", false);
    preferences.clear();
    preferences.end();

    preferences.begin("bcast_block", false);
    preferences.clear();
    preferences.end();
    
    clearAllPWMMappings();

    Serial.println("NVS cleared. Restarting...");
    delay(2000);
    ESP.restart();
}

void storeKyberSettings(const String &message){
  Serial.println(message);
  if (message.equals("local")){
    Kyber_Local = true;
    Kyber_Remote = false;
    Kyber_Location = "local";
  } else if (message.equals("remote")){
    Kyber_Local = false;
    Kyber_Remote = true;
    Kyber_Location = "remote";
  } else if (message.equals("clear")){
    Kyber_Location = "";
    Kyber_Local = false;
    Kyber_Remote =false;
  }
  preferences.begin("kyber_settings", false);
  preferences.putString("K_Location", Kyber_Location);
  preferences.end();
    Serial.printf("Saving stored location: %s\n", Kyber_Location);

};


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


void printKyberSettings(){
  String temp;
  // Serial.printf("REcalling stored location: %s\n", temp);
  if (Kyber_Location == "local"){
      temp = "Local";
      Serial.printf("Kyber is %s\n", temp);
  } else if (Kyber_Location == "remote"){
      temp = "Remote";
      Serial.printf("Kyber is %s\n", temp);
  } else if (Kyber_Location == " "){
            temp = "Not used";
  }
  // Serial.printf("Kyber is %s\n", temp);
};

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
    // Format: ?SM3,S1,W3S3,S0,W2S0
    // message = "3,S1,W3S3,S0,W2S0"
    
    int firstComma = message.indexOf(',');
    if (firstComma == -1) {
        Serial.println("Invalid format. Use: ?SMx,dest1,dest2,... where x=1-5");
        return;
    }
    
    int inputPort = message.substring(0, firstComma).toInt();
    if (inputPort < 1 || inputPort > 5) {
        Serial.println("Invalid input port. Must be 1-5");
        return;
    }
    
    // Find or create mapping slot
    int slotIndex = -1;
    for (int i = 0; i < MAX_SERIAL_MONITOR_MAPPINGS; i++) {
        if (serialMonitorMappings[i].active && serialMonitorMappings[i].inputPort == inputPort) {
            slotIndex = i;
            break;
        }
    }
    
    if (slotIndex == -1) {
        // Find empty slot
        for (int i = 0; i < MAX_SERIAL_MONITOR_MAPPINGS; i++) {
            if (!serialMonitorMappings[i].active) {
                slotIndex = i;
                break;
            }
        }
    }
    
    if (slotIndex == -1) {
        Serial.println("No available mapping slots!");
        return;
    }
    
    // Clear existing mapping
    serialMonitorMappings[slotIndex].active = false;
    serialMonitorMappings[slotIndex].inputPort = inputPort;
    serialMonitorMappings[slotIndex].outputCount = 0;
    
    // Parse outputs
    String remaining = message.substring(firstComma + 1);
    int outputIndex = 0;
    int startIdx = 0;
    
    while (startIdx < remaining.length() && outputIndex < 10) {
        int nextComma = remaining.indexOf(',', startIdx);
        String output = (nextComma == -1) ? remaining.substring(startIdx) : remaining.substring(startIdx, nextComma);
        output.trim();
        
        if (output.length() > 0) {
            uint8_t wcbNum = 0;
            uint8_t serialNum = 0;
            
            // Parse format: "S1" or "W3S3" or "S0" (USB)
            if (output.startsWith("W") || output.startsWith("w")) {
                int sPos = output.indexOf('S', 1);
                if (sPos == -1) sPos = output.indexOf('s', 1);
                if (sPos != -1) {
                    wcbNum = output.substring(1, sPos).toInt();
                    serialNum = output.substring(sPos + 1).toInt();
                }
            } else if (output.startsWith("S") || output.startsWith("s")) {
                wcbNum = 0;  // Local
                serialNum = output.substring(1).toInt();
            }
            
            if (wcbNum <= 9 && serialNum <= 5) {
                serialMonitorMappings[slotIndex].outputs[outputIndex].wcbNumber = wcbNum;
                serialMonitorMappings[slotIndex].outputs[outputIndex].serialPort = serialNum;
                outputIndex++;
            }
        }
        
        if (nextComma == -1) break;
        startIdx = nextComma + 1;
    }
    
    serialMonitorMappings[slotIndex].outputCount = outputIndex;
    serialMonitorMappings[slotIndex].active = true;
    
    saveSerialMonitorMappings();
    
    Serial.printf("Serial Monitor mapping added for Serial%d -> %d destinations\n", inputPort, outputIndex);
    listSerialMonitorMappings();
}

void removeSerialMonitorMapping(int port) {
    if (port < 1 || port > 5) {
        Serial.println("Invalid port. Must be 1-5");
        return;
    }
    
    for (int i = 0; i < MAX_SERIAL_MONITOR_MAPPINGS; i++) {
        if (serialMonitorMappings[i].active && serialMonitorMappings[i].inputPort == port) {
            serialMonitorMappings[i].active = false;
            saveSerialMonitorMappings();
            Serial.printf("Removed Serial Monitor mapping for Serial%d\n", port);
            return;
        }
    }
    
    Serial.printf("No Serial Monitor mapping found for Serial%d\n", port);
}

void clearAllSerialMonitorMappings() {
    for (int i = 0; i < MAX_SERIAL_MONITOR_MAPPINGS; i++) {
        serialMonitorMappings[i].active = false;
    }
    
    preferences.begin("serial_monitor", false);
    preferences.clear();
    preferences.end();
    
    Serial.println("All Serial Monitor mappings cleared");
}

void listSerialMonitorMappings() {
    bool found = false;
    
    Serial.println("\n--- Serial Monitor Mappings ---");
    for (int i = 0; i < MAX_SERIAL_MONITOR_MAPPINGS; i++) {
        if (serialMonitorMappings[i].active) {
            found = true;
            Serial.printf("Serial%d monitors to: ", serialMonitorMappings[i].inputPort);
            
            for (int j = 0; j < serialMonitorMappings[i].outputCount; j++) {
                if (j > 0) Serial.print(", ");
                
                if (serialMonitorMappings[i].outputs[j].wcbNumber == 0) {
                    if (serialMonitorMappings[i].outputs[j].serialPort == 0) {
                        Serial.print("Local USB");
                    } else {
                        Serial.printf("Local Serial%d", serialMonitorMappings[i].outputs[j].serialPort);
                    }
                } else {
                    if (serialMonitorMappings[i].outputs[j].serialPort == 0) {
                        Serial.printf("WCB%d USB", serialMonitorMappings[i].outputs[j].wcbNumber);
                    } else {
                        Serial.printf("WCB%d Serial%d", 
                            serialMonitorMappings[i].outputs[j].wcbNumber,
                            serialMonitorMappings[i].outputs[j].serialPort);
                    }
                }
            }
            Serial.println();
        }
    }
    
    if (!found) {
        Serial.println("No Serial Monitor mappings configured");
    }
    Serial.println("--- End of Mappings ---\n");
}

void saveSerialMonitorMappings() {
    preferences.begin("serial_monitor", false);
    
    for (int i = 0; i < MAX_SERIAL_MONITOR_MAPPINGS; i++) {
        String keyActive = "sm" + String(i) + "_act";
        String keyInput = "sm" + String(i) + "_in";
        String keyCount = "sm" + String(i) + "_cnt";
        
        preferences.putBool(keyActive.c_str(), serialMonitorMappings[i].active);
        
        if (serialMonitorMappings[i].active) {
            preferences.putUChar(keyInput.c_str(), serialMonitorMappings[i].inputPort);
            preferences.putUChar(keyCount.c_str(), serialMonitorMappings[i].outputCount);
            
            for (int j = 0; j < serialMonitorMappings[i].outputCount; j++) {
                String keyWCB = "sm" + String(i) + "_" + String(j) + "w";
                String keyPort = "sm" + String(i) + "_" + String(j) + "p";
                preferences.putUChar(keyWCB.c_str(), serialMonitorMappings[i].outputs[j].wcbNumber);
                preferences.putUChar(keyPort.c_str(), serialMonitorMappings[i].outputs[j].serialPort);
            }
        }
    }
    
    preferences.end();
    Serial.println("Serial Monitor mappings saved to NVS");
}

void loadSerialMonitorMappings() {
    preferences.begin("serial_monitor", true);
    
    for (int i = 0; i < MAX_SERIAL_MONITOR_MAPPINGS; i++) {
        String keyActive = "sm" + String(i) + "_act";
        serialMonitorMappings[i].active = preferences.getBool(keyActive.c_str(), false);
        
        if (serialMonitorMappings[i].active) {
            String keyInput = "sm" + String(i) + "_in";
            String keyCount = "sm" + String(i) + "_cnt";
            
            serialMonitorMappings[i].inputPort = preferences.getUChar(keyInput.c_str(), 0);
            serialMonitorMappings[i].outputCount = preferences.getUChar(keyCount.c_str(), 0);
            
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

bool isSerialPortMonitored(int port) {
    for (int i = 0; i < MAX_SERIAL_MONITOR_MAPPINGS; i++) {
        if (serialMonitorMappings[i].active && serialMonitorMappings[i].inputPort == port) {
            return true;
        }
    }
    return false;
}