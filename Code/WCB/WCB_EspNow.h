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
// has been accepted or completed for 5 s are a stall, reported with the heap figures, as is the recovery from one.
#include <stdint.h>
#include <stddef.h>
#include "esp_err.h"

esp_err_t wcbEspNowSend(const uint8_t *mac, const uint8_t *data, size_t len);
void      wcbEspNowSendDone();       // espNowSendCallback, once per frame sent (WiFi task)
void      wcbEspNowNoteWifiTask();   // espNowReceiveCallback, first thing: learns the WiFi task
uint32_t  wcbEspNowDropped();        // frames given up since boot or the last ?STATS reset
void      wcbEspNowResetStats();     // resetESPNowStats()
void      wcbEspNowReportDrops();    // loop(): one line a second while frames are being given up, and stalls
