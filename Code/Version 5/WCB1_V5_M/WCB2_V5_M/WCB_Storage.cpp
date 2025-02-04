#include "WCB_Storage.h"
#include <Preferences.h>

// Declare the external variables that are defined in the main sketch
extern Preferences preferences;
extern int baudRates[5];
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

// ==================== Load & Save Functions ====================
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
    return;
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
    Serial.printf("Loaded WCB Number: %d\n", WCB_Number);
}

void saveWCBNumberToPreferences(int wcb_number_f) {
    preferences.begin("wcb_config", false);
    preferences.putInt("WCB_Number", wcb_number_f);
    preferences.end();
    Serial.printf("Changed WCB Number to: %d\nPlease reboot to take effect\n", wcb_number_f);
}
// Recall a Stored Command
void recallCommandSlot(int slot, int sourceID) {
  if (slot < 1 || slot > MAX_STORED_COMMANDS) {
    Serial.println("Invalid command slot number.");
    return;
  }
  String recalledCommand = storedCommands[slot - 1];
  if (recalledCommand.length() == 0) {
    Serial.println("No command stored in the requested slot.");
    return;
  }
  Serial.printf("Recalling command slot %d: %s\n", slot, recalledCommand.c_str());
  parseCommandsAndEnqueue(recalledCommand, sourceID);
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
        Serial.printf("  Serial%d Baud Rate: %d,  Broadcast: %s\n",
                      i + 1, baudRates[i], serialBroadcastEnabled[i] ? "Enabled" : "Disabled");
    }
}

// Load broadcast settings from preferences
void loadBroadcastSettingsFromPreferences() {
    preferences.begin("broadcast_settings", true);
    for (int i = 0; i < 5; i++) {
        String key = "BroadcastSerial" + String(i + 1);
        serialBroadcastEnabled[i] = preferences.getBool(key.c_str(), true); // FIX: Use `.c_str()`
    }
    preferences.end();
}

// Save broadcast settings to preferences
void saveBroadcastSettingsToPreferences() {
    preferences.begin("broadcast_settings", false);
    for (int i = 0; i < 5; i++) {
        String key = "BroadcastSerial" + String(i + 1);
        preferences.putBool(key.c_str(), serialBroadcastEnabled[i]); // FIX: Use `.c_str()`
    }
    preferences.end();
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
}

// Load ESP-NOW password from preferences
void loadESPNowPasswordFromPreferences() {
    preferences.begin("espnow_config", true);
    preferences.getString("password", espnowPassword, sizeof(espnowPassword));
    preferences.end();
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
        char storedCommandBuffer[200] = {0};
        String key = "CMD" + String(i + 1);
        preferences.getString(key.c_str(), storedCommandBuffer, sizeof(storedCommandBuffer));
        storedCommands[i] = String(storedCommandBuffer);
    }
    preferences.end();
}

// Save stored commands to preferences
void saveStoredCommandsToPreferences() {
    preferences.begin("stored_commands", false);
    for (int i = 0; i < MAX_STORED_COMMANDS; i++) {
        String key = "CMD" + String(i + 1);
        preferences.putString(key.c_str(), storedCommands[i]);
    }
    preferences.end();
}

// Clear all stored commands
void clearAllStoredCommands() {
    for (int i = 0; i < MAX_STORED_COMMANDS; i++) {
        storedCommands[i] = "";
    }
    saveStoredCommandsToPreferences();
}

// Erase all NVS preferences
void eraseNVSFlash() {
    preferences.begin("serial_baud", false);
    preferences.clear();
    preferences.end();

    preferences.begin("broadcast_settings", false);
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

    Serial.println("NVS cleared. Restarting...");
    delay(2000);
    ESP.restart();
}
