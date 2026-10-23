#pragma once
// ════════════════════════════════════════════════════════════════════════════
//  WCB_ResultWire.h — a mesh management RESULT too long for one relay reply, in
//  PARTS: ?MGMT,STATS, ?MGMT,ETM,CHAR and ?MGMT,SEQGET (one stored sequence).
//  Where a result is split, the part header, and the requester's checks and
//  clock (no Arduino, no radio). Design: docs/MGMT_RELAY.md §10.
//
//  PURE C++11 ON PURPOSE. Everything here is host-tested by
//  tests/result_parts_test.cpp, and a BYTE-IDENTICAL copy ships in WCB_Client
//  (src/WCB_ResultWire.h): the WCB firmware (target and relay), WcbMgmt and the
//  MgmtRelay example (relays) and WCB_Client::requestSequence (a requester) must
//  split, frame and join these parts the same way. Change both copies together.
//
//  One relay reply carries at most 16 frags x 182 = 2912 characters
//  (sendResultFrags). A ?STATS on a full fleet is up to ~6.6 KB, an ETM
//  characterization of 19 peers ~5.2 KB, a stored sequence "<key>,OK,<value>" up
//  to 4018 characters. So a result goes out like this:
//    - one that fits one reply goes out exactly as it always has, as packet type
//      9 / 10 / 16, to an old request and a new one alike;
//    - one that does not fit goes, ONLY to a requester that asked for parts, a
//      part per request as packet type 32 / 33 / 34 - each part its own reply;
//    - an old request for one that does not fit gets what it always got: the
//      "[ERROR] Result too large to relay" text (STATS, ETM) or TOOBIG (a
//      sequence), which every released relay prints (Greg, 2026-10-23: keep it).
//
//  A requester asks with the new request (30, or 31 for a sequence) THREE times
//  and the old one behind it: a target that predates parts drops the 59-byte
//  frame at its receive filter and answers the old request; a target with parts
//  answers the first new one and takes the old one for a copy of it (its dedup
//  window runs from the END of its answer).
//
//  A part's payload is
//      <id 4 hex>,<part>,<parts>,<len>,<crc 8 hex>:<data>
//  id names the result on the target (STATS and ETM are held there, as built,
//  while the requester pages through them; 0000 for a sequence, rebuilt from the
//  store for every part), part is 0-based, parts is how many there are, len is the
//  whole result's length in bytes and crc its CRC-32 (the config pull's). Joined,
//  the parts' data is exactly the text the one-reply form would carry: a relay
//  prints the same line(s) it always printed for it.
//
//  STATS and ETM are lines: a part ends just after a '\n' (every line is far
//  shorter than a part), so a relay prints each part as it comes, one write each,
//  and holds none of them - another task's print can only land between lines. A
//  sequence is one line: a relay joins it in one buffer of its exact length and
//  prints it with one write, as before.
// ════════════════════════════════════════════════════════════════════════════

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

namespace rparts {

// ---- Transport ------------------------------------------------------------
// The old types these ride beside (WCB.ino PACKET_TYPE_*).
static const uint8_t PT_STATS_REQ   = 7;    // 43 B, requester → target: ?STATS, one reply
static const uint8_t PT_ETM_REQ     = 8;    // 43 B: run ?ETM,CHAR, results in one reply
static const uint8_t PT_STATS_FRAG  = 9;    // 230 B frags, target → requester: the whole ?STATS
static const uint8_t PT_ETM_FRAG    = 10;   // the whole ETM characterization result
static const uint8_t PT_SEQVAL_REQ  = 15;   // 59 B: one sequence, one reply
static const uint8_t PT_SEQVAL_FRAG = 16;   // "<key>,<status>,<value>" whole
// The new ones. A released target drops a 59-byte frame of any type but 15 (and 28 since paged names) at its
// receive filter, and a released relay drops a 230-byte frag of a type it does not know.
static const uint8_t PT_RESULT_REQ      = 30;   // 59 B: start a STATS / ETM result in parts, or ask one part of it
static const uint8_t PT_SEQVAL_PART_REQ = 31;   // 59 B: one part of a stored sequence, by key
static const uint8_t PT_STATS_PART      = 32;   // 230 B frags: one part of a ?STATS
static const uint8_t PT_ETM_PART        = 33;   // one part of an ETM characterization result
static const uint8_t PT_SEQVAL_PART     = 34;   // one part of "<key>,OK,<value>"

// The request structs (59 bytes, the size of the single-sequence request on purpose): password[40], packetType,
// targetWCB, requesterWCB, then
//   RESULT_REQ:      kind, part, id (uint16, little-endian), 12 reserved bytes (zero)
//   SEQVAL_PART_REQ: part, key[15] (NUL-padded; a 15-character key fills it and has no NUL)
// Pinned with static_assert on every side.
static const size_t REQ_LEN      = 59;
static const size_t REQ_KIND_AT  = 43;
static const size_t REQ_PART_AT  = 44;
static const size_t REQ_ID_AT    = 45;
static const size_t SEQ_PART_AT  = 43;
static const size_t SEQ_KEY_AT   = 44;
static const size_t SEQ_KEY_LEN  = 15;    // SEQ_KEY_MAX_LEN, the NVS key limit

// What is being pulled. RESULT_REQ carries STATS or ETM; SEQVAL has its own request (it needs the key).
static const uint8_t KIND_STATS  = 1;
static const uint8_t KIND_ETM    = 2;
static const uint8_t KIND_SEQVAL = 3;

inline uint8_t partType(uint8_t kind) {
  return kind == KIND_STATS ? PT_STATS_PART : kind == KIND_ETM ? PT_ETM_PART : PT_SEQVAL_PART;
}
inline uint8_t wholeType(uint8_t kind) {
  return kind == KIND_STATS ? PT_STATS_FRAG : kind == KIND_ETM ? PT_ETM_FRAG : PT_SEQVAL_FRAG;
}
// The kind a 230-byte reply type belongs to, and whether it is a part; 0 for any other type.
inline uint8_t kindOfType(uint8_t type, bool &part) {
  part = type == PT_STATS_PART || type == PT_ETM_PART || type == PT_SEQVAL_PART;
  if (type == PT_STATS_FRAG || type == PT_STATS_PART)   return KIND_STATS;
  if (type == PT_ETM_FRAG   || type == PT_ETM_PART)     return KIND_ETM;
  if (type == PT_SEQVAL_FRAG || type == PT_SEQVAL_PART) return KIND_SEQVAL;
  return 0;
}
// STATS and ETM are lines, split after a '\n'; a sequence is split anywhere but inside a UTF-8 character.
inline bool kindIsLines(uint8_t kind) { return kind != KIND_SEQVAL; }

// ---- Sizes ----------------------------------------------------------------
static const unsigned REPLY_MAX     = 2912;   // one reply: MGMT_MAX_CHUNKS 16 x (CONFIG_PAYLOAD_SIZE - 1) 182
static const unsigned HEADER_MAX    = 26;     // "<id 4>,<part 2>,<parts 2>,<len 5>,<crc 8>:"
static const unsigned PART_DATA     = 2880;   // a part's data, at most (the default)
static const unsigned PART_DATA_MIN = 512;    // the smallest ?DEBUG,RESULTPART takes
static const unsigned MAX_PARTS     = 99;     // the header carries 2 digits; a requester refuses more
static const unsigned LEN_MAX       = 65535;  // a requester refuses a longer result
static const unsigned UTF8_BACKOFF  = 3;      // a UTF-8 character has at most 3 continuation bytes

static_assert(HEADER_MAX + PART_DATA <= REPLY_MAX, "a part (header + data) must fit one reply");
static_assert(PART_DATA_MIN > 2 * UTF8_BACKOFF + 1, "a part must always make progress");
// The largest results, so every part count fits the header: a ?STATS at the extreme (19 peers and a special peer,
// 20 reported rows, every counter 2^32-1) is 6587 bytes; in parts of PART_DATA_MIN, with a line of at most 174
// characters left over each time, that is at most ceil(6587 / (512 - 174)) = 20 parts.

// ---- Timing ---------------------------------------------------------------
// The first answer (one reply, or part 0) is given WAIT_FIRST_MS, as the one-reply request always was: a board that is
// not there costs no more than before. An ETM characterization runs 30-60 s or more before it answers, and the Wizard
// waits 120 s for it. A later part that brings no frag for RETRY_MS is asked again, at most PART_TRIES times: the target
// ignores a repeat of the same request for DEDUP_MS after it finished answering, and may start answering ~0.75 s after
// the request (busy sending another reply), so not shorter.
static const uint32_t WAIT_FIRST_MS     = 4000;
static const uint32_t WAIT_FIRST_ETM_MS = 180000;
static const uint32_t RETRY_MS          = 3000;
static const uint8_t  PART_TRIES        = 3;
static const uint8_t  RESTARTS_MAX      = 2;       // a sequence edited mid-pull: start over at most this often
static const uint32_t PULL_MAX_MS       = 60000;   // from part 0: a pull that has not finished by then is given up
// Target side.
static const uint32_t DEDUP_MS          = 1500;    // a repeat of the same request, from the end of the answer
static const uint32_t JOB_IDLE_MS       = 10000;   // a held STATS / ETM result nobody asked a part of for this long

// ---- CRC-32 ---------------------------------------------------------------
// Reflected, poly 0xEDB88320, init and final XOR 0xFFFFFFFF: the config pull's (WCB_ConfigParts.h) and zlib's.
static const uint32_t CRC_INIT = 0xFFFFFFFFu;
inline uint32_t crcUpdate(uint32_t crc, const void *data, size_t n) {
  const uint8_t *p = (const uint8_t *)data;
  for (size_t i = 0; i < n; i++) {
    crc ^= p[i];
    for (int j = 0; j < 8; j++) crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
  }
  return crc;
}

// ---- The text -------------------------------------------------------------
// A result as up to two pieces, so a sequence's "<key>,OK," and its value need not be copied into one buffer.
struct Text {
  const char *a;
  size_t      na;
  const char *b;
  size_t      nb;
  Text(const char *a_, size_t na_, const char *b_ = nullptr, size_t nb_ = 0) : a(a_), na(na_), b(b_), nb(nb_) {}
  size_t  len() const { return na + nb; }
  uint8_t at(size_t i) const { return (uint8_t)(i < na ? a[i] : b[i - na]); }
  // Copies bytes [lo, lo + n) into out.
  void copy(size_t lo, size_t n, char *out) const {
    if (lo < na) {
      const size_t k = (na - lo) < n ? (na - lo) : n;
      memcpy(out, a + lo, k);
      out += k;
      n -= k;
      lo = na;
    }
    if (n) memcpy(out, b + (lo - na), n);
  }
  uint32_t crc() const {
    uint32_t c = crcUpdate(CRC_INIT, a, na);
    if (nb) c = crcUpdate(c, b, nb);
    return ~c;
  }
};

// ---- The split ------------------------------------------------------------
inline bool isCont(uint8_t c) { return (c & 0xC0) == 0x80; }   // a UTF-8 continuation byte

// Where the part that starts at `start` ends (exclusive). The rest whole when it fits D bytes; else, for lines, just
// after the last '\n' in the next D bytes; else D bytes on, stepped back over at most 3 UTF-8 continuation bytes so no
// character is cut. Every part holds at least one byte, so the walk always ends.
inline size_t partEnd(const Text &t, size_t start, size_t D, bool lines) {
  const size_t L = t.len();
  if (start >= L) return L;
  if (L - start <= D) return L;
  size_t cut = start + D;
  if (lines) {
    for (size_t i = cut; i > start; i--)
      if (t.at(i - 1) == '\n') return i;
  }
  for (unsigned s = 0; s < UTF8_BACKOFF && cut > start + 1 && isCont(t.at(cut)); s++) cut--;
  return cut;
}

// How many parts t makes, or 0 when more than MAX_PARTS.
inline unsigned partCount(const Text &t, size_t D, bool lines) {
  if (D == 0) return 0;
  const size_t L = t.len();
  unsigned n = 0;
  size_t   at = 0;
  do {
    at = partEnd(t, at, D, lines);
    if (++n > MAX_PARTS) return 0;
  } while (at < L);
  return n;
}

// Bytes [lo, hi) of part `part`; false past the last part.
inline bool partRange(const Text &t, size_t D, bool lines, unsigned part, size_t &lo, size_t &hi) {
  if (D == 0) return false;
  const size_t L = t.len();
  size_t a = 0, b = partEnd(t, 0, D, lines);
  for (unsigned p = 0; p < part; p++) {
    if (b >= L) return false;
    a = b;
    b = partEnd(t, a, D, lines);
  }
  lo = a;
  hi = b;
  return true;
}

// Does a result of `len` bytes go whole (the old reply type)? Up to one reply; with a test part size (?DEBUG,RESULTPART
// below PART_DATA), up to that size, so a bench result splits.
inline bool fitsOneReply(size_t len, size_t D) { return len <= (D >= PART_DATA ? (size_t)REPLY_MAX : D); }

// ---- The part header ------------------------------------------------------
struct Header {
  uint16_t id;
  unsigned part, parts, len;
  uint32_t crc;
};

// "<id 4 hex>,<part>,<parts>,<len>,<crc 8 hex>:" into out; its length, or -1 when it does not fit.
inline int formatHeader(char *out, size_t cap, const Header &h) {
  const int n = snprintf(out, cap, "%04X,%u,%u,%u,%08lX:", (unsigned)h.id, h.part, h.parts, h.len, (unsigned long)h.crc);
  return (n < 0 || (size_t)n >= cap) ? -1 : n;
}

inline bool hexN(const char *&p, const char *end, unsigned n, uint32_t &v) {
  if ((size_t)(end - p) < n) return false;
  uint32_t r = 0;
  for (unsigned i = 0; i < n; i++) {
    const char c = p[i];
    unsigned d;
    if (c >= '0' && c <= '9') d = (unsigned)(c - '0');
    else if (c >= 'A' && c <= 'F') d = (unsigned)(c - 'A' + 10);
    else return false;   // upper-case only, as formatHeader writes it
    r = (r << 4) | d;
  }
  p += n;
  v = r;
  return true;
}

// 1-5 decimal digits, no sign, no leading zero but a lone 0, at most `max`.
inline bool decN(const char *&p, const char *end, unsigned &v, unsigned max) {
  unsigned long r = 0;
  int n = 0;
  const char *s = p;
  while (p < end && *p >= '0' && *p <= '9') {
    if (++n > 5) return false;
    r = r * 10 + (unsigned long)(*p - '0');
    p++;
  }
  if (n == 0 || r > max || (n > 1 && *s == '0')) return false;
  v = (unsigned)r;
  return true;
}

inline bool takeChar(const char *&p, const char *end, char c) {
  if (p >= end || *p != c) return false;
  p++;
  return true;
}

// A part's payload: its header, and where its data starts (just after the ':'). Strict: every field as formatHeader
// writes it, part < parts <= MAX_PARTS, len <= LEN_MAX.
inline bool parseHeader(const char *s, size_t n, Header &h, size_t &dataAt) {
  const char *p = s, *end = s + n;
  uint32_t id = 0, crc = 0;
  if (!hexN(p, end, 4, id) || !takeChar(p, end, ',')) return false;
  if (!decN(p, end, h.part, MAX_PARTS - 1) || !takeChar(p, end, ',')) return false;
  if (!decN(p, end, h.parts, MAX_PARTS) || !takeChar(p, end, ',')) return false;
  if (!decN(p, end, h.len, LEN_MAX) || !takeChar(p, end, ',')) return false;
  if (!hexN(p, end, 8, crc) || !takeChar(p, end, ':')) return false;
  if (h.parts == 0 || h.part >= h.parts) return false;
  h.id  = (uint16_t)id;
  h.crc = crc;
  dataAt = (size_t)(p - s);
  return true;
}

// ---- Reassembly (requester) -----------------------------------------------
// One per pull. Parse each part's header (parseHeader), check() it; for part 0 start(); then take() its data. The
// assembly counts and checksums; where the data goes (printed at once, or copied into a buffer) is the host's business.
enum Step : uint8_t {
  STEP_OK,        // check(): the part asked for, go on
  STEP_STALE,     // not the part asked for (a late copy): ignore it, keep waiting
  STEP_RESTART,   // built from another result (a sequence edited mid-pull, or a held result replaced): start over
  STEP_BAD,       // malformed or out of bounds: ignore it, keep waiting
  STEP_NEXT,      // take(): part taken, ask for `next`
  STEP_DONE,      // take(): the whole result is in, and its CRC is right
};

struct Assembly {
  Header   first;    // part 0's header (valid once next > 0)
  unsigned next;     // the part asked for
  size_t   got;      // bytes taken
  uint32_t crcRun;

  Assembly() { reset(); }
  void reset() {
    first.id = 0;
    first.part = first.parts = first.len = 0;
    first.crc = 0;
    next = 0;
    got  = 0;
    crcRun = CRC_INIT;
  }

  Step check(const Header &h) const {
    if (h.parts == 0 || h.parts > MAX_PARTS || h.part >= h.parts || h.len > LEN_MAX) return STEP_BAD;
    if (next == 0) return h.part == 0 ? STEP_OK : STEP_STALE;
    if (h.id != first.id || h.parts != first.parts || h.len != first.len || h.crc != first.crc) return STEP_RESTART;
    return h.part == next ? STEP_OK : STEP_STALE;
  }

  void start(const Header &h) {
    first  = h;
    next   = 0;
    got    = 0;
    crcRun = CRC_INIT;
  }

  // Part `next`'s data, n bytes. NEXT, DONE, BAD (an empty part of a non-empty result: ignore it), or RESTART when the
  // parts do not add up to part 0's len, or the whole does not match its CRC.
  Step take(const char *data, size_t n) {
    if (n == 0 && first.len > 0) return STEP_BAD;
    if (got + n > first.len) return STEP_RESTART;
    const bool last = next + 1 >= first.parts;
    if (!last && got + n >= first.len) return STEP_RESTART;   // parts remain, but no bytes for them
    const uint32_t c = crcUpdate(crcRun, data, n);
    if (last && (got + n != first.len || ~c != first.crc)) return STEP_RESTART;
    crcRun = c;
    got += n;
    next++;
    return last ? STEP_DONE : STEP_NEXT;
  }
};

// ---- The requester's clock ------------------------------------------------
// One pull, timed the same way on every requester. The host sends what tick() asks for and calls heard() for every
// frag of the part it waits for. Before part 0 the pull waits for the first answer (one reply or part 0) and ends in
// silence without one, as the one-reply request always did; after it, a part with no progress is asked again.
struct Clock {
  uint8_t  kind;           // 0 = idle
  uint8_t  target;
  uint8_t  tries;          // requests of the part asked for
  uint8_t  restarts;
  bool     paging;         // part 0 is in (or the pull restarted): a part is asked again, not given up in silence
  uint32_t askedMs, heardMs, pagingMs, firstWaitMs;

  Clock() { stop(); }
  void stop() {
    kind = target = tries = restarts = 0;
    paging = false;
    askedMs = heardMs = pagingMs = firstWaitMs = 0;
  }
  bool active() const { return kind != 0; }
  void begin(uint8_t k, uint8_t t, uint32_t now) {
    stop();
    kind = k;
    target = t;
    tries = 1;
    askedMs = heardMs = now;
    firstWaitMs = k == KIND_ETM ? WAIT_FIRST_ETM_MS : WAIT_FIRST_MS;
  }
  void heard(uint32_t now) { heardMs = now; }
  // Part 0 is in: from here a part is asked again, and the whole pull has PULL_MAX_MS.
  void startPaging(uint32_t now) {
    if (!paging) pagingMs = now;
    paging = true;
  }
  // The next part (or part 0 again, after a restart) is asked now.
  void asked(uint32_t now) {
    tries = 1;
    askedMs = now;
  }

  enum Tick : uint8_t {
    T_WAIT,      // nothing to do
    T_ASK,       // ask the part again (then call again(now))
    T_SILENT,    // no first answer: end the pull, print nothing (a board that is not there)
    T_NEVER,     // a part asked PART_TRIES times never came: end it and say so
    T_TIMEOUT,   // PULL_MAX_MS since part 0: end it and say so
  };
  Tick tick(uint32_t now) const {
    if (!kind) return T_WAIT;
    const uint32_t last = (int32_t)(heardMs - askedMs) > 0 ? heardMs : askedMs;
    if (!paging) return (now - last >= firstWaitMs) ? T_SILENT : T_WAIT;
    if (now - pagingMs >= PULL_MAX_MS) return T_TIMEOUT;
    if (now - last < RETRY_MS) return T_WAIT;
    return tries >= PART_TRIES ? T_NEVER : T_ASK;
  }
  void again(uint32_t now) {
    tries++;
    askedMs = now;
  }
};

}  // namespace rparts
