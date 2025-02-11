// #include "WCB_Core.h"



// void handleSingleCommand(String cmd, int sourceID) {
//     cmd.toLowerCase(); // Convert entire command to lowercase

//     // 1) LocalFunctionIdentifier-based commands (e.g., `?commands`)
//     if (cmd.startsWith(String(LocalFunctionIdentifier))) {
//         processLocalCommand(cmd.substring(1)); // Process local function
//     } 
//     // 2) CommandCharacter-based commands (e.g., `;commands`)
//     else if (cmd.startsWith(String(CommandCharacter))) {
//         procesCommandCharcter(cmd.substring(1), sourceID); // Process serial-specific command
//     } 
//     // 3) Default to broadcasting if not a recognized prefix
//     else {
//         processBroadcastCommand(cmd, sourceID);
//     }
// }
// //*******************************
// /// Processing Local Function Identifier Functions
// //*******************************
// void processLocalCommand(const String &message) {
//     if (message == "don") {
//         debugEnabled = true;
//         Serial.println("Debugging enabled");
//     } else if (message == "doff") {
//         debugEnabled = false;
//         Serial.println("Debugging disabled");
//     } else if (message.startsWith("lf")) {
//         updateLocalFunctionIdentifier(message);
//     } else if (message.startsWith("cc")) {
//         updateCommandCharacter(message);
//     } else if (message == "reboot") {
//         reboot();
//     } else if (message == "config") {
//         Serial.println("Processing ?CONFIG command");
//         printConfigInfo();
//     } else if (message.startsWith("c")) {
//         storeCommand(message);
//     } else if (message.startsWith("d")) {
//         updateCommandDelimiter(message);
//     } else if (message == "cclear") {
//         clearAllStoredCommands();
//     } else if (message.startsWith("m2")){
//         update2ndMACOctet(message);
//     } else if (message.startsWith("m3")) {
//         update3rdMACOctet(message);
//     } else if (message == "wcb_erase") {
//         eraseNVSFlash();
//     } else if (message.startsWith("wcbq")) {
//         updateWCBQuantity(message);
//     } else if (message.startsWith("wcb")) {
//         updateWCBNumber(message);
//     } else if (message.startsWith("epass")) {
//         updateESPNowPassword(message);
//     } else if (message.startsWith("s")) {
//         updateSerialSettings(message);
//     } else if (message.startsWith("maestro_enable")) {
//         enableMaestroSerialBaudRate();
//     }else if (message.startsWith("maestro_disable")) {
//         disableMaestroSerialBaudRate();
//     } else if (message.startsWith("hw")) {
//         updateHWVersion(message);
//     } else {
//         Serial.println("Invalid Local Command");
//     }
// }

// void updateLocalFunctionIdentifier(const String &message){
//   if (message.length() >= 3) {
//     LocalFunctionIdentifier = message.charAt(2);
//     saveLocalFunctionIdentifierAndCommandCharacter();
//     Serial.printf("LocalFunctionIdentifier updated to '%c'\n", LocalFunctionIdentifier);
//   } else {
//     Serial.println("Invalid LocalFunctionIdentifier update command");
//   }
// }

// void updateCommandCharacter(const String &message){
//   if (message.length() >= 3) {
//     CommandCharacter = message.charAt(2);
//     saveLocalFunctionIdentifierAndCommandCharacter();
//     Serial.printf("CommandCharacter updated to '%c'\n", CommandCharacter);
//   } else {
//     Serial.println("Invalid CommandCharacter update command");
//   }
// }

// void storeCommand(const String &message){
//   int slot = message.substring(1, 3).toInt();
//   String commandData = message.substring(3);
//   if (slot >= 1 && slot <= MAX_STORED_COMMANDS) {
//     // store in slot
//     storedCommands[slot - 1] = commandData;
//     saveStoredCommandsToPreferences();
//     Serial.printf("Stored command in slot %d: %s\n", slot, commandData.c_str());
//   } else {
//     Serial.println("Invalid command slot. Please enter a number between 1 and 50.");
//   }

// }
// void updateCommandDelimiter(const String &message){
//   if (message.length() == 2) {
//     char newDelimiter = message.charAt(1);
//     setCommandDelimiter(newDelimiter);
//     Serial.printf("Command delimiter updated to: '%c'\n", newDelimiter);
//   } else {
//     Serial.println("Invalid command delimiter update. Use ?Dx where x is the new delimiter.");
//   }
// }


// void update2ndMACOctet(const String &message){
//     String hexValue = message.substring(2, 4);
//     int newValue = strtoul(hexValue.c_str(), NULL, 16);
//     umac_oct2 = static_cast<uint8_t>(newValue);
//     saveMACPreferences();
//     Serial.printf("Updated the 2nd Octet to 0x%02X\n", umac_oct2);
// }

// void update3rdMACOctet(const String &message){
//   String hexValue = message.substring(2, 4);
//   int newValue = strtoul(hexValue.c_str(), NULL, 16);
//   umac_oct3 = static_cast<uint8_t>(newValue);
//   saveMACPreferences();
//   Serial.printf("Updated the 3rd Octet to 0x%02X\n", umac_oct3);
// }

// void updateWCBQuantity(const String &message){
//   int wcbQty = message.substring(4).toInt();
//   saveWCBQuantityPreferences(wcbQty);
// }

// void updateSerialSettings(const String &message){
// //  Handle enabling/disabling broadcast on serial ports
//   int port = message.substring(1, 2).toInt();
//   int state = message.substring(2, 3).toInt();
//   if ((state == 0 || state == 1) && message.length()==3){
//     if (port >= 1 && port <= 5) {
//       serialBroadcastEnabled[port - 1] = (state == 1);
//       saveBroadcastSettingsToPreferences();
//       Serial.printf("Serial%d broadcast %s and stored in NVS\n",
//           port, serialBroadcastEnabled[port - 1] ? "Enabled" : "Disabled");
//     }
//   } else {
//     int baud = message.substring(2).toInt();
//     if (port >= 1 && port <= 5) {
//       updateBaudRate(port, baud);
//       Serial.printf("Updated Serial%d baud rate to %d and stored in NVS\n", port, baud);
//     }
//   }
// }

// void updateWCBNumber(const String &message){
//   int newWCB = message.substring(3).toInt();
//   if (newWCB >= 1 && newWCB <= 9) {
//     saveWCBNumberToPreferences(newWCB);
//   }
// }

// void updateESPNowPassword(const String &message){
//   String newPassword = message.substring(5);
//   if (newPassword.length() > 0 && newPassword.length() < sizeof(espnowPassword)) {
//     setESPNowPassword(newPassword.c_str());
//     Serial.printf("ESP-NOW Password updated to: %s\n", newPassword.c_str());
//   } else {
//     Serial.println("Invalid ESP-NOW password length. Must be between 1 and 39 characters.");
//   }
// }

// void enableMaestroSerialBaudRate(){
//     recallBaudRatefromSerial(1);
//     Serial.printf("Saved original baud rate of %d for Serial 1\n, Updating to 57600 to support Maestro\n", storedBaudRate[1], 1);
//     updateBaudRate(1, 57600);
// }

// void disableMaestroSerialBaudRate(){
//     updateBaudRate(1, storedBaudRate[1]);    
//     Serial.printf("Saved original baud rate of %d for Serial 1\n", storedBaudRate[1]);
// }

// void updateHWVersion(const String &message) {
//   int temp_hw_version = message.substring(2).toInt();
//   saveHWversion(temp_hw_version);
// }


// void processCommandCharcter(const String &message, int sourceID) {
//     if (message.startsWith("s")) {
//         processSerialMessage(message);
//     } else if (message.startsWith("w")) {
//         processWCBMessage(message);
//     } else if (message.startsWith("c")) {
//         recallStoredCommand(message, sourceID);
//     } else if (message.startsWith("m")) {
//         processMaestroCommand(message);
//     } else {
//         Serial.println("Invalid Serial Command");
//     }
// }

// //*******************************
// /// Processing Command Character Functions
// //*******************************
// void processSerialMessage(const String &message) {
//     if (message.length() < 2) {
//         Serial.println("Invalid serial message format.");
//         return;
//     }

//     // Extract serial port number
//     int target = message.substring(1, 2).toInt();  // Extract Serial port (1-5)
//     if (target < 1 || target > 5) {
//         Serial.println("Invalid serial port number. Must be between 1 and 5.");
//         return;
//     }

//     // Extract message to send
//     String serialMessage = message.substring(2);
//     serialMessage.trim();

//     // Send to selected serial port
//     Stream &targetSerial = getSerialStream(target);
//     writeSerialString(targetSerial, serialMessage);
//     targetSerial.flush(); // Ensure it's sent immediately

//     if (debugEnabled) {
//         Serial.printf("Sent to Serial%d: %s\n", target, serialMessage.c_str());
//     }
// }

// void processWCBMessage(const String &message){
//   int targetWCB = message.substring(1, 2).toInt();
//         String espnow_message = message.substring(2);
//         if (targetWCB >= 1 && targetWCB <= Default_WCB_Quantity) {
//           Serial.printf("Sending Unicast ESP-NOW message to WCB%d: %s\n", targetWCB, espnow_message.c_str());
//           sendESPNowMessage(targetWCB, espnow_message.c_str());
//         } else {
//           Serial.println("Invalid WCB number for unicast.");
//         }
// }

// void recallStoredCommand(const String &message, int sourceID) {
//   int slot = message.substring(1, 3).toInt();  // Extract slot number
//   if (slot >= 1 && slot <= MAX_STORED_COMMANDS) {
//     // Retrieve stored command
//     String recalledCommand = storedCommands[slot - 1];

//     // If the slot is empty, return an error
//     if (recalledCommand.length() == 0) {
//         Serial.printf("No command stored in slot %d.\n", slot);
//         return;
//     }

//     Serial.printf("Recalling command from slot %d: %s\n", slot, recalledCommand.c_str());

//     // Enqueue the recalled command for processing
//     enqueueCommand(recalledCommand, sourceID);
//   } else {
//             Serial.printf("Invalid slot number %d. Please enter a number between 1 and %d.\n", slot, MAX_STORED_COMMANDS);
//   }  
// }

// void processMaestroCommand(const String &message){
//   int message_maestroID = message.substring(1,2).toInt();
//     int message_maestroSeq = message.substring(2).toInt();
//     sendMaestroCommand(message_maestroID,message_maestroSeq);
// }


// //*******************************
// /// Processing Broadcast Function
// //*******************************
// void processBroadcastCommand(const String &cmd, int sourceID) {
//     if (debugEnabled) {
//         Serial.printf("Broadcasting command: %s\n", cmd.c_str());
//     }

//     // Send to all serial ports
//     for (int i = 1; i <= 5; i++) {
//         if (i == sourceID || !serialBroadcastEnabled[i - 1]) continue;
//         writeSerialString(getSerialStream(i), cmd);
//         if (debugEnabled) { Serial.printf("Sent to Serial%d: %s\n", i, cmd.c_str()); }
//     }

//     // Also send via ESP-NOW broadcast
//     sendESPNowMessage(0, cmd.c_str());
//     if (debugEnabled) { Serial.printf("Broadcasted via ESP-NOW: %s\n", cmd.c_str()); }
// }

