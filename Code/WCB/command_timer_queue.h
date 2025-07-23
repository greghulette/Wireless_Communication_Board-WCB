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

void parseCommandGroups(const String &input);
void processCommandGroups();
std::vector<String> splitString(const String &str, char delimiter);

#endif
