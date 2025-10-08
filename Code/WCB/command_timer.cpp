#include "WCB_Storage.h"
#include "command_timer_queue.h"

// Constants for performance and safety
#define MAX_DELAY_MS 1800000  // 30 Minutes (30 * 60 * 1000)

std::vector<CommandGroup> commandGroups;
unsigned long lastGroupTime = 0;
size_t currentGroupIndex = 0;
bool commandTimerModeEnabled = false;
bool waitingForNextGroup = false;

bool isTimerCommand(const String &input);
void stopTimerSequence();
bool checkForTimerStopRequest(const String &input);

// Optimized string splitting that handles variable message lengths
std::vector<String> splitString(const String &str, char delimiter) {
    std::vector<String> result;
    if (str.length() == 0) return result;
    
    result.reserve(8);  // Pre-reserve for performance
    
    int start = 0;
    int end = str.indexOf(delimiter);
    while (end != -1) {
        String token = str.substring(start, end);
        token.trim();
        if (token.length() > 0) {  // Only add non-empty tokens
            result.push_back(token);
        }
        start = end + 1;
        end = str.indexOf(delimiter, start);
    }
    
    // Add remaining part
    String lastToken = str.substring(start);
    lastToken.trim();
    if (lastToken.length() > 0) {
        result.push_back(lastToken);
    }
    
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
    Serial.println("ℹ️ Timer sequence manually stopped.");
}

bool checkForTimerStopRequest(const String &input) {
    return input.equalsIgnoreCase(String(LocalFunctionIdentifier) + "STOP");
}

// Enhanced command group parsing that handles variable message lengths
void parseCommandGroups(const String &input) {
    commandGroups.clear();
    currentGroupIndex = 0;
    commandTimerModeEnabled = true;
    waitingForNextGroup = false;

    if (input.length() == 0) {
        Serial.println("Empty timer command sequence");
        commandTimerModeEnabled = false;
        return;
    }

    String working = input;
    working.replace("\r", "");
    working.replace("\n", "");

    if (debugEnabled) {
        Serial.printf("Parsing timer sequence (%d chars): %.100s%s\n", 
                     working.length(), working.c_str(), 
                     working.length() > 100 ? "..." : "");
    }

    std::vector<String> tokens = splitString(working, commandDelimiter);
    
    if (tokens.empty()) {
        Serial.println("No valid tokens found in timer sequence");
        commandTimerModeEnabled = false;
        return;
    }

    CommandGroup currentGroup;
    currentGroup.delayAfterPrevious = 0;

    for (const String &token : tokens) {
        if (token.startsWith(String(CommandCharacter) + "t") || 
            token.startsWith(String(CommandCharacter) + "T")) {
            
            // Save previous group if it has commands
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
            
            unsigned long parsedDelay = delayStr.toInt();
            
            // Validate delay
            if (debugEnabled && parsedDelay > MAX_DELAY_MS) {
                Serial.printf("⚠️ Warning: Delay exceeds configured limits of %lu. Input: %s ms\n", 
                             MAX_DELAY_MS, delayStr.c_str());
            }
            if (parsedDelay > MAX_DELAY_MS) {
                parsedDelay = MAX_DELAY_MS;
                if (debugEnabled) {
                    Serial.printf("⏱️ Delay capped to %lu ms : originally requested %s ms\n", 
                                 MAX_DELAY_MS, delayStr.c_str());
                }
            }
            
            currentGroup.delayAfterPrevious = parsedDelay;
            
            // Add remaining command if present (handles variable length commands)
            if (!remaining.isEmpty()) {
                remaining.trim();
                if (remaining.length() > 0) {
                    currentGroup.commands.push_back(remaining);
                }
            }
        } else {
            // Regular command - can be any length
            String trimmedToken = token;
            trimmedToken.trim();
            if (trimmedToken.length() > 0) {
                currentGroup.commands.push_back(trimmedToken);
            }
        }
    }
    
    // Add final group if it has commands
    if (!currentGroup.commands.empty()) {
        commandGroups.push_back(currentGroup);
    }
    
    if (commandGroups.empty()) {
        Serial.println("No valid command groups created");
        commandTimerModeEnabled = false;
        return;
    }
    
    if (debugEnabled) {
        Serial.printf("Created %d command groups for timer sequence\n", (int)commandGroups.size());
    }
    
    lastGroupTime = millis();
}

// Enhanced command group processing with better error handling
void processCommandGroups() {
    if (!commandTimerModeEnabled || currentGroupIndex >= commandGroups.size()) {
        return;
    }

    // Fixed: Use int instead of size_t to avoid unsigned wraparound issues
    static int lastProcessedGroupIndex = -1;
    if (!commandTimerModeEnabled) return;
    
    if (currentGroupIndex != static_cast<size_t>(lastProcessedGroupIndex)) {
        lastProcessedGroupIndex = static_cast<int>(currentGroupIndex);
    }
    
    // Bounds check to prevent crashes
    if (currentGroupIndex >= commandGroups.size()) {
        commandTimerModeEnabled = false;
        if (debugEnabled) Serial.println("✅ Timer command sequence complete (index out of bounds).");
        return;
    }
    
    CommandGroup &group = commandGroups[currentGroupIndex];

    // Enhanced bounds check for commands array
    if (group.commands.empty()) {
        if (debugEnabled) {
            Serial.printf("Warning: Empty command group at index %d, skipping\n", (int)currentGroupIndex);
        }
        currentGroupIndex++;
        if (currentGroupIndex >= commandGroups.size()) {
            commandTimerModeEnabled = false;
            if (debugEnabled) Serial.println("✅ Timer command sequence complete.");
        }
        return;
    }

    // Check for stop request
    if (checkForTimerStopRequest(group.commands[0])) {
        stopTimerSequence();
        return;
    }

    if (waitingForNextGroup) {
        if (millis() - lastGroupTime >= group.delayAfterPrevious) {
            // Execute all commands in the group (supports variable length commands)
            for (size_t cmdIdx = 0; cmdIdx < group.commands.size(); cmdIdx++) {
                const String &cmd = group.commands[cmdIdx];
                if (debugEnabled) {
                    Serial.printf("[TimerGroup %u][Cmd %u] Executing (%d chars): %.50s%s\n", 
                                 currentGroupIndex + 1, cmdIdx + 1,
                                 cmd.length(), cmd.c_str(), 
                                 cmd.length() > 50 ? "..." : "");
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
        if (debugEnabled) {
            Serial.printf("Starting timer group %u with %d commands, delay: %lu ms\n",
                         currentGroupIndex + 1, (int)group.commands.size(), group.delayAfterPrevious);
        }
    }
}