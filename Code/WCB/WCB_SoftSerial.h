#pragma once
// ── S3-S5: EspSoftwareSerial RX + RMT (hardware-timed) TX ─────────────────────────────────────
// S3-S5 have no UART. EspSoftwareSerial used to do both directions in software, and its TX
// bit-bangs every bit against the CPU clock: an interrupt landing mid-byte compressed the rest of
// the byte (commands arrived cut short — CLAUDE.md rule 13), and the fix for THAT, enableIntTx(false),
// masked core 1's interrupts for ~90% of every transmitted byte, which blinded the soft-serial RX
// edge ISR on ALL three ports (tracker #16) and, before the UART0 ISR moved to core 0, corrupted USB
// input too (#57). There was no setting that gave clean TX and working RX at once.
//
// WcbSoftSerial keeps the library for RECEIVE (a GPIO ISR, unaffected once nothing masks
// interrupts; the library is vendored in src/EspSoftwareSerial and receives on level-emulated
// interrupts on the classic ESP32 - erratum GPIO-3.14, tracker #78) and hands TRANSMIT to an RMT
// channel: the peripheral clocks the start/data/stop bits out itself, so TX timing no longer depends on the CPU and no interrupt is ever masked for it.
// write() blocks until the bytes are on the wire, like the bit-banged write did, but it SLEEPS on the
// driver instead of busy-waiting, so the calling task gives the CPU up for the duration.
//
// Channels: classic ESP32 has 8 TX-capable RMT channels, ESP32-S3 has 4 — the status LED
// (Adafruit_NeoPixel) takes one, S3-S5 one each, which fills the S3 exactly. If a channel cannot be
// had (or the port is configured with anything but 8N1), begin() falls back to the library's
// bit-banged TX for that port and says so; applySoftSerialIntTx() then governs it as before.
//
// A port whose TX pin is borrowed by ;P (processPWMOutput takes it with pinMode) is re-routed back to
// its RMT channel on the next serial write — see noteTxPinBorrowed().

#include "src/EspSoftwareSerial/SoftwareSerial.h"   // vendored + patched (tracker #78, CLAUDE.md rule 7)
#include "driver/rmt_tx.h"
#include "driver/rmt_encoder.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

class WcbSoftSerial : public SoftwareSerial {
public:
  explicit WcbSoftSerial(uint8_t port) : SoftwareSerial(), _port(port) {}

  // Same shape as the library's begin() the firmware already calls; hides it (non-virtual).
  void begin(uint32_t baud, EspSoftwareSerial::Config config, int8_t rxPin, int8_t txPin,
             bool invert, int bufCapacity = 64);
  void end();

  using SoftwareSerial::write;
  size_t write(uint8_t byte) override;
  size_t write(const uint8_t *buffer, size_t size) override;
  int    availableForWrite() override;

  // available/read/peek run EspSoftwareSerial 8.1.0's rxBits(), which tests "ISR edge buffer empty"
  // and THEN reads micros() (SoftwareSerial.cpp:483). A task switch between the two lets edges pile
  // up unseen while the check still passes, so it injects a faux stop bit mid-byte (:486): the byte's
  // tail reads as 1s and the following bytes are framed from mid-byte until an idle gap. Several
  // priority-1 tasks share core 1 with the soft-port reader (loopTask, KyberRemoteTask,
  // MeshSerialOutTask), so a 1 ms time slice can land there - HIL input.soft_rx_under_soft_tx lost
  // 3 of 40 lines to it. Suspending the scheduler closes the window. ISRs keep running, so no edge
  // is lost. readBytes() is not wrapped (it spins on a timeout) - nothing here uses it on S3-S5.
  using SoftwareSerial::read;   // keep read(buf, n) visible
  int available() override;
  int read() override;
  int peek() override;

  bool rmtTx() const { return _ch != nullptr; }      // false = bit-banged fallback (or not begun)
  void noteTxPinBorrowed() { _borrowed = true; }      // ;P took the pin; reclaim it on next write

private:
  static size_t encodeCb(const void *data, size_t size, size_t symbolsWritten, size_t symbolsFree,
                         rmt_symbol_word_t *out, bool *done, void *arg);
  int  byteHalves(uint8_t b, uint16_t *dur, uint8_t *lvl) const;
  bool startRmt(uint32_t baud, int8_t txPin);
  void stopRmt();

  uint8_t              _port;
  int8_t               _txPin     = -1;
  rmt_channel_handle_t _ch        = nullptr;
  rmt_encoder_handle_t _enc       = nullptr;
  SemaphoreHandle_t    _lock      = nullptr;   // one transaction in flight per port
  uint32_t             _edge[11]  = {};        // tick offset of each bit boundary within a byte
  volatile size_t      _encPos    = 0;         // next byte to encode (encoder callback)
  size_t               _chunk     = 8;         // bytes per RMT transaction - see startRmt()
  volatile bool        _borrowed  = false;
};

extern WcbSoftSerial Serial3;
extern WcbSoftSerial Serial4;
extern WcbSoftSerial Serial5;
