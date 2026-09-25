#pragma once

// ════════════════════════════════════════════════════════════════
//  WCB Config Parts — split and framing arithmetic for the mesh config pull
//
//  A relayed pull (?MGMT,PULL,<n>) reaches the requester through ONE relay reassembly
//  session, and a session holds at most 16 frags x 182 bytes = 2912 characters: every
//  released relay rejects totalChunks > 16 (its receive masks are 16-bit) and prints into a
//  2960-byte output slot. So:
//    - a reply of 2912 characters or less goes out exactly as it always has: packet type 6,
//      one session, no framing (older Wizards store ANY non-empty [MGMT:CONFIG,n] body as the
//      board's config, so nothing new may ever travel as type 6);
//    - a longer reply goes out ONLY to a request that asked for parts (type 19, sent by a new
//      relay for ?MGMT,PULL,<n>,P), as K parts, each its own type-18 session:
//          P<id>,<k>,<K>:<data>~       id = 4 upper-case hex digits, random per job; k 1-based
//    - anything that cannot be sent goes out as one type-18 error of one chunk:
//          E<CODE>,<detail>            ASCII, never any config text
//
//  The target never holds the whole reply. It walks collectConfigCommands once to measure it
//  (length, body CRC, and the bytes around each split point), then once per message, copying
//  only that message's window into a buffer of at most 2912 bytes. CfgpWalk is that walk: fed
//  the virtual stream "[VER:<fw>]" + body + "^?CHK<8 hex>" a piece at a time, it counts, runs
//  the CRC over the body, records the split-point bytes and copies a window.
//
//  Split points are grid-anchored: split_k = back(k*D), where back() steps down over UTF-8
//  continuation bytes, at most 3. Anchoring each split to the grid, not to the previous split,
//  keeps K = ceil(L/D) exact whatever the text holds, and every part within D-3..D+3 bytes.
//  Splitting on '^?' token boundaries is NOT an option: a stored sequence value can hold '^?'.
//
//  Pure arithmetic: no Arduino, no String, no heap. tests/config_parts_test.cpp includes this
//  header as it is and runs it on the host (CI: .github/workflows/wdp-tests.yml). WCB.ino
//  static_asserts the constants that mirror its own (CONFIG_PAYLOAD_SIZE, MGMT_MAX_CHUNKS,
//  MGMT_OUT_BUFSZ), so the two cannot drift.
// ════════════════════════════════════════════════════════════════

#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>

// ── Sizes ─────────────────────────────────────────────────────────
static const size_t   CFGP_CHUNK_BYTES   = 182;    // payload bytes per frag: CONFIG_PAYLOAD_SIZE - 1 (the last stays NUL)
static const unsigned CFGP_MAX_CHUNKS    = 16;     // MGMT_MAX_CHUNKS: relays reject more (uint16_t receive masks)
static const size_t   CFGP_MSG_MAX       = CFGP_CHUNK_BYTES * CFGP_MAX_CHUNKS;   // 2912: one relay session
static const size_t   CFGP_LEGACY_MAX    = CFGP_MSG_MAX;   // a reply this long or shorter goes out whole, as type 6
static const size_t   CFGP_PART_DATA     = 2880;   // default part data size D (and the largest ?DEBUG,PULLPART takes)
static const size_t   CFGP_PART_DATA_MIN = 512;    // smallest D: the WIFI,AP|JOIN passphrase and EPASS sit in the
                                                   // chain's first ~300 characters, so they stay whole inside part 1
static const unsigned CFGP_MAX_PARTS     = 16;     // K above this is ERROR TOOBIG
static const unsigned CFGP_UTF8_BACKOFF  = 3;      // a UTF-8 character has at most 3 continuation bytes
static const size_t   CFGP_PART_HDR_MAX  = 12;     // "P" + 4 hex + "," + "16" + "," + "16" + ":"
static const size_t   CFGP_PART_MSG_MAX  = CFGP_PART_HDR_MAX + CFGP_PART_DATA + CFGP_UTF8_BACKOFF + 1;   // + '~' = 2896
static const size_t   CFGP_ERR_MAX       = CFGP_CHUNK_BYTES;   // an error is exactly one chunk

static_assert(CFGP_PART_MSG_MAX <= CFGP_MSG_MAX, "a part (header + D+3 data bytes + '~') must fit one relay session");
static_assert(CFGP_PART_DATA_MIN > CFGP_UTF8_BACKOFF + 1, "grid windows must not overlap");
static_assert((unsigned long)CFGP_MAX_PARTS * CFGP_PART_DATA < 65536UL, "split points are stored as uint16_t");

// ── CRC-32 ────────────────────────────────────────────────────────
// Reflected CRC-32, poly 0xEDB88320: the same function as WCB.ino's crc32Update, which the
// ^?CHK checksum and every verifier (Wizard, harness) already use. Start at CFGP_CRC_INIT, feed
// every byte in order, finish with ~crc.
static const uint32_t CFGP_CRC_INIT = 0xFFFFFFFFu;
static inline uint32_t cfgpCrc32Update(uint32_t crc, const uint8_t *p, size_t n) {
  for (size_t i = 0; i < n; i++) {
    crc ^= p[i];
    for (int j = 0; j < 8; j++) crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
  }
  return crc;
}

// ── The walk ──────────────────────────────────────────────────────
// Positions are byte offsets into the virtual reply stream [0, L).
struct CfgpWalk {
  size_t   len;                         // bytes of the stream fed so far (L once the walk ends)
  uint32_t crcRun;                      // running CRC-32 over the body bytes
  uint32_t crc;                         // the finished body CRC; set by cfgpReplyEnd
  size_t   gridD;                       // record the bytes around every multiple of gridD; 0 = don't
  uint8_t  grid[CFGP_MAX_PARTS][4];     // grid[k-1] = the bytes at k*D-3 .. k*D
  uint8_t *dst;                         // copy the window [lo, hi) here (hi - lo bytes); nullptr = don't
  size_t   lo, hi;
  bool     tokenFed;                    // a token has been fed: the next one is joined by "^?"
  unsigned lostTokens;                  // tokens that came in missing or empty (cfgpReplyToken)
};

static inline void cfgpWalkBegin(CfgpWalk &w, size_t gridD, uint8_t *dst, size_t lo, size_t hi) {
  memset(&w, 0, sizeof(w));
  w.crcRun = CFGP_CRC_INIT;
  w.gridD  = gridD;
  w.dst    = dst;
  w.lo     = lo;
  w.hi     = hi;
}

// Feed the next n bytes of the stream. body: the bytes belong to the checksummed body.
static inline void cfgpWalkFeed(CfgpWalk &w, const void *data, size_t n, bool body) {
  if (!data || n == 0) return;
  const uint8_t *p  = (const uint8_t *)data;
  const size_t   lo = w.len, hi = w.len + n;                  // this piece is stream bytes [lo, hi)
  if (body) w.crcRun = cfgpCrc32Update(w.crcRun, p, n);
  if (w.gridD) {
    size_t k = lo / w.gridD;                                  // no earlier window can reach lo
    if (k < 1) k = 1;
    for (; k <= CFGP_MAX_PARTS; k++) {
      const size_t ws = k * w.gridD - CFGP_UTF8_BACKOFF;      // window k is [k*D-3, k*D]
      const size_t we = k * w.gridD + 1;
      if (ws >= hi) break;
      const size_t a = ws > lo ? ws : lo, b = we < hi ? we : hi;
      for (size_t q = a; q < b; q++) w.grid[k - 1][q - ws] = p[q - lo];
    }
  }
  if (w.dst && w.lo < hi && w.hi > lo) {
    const size_t a = w.lo > lo ? w.lo : lo, b = w.hi < hi ? w.hi : hi;
    memcpy(w.dst + (a - w.lo), p + (a - lo), b - a);
  }
  w.len = hi;
}

// The reply's framing, byte for byte what the pre-F13 buildConfigString() made:
//   "[VER:" <fw> "]"  "?" <token 1>  "^?" <token 2> ...  "^?CHK" <%08X of the body CRC>
// The body is everything between "]" and "^?CHK". An empty config's body is empty (CRC 0).
static inline void cfgpReplyBegin(CfgpWalk &w, const char *ver, size_t verLen) {
  cfgpWalkFeed(w, "[VER:", 5, false);
  cfgpWalkFeed(w, ver, verLen, false);
  cfgpWalkFeed(w, "]", 1, false);
}
// A token that comes in missing or empty is counted in lostTokens and feeds nothing. No config
// command is empty, so such a token was an Arduino String whose allocation failed: core 3.3.4
// invalidates it (c_str() nullptr, length 0) instead of failing the call. Fed through, the reply
// would go out without that command under a ^?CHK that agrees with it (tracker #90) - or, lost in
// one walk only, as a CRC mismatch misreported as "config changed". The caller treats a walk with
// lostTokens as out of memory.
static inline void cfgpReplyToken(CfgpWalk &w, const char *tok, size_t n) {
  if (!tok || n == 0) { w.lostTokens++; return; }
  if (w.tokenFed) cfgpWalkFeed(w, "^?", 2, true);
  else            cfgpWalkFeed(w, "?", 1, true);
  w.tokenFed = true;
  cfgpWalkFeed(w, tok, n, true);
}
static inline void cfgpReplyEnd(CfgpWalk &w) {
  w.crc = ~w.crcRun;
  char chk[16];
  snprintf(chk, sizeof(chk), "^?CHK%08lX", (unsigned long)w.crc);
  cfgpWalkFeed(w, chk, 13, false);
}

// ── Splitting ─────────────────────────────────────────────────────
static inline bool cfgpIsCont(uint8_t b) { return (b & 0xC0) == 0x80; }   // UTF-8 continuation byte

// back(x): step x down while the byte at x is a continuation byte, at most 3 steps. None of
// the 4 bytes a lead byte (not UTF-8): x unchanged. win = the bytes at x-3, x-2, x-1, x.
static inline size_t cfgpBackOff(size_t x, const uint8_t win[4]) {
  for (unsigned s = 0; s <= CFGP_UTF8_BACKOFF && s <= x; s++)
    if (!cfgpIsCont(win[CFGP_UTF8_BACKOFF - s])) return x - s;
  return x;
}

static inline size_t cfgpPartCount(size_t L, size_t D) { return D ? (L + D - 1) / D : 0; }

// Fills split[0..K] (split[0] = 0, split[K] = L) from a measure walk's grid. Returns K, or 0
// when K would exceed CFGP_MAX_PARTS (ERROR TOOBIG). Part k is stream bytes [split[k-1], split[k]).
static inline unsigned cfgpSplit(size_t L, size_t D, const uint8_t (*grid)[4], uint16_t *split) {
  const size_t K = cfgpPartCount(L, D);
  if (K == 0 || K > CFGP_MAX_PARTS) return 0;
  split[0] = 0;
  for (size_t k = 1; k < K; k++) split[k] = (uint16_t)cfgpBackOff(k * D, grid[k - 1]);
  split[K] = (uint16_t)L;
  return (unsigned)K;
}

// D for a requested ?DEBUG,PULLPART value: 0 = the default, 512..2880 as given, else 0 (invalid).
static inline size_t cfgpPartDataSize(long req) {
  if (req == 0) return CFGP_PART_DATA;
  if (req >= (long)CFGP_PART_DATA_MIN && req <= (long)CFGP_PART_DATA) return (size_t)req;
  return 0;
}

// ── What to send ──────────────────────────────────────────────────
enum : uint8_t { CFGP_PLAN_LEGACY = 0, CFGP_PLAN_PARTS, CFGP_PLAN_NOPARTS, CFGP_PLAN_TOOBIG };
static inline uint8_t cfgpPlan(size_t L, bool partsOk, size_t D) {
  if (L <= CFGP_LEGACY_MAX)                    return CFGP_PLAN_LEGACY;
  if (!partsOk)                                return CFGP_PLAN_NOPARTS;   // never parts to a requester that did not ask
  if (cfgpPartCount(L, D) > CFGP_MAX_PARTS)    return CFGP_PLAN_TOOBIG;
  return CFGP_PLAN_PARTS;
}

// "P<id>,<k>,<K>:" into out. Returns its length (<= CFGP_PART_HDR_MAX for k, K <= 16).
static inline size_t cfgpPartHeader(char *out, size_t cap, uint16_t id, unsigned k, unsigned K) {
  if (cap == 0) return 0;
  const int n = snprintf(out, cap, "P%04X,%u,%u:", (unsigned)id, k, K);
  if (n < 0) { out[0] = '\0'; return 0; }
  return (size_t)n < cap ? (size_t)n : cap - 1;
}

// Frags a message of msgLen bytes takes (an empty message still takes one, with an empty payload).
static inline unsigned cfgpChunkCount(size_t msgLen) {
  return msgLen ? (unsigned)((msgLen + CFGP_CHUNK_BYTES - 1) / CFGP_CHUNK_BYTES) : 1u;
}

// A sessionId for the next message: never 0 (relays' "no session") or 0xFFFF (their ring-buffer
// init), and never the previous one this target sent - a relay keeps ONE lastDeliveredSession
// and drops a session equal to it as a second pass. rnd: any random 32-bit value.
static inline uint16_t cfgpSessionId(uint32_t rnd, uint16_t prev) {
  uint16_t s = (uint16_t)(1u + rnd % 0xFFFEu);             // 1..0xFFFE
  if (s == prev) s = (uint16_t)(s % 0xFFFEu + 1u);         // the next one, wrapping within 1..0xFFFE
  return s;
}

// ── Errors ────────────────────────────────────────────────────────
// The relay prints "[MGMT:CFGERR,<n>]" + the text without its 'E'. NOMEM and CHANGED are worth a
// retry, and so is NOPARTS: a request for parts (?MGMT,PULL,<n>,P) only gets it when every type-19
// copy was lost and a type-5 copy got through, which the next attempt undoes. A plain request gets
// it every time, but only a person typing one sees that: every tool that knows the code asks with
// ,P. TOOBIG comes back every time.
enum : uint8_t { CFGP_E_NONE = 0, CFGP_E_NOMEM, CFGP_E_CHANGED, CFGP_E_NOPARTS, CFGP_E_TOOBIG };

static inline const char *cfgpErrName(uint8_t code) {
  switch (code) {
    case CFGP_E_NOMEM:   return "NOMEM";
    case CFGP_E_CHANGED: return "CHANGED";
    case CFGP_E_NOPARTS: return "NOPARTS";
    case CFGP_E_TOOBIG:  return "TOOBIG";
    default:             return "UNKNOWN";
  }
}
static inline bool cfgpErrRetryable(uint8_t code) {
  return code == CFGP_E_NOMEM || code == CFGP_E_CHANGED || code == CFGP_E_NOPARTS;
}

// "E<CODE>,<detail>" into out, at most CFGP_ERR_MAX characters. Numbers only, no config text.
//   NOMEM   a = bytes needed (0: a config line's String, whose size is not known), b = free heap,
//           c = largest free block
//   CHANGED a = walks tried
//   NOPARTS a = reply length, b = the one-reply limit. Neutral about the cause: the requester may have
//           asked for parts and lost every copy, or be a Wizard or relay too old to ask
//   TOOBIG  a = reply length, b = the largest config parts can carry, c = the part limit
static inline size_t cfgpErrorText(char *out, size_t cap, uint8_t code,
                                   unsigned long a, unsigned long b, unsigned long c) {
  if (cap == 0) return 0;
  if (cap > CFGP_ERR_MAX + 1) cap = CFGP_ERR_MAX + 1;
  int n;
  switch (code) {
    case CFGP_E_NOMEM:
      n = a ? snprintf(out, cap, "ENOMEM,need %lu bytes; free %lu; largest block %lu", a, b, c)
            : snprintf(out, cap, "ENOMEM,a config line did not fit; free %lu; largest block %lu", b, c);
      break;
    case CFGP_E_CHANGED:
      n = snprintf(out, cap, "ECHANGED,the config changed while it was sent (%lu tries); retry", a); break;
    case CFGP_E_NOPARTS:
      n = snprintf(out, cap, "ENOPARTS,%lu chars; one reply holds %lu; ask with ,P - an older Wizard or relay cannot",
                   a, b); break;
    case CFGP_E_TOOBIG:
      n = snprintf(out, cap, "ETOOBIG,%lu chars; max %lu in %lu parts; read it over USB with ?backup", a, b, c); break;
    default:
      n = snprintf(out, cap, "E%s,", cfgpErrName(code)); break;
  }
  if (n < 0) { out[0] = '\0'; return 0; }
  return (size_t)n < cap ? (size_t)n : cap - 1;
}
