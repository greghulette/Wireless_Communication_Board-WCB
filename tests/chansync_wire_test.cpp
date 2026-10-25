// Host test for the channel-sync wire format and decisions (Code/WCB/WCB_ChanSyncWire.h).
//   g++ -std=c++11 -Wall -Wextra -O2 tests/chansync_wire_test.cpp -o chansync_wire_test && ./chansync_wire_test
// The same header ships byte-identical in WCB_Client (src/WCB_ChanSyncWire.h); the
// golden bytes below are what both sides must put on the air.
#include "../Code/WCB/WCB_ChanSyncWire.h"
#include <stdio.h>

using namespace chansync;

static int failures = 0;
#define CHECK(cond) do { if (!(cond)) { printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); failures++; } } while (0)

static Key K(uint8_t id, uint8_t rank, uint8_t flags = F_SYNC) { Key k; k.id = id; k.rank = rank; k.flags = flags; return k; }

static void testOrdering() {
  const Key navi = K(20, RANK_NAVICORE), w1 = K(1, 1), w2 = K(2, 2), w3p = K(3, 3, F_SYNC | F_PINNED);
  const Key relay = K(19, RANK_NEVER), relayPinned = K(19, RANK_NEVER, F_PINNED);
  CHECK(better(navi, w1));            // NaviCore > WCB1 > WCB2
  CHECK(better(w1, w2));
  CHECK(!better(w2, w1));
  CHECK(better(w3p, navi));           // pinned beats rank: an AP's channel cannot be moved from here
  CHECK(better(w2, relay));           // RANK_NEVER never leads...
  CHECK(better(w2, relayPinned));     // ...even pinned
  CHECK(better(K(4, 7), K(9, 7)));    // a shared rank falls to the lower id
  CHECK(!better(w1, w1));
  CHECK(better(w2, noKey()));
  CHECK(!better(noKey(), w2));
}

static void testTlv() {
  ChanFacts f; f.channel = 6; f.rank = 1; f.flags = F_SYNC | F_PINNED; f.cc[0] = 'D'; f.cc[1] = 'E'; f.cc[2] = 0;
  uint8_t buf[16]; memset(buf, 0xEE, sizeof(buf));
  int o = putChanTlv(buf, 0, sizeof(buf), f);
  const uint8_t golden[] = { 0x14, 0x05, 0x06, 0x01, 0x03, 'D', 'E' };
  CHECK(o == 7);
  CHECK(memcmp(buf, golden, sizeof(golden)) == 0);
  ChanFacts g; CHECK(parseChanTlv(buf + 2, buf[1], g));
  CHECK(g.channel == 6 && g.rank == 1 && g.flags == (F_SYNC | F_PINNED) && strcmp(g.cc, "DE") == 0);
  CHECK(!parseChanTlv(buf + 2, 4, g));                  // short value
  CHECK(putChanTlv(buf, 10, 16, f) == 10);              // does not fit: offset unchanged
  const uint8_t junk[] = { 1, 2, 3, ',', 0 };
  CHECK(parseChanTlv(junk, 5, g) && g.cc[0] == '?' && g.cc[1] == '?');   // scrubbed: it lands in dump lines
}

static void testFrames() {
  Move m; m.newChannel = 6; m.rank = 3; m.flags = F_SYNC | F_PINNED; m.moveId = 0xBEEF; m.delayMs = 1500;
  m.cc[0] = '0'; m.cc[1] = '1'; m.cc[2] = 0;
  uint8_t b[200]; memset(b, 0, sizeof(b));
  CHECK(encodeMove(b, sizeof(b), m) == MOVE_LEN);
  const uint8_t gMove[] = { 'C', 0x01, 6, 3, 0x03, 0xEF, 0xBE, 0xDC, 0x05, '0', '1' };
  CHECK(memcmp(b, gMove, sizeof(gMove)) == 0);
  Move m2; CHECK(decodeMove(b, 200, m2));
  CHECK(m2.newChannel == 6 && m2.moveId == 0xBEEF && m2.delayMs == 1500 && strcmp(m2.cc, "01") == 0);
  b[0] = 'W'; CHECK(!decodeMove(b, 200, m2));          // a WDP advert is not a MOVE
  b[0] = 'C'; b[1] = 2; CHECK(!decodeMove(b, 200, m2)); // unknown version
  CHECK(!decodeMove(gMove, MOVE_LEN - 1, m2));

  Probe p; p.homeChannel = 1; p.rank = 2; p.flags = F_SYNC; p.nonce = 0x1234;
  CHECK(encodeProbe(b, sizeof(b), p) == PROBE_LEN);
  const uint8_t gProbe[] = { 'C', 0x01, 1, 2, 0x02, 0x34, 0x12 };
  CHECK(memcmp(b, gProbe, sizeof(gProbe)) == 0);
  Probe p2; CHECK(decodeProbe(b, 200, p2) && p2.nonce == 0x1234 && p2.homeChannel == 1);

  Reply r; r.channel = 6; r.nonce = 0x1234; r.rank = 1; r.flags = F_SYNC; r.best = K(20, 0);
  r.cc[0] = 'U'; r.cc[1] = 'S'; r.cc[2] = 0;
  CHECK(encodeReply(b, sizeof(b), r) == REPLY_LEN);
  const uint8_t gReply[] = { 'C', 0x01, 6, 0x34, 0x12, 1, 0x02, 20, 0, 0x02, 'U', 'S' };
  CHECK(memcmp(b, gReply, sizeof(gReply)) == 0);
  Reply r2; CHECK(decodeReply(b, 200, r2) && r2.best.id == 20 && r2.best.rank == 0 && strcmp(r2.cc, "US") == 0);
}

static void testJudgeMove() {
  Move m; memset(&m, 0, sizeof(m)); m.newChannel = 6; m.rank = 1; m.flags = F_SYNC;
  const Key w1 = K(1, 1), w2 = K(2, 2), w3 = K(3, 3), navi = K(20, 0);
  // WCB1 leads a group of WCB2 + WCB3: both follow it.
  CHECK(judgeMove(m, w1, w2, w1, 1, 11, true) == MV_ACCEPT);
  // An unpinned WCB3 announcing while WCB1 is heard: WCB1 outranks it, so WCB2 refuses.
  Move m3 = m; m3.rank = 3;
  CHECK(judgeMove(m3, w3, w2, w1, 1, 11, true) == MV_OUTRANKED);
  // ...and WCB1 itself refuses too (it outranks the sender).
  CHECK(judgeMove(m3, w3, w1, w1, 1, 11, true) == MV_OUTRANKED);
  // A pinned WCB3 outranks NaviCore: NaviCore follows it.
  const Key w3p = K(3, 3, F_SYNC | F_PINNED);
  Move mp = m; mp.rank = 3; mp.flags = F_SYNC | F_PINNED;
  CHECK(judgeMove(mp, w3p, navi, navi, 1, 11, true) == MV_ACCEPT);
  // A visiting better leader pulls a group whose own leader is worse.
  Move pull = m; pull.rank = 0; pull.newChannel = 1;    // NaviCore, home on 1, visits the group on 6
  CHECK(judgeMove(pull, navi, w2, w1, 6, 11, true) == MV_ACCEPT);
  // Region, same channel, sync off.
  Move m12 = m; m12.newChannel = 12;
  CHECK(judgeMove(m12, w1, w2, w1, 1, 11, true) == MV_BAD_CHANNEL);
  CHECK(judgeMove(m12, w1, w2, w1, 1, 13, true) == MV_ACCEPT);
  Move m14 = m; m14.newChannel = 14;
  CHECK(judgeMove(m14, w1, w2, w1, 1, 14, true) == MV_BAD_CHANNEL);   // 14 is never used
  CHECK(judgeMove(m, w1, w2, w1, 6, 11, true) == MV_SAME_CHANNEL);
  CHECK(judgeMove(m, w1, w2, w1, 1, 11, false) == MV_SYNC_OFF);
  // An operator move: anyone may send it; an associated board still refuses.
  Move op = m; op.rank = 3; op.flags = F_SYNC | MF_OPERATOR;
  CHECK(judgeMove(op, w3, w2, w1, 1, 11, true) == MV_ACCEPT);
  const Key w2p = K(2, 2, F_SYNC | F_PINNED);
  CHECK(judgeMove(op, w3, w2p, w2p, 1, 11, true) == MV_PINNED_SELF);
  // A pinned board leaves only for a pinned device that outranks it.
  CHECK(judgeMove(m, w1, w2p, w2p, 1, 11, true) == MV_PINNED_SELF);
  Move mpw1 = m; mpw1.flags = F_SYNC | F_PINNED;
  CHECK(judgeMove(mpw1, K(1, 1, F_SYNC | F_PINNED), w2p, w2p, 1, 11, true) == MV_ACCEPT);
}

static void testSearch() {
  const Key w1 = K(1, 1), w2 = K(2, 2), navi = K(20, 0);
  CHECK(shouldSearch(w1, w1, true, true, true));        // alone or leading, a board is missing
  CHECK(!shouldSearch(w2, w1, true, true, true));       // a follower never searches
  CHECK(!shouldSearch(w1, w1, false, true, true));      // nobody missing
  CHECK(!shouldSearch(w1, w1, true, false, true));      // sync off
  CHECK(!shouldSearch(w1, w1, true, true, false));      // cannot hop (AP with stations, JOIN busy)
  CHECK(!shouldSearch(K(19, RANK_NEVER), K(19, RANK_NEVER), true, true, true));

  Key found[MAX_CHANNEL_CAP + 1];
  for (int i = 0; i <= MAX_CHANNEL_CAP; i++) found[i] = noKey();
  SearchResult r = judgeSearch(w1, 1, found);
  CHECK(r.moveTo == 0 && r.pullCount == 0);             // nobody anywhere
  found[6] = navi;                                      // NaviCore's group is on 6: go there
  found[9] = w2;
  r = judgeSearch(w1, 1, found);
  CHECK(r.moveTo == 6 && r.pullCount == 0);
  found[6] = noKey();                                   // only a worse group on 9: pull it home
  r = judgeSearch(w1, 1, found);
  CHECK(r.moveTo == 0 && r.pullCount == 1 && r.pull[9] == 1);
  found[1] = navi;                                      // the home channel is never a result
  r = judgeSearch(w1, 1, found);
  CHECK(r.moveTo == 0);
}

static void testSchedule() {
  CHECK(searchAttemptOffsetMs(0) == 0);
  CHECK(searchAttemptOffsetMs(1) == 60000UL);
  CHECK(searchAttemptOffsetMs(6) == 32UL * 60000UL);
  CHECK(searchAttemptOffsetMs(7) == 0xFFFFFFFFUL);
}

int main() {
  testOrdering();
  testTlv();
  testFrames();
  testJudgeMove();
  testSearch();
  testSchedule();
  if (failures) { printf("%d FAILURE(S)\n", failures); return 1; }
  printf("chansync_wire_test: all passed\n");
  return 0;
}
