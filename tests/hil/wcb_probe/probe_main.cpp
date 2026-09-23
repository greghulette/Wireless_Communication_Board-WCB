// WCB bench probe v2 — test-instrument firmware for a WCB V2.4 board (ESP32-PICO-V3-02).
//
// This is NOT WCB firmware. It turns a spare V2.4 board into a general bench instrument that the
// PC app (tests/hil) reconfigures at runtime, so a new test almost never needs new probe firmware
// (TXSKEW is the exception: sub-microsecond timing between two lines cannot come over USB):
//   - five serial channels on any S1-S5 header, any baud / format / inversion
//       A, B = hardware UART1 / UART2 (use these above 38400 baud)
//       C, D, E = EspSoftwareSerial (the WCB's patched copy, src/EspSoftwareSerial: level-triggered RX)
//   - capture the bytes a WCB puts on a port, timestamped, with framing / parity / overflow
//     errors; inject bytes into a WCB port
//   - reply rules: answer a device query on the probe itself, faster than a USB round trip
//     (a Maestro get-query must be answered within 25 ms)
//   - servo pulse measure and generate on several headers, pin levels, and edge counting
//     (how the app discovers which header is wired to which WCB port)
//   - mesh mode: join the ESP-NOW mesh as a WCB_Client with parameters sent at runtime
//
// Build/flash (classic ESP32; WCB_Client comes from the Arduino-Code sketchbook, EspSoftwareSerial is bundled in
// src/EspSoftwareSerial - a byte-identical copy of Code/WCB/src/EspSoftwareSerial, kept in lockstep by selftest.py):
//   arduino-cli compile --fqbn esp32:esp32:esp32 --build-path <dir> tests/hil/wcb_probe
//   arduino-cli upload  --fqbn esp32:esp32:esp32 --input-dir <dir> -p COMx
//
// Wiring: GND to the WCB's GND, WCB port TX to the probe header's RX, WCB RX to the probe TX.
// Leave 5V unconnected when both boards have their own supply. A straight-through cable works
// too — bind with SWAP. A listen-only tap wires only GND + the line being watched; bind RXONLY.
//
// USB protocol: 921600 8N1, one message per line. Verbs and KEY= names are case-insensitive;
// values keep their case (a mesh password is case-sensitive).
//   HELLO                                  -> HELLO wcb_probe <ver> mac=<mac> mesh=<id|0>
//   RESET                                  release every channel, pin, rule and PWM -> OK RESET
//   SCAN                                   -> SCAN S1 tx=<0|1|busy> rx=<..> ... (read against pull-down)
//   BIND <A-E> <S1-S5> <baud> [FMT=8N1] [INV] [SWAP] [RXONLY]
//   UNBIND <A-E>
//   TX <A-E> <hex>
//   TXSKEW <baud> <Sx>:<TX|RX> <hex> [<Sy>:<TX|RX> <skew_ns> <hex>] [<Sz>:<TX|RX> <skew_ns> <hex>]
//                                          bit-bang up to 3 8N1 lines at once onto pins held with LEVEL .. 1;
//                                          line y/z starts skew_ns after line x (negative: before), |skew| <= 1
//                                          bit, 1-40 bytes each, 1200-57600 baud, <= 60 ms -> OK TXSKEW n=<lines> span_us=<n>
//   RULE ADD <id 0-255> <A-E> <pattern hex, ?? = any byte> <reply hex> [DELAY=<ms>] [ONCE]
//   RULE DEL <id> | RULE CLEAR
//   PWMIN <S1-S5> [SWAP] | PWMIN OFF [<S1-S5>]
//   PWMOUT <S1-S5> <us> [HZ=<n>] [SWAP] | PWMOUT OFF [<S1-S5>]
//   EDGES START | EDGES READ | EDGES STOP  count transitions on every free header pin (pull-up)
//   LEVEL <S1-S5> <TX|RX> READ|0|1|Z
//   STAT | CLEAR
//   MESH JOIN ID=<n> OCT2=<hex> OCT3=<hex> QTY=<n> PASS=<pw> [CHAN=1] [CHK=1] [TEMP=1] [TYPE=HILProbe]
//   MESH LEAVE (reboots the probe) | MESH FORGET | MESH STATE
//   MSEND <wcb> <ensured 0|1> <hex text> | MBROADCAST <ensured 0|1> <hex text> | MRAW <wcb> <port> <hex>
// probe -> host
//   BOOT wcb_probe <ver> mac=<mac>
//   RX <A-E> <ms> <hex>                   one burst; <ms> = millis() when the loop first saw it
//   RXERR <A-E> <ms> <BREAK|FRAME|PARITY|FIFO_OVF|BUFFER_FULL> n=<count since last report>
//   RULEHIT <id> <ms>
//   PWM <Sx> <us> n=<pulses> | PWM <Sx> NONE      5 Hz per active PWMIN header
//   EDGES S1 tx=<n|busy|storm> rx=<..> ...
//   MRX <sender> <hex text> | MSTATUS <wcb> <0|1> | MSTATE id=<n> online=<ids> | MDROP n=<count>
//   OK <verb> ... | ERR <text>

#include <Arduino.h>
#include <atomic>
#include "src/EspSoftwareSerial/SoftwareSerial.h"   // the WCB's patched copy (tracker #78), not the stock library
#include <WCB_Client.h>
#include "driver/gpio.h"   // gpio_install_isr_service - soft-channel edge priority, see setup()
#include "esp_cpu.h"       // esp_cpu_get_cycle_count - TXSKEW timing
#include "esp_log.h"
#include "soc/gpio_struct.h"   // GPIO.out_w1ts / out_w1tc - TXSKEW writes both pins in one store

// 7: setup() disarms every header pin's interrupt before the ISR service goes in, MESH LEAVE unbinds everything before
// it restarts, and a released pin is left disarmed - v6 boot-looped on interrupt-WDT panics after a CPU-only reset left
// a soft-RX level arm with no handler (disarmPinIrq, tracker #78). 6: soft channels receive on level-triggered
// interrupts (the WCB's patched EspSoftwareSerial, tracker #78). 5: an unbound channel's TX line stays high
// (unbindChannel, tracker #79). 4: TXSKEW (tracker #78). 3: GPIO ISR service at level 3 (setup()). Otherwise the USB
// protocol is unchanged from 2.
static const char *PROBE_VERSION = "7";

// V2.4 header pins — must match wcb_hw_version 24 in Code/WCB/wcb_pin_map.cpp.
static const int8_t HDR_TX[6] = {-1, 8, 20, 25, 14, 13};
static const int8_t HDR_RX[6] = {-1, 21, 7, 4, 27, 26};
static const int STATUS_LED_PIN = 19;   // WS2812B-2020
static const int NUM_CH = 5;

// ---------------------------------------------------------------- pin ownership
enum Owner : uint8_t { OWN_NONE, OWN_CH, OWN_PWMIN, OWN_PWMOUT, OWN_EDGES, OWN_LEVEL };
static uint8_t pinOwner[40];

static bool pinFree(int p) { return p >= 0 && p < 40 && pinOwner[p] == OWN_NONE; }
static void claimPin(int p, Owner o) { if (p >= 0 && p < 40) pinOwner[p] = o; }

// Clear a pin's interrupt ENABLE and TYPE. A CPU-only reset (ESP.restart(), or a panic) keeps both in the GPIO
// registers, and a soft channel arms a LEVEL type (v6, tracker #78). Left armed with no handler, it re-asserts for as
// long as the line sits at the armed level: once gpio_install_isr_service() routes the GPIO interrupt, the IDF
// dispatcher finds no callback and re-enters until the interrupt watchdog panics - another CPU reset, so v6 looped
// 2-3 panics deep until the wired WCB booted and drove its TX line again. Arduino's pinMode would re-arm it on its
// own: __pinMode copies the pin's current int_type into gpio_config (esp32-hal-gpio.c), which enables it.
// Only for a pin with no handler attached (detachInterrupt already does this; calling it again is harmless).
static void disarmPinIrq(int p) {
  if (p < 0 || p >= 40) return;
  gpio_intr_disable((gpio_num_t)p);
  gpio_set_intr_type((gpio_num_t)p, GPIO_INTR_DISABLE);
}

static void releasePin(int p) {
  if (p < 0 || p >= 40) return;
  pinOwner[p] = OWN_NONE;
  disarmPinIrq(p);   // every owner detaches first; this just guarantees pinMode below can't re-arm a stale type
  pinMode(p, INPUT);
}

static int parseHeader(const String &s) {
  if (s.length() == 2 && (s[0] == 'S' || s[0] == 's') && s[1] >= '1' && s[1] <= '5') return s[1] - '0';
  return 0;
}

// ---------------------------------------------------------------- text helpers
static int hexVal(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  return -1;
}

static bool parseHex(const String &hex, uint8_t *out, size_t cap, size_t &n) {
  if (hex.length() % 2) return false;
  n = hex.length() / 2;
  if (n > cap) return false;
  for (size_t i = 0; i < n; i++) {
    int hi = hexVal(hex[2 * i]), lo = hexVal(hex[2 * i + 1]);
    if (hi < 0 || lo < 0) return false;
    out[i] = (uint8_t)(hi << 4 | lo);
  }
  return true;
}

static size_t appendHex(char *dst, const uint8_t *b, size_t n) {
  static const char H[] = "0123456789ABCDEF";
  for (size_t i = 0; i < n; i++) {
    dst[2 * i] = H[b[i] >> 4];
    dst[2 * i + 1] = H[b[i] & 0x0F];
  }
  return 2 * n;
}

static const int MAX_TOK = 16;
static int tokenize(const String &line, String tok[]) {
  int nt = 0, start = 0, len = line.length();
  while (nt < MAX_TOK && start < len) {
    int sp = line.indexOf(' ', start);
    if (sp < 0) sp = len;
    if (sp > start) tok[nt++] = line.substring(start, sp);
    start = sp + 1;
  }
  return nt;
}

// "KEY=value" among tok[from..nt) — returns the value (case preserved) or "".
static String keyValue(String tok[], int nt, int from, const char *key) {
  size_t kl = strlen(key);
  for (int i = from; i < nt; i++) {
    if (tok[i].length() > kl && tok[i][kl] == '=' && tok[i].substring(0, kl).equalsIgnoreCase(key))
      return tok[i].substring(kl + 1);
  }
  return String();
}

static bool hasFlag(String tok[], int nt, int from, const char *flag) {
  for (int i = from; i < nt; i++)
    if (tok[i].equalsIgnoreCase(flag)) return true;
  return false;
}

// ---------------------------------------------------------------- serial formats
struct Fmt {
  const char *name;
  uint32_t hw;
  EspSoftwareSerial::Config sw;
  uint8_t parity;   // 0 none, 1 even, 2 odd
};

static const Fmt FMTS[] = {
    {"8N1", SERIAL_8N1, SWSERIAL_8N1, 0}, {"8N2", SERIAL_8N2, SWSERIAL_8N2, 0},
    {"8E1", SERIAL_8E1, SWSERIAL_8E1, 1}, {"8E2", SERIAL_8E2, SWSERIAL_8E2, 1},
    {"8O1", SERIAL_8O1, SWSERIAL_8O1, 2}, {"8O2", SERIAL_8O2, SWSERIAL_8O2, 2},
    {"7N1", SERIAL_7N1, SWSERIAL_7N1, 0}, {"7N2", SERIAL_7N2, SWSERIAL_7N2, 0},
    {"7E1", SERIAL_7E1, SWSERIAL_7E1, 1}, {"7E2", SERIAL_7E2, SWSERIAL_7E2, 1},
    {"7O1", SERIAL_7O1, SWSERIAL_7O1, 2}, {"7O2", SERIAL_7O2, SWSERIAL_7O2, 2},
};

static const Fmt *findFmt(const String &s) {
  if (s.length() == 0) return &FMTS[0];
  for (const Fmt &f : FMTS)
    if (s.equalsIgnoreCase(f.name)) return &f;
  return nullptr;
}

// ---------------------------------------------------------------- channels
enum ErrKind { EK_BREAK, EK_FRAME, EK_PARITY, EK_FIFO_OVF, EK_BUFFER_FULL, EK_COUNT };
static const char *ERR_NAMES[EK_COUNT] = {"BREAK", "FRAME", "PARITY", "FIFO_OVF", "BUFFER_FULL"};

struct Channel {
  char name;
  HardwareSerial *hw;
  SoftwareSerial *sw;
  int header;
  int rxPin, txPin;
  bool rxOnly;
  uint32_t baud, gapUs;
  const Fmt *fmt;
  uint8_t buf[192];
  size_t len;
  uint32_t firstMs, lastByteUs, rxTotal;
  std::atomic<uint32_t> errCount[EK_COUNT];
  uint32_t errReported[EK_COUNT];
  uint8_t hist[32];
  uint8_t histLen;
  Stream *stream() { return hw ? static_cast<Stream *>(hw) : static_cast<Stream *>(sw); }
};

static SoftwareSerial swC, swD, swE;
static Channel chans[NUM_CH];

static Channel *channelByName(const String &s) {
  if (s.length() != 1) return nullptr;
  int i = toupper(s[0]) - 'A';
  return (i >= 0 && i < NUM_CH) ? &chans[i] : nullptr;
}

static void noteError(Channel &c, hardwareSerial_error_t e) {
  int k;
  switch (e) {
    case UART_BREAK_ERROR:       k = EK_BREAK; break;
    case UART_FRAME_ERROR:       k = EK_FRAME; break;
    case UART_PARITY_ERROR:      k = EK_PARITY; break;
    case UART_FIFO_OVF_ERROR:    k = EK_FIFO_OVF; break;
    case UART_BUFFER_FULL_ERROR: k = EK_BUFFER_FULL; break;
    default: return;
  }
  c.errCount[k]++;   // UART event task; printed from loop()
}

static uint32_t ledOffAtMs = 0;
static WCB_Client *mesh = nullptr;
static uint8_t meshId = 0;

static void ledIdle() {
  if (mesh) rgbLedWrite(STATUS_LED_PIN, 8, 0, 8);
  else rgbLedWrite(STATUS_LED_PIN, 0, 0, 10);
}

static void ledActivity() {
  if (!ledOffAtMs) rgbLedWrite(STATUS_LED_PIN, 0, 10, 0);
  ledOffAtMs = millis() + 60;
}

static void flushChannel(Channel &c) {
  if (!c.len) return;
  char line[24 + sizeof(c.buf) * 2];
  int n = snprintf(line, sizeof(line), "RX %c %lu ", c.name, (unsigned long)c.firstMs);
  n += appendHex(line + n, c.buf, c.len);
  line[n] = 0;
  Serial.println(line);
  c.len = 0;
}

static void unbindChannel(Channel &c) {
  if (!c.header) return;
  flushChannel(c);
  // A UART line idles HIGH, and the TX pin drives a WCB's RX. Re-binding a channel (every baud change) used to
  // pull that line low between end() and the next begin(): the WCB read a break, i.e. a NUL at the front of its
  // next line, which its parser then broadcast as an empty command and the real line was lost (tracker #79,
  // kyber.local_port_move_releases_old). Latch the pin's GPIO output high before end() detaches it from the UART,
  // and leave the released pin pulled up rather than floating.
  if (c.txPin >= 0) gpio_set_level((gpio_num_t)c.txPin, 1);
  if (c.hw) c.hw->end();
  else c.sw->end();
  releasePin(c.rxPin);
  if (c.txPin >= 0) {
    releasePin(c.txPin);
    pinMode(c.txPin, INPUT_PULLUP);
  }
  c.header = 0;
  c.rxPin = c.txPin = -1;
  c.rxOnly = false;
  c.histLen = 0;
}

static const char *bindChannel(Channel &c, int header, uint32_t baud, const Fmt *fmt, bool inv,
                               bool swap, bool rxOnly) {
  unbindChannel(c);
  int rx = swap ? HDR_TX[header] : HDR_RX[header];
  int tx = swap ? HDR_RX[header] : HDR_TX[header];
  // A hardware UART always gets its TX pin: -1 would keep the core's default UART pin, which
  // on some ESP32 modules is a flash pin. RXONLY on a hardware channel just never transmits.
  bool needTx = (c.hw != nullptr) || !rxOnly;
  if (!pinFree(rx) || (needTx && !pinFree(tx))) return "pin in use";
  if (c.hw) {
    c.hw->end();
    c.hw->setRxBufferSize(4096);
    c.hw->begin(baud, fmt->hw, rx, tx, inv);
  } else {
    c.sw->begin(baud, fmt->sw, rx, rxOnly ? -1 : tx, inv, 512);
    if (!(*c.sw)) {
      c.sw->end();
      return "software serial begin failed";
    }
  }
  claimPin(rx, OWN_CH);
  if (needTx) claimPin(tx, OWN_CH);
  c.header = header;
  c.rxPin = rx;
  c.txPin = needTx ? tx : -1;
  c.rxOnly = rxOnly;
  c.baud = baud;
  c.fmt = fmt;
  uint32_t charUs = 11UL * 1000000UL / baud;
  c.gapUs = max((uint32_t)4000, 4 * charUs);
  c.len = 0;
  c.histLen = 0;
  while (c.stream()->available()) c.stream()->read();
  return nullptr;
}

// ---------------------------------------------------------------- reply rules
struct Rule {
  bool used;
  uint8_t id, ch, plen, rlen;
  uint8_t pat[32], mask[32], reply[64];
  uint16_t delayMs;
  bool once;
};
static Rule rules[16];

struct PendingReply {
  bool used;
  uint8_t ch, len, id;
  uint8_t data[64];
  uint32_t dueMs;
};
static PendingReply pendingReplies[8];

static void writeReply(uint8_t chIdx, const uint8_t *d, uint8_t n) {
  Channel &c = chans[chIdx];
  if (!c.header || c.rxOnly) return;
  c.stream()->write(d, n);
}

static void checkRules(int chIdx) {
  Channel &c = chans[chIdx];
  for (Rule &r : rules) {
    if (!r.used || r.ch != chIdx || r.plen > c.histLen) continue;
    const uint8_t *tail = c.hist + c.histLen - r.plen;
    bool match = true;
    for (int i = 0; i < r.plen && match; i++) match = ((tail[i] & r.mask[i]) == (r.pat[i] & r.mask[i]));
    if (!match) continue;
    if (r.delayMs == 0) {
      writeReply(chIdx, r.reply, r.rlen);
    } else {
      for (PendingReply &p : pendingReplies) {
        if (p.used) continue;
        p.used = true;
        p.ch = chIdx;
        p.len = r.rlen;
        p.id = r.id;
        memcpy(p.data, r.reply, r.rlen);
        p.dueMs = millis() + r.delayMs;
        break;
      }
    }
    Serial.printf("RULEHIT %u %lu\n", r.id, (unsigned long)millis());
    if (r.once) r.used = false;
  }
}

static void servicePendingReplies() {
  uint32_t now = millis();
  for (PendingReply &p : pendingReplies) {
    if (p.used && (int32_t)(now - p.dueMs) >= 0) {
      writeReply(p.ch, p.data, p.len);
      p.used = false;
    }
  }
}

static void pushHistory(Channel &c, uint8_t b) {
  if (c.histLen == sizeof(c.hist)) {
    memmove(c.hist, c.hist + 1, sizeof(c.hist) - 1);
    c.histLen--;
  }
  c.hist[c.histLen++] = b;
}

static void pollChannel(int idx) {
  Channel &c = chans[idx];
  if (!c.header) return;
  Stream *s = c.stream();
  int avail = s->available();
  if (avail > 0) {
    uint32_t nowUs = micros();
    if (!c.len) c.firstMs = millis();
    while (avail-- > 0) {
      int b = s->read();
      if (b < 0) break;
      if (!c.hw && c.fmt->parity) {
        bool bit = c.sw->readParity();
        bool want = (c.fmt->parity == 1) ? SoftwareSerial::parityEven((uint8_t)b)
                                         : SoftwareSerial::parityOdd((uint8_t)b);
        if (bit != want) c.errCount[EK_PARITY]++;
      }
      c.buf[c.len++] = (uint8_t)b;
      c.rxTotal++;
      if (c.len == sizeof(c.buf)) {
        flushChannel(c);
        c.firstMs = millis();
      }
      pushHistory(c, (uint8_t)b);
      checkRules(idx);
    }
    c.lastByteUs = nowUs;
    ledActivity();
  } else if (c.len && (uint32_t)(micros() - c.lastByteUs) > c.gapUs) {
    flushChannel(c);
  }
  if (!c.hw && c.sw->overflow()) c.errCount[EK_BUFFER_FULL]++;
  for (int k = 0; k < EK_COUNT; k++) {
    uint32_t n = c.errCount[k].load();
    if (n != c.errReported[k]) {
      flushChannel(c);   // keep the error in stream order with the bytes around it
      Serial.printf("RXERR %c %lu %s n=%lu\n", c.name, (unsigned long)millis(), ERR_NAMES[k],
                    (unsigned long)(n - c.errReported[k]));
      c.errReported[k] = n;
    }
  }
}

// ---------------------------------------------------------------- PWM in / out
struct PwmIn {
  int pin;
  volatile uint32_t rise, width, pulses, lastMs;
  uint32_t reported;
};
static PwmIn pwmIn[6];   // indexed by header
static uint32_t pwmReportMs = 0;

static void IRAM_ATTR pwmIsr(void *arg) {
  PwmIn *s = static_cast<PwmIn *>(arg);
  uint32_t now = micros();
  if (digitalRead(s->pin)) {
    s->rise = now;
  } else if (s->rise) {
    s->width = now - s->rise;
    s->lastMs = millis();
    s->pulses = s->pulses + 1;
  }
}

static void pwmInOff(int h) {
  if (pwmIn[h].pin < 0) return;
  detachInterrupt(digitalPinToInterrupt(pwmIn[h].pin));
  releasePin(pwmIn[h].pin);
  pwmIn[h].pin = -1;
}

static void pollPwmIn() {
  if (millis() - pwmReportMs < 200) return;
  pwmReportMs = millis();
  for (int h = 1; h <= 5; h++) {
    PwmIn &s = pwmIn[h];
    if (s.pin < 0) continue;
    uint32_t pulses = s.pulses, width = s.width, last = s.lastMs;
    if (pulses != s.reported && millis() - last < 300)
      Serial.printf("PWM S%d %lu n=%lu\n", h, (unsigned long)width, (unsigned long)(pulses - s.reported));
    else
      Serial.printf("PWM S%d NONE\n", h);
    s.reported = pulses;
  }
}

struct PwmOut {
  int pin;
  uint32_t hz;
};
static PwmOut pwmOut[6];

static void pwmOutOff(int h) {
  if (pwmOut[h].pin < 0) return;
  ledcDetach(pwmOut[h].pin);
  releasePin(pwmOut[h].pin);
  pwmOut[h].pin = -1;
}

// ---------------------------------------------------------------- edges
static volatile uint32_t edgeCnt[40];
static uint32_t edgeSeen[40];
static bool edgeStorm[40];
static bool edgesOn = false;
static uint32_t edgeCheckMs = 0;

static void IRAM_ATTR edgeIsr(void *arg) {
  int p = (int)(intptr_t)arg;
  edgeCnt[p] = edgeCnt[p] + 1;
}

static void edgesStop() {
  for (int p = 0; p < 40; p++) {
    if (pinOwner[p] != OWN_EDGES) continue;
    if (!edgeStorm[p]) detachInterrupt(digitalPinToInterrupt(p));
    releasePin(p);
    edgeStorm[p] = false;
  }
  edgesOn = false;
}

static void edgesStart() {
  edgesStop();
  for (int h = 1; h <= 5; h++) {
    int pins[2] = {HDR_TX[h], HDR_RX[h]};
    for (int p : pins) {
      if (!pinFree(p)) continue;
      // Pull-UP: a UART line idles high, so a wired TX is driven high, a wired WCB RX already has
      // its own pull-up, and an unwired pin sits high. Nothing fights, nothing floats.
      pinMode(p, INPUT_PULLUP);
      edgeCnt[p] = 0;
      edgeSeen[p] = 0;
      edgeStorm[p] = false;
      claimPin(p, OWN_EDGES);
      attachInterruptArg(digitalPinToInterrupt(p), edgeIsr, (void *)(intptr_t)p, CHANGE);
    }
  }
  edgesOn = true;
}

static void edgesGuard() {
  // A pin sitting at an undefined level can interrupt continuously; cut it loose instead of
  // letting it starve the loop.
  if (!edgesOn || millis() - edgeCheckMs < 100) return;
  edgeCheckMs = millis();
  for (int p = 0; p < 40; p++) {
    if (pinOwner[p] != OWN_EDGES || edgeStorm[p]) continue;
    uint32_t n = edgeCnt[p];
    if (n - edgeSeen[p] > 50000) {
      detachInterrupt(digitalPinToInterrupt(p));
      edgeStorm[p] = true;
    }
    edgeSeen[p] = n;
  }
}

static String edgeField(int p) {
  if (pinOwner[p] != OWN_EDGES) return "busy";
  if (edgeStorm[p]) return "storm";
  uint32_t n = edgeCnt[p];
  edgeCnt[p] = 0;
  edgeSeen[p] = 0;
  return String(n);
}

// ---------------------------------------------------------------- mesh mode
struct MeshMsg {
  uint8_t kind;   // 0 command, 1 status
  uint8_t sender;
  uint16_t len;
  char text[240];
};
static QueueHandle_t meshQ = nullptr;
static std::atomic<uint32_t> meshDrops{0};
static uint32_t meshDropsReported = 0;
static char meshPass[40];
static char meshType[32];

static void onMeshCommand(uint8_t sender, const char *cmd) {   // WiFi task
  MeshMsg m;
  m.kind = 0;
  m.sender = sender;
  size_t n = strnlen(cmd, sizeof(m.text));
  memcpy(m.text, cmd, n);
  m.len = (uint16_t)n;
  if (xQueueSend(meshQ, &m, 0) != pdTRUE) meshDrops++;
}

static void onMeshStatus(uint8_t id, bool online) {   // WiFi / loop task
  MeshMsg m;
  m.kind = 1;
  m.sender = id;
  m.len = online ? 1 : 0;
  if (xQueueSend(meshQ, &m, 0) != pdTRUE) meshDrops++;
}

static void drainMesh() {
  MeshMsg m;
  while (xQueueReceive(meshQ, &m, 0) == pdTRUE) {
    if (m.kind == 0) {
      char line[16 + sizeof(m.text) * 2];
      int n = snprintf(line, sizeof(line), "MRX %u ", m.sender);
      n += appendHex(line + n, (const uint8_t *)m.text, m.len);
      line[n] = 0;
      Serial.println(line);
    } else {
      Serial.printf("MSTATUS %u %u\n", m.sender, m.len);
    }
  }
  uint32_t d = meshDrops.load();
  if (d != meshDropsReported) {
    Serial.printf("MDROP n=%lu\n", (unsigned long)(d - meshDropsReported));
    meshDropsReported = d;
  }
}

static bool decodeHexText(const String &hex, char *out, size_t cap) {
  size_t n;
  if (!parseHex(hex, (uint8_t *)out, cap - 1, n)) return false;
  out[n] = 0;
  return true;
}

// ---------------------------------------------------------------- TXSKEW (tracker #78)
// Asks whether a classic-ESP32 WCB loses soft-port input when two of its S3-S5 pins take an edge at
// nearly the same moment (erratum GPIO-3.14, see setup()). That needs one line's start placed a chosen
// fraction of a microsecond after another's, on two WCB inputs at once. The hardware channels cannot do
// it: a UART starts a frame on its own baud tick, not on the FIFO write, so the skew between A and B is
// whatever their phases happen to be. Instead the whole waveform is built as a list of level changes and
// played from IRAM with this core's interrupts masked, timed on the CPU cycle counter. Changes that fall
// on the same cycle go out in ONE store, so a zero skew is truly simultaneous (a rise and a fall on the
// same cycle are two stores, a few APB cycles apart). The pins must be LEVEL-held (OWN_LEVEL, idle high),
// so no bound channel can share one; LEVEL .. Z hands them back.
struct SkewEv {
  uint32_t at;         // CPU cycles after the start
  uint32_t set, clr;   // GPIO.out_w1ts / out_w1tc masks (pins 0-31)
};
struct SkewRaw {
  uint32_t at;
  bool high;
};
static const int SKEW_MAX_LINES = 3, SKEW_MAX_BYTES = 40, SKEW_MAX_MS = 60;
static SkewRaw skewRaw[SKEW_MAX_LINES][SKEW_MAX_BYTES * 10];   // an 8N1 byte changes level at most 10 times
static SkewEv skewEv[SKEW_MAX_LINES * SKEW_MAX_BYTES * 10];

// Masked for up to SKEW_MAX_MS, well inside the 300 ms interrupt watchdog. Nothing else may need this
// core meanwhile: TXSKEW is refused in mesh mode (WiFi, NVS writes) and while EDGES counts, and the host
// sends nothing until the OK. A probe channel still bound would lose what it receives in the window, so
// the test releases every wire on this probe first. noinline: with one caller GCC inlines it into
// txSkew, whose copy runs from flash, and a cache miss inside the loop would stall an edge.
static void IRAM_ATTR __attribute__((noinline)) playSkew(const SkewEv *ev, int n) {
  portDISABLE_INTERRUPTS();
  const uint32_t t0 = esp_cpu_get_cycle_count() + 2000;   // lead time, so the first change is not late
  for (int i = 0; i < n; i++) {
    while ((int32_t)(esp_cpu_get_cycle_count() - (t0 + ev[i].at)) < 0) {
    }
    if (ev[i].set) GPIO.out_w1ts = ev[i].set;
    if (ev[i].clr) GPIO.out_w1tc = ev[i].clr;
  }
  portENABLE_INTERRUPTS();
}

static void txSkew(String tok[], int nt) {
  static const char *USAGE =
      "ERR usage: TXSKEW <baud> <Sx>:<TX|RX> <hex> [<Sy>:<TX|RX> <skew_ns> <hex>] [<Sz>:<TX|RX> <skew_ns> <hex>]";
  const long baud = tok[1].toInt();
  if (baud < 1200 || baud > 57600 || (nt != 4 && nt != 7 && nt != 10)) { Serial.println(USAGE); return; }
  if (mesh) { Serial.println("ERR TXSKEW masks interrupts - not in mesh mode"); return; }
  if (edgesOn) { Serial.println("ERR EDGES active"); return; }
  const int nLines = (nt - 1) / 3;
  const uint32_t mhz = ESP.getCpuFreqMHz();
  const uint64_t cpuHz = (uint64_t)mhz * 1000000ULL;
  const long bitNs = 1000000000L / baud;
  static uint8_t data[SKEW_MAX_LINES][SKEW_MAX_BYTES];
  size_t len[SKEW_MAX_LINES];
  uint32_t mask[SKEW_MAX_LINES], allPins = 0;
  int32_t skewCyc[SKEW_MAX_LINES], minSkew = 0;
  for (int i = 0; i < nLines; i++) {
    // line 0: tok[2] pin, tok[3] hex; line i >= 1: tok[3i+1] pin, tok[3i+2] skew, tok[3i+3] hex
    const String &pinTok = tok[i ? 3 * i + 1 : 2];
    const String &hexTok = tok[i ? 3 * i + 3 : 3];
    const long skewNs = i ? tok[3 * i + 2].toInt() : 0;
    const int colon = pinTok.indexOf(':');
    const int h = colon > 0 ? parseHeader(pinTok.substring(0, colon)) : 0;
    String which = colon > 0 ? pinTok.substring(colon + 1) : String();
    which.toUpperCase();
    if (!h || (which != "TX" && which != "RX")) { Serial.println(USAGE); return; }
    const int pin = (which == "TX") ? HDR_TX[h] : HDR_RX[h];
    if (pin < 0 || pin >= 32) { Serial.printf("ERR S%d %s is GPIO%d - TXSKEW drives GPIO0-31 only\n", h, which.c_str(), pin); return; }
    if (pinOwner[pin] != OWN_LEVEL || !((GPIO.out >> pin) & 1)) {
      Serial.printf("ERR S%d %s is not held high - LEVEL S%d %s 1 first\n", h, which.c_str(), h, which.c_str());
      return;
    }
    if (allPins & (1UL << pin)) { Serial.println("ERR TXSKEW names a pin twice"); return; }
    if (!parseHex(hexTok, data[i], SKEW_MAX_BYTES, len[i]) || !len[i]) {
      Serial.printf("ERR TXSKEW takes 1-%d bytes of hex per line\n", SKEW_MAX_BYTES);
      return;
    }
    if ((skewNs < 0 ? -skewNs : skewNs) > bitNs) {
      Serial.printf("ERR TXSKEW skew must be within one bit (%ld ns at %ld baud)\n", bitNs, baud);
      return;
    }
    mask[i] = 1UL << pin;
    allPins |= mask[i];
    skewCyc[i] = (int32_t)((int64_t)skewNs * mhz / 1000);
    if (skewCyc[i] < minSkew) minSkew = skewCyc[i];
  }

  // Each line's level changes, on its own timeline shifted so the earliest line starts at 0. Bit k is
  // placed at k * cpuHz / baud from the line's start, so rounding never accumulates along the line.
  int rawN[SKEW_MAX_LINES] = {0};
  uint32_t spanCyc = 0;
  for (int i = 0; i < nLines; i++) {
    const uint32_t off = (uint32_t)(skewCyc[i] - minSkew);
    const uint32_t bits = len[i] * 10;   // 8N1: start, 8 data bits LSB first, stop
    bool level = true;                   // idle high
    for (uint32_t k = 0; k < bits; k++) {
      const uint8_t b = data[i][k / 10];
      const uint32_t j = k % 10;
      const bool bit = (j == 0) ? false : (j == 9) ? true : ((b >> (j - 1)) & 1);
      if (bit == level) continue;
      skewRaw[i][rawN[i]++] = {off + (uint32_t)(k * cpuHz / baud), bit};
      level = bit;
    }
    const uint32_t end = off + (uint32_t)(bits * cpuHz / baud);
    if (end > spanCyc) spanCyc = end;
  }
  if (spanCyc > (uint32_t)SKEW_MAX_MS * 1000UL * mhz) {
    Serial.printf("ERR TXSKEW lasts %lu us - %d ms max\n", (unsigned long)(spanCyc / mhz), SKEW_MAX_MS);
    return;
  }

  // Merge the lines into one time-ordered list; changes on the same cycle share an entry.
  int n = 0, head[SKEW_MAX_LINES] = {0};
  for (;;) {
    int best = -1;
    for (int i = 0; i < nLines; i++)
      if (head[i] < rawN[i] && (best < 0 || skewRaw[i][head[i]].at < skewRaw[best][head[best]].at)) best = i;
    if (best < 0) break;
    const SkewRaw &e = skewRaw[best][head[best]++];
    if (!n || skewEv[n - 1].at != e.at) skewEv[n++] = {e.at, 0, 0};
    if (e.high) skewEv[n - 1].set |= mask[best];
    else skewEv[n - 1].clr |= mask[best];
  }
  playSkew(skewEv, n);
  delayMicroseconds(bitNs / 1000 + 1);   // let the last stop bit finish before the host can send the next
  Serial.printf("OK TXSKEW n=%d span_us=%lu\n", nLines, (unsigned long)(spanCyc / mhz));
}

// ---------------------------------------------------------------- misc
static String macString() {
  uint64_t m = ESP.getEfuseMac();
  char s[18];
  snprintf(s, sizeof(s), "%02X:%02X:%02X:%02X:%02X:%02X", (uint8_t)m, (uint8_t)(m >> 8),
           (uint8_t)(m >> 16), (uint8_t)(m >> 24), (uint8_t)(m >> 32), (uint8_t)(m >> 40));
  return String(s);
}

static void resetAll() {
  for (Channel &c : chans) unbindChannel(c);
  for (int h = 1; h <= 5; h++) {
    pwmInOff(h);
    pwmOutOff(h);
  }
  edgesStop();
  for (Rule &r : rules) r.used = false;
  for (PendingReply &p : pendingReplies) p.used = false;
  for (int p = 0; p < 40; p++)
    if (pinOwner[p] == OWN_LEVEL) releasePin(p);
}

// ---------------------------------------------------------------- commands
static void handleLine(String line) {
  line.trim();
  if (!line.length()) return;
  String tok[MAX_TOK];
  int nt = tokenize(line, tok);
  String verb = tok[0];
  verb.toUpperCase();

  if (verb == "HELLO") {
    Serial.printf("HELLO wcb_probe %s mac=%s mesh=%u\n", PROBE_VERSION, macString().c_str(), meshId);

  } else if (verb == "RESET") {
    resetAll();
    Serial.println("OK RESET");

  } else if (verb == "SCAN") {
    String out = "SCAN";
    for (int h = 1; h <= 5; h++) {
      int pins[2] = {HDR_TX[h], HDR_RX[h]};
      String v[2];
      for (int i = 0; i < 2; i++) {
        if (!pinFree(pins[i])) { v[i] = "busy"; continue; }
        pinMode(pins[i], INPUT_PULLDOWN);
        delay(5);
        v[i] = String(digitalRead(pins[i]));
        pinMode(pins[i], INPUT);
      }
      out += " S" + String(h) + " tx=" + v[0] + " rx=" + v[1];
    }
    Serial.println(out);

  } else if (verb == "BIND") {
    Channel *c = channelByName(tok[1]);
    int h = parseHeader(tok[2]);
    long baud = tok[3].toInt();
    const Fmt *fmt = findFmt(keyValue(tok, nt, 4, "FMT"));
    if (!c || !h || baud < 50 || baud > 2000000) {
      Serial.println("ERR usage: BIND <A-E> <S1-S5> <baud> [FMT=8N1] [INV] [SWAP] [RXONLY]");
      return;
    }
    if (!fmt) { Serial.println("ERR unknown FMT"); return; }
    if (edgesOn) { Serial.println("ERR EDGES active"); return; }
    const char *err = bindChannel(*c, h, (uint32_t)baud, fmt, hasFlag(tok, nt, 4, "INV"),
                                  hasFlag(tok, nt, 4, "SWAP"), hasFlag(tok, nt, 4, "RXONLY"));
    if (err) { Serial.printf("ERR %s\n", err); return; }
    Serial.printf("OK BIND %c S%d %ld %s rx=%d tx=%d%s\n", c->name, h, baud, fmt->name, c->rxPin, c->txPin,
                  c->rxOnly ? " RXONLY" : "");

  } else if (verb == "UNBIND") {
    Channel *c = channelByName(tok[1]);
    if (!c) { Serial.println("ERR usage: UNBIND <A-E>"); return; }
    unbindChannel(*c);
    Serial.println("OK UNBIND");

  } else if (verb == "TX") {
    Channel *c = channelByName(tok[1]);
    if (!c || !c->header) { Serial.println("ERR channel not bound"); return; }
    if (c->rxOnly) { Serial.println("ERR channel is RXONLY"); return; }
    static uint8_t bytes[1024];
    size_t n;
    if (!parseHex(tok[2], bytes, sizeof(bytes), n)) { Serial.println("ERR bad hex"); return; }
    c->stream()->write(bytes, n);
    c->stream()->flush();
    Serial.printf("OK TX %c %u\n", c->name, (unsigned)n);

  } else if (verb == "TXSKEW") {
    txSkew(tok, nt);

  } else if (verb == "RULE") {
    String sub = tok[1];
    sub.toUpperCase();
    if (sub == "CLEAR") {
      for (Rule &r : rules) r.used = false;
      for (PendingReply &p : pendingReplies) p.used = false;
      Serial.println("OK RULE CLEAR");
    } else if (sub == "DEL") {
      int id = tok[2].toInt();
      for (Rule &r : rules)
        if (r.used && r.id == id) r.used = false;
      Serial.println("OK RULE DEL");
    } else if (sub == "ADD") {
      int id = tok[2].toInt();
      Channel *c = channelByName(tok[3]);
      String pat = tok[4], rep = tok[5];
      if (!c || id < 0 || id > 255 || pat.length() == 0 || pat.length() % 2 || rep.length() % 2) {
        Serial.println("ERR usage: RULE ADD <id> <A-E> <pattern hex, ?? is any byte> <reply hex> [DELAY=ms] [ONCE]");
        return;
      }
      Rule nr = {};
      nr.plen = pat.length() / 2;
      if (nr.plen > sizeof(nr.pat)) { Serial.println("ERR pattern too long (32 bytes max)"); return; }
      for (int i = 0; i < nr.plen; i++) {
        if (pat[2 * i] == '?' && pat[2 * i + 1] == '?') {
          nr.pat[i] = 0;
          nr.mask[i] = 0;
          continue;
        }
        int hi = hexVal(pat[2 * i]), lo = hexVal(pat[2 * i + 1]);
        if (hi < 0 || lo < 0) { Serial.println("ERR bad pattern hex"); return; }
        nr.pat[i] = (uint8_t)(hi << 4 | lo);
        nr.mask[i] = 0xFF;
      }
      size_t rl;
      if (!parseHex(rep, nr.reply, sizeof(nr.reply), rl)) { Serial.println("ERR bad reply hex"); return; }
      nr.rlen = (uint8_t)rl;
      nr.used = true;
      nr.id = (uint8_t)id;
      nr.ch = (uint8_t)(c - chans);
      nr.delayMs = (uint16_t)keyValue(tok, nt, 6, "DELAY").toInt();
      nr.once = hasFlag(tok, nt, 6, "ONCE");
      Rule *slot = nullptr;
      for (Rule &r : rules)
        if (r.used && r.id == id) slot = &r;   // same id replaces
      if (!slot)
        for (Rule &r : rules)
          if (!r.used) { slot = &r; break; }
      if (!slot) { Serial.println("ERR rule table full"); return; }
      *slot = nr;
      Serial.printf("OK RULE ADD %d\n", id);
    } else {
      Serial.println("ERR usage: RULE ADD|DEL|CLEAR");
    }

  } else if (verb == "PWMIN") {
    if (tok[1].equalsIgnoreCase("OFF")) {
      int h = parseHeader(tok[2]);
      if (h) pwmInOff(h);
      else
        for (int i = 1; i <= 5; i++) pwmInOff(i);
      Serial.println("OK PWMIN OFF");
      return;
    }
    int h = parseHeader(tok[1]);
    if (!h) { Serial.println("ERR usage: PWMIN <S1-S5> [SWAP] | PWMIN OFF [Sx]"); return; }
    if (edgesOn) { Serial.println("ERR EDGES active"); return; }
    pwmInOff(h);
    int pin = hasFlag(tok, nt, 2, "SWAP") ? HDR_TX[h] : HDR_RX[h];
    if (!pinFree(pin)) { Serial.println("ERR pin in use"); return; }
    PwmIn &s = pwmIn[h];
    pinMode(pin, INPUT_PULLDOWN);
    s.rise = 0;
    s.width = 0;
    s.pulses = 0;
    s.lastMs = 0;
    s.reported = 0;
    s.pin = pin;
    claimPin(pin, OWN_PWMIN);
    attachInterruptArg(digitalPinToInterrupt(pin), pwmIsr, &s, CHANGE);
    Serial.printf("OK PWMIN S%d pin=%d\n", h, pin);

  } else if (verb == "PWMOUT") {
    if (tok[1].equalsIgnoreCase("OFF")) {
      int h = parseHeader(tok[2]);
      if (h) pwmOutOff(h);
      else
        for (int i = 1; i <= 5; i++) pwmOutOff(i);
      Serial.println("OK PWMOUT OFF");
      return;
    }
    int h = parseHeader(tok[1]);
    long us = tok[2].toInt();
    String hzs = keyValue(tok, nt, 3, "HZ");
    long hz = hzs.length() ? hzs.toInt() : 50;
    if (!h || us < 0 || hz < 1 || hz > 2000 || us > 1000000L / hz) {
      Serial.println("ERR usage: PWMOUT <S1-S5> <us> [HZ=n] [SWAP] | PWMOUT OFF [Sx]");
      return;
    }
    if (edgesOn) { Serial.println("ERR EDGES active"); return; }
    int pin = hasFlag(tok, nt, 3, "SWAP") ? HDR_RX[h] : HDR_TX[h];
    if (pwmOut[h].pin != pin || pwmOut[h].hz != (uint32_t)hz) {
      pwmOutOff(h);
      if (!pinFree(pin)) { Serial.println("ERR pin in use"); return; }
      if (!ledcAttach(pin, hz, 16)) { Serial.println("ERR ledcAttach failed"); return; }
      pwmOut[h].pin = pin;
      pwmOut[h].hz = hz;
      claimPin(pin, OWN_PWMOUT);
    }
    uint64_t periodUs = 1000000ULL / (uint64_t)hz;
    ledcWrite(pin, (uint32_t)((uint64_t)us * 65536ULL / periodUs));
    Serial.printf("OK PWMOUT S%d %ld HZ=%ld pin=%d\n", h, us, hz, pin);

  } else if (verb == "EDGES") {
    String sub = tok[1];
    sub.toUpperCase();
    if (sub == "START") {
      edgesStart();
      Serial.println("OK EDGES START");
    } else if (sub == "STOP") {
      edgesStop();
      Serial.println("OK EDGES STOP");
    } else if (sub == "READ") {
      String out = "EDGES";
      for (int h = 1; h <= 5; h++)
        out += " S" + String(h) + " tx=" + edgeField(HDR_TX[h]) + " rx=" + edgeField(HDR_RX[h]);
      Serial.println(out);
    } else {
      Serial.println("ERR usage: EDGES START|READ|STOP");
    }

  } else if (verb == "LEVEL") {
    int h = parseHeader(tok[1]);
    String which = tok[2], val = tok[3];
    which.toUpperCase();
    val.toUpperCase();
    if (!h || (which != "TX" && which != "RX")) {
      Serial.println("ERR usage: LEVEL <S1-S5> <TX|RX> READ|0|1|Z");
      return;
    }
    int pin = (which == "TX") ? HDR_TX[h] : HDR_RX[h];
    bool mine = pinOwner[pin] == OWN_LEVEL;
    if (val == "READ") {
      if (!pinFree(pin) && !mine) { Serial.println("ERR pin in use"); return; }
      if (!mine) pinMode(pin, INPUT);
      Serial.printf("LEVEL S%d %s %d\n", h, which.c_str(), digitalRead(pin));
    } else if (val == "Z") {
      if (mine) releasePin(pin);
      Serial.println("OK LEVEL Z");
    } else if (val == "0" || val == "1") {
      if (!pinFree(pin) && !mine) { Serial.println("ERR pin in use"); return; }
      pinMode(pin, OUTPUT);
      digitalWrite(pin, val == "1" ? HIGH : LOW);
      claimPin(pin, OWN_LEVEL);
      Serial.printf("OK LEVEL S%d %s %s\n", h, which.c_str(), val.c_str());
    } else {
      Serial.println("ERR usage: LEVEL <S1-S5> <TX|RX> READ|0|1|Z");
    }

  } else if (verb == "STAT") {
    String out = "STAT";
    for (Channel &c : chans) {
      uint32_t e = 0;
      for (int k = 0; k < EK_COUNT; k++) e += c.errCount[k].load();
      out += " " + String(c.name) + " rx=" + String(c.rxTotal) + " err=" + String(e);
    }
    Serial.println(out);

  } else if (verb == "CLEAR") {
    for (Channel &c : chans) {
      c.rxTotal = 0;
      for (int k = 0; k < EK_COUNT; k++) {
        c.errCount[k] = 0;
        c.errReported[k] = 0;
      }
    }
    Serial.println("OK CLEAR");

  } else if (verb == "MESH") {
    String sub = tok[1];
    sub.toUpperCase();
    if (sub == "JOIN") {
      if (mesh) { Serial.println("ERR already joined — MESH LEAVE reboots the probe"); return; }
      long id = keyValue(tok, nt, 2, "ID").toInt();
      String o2 = keyValue(tok, nt, 2, "OCT2"), o3 = keyValue(tok, nt, 2, "OCT3");
      long qty = keyValue(tok, nt, 2, "QTY").toInt();
      String pass = keyValue(tok, nt, 2, "PASS");
      String chan = keyValue(tok, nt, 2, "CHAN"), chk = keyValue(tok, nt, 2, "CHK"),
             temp = keyValue(tok, nt, 2, "TEMP"), type = keyValue(tok, nt, 2, "TYPE");
      if (id < 1 || id > 20 || !o2.length() || !o3.length() || qty < 1 || !pass.length() ||
          pass.length() >= sizeof(meshPass)) {
        Serial.println("ERR usage: MESH JOIN ID=<1-20> OCT2=<hex> OCT3=<hex> QTY=<n> PASS=<pw> [CHAN=1] [CHK=1] [TEMP=1] [TYPE=..]");
        return;
      }
      strncpy(meshPass, pass.c_str(), sizeof(meshPass) - 1);
      strncpy(meshType, type.length() ? type.c_str() : "HILProbe", sizeof(meshType) - 1);
      mesh = new WCB_Client((uint8_t)strtoul(o2.c_str(), nullptr, 16), (uint8_t)strtoul(o3.c_str(), nullptr, 16),
                            meshPass, (uint8_t)qty, (uint8_t)id, onMeshCommand, onMeshStatus);
      mesh->setMeshChannel(chan.length() ? (uint8_t)chan.toInt() : 1);
      mesh->setChecksum(chk.length() ? chk.toInt() != 0 : true);
      mesh->setTemporary(temp.length() ? temp.toInt() != 0 : true);
      mesh->setIdentity(meshType, "wcb_probe-2");
      if (!mesh->begin()) {
        Serial.println("ERR WCB_Client begin failed — MESH LEAVE to reboot");
        return;
      }
      meshId = (uint8_t)id;
      ledIdle();
      Serial.printf("OK MESH JOIN ID=%u\n", meshId);
    } else if (sub == "LEAVE") {
      // Unbind and detach everything first: ESP.restart() is a CPU-only reset that keeps each pin's interrupt
      // type and enable, and a soft channel's level arm surviving it boot-looped v6 (disarmPinIrq). setup()
      // disarms the header pins as well; this keeps the deliberate reboot clean on its own.
      resetAll();
      Serial.println("OK MESH LEAVE rebooting");
      Serial.flush();
      delay(50);
      ESP.restart();
    } else if (sub == "FORGET") {
      if (!mesh) { Serial.println("ERR not joined"); return; }
      mesh->clearLearnedPeers();
      Serial.println("OK MESH FORGET");
    } else if (sub == "STATE") {
      String online;
      if (mesh)
        for (int i = 1; i <= 20; i++)
          if (mesh->isOnline(i)) online += (online.length() ? "," : "") + String(i);
      Serial.printf("MSTATE id=%u online=%s\n", meshId, online.length() ? online.c_str() : "-");
    } else {
      Serial.println("ERR usage: MESH JOIN|LEAVE|FORGET|STATE");
    }

  } else if (verb == "MSEND" || verb == "MBROADCAST" || verb == "MRAW") {
    if (!mesh) { Serial.println("ERR not joined"); return; }
    static char text[512];
    bool ok;
    if (verb == "MSEND") {
      if (!decodeHexText(tok[3], text, sizeof(text))) { Serial.println("ERR bad hex"); return; }
      ok = mesh->send((uint8_t)tok[1].toInt(), text, tok[2].toInt() != 0);
    } else if (verb == "MBROADCAST") {
      if (!decodeHexText(tok[2], text, sizeof(text))) { Serial.println("ERR bad hex"); return; }
      ok = mesh->broadcast(text, tok[1].toInt() != 0);
    } else {
      size_t n;
      if (!parseHex(tok[3], (uint8_t *)text, 177, n)) { Serial.println("ERR bad hex (177 bytes max)"); return; }
      ok = mesh->sendRaw((uint8_t)tok[1].toInt(), (uint8_t)tok[2].toInt(), (const uint8_t *)text, n);
    }
    Serial.printf("OK %s %d\n", verb.c_str(), ok ? 1 : 0);

  } else {
    Serial.printf("ERR unknown verb %s\n", verb.c_str());
  }
}

// ---------------------------------------------------------------- main
static String hostLine;

void setup() {
  Serial.setRxBufferSize(4096);
  Serial.setTxBufferSize(16384);
  Serial.begin(921600);

  // Soft channels C-E decode bits from the TIME each edge's GPIO interrupt starts, so a late edge
  // is a wrong bit. Level 3 lets edges pre-empt the level-1 UART and RMT (LED) interrupts, the same
  // as the WCB's own S3-S5. It did NOT cure the mis-decodes seen when two soft channels receive at
  // once: those were LOST edges, ESP32 erratum GPIO-3.14 (pins 0-31 share one interrupt status
  // register, and the dispatcher's W1TC clear for one pin's edge can swallow another pin's edge
  // arriving at that moment; all revisions, no fix). Since v6 the soft channels receive on
  // level-triggered, polarity-flipping interrupts (the WCB's patched library, src/EspSoftwareSerial),
  // which the erratum cannot lose (WCB tracker #78). TXSKEW (v4) turns the same question on a WCB: it starts lines on two
  // or three of the WCB's soft RX pins with a set sub-microsecond skew (tests/hil
  // softrx.erratum_pairs). NOT ESP_INTR_FLAG_IRAM: Arduino's __onPinInterrupt dispatcher is in
  // flash, and in mesh mode this probe writes NVS. Must run before any attachInterrupt (BIND).
  // v7: disarm every header pin FIRST - a CPU-only reset (MESH LEAVE, a panic) can leave a soft channel's level arm
  // behind with no handler, and the service below would route it straight into an interrupt-WDT boot loop
  // (disarmPinIrq). This must also precede the pinMode loop further down, which would otherwise re-enable it.
  for (int h = 1; h <= 5; h++) {
    disarmPinIrq(HDR_TX[h]);
    disarmPinIrq(HDR_RX[h]);
  }
  gpio_install_isr_service(ESP_INTR_FLAG_LEVEL3);
  // Arduino's first attachInterrupt then re-installs, gets INVALID_STATE (which it accepts) and the
  // IDF logs an ERROR line. Here that would land mid-protocol at the first BIND, not at boot, so the
  // gpio tag stays muted for good on this instrument.
  esp_log_level_set("gpio", ESP_LOG_NONE);

  memset(pinOwner, 0, sizeof(pinOwner));
  HardwareSerial *hws[2] = {&Serial1, &Serial2};
  SoftwareSerial *sws[3] = {&swC, &swD, &swE};
  for (int i = 0; i < NUM_CH; i++) {
    Channel &c = chans[i];
    c.name = 'A' + i;
    c.hw = i < 2 ? hws[i] : nullptr;
    c.sw = i < 2 ? nullptr : sws[i - 2];
    c.header = 0;
    c.rxPin = c.txPin = -1;
    c.rxOnly = false;
    c.fmt = &FMTS[0];
    c.len = 0;
    c.histLen = 0;
    c.rxTotal = 0;
    for (int k = 0; k < EK_COUNT; k++) {
      c.errCount[k] = 0;
      c.errReported[k] = 0;
    }
  }
  Serial1.onReceiveError([](hardwareSerial_error_t e) { noteError(chans[0], e); });
  Serial2.onReceiveError([](hardwareSerial_error_t e) { noteError(chans[1], e); });
  for (int h = 0; h < 6; h++) {
    pwmIn[h].pin = -1;
    pwmOut[h].pin = -1;
    pwmOut[h].hz = 50;
  }
  for (int h = 1; h <= 5; h++) {
    pinMode(HDR_TX[h], INPUT);
    pinMode(HDR_RX[h], INPUT);
  }
  meshQ = xQueueCreate(32, sizeof(MeshMsg));
  ledIdle();
  Serial.printf("BOOT wcb_probe %s mac=%s\n", PROBE_VERSION, macString().c_str());
}

void loop() {
  while (Serial.available()) {
    char ch = Serial.read();
    if (ch == '\n') {
      handleLine(hostLine);
      hostLine = "";
    } else if (ch != '\r' && hostLine.length() < 4096) {
      hostLine += ch;
    }
  }
  for (int i = 0; i < NUM_CH; i++) pollChannel(i);
  servicePendingReplies();
  pollPwmIn();
  edgesGuard();
  if (mesh) {
    mesh->update();
    drainMesh();
  }
  if (ledOffAtMs && (int32_t)(millis() - ledOffAtMs) >= 0) {
    ledIdle();
    ledOffAtMs = 0;
  }
}
