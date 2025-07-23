#include "WCB_Storage.h" // Or wherever your commandDelimiter and parseCommandsAndEnqueue are
#include "command_timer_queue.h"

std::vector<CommandGroup> commandGroups;
unsigned long lastGroupTime = 0;
size_t currentGroupIndex = 0;
bool commandTimerModeEnabled = false;
bool waitingForNextGroup = false;

std::vector<String> splitString(const String &str, char delimiter) {
  std::vector<String> result;
  int start = 0;
  int end = str.indexOf(delimiter);
  while (end != -1) {
    result.push_back(str.substring(start, end));
    start = end + 1;
    end = str.indexOf(delimiter, start);
  }
  result.push_back(str.substring(start));
  return result;
}

void stopTimerSequence() {
  commandGroups.clear();
  commandTimerModeEnabled = false;
  currentGroupIndex = 0;
  waitingForNextGroup = false;
  Serial.println("⏹️ Timer sequence manually stopped.");
}

bool checkForTimerStopRequest(const String &input) {
  return input.equalsIgnoreCase(String(LocalFunctionIdentifier) + "STOP");
}

void parseCommandGroups(const String &input) {
  commandGroups.clear();
  currentGroupIndex = 0;
  commandTimerModeEnabled = true;
  waitingForNextGroup = false;

  String working = input;
  working.replace("\r", "");
  working.replace("\n", "");

  std::vector<String> tokens = splitString(working, commandDelimiter);

  CommandGroup currentGroup;
  currentGroup.delayAfterPrevious = 0;

  for (const String &token : tokens) {
    if (token.startsWith(String(CommandCharacter) + "t") || token.startsWith(String(CommandCharacter) + "T")) {
      if (!currentGroup.commands.empty()) {
        commandGroups.push_back(currentGroup);
        currentGroup = CommandGroup();
      }
      String delayStr;
      String remaining;
      int commaIndex = token.indexOf(',');
      if (commaIndex != -1) {
        delayStr = token.substring(2, commaIndex);
        remaining = token.substring(commaIndex + 1);
      } else {
        delayStr = token.substring(2);
        remaining = "";
      }
      currentGroup.delayAfterPrevious = delayStr.toInt();
      if (!remaining.isEmpty()) {
        currentGroup.commands.push_back(remaining);
      }
    } else {
      currentGroup.commands.push_back(token);
    }
  }
  if (!currentGroup.commands.empty()) {
    commandGroups.push_back(currentGroup);
  }
  lastGroupTime = millis();
}

void processCommandGroups() {
  if (!commandTimerModeEnabled || currentGroupIndex >= commandGroups.size()) {
    return;
  }

  static size_t lastProcessedGroupIndex = -1;
  if (!commandTimerModeEnabled) return;
  if (currentGroupIndex != lastProcessedGroupIndex) {
    lastProcessedGroupIndex = currentGroupIndex;
  }
  CommandGroup &group = commandGroups[currentGroupIndex];

  if (checkForTimerStopRequest(group.commands[0])) {
    stopTimerSequence();
    return;
  }

  if (waitingForNextGroup) {
    if (millis() - lastGroupTime >= group.delayAfterPrevious) {
      for (const String &cmd : group.commands) {
        if (debugEnabled) {
          Serial.printf("[TimerGroup %u] Executing command: %s\n", currentGroupIndex + 1, cmd.c_str());
        }
        parseCommandsAndEnqueue(cmd, 0);
      }
      lastGroupTime = millis();
      currentGroupIndex++;

      if (currentGroupIndex >= commandGroups.size()) {
        commandTimerModeEnabled = false;
        if (debugEnabled) Serial.println("✅ Timer command sequence complete.");
      }
    }
  } else {
    waitingForNextGroup = true;
  }
} 
