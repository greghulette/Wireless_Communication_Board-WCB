// ============================================================================
// Sequence names, paged: split & reassembly SPEC TEST  (host-side, no Arduino)
//
// WHAT THIS IS
//   A regression guard for the stored-sequence names reply (docs/SEQUENCE_INVENTORY.md
//   §3): when a list goes out whole (packet type 14) and when in pages (29), where
//   the pages split, the page header, the one-reply parse that refuses a 7.0.1
//   board's "[ERROR] Result too large" text, and the requester's reassembly - stale
//   copies, a list that changes mid-pull, a lost page asked again. Runs anywhere a
//   C++11 compiler exists (CI: .github/workflows/wdp-tests.yml).
//
// THIS LINKS THE REAL CODE
//   Code/WCB/WCB_SeqNamesWire.h has no Arduino dependency, so it is included as it
//   is: what runs here is what the firmware and WCB_Client (a byte-identical copy,
//   src/WCB_SeqNamesWire.h) run. The one mirror below is targetReply(), the
//   firmware's buildSequenceNamesReply() (WCB_Storage.cpp) without Arduino String -
//   keep the two in step.
//
// BUILD + RUN
//   g++ -std=c++11 -Wall -Wextra -O2 tests/seqnames_page_test.cpp -o seqnames_page_test
//   ./seqnames_page_test      # exit 0 = all pass, 1 = a failure (prints which)
// ============================================================================
#include "../Code/WCB/WCB_SeqNamesWire.h"

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

using namespace seqn;

static int g_fail = 0;
#define CHECK(cond, msg) do{ if(!(cond)){ printf("  FAIL: %s (line %d)\n", msg, __LINE__); g_fail++; } }while(0)
#define CHECKF(cond, ...) do{ if(!(cond)){ char _m[256]; snprintf(_m, sizeof(_m), __VA_ARGS__); printf("  FAIL: %s (line %d)\n", _m, __LINE__); g_fail++; } }while(0)

// Deterministic PRNG (xorshift32) - the test must fail the same way every run.
static uint32_t g_rng = 0x5EC0A3E5u;
static uint32_t rnd() { g_rng ^= g_rng << 13; g_rng ^= g_rng >> 17; g_rng ^= g_rng << 5; return g_rng; }

static std::vector<std::string> randomKeys(unsigned n, unsigned minLen = 1, unsigned maxLen = KEY_MAX) {
  static const char al[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-";
  std::vector<std::string> v;
  for (unsigned i = 0; i < n; i++) {
    const unsigned len = minLen + rnd() % (maxLen - minLen + 1);
    std::string k;
    for (unsigned j = 0; j < len; j++) k += al[rnd() % (sizeof(al) - 1)];
    v.push_back(k);
  }
  return v;
}

static std::string keyText(const std::vector<std::string> &keys) {
  std::string s;
  for (const std::string &k : keys) { s += ','; s += k; }
  return s;
}

static std::string wholeForm(uint32_t hash, const std::vector<std::string> &keys) {
  char h[32];
  snprintf(h, sizeof(h), "%08X,%u", (unsigned)hash, (unsigned)keys.size());
  return std::string(h) + keyText(keys);
}

// ---- mirror: the firmware's buildSequenceNamesReply (WCB_Storage.cpp) --------
enum { R_REPLY, R_NOFIT };
struct Reply { int rc; uint8_t type; std::string text; unsigned pages; };

static Reply targetReply(const std::vector<std::string> &keys, uint32_t hash, int page) {
  Reply r; r.rc = R_REPLY; r.type = 0; r.pages = 0;
  const bool legacy = page < 0;
  const unsigned want = legacy ? 0 : (unsigned)page;
  Pager pg(want);
  std::string text;
  size_t page0End = 0;
  for (const std::string &k : keys) {
    const bool mine = pg.add(k.size());
    if (want == 0) {
      if (pg.keyBytes <= REPLY_MAX) { text += ','; text += k; }
      if (pg.page == 0) page0End = text.size();
    } else if (mine) {
      text += ','; text += k;
    }
  }
  r.pages = pg.pages();
  char hdr[HEADER_MAX + 1];
  int n;
  size_t bodyLen = text.size();
  if (want == 0 && wholeFits(pg.count, pg.keyBytes)) {
    r.type = PT_SEQ_FRAG;
    n = formatWholeHeader(hdr, sizeof(hdr), hash, pg.count);
  } else if (legacy) {
    r.rc = R_NOFIT;
    return r;
  } else {
    r.type = PT_PAGE;
    PageHeader ph; ph.hash = hash; ph.count = pg.count; ph.page = want; ph.pages = pg.pages(); ph.keyBytes = pg.keyBytes;
    n = formatPageHeader(hdr, sizeof(hdr), ph);
    if (want == 0) bodyLen = page0End;
  }
  if (n < 0) { r.rc = -1; return r; }
  r.text = std::string(hdr, (size_t)n) + text.substr(0, bodyLen);
  return r;
}

// ---- a requester (WCB relay / WCB_Client), driven against a list that may change ----
struct Pull {
  Assembly as;
  std::vector<char> buf;
  unsigned asked = 0, restarts = 0, stale = 0;
  bool done = false, whole = false, failed = false;
  std::string result;
};

// One reply to a request for `page`; returns the next page to ask for, or -1 when the pull ended.
static int feed(Pull &p, const Reply &r) {
  if (r.type == PT_SEQ_FRAG) {
    uint32_t h; unsigned c; size_t at;
    if (!parseWhole(r.text.data(), r.text.size(), h, c, at)) { p.failed = true; return -1; }
    p.result = r.text; p.done = p.whole = true;
    return -1;
  }
  PageHeader h; size_t at;
  if (!parsePageHeader(r.text.data(), r.text.size(), h, at)) { p.failed = true; return -1; }
  const Step c = p.as.check(h);
  if (c == STEP_STALE) { p.stale++; return (int)p.as.next; }
  if (c == STEP_BAD) { p.failed = true; return -1; }
  if (c == STEP_RESTART) { p.restarts++; p.as.reset(); return 0; }
  if (h.page == 0) {
    p.buf.assign(wholeLen(h.count, h.keyBytes) + 1, '\0');
    if (!p.as.start(h, p.buf.data(), p.buf.size())) { p.failed = true; return -1; }
  }
  const Step s = p.as.append(r.text.data() + at, r.text.size() - at, p.buf.data(), p.buf.size());
  if (s == STEP_NEXT) return (int)p.as.next;
  if (s == STEP_DONE) { p.done = true; p.result.assign(p.buf.data(), p.as.len); return -1; }
  if (s == STEP_RESTART) { p.restarts++; p.as.reset(); return 0; }
  p.failed = true;
  return -1;
}

static Pull runPull(const std::vector<std::string> &keys, uint32_t hash) {
  Pull p;
  int page = 0;
  while (page >= 0 && p.asked < 200) { p.asked++; page = feed(p, targetReply(keys, hash, page)); }
  return p;
}

// ============================================================================
static void testSizes() {
  printf("sizes and the one-reply boundary\n");
  CHECK(REPLY_MAX == 16 * 182, "one reply = 16 chunks x 182");
  CHECK(HEADER_MAX + PAGE_KEYS_MAX <= REPLY_MAX, "a page fits one reply");
  char hdr[64];
  PageHeader big; big.hash = 0xFFFFFFFFu; big.count = COUNT_MAX; big.page = 255; big.pages = 255; big.keyBytes = KEYBYTES_MAX;
  CHECKF(formatPageHeader(hdr, sizeof(hdr), big) == (int)HEADER_MAX, "the widest header is %u", HEADER_MAX);
  // wholeFits at the exact edge: "XXXXXXXX,<count>" + key text == 2912
  CHECK(wholeLen(0, 0) == 10, "811C9DC5,0 is 10 characters");
  CHECK(wholeFits(100, REPLY_MAX - 12), "8 + 1 + 3 + (2912 - 12) = 2912 fits");
  CHECK(!wholeFits(100, REPLY_MAX - 11), "2913 does not");
  CHECK(PAGE_REQ_LEN == 59 && PAGE_REQ_PAGE_AT == 43, "the page request is the 59-byte request, page at byte 43");
  CHECK(PT_PAGE_REQ == 28 && PT_PAGE == 29 && PT_SEQ_REQ == 13 && PT_SEQ_FRAG == 14, "packet types");
}

static void testSplit() {
  printf("the split: greedy, stable, every page within budget and non-empty\n");
  for (int trial = 0; trial < 300; trial++) {
    const unsigned n = rnd() % 2000;
    const std::vector<std::string> keys = randomKeys(n);
    Pager all(0);
    for (const std::string &k : keys) all.add(k.size());
    const unsigned pages = all.pages();
    std::string joined;
    unsigned seen = 0;
    for (unsigned p = 0; p < pages; p++) {
      Pager pg(p);
      std::string t;
      for (const std::string &k : keys) if (pg.add(k.size())) { t += ','; t += k; }
      CHECKF(pg.pages() == pages && pg.count == all.count && pg.keyBytes == all.keyBytes, "trial %d page %u: totals differ", trial, p);
      CHECKF(t.size() <= PAGE_KEYS_MAX, "trial %d page %u: %u bytes of keys", trial, p, (unsigned)t.size());
      CHECKF(n == 0 || !t.empty(), "trial %d page %u is empty", trial, p);
      // greedy: the next page's first key would not have fitted this one
      if (p + 1 < pages) {
        Pager nx(p + 1);
        std::string first;
        for (const std::string &k : keys) if (nx.add(k.size())) { first = k; break; }
        CHECKF(t.size() + first.size() + 1 > PAGE_KEYS_MAX, "trial %d page %u: '%s' would have fitted", trial, p, first.c_str());
      }
      joined += t;
      unsigned k = 0;
      checkKeyText(t.data(), t.size(), k);
      seen += k;
    }
    CHECKF(joined == keyText(keys), "trial %d: the pages joined are not the list", trial);
    CHECKF(seen == n && all.keyBytes == keyText(keys).size(), "trial %d: count/keyBytes", trial);
  }
  Pager empty(0);
  CHECK(empty.pages() == 1 && empty.count == 0 && empty.keyBytes == 0, "an empty list is one empty page");
}

static void testParse() {
  printf("parsing: the one-reply form, the page header, refusals\n");
  uint32_t h; unsigned c; size_t at;
  const char *e = "811C9DC5,0";
  CHECK(parseWhole(e, strlen(e), h, c, at) && h == 0x811C9DC5u && c == 0 && at == strlen(e), "the empty list");
  const char *w = "A1B2C3D4,3,wave,park,scream";
  CHECK(parseWhole(w, strlen(w), h, c, at) && h == 0xA1B2C3D4u && c == 3 && strcmp(w + at, "wave,park,scream") == 0, "three names");
  const char *lower = "a1b2c3d4,1,x";
  CHECK(parseWhole(lower, strlen(lower), h, c, at) && h == 0xA1B2C3D4u, "lower-case hex");
  // What a 7.0.1 board sends for a list that does not fit: an old WCB_Client read it as an EMPTY inventory.
  const char *err = "[ERROR] Result too large to relay (3201 chars, max 2912). Reduce stored variables/sequences or query this board directly.";
  CHECK(!parseWhole(err, strlen(err), h, c, at), "the [ERROR] text is not an inventory");
  const char *bad[] = {"A1B2C3D4,2,wave", "A1B2C3D4,1,wave,park", "A1B2C3D4,1,", "A1B2C3D4,2,a,,b", "A1B2C3D,1,x",
                       "A1B2C3D4X,1,x", "A1B2C3D4,1,ABCDEFGHIJKLMNOP", "A1B2C3D4,-1", "A1B2C3D4, 1,x", "A1B2C3D4,",
                       "A1B2C3D4", "", "A1B2C3D4,1,x\ny", "A1B2C3D4,123456,x"};
  for (const char *b : bad) CHECKF(!parseWhole(b, strlen(b), h, c, at), "'%s' parsed as an inventory", b);
  CHECK(parseWhole("A1B2C3D4,1,ABCDEFGHIJKLMNO", 26, h, c, at), "a 15-character key");

  PageHeader ph;
  const char *pg = "0BADF00D,400,1,3,6400,k1,k2";
  CHECK(parsePageHeader(pg, strlen(pg), ph, at) && ph.hash == 0x0BADF00Du && ph.count == 400 && ph.page == 1 &&
        ph.pages == 3 && ph.keyBytes == 6400 && strcmp(pg + at, ",k1,k2") == 0, "a page header");
  const char *last = "0BADF00D,400,5,3,6400";   // a page past the last: no keys
  CHECK(parsePageHeader(last, strlen(last), ph, at) && at == strlen(last), "a page past the last has no key text");
  const char *pbad[] = {"0BADF00D,400,1,0,6400", "0BADF00D,400,1,65,6400", "0BADF00D,400,1,3", "0BADF00D,0,0,1,4",
                        "0BADF00D,4,0,1,0", "0BADF00D,4,0,1,7", "0BADF00D,4,0,1,65", "0BADF00D,400,256,3,6400",
                        "0BADF00D,400,1,3,6400k1", "0BADF00D,400,1,3,6400;k1"};
  for (const char *b : pbad) CHECKF(!parsePageHeader(b, strlen(b), ph, at), "'%s' parsed as a page header", b);
  // A page header is not a one-reply list, and the other way round.
  CHECK(!parseWhole(pg, strlen(pg), h, c, at), "a page is not a one-reply list");
  CHECK(!parsePageHeader(w, strlen(w), ph, at), "a one-reply list is not a page");
}

static void testTarget() {
  printf("the target: whole, paged, or (old request) nothing\n");
  const uint32_t H = 0x12345678u;
  // fits: the one-reply form, byte for byte what it always was, to both requests
  const std::vector<std::string> small = randomKeys(20);
  Reply r0 = targetReply(small, H, -1), r1 = targetReply(small, H, 0);
  CHECK(r0.rc == R_REPLY && r0.type == PT_SEQ_FRAG && r0.text == wholeForm(H, small), "old request, small list: type 14");
  CHECK(r1.rc == R_REPLY && r1.type == PT_SEQ_FRAG && r1.text == r0.text, "page request, small list: the same type 14");
  const std::vector<std::string> none;
  CHECK(targetReply(none, 0x811C9DC5u, -1).text == "811C9DC5,0", "an empty store");
  // does not fit: an old request gets nothing, a page request page 0
  const std::vector<std::string> big = randomKeys(400, 15, 15);   // 6400 bytes of key text
  CHECK(targetReply(big, H, -1).rc == R_NOFIT, "old request, 400 names: no answer");
  Reply p0 = targetReply(big, H, 0);
  CHECK(p0.rc == R_REPLY && p0.type == PT_PAGE && p0.pages == 3 && p0.text.size() <= REPLY_MAX, "page request, 400 names: page 0 of 3");
  CHECK(p0.text.compare(0, 23, "12345678,400,0,3,6400,") == 0 || p0.text.compare(0, 22, "12345678,400,0,3,6400,") == 0,
        "page 0's header");
  for (unsigned p = 0; p < 3; p++)
    CHECKF(targetReply(big, H, (int)p).text.size() <= REPLY_MAX, "page %u over one reply", p);
  Reply past = targetReply(big, H, 7);
  PageHeader ph; size_t at;
  CHECK(past.type == PT_PAGE && parsePageHeader(past.text.data(), past.text.size(), ph, at) && at == past.text.size() &&
        ph.pages == 3, "a page past the last: header only");
  // the gap: the whole list fits one reply but not one page (2881..2898 bytes of key text) - whole to both
  std::vector<std::string> gap;
  while (true) {
    Pager pg(0);
    for (const std::string &k : gap) pg.add(k.size());
    if (pg.keyBytes > PAGE_KEYS_MAX) break;
    gap.push_back(randomKeys(1, 15, 15)[0]);
  }
  Pager gp(0);
  for (const std::string &k : gap) gp.add(k.size());
  CHECKF(gp.pages() == 2 && wholeFits(gp.count, gp.keyBytes), "the gap list: 2 pages (%u), fits whole", gp.pages());
  CHECK(targetReply(gap, H, -1).type == PT_SEQ_FRAG && targetReply(gap, H, 0).type == PT_SEQ_FRAG,
        "the gap list goes whole to both requests");
  CHECK(targetReply(gap, H, 0).text == wholeForm(H, gap), "and whole is the whole list");
}

static void testRoundTrip() {
  printf("round trip: every list comes back whole, in one reply or in pages\n");
  unsigned paged = 0;
  for (int trial = 0; trial < 400; trial++) {
    const unsigned n = (trial < 50) ? (unsigned)trial * 9 : rnd() % 2200;
    const std::vector<std::string> keys = randomKeys(n);
    const uint32_t H = rnd();
    Pull p = runPull(keys, H);
    CHECKF(p.done && !p.failed && p.restarts == 0 && p.stale == 0, "trial %d (%u names): done %d failed %d", trial, n, p.done, p.failed);
    CHECKF(p.result == wholeForm(H, keys), "trial %d (%u names): the joined list differs", trial, n);
    Pager pg(0);
    for (const std::string &k : keys) pg.add(k.size());
    const bool fits = wholeFits(pg.count, pg.keyBytes);
    CHECKF(p.whole == fits, "trial %d: whole %d, fits %d", trial, p.whole, fits);
    CHECKF(p.asked == (fits ? 1 : pg.pages()), "trial %d: %u requests for %u pages", trial, p.asked, pg.pages());
    if (!fits) paged++;
  }
  CHECKF(paged > 100, "only %u of the lists were paged", paged);
  // the largest store: 32768 bytes of 15-character keys with 1-character values, 18 bytes a line
  const std::vector<std::string> full = randomKeys(32768 / 18, 15, 15);
  Pager fp(0);
  for (const std::string &k : full) fp.add(k.size());
  CHECKF(fp.pages() <= 12 && fp.pages() <= MAX_PAGES && fp.keyBytes <= KEYBYTES_MAX, "a full store: %u pages, %u bytes",
         fp.pages(), fp.keyBytes);
  Pull pf = runPull(full, 0xCAFEF00Du);
  CHECK(pf.done && pf.result == wholeForm(0xCAFEF00Du, full), "a full store comes back whole");
}

static void testChanges() {
  printf("a list that changes mid-pull, stale copies, malformed pages\n");
  const uint32_t H1 = 0x11111111u, H2 = 0x22222222u;
  std::vector<std::string> keys = randomKeys(500, 15, 15);
  // page 0 from one list, page 1 from another: start over, then finish on the new list
  Pull p;
  int page = feed(p, targetReply(keys, H1, 0));
  CHECK(page == 1, "page 0 taken, page 1 next");
  std::vector<std::string> more = keys;
  more.push_back("NEWONE");
  page = feed(p, targetReply(more, H2, 1));
  CHECK(page == 0 && p.restarts == 1, "a page from another list: start over");
  while (page >= 0) page = feed(p, targetReply(more, H2, page));
  CHECK(p.done && p.result == wholeForm(H2, more), "and the new list comes back whole");
  // the same list, but its hash moved (a value edited): start over too
  Pull q;
  page = feed(q, targetReply(keys, H1, 0));
  page = feed(q, targetReply(keys, H2, 1));
  CHECK(page == 0 && q.restarts == 1, "an edited value (same names, new hash): start over");
  // the list shrank to one reply mid-pull: page 1 comes back past the end of a 1-page list; then page 0 whole
  Pull s;
  page = feed(s, targetReply(keys, H1, 0));
  std::vector<std::string> few(keys.begin(), keys.begin() + 10);
  page = feed(s, targetReply(few, H2, 1));
  CHECK(page == 0 && s.restarts == 1, "the list shrank: start over");
  page = feed(s, targetReply(few, H2, 0));
  CHECK(page == -1 && s.done && s.whole && s.result == wholeForm(H2, few), "and it comes back in one reply");
  // a stale copy of page 0 while waiting for page 1: ignored
  Pull t;
  page = feed(t, targetReply(keys, H1, 0));
  page = feed(t, targetReply(keys, H1, 0));
  CHECK(page == 1 && t.stale == 1 && t.as.next == 1, "a second copy of page 0 is stale");
  // ... and so is a page ahead of the one asked for (page 2 while waiting for page 1)
  page = feed(t, targetReply(keys, H1, 2));
  CHECK(page == 1 && t.stale == 2 && t.as.next == 1, "a page ahead of the one asked for is stale");
  while (page >= 0) page = feed(t, targetReply(keys, H1, page));
  CHECK(t.done && t.result == wholeForm(H1, keys), "and the pull still finishes whole");
  // a malformed page (a key over 15 characters) is BAD and changes nothing
  Assembly as;
  PageHeader h; size_t at;
  Reply r0 = targetReply(keys, H1, 0);
  parsePageHeader(r0.text.data(), r0.text.size(), h, at);
  std::vector<char> buf(wholeLen(h.count, h.keyBytes) + 1);
  CHECK(as.check(h) == STEP_OK && as.start(h, buf.data(), buf.size()), "start");
  const size_t before = as.len;
  const char *junk = ",ABCDEFGHIJKLMNOP";
  CHECK(as.append(junk, strlen(junk), buf.data(), buf.size()) == STEP_BAD && as.len == before && as.next == 0 &&
        buf[before] == '\0', "a key over 15 characters: BAD, nothing taken");
  CHECK(as.append("", 0, buf.data(), buf.size()) == STEP_BAD, "an empty page of a non-empty list: BAD");
  CHECK(as.append("x,y", 3, buf.data(), buf.size()) == STEP_BAD, "key text must start with a comma");
  // commit(): the page's key text copied into place by the caller (the WCB relay, from its chunks)
  memcpy(buf.data() + as.len, r0.text.data() + at, r0.text.size() - at);
  CHECK(as.commit(buf.data(), r0.text.size() - at, buf.size()) == STEP_NEXT && as.next == 1, "commit page 0 in place");
  // a page whose keys overrun what page 0 announced: RESTART
  Assembly ov;
  CHECK(ov.start(h, buf.data(), buf.size()), "start again");
  std::string huge;
  for (unsigned i = 0; i < h.count + 1; i++) huge += ",K";
  CHECK(ov.append(huge.data(), huge.size(), buf.data(), buf.size()) == STEP_RESTART, "more keys than announced: RESTART");
  // a last page that comes up short of the count: RESTART
  std::vector<std::string> two = randomKeys(250, 15, 15);
  Reply a = targetReply(two, H1, 0), b = targetReply(two, H1, 1);
  Assembly sh;
  PageHeader ha, hb; size_t aa = 0, ab = 0;
  CHECK(parsePageHeader(a.text.data(), a.text.size(), ha, aa) && parsePageHeader(b.text.data(), b.text.size(), hb, ab) &&
        ha.pages == 2, "a two-page list");
  std::vector<char> b2(wholeLen(ha.count, ha.keyBytes) + 1);
  sh.start(ha, b2.data(), b2.size());
  CHECK(sh.append(a.text.data() + aa, a.text.size() - aa, b2.data(), b2.size()) == STEP_NEXT, "page 0");
  const size_t cut = b.text.rfind(',');
  CHECK(sh.check(hb) == STEP_OK && sh.append(b.text.data() + ab, cut - ab, b2.data(), b2.size()) == STEP_RESTART,
        "a last page one key short: RESTART");
  CHECK(sh.append(b.text.data() + ab, b.text.size() - ab, b2.data(), b2.size()) == STEP_DONE, "the real last page: DONE");
  CHECK(std::string(b2.data(), sh.len) == wholeForm(H1, two), "joined");
}

int main() {
  testSizes();
  testSplit();
  testParse();
  testTarget();
  testRoundTrip();
  testChanges();
  if (g_fail) { printf("%d FAILED\n", g_fail); return 1; }
  printf("all passed\n");
  return 0;
}
