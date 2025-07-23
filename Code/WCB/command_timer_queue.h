#ifndef COMMAND_TIMER_QUEUE_H
#define COMMAND_TIMER_QUEUE_H

#include <vector>
#include <Arduino.h>



struct CommandGroup {
  unsigned long delayAfterPrevious;
  std::vector<String> commands;
};

extern std::vector<CommandGroup> commandGroups;
extern unsigned long lastGroupTime;
extern size_t currentGroupIndex;
extern bool commandTimerModeEnabled; // Flag to indicate timer-based parsing
extern bool waitingForNextGroup;     // Tracks if we're in a delay period
extern bool firstGroupExecuted;
bool checkForTimerStopRequest(const String &input);
void stopTimerSequence();
void parseCommandGroups(const String &input);
void processCommandGroups();
std::vector<String> splitString(const String &str, char delimiter);

#endif