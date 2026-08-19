# WCB Code Review — verification verdicts

Companion to [CODE_REVIEW_2026-08.md](CODE_REVIEW_2026-08.md). Each S1/S2 finding was given
to a fresh adversarial verifier told to assume it was wrong, open the cited file, re-derive
every constant itself, grep for guards and callers, and check comments + git history for
deliberate intent.

**Progress: 128 of 128 verified.** The rest were killed by a session limit
and can be resumed — completed verdicts replay from cache, so only the unfinished ones re-run.

| | |
|---|---|
| Verdicts returned | 128 |
| CONFIRMED | 124 |
| REFUTED | 4 |
| UNCERTAIN | 0 |
| **Severity revised** | **92** |
| **Proposed fix was WRONG** | **65** |

The headline: findings survive, **severities do not**. 92 of 128 had the wrong severity and 65 shipped a fix that would not have worked.
Fix from the verdict below, not from the original finding.

---


## CONFIRMED

### `Code/WCB/WCB.ino:577` — CONFIRMED · S1

**etmCharPhaseSentTimes is sized [3][200] but is indexed by etmCharMessageCount x peerCount — a fleet of 11+ online boards overruns it**

**Correction to the finding as filed:**

The observation is right; three parts of the characterization need fixing.

1. TRIGGER IS MUCH WIDER THAN "11+ BOARDS". The real condition is `etmCharMessageCount * peerCount > 200`. Since `?ETM,COUNT` accepts up to 200 (WCB.ino:4696), `?ETM,COUNT,200` + `?ETM,CHAR` on a 3-board mesh (2 online peers, totalMessages=400) overruns by 800 bytes. Stating "requires a 12-board fleet" makes this look far more exotic than it is and would invite a wrong triage.

2. ONLY PHASE 3 ACTUALLY LEAVES THE OBJECT. The array is 2400 bytes (unsigned long is 4 bytes on xtensa32, not 8). Phase 1 (row 0) stays in-object until index 600 and phase 2 (row 1) until 400 — those indices corrupt the *other phases' timestamps*, producing a garbage latency report but no memory corruption. `etmCharPhaseSentTimes[2][200]` (WCB.ino:1506, :1513) is the first address past the end. The finding should be framed as "phase 3 writes out of bounds; phases 1/2 cross-corrupt the array" rather than one undifferentiated overrun.

3. THE NAMED VICTIM GLOBALS ARE UNVERIFIED. etmCharPhaseMessageIndex/etmCharPhaseStartTime/etmLoadRunning are merely the next source-order declarations (WCB.ino:578-586); .bss ordering is a linker choice. Assert the OOB write, not the specific casualty.

Also worth recording: the run is guaranteed to *reach* phase 3 whenever totalMessages > 200, because the ack guard at WCB.ino:1569 drops mIdx>=200, making `allDone` (:1485, :1531) unsatisfiable so every phase exits by timeout having sent all totalMessages.

BETTER FIX than the proposed one — clamp per-board, which preserves the documented semantic instead of silently shrinking it:

    int perBoard = min(etmCharMessageCount, 200 / peerCount);
    int totalMessages = perBoard * peerCount;   // provably <= 200

peerCount is at most 20, so `200 / peerCount` is at least 10, and etmCharMessageCount's floor is also 10 (constrain at :4696/:5303) — perBoard can never be 0. Print a warning in startETMChar when perBoard < etmCharMessageCount so the operator knows the sample was reduced, and fix the header at WCB.ino:1436 ("Messages per board per phase") to report perBoard rather than the requested count.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived from source, not the summary.

Declaration (WCB.ino:576-577):
  ETMCharBoardResult etmCharBoardResults[3][MAX_WCB_COUNT];
  unsigned long etmCharPhaseSentTimes[3][200];  // [phase][msgIndex]
The stale comment above it says "[board index 0-7]" but MAX_WCB_COUNT is 20 (WCB.ino:119-120, mirrored in WCB_OTA.h:64 and WCB_Storage.h:12). On xtensa32 `unsigned long` is 4 bytes, so the object is 3*200*4 = 2400 bytes, rows of 800.

The index is per-PHASE, not per-board, exactly as claimed. WCB.ino:1445-1456:
  int peers[MAX_WCB_COUNT]; int peerCount = 0;
  for (int i = 1; i <= MAX_WCB_COUNT; i++) if (wcbPeerActive[i-1] && boardTable[i-1].online) peers[peerCount++] = i;
  int totalMessages = etmCharMessageCount * peerCount;
peerCount is genuinely up to 20 (loop bound = MAX_WCB_COUNT; `peers[]` itself is correctly sized). etmCharMessageCount defaults to 20 (WCB.ino:269) and is `constrain(..., 10, 200)` at both setters (WCB.ino:4696 `?ETM,COUNT`, WCB.ino:5303 `etmcharcount`). I checked for an unclamped back door: loadETMSettings does `getInt("etmCharCount", 20)` unclamped (WCB_Storage.cpp:2231), but the only writer is WCB_Storage.cpp:2218 putInt from the already-constrained global — grep for "etmCharCount" returns exactly those two lines. So the true worst case is totalMessages = 200 * 20 = 4000.

All three write sites sit inside `if (etmCharPhaseMessageIndex < totalMessages)` with no second bound — I read them in context (WCB.ino:1461-1470 for phases 1/2; WCB.ino:1498-1513 for phase 3, both the broadcast branch :1506 and the unicast branch :1513). Index runs 0..totalMessages-1.

Row arithmetic (this is where the finding is right but under-explained):
- Phase 1 writes row [0]: stays inside the 2400-byte object until index 600.
- Phase 2 writes row [1]: inside until index 400.
- Phase 3 writes row [2]: ESCAPES THE OBJECT AT INDEX 200. &[2][200] == base+2400 == one past the end.
So phases 1/2 merely cross-corrupt the other phases' timestamps (garbage latency report); phase 3 is the real out-of-bounds .bss write.

Magnitudes: default count=20 with 11 peers → totalMessages=220 → indices 200..219 → 20 dwords / 80 bytes past the object (reviewer's number is right). Worst case count=200, peerCount=20 → index 3999 → base+(400+3999)*4 = base+17596, i.e. 15,196 bytes past the 2400-byte object.

THE REVIEWER UNDERSTATED REACHABILITY. "11+ online boards" is only the stock-config trigger. Because `?ETM,COUNT` accepts up to 200, the condition is simply etmCharMessageCount * peerCount > 200, so `?ETM,COUNT,200` then `?ETM,CHAR` on a THREE-board mesh (2 online peers → 400) overruns row 2 by 200 dwords / 800 bytes. startETMChar's only gates are etmEnabled and `Default_WCB_Quantity < 2` (WCB.ino:1408-1412), and processETMChar's only gate is peerCount != 0 (WCB.ino:1450). Nothing clamps against 200 anywhere.

REACHABILITY of the trigger — both paths are first-class operator features:
- Local: `?ETM,CHAR` (WCB.ino:4704) and `etmchar` (WCB.ino:5311), both in processLocalCommand, reached from any serial source via WCB.ino:4381.
- Remote: `?MGMT,ETM,CHAR,<target>` → PACKET_TYPE_ETM_REQ (WCB.ino:234, :3521) → handleETMReqPacket → startETMChar() (WCB.ino:3245-3246), password-checked at :3238.
- The Wizard exposes it as a button: Wizard/app.js:11953 (`${lfi}ETM,CHAR`) and :11982 (the relay form).

INTENT — git blame settles that this is an oversight, not a trade-off. `git log -L 577,577` shows commit 1ea6312 changed
  unsigned long etmCharPhaseSentTimes[200];  // timestamps when each message was sent (max 200 msgs)
into
  unsigned long etmCharPhaseSentTimes[3][200];  // [phase][msgIndex]
The 200 was originally the cap on etmCharMessageCount itself (which really is constrained to <=200). When the run went per-board (totalMessages = count * peerCount) the second dimension was never re-derived. Corroborating evidence that 200 is a known-and-forgotten limit: the READ side is explicitly guarded — `if (phase >= 0 && mIdx >= 0 && mIdx < 200)` at WCB.ino:1249, and `if (mIdx < 0 || mIdx >= 200) return;  // guard against out-of-bounds sentTime read` at WCB.ino:1569. The author bounded the read and missed the write.

A CONSEQUENCE THE REVIEWER MISSED, which makes phase 3 reliably reached: because the ack path drops mIdx>=200 (:1569), `totalAcked` can never reach `totalMessages` when totalMessages > 200, so `allDone` at :1485 is unsatisfiable and phases 1/2 always exit via `timedOut` instead. phaseTimeout scales with totalMessages (:1483, :1529), so the run is slow (~2-4 min/phase) but does advance 1→2→3 via advanceETMCharPhase (:1540-1552) and does reach the escaping writes. Phase 3's allDone (`totalAcked >= totalSent`, :1531) is likewise unsatisfiable, so it too runs to timeout, sending every one of totalMessages.

The one detail I could NOT verify is which globals get stomped. The finding (and the wave doc) name etmCharPhaseMessageIndex / etmCharPhaseStartTime / etmLoadRunning — those are the next declarations in source order (WCB.ino:578-586), but .bss layout is not guaranteed to follow source order. The OOB write itself is certain; the specific victim is not. Notably, if it does land on etmCharPhaseMessageIndex, writing a millis() value there makes the index huge and self-terminates the send loop — so the smallest case may be partly self-limiting, while the large-count case walks 15 KB regardless.

SEVERITY. The repo's own rubric (docs/CODE_REVIEW_2026-08.md:10) is outcome-based: "S1 crashes / corrupts / bricks · S2 wrong behaviour in a real scenario". This is a literal out-of-bounds write into .bss, remotely triggerable over an authenticated mgmt relay and exposed as a Wizard button, scaling to ~15 KB past the object. That is "corrupts", so S1 holds under this rubric — though it is at the mild end of S1, since it lives on a deliberately-invoked diagnostic path rather than any normal operating path.

</details>

### `Code/WCB/WCB.ino:1470` — CONFIRMED · S1

**etmCharPhaseSentTimes[3][200] is indexed by etmCharMessageCount × peerCount with no bound check — global out-of-bounds write**

**Correction to the finding as filed:**

The defect stands; four numbers and one consequence in the write-up need correcting.

1. Byte math is wrong by 2x — `unsigned long` is 4 bytes on ESP32/S3, not 8. Phase 1 writing indices 200..219 is 80 bytes past the row, not 160; `?ETM,COUNT,200` with 2 peers walking 200..399 on row 2 is 800 bytes past the object, not 1600.

2. The reviewer's headline scenario understates *which* write actually leaves the object. Row 0 (phase 1) indices 200..599 and row 1 (phase 2) indices 200..399 land inside the same array — they silently clobber the OTHER phases' sentTimes (bad data, wrong latency report) but stay in-object. The write that genuinely runs past the end of the 2400-byte .bss object is the row-2 (phase 3) one at WCB.ino:1506/:1513, which escapes the moment msgIdx >= 200. Row 1 escapes at idx >= 400, row 0 at idx >= 600. So on an 11-peer fleet at default count the "160 bytes past the row" claim for phase 1 is really intra-array corruption, and the true out-of-object write is the 80 bytes from phase 3 later in the same run.

3. Worst case is count 200 x 19 peers = 3800 messages (peerCount excludes self — WCB.ino:6860), so phase 3 reaches flat slot 4199, about 14.4 KB past the end of the array — a large .bss/heap smear of millis() values, not the "3799" figure. The overrun is bounded by totalMessages (send loops stop at WCB.ino:1461/:1498), not unbounded.

4. Which globals get hit cannot be asserted from source order — .bss layout is the linker's choice, so "clobbers etmCharPhaseMessageIndex/etmLoadRunning" is speculation. The provable statement is: millis()-valued 4-byte stores into unspecified adjacent .bss/heap on a chip with no write protection.

5. Missing consequence the fix should cover: because both read sites already discard mIdx >= 200 (WCB.ino:1250, :1569), messages beyond 200 never register an ACK, so `totalAcked >= totalMessages` (WCB.ino:1485) can never become true and EVERY phase runs to its full timeout. count=200 / 2 peers stalls the board's characterization for ~11.6 minutes of wall clock after ~2 minutes of actual sending. A write-guard-only patch leaves this in place; clamping totalMessages fixes both. Recommend the clamp as the primary fix (`int totalMessages = min(etmCharMessageCount * peerCount, 200);` at WCB.ino:1456) with the three write guards as defense in depth, plus a one-line comment recording why 200 is the ceiling — this is exactly the "trap someone could walk into again" class the repo's doc rule calls out (the read guard already existed and the write still got it wrong once).

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived from source, not the summary.

Declaration: `unsigned long etmCharPhaseSentTimes[3][200];  // [phase][msgIndex]` (Code/WCB/WCB.ino:577). On the xtensa ESP32/ESP32-S3 targets `unsigned long` is 4 bytes (ILP32; the codebase itself assumes 32-bit millis — see the wraparound comment at WCB.ino:288 "millis() exceeds INT32_MAX"), so the whole object is 3*200*4 = 2400 bytes.

Bound: `int totalMessages = etmCharMessageCount * peerCount;` (WCB.ino:1456). Neither factor is clamped against 200:
- `peerCount` is built by walking all MAX_WCB_COUNT slots (WCB.ino:1445-1449, `int peers[MAX_WCB_COUNT]`), with `#define MAX_WCB_COUNT 20` (WCB.ino:120). Self is excluded from wcbPeerActive[] (`rebuildActivePeers`, WCB.ino:6860 `&& (i + 1 != WCB_Number)`), so peerCount tops out at 19.
- `etmCharMessageCount` defaults to 20 (WCB.ino:269) and is user-settable to 10..200 via `constrain(etmVal.toInt(), 10, 200)` at WCB.ino:4696 (`?ETM,COUNT,n`) and WCB.ino:5303 (legacy `?ETMCHARCOUNTn`). The 10-200 range is advertised to users in help: WCB_Help.cpp:270-271 "COUNT,n  Test messages per board per phase during CHAR / Range: 10-200, Default: 20". It is also pushed by the Wizard (Wizard/parser.js:1493 `ETM,COUNT,${etm.messageCount}`) and persisted to NVS (WCB_Storage.cpp:2218/2231).

Writes are unguarded at all three sites — WCB.ino:1470 (phases 1 and 2, row `etmCharPhase - 1`), WCB.ino:1506 and :1513 (phase 3, row 2) — with the index running 0..totalMessages-1 (the send gates are `if (etmCharPhaseMessageIndex < totalMessages)` at :1461 and :1498, and the increments at :1474/:1518 are purely time-gated by interDelay, not gated on ACK or on success).

The asymmetry is real and is the tell: BOTH read sites are guarded at 200 — WCB.ino:1249-1251 `if (phase >= 0 && mIdx >= 0 && mIdx < 200)` and WCB.ino:1569 `if (mIdx < 0 || mIdx >= 200) return;  // guard against out-of-bounds sentTime read`. The 200 bound was known; only the reads got it.

REACHABILITY — `startETMChar()` has three entry points: `?ETM,CHAR` (WCB.ino:4704), legacy `?ETMCHAR` (WCB.ino:5311), and remotely via a relay's ETM_REQ packet (`handleETMReqPacket` → WCB.ino:3246), which is what the Wizard's remote button uses (Wizard/app.js:11982 `MGMT,ETM,CHAR,${targetWCBNum}`; local button at app.js:11953). `processETMChar()` is driven every loop() (WCB.ino:7488). The only precondition checks are `etmEnabled` and `Default_WCB_Quantity >= 2` (WCB.ino:1406-1413) — neither bounds totalMessages. Phase 3 is always entered (`advanceETMCharPhase`, WCB.ino:1541-1551) regardless of whether the ETMLOAD trigger at :1495 is delivered, so the row-2 writes at :1506/:1513 always run.

Nothing cuts the run short before the index passes 200: with count=200 and 2 peers, sends take 400*100ms = 40s while `phaseTimeout` (WCB.ino:1529) is 400*(100+500)+5000 = 245s (etmCharDelayMs default 100, WCB.ino:270; etmTimeoutMs default 500, WCB.ino:268). Every one of the totalMessages writes happens.

INTENT — no comment or doc sanctions this. git blame: the 200-cap read guard came in with 8588cb6 "added support for up to 20 WCBs in a network", the same commit that raised MAX_WCB_COUNT — i.e. the peer-count increase is exactly what made the product overflow, and the author patched the read but not the write. Nothing in docs/ or CLAUDE.md covers it. (docs/CODE_REVIEW_2026-08*.md do mention it, but those are this same unverified review checked in — not independent evidence.)

Verified the overrun is bounded, not unbounded: it stops at totalMessages-1. Worst case is count=200 x 19 peers = 3800, phase 3 → flat slot 400+3799 = 4199, i.e. ~14.4 KB past the end of a 2400-byte .bss object, written as millis() values.

FIX CHECK — the clamp is the essential half and is safe. `buildETMCharResultsString` (WCB.ino:1799-1836) computes everything from per-board `r.sent`/`r.acked` and never divides by etmCharMessageCount, so an uneven per-board split (200 % peerCount != 0) does not corrupt the report. Arduino's `min()` macro on two ints is fine here. The write guards alone would stop the corruption but leave a real secondary defect in place (see correction).

</details>

### `Code/WCB/WCB.ino:2617` — CONFIRMED · S2 → **S1** · ⚠ PROPOSED FIX IS WRONG

**handleMgmtForward silently truncates every MGMT FRAG payload to 179 chars instead of rejecting or re-fragmenting**

**Correction to the finding as filed:**

The observation and the line cite are correct; three corrections to the framing, and the severity is understated.

1. This is one bug with two sides, and the write-up files only the firmware side. The multi-chunk corruption is a **sender/receiver mismatch**: `Wizard/app.js:67` fragments at 180, the relay carries 179. Fixing only the firmware guard does not fix the push path — `MGMT_CHUNK_SIZE` must become 179 (matching `WCBClient/src/WCB_Client.h:189`, which already documents 179 with the reason).

2. Case (a) needs re-fragmentation, not just rejection. The correct model already exists in-ecosystem: `MgmtRelay.ino:404-407` auto-fragments a too-long single-chunk command. Either the Wizard fragments `?SEQ,SAVE` when `cmd.length > 198` (`WCB.ino:2597`), or the relay re-fragments. Rejection alone leaves the feature broken, only loudly.

3. Severity S2 -> S1. Silent data corruption on the primary remote-management path, with zero diagnostic on either end, a green success toast (`app.js:4458`), and a baseline patch (`:4468-4474`) that hides the damage from the next diff push. Every relayed config push over 180 chars is affected, which is effectively all of them.

4. Note for whoever fixes it: `MGMT_PAYLOAD_SIZE` is 180 and the receive side already copies 180 into a 181-byte slot (`WCB.ino:838`, `:848`, `:2693-2694`), so a `memcpy` of up to 180 on the send side would also work — but that changes the wire contract `WCBClient` has pinned at 179, so keep 179 and fix the senders.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived from source, not the summary.

- `espnow_struct_mgmt` is declared at `Code/WCB/WCB.ino:786-794`; the field is `char payload[180];` (`:793`). So `sizeof(pkt.payload) - 1` == **179**. The forward at `WCB.ino:2617-2618` is exactly `strncpy(pkt.payload, payload.c_str(), sizeof(pkt.payload) - 1); pkt.payload[sizeof(pkt.payload) - 1] = '\0';` — 179 data chars max, no length check, no log, no error return.
- The only validation above it is `WCB.ino:2583`: `targetWCB < 1 || targetWCB > MAX_WCB_COUNT || totalChunks == 0 || totalChunks > MGMT_MAX_CHUNKS`. Nothing tests `payload.length()`. Confirmed by reading the whole function `handleMgmtForward` (`WCB.ino:2535-2628`).
- The ETM shortcut gate is `WCB.ino:2597`: `if (totalChunks == 1 && payload.length() <= 198)`. A single-chunk payload of 199+ chars falls through to the truncating broadcast branch. On the target, `expectedMask = (1 << 1) - 1 = 1` (`:2702`), so the one truncated chunk immediately satisfies `receivedMask == expectedMask` (`:2703`) and the mangled command executes as if complete.
- The firmware's own two halves disagree, which is the tell that 179 is an off-by-one and not a design: `#define MGMT_PAYLOAD_SIZE 180` (`:838`), reassembly buffer `char chunks[MGMT_MAX_CHUNKS][MGMT_PAYLOAD_SIZE + 1]` (`:848`), and the receive copy takes a full **180** (`:2693-2694`). The receiver is built for 180 data chars; the sender caps at 179.

REACHABILITY — two live paths, both routine.

(a) Single-chunk > 198. `handleMgmtForward` has exactly one caller, `WCB.ino:5091` (`rootUpper == "MGMT"`), fed from serial. The parser deliberately keeps the whole `?MGMT,...` line intact and never splits it on `^` (`WCB.ino:2079-2093`), the serial line buffer is an unbounded `String` (`:6381-6420`), and `enqueueCommand` mallocs the exact length (`:1926-1934`) — so nothing upstream truncates first. In the Wizard, `updateSequence` builds `const cmd = \`${funcChar}SEQ,SAVE,${key},${value}\`` (`Wizard/app.js:4446`) and ships it as a single chunk, `...,0,1,${cmd}` (`:4455`), with no fragmentation and no length test. The value textarea has **no** maxlength (`app.js:4226`; only the key is capped at 15). With a 4-char key the header is 15 chars, so any sequence body over ~184 chars trips it — and `docs/SEQUENCE_INVENTORY.md:190` states a single stored value can run to ~1800 chars, so long sequences are expected. `Wizard/app.js:8901` (remote terminal) forwards any hand-typed command the same way, so a pasted long command hits it too.

(b) Multi-chunk push. `const MGMT_CHUNK_SIZE = 180` (`Wizard/app.js:67`) feeds `fragmentString(cmdToSend, MGMT_CHUNK_SIZE)` (`:7852`), which slices exactly 180 chars per chunk (`:7761-7767`). Every non-final chunk is therefore 180 chars and loses its last character at `WCB.ino:2617`. A full remote config push is far longer than 180 chars, so this corrupts essentially every relayed push.

CROSS-REPO CORROBORATION. `WCBClient/src/WCB_Client.h:189` already defines `#define WCB_MGMT_CHUNK_LEN 179   // payload[180] minus NUL (firmware strncpy)` and chunks at 179 (`WCB_Client.cpp:500-503`). The Wizard is the only one of the three senders using 180. Separately, the `MgmtRelay` example handles the single-chunk-too-long case correctly — for `total == 1` it calls `wcb.send(target, marked, true)` with the comment "ensured (ETM + CRC); auto-frags if long" (`WCBClient/examples/MgmtRelay/MgmtRelay.ino:404-407`) — i.e. the sibling relay implementation does the very thing the WCB firmware fails to do.

INTENT — nothing sanctions this. The comments at `WCB.ino:2589-2596` and `:2606-2608` explain the marker and the multi-chunk rationale, not truncation. Decisively, the repo already recorded this exact bug class on the *pull* path and fixed it there: `WCB.ino:2994-3000` — "The original code used all 183 bytes and then forcibly set [182] = '\0', silently dropping one character from every full chunk … the comma in \"SEQ,SAVE,key,value\" were stripped, corrupting the parsed config" — with `chunkStride = CONFIG_PAYLOAD_SIZE - 1`. `docs/SEQUENCE_INVENTORY.md:118-120` documents it. The same discipline was never applied to the MGMT forward path. This is a known trap that bit before, still open on the other path.

CONSEQUENCE. No diagnostic at any point, not even under `debugMGMT` — the log at `WCB.ino:2622-2624` prints "Broadcast chunk n/m" as if fine. The Wizard then toasts `'success'` (`app.js:4458`) and patches the FULL untruncated value into both `boardConfigs` and `boardBaselines` (`:4468-4474`), so the next diff-based push sees no delta and never repairs the corrupted board.

</details>

### `Wizard/app.js:4455` — CONFIRMED · S1 · ⚠ PROPOSED FIX IS WRONG

**updateSequence sends ?SEQ,SAVE as a single un-fragmented MGMT chunk — relay silently truncates it to 179 chars**

**Correction to the finding as filed:**

Two corrections to the write-up, plus one to the fix.

1. Line citation: the `const cmd` line is `Wizard/app.js:4446`, not 4448 (4448 is `if (relayN) {`). The 4455 citation is correct.

2. This is not a new finding — it is case (a) of already-filed F-262 (`docs/CODE_REVIEW_2026-08_wave1.md:170-193`, filed there at S2 against `WCB.ino:2617`), and a sibling of F-139 (`docs/CODE_REVIEW_2026-08.md:289-295`, hand-verified S1). Severity S1 is right, and is consistent with the repo's own hand-verification of F-139; but it should be merged into that family, not tracked separately.

3. Correct fix (two parts, the firmware half being the important one):
   - FIRMWARE (the real fix — makes any buggy sender fail loudly instead of corrupting NVS): in `handleMgmtForward`, before building the packet at `WCB.ino:2610`, add
     `if (payload.length() > sizeof(pkt.payload) - 1) { Serial.printf("[MGMT] FRAG payload %u > %u — rejected\n", payload.length(), (unsigned)(sizeof(pkt.payload) - 1)); return; }`
     This is ungated (not `debugMGMT`) so the operator sees it.
   - WIZARD: fix `MGMT_CHUNK_SIZE` at `app.js:67` from 180 to **179** to match `payload[180]` minus the NUL (`WCBClient/src/WCB_Client.h:189` already documents 179: "payload[180] minus NUL (firmware strncpy)"), then in `updateSequence` fragment ONLY when the single-chunk path cannot carry it:
     ```js
     if (cmd.length <= 198) {                        // relay's ETM shortcut, WCB.ino:2597
       await sendMgmtReliable(relayConn, `${relayFc}MGMT,FRAG,${t},${sid},0,1,${cmd}`, relayN);
     } else {
       const chunks = fragmentString(cmd, MGMT_CHUNK_SIZE);   // now 179
       for (let i = 0; i < chunks.length; i++) {
         await sendMgmtReliable(relayConn, `${relayFc}MGMT,FRAG,${t},${sid},${i},${chunks.length},${chunks[i]}`, i === 0 ? relayN : null);
         if (i < chunks.length - 1) await sleep(MGMT_CHUNK_DELAY);
       }
     }
     ```
   - Also worth doing while in here: on the multi-chunk (broadcast) path there is no ACK, so the success toast at `:4458` and the baseline patch at `:4468-4475` are unearned. Either verify with a `?SEQ,GET,<key>` round-trip before patching the baseline, or leave the baseline unpatched so the next diff push re-sends it.

<details><summary>Full verification reasoning</summary>

I re-derived every load-bearing fact from source rather than the summary.

WIZARD SIDE (all verified by reading, not the summary):
- `Wizard/app.js:4446` — `const cmd = \`${funcChar}SEQ,SAVE,${key},${value}\`;` (the finding says 4448; 4448 is `if (relayN) {`. Minor citation slip.)
- `Wizard/app.js:4455` — `const mgmtCmd = \`${relayFc}MGMT,FRAG,${seqSaveTargetWCB},${sessionId},0,1,${cmd}\`;` — chunk index and total are literal `0,1`. No length check anywhere in `updateSequence` (4399-4485).
- `sendMgmtReliable` (`app.js:7682-7688`) just does `relayConn.send(mgmtCmd + '\r')`; `send()` (`app.js:5209-5228`) writes the whole string with one `writer.write()` — no splitting, no cap.
- No cap upstream either: the value textarea (`app.js:4226`) has a char counter but NO `maxlength` (contrast the key input at `:4220`, `maxlength="15"`). `validateSequenceValue` (`:4105-4112`) only rejects embedded `IF` in `;t`/`;w` payloads. `seqTextareaToValue` (`:4091-4097`) preserves inline `***` comments, so real values get long fast.

FIRMWARE SIDE:
- `Code/WCB/WCB.ino:2597` — `if (totalChunks == 1 && payload.length() <= 198)` → ETM unicast. Verbatim, threshold correct.
- Fall-through at `:2610-2618` builds `espnow_struct_mgmt`; `strncpy(pkt.payload, payload.c_str(), sizeof(pkt.payload) - 1)` (`:2617`) into `char payload[180]` (`:793`), then forced NUL at `:2618` → exactly 179 chars survive. Truncation, not rejection; the only output is a `debugMGMT`-gated "Broadcast chunk 1/1" (`:2622`) that reports success.
- Target: `handleMgmtPacket` copies the chunk (`:2693`, `MGMT_PAYLOAD_SIZE 180` at `:838`), `expectedMask = (1<<1)-1 = 1` equals `receivedMask` immediately (`:2702-2703`), and it executes `parseCommandsAndEnqueue(fullCmd, 0)` (`:2731`). So a HALF command runs, it is not dropped.
- `saveStoredCommandsToPreferences` (`WCB_Storage.cpp:625-663`) has no length validation at all — it splits at the first comma and `preferences.putString(key, value)` writes whatever it got. `?SEQ,SAVE` reaches it via `WCB.ino:4995`.

ARITHMETIC (redone by hand): `"?SEQ,SAVE,"` is 10 chars (measured, not assumed). payload = 10 + keyLen + 1 + valueLen. Truncation fires when that exceeds 198, i.e. valueLen > 187 - keyLen. For key `dome_open` (9) that is valueLen >= 179. The relay then cuts the payload to 179, leaving 179-10-9-1 = 159 chars of value — 20+ chars silently deleted, and the cut always lands inside the VALUE (10+15+1 = 26 max prefix, far below 179), so the key survives and a corrupt sequence is committed to NVS.

REACHABILITY — no guard anywhere on the path:
- Relay serial input is an unbounded `String serialBuffer` (`WCB.ino:6380-6418`) — long lines are accepted.
- `parseCommandsAndEnqueue`'s `?CHK` branch (`:1946-1998`) only engages if the line contains `^?CHK`; otherwise the `?MGMT,` branch (`:2085-2095`) takes the whole remainder of the line without splitting on `^`. Correct handling, no early drop.
- `enqueueCommand` (`:1912-1941`) mallocs `cmd.length()+1` — no 200-byte truncation there (the `char command[200]` at `:512` is `PendingMessage`, an ETM structure, not the queue item).
- Does not require ETM: the truncating path is a plain `esp_now_send` broadcast, so it bites with `?ETM,ON` or `?ETM,OFF`.

BLAST RADIUS (why S1 stands): the toast at `app.js:4458` says "success", and `:4468-4475` patches the full value into BOTH `boardConfigs[n]` and `boardBaselines[n]`, so the delta push at `boardGoRemote` will never re-send it. The board holds a sequence cut mid-token, the Wizard believes it is in sync, and nothing anywhere reports failure until someone pulls config. This is the repo's own calibration for the sibling defect: `docs/CODE_REVIEW_2026-08.md:295` rates F-139 (the 180-vs-179 off-by-one on the multi-chunk push) "HAND-VERIFIED, CONFIRMED S1" for losing ONE char per chunk. This case loses 20+.

INTENT: nothing deliberate. The comment at `WCB.ino:2594-2596` explains the ETM shortcut ("single-chunk MGMT payloads are <= 180 bytes") — i.e. the firmware author ASSUMED senders keep single-chunk payloads under 180; `updateSequence` violates that assumption with no enforcement on either side. `git blame` shows `4455` last touched in 5b438bd7 (2026-03-31) for WCB-number routing, unrelated. No fix commit since.

DEDUP NOTE for the parent: this is a re-derivation of an already-recorded finding. `docs/CODE_REVIEW_2026-08_wave1.md:185` (F-262, filed S2 at `WCB.ino:2617`) explicitly enumerates "(a) Single-chunk commands over 198 chars... `Wizard/app.js:4455` sends `?SEQ,SAVE,<key>,<value>` this way with `0,1` and no length cap". It is still unfixed in the tree, so the defect stands — but it should be merged with F-262/F-139 rather than counted as new.

</details>

### `Wizard/app.js:7852` — CONFIRMED · S1

**Remote config push fragments at 180 chars, but the relay only forwards 179 — one character is silently dropped from every full chunk**

**Correction to the finding as filed:**

The mechanism, lines and severity are right; three refinements.

(a) Scope is narrower than "every push": the relay short-circuits `totalChunks == 1 && payload.length() <= 198` into an ETM unicast that carries the FULL payload (`WCB.ino:2596-2603`), so single-chunk pushes (cmdToSend ≤ 180 chars — the common small diff push) are NOT affected. The defect hits every relayed push whose command string exceeds 180 chars — i.e. full pushes after a flash and multi-setting diffs.

(b) The final chunk is not always safe: when `cmdToSend.length()` is an exact multiple of 180 the last chunk is also 180 chars and is truncated too.

(c) The 180-char boundary carries a second, independent hazard the proposed fix does NOT remove. `processLocalCommand` opens with `if (message.endsWith("?")) { … printCommandHelp(cmd); return; }` (`WCB.ino:4402-4406`). Whenever a chunk boundary lands immediately after a `^` delimiter the chunk's last character is the func char `?`, so the entire `?MGMT,FRAG,…` line is swallowed as a help request and that chunk is never forwarded — the session then dies at the 15 s `MGMT_SESSION_TIMEOUT_MS` (`WCB.ino:839`) and the whole push is silently lost. Moving 180→179 only relocates the boundaries. Worth filing separately (the durable fix is to hoist the `?MGMT,` check above the help shortcut).

Also note the fix must be made on the Wizard side, not by widening `payload[180]`: the 226-byte `espnow_struct_mgmt` size is load-bearing for the size-first receive router (see the "59 bytes is deliberately distinct from every other packet size (43/55/226/230/243/249/252) — the receive path routes BY SIZE first" comment at `WCB.ino:804-808`), and `WCBClient/src/WCB_Client.h:199` hardcodes the same `char payload[180]`.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived independently, not from the finding's transcription:

1. Wizard side. `Wizard/app.js:67` is literally `const MGMT_CHUNK_SIZE  = 180;   // max payload chars per ESP-NOW packet`. `fragmentString` (`Wizard/app.js:7760-7766`) is a plain `for (i=0; i<str.length; i+=chunkSize) chunks.push(str.slice(i, i+chunkSize))` — no clamping, so every chunk but the tail is EXACTLY 180 chars. `Wizard/app.js:7852` calls it as `fragmentString(cmdToSend, MGMT_CHUNK_SIZE)`, and `:7860-7862` emits `?MGMT,FRAG,<wcb>,<sess>,<i>,<total>,<chunk>\r` verbatim to the relay. Grep confirms `MGMT_CHUNK_SIZE` has exactly two occurrences in the Wizard (`:67`, `:7852`) — no second, corrected fragmenter.

2. Nothing truncates the chunk before the relay's copy. The relay serial reader accumulates into an Arduino `String serialBuffers[6]` with no length cap (`WCB.ino:6380-6417`); `parseCommandsAndEnqueue` has an explicit special case that does NOT split `?MGMT,...` on `^` and takes `data.substring(startIdx)` whole (`WCB.ino:2078-2092`); `enqueueCommand` mallocs `cmd.length()+1` (`WCB.ino:1927-1934`). So the full 180-char payload reaches `handleMgmtForward` intact via `processLocalCommand` (`WCB.ino:4380-4381`, dispatch `:5089-5092`), which extracts `String payload = args.substring(c5 + 1);` (`WCB.ino:2581`).

3. Firmware relay copy. `char payload[180];` at `WCB.ino:793` (reviewer said 792 — off by one, immaterial). `WCB.ino:2617-2618`: `strncpy(pkt.payload, payload.c_str(), sizeof(pkt.payload) - 1);` → n = 179, copying src[0..178]; then `pkt.payload[179] = '\0'`. A 180-char source loses character #180. Arithmetic redone by hand: sizeof = 180, n = 179, 179 chars survive.

4. Target reassembly preserves the truncation: `strncpy(mgmtSession.chunks[idx], pkt.payload, MGMT_PAYLOAD_SIZE)` with `MGMT_PAYLOAD_SIZE 180` (`WCB.ino:838`) into `chunks[16][181]` (`:848`), then plain concatenation `fullCmd += String(mgmtSession.chunks[i])` (`WCB.ino:2705`) and `parseCommandsAndEnqueue(fullCmd, …)`. No recovery anywhere.

REACHABILITY — real user, real path:
- `Wizard/index.html:496` `<button id="b{N}-btn-go" onclick="boardGo({N})">Push Config</button>` → `boardGo` (`app.js:6991-6993`) → `if (remoteRelayForBoard[n]) return boardGoRemote(n, opts);`. Also reached from the network-group confirm path (`app.js:8293`). So any board reached through a relay pushes down this path.
- The multi-chunk branch is the one that truncates. `WCB.ino:2596-2603` short-circuits `totalChunks == 1 && payload.length() <= 198` through ETM unicast with the FULL payload, so pushes ≤180 chars are unaffected. Anything longer (full push after a flash, or a diff of more than a couple of commands) takes the `espnow_struct_mgmt` path at `:2606-2620` and loses one char per 180-char boundary.

INTENT — the surrounding comments argue the opposite of "deliberate". The identical bug in the reverse (pull) direction was found, fixed, and permanently commented in the same file, `WCB.ino:2994-2999`: "The original code used all 183 bytes and then forcibly set [182] = '\0', silently dropping one character from every full chunk — causing bytes at positions 182, 364, … to be lost… corrupting the parsed config." That direction now uses `const int chunkStride = CONFIG_PAYLOAD_SIZE - 1;` (`:3000`). The `-1` gap is the established, field-proven contract here; the Wizard's push side never got it. Cross-repo, `WCBClient/src/WCB_Client.h:189` is `#define WCB_MGMT_CHUNK_LEN 179   // payload[180] minus NUL (firmware strncpy)` and `WCB_Client.cpp:464,500-502` fragments at 179 into the same 226-byte struct — a third implementation that already has it right. Nothing in `docs/` or `CLAUDE.md` sanctions 180.

git blame: both constants come from the same commit 978ecf4 (2026-03-03, "working on wizard and config tool"), so this is longstanding shipped behaviour, not a regression in this branch. That does not make it benign — it explains why it survived (typical diff pushes are single-chunk and exempt; full pushes after a flash are not).

Severity per the repo's own scale in docs/CODE_REVIEW_2026-08.md:10 ("S1 crashes / corrupts / bricks"): this silently corrupts a pushed config while the Wizard reports success and sets `boardBaselines[n] = config` (`app.js:7873`), so the tool's baseline diverges from the board. S1 stands.

</details>

### `Code/WCB/command_timer.cpp:203` — CONFIRMED · S1 → **S2** · ⚠ PROPOSED FIX IS WRONG

**Use-after-free: processCommandGroups() holds references into commandGroups across a vTaskDelay() that serialCommandTask can clear**

**Correction to the finding as filed:**

Two corrections to the finding, plus a better fix.

(1) Location/attribution: the write-up's line cites are correct (command_timer.cpp:187, :203, :208), but it omits the second window between the size guard at command_timer.cpp:183 and the derefs at :187/:189, and it overstates reproducibility ("reproduces reliably under any load"). WCB.ino:6489 excludes every '?'-prefixed line, so Wizard config traffic cannot trip this; only an exact `?STOP` or a non-'?' line containing `;t`/`;T` can. Realistic hit rate is well under 1% per operator action.

(2) The proposed fix should be replaced. Fix the victim, not each mutator: in `processCommandGroups`, copy `commandGroups[currentGroupIndex].commands` into a local `std::vector<String>` before the loop so nothing iterated can be freed under the yield, and after the loop re-check `commandTimerModeEnabled` / `commandGroups.size()` (or bump a `timerGeneration` counter in both `stopTimerSequence` and `parseCommandGroups` and compare it) before doing `currentGroupIndex++` at :213 — otherwise a chain replaced mid-fire still loses its group 0. Also re-read `commandGroups[currentGroupIndex]` under a fresh bounds check rather than holding `CommandGroup &group` from :187. Independently, correct the false invariant recorded at WCB.ino:398-399 ("The local-serial entry point ... run inside loop() already") — the serial entry point runs in serialCommandTask (WCB.ino:6595, created at :7439).

If the deferral route is taken anyway, it must use a heap-pointer queue modelled on `CommandQueueItem` (WCB.ino:890-901; `char *cmd` malloc'd at :1926, freed at :7504) rather than the fixed 220-byte slot, must push the stop through the same queue as an ordered marker (or a `volatile bool timerStopRequested` consumed at the top of `drainPendingTimerChains`), and must log overflow drops.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived from source, not the write-up.

`processCommandGroups()` (command_timer.cpp:182) binds `CommandGroup &group = commandGroups[currentGroupIndex]` at :187 — a reference into the vector's element storage — then at :203 enters `for (const String &cmd : group.commands)`. The range-for caches `__begin`/`__end` from the *inner* `std::vector<String>` once, before the first iteration. Inside the body, after `parseCommandsAndEnqueue(cmd, 0)` at :207, line :208 executes `vTaskDelay(pdMS_TO_TICKS(1))` — an unconditional blocking yield with `group` and the cached iterators live. Verified verbatim by reading command_timer.cpp:182-224.

The reference is genuinely unsafe if another context clears the vector. `commandGroups.clear()` retains the outer buffer (capacity preserved), so `group` itself still points into live memory — but `clear()` runs `~CommandGroup`, which destroys the inner `std::vector<String> commands` (command_timer_queue.h:8-10), freeing that heap array and every String's own buffer. The cached `__begin`/`__end` therefore dangle, and the next iteration's `*__begin` reads a destroyed `String`, whose freed `buffer` pointer is then handed to `parseCommandsAndEnqueue`. If the clearer was `parseCommandGroups`, its own `push_back`s (command_timer.cpp:123/:177) reuse that same freed storage, so the read is of actively-overwritten memory — garbage command dispatched, or LoadProhibited.

CROSS-TASK WRITERS — I grepped every mutation of the timer state (`grep -rn "commandGroups\.\|commandTimerModeEnabled *=\|currentGroupIndex *=\|waitingForNextGroup *="`); all of it lives in command_timer.cpp, inside exactly two functions. Their call sites:
- `stopTimerSequence()` — command_timer.cpp:190 (loop, self) and **WCB.ino:6431**.
- `parseCommandGroups()` — WCB.ino:428 (`drainPendingTimerChains`, loop), WCB_Storage.cpp:615 (SEQ playback), and **WCB.ino:6490**.

Both bolded sites are inside `processSerialCommandHelper` (WCB.ino:6423), whose only caller is `processIncomingSerial` (WCB.ino:6411), whose only callers are WCB.ino:6598-6621 — all inside `serialCommandTask` (WCB.ino:6595). There is no deferral in that path: `processIncomingSerial` calls the helper synchronously at :6411. `serialCommandTask` is created at WCB.ino:7439 `xTaskCreatePinnedToCore(serialCommandTask, "Serial Command Task", 4096, NULL, 1, NULL, 1)` — core 1, priority 1. `loopTask` is created in the pinned core at Arduino15/.../3.3.4/cores/esp32/main.cpp:110 `xTaskCreateUniversal(loopTask, "loopTask", …, NULL, 1, &loopTaskHandle, ARDUINO_RUNNING_CORE)` — priority 1, same core (esp32-hal.h:57). Same core + same priority means the `vTaskDelay` at command_timer.cpp:208 is a guaranteed yield to `serialCommandTask` whenever its own 5 ms poll delay (WCB.ino:6624) has expired. `processCommandGroups()` runs only from loop() (WCB.ino:7481), and `handleSingleCommand` only from loop() (WCB.ino:7512), so the SEQ-playback and ESP-NOW paths are clean — the serial path is the sole offender.

The finding is also right that this contradicts a recorded invariant. WCB.ino:390-399 explains the pendingTimerChainQueue and states "The local-serial entry point and SEQ playback both run inside loop() already, so they keep calling parseCommandGroups() directly." The SEQ half is true; **the local-serial half is false** — it runs in serialCommandTask. That comment is itself a defect and must be corrected whatever fix is chosen. No mutex or critical section guards any of this (the only `portENTER_CRITICAL` in the sketch is `etmAckQMux`, WCB.ino:1120/1134).

REACHABILITY — real, but narrower than the write-up implies. WCB.ino:6489 gates the parse on `!data.startsWith(String(LocalFunctionIdentifier)) && isTimerCommand(data)`, so every `?`-prefixed line (the whole Wizard config push, ?SEQ, ?MGMT, ?OTA) goes to `parseCommandsAndEnqueue` and never touches `commandGroups`. Only two serial inputs mutate it: a line equal to `?STOP` (`equalsIgnoreCase`, command_timer.cpp:61-63; `LocalFunctionIdentifier='?'`, WCB.ino:155) or a non-`?` line containing `;t`/`;T` anywhere (command_timer.cpp:48-50; `CommandCharacter=';'`, WCB.ino:156). Both are operator actions, and `?STOP` is correlated with the vulnerable state by design — interrupting a running chain is its only purpose. But the window is ~1 ms per command in a firing group, once per inter-group delay, against a 5 ms serial poll: order 0.5-1% per operator action on a typical `;t500`/`;t1000` chain. The reviewer's "reproduces reliably under any load where serial input arrives while a timer sequence is mid-run" is wrong.

A second, much narrower window exists that the finding didn't name: between the `currentGroupIndex >= commandGroups.size()` guard at :183 and the `commandGroups[currentGroupIndex]` / `group.commands[0]` derefs at :187/:189 — open on every loop pass for the sequence's whole life, but only a few dozen instructions wide, needing a tick-boundary preempt. (Empty groups are impossible — push_back is guarded by `!currentGroup.commands.empty()` at :122/:176 — so `commands[0]` is safe absent the race.)

Non-crash outcome worth noting: even when the freed read survives, the post-loop `currentGroupIndex++` at :213 is applied to whatever chain now occupies the vector, so a replacement `;t` chain silently loses its group 0.

INTENT — this is not a documented trade-off. git blame shows :187/:203/:209 from 40d9ac3 (2025-07-23), the `vTaskDelay` added later in 7327091 (2025-11-19) for queue pacing, with no concurrency comment. The pendingTimerChainQueue (5c49642) shows the author knew the race class and closed the WiFi-task half while explicitly, and mistakenly, exempting the serial half.

SEVERITY — crash/heap-corruption class consequence, but a low per-event probability requiring a specific operator-typed line landing in a sub-1% window. S2, not S1.

</details>

### `Code/WCB/WCB_Storage.cpp:352` — CONFIRMED · S2

**?BCAST,RESET does not reset broadcast INPUT blocking at all and leaves RAM stale, so the next unrelated save re-persists the pre-reset values**

**Correction to the finding as filed:**

Mechanism is right; three refinements. (1) Drop gap 2 as a standalone defect — the absent `S0` key reads back as the correct default (WCB_Storage.cpp:348 vs WCB.ino:630); it only matters via the stale-RAM re-persist. (2) The re-persist trigger in the shipped fixture is `?MAESTRO,CLEAR,ALL`, not `?KYBER,CLEAR`: `clearAllMaestroConfigs()` calls BOTH savers unconditionally (WCB_Maestro.cpp:920-921) and re-enables only `portsUsed[]` ports (:872-877), so it fires on every board regardless of prior state, one token earlier. `?MAP,CLEAR,ALL` also unconditionally calls `saveBroadcastBlockSettings()` (WCB_Storage.cpp:1929). The `?KYBER,CLEAR` path the reviewer described is real too (`kyberPort` defaults to 2 at :1080, so it hits index [1] = S2 exactly as claimed) but is conditional. (3) Strongest evidence the reviewer left out: the command's own help promises "All ports enabled for input and output" (WCB_Help.cpp:181-182) and TestConfigs/TestPlan.md:104 test 3B.8 asserts exactly that via `?config` — which reads RAM (WCB_Storage.cpp:302-303), so 3B.8 fails for both directions today. Also worth recording in the fix: `git log -L352,371:Code/WCB/WCB_Storage.cpp` shows the function was written in f8bcc09 purely as a `BroadcastSerial<n>` → `S<n>` key migration, which is why it is NVS-only and says "Please reboot".

<details><summary>Full verification reasoning</summary>

I read the function, every writer/reader of the two namespaces, every caller in the shipped fixture chain, the help text it promises against, and the commit that introduced it. The finding stands.

**1. The function body — verified verbatim (WCB_Storage.cpp:352-371).** It opens ONLY `preferences.begin("bdcst_set", false)` (:354), `clear()`, `end()`, then reopens and writes `S1`..`S5` = `putInt(key, true)` (:362-368), prints "Done. Please reboot." (:370). It writes no `S0` key, never touches RAM, and never opens `bcast_block`. Nothing else is in the function.

**2. Gap 1 (input blocking never reset) — CONFIRMED and it survives the reboot.** `blockBroadcastFrom[5]` (declared WCB.ino:600, all false) persists in a different namespace: `saveBroadcastBlockSettings()` writes keys `blk1`..`blk5` to `bcast_block` (WCB_Storage.cpp:1583-1590), `loadBroadcastBlockSettings()` reads them (:1574-1581, called from setup at WCB.ino:7218). The ONLY code that clears `bcast_block` is `eraseNVSFlash()` (:994) — i.e. `?ERASE,NVS`, a different command. So after `?BCAST,RESET` + reboot, every auto-set input block is still there. Auto-blockers are numerous and routine: local Maestro (WCB_Maestro.cpp:670-674), Kyber LOCAL (WCB_Storage.cpp:1170-1176), `?MAP,SERIAL` (WCB_Storage.cpp:1738-1748), MP3 (WCB_MP3.cpp:351-355), plus DFP/HCR/WLED/PWM equivalents.

**3. Gap 3 (stale RAM re-persisted) — CONFIRMED, and the trigger is one token earlier than the reviewer said.** Every saver writes the WHOLE array from RAM (`saveBroadcastSettingsToPreferences()` :331-339 loops all five + `S0`). The reviewer blamed `?KYBER,CLEAR`; that path is real — `?KYBER,CLEAR` has no comma so `kyberPort = 2` (WCB_Storage.cpp:1080), and the clear branch does `if (!serialBroadcastEnabled[1]) {…; saveBroadcastSettingsToPreferences();}` (:1197-1207), exactly as claimed. But the *stronger* trigger fires first in the same paste: `?MAESTRO,CLEAR,ALL` → `clearAllMaestroConfigs()` (WCB.ino:4900-4901) calls **both** savers **unconditionally** at WCB_Maestro.cpp:920-921, after re-enabling only ports in `portsUsed[]` (:872-877). `clearAllSerialMappings()` likewise calls `saveBroadcastBlockSettings()` unconditionally (WCB_Storage.cpp:1929) — and `?MAP,CLEAR,ALL` is also in the chain. So on the shipped fixture line (TestConfigs/WCB1_01_base.txt:64: `…?BCAST,RESET^?MAP,CLEAR,ALL^?MAESTRO,CLEAR,ALL^?KYBER,CLEAR^?MP3,CLEAR^?config`) the reset's NVS writes are guaranteed to be overwritten by stale RAM two tokens later, on every board.

**4. It contradicts its own printed contract — this is not a deliberate trade-off.** WCB_Help.cpp:181-182: `"RESET  Reset all broadcast settings to defaults"` / `"All ports enabled for input and output"`. TestConfigs/TestPlan.md:104 test 3B.8 asserts `?config` shows every port "Broadcast Input: Enabled / Output: Enabled" after `?BCAST,RESET`. `?config` prints from RAM (WCB_Storage.cpp:302-303; emitters WCB.ino:2811-2814), which the reset never touches — so 3B.8 fails immediately for BOTH directions, and fails for input even after the reboot the function asks for. There is no comment anywhere defending NVS-only behaviour.

**5. Origin explains it.** `git log -L352,371` shows the function was born in f8bcc09 "fixed bug with broadcast persistant after reboot" as a one-off NVS key migration (old `BroadcastSerial<n>`/`putBool` → new `S<n>`/`putInt`). "Please reboot" made sense for a key-rename hack. The help text later promised "restore all defaults", which the code never grew into.

**Corrections to the reviewer.** (a) Gap 2 ("S0 key not re-created") is NOT an independent defect: `loadBroadcastSettingsFromPreferences()` reads `S0` with default 0 (:348, comment "S0/USB output defaults OFF"), matching `bool broadcastToS0 = false` (WCB.ino:630) — the missing key yields the correct default. S0's only exposure is the same stale-RAM re-persist as gap 3. (b) Immaterial line drift: savers/loaders are :1583-1590 / :1574-1581 (not :1584-1589 / :1574-1580); Maestro input-block is WCB_Maestro.cpp:670-674 (the :664-668 block is the *output* half); MAP,SERIAL block is :1738-1748.

**Severity.** Keeping S2. It is silent (no error), permanent across reboot for the input direction, contradicts printed help and a written bench test, and corrupts the "known-clean baseline" that all five TestConfigs base fixtures exist to establish. The failure mode — a port whose data silently never reaches the mesh — is a multi-hour debugging trap. Not S1: no crash, no data loss, recoverable with per-port `?BCAST,IN,Sx,ON` or `?ERASE,NVS`.

</details>

### `Code/WCB/WCB_Storage.cpp:834` — CONFIRMED · S2 · ⚠ PROPOSED FIX IS WRONG

**The legacy stored_commands namespace is never cleared and the migration guard lives inside the namespace that IS cleared — ?SEQ,CLEAR,ALL or ?ERASE,NVS resurrects deleted sequences on the next boot**

**Correction to the finding as filed:**

The headline sub-option of the proposed fix is wrong and must not be taken: moving the flag to `wcb_config` does NOT fix the factory-reset path, because `eraseNVSFlash()` clears `wcb_config` itself at WCB_Storage.cpp:954-956. The flag would die again on `?ERASE,NVS` and legacy sequences would still resurrect on the reboot at :1032.

The correct fix is two edits, and the primary one is the erase-list edit, not the flag move:

1. Add the legacy namespace to `eraseNVSFlash()` (WCB_Storage.cpp:966-968, next to the `stored_cmds` block) so the data is destroyed rather than merely hidden:
   `preferences.begin("stored_commands", false); preferences.clear(); preferences.end();`
   (Opening RW creates the namespace if absent — same cost the other 24 entries in that list already pay, ~1 NVS entry.)

2. Make `clearAllStoredCommands()` (WCB_Storage.cpp:832-837) also wipe the legacy namespace and re-assert the flag on the still-open RW handle, so "clear all" is durable without a reboot dependency:
   ```
   preferences.begin("stored_cmds", false);
   preferences.clear();
   preferences.putBool("seq_mig_done", true);   // clear() erased it; migration must not re-run
   preferences.end();
   preferences.begin("stored_commands", false); preferences.clear(); preferences.end();
   invalidateSequenceInventoryHash();
   ```

Also: the "3 lines above/below" scope is not the whole story — the real regression is historical. `483b4d8:462-464` shows `eraseNVSFlash()` once cleared `stored_commands`; the `stored_cmds` rename replaced that entry instead of adding to it. Whatever fix lands should note that in a comment, matching the existing precedent comment at WCB_Storage.cpp:1022-1023.

Per CLAUDE.md ("a fix whose cause is a trap someone could walk into again earns a doc line"), this qualifies: the constraint is "a one-time migration flag must never live in a namespace that any wipe path clears, and every namespace the firmware reads must appear in the eraseNVSFlash list."

<details><summary>Full verification reasoning</summary>

MECHANISM — every load-bearing fact re-derived from source, not from the finding.

1. `clearAllStoredCommands()` is exactly 6 lines (`Code/WCB/WCB_Storage.cpp:832-837`): `preferences.begin("stored_cmds", false); preferences.clear(); preferences.end(); invalidateSequenceInventoryHash();`. `Preferences::clear()` in the pinned core (`Arduino15/packages/esp32/hardware/esp32/3.3.4/libraries/Preferences/src/Preferences.cpp`) is `nvs_erase_all(_handle)` + `nvs_commit` — it erases EVERY key in the namespace, `seq_mig_done` included. There is no re-write of the flag anywhere in that function.

2. The migration guard genuinely lives inside the namespace being erased: read at `WCB_Storage.cpp:876-877` (`begin("stored_cmds", true)` → `getBool("seq_mig_done", false)`), early-returned at `:880`, written back at `:925-926`. Key length `seq_mig_done` = 12 chars, under the 15-char NVS limit, so it does persist normally — the flag works, it just isn't durable across a namespace wipe.

3. `eraseNVSFlash()` (`WCB_Storage.cpp:941-1033`) clears `stored_cmds` at `:966-968` — same `clear()`, same loss of the flag — and its namespace list (`:942-1026`) does NOT contain `stored_commands`. It ends in `ESP.restart()` (`:1032`), so the resurrection boot is automatic and immediate.

4. Legacy namespace is read-only in the whole tree. Repo-wide grep for `stored_commands` in `*.ino/*.cpp/*.h` returns exactly one live code site — the read at `WCB_Storage.cpp:887` — plus comments at `:545` (commented-out `loadStoredCommandsFromPreferences`), `:869`, `:897`. Nothing writes it, nothing erases it. Confirmed no `nvs_flash_erase` / `nvs_erase_*` anywhere in the firmware (grep returned only an unrelated comment in `WCB_Variables.cpp:9`).

REACHABILITY — real, on ordinary user paths.
- `?SEQ,CLEAR,ALL` → `WCB.ino:4988-4989` (`if (seqArgsUpper == "ALL") clearAllStoredCommands();`). Legacy terminal `cclear`/`CCLEAR` → `WCB.ino:5185`.
- `?ERASE,NVS` → `WCB.ino:5069-5071`; legacy `wcb_erase` → `:5209`.
- Wizard drives `?ERASE,NVS` from two buttons: the factory-reset-then-push path (`Wizard/app.js:7303-7305`) and the erase-only path (`app.js:9203-9205`).
- `migrateOldStoredCommands()` is called unconditionally from setup at `WCB.ino:7214`, with no guard other than the flag it just lost.
- `preferences.begin("stored_commands", true)` is READ-ONLY, so on a board that never had legacy data `nvs_open` fails, `getString` returns "", and nothing is recovered. No false-positive resurrection — the bug fires only on boards carrying real legacy data.

Can a real board be in that state? Yes. `git show 483b4d8:Code/WCB/WCB_Storage.cpp` (the initial commit of this firmware, 2025-07-09) already saved to `stored_cmds` (`:330-341`) and only READ `stored_commands` (`:284`) — so the writer of `stored_commands` predates this repo entirely, i.e. real fielded boards. Critically, that same old `eraseNVSFlash()` DID clear `stored_commands` (`483b4d8:462-464`); the rename to `stored_cmds` dropped the legacy namespace from the erase list and never re-added it — a regression, not a design choice. And `Wizard/flasher.js:682` sets `eraseAll: false`, so every Wizard firmware update preserves NVS: a board carried forward from the original firmware still holds its `stored_commands` CMD keys today.

INTENT — not deliberate. `git blame` shows the migration authored in `f992e4e` (2026-04-07) with the comment at `:872-873` stating the flag is stored in `stored_cmds` — the author was aware of where it lives but not that `clear()` destroys it. Nothing in `docs/`, `CLAUDE.md`, or the surrounding comments defends the behaviour. The opposite: `WCB_Storage.cpp:1022-1023` carries the comment "Previously MISSED by factory reset — HCR, WLED, and user-variable configs survived an erase and re-seized their ports / reappeared after reboot", which is precedent that this exact class of omission is treated as a bug here.

ONE THING THE FINDING UNDERSTATES: on an affected board the deletion is not merely undone once, it is un-undoable. After the resurrection boot the flag is re-set (`:925-926`), but a second `?SEQ,CLEAR,ALL` erases it again and the next boot resurrects again — an unbounded loop. `?SEQ,CLEAR,<key>` (→ `eraseStoredCommandByName`, `:666-700`, uses `remove()` not `clear()`) does NOT touch the flag, so per-key deletes stick. So the user-visible rule is: deleting sequences one at a time works; "clear all" and "factory reset" never do.

Correcting one detail of the transcription: the finding's Case-2 framing is irrelevant to the failure. After `clear()`, `stored_cmds` is empty, so Case 2 (`:900-922`) recovers nothing. Only Case 1 (`:884-898`, legacy namespace) resurrects. On a board with no legacy data both wipe paths are harmless — migration just reruns and prints "No legacy sequences found" (`:936`).

Severity: this repo's own rubric (`docs/CODE_REVIEW_2026-08.md:10-11`) is S2 = "wrong behaviour in a real scenario", S3 = "latent race, bound, leak, overflow". This is deterministic wrong behaviour on a normal path, not a race or bound — S2 stands, with the caveat that the affected population is limited to boards carrying pre-2025-07 NVS state.

</details>

### `Code/WCB/WCB_Storage.cpp:1024` — CONFIRMED · S2 · ⚠ PROPOSED FIX IS WRONG

**?ERASE,NVS never clears the dfp_cfg namespace — the DFPlayer config and its port claim survive a factory reset, and the adjacent WCB_DFP comment claims otherwise**

**Correction to the finding as filed:**

The defect, mechanism, line and severity all stand. Two corrections to the write-up, one of them to the fix itself:

(a) **The second half of the proposed fix is wrong and must not be applied.** The reviewer says "Then fix WCB_DFP.cpp:109 so it names a path that exists." That comment (WCB_DFP.cpp:108-110) reads "the auto-learned remote route is a separate axis (?DFP,REMOTE,OFF / factory reset)". It is currently inaccurate only *because* of this bug — the moment `dfp_cfg` is added to `eraseNVSFlash()`, factory reset does clear `rwcb` and the comment becomes true. It is also verbatim the same comment as the already-correct `clearMP3Config()` (WCB_MP3.cpp, whose `mp3_cfg` IS cleared at WCB_Storage.cpp:1006) and `clearHCRConfig()`. Rewriting it would introduce drift across three siblings. Leave it alone; the erase is the whole fix.

(b) **The blast radius is understated.** Beyond the local port seizure, the reset board keeps advertising `WDP_CAP_DFPLAYER` (WCB_WDP.cpp:109) and peers persist the stale `;D` route via `dfpAutoAddRemote()` (WCB_WDP.cpp:735, WCB_DFP.cpp:429-437), which is first-host-wins and therefore sticky — each peer needs a manual `?DFP,REMOTE,OFF`. This is the strongest argument for holding S2.

**Corrected fix** — insert alongside the trio at WCB_Storage.cpp:1024-1026 (placement matters: it must precede `clearAllPWMMappings()` at :1028, which calls `ESP.restart()` and never returns):

    preferences.begin("dfp_cfg", false);   preferences.clear(); preferences.end();

and extend the "Previously MISSED" comment to name DFPlayer as the third occurrence.

**Land the sibling in the same commit** (same family, same fix): `stored_commands` is also never cleared, while the `seq_mig_done` guard that suppresses the migration lives in `stored_cmds`, which IS cleared (WCB_Storage.cpp:966). So `migrateOldStoredCommands()` (WCB_Storage.cpp:874, boot-called at WCB.ino:7214) re-runs after every factory reset and resurrects legacy sequences the user just erased. Add a `stored_commands` clear too.

**Structural note worth acting on:** this is the third time the hand-maintained list has been found incomplete (HCR/WLED/vars, now DFP + stored_commands). The list wants to be derived from a single table of namespace literals shared with the modules that open them, so a new subsystem cannot be added without appearing in it.

<details><summary>Full verification reasoning</summary>

I re-derived every load-bearing fact from source rather than trusting the summary.

**1. The namespace really is missing.** I read `eraseNVSFlash()` in full at Code/WCB/WCB_Storage.cpp:941-1033. It is a hand-maintained list of `preferences.begin(ns,false)/clear()/end()` triples — there is no blanket `nvs_flash_erase()` anywhere in the sketch. I then machine-diffed it rather than eyeballing: extracted every `begin("...")` inside lines 941-1033 and compared against every `preferences.begin("...")` in Code/WCB/**. 27 distinct namespaces exist; the set not cleared by the erase body is exactly `{dfp_cfg, pwm_mappings, pwm_outputs, stored_commands}`. `pwm_mappings` (WCB_PWM.cpp:499) and `pwm_outputs` (WCB_PWM.cpp:503) are handled by `clearAllPWMMappings()` called at WCB_Storage.cpp:1028, so they are covered. **`dfp_cfg` is genuinely absent**, while its twin `mp3_cfg` is cleared at :1006.

**2. The writer/reader are real and unconditional.** `saveDFPSettings()` opens `dfp_cfg` at WCB_DFP.cpp:388 and writes `port/baud/en/vol/defvol/onErr/rwcb` (:389-395 — all keys ≤6 chars, well under the 15-char NVS limit, so nothing is silently truncated). `loadDFPSettings()` (WCB_DFP.cpp:399-407) reads them back and is called unconditionally from setup at WCB.ino:7142, with no gate above it. `dfpConfig.configured` and `.serialPort` come straight back.

**3. The port claim is real.** `isSerialPortUsedForDFP()` (WCB_DFP.cpp:63-65) returns `dfpConfig.configured && serialPort==port` — no other predicate. Two consumers: `processIncomingSerial()` early-returns at WCB.ino:6370 (RX dropped), and the broadcast fan-out `continue`s at WCB.ino:6328 (TX skipped). So the claimed port is fully dead to normal traffic after the "factory reset".

**4. Reachability is a first-class user flow, not exotic.** `eraseNVSFlash()` is reached from `?ERASE,NVS` (WCB.ino:5069-5071) and legacy `?WCB_ERASE` (WCB.ino:5209), and from two Wizard buttons that send `?ERASE,NVS` over the live port (Wizard/app.js:7305 and :9205), both of which then log "NVS erased — board rebooting…" to the user. The built-in help promises "Erases ALL settings stored in NVS flash memory… returns the board to factory default settings" (WCB_Help.cpp:940-945). The contract is explicit and violated.

**5. Intent — this is not a deliberate trade-off; it is the exact trap the adjacent comment records.** WCB_Storage.cpp:1022-1023 reads "Previously MISSED by factory reset — HCR, WLED, and user-variable configs survived an erase and re-seized their ports / reappeared after reboot." `git log -L 1020,1028` shows that comment+trio landed in a32e9cb; `git log -- WCB_DFP.cpp` shows the DFPlayer module landed in dac753e, and `git merge-base --is-ancestor a32e9cb dac753e` confirms the erase-fix came first. The new module simply did not get added to the list.

**6. The blast radius is larger than the finding states, which is what holds severity at S2.** The stale `configured=true` also keeps the board advertising `WDP_CAP_DFPLAYER` (WCB_WDP.cpp:109 `if (dfpConfig.configured) f |= WDP_CAP_DFPLAYER;`). Peers act on it at WCB_WDP.cpp:735 via `dfpAutoAddRemote()`, which **persists** the route (`saveDFPSettings()` inside it) and is first-host-wins — it returns false if `remoteWCB != 0`, so once a peer has wrongly learned the reset board as the `;D` host, that route is sticky in *that peer's* NVS and needs a manual `?DFP,REMOTE,OFF` on each one. A factory reset on one board therefore writes durable wrong state onto other boards. Compounding it: the Wizard's erase path snapshots the config and forces a full push on the assumption the board is blank (Wizard/app.js:7299-7311), so a pushed config that assigns S2 to something else lands on a port the DFP still owns, and `processIncomingSerial` returns at :6370 before any other handler sees a byte.

**Mitigations weighed (why not S1):** the boot log does print "[DFP] Loaded: S2 at 9600 baud" (WCB_DFP.cpp:412-417), recovery is a single `?DFP,CLEAR`, no data is lost, and DFP is a recent feature so the installed base is small. That keeps it out of S1 but not out of S2 — it is deterministic, 100% reproducible, on the primary reset path, and it corrupts peer state.

I also confirmed the sibling leak in the same family is real: `migrateOldStoredCommands()` (WCB_Storage.cpp:874, called at WCB.ino:7214) stores its done-flag `seq_mig_done` in `stored_cmds` — which the erase *does* clear (:966) — while the legacy `stored_commands` namespace it reads from (:887) is never cleared. So a factory reset re-arms the migration and re-imports legacy CMD1..CMDn sequences on the next boot.

</details>

### `Code/WCB/WCB_Storage.cpp:1078` — CONFIRMED · S2 · ⚠ PROPOSED FIX IS WRONG

**?KYBER,CLEAR is emitted in every non-Kyber board's config chain and always resets Serial2's baud + broadcast flags, silently overwriting settings the same push just applied**

**Correction to the finding as filed:**

The defect, location and severity stand; the fix does not.

FIX AS PROPOSED FAILS. `(kyberPort >= 1 && kyberPort <= 5) ? kyberPort : kyberLocalPort` picks `kyberPort`, which is hard-coded to 2 at WCB_Storage.cpp:1082 on the only path a bare `?KYBER,CLEAR` can take. `clearPort` is therefore still 2 and S2 is still clobbered. The ternary only fires its fallback for the malformed `?KYBER,CLEAR,S0` case.

Also note `kyberPort = 2` cannot simply be removed: bare `?KYBER,LOCAL` depends on it (WCB.ino:4654, WCB.ino:5233; documented at WCB_Help.cpp:224 "This board has Kyber on S2").

CORRECT FIX — the clear branch must default to the port Kyber is *actually* on, and only honour an explicitly supplied port. `firstComma` is already in scope from :1073:

```cpp
} else if (baseCommand.equals("clear")) {
    // The no-comma kyberPort=2 default (:1082) exists only so bare
    // "?KYBER,LOCAL" means S2 — it must NOT decide which port to un-reserve.
    // Use the port Kyber is actually on; an explicit "?KYBER,CLEAR,Sx" wins.
    // 0 = nothing was ever reserved -> touch nothing.
    int clearPort = (firstComma != -1 && kyberPort >= 1 && kyberPort <= 5)
                    ? kyberPort : kyberLocalPort;
    Kyber_Location = " ";
    Kyber_Local = false;
    Maestro_Remote = false;
    kyberLocalPort = 0;              // safe to zero now, clearPort is captured
    kyberUseTargeting = false;

    preferences.begin("kyber_settings", false);
    preferences.putString("K_Location", Kyber_Location);
    preferences.end();

    if (clearPort >= 1 && clearPort <= 5) {   // same guard shape as :1162
      updateBaudRate(clearPort, 9600);
      Serial.printf("v Reset S%d baud rate to 9600 (was Kyber port)\n", clearPort);
      if (!serialBroadcastEnabled[clearPort - 1]) { ... }
      if (blockBroadcastFrom[clearPort - 1])      { ... }
    }
    saveKyberTargets();
    ...
```

This also fixes the mirror half the finding omits (Kyber really on S3 → S3 correctly returned to 9600/unblocked instead of being left at 115200), and closes the `?KYBER,CLEAR,S0` OOB write at :1200/:1205 (F-066) as a side effect. Leave the Wizard emitting the bare `KYBER,CLEAR` — it is the only spelling that zeroes `kyberTargets[]` (:1084-1089); the comma form instead auto-repopulates enabled targets at :1118-1135 and persists them at :1210.

Two corrections to the write-up itself: (1) the "a device on S2 stops working" scenario only holds for a plain `?BAUD`/`?BCAST`-configured device — a Maestro/WLED/MP3/HCR on S2 self-heals because its emitter runs after Kyber in the same chain (WCB.ino:2864, parser.js:1360-1457) and re-reserves the port (WCB_Maestro.cpp:659-675, WCB_WLED.cpp:165-178); it is also a no-op on a default board (WCB.ino:634-640, :629, :600). (2) This duplicates F-065 (docs/CODE_REVIEW_2026-08_wave2.md:719) and the existing verdict at docs/CODE_REVIEW_2026-08_verdicts.md:1128.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived from source, every number read directly, none taken from the write-up.

- `Code/WCB/WCB_Storage.cpp:1072-1082`: `int kyberPort = 0;` (:1076), then `if (firstComma == -1) { baseCommand = message; baseCommand.toLowerCase(); kyberUseTargeting = false; kyberPort = 2; }`. The `2` is hard-coded and bare — no comment, no guard.
- Dispatch: `WCB.ino:4651-4657` — `rootUpper == "KYBER"` and `argsUpper.startsWith("CLEAR")` → `storeKyberSettings(args)` with `args == "CLEAR"` (no comma). Also `WCB.ino:5239` `storeKyberSettings("clear")` for the `?KYBER_CLEAR` alias. Both are comma-free, so `firstComma == -1` and `kyberPort == 2` deterministically on every clear.
- Clear branch `WCB_Storage.cpp:1184-1212`: `:1188 kyberLocalPort = 0;` destroys the only record of the real port before it is read; `:1191-1194 if (kyberPort > 0) updateBaudRate(kyberPort, 9600);`; `:1200-1204 if (!serialBroadcastEnabled[kyberPort-1]) { = true; saveBroadcastSettingsToPreferences(); }`; `:1205-1209 if (blockBroadcastFrom[kyberPort-1]) { = false; saveBroadcastBlockSettings(); }`. With `kyberPort==2` those are index 1 = S2. The branch `return`s at :1212.
- PERSISTENCE is real, not just runtime. `updateBaudRate` (`WCB_Storage.cpp:146-181`) range-checks the port (:151), validates the baud (:155), calls `applyLiveBaud` (:166), writes `baudRates[port-1]` (:172), then `preferences.begin("serial_baud", false); putInt("Serial2", 9600)` (:175-178). NVS key "Serial2" = 7 chars, under the 15-char limit. `saveBroadcastSettingsToPreferences` (`:331-339`, ns `bdcst_set`, keys "S1".."S5"/"S0") and `saveBroadcastBlockSettings` (`:1583-1590`, ns `bcast_block`, keys "blk1".."blk5") both persist. All key names are well under 15 chars.
- Real port location confirmed: `kyberLocalPort` declared `WCB.ino:162`, set at `WCB_Storage.cpp:1159` on the LOCAL path, restored at `:1458` (`(storedPort > 0 && storedPort <= 5) ? storedPort : 2`), zeroed at `:1462`/`:1466`. The finding's cites (:1159, :1458) are accurate.
- Array bounds: `bool serialBroadcastEnabled[5]` (`WCB.ino:629`), `bool blockBroadcastFrom[5]` (`WCB.ino:600`). Index 1 is in range on this path, so no OOB here (the OOB variant needs `?KYBER,CLEAR,S0` — a separate defect).

REACHABILITY — a real user hits this on ordinary paths, and the repo's own bench fixture already does.

- Firmware config chain: `collectConfigCommands` (`WCB.ino:2762`) emits `BAUD,S1..S5` at `:2804`, `BCAST,OUT/IN` at `:2809-2814`, and `emit("KYBER,CLEAR", true)` at `:2859` in the `else` of `if (Kyber_Local) / else if (Maestro_Remote)` — i.e. for **every board that isn't running Kyber**. `includeInLive=true` puts it in the live restore chain built by `printBackupConfig` (`WCB.ino:5717`, emitter `:5744-5754`). So a board restoring its own `?BACKUP` clobbers its own S2.
- Wizard: `Wizard/parser.js:1355` `add('KYBER,CLEAR')` in the `else` of `mode==='local'`/`'remote'`; default mode is `'none'` (`parser.js:65`) and `parseBackupString` sets `mode='none'; port=null` for a non-Kyber board (`:640-641`). `kyberChanged = fullPush || !baseline || ...` (`:1339-1343`), so a full push always emits it. Order verified by reading top-to-bottom: BAUD `:1310-1315`, BCAST `:1326-1333`, S0 `:1335-1336`, Kyber `:1339-1357`; `add()` is `cmds.push(lfi + cmd)` (`:1230`) and the function returns `cmds.join(delim)` (`:1540`) — no reorder, no dedup. `fullPush = !boardBaselines[n]` (`app.js:7417`, `:7810`).
- IN-REPO BENCH EVIDENCE: `TestConfigs/WCB1_01_base.txt:64` is a single paste containing `?BAUD,S2,57600 … ?BCAST,RESET … ?MAESTRO,CLEAR,ALL^?KYBER,CLEAR^?MP3,CLEAR^?config`, and `:40` documents S2 as "S2 Kyber Maestro port" at 57600. That fixture, as written, leaves S2 at 9600 and the immediately following `?config` prints the wrong value.

INTENT — not deliberate. `:1082` carries no comment. The nearby comments argue the other way: `:1145-1148` ("Don't seize a UART another subsystem already owns"), `:1162` guards the local branch with `if (kyberPort > 0 && kyberPort <= 5)` while the clear branch does not, and `updateBaudRate`'s `:168-172` comment exists specifically to stop a config-tool re-push loop caused by stale baud. `WCB_Help.cpp:228` documents `?KYBER,CLEAR` only as "Disable Kyber". `git log -L1184,1213` shows the region only ever touched incidentally (adebe4e renamed `Kyber_Remote`→`Maestro_Remote`, 9a390af added the `kyberLocalPort = 0` line) — nobody defended `kyberPort = 2`.

SCOPE CORRECTION (narrows, does not refute): the exposure is smaller than the title implies. Every subsystem emitter runs AFTER Kyber in both chains (`WCB.ino:2864 emitHelpers()`; parser.js MP3 `:1360`, HCR `:1378`, DFP `:1395`, WLED `:1413`, MAESTRO `:1457`), and each re-reserves its port — `WCB_Maestro.cpp:659-675`, `WCB_WLED.cpp:165-178` all re-apply baud and both broadcast flags. So an S2 hosting a Maestro/WLED/MP3/HCR self-heals within the same push. It is also a no-op on a default board (S2 already 9600, broadcast on, unblocked — `WCB.ino:634-640`, `:629`, `:600`). Residual real exposure: (a) a plain device on S2 configured only via `?BAUD`/`?BCAST` — exactly the `TestConfigs` Kyber-Maestro/Marcduino case; (b) a deliberately blocked/disabled S2 broadcast flag; (c) the mirror case the finding omits — when Kyber genuinely was LOCAL on S3, `?KYBER,CLEAR` leaves S3 stuck at 115200 with broadcast blocked forever (set at `:1163-1175`) while molesting S2 instead. It also self-heals on the next delta push after a pull (`kyberChanged` false → no CLEAR emitted).

DUPLICATE: this is `docs/CODE_REVIEW_2026-08_wave2.md:719` F-065 re-filed at the firmware location, and it was already verified once as `docs/CODE_REVIEW_2026-08_verdicts.md:1128` (filed there against `Wizard/parser.js:1355`). Don't count it three times.

FIX CHECK — the proposed fix is a no-op for the reported failure. `int clearPort = (kyberPort >= 1 && kyberPort <= 5) ? kyberPort : kyberLocalPort;` evaluates its condition against `kyberPort == 2` on the bare-clear path (hard-coded at `:1082`), so `2 >= 1 && 2 <= 5` is true and `clearPort == 2` — byte-for-byte the current behaviour. It only changes anything when `kyberPort` is out of range, i.e. the `?KYBER,CLEAR,S0` OOB case (a different finding, F-066). And you cannot simply delete `kyberPort = 2` either: it is load-bearing for the bare `?KYBER,LOCAL` form (`WCB.ino:4654` → `storeKyberSettings("LOCAL")`, `WCB.ino:5233` `storeKyberSettings("local")`), which `WCB_Help.cpp:224` documents as "This board has Kyber on S2".

</details>

### `Code/WCB/WCB.ino:398` — CONFIRMED · S1 → **S2** · ⚠ PROPOSED FIX IS WRONG

**Pending-timer-chain queue only closes the WiFi-task half of the commandGroups race — the local-serial path still mutates the vector from serialCommandTask while loop() iterates it**

**Correction to the finding as filed:**

Three corrections to the finding as written.

(1) TRIGGER SET is overstated. wave2's "any byte-complete line arrives on USB or S1-S5 — the Wizard pushing config" is wrong: WCB.ino:6489 excludes every LocalFunctionIdentifier ('?') line from parseCommandGroups, so the Wizard's config traffic cannot trip it. Only an exact `?STOP` (WCB.ino:6431) or a non-'?' line containing `;t`/`;T` (WCB.ino:6490) mutates commandGroups from serialCommandTask.

(2) "Reproduces reliably under any load" is wrong. The window is ~1 ms per command in a firing group, once per inter-group delay, against a 5 ms serialCommandTask poll — on the order of 0.5-1% per operator action for typical `;t500`/`;t1000` chains. Real UB, low per-event probability. Hence S2, not S1.

(3) DEFECT SITE. The finding is filed at WCB.ino:398 (the false comment). The actual defect sites are command_timer.cpp:187 (reference bound into the vector) and :203-209 (range-for held across `vTaskDelay`), with WCB.ino:6431 and :6490 as the cross-task mutators. The comment at WCB.ino:398-399 is a separate, definite bug — it records a false invariant and must be corrected regardless of which fix is chosen.

CORRECTED FIX. Prefer fixing the victim, not every mutator: in processCommandGroups, stop holding a reference across the yield. Copy `commandGroups[currentGroupIndex].commands` into a local `std::vector<String>` (and re-read `currentGroupIndex`/`commandGroups.size()` after the loop rather than trusting the pre-yield values), so no caller — present or future — can dangle it. If instead the deferral route is taken, it must use a heap-pointer queue modelled on `CommandQueueItem` (WCB.ino:890-901, `char *cmd` malloc'd at :1926, freed in loop() at :7504) rather than the 220-byte fixed slot, must push the `?STOP` through the SAME queue as an ordered marker, and must log overflow drops.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived from source, nothing taken from the write-up:

1. The comment is real and says what is claimed (WCB.ino:389-399, verbatim at :398-399: "The local-serial entry point and SEQ playback both run inside loop() already, so they keep calling parseCommandGroups() directly").

2. The SEQ half of that claim is TRUE. WCB_Storage.cpp:615 `parseCommandGroups(stripped)` sits in `recallCommandSlot()` (WCB_Storage.cpp:556); its only caller chain is recallStoredCommand (WCB.ino:6085) <- processCommandCharcter (WCB.ino:5854) <- handleSingleCommand (WCB.ino:4385) <- loop() (WCB.ino:7512). `grep -rn "handleSingleCommand" Code/WCB/` returns exactly one call site: WCB.ino:7512, inside loop(). So SEQ playback really is loop-only.

3. The local-serial half is FALSE. `processIncomingSerial` is defined at WCB.ino:6361; it calls `processSerialCommandHelper` at WCB.ino:6411; that helper (defined WCB.ino:6423) calls `stopTimerSequence()` at WCB.ino:6431 and `parseCommandGroups(data)` at WCB.ino:6490. `grep -rn "processIncomingSerial" Code/WCB/` gives call sites only at WCB.ino:6598, :6603, :6604, :6605, :6613, :6618, :6621 — every one inside `serialCommandTask` (WCB.ino:6595), which is created at WCB.ino:7439 as `xTaskCreatePinnedToCore(serialCommandTask, "Serial Command Task", 4096, NULL, 1, NULL, 1)` — priority 1, core 1. It is NOT called from loop() (WCB.ino:7476-7517). The reviewer's line numbers all check out.

4. The victim is real. loop() calls `processCommandGroups()` at WCB.ino:7480. In command_timer.cpp:187 it binds `CommandGroup &group = commandGroups[currentGroupIndex];` — a reference INTO the vector — dereferences it at :189 (`group.commands[0]`) and :195 (`group.delayAfterPrevious`), then iterates `for (const String &cmd : group.commands)` at :203 with an unconditional `vTaskDelay(pdMS_TO_TICKS(1))` at :208 inside that loop. The reference and the range-for iterators are held live across a guaranteed reschedule.

5. The mutators genuinely free that memory. `stopTimerSequence()` (command_timer.cpp:53-59) calls `commandGroups.clear()` — destroys every CommandGroup, freeing each inner `std::vector<String>` buffer and every String's heap buffer. `parseCommandGroups()` (command_timer.cpp:85-88) does `commandGroups.clear()` then `push_back` (:174, :176), which additionally reallocates the element buffer if the new chain needs more capacity. Either way `group` / the range-for iterators dangle and `parseCommandsAndEnqueue(cmd, 0)` at :207 runs on a destroyed String.

6. No synchronization exists. `grep -rn "Mutex|SemaphoreHandle|portENTER_CRITICAL|taskENTER" Code/WCB/*.{ino,cpp,h}` returns only WCB.ino:1120 and :1134 — the ETM ACK queue mux. Nothing guards `commandGroups`.

7. Concurrency is provable without relying on time-slicing assumptions: the `vTaskDelay(pdMS_TO_TICKS(1))` at command_timer.cpp:208 blocks loopTask outright, and serialCommandTask re-arms every 5 ms (its own `vTaskDelay(pdMS_TO_TICKS(5))` at the end of WCB.ino:6595's while-loop), so it is ready inside roughly one in five of those yields.

INTENT — this is not a documented trade-off. `git log -L 389,399:Code/WCB/WCB.ino` shows the comment was introduced by 5c49642 "bug fixes" (2026-05-27). `git log -S"serialCommandTask" -- Code/WCB/WCB.ino` shows serialCommandTask predates it by several commits (483b4d8, d579147, 4dce195, ff0b370). So the invariant was already false the day it was written; it is a mistaken belief, not an accepted risk. Nothing in CLAUDE.md or docs/ carves this out.

REACHABILITY — real but narrower than claimed, which is why I downgrade. WCB.ino:6489 guards the parse with `!data.startsWith(String(LocalFunctionIdentifier)) && isTimerCommand(data)`, so every `?`-prefixed line (the entire Wizard config push, ?SEQ, ?MGMT, ?OTA...) goes to parseCommandsAndEnqueue and never touches commandGroups. Only two serial inputs can mutate it: a line that is exactly `?STOP` (checkForTimerStopRequest, command_timer.cpp:61, uses `equalsIgnoreCase`), or a non-`?` line containing `;t`/`;T` anywhere (isTimerCommand, command_timer.cpp:48-50). Exposure per group fire is ~1 ms x commands-in-group out of the whole inter-group delay, so for a typical `;t500`/`;t1000` chain an operator `?STOP` has on the order of a 0.5-1% chance of landing in the window. Genuine latent UB with a LoadProhibited/heap-corruption outcome, but not the deterministic crash the write-up implies.

For context: this same defect is already written up twice in the repo's own prior review docs (docs/CODE_REVIEW_2026-08_wave1.md:67, wave2.md:48) — it is a repeat finding, not something that has since been fixed.

</details>

### `Code/WCB/WCB.ino:1569` — CONFIRMED · S2

**processETMCharAck silently discards ACKs for message index >= 200, and the corresponding sentTime writes are unbounded**

**Correction to the finding as filed:**

The finding is real but misfiled. Correct it as follows:

LOCATION: not WCB.ino:1569. The defect is the unclamped `int totalMessages = etmCharMessageCount * peerCount;` at WCB.ino:1456 and the three unguarded writes it authorizes at :1470, :1506 and :1513.

TITLE: "processETMChar writes etmCharPhaseSentTimes[3][200] with an unbounded index — totalMessages is never clamped to 200".

MECHANISM: the ACK read is already correctly guarded, at the CALL SITE (WCB.ino:1249) before indexing at :1250 — the identical check at :1569 is a redundant second guard, not the thing protecting the read. Drop the "silently discards ACKs" framing as the headline; it is a real but secondary effect (an overflowing run can never satisfy allDone and burns the full per-phase timeout, e.g. 245 s at count=200/2 peers).

Also correct the overflow geometry: because indexing is row-major into one flat 600-element object, phase 1 (row 0) and phase 2 (row 1) writing idx 200+ corrupt the OTHER PHASES' ROWS and stay inside the object until idx >= 600 / >= 400. It is PHASE 3 (row 2, base 400) that escapes the object the moment idx hits 200. Same threshold, different phase.

REACHABILITY should be stated much more widely than "11+ boards": the condition is simply `etmCharMessageCount * peerCount > 200`. Since `?ETM,COUNT` accepts up to 200 (WCB.ino:4696) and help advertises that range (WCB_Help.cpp:269-270), `?ETM,COUNT,200` then `?ETM,CHAR` on a THREE-board mesh (2 online peers, totalMessages=400) writes 800 bytes past the array. Entry points: `?ETM,CHAR` (:4704), `etmchar` (:5311), remote `?MGMT,ETM,CHAR,<n>` -> handleETMReqPacket (:3246), and the Wizard buttons (Wizard/app.js:11953, :11982).

Drop the claim that it clobbers etmCharPhaseMessageIndex / etmCharPhaseStartTime / etmCharLastSendTime — those are declared next in source (:578-580) but .bss ordering is not guaranteed, so which globals are hit is unknown without a link map.

FIX: keep the proposed clamp but put it only at :1456 (startETMChar never computes totalMessages, so there is nothing to change there), and also report the degraded count so the ":1436 Messages per board per phase: %d" banner does not lie:
    static const int ETM_CHAR_MAX_MSGS = 200;   // == etmCharPhaseSentTimes row width (:577)
    int totalMessages = min(etmCharMessageCount * peerCount, ETM_CHAR_MAX_MSGS);
A cleaner alternative is to tighten the setters' constrain upper bound (WCB.ino:4696, :5303) and the advertised range in WCB_Help.cpp:270 so count*peers can never exceed the row — but that alone does not fix the large-fleet case, so the :1456 clamp is still required.

<details><summary>Full verification reasoning</summary>

WHAT I OPENED: Code/WCB/WCB.ino (lines 120, 260-290, 550-600, 1200-1275, 1400-1620, 3238-3250, 4685-4715, 5295-5320, 6850-6900), Code/WCB/WCB_Storage.cpp:2210-2240, Code/WCB/WCB_Help.cpp:269-270, Wizard/app.js, Wizard/parser.js. Grepped for MAX_WCB_COUNT, etmCharMessageCount, etmCharPhaseSentTimes, startETMChar/processETMChar, Default_WCB_Quantity. Ran git blame on :575-580 and :1566-1572.

MECHANISM — re-derived, not taken from the summary:
- `unsigned long etmCharPhaseSentTimes[3][200];` at WCB.ino:577. `unsigned long` is 4 bytes on ESP32/S3, so the object is 600 elements / 2400 bytes.
- `int totalMessages = etmCharMessageCount * peerCount;` at WCB.ino:1456. There is no clamp on it anywhere.
- `MAX_WCB_COUNT` is 20 (WCB.ino:120). peerCount loops i=1..20 gated on `wcbPeerActive[i-1] && boardTable[i-1].online` (:1447-1448); `rebuildActivePeers()` (:6857-6861) excludes self, so it is normally <=19.
- `etmCharMessageCount` defaults to 20 (:269) and is `constrain(..., 10, 200)` at both setters — :4696 (`?ETM,COUNT`) and :5303 (legacy `etmcharcount`). The 10-200 range is *advertised to users* in help (WCB_Help.cpp:269-270: "Range: 10-200, Default: 20"). loadETMSettings does an unclamped `getInt("etmCharCount", 20)` (WCB_Storage.cpp:2231), but its only writer is the already-constrained putInt at :2218, so 200 is the real ceiling.
- The three writes are gated ONLY by `etmCharPhaseMessageIndex < totalMessages`: :1470 `[etmCharPhase-1][etmCharPhaseMessageIndex]` (phases 1-2), :1506 and :1513 `[2][msgIdx]` (phase 3). No bound check on any of them. So totalMessages > 200 writes past the row.

ROW ARITHMETIC — the reviewer missed this and it matters. Because indexing is row-major into one flat 600-element object:
- phase 1 -> row 0 (base 0): idx 200..599 corrupts rows 1 and 2 (garbage latency data, still inside the object); escapes the object only at idx >= 600, which needs count*peers > 600.
- phase 2 -> row 1 (base 200): escapes at idx >= 400.
- phase 3 -> row 2 (base 400): escapes the object at idx >= 200 — immediately.
So genuine out-of-object corruption is phase 3, and the threshold is exactly totalMessages > 200 — the same number the reviewer gave, by a different route. Phase 3 is always entered (advanceETMCharPhase, :1540-1551), so the row-2 writes always run.

REACHABILITY — a real user can trivially produce this:
- Local: `?ETM,CHAR` (:4704) and `etmchar` (:5311), both in the local command parser, reachable from any serial source.
- Remote: `?MGMT,ETM,CHAR,<n>` -> handleETMReqPacket -> startETMChar() (:3246), password-checked at :3238.
- Wizard buttons: Wizard/app.js:11953 (local) and :11982 (relay form). `ETM,COUNT` is also pushed by the Wizard config round-trip (Wizard/parser.js:1493).
- The only preconditions are `etmEnabled` and `Default_WCB_Quantity >= 2` (:1408-1412) plus `peerCount != 0` (:1450). Nothing bounds totalMessages.
- Worked example: a THREE-board mesh (2 online peers) with the documented, in-range `?ETM,COUNT,200` gives totalMessages = 400. Phase 3 writes flat offsets 400..799 into a 600-element object = 200 dwords = 800 bytes past the end of .bss storage. The reviewer's "12-board fleet" framing is the stock-config trigger only and makes this look more exotic than it is.

INTENT — not deliberate. git blame shows the array (1ea6312c, 2026-02-24) and the read guard (8588cb6f, 2026-03-19) were added at different times; the :1569 comment "guard against out-of-bounds sentTime read" shows the author was thinking about the read only and never revisited the writes. No comment, doc, or CLAUDE.md rule sanctions this.

WHERE THE REVIEWER IS WRONG (does not change the verdict):
1. Location/title. The finding is filed at :1569 and says processETMCharAck discards ACKs >= 200. In fact the CALLER at :1249 applies the identical `phase >= 0 && mIdx >= 0 && mIdx < 200` test before indexing at :1250, so processETMCharAck is never even entered for mIdx >= 200. The :1569 check is a redundant second guard. The read is correctly guarded in both places; the defect is purely the unbounded writes, which live at :1456/:1470/:1506/:1513.
2. The named clobber victims (etmCharPhaseMessageIndex / etmCharPhaseStartTime / etmCharLastSendTime at :578-580) are source-order speculation. .bss layout is not guaranteed to follow declaration order; without a link map you cannot say which globals get hit. The corruption is real, the specific victims are not established.
3. "up to 200 x 19 = 3800" — arithmetic is right, but self-exclusion only holds when WCB_Number is within 1..20.

SECONDARY (real but minor) CONSEQUENCE the title half-captures: with mIdx >= 200 ACKs dropped at :1249, `totalAcked >= totalMessages` can never hold, so an overflowing run never satisfies allDone and each phase burns its full timeout — `totalMessages * (delay + timeout) + 3000/5000` (:1483-1484, :1527-1528), e.g. 400*600+5000 = 245 s per phase.

NOTE ON PRIOR WORK: I found docs/CODE_REVIEW_2026-08_verdicts.md already containing a verdict on this same finding. It is an artifact of this same review pass, not independent evidence, so I derived every fact above from the source myself; it happens to agree.

</details>

### `Code/WCB/WCB.ino:1585` — CONFIRMED · S2 · ⚠ PROPOSED FIX IS WRONG

**Phase 3 "Loaded Network" is never loaded — the ETMLOAD trigger is sent non-ETM and is dropped by every receiver's ETM-mismatch guard, making processETMLoad unreachable**

**Correction to the finding as filed:**

The mechanism, line numbers and severity are right as stated. Two things the finding does not capture, both of which change the fix:

1. `processETMLoad()`'s OWN load traffic is also sent non-ETM — `sendESPNowMessage(0, payload, false)` at :1614 and `sendESPNowMessage(target, payload, false)` at :1629. Fixing only the trigger leaves those hitting the same guard on every peer. That does not defeat the load (the frames are still transmitted and still consume RF airtime and the peers' WiFi RX path, which is what "loaded network" means), but it does mean every peer with `?DEBUG,ON` or `?DEBUG,ETM,ON` prints `"[ETM] ⚠️ Dropped non-ETM packet — ETM is ON here but sender has ETM OFF. Enable ETM on all boards."` (:3708) roughly once per 100 ms for the full 10 s of the load — an actively misleading message emitted during the very diagnostic being run. Note `startETMChar` suppresses `debugETM` on the INITIATOR only (:1417-1421), never on the peers.

2. `lastReceivedViaESPNOW` latches. Whichever route delivers ETMLOAD, the receive path sets `lastReceivedViaESPNOW = true` first (:3883 on the ETM path, :4159 on the normal path), and it is only cleared by a serial command (:6404) or the per-item restore in the queue drain (:7510) — which runs AFTER `processETMLoad()` at :7489. During phase 3 the peer keeps receiving the initiator's ETM measurement packets, re-latching it every time. So `sendESPNowMessage(0, …)` at :1614 hits the loop-prevention early-return at :2275 and the broadcast third of the load is silently dropped at the sender. Load degrades to unicast-only.

Recommended fix instead of the reviewer's: whitelist the characterization control/load payloads in the mismatch guard rather than moving the trigger onto ETM. At :3706 add a fifth predicate, e.g.
    bool isEtmCharPacket = (strncmp(peek.structCommand, "ETMLOAD", 7) == 0 ||
                            strncmp(peek.structCommand, "ETMCHAR_", 8) == 0 ||
                            strncmp(peek.structCommand, "LOAD_", 5) == 0);
and include `!isEtmCharPacket` in the `if`. This keeps the existing :4175 handler working, needs no new ETM arm, costs no pending-table slot, and kills the misleading warning flood. Then also address (2) — either save/clear/restore `lastReceivedViaESPNOW` around the two sends in `processETMLoad`, or drop the broadcast third and make the load unicast-only and say so in the phase label. Separately, the phase-3 label at :1799 stays a lie until the load actually runs, so this and the sibling "phase 2 says Broadcast but sends unicast" finding should be fixed together.

<details><summary>Full verification reasoning</summary>

I re-derived every load-bearing fact from source; the reviewer's observation holds exactly.

SEND SIDE. `Code/WCB/WCB.ino:1493-1496` — phase 3 entry does `sendESPNowMessage(0, "ETMLOAD", false);` at :1495. In `sendESPNowMessage` (:2273) the ETM branch is gated `if (etmEnabled && useETM && !isPWMMessage)` (:2280), so `useETM=false` falls through to the NORMAL SEND PATH and emits an `espnow_struct_message` (the 249-byte form), not `espnow_struct_message_etm`.

RECEIVE SIDE. `espNowReceiveCallback` classifies by length at :3676-3681 (`isETMPacket` only when `len == sizeof(espnow_struct_message_etm)`), then the mismatch guard at :3688-3711. I evaluated the whitelist against the literal payload myself:
- `peek.structTargetID` = "0" → `atoi` = 0. `WCB_TARGET_RAW_SERIAL` is 97 and `WCB_TARGET_KYBER` is 98 (`WCB_Storage.h:14-15`, mirrored `WCB.ino:122-123`), and `structTargetID[0]` is '0' not 'K' → `isRawPacket` false (:3692).
- `structCommand[0]` == 'E' → not ';' → `isPWMPacket` false (:3693); not '{' → `isRcJsonPacket` false (:3704).
- `structCommand[1]` == 'T' → not 'M'/'m' → `isMaestroPacket` false (:3697).
All four false → `return` at :3710. Every ETM-enabled peer drops it.

ONLY WRITER. `grep -n etmLoadRunning Code/WCB/WCB.ino` returns exactly one `= true`: :4176, and it sits at :4175 inside the NORMAL RECEIVE PATH (block starts :4006; `lastReceivedViaESPNOW = true` at :4159 immediately precedes it). That path is only reached by a non-ETM-sized packet, which the guard above already discarded when `etmEnabled`. The ETM path at :3893 (`if (!etmCmd.startsWith("ETMCHAR_") && !etmCmd.startsWith("ETMLOAD"))`) *excludes* ETMLOAD from processing and — I read to the close of the block at :3996-4003 — there is no `else` arm, so even an ETM-wrapped ETMLOAD would be ACKed and discarded. `processETMLoad()` (:1585) is driven from `loop()` at :7489 but its first line `if (!etmLoadRunning) return;` (:1586) never passes on any peer.

CAN A REAL USER PRODUCE THE STATE? Yes, and in fact it is the *only* reachable state. `startETMChar()` hard-requires `etmEnabled` (:1406-1409), and `processETMChar()` only counts peers with `wcbPeerActive[] && boardTable[].online` (:1447-1449); `boardTable[].online = true` is set only in the ETM receive path (:3769). So a peer that could be a target is by construction ETM-on, which is exactly the condition under which the guard drops ETMLOAD. There is no fleet configuration in which the trigger is delivered. Consistent with CLAUDE.md rule 4 (ETM is all-or-nothing).

INTENT. Not deliberate. `git log -L 1490,1500:Code/WCB/WCB.ino` shows the line was born in cc6c078 ("ETM") with the comment `// broadcast, not ETM tracked` — i.e. the author's intent was "don't burn a pending-ACK slot", written without awareness that the mismatch guard would eat it. Later commit 1ea6312 only stripped the comment. The `"ETMLOAD"` clause in the :3893 filter shows the author expected it on the ETM path too, but never wrote the handler. Nothing in docs/ documents this as a trade-off.

CONSEQUENCE (this is where I temper the finding). `buildETMCharResultsString` (:1791) prints phase 3 as "Loaded Network (all boards transmitting)" (:1799) when in fact only the initiator is transmitting. The "Recommended ETM timeout" (:1826-1828) is `max(avgLatency over ALL phases) * 5`, rounded up to 50 ms, floored at 150 — so the miss only matters when phase 3 would have been the worst phase, and the number is *printed*, never auto-applied (`?ETM,TIMEOUT` at :4680 is a separate operator action). Applying a too-tight timeout costs retries (max 3, :1300) which are ACKed-but-not-executed by the seq-history dedup at :3843-3849 — wasteful, not corrupting. So: real, operator-visible wrong behaviour in a diagnostic tool, no runtime/wire effect. Under this repo's own legend (docs/CODE_REVIEW_2026-08.md:12: S2 = "wrong behaviour in a real scenario", S3 = "latent race, bound, leak, overflow") this is S2, but it belongs at the very bottom of the S2 band and should be triaged below any S2 that touches the wire.

Note: this is the same defect already recorded as F-010 / docs/CODE_REVIEW_2026-08_wave1.md:1436-1450; the wave doc's analysis matches mine.

</details>

### `Code/WCB/WCB.ino:1892` — CONFIRMED · S2 · ⚠ PROPOSED FIX IS WRONG

**applyLiveBaud tears down a live SoftwareSerial (end() then begin()) while other tasks may be reading it — end() leaves m_rxValid true with a freed buffer**

**Correction to the finding as filed:**

Two mechanism corrections, both widening the finding, and the severity is right as claimed.

1. Raw-mapping is NOT a precondition. The reviewer's scenario requires `?MAP,S3,...` so RawSerialForwardingTask is polling. Not needed: `serialCommandTask` polls `processIncomingSerial(Serial3/4/5)` every 5 ms on any port that is *not* raw-mapped (WCB.ino:6603-6605, `isSerialPortRawMapped` false by default per WCB_Storage.cpp:1807-1816), hitting `serial.available()` at :6362 and `serial.read()` at :6384. The default configuration is exposed.

2. Reads are not the only fatal path. `UARTBase::write()` calls `if (m_rxValid) { rxBits(); }` at SoftwareSerial.cpp:349 — *before* the `if (!m_txValid) return -1;` at :350 — and `rxBits()` dereferences `m_isrBuffer->for_each(...)` unconditionally at :475. So a concurrent *transmit* on S3-S5 from another task crashes too, e.g. RawSerialForwardingTask writing to an S3-S5 output port at WCB.ino:6679-6680. The `m_isrBuffer`-null window is also slightly longer than the `m_buffer` one: it closes at beginRx SoftwareSerial.cpp:105, not :99. `peek()` (:452) and `flush()` (:436) have the same shape.

Also worth noting for triage: `updateBaudRate` (WCB_Storage.cpp:146) has no "already this value" short-circuit, and the `?BAUDSn` handler (WCB.ino:5689) calls it unconditionally — so every Wizard config restore tears down S3, S4 and S5 even when the baud is unchanged. Making `applyLiveBaud` a no-op when `baud == baudRates[port-1]` is a one-line change that removes most of the real-world exposure without touching concurrency, and is worth doing regardless of the real fix.

<details><summary>Full verification reasoning</summary>

MECHANISM — every library fact the reviewer cited is accurate, and I re-read all of it in the version that actually ships.

Which library ships: `.github/workflows/build.yml:66` does `arduino-cli lib install "EspSoftwareSerial@8.1.0"`, and the sketchbook copy at `Arduino-Code/libraries/EspSoftwareSerial/library.properties:2` is also `version=8.1.0` — same code both locally and in CI, so the source I read is what runs.

- `UARTBase::end()` — SoftwareSerial.cpp:125-136. It calls `enableRx(false)`, sets `m_txValid = false`, then `m_buffer.reset()`, `m_parityBuffer.reset()`, `m_isrBuffer.reset()`. It never touches `m_rxValid`.
- `m_rxValid` is declared `bool m_rxValid = false;` (SoftwareSerial.h:338) and is assigned true at exactly one site, `beginRx()` SoftwareSerial.cpp:108. Grep over the whole `src/` tree returns no site that clears it (only reads at .cpp:69, :183, :201, :221, :238, :256, :349, :437, :453). So after `end()` the object is `m_rxValid == true` with `m_buffer == nullptr` and `m_isrBuffer == nullptr`.
- `read()` SoftwareSerial.cpp:200-202 → `if (!m_rxValid) return -1;` then `m_buffer->available()` — unguarded null deref. Same shape in `available()` :255-258 (which first calls `rxBits()`), `peek()` :452-454, `read(buf,size)` :220-223, `flush()` :436-438.
- `rxBits()` :463-488 dereferences `m_isrBuffer->for_each(...)` at :475 with no null check.

`applyLiveBaud()` (WCB.ino:1886-1909, read in full) does `Serial3.end(); Serial3.begin(baud, SWSERIAL_8N1, SERIAL3_RX_PIN, SERIAL3_TX_PIN, false, 95);`. That 6-arg `begin` is SoftwareSerial.h:399-410 → `UARTBase::begin` (pure arithmetic, .cpp:80-93) → `beginRx()` which re-allocates `m_buffer` at .cpp:99 and `m_isrBuffer` at .cpp:105. So the exposure window (`m_rxValid` true, buffers null) runs from .cpp:130 to .cpp:99 for `m_buffer` and to .cpp:105 for `m_isrBuffer` — roughly six heap ops (free 95 B + free ~3800 B + two objects, then malloc them back), on the order of 10-20 µs.

REACHABILITY — genuine, and *wider* than the reviewer claimed.
- Concurrency is real and on the same core. `loop()` drains the command queue and calls `handleSingleCommand` (WCB.ino:7503-7512), so `?BAUDS3,…` → `updateBaudRate` (WCB_Storage.cpp:146-181) → `applyLiveBaud` (:166) runs in loopTask. arduino-esp32 3.3.4 creates loopTask at priority 1 on `ARDUINO_RUNNING_CORE` (cores/esp32/main.cpp:113), and the installed sdkconfig for both targets has `CONFIG_ARDUINO_RUNNING_CORE=1` and `CONFIG_FREERTOS_HZ=1000`. Every competing task is *also* core 1 priority 1: serialCommandTask (WCB.ino:7439), KyberLocalTask (:7443), KyberRemoteTask (:7447), RawSerialForwardingTask (:7464). Equal priority + same core = 1 ms round-robin time slicing, so loopTask can be preempted mid-`applyLiveBaud`. The codebase already knows this — WCB.ino:6663-6664 comments "a get-query can start under us mid-drain (same-core time-slice)".
- The reviewer's premise that S3 must be raw-mapped is too narrow. `serialCommandTask` calls `processIncomingSerial(Serial3..Serial5)` every 5 ms whenever the port is *not* raw-mapped (WCB.ino:6603-6605) — and `isSerialPortRawMapped` (WCB_Storage.cpp:1807-1816) is false by default. `processIncomingSerial` calls `serial.available()` at :6362 and `serial.read()` at :6384. So on a stock board with S3-S5 initialised, a concurrent reader is polling those exact objects continuously.
- `RawSerialForwardingTask` is worse than described: it only delays when idle (`if (!hadData) vTaskDelay(1ms)`, WCB.ino:6717-6720) — while raw data flows it spins with no delay, so it is essentially always ready at a tick boundary.
- Kyber tasks add a third path when `kyberLocalPort` is 3-5 (allowed: `if (kyberPort > 0 && kyberPort <= 5)`, WCB_Storage.cpp:1162); `forwardDataFromKyber` reads `kyberSerial.available()/read()` at WCB.ino:4254-4256.
- No guard exists anywhere: grep for `portENTER_CRITICAL|xSemaphoreTake|SemaphoreHandle_t|taskENTER_CRITICAL|vTaskSuspendAll` across `Code/WCB/` returns only the ETM ACK queue mux (WCB.ino:1120, :1134). Nothing protects serial objects.
- Trigger is routine, not exotic. The plain `?BAUDSn` path (WCB.ino:5689) and the deprecated `?BAUDn` path (:5662) call `updateBaudRate` unconditionally — `updateBaudRate` has no "already this value" short-circuit, so every Wizard config restore containing `?BAUDS3/S4/S5` performs three live teardowns even when nothing changed. `?MAESTRO,CLEAR` does five in a row (WCB_Maestro.cpp:913-915). The device-config paths do guard on a change (WCB_WLED.cpp:165, WCB_HCR.cpp:494, WCB_MP3.cpp:341, WCB_DFP.cpp:295, WCB_Maestro.cpp:659).

INTENT — not a documented trade-off. `git log -L 1886,1909:Code/WCB/WCB.ino` shows the block was added whole in abb46ee (2026-05-19, "Live baud re-init for software serial S3-S5; bump to 6.0.3"). The comment at WCB.ino:1880-1885 explains only *why* the re-begin is needed and that PWM ports are skipped; it says nothing about task safety. No doc in `docs/` covers it.

SEVERITY — the reviewer's S2 is right by this review's own calibration. The ladder (docs/CODE_REVIEW_2026-08.md:10-11) reads S1 = crashes, S3 = latent race, so this sits between them; the precedent at :66-78 settles it — the `commandGroups` use-after-free, also a crash-race, was explicitly rated "S2, not S1" because the per-event probability was ~0.5-1%. Here a 1 ms tick against a ~10-20 µs window gives ~1-2% per S3-S5 teardown, times the chance the switched-to task enters that port's code (high, given serialCommandTask walks S3/S4/S5 on every wake). Same band, and the trigger is a routine Wizard config save. Consequence is a LoadProhibited panic and reboot mid-restore, i.e. a half-applied config in NVS. S2 stands.

</details>

### `Code/WCB/WCB.ino:2053` — CONFIRMED · S2 · ⚠ PROPOSED FIX IS WRONG

**Factory-reset backup chain: tokens after ?FUNCCHAR carry the NEW func char, but the ?SEQ,SAVE / ?CS / ?MGMT splitter matches on the RECEIVING board's LocalFunctionIdentifier — stored sequences are split on ^ and their ta**

**Correction to the finding as filed:**

The defect stands, but the proposed fix is a no-op and the stated alternative is actively harmful. The reviewer inverted which character is on the wire.

Why the primary fix does nothing: the offending token is `#SEQ,SAVE,` — it carries the SOURCE board's custom char, exactly as the finding's own description says. On the fresh target `LocalFunctionIdentifier == '?'`, so `startsWith(String(LocalFunctionIdentifier) + "SEQ,SAVE,")` and `startsWith("?SEQ,SAVE,")` are the SAME test. Adding the second term changes nothing. Same for the boundary half: `commandDelimiter + LocalFunctionIdentifier` and `commandDelimiter + '?'` are both `"^?"`, while the real next boundary is `"^#"`. Both halves are no-ops in the scenario they are meant to fix. (In the mirror case — a '?'-sourced chain pasted onto a board that already has `#` — the '?' alternative would make the matcher fire, but the boundary search would then find nothing and the FIRST `?SEQ,SAVE` would swallow every remaining token to end-of-string, including all later SEQ,SAVE and the PWM `MAP` lines. That is worse than the current bug.)

Why the alternative ("stop flipping defaultFunc at :5749, keep the whole factory chain on '?'") is worse: its premise — "the func char is dispatched by the token's own content, not its prefix" — is false. `handleSingleCommand` dispatches on `cmd.startsWith(String(LocalFunctionIdentifier))` (WCB.ino:4380), else `String(CommandCharacter)` (:4384), else `processBroadcastCommand` (:4388). `?FUNCCHAR,#` is near the front of the FIFO, so it sets LFI to `#` early in the drain; every remaining `?`-prefixed token would then match neither :4380 nor :4384 and be broadcast as raw text out the serial ports and the mesh. The entire rest of the restore would be lost. The flip at :5749 exists precisely for this, as its own comment at :5739-5740 states.

Correct fix (splitter-side, handles both directions): give `parseCommandsAndEnqueue` a chain-local func char that mirrors the emitter's flip. Initialise `char curFunc = LocalFunctionIdentifier;` before the `while` at :2012; use `curFunc` in place of `LocalFunctionIdentifier` in the `?CS` test (:2015-2016, :2020, :2022), the `?SEQ,SAVE` test and boundary (:2053, :2057), and the `?MGMT` test (:2084); and after each token is taken, if it is `<curFunc>FUNCCHAR,<one char>` (case-insensitive on the verb), set `curFunc` to that character for the remainder of the walk. This is chain-local state on the stack, matching the pattern already used for `ifSkipping` (:2010), and it costs nothing on the common default-'?' path.

Do NOT try the writer-side alternative of moving `FUNCCHAR` to the end of the chain: `collectConfigCommands` emits PWM mappings last on purpose (`// mappings LAST — restore triggers a reboot`, WCB.ino:2916) and `configurePWMMapping` does `delay(3000); ESP.restart();` (WCB_PWM.cpp:339-343), so anything appended after them would never run.

Two citation slips in the finding, neither load-bearing: the generic fall-through branch is :2096-2115 (finding says :2095-2113), and the boundary line is :2057 (finding says :2058).

Worth recording as a docs/comment constraint when this is fixed: the factory chain's prefix is not constant — it flips at `?FUNCCHAR`, and any split-time matcher keyed on `LocalFunctionIdentifier` must track that flip, because the split completes before a single queued token executes.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived from source, not from the finding.

1. Emitter flip is real. `printBackupConfig` declares `String defaultFunc = "?"` (WCB.ino:5732) and the emitter appends `defaultFunc + c` to the factory chain (WCB.ino:5748), then flips it: `if (c.startsWith("FUNCCHAR,")) defaultFunc = String(LocalFunctionIdentifier);` (WCB.ino:5749). The separator `defaultSep` deliberately never flips (:5731, comment :5741-5743).

2. Ordering is as claimed. In `collectConfigCommands`, `emit("FUNCCHAR," + String(LocalFunctionIdentifier), false)` is at WCB.ino:2800 (guarded `if (LocalFunctionIdentifier != '?')`), and `emit("SEQ,SAVE," + key + "," + value, true)` is at WCB.ino:2910 — 110 lines later, after Kyber, the sub-emitters, ETM, CONTROLLER and WDP. So on a source board with `?FUNCCHAR,#`, the factory chain contains `...^?FUNCCHAR,#^#CMDCHAR,;^...^#SEQ,SAVE,wave,;M1,1^;t500^;M1,2^#MAP,PWM,...`.

3. Sequence values really do carry bare `^`. `saveStoredCommandsToPreferences` (WCB_Storage.cpp:625-663) splits at the first comma and does `preferences.putString(key, value)` verbatim — no escaping, no normalisation. Multi-step values are the documented form (WCB_Help.cpp:766-767, `?SEQ,SAVE,wave,CMD1^CMD2^CMD3`). Such a value can be created on the custom-funcChar source board because there the split-time matcher DOES match (`#SEQ,SAVE,` vs LFI `#`).

4. The split happens once, up front, with the receiving board's LFI. `parseCommandsAndEnqueue` (WCB.ino:1945-2117) tests `restUpper.startsWith(String(LocalFunctionIdentifier) + "SEQ,SAVE,")` at :2053 and computes `nextFuncBoundary = String(commandDelimiter) + String(LocalFunctionIdentifier)` at :2057. Every token is `enqueueCommand`'d (:1911-1942) into `commandQueue` (created :7191), which is drained only later in loop() at :7501-7519 via `handleSingleCommand`. So nothing in the chain executes during the split — LFI is stable at `'?'` for the whole walk. No race: enqueue and drain are both in the loop() task.

5. Failure confirmed step by step for the described paste onto a fresh board (LFI `'?'`, delim `'^'`):
   - The trailing `?CHK…` is literal `"?CHK"` in the factory chain (:5776), so the verifier at :1953 finds it, CRC passes, and it recurses on the stripped chain (:1989). No protection here — the restore is reported "✓ Command checksum VERIFIED".
   - At token `#SEQ,SAVE,wave,;M1,1^;t500^;M1,2`: `startsWith("?CS")` false (:2015), `startsWith("?SEQ,SAVE,")` false (:2053), `startsWith("?MGMT,")` false (:2084) → falls to the generic branch (:2096-2115) and splits on every `^`.
   - Dispatch order is FIFO, and `?FUNCCHAR,#` was enqueued far earlier, so by the time the fragment dispatches `LocalFunctionIdentifier == '#'`. `handleSingleCommand:4380` matches `'#'` → `processLocalCommand("SEQ,SAVE,wave,;M1,1")` → :4955/:4994 → `saveStoredCommandsToPreferences` stores `wave = ";M1,1"`. Sequence silently truncated to its first step.
   - `;t500` and `;M1,2` then dispatch as ordinary commands (`handleSingleCommand:4384` → `processCommandCharcter`), i.e. a real Maestro command is executed and fanned out to the mesh mid-restore (`lastReceivedViaESPNOW` is false for a serial paste). Nothing reports an anomaly.

REACHABILITY — real. `?FUNCCHAR,x` is a supported, NVS-persisted setting (handler WCB.ino:5044-5053, help WCB_Help.cpp:886-899, :1075) and the Wizard round-trips it (`g-funcchar`, Wizard/app.js:1177, 1419, 1464, 7443-7448). The "For Factory Reset/Fresh Boards" chain (:5785-5787) exists specifically for pasting onto a replacement board, which is exactly the LFI-mismatch case. Only the factory chain is affected — the live chain uses `lfi` throughout (:5745-5747) and matches the receiving board when that board already has the same funcChar.

INTENT — not a sanctioned trade-off. The comment at :5739-5743 explains why the SEPARATOR is not flipped ("the restore is split ONCE up front using the receiving board's current delimiter") and why the FUNC char IS flipped ("governs DISPATCH at restore time"). The author reasoned about split-time vs dispatch-time for the delimiter but not for the func char; the interaction with the :2053 split-time matcher is unaddressed anywhere. The flip dates to commit 4075057 (2026-06-25, "Bug Fix"), so this is long-standing, not a new regression on this branch. Also independent of the already-adjudicated `<funcChar>CHK` gap (docs/CODE_REVIEW_2026-08_verdicts.md:1989-2007) — different function, and the factory chain's CHK is correctly literal.

SEVERITY — I keep S2. It silently corrupts persisted user data on the documented board-replacement workflow while printing a success message, and additionally causes unintended physical actuation (a Maestro command fired and mesh-fanned during a config restore). The prior S3 calibration for a custom-FUNCCHAR-gated finding (verdicts:1993) applied to something that only cost a safety net and required a second condition (a corrupted paste); this one needs only the non-default funcChar and causes actual loss plus side effects.

FIX — wrong, in both variants. See the correction field.

</details>

### `Code/WCB/WCB.ino:2298` — CONFIRMED · S2 · ⚠ PROPOSED FIX IS WRONG

**ETM checksum mode silently destroys the CRC for commands over 187 chars — target ACKs, then discards**

**Correction to the finding as filed:**

Three corrections to the finding as written.

1. Wrong citation for the array: the ETM `structCommand[200]` is at **WCB.ino:209**, not `:781` (`:781` is the unrelated non-ETM `espnow_struct_message`). Also add the retry-path twin at **WCB.ino:1310-1316** and the pending-table field `PendingMessage.command[200]` at **WCB.ino:512**.

2. The failure-scenario arithmetic is wrong and picks a length that does not trigger the bug. `?SEQ,SAVE,dome_open,` is 20 chars, so `+ <180 chars>` = **200**, not 190 — and 200 > 198 fails the `:2597` test, so it takes the *other* branch entirely. The correct scenario is a sequence value of **167–178 chars** (total cmd 187–198, marked payload 188–199).

3. Understated reachability: the finding frames this as requiring an operator to have run `?ETM,CHKSM,ON`. CHKSM is **on by default** (`WCB.ino:271`, `WCB_Storage.cpp:2233`), so the default fleet is affected. Add the second vector the finding misses: serial-monitor mapping passthrough at `WCB.ino:6274-6276` silently drops any mapped serial line ≥ 185 chars over ETM.

Correct fix shape: put the guard at the top of the ETM branch in `sendESPNowMessage` (before `etmAddToPendingTable` at :2313, so no poisoned pending entry is created), define `ETM_MAX_CMD_CHKSM = 187`, and log-and-drop. At `:2597`, **reject with a log** rather than falling through (see fix_is_correct). Hoist the CRC check above the dedup block at `:3826` and leave `etmSendAck` between them, so duplicates keep ACKing. Fix `CLAUDE.md:46` 188 → 187 in the same commit.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived from source, not the summary.

- ETM wire struct: `char structCommand[200]` at **WCB.ino:209** (inside `espnow_struct_message_etm`, WCB.ino:204-212). The reviewer cited `WCB.ino:781`, which is the *normal* `espnow_struct_message` (WCB.ino:777-783). Same dimension, wrong citation.
- Send path WCB.ino:2294-2302: `char withCRC[210]; snprintf(withCRC, sizeof(withCRC), "%s|CRC%08X", message, crc); strncpy(etmMsg.structCommand, withCRC, sizeof(etmMsg.structCommand) - 1);` then `etmMsg.structCommand[199] = '\0'` at :2302. Max stored string = 199 chars. Suffix `|CRC` (4) + `%08X` (exactly 8) = 12. Full CRC survives iff L+12 <= 199, i.e. **L <= 187**. Arithmetic re-checked: `|CRC` itself is fully present iff L+4 <= 199 → L <= 195, so 188..195 = truncated hex (at 195, zero hex digits → `strtoul("")` = 0), 196..199 = marker itself cut. The reviewer's ranges are correct.
- Receive path: `lastIndexOf("|CRC")` at :3856, `strtoul` at :3864, `rxCRC != calcCRC` reject at :3866-3872 — both rejections `return` with output only under `debugETM`.
- **ACK-before-CRC is real.** `etmSendAck(senderWCB, ...)` fires at **WCB.ino:3822**, inside the `PACKET_TYPE_COMMAND` branch that starts at :3805. The dedup block is :3826-3850 and the CRC check is :3855-3873 — the ACK is unconditionally ahead of both. Sender clears its pending entry, `espnowCommandDelivered++` (:2320), no retry.

REACHABILITY — the "defaults" make this much worse than an opt-in.

- `bool etmEnabled = true;` (WCB.ino:264) and `bool etmChecksumEnabled = true;` (WCB.ino:271), with the NVS default also `true` (`WCB_Storage.cpp:2233`). **CHKSM is ON by default** — this is not an opt-in mode.
- No length guard exists. The only length check on this path is `payload.length() <= 198` at WCB.ino:2597 (`// 198 = 200 - 1 (marker) - 1 (null)`) — sized for the no-CRC case only. `handleMgmtForward` then does `sendESPNowMessage(targetWCB, markedPayload.c_str(), true)` at :2599 with markedPayload up to **199** chars.
- Nothing truncates upstream: `enqueueCommand` mallocs (`WCB.ino:1926-1933`), and `?MGMT,` is passed whole and unsplit (`WCB.ino:2081-2093`).
- Two independent real-user vectors:
  1. **Wizard single-chunk MGMT.** `updateSequence` builds `?SEQ,SAVE,${key},${value}` and sends `MGMT,FRAG,...,0,1,${cmd}` (`Wizard/app.js:4455`) via `sendMgmtReliable` (`app.js:7682-7688`, fire-and-forget, no ACK wait). `value` comes from `.seq-val-textarea` with no maxlength and no length validation (`app.js:4407-4415`). A `cmd` of 187–198 chars silently vanishes; the Wizard then shows a success toast (`app.js:4458`) **and patches `boardConfigs`/`boardBaselines`** (`app.js:4468-4474`), so the delta push never retries. Note the config-*push* path is safe by luck: it fragments at `MGMT_CHUNK_SIZE = 180` (`app.js:67`, used at `:7852`), keeping marked payloads at 181 ≤ 199-12.
  2. **Serial-monitor mapping passthrough.** `String espnowCmd = ";S" + String(destPort) + cmd; sendESPNowMessage(destWCB, espnowCmd.c_str());` at WCB.ino:6274-6276 — `useETM` defaults true (WCB.ino:911). Any mapped serial line ≥ 185 chars is silently dropped at the far end.
- Retry path has the identical defect: WCB.ino:1310-1316 rebuilds the same `withCRC[210]` from `entry.command`, and `PendingMessage.command[200]` (WCB.ino:512) is itself `strncpy`-truncated at :1173.

INTENT — not deliberate. `git log -L 2293,2303` shows the block was added whole by commit `5e2f0d5 "Added Checksums to ETM option"` with no guard and no comment. The surrounding comments (:3812-3821 on the ACK exception, :2593-2596 on the 198 limit) discuss other concerns entirely. `CLAUDE.md:45-46` records the constraint ("CHKSM adds 12 bytes and drops the max command to 188 chars") but nothing enforces it — and it is off by one (187 is the last safe length; 188+12 = 200 > 199).

The defect stands: on a default-configured mesh, a command in a ~12-char-wide length window is silently discarded by the receiver while the sender is told it was delivered. S2 is right — silent data loss that actively reports success, on the default config, via ordinary Wizard use. Not S1 (no crash, bounded length window, no fleet-wide bricking); not S3 (the false-success reporting defeats every retry mechanism above it).

</details>

### `Code/WCB/WCB.ino:3706` — CONFIRMED · S2

**?ETM,CHAR phase 3 never loads the network: the ETMLOAD trigger is sent non-ETM and is dropped by the ETM-mismatch filter on every peer**

**Correction to the finding as filed:**

Mechanism, line numbers and severity are all correct as filed. Three refinements:

1. It is a DUPLICATE — same defect as F-010 (docs/CODE_REVIEW_2026-08.md:766) and F-312 (wave1:1436-1450), already verified in docs/CODE_REVIEW_2026-08_verdicts.md:~440-466. Merge rather than filing again.

2. Of the two proposed variants, prefer the whitelist. Variant A (send with `useETM=true`) works only if the ETMLOAD arm is also added — the reviewer did say that — but it also costs a pending-table slot with `expectAckFrom[]` set for every active peer (:1178-1185 via :2311), plus an ACK from each peer, fired at the exact moment phase 3 starts measuring latency. That self-perturbs the measurement, which is what the original `// broadcast, not ETM tracked` comment was avoiding. Variant B is cleaner; whitelist the load payloads too, not just the trigger:
     bool isEtmCharPacket = (strncmp(peek.structCommand, "ETMLOAD", 7) == 0 ||
                             strncmp(peek.structCommand, "LOAD_",   5) == 0);
   and add `!isEtmCharPacket` to the `if` at :3706. Whitelisting "ETMCHAR_" (as the finding suggests) is a no-op — every ETMCHAR_ send passes `true` (:1473, :1509, :1515), so those never reach this guard. `LOAD_` matters for a different reason: processETMLoad's payloads (:1614, :1629) are also non-ETM and currently make every peer print the misleading "[ETM] Dropped non-ETM packet — sender has ETM OFF. Enable ETM on all boards." (:3708) once per load packet whenever `debugEnabled` is on. `debugETM` is suppressed during CHAR (:1420-1422) but `debugEnabled` is not.

3. Neither variant fully restores "all boards transmitting". A peer arms `etmLoadRunning` at :4176 immediately after `lastReceivedViaESPNOW = true` at :4159, and the handler `return`s at :4181 without enqueueing anything — so on an idle peer nothing resets that flag (only :6404 on serial input, or :7510 per drained queue item). For the whole 10 s window the broadcast third of the load (:1614, `target == 0`) is silently dropped at the sender by :2275; only the unicast two thirds (:1629) go out. Fixing the trigger without also save/clearing/restoring `lastReceivedViaESPNOW` around processETMLoad's sends leaves phase 3 measuring roughly two thirds of the intended load. This is a pre-existing separate defect, not one the proposed fix introduces.

<details><summary>Full verification reasoning</summary>

I re-derived every load-bearing fact from source; the finding holds exactly as written.

SEND SIDE. `Code/WCB/WCB.ino:1493-1496` — phase-3 entry does `sendESPNowMessage(0, "ETMLOAD", false);` at :1495. In `sendESPNowMessage` (:2273) the ETM branch is gated `if (etmEnabled && useETM && !isPWMMessage)` (:2280), so `useETM=false` skips it and falls into the NORMAL SEND PATH (:2328+), emitting an `espnow_struct_message`, not `espnow_struct_message_etm`. The send itself is not suppressed: the only early return is `if (target == 0 && lastReceivedViaESPNOW) return;` (:2275), and on the initiator the incoming ETM ACKs return at :3797-3803 and heartbeats at :3790-3795 without ever setting that flag, so a serial-initiated `?ETM,CHAR` (flag cleared at :6404) does transmit.

RECEIVE SIDE. `espNowReceiveCallback` classifies by length at :3676-3681 (`isETMPacket` only when `len == sizeof(espnow_struct_message_etm)`), then the mismatch guard at :3688-3712. I evaluated all four predicates against the literal payload myself:
- `peek.structTargetID` = "0" → `atoi` = 0. `WCB_TARGET_RAW_SERIAL` = 97 and `WCB_TARGET_KYBER` = 98 (`WCB_Storage.h:14-15`, mirrored `WCB.ino:122-123`); `structTargetID[0]` is '0', not 'K' → `isRawPacket` false (:3692).
- `structCommand[0]` == 'E' → not ';' → `isPWMPacket` false (:3693-3694); not '{' → `isRcJsonPacket` false (:3704).
- `structCommand[1]` == 'T' → not 'M'/'m' → `isMaestroPacket` false (:3697).
All four false → `return` at :3709. Every ETM-enabled peer drops it.

ONLY WRITER. `grep -n etmLoadRunning Code/WCB/*` returns exactly one `= true`: :4176, sitting inside the NORMAL RECEIVE PATH (block opens :4006 with the password check at :4010; `lastReceivedViaESPNOW = true` at :4159 immediately precedes it). That path is reachable only by a non-ETM-sized packet, which the guard already discarded when `etmEnabled`. The ETM path at :3893 (`if (!etmCmd.startsWith("ETMCHAR_") && !etmCmd.startsWith("ETMLOAD"))`) excludes ETMLOAD, and I read to the close of that block (:3996-4003) — there is no `else` arm, so even an ETM-wrapped ETMLOAD would be ACKed (:3822) and discarded. `processETMLoad()` (:1585) is driven from loop() but its first line `if (!etmLoadRunning) return;` (:1586) never passes on any peer.

REACHABILITY — the broken state is the ONLY state. `startETMChar()` hard-requires `etmEnabled` (:1405-1408, "ETM is not enabled. Use ?ETMON first."), and `processETMChar()` counts only peers with `wcbPeerActive[] && boardTable[].online` (:1446-1449), where `online` is set in the ETM receive path. So any peer that can be a phase-3 target is by construction ETM-on — precisely the condition under which the guard eats ETMLOAD. There is no fleet configuration where the trigger lands. Consistent with CLAUDE.md rule 4 (ETM is all-or-nothing).

INTENT — not deliberate. `git log -L 1490,1500:Code/WCB/WCB.ino` shows the line born in cc6c078 ("ETM") commented `// broadcast, not ETM tracked` — the author's intent was "don't burn a pending-ACK slot", written without awareness that the mismatch guard would eat it; 1ea6312 only stripped the comment. The `"ETMLOAD"` clause at :3893 shows the author expected it on the ETM path too but never wrote the handler. Nothing in docs/ frames this as a trade-off.

CONSEQUENCE (why it is bottom-of-band S2). `buildETMCharResultsString` (:1791) labels phase 3 "Loaded Network (all boards transmitting)" (:1798) when only the initiator transmits. "Recommended ETM timeout" (:1824-1826) is `worstAvgLatency*5` over ALL phases, floored at 150 — so the miss matters only when phase 3 would have been worst, and the value is printed with "Apply with: ?ETM,TIMEOUT,n", never auto-applied. Deterministic wrong output from a diagnostic tool; no wire or runtime effect. Repo legend (docs/CODE_REVIEW_2026-08.md:9-10): S2 = "wrong behaviour in a real scenario"; S3 = "latent race, bound, leak, overflow" — S3 does not fit, so S2 stands, but triage it below any S2 that touches the wire.

DUPLICATE. Already recorded as F-010 (docs/CODE_REVIEW_2026-08.md:766, marked "partly checked — to finish: read the ETM gate") and as F-312 in docs/CODE_REVIEW_2026-08_wave1.md:1436-1450, and already adversarially verified at docs/CODE_REVIEW_2026-08_verdicts.md:~440-466. My trace was done independently and agrees with both.

</details>

### `Code/WCB/WCB.ino:4130` — CONFIRMED · S2

**Serial3/4/5 (ESPSoftwareSerial) are written from the WiFi callback and three other tasks with no mutual exclusion; the adjacent "write() drains async" comment is false for these ports**

**Correction to the finding as filed:**

Six corrections, none of which overturn the finding:

1. The 177-byte figure is the receiver's bound, not a reachable size from a WCB peer. `sendESPNowRawSerial` is called only from RawSerialForwardingTask, which uses `static uint8_t buffer[64];` (WCB.ino:6637), so a WCB-originated raw chunk is <=64 bytes → ~67 ms at 9600, not 184 ms. (177 is reachable only from a hand-crafted or WCB_Client sender; note also the sender chunks at 180 (WCB.ino:2496) while the receiver rejects >177 — a separate latent mismatch.) The targeted-Kyber figure at :4065 is genuinely up to 196 bytes / ~204 ms.

2. Fix (a) understates the flush() error. `UARTBase::flush()` (SoftwareSerial.cpp:436-444) does `if (!m_rxValid) return; m_buffer->flush();` — it flushes the **RX** buffer and never waited on TX. So the "flushing here blocks the WiFi task ... (~200ms on a SW UART)" rationale at WCB.ino:4066-4069 was never true for S3-S5, and the pre-dd986df `targetSerial.flush()` was actively *discarding received bytes* on those ports. The removal was correct for the wrong reason; the corrected comment should say so.

3. The same false comment exists a third time, in RawSerialForwardingTask itself: WCB.ino:6681 "No flush() — write() queues; the UART drains async." That one blocks the forwarding sweep, starving the RX side of every other mapping. Fix (a) should cover :6681 as well as :4066-4070 and :4131-4132.

4. Fix (b) as written only moves the raw path (:4130) off the WiFi task. The identical problem exists at :4065 (targeted Kyber, up to 196 B), :4097, :4101 and :4107 (Kyber broadcast to every local Maestro port). Deferring only :4130 leaves the worst case in place.

5. Fix (c) is insufficient on its own and, taken alone, is a regression: a mutex acquired inside espNowReceiveCallback would block the WiFi task for the full frame time of whatever core-1 task holds it (up to ~200 ms), converting corruption into exactly the stall the comment claims to avoid. It is only sound paired with (b) — which is how the reviewer presents it, so the combined fix is fine. (Taking a FreeRTOS mutex there is legal — it is task context, not an ISR.)

6. Unmentioned adjacent hole of the same class: `addSerialMonitorMapping` refuses a PWM-output port as a mapping *input* (WCB_Storage.cpp:1629-1632), but the receiver-side raw path at WCB.ino:4123 has no `isSerialPortPWMOutput()` check, so a remote raw chunk can bit-bang a TX pin that PWMTask is driving (WCB_PWM.cpp:283-289) on core 0 vs core 0 — worth folding into the same fix.

Also note this overlaps almost entirely with the sibling finding at docs/CODE_REVIEW_2026-08_wave1.md:1862 and partially with the one at :897; they should be merged rather than fixed three times.

<details><summary>Full verification reasoning</summary>

I re-derived every load-bearing fact from source rather than the summary.

MECHANISM — verified.
1. `getSerialStream()` (Code/WCB/WCB.ino:1869-1878) returns `Serial3/4/5` for ports 3-5, and those are `SoftwareSerial Serial3(SERIAL3_RX_PIN, SERIAL3_TX_PIN);` etc. at WCB.ino:885-887, from `#include <SoftwareSerial.h>  // ESPSoftwareSerial ... V8.1.0` (WCB.ino:76). The library on this machine is Arduino-Code/libraries/EspSoftwareSerial, `version=8.1.0` in library.properties. Both build targets (esp32 and esp32s3) have only 3 hardware UARTs, so S3-S5 are always software on both.
2. `size_t IRAM_ATTR UARTBase::write(const uint8_t* buffer, size_t size, Parity parity)` at EspSoftwareSerial/src/SoftwareSerial.cpp:348 is a `for (cnt = 0; cnt < size; ++cnt)` loop that bit-bangs each byte through `writePeriod()` (SoftwareSerial.cpp:294), which spins in `preciseDelay()` (SoftwareSerial.cpp:285: `do { ticks = microsToTicks(micros()); } while ((ticks - m_periodStart) < m_periodDuration);`). There is no TX ring buffer and no ISR drain. So "write() drains async" at WCB.ino:4131-4132 is factually false for ports 3-5. It IS true for S1/S2 (HardwareSerial), so the comment is right for 1-2 and wrong for 3-5, exactly as claimed.
3. Baud: `#define SERIAL3_DEFAULT_BAUD_RATE 9600` is at WCB.ino:624 — the reviewer's citation is exact. 177*10/9600 = 184 ms; 196*10/9600 = 204 ms. Arithmetic checks out.
4. Guards at the write site: WCB.ino:4123 rejects only `chunkLen > 177 || chunkLen == 0 || targetPort < 1 || targetPort > 5`. Nothing excludes S3-S5, and nothing excludes a PWM-output port.
5. No mutual exclusion exists: grepping `SemaphoreHandle|xSemaphoreTake|portMUX|taskENTER_CRITICAL|[Mm]utex` across Code/WCB/*.{ino,cpp,h} yields exactly one hit — `static portMUX_TYPE etmAckQMux` (WCB.ino:1113), for the ETM ACK queue, not serial. Confirmed: zero arbitration on the SoftwareSerial objects.
6. `m_intTxEnabled = true` by default (SoftwareSerial.cpp:92) and WCB never calls `enableIntTx()` (grep: no hits), so interrupts stay on during the bit-bang and FreeRTOS preemption mid-frame is possible even single-core.

CONTEXT / CONCURRENCY — verified, and stronger than claimed.
- `esp_now_register_recv_cb(espNowReceiveCallback)` at WCB.ino:7434; the callback's own comments assert WiFi-task context (WCB.ino:4066) and it deliberately defers heavy work (`enqueueMgmtReq` at :3637/:3644, `drainOtaPackets`) precisely because the WiFi task must not block — so blocking it for tens/hundreds of ms directly contradicts the file's own stated design rule.
- The other writers and their contexts: loopTask drains `commandQueue` at WCB.ino:7503 → `processBroadcastCommand` writes S1-S5 at :6343; `RawSerialForwardingTask` (created WCB.ino:7464, core 1 prio 1) writes outputs at :6680; `KyberLocalTask` (:7443) → `forwardDataFromKyber` writes at :4269/:4285; `KyberRemoteTask` (:7447) → :4338; `PWMTask` (:7459, core 0 prio 2) `digitalWrite`s the very same SERIAL3/4/5_TX_PIN (WCB_PWM.cpp:283-289, :607-613, :710-717, :752-759). So "the WiFi callback and three other tasks" is if anything conservative. loopTask / RawSerialForwardingTask / KyberLocalTask are all prio 1 on core 1 with time slicing, so two of THOSE can also interleave; the WiFi task (higher prio, core 0) both preempts and runs genuinely in parallel.
- The codebase already enforces single-owner discipline for READING a port (serialCommandTask skips raw-mapped ports, WCB.ino:6604-6607; RawSerialForwardingTask skips kyberLocalPort, :6654) — proving the authors reason about port ownership, but only on the RX side. Nothing equivalent exists for TX.

REACHABILITY — confirmed by the project's own user documentation.
`sendESPNowRawSerial` (WCB.ino:2492) is called only from RawSerialForwardingTask (:6700) and stamps `WCB_TARGET_RAW_SERIAL` (=97, WCB.ino:122). At the receiver, WCB.ino:3692 explicitly whitelists raw packets past the ETM-mismatch filter, so the path is live with ETM on or off. The wiki's canonical example for a raw mapping is `?SMS3R,W1S5` (Wireless_Communication_Board-WCB.wiki/Serial-Communication.md:1276) — i.e. the documented, advertised usage puts raw ESP-NOW chunks onto a remote board's **S5**, a SoftwareSerial port, written from the WiFi task at WCB.ino:4130. A user cannot avoid this by configuration; it is the recommended configuration.

INTENT — not a deliberate trade-off. `git log -L 4129,4133` shows commit dd986df ("bug fixes", 2026-06-29) replaced `targetSerial.flush()` with the comment. The author's mental model there is a hardware UART with a TX FIFO; nothing in docs/ or CLAUDE.md acknowledges SoftwareSerial's synchronous TX. So the comment is a mistaken rationale, not a recorded constraint.

The observation stands on both halves: (i) the WiFi task busy-waits for the whole transmission on S3-S5, and (ii) nothing prevents two contexts bit-banging the same TX pin. Severity S2 is right under this review's own scale (docs/CODE_REVIEW_2026-08.md:10 — "S1 crashes / corrupts / bricks · S2 wrong behaviour in a real scenario"). Not S1: no memory corruption, crash or brick.

</details>

### `Code/WCB/WCB.ino:4159` — CONFIRMED · S2 · ⚠ PROPOSED FIX IS WRONG

**The WiFi task rewrites lastReceivedViaESPNOW mid-dispatch, so loop()'s per-item snapshot restore does not survive to the point it is read**

**Correction to the finding as filed:**

Corrections to the finding as written:

(a) Line-number slip: the non-ETM JSON branch returns at WCB.ino:4209 (enqueue at :4203), not :4204. The ETM JSON twin returns at :3935.

(b) The reviewer framed the JSON no-restore as the core defect. It is not — every normal dispatch path re-establishes the flag (:6404 serial, :7510 queue drain, :3883 ETM, :2725 MGMT), so a latched true is harmless on its own. The actual defect is the concurrent core-0 write landing inside the core-1 dispatch window, which a plain received COMMAND causes just as well as an rc_hb/rc_ch frame. The JSON path only raises the frequency. The one place the latch alone bites (deterministically, no race needed) is loop-context broadcasts with no preceding restore: WCB.ino:1495, :1509, :1614 — `?ETM,LOAD` and `?ETM,CHAR` broadcast phases are silently dropped after any received mesh command.

(c) The aggravator worth recording is the ordering in `processBroadcastCommand`: the mesh send at WCB.ino:6356 is LAST, after up to ~30 ms of blocking bit-banged writes to S3-S5 (`writeSerialString` :1862, EspSoftwareSerial at 9600 default :626, all ports broadcast-enabled :629). That is what turns a theoretical race into a routinely-hit one.

(d) The blast radius is larger than the one call chain the fix threads. Other readers of the same global inside the same dispatch window: WCB_Maestro.cpp:143, :175, :224, :233, :316, :338, :367, :428; WCB_WLED.cpp:151; `routeStoredOrCap` WCB.ino:5829; `recallStoredCommand` WCB.ino:6119. Plus the enqueue-side variant (:6404 → snapshot at :1920).

Recommended fix instead: make the global loop-task-private and stop the WiFi task writing it at all. I verified the callback only ever WRITES it (grep over :3584-:4230 shows :3883 and :4159 and nothing else; :2725 via `handleMgmtPacket` called at :3628) and never reads it, so nothing in callback context depends on the shared value. Give `enqueueCommand` / `parseCommandsAndEnqueue` / `enqueuePendingTimerChain` an explicit `espnowOrigin` argument (defaulted to the global for the ~40 existing call sites) and have the three callback writers pass their origin instead of assigning it. That closes both the dispatch-side and enqueue-side races for every reader at once, needs no change to `sendESPNowMessage`'s signature, and matches the pattern the file already uses everywhere else (etmEnqueueAck, enqueueWdpPacket, enqueueOtaPacket, enqueueMgmtReq, enqueueRcJsonRelay).

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived from source, not the summary.

1. The variable is a plain (non-volatile, non-atomic) global: `bool lastReceivedViaESPNOW = false;` at Code/WCB/WCB.ino:165.

2. Writers, and the task each runs on:
   - WCB.ino:3883 `lastReceivedViaESPNOW = !wizardOrigin;` — inside `espNowReceiveCallback` (starts WCB.ino:3584, ends :4230), ETM COMMAND branch.
   - WCB.ino:4159 `lastReceivedViaESPNOW = true;` — same callback, non-ETM "Normal command for this board" branch. Note it is set BEFORE the target check at :4163, so even a packet not addressed to this board latches it.
   - WCB.ino:2725 `lastReceivedViaESPNOW = unicastOrigin;` in `handleMgmtPacket`, which is called inline from the callback at WCB.ino:3628.
   - WCB.ino:6404 `lastReceivedViaESPNOW = false;` in `processIncomingSerial`, driven by `serialCommandTask` (WCB.ino:6595, created pinned to core 1 at :7439).
   - WCB.ino:7510 `lastReceivedViaESPNOW = inItem.espnowOrigin;` in `loop()`.
   The callback is registered directly with `esp_now_register_recv_cb(espNowReceiveCallback)` (WCB.ino:7434) — no deferral — so it executes in WiFi-task context. The repo itself states the core split: WCB.ino:293-296 "volatile: written on Core 1 (processWCBMessage in serialCommandTask) and read on Core 0 (rcJsonRelaySubscribed() in the ESP-NOW receive callback)". So loop()/serialCommandTask are core 1 and the receive callback is core 0 — genuinely parallel, not merely interleaved.

3. The reader that matters is the loop-prevention gate: WCB.ino:2275 `if (target == 0 && lastReceivedViaESPNOW) return;` at the very top of `sendESPNowMessage`, i.e. before the ETM branch, so it gates every broadcast regardless of `useETM`.

4. The exposure window is wide BY CONSTRUCTION. `handleSingleCommand` (WCB.ino:4354, sole caller loop():7512) falls through to `processBroadcastCommand` (:4389 → :6234). That function writes the text to S1..S5 FIRST (loop at :6295-6316, `writeSerialString`) and only then calls `sendESPNowMessage(0, cmd.c_str())` at WCB.ino:6356. `writeSerialString` (WCB.ino:1862-1867) writes byte-by-byte; S3/S4/S5 are bit-banged EspSoftwareSerial (`Serial3.begin(baud, SWSERIAL_8N1, ...)` WCB.ino:1893/1899/1905) whose write blocks the calling task, and they default to 9600 baud (SERIAL5_DEFAULT_BAUD_RATE 9600 at :626) with broadcast enabled on all five ports by default (`serialBroadcastEnabled[5] = {true,...}` :629). A ~10-char command out three bit-banged ports is ~30 ms of blocking core-1 time between the restore at :7510 and the read at :2275. The WiFi task on core 0 can set the flag true anywhere in that window, and `sendESPNowMessage` then silently returns — no log (the debug print at :6357 is itself gated on `!lastReceivedViaESPNOW`).

5. REACHABILITY of the concurrent write. Heartbeats (:3792 area), WDP adverts, ACKs and the mgmt/OTA/config/rterm packet classes all return before the writes, so they do not latch. But the JSON-telemetry claim checks out: WCB.ino:4196-4209 — the `{`-prefixed branch runs AFTER :4159 and returns at :4209 with no restore (the reviewer wrote :4204; the enqueue is :4203, the `return;` is :4209 — a cosmetic transcription slip). Same shape in the ETM path: :3883 sets it, the JSON branch returns at :3935. Non-ETM RC JSON is explicitly whitelisted through the ETM-mismatch filter (`isRcJsonPacket` WCB.ino:3745, allowed at :3746-3751), so it reaches :4159 even on an ETM-enabled board. Rates confirmed cross-repo: NaviCore/rc_telemetry.h:30-31 "rc_hb 0.5 Hz / rc_ch 5 Hz", NaviCore/rc_config.h:631 chRateHz default 5, range 1-20. So a controller on the mesh drives WCB.ino:4159 every 200 ms (up to every 50 ms), and any peer WCB broadcast does the same. At a ~30 ms window and 5 Hz that is a double-digit percentage of USB/serial-originated broadcasts silently not reaching the mesh.

6. The queue snapshot does NOT cover this. WCB.ino:1920 snapshots at enqueue, :7510 restores at drain; the comment at :893-897 says explicitly it fixes the value "when the queue happened to be drained (the global races across queue boundaries)". It says nothing about a concurrent core-0 write during the dispatch itself, and there is no such protection. The same hole exists on the enqueue side: :6404 sets false on serialCommandTask, then parsing runs before :1920 takes the snapshot — core 0 can set true in between, so the item can even be enqueued with the wrong origin.

7. INTENT check. Nothing documents this as accepted. The opposite: the file is full of the "never mutate X from the WiFi task, defer to loop()" pattern — `etmEnqueueAck` (:3939 area, "Defer to loop() — never mutate the pending table from the WiFi task"), `enqueueWdpPacket`, `enqueueOtaPacket`, `enqueueMgmtReq` (:3627-3670), `enqueueRcJsonRelay` ("we're on the WiFi task (Core 0) and Serial isn't atomic across cores"). `lastReceivedViaESPNOW` is the one piece of dispatch state that escaped that rule. WCB.ino:4356-4361 shows the author already rejected a global for IF-gating because "a global raced across sources, queue-drain boundaries, and timer groups". docs/ and CLAUDE.md contain no accepted-tradeoff note for this.

8. SECONDARY, non-racy consequence the reviewer missed: because the callback never restores, the global sits latched true after any received mesh command or RC frame. Loop-context broadcasts that are not preceded by a per-item restore therefore read a stale true deterministically — `sendESPNowMessage(0, "ETMLOAD", false)` (:1495), the phase-2/3 characterization broadcast (:1509) and the load-test broadcast (:1614) are silently suppressed whenever a controller is streaming or any peer command has been received, so `?ETM,CHAR` reports bogus broadcast rows. Diagnostic-only, but real and repeatable.

SEVERITY. Holding S2, with the caveat that it rests on the window width: with S3-S5 broadcast-enabled at 9600 (the shipped default) the window is tens of ms and the drop rate is material; on a board with S3-S5 disabled for broadcast the window collapses toward microseconds and this is closer to S3. The failure is silent, has no retry and no log, and hits the board's primary function (mesh command fan-out), which is what keeps it above S3 for me.

</details>

### `Code/WCB/WCB.ino:4269` — CONFIRMED · S2 · ⚠ PROPOSED FIX IS WRONG

**forwardDataFromKyber writes each Kyber byte once PER TARGET, so two Maestros sharing one serial port receive every byte twice**

**Correction to the finding as filed:**

The defect and both locations are right; the fix text is not. "keep a `uint8_t sentLocalPorts = 0;` bitmask **per byte (or better, per drain pass)**" — the parenthetical is backwards and would be a far worse bug than the one being fixed. A mask hoisted outside the `while (kyberSerial.available())` loop stays set after the first byte, so exactly ONE byte per drain pass would reach each port and every subsequent byte would be dropped — Kyber passthrough would break for ALL users, not just daisy-chain ones. The mask must be reset per byte, i.e. declared inside the while-loop body at Code/WCB/WCB.ino:4256.

Better still, and cheaper than rescanning 9 targets per byte: compute the local port set ONCE before the drain loop, then write each byte to each distinct port —

  uint8_t localPortMask = 0;                       // bit p → port p (1..5)
  bool anyRemote = false;
  for (int i = 0; i < MAX_KYBER_TARGETS; i++) {
    if (!kyberTargets[i].enabled) continue;
    if (kyberTargets[i].targetWCB == WCB_Number) {
      if (kyberTargets[i].targetPort >= 1 && kyberTargets[i].targetPort <= 5)
        localPortMask |= (uint8_t)(1u << kyberTargets[i].targetPort);
    } else anyRemote = true;
  }

then per byte: `for (int p = 1; p <= 5; p++) if (localPortMask & (1u << p)) getSerialStream(p).write(b);` and the existing single-queue remote accumulate guarded by `anyRemote`. Same shape as the dedup at Code/WCB/WCB_Maestro.cpp:105-116 but O(5) per byte instead of O(9) with a rescan, which matters on the 1 ms Kyber task. Note the mask must be rebuilt if targets change at runtime (`?KYBER,LOCAL,...` and the WDP reconcile at Code/WCB/WCB_WDP.cpp:717-723 both mutate `kyberTargets[]` from another task), so build it at the top of each `forwardDataFromKyber()` call, not statically.

Apply the same per-port dedup to the non-targeting branch at Code/WCB/WCB.ino:4283-4287 (dedup on `maestroConfigs[i].serialPort`) — the reviewer is right that it has the same hole.

One extra gap worth folding in while there: the manual target parser (Code/WCB/WCB_Storage.cpp:1237-1262) accepts a literally identical `M1:W1S1:115200` twice, so even a single Maestro can be doubled by a typo. A duplicate-`(targetWCB,targetPort)` warning there would make the config error visible instead of silent.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived from source, not the summary.

`forwardDataFromKyber()` is at Code/WCB/WCB.ino:4244. Per drained byte, the targeting branch loops all 9 slots (Code/WCB/WCB.ino:4263) and, for every enabled target whose `targetWCB == WCB_Number` (:4265), does `getSerialStream(kyberTargets[i].targetPort).write(b)` (:4268-4269). `getSerialStream()` (Code/WCB/WCB.ino:1869-1878) is a plain switch returning the SAME `Serial1..Serial5` object for the same port number, so two enabled targets with the same `targetPort` produce two `write(b)` calls on one UART — byte doubled on the wire. The remote branch is explicitly deduped with `addedToRemote` (:4262, :4274-4278); the local branch has no equivalent. Constants check out: `struct KyberTarget {maestroID, targetWCB, targetPort, enabled}` at Code/WCB/WCB_Storage.h:97-103, `MAX_KYBER_TARGETS 9` at :104, `MAX_MAESTROS_PER_WCB 9` at Code/WCB/WCB_Maestro.h:14. The reviewer's line cites are accurate.

The non-targeting branch has the identical defect: Code/WCB/WCB.ino:4283-4287 writes once per configured local Maestro slot, keyed on `maestroConfigs[i].serialPort`, with no per-port dedup.

REACHABILITY — can two enabled targets share one local port?

1. Two Maestro IDs on ONE local port is a legal, first-class config. Slot identity is `(maestroID, serialPort, remoteWCB)` — `findSlotByMaestroIDPortTarget()` at Code/WCB/WCB_Maestro.cpp:61-70, used by `configureMaestro()` at :627-629. Nothing rejects a second ID on an already-used port (the only "already configured" comment, WCB_Maestro.cpp:738, deliberately removes such a guard). The codebase names the daisy chain as the reason the id-0 path skips dedup: "multiple Maestros can be daisy-chained on one serial line" (Code/WCB/WCB_Maestro.cpp:103-104), echoed in CLAUDE.md rule 5.
2. Auto-populate makes one target per configured Maestro with `targetPort = maestroConfigs[i].serialPort` and `targetWCB = WCB_Number` for local ones — Code/WCB/WCB_Storage.cpp:1114-1136 (loop at :1120-1132). Two same-port Maestros ⇒ two same-port local targets.
3. The manual list parser (Code/WCB/WCB_Storage.cpp:1219-1262) validates only id 1-9, wcb 1..MAX_WCB_COUNT, port 1-5 and a baud whitelist — no uniqueness check on port or even on maestroID, so `?KYBER,LOCAL,S2,M1:W1S1:115200,M2:W1S1:115200` is accepted verbatim.
4. `loadKyberTargets()` (Code/WCB/WCB_Storage.cpp:2095-2112) restores whatever was saved with no sanitisation, and `reconcileKyberTargetsFromMaestroConfigs()` (:1043-1069) is add-only, so the state persists across reboots.
5. Live path: `KyberLocalTask` calls it every ~1 ms (Code/WCB/WCB.ino:6576-6585), gated only on `kyberLocalPort != 0` (:4245).

INTENT — not a documented trade-off. `git log -L 4258,4292:Code/WCB/WCB.ino` shows commit d7b2b7f ADDED the `addedToRemote` dedup precisely because the multi-target loop was queueing a byte more than once — and touched only the remote side; a32e9cb later added the 1-5 port guard and the buffer-flush fix, again leaving the local side. The comment at :4259-4261 explains only why the REMOTE side is queued once ("remote targets share one broadcast packet"); it says nothing about multiple local targets resolving to one port. No doc in docs/ addresses it (docs/WCB_KYBER_PASSTHROUGH_PARAMS.md covers the remote leg only). So: oversight, not intent.

WHAT NARROWS IT (the reviewer did not mention this): the Wizard cannot produce the triggering config. `refreshMaestroPortDropdown()` filters out any port held by a different Maestro row (Wizard/app.js:3284-3303, comment at :3296-3300), and the Kyber target list is copied wholesale from that same de-conflicted table (Wizard/app.js:3348-3366, emitted at Wizard/parser.js:1347-1350). So only a user configuring over the serial CLI — or restoring a backup built from such a board (the backup emitter at Code/WCB/WCB.ino:2838-2854 re-emits every enabled target verbatim) — hits it.

Impact when it does trigger is total and silent: every Pololu frame becomes AA AA 0C 0C ... and the Kyber passthrough is non-functional, with no error anywhere. Holding S2 ("wrong behaviour in a real scenario"): it is deterministic rather than latent (so not S3 by this review's legend), but the config is uncommon and Wizard-blocked, so it is at the bottom of S2, not a candidate for promotion.

</details>

### `Code/WCB/WCB.ino:5703` — CONFIRMED · S2

**updateESPNowPassword() strips 6 chars off a 5-char "EPASS" prefix — the legacy ?EPASS form silently drops the first password character**

**Correction to the finding as filed:**

The finding's mechanism, line and severity are right; three corrections/additions.

1. THIS IS A RE-RAISE OF F-273, and the existing record is wrong. docs/CODE_REVIEW_2026-08.md:127 says "F-273 — REFUTED. `?EPASS,password` is handled correctly by `WCB.ino:4750`", and :303-307 says "REFUTED for the documented form; downgraded S2 → S4 … reached only by an *undocumented* no-comma spelling". The load-bearing word "undocumented" is factually wrong: WCB_Help.cpp:352-356 prints `?EPASSpassword   (e.g. ?EPASSmy_password)` under "Legacy commands:" in the `?EPASS?` help page, and WCB_Help.h:182 lists it in the LEGACY COMMANDS block. Fixing the code without correcting that doc row leaves a wrong refutation on record that will get this re-raised a third time.

2. PROVENANCE, worth a line in the fix commit: e4f26de ("fixed some bugs", 2026-04-07) changed `substring(5)` → `substring(6)` at a time when the comma-form handler already existed, i.e. someone "fixed" the strip for `"EPASS,"` on a path that can never carry a comma. That is the trap to record.

3. IMPACT is narrower than the finding implies. Not reachable from the Wizard (Wizard/parser.js:1291) or from backup/restore (WCB.ino:2796) — both emit the comma form. The wrong password is echoed immediately at WCB.ino:5706 and the board is recoverable over local USB serial, so this is a self-announcing, locally-fixable misconfiguration rather than a silent fleet outage. Still S2 by this review's legend, but at the low end — it belongs with the F-272/F-032 "retire or fix the dead legacy commands" cleanup, not with the blockers.

Preferred fix: `String newPassword = message.substring(5);` alone. Drop the leading-comma strip (see fix_is_correct note), and either retire the legacy form or leave WCB_Help.cpp:356 as-is now that it works.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived by hand, not from the summary.

`updateESPNowPassword()` is at Code/WCB/WCB.ino:5702-5710 and line 5703 really is `String newPassword = message.substring(6);`. "EPASS" is 5 characters (E=0, P=1, A=2, S=3, S=4), so for `message == "EPASSmy_password"` index 5 is 'm' and `substring(6)` yields `"y_password"`. Arduino `String::substring(begin)` returns begin..end, so the first password character is discarded. The off-by-one is real.

The codebase's own convention confirms it is a bug, not a style: every other legacy prefix-stripper uses the exact prefix length — WCB.ino:5694 `substring(3)` for "WCB", :5461 `substring(4)` for "WCBQ", :5713 `substring(2)` for "HW", :5428/:5445 `substring(2,4)` for "M2"/"M3". Only EPASS is prefix-length+1.

REACHABILITY — traced end to end.

`updateESPNowPassword` has exactly one caller: WCB.ino:5215, inside the "LEGACY FORMAT COMMANDS" else-if chain of `processLocalCommand` (function spans :4394-5349; legacy section starts at the banner on :5145). `processLocalCommand` itself has exactly one caller, `handleSingleCommand` at WCB.ino:4381, which passes `cmd.substring(1)` — so the leading funcChar is already stripped and `message` is `"EPASSmy_password"`. `LocalFunctionIdentifier` is a single `char` (WCB.ino:155), so the substring(1) strip is exact.

The reviewer is right that the comma form never lands there: `processLocalCommand` splits on the first comma at :4409-4414, `?EPASS,pw` gives `rootUpper == "EPASS"`, which is handled at :4750-4762 with `setESPNowPassword(args.c_str())` and **returns**. I also walked the whole legacy chain from :5148 to :5214 — nothing earlier matches "epass"/"EPASS", so the branch is genuinely reached.

The comma-less form is a REAL, DOCUMENTED user path, not a phantom: WCB_Help.cpp:352-356 prints, in the `?EPASS?` help page, `"\nLegacy commands:"` followed by `"  ?EPASSpassword   (e.g. ?EPASSmy_password)"`, and WCB_Help.h:182 lists `?EPASSpassword` in the LEGACY COMMANDS block. A user who reads the board's own help for EPASS is told to type the exact form that eats a character.

CONSEQUENCE — real but bounded. `espnowPassword` (WCB.ino:148, `char[40]`) is both the outbound stamp (`strncpy(... structPassword ...)` at :931, :2333, :2611, WCB_OTA.cpp:365 etc.) and the inbound gate (`if (String(pkt.structPassword) != String(espnowPassword)) return;` at :2643, :2973, :3046, :3130, :3144, :3183, :3238, :3305, :3720, :4011, WCB_OTA.cpp:446, WCB_RemoteTerm.cpp:150). `setESPNowPassword` (WCB_Storage.cpp:494-500) validates nothing and writes NVS immediately, so the board goes deaf and mute on the mesh and stays that way across reboots. Mitigations that cap the severity: the wrong value is echoed back on the spot at WCB.ino:5706 ("ESP-NOW Password updated to: y_password"), `?CONFIG` reprints it (:7333, :2169), and it is fixable over local USB serial with `?EPASS,<pw>`. Nothing is bricked.

Not on any tool path: `printBackupConfig` emits the comma form (WCB.ino:2796 `emit("EPASS," + String(espnowPassword), true)`) and the Wizard emits the comma form (Wizard/parser.js:1291 `add(\`EPASS,${config.espnowPassword}\`)`). So backup/restore and the Wizard are unaffected — only hand-typing (or an old paste buffer) of the help-documented legacy spelling.

INTENT — checked. No comment defends the offset. `git log -L 5702,5710:Code/WCB/WCB.ino` shows e4f26de ("fixed some bugs", 2026-04-07) changed `substring(5)` → `substring(6)`. At that same commit the comma handler already existed (`git show e4f26de:Code/WCB/WCB.ino` line 3304, `if (rootUpper == "EPASS")`), so the author was almost certainly stripping `"EPASS,"` (6 chars) for a form that provably cannot reach this function. That is a mis-fix, not a documented trade-off — so INTENT does not refute it.

SEVERITY, against this review's own legend (docs/CODE_REVIEW_2026-08.md:10-11): S2 = "wrong behaviour in a real scenario"; S3 = "latent race, bound, leak, overflow"; S4 = "dead code, stale comment, doc drift". This is not a race/bound/leak and it is not dead code — the branch dispatches and does the wrong thing on a spelling the firmware's own help advertises. S2 stands, at the low end of it.

</details>

### `Code/WCB/WCB.ino:6193` — CONFIRMED · S2 · ⚠ PROPOSED FIX IS WRONG

**processPWMOutput will pinMode/drive ANY serial port's TX pin — it never checks the port is a declared PWM output, defeating canUsePWMOnPort's stated guarantee**

**Correction to the finding as filed:**

The observation and mechanism are right; the PROPOSED FIX is wrong in two concrete ways.

(1) The gate is too strict and regresses a real, known configuration. `isSerialPortPWMOutput(port)` demands a POSITIVE declaration on the receiver. Today the unconditional `pinMode` at WCB.ino:6207 is what makes remote PWM work on a receiver that never got `?MAP,PWM,OUT,S<n>` — the peer was powered off when the mapping was made and WDP self-config is unavailable (WDP rides ETM and dies with ETM off, CLAUDE.md rule 4; it also needs `wdpAutoJoin` and a non-temp, non-client peer, WCB_WDP.cpp:704-705). The WDP comment at WCB_WDP.cpp:738-743 names that exact scenario ("this is the fix for 'a board powered off when the mapping was made never got configured'"), i.e. it is known to happen in the field. With the reviewer's gate those installs go from "servo driven, port otherwise unused" to "servo silently stops" — trading one silent failure for another.

Gate on the port being CLAIMED BY ANOTHER SUBSYSTEM instead, which is precisely the invariant WCB_PWM.cpp:68-70 states and leaves the unclaimed-port case working:

    // Mirror canUsePWMOnPort's reservation set (WCB_PWM.cpp:55-77): never bit-bang a TX
    // pin a serial-device module owns. Kept quiet + rate-limited — this runs at the RC
    // frame rate on the command-queue drain.
    if (isSerialPortUsedForHCR(port) || isSerialPortUsedForMP3(port) ||
        isSerialPortUsedForWLED(port) ||
        (port == 1 && (Kyber_Local || Maestro_Remote)) || (port == 2 && Kyber_Local)) {
        static unsigned long lastWarn = 0;
        if (millis() - lastWarn > 5000) { lastWarn = millis();
            Serial.printf("[PWM] ;P%d ignored - S%d is reserved for a serial device\n", port, port); }
        return;
    }

Do NOT call `canUsePWMOnPort(port)` directly here: it `Serial.printf`s on every rejection (WCB_PWM.cpp:57, :63, :72), and this path executes tens of times per second inside the loopTask queue drain — it would flood USB and add blocking serial writes to a path that already blocks for up to 2.5 ms in `delayMicroseconds`. Either add a `quiet` parameter to `canUsePWMOnPort` or factor the predicate out.

(2) The diagnostic in the proposed fix is dead code. `debugPWMEnabled` is declared `false` at WCB.ino:177 and has exactly two references in the whole tree — that declaration and the read at WCB.ino:6194. Nothing ever sets it (the settable flag is `debugPWMPassthrough`, WCB.ino:178, toggled at :4437/:4440 and :5165/:5168). A fix that logs under `debugPWMEnabled` drops the packet as silently as the bug it replaces. Use `debugPWMPassthrough`, or an unconditional rate-limited line as above — the operator needs this one visible, since the whole complaint is that the failure is silent. (Separately worth filing: WCB.ino:6194's existing "Invalid PWM output command" is unreachable for the same reason.)

Also worth folding in while touching this: the guard set above still omits a locally-configured Maestro and DFPlayer, the same hole already filed against `canUsePWMOnPort` as F-049 (docs/CODE_REVIEW_2026-08_wave2.md:340) — fix both predicates together or the new gate inherits the gap.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived from source, not from the write-up.

`processPWMOutput` is `Code/WCB/WCB.ino:6187-6215`. Verbatim, the only validation is `if (port < 1 || port > 5 || pulseWidth < 500 || pulseWidth > 2500)` (WCB.ino:6193), then a `switch` to `SERIALn_TX_PIN` and, unconditionally, `pinMode(txPin, OUTPUT); digitalWrite(txPin, HIGH); delayMicroseconds(pulseWidth); digitalWrite(txPin, LOW);` (WCB.ino:6207-6212). I grepped the whole `Code/WCB` tree for the reservation predicates (`isSerialPortPWMOutput`, `isSerialPortUsedForHCR/MP3/WLED`, `canUsePWMOnPort`): 40+ hits across WCB.ino, WCB_PWM/HCR/MP3/WLED/DFP/Storage/WDP — and none of them is inside or above `processPWMOutput`. The invariant comment the finding quotes is real and verbatim at `WCB_PWM.cpp:68-70` ("so two subsystems can never silently drive the same UART"), and the symmetric guards on the other side are real: `WCB_HCR.cpp:649-655`, `WCB_MP3.cpp:314-320`, `WCB_WLED.cpp:326-332`, `WCB_DFP.cpp:267-271`. So the claim "the guarantee is enforced only on the configuration path" is accurate.

REACHABILITY — traced end to end, both directions.
- Sender: `processPWMPassthrough` builds `snprintf(msg, sizeof(msg), ";P%d%lu", targetPort, pulseWidth)` and `sendESPNowMessage(targetWCB, msg, false)` at `WCB_PWM.cpp:723-724`. `addPWMMapping` validates a remote output's port ONLY for `wcbNum == 0` (local) — `WCB_PWM.cpp:249-256`; for a remote it just records it and fires `?MAP,PWM,OUT,S<n>` at the peer (`WCB_PWM.cpp:326-330`). Nothing on the sender ever learns whether the peer accepted.
- Receiver: `;P…` is explicitly whitelisted through the ETM-mismatch filter as `isPWMPacket` (`WCB.ino:3693-3694`, `:3706`), lands in `parseCommandsAndEnqueue(receivedCmd, 0)` (`WCB.ino:4227`), is drained by `handleSingleCommand` (`WCB.ino:7512`), which dispatches the `;` prefix to `processCommandCharcter` (`WCB.ino:4385`), whose `p`/`P` branch calls `processPWMOutput(message)` with no gate (`WCB.ino:5857-5858`). Only one call site exists (`grep processPWMOutput` → WCB.ino:5858 definition at :6187).
- Refusal on the receiver is real: `addPWMOutputPort` → `canUsePWMOnPort` → `isSerialPortUsedForHCR(port)` → prints "❌ Cannot use PWM on Serial%d - reserved for HCR/MP3/WLED" and returns (`WCB_PWM.cpp:798-800`, `:71-74`). The WDP self-config path refuses identically and *deliberately silently*: `if (!canUsePWMOnPort(prt)) continue;` (`WCB_WDP.cpp:746-748`). So the receiver refuses, logs at most once, and then honours the stream anyway. That is exactly the gap claimed.
- Additional route into the state with no simultaneous operator error: `loadPWMOutputPortsFromPreferences` re-validates each saved port at boot and silently drops+rewrites NVS if `canUsePWMOnPort` now fails (`WCB_PWM.cpp:905-916`), so a board can lose its PWM-output declaration across a reboot while the sender keeps streaming.

IMPACT — I checked the actual pinned core (esp32:esp32@3.3.4, per CLAUDE.md; core present at AppData/Local/Arduino15/.../3.3.4).
- S3-S5 are `SoftwareSerial` (`WCB.ino:885-887`), so the damage there is line-level: the TX line is yanked HIGH for 0.5-2.5 ms mid-frame and then left LOW (idle for a UART is HIGH), i.e. a break asserted between pulses at the RC frame rate. HCR/WLED/MP3 traffic on that port is corrupted. Real, matches the finding.
- S1/S2 are the hardware UARTs, and the finding's stronger claim checks out: `__pinMode` calls `perimanClearPinBus(pin)` (`cores/esp32/esp32-hal-gpio.c:117-121`), whose UART-TX deinit is `_uartDetachBus_TX` → `_uartDetachPins`, which does `esp_rom_gpio_connect_out_signal(txPin, SIG_GPIO_OUT_IDX, …)` and `uart->_txPin = -1` (`cores/esp32/esp32-hal-uart.c:248-257`, registered at `:512`). Every `Serial1.begin`/`Serial2.begin` in the firmware is inside `setup()` (`WCB.ino:7225-7244`, no other hits), so a detached TX stays dead until reboot. Pins are all positive on every HW rev (`wcb_pin_map.cpp:14,31-33,50-52,67-69,84-86,101-103`), so the `txPin > 0` gate never saves it.

INTENT — no comment, doc or blame defends it. `git log -L 6187,6215:Code/WCB/WCB.ino` shows the last touch was 0d85165 widening the pulse range 800-2200 → 500-2500; nothing about reservation. `docs/` has no PWM design page; the only mentions are prior review docs. Related-but-distinct prior finding: F-049 (`docs/CODE_REVIEW_2026-08_wave2.md:340`) covers `canUsePWMOnPort` omitting local Maestro/DFPlayer — a different hole in the same area, not this one.

MINOR CORRECTIONS to the write-up: the rate "50-200x/second" is loose — `PWM_MIN_TRANSMIT_INTERVAL` is 1 ms (`WCB_PWM.cpp:35`) but the real ceiling is the RC input's frame rate via `pwmNewData[]`, so ~50-333/s. Not load-bearing.

SEVERITY — S2 stands under the review's own rubric ("wrong behaviour in a real scenario", docs/CODE_REVIEW_2026-08.md:10). It is not S1 (nothing crashes, corrupts NVS or bricks; a reboot plus a config fix recovers), and it is above S3 because the end state is a silently dead device link on a board that explicitly refused the configuration and told the operator it had.

</details>

### `Code/WCB/WCB.ino:6490` — CONFIRMED · S1 → **S2** · ⚠ PROPOSED FIX IS WRONG

**processSerialCommandHelper calls parseCommandGroups/stopTimerSequence from serialCommandTask, racing loop()'s live reference into the commandGroups vector**

**Correction to the finding as filed:**

Mechanism, location and reachability are correct as stated; the severity and the fix are not.

Severity: S2, not S1. Both trigger windows are millisecond-scale and require an operator-typed line during an active `;t` sequence; "reproduces reliably" is not supported.

Fix: the direction is right (the drain at WCB.ino:7480 already runs before processCommandGroups at :7481), but the literal instruction "at WCB.ino:6490 call enqueuePendingTimerChain(data)" introduces a new silent-corruption bug. `enqueuePendingTimerChain` (WCB.ino:434-442) does `strncpy(slot.buf, chain.c_str(), sizeof(slot.buf) - 1)` into `char buf[TIMER_CHAIN_MAX_LEN]` with `TIMER_CHAIN_MAX_LEN = 220` (WCB.ino:404). That bound was chosen for the mesh path only — the comment at WCB.ino:400-401 says "Slot size 220 covers the 200-byte ESP-NOW structCommand", and `structCommand[200]` is confirmed at WCB.ino:209 and :782. The local-serial path has no such bound: `serialBuffer` is an Arduino String accumulated one char at a time with no cap (WCB.ino:6387-6388). So any console/Wizard-typed `;t` chain over 219 chars would be silently truncated mid-token — trading a rare crash for a routine mangle. `xQueueSend(..., 0)` at WCB.ino:441 also has no drop accounting (contrast `enqueueRcJsonRelay`, WCB.ino:406-410, which counts and logs), so an 8-slot overflow is invisible too.

Corrected fix: keep the deferral, but size it for the serial path — raise TIMER_CHAIN_MAX_LEN (or give the serial path a heap-allocated slot like `commandQueue` does with its `char* cmd`), and add a logged drop/over-length rejection so truncation can never be silent. For WCB.ino:6431, a `volatile bool timerStopRequested` set on the serial task and acted on in loop() before processCommandGroups is sound (single writer each direction). Finally, correct the false clause in the comment at WCB.ino:397-399 rather than leaving it asserting an invariant that never held.

<details><summary>Full verification reasoning</summary>

I re-derived every step rather than trusting the write-up.

REACHABILITY (the reviewer's central claim — verified true):
- `processSerialCommandHelper` is defined at WCB.ino:6423 and has exactly ONE caller: WCB.ino:6411, inside `processIncomingSerial`.
- `processIncomingSerial` (WCB.ino:6361) has callers ONLY inside `serialCommandTask`: WCB.ino:6598 (Serial/USB), :6603/:6604/:6605 (Serial3-5), :6613, :6618, :6621. Grep over Code/WCB/*.ino, *.cpp, *.h returns no other call site.
- `serialCommandTask` is created at WCB.ino:7439: `xTaskCreatePinnedToCore(serialCommandTask, "Serial Command Task", 4096, NULL, 1, NULL, 1)` — separate FreeRTOS task, priority 1, core 1. It is NOT loop().
So the local-serial entry point is provably off the loop task. The comment at WCB.ino:397-399 ("The local-serial entry point and SEQ playback both run inside loop() already, so they keep calling parseCommandGroups() directly") is half false. `git log -L 388,400:Code/WCB/WCB.ino` shows the clause was introduced with the queue in commit 5c49642 and has never been true.

The SEQ half of that comment IS correct, and I checked it so I could isolate the real gap: WCB_Storage.cpp:615 `parseCommandGroups(stripped)` <- `recallCommandSlot` (WCB_Storage.cpp:556) <- WCB.ino:6125 in `recallStoredCommand` <- `processCommandCharcter` (WCB.ino:5854) <- `handleSingleCommand` (WCB.ino:4385), whose only caller is the loop() queue drain at WCB.ino:7512. The MP3/DFP ONFIN lambdas (WCB_MP3.cpp:50, WCB_DFP.cpp:47) also reach `recallCommandSlot`, but via `processMP3Responses`/`processDFPResponses` at WCB.ino:7496/:7497 — loop() as well. So WCB.ino:6490 and :6431 are the only off-loop mutators.

MECHANISM (re-derived from command_timer.cpp, not the summary):
- Shared state: `std::vector<CommandGroup> commandGroups` (command_timer.cpp:13), `currentGroupIndex` (:15), `commandTimerModeEnabled` (:16), `waitingForNextGroup` (:17). No mutex, no portMUX anywhere — grepped.
- Writers on serialCommandTask: WCB.ino:6431 `stopTimerSequence()` -> `commandGroups.clear()` (command_timer.cpp:54) + `currentGroupIndex = 0` (:56); WCB.ino:6490 `parseCommandGroups(data)` -> `commandGroups.clear()` (:85), `currentGroupIndex = 0` (:86), then `push_back` at :123 and :177 (reallocation, not just element destruction).
- Reader on loop(): WCB.ino:7481 `processCommandGroups()` binds `CommandGroup &group = commandGroups[currentGroupIndex]` (command_timer.cpp:187), dereferences `group.commands[0]` at :189, and at :203 runs `for (const String &cmd : group.commands)` containing an explicit `vTaskDelay(pdMS_TO_TICKS(1))` at :207 — i.e. it yields the CPU while the reference and the range-for's begin/end iterators are live into the vector's heap storage.
- Scheduling: Arduino's loopTask is priority 1 on ARDUINO_RUNNING_CORE (esp32-hal.h:57 -> CONFIG_ARDUINO_RUNNING_CORE = 1 on dual-core esp32/esp32s3), same priority and core as serialCommandTask, which itself only delays 5 ms per pass (WCB.ino:6624). The `vTaskDelay(1)` at command_timer.cpp:207 is therefore a guaranteed scheduling point, not a theoretical one.
So: `clear()` destroys every CommandGroup (running each inner `std::vector<String>`'s destructor and freeing its String buffers) and `push_back` can reallocate the element array outright. loop() then resumes and reads `cmd` / `group.delayAfterPrevious` out of freed heap. That is genuine UB — LoadProhibited panic or a garbage command string handed to `parseCommandsAndEnqueue`.

I also checked the obvious ways this could already be guarded and found none: `parseCommandsAndEnqueue`/`enqueueCommand` from the serial task ARE safe (they go through `commandQueue`, WCB.ino:1937, drained only in loop() at :7503) — the timer path is the one that bypasses that. The other tasks (KyberLocalTask WCB.ino:6577, KyberRemoteTask :6588, RawSerialForwardingTask, PWMTask) never touch commandGroups. `drainPendingTimerChains()` at WCB.ino:7480 correctly precedes `processCommandGroups()` at :7481, so the ESP-NOW path (WCB.ino:3993, :4225) is already fixed — the serial path was simply missed.

WHY S2, NOT S1: the consequence is crash-class, but the reviewer's "reproduces reliably under any load where serial input arrives while a timer sequence is mid-run" is an overstatement. The wide window (command_timer.cpp:207) is only open ~1 ms per command while a group is actually firing — a small duty cycle across a multi-second chain. The other window (between the `currentGroupIndex >= commandGroups.size()` check at :183 and the `group.commands[0]` deref at :189) is open on every loop pass for the whole sequence but is only a few dozen instructions wide. Both writers require an operator-typed console line (`?STOP` or a new `;t` chain) landing inside that window. Real memory-safety defect, narrow trigger — S2.

Secondary (real, same call path, not memory-unsafe): `processIncomingSerial` writes `lastReceivedViaESPNOW = false` (WCB.ino:6404) and `inSequenceBody = false` (:6409) from the serial task while `processCommandGroups` is inside its save/set/restore window on those same two globals (command_timer.cpp:199-211), so a timer group can be enqueued with the wrong mesh-fanout origin. The proposed fix does not close this.

</details>

### `Code/WCB/WCB.ino:6598` — CONFIRMED · S1 → **S2** · ⚠ PROPOSED FIX IS WRONG

**serialCommandTask parses timer chains off the loop task, racing loop()'s live iteration of the commandGroups std::vector**

**Correction to the finding as filed:**

The reviewer's mechanism, line numbers and reachability are right; two things need correcting.

(a) Severity and symptom. S2, not S1. The most probable outcome is silent state corruption, not a crash: with the typical one-command-per-group chain the range-for exits before dereferencing anything, and the damage is loop() doing `currentGroupIndex++` (command_timer.cpp:213) over the serial task's `currentGroupIndex = 0` (command_timer.cpp:84), so the freshly issued chain drops its first group. The use-after-free DEREF additionally requires a group with >=2 commands (e.g. `;t500,;S1,A^;S2,B^;t500,...`).

(b) The exact yield that makes it deterministic is `vTaskDelay(pdMS_TO_TICKS(1))` at command_timer.cpp:209, inside the `for (const String &cmd : group.commands)` at :203. Worth citing, because it means the bug does not depend on FreeRTOS round-robin time-slicing at all — loopTask voluntarily blocks with live iterators into the shared vector.

Corrected fix. Defer-to-loop is the right direction but needs two changes the reviewer's version lacks:
 1. `enqueuePendingTimerChain` copies into `char buf[TIMER_CHAIN_MAX_LEN]` with `TIMER_CHAIN_MAX_LEN = 220` (WCB.ino:404, :406) via `strncpy(slot.buf, chain.c_str(), sizeof(slot.buf) - 1)` (WCB.ino:437). The comment at WCB.ino:401-402 sizes that slot for "the 200-byte ESP-NOW structCommand". Local serial lines are unbounded — `serialBuffer += c` at WCB.ino:6417 has no cap — so routing the serial path through this queue silently truncates any typed/pasted timer chain over 219 chars. Either raise TIMER_CHAIN_MAX_LEN or queue a heap-allocated `String*` (the command queue already does the malloc'd-`char*` pattern, WCB.ino:7504-7505).
 2. There is no queue for `stopTimerSequence()` at all. Enqueueing "?STOP" into pendingTimerChainQueue happens to work only by accident, via the `checkForTimerStopRequest(group.commands[0])` re-entry at command_timer.cpp:189 — and only after building a bogus one-group chain with `commandTimerModeEnabled = true` for a tick. Use an explicit `volatile bool pendingTimerStop` consumed at the top of `drainPendingTimerChains()` instead.

A smaller, lower-risk alternative that fixes the memory-unsafety without touching the serial path or the queue sizing: in `processCommandGroups`, copy `group.commands` into a local `std::vector<String>` before the fire loop so no reference or iterator into the shared vector survives the `vTaskDelay`, and re-check `commandTimerModeEnabled` plus a generation counter after the loop before doing `currentGroupIndex++`. That closes both the UAF and the skipped-group corruption. Either way, fix the now-known-false claim in the comment at WCB.ino:398-399.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived from source, not the summary.

1. `serialCommandTask` is a genuinely separate FreeRTOS task. Definition `Code/WCB/WCB.ino:6595`; created unconditionally in setup() at `WCB.ino:7439` as `xTaskCreatePinnedToCore(serialCommandTask, "Serial Command Task", 4096, NULL, 1, NULL, 1)` — core 1, priority 1. Arduino's `loopTask` is created at `AppData/Local/Arduino15/packages/esp32/hardware/esp32/3.3.4/cores/esp32/main.cpp:113` with priority 1 on `ARDUINO_RUNNING_CORE`, and `boards.txt:1796` lists `esp32.menu.LoopCore.1=Core 1` first, so the default (build.sh uses `esp32:esp32:esp32:PartitionScheme=min_spiffs`, no LoopCore override) is core 1. Same core, same priority, two tasks.

2. `processIncomingSerial` is called from NOWHERE but that task. Grepped the whole firmware: the only call sites are `WCB.ino:6598, 6603, 6604, 6605, 6613, 6618, 6621` — all inside `serialCommandTask`. It is never called from loop().

3. That path reaches both destructive writers. `processIncomingSerial` (WCB.ino:6361) -> `processSerialCommandHelper` (WCB.ino:6421) -> `WCB.ino:6429-6432 checkForTimerStopRequest(data) { stopTimerSequence(); }` and `WCB.ino:6489-6491 if (!data.startsWith(LocalFunctionIdentifier) && isTimerCommand(data)) parseCommandGroups(data);`. `stopTimerSequence()` is `command_timer.cpp:53-59` -> `commandGroups.clear()` at :54. `parseCommandGroups()` is `command_timer.cpp:74` -> `commandGroups.clear()` at :83, `currentGroupIndex = 0` at :84, `push_back` at :121 and :178.

4. loop() concurrently iterates the same vector. `WCB.ino:7481 processCommandGroups();` -> `command_timer.cpp:187 CommandGroup &group = commandGroups[currentGroupIndex];` and `command_timer.cpp:203 for (const String &cmd : group.commands)`. Line 209 is `vTaskDelay(pdMS_TO_TICKS(1));` INSIDE that range-for. That is an unconditional block of loopTask while a reference and cached begin/end iterators into `commandGroups[i].commands` are live — it does not even depend on FreeRTOS time-slicing; loopTask blocks and serialCommandTask (which wakes every 5 ms, `WCB.ino:6624`) is the equal-priority task that runs.

5. No guard exists. Grepped `Mutex|xSemaphore|portENTER_CRITICAL|taskENTER` in `command_timer.cpp` / `command_timer_queue.h` — nothing. Grepped every file for direct use of `commandGroups`/`currentGroupIndex`/`commandTimerModeEnabled`/`waitingForNextGroup`/`lastGroupTime` outside `command_timer.cpp` — no other manipulator, so no external serialization either.

INTENT — this is the decisive part, and it goes AGAINST the code, not for it. `WCB.ino:389-399` documents this exact hazard and the queue built to avoid it: "parseCommandGroups() writes the commandGroups std::vector ... It must NOT be called from the ESP-NOW WiFi-task callback because loop() concurrently iterates the same vector via processCommandGroups() — a vector resize concurrent with iteration is undefined behavior." Then `WCB.ino:398-399`: "The local-serial entry point and SEQ playback both run inside loop() already, so they keep calling parseCommandGroups() directly." The SEQ half is TRUE — I verified `WCB_Storage.cpp:615` sits in `recallCommandSlot` (`WCB_Storage.cpp:556`), reached only via `handleSingleCommand` (`WCB.ino:4354`), whose sole caller is `WCB.ino:7512` inside loop()'s command-queue drain. The local-serial half is FALSE. So this is a mistaken premise in a comment, not a documented trade-off. `git log -S` shows serialCommandTask predates the queue (present by 483b4d8, 2025-07-09; queue added 5c49642, 2026-05-27), so the comment was already wrong when written.

REACHABILITY — real but timing-narrow. A chain like `;t500,;S1,A^;t500,;S2,B` typed on USB or arriving on S1-S5 puts loop() in the fire loop with a guaranteed 1 ms yield per command. If a second line arrives on any of the six ports in that window and either equals `?STOP` (`LocalFunctionIdentifier='?'`, WCB.ino:155) or contains `;t`/`;T` (`CommandCharacter=';'`, WCB.ino:156), the race fires. Two distinct outcomes, and the common one is NOT a crash:
 - Single-command groups (the usual shape): the range-for exits without another deref, so no UAF read — but loop() then executes `currentGroupIndex++` (command_timer.cpp:213) on top of the `currentGroupIndex = 0` the serial task just wrote, so the newly issued chain SILENTLY LOSES ITS FIRST GROUP, and `lastGroupTime = millis()` (:212) is clobbered.
 - Multi-command groups (`;t500,;S1,A^;S2,B^;t500,...`): `clear()` runs the inner `std::vector<String>` destructors, freeing the String array the range-for cached `__begin`/`__end` into. `*__begin` on the next iteration is a read of freed heap, passed as `const String&` to `parseCommandsAndEnqueue` (:207) and to `cmd.c_str()` (:205) — genuine use-after-free, garbage command dispatch or LoadProhibited panic. Note `clear()` keeps the OUTER vector's capacity, so the outer buffer usually is not freed; the dangling pointers are the inner command arrays.

`?STOP` is the notable case because interrupting a running chain is precisely what `?STOP` is for, so the two events are correlated by design.

SEVERITY — downgrading S1 -> S2. It is a real, unguarded data race on a std::vector across two tasks, and the author's own comment shows it was not intended. But the window is ~1 ms per group fire out of a chain lasting hundreds of ms, the dominant manifestation is a dropped first group rather than a crash, and the UAF deref additionally requires a multi-command timer group. That is a should-fix, not a certain-crash-on-the-common-path blocker.

</details>

### `Wizard/app.js:1863` — CONFIRMED · S2

**Post-OTA ETM-edge replay fires every deferred remote pull in parallel on one relay**

**Correction to the finding as filed:**

The mechanism and location are right; three details in the impact narrative need correcting, and the fix is incomplete.

(a) The cross-assignment is **permanent, not transient**. When the shared line reaches each listener, `done = true; clearTimeout(timer)` (app.js:7942-7943) fires in all of them — so the losing slots never retry and are left holding the other board's config with `boardBaselines` set (app.js:7985). That baseline then makes `relayRouteAll` skip the slot on a later "Manage all" (`targets.filter(n => !boardBaselines[n] ...)`, app.js:527), so the wrong data sticks until a manual pull or page reload.

(b) The wrong config is **not** written onto the wrong physical board by the tool. Push resolves its destination from the slot's own config: `const pushTargetWCB = boardConfigs[n]?.wcbNumber || n` (app.js:7862, with a comment at :7859-7861 explaining exactly why), as do relayed MGMT (app.js:11985 area) and OTA (app.js:1728). So a poisoned slot pushes back to the board the config came from. The real hardware risk is the inverse and still serious: the user edits slot 3 believing it is WCB3, and the push lands on WCB2.

(c) Two symptoms the finding missed. The first listener's `cleanup()` sets `relayConn._lineTransform = null` (app.js:7934), which is what collapses the config blob into a "<N chars received>" summary — so a still-pending concurrent pull dumps the raw ~3 KB config into the terminal. And `startRemoteTermSession(r, bn)` at app.js:1864 fires N `?MGMT,FRAG,...RTERM,START` bursts (each `sendMgmtReliable` ×3, app.js:6073) back-to-back into the same relay on top of the pulls.

(d) Fix gap: serialising this loop still leaves the OTA target's own delayed re-pull — `setTimeout(..., 8000)` → `remoteBoardPull(relayN, n, 1, 12)` at app.js:1836-1841 — free to collide with a replay that is still running (three pulls at ~0.5 s each plus `MGMT_CHUNK_DELAY` 250 ms, or up to ~6 s each on timeout, easily exceeds 8 s). The same unserialised burst also exists in `installEtmListener` itself at app.js:7716 and :7741 when several boards flip together outside an OTA. The durable fix is a per-relay pull queue that all four call sites go through, not a patch at this one site.

<details><summary>Full verification reasoning</summary>

I re-derived every load-bearing fact from source rather than the summary.

**1. The loop is genuinely concurrent, not merely racy.** `Wizard/app.js:1854-1866`: inside `boardOtaRelay`'s `finally`, `const boards = [..._suppressedEtmEdges]` then a bare `for` loop calling `remoteBoardPull(r, bn)` at :1863 and `startRemoteTermSession(r, bn)` at :1864 — no `await`, no `.catch`. `remoteBoardPull` is `async` (app.js:7900) and its *only* `await` is `relayConn.send(...)` at the very end (app.js:8016); everything before it — pushing `onLine` onto `relayConn._dataCallbacks` (app.js:8013), arming the 6 s `timer` (app.js:8014), overwriting `relayConn._lineTransform` (app.js:7926) — runs synchronously. So after one loop tick all N listeners and N timers are live on the same connection. This is concurrency by construction, not a timing race.

**2. The listener is genuinely unfiltered.** `prefixBase = '[MGMT:CONFIG,'` (app.js:7921) with the comment at :7919-7920 explaining the choice ("Before the first pull we don't know the board's WCB_Number"). `onLine` (app.js:7938-7941) tests only `line.startsWith(prefixBase)` — it never compares the number it parses out of the prefix against `targetN`. It then writes `boardConfigs[targetN] = config` (app.js:7984) and `boardBaselines[targetN] = deep copy` (app.js:7985). Dispatch fans out to every callback: `this._dataCallbacks.forEach(cb => cb(line))` (app.js:5313). `cleanup()` (app.js:7933-7936) rebuilds `_dataCallbacks` as a *new* array, so `forEach` — already iterating the old array object — still delivers the same line to the remaining listeners. One `[MGMT:CONFIG,2]` line therefore poisons every concurrently-pending slot.

**3. `_pullingBoards` does not save it.** The guard (app.js:7906-7912) is keyed by `targetN`. Distinct targets never collide, and the loop explicitly `_pullingBoards.delete(bn)` at :1862 first anyway.

**4. The firmware confirms overlapping pulls are destructive.** The relay's reassembly state is a single object: `if (!pullSession.active || pullSession.sessionId != pkt.sessionId) { memset(&pullSession, 0, ...) }` (WCB.ino:3058-3068). `lastDeliveredSession` is one `static` (WCB.ino:3052-3053). Each target streams up to 16 chunks × 182 bytes twice with `delay(20)` per frag (WCB.ino:2999-3035, `CONFIG_PAYLOAD_SIZE` 183 at :252, `MGMT_MAX_CHUNKS` 16 at :837) — ~640 ms of frags. The relay's `handleMgmtPullRequest` blocks only ~45 ms (3 × `delay(15)`, WCB.ino:3546-3551), so three PULLs go out ~50 ms apart and their frag streams interleave, each resetting the other's session.

**5. The codebase already documents this exact rule — and this loop breaks it.** `relayRouteAll`'s comment at app.js:501-505: "The relay reassembles one config reply at a time, and the pull's `[MGMT:CONFIG,]` listener isn't target-filtered — so overlapping pulls would cross-assign configs to the wrong board. Hence one-at-a-time," implemented at app.js:531-538 via the `onComplete` callback. `send()`'s comment (app.js:5212-5217) likewise serialises writes for "arming several boards through ONE relay". So this is a violation of a documented constraint, not a deliberate trade-off — that rules out REFUTED on intent grounds.

**6. Reachability is not exotic — multi-board is the expected shape.** The firmware's offline sweep is one loop over all peers in a single tick (WCB.ino:1041-1051), printing consecutive `[ETM] WCB%d went OFFLINE` lines, so saturation takes several boards offline *together*. `installEtmListener` defers each into `_suppressedEtmEdges` at app.js:7709 (online) and :7730 (offline), gated on `remoteRelayForBoard[bn] === relayN`. The comment at app.js:7727-7728 states offline edges are "EXPECTED noise while an OTA saturates the channel". Additionally, the direct-USB OTA path adds to `_otaInProgress` (app.js:1548) but its `finally` (app.js:1698-1704) has **no** replay — so edges deferred during a USB OTA accumulate and are only drained by the next relay OTA, making the burst larger and staler. app.js:1854/1855/1856 are the only reader/clearer of the Set.

The defect stands. Severity S2 is right: it is silent state corruption in the tool's model of the fleet with a plausible path to misconfiguring real hardware, but it needs a wireless relay OTA plus ≥2 deferred boards on that same relay, and the poisoned card shows the wrong WCB number/alias so an attentive user can notice.

</details>

### `Wizard/app.js:3700` — CONFIRMED · S2 · ⚠ PROPOSED FIX IS WRONG

**Bidirectional-mapping code indexes slot-keyed maps (boardConfigs / boardConnections / boardPull) with a WCB number**

**Correction to the finding as filed:**

The defect stands; two things in the write-up need correcting.

1) Failure scenario — use a scenario that actually derives the mismatch. The clean one needs no user error: first-time auto-connect assigns slots by USB enumeration order (app.js:6577-6584). If the WCB2 board enumerates first it takes slot 1 and is kept there by the conflict branch (app.js:6873-6883), while the WCB1 board sits in slot 2 and can never migrate because the gate requires `detected >= 2` (app.js:6871). On card 1 the user maps S1 → "WCB 1" with bidir on: `boardConfigs[1]` is that same board's own config, so the reverse row is rendered on its own card and `saveMappingRow` sends `?MAP,SERIAL,S<port>,W2S<src>` down `boardConnections[1]` — a self-loop on the WCB2 board — while the real WCB1 gets nothing. Also note the send happens in `saveMappingRow` itself (app.js:3967), not only via Push All, and that `!boardConfigs[wcb]` seldom guards anything because `addBoardSection` seeds a default config with `wcbNumber = n` for every rendered slot (app.js:615-618).

2) Fix — resolving the slot is right, but copying app.js:3890-3894 verbatim is not sufficient. `Object.keys` on integer-like keys enumerates ascending (verified: `Object.keys({3:…,1:…,2:…})` → ['1','2','3']), so `find(idx => boardConfigs[idx]?.wcbNumber === X)` returns the LOWEST matching slot — and since every rendered-but-empty slot is seeded with `wcbNumber = slot`, a placeholder at slot X always shadows a real board numbered X living at a higher slot. The `?? removed.wcbNumber` fallback then re-introduces the exact number-as-slot bug when nothing matches. A correct helper prefers a real board and refuses to guess:

```js
function slotForWcb(num) {
  const keys = Object.keys(boardConfigs);
  const live = keys.find(k => boardConfigs[k]?.wcbNumber === num && boardConnections[k]?.isConnected?.());
  const pulled = keys.find(k => boardConfigs[k]?.wcbNumber === num && boardBaselines[k]);
  return live ?? pulled ?? null;   // null = not loaded; never fall back to `num`
}
```
Call sites then use the slot for `boardConfigs` / `boardConnections` / `boardBaselines` / `remoteRelayForBoard` / `boardPull` / `updateBoardStatusBadge` / `appendMappingRow` / the `b${slot}-…` DOM ids, and keep the raw WCB number only for the wire text (`W<n>S<p>`, app.js:3840/3951) and the MGMT target field (:3961). On `null`, skip the write and toast "WCB <n> not loaded — reverse mapping not applied" instead of silently returning. The same helper should replace the existing hand-rolled lookups at :3891-3894, :4078-4081 and :7592-7594, and cover `detectBidirMappings` (:3755-3757) and the PWM re-pull `boardPull(dest.wcbNumber)` (:3878).

Duplicate WCB numbers in the mesh remain inherently ambiguous; the helper picks the live one, which is the best available answer.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived from source, not the write-up.

1. The maps really are slot-keyed. Declarations: `let boardConnections = {}; // { boardIndex: BoardConnection }` (Wizard/app.js:42), `boardConfigs` (:43), `boardBaselines` (:44), `remoteRelayForBoard = {} // { boardSlot: relaySlot }` (:58). `boardPull(n)` opens with `const conn = boardConnections[n]` (app.js:6779) and `remoteRelayForBoard[n]` (:6766) — n is a slot. `updateBoardStatusBadge(n)` (:3184-3187) and `appendMappingRow(n)` (:3474) address DOM ids `b${n}-…`, i.e. the slot's card.

2. The destination selector emits WCB NUMBERS, not slots: `appendMappingDestination` builds options from `1..wcbQty` labelled `WCB ${num}`, with the local board's `wcbNumber` replaced by "Local" (app.js:3782-3788). So `wcb`/`dest.wcbNumber` is a number by construction.

3. The two bidir paths use that number as a slot key — verbatim:
   - `applyBidirMapping`: `if (!wcb || isNaN(port) || !boardConfigs[wcb]) return; const destCfg = boardConfigs[wcb];` … `destCfg.mappings.push(reverseMapping)` (:3719), `document.getElementById(\`b${wcb}-mappings-container\`)` (:3704), `appendMappingRow(wcb, …)` (:3724), `syncMappingsToConfig(wcb)` (:3725), `updateBoardStatusBadge(wcb,'unsaved')` (:3726).
   - `saveMappingRow` bidir block: `if (dest.wcbNumber > 0 && boardConfigs[dest.wcbNumber])` (:3932), `const destWcb = dest.wcbNumber` (:3934), then `remoteRelayForBoard[destWcb]` (:3953), `boardConnections[destWcb]` (:3954), `await destConn.send(reverseCmd + '\r')` (:3967), `boardBaselines[destWcb]` (:3963/:3968), `boardPull(destWcb)` (:3965/:3971).
   - Also `setTimeout(() => boardPull(dest.wcbNumber), 4000)` in the PWM re-pull (:3878) and `boardConfigs[dest.wcbNumber]` in `detectBidirMappings` (:3755-3757).
   The same function resolves the slot properly 160 lines later — `// remoteRelayForBoard and boardConnections are keyed by board SLOT, not WCB number. Find the slot whose wcbNumber matches.` (:3889-3894), added 2026-03-31 in 5b438bd7 — and again at :4078-4081 and in the push path at :7592-7597. The bidir code is older (e5e0ca7f, 2026-03-05) and was never brought along. So this is an acknowledged hazard with an unpatched instance, not a deliberate design.

REACHABILITY — slot ≠ wcbNumber is producible three ways, two of them without any user error:
   (a) The migration gate is `if (detected !== n && detected >= 2 && detected <= WCB_MAX)` (app.js:6871). A board configured as **WCB 1** therefore NEVER migrates: if it lands in slot 2+ it stays there with `wcbNumber = 1` and no warning at all (falls into the "Board number matches slot (normal path)" else-branch at :6915-6922).
   (b) The conflict branch at :6873-6883 keeps a board in its landed slot when the target slot is occupied ("…keeping here").
   (c) `onWCBNumberChange` sets `boardConfigs[n].wcbNumber = val` in place, no slot move (app.js:1877-1885).
   Concrete two-board case, no misuse required: first-time auto-connect assigns slots 1..k in `navigator.serial.getPorts()` order (app.js:6577-6584). If the WCB2 board enumerates first it takes slot 1 and WCB1 takes slot 2; both pull at +3 s. Slot-1's pull detects 2, sees `boardConnections[2].isConnected()` → keeps it at slot 1 (`boardConfigs[1].wcbNumber === 2`); slot-2's pull detects 1, fails `detected >= 2` → stays (`boardConfigs[2].wcbNumber === 1`). Both boards live, both mismatched, permanently. Now on card 1 (the WCB2 board) the destination dropdown offers "Local" and "WCB 1"; picking WCB 1 gives `wcb = 1` → `boardConfigs[1]` is the board's OWN config. `applyBidirMapping` pushes the reverse mapping into its own config and renders the row on its own card; `saveMappingRow` then sends `?MAP,SERIAL,S<destPort>,W2S<src>` over `boardConnections[1]` — i.e. to the WCB2 board itself, creating a self-loop — and toasts "Reverse mapping pushed to WCB 1". The real WCB1 never gets its reverse mapping.
   The `!boardConfigs[wcb]` guard rarely saves anything, because `addBoardSection` seeds every rendered slot: `if (!boardConfigs[n]) { boardConfigs[n] = WCBParser.createDefaultBoardConfig(); boardConfigs[n].wcbNumber = n; }` (app.js:615-618). So the usual outcome is a write into a placeholder/other-board config, not a clean early return. When the guard does fire (slot deleted after a migration), the symptom is the bidir toggle silently doing nothing — the reverse mapping is neither stored nor sent.

INTENT — no comment, doc or CLAUDE.md rule sanctions the number-as-slot indexing; the only comments on the subject (:3889-3890) say the opposite. This is not a documented trade-off.

Severity: keep S2. It requires a mis-slotted board, but that state is reached by ordinary USB enumeration order in a plain two-board setup, and the outcome is either a `?MAP,SERIAL` written to the wrong physical board or a silently no-op bidir toggle, in both cases with a green success toast.

Reviewer sloppiness worth noting (does not change the verdict): the stated scenario ("stays in slot 3 with wcbNumber = 2… a different board occupies slot 2") never explains why slot 2's occupant would have a different number, and if that occupant is itself a WCB 2 the write is ambiguous rather than clearly wrong; and it says "Push All then writes…" when `saveMappingRow` itself sends the reverse command directly at :3967 (Push All is a second, separate path).

</details>

### `Wizard/app.js:3784` — CONFIRMED · S2 · ⚠ PROPOSED FIX IS WRONG

**Wizard mapping-destination WCB dropdown is built from the WCBQ floor, so any destination above it is silently rewritten to "Local"**

**Correction to the finding as filed:**

Two corrections to the finding as written.

1. The rewrite target is not always "Local". `Local` is emitted at the index of `localWCB` (app.js:3786), so it is the first option only when `localWCB === 1`. When `localWCB !== 1` the browser falls back to option 0 = `WCB 1`, silently retargeting the mapping at a different physical board. And when `localWCB > wcbQty` the list contains **no Local option at all**, so a genuinely local destination (`wcbNumber === 0`) is likewise rewritten to `WCB 1`. The second half is missing from the finding and is the more damaging case.

2. The on-board consequence is additive, not destructive. `WCB_Storage.cpp:1636-1655` reuses the existing mapping slot without resetting `outputCount`, and neither the per-row Save (app.js:3837-3841) nor `buildCommandString` (parser.js:1513-1520) emits a preceding `MAP,...,CLEAR,Sx`. The real destination survives on the board; the push adds a spurious one. So: wrong Wizard display (immediate, zero user action), corrupted in-memory/exported config (after one mapping edit), and an extra unintended serial fan-out destination on the board (after a push) — recoverable with `?MAP,SERIAL,CLEAR,Sx`. S2 stands; it is not S1.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived from source, not the summary.

`Wizard/app.js:3774` `appendMappingDestination(rowId, n, dest)`. Lines 3782-3788 verbatim (I read them, the reviewer's transcription is byte-accurate):

  3782  const wcbQty    = parseInt(document.getElementById('g-wcbq')?.value) || systemConfig?.general?.wcbQuantity || 1;
  3783  const localWCB  = boardConfigs[n]?.wcbNumber || n;
  3784  const wcbOptions = Array.from({length: wcbQty}, (_,i) => {
  3785    const num = i + 1;
  3786    if (num === localWCB) return `<option value="0" ...>Local</option>`;
  3787    return `<option value="${num}" ...>WCB ${num}</option>`;
  3788  }).join('');

The option list is exactly 1..wcbQty, and the local board's number is *consumed* by the "Local" entry rather than added to it. Two consequences, not one:
(a) any dest.wcbNumber > wcbQty has no matching option;
(b) when localWCB > wcbQty there is **no "Local" option at all**, so a legitimate local destination (`dest.wcbNumber === 0`) also has no matching option.
In both cases no `<option>` carries `selected`, so the browser selects index 0 — "Local" (value 0) when localWCB === 1, otherwise "WCB 1" (value 1). The finding's title says "Local"; that is only the localWCB===1 case. The other fallback silently retargets to a *different real board*, which is worse.

The values are real, not clamped anywhere upstream:
- `Wizard/parser.js:961` `/^W(\d+)S(\d+)$/` → `{wcbNumber: parseInt(...)}`, no bound (parser.js:960-967). `parseMappingToken` (parser.js:917-950) pushes whatever `parseDestination` returns. `evaluatePortClaims` (parser.js:986+) only reads local (wcbNumber===0) dests.
- Firmware: `Code/WCB/WCB_Storage.cpp:1717` rejects only `wcbNum > MAX_WCB_COUNT || serialPort < 0 || serialPort > 5`. `MAX_WCB_COUNT` is 20 — I opened the declaration: `Code/WCB/WCB.ino:120` and `Code/WCB/WCB_Storage.h:12`, both 20. So destinations 1..20 are accepted regardless of WCBQ.
- A board number above WCBQ is legal by design: `updateWCBNumber` (`WCB.ino:5694-5697`) accepts 1..MAX_WCB_COUNT with no WCBQ check; `Default_WCB_Quantity` defaults to **1** (`WCB.ino:135`); `rebuildActivePeers` (`WCB.ino:6859`) treats WCBQ as a floor unioned with learned peers; `addActivePeer` (`WCB.ino:6926-6930`) has an explicit comment "Only ids ABOVE the WCBQ floor carry the learned (persisted) bit". The reviewer's cited 6918-6924 is the right function, one comment block off.

THE LOSS PATH. `syncMappingsToConfig` (app.js:3584-3620) rebuilds `config.mappings` **entirely from the DOM** — `const wcb = parseInt(destRow.querySelector('[id$="-wcb"]')?.value) || 0` (:3609). It is called from `onMappingChange` (:3572), `onBidirChange` (:3579), `removeMappingRow` (:3635), `_removeBidirRows` (:3675), `applyBidirMapping` (:3725) and `saveMappingRow` (:3827). So editing *any* mapping row on the board rewrites *every* row's destinations from the truncated selects. Note `boardGo` (:7780-7787) does **not** call `syncMappingsToConfig`, so a pull-then-push with no mapping interaction is safe — the corruption needs one mapping edit. After that, `buildCommandString` (parser.js:1508-1521) sees `JSON.stringify(baseline.mappings) !== JSON.stringify(config.mappings)` and emits the rewritten `MAP,...` for every mapping.

TEMPERING (the reviewer overstates the on-board damage). The firmware `MAP,SERIAL,Sx,...` handler reuses an existing active mapping for that input port and only zeroes `outputCount` when it allocates a *new* slot (`WCB_Storage.cpp:1636-1655`); destinations are then appended (`:1731-1735`) with a duplicate check. Neither `saveMappingRow` (:3837-3841) nor `buildCommandString` sends a preceding `MAP,...,CLEAR,Sx`. So the real remote destination is **not deleted on the board** — the push *adds* a bogus one. Result on the wire: S3 now fans out to W12S1 *and* S1 (or, in the missing-"Local" case, S3 → W1S2 unicast to a board that was never meant to receive it). Wrong behaviour, silent, but recoverable via `?MAP,SERIAL,CLEAR,Sx` — that is S2 ("wrong behaviour in a real scenario", docs/CODE_REVIEW_2026-08.md:10), not S1.

REACHABILITY — I checked for a guard and there is none. Nothing ever rebuilds the destination `-wcb` select after creation: `onMappingTypeChange` (:3549-3565) rebuilds only `portSel.innerHTML`. Every `[id$="-wcb"]` reader (:3609, :3698, :3711, :3831) reads the truncated select. `g-wcbq` is set from the first pulled board only (`syncGeneralFromConfig`, :8069-8077, guarded by `if (!generalBaseline)` at :6855) and is never raised by WDP discovery — `addDiscoveredBoards` (:588-595) and `desiredBoardNumbers` (:431-438) render sections above `_boardFloor` without touching `g-wcbq`. So the Wizard genuinely renders a section for WCB 12 while `g-wcbq` reads 2. Two concrete user-reachable states:
1. Pulled config carries a destination above WCBQ (created by terminal `?MAP`, a backup restore, or WCBQ later lowered) → displayed wrong immediately, rewritten on the next mapping edit.
2. **Entirely inside the Wizard, no terminal needed**: `applyBidirMapping` (:3718) builds the reverse mapping with `destinations: [{ wcbNumber: localWCB, port: src }]` where `localWCB = boardConfigs[n]?.wcbNumber || n`. If board n self-identifies above the floor (the auto-migration branch at :6871 accepts `detected >= 2 && detected <= WCB_MAX`), that reverse destination cannot be rendered, and `syncMappingsToConfig(wcb)` fires on the *very next line* (:3725) and rewrites it to WCB 1 before the user does anything else. Ticking the bidir toggle is enough.

INTENT — this is drift, not a trade-off. `git log -L 3774,3790:Wizard/app.js` shows commit 7ff9025 replaced a fixed `Array.from({length: 9})` list with the WCBQ-driven one; the intent was to narrow the list to the fleet and put "Local" at the board's own number. Nothing in that commit or any comment addresses above-floor numbers. Decisively, the same file already solves this exact problem twice: `populateUIFromConfig` (:2926-2931) widens with `Math.max(..., selectedWcb /* always include the board's self-reported number */)`, and `_populateRouteHostDropdown` (:2517-2523) widens with `Math.max(..., self, selectedHost || 0, 2)` and carries the comment "NEVER let the <select> silently default to this board's own number ... which would then re-push forever" — the identical failure class, already recognised and guarded elsewhere. `appendMappingDestination` is the one dropdown that was missed.

</details>

### `Wizard/app.js:3837` — CONFIRMED · S2

**`?MAP,SERIAL` appends destinations in firmware, so editing or removing a destination in the Wizard leaves the old one live**

**Correction to the finding as filed:**

The observation and mechanism stand as written; four refinements.

1. Third failure mode the finding misses: deleting ALL destinations and saving sends `?MAP,SERIAL,S1`, which the dispatcher rejects at WCB.ino:4491-4494 ("Invalid format. Use: ?MAP,SERIAL,Sx,dest…") — the mapping is left completely intact, not merely stale.

2. The finding should note the 2 s auto-pull (app.js:3866 → populateMappingsFromConfig) repopulates the row with the board's real list, so the wrong state is visible rather than silent; the toast is what lies, and the board's routing is what is actually broken.

3. The fix must be SERIAL-only, as written. Do not generalise it to `MAP,${type.toUpperCase()},CLEAR,S${src}`: for PWM that reaches `removePWMMapping()` (WCB_PWM.cpp:344), which ends in `delay(3000); ESP.restart();` — the board would reboot and swallow the MAP command that follows. Also both branches of saveMappingRow need it (direct `conn.send`, and a second `sendMgmtReliable` MGMT,FRAG for relayed boards).

4. The buildCommandString half of the fix is under-specified and leaves a live path broken. Gating the CLEAR on "destination set differs from the baseline" emits nothing when `fullPush` is true (`const fullPush = !boardBaselines[n]`, app.js:7417 / :7810) — and the post-FLASH restore sets `boardBaselines[n] = null` at app.js:7186 while a firmware flash does not erase NVS (the erase path is separate, at :7341/:7357 with the explicit "board NVS is blank" comment). So a post-flash full push still appends onto the board's retained mappings. Emit `MAP,SERIAL,CLEAR,S<src>` unconditionally before each serial mapping — it is idempotent (prints "No mapping found for Serialx", WCB_Storage.cpp:1904, when absent).

Two things worth knowing when implementing: (a) CLEAR-then-ADD also fixes rawMode never persisting on an all-duplicate save, because the re-add makes `outputsAdded > 0` and reaches `saveSerialMonitorMappings()` (:1753); (b) it churns the broadcast flags — the CLEAR re-enables broadcast in/out for the port (WCB_Storage.cpp:1885-1898) and the re-add auto-blocks them again (:1739-1750) — the end state is identical, so no regression, just two extra NVS namespace writes per save.

Separate but adjacent gap, not covered by this fix: a mapping present in the baseline and absent from the pushed config still gets no CLEAR from buildCommandString, so loading a config file with fewer mappings leaves the extras live on the board.

<details><summary>Full verification reasoning</summary>

I re-derived the whole chain rather than trusting the summary.

WIZARD SIDE (verified by reading, not the summary):
- `saveMappingRow()` is at Wizard/app.js:3808. It rebuilds the destination list from the live DOM (:3830-3834) and composes `let cmd = `${lfi}MAP,${type.toUpperCase()},S${src}`` at :3837, `,R` at :3838, then appends `,S<p>` / `,W<n>S<p>` per destination at :3839-3841. It sends exactly that one string — `await conn.send(cmd + '\r')` (:3857) or wrapped in `MGMT,FRAG` for a relayed board (:3852-3853). No clear precedes it on either branch.
- I grepped every `CLEAR` in Wizard/app.js. The only mapping-related clears are `MAP,${type},CLEAR,S${src}` in `removeMappingRow()` (:3641 — whole-row delete only) and `MAP,PWM,CLEAR,OUT,S<p>` for removed remote PWM destinations (:3897). There is no serial clear anywhere else. `grep -n "MAP,SERIAL" Wizard/*.js` returns only the two build sites (app.js:3950 bidir reverse, parser.js) and comments.
- Push Config path confirmed: parser.js:1509-1522 — `mappingsChanged` gate at :1509-1510, `for (const m of config.mappings)` at :1512, `add(cmd)` at :1520. Same append-only string, no `CLEAR`. Notable contrast in the same function: removed Maestros DO get `MAESTRO,CLEAR,...` (:1474) and removed sequences DO get `SEQ,CLEAR,...` (:1535). Serial mappings are the omission.
- Destination deletion is genuinely reachable: the per-destination trash button at app.js:3803 does `document.getElementById('${destId}').remove();onMappingChange(...)`, and Save is an inline `onclick="saveMappingRow('${rowId}',${n})"` at :3510.

FIRMWARE SIDE (all line numbers re-checked by hand):
- Dispatch: WCB.ino:4473 `if (rootUpper == "MAP")`. `CLEAR,ALL` → :4485, `CLEAR,S` → :4487-4489 `removeSerialMonitorMapping(portPart)`. Anything else falls to :4490-4508, which rewrites `S1,W2S3` into the legacy form and calls `addSerialMonitorMapping(legacyStr)` (:4508). Nothing clears first.
- `addSerialMonitorMapping()` is at WCB_Storage.cpp:1593. The find-existing loop is :1636-1642 and it takes `mapping = &serialMonitorMappings[i]; break;` with NO reset. `mapping->outputCount = 0` appears only in the create-NEW branch (:1650). Duplicate destinations are skipped with `continue` (:1723-1730), new ones are appended at :1732-1737 (`if (mapping->outputCount < 10)`), and the save is gated on `if (outputsAdded > 0)` (:1752) — so a pure-removal command persists nothing and prints "No new destinations added (all were duplicates or invalid)" (:1758). Struct confirmed: `SerialMonitorOutput outputs[10]` (WCB_Storage.h:88-94), `MAX_SERIAL_MONITOR_MAPPINGS 5` (:70).
- The asymmetry with PWM is real: `addPWMMapping()` sets `mapping.outputCount = 0;` at WCB_PWM.cpp:227 before its parse loop — replace semantics — and even sends `MAP,PWM,CLEAR,OUT,S<p>` to remote boards that dropped out (:298-318).
- Round-trip confirmed: the backup emitter writes the full destination list per mapping (WCB.ino:2817-2829), so `boardPull` re-reads the board's real (stale) list.

SCENARIO RE-DERIVED: board holds S1 → S2, W2S3. User deletes the S2 dest row and saves. Wizard sends `?MAP,SERIAL,S1,W2S3`; :1636-1642 finds the S1 slot, W2S3 hits the dup skip at :1723, `outputsAdded == 0`, nothing saved, S2 still fanned out. The "worse on change" variant is also exactly right: replacing S2 with S4 sends `?MAP,SERIAL,S1,S4`, S4 is not a duplicate, so it is appended at :1732 and S1 now feeds S2 AND S4, persisted to NVS at :1753. A green "Mapping saved to WCB n" toast fires either way (app.js:3860).

INTENT: no comment, doc, or help text calls the append behaviour deliberate. `?MAP ?` help (WCB_Help.cpp:61-90) documents only `SERIAL,Sx,dest` / `SERIAL,CLEAR,Sx` and gives whole-mapping examples. Nothing in docs/ or CLAUDE.md sanctions incremental appends, and the Wizard is plainly written against replace semantics. `git log -L` on the function shows the command has never had a preceding clear.

MITIGATION I CHECKED (does not rescue it): `setTimeout(() => boardPull(n), 2000)` at app.js:3866 pulls the board back, and `populateUIFromConfig` → `populateMappingsFromConfig` (app.js:2923 + ~:3147) rebuilds the rows, so the stale destination visibly reappears ~2 s after the success toast. So the state is not permanently hidden — but the routing on the board is wrong and stays wrong, and the row's own Save button can never undo it. Only deleting the entire row (:3641) or a terminal `?MAP,SERIAL,CLEAR,S<n>` clears it.

Severity: keep S2. It is not a crash, and the auto-pull surfaces the contradiction, but the primary edit operation of a config tool silently does the wrong thing, the board keeps forwarding serial traffic to a port the user removed (in a droid: stray text into a Maestro/MP3/WLED port), and the "change a destination" case leaves a double-route the user cannot remove from the row. It also hits the Push Config path, not just the per-row Save.

</details>

### `Wizard/app.js:7039` — CONFIRMED · S2

**Push All throws a TypeError on a MgmtRelay slot and silently aborts the remaining stages**

**Correction to the finding as filed:**

The mechanism, line and severity are right; the *consequence* is overstated in the headline scenario and understated in another.

Overstated: "critically, stage 4 never sends the deferred reboots". In the exact scenario given (a MgmtRelay at slot 19 with WCB 2 and WCB 5 routed through it), `relayRebootList` would be empty anyway — it is filled only from stage 3's `boardGo(n,{skipReboot:true})` return value (app.js:8301), and the only member of `directRelays` there is the MgmtRelay itself, which throws before returning. The remote boards' reboots are NOT at risk: `boardGoRemote` appends `^?reboot` into the same MGMT session payload (app.js:7843-7848) during stage 1, precisely to dodge the MAC-filter race. So in that scenario the actual losses are (a) the relay's own push — which should never have happened, and (b) `generalSettingsDirty = false; updatePushAllButton()` at :8333-8334, leaving Push All stuck amber. Stage 4 only genuinely loses a queued reboot in the narrower case where a *WCB-firmware* relay board is also in `directRelays` and sorts BEFORE the MgmtRelay id (e.g. WCB relay 1 + MgmtRelay 19) — that board gets "Config applied — reboot queued by Push All…" (:7551-7561) and then never reboots.

Understated: the crash does not need any board routed through the relay. With a MgmtRelay connected and `remoteRelayForBoard` empty, `relayBoardSet` is empty and the relay slot falls into `directNonRelays`, so stage 2 crashes. Since `Object.keys` on integer-like keys iterates ascending, a relay whose `DEVICE_ID` sorts below a real board (e.g. relay id 3 alongside WCB 4 and WCB 7) silently skips every direct board after it — that is the worst case, and it is the one worth leading with.

Fix note: the load-bearing half is the `boardGoAll` filter, not the button guard. Guarding `btn` alone lets `boardGo` run its whole configure path against the relay: `syncSerialUIToConfig(19)` (app.js:2904-2921) reads a DOM that does not exist and overwrites `boardConfigs[19].serialPorts` with `|| 9600` / `?? true` / `?? ''` defaults, and `config.espnowPassword/macOctet2/3/meshChannel/delimiter/funcChar/cmdChar/wcbQuantity` are then taken from the global `g-*` fields (:7408-7415) and diffed against the relay's real baseline (`boardBaselines[relaySlot]`, :6829) — so a Wizard whose global password/WCBQ/chars differ from the MgmtRelay's compile-time constants would push spurious `?EPASS`/`?WCBQ`/`?CMDCHAR` (and even a bootstrap `?FUNCCHAR`) at a device that implements none of them, then time out on each `sendAndAwaitIdle`. Apply both halves, and note that `_relaySlots` holds NUMBERS (`_relaySlots.add(relaySlot)`, :6844) while `Object.keys` yields strings — the filter must use `!_relaySlots.has(+n)`, exactly as the repo already does at :5751/:5753. The version written in docs/CODE_REVIEW_2026-08_wave3.md does use `+n`; the truncated snippet in this task does not show it.

<details><summary>Full verification reasoning</summary>

I re-derived every link in the chain from source rather than the summary.

1. The unguarded deref is real and transcribed correctly. `Wizard/app.js:7005` `const btn = document.getElementById(`b${n}-btn-go`);` and `Wizard/app.js:7039-7040` `btn.disabled = true; btn.textContent = …` with no null test. The only guards between them are `if (remoteRelayForBoard[n]) return boardGoRemote(...)` (:6992), `if (!conn?.isConnected())` (:6997) and the shared-hub borrow block (:7014-7038, entered only for flash/update/factory/erase). Nothing else can return early for a relay slot.

2. `b<n>-btn-go` exists ONLY inside the numbered board section. `Wizard/index.html:496` is the sole occurrence, inside `<template id="board-template">` (index.html:414), instantiated only by `addBoardSection()` (app.js:611-643), called only from `reconcileBoardGrid()` (app.js:459).

3. A MgmtRelay slot has a live connection but no section. `boardPull`'s relay branch: `boardConnections[relaySlot] = conn` (app.js:6828), `_relaySlots.add(relaySlot)` (:6844), `applyRelayRole(relaySlot)` (:6845). `applyRelayRole` (:479-484) removes `section-board-<n>`, deletes it from `_meshBoards`, reconciles, and renders the relay card. `desiredBoardNumbers()` (:437) ends `.filter(n => n >= 1 && n <= WCB_MAX && !_relaySlots.has(n))`, so the section is never re-created; the mesh-render loop also skips relays (`_relaySlots.has(n)` at :12630). I read `renderRelayCard` (:545-583) in full — its innerHTML contains only Terminal / Manage all via relay / Disconnect buttons, no `btn-go`. So `document.getElementById('b19-btn-go')` is null while `boardConnections[19].isConnected()` is true.

4. `boardGoAll` really does pass that slot to `boardGo`. `app.js:8254-8256`: `Object.keys(boardConnections).filter(n => boardConnections[n]?.isConnected()).map(Number)` — no relay filter. `relayBoardSet = new Set(Object.values(remoteRelayForBoard).map(Number))` (:8281) contains relay SLOTS (`setRemoteConnected(targetN, relaySlot)` via `relayManageOne`, :493), so a MgmtRelay with routed boards lands in `directRelays` → `await boardGo(n,{skipReboot:true})` (:8301); with no boards routed it lands in `directNonRelays` → `await boardGo(n)` (:8297). Either way `btn.disabled = true` throws `TypeError: Cannot set properties of null`.

5. The throw is silent and aborts the rest. `boardGoAll` has no try/catch around either loop and its only caller is the inline `onclick="boardGoAll()"` at `Wizard/index.html:33`; I grepped app.js and index.html — there is no `unhandledrejection`/`window.onerror` handler anywhere in the Wizard. So the user sees the "Pushing to N boards…" toast (:8285) and then nothing but a console trace.

6. Reachability is real, not theoretical. `Wizard/parser.js:475-479` sets `config.isRelay` from `?RELAY,1`, and `WCBClient/examples/MgmtRelay/MgmtRelay.ino:329` prints exactly `Serial.println("?RELAY,1");` inside `wcbPrintBackup()` — the reply to the Wizard's `WCB_WEBTOOL_CONFIG_PULL`/`?backup` bootstrap. Connect a MgmtRelay over USB, pull, click Push All: crash. No firmware source under Code/WCB emits `?RELAY,` (grep found only OTA/RemoteTerm comments), so this is MgmtRelay-specific, as claimed.

7. Not deliberate. `git log -L 7035,7042:Wizard/app.js` shows the line untouched since d249b1e (2026-07-30, the auto-share/auto-demote work) — it predates the relay-card feature; no comment or doc anywhere claims relay slots are meant to be pushed. On the contrary, `app.js:6812-6814` states "a relay is a special conduit, not a configurable board".

The finding stands. One overstatement in its impact claim, corrected below.

</details>

### `Wizard/app.js:7447` — CONFIRMED · S1 → **S2** · ⚠ PROPOSED FIX IS WRONG

**Changing Local Function Char back to '?' is never sent — the whole push is then mis-dispatched to the broadcast path (and out over ESP-NOW)**

**Correction to the finding as filed:**

The mechanism is wrong: `xFUNCCHAR,?` is NOT accepted by the firmware. `processLocalCommand` is entered at WCB.ino:4394 with `message == "FUNCCHAR,?"` and is intercepted at WCB.ino:4402 by `if (message.endsWith("?"))`, which calls `printCommandHelp("FUNCCHAR,")` and returns — the FUNCCHAR setter at WCB.ino:5045 is unreachable for this input, whatever prefix char carries it. Because `printCommandHelp` (WCB_Help.cpp:10-13) does not strip the trailing comma, `c == "FUNCCHAR"` at WCB_Help.cpp:886 misses and the unmatched `else` at WCB_Help.cpp:1015 dumps the whole top-level command reference.

The guard at app.js:7447 / parser.js:1302 is therefore a deliberate, correctly-reasoned workaround for a firmware limitation — its comment is accurate. The bug is that the workaround is incomplete: it suppresses the unsendable command but does not tell the user or abort the push.

Correct fix (firmware, required): exempt a `FUNCCHAR,`-rooted message from the `endsWith("?")` help branch at WCB.ino:4402, or add an explicit spelling such as `?FUNCCHAR,DEFAULT` alongside the `args.length()==1` case at WCB.ino:5046. Only after that can the Wizard guard be relaxed.

Correct fix (Wizard, interim — safe to land now): in the bootstrap block at app.js:7436-7448, detect `curFuncChar !== '?' && config.funcChar === '?'` and abort the push with a toast telling the user to run `?ERASE,NVS` or set the char from the board's terminal — a rejected edit is far better than a push that sprays `?MAC`/`?EPASS`/`?WCB` over ESP-NOW. The same guard is needed in `boardGoRemote` (app.js:7805 reads g-funcchar with no bootstrap at all).

Secondary observation that does stand independently: gate the reboot-path badge at app.js:7507 on `_pushFullyAcked`, matching the no-reboot path at app.js:7572.

<details><summary>Full verification reasoning</summary>

SYMPTOM — verified, stands.

1. The guard is real and blocks exactly one direction. `Wizard/app.js:7447`: `if (config.funcChar !== curFuncChar && config.funcChar !== '?')`. With `curFuncChar = boardBaselines[n]?.funcChar ?? '?'` (`app.js:7433`), the transition `'x' -> '?'` passes term 1 and fails term 2, so `bootstrap` stays empty and the `for` loop at `:7449` sends nothing. `Wizard/parser.js:1302` has the same wrapper (`if (config.funcChar !== '?') { ... add('FUNCCHAR,...') }`), so no FUNCCHAR is emitted from the diff either.

2. The rest of the push is built with the TARGET char. `parser.js:1226`: `const lfi = opts.outputFuncChar ?? config.funcChar ?? '?'`, and `add()` at `:1230` prefixes every command with `lfi`. `boardGo` passes no `opts` (`app.js:7418`), so every command goes out as `?...` while the board is still on `'x'`.

3. The mis-dispatch and mesh spray are real. `WCB.ino:4380` `if (cmd.startsWith(String(LocalFunctionIdentifier)))` fails for `?MAC,2,AB` when LFI=='x'; `:4384` CommandCharacter fails; `:4390` falls to `processBroadcastCommand`. That function writes the text out every non-reserved serial port (`WCB.ino:6343`) and then unconditionally `sendESPNowMessage(0, cmd.c_str())` (`WCB.ino:6354`, comment "Always send via ESP-NOW broadcast"). A peer still on default '?' receives it at `WCB.ino:4227` `parseCommandsAndEnqueue(receivedCmd, 0)` -> `handleSingleCommand` -> `processLocalCommand`, and applies `?MAC,2,..` / `?EPASS,..` / `?WCB,..` to itself.

4. Reachable. `Wizard/index.html:151` is a plain `maxlength=1` text input with `oninput="onGeneralCmdCharChange()"`; `onGeneralCmdCharChange` (`app.js:1175-1182`) does no validation, and `app.js:7413` just reads `.value || '?'`. Nothing refuses '?'. A pull re-syncs the field from the board (`app.js:5364-5372`), so the user must deliberately type '?' — but nothing stops them.

5. The badge claim is correct on the reboot path. MAC triggers reboot (`app.js:8189` `if (u.includes('MAC,')) return true`), and the reboot branch calls `updateBoardStatusBadge(n, 'configured')` at `app.js:7507` with NO `_pushFullyAcked` gate — unlike the no-reboot branch at `app.js:7572` which does gate on it.

MECHANISM AS STATED BY THE REVIEWER — FACTUALLY WRONG.

The finding asserts "the firmware handles that fine — WCB.ino:4380 dispatches ... then WCB.ino:5045-5049 takes args = '?' ... and assigns LocalFunctionIdentifier = '?'". It never reaches `:5045`. `WCB.ino:4380` calls `processLocalCommand(cmd.substring(1))`, i.e. `message == "FUNCCHAR,?"`. `processLocalCommand` (defined `WCB.ino:4394`) hits `WCB.ino:4402` `if (message.endsWith("?")) { String cmd = message.substring(0, message.length()-1); cmd.trim(); printCommandHelp(cmd); return; }` — it returns before the comma split at `:4409` and before the FUNCCHAR handler at `:5045`. The prefix character is irrelevant; `xFUNCCHAR,?` fails identically. So the comment at `app.js:7445` and `parser.js:1297-1301` is accurate, not stale.

Worse for the fix: `printCommandHelp` (`WCB_Help.cpp:10-13`) only does `c.trim(); c.toUpperCase();` — it does not strip the trailing comma, so `c == "FUNCCHAR"` at `WCB_Help.cpp:886` is FALSE ("FUNCCHAR," != "FUNCCHAR"). It falls to the unmatched `else` at `WCB_Help.cpp:1015`, which prints the entire top-level command reference.

No firmware escape hatch exists on this branch: `WCB.ino:5045-5053` accepts only `args.length() == 1`; there is no `FUNCCHAR,DEFAULT`; and the legacy form (`?LFx`, advertised at `WCB_Help.cpp:905`) is handled at `WCB.ino:5182`, far below the `:4402` interception, so `xLF?` is swallowed the same way.

PRIOR ADJUDICATION. This is a re-raise of F-138. `docs/CODE_REVIEW_2026-08.md:89` already records "F-138 — symptom real, proposed fix wrong. The firmware cannot parse FUNCCHAR,? at all", with the full hand-verification at `docs/CODE_REVIEW_2026-08.md:315-348`. I re-derived it independently above rather than taking that doc's word, and it is correct.

SEVERITY. Downgraded S1 -> S2. The defect is real, silent, and can misconfigure peer boards, but it requires (a) the board to already be on a non-default funcChar — a niche setting, (b) the user to deliberately revert to '?', and (c) for the destructive peer-apply, a mixed-char fleet (peers still on '?' while the target is on 'x'). The Wizard also self-corrects its displayed state on the next auto-pull (`app.js:7541-7542` schedules `boardPull`, and `app.js:5364-5372` re-syncs g-funcchar from the board), so the tool does not carry a false baseline forward. It is a known firmware limitation with an incomplete mitigation, not a fresh regression on the common path.

</details>

### `Wizard/app.js:7481` — CONFIRMED · S2

**A push containing a PWM input mapping makes the firmware auto-reboot mid-push; every command after it is silently lost**

**Correction to the finding as filed:**

The defect stands; four corrections.

(a) THE TITLE OVERSTATES THE BLAST RADIUS. It is not "every command after it". The loss window is bounded and endsat an explicit flush: `WCB.ino:7119-7122` (`setRxBufferSize(2048)` → `Serial.begin(115200)` → `delay(1000)` → `while (Serial.available()) Serial.read();`). Typically the TWO commands immediately following the mapping are destroyed (one by `ESP.restart()`, one by the :7122 flush); commands the Wizard sends after the flush are absorbed by the 2 KB ring and do execute. Accurate title: "the two commands following a PWM input mapping are destroyed by the firmware's auto-reboot and scored as ACKed".

(b) LINE NUMBERS. The `if (autoReboot)` tail is WCB_PWM.cpp:339-343, not 338-342. The send is app.js:7483; the loop 7481-7493. Parser blocks: Mappings 1508-1522, Stored Sequences 1524-1538. Variables/Maestro/ETM are already ahead of the mappings block, so ONLY `SEQ,SAVE`/`SEQ,CLEAR` — and any Serial mapping row ordered after a PWM row — are stranded.

(c) THE SHARPEST SYMPTOM IS UNDER-STATED. The reason this is worse than "commands lost" is that the loop's own safety net is defeated: `sendAndAwaitIdle` returns `ok: lines.length > 0` (app.js:5280) and the boot banner satisfies that, so no retry fires, no red line is logged, `_pushFullyAcked` stays true, and the user gets the "WCB n configured — rebooting…" success toast (:7508). The post-push verify pull then overwrites `boardConfigs[n]` with the board's real (incomplete) state, so the intended sequences also vanish from the UI without a warning.

(d) THE FIX NEEDS ONE MORE CLAUSE AND HAS A KNOWN RESIDUE. Reordering must place PWM input mappings after BOTH the sequences AND the Serial mappings, since `config.mappings` preserves DOM/parse order. Even then, two or more PWM input mappings in one push still lose all but the first, because each reboots — separately logged at docs/CODE_REVIEW_2026-08_wave2.md:437. The durable fix is firmware-side: WCB.ino:4542 → `addPWMMapping(pwmConfig, false)` plus a deferred-restart flag honoured once the command queue drains, which is plainly what the currently-dead `autoReboot=false` branch was added for. Note also this finding duplicates docs/CODE_REVIEW_2026-08_verdicts.md:528 (same defect, filed at parser.js:1531) — merge them rather than shipping two.

<details><summary>Full verification reasoning</summary>

I re-derived every step from source rather than the write-up.

1. THE LOOP (Wizard/app.js:7481-7493). Read 7380-7580. `const fullPush = !boardBaselines[n]` (:7417) → `WCBParser.buildCommandString(...)` (:7418) → split on `delim+funcChar` (:7467-7468) → `for (const cmd of commands)` (:7481) with `conn.sendAndAwaitIdle(cmd, {quietMs:250, hardTimeoutMs:5000})` (:7483). The only failure the loop recognises is "no response". `commandStringNeedsReboot` is consulted only AFTER the loop, at :7496 — so the Wizard's own knowledge that this command reboots the board (comment at :8192 "firmware auto-reboots, but we signal it too", regex `/MAP,PWM,S\d/i` at :8194) is applied too late to protect the loop.

2. THE REBOOT IS REAL AND UNCONDITIONAL. `Code/WCB/WCB.ino:4509-4543`: the `?MAP,PWM,…` handler falls past LIST / CLEAR,ALL / CLEAR,OUT,S / CLEAR,S / OUT,S and reaches `addPWMMapping(pwmConfig);` at :4542 — one argument. Declaration `Code/WCB/WCB_PWM.h:38  void addPWMMapping(const String &config, bool autoReboot = true);`. `grep -rn addPWMMapping Code/WCB` gives exactly two call sites (WCB.ino:4542, WCB.ino:5254) and both are one-arg, so the `false` branch is dead. Definition tail `Code/WCB/WCB_PWM.cpp:339-343`: `if (autoReboot) { Serial.println("Rebooting in 3 seconds to apply PWM configuration..."); delay(3000); ESP.restart(); }`. There is no idempotency early-return: `addPWMMapping` (WCB_PWM.cpp:170) re-applies and reboots even for an identical mapping. I checked the sibling `addPWMOutputPort` (WCB_PWM.cpp, `?MAP,PWM,OUT,S<n>`) — it does NOT reboot, so the reviewer is right that only the input-mapping form is hazardous.

3. THE EMITTER ORDER PUTS COMMANDS BEHIND IT. Wizard/parser.js: PWM Output Ports 1499-1506, Mappings 1508-1522 (`add(cmd)` at :1520, cmd = `MAP,${m.type.toUpperCase()},S${m.sourcePort}…`), Stored Sequences 1524-1538 (`SEQ,SAVE` at :1531, `SEQ,CLEAR` at :1536), `return cmds.join(delim)` at :1540. Variables (1428-1443), Maestro (1445-1481) and ETM (1483-1497) are all ahead of the mappings block, so only SEQ commands are stranded — plus any Serial mapping that sits after a PWM row, since `config.mappings` keeps DOM/parse order (app.js:3599-3618, parser.js:945).

4. THE ACK NET DOES NOT CATCH IT — and this is the part that makes it silent. `sendAndAwaitIdle` (app.js:5267-5294) resolves `{ ok: lines.length > 0 }` (:5280) after 250 ms of quiet. The board prints the reboot line then goes silent inside `delay(3000)`, so the Wizard resolves ~250 ms in and immediately writes the next command into a board with ~2.75 s of `delay()` to go. Then `ESP.restart()`. The board's boot banner arrives well inside that command's 5 s hard window and is fed to `_dataCallbacks` by `_handleLine` (boot lines are not RC noise), so `lines.length > 0` → `ok:true` → no retry, no error line, `_pushFullyAcked` stays true. And the reboot path (:7497-7548) never reads `_pushFullyAcked` anyway.

5. A FACT NEITHER THE REVIEWER NOR THE EXISTING VERDICT FOUND, WHICH MAKES THE LOSS DETERMINISTIC. `WCB.ino:7119-7122`: `Serial.setRxBufferSize(2048); Serial.begin(115200); delay(1000); while (Serial.available()) Serial.read();  // flush startup junk`. So the board explicitly DISCARDS its whole RX ring one second after `Serial.begin`. The deaf window is therefore not just the `delay(3000)` — it is the remaining ~2.75 s of delay, plus ROM boot, plus the 1 s stabilise delay, terminated by an explicit flush. In the natural timeline the Wizard fires the next command ~250 ms into the delay (lost to `ESP.restart()`), then its `sendAndAwaitIdle` resolves 250 ms into the silent `delay(1000)` at :7121 and fires a second command — which is destroyed by the flush at :7122. Two commands lost, both scored as ACKed. Commands issued after the flush do land in the 2048-byte ring and execute.

6. REACHABILITY IS ROUTINE, NOT EXOTIC. PWM is a first-class mapping type in the UI (`<option value="PWM">` in `appendMappingRow`, app.js:3490). Full pushes are forced at app.js:7144, 7186, 7202, 7259 (post-flash restore), 7341/7357 (`// force full push — board NVS is blank` after `?ERASE,NVS`) and 11712 (`// force a full push` in `wizardWatchForConnect`). Any of those on a board whose config carries a PWM input mapping plus stored sequences hits this. A delta push hits it too whenever both a mapping and a sequence changed (`mappingsChanged`, parser.js:1509-1510).

7. INTENT ARGUES FOR THE FINDING, NOT AGAINST. The firmware's own canonical emitter — `collectConfigCommands`, declared "SINGLE SOURCE OF TRUTH for the WCB config command list" (WCB.ino:2749) — emits `SEQ,SAVE` at :2910 and only then `// PWM output ports + mappings (mappings LAST — restore triggers a reboot)` at :2916. parser.js inverts exactly the order the firmware comment says to keep. No comment, doc or CLAUDE.md rule sanctions the Wizard's order.

8. DUPLICATE. This is the same defect already recorded as CONFIRMED at S2 in docs/CODE_REVIEW_2026-08_verdicts.md:528, filed there against `Wizard/parser.js:1531` (the emitter) rather than app.js:7481 (the consumer). The firmware half is separately logged at docs/CODE_REVIEW_2026-08_wave2.md:437. My verification is independent and reaches the same conclusion, with the :7122 flush as an addition.

</details>

### `Wizard/app.js:7811` — CONFIRMED · S2 · ⚠ PROPOSED FIX IS WRONG

**boardGoRemote has no funcChar/delimiter/cmdChar bootstrap — changing a command character over a relay sends a payload the target cannot parse**

**Correction to the finding as filed:**

Three corrections. (1) MECHANISM for the headline funcChar scenario: the payload never reaches the target. `onGeneralCmdCharChange` (Wizard/app.js:1175-1186, wired oninput at index.html:147/152/157) has already rewritten `boardConfigs[relayN].funcChar` to the new char, so the OUTER wrapper built at app.js:7864 is `.MGMT,FRAG,...`. The relay's LFI is still '?', so WCB.ino:2084's MGMT-keep-whole branch misses, the line is split on '^', and each piece falls through handleSingleCommand (WCB.ino:4380-4390) into processBroadcastCommand (WCB.ino:6234) — sprayed out the relay's unclaimed serial ports (:6343) and ESP-NOW-broadcast (:6355). The reviewer's "target discards the chain" description applies to the DELIMITER case, not the funcChar case. (2) SCOPE: cmdChar is not affected over the relay — buildCommandString formats only with funcChar/delimiter (parser.js:1225-1226), so `?CMDCHAR,<new>` inside the chain applies normally. And a delimiter-only DIFF push emits just `?DELIM,<new>` (parser.js:1294-1295) and works; the delimiter case only breaks on a fullPush (`!boardBaselines[n]`, :7810) or when other settings changed alongside it. (3) BLAST RADIUS is larger than stated: app.js:7873 writes the pushed config into boardBaselines[n] after the false "success", and the same stale `boardConfigs[relayN].funcChar` breaks every other relay operation (pull :8033, relayed terminal :8900, variable/sequence sends :4518/:4386/:4453), so the remote board is unrecoverable over the relay and needs USB.

<details><summary>Full verification reasoning</summary>

WHAT I OPENED: Wizard/app.js:1175-1186, 6991-6993, 7380-7520, 7769-7885, 8203-8209, 8252-8335; Wizard/parser.js:1219-1310; Wizard/index.html:140-160, 496; Code/WCB/WCB.ino:1946-2110, 2630-2735, 4355-4400, 5088-5092, 6234-6357, 6380-6500; docs/CODE_REVIEW_2026-08_verdicts.md; git log -L on the bootstrap block.

THE GAP IS REAL. boardGoRemote (app.js:7769) goes config-sync -> `const cmdString = WCBParser.buildCommandString(config, boardBaselines[n] ?? null, fullPush)` (:7811) -> fragment (:7852) -> `${relayFcPush}MGMT,FRAG,...` loop (:7863-7869). The words DELIM / CMDCHAR / FUNCCHAR appear nowhere between :7769 and :7885 — confirmed by grep. The direct path's bootstrap is at :7427-7460 and its comment states the constraint explicitly. buildCommandString formats with `const lfi = opts.outputFuncChar ?? config.funcChar ?? '?'` / `const delim = opts.outputDelim ?? config.delimiter ?? '^'` (parser.js:1225-1226) and boardGoRemote passes no opts, so the chain is written in the NEW characters. Reachable: index.html:496 `onclick="boardGo({N})"` -> app.js:6993 `if (remoteRelayForBoard[n]) return boardGoRemote(n, opts)`, and boardGoAll stage 1 at :8292-8294. No guard covers it — commandStringChangesNetworkGroup (:8203-8208) only matches MAC,2 / MAC,3 / EPASS / WCBCH.

DELIMITER CASE — reviewer's mechanism is exactly right. Relay: LFI is still '?', so `?MGMT,` matches the special branch at WCB.ino:2084 and the line is kept whole and forwarded. Target: handleMgmtPacket reassembles and calls `parseCommandsAndEnqueue(fullCmd, 0)` (:2733); that scans with the live `commandDelimiter` (:2094 `data.indexOf(commandDelimiter, startIdx)`), finds no match, so `singleCmd` is the whole chain -> handleSingleCommand -> processLocalCommand parses only the first root token (e.g. `HW` with args `32~?WCB,3~...`, `args.toInt()` = 32 at :5119). Everything after is dropped. CAVEAT the reviewer missed: this only bites when the chain has more than one command. A delimiter-only DIFF push emits just `?DELIM,<new>` (parser.js:1294-1295) and works fine. It reliably breaks on a fullPush (`!boardBaselines[n]`, :7810 — a remote board that was never pulled), or whenever anything else changed alongside the delimiter, because DELIM is emitted after HW/WCB/MAC/EPASS.

FUNCCHAR CASE — real, but it fails one hop EARLIER than described, with bigger blast radius. onGeneralCmdCharChange (app.js:1175-1186) is wired `oninput` on all three fields (index.html:147, 152, 157) and writes the new char into EVERY boardConfigs entry, including the relay's. So at :7864 `relayFcPush = boardConfigs[relayN]?.funcChar || '?'` is already the NEW char while the relay board still speaks the old one. The relay therefore never sees `?MGMT,...`: WCB.ino:6398 `_isMgmt` false, WCB.ino:2084's MGMT branch needs the LFI prefix so it misses, the line is split on the still-current '^', and each piece reaches handleSingleCommand (:4380-4390) matching neither LocalFunctionIdentifier nor CommandCharacter -> `processBroadcastCommand` (:6234). The relay writes the config fragments out every unclaimed serial port (:6343) and ESP-NOW-broadcasts them (:6355). Nothing is forwarded to the target at all. Same end state (target unchanged, silently), worse side effects.

CMDCHAR — not affected remotely. buildCommandString formats only with funcChar/delimiter, so `?CMDCHAR,<new>` inside the chain reaches the target and applies. Including cmdChar in the title is an overreach.

EXTRA DAMAGE THE REVIEWER UNDERSTATED: :7873 sets `boardBaselines[n]` to the pushed config on "success" and :7867 toasts success, so the Wizard permanently believes the remote board is converted and no later diff will re-send it. And because the stale `boardConfigs[relayN].funcChar` is used by every other relay path too (pull :8033, relayed terminal :8900, variables :4518, sequences :4386/:4453, board state :8596), a funcChar change makes the whole relayed board unreachable from the Wizard until the relay is pushed over USB — after which the relay speaks the new char and the target still speaks the old one, so it can never be fixed over the relay at all. USB recovery only.

INTENT: no comment anywhere defends the omission. git log -L 7420,7465 shows the bootstrap was added to boardGo alone and later hardened with ACK-pacing; boardGoRemote was never given the equivalent. docs/CODE_REVIEW_2026-08_verdicts.md:926 already noted in passing "The same guard is needed in boardGoRemote (app.js:7805 reads g-funcchar with no bootstrap at all)" — an acknowledged, unfixed gap, not a deliberate trade-off.

SEVERITY: S2 stands. Silent, produces a split fleet, poisons the baseline, and recovery needs a USB cable — but it requires Advanced mode (the three inputs carry `advanced-only hidden`), a relay setup, and a deliberate one-time char change. That matches the repo's own calibration of the sibling funcChar defect at verdicts.md:954 (S1 -> S2 for the same "niche setting" reason).

</details>

### `Wizard/app.js:7853` — CONFIRMED · S2

**boardGoRemote never checks the 16-chunk MGMT ceiling — an oversized config is rejected wholesale by the relay while the Wizard reports success**

**Correction to the finding as filed:**

Two numeric/scope corrections, the second of which is a separate and lower-threshold defect the reviewer walked past:

1. Threshold. `Wizard/app.js:67` sets `MGMT_CHUNK_SIZE = 180`, so `total` exceeds 16 at 2881 chars, not 2865. The finding's "16 × 179 = 2864" is the firmware's true carrying capacity (correctly quoted from docs/SEQUENCE_INVENTORY.md:216 and WCBClient/src/WCB_Client.h:189), not the Wizard's current chunk boundary.

2. The 180-vs-179 mismatch is itself a live silent-corruption bug at the same call site, and it bites far below 16 chunks. `Code/WCB/WCB.ino:793` declares `char payload[180];` and `WCB.ino:2617-2618` does `strncpy(pkt.payload, payload.c_str(), sizeof(pkt.payload) - 1); pkt.payload[sizeof(pkt.payload) - 1] = '\0';` — 179 chars max, the 180th char of every full chunk is discarded and never transmitted. `WCB_Client.h:189` states this outright: "179 // payload[180] minus NUL (firmware strncpy)". So any Wizard push over 180 chars (i.e. essentially every full push, ~6 chunks at ~1000 chars) currently loses the character at each 180-char boundary, mangling whatever command straddles it. The firmware already fixed this exact class of bug on the config-PULL path and left a comment recording it (`WCB.ino:2994-2999`: "The original code used all 183 bytes and then forcibly set [182] = '\0', silently dropping one character from every full chunk ... corrupting the parsed config"). The push path never got the same treatment on the Wizard side.

Correct fix is therefore both halves, which also makes the reviewer's 2864 figure exactly right:
  - `Wizard/app.js:67` → `const MGMT_CHUNK_SIZE = 179;  // firmware espnow_struct_mgmt.payload[180] minus NUL (WCB.ino:2617) — matches WCB_MGMT_CHUNK_LEN`
  - after `Wizard/app.js:7853` add the `if (total > 16)` guard, aborting before the `boardBaselines[n]` write at 7873 so the baseline is not poisoned.
Optionally mirror `MGMT_MAX_CHUNKS` as a named constant rather than a literal 16, and add a matching guard to `updateSequence` (app.js:4455) and `sendVariableCommand` (app.js:4513), which hardcode `,0,1,` and will silently truncate to 179 chars when a single sequence value pushes the command past 198 (WCB.ino:2593 declines the ETM path, falling through to the truncating broadcast at 2617).

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived from source, not the summary.

Sender side (Wizard):
- `Wizard/app.js:67` — `const MGMT_CHUNK_SIZE = 180;` (NOT 179 as the finding's arithmetic implies).
- `Wizard/app.js:7761-7767` — `fragmentString()` slices unconditionally; no cap.
- `Wizard/app.js:7852-7853` — `const chunks = fragmentString(cmdToSend, MGMT_CHUNK_SIZE); const total = chunks.length;`. I read 7769-7885 in full: from the `relayN`/`relayConn` checks (7774-7777) through the config sync, the `getVariablesFromUI` null guard (7793-7798), the network-group confirm (7820-7826) and the reboot append (7842-7848), there is no length or chunk-count bound anywhere.
- `Wizard/app.js:7865` — `?MGMT,FRAG,<target>,<sid>,${i},${total},<chunk>` sends `total` verbatim.

Receiver side (relay firmware):
- `Code/WCB/WCB.ino:837` — `#define MGMT_MAX_CHUNKS 16`. Verified, not transcribed.
- `Code/WCB/WCB.ino:2583` — `if (targetWCB < 1 || targetWCB > MAX_WCB_COUNT || totalChunks == 0 || totalChunks > MGMT_MAX_CHUNKS) { ... return; }`. It is inside `handleMgmtForward()` and evaluated per packet, so with `total = 17+` every fragment returns here. Nothing is ever broadcast, so the target never opens a session — confirmed by reading `handleMgmtPacket()` (2630+), which is only reached via the ESP-NOW broadcast at 2620 that never happens.
- The only diagnostic is `if (debugMGMT) Serial.println("[MGMT] Invalid parameters");` and `Code/WCB/WCB.ino:256` — `bool debugMGMT = false; // remote management protocol — off by default`. So the drop is genuinely silent in stock configuration.
- Single-chunk sends are unaffected: 2593 routes `totalChunks == 1 && payload.length() <= 198` through ETM. All other Wizard MGMT senders hardcode `,0,1,` (app.js:2879, 3650, 3852, 3908, 3962, 4284, 4388, 4433, 4455, 4520, 6072, 6087, 8598, 8901, 9137). `boardGoRemote` is the sole multi-chunk sender in the Wizard.

False-success claim — verified and it is worse than described:
- `Wizard/app.js:7870` `showToast(...'success')`, `:7871` "All chunks sent", `:7882` `updateBoardStatusBadge(n,'configured')`.
- `Wizard/app.js:7873` `boardBaselines[n] = JSON.parse(JSON.stringify(config));` — the baseline is set to a config the board never received. Every later push is then diffed against a fiction and yields "Nothing to push — no changes detected" (7812). The user cannot recover by retrying; only a re-pull or page reload fixes it. The reviewer did not mention this and it is the aggravating factor.

REACHABILITY — the required state is producible without exotic user behaviour:
- `Wizard/app.js:8352-8400` `loadSystemFileContent()` writes `boardConfigs[n] = board` (8383) for every board in the file and never touches `boardBaselines`. So "load my saved droid file, connect the relay, push board N" hits `const fullPush = !boardBaselines[n]` (7810) → true → full push including every sequence.
- `Wizard/parser.js:1524-1533` emits `?SEQ,SAVE,<key>,<value>` per sequence and `parser.js:1429-1443` emits `?VAR,SET` per variable, all included on a full push. `Wizard/app.js:4200-4240` — the sequence value `<textarea>` has no `maxlength` (only the 15-char key does).
- Sizing it myself against `TestConfigs/*.txt` (largest real fixture is 506 chars, sequence-free) plus the full-push command set in `buildCommandString` (HW/LED/WCB/ALIAS/WCBQ/WCBCH/CONTROLLER/MAC×2/EPASS/DELIM/CMDCHAR, BAUD×5, LABEL×5, BCAST×11, KYBER, MP3, HCR, DFP, WLED, MAESTRO, ETM×8, MAP…) a fully-populated board lands near ~1000 chars before any sequences. That leaves roughly 1880 chars of headroom, i.e. the ceiling is crossed at somewhere around 10-22 stored sequences depending on length. `Code/WCB/WCB_Storage.h:69` allows `MAX_STORED_COMMANDS 80`, and `docs/SEQUENCE_INVENTORY.md` exists precisely because large sequence sets are normal here. This is well inside real use.
- Confirming it is an oversight and not a deliberate trade-off: every other sender of this wire format bounds itself. `Code/WCB/WCB.ino:3002` guards the config-pull direction (`if (totalChunks > MGMT_MAX_CHUNKS) { log; return; }`), and `WCBClient/src/WCB_Client.h:188-190` + `WCBClient/src/WCB_Client.cpp:437-441` define `WCB_MGMT_MAX_COMMAND_LEN` and refuse with a printed error. `docs/SEQUENCE_INVENTORY.md:216` documents the 16 × 179 = 2864 ceiling exactly as the finding cites. The Wizard is the only sender missing the check.

INTENT — `git log -L 7850,7870:Wizard/app.js` shows the last touch is 02f997d (2026-07-15), which changed the routing target to `wcbNumber` and added a comment about that; nothing about size limits. `git log -S MGMT_CHUNK_SIZE` shows 978ecf4 as the only commit — the constant has never been revisited. No comment, doc page, or CLAUDE.md rule sanctions the unbounded send.

WHAT THE REVIEWER GOT WRONG (numbers only): the Wizard chunks at 180, not 179, so the trigger threshold is `cmdToSend.length > 2880`, not > 2864. See the correction field — chasing that discrepancy surfaced a second, lower-threshold defect at the same site.

</details>

### `Wizard/app.js:7938` — CONFIRMED · S1 → **S2**

**Remote-pull listener is not target-filtered — a config reply is written to every board with a pull in flight**

**Correction to the finding as filed:**

The finding stands; three corrections/additions.

(1) Better reachability than the one cited: the strongest case is not the ETM race but `app.js:1855-1868`, the post-OTA reconciliation loop, which fires `remoteBoardPull(r, bn)` for every deferred board with no `await` — two boards on one relay arm two listeners in the same tick, deterministically. Second-strongest is a user clicking two "Manage via relay" buttons (`app.js:563` → `relayManageOne` → `:494`) or two Pull Config buttons within the 6 s timeout.

(2) The reviewer missed that this is a KNOWN hazard, already spelled out at `app.js:498-502` and mitigated only inside `relayRouteAll`. That strengthens the finding (the other call sites are unguarded) and should be cited in the fix commit.

(3) Fix placement matters. Capture the requested number BEFORE registering the listener, and compare against the captured value — do not re-evaluate `boardConfigs[targetN]?.wcbNumber` inside `onLine`. `onWCBNumberChange` (`app.js:1877-1882`) mutates `boardConfigs[n].wcbNumber` live from a dropdown, so a mid-flight edit would make the reviewer's expression reject the legitimate reply. Concretely: hoist `const pullWCBNum = boardConfigs[targetN]?.wcbNumber || targetN;` (currently computed at `:8032`, after the `_dataCallbacks.push` at `:8013`) above the listener, then in `onLine` after `closeIdx`: `const src = parseInt(line.slice(prefixBase.length, closeIdx), 10); if (Number.isFinite(src) && src !== pullWCBNum) return;` (return WITHOUT setting `done`, so the other listener still gets it and this one keeps waiting). Delete the now-dead `prefix` at `:7946`.

Two residual defects the target filter does not fix, worth noting in the same change: `relayConn._lineTransform` is a single shared slot (`:7926`, nulled by `cleanup` at `:7934`), so concurrent pulls clobber each other's terminal summariser and the raw config blob dumps to the terminal (cosmetic); and the relay firmware has a single global `pullSession` plus one `static uint16_t lastDeliveredSession` (`WCB.ino:3057-3074`), so genuinely overlapping pulls thrash reassembly on the board too. After the filter those become honest timeouts + retries instead of a wrong-board write, which is the right outcome — but the complete fix is a per-relay pull mutex (serialize the way `relayRouteAll` already does at `:530-537`), with the target filter as defence in depth.

<details><summary>Full verification reasoning</summary>

MECHANISM — every load-bearing fact re-derived from source, not the summary.

1. The listener really is unfiltered. `Wizard/app.js:7921` `const prefixBase = '[MGMT:CONFIG,'`; `:7938-7942` `onLine` returns only on `done` or a non-matching prefix, then sets `done = true` for ANY `[MGMT:CONFIG,*]` line. `:7946` `const prefix = line.slice(0, closeIdx + 1)` — I grepped the whole file: `prefix` (word-bounded) appears at 1929, 1930, 7946, and nowhere inside the 7938-8011 body except its own declaration. It is dead. The body then unconditionally does `boardConfigs[targetN] = config; boardBaselines[targetN] = JSON.parse(JSON.stringify(config)); populateUIFromConfig(targetN, config)` (`:7984-7991`) and toasts success (`:7993`).

2. Fan-out is confirmed: `BoardConnection._handleLine` does `this._dataCallbacks.forEach(cb => cb(line))` (`app.js:5313`), and `onData` is a plain `push` (`:5298`). `cleanup()` (`:7933-7936`) reassigns `_dataCallbacks` to a NEW filtered array, so the in-progress `forEach` (bound to the OLD array) still delivers the same line to every other armed listener. Two armed listeners therefore both consume the FIRST reply.

3. Only one emitter of the string exists: `Code/WCB/WCB.ino:3088` `Serial.printf("[MGMT:CONFIG,%d]%s\n", srcWCB, ...)`, where `srcWCB = pullSession.sourceWCB` = the responding board's `WCB_Number` (`:3019-3022`). And the request is hard target-addressed: `handleMgmtPullRequest` sets `pkt.targetWCB` (`WCB.ino:3538`) and `handleConfigReqPacket` returns unless `pkt.targetWCB == WCB_Number` (`WCB.ino:2974`). So the reply number always equals what was asked for — the "we don't know the number yet" rationale at `app.js:7919-7920` does not actually require accepting `<any>`.

4. The existing dedup guard does NOT cover this. `_pullingBoards` (`app.js:60`, checked at `:7904-7911`) is keyed by `targetN` only — it stops the duplicate boot-announce pull for the SAME board, not two different boards on the same relay stream. There is no per-relay lock except `_relayRouteAllBusy` (`:504`), which only guards `relayRouteAll`.

INTENT — the codebase already knows. `app.js:498-502` states it outright: "the pull's [MGMT:CONFIG,] listener isn't target-filtered — so overlapping pulls would cross-assign configs to the wrong board. Hence one-at-a-time." That is a documented hazard with a mitigation applied at exactly ONE call site. It is not a deliberate accepted trade-off; every other caller is unguarded. The sibling `[TERM:n]` path at `app.js:5383-5389` already does the correct source→slot mapping with a comment about panes "cross-contaminating" — the lesson was learned for TERM lines and not applied to CONFIG lines.

REACHABILITY — I found three paths, one of them deterministic:
- `app.js:563` renders a per-node "Manage via relay" button → `relayManageOne(n, nd.n)` with `pull=true` → `remoteBoardPull` (`:494`). Clicking two nodes on the relay card within the 6 s `PULL_TIMEOUT_MS` (`:7885`) arms two listeners on one stream. Same for the per-board Pull Config button (`:6767` → `boardPullRemote` → `:8058`).
- `app.js:1855-1868` (post-OTA reconciliation) loops `for (const bn of boards) { ...; remoteBoardPull(r, bn); }` with NO await. If two boards behind the same relay had ETM edges deferred during an OTA, this arms both listeners in the same tick — deterministic, not a race.
- The ETM path the reviewer cited (`:7716`) is real too. Firmware sends 3 boot announces 1200 ms apart (`WCB.ino:7423` `etmBootAnnouncesLeft = 3`; `:1023-1031`), and `WCB.ino:3771-3778` forces a "came ONLINE" print on every one. A board that reboots but isn't answering `?MGMT,PULL` yet keeps its listener armed across 3 attempts (~18 s of the ~23 s cycle, `:7884-7886`), which is plenty of window for a second board's edge during Push All (`boardGoAll` `:8291-8293` reboots each remote board in sequence).

EFFECT — the second listener writes the first board's config into `boardConfigs[targetN]`/`boardBaselines[targetN]` and the UI, marks `done`, so the real reply is never awaited and no retry fires; the user gets a green "Config pulled from WCBn" toast. A later push from that card uses `pushTargetWCB = boardConfigs[n]?.wcbNumber || n` (`:7862`), i.e. the OTHER board's number, so edits made on card 3 are sent to board 2 and board 3 is never configured.

SEVERITY — downgrading S1→S2. It is real, silent, and reachable from ordinary clicks, but the damage is Wizard-side model state: nothing is written to a board's NVS until the user pushes, the wrong WCB number is visible on the card, and a re-pull fully recovers. Device-side corruption needs a second user action (pushing from the mis-populated card). Borderline S1/S2; I land on S2 because no board is bricked, renumbered, or loses stored data on its own.

</details>

### `Wizard/app.js:7946` — CONFIRMED · S1 → **S2**

**remoteBoardPull accepts ANY [MGMT:CONFIG,<n>] reply — two overlapping pulls write one board's config onto another board's slot**

**Correction to the finding as filed:**

Severity should be S2, not S1. Correct the failure narrative: because the relay has a single `pullSession` (WCB.ino:3057-3067) and `lastDeliveredSession` dedup (WCB.ino:3053), overlapping pulls usually produce only ONE `[MGMT:CONFIG,n]` line — the un-answered slot is left permanently holding the other board's config with no retry (done=true + clearTimeout at app.js:7942-7943), rather than being transiently overwritten. Also correct the claimed impact: the wrong config is NOT pushed onto the wrong physical board — push (app.js:7862), relayed MGMT queries (app.js:11985) and OTA (app.js:1728) all resolve the destination from `boardConfigs[n].wcbNumber`, so the mis-assigned data is sent back to the board it came from; the damage is a card showing another board's settings, a poisoned baseline that blocks re-pull (app.js:527), and the user's intended edits landing on the wrong physical board. Add two symptoms the reviewer missed: the first listener's cleanup nulls `relayConn._lineTransform` (app.js:7934), so a still-live concurrent pull dumps a raw ~3 KB config blob into the terminal; and the worst unserialised caller is not the ETM listener but the OTA reconciliation loop at app.js:1856-1866, which fires `remoteBoardPull(r, bn)` for every deferred edge in a for-loop with no await — concurrent by construction, not by race.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived from source, every fact opened:

1. The listener really is unfiltered. Wizard/app.js:7921 `const prefixBase = `[MGMT:CONFIG,`;` and app.js:7939 `if (done || !line.startsWith(prefixBase)) return;`. app.js:7946 `const prefix = line.slice(0, closeIdx + 1);` — I grepped the whole closure body (awk over NR 7938-8012 for /prefix/): the only hits are `prefixBase` on 7939/7940 and this single `prefix` assignment on 7946, plus two unrelated comment words. `prefix` is genuinely dead. The store is unconditional against the closure's slot: app.js:7984-7985 `boardConfigs[targetN] = config; boardBaselines[targetN] = JSON.parse(...)`, then app.js:7991 `populateUIFromConfig(targetN, config)`.

2. Two listeners really do both fire on one line. Dispatch is app.js:5313 `if (!_isRcNoise) this._dataCallbacks.forEach(cb => cb(line));`. The first listener's `cleanup()` (app.js:7935) REASSIGNS a new filtered array — it does not splice the array forEach is walking. I verified this empirically with node: a cb1 that reassigns `obj.arr` still lets cb2 receive the same line. So listener #1 (target 2) consumes a `[MGMT:CONFIG,3]` line, sets `done = true`, `clearTimeout(timer)`, and writes WCB3's config into slot 2 — and listener #2 also gets it.

3. Nothing serializes pulls across targets. `_pullingBoards` (app.js:60, guard at :7906) is keyed by targetN only, so pulls for two different targets on the same relay connection are both live for up to PULL_TIMEOUT_MS = 6000 (app.js:7894).

4. The discriminator exists in the wire format. Code/WCB/WCB.ino:3088 `Serial.printf("[MGMT:CONFIG,%d]%s\n", srcWCB, ...)`, where srcWCB comes from `pullSession.sourceWCB` (WCB.ino:3084) which is stamped by the responding target as `frag.sourceWCB = WCB_Number` (WCB.ino:3020). And the target only answers its own number: WCB.ino:2974 `if (pkt.targetWCB != WCB_Number) return;`. So the reply's number always equals what the Wizard asked for at app.js:8035 `?MGMT,PULL,${pullWCBNum}`.

REACHABILITY — the ETM auto path is unguarded and is the common one:
- installEtmListener fires `remoteBoardPull(relayN, bn)` per ONLINE edge (app.js:7716) and per OFFLINE edge (app.js:7741) with no sequencing, gated only on `remoteRelayForBoard[bn] === relayN` (:7712/:7737) — true for every board armed by a prior relayRouteAll. Two boards announcing within 6 s on droid power-up is ordinary.
- The OTA reconciliation loop is worse: app.js:1856-1866 iterates `_suppressedEtmEdges` and calls `remoteBoardPull(r, bn)` in a bare for-loop with NO await — 2+ deferred edges = simultaneous pulls by construction.
- Other unserialised entries: :494 (relayManageOne), :1838, :5945 (modalRemoteConnect), :8058 (boardPullRemote), :8066 (boardRetryPull).

INTENT — the in-tree comment is evidence FOR the finding, not against it. app.js:498-502: "The relay reassembles one config reply at a time, and the pull's [MGMT:CONFIG,] listener isn't target-filtered — so overlapping pulls would cross-assign configs to the wrong board. Hence one-at-a-time…". `git log -L 496,506:Wizard/app.js` shows commit 69fdb59 added exactly that serialization — but ONLY inside relayRouteAll. The hazard is acknowledged and mitigated in one path; the auto-trigger paths were never covered. That is a gap, not a deliberate trade-off.

WHY S2 AND NOT S1 — I traced the blast radius and it is narrower than the title implies. Every downstream write resolves its destination from the (now wrong) config, not from the slot: push uses app.js:7862 `const pushTargetWCB = boardConfigs[n]?.wcbNumber || n;`, relayed MGMT queries use app.js:11985 `boardConfigs[n]?.wcbNumber || n`, and OTA uses app.js:1728 `const targetWcb = boardConfigs[n]?.wcbNumber ?? n;` with the chip family from the same config (app.js:1734-1736, plus the firmware BEGIN guard). So board A's config is never flashed onto board B, and an OTA launched from the corrupted slot still sends a family-correct image to the board the config actually came from. The real damage is: one card silently displays another board's settings with `boardBaselines` set (so relayRouteAll's `!boardBaselines[n]` filter at app.js:527 won't re-pull it), that slot's listener set `done=true` and cleared its timer so it never retries, and the user's intended edits for board B land on board A. Real, silently persistent, reachable by timing — but in-memory browser state with a visible tell (two cards showing the same WCB Number, and the general-mismatch modal at app.js:7977 may fire) and one Pull Config click to recover.

CORRECTIONS TO THE FINDING'S MECHANISM: the failure narrative assumes both configs come back. They usually don't — the relay has ONE reassembly buffer (`memset(&pullSession, 0, ...)` on sessionId change, WCB.ino:3057-3067) plus `lastDeliveredSession` suppression (WCB.ino:3053-3055), so overlapping requests thrash and typically only one `[MGMT:CONFIG,x]` line is ever emitted. That makes it worse, not better: the un-answered slot is left holding the other board's config permanently. Second missed symptom: the first listener's cleanup sets `relayConn._lineTransform = null` (app.js:7934), so a still-live second pull loses the blob-summariser installed at app.js:7926 and dumps the raw ~3 KB config line into the terminal.

</details>

### `Wizard/app.js:8419` — CONFIRMED · S2 · ⚠ PROPOSED FIX IS WRONG

**exportSystemFile only walks slots 1..wcbQuantity — any board discovered or relayed at a higher number is silently dropped from the exported file**

**Correction to the finding as filed:**

The defect stands at Wizard/app.js:8419 with severity S2, but the mechanism should be stated as "any board numbered above the WCBQ floor", not "discovered or relayed at a higher number" — the firmware's own ?WCBQ help (WCB_Help.cpp:386-394) recommends keeping WCBQ small and letting WDP auto-join cover the rest, so boards above the floor are the recommended configuration, and the loss hits a plain 4-board droid with ?WCBQ,1 just as hard as the invented WCB19 case. Also note the finding's own WCB19 example does not actually reproduce: Greg's WCB19 is the MgmtRelay, which `_relaySlots` excludes from the grid regardless (app.js:437, :6844).

Correct fix — iterate `desiredBoardNumbers()`, not `Object.keys(boardConfigs)`:

  for (const n of desiredBoardNumbers()) {
    if (!boardConfigs[n]) continue;
    if (boardConfigs[n].type === 'client') continue;   // clients run their own sketch
    ... existing body ...
  }

`desiredBoardNumbers()` (app.js:431-438) is exactly the set of RENDERED sections — `reconcileBoardGrid` adds every member and removes every non-member (:452-459) — so the DOM-sync helpers can never run against a missing section, which is what makes the `Object.keys` version destructive. It also already filters `_relaySlots`, so the relay is excluded for free (and that fixes a pre-existing bug: a relay whose id happens to be <= qty is exported today as a bogus board block).

Do NOT auto-raise `systemConfig.general.wcbQuantity` — that value is pushed to the whole fleet on restore (parser.js:1200, :1266) and inflating it to 19 exhausts the 20-peer ESP-NOW table (WCB.ino:7349-7384, :7395). Fix the import side instead: after `renderBoards(system.general.wcbQuantity)` at app.js:8383, render sections for the file's actual board numbers before `populateUIFromConfig` — but via a mechanism that does not pollute `_meshBoards` (which means "heard on the mesh"), e.g. a direct `addBoardSection(n)` for numbers not already rendered. And fix parser.js:1188-1189 to raise the quantity toward the file's maximum board NUMBER rather than the board COUNT if that field is to stay meaningful at all.

Worth flagging separately (out of scope for this finding but adjacent): a discovered-but-never-pulled board already gets `boardConfigs[n] = createDefaultBoardConfig()` at :617, so today's export can already write a fabricated `[WCBn]` for any such slot inside 1..qty. Gating the export body on `boardBaselines[n]` (the "was actually pulled" marker, cf. :527) would close that too.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived, every number checked against source.

`exportSystemFile` (Wizard/app.js:8405) does exactly what the finding transcribes. I read 8405-8444: `const qty = systemConfig.general.wcbQuantity;` (:8418, taken from `g-wcbq` at :8406) then `for (let n = 1; n <= qty; n++) { if (boardConfigs[n]) { … systemConfig.boards.push(boardConfigs[n]); } }` (:8419-8433). `buildSystemFile` (Wizard/parser.js:1548) then emits one `[WCB<n>]` per entry of `system.boards` (:1579-1584) — it faithfully writes whatever it is handed, no bounds check of its own. The success toast at :8443 is unconditional. No warning path exists.

The grid is genuinely sparse and genuinely unbounded by WCBQ:
- `desiredBoardNumbers()` (app.js:431-438) = `1.._boardFloor` ∪ `_meshBoards` ∪ live connections, filtered only to `n >= 1 && n <= WCB_MAX && !_relaySlots.has(n)`. `WCB_MAX = 20` (app.js:1081), verified by reading the declaration.
- `reconcileBoardGrid`'s own comment (app.js:440-442) says "Sparse-aware (sections are keyed by number, so gaps like {1,2,20} are fine)".
- `_boardFloor` is written in exactly one place — `renderBoards` (app.js:469) — and `renderBoards` has only 6 callers (:313, :1125 WCBQ dropdown, :6867 first-pull, :8383 file load, :10888 wizard apply). None of them are discovery.
- `addDiscoveredBoards` (app.js:587-594) adds any 1..20 to `_meshBoards` and raises neither `_boardFloor` nor `g-wcbq`. Its callers are `relayManageOne` (:492), `upsertClientCard` (:12548), and the WDP discovery sweep (:12642).
- `boardConfigs[targetN] = config` for a relayed pull is at app.js:7984 (I counted the lines from the 7940 anchor — the finding's cite is exact). The direct-USB migrate path writes `boardConfigs[detected]` after the `detected >= 2 && detected <= WCB_MAX` test at :6869.

Nothing raises `g-wcbq` from discovery. I grepped every `g-wcbq` hit (12 in app.js, 1 in index.html): it is written only by `syncGeneralFromConfig` (:8077 — and that runs only on the FIRST pull, gated by `if (!generalBaseline)` at :6853/:7971), the wizard (:10886), and the user's own dropdown (:1117). `wcbQuantity` is not in `extractGeneralFields` (app.js:1412-1431), so a later board reporting a different WCBQ does not even raise the general-mismatch modal. `renderBoards(config.wcbQuantity)` at :6867 raises `_boardFloor` but not the DOM field the exporter reads.

REACHABILITY — stronger than the finding argues, and it does not depend on the invented "spare board 19".

WCBQ is a FLOOR, not a total, and the firmware's own help actively tells the user to keep it small. `saveWCBQuantityPreferences` accepts 1..`MAX_WCB_COUNT` = 20 (WCB_Storage.cpp:437-438, WCB_Storage.h:12). `rebuildActivePeers` computes `wcbPeerActive[] = {1..Default_WCB_Quantity} ∪ learned` (WCB.ino:6854-6862, `bool floorMember = (i < Default_WCB_Quantity)`), and the design comment at WCB.ino:456-466 spells the union out. `?WCBQ?` help (WCB_Help.cpp:386-390, :394) literally reads: "Sets the baseline (FLOOR) number of WCB boards … boards heard on the mesh beyond this floor are added automatically, so WCBQ no longer has to cover every board" with the example "?WCBQ,1 - Single board (rely on auto-join for the rest)". A user who follows that advice and then pulls board 1 lands on `g-wcbq = 1` — and Export writes exactly one `[WCB1]` block for a four-board droid whose other three cards are on screen, pulled, and editable. That is the documented-recommended configuration, not an edge case.

Greg's actual WCB19 is the MgmtRelay itself (app.js:6813 comment "This fixes the duplicate 'WCB 19'"), which `_relaySlots` keeps out of the grid anyway — so the finding's specific narrative is weaker than it claims. The general mechanism is not.

INTENT — not a documented trade-off. `git log -L 8405,8444:Wizard/app.js` shows the 1..qty loop is original (f15433a "bug fixes", unchanged through d4dd5d4); it predates `_boardFloor`/`_meshBoards`/`desiredBoardNumbers` and the relay feature entirely. No comment defends it. The rest of the file layer shares the same stale contiguous assumption: `parseSystemFile` raises `general.wcbQuantity` to `system.boards.length` — the COUNT, not the max number (parser.js:1188-1189) — so a sparse file re-imports with sections 1..count and `boardConfigs[19]` orphaned with no DOM (app.js:8383-8390). The asymmetry is real in both directions.

Per the review's own rubric (docs/CODE_REVIEW_2026-08.md:10-11), silent omission from a user's only backup file, in a firmware-recommended configuration, with a green success toast, is "wrong behaviour in a real scenario" = S2. It is not S1: nothing crashes and the physical board keeps its own NVS config.

FIX CHECK — the proposed fix is wrong in all three of its parts.

(a) `Object.keys(boardConfigs)` is the wrong iteration set. `addBoardSection` (:616-618) seeds `boardConfigs[n] = createDefaultBoardConfig()` for every merely-DISCOVERED board, and `upsertClientCard` (:12548-12552) does the same for WCB_Clients (NaviCore lands at id 20). Iterating all keys exports fabricated default `[WCBn]` blocks for boards nobody ever pulled — restoring from that file pushes factory defaults over a real board's config. That is worse than dropping it.

(b) Worse, it can run the DOM-sync helpers against a slot with no rendered section. `syncSerialUIToConfig` (app.js:2904-2921) writes `parseInt(getElementById(...)?.value) || 9600`, `?? true`, `?? ''` into all five ports; `getSequencesFromUI` (:4487-4497) and `getVariablesFromUI` (:4894-4910) return `[]` when the tbody is missing. That state is exactly what an imported sparse file produces (boardConfigs[19] set at :8387, no section because `renderBoards` only built 1..count). So the "fix" would silently wipe the baud rates, sequences and variables of the very board it was written to rescue. docs/CODE_REVIEW_2026-08_verdicts.md:890 already documents this same `syncSerialUIToConfig(19)`-against-missing-DOM hazard independently.

(c) "Raise `wcbQuantity` to at least the highest exported board number" is actively harmful. `applyGeneralToBoard` copies general WCBQ onto every board (parser.js:1200) and `buildCommandString` emits `WCBQ,<n>` (parser.js:1266-1267), so the inflated value is pushed to the whole fleet on restore. Boot registers one ESP-NOW unicast peer per floor id (WCB.ino:7349-7362): WCBQ=19 on WCB1 registers ids 2..19 = 18 peers, plus the controller peer at id 20 (:7367-7384, added separately because 20 > 19), plus the broadcast peer (:7395) = 20, exactly the 20-peer ESP-NOW cap — leaving zero headroom, so any subsequent `addActivePeer`/`addTemporaryPeer` (a MgmtRelay adopting as a temporary peer, for instance) fails. It also inverts the semantic the firmware help is at pains to teach. (One thing the reviewer would have gotten away with: broadcasts would NOT wedge on the phantom floor members — `etmAddToPendingTable` skips `if (!boardTable[b].online) continue;` at WCB.ino:1204. So the ETM cost is not the problem; the peer-table exhaustion and the semantic inversion are.)

</details>

### `Wizard/app.js:11701` — CONFIRMED · S2 · ⚠ PROPOSED FIX IS WRONG

**A stale boardBaselines[n] satisfies the watcher's readiness gate — the push starts ~500 ms after port open and races the scheduled pull**

**Correction to the finding as filed:**

The finding is correct on mechanism, line and severity. One correction to the reachability framing and one to the fix.

Reachability: the reviewer's "Same on the wizard's own 🔌 Reconnect — wizardManualConnect calls boardDisconnect, which preserves the baseline" is right, but it understates the dominant path. On the recommended 🔍 Detect flow, `boardDisconnect` is never called at all: `wizPortDetect` calls `boardConnections[n].closeForReconnect()` (Wizard/app.js:11473-11475) — with the explicit comment at :11242 "We do NOT call boardDisconnect() here because wizPortComplete() handles the full reconnect" — and that sets `_connected = false` (:4977), so wizPortComplete's `if (boardConnections[n]?.isConnected()) await boardDisconnect(n)` (:11584) is skipped.

Fix: put the clear where every wizard path passes, in `wizPortComplete` immediately after `establishConnection` (:11585-11586, next to `delete remoteRelayForBoard[n]`):

    delete boardBaselines[n];   // gate at :11701 must mean "the post-connect pull finished",
                                // not "some earlier pull left a value here"

Nothing is lost by that: :11712 discards the value anyway to force a full push.

The second half of the proposed fix ("hold the scheduled verify boardPull until the push completes") is unnecessary once the baseline is cleared — the gate then waits for the t=3000 pull to finish, which is the designed ordering. Adding it is harmless but is not what makes the fix work.

Two adjacent leaks worth fixing in the same commit, both stale-baseline of the same family: `reconcileBoardGrid` (:452-457) deletes `boardConfigs[n]` for a yanked slot but not `boardBaselines[n]`; and `mode: 'erase'` (:7305) fires `?ERASE,NVS` unacknowledged, so the same premature start silently skips the erase.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived, every claim checked against source.

1. The gate is exactly as quoted. `Wizard/app.js:11696-11701`: `setInterval(async () => { attempts++; ... if (boardConnections[n]?.isConnected() && boardBaselines[n]) {` on a 500 ms interval (`:11758`). The comment at `:11698-11700` states the intended invariant verbatim ("boardBaselines[n] being non-null means boardPull finished — the board is fully booted and responsive").

2. Nothing clears `boardBaselines[n]` on a disconnect/reconnect. I grepped every reference (44 hits). `boardDisconnect` (`:6196-6219`) deletes `boardConnections[n]` (`:6210`) and `remoteRelayForBoard[n]` (`:6211`) — no baseline touch. `establishConnection` (`:5634-5730`) never touches it. `openWizard` (`:9392-9403`) clears watchers and rebuilds `wizardState` only. `wizardApplyConfig` (`:10882-11010`) writes `boardConfigs[n] = cfg` (`:11005`) but not the baseline. `reconcileBoardGrid` (`:452-457`) deletes `boardConfigs[n]` but leaves `boardBaselines[n]`. The only deletes are in boardPull's relay/auto-migration branches (`:6832`, `:6893`) and the `= null` forcing sites.

3. Ordering is as claimed. `wizPortComplete` wizard branch: `await establishConnection` (`:11585`) → `setTimeout(() => boardPull(n), 3000)` (`:11589`) → `wizardWatchForConnect(...)` (`:11590`). With a stale baseline the gate is true at tick 1 (t≈500 ms), i.e. 2.5 s before the pull that was supposed to establish readiness ever runs.

4. The board is genuinely mid-boot at t=500 ms — I verified this from both sides. Browser side: `BoardConnection.connect` asserts DTR on open and the comment at `:4964-4967` says "The RC differentiator fires a one-shot reset". On the 🔍 Detect path the user has just pressed the physical reset button (that is how `DETECT_RE` matches). Firmware side: `Code/WCB/WCB.ino:7120-7124` — `Serial.setRxBufferSize(2048); Serial.begin(115200); delay(1000); while (Serial.available()) Serial.read();  // 🔥 flush startup junk`. Anything sent in that first ~1.2-1.5 s (ROM boot + the 1 s USB-stabilise delay) is **explicitly discarded**; setup() then does another `delay(1000)` (`:7184`), NeoPixel init, WiFi/ESP-NOW init and the UART inits before loop() can parse anything.

5. The dropped commands are silently reported as ACKed. `sendAndAwaitIdle` (`:5267-5295`) resolves `ok: lines.length > 0` — the ESP32 ROM/boot banner supplies lines, so `ok === true` even though the parser never saw the command. `_pushFullyAcked` (`:7474`) therefore stays true and `updateBoardStatusBadge(n, 'configured')` fires (`:7570`), and the watcher lands on `wizardSetConnectStatus(n,'ok','✓ Done')` (`:11748`).

6. What actually gets lost is the worst possible slice. `WCBParser.buildCommandString` (`Wizard/parser.js:1220-1295`) emits, in order: `HW`, `LED,PIN`, `WCB,<num>`, `ALIAS`, `WCBQ`, `WCBCH`, `CONTROLLER`, `MAC,2`, `MAC,3`, `EPASS`, `DELIM`… At ~250-400 ms per command (quiet window 250 ms) the first 2-4 — board identity and hardware revision — land inside the flush window. The push IS a full push (`:11712` sets `boardBaselines[n] = null`), so the payload is right; only the head of it is eaten.

7. The second half of the race is real too. At t=3000 the scheduled `boardPull(n)` runs; the connection is still open on the configure path, `_boardPullInFlight` (`:6763`) only dedups pulls against other pulls — there is no push/pull mutex. It sends `WCB_WEBTOOL_CONFIG_PULL` (`:6800`) into the middle of the push, and the resulting backup dump both keeps every concurrent `sendAndAwaitIdle` quiet-timer alive to its 5 s hard cap and, on success, overwrites `boardConfigs[n]`/`boardBaselines[n]` from the half-configured board (`:6916-6917`) and repopulates the UI mid-push.

REACHABILITY — a real user can produce this trivially.
`wizardWatchForConnect` has exactly one caller (`:11590`), in the wizard context of `wizPortComplete`. Two everyday paths arrive with a stale baseline:
 (a) Board connected + pulled on the config page (non-wizard branch `:11614` sets the baseline), then the user opens the Wizard. `openWizard` does not clear it. This is the high-impact case: the wizard exists to (re)assign `?WCB,<num>` / `?HW`, and those are precisely the commands dropped.
 (b) The wizard's own 🔌 Reconnect button (`:10347` → `wizardManualConnect` `:11675`) after ✓ Done — the verify pull at `:11735` guarantees the baseline is set. Lower blast radius (board is already correct) but always triggers.
The flash/update/factory modes are NOT affected (esptool does its own reset, and the t=3000 pull no-ops because `closeForReconnect` at `:7065` makes `isConnected()` false). `mode: 'erase'` IS affected differently: `conn.send('?ERASE,NVS')` at `:7305` is fire-and-forget into a booting board, so the erase can silently not happen.

INTENT — not a deliberate trade-off. `git log -L 11688,11760` shows the last touch (6704593, "update to fix mac os flashing") only widened the reconnect/verify timings; the gate and its comment are unchanged and the comment asserts the invariant that the code does not maintain. This is a plain invariant break, not a documented compromise.

SEVERITY — I keep S2. It is not a brick or data loss, but the Wizard is the primary user-facing onboarding tool, the failure is silent (✓ Done, badge 'configured'), and it lands on board identity/mesh-network settings whose corruption is exactly the class of thing CLAUDE.md rule 4 warns about (fleet-wide consistency). The verify pull afterwards replaces `boardConfigs[n]` with the board's real state (`:6917`), so no pending diff is left to reveal it.

</details>

### `Wizard/parser.js:1355` — CONFIRMED · S2 · ⚠ PROPOSED FIX IS WRONG

**`?KYBER,CLEAR` is sent with no port, so the firmware resets Serial 2 to 9600 and re-enables its broadcast — undoing settings from earlier in the same push**

**Correction to the finding as filed:**

Three corrections, one of which kills the proposed fix.

(1) WRONG LOCATION / BLAME. The defect is in the firmware, not `Wizard/parser.js:1355`. The bare `?KYBER,CLEAR` the Wizard emits is the *only* spelling that correctly zeroes the Kyber target table (`WCB_Storage.cpp:1084-1089`); the comma form does not. The bug is `kyberPort = 2` hardcoded at `WCB_Storage.cpp:1082` plus the clear branch acting on it at `:1191-1209` while the real port is sitting in `kyberLocalPort`, zeroed one line earlier at `:1188`. File it against `Code/WCB/WCB_Storage.cpp:1082`.

(2) NOT WIZARD-ONLY. The firmware's own backup/restore chain has the identical ordering: `emit("BAUD,S…")` at `WCB.ino:2804`, `emit("BCAST,OUT/IN,S…")` at `:2811-2814`, `emit("KYBER,CLEAR", true)` at `:2859`. So a board restoring its own `?BACKUP` chain also drops S2 to 9600. And the repo's own bench fixture already contains the exact sequence: `TestConfigs/WCB1_01_base.txt:64` pastes `…?BAUD,S2,57600…?KYBER,CLEAR…` on the board whose S2 is documented as the 57600 Kyber Maestro port (`:40`).

(3) THE STATED EXAMPLE IS HALF WRONG. "a WLED … wired there" would self-heal — every subsystem emitter runs *after* Kyber in the same push (MP3 `parser.js:1360`, HCR `:1378`, DFP `:1395`, WLED `:1413`, MAESTRO `:1457`, PWM `:1500`) and each re-applies its port's baud and broadcast flags (`WCB_WLED.cpp:165,174`, `WCB_MP3.cpp:342,352`, `WCB_HCR.cpp:495,507`, `WCB_Maestro.cpp:660,672`). Residual exposure is a *plain* device on S2 configured only via `?BAUD`/`?BCAST` (the Marcduino/Kyber-Maestro case), or a user-disabled broadcast flag. It is also a no-op on a default board (9600 / broadcast on — `WCB.ino:629`, `:600`), and it self-heals on the next delta push (post-pull, `baseline.kyber.mode === config.kyber.mode` so `kyberChanged` is false and no CLEAR is emitted).

MISSING HALF worth adding: the mirror case. When Kyber genuinely was LOCAL on S3, `?KYBER,CLEAR` leaves S3 stuck at 115200 with broadcast blocked (set at `:1163-1177`) forever, while molesting S2.

CORRECT FIX (firmware): in the clear branch capture `int clearPort = (kyberPort >= 1 && kyberPort <= 5) ? kyberPort : kyberLocalPort;` *before* `kyberLocalPort = 0` at `:1188`, skip the baud/broadcast restoration when it is 0, and range-guard `[clearPort-1]` the way `:1162` already does. Leave the Wizard emitting bare `KYBER,CLEAR`.

DUPLICATE: this is already written up as F-065 in `docs/CODE_REVIEW_2026-08_wave2.md:721` (that doc is itself flagged ALL UNVERIFIED at `:6`). Don't double-count.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived, every number checked by hand:

- `Wizard/parser.js:1355` is exactly `add('KYBER,CLEAR');`, in the `else` of `mode === 'local'` / `'remote'` (`:1346`, `:1351`). Default is `mode: 'none'` at `parser.js:65`, and `parseBackupString` sets `mode='none'; port=null` for a board with no Kyber (`:640-641`). `kyberChanged` is `fullPush || !baseline || …` (`:1340`), so on a full push it always fires.
- ORDER confirmed by reading the function top-to-bottom: BAUD loop `:1310-1315`, BCAST loop `:1326-1333`, S0 `:1335-1336`, Kyber `:1339-1357`. `add()` is `cmds.push(lfi + cmd)` (`:1229`) and the function returns `cmds.join(delim)` (`:1540`) — no reordering, no dedup. So `?BAUD,S2,x` and `?BCAST,*,S2,*` are executed before `?KYBER,CLEAR`.
- DISPATCH: `WCB.ino:4650-4658` — `rootUpper == "KYBER"` and `argsUpper.startsWith("CLEAR")` → `storeKyberSettings(args)` with `args == "CLEAR"`. Also `WCB.ino:5239` `storeKyberSettings("clear")`.
- `WCB_Storage.cpp:1073` `firstComma = message.indexOf(',')` → -1 → `:1078-1082` sets `kyberUseTargeting=false; kyberPort = 2;` (hardcoded, verified char-for-char) and zeroes all targets `:1084-1089`.
- Clear branch `:1184-1212`: `:1188 kyberLocalPort = 0` (destroys the real port before it is ever used), `:1191-1193 if (kyberPort > 0) updateBaudRate(kyberPort, 9600)`, `:1200-1204` `serialBroadcastEnabled[kyberPort-1] = true` + `saveBroadcastSettingsToPreferences()`, `:1205-1209` `blockBroadcastFrom[kyberPort-1] = false` + `saveBroadcastBlockSettings()`. With `kyberPort==2` those are index 1 = S2.
- PERSISTENCE verified, not just runtime: `updateBaudRate` (`WCB_Storage.cpp:146-181`) calls `applyLiveBaud(port,…)` (`:165`), writes `baudRates[port-1]` (`:170`), then `preferences.begin("serial_baud", false); putInt("Serial2", 9600)`. Key `"Serial2"` is 7 chars — under the 15-char NVS limit, fine. The two broadcast saves persist as well.
- Array bounds: `bool serialBroadcastEnabled[5]` (`WCB.ino:629`), `bool blockBroadcastFrom[5]` (`WCB.ino:600`) — index 1 is in range, so no OOB on this path (the OOB variant needs `?KYBER,CLEAR,S0`, a separate finding).

REACHABILITY — a real user can produce this:
- `Wizard/app.js:7417-7418` and `:7810-7811`: `const fullPush = !boardBaselines[n]`.
- `app.js:7341` and `:7357`: `boardBaselines[n] = null;   // force full push — board NVS is blank`, set immediately after the NVS erase and before `boardGo(n, {mode:'configure'})` — i.e. exactly the erase-then-push flow the finding names, with `preEraseConfigSnapshot` restored into `boardConfigs[n]` so the user's 57600/broadcast-off S2 is re-pushed and then clobbered.
- `parser.js:1573` and `:1582`: `buildSystemFile` calls `buildCommandString(board, null, true, FILE_OPTS)` — so every exported `WCB_system.txt` also carries `?KYBER,CLEAR` after its `?BAUD`/`?BCAST` lines.
- Firmware side: `WCB.ino:2804/2811-2814` then `:2859` — same ordering in `collectConfigCommands`, `includeInLive=true`.
- Bench evidence in-repo: `TestConfigs/WCB1_01_base.txt:64` is a single paste containing `?BAUD,S2,57600 … ?BCAST,RESET … ?KYBER,CLEAR`; `:40` documents S2 as the 57600 Kyber Maestro port. That fixture leaves S2 at 9600.

INTENT — no comment anywhere defends `kyberPort = 2`. `:1082` is bare. The surrounding comments (`:1149-1152` "Don't seize a UART another subsystem already owns", `:162-166` on the in-memory baud sync existing to stop a re-push loop) argue the opposite way. No doc describes CLEAR as S2-specific: `WCB_Help.cpp:228` says only "Disable Kyber". `git log` on `WCB_Storage.cpp` shows no commit touching this region deliberately.

FIX CHECK — the proposed fix fails on its own terms:
1. `add(\`KYBER,CLEAR,S${config.kyber.port || 2}\`)`: in the finding's own scenario `mode === 'none'`, and `parser.js:66` / `:641` set `kyber.port = null`, so `|| 2` evaluates to **2**. The emitted command is `?KYBER,CLEAR,S2` — the identical port, the identical clobber. It does not fix the reported failure at all.
2. Worse, it switches the firmware to the comma branch: `:1096` `params.startsWith("S")`, `:1102-1103` `portStr="2"; params=""`, `:1105-1106` `kyberPort=2; kyberUseTargeting = true`. The guard at `:1112` only applies to `local`. Then `:1118 if (kyberUseTargeting && params.length() == 0)` is TRUE, so `:1120-1132` rebuilds an **enabled** `kyberTargets[]` entry for every configured Maestro, and the clear branch persists that table via `saveKyberTargets()` at `:1210` (`:2078-2089` writes `kt<i>_en` for all 9 slots). The bare form zeroes id/wcb/port/enabled at `:1084-1089`; the comma form does not. So the fix converts a correct clear into a clear that leaves stale enabled targets in NVS. (This is the already-written-up F-064, `docs/CODE_REVIEW_2026-08_wave2.md:697`.)
3. The "better" alternative — gate on `baseline?.kyber?.mode` — breaks precisely the paths that matter, because `fullPush` is defined as `baseline === null` (`app.js:7417`) and `buildSystemFile` passes `null` explicitly (`parser.js:1573`, `:1582`). The guard would suppress `KYBER,CLEAR` on every full push and in every exported system file, so restoring a file onto a board that has Kyber configured would silently leave it configured.

</details>

### `Wizard/parser.js:1531` — CONFIRMED · S1 → **S2**

**Stored sequences are pushed AFTER the PWM mapping command that reboots the board — they are silently discarded**

**Correction to the finding as filed:**

Facts to correct in the write-up: the Stored Sequences block is parser.js:1524-1538 (1540-1541 are `return`/close brace); the `autoReboot` tail is WCB_PWM.cpp:339-343, not 338-342; the firmware's sequences block is WCB.ino:2895-2913 and the "mappings LAST" comment is WCB.ino:2916, not :2887/:2907; the send call is app.js:7483. The reviewer's hedge "and the Variables/Maestro/ETM blocks if any are still after it" is moot — Variables (1428-1443), Maestros (1445-1481) and ETM (1483-1497) are all already ahead of the PWM/Mappings blocks. The parenthetical claim that the mappings diff fires "on essentially every save after a pull" overstates it: `boardGo` never calls `syncMappingsToConfig`, so the `bidir`-key drift only makes `mappingsChanged` true after the user edits something in the Mappings UI (onMappingChange/onBidirChange/saveMappingRow); the always-reachable trigger is the full push, most importantly the post-flash restore that sets `boardBaselines[n] = null` at app.js:7186.

The fix should also sort the Mappings loop so PWM-type entries are emitted after Serial-type ones — a UI-authored config keeps DOM row order (app.js:3599-3618), so a Serial mapping listed below a PWM row is stranded by the same reboot even after the sequences move. And note what the reorder cannot fix: two or more PWM input mappings still lose all but the first, because each one reboots (already logged as docs/CODE_REVIEW_2026-08_wave2.md:437). The durable fix is firmware-side — have WCB.ino:4542 call `addPWMMapping(pwmConfig, false)` and set a deferred-restart flag honoured once the command queue drains, which is plainly what the `autoReboot` parameter was added for.

<details><summary>Full verification reasoning</summary>

Mechanism re-derived from source, every step:

1. ORDER IN THE EMITTER. Read `Wizard/parser.js:1220-1541` in full. The last three blocks are, in order: `── PWM Output Ports ──` (1499-1506, `add(\`MAP,PWM,OUT,S${port}\`)` at :1504), `── Mappings ──` (1508-1522, `add(cmd)` at :1520 where cmd = `MAP,${m.type.toUpperCase()},S${m.sourcePort},…`), then `── Stored Sequences ──` (1524-1538, `add(\`SEQ,SAVE,${seq.key},${seq.value}\`)` at :1531, `SEQ,CLEAR` at :1536). Nothing else follows; `return cmds.join(delim)` is :1540. Mapping type values are `'PWM'` / `'Serial'` (parser.js:945 pushes `{type,…}`, consumed at parser.js:1044/1071), so a PWM row emits literally `?MAP,PWM,S3,S5`.

2. THE REBOOT IS REAL AND UNCONDITIONAL. `Code/WCB/WCB.ino:4510-4542`: the `?MAP,PWM,…` handler falls through the LIST / CLEAR,ALL / CLEAR,OUT,S / CLEAR,S / OUT,S branches to `addPWMMapping(pwmConfig)` at :4542 with no second argument. Declaration `Code/WCB/WCB_PWM.h:38  void addPWMMapping(const String &config, bool autoReboot = true);` — default true, and no caller anywhere passes false (`grep addPWMMapping` → WCB.ino:4542, WCB.ino:5254, both one-arg). Tail of the definition is `WCB_PWM.cpp:339-343` (reviewer said 338-342, off by one): `if (autoReboot) { Serial.println("Rebooting in 3 seconds…"); delay(3000); ESP.restart(); }`. Verified the sibling `addPWMOutputPort` (`WCB_PWM.cpp:791-820`) does NOT reboot, so the reviewer is right that only the input-mapping block is hazardous.

3. THE PUSH LOOP WALKS INTO IT. `Wizard/app.js:7469-7495`: the string is split on `delim+funcChar` and each command sent individually via `conn.sendAndAwaitIdle(cmd, {quietMs:250, hardTimeoutMs:5000})` (:7483). `sendAndAwaitIdle` (app.js:5267-5294) resolves `{ok: lines.length > 0}` after 250 ms of silence following the last line. The board prints "Rebooting in 3 seconds…" and then goes silent inside `delay(3000)`, so the Wizard resolves ~250 ms in and immediately writes the next command into a board that has ~2.75 s of `delay()` left and then calls `ESP.restart()` — the RX ring buffer and the enqueued command die with the restart. Worse, `ok` is `lines.length > 0`, and the board's boot banner arrives inside that command's 5 s hard window, so the dropped `?SEQ,SAVE` is scored as ACKed (`_pushFullyAcked` stays true). No code in the loop reorders or treats a mapping as terminal — `commandStringNeedsReboot` (app.js:8185-8196, `/MAP,PWM,S\d/i` at :8194, with a comment that already says "firmware auto-reboots") is only consulted AFTER the whole loop, at :7496.

4. THE FIRMWARE ITSELF DOCUMENTS THE CONSTRAINT THE WIZARD BREAKS. `collectConfigCommands` — declared "SINGLE SOURCE OF TRUTH for the WCB config command list" (WCB.ino:2749) — emits `SEQ,SAVE` at :2910 (block starts at the `// Stored sequences` comment, :2895) and only then, at :2916, `// PWM output ports + mappings (mappings LAST — restore triggers a reboot)` with `MAP,PWM,OUT` at :2918 and `MAP,PWM,S…` at :2919-2929. Serial mappings are emitted far earlier (:2816-2830). So the canonical order is sequences-then-mappings and the comment states exactly why. parser.js inverts it. (Reviewer cited :2887/:2907 for these; actual are :2895/:2916.)

5. REACHABILITY — a real user can produce this. (a) Full push: `fullPush = !boardBaselines[n]` (app.js:7417) emits every mapping AND every sequence. The flash→restore path sets `boardBaselines[n] = null` explicitly at app.js:7186 and then calls `boardGo(n,{mode:'configure'})` at :7189 — so every post-flash config restore of a board that has a PWM input mapping drops the entire sequence library. (b) Delta push: `mappingsChanged` at :1509-1510 is a `JSON.stringify` compare, and `syncMappingsToConfig` (app.js:3614) pushes `{type, sourcePort, rawMode, bidir, destinations}` while the parser (parser.js:945) pushes `{type, sourcePort, rawMode, destinations}` — the extra `bidir` key makes the compare permanently unequal once the user touches any mapping control. I checked the reviewer's stronger claim that this "fires on essentially every save after a pull": that is NOT quite right — `boardGo` does not call `syncMappingsToConfig` (app.js:7387-7396 syncs serial/maestro/kyber/mp3/hcr/dfp/wled only), `appendMappingRow` (:3473) doesn't call it, and `detectBidirMappings` (:3732-3767) only ticks checkboxes. It fires from `onMappingChange`/`onBidirChange`/`saveMappingRow` (:3571-3581, :3827), i.e. after any user edit in the Mappings UI. Still easily reachable, just not automatic.

6. INTENT — no comment, blame or doc defends the current order. `git blame Wizard/parser.js` shows the mappings block (3a846203, 2026-02-27) predates the sequences block being made a per-key diff (978ecf46, 2026-03-03); the sequences `add()` itself is from the same original commit. Nothing in docs/ or CLAUDE.md sanctions it, and the firmware comment at WCB.ino:2916 argues the opposite. Related prior art: docs/CODE_REVIEW_2026-08_wave2.md:437-457 already logs the firmware-side half of this (addPWMMapping always reboots, so 2+ PWM mappings in one chain lose all but the first) and quotes the same :2916 comment.

Severity: I am downgrading S1→S2. It is genuine silent data loss reported as success, but (i) it needs the board to actually use PWM input passthrough, (ii) the only commands stranded behind the reboot are the stored sequences (serial mappings sort ahead of PWM ones in any pulled config, per WCB.ino:2816 vs :2919), (iii) the source data still lives in the UI/backup file on a plain flash and a re-push after the reboot succeeds (mappings now match the baseline, so no `MAP,PWM` is re-emitted), and (iv) the post-push verify pull (app.js:7538/:7580) makes the absence visible on the very next pull rather than baking in a wrong-looking value. It is worse than a nuisance and clearly worth fixing before release, but it is not in the same tier as the two peer S1s (a flash that dead-ends leaving a board unconfigured, or a sequence silently truncated to a corrupt value in NVS).

</details>

### `Wizard/serial-hub.js:114` — CONFIRMED · S2 · ⚠ PROPOSED FIX IS WRONG

**`_leaderPortOpen` latches true forever — `portOpen` lies, so the Wizard shows "Connected" on a dead share and drops every send**

**Correction to the finding as filed:**

Mechanism correction: `_leaderPortOpen` does NOT "latch true forever" in general — serial-hub.js:500 clears it whenever a live leader announces `portOpen:false` while we are a follower. The real defect is narrower and sharper: the clearing site is gated by `if (this._role !== 'leader')` (serial-hub.js:499) and BroadcastChannel never self-delivers, so **the moment a tab is promoted to leader, `_leaderPortOpen` freezes at its last follower-era value and can never be cleared again**. A tab that was ever a follower of a live leader therefore reports `portOpen === true` for the rest of the page's life.

Repro correction: the stated scenario (unplug, THEN close tab A) mostly does not reproduce — leader A's read loop exits and announces `portOpen:false` within ~250 ms of the unplug (serial-hub.js:381, :407-410), clearing tab B's flag before A is closed. The deterministic repro is the reverse order: tab A is closed FIRST (board still plugged) → tab B promotes, `_adoptGrantedPort()` succeeds, port opens normally → the user unplugs LATER → `_readLoop` sets `_portOpen=false` and `_announceState()`s, but `_emitState()` publishes `false || true` = true. Card stays green and every send dies on the LEADER path — `_writeToPort` throws `'port not open'` (serial-hub.js:415) into the swallowing `.catch` at :194 — not on the follower 'tx'-into-the-void path the finding describes.

Fix correction: part (a) of the proposal is wrong and must be dropped. `case 'bye'` is broadcast by `leave()` (:207) and `releasePortKeepOpen()` (:230) from ANY tab, including followers (app.js:5449 `leaveShared()` → `hub?.leave()`, app.js:5760 `_sharedHub.leave()`). Clearing `_leaderPortOpen` on 'bye' would make one follower disconnecting flip every OTHER follower to "Not connected" while the leader still holds the port open, with no re-announce to heal it (the leader only announces on 'hello' or a state change). That trades a false-positive for a false-negative.

The minimal correct fix is a role-aware getter, which makes the whole "who clears it when" problem disappear:

  get portOpen() {
    if (this._role === 'leader')   return this._portOpen;        // only OUR port counts
    if (this._role === 'follower') return this._leaderPortOpen;  // only the leader's does
    return false;                                                // 'idle' — we left the hub
  }

Keep the reviewer's (b) and (c) as belt-and-braces if desired — `this._leaderPortOpen = false` at the top of `_becomeLeader()` (before `_setRole('leader')` at :284, so no stale-true state is emitted) and alongside `_portOpen = false` in `leave()`/`releasePortKeepOpen()` — but with the role-aware getter neither is load-bearing.

Behaviour note for whoever applies it: during a failover the promoted tab now reports `portOpen:false` for the ~0.3-1.2 s that `_openAndRead()`'s retry loop runs (:342-359), so the card briefly shows "Not connected" then returns to "Connected". That is honest, and the app's bounded waits (`for (let i = 0; i < 60 && !hub.portOpen; …)` at app.js:5653, :5492, :5797) already tolerate it.

Apply to BOTH copies but edit each in place: Wizard/serial-hub.js:114 and NaviCore/config_tool/serial-hub.js:114. The two files have diverged since the last reconciliation (NaviCore steps down on open-failure and on read-loop death; the Wizard copy does not), so do not copy one file over the other.

<details><summary>Full verification reasoning</summary>

I read all 549 lines of Wizard/serial-hub.js, the consumer paths in Wizard/app.js, and diffed the NaviCore copy.

WHAT I VERIFIED AS STATED
- serial-hub.js:114 `get portOpen() { return this._portOpen || this._leaderPortOpen; }` — exact.
- serial-hub.js:534 `_emitState()` publishes `portOpen: this.portOpen` (the getter, not `_portOpen`).
- app.js:4935 `isConnected() { return this._shared ? !!this._hub?.portOpen : this._connected; }` — exact.
- app.js:5427-5433 `_onHubState` does `const open = !!st.portOpen; this._connected = open; updateConnectionUI(this.boardIndex, open);` — exact. `updateConnectionUI` (app.js:7611) only early-returns when `connected` is FALSE (:7615, :7619), so a stale `true` paints the dot green and the label "Connected" (:7627-7629).
- The visibility-handoff subsystem really is dead code: `visibilityHandoff: false` (:58), read once at :79, gated at :145. `_relinquish()` (:472, the `_leaderPortOpen = false` at :478) is called only from `case 'claim'` (:513); 'claim' is posted only by `_claimLeadership` (:466); that is called only from `_onVisibilityChange` (:449) — wired only inside the `if (this._visibilityHandoff)` block — and from `case 'yield'` (:516), where 'yield' is posted only by `_onVisibilityChange` (:454). `grep -rn visibilityHandoff Wizard/` returns exactly 3 hits, all inside serial-hub.js. So :478 is unreachable in shipped config. Confirmed.
- `case 'bye'` (:518) is an empty `break;`. `leave()` (:202-220) posts 'bye' and calls `_closePort()` (which sets `_portOpen=false` at :431) but never calls `_announceState()`. Same for `releasePortKeepOpen()` (:227-246). Confirmed.
- BroadcastChannel does not self-deliver, and the code proves it structurally: `join()` posts 'hello' (:138) and `case 'hello'` makes a leader `_announceState()` (:507) — self-delivery would infinite-loop. So a tab never hears its own 'state'.
- The clearing site at :500 is gated by `if (this._role !== 'leader')` (:499). Therefore ONCE A TAB BECOMES LEADER, `_leaderPortOpen` is frozen at whatever it was, permanently.

WHERE THE REVIEWER IS WRONG (mechanism + repro)
1. "the ONLY place that ever clears it is `_relinquish()`" is false. serial-hub.js:500 `this._leaderPortOpen = !!msg.portOpen;` clears it routinely whenever a live leader announces `portOpen:false` and we are still a follower. The reviewer contradicts himself two paragraphs later ("unless some OTHER live leader broadcasts portOpen:false") — that later sentence is the accurate one.
2. The stated failure scenario is the shaky one. "The user unplugs the board and closes tab A": on unplug, leader A's `_readLoop` while-condition (`this._port.readable`, :381) fails, the loop exits, and :407-410 sets `_portOpen=false` + `_announceState()` — within ~250 ms, and at worst ~1.5 s via the 6-empty-session cap (:404-405). Tab B therefore clears `_leaderPortOpen` at :500 BEFORE A's tab is closed, and the subsequent portless promotion reports `portOpen:false` correctly. That repro only fires if the tab is closed inside a sub-second window.

THE REPRO THAT ACTUALLY HOLDS (deterministic, no race)
1. Tab A leads with the port open; it announces `state{portOpen:true, portInfo}`. Tab B (Wizard follower) sets `_leaderPortOpen = true` and `_sawLeaderPort = true` and queues for the lock (:500-502).
2. Tab A is closed (or navigated away) while the board is still plugged in. Nothing announces `portOpen:false` — there is no beforeunload/pagehide handler anywhere in Wizard/ (grep for beforeunload|pagehide|unload returns only an unrelated comment at app.js:4073), and even a graceful `leave()` only posts the no-op 'bye'.
3. The browser releases the Web Lock; Tab B's callback fires `_becomeLeader()` (:282). `_adoptGrantedPort()` (:318) finds the still-present port, `_openAndRead()` opens it, `_portOpen = true` (:367). Everything works — but `_leaderPortOpen` is still true and is now permanently unclearable because of the `role !== 'leader'` gate at :499.
4. The user later unplugs the board. `_readLoop` exits, :407-409 sets `_portOpen = false` and calls `_announceState()` → `_emitState()` → `portOpen: false || true` = TRUE.
5. `_onHubState` gets `st.portOpen === true` → card stays green "Connected"; `isConnected()` (app.js:4935) returns true.
6. Every send is silently dropped, on the LEADER path (not the follower path the reviewer describes): `send()` (:193) → `_writeToPort()` → `:415 if (!this._port || !this._port.writable) throw new Error('port not open')` → swallowed by the `.catch` at :194. `send()` resolves. `BoardConnection.send` (app.js:5212) does `await this._hub.send(data); return;` — no error surfaces. Pull/Push/Go just hang waiting for a reply.

EXPOSURE. Not niche: `establishConnection` auto-shares the FIRST board connected (`allowShare` defaults true, app.js:5634-5641), so a normal single-board Wizard session is in shared mode by default. The bug additionally needs a second same-origin tab to have led at some point — which is the entire purpose of the hub and is an actively exercised path (commits 4a524c1, 581d939; the failover comment at :286-292).

Single-tab-only sessions are NOT affected: that tab never receives a 'state', so `_leaderPortOpen` stays false and the getter is honest.

BOTH COPIES. NaviCore/config_tool/serial-hub.js has the identical getter (:114), the identical `role !== 'leader'` gate (:471) and the identical dead `_relinquish` clear (:449), so it carries the same defect. Note the two files have DIVERGED since the last reconciliation (NaviCore has `_stepDownPortless()` on open-failure and on read-loop death; the Wizard copy still only `_announceState()`s at :356/:409) — so the fix must be applied surgically to each, not copied wholesale.

INTENT CHECK. This is not a documented trade-off. The `|| this._leaderPortOpen` is deliberate ("is the shared port open ANYWHERE?"), and app.js:7015-7020 and app.js:12131-12133 both explicitly note the getter is true for a follower and therefore use the private `_portOpen` for flash gating. Nothing anywhere anticipates the value surviving a role change. `git blame` puts the line in the original 4a524c1, before conditional-campaigning and failover existed — it predates the promotion path that breaks it.

Under this review's own ladder (S1 crash/corrupt/brick, S2 wrong behaviour in a real scenario, S3 latent race/bound/leak), this is wrong behaviour in a real scenario, not a latent race — S2 stands. It is fail-CLOSED (nothing is written to a board, no config corruption) and a page reload clears it, which is why it is not S1.

</details>

### `Wizard/serial-hub.js:356` — CONFIRMED · S2

**Wizard/serial-hub.js is missing two lock-squat fixes that NaviCore's required-lockstep copy has**

**Correction to the finding as filed:**

Two corrections to the framing, neither of which undermines the finding.

1. The divergence is BIDIRECTIONAL, not "the Wizard is stale". The Wizard copy carries two methods NaviCore's does not: `adoptPort()` (Wizard/serial-hub.js:175-182) and `releasePortKeepOpen()` (:227-246). Wizard/app.js depends on both (`adoptPort` at app.js:5490 and :5765, `releasePortKeepOpen` at :7032 for the flash auto-demote borrow), and `grep -rn "adoptPort|releasePortKeepOpen" NaviCore/config_tool/` returns nothing. So anyone "re-syncing" must apply the three hunks by hand — copying NaviCore's file over the Wizard's would break auto-share and flash auto-demote outright.

2. The third change is REQUIRED, not cosmetic, and the reviewer's fix text is truncated before naming it. Moving `_portlessRetries = 0` out of `_becomeLeader` (:291) and into `_openAndRead` after a successful open is load-bearing: if you apply only the two step-downs, a dead port produces `_stepDownPortless()` (retries=1, ~800-1500 ms backoff) → `_maybeCampaign()` → sole candidate re-wins → `_becomeLeader` sees `this._port` truthy and resets the counter to 0 → open fails again (4 x 300 ms). That is a permanent ~2-2.7 s leader/follower churn loop, each cycle spamming "[hub] became LEADER" / "[hub] open failed" into the Wizard terminal via `_onHubLog` → `termLog` (app.js:5437). NaviCore's comment at config_tool/serial-hub.js:385-387 says exactly this. All three must land together.

Minor: the second location is Wizard/serial-hub.js:407-411, not :406-410.

<details><summary>Full verification reasoning</summary>

TRANSCRIPTION CHECK (all reviewer numbers verified, none trusted): `wc -l` → NaviCore/config_tool/serial-hub.js = 519, Wizard/serial-hub.js = 548. `md5sum` → Wizard a80702f21f141af8d165f14fc0f54c8f, NaviCore fb9675b8e7a9107c27afdf9c6b9ce02b. Both match the finding exactly. The quoted NaviCore block is verbatim at NaviCore/config_tool/serial-hub.js:315-318. The Wizard line is verbatim at Wizard/serial-hub.js:356.

DIVERGENCE IS REAL. `diff -u` gives five hunks. Three are functional and all favour NaviCore:
(1) Wizard/serial-hub.js:356 `if (i === attempts - 1) { this._log('open failed: ' + msg); this._announceState(); return; }` vs NaviCore:318 `… this._stepDownPortless(); return;`
(2) Wizard:407-411 `if (this._portOpen && …) { this._portOpen = false; this._announceState(); this._log('port read loop ended (unplug?)'); }` vs NaviCore:378-381 `{ this._log(…); await this._closePort(); this._stepDownPortless(); }`
(3) the `_portlessRetries = 0` reset: Wizard resets it in `_becomeLeader` (:291) before the open; NaviCore moved it into `_openAndRead` after a *successful* open (its comment at :385-387 explains why).
Git: NaviCore's copy last changed in f72878a "Apply 28 confirmed medium-severity review fixes" (2026-08-18); the Wizard's last changed in 581d939 (2026-08-05). So NaviCore is ahead and the Wizard was never re-synced.

MECHANISM RE-DERIVED. `_campaign()` (Wizard/serial-hub.js:264-280) holds one exclusive Web Lock for the tab's whole reign via a promise that stays pending until `_releaseLock()` fires. In BOTH Wizard failure paths the function returns with `this._role === 'leader'` and `this._releaseLock` still set — so the lock is never released and every other same-origin tab stays parked in the browser's lock queue. Path (2) additionally never calls `_closePort()`, so the dead OS handle is retained too. `_maybeCampaign()` (:257-262) bails at `if (this._lockAbort || this._releaseLock) return;`, so the squatting tab also never retries itself. That is a genuine squat, not a transient one.

REACHABILITY — REAL. The Wizard auto-shares the FIRST board connected (`establishConnection`, Wizard/app.js:5648-5665), so a Wizard tab is routinely the hub leader, and `index.html:1052` loads the file. Path (2) is triggered by an ordinary unplug or the 6-empty-session cap (:404). I grepped for app-level rescue: there is NO `navigator.serial` 'disconnect' listener anywhere in Wizard/*.js, and `_onHubState` (app.js:5427-5435) only flips the card to "Not Connected". Nothing releases the lock. Recovery is exactly what NaviCore's comment (:376-377) says — "until the user disconnected this tab by hand" — i.e. `boardDisconnect` → `leaveShared()` → `hub.leave()` (app.js:6202, serial-hub.js:212-215).

PARTIAL MITIGATION the reviewer did not mention: path (1) is already covered at its most common entry point. `establishConnection` waits ~3 s for `hub.portOpen` and on failure calls `conn.leaveShared()` + `_sharedHub.leave()` (app.js:5650-5661) — the comment there even says "adoptPort/_openAndRead SWALLOW an open failure". But `modalSharedConnect` (app.js:5798-5803) only toasts "no tab has opened the port yet" and leaves the leader squatting, and `becomeShared()` (app.js:5484-5497) waits 2 s with no teardown. So path (1) is partly guarded and path (2) is not guarded at all.

INTENT: not a deliberate trade-off. The Wizard has no comment defending the announce-only behaviour; NaviCore carries three explicit comments saying the opposite, and MEMORY records "both serial-hub.js copies reconciled to lockstep (keep them in sync)". It is already logged as F-366 under "Tier 3 — cross-repo" in docs/CODE_REVIEW_2026-08.md:119-121.

SEVERITY: on this review's own scale (docs/CODE_REVIEW_2026-08.md:10-11, S2 = "wrong behaviour in a real scenario") S2 is correct — unplugging a shared board blocks the port for every tab on the origin. It sits at the low end of S2: it needs the cross-tab share feature plus a second tab to actually bite anyone, causes a stall rather than corruption, and is one-click recoverable via Disconnect.

</details>

### `c:/Users/ghulette/Documents/GitHub/WCBClient/src/WCB_Client.cpp:2007` — CONFIRMED · S2 → **S3**

**WCB_Client decodes a WDP SOLICIT as an advert, wiping the sending board's neighbor record**

**Correction to the finding as filed:**

Three corrections.

1. Severity S2 → S3. The failure is a transient, self-healing (≤60 s) blank in ONE slot of a client's RAM roster, reachable only when an operator explicitly clicks "Poll mesh" or types ?WDP,POLL. The consumer path that would persist damage is already guarded (NaviCore.ino:4584 `if (nb.name[0])` keeps the alias cache), no functional routing reads the wiped fields, and the temporary-peer→persistent-NVS escalation is unreachable because the WCB firmware never advertises WDP_ADVFLAG_TEMPORARY. Real damage is limited to empty portLabels[] and seqHash→0 in NaviCore's WCB_META (rc_telemetry.h:1919-1944), which the tool caches. It would become S2 if any consumer starts acting on neighbor capFlags/maestroIds.

2. Blast radius overstated. The description implies the sending board's record is wiped everywhere on every poll — true, but it is only the ONE polled board's slot, not the mesh. Every other board answers the solicit with a real advert and is unaffected.

3. The fix is correct but incomplete. Returning early stops the client corrupting itself, but it also leaves the client deaf to the poll: the firmware's guard calls `wdpArmSolicitedAdvert()` (WCB_WDP.cpp:347-353) so the board answers within 0-600 ms. A WCB_Client that has called setIdentity should mirror that — set a flag and let `_wdpTick()` (WCB_Client.cpp:1977-1991) send it by pulling `_wdpBootLeft`/`_wdpNextBootMs` forward with a jitter. It must NOT call `_sendWdpAdvert()` inline: `_handleWdpAdvert` runs on the ESP-NOW RX callback (WiFi task, small stack — the CRITICAL comment at WCB_Client.cpp:2071-2075 makes that a hard rule for this function) and `_sendWdpAdvert` stack-allocates a 252-byte `wcb_packet_etm_t` plus esp_now_send. Without the arm-and-defer half, "Poll mesh" still will not refresh NaviCore's row in the Wizard mesh panel. Also worth folding in: add a SOLICIT row to docs/WDP_DESIGN.md §12 and soften the "forward compatible in both directions" claim at :75, and bump WCB_Client (library.properties currently 1.15.0) since the fix must reach the shipped library, not just the sketchbook copy.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived, holds.

1. Sender side. `wdpSendSolicit()` (WCB/Code/WCB/WCB_WDP.cpp:332-342) builds exactly 5 bytes: `['W', WDP_PROTO_VERSION, 0x11, 0x00, 0x00]` and hands them to `wdpBroadcast()` (Code/WCB/WCB.ino:968-981), which zeroes an `espnow_struct_message_etm`, sets `structCommandIncluded = 1`, `structSenderID = WCB_Number`, `structPacketType = PACKET_TYPE_WDP`, and sends to the broadcast MAC. So a solicit is indistinguishable at the packet layer from an advert.

2. Receiver side. `WCB_Client::_handleReceive` dispatches `case WCB_PACKET_WDP:` → `_handleWdpAdvert((uint8_t)senderID, pkt->structCommand)` whenever `senderID` is 1..WCB_MAX_BOARDS and `structCommandIncluded` (WCBClient/src/WCB_Client.cpp:2695-2698). No packet-content filter.

3. `_handleWdpAdvert` (WCB_Client.cpp:2001-2094): the magic/proto gate at :2003 passes (`WCB_WDP_MAGIC 'W'` / `WCB_WDP_PROTO 0x01` at :1819-1820 match the firmware's), then `memset(&nb, 0, sizeof(nb))` at :2006 (the reviewer cited :2007, which is actually `nb.valid = true` — trivial off-by-one in the citation). The TLV walk then reads `cmd[2] = 0x11`, which is not in the switch, falls to `default: break` at :2061 (`o += 2`), reads `cmd[4] = 0x00 = WCB_WDP_TLV_END` and exits at :2014. Net: name, fw, hwVer, hwRev, capFlags, capTags, ctrlId, seqHash, temporary, maestroIds and portLabels are all zeroed, `valid` stays true, and `_neighborCallback(nb)` fires with the blank record at :2093.

4. WCB_Client has no knowledge of 0x11: `grep -rn "SOLICIT\|0x11" WCBClient/src WCBClient/examples` returns nothing; the TLV defines at WCB_Client.cpp:1819-1837 cover 0x01,0x03,0x04,0x05,0x06,0x09,0x0A,0x0B,0x0C,0x0D,0x0E,0x12,0x13 — 0x11 is absent. `Arduino-Code/libraries/WCB_Client/src/WCB_Client.cpp` is byte-identical to master (`diff -q` → identical), so the bench copy is affected too.

5. Aggravator the reviewer missed: `nb.lastSeenMs = millis()` at :2009 is refreshed by the solicit, so `_ageNeighbors` (:1475-1490) keeps the blank record `valid` for the full TTL instead of expiring it.

REACHABILITY — real but narrower than claimed. `wdpSendSolicit` has exactly one caller: `processWdpCommand` "POLL" at WCB_WDP.cpp:1209-1216. Wizard `wdpPollMesh()` (Wizard/app.js:12514-12518) sends `?WDP,POLL` to the connected board only, from the one button at app.js:12394; the automatic mesh-discovery timer issues only `?WDP,DUMP` (app.js:12523-12528 comment), never POLL. So one click blanks exactly ONE slot — the polled board's — in every WCB_Client on the mesh, not the whole roster. Other boards answer the solicit with genuine adverts and are fine.

Self-heal window: POLL sends the board's own advert immediately BEFORE the solicit (:1213-1214), so the client is handed fresh facts and then wipes them. ESP-NOW does not loop back, so the polled board never hears its own solicit and arms no solicited advert; `wdpSendAdvert()` does not reschedule `wdpNextAdvertMs` (:320-327) and the content hash is unchanged, so the blank persists until the 60 s backstop (`WDP_PERIOD_MS`, WCB_WDP.cpp:52) — 0-60 s, ~30 s average.

DOWNSTREAM IMPACT — mostly guarded, which is why I am downgrading. In NaviCore (the main consumer): `onWcbNeighbor` (NaviCore.ino:4584) guards the alias cache with `if (nb.name[0])`, so the blank name is ignored and the roster name survives; `setWcbClient(nb.wcbNumber, nb.isClient)` is unconditional but the sender is always a real WCB so `false` is correct either way. Damage is confined to `buildWcbMeta` (rc_telemetry.h:1919-1944) — that board's `portLabels[]` read empty and `seqHash` reads 0 ("not yet known") while blank, and the tool caches WCB_META, so it can cache empty labels and re-pull the sequence inventory unnecessarily. No routing/dispatch decision in NaviCore reads neighbor `capFlags`/`maestroIds` (grep over NaviCore/*.ino,*.h finds no consumers). The one path that would persist damage is NOT reachable: wiping `nb.temporary` could let auto-join (:2087-2091) promote a temporary peer to a persisted NVS member, but the WCB firmware never SETS `WDP_ADVFLAG_TEMPORARY` in its own payload (only decodes it at WCB_WDP.cpp:539-540; the sole setter in the ecosystem is WCB_Client.cpp:1955-1957), so no temporary device ever sends a solicit.

INTENT — the opposite of a deliberate trade-off. WCB CLAUDE.md rule 10: "WDP is a wire format shared with WCB_Client. New TLVs need both sides, and must not break an older peer's parse." docs/WDP_DESIGN.md:95 states the contract — "carries no facts, receivers reply with a jittered advert and never record it" — and the firmware carries a dedicated host test, tests/wdp_wire_test.cpp:268-273 "SOLICIT short-circuit (no record wipe)". The client half was simply never updated. SOLICIT landed in c0a8d93 (2026-07-21) and has no row in the WDP_DESIGN.md revision log (§12 stops at the two SEQHASH rows) — a secondary doc gap, and docs/WDP_DESIGN.md:75 ("Unknown TLV types are skipped via the length prefix — forward compatible in both directions") is now literally false for a packet-level TLV like SOLICIT.

Bounds check on the proposed pre-pass: `structCommand` is `char[200]` on both sides (WCB_Client.h:148 and :159; WCB.ino:209 and :782), so the firmware's `s + 2 + ln > 200` bound the fix copies is exact and cannot overrun.

</details>

### `Code/bin/build.sh:181` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**build.sh deletes the committed firmware binaries and exits 0 when the branch name contains a "/" — CI then commits and pushes the deletion as a green build**

**Correction to the finding as filed:**

Severity S2 → S3: real and historically demonstrated (`e746e56`), but unreachable on `main`, so no released firmware, no GitHub Release, and no production-Wizard impact; contained to non-default branches, recoverable from git history, self-healing on the next non-slash build. The claimed forward-merge escalation to "breaks the production flasher for every user" does not hold — the merge commit touches `Code/**`, which triggers the main build that republishes, and the version+branch-stamped filenames usually mean the deletion doesn't propagate at all.

Two small corrections to the write-up, neither fatal: (1) the surviving fixed-name `Wizard/bin/*` copies are not a mitigation — `Wizard/bin` is referenced nowhere except `build.yml:134`'s `git add`; the flasher (`flasher.js:47,116-131`) and the update indicator (`app.js:127-131`) both read `Code/bin` over the GitHub Contents API. (2) The concrete live consequence is narrower than stated but real: a `/dev/<slash/branch>/Wizard` preview — a configuration `flasher.js:66-67` explicitly supports — can never flash, because `Code/bin` on that branch is empty.

Corrected fix: keep (a) `BRANCH="${BRANCH//\//-}"` immediately after `Code/bin/build.sh:51` — verified safe: the release glob at `build.yml:182` is `_main_*` (no slash), `flasher.js:130` matches by `_ESP32.bin` suffix only, and `app.js:135` matches the `WCB_<version>_` prefix, so none of them care about the branch segment. Drop the `set -e` half of (b) and use only the explicit form: `cp … || { echo "✗ publish failed"; rm -rf "$TMP_ESP32" "$TMP_S3"; exit 1; }` on `:183-202`, plus an existence assertion on the expected outputs before `:213`. Optionally add `'!claude/**'` to `build.yml:5` the way `pages-deploy.yml:34` already does.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived line by line from `Code/bin/build.sh` (read in full, 213 lines):

- `:48-51` `BRANCH=$(git -C "$SKETCH_PATH" rev-parse --abbrev-ref HEAD 2>/dev/null)`, falling back to `${GITHUB_REF_NAME:-unknown}` only when it is empty or the literal `HEAD`. No sanitization anywhere — `BRANCH` is used verbatim in filenames and nowhere else.
- `:181` `rm -f "$OUTPUT_DIR"/WCB_*_ESP32*.bin`. The comment at `:179-180` is accurate: the glob spares `WCB_S3_custom_bootloader_{8,16}MB_wdt3s.bin` (no `_ESP32` substring). It does delete all 8 version-named artifacts currently committed (`ls Code/bin` on OTA: 8 files matching `WCB_6.2.0_172007RAUG2026_OTA_*`).
- `grep -c 'OUTPUT_DIR/WCB_${VERSION}_${BRANCH}'` = **8** — the reviewer's count is right (`:183,184,185,190,191,195,196,201`). Fixed-name `WIZARD_BIN` copies at `:186-188,192-193,197-198,202` use no `$BRANCH`.
- `grep -n "^[[:space:]]*set "` in build.sh → **none**. No `set -e`, no `cp` exit-code check anywhere in `:183-202`. `:213` is an unconditional `exit 0`.
- The two `check_boot` guards (`:176-177`) run *before* the `rm`, so they cannot prevent it.

I simulated the exact publish block with `BRANCH="fix/raw-serial-bidirectional"` in a scratch dir: the `rm` removed the version-named bins, each `cp` printed `cp: cannot create regular file '.../WCB_6.2.0_..._fix/raw-serial-bidirectional_ESP32.bin': No such file or directory`, the fixed-name Wizard copy succeeded, the custom bootloader survived, and the script exited **0**.

REACHABILITY — this is not hypothetical, it has already happened in this repo:

- `.github/workflows/build.yml:5` `branches: [ '**' ]` with `paths: Code/**` — the build runs on every branch, and the "Commit bin files" step (`:126-152`) has **no** branch condition: `git add Code/bin/ Wizard/bin/ …`, commit, `git push origin HEAD:${GITHUB_REF_NAME}`.
- `refs/remotes/origin/fix/raw-serial-bidirectional` exists (and 8 local `claude/*` branches).
- `git ls-tree origin/fix/raw-serial-bidirectional Code/bin/` returns **only** `build.ps1` and `build.sh` — no firmware at all — while `Wizard/bin/` on that same branch carries all 6 fixed-name bins.
- The deletion commit is `e746e56` "Auto-build: update firmware binaries [skip ci]": `git show --stat` shows 6 `Code/bin/WCB_6.0_…_main_*.bin` going `Bin N -> 0 bytes` (deleted, none added) in the same commit that *updates* `Wizard/bin/WCB_ESP32.bin` and `WCB_ESP32S3.bin` — i.e. both targets compiled, the fixed-name copies landed, the version-named ones did not, and CI committed and pushed the deletion as a green run. That was the older script (`BRANCH` unsanitized at `:29`, `rm -f "$OUTPUT_DIR"/*.bin` at `:45`), but the slash mechanism at `:48-51`/`:183-201` is unchanged in today's version.
- `git add <dir>` staging deletions (git 2.55 here) is confirmed empirically by `e746e56` itself, not just by doctrine.

INTENT — the header comment `:25-35` documents the *opposite* as a fixed bug: "if BOTH succeeded… If ANY build fails it touches nothing and exits NON-ZERO… (A previous version deleted the bins up front and always exited 0, so a failed CI build silently wiped Code/bin while reporting success.)" The compile-failure half was genuinely fixed (`:119-128` exits 1 before the `rm`); the publish half was not. So this is not a deliberate trade-off — it is the same failure mode the comment claims is gone.

WHY I DOWNGRADE TO S3 (reviewer said S2):
- `main` has no slash, so nothing here can touch released firmware, the auto-version stamp, the tag, or `gh release create … Code/bin/WCB_${VERSION}_main_*.bin` (`build.yml:182`).
- The production Wizard flasher resolves to `main` by default (`Wizard/flasher.js:46` `GITHUB_BRANCH_DEFAULT='main'`, `:65-81` `getFirmwareBranch()` — URL `/dev/<branch>/Wizard` wins, else localStorage escape hatch, else `main`), so public users are unaffected.
- The reviewer's escalation "a wiped Code/bin merged forward to main would break the production flasher for every user" is overstated: a merge that deletes `Code/bin/*` touches `Code/**`, which itself triggers the main build that immediately republishes; and because filenames embed `<version>_<branch>`, main's current artifacts usually aren't the ones the slash branch deleted, so the deletion typically does not even propagate.
- Damage is contained to non-default branches, is fully recoverable from git history, and self-heals on the next non-slash build.
- Not S4, because it is silent, green, destroys tracked artifacts, and does have one live consequence I verified: `Wizard/flasher.js:66-67` explicitly supports slash refs ("<branch> is the exact git ref name (slashes preserved, e.g. feature/x)") and `pages-deploy.yml:71-72` publishes `dev/$BRANCH/Wizard` for them, yet a slash branch can never have binaries in `Code/bin` — so its preview flasher dies with `No file ending with "_ESP32.bin" found in Code/bin/` (`flasher.js:130`). Note also that `Wizard/bin/` is referenced nowhere in the repo except `build.yml:134`'s `git add` (grep over *.js/*.html/*.yml/*.md): both `flasher.js:116-131` and `app.js:127-131` fetch `Code/bin` through the GitHub Contents API. So the copies that *do* succeed on a slash branch are the ones nothing consumes.

FIX CHECK — see `fix_is_correct`/`correction`.

</details>

### `Code/WCB/WCB_Help.cpp:132` — CONFIRMED · S2 → **S3**

**?BAUD help documents the legacy form `?Sx,rate`, which the parser rejects (it needs no comma)**

**Correction to the finding as filed:**

Real, but mis-severed and the half that matters is the parenthetical, not the headline.

1. Severity S2 → S3. The help line alone is pure doc drift (S4 by this review's own legend at docs/CODE_REVIEW_2026-08.md:11). No NVS is written, no live baud changes, nothing in the Wizard parses the output, and the user does see `Invalid baud rate` first. What keeps it above S4 is the lying success print.

2. The load-bearing defect is WCB.ino:5663, not WCB_Help.cpp:132 — and the reviewer missed its twin. `Serial.printf("Updated Serial%d baud rate to %d and stored in NVS\n", ...)` fires unconditionally after `updateBaudRate()` in BOTH legacy paths: WCB.ino:5663 (`?Sx<rate>`) and WCB.ino:5690 (`updateSerialBaudRate`). Both are also redundant on success, because `updateBaudRate` already prints `Baud rate for Serial%d updated to %d` at WCB_Storage.cpp:180. The correct fix is to DELETE both lines (5663 and 5690), which removes the false success and the duplicate success in one move. The new-format `?BAUD,Sx,rate` path (WCB.ino:4555-4566) already does the right thing — it calls `updateBaudRate` and prints nothing extra.

3. Reviewer's trace of the first help line is wrong in a harmless way: `?BAUDSx,rate` is handled inline at WCB.ino:5336-5342, not by `updateSerialBaudRate`. That function (WCB.ino:5668-5691) plus its guard branch at WCB.ino:5655-5657 are unreachable dead code — the only call site (5656) sits behind a `startsWith("s"/"S")` gate at 5344 that a "baud..." string can never satisfy. Worth deleting (separate S4).

4. On the help line itself, deleting line 132 outright is cleaner than correcting it. `?Sx,rate` never worked in any shipped firmware (`git log -S` puts the no-comma `substring(2)` parse in the repo's earliest WCB.ino commit 483b4d8), and the wiki has never documented it — so no user config sheet carries that form, and the reviewer's "user on an older config sheet" scenario is speculative. Documenting a deprecated form that collides with the `?Sx0/1` broadcast toggle in the same namespace invites new confusion; if it is kept, the reviewer's replacement text is accurate.

<details><summary>Full verification reasoning</summary>

I re-derived every step rather than trusting the summary.

HELP TEXT (the filed location). `Code/WCB/WCB_Help.cpp:107-132` is the `?BAUD ?` page. Lines 130-132 read exactly:
  `Serial.println(F("\nLegacy commands:"));`
  `Serial.println(F("  ?BAUDSx,rate  (e.g. ?BAUDS1,57600)"));`
  `Serial.println(F("  ?Sx,rate      (deprecated, will be removed)"));`
Transcription in the finding is accurate.

DISPATCH of `?S1,57600`. `processLocalCommand` (WCB.ino:4394) splits on the first comma at WCB.ino:4409-4411, so `rootUpper == "S1"`. I listed every `rootUpper == "..."` case (grep: 36 of them, WCB.ino:4423-5128) — none is `S1`. Control reaches the LEGACY chain (WCB.ino:5147). I enumerated every `startsWith("s"/"S")` branch ahead of the catch-all: `smclear`/`smlist`/`smr`/`sm` (5245-5251), `sls`/`slc` (5278-5280), `sbi`/`sbo` (5282-5285), `statsreset`/`stats` (5290-5292). `S1,57600` matches none, so it lands on the catch-all `message.startsWith("s") || message.startsWith("S")` at WCB.ino:5343 → `updateSerialSettings("S1,57600")`. Confirmed.

PARSE (WCB.ino:5644-5666), read line by line:
- 5645 `port = substring(1,2).toInt()` → "1" → 1.
- 5646 `state = substring(2,3).toInt()` → "," → Arduino `String::toInt()` is `atol(buffer())`; `atol(",")` = 0.
- 5647 guard is `(state==0||state==1) && message.length()==3`. `"S1,57600"`.length() is 8 (I counted), so the broadcast-toggle branch is skipped.
- 5655 `startsWith("baud")` — no.
- 5659 else: `baud = message.substring(2).toInt()` → `atol(",57600")` = **0**. Confirmed, not transcribed on faith.
- 5660 prints the deprecation warning; 5662 calls `updateBaudRate(1, 0)`.

`updateBaudRate` (WCB_Storage.cpp:146): port guard 1-5 passes (port=1), then the whitelist at :155-160 rejects 0 → prints `Invalid baud rate` and returns. No `applyLiveBaud`, no NVS write, no `baudRates[]` update — so there is NO state corruption or NVS pollution (the comment at WCB_Storage.cpp:147-150 shows that guard was added deliberately for exactly this class of upstream parse failure).
Then WCB.ino:5663 unconditionally prints `Updated Serial1 baud rate to 0 and stored in NVS`. So the three-line output the finding describes is exactly what the board emits, in that order.

THE FORM THAT ACTUALLY WORKS. `?S157600` → port=1, state=`substring(2,3)`="5"→5, length 7 so the toggle guard is skipped, `substring(2)`="57600"→57600, `updateBaudRate(1,57600)` succeeds. So the real legacy form is `?Sx<rate>` with no comma, as the finding says.

INTENT / HISTORY. `git log -L 130,133:Code/WCB/WCB_Help.cpp` shows those three lines were born with the file in 64041df (2026-02-25) — the help page is new, the parser is not. `git log -S 'int baud = message.substring(2).toInt'` traces the no-comma parse back to 483b4d8, the earliest commit touching WCB.ino. So `?Sx,rate` has NEVER worked; the help line is invented, not stale-after-a-change. The wiki (`Wireless_Communication_Board-WCB.wiki`) documents only `?BAUDSx,rate` (Command-Reference.md:1082, Configuration-Guide.md:894, Serial-Communication.md:1269) and `?Sx0/1` as the broadcast toggle (Changelog.md:235,260; Troubleshooting.md:194) — it never documents `?Sx,rate`. No comment anywhere defends the line. Not a deliberate trade-off.

BLAST RADIUS (why I downgrade). Nothing machine-readable consumes the false success line: grep of `Wizard/` for "Updated Serial"/"baud rate to" hits only tooltip text in index.html:639,701 — no parser. The config-backup path emits the NEW form (`emit("BAUD,S" + ... , true)` at WCB.ino:2804), so restore never uses `?Sx,rate`. No NVS write occurs. The user also sees `Invalid baud rate` immediately before the bogus success line. Against this review's own legend (docs/CODE_REVIEW_2026-08.md:10-11: S2 = wrong behaviour in a real scenario, S4 = doc drift), the filed location is literally doc drift; what lifts it above S4 is the contradictory `...and stored in NVS` line, which is genuinely wrong output. S3.

TWO REVIEWER MIS-TRACES (neither changes the verdict). (1) `?BAUDSx,rate` is NOT handled by `updateSerialBaudRate`; it is handled inline at WCB.ino:5336-5342 (`substring(5,commaPos)`="1", `substring(commaPos+1)`="57600" for `BAUDS1,57600` — I counted the indices). (2) `updateSerialBaudRate` (WCB.ino:5668) is dead code: its only call site is WCB.ino:5656, inside `updateSerialSettings`, whose only call site is WCB.ino:5344 which requires the message to start with s/S — a message starting with 's' can never start with "baud". Repo-wide grep across Code/ confirms only those call sites exist.

</details>

### `Code/WCB/WCB_Help.cpp:669` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**?WLED? help documents only the legacy single-device form — the canonical ID-addressed config and ;L<id> addressing are undocumented**

**Correction to the finding as filed:**

Severity S3, not S2 — this is stale help text, not a functional defect, and the help's own documented workflow (`?WLED,PORT,S2:115200` + `;W4,;L,PS,2`) still works end-to-end, so the claimed "user gets [WLED] WLED 2 not configured" outcome requires the user to invent `;L2`, which the help never mentions. Cited line range should be WCB_Help.cpp:668-673, not 669-672.

The scope of the fix should be the whole topic, not just the Configuration block:
- :661 — `;L[id],<command>[,options]` (id optional; omitted = this board's lowest-id local WLED, WCB_WLED.cpp:126-137).
- :668-673 — lead with `<id>:W<wcb>S<port>:<baud>  Configure WLED <id> (1-9), local or on another board`; add `CLEAR,<id>  Clear one WLED` (WCB_WLED.cpp:214/:236); correct `CLEAR` to "Clear ALL WLED config (every slot, incl. remote proxies)" — the current wording "Release the WLED port" misdescribes `clearWLEDConfig()` (WCB_WLED.cpp:199-212).
- :674-686 — show `;L<id>,…` alongside bare `;L,…`, and add an example such as `?WLED,2:W4S1:115200` / `;L2,PS,3`.
- :689 — DELETE or rewrite. "One WLED per WCB" is false: MAX_WLED_PER_WCB is 9 (WCB_WLED.h:51), several local WLEDs on one board are supported, and remote proxies are auto-adopted from WDP adverts (wledAutoAddRemote, WCB_WLED.cpp:417). Leaving this line while fixing the block above would leave the topic self-contradictory.
- Move `PORT,Sx:baud` into the topic's `\nLegacy commands:` section (the pattern used by 19 other topics, e.g. MAESTRO at WCB_Help.cpp:466-468), noting it maps to id 1 local to this board.
- Same commit: docs/WLED_INTEGRATION.md:86-87 and §4 are stale in the same way (and `?WLED,PORT,CLEAR` there is not accepted by the parser at all).

<details><summary>Full verification reasoning</summary>

OBSERVATION IS REAL, SEVERITY IS NOT.

What I opened and re-derived:

1. The help block, WCB_Help.cpp:658-691. The "Configuration Commands (?WLED,...)" section is lines 668-673 (the reviewer said 669-672; :670 is a wrapped continuation of the PORT entry and :673 STATUS is a fifth line). It lists exactly PORT/CLEAR/LIST/STATUS. I grepped every "WLED" and every ";L" occurrence in WCB_Help.cpp — the strings `<id>`, `;L<id>`, `W<wcb>S<port>` and `CLEAR,<id>` appear nowhere in the file. The only other mention is the topic index at :1043. So the canonical form is genuinely undocumented in `?<HELP>`.

2. The parser really does accept strictly more, and really does call the documented form legacy. WCB_WLED.cpp:242-253 is the `PORT,` branch, comment at :243 verbatim "Legacy: ?WLED,PORT,S<port>:<baud>  ->  WLED id 1, local to this board.", forcing `wledID = 1` at :251. The canonical branch is :254-267, comment "New: ?WLED,<id>:W<wcb>S<port>:<baud>". Per-id clear is `clearWLEDByID` at :214, dispatched from :236 `if (aU.startsWith("CLEAR,"))`. Runtime id parse is WCB_WLED.cpp:113-142 (`;L3,PS,2` -> id 3, `;L,…` -> id 0 = lowest-id local). `MAX_WLED_PER_WCB` is 9, declared WCB_WLED.h:51 — I read the declaration, not the reviewer's word.

3. The staleness is WIDER than the finding states, which is the part worth carrying forward:
   - :661 `;L,<command>[,options]` and the whole Runtime Actions block :674-681 plus examples :684-686 show only the bare `;L,` form; `;L<id>` never appears.
   - :671 `CLEAR  Release the WLED port` is now wrong in a way that can cost a user work: bare `?WLED,CLEAR` runs `clearWLEDConfig()` (WCB_WLED.cpp:199-212) which `memset`s the ENTIRE 9-slot table including WDP-learned remote proxies, not "the port". The per-id `CLEAR,<id>` that would do what the text describes is undocumented.
   - :689 Note "One WLED per WCB; reach others by targeting their WCB" is now flatly false — 9 slots, multiple local slots supported (`wledPortUsedByOtherLocal`, WCB_WLED.cpp:57-64), and remote proxies are auto-learned from WDP (`wledAutoAddRemote`, :417).

4. WHY IT IS NOT S2 — the reviewer's failure scenario does not hold up. `git log -L 658,692` shows the block was written whole in 172c5b5 ("WLED module + …") when one-WLED-per-board was the design, and was never touched by the four later WLED commits (e01501f, cd4d35f, c132c50, ebf7e64 "updates for wled routing"). But a user following the help end-to-end SUCCEEDS: the help's own example :686 `;W4,;L,PS,2` works today, because bare `;L` resolves to the lowest-id local WLED on the receiving board (WCB_WLED.cpp:126-137). The reviewer's user only reaches `[WLED] WLED 2 not configured` (:142) by inventing `;L2`, a syntax the help never suggests — and that path errors loudly rather than misfiring. Three in-product surfaces already print the canonical form: `printWLEDSettings` when empty (:368-369), the bare-;L no-local-WLED message (:132-133), and `configureWLED`'s usage error (:258-259). The Wizard emits the canonical form (Wizard/parser.js:1424 `WLED,${w.id}:W${config.wcbNumber}S${w.port}:${w.baud}`) and parses both legacy and new (parser.js:751, :756). Nothing is broken and no configuration is silently corrupted — this is a discoverability/accuracy defect in help text. S3.

5. Not deliberate. Nothing in the comments or docs says the id form is hidden on purpose; WCB_WLED.h:30 already documents the runtime as `;L[id],...`. It is simple lag, exactly the kind CLAUDE.md's "keep docs current, same commit" rule targets.

6. Second, separate stale surface the finding misses: docs/WLED_INTEGRATION.md is the pre-implementation design doc and still shows only `?WLED,PORT,S1:115200` (:86) plus a `?WLED,PORT,CLEAR` (:87) the parser now rejects (the PORT branch requires a leading "S" and a ':' — WCB_WLED.cpp:247), and §4 still says per-node addressing within a board "isn't needed — deferred." Under the repo rule that a doc contradicting the code is a bug, that page should move in the same commit as the help fix.

</details>

### `Code/WCB/WCB_Help.cpp:925` — CONFIRMED · S2 → **S3**

**?CMDCHAR? documents `;Px,width` but the parser rejects the comma, and the rejection is silent**

**Correction to the finding as filed:**

Three corrections to the finding as written.

(1) The silence is stronger than described, and should be stated as such: the diagnostic at WCB.ino:6194 is *unreachable*, not merely debug-gated. `debugPWMEnabled` is defined `false` at WCB.ino:177 and read only at :6194 — no command assigns it. `?DEBUG,PWM,ON` sets `debugPWMPassthrough` (WCB.ino:4437), a different variable. So `;P4,1800`, `;P6,1500` and `;P4,3000` are all silent in every configuration.

(2) The failure narrative is overstated. The correct syntax IS documented for users — in the wiki, `PWM-Passthrough.md:368` shows `;P31500`. The defect is that the *on-board* help contradicts it. Drop the "only way to learn the real syntax is to read WCB.ino:6188" claim.

(3) Line numbers: the ROUTING COMMANDS block is WCB_Help.cpp:1057-1064, not 1058-1065.

Severity S3, not S2: a wrong help string for a niche runtime verb whose normal use path (`?MAP,PWM`) generates the correct form internally, with the correct form already published in the wiki. No functional, protocol, or persistence impact.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived from source, not the summary.

1. The help line is exactly as quoted. `Code/WCB/WCB_Help.cpp:925` inside the `?CMDCHAR?` topic (`} else if (c == "CMDCHAR")` at :908) prints `"  ;Px,width     Output PWM pulse on port x"` under the "Command character prefixes:" header at :919.

2. The parser rejects the comma. `processCommandCharcter()` (WCB.ino:5847) receives the command with the CommandCharacter already stripped — `handleSingleCommand` does `processCommandCharcter(cmd.substring(1), sourceID)` at WCB.ino:4385. The `p`/`P` branch at WCB.ino:5857-5858 calls `processPWMOutput(message)`. In `processPWMOutput` (WCB.ino:6187-6196), for `message == "P4,1800"`: `message.substring(1,2)` == `"4"` → `port = 4`; `message.substring(2)` == `",1800"` → Arduino `String::toInt()` is `atol(buffer())`, and `atol(",1800")` returns 0 because `,` is neither whitespace, sign, nor digit. So `pulseWidth == 0`, which fails `pulseWidth < 500` at WCB.ino:6193 and the function returns having pulsed nothing.

3. No upstream comma stripping. The chain splitter uses the CommandDelimiter (`^`, WCB.ino:5416 `updateCommandDelimiter`), never a comma; the queue drain at WCB.ino:7502-7512 passes the string through intact. So `;P4,1800` reaches the handler whole.

4. The rejection is *unconditionally* silent — stronger than the finding states. `debugPWMEnabled` has exactly two occurrences in the whole firmware: the definition `bool debugPWMEnabled = false;` (WCB.ino:177) and the read at WCB.ino:6194. It is never assigned `true` anywhere. `?DEBUG,PWM,ON` sets a *different* variable, `debugPWMPassthrough` (WCB.ino:4437, :5168). So no debug configuration can make that line print.

5. The correct grammar is corroborated by two independent places in the source: the handler's own comment `// Format: ;Pxnnnn where x=port, nnnn=pulse width in microseconds` (WCB.ino:6188), and the firmware's own emitter `snprintf(msg, sizeof(msg), ";P%d%lu", targetPort, pulseWidth);` (WCB_PWM.cpp:724) — no separator.

COVERAGE claims — checked directly, both true:
- The top-level menu's ROUTING COMMANDS block is WCB_Help.cpp:1057 (header) through :1064; it lists `;Sx`, `;Wx`, `;M<id><seq>`, `;M<dev>,verb`, `;A,PLAY,n`, `;Ckey`, bare `msg` — no `;P`. (The finding cited 1058-1065; off by one at each end, immaterial.)
- `grep -n ";P" Code/WCB/WCB_Help.h` returns nothing. Confirmed absent from the reference block.
- Repo-wide, the only two `;P` strings in the help system are :925 and :727 (`IF,mode>2,AND,armed=1^;PP100`, which is also invalid — `substring(1,2)=="P"`, `toInt()==0`, port 0 rejected).

REACHABILITY — trivially real. A user typing `;P4,1800` at the USB console hits it directly. The finding's stored-sequence variant also works: `?SEQ,SAVE,liftup,;P4,1800` → WCB.ino:4993 `saveStoredCommandsToPreferences("liftup,;P4,1800")` → WCB_Storage.cpp:626-633 splits on the **first** comma only, so `key="liftup"`, `value=";P4,1800"` is stored verbatim and replayed verbatim.

INTENT — nothing exculpatory. `git log -L 925,925` shows the line has existed unchanged since the file was created (64041df "updates"); there is no comment anywhere claiming a comma form is accepted, and the adjacent handler comment says the opposite.

WHY I DOWNGRADE TO S3 (two mitigations the reviewer missed or overstated):
- The failure-scenario claim "the only way to learn the real syntax is to read WCB.ino:6188" is **factually wrong**. The user-facing wiki documents the correct form in three places: `Wireless_Communication_Board-WCB.wiki/PWM-Passthrough.md:200` ("Format ESP-NOW message: ;Px[pulsewidth]"), `:368` (";P3[pulsewidth]  // Example: ;P31500"), and `:371` ("Message format: `;Px[value]`").
- Typed `;P` is a niche path. The normal PWM workflow is `?MAP,PWM,...`, and the firmware generates the correct comma-less form itself (WCB_PWM.cpp:724). Nothing functional, protocol-level, or persistent is broken — this is a wrong string in an on-board help topic. Real, user-visible, worth fixing; not a major defect.

</details>

### `Code/WCB/WCB_Storage.cpp:643` — CONFIRMED · S2 → **S3**

**?SEQ,SAVE accepts a key longer than the 15-char NVS limit — value silently discarded but the name is recorded**

**Correction to the finding as filed:**

The defect is real but S3, not S2. Corrections to the write-up:

1. Severity. Both shipping write clients already block it — Wizard/app.js:4220-4222 (`maxlength="15"` plus an n/15 counter on `.seq-key-input`) and WCBClient/src/WCB_Client.cpp:1209-1214 `wcbSeqKeyValid()` against `WCB_SEQ_KEY_MAX 15` (WCB_Client.h:223). The exposed path is a hand-typed `?SEQ,SAVE` / `?CS` at the console, or a third-party client. And the result is fully recoverable: `?SEQ,CLEAR,<name>` removes the phantom because `eraseStoredCommandByName()` rebuilds `key_list` at WCB_Storage.cpp:696-701 regardless of the failed NVS `remove()` at :676. Nothing is corrupted and nothing else breaks — a listed name that never fires, cleanable in one command.

2. Partial documented intent. docs/SEQUENCE_INVENTORY.md:226-229 already states the key rules are "enforced client-side so a bad write fails loudly instead of silently." The silent firmware failure was known; the gap is that the console has no client. Worth saying so in the finding rather than presenting it as unconsidered.

3. "The same guard the peer paths already use" is inaccurate. The only firmware path bounding at 15 is `?MGMT,SEQGET` (WCB.ino:3492-3495). The ONERR paths bound at 23 (WCB_MP3.cpp:242, WCB_DFP.cpp:193), which is `sizeof(onErrCmd)-1` — a buffer size, unrelated to the NVS limit. That 23-vs-15 mismatch is a real but separate defect (an ONERR key of 16-23 chars can never name a storable sequence).

4. The fix is incomplete as proposed. The reason a console user hits this at all is that `?SEQ ?` never states the limit — WCB_Help.cpp:756 says only "key: short name, no spaces" and the Notes at :777-788 are silent. The guard should ship with a matching help line ("key: 1-15 chars (NVS limit), no spaces, no comma"). Per CLAUDE.md this also earns a Revision-log row on docs/SEQUENCE_INVENTORY.md, since §3b's "enforced client-side" wording becomes stale once the firmware enforces it too.

<details><summary>Full verification reasoning</summary>

MECHANISM — verified end to end, not taken from the summary.

`saveStoredCommandsToPreferences()` is `Code/WCB/WCB_Storage.cpp:625-664`. I read it in full. The transcription in the finding is accurate: the only validation is the split-on-first-comma check (:627-630) and the emptiness check (:637-640). `preferences.putString(key.c_str(), value)` at :643 discards its return, `key_list` is appended at :656-659 independently of that result, and :663 prints the unconditional success line `Stored: Key='%s', Value='%s'`.

The 15-char claim is real and I re-derived it from the pinned core, not from memory:
- `.../Arduino15/packages/esp32/tools/esp32-arduino-libs/idf-release_v5.5-.../esp32/include/nvs_flash/include/nvs.h:61` — `#define NVS_KEY_NAME_MAX_SIZE 16 /* including null terminator */`, i.e. 15 usable chars; `:38` defines `ESP_ERR_NVS_KEY_TOO_LONG`. So `nvs_set_str` errors, it does not truncate.
- `.../esp32/hardware/esp32/3.3.4/libraries/Preferences/src/Preferences.cpp:264-279` — `putString` returns 0 on `nvs_set_str` failure and its only diagnostic is `log_e` at :270.
- That `log_e` is compiled out: `Code/bin/build.sh:89` and `:105` use `--fqbn esp32:esp32:esp32:PartitionScheme=min_spiffs` / `esp32s3:...` with no `DebugLevel` field, so `CORE_DEBUG_LEVEL` is 0. Nothing at all is printed. The reviewer's "silently" is correct.
- Character counts redone by hand: `DomePanelOpen1` = 14 (fits), `DomePanelsOpenAll` = 17 (fails). Correct as written.

The codebase itself states the same constant in four places, which independently corroborates it: `WCB.ino:821` (`char key[16]; // 15 chars = the NVS key limit`), `WCB.ino:3492`, `WCB_Variables.h:28`, `docs/SEQUENCE_INVENTORY.md:227`.

REACHABILITY — I grepped every caller and every writer.

Four call sites total: `WCB.ino:4995` (`?SEQ,SAVE`), `WCB.ino:5409` (legacy `?CS<key>,<value>` via `storeCommand`), and `WCB_Storage.cpp:895` / `:919` (migration). I read the `?SEQ` dispatch at `WCB.ino:4993-4996` — it splits on the second comma and passes `seqArgs` straight through with no length check anywhere upstream. So `?SEQ,SAVE,DomePanelsOpenAll,;MD1^;t500^;MD2` typed at the console reaches :643 unguarded. Confirmed reachable.

BUT the two shipping write clients both already enforce 15, which is what moves this off S2:
- Wizard: `Wizard/app.js:4220-4222` — the sequence key input is `maxlength="15"` with a live `${keyLen}/15` counter. `updateSequence()` (`app.js:4400-4446`) reads that input, so the Wizard cannot emit an over-length key.
- `WCB_Client`: `WCBClient/src/WCB_Client.cpp:1209-1214` `wcbSeqKeyValid()` rejects `n > WCB_SEQ_KEY_MAX`, and `WCB_Client.h:223` defines `WCB_SEQ_KEY_MAX 15 // NVS key limit — a longer key cannot be stored at all`. `saveSequence()` calls it at `:1321`.

So the exposed surface is a hand-typed console command (or `?CS`), or a third-party client. Not the normal path.

The consequence is also fully recoverable, which the finding does not mention. `eraseStoredCommandByName()` (`WCB_Storage.cpp:667-710`) rebuilds `key_list` without the name and writes it at :701 **regardless** of whether the NVS `remove()` at :676 succeeded, so `?SEQ,CLEAR,DomePanelsOpenAll` cleans the phantom up. No corruption, no crash, no security angle — just a listed name that never fires (`recallCommandSlot`, `WCB_Storage.cpp:556-564`, prints "No command stored under key").

Blast radius I checked and found harmless: `sequenceInventoryHash()` (:759-796) hashes the empty value, `buildSequenceNamesString()` (:798-829) emits the phantom name, and `espnow_struct_seqval_req.key[16]` is filled with `strncpy(..., sizeof-1)` at `WCB.ino:3503` so a 17-char request truncates rather than overflows. The config backup at `WCB.ino:2910` emits `SEQ,SAVE,<key>,` with an empty value, which a restore then rejects at :637 — cosmetic, not corrupting.

INTENT — this is the part that argues for a downgrade rather than a refute.

`docs/SEQUENCE_INVENTORY.md:226-229` says explicitly: "**Key rules** (enforced client-side so a bad write fails loudly instead of silently): 1–15 chars (NVS key limit), and **no comma**". The author knew about the silent firmware failure and made a deliberate choice to enforce it on the clients. That is a documented, if incomplete, position — it just has a hole where there is no client (the console). `git blame` shows :637-643 is `da509a52` (2025-07-23) and untouched since; no comment records a decision to leave it open.

The firmware is also inconsistent with itself: `WCB.ino:3492-3495` enforces exactly this on the **read** side (`?MGMT,SEQGET`: `key.length() == 0 || key.length() > 15` → "key must be 1-15 chars"), so the write side is the odd one out. That is the strongest argument the gap is an oversight, not intent.

The real reason a console user walks into this: `WCB_Help.cpp:743-792` — `?SEQ ?` says only "key: short name, no spaces" (:756) and the Notes block (:777-788) never mentions 15.

One correction to the finding's own supporting claim: "the same guard the peer paths already use" is half wrong. The ONERR paths an earlier review wave cited bound at **23**, not 15 (`WCB_MP3.cpp:242`, `WCB_DFP.cpp:193` — "key must be 1-23 characters"), and 23 is `sizeof(onErrCmd)-1`, a buffer size, not the NVS limit. Only `WCB.ino:3492` uses 15.

</details>

### `Code/WCB/WCB_Storage.cpp:1028` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**eraseNVSFlash() never reaches its own confirmation or restart, and ?ERASE,NVS silently reboots OTHER boards in the fleet**

**Correction to the finding as filed:**

Real and reachable, but S3, not S2, and the framing needs three fixes.

(a) Consequence 1 has no functional impact. The erase fully completes before WCB_Storage.cpp:1028; the only loss is the console string and a 3 s instead of 2 s reboot delay. Nothing parses "NVS cleared" — both Wizard erase paths use a fixed `sleep(4000)` (app.js:7306-7310, :9205-9210) and their comment "counts down ~3 s" already describes the PWM path's `delay(3000)`, not the dead `delay(2000)` at :1031.

(b) "Silently reboots OTHER boards" overstates the novelty. Rebooting a peer whose PWM output port is being removed is the established contract: `?MAP,PWM,CLEAR,OUT,S<n>` reboots the receiver at WCB.ino:4520-4527, and both `removePWMMapping()` (WCB_PWM.cpp:344-376) and `addPWMMapping()`'s stale-remote cleanup (:300-319) depend on it. The `sendESPNowMessage(wcb, "?REBOOT")` at WCB_PWM.cpp:491 is in fact redundant — the `?PX` handler already reboots (WCB.ino:5270-5277). And the peer only acts at all if its `LocalFunctionIdentifier` is `'?'`, because `?PX%d` hardcodes the char (WCB_PWM.cpp:482) while dispatch compares against the runtime value (WCB.ino:4379) — already logged as F-094.

(c) The finding misses the second instance of its own pattern: WCB.ino:4547 `Serial.println("All serial and PWM mappings cleared")` is also unreachable, because `clearAllSerialMonitorMappings()` (WCB_Storage.cpp:1912-1931) returns normally and `clearAllPWMMappings()` at :4546 then restarts.

Better fix than the one proposed: mirror the precedent already in this file — `addPWMMapping(const String&, bool autoReboot = true)` (WCB_PWM.h:38, WCB_PWM.cpp:170, honoured at :337-342). Give `clearAllPWMMappings(bool autoReboot = true)` the same parameter, keep the peer `?PX`/`?REBOOT` cleanup unconditional, and call `clearAllPWMMappings(false)` from `eraseNVSFlash()` and from WCB.ino:4546. Both call sites then reach their own confirmation and own the restart, and no peer is left with an orphan output port. While there, fix WCB_PWM.cpp:482/491 to use `%c`+`LocalFunctionIdentifier` (F-094), drop the redundant `?REBOOT`, and add a line to the `?ERASE` help (WCB_Help.cpp:945-956) stating that peers receiving PWM from this board are cleaned up and rebooted.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived, not taken from the finding.

`eraseNVSFlash()` is WCB_Storage.cpp:941-1033. I read the whole body. The last namespace pair is `wcb_vars` at :1026, then `clearAllPWMMappings();` at :1028, then :1030 `Serial.println("NVS cleared. Restarting...")`, :1031 `delay(2000)`, :1032 `ESP.restart()`. Nothing else follows.

`clearAllPWMMappings()` is WCB_PWM.cpp:454-512. Its tail is :499-505 the two `preferences.begin/clear/end` pairs for `pwm_mappings` and `pwm_outputs`, :507 `Serial.println("All PWM mappings cleared")`, :509 `Serial.println("Rebooting in 3 seconds to apply changes...")`, :510 `delay(3000)`, :511 `ESP.restart()`. The restart is at function scope, outside every `if` — unconditional, on a board with zero PWM mappings too. So WCB_Storage.cpp:1030-1032 is provably unreachable. Line numbers in the finding are exact. `git blame` puts :1028 and the PWM restart in the same commit 7327091 (2025-11-19); :1030-1032 predates it (483b4d8b, 2025-07-09) — the dead code is collateral of adding the call, not a deliberate choice, and no comment anywhere marks it as intentional.

Call sites confirmed by grep: `eraseNVSFlash()` at WCB.ino:5071 (`?ERASE,NVS`) and WCB.ino:5209 (`WCB_ERASE`); `clearAllPWMMappings()` at WCB.ino:4514, :4546, :5256 plus WCB_Storage.cpp:1028.

REACHABILITY of the peer half. The notify block is WCB_PWM.cpp:477-497, gated on `canSendESPNow()` (WCB_PWM.cpp:50-52: `return millis() > 5000` — true for any user-typed command) and on at least one `pwmMappings[i].active` carrying an output with `wcbNumber != 0`. `?PX%d` is built at :482 and sent at :483; `sendESPNowMessage(wcb, "?REBOOT")` at :491. Both use the 2-arg form, so `useETM` defaults true (WCB.ino:911). Reachable by a real user with a `?MAP,PWM,S1,W2S3`-style mapping.

BUT four corrections the finding misses, all of which cut against S2:

1. The confirmation half is purely cosmetic, not functional. Every `preferences.clear()` runs before :1028, so the factory reset genuinely completes. Nothing machine-parses "NVS cleared": both Wizard erase paths (`app.js:7303-7311` and `app.js:9203-9211`) send `${fc}ERASE,NVS` then `await sleep(4000)` with no output matching — and their own comment says "Firmware counts down ~3 s before erasing and rebooting", which matches WCB_PWM.cpp:510's `delay(3000)`, not the `delay(2000)` at :1031. The Wizard was written against the actual (PWM-path) behaviour. Zero functional impact.

2. Peer reboot on PWM-output-port removal is the codebase-wide deliberate pattern, not something `?ERASE,NVS` uniquely inflicts. `?MAP,PWM,CLEAR,OUT,S<n>` reboots the receiver itself (WCB.ino:4520-4527), and that is what `removePWMMapping()` (WCB_PWM.cpp:344-376) and `addPWMMapping()`'s stale-remote cleanup (WCB_PWM.cpp:300-319) both rely on. So "removing this board's remote PWM output reboots that board" is the designed contract everywhere.

3. The `?REBOOT` at WCB_PWM.cpp:491 is redundant: the legacy `?PX` handler already ends with `removePWMOutputPort(port); Serial.println("Rebooting in 3 seconds..."); delay(3000); ESP.restart();` (WCB.ino:5270-5277). The peer reboots from `?PX` alone.

4. The peer half only fires at all if the peer's `LocalFunctionIdentifier` is `'?'` — dispatch is `cmd.startsWith(String(LocalFunctionIdentifier))` at WCB.ino:4379, and `?PX%d` hardcodes `'?'` while the rest of the file uses `%c`+`LocalFunctionIdentifier` (:311, :328, :356). On a peer with a changed funcChar the strings fall through to `processBroadcastCommand` (WCB.ino:4386) and get sprayed out its serial ports as data. That is already logged as F-094 (docs/CODE_REVIEW_2026-08_wave2.md:1485).

INTENT. `?ERASE,NVS` help (WCB_Help.cpp:936-961) describes a purely local factory reset and lists what gets erased; nothing mentions fleet effects. So the surprise is real — but it is a surprising-yet-defensible cleanup (the mappings really are gone, so the peer's output-port declaration really is orphaned), plus a cosmetic dead println. That is S3, not S2.

Also found while verifying: the identical dead-code pattern exists a second time at WCB.ino:4546-4547 — `clearAllSerialMonitorMappings(); clearAllPWMMappings(); Serial.println("All serial and PWM mappings cleared");`. I read `clearAllSerialMonitorMappings()` (WCB_Storage.cpp:1912-1931) and it does NOT restart, so :4546 is reached and the :4547 println is likewise unreachable. The finding does not mention it.

</details>

### `Code/WCB/WCB_Storage.cpp:1636` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**?MAP,SERIAL appends destinations rather than replacing them, so a destination removed or changed in the Wizard stays live on the board**

**Correction to the finding as filed:**

The defect stands, but both halves of the proposed fix are wrong, and the corrected line numbers are :1636-1642 (reuse), :1650 (reset only on fresh slot), :1723-1729 (dup skip), :1732-1736 (append), :1752 (save gate).

Why "drop the outputsAdded > 0 gate so an emptied list persists" cannot work: the Wizard never gets a dest-less MAP to the storage function. With zero destinations app.js:3837 produces `?MAP,SERIAL,S1`, and WCB.ino:4490-4494 rejects it before addSerialMonitorMapping is ever called (`thirdComma == -1` → "Invalid format"). Only the raw variant `?MAP,SERIAL,S1,R` gets through (it becomes legacyStr "S1R," at :4502) — and dropping the gate there would make things WORSE: :1644-1655 already allocates and activates a zero-output slot in RAM, and WCB.ino:6238-6285 treats any active mapping as replacing all broadcast behaviour ("Exit early - mapping replaces all broadcast behavior", :6284), so a zero-output mapping black-holes that port's traffic. Today that state is RAM-only and dies at reboot; removing the gate would persist it to NVS.

Why "zero outputCount on the reused slot" breaks something else: the Wizard's bidir push at app.js:3950-3952 deliberately sends the destination board a SINGLE-destination `?MAP,SERIAL,S<destPort>[,R],W<srcWcb>S<src>`, relying on append (it mirrors this by `push`ing an extra mapping into destCfg.mappings at :3941-3944). Under replace semantics that command would silently wipe every other destination already mapped from that port on the peer board — and since the firmware holds exactly one mapping per input port (:1636-1642), there is no recovery. Any firmware-side switch to replace must be landed together with rebuilding the bidir command as the peer's full destination list, plus a help/wiki update (WCB_Help.h:42, wiki Broadcast.md:172-200) since the CLI's advertised behaviour changes.

The fix that actually works is Wizard-side, keeping the firmware verb additive: in saveMappingRow, when the row's baseline already had a mapping for that source port, send `${lfi}MAP,SERIAL,CLEAR,S${src}` immediately before the MAP command (and when destinations.length === 0, send ONLY the CLEAR, since `?MAP,SERIAL,S<n>` is rejected). Apply the same CLEAR-first treatment in parser.js:1508-1521 for Push Config, mirroring the existing MAESTRO,CLEAR/SEQ,CLEAR diffing. Side effects checked and benign: removeSerialMonitorMapping re-enables broadcast in/out for the port (WCB_Storage.cpp:1886-1899) and addSerialMonitorMapping re-blocks both (:1739-1748), so the net persisted state is identical — only a few extra NVS writes, and the two commands travel in the same send. This also fixes a related consequence of the same gate: a raw-mode-only change is applied in RAM (:1663-1665) but never persisted when outputsAdded == 0, so today toggling R alone is lost on reboot.

<details><summary>Full verification reasoning</summary>

MECHANISM — verified line by line in Code/WCB/WCB_Storage.cpp (reviewer's line numbers are 1-4 low; the code quoted is otherwise accurate):
- :1636-1642 `addSerialMonitorMapping()` finds an existing active mapping whose `inputPort` matches and reuses it via `break`, with no reset of any field.
- :1644-1655 `outputCount = 0` is executed ONLY inside the "allocate a fresh slot" branch (:1650). Confirmed the reused-slot path never touches it.
- :1723-1729 duplicates are skipped with `continue` ("skipping duplicate"); :1732-1736 non-duplicates are appended (`mapping->outputs[mapping->outputCount] = …; outputCount++`) under a hardcoded cap `< 10`, which matches the struct (`SerialMonitorOutput outputs[10]`, WCB_Storage.h:88-94; MAX_SERIAL_MONITOR_MAPPINGS is 5, WCB_Storage.h:70). Nothing in the function ever removes an output — grepped the whole function body.
- :1752-1758 the NVS write is gated on `outputsAdded > 0`.
- For contrast the reviewer's PWM comparison is correct: WCB_PWM.cpp:222-225 sets `mapping.outputCount = 0` unconditionally on the reused slot and snapshots old remote outputs (:210-220) to send CLEARs — PWM genuinely has replace semantics, serial does not.

REACHABILITY — the command path is real and unguarded:
- WCB.ino:4482-4508 is the only `?MAP,SERIAL` dispatcher. `LIST`, `CLEAR,ALL`, `CLEAR,Sx` are handled; everything else falls through to `addSerialMonitorMapping(legacyStr)` (:4508) with no clear first.
- Wizard save path: Wizard/app.js:3837-3842 builds `<fc>MAP,<TYPE>,S<src>[,R]` plus the FULL current destination list and sends it (:3855) with no preceding CLEAR. The only CLEAR the Wizard ever emits is on whole-row delete (app.js:3641 `MAP,<TYPE>,CLEAR,S<src>`).
- Per-destination delete is a real UI affordance: app.js:3803 is a 🗑 that only does `element.remove(); onMappingChange(...)`, and onMappingChange (:3571-3576) only syncs local config — it sends nothing. Row Save is app.js:3510.
- So the claimed failure reproduces exactly: existing S1 → S2, W2S3; user deletes the S2 dest and clicks Save; Wizard sends `?MAP,SERIAL,S1,W2S3`; firmware reuses the S1 slot, sees W2S3 as a duplicate (:1723), `outputsAdded == 0`, prints "No new destinations added" (:1757), and S1 still fans out to S2. On an EDIT (S2→S4) the new dest is added, `saveSerialMonitorMappings()` runs (:1753), and the board persists S2 AND S4 to NVS — stale routing survives reboot.
- Second affected path the reviewer missed: Push Config has the same defect. Wizard/parser.js:1508-1521 re-sends `MAP,SERIAL,S<n>,…` for every mapping when the set differs from the baseline, with no CLEAR for shrunken destination lists — even though the same builder deliberately emits `MAESTRO,CLEAR` for orphaned Maestro rows (parser.js:1474) and `SEQ,CLEAR` for removed sequences (:1534).

INTENT — additive is deliberate for the CLI, not for the tool. The duplicate-skip comment and the runtime message "%d new destination(s) added (total: %d)" (:1754-1755) show add-semantics were intended for a hand-typed verb, with `?MAP,SERIAL,CLEAR,Sx` as the documented removal (WCB_Help.h:42, wiki Broadcast.md:235). Nothing documents the interaction with the Wizard's replace-shaped Save. So this is a genuine tool/firmware semantic mismatch, not a documented trade-off.

SEVERITY — downgrading S2→S3. It is real, persists to NVS, and can push unwanted bytes at a device port or waste ESP-NOW traffic. But: it is not silent for long — saveMappingRow schedules `boardPull(n)` 2 s later (app.js:3860) and the pull repopulates the rows from the board (populateMappingsFromConfig, app.js:3147), so the deleted destination visibly reappears; the firmware also prints "No new destinations added"; and there is a one-click workaround (delete the row — that DOES send CLEAR,S — then re-add). No data loss, no crash, no fleet-wide protocol break.

</details>

### `Code/WCB/WCB_Storage.cpp:1663` — CONFIRMED · S2 → **S3**

**Toggling raw mode on an existing serial mapping changes it in RAM but is never persisted — it silently reverts on reboot**

**Correction to the finding as filed:**

Three corrections to scope and one addition.

1. Scope the title: creating a NEW mapping with `R` DOES persist (rawMode set at WCB_Storage.cpp:1651, committed at :1753 because outputsAdded > 0). The defect is confined to changing raw mode on an ALREADY-EXISTING mapping whose destination list is unchanged. Correct title: "Toggling raw mode on an existing serial mapping is applied to RAM but not to NVS when no new destination is added."

2. It is symmetric — un-ticking Raw (line mode requested, `,R` absent, inputRawMode == false) fails to persist identically, so a board can also come back raw after the user turned raw off.

3. It is not just the Save button: Push Config takes the same broken path (Wizard/parser.js:1508-1522 emits `MAP,SERIAL,S<x>[,R],<dests>` with no preceding CLEAR), so a full config push cannot repair a board whose NVS holds the wrong raw flag.

4. Additional case worth fixing in the same edit: `?MAP,SERIAL,S<n>,R` with no destination (WCB.ino:4501-4504 → legacy `S<n>R,`) creates an active, zero-output, raw mapping. That silently removes S<n> from normal command processing (WCB.ino:6603 via isSerialPortRawMapped) while forwarding to nothing. The function should reject a create with zero valid destinations rather than leaving the slot active.

5. Cosmetic bug in the fix as literally proposed: OR'ing `changed` into the :1752 condition makes the success printf at :1754-1755 report "0 new destination(s) added". Split the message, e.g. keep :1754 for outputsAdded > 0 and print "Raw mode updated" for the changed-only case.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived from source, not the summary.

`addSerialMonitorMapping()` is at Code/WCB/WCB_Storage.cpp:1593. I read :1593-1759 in full.
- :1614-1617 strips the `R` suffix off the source token into `bool inputRawMode`.
- :1637-1642 finds an existing active mapping for that input port; :1644-1655 creates one if none, and a NEWLY created mapping gets `mapping->rawMode = inputRawMode` at :1651.
- :1663-1665 is exactly as quoted: `if (mapping->rawMode != inputRawMode) { mapping->rawMode = inputRawMode; }` — RAM only, no NVS write, no `changed` flag, no comment.
- The destination loop's duplicate gate is at :1723-1729 (`mappingDestinationExists`, defined WCB_Storage.cpp:57-65, a plain linear scan over `outputs[0..outputCount)`), and `continue` skips the `outputsAdded++` at :1736.
- The only `saveSerialMonitorMappings()` call in the function is at :1753, gated on `if (outputsAdded > 0)` at :1752. Else-branch at :1757 prints "No new destinations added (all were duplicates or invalid)".
- `saveSerialMonitorMappings()` (:1830-1856) writes `sm<i>_raw` at :1844; `loadSerialMonitorMappings()` (:1535-1561) reads it at :1549 with default `false`. Keys re-counted by hand: `MAX_SERIAL_MONITOR_MAPPINGS` is 5 (WCB_Storage.h:70), so `i` is 0..4 and the longest key here is `sm4_raw` = 7 chars — well under the 15-char NVS limit, so no key-truncation confound. `rawMode` is a real struct field (WCB_Storage.h:88-94).

So the RAM/NVS split is real: raw-mode toggling on an EXISTING mapping whose destinations are unchanged is applied to RAM and never committed.

The RAM flag is live-read on other tasks, so the change appears to take effect: `RawSerialForwardingTask` gates on `serialMonitorMappings[i].rawMode` at WCB.ino:6644, and `serialCommandTask` gates ports 3/4/5 on `isSerialPortRawMapped()` at WCB.ino:6603-6605 (implemented WCB_Storage.cpp:1807-1816, reads the same RAM flag).

REACHABILITY — a real user hits this on the normal path, via three independent routes.
1. Console/mesh legacy form: WCB.ino:5252 `addSerialMonitorMapping(message.substring(2))` for `?SMS…`.
2. Modern form: WCB.ino:4491-4508 translates `?MAP,SERIAL,S3,R,W2S1` into the legacy `S3R,W2S1` before calling it — I traced the substring arithmetic, the `R` does survive.
3. Wizard, both write paths, neither of which emits a CLEAR first:
   - Save button: `saveMappingRow()` builds `?MAP,SERIAL,S<src>` and appends `,R` at Wizard/app.js:3837-3838 from the raw checkbox at app.js:3499/:3814.
   - Push Config: parser.js:1508-1522 emits the same shape (`if (m.type.toUpperCase() === 'SERIAL' && m.rawMode) cmd += ',R'`) with no preceding clear.
   The only CLEAR is app.js:3641, on the row-delete button.
   The Wizard then shows a green "Mapping saved to WCB N" toast (app.js:3855) and re-pulls 2 s later — and the pull re-reads RAM (WCB.ino:2820 `if (serialMonitorMappings[i].rawMode) cmd += ",R";`), so the UI positively confirms a state that NVS does not hold.

`setSerialMappingRawMode()` (WCB_Storage.cpp:1818-1828) does call `saveSerialMonitorMappings()` at :1822, and I confirmed the zero-caller claim with a repo-wide grep excluding .git: only the definition (:1818) and the declaration (WCB_Storage.h:239). It is dead code — the reviewer's transcription is accurate.

INTENT — not deliberate. `git log -L 1660,1670` gives one commit, 6ea724a "created a system to map serial and pass raw data" (2026-01-27), which replaced an unconditional clear-and-rebuild with the in-place update; there is no comment at :1662-1665 and nothing in docs/ or CLAUDE.md describing raw mode as non-persistent. The nearby comments that ARE constraints (:1676 infinite-loop note, WCB.ino:6601-6603 and :6652-6655 task-ownership notes) are unrelated to persistence.

WHY I DOWNGRADE TO S3 — the reviewer's title over-states the scope. The common first-time flow works: creating a NEW mapping with raw ticked sets rawMode at :1651 and, because at least one destination is added, persists at :1753. The bug window is specifically "toggle raw on an already-existing mapping whose destination list is unchanged". It corrupts one boolean, causes no crash and no other state damage, is recoverable inside the same UI (delete the mapping row, re-add it with Raw ticked), and the firmware does emit a (misleading) line about it. Real and genuinely silent-until-reboot, but narrower than an S2.

ADDITIONAL DEFECT THE REVIEWER MISSED (same root cause) — `?MAP,SERIAL,S3,R` with no destination is accepted: WCB.ino:4501-4504 turns it into legacy `S3R,` and :1671 sees `remaining == ""`, so the loop never runs. If no mapping existed, :1644-1653 has already created an ACTIVE, zero-output, rawMode=true mapping. That makes `isSerialPortRawMapped(3)` true, which takes S3 out of normal command processing at WCB.ino:6603 while `RawSerialForwardingTask` forwards its bytes to zero destinations (WCB.ino:6674 loop over `outputCount == 0`) — S3 goes deaf until reboot, and it is likewise unpersisted.

</details>

### `Code/WCB/WCB_Storage.cpp:2229` — CONFIRMED · S2 → **S3**

**etmMissedHeartbeats initializer of 5 is overwritten at boot by an NVS fallback of 3, so the documented 55 s offline window is never in effect**

**Correction to the finding as filed:**

Three corrections to the finding:

1. Severity is S3, not S2. `lastSeenMs` is refreshed by ANY received ETM packet (`WCB.ino:3762-3771`), a false-offline peer self-heals on its next packet (`:3769`), and the initial send still goes out — only the ETM retry accounting and `wcbPeerOnline()`-gated capability routing degrade. It is a tuning tolerance silently reverted, not a delivery failure.

2. Drop the suggested "if the stored value is exactly 3, rewrite it to 5" migration. 3 is a legal user-selectable value via `?ETM,MISS` (`WCB.ino:4688`, legacy `:5325`), so the migration cannot distinguish a stale default from a deliberate setting and would silently override the latter. It is also unnecessary — `etmMissedHeartbeats` is board-local (read only at `WCB.ino:1040`, `:2255` `?SETTINGS` print, `:2869` emit), never on the wire, so a mixed 3/5 fleet is harmless. Existing boards that already persisted 3 simply need `?ETM,MISS,5` or an NVS erase; that is a release-note item, not a code path.

3. The one-line firmware fix is necessary but NOT sufficient if the intent is that new installs run 5 — the Wizard carries its own default of 3 in six places and actively pushes it: `Wizard/parser.js:122`, `:176`, `:828` (parse fallback), `Wizard/app.js:1424`, `:9302` (setup-wizard `etmConfig`), `:10782`; and `Wizard/parser.js:1491` emits `ETM,MISS,${etm.missedHeartbeats}` on a full push. A config created from scratch in the Wizard therefore writes 3 into NVS on the first push, re-introducing the drift after the firmware fallback is fixed. Better still: define the value once (e.g. `#define ETM_MISSED_DEFAULT 5`) used by both `WCB.ino:267` and `WCB_Storage.cpp:2229`, so the pair cannot drift a third time, and move the Wizard defaults to 5 in the same commit. Also worth a `docs/` note (no docs page currently states either 33 s or 55 s — the source comment is the only record).

<details><summary>Full verification reasoning</summary>

MECHANISM — every load-bearing fact re-derived, nothing taken from the write-up:

1. `Code/WCB/WCB_Storage.cpp:2229` reads verbatim `etmMissedHeartbeats = preferences.getInt("etmMiss", 3);` inside `loadETMSettings()` (`WCB_Storage.cpp:2224-2238`), which opens `preferences.begin("etm_config", true)` — read-only, so it neither creates the namespace nor writes a default. NVS key `"etmMiss"` is 7 chars, far inside the 15-char limit, so the key itself is fine; the literal `3` is the defect.
2. `Code/WCB/WCB.ino:267` reads verbatim `int etmMissedHeartbeats = 5;   // (etmHeartbeatSec+1)*5 = 55s offline window — widened from 3/33s ... (mirrors WCBClient _missedBeforeOffline)`. Transcription in the finding is exact.
3. Every sibling default in the same pair agrees — `WCB_Storage.cpp:2226-2233` (true / 2 / 10 / 500 / 20 / 100 / true) vs `WCB.ino:264-271` (true / 2 / 10 / 500 / 20 / 100 / true). `etmMiss` is the single drifted pair, and `WCB.ino:264` even carries the note "NVS default is also true — overwritten at boot by loadETMSettings()", showing mirroring was the intent.
4. Arithmetic redone at `WCB.ino:1040`: `offlineThresholdMs = (unsigned long)(etmHeartbeatSec + 1) * etmMissedHeartbeats * 1000UL` = (10+1)*3*1000 = 33000 ms, not 55000. Correct.

REACHABILITY:
- `loadETMSettings()` is called unconditionally in setup at `WCB.ino:7416` (I read the surrounding block — no guard; it sits between `loadLocalFunctionIdentifierAndCommandCharacter()` and the `if (etmEnabled)` ETM init).
- Full-repo grep for writers: the only writer of `etmMiss` is `saveETMSettings()` (`WCB_Storage.cpp:2211-2221`), called ONLY from the `?ETM,*` handlers (`WCB.ino:4673-4714`) and the legacy handlers (`:5304-5334`). No boot-time seeding, no migration. `eraseNVSFlash()` clears the `etm_config` namespace (`WCB_Storage.cpp:1002-1004`), so a factory-reset board lands on 3 too.
- Worse than "fresh board only": once ANY `?ETM,*` command runs, `saveETMSettings()` persists all eight keys from the runtime values — which already contain the loaded 3 — so 3 becomes sticky and the 5 can never appear.
- No clamp or guard anywhere restores 5. Only user runtime writers are `WCB.ino:4688` and `:5325`.

INTENT — this is drift, not a deliberate trade-off. `git log -S'etmMissedHeartbeats = 5'` gives commit 3d88bdd (2026-07-30, "WCB: widen ETM offline window to 5 missed heartbeats (33s -> 55s)"). `git show --stat 3d88bdd` touches ONE file, `Code/WCB/WCB.ino` (3 insertions/3 deletions — two of them the version stamp). `WCB_Storage.cpp` was never touched by that commit. The commit message describes a real observed bench failure (20 Hz `rc_ch` live-monitor congestion flapping a live board out of the roster) and says it mirrors WCBClient 1.9.11; I checked the sibling repo — `WCBClient/src/WCB_Client.h:1086` is `uint8_t _missedBeforeOffline = 5;` with a matching comment, and WCBClient has no NVS override, so WCBClient genuinely runs 5 while WCB runs 3. So the intended cross-repo consistency is also broken.

CONSEQUENCES (verified, and narrower than the reviewer implies):
- A false-offline peer is skipped for ACK tracking (`WCB.ino:1204 if (!boardTable[b].online) continue;`) and in-flight retries are cancelled (`WCB.ino:1292-1297`), so ETM unicasts to a live board go out once, best-effort, un-retried. It is also dropped from the ETM-char peer list (`:1448`) and from `wcbPeerOnline()` (`WCB.ino:6868-6880`), which gates WDP capability routing/failover — a live capability owner can be re-elected away.
- Mitigations that cap the blast radius: `boardTable[].lastSeenMs` is refreshed on EVERY valid received ETM packet, not just heartbeats (`WCB.ino:3762-3771`, comment added 2026-05-27, i.e. already present when the flap was observed), and any subsequent packet sets `online = true` again (`:3769`) — so recovery is automatic within ~10 s. The commit also notes the congestion source itself was removed by the paired NaviCore rc_ch 20 Hz -> 5 Hz default. Effect is a reduced tolerance window (33 s vs 55 s) and occasional flap under load, not data loss or a crash — hence S3, not S2.

Note: the reviewer's cited consumer `isBoardOnline()` does not exist in the tree (grep returns nothing); the real function is `wcbPeerOnline()` at `WCB.ino:6868`. That does not change the verdict.

</details>

### `Code/WCB/WCB.ino:267` — CONFIRMED · S2 → **S3**

**etmMissedHeartbeats default 5 is undone at boot by loadETMSettings()'s NVS fallback of 3 — the documented 55 s offline window never applies to a fresh board**

**Correction to the finding as filed:**

The observation and location are right; the fix is under-scoped and the severity is one notch high. Three corrections:

1. THE ONE-LINER IS NECESSARY BUT NOT SUFFICIENT. The Wizard is the normal configuration path and it writes 3 back over the firmware default: `Wizard/parser.js:1491` emits `ETM,MISS,${etm.missedHeartbeats}` on any full push, and the value defaults to 3 in six places — `Wizard/parser.js:122`, `:176`, `:828` (`parseInt(parts[2]) || 3`), `Wizard/app.js:1424` (`?? 3`), `:9302` (`wizardState.etmConfig … missedHeartbeats:3`), `:10782` (`?? 3`). All six must move to 5 in the same change or a Wizard-configured fleet still runs 33 s.

2. EXISTING BOARDS ARE NOT FIXED BY EITHER CHANGE. Every `?ETM,*` handler calls `saveETMSettings()`, which writes all eight keys from the runtime values `loadETMSettings()` had already set — so any board that has ever been given any ETM command already has `etmMiss = 3` persisted. Those boards need an explicit `?ETM,MISS,5` (or an NVS erase) after flashing. That belongs in the release note; do NOT add a "if stored == 3, rewrite to 5" migration — 3 is a legal user-selectable value via `?ETM,MISS` (`WCB.ino:4688`, legacy `:5325`), so a migration cannot tell a stale default from a deliberate setting.

3. "the documented 55 s offline window" overstates it. grep over `docs/*.md` finds no ETM-timing page; 55 s appears only in the WCB.ino:267 comment and commit 3d88bdd. Per CLAUDE.md a source comment is a constraint, so the intent claim stands — but "documented" here means the comment, not a doc page.

The reviewer's parenthetical about validating `?ETM,MISS` is a separate, legitimate finding and should be tracked on its own: `WCB.ino:4688` and `:5325` are both bare `toInt()` with no range check (contrast `COUNT` at `:4697`, which uses `constrain(…,10,200)`), so `?ETM,MISS,0` persists 0, makes `offlineThresholdMs` 0 at `:1040`, and marks every peer offline on every sweep.

<details><summary>Full verification reasoning</summary>

MECHANISM — every fact re-derived from source, not from the write-up.

1. `Code/WCB/WCB.ino:267` is verbatim `int etmMissedHeartbeats = 5;` followed by the comment "(etmHeartbeatSec+1)*5 = 55s offline window — widened from 3/33s so a few lost broadcast heartbeats on a busy mesh don't flap a live board (mirrors WCBClient _missedBeforeOffline)". Transcription in the finding is exact.

2. `Code/WCB/WCB_Storage.cpp:2224-2238` `loadETMSettings()` opens the namespace read-only (`preferences.begin("etm_config", true)`) and does `etmMissedHeartbeats = preferences.getInt("etmMiss", 3);` at :2229. Key `"etmMiss"` is 7 chars — well inside the 15-char NVS limit, so no truncation/aliasing angle. The assignment is unconditional, so the C++ initializer is discarded whenever the key is absent.

3. `loadETMSettings()` is called exactly once, unconditionally, in setup(): `Code/WCB/WCB.ino:7416` (right after `loadCommandDelimiter()` / `loadLocalFunctionIdentifierAndCommandCharacter()`, and BEFORE the `if (etmEnabled)` heartbeat init at :7418-7425). No guard, no early return, no clamp.

4. `saveETMSettings()` (`WCB_Storage.cpp:2211-2222`, writes all eight keys) has no boot-time caller. Grep over all of `Code/` returns only the `?ETM,*` handlers at `WCB.ino:4673,4677,4681,4685,4689,4693,4697,4701,4710,4714` and the legacy bare-verb handlers at `:5304,5308,5314,5318,5322,5326,5330,5334`. So on a never-reconfigured board the key genuinely does not exist and the fallback 3 wins.

5. The oversight is proved by the other seven keys: enabled true/true, etmBoot 2/2, etmHB 10/10, etmTimeout 500/500, etmCharCount 20/20, etmCharDelay 100/100, etmChksm true/true — every declaration at WCB.ino:264-271 matches its fallback at WCB_Storage.cpp:2226-2233. `etmMiss` is the only divergence.

6. INTENT — not a deliberate trade-off. `git log -S"etmMissedHeartbeats = 5"` gives 3d88bdd (2026-07-30) "WCB: widen ETM offline window to 5 missed heartbeats (33s -> 55s)"; body cites a real observed failure ("A busy mesh (20Hz rc_ch live-monitor) drops enough unACK'd broadcast heartbeats that a live board crosses the 33s (3-miss) offline threshold and flaps in/out of the roster"). `git show --stat 3d88bdd` = `Code/WCB/WCB.ino | 6 +++---`, one file — two of those lines are the version stamp. WCB_Storage.cpp was never touched. The sibling side WAS updated: `../WCBClient/src/WCB_Client.h:1086` `uint8_t _missedBeforeOffline = 5;` with the same rationale, so the firmware today disagrees with its own client library.

7. CONSEQUENCE is real, not cosmetic. `WCB.ino:1040` `offlineThresholdMs = (unsigned long)(etmHeartbeatSec+1) * etmMissedHeartbeats * 1000UL` → 11*3 = 33 s, not 55 s. A falsely-offline peer: is skipped when snapshotting the expected-ACK set (`WCB.ino:1204 if (!boardTable[b].online) continue;`) so sends to it are best-effort with no ETM retry; has in-flight retries cancelled (`:1292-1296`); and reads false from `wcbPeerOnline()` (`:6879`), which gates `wdpCapOwner()` capability routing, so a live board can lose capability ownership. Plus OFFLINE/ONLINE console churn (`:1046-1048`, `:3775`).

WHY S3 RATHER THAN S2. Three things the write-up did not weigh. (a) Presence is refreshed on EVERY valid received ETM packet, not just heartbeats (`WCB.ino:3766-3770`, with a comment recording exactly that reason), so only pairs whose sole traffic is the broadcast heartbeat are exposed. (b) The commit itself says "Pairs with the NaviCore rc_ch 20Hz -> 5Hz default that removes the congestion source" — the trigger was independently fixed. (c) This is not a new regression: 33 s is the behaviour that shipped for months; the bug is that an intended tuning change silently never took effect. No crash, no data loss, no wire-format or fleet-consistency issue (`etmMissedHeartbeats` is board-local — read only at :1040, :2255, :2869; it is not in any packet). Worth fixing, but it does not rise to S2.

REACHABILITY of the required state: trivially yes — every freshly flashed board, and every board after `eraseNVSFlash`.

</details>

### `Code/WCB/WCB.ino:1011` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**scheduleNextHeartbeat computes a wrapped negative interval when etmHeartbeatSec <= 0, causing a heartbeat broadcast every loop iteration**

**Correction to the finding as filed:**

Mechanism is right; magnitude, trigger and blast radius all need correcting.

Trigger: the realistic path is the bare query `?ETM,HB` (no value) — WCB.ino:4665-4669 yields `etmVal=""` and `String::toInt()` returns 0 (WString.cpp:904-909). Help documents no query form (WCB_Help.cpp:285), so a user reading the setting silently writes 0 to NVS. Legacy `?ETMHB` is identical (WCB.ino:5320). Not "operator types 0 to disable", and NOT via a Wizard config restore of 0 — `Wizard/parser.js:827` coerces 0 back to 10. A Wizard-pushed NEGATIVE is possible (`Wizard/index.html:174` has no `min`; `Wizard/app.js:1216` `|| 10` passes −5 through).

Rate: with hb=0 the draw is uniform over [−1000,999], so ~half the draws fire immediately — bursts of ~2 sends every ~500 ms, ≈4 heartbeats/s versus 0.1/s. Not loop rate, not channel saturation. Only hb < 0 gives every-loop broadcast (hb=−1 → random(−2000,0), all draws negative).

Bigger omission: the same unvalidated value also drives `offlineThresholdMs` at WCB.ino:1040. hb=0 → 5000 ms (miss=5, WCB.ino:267) against a ~10 s peer heartbeat, so the board marks EVERY peer permanently offline. That falsifies `wcbPeerOnline()` (WCB.ino:6868-6879) and therefore `routeStoredOrCap`'s dead-pin failover (WCB.ino:5835) and `wdpCapOwner()` election (WCB_WDP.cpp:443) — capability triggers stop routing to their real hosts. A persisted negative inverts it (hb=−5 → 4294947296 ms, peers never go offline). Any fix must cover line 1040, not just line 1011.

The `isBoot` branch at WCB.ino:1008 is safe and needs no change: `random(1000, 0)` returns 1000 via the `howsmall >= howbig` guard.

Recommended fix instead of the proposed one: (a) at both setters, reject an empty/non-numeric arg — print `Use: ?ETM,HB,<sec>` plus the current value — rather than storing `toInt()`'s 0; (b) `constrain()` a real value to 2..3600; (c) clamp once in `loadETMSettings()` (WCB_Storage.cpp:2228) so a bad value already in NVS from an older build is neutralised for every consumer — WCB.ino:1011, :1040, the `?config` report at :2252-2255 and the backup emit at :2868. The same empty-arg hole exists for `?ETM,TIMEOUT`, `?ETM,MISS`, `?ETM,BOOT` and `?ETM,DELAY` (WCB.ino:4679-4701) and is worth closing in the same pass.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived, holds.

`Code/WCB/WCB.ino:1004-1017` is verbatim as quoted; `etmHeartbeatSec` is a plain `int` (WCB.ino:266, `int etmHeartbeatSec = 10;`). `random(long,long)` in the pinned core is exactly as transcribed — `C:/Users/ghulette/AppData/Local/Arduino15/packages/esp32/hardware/esp32/3.3.4/cores/esp32/WMath.cpp:64-70` (`if (howsmall >= howbig) return howsmall; long diff = howbig - howsmall; return random(diff) + howsmall;`), declared `long random(long,long)` at `Arduino.h:162`. No local override in `Code/` (grepped `long random` / `#define random` — nothing).

I re-ran the 32-bit arithmetic on the host rather than trusting the numbers:
- hb=0: `(0-1)*1000UL` = 4294966296 → `long` −1000; `(0+1)*1000UL` = 1000. `random(−1000,1000)` → uniform [−1000, 999].
- hb=−1: lo=−2000, hi=0 → every draw in [−2000,−1], all negative.
- hb=−5: lo=−6000, hi=−4000, all negative.
`etmNextHeartbeatMs` is `unsigned long` (WCB.ino:497); `millis() + (unsigned long)(−500)` wraps to `millis()−500`, so `millis() >= etmNextHeartbeatMs` (WCB.ino:1034) is immediately true. Confirmed numerically.

The `isBoot` branch (WCB.ino:1008) is NOT affected: `random(1000, 0)` hits `howsmall >= howbig` and returns 1000.

REACHABILITY — real, and easier than the finding says.

Both setters are unvalidated as claimed: WCB.ino:4683-4686 and WCB.ino:5320-5323. `saveETMSettings` writes key `etmHB` (WCB_Storage.cpp:2215, 5 chars, well under the 15-char NVS limit) and `loadETMSettings` reads it back with no clamp (WCB_Storage.cpp:2228), so it survives reboot. `scheduleNextHeartbeat` is called from `processETMHeartbeats` (WCB.ino:1036), which runs every `loop()` (WCB.ino:7482), gated only on `etmEnabled` (WCB.ino:1020) — and WCB CLAUDE.md rule 4 says ETM is effectively mandatory (WDP needs it).

The most likely trigger is not "operator types 0 to disable". It is the bare query: `?ETM,HB`. Arg splitting at WCB.ino:4665-4669 gives `etmCmd="HB"`, `etmVal=""`; `String::toInt()` on an empty buffer returns 0 (`WString.cpp:904-909`). There is no query form for the interval — `WCB_Help.cpp:285` documents only `?ETM,HB,15` — so a user typing `?ETM,HB` to *read* it silently stores and persists 0. Legacy `?ETMHB` has the identical hole (WCB.ino:5320, `substring(5)` of "etmhb" is ""). `?ETM,HB?` is safe: intercepted as help at WCB.ino:4401.

Wizard: negatives are reachable fleet-wide — `Wizard/index.html:174` `<input type="number" id="g-etm-hb">` has no `min`, `Wizard/app.js:1216` is `parseInt(...) || 10` (maps 0→10 but passes −5), and `Wizard/parser.js:1490` emits `ETM,HB,${...}` verbatim. But 0 does NOT replicate through a backup: `Wizard/parser.js:827` is `parseInt(parts[2]) || 10`, so a `ETM,HB,0` in a pulled config is read back as 10.

INTENT — nothing deliberate. `git log -L 1004,1017` shows the lines have been untouched since the original `cc6c078 "ETM"` commit; the only comment is a restatement of the intended range. No doc anywhere states a valid range.

TWO CORRECTIONS TO THE REVIEWER'S IMPACT CLAIM.

(1) "broadcast at loop rate / saturating the TX queue and channel" is wrong for the headline case hb=0. The draw is uniform over [−1000, 999]; ~50% of draws are ≤0, so the pattern is a geometric run of ~2 back-to-back sends followed by a wait uniform in 1..999 ms — roughly 4 heartbeats/s versus the normal 0.1/s. A 252-byte frame at 4/s is ~40x normal, not channel saturation. The "every loop iteration" claim is only true for a NEGATIVE `etmHeartbeatSec`, where every draw is negative.

(2) The reviewer missed the consequence that actually breaks function, and it comes from the *same* unvalidated value. At WCB.ino:1040, `offlineThresholdMs = (unsigned long)(etmHeartbeatSec + 1) * etmMissedHeartbeats * 1000UL`. With hb=0 that is 5000 ms (miss=5, WCB.ino:267; 3000 ms with the NVS default 3 at WCB_Storage.cpp:2229) while peers only beat every ~10 s — so this board declares every peer permanently OFFLINE and flaps them. That poisons `wcbPeerOnline()` (WCB.ino:6868-6879), which gates the "genuinely dead pin" failover in `routeStoredOrCap` (WCB.ino:5835), `wdpCapOwner()` election (WCB_WDP.cpp:443, `if (!wcbPeerOnline(nb.wcbNumber)) continue;`) and the ETM-char peer list (WCB.ino:1448). A persisted negative is worse in the other direction: hb=−5 gives `offlineThresholdMs` = 4294947296 ms (~49.7 days), so peers never go offline.

SEVERITY — S3, not S2. It is real, silent at the moment of the mistake ("ETM heartbeat set to 0 sec" is the only signal), and persists in NVS across reboots — that keeps it above S4. But the trigger is out-of-range operator input, not a normal-operation path; there is no crash, corruption or data loss; the console fills with "[ETM] WCBn went OFFLINE" which makes it discoverable; and recovery is one command (`?ETM,HB,10`).

</details>

### `Code/WCB/WCB.ino:1396` — CONFIRMED · S2 → **S3**

**etmSendAck sends the ACK without ensuring the ESP-NOW peer exists, so ACKs to unregistered senders are silently dropped**

**Correction to the finding as filed:**

Two corrections to the finding as written.

1. Consequence. The claimed failure implies the ensured command fails. It does not — Board B executes it (etmSendAck at WCB.ino:3823 runs before execution and its failure aborts nothing), and A's 3 retries are dedup-suppressed by the 8-deep seq ring (WCB.ino:558, :3831-3849), so there is no double-fire either. What actually breaks is the confirmation: a false "[ETM] WCB%d failed to ACK seq %d after 3 retries" (WCB.ino:1347), 3 wasted retransmissions, inflated etmStatsFailed[] skewing ?STATS (:1345/:1696), and a pending slot held ~2 s out of ETM_PENDING_MAX=10 (:508, etmTimeoutMs=500 at :268). Broadcast impact is narrower than implied: etmAddToPendingTable:1212 already drops learned-but-unreciprocated peers from expectAckFrom, so only floor members can wedge a broadcast.

2. Duration. WDP auto-join is ON by default (WCB_WDP.cpp:49/:1283) on a 60 s advert backstop (:52) with >=2-advert vetting (:657), and the learned set persists to NVS across reboots, so in a stock fleet the gap closes in ~60-120 s from first contact. The permanent cases are ?WDP,AUTOJOIN,OFF (:1229), ?WDP,OFF (:1219), a peer whose firmware doesn't advertise WDP, the 180 s temporary-peer eviction window (WCB.ino:1070-1088), or a full 20-slot peer table. Hence S3, not S2.

3. One amendment to the fix. Keep part (a) verbatim — it is right. Drop part (b), the unconditional non-debugETM warning on result != ESP_OK: etmSendAck runs on the ESP-NOW receive callback (WiFi task), and this codebase treats unconditional Serial writes from that task as a defect in its own right — WCB_OTA.cpp:447-451 ("This runs in the ESP-NOW receive callback (WiFi task). ESP32 Serial isn't atomic across cores … Defer the print to loop()"), same reasoning at WCB.ino:3908-3912 for the RC-JSON relay. In the exact bad state this finding describes it would also fire once per received command. Either leave the log under debugETM as it is today, or route it through the existing deferred print queue.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived from source, not the summary.

`etmSendAck` is `Code/WCB/WCB.ino:1384-1401`. The body is exactly as claimed: bounds check `senderWCB < 1 || > MAX_WCB_COUNT` (:1385), build the ACK struct, then `uint8_t *mac = WCBMacAddresses[senderWCB - 1];` (:1396) and a bare `esp_now_send(mac, …)` (:1397) whose `result` is only printed under `debugETM` (:1398-1400). There is no `esp_now_is_peer_exist` / `esp_now_add_peer` anywhere in the function, and no comment explaining the omission — `grep esp_now_is_peer_exist Code/WCB/` returns 11 hits, none in etmSendAck.

That `esp_now_send` to an unregistered unicast peer transmits nothing is not my assumption — this repo asserts it three times, each recording a real diagnosed failure:
- `WCB.ino:1323-1326` (the retry path, 70 lines above): "The unicast peer may have been evicted from the ESP-NOW table (~20 cap) … esp_now_send would then fail every retry and burn the whole budget." Added by commit a32e9cb (git log -L 1320,1340).
- `WCB_RemoteTerm.cpp:100-110`: "a relay numbered above this board's WCBQ … may not be in the peer table, and esp_now_send would then fail ESP_ERR_ESPNOW_NOT_FOUND and silently drop every mirrored line. Mirrors the on-demand registration the ETM retry uses."
- `WCB_OTA.cpp:329-344` (`otaEnsurePeer`): "esp_now_send() only reaches a REGISTERED peer. Normal unicasts register the target via addActivePeer(); the OTA send paths did not — so a target (or relay) that wasn't already mutually peered couldn't unicast the OTA ACK / frame, the send returned ESP_ERR_ESPNOW_NOT_FOUND and was silently lost, and the Wizard saw 'no response … via relay'."

That last one also proves the asymmetric state is real on this fleet: the target *received* a unicast frame from a board it had not peered, and could not reply. So receive-from-unpeered works; send-to-unpeered does not. etmSendAck is the one remaining reply path without the guard.

REACHABILITY — traced the full receive path. `espNowReceiveCallback` (:3584) has only one early filter: the MAC-group octet check (:3593). No peer-table membership gate anywhere. In the ETM branch the sender is validated (:3738-3745) and MAC-bound (:3756-3760), then presence is refreshed only `if (wcbPeerActive[senderIdx] || isSpecial)` (:3766) — a non-member falls straight through. In the COMMAND branch the only gate before the ACK is `if (targetWCB != 0 && targetWCB != WCB_Number) return;` (:3808), then `etmSendAck(...)` at :3823. Confirmed: membership is never consulted before the ACK.

Boot registers only ids 1..Default_WCB_Quantity (:7348-7364, `Default_WCB_Quantity` defaults to 1 at :135) plus the special peer (:7367-7384); everything else comes from `addActivePeer` (:6905-6935) / `addTemporaryPeer` (:6944-6975), both transactional and both able to fail on the 20-peer cap (MAX_WCB_COUNT = 20, :120). So a sender whose id exceeds the receiver's WCBQ and hasn't been learned gets no ACK. Reviewer's cited line numbers all check out.

WHERE THE REVIEWER OVERSTATES IT — the window normally self-closes. `wdpAutoJoin` defaults true (WCB_WDP.cpp:49, :1283), `wdpEnabled` defaults true (:48, :1282), the advert backstop is `WDP_PERIOD_MS = 60000` (:52), vetting is `>=2 adverts` (:657), and the learned set is persisted to NVS and re-registered at boot (`loadLearnedPeers`, WCB.ino:7396+). So in a default fleet the gap is ~60-120 s from first contact and then permanent-fixed. It stays permanent only under: `?WDP,AUTOJOIN,OFF` (WCB_WDP.cpp:1229), `?WDP,OFF` (:1219), a peer that doesn't advertise WDP (mixed fleet — the code already contemplates "non-reciprocating fw", WCB.ino:1211), the 180 s temporary-peer eviction window (WCB.ino:1070-1088), or a genuinely full peer table.

WHERE THE REVIEWER OVERSTATES THE CONSEQUENCE — the command is NOT lost. `etmSendAck` fires before execution and its failure doesn't abort anything; the receiver executes normally. Retries carry the same seq and are caught by the 8-deep per-sender ring (`ETM_SEQ_HISTORY = 8`, :558-559; check at :3831-3849) so there is no double-fire. Net damage: a false `[ETM] WCB%d failed to ACK seq %d after 3 retries` (:1347), 3 wasted unicast retransmissions per command, `etmStatsFailed[]` inflated so `?STATS` misreports mesh health (:1345, printed :1696), and a pending slot held ~4 × `etmTimeoutMs` (500 ms default, :268) out of `ETM_PENDING_MAX` 10 (:508). For broadcasts the blast radius is smaller than implied: `etmAddToPendingTable:1212` already excludes learned-but-unreciprocated peers from `expectAckFrom`, so only floor members (id <= sender's WCBQ) can wedge a broadcast.

INTENT — no comment, doc, or CLAUDE.md rule defends the omission. The opposite: docs/CODE_REVIEW_2026-08.md:665 already lists this as F-008/S2 under "UNVERIFIED — verification never ran. Confirm before acting", i.e. it is an open unchecked item, not a dispositioned trade-off. Nothing periodically deletes an on-demand-added peer either: the `esp_now_del_peer` at :1083 is inside the temporary-peer TTL branch (gated on `wcbPeerTemporary[i]`), and `syncActivePeerRegistrations()` runs only on a `?WCBQ` change (:4780, :5464).

Verdict: real, reachable, unguarded, and precedented three times in this same file tree — CONFIRMED. Downgraded to S3 because no command is lost and the default configuration heals it within two advert periods; what it actually corrupts is the delivery-confirmation signal an operator uses to judge the mesh.

</details>

### `Code/WCB/WCB.ino:1473` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**ETM characterization phase 2 is labelled "Broadcast (no load)" but sends unicast — the broadcast measurement never happens**

**Correction to the finding as filed:**

Severity S3, not S2. Drop the stated consequence entirely: "Recommended ETM timeout" (WCB.ino:1826) is computed from `worstAvgLatency`, a maximum accumulated across all three phases and all boards at WCB.ino:1819-1821, so an optimistic phase-2 row provably cannot tighten it, and phase 3 (which does broadcast, at :1509, under load) is inside the same max. Correct statement of harm: `?ETM,CHAR` prints — locally and over the MGMT relay — a row labelled "Broadcast (no load)" (WCB.ino:1547, :1798) containing unicast measurements taken at `etmCharDelayMs` pacing, i.e. phase 1 repeated at a different rate; and because phase 3 merges its broadcast and unicast samples into one set of per-board counters (WCB.ino:1506-1516), no isolated broadcast figure exists anywhere in the report. Also worth folding in: the same comment at :1548 claims it will "re-init results for phase 2" and does not — results are zeroed once in startETMChar (:1428). Cheapest correct fix is to relabel both sites honestly (e.g. "Unicast at configured pacing") and delete the stale comment; implementing real broadcast is the larger option and needs the three corrections below.

<details><summary>Full verification reasoning</summary>

OBSERVATION — VERIFIED, and every transcribed line is accurate.

Read Code/WCB/WCB.ino:1440-1553. `processETMChar()` handles phases 1 and 2 in one branch (`:1458`). The only thing phase 2 changes is pacing: `:1459` `unsigned long interDelay = (etmCharPhase == 1) ? 20UL : (unsigned long)etmCharDelayMs;` (`etmCharDelayMs` defaults to 100, `:270`). The send is `:1473` `sendESPNowMessage(targetWCB, payload.c_str(), true);`.

`targetWCB` can provably never be 0 (0 is the broadcast selector — `:514` "0 = broadcast, 1-MAX_WCB_COUNT = unicast target", and the guard at `:2275` `if (target == 0 && lastReceivedViaESPNOW) return;` keys on it). `peers[]` is filled at `:1447-1449` by `for (int i = 1; i <= MAX_WCB_COUNT; i++) ... peers[peerCount++] = i;` — the index starts at 1, so every entry is >= 1 (`MAX_WCB_COUNT` is 20, `:120`). `targetWCB = peers[peerIdx]` (`:1464`) is therefore always a unicast id. Phase 2 is unicast round-robin, full stop.

Contrast phase 3, which does broadcast properly: `:1501` `bool sendBroadcast = (msgIdx % 3 == 2);` → `:1507-1509` credit `.sent++` to every peer then `sendESPNowMessage(0, payload.c_str(), true)`.

LABELS — verified by hand, both sites:
- `:1547` `Serial.printf("Phase 2 - Broadcast (no load)...\n");`
- `:1548-1549` the comment "Phase 2 is actually broadcast to all — re-init results for phase 2 / We'll send to broadcast but track per-board via ACKs"
- `:1796-1800` `const char* phaseNames[]` with `"Broadcast (no load)"` at `:1798`, printed per phase at `:1802-1803` in `buildETMCharResultsString`.
Note the comment also promises to "re-init results for phase 2" and does not do that either — rows are zeroed once in `startETMChar` (`:1428`).

INTENT — not a deliberate trade-off. `git log -L 1546,1552:Code/WCB/WCB.ino` shows the block arrived whole in cc6c078 ("ETM", 2026-02-24) with the comment already stating an intent ("We'll send to broadcast") that the send never implemented. There is no comment anywhere explaining a decision to keep it unicast. Nothing in docs/ or the wiki (`../Wireless_Communication_Board-WCB.wiki/ETM.md:170-200`) describes phase 2 as broadcast — the wiki only says "Sends a configurable number of test messages to each peer", so the false claim is confined to the firmware's own console/relay output. (The `docs/CODE_REVIEW_2026-08*.md` files are untracked — `git ls-files docs/` does not list them — i.e. artifacts of this review run, so I derived everything independently rather than leaning on them.)

REACHABILITY — real and user-facing. `startETMChar()` entries: `?ETM,CHAR` (`:4704`), legacy `etmchar`/`ETMCHAR` (`:5310-5311`), and remote `handleETMReqPacket` → `startETMChar()` (`:3245-3246`). `processETMChar()` is driven from `loop()` (`:7488`). Guards at `:1406-1412` are only `etmEnabled` and `Default_WCB_Quantity >= 2`; nothing suppresses or relabels the phase-2 row. Results also go back over the relay as fragments (`:1849-1856`).

WHERE THE FINDING IS WRONG — the claimed failure mechanism does not exist. It asserts the optimistic "Broadcast" row feeds "Recommended ETM timeout" (`:1826`) and yields a timeout tuned too tight. I read `buildETMCharResultsString`: the recommendation is a MAXIMUM over every phase and every board — `:1819-1821` `if (r.latencyMax > worstMaxLatency) …; if (avgLatency > worstAvgLatency) …;` inside the `for (int p = 0; p < 3; p++)` loop (`:1801`), then `:1824` `recommendedTimeout = ((worstAvgLatency * 5) / 50 + 1) * 50;` floored at 150 (`:1825`). A low phase-2 average cannot lower a max, and phase 3 — which does emit real broadcasts (`:1509`) and runs under load — is inside that same max. The tool's actionable output is not degraded by the mislabel.

So the residual harm is exactly one thing: `?ETM,CHAR` prints a row titled "Broadcast (no load)" whose numbers are unicast — phase 1 repeated at `etmCharDelayMs` pacing — and no phase in the report ever isolates broadcast behaviour, because phase 3 folds its broadcast and unicast samples into the same per-board counters (`:1506-1516`). Diagnostic misreporting, no data-plane effect, no memory unsafety, no NVS/config impact. That is S3, not S2.

</details>

### `Code/WCB/WCB.ino:1798` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**ETM characterization phase 2 sends unicast, but the code comment and the results table both report it as "Broadcast"**

**Correction to the finding as filed:**

Real, but downgrade to S3 and drop the stated consequence. The recommended-timeout path is a max over all phases and boards (WCB.ino:1819-1824), so an optimistic phase-2 row provably cannot tighten the recommendation — the reviewer's "timeout tuned too tight" mechanism does not exist. The correct statement of harm is narrower: `?ETM,CHAR` prints a row labelled "Broadcast (no load)" containing unicast measurements taken at `etmCharDelayMs` pacing, i.e. phase 1 repeated at a different rate, and no phase in the report ever isolates broadcast behaviour (phase 3 merges its broadcast and unicast samples into the same per-board counters at WCB.ino:1506-1516).

Preferred fix is the rename, not the re-implementation: change `phaseNames[1]` (WCB.ino:1798) and the printf at WCB.ino:1547 to something honest such as "Unicast at configured delay (no load)", and delete the false comment at WCB.ino:1548-1549.

That comment deletion has a cross-file consequence the reviewer only noted in passing and which must be part of the same commit: CLAUDE.md:31 quotes that exact comment ("send to broadcast but track per-board via ACKs") as `WCB.ino:1484` — a stale line number (1484 is the `phaseTimeout` arithmetic) — and uses it as the authority for rule 2, broadcast ACK fan-out. Rule 2 itself is true, but its evidence is the pending table, not this comment: for `targetWCB == 0`, `etmAddToPendingTable` (WCB.ino:1145) sets `entry.expectAckFrom[b]` for every online active peer (WCB.ino:1183-1208) on a single pending entry (`ETM_PENDING_MAX` is 10, WCB.ino:507-518). Repoint CLAUDE.md:31 at WCB.ino:1195-1208 rather than at a comment that is about to be deleted.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived from source, not the summary.

1. Phase 2 is provably unicast. `processETMChar()` handles phases 1 and 2 in one branch: `Code/WCB/WCB.ino:1458` `if (etmCharPhase == 1 || etmCharPhase == 2)`. Inside it the only send is `Code/WCB/WCB.ino:1472` `sendESPNowMessage(targetWCB, payload.c_str(), true);` where `targetWCB = peers[peerIdx]` (`:1464`). `peers[]` is filled at `:1447-1448` with `i` running 1..MAX_WCB_COUNT, so `targetWCB >= 1` and can never be 0. In `sendESPNowMessage` (`:2273`) the MAC is chosen at `:2313` / `:2345`: `uint8_t *mac = (target == 0) ? broadcastMACAddress[0] : WCBMacAddresses[target - 1];` — target != 0 is a plain unicast. There is no `sendESPNowMessage(0, …)` anywhere in the phase-1/2 branch (`:1458-1490`). The ONLY difference between phase 1 and phase 2 is the inter-message delay: `:1459` `unsigned long interDelay = (etmCharPhase == 1) ? 20UL : (unsigned long)etmCharDelayMs;` (etmCharDelayMs default 100, `:270`). Phase 2 is therefore phase 1 re-run at the configured pacing.

2. The labels are exactly as transcribed. `Code/WCB/WCB.ino:1547` `Serial.printf("Phase 2 - Broadcast (no load)...\n");`; `:1548-1549` the comment "Phase 2 is actually broadcast to all" / "We'll send to broadcast but track per-board via ACKs"; `:1796-1800` the `phaseNames[]` array with `"Broadcast (no load)"` on `:1798` (line number in the finding is correct — I counted it: `:1796` is `const char* phaseNames[] = {`, `:1797` Individual Baseline, `:1798` Broadcast). Note the comment also claims it will "re-init results for phase 2" and does not do that either (rows are zeroed once in `startETMChar`, `:1428`).

3. Phase 3 really does broadcast, which proves the pattern was available and simply not used in phase 2: `:1502` `bool sendBroadcast = (msgIdx % 3 == 2);`, `:1506-1509` stamps the sent time, bumps `.sent` for every peer, then `sendESPNowMessage(0, payload.c_str(), true)`.

REACHABILITY — real. `startETMChar()` entry points: `?ETM,CHAR` (`:4703-4704`), legacy `etmchar`/`ETMCHAR` (`:5310-5311`), and remotely via `handleETMReqPacket` → `startETMChar()` (`:3245-3246`). The Wizard exposes both: `Wizard/app.js:11953` (`${lfi}ETM,CHAR`) and `:11982` (`MGMT,ETM,CHAR,${targetWCBNum}`). Guards at `:1406-1412` are only `etmEnabled` and `Default_WCB_Quantity >= 2`; nothing suppresses the phase-2 row. The wiki (`../Wireless_Communication_Board-WCB.wiki/ETM.md:167-200`) tells users to run `?ETM,CHAR` before production, so the mislabeled row is squarely user-facing.

INTENT — not a deliberate trade-off. `git log -S "Phase 2 - Broadcast" -- Code/WCB/WCB.ino` returns exactly one commit, cc6c078 (2026-02-24, "ETM"), and the diff shows the "Phase 2 is actually broadcast to all" comment landing in the same commit as the unicast-only send. It was never broadcast; the comment was aspirational from day one. No comment or doc anywhere records a reason for the phase to be unicast.

WHERE THE REVIEWER OVERSTATES IT — the failure scenario's causal chain is wrong. It claims the optimistic "Broadcast" row feeds "Recommended ETM timeout" and produces a timeout "tuned too tight". The recommendation is a MAXIMUM over every phase and every board: `:1819-1821` `if (r.latencyMax > worstMaxLatency) …; if (avgLatency > worstAvgLatency) …;` then `:1824` `recommendedTimeout = ((worstAvgLatency * 5) / 50 + 1) * 50;` (floored at 150). A too-low phase-2 average cannot lower a max, and phase 3 — which does contain real broadcasts — is included in that max. So the actionable output of the tool is not degraded by the mislabeling. The residual harm is exactly one thing: the report presents a row titled "Broadcast (no load)" whose numbers are unicast, and the operator who ran the test specifically to compare broadcast vs unicast gets a duplicate unicast measurement. (Worth adding: phase 3 folds its broadcast and unicast sends into the SAME per-board counters at `:1506-1516`, so an isolated broadcast figure exists nowhere in the report.)

That is a diagnostic/labelling defect — no memory corruption, no wire-format change, no mesh behaviour change, no unsafe recommendation. S3, not S2. (The genuinely S2-class problem in this same function is the separate unbounded index into `etmCharPhaseSentTimes[3][200]` declared at `:577`, which is already tracked elsewhere in docs/CODE_REVIEW_2026-08_wave1.md; it is not this finding.)

</details>

### `Code/WCB/WCB.ino:1920` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**enqueueCommand snapshots lastReceivedViaESPNOW/inSequenceBody from unsynchronised globals while running on the WiFi (Core 0) task**

**Correction to the finding as filed:**

Three corrections.

1. The title attributes the defect to the wrong side. `enqueueCommand` running on Core 0 is not itself the problem — the WiFi callback sets the global at :3883/:4159 and snapshots it at :1920 on the SAME task, so that path is internally consistent. The defect is that `lastReceivedViaESPNOW`/`inSequenceBody` are unsynchronised shared mutable state written by three tasks across two cores (WiFi callback Core 0 at :2725/:3883/:4159; serialCommandTask Core 1 at :6404/:6408; loop task Core 1 at :7510/:7511), so ANY of them can clobber the value another has staged. Restating it that way also captures the reverse direction the reviewer omitted, which is arguably worse than the one described: a mesh-received broadcast snapshotted with espnowOrigin=false gets re-broadcast, costing an extra ETM fan-out (ACKed per board, retried 3x) and double-executing on peers.

2. The reviewer missed the dominant exposure. The enqueue window (:3883 → :3997, a few hundred microseconds of String work) is the SMALL one. The large one is dispatch: WCB.ino:7510-7511 writes both globals on the loop task and then `handleSingleCommand(commandStr, inItem.sourceID)` runs at :7512 for the entire duration of the command — serial writes, ETM sends with retries, milliseconds — while every reader listed above pulls the global directly. The WiFi task can flip it at any point in that window. Same root cause, ~10x the window, and completely untouched by the proposed fix.

3. Correct fix. The minimal change that matches this file's own established idiom is to stop the WiFi callback from ever writing these globals: give the ESP-NOW receive path a deferred queue carrying the command string plus its two origin bools (exactly the shape of `PendingTimerChainSlot` at :405-410 / `enqueuePendingTimerChain` at :434), and have loop() drain it alongside `drainPendingTimerChains`/`drainWdpPackets`/`drainMgmtReqs`. That removes Core 0 as a writer entirely, which is what actually makes both windows safe. The complete fix is to delete the globals and carry the two flags as per-command context through `handleSingleCommand` into WCB_Maestro/WCB_WLED/sendESPNowMessage/routeStoredOrCap/recallStoredCommand — larger, and a judgement call for pre-release.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived from source, every fact checked.

Declarations: `bool lastReceivedViaESPNOW = false;` (WCB.ino:165) and `bool inSequenceBody = false;` (WCB.ino:172). Both are plain `bool` — not `volatile`, not atomic. The ONLY synchronization primitive anywhere in Code/WCB is `static portMUX_TYPE etmAckQMux` (WCB.ino:1113), used for the ETM ACK queue; grep for portENTER_CRITICAL/taskENTER_CRITICAL/std::atomic/volatile bool returns nothing else touching these two globals. So there is genuinely no guard.

`enqueueCommand` (WCB.ino:1912) reads both: `item.espnowOrigin = lastReceivedViaESPNOW;` (:1920), `item.sequenceBody = inSequenceBody;` (:1923). Transcription in the finding is accurate.

TASK CONTEXTS — this is the load-bearing claim and it holds:
- `esp_now_register_recv_cb(espNowReceiveCallback)` at WCB.ino:7434; the callback body starts at :3584. It writes the globals at :3883-3884 (ETM COMMAND path, `lastReceivedViaESPNOW = !wizardOrigin; inSequenceBody = false;`), :4159-4160 (normal COMMAND path), and :2725-2726 (`handleMgmtPacket`, defined :2631, called from the callback at :3628). It then calls `parseCommandsAndEnqueue(...)` INLINE at :3997, :4227 and :2731 — no deferral.
- The file itself states the core: WCB.ino:3915-3918 — "we're on the WiFi task (Core 0) and Serial isn't atomic across cores. The main loop drains the queue safely on Core 1." Also :3979-3982 "this runs on the WiFi recv-callback task". So the Core 0 premise is the codebase's own, not the reviewer's assumption.
- `xTaskCreatePinnedToCore(serialCommandTask, "Serial Command Task", 4096, NULL, 1, NULL, 1)` — WCB.ino:7439, priority 1, CORE 1. Its `processIncomingSerial` writes `lastReceivedViaESPNOW = false` at :6404 and `inSequenceBody = false` at :6408, then calls `processSerialCommandHelper` → `parseCommandsAndEnqueue` at :6476/:6495 → `enqueueCommand`.
- The Arduino loop task (Core 1) writes both globals on EVERY dispatched queue item: WCB.ino:7510-7511.

Core 0 and Core 1 execute simultaneously, so there is no scheduler serialization at all between the WiFi callback and serialCommandTask/loop. The race is real.

FREQUENCY — I checked whether background chatter can trigger it. Heartbeats/boot announces return at WCB.ino:3790-3795 and WDP adverts at :3784-3787 WITHOUT touching the globals, so idle mesh traffic is safe. But the write at :3883 happens BEFORE the RC-JSON check at :3905 and its `return` at :3934 — so every rc_hb/rc_ch telemetry frame (the comment at :3866 says "up to 5 Hz") slams `lastReceivedViaESPNOW = true` on Core 0 of every board on the mesh. That is a continuous, real-world writer.

CORROBORATING INTENT — the authors have already fixed at least four races of exactly this shape by deferring WiFi-callback work to loop(): `etmEnqueueAck` (:3800, "never mutate the pending table from the WiFi task"), `enqueueWdpPacket` (:3785), `drainMgmtReqs`/`drainOtaPackets` (:7492-7493), and `enqueuePendingTimerChain` (:3995, :4225) which even carries its own per-slot `espnowOrigin`/`sequenceBody` snapshot (:405-410, :438-440). So the hazard class is acknowledged in this file; the command-enqueue path is simply the one instance that was not covered.

NOT DELIBERATE-AND-DOCUMENTED — the comments at :1917-1919 and :893-897 justify the snapshot against the *queue-boundary* race (async drain reordering), which is a different hazard. Nothing anywhere claims cross-task safety for these globals. git log -L on the function shows the snapshot was added in 4075057 ("Bug Fix") and extended in ea98f0f ("updates for sequence routing") — both about queue-boundary correctness, not concurrency.

WHY S3, NOT S2 — on Xtensa a `bool` is a byte; l8ui/s8i are single instructions, so there is no torn read. The worst case is a stale-but-valid value, and the only things these flags gate are forwarding/fan-out decisions (sendESPNowMessage :2275 `if (target == 0 && lastReceivedViaESPNOW) return;`, routeStoredOrCap :5829, recallStoredCommand :6119, WCB_Maestro.cpp:143/175/224/233/316/338/367/428, WCB_WLED.cpp:151). Consequence is one command routed wrong — either not fanned out to the mesh, or fanned out once extra (double-fire on peers). Transient, self-correcting on the next command, no corruption, no crash, no persisted state change. The window is sub-millisecond and requires concurrent local + mesh traffic. Real, worth fixing before release, but not S2.

WHAT THE REVIEWER GOT WRONG — see `correction`. The headline blames the wrong side of the race, misses the far larger dispatch-time window, and the proposed fix does not close it.

</details>

### `Code/WCB/WCB.ino:1953` — CONFIRMED · S2 → **S3**

**Backup checksum is written with the live function identifier but both verifiers hardcode "?CHK" — a custom-funcChar board's own backup can never be verified**

**Correction to the finding as filed:**

Three corrections. (1) Reachability is narrower than claimed: only ONE verifier is on the restore path. `:6440` sits behind `data.startsWith(String(LocalFunctionIdentifier) + "C")` at `:6436`, and the live backup chain begins `<lfi>HW,` because `collectConfigCommands` emits `HW,<v>` first (`:2776`) — so the `?C…` path never sees a backup paste. Both verifiers do hardcode `'?'` (that part is accurate), but the one that matters for a pasted backup is `:1953` alone; fixing `:6440` is correct hygiene but dead for this bug. (2) `?FUNCCHAR` is emitted at `WCB.ino:2800`, not :2799. (3) Add the intent evidence and the inverse-fix warning: `git log -L 5770,5780` shows `5e2f0d5` deliberately changed the live writer `"?CHK"` → `lfi + "CHK"`, so the writer is intentional and only the verifier lagged — fix the VERIFIER, never the writer. Emitting a literal `"?CHK"` in the live chain would be actively worse: if that token survives to dispatch on a custom-funcChar board it matches neither `LocalFunctionIdentifier` (`:4380`) nor `CommandCharacter` (`:4384`) and falls into `processBroadcastCommand` (`:4388`), pushing the checksum text out over the mesh and serial ports as data. Severity S3, not S2.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived from source, not the summary.

Writer: `Code/WCB/WCB.ino:5733` `String lfi = String(LocalFunctionIdentifier);`. The per-command emitter at `:5744-5750` appends `lfi + c` to the LIVE chain (`:5747`) and `defaultFunc + c` to the FACTORY chain (`:5748`). `:5772` `String checksumCmd = lfi + "CHK" + String(crcStrBuf);` and `:5778` `chainedWithChecksum = chainedConfig + String(commandDelimiter) + checksumCmd;`. The factory form at `:5776` is the fixed literal, and it carries the explaining comment "factory reset always uses ?" — that one is deliberate; the live one carries no such comment.

Verifiers: `grep -n "CHK" Code/WCB/*.{ino,cpp,h}` returns exactly six sites — `:1953, :1965, :2960, :5772, :5776, :6440` (plus unrelated `ETM,CHKSM`). Only two are verifiers: `:1953` `data.lastIndexOf(String(commandDelimiter) + "?CHK")` (lowercase fallback `:1955`) and `:6440` `String chkPattern = String(commandDelimiter) + "?CHK";` (fallback `:6443`). Both take `commandDelimiter` live and hardcode only the `'?'`, so the finding is right that this is an omission rather than a fixed grammar. `:2960` (`out += "^?CHK"` in `buildConfigString`) is the always-'?' Wizard pull grammar and is correctly fixed, as the finding concedes. `verifyBackupChecksum` (`:5792`) has no caller anywhere in the tree.

INTENT — this is an incomplete change, not a deliberate trade-off. `git log -L 5770,5780:Code/WCB/WCB.ino` shows commit `5e2f0d5 "Added Checksums to ETM option"` changed the live writer from `"?CHK" + …` to `lfi + "CHK" + …` and added the "factory reset always uses ?" comment to the *other* line in the same hunk. `git log -L 1945,1960:Code/WCB/WCB.ino` shows the verifier was `"?CHK"` from its introduction (`f5d758f`) and was only ever touched by `a32e9cb` (indexOf→lastIndexOf); it was never made funcChar-aware. So the writer was intentionally made live-identifier-based and the verifier was left behind.

REACHABILITY — traced the actual paste route, and one of the finding's two verifiers does NOT apply. `?FUNCCHAR,x` is a first-class supported setting: parser at `:5044-5052` (`args.length()==1` → `LocalFunctionIdentifier = args.charAt(0)`, `saveLocalFunctionIdentifierAndCommandCharacter()`), reloaded at boot (`:7415`), documented (`WCB_Help.cpp:886-899`, `:1075`), and the Wizard both reads it (`parser.js:324`, `:557`) and writes it (`parser.js:1303`, `app.js:7448`). `collectConfigCommands` emits it at `:2800` (the finding says :2799 — off by one) with `includeInLive=false`.
A pasted "For Configured Boards" chain enters `processSerialCommandHelper` (`:6423`). `collectConfigCommands` emits `HW,<v>` FIRST (`:2776`, the first `emit` in the function), so the live chain begins `<lfi>HW,` and the gate at `:6436` `data.startsWith(String(LocalFunctionIdentifier) + "C")` is FALSE — the `:6440` verifier is not on the restore path at all (it serves `?C…`/`?CS`-style chains, and no writer in the tree produces a `<lfi>C…` chain terminated by `<lfi>CHK`). The timer test at `:6489` is skipped because the chain starts with the funcChar, so control reaches `parseCommandsAndEnqueue` at `:6495`. There, with e.g. funcChar `'#'`, the chain ends `…^#CHK1A2B3C4D`; `lastIndexOf("^?CHK")` and `lastIndexOf("^?chk")` both return -1, so the entire block `:1958-1997` is skipped — including the "✗ Command checksum FAILED … Aborting restore" abort at `:1992-1996`. The chain is then split normally and every config token applied unverified. The leftover `#CHK…` token dispatches via `:4380` into `processLocalCommand("CHK1A2B3C4D")`, matches nothing in the modern verb dispatcher (no comma, so root is the whole token) nor the legacy chain (`cc`/`cs`/`ce`/`backup`/… `:5187-5348`), and lands on `Serial.printf("Unknown command: %s\n", …)` at `:5346`. Harmless, and it is the only symptom besides the missing "✓ Command checksum VERIFIED".
The manual serial paste is the sole consumer of this CRC — `grep -rn CHK Wizard/` yields only `ETM,CHKSM` strings and `parser.js:902 case 'CHK': break;`, so there is no second safety net.

So the defect is real and reachable by a real user with a supported setting. Downgrading to S3: it needs a non-default `?FUNCCHAR` AND an independently corrupted/truncated paste before anything bad happens; what is lost is a safety net, not correctness — the misapplied config is re-pastable over the same serial link, nothing on the wire or in the mesh format changes, and the operator does get a weak tell (no "✓ VERIFIED" line, plus a stray "Unknown command: CHK…"). Two independent uncommon conditions gating a recoverable outcome is S3, not S2.

</details>

### `Code/WCB/WCB.ino:1989` — CONFIRMED · S2 → **S3**

**parseCommandsAndEnqueue recurses into itself and re-enters the ?CHK branch, re-introducing the embedded-checksum abort its own comment says lastIndexOf fixed**

**Correction to the finding as filed:**

Two corrections to the finding.

1. The claimed failure scenario is wrong. "A board stores a sequence whose value contains a config chain fragment ending in ^?CHK1A2B3C4D" cannot happen on a default-delimiter board — that save line is itself intercepted at WCB.ino:1953 and rejected at :1995 before the ?SEQ,SAVE boundary logic at :2053 ever runs. The real preconditions are (a) a legacy value recovered verbatim by migrateOldStoredCommands (WCB_Storage.cpp:895/:919), or (b) the value was stored while commandDelimiter was not '^' (?DELIM), after which printBackupConfig's fixed-'^' factory chain (WCB.ino:5732) exposes it on restore to a fresh board.

2. Severity S2 -> S3. The failure is fail-safe: the restore aborts with an explicit message and applies nothing (WCB.ino:1992-1996). No corruption, no data loss, no crash, no unbounded recursion (depth is 2 — each pass strips >=5 chars and a mismatch returns immediately). The silent-truncation variant needs a CRC32 collision.

Also worth folding in while touching this: the sibling verifier at WCB.ino:6437-6479 still uses first-occurrence indexOf (:6441) and an unbounded checksum tail (:6451), i.e. the pre-a32e9cb shape. Fixing :1989 without consolidating that one leaves the original bug alive on the ?C-prefixed path.

<details><summary>Full verification reasoning</summary>

MECHANISM — verified, stands exactly as described.

`parseCommandsAndEnqueue` is `Code/WCB/WCB.ino:1945` (`const String &data, int sourceID`; also declared `extern` at `Code/WCB/WCB_Storage.h:75`). The prologue is unconditional: `firstDelim = data.indexOf(commandDelimiter)` (`:1947`), then `chkPos = data.lastIndexOf(String(commandDelimiter) + "?CHK")` (`:1953`) with the lowercase fallback at `:1955`. On a verify, `:1989` calls itself with `data.substring(0, chkPos)` (`:1978`) and returns. There is no flag, no parameter, and no state marking the checksum as already consumed — the second frame runs the identical prologue. So if the stripped string still contains a `^?CHK`, `:1953` matches it, `:1978-1979` CRC an arbitrary prefix of the chain, and `:1991-1997` prints "✗ Command checksum FAILED … Aborting restore" and returns having enqueued nothing. The `lastIndexOf` guard documented at `:1949-1952` is therefore exactly one level deep, while its comment claims the class of bug is fixed. Confirmed by `git log -L 1945,1999:Code/WCB/WCB.ino`: commit a32e9cb changed `indexOf`→`lastIndexOf` and added that comment, but left the `:1989` recursion untouched.

Two things the reviewer did not check, both of which I did:
- No unbounded recursion. Each pass strips ≥5 chars and a CRC mismatch returns immediately, so real depth is 2. Not a stack risk.
- No legitimate nested-checksum producer exists, so the recursion has no purpose other than reusing the parse loop. The only two writers append exactly one trailing token: `WCB.ino:2960` (`out += "^?CHK" + String(chkBuf)` in `buildConfigString`) and `WCB.ino:5772-5778` (`printBackupConfig`). The Wizard never emits a CHK at all — `grep -n CHK Wizard/*.js Wizard/index.html` returns only `parser.js:902 case 'CHK': break;`.

REACHABILITY — real but far narrower than the finding says; this is why I am downgrading.

The finding's own failure scenario does not work on a stock board. Saving that macro is blocked by the same branch it complains about: the line `?SEQ,SAVE,push,<…>^?CHK1A2B3C4D` reaching `:1947` has `firstDelim != -1`, `:1953` reads the embedded token as the chain checksum, CRC over `?SEQ,SAVE,push,<…>` mismatches, and `:1995` aborts the save. The `?SEQ,SAVE` boundary rule at `:2053-2059` (split only at `commandDelimiter + LocalFunctionIdentifier`) is never reached. Same for `?CS` (`:2018`→`storeCommand`, `:5189`) and for the legacy `?C…` path (`WCB.ino:6437-6479`, first-occurrence `indexOf` at `:6441`). Variables are ruled out entirely — values are `int32_t` (`WCB_Variables.cpp:135`, `:172`) and the backup emits `String((long)vars[i].value)` at `:484`, so a variable can never carry `^?CHK`.

Two paths I did verify can plant such a value:
1. `migrateOldStoredCommands` (`WCB_Storage.cpp:873-921`) copies legacy `stored_commands` / `stored_cmds` `CMD<n>` values verbatim into the live store via `saveStoredCommandsToPreferences(key + "," + value)` at `:895` and `:919`, bypassing every parser. A value written by pre-guard firmware survives intact and is then re-emitted by `collectConfigCommands` as `emit("SEQ,SAVE," + key + "," + value, true)` (`WCB.ino:2910`).
2. A non-default delimiter at save time. `:1953` searches `String(commandDelimiter) + "?CHK"` and `:2057` uses `commandDelimiter + LocalFunctionIdentifier`, so after `?DELIM,|` a literal `^?CHK` inside a sequence value is neither a checksum nor a boundary and stores verbatim. `printBackupConfig`'s factory chain uses a fixed `defaultSep = "^"` (`:5732`, deliberately, per the comment at `:5744-5747`), so the embedded `^?CHK` becomes a real `^`-delimited token in that chain — and restoring the factory chain onto a fresh board hits the bug.

Note the internal consistency argument that pushes this to CONFIRMED rather than REFUTED: the comment at `:1949-1952` is a first-person record of a chain that actually had an embedded `^?CHK` ("matching the first occurrence truncated the chain and aborted the whole restore"). If that chain existed, it still aborts today — the change moved the abort from pass 1 to pass 2, it did not remove it.

IMPACT — fail-safe, loud, recoverable, which is what caps this at S3. The console prints "✓ Command checksum VERIFIED - Configuration is intact" (`:1985`) immediately followed by "✗ Command checksum FAILED … Aborting restore to prevent corruption" (`:1992-1995`). Nothing is enqueued, no NVS write occurs, no crash. The contradictory output is confusing but diagnosable. The dangerous shape — pass 2 verifying and silently truncating the chain at `:1984-1990` — requires the embedded checksum to equal CRC32 of the full outer prefix, i.e. a deliberate craft or a 2^-32 collision.

Not S2: S2 implies functional breakage on a path a normal user hits. A stock-configured board cannot create the precondition through any current command path; it takes a legacy-NVS migration or a custom `?DELIM` plus a factory-format restore.

</details>

### `Code/WCB/WCB.ino:2599` — CONFIRMED · S2 → **S3**

**MGMT single-chunk relay prepends '\x01' but the non-ETM receive path never strips it — all single-chunk Wizard commands die when ETM is off**

**Correction to the finding as filed:**

Three corrections to the finding as written.

1. Severity: S3, not S2. The precondition is fleet-wide `?ETM,OFF`, which is non-default (WCB.ino:264) and which the Wizard itself states is incompatible with remote management (Wizard/app.js:10179) — a statement that predates the marker commit (ce97085 2026-03-03 vs 02cc8ed 2026-03-31). In that mode discovery, online tracking and the relay node list are already dead by design (WCB_WDP.h:20-21, WCB.ino:3769), so the feature is not "supported and silently broken" so much as "already unsupported and now failing dirtier".

2. The effect is worse than "the command dies". `"\x01?..."` matches neither `?` (WCB.ino:4379) nor `;` (:4384), so `handleSingleCommand` falls to case 3 and hands it to `processBroadcastCommand` (:4388), which writes the raw marked text out every non-device serial port (:6294+). Not just a no-op — visible junk on the target's ports. Mesh re-broadcast is still suppressed by the `lastReceivedViaESPNOW = true` at :4159.

3. Line anchors: the NUL-forcing line is WCB.ino:4172 and `String receivedCmd` is :4173 (finding said :4196). `sendESPNowMessage`'s ETM gate at :2281 also carries `&& !isPWMMessage` — inert for marked payloads, since the SOH makes `isPWMMessage` (:2278) false, but the finding should not present the condition as two-term.

Scope note the finding misses: only the single-chunk path is affected. Config pull, stats, sequence pull and multi-chunk push all use their own ETM-independent structs (:3440, :3455, :3479, :3516, :3529, :2609) and keep working with ETM off, which is exactly why the failure is confusing in the field — the board pulls fine and shows connected, then quietly ignores every push.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived from source, all of it holds.

1. `handleMgmtForward` (Code/WCB/WCB.ino:2535) single-chunk branch at :2597-2604 really does prepend SOH unconditionally and call `sendESPNowMessage(targetWCB, markedPayload.c_str(), true)` (:2598-2599). Reviewer's transcription is exact.
2. `sendESPNowMessage` (:2273) gates the ETM branch on `if (etmEnabled && useETM && !isPWMMessage)` (:2281) — the reviewer omitted `!isPWMMessage`, but that term is inert here: with the marker in place `message[0]=='\x01'`, so `isPWMMessage` (:2278) is always false for marked payloads. With `etmEnabled == false` the function falls through to the NORMAL send path (:2330-2353) and copies the SOH-prefixed string straight into `msg.structCommand` (:2342). Both structs declare `char structCommand[200]` (:209 ETM, :782 normal) so there is no truncation — the marker simply rides along.
3. The strip exists in exactly one place. My own grep for `x01|SOH|0x01` over WCB.ino returns only :2593, :2598, :3876, :3881 — matching the reviewer. `bool wizardOrigin = (etmCmd.length() > 0 && (uint8_t)etmCmd[0] == 0x01);` at :3881, `if (wizardOrigin) etmCmd = etmCmd.substring(1);` and `lastReceivedViaESPNOW = !wizardOrigin;` at :3882-3883 — inside the ETM RECEIVE branch only.
4. The non-ETM receive path has no equivalent: `lastReceivedViaESPNOW = true;` (:4159), NUL-forcing (:4172), `String receivedCmd = String(received.structCommand);` (:4173), then straight to `parseCommandsAndEnqueue(receivedCmd, 0)` (:4227). (The reviewer cited :4196 for the NUL line; actual is :4172 — citation drift, not a mechanism error.)
5. I followed the SOH string through the parser to confirm it actually dies. `parseCommandsAndEnqueue` (:1945): no delimiter → no `?CHK` branch; `restOfString.startsWith("?CS")` false; `restUpper.startsWith("?SEQ,SAVE,")` false; `"?MGMT,"` false; falls to the generic branch (:2096-2113) where `singleCmd.trim()` runs — Arduino `String::trim()` uses `isspace()`, and `isspace(0x01)` is false, so the marker survives. `handleSingleCommand` (:4354) then tests `startsWith("?")` (:4379) and `startsWith(";")` (:4384), both false, and falls to case 3 `processBroadcastCommand(cmd, sourceID)` (:4388). So the outcome is worse than "dies": the raw `"\x01?RTERM,START,1"` text is emitted as a broadcast word out every non-device serial port (:6294 onward). Mesh re-broadcast is suppressed because :4159 already set `lastReceivedViaESPNOW = true`, so no storm.

REACHABILITY — real but narrow.
- `?ETM,OFF` (:4676) and `;etmoff` (:5333) both exist, persist via `saveETMSettings()`, and print no warning. Default is ON (`bool etmEnabled = true;` :264, "NVS default is also true").
- Both endpoints must be ETM-off. Mixed states fail earlier for unrelated, documented reasons: ETM-on relay → ETM-off target is dropped at :3684; ETM-off relay → ETM-on target is dropped at :3708 (marked payload is not raw/PWM/Maestro/RC-JSON — `peek.structCommand[1]` is `'?'`, not `'M'`).
- The Wizard can be armed for relay management with no ETM check at all: `openConnectModal` (Wizard/app.js:5812) lists "Remote via WCB r" for every USB-connected board (:5829) → `modalRemoteConnect` (:5938) → `setRemoteConnected` + `remoteBoardPull`. So a user in ETM-off mode can get to this state deliberately.
- Everything else in remote management survives ETM-off, which is what makes the failure confusing rather than obvious: `?MGMT,PULL`/`STATS`/`SEQ`/`SEQGET` all use their own broadcast structs with no ETM involvement (:3440, :3455, :3479, :3516, :3529), and multi-chunk push uses `espnow_struct_mgmt` (:2609-2620) whose receiver `handleMgmtPacket` never consults `etmEnabled` and deliberately sets `lastReceivedViaESPNOW = unicastOrigin` (:2724). So a config pull and a full config push work, while every single-chunk op silently no-ops.
- The single-chunk sites the reviewer lists are real: Wizard/app.js:4455 (seq save), :8901 (remote terminal send), :2879 (WLED), :3650 (mapping clear), :6072 (RTERM,START). `sendMgmtReliable` (:7682) has no target-side ACK, so the failure is silent to the Wizard.

INTENT — this is why I downgrade rather than confirm at S2.
- The comment block at :2588-2596 explains the marker's purpose (reset `lastReceivedViaESPNOW` so wizard-relayed plain-text commands can still fan out to peers, matching the FRAG semantics at :2717-2724). It does not address ETM-off; the fall-through is unhandled, not deliberate.
- But ETM-off + remote management is already documented as unsupported, and that documentation predates the marker. Wizard/app.js:10179: "ETM is enabled by default and is required for remote management of WCBs over ESP-NOW" — added in ce97085 (2026-03-03), while the marker arrived in 02cc8ed (2026-03-31, `git log -L 2588,2604:Code/WCB/WCB.ino`). ce97085 is an ancestor of 02cc8ed.
- ETM-off also structurally degrades the feature independently of this bug: `boardTable[i].online = true` is written in exactly one place, :3769, inside the ETM receive branch, so no board is ever marked online; WDP rides the ETM struct and gate (WCB_WDP.h:20-21), so the relay card's node list is empty and `relayRouteAll` has nothing to route; the Wizard's `installEtmListener` (app.js:6003) never fires.
- TestConfigs/TestPlan.md exercises ETM enabled throughout (3.1-3.9, 12.2, 14.1) — no ETM-off case exists.

Net: a genuine one-sided-strip defect in a mode that is non-default, explicitly documented as incompatible with this feature, silent, and non-corrupting. Real, small, cheap to fix — S3, not S2.

ADJACENT GAP (not part of this finding, worth its own row): WCB_Client never strips the marker either. C:/Users/ghulette/Documents/GitHub/WCBClient/src/WCB_Client.cpp:2628-2631 copies `pkt->structCommand` straight to `_commandCallback` and only handles a CRC suffix — my grep for `0x01`/`SOH`/`wizardOrigin` over WCB_Client.{cpp,h} finds only WDP proto/TLV constants (:1820, :1827, :1830). So a Wizard single-chunk MGMT command relayed to a client-numbered target delivers a leading 0x01 to the sketch's callback, with ETM ON. Lower impact (relayRouteAll skips clients at app.js:5947 `if (nd.client ...) continue;`), but the same asymmetry.

</details>

### `Code/WCB/WCB.ino:3002` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**handleConfigReqPacket silently sends NOTHING when the config exceeds 2912 chars — reachable with ~80 stored variables alone**

**Correction to the finding as filed:**

Four corrections.

1. **Not new — already documented.** `docs/SEQUENCE_INVENTORY.md:43-57` describes this exact failure, including the 2912 figure and the debugMGMT-hidden diagnostic. The finding should be framed as "the documented trap was never fixed on the config path", which is what makes the small fix worth doing.

2. **Headline overstates the variable trigger.** "~80 stored variables alone" only overflows if names+values average ~16 chars (e.g. 12-char names) on top of a ~750-char base config. With typical short names (4-8 chars, ~20 chars/entry) you need roughly the full table of 100 (`WCB_Variables.h:27`). The realistic trigger is stored sequences (`WCB.ino:2910`), which have no cap of any kind.

3. **Line cites drifted.** `WCB_MAX_VARIABLES` is `WCB_Variables.h:27` and `WCB_VAR_NAME_MAX` is `:28` (finding said :26/:27). `emitHelpers` is `WCB.ino:2943` (not :2863), the `printVariablesBackup` call is `:2951`, the variable emit is `WCB_Variables.cpp:484`, and `SEQ,SAVE` is `WCB.ino:2910`. `MGMT_MAX_CHUNKS`=837 and the 3002/3000/252/256 cites are correct.

4. **Corrected fix.** The firmware half must be paired with a Wizard-side sentinel check, and the log promotion is safe as proposed (`handleConfigReqPacket` runs in loop() via `drainMgmtReqs`, `WCB.ino:3288`, not the WiFi callback). Concretely: emit the one-chunk `[CONFIGERR:TOOBIG,<len>,2912]` frag AND add, in `remoteBoardPull` before `WCBParser.parseBackupString` (`Wizard/app.js:7967`), `if (configStr.startsWith('[CONFIGERR:'))` → badge `error`, explicit toast naming the size, `onComplete?.(false)`, `return`. Also note the "raise MGMT_MAX_CHUNKS" long-term option costs more than the finding says: `ConfigPullSession.chunks` is `[16][184]` and there are five live instances (`WCB.ino:873-877`), so going to 32 chunks adds ~14.7 KB of static RAM as well as widening `receivedMask` at `:847` and `:869`. Omitting `SEQ,SAVE` values from `buildConfigString` (the sequences are already reachable via SEQ/SEQVAL) is the cheaper structural fix.

<details><summary>Full verification reasoning</summary>

MECHANISM — every load-bearing number re-derived from source, not the summary:

- `Code/WCB/WCB.ino:3002` is exactly as quoted: `if (totalChunks > MGMT_MAX_CHUNKS) { if (debugMGMT) Serial.printf("[MGMT] Config too large..."); return; }`. The `return` precedes every `esp_now_send` (the send loop starts at `:3014`), so the requester genuinely gets no frag, no error frame, no NAK.
- `MGMT_MAX_CHUNKS` = 16 (`WCB.ino:837`) ✓. `CONFIG_PAYLOAD_SIZE` = 183 (`WCB.ino:252`) ✓. `chunkStride = CONFIG_PAYLOAD_SIZE - 1` = 182 (`WCB.ino:3000`) ✓. 16 × 182 = **2912** ✓.
- `debugMGMT = false` at `WCB.ino:256` ✓ — so the only trace is off by default.
- The relay never opens a session either (`handleConfigFragPacket`, `WCB.ino:3041`, is never entered), so there is no target-side or relay-side timeout log to correlate against.
- Producer side: `buildConfigString()` (`:2938`) drives `collectConfigCommands` (`:2762`) with no size awareness, and that emitter **does** include every stored sequence's full value — `emit("SEQ,SAVE," + key + "," + value, true)` at `WCB.ino:2910` — plus `printVariablesBackup` via `emitHelpers` (`:2951`). Per-variable cost is `defSep + defFunc + "VAR,SET," + name + "," + value` (`WCB_Variables.cpp:484`), with `defSep="^"`, `defFunc="?"` defaulted at `WCB_Variables.h:80-81`, i.e. 12 chars minimum and 2+8+15+1+11 = 37 maximum. `WCB_MAX_VARIABLES` = 100, `WCB_VAR_NAME_MAX` = 15.

REACHABILITY — real, and the repo already says so. `handleConfigReqPacket` is reached from `drainMgmtReqs` (`WCB.ino:3288`) on `PACKET_TYPE_CONFIG_REQ`, which the Wizard triggers via `?MGMT,PULL,<n>` from `remoteBoardPull` (`Wizard/app.js:7900`), called on every discovery/auto-migration path (`app.js:494, 533, 1838, 1863, 5945, 7716, 7741, 8058`). No guard, clamp or truncation exists anywhere upstream. Wizard retry math checks out: `MAX_PULL_ATTEMPTS` 3, `PULL_TIMEOUT_MS` 6000, `PULL_RETRY_MS` 2500 (`app.js:7893-7895`) → ~23 s (not "~20 s") ending in badge `error` + "config pull failed after 3 attempts" (`app.js:8024-8027`) with no hint of the real cause.

INTENT — this is NOT an undiscovered bug, and that is the main correction. `docs/SEQUENCE_INVENTORY.md:43-57` documents it verbatim under the heading "The config pull carries values, and overflows": "...the whole thing caps at MGMT_MAX_CHUNKS × 182 = 2912 characters... A board with ~20 real sequences exceeds that. When it does, the target sends nothing at all, and the 'Config too large' diagnostic is behind debugMGMT — so from the consumer's side it is indistinguishable from a timeout. This is the trap this feature exists to route around." `WCB.ino:3173-3179` restates it. But it is documented as a *trap*, not as an accepted trade-off — the SEQ/SEQVAL inventory routes *sequence discovery* around it and leaves the config pull itself broken for such a board. So it stands as a real defect, not a REFUTED deliberate design.

SEVERITY — S3, not S2. Against S2: it is pre-existing and explicitly documented; nothing is corrupted or crashed; the user is not left in silence, they get an error toast and an `error` badge (the failure is misattributed, not invisible); recovery is trivial (plug the board in over USB, `?backup` has no chunk ceiling). The trigger requires a well-loaded board — the repo's own estimate is ~20 substantial sequences. Not S4 because the loss is functional: a board over the ceiling cannot be managed remotely at all, and every discovery re-burns ~23 s serially through `relayRouteAll`.

</details>

### `Code/WCB/WCB.ino:3088` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**Config/stats/seq/ETM fragment reassembly does multi-KB String building and a blocking Serial.printf inside the ESP-NOW receive callback**

**Correction to the finding as filed:**

Two corrections to the finding, plus one addition.

1. Numbers: 2912 bytes is the theoretical cap (16 x 182), not the observed size. Real configs are 2353-2711 bytes (`TestConfigs/WCB2_02_full.txt`, `WCB1_02_full.txt`) = 13-15 chunks, so the stall is ~205-235 ms, and `handleConfigReqPacket` sends 13-15 frags per pass, not 16. Dispatch is at `WCB.ino:3651-3655`, not 3653-3657.

2. Severity S2 -> S3. Bounded one-shot stall on an explicit, sequentialized management action; well under the 5 s TWDT; consequences absorbed by ETM's 3x retry. It is a genuine breach of the repo's own stated rule (`WCB.ino:3249-3257`, `WCB_RemoteTerm.cpp:14-16`) and should be fixed, but it neither crashes nor corrupts.

3. Addition the reviewer missed: because HAL locks are enabled (`sdkconfig:470`), the WiFi task holds the UART0 mutex for the whole ~235 ms, so any `Serial.*` call from loop() on core 1 blocks behind it too. The stall is not confined to the WiFi task.

Correct fix (neither of the two proposed): keep the cheap reassembly in the callback and defer only the delivery, via a dedicated single-shot output buffer sized to the reassembly ceiling, e.g.
```
static char  s_mgmtOutBuf[MGMT_MAX_CHUNKS * (CONFIG_PAYLOAD_SIZE - 1) + 32];  // 2944
static volatile bool s_mgmtOutPending = false;
```
set in each handler in place of the inline `Serial.printf`, and printed by a new `drainMgmtOut()` added next to `drainMgmtReqs()` at `WCB.ino:7492`. One buffer is sufficient: the five sessions are already mutually exclusive in practice (the Wizard serializes pulls) and each handler already owns a single `ConfigPullSession` (`WCB.ino:873-877`). Alternatively, a new `xQueueCreate(8, sizeof(espnow_struct_config_frag))` fed from the callback and reassembled in loop() — but see below on why it cannot be the existing queue.

<details><summary>Full verification reasoning</summary>

MECHANISM — every number re-derived from source, none taken from the reviewer.

Dispatch is real and inline. `espNowReceiveCallback` at `Code/WCB/WCB.ino:3648-3656` routes all five frag types with no queue:
```
if (len == sizeof(espnow_struct_config_frag)) {
  uint8_t ptype = ((const espnow_struct_config_frag*)incomingData)->packetType;
  if      (ptype == PACKET_TYPE_CONFIG_FRAG) handleConfigFragPacket(incomingData);   // :3651
  ... STATS/SEQ/SEQVAL/ETM ...                                                        // :3652-3655
```
(reviewer said 3653-3657 — off by two, immaterial). `grep -rn` over `Code/WCB/` shows these five functions have exactly one call site each, that one — no loop()-side caller exists.

Sizes, recomputed by hand: `CONFIG_PAYLOAD_SIZE 183` (`WCB.ino:252`), `MGMT_MAX_CHUNKS 16` (`:837`), `chunkStride = 183-1 = 182` (`:3000`). Max reassembly = 16 x 182 = 2912 bytes — the reviewer's 2912 is right, but it is the ceiling, not the typical case. Real bench configs in `TestConfigs/` are 2353-2711 bytes (`WCB2_02_full.txt` 2353, `WCB1_02_full.txt` 2711), i.e. 13-15 chunks, not 16.

The print really does block. `Serial.begin(115200)` at `WCB.ino:7120`, 8N1, and nothing ever changes UART0's rate outside a gated `?OTALOCAL,BAUD` session (`WCB_OTA.cpp:250-259`, requires `ota.active`). Core 3.3.4: `HardwareSerial` ctor sets `_txBufferSize(0)` (`cores/esp32/HardwareSerial.cpp:123`); the sketch calls only `setRxBufferSize(2048)` (`WCB.ino:7119`), never `setTxBufferSize`, so `uart_driver_install(..., tx_buffer_size=0, ...)` (`esp32-hal-uart.c:868`) and `uartWriteBuf` -> `uart_write_bytes` (`:1160-1167`) does not return until the bytes are on the wire. `Print::vprintf` (`cores/esp32/Print.cpp`) mallocs one buffer for len>=64 and issues a **single** `write(buf, len)` — so it is one uninterrupted UART transaction. 2711 x 10 / 115200 = **235 ms**; the 2912 ceiling = 253 ms. Reviewer's "~250 ms" is right to the significant figure.

Serial is also mutex-protected: `# CONFIG_DISABLE_HAL_LOCKS is not set` in `esp32-arduino-libs/idf-release_v5.5-.../esp32/sdkconfig:470`, so `UART_MUTEX_LOCK()` is live and any core-1 `Serial.*` from loop() blocks behind the WiFi task for the same window (device ports Serial1-5 are separate `uart_t` locks, unaffected).

INTENT — this is the opposite of a documented trade-off; it is the one hole in a rule the repo states three separate times, and one of those comments records a real crash:
- `WCB_RemoteTerm.cpp:14-16`: "Packets are enqueued from the WiFi-task callback and dequeued in loop(). **This prevents blocking UART writes inside the ESP-NOW receive callback, which caused the WiFi watchdog to fire and restart the board.**"
- `WCB.ino:3249-3257` (directly above `MgmtReqSlot`): "Running that in the ESP-NOW receive callback (WiFi task) stalls ESP-NOW and drops other inbound packets (heartbeats, OTA frags, RC telemetry). So the callback only ENQUEUES the 43-byte request."
- `WCB.ino:3898-3901`: "we push to the relay queue rather than calling Serial.println here directly — we're on the WiFi task (Core 0)."
`loop()` at `WCB.ino:7478-7495` carries eight separate `drain*()` calls whose comments all say "off the WiFi callback". The request side of this exact feature was deferred; the response side was not. `git log -L 3079,3092` shows the frag deliverer dates to `978ecf4` ("working on wizard and config tool"), while the deferral machinery landed later (`5c49642` 2026-05-27 rcJsonRelayQueue, `dd986df` 2026-06-29 drainMgmtReqs) — an omission, not a decision. No comment anywhere justifies the inline print; the one comment present (`:3087` "always printed regardless of debugMGMT") explains unconditionality, not context.

REACHABILITY — a real user hits this routinely. `Wizard/app.js:8035` sends `?MGMT,PULL,<n>`; `app.js:494` auto-fires `remoteBoardPull` whenever a board is routed through a relay, and `app.js:6005`/`:1825` show it also fires off "[ETM] WCBn came ONLINE", i.e. on any board reboot with the Wizard open. `app.js:11983` does the same for `?MGMT,STATS`/`?MGMT,ETM`.

WHY S3, NOT S2 — the defect is real but bounded and self-limiting:
- One-shot ~235 ms, not a per-packet hot path. TWDT is 5 s with only IDLE0 subscribed (`sdkconfig:1653-1654` `CONFIG_ESP_TASK_WDT_TIMEOUT_S=5`, `CHECK_IDLE_TASK_CPU0=y`), so a single occurrence cannot reproduce the RTERM-era reboot; that failure came from a *continuous* stream of relayed lines, not one burst.
- No corruption, no hang, no data loss. Dropped frames in the window are covered by ETM's 3x retry and by heartbeat periodicity.
- It cannot compound: `Wizard/app.js:487-488` explicitly pulls "SEQUENTIALLY... the relay reassembles ONE config reply at a time", and `handleConfigFragPacket:3055-3056` suppresses the redundant second pass.
- Proportion: the same pull already costs the *target* 32 x `delay(20)` = 640 ms in loop() (`WCB.ino:3014-3037`) by design. A 235 ms WiFi-task stall on the relay is the smaller half of an operation the design already treats as expensive.
The one scenario that would justify S2 — a pull completing mid-OTA-relay, where `WCB.ino:337-344` documents that a single lost `[OTA:ACK]` "looked like a hang at ~70%" — does not co-occur in practice (`app.js:1825` notes a rebooting board "may not answer ?MGMT,PULL for a while"). Worth fixing before release, not a release blocker.

</details>

### `Code/WCB/WCB.ino:3099` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**sendResultFrags silently drops any relayed result over 2912 chars — ?MGMT,STATS and ?MGMT,ETM both exceed it on a full 20-board mesh**

**Correction to the finding as filed:**

The mechanism stands; three corrections.

1. Crossover numbers: buildETMCharResultsString crosses at 16 peers, not "roughly 15" — 15 peers is 2851 chars, exactly 16 chunks, and fits. buildStatsString at 19 active peers is 2234-2404 chars (not "≈1970"), and needs about 5 `?STATS,RPT` reporter rows to tip over, not 7.

2. Reachability is narrower than stated. `?STATS,RPT` is never sent by WCB firmware — the only producer in the whole ecosystem is `NaviCore/rc_telemetry.h:1265`. So the "Reported by Other Nodes" block only fills with one row per NaviCore-class WCB_Client host, not per WCB. The scenario requires ~19 active peers AND >=5 NaviCore nodes reporting.

3. The ETM arm is effectively unreachable in a clean state: `etmCharPhaseSentTimes[3][200]` (WCB.ino:577) is written at indices up to `etmCharMessageCount(20, :269) * peerCount` (`:1456`, `:1470`, `:1506`, `:1513`), overrunning the object in phase 3 at peerCount >= 11. That S1 (already filed) fires five peers before this ceiling does.

Right fix is the reviewer's "at minimum" arm, not its headline: promote the WCB.ino:3100 log out from behind `debugMGMT`, and send a one-chunk `TOOBIG` reply frag carrying actual/allowed length, mirroring `handleSeqValReqPacket` at WCB.ino:3216-3224. Optionally have `buildStatsString`/`buildETMCharResultsString` take a max-length and append a truncation marker. Do NOT raise MGMT_MAX_CHUNKS (see fix_is_correct).

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived, not taken from the summary.

- `Code/WCB/WCB.ino:837` `#define MGMT_MAX_CHUNKS 16`; `WCB.ino:252` `#define CONFIG_PAYLOAD_SIZE 183`; `WCB.ino:3096` `chunkStride = CONFIG_PAYLOAD_SIZE - 1` = 182. Ceiling = 16 x 182 = 2912. Arithmetic confirmed.
- `WCB.ino:3099-3102` is a bare `return` on overflow, with the only diagnostic behind `debugMGMT`, declared `false` at `WCB.ino:256`. Confirmed verbatim.
- `WCB.ino:120` `MAX_WCB_COUNT 20`. `buildStatsString()` (`WCB.ino:1639`) loops `b < MAX_WCB_COUNT` at `:1690` (ETM per-board rows) and `:1724` (reported-by-other-nodes rows), with no length budget anywhere. `buildETMCharResultsString()` (`WCB.ino:1801`) emits one row per peer per phase, 3 phases (`:1815-1817`), also unbounded.
- I did NOT trust the reviewer's char counts. I reconstructed both functions' exact format strings in node and measured. buildStatsString: 19 active peers + 0 reporters = 2234 chars (13 chunks, fits); 19 active + 5 reporters = ~2959 (17 chunks, OVER); 19 active + 20 reporters = 4819 chars / 27 chunks. buildETMCharResultsString: 15 peers = 2851 chars = exactly 16 chunks (fits); 16 peers = 3016 / 17 chunks (OVER). So the reviewer's "~15 peers" is off by one (real crossover is 16) and "~4500 chars / 25 chunks" is slightly low (4600-4800 / 26-27), but every load-bearing claim holds.

CALL PATHS — traced, not assumed. `?MGMT,STATS,<n>` -> `handleMgmtStatsRequest` (`WCB.ino:3441`) -> STATS_REQ broadcast -> target `espNowReceiveCallback` `:3636` -> `enqueueMgmtReq` `:3269` -> `handleStatsReqPacket` `:3126-3134` -> `sendResultFrags(..., PACKET_TYPE_STATS_FRAG)`. ETM: `?MGMT,ETM,CHAR,<n>` (`WCB.ino:2557-2564`) -> `handleMgmtETMRequest` `:3514` -> `printETMCharResults` `:1839` -> `sendResultFrags` `:1852`. Both real, both live. Wizard drives them at `Wizard/app.js:11982-11983`, and on a silent drop the user gets only `Timed out waiting for stats response from WCB n` (`app.js:12005`) with no cause — the finding's claim about the user-visible symptom is correct.

REACHABILITY — the honest picture is narrower than "20-board mesh". `wcbPeerActive[]` is the WCBQ floor union learned peers (`rebuildActivePeers`, `WCB.ino:6854-6862`, `bool floorMember = (i < Default_WCB_Quantity)`), so `?WCBQ,20` alone produces 19 ETM rows even for boards that are offline — but offline rows print the short `OFFLINE` form, and 19 of them still only reach ~2234-2404 chars. Overflow additionally needs the "Reported by Other Nodes" block. `?STATS,RPT` is never emitted by WCB firmware — I grepped all sibling repos: the only sender is `NaviCore/rc_telemetry.h:1265`, pushed every 30 s (`WCB.ino:5009` comment), and rows are RAM-only and never aged out (`WCB.ino:530-551`, cleared only by `?STATS,RESET` at `:5375`). So the trip condition is ~19 active peers PLUS >=5 NaviCore-class client nodes reporting. Real but not routine.

INTENT — this is NOT a documented trade-off; the codebase corroborates the finding. `WCB.ino:3168-3176` says of the seq-value path: "TOOBIG is reported EXPLICITLY rather than dropped. Silent overflow is precisely what makes the config-pull path unusable for sequences (it sends nothing and logs only under debugMGMT), and reintroducing it here would recreate the bug this whole feature exists to route around" — and `WCB.ino:3216-3224` implements exactly that budget check. The author knows about the silent drop, fixed it for one caller, and left STATS/ETM unguarded. That is an oversight, not a deliberate choice.

DUPLICATE — this identical finding is already filed at `docs/CODE_REVIEW_2026-08_wave1.md:255-270` (same line, same two producers, same fix menu). The wave4 row is a re-report of an existing wave1 finding.

WHY S3, NOT S2. (a) The ETM half is masked by an already-logged S1: `etmCharPhaseSentTimes[3][200]` (`WCB.ino:577`) is indexed by `etmCharPhaseMessageIndex` up to `totalMessages = etmCharMessageCount * peerCount` (`:1456`, written at `:1470`, `:1506`, `:1513`) with `etmCharMessageCount = 20` (`:269`) — that overruns the 600-element object in phase 3 at peerCount >= 11, i.e. five peers before the 2912 ceiling is even reached. At the mesh size needed to trip this finding, the characterization has already smashed adjacent globals. (b) The STATS half needs 19 peers plus >=5 separate NaviCore reporters. (c) The failure mode is a diagnostics pane timing out: no data loss, no corruption, no mesh disruption, fully recoverable, and the Wizard's direct (non-relay) `?STATS` branch (`app.js:12036-12042`, serial `sendAndCollect`) is unaffected. Real capacity ceiling worth fixing, but not S2.

</details>

### `Code/WCB/WCB.ino:3245` — CONFIRMED · S2 → **S3**

**etmCharRelayRequesterWCB latches when startETMChar()/processETMChar() bail — a later LOCAL ?ETM,CHAR frag-sends its results to a board that never asked**

**Correction to the finding as filed:**

Three corrections to the finding.

(a) "WCB2 gets no response and no error — it just waits forever" is wrong. The Wizard's relay collector rejects on a timer: `relayTimeout = 120000` for ETM (`Wizard/app.js:11972`) with the reject at `:12001-12007`. The operator sees "Timed out waiting for ETM response from WCB n" after ~2 min. On a bare serial console they see nothing at all. Also note this silent-failure symptom is a SEPARATE defect (no error frame is ever sent back when the target bails) and is NOT fixed by the proposed change — the proposed fix addresses only the latch.

(b) Reachability understated. The most likely trigger is not ETM-off (default `etmEnabled = true`, WCB.ino:264) but `Default_WCB_Quantity < 2`: the default is 1 (WCB.ino:135) and WCBQ is only the *floor* of peer membership, unioned with WDP-learned peers (WCB.ino:456-463, :6854-6859). A board that is fully meshed, discovered, and offered by the Wizard can still have WCBQ=1 and bail at :1410 on every single relay ETM request.

(c) Same-abort-path sibling bug the reviewer missed, which belongs in the same fix: the no-peers abort at WCB.ino:1450-1454 also never restores `debugETM` from `etmCharDebugWasSaved` — that restore lives only in `printETMCharResults` (:1843-1848). So an aborted run leaves `?ETM` debug silently switched off (suppressed at :1418-1421) until some later run completes. Both leaks share one root cause: `processETMChar`'s abort path skips the single cleanup site.

Recommended shape (superset of the proposal): in `handleETMReqPacket`, `etmCharRelayRequesterWCB = 0; startETMChar(); if (etmCharRunning) etmCharRelayRequesterWCB = pkt.requesterWCB;` — the unconditional zero also drains any latch inherited from the abort path. Then factor the cleanup in `printETMCharResults` (:1843-1856) into an `endETMCharRun()` helper and call it from the :1450-1454 abort too, so debug-restore and requester-clear can never again be skipped.

<details><summary>Full verification reasoning</summary>

MECHANISM — every fact re-derived from source, not the summary.

1. The set site is real and unconditional. `Code/WCB/WCB.ino:3234-3247` (`handleETMReqPacket`): after the password check (`:3238`), the `pkt.targetWCB != WCB_Number` check (`:3239`) and the `etmCharRunning` re-entrancy guard (`:3241-3244`), it does `etmCharRelayRequesterWCB = pkt.requesterWCB;` (`:3245`) and only THEN `startETMChar();` (`:3246`). No error path, no rollback.

2. Writers of the global, grepped repo-wide: `WCB.ino:878` (init `= 0`), `:3245` (set), `:1855` (clear). That is the complete set — the reviewer's "the only place that clears it is printETMCharResults" is correct.

3. All three early-out paths exist exactly as claimed:
   - `startETMChar()` at `:1405`; `if (!etmEnabled)` returns at `:1406-1409` (prints "ETM is not enabled. Use ?ETMON first.").
   - `if (Default_WCB_Quantity < 2)` returns at `:1410-1413`.
   - `etmCharRunning = true` is only reached at `:1423`, after both guards.
   - `processETMChar()` `:1440`; the no-peers abort is at `:1450-1454` — `Serial.println(...); etmCharRunning = false; return;` with no `printETMCharResults` call. Line numbers verified by `awk` line-numbering, not trusted from the finding.
   - Confirmed there is no OTHER unclean exit: `etmCharRunning` is written only at `:562, :1423, :1452, :1535`, and `:1535` is immediately followed by `printETMCharResults` (`:1536`). Phase can only be 1/2/3 (`advanceETMCharPhase` `:1541` is called only from the phase-1/2 branch), so phase 3 completion is the sole clean exit.

4. REACHABILITY with ETM off is correct, and I verified it rather than taking it on faith. `espNowReceiveCallback` (`:3554`) dispatches by size: the 43-byte `espnow_struct_config_req` branch is at `:3631-3641` and enqueues `PACKET_TYPE_ETM_REQ` via `enqueueMgmtReq`. The ETM/non-ETM mismatch gate (`if (isETMPacket && !etmEnabled) return;`) is at `:3681-3684`, i.e. AFTER and only for the 249/252-byte message structs. `drainMgmtReqs()` (`:3279`, called from loop at `:7492`) routes `PACKET_TYPE_ETM_REQ → handleETMReqPacket` (`:3290`) with no ETM gate. So an ETM-disabled board does reach `:3245`.

5. REACHABILITY is in fact BROADER than the reviewer argued, via `Default_WCB_Quantity`. Its default is `1` (`WCB.ino:135`), loaded from NVS (`WCB_Storage.cpp:392`). Critically, WCBQ is only the *floor* of peer membership — `wcbPeerActive[] = {1..Default_WCB_Quantity} ∪ {learned}` (`WCB.ino:456-463`, `:6854-6859`), so with WDP auto-join a board can be fully meshed, discovered, and relay-reachable while still having WCBQ=1. That is a board the Wizard will happily offer "Run ETM characterization" for, and the run bails at `:1410` every time while latching the global.

6. The user path is real, not theoretical. Wizard `app.js:11983` sends `?MGMT,ETM,CHAR,<n>` over the relay; firmware parses it at `WCB.ino:2557-2564` → `handleMgmtETMRequest` (`:3515`) → builds `PACKET_TYPE_ETM_REQ` (`:3521`) and sends it. The two local trigger paths that would later misfire are `?ETM,CHAR` at `:4703-4704` and `etmchar` at `:5310-5311` — I read both; neither resets `etmCharRelayRequesterWCB`.

7. CONSEQUENCE, checked rather than assumed. `printETMCharResults` (`:1839`) calls `sendResultFrags(results, etmCharRelayRequesterWCB, PACKET_TYPE_ETM_FRAG)` (`:1852`). `sendResultFrags` (`:3095`) broadcasts and the receiver filters on `pkt.requesterWCB != WCB_Number` (`:3411`), so there is NO out-of-bounds risk from an unvalidated `requesterWCB` — good, that kills the one path that could have made this S1/S2. The receiver `handleETMFragPacket` (`:3406-3437`) reassembles and prints `[MGMT:ETM,%d]%s`. Two real harms: (a) a spurious multi-KB dump on an operator's console, and (b) session clobber — `:3414-3421` memsets `etmRelaySession` whenever the `sessionId` differs, so a stale dump arriving mid-reassembly destroys a legitimate in-flight relay collection. The Wizard collector matches on the bare prefix `[MGMT:ETM,` with no board-number check (`app.js:11984`, `:12010-12012`), so a stale dump can also satisfy a collection aimed at a different board.

8. INTENT — not deliberate. `git log -L3233,3247` shows the function landed in `898ebc7` ("updated to have stats work on relay") and was only touched by `39d4d65` to delete a `Serial.printf` debug line. The only comment on the function (`:3233`) says "results sent in printETMCharResults" — i.e. it assumes the run always reaches that function. No comment, doc (`docs/`), or CLAUDE.md rule defends the current shape.

WHY S3, NOT S2. The mechanism stands, but the blast radius is diagnostic tooling only: misdirected characterization text, no crash, no memory unsafety (point 7), no config/NVS corruption, nothing on the data plane. It needs a bail followed by a *local* console run on the same board (a relay run from a different requester overwrites the stale value correctly). Real and worth fixing, not release-blocking.

</details>

### `Code/WCB/WCB.ino:3831` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**etmSeqHistory is never cleared when a peer reboots, so the first commands from a restarted board are ACKed but silently not executed**

**Correction to the finding as filed:**

Mechanism correction (the headline is too broad): the drop is NOT "the first commands from any restarted board." It happens only when the rebooting sender had sent at most ETM_SEQ_HISTORY (8) ETM COMMAND packets that this peer actually recorded since the sender's previous boot. Simulating the literal ring code across a reboot: N<=8 pre-reboot commands -> exactly N post-reboot commands ACKed-and-dropped; N>=9 -> zero drops, because the round-robin eviction removes the low seqs before the restarted counter reaches them. Also note heartbeats/boot announces/WDP/OTA all send seq 0 (WCB.ino:936, :955, :979) and do NOT advance the counter, so N counts only real commands — which is what keeps N<=8 reachable on a bench.

Fix correction — the proposal as written has two traps and one residual risk:
1. It offers the branch at :3789-3795, which is SHARED between PACKET_TYPE_HEARTBEAT and PACKET_TYPE_ETM_BOOT. An unconditional memset there clears the ring on every heartbeat (every etmHeartbeatSec seconds), destroying duplicate suppression outright and reinstating the exact double-fire the ring was added to fix (:553-557, commit a32e9cb). The clear must be guarded on `structPacketType == PACKET_TYPE_ETM_BOOT`.
2. Its alternative location, ":3768 where isBootAnnounce is computed", sits INSIDE `if (wcbPeerActive[senderIdx] || isSpecialPeer)` (:3766). The COMMAND branch has no such gate, so a non-active-peer sender's commands do execute and do populate the ring — that placement silently misses them.
3. Boot announces are unacked broadcasts sent 3x at ~1.5s/2.7s/3.9s after boot (:7423-7424, :1027-1031). Each one re-clears the ring, while etmTimeoutMs=500 with up to 3 retries (:268, :1301) gives commands a ~1.5s retry life — so a command sent in the boot window whose ACK is lost can have its retry land after a later announce clears the ring and execute twice. Debounce it (clear at most once per ~10s per sender).

Recommended patch, in the BOOT branch at :3789-3795:
    if (etmReceived.structPacketType == PACKET_TYPE_ETM_BOOT) {
      // Peer restarted: its etmSequenceCounter (:522) is RAM-only and resumes at 1,
      // so our stale ring would false-dup its first commands (ACKed, never executed).
      // Debounced: 3 announces are sent per boot (:7423) and a mid-window clear could
      // let a retry double-fire.
      if (millis() - etmSeqHistClearMs[senderIdx] > 10000UL) {
        memset(etmSeqHistory[senderIdx], 0, sizeof(etmSeqHistory[senderIdx]));  // 8*2 = 16 bytes
        etmSeqHistoryIdx[senderIdx] = 0;
        etmSeqHistClearMs[senderIdx] = millis();
      }
    }
(`sizeof(etmSeqHistory[senderIdx])` is correct — a real uint16_t[8], 16 bytes, not a pointer. Running this in the ESP-NOW RX callback is fine: the ring's only other writer, :3838-3840, is in the same callback.)

Better primary fix, because it needs no receiver change, survives a missed boot announce, and also fixes the wider WCBClient window (_cmdSeqDup, WCB_Client.cpp:857-876, 32 deep): randomize the sender's counter at boot next to the ETM init at :7419-7425 — `etmSequenceCounter = (uint16_t)(esp_random() & 0x7FFF); if (etmSequenceCounter == 0) etmSequenceCounter = 1;` — dropping the collision odds from ~100% (N<=8) to ~0.1%. Nothing compares seqs for ordering; the pending table (:1320) and ACK matching use equality only. Guard the wrap at :2304 (`if (etmMsg.structSequenceNumber == 0) etmMsg.structSequenceNumber = ++etmSequenceCounter;`), as WCB_Client already does at WCB_Client.cpp:2141, since 0 collides with the ring's empty-slot sentinel. Do both for belt and braces.

<details><summary>Full verification reasoning</summary>

MECHANISM — every load-bearing fact re-derived from source, not the write-up.

1. Ring: `static const int ETM_SEQ_HISTORY = 8;` (WCB.ino:558), `uint16_t etmSeqHistory[MAX_WCB_COUNT][ETM_SEQ_HISTORY] = {{0}};` (:559), `uint8_t etmSeqHistoryIdx[MAX_WCB_COUNT] = {0};` (:560). Read loop at :3831-3833, write at :3838-3840. Comment at :553-557 says "Seq 0 is never sent, so 0 = empty slot."
2. Sender counter is RAM-only and restarts at 1: `uint16_t etmSequenceCounter = 0;` (:522), `etmMsg.structSequenceNumber = ++etmSequenceCounter;` (:2304). `grep -rn "etmSequenceCounter"` over Code/ returns EXACTLY :522 and :2304 — no NVS persist, no reset elsewhere.
3. Only COMMAND packets consume a seq. Heartbeat (:936), boot announce (:955), WDP (:979) all hardcode `structSequenceNumber = 0`; OTA uses its own packet types (:3663, PACKET_TYPE_OTA_*), never sendESPNowMessage's ETM path. So N (seqs burned per boot) counts real commands only.
4. `grep -rn "etmSeqHistory"` over Code/ returns ONLY :556/:558/:559/:560/:3831/:3832/:3838/:3840 — nothing ever clears it. `memset(boardTable,0,...)` at :7420 clears boardTable only. Confirmed: never cleared.
5. The receiver ACKs before the dup check — `etmSendAck(...)` at :3823, dup check at :3830-3841, with the comment at :3808 "ACK - even duplicates (so an ensured sender stops retrying)". So a suppressed command is reported delivered. The suppressed path only logs under debugETM (:3844-3847) and does a blue status wipe. Silent by default. Retries reuse the seq (:1320), which is why the ring exists at all (:553-557; the ring replaced a single slot in commit a32e9cb).

BUT THE REVIEWER'S MECHANISM IS OVERSTATED, AND THE MISSING PRECONDITION MATTERS. The ring is written round-robin, and eviction happens to walk in exactly the order that removes the low seqs before the restarted counter reaches them. I simulated the literal code (ring[8], skip-write-on-dup, idx = (idx+1)%8) across a reboot for N = 1..40 pre-reboot commands: drops occur ONLY for N <= 8 (N drops, first executed seq = N+1); for N >= 9 there are ZERO false duplicates — seq 1 lands while the ring still holds {N-7..N}, all >= 2, and each write evicts the oldest before the counter climbs to it. Generalising: the ring is monotonic (s_1<...<s_8) and a collision at step k needs s_k == k, i.e. the receiver recorded at most 8 commands from that sender in the sender's previous session, contiguous from 1. So the correct claim is not "the first commands from a restarted board" — it is "the first up-to-8 commands after a reboot, and ONLY if that sender had sent <= 8 ETM commands to that peer since its own previous boot." Self-heals thereafter.

REACHABILITY — still a real, reachable state. The COMMAND branch has no `wcbPeerActive` gate (contrast :3766), so any MAC-bound valid sender reaches the ring. Affected traffic includes Wizard-relayed single-chunk commands: `handleMgmtForward` routes `totalChunks == 1 && payload.length() <= 198` through `sendESPNowMessage(targetWCB, markedPayload, true)` (:2598), i.e. ETM COMMAND with a seq, so relayed `?`-config lines and remote-terminal commands are subject to the ring. (Multi-chunk config push is NOT — it uses PACKET_TYPE_MGMT_FRAG with its own reassembly, :2608-2620.) Realistic bench repro, which is exactly the reviewer's scenario: boot the bench, send 3-4 commands from the tethered/relaying board, reflash or OTA that board, then drive the mesh — its next 3-4 commands are ACKed, reported success by the Wizard, and never executed by the still-up peers.

INTENT — nothing sanctions it. No comment, doc page, or CLAUDE.md rule mentions counter restart or ring clearing. The receiver DOES observe the restart (`isBootAnnounce` at :3768, branch at :3789-3795) and deliberately uses it to force a "came ONLINE" edge — the signal is present and unused for dedup state.

CROSS-REPO CORROBORATION: WCBClient has the same class of bug with a WIDER window — `_seqCounter{0}` (WCB_Client.h:1079), reset in begin() (WCB_Client.cpp:77), `uint16_t seq = ++_seqCounter` (:2140), and `_cmdSeqDup` (:857-876) is a high-water-mark + 32-bit mask that returns true for anything within 32 below the high. A WCB restarting with N <= 33 gets its first N commands dropped by a NaviCore/WCB_Client host. Not this finding's scope, but it means a receiver-side-only fix in WCB firmware does not cover clients.

SEVERITY: downgraded to S3. It is genuinely silent and genuinely user-visible, but it needs a narrow precondition (sender rebooted with <= 8 commands sent), is self-limiting to at most 8 commands, and leaves no persistent corruption — unlike the S1/S2 findings in this repo's own calibration (docs/CODE_REVIEW_2026-08_verdicts.md:234), which corrupt stored NVS config the Wizard then believes is in sync.

</details>

### `Code/WCB/WCB.ino:3983` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**?WHOAMI reply is sent as a 249-byte non-ETM packet that WCB_Client (NaviCore) routes to the raw hook and never delivers**

**Correction to the finding as filed:**

Mechanism stands; severity and failure scenario need correcting, and the primary proposed fix is unsafe.

Corrected impact: the alias is NOT permanently lost and the query does NOT loop forever. WDP TLV 0x01 carries the alias (WCB_WDP.cpp:232-234 -> WCB_Client.cpp:2019-2020 -> NaviCore.ino:4594), and ?WHOAMI is capped at ALIAS_MAX_TRIES=4 per online session (rc_telemetry.h:1754, :1773-1775). The real defect is that the ?WHOAMI fallback path is dead: up to 4 undeliverable replies per board per online session, name display delayed by up to WDP_PERIOD_MS=60 s (WCB_WDP.cpp:52) on a cold NaviCore boot, and no alias at all for a board running ETM with ?WDP,OFF (WCB_WDP.cpp:1219).

Corrected fix: do NOT simply flip the flag back to true at WCB.ino:3983. That reintroduces exactly what commit a32e9cb fixed — sendESPNowMessage(...,true) calls etmAddToPendingTable (WCB.ino:2311) from the ESP-NOW RX callback (WiFi task), and WCB.ino:1100-1107 documents that the pending table must be touched only by the loop task (the shift-eviction at WCB.ino:1163-1168 racing loop()'s retry scan corrupts it). That line is the ONLY sendESPNowMessage call anywhere inside espNowReceiveCallback (WCB.ino:3584-4230), so the invariant is currently intact and flipping it would be the sole violation.

Two safe fixes:
1. Minimal — send an untracked 252-byte ETM COMMAND inline at the WHOAMI site: build an espnow_struct_message_etm with structPacketType = PACKET_TYPE_COMMAND and esp_now_send it WITHOUT calling etmAddToPendingTable. This is the same carve-out the ETM path already makes for best-effort JSON at WCB.ino:2308-2311, just applied to a unicast. The client ACKs it (WCB_Client.cpp COMMAND case) and the ACK finds no pending slot, which is harmless. Prefer a dedicated helper over relaxing the shared bestEffortTelemetry predicate, so other unicast senders keep their ETM guarantees.
2. Or the reviewer's second option — enqueue the reply for loop() and send it with useETM=true there.

Either way the comment at WCB.ino:3980-3982 must be rewritten: the operative constraint is that the reply must be 252 bytes to be ingestible by WCB_Client at all, and "best-effort" must not be achieved by changing the packet size.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived, holds.

1. `Code/WCB/WCB.ino:3970-3986`: inside `espNowReceiveCallback` (declared `WCB.ino:3584`, body ends `:4230`), the ETM COMMAND branch answers `?WHOAMI` with `sendESPNowMessage((uint8_t)senderWCB, reply.c_str(), false)` at `:3983`. Transcription is accurate.

2. `WCB.ino:2280`: `if (etmEnabled && useETM && !isPWMMessage)` — `useETM=false` skips the ETM path and falls to the NORMAL SEND PATH at `:2330`, which builds `espnow_struct_message`. I read the real declaration at `WCB.ino:777-783`: packed `char[40] + char[4] + char[4] + uint8_t + char[200]` = 249 bytes. The ETM struct at `WCB.ino:204-212` adds `uint8_t + uint16_t` = 252. `WCB_RemoteTerm.h:47-48` states the same two sizes independently.

3. `WCBClient/src/WCB_Client.cpp:2510`: `if (len != (int)sizeof(wcb_packet_etm_t)) { if (_maybeHandleSeqFrag(data,len)) return; if (_rawPacketCallback) _rawPacketCallback(mac,data,len); return; }`. `wcb_packet_etm_t` (WCB_Client.h:153-168) is the same packed 252. So a 249-byte frame provably never reaches the `switch (pkt->structPacketType)` at `:2541` and never reaches `_commandCallback`. `_maybeHandleSeqFrag` (`WCB_Client.cpp:1344-1346`) bails unless `_seqPending` and `len == sizeof(wcb_packet_seq_frag_t)`, so it does not rescue it. The library's own comment at `WCB_Client.cpp:569-571` documents the 249-vs-252 size routing, so this is not an accident of my reading.

4. NaviCore registers exactly one raw hook: `NaviCore.ino:4213` `wcb->onRawPacket(naviota::otaRawPacketHook)`. That hook (`navicore_ota.h:488-496`) only acts on `len == sizeof(espnow_struct_ota_ctrl)` (static_assert 55, `navicore_ota.h:83`) or `sizeof(espnow_struct_ota_data)` (static_assert 243, `:84`) — 249 falls out silently. The static_assert message even enumerates 249 as a distinct mesh size to stay clear of. So the reply is dropped, and `rcTelemetry::handle`'s `wcb_alias` case (`rc_telemetry.h:2109-2110`) never fires.

5. Requester set: `?WHOAMI` appears in source only at `WCB.ino:3970` (handler) and `NaviCore/rc_telemetry.h:1774` (`wcb->send((uint8_t)i, "?WHOAMI")`). Nothing in Wizard/, WCBClient/, or SBUSController/ sends it. So the only requester is a WCB_Client host — i.e. the one path that cannot ingest the reply. The sketchbook copy `Arduino-Code/libraries/WCB_Client/src/WCB_Client.cpp` is byte-identical to the master repo (diff -q clean), so no shadowing escape hatch.

INTENT — deliberate change, but the consequence was not the intended one. `git log -L` shows the reply shipped as `useETM=true` in db748b2, and was flipped to `false` in a32e9cb with the comment now at `:3980-3982`. The stated rationale is the pending-table race (`WCB.ino:1100-1107`: the table is loop-task-only; ACKs from the WiFi task are queued via `etmEnqueueAck` precisely to avoid corrupting it). That rationale is real. But the comment's justification — "It's a JSON reply NaviCore re-requests anyway, so reliability isn't needed" — presumes the reply still arrives *sometimes*. It never arrives, because the change also changed the wire SIZE. So this is not a documented trade-off; it is an unintended side effect of a correct concurrency fix. CONFIRMED.

SEVERITY — S2 is too high; two of the three impact claims are false.

(a) "no alias is ever cached" is wrong. The alias reaches NaviCore over WDP: `WCB_WDP.cpp:232-234` emits `WDP_TLV_ALIAS` from `wcb_alias`, `WCB_Client.cpp:2019-2020` decodes TLV 0x01 into `nb.name`, and `NaviCore.ino:4594` `if (nb.name[0]) rcTelemetry::setWcbAlias(nb.wcbNumber, nb.name)`. `rc_telemetry.h:1761-1763` says exactly this: "A named board never reaches the send… ?WHOAMI is only a fallback."

(b) "re-fires on every status poll forever… a burst that can never terminate" is wrong. The reviewer quoted `rc_telemetry.h:1748` and stopped six lines short of the cap: `ALIAS_MAX_TRIES = 4` (`:1754`) and the `_aliasTries[i] < ALIAS_MAX_TRIES` gate at `:1773-1775`. Bounded at 4 queries per online session.

(c) Residual real impact: the fallback is dead code. Worst case with WDP on (default `wdpEnabled = true`, `WCB_WDP.cpp:48`, `:1282`) is display latency — a board already running when NaviCore boots shows unnamed until its next backstop advert, `WDP_PERIOD_MS = 60000` (`WCB_WDP.cpp:52`), instead of ~one status poll; an alias *edited* while up re-adverts within ~500 ms via the dirty-hash check (`WCB_WDP.cpp:373-383`). The one case with no recovery at all is a board with ETM on but `?WDP,OFF` (`WCB_WDP.cpp:1219`, persisted) — then NaviCore can never learn its alias. Consumers of `wcbAlias()` are display-only (`NaviCore.ino:3692`, `:4647`, `:4670`; `rc_telemetry.h:1888`, `:1913`). Cosmetic loss in a non-default config plus 4 wasted round trips per board per session = S3.

FIX — the reviewer's first (primary) option is unsafe; see the correction field.

</details>

### `Code/WCB/WCB.ino:4342` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**forwardMaestroDataToRemoteKyber reads and DISCARDS Maestro bytes past 64 — the exact drop the sibling function was fixed to stop doing**

**Correction to the finding as filed:**

Three corrections.

1. SEVERITY: S3, not S2. The channel is specified as lossy with no delivery, ordering, or integrity guarantees and an explicit "build your own framing + retry" contract (docs/WCB_KYBER_PASSTHROUGH_PARAMS.md §2, §6), and the path is gated behind Maestro_Remote-at-boot (WCB.ino:7446-7449) plus an explicitly configured local Maestro (WCB.ino:4336).

2. FAILURE SCENARIO: the exclusive-loss window is ~5.6 ms to ~22 ms of task starvation at 115200, not the reviewer's ~30 ms / ~300-byte case. Serial1 keeps the Arduino default 256 B RX ring (only UART0 is enlarged — WCB.ino:7119, with the comment at :7112-7118 saying so), so beyond ~22 ms the hardware ring overflows independently and fixing :4342 does not recover those bytes. Below ~5.6 ms nothing is lost. The reviewer's own "~92 bytes accumulated" figure is inside the correct window, so the mechanism stands; the headline scenario just picks the wrong end of it.

3. THE 4323 HALF OF THE FIX IS WRONG (see fix_is_correct). Line 4323 sits in forwardMaestroDataToLocalKyber(), whose only caller is KyberLocalTask (WCB.ino:6579), created only if Kyber_Local (:7443). Kyber_Local and Maestro_Remote are mutually exclusive (WCB_Storage.cpp:1156-1157, :1178-1179, :1454-1466), so the `Maestro_Remote &&` guard at :4323 is false whenever that function runs. The one exception is a pre-reboot transition: `?KYBER,REMOTE` at runtime sets Maestro_Remote=true without zeroing kyberLocalPort (WCB_Storage.cpp:1177-1181), so a board booted Kyber_Local briefly executes :4323 live. That is the only state in which patching :4323 changes anything, and another finding in this same review wave recommends deleting :4311/:4323/:4327 as dead — the two findings contradict each other.

Correct fix, at WCB.ino:4342 only:
    if (remoteLen >= (int)sizeof(remoteBuf)) { sendESPNowRaw(remoteBuf, remoteLen); remoteLen = 0; }
    remoteBuf[remoteLen++] = b;

And per the repo's doc rule, the same commit must correct docs/WCB_KYBER_PASSTHROUGH_PARAMS.md §1/§5, which today describes the drop for the whole target-98 path even though WCB.ino:4275/:4289 stopped dropping in a32e9cb (2026-07-13) — the doc was authored two days later (a8a3c46, 2026-07-15) and never caught up. Once :4342 is fixed, no direction drops, and "≤1 frame per poll" (line 22) becomes wrong for both. That page has no Revision log; add one.

<details><summary>Full verification reasoning</summary>

MECHANISM — verified literally, not from the summary.

`Code/WCB/WCB.ino:4330-4347` is `forwardMaestroDataToRemoteKyber()`. `remoteBuf` is `static uint8_t remoteBuf[64]` (`:4331`), `remoteLen` is a plain local reset to 0 each call (`:4332`). The drain is `while (maestroSerial.available() > 0) { ... uint8_t b = read(); if (remoteLen < (int)sizeof(remoteBuf)) remoteBuf[remoteLen++] = b; }` (`:4339-4343`). The `read()` is unconditional and precedes the bounds test, so once `remoteLen == 64` every further byte is pulled out of the UART ring and thrown on the floor. Single flush at `:4346`. No error path, no counter, no log. The reviewer's transcription of `:4342` is byte-accurate.

The sibling comparison is real. `git log -L 4270,4295:Code/WCB/WCB.ino` shows commit a32e9cb (2026-07-13, "updates") converting `if (!addedToRemote && remoteLen < (int)sizeof(remoteBuf))` into the flush form now at `:4275` and `:4289`, carrying the comment at `:4271-4273`: "If the buffer is full, FLUSH and keep going — don't silently drop bytes past 64, which truncated a large Maestro burst into a corrupt frame." That comment is a record of an observed failure, so the project's own position is that this drop is a bug, not a design. `git log -L 4307,4347` shows `forwardMaestroDataToRemoteKyber` was never touched by that commit — the last change to it was 2179f09 (the `maestroQueryPort` guards). So the asymmetry is accidental, not chosen.

The flush is not a wire constraint: `sendESPNowRaw()` (`:2371-2414`) already chunks at 180 bytes with a 2-byte LE length prefix, so emitting more than 64 bytes per poll is already supported by the transport.

REACHABILITY — narrower than the finding implies, but real.
- Sole caller: `KyberRemoteTask` (`:6588-6592`), 1 ms cadence.
- That task is created ONLY in `setup()` and ONLY `if (Maestro_Remote)` (`:7446-7449`). `Maestro_Remote` is set at boot by `loadKyberSettings()` when `K_Location == "remote"` (`WCB_Storage.cpp:1459-1463`). Setting `?MAESTRO,REMOTE` at runtime (`WCB_Storage.cpp:1177-1181`) does not spawn the task — reboot required.
- `Kyber_Local` and `Maestro_Remote` are mutually exclusive at both setters (`WCB_Storage.cpp:1156-1157`, `:1178-1179`) and at load (`:1454-1466`).
- The port loop requires at least one `configured && remoteWCB == 0 && serialPort > 0` slot (`:4336`). Note the RX side has a legacy "write to Serial1 if no configs" fallback (`:4110-4113`) but the TX side has none — so a legacy Maestro_Remote board with no explicit `?MAESTRO,M…` config forwards nothing at all and never reaches this line.
- In Maestro_Remote mode only Serial1 is opened for the Maestro (`:7227-7231`) and `serialCommandTask` deliberately skips S1 (`:6610-6614`), so nothing else competes for those bytes — the drop is the only consumer.

ARITHMETIC — I redid the window the reviewer hand-waved. Serial1's RX ring is the Arduino-ESP32 default 256 B; the firmware raises only UART0, and the comment at `:7112-7118` says so explicitly ("default 256 B … only affects Serial (UART0 / the USB programming port), not the device ports Serial1-5"). At 115200 8N1 that is 11.52 B/ms. So:
- < ~5.6 ms of task starvation → ≤64 bytes → no loss.
- ~5.6 ms to ~22 ms → 64-256 bytes → the `:4342` cap is the SOLE loss point. This is the bug's exclusive window and it is genuinely reachable: the same `:7113-7115` comment records that an NVS commit "blocks loop() for tens of ms", and loop(), serialCommandTask, RawSerialForwardingTask and KyberRemoteTask are all priority 1 pinned to core 1 (`:7439`, `:7443`, `:7447`, `:7464`), while a flash commit stalls cache on both cores.
- > ~22 ms → the 256 B hardware ring overflows too, so wave1's headline "30 ms stall / ~300 bytes" scenario would have lost ~90 bytes in the driver anyway. Fixing `:4342` does not save that case. The reviewer overstated by picking a scenario where the hardware already loses data.

INTENT — this is where the finding needs correcting, and it is why I am not leaving it at S2. `docs/WCB_KYBER_PASSTHROUGH_PARAMS.md` documents this exact 64-byte silent drop as a published integration parameter (§1 line 24-27 "Design to a 64-byte effective MTU … the excess is read and silently discarded"; §5 line 60 "64-byte batch cap … excess dropped, no error"). It is NOT a deliberate trade-off with a stated reason — §5 is titled "Silent-drop points to avoid" and there is no justifying comment at `:4342` — so this is not a REFUTED-on-intent. But §2 of that same doc is load-bearing for severity: this channel is specified as "fire-and-forget broadcast … No MAC-layer ACK … No application ACK, sequence number, CRC, dedup, or retransmit … No ordering guarantee … Build your own framing, integrity, ordering, and retry above this channel. Treat every frame as independently lossy." A truncated frame here is inside the documented contract; the consumer is already required to detect and recover from it. Combined with the boot-only Maestro_Remote gate, the requirement for an explicit local Maestro config, the low intrinsic volume of the Maestro reply direction (Pololu responses are 1-2 bytes each), and the fact that there is no crash and no persistent corruption — this is S3, not S2. It is a real, avoidable, one-line-fixable data loss on a documented best-effort path, not a fleet-affecting defect.

Secondary observation the finding gets right and is worth keeping: `remoteLen` is declared once at `:4332` and shared across the whole port loop `:4335-4344`, so with two configured local Maestros the first port can consume all 64 and every byte from the second port is read-and-dropped in that pass. (The buffer also concatenates ports with no port tagging, which the proposed fix does not address and is pre-existing.)

</details>

### `Code/WCB/WCB.ino:4402` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**The endsWith("?") help intercept makes every "set the character back to ?" command impossible — including the exact examples the built-in help prints**

**Correction to the finding as filed:**

Three corrections.

1. Not "impossible, unconditionally." `?LF?x` — the legacy form with any trailing character — reaches `updateLocalFunctionIdentifier` (`WCB.ino:5182` → `:5388`), which reads `charAt(2)` and persists `'?'`. So is `?LF?,`. Undocumented and unguessable, but the board is not stranded, and recovery does not require `?ERASE,NVS`.

2. Scope is wider than the char verbs. The intercept precedes the comma split, so *any* local command whose last argument ends in `?` is consumed as help: `?EPASS,pass?` (`WCB.ino:4750`), `?LABEL,S1,Ready?` (`:4570`), `?VAR,SET,n,huh?` (`:4949`), `?SEQ,SAVE,key,<body ending in ?>` (`:4955`). Silent no-op plus a misleading full help dump in every case.

3. This duplicates already-recorded work. `docs/CODE_REVIEW_2026-08.md:487-492` and `docs/CODE_REVIEW_2026-08_verdicts.md:920-924` document this exact mechanism and line as the root cause of the Wizard F-138 finding, with the same two fix options. Worth filing as "still open" rather than as a new discovery.

Correct minimal fix (firmware): keep the intercept but require the remainder to be comma-free —
```cpp
if (message.endsWith("?") && message.indexOf(',') == -1) {
```
Safe: no help topic in `WCB_Help.cpp` contains a comma (grep of every `c == "…"` for a comma returns nothing), so nothing that resolved to real topic help stops resolving. `?ETM?`/`?MAP?` (`WCB_Help.cpp:1020-1022`) still work; `?FUNCCHAR,?`, `?CMDCHAR,?`, `?DELIM,?`, `?EPASS,pass?` now reach their handlers.

That still leaves `?LF?` (no comma) intercepted. If the legacy form must work, add an explicit exemption before the intercept, or accept the `docs/CODE_REVIEW_2026-08_verdicts.md:924` alternative of an explicit `?FUNCCHAR,DEFAULT` spelling alongside the `args.length() == 1` case at `WCB.ino:5046`.

Wizard side, once the firmware lands: relax the `&& config.funcChar !== '?'` term at `Wizard/app.js:7447` and the `if (config.funcChar !== '?')` wrapper at `Wizard/parser.js:1301`, and update the comments at `app.js:7445` / `parser.js:1297` — note their stated mechanism ("re-parsed as a new command invocation") is itself wrong; it is the help intercept.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived from source, not the summary.

`processLocalCommand` starts at `Code/WCB/WCB.ino:4394`. `WCB.ino:4397-4400` handles the bare `?`/`HELP` case. `WCB.ino:4402-4407` is verbatim as quoted:
```
4402:    if (message.endsWith("?")) {  // no space required
4403:        String cmd = message.substring(0, message.length() - 1);
4404:        cmd.trim();
4405:        printCommandHelp(cmd);
4406:        return;
4407:    }
```
It precedes the comma split at `WCB.ino:4410-4416` and every `rootUpper ==` test. `printCommandHelp` (`WCB_Help.cpp:10-13`) does `c.trim(); c.toUpperCase();` — whitespace only, no comma strip — so `"FUNCCHAR,"` misses `c == "FUNCCHAR"` at `WCB_Help.cpp:886` and lands in the unmatched `else` at `WCB_Help.cpp:1015`, dumping the whole top-level reference. The FUNCCHAR setter at `WCB.ino:5045-5054` (`args.length() == 1` → `LocalFunctionIdentifier = args.charAt(0)`) is never reached. Same for CMDCHAR at `WCB.ino:5057-5065` and DELIM at `WCB.ino:5033-5042`. Every cited line number and string in the finding checks out exactly.

REACHABILITY — `processLocalCommand` has exactly one caller, `handleSingleCommand:4381`, which passes `cmd.substring(1)` after matching `cmd.startsWith(String(LocalFunctionIdentifier))` at `:4380`. `LocalFunctionIdentifier` is a single `char` (`WCB.ino:155`), so the strip is exact and the intercept fires whatever prefix char carries the command — the finding's `xFUNCCHAR,?` scenario is correct. No upstream guard: the chain splitter special-cases only `?CS` (`:2015`), `?SEQ,SAVE` (`:2053`) and `?MGMT` (`:2084`); nothing touches a trailing `?`.

The documented examples are real: `WCB_Help.cpp:898` prints `?FUNCCHAR,?    - Set to ? (default)` and `WCB_Help.cpp:905` prints `?LFx   (e.g. ?LF?)`. Both are unexecutable as printed.

INTENT — not a deliberate trade-off. `git log -L 4394,4410` shows the block arrived with the help system in commit 64041df ("updates"); the only comment is `// no space required`, which explains the *no-space* affordance (`WCB_Help.cpp:1020-1022` teaches `Type ?COMMAND?`, e.g. `?ETM?`), not the argument collision. The Wizard treats it as a firmware limitation to route around, not a design: `Wizard/app.js:7445` ("Never send ?FUNCCHAR,? — '?' suffix is re-parsed as a new command invocation") and `Wizard/parser.js:1297-1300`. `WCB.ino:2800` (`if (LocalFunctionIdentifier != '?') emit("FUNCCHAR,"...)`) is consistent with the same constraint.

ALREADY ON RECORD — this is not new. `docs/CODE_REVIEW_2026-08.md:487-492` and `docs/CODE_REVIEW_2026-08_verdicts.md:920-924` describe this exact mechanism, same lines, as the root cause behind the Wizard finding F-138. Still unfixed at HEAD.

WHERE THE FINDING OVERSTATES — "impossible, unconditionally" is wrong. `?LF?x` (any trailing char) works: `message = "LF?x"` does not end with `?`, has no comma so `rootUpper = "LF?X"` matches no new-format block, falls into the legacy chain (banner `WCB.ino:5146`; nothing before it matches — don/doff/dm*/dk*/dpwm*/detm*/dhcr*), hits `message.startsWith("LF")` at `WCB.ino:5182`, and `updateLocalFunctionIdentifier` (`WCB.ino:5388-5396`) takes `charAt(2)` = `'?'` and persists it. `?LF?,` works identically. Undocumented and no user would find it, but the state is recoverable without `?ERASE,NVS`.

WHERE THE FINDING UNDERSTATES — the blast radius is not limited to the char-setting verbs. Any local command whose final argument ends in `?` is swallowed the same way: `?EPASS,pass?` (`WCB.ino:4750-4762`, args passed raw to `setESPNowPassword`), `?LABEL,S1,Ready?` (`:4570`), `?VAR,SET,n,huh?` (`:4949`), and a `?SEQ,SAVE,key,<body ending in ?>` (`:4955`). All silently become a top-level help dump.

SEVERITY — S3, not S2. It is deterministic and user-visible, and a printed example provably cannot work, but: it only bites a board whose funcChar was moved off the default; nothing crashes, no data is lost, the mesh is unaffected; and recovery exists (`?LF?x`, or `?ERASE,NVS`). The genuinely S2-shaped consequence — the Wizard silently dropping the revert at `Wizard/app.js:7447` and then mis-dispatching the rest of the push over ESP-NOW — is a separate, already-filed Wizard finding rated S2 in `docs/CODE_REVIEW_2026-08_verdicts.md:912`. Rating this firmware line S2 as well double-counts that consequence.

FIX — see the fix field; the comma clause is sound, the other two clauses listed are not, and none of them restore `?LF?`.

</details>

### `Code/WCB/WCB.ino:4684` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**?ETM,HB with no value silently stores a 0-second heartbeat interval and turns the board into an ESP-NOW broadcast storm**

**Correction to the finding as filed:**

Keep the finding, fix its mechanism, consequence and severity.

Corrected consequence: not a "broadcast storm of hundreds per second". `scheduleNextHeartbeat` redraws after every fire, so at etmHeartbeatSec==0 the draw is uniform [-1000, 999] (WCB.ino:1011) and the steady-state heartbeat rate is ~4 Hz — a 40x traffic increase of unacked 252-byte broadcasts (~1 KB/s, `sendETMHeartbeat` is a bare esp_now_send with no ETM ACK tracking, WCB.ino:928-942). The damaging effect the finding missed is at WCB.ino:1040: `offlineThresholdMs` collapses to 3-5 s while peers heartbeat every ~10 s, so every peer flaps OFFLINE/ONLINE forever (WCB.ino:1046 / :3769), spamming the console and flapping `wcbPeerOnline()` (WCB.ino:6868), which gates cap-route failover (WCB.ino:5835) and WDP owner election (WCB_WDP.cpp:443).

Corrected trigger set: the bare `?ETM,HB` is one path, but `?ETM,HB,0` typed to mean "disable heartbeats" is at least as likely, and the only fleet-wide path is the Wizard — clearing the pre-filled ETM heartbeat field makes `parseInt("")` NaN (Wizard/app.js:10781, `?? 10` never fires because `.value` is ""), which `Wizard/parser.js:1490` emits as `ETM,HB,NaN` → atol("NaN")==0 on every board in the push. A Wizard-pushed NEGATIVE is also reachable (Wizard/index.html:174 has no `min`; app.js:1216 `parseInt(...)||10` passes −5) and IS the unconditional every-loop-pass storm the finding described, because random(-6000,-4000) is always negative.

Corrected severity: S3.

<details><summary>Full verification reasoning</summary>

DEFECT IS REAL — traced end to end.

Parse: `processLocalCommand` (WCB.ino:4394). Help is intercepted only for a message ending in "?" (WCB.ino:4401-4406), so `?ETM,HB` (no trailing ?) falls through. Split at :4384-4390 gives rootCmd="ETM", args="HB". Inside the ETM branch, WCB.ino:4665-4669: `secondComma = args.indexOf(',')` is -1, so `etmCmd="HB"`, `etmVal=""`. WCB.ino:4684 `etmHeartbeatSec = etmVal.toInt();` — I opened the core: `String::toInt()` is `atol(buffer())` (esp32 3.3.4 WString.cpp:904-909) and atol("") == 0. WCB.ino:4685 `saveETMSettings()` writes it (WCB_Storage.cpp:2215, key "etmHB" = 5 chars, namespace "etm_config" = 10 chars — both well under the 15-char NVS limit, so the reviewer's implied persistence is real). `loadETMSettings()` reads `getInt("etmHB", 10)` with no clamp (WCB_Storage.cpp:2228). Grep confirms the ONLY writers of etmHeartbeatSec are the declaration (WCB.ino:266), WCB.ino:4684, WCB.ino:5321 and the NVS load — no clamp anywhere. Legacy hole confirmed reachable: WCB.ino:5321 `message.substring(5).toInt()` is in the same processLocalCommand else-if chain (function starts :4394; "ETMHB" has no comma so rootUpper != "ETM" and it falls through the new-format block), and bare `?etmhb` gives substring(5)=="" → 0.

UNDERFLOW math re-derived, not trusted: WCB.ino:1011 `random((etmHeartbeatSec-1)*1000UL, (etmHeartbeatSec+1)*1000UL)`. `(0-1)` is int -1; the other operand is unsigned long, so -1 converts to 4294967295UL, ×1000 mod 2^32 = 4294966296UL, which as the `long` parameter is -1000. Second arg = 1000. Core impl (WMath.cpp:63-68, 51-61): howsmall<howbig → diff=2000 → `esp_random()%2000 + (-1000)` → uniform on [-1000, 999]. Exactly 50% of draws are negative; `intervalMs = (unsigned long)(negative)` makes `etmNextHeartbeatMs = millis() + intervalMs` (WCB.ino:1016) a wrapped past value, so `millis() >= etmNextHeartbeatMs` (:1035) fires immediately. So the underflow claim stands.

BUT THE CLAIMED CONSEQUENCE IS WRONG BY ~2 ORDERS OF MAGNITUDE. Each fire calls `scheduleNextHeartbeat(false)` again (:1036), which redraws. The immediate-refire run is geometric(p=0.5), mean 2, then a positive draw waits uniform 0-999 ms. Expected rate ≈ 2 sends per ~500 ms ≈ 4 Hz — not "every loop iteration (hundreds per second)". `sendETMHeartbeat()` (WCB.ino:928-942) is a bare `esp_now_send` of one 252-byte struct with no ETM ACK tracking and no retry, so there is no per-board pending-ACK fan-out (CLAUDE.md rule 3 territory). ~4 × 252 B/s ≈ 1 KB/s. That does not saturate ESP-NOW, does not starve WDP adverts, and does not "degrade the whole mesh". The "broadcast storm" framing is unsupported.

THE FINDING MISSES THE ACTUALLY DAMAGING EFFECT. With etmHeartbeatSec==0, WCB.ino:1040 `offlineThresholdMs = (0+1) * etmMissedHeartbeats * 1000UL` collapses to 3000 ms (NVS default 3, WCB_Storage.cpp:2229) or 5000 ms (RAM default 5, WCB.ino:267). Peers still heartbeat on their own ~10 s cadence, and presence is refreshed only on a received ETM packet (WCB.ino:3766-3769). So this board flips every peer OFFLINE (:1046) and back ONLINE (:3769) continuously — console spam, and `wcbPeerOnline()` (WCB.ino:6868-6880) flaps, which gates cap-route failover at WCB.ino:5835 and WDP owner election at WCB_WDP.cpp:443. That is the real functional break, and the reviewer did not identify it.

TRIGGER PLAUSIBILITY — weaker than claimed but not absent. There is no query form (WCB_Help.cpp:283-287 documents only `?ETM,HB,15`; the value is readable via `?config`, WCB.ino:2252). `?ETM,HB?` is safe (help intercept :4401). The bare-setter slip is inconsistent with this parser's own convention — `?MAC` (:4728-4731) and `?EPASS` (:4751-4753) both print "Invalid format. Use: …" — so it is an oversight, not a documented trade-off. Equally likely triggers the finding didn't name: a deliberate `?ETM,HB,0` meaning "stop heartbeats", and a genuinely fleet-wide Wizard path — `Wizard/app.js:10781` `parseInt(get('wiz-etm-hb')?.value ?? 10)` gives NaN when the pre-filled field is cleared (`.value` is "", so `??` never fires), NaN flows into `cfg.etm` (app.js:11000/11061/11136) and `Wizard/parser.js:1490` emits `ETM,HB,NaN` verbatim → atol("NaN")==0 on every board pushed. Negatives are also pushable (`Wizard/index.html:174` has no `min`; `app.js:1216` `parseInt(...)||10` maps 0→10 but passes −5), and a negative is strictly worse: random(-6000,-4000) is ALWAYS negative, so that really is a fire-every-loop-pass storm. A 0 does NOT replicate through a config backup — `Wizard/parser.js:827` is `parseInt(parts[2]) || 10`.

SEVERITY — S3, not S2. Real, persists in NVS across reboot, and the confirmation line "ETM heartbeat set to 0 sec" is the only signal at the moment of the mistake. But: trigger is malformed/out-of-range operator input, no crash, no corruption, no data loss, single board (unless via the Wizard NaN push), and recovery is one command (`?ETM,HB,10`). The console immediately fills with "[ETM] WCBn went OFFLINE (no heartbeat for 3s)", which makes it self-announcing rather than silent-and-lasting.

</details>

### `Code/WCB/WCB.ino:5046` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**?FUNCCHAR accepts ';' with no conflict check, and handleSingleCommand tests the function identifier first — the entire ';' command family is permanently shadowed**

**Correction to the finding as filed:**

Real defect, but three things in the finding need correcting.

(1) TITLE — "permanently shadowed" is false. The state is persistent (NVS) but recoverable in place: with funcChar == ';' the ';' family routes to `processLocalCommand`, so `;FUNCCHAR,/` restores full function and `;ERASE,NVS` resets everything. What is genuinely unrecoverable is only the literal default '?' (blocked by the `endsWith("?")` intercept at WCB.ino:4402 and by the Wizard's deliberate `config.funcChar !== '?'` guard at app.js:7447) — that costs a full NVS wipe, not a reflash.

(2) FAILURE SCENARIO — replace the "typo for ?CMDCHAR,;" story, which is weak, with the Wizard path, which is much stronger and is what actually makes this worth fixing: `Wizard/index.html:151` is an unvalidated one-char text field wedged between the delimiter and cmdchar fields; `app.js:1177-1182` fans its value out to every board; `app.js:7447` pushes `?FUNCCHAR,;` to each one. The push then *looks successful* — every subsequent chain command carries the new ';' prefix and is still dispatched to `processLocalCommand` — so the operator gets a clean green push and discovers the breakage only when device commands stop working. Also correct the `;S1open` claim: it is not silent, it prints the deprecation warning (WCB.ino:5661) and then "Invalid baud rate" (WCB_Storage.cpp:161), because updateBaudRate whitelists the value. The actually-destructive shadowed case is the 3-char form `;S10`, which hits WCB.ino:5647 and silently persists `serialBroadcastEnabled[0] = false`.

(3) SEVERITY — S3. Self-inflicted, warned against in the help the command itself prints, in-place recoverable, no data loss.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived, every load-bearing fact holds.

1. Dispatch order is exactly as quoted. `Code/WCB/WCB.ino:4380` `if (cmd.startsWith(String(LocalFunctionIdentifier))) processLocalCommand(cmd.substring(1));` / `:4384` `else if (cmd.startsWith(String(CommandCharacter))) processCommandCharcter(...)` / `:4388` broadcast. The funcChar test wins on equality. Cited line numbers are exact.

2. Declarations: `WCB.ino:155` `char LocalFunctionIdentifier = '?';`, `:156` `char CommandCharacter = ';';`, `:151` `char commandDelimiter = '^';`.

3. No guard exists at any writer. I grepped every write site of both chars across `Code/WCB/*.ino|*.cpp|*.h`. Assignments: `WCB.ino:5047` (?FUNCCHAR, condition is only `args.length() == 1` at :5046), `:5059` (?CMDCHAR, same), `:5390` (legacy `?LFx`, `updateLocalFunctionIdentifier`, condition only `message.length() >= 3`), `:5400` (legacy `?CCx`). `setCommandDelimiter` (`WCB_Storage.cpp:536`) has no guard either. There is no comparison of the three chars anywhere in the firmware — the only reads are prefix tests.

4. Persistence confirmed: `WCB_Storage.cpp:518-522` `putString("LocalFunc",…)/putString("CmdChar",…)` in namespace `command_config`; restored at `WCB_Storage.cpp:503-513`, called at boot from `WCB.ino:7415`. NVS keys "LocalFunc" (9) and "CmdChar" (7) are both under the 15-char limit — fine.

5. The constraint IS documented and NOT enforced: `WCB_Help.cpp:901` prints `"  - Must not conflict with CommandCharacter (;)"` under ?FUNCCHAR, and `WCB_Help.cpp:930` prints the mirror `"  - Must not conflict with LocalFunctionIdentifier (?)"` under ?CMDCHAR. Nothing acts on it. No code comment anywhere claims the permissiveness is deliberate; `git log -S` on the help string returns only 64041df "updates". So this is an unenforced documented constraint, not a documented trade-off — that is a real gap, not a REFUTED.

REACHABILITY — real, and the strongest vector is one the reviewer did not name.
- Serial CLI: `?FUNCCHAR,;` is accepted verbatim. Reachable but requires the operator to type the exact thing the help warns against; the reviewer's "plausible typo for ?CMDCHAR,;" is weak, since ';' is already the CMDCHAR default and nobody types that.
- The Wizard is the real vector and has no validation at all. `Wizard/index.html:151` is a free-text `maxlength="1"` input (`g-funcchar`) sitting immediately between the delimiter and cmdchar boxes (:146, :156). `app.js:1177-1182` copies its value into `systemConfig.general.funcChar` and then into **every** `boardConfigs[n].funcChar`; the push path reads it at `:7413` and the bootstrap at `:7447` sends `${curFuncChar}FUNCCHAR,${config.funcChar}` to each board. I grepped `Wizard/*.js` for any funcChar/cmdChar collision check — there is none (only `:7436`/`:7447` change-detection). So one mis-typed character in the Advanced section propagates fleet-wide.
- Worse, the push then *appears to succeed*: after the board flips its funcChar to ';', every remaining command in the chain is prefixed with ';' and still lands in `processLocalCommand`, so the Wizard sees clean responses. The damage only surfaces later when `;M11`, `;Cwave`, `;S1…` stop working.

CONSEQUENCE — verified, with two corrections to the reviewer's detail.
- `;M11` → `processLocalCommand("M11")`: no root match, falls through the legacy chain (5204-5343) to `WCB.ino:5346` `"Unknown command: %s"`. Correct as claimed.
- `;S1open` is NOT "swallowed with no output". `WCB.ino:5343` `startsWith("S")` → `updateSerialSettings` (`WCB.ino:5644`); length is 6 so the `length()==3` branch is skipped, `baud = "open".toInt() = 0`, it prints the deprecation warning at :5661 and calls `updateBaudRate(1,0)`, which the whitelist at `WCB_Storage.cpp:157-162` rejects with "Invalid baud rate". Noisy, not silent, not destructive.
- The genuinely destructive case the reviewer missed is the 3-char form: `;S10` (sending the byte "0" out port 1) hits the `length()==3` branch at `WCB.ino:5647-5656`, sets `serialBroadcastEnabled[0] = false` and persists it to NVS. So a shadowed board silently rewrites its own broadcast config from ordinary traffic.

SEVERITY — S3, not S2.
- It cannot happen without the operator explicitly entering a character the help warns against; nothing in the firmware or mesh generates it.
- "Permanently shadowed" in the title is wrong, and the finding's own body contradicts it: since ';' now routes to `processLocalCommand`, `;FUNCCHAR,/` fully restores function in place, and `;ERASE,NVS` is the nuclear option. No reflash needed, no data loss.
- The one aggravator that is real: you cannot get back to the literal default '?'. `;FUNCCHAR,?` is intercepted by the `endsWith("?")` help branch at `WCB.ino:4402-4406` (already adjudicated as F-138/F-387, `docs/CODE_REVIEW_2026-08_verdicts.md:920`), and the Wizard refuses to push '?' by design (`app.js:7447` `config.funcChar !== '?'`, with the comment at :7445 explaining why). So restoring the exact default requires a full `;ERASE,NVS`. That is an annoyance on top of a self-inflicted, in-place-recoverable state — S3.

No prior verdict exists for F-388 (`grep F-388 docs/CODE_REVIEW_2026-08_verdicts.md` and `CODE_REVIEW_2026-08.md` both empty), so this is not a re-raise.

</details>

### `Code/WCB/WCB.ino:5279` — CONFIRMED · S2 → **S3**

**?SLS<x>,<label> is completely dead — the caller strips "SLS" and updateSerialLabel() strips it again**

**Correction to the finding as filed:**

Two corrections. (1) Line number: the `startsWith("S")` guard is at WCB.ino:5475, not :5473 (:5474 is its comment). (2) Blast radius is smaller than described: the "restored legacy backup" scenario does not apply to current backups — printBackupConfig emits the modern `?LABEL,Sx,text` (WCB.ino:2807) and that handler works (:4587-4595), and the Wizard never sends ?SLS (grep hits only the committed .bin images). The real reachable exposure is (a) the built-in help example WCB_Help.cpp:159 / WCB_Help.h:187, and (b) two live copy-paste chain generators that still emit the dead form — WCB_Storage.cpp:1417 (storeKyberSettings, "COPY-PASTE SETUP COMMANDS FOR OTHER WCBs") and WCB_Storage.cpp:2197 (printKyberList) — so a user pasting the board's own generated setup chain into another WCB gets "Invalid format" lines and unlabelled ports. Failure is loud, and serialPortLabels[] is display/WDP-advertisement metadata only (WCB.ino:2144/:2156, WCB_Storage.cpp:284-310/:1529, WCB_WDP.cpp:297/:1119) — nothing functional depends on it. Hence S3. Also, strictly, "no input can reach the save" is slightly overstated: a garbage token such as `?SLSSAB3,x` yields portStr=="3" and does save; no sane spelling works. If the fix is applied, also consider updating WCB_Storage.cpp:1417/:2197 to emit the modern `?LABEL,S<port>,<text>` form so the generated chains stop depending on a legacy alias. Do NOT adopt the wave-1 variant's extra suggestion of changing the :5475 guard to test `message.substring(2)` — with the full token that guard is tautological either way, and touching it adds risk for no benefit.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived from source, not the summary.

Dispatch (Code/WCB/WCB.ino:5278-5279):
  `} else if (message.startsWith("sls") || message.startsWith("SLS")) {`
  `    updateSerialLabel(message.substring(3));`

Callee (WCB.ino:5467-5504), read line-by-line via `grep -n`:
  :5469 `if (message.length() < 4)` reject
  :5475 `if (!message.startsWith("S") && !message.startsWith("s"))` reject   <- finding cited :5473; the guard is actually at :5475 (the comment "Check for 'S' prefix after SL" is at :5474). Off-by-two in the finding's transcription, harmless.
  :5480 `int commaPos = message.indexOf(',');`
  :5487 `String portStr = message.substring(3, commaPos);  // Skip "SLS"`
  :5490 `if (port < 1 || port > 5)` reject
  :5503 `saveSerialLabelToPreferences(port, label);`

The callee is unambiguously written for the FULL token: only "SLS1,x" makes :5487 (`substring(3,4)`=="1") produce a valid port. The caller removes exactly those 3 chars first. Trace of the help example `?SLS1,Marcduino`: handleSingleCommand (:4380) strips the LocalFunctionIdentifier → processLocalCommand("SLS1,Marcduino"); the new-format section splits on the first comma so rootUpper=="SLS1", which matches no `rootUpper ==` case; it falls to the legacy chain and hits :5278; updateSerialLabel receives "1,Marcduino"; :5475 fires; "Invalid format. Use: ?SLSx,label where x=1-5"; nothing saved. Dead, exactly as claimed.

Alternate spelling `?SLSS1,x` → callee gets "S1,x", commaPos==2, `substring(3,2)`. I verified Arduino's swap behaviour against the actually-pinned core, not memory: esp32 3.3.4 `cores/esp32/WString.cpp:754-759` swaps left/right when left>right, so this yields "," → toInt()==0 → rejected at :5490. Confirmed. (Pedantic caveat: the finding's absolute "there is no input that reaches the save" is slightly overstated — a garbage token like `?SLSSAB3,x` gives portStr=="3" and does save to port 3. Nonsense input; does not affect the verdict.)

REACHABILITY — not shadowed, and reachable from more than a typo.
I extracted every literal in the legacy chain in dispatch order (WCB.ino:5150-5290): don DON doff … pms pclear plist prs pos po px **sls SLS** slc SLC sbi SBI sbo SBO … Nothing earlier is a prefix of "sls"; the `s<x>` catch-all is last. So the branch is genuinely entered and the double-strip is genuinely executed. `grep -rn updateSerialLabel Code/` returns exactly one definition (:5467) and exactly one call site (:5279) — no overload, no alternate caller.

Live emitters of the dead form, both user-facing copy-paste chains:
 - WCB_Storage.cpp:1417 `command += "^?" + String("SLS") + String(targetPort) + ",Maestro " + …` inside `storeKyberSettings()`, under the banner at :1341 "COPY-PASTE SETUP COMMANDS FOR OTHER WCBs".
 - WCB_Storage.cpp:2197 same shape inside `printKyberList()`.
Help advertises the exact rejected syntax: WCB_Help.cpp:159 `"  ?SLSx,label   - Set label  (e.g. ?SLS1,Marcduino)"` and WCB_Help.h:187. Commands arriving over ESP-NOW funnel through the same handleSingleCommand, so a remote `?SLS` is equally dead.

INTENT — no evidence this is deliberate. The adjacent :5283/:5286 lines carry the explicit comment `// pass FULL "SBISxON" — the fn strips "SBI" itself (was double-stripped here → whole command was dead)`, i.e. this exact class of bug was found and fixed for the two neighbouring commands and ?SLS was missed. `git log -L 5278,5280` shows the line entering at 64041df (2026-02-25) already in the broken form. No comment or doc defends it.

WHY I DOWNGRADE S2 → S3. The reviewer's blast-radius claims are inflated on two points I checked:
 - "a restored legacy backup" — printBackupConfig emits the MODERN form, `emit("LABEL,S" + String(i+1) + "," + serialPortLabels[i], true)` at WCB.ino:2807, and the live `?LABEL,Sx,text` handler at :4587-4595 works. Current backup/restore is unaffected.
 - Wizard does not send ?SLS: `grep -rn "SLS" Wizard/` hits only the committed .bin firmware images, no JS.
 - Failure is loud (an "Invalid format" line), not silent, and serialPortLabels[] is display/metadata only — printing (WCB.ino:2144/:2156, WCB_Storage.cpp:284-310, :1529) and WDP advertisement (WCB_WDP.cpp:297/:1119). No routing, baud, or delivery decision reads it.
So: a real, 100%-reproducible defect in a command the board's own help and its own copy-paste generator still hand the user, but on a legacy path with a working modern equivalent, loud failure, and display-only consequence. S3, not S2.

FIX — correct as proposed (`updateSerialLabel(message);` at :5279), and I checked both case paths since the legacy chain accepts all-lower and all-upper: "SLS1,Marcduino" → len 14 ✓, :5475 startsWith("S") ✓, commaPos 4, substring(3,4)=="1" ✓, label substring(5) ✓ → saves. "sls1,marcduino" → :5475 startsWith("s") ✓, same result. Multi-digit "SLS10,x" → substring(3,5)=="10" → correctly rejected at :5490. Labels containing commas survive (first comma only). No other caller to disturb.

</details>

### `Code/WCB/WCB.ino:5772` — CONFIRMED · S2 → **S3**

**printBackupConfig emits the live-chain checksum as <funcChar>CHK, but both verifiers hard-code "?CHK" — restore integrity checking is silently skipped on any board with a custom FUNCCHAR**

**Correction to the finding as filed:**

Mechanism and location are right; two details are off. (1) The `?CHK` site at :6440 is in `processSerialCommandHelper` (:6423), not `handleSerialData`, and it is NOT on the backup-restore path: `collectConfigCommands` emits `HW,<v>` first (:2777), so the chain starts `<lfi>HW,` and the `startsWith(lfi + "C")` gate at :6436 is false. The single verifier that matters for a restore paste is :1953. (2) Severity is S3, not S2 — it requires a non-default FUNCCHAR *and* a corrupted paste, costs a safety net rather than causing loss, and leaves a visible "Unknown command: CHK…" plus a conspicuously missing "✓ Command checksum VERIFIED". Also worth recording as a constraint: the inverse fix (making the writer emit a literal "?CHK" in the live chain) is actively worse — if that token ever survives to dispatch on a custom-funcChar board it matches neither `LocalFunctionIdentifier` nor `CommandCharacter` at :4380/:4384 and falls into `processBroadcastCommand` (:4388), pushing the checksum text out over the mesh and serial ports as data. Fix the verifiers, not the writer.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived line by line, nothing taken from the reviewer's transcription.

Writer: `Code/WCB/WCB.ino:5733` `String lfi = String(LocalFunctionIdentifier);`; the emitter at `:5744-5750` appends `lfi + c` to the live chain (`:5747`) and `defaultFunc + c` to the factory chain (`:5748`); `:5772` `String checksumCmd = lfi + "CHK" + String(crcStrBuf);` and `:5778` `chainedWithChecksum = chainedConfig + String(commandDelimiter) + checksumCmd;`. The factory form at `:5776` is the fixed literal with the explaining comment "factory reset always uses ?" — that one IS deliberate; the live one is not commented as deliberate.

`LocalFunctionIdentifier` is a single `char` defaulting to `'?'` (`WCB.ino:155`), settable at runtime by `?FUNCCHAR,x` (`WCB.ino:5044-5053`) and the legacy `?LFx` (`:5388-5393`), and persisted/reloaded via `saveLocalFunctionIdentifierAndCommandCharacter` / `loadLocalFunctionIdentifierAndCommandCharacter` (`WCB_Storage.cpp:518` / `:503`, loaded at boot `WCB.ino:7415`). It is a documented, supported setting (`WCB_Help.cpp:886-905`) and is round-tripped by the Wizard (`Wizard/app.js:7443-7448`, `Wizard/parser.js:320-324`, `:1303`).

Verifiers: only two exist. `WCB.ino:1953` `data.lastIndexOf(String(commandDelimiter) + "?CHK")` with the lowercase fallback at `:1955`; and `WCB.ino:6440` `String chkPattern = String(commandDelimiter) + "?CHK";` (fallback `:6443`). Both take `commandDelimiter` live and hard-code only the `'?'` — so this is a straight omission, not a fixed grammar. `grep -rn "CHK"` over `Code/WCB/` returns exactly six sites (`:1953, :1965, :2960, :5772, :5776, :6440`); there is no third verifier, and `verifyBackupChecksum` (`:5792`) has no caller anywhere in the tree.

REACHABILITY — traced the actual paste route. A pasted `?BACKUP` "For Configured Boards" chain enters `processSerialCommandHelper` (`:6423`). `collectConfigCommands` emits `HW,<v>` first (`:2777`), so the chain begins `<lfi>HW,` and the `data.startsWith(lfi + "C")` gate at `:6436` is FALSE — the `?C` path is not on the restore route at all (it serves `?CS`-style stored-command chains). The timer test at `:6489` is skipped because the chain starts with the funcChar, so control reaches `parseCommandsAndEnqueue(data, sourceID)` at `:6495`. There, with funcChar `'#'`, the chain ends `…^#CHK1A2B3C4D`, `lastIndexOf("^?CHK")` returns -1, `lastIndexOf("^?chk")` returns -1, and the entire verification block `:1958-1997` — including the "✗ Command checksum FAILED … Aborting restore" abort at `:1992-1996` — is skipped. The chain is then split normally and every config token applied. The leftover `#CHK…` token dispatches via `:4380` into `processLocalCommand("CHK1A2B3C4D")`: no comma so `rootUpper` is the whole token and matches no verb; in the legacy chain it does not match `cc`/`cs`/`ce` (`:5187-5191`) or anything else, so it lands on `Serial.printf("Unknown command: %s\n", …)` at `:5346`. Harmless, but it is the only symptom. The Wizard never sends a checksum token at all (`grep CHK Wizard/` yields only `ETM,CHKSM` and the parser's `case 'CHK': break` at `parser.js:902`), so the manual serial paste is the sole consumer of this CRC — there is no second safety net.

INTENT — the comments at `:5735-5743` explain includeInLive, the non-flipping separator and the flipping `defaultFunc`; `:5764-5768` explains the `%08X` padding fix; `:1949-1952` explains the `lastIndexOf` choice (git blame a32e9cb5, 2026-07-13). None of them says the live checksum is meant to be unverifiable on a custom funcChar. This is an omission, not an explained trade-off.

SEVERITY — downgrading to S3. It needs a non-default `?FUNCCHAR` AND an independently corrupted/truncated paste before anything bad happens; the loss is a safety net, not a wire-format break or data loss, the misapplied config is re-pastable, and the operator does get a weak tell (the missing "✓ Command checksum VERIFIED" plus a stray "Unknown command: CHK…"). Real, but conditional on two independent uncommon events.

Two factual corrections to the write-up, both cosmetic: the second verifier lives in `processSerialCommandHelper` (`:6423`), not `handleSerialData`; and that path is not actually on the config-restore route (see above), so in practice there is one live verifier, not two.

</details>

### `Code/WCB/WCB.ino:5894` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**;S<port>,<msg> sends a literal leading comma — the separator every sibling runtime verb strips**

**Correction to the finding as filed:**

The finding stands but two things need correcting.

1. "Nothing is logged on the WCB" is false. WCB.ino:5909-5910 `Serial.printf("Sent to %s: %s\n", getSerialLabel(target).c_str(), serialMessage.c_str())` prints the exact payload — stray comma included — under `?DEBUG,ON`.

2. The `;V` citation should be `WCB_Variables.cpp:285` for the tolerance line (`if (a.length() && a[0] == ',') a = a.substring(1);` in `processVarConfig`), not :288.

3. Direction of the fix is genuinely ambiguous and the write-up leans the wrong way. The internal forwarder at WCB.ino:6275 (`";S" + String(destPort) + cmd`) and the wiki's canonical table (Command-Reference.md:594 `;Sxmessage`, example `;S1:PP100`) both define `;S<port>` as a byte-transparent pipe with no separator. Whichever direction is chosen, the OTHER side must be fixed in the same commit — WCB_Help.cpp:920, :1058, WCB_Help.h:150, TestConfigs/TestPlan.md:115-117,190, docs/SEQUENCE_INVENTORY.md:25,278,301, and wiki Command-Reference.md:656,661-662,972,1013 + Configuration-Guide.md:588.

If the strip IS adopted, it must be done as:
    String serialMessage = message.substring(2);
    serialMessage.trim();
    if (serialMessage.startsWith(",")) { serialMessage = serialMessage.substring(1); serialMessage.trim(); }
(trim → strip → trim, matching WCB_WLED.cpp:121-123), AND WCB.ino:6275 must double a leading comma to stay lossless:
    String espnowCmd = ";S" + String(destPort) + (cmd.startsWith(",") ? "," : "") + cmd;

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived from source, every claim holds.

`Code/WCB/WCB.ino:5880-5911` is `processSerialMessage`. I read it verbatim:
- `:5887` `int target = message.substring(1, 2).toInt();` — one digit only.
- `:5894` `String serialMessage = message.substring(2);`
- `:5895` `serialMessage.trim();`
No comma handling anywhere between `:5880` and the write. The reviewer's line numbers are exact (verified with `awk NR`).

`String::trim()` really is whitespace-only in the PINNED core: `c:/Users/ghulette/AppData/Local/Arduino15/packages/esp32/hardware/esp32/3.3.4/cores/esp32/WString.cpp:880-894` — two `isspace()` loops, nothing else. A leading `,` survives.

`writeSerialString` is at `WCB.ino:1862-1867` (not 1862 by luck — the prototype is at `:910`): `String completeString = stringData + '\r';` then a byte-for-byte `serialPort.write()` loop. So `;S1,TEST` genuinely puts `,TEST\r` on S1. Confirmed.

The four sibling strips are real and at the cited lines (I opened each):
- `WCB_MP3.cpp:98` `if (rest.startsWith(",")) rest = rest.substring(1);`
- `WCB_DFP.cpp:74` identical
- `WCB_WLED.cpp:122` identical (preceded by `rest.trim()` at :121 and followed by `rest.trim()` at :123)
- `WCB_Variables.cpp:285` `if (a.length() && a[0] == ',') a = a.substring(1);` (the reviewer cited :288 for the `;V` path; the `?VAR` tolerance is at :285 — same idea, cite is one function off but the claim stands)
`;P` (`processPWMOutput`, `WCB.ino:6187-6196`) likewise has no strip — `message.substring(2).toInt()` on `",1500"` yields 0 and is rejected at `:6193`. That is the separate F-390, not this one.

REACHABILITY — real, and the comma form is what the SHIPPED help tells users to type:
- `WCB_Help.cpp:920` `";Sx,msg       Send msg to serial port x"` (inside the `?CMDCHAR` topic)
- `WCB_Help.cpp:1058` same line in the top-level menu
- `WCB_Help.h:150` `;Sx,message   Send message to local serial port x (1-5)`
- `TestConfigs/TestPlan.md:115` `From WCB2: ;S1,TEST` → expected `"TEST" appears at device`; also `:116`, `:117`, `:190`
- `docs/SEQUENCE_INVENTORY.md:25`, `:278`, `:301` all store `;S5,<CA1021>`
- the wiki is internally split: `Command-Reference.md:656` `;T5000,;S1,Test`, `:661-662` `;S1,:PS1^;T1000^;S1,:PS2`, `:972`, `:1013`, `Configuration-Guide.md:588` `;S1,A&;S2,B&;S3,C` — every one of those sends a stray comma.
Only caller is `WCB.ino:5850` in `processCommandCharcter`; nothing upstream touches commas — `commandDelimiter` is `'^'` (`WCB.ino:151`), and the chain splitter (`:2097`) splits on that, not on `,`. So there is no guard anywhere.

INTENT — no comment marks this as deliberate, and `git log -L 5880,5915:Code/WCB/WCB.ino` shows the substring(2)+trim() shape is unchanged since the very first commit (483b4d8); the only edit (b0bd206) added the `target == 0` USB branch. BUT there is a strong counter-signal that the comma-less grammar is the designed one, which the reviewer found and correctly reported: `WCB.ino:6275` `String espnowCmd = ";S" + String(destPort) + cmd;` — the serial-mapping forwarder wraps ARBITRARY device bytes in `;S<port>` with no separator, i.e. it treats `;S<port>` as a byte-transparent pipe. The user-facing wiki agrees: `Command-Reference.md:594` `| ;Sxmessage | Send to Serial port x (1-5) | ;S1:PP100 |`, with `:599-600`, `:913`, `:971`, `:983`, `:1029` (`;S1Hello World`), `:1037` all comma-less — and the wiki explicitly offers a comma ALTERNATIVE for `;W` (`:607-611`) but not for `;S`. The in-source `;w` comment at `WCB.ino:5947` also spells it `;s4:PP100,50:W42:P`.

So the finding is real: two conventions coexist in one codebase and only one works. A user following the on-board `?` help, the TestPlan, or SEQUENCE_INVENTORY gets a corrupted payload. That is a genuine defect — but it is at least as much a doc defect as a code defect, and the reviewer's own text acknowledges the alternative.

SEVERITY — S2 is too high; I call it S3:
- The canonical/most-used form (`;S1:PP100`) works, is what the wiki table documents, and is what every internal emitter produces.
- The reviewer's "Nothing is logged on the WCB" is wrong: `WCB.ino:5909-5910` prints `Sent to <label>: %s` with the payload including the stray comma whenever `debugEnabled` — the first thing anyone does when a device is silent.
- No state corruption, no mesh-wide effect, no data loss; workaround is deleting one character.
- Unchanged since the initial commit — not a regression introduced by this release.

</details>

### `Code/WCB/WCB.ino:6125` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**Nested ;C / ;SEQ recalls have no depth or cycle guard — a self-referencing stored sequence loops forever inside loop()'s queue drain**

**Correction to the finding as filed:**

The defect is real but the fix and one framing point are wrong. (1) There is NO stack recursion — `recallStoredCommand` returns immediately after `parseCommandsAndEnqueue` enqueues; the nested recall executes later, from a fresh top-level call in a subsequent iteration of the same drain. A static depth counter incremented on entry and decremented on exit of `recallStoredCommand` is therefore always 1 and would be a complete no-op. (2) The runaway is drain-scoped, so the guard must be too. Correct shape: keep a small per-drain-pass set of keys already recalled during this pass, cleared at the top of the drain at WCB.ino:7501-7503, and have `recallStoredCommand` refuse (with a printed reason) a key already in that set. That breaks self-reference and mutual recursion while leaving legitimate one-level sub-sequence calls (`;Cfull` → `;Cpart1` + `;Cpart2`) working. Additionally, bound the drain itself (`for (int n = 0; n < N && xQueueReceive(...) == pdTRUE; n++)`) so loop() always returns and ETM heartbeats / WDP / OTA keep running even under a runaway. (3) Scope: this only fires for a body with no `;T`/`;t` token — a timer-bearing recursive sequence goes through `parseCommandGroups` (WCB_Storage.cpp:614-616) and does not hang. (4) Reachability wording: `?Cwave,<body>` is not a valid save form (use `?SEQ,SAVE,wave,…` or legacy `?CSwave,…`), and the board is recoverable over serial with `?SEQ,CLEAR,<key>` or `?REBOOT`, not only by power cycle. Worth also fixing the wiki (Stored-Commands.md:681-693), which teaches recursion and offers `?STOP` as the stop mechanism — `?STOP` only stops TIMER sequences (command_timer.cpp:53-63, dispatched at WCB.ino:6429-6432) and cannot break a timer-less recall loop.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived from source, not the summary.

1. No guard exists. `recallStoredCommand` (WCB.ino:6085-6126) parses the key, optionally broadcasts the trigger (:6119-6122), then calls `recallCommandSlot(key, sourceID)` (:6125) and returns. There is no depth counter, no in-flight key set, no self-reference check. `recallCommandSlot` (WCB_Storage.cpp:556-620) reads the body from NVS (:557-559), strips comments, sets `inSequenceBody = true` (:613) and calls `parseCommandsAndEnqueue(stripped, sourceID)` (:617) — which splits on `commandDelimiter` and pushes each token via `enqueueCommand` (WCB.ino:2110 → :1912-1943). `?SEQ,SAVE` (WCB.ino:4993 → `saveStoredCommandsToPreferences`, WCB_Storage.cpp:623-660) validates only non-empty key/value — it will happily store a body that names its own key.

2. The `;C<key>` token really does re-enter. Drain at WCB.ino:7501-7519: `while (xQueueReceive(commandQueue, &inItem, 0) == pdTRUE) { ... handleSingleCommand(...) }`. `handleSingleCommand` (:4381-4383) → `processCommandCharcter` → `startsWith("c"/"C")` → `recallStoredCommand` (:5853-5854). So the enqueued body's recall token calls back into the recall inside the same drain iteration.

3. The 200-slot queue is NOT an escape hatch (I checked, because it was the obvious refutation). `commandQueue = xQueueCreate(200, ...)` (WCB.ino:7191), and a full queue discards (:1937-1941). But the drain is FIFO and pops *everything*: for body `;S1A^;Cwave`, each cycle pops 2 items (S1A, Cwave) and pushes 2 — steady state, queue size never exceeds the body's token count, the discard path is never reached, and `xQueueReceive` never returns pdFALSE. loop() genuinely never returns.

4. Nothing resets the board. The loop-task WDT reset is commented out (WCB.ino:6732) and `bootGuardDisarm()` runs at the end of setup (WCB.ino:7473, defined :6802-6807) precisely so a running board is never reset by it. The drain blocks on Serial/NVS so it yields to other tasks — no idle-starvation panic either. Everything after the drain point in loop() — `processETMHeartbeats`, `wdpTick`, `processETMAcksAndRetries`, `drainOtaPackets`, `checkOtaTimeout`, `processCommandGroups` (WCB.ino:7477-7499) — stops permanently. The board silently leaves the mesh.

SCOPE CORRECTION the reviewer missed (this is why I cut severity). The hang only occurs for a body containing NO `;T`/`;t` token. `recallCommandSlot` routes a timer-bearing body to `parseCommandGroups` instead (WCB_Storage.cpp:614-618, `isTimerCommand` at command_timer.cpp:48-51), and timer groups fire one per loop() pass from `processCommandGroups` (command_timer.cpp:182-221) — non-blocking, loop() returns normally. Every recursive pattern the wiki teaches carries a timer: Stored-Commands.md:681-712 ("Recursive Commands (Use Carefully)", "Progressive Sequences", "State Machines") all use `;T1000,;SEQloop`. So the documented recursion is safe; only the timer-less variant hangs. That cuts both ways: the wiki actively encourages recursion, so dropping the `;T` is one slip away, and the wiki's stated escape (`?STOP`) does not cover it — `checkForTimerStopRequest` (command_timer.cpp:61-63) is handled synchronously in the serial task at WCB.ino:6429-6432 and calls `stopTimerSequence()` (command_timer.cpp:53-59), which only clears timer state and never touches commandQueue.

REACHABILITY. Callers of `recallCommandSlot`: WCB.ino:6125, plus MP3/DFP ONFIN and ONERR callbacks (WCB_MP3.cpp:50-51, WCB_DFP.cpp:47-48) — no boot autoexec, so nothing self-triggers this at power-on; it re-arms only when someone triggers the key again. A remote peer cannot induce it without the recursive sequence already being stored on that board.

INTENT. The comment block at WCB.ino:6074-6084 blesses nesting and explains only the fan-out suppression; it says nothing about bounding depth. `?SEQ?` help (WCB_Help.cpp:784-785) likewise advertises "Sequences can still call other sequences with ;Ckey" with no warning. Nothing in docs/ or the source records this as a deliberate trade-off, so it is not a REFUTED-by-design.

CORRECTIONS to the reviewer's write-up: (a) `?Cwave,<body>` is not a save form — `processLocalCommand` would see root "Cwave", match no handler, and print "Unknown command"; the real forms are `?SEQ,SAVE,wave,<body>` (WCB.ino:4993) or legacy `?CSwave,<body>` (WCB.ino:5408 `storeCommand` → `substring(2)`). (b) "until it is power-cycled" is overstated: `serialCommandTask` is an independent task (WCB.ino:6595-6627), so a typed `?SEQ,CLEAR,<key>` is enqueued and executed *by the spinning drain*; `eraseStoredCommandByName` removes the NVS key (WCB_Storage.cpp:676) so the next recall hits the empty-value early return (WCB_Storage.cpp:561-564) and the drain exits. `?REBOOT` works the same way. Recovery is possible over USB — through a console being flooded with two Serial lines per iteration.

Severity: real, unbounded, no watchdog, whole-board mesh outage — but it needs operator-authored timer-less self/mutual recursion, the documented recursive form is safe, and it is serial-recoverable. S3, not S2. (S2 is defensible if you weight "board silently drops off the mesh with no diagnostic" heavily; S1 in this review's calibration is memory-corruption class, which this is not.)

</details>

### `Code/WCB/WCB.ino:6191` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**;Px,width as documented cannot work, and its rejection is silent because debugPWMEnabled is never assigned anywhere**

**Correction to the finding as filed:**

The observation is right; the direction of the fix is wrong. The parser is canonical, not the help text.

`;Pxnnnn` is the form the source comment declares (WCB.ino:6188), the form the firmware itself puts on the wire (WCB_PWM.cpp:724, `";P%d%lu"`), and the form the wiki documents (Wireless_Communication_Board-WCB.wiki/PWM-Passthrough.md:368, `;P31500`). The single outlier is WCB_Help.cpp:925.

Correct fix, two one-liners:
1. WCB_Help.cpp:925 — change to match the code and the wiki, e.g. `Serial.println(F("  ;Px<width>    Output PWM pulse on port x (e.g. ;P41500)"));`
2. WCB.ino:6194 — gate the rejection on the flag a user can actually set, `if (debugPWMPassthrough) Serial.println("Invalid PWM output command");`, and delete the now-unreferenced `bool debugPWMEnabled = false;` at WCB.ino:177. (Making the message unconditional is also defensible and matches processSerialMessage's unconditional bad-port report at WCB.ino:5886 — but note `;P` is also the high-rate passthrough wire command, so an unconditional print on a malformed remote frame could spam the console; the debugPWMPassthrough gate is the safer choice.)

Also worth folding in while touching this topic: WCB_Help.cpp:727's `IF,mode>2,AND,armed=1^;PP100` example is likewise unparseable (`substring(1,2)` = `"P"` -> port 0 -> rejected).

<details><summary>Full verification reasoning</summary>

MECHANISM — every load-bearing fact re-derived from source.

1. The parser. `Code/WCB/WCB.ino:6187-6215`, read in full. Dispatch is `WCB.ino:5857-5858` (`message.startsWith("p")||("P") -> processPWMOutput(message)`), reached from `processCommandCharcter(cmd.substring(1), sourceID)` at `WCB.ino:4383` — so the leading CommandCharacter is already stripped and `message` is literally `P4,1500`. Then `message.substring(1,2)` = `"4"` -> port 4, and `message.substring(2)` = `",1500"`.

2. `String::toInt()` on `",1500"`. Checked the pinned core, not memory: `C:\Users\ghulette\AppData\Local\Arduino15\packages\esp32\hardware\esp32\3.3.4\cores\esp32\WString.cpp:904-909` — `return atol(buffer())`. `atol(",1500")` stops at the first non-space/non-sign/non-digit char and returns 0. So `pulseWidth == 0`, the guard at `WCB.ino:6193` (`pulseWidth < 500`) fires, and `WCB.ino:6195` returns. The reviewer's parse claim is exactly right.

3. No upstream comma splitting rescues it. The chain delimiter is `^`, not `,` — `WCB.ino:150-151` (`char commandDelimiter = '^';`), split at `WCB.ino:2097`. So `;P4,1500` arrives intact.

4. The dead flag. `grep -rn debugPWMEnabled Code/` returns exactly two hits: the declaration `WCB.ino:177` (`bool debugPWMEnabled = false;`) and the read `WCB.ino:6194`. No assignment anywhere, no `extern` in any header. `?DEBUG,PWM,ON` sets a *different* variable — `WCB.ino:4436-4438` sets `debugPWMPassthrough = true` and prints "PWM debugging enabled"; legacy `?DPWMON` at `WCB.ino:5167-5168` does the same. `debugPWMPassthrough` is consumed only in `WCB_PWM.cpp:696,718,726`. So line 6194 is unreachable and the rejection is fully silent. `git log -L 6187,6215:Code/WCB/WCB.ino` shows commit 56761a1 renamed the guard from `debugEnabled` to `debugPWMEnabled` on that line without ever wiring the new flag — a rename regression, not a deliberate choice.

5. The help line. `WCB_Help.cpp:925` — `Serial.println(F("  ;Px,width     Output PWM pulse on port x"));` — verified verbatim, inside the `?CMDCHAR` topic. It is the ONLY place in the tree that spells `;P` with a comma.

INTENT / WHICH SIDE IS CANONICAL — this is where the reviewer got it backwards.
- The source comment at `WCB.ino:6188` states the grammar: `// Format: ;Pxnnnn where x=port, nnnn=pulse width in microseconds`.
- The firmware's own emitter uses the comma-less form: `Code/WCB/WCB_PWM.cpp:724` — `snprintf(msg, sizeof(msg), ";P%d%lu", targetPort, pulseWidth);` then `sendESPNowMessage(targetWCB, msg, false)`. This is the PWM-passthrough remote-output path and is the only automatic producer of `;P` in the ecosystem.
- The user-facing wiki agrees with the code: `Wireless_Communication_Board-WCB.wiki/PWM-Passthrough.md:368` — `;P3[pulsewidth]  // Example: ;P31500`, and `Message format: ;Px[value]`.
- `grep` of `Wizard/*.js`, `Wizard/index.html` finds no `;P` emitter at all; `WCB_Help.h`'s block reference does not mention `;P`.

So the code, its comment, the wiki, and the wire producer all agree on `;Pxnnnn`. One help string is the outlier. Per CLAUDE.md ("the code is the source of truth... if a doc and the code disagree, the code is right and the doc is a bug"), the defect is the help line, not the parser.

REACHABILITY — real but narrow. A user who reads `?CMDCHAR?` and types `;P4,1500` gets nothing, with no output in any debug configuration; that is genuinely reachable and genuinely silent, so the finding stands as a defect. But nothing automatic is broken: the passthrough path (`WCB_PWM.cpp:724`) emits the form the parser accepts, and `;P41500` typed by hand works.

SEVERITY — S2 is too high. The claimed failure scenario ("wires a servo/ESC to S4... nothing happens on the pin") overstates twice over: even with the correct spelling, `processPWMOutput` fires ONE blocking 500-2500 us pulse (`WCB.ino:6208-6212`) and returns — a servo/ESC needs a repeating ~50 Hz train, which is precisely what the passthrough streamer provides and what manual `;P` does not. The real damage is (a) one wrong line of built-in help that silently no-ops, and (b) a dead diagnostic flag. That is a docs-and-diagnostics defect, not a broken feature: S3.

</details>

### `Code/WCB/WCB.ino:6603` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**serialCommandTask hard-codes the Kyber/Maestro port skip to S1/S2 while the Kyber and Maestro ports are configurable S1-S5, so both tasks drain the same UART**

**Correction to the finding as filed:**

The observation stands; three corrections.

(a) Trigger is `kyberLocalPort ∈ {3,4,5}` only — S1/S2 are accidentally covered, and S2 is the default (Wizard/index.html:631, WCB_Storage.cpp:1458), which is why it has never shown on the bench.

(b) Same root cause, second symptom the finding omits: whenever the Kyber is NOT on S1/S2, `serialCommandTask` still skips both, and nothing else reads them unless a local Maestro slot occupies them (WCB.ino:4310-4313) — so S1/S2 are opened at boot (:7224-7226) and then silently never parsed.

(c) Practical exposure is capped by an independent blocker: `?KYBER,LOCAL,Sx` forces the port to 115200 (WCB_Storage.cpp:1163) and S3-S5 are `SoftwareSerial` (WCB.ino:885-887), a combination the firmware explicitly blocks for Maestro configs as "TOO HIGH for software serial" (WCB_Maestro.cpp:643-655). Fixing the race does not make Kyber-on-S3 work; the port/baud policy and the S1/S2 reservations in WCB_WLED.cpp:328, WCB_HCR.cpp:651, WCB_MP3.cpp:316, WCB_DFP.cpp:270, WCB_PWM.cpp:57/63 and Wizard/parser.js:1096-1097 all still assume S1/S2. Either finish the generalisation everywhere, or reject `?KYBER,LOCAL` on S3-S5 outright (cheaper, and consistent with the existing Maestro software-serial block) — but do not leave the byte-stealing in place.

Corrected fix (see fix_is_correct): put the guard inside `processIncomingSerial` beside the existing MP3/DFP/HCR/query early-returns at WCB.ino:6364-6378, so no future caller can bypass it:

    if (sourceID != 0 && Kyber_Local && sourceID == kyberLocalPort) return;   // KyberLocalTask owns this port
    if (sourceID != 0 && (Kyber_Local || Maestro_Remote) && isLocalMaestroPort(sourceID)) return;  // forwardMaestroDataTo*Kyber owns it

with `isLocalMaestroPort(p)` = any `maestroConfigs[i].configured && maestroConfigs[i].serialPort == p` (remote slots already force `serialPort = 0`, WCB_Maestro.cpp:690, so no `remoteWCB` test is needed), and keep the existing unconditional S1 skip for `Maestro_Remote` when NO local Maestro slot exists — see the fix-check note. Leave lines 6603-6605 and the mode branch at 6608-6623 as a plain `for (p = 1..5) if (!isSerialPortRawMapped(p)) processIncomingSerial(getSerialStream(p), p);` once the ownership tests live in the callee.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived from source, every fact checked.

1. The code is exactly as quoted. `serialCommandTask` (WCB.ino:6595-6627) calls `processIncomingSerial(Serial3,3)/(Serial4,4)/(Serial5,5)` unconditionally except for the raw-map test (`:6603-6605`), then branches on mode: `if (Kyber_Local) { /* skip S1,S2 */ } else if (Maestro_Remote) { S2 only } else { S1,S2 }` (`:6608-6623`). The port numbers are literals; `kyberLocalPort` is never consulted.

2. The Kyber port really is runtime-configurable 1-5. `kyberLocalPort` is declared at WCB_Storage.h:51 / WCB.ino:162, set from `?KYBER,LOCAL,Sx` at WCB_Storage.cpp:1159, validated at :1112-1114 (`if (baseCommand.equals("local") && kyberUseTargeting && (kyberPort < 1 || kyberPort > 5))` → "Invalid Kyber port. Must be S1-S5"), persisted as `K_Port` (:1435) and reloaded at :1458 (`(storedPort > 0 && storedPort <= 5) ? storedPort : 2`).

3. The Kyber reader keys off that variable: `forwardDataFromKyber()` opens `getSerialStream(kyberLocalPort)` and drains it (`WCB.ino:4245`, `:4254`), driven by `KyberLocalTask` every 1 ms (`:6577-6586`), created only when `Kyber_Local` (`:7442-7445`). `serialCommandTask` is created unconditionally (`:7440`), same core 1, same priority 1, 5 ms period.

4. No guard exists anywhere else on that path. I read `processIncomingSerial` in full (WCB.ino:6361-6420): its early returns cover MP3 (`:6365`), DFP (`:6369`), HCR (`:6373`) and `maestroQueryPort` (`:6378`, plus a re-test mid-drain at `:6383`) — there is no Kyber-port test and no local-Maestro-port test. `isSerialPortRawMapped()` (WCB_Storage.cpp:1807-1816) only reports serial-monitor mappings; `?KYBER,LOCAL` creates none. `blockBroadcastFrom[kyberPort-1] = true` (WCB_Storage.cpp:1174-1178) only suppresses re-broadcast *after* the bytes have already been read and parsed (the check is at WCB.ino:6287), so it does not stop the theft.

So with `?KYBER,LOCAL,S3` both tasks drain Serial3 concurrently. That is genuinely the bug the sibling code was written to avoid: `RawSerialForwardingTask` carries the exact guard `if (Kyber_Local && kyberLocalPort > 0 && inputPort == kyberLocalPort) continue;` with the comment "KyberLocalTask owns it. Reading here would steal bytes from the Kyber forwarding path and double-send them" (WCB.ino:6650-6655), and WCB_Maestro.cpp:8-10 names `processIncomingSerial` as one of "the three background readers that drain Maestro RX" that must skip a busy port. So the codebase already recognises this class of race and simply never updated `serialCommandTask`. Not deliberate, not documented — a half-finished generalisation.

REACHABILITY — real but narrower than claimed.
- Reachable from the primary config path: the Wizard's Kyber port `<select>` offers Serial 1-5 (Wizard/index.html:628-635) and `updateKyberPortDropdown` (app.js:979-998) filters only on port claims, not on port number. `?KYBER,LOCAL,S3` over USB works too.
- BUT the trigger is narrow: the hard-coded skip is accidentally correct for `kyberLocalPort` 1 or 2, and 2 is both the shipped default (Wizard `<option value="2" selected>`, firmware NVS fallback at WCB_Storage.cpp:1458) and every bench/doc setup. Only S3/S4/S5 double-drain.
- And that configuration is independently marginal: S3-S5 are `SoftwareSerial` (WCB.ino:885-887, EspSoftwareSerial 8.1.0 per :76), and `?KYBER,LOCAL,Sx` force-sets the port to 115200 (WCB_Storage.cpp:1163) — a baud the firmware itself refuses for the analogous case, "S%d is SOFTWARE SERIAL / Baud rate (%d) is TOO HIGH for software serial / CONFIGURATION BLOCKED" (WCB_Maestro.cpp:643-655). So a Kyber on S3 has a second, independent reason to be broken.
- The whole rest of the firmware still hard-codes the same S1/S2 invariant — WLED (WCB_WLED.cpp:328-329), HCR (:651-652), MP3 (:316-317), DFP (:270-271), PWM (WCB_PWM.cpp:57,63), the broadcast loop (WCB.ino:6295-6296), setup's UART init (:7223-7228) — and Wizard/parser.js:1091-1097 deliberately mirrors it ("mirror the firmware's device-port guards"). Kyber-off-S2 is therefore an under-supported configuration everywhere, not just here.

The finding's second scenario (Maestro on S2 under `Maestro_Remote`) is real but much weaker: a Maestro transmits only in reply to a query, and both readers already skip `maestroQueryPort` (WCB.ino:6378, :4335), so the exposure is limited to a Pololu get issued by the *remote* Kyber over the mesh.

WHAT THE FINDING MISSES: the same hard-coding has a second symptom on the same trigger — with `Kyber_Local` and `kyberLocalPort == 3`, S1 and S2 are skipped by `serialCommandTask` and are not read by anything else unless a local Maestro slot sits on them (`forwardMaestroDataToLocalKyber`, WCB.ino:4307-4326). setup() still opens both (`:7224-7226`), so a plain serial device on S1/S2 is silently never parsed.

SEVERITY: by this review's own scale (S2 = wrong behaviour in a real scenario, S3 = latent race), this is a genuine race gated behind a non-default port choice that is itself unsupported by five other subsystems and by a forced 115200 on a software UART. Real, worth fixing, not release-blocking → S3.

</details>

### `Code/WCB/WCB.ino:6608` — CONFIRMED · S2 → **S3**

**Kyber_Local / Maestro_Remote are settable at runtime but KyberLocalTask / KyberRemoteTask are only created at boot, leaving S1 and S2 with no reader at all**

**Correction to the finding as filed:**

Real, but narrower and less severe than written, and incomplete.

Corrected statement: `Kyber_Local` and `Maestro_Remote` are settable at runtime (WCB_Storage.cpp:1156, :1179, :1186) while `KyberLocalTask` / `KyberRemoteTask` are created only at boot (WCB.ino:7442-7449, the only creation sites in the sketch). A hand-typed `?KYBER,LOCAL,Sx` therefore makes `serialCommandTask` stop servicing S1/S2 (WCB.ino:6608) with no Kyber task in existence, so the local Kyber bridge silently does nothing until a reboot — while `printKyberSettings()` prints "Initialized Serial1 & Serial2 for Kyber Local mode" (WCB_Storage.cpp:1481-1482), a confirmation that is false on this path. The repo's own bench fixture hits it (TestConfigs/WCB1_02_full.txt:31, :52).

Severity S3, not S2: the Wizard already forces `?reboot` after any `KYBER,` command (Wizard/app.js:8191 → :7504 direct, :7846 relay-embedded), the firmware's own copy-paste generators end in `^?REBOOT` (WCB_Storage.cpp:1425, :2202), NVS is written correctly and the next boot is right (:1433-1435, :1449-1467), USB/S3-S5/mesh stay fully alive (WCB.ino:6598, :6603-6605), and recovery is one reboot with no data loss.

Drop the "Same for `?MAESTRO,REMOTE` (S1 goes dark)" sentence: the mesh→Maestro direction works live off the flag alone, including a Serial1 fallback (WCB.ino:4101-4113); only the reply direction is dead, and the WDP auto-path deliberately documents exactly this ("reboot to fully apply", WCB_WDP.cpp:466-467, :481).

Add to the fix, in the same pass:
1. Clear `kyberLocalPort = 0` in the remote branch (WCB_Storage.cpp:1178-1182), matching the clear branch (:1188) and `loadKyberSettings` (:1462) — otherwise a live local→remote switch leaves the still-running `KyberLocalTask` and `serialCommandTask`'s `processIncomingSerial(Serial2, 2)` (WCB.ino:6612-6613) fighting over the same UART.
2. Guard `forwardMaestroDataToRemoteKyber` (WCB.ino:4330) on `Maestro_Remote` — it has no flag check, so a board that booted remote keeps broadcasting Maestro bytes after `?KYBER,CLEAR`.
3. Add `MAESTRO,REMOTE` to `commandStringNeedsReboot` (Wizard/app.js:8185-8196) — parser.js:1353 emits that spelling for remote mode and only `KYBER,` is matched today (:8191).

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived, every load-bearing line opened:

1. `serialCommandTask` gates on the LIVE globals. `Code/WCB/WCB.ino:6607-6623`: `// Handle Serial1 and Serial2 based on mode` / `:6608 if (Kyber_Local) {` / `:6609 // Skip Serial1 and Serial2 - handled by Kyber task` / `:6610 } else if (Maestro_Remote) {` (Serial2 only) / `:6615 } else {` (both). Loop period `vTaskDelay(pdMS_TO_TICKS(5))` at `:6625`. Reviewer's quote and line numbers are exact.

2. The compensating tasks are created ONCE, in setup(), from the boot-time flags. `WCB.ino:7442-7449`: `if (Kyber_Local) { xTaskCreatePinnedToCore(KyberLocalTask, "Kyber Local Task", 4096, NULL, 1, NULL, 1); Serial.println("Kyber_Local Task Created"); }` and `if (Maestro_Remote) { xTaskCreatePinnedToCore(KyberRemoteTask, …); }`. I re-ran the grep myself: `grep -rn "xTaskCreatePinnedToCore" Code/WCB/` returns exactly 6 hits — :5082 (identifyTask), :7439 (serialCommandTask), :7443, :7447, :7459 (PWMTask), :7464 (RawSerialForwardingTask). There is no other creation site for either Kyber task and no `vTaskDelete`/resume path anywhere. Confirmed.

3. `storeKyberSettings()` flips both flags live, persists to NVS, and never reboots. `Code/WCB/WCB_Storage.cpp:1156-1157` (`Kyber_Local = true; Maestro_Remote = false;` + `:1159 kyberLocalPort = kyberPort;`), `:1179-1181` (remote), `:1186-1188` (clear). `grep -niE "reboot|restart" Code/WCB/WCB_Storage.cpp` shows the only `ESP.restart()` in the file is in `eraseNVSFlash()` at `:1032`. Confirmed.

4. Reachable from a plain typed command: `WCB.ino:4651-4656` (`?KYBER,LOCAL/REMOTE/CLEAR` → `storeKyberSettings(args); printKyberSettings();`), `:4905-4909` (`?MAESTRO,REMOTE`), `:5228-5240` (legacy `?kyber_local` / `?kyber_remote` / `?kyber_clear`). No reboot on any of them.

5. The consequence is real. With `Kyber_Local` flipped true live: `serialCommandTask` stops calling `processIncomingSerial` on S1/S2 from the next 5 ms tick, and `KyberLocalTask` does not exist, so `forwardMaestroDataToLocalKyber()` (`WCB.ino:4307`) and `forwardDataFromKyber()` (`:4244`) never run — the whole local Kyber bridge is dead. Meanwhile `printKyberSettings()` prints, at `WCB_Storage.cpp:1481-1482`, `"Initialized Serial1 & Serial2 for Kyber Local mode"` — which is false on this path (setup() at `WCB.ino:7223-7226` is what actually begins those UARTs, and it already ran). So the board actively confirms the opposite of what happened.

6. The repo's own bench fixture walks straight into it. `TestConfigs/WCB1_02_full.txt:31` documents `?KYBER,LOCAL`, and the paste chain at `:52` is `…^?KYBER,CLEAR^?MP3,CLEAR^?KYBER,LOCAL^?MP3,S5:9600:V0^…^?config` with no `?REBOOT`. (`?KYBER,LOCAL` with no comma defaults `kyberPort = 2` — `WCB_Storage.cpp:1078-1082` — so `kyberLocalPort` becomes 2.) `grep -ni reboot TestConfigs/` finds reboots only in unrelated steps (`TestPlan.md:105`, `:222`).

WHY S3, NOT S2 — the mitigations the reviewer did not check:

- The dominant configuration path already reboots. `Wizard/app.js:8185-8196 commandStringNeedsReboot()` → `:8191 if (u.includes('KYBER,')) return true;   // Kyber mode — serial port reservation`. `boardGo` consumes it at `:7496-7504` (sends `?reboot`) and the relay path embeds `^?reboot` in the same MGMT session at `:7843-7847`. `Wizard/parser.js:1347` emits `KYBER,LOCAL,S…` and `:1355` emits `KYBER,CLEAR`, both of which match. So a Wizard-driven local/clear change never leaves the board in the half-state.
- The firmware's own copy-paste generators end in `^?REBOOT` — `WCB_Storage.cpp:1425` (peer chains printed by `storeKyberSettings` itself) and `:2202` (`printKyberList`). The author clearly treats reboot as part of applying Kyber config.
- Nothing is corrupted or mis-persisted; NVS gets the right value (`:1433-1435` writes `K_Location` + `K_Port`) and `loadKyberSettings()` (`:1449-1467`) reconstructs the correct state on the next boot. Recovery is one reboot.
- The board is not bricked or unreachable: `serialCommandTask` still services USB `Serial` (`:6598`) and S3–S5 (`:6603-6605`), and the whole ESP-NOW/mesh path is untouched.
- Under `Kyber_Local` the end-state of S1/S2 being closed to generic commands is *intended*, so the only genuine loss is the bridge not starting — not "S1 and S2 died" in the sense of losing something the user still wanted.

That profile — a normal-operation config command silently not taking effect, recoverable by a reboot, already handled correctly by the GUI, no data loss — is S3 on this project's scale (compare docs/CODE_REVIEW_2026-08_verdicts.md:554 and :614). It sits at the top of S3 because the board prints a confirmation that is actively wrong.

WHAT THE REVIEWER GOT WRONG:

(a) "leaving S1 and S2 with no reader at all" is imprecise. Only `serialCommandTask` hardcodes S1/S2. The Kyber tasks read `kyberLocalPort` (`WCB.ino:4245`, `:4254`) and `maestroConfigs[i].serialPort` (`:4312-4315`, `:4335-4338`), which are S1–S5. The claim is exact only for the legacy layout (kyberLocalPort==2, Maestro on S1). The port-mismatch itself is the *adjacent* finding at docs/CODE_REVIEW_2026-08_wave1.md:504-524, not this one.

(b) "Same for `?MAESTRO,REMOTE` (S1 goes dark)" materially overstates it. The primary direction of Maestro_Remote — mesh → local Maestro — is handled in the ESP-NOW receive path off the live flag, not by any task: `WCB.ino:4101-4113`, including a fallback `if (!wroteAny) Serial1.write(dataPtr, chunkLen);` at `:4113`. So a live flip to remote still drives the Maestro; only the reply direction (`forwardMaestroDataToRemoteKyber`, `:4330`) stays dead. And for the WDP auto-path this is a DELIBERATE, DOCUMENTED trade-off: `Code/WCB/WCB_WDP.cpp:466-467` — "storeKyberSettings persists the flag live, but the receive relay task only spawns at boot, so we note 'reboot to apply.'" — with `:481` printing `"[WDP] %s — enabling Maestro remote (reboot to fully apply)"`. That sub-case is REFUTED as a defect.

(c) "no reboot and no warning" is true only of the hand-typed CLI, not of the tooling (see mitigations above).

TWO THINGS IN THE SAME DEFECT THE REVIEWER MISSED (worth folding into the fix):

(d) The Wizard's remote path is NOT covered. `Wizard/parser.js:1352-1353` emits `MAESTRO,REMOTE` (the canonical name; matching firmware `WCB.ino:2857`), and `commandStringNeedsReboot` (`app.js:8185-8196`) has no `MAESTRO,REMOTE` test — so a diff push that changes only the kyber mode to remote gets no reboot. Add `if (u.includes('MAESTRO,REMOTE')) return true;`.

(e) The reverse transitions are broken too, and one of them is worse than the reported direction. `WCB_Storage.cpp:1178-1182` (the remote branch) never clears `kyberLocalPort` — unlike the clear branch (`:1188`) and `loadKyberSettings` (`:1462`). So a board that booted `Kyber_Local` and is switched live with `?KYBER,REMOTE` keeps the already-running `KyberLocalTask` draining `kyberLocalPort` every 1 ms while `serialCommandTask` now takes the `Maestro_Remote` branch and starts calling `processIncomingSerial(Serial2, 2)` (`WCB.ino:6612-6613`) — two tasks racing for the same bytes when kyberLocalPort==2. Separately, `forwardMaestroDataToRemoteKyber` (`WCB.ino:4330-4348`) has NO `Maestro_Remote` guard at all, so a board that booted remote keeps draining every local Maestro port and broadcasting to the mesh even after `?KYBER,CLEAR`.

</details>

### `Code/WCB/WCB.ino:7225` — CONFIRMED · S2 → **S3**

**Serial1/Serial2 begin() has no baudRates>0 guard (Serial3-5 does) — a 0 in NVS blocks 20 s in the core's baud auto-detect and the boot guard reboots the board forever**

**Correction to the finding as filed:**

Two corrections.

1. The repo's existing verdict for this finding (docs/CODE_REVIEW_2026-08.md:228-231) says a 0 baud is "**not reachable**". That is wrong and should be amended: the shipped firmware from 483b4d8 (2025-07-09) through f5d758f (2025-11-23) explicitly whitelisted `baud == 0` (`git show 483b4d8:Code/WCB/WCB_Storage.cpp:84`), and `String::toInt()` turns any malformed `?BAUD` argument into 0. NVS survives a reflash, so a board configured in that window can carry a 0 into this release. The correct framing is "reachable only via legacy NVS state", not "unreachable". The S3 grade stands either way.

2. Take the *second* of the two proposed fixes, not the first. Guarding the `begin()` calls alone stops the boot loop but leaves S1/S2 unrecoverable without a reboot: `applyLiveBaud()` (WCB.ino:1886-1889) handles ports 1-2 with only `SerialN.updateBaudRate(baud)`, which is `uartSetBaudRate(_uart, baud)` (HardwareSerial.cpp:477-479), and that returns false immediately on `_uart == NULL` (esp32-hal-uart.c:1188-1191). So after a skipped `begin()`, a user typing `?BAUDS1,9600` would fix NVS and `baudRates[0]` but the UART would stay closed until the next boot — unlike S3-5, where applyLiveBaud does a real `end()`/`begin()` (WCB.ino:1892-1893). Sanitising in `loadBaudRatesFromPreferences()` (WCB_Storage.cpp:247-254) avoids that entirely, and 9600 is the right fallback for every port (SERIAL1..5_DEFAULT_BAUD_RATE are all 9600, WCB.ino:622-626). If you factor the whitelist out of `updateBaudRate()` into a shared `isValidBaud()` so load and store cannot drift, note the current list omits 4800/230400/921600 — keep the two sides literally the same function rather than duplicating the constants.

3. Minor: the finding says the same unguarded call appears "again at :7229, :7232, :7239, :7244" — there are six Serial1/Serial2 begin sites in total (:7225, :7226, :7229, :7232, :7239, :7244), so any guard has to be applied at all six, or better, once on load.

<details><summary>Full verification reasoning</summary>

I opened the real code and re-derived every number rather than trusting the transcription.

**The asymmetry is exactly as described.** `Code/WCB/WCB.ino:7223-7247` — all five `Serial1/Serial2.begin(baudRates[0|1], SERIAL_8N1, ...)` call sites (:7225, :7226, :7229, :7232, :7239, :7244 — six, not five) are gated only on Kyber_Local/Maestro_Remote and `isSerialPortUsedForPWMInput/isSerialPortPWMOutput`. Thirteen lines later `WCB.ino:7250-7278` guards S3/S4/S5 with `if (baudRates[2|3|4] > 0)` and an explicit `"SerialN has invalid baud rate (0) - skipping UART init"`. Verbatim, correct line numbers.

**Mechanism, verified against the pinned core (esp32:esp32@3.3.4, `Arduino15/packages/esp32/hardware/esp32/3.3.4`):**
- `HardwareSerial.cpp:428-448`: `if (!baud) { uartStartDetectBaudrate(...); while (millis() - startMillis < timeout_ms && !(detectedBaudRate = uartDetectBaudrate(_uart))) { yield(); } ... else { _uart = NULL; } }`. Default `timeout_ms = 20000UL` (`HardwareSerial.h:314`), and none of the six WCB call sites passes it. So `begin(0,…)` blocks up to 20 s and then nulls `_uart`.
- On ESP32-S3 it is guaranteed to be the full 20 s: `esp32-hal-uart.c:1531-1578` compiles the detect body only `#if CONFIG_IDF_TARGET_ESP32 || CONFIG_IDF_TARGET_ESP32S2`; the `#else` at :1575-1577 is `log_e(...); return 0;` — so the loop spins the whole timeout (and log-spams). On classic ESP32 detection only succeeds if data is present on RX; an idle Maestro/Kyber TX line means the same 20 s.
- Boot guard is real and the arithmetic holds: `WCB.ino:6787 #define BOOT_GUARD_TIMEOUT_MS 10000`, `bootGuardArm()` is the *first* statement of `setup()` (`:7107-7109`), `_bootGuardFired` = bare `ESP.restart()` (`:6789`) dispatched `ESP_TIMER_TASK`, disarmed only at `:7473`. The design comment at `:6779-6782` explicitly states the callback runs in the esp_timer task so "a hung setup() can't stop it from firing" — i.e. the `yield()` in the detect loop will not save it.
- Timing to :7225 is well under 10 s: `delay(1000)` at :7121, the NVS load block :7137-7166, `initStatusLEDWithRetry(5, 100)` worst case ~500 ms (:7172), `delay(1000)` at :7184, then the baud loads. ~2.5-3.5 s. Guard fires ~6.5 s into a 20 s stall → `ESP.restart()` → identical boot → permanent loop. No serial window (the drain loop never starts), no WiFi/ESP-NOW (initialised after :7280), so no OTA. Recovery is an NVS/flash erase. The reviewer's chain is sound.

**Reachability — this is where the finding is stronger than the repo's own prior verdict.** `docs/CODE_REVIEW_2026-08.md:228-231` already adjudicated this as F-322, downgraded S2→S3 on the grounds that "a 0 baud is **not reachable**". That reasoning is correct for *today's* firmware but incomplete:
- Current writers: `updateBaudRate()` whitelists 13 values, 0 not among them (`WCB_Storage.cpp:156-161`), and is the only path that reaches `preferences.putInt` on `serial_baud` (`:175-178`). Every caller I found routes through it (WCB.ino:4565, :5341, :5662, :5689; WCB_DFP.cpp:138/296; WCB_HCR.cpp:470/495; WCB_Maestro.cpp:660/794/915; WCB_MP3.cpp:176/342; WCB_WLED.cpp:165/192; WCB_Storage.cpp:1163/1192/1296). `saveBaudRatesToPreferences()` (`WCB_Storage.cpp:259`) is dead — no declaration in `WCB_Storage.h`, no callers. `eraseNVSFlash()` only `clear()`s the namespace (`:941-943`), which restores the compiled 9600 default via `getInt(key, baudRates[i])` (`:251`). All three of those checks hold.
- **But the NVS blob outlives the firmware.** `git log -S"baud == 0 ||" -- Code/WCB/WCB_Storage.cpp` shows the whitelist was introduced in `483b4d8` (2025-07-09) *including* `baud == 0` — `git show 483b4d8:Code/WCB/WCB_Storage.cpp:84` reads `if (!(baud == 0 || baud == 110 || …`. It was removed in `f5d758f` (2025-11-23) — **the same commit that added the `baudRates[i] > 0` guard for S3-5**. That is a strong intent signal, not a coincidence: under that firmware a stored 0 on S3-5 hit `m_bitTicks = (microsToTicks(1000000UL) + baud / 2) / baud;` (`Arduino-Code/libraries/EspSoftwareSerial/src/SoftwareSerial.cpp:91` — line number exact) = IntegerDivideByZero panic. The author fixed the write path and guarded the SW-serial read path, and simply did not carry the guard to S1/S2.
- Getting a 0 stored under that firmware did not require the user to intend it: every `?BAUD` parse uses `String::toInt()` (`WCB.ino:4564`, `:5339`, `:5673`, `:5686`), which yields 0 for any non-numeric or truncated argument, and the old whitelist accepted it.
- I checked and closed two adjacent theories: old `disableMaestroSerialBaudRate()` restoring `storedBaudRate[1]` cannot produce 0 (`git show 483b4d8:Code/WCB/WCB.ino:121-127` initialises index 1 to `SERIAL1_DEFAULT_BAUD_RATE`, index 0 is the dummy), and `setBaudRateForSerial()`'s `storedBaudRate[ser] = preferences.putInt(...)` bug (putInt returns byte count) had no callers. Neither survives into the current tree.

So: mechanism fully confirmed, reachability real but confined to a board configured during the 2025-07-09 → 2025-11-23 window whose S1/S2 baud has not been set since. Under that firmware the same state produced a visible 20 s boot stall plus a dead port, which most owners would have corrected; the boot guard (added `3086750`, 2026-06-09) is what turns the survivors from "slow boot" into "brick". Real, catastrophic-if-hit, low-probability, legacy-gated — that is the review's own S3 definition ("latent"), not S2 ("wrong behaviour in a real scenario"). I land on the same S3 as the prior verdict, but for the opposite reason: it is *not* unreachable, it is reachable only through legacy NVS.

</details>

### `Wizard/app.js:454` — CONFIRMED · S1 → **S3** · ⚠ PROPOSED FIX IS WRONG

**reconcileBoardGrid can delete a board slot that is mid-flash, killing the post-flash config restore**

**Correction to the finding as filed:**

Two corrections to the finding, both about the size of the exposed window rather than the mechanism.

(a) The window is LONGER than "the 30-60 s flash", and the more dangerous part of it is not covered by `_boardFlashing` at all. `delete _boardFlashing[n]` runs at app.js:7109, *before* the 1.5 s settle + up to 6x300 ms `port.close()` retries (:7118-7120) and `conn.reconnect(_isWindows ? 14 : 12, 2000)` (:7124) — up to ~30 s on Windows during which `isConnected()` is still false and the flash flag is already cleared. An eviction there is strictly worse, because it lands immediately before the `boardGo(n,{mode:'configure'})` at :7189.

(b) The same eviction exposure exists on four other close/reconnect paths that never set `_boardFlashing` at all, with the same "config never pushed" consequence: the Factory/Erase path (`closeForReconnect` :7315 → `reconnect(10,1500)`, restore+`boardGo` at :7345/:7360), the relay-reboot path (:8316), the erase-only path (:9215), and the serial-OTA path (:1663). OTA is only partly covered — `_otaInProgress` gates the mesh tick (:12611) but not the other reconcile callers (:1125, :5761, :6867, :12605).

Correct fix: introduce a single `_boardBusy` set (or extend `_boardFlashing`'s lifetime), set it synchronously before every `closeForReconnect()` and clear it only after the matching `reconnect()` settles, on all five paths; then add it to `desiredBoardNumbers()` (app.js:431-438) so the slot stays in `want` rather than merely being spared by a `continue`. Independently worth doing: have the post-flash restore re-register the slot (`boardConnections[n] = conn;` before the `boardGo(n,{mode:'configure'})` calls at :7189/:7205/:7345), so the restore cannot be defeated by any slot bookkeeping at all.

<details><summary>Full verification reasoning</summary>

MECHANISM — every load-bearing fact re-derived from source, all line numbers verified by hand:

1. `desiredBoardNumbers()` (Wizard/app.js:431-438) builds `want` from exactly three sources: the WCBQ floor `1.._boardFloor` (:433), `_meshBoards` (:434), and `for (const k in boardConnections) if (boardConnections[k]?.isConnected?.()) s.add(+k)` (:435), minus `_relaySlots` (:437). No flash/busy notion anywhere.

2. `reconcileBoardGrid()` (app.js:443-465): the eviction loop at :452-458 has exactly the two tests quoted — `wantSet.has(n)` (:453) and `boardConnections[n]?.isConnected?.()` (:454) — then `el.remove(); delete boardConnections[n]; delete boardConfigs[n];` (:455-457). Transcription in the finding is accurate.

3. `isConnected()` is `return this._shared ? !!this._hub?.portOpen : this._connected;` (app.js:4935), and `closeForReconnect()` sets `this._connected = false` as its first statement (app.js:4977). So during a flash `isConnected()` is genuinely false — the reviewer got this right.

4. `boardGo` flash path: `_boardFlashing[n] = true;` (app.js:7063) then `await conn.closeForReconnect();` (:7064). `updateConnectionUI` does carry the `_boardFlashing` guard at :7619 with the comment the reviewer quotes.

5. Consequence chain is real. `conn` is a local (`const conn = boardConnections[n]`, app.js:6996) so the flash and `conn.reconnect()` (:7124) still succeed after `delete boardConnections[n]` — but `reconnect()` never re-registers itself in `boardConnections` (verified: it only sets `this._connected = true` and calls `this._startReading()`). The post-flash restore then does `boardConfigs[n] = …preFlashConfigSnapshot…` (:7186) and `await boardGo(n, { mode: 'configure' })` (:7189), and re-entrant `boardGo` bails at :6996-6997 — `const conn = boardConnections[n]; if (!conn?.isConnected()) { showToast('Board not connected', 'error'); return; }`. Config is never pushed to a board whose NVS was just blanked, and the section is gone from the grid with no Connect button to recover from (only `g-wcbq` / a re-discovery brings it back). So the failure the reviewer describes is mechanically correct.

REACHABILITY — this is where the S1 doesn't hold up.

The vulnerable state requires the slot to be in `want` via the `isConnected()` clause *alone*: above the floor AND absent from `_meshBoards`. `_meshBoards` entries are effectively permanent (only deleted at :481 applyRelayRole, :6834 relay landing, :12600 removeClientCard), and the mesh tick adds any heard non-connected WCB (:12642). So a board that was ever heard on the mesh while not USB-connected is permanently safe. The exposed case is really only the auto-migration path (:6885-6899) creating slot `detected` purely from the live connection — the reviewer's scenario.

Trigger reachability in the window is narrower than claimed. I enumerated every `reconcileBoardGrid()` caller (:470 renderBoards, :482 applyRelayRole, :592 addDiscoveredBoards, :5567 relay-reconnect-failed, :5761 sharedConnect, :6216 boardDisconnect-of-relay, :6899 auto-migration, :12605 removeClientCard) and every `renderBoards()` caller (:313, :1125, :6867, :8383, :10888):
- The reviewer's own quoted trigger — the 12 s `meshAutoDiscoverTick` — cannot fire at all when the flashing board is the only connection: `_wdpMeshConn()` (:12458-12466) iterates `boardConnections` for `isConnected()` and returns null, so the tick early-returns at :12615. A *second* live board is required.
- Even with a second board, `addDiscoveredBoards` only reconciles when `added` is true (:592) — a genuinely new peer must appear mid-flash; and `removeClientCard` (:12605) only runs for a TEMPORARY peer (`prev.peer === 4`) outside the floor (:12664).
- `renderBoards(config.wcbQuantity)` from another board's pull (:6867) is gated on `wcbQuantity > 1`, and raising `_boardFloor` to qty protects any slot ≤ qty — it only evicts a slot numbered *above* WCBQ.
So it needs a conjunction: an auto-migrated slot above the floor and never mesh-heard, a second live connection, and a mesh-membership change (or a deliberate user action — WCBQ edit :1125, shared connect :5761, backup load :8383) inside the window. Plausible on a multi-board bench, not the common single-board flash. And it is recoverable: `boardConfigs[n]` is restored at :7186, so nothing is permanently lost; the user re-flashes or bumps WCBQ.

INTENT — no comment or doc defends this. The comment at :442 ("never removes a board that currently has a live connection") and :454 ("never yank a live board") show the intent was exactly to protect an in-use slot; the flash window is an unhandled case, not a trade-off. `meshAutoDiscoverTick` explicitly guards `_otaInProgress` (:12611) — precedent that this class of interference was thought about for OTA but not for esptool flash. `git blame` puts :443-458 in 010fde70 (2026-07-10), predating nothing relevant. Nothing in docs/ or CLAUDE.md covers reconcile.

So: real defect, correctly diagnosed, over-rated at S1 and under-specified in its window. S3.

</details>

### `Wizard/app.js:628` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**addBoardSection rebuilds a section with factory defaults when a config already exists but the board is not USB-connected**

**Correction to the finding as filed:**

Real bug, but three corrections. (1) Severity is S3, not S2: neither reachable path can push the defaulted DOM to a board (disconnected slot / Wizard-only client slot), the in-memory config is not corrupted here, and the direct-USB case that CAN push is already covered by the existing guard. (2) The strongest evidence is not the relay scenario the reviewer described but the ephemeral-client one, which contradicts an explicit comment: app.js:12602-12603 in removeClientCard says the config is kept so "a user-typed client alias isn't lost", yet on return upsertClientCard (app.js:12525-12552) skips both updateSlotTypeUI and the alias reseed because cfg.type/cfg.clientAlias are already set, so the rebuilt card shows the default WCB pane with b{N}-client-pane hidden (index.html:458,470) and the client status block invisible. (3) The relay scenario needs a precondition the finding omits: after boardDisconnect (app.js:6195-6219) the slot is only rebuilt if it re-enters desiredBoardNumbers() — i.e. relaySlot <= WCBQ floor, or another connected board's WDP sweep re-adds it (app.js:12645). Also drop the claim that this misses relay-MANAGED boards: those slots are pinned in _meshBoards by relayManageOne→addDiscoveredBoards (app.js:492) and painted at app.js:7991, so their sections are never rebuilt with remoteRelayForBoard live.

<details><summary>Full verification reasoning</summary>

MECHANISM — verified line by line, not from the summary.

`addBoardSection` is Wizard/app.js:596-632. It builds the section from `#board-template`, calls `renderSerialTable(n)` (app.js:731-762), preserves an existing `boardConfigs[n]` (app.js:616-619), and paints the model into the DOM ONLY under `if (boardConnections[n]?.isConnected?.())` (app.js:628-631). Every load-bearing sub-claim holds:
- `renderSerialTable` is config-blind: it emits `<option ... ${b === 9600 ? 'selected' : ''}>` (app.js:745-747), `checked` on both `-bcin`/`-bcout` (app.js:748,752) and a label input with only a placeholder (app.js:755). So a rebuilt table always reads 9600 / both broadcasts on / blank labels regardless of `boardConfigs[n]`.
- `populateUIFromConfig` (app.js:2923-3153) is the only model→DOM painter for Kyber/Maestro/HCR/MP3/DFP/WLED/sequences/variables/slot-type, and it is skipped.
- `setRemoteConnected` (app.js:5948-6009) really does only set `remoteRelayForBoard[n]` + touch DOM; it never creates a `BoardConnection`, so the guard is false for relay-managed slots.

REACHABILITY — I grepped every `section-board-` site (app.js:390,411,449,461,480,3218,6830,12604) and every `_relaySlots`/`_meshBoards` mutation. Only two states can present "config exists, section absent, no USB connection", and both are reachable:

(a) Relay slot, the reviewer's scenario. `applyRelayRole(n)` (app.js:479-484) removes `section-board-n` and drops n from `_meshBoards` but deliberately keeps `boardConfigs[n]` (set at app.js:6843). `boardDisconnect(n)` (app.js:6195-6219) then deletes `boardConnections[n]`, `_relaySlots.delete(n)`, and calls `reconcileBoardGrid()`. If n re-enters `desiredBoardNumbers()` (app.js:431-437) — i.e. n ≤ `_boardFloor` (WCBQ; relaySlot = `config.wcbNumber`, app.js:6819, so WCB19 with WCBQ 20 qualifies) or another connected board's WDP sweep re-adds it (app.js:12645) — `addBoardSection(n)` runs with the relay's real config in memory and paints defaults. Same via the failed-reconnect path at app.js:5560-5568.

(b) Ephemeral WDP client, and this one contradicts an explicit comment. `removeClientCard(n)` (app.js:12597-12606) removes the section and states: "Keep boardConfigs[n]: if this ephemeral peer returns, addBoardSection preserves the existing config so a user-typed client alias isn't lost." When the peer returns, `upsertClientCard` (app.js:12525-12552) calls `addDiscoveredBoards([n])` → `addBoardSection(n)`; the section is rebuilt from the template, whose slot-type radio defaults to `value="wcb" checked` and whose `b{N}-client-pane` is `style="display:none"` (index.html:458,470). Because `cfg.type` is ALREADY 'client', `upsertClientCard`'s `if (cfg.type !== 'client') updateSlotTypeUI(n)` never fires, and because `cfg.clientAlias` is already set, the alias-input reseed never fires either. Result: the returning client renders as a blank default WCB card with its status block (`b{n}-client-status`, written at app.js:12552) hidden inside the invisible pane — the exact thing app.js:12602 claims is protected. So the intent recorded in the source says this should work, and it doesn't. That is a CONFIRM, not a refute.

WHAT I COULD NOT REACH — the reviewer's framing that this "misses the remote case" for relay-MANAGED boards. `relayManageOne` (app.js:488-496) always calls `addDiscoveredBoards([targetN])`, which puts targetN in `_meshBoards` permanently; `_meshBoards.delete` only happens in `applyRelayRole` (the relay's own slot), `removeClientCard` (clients only) and app.js:6833 (relay-landing migration, which also deletes the config). So a managed board's section is never torn down while `remoteRelayForBoard[targetN]` is live, and its config is painted by `populateUIFromConfig(targetN, config)` at app.js:7991. The affected slot in path (a) is the relay's OWN slot, after it stopped being a relay.

IMPACT — why S3, not S2. In both reachable paths the stale card cannot push wrong bytes to hardware: path (a) the board is disconnected (`boardGo` bails with "Board not connected", app.js:6996-6997) and a later USB reconnect auto-pulls after 3 s (app.js:5931) which calls `populateUIFromConfig`; path (b) is a Wizard-only client slot with nothing to push. The model itself is not corrupted by this code. The one data-loss tail is `exportSystemFile` (app.js:8405-8433), which for every slot 1..WCBQ calls `syncSerialUIToConfig(n)` (app.js:2904-2921) + `getSequencesFromUI`/`getVariablesFromUI`, writing the default DOM back over the preserved config — but that already happens to a relay slot with NO section at all (missing elements → `parseInt(undefined) || 9600`, `?? ''`), so it is a pre-existing separate bug, not damage caused by this guard. And path (b) slots are `id > _boardFloor` (app.js:12664), outside the export loop. Net: a real, reachable, user-visible wrong-state render that defeats a documented intent — worth fixing, but not S2.

</details>

### `Wizard/app.js:737` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**S3-S5 baud dropdown is capped at 57600, so any higher configured baud blanks the select and is silently rewritten to 9600**

**Correction to the finding as filed:**

The defect is real but the reviewer's trigger path and severity are wrong.

WRONG: "Push succeeds (?BAUD,S3,115200). The user then types a label on Serial 1 and tabs out … the next push sends ?BAUD,S3,9600; the Kyber Maestro link on S3 drops to 9600." Both push paths (Wizard/app.js:7388-7394 and :7780-7787) call syncKyberToConfig AFTER syncSerialUIToConfig, and syncKyberToConfig (app.js:2044-2049) restores config.serialPorts[kyber.port-1].baud from the Kyber baud select. Same protection for Maestro (:3433), WLED (:2835), HCR (:2640), MP3 (:2310), DFP. The push is therefore correct for any device-claimed port.

RIGHT: the unmitigated path is EXPORT. exportSystemFile (app.js:8404, loop :8419-8432) calls only syncSerialUIToConfig — no device sync — and buildSystemFile → buildCommandString emits BAUD,S3,<cur.baud> (parser.js:1314). So a board with S3 at 115200 exports as ?BAUD,S3,9600, and on re-import the Kyber baud follows it down because populateUIFromConfig derives it from serialPorts[port-1].baud (app.js:2991-2994). The Kyber link then really does end up at 9600 — via an export/import round trip, not via a label edit. A second, narrower path is a push against an S3-S5 port above 57600 that is unclaimed or only soft-claimed (serial-map / kyber-reserved / PWM), which nothing restores; that state requires the >57600 to have been set from outside the Wizard.

Also note the 57600 number is not arbitrary — it deliberately mirrors the firmware's own software-serial guard at Code/WCB/WCB_Maestro.cpp:643. The cap should not be removed.

Minimal correct fix (three parts):
1. app.js:2908 — stop coercing an empty select to 9600. Use `const v = parseInt(el?.value); if (!isNaN(v)) config.serialPorts[p-1].baud = v;` so a select that cannot represent the configured value leaves the config untouched instead of silently rewriting it.
2. Whenever a value is written into an S3-S5 baud select (app.js:2969 in populateUIFromConfig, :973 in onKyberBaudChange), inject a matching <option> for an out-of-range configured baud so the display and the config never diverge — or clamp and clamp config to match, with a visible warning.
3. Add a `_kyberApplyBaudCap(n)` mirroring `_hcrApplyBaudCap` (app.js:2468-2490): when the Kyber port is S3-S5, hide Kyber baud options above 57600, clamp the selection, and mirror the clamped value into both config.serialPorts[port-1].baud and b{n}-s{port}-baud. Call it from onKyberPortChange (:927) and populateUIFromConfig.
Separately worth fixing (it is the actual leak in (a)): exportSystemFile should call syncKyberToConfig / syncMaestrosToConfig / syncMP3ToConfig / syncHCRToConfig / syncDFPToConfig / syncWLEDsToConfig alongside syncSerialUIToConfig, matching the push paths.

<details><summary>Full verification reasoning</summary>

MECHANISM — every step re-derived, all true:

1. `Wizard/app.js:737` is verbatim `const maxBaud = p >= 3 ? 57600 : Infinity;` and `:738` filters `BAUD_RATES` (declared at `app.js:7` as `[110,300,600,1200,2400,9600,14400,19200,38400,57600,115200,128000,256000]`). So S3-S5 selects top out at 57600. The options are built once — `renderSerialTable` is called from exactly one place, `addBoardSection` (`app.js:611`), and never rebuilt (grep for `renderSerialTable` returns only `:611` and the definition at `:731`).

2. Firmware imposes no per-port cap on the generic `?BAUD` path. `updateBaudRate()` (`Code/WCB/WCB_Storage.cpp:146-180`) validates only port 1-5 and the 13-value baud whitelist (`:154-157`), then calls `applyLiveBaud` (`Code/WCB/WCB.ino:1886-1909`), which does `Serial3.end(); Serial3.begin(baud, SWSERIAL_8N1, …)`. So `serialPorts[2..4].baud` can legitimately be 115200/128000/256000 on a real board.

3. `Wizard/parser.js:566-572` parses `?BAUD,Sx,<b>` into `config.serialPorts[portIdx].baud` with no whitelist, so a Pull carries the high value into the config.

4. `populateUIFromConfig` `app.js:2969` does `el('baud').value = sp.baud`. Setting a `<select>` to a non-existent option gives `selectedIndex = -1` / `value === ""` → the cell renders blank. Confirmed no later writer restores it: `updatePortClaimUI`'s unclaimed branch (`app.js:781-785`) and its soft-claim branches (`:838-840`, `:848-850`) restore bcin/bcout from config but never touch `baudSel.value`.

5. `syncSerialUIToConfig` `app.js:2908`: `parseInt(document.getElementById(...)?.value) || 9600` → `parseInt("")` is NaN → `9600`. The silent rewrite is real.

6. The Kyber writer is real and reachable: `index.html:631-636` offers Serial 3/4/5 as the Kyber Maestro port; `index.html:640-645` offers `115200` **selected by default** with no port-dependent filtering; `onKyberBaudChange` `app.js:967-976` sets `config.serialPorts[portVal-1].baud = baud` and `serialBaudEl.value = baud` — the DOM mirror silently no-ops for S3-S5.

REACHABILITY — the reviewer's headline scenario is WRONG. Both push paths run the sync functions in a fixed order: `app.js:7388-7394` and `:7780-7787` do `syncSerialUIToConfig → syncMaestrosToConfig → syncKyberToConfig → syncMP3ToConfig → syncHCRToConfig → syncDFPToConfig → syncWLEDsToConfig`. `syncKyberToConfig` (`app.js:2044-2049`) re-writes `config.serialPorts[portVal-1].baud` from the Kyber baud select **after** the serial sync clobbered it, with the comment "Keep serial port baud in sync so the ?BAUD command is generated correctly". Same for Maestro (`:3433`), WLED (`:2835` + `sp.baud`), HCR (`:2640`), MP3 (`:2310`). So "type a label on Serial 1, next push sends ?BAUD,S3,9600 and the Kyber link drops" does NOT happen — the 9600 is overwritten before `buildCommandString` (`parser.js:1311-1315`) runs.

What survives, and it is real:

(a) EXPORT is unmitigated. `exportSystemFile` (`app.js:8404`, loop at `:8419-8432`) calls **only** `syncSerialUIToConfig(n)` — none of the device sync functions. `buildSystemFile` (`parser.js:1548`) → `buildCommandString(…, fullPush=true)` → `add(\`BAUD,S${i+1},${cur.baud}\`)` (`parser.js:1314`) for all five ports. So a board whose S3 really is at 115200 exports as `?BAUD,S3,9600`. On re-import, `populateUIFromConfig` `app.js:2991-2994` derives the Kyber baud from `config.serialPorts[config.kyber.port-1]?.baud`, so the Kyber baud silently follows it down to 9600. The codebase already knows this export hazard exists — the WLED comment at `app.js:2833-2834` says exactly "an EXPORT (which reads the DOM via syncSerialUIToConfig, not syncWLEDsToConfig) can't read a stale value back over these". The Kyber path attempts the same DOM mirror at `:973-974` and it fails precisely because of the `:737` cap. That is the defect.

(b) Push downgrade for an S3-S5 port above 57600 that is not device-claimed — unclaimed, `serial-map` (soft claim, `:841-851`), `kyber-reserved` (`:833-840`), or PWM. Nothing restores those, so the push emits `BAUD,S4,9600` against a 115200 baseline. But getting there requires setting >57600 on a software-serial port from outside the Wizard (terminal `?BAUD,S4,115200` or a hand-edited file).

(c) The blank baud cell itself, on any Pull of a board with S3-S5 > 57600.

INTENT — the 57600 cap is deliberate, not arbitrary: it mirrors `WCB_Maestro.cpp:643` (`if (serialPort >= 3 && serialPort <= 5 && baudRate > 57600)` → "CONFIGURATION BLOCKED!"). The stricter 9600 soft-serial guards live at `WCB_HCR.cpp:635`, `WCB_MP3.cpp:292`, `WCB_WLED.cpp:312`, and `WCB_DFP.cpp:254-256` explains why DFP needs none. `git blame` puts `:737` at 45d3aff9 (2026-03-02) with no comment. The missing piece is the Kyber counterpart of `_hcrApplyBaudCap` (`app.js:2464-2490`), whose own comment states the pattern: "Keep the underlying serial port baud in sync so buildCommandString doesn't emit a contradictory ?BAUD,S&lt;port&gt;,&lt;high&gt; alongside ?HCR,PORT,S&lt;port&gt;:9600 on the same push."

So: real defect, wrong mechanism, wrong severity. Not a live-link drop on the next push (guarded); an export/import data loss plus a blank UI cell → S3.

</details>

### `Wizard/app.js:855` — CONFIRMED · S2 → **S3**

**MP3 / HCR / DFPlayer port dropdowns are never refreshed when another feature frees a port**

**Correction to the finding as filed:**

Two corrections to the finding. (1) The cited line for the MP3 filter is wrong: it is app.js:2287, not :2283 (function `updateMP3PortDropdown` starts at :2276). (2) The stated failure scenario is not the one that bites. Enabling MP3 via the checkbox (index.html:780 → onAudioToggle app.js:2151 → onMP3Change) rebuilds the dropdown at :2196/:2208, so "free Kyber, then set up an MP3" self-heals. The real stale case is a device that is ALREADY Local: MP3 local on S3 + Kyber local claiming S1/S2, user sets Kyber to None, then tries to MOVE the MP3 onto the freed hardware UART — S1/S2 are absent from the list until the section is repopulated. Also worth adding: the reviewer omitted that `populateUIFromConfig` (app.js:2923) already refreshes all six pickers (:3042/:3084/:3124), so every full-repopulate path (board pull, file load, controller switch via `_applyControllerToBoards` app.js:1318) is unaffected — this is an incremental-edit-only staleness bug. Finally, the sharper consequence is the opposite direction from the one described: a stale list that still OFFERS a port which just became `kyber-reserved` lets the user select it, and the selection sticks (the no-clobber reserve at parser.js:1093 lets the `mp3` claim win, so the rebuild at :2248 keeps the option), producing a push that the firmware silently rejects at WCB_MP3.cpp:316-318 — exactly the failure the reservation comment at parser.js:1085-1092 says it exists to prevent.

<details><summary>Full verification reasoning</summary>

MECHANISM — verified line by line.

`updatePortClaimUI(n)` is Wizard/app.js:764-857. Its tail is exactly as quoted (comment at :853-854, `refreshAllMaestroPortDropdowns(n)` :855, `refreshAllWLEDPortDropdowns(n)` :856) and it returns at :857 without touching the three single-device pickers.

Grepping the whole Wizard dir for the six refreshers gives every call site:
- `updateMP3PortDropdown` — defined app.js:2276, called ONLY at :2196 (onMP3Change), :2208 (onMP3Change tail), :2248 (onMP3PortChange), :3042.
- `updateHCRPortDropdown` — defined :2493, called ONLY at :2564, :2577, :2603, :3084.
- `updateDFPPortDropdown` — defined :2417, called ONLY at :2359, :2371, :2396, :3124.
The reviewer's enumeration is accurate. I confirmed :3042/:3084/:3124 all sit inside `populateUIFromConfig` (declared app.js:2923), which also calls `evaluatePortClaims` + `updatePortClaimUI` at :3141-3142 — so every FULL-repopulate path (board pull, file load, `_applyControllerToBoards` at :1318, post-flash resync) already refreshes all six. That is a real mitigation the finding does not mention and it bounds the bug to incremental in-session edits.

The claim source is real: `evaluatePortClaims` (parser.js:986) wipes all claims at :988-990 and re-derives them; the mode reservation is parser.js:1091-1098 — `reserve(0); reserve(1)` for `kyber.mode === 'local'` (:1096) and `reserve(0)` for `'remote'` (:1097), assignment `{ type: 'kyber-reserved' }` at :1094 (reviewer cited :1096 — the call, not the assignment; immaterial). The MP3 filter is `if (!claim || claim.type === 'mp3')` at app.js:2287, not :2283 as claimed (function starts at :2276). HCR filter :2501, DFP filter :2428 — same shape.

REACHABILITY — the gap is real and unguarded, but the reviewer's stated scenario is the wrong one.

`onKyberChange` (app.js:860) is wired from three inline radios in index.html:623-625 and also reached from `onNavicoreRemoteChange` → `setBoardKyberMode` (app.js:1302-1306). It calls `evaluatePortClaims` (:920) → `updatePortClaimUI` (:921) → `updateKyberPortDropdown` (:922), `updateKyberMarcPortDropdown` (:923), `updateRemoteSectionsUI` (:924). I read `updateRemoteSectionsUI` (:2067-2089) in full — it only flips radios/section visibility and calls `updateNavicoreStatusText`. Nothing on that path rebuilds the MP3/HCR/DFP pickers. Same for `syncMaestrosToConfig` (:3438-3440), `syncWLEDsToConfig` (:2842-2844) and `syncMappingsToConfig` (:3620-3621). So a Maestro/WLED/PWM/Kyber claim change leaves those three selects stale. Confirmed defect.

BUT the finding's own failure scenario ("user sets Kyber to None to free the UARTs for an MP3 Trigger") largely self-heals: turning MP3 on goes through the checkbox at index.html:780 → `onAudioToggle` (:2151) → `onMP3Change`, which calls `updateMP3PortDropdown` twice (:2196, :2208). The stale window only exists when MP3/HCR/DFP is ALREADY Local with its port wrap visible and the user wants to MOVE it to a newly-freed port.

INTENT — not a documented trade-off. `git log -L 850,857:Wizard/app.js` shows the Maestro refresh added in 20b09d5 (2026-07-13 10:37) and the WLED refresh bolted on 4 hours later in 55a20b5 (14:19). The comment reads "…so they stay consistent with HCR/MP3/Kyber/PWM" — i.e. it was written to keep the multi-row pickers in sync with the single-device handlers; the reverse direction was simply never covered. Incremental patch, not a deliberate omission.

WORSE VARIANT the finding missed (and why it isn't purely cosmetic): the stale-OFFER direction defeats the very purpose parser.js:1085-1092 states — "device dropdowns offer ports the board will refuse and the push fails silently." I confirmed those firmware guards exist: WCB_MP3.cpp:316-318, WCB_HCR.cpp:651-653, WCB_DFP.cpp:270-272, WCB_WLED.cpp:328-330, all `(serialPort == 1 && (Kyber_Local || Maestro_Remote)) || (serialPort == 2 && Kyber_Local)`. Sequence: MP3 already Local, user then sets Kyber Local (or NaviCore Remote) → S1/S2 reserved → stale MP3 list still offers S1 → user picks S1 → `onMP3PortChange` sets `claimedBy={type:'mp3'}` and `reserve()` is no-clobber (parser.js:1093), so the rebuild at :2248 KEEPS S1 (claim.type==='mp3'). The bad selection sticks. Push order is KYBER (parser.js:1347) before MP3/HCR (:1382), so the board applies Kyber first and then silently rejects `?MP3,PORT,S1`.

SEVERITY — downgrading to S3. It is a browser-tool UI-staleness bug, not firmware. No data loss; the freeing direction has an obvious workaround (toggle the device mode off/on, re-pull, or reload — all route through `populateUIFromConfig`); the offer direction needs a specific edit order to hit. Real, worth fixing, one line — but not S2.

</details>

### `Wizard/app.js:962` — CONFIRMED · S2 → **S3**

**Changing the Kyber serial port never marks the board unsaved — the only device port handler missing the dirty call**

**Correction to the finding as filed:**

Severity only. The defect is real and the location, mechanism and fix are all correct as stated, but the claimed failure overstates the consequence. The change is NOT at risk of being lost: Push Config diffs against boardBaselines (app.js:7809-7811) and parser.js:1342 explicitly compares `baseline.kyber.port !== config.kyber.port`, so the moved port is emitted as `?KYBER,LOCAL,S<port>`; Push All (app.js:8253-8303) pushes every connected/remote slot with no dirty filter; there is no beforeunload guard and no localStorage persistence of boardConfigs. The unsaved badge is read only by updatePushAllButton:3158 to toggle a CSS class. The user also does get visible feedback that the change landed, since app.js:949-951 relabels the serial row to 'Kyber Maestro'. Impact is therefore the missing "Changes pending" toast and the missing amber Unsaved badge / button tint — a cosmetic inconsistency with every sibling handler, worth fixing for consistency, but S3 rather than S2.

<details><summary>Full verification reasoning</summary>

MECHANISM — verified by reading, not by trusting the summary. Wizard/app.js:928-962 is `onKyberPortChange(n)`; it clears the old `kyber` claim (933-942), sets `config.kyber.port` + claim + label (944-956), then ends with `WCBParser.evaluatePortClaims(config); updatePortClaimUI(n); updateKyberPortDropdown(n); updateKyberMarcPortDropdown(n);` at 958-961 and closes at 962. There is no `onBoardFieldChange(n)`. The transcription in the finding is exact.

I re-derived the "only handler missing it" claim mechanically rather than accepting it. An awk pass over all 44 `^function on*Change` bodies in app.js flagged 7 with no direct dirty call: onKyberPortChange:928, onWCBQuantityChange:1116, onNavicoreRemoteChange:2121, onWLEDPortChange:2776, onMaestroPortChange:3323, onMappingTypeChange:3546, onWizKyberPortChange:10060. Six of those delegate: onWLEDPortChange:2787 -> onWLEDChange:2789 -> onBoardFieldChange:2792; onMaestroPortChange:3339 -> onMaestroChange:3377 -> onBoardFieldChange:3379; onMappingTypeChange:3570 -> onMappingChange:3571 -> onBoardFieldChange:3573; onNavicoreRemoteChange:2123 -> setBoardKyberMode:2114-2117 -> onKyberChange -> onBoardFieldChange:925. onWCBQuantityChange:1116 (grid sizing) and onWizKyberPortChange:10060 (first-run wizard, writes wizardState not boardConfigs) are not per-board device handlers. So onKyberPortChange genuinely is the sole per-board device handler with no dirty marking, directly or by delegation. Its own siblings all have it: onKyberChange:925, onKyberBaudChange:976, onKyberMarcPortChange:1062.

REACHABILITY — real. `grep -rn onKyberPortChange Wizard/` returns exactly two hits: the definition (app.js:928) and the inline binding `<select id="b{N}-kyber-port" onchange="onKyberPortChange({N})">` at index.html:630. The wrap `b{N}-kyber-port-wrap` is display:none by default and shown under Kyber mode 'local', and `updateKyberPortDropdown:979-998` offers every port that is unclaimed or claimed by kyber, so a pulled-clean board with Kyber already local can have its port moved with no other edit. No guard, clamp or early return blocks it.

INTENT — not deliberate. No comment near 958-962 explains an omission; the comments there are about port-claim re-filtering. `git log -L 928,962:Wizard/app.js` shows the last touch was cdb0708 ("web page cleanup"), a label/baud rework that added the 'Kyber Maestro' label handling and simply never added the dirty call. Nothing in CLAUDE.md or docs/ describes a Kyber-port exemption.

WHY S2 IS TOO HIGH — this is the part the reviewer did not check. The dirty flag is purely cosmetic on this path. onBoardFieldChange:1483-1486 does only `_notifyBoardChanged(n)` (a toast, 1068-1071) and `updateBoardStatusBadge(n,'unsaved')`. The badge is read in exactly one place, updatePushAllButton:3158, which only toggles btn-success/btn-pending CSS classes. Nothing gates behaviour on it: the push is diff-based against boardBaselines, not against dirty (app.js:7809-7811 `const fullPush = !boardBaselines[n]; const cmdString = WCBParser.buildCommandString(config, boardBaselines[n] ?? null, fullPush)`), and parser.js:1341-1343 tests `baseline.kyber.port !== config.kyber.port` explicitly, emitting `KYBER,LOCAL,S<port>` at 1347-1350. boardGoAll:8253-8303 iterates every connected/remote slot with no dirty filter. There is no beforeunload handler (grep: zero hits) and boardConfigs are never persisted to localStorage. So the port change is captured in config and IS pushed on the next Push Config / Push All — no data loss, no wrong wire bytes. The user also still gets visual confirmation the change took: app.js:947-951 sets the claim and writes 'Kyber Maestro' into the serial-row label field, and updatePortClaimUI(n) runs at 959. updateActionSummary:3164-3182 lists HCR/MP3/DFPlayer/mappings/maestros/sequences/variables and no Kyber field, so skipping it causes no stale summary either. The whole impact is a missing toast and a missing amber cue — a UX inconsistency, not a functional defect. S3.

</details>

### `Wizard/app.js:1219` — CONFIRMED · S2 → **S3**

**ETM Message Count defaults to 3, below the firmware's clamp floor of 10 — the value can never round-trip**

**Correction to the finding as filed:**

Real, but not S2 and not for the stated reason. Mechanism correction: the value is the `?ETM,CHAR` benchmark's messages-per-board-per-phase (WCB.ino:1436/:1456), not an operational ETM setting — the firmware clamps it, so nothing invalid reaches the board and the mesh is unaffected. Reachability correction: the "fresh load, no pull" path self-heals (generalBaseline is null, so app.js:6855 adopts the board's values and app.js:8114 rewrites the field) and shows no modal. The modal/never-round-trips loop needs a board whose pulled config has ETM disabled, because app.js:8107 gates the ETM DOM refresh on `config.etm.enabled` — then g-etm-count stays "3", the user turns ETM on and edits an ETM field, updateGeneralBaseline (app.js:1192) bakes 3 into the baseline, the push is clamped to 10, and every subsequent verify pull re-pops the mismatch modal that "keep my values" cannot resolve. Fix as proposed (20 + range hints), and while in that line also fix the two wrong tooltips at index.html:177-178 (this field is the CHAR test sample size, per WCB_Help.cpp:269-270, not an "offline alert repeat"). Separately, raise the neighbouring g-etm-miss default from 3 to 5 (index.html:175, app.js:1217, parser.js:122/175, app.js:1428): ETM,MISS is unclamped (WCB.ino:4687), so a Wizard full push reverts the deliberate 3→5 widening documented at WCB.ino:266 — that one has a real operational effect (33 s vs 55 s offline window / board flapping) and outranks this finding.

<details><summary>Full verification reasoning</summary>

FACTS RE-DERIVED FROM SOURCE (all verified by opening the files, not the summary):

1. The literal code is as claimed. Wizard/app.js:1219 `const count = parseInt(document.getElementById('g-etm-count')?.value) || 3;` — and it is the odd one out: :1215 timeout||500, :1216 hb||10, :1217 miss||3, :1218 boot||2, :1220 delay||100. Wizard/index.html:177 ships `value="3"`. Every other Wizard default for this field is 20: parser.js:124 and :178 (`messageCount: 20`), parser.js:830 (`parseInt(parts[2]) || 20`), app.js:1426 (`etmCount: config.etm?.messageCount ?? 20`), app.js:9303 (`messageCount:20`).

2. The firmware floor is real: `constrain(etmVal.toInt(), 10, 200)` at Code/WCB/WCB.ino:4696 (`?ETM,COUNT,n`) and WCB.ino:5303 (legacy `ETMCHARCOUNTn`). Default is 20 (WCB.ino:269), NVS load default 20 (WCB_Storage.cpp:2231), and the on-board help states it explicitly: WCB_Help.cpp:269-270 "COUNT,n  Test messages per board per phase during CHAR / Range: 10-200, Default: 20". The backup emits it unconditionally (WCB.ino:2871), and the Wizard pushes it at parser.js:1493. So a pushed 3 is stored as 10 and can never equal 3 on a re-read. That half of the finding stands.

3. git log -L 177,177 Wizard/index.html: `value="3"` has been there since the field was first added (7283365 / b9230b4). Sibling fields were later corrected in the same block (timeout 30000→500, boot 30→2) and count was missed. Nothing deliberate, no explaining comment — so this is not a documented trade-off.

WHERE THE FINDING IS WRONG:

4. The claimed failure narrative does not happen as written. On a fresh load `generalBaseline` is null (app.js:71), so `updateGeneralBaseline()` early-returns (app.js:1191) and never records etmCount:3. The verify pull then hits app.js:6855 `if (!generalBaseline)` → `syncGeneralFromConfig(config)` + baseline adopted from the board — no `getGeneralMismatches` call, no modal, and app.js:8114 rewrites the DOM field to the board's value. boardConfigs[n] and boardBaselines[n] are also replaced wholesale by the pulled config (app.js:6917), and the push path deliberately does NOT advance the baseline optimistically (app.js:7554-7580 comment) — it re-pulls 300 ms later. So the "fresh load" path self-heals silently; the reviewer's modal does not appear there.

5. The modal IS reachable, but only by a narrower route the reviewer did not find: app.js:8107 gates the ETM DOM refresh on `if (config.etm.enabled)`. Pull a board with ETM OFF → g-etm-count stays at the HTML "3". Now the user turns ETM on (onETMToggle) and nudges any ETM number (onGeneralETMChange) → systemConfig + every boardConfigs gets 3, and this time `updateGeneralBaseline()` does write etmCount:3 into the existing baseline (app.js:1192). Push sends `ETM,COUNT,3` (parser.js:1487-1493, gated on etm.enabled), firmware stores 10, the verify pull hits app.js:6859 and pops showGeneralMismatchModal with "ETM Message Count 3 vs 10" — and "Keep my values" (app.js:6130-6137, applyGeneralFieldsToBoardConfig writes 3 back) makes it repeat forever. That is the genuine un-round-trippable loop.

6. SEVERITY IS OVERSTATED. `etmCharMessageCount` is not an operational ETM parameter at all — it is the sample size of the `?ETM,CHAR` characterization benchmark: `Serial.printf("Messages per board per phase: %d", etmCharMessageCount)` (WCB.ino:1436) and `int totalMessages = etmCharMessageCount * peerCount` (WCB.ino:1456). It has zero effect on ACKs, retries, timeouts or heartbeats. The firmware clamps, so no invalid state ever reaches NVS. Worst real outcome: a board's benchmark sample silently drops from 20 to 10, plus the narrow unresolvable mismatch modal above. That is a UX/consistency defect, not S2.

7. Bonus finding for the parent while I was in here: the Wizard's tooltip for this field (index.html:177, "How many times to repeat the offline alert message when a board is detected as unresponsive") is factually wrong — same for g-etm-delay at :178. And the genuinely operational sibling: g-etm-miss defaults to 3 (index.html:175, app.js:1217, parser.js:122/175, app.js:1428) while the firmware default is 5, with an explicit comment at WCB.ino:266 recording WHY it was widened ("(etmHeartbeatSec+1)*5 = 55s offline window — widened from 3/33s so a few lost broadcast heartbeats on a busy mesh don't flap a live board"). `ETM,MISS` has no clamp (WCB.ino:4687), so any Wizard full push (`fullPush = !boardBaselines[n]`, app.js:7417) silently reverts that deliberate widening on every board. That one is worth more than the finding under review.

</details>

### `Wizard/app.js:1663` — CONFIRMED · S2 → **S3**

**boardOtaSerial's post-OTA reboot handling has no `_shared` guard, so a successful OTA on a shared-hub port leaves the board card stuck at "Not connected"**

**Correction to the finding as filed:**

Two corrections/additions to the finding as written:

1. Line numbers drift slightly: `updateConnectionUI` starts at `Wizard/app.js:7611` (not 7627); the button-disabling block is `:7637-7642`. The sibling fix to mirror is `:7513-7520`, added by d249b1e, and its comment already states this exact failure mode — cite it in the fix comment.

2. The reviewer treats the skipped baud raise as incidental. It is a second, separate consequence worth calling out: `:1581` gates the raise on `conn.port && typeof conn.port.reconfigure === 'function'`, so a shared OTA runs the whole transfer at 115200. The ESP32 app image is 1,284,464 bytes (`Code/bin/WCB_6.2.0_172007RAUG2026_OTA_ESP32.bin`); at CHUNK=1024 (`:1605`) that is 1255 chunks of ~1392 wire bytes each ≈ 1.75 MB, ~152 s of pure UART time at 11.5 KB/s plus per-chunk flash-write, versus roughly 20 s at OTA_XFER_BAUD=921600 (`:1530`). That degradation is inherent — `setBaud` (`:5204`) needs the raw port the hub owns — so the guard at `:1581` is correct, but the shared branch should log "transfer runs at 115200 on the shared port — this takes a few minutes" so the slowness isn't mistaken for a stall.

<details><summary>Full verification reasoning</summary>

I re-derived every load-bearing fact from source rather than the summary.

MECHANISM — all four links verified:
1. `Wizard/app.js:4916` — `BoardConnection.constructor` sets `this.port = null`.
2. `Wizard/app.js:5413-5440` — `connectShared(hub)` sets `_shared=true`, `_hub`, `_sharedBuf`, `_sharedDecoder`, `_onHubData/_onHubState/_onHubLog`, `hub.join()`, `this._connected = hub.portOpen`. It never touches `this.port`. Confirmed by `becomeShared()` at `:5487` which explicitly does `this.port = null; // the hub owns the port now`, and by `becomeDirect(port)` at `:5568` which is the only thing that gives a shared conn a port back.
3. `Wizard/app.js:4935` — `isConnected() { return this._shared ? !!this._hub?.portOpen : this._connected; }` → a shared board passes `boardOtaSerial`'s only connection gate at `:1535`.
4. `Wizard/app.js:4997` — `async reconnect(...) { if (!this.port) return false; ...}` is the literal first statement. For a shared conn it returns false with zero attempts.

I grepped every `_shared` occurrence in app.js (4927, 4935, 5212, 5414-5465, 5488, 5597, 5607, 5659, 5665, 5716, 5746, 5760, 6202, 6978, 7014, 7033, 7513, 12144). None of them is inside `boardOta`/`boardOtaSerial` (1515-1706). There is no shared guard on this path.

REACHABILITY — stronger than the reviewer claimed. The shared state is not an exotic opt-in; it is the DEFAULT for the first board:
- `Wizard/index.html:585-587` — the `⬆ OTA` button is unconditionally rendered (no `display:none`, unlike `b{N}-btn-update-fw` at `:583`) and calls `boardOta({N})`.
- `Wizard/app.js:5634` — `async function establishConnection(n, port, usedPorts = new Set(), allowShare = true)`, and `:5641` auto-shares when no shared port exists yet. The ordinary Connect-modal path calls it with the default (`:6512`, `:6621`, `:5927`, `:11585/11605/11629`); only the first-time BULK auto-detect opts out (`:6582`, `allowShare=false`). So "connect WCB 1, click ⬆ OTA" is the plain single-board flow and it is shared.
- `boardOtaSerial` works fine over the hub: `send()` dispatches to `this._hub.send(data)` at `:5212`; `serial-hub.js:190` writes directly as leader / relays `{t:'tx'}` as follower; inbound bytes go `_onHubData` → `_handleLine` (`:5424`) → `this._dataCallbacks.forEach(...)` at `:5313`, which is what `sendAndCollect` (`:5232`) listens on. So STATUS/BEGIN/DATA/END all complete and `[OTA:END,OK]` is really reached.

I checked one plausible mitigation the reviewer didn't: the "Flashing…" early return in `updateConnectionUI` (`:7618-7625`) is gated on `_boardFlashing[n]`, and grep shows `_boardFlashing` is written ONLY at `:7063`/`:7109` — inside boardGo's esptool path. `setFlashUI` (`:9072`) does not set it. So neither `updateConnectionUI(n,false)` call is absorbed; the reviewer's "runs twice" is correct and both run in full: `:7627` label → "Not connected", `:7637-7642` disable pull/go/erase/etm-char/stats/identify, plus `updateSequencePlayButtons`/`updateVariableButtons`.

No automatic recovery exists. The only thing that repaints a shared card is `_onHubState` (`:5433`), driven by `serial-hub.js` `_emitState()`, which fires only from `_setRole` (`:530`), `_announceState` (`:525`, on port open/close and a `hello`), and a follower receiving the leader's `state` (`:503`). A software `?reboot` on a UART-bridge WCB does not drop the port, so no state event fires. `addBoardSection`'s re-derive at `:628-631` only runs on card creation.

INTENT — the opposite of deliberate. `git blame` shows the OTA reboot block is 0bcef12a (2026-06-30). The identical bug in the config-push reboot was fixed a month LATER in d249b1e (2026-07-30), `Wizard/app.js:7513-7520`, whose comment says verbatim: "Running the direct closeForReconnect()+reconnect() here would operate on a portless shared conn (this.port===null) and falsely report 'did not come back'." Its commit message repeats it. The serial-OTA path was simply not swept in that pass.

Net effect: the firmware IS correctly written, verified and booted; the UI falsely says the opposite. Card shows "Not connected", six action buttons grey out, and an error toast reads "WCB n did not come back after OTA — reconnect manually". The post-OTA `waitForBoardReady`/`boardPull` never runs, so the displayed Software Version stays stale.

SEVERITY — I downgrade S2 → S3. It is real and on the default path, but there is no firmware, data or config loss and no risk to the board: the update succeeded. Recovery is self-service (reload, or two clicks — note `boardConnect` at `:6185` tests `isConnected()`, which is still true, so the button labelled "Connect" actually calls `boardDisconnect`, tearing the share down; a second click re-establishes it). That is confusing-and-wrong, not damaging.

</details>

### `Wizard/app.js:2003` — CONFIRMED · S2 → **S3**

**populateUIFromConfig calls onHWVersionChange, which silently rewrites the pulled LED pin (HW 3.2 @ GPIO38 → 48) and marks the board dirty**

**Correction to the finding as filed:**

The observation and mechanism stand; three things need correcting.

(a) **"Every subsequent pull re-introduces the same drift" is false.** Once the bad push lands, NVS holds 48, `?backup` emits `LED,PIN,48`, and on the next pull `cur === 48` with `hwVal === 32` makes `def === null` (Wizard/app.js:2003) — the heuristic does not fire again. It is a one-shot corruption per NVS state, not a pull/push loop. This matters because **docs/CODE_REVIEW_2026-08_wave1.md:433 builds a separate finding on the false premise** ("per wave3 a stock HW-3.2 board re-emits `LED,PIN,48` on every pull/push cycle, so this fires repeatedly" — used to argue an unbounded NeoPixel leak). That dependent claim must be corrected too: the repeat driver there is a user retrying pins by hand, not the Wizard.

(b) **The reviewer missed the more damaging direction.** The second arm at Wizard/app.js:2004 is `(hwVal === 31 && (cur === 48 || cur == null)) ? 38`. A HW **3.1** user who deliberately ran `?LED,PIN,48` because their DevKitC-1 is a v1.0 has that *deliberate, working* setting silently reverted to 38 on the very next Pull, and pushed back. That case destroys an intentional user configuration rather than a default, and is the stronger argument for fixing this.

(c) **The underlying rule, not just the call site, is wrong.** Both WCB 3.1 and 3.2 carry the identical `ESP32-S3-DEVKITC-1-N8R2` (PCB/…V3.1 and …V3.2 `.kicad_sch`), and neither carrier PCB has an LED net at all — so "3.1 → 38, 3.2 → 48" has no hardware basis in either direction. `wizardOnHWVerChange` (Wizard/app.js:9829-9848) carries a byte-identical copy of the same heuristic and should be fixed in the same commit. The right fix is to delete the mapping (leave the pin at the firmware default of 38 and let the user pick 48/47 from the dropdown), not merely to gate it off the load path. If it is kept, the app.js:1999 comment and the parser.js:24 comment ("48 applied on HW-version select for 3.2") both contradict WCB_Help.cpp:977 and the wiki and need correcting.

Severity: **S3, not S2.** It is real, silent, deterministic, persisted to NVS, and reachable from the primary workflow — but the blast radius is one status LED, it is visible in the LED-pin dropdown after the pull, and recovery is a single documented command (`?LED,PIN,38`). It sits clearly below the wave's S1/S2 exemplars (a flash that dead-ends leaving a board with blank NVS; user sequence data silently truncated into NVS).

Separately and independently of the LED pin: `onHWVersionChange` calls `onBoardFieldChange(n)` unconditionally at Wizard/app.js:2012, so **every** pull of **every** board — all HW versions — fires `_notifyBoardChanged` (app.js:1068-1071) and shows a spurious "Changes pending — push to WCB N" toast next to the genuine "Config pulled" toast. That half is S4 cosmetic (the `'unsaved'` badge set at :1485 is immediately overwritten by the caller's `updateBoardStatusBadge(n,'connected')`), and it is worth fixing in the same edit.

<details><summary>Full verification reasoning</summary>

I re-derived every link in the chain from source rather than from the finding's summary.

**1. The heuristic exists exactly as quoted.** `Wizard/app.js:1988-2013`. Lines 2001-2010 are verbatim what the finding shows; the comment at :1999-2000 claims "3.1 → GPIO38, 3.2 → GPIO48". `git log -L 1988,2015:Wizard/app.js` shows the block was added in e9c2baf ("updates") with no accompanying rationale beyond that comment — it is not a documented trade-off.

**2. The load path really does run it.** `Wizard/app.js:2946` — `if (hwSel) { hwSel.value = config.hwVersion || 0; onHWVersionChange(n); }`, inside `populateUIFromConfig` (declared at :2923). `onHWVersionChange` re-reads the value out of the DOM (`parseInt(document.getElementById(...).value)`), and `Wizard/index.html:535-543` does contain `<option value="32">`, so `hwSel.value='32'` sticks and `hwVal === 32`. There is no `fromLoad` flag, no `_populating` guard — I grepped for `_suppress*`/`_loading*`/`_populating` and the only hits are unrelated (`_suppressedEtmEdges`, `_suppressTerminalLine`).

**3. `boardConfigs[n] === config` at that moment, in every pull path.** `Wizard/app.js:6879`, `:6890`, `:6916`, `:7984` (and `:8388`, `:11006`) all do `boardConfigs[X] = config` **before** `populateUIFromConfig(X, config)`. Critically, `boardBaselines[X] = JSON.parse(JSON.stringify(config))` is taken on the line *before* the populate (`:6880`, `:6917`, `:7985`), i.e. the baseline snapshots 38 and the live config is then mutated to 48.

**4. The firmware really does report 38 for HW 3.2.** `Code/WCB/wcb_pin_map.cpp:97` — the branch is `else if (wcb_hw_version == 32 || wcb_hw_version == 31)` and `:112` sets `STATUS_LED_PIN = 38` for *both*. `Code/WCB/WCB_Storage.cpp:115-123` `loadStatusLEDPin()` only overrides when NVS `led_config/led_pin` is >= 0, so a stock board keeps 38. `Code/WCB/WCB.ino:2779-2780` emits `LED,PIN,<STATUS_LED_PIN>` in the config backup for hw 31/32, and `Wizard/parser.js:482` parses it into `config.statusLedPin`.

**5. The drift reaches the wire.** `Wizard/app.js:7417` `const fullPush = !boardBaselines[n];` — after a pull the baseline exists, so it is a diff push. `Wizard/parser.js:1244-1249`: `ledPin = 48`, `bLedPin = 38`, `48 !== 38` → `add('LED,PIN,48')`. `Code/WCB/WCB.ino:5127-5141` accepts it (0-48 range check passes), `saveStatusLEDPin(48)` writes NVS, `STATUS_LED_PIN = 48`, `initStatusLEDWithRetry(5,100)` re-inits the NeoPixel on the wrong GPIO. Persisted across reboot. Side confirmation that the drift is real: without it, Pull→Push with no edits would hit the `if (!cmdString) showToast('No changes to push')` branch at `:7420`; with it, it always has something to send.

**6. The reviewer's hardware premise is right, and I found it is *more* wrong than stated.** Both `PCB/Wireless Communication Board (WCB)V3.1/*.kicad_sch` and `.../V3.2/*.kicad_sch` populate the same `ESP32-S3-DEVKITC-1-N8R2` module, and neither schematic has any LED net (the only labels are `5V`, `GND`, `S1_RX`…`S5_TX`) — the status NeoPixel is the DevKitC-1's own RGB LED, whose pin is GPIO48 on DevKitC-1 v1.0 and GPIO38 on v1.1. So the pin is a function of *which devkit batch the user bought*, not of the WCB revision. That is confirmed in the docs: wiki `Command-Reference.md:101` ("mainly for DIY ESP32-S3 boards (HW 3.1/3.2) whose onboard LED isn't on the default pin… the default otherwise follows `?HW,x`"), wiki `Changelog.md:172` ("Update onboard LED pin to match your ESP32S3 Dev Kit"), `Code/WCB/WCB_Help.cpp:977-979` ("38 - HW 3.x default", 48/47 as alternatives), and the Wizard's own tooltip at `Wizard/index.html:552` ("Default 38. Common alternatives: 48, 47"). Nothing anywhere except the app.js:1999 comment asserts a per-variant rule.

**7. Reachability is the normal workflow.** `Pull Config` → `Push Config` / `Push All`. No hardware state is required beyond "HW 3.2, LED pin never customised" — i.e. the factory state of every 3.2 board.

**Two corrections to the finding (see `correction`)**: the drift is one-shot per NVS state, not recurring; and the reviewer missed the more damaging symmetric case.

</details>

### `Wizard/app.js:2236` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**MP3 baud picker lets the user set values the firmware rejects outright — the whole `?MP3,…` line is silently dropped**

**Correction to the finding as filed:**

Mechanism, lines and quoted firmware text are all correct — no correction needed there. Two corrections:

1. SEVERITY S2 → S3, because the failure is NOT uniformly silent. On the direct-USB push path the no-reboot branch (Wizard/app.js:7564-7580) deliberately refuses to advance the baseline and fires a verify `boardPull(n)`, which repopulates the UI (app.js:6918) so the MP3 block reverts to unconfigured and the diff comes back; the firmware's "[MP3] MP3 Trigger only supports 9600 or 38400 baud" / "CONFIGURATION BLOCKED!" text is also NOT filtered by `_suppressTerminalLine` (app.js:8781-8791). The genuinely silent-and-sticky case is the RELAYED path only: `boardGoRemote` (app.js:7769) sets `boardBaselines[n] = JSON.parse(JSON.stringify(config))` at :7873 with no verify pull, so the rejected MP3 line is never re-sent. Lead with that path, not "the push shows green".

2. ADD a consequence the finding misses: `syncMP3ToConfig` (app.js:2308-2310) mirrors `mp3.baud` into `config.serialPorts[port-1].baud`, and `parser.js:1314` emits `?BAUD,S<p>,<b>` BEFORE the `?MP3` line at `:1364`. `updateBaudRate` (Code/WCB/WCB_Storage.cpp:146-180) accepts all 13 values on all 5 ports with no soft-serial cap, so the port baud is applied while the MP3 config is rejected — a half-applied state (S3 really at 38400, no MP3), not a clean no-op.

Also note the simplest repro is easier than the one filed and needs no port move: MP3 on S1/S2 with any of the 11 non-{9600,38400} options — the Wizard applies no clamp at all on hardware serial.

<details><summary>Full verification reasoning</summary>

MECHANISM — every number re-derived from source, all correct as filed.

- `Wizard/index.html:809-822`: the `b{N}-mp3-baud` select really does offer 13 values — 110, 300, 600, 1200, 2400, 9600 (`selected`), 14400, 19200, 38400, 57600, 115200, 128000, 256000. That list is character-for-character the whitelist in `updateBaudRate` (`Code/WCB/WCB_Storage.cpp:154-157`), i.e. it was copied from the generic serial-baud picker.
- `Code/WCB/WCB_MP3.cpp:287-290`: `if (baudRate != 9600 && baudRate != 38400) { Serial.println("[MP3] MP3 Trigger only supports 9600 or 38400 baud"); return; }` — verbatim.
- `Code/WCB/WCB_MP3.cpp:292-303`: `if (serialPort >= 3 && baudRate > 9600) { … "❌ CONFIGURATION BLOCKED!" … return; }` — verbatim.
- Both returns precede ALL state writes: the first assignment in `configureMP3` is `WCB_MP3.cpp:341` (`if ((uint32_t)baudRate != baudRates[serialPort-1]) updateBaudRate(...)`), and `mp3Config` itself is written later still. So the entire `?MP3,S<p>:<b>:V<v>` line built at `Wizard/parser.js:1364` is discarded — port and volume included. The reviewer's "whole line dropped" claim is exactly right.
- `Wizard/app.js:2230-2243` (the cited :2236) uses `> 57600`, and `populateUIFromConfig` repeats it at `app.js:3046-3054`. Confirmed.
- Contrast proves intent was to mirror the firmware: HCR's select (`index.html:861-866`) offers exactly the five values `WCB_HCR.cpp:631-633` accepts, and `_hcrApplyBaudCap` (`app.js:2468-2491`) uses `> 9600` matching HCR's own soft-serial guard. MP3 got the Maestro-shaped 57600 ceiling (`WCB_Maestro.cpp:643`) applied to a subsystem with a stricter rule.

REACHABILITY — real, and the reviewer understated it. No guard exists at any call site. The simplest repro needs no port move at all: MP3 on S1/S2 with any of the 11 non-{9600,38400} options — the Wizard applies NO clamp whatsoever on hardware serial — is rejected at `WCB_MP3.cpp:287`. The reviewer's 38400→S3 scenario is also valid (`onMP3PortChange` leaves 38400 because 38400 ≤ 57600; `syncMP3ToConfig:2304` re-reads the DOM at push time and ships it) and hits `:292`.

INTENT — not deliberate. `git log -L 2225,2250:Wizard/app.js` shows the block arrived whole in `ee11659 "updates for adding mp3 trigger."` with no explanatory comment. The tooltip at `index.html:808` ("Serial 3–5 are capped at 57,600") is wrong in the same direction. Nothing in `docs/` or `CLAUDE.md` sanctions it.

CONSEQUENCE — this is where the finding overstates, and it splits by push path (the reviewer did not check either):
- DIRECT-USB push (`boardGo` → `app.js:7418`): an MP3-only change is NOT in `commandStringNeedsReboot` (`app.js:8185-8196` — no MP3 clause), so it takes the no-reboot branch at `app.js:7564-7580`, whose comment is explicit: "Do NOT advance the baseline optimistically; let boardPull() set it from the board's real post-push state so any dropped setting stays a visible pending diff instead of being masked as 'No changes to push'." It then fires `boardPull(n)` after 300 ms, which sets `boardConfigs[n] = config` + fresh baseline and calls `populateUIFromConfig` (`app.js:6918`) — so the MP3 block visibly reverts to unconfigured and the diff reappears. The firmware's rejection text also reaches the terminal: `_suppressTerminalLine` (`app.js:8781-8791`) has no rule matching `[MP3]` or the BLOCKED banner. So on the dominant path it is neither silent nor sticky — just a misleading green toast.
- RELAYED/MGMT push (`boardGoRemote`, `app.js:7769`, entered from `boardGo` at `app.js:6993` whenever `remoteRelayForBoard[n]` is set): success toast at `:7869`, then `boardBaselines[n] = JSON.parse(JSON.stringify(config))` at `:7873` with NO verify pull anywhere in that function or its caller. Baseline poisoned → the MP3 line is never re-emitted on any later diff push. This is the genuinely silent, sticky path, and it is the strongest part of the finding.

EXTRA (reviewer missed): `syncMP3ToConfig:2308-2310` mirrors `mp3.baud` into `config.serialPorts[port-1].baud`, and `buildCommandString` emits `BAUD,S<p>,<b>` at `parser.js:1314` BEFORE the MP3 line at `:1364`. `updateBaudRate` (`WCB_Storage.cpp:146-180`) accepts all 13 values on all 5 ports with no soft-serial cap. So the port baud IS applied while the MP3 config is rejected — S3 really ends up running at 38400 with no MP3 configured. Half-applied, not merely no-op.

SEVERITY S2 → S3. Real, reachable, user-facing, and the UI actively misinforms (the tooltip promises 57,600 works). But: the pre-selected default 9600 is valid; the MP3 Trigger's own hardware only does 9600/38400 so a user reading MP3TRIGR.INI picks a valid one; on direct USB the error prints to the terminal and the verify pull un-does the false UI within ~300 ms; and recovery is picking 9600 and re-pushing. One subsystem, one board, no data loss, no crash, no mesh impact. That is S3.

</details>

### `Wizard/app.js:2305` — CONFIRMED · S2 → **S3**

**syncMP3ToConfig re-reads the raw MP3 volume input, discarding onMP3VolChange's clamp — an out-of-range value makes the firmware reject the whole MP3 config**

**Correction to the finding as filed:**

The defect is real and the mechanism is right; three corrections.

(1) Severity is S3, not S2. The baseline is deliberately not advanced on push (`Wizard/app.js` ~:7554, "Do NOT advance boardBaselines optimistically"), so the bad value is not sticky across a re-pull, and the firmware's "[MP3] Volume must be 0-64" reply is echoed into the board terminal by the send loop at `Wizard/app.js:7484-7492`. The real gap is that the push still toasts success because an error line counts as a response (`r.ok` true) — a feedback bug layered on a typo, not silent unrecoverable breakage.

(2) The `??`/NaN sub-claim is correct in JS but harmless in practice: `MP3,S2:9600:VNaN` reaches `Code/WCB/WCB_MP3.cpp:279`, where `String("NaN").toInt()` returns 0, which passes the `:305` range check as volume 0 — the same value `onMP3VolChange` stores. It should still be fixed, but it is not a second failure mode.

(3) The finding understates one reach path that actually makes the primary fix location the right one: loading a saved system JSON writes config→DOM at `Wizard/app.js:3056` without ever firing `onMP3VolChange`, so for an imported or hand-edited config `syncMP3ToConfig` is the only possible clamp point. Fixing only `onMP3VolChange` would leave that path open.

<details><summary>Full verification reasoning</summary>

I opened every file the finding depends on and re-derived each fact rather than trusting the transcription.

**Both code snippets are transcribed accurately.**
- `Wizard/app.js:2259-2266` — `onMP3VolChange` reads the DOM, `if (isNaN(v)) v = 0;`, `v = Math.max(0, Math.min(64, v));`, `config.mp3.volume = v;`, then `onBoardFieldChange(n)`. It never writes `v` back to the input.
- `Wizard/app.js:2305` — `config.mp3.volume = parseInt(document.getElementById(`b${n}-mp3-vol`)?.value) ?? 0;` inside `syncMP3ToConfig`'s `if (config.mp3.enabled)` branch. No clamp. Exactly as claimed.

**The clamp really is discarded.** `onBoardFieldChange` (`Wizard/app.js:1483-1486`) is only `_notifyBoardChanged` + `updateBoardStatusBadge(n,'unsaved')` — it does not re-render, so the only writer that could push the clamped value back into the DOM (`Wizard/app.js:3056`, `volEl.value = config.mp3.volume ?? 0`, inside `renderBoardConfig`) does not run. The input keeps the raw text. `Wizard/index.html:827` is `<input type="number" id="b{N}-mp3-vol" min="0" max="64" value="0" onchange="onMP3VolChange({N})">` — `min`/`max` on a number input only affect the spinner and `:invalid`; a typed "200" is still returned by `.value`. I grepped `Wizard/app.js` and `Wizard/index.html` for `checkValidity|reportValidity|:invalid` — **zero hits**, so nothing anywhere gates the push on validity.

**Reachability confirmed.** `syncMP3ToConfig` is called at `Wizard/app.js:7391` (USB `boardGo`) and `:7783` (`boardGoRemote`), on every push, before `WCBParser.buildCommandString` at `:7418`. `Wizard/parser.js:1364` emits `MP3,S${port}:${baud}:V${volume}` with no clamp — I grepped `Wizard/parser.js` for `Math.min|Math.max`: **zero hits**. The only `Math.min(64, …)` in the whole Wizard is `app.js:2264`, the one being overwritten.

**Firmware rejection confirmed at the cited line.** `Code/WCB/WCB_MP3.cpp:279` `int volume = volStr.substring(1).toInt();` then `:305-308`:
```cpp
if (volume < 0 || volume > 64) {
  Serial.println("[MP3] Volume must be 0-64 (0=loudest, 64=inaudible)");
  return;
}
```
This `return` precedes all of `:359` `mp3Config.volume`, `:360-362`, and `mp3BindCodec()` at `:366`, so port and baud are discarded too — the MP3 Trigger is left entirely unconfigured, not merely at a wrong volume.

**The `??` sub-claim is also correct but milder than implied.** `parseInt('')` is `NaN` and `NaN ?? 0` is `NaN`, so an emptied field yields `volume: NaN` → `MP3,S2:9600:VNaN`. But Arduino `String("NaN").toInt()` is 0, and 0 is in range, so the firmware *accepts* it as volume 0 — same value `onMP3VolChange` would have stored. The `JSON.stringify`→`null` export self-heals on reload via `config.mp3.volume ?? 0` at `app.js:3056`. So the NaN path is cosmetic, not the real defect; the out-of-range path is.

**Not deliberate — the opposite.** `git log -L` shows `syncMP3ToConfig`'s volume line has been unclamped since `ad58a19`, while commit `3d74fa8` ("Wizard: DFPlayer config + unified Audio section") — which touched this very function — added the *correct* pattern to its sibling at `Wizard/app.js:2447-2449`:
```js
let v = parseInt(document.getElementById(`b${n}-dfp-vol`)?.value);
if (isNaN(v)) v = 15;
config.dfp.volume  = Math.max(0, Math.min(30, v));
```
Same author, same commit, clamped in the sync function. MP3 was simply not brought along. No comment anywhere defends the asymmetry.

**Second, unmentioned reach path that strengthens it:** loading a saved system JSON goes `config → DOM` at `app.js:3056` without ever firing `onMP3VolChange`, so for a hand-edited or corrupted config file `syncMP3ToConfig` is the *only* possible chokepoint — and it has no clamp. Fixing `onMP3VolChange` alone would not cover this; the reviewer's primary fix (in `syncMP3ToConfig`) does.

**Why I downgrade S2 → S3.** The reviewer overstates the blast radius on two counts I checked:
1. The baseline is **not** poisoned. `app.js` ~:7554 carries the comment "Do NOT advance boardBaselines optimistically — the post-reboot [pull refreshes it]", so a re-pull picks up the firmware's real (unconfigured) MP3 state and the next push retries. The misconfiguration is not sticky.
2. The user does get diagnostics: the firmware's `[MP3] Volume must be 0-64` reply is echoed into the board terminal by the ACK-paced send loop (`app.js:7484-7492`), and because an error line *is* a response, `r.ok` is true, so the push toast still says success — that mismatch is the genuine aggravating factor, but it is a UX gap on top of a self-inflicted typo, not silent unrecoverable breakage.

Normal in-range use is completely unaffected; the trigger is a user typing outside a field that is labelled `min="0" max="64"` with a tooltip stating the range; recovery is one corrected digit and a re-push. Real bug, correct mechanism, wrong severity band.

</details>

### `Wizard/app.js:3246` — CONFIRMED · S2 → **S3**

**Maestro ID dropdown offers 9, which the firmware rejects as a reserved routing target**

**Correction to the finding as filed:**

Severity S2 → S3, and the fix scope needs correcting on two points.

1. The parenthetical "the Kyber-target row builder at app.js:8953 … use the same 1..9 expression and should be checked" is factually wrong about matching the firmware. `appendKyberTargetRow` (app.js:8944, dropdown at :8953) feeds `?KYBER,LOCAL`, whose parser accepts **1-9** (Code/WCB/WCB_Storage.cpp:1248), not 1-8. Tightening it to 8 is still worth doing, but for the opposite reason the finding implies — not to "match the firmware", but to close the vector into the wave2 slot-shadowing bug.

2. The finding did not enumerate `app.js:2679`, the fourth `Array.from({length: 9}, ...)` in the file. That one is the **WLED** ID dropdown, and it is CORRECT: `Code/WCB/WCB_WLED.cpp:270` accepts 1-9 (`if (wledID < 1 || wledID > 9)`), as do `:216` and `:418`. A grep-and-replace across all four `{length: 9}` sites would break WLED ID 9.

Correct scope: change `{length: 9}` → `{length: 8}` at app.js:3246 and app.js:10151 (setup-wizard Maestro row, same `?MAESTRO` wire path). Leave app.js:2679 alone. Optionally tighten app.js:8953 as defence-in-depth against the id-9 Kyber slot.

One wrinkle the fix should handle: `populateMaestrosFromConfig` (app.js:3459-3465) renders parsed board state, and `parser.js:794` accepts any `M(\d+)`. A board carrying a stale id-9 slot (creatable today via the Kyber path) would render a select with no matching option, the browser selects option 1, and `syncMaestrosToConfig` (app.js:3424) then silently rewrites it to M1 on the next push. Better than the status quo, but worth either range-filtering in `parser.js:794` or surfacing a warning rather than coercing.

The real durable fix is firmware-side: change WCB_Storage.cpp:1248 to `maestroID >= 1 && maestroID <= 8` so both config paths enforce the invariant WCB_Maestro.h:14-16 declares.

<details><summary>Full verification reasoning</summary>

I re-derived every load-bearing fact from source rather than the summary.

OBSERVATION — stands, verbatim. `Wizard/app.js:3237` `appendMaestroRow(n, maestro, readOnly=false)`; `:3246` is literally `const idOptions = Array.from({length: 9}, (_, i) => i + 1).map(v =>` and it is rendered into `<select id="${rowId}-id" ... onchange="onMaestroChange(${n})">` at `:3267`. Default from `addMaestroRow` is `{ id: 1, ... }` (`:3234`), so 9 requires an affirmative user pick — but it is offered.

FIRMWARE CONTRACT — confirmed and stricter than the reviewer showed. `Code/WCB/WCB_Maestro.cpp:578-581` is exactly as quoted (`if (maestroID < 1 || maestroID > 8)` → print → `startIdx = nextComma + 1; continue;`). Three more enforcers agree: `:724` (`maestroAutoAddRemote`), `:817`, `:844`. The invariant is stated in `Code/WCB/WCB_Maestro.h:14-16` — "Maestro device IDs are 1-8; id 9 (all local) and id 0 (all Maestros) are RESERVED routing targets, never stored as a device." `sendMaestroCommand`'s id-9 fan-out is at `:190`, structurally *after* `if (handled) return;` at `:158`. The Wizard's own tooltip contradicts its own dropdown: `Wizard/index.html:699` says "Maestro device ID 1-8" (reviewer said 697 — off by two).

REACHABILITY — traced end to end, no guard anywhere. `syncMaestrosToConfig` reads `[id$="-id"]` and pushes unfiltered at `app.js:3424` (`config.maestros.push({ id, port, baud })`, guarded only by `if (id && port)`); `rebuildMaestroRoutingTables` copies it into `all[]` at `:3355` and into **every** non-client board's `cfg.maestroTable` at `:3364`; `parser.js:1455` reads `config.maestroTable` and `:1477` emits `.map(m => \`M${m.id}:W${m.wcb}S${m.port}:${m.baud}\`)` into `add(\`MAESTRO,${chain}\`)` at `:1479`. No range filter on any hop. The pull side is equally unfiltered: `parser.js:794` matches `/^M(\d+):W(\d+)S(\d+):(\d+)$/` with no range check. Setup-wizard path is the same defect: `app.js:10151` builds 1..9 and `:10990` / `:11128` feed it into `cfg.maestroTable`.

INTENT — not deliberate. `git log -L 3246,3248:Wizard/app.js` → commit `5687b16` "updates to web" (2026-02-27) replaced `Array.from({length: maestroQty}, ...)` (maestroQty from `#g-wcbq`, default 8) with hardcoded `{length: 9}`. No comment, no doc, nothing in CLAUDE.md or docs/ defends 9 here — and CLAUDE.md rule 5 states the opposite ("IDs 1-8 are devices; id 9 … reserved").

WHY S3, NOT S2 — the reviewer overstated the plain path. The `?MAESTRO` loop `continue`s past only the bad entry, so the rest of the chain is stored normally; there is no collateral loss, no corruption, and the board prints an explicit reason. The user must pick an option the adjacent tooltip already says is out of range, and the phantom row vanishes on the next pull. That is a UI/firmware contract mismatch with silent partial config loss — real, but not S2.

WHAT THE REVIEWER MISSED, AND IT CUTS BOTH WAYS — on a Kyber-local board this is genuinely worse than described. `app.js:3365-3367` also copies the same list into `cfg.kyber.targets` when `cfg.kyber.mode === 'local'`, and `parser.js:1349` emits it as `?KYBER,LOCAL,S<port>,M<id>:W<wcb>S<port>:<baud>`. `storeKyberSettings` accepts **1-9** (`Code/WCB/WCB_Storage.cpp:1248`) and persists a slot with `maestroConfigs[maestroSlot].maestroID = maestroID` (`:1283`). A stored id-9 slot then matches the loop in `sendMaestroCommand` and returns at `WCB_Maestro.cpp:158` before the id-9 branch at `:190` — permanently shadowing the "all local Maestros" fan-out, across reboots. That firmware half is already logged separately as docs/CODE_REVIEW_2026-08_wave2.md:1197 (no verdict recorded yet); this dropdown is the Wizard-side vector into it.

FIX — the core change works, but the finding's parenthetical is wrong on one of the two sites it names, and a naive sweep of all four 1..9 builders would introduce a new bug (see correction).

</details>

### `Wizard/app.js:3594` — CONFIRMED · S2 → **S3**

**syncMappingsToConfig() wipes config.pwmOutputPorts and never repopulates it**

**Correction to the finding as filed:**

Mechanism and location are right (Wizard/app.js:3594; sole other producer Wizard/parser.js:885-887). Three corrections:

1. The claimed failure overstates the immediate UI effect. `syncMappingsToConfig` touches no DOM, so the ghost row does NOT disappear at that moment — it stays on screen, now contradicting the serial-port row, which has silently unlocked (baud enabled, "Managed by PWM" note gone, port re-offered in the Maestro/WLED/HCR/MP3/DFP dropdowns). The ghost row vanishes for good only on the next `refreshPWMGhostRows(n)` (app.js:4017-4022), which fires when the SOURCE board is pulled (app.js:4082), because `appendPWMOutputGhostRows` early-returns on the now-empty array (app.js:4027).

2. Two consequences the reviewer missed. (a) `buildSystemFile` calls `buildCommandString` with `fullPush = true` (parser.js:1573, :1582), and the PWM OUT emitter is `parser.js:1498-1505` — so a config file saved after any mapping edit silently omits every `?MAP,PWM,OUT,S<port>` line. (b) `applyBidirMapping` calls `syncMappingsToConfig(wcb)` on the DESTINATION board (app.js:3725) and `_removeBidirRows` on any matching board (app.js:3675), so the wipe reaches boards the user is not editing.

3. Severity is S3, not S2. Nothing wrong is sent to any board: `pwmChanged` (parser.js:1498) goes true but the loop over the empty array emits nothing, and the real `?MAP,PWM,CLEAR,OUT,S<n>` (app.js:3897) is computed from `boardBaselines[n]` — deliberately, per the comment at app.js:3821-3825 — so no spurious clear is triggered. The state self-repairs on any pull, since `boardPull` replaces the entire config object (app.js:6809, :6879) and the firmware always re-emits the declaration (WCB.ino:2919-2920).

Unrelated dead code worth cleaning in the same edit: app.js:3588-3591 and 3615-3617 both set/clear `claimedBy` and are then completely overwritten by `evaluatePortClaims`, which resets every claim at parser.js:987-989 before re-deriving. The 3615-3617 block is also wrong on its face — it stamps `{type:'pwm'}` on the source port of every mapping row including Serial ones.

<details><summary>Full verification reasoning</summary>

MECHANISM — verified line by line, not from the summary.

`Wizard/app.js:3584-3622` is the whole of `syncMappingsToConfig()`. Line 3594 is exactly `config.pwmOutputPorts = [];`. The rebuild loop that follows (3599-3618) writes only `config.mappings.push(...)` (3614) and `config.serialPorts[..].claimedBy` (3616). Nothing in the function body ever writes `config.pwmOutputPorts`. Confirmed by grep of the entire Wizard — the only writers anywhere are:
- `Wizard/parser.js:136` — `pwmOutputPorts: []` in `createDefaultConfig()`
- `Wizard/parser.js:885-887` — `if (!config.pwmOutputPorts.includes(port)) config.pwmOutputPorts.push(port);` in the `?MAP,PWM,OUT,S<n>` parse case
- `Wizard/app.js:3594` — this clear
So the reviewer's core observation is factually correct: after any call to `syncMappingsToConfig`, the array is empty and stays empty until a fresh pull. It IS repaired by a pull — `boardPull` builds a brand-new object (`const config = WCBParser.parseBackupString(raw)`, app.js:6809) and assigns `boardConfigs[n] = config` (e.g. app.js:6879, :6890), and the firmware always re-emits it (`Code/WCB/WCB.ino:2919-2920`: `emit("MAP,PWM,OUT,S" + String(pwmOutputPorts[i]), true)`).

CONSEQUENCE 1 — port claim released (real). `evaluatePortClaims()` at app.js:3620 starts by wiping every claim (`parser.js:987-989`), then re-derives. Its PWM-output block is `parser.js:1061-1067`, iterating `config.pwmOutputPorts` — now empty, so no claim. `updatePortClaimUI(n)` (app.js:3621 → :764) then takes the `!claim` branch (app.js:782-787): claim note cleared, baud select re-enabled, broadcast checkboxes re-enabled. Concretely the port stops being filtered out of every device dropdown, which all reject `claim.type === 'pwm'`: Maestro (app.js:3294, `heldByOtherFeature = claim && claim.type !== 'maestro'`), and the same "unclaimed or mine" rule at app.js:2285 (MP3), :2426 (DFP), :2500 (HCR), :2719 (WLED). So the user can now assign a device to a UART a remote board is driving with PWM. Note `refreshMappingSourceDropdown` (app.js:3532-3536) explicitly allows `pwm`-claimed ports anyway, so mapping sources are unaffected.

CONSEQUENCE 2 — backup file loses the declaration (real, and the reviewer did not mention it). `buildCommandString` emits PWM OUT at `parser.js:1498-1505`; `buildSystemFile` calls it with `fullPush = true` (`parser.js:1573`, `:1582`). Saving a config file after any mapping edit silently drops every `?MAP,PWM,OUT,S<port>` line.

CONSEQUENCE 3 — cross-board reach (reviewer did not mention). `applyBidirMapping` calls `syncMappingsToConfig(wcb)` on the DESTINATION board (app.js:3725), and `_removeBidirRows` calls it for any board with matching rows (app.js:3675). So editing bidir on WCB2 can wipe WCB3's `pwmOutputPorts`.

NOT DESTRUCTIVE ON THE WIRE — I checked this specifically. `pwmChanged` at parser.js:1498-1500 does go true (baseline `[4]` vs config `[]`), but the body only iterates `config.pwmOutputPorts`, so it emits nothing — there is no "clear" branch. The real clear command `?MAP,PWM,CLEAR,OUT,S<n>` is produced only at app.js:3897, and it is derived from `oldRemotePWMDests`, which app.js:3821-3825 deliberately reads from `boardBaselines[n]` — with an explicit comment saying it must not read the live config *because* `syncMappingsToConfig` has already run. So no spurious clear reaches any board.

REACHABILITY — real user, no exotic state. `syncMappingsToConfig` is called from `onMappingChange` (app.js:3572), `onBidirChange` (:3579), `removeMappingRow` (:3635), `_removeBidirRows` (:3675), `applyBidirMapping` (:3725), `saveMappingRow` (:3827). `onMappingChange` fires on every field change of any mapping row, long before Save. The precondition — a non-empty `pwmOutputPorts` — is normal: `Code/WCB/WCB_PWM.cpp:326-330` has the source board unicast `%cMAP,PWM,OUT,S%d` to each remote destination automatically. Nothing wipes it on load: `detectBidirMappings` (app.js:3732-3767) only sets checkbox state and never calls sync, so a pull leaves the array intact until the first edit.

INTENT — not deliberate. `git log -S "config.pwmOutputPorts = []"` returns a single commit, `3a84620 "workign on wizard"` — a WIP commit, no explanatory comment on or near the line. The only comment in the function is "Release pwm claims" (app.js:3588), which refers to the claimedBy loop above, not to this array. No doc in docs/ or CLAUDE.md sanctions it.

SEVERITY — real, but S3 not S2. It is Wizard-only view state; the board is never told anything wrong; any pull fully repairs it (and the Wizard auto-pulls at app.js:3866 / :3878 / :7595 after mapping saves); and the backup-file omission is itself recoverable because restoring the source board's PWM mapping makes the firmware re-send OUT (WCB_PWM.cpp:326-330). The worst realistic outcome is a user assigning a device to a port a remote board drives, which the next pull exposes.

FIX — correct as proposed. `pwmOutputPorts` is a board-declared fact received over ESP-NOW, not derived from the mapping DOM, so deleting line 3594 is right. No shrink path is lost: the parser dedupes on insert (parser.js:885), a pull replaces the whole config object so stale entries cannot survive, and local-destination PWM claims come from a different code path (parser.js:1049-1057, iterating `mapping.destinations` with `wcbNumber === 0`) that is unaffected. Deleting is cleaner than snapshot/restore.

</details>

### `Wizard/app.js:3866` — CONFIRMED · S2 → **S3**

**PWM mapping Save schedules a config pull at 2 s, but the board blocks 3 s and reboots**

**Correction to the finding as filed:**

Keep the finding, fix three things in the write-up.

1) Mechanism: it is not "the board services no serial input for 3 s". `serialCommandTask` (WCB.ino:6595) keeps reading and enqueuing during the `delay(3000)` (vTaskDelay yields); it is loop() (WCB.ino:7503-7512) that is blocked, so the enqueued `WCB_WEBTOOL_CONFIG_PULL` is never drained and dies with `ESP.restart()`. Cite WCB_PWM.cpp:339-343, not 338-342.

2) Symptom: "The board card flips to ✕ Pull Failed" is false on the direct-USB path the scenario describes. `updateBoardStatusBadge(...,'error')` exists only in `remoteBoardPull` (app.js:7950/8006/8024/8047). `sendAndCollect` (app.js:5232) resolves on timeout and `parseBackupString` returns a full DEFAULT config on unparseable text (verified under node), and boardPull has no sentinel guard between :6804 and :6809 — so a failed pull takes the SUCCESS branch at :6914-6923 and silently overwrites boardConfigs[n]/boardBaselines[n] with factory defaults while toasting "Config pulled from WCB n" in green. Rewrite the scenario as: the mapping card and the rest of the board card blank back to defaults with a success toast, and the board's real config is only recoverable by a manual pull.

3) Likelihood: state that the pull often self-heals. The tier-2 `?backup` at t≈5 s lands within a few hundred ms of the firmware's boot-time `while (Serial.available()) Serial.read();` (WCB.ino:7122, ~t≈4.4-4.7 s); after the flush it survives the 2 KB RX ring and the pull completes ~7 s late. This is a race, not a guaranteed failure.

Recommended fix beyond the reviewer's: also harden boardPull so a no-sentinel response cannot clobber state — e.g. after the tier-2 collect, `if (!raw.includes('End of Backup')) throw new Error('pull timed out — no backup received');` before `parseBackupString` at app.js:6809. That is the line that turns every failed pull anywhere in the tool into a silent factory-defaults overwrite, and it is worth its own entry.

<details><summary>Full verification reasoning</summary>

MECHANISM — the core claim holds, but one detail is wrong.

- `Wizard/app.js:3866` is verbatim `setTimeout(() => boardPull(n), 2000);` and it is unconditional; the `type === 'PWM'` branches only start at :3871 and :3884. Reachable from the Save button's inline onclick, `Wizard/app.js:3510` (`onclick="saveMappingRow('${rowId}',${n})"`), the only caller; the type `<select>` at :3490-3491 offers Serial/PWM, so a user reaches it trivially.
- Firmware: `Code/WCB/WCB.ino:4542` is `addPWMMapping(pwmConfig);` (one arg) after falling through the LIST / CLEAR,ALL / CLEAR,OUT,S / CLEAR,S / OUT,S branches; `Code/WCB/WCB_PWM.h:38` is `void addPWMMapping(const String &config, bool autoReboot = true);`. Tail is `Code/WCB/WCB_PWM.cpp:339-343` (reviewer wrote 338-342 — off by one, same slip already flagged at docs/CODE_REVIEW_2026-08_verdicts.md:534): `if (autoReboot) { Serial.println("Rebooting in 3 seconds to apply PWM configuration..."); delay(3000); ESP.restart(); }`. No caller passes false (only WCB.ino:4542 and :5254, both one-arg).
- The reviewer's mechanism sentence "the board services no serial input for 3 s" is WRONG. Serial is read by `serialCommandTask` (WCB.ino:6595, pinned core 1) → `processIncomingSerial` (:6361) → `processSerialCommandHelper` → `enqueueCommand`, while `handleSingleCommand` (which handles `WCB_WEBTOOL_CONFIG_PULL`, :4374) runs only from the loop-task queue drain (:7503-7512). `delay()` is `vTaskDelay`, so the serial task keeps reading and enqueuing during the 3 s; it is loop() that is blocked, and `ESP.restart()` then destroys the queue. Same net result: the pull sent at t≈2 s is never answered. Conclusion right, mechanism detail wrong.
- Reviewer is right that the SERIAL path does not reboot: `addSerialMonitorMapping` (WCB_Storage.cpp:1593) contains no `ESP.restart()`/`delay()` (checked by awk over the whole function body). Also verified `addPWMOutputPort` (WCB_PWM.cpp) does not reboot, so the remote-destination pull at app.js:3878 (4 s) is fine.

INTENT — not a deliberate trade-off. The 10 s clear-path pull with its explicit comment ("Board reboots ~3 s after receiving the clear then takes ~4-5 s to boot; pull after 10 s.", app.js:3920-3921) landed in commit 5b438bd; the 2 s pull at :3864-3866 landed LATER in 02cc8ed (blob chain 6fcaa42→0a62e09→7c5c37d). The Wizard also already encodes this knowledge in `commandStringNeedsReboot` (app.js:8185-8196) with `/MAP,PWM,S\d/i` at :8194 and the comment "firmware auto-reboots, but we signal it too". So the file knows; this line just doesn't consult it. Oversight, not intent.

THE CLAIMED SYMPTOM IS FALSE — this is the main correction.
- `updateBoardStatusBadge(n,'error')` ("✕ Pull Failed", defined app.js:3210) is never called from `boardPull`. Its only four call sites (7950, 8006, 8024, 8047) are all inside `remoteBoardPull` (7900), the relay path. `boardPull`'s catch (6925-6932) only toasts + termLogs.
- `boardPull` cannot even reach that catch on a timeout: `sendAndCollect` (app.js:5232) resolves — never rejects — on timeout (`timer = setTimeout(finish, timeoutMs)` → `resolve(lines.join('\n'))`), and there is NO sentinel guard before the parse (6800-6809: tier-1 3 s, tier-2 `?backup` 8 s, then straight into `parseBackupString(raw)`). I ran Wizard/parser.js under node against `''` and a synthetic boot banner: both return a full DEFAULT config (wcbNumber 1, mappings [], equalsDefault true) with only a console.warn. So a genuinely failed pull takes the SUCCESS branch at 6914-6923 — `boardConfigs[n]` and `boardBaselines[n]` overwritten with factory defaults, `populateUIFromConfig`, badge 'connected', green "Config pulled from WCB n". Silent clobber, not a red badge.
- On the relay path (`boardPull` delegates at :6766 → `boardPullRemote` → `remoteBoardPull`), MAX_PULL_ATTEMPTS=3 / PULL_TIMEOUT_MS=6000 / PULL_RETRY_MS=2500 (7893-7895) means attempt 2 fires at ~t=10.5 s, by which time the board is back — it self-heals with a transient "↻ Retrying…".

WHETHER IT EVEN FAILS — a genuine race I cannot settle from the repo.
`boardPull`'s tier-2 fallback sends `?backup` at t≈5.0 s with an 8 s window (to t≈13 s). Board restarts at t≈3.15 s; `setup()` does `Serial.setRxBufferSize(2048); Serial.begin(115200); delay(1000); while (Serial.available()) Serial.read();  // flush startup junk` (WCB.ino:7119-7122), i.e. the flush lands ~t≈4.4-4.7 s. If the tier-2 command arrives after that flush it survives in the 2 KB ring, is read once serialCommandTask starts (+500 ms, :6596), and the full backup prints well inside the window — the pull SUCCEEDS, just ~7 s late. Margin is only a few hundred ms and shrinks by up to 500 ms from the `delay(50)` per remote output/clear in addPWMMapping (WCB_PWM.cpp:305, :322) plus NVS commit time. Settling which side wins needs a bench test (save a PWM mapping with 1 and with 5 remote destinations, watch the terminal for whether `?backup` is answered).

NET: the defect is real — a pull is deliberately scheduled into a window where the board provably cannot answer, on a path whose own file gets it right 55 lines below. But the consequence is either a ~7 s-late successful refresh or a silently defaults-clobbered UI card, not the claimed "✕ Pull Failed", and nothing wrong is written to the board. That is S3, not S2 (compare the S2 in docs/CODE_REVIEW_2026-08_verdicts.md:554, which is actual NVS data loss reported as success).

</details>

### `Wizard/app.js:4092` — CONFIRMED · S2 → **S3**

**seqValueToLines/seqTextareaToValue are not inverses: '^^***' standalone comments collapse, and a plain Push rewrites stored sequences**

**Correction to the finding as filed:**

Severity should be S3, not S2. Four corrections to the writeup:

1. The defect is display-only. The finding never states the impact ceiling, and the mechanism proves it is low: WCB_Storage.cpp:582-585 cuts each delimiter-separated part at `***` and drops empty parts, so `;M11^^***note`, `;M11^***note` and `;M11***note` all execute as exactly `;M11`. No command is lost or altered; only the comment's line placement in the editor changes, plus at most two silent no-op SEQ,SAVE writes.

2. "The firmware deliberately preserves the distinction — normaliseSeqComments()" is true but overstated. That function's only callers are WCB_Storage.cpp:892 and :918, both inside migrateOldStoredCommands (a one-time legacy-namespace migration). A normal `?SEQ,SAVE` stores the value verbatim with no normalisation. `^^***` therefore only reaches a board via legacy migration, a hand-typed terminal save, or a restored backup — never from the Wizard, which narrows the affected population considerably.

3. The second LOSSY line the finding reports (`";M11^***inline note" => ";M11***inline note"`) should be dropped from the finding, not fixed. That fold is deliberate and documented at app.js:4117-4118, and it produces exactly what the firmware's own normaliseSeqComments does at WCB_Storage.cpp:861 (`^*** -> ***`). Fixing it would put the Wizard at odds with the firmware.

4. A more reachable symptom of the same root cause is missing: a user with no legacy data at all can type a standalone comment line in the Wizard textarea today; it is stored as `^***` (single delimiter) and re-displays glued to the previous command on the next pull. Verified: ";M11\n*** raise the dome\n;t500" -> ";M11^*** raise the dome^;t500" -> ";M11 *** raise the dome\n;t500". The editor offers no way to author a standalone comment that survives a round trip — that, rather than the legacy `^^` corpus, is the everyday version of this bug and is what makes the fix worth doing.

<details><summary>Full verification reasoning</summary>

MECHANISM — verified by execution, not by reading. I extracted the two functions verbatim from Wizard/app.js (seqTextareaToValue at :4091-4096, seqValueToLines at :4127-4174) into node and ran them. Results:

  ";M11^^***standalone note^;t500^;M12" -> lines ";M11\n***standalone note\n;t500\n;M12" -> back ";M11^***standalone note^;t500^;M12"   LOSSY
  ";M11^*** raise the dome^..."         -> lines ";M11 *** raise the dome\n..."          -> back ";M11*** raise the dome^..."            LOSSY
  ";M11***inline^;M12"                  -> round-trips exactly
  "***leading note^;M11"                -> round-trips exactly (guarded by the `lines.length === 0` branch at :4146)

So the asymmetry is real and exactly where the reviewer put it: the reader consumes the empty segment (`prevWasEmpty` set at :4141, consumed at :4146) and the writer's `.filter(Boolean)` at :4094 can never regenerate one. The header comment at :4116-4121 does say `^^***` is the standalone marker, so reader and writer genuinely disagree with each other.

The reviewer's transcription of the header comment is slightly off ("(double delimiter before ***)" is their paraphrase; the file says ":4119-4120  ^^***text — standalone comment (double delimiter before ***): the empty segment produced by ^^ signals 'own line'..."), but the substance is accurate.

PUSH PATH — confirmed end to end. `getSequencesFromUI` (app.js:4488-4496) calls seqTextareaToValue at :4493; `config.sequences = getSequencesFromUI(n)` runs on the direct push at app.js:7397 and the relay push at :7791. parser.js:1527-1533 diffs per key by exact string equality (`baseSeqMap.get(seq.key) !== seq.value`) and emits `SEQ,SAVE,<key>,<value>`. The baseline is a deep copy of the *parsed board config* (app.js:6917 `boardBaselines[n] = JSON.parse(JSON.stringify(config))`), i.e. it holds the raw `^^` value. So an untouched sequence does diff and does get re-pushed. The value survives transport intact: WCB.ino:2046-2077 gives `?SEQ,SAVE,` its own branch that swallows everything up to the next `^` + funcChar, and both Wizard extraction paths preserve `^^` (parser.js:316 splits on newlines for board backups; :401 splits on `^?` for chained). The two-step degradation is real and terminates: I ran the second cycle and got ";M11*** raise the dome^..." which then round-trips exactly, and app.js:7578 re-pulls after each push (resetting the baseline at :6917), so it is exactly two spurious pushes, not a loop.

REACHABILITY — real but narrower than implied. `^^***` never comes from the Wizard. It reaches a board from (a) legacy migration, (b) a hand-typed `?SEQ,SAVE` at the terminal, (c) a restored backup/system file. The reviewer cites normaliseSeqComments as proof the firmware "deliberately preserves the distinction" — that is true (WCB_Storage.cpp:849 "^^*** (standalone) is left untouched", implemented :856-862) but it is a *migration-only* function: its only two callers are inside migrateOldStoredCommands (WCB_Storage.cpp:892 and :918). An ordinary `?SEQ,SAVE` does no normalisation at all.

WHY THIS IS NOT S2 — the drift has provably zero effect on what the droid does. recallCommandSlot (WCB_Storage.cpp:568-589) splits on the delimiter, cuts each part at `commentDelimiter` (:582-583) and drops empty parts (:585). So `;M11^^***note`, `;M11^***note` and `;M11***note` all execute as exactly `;M11`. Nothing is truncated, no command is lost, no NVS key is at risk (it overwrites an existing key). The entire user-visible damage is that a comment moves from its own line to the end of the previous line, plus at most two silent no-op `SEQ,SAVE` writes. That is a real defect and it silently rewrites stored user text without being asked, which is why I keep it above a nit — but it cannot break a board, so S2 overstates it.

INTENT — I checked. The inline fold at :4150-4151 is deliberate and documented (:4117-4118), and matches what the firmware itself does at WCB_Storage.cpp:861 (`^*** -> ***`), so the reviewer's *second* LOSSY line is not a bug and must not be "fixed". Only the `^^` case is an unintended gap. git log -L on the region shows seqTextareaToValue was written first (commit 4101c69) and never revisited when the `^^`/prevWasEmpty reader logic was added — an omission, not a trade-off.

</details>

### `Wizard/app.js:5541` — CONFIRMED · S2 → **S3**

**A relay's transient USB drop tears down every relay-managed remote board and the successful reconnect never rebuilds them**

**Correction to the finding as filed:**

Severity S3, not S2 — visible, non-destructive, one-click recovery. Two factual corrections: (a) helper line numbers are 6012/6022/5948, not 5995; (b) no RTERM,STOP is actually sent during the teardown (stopRemoteTermSession:6081 early-returns because 5539 already cleared _connected), so the targets keep streaming and their terminal lines land in the number-keyed pane via the 5388 fallback rather than going dead.

The fix should re-arm through relayManageOne, not a bare setRemoteConnected, so the direct-USB guard at 490 still applies (during the ≤15 s reconnect window a user can plug a managed board in directly; setRemoteConnected would then relabel a live USB board "Remote via WCB n", disable its flash/update/factory radios at 5983-5986, and make updateConnectionUI's guard at 7615 swallow its later disconnect events). Concretely, snapshot before 5541:

  const _managed = Object.keys(remoteRelayForBoard).filter(k => remoteRelayForBoard[k] === this.boardIndex).map(Number);

and in the reconnected branch after 5546:

  for (const bn of _managed) relayManageOne(this.boardIndex, bn, false);

relayManageOne → setRemoteConnected re-installs the ETM listener (installEtmListener) and re-issues RTERM,START (startRemoteTermSession), which restores the self-healing ETM-online path at 7703-7723 — so even if the relay is still booting and the immediate RTERM,START is lost, the next ONLINE edge repairs it. Worth also re-rendering the relay card (relayManageOne already calls renderRelayCard).

<details><summary>Full verification reasoning</summary>

MECHANISM — verified in source, not from the summary.

`_startReading()` (Wizard/app.js:5497) falls out of `outer:` on any unexpected reader failure (`getReader` throw 5505, `done` 5513, mid-read throw 5525). The cleanup block is Wizard/app.js:5538-5566 and the reviewer's transcription of 5538-5552 is accurate:
- 5539 `this._connected = false;`
- 5540 `updateConnectionUI(this.boardIndex, false);`
- 5541 `clearRemoteBoardsForRelay(this.boardIndex);`
- 5544 `const reconnected = await this.reconnect(10, 1500);`
- 5545-5555 success branch: `updateConnectionUI(true)`, termLog, toast, and (only if `!_wizardOpen`) `setTimeout(() => boardPull(this.boardIndex), 3000)` — a pull of the RELAY's own config. Nothing touches `remoteRelayForBoard`.

(Reviewer's line cites for the helpers are off by ~27: `clearRemoteBoardsForRelay` is at 6012, `clearRemoteConnected` at 6022, `setRemoteConnected` at 5948 — not 5995. Mechanism unaffected.)

`clearRemoteBoardsForRelay` (6012) walks every slot whose value === the relay slot and calls `clearRemoteConnected` (6022), which `delete remoteRelayForBoard[n]` (6026), re-enables the flash/update/factory radios, restores button styles, calls `removeEtmListener(relayN)` once the last board is gone (6051-6053), and `updateConnectionUI(n, false)` (6054). Keys/values here are board SLOTS, consistent with the relay's `conn.boardIndex` (re-anchored to the relay's WCB number at 6826-6827), so the filter really does match — confirmed by the comment at 3889.

NO REBUILD EXISTS. I grepped every writer of `remoteRelayForBoard`: the only ones that re-arm are `setRemoteConnected` (5948) called from `modalRemoteConnect` (5944), `relayManageOne` (493, user click / `relayRouteAll`), and the wireless-OTA completion (1833). The ETM self-healing listener (7692-7746) is gated on `remoteRelayForBoard[bn] === relayN` at 7703/7712/7728/7733 — with the map emptied it is dead, and `removeEtmListener` has already unhooked the callback anyway. The 12 s mesh sweep (`meshAutoDiscoverTick`, 12607) only refreshes `_relayNodes` and re-renders the relay card (12622); the comment at 12643-12648 says explicitly it does NOT auto-connect. So after a successful relay reconnect the boards stay unmanaged until the user clicks again.

INTENT — the teardown is deliberate, the missing rebuild is not. `git log -L5535,5545` shows 5541 added in 32f4f94 as a plain one-liner ("drop any remote boards using this as relay"), with no reconnect counterpart. Two pieces of in-tree evidence say the author considers the post-reconnect state wrong:
1. Wizard/app.js:1829-1833 (wireless OTA): "setRemoteConnected (not a raw mapping write) so that if the relay's reader blipped mid-OTA (clearRemoteBoardsForRelay tore down the mapping, remote UI, and ETM listener) the WHOLE remote link is rebuilt, not just the map. It is idempotent when nothing was torn down." That is exactly the reviewer's fix, applied to one path only. Also 1852-1856 re-resolves the relay live "(it may have been torn down by clearRemoteBoardsForRelay mid-OTA)".
2. Every DELIBERATE relay reboot bypasses the teardown entirely via `_rebootManaged` + `closeForReconnect()` (7503, 8311, 1657) — the guard at 5538 skips the whole block — so a planned relay reboot KEEPS the boards managed. `relayRouteAll`'s comment at 514-520 ("A board that is ALREADY managed (e.g. after the relay reconnected) keeps its UI state…") describes that path. The unexpected-drop path is therefore inconsistent with the designed behaviour, not a documented trade-off.

REACHABILITY — real. Any relay drop not initiated by the Wizard reaches 5541: USB re-enumeration, relay brownout/crash, an external reset, a `;reboot` reaching the relay over the mesh. `this.reconnect(10, 1500)` (4996) succeeds on the common blip, so the "reconnected but unmanaged" state is the normal outcome, not the rare one. Shared-hub relays never run `_startReading` (5410+ `connectShared`), so this is direct-USB relays only — which is what a MgmtRelay is.

SEVERITY — downgraded to S3. Consequence is bounded: `boardConfigs`/`boardBaselines` survive, nothing is written to hardware, the failure is visible (the boards flip to "Not connected" and the relay card at 563 re-renders each node with a "Manage via relay" button), and recovery is one click on "Manage all via relay" (579 → `relayRouteAll`, whose `toPull` filter skips already-pulled boards, so it is fast). One detail the reviewer got wrong makes it slightly better and slightly worse: `clearRemoteConnected` does NOT actually send RTERM,STOP here — `stopRemoteTermSession` (6079) early-returns on `!relayConn?.isConnected()`, and 5539 already set `_connected=false`. So the targets keep mirroring; their [TERM:n] lines still arrive but fall through the slot-lookup at 5385-5390 to `slot = srcWcb`, landing in whatever pane matches the raw WCB number. Terminals are cosmetically wrong rather than uniformly dead, but control of the boards is genuinely lost.

</details>

### `Wizard/app.js:5771` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**Cancelling the shared-port picker still installs a dead _shared connection, permanently latching _hasSharedPort() and blocking auto-share**

**Correction to the finding as filed:**

Corrected impact: not a permanent latch. `establishConnection`'s `boardConnections[n] = conn` (app.js:5728), `boardDisconnect` (6196-6210) and `sharedConnect`'s own teardown (5746-5758) each clear it. The real damage is (a) auto-share is suppressed for every OTHER slot for as long as the cancelled slot is left untouched, so no shared port exists for the session and no other tab can attach, and (b) because 5728 replaces the dead conn without `leaveShared()`, its hub listeners (5436-5438) are orphaned on the still-joined `_sharedHub` and will pump another tab's port data into that slot's `_handleLine` (5418-5424) if a leader appears later.

Better fix than the one proposed — mirror the cleanup that already exists on the auto-share path (5657-5663) into `modalSharedConnect`'s `!hub.portOpen` branch at 5802-5804: `conn.leaveShared()`, `_sharedHub.leave(); _sharedHub = null`, `delete boardConnections[n]`, clear `wcbSharedSlot` when it points at n, `updateConnectionUI(n,false)`, and toast "port picker cancelled / no shared port" instead of "shared — no tab has opened the port yet". This needs `sharedConnect` to return the conn (it already does, 5778) and needs no new cancellation/leader discrimination at all, so it cannot misfire in the genuine follower case: a follower whose leader is live has `hub.portOpen` true within the existing 4 s wait and is untouched. Optionally also make 5728 defensive: `if (boardConnections[n]?._shared) boardConnections[n].leaveShared();` before overwriting, to kill the orphan-listener path.

<details><summary>Full verification reasoning</summary>

MECHANISM — verified line by line, not from the summary.

`Wizard/app.js:5763-5778` is exactly as quoted. With `existingPort === null` (the only caller in that shape is `modalSharedConnect`, app.js:5791), `hub.requestPort()` (serial-hub.js:159-160 → `navigator.serial.requestPort()`) rejects with `NotFoundError` when the user dismisses the picker. `catch (_) {}` at 5767 swallows it, and 5769-5771 unconditionally build and install a `_shared` BoardConnection, then 5777 writes `wcbSharedSlot`.

State after a cancel with no other tab sharing:
- `conn._shared = true` (5414), `conn._hub = hub`, `hub.join()` (5439). In `join()` the hub posts hello and calls `_maybeCampaign()` (serial-hub.js:139), which returns immediately because `!this._port && !this._sawLeaderPort` (serial-hub.js:261). So the hub is a joined, portless follower that will never open anything on its own — it does NOT squat the Web Lock (that part of the memory-noted portless-leader fix holds).
- `isConnected()` = `!!this._hub?.portOpen` (app.js:4935) = false, so `modalSharedConnect` takes the 5802-5804 branch: warning toast "WCB n: shared — no tab has opened the port yet", and `updateConnectionUI(n,false)` at 5798 leaves the card on "Not connected"/"Connect" (7644-7651). Nothing is torn down.
- `_hasSharedPort()` (5596-5597) iterates `boardConnections` for `c._shared` → now TRUE. That is the sole gate on auto-share at 5641, so every subsequent `establishConnection(..., allowShare=true)` skips the "first board becomes the shared port" branch and connects direct.

REACHABILITY — real. `modalSharedConnect` is reached from the connect modal's "Share port across tabs" option (`app.js:5841` builds `onclick="modalSharedConnect()"`), which `boardConnect(n)` (6184-6188) opens whenever the slot isn't connected. Pressing Escape in a WebSerial picker is an ordinary user action.

INTENT — the swallow is deliberate only for the follower case. The comment at 5731-5736 says "cancelling the picker is the right move for a follower", and `git log -L` shows the swallow dates to 8c9a8ac in its original `try { await hub.requestPort(); } catch (_)` form. Nothing in any comment addresses the no-leader cancel. Two pieces of evidence say the leftover is an oversight, not a considered trade-off: (1) the sibling auto-share path already does the exact cleanup this path lacks — `establishConnection` 5650-5663 waits ~3 s for `hub.portOpen` and, if it never opens, calls `conn.leaveShared()`, `_sharedHub.leave()`, `_sharedHub = null`, `delete boardConnections[n]`, and removes the `wcbSharedSlot` marker; `modalSharedConnect` waits 4 s for the same condition (5797) and then just toasts; (2) the codebase does distinguish picker cancellation elsewhere — `modalAuthorize` at 5911 tests `e?.name !== 'NotFoundError'` before toasting.

WHAT THE REVIEWER GOT WRONG — the stated failure scenario is incorrect. "The user then connects WCB 1 normally via USB and connects WCB 2: WCB 2 never auto-shares" does not happen: a direct connect of slot 1 goes through `establishConnection`, whose `boardConnections[n] = conn` at 5728 REPLACES the dead conn, so `_hasSharedPort()` goes false again and WCB 2 does auto-share. The latch is not permanent — it clears the moment that slot is connected (5728), disconnected (`boardDisconnect` 6202-6210 also clears the marker), or re-shared (`sharedConnect`'s teardown loop 5746-5758). The real bad scenario is narrower: cancel on slot 1, then leave slot 1 alone and connect slots 2..N — all of them go direct, no board ever becomes the shared port, and the NaviCore/second-Wizard tab has nothing to attach to for the rest of the session, with no message explaining why.

ONE CONSEQUENCE THE REVIEWER MISSED, which is worse than the latch: 5728 overwrites the dead conn without ever calling `leaveShared()`, so its `data`/`state`/`log` listeners (5436-5438) stay registered on the still-joined `_sharedHub`. If another same-origin tab later opens the shared port, the orphan receives relayed bytes (serial-hub.js:492) and pumps a different board's serial stream through slot n's `_handleLine` (5418-5424) while `_onHubState` (5428-5435) toggles slot n's connection UI.

Net: real defect, user-recoverable, no data loss and no config written to the wrong board beyond what the Share button designedly does. S3, not S2.

</details>

### `Wizard/app.js:6136` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**General-mismatch modal's "Use WCB<n> values" silently ignores the NaviCore rows it just displayed**

**Correction to the finding as filed:**

Two corrections to the finding, plus a completion of the fix.

(a) MECHANISM: pushing WCB 1 does NOT "re-send ?CONTROLLER,ON,20". parser.js:1277-1281 emits CONTROLLER only on a diff against boardBaselines[n].specialPeer; nothing moved, so the incremental push emits no CONTROLLER command at all. (A full push - `!boardBaselines[n]`, app.js:7417 - would emit CONTROLLER,ON,20.) Same end state, different mechanism.

(b) RECURRENCE: "pulling WCB 2 re-opens the identical conflict modal" is wrong. `generalBaseline.fields` set at app.js:6137 is overwritten moments later by `updateGeneralBaseline()` (app.js:1190-1193), called at the end of all seven follow-up handlers, which re-derives the fields from `systemConfig.general` - whose specialPeer/specialPeerId were never updated. The baseline therefore REVERTS to the old NaviCore values, so a board matching the old value shows no row. The real recurrence is on the same board: re-pulling the mismatching board re-opens the modal with the NaviCore row every time, and "Use <new> values" can never clear it. "Keep <src> values" does clear it.

(c) FIX COMPLETION: the proposed fix's substantive half is right but it stops short. `refreshControllerUI()` (app.js:1330-1339) renders from `systemConfig.general.controller`, which the fix never updates, so turning NaviCore off would leave the segmented chip on "NaviCore" and `g-navicore-detail` visible; `updateRemoteSectionsUI` (app.js:2084-2085) keys the per-board NaviCore/Kyber section visibility off the same `controller` value, so those stay wrong too. Worse, a stale `controller === 'navicore'` means any later `_applyControllerToBoards()` (app.js:1305-1318) would re-set `specialPeer = true` on every board and silently undo the fix. Minimal correct version, inserted before the seven handler calls at app.js:6155 so `updateGeneralBaseline()` picks it up:

    systemConfig.general.specialPeer   = newFields.navicoreEnabled;
    systemConfig.general.specialPeerId = newFields.navicoreId;
    for (const n in boardConfigs) {
      if (boardConfigs[n].type === 'client') continue;   // same guard as app.js:1476
      boardConfigs[n].specialPeer   = newFields.navicoreEnabled;
      boardConfigs[n].specialPeerId = newFields.navicoreId;
    }
    deriveControllerFromBoards();   // app.js:1368 - re-derive 'navicore'|'kyber'|'none'
    refreshControllerUI();          // also rewrites g-navicore-id from specialPeerId
    refreshAllNavicoreStatus();     // app.js:2108 - per-board section visibility + status text

`set('g-navicore-id', …)` is then redundant (refreshControllerUI:1337-1338 does it). The stated alternative - dropping the two keys from GENERAL_FIELD_LABELS - is safe (extractGeneralFields hardcodes its own key list and is not driven by that map, so `applyGeneralFieldsToBoardConfig` keeps working on the "Keep" branch) but it hides a genuine cross-mesh inconsistency that parser.js:1274-1276 says must not exist.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived from source, not the summary.

1. The NaviCore rows really are rendered. `GENERAL_FIELD_LABELS` (Wizard/app.js:1392-1410) has exactly 16 keys, the last two being `navicoreEnabled:'NaviCore'` (:1408) and `navicoreId:'NaviCore ID'` (:1409). `extractGeneralFields` populates them from `config.specialPeer` / `config.specialPeerId` (:1429-1430). `getGeneralMismatches` diffs `Object.keys(GENERAL_FIELD_LABELS)` with a `String()` compare (:1435-1437), and the modal table renders every returned row (:6109-6113) under the text "These must match across all boards - choose which values to keep" (:6101). Not incidental either: parser.js:165-168 comments the general-block `specialPeer`/`specialPeerId` fields as "network-wide: same on every WCB. Field names mirror the per-board config so extractGeneralFields() works uniformly" - NaviCore is deliberately in this set.

2. The asymmetry is real and complete. "Keep <src> values" (:6124-6133) calls `applyGeneralFieldsToBoardConfig(newBoard, baselineFields)`, which writes `cfg.specialPeer = fields.navicoreEnabled; cfg.specialPeerId = fields.navicoreId;` behind an `if (cfg.type !== 'client')` guard (:1476-1479). I read the whole "Use <new> values" handler (:6136-6170): it writes g-password, g-mac2, g-mac3, g-meshch, g-delimiter, g-funcchar, g-cmdchar, g-etm-enabled, g-etm-chksm and the six ETM numerics, then calls onGeneralPasswordChange / onGeneralMacChange / onMeshChannelChange / onGeneralCmdCharChange / onETMToggle / onETMChecksumToggle / onGeneralETMChange (:6155-6161). I opened all seven of those handlers (:1136-1141, :1144-1152, :1154-1160, :1175-1187, :1194-1203, :1206-1212, :1214-1237). Every one of them writes systemConfig.general AND loops `for (const n in boardConfigs)` to propagate. None touches specialPeer, specialPeerId, controller, or `g-navicore-id`. So 14 of the 16 diffed keys are honoured on both branches; the two NaviCore keys are honoured on "Keep" only.

3. Nothing downstream recovers it. The push path reads the general values straight out of the DOM at app.js:7408-7415 (password, mac2, mac3, meshch, delimiter, funcchar, cmdchar, wcbq) - no controller read - then `WCBParser.buildCommandString(config, boardBaselines[n], fullPush)`. The CONTROLLER emitter is parser.js:1277-1281 and diffs the PER-BOARD `config.specialPeer` against `baseline.specialPeer`. Since neither side moved, the incremental push emits nothing at all for CONTROLLER (the finding says it "re-sends ?CONTROLLER,ON,20"; it actually emits no CONTROLLER command - same net effect, wrong mechanism). `boardConfigs[newBoard]` was already replaced by the pulled config (:6857+ / :7984), so the fleet is left split.

4. REACHABILITY. Two live call sites, both real: USB `boardPull` (:6854-6866) and the relayed pull (:7970-7982), both `setTimeout(..., 200)` so the modal opens after the pull has committed. Guards are only `mismatches.length > 0 && !_wizardOpen && !isDefaultNetworkSettings(incomingGeneral)`; `isDefaultNetworkSettings` (:1449-1452) only suppresses MAC 00/00 factory boards, so a configured board with CONTROLLER off (or a different id) reaches the modal. Buttons are wired only at :6124/:6136 - index.html:315-316 carry no onclick - so there is no second handler. Confirmed the trigger state is trivially producible: a board pushed before NaviCore was turned on, or a spare board joined later.

5. ONE CORRECTION TO THE REVIEWER'S OUTCOME. The finding claims "generalBaseline.fields now holds the new values ... the same modal re-fires on the next pull of any OTHER board." That is wrong. `generalBaseline = {sourceBoard:newBoard, fields:newFields}` at :6137 is immediately clobbered: each of the seven follow-up handlers ends with `updateGeneralBaseline()`, which does `generalBaseline.fields = extractGeneralFields(systemConfig.general)` (:1190-1193). Because systemConfig.general.specialPeer/specialPeerId were never updated, the baseline's NaviCore fields revert to the OLD values. So pulling WCB2 (which matches the old value) shows NO NaviCore row. What actually happens is worse in one respect: re-pulling the SAME board re-opens the modal with the NaviCore row forever, and clicking "Use <new> values" can never clear it - a permanently unresolvable dialog on that branch. "Keep <src> values" does resolve it (it dirties boardConfigs[newBoard].specialPeer, so parser.js:1281 emits CONTROLLER,ON/OFF on the next push).

6. INTENT. git log -S"navicoreEnabled" -- Wizard/app.js returns adebe4e ("updates for NC and wizards") and e9c2baf; adebe4e added the labels and did not touch the general-conflict-use handler. `git log -L 6136,6172` shows the handler being extended twice (meshch added, then the ETM block) - it has been grown field-by-field as fields were added, and NaviCore was simply never added. No comment anywhere claims this is deliberate; the adjacent comment at :6162-6164 is only about badge state. Nothing in CLAUDE.md or docs/ documents an exclusion.

WHY S3 AND NOT S2. Real, user-visible, and the success toast at :6169 is a lie - but this is browser-side in-memory state only. Nothing wrong is written to any board (the incremental diff emits no CONTROLLER command at all), nothing is destroyed, no baseline is corrupted, the fleet is left exactly as it already was, and the true state stays visible in General Settings (the controller chip and NaviCore ID still show the old value, so the UI is self-consistent with reality even though it contradicts what the user picked). The other branch works, and the manual workaround - set the controller in General Settings and push - is one click away. In this same review set, docs/CODE_REVIEW_2026-08_verdicts.md:592 downgraded S2 to S3 on exactly this reasoning ("an intended improvement simply never took effect"), and the S2 findings around it (e.g. F-170, boardPull committing a truncated backup over config AND baseline) involve actual state destruction. This is a fix-up dialog that fails to fix up, not a corrupting write.

</details>

### `Wizard/app.js:6800` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**boardPull commits an empty or truncated backup as a successful pull, overwriting config AND baseline with factory defaults**

**Correction to the finding as filed:**

Mechanism, line numbers and constants are all accurate as reported; the corrections are to severity and to the fix.

Severity: S3, not S2. Nothing is written to the board or to disk by the bug itself — `boardPull` only mutates in-memory `boardConfigs[n]`/`boardBaselines[n]` and the DOM (`app.js:6915-6917`). Real damage (factory `espnowPassword`/`wcbNumber` reaching hardware) needs a subsequent user Go/Push after a visibly-reset UI. Recovery is a re-pull or re-loading the system file.

Add to the description (the reviewer missed these, and they are the strongest evidence): the catch's own "Pull timed out — board may have no firmware loaded" message at `app.js:6926-6931` is UNREACHABLE, because both `sendAndCollect` awaits sit outside the try (try opens at `:6808`, awaits are at `:6800`/`:6804`) and neither `sendAndCollect` nor `parseBackupString` ever rejects or throws. And the wizard's verify retries at `app.js:11737`/`:11742`, keyed on `!boardBaselines[n]`, can never fire for the same reason. Both are dead code that document the intended contract this bug violates.

Fix: gating on `raw.includes('End of Backup')` before parsing is correct and safe with respect to the sentinel contract (`WCB.ino:5789` unconditional; `MgmtRelay.ino:334` plus its `:316-317` comment naming it as the Wizard's acceptance requirement), but it MUST be paired with a re-pull, or it regresses `wizardWatchForConnect`. That watcher (`app.js:11694-11701`) waits on `boardBaselines[n]` being truthy and the wizard flow contains exactly one pull (`app.js:8325`, t+3 s), with reconnect auto-pulls deliberately suppressed in wizard mode (`app.js:5548-5555`). Today the defaults-commit is what unblocks it. Suggested shape: on a missing sentinel, leave `boardConfigs[n]`/`boardBaselines[n]` untouched, `termLog` + toast the already-written timeout message, and either (a) retry inside `boardPull` (2-3 attempts, ~5 s apart) or (b) have the wizard path call `waitForBoardReady(n, conn)` — which already exists at `app.js:6685` and correctly re-sends both tiers every 10 s with a persistent listener — before its verify pull.

<details><summary>Full verification reasoning</summary>

I re-derived every load-bearing fact from source rather than the summary.

MECHANISM — all three links hold:
1. `Wizard/app.js:6800` and `:6804` — both `sendAndCollect` awaits sit OUTSIDE the try, which starts at `:6808`. So no timeout can ever reach the catch at `:6925`.
2. `sendAndCollect` (`app.js:5232-5254`) resolves and never rejects: `timer = setTimeout(finish, timeoutMs)` (`:5252`) and `this.send(command + '\r').catch(finish)` (`:5253`) — a send failure is converted into a resolve. On a silent board `raw === ''`.
3. `parseBackupString` never throws on junk — `parser.js:244-247` logs `console.warn('[WCB Parser] No command tokens found in backup output')` and returns the object from `createDefaultBoardConfig()`. I read that factory object at `parser.js:20-48`: `wcbNumber: 1` (`:25`), `espnowPassword: 'change_me_or_risk_takeover'` (`:41`), `macOctet2/3: '00'` (`:42-43`), `hwVersion: 0` (`:23`), `funcChar: '?'`, `delimiter: '^'`, `cmdChar: ';'`. The reviewer's transcription is accurate.

COMMIT PATH — with `detected = config.wcbNumber = 1`, the guard at `app.js:6871` (`detected !== n && detected >= 2 && detected <= WCB_MAX`) is false for every slot, so control falls to the "normal path" `else` at `app.js:6914-6923`: `boardConfigs[n] = config; boardBaselines[n] = JSON.parse(JSON.stringify(config)); populateUIFromConfig(n, config); showToast('Config pulled from WCB ' + n, 'success')`. Factory defaults are committed to config AND baseline AND the DOM, with a SUCCESS toast. No completeness check exists anywhere between `:6804` and `:6922`.

TWO PIECES OF DEAD CODE PROVE THE INTENDED DESIGN WAS THE OPPOSITE:
- `app.js:6926-6931` builds the message "Pull timed out — board may have no firmware loaded. Try Flash mode first." behind `/timeout/i.test(e.message)`. Because the awaits are outside the try and neither `sendAndCollect` nor `parseBackupString` ever rejects/throws, that branch is unreachable. The authors wrote handling for exactly this case; the control flow bypasses it.
- `app.js:11737` and `:11742` retry the wizard verify `if (boardConnections[n]?.isConnected() && !boardBaselines[n])`. Since `boardPull` sets `boardBaselines[n]` unconditionally on every non-relay, non-migration path, that retry can never fire. The "did the board answer?" signal is destroyed by the same bug.

CORROBORATING COMMENTS (this codebase's comments record real failures):
- `app.js:5548-5551`: "In wizard mode wizardWatchForConnect handles the verify pull itself. Auto-pulling here would race with that pull and potentially clobber boardConfigs[n] with factory defaults before the wizard pushes config." The author has already seen defaults get committed.
- `app.js:11707-11710`: "boardPull will have overwritten boardConfigs[n] and the general DOM fields."
So this is a known-hazard that is narrowly worked around at two call sites, not a deliberate documented trade-off.

REACHABILITY — real. `boardPull` is reached from an inline `onclick` (`Wizard/index.html:495`, `{manual:true}`) and from ~20 `setTimeout(() => boardPull(n), 3000)` sites after connect/reconnect (`app.js:5554`, `:6516`, `:6584`, `:8325`, `:9226`, `:11589`). The failure needs the board silent across ~11 s (3 s tier-1 + 8 s tier-2). That is producible: a never-flashed board (the dead toast names this case), wrong baud/bad cable, or a long boot — `waitForBoardReady`'s own comment at `app.js:6678-6680` documents "After a factory-reset flash the ESP32 takes 60+ s to boot (WiFi init, ESP-NOW init, loading NVS defaults)", and `WCB.ino setup()` carries `delay(1000)` + `delay(1000)` + a 6×250 ms settle loop before WiFi/ESP-NOW init. It is NOT the everyday path (a booted board answers tier-1 in well under 3 s, and the enlarged UART0 RX ring buffer means a command sent mid-boot is buffered and answered late, often still inside the tier-2 window).

SEVERITY — downgrading S2 → S3. Real blast radius: false success toast, in-memory config+baseline replaced by defaults, and if this is the first pull of the session, `app.js:6853-6856` seeds `generalBaseline` from those factory defaults, which then drives the general-mismatch modal against every subsequent real board. But: nothing is written to the board or to disk (a push requires the user to press Go afterwards), the UI visibly resets to defaults, and recovery is a re-pull or re-load of the system file. That is a genuine correctness bug worth fixing, not data destruction.

FIX CHECK — direction right, literal patch regresses the wizard. The sentinel is a stable contract: `WCB.ino:5789` prints "--------- End of Backup ---------" unconditionally at the end of `printBackupConfig()` (no early returns), and `WCBClient/examples/MgmtRelay/MgmtRelay.ino:334` prints it too, with a comment at `:316-317` stating "The Wizard only requires the 'WCB Configuration Backup' header + an 'End of Backup' line to accept the device." So gating on it does not break relay pulls. BUT `wizardWatchForConnect` gates on `boardBaselines[n]` being truthy (`app.js:11694-11701`, comment: "boardBaselines[n] being non-null means boardPull finished"), and the only pull in that flow is the single `setTimeout(() => boardPull(n), 3000)` at `app.js:8325` — reconnect auto-pulls are deliberately suppressed in wizard mode (`app.js:5548-5555`). Today the defaults-commit accidentally unblocks that watcher; with the literal fix a board needing >~14 s to answer would leave `boardBaselines[n]` unset forever and the wizard would report "✕ Timed out" at 90 s instead of pushing a perfectly good config. The fix must be paired with a re-pull.

</details>

### `Wizard/app.js:7140` — CONFIRMED · S2 → **S3**

**Failed post-flash / post-erase boot-wait leaves the Push Config button permanently disabled and labelled "Flashing…"**

**Correction to the finding as filed:**

Real defect, right location, right mechanism — but the severity and the "permanently disabled" framing are wrong, and one cited line number is off.

Corrections:
1. Severity S3, not S2. The button is not permanently disabled: `updateConnectionUI(n, true)` sets `goBtn.disabled = false` (app.js:7638, not 7645), so any subsequent disconnect/reconnect re-enables it — only the 'Flashing…'/'Erasing…' label is sticky. And config pushing is never actually blocked: "▶▶ Push All" (index.html:33 → boardGoAll at app.js:8253) calls `boardGo(n)` programmatically, ignores the button's disabled state, and because `boardFlashMode[n]` was already reset to 'configure' (7127/7235/7317) it runs the configure path and restores the button at app.js:7606-7607. Same for Update FW / Factory Reset. The wizard's own flow (app.js:11722-11749) also proceeds normally. Impact is one stuck/mislabelled button on a rare timeout path the user has already been told about, with several non-destructive recoveries.
2. Correct tail line numbers: flash/update restore is app.js:7285-7288, erase is app.js:7371-7374, configure is app.js:7606-7608.
3. Worth adding: while the button is stuck disabled, `updateBoardStatusBadge(n,'unsaved')` also cannot apply the amber pending-changes styling, because that branch is gated on `!goBtn.disabled` (app.js:3191) — so the board card additionally stops signalling unsaved edits.
4. Scope note for whoever fixes it: only these three awaited wizard branches leak. Do NOT "fix" app.js:7264-7273 (already falls through correctly) or the non-wizard IIFE returns at 7170-7174 / 7193-7206 / 7348-7361 (their `return`s exit the IIFE, not boardGo, so the tail runs).

<details><summary>Full verification reasoning</summary>

I re-derived every load-bearing fact from Wizard/app.js (12705 lines) rather than the summary.

MECHANISM — the three bare returns are real and at the claimed lines:
- `btn` is captured at app.js:7005 (`b${n}-btn-go`, declared in Wizard/index.html:496 with label "Push Config").
- app.js:7038-7039 sets `btn.disabled = true` and `btn.textContent = (isFlash||isUpdate||isFactory) ? 'Flashing…' : isErase ? 'Erasing…' : 'Pushing…'`. (7078 re-sets 'Flashing…' again just before flashFirmware.)
- Flash/update tail that restores it: app.js:7285-7288 (`_reshareAfterFlash`, `btn.disabled=false`, `btn.textContent='Push Config'`, `return`). Erase tail: app.js:7371-7374. Configure tail: app.js:7606-7608.
- Three wizard-only branches exit before those tails with a bare `return`:
  * app.js:7136-7141 — isUpdate && `_wizardOpen`, after `waitForBoardReady` returns false.
  * app.js:7177-7182 — flash/factory && pushConfig && `_wizardOpen`, same shape.
  * app.js:7332-7337 — erase && `_wizardOpen`, same shape.
  Verified by eye, line by line; no `try/finally` wraps the flash or erase blocks (the only `try` inside the flash block, 7081-7105, wraps flashFirmware itself).
- Nothing else rewrites the Go button's label: grepping `"Push Config"` across app.js/index.html gives only 7287, 7373, 7404, 7423, 7607, 7789/7796/7812/7823/7881 (all boardGo/boardGoRemote exits) and index.html:496. `updateConnectionUI` (app.js:7611, the button block at 7638) writes `goBtn.disabled = !connected` and NEVER the label. `updateBoardStatusBadge` (3187-3199) only toggles btn-pending/btn-success classes, and its amber-on-unsaved branch is itself gated on `!goBtn.disabled` (3191), so it is also suppressed. `boardPull`'s `btn` (6788) is the Pull button, not Go — it never touches Go.

REACHABILITY — a real user can get here. `waitForBoardReady` (app.js:6686) resolves false only on `totalTimeoutMs = 150000`; the header comment at 6675-6685 documents that a factory-reset board takes 60+ s to boot, so a 150 s miss is plausible, not theoretical. The wizard genuinely enters boardGo in these modes: `wizardNext` sets `boardFlashMode[i+1]` to 'erase' | 'factory' | 'update' | 'configure' at app.js:9438-9446, and `wizardWatchForConnect` calls `await boardGo(n)` at app.js:11719 with no opts, so `mode = boardFlashMode[n]` (7000). `_wizardOpen` is true for that whole window (set 9393, cleared 9407). All three branches are live.

WHAT THE REVIEWER OVERSTATED — "permanently disabled" is not accurate. `updateConnectionUI(n, true)` sets `goBtn.disabled = false` (app.js:7638), so any later disconnect+reconnect, auto-reconnect, or auto-detect re-enables the button; only the stale 'Flashing…'/'Erasing…' label survives. And the lockout does not block config pushing: "▶▶ Push All" (index.html:33 → boardGoAll, app.js:8253) calls `boardGo(n)` programmatically and never inspects the button, and since 7127/7235/7317 already reset `boardFlashMode[n]='configure'`, that call runs the configure path and fully restores the button at 7606-7607. Update FW / Factory Reset do the same. The wizard's own flow is unaffected — 11722-11749 continues to verify and marks "✓ Done" regardless. So the impact is: one board card's Push Config button stuck disabled with a wrong label until any reconnect or any other boardGo, on a rare error path where the user has already been told in the terminal and a toast that the board did not answer. Non-destructive, several workarounds. That is S3, not S2.

INTENT — not deliberate. `git log -L 7134,7142:Wizard/app.js` shows commit 6704593 ("update to fix mac os flashing") replaced `await sleep(4000);` with the `waitForBoardReady` + `if (!ready) { … return; }` block. Before that commit there was no early exit and the tail always ran, so the leak is a regression introduced with the boot-wait, with no comment defending it. The author clearly did not intend it: the structurally identical case at app.js:7264-7273 (flash failed, version already matches, wait times out) uses `if (ready) {…} else { termLog/showToast }` with NO return and correctly falls through to the tail, and the non-wizard variants at 7170-7174, 7193-7206 and 7348-7361 put their `return`s inside IIFEs so boardGo's tail still runs. Only the three awaited wizard branches leak. `boardGoRemote` even carries the comment "Always restore the button — boardGoRemote has no other reset path" (app.js:7880-7881), showing the invariant is understood elsewhere.

FIX — the proposed inline restore works and skips nothing important: `_reshareAfterFlash` (app.js:6967) opens with `if (!conn || !conn._wasSharedBeforeFlash) return;` and clears the flag on every path, and it has already been called at 7128 (flash/update) and 7324 (erase) before these branches, so the tail calls at 7285/7371 are provable no-ops here. Enabling the button is also the correct end state — the board reconnected successfully (7126/7322 already ran `updateConnectionUI(n, true)`), so a manual retry push is exactly what the user should be able to do.

</details>

### `Wizard/app.js:7902` — CONFIRMED · S2 → **S3**

**remoteBoardPull leaks its _pullingBoards entry on a retry attempt, permanently locking that board out of all future pulls**

**Correction to the finding as filed:**

Two corrections to the reviewer's write-up, neither of which changes the verdict.

1. "The entry is never cleared by anything else ... the only deletes are inside remoteBoardPull itself" is factually wrong: `Wizard/app.js:1834` and `:1862` also delete, in the wireless-OTA path. They are OTA-scoped so they do not fix the general case, but the claim as stated is inaccurate — and their comments actually document this exact failure mode, so they should have been cited as supporting evidence rather than denied.

2. The failure scenario is understated. The dominant trigger is not a user unplugging USB during the 2.5 s retry gap; it is a relay board REBOOT. `Wizard/app.js:5523` breaks the read loop on a mid-read error ("e.g. reboot"), `:5537` sets `_connected = false`, and `:5544` spends up to ~15 s in `reconnect(10, 1500)` with `isConnected()` false. Any pending retry firing in that window leaks. The vulnerable window is the whole in-flight pull minus the final attempt (>= ~8.5 s with the default 3 attempts, up to ~100 s for the 12-attempt post-OTA pull at `:1838`), not 2.5 s.

Worth adding to the impact statement: the user's explicit recovery control, `boardRetryPull` (`:8062`), sets the badge back to 'remote' at `:8065` before the pull is silently deduped at `:7906` (termLog only, no toast), so the UI actively reports health while doing nothing.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived from source, not the summary.

`Wizard/app.js:7900-7912` reads exactly as quoted. The liveness guard is `:7902` and runs unconditionally on every attempt; the `_pullingBoards.add(targetN)` at `:7911` sits inside `if (attempt === 1)` at `:7905`. So on any attempt >= 2 the `:7902` early return exits with the marker already set and never deletes it. The retry is scheduled at `:8021` (`setTimeout(() => remoteBoardPull(relayN, targetN, attempt + 1, maxAttempts, onComplete), PULL_RETRY_MS)`), and nothing cancels that timer on disconnect. Constants verified at `:7892-7894`: MAX_PULL_ATTEMPTS=3, PULL_TIMEOUT_MS=6000, PULL_RETRY_MS=2500.

I exhaustively enumerated every reference to the set (11 total, `grep -rn "_pullingBoards" Wizard/`): declaration `:60`, reads `:527` and `:7906`, the single add `:7911`, deletes `:1834, :1862, :7949, :8002, :8005, :8023, :8043`. Confirmed no `.clear()` and no cleanup in any teardown path: `boardDisconnect` (`:6196-6220`), `clearRemoteBoardsForRelay` (`:6012`), and `clearRemoteConnected` (`:6022`) all leave the set untouched.

REVIEWER GOT ONE FACT WRONG, in his own disfavor. He wrote "the only deletes are inside remoteBoardPull itself". False — `:1834` and `:1862` are in the wireless-OTA path. But they do not rescue the general case (both are OTA-scoped, and `:1862` explicitly skips `n` and `targetWcb`), and their comments are the strongest evidence FOR the finding. `:1830-1833`: "clear any stale in-flight guard left by an ETM-offline pull fired DURING the OTA, otherwise every re-pull is silently deduped -> 'nothing happens'". `:1857-1861`: "clear any stale in-flight pull so the reconciliation isn't deduped into a no-op." The author has already been bitten by exactly this failure mode and patched the two OTA call sites defensively instead of fixing the root cause at `:7902`.

REACHABILITY — the real trigger is far more routine than the reviewer's USB-unplug story. `isConnected()` at `:4935` returns `this._connected` for a non-shared conn. The read loop at `:5523-5525` breaks on any mid-read error with the comment "Board disconnected mid-read (e.g. reboot)", then `:5537` sets `this._connected = false`, `:5541` calls `clearRemoteBoardsForRelay`, and `:5544` runs `await this.reconnect(10, 1500)` — up to ~15 s during which `isConnected()` is false. A relay board REBOOT (every `?reboot`, every config push needing a reboot, every power blip) therefore opens a ~15 s window. That same event is what stalls the in-flight pull in the first place, so the timeout and the aborted retry are causally linked rather than a coincidence. The exposure window is also wider than claimed: the leak needs the relay down when a retry *starts*, and with attempt 1 timing out at 6 s and retrying at 8.5 s, a disconnect anywhere in the first ~8.5 s suffices — up to ~100 s for the post-OTA `remoteBoardPull(relayN, n, 1, 12)` at `:1838`.

CONSEQUENCE — verified every recovery path funnels into the silent dedup. All callers (`:494` relayManageOne, `:527/:533` relayRouteAll, `:5945` modalRemoteConnect, `:7716/:7741` ETM online/offline listener, `:8058` boardPullRemote, `:8066` boardRetryPull) invoke `remoteBoardPull` with the default attempt=1 and hit `:7906`, which does a `termLog` only — no toast (`:7907`). `relayRouteAll` independently filters the board out at `:527`. Worst of it: `boardRetryPull` (`:8062`, commented "Manual retry after a failed remote pull — resets badge and starts a fresh attempt cycle") sets the badge to 'remote' at `:8065` so the UI looks healthy, then the pull is silently swallowed. The board keeps a working terminal (startRemoteTermSession runs independently in setRemoteConnected `:6007`) but its config never loads, with no in-UI recovery short of a page reload.

INTENT — not deliberate. `git log -L 7900,7913` shows the `:7902` liveness return predates the dedup block: 02cc8ed (2026-03-31) added the `if (attempt === 1) { ... _pullingBoards.add }` block *below* the already-existing early return. The ordering is accretion, not design, and no comment defends it. 63006ad and 69fdb59 later touched these exact lines (adding maxAttempts, then `onComplete?.(false)` to the `:7902` return) without noticing the marker leak — 69fdb59 in particular shows the author auditing this very return statement for the callback path and missing the set.

SEVERITY — downgrading S2 to S3. It is a real, routinely-triggerable, silent-failure bug with a lying retry button, which justifies more than S4. But the blast radius is bounded: no wrong bytes on the wire, no bad config written to hardware, no firmware/NVS impact, terminal still works, and recovery is a page reload. It is browser-tool-only state.

</details>

### `Wizard/app.js:8282` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**Push All includes MgmtRelay slots, whose board section (and Push button) was deleted — boardGo throws and Push All dies before the relay-reboot stage**

**Correction to the finding as filed:**

Mechanism, location (7005/7039) and stage numbering are all correct. Two corrections.

(1) Impact overstated. In the common MgmtRelay-only case the relay-reboot stage is a no-op (relayRebootList is empty), and all remote boards were fully pushed+rebooted in Stage 1. Actual harm: an unhandled promise rejection with no toast, and generalSettingsDirty (8334) never clearing so Push All stays amber. The genuinely bad outcome - a WCB-firmware relay left pushed-with-skipReboot and never rebooted - only occurs when a real WCB relay and a MgmtRelay are both directly connected.

(2) Fix as written is a no-op. Object.keys() yields STRING keys and the proposed `!_relaySlots.has(n)` is inserted into the filter at 8255, which runs BEFORE the .map(Number) at 8256. _relaySlots holds Numbers (relaySlot = config.wcbNumber || n, app.js:6819/6844; wcbNumber is parseInt'd in parser.js), so _relaySlots.has("19") === false and nothing is filtered. Correct form - map first, then filter:

  const directSlots = Object.keys(boardConnections)
    .map(Number)
    .filter(n => boardConnections[n]?.isConnected() && !_relaySlots.has(n));

(3) The "harden boardGo" half is incomplete: guarding only 7039-7040 moves the throw to `btn.disabled = false; btn.textContent = 'Push Config'` at 7606-7607 (and 7078, 7286-7287, 7373, 7404, 7423 on the flash/erase paths). A single early return is cleaner and states the invariant:

  if (_relaySlots.has(n)) return;   // MgmtRelay conduit - no board section, nothing to push

placed right after the boardGoRemote delegation at 6993.

<details><summary>Full verification reasoning</summary>

Traced end to end in Wizard/app.js.

MECHANISM (holds): boardGoAll (8253) builds directSlots from raw boardConnections keys (8254-8256) with no _relaySlots exclusion, and relayBoardSet from Object.values(remoteRelayForBoard) (8281); directRelays = directSlots.filter(n => relayBoardSet.has(n)) (8282). A MgmtRelay always enters relayBoardSet: relayManageOne (app.js:493) -> setRemoteConnected (app.js:5949) sets remoteRelayForBoard[target] = relaySlot, and relayRouteAll does it for every heard board (the "Manage all via relay" button rendered at app.js:579). The relay slot is a live boardConnections entry (boardConnections[relaySlot] = conn, app.js:6822; _relaySlots.add(relaySlot), app.js:6844). Its board section is removed by applyRelayRole (document.getElementById(`section-board-${n}`)?.remove(), app.js:481) and can never be recreated: desiredBoardNumbers() filters _relaySlots (app.js:437, comment "MgmtRelay slots render as a dedicated card, never a numbered grid section"), and addBoardSection has exactly one caller, reconcileBoardGrid (app.js:459).

So boardGo(relaySlot, {skipReboot:true}) at 8300: remoteRelayForBoard[relaySlot] is undefined so no delegation to boardGoRemote (6993); conn.isConnected() true (6995); mode resolves to 'configure' (boardFlashMode[relaySlot] never set - no section, no radios) so the _shared early-return at 7014-7031 cannot fire; btn = getElementById(`b${n}-btn-go`) is null (7005); btn.disabled = true at 7039 throws TypeError. Confirmed unguarded by direct read of 6991-7040. boardGoAll has no try/catch and Wizard/ has no unhandledrejection or window.onerror handler (grepped, zero hits), so it fails silently; Push All is invoked from Wizard/index.html:33 onclick="boardGoAll()" with no catch.

REACHABILITY: high. This is the documented MgmtRelay workflow (connect the WCB_Client MgmtRelay over USB, "Manage all via relay", edit, Push All). Only WCBClient/examples/MgmtRelay/MgmtRelay.ino:329 emits ?RELAY,1; the WCB firmware never does (grep RELAY over Code/ -> 3 hits, all unrelated: WCB_RemoteTerm.h, WCB_OTA.cpp, WCB.ino). So _relaySlots holds only WCB_Client conduits, while a plain USB-connected WCB can also be a relay via openConnectModal (5820-5836) -> modalRemoteConnect -> setRemoteConnected; that case has a real board section and boardGo works. Both can coexist.

INTENT: not deliberate. The 4-stage Push All relay logic predates the MgmtRelay feature by four months (git log -S"directNonRelays" -> 978ecf4 2026-03-03; git log -S"_relaySlots" -> a7435a3/69d00ab/e734e49 all 2026-07-15). updateConnectionUI already encodes the invariant correctly ("A relay slot has no board section", app.js:7661-7662, every deref null-guarded) - boardGo is simply an un-updated caller.

SEVERITY (downgraded S2 -> S3): in the dominant MgmtRelay-only topology, Stage 1 (boardGoRemote, 8300) has already pushed and rebooted every managed board before the throw, and relayRebootList is empty, so "dies before the relay-reboot stage" costs nothing there. Residual harm is a silent console exception plus generalSettingsDirty never being cleared (8334), which only tints the Push All button (updatePushAllButton, 3154-3161). The S2-grade outcome - a WCB-firmware relay board pushed with skipReboot:true at 8300 that never receives its Stage-4 reboot (8306-8330), so reboot-requiring changes (MAC/WCB#/HW/KYBER/PWM map) silently do not apply - requires the rarer mixed topology (a WCB-as-relay AND a MgmtRelay both directly connected, WCB slot sorting first in Object.keys numeric order).

</details>

### `Wizard/app.js:8966` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**Kyber target baud picker uses the full `BAUD_RATES` list; the firmware's Kyber target parser accepts only six of those rates and silently skips the rest**

**Correction to the finding as filed:**

Location is wrong. Wizard/app.js:8966 is dead code — `b{N}-kyber-target-tbody` does not exist in index.html (only the read-only auto-computed div at index.html:661-664), and `addKyberTargetRow` (app.js:8938), the sole caller of `appendKyberTargetRow`, is itself never called from app.js or from any inline onclick. That picker never renders.

The real reachable location is the Maestro row baud picker at Wizard/app.js:3251-3255, flowing through `syncMaestrosToConfig` (app.js:3421) → `autoComputeKyberTargets` (app.js:9057, called at :7395 and :7789) → `parser.js:1349` → the six-rate validator at Code/WCB/WCB_Storage.cpp:1251-1252.

Root cause is better stated as an intra-firmware inconsistency, not a UI oversight: `updateBaudRate` accepts 13 rates (WCB_Storage.cpp:155-157) and `configureMaestro` accepts 13 + 0 (WCB_Maestro.cpp:610-615), but `storeKyberSettings` accepts 6 — with no comment or doc explaining the narrower set.

Preferred fix: widen Code/WCB/WCB_Storage.cpp:1251-1252 to the same 13-rate list `configureMaestro` already uses (the Kyber path calls `updateBaudRate` at WCB_Storage.cpp:1288 anyway, which accepts all 13), so the two config paths that write the same Maestro slot can never disagree. If the six-rate restriction is intentional, it belongs in a comment there and must then be enforced in `configureMaestro` too — otherwise `?MAESTRO` keeps accepting a baud `?KYBER,LOCAL` rejects.

Also add: the rejection makes the config non-convergent. The backup emits only enabled targets (WCB.ino:2838-2853), so the Wizard's diff (parser.js:1341-1344) sees a change forever and re-pushes the rejected `?KYBER,LOCAL` line on every save.

<details><summary>Full verification reasoning</summary>

WHAT I OPENED: Wizard/app.js (lines 1-30, 3200-3300, 3398-3425, 7380-7400, 7778-7795, 8920-9070), Wizard/index.html (614-670), Wizard/parser.js (1330-1360, 1435-1475), Code/WCB/WCB_Storage.cpp (120-175, 1025-1145, 1200-1340, 2030-2075), Code/WCB/WCB_Maestro.cpp (531-620), Code/WCB/WCB_WDP.cpp (690-740), Code/WCB/WCB.ino (2820-2860, 4244-4300).

=== THE CITED LINE IS DEAD CODE — the reviewer's stated mechanism is wrong ===
`appendKyberTargetRow` (Wizard/app.js:8944) opens with `const tbody = document.getElementById(\`b${n}-kyber-target-tbody\`); if (!tbody) return;` (:8945-8946). That element does not exist. `grep -rn "kyber-target-tbody" Wizard/` returns ONLY app.js:8945, :8990, :9008 — nothing in index.html, and nothing in app.js creates it. What index.html actually has is a read-only info div, with the intent stated in the markup comment: `<!-- Kyber Targets (auto-computed from Maestros — shown when LOCAL) -->` at index.html:660, wrapping `b{N}-kyber-targets-wrap` (:661) and `b{N}-kyber-targets-info` (:663) only. `addKyberTargetRow` (app.js:8938) — the sole caller of `appendKyberTargetRow` — has no caller anywhere in Wizard/ (no onclick in index.html either). So the picker at app.js:8966 never renders and no user can ever select a baud from it. That whole block (`addKyberTargetRow`/`appendKyberTargetRow`/`removeKyberTargetRow`/`onKyberTargetsChange`/`syncKyberTargetsToConfig`) is a leftover from a manual targets table that was replaced by auto-computation; `populateKyberTargetsFromConfig` even says so at :9020-9021.

=== BUT THE DEFECT IS REAL VIA A DIFFERENT PATH — and the reviewer's own failure scenario describes it ===
The live path is: Maestro row baud picker → autoComputeKyberTargets → parser → firmware.
1. Wizard/app.js:3253-3255 `const baudOptions = BAUD_RATES.filter(b => b <= maxBaud)` with `maxBaud = (maestro.port >= 3) ? 57600 : Infinity` (:3251). BAUD_RATES is verbatim `[110,300,600,1200,2400,9600,14400,19200,38400,57600,115200,128000,256000]` (app.js:7) — I read it, not the summary. So on S1/S2 all 13 are offered; on S3-S5, 110/300/600/1200/2400 are still offered.
2. `syncMaestrosToConfig` reads that select's value into `config.maestros[].baud` (app.js:3421).
3. `autoComputeKyberTargets` copies it verbatim: `config.kyber.targets.push({ id: m.id, wcb: wcbNum, port: m.port, baud: m.baud })` (app.js:9057). It is called in BOTH push paths — app.js:7395 and :7789.
4. parser.js:1349 emits it unmodified: `cmd += ',' + config.kyber.targets.map(t => \`M${t.id}:W${t.wcb}S${t.port}:${t.baud}\`).join(',')`.
5. Firmware `storeKyberSettings` accepts only six: WCB_Storage.cpp:1251-1252 `(baudRate == 9600 || baudRate == 14400 || baudRate == 19200 || baudRate == 38400 || baudRate == 57600 || baudRate == 115200)` — transcription verified. The else branch (:1327-1328) prints the skip message and `targetIndex++` (:1315) is inside the accepted branch only, so `kyberTargets[…].enabled` stays false — all slots were pre-cleared at :1219-1221.
6. Consequence in `forwardDataFromKyber`: `kyberUseTargeting` is true (set at WCB_Storage.cpp:1106 because params start with "S"), and the targeting branch (WCB.ino:4258-4280) iterates `kyberTargets[]` finding nothing enabled → the sabre stream reaches no Maestro at all. `;M1…` command routing still works, because parser.js sends `?MAESTRO` separately (see its comment at :1450-1457) and `configureMaestro` accepts all 13 rates plus 0 (WCB_Maestro.cpp:609-616). So it is exactly a silent Kyber-routing loss, as claimed.

=== THE UNDERLYING BUG IS AN INTRA-FIRMWARE INCONSISTENCY, NOT A UI OVERSIGHT ===
Three validators in the same firmware disagree on the legal baud set: `updateBaudRate` accepts 13 (WCB_Storage.cpp:155-157), `configureMaestro` accepts 13 + 0 (WCB_Maestro.cpp:610-615), `storeKyberSettings` accepts 6 (WCB_Storage.cpp:1251-1252). No comment, doc, or help text justifies the narrower set — WCB_Help.cpp:225 only shows examples. Nothing in docs/ documents a six-rate Kyber restriction. So it is not a deliberate-and-explained trade-off.

=== AGGRAVATOR THE REVIEWER MISSED ===
The config never converges. The board's backup emits only enabled targets (WCB.ino:2838-2853), so it returns `KYBER,LOCAL,S<port>` with zero targets; the Wizard recomputes the rejected target from the Maestro row on every push, so `kyberChanged` (parser.js:1341-1344, the `JSON.stringify(baseline.kyber?.targets) !== JSON.stringify(config.kyber?.targets)` clause) is true forever and the same rejected line is re-pushed on every save — the exact failure mode parser.js:1450-1457 documents for the sibling ?MAESTRO case.

=== WHY S3, NOT S2 ===
- Requires the user to pick a Maestro baud outside the six. Of the seven offending values, 128000/256000 are not supported by Maestro hardware at all (so the Maestro is dead regardless and the user notices immediately), and 110/300/600/1200/2400 are useless for a servo stream. Real Maestro configs are 9600/57600/115200. The Wizard's own defaults are 115200 (app.js:3234 `addMaestroRow`) and 57600 (app.js:8939, :9014 fallback) — every default is legal.
- Requires a Kyber-local board in the fleet.
- The firmware prints a specific, actionable diagnostic naming the six legal rates (WCB_Storage.cpp:1327-1328), which the Wizard terminal surfaces. Not truly silent at the console — only silent in the UI.
- Partial incidental recovery exists: `reconcileKyberTargetsFromMaestroConfigs` (WCB_Storage.cpp:1043-1069, add-only over ALL maestroConfigs) would re-add the dropped target — but only when fired from WCB_WDP.cpp:718-725, which needs `maestroChanged` from a WDP-learned REMOTE Maestro plus `Kyber_Local && kyberUseTargeting`. A purely local setup gets no recovery.
Real, reachable, user-visible when hit — but a narrow, self-announcing trigger. S3.

</details>

### `Wizard/app.js:9341` — CONFIRMED · S2 → **S3**

**Reducing the slot count leaves activeBoardTab out of range — the Board Identity step renders with no visible panel**

**Correction to the finding as filed:**

The defect is real and the mechanism/lines are accurately reported, but two things need correcting.

(1) Severity: S3, not S2. The bad state is purely visual and one-click recoverable — the tab strip still renders and `wizardSwitchBoardTab` (Wizard/app.js:10545-10553) restores the panel. The hidden panels remain in the DOM, so `wizardSaveStep('identity')` (:10674) and `wizardValidateStep('identity')` (:10803) still read correct values; no bad configuration can reach a board.

(2) The fix's second half is based on a wrong analogy. The reviewer suggests clamping "the way `wizardHTMLSerial` already snaps the tab off a Client slot (:9879-9882)". That snap would NOT catch this case: it tests `(wizardState.boards[wizardState.activeBoardTab]?.type || 'wcb') === 'client'`, and for an out-of-range index `boards[5]` is `undefined`, so `undefined?.type || 'wcb'` yields `'wcb'` and the snap is skipped. The serial step is protected by something else entirely — the unconditional `wizardState.activeBoardTab = 0;` in the dispatcher at :9553. Anyone "fixing" identity by copying the serial snap pattern would ship a no-op. A defensive clamp in `wizardHTMLIdentity` must be a range check (`if (wizardState.activeBoardTab >= wizardState.boards.length) wizardState.activeBoardTab = 0;`), not a type check.

<details><summary>Full verification reasoning</summary>

MECHANISM — every load-bearing fact re-derived from source, not from the summary:

1. `wizardInitBoards(qty)` is at Wizard/app.js:9336. It rebuilds `wizardState.boards` to exactly `qty` entries (`Array.from({length: qty}, (_, i) => prev[i] ?? wizardDefaultBoard(i+1))`, :9338-9340), clamps `kyberBoard` (:9341), and filters orphaned `maestros` (:9351) and `kybers` (:9354). It never touches `activeBoardTab`. Confirmed by reading :9336-9356 directly.

2. Writers of `activeBoardTab` — grep over all of Wizard/ returned matches in app.js only (no other file, and nothing in index.html): the default `0` in `wizardDefaultState` (:9307), the unconditional reset in the step dispatcher `case 'serial': wizardState.activeBoardTab = 0;` (:9553), the Client-snap in `wizardHTMLSerial` (:9879-9882), and `wizardSwitchBoardTab` (:10547). No identity-step reset, no clamp anywhere. The reviewer's list is accurate.

3. Readers: `wizardHTMLIdentity` panels `class="wizard-tab-panel ${i === wizardState.activeBoardTab ? 'active' : ''}"` (:9695), `wizardHTMLSerial` panels (:9942), and `wizardBoardTabs` (:10562).

4. The CSS is the proof the panel actually disappears — Wizard/styles.css:812-813: `.wizard-tab-panel { display: none; }` / `.wizard-tab-panel.active { display: flex; ... }`. With `activeBoardTab` past the end, no panel gets `.active`, so every panel is `display:none`.

5. `wizardHTMLIdentity` returns `${tabs}${panels}${specialPanel}${labelSVG}` (:9827). `labelSVG` (:9816-9822, the `../Images/LabelOnly.jpg` hardware-label photo) and the section title/desc sit outside the panels, so the step renders as title + an unhighlighted tab strip + the photo, with zero fields — exactly as the finding describes.

REACHABILITY — a real user can produce the state:
- `wizardInitBoards` has exactly one caller: `wizardSelectQty(n)` at :10491, which is the `onclick` of every quantity button rendered by `wizardHTMLQuantity` (:9603-9613, both the 1-8 grid and the 9-20 expanded grid).
- `wizardState.boards` has exactly one writer in the whole file (grep for `wizardState.boards =` / `.splice` / `.pop` → only :9338), so truncation only ever happens through that path.
- Back is available on identity: `wizardRenderStep` hides the Back button only at `idx === 0` (:9497); identity is index 2 of `['welcome','quantity','identity','control',…]` (:9365).
- `wizardBack` (:9456) saves the identity step and decrements; nothing resets the tab. `wizardSelectQty` then truncates and calls `wizardAdvanceAfterChoice()` (:10499) → `wizardNext()`. There is no `case 'quantity'` in either `wizardValidateStep` (:10791, cases: control/network/identity/maestro-config) or `wizardSaveStep` (:10650, cases: control/navicore-config/network/identity/serial/kyber-config/firmware/maestro-config/etm) — so nothing clamps on the way back through. Identity then re-renders with a stale `activeBoardTab`.
- Serial is immune because :9553 forces the tab to 0 before `wizardHTMLSerial` runs; identity is the only affected step. The reviewer got that right.

INTENT — not a documented trade-off. `git log -L 9336,9356:Wizard/app.js` shows the shrink-handling block was added in 5c49642 ("bug fixes") specifically to make truncation self-consistent, with a comment reasoning about `maestros`, `kybers`, and `kyberBoard`. `activeBoardTab` was simply overlooked in that same pass. Nothing in CLAUDE.md or docs/ covers wizard tab state.

SEVERITY — this is where the finding overreaches. The failure is a rendering dead-end, not data loss: the tab strip is still rendered and clickable, and one click on any tab calls `wizardSwitchBoardTab('identity', i)` (:10545) which sets `activeBoardTab = i` and toggles the panel classes, fully recovering. The hidden panels still exist in the DOM, so `wizardSaveStep('identity')` (:10674-10700) and `wizardValidateStep('identity')` (:10803-10831) both read correct values from them — no wrong config is ever produced or pushed to hardware. Worst case is a user who reduced the count without ever setting HW versions on the surviving slots clicks Next, gets the toast "Please select a hardware version for every WCB slot." (:10831) with no visible fields to fix — confusing, but self-rescuing via the visible tabs. Recoverable-with-one-click UI glitch behind a Back-then-reduce sequence = S3, not S2.

ADJACENT (separate, not this finding): `wizardSwitchBoardTab` (:10549-10552) toggles by DOM order (`ti`/`pi`) while the onclick passes the boards-array index `i`. On the serial step, client slots are omitted from both the tab strip (:10561) and the panels (:9944), so DOM index != board index whenever a Client precedes a WCB, and the wrong panel is activated. Worth a separate look; it does not affect the identity step, which renders all slots.

</details>

### `Wizard/app.js:9520` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**Re-entering the Connect step after Back discards every later edit and leaves the port panel dead**

**Correction to the finding as filed:**

The defect stands; two details in the report are wrong. (a) "no buttons" — the ✕ cancel button IS rendered and visible on the active row (:10349-10350); the escape is ✕ → 🔌 Reconnect, which the reviewer half-noted. (b) Severity: S3, not S2 — the stale push is uniform across all boards in the session, so it cannot create the split-config/ETM-mismatch class of failure; it loses the user's post-Back edits, which the visible-but-odd dead panel gives them a chance to notice.

Also worth recording (same root cause, not in the report): `connectSeqN` survives the round trip, so re-entering a partially-completed sequence re-renders every finished row's status back to "Waiting…"/"Select port…" — the ✓ Done markers and the banner set imperatively in `wizardSeqAdvance` (:10476) are lost.

Corrected fix: keep the `!connectMode` bootstrap for the first visit, and add an explicit re-entry branch:
  if (key === 'connect') {
    nextBtn.style.display = 'none';
    if (!wizardState.connectMode) { setTimeout(() => wizardSelectConnectMode('seq'), 0); }
    else {
      const n = wizardState.connectSeqN;
      const busy = !!wizardConnectWatchers[n] || boardConnections[n]?.isConnected?.();
      if (!busy) { wizardApplyConfig(); setTimeout(() => _wizPortOpenPanel(n), 0); }
    }
  }
The `busy` guard is the part the reviewer's version is missing, and it is load-bearing — see fix_is_correct.

<details><summary>Full verification reasoning</summary>

MECHANISM — every load-bearing fact re-derived from source, not the summary.

1. The guard is real and transcribed correctly. `Wizard/app.js:9516-9522`:
   `if (key === 'connect') { nextBtn.style.display='none'; if (!wizardState.connectMode) { setTimeout(() => wizardSelectConnectMode('seq'), 0); } }`. The comment above it ("Auto-select sequential mode if not set") documents the auto-select, not any intent to skip re-entry.

2. `wizardApplyConfig` has exactly one call site. `grep -rn wizardApplyConfig Wizard/` returns only :9853 (a comment), :10429 (`if (mode) wizardApplyConfig();` inside `wizardSelectConnectMode`), :10882 (the definition) and :11076 (a comment in `wizardExportConfig`). Nothing in `Wizard/index.html` references it — the only inline handler there is `wizard-back-btn` → `wizardBack()` (index.html:299).

3. `_wizPortOpenPanel` is likewise only reached from three places: `wizardSelectConnectMode` (:10440 seq, :10446 all-at-once), `wizardSeqAdvance` (:10485) and `wizardManualConnect` (:11685). None of them run on a plain re-render of the step.

4. The post-render hook is a no-op. `wizardRenderStep` ends with `if (key === 'connect') wizardStartConnectWatchers();` (:9540), and `wizardStartConnectWatchers` (:11802-11807) has an empty body with a comment explaining that this is deliberate ("The user must always click 'Connect' for each slot"). So nothing else opens the panel.

5. `wizardRenderStep` blows away the whole body (`body.innerHTML = wizardBuildStepHTML(key)`, :9537), and `wizardHTMLConnect` renders the active slot's panel as a literal placeholder: `<div id="wiz-port-panel-${n}" …>${isActive ? '<span …>Loading ports…</span>' : ''}</div>` (:10358-10360). With nothing to replace it, "Loading ports…" is permanent.

REACHABILITY — a real user can produce this.
- `connect` is the last step (`buildWizardSteps`, :9360-9376 → `steps.push('serial','network','etm','review','firmware','connect')`).
- Back is live there: `document.getElementById('wizard-back-btn').style.visibility = idx === 0 ? 'hidden' : ''` (:9498) and nothing else touches `wizard-back-btn` in either file. `wizardBack` (:9455-9462) just saves, decrements, re-renders — it does NOT clear `connectMode`.
- `connectMode` is written in exactly two places: the initial `null` in `wizardDefaultState()` (:9308) and `wizardSelectConnectMode` (:10425). `wizardState` is only reset in `openWizard()` (:9396) — i.e. once per wizard session. So within one session, the second and every later visit to `connect` skips both the apply and the panel open.
- Forward from `firmware` → `connect` is the plain tail of `wizardNext` (:9449-9452).

CONSEQUENCE 1 (stale config) — confirmed. `wizardSaveStep('etm')` (:10777-10786) writes only `wizardState.etmConfig`; nothing else copies wizard state into `boardConfigs`. `wizardApplyConfig` is what builds `boardConfigs[n] = cfg` / `populateUIFromConfig` (:11005-11007) and stamps the general ETM DOM fields (:11013-11024). `_wizPortOpenPanel` snapshots `boardConfigs[n]` + `captureGeneralDOMSnapshot()` (:11555-11558), `wizardWatchForConnect` restores that snapshot and then `boardGo(n)` (:11722-11727) — which reads `boardConfigs[n]` and the general DOM live. So a post-Back edit never reaches the board, and is also silently lost on the main config page.

CONSEQUENCE 2 (dead panel) — confirmed but overstated. The row is not buttonless: `wiz-cancel-btn-${n}` renders with no `display:none` when `isActive` (:10349-10350), so ✕ is visible, and ✕ → `wizardCancelConnect` (:11655) → `wizardEnableConnectBtns` reveals 🔌 Reconnect → `wizardManualConnect` → `_wizPortOpenPanel`. The escape is one extra click, not a lockout — but it does not fix consequence 1, because `wizardApplyConfig` still never re-runs.

INTENT — not deliberate. `git log -L 9516,9524:Wizard/app.js` shows the guard arrived in b6cc31a ("workign on new upload step") purely to replace the old two-branch mode-picker (e98b80d) with auto-select. No comment, doc or CLAUDE.md rule covers re-entry.

SEVERITY — downgraded to S3. It is a real, silent loss of the user's later edits, but the blast radius is contained: because nothing ever re-applies, every board in the session is pushed from the SAME stale `boardConfigs`, so the fleet stays internally consistent (no split password/MAC/ETM group, which is the failure mode CLAUDE.md rule 4 warns about). Combined with a visible ✕/Reconnect escape and a non-primary navigation path, this is moderate, not major.

</details>

### `Wizard/app.js:10550` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**wizardSwitchBoardTab uses the board index against positional DOM indices — clicking a Serial tab hides every panel**

**Correction to the finding as filed:**

Severity is S3, not S2 — the serial step's data path is unaffected (`wizardSaveStep` keys inputs by board index at Wizard/app.js:10704-10733) and the state self-heals on re-entry (:9553 resets `activeBoardTab`, :9879 re-snaps), so the defect is a visual/navigation break only.

The repro is also broader than stated: it does not need 3 slots. Quantity 2 with slot 1 = Client and slot 2 = WCB already breaks it — the single rendered tab carries `i = 1` against a `querySelectorAll` of length 1, so one click on the only tab blanks Step 5.

The finding's proposed fix is only half-right (see fix_is_correct). A complete fix needs an identifier on the tab button too, since `wizardBoardTabs` emits no `id` on it (Wizard/app.js:10562-10565):

  // wizardBoardTabs, :10562
  return `<button class="wizard-board-tab ${i === wizardState.activeBoardTab ? 'active' : ''}"
           data-board="${i}"
           onclick="wizardSwitchBoardTab('${step}',${i})">

  // wizardSwitchBoardTab, :10548-10551
  document.querySelectorAll('.wizard-board-tab').forEach(t =>
    t.classList.toggle('active', Number(t.dataset.board) === i));
  document.querySelectorAll(`[id^="wiz-panel-${step}-"]`).forEach(p =>
    p.classList.toggle('active', p.id === `wiz-panel-${step}-${i}`));

Exact-id comparison (rather than `pi === i`) is also what keeps this correct at quantity 10-20, where the `[id^="wiz-panel-serial-"]` prefix selector matches both `-1` and `-1x`. The identity step keeps working unchanged, since `includeClients` is true there (:10558) and index == position already.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived from source, every claim checked:

1. `wizardSwitchBoardTab(step, i)` (Wizard/app.js:10545-10552) toggles `active` by **enumeration position**: `.forEach((t, ti) => t.classList.toggle('active', ti === i))` at :10548-10549 and `.forEach((p, pi) => p.classList.toggle('active', pi === i))` at :10550-10551. Transcription in the finding is exact.

2. The `i` passed in is the **board array index**: `wizardState.boards.map((b, i) => ... onclick="wizardSwitchBoardTab('${step}',${i})")` at Wizard/app.js:10560-10565.

3. Both producers filter Client slots for non-identity steps: tabs at :10561 (`if (!includeClients && (b.type || 'wcb') === 'client') return '';`, with `includeClients = (step === 'identity')` at :10558), panels at :9887 (`if ((b.type || 'wcb') === 'client') return '';`). Panel ids are board-indexed: `id="wiz-panel-serial-${i}"` at :9942, `id="wiz-panel-identity-${i}"` at :9695. So for `step === 'serial'` the DOM node count is the WCB count while the onclick argument is the board index — they diverge as soon as any Client slot precedes a WCB slot.

4. The visual consequence is real, not theoretical: Wizard/styles.css:812 `.wizard-tab-panel { display: none; }` / :813 `.wizard-tab-panel.active { display: flex; ... }`. No `active` panel = blank step body below the tab strip.

5. Ran the actual filter/toggle logic under node with `boards = [client, wcb, wcb]`: the snap at :9877-9882 sets `activeBoardTab = 1`; tab strip renders 2 buttons carrying `i = 1` and `i = 2`. Click the first (i=1) → active moves to the **second** tab and second panel (wrong tab selected). Click the second (i=2) → zero tabs and zero panels active — Step 5 goes blank. Reviewer's repro reproduces exactly.

6. The **minimum** repro is smaller than the reviewer's: quantity 2, slot 1 = Client, slot 2 = WCB. One tab renders, carrying `i = 1`; `querySelectorAll` returns length 1 (index 0); `0 === 1` is false, so a single click on the only visible tab blanks the step. Any leading Client breaks it.

REACHABILITY — a real user can produce this:
- `wizardSelectQty` allows 1-20 slots (`wizardHTMLQuantity` renders buttons 1-8 plus 9-20; setter at :10490).
- Any slot can be flipped to Client by radio at :9704-9710 → `wizardOnSlotTypeChange(i)` :9854-9858 sets `wizardState.boards[i].type`. No ordering constraint anywhere.
- `wizardValidateStep('identity')` (:10806-10832) checks only ID range 1-99, ID uniqueness, reserved ID 20, and HW version for WCB slots. Nothing requires WCBs to come first.
- `buildWizardSteps()` (:9366-9375) always pushes `'serial'`, so the step is always reached.
- `wizardSwitchBoardTab` is called only from the generated onclick at :10563 — no hits in Wizard/index.html.

INTENT — not a deliberate trade-off. `git log -L` shows `wizardSwitchBoardTab` was written in 45d3aff, when every slot was a WCB and position == index. The Client filter was added later in b862a0a ("WCB aliases + slot-type (WCB vs WCB_Client)"), which rewrote `wizardBoardTabs` to filter but left `wizardSwitchBoardTab` untouched. The author was aware of the index hazard at *render* time — the comment at :9877-9878 "Snap activeBoardTab off any Client slot before rendering so the tab strip and panels stay in sync" — but only patched the render path, not the click path. Classic half-fixed regression.

WHY S3, NOT S2 — no data is lost or corrupted:
- `wizardSaveStep('serial')` (:10704-10733) reads `wiz-b${i}-s${p}-baud` / `-label` by **board index**, and hidden panels still hold their inputs, so all serial config saves correctly even while the step looks broken.
- It self-heals: `wizardBuildStepHTML` :9553 resets `activeBoardTab = 0` on every entry to the step, and :9879 re-snaps off a Client, so Back→Next restores a correct view.
So the damage is confined to Wizard navigation on one step for one slot ordering — user-visible and genuinely broken-looking, but recoverable and non-destructive. S3.

</details>

### `Wizard/app.js:10822` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**Identity validation hardcodes ID 20 instead of the configurable NaviCore ID, allowing a controller/board ID collision**

**Correction to the finding as filed:**

The observation is right; the mechanism, the reachability and the fix all need correcting.

1. Primary harm is a DUPLICATE ESP-NOW MAC, not just a shared table index. WCB.ino:7317 (`WCBMacAddresses[i][5] = i + 1`) + :7328 (own MAC = WCBMacAddresses[WCB_Number-1]) and WCBClient/src/WCB_Client.cpp:1598 use the same derivation, so controller-ID N and WCB N are byte-identical on the air. The anti-spoof check at WCB.ino:3752 cannot tell them apart. Secondary: WCB.ino:6908 / :6946 refuse the real board at that ID regular peer membership.

2. The guard at app.js:10822 is ALREADY a no-op on the normal forward path, even for the default 20. `wizardValidateStep` runs only from `wizardNext` (app.js:9419) for the current step, `wizardBack` (:9456) does not validate, and step order is welcome/quantity/identity/control/navicore-config (:9366-9370) while `useSpecialPeer` is false until the control step (:9315, :10531, :10659). It fires only after back-navigation to identity. The reviewer's scenario reaches a broken config, but so does the plain ID-20 case they assumed was covered.

3. Correct fix is NOT the identity-step one-liner alone. Add a `case 'navicore-config':` to `wizardValidateStep` (no such case exists today — only control, network, identity, maestro-config) that rejects a chosen NaviCore ID already used by a board, since that is the step where the colliding value is actually entered and where the board IDs are already known:

  case 'navicore-config': {
    const idv = parseInt(document.getElementById('wiz-navicore-id')?.value);
    if (!isNaN(idv) && wizardState.boards.some(b => b.wcbNumber === idv))
      return `ID ${idv} is already used by a board — the controller needs an ID no board occupies.`;
    break;
  }

Then ALSO de-hardcode the identity check for the back-nav path (`const spId = wizardState.navicoreId || 20; ... ids.includes(spId)`), and interpolate the live ID into the UI strings at :9801, :9967, :10389, :10393 (and the comment at :9797).

<details><summary>Full verification reasoning</summary>

TRANSCRIPTION CHECK — every cited fact re-derived, all accurate:
- Wizard/app.js:10822-10823 is verbatim `if (wizardState.useSpecialPeer && ids.includes(20))` → 'ID 20 is reserved for the special slot…'. Literal 20, confirmed.
- Wizard/app.js:9983-9985 `const id = wizardState.navicoreId || 20; Array.from({length: 20}, (_, i) => i + 1)` — a live 1..20 dropdown, confirmed.
- Wizard/app.js:10663-10664 `if (!isNaN(idv) && idv >= 1 && idv <= 20) wizardState.navicoreId = idv;` confirmed.
- Wizard/app.js:10922 and :11080 both `cfg.specialPeerId = ws.navicoreId || 20;` per non-client board; :11036 sets systemConfig.general.specialPeerId the same way.
- Wizard/parser.js:1281 `add(curSpecial ? \`CONTROLLER,ON,${config.specialPeerId ?? 20}\` : \`CONTROLLER,OFF\`)` confirmed; parser.js:519 accepts 1..20.
- Board IDs are free-entry `min="1" max="99"` number inputs (app.js:9720 WCB, :9774 Client), validated only as 1..99 + unique (:10818, :10820). So a board at any ID 1..20 is trivially reachable.

MECHANISM — real, and materially worse than the reviewer described. The reviewer said the controller "shares the single ID-indexed peer/board table". True (boardTable[id-1]), but the actual primary harm is a duplicate ESP-NOW MAC. Code/WCB/WCB.ino:7312-7317 derives `WCBMacAddresses[i][5] = i + 1`, and :7328 sets this board's own STA MAC to `WCBMacAddresses[WCB_Number - 1]`. WCBClient/src/WCB_Client.cpp:1598 uses the identical scheme (`_wcbMACs[i][5] = (uint8_t)(i + 1)`). So a NaviCore on controller ID 5 and a WCB numbered 5 transmit with byte-identical MAC 02:oct2:oct3:00:00:05. The anti-spoof guard at WCB.ino:3752 (`if (info->src_addr[5] != senderWCB) return;`) cannot separate them — both legitimately carry last octet 5. Additionally WCB.ino:6908 (`addActivePeer`) and :6946 (`addTemporaryPeer`) both hard-reject `id == WCB_SPECIAL_PEER_ID`, so the real board at the colliding ID is refused regular membership, while WCB.ino:1054/1199/3766/7022 all fold the special peer into the same `boardTable[id-1]` / peer slot. `enableControllerPeer` (WCB.ino:6816) guards only `id < 1 || id > MAX_WCB_COUNT` — no collision check anywhere in firmware.

INTENT — supports the finding rather than refuting it. Wizard/app.js:9796-9797 comment: "Renders only when wizardState.useSpecialPeer is set. Always a client (no WCB lives at ID 20 by design)". The design intent is exactly that no WCB occupies the special ID; the 10822 check is the enforcement of that intent, and hardcoding 20 defeats it for every non-default ID. The hint text at :9990 explicitly invites the change ("Only change this if your NaviCore is configured for a different ID"), so this is a supported, first-class option, not an unsupported edge.

REACHABILITY — the reviewer's conclusion holds but they missed a bigger adjacent hole. `wizardValidateStep` is called from exactly one place, `wizardNext` at app.js:9419, for the CURRENT step only; `wizardBack` (:9456-9463) does not validate. Step order is `['welcome','quantity','identity','control']` then `navicore-config` (app.js:9366-9370) — identity comes BEFORE control. `useSpecialPeer` defaults to false (:9315) and is only set true in `wizardSetControl` (:10531) and `wizardSaveStep('control')` (:10659), both AFTER identity. Consequence: on a normal forward run the guard at 10822 never fires at all — not even for the default ID 20. It only fires if the user navigates back to identity and clicks Next. There is also no `case 'navicore-config':` in `wizardValidateStep` (the only cases are control, network, identity, maestro-config), and the review step (:10230-10232) just prints "peer ID N" with no cross-check. So nothing anywhere catches the collision.

SEVERITY — downgrading S2 → S3. The defect is a missing validation that lets a user author a bad config, not a defect that breaks a correct one; the default path (ID 20, boards 1..N) is unaffected; and it requires a deliberate non-default ID that happens to coincide with a board number. The eventual field symptom (duplicate MAC, silently half-broken mesh) is genuinely nasty and hard to diagnose, which keeps it above cosmetic.

Cosmetic follow-on, also real: the "ID 20" strings at app.js:9801, :9967, :10389, :10393 are hardcoded and become factually wrong once navicoreId != 20.

</details>

### `Wizard/app.js:10931` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**wizardApplyConfig never writes boardConfigs[n] for Client slots — slot type and client alias are silently dropped**

**Correction to the finding as filed:**

The defect stands, but the description and severity need correcting on three points. (a) The client slot does NOT lose password/MAC/command chars: onGeneralPasswordChange/onGeneralMacChange/onGeneralCmdCharChange (Wizard/app.js:1136-1186) run at :10899-10901 over `for (const n in boardConfigs)` after renderBoards at :10888, so those land on the default object. What is dropped is type, alias, clientAlias, the client's chosen ID (b.wcbNumber), hwVersion/statusLedPin and serial baud/labels. (b) "Push All targets a board that doesn't exist" is wrong — boardGoAll (:8253-8262) only pushes slots with a live connection or a remote-relay mapping, which a client slot never has; nothing is pushed. (c) Impact is Wizard-only (parser.js:32-36 — type/clientAlias are never sent to firmware) and partially self-healing via upsertClientCard (:12537-12563), which flips the slot to client and seeds the alias once the client is heard over WDP. Real residual harm is the lost user-typed ID/alias, a phantom full WCB card (permanent when the client's ID differs from its wizard slot index, in which case discovery also creates a second, correct card at slot=ID), and a config-file export that writes the slot as [WCB n]. That makes it S3, not S2. Correct fix: wrap the Kyber/Maestro/ETM block (:10933-11003) in `if (cfg.type !== 'client') { … }` and leave `boardConfigs[n] = cfg; populateUIFromConfig(n, cfg); onBoardFieldChange(n);` as the last statements of the callback — the same shape already shipped for wizardExportConfig at :11086-11091.

<details><summary>Full verification reasoning</summary>

MECHANISM (verified, not transcribed): Wizard/app.js:10882 `wizardApplyConfig` sets cfg.type/alias/clientAlias at :10919-10921, hits `if (cfg.type === 'client') return;` at :10931, and the store block `boardConfigs[n] = cfg; populateUIFromConfig(n, cfg); onBoardFieldChange(n);` is at :11005-11007 — after the return, inside the same `ws.boards.forEach` callback opened at :10904. So client slots never get their cfg stored. `renderBoards(ws.quantity)` at :10888 has already run reconcileBoardGrid → addBoardSection, which seeds `boardConfigs[n] = WCBParser.createDefaultBoardConfig(); boardConfigs[n].wcbNumber = n;` (:616-618); parser.js:25-36 confirms that default carries `type:'wcb'`, `clientAlias:''`, `wcbNumber:1` (overwritten to n). Wizard/index.html:454-465 shows the card template's slot-type radio is `value="wcb" checked` with the client pane (`b{N}-client-pane`, index.html:470) `display:none`. Net: the user's typed client ID (read into b.wcbNumber at app.js:10681-10683) and Client Alias (:10687) are silently dropped, and the slot renders as a full WCB card.

REACHABILITY: `wizardApplyConfig` has exactly one caller — `wizardSelectConnectMode` at :10429 (`if (mode) wizardApplyConfig();`), which the wizard auto-invokes with 'seq' (:10312-10316 comment). Client slots are user-selectable in Step 2 (radio `wiz-slot-type-${i}`, :9705-9710) and quantity counts them (:10212-10214). So an ordinary run reaches it. No other writer repairs it: greps for `boardConfigs[...].type =` find only :1974 (manual UI radio, onSlotTypeChange) and :12552 (`upsertClientCard`).

CORRECTIONS TO THE REVIEWER'S CLAIMS: (1) "blank default config (no password/MAC from the wizard)" is FALSE — `onGeneralPasswordChange` (:1136-1141), `onGeneralMacChange` (:1144-1152) and `onGeneralCmdCharChange` (:1176-1186) each loop `for (const n in boardConfigs)` and are called at :10899-10901, AFTER renderBoards at :10888, so the client slot's default object does receive password, macOctet2/3, delimiter, funcChar, cmdChar. What is actually lost is type, alias, clientAlias, the client's chosen wcbNumber, hwVersion/statusLedPin and serial baud/labels. (2) "Pushing Push All ... targets a board that doesn't exist" is FALSE — `boardGoAll` (:8253-8262) builds its slot list only from `boardConnections[n].isConnected()` and `remoteRelayForBoard`; a wizard client slot has neither, so nothing is pushed to it.

BLAST RADIUS / SEVERITY: parser.js:32-36 marks `type` and `clientAlias` as "Wizard-only metadata (never sent to firmware)" — grep of parser.js confirms neither is serialized by buildCommandString (only `alias` → ?ALIAS at :1255-1262, which is empty for a client slot). So no wrong bytes reach hardware and no board is misconfigured. It also partially self-heals: `upsertClientCard` (:12537-12563) sets `cfg.type='client'`, calls updateSlotTypeUI and seeds clientAlias from the WDP advert once the client is heard on the mesh. Residual harm: typed data silently lost until then; a permanent phantom "WCB n" card (plus a duplicate real Client card at slot=ID) when the client's ID differs from its wizard slot index; and the config-page export (:8417-8433) writes that slot as a [WCB n] block with the slot index instead of the client's ID. Real, user-visible, trivially fixable — but browser-tool-only, non-destructive and self-healing in the common case, so S3 rather than S2.

FIX CHECK: the primary variant ("move the boardConfigs[n]=cfg / populateUIFromConfig / onBoardFieldChange block above the client early-return") would break WCB slots. `populateUIFromConfig` (:2923-2983+) renders wcbNumber, serial baud/labels (:2966-2973) and the Kyber mode radio/port/targets (:2975-2983) straight from cfg; running it before the :10933-11003 block means every Kyber-local board's card would show mode 'none', the un-mirrored Kyber baud (:10940), and no Marcduino port/labels (:10943-10950), plus no maestro table or ETM. (`boardConfigs[n] = cfg` alone would be harmless — same object reference — and `onBoardFieldChange` is just :1483-1486, notify + 'unsaved' badge; it is populateUIFromConfig that regresses.) The parenthetical alternative in the finding — wrapping :10933-11003 in `if (cfg.type !== 'client') { … }` and leaving the store block last — is correct and mirrors the already-shipped export fix at :11086-11091 (commit 5c49642).

GIT EVIDENCE: `git log -L 10925,10935:Wizard/app.js` shows the return added by b862a0a "WCB aliases + slot-type (WCB vs WCB_Client) in config tool and Wizard" with the routing-only comment; `git log -L 11085,11092:Wizard/app.js` shows the identical copy in wizardExportConfig introduced by the same commit and repaired in 5c49642 ("Return the metadata-only cfg so it's still in the exported file"). So the apply path is the unfixed twin, not a deliberate trade-off.

</details>

### `Wizard/app.js:11748` — CONFIRMED · S1 → **S3** · ⚠ PROPOSED FIX IS WRONG

**Connect & Push always reports "✓ Done", even when the push never happened or the board never came back**

**Correction to the finding as filed:**

The observation stands but the stated mechanism and the fix both need correcting.

MECHANISM: drop the "boardGo hits `!conn?.isConnected()` at 6997" story — that early return is unreachable from `wizardWatchForConnect`, because no `await` exists between the watcher's gate (11700) and that check (6997). The real triggers are (a) the background `conn.reconnect(15, 2000)` at 7532 failing so the 30 s poll at 11723 exits disconnected, (b) all three verify pulls failing so `boardBaselines[n]` is still null at 11747, (c) the abort at 7404 when `getVariablesFromUI()` returns null, and (d) `_pushFullyAcked === false` (7473/7489) never reaching the wizard.

FIX: do NOT change boardGo's return type to a success/failure result. `boardGo` already returns `_needsReboot` (7608), and `boardGoAll` consumes it at 8301 — `const needsReboot = await boardGo(n, { skipReboot: true }); if (needsReboot) relayRebootList.push(n);`. Repurposing the return value silently breaks Stage 3/4 of Push All, so relay boards either never get their queued reboot (config applied but not committed) or all reboot unnecessarily. The "or throw" variant is worse: boardGo is invoked from an inline `onclick="boardGo({N})"` (Wizard/index.html:496) and from several un-awaited call sites (app.js:1493, 7205, 7360, 9189), so throwing yields unhandled promise rejections — and the throw would have to be placed after boardGo's own catch at 7601, which today converts every push error into a toast.

Correct shapes, either of:
- Widen the return to `{ ok, needsReboot }` and update the one consumer at 8301 to `.needsReboot`; or
- Leave the return alone and record the outcome in a module-level map, e.g. set `boardPushOutcome[n] = { ok: _pushFullyAcked, aborted: false }` next to the badge write at 7572, `{ ok:false, aborted:true }` at each early return (6997, 7022, 7029, 7404), and `{ ok:false }` in the catch at 7601. Note `{ ok:true }` for the 7420 "No changes to push" return — that is a legitimate success, not a failure.

Then gate 11748 on all four signals: push not aborted AND `_pushFullyAcked` AND the reconnect poll ended connected AND `boardBaselines[n]` non-null after verify — else `wizardSetConnectStatus(n, 'err', …)` with a message naming which stage failed. Distinguishing "pushed but could not verify" from "never pushed" is worth doing, since only the latter means the board is unconfigured.

Worth fixing in the same pass: boardGo's early returns at 6997/7022/7029/7404/7420 all return `undefined`, which `boardGoAll:8301` cannot distinguish from `_needsReboot === false` — an existing latent hole in Push All's relay-reboot staging.

<details><summary>Full verification reasoning</summary>

DEFECT IS REAL — verified line by line.

`Wizard/app.js:11748` is `wizardSetConnectStatus(n, 'ok', '✓ Done');`, sitting at the end of the try block with no guard. I read 11688–11760. Both preceding branches are dead ends for the outcome:
- 11723–11727: the reconnect poll `for (let i = 0; i < 60 && !boardConnections[n]?.isConnected(); i++) await sleep(500);` — exits after ~30 s whether or not the board returned; the loop result is never tested afterwards.
- 11729–11747: verify is wrapped in `if (boardConnections[n]?.isConnected())`, and the three `boardPull(n)` attempts (8 s + 5 s + 5 s) re-check `!boardBaselines[n]`, but `boardBaselines[n]` is never consulted after the last attempt.
Execution falls straight to 11748.

The reviewer's claim that the `catch` at 11749 is effectively unreachable is CORRECT, and I confirmed both halves:
- `boardGo` has its own `catch (e)` at app.js:7601 (`showToast(\`Push failed: ${e.message}\`)`) wrapping the entire configure path from 7377, and it does not rethrow. It ends `return _needsReboot;` at 7608.
- `boardPull` likewise swallows everything in its own `catch (e)` (toast + termLog, no rethrow) — I read it at `async function boardPull` + ~160 lines. Its not-connected early return is *deliberately* quiet for background pulls: "Only a USER-initiated pull (Pull Config button → {manual:true}) shows the error." The wizard calls bare `boardPull(n)`, so `opts.manual` is unset.
So nothing the wizard awaits can throw for a real push/verify failure.

REACHABILITY — the reviewer's specific scenario is WRONG, but others are live.
Their scenario (USB re-enumerates, `boardGo` hits `!conn?.isConnected()` at 6997 and returns) is NOT reachable from this path. JS is single-threaded, and there is no yield between the watcher's gate at 11700 (`isConnected() && boardBaselines[n]`) and boardGo's check at 6997: I grepped 11700–11720 and the only `await` is `await boardGo(n)` itself, and `wizardEnableConnectBtns` (11648), `populateUIFromConfig` (2923) and `restoreGeneralDOMSnapshot` (8159) are all plain `function`, none awaited. A disconnect event cannot be dispatched in that window.

Genuinely reachable false-"✓ Done" paths:
1. DOMINANT — reboot path. The wizard forces `boardBaselines[n] = null` (11712) for a full push, so a wizard push reliably trips `commandStringNeedsReboot`. boardGo then closes the port and fires a background `conn.reconnect(15, 2000)` (app.js:7532). If the board never re-enumerates, `reconnected` is false, and the watcher's 30 s poll exits disconnected → verify skipped → green "✓ Done".
2. Verify-fail. Board reconnects but never answers `?backup`; `boardBaselines[n]` stays null after all three retries → "✓ Done" anyway.
3. Push aborted outright at 7404 (`getVariablesFromUI()` returned null on an invalid variable name): boardGo returns before building any command string, the board stays connected, the verify pull succeeds, and the row goes green having pushed *nothing*.
4. `_pushFullyAcked` (declared 7473, cleared 7489 when a command gets no response even after retry) already exists as a real failure signal and feeds only the badge at 7572 — the wizard never sees it.

CONSEQUENCE is as claimed: `wizardSetConnectStatus` (11762) calls `wizardSeqAdvance(n)` on 'ok' (11769), which advances to the next board and can set the banner to "✅ All boards configured!" (10469); `wizardCheckAllDone` then auto-closes the wizard 2.5 s later.

INTENT — not deliberate. The comment at app.js:7527–7529 states the opposite of the behaviour: "In wizard context, wizardWatchForConnect polls isConnected() and waits for this background reconnect to finish before marking ✓ Done." `git log -L 11744,11752:Wizard/app.js` shows 11748 unconditional since it was introduced (45d3aff), with later commits (ce97085, 6704593 "update to fix mac os flashing") adding the reconnect poll and the verify retries *around* it without ever wiring an outcome. Drift, not a documented trade-off.

WHY S3, NOT S1. This is a status-reporting bug in a no-build-step browser tool: no crash, no data loss, no corruption of board state, and the user can re-pull at any time. The dominant path (1) is not silent — `showToast(\`WCB ${n} did not come back — reconnect manually\`, 'error')` fires in the reconnect IIFE, and `showToast` (9238) forces error toasts to `Math.max(duration, 12000)`, so it outlives the wizard's 2.5 s auto-close; termLog and the 'error' board badge (7572) also fire on some paths. That materially undercuts the "undetectable" framing S1 rested on. S2 is defensible on the strength of paths 2 and 3, which are quiet, and because a board that silently missed its network push is then silently ignored by the whole mesh (CLAUDE.md rule 4 — ETM is all-or-nothing), which is an expensive debug. But given a loud persistent toast on the common path, S3 is the honest call.

</details>

### `Wizard/app.js:12610` — CONFIRMED · S2 → **S3**

**The 12 s mesh-discovery poll injects ?WDP,DUMP into an in-flight config push, faking ACKs for dropped settings**

**Correction to the finding as filed:**

Mechanism, location and line numbers are right; two refinements. (1) The title overstates the rate: the fake ACK only causes loss when the config command was independently dropped — for a command the board actually answered, the extra lines merely extend the quiet window and are harmless. Severity is S3, not S2. (2) The proposed fix is placed too late. Setting `_pushBusy` "before the for loop at 7481" leaves the bootstrap char-change loop at :7445-7462 exposed, and the code's own comment there (:7449-7450) says those DELIM/CMDCHAR/FUNCCHAR commands "gate every command that follows, so a dropped one breaks the whole push" — a fake ACK there is worse than in the main loop. Set the flag immediately after the `try {` at :7377 instead. Also note there is currently no `finally` on that try (it is `try` :7377 … `catch` :7601-7604 only), so the fix must add one (or clear the flag after the catch at :7605, which the swallowing catch makes equivalent) — a bare `_pushBusy = true` with no clear-on-all-exits latches and kills mesh discovery for the page lifetime. Finally, the same "any line = ACK" / shared-`_dataCallbacks` exposure exists in `boardGoRemote` (:7769, relay push, where the relay IS typically the first connected board) and in `boardPull`'s `sendAndCollect` (:6800/:6804); a flag set only in `boardGo` leaves those open. A per-connection busy mutex honoured by the tick, or making `sendAndAwaitIdle` match the command's own echo rather than "any line", closes the whole class instead of one caller.

<details><summary>Full verification reasoning</summary>

MECHANISM — every load-bearing fact re-derived from source, none taken from the reviewer's summary.

1. The tick and its guards are exactly as described. `meshAutoDiscoverTick` starts at Wizard/app.js:12608 and guards only three things: `_meshDiscoverBusy` (:12609), `_statsFetchBusy` (:12610, comment "don't inject ?WDP,DUMP into an in-flight ETM/stats capture"), and `_otaInProgress.size > 0` (:12611). It then does `await t.conn.sendAndCollect(`${t.fc}WDP,DUMP`, 4000, '[WDP:END')` (:12616). The timer is page-lifetime: `setInterval(meshAutoDiscoverTick, 12000)` at :12672 and a first sweep via `setTimeout(..., 4000)` at :12673 (reviewer's line numbers check out). There is no push guard and no pull guard anywhere — grep for `Busy` in Wizard/app.js returns only `_relayRouteAllBusy` (:367, a double-click guard), `_statsFetchBusy` (:11814, set :11973, cleared :12071) and `_meshDiscoverBusy` (:12529).

2. Target selection. `_wdpMeshConn()` (:12458-12467) walks `Object.entries(boardConnections)` and returns the FIRST connected one. `boardConnections` is keyed by integer slot, so iteration is ascending numeric — for a single-board setup, or the first board of a Push All, that is the board being pushed. `boardGo` uses `const conn = boardConnections[n]` (:6996), the identical object.

3. The ACK test really is "any line at all". `sendAndAwaitIdle` (:5267-5303) registers `onLine` into the per-connection `this._dataCallbacks` (:5299) and resolves `{ ok: lines.length > 0, lines }` (:5288). `_handleLine` dispatches every non-RC-noise line to ALL registered callbacks (`this._dataCallbacks.forEach(cb => cb(line))`), and `_suppressTerminalLine` is display-only and runs afterwards (its own comment at :8788 says so, and the call sites are :5390/:5400, downstream of the dispatch). So the DUMP's reply lines land in whatever `sendAndAwaitIdle` is pending on that same connection.

4. The injection always produces lines. Firmware `printWdpDump` (Code/WCB/WCB_WDP.cpp) unconditionally prints `[WDPCFG:EN=%d,AUTOJOIN=%d,PEERS=%d]` and `[WDP:END,count=%d]` (:1189-1191) even with an empty neighbor table — a minimum of two lines. It is read-only (no NVS write). `?WDP,DUMP` dispatch is WCB_WDP.cpp:1207.

5. Consequence in the push loop (:7481-7493): `let r = await conn.sendAndAwaitIdle(cmd, {quietMs:250, hardTimeoutMs:5000}); if (!r.ok) retry; if (!r.ok) _pushFullyAcked = false;`. A genuinely dropped config command sits in its 5 s hard window; if the 12 s tick fires inside that window, the DUMP lines make `ok` true, so the retry never runs and `_pushFullyAcked` stays true. The board then gets badge 'configured' + success toast (:7571-7575) instead of the 'error' badge and "some settings may not have applied" toast. The follow-up verify pull (:7580) does `boardConfigs[n] = config; boardBaselines[n] = deep-copy; populateUIFromConfig(n, config)` (:6914-6920) plus a "Config pulled" success toast, so the dropped setting silently snaps back to the board's old value with no warning anywhere. The comment at :7565-7570 ("let boardPull() set it from the board's real post-push state so any dropped setting stays a visible pending diff") shows the author intended the mismatch to remain visible; because the pull rewrites the UI too, the `_pushFullyAcked` error path is in practice the ONLY user-visible signal — and that is exactly what the fake ACK erases.

REACHABILITY — real. `boardGo` is wired to `onclick="boardGo({N})"` (Wizard/index.html:496) and driven by the wizard at app.js:7147, :7189, :7268, :7345, and by `boardGoAll` (:8297). Nothing clears the interval during a push (`clearInterval` hits at :6658/:9381/:11665/:11680/:11702/:11753/:11852 are all unrelated watchers/timers).

INTENT — not a deliberate trade-off. `git log -S"_statsFetchBusy"` gives one commit, 4a524c1 "Wizard: fix Network Test capture + add cross-tab port sharing (Phase 2)" — i.e. the identical collision was found and fixed for the stats capture and simply never extended to the push. So this is an oversight with an in-repo precedent for the remedy.

WHY I DOWNGRADE TO S3, not S2. The defect is real but is second-order: it needs an independent command drop to do damage. Writes are serialized per connection by `_sendChain` (:5215-5227), so the injected `?WDP,DUMP` cannot splice into a config command's bytes, and the DUMP is 10 bytes of read-only traffic that the board only services once its handler returns — the poll does not itself cause the drop. With a successful command the injected lines only restart the 250 ms quiet timer (bounded by the 5 s hard cap), which is harmless. So the harm chain is: (a) a config command is genuinely dropped — the very thing the ACK pacing was added to make rare, (b) the tick lands inside that command's 5 s window (~40% given a 12 s period), (c) the pushed board is the lowest-numbered connected one. Impact when it does hit is one setting silently unapplied with a false "configured", recoverable by re-pushing, and the UI ends up showing the board's true state. Real, worth the three-line fix, but not the "faking ACKs" routine event S2 implies. Also worth noting the reciprocal direction the reviewer missed: the push's own response lines pollute the tick's `sendAndCollect` buffer, and if the 4 s DUMP timeout trips mid-roster the partial parse passes the `if (!parsed.nodes.length) return` guard (:12618) and can flap client cards Offline / `removeClientCard` a TEMPORARY peer — cosmetic and self-healing on the next sweep.

</details>

### `Wizard/flasher.js:414` — CONFIRMED · S2 → **S3**

**Recognized-but-unsupported chips (ESP32-S2/C2/C3/C5/C6/C61/H2/P4) are flashed with ESP32 or S3 firmware whenever a HW version is selected**

**Correction to the finding as filed:**

Mechanism and location stand as written. Four corrections:

1. Severity S2 → S3 (recoverable via ROM download mode; requires non-WCB silicon; never endangers WCB hardware or saved config).

2. "the dropdown must be filled" is wrong. Wizard/index.html:536 offers `<option value="0">— Select —</option>` as the default, and boardGo explicitly tolerates the unset case (app.js:7043-7052, "HW version is a hint, not a gate"), in which the refusal at flasher.js:405-412 correctly fires. The mis-flash requires the user to have actively selected a HW version (or to have pulled a config from a real WCB earlier onto that same card).

3. "brick" overstates it. Nothing writable can disable the ESP mask-ROM loader; the board is non-booting until re-flashed, not destroyed.

4. The HW 3.1/3.2 sub-case is already blocked for the common 4 MB C-series dev board: no 8/16 MB S3 bootloader matches, `bootBlockReason` is set (flasher.js:429-440) and the guard at :602-612 aborts. That guard is scoped to `imagesToFlash === flashImages`, so an app-only "Update FW" still writes an S3 app at 0x10000 — the fix is still needed, just for a narrower set of paths than claimed.

Preferred fix shape (stronger than the proposed deny-list): make the fall-back conditional on the chip not having identified itself at all, rather than enumerating bad parts — `if (chipName && /^ESP/i.test(chipName)) { refuse } else { fall back to hwVersion }`. That fails closed for future parts (an "ESP32-R1" would slip through `/^ESP(8266|32[-_](S2|C\d+|H\d+|P\d+))/i`), keeps the fall-back only for the genuinely-unreadable `chipName === ''` case that :384's `?? String(chip ?? '')` exists to cover, and matches the invariant already written at :381-382. Either way, update the comment at :381-382 so it describes what the code now does.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived, not taken from the summary.

1. Allowlist (Wizard/flasher.js:383-389). I ran the two real regexes in node against every `CHIP_NAME` the vendored loader can produce (`grep -o 'CHIP_NAME="[^"]*"' Wizard/vendor/esptool-js/esptool-js-0.4.7.bundle.js` → ESP32, ESP32-C2/C3/C5/C6/C61, ESP32-H2, ESP32-P4, ESP32-S2, ESP32-S3, ESP8266). Result: `detectedType` is `ESP32` only for "ESP32"/"ESP32-D0WD-V3"/"ESP32-PICO-D4", `ESP32S3` for "ESP32-S3", and **null for all nine other names**. The reviewer's transcription of the allowlist behaviour is exact.

2. Null handler (flasher.js:404-414). Verified by reading, not summary: the only refusal inside the `!detectedType` branch is gated on `if (!hwVersion)` (:405-413). With a truthy `hwVersion`, execution falls through :414 with `binaryType` still equal to `WCBParser.HW_VERSION_MAP[String(hwVersion)]?.binary` (:327). `HW_VERSION_MAP` (Wizard/parser.js:196-203) maps 1/21/23/24→ESP32 and 31/32→ESP32S3 — no entry can ever produce a variant-correct value.

3. Nothing downstream re-checks chip family. `bootAddr` is derived from `binaryType`, not from the chip (:426). For an ESP32 selection, `fw.boot.single` is unconditionally used (:443-446) and `flashImages` = boot@0x1000 + part@0x8000 + app@0x10000 (:450). The two later guards only cover missing images (:588) and S3 bootloader flash-size (:602) — neither knows the chip family. esptool-js 0.4.7 does no chip-ID validation either: the bundle contains no "unexpected chip id" string, and `_updateImageFlashParams` only inspects the image when the address equals `chip.BOOTLOADER_FLASH_OFFSET` and otherwise just warns. The call uses `flashSize:'keep'`, `eraseAll:false` (:679-682), so the write proceeds and completes.

4. Comment vs code. flasher.js:381-382 states the invariant "Variants (S2, C3, C6, H2, P4, …) must NOT fall through to the classic ESP32 binary." `git blame` shows :414 predates it (f15433a 2026-06-11) and the strict allowlist + `!hwVersion` refusal were added the next day (7c5a746 2026-06-12, message just "bug fixes"). The 2026-06-12 commit enforced the invariant for only one of the two null sub-cases and left :414 in place. So this is a half-applied intent, not a documented trade-off — nothing in CLAUDE.md or docs/ argues for trusting the HW dropdown over a chip that positively identified itself.

REACHABILITY — real but narrow.
- Single caller: Wizard/app.js:7084 inside `boardGo`, passing `boardConfigs[n]?.hwVersion` (:7049). No other caller (`grep -rn flashFirmware` → only app.js:7084 and the definition).
- The Flash/Go button `b{N}-btn-go` lives inside `b{N}-section-body` (Wizard/index.html:486,496), which `updateSlotTypeUI` hides for `type === 'client'` (app.js:1952-1965) — so WCB_Client slots cannot reach this path at all.
- No WCB hardware revision uses a C/H/P/S2 part (HW_VERSION_MAP: 1/2.x = ESP32, 3.x = ESP32-S3), so triggering requires the user to physically attach foreign silicon to a WCB slot card whose HW dropdown is set.
- Partial existing mitigation the reviewer missed: with HW 3.1/3.2 selected, `binaryType` is ESP32S3 and a bootloader is only chosen for exactly 8 or 16 MB flash (:429-432); a typical 4 MB C3/C6 dev board sets `bootBlockReason` and the guard at :602-612 aborts the full flash. The unguarded mis-flash is therefore the ESP32-family selections (HW 1/2.1/2.3/2.4, any flash size) plus 8/16 MB variant boards under HW 3.x — and the app-only "Update FW" mode in either case, since it filters to 0x10000 and bypasses that guard.

SEVERITY — downgraded to S3. The outcome is a foreign dev board whose bootloader/partition/app regions are overwritten with the wrong family, so it stops booting; the mask-ROM download loader is not writable on any ESP32 variant, so BOOT+RST re-flash always recovers it. No WCB board, no user NVS/config, and no mesh state is at risk, and the terminal does log the fall-back (:414). Real defect, contradicts its own stated invariant, cheap to fix — but not S2.

</details>

### `Wizard/flasher.js:686` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**Flash progress bar stalls at ~62% — esptool reports COMPRESSED bytes but the denominator is the uncompressed total**

**Correction to the finding as filed:**

The defect is real and the location/mechanism/numbers are correct; the severity and the headline fix are not.

Severity: S3 (cosmetic progress reporting), not S2. The "user unplugs mid-write and bricks the board" scenario does not hold — at the 62% freeze all regions are already written and the earlier files are already MD5-verified; only the app's MD5 read-back plus flashDeflFinish/reset remain. The under-report is also in the fail-safe direction.

Correct fix (this is the doc's own second alternative, not its headline one) — weight each file's compressed fraction by that file's uncompressed size, using the file index esptool already supplies and `flasher.js` currently discards as `_fileIdx`. `fileArray` is built by `imagesToFlash.map(...)` at :678, so the index the bundle passes (`s` in `for(let s=0;s<t.fileArray.length;s++)`) indexes `imagesToFlash` directly:

    let rawDone = 0;
    ...
    reportProgress: (fileIdx, written, total) => {
      // esptool reports COMPRESSED bytes per file (compress:true); weight each
      // file's fraction by its UNCOMPRESSED size so the sum matches totalBytes.
      const w = imagesToFlash[fileIdx]?.buf.byteLength ?? 0;
      onProgress(rawDone + (total ? w * written / total : 0), totalBytes);
      if (written === total) rawDone += w;
    },

This keeps the byte units used at :662, :698 and :717, stays monotonic, and ends at exactly `totalBytes`. Zero-length images are `continue`d by the bundle with no callback, but their weight is 0, so `rawDone` stays consistent.

<details><summary>Full verification reasoning</summary>

MECHANISM — verified from source, not the summary.

1. `Wizard/flasher.js:644` — `const totalBytes = imagesToFlash.reduce((sum, img) => sum + img.buf.byteLength, 0);` is the sum of **uncompressed** ArrayBuffer sizes (images assembled at :629 otadata 0x2000, :634 nvs 0x5000, plus boot/part/app, sorted at :642).

2. `Wizard/flasher.js:683` passes `compress: true`; `:684-688` is
   `reportProgress: (_fileIdx, written, total) => { onProgress(bytesWritten + written, totalBytes); if (written === total) bytesWritten += total; }`.

3. I dumped the vendored bundle (`Wizard/vendor/esptool-js/esptool-js-0.4.7.bundle.js`, byte offset ~69000, `writeFlash`) and the reviewer's transcription is accurate. The relevant sequence is: `const n=e.length;` (uncompressed) … `if(t.compress){const A=this.bstrToUi8(e); e=this.ui8ToBstr(Ce(A,{level:9})), h=await this.flashDeflBegin(n,e.length,i)}` — **`e` is reassigned to the compressed string** — then `let r=0,g=0; const B=e.length;` and both `t.reportProgress(s,0,B)` and, in the block loop, `g+=E.length; t.reportProgress(s,g,B)` where `E=this.bstrToUi8(e.slice(0,this.FLASH_WRITE_SIZE))` is a slice of the **compressed** stream. So `written` and `total` handed to `:684` are compressed counts. Numerator compressed, denominator uncompressed — confirmed.

4. ARITHMETIC redone myself with node zlib level 9 against the actual committed binaries in `Code/bin/` (I did not take the reviewer's numbers): boot32 25184→16515, part 3072→145, app32 1284464→803458 (62.6%), appS3 1250352→789256 (63.1%), 8 KB 0xFF otadata→31, 20 KB 0xFF nvs→43. Totals: ESP32 full 820149/1320912 = **62%**; ESP32 factory 61%; app-only update 803489/1292656 = **62%**; S3 full/app-only **63%**. My independent numbers match the reviewer's exactly.

5. REACHABILITY — `flashFirmware` is called only from `Wizard/app.js:7084` (the Flash / Update FW / Factory Reset button path in `boardGo`), with `onProgress: (written,total) => updateFlashBar(n,written,total)`. `updateFlashBar` (`app.js:9099`) is `const pct = total > 0 ? Math.round(written/total*100) : 0`, written straight into `b{N}-flash-bar` width (`index.html:509`), into `setFlashStatus(n, "Flashing… ${pct}%")` and mirrored into the Wizard connect row. No clamp, no rescale anywhere. The non-compressed branch of the bundle throws (`"Yet to handle Non Compressed writes"`), so `compress:true` is not optional. `bytesWritten` is declared inside `attemptWrite` (:676) so the retry at :700 resets cleanly. Every scenario a user can produce lands at 61–63%.

6. INTENT — no comment or doc anywhere acknowledges this. `git log -L 675,692:Wizard/flasher.js` shows the line was written this way at file creation (423d8df, 2026-02-27) and only re-indented into `attemptWrite` by 5c49642; the only comment is "esptool-js resets written/total per file; accumulate for overall progress", which is about the per-file reset, not about compression. Not a documented trade-off.

WHY I DOWNGRADE TO S3 — the observation is right, the impact story is not:
- The stall is **not** a dangerous window. The bar reaches 62% only on the **final** `reportProgress` of the **last** file (app @0x10000, last after the ascending-address sort at :642). At that instant every byte of every region is already on flash; what remains is that file's `flashMd5sum` verify, `flashBegin(0,0)` + `flashDeflFinish`, then the hard reset. A user who unplugs there does **not** get "the half-written bootloader/partition state" the reviewer claims — boot/part/otadata were written and MD5-verified several files earlier. The frozen window is the MD5 read-back of ~1.28 MB, i.e. seconds, not the 40 s implied.
- The error is in the **safe** direction: the bar always under-reports, so a user is told less progress has been made than actually has — they wait longer, not less.
- There is a second, correct live signal throughout the write: `flasher.js:345-349` routes esptool's terminal into `onLog`, and the bundle's `info()`→`write()`→`terminal.writeLine()` chain means each block emits `[esptool] Writing at 0x… (n%)` with a correct per-file percent. `termLog` does not run `_suppressTerminalLine` (that filter is on the RX path, `app.js:5390/5400`), so those lines are visible.

Net: a real, always-reproducible cosmetic defect in the primary progress indicator of a 1–2 minute operation aimed at non-technical users (and it also puts two disagreeing percentages on screen — log says 100%, bar says 62%). Worth fixing, not S2.

</details>

### `Wizard/index.html:640` — CONFIRMED · S2 → **S3**

**Kyber Maestro Baud <select> cannot represent 5 of the 13 firmware-legal baud rates — a pulled 19200 blanks the control and Push silently rewrites the port to 115200**

**Correction to the finding as filed:**

Three corrections, none fatal to the finding.

1. TITLE ARITHMETIC IS WRONG. "5 of the 13" should be **9 of the 13**. 13 legal rates (WCB_Storage.cpp:155-158) minus the 4 offered = 9 missing: 110, 300, 600, 1200, 2400, 14400, 19200, 128000, 256000 — which is precisely the list the reviewer's own fix section enumerates. The body is self-consistent; only the headline number is off.

2. SEVERITY S2 → S3. The trigger cannot be produced through the Wizard's own UI: `updatePortClaimUI` disables `b{n}-s{p}-baud` for a `kyber`-claimed port (app.js:786-792), and the firmware ships all five ports at 9600 (WCB.ino:622-626). Reaching the bad state needs a terminal `?BAUD,Sx,<rate>` or an imported/hand-edited backup. Blast radius is one config field, the symptom is partly visible (blank dropdown), and it is recoverable. Compare the sibling MP3 findings F-193/F-194 — those fire on values the dropdown offers by default and are correctly S2; this one should sit below them.

3. THE FAILURE SCENARIO UNDERSTATES IT IN ONE WAY. It says "the user changes something unrelated … and clicks Push Config". No unrelated change is required. app.js:7418-7425 builds the diff and only aborts on an empty command string; a bare Pull → Push emits `?BAUD,S2,115200` on its own, because the corrupted field is itself the diff. Worth fixing in the write-up since it removes the "user had to do something else first" mitigation a reader might infer.

Also worth adding to the fix section: the *root* overwrite is the hardcoded `|| 115200` fallback in `syncKyberToConfig` (app.js:2044) and `onKyberBaudChange` (app.js:967). Changing it to `|| config.serialPorts[portVal - 1]?.baud || 115200` stops the data loss on its own and is robust against any future rate the option list still cannot show.

<details><summary>Full verification reasoning</summary>

I re-derived every load-bearing fact rather than trusting the write-up. The mechanism is real end to end.

OPTION LIST. Wizard/index.html:640-645 is exactly as claimed: `<select id="b{N}-kyber-baud" onchange="onKyberBaudChange({N})">` with four options — 9600 (641), 38400 (642), 57600 (643), 115200 selected (644). `git log -L 638,646:Wizard/index.html` shows the block was added in 45d3aff with these same four options and only ever had its <label> text touched (c6701b5, 7283365). No comment anywhere defends the curation.

NEVER REPOPULATED. `grep -n "kyber-baud" Wizard/app.js` returns only 863, 907-908, 954-955, 967, 2044, 2985, 2998-2999 — every hit is a `.value` read/write or a `-wrap` display toggle. No `innerHTML`, no `appendChild`, no `new Option`. Contrast the siblings: `updateKyberPortDropdown` (app.js:979, `portSel.innerHTML = ''` at :985) and `updateKyberMarcPortDropdown` (app.js:1000, :1006) both rebuild. The reviewer's line numbers for those are right.

FIRMWARE-LEGAL SET. Code/WCB/WCB_Storage.cpp:155-158 `updateBaudRate()` accepts 110, 300, 600, 1200, 2400, 9600, 14400, 19200, 38400, 57600, 115200, 128000, 256000 — I counted 13. The Wizard's own `BAUD_RATES` (app.js:7) is that identical 13-element list, and the serial table renders all 13 for S1/S2 (app.js:737 `const maxBaud = p >= 3 ? 57600 : Infinity`; :738 filter). So 9 of 13 are unrepresentable in the Kyber select, not 5.

VALUE SOURCE. WCB.ino:2804 emits `BAUD,S<i>,<baudRates[i]>` for all five ports in the config backup. parser.js:566-572 stores it with no whitelist (`config.serialPorts[portIdx].baud = parseInt(parts[2]) || 9600`, :570). parser.js never writes `kyber.baud` at all (grep confirms: parser touches only kyber.mode/port/marcduinoPort/targets), which is exactly why app.js:2994-2999 derives the select from the serial port — its own comment says "Derive kyber baud from the serial port baud (populated from ?BAUD commands in backup)". So the port baud IS the intended source of truth for this control, and it can legally hold a value the control cannot show.

BLANKING + REWRITE. Setting `select.value` to a string no `<option>` carries sets selectedIndex to -1 and makes `.value` return '' (HTML spec; `baudSel.value = 19200` coerces to "19200", matches nothing). Then app.js:2044 `parseInt('') || 115200` → 115200, and :2048 writes it over `config.serialPorts[portVal-1].baud`. `syncKyberToConfig` runs on both push paths — app.js:7390 (direct/USB) and app.js:7782 (relay) — and in both it runs AFTER `syncSerialUIToConfig` (7388/7780), so it clobbers the correct 19200 that :2908 had just read out of the (disabled, but still JS-readable) serial-table select. parser.js:1313-1316 then diffs `cur.baud` 115200 against the pulled baseline 19200 and emits `BAUD,S2,115200`.

STRONGER THAN THE REVIEWER STATED: no user edit is needed. app.js:7418-7425 shows the push is not gated on a dirty flag — it builds the diff and only bails `if (!cmdString)`. A bare Pull → Push with zero user changes produces a non-empty diff consisting solely of the corrupted `?BAUD`.

REACHABILITY — this is what caps the severity. Firmware defaults are 9600 on all five ports (WCB.ino:622-626 `SERIALn_DEFAULT_BAUD_RATE 9600`, :634-640), so a default board never hits this. And the Wizard cannot create the state itself: once a port carries a `kyber` claim, `updatePortClaimUI` disables `b{n}-s{p}-baud` (app.js:788). The precondition therefore requires `?BAUD,S2,19200` typed at a terminal, or a hand-edited/imported backup. 19200 is a legitimate rate for this link — WCB_Maestro.cpp:612 and WCB_Storage.cpp:1251 both list 9600/14400/19200/38400/57600/115200 as valid Maestro/Kyber-target baud — so it is plausible, just not common. There is also a partial visual cue (the dropdown renders empty). Real bug, narrow trigger, one field, recoverable → S3, not S2.

NOT DELIBERATE. The 4-value list is repeated in the setup wizard (`const kyberBaudRates = [9600, 38400, 57600, 115200]`, app.js:10021) with the hint "115,200 current · 57,600 older" (app.js:10047), so the curation is intentional — but nothing anywhere makes the *derived-from-serial-port* write safe against a value outside it. Every other baud picker in the tool is built from a list at runtime and clamped (Maestro app.js:3251-3253 and :3328-3336; WLED :2781-2784; serial table :737-739). The Kyber baud select is the only static one, and it is the one fed from an unvalidated firmware value.

Note for the parent: this finding is F-192 in docs/CODE_REVIEW_2026-08_wave3.md:1489 and has no verdict recorded in docs/CODE_REVIEW_2026-08_verdicts.md yet.

</details>

### `Wizard/index.html:809` — CONFIRMED · S2 → **S3**

**MP3 baud picker offers values the firmware rejects outright, discarding the whole MP3 config**

**Correction to the finding as filed:**

The defect is real; the consequence is overstated. Corrections:

1. There is no phantom-configured UI state. `commandStringNeedsReboot` (app.js:8185-8196) returns false for `?MP3`, so the push takes the no-reboot branch, which deliberately does NOT advance the baseline (comment at app.js:7566-7571) and calls `boardPull(n)` 300 ms later (app.js:7580). `boardPull` overwrites `boardConfigs[n]`/`boardBaselines[n]` from the real `?backup` and re-runs `populateUIFromConfig` (app.js:6914-6919), so the MP3 section reverts to un-configured. The real symptom is a green "WCB n configured" toast (app.js:7574 — `_pushFullyAcked` stays true because the board replies, just with an error) immediately followed by the user's MP3 settings silently reverting on the verify pull, with the reason printed in the terminal (`_suppressTerminalLine`, app.js:8781-8791, does not filter `[MP3] …`).

2. The durable damage the finding misses is the more interesting half: `syncMP3ToConfig` (app.js:2308-2310) copies `config.mp3.baud` into `config.serialPorts[port-1].baud`, and `parser.js:1314` emits `BAUD,S<p>,<baud>` ahead of the MP3 line. `updateBaudRate` (Code/WCB/WCB_Storage.cpp:145-180) accepts all 13 rates and persists to NVS key `Serial<p>`. So the port baud change lands and survives reboot while the MP3 config is discarded — the port is left at a rate the MP3 Trigger cannot speak, and only a manual `?BAUD,S<p>,9600` undoes it.

3. The sharpest sub-case is buried in the finding and deserves top billing: the Wizard's own soft-serial clamp produces an invalid value with no user error. `app.js:2239-2241` sets `baudSel.value = '57600'` when MP3 moves to S3-S5 from anything above 57600 — and 57600 is rejected by BOTH `WCB_MP3.cpp:287` and `:292`. Likewise a valid 38400 moved to S3 is left untouched (38400 ≤ 57600) and then blocked by `:292`.

4. Minor: the "Apply" block begins at WCB_MP3.cpp:340, first state write `mp3Config.serialPort` at :357 (not :341 — :341-343 is the `updateBaudRate` call). Conclusion unchanged.

5. Also worth folding into the same fix: mirror `_hcrApplyBaudCap`'s extra step (app.js:2484-2488) — sync `config.serialPorts[portVal-1].baud` and the `b{n}-s{port}-baud` select at clamp time, not only at push time, so the serial table never displays a baud contradicting the MP3 line.

<details><summary>Full verification reasoning</summary>

MECHANISM — verified line by line, every number re-read from source.

`Wizard/index.html:809` is `<select id="b{N}-mp3-baud" onchange="onMP3BaudChange({N})">` and options `810-822` are exactly `110,300,600,1200,2400,9600(selected),14400,19200,38400,57600,115200,128000,256000` — 13 values, matching `BAUD_RATES` at `Wizard/app.js:7`. Transcription is correct.

Firmware, re-read directly: `Code/WCB/WCB_MP3.cpp:287-290` is `if (baudRate != 9600 && baudRate != 38400) { Serial.println("[MP3] MP3 Trigger only supports 9600 or 38400 baud"); return; }`, and `:292-304` is `if (serialPort >= 3 && baudRate > 9600) { ...\"❌ CONFIGURATION BLOCKED!\"... return; }`. The `// ---- Apply ----` block starts at `:340`; the first state write is `mp3Config.serialPort = ...` at `:357`, `mp3Config.configured = true` at `:360`. Both guards return well before any of that, so the whole `?MP3,S<p>:<b>:V<v>` line is discarded — port, baud and volume together. Reviewer's "assignments do not start until :341" is off by one on the label but correct in substance.

Command construction: `Wizard/parser.js:1364` is `add(\`MP3,S${config.mp3.port}:${config.mp3.baud}:V${config.mp3.volume}\`)`, gated only on a JSON diff of `config.mp3` (`parser.js:1358-1360`). No whitelist anywhere between the select and the wire — grep for `38400` across `Wizard/*.js` + `index.html` returns only `app.js:7` (BAUD_RATES), `app.js:2658` (WLED_BAUD_RATES), `app.js:10021` (kyberBaudRates), and three `<option>` lines. Nothing validates MP3 baud.

REACHABILITY — a real user can trivially produce it. `onMP3BaudChange` (app.js:2251-2256) writes whatever is selected straight into `config.mp3.baud`; `syncMP3ToConfig` (app.js:2304) re-reads the same select. The only constraint is `onMP3PortChange` (app.js:2231-2242): `opt.hidden = isSoftSerial && parseInt(opt.value) > 57600` and a clamp `baudSel.value = '57600'`. That cap is the wrong number and it makes things worse, not better — 57600 is rejected by BOTH firmware guards, so the Wizard's own automatic clamp hands the user an invalid value. Concretely: pick 115200 (offered), move MP3 Port to Serial 3 → Wizard silently sets 57600 → `?MP3,S3:57600:V0` → blocked. Pick 38400 (a valid rate on S1/S2) and move to S3 → 38400 ≤ 57600, no clamp fires → blocked by `:292`. `populateUIFromConfig` (app.js:3047-3053) repeats the same 57600 cap.

Compounding, and the part that actually persists: `syncMP3ToConfig` (app.js:2308-2310) mirrors `config.mp3.baud` into `config.serialPorts[port-1].baud`, and `parser.js:1314` emits `BAUD,S<p>,<baud>` for it. `updateBaudRate` (`Code/WCB/WCB_Storage.cpp:145-180`) accepts all thirteen rates, applies them live and writes NVS key `Serial<p>`. So on a push the port baud change SUCCEEDS and survives reboot while the `?MP3` line is thrown away — the port ends up running at a rate the MP3 Trigger cannot speak, with no MP3 configured on it.

INTENT — this is drift, not a deliberate trade-off. Every sibling picker was reconciled with its firmware: HCR `index.html:860-866` offers exactly 9600/19200/38400/57600/115200, matching `WCB_HCR.cpp:630-632`, and `_hcrApplyBaudCap` (app.js:2468-2490) caps S3-S5 at >9600, matching `WCB_HCR.cpp:635` — with a comment at :2481-2483 explaining it also syncs `config.serialPorts[].baud` "so buildCommandString doesn't emit a contradictory ?BAUD". Kyber offers only 9600/38400/57600/115200 (`index.html:640-645`); DFPlayer has no baud select at all (tooltip `index.html:876`: "Baud is fixed at 9600 by the module"). MP3 is the only one left on the generic 13-value list, and `git log -L 809,822:Wizard/index.html` shows it unchanged since `ee11659 "updates for adding mp3 trigger."` (Mar 2026) — i.e. it predates the HCR reconciliation and was never revisited. No comment anywhere defends the wide list.

WHY S3 AND NOT S2 — the reviewer's stated failure narrative is factually wrong on its most damaging clause. It claims "The Wizard does not parse that reply, clears the unsaved badge and updates its baseline, so the UI shows the MP3 Trigger as configured on S2 while the board has no MP3 device at all." That is not what the code does:
- `commandStringNeedsReboot` (app.js:8185-8196) returns false for MP3, so the push takes the no-reboot branch.
- app.js:7566-7580 carries an explicit comment: "Do NOT advance the baseline optimistically; let boardPull() set it from the board's real post-push state so any dropped setting stays a visible pending diff instead of being masked as 'No changes to push'", followed by `setTimeout(() => boardPull(n), 300)`.
- `boardPull` (app.js:6914-6919) does `boardConfigs[n] = config; boardBaselines[n] = <deep copy>; populateUIFromConfig(n, config)` from the board's real `?backup`. The MP3 checkbox un-ticks itself; the UI does NOT show a phantom configured MP3.
- The rejection text `[MP3] MP3 Trigger only supports 9600 or 38400 baud` is not filtered — `_suppressTerminalLine` (app.js:8781-8791) matches only `[WDP*:`, `"sys":1`, fragment/bulk envelopes and a fixed `"type":"…"` list. The user sees the reason in the terminal.
So it is self-correcting and diagnosable, and the default selection (9600) is valid. What's left is a genuine but moderate defect: a picker that offers 11 values the firmware provably rejects, an auto-clamp that produces a 12th rejected value on its own, a misleading green "WCB n configured" toast (app.js:7574 — `_pushFullyAcked` stays true because the board DOES reply, just with an error), and a persisted wrong port baud. That is S3, not S2.

</details>

### `Wizard/parser.js:902` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**The Wizard drops ?WDP,OFF / ?WDP,AUTOJOIN,OFF from a pulled config and never re-emits them — an export/restore silently re-enables discovery**

**Correction to the finding as filed:**

Three corrections. (a) Severity is S3, not S2 — a reverted two-boolean preference, visible in the Wizard's WDP panel and undone by one command. (b) The failure scenario is narrower in one direction and wider in another: an omitted token cannot flip a board whose NVS already holds OFF, so a pull→push within a session is harmless; but the loss does NOT require a dead board — `?ERASE,NVS` (Wizard/app.js:7341) and the factory-reset flash both blank NVS to defaults-ON and the follow-up full push carries no WDP token. (c) Line citation: the firmware comment starts at WCB.ino:2887, not 2886 (emits at :2892-2893 are exact); parser.js:902 is `case 'CHK':`, the `default:` that swallows the token is :904-909. Also worth stating: `?WDP,AUTOJOIN,OFF` is not terminal-only — it has a Wizard button (app.js:12389 → wdpAutoJoinToggle at app.js:12497), which makes the OFF state easy to reach by accident.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived end to end, not taken from the finding.

1. Firmware side. `collectConfigCommands()` (Code/WCB/WCB.ino:2762) is the documented "SINGLE SOURCE OF TRUTH for the WCB config command list" (comment WCB.ino:2749-2760). Its WDP block is at WCB.ino:2887-2893 — comment starts at :2887 (reviewer wrote 2886-2893; off by one at the start), emits are exactly `if (!wdpEnabled) emit("WDP,OFF", true);` / `if (!wdpAutoJoin) emit("WDP,AUTOJOIN,OFF", true);` at :2892-2893. Both callers drive it: `printBackupConfig()` (WCB.ino:5762, the `?backup` serial chain) and `buildConfigString()` (WCB.ino:2953, the always-'?' string the Wizard reads over the ESP-NOW relay pull). So `?WDP,OFF` genuinely rides every config chain the Wizard can pull.

2. The state is real, persistent, and firmware-restorable. `wdpEnabled`/`wdpAutoJoin` default true (WCB_WDP.cpp:48-49); NVS namespace "wdp_cfg", keys "en"/"autojoin" (WCB_WDP.cpp:1287-1291, loaded :1281-1285 with `getBool(...,true)` defaults). Both key names are well under the 15-char NVS limit. `?WDP,OFF` and `?WDP,AUTOJOIN,OFF` are dispatched — WCB.ino:4941 `if (rootUpper == "WDP") { processWdpCommand(args); return; }` → WCB_WDP.cpp:1218-1219 (ON/OFF) and :1226-1236 (AUTOJOIN,ON|OFF), each calling `saveWdpSettings()`. So a restore chain containing the token would be accepted; nothing gates it on ETM.

3. Wizard side — the gap is real. `grep -n WDP Wizard/parser.js` returns exactly one hit: a comment at :506 (about PEERSLIVE). I read parser.js:471-912 directly; the switch cases are HW, RELAY, LED, WCB, WCBQ, WCBCH, PEERSLIVE, CONTROLLER/SPECIAL, ALIAS, EPASS, MAC, DELIM, FUNCCHAR, CMDCHAR, BAUD, LABEL, BCAST, KYBER, MP3, HCR, DFP, WLED, MAESTRO, ETM, SEQ, VAR, MAP, CHK. No `case 'WDP'`. `case 'CHK':` is at :902 (the reviewer's cited line) and `default:` at :904-909 → `console.debug('[WCB Parser] Unknown token:', body)`. There is no unknown-token retention anywhere: `parseBackupString` builds a fresh `createDefaultBoardConfig()` (parser.js:20+, no wdp field) and only fills recognised cases.

4. The loss propagates to the export. `exportSystemFile()` (Wizard/app.js:8405) rebuilds the file from the config OBJECTS — `WCBParser.buildSystemFile(systemConfig)` (parser.js:1548) → `buildCommandString(...)` (parser.js:1220-1541) — it does not re-emit the raw pulled chain. `grep -n "function export" Wizard/app.js` shows exportSystemFile is the only export path. `buildCommandString` never emits any WDP token, so the exported .txt has no WDP line.

5. I cross-checked whether the Wizard is simply a lossy model (which would make this a design stance, not a bug). It is not: I enumerated every `emit(` in collectConfigCommands (WCB.ino:2776-2893 → HW, LED,PIN, MAC,2/3, WCB, ALIAS, WCBQ, WCBCH, PEERSLIVE, EPASS, DELIM, FUNCCHAR, CMDCHAR, BAUD, LABEL, BCAST, KYBER/MAESTRO,REMOTE, ETM,*, CONTROLLER, WDP, SEQ, MAP) plus the helper emitters (MAESTRO/MP3/DFP/HCR/WLED/VAR). Every single one has a parser case except WDP. It is the sole orphan.

INTENT — the opposite of a deliberate omission. The firmware comment at WCB.ino:2887-2891 exists specifically to make this survive a restore, and git blame (`git log -L 2886,2894`) shows it added in f7ab40e with that rationale. The identical pattern is already implemented Wizard-side for the analogous UI-less boolean `broadcastToS0`: declared parser.js:60-61 ("Round-tripped through backup import/export … No UI toggle"), parsed at :594-597, emitted at :1334-1336, diffed at :1624. Nothing in parser.js, app.js or docs/ claims WDP enable/auto-join is live-only; the only explicit live-only stance is PEERSLIVE (parser.js:504-509, `emit(..., false)` at WCB.ino:2795).

REACHABILITY — broader than "board dies". `?WDP,AUTOJOIN,OFF` has a first-class Wizard button: `wdpAutoJoinToggle()` at app.js:12497 wired from the mesh-panel button at app.js:12389 (the reviewer's supporting text elsewhere cited 2497 — wrong, it is 12497). And the state is lost without any replacement board: `?ERASE,NVS` (app.js:7341 `boardBaselines[n] = null; // force full push — board NVS is blank`) and the factory-reset flash (`eraseNvs`, Wizard/flasher.js) both return NVS to defaults ON, and the follow-up full push carries no WDP token. Conversely, a pull→push inside one session does NOT flip a board that already holds OFF — an omitted token cannot change NVS — so the finding's "silently re-enables" only bites when NVS is at defaults.

SEVERITY — real but S3, not S2. Blast radius is two booleans; the failure is a reverted preference (discovery and auto-join come back ON, board starts advertising and adopting neighbours), it is plainly visible in the Wizard's own WDP panel (`[WDPCFG:EN=..,AUTOJOIN=..,PEERS=..]` parsed at app.js:12325, rendered at :12375+), and it is undone by one command. No data loss, no crash, no wire-format break — unlike the S2 neighbours in this corpus where a whole board config is lost.

Note: I found docs/CODE_REVIEW_2026-08_verdicts.md:3038-3066 contains a prior wave's verdict on this same finding. I derived everything above from source independently before reading it; it agrees (CONFIRMED, S3), which I treat as corroboration rather than evidence.

</details>

### `Wizard/parser.js:905` — CONFIRMED · S2 → **S3**

**Parser drops `?WDP,OFF` and `?WDP,AUTOJOIN,OFF` from a board backup — a config export/restore silently re-enables WDP discovery**

**Correction to the finding as filed:**

The defect stands, with three corrections. (a) Severity is S3, not S2 — a reverted two-boolean preference, visible in the Wizard's WDP panel and undone by one command. (b) "The same loss happens on any pull → push cycle within one session" is false: an omitted token cannot flip a board that already holds OFF in NVS; the loss requires default NVS (replacement board, `?ERASE,NVS` at Wizard/app.js:7299-7345, or a factory-reset flash with `eraseNvs` at Wizard/flasher.js:631-635 followed by the post-flash push at app.js:7143). (c) Two cited line numbers are off: the firmware comment is WCB.ino:2887-2891 (not 2886-2890) and `wdpAutoJoinToggle()` is Wizard/app.js:12497 (not 2497) — worth noting because the auto-join half of the setting has a real Wizard button (app.js:12389), which makes the state far easier to reach than "terminal-only" implies.

Preferred fix shape: copy the existing `broadcastToS0` precedent verbatim rather than the "emit only the OFF forms" variant — add `wdpEnabled: true, wdpAutoJoin: true` to `createDefaultBoardConfig()` (parser.js:20), a `case 'WDP':` in `parseToken()` setting them from `WDP,ON|OFF` and `WDP,AUTOJOIN,ON|OFF`, emits mirroring parser.js:1335-1336 (`if (fullPush || baseline?.wdpEnabled !== config.wdpEnabled) add('WDP,' + (config.wdpEnabled ? 'ON' : 'OFF'))`, same for AUTOJOIN), and two `check(...)` rows in `diffConfigs` (parser.js:1624 area). Emitting the explicit ON form is safe — the firmware accepts `?WDP,ON` / `?WDP,AUTOJOIN,ON` at WCB_WDP.cpp:1218 and :1228 — and it fixes the reverse asymmetry the reviewer's version leaves open: with OFF-only emission, a full push of a wdpEnabled=true config onto a board whose NVS still says OFF (the ordinary non-erasing reflash path, app.js:7058 with `boardBaselines[n] = null`) silently leaves the board disabled.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived, not taken from the finding:

1. Firmware side. `collectConfigCommands()` (Code/WCB/WCB.ino:2762) ends its network block with the comment at WCB.ino:2887-2891 ("WDP settings — both default ON, so only the OFF states need to ride the config chain (otherwise a restore silently reverts a deliberate ?WDP,OFF / ?WDP,AUTOJOIN,OFF back to ON on the replacement board)") followed by `if (!wdpEnabled) emit("WDP,OFF", true);` / `if (!wdpAutoJoin) emit("WDP,AUTOJOIN,OFF", true);` at WCB.ino:2892-2893. (Reviewer cited the comment as 2886-2890 — actually 2887-2891; the emit lines 2892-2893 are exact.)

2. Both consumers carry it. `collectConfigCommands` is driven by `printBackupConfig()` (WCB.ino:5762, serial `?BACKUP`) and by `buildConfigString()` (WCB.ino:2953, the always-'?' string the Wizard reads on a config pull — see the header comment at WCB.ino:2749-2760 naming it the single source of truth for both). So the OFF tokens genuinely reach the Wizard on every pull, over serial and over the relay.

3. The settings are real and persistent. `wdpEnabled`/`wdpAutoJoin` default true (Code/WCB/WCB_WDP.cpp:48-49), are set by `?WDP,ON|OFF` (WCB_WDP.cpp:1218-1219) and `?WDP,AUTOJOIN,ON|OFF` (WCB_WDP.cpp:1228-1229), each calling `saveWdpSettings()` → NVS namespace "wdp_cfg", keys "en"/"autojoin" (WCB_WDP.cpp:1287-1291), reloaded at boot by `loadWdpSettings()` (:1281-1285). Dispatch of the token exists: `if (rootUpper == "WDP")` at WCB.ino:4941 — so a restore chain containing `?WDP,OFF` would be accepted.

4. Wizard side. `parseToken()` switch runs Wizard/parser.js:471-903 (cases HW, RELAY, LED, WCB, WCBQ, WCBCH, PEERSLIVE, CONTROLLER/SPECIAL, ALIAS, EPASS, MAC, DELIM, FUNCCHAR, CMDCHAR, BAUD, LABEL, BCAST, KYBER, MP3, HCR, DFP, WLED, MAESTRO, ETM, SEQ, VAR, MAP, CHK). I read 860-912 directly: there is no `case 'WDP'`, and `?WDP,OFF` lands on `default:` → `console.debug('[WCB Parser] Unknown token:', body)` at parser.js:905-910. `grep -n "WDP" Wizard/parser.js` returns exactly one hit — a comment at :506 about PEERSLIVE — so no field holds it, `createDefaultBoardConfig()` (parser.js:20-…) has no wdp field, and `buildCommandString()` (parser.js:1220-1541) never re-emits it. No unknown-token retention exists anywhere in the parse path (`parseBackupString` parser.js:222 builds a fresh default config and only fills recognised cases).

5. Export is a rebuild, not a passthrough. `exportSystemFile()` (Wizard/app.js:8405) calls `WCBParser.buildSystemFile(systemConfig)` (parser.js:1548), which regenerates each section with `buildCommandString(board, null, true, {outputFuncChar:'?', outputDelim:'^'})` (parser.js:1580-1583). Nothing that was not parsed can survive. Re-import via `parseSystemFile` (parser.js:1114) → `parseBackupString` closes the loop.

REACHABILITY — the finding's scenario is not the strongest one, and the state is easy to produce:
- Producing the state: `?WDP,OFF` is terminal-only, but `?WDP,AUTOJOIN,OFF` has a first-class Wizard control — `wdpAutoJoinToggle()` at Wizard/app.js:12497 wired to the mesh-panel button at app.js:12389 (reviewer wrote "app.js:2497" — wrong, it is 12497). So a user can reach the OFF state with a click, and the panel even presents it as a persistent board property ("OFF — peer list is pinned").
- Losing it without a dead board: the Wizard's own factory-reset flash writes a blank NVS image (`eraseNvs` → 0x9000, Wizard/flasher.js:300-302, :631-635) and the post-flash path then pushes the pre-flash snapshot back (app.js:7058, :7143). Same for `?ERASE,NVS` (app.js:7299-7345 → firmware `eraseNVSFlash()`, WCB.ino:5068-5072), which explicitly sets `boardBaselines[n] = null; // force full push — board NVS is blank`. In both, NVS returns to defaults ON and the full push carries no WDP token, so a deliberate opt-out is silently reverted — no replacement board needed.

INTENT — this is the opposite of a deliberate omission. The firmware comment at WCB.ino:2887-2891 was written specifically to make this state ride the config chain, and the identical pattern is already implemented on the Wizard side for the analogous UI-less boolean `broadcastToS0` (parser.js:60-61 "Round-tripped through backup import/export … No UI toggle", parse at :595, emit at :1335-1336, diff at :1624). WDP is simply the one token the parser never caught up with. Nothing in parser.js, app.js or docs/ claims WDP enable/auto-join is live-only state (the only "live-only" stance is PEERSLIVE, parser.js:504-509).

SEVERITY — real, but S3 not S2. Blast radius is two booleans; the failure is a reverted preference (discovery/auto-join come back ON), fully visible in the Wizard's own WDP mesh panel, and recoverable with one command (`?WDP,OFF`, plus `?WDP,CLEAR` for peers learned in the meantime). No data loss, no crash, no wire-format break. Compare the S2 neighbours in the same corpus (e.g. F-197, two boards collapsing into one on save/reload — an entire board config lost).

One claim in the finding is overstated and should not be repeated: "The same loss happens on any pull → push cycle within one session." It does not. A push simply omits the WDP tokens, so a board whose NVS already says OFF stays OFF. The loss materialises only where NVS is at defaults — replacement board, factory-reset flash, or `?ERASE,NVS`.

</details>

### `Wizard/parser.js:1143` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**Two boards that share a WCB number collapse into one on config-file save → reload (whole board config lost)**

**Correction to the finding as filed:**

The observation stands; the severity and the fix do not. Corrected severity S3, and the cheap correct fix is at export, not in the file format: in `exportSystemFile` (Wizard/app.js:8417-8433) detect two populated slots sharing a `wcbNumber` and block or warn ("WCB 1 is assigned to two slots — renumber before exporting"), mirroring the wizard's existing guard at app.js:10820. Optionally also close the silent hole at app.js:6869 by dropping the `detected >= 2` lower bound in favour of an explicit "another slot already claims WCB {detected}" warning, so the fresh-board case is at least visible.

If the parser is also hardened, the section map must become an array of `{name, content}` (push, never assign, at parser.js:1143/:1152) — but note that alone does NOT fix the user-visible loss, because `loadSystemFileContent` re-collapses the two boards at app.js:8386 (`const n = board.wcbNumber || (i + 1)`; both map to `boardConfigs[1]`). A parser-only fix needs a companion slot-assignment change on load.

<details><summary>Full verification reasoning</summary>

MECHANISM — every load-bearing line checked, none taken from the summary:

1. Write side: `Wizard/parser.js:1580` is literally ``lines.push(`[WCB${board.wcbNumber}]`)`` inside `for (const board of system.boards)` (:1579-1584). Section name derives from `wcbNumber` only — no index, no uniquifier.
2. Read side: `parseSystemFile` (:1114) accumulates into a plain object — `sections[currentSection] = currentLines.join('\n')` at :1143 (on hitting the next `[...]`) and :1152 (at EOF). Key is `sectionMatch[1].toUpperCase()` (:1145). A repeated name overwrites; last-in-file wins. The board loop is `for (const [sectionName, content] of Object.entries(sections))` with `/^WCB(\d+)$/` (:1172-1176) → one entry, one board.
3. Empirically reproduced with node against the real `Wizard/parser.js` (it exports for node at :1673-1689). Two boards, both `wcbNumber=1` (Dome/HW32/1 Maestro and Body/HW31): `buildSystemFile` emits two `[WCB1]` blocks, both complete; `parseSystemFile` returns `boards.length === 1`, `[{n:1, alias:'Body', hw:31, maestros:0}]`, while `general.wcbQuantity` stays 2. The first board is silently gone. Exactly as claimed.
4. App layer confirms the loss is user-visible: `loadSystemFileContent` keys `boardConfigs[n]` by `const n = board.wcbNumber || (i + 1)` (app.js:8386), `renderBoards(2)` draws two cards, one is blank, and the only signal is the toast `Loaded: … — 1 board(s)` (app.js:8394).

REACHABILITY — can a real user hold two slots with the same `wcbNumber` at export time? Yes, three ways, and I checked each:
- `boardPull`, app.js:6869-6875: `const detected = config.wcbNumber; if (detected !== n && detected >= 2 && detected <= WCB_MAX)`. The `>= 2` gate means a board reporting **WCB 1** in slot 2 never migrates and never warns — it falls into the "Board number matches slot (normal path)" else-branch at :6915-6923 and is stored as `boardConfigs[2]` with `wcbNumber = 1`, with the misleading toast `Config pulled from WCB 2`. Fresh boards do report 1: `WCB.ino:133 int WCB_Number = 1;` and `WCB_Storage.cpp:185 preferences.getInt("WCB_Number", 1)`. So the reviewer's "two freshly flashed boards" scenario is real and unguarded.
- `boardPull` conflict branch, app.js:6871-6883: when the target slot is already connected it *deliberately* keeps the board in slot n carrying the other slot's number ("keeping here"), with a warning toast. Duplicate by design, warned.
- `onWCBNumberChange`, app.js:1877-1884: `if (boardConfigs[n] && val >= 1 && val <= qty) boardConfigs[n].wcbNumber = val;` — range check only, no uniqueness check.
- `exportSystemFile` (app.js:8417-8433) then pushes `boardConfigs[n]` for every populated slot with no duplicate check, and `buildSystemFile` is called at :8435.

One reviewer-adjacent claim is WRONG and worth recording: the *setup wizard* export path (app.js:11141) cannot produce this. `wizardValidateStep('identity')` blocks it outright — app.js:10820: `if (new Set(ids).size !== ids.length) return 'Each slot must have a unique ID (WCBs and Clients share the same address space).'` So only the main-page export is affected.

INTENT — nothing documents this as a trade-off. `git log -L` on the migration line shows only `detected <= 8` → `<= WCB_MAX` (6fb383c); the `>= 2` lower bound dates to d39391d with no comment explaining it. No `docs/` page describes the `[WCBn]` file format's uniqueness assumption.

SEVERITY — real, but S2 overstates it. (a) The exported file is complete and correct: both `[WCB1]` blocks carry full configs, so nothing is destroyed — the reader is lossy, and a user can recover by editing one header to `[WCB2]`. (b) The precondition is an already-invalid mesh state: two boards numbered 1 collide on the wire (every unicast to WCB 1 hits both), so the file being round-tripped could never be deployed as-is. (c) The UI discourages the state from three directions — wizard hard-blocks duplicates (:10820), pulls auto-migrate for numbers ≥2 (:6869), the ≥2 conflict warns (:6874), and both cards visibly read "WCB 1" via `updateBoardAliasUI` (app.js:1926). Silent loss of typed-in work (aliases, Maestro rows) in a transient setup state = S3.

</details>

### `Wizard/parser.js:1321` — CONFIRMED · S2 → **S3**

**Clearing a serial-port label never reaches the board, and the stale label resurrects a phantom Kyber Marcuino port on the next pull**

**Correction to the finding as filed:**

The mechanism, the line, and the fix are right; the severity and the stated consequence are not.

Severity S2 → S3. The claimed "locked out of the device dropdowns, so the user cannot assign S3 to an MP3/HCR" is overstated. `claimedBy` is recomputed per session, so setting the Kyber Marcduino dropdown to "— None —" (`app.js:1026-1033`) frees S3 immediately, and assigning MP3/HCR/DFPlayer to it overwrites the stale NVS label on the board (`WCB_MP3.cpp:365`, `WCB_HCR.cpp:511`, `WCB_DFP.cpp:319`). Recovery is also available as a one-line `?LABEL,CLEAR,S3` in the terminal. Real silent divergence, confusing recurring phantom, single port, recoverable.

Add a consequence the reviewer missed, which is the sharper one: moving the Marcduino from S3 to S4 leaves `Kyber Marcuino` stored on BOTH ports, and `parser.js:278-280` uses `findIndex` (first match wins), so the next pull silently reverts `marcduinoPort` to 3 — the user's move is undone, not just a leftover.

Two caveats on the fix as written. (1) It emits nothing when `baseline` is null (fullPush), since `base?.label` is undefined — correct for the "board NVS is blank" callers (`app.js:7341`, `:7357`) but it means a never-pulled board keeps stale labels; that is unchanged from today, not a regression. (2) It does not retroactively repair an already-diverged board on the next push, because `app.js:7873` overwrites `boardBaselines[n]` with the pushed config, so `base.label` is already `''`. The user must Pull once (which restores the stale label into both config and baseline) and clear it again — after which the fix does emit the clear. Worth a line in the commit message.

Scope note: the same `LABEL` gap also silently swallows label clears from `app.js:877-880`, `:1293-1297` (Kyber), `:2804-2811` (WLED), `:3405-3408` (Maestro) and the plain user-typed path at `:2919`. Fixing line 1321 covers all of them.

<details><summary>Full verification reasoning</summary>

MECHANISM — re-derived, not taken on trust.

`Wizard/parser.js:1318-1323` is verbatim what the reviewer quoted:
```
for (let i = 0; i < 5; i++) {
  const cur  = config.serialPorts[i];
  const base = baseline?.serialPorts?.[i];
  if (cur.label && (fullPush || !base || base.label !== cur.label))
    add(`LABEL,S${i+1},${cur.label}`);
}
```
The `cur.label &&` guard is unconditional — it also suppresses on `fullPush`. I ran the real module under Node (`Wizard/parser.js:1900+` exports `buildCommandString`):
- baseline S3 label `Kyber Marcuino`, current `''` → delta output is `""` (exactly the empty string), and a fullPush emits no `LABEL,S3` either.
- Plain case (typed label `Dome Maestro` → `''`) → delta output `""`.
`Wizard/app.js:7418-7422` then hits `showToast('No changes to push')`. The reviewer's repro is exact.

The firmware clear verb exists and I parsed it by hand against the real handler. `Code/WCB/WCB.ino:4412` (`processLocalCommand`) splits root at the first comma, so `?LABEL,CLEAR,S3` gives `args="CLEAR,S3"`; at `:4571-4586` `labelArg1="CLEAR"` → `target="S3"` → `clearSerialLabel(3)` (`Code/WCB/WCB_Storage.cpp:1515-1519`, which calls `saveSerialLabelToPreferences(port,"")` → `preferences.remove(key)` at `:1503-1509`). `?LABEL,CLEAR,ALL` also exists (`:4579-4581`). Grep of `Wizard/*.js` + `Wizard/index.html` for `LABEL` finds exactly one emitter — `parser.js:1322`. The Wizard never sends any clear form.

LOAD-BEARING, NOT COSMETIC. `kyber.marcduinoPort` is never pushed as a field: the `kyberChanged` test at `parser.js:1340-1343` compares only `mode`, `port` and `targets`, and the emitted command is `KYBER,LOCAL,S<port>[,targets]` (`:1347-1350`) — no marcduino. The serial label IS the persistence mechanism, stated in both comments: `app.js:1046` "`'Kyber Marcuino'; // used to recover marcduinoPort after boardPull`" and `parser.js:273-276`. So losing the clear loses real state, not decoration.

ROUND TRIP — reproduced. Firmware re-emits stored labels in the backup (`WCB.ino:2805-2807`, non-empty only). Feeding `parseBackupString` a backup containing `?KYBER,LOCAL,S2` + `?LABEL,S3,Kyber Marcuino` returns `kyber.marcduinoPort = 3` (`parser.js:277-282`) and `serialPorts[2].claimedBy = {type:'kyber-marc'}` (`parser.js:993-1003`). Phantom confirmed.

REACHABILITY — real user path. `Wizard/index.html:649` wires `onchange="onKyberMarcPortChange({N})"`; the dropdown carries `<option value="0">— None —</option>` (`app.js:1007`). `onKyberMarcPortChange` (`app.js:1020-1033`) blanks both the model label and the DOM input when the marc claim is released. Same shape at `app.js:877-880` (`onKyberChange`), `1293-1297` (`_reconcileKyberPortLabels`), `2804-2811` (WLED), `3405-3408` (Maestro) — five paths that blank a label locally, none of which reach the board. Plus the plain path: `app.js:2919` reads the label straight from a user-editable text input.

INTENT — the opposite of deliberate. `git log -L 1317,1323:Wizard/parser.js` shows the block untouched since parser.js was created (`3a84620`). The sibling ALIAS handling at `parser.js:1255-1264` does exactly the set→empty clear and was added later (`b862a0a`) with the comment "clearing (set -> empty) sends ALIAS,CLEAR" — established pattern, label block simply never got it. And `app.js:1284-1288` comments that orphaned Kyber labels "*would push stale values to NVS*", showing the author cared about label hygiene but only prevented re-pushing, never clearing.

WHERE THE REVIEWER OVERSTATES IT (why S3, not S2). "Locked out of the device dropdowns, so the user cannot assign S3" is not durable. The `kyber-marc` claim is a live in-session value: setting the Kyber Marcduino dropdown back to "— None —" releases it (`app.js:1026-1033`) and S3 immediately reappears in the MP3/HCR/DFP/WLED/Maestro dropdowns (`app.js:2285-2288`, `:2426`, `:2500`, `:2719`, `:3294`). Assigning any firmware-managed device then overwrites the stale NVS label outright — `WCB_MP3.cpp:365` `saveSerialLabelToPreferences(serialPort,"MP3 Trigger")`, same at `WCB_HCR.cpp:511` and `WCB_DFP.cpp:319`. So the board self-heals on the next real assignment, and a `?LABEL,CLEAR,S3` in the terminal fixes it manually. The defect is a genuine silent config divergence with a confusing recurring phantom, confined to one port on one board, recoverable. That is S3, not S2.

One consequence the reviewer missed, which is worse than the one claimed: moving the Marcduino S3→S4 leaves the board holding `Kyber Marcuino` on BOTH S3 (stale) and S4 (new push). `parser.js:278-280` uses `findIndex`, which returns the first match, so the next pull restores `marcduinoPort = 3` — silently reverting the user's move rather than merely lingering.

</details>

### `Wizard/serial-hub-test.html:93` — CONFIRMED · S2 → **S3**

**serial-hub-test.html: "Pick Port" can never be enabled — the shipped test harness is unusable**

**Correction to the finding as filed:**

Two corrections, neither of which changes the verdict:

(a) Severity S2 -> S3. serial-hub-test.html is an unreferenced developer bench harness (repo-wide grep finds no link to it from Wizard/index.html or anywhere else — only docs/CODE_REVIEW_2026-08_wave3.md mentions it). It ships to Pages via pages-deploy.yml:95, but no user path reaches it and no firmware, config, or user-facing Wizard behaviour is affected.

(b) The claim "Every control on the page except Join/Leave is dead" is only true for the page's own documented standalone two-tab flow. The test page uses the DEFAULT channel/lock names (serial-hub.js:44-45) and so does the Wizard (app.js:5587) — so if a Wizard tab on the same origin is leading with an open port, the test page's `{t:'state'}` handler (:500-503) sets `_leaderPortOpen`, `get portOpen()` (:114) returns true, and the test page's `canSend` (test :94) enables tx/send: it works as a follower, sending and receiving. Only `pick` stays dead. Also, `_port` has a third writer the finding omits — `_adoptGrantedPort()` at serial-hub.js:331 — but it runs only inside `_becomeLeader()` behind `_sawLeaderPort` (:290), so it opens no additional route to leadership.

Recommended fix, same as proposed: change :93 to `$('pick').disabled = st.role === 'idle';` and rewrite the instructional copy at :36-39 to the post-581d939 model ("click Pick Port in one tab; that tab becomes LEADER; the other stays FOLLOWER"). Worth checking NaviCore/config_tool/serial-hub.js stays in lockstep — no test page exists there, so no parallel change is needed.

<details><summary>Full verification reasoning</summary>

Mechanism re-derived from source, every line reference checked:

1. Wizard/serial-hub-test.html is 141 lines; I read all of it. `<button id="pick" disabled>` is at :43. The ONLY assignment to `$('pick').disabled` other than the `leave` handler (:123, which sets it back to true) is `$('pick').disabled = st.role !== 'leader';` at :93, inside `hub.on('state', ...)`. The page's only `hub.requestPort()` call is inside `$('pick').onclick` (:116). So `pick` can only be enabled by this tab reaching role 'leader'.

2. Can this page ever reach 'leader'? `_setRole('leader')` occurs only in `_becomeLeader()` (serial-hub.js:284), which runs only from the lock callback in `_campaign()` (:274-278). `grep -n "_campaign()"` over serial-hub.js returns exactly two hits: the definition (:264) and the single call site inside `_maybeCampaign()` (:261). `_maybeCampaign()` gates at :260 `if (!this._port && !this._sawLeaderPort) return;`. `join()` (:128-152) does `_setRole('follower')` (:137), `_post({t:'hello'})` (:138), `_maybeCampaign()` (:139) — with `_port = null` (constructor :85) and `_sawLeaderPort = false` (:98), that call returns immediately. `_claimLeadership()` (:458) only posts a `{t:'claim'}` message and is behind `visibilityHandoff`, which is `false` by DEFAULTS (:58) and not overridden by the test page (`new WcbSerialHub({ baudRate: 115200 })`, test :80). So there is no other route to leadership.

3. `_port` writers: `requestPort()` :161, `adoptPort()` :176, and `_adoptGrantedPort()` :331 (the reviewer missed this third one, but it is not a new route — it runs only inside `_becomeLeader()` and only when `_sawLeaderPort` is already true, :290). `_sawLeaderPort` writers: :475 (inside `_relinquish()`, visibility-handoff only, disabled) and :502 (on a leader's `{t:'state', portOpen, portInfo}`). In the documented two-tab flow neither tab ever opens a port, so no leader ever exists to `_announceState()` (:523, called only from leader-side paths :302/:356/:368/:409/:507), so `_sawLeaderPort` stays false in both tabs. Both tabs sit at 'follower' forever with `pick` greyed out. Confirmed dead.

4. Regression is real, not a misread of intent: `git log -- Wizard/serial-hub-test.html` shows one commit, 4a524c1 (2026-07-27). `git show 4a524c1:Wizard/serial-hub.js` line 126 shows `join()` then called `this._campaign();` unconditionally — so the first tab to join promoted to leader with no port and `pick` lit up, exactly as the page's copy at :36-39 describes. 581d939 (2026-08-05, "conditional campaigning — a portless tab never squats the lock") inverted that invariant and the test page was never updated. The comment at :92 ("Only the leader can pick the physical port") is a leftover of the old model, not a live constraint — nothing breaks if a follower picks, because `requestPort()` only opens the port when `this._role === 'leader'` (:164).

5. It does ship: pages-deploy.yml:95 `cp -a "$WIZ/." "$GHP/$DEST/"` copies the whole Wizard directory to gh-pages, so the file lands next to the production Wizard. But nothing links to it — a repo-wide grep for "serial-hub-test" outside .git hits only docs/CODE_REVIEW_2026-08_wave3.md (the review doc itself). No user flow, no firmware behaviour, no data is affected; the only victim is a developer who opens the bench harness. That is why I downgrade S2 to S3: real and worth fixing, but it is an unreferenced dev-only page, not a shipped feature (compare the S1/S2 neighbours in the same doc: silent NVS truncation, a "Connected" indicator that lies).

6. Fix check: the proposed `$('pick').disabled = st.role === 'idle';` works. `hub.join()` calls `_setRole('follower')` which emits 'state' synchronously (:530-537), so the handler fires and enables the button the moment Join is clicked; `requestPort()` is still invoked from a click handler so transient activation for `navigator.serial.requestPort()` is preserved; setting `_port` then calling `_maybeCampaign()` (:163) passes the gate and drives the election; `_becomeLeader()` opens the port whether or not the lock grant lands before `requestPort()` returns (:292). This is exactly how the shipping Wizard already works — app.js:5766 calls `hub.requestPort()` with no leadership precondition ("explicit 'Share' button: pick a port now"), which is independent proof that a non-leader picking is the intended model. A second tab that also picks stays queued as a follower and does not open its port (only `_becomeLeader` calls `_openAndRead`), so nothing is broken by enabling the button in both tabs.

</details>

### `Wizard/serial-hub.js:325` — CONFIRMED · S2 → **S3**

**`_adoptGrantedPort()` takes the first VID/PID match — failover to the WRONG physical WCB when two identical adapters are granted**

**Correction to the finding as filed:**

Two corrections to the finding.

(1) Severity S3, not S2, and the failure-scenario framing should be "a bench setup with two same-revision boards plugged in and granted" rather than "the normal case in a droid" — droid-resident WCBs are on the mesh, not on the browser's USB.

(2) The proposed fix is incomplete if applied only to the `find` at serial-hub.js:325. The `if (!pick && ports.length === 1) pick = ports[0];` fallback at :330 is a SECOND wrong-board path, and a worse one: it adopts the sole granted port with no VID/PID check at all. Concretely — the shared WCB (CH340) is unplugged while a different board (CP2102) stays plugged; the promoted tab's VID/PID match fails, `ports.length === 1`, and it opens and announces a board whose announced identity demonstrably does NOT match. Gate that fallback on `want == null || want.usbVendorId == null` (its original purpose — the leader announced no usable info), or drop it entirely: `_sawLeaderPort` is only set when `msg.portInfo` is present (:502), so `want` is essentially always non-null on this path and the fallback earns almost nothing.

Also note the fix must be mirrored into C:/Users/ghulette/Documents/GitHub/NaviCore/config_tool/serial-hub.js:277, where the function is identical — and that repo is the one whose ?share=1 tab is the primary portless promoter, so fixing only the WCB copy fixes the smaller half of the exposure.

<details><summary>Full verification reasoning</summary>

MECHANISM — verified line by line in C:/Users/ghulette/Documents/GitHub/Wireless_Communication_Board-WCB/Wizard/serial-hub.js:318-338. The code is exactly as quoted: `ports.find(p => i.usbVendorId === want.usbVendorId && i.usbProductId === want.usbProductId)` (:325-328), no match count, no ambiguity refusal; `this._port = pick` (:332) and the reassuring log at :334. `want` is `this._leaderPortInfo` (:322), which is set from the leader's `'state'` broadcast at :501 and is only ever `{usbVendorId, usbProductId}` from `SerialPort.getInfo()` — the API exposes no serial number or path, so two boards behind the same USB-UART bridge are provably indistinguishable here. That WCBs use such bridges is in the repo: the Wizard's own vendor table lists 0x10C4 CP2102 / 0x1A86 CH340 / 0x0403 FT232R (app.js:6228-6230), and app.js:5702-5703 states the limitation outright — "two identical-model boards share VID/PID, so this stays best-effort in that case".

REACHABILITY — the portless promoted tab is not hypothetical, it is the designed consumer of this function. NaviCore/config_tool/index.html:18812-18821 auto-runs `connectViaWcbOpt()` in a `setTimeout` 600 ms after load when the Wizard opens it with `?share=1` (that URL is built at Wizard/app.js:12126); `connectSharedPort()` then does `try { await sharedHub.requestPort(); } catch (_) {}` (index.html:4558) which MUST throw — no user gesture — leaving `hub._port === null`. It then `join()`s, receives the leader's `'state'`, sets `_sawLeaderPort = true` and campaigns (serial-hub.js:502). When the leader tab closes, `_becomeLeader()` hits `if (!this._port && this._sawLeaderPort) await this._adoptGrantedPort()` (:290) and then `_openAndRead()` + `_announceState()` publish whatever it picked as THE shared port (:367-368).
Multiple same-VID/PID grants on one origin is likewise a supported flow, not a stretch: the Wizard is explicitly multi-board (per-slot `usedPorts` sets at app.js:5921/6489/6559 and a picker modal that marks ports already "claimed" by another board, app.js:6254-6345), and Chrome's per-origin grants persist and `getPorts()` returns every currently-connected granted port. Two WCBs of the same hardware revision plugged in at the bench therefore return two identical `getInfo()` objects and `find` takes whichever `getPorts()` lists first (grant order — so for a given user it is deterministically wrong, not 50/50).

NO GUARD ANYWHERE — I grepped for every consumer. The hub never compares identities on the receive side either: `case 'state'` blindly overwrites `_leaderPortInfo` (:501). The only identity guard in the codebase is app.js:5704-5722, which is on a different path (a DIRECT open failing → auto-join the leader's share), and its own comment concedes it cannot cover identical models. `BoardConnection._onHubState` (app.js:5427-5434) only flips the connected flag/UI, so a follower's board card simply goes green again pointed at a different physical board.

INTENT — not a documented trade-off. `git blame` puts the whole function in 4a524c1f (2026-07-27, greghulette). The comment at :314-317 says "Match the identity the old leader announced; fall back to the sole granted port if there's exactly one", and the else-branch log at :336 reads "could not disambiguate the granted port" — i.e. the author's stated intent WAS to only adopt when unambiguous, and the `ports.length === 1` fallback (:330) is that instinct applied to the no-info case. The VID/PID branch just never got the same treatment. That makes this an omission, not an accepted risk.

SEVERITY — real but conditional, so S3 rather than S2. The finding's premise that several identical CH340 WCBs granted to the origin is "the normal case in a droid" is overstated: in a running droid the WCBs are on the mesh, not USB-tethered to the browser; you need a bench setup with two same-revision boards simultaneously enumerated AND granted, plus a portless `?share=1` tab, plus the leader tab dying. The immediate harm (the shared session silently binds to the wrong physical board) is real and silent; the escalation the finding headlines — "the next Push Config writes WCB 1's config into WCB 3" — needs a further step (a second Wizard tab riding as a follower, or a reconnect that trips the best-effort join guard at app.js:5713). It is also at least visible in the terminal, since hub logs are surfaced as `[hub] …` (app.js:5435). Cross-repo: NaviCore/config_tool/serial-hub.js:277+ carries the identical function, so any fix must land in both copies (they are kept in lockstep; the only other differences are the Wizard-only `adoptPort`/`releasePortKeepOpen` and NaviCore's newer step-down-on-open-failure changes).

</details>

### `Wizard/serial-hub.js:407` — CONFIRMED · S2 → **S3** · ⚠ PROPOSED FIX IS WRONG

**Read loop death (unplug) leaves the lock held and the dead port handle open — board unreachable from every tab**

**Correction to the finding as filed:**

Three corrections. (1) It is not a new finding — it is part (b) of F-366, already recorded at docs/CODE_REVIEW_2026-08_wave4.md:157 and tracked as Tier 3 in docs/CODE_REVIEW_2026-08.md:118-120. (2) Severity is S3, not S2: the state is announced (card goes "Not Connected") and logged, and it is cleared by Disconnect, by a second "Share port", or by reloading/closing the tab — a Web Lock dies with its context. (3) The fix must be the full three-hunk lockstep sync with NaviCore, not the read-loop tail alone: also change Wizard/serial-hub.js:356 to `{ this._log('open failed: ' + msg); this._stepDownPortless(); return; }`, and move the `this._portlessRetries = 0` reset out of `_becomeLeader` (Wizard:291) to immediately after `this._portOpen = true` in `_openAndRead` (mirroring NaviCore:328-332). While there, run `node C:\Users\ghulette\tools\jscheck.js Wizard/index.html` — the Wizard has no build step — and note the two copies are NOT byte-identical by design: the Wizard uniquely carries `adoptPort()` (:175) and `releasePortKeepOpen()` (:227) for its flash auto-demote, so sync the three hunks, not the whole file.

<details><summary>Full verification reasoning</summary>

MECHANISM — verified verbatim. `Wizard/serial-hub.js:372-412` is `_readLoop()`. The loop guard is `while (this._role === 'leader' && this._port && this._port.readable && !this._leaving)` (:381), with the give-up at `else if (++emptySessions >= 6) break;` (:404). The tail block is exactly as quoted — `Wizard/serial-hub.js:407-411` does only `this._portOpen = false; this._announceState(); this._log('port read loop ended (unplug?)');`. No `_closePort()`, no `_stepDownPortless()`. `_releaseLock` is only cleared in `leave()` (:215), `releasePortKeepOpen()` (:240), `_stepDownPortless()` (:304) and `_relinquish()` (:479) — none of which run here — so the tab returns from `_readLoop` with `_role === 'leader'` and the exclusive Web Lock `wcb-shared-serial-owner` still held, and `this._port` never `close()`d. `_portOpen` is provably true at loop exit (set at :367, cleared only by `_closePort`/`releasePortKeepOpen`/this block), so the block does fire.

STATE-MACHINE CONSEQUENCE — the hub is written around the invariant that a leader must never hold the lock without a usable port: `_maybeCampaign` (:250-256 comment), `_becomeLeader` (:292 `else this._stepDownPortless(); // won the lock but have no port … → yield, don't squat`), and `_stepDownPortless`'s own comment (:294-300). This tail block violates that invariant. `_campaign()` (:270-279) parks every other tab in `navigator.locks`, so they can never promote; there is no timer or re-campaign after the tail block, so a replug recovers nothing on its own.

REACHABILITY — real, and not niche. `establishConnection()` (Wizard/app.js:5641) auto-shares the FIRST board connected, so the hub is live in ordinary single-board use, not just an opt-in. `_readLoop` is called only from `_openAndRead` (:369), which is reached from `requestPort`/`adoptPort`/`_becomeLeader`. No `navigator.serial` `disconnect` listener exists anywhere in Wizard/*.js (grep: zero hits), and `_onHubState` (app.js:5427-5434) only mirrors `portOpen` into the card — it does not tear down or re-campaign. So an unplug/brownout re-enumeration leaves the leader squatting.

BOUNDING THE BLAST RADIUS (why S3, not S2) — the failure is loud, not silent: the card flips to "Not Connected" via `_onHubState`, and `[hub] port read loop ended (unplug?)` is printed to the terminal. It is recoverable by ordinary user action: `boardDisconnect` → `leaveShared()` → `hub.leave()` → `_closePort()` + release (app.js:6202, serial-hub.js:212-215); `sharedConnect()` rebuilds the hub from scratch on every shared connect (app.js:5745-5755); and simply reloading/closing the tab releases both the Web Lock and the OS handle. No data loss, no wrong bytes on the wire, firmware untouched. This matches NaviCore's own comment: "until the user disconnected this tab by hand."

INTENT — not deliberate. `NaviCore/config_tool/serial-hub.js:372-383` carries the fix plus the failure comment, landed 2026-08-18 in `f72878a "Apply 28 confirmed medium-severity review fixes"` (I read the diff: it changes three hunks — the read-loop tail, the `_openAndRead` out-of-retries branch, and the `_portlessRetries` reset placement). The Wizard copy's last touch is `581d939`, dated 2026-08-05. The files are required to stay in lockstep. One caveat on the evidence: NaviCore's "left the board unreachable from EVERY tab" comment was written BY that review-fix commit, so it is a reviewer's claim, not a recorded bench observation.

DUPLICATE — this is part (b) of an already-logged finding: `docs/CODE_REVIEW_2026-08_wave4.md:157` F-366 (S2), summarised as Tier 3 cross-repo work at `docs/CODE_REVIEW_2026-08.md:118-120`. F-366 covers all three divergent hunks; this finding covers only one of them.

</details>

### `Wizard/serial-hub.js:501` — CONFIRMED · S2 → **S3**

**A promoted leader broadcasts the PREVIOUS leader's port identity, defeating the Wizard's wrong-board guard**

**Correction to the finding as filed:**

The defect is real; three corrections to the framing and the fix.

1. Title/severity overstated. The poisoned value does not wave a wrong board THROUGH the guard — that fail-open case is unreachable, because app.js:5695 only enters the join branch when the picked port failed to open as busy, and after the leader promotes onto P1 the stale-announced P2 is free and opens fine. The reachable outcome is fail-closed: a legitimate join to the real shared port P1 is refused with a toast (app.js:5715-5721). Real bug, safe failure direction, recoverable. S3.

2. The fix is placed one step too late. `_openAndRead` announces at TWO sites: the out-of-retries failure at serial-hub.js:356 (`this._log('open failed: …'); this._announceState(); return;`) as well as :368. Since :501 overwrites a follower's `_leaderPortInfo` with NO `msg.portOpen` gate, the :356 announcement leaks the stale identity too. Put the refresh right after the `if (!this._port) return;` at :343 instead — the identity of the port we are about to open is known regardless of whether the open succeeds, and that one placement covers :356, :368 and the read-loop-death announce at :409:

    if (!this._port) return;
    try { if (this._port.getInfo) this._leaderPortInfo = this._port.getInfo(); } catch (_) {}

3. The fix must land in BOTH copies. NaviCore/config_tool/serial-hub.js carries the identical dual-purpose variable — overwrite at :472, announce at :495, matcher at :281 — so a NaviCore tab promoted with its own port poisons the origin exactly the same way and the Wizard-only patch leaves the bug live from the other tab. Note the two files have already drifted apart (NaviCore's `_openAndRead` steps down on open failure, :315-318, where the Wizard's just returns at :356; NaviCore has no `adoptPort`/`releasePortKeepOpen`), so this is a merge, not a copy — and MEMORY.md records these copies are meant to stay in lockstep.

Root-cause fix worth preferring: split the field. `_becomeLeader` (:290) deliberately prefers our own port over the announced one, so "the identity of the port I hold" and "the identity the current leader announced" are two different facts and should be two fields (`_myPortInfo` / `_leaderPortInfo`), with `_adoptGrantedPort` and `_announceState` each reading the right one.

Separately (out of scope for this finding, but the same state produced it): the explicit share path `modalSharedConnect` → `sharedConnect` has no `_anotherTabLeadsShare()` check, while both automatic paths do (app.js:5641, 5154-5158). Adding it there would prevent the follower-holding-a-different-port state from arising at all.

<details><summary>Full verification reasoning</summary>

MECHANISM — every load-bearing line re-read, and every line number the reviewer cited is accurate (unusual; I checked each).

`_leaderPortInfo` really is dual-purpose in Wizard/serial-hub.js:
- written from OUR port at :162 (`requestPort`) and :177 (`adoptPort`);
- overwritten from a peer's announcement at :501 — `if (msg.portInfo) this._leaderPortInfo = msg.portInfo;` — gated only on `this._role !== 'leader'` (:499). Note it is NOT gated on `msg.portOpen`; only the `_sawLeaderPort` set on the next line (:502) is.

`_becomeLeader()` :290 calls `_adoptGrantedPort()` (the only path that refreshes the identity, :333) ONLY when `!this._port`. A promoted tab that already holds its own port skips it, and `_openAndRead()` never re-derives the identity: :343 `if (!this._port) return;` → open loop → :367 `this._portOpen = true` → :368 `_announceState()` → :524 `this._post({ t:'state', portOpen:…, portInfo: this._leaderPortInfo })` and :536 emits the same to local listeners. So a promoted leader opens P1 and tells the origin P2. Mechanism stands exactly as described.

NOT DELIBERATE — the comment at :164-165 ("port selected; will open it if this tab becomes the leader") and the `_becomeLeader` comment at :286-289 establish that keeping and later opening OUR port is the intended design. Announcing a different port's identity is therefore inconsistent with the documented intent, not a documented trade-off.

REACHABILITY — genuinely reachable, via the path the reviewer named. `modalSharedConnect()` (Wizard/app.js:5782, wired by the inline `onclick="modalSharedConnect()"` the Wizard builds at app.js:5841) → `sharedConnect(n)` with no `existingPort` → `hub.requestPort()` (app.js:5766, unfiltered picker) → `conn.connectShared(hub)` → `hub.join()` (app.js:5439). requestPort's `_maybeCampaign()` is a no-op there (`!this._joined`, serial-hub.js:258), so the campaign happens in join() with `this._port` already set — we queue behind the existing leader and stay a follower holding P1, then the leader's reply to our 'hello' (:507) overwrites `_leaderPortInfo` with P2. Critically, this explicit path has NO `_anotherTabLeadsShare()` guard, unlike the two automatic paths that do (auto-share app.js:5641; RC-tool promote app.js:5154-5158, whose comment names this exact hazard). NaviCore's `connectSharedPort()` (NaviCore/config_tool/index.html:4558) is the same shape — its comment says a follower "can just cancel the picker", i.e. the design expects a cancel but nothing enforces one.

BLAST RADIUS — this is where S2 does not hold. The identity has exactly two consumers anywhere: `_adoptGrantedPort`'s matcher (serial-hub.js:322-329) and the Wizard guard (app.js:5712). NaviCore's config tool ignores `st.portInfo` entirely — `onSharedState` reads only `st.portOpen` (NaviCore/config_tool/index.html:4546-4548). Both consumers compare ONLY usbVendorId/usbProductId (serial-hub.js:327, app.js:5713-5714), so for a fleet of same-revision WCBs sharing one USB-UART chip the stale value is byte-identical to the fresh one and nothing observable changes — the Wizard's own comment concedes this ("two identical-model boards share VID/PID, so this stays best-effort", app.js:5705-5706). It only bites with mixed USB chips.

Also, the reviewer's title overstates the guard damage. I traced the fail-open case and it is unreachable: for a third tab to be waved INTO the wrong session it would have to pick P2 and have the direct open fail, but app.js:5695 requires an `/open|already|access|busy|in use/i` error and P2 is free once tab A closed (the leader now holds P1), so the open succeeds and the join branch is never entered. The reachable outcome is the opposite — a tab picking P1 (genuinely busy) is compared against stale P2, mismatches, and the guard REFUSES a legitimate join with the "different board than the shared session holds" toast (app.js:5715-5721). That is fail-CLOSED and recoverable via the explicit Share button. The genuinely nasty consequence — a portless follower later promoting and adopting P2 via the :322-329 matcher, silently moving the session to another board — sits one condition deeper still.

So: real defect, reachable, but behind a 4-condition chain (user picks a different port where the design expects a cancel; two ports with DIFFERENT VID/PID; the leader then closes; a third reader consults the identity), and its reachable failure mode is a refused join, not a wrong-board write. S3, not S2.

</details>

### `Code/WCB/WCB_Help.cpp:128` — CONFIRMED · S2 → **S4** · ⚠ PROPOSED FIX IS WRONG

**?BAUD? help tells the user to run ?MAESTRO,ENABLE, which is not implemented**

**Correction to the finding as filed:**

Severity is S4, not S2 — per the review's own rubric (docs/CODE_REVIEW_2026-08.md:10-11) this is "doc drift"/stale text, not "wrong behaviour in a real scenario". The firmware behaves correctly; only the help string lies.

Minor line-number correction: the rejecting guard is WCB_Maestro.cpp:537-543 with the error printed at :539-540 (reviewer said 538-542).

Additional route the reviewer missed (does not change the verdict): the legacy path at WCB.ino:5226 (`message.startsWith("maestro")` → `configureMaestro(message.substring(7))`) also fails, so no spelling of the command works.

Corrected fix: the simplest correct change is to delete WCB_Help.cpp:128 outright — no information is lost, because the `?MAESTRO` topic already carries the accurate cross-reference in the other direction at WCB_Help.cpp:462 ("Set baud rate with: ?BAUD,Sx,57600"). If a pointer is wanted instead, it must not hardcode W1 and must scope the baud claim to the local case, e.g.:

Serial.println(F("  - A local ?MAESTRO,M<id>:W<thisWCB>S<port>:<baud> sets that port's baud too"));

<details><summary>Full verification reasoning</summary>

OBSERVATION — verified verbatim, not trusted from the summary.

`Code/WCB/WCB_Help.cpp:128` reads exactly:
`Serial.println(F("  - Use ?MAESTRO,ENABLE as a shortcut for S1 Maestro setup"));`
inside the `} else if (c == "BAUD") {` topic that opens at WCB_Help.cpp:107.

REACHABILITY — a real user hits this trivially. `processLocalCommand()` (WCB.ino:4394) takes any message ending in `?` (WCB.ino:4402-4407), strips the `?`, and calls `printCommandHelp(cmd)`. So `?BAUD?` → `printCommandHelp("BAUD")` → prints line 128. No guard, no gate, no debug flag.

MECHANISM — re-derived independently, and the reviewer is right that `ENABLE` does not exist.
- I grepped the entire firmware for the token: `grep -rn '"ENABLE"|ENABLE,|,ENABLE' Code/WCB/*.cpp *.ino *.h` returns **exactly one hit — WCB_Help.cpp:128 itself**. The only other repo hits are the review docs and the two committed `Wizard/bin/*.bin` images (which merely contain this same baked-in help string). There is no `ENABLE` sub-verb anywhere.
- Dispatcher confirmed at WCB.ino:4897-4914, and the reviewer's quote is accurate: `LIST` / `CLEAR,ALL` / `CLEAR,` prefix / `REMOTE`, else `configureMaestro(args)`; `return` at :4913.
- I checked the *other* possible route too, which the reviewer did not: the legacy underscore path at WCB.ino:5216-5226. `message.startsWith("maestro")` → `configureMaestro(message.substring(7))`. That path also lands in the same rejecter (`",ENABLE"` does not start with `M`). Both routes fail identically — no hidden implementation.
- Rejecter confirmed at `WCB_Maestro.cpp:531` (`void configureMaestro`), guard at :537, error print at :539-540. Reviewer said "538-542"; actual is 537-543 — trivial slop, not material. Note the guard's operator precedence: `isEmpty() || !startsWith("M") && !startsWith("m")` parses as `isEmpty || (notM && notm)`, so `"ENABLE"` → true → error. The printed message is `Invalid format. Use: ?MAESTRO,M<maestroID>:W<wcb>S<port>:<baud>` — it indeed never mentions ENABLE, so the reviewer's failure scenario (reads like a typo, not like "no such command") is accurate.

INTENT — not a deliberate trade-off. `git log -L 128,128:Code/WCB/WCB_Help.cpp` shows the line was introduced in the commit that created WCB_Help.cpp (`64041df "updates"`) and has never been touched since. It is original stale text, not a regression, and no comment defends it. Corroborating: the authoritative `?MAESTRO` help topic (WCB_Help.cpp:406-468) lists its Commands as `Mx:WxSx:baud`, `LIST`, `CLEAR,x`, `CLEAR,ALL` — **no ENABLE** — and its own Notes at WCB_Help.cpp:462 say "Set baud rate with: ?BAUD,Sx,57600". So the two topics disagree, and the `?MAESTRO` one is the correct one.

SEVERITY — S2 is wrong by this project's own published rubric. `docs/CODE_REVIEW_2026-08.md:10-11` defines: "**S2** wrong behaviour in a real scenario ... **S4** dead code, stale comment, **doc drift**." Nothing behaves wrongly here: the firmware correctly rejects a nonexistent command and prints a valid format string. The only defect is a stale help line — textbook doc drift. This is the same call the review itself already made on F-272 (`docs/CODE_REVIEW_2026-08.md:128`, "downgraded S2 → S4"). Real, worth a one-line fix, but S4.

FIX CHECK — the offered fix is half right, and the half shown as code is wrong.
The delete option works. The *replacement* line the reviewer proposes — `?MAESTRO,M1:W1S1:57600 (sets the port baud too)` — is only true when the target WCB is this board. `configureMaestro` branches at WCB_Maestro.cpp:639 `if (targetWCB == WCB_Number)`; only that LOCAL branch calls `updateBaudRate(serialPort, baudRate)` at WCB_Maestro.cpp:658-661 (which does genuinely apply live baud + NVS — WCB_Storage.cpp:146-180). The REMOTE branch at WCB_Maestro.cpp:688-697 only writes `maestroConfigs[slot].baudRate` and calls `saveMaestroSettings()` — it never calls `updateBaudRate`. So on any board that is not WCB1, the proposed `W1S1` example sets no local port baud, and the parenthetical becomes a fresh false statement of exactly the class this finding exists to remove.

</details>

### `Code/WCB/WCB_Help.cpp:364` — CONFIRMED · S2 → **S4**

**?WCB? help caps the board number at 9; the parser accepts 1-20**

**Correction to the finding as filed:**

Severity S2 -> S4 (doc drift by the review's own rubric at docs/CODE_REVIEW_2026-08.md:10-11 — no code path behaves incorrectly). The failure scenario should be restated: a builder cannot "renumber under 9" with 11 boards; the real cost is momentary confusion, self-correcting the first time ?WCB,10 succeeds or they read ?WCBQ? (WCB_Help.cpp:392) or the wiki (Command-Reference.md:93), both of which already say 1-20.

The fix is also INCOMPLETE as transcribed. Three more sites in the same family must change in the same commit or the contradiction just moves:
  - WCB_Help.cpp:1027 — top-level `??` menu: `?WCB,x          Set this board's number (1-9)` -> `(1-20)`
  - WCB_Help.h:21 — header block: `//   ?WCB,x                   Set this board's WCB number (1-9)` -> `(1-20)`
  - WCB_Help.h:22 — `//   ?WCBQ,x                  Set total number of WCB boards in system (1-9)` -> `(1-20)`. This one is a SECOND, separate drift the finding does not mention: saveWCBQuantityPreferences bounds quantity to 1..MAX_WCB_COUNT (WCB_Storage.cpp:437-438) and the ?WCBQ? topic already says 1-20 (WCB_Help.cpp:392), so the header comment contradicts both.

Optional, same family, S4: WCB_Storage.h:78 `uint8_t wcbNumber;    // 0 = local, 1-9 = remote WCB` on SerialMonitorOutput — the mapping parser accepts 1..MAX_WCB_COUNT (WCB_Storage.cpp:1249, :1264, :1717) and uint8_t holds 20, so this is comment-only.

Per CLAUDE.md this is a help-string correction with no observable behaviour change, so it needs no docs/ Revision-log row — but the wiki is already correct and needs no edit either.

<details><summary>Full verification reasoning</summary>

OBSERVATION IS REAL — I re-derived every load-bearing fact from source rather than trusting the transcription.

THE TEXT. Code/WCB/WCB_Help.cpp:364 prints "Sets this board's WCB number (1-9). The WCB number determines" and :369 prints "  number        1-9, must be unique in the system". Both quoted correctly.

THE ACTUAL BOUND. WCB.ino:119-120: `#ifndef MAX_WCB_COUNT` / `#define MAX_WCB_COUNT        20   // Maximum number of WCB peers (limited by ESP-NOW unicast peer count)`. Mirrored at WCB_Storage.h:12 and WCB_OTA.h:64 with the same value, so no TU sees a different number. The ?WCB parser at WCB.ino:4764-4772 is `if (newWCB >= 1 && newWCB <= MAX_WCB_COUNT) saveWCBNumberToPreferences(newWCB); else Serial.printf("Invalid WCB number %d. Valid range: 1-%d.\n", ...)`. The legacy path `updateWCBNumber()` at WCB.ino:5694-5698 uses the identical bound on `message.substring(3).toInt()` — substring(3) of "?WCB11" is "11", so the legacy form is two-digit-capable too. saveWCBNumberToPreferences (WCB_Storage.cpp:190-198) does a bare putInt with no clamp. All three transcriptions in the finding are accurate.

REACHABILITY — >9 IS NOT VESTIGIAL, IT WORKS. I checked whether a 9-cap survives elsewhere that would make the help accidentally true. It does not: (a) MAC synthesis is a loop over all MAX_WCB_COUNT slots writing `WCBMacAddresses[i][5] = i + 1` (WCB.ino:7311-7317), fine to 20; (b) `;w` unicast routing has an explicit designed two-digit form — the comment block at WCB.ino:5931-5947 spells out "Comma right after the digit run -> the WHOLE run is the target, enabling two-digit targets:  ;w13,RA  /  ;w20,<cmd>"; (c) the Wizard's ceiling is `const WCB_MAX = 20;  // absolute hardware ceiling` (Wizard/app.js:1081) and its board dropdown populates 1..max (app.js:1086-1097); (d) the residual hard-coded `> 9` checks I found are Maestro IDs (WCB_Storage.cpp:1322) and WLED IDs (WCB_WLED.cpp:216, :270, :418) — different ID space, not board numbers. So a real builder can set ?WCB,11 and the board meshes, routes and is addressable.

INTENT — DRIFT, NOT A DELIBERATE TRADE-OFF. `git log -L 364,364:Code/WCB/WCB_Help.cpp` shows the line was introduced in 64041df (Wed Feb 25 2026, "updates") as part of the file's creation. `git log -S "MAX_WCB_COUNT        20"` shows the cap was raised in 8588cb6 (Thu Mar 19 2026, "added support for up to 20 WCBs in a network") — three weeks LATER. So the help was correct when written and was simply not updated with the cap. Decisive corroboration: the adjacent ?WCBQ topic WAS revised in that era and now reads "quantity      Baseline number of WCB boards (1-20)" (WCB_Help.cpp:392), and the wiki already says "| `?WCB,x` | Set WCB number (1-20) |" (Wireless_Communication_Board-WCB.wiki/Command-Reference.md:93). No comment anywhere sanctions a conservative 9. The on-device help is now the ONLY place in the ecosystem still claiming 9.

SEVERITY IS THE ERROR. The review's own rubric (docs/CODE_REVIEW_2026-08.md:10-11) defines S2 as "wrong behaviour in a real scenario" and S4 as "dead code, stale comment, doc drift". Nothing here misbehaves: the firmware accepts 1-20, and a user who ignores the help and types ?WCB,11 gets a correctly configured board. The stated failure scenario is also overdrawn — a builder with 11 boards cannot "renumber their fleet to squeeze under 9" (11 boards will not fit in 9 numbers), so the realistic worst case is a moment of confusion resolved the first time they type ?WCB,10 and it succeeds, or read ?WCBQ? / the wiki, both of which say 1-20. This is textbook doc drift = S4.

NOTE ON PRIOR ART (not independent evidence): docs/CODE_REVIEW_2026-08.md:692 and _wave4.md:2473 already list this as F-028 S2 — but those are this same unverified review checked into the tree, so I did not count them as corroboration. _wave4.md:2473 does independently name the two extra sites I found below.

</details>

### `Code/WCB/WCB_Help.cpp:1015` — CONFIRMED · S2 → **S4**

**Ten implemented ? commands have no help topic and no menu entry; asking for help on them silently prints the generic menu**

**Correction to the finding as filed:**

Real, but already filed — and at the right severity the first time. This is F-030 (docs/CODE_REVIEW_2026-08.md:793, `WCB_Help.cpp:1015`, S4) merged with F-029 (:792, `?OTA`/`?OTALOCAL`, S4). It should be folded into those rows, not counted as a new S2. Severity S2 → S4 per the review's own rubric at docs/CODE_REVIEW_2026-08.md:10 ("S4 … doc drift"): all ten commands work correctly; only their on-device documentation is missing. Trim the failure narrative: the mesh channel is exposed as a labeled dropdown with a full constraint tooltip at Wizard/index.html:95-101 and its command name appears in every `?config` dump (WCB.ino:2788), so it is behind an advanced toggle, not undiscoverable. The strongest concrete instance is `?TRACK` — no Wizard exposure at all, and WCB.ino:5028 tells the user to type `?TRACK ?`, which prints the generic menu. If fixed, add the CONTROLLER topic under both `CONTROLLER` and the `SPECIAL` alias (the dispatch accepts both, WCB.ino:4861), and update the wiki Command-Reference in the same commit — it documents none of these either.

<details><summary>Full verification reasoning</summary>

WHAT I OPENED: Code/WCB/WCB_Help.cpp:1–20 and :960–1081, Code/WCB/WCB.ino:4370–4425 / 4776–4900 / 5003–5130, Code/WCB/WCB_Help.h:1–20, Wizard/index.html:93–101, Wizard/app.js + parser.js (WCBCH/ALIAS/CONTROLLER/IDENTIFY/RTERM), docs/CODE_REVIEW_2026-08.md:1–25 and :783–800, and the wiki repo at C:/Users/ghulette/Documents/GitHub/Wireless_Communication_Board-WCB.wiki. Grepped repo-wide for WCBCH and for each of the ten roots inside WCB_Help.cpp.

MECHANISM — re-derived, and it holds exactly:
1. `processLocalCommand` intercepts ANY line ending in `?` before the dispatch chain: WCB.ino:4401 `if (message.endsWith("?"))` → strips it, trims, calls `printCommandHelp(cmd)` and returns (WCB.ino:4402-4406). So `?WCBCH?` never reaches the WCBCH setter.
2. `printCommandHelp` (WCB_Help.cpp:10) upper-cases and runs a flat else-if chain. I counted the branches myself: `grep -c 'c == "'` = 26 lines (WCB_Help.cpp:16 DEBUG … :987 HW; :534 is the one line carrying two names, `DFP || DFPLAYER`). The terminal `else` at :1015 prints the full top-level menu (:1017-1079) with **no** "no help for X" line — exactly as claimed.
3. Dispatch roots: `grep -c 'rootUpper == "'` = 36 lines / 37 comparisons (WCB.ino:4861 carries `CONTROLLER || SPECIAL`), which is the reviewer's 37. Diffing the two sets by hand, the roots with no topic are exactly ten: WCBCH (:4790), PEERSLIVE (:4803), OTALOCAL (:4812), OTA (:4826), ALIAS (:4839), CONTROLLER/SPECIAL (:4861), TRACK (:5018), IDENTIFY (:5079), MGMT (:5090), RTERM (:5099). Every line number in the reviewer's table is correct. None of WCBCH/ALIAS/CONTROLLER/SPECIAL/TRACK/IDENTIFY/RTERM/OTALOCAL/OTA appears anywhere in WCB_Help.cpp; PEERSLIVE appears only as a passing note in the WDP topic (:862) and MGMT only as examples inside the SEQ topic (:773-774).
4. Corroborating self-contradiction (already on record, see below): WCB.ino:5028 prints `"Invalid TRACK command. Use: ?TRACK ?"` — the firmware itself points the user at a help topic that does not exist. And the menu's own second line (WCB_Help.cpp:1021) advertises `?COMMAND?  - Quick syntax for a specific command`, a promise these ten break.

REACHABILITY: trivially real — any user on USB/S0 typing `?WCBCH?`. No guard, no clamp, nothing upstream.

INTENT: no comment anywhere marks the omission as deliberate; git history shows WCB_Help.cpp created whole in 64041df (2026-02-25) and never revisited as commands were added. This is drift, not a trade-off.

WHY THE SEVERITY IS WRONG (the substantive correction):
(a) DUPLICATE. This is a re-derivation of two findings already filed in this same review, at the same file:line. docs/CODE_REVIEW_2026-08.md:793 — "F-030 | **S4** | `WCB_Help.cpp:1015` | Nine implemented `?` commands have no help topic — including `?TRACK`, whose own error message tells the user to type `?TRACK ?`" — plus docs/CODE_REVIEW_2026-08.md:792 — "F-029 | **S4** | `WCB_Help.cpp:1071` | No help topic for `?OTA` / `?OTALOCAL`". F-030's nine + F-029's OTA pair is this finding's ten (F-030 evidently counted CONTROLLER and SPECIAL separately). The repo already triaged this exact defect at S4; the new pass merges the two rows and inflates to S2 with no new evidence.
(b) The repo's own rubric (docs/CODE_REVIEW_2026-08.md:10) is outcome-based: "S1 crashes/corrupts/bricks · S2 **wrong behaviour in a real scenario** · S3 latent race, bound, leak, overflow · S4 dead code, stale comment, **doc drift**". All ten commands execute correctly; nothing behaves wrongly. Missing on-device help is doc drift. S4.
(c) The claimed failure scenario is dramatized. "Undiscoverable" is false for the primary configuration path: Wizard/index.html:95-101 has a labeled **Mesh Channel** dropdown (`g-meshch`, 1-11) whose tooltip already carries the precise constraint the reviewer wants added to help — "move the ENTIRE fleet together: a push stages the change, and each board applies it on its next reboot"; parser.js:496 parses `WCBCH` and parser.js:1272 emits it; and WCB.ino:2788 `emit("WCBCH," + String(meshChannel), true)` puts the literal command name in every `?config` / backup dump, so the name is one `?config` away on the device too. The field is behind the advanced toggle (`advanced-only` class, index.html:95), which is real friction — but not "the feature is undiscoverable". Same for ALIAS (app.js:9753 field), IDENTIFY (app.js:9127-9153 button), CONTROLLER (parser.js:1281), RTERM (app.js:516-522, tool-driven, never typed). The genuinely device-only one is `?TRACK` — zero Wizard hits — and that is the one whose own error text points at the missing topic. Worth noting against the "cheap to fix, worth doing before release" framing the review already gives this block: the wiki has no coverage either (`grep -ic alias|identify` on Wireless_Communication_Board-WCB.wiki/Command-Reference.md = 0; no WCBCH anywhere in the wiki), so if this gets fixed, the wiki edit belongs in the same commit per CLAUDE.md.

</details>

### `Code/WCB/WCB.ino:5138` — CONFIRMED · S2 → **S4** · ⚠ PROPOSED FIX IS WRONG

**?LED,PIN re-inits the NeoPixel object while the WiFi task dereferences it, leaking the old object and opening a use-after-free**

**Correction to the finding as filed:**

Title should be: "?LED,PIN re-init leaks the previous NeoPixel object (WCB.ino:6740 assigns without deleting)". Severity S4, not S2 — ~50 bytes per invocation of a rare config command, no crash, no RMT exhaustion, no use-after-free.

Drop the use-after-free claim entirely. The only `delete` (WCB.ino:6754) sits behind `getPixelColor(0) != 0` (:6750), which reads the library's own RAM buffer with `brightness == 0` and so always returns 0x050000 — unreachable outside heap exhaustion. Drop the RMT/hardware-contention implication too: esp.c:37-110 shares one static rmtPin under `show_mutex` and re-inits on pin change at esp.c:76-84.

Correct fix — do not allocate at all. Replace the re-init in the `?LED,PIN` handler with a live re-pin of the existing object:

    saveStatusLEDPin(pin);
    STATUS_LED_PIN = pin;
    if (statusLED) { statusLED->setPin(pin); statusLED->show(); }
    else           { initStatusLEDWithRetry(5, 100); }
    turnOffLED();

`Adafruit_NeoPixel::setPin` (Adafruit_NeoPixel.cpp:3347-3354) releases the old pin (`pinMode(pin, INPUT)`), claims the new one OUTPUT/LOW, and the next `show()` makes espShow notice `pin != rmtPin` and do the rmtDeinit/rmtInit itself (esp.c:76-84). The pointer never changes, so there is nothing to leak and nothing for the WiFi task to race against. It also removes the 200 ms of `delay()` (:6735, :6741, :6747) that `?LED,PIN` currently stalls the loop task with.

If a re-init is genuinely wanted, the delete must be deferred, not "publish null then delete" — e.g. keep the outgoing pointer in a static and free it on the NEXT re-init, or guard `colorWipeStatus` and the swap with a shared mutex.

<details><summary>Full verification reasoning</summary>

I opened the handler, the init routine, the LED consumers, and the actual Adafruit_NeoPixel sources in the sketchbook (Arduino-Code/libraries/Adafruit_NeoPixel, library.properties:2 = v1.15.5, the copy that shadows everything on this machine). The finding splits cleanly into a real half and a wrong half.

REAL HALF — the leak. Code/WCB/WCB.ino:5128-5144 is the `?LED,PIN` handler; :5138 calls `initStatusLEDWithRetry(5, 100)`. Inside it, WCB.ino:6740 does `statusLED = new Adafruit_NeoPixel(...)` with no prior `delete`. `statusLED` is declared at WCB.ino:658 and setup() calls the same routine at WCB.ino:7173 (the `if (statusLED == nullptr)` check at :7176 confirms it is expected non-null afterwards), so by the time any `?LED,PIN` arrives the pointer is already live. Every `?LED,PIN` therefore drops one object on the floor. Grep over Code/WCB (excluding src/) shows the ONLY writers of `statusLED` are WCB.ino:658, :6740, :6754-6755 and :6764 — there is no delete on the success path anywhere. Confirmed.

Magnitude (the reviewer never quantified it): Adafruit_NeoPixel.h:392-405 gives the member set — 2 bools, 2 uint16, 1 int16, 5 uint8, 1 uint8*, 1 uint32; the `port`/`pinMask` and `gpioPort` members are #if-gated to AVR/STM32/CH32, not ESP32. That is ~24 B, plus `malloc(3)` for the 1-pixel buffer (STATUS_LED_COUNT = 1, WCB.ino:645), plus two ESP32 heap block headers. Roughly 50 bytes per `?LED,PIN`. You would need ~2000 invocations to lose 100 KB.

WRONG HALF #1 — the use-after-free is not reachable. The only `delete statusLED` is WCB.ino:6754, on the FAILED-attempt branch gated by `if (statusLED->getPixelColor(0) != 0)` at :6750. That check reads back the library's own RAM buffer, not the hardware. The constructor initialises `brightness(0)` (Adafruit_NeoPixel.cpp:86) and `initStatusLEDWithRetry` never calls `setBrightness` on the fresh object before the check, so `setPixelColor(0, Color(5,0,0))` takes the raw-store path (Adafruit_NeoPixel.cpp:3450 `if (brightness)` is false → :3462-3464 store 5,0,0) and `getPixelColor(0)` takes the raw-return path (Adafruit_NeoPixel.cpp:3629-3632) returning 0x050000. Non-zero, always, on attempt 1 — the only way to reach :6754 is `numLEDs == 0`, i.e. the 3-byte malloc in updateLength failed (Adafruit_NeoPixel.cpp:3613-3614 returns 0 when `n >= numLEDs`). Heap exhaustion only. And the deeper point the reviewer inverted: because the OLD object is never freed, a WiFi-task thread holding a stale `statusLED` value dereferences memory that is still allocated. The leak is exactly what makes today's code safe.

WRONG HALF #2 — no hardware/RMT hazard either. With ESP32 core 3.3.4 (IDF 5.x) the constructor calls `espInit()` (Adafruit_NeoPixel.cpp:91-95), which only creates a mutex (esp.c:117-121). `espShow` (esp.c:37-110) uses a SINGLE static `rmtPin` + `led_data` shared across all instances, guarded by `show_mutex` (esp.c:52), and handles a pin change itself via `rmtDeinit(rmtPin)` / `rmtInit(pin,...)` at esp.c:76-84. So no per-object RMT channel is leaked, and concurrent `show()` from the WiFi task and the loop task serialise inside the library.

The threading premise itself is correct, for the record: `colorWipeStatus` IS called unconditionally from `espNowReceiveCallback` (WCB.ino:3584; call sites :3848, :3860, :3870, :3885, :3932, :3984, :4000, :4161, :4165, :4208, :4229), which runs on the WiFi task — the surrounding comments say so explicitly ("queued by the WiFi callback" :3840, "off the WiFi callback" :3852/:3854, "cross-core safety" :4194). And `?LED,PIN` always runs on the loop task (processLocalCommand WCB.ino:4394 ← handleSingleCommand :4381 ← the queue drain at :7512, inside `loop()` which starts at :7477). Two tasks, one unsynchronised pointer — the observation is sound; it just does not produce a UAF given the code that is actually there.

Reachability of the command is fine (Wizard/parser.js:1246-1249 emits `LED,PIN,<n>` on fullPush or on a diff), but it is a rare, human-driven config command, not a hot path.

Net: a genuine but ~50-byte, non-crashing leak on a rare command. S4, not S2.

</details>

### `Wizard/flasher.js:720` — CONFIRMED · S2 → **S4**

**Post-flash hard reset never runs — `loader.afterFlash` does not exist in the vendored esptool-js 0.4.7**

**Correction to the finding as filed:**

Severity S2 → S4, and the failure scenario is wrong. The board does NOT stay in the download stub in the shipped flow: `Wizard/app.js:7112-7117` closes the port and `BoardConnection.reconnect` (`app.js:5040-5049`) reopens it and pulses DTR false→150 ms→true, which its own comment states "triggers the RC differentiator, giving the board a clean reset at 115200" (same mechanism documented in `connect()` at `app.js:4958-4966`). The correct characterisation: `flasher.js:721` is dead code that never fires, `flasher.js:719` carries a fabricated rationale (no esptool-js release ever exposed `after_flash`), and `:715`/`:724` log a reset that did not happen — so the post-flash reboot silently depends on the reconnect path instead of on the flasher's own explicit step. Fix it as hygiene + defence-in-depth, not as a live user-facing bug. The right shape mirrors the `writeFlash` guard at `:665-669`: `const resetFn = loader.hardReset; if (typeof resetFn === 'function') { try { await resetFn.call(loader); onLog('Hard reset issued'); } catch (_) {} } else { onLog('⚠ esptool-js: hardReset not found — relying on reconnect DTR reset'); }` — drop the bogus `afterFlash`/`after_flash` fallbacks and the wrong comment, and only log success when a reset actually ran.

<details><summary>Full verification reasoning</summary>

MECHANISM — the dead call is real, verified by hand, not taken from the write-up.

1. `Wizard/flasher.js:719-721` (unchanged since 423d8dff, 2026-02-27, per `git blame -L 712,726`):
   `const afterFlashFn = loader.afterFlash ?? loader.after_flash;`
   `try { if (afterFlashFn) await afterFlashFn.call(loader, 'hard_reset'); } catch (_) {}`
2. The bundle actually loaded is `Wizard/vendor/esptool-js/esptool-js-0.4.7.bundle.js` (`flasher.js:32` ESPTOOL_SRC, imported at `flasher.js:317`). I grepped it myself: `afterFlash` → 0 hits, `after_flash` → 0 hits, `hard_reset` → 0 hits, `hardReset` → 2 hits. So `afterFlashFn` is `undefined`, the `if` is always false, and the intended reset never issues. The comment "esptool-js 0.4.x renamed after_flash → afterFlash" is fabricated — no esptool-js version ever had `after_flash`. (Contrast `flasher.js:665-669`, where the same defensive pattern uses the REAL rename `write_flash`→`writeFlash` and THROWS if absent; only :721 fails open.)
3. The real method exists on the class we instantiate: `async hardReset(){await this.transport.setRTS(!0),await this._sleep(100),await this.transport.setRTS(!1)}`, sitting in the same contiguous class body as `writeFlash`/`flashId`/`getFlashSize`/`softReset` (i.e. `Qe`, exported as `ESPLoader`; the class ends `}class Fe{getEraseSize…` = `ROM`). So `loader.hardReset` is truthy at runtime.
4. The chip really is left in the stub by step 4. `writeFlash` ends with `this.IS_STUB&&(await this.flashBegin(0,0), t.compress?await this.flashDeflFinish():await this.flashFinish())`, and `async flashDeflFinish(A=!1){const t=A?0:1;…}` — reboot defaults false → payload 1 = "stay in loader". flasher.js passes `compress:true` (`:681`). No reset anywhere in writeFlash.

So the observation stands: `flasher.js:715` logs "Resetting board into firmware…" and `:724` "Flash complete — board rebooting" while nothing is reset. That is dead code plus a lying log and a wrong comment.

REACHABILITY / IMPACT — this is where the S2 collapses. `flashFirmware` has exactly one caller: `Wizard/app.js:7084` (grepped whole repo incl. index.html inline handlers; the only other hits are comments at app.js:120/285/7044). Every success path from there runs `app.js:7112-7117` (sleep 1500, close port with 6 retries) then `conn.reconnect(_isWindows?14:12, 2000)` → `BoardConnection.reconnect`, `app.js:5040-5049`:
  `await this.port.open({baudRate:115200});`
  `// … The false→true edge also triggers the RC differentiator, giving the board a clean reset at 115200.`
  `setSignals({dataTerminalReady:false}); await 150ms; setSignals({dataTerminalReady:true});`
That is a DELIBERATE, commented hardware reset — not the "incidental DTR/RTS transition" the reviewer dismissed (they cited only the close loop at app.js:7113-7116 and missed the reset comment at :5041-5045 and the sibling comment in `connect()` at `app.js:4958-4966`: "The RC differentiator fires a one-shot reset (not a continuous EN-pin hold), so the board boots normally and stays running with DTR=true"). The repo's own earlier hand-verified verdict relies on the same mechanism (`docs/CODE_REVIEW_2026-08_verdicts.md:757`: re-asserting DTR reboots the board and re-prints the boot banner).

Empirical cross-check: the post-flash path then calls `waitForBoardReady` (`app.js:6686`), which re-sends `?backup` and waits up to 150 s for "End of Backup" before pushing config. If the board genuinely sat in the download stub, EVERY Wizard flash would end in "✕ Timed out after 150s / board did not respond" — a constant, glaring failure. Nothing in docs/ or the memory notes reports it, and the flash→reconnect→push flow has been iterated on repeatedly since February. The reviewer's own text even concedes the board is rebooted by the reopen, then asserts a failure scenario ("the chip stays in the ESP download stub") that contradicts it.

HARDWARE: V2.4 has its own DTR/RTS/EN/IO0 nets (`PCB/…V2.4.kicad_sch` global labels DTR/RTS/EN/IO0/CHREN); V3.1/V3.2 have no USB at all on the WCB PCB — the only component is `ESP32-S3-DEVKITC-1-N8R2`, so the user plugs into the DevKitC's own UART bridge with the stock two-transistor auto-program circuit. On that circuit a close/reopen signal transient is the classic "opening the serial monitor resets an ESP32 devkit" behaviour. I could not settle the exact S3 edge polarity from the repo, but it does not change the verdict: something demonstrably reboots those boards today.

INTENT: nothing documents step 5 as intentionally inert — it is a bug, just an inconsequential one. Per the repo's own scale (`docs/CODE_REVIEW_2026-08.md:10`): S4 = "dead code, stale comment". That is exactly what this is. It is worth fixing (cheap, and it removes a single point of failure — the actual reboot currently depends entirely on app.js's reconnect path), but it is not "wrong behaviour in a real scenario".

</details>


## REFUTED

### `Code/WCB/WCB_Storage.cpp:978` — REFUTED · ⚠ PROPOSED FIX IS WRONG

**?ERASE,NVS clears the hw_version namespace, so a factory reset reboots the board into the hw_version==0 pin map that binds all ten serial pins to GPIO5**

**Correction to the finding as filed:**

The mechanism is right; the disposition is wrong. Correct version: this is not a separate defect at WCB_Storage.cpp:978 — merge it into F-038 (`wcb_pin_map.cpp:14`) as an additional reachability note ("a factory reset on an already-wired board re-enters the hw 0 map, so the contention lands on devices that are physically connected, not just on a bare bench board"). Clearing `hw_version` is documented behaviour in the firmware help (WCB_Help.cpp:955) and the Wizard modal (Wizard/index.html:362-364). Two claims in the write-up must not be repeated: (a) "nothing warns the user before or after" is false — printHWversion() at WCB.ino:7208 prints "SET YOUR HARDWARE VERSION BEFORE PROCEEDING!!" (WCB_Storage.cpp:141) in the boot banner immediately before the SerialN.begin block; (b) "it cannot be re-derived at runtime" is true but not the hazard — the hazard is that hw 0 aliases ten pins onto GPIO5 instead of being inert, which is F-038 and fires on every fresh flash with no erase at all. Residual value here is S4 at most: `eraseNVSFlash()`'s farewell line (WCB_Storage.cpp:1030) could say "NVS cleared — set ?HW,xx before use. Restarting..." That one string is worth taking; nothing else at this line is.

<details><summary>Full verification reasoning</summary>

MECHANISM — every link the reviewer describes is factually correct, and I re-derived each one rather than trusting the write-up:
- `eraseNVSFlash()` (Code/WCB/WCB_Storage.cpp:941) is a hand-maintained namespace sweep; `preferences.begin("hw_version", false); preferences.clear(); preferences.end();` is at :978-980, and nothing re-writes the value before `ESP.restart()` at :1032. The only writer anywhere is `saveHWversion()` (:80-82) — confirmed by `grep -rn hw_version Code/WCB/`, which returns exactly one `putInt`.
- `loadHWversion()` (:100-106) does `getInt("hw_version", 0)` then `updatePinMap()`.
- `wcb_pin_map.cpp:10-25`, hw==0 branch: SERIAL1..5 TX and RX are all literally `5`; `updatePinMap()` ends at :114 with no final `else`.
- WCB.ino:7138 `loadHWversion()`, :7224-7280 the `SerialN.begin()` block. The only guards there are Kyber, `isSerialPortUsedForPWMInput/isSerialPortPWMOutput`, and `baudRates[n] > 0` — and the baud fallback is the compiled 9600 (WCB.ino:622-626 → WCB_Storage.cpp:251 `getInt(key, baudRates[i])`), so nothing skips the begins on a just-erased board.
- Hardware claim verified in the PCB, not assumed: in `PCB/Wireless Communication Board (WCB)V3.2/…kicad_pcb`, module pad `J1_5` carries `(pinfunction "GPIO5")` and `(net 2 "S1_RX")` (~:1590-1600), and the 1x4 S1 header pad "4" is on the same `net 2 "S1_RX"` (~:3250-3260) — a direct trace, no series resistor or buffer.
- Reachability is real: `?ERASE,NVS` (WCB.ino:5069-5071), legacy `WCB_ERASE` (:5208-5209), Wizard `mode:'erase'` (Wizard/app.js:7303) and the Factory Reset "Erase NVS Only" button (app.js:9203).

So the state is real and reachable. It still does not stand as a finding at WCB_Storage.cpp:978, for three independent reasons.

1. DELIBERATE AND DISCLOSED, in two user-facing places. The firmware's own `?HELP,ERASE` lists, under "What gets erased": `Serial.println(F("  - Hardware version"));` — Code/WCB/WCB_Help.cpp:955. The Wizard's factory-reset modal says the same before the user commits: "This will erase all stored settings including baud rates, MAC octets, ESP-NOW password, WCB number, hardware version, sequences, and Kyber config" — Wizard/index.html:362-364. The reviewer's thesis is "hw_version is not configuration and should not be in this list"; that is a design opinion contradicted by the shipped, documented contract at the point of use. Removing it from the sweep is a behaviour change that also falsifies both of those strings.

2. THE "NO WARNING" CLAIM IS FALSE, and it is load-bearing for the S2 call. The finding says "nothing warns the user before or after, and the only visible hint is 'No LED was setup. Define HW version' from WCB.ino:7167". `printHWversion()` is called at WCB.ino:7208 — inside the boot banner, ~16 lines before the `SerialN.begin()` block — and its final `else` prints `"SET YOUR HARDWARE VERSION BEFORE PROCEEDING!!"` (WCB_Storage.cpp:140-142). That is a second, explicit, unmissable console warning on the port that still works.

3. IT IS THE SAME BUG AS AN ALREADY-TRACKED FINDING, at a worse location. Everything harmful here — UART TX driving the S1_RX header pin, three SoftwareSerials colliding on one GPIO5 ISR, the self-echo loop — is a property of the `wcb_hw_version == 0` branch, which fires on EVERY freshly flashed board with no erase involved. That is F-038 (docs/CODE_REVIEW_2026-08_wave2.md:52, `wcb_pin_map.cpp:14`), already hand-verified CONFIRMED/S2 in docs/CODE_REVIEW_2026-08.md:502-514. This finding's own parenthetical concedes it ("documented separately as F-038; what the sweep adds is that ?ERASE,NVS … deterministically produces that state"). What it adds is timing — devices are already wired when a working board is erased — which is a note on when F-038 bites, not a second defect, and certainly not a second S2 at a different file:line.

Recoverability also argues against S2 on its own: the pin map touches only S1-S5 and the LED. USB/Serial0 and the radio are unaffected, so `?HW,32` + reboot restores everything, and the Wizard's erase→push path does exactly that automatically (parser.js:1241 emits `HW,${config.hwVersion}`, app.js:8187 flags `HW,` as reboot-required, app.js:7341/7357 force a full push with `boardBaselines[n] = null; // force full push — board NVS is blank`).

</details>

### `Code/WCB/WCB.ino:6417` — REFUTED · ⚠ PROPOSED FIX IS WRONG

**processIncomingSerial's line-assembly buffer has no maximum length — a port that never sends CR/LF grows a String until the heap is exhausted**

**Correction to the finding as filed:**

Corrected statement of the real issue: `processIncomingSerial` has no sanity cap on its per-port line buffer, so a stream containing no 0x0D/0x0A can grow `serialBuffers[n]` to the ESP32 `String` ceiling of 65535 bytes (`WString.h:335`, PSRAM off on both WCB FQBNs) and hold that allocation permanently — `serialBuffer = ""` at `WCB.ino:6414` does not release it, because `reserve(0)` short-circuits at `WString.cpp:166-168`. Beyond the cap the core drops bytes silently rather than crashing. Severity S4 (defensive hardening / bounded memory retention), not S2 (heap exhaustion).

The separate real bug found while verifying, which deserves its own finding: `?KYBER,LOCAL,S3|S4|S5` is explicitly accepted (`WCB_Storage.cpp:1112`, `:1458`) but `serialCommandTask` only excludes S1/S2 under `Kyber_Local` (`WCB.ino:6607-6609`), so `processIncomingSerial` at `:6603-6605` races `forwardDataFromKyber` (`:4244`) for the same UART and steals Kyber bytes. Symmetrically, `Maestro_Remote` mode leaves S2-S5 processed while `forwardMaestroDataToRemoteKyber` (`:4329`) drains every local Maestro port. Fix by gating on the actual configured port, not a hardcoded S1/S2: `if (isSerialPortKyberLocal(i) || isSerialPortUsedForLocalMaestro(i)) continue;` — mirroring the existing MP3/DFP/HCR early-returns at `:6366-6374`.

If a cap is added anyway, it must be a discard-until-delimiter state (set an overflow flag, keep consuming and dropping bytes until the next 0x0D/0x0A, then clear the flag), and the threshold must be at least ~2 KB — not 200.

<details><summary>Full verification reasoning</summary>

WHAT I OPENED

`Code/WCB/WCB.ino:6360-6420` (the real `processIncomingSerial`), its only caller `serialCommandTask` (`WCB.ino:6595-6626`), the competing UART drains (`forwardDataFromKyber` `WCB.ino:4244`, `forwardMaestroDataToLocalKyber` `:4307`, `forwardMaestroDataToRemoteKyber` `:4329`, `RawSerialForwardingTask` `:6636`), the Maestro query gate (`WCB_Maestro.cpp:415-450`), the Kyber config parser (`WCB_Storage.cpp:1071-1200`, `loadKyberSettings` `:1447-1468`), the ESP32 core String implementation actually compiled (`Arduino15/packages/esp32/hardware/esp32/3.3.4/cores/esp32/WString.{h,cpp}`), `boards.txt`, `Code/bin/build.sh`, `Code/WCB/WCB_OTA.cpp`, and `Wizard/app.js`.

WHAT IS TRUE

The literal code reading is correct. `static String serialBuffers[6]` (`WCB.ino:6380`), `serialBuffer += c` with no length test (`WCB.ino:6416-6418`), buffer cleared only on 0x0D/0x0A (`:6385`, `:6414`), no byte budget on the `while (serial.available())` (`:6382`). The skip list is exactly MP3/DFP/HCR/maestroQueryPort (`:6366`, `:6370`, `:6374`, `:6378`) plus raw-mapped ports at the call site (`:6603-6605`). WLED is genuinely absent from the skip list, and there is no `isSerialPortUsedForMaestro` guard anywhere (grepped the whole `Code/WCB/` tree). No existing cap anywhere in the file.

WHY THE FINDING AS STATED FALLS

1. "grows a String until the heap is exhausted" is factually wrong. `String::changeBuffer` refuses any allocation over `CAPACITY_MAX` (`WString.cpp:199-203`), and `CAPACITY_MAX` is 65535 unless `BOARD_HAS_PSRAM` is defined (`WString.h:329-337`). Both WCB FQBNs — `esp32:esp32:esp32:PartitionScheme=min_spiffs` (`build.sh:89`) and `esp32:esp32:esp32s3:PartitionScheme=min_spiffs` (`build.sh:105`) — omit the PSRAM menu key, and the default menu entry is `disabled` with empty `build.defines` (`boards.txt:1681-1683`, `:1043-1045`). So `BOARD_HAS_PSRAM` is undefined and the buffer is HARD-CAPPED at 64 KB. Past that, `reserve` returns false and `String::concat` returns false at `WString.cpp:330-332` — note this core does NOT call `invalidate()` on that path, so the String simply keeps its contents and the byte is silently dropped. No crash, no OOM, no panic. Same graceful outcome earlier if `realloc` can't find a contiguous block. There is no "heap exhaustion" reachable through this line.

2. The stated failure scenario's key premise is unproven and is probably false. The reviewer asserts of Maestro reply/echo bytes: "None of those bytes are 0x0D/0x0A." A Maestro `getPosition` reply is 2 little-endian bytes of quarter-microseconds (`WCB_Maestro.cpp:445`: `reply[0] | (reply[1] << 8)`) — 0x0A and 0x0D are ordinary byte values in that range. In compact-protocol frames the channel byte is 0-23, so channels 10 and 13 are literally 0x0A and 0x0D. Any binary stream statistically hits a delimiter roughly every ~128 bytes and flushes. Getting anywhere near 64 KB requires a device that provably emits tens of thousands of consecutive bytes containing neither 0x0A nor 0x0D — not the Maestro described.

3. The specific vector is additionally weak. A Pololu Maestro only transmits in reply to a query or from a script's `serial_send_byte`. The query window is gated on both entry and mid-drain (`WCB.ino:6378`, `:6383`; set/cleared at `WCB_Maestro.cpp:439`, `:448`), and each query flushes stale bytes first (`WCB_Maestro.cpp:440`: `while (s.available() > 0) s.read();`). A timed-out reply leaks at most `expectedBytes` (1-2).

WHAT IS REALLY THERE (and is a different, separate bug)

There IS one supported configuration that feeds a genuine non-line-oriented binary stream into this exact buffer, and the reviewer missed it. `?KYBER,LOCAL,S<n>` explicitly accepts S1-S5 (`WCB_Storage.cpp:1112` "Invalid Kyber port. Must be S1-S5"; persisted and reloaded 1-5 at `:1458`), but `serialCommandTask` excludes only S1 and S2 under `Kyber_Local` (`WCB.ino:6607-6609`, comment "Skip Serial1 and Serial2 - handled by Kyber task") while S3/S4/S5 are unconditionally processed at `:6603-6605`. So Kyber on S3-S5 makes `processIncomingSerial(Serial3,3)` race `forwardDataFromKyber()` (`:4244`, 1 ms cadence vs 5 ms) for the same UART. Same shape in `Maestro_Remote` mode: `forwardMaestroDataToRemoteKyber` (`:4329`) drains every local Maestro port while `serialCommandTask` still processes S2-S5. That is a real byte-stealing bug — but its symptom is corrupted Kyber forwarding and garbage commands executed at each embedded 0x0A/0x0D, not heap exhaustion, precisely because the stream keeps flushing. `blockBroadcastFrom` does not help: it is only consulted inside the broadcast path (`WCB.ino:6288`, `:2142`), never in `processIncomingSerial`.

Net: a missing sanity cap on a line assembler is a fair defensive-hardening note, bounded at 64 KB with graceful degradation and no demonstrated real-world trigger. That is S4, not S2.

</details>

### `Code/WCB/WCB.ino:7214` — REFUTED · ⚠ PROPOSED FIX IS WRONG

**migrateOldStoredCommands() runs 200 lines before loadCommandDelimiter(), so the one-shot, irreversible sequence-comment rewrite uses the default '^' instead of the operator's configured delimiter**

**Correction to the finding as filed:**

Corrected statement of the issue: "migrateOldStoredCommands() at WCB.ino:7214 runs before loadCommandDelimiter() at :7414, so normaliseSeqComments (WCB_Storage.cpp:851) always uses '^'. On a board with a non-default delimiter the cosmetic comment normalisation is silently skipped. There is no functional consequence: recallCommandSlot strips everything from '***' onward per part regardless of delimiter (WCB_Storage.cpp:582-583) so comments can never execute, and the Wizard renders the un-normalised form identically (app.js:4150-4152 vs :4166-4168) and rewrites it to the normalised form on the next save (app.js:4091-4096)."

Severity S2 -> S4. Both claimed failure modes are wrong: comments cannot execute (delimiter-independent '***' strip at WCB_Storage.cpp:582-583), and the '^***' rewrite only removes a caret from a part that is truncated at '***' anyway.

Recommended action: leave the call order alone and add one comment at WCB.ino:7214 recording WHY, e.g. "runs before loadCommandDelimiter(); normaliseSeqComments therefore uses '^'. Deliberate — legacy CMDn values predate the current ?DELIM setting and were authored under the default. The normalisation is cosmetic; recallCommandSlot strips '***' comments delimiter-independently." That is the durable artifact here, since the next reviewer will re-raise this.

<details><summary>Full verification reasoning</summary>

THE ORDERING OBSERVATION IS TRUE; THE DEFECT IS NOT.

What I verified as correct in the finding:
- `char commandDelimiter = '^';` is a plain global default (WCB.ino:151). Its only mutators are `loadCommandDelimiter()` (WCB_Storage.cpp:526-532) and `setCommandDelimiter()` (:534-541).
- `migrateOldStoredCommands();` is at WCB.ino:7214; `loadCommandDelimiter();` / `loadLocalFunctionIdentifierAndCommandCharacter();` are at WCB.ino:7414-7415. setup() begins at WCB.ino:7106.
- `normaliseSeqComments` reads the global at WCB_Storage.cpp:851 and is called only from the migration, at :892 and :918; the one-shot guard is `seq_mig_done` (:877, set :926).
- I ran `awk 'NR>=7100 && NR<=7414' Code/WCB/WCB.ino | grep -n "commandDelimiter|LocalFunctionIdentifier|CommandCharacter|commentDelimiter"` — it returns NOTHING. So the migration really is the sole early consumer, and commandDelimiter is provably '^' at :7214 no matter what NVS holds.

So the ordering is real. Both claimed consequences are false.

CLAIM (a) — "every recovered sequence executes its comment text as commands on recall" — REFUTED outright. `recallCommandSlot` (WCB_Storage.cpp:568-589) strips comments in a way that is DELIMITER-INDEPENDENT: for each part it does `int commentPos = part.indexOf(commentDelimiter); if (commentPos >= 0) part = part.substring(0, commentPos);` (:582-583), with `commentDelimiter = "***"` (WCB.ino:152), then drops empty parts (:585). At recall time the delimiter IS correctly loaded (runtime, long after :7414), so `;M11|***note` splits on '|' into `["' ;M11'", "***note"]`; the second part is cut at index 0 and dropped. Even if no split occurred, `indexOf("***")` cuts from `***` onward anywhere in the part. There is no delimiter configuration under which a `***` comment can execute. The comment at :568-569 states this is the explicit purpose.

CLAIM (b) — "rewrites any literal '^***' inside a sequence body to '***' ... damage is permanent" — requires a legacy corpus that both uses a non-'^' delimiter AND contains a literal '^' immediately followed by '***'. Even then, the net effect at recall is that a part reading `cmd^***note` (which :582-583 already truncates to `cmd^`) becomes `cmd` — it drops a stray caret from an already-truncated part. That is not damage.

WHAT IS ACTUALLY LOST IS NOTHING OBSERVABLE. The only real consequence is that the cosmetic normalisation is skipped, and I traced that to a no-op:
- Wizard `seqValueToLines(value, delim)` (app.js:4127-4173) with delim='|': un-normalised `;M11|***note` splits to `[";M11","***note"]`; `prevWasEmpty` is false so :4150-4152 folds it inline -> `;M11 ***note`.
- Had normalisation worked, `;M11***note` is one part and falls to :4166-4168 -> `;M11 ***note`.
IDENTICAL rendering. And `seqTextareaToValue` (app.js:4091-4096) rejoins lines on save, so the stored string self-normalises on the first Wizard save regardless. The `^^***` standalone case is deliberately protected by :857/:859 and is untouched on both paths.

THE FINDING'S PREMISE IS ALSO UNSOUND. It assumes a legacy corpus is marked with the delimiter configured TODAY. Stored values are authored under whatever delimiter was live at authoring time. The likeliest way a board holds both pre-key_list `CMDn` keys and a non-'^' delimiter is: authored under the default '^' on old firmware, then `?DELIM,|` later (WCB.ino:5033-5037 -> setCommandDelimiter). In that corpus the separators genuinely ARE '^', so today's '^'-based migration is the correct one and the proposed fix would invert it.

INTENT: the migration's own comment block (WCB_Storage.cpp:838-848) frames this as display/round-trip hygiene ("matching how the wizard now displays and saves sequences"), not correctness. :922-925 carries a deliberate belt-and-braces comment about setup() reordering, showing the author considered ordering here. This finding is wave1.md:1625; I grepped docs/CODE_REVIEW_2026-08_verdicts.md and it has no verdict yet, though :2380 and :2405 independently establish the same "display-only, cannot break a board" ceiling for the adjacent `^^***` finding.

Downgrade to S4: a genuine latent ordering inconsistency worth a one-line comment, with provably zero user-visible effect.

</details>

### `Wizard/app.js:5361` — REFUTED

**Boot-banner sniffer dereferences boardConfigs[n] unguarded — TypeError kills the read loop and starts an endless reset/reconnect loop**

**Correction to the finding as filed:**

Downgrade to S4 (latent/defensive, not user-reachable). Corrected statement: `Wizard/app.js:5361-5363` reads `boardConfigs[n]` without the `if (boardConfigs[n])` guard its four sibling writers use (`:5338`, `:5344`, `:5350`, `:5355`), which is inconsistent with the deliberately-defensive `boardBootChars` fallback pattern (`:45`, consumed at `:1538`, `:6739`, `:6802`, `:6952`, `:12461`). It is NOT reachable today: the only caller that can attach a BoardConnection to a slot with no card is `boardAutoDetect` (`:6524`, bulk block `:6569-6592`), which has zero callers — no inline handler in `Wizard/index.html`, no reference in `app.js` — and is already documented as dead code in `docs/CODE_REVIEW_2026-08_wave3.md:2788-2820`. Every live connect path binds `n` to a rendered card, and `addBoardSection` (`:616-619`) unconditionally creates `boardConfigs[n]`; `reconcileBoardGrid` (`:454`) never deletes a config for a live slot; both migrate paths (`:6827`→`:6842`, `:6886`→`:6890`) set the new slot's config synchronously in the same task. Worth the one-line guard as cheap insurance, but it should not block a release.

<details><summary>Full verification reasoning</summary>

WHAT I OPENED

1. `Wizard/app.js:5304-5403` — the real `BoardConnection._handleLine()`. The reviewer's transcription of 5335-5373 is accurate: the four writers are guarded (`if (boardConfigs[n]) …` at :5338, :5344, :5350, :5355), `charDetected = true` is set unconditionally at :5339, :5345, :5351, and the consumer at :5361-5363 reads `boardConfigs[n].funcChar/.delimiter/.cmdChar` with no guard. So the *code shape* claim stands.

2. The crash-propagation claim also stands mechanically. `_handleLine` is called from inside the inner `try` of the read loop (`Wizard/app.js:5520` `if (line) this._handleLine(line);`). A TypeError there is caught at :5523-5525 → `termLog(…, 'Port closed: ' + e.message, 'sys'); break outer;` → falls into :5537 `if (this._connected && !this._rebootManaged)` → `updateConnectionUI(false)` + `this.reconnect(10, 1500)`, which reopens the port, re-asserts DTR (:5544 in `connect`, and the comment at :5540-5546 confirms the RC differentiator fires a one-shot reset), the board reboots, prints the banner again, and the cycle repeats. And the firmware does print those exact lines — `Code/WCB/WCB.ino:7428-7430` (`"Delimeter Character: %c\n"`, `"Local Function Identifier: %c\n"`, `"Command Character: %c\n"`).

WHY IT FALLS: THE REQUIRED STATE IS UNREACHABLE

The finding's entire reachability argument is the first-time bulk auto-detect at `Wizard/app.js:6569-6592` inside `boardAutoDetect(n)` (`:6524`). **`boardAutoDetect` is dead code.** `grep -rn "boardAutoDetect|boardManualConnect"` over the whole repo returns only the two definitions (`:6485`, `:6524`) and a stale comment at `:7614`. I dumped every inline handler in the HTML (`grep -o 'onclick="[^"]*"' Wizard/index.html | sort -u`, 63 unique) — neither name appears; the only connect entry point is `onclick="event.stopPropagation();boardConnect({N})"` at `Wizard/index.html:428`. The repo's own prior review already documented this: `docs/CODE_REVIEW_2026-08_wave3.md:2788-2820` ("boardManualConnect, boardAutoDetect and the entire port-picker modal are unreachable dead code"). So the described user story — "Connect → Auto-Detect, picker authorizes 3 ports, slots 1/2/3 claimed" — cannot happen in the shipped Wizard.

I then checked whether any *live* path can produce "BoardConnection on slot n with no `boardConfigs[n]`":

- `boardConnections[x] = conn` happens in exactly 4 places: `:5728` (`establishConnection`), `:5771` (`sharedConnect`), `:6828`, `:6887` (the two boardPull migrations).
- Live callers of `establishConnection`: `:5927` (`_modalDoConnect`), `:11585`/`:11605`/`:11629` (the three `wizPortComplete` contexts). The dead ones are `:6512`, `:6582`, `:6621`. Every live caller takes `n` from `_connectModalSlot` (set by `openConnectModal(n)`, whose only caller is `boardConnect(n)` at `:6187`, whose button lives inside the rendered card) or from a `b{n}-*` / `wiz-*-{n}` element that only exists in a rendered card/row.
- `addBoardSection(n)` (`:596`) *unconditionally* ensures the slot's config: `:616-619` `if (!boardConfigs[n]) { boardConfigs[n] = WCBParser.createDefaultBoardConfig(); … }`. So any slot with a card has a config.
- Wizard flow: `wizardSelectConnectMode` (`:10429`) calls `wizardApplyConfig()` → `renderBoards(ws.quantity)` (`:10888`) *before* the connect rows/port panels open, so slots 1..qty all have configs.
- The only two places `boardIndex` is reassigned are `:6827` and `:6886`; in both, the new slot's config is written synchronously in the same task (`:6842` and `:6890`) with no `await` in between, so no serial line can be dispatched in the gap.
- `reconcileBoardGrid` (`:443`) is the only place a config is deleted out from under a slot (`:457`), and it is explicitly gated by `:454` `if (boardConnections[n]?.isConnected?.()) continue; // never yank a live board`. `removeClientCard` (`:12598`) carries the same guard and deliberately *keeps* `boardConfigs[n]`. The flash/erase paths (`:7143`, `:7185`, `:7201`, `:7258`, `:7340`, `:7356`) only ever restore configs, never delete them.

INTENT

The surrounding code shows the author was deliberately treating `boardConfigs[n]` as possibly-absent — that is the whole reason `boardBootChars` exists (`:45`), and it is consumed as a fallback at `:1538`, `:6739`, `:6802`, `:6952`, `:12461` (`boardConfigs[n]?.funcChar ?? boardBootChars[n]?.funcChar ?? '?'`). `git log -L5335,5375:Wizard/app.js` shows the whole block arrived in one commit (4a524c1, cross-tab port sharing) already in this shape — the missing guard at :5361 is an oversight in an otherwise-defensive block, not a documented trade-off.

CONCLUSION

The observation is correct as an inconsistency; the failure scenario, the reachability argument, and therefore the S1 severity are wrong. It is a latent robustness gap that only becomes live if someone revives the bulk-connect block or adds a connect path that isn't card-bound.

</details>
