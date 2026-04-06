// ════════════════════════════════════════════════════════════════
//  WCB_RemoteTerm.cpp
//  Implementation of WCBSerial and relay-side packet handler.
// ════════════════════════════════════════════════════════════════

#include "WCB_RemoteTerm.h"
#include "WCB_Storage.h"
#include <string.h>

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

size_t WCBSerial::write(uint8_t c) {
  size_t r = HardwareSerial::write(c);    // always send to USB
  if (_relayWCB) _bufChar(c);
  return r;
}

size_t WCBSerial::write(const uint8_t *buf, size_t size) {
  size_t r = HardwareSerial::write(buf, size);  // always send to USB
  if (_relayWCB) {
    for (size_t i = 0; i < size; i++) _bufChar(buf[i]);
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
void WCBSerial::_flushLine() {
  if (_lineLen == 0) return;
  _sendPacket(_lineBuf, (uint8_t)_lineLen);
  _lineLen = 0;
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
  esp_now_send(WCBMacAddresses[_relayWCB - 1], (uint8_t *)&pkt, sizeof(pkt));
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

void rtermRelayHandlePacket(const uint8_t *data) {
  espnow_struct_remote_term pkt;
  memcpy(&pkt, data, sizeof(pkt));
  pkt.structPassword[sizeof(pkt.structPassword) - 1] = '\0';

  // Validate password
  if (strncmp(pkt.structPassword, espnowPassword, sizeof(pkt.structPassword) - 1) != 0) return;

  if (pkt.packetType != PACKET_TYPE_REMOTE_TERM) return;
  if (pkt.textLen == 0 || pkt.textLen > RTERM_TEXT_SIZE)          return;

  // Null-terminate and strip trailing CR/LF so we control the line ending
  char buf[RTERM_TEXT_SIZE + 1];
  memcpy(buf, pkt.text, pkt.textLen);
  buf[pkt.textLen] = '\0';

  size_t len = pkt.textLen;
  while (len > 0 && (buf[len - 1] == '\n' || buf[len - 1] == '\r')) len--;
  buf[len] = '\0';

  if (len > 0) {
    // Print to relay's USB serial — wizard parses the [TERM:N] prefix
    // and routes the text to board N's terminal pane.
    // We call HardwareSerial::printf directly (via WCBDebugSerial's parent)
    // to avoid triggering our own forwarding loop on the relay
    // (relay's _relayWCB is always 0, so the loop check is redundant
    //  but explicit parent-class use documents intent clearly).
    WCBDebugSerial.printf("[TERM:%d]%s\n", (int)pkt.sourceWCB, buf);
  }
}
