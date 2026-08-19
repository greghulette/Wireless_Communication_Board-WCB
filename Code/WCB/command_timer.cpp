#include "WCB_RemoteTerm.h"  // Must be first — redirects Serial → WCBDebugSerial
#include "WCB_Storage.h" // Or wherever your commandDelimiter and parseCommandsAndEnqueue are
#include "command_timer_queue.h"
#include "WCB_Variables.h"   // isIfChainToken / evaluateIfCondition (IF gating)

// '***' comment marker — defined in WCB.ino (mirrors WCB_Storage.cpp / WCB_Variables.cpp)
extern String commentDelimiter;
// ESP-NOW origin flag (loop-prevention), owned by WCB.ino.
extern bool lastReceivedViaESPNOW;
// Sequence-body flag (nested-recall fanout suppression), owned by WCB.ino.
extern bool inSequenceBody;

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
// Sequence-body flag captured when the active timer sequence was parsed, re-applied while
// enqueuing each group's commands (parallel to commandGroupsEspnowOrigin) so a nested `;C`
// inside a TIMER sequence body is not re-fanned out to the mesh.
bool commandGroupsSequenceBody = false;

// Bumped by EVERY mutation of commandGroups (parseCommandGroups, stopTimerSequence).
//
// processCommandGroups() runs on the loop task but yields (vTaskDelay) between the commands of a
// group, and serialCommandTask — a SEPARATE task, WCB.ino:7439 — can reach parseCommandGroups()
// (WCB.ino:6490) or stopTimerSequence() (WCB.ino:6431) during that yield. Both clear() the vector,
// destroying every CommandGroup and freeing every String in it.
//
// Holding a reference or a range-for iterator across that yield was therefore a use-after-free.
// processCommandGroups now copies what it needs before yielding and re-checks this counter
// afterwards, so a chain replaced mid-flight is abandoned instead of having its indices applied to
// the new chain. (The rationale comment at WCB.ino:390-399 claimed the serial path runs inside
// loop() — it does not; only SEQ playback does.)
uint32_t commandGroupsGeneration = 0;

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
  commandGroupsGeneration++;   // invalidate any in-flight processCommandGroups() pass
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
  // Single global timer state — only one timer sequence runs at a time. If one is
  // still in flight (e.g. a running sequence recalled another timer-bearing stored
  // sequence, or a new ;t… was issued mid-run) this replaces it. That used to be
  // SILENT, dropping the outer sequence's remaining groups; surface it so the loss
  // is visible. (True nested timer sequences would need a save/restore stack.)
  if (commandTimerModeEnabled && currentGroupIndex < commandGroups.size()) {
    Serial.printf("⚠️ Timer sequence replaced mid-run — %u remaining group(s) of the "
                  "previous sequence dropped (nested timer sequences aren't supported).\n",
                  (unsigned)(commandGroups.size() - currentGroupIndex));
  }
  commandGroups.clear();
  commandGroupsGeneration++;   // invalidate any in-flight processCommandGroups() pass
  currentGroupIndex = 0;
  commandTimerModeEnabled = true;
  waitingForNextGroup = false;
  // Capture the origin now; it's re-applied as each group fires (see processCommandGroups).
  commandGroupsEspnowOrigin = lastReceivedViaESPNOW;
  commandGroupsSequenceBody = inSequenceBody;   // same, for nested-recall fanout suppression

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

  // Deliberately NO reference into commandGroups — see commandGroupsGeneration above.
  // A parse or stop from serialCommandTask during the vTaskDelay below frees this vector's
  // contents, so everything needed must be copied out first.
  if (commandGroups[currentGroupIndex].commands.empty()) {
    // A group with no commands cannot be indexed at [0] below and would stall the chain.
    stopTimerSequence();
    return;
  }
  const unsigned long groupDelay = commandGroups[currentGroupIndex].delayAfterPrevious;

  if (checkForTimerStopRequest(commandGroups[currentGroupIndex].commands[0])) {
    stopTimerSequence();
    return;
  }

  if (waitingForNextGroup) {
    if (millis() - lastGroupTime >= groupDelay) {
      // Re-apply the origin captured when this sequence was parsed so the commands
      // enqueued now inherit the right ESP-NOW semantics. enqueueCommand snapshots
      // the global, so it must hold this group's origin during the enqueue.
      // Copy this group's commands out of the vector BEFORE executing. parseCommandsAndEnqueue
      // yields below, and a concurrent parse/stop on serialCommandTask frees the originals.
      const std::vector<String> cmds = commandGroups[currentGroupIndex].commands;
      const uint32_t gen            = commandGroupsGeneration;
      const size_t   groupNumber    = currentGroupIndex + 1;   // for logging only

      bool _savedEspNowOrigin = lastReceivedViaESPNOW;
      bool _savedSeqBody       = inSequenceBody;
      lastReceivedViaESPNOW = commandGroupsEspnowOrigin;
      inSequenceBody        = commandGroupsSequenceBody;
      for (const String &cmd : cmds) {
        if (debugEnabled) {
          Serial.printf("[TimerGroup %u] Executing command: %s\n", (unsigned)groupNumber, cmd.c_str());
        }
        parseCommandsAndEnqueue(cmd, 0);
                vTaskDelay(pdMS_TO_TICKS(1)); // ← Give queue time to breathe
      }
      lastReceivedViaESPNOW = _savedEspNowOrigin;
      inSequenceBody        = _savedSeqBody;

      // If another task replaced or stopped the chain while we were yielding, its state (index,
      // size, enabled flag) is already correct for the NEW chain — advancing our stale index here
      // would corrupt it. Abandon this pass instead.
      if (gen != commandGroupsGeneration) return;

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


