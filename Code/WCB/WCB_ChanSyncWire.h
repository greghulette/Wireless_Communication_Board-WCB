#pragma once
// ════════════════════════════════════════════════════════════════════════════
//  WCB_ChanSyncWire.h — channel sync: wire format, leader ordering, and the
//  pure decisions (no Arduino, no radio). Design: docs/CHANNEL_SYNC.md.
//
//  PURE C++11 ON PURPOSE. Everything here is host-tested by
//  tests/chansync_wire_test.cpp, and a BYTE-IDENTICAL copy ships in WCB_Client
//  (src/WCB_ChanSyncWire.h), because the WCB firmware and every WCB_Client host
//  (NaviCore) must encode, decode and judge these frames the same way. Change
//  both copies together; the test pins the bytes.
//
//  The radio has one channel (docs/WIFI_DESIGN.md §2), so the whole mesh must
//  agree on it. Channel sync keeps it agreed when it moves: the device that
//  should lead announces a MOVE to everyone it can hear, and a local leader that
//  misses a known board searches the other channels for it (PROBE / REPLY).
// ════════════════════════════════════════════════════════════════════════════

#include <stdint.h>
#include <string.h>

namespace chansync {

// ---- Transport ------------------------------------------------------------
// ETM packet types (espnow_struct_message_etm.structPacketType), payload in
// structCommand[200]. Released WCB firmware and WCB_Client drop an ETM packet
// type they do not know (the fall-through return of the ETM receive path; the
// client's packet switch has no default), so adding these breaks no older peer.
static const uint8_t PT_MOVE        = 25;   // "the mesh moves to channel N", broadcast
static const uint8_t PT_PROBE       = 26;   // a searching board asks "who is on this channel?", broadcast
static const uint8_t PT_PROBE_REPLY = 27;   // the answer, broadcast (the prober is not a registered peer here)

static const uint8_t MAGIC   = 'C';
static const uint8_t VERSION = 0x01;

// WDP TLV carrying a device's channel facts. Unknown TLVs are skipped by length,
// so older peers ignore it (docs/WDP_DESIGN.md §3).
static const uint8_t TLV_CHAN     = 0x14;
static const uint8_t TLV_CHAN_LEN = 5;      // [channel][rank][flags][cc0][cc1]; a longer value is accepted (future fields)

// ---- Device flags (TLV, MOVE, PROBE, REPLY) ---------------------------------
static const uint8_t F_PINNED = 0x01;  // associated with an access point: the AP owns its channel, nobody here can move it
static const uint8_t F_SYNC   = 0x02;  // takes part in channel sync (?CHSYNC,ON / setChanSyncEnabled(true))
// MOVE only
static const uint8_t MF_OPERATOR = 0x80;  // an operator's ?CHSYNC,MOVE: obeyed whoever sent it (a pinned follower still refuses)

// ---- Rank: who leads ----------------------------------------------------------
// Lower leads. A WCB's rank is its number (1-20). NaviCore sets 0. Other
// WCB_Client devices default to RANK_CLIENT_BASE + id. RANK_NEVER never leads
// (temporary peers, management relays) — it still follows.
static const uint8_t RANK_NAVICORE    = 0;
static const uint8_t RANK_CLIENT_BASE = 100;
static const uint8_t RANK_NEVER       = 255;

static const uint8_t MAX_CHANNEL_CAP = 13;   // channel 14 is never used (Japan only, 802.11b only)

// ---- Timings both sides share ------------------------------------------------
static const uint16_t MOVE_COUNTDOWN_MS   = 1500;  // a MOVE's first frame: switch this long after it
static const uint8_t  MOVE_REPEATS        = 5;     // frames per MOVE (broadcast is unacknowledged)
static const uint16_t MOVE_REPEAT_GAP_MS  = 300;   // between them; each carries the time still left
static const uint16_t PULL_COUNTDOWN_MS   = 600;   // a MOVE sent on a FOREIGN channel by a visiting leader
static const uint8_t  PULL_REPEATS        = 3;
static const uint16_t PULL_REPEAT_GAP_MS  = 100;
static const uint16_t PROBE_DWELL_MS      = 150;   // time on each visited channel, collecting replies
static const uint8_t  REPLY_JITTER_STEP_MS = 3;    // a reply waits (id % 16) * this, so a group does not answer in lockstep
static const uint32_t BOOT_GRACE_MS       = 40000; // no search decision before this long after boot (presence must settle)
static const uint32_t TRIGGER_STABLE_MS   = 10000; // the search condition must hold this long before the first attempt
static const uint32_t MOVE_DEFER_MAX_MS   = 30000; // a busy board (OTA, config pull, queue) applies a MOVE at most this late
static const uint8_t  SEARCH_ATTEMPTS     = 7;     // per trigger; then it stops until reboot, ?CHSYNC,SEARCH, or the condition clears

// Minutes from the trigger to search attempt n (0-based): 0, 1, 2, 4, 8, 16, 32
// -> seven attempts over about an hour. Returns 0xFFFFFFFF past the last one.
inline uint32_t searchAttemptOffsetMs(uint8_t n) {
  static const uint8_t mins[SEARCH_ATTEMPTS] = { 0, 1, 2, 4, 8, 16, 32 };
  if (n >= SEARCH_ATTEMPTS) return 0xFFFFFFFFUL;
  return (uint32_t)mins[n] * 60000UL;
}

// ---- Leader ordering -----------------------------------------------------------
struct Key {
  uint8_t id;      // WCB number / client device id (1-20); 0 = none
  uint8_t rank;
  uint8_t flags;   // F_PINNED / F_SYNC
};

inline Key noKey() { Key k; k.id = 0; k.rank = RANK_NEVER; k.flags = 0; return k; }

// True when a should lead over b. In order:
//   1. a device that can lead beats one that cannot (RANK_NEVER),
//   2. a PINNED device beats an unpinned one — an access point's channel cannot
//      be moved from here, so the mesh has to go to it, whatever the ranks say,
//   3. the lower rank,
//   4. the lower id (two devices claiming one rank; a duplicate number).
inline bool better(const Key &a, const Key &b) {
  if (a.id == 0) return false;
  if (b.id == 0) return true;
  const bool la = a.rank != RANK_NEVER, lb = b.rank != RANK_NEVER;
  if (la != lb) return la;
  const bool pa = (a.flags & F_PINNED) != 0, pb = (b.flags & F_PINNED) != 0;
  if (pa != pb) return pa;
  if (a.rank != b.rank) return a.rank < b.rank;
  return a.id < b.id;
}

inline bool sameKey(const Key &a, const Key &b) { return a.id == b.id && a.rank == b.rank && a.flags == b.flags; }

inline bool validChannel(uint8_t ch, uint8_t maxCh) {
  if (maxCh > MAX_CHANNEL_CAP) maxCh = MAX_CHANNEL_CAP;
  return ch >= 1 && ch <= maxCh;
}

// ---- Little-endian helpers ---------------------------------------------------
inline void putU16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)(v & 0xFF); p[1] = (uint8_t)(v >> 8); }
inline uint16_t getU16(const uint8_t *p)   { return (uint16_t)(p[0] | ((uint16_t)p[1] << 8)); }

// Country code bytes on the wire: two characters, [0-9A-Z]; anything else reads as '?'.
inline char ccChar(uint8_t c) { return ((c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z')) ? (char)c : '?'; }

// ---- WDP TLV 0x14 CHAN -----------------------------------------------------------
struct ChanFacts {
  uint8_t channel;   // the channel this device is on now
  uint8_t rank;
  uint8_t flags;     // F_PINNED / F_SYNC
  char    cc[3];     // region, e.g. "01" (world safe, 1-11), "US", "DE"; NUL-terminated
};

// Append the TLV at buf[o]; returns the new offset (unchanged when it does not fit).
inline int putChanTlv(uint8_t *buf, int o, int max, const ChanFacts &f) {
  if (o < 0 || o + 2 + TLV_CHAN_LEN > max) return o;
  buf[o++] = TLV_CHAN;
  buf[o++] = TLV_CHAN_LEN;
  buf[o++] = f.channel;
  buf[o++] = f.rank;
  buf[o++] = f.flags;
  buf[o++] = (uint8_t)f.cc[0];
  buf[o++] = (uint8_t)f.cc[1];
  return o;
}

// Decode a CHAN TLV value (v, len). False on a short value.
inline bool parseChanTlv(const uint8_t *v, uint8_t len, ChanFacts &out) {
  if (!v || len < TLV_CHAN_LEN) return false;
  out.channel = v[0];
  out.rank    = v[1];
  out.flags   = v[2];
  out.cc[0]   = ccChar(v[3]);
  out.cc[1]   = ccChar(v[4]);
  out.cc[2]   = '\0';
  return true;
}

// ---- Frames (structCommand[200]) -----------------------------------------------------
// MOVE   [0]'C' [1]ver [2]newChannel [3]senderRank [4]flags(F_* | MF_OPERATOR) [5..6]moveId LE [7..8]delayMs LE [9]cc0 [10]cc1
// PROBE  [0]'C' [1]ver [2]homeChannel [3]proberRank [4]proberFlags [5..6]nonce LE
// REPLY  [0]'C' [1]ver [2]channel [3..4]nonce LE [5]responderRank [6]responderFlags [7]bestId [8]bestRank [9]bestFlags [10]cc0 [11]cc1
// The SENDER of every frame is structSenderID, which the receive path has already
// bound to the source MAC (docs/WDP_DESIGN.md §8) — no id travels in the payload
// except a REPLY's bestId.
static const int MOVE_LEN  = 11;
static const int PROBE_LEN = 7;
static const int REPLY_LEN = 12;

struct Move {
  uint8_t  newChannel;
  uint8_t  rank;       // the sender's
  uint8_t  flags;      // the sender's F_*, plus MF_OPERATOR
  uint16_t moveId;     // random per move; a follower acts on the first frame of a moveId and ignores the rest
  uint16_t delayMs;    // from THIS frame to the switch
  char     cc[3];
};

struct Probe {
  uint8_t  homeChannel;
  uint8_t  rank;
  uint8_t  flags;
  uint16_t nonce;
};

struct Reply {
  uint8_t  channel;
  uint16_t nonce;
  uint8_t  rank;       // the responder's
  uint8_t  flags;
  Key      best;       // the best device the responder hears right now, itself included
  char     cc[3];
};

inline bool header(const uint8_t *b, int len, int need) {
  return b && len >= need && b[0] == MAGIC && b[1] == VERSION;
}

inline int encodeMove(uint8_t *b, int max, const Move &m) {
  if (!b || max < MOVE_LEN) return 0;
  b[0] = MAGIC; b[1] = VERSION; b[2] = m.newChannel; b[3] = m.rank; b[4] = m.flags;
  putU16(b + 5, m.moveId); putU16(b + 7, m.delayMs);
  b[9] = (uint8_t)m.cc[0]; b[10] = (uint8_t)m.cc[1];
  return MOVE_LEN;
}
inline bool decodeMove(const uint8_t *b, int len, Move &m) {
  if (!header(b, len, MOVE_LEN)) return false;
  m.newChannel = b[2]; m.rank = b[3]; m.flags = b[4];
  m.moveId = getU16(b + 5); m.delayMs = getU16(b + 7);
  m.cc[0] = ccChar(b[9]); m.cc[1] = ccChar(b[10]); m.cc[2] = '\0';
  return true;
}

inline int encodeProbe(uint8_t *b, int max, const Probe &p) {
  if (!b || max < PROBE_LEN) return 0;
  b[0] = MAGIC; b[1] = VERSION; b[2] = p.homeChannel; b[3] = p.rank; b[4] = p.flags;
  putU16(b + 5, p.nonce);
  return PROBE_LEN;
}
inline bool decodeProbe(const uint8_t *b, int len, Probe &p) {
  if (!header(b, len, PROBE_LEN)) return false;
  p.homeChannel = b[2]; p.rank = b[3]; p.flags = b[4]; p.nonce = getU16(b + 5);
  return true;
}

inline int encodeReply(uint8_t *b, int max, const Reply &r) {
  if (!b || max < REPLY_LEN) return 0;
  b[0] = MAGIC; b[1] = VERSION; b[2] = r.channel; putU16(b + 3, r.nonce);
  b[5] = r.rank; b[6] = r.flags; b[7] = r.best.id; b[8] = r.best.rank; b[9] = r.best.flags;
  b[10] = (uint8_t)r.cc[0]; b[11] = (uint8_t)r.cc[1];
  return REPLY_LEN;
}
inline bool decodeReply(const uint8_t *b, int len, Reply &r) {
  if (!header(b, len, REPLY_LEN)) return false;
  r.channel = b[2]; r.nonce = getU16(b + 3); r.rank = b[5]; r.flags = b[6];
  r.best.id = b[7]; r.best.rank = b[8]; r.best.flags = b[9];
  r.cc[0] = ccChar(b[10]); r.cc[1] = ccChar(b[11]); r.cc[2] = '\0';
  return true;
}

// ---- Decisions ------------------------------------------------------------------
enum MoveVerdict {
  MV_ACCEPT = 0,
  MV_SYNC_OFF,       // this device does not take part
  MV_BAD_CHANNEL,    // outside this device's region (or 0 / > 13)
  MV_SAME_CHANNEL,   // already there
  MV_PINNED_SELF,    // this device is associated with an AP; it leaves only for a device that outranks it
  MV_OUTRANKED,      // a device this one hears outranks the sender: the sender is not its leader
};

// Should a device obey a MOVE?
//   sender      the sender's key (id from the frame's MAC-bound sender, rank/flags from the frame)
//   self        this device's key
//   bestPresent the best device this one hears now, ITSELF INCLUDED
inline MoveVerdict judgeMove(const Move &m, const Key &sender, const Key &self, const Key &bestPresent,
                             uint8_t myChannel, uint8_t myMaxCh, bool syncOn) {
  if (!syncOn) return MV_SYNC_OFF;
  if (!validChannel(m.newChannel, myMaxCh)) return MV_BAD_CHANNEL;
  if (m.newChannel == myChannel) return MV_SAME_CHANNEL;
  if (self.flags & F_PINNED) {
    // An operator move never takes an associated board off its AP; a pinned
    // sender that outranks it can (two networks on two channels: one has to give).
    if (m.flags & MF_OPERATOR) return MV_PINNED_SELF;
    if (!better(sender, self)) return MV_PINNED_SELF;
    return MV_ACCEPT;
  }
  if (m.flags & MF_OPERATOR) return MV_ACCEPT;
  if (bestPresent.id != sender.id && better(bestPresent, sender)) return MV_OUTRANKED;
  return MV_ACCEPT;
}

// A board searches only when it is its group's leader (nobody it hears outranks
// it) and a board it knows of is missing. Followers never search: their leader
// does it for the group and they follow its MOVE. Alone counts as leader.
inline bool shouldSearch(const Key &self, const Key &bestPresent, bool memberMissing, bool syncOn, bool canHop) {
  if (!syncOn || !canHop || !memberMissing) return false;
  if (self.rank == RANK_NEVER) return false;            // never leads, so never searches for the group
  return !(bestPresent.id != self.id && better(bestPresent, self));
}

// What a finished search does. `found` holds, per visited channel, the best leader
// any reply on that channel named (id 0 = nobody answered there).
struct SearchResult {
  uint8_t moveTo;          // nonzero: a better leader is on this channel — announce a MOVE at home and go
  uint8_t pull[MAX_CHANNEL_CAP + 1];   // pull[ch] = 1: a worse group is on ch — visit it and send it a MOVE home
  uint8_t pullCount;
};

inline SearchResult judgeSearch(const Key &self, uint8_t homeChannel, const Key found[MAX_CHANNEL_CAP + 1]) {
  SearchResult r;
  memset(&r, 0, sizeof(r));
  Key bestFound = noKey();
  uint8_t bestCh = 0;
  for (uint8_t ch = 1; ch <= MAX_CHANNEL_CAP; ch++) {
    if (ch == homeChannel || found[ch].id == 0) continue;
    if (better(found[ch], bestFound)) { bestFound = found[ch]; bestCh = ch; }
  }
  if (bestCh && better(bestFound, self)) {
    r.moveTo = bestCh;      // everything else will find that group too: it is the best one heard
    return r;
  }
  for (uint8_t ch = 1; ch <= MAX_CHANNEL_CAP; ch++) {
    if (ch == homeChannel || found[ch].id == 0) continue;
    if (better(self, found[ch])) { r.pull[ch] = 1; r.pullCount++; }
  }
  return r;
}

}  // namespace chansync
