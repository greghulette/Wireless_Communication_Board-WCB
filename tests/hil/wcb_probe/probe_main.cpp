// WCB bench probe v2 — test-instrument firmware for a WCB V2.4 board (ESP32-PICO-V3-02).
//
// This is NOT WCB firmware. It turns a spare V2.4 board into a general bench instrument that the
// PC app (tests/hil) reconfigures at runtime, so a new test never needs new probe firmware:
//   - five serial channels on any S1-S5 header, any baud / format / inversion
//       A, B = hardware UART1 / UART2 (use these above 38400 baud)
//       C, D, E = EspSoftwareSerial (the same library the WCB uses for its S3-S5)
//   - capture the bytes a WCB puts on a port, timestamped, with framing / parity / overflow
//     errors; inject bytes into a WCB port
//   - reply rules: answer a device query on the probe itself, faster than a USB round trip
//     (a Maestro get-query must be answered within 25 ms)
//   - servo pulse measure and generate on several headers, pin levels, and edge counting
//     (how the app discovers which header is wired to which WCB port)
//   - mesh mode: join the ESP-NOW mesh as a WCB_Client with parameters sent at runtime
//
// Build/flash (classic ESP32; EspSoftwareSerial + WCB_Client come from the Arduino-Code sketchbook):
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
#include <SoftwareSerial.h>
#include <WCB_Client.h>

static const char *PROBE_VERSION = "2";

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
static void releasePin(int p) {
  if (p < 0 || p >= 40) return;
  pinOwner[p] = OWN_NONE;
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
  if (c.hw) c.hw->end();
  else c.sw->end();
  releasePin(c.rxPin);
  if (c.txPin >= 0) releasePin(c.txPin);
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
