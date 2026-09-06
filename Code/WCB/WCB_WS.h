#pragma once

// ════════════════════════════════════════════════════════════════════════════
//  WCB WebSocket command endpoint — ws://<board-ip>/ws
// ════════════════════════════════════════════════════════════════════════════
//
//  A second mouth for the SAME protocol the USB port speaks. Every line that
//  arrives here goes to processSerialCommandHelper(), the identical dispatcher
//  the Serial0 reader feeds, so there is exactly one implementation of the
//  command surface and the two transports cannot drift.
//
//  Only runs when ?WIFI has brought an interface up (see WCB_WiFi.h). With WiFi
//  off this costs nothing but the linked code.
//
//  ── THE CONCURRENCY RULE THIS FILE EXISTS TO OBEY ───────────────────────────
//  The httpd task is NOT the loop task. It runs on Core 0 beside the ESP-NOW
//  receive callback, and the same prohibition applies: the handler may only
//  copy, enqueue and return. processSerialCommandHelper() writes NVS, drives
//  bit-banged SoftwareSerial and blocks on ETM retries — none of which may
//  happen there. drain() runs the command from loop() on Core 1 instead, which
//  is the same split drainMgmtOut() already uses for mesh-relayed output.
//
//  The handler also never PRINTS. UART0 has no TX buffer, so a print from the
//  httpd task blocks it for the duration and, with HAL locks on, holds the UART0
//  mutex against loop() too — the failure that fired the WiFi watchdog in
//  WCB_RemoteTerm.cpp:14. Drops are counted and reported from loop() instead.
//
//  ── WHY THE LINE CAP IS 1536 AND NOT MgmtRelay's 384 ────────────────────────
//  The relay's WebSocket only ever carries `?OTA,DATA` lines, whose firmware
//  chunk is 192 bytes. This endpoint is the one an OTA over WiFi would use, and
//  that is the DIRECT path — `?OTALOCAL,DATA,<base64>` with a 1024-byte chunk
//  (Wizard app.js `const CHUNK = 1024`), which base64-encodes to ~1368 chars
//  plus the prefix. A 384-byte cap would truncate every DATA frame at the
//  line-too-long guard and the transfer would ACK the BEGIN and then move
//  nothing. Keep this at or above the largest line the USB path accepts.
// ════════════════════════════════════════════════════════════════════════════

#include <Arduino.h>

// Start the server. Safe to call when WiFi never came up — it does nothing and
// returns false. Self-contained: on any failure it says so and leaves everything
// inert, so a WebSocket that will not start can never take the board down.
bool wcbWsBegin();

// Serviced every loop() pass. Runs at most ONE queued command, then flushes
// buffered output to the sockets. Never blocks.
void wcbWsService();

// True when at least one client is connected. Used by the Serial tee below to
// skip all work in the overwhelmingly common case of nobody being attached.
bool wcbWsSinkLive();

// Output tee, called from WCBSerial::write() so a WebSocket client sees exactly
// what a USB console would. MUST NOT print, allocate or block: it runs inside
// arbitrary Serial.printf calls on whatever task is printing. It only appends to
// a buffer; the actual TCP send happens in wcbWsService() from loop().
void wcbWsSinkWrite(uint8_t c);

// Number of connected clients — reported by ?WIFI.
int wcbWsClientCount();

// True once httpd is actually listening. Distinct from wcbWsClientCount() == 0,
// which is also what a server that FAILED to start reports — ?WIFI needs to tell
// "up, nobody connected" from "never came up", because those want different fixes.
bool wcbWsRunning();
