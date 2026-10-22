#include "WCB_RemoteTerm.h"   // First, as in every subsystem file (CLAUDE.md rule 12). This one prints only through the ROM.
// WCB_LoopWatch.cpp - see WCB_LoopWatch.h.
#include <Arduino.h>
#include "esp_heap_caps.h"
#include "esp_rom_sys.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "WCB_LoopWatch.h"
#include "WCB_WS.h"

volatile uint32_t g_loopTick = 0;
const char *volatile g_loopStep = "setup";
const char *volatile g_loopCmd = nullptr;

static TaskHandle_t s_loopTask = nullptr;
static esp_timer_handle_t s_timer = nullptr;

// The verb of a command line: up to the first ',' or space, at most n-1 characters. Never more - the arguments of a
// ?WIFI or ?EPASS line are a password.
static void verbOf(const char *cmd, char *out, size_t n) {
  size_t i = 0;
  if (cmd) {
    while (i + 1 < n && cmd[i] && cmd[i] != ',' && cmd[i] != ' ' && cmd[i] != '\r' && cmd[i] != '\n') {
      out[i] = cmd[i];
      i++;
    }
  }
  out[i] = 0;
}

static const char *taskState(TaskHandle_t t) {
  if (!t) return "?";
  switch (eTaskGetState(t)) {
    case eRunning:   return "running";
    case eReady:     return "ready";
    case eBlocked:   return "blocked";
    case eSuspended: return "suspended";
    default:         return "?";
  }
}

// esp_timer task, every second. Reads only: no lock, no allocation (the heap figures are plain reads).
static void watchTick(void *) {
  static uint32_t seen = 0, since = 0, lastReport = 0;
  static bool reported = false;
  const uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);
  const uint32_t tick = g_loopTick;
  if (tick != seen) {
    if (reported) esp_rom_printf("[HANG] loop() running again after %u s\n", (unsigned)((now - since) / 1000));
    seen = tick;
    since = 0;
    reported = false;
    return;
  }
  if (!since) { since = now; return; }
  if (now - since < LOOP_STUCK_MS || (reported && now - lastReport < LOOP_REPORT_EVERY_MS)) return;
  reported = true;
  lastReport = now;
  char verb[24];
  const char *cmd = g_loopCmd;
  verbOf(cmd, verb, sizeof(verb));
  esp_rom_printf("[HANG] loop() stuck %u s in %s%s%s (loop task %s); WS lock held by %s; heap free %u, min %u\n",
                 (unsigned)((now - since) / 1000), g_loopStep, cmd ? " running " : "", cmd ? verb : "",
                 taskState(s_loopTask), wcbWsLockHolderName(),
                 (unsigned)heap_caps_get_free_size(MALLOC_CAP_8BIT),
                 (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_8BIT));
}

void loopWatchBegin() {
  s_loopTask = xTaskGetCurrentTaskHandle();
  if (s_timer) return;
  esp_timer_create_args_t a = {};
  a.callback = watchTick;
  a.name = "loopwatch";
  a.dispatch_method = ESP_TIMER_TASK;
  if (esp_timer_create(&a, &s_timer) == ESP_OK) esp_timer_start_periodic(s_timer, 1000000);
}
