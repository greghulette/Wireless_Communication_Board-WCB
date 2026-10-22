#pragma once
// ── ESP-NOW send back-pressure (tracker #102, #107) ──────────────────────────────────────────
// Every frame esp_now_send accepts holds a WiFi-driver buffer from the heap until the radio has
// sent it, which the send callback reports. Nothing used to wait for that. A sender that outran
// the radio filled the driver's queue: esp_now_send failed ESP_ERR_ESPNOW_NO_MEM and the frame was
// lost unnoticed (an ?RTERM mirror of ?backup lost ten short lines in a row, #107). A sustained
// flood took the ~18 KB AP-mode heap with it (CLAUDE.md rule 14): the WiFi driver's next allocation,
// its first stdio print on core 0, aborted the board (#102, a console flood of JSON broadcasts).
//
// So EVERY ESP-NOW send goes through wcbEspNowSend(), never esp_now_send() directly (CLAUDE.md
// rule 16; tests/hil/selftest.py checks the tree). It bounds the frames in flight:
//  - A task - loop(), the serial, Kyber, raw-forwarding, mesh-out and PWM tasks - sleeps in 1 ms
//    ticks while ESPNOW_INFLIGHT_TASK (12) frames are in flight, for up to ESPNOW_SEND_WAIT_MS
//    (300 ms), and gives the frame up after that. Its output then goes at the radio's pace, and input
//    backs up into the queues and UART rings behind it, which count what they lose.
//  - The WiFi task never waits (CLAUDE.md rule 11): the send callbacks that free a slot run on that
//    same task. It sends the ETM ACKs from the receive callback, and anything it prints while an
//    ?RTERM session mirrors the console. It may go on to ESPNOW_INFLIGHT_MAX (20), then drops.
// A frame given up is counted (?STATS) and reported by loop(), at most one line a second. Frames given up while none
// has completed for 5 s are a stall, reported with the heap figures, as is the end of one.
//
// ── The transmit wedge (tracker #112) ──────────────────────────────────────────────────────────
// The WiFi driver sometimes loses a frame esp_now_send accepted: it never goes on the air, its send callback never
// comes, and the TX buffer it holds is never given back. Once enough are lost - or one leaves the driver's TX queue
// broken, so every frame queued after it is lost too - esp_now_send refuses every frame with ESP_ERR_ESPNOW_NO_MEM,
// heartbeats included, and the board's peers call it offline while it still hears them. Reported upstream with the
// same signature on ESP32 and ESP32-S3 under IDF 5.5 (espressif/esp-idf #18682, #19269: ESP-NOW's insert into the
// driver's TX queue races the driver's own task); seen here on W2 (ESP32, 2026-10-06), W1 (ESP32, 2026-10-12) and W3
// (ESP32-S3, 2026-10-21/22), each under a sustained ETM or remote-terminal load. Only a WiFi driver restart
// (esp_wifi_deinit/init) or a reboot clears it for certain; an ESP-NOW restart alone sometimes does.
// So wcbEspNowService() watches for it - frames handed to the radio for ESPNOW_WEDGE_MS, at least
// ESPNOW_WEDGE_MIN_TRIES of them, and not one completed - and has loop() put the radio back without a reboot:
//  1. ESP-NOW restarted (esp_now_deinit/init, both callbacks, every peer registered again from RAM);
//  2. still wedged ESPNOW_WEDGE_MS later: the WiFi driver restarted too, on a board that booted with WiFi OFF only -
//     an access point or a joined network is never torn down under its clients (docs/WIFI_DESIGN.md §4). A board
//     that runs one says that only a reboot clears it;
//  3. still wedged: the line every ESPNOW_STALL_REPEAT_MS, the ladder tried again every ESPNOW_WEDGE_RETRY_MS.
// Never while the radio is away on another channel or a restart is pending (rule 18, rule 11). Each step prints one
// ungated "[MESH] ESP-NOW transmit wedged ..." line, and its end one "[MESH] ESP-NOW transmit wedge cleared ..." line,
// which the HIL harness greps (hil/runner.py). ?DEBUG,FAULT,TXWEDGE[,1-3] simulates the wedge (wcbEspNowSimulateWedge);
// every line it causes says "(simulated, ?DEBUG,FAULT,TXWEDGE)".
#include <stdint.h>
#include <stddef.h>
#include "esp_err.h"

esp_err_t wcbEspNowSend(const uint8_t *mac, const uint8_t *data, size_t len);
void      wcbEspNowSendDone();       // espNowSendCallback, once per frame sent (WiFi task)
void      wcbEspNowNoteWifiTask();   // espNowReceiveCallback, first thing: learns the WiFi task
uint32_t  wcbEspNowDropped();        // frames given up since boot or the last ?STATS reset
void      wcbEspNowResetStats();     // resetESPNowStats()

// loop(), every pass: the drop line (one a second while frames are given up), stalls, and the wedge watch. Returns
// the remedy loop() is to run now - 0 none, 1 restart ESP-NOW, 2 restart the WiFi driver and ESP-NOW - as
// wcbEspNowRestartBegin(), the restart, wcbEspNowRestartEnd(). hold: the radio is away or a restart is pending, so no
// remedy now. noWifiWhy: why the WiFi driver may not be restarted on this board now, or nullptr when it may.
uint8_t   wcbEspNowService(bool hold, const char *noWifiWhy);
void      wcbEspNowRestartBegin(bool wifiToo);   // senders refused from here; wifiToo: the WiFi task is forgotten
void      wcbEspNowRestartEnd(uint8_t level, esp_err_t initResult, int peers);   // counters reset, says what it did
bool      wcbEspNowRestarting();     // a remedy is putting ESP-NOW back: make no esp_now_* call (peer adds included)
uint32_t  wcbEspNowLostFrames();     // frames the radio accepted and never completed, since boot (?STATS)
uint32_t  wcbEspNowWedges();         // wedges detected since boot (?STATS)
uint32_t  wcbEspNowWedgesCleared();  // ...and ended by a remedy or on their own (?STATS)
// ?DEBUG,FAULT,TXWEDGE[,level] (a test knob, RAM only): from now every frame is lost the way a wedged driver loses it -
// the first 8 "accepted" and never completed, then every one refused NO_MEM. level 1 ends at the next ESP-NOW
// restart, 2 at the next WiFi driver restart, 3 at neither (FAULT,OFF or a reboot). 0 = off.
void      wcbEspNowSimulateWedge(uint8_t level);
uint8_t   wcbEspNowSimulatedWedge();
