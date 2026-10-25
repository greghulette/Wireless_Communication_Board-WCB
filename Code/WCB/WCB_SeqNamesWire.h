#pragma once
// ════════════════════════════════════════════════════════════════════════════
//  WCB_SeqNamesWire.h — the stored-sequence NAMES reply, paged: where a list is
//  split, the page header, and the requester's reassembly (no Arduino, no radio).
//  Design: docs/SEQUENCE_INVENTORY.md §3.
//
//  PURE C++11 ON PURPOSE. Everything here is host-tested by
//  tests/seqnames_page_test.cpp, and a BYTE-IDENTICAL copy ships in WCB_Client
//  (src/WCB_SeqNamesWire.h), because the WCB firmware (target and ?MGMT,SEQ
//  requester) and every WCB_Client host (NaviCore) must split, frame and join
//  these pages the same way. Change both copies together; the test pins them.
//
//  One relay reply carries at most 16 frags x 182 = 2912 characters
//  (sendResultFrags), and the sequence store holds a few hundred sequences, so
//  from about 180 names the list does not fit one reply. Then it goes out in
//  PAGES, each its own reply, each asked for on its own:
//    - a list that fits one reply goes out exactly as it always has: packet type
//      14, "<8 hex>,<count>[,<key>...]", to an old request (13) and a new one (28);
//    - a list that does not fit goes only to a request that can page (28, which
//      names the page it wants), one page per request, as packet type 29;
//    - an old request (13) for a list that does not fit is NOT answered: every
//      WCB_Client up to 1.18.1 reads any type-14 reply as the inventory, so the
//      "[ERROR] Result too large" text a 7.0.1 board sends read as an EMPTY list.
//
//  A page's payload is
//      <8 hex hash>,<count>,<page>,<pages>,<keyBytes>[,<key>...]
//  hash and count are the whole list's (the same as the one-reply form), page is
//  0-based, pages is how many there are, and keyBytes is the length of every
//  page's key text together (each key with its comma), so a requester can size
//  its buffer at page 0 and check the join at the end. Every page of one list
//  carries the same four; a page whose four differ from page 0's was built from
//  a different list (the store changed mid-pull): start over.
// ════════════════════════════════════════════════════════════════════════════

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

namespace seqn {

// ---- Transport ------------------------------------------------------------
// The page request rides the 59-byte struct of the single-sequence request
// (SEQVAL_REQ, 15): password[40], packetType, targetWCB, requesterWCB, then the
// page in the first byte of what is the key there, the rest zero. The 43-byte
// request (13) has no spare byte: password[40] + three. A released target drops
// a 59-byte request of any type but 15 at its receive filter (and older firmware
// drops every 59-byte frame), and answers the type-13 copies sent behind it.
static const uint8_t  PT_SEQ_REQ      = 13;    // requester → target: the names, one reply (43 B)
static const uint8_t  PT_SEQ_FRAG     = 14;    // target → requester: the whole list (230-byte frags)
static const uint8_t  PT_PAGE_REQ     = 28;    // requester → target: one page of the names (59 B)
static const uint8_t  PT_PAGE         = 29;    // target → requester: one page (230-byte frags)
static const size_t   PAGE_REQ_LEN    = 59;    // sizeof the request struct (pinned on both sides)
static const size_t   PAGE_REQ_PAGE_AT = 43;   // offset of the page byte in it

// ---- Sizes ----------------------------------------------------------------
static const unsigned REPLY_MAX     = 2912;    // one reply: MGMT_MAX_CHUNKS 16 x (CONFIG_PAYLOAD_SIZE - 1) 182
static const unsigned KEY_MAX       = 15;      // SEQ_KEY_MAX_LEN, the NVS key limit
static const unsigned HEADER_MAX    = 28;      // "<8 hex>,<count 5>,<page 3>,<pages 3>,<keyBytes 5>"
static const unsigned PAGE_KEYS_MAX = 2880;    // a page's key text, each key with its leading comma
static const unsigned MAX_PAGES     = 64;      // a requester refuses more; a full 32 KB store makes about 12
static const unsigned COUNT_MAX     = 65535;   // WCB_Client's callback carries a uint16_t count
static const unsigned KEYBYTES_MAX  = 65535;

static_assert(HEADER_MAX + PAGE_KEYS_MAX <= REPLY_MAX, "a page (header + key text) must fit one reply");
static_assert(KEY_MAX + 1 <= PAGE_KEYS_MAX, "every key must fit an empty page");

// ---- Timing (requester) ---------------------------------------------------
// Page 0 is asked once (three copies of each request type, 15 ms apart) and given
// WAIT_FIRST_MS, as the one-reply request always was: a board that is not there
// costs no more than before. A later page is asked again after RETRY_MS with no
// progress (no frag of it heard), at most PAGE_TRIES times. Not shorter: the target
// ignores a repeat of the same (requester, page) for 1500 ms after it finished
// answering, and it may start answering ~0.5 s after the request (busy sending the
// last page's second pass), so a retry at 2.5 s could still land inside that window.
static const uint32_t WAIT_FIRST_MS = 4000;
static const uint32_t RETRY_MS      = 3000;
static const uint8_t  PAGE_TRIES    = 3;
static const uint8_t  RESTARTS_MAX  = 2;       // the list changed mid-pull: start over at most this often
static const uint32_t PULL_MAX_MS   = 60000;   // a pull that has not finished by then is given up

// ---- Arithmetic -----------------------------------------------------------
inline unsigned digits(unsigned v) {
  unsigned d = 1;
  while (v >= 10) { v /= 10; d++; }
  return d;
}

// What a key costs in a reply: its comma and its characters.
inline unsigned keyCost(size_t len) { return (unsigned)len + 1; }

// Length of the one-reply form, "<8 hex>,<count>" and every key with its comma.
inline unsigned wholeLen(unsigned count, unsigned keyBytes) { return 8 + 1 + digits(count) + keyBytes; }

// Does the whole list fit one reply (packet type 14)?
inline bool wholeFits(unsigned count, unsigned keyBytes) { return wholeLen(count, keyBytes) <= REPLY_MAX; }

// ---- The split (target) ---------------------------------------------------
// Fed every key in save order, once per request: counts the list, sums its key
// text, and says which keys belong on the page asked for. Greedy: a key goes on
// the current page while that page's key text stays within PAGE_KEYS_MAX, so the
// same list always splits the same way and a page is built without the others.
struct Pager {
  unsigned want;       // the page asked for
  unsigned count;      // keys fed
  unsigned keyBytes;   // their cost
  unsigned page;       // the page the last key went on
  unsigned fill;       // that page's key text so far

  explicit Pager(unsigned wantPage) : want(wantPage), count(0), keyBytes(0), page(0), fill(0) {}

  // True when this key (len characters) belongs on page `want`.
  bool add(size_t len) {
    const unsigned c = keyCost(len);
    if (count > 0 && fill + c > PAGE_KEYS_MAX) { page++; fill = 0; }
    fill += c;
    count++;
    keyBytes += c;
    return page == want;
  }
  unsigned pages() const { return page + 1; }   // at least 1, an empty list too
};

// ---- Page header ----------------------------------------------------------
struct PageHeader {
  uint32_t hash;
  unsigned count, page, pages, keyBytes;
};

// "<8 hex>,<count>,<page>,<pages>,<keyBytes>" into out; its length, or -1 when it does not fit.
inline int formatPageHeader(char *out, size_t cap, const PageHeader &h) {
  const int n = snprintf(out, cap, "%08X,%u,%u,%u,%u", (unsigned)h.hash, h.count, h.page, h.pages, h.keyBytes);
  return (n < 0 || (size_t)n >= cap) ? -1 : n;
}

// "<8 hex>,<count>": the one-reply form's head, and the head of a joined list.
inline int formatWholeHeader(char *out, size_t cap, uint32_t hash, unsigned count) {
  const int n = snprintf(out, cap, "%08X,%u", (unsigned)hash, count);
  return (n < 0 || (size_t)n >= cap) ? -1 : n;
}

// Exactly 8 hex digits.
inline bool parseHex8(const char *&p, const char *end, uint32_t &v) {
  if (end - p < 8) return false;
  uint32_t r = 0;
  for (int i = 0; i < 8; i++) {
    const char c = p[i];
    unsigned d;
    if (c >= '0' && c <= '9') d = (unsigned)(c - '0');
    else if (c >= 'A' && c <= 'F') d = (unsigned)(c - 'A' + 10);
    else if (c >= 'a' && c <= 'f') d = (unsigned)(c - 'a' + 10);
    else return false;
    r = (r << 4) | d;
  }
  p += 8;
  v = r;
  return true;
}

// 1-5 decimal digits, no sign, no space, at most `max`.
inline bool parseUint(const char *&p, const char *end, unsigned &v, unsigned max) {
  unsigned long r = 0;
  int n = 0;
  while (p < end && *p >= '0' && *p <= '9') {
    if (++n > 5) return false;
    r = r * 10 + (unsigned long)(*p - '0');
    p++;
  }
  if (n == 0 || r > max) return false;
  v = (unsigned)r;
  return true;
}

inline bool take(const char *&p, const char *end, char c) {
  if (p >= end || *p != c) return false;
  p++;
  return true;
}

// Key text ",k1,k2,..." (or empty): every key 1..KEY_MAX characters. Counts the keys.
inline bool checkKeyText(const char *s, size_t n, unsigned &keys) {
  keys = 0;
  size_t i = 0;
  while (i < n) {
    if (s[i] != ',') return false;
    size_t j = i + 1;
    while (j < n && s[j] != ',') {
      if (s[j] == '\0' || s[j] == '\r' || s[j] == '\n') return false;
      j++;
    }
    const size_t len = j - (i + 1);
    if (len == 0 || len > KEY_MAX) return false;
    keys++;
    i = j;
  }
  return true;
}

// A page reply: its header, and where its key text starts (at its first comma, or n).
inline bool parsePageHeader(const char *s, size_t n, PageHeader &h, size_t &keysAt) {
  const char *p = s, *end = s + n;
  if (!parseHex8(p, end, h.hash) || !take(p, end, ',')) return false;
  if (!parseUint(p, end, h.count, COUNT_MAX) || !take(p, end, ',')) return false;
  if (!parseUint(p, end, h.page, 255) || !take(p, end, ',')) return false;
  if (!parseUint(p, end, h.pages, MAX_PAGES) || !take(p, end, ',')) return false;
  if (!parseUint(p, end, h.keyBytes, KEYBYTES_MAX)) return false;
  if (p < end && *p != ',') return false;
  if (h.pages == 0) return false;
  if ((h.count == 0) != (h.keyBytes == 0)) return false;
  if (h.keyBytes < 2 * h.count || h.keyBytes > (KEY_MAX + 1) * h.count) return false;
  keysAt = (size_t)(p - s);
  return true;
}

// A one-reply list, "<8 hex>,<count>[,<key>...]", checked whole: the count must be
// the number of keys. Anything else - "[ERROR] Result too large to relay (...)" from
// a 7.0.1 board whose list does not fit - is not an inventory. namesAt: the first
// key (after its comma), or n for an empty list.
inline bool parseWhole(const char *s, size_t n, uint32_t &hash, unsigned &count, size_t &namesAt) {
  const char *p = s, *end = s + n;
  if (!parseHex8(p, end, hash) || !take(p, end, ',')) return false;
  if (!parseUint(p, end, count, COUNT_MAX)) return false;
  unsigned keys = 0;
  if (!checkKeyText(p, (size_t)(end - p), keys) || keys != count) return false;
  namesAt = p < end ? (size_t)(p - s) + 1 : n;
  return true;
}

// ---- Reassembly (requester) -----------------------------------------------
// One per pull. The caller parses each page reply's header (parsePageHeader), asks
// check(); for page 0 it then allocates wholeLen(count, keyBytes) + 1 bytes (plus
// whatever it puts around the list) and calls start(); then append() with the
// page's key text, or commit() once it has copied that text into place itself.
// The joined list is the one-reply form: "<8 hex>,<count>,k1,...".
enum Step : uint8_t {
  STEP_OK,        // check(): this is the page asked for, go on
  STEP_STALE,     // not the page asked for (a late copy): ignore it, keep waiting
  STEP_RESTART,   // built from another list (the store changed mid-pull): ask for page 0 again
  STEP_BAD,       // malformed, or out of bounds: ignore it, keep waiting
  STEP_NEXT,      // append(): page taken, ask for `next`
  STEP_DONE,      // append(): the whole list is joined
};

struct Assembly {
  PageHeader first;   // page 0's header (valid once next > 0)
  unsigned   next;    // the page asked for
  unsigned   keys;    // keys joined so far
  unsigned   bytes;   // their key text, commas included
  size_t     len;     // characters written to the caller's buffer

  Assembly() { reset(); }
  void reset() {
    first.hash = 0;
    first.count = first.page = first.pages = first.keyBytes = 0;
    next = keys = bytes = 0;
    len = 0;
  }

  Step check(const PageHeader &h) const {
    if (h.pages > MAX_PAGES || h.pages == 0) return STEP_BAD;
    if (next == 0) {
      if (h.page != 0) return STEP_STALE;
      return STEP_OK;
    }
    if (h.hash != first.hash || h.count != first.count || h.pages != first.pages || h.keyBytes != first.keyBytes)
      return STEP_RESTART;
    if (h.page != next) return STEP_STALE;
    return STEP_OK;
  }

  // Page 0 accepted: writes "<8 hex>,<count>" into out (cap bytes, at least wholeLen + 1).
  bool start(const PageHeader &h, char *out, size_t cap) {
    if (cap < (size_t)wholeLen(h.count, h.keyBytes) + 1) return false;
    const int n = formatWholeHeader(out, cap, h.hash, h.count);
    if (n < 0) return false;
    first = h;
    keys = bytes = 0;
    len = (size_t)n;
    out[len] = '\0';
    return true;
  }

  // The page's key text (",k1,k2..."), already written by the caller at out + len (n bytes, within cap): taken, or
  // not (the buffer then still ends at len). NEXT, DONE, BAD, or RESTART when the pages together do not add up to
  // page 0's count and keyBytes.
  Step commit(char *out, size_t n, size_t cap) {
    if (len + n + 1 > cap) return STEP_BAD;
    unsigned k = 0;
    Step s = STEP_NEXT;
    if (!checkKeyText(out + len, n, k)) s = STEP_BAD;
    else if (k == 0 && first.count != 0) s = STEP_BAD;        // every page of a non-empty list carries a key
    else if (keys + k > first.count || bytes + n > first.keyBytes) s = STEP_RESTART;
    else if (next + 1 >= first.pages && (keys + k != first.count || bytes + n != first.keyBytes)) s = STEP_RESTART;
    if (s != STEP_NEXT) {
      out[len] = '\0';
      return s;
    }
    len += n;
    out[len] = '\0';
    keys += k;
    bytes += (unsigned)n;
    next++;
    return next < first.pages ? STEP_NEXT : STEP_DONE;
  }

  // As commit(), copying the key text in first.
  Step append(const char *text, size_t n, char *out, size_t cap) {
    if (len + n + 1 > cap) return STEP_BAD;
    memcpy(out + len, text, n);
    return commit(out, n, cap);
  }
};

}  // namespace seqn
