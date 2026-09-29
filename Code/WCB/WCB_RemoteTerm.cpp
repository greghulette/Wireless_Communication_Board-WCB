// ════════════════════════════════════════════════════════════════
//  WCB_RemoteTerm.cpp
//  Implementation of WCBSerial and relay-side packet handler.
// ════════════════════════════════════════════════════════════════

#include "WCB_RemoteTerm.h"
#include "WCB_Storage.h"
#include "WCB_WS.h"           // WebSocket output tee — see write() below
#include "WCB_EspNow.h"       // wcbEspNowSend: every ESP-NOW send is paced (CLAUDE.md rule 16)
#include <string.h>
#include <atomic>
#include <freertos/FreeRTOS.h>
#include <freertos/ringbuf.h>

// ── Relay-side line ring ──────────────────────────────────────────────────────
// Packets are queued from the WiFi-task callback and printed in loop().
// This prevents blocking UART writes inside the ESP-NOW receive callback,
// which caused the WiFi watchdog to fire and restart the board.
// The relay prints each line with a "[TERM:n]" prefix at the same baud rate the
// target printed it, so a burst of short lines always leaves a backlog here. A
// ring of variable-length items holds ~80 short lines in the heap the old 16
// fixed 163-byte slots took (2.6 KB), which lost ten short ?backup lines in a
// row (tracker #107). A line that still does not fit is counted and reported.
// Not bigger: on a WiFi-AP board every KB is felt (CLAUDE.md rule 14) - a 3 KB
// ring was enough to move where a 2950-character ?SEQ,SAVE ran out of heap.
#define RTERM_RING_BYTES  2560

static RingbufHandle_t s_rtermRing = nullptr;
static std::atomic<uint32_t> s_rtermLost{0};   // lines the ring had no room for
static volatile uint8_t s_rtermLostFrom = 0;   // the source WCB of the last one

static bool ensureRing() {
  if (!s_rtermRing)
    s_rtermRing = xRingbufferCreate(RTERM_RING_BYTES, RINGBUF_TYPE_NOSPLIT);
  return s_rtermRing != nullptr;
}

// ── Globals from WCB.ino ──────────────────────────────────────────
// These are defined in WCB.ino (the concatenated sketch translation unit).
// Extern declarations give WCB_RemoteTerm.cpp access without duplication.
extern char    espnowPassword[];
extern uint8_t WCBMacAddresses[MAX_WCB_COUNT][6];
extern int     WCB_Number;

// ── Global instance ────────────────────────────────────────────────
// This single WCBSerial(0) instance owns UART0 for the entire sketch.
// Arduino's built-in "Serial" symbol is redirected to this object via
// the #define in WCB_RemoteTerm.h, so setup()'s Serial.begin(115200)
// correctly initialises this instance.
WCBSerial WCBDebugSerial(0);

// ════════════════════════════════════════════════════════════════
//  write() overrides — pass-through + optional line buffering
// ════════════════════════════════════════════════════════════════

// A WebSocket client is a THIRD destination alongside USB and the mesh relay.
// Teeing here rather than at each print site is what keeps the three transports
// from drifting — every handler goes on printing exactly as it always has.
// wcbWsSinkWrite() only appends to a buffer: no print (that would recurse
// straight back into here), no allocation, no TCP send.
size_t WCBSerial::write(uint8_t c) {
  size_t r = HardwareSerial::write(c);    // always send to USB
  if (_relayWCB) _bufChar(c);
  if (wcbWsSinkLive()) wcbWsSinkWrite(c);
  return r;
}

size_t WCBSerial::write(const uint8_t *buf, size_t size) {
  size_t r = HardwareSerial::write(buf, size);  // always send to USB
  if (_relayWCB) {
    for (size_t i = 0; i < size; i++) _bufChar(buf[i]);
  }
  if (wcbWsSinkLive()) {
    for (size_t i = 0; i < size; i++) wcbWsSinkWrite(buf[i]);
  }
  return r;
}

// ════════════════════════════════════════════════════════════════
//  Private helpers
// ════════════════════════════════════════════════════════════════

// Buffer one character; flush on newline or when buffer is full.
void WCBSerial::_bufChar(uint8_t c) {
  if (c == '\r') return;          // strip CR; flush only on LF

  _lineBuf[_lineLen++] = (char)c;

  if (c == '\n' || _lineLen >= LINEBUF_SIZE) {
    _flushLine();
  }
}

// Send the accumulated line buffer via ESP-NOW and reset it.
// The line is taken out BEFORE the send: the send may now wait for the radio
// (wcbEspNowSend), and whatever another task printed meanwhile went into
// _lineBuf, which a reset after the send threw away.
void WCBSerial::_flushLine() {
  if (_lineLen == 0) return;
  char line[LINEBUF_SIZE];
  const uint8_t n = (uint8_t)_lineLen;
  memcpy(line, _lineBuf, n);
  _lineLen = 0;
  _sendPacket(line, n);
}

// Build and unicast a REMOTE_TERM packet to the relay board.
void WCBSerial::_sendPacket(const char *data, uint8_t len) {
  if (!_relayWCB || _relayWCB > MAX_WCB_COUNT) return;

  espnow_struct_remote_term pkt;
  memset(&pkt, 0, sizeof(pkt));

  strncpy(pkt.structPassword, espnowPassword, sizeof(pkt.structPassword) - 1);
  pkt.packetType = PACKET_TYPE_REMOTE_TERM;
  pkt.sourceWCB  = (uint8_t)WCB_Number;
  pkt.destWCB    = _relayWCB;
  pkt.textLen    = (len > RTERM_TEXT_SIZE) ? RTERM_TEXT_SIZE : len;
  memcpy(pkt.text, data, pkt.textLen);

  // Unicast to the relay board's pre-computed MAC address.
  // WCBMacAddresses is indexed 0-based (WCB1 → index 0).
  // Ensure the relay is a registered ESP-NOW peer first: a relay numbered above
  // this board's WCBQ (e.g. a high-numbered MgmtRelay) may not be in the peer
  // table, and esp_now_send would then fail ESP_ERR_ESPNOW_NOT_FOUND and silently
  // drop every mirrored line. Mirrors the on-demand registration the ETM retry uses.
  if (!esp_now_is_peer_exist(WCBMacAddresses[_relayWCB - 1])) {
    esp_now_peer_info_t p = {};
    memcpy(p.peer_addr, WCBMacAddresses[_relayWCB - 1], 6);
    p.channel = 0; p.encrypt = false;
    esp_now_add_peer(&p);   // best-effort; if the peer table is full the send fails as before
  }
  // Paced like every ESP-NOW send: a burst of short lines outran the radio, and each
  // failed esp_now_send was a mirrored line lost unnoticed (tracker #107). Nothing can
  // print here - a print would come straight back into this mirror - so a line given
  // up is counted and loop() says so (wcbEspNowReportDrops).
  wcbEspNowSend(WCBMacAddresses[_relayWCB - 1], (uint8_t *)&pkt, sizeof(pkt));
}

// ════════════════════════════════════════════════════════════════
//  Session management
// ════════════════════════════════════════════════════════════════

void WCBSerial::startSession(uint8_t relayWCB) {
  _lineLen  = 0;
  memset(_lineBuf, 0, sizeof(_lineBuf));
  _relayWCB = relayWCB;
  // Announce over USB (forwarding is now active so the relay sees it too)
  HardwareSerial::printf("[RTERM] Session started → relay WCB%d\n", relayWCB);
}

void WCBSerial::stopSession() {
  // Flush anything pending before closing
  if (_lineLen > 0) _flushLine();
  _relayWCB = 0;
  _lineLen  = 0;
  HardwareSerial::println("[RTERM] Session stopped");
}

// ════════════════════════════════════════════════════════════════
//  Relay-side packet handler
//  Called from espNowReceiveCallback when
//  len == sizeof(espnow_struct_remote_term).
// ════════════════════════════════════════════════════════════════

// Called from the ESP-NOW receive callback (WiFi task context).
// MUST NOT do blocking serial I/O here — doing so starves the WiFi
// stack and triggers the watchdog, crashing the board.
// Instead, copy the relevant fields into the queue; rtermRelayDrain()
// prints them from the main loop.
void rtermRelayHandlePacket(const uint8_t *data) {
  espnow_struct_remote_term pkt;
  memcpy(&pkt, data, sizeof(pkt));
  pkt.structPassword[sizeof(pkt.structPassword) - 1] = '\0';

  if (strncmp(pkt.structPassword, espnowPassword, sizeof(pkt.structPassword) - 1) != 0) return;
  if (pkt.packetType != PACKET_TYPE_REMOTE_TERM) return;
  if (pkt.textLen == 0 || pkt.textLen > RTERM_TEXT_SIZE) return;

  // One item: the source WCB, then the text (no terminator).
  uint8_t item[1 + RTERM_TEXT_SIZE];
  item[0] = pkt.sourceWCB;
  memcpy(item + 1, pkt.text, pkt.textLen);

  // Non-blocking — count the line lost if the ring is full rather than block the
  // WiFi task. This runs in the WiFi-task context (NOT an ISR), so the plain send
  // with a zero timeout, not the FromISR variant.
  if (!ensureRing() || xRingbufferSend(s_rtermRing, item, 1 + pkt.textLen, 0) != pdTRUE) {
    s_rtermLost++;
    s_rtermLostFrom = pkt.sourceWCB;
  }
}

// Called from loop() on the relay board — safe to do serial I/O here.
void rtermRelayDrain() {
  if (s_rtermRing) {
    size_t size = 0;
    while (const uint8_t *item = (const uint8_t *)xRingbufferReceive(s_rtermRing, &size, 0)) {
      // Strip trailing CR/LF so we control the line ending
      const char *text = (const char *)item + 1;
      size_t len = size > 1 ? size - 1 : 0;
      while (len > 0 && (text[len - 1] == '\n' || text[len - 1] == '\r')) len--;

      if (len > 0) {
        // Use parent-class printf to avoid re-triggering the WCBSerial forwarding path
        WCBDebugSerial.HardwareSerial::printf("[TERM:%d]%.*s\n", (int)item[0], (int)len, text);
      }
      vRingbufferReturnItem(s_rtermRing, (void *)item);
    }
  }
  // After what did arrive: the lines lost came after those already in the ring.
  const uint32_t lost = s_rtermLost.exchange(0);
  if (lost)
    WCBDebugSerial.HardwareSerial::printf("[RTERM] %lu line(s) from WCB%d lost at this relay: its queue was full\n",
                                          (unsigned long)lost, (int)s_rtermLostFrom);
}
