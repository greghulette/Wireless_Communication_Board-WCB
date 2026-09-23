#ifndef COMMAND_TIMER_QUEUE_H
#define COMMAND_TIMER_QUEUE_H

#include <Arduino.h>
#include <vector>
 
struct CommandGroup {
  unsigned long delayAfterPrevious;
  std::vector<String> commands;
};

extern std::vector<CommandGroup> commandGroups;
extern unsigned long lastGroupTime;
extern size_t currentGroupIndex;
extern bool commandTimerModeEnabled;
extern bool waitingForNextGroup;

bool isTimerCommand(const String &input);
void stopTimerSequence();
bool checkForTimerStopRequest(const String &input);

// sourceID: the port the chain was typed on, 0 for USB/mesh. Carried the same way the ESP-NOW
// origin is, so each group's commands keep the source-port skip and the port's serial mapping.
void parseCommandGroups(const String &input, int sourceID = 0);
void processCommandGroups();
std::vector<String> splitString(const String &str, char delimiter);

#endif
