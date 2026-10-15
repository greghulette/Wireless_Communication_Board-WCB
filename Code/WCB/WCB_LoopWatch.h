// WCB_LoopWatch.h - say where loop() stopped, when it stops.
//
// W1 went silent twice in a long HIL run (intellex.flash_navicore_app, 2026-10-14 and 10-15): no console reply, off the
// mesh, not in the bootloader, and no watchdog fired - loop() had stopped while the rest of the system ran on. Every
// look at it reset it first. This leaves a line on the console instead: a 1 s esp_timer compares loop()'s pass count,
// and once it has not moved for LOOP_STUCK_MS it prints - through the ROM's own UART print, which takes no lock a stuck
// task could be holding - the step loop() is in (LOOP_STEP), the verb of the command it is running (never its
// arguments: a ?WIFI line carries a password), the loop task's state (blocked = waiting on a lock or a queue,
// running/ready = spinning), the task holding the WebSocket sender's lock, and the free heap. Then every
// LOOP_REPORT_EVERY_MS while it stays stuck, and one line when it moves again. Print only: nothing is reset.
#pragma once
#include <stdint.h>

#define LOOP_STUCK_MS        15000UL
#define LOOP_REPORT_EVERY_MS 30000UL

extern volatile uint32_t g_loopTick;            // loop() passes
extern const char *volatile g_loopStep;         // the step loop() is in (a string literal)
extern const char *volatile g_loopCmd;          // the command handleSingleCommand() is running, or nullptr

// Run `call` as a named step of loop().
#define LOOP_STEP(call) do { g_loopStep = #call; call; } while (0)

void loopWatchBegin();   // from setup(), on the loop task: starts the watch
