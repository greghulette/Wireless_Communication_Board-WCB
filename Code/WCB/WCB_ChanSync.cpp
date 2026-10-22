#include "WCB_RemoteTerm.h"   // Must be first — redirects Serial -> WCBDebugSerial (CLAUDE.md rule 12)
#include "WCB_ChanSync.h"
#include "WCB_Storage.h"      // meshChannel, WCB_Number, preferences, etmEnabled, specialPeerEnabled, MAX_WCB_COUNT
#include "WCB_WDP.h"          // wdpNeighborGet: a neighbor's CHAN TLV
#include "WCB_WiFi.h"         // pinned (JOIN associated), can-hop, interface up, full-band scan
#include "WCB_PWM.h"          // pwmResendPassthrough: a PWM change sent while the radio was away

#include <esp_wifi.h>
#include <esp_random.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>

namespace cs = chansync;

// ---- Provided by WCB.ino --------------------------------------------------------
extern bool wcbPeerActive[MAX_WCB_COUNT];
extern bool wcbPeerTemporary[MAX_WCB_COUNT];
extern bool wcbPeerOnline(uint8_t id);   // a member (the controller peer included) AND ETM-online
extern bool debugMGMT;                   // per-frame lines ride ?DEBUG,MGMT, like WDP's
extern void reboot();                    // the deferred restart (rebootPending, CLAUDE.md rule 11)
// The ETM-struct broadcast envelope WDP uses (wdpBroadcastAs): sent through wcbEspNowSend (rule 16), never gated by
// lastReceivedViaESPNOW - these are not commands, so the loop-prevention rule (CLAUDE.md rule 3) has nothing to say.
extern void chanSyncBroadcastFrame(uint8_t packetType, const uint8_t *payload, int len);
// What would be cut in half by taking the radio off the mesh channel: an OTA session, a config pull, a pending
// restart; for a search also the command queue, ETM commands awaiting an ACK and an ETM characterization.
extern bool chanSyncHostBusy(bool forSearch, const char **why);
extern bool chanSyncRestartPending();

// ════════════════════════════════════════════════════════════════════════════
//  State
// ════════════════════════════════════════════════════════════════════════════
static bool    s_enabled = true;                   // ?CHSYNC,ON (NVS wcb_config/chsync)
static char    s_savedCc[3]   = "01";              // ?REGION (NVS wcb_config/region): what the next boot applies
static char    s_appliedCc[3] = "01";              // what this boot applied
static uint8_t s_maxCh = 11;                       // the applied region's top channel, capped at 13
static uint8_t s_home  = WCB_MESH_CHANNEL_DEFAULT; // the channel the radio is on when it is home
static volatile bool s_away = false;               // a search hop or a pull: the radio is on another channel

// The IDF's own list (esp_wifi.h, esp_wifi_set_country_code, attention 6). "01" is world safe mode - the driver's
// default, channels 1-11 - and makes no call at all.
static const char REGIONS[] = "01 AT AU BE BG BR CA CH CN CY CZ DE DK EE ES FI FR GB GR HK HR HU IE IN IS IT JP KR LI LT "
                              "LU LV MT MX NL NO NZ PL PT RO SE SI SK TW US";

// Frames from the receive callback. REPLY is the longest frame.
static_assert(cs::REPLY_LEN >= cs::MOVE_LEN && cs::REPLY_LEN >= cs::PROBE_LEN, "CsFrame must hold every frame");
struct CsFrame {
  uint8_t type;
  uint8_t sender;
  uint8_t away;      // it arrived while the radio was visiting another channel
  uint8_t pad;
  uint8_t b[cs::REPLY_LEN];
};
static QueueHandle_t     s_q = nullptr;
static volatile uint32_t s_qDropped = 0;           // full queue (WiFi task counts, loop reports)

enum : uint8_t { M_IDLE = 0, M_SEARCH, M_MOVE };
static uint8_t s_mode = M_IDLE;

// A move this board leads (it announces) or follows (it heard one). One at a time.
struct MoveJob {
  uint8_t       ch;
  bool          leading;
  bool          joinFollow;    // led by a JOIN board that found its network on ch
  bool          restarting;    // applied by a restart: nothing more happens until it
  uint8_t       framesLeft;    // leader: MOVE frames still to send
  uint8_t       flags;         // leader: the frames' flags
  uint8_t       from;          // follower: who sent it
  uint16_t      moveId;
  unsigned long switchAt;
  unsigned long nextFrameAt;
  unsigned long deferSince;    // 0 = not deferred
  const char   *why;
};
static MoveJob s_mv;
static uint16_t s_seenMove[MAX_WCB_COUNT + 1];     // per sender: the last moveId acted on (0 = none)

// A search: visit each other channel, PROBE, collect REPLYs; then go to a better group or pull worse ones home.
enum : uint8_t { SP_HOP = 0, SP_PULL };
struct SearchJob {
  uint8_t       phase;
  uint8_t       chans[cs::MAX_CHANNEL_CAP];
  uint8_t       n, idx;
  uint16_t      nonce[cs::MAX_CHANNEL_CAP + 1];
  cs::Key       found[cs::MAX_CHANNEL_CAP + 1];
  unsigned long until;         // hop: end of the dwell on chans[idx]; pull: when the next frame is due
  uint8_t       pulls[cs::MAX_CHANNEL_CAP];
  uint8_t       nPulls, pullIdx, pullFrames;
  uint16_t      pullId;
  bool          manual;
};
static SearchJob s_sr;

// The search trigger (docs/CHANNEL_SYNC.md §4): the first attempt TRIGGER_STABLE_MS after the condition began, each next
// one the gap between their searchAttemptOffsetMs() after the one before STARTED - so attempts held up (a station on
// the access point, a busy board) never run back to back once free.
static unsigned long s_condSince = 0;              // when shouldSearch() began to hold (0 = it does not)
static uint8_t       s_attempt   = 0;              // the next attempt (0-based); SEARCH_ATTEMPTS = all made
static unsigned long s_nextDue   = 0;              // when attempt s_attempt may start
static bool          s_sched     = false;          // a schedule is under way (s_attempt / s_nextDue mean something)
static bool          s_gaveUpSaid = false;
// A condition that clears and comes back within this long carries on the schedule it had instead of starting a new
// one: a member that flaps offline and online would otherwise restart the seven attempts every cycle, without bound.
static const unsigned long TRIGGER_RESET_MS = 10UL * 60000UL;
static unsigned long s_clearedAt = 0;              // when the condition last cleared, with a schedule under way (0 = none)
static unsigned long s_nextEval  = 0;
static bool          s_manualPending = false;      // ?CHSYNC,SEARCH waiting for the board to be able to hop
static unsigned long s_manualSince   = 0;
static const unsigned long MANUAL_WAIT_MS = 10000; // ...for this long
static const unsigned long EVAL_EVERY_MS  = 500;
static const unsigned long PULL_TAIL_MS   = 50;    // after a pull's last frame, before the radio leaves that channel

// A REPLY this board owes a prober, sent after (id % 16) * REPLY_JITTER_STEP_MS.
static bool          s_replyArmed = false;
static uint16_t      s_replyNonce = 0;
static uint8_t       s_replyProber = 0;            // who asked: never named as the best device here (sendReply)
static unsigned long s_replyAt    = 0;

// The latest MOVE heard at home while a search had the radio, judged once it is home again (judge): an operator move
// made meanwhile would otherwise be reversed by this board's next search, which would find its group gone and pull it
// back. Not one heard away: that is another channel's.
static bool          s_heldValid = false;
static CsFrame       s_held;
static unsigned long s_heldAt    = 0;

static bool          s_wasAway   = false;          // chanSyncRadioAway() at the last tick: home again -> PWM resend

static bool syncActive() { return s_enabled && etmEnabled && wdpEnabled; }   // chanSyncActive (WCB_ChanSync.h)

// ════════════════════════════════════════════════════════════════════════════
//  Keys: who this board hears, and who leads
// ════════════════════════════════════════════════════════════════════════════
static cs::Key selfKey() {
  cs::Key k;
  k.id    = (uint8_t)WCB_Number;
  k.rank  = (uint8_t)WCB_Number;                   // a WCB's rank is its number (§1)
  k.flags = (uint8_t)((syncActive() ? cs::F_SYNC : 0) | (wcbWifiJoinAssociated() ? cs::F_PINNED : 0));
  return k;
}

// A member of this board's peer set: the WCBQ floor, learned and temporary peers, and the controller peer.
static bool memberOf(uint8_t id) {
  if (id < 1 || id > MAX_WCB_COUNT || id == (uint8_t)WCB_Number) return false;
  return wcbPeerActive[id - 1] || (specialPeerEnabled && id == WCB_SPECIAL_PEER_ID);
}

// A member's key: its CHAN TLV while its advert is fresh (WDP's TTL), else derived (§2) - a WCB's rank is its
// number, a client's 100 + id, a temporary peer never leads; unpinned and SYNC off either way.
static cs::Key keyOf(uint8_t id) {
  cs::Key k;
  k.id    = id;
  k.flags = 0;
  const WdpNeighbor *nb = wdpNeighborGet(id);
  if (nb && nb->confirmed && nb->hasChan) {
    k.rank  = nb->chan.rank;
    k.flags = (uint8_t)(nb->chan.flags & (cs::F_PINNED | cs::F_SYNC));
    return k;
  }
  const bool temporary = wcbPeerTemporary[id - 1] || (nb && nb->temporary);
  const bool client    = nb ? nb->isClient : (specialPeerEnabled && id == WCB_SPECIAL_PEER_ID);
  k.rank = temporary ? cs::RANK_NEVER : (client ? (uint8_t)(cs::RANK_CLIENT_BASE + id) : id);
  return k;
}

// What this board hears right now.
struct View {
  cs::Key self;
  cs::Key best;        // the best device present, ITSELF INCLUDED - among those that take part (F_SYNC)
  cs::Key bestPinned;  // the best pinned device heard (any, sync or not); noKey() = none
  uint8_t heard;
  uint8_t missing;     // members that can lead and are offline by the ETM sweep (temporary peers never count)
  uint8_t missingFirst;
};

// A device that does not take part - SYNC off, or firmware without channel sync (a derived key) - never leads here:
// it cannot move or search, so a group led by it would never look for a stray board, and would refuse every MOVE
// but its own. It is still counted as heard, and a pinned one still keeps the mesh where it is (bestPinned).
// exclude: a member left out of the view - the prober a REPLY answers, which may still read online here from before
// it left this channel, and is not who leads here.
static View look(uint8_t exclude = 0) {
  View v;
  v.self = selfKey();
  v.best = v.self;
  v.bestPinned = cs::noKey();
  v.heard = v.missing = v.missingFirst = 0;
  for (uint8_t id = 1; id <= MAX_WCB_COUNT; id++) {
    if (!memberOf(id) || id == exclude) continue;
    const cs::Key k = keyOf(id);
    if (wcbPeerOnline(id)) {
      v.heard++;
      if ((k.flags & cs::F_PINNED) && cs::better(k, v.bestPinned)) v.bestPinned = k;
      if ((k.flags & cs::F_SYNC) && cs::better(k, v.best)) v.best = k;
    } else if (k.rank != cs::RANK_NEVER && !wcbPeerTemporary[id - 1]) {
      v.missing++;
      if (!v.missingFirst) v.missingFirst = id;
    }
  }
  return v;
}

static void copyCc(char *dst, const char *src) { dst[0] = src[0]; dst[1] = src[1]; dst[2] = '\0'; }

// ════════════════════════════════════════════════════════════════════════════
//  NVS
// ════════════════════════════════════════════════════════════════════════════
static bool regionKnown(const char *cc) {
  if (!cc || strlen(cc) != 2) return false;
  for (const char *p = REGIONS; ; p += 3) {
    if (p[0] == cc[0] && p[1] == cc[1]) return true;
    if (p[2] != ' ') return false;
  }
}

void chanSyncLoadSettings() {
  preferences.begin("wcb_config", true);
  s_enabled = preferences.getBool("chsync", true);
  String cc = preferences.getString("region", "01");
  preferences.end();
  cc.toUpperCase();
  copyCc(s_savedCc, regionKnown(cc.c_str()) ? cc.c_str() : "01");   // a corrupt value reads as the default
}

static void saveEnabled() {
  preferences.begin("wcb_config", false);
  preferences.putBool("chsync", s_enabled);
  preferences.end();
}

// The channel the next boot starts on. Written directly, not through saveMeshChannelToPreferences: that one also sets
// meshChannel and prints "reboot to apply", and a live move sets meshChannel itself, once the radio is there.
static void persistChannel(uint8_t ch) {
  preferences.begin("wcb_config", false);
  preferences.putUChar("mesh_channel", ch);
  preferences.end();
}

// ════════════════════════════════════════════════════════════════════════════
//  Boot
// ════════════════════════════════════════════════════════════════════════════
uint8_t chanSyncApplyRegionAtBoot() {
  copyCc(s_appliedCc, "01");
  if (strcmp(s_savedCc, "01") != 0) {
    // 802.11d OFF: with it on, a JOIN board adopts its router's region while associated, and the channels it may use
    // stop being the ones the rest of the mesh was configured for. This never reaches the driver's own flash copy:
    // WiFi.persistent(false) in setup() has already set WIFI_STORAGE_RAM (WiFiGeneric.cpp wifiLowLevelInit), so
    // ?REGION,01 returns this board to the driver default at the next boot, as promised.
    const esp_err_t e = esp_wifi_set_country_code(s_savedCc, false);
    if (e == ESP_OK) copyCc(s_appliedCc, s_savedCc);
    else Serial.printf("[REGION] esp_wifi_set_country_code(\"%s\") FAILED (0x%x) - running on the default region 01\n",
                       s_savedCc, (unsigned)e);
  } else {
    // "01" makes no call - unless the driver holds another region anyway: the IDF keeps the country in its own NVS
    // (esp_wifi.h, esp_wifi_set_country_code attention 4) whenever the storage is FLASH, which other firmware on this
    // chip may have left there. Only then is it put back to the default, 802.11d on as the default has it, so a board
    // can never stay in another region whatever ran on it before.
    char now[8] = { 0 };
    if (esp_wifi_get_country_code(now) == ESP_OK && !(now[0] == '0' && now[1] == '1')) {
      const esp_err_t e = esp_wifi_set_country_code("01", true);
      Serial.printf("[REGION] the radio held region \"%.2s\" from before; reset to 01 (%s)\n", now,
                    e == ESP_OK ? "done" : "FAILED");
    }
  }
  wifi_country_t c;
  uint8_t top = 11;
  if (esp_wifi_get_country(&c) == ESP_OK && c.nchan) top = (uint8_t)(c.schan + c.nchan - 1);
  if (top > cs::MAX_CHANNEL_CAP) top = cs::MAX_CHANNEL_CAP;   // 14 is never used (Japan, 802.11b only)
  if (top < 1) top = 11;
  s_maxCh = top;
  if (strcmp(s_appliedCc, "01") != 0) Serial.printf("[REGION] %s: channels 1-%u\n", s_appliedCc, s_maxCh);
  // A saved channel the region does not allow - the region was changed back, or a backup from a board in another
  // region was restored. Channel 1 is the factory default and where older firmware put an out-of-range channel too
  // (loadMeshChannelFromPreferences); the saved value stays, so fixing the region alone brings it back at the next
  // boot. Channel sync then finds the mesh if it is on a channel this region allows.
  if (meshChannel < 1 || meshChannel > s_maxCh) {
    Serial.printf("[REGION] *** saved mesh channel %u is outside region %s (channels 1-%u): running on channel %u "
                  "this boot. The saved value is kept - ?REGION,<cc> that allows it, or ?WCBCH,<1-%u>, then reboot. ***\n",
                  meshChannel, s_appliedCc, s_maxCh, (unsigned)WCB_MESH_CHANNEL_DEFAULT, s_maxCh);
    meshChannel = WCB_MESH_CHANNEL_DEFAULT;
  }
  s_home = meshChannel;
  return s_maxCh;
}

void chanSyncBegin() {
  s_home = meshChannel;
  if (!s_q) s_q = xQueueCreate(16, sizeof(CsFrame));
  memset(&s_mv, 0, sizeof(s_mv));
  memset(&s_sr, 0, sizeof(s_sr));
  memset(s_seenMove, 0, sizeof(s_seenMove));
  if (!s_enabled) Serial.println("[CHSYNC] channel sync is OFF on this board (?CHSYNC,ON)");
}

// ════════════════════════════════════════════════════════════════════════════
//  Receive (WiFi task): copy and return
// ════════════════════════════════════════════════════════════════════════════
bool chanSyncRadioAway() { return s_away || wcbWifiFullScanRunning(); }

void chanSyncOnFrame(uint8_t packetType, int senderId, const uint8_t *payload) {
  if (!s_q || !payload || senderId < 1 || senderId > MAX_WCB_COUNT) return;
  CsFrame f;
  f.type   = packetType;
  f.sender = (uint8_t)senderId;
  f.away   = chanSyncRadioAway() ? 1 : 0;
  f.pad    = 0;
  memcpy(f.b, payload, sizeof(f.b));        // structCommand is 200 bytes: always room
  if (xQueueSend(s_q, &f, 0) != pdTRUE) s_qDropped = s_qDropped + 1;   // only this task writes it
}

// ════════════════════════════════════════════════════════════════════════════
//  WDP
// ════════════════════════════════════════════════════════════════════════════
void chanSyncSelfFacts(cs::ChanFacts &f) {
  const cs::Key k = selfKey();
  f.channel = s_home;
  f.rank    = k.rank;
  f.flags   = k.flags;
  copyCc(f.cc, s_appliedCc);
}

int chanSyncPutTlv(uint8_t *buf, int o, int max) {
  cs::ChanFacts f;
  chanSyncSelfFacts(f);
  return cs::putChanTlv(buf, o, max, f);
}

// ════════════════════════════════════════════════════════════════════════════
//  State queries
// ════════════════════════════════════════════════════════════════════════════
bool        chanSyncEnabled()      { return s_enabled; }
bool        chanSyncActive()       { return syncActive(); }
bool        chanSyncBusy()         { return s_mode != M_IDLE; }

bool chanSyncMayLeaveChannel(const char **why) {
  if (!syncActive())             { if (why) *why = "channel sync is inactive"; return false; }
  if (millis() < cs::BOOT_GRACE_MS) { if (why) *why = "the boot grace (presence and CHAN facts settling)"; return false; }
  return !chanSyncHostBusy(true, why);
}
uint8_t     chanSyncHomeChannel()  { return s_home; }
uint8_t     chanSyncMaxChannel()   { return s_maxCh; }
const char *chanSyncSavedRegion()  { return s_savedCc; }

uint8_t chanSyncWcbchLimit(bool *regionPending) {
  const bool pending = strcmp(s_savedCc, s_appliedCc) != 0;
  if (regionPending) *regionPending = pending;
  return pending ? cs::MAX_CHANNEL_CAP : s_maxCh;
}

// ════════════════════════════════════════════════════════════════════════════
//  Sending
// ════════════════════════════════════════════════════════════════════════════
static uint16_t newId() {
  uint16_t id;
  do { id = (uint16_t)esp_random(); } while (id == 0);
  return id;
}

static void sendMove(uint8_t ch, uint8_t flags, uint16_t moveId, uint16_t delayMs) {
  cs::Move m;
  m.newChannel = ch;
  m.rank       = selfKey().rank;
  m.flags      = flags;
  m.moveId     = moveId;
  m.delayMs    = delayMs;
  copyCc(m.cc, s_appliedCc);
  uint8_t b[cs::MOVE_LEN];
  const int n = cs::encodeMove(b, sizeof(b), m);
  if (n) chanSyncBroadcastFrame(cs::PT_MOVE, b, n);
}

static void sendProbe(uint16_t nonce) {
  const cs::Key k = selfKey();
  cs::Probe p;
  p.homeChannel = s_home;
  p.rank        = k.rank;
  p.flags       = k.flags;
  p.nonce       = nonce;
  uint8_t b[cs::PROBE_LEN];
  const int n = cs::encodeProbe(b, sizeof(b), p);
  if (n) chanSyncBroadcastFrame(cs::PT_PROBE, b, n);
}

static void sendReply(uint16_t nonce, uint8_t prober) {
  const View v = look(prober);
  cs::Reply r;
  r.channel = s_home;
  r.nonce   = nonce;
  r.rank    = v.self.rank;
  r.flags   = v.self.flags;
  r.best    = v.best;
  copyCc(r.cc, s_appliedCc);
  uint8_t b[cs::REPLY_LEN];
  const int n = cs::encodeReply(b, sizeof(b), r);
  if (n) chanSyncBroadcastFrame(cs::PT_PROBE_REPLY, b, n);
  if (debugMGMT) Serial.printf("[CHSYNC] replied to a probe: WCB%u leads here (rank %u)\n", r.best.id, r.best.rank);
}

// ════════════════════════════════════════════════════════════════════════════
//  The radio
// ════════════════════════════════════════════════════════════════════════════
static bool radioTo(uint8_t ch) { return esp_wifi_set_channel(ch, WIFI_SECOND_CHAN_NONE) == ESP_OK; }

static void goHome() {
  if (!radioTo(s_home))
    Serial.printf("[CHSYNC] *** could not return the radio to channel %u - this board is OFF THE MESH ***\n", s_home);
  s_away = false;
}

// ════════════════════════════════════════════════════════════════════════════
//  Moving
// ════════════════════════════════════════════════════════════════════════════
// Forget the schedule outright: after a move, ?CHSYNC,OFF, or a condition that stayed clear TRIGGER_RESET_MS.
static void resetTrigger() {
  s_condSince  = 0;
  s_sched      = false;
  s_attempt    = 0;
  s_nextDue    = 0;
  s_clearedAt  = 0;
  s_gaveUpSaid = false;
}

static void startLeadMove(uint8_t ch, uint8_t flags, const char *why, bool joinFollow) {
  memset(&s_mv, 0, sizeof(s_mv));
  const unsigned long now = millis();
  s_mv.ch          = ch;
  s_mv.leading     = true;
  s_mv.joinFollow  = joinFollow;
  s_mv.framesLeft  = cs::MOVE_REPEATS;
  s_mv.flags       = flags;
  s_mv.moveId      = newId();
  s_mv.switchAt    = now + cs::MOVE_COUNTDOWN_MS;
  s_mv.nextFrameAt = now;
  s_mv.why         = why;
  s_mode = M_MOVE;
  Serial.printf("[CHSYNC] moving the mesh from channel %u to %u in %u ms (%s)\n",
                s_home, ch, (unsigned)cs::MOVE_COUNTDOWN_MS, why);
}

// Apply the move at its switch time. A board with an access point up, or joined to one, moves by restarting (the
// interface comes up on the new channel); any other switches the radio live. ESP-NOW peers are registered on channel 0,
// "the current channel", so nothing needs re-registering.
static void applyMove(unsigned long now) {
  const uint8_t ch = s_mv.ch;
  // A restart already pending would come up on the old channel and lose the move: save the channel now and let that
  // restart take it. (Waiting for it, as for the other busy cases, would let it happen first.)
  if (chanSyncRestartPending()) {
    persistChannel(ch);
    s_mv.restarting = true;
    Serial.printf("[CHSYNC] a restart is already pending: this board comes back on channel %u\n", ch);
    return;
  }
  const char *why = nullptr;
  bool busy = chanSyncHostBusy(false, &why);
  if (!busy && wcbWifiJoinScanning()) { busy = true; why = "a WiFi scan"; }   // the driver refuses a channel mid-scan
  if (busy) {
    if (!s_mv.deferSince) {
      s_mv.deferSince = now | 1;
      Serial.printf("[CHSYNC] the move to channel %u waits for %s (at most %lu s)\n", ch, why,
                    (unsigned long)(cs::MOVE_DEFER_MAX_MS / 1000));
    }
    if (now - s_mv.deferSince < cs::MOVE_DEFER_MAX_MS) return;
    Serial.printf("[CHSYNC] %s is still running after %lu s - moving anyway\n", why,
                  (unsigned long)(cs::MOVE_DEFER_MAX_MS / 1000));
  }
  if (wcbWifiInterfaceUp()) {
    persistChannel(ch);
    s_mv.restarting = true;
    Serial.printf("[CHSYNC] channel %u saved - restarting so the %s comes up there\n", ch,
                  wcbWifiApUp() ? "access point" : "network association");
    reboot();
    return;
  }
  wcbWifiJoinCancelConnect();         // a connect in progress would hold the old channel (a no-op without one)
  if (!radioTo(ch)) {
    persistChannel(ch);
    s_mv.restarting = true;
    Serial.printf("[CHSYNC] the radio refused channel %u live - saved, restarting onto it\n", ch);
    reboot();
    return;
  }
  const uint8_t from = s_home;
  s_home      = ch;
  meshChannel = ch;
  persistChannel(ch);
  s_mode = M_IDLE;
  resetTrigger();
  pwmResendPassthrough();             // a PWM change sent across the switch reached nobody (WCB_PWM.h)
  Serial.printf("[CHSYNC] moved from channel %u to %u\n", from, ch);
}

static void tickMove(unsigned long now) {
  if (s_mv.restarting) return;                       // the restart takes it from here
  // Not while the radio is away (a JOIN full-band scan): the frame would go out on whatever channel it is visiting.
  if (s_mv.leading && s_mv.framesLeft && (long)(now - s_mv.nextFrameAt) >= 0 && !chanSyncRadioAway()) {
    const long left = (long)(s_mv.switchAt - now);
    sendMove(s_mv.ch, s_mv.flags, s_mv.moveId, (uint16_t)(left > 0 ? left : 0));   // the time still left
    s_mv.framesLeft--;
    s_mv.nextFrameAt += cs::MOVE_REPEAT_GAP_MS;
  }
  if ((long)(now - s_mv.switchAt) >= 0 && (!s_mv.leading || !s_mv.framesLeft)) applyMove(now);
}

static const char *verdictText(cs::MoveVerdict v) {
  switch (v) {
    case cs::MV_SYNC_OFF:     return "channel sync is off here";
    case cs::MV_BAD_CHANNEL:  return "the channel is outside this board's region";
    case cs::MV_SAME_CHANNEL: return "already there";
    case cs::MV_PINNED_SELF:  return "this board is joined to its access point";
    case cs::MV_OUTRANKED:    return "a board it hears outranks the sender";
    default:                  return "accepted";
  }
}

// A MOVE frame heard at home, at rxAt: one held while a search had the radio is judged later, with the time it has
// already waited taken off its countdown.
static void judgeMoveFrame(const CsFrame &f, unsigned long rxAt, unsigned long now) {
  cs::Move m;
  if (!cs::decodeMove(f.b, sizeof(f.b), m)) return;
  if (s_seenMove[f.sender] == m.moveId) return;       // a repeat frame of a move already judged
  s_seenMove[f.sender] = m.moveId;
  if (s_mode == M_MOVE && s_mv.restarting) return;
  const unsigned long waited = now - rxAt;
  m.delayMs = (waited >= m.delayMs) ? 0 : (uint16_t)(m.delayMs - waited);   // past its switch: follow at once
  cs::Key sender;
  sender.id    = f.sender;
  sender.rank  = m.rank;
  sender.flags = (uint8_t)(m.flags & (cs::F_PINNED | cs::F_SYNC));
  const View v = look();
  const cs::MoveVerdict verdict = cs::judgeMove(m, sender, v.self, v.best, s_home, s_maxCh, syncActive());
  if (verdict != cs::MV_ACCEPT) {
    if (verdict != cs::MV_SAME_CHANNEL)
      Serial.printf("[CHSYNC] not following WCB%u's move to channel %u: %s\n", f.sender, m.newChannel,
                    verdictText(verdict));
    return;
  }
  memset(&s_mv, 0, sizeof(s_mv));
  s_mv.ch       = m.newChannel;
  s_mv.from     = f.sender;
  s_mv.moveId   = m.moveId;
  s_mv.switchAt = now + m.delayMs;
  s_mv.why      = (m.flags & cs::MF_OPERATOR) ? "an operator move" : "following the mesh leader";
  s_mode = M_MOVE;
  Serial.printf("[CHSYNC] WCB%u moves the mesh to channel %u in %u ms - following%s\n", f.sender, m.newChannel,
                (unsigned)m.delayMs, (m.flags & cs::MF_OPERATOR) ? " (operator move)" : "");
}

static void onMove(const CsFrame &f, unsigned long now) {
  if (f.away) return;                                 // heard on another channel: that channel's business
  if (s_mode == M_SEARCH) {                           // the search decides for the group: judged once home (judge)
    cs::Move m;
    if (!cs::decodeMove(f.b, sizeof(f.b), m) || s_seenMove[f.sender] == m.moveId) return;
    s_held      = f;
    s_heldAt    = now;
    s_heldValid = true;
    return;
  }
  judgeMoveFrame(f, now, now);
}

// ════════════════════════════════════════════════════════════════════════════
//  Replies and probes
// ════════════════════════════════════════════════════════════════════════════
static void onProbe(const CsFrame &f, unsigned long now) {
  cs::Probe p;
  if (!cs::decodeProbe(f.b, sizeof(f.b), p)) return;
  if (f.away || !syncActive()) return;                // sync off: never answers a search (CHANNEL_SYNC.md, intro)
  s_replyArmed = true;
  s_replyNonce  = p.nonce;
  s_replyProber = f.sender;
  s_replyAt    = now + (unsigned long)((WCB_Number % 16) * cs::REPLY_JITTER_STEP_MS);
  if (debugMGMT) Serial.printf("[CHSYNC] WCB%u (home channel %u) is probing this channel\n", f.sender, p.homeChannel);
}

static void onReply(const CsFrame &f) {
  cs::Reply r;
  if (!cs::decodeReply(f.b, sizeof(f.b), r)) return;
  if (s_mode != M_SEARCH || s_sr.phase != SP_HOP) return;
  if (r.channel < 1 || r.channel > cs::MAX_CHANNEL_CAP || r.nonce != s_sr.nonce[r.channel]) return;   // not this search's
  cs::Key responder;
  responder.id    = f.sender;
  responder.rank  = r.rank;
  responder.flags = (uint8_t)(r.flags & (cs::F_PINNED | cs::F_SYNC));
  cs::Key best = r.best;
  best.flags = (uint8_t)(best.flags & (cs::F_PINNED | cs::F_SYNC));
  if (!(best.id >= 1 && best.id <= MAX_WCB_COUNT)) best = cs::noKey();
  // A responder that still lists this board online (its 55 s sweep has not run yet, or older firmware without the
  // exclusion in sendReply) can name it as the best it hears: that is not who leads over there, and judgeSearch would
  // compare this board with itself and neither go nor pull. The responder's own key, below, stands in for it.
  if (best.id == (uint8_t)WCB_Number) best = cs::noKey();
  if (cs::better(responder, best)) best = responder;  // "itself included": hold a responder to it
  if (cs::better(best, s_sr.found[r.channel])) s_sr.found[r.channel] = best;
  if (debugMGMT) Serial.printf("[CHSYNC] channel %u: WCB%u answered, WCB%u leads there (rank %u)\n",
                               r.channel, f.sender, best.id, best.rank);
}

static void drainFrames(unsigned long now) {
  if (!s_q) return;
  static uint32_t reported = 0;
  const uint32_t d = s_qDropped;
  if (d != reported) {
    Serial.printf("[CHSYNC] %lu channel-sync frame(s) dropped: the queue was full\n", (unsigned long)(d - reported));
    reported = d;
  }
  CsFrame f;
  while (xQueueReceive(s_q, &f, 0) == pdTRUE) {
    if      (f.type == cs::PT_MOVE)        onMove(f, now);
    else if (f.type == cs::PT_PROBE)       onProbe(f, now);
    else if (f.type == cs::PT_PROBE_REPLY) onReply(f);
  }
}

// ════════════════════════════════════════════════════════════════════════════
//  Searching
// ════════════════════════════════════════════════════════════════════════════
// May the radio leave the mesh channel now? why = the first reason it may not.
static bool canSearchNow(const char **why) {
  if (!wcbWifiCanHop(why)) return false;
  return chanSyncMayLeaveChannel(why);
}

// A MOVE held while the search had the radio (onMove): judged now that it is home. True when it is being followed.
static bool takeHeldMove(unsigned long now) {
  if (!s_heldValid) return false;
  s_heldValid = false;
  const uint8_t was = s_mode;
  s_mode = M_IDLE;
  judgeMoveFrame(s_held, s_heldAt, now);
  if (s_mode == M_MOVE) return true;
  s_mode = was;
  return false;
}

static void endSearch(const char *line) {
  if (s_away) goHome();
  s_mode = M_IDLE;
  if (line) Serial.println(line);
  takeHeldMove(millis());
}

static void hopNext(unsigned long now) {
  while (s_sr.idx < s_sr.n) {
    const uint8_t c = s_sr.chans[s_sr.idx];
    s_away = true;                                     // before the switch: nothing heard there is taken for home
    if (radioTo(c)) {
      sendProbe(s_sr.nonce[c]);
      s_sr.until = now + cs::PROBE_DWELL_MS;
      return;
    }
    if (debugMGMT) Serial.printf("[CHSYNC] the radio refused channel %u - skipped\n", c);
    s_sr.idx++;
  }
}

static void startSearch(bool manual, unsigned long now) {
  memset(&s_sr, 0, sizeof(s_sr));
  s_sr.manual = manual;
  for (uint8_t c = 1; c <= s_maxCh; c++) {
    if (c == s_home) continue;
    s_sr.chans[s_sr.n++] = c;
    s_sr.nonce[c] = newId();
  }
  for (int c = 0; c <= cs::MAX_CHANNEL_CAP; c++) s_sr.found[c] = cs::noKey();
  s_sr.phase = SP_HOP;
  s_mode = M_SEARCH;
  const View v = look();
  if (manual)
    Serial.printf("[CHSYNC] searching channels 1-%u (all but %u) - asked for\n", s_maxCh, s_home);
  else
    Serial.printf("[CHSYNC] WCB%u is missing - searching channels 1-%u (all but %u), attempt %u of %u\n",
                  v.missingFirst, s_maxCh, s_home, s_attempt, (unsigned)cs::SEARCH_ATTEMPTS);
  hopNext(now);
  if (s_sr.idx >= s_sr.n) endSearch("[CHSYNC] search: no other channel could be visited");
}

static void judge(unsigned long now) {
  goHome();
  if (takeHeldMove(now)) {             // the mesh was moved meanwhile: follow it, do not undo it from what was found
    Serial.println("[CHSYNC] search: a MOVE came while it ran - following that instead");
    return;
  }
  const cs::Key self = selfKey();
  const cs::SearchResult r = cs::judgeSearch(self, s_home, s_sr.found);
  if (r.moveTo) {
    const cs::Key &k = s_sr.found[r.moveTo];
    Serial.printf("[CHSYNC] search: WCB%u (rank %u%s) leads a group on channel %u - taking this group there\n",
                  k.id, k.rank, (k.flags & cs::F_PINNED) ? ", pinned" : "", r.moveTo);
    s_mode = M_IDLE;
    startLeadMove(r.moveTo, self.flags, "a search found a better leader", false);
    return;
  }
  if (r.pullCount) {
    s_sr.phase = SP_PULL;
    s_sr.nPulls = s_sr.pullIdx = 0;
    for (uint8_t c = 1; c <= cs::MAX_CHANNEL_CAP; c++) if (r.pull[c]) s_sr.pulls[s_sr.nPulls++] = c;
    Serial.printf("[CHSYNC] search: %u group(s) led by a board this one outranks - pulling them home to channel %u\n",
                  s_sr.nPulls, s_home);
    s_sr.pullFrames = cs::PULL_REPEATS;              // "done" with a pull that has not started: the tick starts one
    s_sr.until = now;
    return;
  }
  endSearch("[CHSYNC] search: nobody answered on the other channels");
}

static void tickSearch(unsigned long now) {
  if (s_sr.phase == SP_HOP) {
    if ((long)(now - s_sr.until) < 0) return;          // still listening on chans[idx]
    s_sr.idx++;
    if (s_sr.idx < s_sr.n) { hopNext(now); if (s_sr.idx < s_sr.n) return; }
    judge(now);
    return;
  }
  // SP_PULL: on each worse group's channel, PULL_REPEATS frames of a MOVE home, PULL_REPEAT_GAP_MS apart.
  if ((long)(now - s_sr.until) < 0) return;
  if (s_sr.pullFrames < cs::PULL_REPEATS) {
    sendMove(s_home, selfKey().flags, s_sr.pullId,
             (uint16_t)(cs::PULL_COUNTDOWN_MS - s_sr.pullFrames * cs::PULL_REPEAT_GAP_MS));
    s_sr.pullFrames++;
    s_sr.until = now + (s_sr.pullFrames < cs::PULL_REPEATS ? cs::PULL_REPEAT_GAP_MS : PULL_TAIL_MS);
    return;
  }
  while (s_sr.pullIdx < s_sr.nPulls) {                  // the next worse group
    const uint8_t c = s_sr.pulls[s_sr.pullIdx++];
    s_away = true;
    if (!radioTo(c)) continue;
    s_sr.pullId     = newId();
    s_sr.pullFrames = 0;
    s_sr.until      = now;
    return;
  }
  endSearch("[CHSYNC] search: pull sent; the other group follows to this channel");
}

// The trigger (§4): only a group's leader that misses a known board, not before BOOT_GRACE_MS, and only once the
// condition has held TRIGGER_STABLE_MS; then SEARCH_ATTEMPTS attempts, each the searchAttemptOffsetMs() gap after the
// one before started; then nothing until the next boot, ?CHSYNC,SEARCH, or the condition clears for TRIGGER_RESET_MS
// and comes back. A condition back sooner carries on its schedule, at the earliest TRIGGER_STABLE_MS after it returned.
static void evaluateTrigger(unsigned long now) {
  if ((long)(now - s_nextEval) < 0) return;
  s_nextEval = now + EVAL_EVERY_MS;
  if (s_manualPending) {
    const char *why = nullptr;
    const View lv = look();
    if (lv.best.id != (uint8_t)WCB_Number) {          // leadership changed since the command: its leader searches
      s_manualPending = false;
      Serial.printf("[CHSYNC] search not started: WCB%u leads this group now\n", lv.best.id);
      return;
    }
    if (canSearchNow(&why)) {
      s_manualPending = false;
      startSearch(true, now);
      // ?CHSYNC,SEARCH restarts the schedule: if a board is still missing, the next attempts follow from here.
      s_sched      = true;
      s_attempt    = 1;
      s_nextDue    = now + (cs::searchAttemptOffsetMs(1) - cs::searchAttemptOffsetMs(0));
      s_condSince  = now | 1;
      s_clearedAt  = 0;
      s_gaveUpSaid = false;
      return;
    }
    if (now - s_manualSince >= MANUAL_WAIT_MS) {
      s_manualPending = false;
      Serial.printf("[CHSYNC] search not started: %s\n", why ? why : "this board cannot leave the channel now");
    }
    return;
  }
  const View v = look();
  // The condition with canHop = true: whether this board can hop is checked when an attempt is due, so an access
  // point's station coming and going does not restart the schedule.
  const bool cond = syncActive() && now >= cs::BOOT_GRACE_MS &&
                    cs::shouldSearch(v.self, v.best, v.missing > 0, true, true);
  if (!cond) {
    s_condSince = 0;
    if (s_sched) {
      if (!s_clearedAt) s_clearedAt = now | 1;
      else if (now - s_clearedAt >= TRIGGER_RESET_MS) resetTrigger();
    }
    return;
  }
  s_clearedAt = 0;
  if (!s_condSince) s_condSince = now | 1;
  if (!s_sched) {                                   // a new episode
    s_sched     = true;
    s_attempt   = 0;
    s_nextDue   = s_condSince + cs::TRIGGER_STABLE_MS;
    s_gaveUpSaid = false;
  } else if ((long)(s_nextDue - (s_condSince + cs::TRIGGER_STABLE_MS)) < 0) {
    s_nextDue = s_condSince + cs::TRIGGER_STABLE_MS;   // back after a short clear: it must hold again first
  }
  if (s_attempt >= cs::SEARCH_ATTEMPTS) {
    if (!s_gaveUpSaid) {
      s_gaveUpSaid = true;
      Serial.printf("[CHSYNC] WCB%u is still missing after %u searches - not searching again until the next boot, "
                    "?CHSYNC,SEARCH, or it has been back for %lu minutes\n", v.missingFirst,
                    (unsigned)cs::SEARCH_ATTEMPTS, TRIGGER_RESET_MS / 60000UL);
    }
    return;
  }
  if ((long)(now - s_nextDue) < 0) return;
  const char *why = nullptr;
  if (!canSearchNow(&why)) {
    if (debugMGMT) Serial.printf("[CHSYNC] a search is due but waits: %s\n", why ? why : "?");
    return;
  }
  // Re-based on when this attempt really starts: one held up for a while is not followed at once by the next.
  s_attempt++;
  if (s_attempt < cs::SEARCH_ATTEMPTS)
    s_nextDue = now + (cs::searchAttemptOffsetMs(s_attempt) - cs::searchAttemptOffsetMs(s_attempt - 1));
  startSearch(false, now);
}

// ════════════════════════════════════════════════════════════════════════════
//  loop()
// ════════════════════════════════════════════════════════════════════════════
void chanSyncTick() {
  const unsigned long now = millis();
  // Home again after a search hop, a pull or a JOIN full-band scan: a PWM passthrough change sent meanwhile went out
  // on another channel, and the passthrough sends only on change - a servo would stay where that frame left it.
  const bool away = chanSyncRadioAway();
  if (s_wasAway && !away) pwmResendPassthrough();
  s_wasAway = away;
  drainFrames(now);
  if (s_replyArmed && (long)(now - s_replyAt) >= 0) {
    s_replyArmed = false;
    if (!chanSyncRadioAway() && syncActive()) sendReply(s_replyNonce, s_replyProber);   // a JOIN full-band scan too
  }
  switch (s_mode) {
    case M_SEARCH: tickSearch(now); break;
    case M_MOVE:   tickMove(now);   break;
    default:       evaluateTrigger(now); break;
  }
}

// ════════════════════════════════════════════════════════════════════════════
//  WiFi JOIN follows its network (§5)
// ════════════════════════════════════════════════════════════════════════════
ChanSyncJoinVerdict chanSyncJoinFollow(uint8_t ch, uint8_t *outrankedBy) {
  if (outrankedBy) *outrankedBy = 0;
  if (!syncActive()) return CSJ_SYNC_OFF;
  if (!cs::validChannel(ch, s_maxCh)) return CSJ_OUT_OF_REGION;
  // Not inside the boot grace: until presence and the CHAN facts have landed (the boot solicit, WCB_WDP.cpp) no pinned
  // device that outranks this one could be seen, and a second JOIN board on another network would take the mesh - or
  // itself, for good - at every boot. WCB_WiFi.cpp holds its full-band scans until then too (chanSyncMayLeaveChannel).
  if (millis() < cs::BOOT_GRACE_MS || s_mode != M_IDLE) return CSJ_BUSY;
  // About to be pinned: the access point will own the channel. A pinned device it hears that outranks it keeps
  // the mesh - two networks on two channels cannot both be kept, and the lower rank keeps its own.
  const View v = look();
  cs::Key me = v.self;
  me.flags = (uint8_t)(me.flags | cs::F_PINNED);
  if (v.bestPinned.id && cs::better(v.bestPinned, me)) {
    if (outrankedBy) *outrankedBy = v.bestPinned.id;
    return CSJ_PINNED_OUTRANKED;
  }
  startLeadMove(ch, me.flags, "following this board's network", true);
  return CSJ_GO;
}

// ════════════════════════════════════════════════════════════════════════════
//  ?CHSYNC
// ════════════════════════════════════════════════════════════════════════════
static const char *stateName() {
  return s_mode == M_SEARCH ? "search" : (s_mode == M_MOVE ? "move" : "idle");
}

static void printStatus() {
  const View v = look();
  const unsigned long now = millis();
  const bool pin = (v.self.flags & cs::F_PINNED) != 0;
  Serial.println("------ Channel sync ------------------------------------");
  Serial.printf("Channel sync  : %s%s\n", s_enabled ? "ON" : "OFF (never moves, searches or answers)",
                (s_enabled && !etmEnabled) ? " - INACTIVE: it needs ETM (?ETM,ON)"
                : (s_enabled && !wdpEnabled) ? " - INACTIVE: it needs WDP (?WDP,ON)" : "");
  Serial.printf("Channel       : %u  (region %s, channels 1-%u)\n", s_home, s_appliedCc, s_maxCh);
  if (strcmp(s_savedCc, s_appliedCc) != 0)
    Serial.printf("Saved region  : %s - applies at the next boot\n", s_savedCc);
  Serial.printf("This board    : WCB%d, rank %u%s\n", WCB_Number, v.self.rank,
                pin ? ", PINNED (joined to its access point)" : "");
  Serial.printf("Leader        : WCB%u, rank %u%s%s\n", v.best.id, v.best.rank,
                (v.best.flags & cs::F_PINNED) ? ", pinned" : "", v.best.id == (uint8_t)WCB_Number ? " (this board)" : "");
  Serial.printf("Members       : %u heard, %u missing", v.heard, v.missing);
  if (v.missing) {
    Serial.print(" (");
    bool first = true;
    for (uint8_t id = 1; id <= MAX_WCB_COUNT; id++) {
      if (!memberOf(id) || wcbPeerOnline(id) || wcbPeerTemporary[id - 1] || keyOf(id).rank == cs::RANK_NEVER) continue;
      Serial.printf("%sWCB%u", first ? "" : " ", id);
      first = false;
    }
    Serial.print(")");
  }
  Serial.println();
  if (s_mode == M_SEARCH) {
    if (s_sr.phase == SP_HOP)
      Serial.printf("State         : searching (channel %u, %u of %u)\n",
                    s_sr.idx < s_sr.n ? s_sr.chans[s_sr.idx] : s_home, s_sr.idx + 1, s_sr.n);
    else
      Serial.println("State         : searching (pulling a group home)");
  } else if (s_mode == M_MOVE) {
    if (s_mv.restarting)
      Serial.printf("State         : moving to channel %u - restarting onto it\n", s_mv.ch);
    else if (s_mv.deferSince)
      Serial.printf("State         : moving to channel %u - waiting for this board to be free\n", s_mv.ch);
    else
      Serial.printf("State         : moving to channel %u in %ld ms (%s)\n", s_mv.ch,
                    (long)(s_mv.switchAt - now) > 0 ? (long)(s_mv.switchAt - now) : 0L, s_mv.why ? s_mv.why : "");
  } else {
    Serial.println("State         : idle");
  }
  if (s_mode == M_IDLE) {
    if (s_sched && s_attempt >= cs::SEARCH_ATTEMPTS)
      Serial.printf("Search        : all %u attempts made - ?CHSYNC,SEARCH to try again\n", (unsigned)cs::SEARCH_ATTEMPTS);
    else if (s_sched && s_condSince && (long)(s_nextDue - now) > 0)
      Serial.printf("Search        : attempt %u of %u in %lu s\n", s_attempt + 1, (unsigned)cs::SEARCH_ATTEMPTS,
                    (unsigned long)(s_nextDue - now) / 1000UL);
    else if (s_sched && s_condSince) {             // due: say what holds it, which nothing else prints ungated
      const char *why = nullptr;
      canSearchNow(&why);
      Serial.printf("Search        : attempt %u of %u due, held: %s\n", s_attempt + 1,
                    (unsigned)cs::SEARCH_ATTEMPTS, why ? why : "the next check (every 0.5 s)");
    } else if (s_sched)
      Serial.printf("Search        : not needed now (attempt %u of %u kept for %lu min in case it comes back)\n",
                    s_attempt + 1, (unsigned)cs::SEARCH_ATTEMPTS, TRIGGER_RESET_MS / 60000UL);
    else
      Serial.println("Search        : not needed (no known board missing, or another board leads)");
  }
  Serial.printf("[CHSYNC:EN=%d,CH=%u,MAXCH=%u,CC=%s,RANK=%u,PIN=%d,LEAD=%u,LRANK=%u,LPIN=%d,STATE=%s]\n",
                s_enabled ? 1 : 0, s_home, s_maxCh, s_appliedCc, v.self.rank, pin ? 1 : 0,
                v.best.id, v.best.rank, (v.best.flags & cs::F_PINNED) ? 1 : 0, stateName());
  Serial.println("--------------------------------------------------------");
}

static void operatorMove(const String &arg) {
  const int ch = arg.toInt();
  if (!s_enabled) { Serial.println("[CHSYNC] channel sync is OFF on this board - ?CHSYNC,ON first"); return; }
  if (!etmEnabled) { Serial.println("[CHSYNC] channel sync needs ETM (?ETM,ON) - every frame rides it"); return; }
  if (!wdpEnabled) { Serial.println("[CHSYNC] channel sync needs WDP (?WDP,ON) - it is how boards know who leads"); return; }
  if (chanSyncRadioAway()) {           // a JOIN full-band scan: the frames would go out on another channel
    Serial.println("[CHSYNC] the radio is on another channel (a WiFi scan) - try again in a moment");
    return;
  }
  if (ch < 1 || ch > s_maxCh) {                      // s_maxCh is capped at 13 (chanSyncApplyRegionAtBoot)
    Serial.printf("[CHSYNC] channel %d is outside region %s (channels 1-%u)\n", ch, s_appliedCc, s_maxCh);
    return;
  }
  if (ch == s_home) { Serial.printf("[CHSYNC] the mesh is already on channel %d\n", ch); return; }
  if (s_mode != M_IDLE) { Serial.printf("[CHSYNC] busy (%s) - try again in a moment\n", stateName()); return; }
  // A pinned device would stay on its access point's channel while the mesh went, and the next search would bring
  // everyone back to it. Refuse rather than start that.
  const View v = look();
  if (v.self.flags & cs::F_PINNED) {
    Serial.println("[CHSYNC] refused: this board is joined to an access point, which owns the channel. Move the "
                   "access point instead - the mesh follows it.");
    return;
  }
  if (v.bestPinned.id) {
    Serial.printf("[CHSYNC] refused: WCB%u is joined to an access point on this channel and would stay. Move that "
                  "access point instead - the mesh follows it.\n", v.bestPinned.id);
    return;
  }
  startLeadMove((uint8_t)ch, (uint8_t)(v.self.flags | cs::MF_OPERATOR), "operator move", false);
}

void processChanSyncCommand(const String &args) {
  String a = args;
  a.trim();
  String au = a;
  au.toUpperCase();
  if (au.length() == 0) { printStatus(); return; }
  if (au == "ON" || au == "OFF") {
    s_enabled = (au == "ON");
    saveEnabled();
    if (!s_enabled) {
      s_heldValid = false;
      if (s_mode == M_SEARCH) endSearch(nullptr);
      if (s_mode == M_MOVE && !s_mv.restarting) s_mode = M_IDLE;
      s_manualPending = false;
      s_replyArmed    = false;
      resetTrigger();
    }
    Serial.printf("[CHSYNC] channel sync %s\n", s_enabled ? "ON - this board moves with the mesh and searches for "
                                                            "missing boards"
                                                          : "OFF - this board never moves, searches or answers a search");
    return;
  }
  if (au == "SEARCH") {
    if (!s_enabled) { Serial.println("[CHSYNC] channel sync is OFF on this board - ?CHSYNC,ON first"); return; }
    if (!etmEnabled) { Serial.println("[CHSYNC] channel sync needs ETM (?ETM,ON) - every frame rides it"); return; }
    if (!wdpEnabled) { Serial.println("[CHSYNC] channel sync needs WDP (?WDP,ON) - it is how boards know who leads"); return; }
    if (s_mode != M_IDLE) { Serial.printf("[CHSYNC] busy (%s) - try again in a moment\n", stateName()); return; }
    // Only a group's leader searches, as the trigger has it (chansync::shouldSearch). A follower judging what it found
    // against itself can go to a group better than it but worse than its leader, alone, and leave its own group.
    const View lv = look();
    if (lv.best.id != (uint8_t)WCB_Number) {
      Serial.printf("[CHSYNC] refused: WCB%u (rank %u) leads this group - run ?CHSYNC,SEARCH there\n",
                    lv.best.id, lv.best.rank);
      return;
    }
    const char *why = nullptr;
    if (!wcbWifiCanHop(&why)) { Serial.printf("[CHSYNC] this board cannot leave the channel now: %s\n", why); return; }
    // Started from loop() once the command queue is quiet (this command is still in it, so not here).
    s_manualPending = true;
    s_manualSince   = millis();
    s_nextEval      = 0;
    Serial.println("[CHSYNC] searching as soon as the command queue is quiet");
    return;
  }
  if (au.startsWith("MOVE,")) { operatorMove(a.substring(5)); return; }
  Serial.println("Usage: ?CHSYNC | ?CHSYNC,ON | ?CHSYNC,OFF | ?CHSYNC,SEARCH | ?CHSYNC,MOVE,<channel>");
}

// ════════════════════════════════════════════════════════════════════════════
//  ?REGION
// ════════════════════════════════════════════════════════════════════════════
void processRegionCommand(const String &args) {
  String a = args;
  a.trim();
  a.toUpperCase();
  if (a.length() == 0) {
    Serial.printf("Region: %s, channels 1-%u\n", s_appliedCc, s_maxCh);
    if (strcmp(s_savedCc, s_appliedCc) != 0) Serial.printf("Saved for the next boot: %s\n", s_savedCc);
    return;
  }
  if (!regionKnown(a.c_str())) {
    Serial.printf("Unknown region \"%s\". Use 01 (world safe: channels 1-11, the default) or one of: %s\n",
                  a.c_str(), REGIONS + 3);
    return;
  }
  copyCc(s_savedCc, a.c_str());
  preferences.begin("wcb_config", false);
  preferences.putString("region", String(s_savedCc));
  preferences.end();
  Serial.printf("Region saved as %s - applies at the next boot (running now: %s, channels 1-%u).\n",
                s_savedCc, s_appliedCc, s_maxCh);
  if (strcmp(s_savedCc, "01") != 0)
    Serial.println("Set the same region on EVERY board and client. Whether channels 12-13 are legal where the droid "
                   "runs is the operator's call: WiFi devices in the US are limited to 1-11.");
}
