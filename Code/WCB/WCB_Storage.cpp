#include <sys/_types.h>
#include "WCB_Storage.h"
#include <Preferences.h>

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
  } else {
    Serial.println("SET YOUR HARDWARE VERSION BEFORE PROCEEDING!!");
  }
}

void updateBaudRate(int port, int baud) {
  if (!(baud == 0 || baud == 110 || baud == 300 || baud == 600 || baud == 1200 ||
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
    Serial.printf("Invalid serial port: %d\n", port);
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
        Serial.printf(" Serial%d Baud Rate: %d,  Broadcast: %s\n",
                      i + 1, baudRates[i], serialBroadcastEnabled[i] ? "Enabled" : "Disabled");
    }
}

void saveBroadcastSettingsToPreferences() {
    preferences.begin("bdcst_set", false);
    Serial.println("DEBUG: Saving broadcast (INT):");
    for (int i = 0; i < 5; i++) {
        String key = "S" + String(i + 1);
        int value = serialBroadcastEnabled[i] ? 1 : 0;
        size_t result = preferences.putInt(key.c_str(), value);
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


    Serial.println("--- End of Stored Commands ---\n");
}

// Clear all stored commands
void clearAllStoredCommands() {
  preferences.begin("stored_cmds", false);
    preferences.clear();
    preferences.end();   
    // for (int i = 0; i < MAX_STORED_COMMANDS; i++) {
    //     storedCommands[i] = "";
    // }
    // saveStoredCommandsToPreferences();
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
    Kyber_Location = " ";
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


