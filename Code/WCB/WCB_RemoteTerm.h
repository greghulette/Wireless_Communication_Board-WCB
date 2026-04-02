#pragma once

// ════════════════════════════════════════════════════════════════
//  WCB Remote Terminal
//
//  Two-way USB-Serial output/input over ESP-NOW for remote boards.
//
//  HOW IT WORKS
//  ─────────────
//  TARGET board (the remote WCB):
//    • WCBSerial wraps HardwareSerial(0) (USB / UART0).
//    • All Serial.print / println / printf / write calls pass
//      through write() as usual (USB output is unchanged).
//    • When a session is active (_relayWCB > 0), each character
//      is also buffered; lines are flushed to the relay via
//      ESP-NOW PACKET_TYPE_REMOTE_TERM (unicast).
//
//  RELAY board (USB-connected to the wizard):
//    • Receives PACKET_TYPE_REMOTE_TERM packets.
//    • Prints  "[TERM:<sourceWCB>]<text>"  to its own USB serial.
//    • The wizard parses this prefix and routes the text to the
//      correct board's terminal pane.
//
//  USAGE IN WCB.ino
//  ─────────────────
//  1. #include "WCB_RemoteTerm.h"  ← must be the FIRST include.
//     This redefines Serial → WCBDebugSerial for the whole sketch.
//  2. setup(): Serial.begin(115200) now goes to WCBDebugSerial.
//  3. espNowReceiveCallback: add a size check for
//     sizeof(espnow_struct_remote_term) and call rtermRelayHandlePacket().
//  4. processLocalCommand: handle ?RTERM,START,<relayWCB>
//     and ?RTERM,STOP.
//
//  Serial1 … Serial5 are completely separate HardwareSerial
//  objects and are NOT affected by this wrapper.
// ════════════════════════════════════════════════════════════════

#include <HardwareSerial.h>
#include <esp_now.h>

// ── New packet type ───────────────────────────────────────────────
#define PACKET_TYPE_REMOTE_TERM  7

// ── Wire format ───────────────────────────────────────────────────
// Total size = 40+1+1+1+1+160 = 204 bytes.
// Deliberately distinct from all existing struct sizes:
//   espnow_struct_message_etm = 252
//   espnow_struct_message     = 249
//   espnow_struct_config_frag = 230
//   espnow_struct_mgmt        = 226
//   espnow_struct_config_req  =  43
#define RTERM_TEXT_SIZE  160

typedef struct __attribute__((packed)) {
  char    structPassword[40];
  uint8_t packetType;    // PACKET_TYPE_REMOTE_TERM
  uint8_t sourceWCB;     // target board that generated the output
  uint8_t destWCB;       // relay WCB this output is addressed to
  uint8_t textLen;       // valid bytes in text[]  (≤ RTERM_TEXT_SIZE)
  char    text[RTERM_TEXT_SIZE];
} espnow_struct_remote_term;
// static_assert done at runtime via setup() Serial.printf

// ── WCBSerial ─────────────────────────────────────────────────────
// Drop-in replacement for Arduino's stock HardwareSerial Serial.
// Inherits everything from HardwareSerial; only write() is overridden.
class WCBSerial : public HardwareSerial {
public:
  explicit WCBSerial(int uartNum) : HardwareSerial(uartNum) {}

  // All print / println / printf / write calls bottom out here.
  size_t write(uint8_t c)                       override;
  size_t write(const uint8_t *buf, size_t size) override;

  // ── Session management ─────────────────────────────────────────
  void    startSession(uint8_t relayWCB);   // begin forwarding to relay
  void    stopSession();                     // stop forwarding, flush pending

  bool    sessionActive() const { return _relayWCB != 0; }
  uint8_t getRelayWCB()   const { return _relayWCB; }

private:
  uint8_t _relayWCB = 0;

  // Line buffer — flushes on '\n' or when full
  static constexpr size_t LINEBUF_SIZE = RTERM_TEXT_SIZE;
  char   _lineBuf[LINEBUF_SIZE];
  size_t _lineLen = 0;

  void _bufChar(uint8_t c);
  void _flushLine();
  void _sendPacket(const char *data, uint8_t len);
};

// ── Global instance ────────────────────────────────────────────────
// Owns UART0 and replaces Arduino's built-in Serial.
// Defined in WCB_RemoteTerm.cpp.
extern WCBSerial WCBDebugSerial;

// ── Redirect all Serial.xxx calls to our instance ─────────────────
// This header must be included FIRST in WCB.ino so that all
// subsequent headers and source files see the redefinition.
#undef  Serial
#define Serial WCBDebugSerial

// ── Relay-side receive ─────────────────────────────────────────────
// Call from espNowReceiveCallback when:
//   len == sizeof(espnow_struct_remote_term)
void rtermRelayHandlePacket(const uint8_t *data);
