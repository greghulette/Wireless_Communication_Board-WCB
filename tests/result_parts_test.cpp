// ============================================================================
// Mesh results in parts: split, header & reassembly SPEC TEST  (host-side, no Arduino)
//
// WHAT THIS IS
//   A regression guard for the mesh management results too long for one relay reply
//   (docs/MGMT_RELAY.md §10): ?MGMT,STATS, ?MGMT,ETM,CHAR and ?MGMT,SEQGET. When a
//   result goes out whole (packet types 9/10/16) and when in parts (32/33/34), where
//   the parts split (after a '\n' for STATS and ETM, never inside a UTF-8 character for
//   a sequence), the part header, and the requester's reassembly and clock - stale
//   copies, a lost part asked again, a sequence edited mid-pull, a checksum that does
//   not match. Runs anywhere a C++11 compiler exists (CI: .github/workflows/wdp-tests.yml).
//
// THIS LINKS THE REAL CODE
//   Code/WCB/WCB_ResultWire.h has no Arduino dependency, so it is included as it is:
//   what runs here is what the firmware, WcbMgmt, the MgmtRelay example and
//   WCB_Client::requestSequence (a byte-identical copy, WCBClient src/WCB_ResultWire.h)
//   run. The one mirror below is targetAnswer(), the firmware's answerStats() /
//   answerSeqVal() decision (WCB.ino) without Arduino String - keep the two in step.
//
// BUILD + RUN
//   g++ -std=c++11 -Wall -Wextra -O2 tests/result_parts_test.cpp -o result_parts_test
//   ./result_parts_test      # exit 0 = all pass, 1 = a failure (prints which)
// ============================================================================
#include "../Code/WCB/WCB_ResultWire.h"

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

using namespace rparts;

static int g_fail = 0;
#define CHECK(cond, msg) do{ if(!(cond)){ printf("  FAIL: %s (line %d)\n", msg, __LINE__); g_fail++; } }while(0)
#define CHECKF(cond, ...) do{ if(!(cond)){ char _m[256]; snprintf(_m, sizeof(_m), __VA_ARGS__); printf("  FAIL: %s (line %d)\n", _m, __LINE__); g_fail++; } }while(0)

// Deterministic PRNG (xorshift32) - the test must fail the same way every run.
static uint32_t g_rng = 0x2E5017A1u;
static uint32_t rnd() { g_rng ^= g_rng << 13; g_rng ^= g_rng >> 17; g_rng ^= g_rng << 5; return g_rng; }

static uint32_t crcOf(const std::string &s) { return ~crcUpdate(CRC_INIT, s.data(), s.size()); }

// A ?STATS-like text: lines of up to `maxLine` characters (each with its '\n'), `len` bytes in all.
static std::string linesText(size_t len, size_t maxLine) {
  std::string s = "\n";
  while (s.size() < len) {
    size_t n = 20 + rnd() % (maxLine - 20);
    if (s.size() + n > len) n = len - s.size();
    if (n == 0) break;
    std::string line(n - 1, 'a' + (char)(rnd() % 26));
    line += '\n';
    s += line;
  }
  return s.substr(0, len);
}

// A sequence value with multi-byte UTF-8 characters strewn through it.
static std::string utf8Text(size_t len) {
  static const char *pieces[] = { "a", "^", ";S1", ",", "\xC3\xA9", "\xE2\x82\xAC", "\xF0\x9F\x98\x80", "*" };
  std::string s;
  while (s.size() < len) s += pieces[rnd() % 8];
  // keep it whole characters: trim back to a lead byte boundary
  while (!s.empty() && s.size() > len) s.pop_back();
  while (!s.empty() && isCont((uint8_t)s.back())) s.pop_back();
  if (!s.empty() && ((uint8_t)s.back() & 0x80)) s.pop_back();
  return s;
}

static std::vector<std::string> split(const Text &t, size_t D, bool lines) {
  std::vector<std::string> parts;
  const unsigned n = partCount(t, D, lines);
  for (unsigned p = 0; p < n; p++) {
    size_t lo = 0, hi = 0;
    if (!partRange(t, D, lines, p, lo, hi)) break;
    std::string s(hi - lo, '\0');
    t.copy(lo, hi - lo, &s[0]);
    parts.push_back(s);
  }
  return parts;
}

// ---- mirror: the firmware's target decision (WCB.ino answerStats / answerSeqVal / sendHeldETMCharResult) ----------
// `asked`: -1 an old one-reply request (7 / 8 / 15), else the part a parts request asks for (0 starts it).
struct Answer { uint8_t type; std::string payload; };
static Answer targetAnswer(uint8_t kind, const std::string &text, int asked, size_t D, uint16_t id) {
  Answer a;
  const Text t(text.data(), text.size());
  if (asked < 0 || fitsOneReply(text.size(), D)) {
    a.type = wholeType(kind);
    if (text.size() <= REPLY_MAX) a.payload = text;
    else if (kind == KIND_SEQVAL) a.payload = "k,TOOBIG,";   // handleSeqValReqPacket's status for an old request
    else a.payload = "[ERROR] Result too large to relay (" + std::to_string(text.size()) + " chars, max 2912). "
                     "Reduce stored variables/sequences or query this board directly.";
    return a;
  }
  const unsigned parts = partCount(t, D, kindIsLines(kind));
  unsigned p = (unsigned)asked;
  if (p >= parts) p = 0;
  size_t lo = 0, hi = 0;
  partRange(t, D, kindIsLines(kind), p, lo, hi);
  Header h = { id, p, parts, (unsigned)text.size(), t.crc() };
  char hdr[HEADER_MAX + 1];
  const int n = formatHeader(hdr, sizeof(hdr), h);
  a.type = partType(kind);
  a.payload = std::string(hdr, n > 0 ? (size_t)n : 0) + text.substr(lo, hi - lo);
  return a;
}

static void test_constants() {
  printf("constants\n");
  Header h = { 0xFFFF, MAX_PARTS - 1, MAX_PARTS, LEN_MAX, 0xFFFFFFFFu };
  char b[64];
  const int n = formatHeader(b, sizeof(b), h);
  CHECKF(n == (int)HEADER_MAX, "the widest header is %d characters, HEADER_MAX says %u", n, HEADER_MAX);
  CHECK(HEADER_MAX + PART_DATA <= REPLY_MAX, "a part fits one reply");
  CHECK(partType(KIND_STATS) == 32 && partType(KIND_ETM) == 33 && partType(KIND_SEQVAL) == 34, "part types");
  CHECK(wholeType(KIND_STATS) == 9 && wholeType(KIND_ETM) == 10 && wholeType(KIND_SEQVAL) == 16, "whole types");
  bool part = false;
  CHECK(kindOfType(32, part) == KIND_STATS && part, "32 is a STATS part");
  CHECK(kindOfType(9, part) == KIND_STATS && !part, "9 is a whole STATS");
  CHECK(kindOfType(10, part) == KIND_ETM && !part && kindOfType(33, part) == KIND_ETM && part, "ETM types");
  CHECK(kindOfType(16, part) == KIND_SEQVAL && !part && kindOfType(34, part) == KIND_SEQVAL && part, "SEQVAL types");
  CHECK(kindOfType(6, part) == 0 && kindOfType(14, part) == 0 && kindOfType(29, part) == 0, "other types are not results");
  CHECK(REQ_ID_AT + 2 + 12 == REQ_LEN && SEQ_KEY_AT + SEQ_KEY_LEN == REQ_LEN, "the request layouts are 59 bytes");
}

static void test_header() {
  printf("header\n");
  for (int i = 0; i < 2000; i++) {
    Header h;
    h.id = (uint16_t)rnd();
    h.parts = 1 + rnd() % MAX_PARTS;
    h.part = rnd() % h.parts;
    h.len = rnd() % (LEN_MAX + 1);
    h.crc = rnd();
    char b[HEADER_MAX + 1];
    const int n = formatHeader(b, sizeof(b), h);
    CHECK(n > 0 && n <= (int)HEADER_MAX, "every header fits HEADER_MAX");
    std::string s(b, n);
    s += "data";
    Header g;
    size_t at = 0;
    CHECK(parseHeader(s.data(), s.size(), g, at), "a header parses back");
    CHECK(g.id == h.id && g.part == h.part && g.parts == h.parts && g.len == h.len && g.crc == h.crc, "fields round-trip");
    CHECK(at == (size_t)n, "the data starts after the ':'");
  }
  Header g;
  size_t at = 0;
  const char *bad[] = {
    "", "1A2B", "1A2B,0,1,5,0000000A", "1a2b,0,1,5,0000000A:", "1A2B,1,1,5,0000000A:", "1A2B,0,0,0,00000000:",
    "1A2B,0,100,5,0000000A:", "1A2B,01,2,5,0000000A:", "1A2B,0,2,65536,0000000A:", "1A2B,0,2,5,000000A:",
    "1A2B,0,2,5,0000000a:", "1A2B,-0,2,5,0000000A:", "1A2B,0,2,5,0000000A;", "1A2,0,2,5,0000000A:",
    "1A2B, 0,2,5,0000000A:", "[ERROR] Result too large to relay (4500 chars, max 2912).",
  };
  for (const char *s : bad) CHECKF(!parseHeader(s, strlen(s), g, at), "'%s' must not parse", s);
  const char *ok = "0000,0,1,0,00000000:";
  CHECK(parseHeader(ok, strlen(ok), g, at) && at == strlen(ok), "an empty result's header parses");
}

static void test_text() {
  printf("two-piece text\n");
  for (int i = 0; i < 300; i++) {
    std::string a = utf8Text(rnd() % 40), b = utf8Text(rnd() % 600);
    const Text t(a.data(), a.size(), b.data(), b.size());
    const std::string whole = a + b;
    CHECK(t.len() == whole.size(), "len");
    CHECK(t.crc() == crcOf(whole), "the CRC of two pieces is the CRC of the whole");
    bool same = true;
    for (size_t k = 0; k < whole.size(); k++) same = same && t.at(k) == (uint8_t)whole[k];
    CHECK(same, "at() reads the whole");
    if (!whole.empty()) {
      const size_t lo = rnd() % whole.size(), n = rnd() % (whole.size() - lo + 1);
      std::string c(n, '\0');
      t.copy(lo, n, &c[0]);
      CHECK(c == whole.substr(lo, n), "copy() across the seam");
    }
  }
  CHECK(Text("", 0).crc() == 0, "the CRC of nothing is 0, as cfgpReplyEnd's of an empty body");
  CHECK(crcOf("123456789") == 0xCBF43926u, "the standard CRC-32 check value");
}

static void test_split_lines() {
  printf("split: lines (STATS, ETM)\n");
  const size_t Ds[] = { PART_DATA_MIN, 600, 1000, PART_DATA };
  for (size_t D : Ds) {
    for (int i = 0; i < 200; i++) {
      const std::string s = linesText(1 + rnd() % 9000, 180);
      const Text t(s.data(), s.size());
      const std::vector<std::string> parts = split(t, D, true);
      std::string join;
      bool ok = true;
      for (size_t k = 0; k < parts.size(); k++) {
        join += parts[k];
        ok = ok && !parts[k].empty() && parts[k].size() <= D;
        if (k + 1 < parts.size()) ok = ok && parts[k].back() == '\n';   // every line here is shorter than D
      }
      CHECK(ok, "every part is 1..D bytes and ends a line");
      CHECK(join == s, "the parts join into the text");
      CHECK(partCount(t, D, true) == parts.size(), "partCount agrees with partRange");
      size_t lo, hi;
      CHECK(!partRange(t, D, true, (unsigned)parts.size(), lo, hi), "no part past the last");
    }
  }
  // A line longer than a part (never in STATS or ETM, whose lines are under 200 characters): cut at D, the text whole.
  std::string longLine(3000, 'x');
  longLine += "\nend\n";
  const Text t(longLine.data(), longLine.size());
  const std::vector<std::string> parts = split(t, 1000, true);
  std::string join;
  for (const std::string &p : parts) join += p;
  CHECK(join == longLine && parts[0].size() == 1000, "a line longer than D is cut at D and still joins");
}

static void test_split_utf8() {
  printf("split: bytes, whole UTF-8 characters (a sequence)\n");
  for (int i = 0; i < 300; i++) {
    const std::string key = "k" + std::to_string(rnd() % 1000) + ",OK,";
    const std::string val = utf8Text(1 + rnd() % 4000);
    const Text t(key.data(), key.size(), val.data(), val.size());
    const size_t D = PART_DATA_MIN + rnd() % (PART_DATA - PART_DATA_MIN + 1);
    const std::vector<std::string> parts = split(t, D, false);
    std::string join;
    bool ok = true;
    for (size_t k = 0; k < parts.size(); k++) {
      join += parts[k];
      ok = ok && !parts[k].empty() && parts[k].size() <= D;
      if (k + 1 < parts.size()) ok = ok && parts[k].size() + UTF8_BACKOFF >= D;
      if (k > 0) ok = ok && !isCont((uint8_t)parts[k][0]);   // no part starts inside a character
    }
    CHECK(ok, "every part is D-3..D bytes (the last 1..D) and starts on a character");
    CHECK(join == key + val, "the parts join into the text");
  }
}

static void test_fits() {
  printf("whole or parts\n");
  CHECK(fitsOneReply(REPLY_MAX, PART_DATA) && !fitsOneReply(REPLY_MAX + 1, PART_DATA), "the default: one reply, 2912");
  CHECK(fitsOneReply(600, 600) && !fitsOneReply(601, 600), "a test part size splits what is longer");
  CHECK(fitsOneReply(2900, PART_DATA + 100), "a part size above the default is the default");
}

static void test_largest() {
  printf("the largest results fit the header\n");
  // ?STATS at the extreme is 6587 bytes, lines up to 174 characters (docs/MGMT_RELAY.md §10): at PART_DATA_MIN.
  for (int i = 0; i < 50; i++) {
    const std::string s = linesText(6587, 175);
    const Text t(s.data(), s.size());
    const unsigned n = partCount(t, PART_DATA_MIN, true);
    CHECKF(n >= 1 && n <= 20, "STATS at 512: %u parts, the bound says 20", n);
    CHECK(partCount(t, PART_DATA, true) == 3, "STATS at the default: 3 parts");
  }
  // A sequence at the extreme: a 15-character key, ",OK,", 3999 characters = 4018 bytes.
  const std::string key = "abcdefghijklmno,OK,", val(3999, 'v');
  const Text t(key.data(), key.size(), val.data(), val.size());
  CHECK(t.len() == 4018, "the longest sequence reply");
  CHECK(partCount(t, PART_DATA, false) == 2 && partCount(t, PART_DATA_MIN, false) == 8, "sequence part counts");
  // The most a result can be is LEN_MAX; at PART_DATA it is 23 parts, inside MAX_PARTS.
  const std::string big(LEN_MAX, 'z');
  CHECK(partCount(Text(big.data(), big.size()), PART_DATA, false) == 23, "LEN_MAX in 23 parts");
}

static void test_assembly() {
  printf("reassembly\n");
  const std::string s = linesText(7000, 160);
  const Text t(s.data(), s.size());
  const size_t D = 1200;
  const std::vector<std::string> parts = split(t, D, true);
  const unsigned K = (unsigned)parts.size();
  CHECK(K >= 6, "enough parts to test");
  Header h0 = { 0x1A2B, 0, K, (unsigned)s.size(), t.crc() };

  Assembly as;
  Header h = h0;
  h.part = 1;
  CHECK(as.check(h) == STEP_STALE, "a later part before part 0 is stale");
  CHECK(as.check(h0) == STEP_OK, "part 0 is taken first");
  as.start(h0);
  std::string join;
  for (unsigned p = 0; p < K; p++) {
    h.part = p;
    if (p > 0) {
      Header old = h0;
      old.part = p - 1;
      CHECK(as.check(old) == STEP_STALE, "the part before is a late copy");
      Header other = h;
      other.id ^= 1;
      CHECK(as.check(other) == STEP_RESTART, "another result's part restarts the pull");
      other = h;
      other.crc ^= 1;
      CHECK(as.check(other) == STEP_RESTART, "another CRC restarts the pull");
      CHECK(as.check(h) == STEP_OK, "the part asked for");
    }
    const Step st = as.take(parts[p].data(), parts[p].size());
    join += parts[p];
    CHECK(st == (p + 1 < K ? STEP_NEXT : STEP_DONE), "NEXT, then DONE on the last");
  }
  CHECK(join == s && as.got == s.size(), "the join is the text");

  // A last part whose bytes do not match the CRC.
  as.start(h0);
  for (unsigned p = 0; p + 1 < K; p++) as.take(parts[p].data(), parts[p].size());
  std::string bad = parts[K - 1];
  bad[bad.size() / 2] ^= 0x20;
  CHECK(as.take(bad.data(), bad.size()) == STEP_RESTART, "a corrupted part fails the CRC on the last part");

  // Too many bytes, too few, an empty part.
  as.start(h0);
  CHECK(as.take("", 0) == STEP_BAD, "an empty part of a non-empty result is ignored");
  std::string fat = parts[0] + std::string(s.size(), 'q');
  CHECK(as.take(fat.data(), fat.size()) == STEP_RESTART, "more bytes than len restarts");
  Header h1 = { 0, 0, 2, 10, crcOf("0123456789") };
  as.start(h1);
  CHECK(as.take("0123456789", 10) == STEP_RESTART, "all the bytes in the first of two parts restarts");
  Header one = { 0, 0, 1, 3, crcOf("abc") };
  as.start(one);
  CHECK(as.take("ab", 2) == STEP_RESTART, "a short last part restarts");
  as.start(one);
  CHECK(as.take("abc", 3) == STEP_DONE, "a one-part result");

  Header wild = h0;
  wild.parts = 0;
  CHECK(as.check(wild) == STEP_BAD, "0 parts is malformed");
  wild = h0;
  wild.parts = MAX_PARTS + 1;
  CHECK(as.check(wild) == STEP_BAD, "too many parts is malformed");
}

static void test_clock() {
  printf("the requester's clock\n");
  Clock c;
  CHECK(!c.active() && c.tick(0) == Clock::T_WAIT, "idle");
  c.begin(KIND_STATS, 2, 1000);
  CHECK(c.tick(1000 + WAIT_FIRST_MS - 1) == Clock::T_WAIT, "waits for the first answer");
  CHECK(c.tick(1000 + WAIT_FIRST_MS) == Clock::T_SILENT, "no first answer: silent, as the one-reply request was");
  c.begin(KIND_ETM, 2, 1000);
  CHECK(c.tick(1000 + 100000) == Clock::T_WAIT && c.tick(1000 + WAIT_FIRST_ETM_MS) == Clock::T_SILENT,
        "an ETM characterization is waited for much longer");
  c.begin(KIND_SEQVAL, 3, 5000);
  c.heard(5800);
  c.startPaging(5800);
  c.asked(5850);     // part 1 asked
  CHECK(c.tick(5850 + RETRY_MS - 1) == Clock::T_WAIT, "a part is given RETRY_MS");
  CHECK(c.tick(5850 + RETRY_MS) == Clock::T_ASK, "then asked again");
  c.again(5850 + RETRY_MS);
  c.heard(5850 + RETRY_MS + 500);   // a frag of it: progress
  CHECK(c.tick(5850 + RETRY_MS + 500 + RETRY_MS - 1) == Clock::T_WAIT, "progress restarts the wait");
  c.again(20000);
  CHECK(c.tries == PART_TRIES && c.tick(20000 + RETRY_MS) == Clock::T_NEVER, "asked 3 times: never came");
  c.begin(KIND_STATS, 2, 0);
  c.startPaging(100);
  c.asked(100);
  uint32_t now = 100;
  Clock::Tick tk = Clock::T_WAIT;
  for (int i = 0; i < 100 && tk != Clock::T_TIMEOUT; i++) {
    now += 2500;
    c.heard(now);
    c.asked(now);
    tk = c.tick(now);
  }
  CHECK(tk == Clock::T_TIMEOUT && now - 100 >= PULL_MAX_MS, "PULL_MAX_MS from part 0 ends any pull");
  // millis() wraps: the clock works on differences only.
  c.begin(KIND_SEQVAL, 3, 0xFFFFF000u);
  CHECK(c.tick(0xFFFFF000u + WAIT_FIRST_MS - 1) == Clock::T_WAIT && c.tick(0xFFFFF000u + WAIT_FIRST_MS) == Clock::T_SILENT,
        "across the millis() wrap");
}

// ---- The whole exchange over a lossy mesh, old and new on either side ---------------------------------------------
// A requester with parts asks part 0 (new request x3, then the old one), takes parts, asks the next, retries a lost
// one; an old requester sends only the old request. The target is new or old. What comes out is what a relay prints.
struct Outcome { bool got; std::string printed; unsigned asks; };

static Outcome exchange(uint8_t kind, const std::string &text, bool newRequester, bool newTarget, size_t D,
                        unsigned lossPct, std::string tag = "[MGMT:STATS,2]") {
  Outcome o = { false, "", 0 };
  if (!newRequester || !newTarget) {
    Answer a = targetAnswer(kind, text, -1, D, 0);     // only the old request is answered
    if (rnd() % 100 < lossPct && rnd() % 100 < lossPct) return o;   // both passes lost
    o.got = true;
    o.printed = tag + a.payload + "\r\n";
    return o;
  }
  Assembly as;
  Clock c;
  uint32_t now = 0;
  c.begin(kind, 2, now);
  const uint16_t id = 0x4C1D;
  unsigned want = 0;
  for (int step = 0; step < 200; step++) {
    o.asks++;
    const bool lost = rnd() % 100 < lossPct && rnd() % 100 < lossPct;   // a reply is lost when both passes are
    now += 800;
    if (!lost) {
      Answer a = targetAnswer(kind, text, (int)want, D, id);
      if (a.type == wholeType(kind)) {
        o.got = true;
        o.printed = tag + a.payload + "\r\n";
        return o;
      }
      Header h;
      size_t at = 0;
      if (!parseHeader(a.payload.data(), a.payload.size(), h, at)) return o;
      c.heard(now);
      const Step chk = as.check(h);
      if (chk == STEP_OK) {
        if (h.part == 0) { as.start(h); c.startPaging(now); }
        const std::string data = a.payload.substr(at);
        const Step st = as.take(data.data(), data.size());
        o.printed += (h.part == 0 ? tag : "") + data;
        if (st == STEP_DONE) {
          o.printed += "\r\n";
          o.got = true;
          return o;
        }
        if (st != STEP_NEXT) return o;
        want = as.next;
        c.asked(now);
        continue;
      }
    }
    // nothing usable: the clock decides
    now += RETRY_MS;
    const Clock::Tick tk = c.tick(now);
    if (tk == Clock::T_SILENT || tk == Clock::T_NEVER || tk == Clock::T_TIMEOUT) return o;
    if (tk == Clock::T_ASK) c.again(now);
  }
  return o;
}

static void test_exchange() {
  printf("the exchange: old and new on either side, a lossy mesh\n");
  const std::string small = linesText(1500, 170), big = linesText(6587, 175);
  const std::string tag = "[MGMT:STATS,2]";
  // Small: everyone gets the whole result, one reply, byte for byte what was always printed.
  for (int r = 0; r < 2; r++)
    for (int t = 0; t < 2; t++) {
      Outcome o = exchange(KIND_STATS, small, r, t, PART_DATA, 0);
      CHECK(o.got && o.printed == tag + small + "\r\n", "a small result goes whole to every pairing");
    }
  // Big, a new requester and a new target: the whole text, in parts, printed as the one line it always was.
  Outcome o = exchange(KIND_STATS, big, true, true, PART_DATA, 0);
  CHECK(o.got && o.printed == tag + big + "\r\n", "a big result arrives whole in parts");
  // Big, either side old: the [ERROR] text, as always (Greg: keep it).
  for (int r = 0; r < 2; r++)
    for (int t = 0; t < 2; t++) {
      if (r && t) continue;
      Outcome x = exchange(KIND_STATS, big, r, t, PART_DATA, 0);
      CHECK(x.got && x.printed.find("[MGMT:STATS,2][ERROR] Result too large to relay (6587 chars, max 2912).") == 0,
            "an old side gets the error text");
    }
  // A lossy mesh: whenever the pull completes, what it printed is the whole text.
  unsigned whole = 0, lostPulls = 0;
  for (int i = 0; i < 400; i++) {
    const std::string s = linesText(3000 + rnd() % 3600, 175);
    Outcome x = exchange(KIND_STATS, s, true, true, 512 + rnd() % 2000, 40);
    if (x.got) {
      whole++;
      CHECK(x.printed == tag + s + "\r\n", "a completed pull is the whole text");
    } else {
      lostPulls++;
    }
  }
  CHECKF(whole > 300, "most lossy pulls complete (%u of 400; %u gave up)", whole, lostPulls);
  // A sequence: "<key>,OK,<value>" up to 4018 bytes.
  const std::string seq = "abcdefghijklmno,OK," + utf8Text(3999);
  const std::string stag = "[MGMT:SEQVAL,3]";
  Outcome q = exchange(KIND_SEQVAL, seq, true, true, PART_DATA, 0, stag);
  CHECK(q.got && q.printed == stag + seq + "\r\n", "a long sequence arrives whole");
  Outcome oldq = exchange(KIND_SEQVAL, seq, false, true, PART_DATA, 0, stag);
  CHECK(oldq.got && oldq.printed == stag + "k,TOOBIG,\r\n", "an old requester still gets TOOBIG");
}

int main() {
  test_constants();
  test_header();
  test_text();
  test_split_lines();
  test_split_utf8();
  test_fits();
  test_largest();
  test_assembly();
  test_clock();
  test_exchange();
  if (g_fail) {
    printf("\n%d check(s) FAILED\n", g_fail);
    return 1;
  }
  printf("\nall result-parts checks passed\n");
  return 0;
}
