/////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////
// MP3 Trigger Test Sketch - CORRECT COMMANDS ONLY
// Modified to accept commands from Serial2 (WCB input)
/////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////

#include <HardwareSerial.h>

HardwareSerial MP3Serial(2);
HardwareSerial CommandSerial(1);  // Add Serial1 for WCB commands

#define SERIAL2_TX_PIN 20
#define SERIAL2_RX_PIN 7

enum TestMode {
  MANUAL,
  AUTO_CYCLE,
  AUTO_SEQUENCE
};

TestMode currentMode = MANUAL;
unsigned long lastCommandTime = 0;
int commandIndex = 0;
int currentVolume = 20;

void setup() {
  Serial.begin(115200);
  delay(3000);
  
  Serial.println("\n\n========================================");
  Serial.println("MP3 Trigger Test Sketch");
  Serial.println("Using ONLY documented commands");
  Serial.println("Now listening on Serial2 (WCB input)");
  Serial.println("========================================\n");
  
  // MP3 Trigger on Serial2
  MP3Serial.begin(9600, SERIAL_8N1, 21, 8);
  
  // WCB Commands on Serial1 (pins 20/7)
  CommandSerial.begin(9600, SERIAL_8N1, SERIAL2_RX_PIN, SERIAL2_TX_PIN);
  
  delay(1000);
  
  printHelp();
}

void loop() {
  // Check USB Serial for commands
  if (Serial.available()) {
    String input = Serial.readStringUntil('\n');
    input.trim();
    if (input.length() > 0) {
      processCommand(input);
    }
  }
  
  // Check CommandSerial (Serial1 on pins 20/7) for commands from WCB
  if (CommandSerial.available()) {
    String input = CommandSerial.readStringUntil('\n');
    input.trim();
    if (input.length() > 0) {
      Serial.println("Command from WCB: " + input);
      processCommand(input);
    }
  }
  
  if (currentMode == AUTO_CYCLE) {
    if (millis() - lastCommandTime >= 10000) {
      autoCycleCommands();
      lastCommandTime = millis();
    }
  }
  
  if (currentMode == AUTO_SEQUENCE) {
    if (millis() - lastCommandTime >= 5000) {
      autoSequenceCommands();
      lastCommandTime = millis();
    }
  }
  
  if (MP3Serial.available()) {
    char response = MP3Serial.read();
    Serial.print("MP3 Response: ");
    if (response == 'X') {
      Serial.println("Track finished");
    } else if (response == 'x') {
      Serial.println("Track cancelled");
    } else if (response == 'E') {
      Serial.println("ERROR - Track doesn't exist");
    } else if (response == '=') {
      Serial.print("Status: ");
      while (MP3Serial.available()) {
        Serial.print((char)MP3Serial.read());
      }
      Serial.println();
    } else {
      Serial.printf("0x%02X\n", response);
    }
  }
}

// Rest of the functions remain the same...
void processCommand(String cmd) {
  cmd.toUpperCase();
  
  if (cmd == "HELP" || cmd == "?") {
    printHelp();
  } else if (cmd == "MODE") {
    switchMode();
  } else if (cmd == "STOP" || cmd == "O") {
    sendStartStop();
  } else if (cmd == "NEXT" || cmd == "F") {
    sendNext();
  } else if (cmd == "PREV" || cmd == "R") {
    sendPrev();
  } else if (cmd.startsWith("TRACK") || cmd.startsWith("T")) {
    int trackNum = 0;
    if (cmd.startsWith("TRACK")) {
      trackNum = cmd.substring(5).toInt();
    } else {
      trackNum = cmd.substring(1).toInt();
    }
    sendPlayTrack(trackNum);
  } else if (cmd.startsWith("VOL")) {
    int vol = cmd.substring(3).toInt();
    sendSetVolume(vol);
  } else if (cmd == "+") {
    currentVolume += 10;
    if (currentVolume > 64) currentVolume = 64;
    Serial.printf("Volume -> %d (quieter)\n", currentVolume);
    sendSetVolume(currentVolume);
  } else if (cmd == "-") {
    currentVolume -= 10;
    if (currentVolume < 0) currentVolume = 0;
    Serial.printf("Volume -> %d (louder)\n", currentVolume);
    sendSetVolume(currentVolume);
  } else if (cmd == "VERSION" || cmd == "V") {
    sendGetVersion();
  } else if (cmd == "COUNT" || cmd == "C") {
    sendGetTrackCount();
  } else if (cmd == "TEST") {
    runQuickTest();
  } else {
    Serial.println("Unknown command. Type HELP for command list.");
  }
}

void printHelp() {
  Serial.println("\n========== COMMAND REFERENCE ==========");
  Serial.println("Mode Control:");
  Serial.println("  MODE          - Switch between Manual/Auto Cycle/Auto Sequence");
  Serial.println("  HELP or ?     - Show this help");
  Serial.println("  TEST          - Run quick test sequence");
  Serial.println("\nPlayback Control:");
  Serial.println("  STOP or O     - Start/Stop (toggle)");
  Serial.println("  NEXT or F     - Next track");
  Serial.println("  PREV or R     - Previous track");
  Serial.println("\nTrack Selection:");
  Serial.println("  TRACK1 or T1  - Play track 001");
  Serial.println("  TRACK5 or T5  - Play track 005");
  Serial.println("  TRACK51       - Play track 051");
  Serial.println("  T255          - Play track 255");
  Serial.println("\nVolume Control (0=LOUDEST, 64+=quiet):");
  Serial.println("  VOL0          - Maximum volume");
  Serial.println("  VOL20         - Very loud (recommended)");
  Serial.println("  VOL40         - Medium");
  Serial.println("  +             - Quieter (+10)");
  Serial.println("  -             - Louder (-10)");
  Serial.println("\nStatus:");
  Serial.println("  VERSION or V  - Get firmware version");
  Serial.println("  COUNT or C    - Get track count");
  Serial.println("========================================");
  Serial.println("NOTE: No pause/resume - only start/stop toggle");
  Serial.println("Listening on USB Serial AND CommandSerial (pins 7/20)");
  Serial.println("========================================\n");
  
  Serial.print("Current Mode: ");
  if (currentMode == MANUAL) Serial.println("MANUAL");
  else if (currentMode == AUTO_CYCLE) Serial.println("AUTO_CYCLE");
  else if (currentMode == AUTO_SEQUENCE) Serial.println("AUTO_SEQUENCE");
  Serial.println();
}

void switchMode() {
  currentMode = (TestMode)((currentMode + 1) % 3);
  Serial.print("Switched to mode: ");
  if (currentMode == MANUAL) {
    Serial.println("MANUAL - Enter commands via Serial Monitor");
  } else if (currentMode == AUTO_CYCLE) {
    Serial.println("AUTO_CYCLE - Will cycle through all commands every 10 seconds");
    commandIndex = 0;
    lastCommandTime = millis();
  } else if (currentMode == AUTO_SEQUENCE) {
    Serial.println("AUTO_SEQUENCE - Will play predefined sequence");
    commandIndex = 0;
    lastCommandTime = millis();
  }
}

void autoCycleCommands() {
  Serial.println("\n--- Auto Cycle Command ---");
  
  switch (commandIndex) {
    case 0:
      Serial.println("Testing: STOP");
      sendStartStop();
      break;
    case 1:
      Serial.println("Testing: PLAY TRACK 1");
      sendPlayTrack(1);
      break;
    case 2:
      Serial.println("Testing: VOLUME 30");
      sendSetVolume(30);
      break;
    case 3:
      Serial.println("Testing: NEXT");
      sendNext();
      break;
    case 4:
      Serial.println("Testing: PLAY TRACK 5");
      sendPlayTrack(5);
      break;
    case 5:
      Serial.println("Testing: PREVIOUS");
      sendPrev();
      break;
    case 6:
      Serial.println("Testing: VOLUME 15");
      sendSetVolume(15);
      break;
    case 7:
      Serial.println("Testing: STOP");
      sendStartStop();
      break;
  }
  
  commandIndex++;
  if (commandIndex > 7) commandIndex = 0;
}

void autoSequenceCommands() {
  Serial.println("\n--- Auto Sequence Step ---");
  
  switch (commandIndex) {
    case 0:
      Serial.println("Step 1: Set volume to 20");
      sendSetVolume(20);
      break;
    case 1:
      Serial.println("Step 2: Play track 1");
      sendPlayTrack(1);
      break;
    case 2:
      Serial.println("Step 3: Wait 3 seconds");
      break;
    case 3:
      Serial.println("Step 4: Next track");
      sendNext();
      break;
    case 4:
      Serial.println("Step 5: Wait 3 seconds");
      break;
    case 5:
      Serial.println("Step 6: Previous track");
      sendPrev();
      break;
    case 6:
      Serial.println("Step 7: Stop");
      sendStartStop();
      break;
  }
  
  commandIndex++;
  if (commandIndex > 6) commandIndex = 0;
}

void runQuickTest() {
  Serial.println("\n========== QUICK TEST SEQUENCE ==========");
  
  Serial.println("1. Set Volume 20");
  sendSetVolume(20);
  delay(500);
  
  Serial.println("2. Play Track 1");
  sendPlayTrack(1);
  delay(3000);
  
  Serial.println("3. Next Track");
  sendNext();
  delay(3000);
  
  Serial.println("4. Volume 10 (louder)");
  sendSetVolume(10);
  delay(500);
  
  Serial.println("5. Previous Track");
  sendPrev();
  delay(3000);
  
  Serial.println("6. Stop");
  sendStartStop();
  delay(500);
  
  Serial.println("7. Get Version");
  sendGetVersion();
  delay(500);
  
  Serial.println("8. Get Track Count");
  sendGetTrackCount();
  
  Serial.println("========== TEST COMPLETE ==========\n");
}

void sendStartStop() {
  MP3Serial.print('O');
  MP3Serial.flush();
  Serial.println("Sent: START/STOP ('O')");
}

void sendNext() {
  MP3Serial.print('F');
  MP3Serial.flush();
  Serial.println("Sent: NEXT ('F')");
}

void sendPrev() {
  MP3Serial.print('R');
  MP3Serial.flush();
  Serial.println("Sent: PREVIOUS ('R')");
}

void sendPlayTrack(int trackNum) {
  if (trackNum < 1 || trackNum > 255) {
    Serial.println("Error: Track number must be 1-255");
    return;
  }
  
  MP3Serial.print('t');
  MP3Serial.write((uint8_t)trackNum);
  MP3Serial.flush();
  Serial.printf("Sent: PLAY TRACK %d ('t' + 0x%02X)\n", trackNum, trackNum);
}

void sendSetVolume(int volume) {
  if (volume < 0 || volume > 255) {
    Serial.println("Error: Volume must be 0-255");
    return;
  }
  
  currentVolume = volume;
  MP3Serial.print('v');
  MP3Serial.write((uint8_t)volume);
  MP3Serial.flush();
  Serial.printf("Sent: SET VOLUME %d ('v' + 0x%02X)\n", volume, volume);
}

void sendGetVersion() {
  MP3Serial.print('S');
  MP3Serial.print('0');
  MP3Serial.flush();
  Serial.println("Sent: GET VERSION ('S' + '0')");
  Serial.println("Waiting for response...");
}

void sendGetTrackCount() {
  MP3Serial.print('S');
  MP3Serial.print('1');
  MP3Serial.flush();
  Serial.println("Sent: GET TRACK COUNT ('S' + '1')");
  Serial.println("Waiting for response...");
}