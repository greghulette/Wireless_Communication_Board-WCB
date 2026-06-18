#include "WCB_RemoteTerm.h"  // Must be first — redirects Serial → WCBDebugSerial
#include "WCB_HCR.h"
#include "WCB_Storage.h"
#include <Preferences.h>
#include <SoftwareSerial.h>
#include "src/HumanCyborgRelationsAPI/hcr.h"  // WCB-patched HCR, bundled in-sketch (no lib install)

// ---- Externs provided by WCB.ino / WCB_Storage.cpp ---------------------
extern int           WCB_Number;
extern bool          debugEnabled;
extern bool          debugHCR;     // toggled via ?DEBUG,HCR,ON|OFF or dhcron/dhcroff
extern char          LocalFunctionIdentifier;
extern char          commandDelimiter;
extern unsigned long baudRates[5];
extern bool          serialBroadcastEnabled[5];
extern bool          blockBroadcastFrom[5];
extern Preferences   preferences;

// updateBaudRate / saveBroadcastSettingsToPreferences / saveBroadcastBlockSettings
// / saveSerialLabelToPreferences are declared by WCB_Storage.h (included above).
extern Stream      &getSerialStream(int port);
extern void         applyLiveBaud(int port, uint32_t baud);

// Serial3-5 are EspSoftwareSerial instances defined in WCB.ino.
// Serial1/Serial2 are the ESP32 core HardwareSerial globals.
extern SoftwareSerial Serial3;
extern SoftwareSerial Serial4;
extern SoftwareSerial Serial5;

// ---------------------------------------------------------------------------
//  WcbHCR — thin subclass of HCRVocalizer.
//
//  We deliberately do NOT call HCRVocalizer::begin(): on ESP32 it re-opens
//  the UART with DEFAULT pins (clobbering the WCB's custom S1/S2 pin map) and
//  prints "Begin _serial" junk to the HCR. The WCB already owns/initialises
//  the port (applyLiveBaud), so we only need to (a) initialise the library's
//  protected state — the active constructors leave refreshSpeed/emote_*/etc.
//  indeterminate — and (b) set the poll refresh rate. Both are reachable here
//  because those members are `protected` in hcr.h.
// ---------------------------------------------------------------------------
class WcbHCR : public HCRVocalizer {
public:
  WcbHCR(HardwareSerial *s, int baud) : HCRVocalizer(s, baud)  { _init(); }
  WcbHCR(SoftwareSerial *s, int baud) : HCRVocalizer(s, baud)  { _init(); }

  // We do NOT use the library's built-in refreshSpeed auto-poll because it's
  // a uint16_t (ms) and overflows for any pollSec >= 66 s. processHCRTick()
  // schedules getUpdate() on its own millis() timer instead, which natively
  // supports the documented 3-3600 s range.

private:
  void _init() {
    refreshSpeed   = 0;
    emote_happy    = emote_sad = emote_mad = emote_scared = 0;
    state_override = state_musing = state_files = 0;
    state_duration = 0.0f;
    state_chv      = state_cha = state_chb = 0;
    Volume_V       = Volume_A = Volume_B = 0;
    cmdPos         = 0;
  }
};

// ---- Module globals -----------------------------------------------------
HCRConfig hcrConfig = {};

static WcbHCR  *_hcr     = nullptr;   // live instance (heap; rebuilt on port change)
static Stream  *_hcrPort = nullptr;   // concrete port stream — used by ;H,RAW
static unsigned long _hcrLastRxPollMs = 0;  // coarse bookkeeping for ?HCR,LIST
static unsigned long _hcrNextPollMs   = 0;  // next scheduled getUpdate() — own timer

// ---- Volume shadow + per-channel fade state -----------------------------
// HCR has no native fade; we synthesize a non-blocking ramp per channel,
// stepped from processHCRTick(). _hcrVol holds the last *settled* volume
// the WCB commanded for each channel (0=V,1=A,2=B) — used as the fade
// target / restore point because polled getVolume() can be up to pollSec
// stale. -1 = unknown (seed from poll, else default 100).
static int _hcrVol[3] = { -1, -1, -1 };

struct HcrFade {
  bool     active    = false;
  int      from      = 0;
  int      to        = 0;
  uint32_t startMs   = 0;
  uint32_t durMs     = 0;
  uint32_t nextStep  = 0;
  int      lastSent  = -1;
  bool     stopAtEnd = false;   // fade-out: StopWAV + restore when done
  int      restoreTo = 0;
};
static HcrFade _fade[3];                       // by channel (0=V,1=A,2=B)
static const uint16_t HCR_FADE_STEP_MS = 150;  // ramp granularity

// Set a channel volume AND record it as the settled shadow (user intent).
static void hcrSetVol(int ch, int v) {
  if (ch < 0 || ch > 2 || !_hcr) return;
  v = constrain(v, 0, 100);
  _hcr->SetVolume(ch, v);
  _hcrVol[ch] = v;
}

// Best-effort "current" volume: settled shadow, else last poll, else 100.
static int hcrCurVol(int ch) {
  if (ch >= 0 && ch <= 2 && _hcrVol[ch] >= 0) return _hcrVol[ch];
  if (_hcr) { int v = (int)(_hcr->getVolume(ch) + 0.5f);
              if (v > 0) return constrain(v, 0, 100); }
  return 100;
}

static void hcrCancelFade(int ch) {
  if (ch >= 0 && ch <= 2) _fade[ch].active = false;
}

// Begin a ramp on ch from->to over durSec. stopAtEnd: on reaching the end,
// StopWAV(ch) then restore volume to restoreTo (fade-out semantics).
static void hcrStartFade(int ch, int from, int to, int durSec,
                         bool stopAtEnd, int restoreTo) {
  if (ch < 0 || ch > 2 || !_hcr) return;
  from = constrain(from, 0, 100);
  to   = constrain(to,   0, 100);
  HcrFade &f = _fade[ch];
  f.active = false;                       // supersede any in-flight fade
  if (durSec <= 0) {                      // instant — one write, no anchor
    _hcr->SetVolume(ch, to);
    if (stopAtEnd) { _hcr->StopWAV(ch); hcrSetVol(ch, restoreTo); }
    else           { _hcrVol[ch] = to; }
    return;
  }
  // Anchor the start point only if it differs from current — avoids an
  // extra packet when from already matches what's on the wire.
  if (from != hcrCurVol(ch)) _hcr->SetVolume(ch, from);
  f.from = from; f.to = to;
  f.startMs = millis(); f.durMs = (uint32_t)durSec * 1000UL;
  f.nextStep = 0; f.lastSent = from;
  f.stopAtEnd = stopAtEnd; f.restoreTo = restoreTo;
  f.active = true;
  if (debugHCR) Serial.printf("[HCR-DBG] fade start ch=%d %d->%d %ds%s\n",
                              ch, from, to, durSec,
                              stopAtEnd ? " (stop+restore)" : "");
}

// Step all active fades — called every loop tick (non-blocking).
static void hcrStepFades() {
  if (!_hcr) return;
  uint32_t now = millis();
  for (int ch = 0; ch < 3; ch++) {
    HcrFade &f = _fade[ch];
    if (!f.active) continue;
    uint32_t elapsed = now - f.startMs;
    if (elapsed >= f.durMs) {                 // done
      _hcr->SetVolume(ch, f.to);
      if (f.stopAtEnd) {
        _hcr->StopWAV(ch);
        _hcr->SetVolume(ch, f.restoreTo);
        _hcrVol[ch] = f.restoreTo;
      } else {
        _hcrVol[ch] = f.to;
      }
      f.active = false;
      if (debugHCR) Serial.printf("[HCR-DBG] fade done ch=%d -> %d%s\n",
                                  ch, f.stopAtEnd ? f.restoreTo : f.to,
                                  f.stopAtEnd ? " (stopped+restored)" : "");
      continue;
    }
    if (now < f.nextStep) continue;
    f.nextStep = now + HCR_FADE_STEP_MS;
    int v = f.from + (int)((long)(f.to - f.from) * (long)elapsed / (long)f.durMs);
    v = constrain(v, 0, 100);
    if (v != f.lastSent) { _hcr->SetVolume(ch, v); f.lastSent = v; }
  }
}

// ==================== Port-conflict Query ================================

bool isSerialPortUsedForHCR(int port) {
  return hcrConfig.configured && hcrConfig.serialPort == (uint8_t)port;
}

// ==================== Lifecycle =========================================

void beginHCR() {
  // Tear down any previous instance.
  if (_hcr) { delete _hcr; _hcr = nullptr; }
  _hcrPort = nullptr;

  if (!hcrConfig.configured ||
      hcrConfig.serialPort < 1 || hcrConfig.serialPort > 5) {
    return;
  }

  int baud = (int)hcrConfig.baudRate;
  switch (hcrConfig.serialPort) {
    case 1: _hcr = new WcbHCR(&Serial1, baud); _hcrPort = &Serial1; break;
    case 2: _hcr = new WcbHCR(&Serial2, baud); _hcrPort = &Serial2; break;
    case 3: _hcr = new WcbHCR(&Serial3, baud); _hcrPort = &Serial3; break;
    case 4: _hcr = new WcbHCR(&Serial4, baud); _hcrPort = &Serial4; break;
    case 5: _hcr = new WcbHCR(&Serial5, baud); _hcrPort = &Serial5; break;
    default: return;
  }
  // Auto-poll is driven by processHCRTick() on its own millis() timer
  // (handles full 3-3600 s range, unlike library's uint16_t refreshSpeed).
  // pollSec 0 = off (no auto <QD>). update() still parses incoming RX.
  _hcrNextPollMs = 0;  // first tick will fire poll immediately if pollSec>0
  Serial.printf("[HCR] Active on S%d at %lu baud, poll=%us\n",
                hcrConfig.serialPort,
                (unsigned long)hcrConfig.baudRate,
                (unsigned)hcrConfig.pollSec);
}

// Call once per loop() tick. Non-blocking: HCRVocalizer::update() parses one
// RX byte per call. We schedule getUpdate() ourselves on a millis() timer so
// pollSec can be the full documented 3-3600s range (library's refreshSpeed
// is uint16_t and would overflow at >=66s).
void processHCRTick() {
  if (!_hcr) return;
  _hcr->update();
  hcrStepFades();                 // non-blocking volume ramps

  // Own-timer auto-poll. pollSec==0 -> off; user can still ?HCR,REFRESH.
  unsigned long now = millis();
  if (hcrConfig.pollSec > 0 && (long)(now - _hcrNextPollMs) >= 0) {
    _hcr->getUpdate();
    _hcrLastRxPollMs = now;
    _hcrNextPollMs   = now + (unsigned long)hcrConfig.pollSec * 1000UL;
  }

  // When HCR debug is on, periodically dump the parsed status so the user
  // can watch what's coming back from the HCR (the library hides per-byte
  // RX). Throttled to the poll interval (min 2s) to avoid flooding.
  if (debugHCR) {
    static unsigned long _dbgNext = 0;
    if (now >= _dbgNext) {
      uint32_t everyMs = (hcrConfig.pollSec > 0 ? hcrConfig.pollSec : 2) * 1000UL;
      if (everyMs < 2000UL) everyMs = 2000UL;
      _dbgNext = now + everyMs;
      Serial.print("[HCR-DBG] status ");
      printHCRStatus();
    }
  }
}

// ==================== Argument parsing helpers ==========================

// Split a comma list, return field idx (0-based), trimmed. "" if absent.
static String hcrField(const String &s, int idx) {
  int start = 0;
  for (int i = 0; i < idx; i++) {
    int c = s.indexOf(',', start);
    if (c < 0) return "";
    start = c + 1;
  }
  int end = s.indexOf(',', start);
  String v = (end < 0) ? s.substring(start) : s.substring(start, end);
  v.trim();
  return v;
}

// Emotion: H/S/M/C or HAPPY/SAD/MAD/SCARED -> 0..3, OVERLOAD -> 4, else -1
static int hcrEmotion(const String &t) {
  String u = t; u.trim(); u.toUpperCase();
  if (u == "H" || u == "HAPPY")    return HAPPY;
  if (u == "S" || u == "SAD")      return SAD;
  if (u == "M" || u == "MAD")      return MAD;
  if (u == "C" || u == "SCARED")   return SCARED;
  if (u == "O" || u == "OVERLOAD") return OVERLOAD;
  return -1;
}

// Level: MOD/MODERATE/0 -> 0, STRONG/1 -> 1, else -1
static int hcrLevel(const String &t) {
  String u = t; u.trim(); u.toUpperCase();
  if (u == "MOD" || u == "MODERATE" || u == "0") return EMOTE_MODERATE;
  if (u == "STRONG" || u == "1")                 return EMOTE_STRONG;
  return -1;
}

// Audio channel: V/A/B -> 0/1/2, else -1
static int hcrChan(const String &t) {
  String u = t; u.trim(); u.toUpperCase();
  if (u == "V") return CH_V;
  if (u == "A") return CH_A;
  if (u == "B") return CH_B;
  return -1;
}

// ==================== Runtime dispatch (;H,...) =========================

void processHCRRuntimeCommand(const String &message) {
  // message arrives as "H,<verb>,<args...>" (leading 'H' from ;H routing).
  if (!_hcr || !hcrConfig.configured) {
    Serial.println("[HCR] Not configured — use ?HCR,PORT,Sx:baud first");
    return;
  }

  // Strip the leading "H" / "H,"
  String body = message;
  body.trim();
  if (body.length() >= 1 && (body[0] == 'H' || body[0] == 'h')) {
    body = (body.length() >= 2 && body[1] == ',') ? body.substring(2)
                                                  : body.substring(1);
  }
  body.trim();
  if (body.length() == 0) { Serial.println("[HCR] Empty ;H command"); return; }

  String verb = hcrField(body, 0);
  String vU   = verb; vU.toUpperCase();

  if (debugHCR) Serial.printf("[HCR-DBG] cmd in: ;H,%s\n", body.c_str());

  // ---- RAW: send the remainder verbatim (may contain commas) ----------
  if (vU == "RAW") {
    int firstComma = body.indexOf(',');
    String payload = (firstComma < 0) ? "" : body.substring(firstComma + 1);
    if (payload.length() == 0) { Serial.println("[HCR] RAW needs a payload"); return; }
    if (_hcrPort) {
      _hcrPort->print(payload);
      if (!payload.endsWith("\n")) _hcrPort->print('\n');
    }
    if (debugHCR || debugEnabled) Serial.printf("[HCR-DBG] TX raw -> %s\n", payload.c_str());
    return;
  }

  // ---- FN,fn,chan,track : RC-Controller numeric convention ------------
  if (vU == "FN") {
    int fn    = hcrField(body, 1).toInt();
    int chan  = hcrField(body, 2).toInt();
    int track = hcrField(body, 3).toInt();
    switch (fn) {
      case 2:  _hcr->SetEmotion(chan, track);   _hcr->update(); break;
      case 3:  _hcr->Trigger(chan, track);                      break;
      case 4:  _hcr->Stimulate(chan, track);                    break;
      case 5:  _hcr->Overload();                                break;
      case 6:  _hcr->Muse();                                    break;
      case 8:  _hcr->Stop();                                    break;
      case 9:  _hcr->StopEmote();                               break;
      case 11: _hcr->ResetEmotions();                           break;
      case 14: _hcr->PlayWAV(chan, track);      _hcr->update(); break;
      case 16: _hcr->StopWAV(chan);                             break;
      case 17: _hcr->SetVolume(chan, track);                    break;
      default: Serial.printf("[HCR] Unknown fn=%d\n", fn);      return;
    }
    if (debugHCR || debugEnabled) Serial.printf("[HCR-DBG] FN %d,%d,%d\n", fn, chan, track);
    return;
  }

  // ---- Readable verbs --------------------------------------------------
  if (vU == "STIM" || vU == "STIMULATE" || vU == "TRIGGER") {
    int e = hcrEmotion(hcrField(body, 1));
    // OVERLOAD is its own top-level verb; don't silently rewrite it here.
    if (e == OVERLOAD) {
      Serial.printf("[HCR] %s does not take OVERLOAD — use ;H,OVERLOAD\n", vU.c_str());
      return;
    }
    int lv = hcrLevel(hcrField(body, 2));
    if (e < 0 || lv < 0) { Serial.println("[HCR] Usage: ;H,STIM,<H|S|M|C>,<MOD|STRONG>"); return; }
    if (vU == "TRIGGER") _hcr->Trigger(e, lv);
    else                 _hcr->Stimulate(e, lv);
    return;
  }
  if (vU == "OVERLOAD")      { _hcr->Overload();      return; }
  if (vU == "STOPEMOTE")     { _hcr->StopEmote();     return; }
  if (vU == "RESETEMOTIONS") { _hcr->ResetEmotions(); return; }
  if (vU == "STOP")          { _hcr->Stop();          return; }

  if (vU == "SETEMOTION") {
    int e = hcrEmotion(hcrField(body, 1));
    int v = hcrField(body, 2).toInt();
    if (e < 0 || e > SCARED || v < 0 || v > 100) {
      Serial.println("[HCR] Usage: ;H,SETEMOTION,<H|S|M|C>,<0-100>"); return;
    }
    _hcr->SetEmotion(e, v); _hcr->update();
    return;
  }

  if (vU == "OVERRIDE") {
    String a = hcrField(body, 1); a.toUpperCase();
    _hcr->OverrideEmotions((a == "1" || a == "ON") ? 1 : 0);
    return;
  }

  if (vU == "MUSE") {
    String a = hcrField(body, 1); a.toUpperCase();
    if (a == "")        { _hcr->Muse(); return; }                  // single muse
    if (a == "GAP") {
      int mn = hcrField(body, 2).toInt();
      int mx = hcrField(body, 3).toInt();
      _hcr->Muse(mn, mx);
      return;
    }
    if (a == "TOGGLE")  { _hcr->SetMuse(_hcr->GetMuse() ? 0 : 1); return; }
    _hcr->SetMuse((a == "1" || a == "ON") ? 1 : 0);
    return;
  }

  if (vU == "PLAY") {
    int ch = hcrChan(hcrField(body, 1));            // A|B
    int fl = hcrField(body, 2).toInt();
    if ((ch != CH_A && ch != CH_B) || fl < 0 || fl > 9999) {
      Serial.println("[HCR] Usage: ;H,PLAY,<A|B>,<0-9999>[,FADEIN,<sec>]"); return;
    }
    String opt = hcrField(body, 3); opt.toUpperCase();
    if (opt == "FADEIN") {
      int sec    = hcrField(body, 4).toInt();
      int target = hcrCurVol(ch);                   // capture intended level
      hcrCancelFade(ch);
      _hcr->SetVolume(ch, 0);                       // silent BEFORE play (no blip)
      _hcr->PlayWAV(ch, fl); _hcr->update();
      hcrStartFade(ch, 0, target, sec, false, 0);   // 0 -> target
    } else {
      hcrCancelFade(ch);
      _hcr->PlayWAV(ch, fl); _hcr->update();
    }
    return;
  }
  if (vU == "STOPWAV") {
    int ch = hcrChan(hcrField(body, 1));
    if (ch != CH_A && ch != CH_B) { Serial.println("[HCR] Usage: ;H,STOPWAV,<A|B>"); return; }
    hcrCancelFade(ch);
    _hcr->StopWAV(ch);
    return;
  }
  if (vU == "VOL" || vU == "VOLUME") {
    int ch = hcrChan(hcrField(body, 1));            // V|A|B
    int v  = hcrField(body, 2).toInt();
    if (ch < 0 || v < 0 || v > 100) { Serial.println("[HCR] Usage: ;H,VOL,<V|A|B>,<0-100>"); return; }
    hcrCancelFade(ch);
    hcrSetVol(ch, v);
    return;
  }
  if (vU == "VOLUP" || vU == "VOLDN" || vU == "VOLDOWN") {
    // ;H,VOLUP|VOLDN[,<V|A|B>][,<step>]
    // Channel is OPTIONAL: omit it (or pass just the step) to bump ALL channels
    // (V, A and B) by the step. Step default 5.
    String f1   = hcrField(body, 1);
    int    ch   = hcrChan(f1);                       // V|A|B -> index, -1 if not a channel
    bool   all  = (ch < 0);                          // no/invalid channel -> all channels
    // Step is field 2 when a channel was named; otherwise field 1 (the step) or default.
    String sStr = all ? (f1.length() ? f1 : hcrField(body, 2)) : hcrField(body, 2);
    int    step = sStr.length() ? sStr.toInt() : 5;
    if (step <= 0) step = 5;
    const bool up = (vU == "VOLUP");
    const int  chans[3] = { CH_V, CH_A, CH_B };
    const int  n = all ? 3 : 1;
    for (int i = 0; i < n; i++) {
      const int c   = all ? chans[i] : ch;
      const int cur = hcrCurVol(c);
      const int nv  = up ? cur + step : cur - step;
      hcrCancelFade(c);
      hcrSetVol(c, nv);                              // hcrSetVol clamps 0-100
      if (debugHCR) Serial.printf("[HCR-DBG] %s ch=%d %d->%d\n",
                                  vU.c_str(), c, cur, constrain(nv, 0, 100));
    }
    return;
  }
  if (vU == "FADEOUT") {
    int ch  = hcrChan(hcrField(body, 1));           // A|B
    int sec = hcrField(body, 2).toInt();
    if (ch != CH_A && ch != CH_B) { Serial.println("[HCR] Usage: ;H,FADEOUT,<A|B>,<sec>"); return; }
    int cur = hcrCurVol(ch);
    hcrStartFade(ch, cur, 0, sec, true, cur);       // ramp->0, StopWAV, restore cur
    return;
  }
  if (vU == "FADEIN") {
    int ch  = hcrChan(hcrField(body, 1));           // A|B
    int sec = hcrField(body, 2).toInt();
    if (ch != CH_A && ch != CH_B) { Serial.println("[HCR] Usage: ;H,FADEIN,<A|B>,<sec>"); return; }
    int target = hcrCurVol(ch);
    hcrStartFade(ch, 0, target, sec, false, 0);     // 0 -> captured target
    return;
  }

  Serial.printf("[HCR] Unknown ;H command: %s\n", verb.c_str());
}

// ==================== Configuration (?HCR,...) ==========================

void clearHCRConfig() {
  uint8_t freedPort = hcrConfig.serialPort;

  if (_hcr) { delete _hcr; _hcr = nullptr; }
  _hcrPort = nullptr;

  memset(&hcrConfig, 0, sizeof(hcrConfig));
  hcrConfig.configured = false;
  hcrConfig.baudRate   = 9600;
  hcrConfig.pollSec    = 10;

  saveHCRSettings();
  Serial.println("[HCR] Configuration cleared");

  if (freedPort > 0) {
    if (!serialBroadcastEnabled[freedPort - 1]) {
      serialBroadcastEnabled[freedPort - 1] = true;
      saveBroadcastSettingsToPreferences();
      Serial.printf("  ✓ Re-enabled broadcast output on S%d\n", freedPort);
    }
    if (blockBroadcastFrom[freedPort - 1]) {
      blockBroadcastFrom[freedPort - 1] = false;
      saveBroadcastBlockSettings();
      Serial.printf("  ✓ Re-enabled broadcast input on S%d\n", freedPort);
    }
    updateBaudRate(freedPort, 9600);
    saveSerialLabelToPreferences(freedPort, "");
    Serial.printf("  ✓ Released S%d (old HCR port)\n", freedPort);
  }
}

static void hcrReservePort(int serialPort, int baudRate) {
  // Release old port if moving.
  if (hcrConfig.configured && hcrConfig.serialPort > 0 &&
      hcrConfig.serialPort != (uint8_t)serialPort) {
    uint8_t oldPort = hcrConfig.serialPort;
    if (!serialBroadcastEnabled[oldPort - 1]) {
      serialBroadcastEnabled[oldPort - 1] = true;
      saveBroadcastSettingsToPreferences();
    }
    if (blockBroadcastFrom[oldPort - 1]) {
      blockBroadcastFrom[oldPort - 1] = false;
      saveBroadcastBlockSettings();
    }
    saveSerialLabelToPreferences(oldPort, "");
    Serial.printf("  ✓ Released S%d (old HCR port)\n", oldPort);
  }

  // Apply baud live (handles S1/S2 divisor + S3-5 software re-init).
  if ((uint32_t)baudRate != baudRates[serialPort - 1]) {
    updateBaudRate(serialPort, baudRate);
  } else {
    applyLiveBaud(serialPort, (uint32_t)baudRate);
  }

  // Reserve the port: HCRVocalizer owns RX, so disable broadcast both ways.
  if (serialBroadcastEnabled[serialPort - 1]) {
    serialBroadcastEnabled[serialPort - 1] = false;
    saveBroadcastSettingsToPreferences();
    Serial.printf("  ⚠️  Disabled broadcast output on S%d (HCR port)\n", serialPort);
  }
  if (!blockBroadcastFrom[serialPort - 1]) {
    blockBroadcastFrom[serialPort - 1] = true;
    saveBroadcastBlockSettings();
    Serial.printf("  ⚠️  Disabled broadcast input on S%d (HCR port)\n", serialPort);
  }
  saveSerialLabelToPreferences(serialPort, "HCR");

  hcrConfig.serialPort = (uint8_t)serialPort;
  hcrConfig.baudRate   = (uint32_t)baudRate;
  hcrConfig.configured = true;
  // pollSec is preserved from NVS/POLL command (default 10, 0 = user set OFF).

  saveHCRSettings();
  beginHCR();
  Serial.printf("[HCR] Configured on S%d at %d baud\n", serialPort, baudRate);
}

void configureHCR(const String &args) {
  String a = args; a.trim();
  String aU = a;   aU.toUpperCase();

  if (aU == "LIST")            { printHCRSettings(); return; }
  if (aU == "CLEAR")           { clearHCRConfig();   return; }
  if (aU == "STATUS")          { printHCRStatus();   return; }
  if (aU == "REFRESH")         {
    if (_hcr) { _hcr->getUpdate(); Serial.println("[HCR] Refresh requested"); }
    else      Serial.println("[HCR] Not configured");
    return;
  }

  // ---- POLL,<sec|OFF> -------------------------------------------------
  if (aU.startsWith("POLL")) {
    String v = (a.indexOf(',') >= 0) ? a.substring(a.indexOf(',') + 1) : "";
    v.trim(); String vU = v; vU.toUpperCase();
    if (vU == "OFF" || vU == "0") hcrConfig.pollSec = 0;
    else {
      int s = v.toInt();
      if (s < 3 || s > 3600) { Serial.println("[HCR] POLL must be 3-3600 s, or OFF (min 3s keeps any serial port safe)"); return; }
      hcrConfig.pollSec = (uint16_t)s;
    }
    saveHCRSettings();
    // Reschedule next poll immediately so the new interval takes effect now.
    _hcrNextPollMs = millis();
    Serial.printf("[HCR] Poll interval = %us%s\n", (unsigned)hcrConfig.pollSec,
                  hcrConfig.pollSec ? "" : " (off)");
    return;
  }

  // ---- GET,<field> ----------------------------------------------------
  if (aU.startsWith("GET,")) {
    if (!_hcr) { Serial.println("[HCR] Not configured"); return; }
    String f = a.substring(4); f.trim(); String fU = f; fU.toUpperCase();
    String key = hcrField(fU, 0);
    if      (key == "EMOTION") { int e = hcrEmotion(hcrField(fU,1));
                                 Serial.printf("[HCR] EMOTION %s = %d\n", f.c_str(),
                                               e >= 0 ? _hcr->GetEmotion(e) : -1); }
    else if (key == "DURATION") Serial.printf("[HCR] DURATION = %.2f\n", _hcr->GetDuration());
    else if (key == "OVERRIDE") Serial.printf("[HCR] OVERRIDE = %d\n", _hcr->GetOverride());
    else if (key == "MUSE")     Serial.printf("[HCR] MUSE = %d\n", _hcr->GetMuse());
    else if (key == "WAVCOUNT") Serial.printf("[HCR] WAVCOUNT = %d\n", _hcr->GetWAVCount());
    else if (key == "PLAYING")  { int c = hcrChan(hcrField(fU,1));
                                  Serial.printf("[HCR] PLAYING %s = %d\n", f.c_str(),
                                                c >= 0 ? _hcr->GetPlayingWAV(c) : -1); }
    else if (key == "VOL")      { int c = hcrChan(hcrField(fU,1));
                                  Serial.printf("[HCR] VOL %s = %.0f\n", f.c_str(),
                                                c >= 0 ? _hcr->getVolume(c) : -1.0f); }
    else Serial.println("[HCR] GET fields: EMOTION,H|S|M|C / DURATION / OVERRIDE / "
                        "MUSE / WAVCOUNT / PLAYING,V|A|B / VOL,V|A|B");
    return;
  }

  // ---- PORT,S<port>:<baud> -------------------------------------------
  String spec = a;
  if (aU.startsWith("PORT,")) spec = a.substring(5);   // tolerate ?HCR,PORT,S1:9600
  spec.trim();
  String specU = spec; specU.toUpperCase();

  if (!specU.startsWith("S")) {
    Serial.printf("[HCR] Invalid. Use: %cHCR,PORT,S<port>:<baud>  (e.g. S1:9600)\n",
                  LocalFunctionIdentifier);
    return;
  }
  int colon = spec.indexOf(':');
  if (colon < 0) {
    Serial.printf("[HCR] Missing baud. Use: %cHCR,PORT,S<port>:<baud>\n",
                  LocalFunctionIdentifier);
    return;
  }
  int serialPort = spec.substring(1, colon).toInt();
  int baudRate   = spec.substring(colon + 1).toInt();

  if (serialPort < 1 || serialPort > 5) {
    Serial.println("[HCR] Invalid serial port. Must be S1-S5"); return;
  }
  if (baudRate != 9600 && baudRate != 19200 && baudRate != 38400 &&
      baudRate != 57600 && baudRate != 115200) {
    Serial.println("[HCR] Baud must be 9600/19200/38400/57600/115200"); return;
  }
  if (serialPort >= 3 && baudRate > 9600) {
    Serial.println("\n⚠️  =============== WARNING ===============");
    Serial.printf("S%d is SOFTWARE SERIAL — unreliable above 9600 baud\n", serialPort);
    Serial.println("❌ CONFIGURATION BLOCKED!");
    Serial.printf("  Use a hardware port: %cHCR,PORT,S1:%d   or   S%d:9600\n",
                  LocalFunctionIdentifier, baudRate, serialPort);
    Serial.println("=========================================\n");
    return;
  }

  hcrReservePort(serialPort, baudRate);
}

// ==================== Status / List =====================================

void printHCRSettings() {
  Serial.println("---- HCR Configuration ----");
  if (!hcrConfig.configured) {
    Serial.println("  Not configured.  Use ?HCR,PORT,S<port>:<baud>");
    return;
  }
  Serial.printf("  Port:  S%d\n", hcrConfig.serialPort);
  Serial.printf("  Baud:  %lu\n", (unsigned long)hcrConfig.baudRate);
  Serial.printf("  Poll:  %us%s\n", (unsigned)hcrConfig.pollSec,
                hcrConfig.pollSec ? "" : " (off)");
  Serial.printf("  Link:  %s\n", _hcr ? "active" : "inactive");
}

void printHCRStatus() {
  if (!hcrConfig.configured || !_hcr) {
    Serial.println("[HCR:cfg=0]");
    return;
  }
  // Age only meaningful when we've actually polled. Off / pre-first-poll -> -1.
  long ageSec = -1;
  if (hcrConfig.pollSec > 0 && _hcrLastRxPollMs != 0) {
    ageSec = (long)((millis() - _hcrLastRxPollMs) / 1000UL);
  }
  char line[256];
  snprintf(line, sizeof(line),
    "[HCR:cfg=1,port=%d,poll=%u,age=%ld,H=%d,S=%d,M=%d,C=%d,dur=%.2f,"
    "ovr=%d,muse=%d,wav=%d,pV=%d,pA=%d,pB=%d,vV=%.0f,vA=%.0f,vB=%.0f]",
    hcrConfig.serialPort,
    (unsigned)hcrConfig.pollSec,
    ageSec,
    _hcr->GetEmotion(HAPPY), _hcr->GetEmotion(SAD),
    _hcr->GetEmotion(MAD),   _hcr->GetEmotion(SCARED),
    _hcr->GetDuration(),
    _hcr->GetOverride(),
    _hcr->GetMuse(),
    _hcr->GetWAVCount(),
    _hcr->GetPlayingWAV(CH_V), _hcr->GetPlayingWAV(CH_A), _hcr->GetPlayingWAV(CH_B),
    _hcr->getVolume(CH_V), _hcr->getVolume(CH_A), _hcr->getVolume(CH_B));
  Serial.println(line);
}

void printHCRBackup(String &chainedConfig, String &chainedConfigDefault,
                    char delimiter, bool printToSerial,
                    const String &defSep, const String &defFunc) {
  if (!hcrConfig.configured) return;

  String suffix = "HCR,PORT,S" + String(hcrConfig.serialPort) +
                  ":" + String(hcrConfig.baudRate);
  String cmd = String(LocalFunctionIdentifier) + suffix;
  if (printToSerial) Serial.println(cmd);
  chainedConfig        += String(delimiter) + cmd;
  chainedConfigDefault += defSep + defFunc + suffix;

  suffix = "HCR,POLL," + String(hcrConfig.pollSec);
  cmd    = String(LocalFunctionIdentifier) + suffix;
  if (printToSerial) Serial.println(cmd);
  chainedConfig        += String(delimiter) + cmd;
  chainedConfigDefault += defSep + defFunc + suffix;
}

// ==================== NVS storage =======================================

void saveHCRSettings() {
  preferences.begin("hcr_cfg", false);
  preferences.putUChar("port", hcrConfig.serialPort);
  preferences.putUInt ("baud", hcrConfig.baudRate);
  preferences.putBool ("en",   hcrConfig.configured);
  preferences.putUShort("poll", hcrConfig.pollSec);
  preferences.end();
}

void loadHCRSettings() {
  preferences.begin("hcr_cfg", true);
  hcrConfig.serialPort = preferences.getUChar ("port", 0);
  hcrConfig.baudRate   = preferences.getUInt  ("baud", 9600);
  hcrConfig.configured = preferences.getBool  ("en",   false);
  hcrConfig.pollSec    = preferences.getUShort("poll", 10);
  preferences.end();

  if (hcrConfig.configured) {
    Serial.printf("[HCR] Loaded: S%d at %lu baud, poll=%us\n",
                  hcrConfig.serialPort,
                  (unsigned long)hcrConfig.baudRate,
                  (unsigned)hcrConfig.pollSec);
  }
}
