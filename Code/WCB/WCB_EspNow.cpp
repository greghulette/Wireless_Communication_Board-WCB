#include "WCB_RemoteTerm.h"   // FIRST: redirects Serial to the wrapper setup() opens (CLAUDE.md rule 12)
#include "WCB_EspNow.h"
#include <Arduino.h>
#include <atomic>
#include <esp_now.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

// Why every send goes through here: WCB_EspNow.h.
//
// A frame of up to 252 bytes takes about 3 ms of air at ESP-NOW's 1 Mbps, so six in flight keep the
// radio busy for longer than a waiter's 1 ms sleep - the cap costs no throughput - and twelve hold
// the driver's share of the heap to a few KB.
static constexpr int      ESPNOW_INFLIGHT_TASK = 6;
static constexpr int      ESPNOW_INFLIGHT_MAX  = 12;
static constexpr uint32_t ESPNOW_SEND_WAIT_MS  = 50;
// The count says the radio is full, yet no frame has completed and none been accepted for this long:
// completions were lost (ESP-NOW re-initialised with frames queued), so the count starts over. A
// unicast that fails spends its MAC retries in ~30 ms, well inside it.
static constexpr uint32_t ESPNOW_STALL_MS      = 250;

static std::atomic<int>      s_inFlight{0};
static std::atomic<uint32_t> s_progressMs{0};   // millis() of the last completion or accepted send
static std::atomic<uint32_t> s_dropped{0};
static TaskHandle_t          s_wifiTask = nullptr;
static uint32_t              s_reportedDrops = 0;   // loop() only
static uint32_t              s_lastReportMs  = 0;   // loop() only

// Give a slot back, never below zero: a stall reset may have zeroed the count under a frame still
// in the air, whose completion then arrives.
static void releaseSlot() {
  int v = s_inFlight.load();
  while (v > 0 && !s_inFlight.compare_exchange_weak(v, v - 1)) {}
}

void wcbEspNowNoteWifiTask() {
  // Both ESP-NOW callbacks run on the WiFi task, and a send from that task only ever happens inside
  // one of them, so the handle is known before it is first needed.
  if (!s_wifiTask) s_wifiTask = xTaskGetCurrentTaskHandle();
}

void wcbEspNowSendDone() {
  wcbEspNowNoteWifiTask();
  releaseSlot();
  s_progressMs.store(millis());
}

esp_err_t wcbEspNowSend(const uint8_t *mac, const uint8_t *data, size_t len) {
  const bool wifiTask = s_wifiTask && xTaskGetCurrentTaskHandle() == s_wifiTask;
  const int cap = wifiTask ? ESPNOW_INFLIGHT_MAX : ESPNOW_INFLIGHT_TASK;
  const uint32_t start = millis();
  for (;;) {
    int v = s_inFlight.load();
    while (v < cap && !s_inFlight.compare_exchange_weak(v, v + 1)) {}
    if (v < cap) {
      // The slot is taken BEFORE the send: its completion can run on core 0 before esp_now_send returns.
      const esp_err_t r = esp_now_send(mac, data, len);
      if (r == ESP_OK) {
        s_progressMs.store(millis());
        return r;
      }
      releaseSlot();
      if (r != ESP_ERR_ESPNOW_NO_MEM) return r;   // not a full queue: the caller's own error path
    } else if ((uint32_t)(millis() - s_progressMs.load()) > ESPNOW_STALL_MS) {
      s_inFlight.store(0);
      continue;
    }
    if (wifiTask || (uint32_t)(millis() - start) >= ESPNOW_SEND_WAIT_MS) {
      s_dropped++;
      return ESP_ERR_ESPNOW_NO_MEM;
    }
    vTaskDelay(1);
  }
}

uint32_t wcbEspNowDropped() { return s_dropped.load(); }

void wcbEspNowResetStats() { s_dropped.store(0); }

void wcbEspNowReportDrops() {
  const uint32_t d = s_dropped.load();
  if (d < s_reportedDrops) s_reportedDrops = d;          // the stats were reset
  if (d == s_reportedDrops) return;
  const uint32_t now = millis();
  if (s_lastReportMs && (uint32_t)(now - s_lastReportMs) < 1000) return;
  // Said here, from loop(): most of these frames were given up where nothing can print - the WiFi task,
  // or the ?RTERM mirror's own send, which a print would feed straight back into.
  Serial.printf("[MESH] %lu ESP-NOW frame(s) not sent: the radio's queue stayed full (%lu since the stats were "
                "reset)\n", (unsigned long)(d - s_reportedDrops), (unsigned long)d);
  s_reportedDrops = d;
  s_lastReportMs = now;
}
