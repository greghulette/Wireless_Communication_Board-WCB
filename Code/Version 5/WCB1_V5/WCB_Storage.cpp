#include "WCB_Storage.h"

Preferences preferences;


// Define the global variables here
uint8_t umac_oct2 = 0x00;
uint8_t umac_oct3 = 0x00;
int Default_WCB_Quantity = 1;
char espnowPassword[40] = "CHANGE_ME_PLEASE_OR_RISK_TAKEOVER";
int baudRates[5] = {9600, 9600, 9600, 9600, 9600};
bool serialBroadcastEnabled[5] = {true, true, true, true, true};
char commandDelimiter = '^';
String storedCommands[MAX_STORED_COMMANDS];

//-------------------------------------------------------------------
// MAC Address Preferences
//-------------------------------------------------------------------
void loadMACPreferences() {
    preferences.begin("mac_config", true);
    umac_oct2 = preferences.getUChar("umac_oct2", 0x00);
    umac_oct3 = preferences.getUChar("umac_oct3", 0x00);
    preferences.end();
}

void saveMACPreferences() {
    preferences.begin("mac_config", false);
    preferences.putUChar("umac_oct2", umac_oct2);
    preferences.putUChar("umac_oct3", umac_oct3);
    preferences.end();
    Serial.printf("Saved MAC Config: umac_oct2=0x%02X, umac_oct3=0x%02X\n", umac_oct2, umac_oct3);
}

//-------------------------------------------------------------------
// WCB Quantity Preferences
//-------------------------------------------------------------------
void saveWCBQuantityPreferences(int wcbq){
    preferences.begin("wcbq_config", false);
    preferences.putInt("wcb_quantity", wcbq);
    preferences.end();
    Serial.printf("Updated WCB Quantities: %d WCBs setup\n", wcbq);
    Serial.println("Must reboot to take effect");
}

void loadWCBQuantitiesFromPreferences(){
    preferences.begin("wcbq_config", true);
    Default_WCB_Quantity = preferences.getInt("wcb_quantity", Default_WCB_Quantity);
    preferences.end();
}

//-------------------------------------------------------------------
// ESP-NOW Password Storage
//-------------------------------------------------------------------
void loadESPNowPasswordFromPreferences() {
    preferences.begin("espnow", true);
    preferences.getString("password", espnowPassword, sizeof(espnowPassword));
    preferences.end();
    Serial.printf("ESP-NOW Password: %s\n", espnowPassword);
}

void setESPNowPassword(const char *password) {
    strncpy(espnowPassword, password, sizeof(espnowPassword));
    espnowPassword[sizeof(espnowPassword) - 1] = '\0'; // ensure null-termination

    preferences.begin("espnow", false);
    preferences.putString("password", espnowPassword);
    preferences.end();
    Serial.printf("ESP-NOW Password updated: %s\n", espnowPassword);
}

//-------------------------------------------------------------------
// Baud Rate Preferences
//-------------------------------------------------------------------
void loadBaudRatesFromPreferences() {
    preferences.begin("serial_baud", true);
    for (int i = 0; i < 5; i++) {
        baudRates[i] = preferences.getInt(String("Serial" + String(i + 1)).c_str(), baudRates[i]);
    }
    preferences.end();
}

void loadBroadcastSettingsFromPreferences() {
    preferences.begin("serial_bcast", true);
    for (int i = 0; i < 5; i++) {
        serialBroadcastEnabled[i] = preferences.getBool(String("BroadcastSerial" + String(i + 1)).c_str(), true);
    }
    preferences.end();
}

void saveBroadcastSettingsToPreferences() {
    preferences.begin("serial_bcast", false);
    for (int i = 0; i < 5; i++) {
        preferences.putBool(String("BroadcastSerial" + String(i + 1)).c_str(), serialBroadcastEnabled[i]);
    }
    preferences.end();
}

//-------------------------------------------------------------------
// Stored Commands Preferences
//-------------------------------------------------------------------
void loadStoredCommandsFromPreferences() {
    preferences.begin("store_cmds", true);
    for (int i = 0; i < MAX_STORED_COMMANDS; i++) {
        char storedCommandBuffer[201] = "";
        preferences.getString(("cmdslot" + String(i)).c_str(), storedCommandBuffer, sizeof(storedCommandBuffer));
        storedCommands[i] = String(storedCommandBuffer);
    }
    preferences.end();
}

void saveStoredCommandsToPreferences() {
    preferences.begin("store_cmds", false);
    for (int i = 0; i < MAX_STORED_COMMANDS; i++) {
        char storedCommandBuffer[201] = "";
        storedCommands[i].toCharArray(storedCommandBuffer, sizeof(storedCommandBuffer));
        preferences.putString(("cmdslot" + String(i)).c_str(), storedCommandBuffer);
    }
    preferences.end();
}

void clearAllStoredCommands() {
    for (int i = 0; i < MAX_STORED_COMMANDS; i++) {
        storedCommands[i] = "";
    }
    saveStoredCommandsToPreferences();
    Serial.println("All stored commands have been cleared.");
}

//-------------------------------------------------------------------
// Command Delimiter Storage
//-------------------------------------------------------------------
void loadCommandDelimiter() {
    preferences.begin("esp_delim", true);
    String delim = preferences.getString("delimiter", String(commandDelimiter));
    preferences.end();

    if (delim.length() > 0) {
        commandDelimiter = delim.charAt(0);
    }
    Serial.printf("Command Delimiter: '%c'\n", commandDelimiter);
}

void setCommandDelimiter(char c) {
    commandDelimiter = c;
    preferences.begin("esp_delim", false);
    preferences.putString("delimiter", String(c));
    preferences.end();
    Serial.printf("Command Delimiter updated: '%c'\n", commandDelimiter);
}

//-------------------------------------------------------------------
// Erase All NVS Data
//-------------------------------------------------------------------
void eraseNVSFlash() {
    Serial.println("Erasing all stored NVS settings and reverting to defaults...");
    
    const char *namespaces[] = {
        "store_cmds", "serial_baud", "serial_bcast", "esp_delim", "espnow",
        "cmd_chars", "mac_config", "wcb_config", "wcbq_config"
    };

    for (const char *ns : namespaces) {
        preferences.begin(ns, false);
        preferences.clear();
        preferences.end();
    }

    Serial.println("NVS cleared. Restarting...");
    delay(2000);
    ESP.restart();
}
