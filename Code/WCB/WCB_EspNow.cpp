#include "WCB_RemoteTerm.h"   // FIRST: redirects Serial to the wrapper setup() opens (CLAUDE.md rule 12)
#include "WCB_EspNow.h"
#include <Arduino.h>
#include <atomic>
#include <esp_now.h>
#include <esp_wifi.h>
#include <esp_heap_caps.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

// Why every send goes through here: WCB_EspNow.h.
//
// A frame of up to 252 bytes takes about 3 ms of air at ESP-NOW's 1 Mbps, so a dozen in flight keep the radio busy
// for far longer than a waiter's 1 ms sleep - the cap costs no throughput - and twenty hold the driver's share of the
// heap to about 6 KB. The wait is long because a slow radio is not a stuck one: a unicast to a peer that does not
// answer spends tens of ms in MAC retries, and 50 ms gave up a Kyber bridge frame the old unpaced send would have
// queued and delivered (HIL kyber.remote_byte_transparency, run 20260928-220200). Losing a byte-stream frame is worse
// than a task waiting a fraction of a second.
static constexpr int      ESPNOW_INFLIGHT_TASK = 12;
static constexpr int      ESPNOW_INFLIGHT_MAX  = 20;
static constexpr uint32_t ESPNOW_SEND_WAIT_MS  = 300;
// The count says the radio is full, yet no frame has completed and none been accepted for this long: the driver lost
// those frames (the wedge below - their callbacks never come), so the count starts over and they are counted out as
// lost. A unicast that fails spends its MAC retries in well under 100 ms, so frames still in the air always complete
// inside it.
static constexpr uint32_t ESPNOW_STALL_MS      = 500;

static std::atomic<int>      s_inFlight{0};
static std::atomic<uint32_t> s_progressMs{0};   // millis() of the last completion or accepted send
static std::atomic<uint32_t> s_dropped{0};
static TaskHandle_t volatile s_wifiTask = nullptr;
static uint32_t              s_reportedDrops = 0;   // loop() only
static uint32_t              s_lastReportMs  = 0;   // loop() only

// Completions - the send callback - are the one proof the radio transmits: the stall report and the wedge watch go by
// them, not by what esp_now_send accepted (a wedged driver can accept a frame and lose it).
static std::atomic<uint32_t> s_completions{0};      // send callbacks since boot
static std::atomic<uint32_t> s_lastDoneMs{0};       // millis() | 1 of the last one; 0 = none yet
static std::atomic<uint32_t> s_triesSinceDone{0};   // frames accepted or given up since the last completion
static std::atomic<uint32_t> s_firstTryMs{0};       // millis() | 1 of the first of them; 0 = none
static std::atomic<uint32_t> s_lost{0};             // frames accepted and counted out unfinished (stall reset, restart)
static std::atomic<bool>     s_restarting{false};   // a wedge remedy is putting ESP-NOW back (wcbEspNowRestarting)

// A stall: frames are being given up and none has completed for ESPNOW_STALL_REPORT_MS. Said when one starts (with the
// heap the WiFi driver's buffers come from), every ESPNOW_STALL_REPEAT_MS while it lasts, and when frames complete
// again. An idle board gives nothing up and never counts.
static constexpr uint32_t ESPNOW_STALL_REPORT_MS = 5000;
static constexpr uint32_t ESPNOW_STALL_REPEAT_MS = 30000;
static uint32_t              s_dropsSeen      = 0;  // loop() only: the drop count at the last pass
static uint32_t              s_lastDropMs     = 0;  // loop() only: when a frame was last given up
static uint32_t              s_stallSinceMs   = 0;  // loop() only: 0 = not stalled
static uint32_t              s_stallReportMs  = 0;  // loop() only
static bool                  s_stallSim       = false;   // loop() only: the stall began under ?DEBUG,FAULT,TXWEDGE

// The transmit wedge (tracker #112, WCB_EspNow.h): frames handed to the radio for ESPNOW_WEDGE_MS, at least
// ESPNOW_WEDGE_MIN_TRIES of them, and not one completed. Twice the stall report's 5 s, so the stall line (and its heap
// figures) comes first; a heartbeat every 10 s and the adverts make three tries on the quietest board. Nothing healthy
// gets near it: a frame completes in ms, a failed unicast inside ~100 ms, and a channel-sync search keeps the radio
// away ~1.5 s (no remedy runs while it is away). A false verdict costs an ESP-NOW restart - frames already lost.
static constexpr uint32_t ESPNOW_WEDGE_MS        = 10000;
static constexpr uint32_t ESPNOW_WEDGE_MIN_TRIES = 3;
static constexpr uint32_t ESPNOW_WEDGE_RETRY_MS  = 300000;   // out of remedies and still wedged: the ladder again
static uint8_t  s_wedgeLevel   = 0;   // loop() only: 0 none, 1 ESP-NOW restarted, 2 WiFi driver restarted, 3 out of remedies
static uint8_t  s_wedgeRemedy  = 0;   // loop() only: the last remedy run, 1 or 2
static uint32_t s_wedgeStartMs = 0;   // loop() only: the first frame of this wedge handed to the radio
static uint32_t s_wedgeActMs   = 0;   // loop() only: when the last remedy ran, or the ladder ran out
static uint32_t s_wedgeDoneAt  = 0;   // loop() only: s_completions when the wedge was found
static bool     s_wedgeSim     = false;   // loop() only: found while ?DEBUG,FAULT,TXWEDGE was armed
static bool     s_initFailed   = false;   // loop() only: the last remedy could not start ESP-NOW again
static uint32_t s_wedges = 0, s_wedgesCleared = 0;

// ?DEBUG,FAULT,TXWEDGE[,level]: the wedge as the driver makes it, without the driver - the first SIM_LOST frames
// "accepted" and never completed (each holds its slot, as a lost frame does), every one after that refused NO_MEM.
// Exactly what W1 showed for two hours on 2026-10-12 (in flight 8, every frame given up).
static constexpr int         SIM_LOST = 8;
static std::atomic<uint8_t>  s_sim{0};               // the remedy level that clears it; 0 = off
static std::atomic<int>      s_simHeld{0};           // simulated frames holding a slot

static const char *SIM_TAG = " (simulated, ?DEBUG,FAULT,TXWEDGE)";   // hil/runner.py ignores lines carrying it

// Give a slot back, never below zero: a stall reset may have zeroed the count under a frame still
// in the air, whose completion then arrives.
static void releaseSlot() {
  int v = s_inFlight.load();
  while (v > 0 && !s_inFlight.compare_exchange_weak(v, v - 1)) {}
}

// A frame handed to the radio, accepted or given up. Only a completion clears the count.
static void noteTry() {
  s_triesSinceDone++;
  uint32_t none = 0;
  s_firstTryMs.compare_exchange_strong(none, millis() | 1);
}

void wcbEspNowNoteWifiTask() {
  // Both ESP-NOW callbacks run on the WiFi task, and a send from that task only ever happens inside one of them, so
  // the handle is known before it is first needed. Stored every time, not once: a WiFi driver restart (the wedge's
  // remedy 2) ends that task and starts another, whose sends must not wait either.
  s_wifiTask = xTaskGetCurrentTaskHandle();
}

void wcbEspNowSendDone() {
  wcbEspNowNoteWifiTask();
  releaseSlot();
  const uint32_t now = millis();
  s_progressMs.store(now);
  s_lastDoneMs.store(now | 1);
  s_completions++;
  s_triesSinceDone.store(0);
  s_firstTryMs.store(0);
}

// The driver, or the simulated wedge in its place.
static esp_err_t driverSend(const uint8_t *mac, const uint8_t *data, size_t len) {
  if (s_sim.load()) {
    int h = s_simHeld.load();
    while (h < SIM_LOST && !s_simHeld.compare_exchange_weak(h, h + 1)) {}
    return h < SIM_LOST ? ESP_OK : ESP_ERR_ESPNOW_NO_MEM;
  }
  return esp_now_send(mac, data, len);
}

esp_err_t wcbEspNowSend(const uint8_t *mac, const uint8_t *data, size_t len) {
  // A wedge remedy is taking ESP-NOW down and up again (loop(), wcbEspNowRestartBegin..End): the frame goes the
  // caller's own error path, as a send before esp_now_init() would.
  if (s_restarting.load()) return ESP_ERR_ESPNOW_NOT_INIT;
  const bool wifiTask = s_wifiTask && xTaskGetCurrentTaskHandle() == s_wifiTask;
  const int cap = wifiTask ? ESPNOW_INFLIGHT_MAX : ESPNOW_INFLIGHT_TASK;
  const uint32_t start = millis();
  for (;;) {
    int v = s_inFlight.load();
    while (v < cap && !s_inFlight.compare_exchange_weak(v, v + 1)) {}
    if (v < cap) {
      // The slot is taken BEFORE the send: its completion can run on core 0 before esp_now_send returns.
      const esp_err_t r = driverSend(mac, data, len);
      if (r == ESP_OK) {
        s_progressMs.store(millis());
        noteTry();
        return r;
      }
      releaseSlot();
      if (r != ESP_ERR_ESPNOW_NO_MEM) return r;   // not a full queue: the caller's own error path
    } else if ((uint32_t)(millis() - s_progressMs.load()) > ESPNOW_STALL_MS) {
      const int was = s_inFlight.exchange(0);
      if (was > 0) s_lost += (uint32_t)was;
      continue;
    }
    if (wifiTask || (uint32_t)(millis() - start) >= ESPNOW_SEND_WAIT_MS) {
      s_dropped++;
      noteTry();
      return ESP_ERR_ESPNOW_NO_MEM;
    }
    if (s_restarting.load()) return ESP_ERR_ESPNOW_NOT_INIT;   // a remedy began while this waited
    vTaskDelay(1);
  }
}

uint32_t wcbEspNowDropped() { return s_dropped.load(); }

void wcbEspNowResetStats() { s_dropped.store(0); }

uint32_t wcbEspNowLostFrames()     { return s_lost.load(); }
uint32_t wcbEspNowWedges()         { return s_wedges; }
uint32_t wcbEspNowWedgesCleared()  { return s_wedgesCleared; }
bool     wcbEspNowRestarting()     { return s_restarting.load(); }
uint8_t  wcbEspNowSimulatedWedge() { return s_sim.load(); }

void wcbEspNowSimulateWedge(uint8_t level) {
  if (level > 3) level = 3;
  if (!level) {
    // Off: the frames it held were "lost" by nobody - give their slots back, or the cap stays SIM_LOST short.
    s_sim.store(0);
    const int held = s_simHeld.exchange(0);
    for (int i = 0; i < held; i++) releaseSlot();
    return;
  }
  s_sim.store(level);
}

// The heap a WiFi TX buffer comes from: byte-addressable. MALLOC_CAP_INTERNAL also counts the classic ESP32's ~39 KB
// 32-bit-only IRAM heap, which reads a constant "largest block 38900" whatever the state (#58; W1's stall lines on
// 2026-10-12 said exactly that while its byte heap was down to 5.5 KB).
static void heapFigures(unsigned *free_, unsigned *largest, unsigned *minimum) {
  *free_   = (unsigned)heap_caps_get_free_size(MALLOC_CAP_8BIT);
  *largest = (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT);
  *minimum = (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_8BIT);
}

static void watchStall(uint32_t d, uint32_t now) {
  if (d != s_dropsSeen) { s_dropsSeen = d; s_lastDropMs = now; }
  const uint32_t done     = s_lastDoneMs.load();
  const uint32_t idle     = done ? now - done : now;   // none since boot: the whole uptime
  const bool     dropping = s_lastDropMs && (uint32_t)(now - s_lastDropMs) < ESPNOW_STALL_REPORT_MS;
  if (dropping && idle >= ESPNOW_STALL_REPORT_MS) {
    if (!s_stallSinceMs) {
      s_stallSinceMs  = (now - idle) | 1;
      s_stallReportMs = 0;
      s_stallSim      = s_sim.load() != 0;
    }
    if (!s_stallReportMs || (uint32_t)(now - s_stallReportMs) >= ESPNOW_STALL_REPEAT_MS) {
      s_stallReportMs = now;
      unsigned f, l, m;
      heapFigures(&f, &l, &m);
      Serial.printf("[MESH] ESP-NOW transmit stalled %lus: no frame completed while frames are given up "
                    "(in flight %d, heap free %u, largest block %u, min since boot %u)%s\n",
                    (unsigned long)((now - s_stallSinceMs) / 1000), s_inFlight.load(), f, l, m,
                    (s_stallSim || s_sim.load()) ? SIM_TAG : "");
    }
  } else if (s_stallSinceMs && idle < ESPNOW_STALL_REPORT_MS) {
    // A wedge the watch below found says its own end, with what ended it.
    if (!s_wedgeLevel)
      Serial.printf("[MESH] ESP-NOW transmit recovered after %lus\n", (unsigned long)((now - s_stallSinceMs) / 1000));
    s_stallSinceMs = 0;
  }
}

static void reportDrops(uint32_t d, uint32_t now) {
  if (d < s_reportedDrops) s_reportedDrops = d;          // the stats were reset
  if (d == s_reportedDrops) return;
  if (s_lastReportMs && (uint32_t)(now - s_lastReportMs) < 1000) return;
  // Said here, from loop(): most of these frames were given up where nothing can print - the WiFi task,
  // or the ?RTERM mirror's own send, which a print would feed straight back into.
  Serial.printf("[MESH] %lu ESP-NOW frame(s) not sent: the radio's queue stayed full (%lu since the stats were "
                "reset)\n", (unsigned long)(d - s_reportedDrops), (unsigned long)d);
  s_reportedDrops = d;
  s_lastReportMs = now;
}

static bool wedgedNow(uint32_t now) {
  const uint32_t first = s_firstTryMs.load();
  return first && s_triesSinceDone.load() >= ESPNOW_WEDGE_MIN_TRIES && (uint32_t)(now - first) >= ESPNOW_WEDGE_MS;
}

static unsigned long wedgeSecs(uint32_t now) { return (unsigned long)((now - s_wedgeStartMs) / 1000); }

static uint8_t watchWedge(bool hold, const char *noWifiWhy, uint32_t now) {
  const char *tag = s_wedgeSim ? SIM_TAG : "";
  if (s_wedgeLevel && s_completions.load() != s_wedgeDoneAt) {   // a frame completed: transmit is back
    s_wedgesCleared++;
    Serial.printf("[MESH] ESP-NOW transmit wedge cleared after %lus: %s, frames complete again, no reboot%s\n",
                  wedgeSecs(now),
                  s_wedgeLevel == 3 ? "after the remedies ran out" :
                  s_wedgeRemedy == 2 ? "the WiFi driver restart cleared it" : "the ESP-NOW restart cleared it", tag);
    s_wedgeLevel = 0;
    s_wedgeRemedy = 0;
    s_initFailed = false;
    return 0;
  }
  if (hold) return 0;
  // A remedy that could not start ESP-NOW again: no frame reaches the radio to judge by, so try it again on the
  // retry schedule.
  if (s_initFailed) {
    const uint8_t again = s_wedgeRemedy == 2 && !noWifiWhy ? 2 : 1;
    if ((uint32_t)(now - s_wedgeActMs) < ESPNOW_WEDGE_RETRY_MS) return 0;
    Serial.printf("[MESH] ESP-NOW transmit wedged %lus and ESP-NOW is still down - trying the %s restart again%s\n",
                  wedgeSecs(now), again == 2 ? "WiFi driver" : "ESP-NOW", tag);
    s_wedgeActMs = now;
    return again;
  }
  if (!wedgedNow(now)) return 0;
  switch (s_wedgeLevel) {
    case 0: {
      s_wedges++;
      s_wedgeStartMs = s_firstTryMs.load();
      s_wedgeDoneAt  = s_completions.load();
      s_wedgeSim     = s_sim.load() != 0;
      tag = s_wedgeSim ? SIM_TAG : "";
      uint8_t mac[6] = {0};
      esp_wifi_get_mac(WIFI_IF_STA, mac);
      uint8_t ch = 0;
      wifi_second_chan_t second = WIFI_SECOND_CHAN_NONE;
      esp_wifi_get_channel(&ch, &second);
      // The power save in force: setup() turns it off, and the core's STA_START handler could turn it back on
      // (WCB.ino, WiFi.setSleep). Modem sleep holds frames until the radio wakes.
      wifi_ps_type_t ps = WIFI_PS_NONE;
      esp_wifi_get_ps(&ps);
      unsigned f, l, m;
      heapFigures(&f, &l, &m);
      Serial.printf("[MESH] ESP-NOW transmit wedged: %lu frame(s) handed to the radio over %lus and none completed "
                    "(in flight %d, %lu counted out unfinished since boot, STA MAC %02X:%02X:%02X:%02X:%02X:%02X, "
                    "channel %u, power save %s, heap free %u, largest block %u, min since boot %u) - restarting "
                    "ESP-NOW, no reboot (tracker #112)%s\n",
                    (unsigned long)s_triesSinceDone.load(), wedgeSecs(now), s_inFlight.load(),
                    (unsigned long)s_lost.load(), mac[0], mac[1], mac[2], mac[3], mac[4], mac[5], ch,
                    ps == WIFI_PS_NONE ? "off" : ps == WIFI_PS_MIN_MODEM ? "MIN_MODEM" : "MAX_MODEM", f, l, m, tag);
      s_wedgeLevel = s_wedgeRemedy = 1;
      s_wedgeActMs = now;
      return 1;
    }
    case 1:
      if (!noWifiWhy) {
        Serial.printf("[MESH] ESP-NOW transmit wedged %lus and the ESP-NOW restart did not clear it - restarting the "
                      "WiFi driver (this board runs no WiFi interface), no reboot%s\n", wedgeSecs(now), tag);
        s_wedgeLevel = s_wedgeRemedy = 2;
        s_wedgeActMs = now;
        return 2;
      }
      Serial.printf("[MESH] ESP-NOW transmit wedged %lus and the ESP-NOW restart did not clear it; the WiFi driver is "
                    "not restarted (%s) - only a reboot clears it (?reboot)%s\n", wedgeSecs(now), noWifiWhy, tag);
      s_wedgeLevel = 3;
      s_wedgeActMs = now;
      return 0;
    case 2:
      Serial.printf("[MESH] ESP-NOW transmit wedged %lus and the WiFi driver restart did not clear it - only a reboot "
                    "clears it (?reboot)%s\n", wedgeSecs(now), tag);
      s_wedgeLevel = 3;
      s_wedgeActMs = now;
      return 0;
    default:
      if ((uint32_t)(now - s_wedgeActMs) < ESPNOW_WEDGE_RETRY_MS) return 0;
      Serial.printf("[MESH] ESP-NOW transmit wedged %lus - trying the ESP-NOW restart again%s\n",
                    wedgeSecs(now), tag);
      s_wedgeLevel = s_wedgeRemedy = 1;
      s_wedgeActMs = now;
      return 1;
  }
}

uint8_t wcbEspNowService(bool hold, const char *noWifiWhy) {
  const uint32_t now = millis();
  const uint32_t d = s_dropped.load();
  watchStall(d, now);
  reportDrops(d, now);
  return watchWedge(hold, noWifiWhy, now);
}

void wcbEspNowRestartBegin(bool wifiToo) {
  (void)wifiToo;   // the WiFi task's handle is learned again from its successor's first callback
  s_restarting.store(true);
}

void wcbEspNowRestartEnd(uint8_t level, esp_err_t initResult, int peers) {
  // Whatever the radio held is gone with the driver's state; its callbacks never come.
  const int held = s_inFlight.exchange(0);
  if (held > 0) s_lost += (uint32_t)held;
  s_progressMs.store(millis());
  s_triesSinceDone.store(0);
  s_firstTryMs.store(0);
  // The simulated wedge: one this remedy clears ends here, any other starts losing again from nothing held.
  s_simHeld.store(0);
  if (s_sim.load() && s_sim.load() <= level) s_sim.store(0);
  s_restarting.store(false);
  const char *what = level == 2 ? "WiFi driver and ESP-NOW restarted" : "ESP-NOW restarted";
  s_initFailed = initResult != ESP_OK;
  if (s_initFailed)
    Serial.printf("[MESH] ESP-NOW transmit wedged: %s, but ESP-NOW did not start again (%s) - this board is off the "
                  "mesh; trying again in %lus, or ?reboot%s\n", what, esp_err_to_name(initResult),
                  (unsigned long)(ESPNOW_WEDGE_RETRY_MS / 1000), s_wedgeSim ? SIM_TAG : "");
  else
    Serial.printf("[MESH] ESP-NOW transmit wedged: %s, %d peer(s) registered again%s\n", what, peers,
                  s_wedgeSim ? SIM_TAG : "");
}
