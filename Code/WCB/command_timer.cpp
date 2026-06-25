#include "WCB_RemoteTerm.h"  // Must be first — redirects Serial → WCBDebugSerial
#include "WCB_Storage.h" // Or wherever your commandDelimiter and parseCommandsAndEnqueue are
#include "command_timer_queue.h"
#include "WCB_Variables.h"   // isIfChainToken / evaluateIfCondition (IF gating)

// '***' comment marker — defined in WCB.ino (mirrors WCB_Storage.cpp / WCB_Variables.cpp)
extern String commentDelimiter;
// ESP-NOW origin flag (loop-prevention), owned by WCB.ino.
extern bool lastReceivedViaESPNOW;

std::vector<CommandGroup> commandGroups;
unsigned long lastGroupTime = 0;
size_t currentGroupIndex = 0;
bool commandTimerModeEnabled = false;
bool waitingForNextGroup = false;
// Origin captured when the active timer sequence was parsed. Timer groups fire
// long after the original trigger, so the live global is unrelated by then —
// processCommandGroups re-applies this while enqueuing each group's commands so
// a peer-triggered (or locally-triggered) timer sequence keeps the correct
// ESP-NOW re-broadcast behavior. Only one timer sequence is active at a time.
bool commandGroupsEspnowOrigin = false;

bool isTimerCommand(const String &input);
void stopTimerSequence();
bool checkForTimerStopRequest(const String &input);



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

bool isTimerCommand(const String &input) {
  return input.indexOf(String(CommandCharacter) + "T") != -1 ||
         input.indexOf(String(CommandCharacter) + "t") != -1;
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
void printTimerDebugInfo(const String &delayStr, unsigned long parsedDelay, unsigned long limit) {
  Serial.printf("=== TIMER DEBUG ===\n");
  Serial.printf("Input string: '%s'\n", delayStr.c_str());
  Serial.printf("String length: %d\n", delayStr.length());
  Serial.printf("Parsed delay: %lu ms\n", parsedDelay);
  Serial.printf("Limit: %lu ms\n", limit);
  Serial.printf("Exceeds limit: %s\n", (parsedDelay > limit) ? "YES" : "NO");
  Serial.printf("===================\n");
}

void parseCommandGroups(const String &input) {
  commandGroups.clear();
  currentGroupIndex = 0;
  commandTimerModeEnabled = true;
  waitingForNextGroup = false;
  // Capture the origin now; it's re-applied as each group fires (see processCommandGroups).
  commandGroupsEspnowOrigin = lastReceivedViaESPNOW;

  String working = input;
  working.replace("\r", "");
  working.replace("\n", "");

  std::vector<String> tokens = splitString(working, commandDelimiter);

  CommandGroup currentGroup;
  currentGroup.delayAfterPrevious = 0;

  // IF gating (chain-local, evaluated NOW at invoke time — not after the
  // timer delays fire). ALL gate semantics live in the shared
  // ifGateConsumeToken (WCB_Variables.cpp) so this walker can never drift
  // from parseCommandsAndEnqueue. Locally we only handle the timer-token
  // mechanics. NOTE: every check below uses the TRIMMED token — user-typed
  // spaces around '^' must not change classification.
  bool ifSkipping = false;

  for (const String &rawToken : tokens) {
    String token = rawToken;
    token.trim();
    if (token.isEmpty()) continue;

    // Comments are annotations: never executed, never consume the IF gate
    // (matches parseCommandsAndEnqueue, which ignores them pre-gate).
    if (token.startsWith(commentDelimiter)) continue;

    if (ifGateConsumeToken(token, ifSkipping)) continue;

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
        remaining.trim();
        // An IF cannot ride INSIDE a timer token: at fire time each group
        // command is dispatched through its own parseCommandsAndEnqueue
        // call, so the gate state would die with that stack frame and the
        // next command would run ungated. Reject loudly instead of
        // silently not gating.
        if (isIfChainToken(remaining)) {
          Serial.printf("[IF] ERROR: IF cannot be a timer payload (';t%s,%s') — "
                        "write it as IF,cond^%ct%s^command instead. Token ignored.\n",
                        delayStr.c_str(), remaining.c_str(),
                        CommandCharacter, delayStr.c_str());
          remaining = "";
        }
      } else {
        delayStr = token.substring(2);
        remaining = "";
      }
      unsigned long parsedDelay = delayStr.toInt();
      unsigned long parsedDelayLimit = 1800000; // 30 Minutes (30 * 60 * 1000) (Minutes * Seconds in a Minute * milliseconds in a second)
      if (debugEnabled) {
        // printTimerDebugInfo(delayStr, parsedDelay, parsedDelayLimit);
      }
    if (debugEnabled && parsedDelay > parsedDelayLimit) {
        Serial.printf("⚠️ Warning: Delay exceeds configured limits of %i. Input: %s ms\n", parsedDelayLimit, delayStr.c_str());
        // printTimerDebugInfo(delayStr, parsedDelay, parsedDelayLimit);
      }
      if (parsedDelay > parsedDelayLimit) {
        parsedDelay = parsedDelayLimit;
        if (debugEnabled) {
          Serial.printf("⏱️ Delay capped to %i ms : originally requested %s ms\n", parsedDelayLimit, delayStr.c_str());
        }
      }
      // ACCUMULATE (don't overwrite): consecutive delay tokens with no command
      // between them — ';t500^;t300^cmd' or ';t500^***note^;t300^cmd' (the
      // comment is dropped above, leaving the group empty) — must wait the SUM.
      // A fresh group starts at 0, so += is also correct for the normal case.
      currentGroup.delayAfterPrevious += parsedDelay;
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

  CommandGroup &group = commandGroups[currentGroupIndex];

  if (checkForTimerStopRequest(group.commands[0])) {
    stopTimerSequence();
    return;
  }

  if (waitingForNextGroup) {
    if (millis() - lastGroupTime >= group.delayAfterPrevious) {
      // Re-apply the origin captured when this sequence was parsed so the commands
      // enqueued now inherit the right ESP-NOW semantics. enqueueCommand snapshots
      // the global, so it must hold this group's origin during the enqueue.
      bool _savedEspNowOrigin = lastReceivedViaESPNOW;
      lastReceivedViaESPNOW = commandGroupsEspnowOrigin;
      for (const String &cmd : group.commands) {
        if (debugEnabled) {
          Serial.printf("[TimerGroup %u] Executing command: %s\n", currentGroupIndex + 1, cmd.c_str());
        }
        parseCommandsAndEnqueue(cmd, 0);
                vTaskDelay(pdMS_TO_TICKS(1)); // ← Give queue time to breathe
      }
      lastReceivedViaESPNOW = _savedEspNowOrigin;
      lastGroupTime = millis();
      currentGroupIndex++;

      if (currentGroupIndex >= commandGroups.size()) {
        commandTimerModeEnabled = false;
        if (debugEnabled) Serial.println("✅ Timer command sequence complete.");
      }
    }
  } else {
    waitingForNextGroup = true;
        vTaskDelay(pdMS_TO_TICKS(1)); // ← Ensure millis() increments before next check
  }
} 


