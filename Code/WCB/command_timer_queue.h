#ifndef COMMAND_TIMER_QUEUE_H
#define COMMAND_TIMER_QUEUE_H

#include <Arduino.h>
#include <vector>

// Constants
#define MAX_DELAY_MS 1800000  // 30 Minutes (30 * 60 * 1000)

// Structure to hold a group of commands with timing
struct CommandGroup {
  unsigned long delayAfterPrevious;
  std::vector<String> commands;
};

// Global variables for timer functionality
extern std::vector<CommandGroup> commandGroups;
extern unsigned long lastGroupTime;
extern size_t currentGroupIndex;
extern bool commandTimerModeEnabled;
extern bool waitingForNextGroup;

// External dependencies
extern bool debugEnabled;
extern char CommandCharacter;
extern char LocalFunctionIdentifier;

// Function declarations
bool isTimerCommand(const String &input);
void stopTimerSequence();
bool checkForTimerStopRequest(const String &input);
void parseCommandGroups(const String &input);
void processCommandGroups();
std::vector<String> splitString(const String &str, char delimiter);

#endif // COMMAND_TIMER_QUEUE_H