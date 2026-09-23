#include "WCB_RemoteTerm.h"   // FIRST — CLAUDE.md rule 12: without it every Serial line here vanishes
#include "WCB_SoftSerial.h"
#include "soc/soc_caps.h"

// See WCB_SoftSerial.h for why S3-S5 transmit through RMT.

extern bool isSerialPortPWMOutput(int port);

static const uint32_t RMT_HALF_MAX = 32767;   // rmt_symbol_word_t duration fields are 15 bits

// 10 MHz keeps every bit edge within 0.1 us (0.6 % of a bit at 115200), and a whole 10-bit frame
// fits one 15-bit duration down to 4800 baud. Below that, 1 MHz: a bit is >= 833 ticks, and the
// longest frame (110 baud) is split across a few halves by byteHalves().
static uint32_t resolutionFor(uint32_t baud) { return baud >= 4800 ? 10000000UL : 1000000UL; }

void WcbSoftSerial::begin(uint32_t baud, EspSoftwareSerial::Config config, int8_t rxPin,
                          int8_t txPin, bool invert, int bufCapacity) {
  stopRmt();
  _txPin = txPin;
  // RMT drives 8N1, non-inverted — the only framing the firmware uses. Anything else, or no free
  // channel, keeps the library's bit-banged TX so the port still works.
  if (config == SWSERIAL_8N1 && !invert && txPin >= 0 && startRmt(baud, txPin)) {
    SoftwareSerial::begin(baud, config, rxPin, -1, invert, bufCapacity);    // RX only
  } else {
    if (txPin >= 0)
      Serial.printf("[SOFTSERIAL] S%d: no RMT channel - TX falls back to bit-banging (rule 13 limits apply)\n", _port);
    SoftwareSerial::begin(baud, config, rxPin, txPin, invert, bufCapacity);
  }
}

void WcbSoftSerial::end() {
  SoftwareSerial::end();
  stopRmt();
}

bool WcbSoftSerial::startRmt(uint32_t baud, int8_t txPin) {
  if (baud == 0) return false;
  const uint32_t res = resolutionFor(baud);

  rmt_tx_channel_config_t cc = {};
  cc.gpio_num          = (gpio_num_t)txPin;
  cc.clk_src           = RMT_CLK_SRC_DEFAULT;
  cc.resolution_hz     = res;
  cc.mem_block_symbols = SOC_RMT_MEM_WORDS_PER_CHANNEL;   // one block: 64 (ESP32) / 48 (S3)
  cc.trans_queue_depth = 2;
  cc.flags.init_level  = 1;                               // UART idle is HIGH from the first instant
  if (rmt_new_tx_channel(&cc, &_ch) != ESP_OK) { _ch = nullptr; return false; }

  rmt_simple_encoder_config_t ec = {};
  ec.callback       = encodeCb;
  ec.arg            = this;
  ec.min_chunk_size = 8;          // byteHalves() never needs more than 7 symbols for one byte
  if (rmt_new_simple_encoder(&ec, &_enc) != ESP_OK || rmt_enable(_ch) != ESP_OK) {
    if (_enc) { rmt_del_encoder(_enc); _enc = nullptr; }
    rmt_del_channel(_ch);
    _ch = nullptr;
    return false;
  }
  // Bit boundaries measured from the start of the frame, rounded once each, so rounding never
  // accumulates across the ten bits.
  for (int k = 0; k <= 10; k++)
    _edge[k] = (uint32_t)(((uint64_t)k * res + baud / 2) / baud);
  // Bytes per transaction: few enough that the whole chunk is encoded into channel memory up
  // front, when rmt_transmit() starts it. The refill interrupt is then never needed mid-frame -
  // and it matters, because that ISR is not IRAM-safe: during a flash write (every NVS save) it is
  // held off, and a starved channel would put stale symbols on the wire. A late "done" interrupt
  // only stretches the idle-high gap between chunks, which any UART receiver accepts.
  // 2 symbols spare for the driver's end marker.
  const int maxSymPerByte = (res == 10000000UL) ? 5 : 7;
  _chunk = (SOC_RMT_MEM_WORDS_PER_CHANNEL - 2) / maxSymPerByte;
  if (_chunk < 1) _chunk = 1;
  if (!_lock) _lock = xSemaphoreCreateMutex();
  _borrowed = false;
  return true;
}

void WcbSoftSerial::stopRmt() {
  if (!_ch) return;
  if (_lock) xSemaphoreTake(_lock, portMAX_DELAY);   // never pull the channel from under a write
  rmt_disable(_ch);
  if (_enc) { rmt_del_encoder(_enc); _enc = nullptr; }
  rmt_del_channel(_ch);
  _ch = nullptr;
  if (_lock) xSemaphoreGive(_lock);
}

// One 8N1 frame as (duration, level) halves: start 0, data LSB first, stop 1. Runs of equal bits
// merge; a run longer than a 15-bit duration splits. Always returns an EVEN count (a symbol word
// holds two halves) by splitting the stop-bit run when needed — so every byte starts on a symbol
// boundary and the callback can encode whole bytes. Worst case 14 halves (7 symbols).
int WcbSoftSerial::byteHalves(uint8_t b, uint16_t *dur, uint8_t *lvl) const {
  uint8_t bits[10];
  bits[0] = 0;
  for (int i = 0; i < 8; i++) bits[i + 1] = (b >> i) & 1;
  bits[9] = 1;

  int h = 0;
  uint8_t  level  = bits[0];
  uint32_t tStart = 0;
  for (int k = 1; k <= 10; k++) {
    if (k < 10 && bits[k] == level) continue;
    uint32_t d = _edge[k] - tStart;
    while (d > 0) {
      const uint32_t part = d > RMT_HALF_MAX ? RMT_HALF_MAX : d;
      dur[h] = (uint16_t)part;
      lvl[h] = level;
      h++;
      d -= part;
    }
    if (k < 10) { level = bits[k]; tStart = _edge[k]; }
  }
  if (h & 1) {                       // odd: split the stop-bit half (>= 8 ticks at any baud here)
    const uint16_t d = dur[h - 1];
    dur[h - 1] = d - d / 2;
    dur[h]     = d / 2;
    lvl[h]     = 1;
    h++;
  }
  return h;
}

// Encodes whole bytes into the free symbol space; 0 asks to be called again with more room.
// symbolsWritten == 0 marks the start of a transaction. write() sizes each transaction to fit the
// channel memory, so this completes in the first call, from rmt_transmit() in the caller's task;
// the RMT ISR path (refill) is only a fallback and must stay ISR-safe regardless.
size_t WcbSoftSerial::encodeCb(const void *data, size_t size, size_t symbolsWritten,
                               size_t symbolsFree, rmt_symbol_word_t *out, bool *done, void *arg) {
  WcbSoftSerial *self = (WcbSoftSerial *)arg;
  if (symbolsWritten == 0) self->_encPos = 0;
  const uint8_t *bytes = (const uint8_t *)data;
  size_t n = 0;
  uint16_t dur[16];
  uint8_t  lvl[16];
  while (self->_encPos < size) {
    const int h = self->byteHalves(bytes[self->_encPos], dur, lvl);
    if (n + (size_t)(h / 2) > symbolsFree) break;
    for (int i = 0; i < h; i += 2) {
      out[n].duration0 = dur[i];     out[n].level0 = lvl[i];
      out[n].duration1 = dur[i + 1]; out[n].level1 = lvl[i + 1];
      n++;
    }
    self->_encPos++;
  }
  if (self->_encPos >= size) *done = true;
  return n;
}

size_t WcbSoftSerial::write(uint8_t byte) { return write(&byte, 1); }

size_t WcbSoftSerial::write(const uint8_t *buffer, size_t size) {
  if (!_ch) return SoftwareSerial::write(buffer, size);   // bit-banged fallback
  if (size == 0) return 0;
  // A port declared a PWM output carries pulses on this pin; serial bytes would corrupt them.
  if (isSerialPortPWMOutput(_port)) return 0;

  xSemaphoreTake(_lock, portMAX_DELAY);
  if (_borrowed) {                    // ;P re-muxed the pin to plain GPIO — route it back to RMT
    rmt_disable(_ch);
    rmt_tx_switch_gpio(_ch, (gpio_num_t)_txPin, false);
    rmt_enable(_ch);
    _borrowed = false;
  }
  rmt_transmit_config_t tc = {};
  tc.loop_count     = 0;
  tc.flags.eot_level = 1;             // leave the line idle HIGH
  size_t sent = 0;
  while (sent < size) {
    const size_t n = (size - sent) < _chunk ? (size - sent) : _chunk;
    if (rmt_transmit(_ch, _enc, buffer + sent, n, &tc) != ESP_OK) break;
    // Wait for the wire, not just the queue: the encoder reads the buffer while it transmits, and
    // the caller may free or reuse it the moment we return. Bounded by n * 10 / baud.
    rmt_tx_wait_all_done(_ch, -1);
    sent += n;
  }
  xSemaphoreGive(_lock);
  return sent;
}

int WcbSoftSerial::availableForWrite() {
  if (!_ch) return SoftwareSerial::availableForWrite();
  return 256;   // RMT TX never refuses: write() queues and waits
}

// Receive: the library's rxBits() race - see the header. Task context only (every soft-port reader
// is a task); none of these block, so the scheduler is held for microseconds.
int WcbSoftSerial::available() { vTaskSuspendAll(); const int n = SoftwareSerial::available(); xTaskResumeAll(); return n; }
int WcbSoftSerial::read()      { vTaskSuspendAll(); const int c = SoftwareSerial::read();      xTaskResumeAll(); return c; }
int WcbSoftSerial::peek()      { vTaskSuspendAll(); const int c = SoftwareSerial::peek();      xTaskResumeAll(); return c; }
