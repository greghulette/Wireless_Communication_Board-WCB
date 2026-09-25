// ============================================================================
// Config pull parts: split & framing SPEC TEST  (host-side, no Arduino)
//
// WHAT THIS IS
//   A regression guard for the pure arithmetic of the mesh config pull (F13):
//   the reply stream and its CRC, the legacy/parts/error decision, the
//   grid-anchored UTF-8 split points, the part header and message sizes, the
//   error texts, lost-token accounting and the sessionId rule. Runs anywhere a C++11 compiler exists
//   (CI: .github/workflows/wdp-tests.yml); no ESP32 / Arduino toolchain needed.
//
// UNLIKE tests/wdp_wire_test.cpp, THIS LINKS THE REAL CODE
//   Code/WCB/WCB_ConfigParts.h has no Arduino dependency, so it is included as
//   it is: what runs here is what the firmware runs. The one mirror below is
//   refReply(), the pre-F13 buildConfigString() (WCB.ino), because a reply of
//   2912 characters or less must stay byte-identical to what it made
//   (F13 invariant 2) - keep it frozen.
//
// BUILD + RUN
//   g++ -std=c++11 -Wall -Wextra -O2 tests/config_parts_test.cpp -o config_parts_test
//   ./config_parts_test      # exit 0 = all pass, 1 = a failure (prints which)
// ============================================================================
#include "../Code/WCB/WCB_ConfigParts.h"

#include <cstdint>
#include <cstring>
#include <cstdio>
#include <string>
#include <vector>

static int g_fail = 0;
#define CHECK(cond, msg) do{ if(!(cond)){ printf("  FAIL: %s\n", msg); g_fail++; } }while(0)
#define CHECKF(cond, ...) do{ if(!(cond)){ char _m[256]; snprintf(_m, sizeof(_m), __VA_ARGS__); printf("  FAIL: %s\n", _m); g_fail++; } }while(0)

// Deterministic PRNG (xorshift32) - the test must fail the same way every run.
static uint32_t g_rng = 0x12345678u;
static uint32_t rnd() { g_rng ^= g_rng << 13; g_rng ^= g_rng >> 17; g_rng ^= g_rng << 5; return g_rng; }

static uint32_t crcOf(const std::string &s) {
  return ~cfgpCrc32Update(CFGP_CRC_INIT, (const uint8_t *)s.data(), s.size());
}

// ---- mirror: the pre-F13 buildConfigString (WCB.ino), frozen -----------------
static std::string refReply(const std::string &ver, const std::vector<std::string> &tokens) {
  std::string out = "[VER:" + ver + "]";
  const size_t bodyStart = out.size();
  bool first = true;
  for (const std::string &t : tokens) { out += first ? "?" : "^?"; out += t; first = false; }
  char chk[9];
  snprintf(chk, sizeof(chk), "%08X", (unsigned)~cfgpCrc32Update(CFGP_CRC_INIT,
           (const uint8_t *)out.data() + bodyStart, out.size() - bodyStart));
  out += "^?CHK";
  out += chk;
  return out;
}

// The firmware's walk: cfgpReplyBegin + one cfgpReplyToken per token + cfgpReplyEnd.
static void walkReply(CfgpWalk &w, const std::string &ver, const std::vector<std::string> &tokens) {
  cfgpReplyBegin(w, ver.data(), ver.size());
  for (const std::string &t : tokens) cfgpReplyToken(w, t.data(), t.size());
  cfgpReplyEnd(w);
}

// UTF-8 samples: 1, 2, 3 and 4 bytes.
static const char *const U1 = "A";
static const char *const U2 = "\xC3\xB4";              // o-circumflex
static const char *const U3 = "\xE6\x97\xA5";          // CJK
static const char *const U4 = "\xF0\x9F\xA4\x96";      // robot face

static bool validUtf8Start(const std::string &s, size_t at) {
  return at >= s.size() || !cfgpIsCont((uint8_t)s[at]);
}

// Random tokens of printable ASCII with multi-byte characters mixed in, never splitting one.
static std::vector<std::string> randomTokens(size_t targetLen, int utf8Percent) {
  std::vector<std::string> v;
  size_t total = 0;
  while (total < targetLen) {
    std::string t = "SEQ,SAVE,k" + std::to_string(v.size()) + ",";
    const size_t n = 1 + rnd() % 300;
    while (t.size() < n) {
      if ((int)(rnd() % 100) < utf8Percent) {
        const char *u[] = {U2, U3, U4};
        t += u[rnd() % 3];
      } else {
        t += (char)(' ' + rnd() % 95);
      }
    }
    total += t.size() + 2;
    v.push_back(t);
  }
  return v;
}

// Run the whole target-side plan for one reply and check every property the spec states.
// Returns K (0 = legacy / error).
static unsigned checkReply(const std::string &ver, const std::vector<std::string> &tokens, size_t D,
                           bool partsOk, const char *label) {
  const std::string ref = refReply(ver, tokens);
  const size_t L = ref.size();

  // Measure walk: length, body CRC and grid, without holding anything.
  CfgpWalk m;
  cfgpWalkBegin(m, D, nullptr, 0, 0);
  walkReply(m, ver, tokens);
  CHECKF(m.len == L, "%s: measure length %zu != reply %zu", label, m.len, L);
  CHECKF(m.lostTokens == 0, "%s: a whole walk lost %u token(s)", label, m.lostTokens);
  const size_t bodyStart = 5 + ver.size() + 1;
  CHECKF(m.crc == crcOf(ref.substr(bodyStart, L - bodyStart - 13)), "%s: measure CRC is not the body's", label);
  char chk[9]; snprintf(chk, sizeof(chk), "%08lX", (unsigned long)m.crc);
  CHECKF(ref.compare(L - 8, 8, chk) == 0, "%s: ^?CHK value differs from the body CRC", label);

  const uint8_t plan = cfgpPlan(L, partsOk, D);
  if (plan == CFGP_PLAN_LEGACY) {
    // The legacy reply: one copy walk of [0, L), byte-identical to the old buildConfigString.
    std::vector<uint8_t> buf(L ? L : 1);
    CfgpWalk c;
    cfgpWalkBegin(c, 0, buf.data(), 0, L);
    walkReply(c, ver, tokens);
    CHECKF(c.len == L && c.crc == m.crc, "%s: legacy copy walk disagrees with the measure", label);
    CHECKF(std::string((const char *)buf.data(), L) == ref, "%s: legacy reply is not byte-identical", label);
    CHECKF(L <= CFGP_MSG_MAX && cfgpChunkCount(L) <= CFGP_MAX_CHUNKS, "%s: legacy reply over one session", label);
    return 0;
  }
  if (plan != CFGP_PLAN_PARTS) return 0;

  uint16_t split[CFGP_MAX_PARTS + 1];
  const unsigned K = cfgpSplit(L, D, m.grid, split);
  CHECKF(K == cfgpPartCount(L, D) && K >= 2 && K <= CFGP_MAX_PARTS, "%s: K=%u, ceil(L/D)=%zu", label, K,
         cfgpPartCount(L, D));
  if (K == 0) return 0;
  CHECKF(split[0] == 0 && split[K] == L, "%s: split ends", label);

  std::string joined;
  const uint16_t id = (uint16_t)rnd();
  for (unsigned k = 1; k <= K; k++) {
    const size_t lo = split[k - 1], hi = split[k];
    // grid-anchored, backed off at most 3, never onto a continuation byte (the text is valid UTF-8)
    if (k < K) {
      CHECKF(hi <= k * D && hi + CFGP_UTF8_BACKOFF >= k * D, "%s: split %u = %zu, grid %zu", label, k, hi, k * D);
      CHECKF(validUtf8Start(ref, hi), "%s: part %u starts inside a character", label, k + 1);
    }
    const size_t n = hi - lo;
    if (k == 1)      CHECKF(n + CFGP_UTF8_BACKOFF >= D && n <= D, "%s: part 1 holds %zu (D=%zu)", label, n, D);
    else if (k < K)  CHECKF(n + CFGP_UTF8_BACKOFF >= D && n <= D + CFGP_UTF8_BACKOFF, "%s: part %u holds %zu", label, k, n);
    else             CHECKF(n >= 1 && n <= D + CFGP_UTF8_BACKOFF, "%s: last part holds %zu", label, n);

    // The message: header + one copy walk of this window + '~', exactly as the target builds it.
    char hdr[CFGP_PART_HDR_MAX + 1];
    const size_t hl = cfgpPartHeader(hdr, sizeof(hdr), id, k, K);
    const size_t msgLen = hl + n + 1;
    CHECKF(hl <= CFGP_PART_HDR_MAX && msgLen <= CFGP_MSG_MAX && cfgpChunkCount(msgLen) <= CFGP_MAX_CHUNKS,
           "%s: part %u message %zu bytes", label, k, msgLen);
    std::vector<uint8_t> buf(msgLen);
    memcpy(buf.data(), hdr, hl);
    CfgpWalk c;
    cfgpWalkBegin(c, 0, buf.data() + hl, lo, hi);
    walkReply(c, ver, tokens);
    buf[msgLen - 1] = '~';
    CHECKF(c.len == L && c.crc == m.crc, "%s: part %u copy walk disagrees with the measure", label, k);
    const std::string msg((const char *)buf.data(), msgLen);
    char want[32];
    snprintf(want, sizeof(want), "P%04X,%u,%u:", (unsigned)id, k, K);
    CHECKF(msg.compare(0, strlen(want), want) == 0 && msg.back() == '~', "%s: part %u framing", label, k);
    CHECKF(msg.substr(hl, n) == ref.substr(lo, n), "%s: part %u data is not the reply's window", label, k);
    joined += msg.substr(hl, n);
  }
  CHECKF(joined == ref, "%s: parts do not join to the reply", label);
  return K;
}

// ---- tests -------------------------------------------------------------------
static void test_crc() {
  const std::string v = "123456789";
  CHECK(crcOf(v) == 0xCBF43926u, "CRC-32 check value (123456789 -> CBF43926)");
  CHECK(crcOf("") == 0u, "CRC of nothing is 0 (an empty body)");
  // Running form == one-shot, at every split of the input
  const std::string s = "?HW,32^?WCB,2^?ALIAS,D\xC3\xB4me^?SEQ,SAVE,a,x^?y";
  for (size_t cut = 0; cut <= s.size(); cut++) {
    uint32_t c = cfgpCrc32Update(CFGP_CRC_INIT, (const uint8_t *)s.data(), cut);
    c = cfgpCrc32Update(c, (const uint8_t *)s.data() + cut, s.size() - cut);
    CHECKF(~c == crcOf(s), "running CRC split at %zu", cut);
  }
}

static void test_backoff() {
  const uint8_t ascii[4] = {'a', 'b', 'c', 'd'};
  CHECK(cfgpBackOff(1000, ascii) == 1000, "ASCII: no back-off");
  // bytes at x-3..x
  const uint8_t two[4]   = {'a', 'b', 0xC3, 0xB4};   // x is the tail of a 2-byte char
  CHECK(cfgpBackOff(1000, two) == 999, "2-byte char split: back 1");
  const uint8_t three[4] = {'a', 0xE6, 0x97, 0xA5};  // x is the last byte of a 3-byte char
  CHECK(cfgpBackOff(1000, three) == 998, "3-byte char, last byte at x: back 2");
  const uint8_t four[4]  = {0xF0, 0x9F, 0xA4, 0x96};
  CHECK(cfgpBackOff(1000, four) == 997, "4-byte char, last byte at x: back 3");
  const uint8_t lead[4]  = {'a', 'b', 'c', 0xF0};    // x is a lead byte: split before it
  CHECK(cfgpBackOff(1000, lead) == 1000, "lead byte at x: no back-off");
  const uint8_t bad[4]   = {0x80, 0x80, 0x80, 0x80}; // not UTF-8: 4 continuation bytes
  CHECK(cfgpBackOff(1000, bad) == 1000, "4 continuation bytes: unchanged");
  CHECK(cfgpBackOff(2, bad) == 2, "no underflow near 0");
}

static void test_plan_thresholds() {
  const size_t D = CFGP_PART_DATA;
  CHECK(cfgpPlan(0, false, D) == CFGP_PLAN_LEGACY, "empty: legacy");
  CHECK(cfgpPlan(2912, false, D) == CFGP_PLAN_LEGACY, "2912, plain request: legacy");
  CHECK(cfgpPlan(2912, true, D) == CFGP_PLAN_LEGACY, "2912, parts request: still legacy (invariant 2)");
  CHECK(cfgpPlan(2913, false, D) == CFGP_PLAN_NOPARTS, "2913, plain request: NOPARTS, never parts");
  CHECK(cfgpPlan(2913, true, D) == CFGP_PLAN_PARTS, "2913, parts request: parts");
  CHECK(cfgpPartCount(2913, D) == 2, "2913 in 2 parts");
  CHECK(cfgpPlan(16 * D, true, D) == CFGP_PLAN_PARTS && cfgpPartCount(16 * D, D) == 16, "16*D: 16 parts");
  CHECK(cfgpPlan(16 * D + 1, true, D) == CFGP_PLAN_TOOBIG, "16*D+1: TOOBIG");
  CHECK(cfgpPlan(16 * D + 1, false, D) == CFGP_PLAN_NOPARTS, "over everything, plain request: NOPARTS");
  CHECK(cfgpPlan(3000, true, 600) == CFGP_PLAN_PARTS && cfgpPartCount(3000, 600) == 5, "D=600: 3000 in 5 parts");
  CHECK(cfgpPartDataSize(0) == 2880 && cfgpPartDataSize(512) == 512 && cfgpPartDataSize(2880) == 2880,
        "PULLPART accepted values");
  CHECK(cfgpPartDataSize(511) == 0 && cfgpPartDataSize(2881) == 0 && cfgpPartDataSize(-1) == 0,
        "PULLPART rejected values");
}

static void test_sizes() {
  char h[64];
  CHECK(cfgpPartHeader(h, sizeof(h), 0xFFFF, 16, 16) == CFGP_PART_HDR_MAX && strcmp(h, "PFFFF,16,16:") == 0,
        "widest header is 12 characters");
  CHECK(cfgpPartHeader(h, sizeof(h), 0x00A1, 1, 2) == 10 && strcmp(h, "P00A1,1,2:") == 0, "header format");
  CHECK(CFGP_PART_MSG_MAX == 2896 && CFGP_PART_MSG_MAX <= CFGP_MSG_MAX, "largest part 2896 <= 2912");
  // the relay's output slot: 2960 bytes = tag (<= 18) + 2912 + CRLF + NUL
  CHECK(strlen("[MGMT:CFGPART,255]") + CFGP_MSG_MAX + 3 <= 2960, "a part line fits the relay's 2960-byte slot");
  CHECK(cfgpChunkCount(0) == 1 && cfgpChunkCount(1) == 1 && cfgpChunkCount(182) == 1, "chunk count small");
  CHECK(cfgpChunkCount(183) == 2 && cfgpChunkCount(2912) == 16 && cfgpChunkCount(2913) == 17, "chunk count edges");
}

static void test_errors() {
  char e[256];
  const uint8_t codes[] = {CFGP_E_NOMEM, CFGP_E_CHANGED, CFGP_E_NOPARTS, CFGP_E_TOOBIG};
  const unsigned long as[] = {4294967295UL, 0UL};     // 0: NOMEM's "size not known" form
  for (uint8_t code : codes) for (unsigned long a : as) {
    memset(e, 'x', sizeof(e));
    const size_t n = cfgpErrorText(e, sizeof(e), code, a, 4294967295UL, 4294967295UL);
    CHECKF(n == strlen(e) && n <= CFGP_ERR_MAX, "%s: error text %zu chars", cfgpErrName(code), n);
    const std::string s(e);
    const std::string head = std::string("E") + cfgpErrName(code) + ",";
    CHECKF(s.compare(0, head.size(), head) == 0, "%s: starts with %s", cfgpErrName(code), head.c_str());
    bool ascii = true;
    for (char ch : s) if ((unsigned char)ch < 0x20 || (unsigned char)ch > 0x7E) ascii = false;
    CHECKF(ascii && s.find('~') == std::string::npos, "%s: printable ASCII, no '~'", cfgpErrName(code));
  }
  cfgpErrorText(e, sizeof(e), CFGP_E_NOMEM, 2896, 12000, 8000);
  CHECK(strcmp(e, "ENOMEM,need 2896 bytes; free 12000; largest block 8000") == 0, "NOMEM text");
  cfgpErrorText(e, sizeof(e), CFGP_E_NOMEM, 0, 12000, 8000);
  CHECK(strcmp(e, "ENOMEM,a config line did not fit; free 12000; largest block 8000") == 0, "NOMEM text, lost token");
  cfgpErrorText(e, sizeof(e), CFGP_E_TOOBIG, 50000, 46080, 16);
  CHECK(strcmp(e, "ETOOBIG,50000 chars; max 46080 in 16 parts; read it over USB with ?backup") == 0, "TOOBIG text");
  // The requester's length leads the detail (s03 wcb.pull_plain_over_limit looks for it); nothing in it blames the
  // Wizard, since a current one gets NOPARTS only when every type-19 copy of its ,P request was lost.
  cfgpErrorText(e, sizeof(e), CFGP_E_NOPARTS, 3062, 2912, 0);
  CHECK(strcmp(e, "ENOPARTS,3062 chars; one reply holds 2912; ask with ,P - an older Wizard or relay cannot") == 0,
        "NOPARTS text");
  CHECK(cfgpErrRetryable(CFGP_E_NOMEM) && cfgpErrRetryable(CFGP_E_CHANGED), "NOMEM/CHANGED retryable");
  CHECK(cfgpErrRetryable(CFGP_E_NOPARTS), "NOPARTS retryable: on a ,P pull every type-19 copy was lost");
  CHECK(!cfgpErrRetryable(CFGP_E_TOOBIG) && !cfgpErrRetryable(CFGP_E_NONE) && !cfgpErrRetryable(99),
        "TOOBIG / unknown permanent");
  // Into a frag payload (183 bytes): at most 182 characters, so payload[182] stays NUL.
  char payload[183];
  memset(payload, 'x', sizeof(payload));
  const size_t n = cfgpErrorText(payload, sizeof(payload), CFGP_E_NOPARTS, 4294967295UL, 2912, 0);
  CHECK(n <= 182 && payload[n] == '\0', "error fits one chunk with its NUL");
}

static void test_session_ids() {
  const uint32_t edge[] = {0u, 1u, 0xFFFDu, 0xFFFEu, 0xFFFFu, 0x10000u, 0xFFFFFFFFu, 0x1FFFDu};
  const uint16_t prevs[] = {0, 1, 2, 0xFFFE, 0xFFFF, 0x1234};
  for (uint32_t r : edge) for (uint16_t p : prevs) {
    const uint16_t s = cfgpSessionId(r, p);
    CHECKF(s != 0 && s != 0xFFFF && s != p, "sessionId(%lu, %u) = %u", (unsigned long)r, p, s);
  }
  uint16_t prev = 0;
  for (int i = 0; i < 200000; i++) {
    const uint16_t s = cfgpSessionId(rnd(), prev);
    if (s == 0 || s == 0xFFFF || s == prev) { CHECK(false, "random sessionId rule"); break; }
    prev = s;
  }
}

// A token that comes in missing (an invalidated String: c_str() nullptr) or empty is counted and feeds nothing.
// Length and CRC alone would then agree with a walk that never had it, so lostTokens is the only way the firmware
// can tell a short heap from a config that is simply shorter - it must turn such a walk into NOMEM.
static void test_lost_tokens() {
  const std::string ver = "6.3.1_241530RSEP2026";
  const std::vector<std::string> kept = {"HW,32", "WCB,2"};
  CfgpWalk ok;
  cfgpWalkBegin(ok, 0, nullptr, 0, 0);
  walkReply(ok, ver, {"HW,32", "SEQ,SAVE,a,b", "WCB,2"});
  CHECK(ok.lostTokens == 0, "a whole walk loses nothing");
  const char *const lost[] = {nullptr, ""};
  const size_t      ns[]   = {0, 7};                  // an invalidated String reports length 0; guard 7 too
  for (const char *tok : lost) for (size_t n : ns) {
    if (tok && n) continue;                           // "" with length 7 is not a thing a String hands over
    CfgpWalk w;
    cfgpWalkBegin(w, 0, nullptr, 0, 0);
    cfgpReplyBegin(w, ver.data(), ver.size());
    cfgpReplyToken(w, "HW,32", 5);
    cfgpReplyToken(w, tok, n);
    cfgpReplyToken(w, "WCB,2", 5);
    cfgpReplyEnd(w);
    const std::string ref = refReply(ver, kept);
    CHECKF(w.lostTokens == 1, "lost token (%s, %zu) counted", tok ? "empty" : "nullptr", n);
    CHECKF(w.len == ref.size() && w.crc == crcOf(ref.substr(5 + ver.size() + 1, ref.size() - (5 + ver.size() + 1) - 13)),
           "lost token (%s, %zu) feeds nothing", tok ? "empty" : "nullptr", n);
  }
  // The first token lost: the next one still opens with '?', not '^?'.
  CfgpWalk f;
  std::vector<uint8_t> buf(64, 0);
  cfgpWalkBegin(f, 0, buf.data(), 0, 64);
  cfgpReplyBegin(f, "x", 1);
  cfgpReplyToken(f, nullptr, 0);
  cfgpReplyToken(f, "HW,32", 5);
  cfgpReplyEnd(f);
  const std::string ref = refReply("x", {"HW,32"});
  CHECK(f.lostTokens == 1 && f.len == ref.size() && std::string((const char *)buf.data(), f.len) == ref,
        "first token lost: the stream is the reply without it");
}

// Every boundary, every character width, every offset: a D-aligned multi-byte character.
static void test_boundaries_every_offset() {
  const size_t Ds[] = {512, 600, 1000, 2880};
  const char *const chars[] = {U1, U2, U3, U4};
  for (size_t D : Ds) {
    for (const char *u : chars) {
      const size_t w = strlen(u);
      for (size_t off = 0; off < 4; off++) {
        // Build a reply whose body puts a character starting at k*D - off, for every k.
        const std::string ver = "6.3.1_241530RSEP2026";
        const size_t head = 5 + ver.size() + 1 + 1;   // "[VER:" ver "]" "?"
        std::string tok;
        const size_t target = (3 * D > CFGP_LEGACY_MAX ? 3 * D : CFGP_LEGACY_MAX) + 700;   // over 2912: parts
        while (head + tok.size() < target) {
          const size_t pos = head + tok.size();        // stream position of the next byte
          const size_t k = (pos + off) / D;
          if (k >= 1 && pos + off == k * D) tok += u;  // a character starts exactly off bytes before k*D
          else tok += 'z';
        }
        const std::vector<std::string> tokens = {"HW,32", "WCB,2", tok};
        char label[64];
        snprintf(label, sizeof(label), "D=%zu width=%zu off=%zu", D, w, off);
        const unsigned K = checkReply(ver, tokens, D, true, label);
        CHECKF(K >= 2, "%s: went out in parts", label);
      }
    }
  }
}

// L exactly K*D with a 4-byte character across every boundary: K must not grow.
static void test_exact_multiple() {
  const size_t D = 600;
  const std::string ver = "X";
  for (size_t K = 5; K <= 16; K++) {
    const size_t prefix = 5 + ver.size() + 1 + 1;   // "[VER:X]?"
    const size_t bodyTok = K * D - prefix - 13;      // + "^?CHK" + 8 hex = K*D
    std::string tok;
    while (tok.size() < bodyTok) {
      const size_t pos = prefix + tok.size();
      if ((pos + 2) % D == 0 && tok.size() + 4 <= bodyTok) tok += U4;   // straddles k*D
      else tok += 'q';
    }
    const std::vector<std::string> tokens = {tok};
    const std::string ref = refReply(ver, tokens);
    CHECKF(ref.size() == K * D, "exact-multiple fixture is %zu, want %zu", ref.size(), K * D);
    char label[48];
    snprintf(label, sizeof(label), "L=%zu*D", K);
    CHECKF(checkReply(ver, tokens, D, true, label) == K, "%s: K stays %zu", label, K);
  }
  // one byte over 16*D: TOOBIG, never 17 parts
  std::vector<std::string> big = {std::string(16 * D - 5 - ver.size() - 1 - 1 - 13 + 1, 'b')};
  CHECK(refReply(ver, big).size() == 16 * D + 1, "16*D+1 fixture");
  CHECK(cfgpPlan(16 * D + 1, true, D) == CFGP_PLAN_TOOBIG, "16*D+1 is TOOBIG");
  uint16_t split[CFGP_MAX_PARTS + 1];
  uint8_t grid[CFGP_MAX_PARTS][4] = {};
  CHECK(cfgpSplit(16 * D + 1, D, grid, split) == 0, "cfgpSplit refuses 17 parts");
}

static void test_random_replies() {
  const size_t Ds[] = {512, 600, 1024, 2880};
  for (int i = 0; i < 400; i++) {
    const size_t D = Ds[i % 4];
    const size_t len = 100 + rnd() % (16 * D);        // legacy and parts
    const std::vector<std::string> tokens = randomTokens(len, (int)(rnd() % 40));
    char label[48];
    snprintf(label, sizeof(label), "random #%d D=%zu", i, D);
    checkReply("6.3.1_241530RSEP2026", tokens, D, true, label);
    checkReply("6.3.1_241530RSEP2026", tokens, D, false, label);   // plain: legacy or nothing
  }
}

// The legacy reply is byte-identical to the pre-F13 build, at and around the 2912 edge.
static void test_legacy_identity() {
  const std::string ver = "6.3.1_241530RSEP2026";
  for (size_t want = 2900; want <= 2912; want++) {
    const size_t fixed = 5 + ver.size() + 1 + 1 + 13;   // + "?" + "^?CHK" + 8 hex
    std::vector<std::string> tokens = {std::string(want - fixed, 'L')};
    CHECKF(refReply(ver, tokens).size() == want, "fixture %zu", want);
    checkReply(ver, tokens, CFGP_PART_DATA, true, "legacy edge");
    CHECKF(cfgpPlan(want, true, CFGP_PART_DATA) == CFGP_PLAN_LEGACY, "%zu goes out as legacy", want);
  }
  // No tokens at all: "[VER:x]^?CHK00000000"
  CfgpWalk w;
  cfgpWalkBegin(w, 0, nullptr, 0, 0);
  walkReply(w, "x", {});
  CHECK(w.len == refReply("x", {}).size() && w.crc == 0, "empty config: empty body, CRC 0");
}

// Feeding in arbitrary pieces: the grid and the copy window do not depend on where pieces break.
static void test_arbitrary_pieces() {
  std::string s;
  for (int i = 0; i < 9000; i++) s += (char)(rnd() & 0xFF);
  const size_t D = 700;
  CfgpWalk whole;
  cfgpWalkBegin(whole, D, nullptr, 0, 0);
  cfgpWalkFeed(whole, s.data(), s.size(), true);
  for (int trial = 0; trial < 200; trial++) {
    const size_t lo = rnd() % s.size(), hi = lo + rnd() % (s.size() - lo + 1);
    std::vector<uint8_t> dst(hi - lo + 1, 0xEE);
    CfgpWalk w;
    cfgpWalkBegin(w, D, dst.data(), lo, hi);
    size_t at = 0;
    while (at < s.size()) {
      const size_t n = 1 + rnd() % (trial % 3 == 0 ? 3 : 400);
      const size_t m = n < s.size() - at ? n : s.size() - at;
      cfgpWalkFeed(w, s.data() + at, m, true);
      at += m;
    }
    CHECKF(w.len == s.size() && w.crcRun == whole.crcRun, "trial %d: length/CRC", trial);
    CHECKF(memcmp(w.grid, whole.grid, sizeof(w.grid)) == 0, "trial %d: grid", trial);
    CHECKF(memcmp(dst.data(), s.data() + lo, hi - lo) == 0 && dst[hi - lo] == 0xEE,
           "trial %d: window [%zu,%zu) copied exactly, nothing past it", trial, lo, hi);
  }
  for (size_t k = 1; k <= CFGP_MAX_PARTS && k * D < s.size(); k++)
    for (size_t j = 0; j < 4; j++)
      CHECKF(whole.grid[k - 1][j] == (uint8_t)s[k * D - 3 + j], "grid %zu byte %zu", k, j);
}

int main() {
  printf("== Config pull parts: split & framing spec test ==\n");
  test_crc();
  test_backoff();
  test_plan_thresholds();
  test_sizes();
  test_errors();
  test_lost_tokens();
  test_session_ids();
  test_boundaries_every_offset();
  test_exact_multiple();
  test_random_replies();
  test_legacy_identity();
  test_arbitrary_pieces();
  if (g_fail) { printf("\n%d CHECK(s) FAILED\n", g_fail); return 1; }
  printf("\nAll checks passed.\n");
  return 0;
}
