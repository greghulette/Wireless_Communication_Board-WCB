# WCB Code Review — Wave 1 findings (`WCB.ino`, partial)

Companion to [CODE_REVIEW_2026-08.md](CODE_REVIEW_2026-08.md). Generated 2026-08-18.

> **PARTIAL WAVE.** 6 of 12 scopes completed before a session limit; 6 were killed.
> **Still unreviewed — see the master log for the list, `seg07-rxcallback` especially.**

> **STATUS: ALL UNVERIFIED** — reviewers-only run by design.

**Totals:** 47 findings — S1: 3 · S2: 17 · S3: 17 · S4: 10


---

## S1

### F-257 · S1 · `Code/WCB/WCB.ino:1470`

**etmCharPhaseSentTimes[3][200] is written with an unbounded index - ?ETM,CHAR overruns it with >10 online peers**

*Category:* `bounds`

`unsigned long etmCharPhaseSentTimes[3][200];` (WCB.ino:577) is indexed by `etmCharPhaseMessageIndex`, whose bound is `totalMessages = etmCharMessageCount * peerCount` (WCB.ino:1456). Neither factor is clamped against 200:
  * `peerCount` is built at WCB.ino:1445-1449 by walking all MAX_WCB_COUNT (=20) slots; it can reach 19.
  * `etmCharMessageCount` is `constrain(..., 10, 200)` at WCB.ino:4696 and :5303 - i.e. up to 200, default 20.

The three write sites have no guard at all:
  WCB.ino:1470  etmCharPhaseSentTimes[etmCharPhase - 1][etmCharPhaseMessageIndex] = now;
  WCB.ino:1506  etmCharPhaseSentTimes[2][msgIdx] = now;
  WCB.ino:1513  etmCharPhaseSentTimes[2][msgIdx] = now;

The *read* side was guarded - etmProcessAck at WCB.ino:1249 checks `mIdx >= 0 && mIdx < 200` - which shows the 200 limit was known, but the producer was never clamped to match.

With default settings, 11 online peers already gives totalMessages = 20*11 = 220 > 200. In phase 3 the writes go to row index 2 (the last row), so indices >=200 write past the end of the whole 2400-byte array into adjacent .bss - which is `etmCharPhaseMessageIndex` (:578), `etmCharPhaseStartTime` (:579), `etmCharLastSendTime` (:580), `etmLoadRunning` (:583) and friends. Corrupting the loop counter it is itself indexed by makes the overrun unbounded.

(Related stale comment: WCB.ino:575 says "[phase 0-2][board index 0-7]" while the array below it is [3][MAX_WCB_COUNT] - a leftover from the max-8 cap family.)

**Failure scenario.** A fleet of 12+ WCBs with ETM on, all heartbeating. Operator runs `?ETM,CHAR` (or the Wizard's ETM characterization button). peerCount = 11+, etmCharMessageCount = 20 default -> totalMessages >= 220. Phase 3 writes etmCharPhaseSentTimes[2][200..219], i.e. 20 unsigned longs past the end of the array, clobbering etmCharPhaseMessageIndex/etmCharPhaseStartTime/etmLoadRunning. Observable outcome: garbage/hung characterization, a phantom ETM load test starting itself, or a crash once the corrupted index drives the next write further out. Raising `?ETM,COUNT` to 200 makes it fire with only 2 peers.

**Suggested fix.** Clamp at the source: in processETMChar() compute `totalMessages = min(etmCharMessageCount * peerCount, (int)ETM_CHAR_MAX_MSGS)` with `#define ETM_CHAR_MAX_MSGS 200`, and add `if (etmCharPhaseMessageIndex >= ETM_CHAR_MAX_MSGS) { advanceETMCharPhase(...); return; }` before the writes at :1470/:1506/:1513. Better: size the array from MAX_WCB_COUNT and the count cap, or store sent-times per (phase, board) instead of per message. Also fix the stale "board index 0-7" comment at :575.

### F-258 · S1 · `Code/WCB/WCB.ino:6490`

**processSerialCommandHelper calls parseCommandGroups/stopTimerSequence from serialCommandTask, racing loop()'s live reference into the commandGroups vector**

*Category:* `concurrency`

WCB.ino:6490 `parseCommandGroups(data);` and WCB.ino:6431 `stopTimerSequence();` are reached from `processSerialCommandHelper()` <- `processIncomingSerial()` <- `serialCommandTask` (created at WCB.ino:7439, pinned core 1 prio 1). They are NOT on the loop task.

The queue at WCB.ino:390-399 exists precisely to stop this, and its comment states the invariant:
  "parseCommandGroups() writes the commandGroups std::vector ... It must NOT be called from the ESP-NOW WiFi-task callback because loop() concurrently iterates the same vector via processCommandGroups() - a vector resize concurrent with iteration is undefined behavior."
and then asserts (WCB.ino:398-399): "The local-serial entry point and SEQ playback both run inside loop() already, so they keep calling parseCommandGroups() directly."

That second sentence is factually wrong. `processIncomingSerial` has exactly one set of callers - WCB.ino:6598, 6603-6605, 6613, 6618, 6621 - all inside `serialCommandTask`. So the local-serial entry point runs on a *different FreeRTOS task* from loop(), and the race the queue was built to prevent is wide open on the serial path.

The consumer holds a live reference across an explicit yield. `processCommandGroups()` (command_timer.cpp:186-193, called from loop() at WCB.ino:7481) does:
    CommandGroup &group = commandGroups[currentGroupIndex];   // reference into vector storage
    ...
    for (const String &cmd : group.commands) {
        parseCommandsAndEnqueue(cmd, 0);
        vTaskDelay(pdMS_TO_TICKS(1));   // command_timer.cpp:213 - blocks, hands core 1 to serialCommandTask
    }
While blocked there, serialCommandTask (ready every 5 ms, WCB.ino:6624) can run `parseCommandGroups()`, which does `commandGroups.clear()` (command_timer.cpp:84) and then `commandGroups.push_back(...)`, destroying/reallocating the storage `group` and the range-for iterator point into. `stopTimerSequence()` (command_timer.cpp:57) does the same via `commandGroups.clear()`.

Both tasks are priority 1 on core 1, so FreeRTOS time-slicing already round-robins them; the `vTaskDelay(1)` makes the handoff deterministic at the exact instant the dangling reference is held.

**Failure scenario.** A timer chain is running (e.g. `;t1000^;S1open^;t1000^;S1close`) so loop() is inside processCommandGroups(), holding `CommandGroup &group` and yielding 1 ms between commands. The operator (or the Wizard over USB) types `?STOP`, or a second `;t...` chain, on the USB console. serialCommandTask picks it up during that 1 ms yield and calls stopTimerSequence()/parseCommandGroups(), which frees the CommandGroup and its inner std::vector<String>. loop() resumes and dereferences `cmd` -> read of freed heap: LoadProhibited panic / garbage command executed / heap corruption. Reproduces reliably under any load where serial input arrives while a timer sequence is mid-run.

**Suggested fix.** Route the local-serial timer path through the existing pendingTimerChainQueue instead of calling parseCommandGroups() inline: at WCB.ino:6490 call `enqueuePendingTimerChain(data)` (the flags are already correct - processIncomingSerial sets lastReceivedViaESPNOW/inSequenceBody at :6404/:6408 just before). Do the same for the `?STOP` path at WCB.ino:6431 (enqueue a stop marker, or set a `volatile bool timerStopRequested` that loop() acts on). Then correct the comment at WCB.ino:398-399, which currently records a false invariant.

### F-259 · S1 · `Code/WCB/WCB.ino:6598`

**serialCommandTask parses timer chains off the loop task, racing loop()'s live iteration of the commandGroups std::vector**

*Category:* `concurrency`

`serialCommandTask` (WCB.ino:6595-6627, `xTaskCreatePinnedToCore(serialCommandTask, "Serial Command Task", 4096, NULL, 1, NULL, 1)` at WCB.ino:7439) is a FreeRTOS task DISTINCT from Arduino's `loopTask`. It calls `processIncomingSerial(Serial, 0)` (line 6598) and the five port variants (6603-6621); `processIncomingSerial` (6361) calls `processSerialCommandHelper` (6411), which reaches two functions that destructively rewrite the shared `commandGroups` std::vector:
  - WCB.ino:6429-6432 `if (checkForTimerStopRequest(data)) { stopTimerSequence(); }` -> `command_timer.cpp:54 commandGroups.clear();`
  - WCB.ino:6490 `parseCommandGroups(data);` -> `command_timer.cpp:85 commandGroups.clear();` then `push_back` at :123 and :177, plus `currentGroupIndex = 0` at :86.

Meanwhile `loop()` calls `processCommandGroups()` (WCB.ino loop body, alongside `drainPendingTimerChains()`), and that function takes a REFERENCE into the vector and then deliberately yields while holding it:
```
command_timer.cpp:187  CommandGroup &group = commandGroups[currentGroupIndex];
command_timer.cpp:203  for (const String &cmd : group.commands) {
command_timer.cpp:207      parseCommandsAndEnqueue(cmd, 0);
command_timer.cpp:208      vTaskDelay(pdMS_TO_TICKS(1)); // <- Give queue time to breathe
```
During that `vTaskDelay`, `serialCommandTask` (which wakes every 5 ms, line 6625) can run and `clear()` the vector; `group` and `group.commands` become dangling and the range-for continues walking freed String storage.

This is exactly the hazard the `pendingTimerChainQueue` was built to prevent, and the declaration comment claims it is already covered:
```
WCB.ino:397-399  // The local-serial entry point and SEQ playback both run inside loop()
                 // already, so they keep calling parseCommandGroups() directly.
```
That statement is factually wrong for the local-serial entry point: it runs on `serialCommandTask`, not `loop()`. (SEQ playback via WCB_Storage.cpp:615 IS loop-side, so only half the comment holds.)

Secondary consequence on the same path: `processIncomingSerial` writes the globals `lastReceivedViaESPNOW = false` and `inSequenceBody = false` (WCB.ino:6404, 6409) while `processCommandGroups` is inside its save/set/restore window for the same two globals (command_timer.cpp:199-211), so a timer group's commands can be enqueued with the wrong ESP-NOW origin and the saved value can be restored over the console's reset.

**Failure scenario.** Board is running a timer chain (`;t500,;S1,HELLO^;t500,;S2,BYE`) so `commandTimerModeEnabled` is true and loop() is inside `processCommandGroups`' `for (const String &cmd : group.commands)` at its `vTaskDelay(pdMS_TO_TICKS(1))`. A second command arrives on USB/S1-S5 in that 1 ms window - either `?STOP` or any line containing `;t` - `serialCommandTask` runs `stopTimerSequence()`/`parseCommandGroups()`, which calls `commandGroups.clear()`, freeing the CommandGroup and its `std::vector<String>`. loop() resumes the range-for over freed memory: LoadProhibited / heap-corruption panic, or silent execution of garbage command strings out the serial ports. Reproducible any time a user types while a `;t` sequence is mid-flight.

**Suggested fix.** Route the local-serial path through the existing queue instead of calling into command_timer directly from serialCommandTask: in `processSerialCommandHelper`, replace the direct `parseCommandGroups(data)` (6490) and `stopTimerSequence()` (6430) with `enqueuePendingTimerChain(data)` / a queued stop request drained by `drainPendingTimerChains()` in loop(). Alternatively guard every `commandGroups` mutation and `processCommandGroups()` with a portMUX/recursive mutex. Fix the comment at WCB.ino:397-399 to stop asserting the local-serial entry point is loop-side.


---

## S2

### F-260 · S2 · `Code/WCB/WCB.ino:2298`

**ETM checksum mode silently destroys the CRC for commands over 187 chars — target ACKs, then discards**

*Category:* `protocol`

In the ETM send path, with `?ETM,CHKSM,ON`:

```c
uint32_t crc = calculateCRC32(String(message));
char withCRC[210];
snprintf(withCRC, sizeof(withCRC), "%s|CRC%08X", message, crc);   // :2297
strncpy(etmMsg.structCommand, withCRC, sizeof(etmMsg.structCommand) - 1);  // :2298
etmMsg.structCommand[sizeof(etmMsg.structCommand) - 1] = '\0';    // :2302
```

`structCommand` is `char[200]` (`WCB.ino:781`), so `strncpy` copies at most **199** chars. The suffix `|CRC` + 8 hex digits is 12 chars, so the CRC only survives intact when `strlen(message) + 12 <= 199`, i.e. **message <= 187 chars**. Nothing in the code enforces that cap — I grepped the whole sketch for 186/187/188 and there is no length guard anywhere on this path.

For a message of 188..195 chars the `|CRC` marker survives but the hex digits are cut, so the receiver's `strtoul(crcStr)` (`WCB.ino:3865`) yields a wrong value and `rxCRC != calcCRC` (`:3867`) discards it. For 196..199 chars the `|CRC` marker itself is cut, `lastIndexOf("|CRC")` returns -1 (`:3856`) and it is discarded as "missing CRC".

The failure is completely silent in both directions:
- The receiver sends `etmSendAck()` at `WCB.ino:3823`, **before** the CRC block at `:3854-3873`. So the sender gets a valid ACK, `etmProcessAck` clears the pending entry, `espnowCommandSuccess` is incremented (`:2318`) and `?STATS` shows 100% delivery.
- The receiver only logs the rejection under `debugETM` (`:3858`, `:3868`), which is off by default.

This is directly reachable from this same range: `handleMgmtForward` gates the single-chunk ETM shortcut at `payload.length() <= 198` (`WCB.ino:2597`) and prepends a 1-byte marker (`:2598`), so it hands `sendESPNowMessage` strings of up to **199** chars. Its own comment computes the budget as "198 = 200 - 1 (marker) - 1 (null)" — it never accounts for the 12 CRC bytes. The Wizard sends `?SEQ,SAVE,<key>,<value>` as one such single chunk (`Wizard/app.js:4455`) with no length limit.

The identical construction in the ETM retry builder (`WCB.ino:1311-1314`) has the same flaw, so retries reproduce the truncation byte-for-byte rather than recovering.

**Failure scenario.** Fleet has `?ETM,CHKSM,ON`. Wizard manages WCB3 through relay WCB1 and the user saves a sequence whose command is `?SEQ,SAVE,dome_open,<180 chars>` = 190 chars. Relay takes the `<=198` branch (:2597), marks it (191 chars), `sendESPNowMessage` appends the CRC (203 chars) and truncates to 199 — the CRC hex is cut from 8 digits to 4. WCB3 ACKs the packet (:3823), then rejects it on CRC mismatch (:3867) with no output (debugETM off). Relay reports success, Wizard shows a green "Sequence updated" toast and patches its local baseline, so the diff push never retries. The sequence is never stored on WCB3 and nothing anywhere reports a failure.

**Suggested fix.** Reject (or fragment) before sending rather than truncating. In `sendESPNowMessage`, when `etmChecksumEnabled`, check `strlen(message) + 12 > sizeof(etmMsg.structCommand) - 1` and log + return an error instead of silently emitting a corrupt frame; define the limit as a named constant (e.g. `ETM_MAX_CMD_CHKSM = 187`) and use it at `WCB.ino:2597` in place of the hard-coded 198 (`totalChunks == 1 && payload.length() <= (etmChecksumEnabled ? ETM_MAX_CMD_CHKSM - 1 : 198)`), falling through to the fragmented path otherwise. Apply the same guard to the retry builder at `:1311-1314`. Separately, move `etmSendAck()` to after CRC verification, or add a distinct NACK, so a CRC failure is not reported to the sender as success.

### F-261 · S2 · `Code/WCB/WCB.ino:2599`

**MGMT single-chunk relay prepends '\x01' but the non-ETM receive path never strips it — all single-chunk Wizard commands die when ETM is off**

*Category:* `protocol`

`handleMgmtForward` unconditionally prepends the wizard-origin SOH marker and sends with `useETM = true`:

```c
String markedPayload = String("\x01") + payload;      // :2598
sendESPNowMessage(targetWCB, markedPayload.c_str(), true);  // :2599
```

But `sendESPNowMessage` only takes the ETM path when `etmEnabled && useETM` (`WCB.ino:2281`). With ETM disabled, it falls through to the **normal** 249-byte send path (`:2330-2353`) with the SOH still attached.

The marker is stripped in exactly one place — the ETM receive branch:
```c
bool wizardOrigin = (etmCmd.length() > 0 && (uint8_t)etmCmd[0] == 0x01);  // :3881
if (wizardOrigin) etmCmd = etmCmd.substring(1);                            // :3882
```
I grepped the whole sketch for `x01` / `SOH`: the only hits are `:2593`, `:2598`, `:3876`, `:3881`. The normal receive path (`:4158-4230`) has no equivalent — it builds `String receivedCmd = String(received.structCommand)` (`:4197`) with the SOH intact and dispatches it via `parseCommandsAndEnqueue(receivedCmd, 0)` (`:4227`).

Downstream, the leading 0x01 breaks every prefix test: `restOfString.startsWith(LocalFunctionIdentifier + "SEQ,SAVE,")` (`:2054`), the `?CS` test (`:2016`) and the `?MGMT` test (`:2081`) all fail, and `singleCmd.trim()` does not remove 0x01 (Arduino `String::trim` strips `isspace()` only). The command is enqueued as `"\x01?SEQ,SAVE,..."` and dropped by `processLocalCommand` as unrecognised.

The multi-chunk path is unaffected — it broadcasts `espnow_struct_mgmt` directly (`:2620`) and `handleMgmtPacket` never checks `etmEnabled`. So with ETM off, multi-chunk config push still works while every single-chunk remote command silently dies, which makes this very hard to diagnose from the symptoms.

**Failure scenario.** Operator runs `?ETM,OFF` fleet-wide (a supported mode — `WCB.ino:4676`, `:5333`). Wizard manages WCB3 via relay WCB1. Every single-chunk MGMT command — sequence save (`Wizard/app.js:4455`), remote terminal send (`:8901`), WLED preset (`:2879`), mapping clear (`:3650`), `?RTERM,START` (`:6072`) — is relayed as a normal ESP-NOW packet carrying `"\x01?..."`. WCB3 receives it, sets `lastReceivedViaESPNOW = true` (:4159), enqueues the SOH-prefixed string and discards it as an invalid command. Nothing is logged unless `?DEBUG,ON` is set, and even then it prints the invisible SOH. Meanwhile 'Push Config' (multi-chunk) works fine, so the operator concludes the relay link is healthy.

**Suggested fix.** Either strip the marker in the non-ETM receive path too — after `received.structCommand[...] = '\0'` at `WCB.ino:4196`, add the same `(uint8_t)receivedCmd[0] == 0x01` test used at `:3881`, setting `lastReceivedViaESPNOW = !wizardOrigin` instead of the unconditional `= true` at `:4159` — or, in `handleMgmtForward`, only prepend the marker when `etmEnabled` is true so the non-ETM fallback carries a plain command.

### F-262 · S2 · `Code/WCB/WCB.ino:2617`

**handleMgmtForward silently truncates every MGMT FRAG payload to 179 chars instead of rejecting or re-fragmenting**

*Category:* `logic`

The multi-chunk relay branch copies the payload into the wire struct with:

```c
strncpy(pkt.payload, payload.c_str(), sizeof(pkt.payload) - 1);  // :2617
pkt.payload[sizeof(pkt.payload) - 1] = '\0';                     // :2618
```

`espnow_struct_mgmt::payload` is `char[180]` (`WCB.ino:793`), so this transmits at most **179** data chars. There is no length check anywhere above it — the only validation is `targetWCB`, `totalChunks == 0` and `totalChunks > MGMT_MAX_CHUNKS` (`:2583`). Anything longer is dropped with no log and no error to the caller. Two live paths hit this:

**(a) Single-chunk commands over 198 chars.** The ETM shortcut at `:2597` only fires for `totalChunks == 1 && payload.length() <= 198`; a longer single-chunk payload falls through to this branch and is cut at 179. The target then sees `chunkIdx 0 / totalChunks 1`, so `receivedMask == expectedMask` immediately (`:2702-2703`) and it executes the truncated string via `parseCommandsAndEnqueue(fullCmd, 0)` (`:2731`) — a half-command, not a dropped one. `Wizard/app.js:4455` sends `?SEQ,SAVE,<key>,<value>` this way with `0,1` and no length cap, so the truncated body is written to NVS by `saveStoredCommandsToPreferences`.

**(b) Full-size chunks of a multi-chunk push.** `Wizard/app.js:67` sets `MGMT_CHUNK_SIZE = 180` and `:7852` slices with `fragmentString(cmdToSend, MGMT_CHUNK_SIZE)`, so every chunk but the last is exactly 180 chars — and the relay forwards only the first 179. One character is dropped from each full chunk. The correct contract is 179, as the sibling client library already documents: `WCBClient/src/WCB_Client.h:189` — `#define WCB_MGMT_CHUNK_LEN 179 // payload[180] minus NUL (firmware strncpy)`.

The receiving side is sized for a full 180 (`strncpy(..., MGMT_PAYLOAD_SIZE)` then `chunks[idx][180] = '\0'`, `:2693-2694`), so the loss is entirely on the sender.

**Failure scenario.** (a) WCB3 is reached through relay WCB1. The user edits sequence `dome_open` to a ~200-char body; the payload `?SEQ,SAVE,dome_open,<200 chars>` is 220 chars > 198, so the relay takes the broadcast branch and cuts it at 179. WCB3 stores a body ending mid-token (e.g. `...^;t5`) and the Wizard shows a green success toast, patching the FULL value into `boardConfigs`/`boardBaselines` (app.js:4472-4477) so the diff push will never re-send it. Running `;SEQdome_open` executes a partial sequence. (b) A full 'Push Config' to WCB3 produces, say, 3 chunks of 180/180/94 chars; chars at offsets 179 and 359 are silently deleted, so the reassembled config chain has two corrupted tokens (e.g. `?BAUD,3,115200` becomes `?BAUD,3,11520` merged with the next token) which are then written to NVS.

**Suggested fix.** Make the relay refuse what it cannot carry rather than truncating: after parsing, `if (payload.length() > sizeof(pkt.payload) - 1) { Serial.printf("[MGMT] FRAG payload %u > %u — rejected\n", payload.length(), sizeof(pkt.payload) - 1); return; }`. Then fix the two senders to match the 179-char contract: set `MGMT_CHUNK_SIZE = 179` in `Wizard/app.js:67` (matching `WCB_Client.h:189`) and route `updateSequence` (`app.js:4455`) through `fragmentString`/multi-chunk instead of hard-coding `0,1`.

### F-263 · S2 · `Code/WCB/WCB.ino:3002`

**handleConfigReqPacket silently sends NOTHING when the config exceeds 2912 chars — reachable with ~80 stored variables alone**

*Category:* `logic`

The real relay-pull ceiling is `MGMT_MAX_CHUNKS * (CONFIG_PAYLOAD_SIZE - 1)` = 16 (WCB.ino:837) * 182 (WCB.ino:3000, `CONFIG_PAYLOAD_SIZE` 183 at WCB.ino:252) = **2912 characters**. Over that:

```c
  int totalChunks = max(1, (totalLen + chunkStride - 1) / chunkStride);
  if (totalChunks > MGMT_MAX_CHUNKS) {
    if (debugMGMT) Serial.printf("[MGMT] Config too large (%d chars) — cannot send\n", totalLen);
    return;
  }
```

Nothing is truncated — the whole response is dropped, and the only trace is behind `debugMGMT`, which defaults to `false` (WCB.ino:256). The requesting relay gets no frag, no error frame, no NAK.

What `collectConfigCommands` can emit easily exceeds 2912. `emitHelpers()` (WCB.ino:2863) calls `printVariablesBackup`, which walks all `WCB_MAX_VARIABLES` = 100 slots (WCB_Variables.h:26) and appends `defSep + defFunc + "VAR,SET," + name + "," + value` per persistent variable (WCB_Variables.cpp:484-488). With `WCB_VAR_NAME_MAX` = 15 (WCB_Variables.h:27) and a signed 32-bit value that is up to 2+8+15+1+11 = 37 chars each → **3700 chars from variables alone**, before the ~700-900 chars of base settings (HW/MAC/WCB/BAUD x5/LABEL x5/BCAST x11/ETM x8/...), the Maestro/MP3/DFP/HCR/WLED sections, and the `SEQ,SAVE,<key>,<value>` lines at WCB.ino:2910 — which emit every stored sequence's **full value** with no cap of any kind.

The comment at WCB.ino:2758-2760 claims collectConfigCommands ended the "works on serial, missing on relay" drift. It unified the *content*, but the relay transport still cannot carry it: `printBackupConfig` over serial has no chunk ceiling, so the same board backs up fine over USB and returns nothing over the mesh.

The project already knows this is a bug — WCB.ino:3210-3215 says "Silent overflow is precisely what makes the config-pull path unusable for sequences (it sends nothing and logs only under debugMGMT), and reintroducing it here would recreate the bug this whole feature exists to route around." The SEQVAL path was fixed with an explicit `TOOBIG` status (WCB.ino:3221-3224); the config path was not.

**Failure scenario.** A board with ~80 persistent variables (or a dozen medium stored sequences) is pulled through a relay. `?MGMT,PULL,<n>` → buildConfigString() returns ~3200 chars → totalChunks = 18 > 16 → `return`. The Wizard's remoteBoardPull retries 3 times at PULL_TIMEOUT_MS = 6000 ms (Wizard/app.js:7892-7894), then marks the board `error` after ~20 s with no explanation. With debugMGMT off (the default) neither board prints anything. Worse for relayRouteAll (Wizard/app.js:503), which pulls every discovered board sequentially — one over-sized board costs 20 s and yields nothing.

**Suggested fix.** Mirror the SEQVAL fix: when totalChunks > MGMT_MAX_CHUNKS, send a one-chunk frag carrying an explicit status (e.g. `[CONFIGERR:TOOBIG,<len>,<max>]`) instead of returning, and print the refusal unconditionally rather than under debugMGMT. Longer term, either raise MGMT_MAX_CHUNKS (receivedMask is uint16_t, so 16 is the hard ceiling — it would need a uint32_t mask) or omit `SEQ,SAVE` values from the relay config string and let the consumer walk them via the existing SEQ/SEQGET inventory path.

### F-264 · S2 · `Code/WCB/WCB.ino:3088`

**Multi-kilobyte blocking Serial.printf of reassembled MGMT payloads runs inside the ESP-NOW receive callback**

*Category:* `concurrency`

`handleConfigFragPacket()` is called INLINE from espNowReceiveCallback (WCB.ino:3651), i.e. on the WiFi task. On the final fragment it builds the whole reassembled config and writes it straight to UART0:
    WCB.ino:3082  String fullConfig = "";
    WCB.ino:3083  for (...) fullConfig += String(pullSession.chunks[i]);
    WCB.ino:3088  Serial.printf("[MGMT:CONFIG,%d]%s\n", srcWCB, fullConfig.c_str());
Size bound: MGMT_MAX_CHUNKS (16, WCB.ino:837) * CONFIG_PAYLOAD_SIZE (182) = ~2.9 KB. Serial is UART0 at 115200 (WCB.ino:7121), so that write blocks the WiFi task for ~250 ms. The four siblings do the same and are also inline callback paths (WCB.ino:3652-3656): :3329, :3364, :3399, :3434.

This is the exact failure the codebase already fixed elsewhere, twice, and documented:
  * WCB_RemoteTerm.cpp:12-15 - "Packets are enqueued from the WiFi-task callback and dequeued in loop(). This prevents blocking UART writes inside the ESP-NOW receive callback, which caused the WiFi watchdog to fire and restart the board."
  * WCB.ino:313-323 - the rcJsonRelayQueue, added because "ESP32's Serial (HWCDC or UART) is NOT atomic across cores - concurrent writes can interleave at byte granularity, producing garbled JSON that the config tool's web-serial parser can't process."
  * WCB.ino:383-387 - even the single-line OTA ACK was rerouted through that queue for the same reason.

Both hazards apply here and this payload is an order of magnitude larger than the ones already deferred. Additionally, `Serial` is `WCBDebugSerial` (WCB_RemoteTerm.h:105); when an RTERM session is active every byte also runs through `WCBSerial::_bufChar` (WCB_RemoteTerm.cpp:68-76), which does an unguarded `_lineBuf[_lineLen++] = c` on shared instance state - a concurrent Core-1 Serial write there can push `_lineLen` past LINEBUF_SIZE before the bound test.

**Failure scenario.** Wizard is attached to the USB-tethered relay board and issues `?MGMT,PULL,<n>` to pull a remote board's config. The 16 CONFIG_FRAG packets arrive; on the last one the WiFi task blocks ~250 ms writing 2.9 KB to UART0. During that stall the ESP-NOW RX path is not serviced: inbound frames are dropped, this board's own ETM ACKs are delayed past etmTimeoutMs (500 ms default is only 2x the stall) so peers burn retries, and peers can miss enough heartbeats to flap this board offline. With an RTERM session active the same write also races _lineBuf. Per the WCB_RemoteTerm.cpp comment this class of blocking write previously fired the WiFi watchdog and restarted the board.

**Suggested fix.** Defer the delivery print the same way the codebase already does elsewhere: chunk `fullConfig` into RC_JSON_MAX_LEN pieces and push them via enqueueRcJsonRelay() (or add a dedicated result queue), letting drainRcJsonRelay() in loop() do the UART write. Alternatively route CONFIG_FRAG/STATS_FRAG/SEQ_FRAG/SEQVAL_FRAG/ETM_FRAG through enqueueMgmtReq()/drainMgmtReqs() like their REQ counterparts already are (WCB.ino:3637-3648), so the whole reassemble-and-print runs in loop() context.

### F-265 · S2 · `Code/WCB/WCB.ino:3099`

**sendResultFrags' 2912-char ceiling silently drops ?MGMT,STATS and ?MGMT,ETM results on a full 20-board mesh**

*Category:* `logic`

Same silent-refusal shape as the config path, but here the producers scale with `MAX_WCB_COUNT` = 20 (WCB.ino:120):

```c
  int totalChunks = max(1, (totalLen + chunkStride - 1) / chunkStride);
  if (totalChunks > MGMT_MAX_CHUNKS) {
    if (debugMGMT) Serial.printf("[MGMT] Result too large (%d chars) — cannot relay\n", totalLen);
    return;
  }
```

`buildStatsString()` (WCB.ino:1639) emits one ETM per-board row per active peer (WCB.ino:1690-1702) — `"WCB20: Sent: 1234567, ACKd: 1234567, Retries: 123456, Failed: 12345, Online (last seen 3600s ago)\n"` ≈ 99 chars × up to 19 peers ≈ 1880 — plus a "Reported by Other Nodes" block of up to `MAX_WCB_COUNT` rows at ~135 chars each (WCB.ino:1719-1733), i.e. up to 2700 more. The two sections together pass 2912 with only ~4 reported rows on a 20-board mesh.

`buildETMCharResultsString()` (WCB.ino:1791) is worse: it emits a row per peer **per phase, three phases** (WCB.ino:1803-1817), each ~55-74 chars. At 13 online peers that is 39 rows ≈ 2600 chars plus ~400 of headers/summary — already over. At 19 peers it is ~4000 chars.

So `?MGMT,STATS,<n>` and `?MGMT,ETM,<n>` stop working entirely once the mesh is large, with no error anywhere unless debugMGMT is on (default false, WCB.ino:256). This is precisely the "cap that breaks at 20 boards" class the repo has been bitten by before — the cap here just happens to be expressed in chunks rather than in board count.

**Failure scenario.** A 20-board installation where several nodes push `?STATS,RPT` (parsed at WCB.ino:1751). Operator clicks stats-refresh for WCB7 through the relay. WCB7 builds ~3400 chars, sendResultFrags computes 19 chunks > 16, returns, prints nothing. The relay's statsRelaySession never starts, no `[MGMT:STATS,7]` line is ever emitted, and (per the separate finding on checkConfigPullTimeout) nothing ever reaps the state either. Same for a relay-triggered ETM characterization on 13+ online peers: the 30-60 s run completes, prints locally, and the relay-side result is discarded at WCB.ino:3099.

**Suggested fix.** Fragment across multiple sessions instead of refusing (send session 1 of N, or a continuation flag), or at minimum emit a one-chunk `TOOBIG` frag with the actual/allowed lengths so the requester can report the cause. Also make the refusal log unconditional rather than debugMGMT-gated.

### F-266 · S2 · `Code/WCB/WCB.ino:3245`

**etmCharRelayRequesterWCB latches when startETMChar()/processETMChar() bail — a later LOCAL ?ETM,CHAR frag-sends its results to a board that never asked**

*Category:* `logic`

`handleETMReqPacket` commits the relay requester before it knows the run will actually start:

```c
  etmCharRelayRequesterWCB = pkt.requesterWCB;
  startETMChar();
```

There are three paths that leave the run un-started or un-finished, none of which clears the global:
- `startETMChar()` returns immediately if `!etmEnabled` (WCB.ino:1406-1409). Note the ETM/non-ETM size gate in espNowReceiveCallback only applies to the 249/252-byte message structs — an `ETM_REQ` is a 43-byte `espnow_struct_config_req` routed purely by size (WCB.ino:3631-3641), so a board with ETM **off** still reaches this line.
- `startETMChar()` returns if `Default_WCB_Quantity < 2` (WCB.ino:1410-1413).
- `processETMChar()` aborts with `etmCharRunning = false; return;` when there are no online peers (WCB.ino:1450-1454), without calling `printETMCharResults`.

The **only** place that clears it is `printETMCharResults` (WCB.ino:1855), inside the `if (etmCharRelayRequesterWCB != 0)` block at WCB.ino:1851. So the stale requester id survives indefinitely — across every subsequent command, until the next successful characterization consumes it.

This is the same "per-command flag not reset at its source" shape that has already produced a real mesh bug in this codebase.

**Failure scenario.** Relay WCB2 issues `?MGMT,ETM,3`. WCB3 has ETM disabled (or WCBQ=1, or no peers online yet after boot). `etmCharRelayRequesterWCB` is set to 2; startETMChar prints "ETM is not enabled" to WCB3's own console and returns. WCB2 gets no response and no error — it just waits forever (etmRelaySession is never even created). Later the operator fixes ETM on WCB3 and runs `?ETM,CHAR` from WCB3's own USB console (WCB.ino:4704 / :5311). printETMCharResults sees the latched 2, and blasts the whole multi-KB characterization over ESP-NOW as ETM_FRAGs addressed to WCB2, which prints an unsolicited `[MGMT:ETM,3]…` line into whatever the operator is doing on that relay.

**Suggested fix.** Set `etmCharRelayRequesterWCB` only after confirming the run started, e.g. `startETMChar(); if (!etmCharRunning) return; etmCharRelayRequesterWCB = pkt.requesterWCB;` — and clear it in the `processETMChar()` no-peers abort path (WCB.ino:1450-1454) too. Ideally also send a one-chunk ETM_FRAG carrying the failure reason so the relay reports "ETM disabled on WCB3" instead of hanging.

### F-267 · S2 · `Code/WCB/WCB.ino:4130`

**Serial3/4/5 (ESPSoftwareSerial) are written from the WiFi callback and three other tasks with no mutual exclusion; the adjacent "write() drains async" comment is false for these ports**

*Category:* `concurrency`

The raw-serial delivery path in espNowReceiveCallback writes up to 177 bytes to an arbitrary port on the WiFi task:
    WCB.ino:4129  Stream &targetSerial = getSerialStream(targetPort);   // targetPort 1..5
    WCB.ino:4130  targetSerial.write((uint8_t *)(received.structCommand + 3), chunkLen);
    WCB.ino:4131-4132 comment: "No flush() - same reason as the targeted-Kyber path above: flushing here blocks the WiFi task (ESP-NOW) until the UART drains. write() drains async."

That comment is factually wrong for ports 3-5. `getSerialStream()` (WCB.ino:1869-1877) returns the SoftwareSerial objects declared at WCB.ino:885-887. In EspSoftwareSerial 8.1.0, TX has no buffer at all: `UARTBase::write(const uint8_t*, size_t, Parity)` (SoftwareSerial.cpp:348) loops over every byte calling `writePeriod()` (SoftwareSerial.cpp:294), which busy-waits one bit period at a time via preciseDelay(). There is nothing to drain asynchronously - the call returns only after the last stop bit. Serial3/4/5 default to 9600 baud (WCB.ino:624), so 177 bytes = ~184 ms of the WiFi task spent bit-banging. The same applies to the targeted-Kyber write at WCB.ino:4065 (up to 196 bytes) and the Kyber-broadcast writes at :4097/:4101/:4107.

Second, independent problem: these SoftwareSerial objects have no locking and are written from four contexts with nothing arbitrating them -
    WiFi task (Core 0):        WCB.ino:4065, 4097, 4101, 4107, 4130
    loop() (Core 1):           WCB.ino:5908 (`;S<n>` unicast), 6343 (broadcast fan-out)
    RawSerialForwardingTask:   WCB.ino:6679
    KyberLocalTask:            WCB.ino:4269, 4285, 4318, 4338
Two concurrent writers interleave at BIT granularity (not byte), and both mutate the same `m_periodStart` / `m_periodDuration` members and call `rxBits()`. Nothing in the port-selection logic excludes the overlap: processBroadcastCommand skips Maestro/MP3/DFP/HCR ports (WCB.ino:6302-6341) but not a port that is a raw-mapping output or an ESP-NOW raw-serial target.

**Failure scenario.** Board B has a WLED strip on Serial3 and board A has a raw serial mapping S2 -> W(B)S3. A user broadcasts a command: loop() on B is bit-banging the text out Serial3 at :6343 when a raw chunk from A arrives and B's WiFi task starts bit-banging the same Serial3 at :4130. The two writers interleave mid-byte, so the device on S3 receives corrupted framing (wrong bytes on the wire, not merely reordered ones), and B's WiFi task additionally stalls up to 184 ms per chunk at 9600 baud, dropping inbound ESP-NOW frames and delaying its ETM ACKs.

**Suggested fix.** (a) Correct the comments at WCB.ino:4066-4070 and :4131-4132 - EspSoftwareSerial TX is synchronous, so 'no flush' buys nothing on S3-S5. (b) Get the bit-banging off the WiFi task: enqueue the (port, bytes) tuple and write it from RawSerialForwardingTask/loop, mirroring drainOtaPackets/drainWdpPackets. (c) Guard each SoftwareSerial with its own mutex (or funnel all writes for a given port through one owning task) so loop(), RawSerialForwardingTask, KyberLocalTask and the callback can never bit-bang the same port simultaneously.

### F-268 · S2 · `Code/WCB/WCB.ino:4159`

**The WiFi task rewrites lastReceivedViaESPNOW mid-dispatch, so loop()'s per-item snapshot restore does not survive to the point it is read**

*Category:* `race`

`lastReceivedViaESPNOW` (WCB.ino:165) is a plain bool written from three contexts and read late in a long dispatch.

Written on the WiFi task (Core 0) from espNowReceiveCallback:
  WCB.ino:3883  lastReceivedViaESPNOW = !wizardOrigin;   (ETM COMMAND path)
  WCB.ino:4159  lastReceivedViaESPNOW = true;            (normal receive path)
  WCB.ino:2725  lastReceivedViaESPNOW = unicastOrigin;   (handleMgmtPacket, also inline in the callback)
Written on serialCommandTask: WCB.ino:6404. Written on loop(): WCB.ino:7510.

Critically, :4159 runs *before* the JSON-telemetry branch and that branch returns at WCB.ino:4204 without restoring - so every rc_hb / rc_ch frame from a controller leaves the global latched `true`. Same for the ETM JSON branch after :3883.

The queue-item snapshot (WCB.ino:1920, restored at :7510) only fixes the *queue-boundary* race the comment at :893-897 describes. It does not protect the window between the restore at :7510 and the actual read. The read that matters is WCB.ino:2275:
    if (target == 0 && lastReceivedViaESPNOW) return;   // sendESPNowMessage loop-prevention
and that read happens at the very END of processBroadcastCommand: the function first writes the command out all five local serial ports (WCB.ino:6343, byte-by-byte via writeSerialString at :1862-1867, bit-banged on S3-S5 at 9600 baud) and only then calls sendESPNowMessage(0, ...) at WCB.ino:6356. The exposed window is therefore tens of milliseconds of local serial fan-out, not microseconds.

The same unprotected window applies to the other two consumers, WCB.ino:5829 (routeStoredOrCap one-hop cap) and WCB.ino:6119 (recallStoredCommand mesh fan-out gate).

**Failure scenario.** Mesh contains a controller (NaviCore/RC) broadcasting rc_ch JSON at up to 5 Hz, i.e. the WiFi task executes WCB.ino:4159 every ~200 ms and leaves the flag true. Operator types a broadcast command (e.g. `open`) on USB. loop() drains it, restores espnowOrigin=false at :7510, runs processBroadcastCommand, spends ~20 ms writing the text out Serial3/4/5 at 9600 baud - a JSON frame lands in that window and sets the flag true - then reaches :2275, sees true, and returns without transmitting. The command executes on the local ports but is SILENTLY never broadcast to the mesh. The inverse also occurs: serialCommandTask clears the flag at :6404 while loop() is dispatching a mesh-received broadcast, defeating loop prevention and re-broadcasting a command that already circulated.

**Suggested fix.** Stop having the executor read the global. Thread the origin through the call chain instead: pass `bool espnowOrigin` from handleSingleCommand -> processBroadcastCommand -> sendESPNowMessage (an extra parameter defaulting to the global for legacy callers), so the loop-prevention decision uses the same value the queue item carried. If that is too invasive for this release, at minimum snapshot it into a local at the top of processBroadcastCommand (`const bool fromMesh = lastReceivedViaESPNOW;`) and pass that local to sendESPNowMessage, and do the same at :5829 and :6119.

### F-269 · S2 · `Code/WCB/WCB.ino:4269`

**forwardDataFromKyber writes each Kyber byte once PER TARGET, so two Maestros sharing one serial port receive every byte twice**

*Category:* `logic`

In the targeted branch the inner loop is:

```
4263        for (int i = 0; i < MAX_KYBER_TARGETS; i++) {
4264          if (!kyberTargets[i].enabled) continue;
4265          if (kyberTargets[i].targetWCB == WCB_Number) {
4268            if (kyberTargets[i].targetPort >= 1 && kyberTargets[i].targetPort <= 5)
4269              getSerialStream(kyberTargets[i].targetPort).write(b);
```

There is no dedup on the resolved destination port — the byte is written once for every enabled target whose `targetPort` matches. The remote side IS deduped (`addedToRemote`, 4262/4274-4277); the local side is not.

Two enabled targets can legitimately share one port. `KyberTarget` (Code/WCB/WCB_Storage.h:97-102) keys on `maestroID`, and both target-creation paths produce one entry per Maestro ID with `targetPort = maestroConfigs[i].serialPort`:
- `storeKyberSettings()` auto-populate, WCB_Storage.cpp:1123-1135
- `reconcileKyberTargetsFromMaestroConfigs()`, WCB_Storage.cpp:1049-1067

and `configureMaestro()` (WCB_Maestro.cpp:620-628) keys slots on `(maestroID, serialPort, remoteWCB)`, so `?MAESTRO,M1:W1S1:115200,M2:W1S1:115200` — two Pololu-protocol Maestros daisy-chained on S1, the standard Pololu multi-device wiring — creates two slots, hence two kyberTargets with `targetPort == 1`.

The non-targeting branch has the identical defect at line 4285: it iterates `maestroConfigs[]` and writes `b` for every configured local slot, so N slots on one port emit N copies.

The repo already documents the required rule for the `;M` path — WCB_Maestro.cpp:95-107: "Dedup by RESOLVED physical destination so overlapping config slots can't double-fire one device … local -> dedup by serial port (same id + same port = same device on the wire)" — implemented there with the `sentLocalPorts` bitmask (WCB_Maestro.cpp:105, 113-116). The raw Kyber passthrough path never got that treatment.

(The ESP-NOW receive side has the same shape at WCB.ino:4096-4098 and 4105-4108, outside my range.)

**Failure scenario.** WCB1 has Kyber local on S2 and two Maestros daisy-chained on S1 (`?MAESTRO,M1:W1S1:115200` and `?MAESTRO,M2:W1S1:115200`, then `?KYBER,LOCAL,S2` which auto-populates both as targets). Every byte the Kyber emits is written to S1 twice. A Pololu-protocol frame 0xAA 0x0C 0x04 ... arrives at the Maestro as 0xAA 0xAA 0x0C 0x0C 0x04 0x04 — the device sees a stream of malformed/desynchronised frames and the passthrough is completely non-functional. Single-Maestro-per-port setups are unaffected, which is why this survives on the bench.

**Suggested fix.** Mirror WCB_Maestro.cpp:105/113-116: keep a `uint8_t sentLocalPorts = 0;` bitmask per byte (or better, per drain pass) and skip a write when the port bit is already set. Apply the same to the non-targeting branch at 4283-4287 (dedup on `maestroConfigs[i].serialPort`).

### F-270 · S2 · `Code/WCB/WCB.ino:4342`

**forwardMaestroDataToRemoteKyber reads and DISCARDS Maestro bytes past 64 — the exact drop the sibling function was fixed to stop doing**

*Category:* `logic`

```
4339      while (maestroSerial.available() > 0) {
4340        if (maestroConfigs[i].serialPort == maestroQueryPort) break;
4341        uint8_t b = (uint8_t)maestroSerial.read();
4342        if (remoteLen < (int)sizeof(remoteBuf)) remoteBuf[remoteLen++] = b;
4343      }
```

Once `remoteLen` hits 64 the loop keeps calling `read()` — the byte leaves the UART FIFO and is thrown away. There is no flush-and-continue and no error signal.

This is the identical defect that was explicitly fixed in the sibling forwarder. WCB.ino:4271-4275 carries the fix and the reason:

```
4271          // Remote — accumulate once; broadcast reaches all remote boards. If the
4272          // buffer is full, FLUSH and keep going — don't silently drop bytes past
4273          // 64, which truncated a large Maestro burst into a corrupt frame.
4275            if (remoteLen >= (int)sizeof(remoteBuf)) { sendESPNowRaw(remoteBuf, remoteLen); remoteLen = 0; }
```

`git log -L4270,4292` confirms commit a32e9cb changed `if (!addedToRemote && remoteLen < (int)sizeof(remoteBuf))` to the flush form in `forwardDataFromKyber` and in the non-targeting branch (4289) only. `forwardMaestroDataToRemoteKyber` (4342) and `forwardMaestroDataToLocalKyber` (4323) kept the old dropping form.

Made worse by the buffer being shared across ALL local Maestro ports in one call (`remoteLen` is declared once at 4332, the port loop runs 4335-4344): whichever port drains first can consume the whole 64 bytes, and every byte from the remaining ports in that pass is read-and-dropped.

The caller is `KyberRemoteTask` (WCB.ino:6587-6592) with `vTaskDelay(pdMS_TO_TICKS(1))`, pinned to core 1 at priority 1 alongside `serialCommandTask` and `RawSerialForwardingTask` (WCB.ino:7439/7447/7464), so poll gaps well over 1 ms are routine.

**Failure scenario.** Board configured `?MAESTRO,REMOTE` (Maestro_Remote, WCB_Storage.cpp:1178-1182) hosting a local Maestro on S1 at 115200. The remote Kyber/NaviCore issues a burst of queries; the Maestro replies while KyberRemoteTask is preempted for ~8 ms by the WiFi task and serialCommandTask. ~92 bytes have accumulated in the 256-byte RX FIFO. The drain copies the first 64 into remoteBuf, reads and silently destroys the remaining ~28, and broadcasts a truncated frame. The remote Kyber sees a half command with no gap indication — the same 'truncated a large Maestro burst into a corrupt frame' failure recorded at line 4272-4273.

**Suggested fix.** Apply the same flush-and-continue used at 4275/4289: `if (remoteLen >= (int)sizeof(remoteBuf)) { sendESPNowRaw(remoteBuf, remoteLen); remoteLen = 0; } remoteBuf[remoteLen++] = b;` — at line 4342 and at 4323.

### F-271 · S2 · `Code/WCB/WCB.ino:5138`

**?LED,PIN re-inits the NeoPixel object while the WiFi task dereferences it, leaking the old object and opening a use-after-free**

*Category:* `concurrency`

The `?LED,PIN,<n>` handler at WCB.ino:5128-5143 performs a LIVE re-init from the command task: `saveStatusLEDPin(pin); STATUS_LED_PIN = pin; … initStatusLEDWithRetry(5, 100); turnOffLED();`.

`initStatusLEDWithRetry` (WCB.ino:6725-6765) mutates the global `Adafruit_NeoPixel* statusLED` (declared WCB.ino:658) with no synchronisation:
  * :6740 `statusLED = new Adafruit_NeoPixel(...)` — overwrites the existing pointer **without deleting it**. On the first `?LED,PIN` after boot, `statusLED` is already non-null (set in `setup()` at :7174), so the previous object and its pixel buffer are leaked. Every subsequent `?LED,PIN` leaks another.
  * :6754 `delete statusLED; statusLED = nullptr;` on a failed attempt.

That same pointer is dereferenced from the **WiFi task**: `colorWipeStatus()` (WCB.ino:660-679) does `statusLED->setBrightness()` / `->setPixelColor()` / `->show()` at :668-671 after a null check at :663, and it is called from inside `espNowReceiveCallback` (function body WCB.ino:3584-4230) at :3848, :3860, :3870, :3885, :3932, :3984, :4000, :4161, :4165, :4208, :4229 — i.e. on essentially every received ETM/normal packet.

There is no mutex, no critical section, and no "LED busy" flag. The null check at :663 is a TOCTOU: the WiFi task can pass it and then be preempted while the command task runs :6754. `initStatusLEDWithRetry` also publishes the pointer at :6740 and only calls `begin()` 50 ms later at :6742, so for that whole window the WiFi task can call `setBrightness`/`show()` on an object whose RMT/driver state has not been initialised.

**Failure scenario.** A board is on a live mesh (ETM heartbeats every ~10 s from each of N peers, plus command traffic — every one of those runs `colorWipeStatus` on the WiFi task). The operator sends `?LED,PIN,48`, or the Wizard pushes it — per docs/CODE_REVIEW_2026-08_wave3.md:615-623 a stock HW-3.2 board re-emits `LED,PIN,48` on every pull/push cycle, so this fires repeatedly. Guaranteed outcome: one `Adafruit_NeoPixel` object plus its pixel buffer is leaked per invocation, and the freed/replaced RMT resource is never released. Worse outcome: a packet arrives while `initStatusLEDWithRetry` is between :6740 (`new`, pointer published) and :6742 (`begin()`), or during the :6754 `delete`, and the WiFi task calls `show()` on a half-constructed or freed object — heap corruption / LoadProhibited panic inside the ESP-NOW receive callback.

**Suggested fix.** Make the swap atomic from the LED consumers' point of view: in the `?LED,PIN` handler, `Adafruit_NeoPixel* old = statusLED; statusLED = nullptr;` (publish null first so `colorWipeStatus`'s guard short-circuits), then `delete old;` before calling `initStatusLEDWithRetry`. Also add the missing `if (statusLED) { delete statusLED; statusLED = nullptr; }` before the `new` at WCB.ino:6740. Longer term, guard `statusLED` with a portMUX or move all LED writes off the WiFi task into a queue, the way `rtermRelayHandlePacket` already does for serial output (WCB_RemoteTerm.h:106-116).

### F-272 · S2 · `Code/WCB/WCB.ino:5279`

**?SLSx,label is completely dead — the dispatcher strips "SLS" and updateSerialLabel() strips it again**

*Category:* `logic`

WCB.ino:5278-5279 dispatches the legacy label command as `updateSerialLabel(message.substring(3))` — i.e. it removes the "SLS" prefix before the call. But `updateSerialLabel()` (WCB.ino:5467-5504) expects the FULL token: it tests `if (!message.startsWith("S") && !message.startsWith("s"))` at :5475 and extracts the port with `message.substring(3, commaPos)  // Skip \"SLS\"` at :5487.

So for `?SLS1,Marcduino` the function receives `"1,Marcduino"`, fails the `startsWith("S")` test at :5475, prints "Invalid format. Use: ?SLSx,label where x=1-5" and returns. There is no input that reaches the save: even `?SLSS1,x` (which passes :5475 as `"S1,x"`) then computes `substring(3, commaPos=2)`, which Arduino's `String::substring` swaps to `(2,3)` = `","` → `toInt()` = 0 → rejected at :5490.

This is the identical double-strip defect that the adjacent lines record as already-fixed for the sibling commands — WCB.ino:5283-5287 carries the comment `// pass FULL \"SBISxON\" — the fn strips \"SBI\" itself  (was double-stripped here → whole command was dead)` on both `?SBI` and `?SBO`. `?SLS` was missed in that fix. Note the neighbouring `?SLC` path at :5280-5281 uses a completely different inline parse (`clearSerialLabel(message.substring(4).toInt())`) and does work.

The command is user-facing: `Code/WCB/WCB_Help.cpp:159` prints `"  ?SLSx,label   - Set label  (e.g. ?SLS1,Marcduino)"` under "Legacy commands".

**Failure scenario.** A user follows `? LABEL` help and types `?SLS1,Marcduino`. The board prints "Invalid format. Use: ?SLSx,label where x=1-5" and no label is stored — the command has never worked in this build for any spelling. The same happens for any older tool or saved config chain that emits the legacy `?SLSx,` form (the current `?LABEL,Sx,text` handler at WCB.ino:4587-4595 is unaffected).

**Suggested fix.** Pass the full token, matching the `?SBI`/`?SBO` fix: `updateSerialLabel(message);` at WCB.ino:5279. `updateSerialLabel` already strips "SLS" itself at :5487 — but also fix its own :5475 guard, which checks `startsWith("S")` on the full token instead of on `message.substring(2)`, so it accidentally passes only because the token starts with 'S'.

### F-273 · S2 · `Code/WCB/WCB.ino:5703`

**updateESPNowPassword() strips 6 chars off a 5-char "EPASS" prefix — the legacy ?EPASS form silently drops the first password character**

*Category:* `logic`

`updateESPNowPassword()` (WCB.ino:5702-5710) does `String newPassword = message.substring(6);`. The legacy dispatcher reaches it at WCB.ino:5214-5215 via `message.startsWith("epass") || message.startsWith("EPASS")`, passing the FULL message. "EPASS" is five characters (indices 0-4), so the password begins at index 5 and `substring(6)` discards its first character.

The comma form never lands here: `processLocalCommand` splits on the first comma at WCB.ino:4410-4414, so `?EPASS,pw` matches `rootUpper == "EPASS"` at WCB.ino:4750 and is handled correctly at :4756 with `setESPNowPassword(args.c_str())`. The function at 5702 is therefore only ever reached with the comma-less form — the exact form the help advertises: `Code/WCB/WCB_Help.cpp:356` prints `"  ?EPASSpassword   (e.g. ?EPASSmy_password)"`.

The consequence is not cosmetic. `espnowPassword` gates every inbound ESP-NOW packet — `if (String(pkt.structPassword) != String(espnowPassword)) return;` at WCB.ino:2643, :2973, :3046 (and it is stamped into every outbound packet at :931, :950, :2285, :2333 …). `setESPNowPassword` (WCB_Storage.cpp) writes it to the `espnow_config` NVS namespace, so the wrong value survives reboot.

Note the length guard at 5704 also mis-reports: it validates the already-truncated string, so a 1-character password (`?EPASSa`) yields an empty string and is rejected with "Invalid ESP-NOW password length" even though the user supplied a valid one.

**Failure scenario.** Operator follows the documented legacy syntax and types `?EPASSmy_password` on one board (or has it in a saved paste-buffer / older config chain). `updateESPNowPassword` stores `y_password`. The board immediately stops matching every peer's `structPassword` (WCB.ino:2643) and every peer drops its packets, so it goes deaf and mute on the mesh — no ETM, no WDP discovery, no relayed terminal — and the value is persisted in NVS so a reboot does not recover it. Diagnosis is misleading because `?CONFIG` (WCB.ino:2169) prints `y_password`, which looks like a plausible password the operator does not recognise.

**Suggested fix.** Change WCB.ino:5703 to `String newPassword = message.substring(5);` and, to keep the historical `?epass,<pw>` spelling working, strip one leading comma: `if (newPassword.startsWith(",")) newPassword = newPassword.substring(1);`.

### F-274 · S2 · `Code/WCB/WCB.ino:5772`

**printBackupConfig emits the live-chain checksum as <funcChar>CHK, but both verifiers hard-code "?CHK" — restore integrity checking is silently skipped on any board with a custom FUNCCHAR**

*Category:* `logic`

`printBackupConfig()` builds the "For Configured Boards" chain with the board's configured function identifier: `String lfi = String(LocalFunctionIdentifier);` at WCB.ino:5733, every token is emitted as `lfi + c` at :5747, and the trailing checksum token is `String checksumCmd = lfi + "CHK" + String(crcStrBuf);` at :5772. The factory chain deliberately uses the fixed form (`String checksumCmdDefault = "?CHK" + ...` at :5776, comment: "factory reset always uses ?").

Both places that VERIFY a restore chain match on a hard-coded literal, not on `LocalFunctionIdentifier`:
  * `parseCommandsAndEnqueue` — WCB.ino:1953 `int chkPos = data.lastIndexOf(String(commandDelimiter) + "?CHK");` (and :1955 for the lowercase variant).
  * `handleSerialData`'s `?C…` path — WCB.ino:6440 `String chkPattern = String(commandDelimiter) + "?CHK";`.

And there is no `?CHK` verb in the dispatcher at all: `grep 'rootUpper == "CHK"'` over WCB.ino returns nothing (only `etmCmdUpper == "CHKSM"` at :4705). `verifyBackupChecksum()` at :5792, which was clearly written for it (`message.substring(3)` skips "CHK"), has no callers.

So on a board where `?FUNCCHAR,#` has been applied, the live backup chain ends in `^#CHK1A2B3C4D`. `handleSerialData` routes the paste to `parseCommandsAndEnqueue` at :6495; :1953 searches for `^?CHK`, finds nothing, `chkPos == -1`, and the whole `if (chkPos != -1)` verification block at :1958-1997 is skipped. The chain is applied with no integrity check, and the `#CHK…` token is dispatched as a command, falls through every branch in `processLocalCommand`, and prints "Unknown command: CHK1A2B3C4D" at :5346.

**Failure scenario.** A board has `?FUNCCHAR,#` set (a supported, documented setting — WCB_Help.cpp:886-898, and `collectConfigCommands` emits it at WCB.ino:2800). The operator runs `?BACKUP`, copies the "For Configured Boards" chain, and later pastes it back over a serial link that drops or mangles bytes mid-paste. On a default-funcChar board the CRC would catch this and abort with "✗ Command checksum FAILED - Aborting restore to prevent corruption" (:1992-1996). Here it does not: no "✓ Command checksum VERIFIED" line is ever printed, the truncated chain is applied to NVS, and the only symptom is a stray "Unknown command: CHK…". The board is left half-configured with no indication that anything was wrong.

**Suggested fix.** Make the verifiers funcChar-aware rather than the writer '?'-only: at WCB.ino:1953 and :6440 build the pattern as `String(commandDelimiter) + String(LocalFunctionIdentifier) + "CHK"`, keeping the existing literal `"?CHK"` as a second fallback so factory chains and older backups still verify. Then wire up `verifyBackupChecksum()` (or delete it) so a CHK token that does slip through the splitter is not reported as an unknown command.

### F-275 · S2 · `Code/WCB/WCB.ino:6603`

**serialCommandTask hard-codes the Kyber/Maestro port skip to S1/S2 while the Kyber and Maestro ports are configurable S1-S5, so both tasks drain the same UART**

*Category:* `concurrency`

`serialCommandTask` decides which ports to hand to `processIncomingSerial` from the Kyber flags but with the port numbers hard-coded:
```
WCB.ino:6603-6605  if (!isSerialPortRawMapped(3)) processIncomingSerial(Serial3, 3);
                   if (!isSerialPortRawMapped(4)) processIncomingSerial(Serial4, 4);
                   if (!isSerialPortRawMapped(5)) processIncomingSerial(Serial5, 5);
WCB.ino:6608-6609  if (Kyber_Local) { /* Skip Serial1 and Serial2 - handled by Kyber task */ }
WCB.ino:6610-6614  else if (Maestro_Remote) { ...processIncomingSerial(Serial2, 2)... }   // only S1 skipped
```
But the ports the Kyber tasks actually read are runtime config, not S1/S2:
  - `forwardDataFromKyber()` reads `getSerialStream(kyberLocalPort)` (WCB.ino:4254), and `kyberLocalPort` is user-set to ANY of S1-S5: `?KYBER,LOCAL,Sx` accepts 1-5 (WCB_Storage.cpp:1112 `if (baseCommand.equals("local") && kyberUseTargeting && (kyberPort < 1 || kyberPort > 5))`) and stores it at WCB_Storage.cpp:1159 `kyberLocalPort = kyberPort;`.
  - `forwardMaestroDataToLocalKyber()` (4315-4318) and `forwardMaestroDataToRemoteKyber()` (4335-4338) iterate EVERY configured local Maestro and read `getSerialStream(maestroConfigs[i].serialPort)` - again any of S1-S5.

So with Kyber local on S3 (or a Maestro on S2/S3/S4/S5 under Maestro_Remote), `serialCommandTask` and `KyberLocalTask`/`KyberRemoteTask` both call `.read()` on the same Stream, interleaved by the FreeRTOS scheduler (both priority 1, both pinned to core 1 - WCB.ino:7439/7443/7447).

The sibling task 50 lines below has exactly the guard that is missing here, with a comment stating the consequence:
```
WCB.ino:6650-6655  // If this board has a local Kyber, skip that specific port - KyberLocalTask
                   // owns it.  Reading here would steal bytes from the Kyber forwarding path
                   // and double-send them.
                   if (Kyber_Local && kyberLocalPort > 0 && inputPort == kyberLocalPort) continue;
```
and `serialCommandTask`'s own raw-mapping guard makes the same argument (6600-6602). The Kyber guard was simply never generalized past S1/S2.

**Failure scenario.** User runs `?KYBER,LOCAL,S3` (accepted; `applyLiveBaud` even re-begins Serial3 at 115200 - WCB.ino:1889-1893). After reboot, KyberLocalTask calls `forwardDataFromKyber()` on Serial3 every 1 ms while serialCommandTask calls `processIncomingSerial(Serial3, 3)` every 5 ms. Each task consumes whatever bytes it happens to see first: the Kyber's binary Pololu frames are split between the two, so the Maestro receives truncated/garbled frames (servos jitter or stop), and the stolen bytes are appended to `serialBuffers[3]` and eventually parsed as a bogus text command. Same failure for `Maestro_Remote` with a Maestro configured on S2: serialCommandTask processes Serial2 (6612-6613) while KyberRemoteTask drains it, corrupting the ESP-NOW raw-Maestro stream.

**Suggested fix.** Replace the S1/S2 hard-coding with the same predicate RawSerialForwardingTask uses. For each port p in 1..5, skip `processIncomingSerial` when `(Kyber_Local && p == kyberLocalPort)` or when `(Kyber_Local || Maestro_Remote) && isSerialPortUsedForMaestro(p)` (a local Maestro slot with `remoteWCB == 0` on that port), rather than testing `p == 1 || p == 2`.

### F-276 · S2 · `Code/WCB/WCB.ino:6608`

**Kyber_Local / Maestro_Remote are settable at runtime but KyberLocalTask / KyberRemoteTask are only created at boot, leaving S1 and S2 with no reader at all**

*Category:* `logic`

`serialCommandTask` gates on the LIVE globals:
```
WCB.ino:6608-6609  if (Kyber_Local) {
                       // Skip Serial1 and Serial2 - handled by Kyber task
                   } else if (Maestro_Remote) { ... }
```
but the compensating tasks are created once, in setup(), from the boot-time value of those same globals:
```
WCB.ino:7442-7449  if (Kyber_Local)    { xTaskCreatePinnedToCore(KyberLocalTask,  ...); }
                   if (Maestro_Remote) { xTaskCreatePinnedToCore(KyberRemoteTask, ...); }
```
`grep -n "PWMTask\|KyberLocalTask\|KyberRemoteTask"` over the whole sketch shows no other `xTaskCreate*` for either - there is no runtime creation path.

`storeKyberSettings()` sets both flags live with no reboot and no warning:
```
WCB_Storage.cpp:1156-1157  Kyber_Local = true;  Maestro_Remote = false;   // ?KYBER,LOCAL,Sx
WCB_Storage.cpp:1179-1180  Kyber_Local = false; Maestro_Remote = true;    // ?KYBER,REMOTE / ?MAESTRO,REMOTE
```
Neither branch prints a reboot instruction (contrast `addPWMMapping`, WCB_PWM.cpp:339-342, which explicitly `ESP.restart()`s to apply, and WCB_WDP.cpp:482 which at least prints "reboot to fully apply" for the WDP auto-path). So the moment the command lands, serialCommandTask stops servicing S1/S2 while no Kyber task exists to service them either.

**Failure scenario.** A board booted with Kyber cleared (`Kyber_Local == false`) is reconfigured over USB with `?KYBER,LOCAL,S2`. `Kyber_Local` becomes true immediately. From the next 5 ms tick, `serialCommandTask` takes the `if (Kyber_Local)` branch and never calls `processIncomingSerial` on Serial1 or Serial2 again, and `KyberLocalTask` was never created, so `forwardMaestroDataToLocalKyber()` / `forwardDataFromKyber()` never run. Both hardware UARTs go completely unread: every command sent into S1/S2 is silently dropped until the RX ring overflows, and no Kyber/Maestro forwarding happens. The board looks alive on USB and in the mesh, so this presents as "S1 and S2 just died". Only a reboot recovers it. Same for `?MAESTRO,REMOTE` (S1 goes dark).

**Suggested fix.** Create both tasks unconditionally at boot and have their bodies early-return when the corresponding flag is false (they already `vTaskDelay(1)` each pass, so the idle cost is negligible), or create the task on demand inside `storeKyberSettings()` when the flag transitions false->true. At minimum, print an explicit "reboot required" and refuse to change the flag live, so S1/S2 are never left unowned.


---

## S3

### F-277 · S3 · `Code/WCB/WCB.ino:2496`

**sendESPNowRawSerial clamps chunks at 180 bytes but the receiver rejects anything over 177**

*Category:* `protocol`

`sendESPNowRawSerial` fragments its input at 180 bytes:

```c
size_t chunkSize = len - offset;
if (chunkSize > 180) chunkSize = 180;   // :2496
...
msg.structCommand[0] = targetPort;                       // :2510
msg.structCommand[1] = (uint8_t)(chunkSize & 0xFF);      // :2512
msg.structCommand[2] = (uint8_t)((chunkSize >> 8) & 0xFF);
memcpy(msg.structCommand + 3, data + offset, chunkSize); // :2515
```

The matching receiver guard is 177, not 180:
```c
if (chunkLen > 177 || chunkLen == 0 || targetPort < 1 || targetPort > 5) { ... return; }  // :4123
```
(3 header bytes + 177 = 180, so 177 is the value that keeps the header+data inside the sender's own 180-byte budget — the receiver is the side that got it right; `structCommand` is 200 bytes so 197 would also be safe.) A packet carrying 178, 179 or 180 payload bytes is therefore built and transmitted successfully by the sender and silently discarded by the receiver — the rejection log at `:4124` is gated on `debugMaestro`.

This does not fire today only because the sole caller passes a 64-byte buffer: `RawSerialForwardingTask` declares `static uint8_t buffer[64]` (`WCB.ino:6637`) and reads `while (... && len < sizeof(buffer))` (`:6665`), so `len <= 64`. Nothing in `sendESPNowRawSerial` documents or enforces that dependency, and the comment at `:4038-4043` on the sibling Kyber path explicitly enumerates the per-path limits as "180 / 177" — i.e. the 177 was chosen deliberately and the sender was not updated to match.

**Failure scenario.** Someone raises `RawSerialForwardingTask`'s buffer from 64 to 256 to cut per-byte ESP-NOW overhead on a busy raw-serial mapping (a natural throughput tune — the function already loops over `len` in 180-byte chunks as if that were the wire limit). Every full 180-byte chunk is then dropped at the target's `chunkLen > 177` guard while `sendESPNowRawSerial` reports `ESP_OK`; a mapped device on WCB2 receives only the final short chunk of each burst, and with `debugRawSerial` on the sender the bytes appear to have been sent. Nothing in either function names the 177 ceiling.

**Suggested fix.** Change the clamp at `WCB.ino:2496` to 177 to match the receiver's guard at `:4123`, and replace both magic numbers with a shared constant (e.g. `#define ESPNOW_RAW_SERIAL_MAX_CHUNK 177`) with a comment naming the 3-byte `[port][len_lo][len_hi]` header, mirroring the note already at `:4038-4043`.

### F-278 · S3 · `Code/WCB/WCB.ino:2702`

**MGMT session completion uses the packet's totalChunks instead of the session's, so a mismatched packet executes a partial command**

*Category:* `bounds`

`handleMgmtPacket` records the session's chunk count at session start:

```c
mgmtSession.totalChunks = pkt.totalChunks;   // :2682
```

but every subsequent decision uses the value from the *current packet* instead:

```c
if (pkt.chunkIdx >= MGMT_MAX_CHUNKS || pkt.chunkIdx >= pkt.totalChunks) return;   // :2646
uint16_t expectedMask = (uint16_t)((1 << pkt.totalChunks) - 1);                   // :2702
if (mgmtSession.receivedMask == expectedMask) { ... }                             // :2703
for (int i = 0; i < pkt.totalChunks; i++) fullCmd += String(mgmtSession.chunks[i]); // :2706
```

`mgmtSession.totalChunks` is never read anywhere — I grepped every `mgmtSession.` reference in the file (`:2673`-`:2739`) and `:2682` is the only occurrence of that field. So the one piece of state that would let the receiver detect a chunk-count disagreement within a session is written and discarded.

Consequence: two packets carrying the same `sessionId` but different `totalChunks` complete the session at whichever count is smaller. `handleMgmtForward` copies `chunkIdx` and `totalChunks` straight off the serial line (`:2579-2580`, `:2615-2616`) with no cross-check against anything, and the Wizard exposes a raw terminal that can emit `?MGMT,FRAG,...` directly, so the field is attacker/typo/corruption-reachable rather than internally guaranteed. `fullCmd` is then built from `chunks[0..pkt.totalChunks-1]`, which may include slots never written in this session (they are zeroed at `:2680`, so the result is a truncated rather than garbage command) and is executed via `parseCommandsAndEnqueue` at `:2731`.

**Failure scenario.** A 4-chunk config push to WCB3 is in flight; chunks 0 and 1 have arrived (receivedMask = 0b0011). A stray retransmission of an earlier command reuses the same 16-bit sessionId with `chunkIdx 1, totalChunks 2` (or the operator fat-fingers a `?MGMT,FRAG,3,<same id>,1,2,...` line into the relay terminal). The `chunkIdx < totalChunks` check at :2646 passes, the sessionId matches so the busy guard at :2674 is skipped, `expectedMask` becomes 0b0011 at :2702 — which already equals `receivedMask` — and WCB3 immediately executes the first two chunks of the config as a complete command, writing a chain cut mid-token to NVS. The remaining two chunks are then rejected by the ring dedup at :2659-2664 because the sessionId was just recorded as completed at :2712.

**Suggested fix.** Use the session's own value once the session exists, and reject packets that disagree with it: after the session-start block, add `if (pkt.totalChunks != mgmtSession.totalChunks) { if (debugMGMT) Serial.printf("[MGMT] session %04X totalChunks %d != %d — rejecting chunk\n", pkt.sessionId, pkt.totalChunks, mgmtSession.totalChunks); return; }`, then compute `expectedMask` and the reassembly loop from `mgmtSession.totalChunks`.

### F-279 · S3 · `Code/WCB/WCB.ino:2725`

**lastReceivedViaESPNOW / inSequenceBody are written from the WiFi task on a non-volatile global that Core 1 snapshots per-command**

*Category:* `concurrency`

On MGMT session completion `handleMgmtPacket` writes two shared globals:

```c
lastReceivedViaESPNOW = unicastOrigin;   // :2725
inSequenceBody = false;                  // :2726
```

`handleMgmtPacket` is called inline from `espNowReceiveCallback` (`WCB.ino:3628`), i.e. on the **WiFi task, Core 0** — it is not deferred like the CONFIG_REQ / SEQVAL_REQ / OTA packets right below it (`:3634`, `:3646`, `:3663`), which carry explicit "DEFER to loop() … so we don't stall the WiFi task" comments.

`lastReceivedViaESPNOW` is declared `bool lastReceivedViaESPNOW = false;` at `WCB.ino:165` — a plain, non-`volatile` bool. It is written from Core 1 by `processIncomingSerial` (`:6404`) and read a few calls later by `enqueueCommand` (`:1920`, `item.espnowOrigin = lastReceivedViaESPNOW;`), which runs on `serialCommandTask` (created pinned to core 1 at `:7439`). The `CommandQueueItem` comment at `:893-897` states the snapshot exists because "the global races across queue boundaries" — the snapshot closes the *queue-drain* race but not the *cross-core write* race, because the WiFi task can land its write inside the Core 1 window between `:6404` and `:1920`.

That this codebase treats such scalars as needing `volatile` is established at `WCB.ino:293-296`, where `rcJsonRelaySubscribedUntilMs` carries an explicit comment: "volatile: written on Core 1 … and read on Core 0 … Aligned 32-bit access is atomic on Xtensa, so volatile (no caching/hoist) is sufficient". `lastReceivedViaESPNOW` has the same cross-core access pattern and no such qualifier, so the compiler is also free to cache it across the calls between `:6404` and `:1920`.

The same unsynchronized write exists in the ETM branch (`:3883`) and the normal branch (`:4159`); `:2725` is the one in this range.

**Failure scenario.** During bench setup the operator has a Wizard config push running to this board (multi-chunk MGMT_FRAG_UNICAST from a WCB_Client host, so `unicastOrigin == true`) while typing `;M11` on the USB console. `processIncomingSerial` sets `lastReceivedViaESPNOW = false` (:6404) and calls into `parseCommandsAndEnqueue`; before `enqueueCommand` reads the flag (:1920), the WiFi task completes the MGMT session and executes `lastReceivedViaESPNOW = true` (:2725). The console command is queued with `espnowOrigin = true`, so at dispatch (:7510) its mesh fan-out is suppressed by the `target == 0 && lastReceivedViaESPNOW` gate at :2275 — the Maestro trigger runs locally and never reaches the other boards, once, with no error. This is the same latch class already fixed once at the command sources (:6404, :6407).

**Suggested fix.** Declare `lastReceivedViaESPNOW` and `inSequenceBody` `volatile` (matching the precedent and rationale at `WCB.ino:293-296`), and — better — stop writing them from the WiFi task at all: carry the origin as an explicit argument. `parseCommandsAndEnqueue` / `enqueueCommand` already thread `sourceID`; adding an `espnowOrigin` parameter (defaulting to the current global) lets `:2725`, `:3883` and `:4159` pass the value down instead of round-tripping it through a shared global.

### F-280 · S3 · `Code/WCB/WCB.ino:2738`

**mgmtSession and the config/stats/seq/etm relay session structs are memset from loop() while the WiFi callback is writing them**

*Category:* `race`

`MgmtSession mgmtSession` (WCB.ino:850, ~2.9 KB) is reassembled entirely on the WiFi task - handleMgmtPacket is called inline from espNowReceiveCallback at WCB.ino:3628, and writes `mgmtSession.active/sessionId/totalChunks/chunks[]/receivedMask/lastActivityMs` at WCB.ino:2681-2700.

The same struct is memset from loop():
    WCB.ino:2736-2742  checkMgmtTimeout()  -> memset(&mgmtSession, 0, sizeof(mgmtSession));
(called from loop() at WCB.ino:7492). There is no portMUX, mutex, or queue between the two contexts, and the tasks are on different cores.

The same pattern repeats for the five relay-reassembly structs: `pullSession`, `seqRelaySession`, `seqValRelaySession` are memset by checkConfigPullTimeout() at WCB.ino:3565/3573/3579 (loop, called at :7494) while handleConfigFragPacket / handleSeqFragPacket / handleSeqValFragPacket write them from the callback (WCB.ino:3651-3656 dispatch; writes at :3059-3077, :3344-3360, :3379-3395).

Because the callback's sequence is [check .active] -> [store chunk] -> [set receivedMask] -> [test complete], a memset landing between the store and the completeness test wipes the earlier chunk bits while leaving the just-stored chunk's bit set. The session then never reaches expectedMask, and the next chunk takes the `!active` branch and restarts the session from the middle, discarding the chunks already received.

(Note also: statsRelaySession and etmRelaySession are never reaped at all - checkConfigPullTimeout covers only three of the five, despite the comment at WCB.ino:3567-3569 explaining why the reap is needed.)

**Failure scenario.** A multi-chunk Wizard push-config to this board stalls (a couple of broadcast fragments lost on a busy mesh) until mgmtSession is older than MGMT_SESSION_TIMEOUT_MS. loop() enters checkMgmtTimeout and begins the 2.9 KB memset just as a retransmitted fragment arrives; the WiFi task has already passed the `!mgmtSession.active` check and stores its chunk into the struct being zeroed. receivedMask ends up holding only that one bit against a totalChunks of e.g. 6, so the session can never complete; the following fragment sees active==false and restarts from chunk N, permanently losing chunks 0..N-1. The push silently fails with no error on either side.

**Suggested fix.** Make the reassembly single-context like the ACK and OTA paths already are: have handleMgmtPacket (and the four frag handlers) enqueue the raw packet via enqueueMgmtReq()-style deferral and do all session mutation in loop(). If that is too large a change now, wrap every mgmtSession/pullSession/*RelaySession mutation - callback side and timeout side - in a shared portMUX critical section. Separately, add statsRelaySession and etmRelaySession to checkConfigPullTimeout() so all five sessions are reaped consistently.

### F-281 · S3 · `Code/WCB/WCB.ino:2740`

**checkMgmtTimeout() memsets the 2.9 KB mgmtSession from loop() while the WiFi task may be writing it**

*Category:* `race`

`checkMgmtTimeout()` runs on the loop task (called from `loop()` at `WCB.ino:7490`) and clears the whole session struct with no synchronization:

```c
if (mgmtSession.active &&
    (millis() - mgmtSession.lastActivityMs > MGMT_SESSION_TIMEOUT_MS)) {   // :2737-2738
  if (debugMGMT) Serial.printf(...);
  memset(&mgmtSession, 0, sizeof(mgmtSession));                            // :2740
}
```

`MgmtSession` is `char chunks[16][181]` plus scalars (`WCB.ino:841-850`) — a ~2.9 KB memset, not a single-word store. The same struct is mutated concurrently by `handleMgmtPacket`, which runs on the **WiFi task** (called inline at `:3628`): it tests `mgmtSession.active` / `sessionId` (`:2673-2675`), does its own `memset(&mgmtSession, 0, sizeof(mgmtSession))` (`:2680`), writes `sessionId`/`totalChunks`/`unicastOrigin`/`lastActivityMs`/`active` (`:2681-2685`), copies a 180-byte chunk (`:2693`) and ORs `receivedMask` (`:2695`). There is no portMUX, queue, or critical section on either side — contrast the ETM ACK path, which was explicitly restructured for exactly this reason (`WCB.ino:1099-1113`: "Mutating the table from both, unsynchronized, corrupts it… So the callback ENQUEUES the ACK here… all table access stays on the loop task", guarded by `etmAckQMux`).

The interleaving that matters is not the benign one (a chunk lost to a timeout that was going to fire anyway) but the one where the WiFi task *starts a new session* mid-memset: it runs `:2680`'s memset, writes `sessionId`, `totalChunks`, `lastActivityMs` and `active = true`, and loop()'s memset — already past those offsets or still to reach them — zeroes an arbitrary subset. The result is `active == true` with `sessionId == 0` and `totalChunks == 0`, or a `receivedMask` that no longer matches the chunk array. The 2 s busy guard at `:2674-2675` then rejects the genuine sender's remaining chunks (`millis() - 0` is large, so it will actually let them through and restart the session repeatedly), and `checkMgmtTimeout` cannot reap it for another 15 s.

**Failure scenario.** A relayed config push to this board stalls after chunk 1 of 4 (the relay is power-cycled mid-push). 15 s later `loop()` enters `checkMgmtTimeout` and begins the 2.9 KB memset. In that window a WCB_Client host on the mesh starts its own FRAG_UNICAST session; the WiFi task preempts (different core), sees `active` already zeroed, runs its own memset and writes sessionId/totalChunks/active. loop() resumes and finishes zeroing the tail of the struct, wiping `lastActivityMs` and `chunks[]` under the new session. The client's remaining chunks land in a session whose `receivedMask` and chunk buffers disagree; the command never completes and the client's push fails silently, ~15 s after an unrelated timeout.

**Suggested fix.** Give `mgmtSession` a `portMUX_TYPE` and wrap the mutating regions on both sides (`handleMgmtPacket`'s session-start/chunk-store block and `checkMgmtTimeout`'s memset) in `portENTER_CRITICAL`/`portEXIT_CRITICAL` — or, consistent with the ETM ACK precedent at `:1099-1113` and the deferral of CONFIG_REQ/OTA at `:3634`/`:3663`, move MGMT frag handling off the WiFi task entirely by enqueueing the 226-byte packet and draining it in `loop()`, which also removes the String reassembly and `parseCommandsAndEnqueue` heap churn from the receive callback.

### F-282 · S3 · `Code/WCB/WCB.ino:2983`

**Request dedup keeps a single (requester, millis) pair, so two concurrent relays defeat it entirely and each produces a duplicate full fragmented response**

*Category:* `logic`

The burst-suppression state in handleConfigReqPacket is one scalar pair, not per-requester:

```c
  static uint32_t lastConfigReqMs   = 0;
  static uint8_t  lastConfigReqFrom = 0;
  uint32_t nowMs = millis();
  if (pkt.requesterWCB == lastConfigReqFrom && (nowMs - lastConfigReqMs) < 1500) {
    ... return;
  }
  lastConfigReqFrom = pkt.requesterWCB;
  lastConfigReqMs   = nowMs;
```

Any request from a *different* requester overwrites `lastConfigReqFrom`, so two interleaved 3x bursts (WCB.ino:3547-3551 sends 3, 15 ms apart) alternate A,B,A,B,A,B and **no** request is ever suppressed: all six are served. Each service is a full `buildConfigString()` (NVS re-read) plus two passes of up to 16 frags with `delay(20)` each — WCB.ino:3014-3036 — so six responses is ~3.8 s of blocked loop() on the target and up to 192 broadcast frames dumped onto the mesh, at a moment when two relays are already pulling. The identical single-pair pattern is in handleSeqReqPacket (WCB.ino:3153-3161) and, keyed on one extra field, handleSeqValReqPacket (WCB.ino:3192-3205).

Two concurrent relays is a supported configuration, not a hypothetical: the Wizard's `relayRouteAll` guard `_relayRouteAllBusy` is per relay slot (Wizard/app.js:504), and `remoteBoardPull` sequences pulls only within one relay — two USB-connected relay boards that both heard the same target will both pull it.

**Failure scenario.** Two relay boards are connected to the Wizard and both run Route-All; both discovered WCB5. Relay W1 sends 3x CONFIG_REQ for W5 and relay W2 does the same, bursts overlapping. W5's queue receives them interleaved; drainMgmtReqs serves all six because lastConfigReqFrom flip-flops between 1 and 2. W5's loop() is blocked ~3.8 s (ETM heartbeat processing, OTA drain and checkConfigPullTimeout all stall, producing spurious ETM retries), and it broadcasts ~192 frag frames. Both relays then see interleaved frags from the same target: each frag with the other session's id resets the peer session at WCB.ino:3059, so both pulls can fail and be retried, amplifying the flood.

**Suggested fix.** Key the dedup on the requester rather than on a single slot — e.g. a small `uint32_t lastReqMs[MAX_WCB_COUNT]` indexed by `requesterWCB - 1` (bounds-checked), so a burst from W1 is suppressed independently of one from W2. Same change in handleSeqReqPacket and handleSeqValReqPacket.

### F-283 · S3 · `Code/WCB/WCB.ino:3080`

**Frag reassembly completes against the CURRENT packet's totalChunks, not the session's — ConfigPullSession::totalChunks is written by all five handlers and never read**

*Category:* `bounds`

Every one of the five handlers stores the session's chunk count and then ignores it:

```c
    pullSession.totalChunks    = pkt.totalChunks;   // :3063
    ...
  uint16_t expectedMask = (uint16_t)((1 << pkt.totalChunks) - 1);          // :3080
  if (pullSession.receivedMask == expectedMask) {
    for (int i = 0; i < pkt.totalChunks; i++) fullConfig += String(pullSession.chunks[i]);  // :3083
```

Both the completion test and the reassembly loop use `pkt.totalChunks` — a byte taken straight off the wire on *this* packet — rather than `pullSession.totalChunks`, which the session established when it started. Grepping the tree, `ConfigPullSession::totalChunks` (declared WCB.ino:868) is assigned at WCB.ino:3063, :3313, :3348, :3383, :3418 and **read nowhere**. The same pattern repeats at :3324/:3326, :3359/:3361, :3394/:3396, :3428/:3431.

The guards at WCB.ino:3048-3049 only bound the value (`0 < totalChunks <= 16`, `chunkIdx < totalChunks`); they do not check it against the session that is already in progress. A frag whose `sessionId` matches the live session but whose `totalChunks` is smaller therefore reduces `expectedMask` mid-session and can make a partially-filled mask compare equal, delivering a truncated blob as if it were complete. The stored field being dead is what makes this invisible — it looks like the session is validating consistency when nothing does.

**Failure scenario.** A 12-chunk config session is in progress with chunks 0-3 received (receivedMask = 0x000F). A frag arrives bearing the same sessionId but with totalChunks corrupted to 4 (single-byte corruption, or a mixed-version/forged sender): it passes the password check, passes `totalChunks != 0 && <= 16`, passes `chunkIdx < totalChunks`, and does not trip the `sessionId != pkt.sessionId` reset at :3059. expectedMask becomes 0x000F, matches, and the relay emits `[MGMT:CONFIG,n]` containing only the first 4 chunks — plus sets `lastDeliveredSession` (:3085), which then suppresses every remaining real frag of that session, so the pull cannot recover without a full Wizard retry.

**Suggested fix.** Use the session's stored value everywhere after the session is established, and reject a frag that disagrees with it: after the start/continue block, `if (pkt.totalChunks != pullSession.totalChunks) return;` then compute expectedMask and the reassembly loop from `pullSession.totalChunks`. Apply to all five handlers.

### F-284 · S3 · `Code/WCB/WCB.ino:3088`

**All five FRAG handlers build a multi-KB String and do a blocking ~2.9 KB Serial.printf inside the ESP-NOW receive callback — the exact stall the REQ path was deferred to avoid**

*Category:* `concurrency`

espNowReceiveCallback dispatches the frag handlers **inline**, on the WiFi task:

```c
  if (len == sizeof(espnow_struct_config_frag)) {
    uint8_t ptype = ((const espnow_struct_config_frag*)incomingData)->packetType;
    if      (ptype == PACKET_TYPE_CONFIG_FRAG) handleConfigFragPacket(incomingData);
    else if (ptype == PACKET_TYPE_STATS_FRAG)  handleStatsFragPacket(incomingData);
    ...
```
(WCB.ino:3648-3657). On the completing frag each handler does heap String concatenation of up to 16 x 183 bytes and then a blocking Serial write of the whole thing:

```c
    String fullConfig = "";
    for (int i = 0; i < pkt.totalChunks; i++) fullConfig += String(pullSession.chunks[i]);
    ...
    Serial.printf("[MGMT:CONFIG,%d]%s\n", srcWCB, fullConfig.c_str());
```
(WCB.ino:3082-3088; identical at :3326-3329, :3361-3364, :3396-3399, :3431-3434). `Serial.begin(115200)` at WCB.ino:7120 with no `setTxBufferSize()` anywhere in the sketch, so ~2900 bytes is roughly **250 ms** of blocking inside the receive callback.

This directly contradicts the design rule stated 500 lines earlier in the same file, WCB.ino:3251-3256: "Running that in the ESP-NOW receive callback (WiFi task) stalls ESP-NOW and drops other inbound packets (heartbeats, OTA frags, RC telemetry). So the callback only ENQUEUES the 43-byte request here." loop() also carries `drainRcJsonRelay()` (WCB.ino:7479) explicitly labelled "cross-core safe Serial output" for exactly this reason. The request side was deferred; the response side, which produces 20x more Serial output, was not.

Second, related defect at the same site: `checkConfigPullTimeout()` runs in loop() (Arduino task, core 1) and does `memset(&pullSession, 0, sizeof(pullSession))` (WCB.ino:3567, :3575, :3580) on the same structs these handlers write from the WiFi task (core 0), with no portMUX, no queue, and no volatile.

**Failure scenario.** A relay completes a 16-chunk config pull. The final frag lands on the WiFi task, which spends ~250 ms building the String and pushing 2.9 KB out UART0 before returning. During that window the target is still transmitting its second redundant pass (16 frags at 20 ms = 320 ms, WCB.ino:3014-3036) and every other board is sending ETM heartbeats; those inbound packets are dropped at the ESP-NOW layer, showing up as ETM retries/failures and, if an OTA is running concurrently, as dropped OTA data frags. Separately, if a pull stalls for 10 s and the last frag arrives in the same microseconds that loop() executes the timeout memset, the WiFi task reassembles from a zeroed buffer and emits a truncated `[MGMT:CONFIG,n]`.

**Suggested fix.** Do in the frag handlers what WCB.ino:3265-3277 already does for requests: on completion, hand the reassembled String (or the chunk array + srcWCB) to a queue and let a `drainMgmtResults()` in loop() do the Serial.printf. That also removes the cross-core write to pullSession/seqRelaySession/seqValRelaySession, since the reaper would then be on the same task as the only other writer.

### F-285 · S3 · `Code/WCB/WCB.ino:3450`

**STATS_REQ and ETM_REQ are broadcast exactly once while every sibling request is sent 3x; the queue-drop comment claims a 3x retry that does not exist for them**

*Category:* `error-handling`

`handleMgmtStatsRequest` fires a single unacknowledged broadcast:

```c
  esp_now_send(broadcastMACAddress[0], (uint8_t *)&pkt, sizeof(pkt));   // :3450
```
and `handleMgmtETMRequest` does the same at WCB.ino:3524. Every other request on this family sends the identical struct three times, with an explicit comment explaining why: `handleMgmtPullRequest` (WCB.ino:3540-3552) — "ESP-NOW broadcast is unacknowledged and the first frame to a board is frequently dropped on a busy mesh — which used to cost the wizard a full pull-timeout before it retried" — plus `handleMgmtSeqRequest` (WCB.ino:3468-3474) and `handleMgmtSeqValRequest` (WCB.ino:3508-3513). The stated failure mode applies identically to STATS_REQ and ETM_REQ; nothing distinguishes them.

The consequence is compounded by the queue-drop comment at WCB.ino:3276:

```c
  xQueueSend(mgmtReqQueue, &slot, 0);   // drop if full → the requester retries (3x REQ + browser retry)
```

That justification is factually wrong for two of the five packet types this queue carries: a dropped STATS_REQ or ETM_REQ slot is never re-sent by the requester, because the requester only ever sent one. The queue is 8 deep (WCB.ino:7131) and a single `?MGMT,PULL` alone enqueues 3 slots.

Note the target-side dedup guards were added only for the 3x senders (handleConfigReqPacket WCB.ino:2980-2988, handleSeqReqPacket WCB.ino:3153-3161, handleSeqValReqPacket WCB.ino:3192-3205); handleStatsReqPacket (WCB.ino:3126) and handleETMReqPacket (WCB.ino:3234) have none, so simply sending 3x without adding a guard would fire three full fragmented responses.

**Failure scenario.** Operator requests stats from WCB12 through a relay while an OTA or a Kyber stream is loading the mesh. The single STATS_REQ broadcast is dropped. WCB12 never hears it, no frag is ever produced, statsRelaySession is never even created, and (per the checkConfigPullTimeout finding) nothing times out to report it. The Wizard shows no stats and no error; the operator clicks again and it works, which reads as flaky hardware.

**Suggested fix.** Give handleStatsReqPacket and handleETMReqPacket the same `(lastReqFrom, lastReqMs) < 1500 ms` dedup guard the other three have, then send both requests 3x with delay(15) exactly like handleMgmtPullRequest. Fix the WCB.ino:3276 comment once that is true for all five types.

### F-286 · S3 · `Code/WCB/WCB.ino:3563`

**checkConfigPullTimeout reaps only 3 of the 5 ConfigPullSession structs — statsRelaySession and etmRelaySession can never be cleared**

*Category:* `logic`

There are five relay-side reassembly sessions (WCB.ino:873-877): `pullSession`, `statsRelaySession`, `etmRelaySession`, `seqRelaySession`, `seqValRelaySession`. `checkConfigPullTimeout()` — the only reaper, called once per loop() at WCB.ino:7491 — handles exactly three of them:

- `pullSession` (WCB.ino:3564-3568)
- `seqRelaySession` (WCB.ino:3572-3576)
- `seqValRelaySession` (WCB.ino:3577-3581)

`statsRelaySession` and `etmRelaySession` appear nowhere in the function. Grepping the whole tree, the only code that clears them is the `memset` inside their own frag handlers (WCB.ino:3310, 3328 and WCB.ino:3415, 3433). Both of those require a **new** frag to arrive: one whose `sessionId` differs (resetting the session), or one that completes it.

The adjacent comment at WCB.ino:3569-3571 states the reason reaping exists — "Without it a half-received inventory (target rebooted mid-send) stays 'active' until a new sessionId happens along, and its stale chunks would prefix the next reassembly" — and that reason applies verbatim to the stats and ETM sessions, which are structurally identical. They were simply not added when the seq/seqVal ones were.

Unlike the config path, `sendResultFrags` sends **one** pass (WCB.ino:3104-3120), not two like handleConfigReqPacket's loss-resilient double pass (WCB.ino:3014), so a single lost frag leaves a stats/ETM session permanently half-filled with `active = true` and `lastActivityMs` frozen — ~5.8 KB of the two structs pinned holding stale chunk data that nothing will ever invalidate.

**Failure scenario.** Operator pulls stats from WCB7 through a relay. One of the 4 frags is lost on a busy mesh. statsRelaySession stays active with sessionId S and receivedMask 0b1101 forever — checkConfigPullTimeout walks right past it. If a later stats or ETM response happens to draw the same 16-bit id (sessionId = random(1, 0xFFFF), so 1 in 65534 per pull) the `statsRelaySession.sessionId != pkt.sessionId` guard at WCB.ino:3309 does not fire, the memset is skipped, and the previous board's stale chunks are spliced into the new reassembly — emitting a `[MGMT:STATS,n]` line that is part one board's data and part another's, with no CRC on this path to catch it.

**Suggested fix.** Add the two missing blocks to checkConfigPullTimeout, or better, replace the five copy-pasted bodies with a helper: `static void reapSession(ConfigPullSession &s, const char *label)` and call it for all five. The five frag handlers are similarly duplicated and could take the session by reference.

### F-287 · S3 · `Code/WCB/WCB.ino:4688`

**?ETM,MISS / ?ETM,HB / ?ETM,TIMEOUT accept and persist 0 with no range check, unlike every other numeric setter in processLocalCommand**

*Category:* `bounds`

```
4679        } else if (etmCmdUpper == "TIMEOUT") {
4680            etmTimeoutMs = etmVal.toInt();
4683        } else if (etmCmdUpper == "HB") {
4684            etmHeartbeatSec = etmVal.toInt();
4687        } else if (etmCmdUpper == "MISS") {
4688            etmMissedHeartbeats = etmVal.toInt();
4691        } else if (etmCmdUpper == "BOOT") {
4692            etmBootHeartbeatSec = etmVal.toInt();
```

Each calls `saveETMSettings()` immediately, so a bad value is written to NVS and survives reboot. `String::toInt()` returns 0 for an empty or non-numeric argument, so `?ETM,MISS,` and `?ETM,MISS,x` both store 0.

This is the odd one out in this function: `?WCB` range-checks 1..MAX_WCB_COUNT (4767), `?WCBCH` checks 1..11 with an explicit comment about checking before the cast (4784-4795), `?ETM,COUNT` uses `constrain(..., 10, 200)` (4696), `?CONTROLLER,ON,<id>` checks 1..20 (4868).

Traced consequences:
- `?ETM,MISS,0` -> WCB.ino:1040 `offlineThresholdMs = (unsigned long)(etmHeartbeatSec + 1) * etmMissedHeartbeats * 1000UL` = 0, so `millis() - lastSeenMs > 0` (1045) is true for every peer on every sweep. Every board is driven offline immediately after each heartbeat. `boardTable[b].online` gates real behaviour: WCB.ino:1204 `if (!boardTable[b].online) continue;` drops it from the ETM expected-ACK set, WCB.ino:1448 excludes it from the ETM-char peer list, and WCB.ino:6879 `isBoardOnline()` returns false for it.
- `?ETM,HB,0` -> WCB.ino:1011 `random((etmHeartbeatSec - 1) * 1000UL, (etmHeartbeatSec + 1) * 1000UL)`: `(0-1)*1000UL` wraps to 4294966296, which converts to -1000 as the `long` parameter of `random(long,long)`. The result can be negative; `intervalMs = (unsigned long)negative` then makes `etmNextHeartbeatMs = millis() + intervalMs` (1018) a time already in the past, so `millis() >= etmNextHeartbeatMs` (1033) fires a heartbeat broadcast on essentially every loop pass.

The values also round-trip through the config backup (WCB.ino:2868-2870 emit `ETM,HB` / `ETM,MISS` / `ETM,TIMEOUT` verbatim), so a bad value replicates to every board the Wizard pushes that config to.

**Failure scenario.** Operator (or a Wizard field left blank / cleared) sends `?ETM,MISS,0`. It is accepted silently and written to NVS. From the next `processETMHeartbeats()` sweep on, the console prints '[ETM] WCBn went OFFLINE (no heartbeat for 0s)' for every peer, and because 1204/1448/6879 all key off `online`, ETM stops expecting ACKs from any board and `isBoardOnline()` reports the whole mesh down. Rebooting does not clear it. Because the value rides the config backup (2869), pushing that board's config to the fleet spreads it.

**Suggested fix.** Range-check before assigning and persisting, in the style already used two blocks away: `etmHeartbeatSec = constrain(etmVal.toInt(), 1, 3600); etmMissedHeartbeats = constrain(etmVal.toInt(), 1, 60); etmTimeoutMs = constrain(etmVal.toInt(), 50, 10000); etmBootHeartbeatSec = constrain(etmVal.toInt(), 1, 60);` and reject/report a value outside range rather than storing it.

### F-288 · S3 · `Code/WCB/WCB.ino:5131`

**?LED,PIN accepts any GPIO 0-48, including SPI-flash and input-only pins, and persists it before it is proven to work**

*Category:* `bounds`

WCB.ino:5129-5140 validates the LED pin only against a numeric range: `if (pin < 0 || pin > 48) { … return; } saveStatusLEDPin(pin); STATUS_LED_PIN = pin; … initStatusLEDWithRetry(5, 100);`. `saveStatusLEDPin` (WCB_Storage.cpp:108-113) adds no validation of its own — it writes `led_pin` to NVS unconditionally.

0-48 is the ESP32-S3 GPIO span, but the same handler compiles and runs on the classic-ESP32 target (`Code/bin/build.sh` builds `esp32:esp32:esp32` and `esp32s3`), and the classic ESP32 NeoPixel path is live for HW 2.1/2.3/2.4 as well as 3.1/3.2 — WCB.ino:7171 `else if (wcb_hw_version == 21 || 23 || 24 || 31 || 32) { initStatusLEDWithRetry(5, 100); }`. On that target GPIO 6-11 are the SPI flash bus, 34-39 are input-only, and 40-48 do not exist.

Critically the value is saved to NVS **before** the pin is exercised, and it is re-applied on every boot with no sanity check: `loadStatusLEDPin()` (WCB_Storage.cpp:115-123) restores it whenever `savedPin >= 0`, `setup()` calls it at WCB.ino:7139, and `initStatusLEDWithRetry` at :7174 then does `pinMode(STATUS_LED_PIN, OUTPUT); digitalWrite(STATUS_LED_PIN, LOW);` (:6735-6736) on it. So a bad pin is not a one-shot mistake; it is baked into the boot path.

**Failure scenario.** On an ESP32 (HW 2.4) board the operator fat-fingers `?LED,PIN,8` instead of `?LED,PIN,18`. 8 passes the 0-48 check, is written to NVS by `saveStatusLEDPin`, and `initStatusLEDWithRetry` drives GPIO8 — an SPI flash line — LOW while the chip is executing XIP from that flash. The board faults immediately; on reboot `loadStatusLEDPin()` restores 8 and `setup()` at :7174 repeats the fault before the console is usable, so the board is in a boot loop with no serial window to type `?LED,PIN,18` into. Recovery requires esptool. The milder variant (`?LED,PIN,35`, input-only) is silent: all five init attempts fail, `statusLED` stays nullptr, the status LED is dead on every subsequent boot, and only the one-line "⚠️ NeoPixel initialization failed" at :7177 hints at why.

**Suggested fix.** Validate against the actual target: reject the flash/PSRAM and non-existent pins per chip — on ESP32 allow 0-33 minus 6-11 (and warn on 34-39, which cannot drive output); on ESP32-S3 exclude the 26-32 flash/PSRAM range. Better still, only persist after `initStatusLEDWithRetry` reports success — move `saveStatusLEDPin(pin)` below the init and restore the previous pin if `statusLED == nullptr`, so a bad pin can never enter the boot path.

### F-289 · S3 · `Code/WCB/WCB.ino:5321`

**ETM timing setters persist unvalidated integers to NVS — ?ETM,HB,0 wraps the heartbeat interval, ?ETM,MISS,0 marks every peer offline forever**

*Category:* `bounds`

The legacy ETM setters in this range take `toInt()` straight to the global and straight to NVS with no range check:
  * :5313 `etmTimeoutMs = message.substring(10).toInt(); saveETMSettings();`
  * :5317 `etmBootHeartbeatSec = message.substring(7).toInt(); saveETMSettings();`
  * :5321 `etmHeartbeatSec = message.substring(5).toInt(); saveETMSettings();`
  * :5325 `etmMissedHeartbeats = message.substring(7).toInt(); saveETMSettings();`
Only `etmCharMessageCount` is guarded (`constrain(..., 10, 200)` at :5303). The new-format twins at WCB.ino:4680/:4684/:4688/:4692 have the same gap, so this is not a legacy-only path.

The globals are plain `int` (WCB.ino:265-270) and are used in unsigned arithmetic:
  * WCB.ino:1011 `intervalMs = (unsigned long)random((etmHeartbeatSec - 1) * 1000UL, (etmHeartbeatSec + 1) * 1000UL);` — with `etmHeartbeatSec == 0`, `(0-1)` is int −1, converted to `unsigned long` for the `* 1000UL`, giving 4294966296 (i.e. −1000 when the `long` parameter is read). `random(-1000, 1000)` can return a negative value, which the `(unsigned long)` cast at :1011 turns into ~4.29e9 ms, so `etmNextHeartbeatMs = millis() + intervalMs` (:1013) lands ~49 days out and `processETMHeartbeats`'s `if (millis() >= etmNextHeartbeatMs)` at :1034 never fires again.
  * WCB.ino:1040 `unsigned long offlineThresholdMs = (unsigned long)(etmHeartbeatSec + 1) * etmMissedHeartbeats * 1000UL;` — with `etmMissedHeartbeats == 0` this is 0, so the sweep at :1045 `if (millis() - boardTable[i].lastSeenMs > offlineThresholdMs)` is true on essentially every pass and every peer is torn down to `online = false`.
  * WCB.ino:1281 `if (millis() - entry.lastSentMs < (unsigned long)etmTimeoutMs) continue;` — with `etmTimeoutMs == 0` every pending ETM entry is retried on every pass and burns its 3 retries immediately.

`saveETMSettings()` (WCB_Storage.cpp:2211) writes all of these to NVS, so the bad value survives reboot; there is no clamp on load either.

**Failure scenario.** An operator types `?ETM,MISS,0` (or the legacy `?etmmiss0`) intending "never mark a board offline". `offlineThresholdMs` becomes 0, so on the very next `processETMHeartbeats()` pass every entry in `boardTable[]` flips to `online = false` and the console fills with "[ETM] WCBn went OFFLINE (no heartbeat for 0s)". Everything that gates on liveness breaks: `routeStoredOrCap` (WCB.ino:5835) sees `wcbPeerOnline(target)` false and treats every pinned host as "genuinely dead", failing HCR/MP3 triggers over to whatever `wdpCapOwner()` returns. The value is in NVS, so rebooting does not clear it. `?ETM,HB,0` is worse — the board stops sending heartbeats entirely and every peer independently declares it offline.

**Suggested fix.** Range-check before assigning and persisting, in both the legacy block (WCB.ino:5312-5327) and the `?ETM` block (:4679-4694) — e.g. `etmTimeoutMs = constrain(v, 50, 10000)`, `etmHeartbeatSec = constrain(v, 1, 300)`, `etmMissedHeartbeats = constrain(v, 1, 100)`, `etmBootHeartbeatSec = constrain(v, 2, 60)`, mirroring the existing `constrain` on `etmCharMessageCount` at :5303. Clamp on load in `loadETMSettings()` too, so an already-poisoned NVS recovers.

### F-290 · S3 · `Code/WCB/WCB.ino:6681`

**RawSerialForwardingTask's "write() queues; the UART drains async" comment is false for outputs on S3-S5, which bit-bang synchronously and stall the whole forwarding sweep**

*Category:* `doc-drift`

```
WCB.ino:6679-6683
    Stream &outputSerial = getSerialStream(output.serialPort);
    outputSerial.write(buffer, len);
    // No flush() - write() queues; the UART drains async.
    // Avoids blocking this forwarding task per chunk.
```
`getSerialStream` (WCB.ino:1869-1878) returns `Serial3/4/5` for ports 3-5, and those are EspSoftwareSerial instances (WCB.ino:7255-7285 `Serial3.begin(baudRates[2], SWSERIAL_8N1, ...)`). EspSoftwareSerial has no TX ring buffer - `UARTBase::write()` bit-bangs every bit inline:
```
EspSoftwareSerial/src/SoftwareSerial.cpp:348  size_t IRAM_ATTR UARTBase::write(const uint8_t* buffer, size_t size, Parity parity)
:367   for (size_t cnt = 0; cnt < size; ++cnt) { ... writePeriod(...) ... }
:286   void IRAM_ATTR UARTBase::preciseDelay() { do { ticks = ...; } while ((ticks - m_periodStart) < m_periodDuration); }
```
So the call returns only after the last stop bit has been shifted out. The buffer here is 64 bytes (`static uint8_t buffer[64]`, WCB.ino:6637), i.e. 640 bit-times: ~67 ms at the 9600 default baud (`SERIAL3_DEFAULT_BAUD_RATE 9600`, WCB.ino:624). The comment's claim, and the deliberate omission of `flush()` that it justifies, only hold for the hardware UARTs (Serial/Serial1/Serial2).

Because the sweep at 6643-6715 is a single loop over all five mappings and the write happens inside it, that stall blocks the RX side of every OTHER raw-mapped port for the same duration - and the task deliberately does not yield at all while `hadData` is true (6718-6721).

**Failure scenario.** A raw mapping forwards a binary stream from S1 into S4 (a SoftwareSerial port at its 9600 default) while a second raw mapping forwards S2 -> ESP-NOW. A 64-byte chunk arrives on S1; `outputSerial.write(buffer, 64)` on Serial4 blocks the task for ~67 ms. During that window nothing drains S2. At 115200, S2's RX ring accumulates ~770 bytes and overflows well before the sweep comes back around, so bytes are silently lost from the middle of a binary frame - the exact corruption the raw path exists to avoid. The adjacent comment tells the next reader this cannot happen.

**Suggested fix.** Correct the comment to scope the async claim to S0/S1/S2, and either cap the per-chunk write for SoftwareSerial outputs (write in small slices with a `vTaskDelay(0)` between them) or split the sweep so a slow soft-UART output cannot block the read side of the other mappings.

### F-291 · S3 · `Code/WCB/WCB.ino:6740`

**initStatusLEDWithRetry overwrites the existing statusLED pointer without deleting it, leaking an Adafruit_NeoPixel on every ?LED,PIN**

*Category:* `leak`

`initStatusLEDWithRetry` allocates unconditionally at the top of each attempt:
```
WCB.ino:6740  statusLED = new Adafruit_NeoPixel(STATUS_LED_COUNT, STATUS_LED_PIN, NEO_GRB + NEO_KHZ800);
```
The only `delete` is on the failure branch (6754), so any call made while `statusLED` is already non-null drops the previous object on the floor. `statusLED` is the single global `Adafruit_NeoPixel* statusLED = nullptr;` (WCB.ino:658).

That call is reachable at runtime, not just at boot. Boot allocates one at WCB.ino:7174 (`initStatusLEDWithRetry(5, 100);` inside the hw-version branch), and the `?LED,PIN,<gpio>` handler calls it again with `statusLED` still holding the boot instance:
```
WCB.ino:5133-5139  saveStatusLEDPin(pin);
                   STATUS_LED_PIN = pin;
                   Serial.printf("LED pin changed to GPIO%d. Reinitializing LED...\n", pin);
                   initStatusLEDWithRetry(5, 100);
```
Each invocation leaks the object plus its pixel buffer, and also leaves the OLD GPIO still configured as OUTPUT and driven LOW by 6735-6736 with no release.

There is a second, narrower hazard on the same lines: `identifyTask` (WCB.ino:732-749, created at 5082 on core 1 priority 1) dereferences `statusLED` via `colorWipeStatus` at 740-741 after only a `!= nullptr` check. The `delete statusLED` at 6754 and the pointer swap at 6740 are not synchronized with it, so re-initializing the LED while `?IDENTIFY` is running can free the object under that task.

**Failure scenario.** Operator moves the status LED with `?LED,PIN,48`, sees it not light, and tries a few pins (`?LED,PIN,21`, `?LED,PIN,38`, ...). Each command leaks one Adafruit_NeoPixel object plus its 3-byte pixel buffer and leaves another GPIO stuck driven LOW. On a board where the Wizard pushes a full config repeatedly this accumulates with no upper bound and never reclaims. If `?IDENTIFY` is running concurrently, the retry loop's `delete statusLED` (6754) frees the object that identifyTask is calling `setPixelColor`/`show` on -> use-after-free on a live NeoPixel buffer.

**Suggested fix.** At the top of the retry loop do `if (statusLED) { delete statusLED; statusLED = nullptr; }` before the `new`, and restore the previously driven pin to INPUT when STATUS_LED_PIN changes. To close the identifyTask window, delete the identify task (or set a flag it checks) before tearing down `statusLED`.

### F-292 · S3 · `Code/WCB/WCB.ino:6846`

**enableControllerPeer clears only wcbPeerLearned, not wcbPeerTemporary, so a temporary peer promoted to controller stays wcbPeerActive and is later unregistered by the temporary reaper**

*Category:* `logic`

The mutual-exclusion block at the end of `enableControllerPeer` handles only the learned bit:
```
WCB.ino:6841-6851
  // Mutual exclusion with the learned-peer set: an id that is the controller is
  // the SPECIAL peer, never ALSO a learned peer. ...
  if (id >= 1 && id <= MAX_WCB_COUNT && wcbPeerLearned[id - 1]) {
    wcbPeerLearned[id - 1] = false;
    learnedPeersDirty      = true;
    learnedPeersFlushMs    = millis() + LEARNED_FLUSH_DEBOUNCE_MS;
    rebuildActivePeers();
  }
```
`wcbPeerTemporary[id - 1]` is left set. `rebuildActivePeers()` (6857-6862) ORs it straight into membership:
```
WCB.ino:6860  wcbPeerActive[i] = (floorMember || wcbPeerLearned[i] || wcbPeerTemporary[i]) && (i + 1 != WCB_Number);
```
so the controller id is now BOTH `wcbPeerActive[]` and the special peer - the state that `addActivePeer` (6908) and `addTemporaryPeer` (6946) explicitly refuse to create, and that `wcbPeerOnline`'s comment (6870-6871) says cannot exist.

Two consequences:
1. `etmAddToPendingTable`'s special-peer exception is scoped to unicast on purpose - `bool isSpecialPeerSlot = (specialPeerEnabled && (b + 1) == WCB_SPECIAL_PEER_ID && targetWCB == WCB_SPECIAL_PEER_ID);` (WCB.ino:1199-1200), with a long comment at 1189-1198 explaining that expecting the controller's ACK on broadcasts "costs one extra ACK plus a potential unicast retry burst on EVERY broadcast". With `wcbPeerActive[b]` true the `if (!wcbPeerActive[b] && !isSpecialPeerSlot) continue;` guard passes anyway, so every broadcast now expects the controller's ACK - the overhead the comment says was deliberately avoided.
2. The temporary reaper still owns the id:
```
WCB.ino:1071-1088  if (!wcbPeerTemporary[i] || boardTable[i].lastSeenMs == 0) continue;
                   if (millis() - boardTable[i].lastSeenMs > TEMPORARY_PEER_TTL_MS) { ... rebuildActivePeers();
                     if (!wcbPeerActive[i] && esp_now_is_peer_exist(WCBMacAddresses[i])) { esp_now_del_peer(...); ... } }
```
After `TEMPORARY_PEER_TTL_MS` (50 s, WCB.ino:475) of silence it clears the temporary bit, sees the id is no longer active (an out-of-band controller is not a floor member and no longer learned), and `esp_now_del_peer`s the CONTROLLER'S registration - which nothing on the WDP/auto-join path will restore, because WCB_WDP.cpp:660 skips `isSpecialPeer` and `addActivePeer` rejects it at 6908.

**Failure scenario.** A device advertising WDP_TLV_FLAGS bit0 TEMPORARY (WCB_WDP.h:80, adopted via `addTemporaryPeer` at WCB_WDP.cpp:666) is later pinned as the controller with `?CONTROLLER,ON,<that id>` (WCB.ino:4874). `wcbPeerTemporary` stays true. Every broadcast the board sends now allocates an extra pending-ACK slot for the controller and can burn a unicast retry burst on it. Then the controller goes quiet for 50 s (reboot, OTA, moved room); the reaper at WCB.ino:1077-1085 deletes its ESP-NOW peer entry. When it returns, its heartbeats still mark `boardTable[idx].online = true` (WCB.ino:3766 admits it via `isSpecialPeer`), so `wcbPeerOnline()` reports it reachable and routing keeps selecting it - but every unicast to it returns ESP_ERR_ESPNOW_NOT_FOUND until an ETM retry happens to re-register it on demand (WCB.ino:1327-1331). Non-ETM unicasts to the controller stay broken until reboot.

**Suggested fix.** In the mutual-exclusion block at 6846, clear the temporary bit too and trigger the same rebuild: `if (wcbPeerLearned[id-1] || wcbPeerTemporary[id-1]) { wcbPeerLearned[id-1] = false; wcbPeerTemporary[id-1] = false; ... rebuildActivePeers(); }` (only set `learnedPeersDirty` when the learned bit actually changed, so a temporary-only clear does not schedule a pointless NVS write).

### F-293 · S3 · `Code/WCB/WCB.ino:6870`

**wcbPeerOnline's "the special peer is never in wcbPeerActive[]" invariant is broken by rebuildActivePeers for any controller id inside the WCBQ floor**

*Category:* `logic`

```
WCB.ino:6870-6873
  // The controller / special peer is never in wcbPeerActive[] - addActivePeer rejects
  // it because it doesn't consume a WCB slot - yet it IS a registered unicast target
  // ...
```
The claim only holds for out-of-band controller ids. `wcbPeerActive[]` is not written by `addActivePeer` alone - `rebuildActivePeers()` ten lines above recomputes it from scratch and unconditionally admits every floor id:
```
WCB.ino:6858-6861
  for (int i = 0; i < MAX_WCB_COUNT; i++) {
    bool floorMember = (i < Default_WCB_Quantity);
    wcbPeerActive[i] = (floorMember || wcbPeerLearned[i] || wcbPeerTemporary[i]) && (i + 1 != WCB_Number);
  }
```
There is no special-peer exclusion in that expression. The controller id is operator-chosen over the full 1-20 range - the `?CONTROLLER,ON,<id>` handler validates only `if (newId < 1 || newId > 20)` (WCB.ino:4868-4871) and the help text at 4855-4856 says "default ID 20, e.g. NaviCore, but any 1-20 ID for a different controller". So `?CONTROLLER,ON,3` on a board with `?WCBQ,5` makes `wcbPeerActive[2] == true` for the special peer, permanently and by construction (every `rebuildActivePeers()` call re-derives it).

Same downstream effect as the temporary-peer case: `etmAddToPendingTable`'s `if (!wcbPeerActive[b] && !isSpecialPeerSlot) continue;` (WCB.ino:1201) no longer confines the controller's ACK expectation to unicast, defeating the explicitly-reasoned scoping at WCB.ino:1189-1200. It also makes the controller double-counted in `activePeerCount()` (6893-6897) and in the ETM offline sweep, which walks it once at 1041-1051 as an active member and again at 1054-1063 as the special peer.

**Failure scenario.** A five-board droid runs `?WCBQ,5` and pins the NaviCore-role board as controller with `?CONTROLLER,ON,3`. Every broadcast the board sends - including bulk config-pull and OTA traffic, which the comment at WCB.ino:1194-1197 calls out as "exactly when the mesh is least able to absorb it" - now allocates an expected-ACK slot for WCB3 and, if that ACK is lost, fires a unicast retry burst at it. If WCB3 is momentarily offline the broadcast's pending slot stays open until timeout on every send instead of completing. Nothing in the code or logs points at the controller pin as the cause, and the comment at 6870 tells a reader this state is impossible.

**Suggested fix.** Either exclude the special peer in `rebuildActivePeers()` (`&& !(specialPeerEnabled && (i + 1) == WCB_SPECIAL_PEER_ID)`), matching what `addActivePeer`/`addTemporaryPeer` already enforce, or reject an in-band controller id in the `?CONTROLLER,ON` handler. If in-band controllers are intended to be members, correct the comments at 6870-6873 and 1189-1200 and re-derive the ACK scoping from something other than `wcbPeerActive[]`.


---

## S4

### F-294 · S4 · `Code/WCB/WCB.ino:881`

**commandsToSend[10] / commandsToReceive[10] are dead globals costing ~5 KB of DRAM**

*Category:* `dead-code`

WCB.ino:881-882 declare:
    espnow_struct_message commandsToSend[10]; // Includes 1-9 and broadcast
    espnow_struct_message commandsToReceive[10];
A repo-wide grep across Code/ finds no other reference to either symbol - every send path builds its own local `espnow_struct_message msg` / `espnow_struct_message_etm etmMsg` on the stack (WCB.ino:2331, :2374, :2464, :2506, :1479) and every receive path memcpy's into a local (WCB.ino:4008, :3690).

sizeof(espnow_struct_message) is 249 (the size the callback routes on at WCB.ino:3679 and which setup prints at WCB.ino:7185), so these two arrays permanently occupy 2 * 10 * 249 = 4980 bytes of .bss on a board where the queues alone already reserve ~14 KB (RC_JSON_QUEUE_SLOTS * RC_JSON_MAX_LEN, WCB.ino:327-328).

The trailing comment "Includes 1-9 and broadcast" is also a fossil of the pre-MAX_WCB_COUNT 9-board era - the same hard-coded-9 family the repo notes as a known past bug class - which makes the declaration actively misleading to anyone who assumes it is live send state.

**Failure scenario.** No runtime misbehaviour - this is pure waste plus a misleading artifact. Concretely: ~5 KB of DRAM that is never read or written is unavailable to the heap, on a build where a config pull already allocates a ~2.9 KB String on the WiFi task (WCB.ino:3082-3083) and the RC relay queue holds ~14 KB. A reader who trusts the '1-9 and broadcast' comment can also mistake these for the live per-target send buffers and size a change against 9 boards instead of MAX_WCB_COUNT.

**Suggested fix.** Delete both declarations at WCB.ino:881-882.

### F-295 · S4 · `Code/WCB/WCB.ino:2319`

**espnowCommandDelivered is incremented and reset but never read**

*Category:* `dead-code`

`espnowCommandDelivered` has exactly three references in the entire repository:

- declaration: `unsigned long espnowCommandDelivered = 0;` (`WCB.ino:589`)
- increment: `espnowCommandDelivered++;` inside the ETM send path (`WCB.ino:2319`)
- reset: `espnowCommandDelivered = 0;` in the `?STATS,CLEAR` handler (`WCB.ino:5359`)

It is never read. `buildStatsString()` — the only consumer of the ESP-NOW counters, and what `printESPNowStats()` at `:2266-2268` prints — reports the unicast command block from `espnowCommandAttempts` / `espnowCommandSuccess` / `espnowCommandFailed` (`:1667-1668`) or, under ETM, from the per-board `totalSent` / `totalAckd` / `totalFailed` roll-up (`:1657-1658`). Neither reads `espnowCommandDelivered`.

The increment is also misleading where it sits: it fires when `esp_now_send()` returns `ESP_OK` (`:2317`), i.e. when the frame was handed to the WiFi driver — not when an ETM ACK came back. A counter named "Delivered" that is bumped at enqueue time next to `espnowCommandSuccess++` is exactly the kind of thing a future reader will wire into `buildStatsString` and then report as an ACK-confirmed delivery rate, when ETM's real delivery evidence lives in `etmStatsAckd[]` (`:1697`).

**Failure scenario.** No runtime effect — the value is written and discarded. The risk is a future stats change: someone adds `espnowCommandDelivered` to `buildStatsString()` as the "Delivered" column (the name invites it), and `?STATS` then reports send-queue successes as confirmed deliveries, showing ~100% delivery on a mesh where ETM is failing every ACK.

**Suggested fix.** Delete the variable and its three references (`:589`, `:2319`, `:5359`). If a send-attempt-vs-ACK distinction is actually wanted, the data already exists as `espnowCommandSuccess` (send-queued) versus `etmStatsAckd[]` (ACK-confirmed) and should be surfaced from those rather than from a third counter with an ambiguous name.

### F-296 · S4 · `Code/WCB/WCB.ino:2416`

**sendESPNowRawToSpecificWCB and sendESPNowRawToPort are dead code, and one indexes WCBMacAddresses[-1] for targetWCB 0**

*Category:* `dead-code`

Neither function has a single caller anywhere in the repository. `grep -rn "sendESPNowRawToSpecificWCB\|sendESPNowRawToPort" .` returns only the two definitions themselves, at `WCB.ino:2416` and `WCB.ino:2453` — no call site, no `extern` declaration in any `WCB_*.h`, nothing in `Wizard/` or `tests/`. The three live raw senders are `sendESPNowRaw` (called at `:4275`, `:4289`, `:4303`, `:4327`, `:4346`) and `sendESPNowRawSerial` (called at `:6700`).

Both carry latent defects that only stay harmless because they are unreachable:

- `sendESPNowRawToSpecificWCB` indexes `uint8_t *mac = WCBMacAddresses[targetWCB - 1];` (`:2443`) with no bounds check; `targetWCB == 0` reads 6 bytes before the array. Its sibling `sendESPNowRawSerial` handles that case explicitly (`(targetWCB == 0) ? broadcastMACAddress[0] : ...`, `:2517`).
- It also sets `structTargetID` to the plain WCB number (`:2431`) while packing a binary length into `structCommand[0..1]` (`:2436-2437`). On the receive side that combination falls past the `'K'` / `WCB_TARGET_KYBER` / `WCB_TARGET_RAW_SERIAL` branches (`:4029`, `:4075`, `:4120`) into the normal text-command path at `:4158`, where the binary length bytes would be parsed as a command string. The function could not work as written.
- `sendESPNowRawToPort` has the same unchecked `WCBMacAddresses[targetWCB - 1]` at `:2481`.

Leaving them in place is a live trap: the next person needing targeted raw forwarding will reach for the function that already has the right name and signature.

**Failure scenario.** No runtime failure today — the code is unreachable. The cost is on the next change: a developer adding targeted Kyber or raw forwarding calls `sendESPNowRawToSpecificWCB(buf, len, 0)` expecting the broadcast semantics that `sendESPNowRawSerial` gives for target 0, and gets a 6-byte out-of-bounds read at :2443 plus a packet the receiver mis-routes into the text-command parser at :4158.

**Suggested fix.** Delete both functions. If targeted raw forwarding is wanted later, `sendESPNowRawToPort` is the salvageable one (its `'K'` framing matches the live receiver at `:4029-4072`) — keep it only with a `if (targetWCB < 1 || targetWCB > MAX_WCB_COUNT) return;` guard added, and delete `sendESPNowRawToSpecificWCB` outright since its wire format has no receiver.

### F-297 · S4 · `Code/WCB/WCB.ino:4323`

**The Maestro_Remote branch inside forwardMaestroDataToLocalKyber is unreachable dead code**

*Category:* `dead-code`

`forwardMaestroDataToLocalKyber()` accumulates into `remoteBuf` and sends only when `Maestro_Remote` is true:

```
4311  static uint8_t remoteBuf[64];
4312  int remoteLen = 0;
...
4323      if (Maestro_Remote && remoteLen < (int)sizeof(remoteBuf)) remoteBuf[remoteLen++] = b;
...
4327  if (Maestro_Remote && remoteLen > 0) sendESPNowRaw(remoteBuf, remoteLen);
```

`Maestro_Remote` is always false here:
- The only caller is `KyberLocalTask()` (WCB.ino:6577-6581).
- That task is created only when `Kyber_Local` is true (WCB.ino:7442-7444).
- `Kyber_Local` and `Maestro_Remote` are strictly mutually exclusive at every assignment site — WCB_Storage.cpp:1156-1157 (local), 1179-1180 (remote), 1186-1187 (clear), and the boot loader `loadKyberSettings()` at WCB_Storage.cpp:1454-1466.

So lines 4311, 4312, 4323 and 4327 can never do anything. The 64-byte static buffer is allocated in .bss for a path that never runs, and the code reads as if a Kyber_Local board also relays Maestro replies to the mesh — it does not.

**Failure scenario.** No runtime failure. The cost is a maintainer reading 4323/4327 and believing a Kyber_Local board echoes local Maestro replies onto the mesh, then sizing/debugging the remote reply path against a branch that is compiled but never entered.

**Suggested fix.** Delete the `Maestro_Remote` accumulation (4311-4312, 4323, 4327) from `forwardMaestroDataToLocalKyber`, or — if the relay to the mesh was actually intended for Kyber_Local boards — replace the `Maestro_Remote` predicate with the condition that was meant (and add the flush from finding 2).

### F-298 · S4 · `Code/WCB/WCB.ino:4868`

**?CONTROLLER,ON,<id> bounds-checks against a literal 20 instead of MAX_WCB_COUNT, and that check is the only guard on two array indexes**

*Category:* `bounds`

```
4867                int newId = argsUpper.substring(ci + 1).toInt();
4868                if (newId < 1 || newId > 20) {
4869                    Serial.printf("Invalid controller peer ID %d. Valid range: 1-20.\n", newId);
```

`MAX_WCB_COUNT` is `#define`d to 20 (WCB.ino:119-120) and `WCB_SPECIAL_PEER_ID` (WCB.ino:129, `uint8_t`) is used to index arrays sized `[MAX_WCB_COUNT]`:

```
4882                !wcbPeerLearned[WCB_SPECIAL_PEER_ID - 1] &&
4883                esp_now_is_peer_exist(WCBMacAddresses[WCB_SPECIAL_PEER_ID - 1])) {
4884                esp_now_del_peer(WCBMacAddresses[WCB_SPECIAL_PEER_ID - 1]);
```

(`wcbPeerLearned[MAX_WCB_COUNT]` WCB.ino:468, `WCBMacAddresses[MAX_WCB_COUNT][6]` WCB.ino:773). The same literal 20 appears in the help text at 4889 and in the persist path at WCB_Storage.cpp:424. Every other bound in this function is written against the constant: `?WCB` uses `MAX_WCB_COUNT` (4767, 4770), `configureMaestro` uses it (WCB_Maestro.cpp:597), the Kyber target parser uses it (WCB_Storage.cpp:1247).

Nothing is wrong today because 20 == MAX_WCB_COUNT. The defect is that the only thing keeping `WCB_SPECIAL_PEER_ID - 1` inside two 20-element arrays is a magic number in a different file from the array declarations — precisely the hard-coded-board-cap pattern that has bitten this repo before.

**Failure scenario.** MAX_WCB_COUNT is lowered (or the arrays are resized independently) — e.g. to 12 for a memory reduction. `?CONTROLLER,ON,18` is still accepted by line 4868, `saveSpecialPeerIDToPreferences` still accepts it (WCB_Storage.cpp:424), and the subsequent `?CONTROLLER,OFF` reads `wcbPeerLearned[17]` and `WCBMacAddresses[17]` six entries past the end — an out-of-bounds read feeding a MAC address straight into `esp_now_del_peer()`. Conversely, raising MAX_WCB_COUNT leaves controller ids above 20 silently rejected while the rest of the mesh accepts them.

**Suggested fix.** Replace the literals with `MAX_WCB_COUNT` at 4868/4869/4889 and at WCB_Storage.cpp:424-425, and range-check `WCB_SPECIAL_PEER_ID` on load in `loadSpecialPeerIDFromPreferences()` (WCB_Storage.cpp:415-419) the way `loadMeshChannelFromPreferences()` clamps (WCB_Storage.cpp:457).

### F-299 · S4 · `Code/WCB/WCB.ino:5506`

**clearSerialLabelCommand() and verifyBackupChecksum() are orphaned handlers with no call site**

*Category:* `dead-code`

Two complete command handlers in this range are never invoked. `grep -rn 'clearSerialLabelCommand|verifyBackupChecksum' Code/` returns only their definitions.

1. `clearSerialLabelCommand()` (WCB.ino:5506-5527) parses `?SLCSx` properly — length guard, `S` prefix check, and a 1-5 port range check at :5521 — but the live `?SLC` dispatch at WCB.ino:5280-5281 bypasses it entirely with an inline `clearSerialLabel(message.substring(4).toInt())`. That inline form has no port validation (it relies on `clearSerialLabel`'s own `if (port < 1 || port > 5) return;` at WCB_Storage.cpp:1516) and silently no-ops on the exact spelling the help gives — `Code/WCB/WCB_Help.cpp:160` says `"?SLCSx  - Clear label (e.g. ?SLC1)"`, and `?SLC1` yields `substring(4) == ""` → port 0 → silent return. (The help-example half of this is already logged as F-032 in docs/CODE_REVIEW_2026-08.md:474; the orphaned function is not.)

2. `verifyBackupChecksum()` (WCB.ino:5792-5803) is written for a `?CHK<crc>` token (`message.substring(3)` skips "CHK"), but there is no `CHK` case in the dispatcher — `grep 'rootUpper == "CHK"'` over WCB.ino returns nothing, and the only "CHK" match is `etmCmdUpper == "CHKSM"` at :4705. A `?CHK…` token that reaches `processLocalCommand` therefore falls through to :5346 "Unknown command".

**Failure scenario.** No runtime failure from the dead functions themselves. The consequence is maintenance drift: `clearSerialLabelCommand` contains the validation the live path lacks, so anyone reading it concludes `?SLC` is bounds-checked when it is not; and `verifyBackupChecksum`'s presence makes it look as though a stray `?CHK` token is handled, which is exactly the gap that lets the custom-FUNCCHAR checksum token print "Unknown command" instead of being recognised.

**Suggested fix.** Either wire them up — dispatch `?SLC` through `clearSerialLabelCommand(message)` at :5281 (passing the full token, matching how `?SBI`/`?SBO` are dispatched at :5283/:5286), and add a `if (rootUpper.startsWith("CHK")) { verifyBackupChecksum(rootCmd); return; }` case — or delete both.

### F-300 · S4 · `Code/WCB/WCB.ino:5655`

**updateSerialBaudRate() and the "baud" branch of updateSerialSettings() are unreachable dead code**

*Category:* `dead-code`

`updateSerialSettings()` (WCB.ino:5644) has exactly one caller: WCB.ino:5343-5344, reached only when `message.startsWith("s") || message.startsWith("S")`. Its second branch at :5655 is `} else if (message.startsWith("baud") || message.startsWith("BAUD")) { updateSerialBaudRate(message.substring(4)); return; }` — a string cannot both start with 's'/'S' and with "baud"/"BAUD", so the branch can never be taken.

That is also the only call site of `updateSerialBaudRate()` (WCB.ino:5668-5691), so that whole function is dead. `grep -rn 'updateSerialSettings|updateSerialBaudRate' Code/` returns only the definitions plus WCB.ino:5344 and :5656.

The reason is that the `?BAUDSx,yyyyy` form it was written for is intercepted earlier: WCB.ino:5336-5342 handles `message.startsWith("bauds")` inline (`updateBaudRate(message.substring(5, commaPos).toInt(), ...)`), and the modern `?BAUD,Sx,rate` form is handled at :4555-4566. Note the two live paths differ from the dead one: `updateSerialBaudRate` range-checks the port at :5684 before calling `updateBaudRate`, whereas :5341 does not (harmless today only because `updateBaudRate` re-checks at WCB_Storage.cpp:151).

**Failure scenario.** No runtime failure — this is unreachable code. It matters because it looks like the live `?BAUDS` implementation: a future fix applied to `updateSerialBaudRate` (e.g. adding a baud whitelist, or changing the port parse) has no effect on any real command, and the maintainer only finds out on the bench.

**Suggested fix.** Delete `updateSerialBaudRate()` (WCB.ino:5668-5691) and the unreachable `else if` at :5655-5657, or — if the intent was for `?BAUDSx,yyyyy` to route through it — remove the inline handler at :5336-5342 and let it fall through to `updateSerialSettings`, which would then need its own `startsWith("bauds")` test placed before the port/state parse.

### F-301 · S4 · `Code/WCB/WCB.ino:5766`

**Comment at printBackupConfig cites "the verifier at WCB.ino:5121"; the real verifiers are at 1953 and 6440, and 5121 is now the ?HW handler**

*Category:* `doc-drift`

WCB.ino:5765-5768 reads:

    // Format as zero-padded 8 hex chars (%08X) — matches the verifier at
    // WCB.ino:5121 which always uses sprintf %08X. The previous String(x,HEX)
    // form stripped leading zeros, so any CRC with a leading zero nibble
    // (~1/16 chance) failed verification through the ?C* legacy path.

Line 5121 in this build is `if (rootUpper == "HW") {` — the hardware-version handler, which has nothing to do with checksums. The two actual verifiers are `parseCommandsAndEnqueue` at WCB.ino:1953-1982 (`snprintf(calcCrcBuf, sizeof(calcCrcBuf), "%08X", ...)` at :1981) and the `?C*` legacy path in `handleSerialData` at WCB.ino:6440-6460 (`sprintf(calculatedChecksumStr, "%08X", ...)` at :6460).

The same drift appears on the other side of the pair: WCB.ino:1974-1977 says the writer is at "WCB.ino:~4707" (actually :5770-5776) and again points at "the legacy verifier at WCB.ino:5121" (actually :6440). All three citations are stale.

The substance of the comments — that the %08X form must match on both sides, and why — is correct and worth keeping; only the line references are wrong.

**Failure scenario.** No runtime effect. A maintainer changing the checksum format follows the pointer to WCB.ino:5121, lands in the `?HW` handler, concludes the comment is nonsense, and either updates only one of the two verifiers or skips checking them — reintroducing the leading-zero mismatch the comment exists to prevent.

**Suggested fix.** Repoint the citations by name rather than line number, which is what keeps them true across edits: at :5765-5768 say "matches the verifiers in parseCommandsAndEnqueue() and handleSerialData()'s ?C* path, both of which use %08X"; likewise replace "WCB.ino:~4707" / "WCB.ino:5121" at :1974-1977 with "printBackupConfig()" and "handleSerialData()'s ?C* path".

### F-302 · S4 · `Code/WCB/WCB.ino:6750`

**The NeoPixel init success check reads back the driver's own RAM buffer, so it can never fail - the retry loop and its parameters are dead**

*Category:* `dead-code`

```
WCB.ino:6745-6752
    statusLED->setPixelColor(0, statusLED->Color(5, 0, 0));
    statusLED->show();
    delay(100);

    // Check if the LED actually changed
    if (statusLED->getPixelColor(0) != 0) {
      initialized = true;
```
`Adafruit_NeoPixel::getPixelColor()` reads the library's internal `pixels[]` array - the same RAM that `setPixelColor()` just wrote. It performs no I/O and has no knowledge of whether the RMT transfer or the physical LED worked. `Color(5,0,0)` is the non-zero constant 0x050000, so the comparison is true on every attempt in which the pixel buffer exists (allocated in the constructor via `updateLength()`, before `begin()` is even called).

Consequently `initialized` is set on attempt 1 in every realistic case: the `while (attempt < maxRetries && !initialized)` loop (6728) never iterates twice, `delayBetweenMs` (6756) is never used, and the "Failed to initialize NeoPixel after retries" path (6762-6765) - together with the caller-side handling at WCB.ino:7176-7180 ("NeoPixel initialization failed - board will function without status LED") - is unreachable except on a heap-allocation failure inside the constructor. The comment "Check if the LED actually changed" asserts a hardware verification the code does not perform.

**Failure scenario.** The status LED is wired to a pin with no LED attached, or the RMT peripheral fails to drive it (the exact cold-boot scenario the retry loop was written for - see the boot-guard rationale at WCB.ino:6769-6786). `initStatusLEDWithRetry(5, 100)` reports success on the first attempt and prints "NeoPixel initialized successfully" (7180), so the operator gets a positive boot banner for a dead LED and the five configured retries never happen.

**Suggested fix.** Either drop the retry loop, `maxRetries` and `delayBetweenMs` and the unreachable failure banner (documenting that NeoPixel init cannot be verified in software), or replace the check with something that can actually fail - e.g. verify `statusLED->numPixels() != 0` / the `getPixels()` pointer immediately after construction and treat that as the only detectable failure - and correct the comment so it no longer claims the LED state was read back.

### F-303 · S4 · `docs/WCB_KYBER_PASSTHROUGH_PARAMS.md:25`

**docs/WCB_KYBER_PASSTHROUGH_PARAMS.md still documents the >64-byte silent drop that WCB.ino:4275/4289 now flushes instead**

*Category:* `doc-drift`

The doc states, for the local-serial -> mesh direction (target ID 98 = `WCB_TARGET_KYBER`, WCB.ino:123):

- line 22: `| Poll cadence | source port is drained every ~1 ms; <=1 frame per poll |`
- lines 24-27: "**Design to a 64-byte effective MTU.** If more than 64 bytes arrive at the source UART between two 1 ms polls, the excess is **read and silently discarded** (see 5)."
- line 60: "**64-byte batch cap** — > 64 bytes arriving at the source port between two 1 ms polls: excess dropped, no error."

That is no longer true of the Kyber-source forwarder. WCB.ino:4275 and WCB.ino:4289 now flush a full buffer and keep going:

```
4275            if (remoteLen >= (int)sizeof(remoteBuf)) { sendESPNowRaw(remoteBuf, remoteLen); remoteLen = 0; }
4289      if (remoteLen >= (int)sizeof(remoteBuf)) { sendESPNowRaw(remoteBuf, remoteLen); remoteLen = 0; }
```

with the comment at 4272-4273 recording exactly why the drop was removed. So `forwardDataFromKyber` now emits MULTIPLE frames per poll and drops nothing, contradicting lines 22, 24-27 and 60. The drop text is still accurate for `forwardMaestroDataToLocalKyber` (4323) and `forwardMaestroDataToRemoteKyber` (4342) — which share the same target ID 98 and are described by the same doc — so the page is now right about one direction and wrong about the other, with no way for a reader to tell which. The page also has no Revision log.

**Failure scenario.** An integrator writing the droidnet side reads 'design to a 64-byte effective MTU, excess is silently discarded' and builds chunking/pacing around a 64 B/ms cap that the Kyber->mesh direction no longer imposes, while assuming the Maestro->mesh return direction is equally safe — where the drop is still real (see the forwardMaestroDataToRemoteKyber finding). The doc misdirects in both directions at once.

**Suggested fix.** Split section 1/5 by direction: Kyber-source (`forwardDataFromKyber`, WCB.ino:4275/4289) flushes a full 64-byte buffer and may emit several frames per poll — no drop; Maestro-source (`forwardMaestroDataToLocalKyber` 4323 / `forwardMaestroDataToRemoteKyber` 4342) still drops past 64 per poll. Update line 22's '<=1 frame per poll'. Add a Revision log per docs conventions (this page has none).

---

# Part 2 — the six scopes that were killed in session 3

Run 2026-08-18 19:32, **6/6 agents completed**. Scopes: `seg02-tables`,
**`seg07-rxcallback`**, `seg10-routing`, `seg12-setup-loop`, `lens-bounds`, `lens-deadcode`.
With this, **wave 1 is scope-complete (15/15)**.

> **STATUS: ALL UNVERIFIED** — reviewers-only run by design.

**Totals:** 59 findings — S1: 4 · S2: 15 · S3: 18 · S4: 22


## S1 (part 2)

### F-304 · S1 · `Code/WCB/WCB.ino:577`

**etmCharPhaseSentTimes[3][200] is written at index etmCharMessageCount*peerCount-1, which reaches 3799**

*Category:* `bounds`

`unsigned long etmCharPhaseSentTimes[3][200];  // [phase][msgIndex]` (WCB.ino:577) is indexed by `etmCharPhaseMessageIndex`, whose only bound is `totalMessages`:

  WCB.ino:1456  `int totalMessages = etmCharMessageCount * peerCount;`
  WCB.ino:1461  `if (etmCharPhaseMessageIndex < totalMessages) {`
  WCB.ino:1470  `etmCharPhaseSentTimes[etmCharPhase - 1][etmCharPhaseMessageIndex] = now;`
  WCB.ino:1506/1513 (phase 3)  `etmCharPhaseSentTimes[2][msgIdx] = now;`

`peerCount` is the number of ONLINE active peers and is counted over the full range (WCB.ino:1445-1449, `int peers[MAX_WCB_COUNT]`, so up to 19). `etmCharMessageCount` is operator-settable to 200 (`etmCharMessageCount = constrain(etmVal.toInt(), 10, 200);` WCB.ino:4696, and again at :5303) and is persisted to NVS. So `totalMessages` ranges up to 200*19 = 3800, against a second dimension of 200.

That the 200 is the real intended ceiling is confirmed by the READ side, which guards it: `if (phase >= 0 && mIdx >= 0 && mIdx < 200)` (WCB.ino:1249). Only the write side is unguarded.

Indices 200-599 corrupt the other two phase rows inside the array; indices >=600 write `unsigned long` values straight past the end of a 2400-byte .bss object. The run is startable locally (`?ETM,CHAR`, WCB.ino:4704 / `etmchar` :5311) and REMOTELY by a relay (PACKET_TYPE_ETM_REQ → `startETMChar()` at WCB.ino:3246), so the Wizard's remote ETM-characterization button reaches it.

**Failure scenario.** Fleet with 11+ online peers (this repo's own history references WCB19, so >10 boards is a real configuration) at the default `etmCharMessageCount = 20`: totalMessages = 220 > 200, so phase-1 writes spill into the phase-2/3 rows (garbage latencies) and, at 12+ peers with a raised count, past the array entirely. Explicit case: `?ETM,COUNT,200` then `?ETM,CHAR` with 3 online peers → totalMessages = 600, indices 600..? — every index >= 600 writes 4 bytes of a millis() timestamp outside etmCharPhaseSentTimes into whatever the linker placed next in .bss, corrupting unrelated globals until the phase timeout ends the run.

**Suggested fix.** Bound the index at the write sites, e.g. `if (etmCharPhaseMessageIndex < 200) etmCharPhaseSentTimes[...] = now;`, and/or clamp `totalMessages` to 200 in processETMChar(); alternatively size the array `[3][200 * MAX_WCB_COUNT]` or make the per-phase cap `etmCharMessageCount * peerCount <= 200` at startETMChar() and refuse/scale the run otherwise.

### F-305 · S1 · `Code/WCB/WCB.ino:1470`

**etmCharPhaseSentTimes[3][200] is indexed by etmCharMessageCount × peerCount with no bound check — global out-of-bounds write**

*Category:* `bounds`

`unsigned long etmCharPhaseSentTimes[3][200];  // [phase][msgIndex]` (WCB.ino:577) is written at three sites using `etmCharPhaseMessageIndex` / `msgIdx`, which run from 0 up to `totalMessages`:

  WCB.ino:1456  `int totalMessages = etmCharMessageCount * peerCount;`
  WCB.ino:1461  `if (etmCharPhaseMessageIndex < totalMessages) {`
  WCB.ino:1470  `etmCharPhaseSentTimes[etmCharPhase - 1][etmCharPhaseMessageIndex] = now;`
  WCB.ino:1506  `etmCharPhaseSentTimes[2][msgIdx] = now;`
  WCB.ino:1513  `etmCharPhaseSentTimes[2][msgIdx] = now;`

`peerCount` is built by walking all MAX_WCB_COUNT (20) slots (WCB.ino:1445-1450) with no cap, and `etmCharMessageCount` is user-settable to 10..200 (`constrain(etmVal.toInt(), 10, 200)` at WCB.ino:4696 and WCB.ino:5303, default 20 at WCB.ino:269). Nothing anywhere clamps the product to 200.

The asymmetry is the tell: the READ side is guarded — `if (phase >= 0 && mIdx >= 0 && mIdx < 200)` at WCB.ino:1249 and `if (mIdx < 0 || mIdx >= 200) return;  // guard against out-of-bounds sentTime read` at WCB.ino:1569 — but no equivalent guard exists on any of the three write sites. The 200 ceiling is an 8-board-era sizing (see the sibling stale comment `// [phase 0-2][board index 0-7]` at WCB.ino:575) that was never revisited when MAX_WCB_COUNT became 20.

The array is 3×200×8 = 4800 bytes of .bss; the write is an 8-byte store at `base + phase*1600 + idx*8`, so overshoot lands in adjacent globals (`etmCharPhaseMessageIndex`, `etmCharPhaseStartTime`, `etmLoadRunning`, `trackCommandDelivery`, `serialMonitorEnabled[]`, `blockBroadcastFrom[]`, the `preferences` object, the SERIALn_*_PIN ints — WCB.ino:578-615).

**Failure scenario.** Operator runs `?ETM,CHAR` on a fleet with 11 or more online peers at the default `etmCharMessageCount = 20`: totalMessages = 220, and phase 1 writes etmCharPhaseSentTimes[0][200..219] — 160 bytes past the row. Worse, `?ETM,COUNT,200` with only 2 online peers gives totalMessages = 400, walking indices 200..399 (1600 bytes past). Phase 3 does the same via etmCharPhaseSentTimes[2][msgIdx], where phase 2's row is already the end of the array, so overrun leaves the array entirely and corrupts unrelated globals — silently changed pin numbers, flipped broadcast-block flags, or a Preferences object clobbered mid-run.

**Suggested fix.** Add the same guard the read side already has to all three write sites, e.g. `if (etmCharPhaseMessageIndex < 200) etmCharPhaseSentTimes[etmCharPhase-1][etmCharPhaseMessageIndex] = now;`, and clamp `totalMessages` in processETMChar: `int totalMessages = min(etmCharMessageCount * peerCount, 200);`. Alternatively size the array `[3][ETM_CHAR_MAX_MSGS]` with `ETM_CHAR_MAX_MSGS = 200` and assert the product against it at startETMChar().

### F-306 · S1 · `Code/WCB/WCB.ino:1470`

**processETMChar() writes etmCharPhaseSentTimes[3][200] with an index bounded only by etmCharMessageCount * peerCount**

*Category:* `bounds`

`unsigned long etmCharPhaseSentTimes[3][200];` (WCB.ino:577) is indexed by `etmCharPhaseMessageIndex`, whose only ceiling is `int totalMessages = etmCharMessageCount * peerCount;` (WCB.ino:1456). `peerCount` is built from every active+online peer (WCB.ino:1446-1449, up to MAX_WCB_COUNT=20) and `etmCharMessageCount` is user-settable to 200 (`etmCharMessageCount = constrain(etmVal.toInt(), 10, 200)`, WCB.ino:4696; same at :5303). Nothing clamps the product to 200.

Three unguarded writes:
  1470: `etmCharPhaseSentTimes[etmCharPhase - 1][etmCharPhaseMessageIndex] = now;`
  1506: `etmCharPhaseSentTimes[2][msgIdx] = now;`
  1513: `etmCharPhaseSentTimes[2][msgIdx] = now;`
All are inside `if (etmCharPhaseMessageIndex < totalMessages)`, i.e. gated on totalMessages, not on 200.

For phases 1/2 the overflow spills into the next row (corrupting other phases' timings); for phase 3 (row index 2, lines 1506/1513) it writes past the end of the 2400-byte array entirely, into whatever BSS follows — `etmCharPhaseMessageIndex`, `etmCharPhaseStartTime`, `etmCharLastSendTime` are declared immediately after (WCB.ino:578-580), so the test can also corrupt its own loop counter.

The READ side already knows the bound — WCB.ino:1568 has `if (mIdx < 0 || mIdx >= 200) return;  // guard against out-of-bounds sentTime read` and WCB.ino:1249 repeats it — so the 200 limit was understood and simply never applied to the writes. A side effect of that read guard is that the test can also never complete for totalMessages > 200: ACKs for mIdx >= 200 are discarded, `totalAcked` never reaches `totalMessages`, and every phase ends by timeout.

**Failure scenario.** Operator runs `?ETM,COUNT,200` (explicitly inside the documented/constrained range) on a board with 2 online peers, then `?ETM,CHAR` (or a relay sends PACKET_TYPE_ETM_REQ, WCB.ino:3243). totalMessages = 400. Phase 3 writes etmCharPhaseSentTimes[2][200..399] — 200 unsigned longs (800 bytes) past the end of the array, silently corrupting adjacent globals. With stock settings (COUNT=20) the same corruption happens on any fleet with 11 or more online peers (20*11 = 220).

**Suggested fix.** Add a compile-time-known cap and clamp against it in one place, e.g. `static const int ETMCHAR_MAX_MSGS = 200;` then `int totalMessages = min(etmCharMessageCount * peerCount, ETMCHAR_MAX_MSGS);` at WCB.ino:1456 (and guard each write with `if (idx < ETMCHAR_MAX_MSGS)`). Alternatively size the array from `MAX_WCB_COUNT * 200`, but the clamp is cheaper — 200 samples per phase is already plenty for a latency characterisation.

### F-307 · S1 · `Code/WCB/WCB.ino:6490`

**processSerialCommandHelper calls parseCommandGroups()/stopTimerSequence() from serialCommandTask while loop() is iterating the same std::vector**

*Category:* `concurrency`

`processSerialCommandHelper()` calls `parseCommandGroups(data)` (WCB.ino:6490) and `stopTimerSequence()` (WCB.ino:6431). Both mutate the global `std::vector<CommandGroup> commandGroups` — `parseCommandGroups` does `commandGroups.clear()` then `push_back` (command_timer.cpp:84, :121, :171), `stopTimerSequence` does `commandGroups.clear()` (command_timer.cpp:54).

The only caller of `processSerialCommandHelper` is `processIncomingSerial` (WCB.ino:6411), and the only caller of `processIncomingSerial` is `serialCommandTask` (WCB.ino:6598-6621), created as its own FreeRTOS task at WCB.ino:7439 (`xTaskCreatePinnedToCore(serialCommandTask, "Serial Command Task", 4096, NULL, 1, NULL, 1)`). It is NOT loop().

Meanwhile `loop()` calls `processCommandGroups()` at WCB.ino:7481, which takes a live reference into the vector — `CommandGroup &group = commandGroups[currentGroupIndex];` (command_timer.cpp:180) — then iterates `for (const String &cmd : group.commands)` and **yields inside that loop** with `vTaskDelay(pdMS_TO_TICKS(1))` (command_timer.cpp:203). Both tasks are priority 1 pinned to core 1, so they round-robin and the explicit yield guarantees the interleave is reachable.

There is no mutex/critical section anywhere around `commandGroups` — the only portMUX in the firmware guards the ETM ACK queue (WCB.ino:1113).

The comment that authorises this direct call is factually wrong about the code. WCB.ino:390-399 states: "It must NOT be called from the ESP-NOW WiFi-task callback because loop() concurrently iterates the same vector via processCommandGroups() — a vector resize concurrent with iteration is undefined behavior. ... **The local-serial entry point and SEQ playback both run inside loop() already, so they keep calling parseCommandGroups() directly.**" SEQ playback (WCB_Storage.cpp:615) really is on loop(), but the local-serial entry point is not — it is on serialCommandTask. The deferral queue (`enqueuePendingTimerChain`) that the ETM path (WCB.ino:3992) and the normal-receive path (WCB.ino:4223) use to dodge exactly this hazard is never used by the serial path.

**Failure scenario.** A timer sequence is running (`;t500^;S1A^;t500^;S1B` typed earlier, or a stored sequence containing `;t`). loop() enters `processCommandGroups()`, binds `CommandGroup &group = commandGroups[0]`, starts the `for (const String &cmd : group.commands)` loop and hits `vTaskDelay(pdMS_TO_TICKS(1))` after the first command. serialCommandTask is scheduled, reads a line from USB/S1-S5 — either `?STOP` (6430 -> stopTimerSequence -> commandGroups.clear()) or any new `;t…` chain (6489 -> parseCommandGroups -> clear() + push_back). The vector's element storage is freed/reallocated and every `String` in `group.commands` is destructed. loop() resumes and dereferences the dangling `group` / `cmd` references -> reads freed heap, `parseCommandsAndEnqueue(cmd, 0)` runs on a destroyed String -> LoadProhibited panic or silent heap corruption. Same window exists whenever a mesh-triggered stored sequence is running and the operator types anything at the console.

**Suggested fix.** Route the serial path through the existing deferral queue instead of calling into the timer state directly: replace `parseCommandGroups(data)` at 6490 with `enqueuePendingTimerChain(data)` (it already snapshots `lastReceivedViaESPNOW`/`inSequenceBody`, and `drainPendingTimerChains()` runs before `processCommandGroups()` in loop()), and replace the direct `stopTimerSequence()` at 6431 with a flag/queued request drained in loop(). Then fix the stale comment at WCB.ino:398-399, which no longer describes any caller.


## S2 (part 2)

### F-308 · S2 · `Code/WCB/WCB_Storage.cpp:2229`

**etmMissedHeartbeats initializer of 5 is overwritten at boot by an NVS fallback of 3, so the documented 55 s offline window is never in effect**

*Category:* `logic`

SCOPE NOTE: I traced this from the ETM offline sweep that my range feeds (`offlineThresholdMs = (etmHeartbeatSec + 1) * etmMissedHeartbeats * 1000UL`, WCB.ino:1040, consuming the boardTable/BoardStatus state declared at WCB.ino:444-452). The two lines that carry the defect sit just outside my assigned 440-1015 window; reporting anyway because it silently reverts a deliberate, commented fix.

WCB.ino:267 declares `int etmMissedHeartbeats = 5;` with the comment "(etmHeartbeatSec+1)*5 = 55s offline window — widened from 3/33s so a few lost broadcast heartbeats on a busy mesh don't flap a live board (mirrors WCBClient _missedBeforeOffline)".

But setup() calls `loadETMSettings()` (WCB.ino:7416), which does `etmMissedHeartbeats = preferences.getInt("etmMiss", 3);` (WCB_Storage.cpp:2229) — the fallback is still the OLD 3. The neighbouring declaration shows the author is aware of this hazard and keeps the two in sync elsewhere: `bool etmEnabled = true;   // NVS default is also true — overwritten at boot by loadETMSettings()` (WCB.ino:264).

So a board with no saved etm_config (fresh flash, ?ERASE,NVS) boots with 3, not 5; and any board that saved ETM settings before or after the change also has 3 stored, which saveETMSettings() then re-pins. The widened window the comment describes is not reachable by any normal path.

**Failure scenario.** Fresh-flashed board for this release, ETM defaults: the offline threshold computed at WCB.ino:1040 is (10+1)*3*1000 = 33 s, not the intended 55 s. Three consecutive lost broadcast heartbeats on a busy mesh (exactly the flap the comment says was fixed) mark a live peer OFFLINE, which removes it from expectAckFrom in etmAddToPendingTable (WCB.ino:1204) and from capability routing via wcbPeerOnline() (WCB.ino:6879) until its next heartbeat lands.

**Suggested fix.** Change the NVS fallback to 5: `etmMissedHeartbeats = preferences.getInt("etmMiss", 5);` (WCB_Storage.cpp:2229). Consider a one-time migration (if the stored value is exactly the old 3, rewrite it to 5) since existing boards have 3 persisted, and add the same "NVS default is also N" note next to WCB.ino:267 that etmEnabled already carries.

### F-309 · S2 · `Code/WCB/WCB.ino:267`

**etmMissedHeartbeats initializer of 5 is dead — loadETMSettings() overwrites it with an NVS default of 3, contradicting the comment on the same line**

*Category:* `doc-drift`

WCB.ino:267 reads:
  `int etmMissedHeartbeats = 5;   // (etmHeartbeatSec+1)*5 = 55s offline window — widened from 3/33s so a few lost broadcast heartbeats on a busy mesh don't flap a live board (mirrors WCBClient _missedBeforeOffline)`
The comment records a deliberate change (33 s window flapped live boards; widen to 55 s). But setup() calls `loadETMSettings()` (WCB.ino:7416), and that function does `etmMissedHeartbeats = preferences.getInt("etmMiss", 3);` (WCB_Storage.cpp:2229). On any board that has never stored an etmMiss value the RAM initializer is discarded and 3 is restored — exactly the value the comment says was widened away from. Worse, the moment ANY ?ETM,* command runs, saveETMSettings() (WCB_Storage.cpp:2216) persists the loaded 3, so the 5 can never appear again.

Every other ETM default agrees between the two files (etmEnabled true/true, etmBootHeartbeatSec 2/2, etmHeartbeatSec 10/10, etmTimeoutMs 500/500, etmCharMessageCount 20/20, etmCharDelayMs 100/100, etmChecksumEnabled true/true) — and WCB.ino:264 even carries an explicit "NVS default is also true — overwritten at boot by loadETMSettings()" note. etmMissedHeartbeats is the single drifted pair, so this is an oversight, not a deliberate override.

Effective behaviour today: offlineThresholdMs = (10+1) * 3 * 1000 = 33 s, not the documented 55 s.

**Failure scenario.** A fresh board (or any board flashed and configured after an ?ERASE,NVS) boots with etmMissedHeartbeats = 3. On a busy mesh, three consecutive lost broadcast heartbeats — the exact scenario the comment says the widening was for — mark a live peer OFFLINE at 33 s. etmAddToPendingTable then skips that peer for ACK tracking (WCB.ino:1204) and processETMAcksAndRetries cancels its in-flight retries (WCB.ino:1292-1296), so unicasts to a board that is actually up are dropped un-retried.

**Suggested fix.** Change the NVS default at WCB_Storage.cpp:2229 to 5 to match the intent recorded at WCB.ino:267 (`preferences.getInt("etmMiss", 5)`), and consider migrating an existing stored 3 forward. If 3 is in fact the intended value, delete the misleading 5 and the comment at WCB.ino:267 instead — but the two must be made to agree.

### F-310 · S2 · `Code/WCB/WCB.ino:1011`

**scheduleNextHeartbeat computes a wrapped negative interval when etmHeartbeatSec <= 0, causing a heartbeat broadcast every loop iteration**

*Category:* `wraparound`

WCB.ino:1011:

  `intervalMs = (unsigned long)random((etmHeartbeatSec - 1) * 1000UL, (etmHeartbeatSec + 1) * 1000UL);`

`etmHeartbeatSec` is a plain `int` set with NO validation from two command paths and persisted to NVS: `etmHeartbeatSec = etmVal.toInt(); saveETMSettings();` (WCB.ino:4684) and `etmHeartbeatSec = message.substring(5).toInt(); saveETMSettings();` (WCB.ino:5321). loadETMSettings() reads it straight back (`WCB_Storage.cpp:2228`).

With etmHeartbeatSec = 0: `(0 - 1) * 1000UL` promotes the int -1 to unsigned long, giving 4294966296UL, which converts to `long` = -1000 for the `random(long, long)` parameter. The core's implementation is `if (howsmall >= howbig) return howsmall; long diff = howbig - howsmall; return random(diff) + howsmall;` (esp32 core 3.3.4, WMath.cpp:64-70), so this is `random(2000) - 1000` → uniform over [-1000, 999]. About half the draws are negative. `(unsigned long)(-500)` = 4294966796, and `etmNextHeartbeatMs = millis() + intervalMs` (WCB.ino:1013) wraps to millis()-500, i.e. a deadline in the PAST. The caller `if (millis() >= etmNextHeartbeatMs) { sendETMHeartbeat(); scheduleNextHeartbeat(false); }` (WCB.ino:1034-1037) therefore fires again on the very next loop pass, and re-draws another ~50% chance of a past deadline.

A negative value (`?ETM,HB,-1`) is worse: howsmall = -2000, howbig = 0, so EVERY draw is negative and the flood is unconditional.

**Failure scenario.** Operator types `?ETM,HB,0` intending "disable heartbeats" (or restores a config/backup carrying 0 or a negative). The board immediately broadcasts a 252-byte PACKET_TYPE_HEARTBEAT from loop() at loop rate instead of every ~10 s, saturating its ESP-NOW TX queue and the shared channel; its own ETM commands, WDP adverts and ACKs are starved and the rest of the fleet sees degraded delivery. The value is written to NVS by saveETMSettings(), so the storm returns on every reboot until someone knows to set ?ETM,HB back to a positive value.

**Suggested fix.** Validate at the two setters (e.g. `etmHeartbeatSec = constrain(etmVal.toInt(), 1, 3600);`, matching how ?ETM,COUNT already uses constrain at WCB.ino:4696) and defensively clamp inside scheduleNextHeartbeat: `long lo = (long)max(1, etmHeartbeatSec - 1) * 1000; long hi = (long)(etmHeartbeatSec + 1) * 1000;` using signed arithmetic throughout.

### F-311 · S2 · `Code/WCB/WCB.ino:1473`

**ETM characterization phase 2 is labelled "Broadcast (no load)" but sends unicast — the broadcast measurement never happens**

*Category:* `logic`

processETMChar handles phases 1 and 2 in one branch:

  WCB.ino:1458  `if (etmCharPhase == 1 || etmCharPhase == 2) {`
  WCB.ino:1459  `    unsigned long interDelay = (etmCharPhase == 1) ? 20UL : (unsigned long)etmCharDelayMs;`
  WCB.ino:1463  `        int peerIdx = etmCharPhaseMessageIndex % peerCount;`
  WCB.ino:1464  `        int targetWCB = peers[peerIdx];`
  WCB.ino:1473  `        sendESPNowMessage(targetWCB, payload.c_str(), true);`

`targetWCB` is always a real peer id from `peers[]` (populated 1..MAX_WCB_COUNT at WCB.ino:1447), never 0, so phase 2 is a plain unicast round-robin — byte-for-byte the same traffic as phase 1, differing only in the inter-message delay. Compare phase 3, which does broadcast properly via `sendESPNowMessage(0, payload.c_str(), true)` at WCB.ino:1511.

But both the console output and the comment claim otherwise:

  WCB.ino:1547  `Serial.printf("Phase 2 - Broadcast (no load)...\n");`
  WCB.ino:1548  `// Phase 2 is actually broadcast to all — re-init results for phase 2`
  WCB.ino:1549  `// We'll send to broadcast but track per-board via ACKs`

and the results table hard-labels it:

  WCB.ino:1795-1799  `const char* phaseNames[] = { "Individual Baseline (unicast, no load)", "Broadcast (no load)", "Loaded Network..." };`

So `?ETM,CHAR` reports a "Broadcast" row whose numbers were measured with unicast. (Note the repo CLAUDE.md cites this very comment — "send to broadcast but track per-board via ACKs" — as documentation of broadcast ACK fan-out; the comment describes a code path that does not broadcast.)

**Failure scenario.** Operator runs `?ETM,CHAR` to size `?ETM,TIMEOUT` for a mesh where broadcast is the dominant traffic (PWM passthrough, Kyber, `;C` sequence fan-out). The "Phase 2 - Broadcast (no load)" row shows unicast latency/loss, which is materially better than real broadcast behaviour, and the tool's "Recommended ETM timeout" (WCB.ino:1826) is derived from it — producing a timeout tuned too tight for the traffic pattern the operator was trying to characterize.

**Suggested fix.** Either implement phase 2 as it is documented — split it out of the phase-1 branch and send `sendESPNowMessage(0, payload.c_str(), true)` once per message while crediting `.sent++` to every peer (the pattern already written for phase 3 at WCB.ino:1508-1511) — or, if unicast-with-a-different-delay is the intended measurement, rename it in `phaseNames[1]` (WCB.ino:1797) and at WCB.ino:1547 and delete the misleading comment at 1548-1549.

### F-312 · S2 · `Code/WCB/WCB.ino:3706`

**?ETM,CHAR phase 3 never loads the network: the ETMLOAD trigger is sent non-ETM and is dropped by the ETM-mismatch filter on every peer**

*Category:* `logic`

The ETM-mismatch guard drops any non-ETM packet received while ETM is on unless it matches one of four whitelists: `if (!isRawPacket && !isPWMPacket && !isMaestroPacket && !isRcJsonPacket) { ... return; }` (WCB.ino:3706-3710), where the tests are built at 3692-3704 from `peek.structTargetID` / `peek.structCommand[0..1]`.

Phase 3 of ETM characterization broadcasts its load trigger without ETM: `sendESPNowMessage(0, "ETMLOAD", false);` (WCB.ino:1495). Evaluate the whitelist for that payload: `peekTarget = atoi("0") = 0` so not WCB_TARGET_RAW_SERIAL (97) or WCB_TARGET_KYBER (98) and `structTargetID[0] != 'K'` → isRawPacket false; `structCommand[0] == 'E'` → isPWMPacket false and isRcJsonPacket false; `structCommand[1] == 'T'` → isMaestroPacket false. The packet is dropped.

The only receiver-side consumer of ETMLOAD is in the NON-ETM path, WCB.ino:4175-4182 (`etmLoadRunning = true`), which is unreachable for this packet. The ETM path deliberately excludes it — WCB.ino:3893 `if (!etmCmd.startsWith("ETMCHAR_") && !etmCmd.startsWith("ETMLOAD"))` — and nothing else handles it, so even an ETM-wrapped ETMLOAD would do nothing.

Crucially, this can never be a "only when ETM is off" case: `startETMChar()` refuses to run unless ETM is enabled (WCB.ino:1405-1408, "ETM is not enabled. Use ?ETMON first."), and ETM must be set fleet-wide. So the load trigger is dropped by definition on every run.

**Failure scenario.** User runs `?ETM,CHAR` on a 3-board mesh. Phases 1 and 2 measure correctly. Phase 3 prints "Triggering network load on all peers..." (WCB.ino:1494) and broadcasts ETMLOAD; every peer drops it at WCB.ino:3706-3710, so no peer ever sets etmLoadRunning and processETMLoad() (WCB.ino:1585) never runs anywhere. Phase 3 then measures an idle network and the report presents those numbers as latency/loss "under load" — the user tunes etmTimeoutMs against a load figure that was never generated.

**Suggested fix.** Send the trigger over ETM (`sendESPNowMessage(0, "ETMLOAD", true)`) and add an ETMLOAD arm in the ETM COMMAND branch, or add `"ETMLOAD"`/`"ETMCHAR_"` to the whitelist at WCB.ino:3692-3706. Either way the two ETM-characterization control payloads must survive the ETM-mismatch filter, since characterization only ever runs with ETM on.

### F-313 · S2 · `Code/WCB/WCB.ino:3831`

**etmSeqHistory is never cleared when a peer reboots, so the first commands from a restarted board are ACKed but silently not executed**

*Category:* `logic`

Duplicate suppression keeps an 8-deep per-sender ring: `for (int h = 0; h < ETM_SEQ_HISTORY; h++) if (etmSeqHistory[senderIdx][h] == rxSeq) { isDuplicate = true; break; }` (WCB.ino:3831-3833), written at 3838-3840. `ETM_SEQ_HISTORY` is 8 (WCB.ino:558) and `etmSeqHistory[MAX_WCB_COUNT][8]` is zero-initialised at WCB.ino:559.

The sender's counter is RAM-only and restarts from zero on every boot: `uint16_t etmSequenceCounter = 0;` (WCB.ino:522), `etmMsg.structSequenceNumber = ++etmSequenceCounter;` (WCB.ino:2304) — so the first command after any reboot is seq 1, the second seq 2, and so on.

Nothing ever clears the receiver's ring. `grep etmSeqHistory` yields only the declaration (559) and the read/write inside this branch (3832, 3838, 3840). The receiver DOES observe the peer's restart — PACKET_TYPE_ETM_BOOT is handled at WCB.ino:3768 and 3789-3795 — but that branch only prints and returns; it does not reset `etmSeqHistory[senderIdx]` / `etmSeqHistoryIdx[senderIdx]`.

Consequence: if a sender had transmitted N ≤ 8 ETM commands before rebooting, its ring entries {1..N} are still present on every receiver, so its first N commands after the reboot match and take the `isDuplicate` path at 3843 — ACKed at 3823 (so the sender records success and stops retrying) but never dispatched. This is strictly worse than `main`, which kept a single last-seq slot (`etmLastReceivedSeq[senderIdx] == seq`) and could only false-positive on one value.

**Failure scenario.** Bench sequence with two boards: WCB2 fires three ETM commands at WCB1 (seqs 1,2,3 land in WCB1's ring). WCB2 is then updated over the air (this branch's headline feature) or rebooted from the Wizard; WCB1 stays up. WCB2 now sends seq 1, 2, 3 again — WCB1 ACKs all three and executes none. The Wizard/sender reports delivery success; the servos/lights do nothing. Only the 4th post-reboot command works.

**Suggested fix.** In the ETM_BOOT branch (WCB.ino:3789-3795, or right where isBootAnnounce is computed at 3768), clear the sender's dedup state: `memset(etmSeqHistory[senderIdx], 0, sizeof(etmSeqHistory[senderIdx])); etmSeqHistoryIdx[senderIdx] = 0;` — the boot announce is exactly the "this sender's sequence counter restarted" signal.

### F-314 · S2 · `Code/WCB/WCB.ino:3983`

**?WHOAMI reply is sent as a 249-byte non-ETM packet that WCB_Client (NaviCore) routes to the raw hook and never delivers**

*Category:* `protocol`

The ETM COMMAND branch answers `?WHOAMI` with `sendESPNowMessage((uint8_t)senderWCB, reply.c_str(), false)` (WCB.ino:3983). `useETM=false` skips the ETM send path at WCB.ino:2280 (`if (etmEnabled && useETM && !isPWMMessage)`) and falls into the NORMAL SEND PATH at WCB.ino:2329-2346, which builds a 249-byte `espnow_struct_message` (40+4+4+1+200).

The only requester is NaviCore, a `WCB_Client` host. `WCB_Client::_handleReceive` (WCBClient/src/WCB_Client.cpp) routes strictly by size: `if (len != (int)sizeof(wcb_packet_etm_t)) { if (_maybeHandleSeqFrag(...)) return; if (_rawPacketCallback) _rawPacketCallback(mac, data, len); return; }`. `wcb_packet_etm_t` is 252 bytes (WCB_Client.h:37 "All packets use wcb_packet_etm_t (252 bytes, packed)"). A 249-byte frame therefore never reaches the `switch (pkt->structPacketType)` and never reaches `_commandCallback`. NaviCore registers `wcb->onRawPacket(naviota::otaRawPacketHook)` (NaviCore.ino:4213), which only accepts the 55/243-byte OTA structs, so the reply is dropped.

The adjacent comment at WCB.ino:3966-3968 states the reply is "Sent back as a normal COMMAND packet (the only type NaviCore's WCB_Client ingests)" — the code no longer sends a COMMAND packet at all. `main` sent this with `useETM=true` (252-byte COMMAND); this branch changed it to `false`, which is what breaks it.

WCB->WCB still works by luck: the ETM-mismatch whitelist at WCB.ino:3704 lets '{'-prefixed non-ETM payloads through. Only the client path is broken — and the client path is the only caller (NaviCore rc_telemetry.h:1774 `wcb->send((uint8_t)i, "?WHOAMI")`).

**Failure scenario.** NaviCore (device 20) polls WCB status; rc_telemetry.h:1774 unicasts "?WHOAMI" to each online WCB. Each WCB builds {"type":"wcb_alias","id":N,"alias":"..."} and sends it non-ETM. NaviCore's WCB_Client discards it on the size check, `rcTelemetry::handle()` never sees it, and no alias is ever cached. Per rc_telemetry.h:1748 ("?WHOAMI is re-sent each status poll only until a board's alias is cached") the query re-fires on every status poll forever: WCB boards show unnamed in NaviCore's panel and the mesh carries a permanent ?WHOAMI/reply burst that can never terminate.

**Suggested fix.** Send the reply as an ETM COMMAND again (`sendESPNowMessage((uint8_t)senderWCB, reply.c_str(), true)`), or — if keeping it off the WiFi task's pending-table path is the goal — enqueue the reply for loop() and send it with ETM there. Update the comment either way.

### F-315 · S2 · `Code/WCB/WCB.ino:4684`

**?ETM,HB with no value silently stores a 0-second heartbeat interval and turns the board into an ESP-NOW broadcast storm**

*Category:* `logic`

The ?ETM parser builds `String etmVal = (secondComma == -1) ? "" : args.substring(secondComma + 1);` (WCB.ino:4669), then the value setters call `.toInt()` with no validation and immediately persist:
  4680: `etmTimeoutMs = etmVal.toInt();  saveETMSettings();`
  4684: `etmHeartbeatSec = etmVal.toInt();  saveETMSettings();`
  4688: `etmMissedHeartbeats = etmVal.toInt();  saveETMSettings();`
  4692: `etmBootHeartbeatSec = etmVal.toInt();  saveETMSettings();`
A bare `?ETM,HB` (the natural way to *query* the setting — there is no query form) yields etmVal == "", `toInt()` returns 0, and 0 is written to NVS (`preferences.putInt("etmHB", etmHeartbeatSec)`, WCB_Storage.cpp:2215). loadETMSettings() does not clamp on read either (WCB_Storage.cpp:2227), so it survives reboot. The legacy form `?ETMHB` has the same hole (WCB.ino:5320, `message.substring(5).toInt()`).

With etmHeartbeatSec == 0, scheduleNextHeartbeat(false) at WCB.ino:1011 evaluates
  `random((etmHeartbeatSec - 1) * 1000UL, (etmHeartbeatSec + 1) * 1000UL)`
`(0 - 1)` is int -1; `-1 * 1000UL` promotes to unsigned long and wraps to 4294966296UL, which Arduino's `long random(long,long)` sees as -1000. So the result is a value in [-1000, 1000), assigned to `unsigned long intervalMs`. Any negative draw becomes ~4.29e9, and `etmNextHeartbeatMs = millis() + intervalMs` (WCB.ino:1013) wraps to a timestamp in the PAST. The gate at WCB.ino:1034 is a plain `if (millis() >= etmNextHeartbeatMs)`, so sendETMHeartbeat() then fires on every loop() iteration — a continuous ESP-NOW broadcast flood that saturates the radio and starves ACKs/adverts/OTA.

etmMissedHeartbeats == 0 is equally damaging by a different route: `offlineThresholdMs = (unsigned long)(etmHeartbeatSec + 1) * etmMissedHeartbeats * 1000UL` (WCB.ino:1039) becomes 0, so every peer is marked OFFLINE on the first sweep after each packet. Note ?ETM,COUNT at :4696 IS constrained — the omission on the other four is an inconsistency, not a deliberate design.

**Failure scenario.** Operator types `?ETM,HB` expecting to read back the current heartbeat interval. Firmware prints "ETM heartbeat set to 0 sec", writes 0 to NVS, and from the next scheduleNextHeartbeat() onward broadcasts an ETM heartbeat every loop iteration (hundreds per second), permanently, across reboots, until someone notices and runs `?ETM,HB,10`. The whole mesh degrades because the flooding board monopolises the channel.

**Suggested fix.** Validate before assigning, and treat an empty value as a query rather than a set. E.g. at 4680-4693: `if (etmVal.length() == 0) { Serial.printf("ETM heartbeat = %d sec\n", etmHeartbeatSec); return; }` then `int v = etmVal.toInt(); if (v < 1 || v > 120) { print usage; return; }`. Apply matching ranges to TIMEOUT (>=50), MISS (>=1), BOOT (>=1), DELAY (>=1), and clamp the same values in loadETMSettings() so an already-corrupted NVS blob self-heals.

### F-316 · S2 · `Code/WCB/WCB.ino:5279`

**Legacy ?SLSx,label is double-stripped and can never succeed — the label-set command is dead**

*Category:* `dead-code`

The dispatcher strips the "SLS" prefix before calling, but the callee strips it again:

  WCB.ino:5278  `} else if (message.startsWith("sls") || message.startsWith("SLS")) {`
  WCB.ino:5279  `    updateSerialLabel(message.substring(3));`

and inside `updateSerialLabel` (WCB.ino:5467) the parameter is treated as the FULL token:

  WCB.ino:5474  `// Check for 'S' prefix after SL`
  WCB.ino:5475  `if (!message.startsWith("S") && !message.startsWith("s")) {`
  WCB.ino:5487  `String portStr = message.substring(3, commaPos);  // Skip "SLS"`

For `?SLS1,Marcduino`, processLocalCommand receives "SLS1,Marcduino" and passes `substring(3)` = "1,Marcduino". That does not start with 'S', so line 5475 fires and the function returns at 5477 having done nothing but print "Invalid format". There is no input that reaches line 5487, because any string starting with 'S' after the caller's strip (e.g. "?SLSS1,x" → "S1,x") then produces `substring(3, 2)`, which Arduino's String swaps to ",", giving port 0 and the "Invalid port" return at 5491.

This is exactly the double-strip class the adjacent ?SBI/?SBO handlers were fixed for — those callers now pass the FULL string with an explicit comment (WCB.ino:5285 `// pass FULL "SBISxON" — the fn strips "SBI" itself (was double-stripped here → whole command was dead)`). ?SLS was not fixed.

The form is still advertised by the built-in help: `Code/WCB/WCB_Help.cpp:159` prints `?SLSx,label   - Set label  (e.g. ?SLS1,Marcduino)` — the exact syntax the code rejects.

**Failure scenario.** A user (or an older config chain / a script following the on-board `?LABEL ?` help) sends `?SLS1,Marcduino`. The board answers "Invalid format. Use: ?SLSx,label where x=1-5" and the label is never stored, for every possible port and label. The modern `?LABEL,S1,Marcduino` path still works, so the failure looks like a user typo rather than a firmware bug.

**Suggested fix.** Match the ?SBI/?SBO fix: change WCB.ino:5279 to `updateSerialLabel(message);` (pass the full "SLSx,label" token, as the function's own comment at 5468 and its substring(3,...) at 5487 assume), and add the same `// pass FULL — the fn strips "SLS" itself` note so it isn't re-broken.

### F-317 · S2 · `Code/WCB/WCB.ino:5279`

**?SLS<x>,<label> is completely dead — the caller strips "SLS" and updateSerialLabel() strips it again**

*Category:* `logic`

Dispatch at WCB.ino:5278-5279:
  `} else if (message.startsWith("sls") || message.startsWith("SLS")) {`
  `    updateSerialLabel(message.substring(3));`
But updateSerialLabel() (WCB.ino:5467) is written to receive the FULL token: it checks `if (!message.startsWith("S") && !message.startsWith("s"))` (WCB.ino:5473) against the 'S' of "SLS", and extracts the port with `String portStr = message.substring(3, commaPos);  // Skip "SLS"` (WCB.ino:5487). The caller has already removed those 3 characters, so the argument is double-stripped.

Trace `?SLS1,Marcduino` (the exact example in the help text, WCB_Help.cpp:159). processLocalCommand receives message = "SLS1,Marcduino"; the caller passes "1,Marcduino"; updateSerialLabel's startsWith("S") test fails and it prints "Invalid format. Use: ?SLSx,label where x=1-5" and returns. The command can never set a label.

The alternate spelling fails too: for "SLSS1,label" the caller passes "S1,label", commaPos == 2, and `substring(3, 2)` — Arduino's String::substring swaps left/right when left > right, yielding "," — so `portStr.toInt()` is 0 and it dies at the `port < 1 || port > 5` check (WCB.ino:5490).

This is the same defect that was already found and fixed for the sibling commands: WCB.ino:5283 and :5286 carry the comment "pass FULL \"SBISxON\" — the fn strips \"SBI\" itself (was double-stripped here → whole command was dead)". ?SLS was missed in that sweep. (The adjacent ?SLC path at WCB.ino:5281 uses `message.substring(4).toInt()` and is correct for the "SLCSx" form.)

**Failure scenario.** User (or a restored legacy backup) issues `?SLS1,Marcduino`. Board prints "Invalid format. Use: ?SLSx,label where x=1-5" and the label is never stored; serialPortLabels[0] stays empty and every subsequent ?config / getSerialLabel() output shows the port unlabelled. Only the newer `?LABEL,S1,Marcduino` path works.

**Suggested fix.** Pass the full token, matching the SBI/SBO fix: change WCB.ino:5279 to `updateSerialLabel(message);` (and keep the explanatory comment used at :5283/:5286). Verify with `?SLS1,Test` followed by `?LABEL` / `?config`.

### F-318 · S2 · `Code/WCB/WCB.ino:6125`

**Nested ;C / ;SEQ recalls have no depth or cycle guard — a self-referencing stored sequence loops forever inside loop()'s queue drain**

*Category:* `logic`

`recallStoredCommand` ends with `recallCommandSlot(key, sourceID);` (WCB.ino:6125) and carries no recursion depth counter, no in-progress key set, and no cycle check. The header comment at WCB.ino:6074-6077 explicitly blesses the nested case — "NESTED recalls (a `;Ckey` inside a running sequence body) ... run locally only ... That keeps a sub-sequence call (`;C` inside a body — a documented, supported pattern) from being re-broadcast N times" — so nesting is a supported feature, but nothing bounds it.

The cycle is closed and self-sustaining: `recallCommandSlot` (WCB_Storage.cpp:556) reads the body from NVS, sets `inSequenceBody = true` (WCB_Storage.cpp:613) and calls `parseCommandsAndEnqueue(stripped, sourceID)` (WCB_Storage.cpp:617), which pushes each token onto `commandQueue`. loop() drains that queue with `while (xQueueReceive(commandQueue, &inItem, 0) == pdTRUE) { ... handleSingleCommand(...) }` (WCB.ino:7502-7518). A `;C<key>` token in the body dispatches back to `recallStoredCommand`, which enqueues the body again. Because each drain iteration re-fills the queue, the `while` never observes an empty queue and **loop() never returns**.

The `inSequenceBody` flag suppresses the *mesh fan-out* (WCB.ino:6119) but does nothing to stop the local re-entry. Note also there is no task watchdog subscription in this firmware — the only `esp_task_wdt_reset()` is commented out (WCB.ino:6732) — so nothing recovers the board.

**Failure scenario.** Operator saves `?Cwave,;S1A^;Cwave` (a typo, or a sequence intended to be retriggered externally), or two sequences that call each other: `?Ca,;S1X^;Cb` and `?Cb,;S1Y^;Ca`. Triggering `;Cwave` once puts loop() into an unbounded drain: an NVS `preferences.getString` per iteration, a `Serial.print("Recall stored command request received:")` per iteration (WCB.ino:6086), and no exit. ETM heartbeats, `processETMAcksAndRetries`, `wdpTick`, `drainOtaPackets` and `checkOtaTimeout` — all called after the drain in loop() — never run again. The board goes offline to the mesh and stays that way until it is power-cycled; the stored sequence is still in NVS, so it re-arms the moment anyone triggers it again.

**Suggested fix.** Add a re-entry guard around the recall: a small static depth counter incremented in `recallStoredCommand` / decremented on exit with a hard cap (e.g. 4) plus a printed refusal, or a short static ring of currently-executing keys that rejects a key already in flight. A depth cap is enough to break both self-reference and mutual recursion while leaving genuine one-level sub-sequence calls working.

### F-319 · S2 · `Code/WCB/WCB.ino:6193`

**processPWMOutput will pinMode/drive ANY serial port's TX pin — it never checks the port is a declared PWM output, defeating canUsePWMOnPort's stated guarantee**

*Category:* `protocol`

`processPWMOutput` validates only the port number and pulse range:

```
    if (port < 1 || port > 5 || pulseWidth < 500 || pulseWidth > 2500) {
```
(WCB.ino:6193) and then unconditionally does `pinMode(txPin, OUTPUT); digitalWrite(txPin, HIGH); delayMicroseconds(pulseWidth); digitalWrite(txPin, LOW);` (WCB.ino:6207-6212).

It never calls `isSerialPortPWMOutput(port)` (WCB_PWM.cpp:766) nor `canUsePWMOnPort(port)` (WCB_PWM.cpp:55). Those are the reservation checks, and the comment on the latter states the invariant this path breaks: "Reject ports already claimed by a serial-device module (HCR/MP3/WLED). Symmetric with those modules' own guards, which reject PWM ports — **so two subsystems can never silently drive the same UART**" (WCB_PWM.cpp:68-70). The symmetry only exists for the *configuration* path (`addPWMOutputPort` -> `canUsePWMOnPort` -> `configureRemotePWMOutput`, WCB_PWM.cpp:791-819). The *runtime* `;P` path in my range bypasses it entirely.

`;P<port><width>` is not just a typed command — `processPWMPassthrough` on a *remote* board emits it at the passthrough rate: `snprintf(msg, sizeof(msg), ";P%d%lu", targetPort, pulseWidth); sendESPNowMessage(targetWCB, msg, false);` (WCB_PWM.cpp:723-725), with the target board/port taken from the *sender's* mapping. Nothing on the receiving board validates that its own S<port> is free.

On S1/S2 (hardware UART) Arduino-ESP32's `pinMode(pin, OUTPUT)` re-routes the pin's output signal to SIG_GPIO_OUT_IDX and sets the IO MUX to PIN_FUNC_GPIO, which detaches the UART TX matrix routing — the port stops transmitting until it is re-begun. On S3-S5 (EspSoftwareSerial, bit-banged) the pinMode is benign but the HIGH/delay/LOW in the middle of a software-serial frame corrupts the byte being clocked out.

**Failure scenario.** WCB1 has an RC input mapped to a remote output: `?MAP,PWM,S1,W2S3`. WCB2 has an HCR on S3 (`?HCR,S3:...`). WCB2's own `addPWMOutputPort(3)` — whether typed or self-configured from the WDP PWMTARGET TLV — is correctly refused by `canUsePWMOnPort` ("Cannot use PWM on Serial3 - reserved for HCR/MP3/WLED"). But WCB1 still streams `;P3<width>` ~50-200x/second over ESP-NOW; WCB2's receive path dispatches it to `processCommandCharcter` -> `processPWMOutput`, which pulses SERIAL3_TX_PIN into the middle of every HCR command frame. The HCR stops responding and the operator sees no error anywhere — the guard that was supposed to prevent this reported success. The same command typed by hand (`;P11500`) permanently detaches Serial1's hardware-UART TX until reboot, silently killing a Maestro or Kyber link on S1.

**Suggested fix.** Gate the pulse on the port actually being reserved for PWM output: `if (!isSerialPortPWMOutput(port)) { if (debugPWMEnabled) Serial.printf("[PWM] ;P%d ignored — S%d is not a PWM output port\n", port, port); return; }` before touching the pin. `configureRemotePWMOutput()` already does the one-time `pinMode`, so the per-pulse `pinMode` at 6208 can be dropped as well.

### F-320 · S2 · `Code/WCB/WCB.ino:6417`

**processIncomingSerial's line-assembly buffer has no maximum length — a port that never sends CR/LF grows a String until the heap is exhausted**

*Category:* `bounds`

The per-port assembly buffer is `static String serialBuffers[6]` (WCB.ino:6380) and the accumulation is unconditional:

```
    } else {
      serialBuffer += c;
    }
```
(WCB.ino:6416-6418)

There is no length cap, no overflow truncation, and no discard-until-delimiter recovery. The buffer is only emptied when a `\r` or `\n` arrives (WCB.ino:6385-6414). The enclosing `while (serial.available())` (WCB.ino:6382) has no byte budget either, so a continuously streaming port is drained without limit.

The ports that are guaranteed to produce non-line-oriented bytes are excluded — MP3 (6366), DFPlayer (6368-6370), HCR (6374) and a port mid-Maestro-get-query (6378, 6383) all return early. But a *configured local Maestro port is not excluded* outside the narrow query window, and neither is any unconnected/floating S1-S5 RX pin, nor a WLED port (`isSerialPortUsedForWLED` exists at WCB_WLED.cpp:68 but is not consulted here), nor any third-party device wired to S1-S5 that speaks a binary or NUL-terminated protocol.

Arduino-ESP32's `String::concat` reallocates in 16-byte steps, so this is also a heavy heap-fragmentation path long before the allocation actually fails.

**Failure scenario.** A Pololu Maestro is configured on S1 (`?MAESTRO,M1:W<self>S1:115200`). The Maestro emits binary reply/echo bytes outside the `maestroQueryPort` window (a script doing `serial_send_byte`, or a get-query reply that arrives after `maestroQueryPort` is cleared). None of those bytes are 0x0D/0x0A, so `serialBuffers[1]` grows monotonically at line rate. At 115200 baud that is ~11 KB/s: within ~20 s of intermittent chatter the String has consumed the free heap in 16-byte realloc steps. Observable results, in order: ESP-NOW/WiFi allocations start failing, `enqueueCommand`'s malloc fails and prints "Out of memory while enqueuing command!" (WCB.ino:1927), and the board stops responding to mesh traffic. The same happens with any floating S3-S5 RX pin picking up noise, or any wired device whose protocol has no line terminator.

**Suggested fix.** Cap the assembly buffer, e.g. `if (serialBuffer.length() >= SERIAL_LINE_MAX) { serialBuffer = ""; /* optionally warn once */ }` before the `+=`, with SERIAL_LINE_MAX sized to the real maximum command (the ETM struct caps a command at ~200 chars, 188 with CHKSM, so 512 is generous). Discarding to the next delimiter on overflow is preferable to truncating, so a partial line is never executed as a command.

### F-321 · S2 · `Code/WCB/WCB.ino:7214`

**migrateOldStoredCommands() runs 200 lines before loadCommandDelimiter(), so the one-shot, irreversible sequence-comment rewrite uses the default '^' instead of the operator's configured delimiter**

*Category:* `logic`

setup() runs the one-time legacy-sequence migration early:

  7214  migrateOldStoredCommands();       // One-time recovery of pre-key_list sequences

but does not load the operator's command delimiter from NVS until near the end of setup():

  7413  // Load additional config
  7414  loadCommandDelimiter();

`migrateOldStoredCommands()` (WCB_Storage.cpp:874-938) rewrites every recovered sequence through `normaliseSeqComments(value)` at :892 and :918, and that helper is parameterised on the delimiter:

  WCB_Storage.cpp:850  static String normaliseSeqComments(const String &value) {
  :851      char   d   = commandDelimiter;          // typically '^'
  :853      String ddl = dl + dl;                   // "^^"
  :857      result.replace(ddl + cmt, placeholder);  // protect ^^***
  :858      result.replace(dl  + cmt, cmt);          // ^***  →  ***

`commandDelimiter` is defined `char commandDelimiter = '^';` (WCB.ino:151) and is genuinely operator-settable and persisted: `setCommandDelimiter()` (WCB_Storage.cpp:536-541) writes it to the command_config namespace and is reached from WCB.ino:5036 and WCB.ino:5419; `loadCommandDelimiter()` (WCB_Storage.cpp:526-533) restores it. At :7214 that restore has not happened, so `commandDelimiter` is still the hard-coded '^' regardless of what the board is configured for.

The migration then marks itself permanently done — `preferences.putBool("seq_mig_done", true)` at WCB_Storage.cpp:926, with the early-out at :880 — and the rewritten values are what `saveStoredCommandsToPreferences()` persists (:895, :919). There is no second chance.

By contrast, setup() explicitly handles the analogous ordering hazard for Maestro slots (:7156-7158, `normalizeMaestroSelfSlots()` deliberately placed after loadWCBNumberFromPreferences), so the pattern is understood here — the delimiter case was just missed.

**Failure scenario.** An operator has set a non-default delimiter (e.g. ?DELIM,| -> WCB.ino:5419 -> setCommandDelimiter('|')) and has legacy CMDn sequences written by pre-key_list firmware, whose comments are marked '|***'. They flash this release. On first boot setup() calls migrateOldStoredCommands() at :7214 with commandDelimiter still '^'. normaliseSeqComments therefore (a) fails to convert any of the '|***' markers, so every recovered sequence executes its comment text as commands on recall, and (b) rewrites any literal '^***' that appears inside a sequence body to '***'. Both results are written back by saveStoredCommandsToPreferences and seq_mig_done is set, so the migration never re-runs and the damage is permanent. On a default-'^' board nothing is wrong, which is why this survives bench testing.

**Suggested fix.** Move `loadCommandDelimiter();` (and `loadLocalFunctionIdentifierAndCommandCharacter();`, same class of boot-time-consumed setting) up with the other NVS loads before `migrateOldStoredCommands()` at :7214 — e.g. next to loadWCBAlias()/loadWCBQuantitiesFromPreferences() around :7159 — leaving a comment naming the dependency the way :7156-7158 and :7161 already do. Do not just move the migration later: normaliseSeqComments is not the only delimiter consumer in the storage layer (recallCommandSlot, WCB_Storage.cpp:573/:586).

### F-322 · S2 · `Code/WCB/WCB.ino:7225`

**Serial1/Serial2 begin() has no baudRates>0 guard (Serial3-5 does) — a 0 in NVS blocks 20 s in the core's baud auto-detect and the boot guard reboots the board forever**

*Category:* `boot-failure`

setup() opens the two hardware UARTs straight from the NVS-loaded table with no validity check:

  7225  Serial1.begin(baudRates[0], SERIAL_8N1, SERIAL1_RX_PIN, SERIAL1_TX_PIN);
  7226  Serial2.begin(baudRates[1], SERIAL_8N1, SERIAL2_RX_PIN, SERIAL2_TX_PIN);
  (same unguarded calls again at :7229, :7232, :7239, :7244)

Thirteen lines later the SAME function guards the software serials against exactly the value it does not guard here:

  7250  // Serial3-5: Only initialize if NOT used for PWM AND baud rate is valid
  7252      if (baudRates[2] > 0) {
  7255      } else { Serial.println("Serial3 has invalid baud rate (0) - skipping UART init"); }

That guard exists because EspSoftwareSerial divides by the baud rate — `UARTBase::begin` does `m_bitTicks = (microsToTicks(1000000UL) + baud / 2) / baud;` (Arduino-Code/libraries/EspSoftwareSerial/src/SoftwareSerial.cpp:91) — so baud 0 is an IntegerDivideByZero panic. So the authors treat baudRates[i]==0 as a live possibility.

On the hardware UARTs the same 0 is worse, not better. `HardwareSerial::begin` (esp32 core 3.3.4, cores/esp32/HardwareSerial.cpp:428-448) treats baud 0 as "auto-detect the baud rate in place":

  428  if (!baud) {
  430    uartStartDetectBaudrate(_uart);
  433    while (millis() - startMillis < timeout_ms && !(detectedBaudRate = uartDetectBaudrate(_uart))) { yield(); }
  447      _uart = NULL;

`timeout_ms` defaults to 20000UL (HardwareSerial.h:313-316), and this call site does not pass it. So `Serial1.begin(0, ...)` blocks setup() for up to 20 seconds and then leaves the port permanently dead (`_uart = NULL`).

The boot guard armed at :7109 is a one-shot esp_timer with BOOT_GUARD_TIMEOUT_MS 10000 (:6787) that calls ESP.restart() from the esp_timer task (:6789) and is only cancelled at :7473. Line 7225 is reached roughly 3-3.5 s into setup (delay(1000) at :7121, the NVS loads, initStatusLEDWithRetry worst case ~1.5 s, delay(1000) at :7184), so the guard fires ~6.5 s into the 20 s detect loop, long before begin() returns.

Nothing validates the value on load: `loadBaudRatesFromPreferences()` is `baudRates[i] = preferences.getInt(key.c_str(), baudRates[i]);` (WCB_Storage.cpp:251) with no range check. `updateBaudRate()` whitelists on the write side (WCB_Storage.cpp:157-161), so today's firmware won't store a 0 — but a legacy blob, a partially-cleared namespace, or any future writer will, and the read path has no floor at all.

**Failure scenario.** A board whose serial_baud/Serial1 key holds 0 (written by older firmware, or a corrupted/partially-erased NVS blob) is flashed with this release. setup() reaches WCB.ino:7225 at t~3.5 s and calls Serial1.begin(0, ...). The core enters its 20 s baud-detection loop. At t=10 s the boot guard esp_timer fires ESP.restart(). The board reboots, does the same thing, and reboots again — an unrecoverable boot loop with no serial window to type ?BAUD,S1,9600 into (the banner at :7205 has printed, but the console is unusable and the command queue drain never starts). Recovery requires esptool erase_flash. The identical value on Serial3 is caught and reported cleanly three lines later.

**Suggested fix.** Apply the same guard to the hardware UARTs that :7252/:7262/:7272 apply to the software ones — wrap each Serial1/Serial2 begin in `if (baudRates[n] > 0)` with the matching "invalid baud rate (0)" message, or better, sanitise on load: in loadBaudRatesFromPreferences() (WCB_Storage.cpp:247-254) clamp any value that is not in updateBaudRate()'s whitelist back to 9600 and log it once. Sanitising at the source fixes both the S1/S2 brick and the S3-S5 silent-skip in one place.


## S3 (part 2)

### F-323 · S3 · `Code/WCB/WCB.ino:441`

**enqueuePendingTimerChain ignores xQueueSend failure — an overflowed timer chain is dropped with no counter and no log**

*Category:* `error-handling`

WCB.ino:434-442:

  `static inline void enqueuePendingTimerChain(const String& chain) {`
  `  if (!pendingTimerChainQueue) return;`
  `  ... strncpy/flag capture ...`
  `  xQueueSend(pendingTimerChainQueue, &slot, 0);`

The return value is discarded. The queue is 8 deep (`constexpr size_t TIMER_CHAIN_QUEUE_SLOTS = 8;`, WCB.ino:403) and is fed from the ESP-NOW receive path (the deferral exists precisely because parseCommandGroups must not run on the WiFi task — WCB.ino:389-399, and callers at WCB.ino:3992, :4223), then drained once per loop() pass by drainPendingTimerChains().

This is inconsistent with its own sibling in the same file: enqueueRcJsonRelay checks the result, increments `rcJsonRelayDrops` and prints, with the comment "a dropped CONFIG fragment silently fails the browser's reassembly, so we never want this to be invisible" (WCB.ino:372-380). A dropped timer chain is a whole `;t` command group that simply never runs, which is at least as invisible.

**Failure scenario.** A controller fans out a burst of timer-chain commands (or a `;C`/sequence recall expands into several chained groups) and more than 8 arrive between two loop() passes — easy while loop() is stalled on an NVS commit or a 20 ms-per-fragment config pull (`delay(20)` in the frag senders, WCB.ino:3035/3119). The 9th and later chains are discarded by xQueueSend with no trace: the servo/lighting sequence they encoded never plays and nothing on the serial console or in the stats says why.

**Suggested fix.** Mirror the RC-JSON path: `if (xQueueSend(pendingTimerChainQueue, &slot, 0) != pdTRUE) { pendingTimerChainDrops++; Serial.printf("[TIMER] chain queue FULL — dropped a chain (total=%lu)\n", ...); }`.

### F-324 · S3 · `Code/WCB/WCB.ino:522`

**ETM sequence counter wraps to 0, which the duplicate ring treats as its empty-slot sentinel; the comment claiming seq 0 is never sent is wrong**

*Category:* `wraparound`

WCB.ino:553-559 documents the dedup ring as:

  `// ... Seq 0 is never sent, so 0 = empty slot.`
  `static const int ETM_SEQ_HISTORY = 8;`
  `uint16_t etmSeqHistory[MAX_WCB_COUNT][ETM_SEQ_HISTORY] = {{0}};`

The receiver relies on that claim without excluding empty slots: `for (int h = 0; h < ETM_SEQ_HISTORY; h++) { if (etmSeqHistory[senderIdx][h] == rxSeq) { isDuplicate = true; break; } }` (WCB.ino:3831-3833). A rxSeq of 0 matches any not-yet-written slot.

But nothing prevents seq 0 from being sent. The counter is `uint16_t etmSequenceCounter = 0;` (WCB.ino:522) and the ONLY assignment into a packet is `etmMsg.structSequenceNumber = ++etmSequenceCounter;` (WCB.ino:2304) — no skip-zero on wrap. After 65535 ETM sends the 65536th carries seq 0. (grep confirms these are the only two references to etmSequenceCounter in the tree.)

When the match hits, the packet is ACKed first (`etmSendAck(...)`, WCB.ino:3823) and only then dropped as a duplicate (WCB.ino:3843-3850) — so the sender records a successful delivery for a command that never executed.

**Failure scenario.** Board A has been up long enough to issue 65536 ETM sends (a few commands/second over a convention day) and its counter rolls to 0. Board B reboots — after an OTA, say — and within the first 8 ETM commands it receives from A, A's seq-0 packet arrives. B's etmSeqHistory[A] still contains zeroed slots, so the command is classified as a duplicate: B sends the ACK, prints nothing (unless debugETM), and never executes it. A sees the ACK, clears the pending entry, and never retries. One ensured command is silently lost with a success indication on both ends.

**Suggested fix.** Skip zero on wrap where the sequence is assigned: `if (++etmSequenceCounter == 0) etmSequenceCounter = 1; etmMsg.structSequenceNumber = etmSequenceCounter;` (WCB.ino:2304), which makes the WCB.ino:557 comment true again. Belt-and-braces: also skip empty slots in the compare loop at WCB.ino:3832 (`if (etmSeqHistory[senderIdx][h] != 0 && ... == rxSeq)`).

### F-325 · S3 · `Code/WCB/WCB.ino:881`

**commandsToSend[10] and commandsToReceive[10] are never referenced — ~4.9 KB of static RAM permanently reserved**

*Category:* `dead-code`

  WCB.ino:881  `espnow_struct_message commandsToSend[10]; // Includes 1-9 and broadcast`
  WCB.ino:882  `espnow_struct_message commandsToReceive[10];`

Repo-wide grep over Code/ returns only these two definition lines — neither array is read, written, extern-declared, or mentioned in any WCB_*.cpp/.h. Every send builds its packet on the stack (`espnow_struct_message msg; memset(&msg, 0, sizeof(msg));` at WCB.ino:2340, 2377, 2458, 2497) and every receive does the same (`espnow_struct_message received;` at WCB.ino:4014).

`espnow_struct_message` is 40+4+4+1+200 = 249 bytes packed (the size the receive router keys on at WCB.ino:3679). 2 × 10 × 249 = 4980 bytes of .bss that is allocated at every boot and never touched.

The comment "Includes 1-9 and broadcast" also encodes the abandoned 9-board model, not the current MAX_WCB_COUNT of 20.

Related dead sizing in the same file: `BoardStatus` (WCB.ino:446-451) declares `messagesSent`, `messagesAckd`, `totalRetries`, `totalFailed`, none of which is ever read or written — the live per-board counters are the separate `etmStats*[MAX_WCB_COUNT]` arrays at WCB.ino:525-528. That is another 320 bytes across boardTable[20].

**Failure scenario.** Not a crash today, but ~5.3 KB of a ~320 KB DRAM budget is spent on nothing on every board — and this firmware already allocates aggressively at boot: rcJsonRelayQueue 64×220 ≈ 14 KB (WCB.ino:7146), commandQueue 200 × sizeof(CommandQueueItem) (WCB.ino:7189), etmPendingTable 10 × ~270 B, five task stacks of 4096 B each. On the classic ESP32 target with min_spiffs, a heap-exhaustion failure (the FATAL command-queue path at WCB.ino:7192) is exactly the kind of thing this reclaims headroom against.

**Suggested fix.** Delete WCB.ino:881-882, and drop the four never-used fields from `struct BoardStatus` (WCB.ino:448-451), leaving `online` and `lastSeenMs` which are the only ones the ETM sweep and printConfigInfo actually use.

### F-326 · S3 · `Code/WCB/WCB.ino:3651`

**The five config/stats/seq/ETM fragment handlers run inline on the WiFi task and emit up to ~2.9 KB of Serial output from the ESP-NOW receive callback**

*Category:* `concurrency`

The 230-byte fragment branch dispatches all five reassembly handlers inline:

```
3650  uint8_t ptype = ((const espnow_struct_config_frag*)incomingData)->packetType;
3651  if      (ptype == PACKET_TYPE_CONFIG_FRAG) handleConfigFragPacket(incomingData);
3652  else if (ptype == PACKET_TYPE_STATS_FRAG)  handleStatsFragPacket(incomingData);
3653  else if (ptype == PACKET_TYPE_SEQ_FRAG)    handleSeqFragPacket(incomingData);
3654  else if (ptype == PACKET_TYPE_SEQVAL_FRAG) handleSeqValFragPacket(incomingData);
3655  else if (ptype == PACKET_TYPE_ETM_FRAG)    handleETMFragPacket(incomingData);
```

On the completing fragment each one builds a String by concatenating up to MGMT_MAX_CHUNKS (16, WCB.ino:837) × CONFIG_PAYLOAD_SIZE (183, WCB.ino:252) = 2928 chars and writes it straight to Serial from the WiFi task: WCB.ino:3088, 3329, 3364, 3399, 3434.

This is the exact hazard the file documents at WCB.ino:308-322 — "ESP32's Serial (HWCDC or UART) is NOT atomic across cores — concurrent writes can interleave at byte granularity... Observed symptom: occasional `<{"type":"rc_ch"...}Sending Unicast...` concatenated lines" — and the reason `enqueueRcJsonRelay` exists. This branch applied that fix to the OTA ACK line (WCB_OTA.cpp:448-455 defers `[OTA:ACK,...]` via `otaRelayPrint`) and deferred the REQ packets for the same reason (WCB.ino:3632-3634: "DEFER to loop() (drainMgmtReqs) so we don't stall the WiFi task"), but the FRAG handlers were left inline.

`[MGMT:CONFIG,N]…`, `[MGMT:SEQ,N]…` and `[MGMT:SEQVAL,N]…` are the tokens the Wizard's serial parser splits on, so a byte-level interleave from Core 1 corrupts a whole config pull.

**Failure scenario.** Wizard pulls a remote board's config through a USB-tethered relay. The last CONFIG_FRAG arrives on Core 0; handleConfigFragPacket reassembles ~2.9 KB and calls Serial.printf at WCB.ino:3088. UART0's TX ring is 128 bytes, so at 115200 baud the WiFi task blocks roughly 250 ms inside the ESP-NOW receive callback — dropping inbound frames for that window (including any in-flight OTA ACK or the tail of a concurrent transfer). Meanwhile any Core-1 Serial write (a debug line, a heartbeat log, a [TERM:N] drain) interleaves into the middle of the [MGMT:CONFIG,2]… line and the Wizard's parse of that config fails with no diagnostic.

**Suggested fix.** Route the completed-reassembly output through the existing cross-core relay (`otaRelayPrint` / `enqueueRcJsonRelay`) so the print happens in drainRcJsonRelay() on Core 1, chunked to fit RC_JSON_MAX_LEN — or enqueue the raw fragments the way enqueueMgmtReq/enqueueWdpPacket already do and reassemble in loop().

### F-327 · S3 · `Code/WCB/WCB.ino:3697`

**ETM-mismatch whitelist treats any packet whose second command byte is 'M'/'m' as a Maestro packet, without requiring the ';' prefix**

*Category:* `logic`

The guard that lets deliberately-unwrapped traffic through while ETM is enabled builds its predicates like this:

```
3693  bool isPWMPacket     = (peek.structCommand[0] == ';' &&
3694                         (peek.structCommand[1] == 'P' || peek.structCommand[1] == 'p'));
3695  // Maestro script triggers are intentionally sent without ETM (fire-and-forget).
3696  // Allow them through regardless of local ETM mode.
3697  bool isMaestroPacket = (peek.structCommand[1] == 'M' || peek.structCommand[1] == 'm');
```

The PWM test correctly anchors on `structCommand[0] == ';'`; the Maestro test omits it. The comment says the intent is "Maestro script triggers", i.e. `;M…` — but as written the whitelist accepts any 249-byte non-ETM payload whose SECOND byte happens to be 'M' or 'm', with no constraint on the first.

That means a board with ETM ON accepts and executes arbitrary non-ETM commands from a board with ETM OFF whenever the payload's second character is M — for example `?MAESTRO,…` (clears/rewrites Maestro slot config), `?MGMT,…`, `?MP3,…`, or any label/alias command of that shape. Those all fall through to the NORMAL RECEIVE PATH at WCB.ino:4006 and are dispatched via parseCommandsAndEnqueue at WCB.ino:4227 exactly like a legitimate command.

This undermines the documented invariant that ETM is all-or-nothing across the fleet and that a mismatched board's traffic is ignored: the exception is far wider than the one case the comment describes.

**Failure scenario.** A board is re-flashed or restored from a backup with ETM off while the rest of the fleet has it on. Every `;`-command it sends is correctly dropped at WCB.ino:3706-3710 — so the user sees the expected "ETM mismatch" symptom and starts debugging — except its `?MAESTRO,...` and `?MP3,...` config commands, which sail through and are executed on every ETM-enabled peer. Config lands on boards the user believes are isolated from the mis-set board, producing a partial, confusing configuration state.

**Suggested fix.** Anchor the Maestro test the same way the PWM one is anchored: `bool isMaestroPacket = (peek.structCommand[0] == ';' && (peek.structCommand[1] == 'M' || peek.structCommand[1] == 'm'));`

### F-328 · S3 · `Code/WCB/WCB.ino:3852`

**ETM path builds a String from structCommand without forcing a terminator; it is safe only because PACKET_TYPE_COMMAND happens to be 0**

*Category:* `bounds`

The ETM branch does `String etmCmd = String(etmReceived.structCommand);` (WCB.ino:3852) with no NUL forcing. `structCommand` is `char[200]` (WCB.ino:209) with nothing on the wire guaranteeing a terminator.

The non-ETM path treats this as a real hazard and fixes it explicitly, with a comment saying why:

```
4169  // structCommand is a fixed-size field with no guaranteed NUL terminator (a
4170  // malicious/garbled in-group packet can fill all 200 bytes). Force a terminator
4171  // so String()'s strlen can't run off the end of the received stack struct.
4172  received.structCommand[sizeof(received.structCommand) - 1] = '\0';
```

The ETM struct has the identical exposure and did not get the same treatment. It does not overrun today, but only by coincidence: in the packed layout (WCB.ino:204-212) `structCommand` occupies offsets 49-248 and `structPacketType` sits at offset 249, and this branch is gated on `structPacketType == PACKET_TYPE_COMMAND`, which is 0 (WCB.ino:215). So the packet-type byte itself acts as the terminator and strlen stops at exactly 200. Change PACKET_TYPE_COMMAND to a non-zero value, reorder the struct, or add a field after structCommand, and a 200-byte-full packet walks strlen past the 252-byte stack copy on the WiFi task.

The asymmetry is the defect: the same hazard is defended in one branch of the same function and left to an accidental invariant in the other.

**Failure scenario.** A future change gives COMMAND a non-zero packet-type id (e.g. renumbering so 0 means "unset"), or inserts a field between structCommand and structPacketType. An in-group packet with all 200 command bytes non-NUL then makes String()'s strlen read past etmReceived on the WiFi task stack, producing an over-long String — heap growth at best, a stack read fault in the ESP-NOW receive callback at worst. Nothing in the build catches it because the guard is implicit.

**Suggested fix.** Mirror the non-ETM path: add `etmReceived.structCommand[sizeof(etmReceived.structCommand) - 1] = '\0';` immediately after the memcpy at WCB.ino:3717, alongside the existing structPassword termination at 3718.

### F-329 · S3 · `Code/WCB/WCB.ino:3852`

**ETM receive path builds a String from structCommand[200] without forcing a NUL, unlike the non-ETM path that documents why the NUL is required**

*Category:* `bounds`

WCB.ino:3852: `String etmCmd = String(etmReceived.structCommand);`
`etmReceived` is a stack copy of the 252-byte espnow_struct_message_etm made by `memcpy(&etmReceived, incomingData, sizeof(etmReceived))` at WCB.ino:3717. Only structPassword is force-terminated (WCB.ino:3719); structCommand[200] is not. A peer or an in-group attacker can fill all 200 bytes with non-NUL data, so `String(const char*)`'s strlen runs past the field.

It happens to stop safely today by pure coincidence of the layout and the branch: structCommand occupies offsets 49..248 and `uint8_t structPacketType` sits at offset 249; this code only runs inside `if (etmReceived.structPacketType == PACKET_TYPE_COMMAND)` (WCB.ino:3805) and PACKET_TYPE_COMMAND is defined as 0 (WCB.ino:213). So byte 249 is the terminator and the read stays inside the 252-byte stack object. Change PACKET_TYPE_COMMAND to any non-zero value, add a field between structCommand and structPacketType, or stringify structCommand from any other packet-type branch, and this becomes an out-of-bounds stack read of unbounded length.

The equivalent non-ETM path was hardened, with the reason spelled out — WCB.ino:4163-4166:
  `// structCommand is a fixed-size field with no guaranteed NUL terminator (a`
  `// malicious/garbled in-group packet can fill all 200 bytes). Force a terminator`
  `// so String()'s strlen can't run off the end of the received stack struct.`
  `received.structCommand[sizeof(received.structCommand) - 1] = '\0';`
The ETM branch is the one path that did not get the same one-line guard.

**Failure scenario.** Not exploitable as written (packetType 0 terminates the scan). It becomes a stack over-read the moment PACKET_TYPE_COMMAND is renumbered, a field is inserted before structPacketType in espnow_struct_message_etm, or a future packet type stringifies structCommand — none of which would look risky to whoever makes the change, because the same struct is safely stringified 300 lines away.

**Suggested fix.** Add the same guard the non-ETM path uses, right after the memcpy at WCB.ino:3717-3719: `etmReceived.structCommand[sizeof(etmReceived.structCommand) - 1] = '\0';` — and carry over the explanatory comment from WCB.ino:4163 so the constraint is recorded on both paths.

### F-330 · S3 · `Code/WCB/WCB.ino:4066`

**"No flush()" comments claim write() is asynchronous, but Serial3/4/5 are EspSoftwareSerial where write() bit-bangs synchronously and blocks the WiFi task for the whole transfer**

*Category:* `doc-drift`

The targeted-Kyber forward writes up to 196 bytes to an arbitrary port 1-5 straight from the receive callback and justifies dropping flush() like this:

```
4065  targetSerial.write((uint8_t *)(received.structCommand + 4), chunkLen);
4066  // No flush() — we're in the ESP-NOW receive callback (WiFi task). write()
4067  // queues into the UART TX buffer and it drains async; flush() would block
4068  // the WiFi task until the FIFO empties (~200ms on a SW UART), dropping
4069  // inbound ESP-NOW frames.
```

The twin comment is at WCB.ino:4131-4132 on the raw-serial path.

Both statements are false for ports 3-5. `getSerialStream()` (WCB.ino:1869-1878) returns Serial3/Serial4/Serial5, which are `SoftwareSerial` (WCB.ino:885-887). In EspSoftwareSerial, `UARTBase::write(const uint8_t*, size_t, Parity)` (Arduino-Code/libraries/EspSoftwareSerial/src/SoftwareSerial.cpp:348-433) bit-bangs every bit inline via `writePeriod()` before returning — there is no TX queue and no ISR drain. So the WiFi task blocks for the ENTIRE transmission whether or not flush() is called; removing flush() saves nothing on those ports.

The second claim is also inverted: `UARTBase::flush()` (SoftwareSerial.cpp:435-443) flushes the **RX** buffer (`if (!m_rxValid) return; m_buffer->flush();`) — it never waited on TX. The old `targetSerial.flush()` on S3-S5 was discarding received bytes, not stalling ~200 ms.

The comment matters because it is being used as the justification for doing this work inline in the ESP-NOW callback at all.

**Failure scenario.** A Kyber/raw-serial mapping targets Serial4 (SoftwareSerial) at 9600 baud. A 196-byte targeted-Kyber packet arrives; WCB.ino:4065 bit-bangs 196 bytes × 10 bits ÷ 9600 ≈ 204 ms inside espNowReceiveCallback on the WiFi task. For that entire window no ESP-NOW frame can be processed — concurrent ETM commands, heartbeats, config fragments and OTA ACKs are dropped, exactly the outcome the comment claims to be avoiding. At 57600 it is still ~34 ms per packet, repeated per chunk for a stream.

**Suggested fix.** Correct both comments to say the async-TX reasoning holds only for Serial1/Serial2 (HardwareSerial), and that flush() on S3-S5 flushed RX. If the WiFi-task stall matters, hand ports 3-5 off to a queue drained by a Core-1 task rather than writing them from the callback.

### F-331 · S3 · `Code/WCB/WCB.ino:4123`

**Raw-serial receiver rejects chunkLen > 177 but the firmware's own chunker emits 180-byte chunks**

*Category:* `protocol`

The raw-serial receive branch validates the wire-supplied length as:

```
4122  size_t chunkLen    = (uint8_t)received.structCommand[1] | ((uint8_t)received.structCommand[2] << 8);
4123  if (chunkLen > 177 || chunkLen == 0 || targetPort < 1 || targetPort > 5) { ... return; }
```

The matching transmitter, `sendESPNowRawSerial` (WCB.ino:2492-2510), chunks at 180: `if (chunkSize > 180) chunkSize = 180;` (WCB.ino:2497) and writes the payload at `structCommand + 3` (WCB.ino:2505). 3 + 180 = 183 fits inside structCommand[200], so those packets are well-formed and would be safe to accept — the receiver's bound is simply 3 bytes tighter than what its own sender produces (177 looks like `180 - header` applied to the wrong side).

Any chunk of 178, 179 or 180 bytes is therefore silently discarded with no log unless debugMaestro is on. The two sibling paths do not have this mismatch: Kyber broadcast sends ≤180 (WCB.ino:2375) and accepts ≤180 (WCB.ino:4077); targeted Kyber sends ≤180 (WCB.ino:2467) and accepts ≤196 (WCB.ino:4040).

Today this is latent rather than live: the only caller of sendESPNowRawSerial is WCB.ino:6700 inside RawSerialForwardingTask, whose buffer is `static uint8_t buffer[64]` (WCB.ino:6637), so len never exceeds 64. WCB_Client also clips to 177 (WCBClient/src/WCB_Client.cpp:566-567, "WCB firmware caps raw chunks at 177 bytes"). The bug fires the moment that 64-byte buffer is enlarged — which is the obvious performance tweak for this path.

**Failure scenario.** Someone raises RawSerialForwardingTask's buffer to 256 to cut per-chunk ESP-NOW overhead. A device on Serial2 emits a 200-byte burst; the task reads all 200, sendESPNowRawSerial splits it into 180 + 20. The remote board drops the 180-byte packet at WCB.ino:4123 and writes only the trailing 20 bytes to the target port — a silently truncated, corrupt stream with no error on either end.

**Suggested fix.** Make the two ends agree: either raise the receive bound to 197 (structCommand[200] minus the 3-byte header), or lower the sender's chunk cap at WCB.ino:2497 to 177. Ideally define one shared constant for the raw-serial payload size and use it in both places.

### F-332 · S3 · `Code/WCB/WCB.ino:4159`

**Non-ETM path sets lastReceivedViaESPNOW/inSequenceBody before checking whether the packet is even addressed to this board**

*Category:* `race`

In the normal receive path the per-command origin flags are assigned first and the addressing test comes after:

```
4159  lastReceivedViaESPNOW = true;
4160  inSequenceBody = false;
4161  colorWipeStatus("ES", green, 200);
4162
4163  if (targetWCB != 0 && targetWCB != WCB_Number) {
4164    if (debugEnabled) Serial.println("Message not for this WCB, ignoring.");
4166    return;
```

So every non-ETM unicast on the mesh addressed to some *other* board still flips this board's globals before being discarded. The ETM branch gets the order right — it returns on `targetWCB != 0 && targetWCB != WCB_Number` at WCB.ino:3806, long before setting the flags at 3883-3884.

These globals are cross-core state. This callback runs on the WiFi task (Core 0); `serialCommandTask` is pinned to Core 1 (WCB.ino:7439), as are KyberLocal/KyberRemote/RawSerialForwarding. `enqueueCommand` snapshots the live global at enqueue time (`item.espnowOrigin = lastReceivedViaESPNOW;`, WCB.ino:1920) and `handleSingleCommand` restores it at dispatch (WCB.ino:7510) — but the *live* global is then read again during execution by `sendESPNowMessage` (`if (target == 0 && lastReceivedViaESPNOW) return;`, WCB.ino:2275), by the routing short-circuit at WCB.ino:5829, by the recall fan-out gate at WCB.ino:6119, and by the debug print at WCB.ino:6357. A Core-0 write at 4159 lands inside those Core-1 reads with no mux, queue, or volatile.

**Failure scenario.** Board WCB1 is USB-tethered and the user types a broadcast command (e.g. a `;`-verb that fans out to the mesh). While handleSingleCommand runs on Core 1 with the correct snapshot restored (lastReceivedViaESPNOW=false), a Kyber/raw-serial or plain command packet addressed to WCB3 arrives on Core 0. Line 4159 sets lastReceivedViaESPNOW=true before line 4163 discards it. The in-flight local command reaches sendESPNowMessage(0, ...) at WCB.ino:2275, sees the flag true, and returns immediately — the broadcast is silently never transmitted, with no error and no log. On a busy mesh (heartbeats + adverts + PWM/Kyber traffic) this window is hit routinely.

**Suggested fix.** Move the `targetWCB != 0 && targetWCB != WCB_Number` check above the two assignments, matching the ETM path's order (WCB.ino:3806 before 3883). Longer term the two globals want the same treatment the ACK path got (etmEnqueueAck) — set them only on the task that consumes them.

### F-333 · S3 · `Code/WCB/WCB.ino:4342`

**forwardMaestroDataToRemoteKyber() silently drops Maestro reply bytes past 64, the exact bug already fixed in forwardDataFromKyber()**

*Category:* `logic`

forwardMaestroDataToRemoteKyber() (WCB.ino:4330) accumulates bytes read from every locally-configured Maestro port into one shared 64-byte buffer:
  4342: `if (remoteLen < (int)sizeof(remoteBuf)) remoteBuf[remoteLen++] = b;`
  4346: `if (remoteLen > 0) sendESPNowRaw(remoteBuf, remoteLen);`
The read loop at 4339-4343 drains `while (maestroSerial.available() > 0)` for EACH of MAX_MAESTROS_PER_WCB slots before the single send, but the guard discards every byte after the 64th instead of flushing. There is no bounds violation — the bytes are simply lost, producing a truncated frame on the wire.

The sibling function forwardDataFromKyber() had this same shape and it was deliberately fixed; the comment at WCB.ino:4270-4273 states the reasoning explicitly: "If the buffer is full, FLUSH and keep going — don't silently drop bytes past 64, which truncated a large Maestro burst into a corrupt frame", implemented as `if (remoteLen >= (int)sizeof(remoteBuf)) { sendESPNowRaw(remoteBuf, remoteLen); remoteLen = 0; }` at WCB.ino:4275 and :4289. forwardMaestroDataToRemoteKyber() never got that treatment.

KyberRemoteTask calls this every 1 ms (WCB.ino:6588-6591), so a steady trickle is fine — but any stall of the task (an NVS commit in loop(), an OTA flash write, a config push) lets the UART RX buffer accumulate, and the next pass drains the whole backlog into a 64-byte window. sendESPNowRaw() itself already chunks at 180 bytes per packet (WCB.ino:2372), so the 64-byte ceiling is not a wire constraint.

**Failure scenario.** Board is in ?MAESTRO,REMOTE mode with a Maestro on S1 at 115200. loop() stalls ~30 ms during an NVS commit from a Wizard config push; ~300 bytes of Maestro response accumulate in the UART RX buffer. The next forwardMaestroDataToRemoteKyber() pass reads all ~300 bytes, keeps the first 64, and discards the rest. The remote Kyber/controller receives a truncated Pololu frame and mis-parses or hangs waiting for the remainder.

**Suggested fix.** Mirror the fix already present at WCB.ino:4275/4289: replace line 4342 with `if (remoteLen >= (int)sizeof(remoteBuf)) { sendESPNowRaw(remoteBuf, remoteLen); remoteLen = 0; } remoteBuf[remoteLen++] = b;` and keep the trailing flush at 4346.

### F-334 · S3 · `Code/WCB/WCB.ino:6264`

**Serial-monitor mapping treats a destination of W<own number> as remote and unicasts it to itself, silently losing the data — the raw path handles the same case as local**

*Category:* `logic`

The line-mode mapping fan-out classifies a destination purely on `destWCB == 0`:

```
            if (destWCB == 0) {
                // Local destination
```
(WCB.ino:6264). Anything else goes to the ESP-NOW branch at 6273-6280.

`addSerialMonitorMapping` accepts a board number equal to this board's own: the only bound is `if (wcbNum > MAX_WCB_COUNT || serialPort < 0 || serialPort > 5)` (WCB_Storage.cpp:1717) — there is no `wcbNum != WCB_Number` check and no normalisation to 0.

The raw-mode path for the *same* mapping table gets this right and documents that the case is real: `if (output.wcbNumber == 0 || output.wcbNumber == WCB_Number) { // Local output (either explicitly local or targeting self)` (WCB.ino:6688-6689), with a debug line "Mapping target W%dS%d is local - forwarding directly".

So the identical mapping behaves differently depending on whether the `R` (raw) suffix was used, and only the line-mode half is broken. `sendESPNowMessage` will look up `WCBMacAddresses[target - 1]` (WCB.ino:2313) — this board's own MAC, which is not a registered ESP-NOW peer — and the send fails; under ETM it also consumes a pending-table slot and burns the 3-retry budget first (`etmAddToPendingTable`, WCB.ino:2311).

**Failure scenario.** On WCB2 the operator (or the Wizard, which writes destinations in the fully-qualified `W<n>S<p>` form) creates `?SMS3,W2S1` — mirror S3 to S1 on this same board. Raw mode (`?SMS3R,W2S1R`) works and forwards locally. Line mode silently does not: every line read on S3 is packaged as `;S1<line>` and unicast over ESP-NOW to WCB2's own MAC, which is not a peer. `esp_now_send` fails, the ETM pending table holds the entry for the full retry cycle, and the only output is `[ETM] Send failed seq N, error: …` (WCB.ino:2323) under `debugETM`. The port mapping appears configured (`?MAP,LIST` shows it) but delivers nothing.

**Suggested fix.** Mirror the raw path: `if (destWCB == 0 || destWCB == WCB_Number) { /* local */ }` at WCB.ino:6264. Optionally also normalise `wcbNum` to 0 in `addSerialMonitorMapping` (WCB_Storage.cpp:1717) when it equals `WCB_Number`, so the stored mapping and both consumers agree.

### F-335 · S3 · `Code/WCB/WCB.ino:6275`

**Serial-monitor mapping to a remote board hardcodes ';' instead of CommandCharacter, so on a board with a custom command char the mapped data is broadcast out all its ports as text**

*Category:* `protocol`

The remote branch of the serial-mapping fan-out builds the wire command with a literal semicolon:

```
                // Remote destination via ESP-NOW
                String espnowCmd = ";S" + String(destPort) + cmd;
                sendESPNowMessage(destWCB, espnowCmd.c_str());
```
(WCB.ino:6275-6276)

`CommandCharacter` is a runtime-configurable global (`char CommandCharacter = ';';` WCB.ino:156, changed by `?CMDCHAR` at WCB.ino:5059 and `updateCommandCharacter` at WCB.ino:5400, persisted by `saveLocalFunctionIdentifierAndCommandCharacter`). Every other command-building site in this range uses the variable — `String fwd = String(CommandCharacter) + message;` (WCB.ino:5841), `String trigger = String(CommandCharacter) + (isSeq ? "SEQ" : "C") + key;` (WCB.ino:6120).

The receiving board dispatches on *its own* value: `else if (cmd.startsWith(String(CommandCharacter)))` (WCB.ino:4384). When they differ, the `;S<port>…` payload does not match, falls through the if/else chain to case 3 and is handed to `processBroadcastCommand(cmd, sourceID)` (WCB.ino:4389).

**Failure scenario.** WCB2 is configured with `?CMDCHAR,:` (a documented, persisted setting). WCB1 has `?SMS3,W2S1` — mirror S3 into WCB2's S1. A line arrives on WCB1's S3 and is sent to WCB2 as `;S1<line>`. WCB2's `handleSingleCommand` does not recognise `;` as its command character, so instead of writing `<line>` to its S1 it treats the whole string `;S1<line>` as a broadcast: it is written verbatim out S1-S5 (every port not otherwise reserved), echoed to S0 if `broadcastToS0` is on, and re-broadcast over ESP-NOW to the whole mesh (WCB.ino:6356). One mapped serial line turns into a mesh-wide spray of malformed text on every board's serial ports.

**Suggested fix.** Build the payload with the live command character, matching every other site: `String espnowCmd = String(CommandCharacter) + "S" + String(destPort) + cmd;`. (The same literal appears in `processPWMPassthrough`'s remote branch, WCB_PWM.cpp:723, and should be fixed with it — note `sendESPNowMessage`'s PWM detection at WCB.ino:2278 also hardcodes ';'.)

### F-336 · S3 · `Code/WCB/WCB.ino:6404`

**lastReceivedViaESPNOW / inSequenceBody are written from three tasks on two cores with no synchronisation, so a locally-typed broadcast can be silently dropped (and a mesh-received recall can be re-broadcast)**

*Category:* `race`

`processIncomingSerial` resets the two origin globals just before dispatch:

```
          lastReceivedViaESPNOW = false;
          ...
          inSequenceBody = false;
```
(WCB.ino:6404, :6408). Both are plain `bool` globals (WCB.ino:165, :172) — not atomic, not volatile, not guarded.

Three contexts write them:
* serialCommandTask (core 1) — WCB.ino:6404/6408, this range.
* espNowReceiveCallback, the WiFi task on core 0 — `lastReceivedViaESPNOW = !wizardOrigin; inSequenceBody = false;` (WCB.ino:3883-3884) and `lastReceivedViaESPNOW = true; inSequenceBody = false;` (WCB.ino:4159-4160). The callback spans WCB.ino:3584-4230 (next function starts at 4231), so both writes are on the WiFi task.
* the loop task — restore-per-item at WCB.ino:7510-7511 and reset at :7518.

The snapshot that is supposed to make this safe happens *later*, on the serial task, inside `enqueueCommand` (`item.espnowOrigin = lastReceivedViaESPNOW;` WCB.ino:1920) — after `processSerialCommandHelper` -> `parseCommandsAndEnqueue` has done CRC work and string splitting. The window between the reset at 6404 and that snapshot is wide open to a core-0 write. The comment at WCB.ino:6405-6407 addresses only the drain-boundary latch (a same-task ordering issue); it does not address the cross-task write.

**Failure scenario.** Direction A (silent drop): operator types a bare broadcast, e.g. `OPEN`, on USB. serialCommandTask sets `lastReceivedViaESPNOW = false` at 6404; before `enqueueCommand` snapshots it, a command packet from another board arrives and the WiFi callback sets it `true` at WCB.ino:4159. The queue item is stamped `espnowOrigin = true`; at dispatch `processBroadcastCommand` reaches `sendESPNowMessage(0, cmd.c_str())` (WCB.ino:6356) which returns immediately at WCB.ino:2275 (`if (target == 0 && lastReceivedViaESPNOW) return;`). The command goes out the local serial ports but never reaches the mesh, and nothing is printed.

Direction B (unwanted fan-out): loop() is dispatching a `;Ckey` that arrived over the mesh (item restored to `espnowOrigin = true` at WCB.ino:7510). `recallStoredCommand` reads the global directly at WCB.ino:6119 (`if (!localOnly && !lastReceivedViaESPNOW && !inSequenceBody)`); if serialCommandTask sets it `false` at 6404 in that instant, the received recall re-broadcasts the trigger to every board — exactly the loop the comment at WCB.ino:6084 claims the one-hop cap prevents.

**Suggested fix.** Stop letting a global carry per-command origin across task boundaries. Either capture the origin into a local at the top of `processIncomingSerial`'s per-line block and thread it through `processSerialCommandHelper`/`parseCommandsAndEnqueue` into `enqueueCommand` as an explicit parameter, or (minimal change) have the WiFi callback set only a queue-item field and never the shared global, so 6404/6408 and 7510 are the only writers and both are on core 1.

### F-337 · S3 · `Code/WCB/WCB.ino:6417`

**processIncomingSerial() appends to a static String with no length cap — a device that never sends CR/LF grows it until the heap is gone**

*Category:* `leak`

WCB.ino:6380: `static String serialBuffers[6];  // one for each serial port (0 = Serial, 1–5 = Serial1-5)`
WCB.ino:6382-6418 reads bytes and only ever terminates a line on '\r' or '\n':
  `if (c == '\r' || c == '\n') { ... serialBuffer = ""; }`
  `else { serialBuffer += c; }`   <- line 6417
There is no maximum-length check anywhere in the loop. The buffers are `static`, so growth accumulates across calls for the life of the board, and Arduino String reallocates on the heap on each growth step.

The skip list at the top of the function (WCB.ino:6362-6377) covers MP3, DFPlayer, HCR and the in-flight Maestro query port, and serialCommandTask additionally skips raw-mapped ports (WCB.ino:6605-6608) — but an ordinary configured Maestro port, a WLED port, or any other attached device that emits binary or non-line-terminated output still reaches line 6417.

Consequence is not a buffer overrun (String reallocates) but heap exhaustion: the buffer grows until reserve() fails, at which point String::concat silently returns false and the data is dropped, while the ~100+ KB it already claimed stays allocated forever. Every other allocator on the board then fails — enqueueCommand()'s `malloc` (WCB.ino:1926-1930, prints "Out of memory while enqueuing command!" and drops the command), the ESP-NOW/WiFi stack, and NVS commits.

Contrast: the RX-side hardening effort is visible elsewhere in this file — Serial.setRxBufferSize(2048) at WCB.ino:7137 with a comment about burst overflow — so the input path was thought about, just not this accumulator.

**Failure scenario.** A device on S1-S5 (e.g. a Maestro streaming binary status, or a sensor emitting a continuous non-line-oriented stream) is attached to a port that is configured but not raw-mapped and not one of the MP3/DFP/HCR skips. serialBuffers[n] grows without bound; after minutes to hours the heap is exhausted. The board stops accepting commands ("Out of memory while enqueuing command!"), ESP-NOW sends start failing, and it appears hung while still printing heartbeat logs.

**Suggested fix.** Cap the accumulator at the largest command the parser accepts. At WCB.ino:6417: `if (serialBuffer.length() >= MAX_SERIAL_LINE) { Serial.printf("[S%d] line >%d chars — discarded\n", sourceID, MAX_SERIAL_LINE); serialBuffer = ""; } else { serialBuffer += c; }` with e.g. `#define MAX_SERIAL_LINE 512`. Logging the discard matters — a silent reset would make a mis-wired port look like intermittent command loss.

### F-338 · S3 · `Code/WCB/WCB.ino:6489`

**The timer-chain branch of processSerialCommandHelper drops sourceID, so a timer-grouped broadcast is echoed back out the port it came from**

*Category:* `logic`

Every other dispatch in `processSerialCommandHelper` forwards the originating port: `enqueueCommand(cmdWithoutChecksum, sourceID)` (WCB.ino:6465), `parseCommandsAndEnqueue(data, sourceID)` (WCB.ino:6476, :6495). The timer branch does not:

```
    if (!data.startsWith(String(LocalFunctionIdentifier)) && isTimerCommand(data)) {
        parseCommandGroups(data);
        return;
    }
```
(WCB.ino:6489-6492)

`parseCommandGroups` has no sourceID parameter (command_timer.cpp:73), and when the groups fire, `processCommandGroups` hardcodes source 0: `parseCommandsAndEnqueue(cmd, 0);` (command_timer.cpp:202).

Source 0 defeats both per-port input guards in `processBroadcastCommand`: the block check `if (sourceID >= 1 && sourceID <= 5 && blockBroadcastFrom[sourceID - 1])` (WCB.ino:6288) is skipped, and the echo-suppression term `i == sourceID` in the output loop (WCB.ino:6298) can never match ports 1-5. So a timer-grouped broadcast is written back out to the very port that supplied it, while the identical chain without a `;t` token is correctly suppressed there.

**Failure scenario.** A device on S2 is a full-duplex console that echoes what it receives. Operator (or the device) sends `;t500^OPEN` on S2. Non-timer form (`OPEN`) is suppressed on S2 by `i == sourceID`. The timer form is not: the group fires with sourceID 0, `processBroadcastCommand("OPEN", 0)` writes `OPEN` out S1-S5 including S2. The device echoes it, `processIncomingSerial` reads it back on S2 with sourceID 2 — and since `blockBroadcastFrom[1]` is false by default (WCB.ino:600) it is broadcast again, out S2 again. The chain ping-pongs, and each pass also fires `sendESPNowMessage(0, cmd)` (WCB.ino:6356), flooding the mesh. Same loss of source applies to a timer-bearing stored sequence (WCB_Storage.cpp:614-615 likewise calls parseCommandGroups without sourceID while the non-timer branch at :617 passes it).

**Suggested fix.** Thread the source through the timer path: add a `sourceID` parameter to `parseCommandGroups`, store it alongside `commandGroupsEspnowOrigin`/`commandGroupsSequenceBody` (command_timer.cpp:26-27), and use it in `processCommandGroups`'s `parseCommandsAndEnqueue(cmd, storedSourceID)` (command_timer.cpp:202). Update the two other callers (WCB.ino:428 in drainPendingTimerChains, WCB_Storage.cpp:615) to pass their source as well.

### F-339 · S3 · `Code/WCB/WCB.ino:7149`

**wdpBegin() is called before loadWCBNumberFromPreferences(), so the per-board WDP advert stagger is computed from the default WCB_Number and is identical on every board**

*Category:* `race`

setup() arms WDP six lines before the board learns its own number:

  7148  wdpPktQueue = xQueueCreate(8, sizeof(WdpPktSlot));
  7149  wdpBegin();             // WDP discovery: load on/off flag, clear neighbor table, arm advert cadence
  ...
  7155  loadWCBNumberFromPreferences();

`wdpBegin()` derives its advert phase from WCB_Number:

  WCB_WDP.cpp:1299  // Per-board phase stagger so periodic backstops from boards that booted
  :1300             // together don't all fire in the same tick.
  :1301  wdpNextAdvertMs = millis() + WDP_PERIOD_MS + (unsigned long)((WCB_Number % 16) * 500);

At :7149, WCB_Number is still the compile-time default — `int WCB_Number = 1;` (WCB.ino:133) — because loadWCBNumberFromPreferences() (WCB_Storage.cpp:183-187) has not run. Every board in the fleet therefore computes the same `(1 % 16) * 500 = 500 ms` offset, and the stagger the comment describes does not exist. It is never recovered: wdpTick() reschedules with a flat period, `wdpNextAdvertMs = now + WDP_PERIOD_MS;` (WCB_WDP.cpp:366), with no jitter term.

The same ordering also inverts the second scheduling relationship in wdpBegin:

  WCB_WDP.cpp:1298  wdpNextBootMs = millis() + 1600;   // just after the ETM boot announce (1500)

The ETM boot announce is armed at the far end of setup, `etmNextBootAnnounceMs = millis() + 1500;` (WCB.ino:7424), roughly 3 seconds of setup later (two delay(1000)s at :7121/:7184, the LED retry loop, WiFi + ESP-NOW bring-up). So wdpNextBootMs is already in the past when loop() first runs and the WDP boot advert fires *before* the ETM boot announce, not after it.

docs/WDP_DESIGN.md:53 documents the intended behaviour that the code does not implement: "| Periodic backstop | every 60 s | staggered per board (WCB_Number-based phase) so co-booted boards don't collide |".

**Failure scenario.** A droid's boards are all powered from one switch, so they boot within a few hundred ms of each other. Every board computes wdpNextAdvertMs = millis() + 60000 + 500 with the same WCB_Number default of 1, and wdpTick() then re-arms with a flat +60000 forever. From then on all boards broadcast their ~200-byte WDP adverts inside the same tick every 60 s, on one shared ESP-NOW channel — exactly the collision the stagger exists to prevent. Adverts lost to the collision are only recovered on the next 60 s backstop, so neighbor rows go stale (wdpTick's TTL sweep at WCB_WDP.cpp:385-390 clears `confirmed`) and capability routing through wdpCapOwner()/wcbPeerOnline() intermittently judges a live board unreachable.

**Suggested fix.** Move `wdpBegin();` (and the wdpPktQueue create with it, or leave the queue where it is) to after `loadWCBNumberFromPreferences();` at :7155 — the boot-announce arm at :7423-7424 is the natural neighbour, and placing it there also restores the "just after the ETM boot announce (1500)" relationship the wdpBegin comment claims. Add a comment naming the dependency, matching the existing precedent at :7156-7158 and :7161. Consider also adding the jitter term to the re-arm at WCB_WDP.cpp:366 so the stagger survives long uptimes rather than only being seeded at boot.

### F-340 · S3 · `Code/WCB/WCB.ino:7439`

**Every xTaskCreatePinnedToCore() return in setup() is ignored and the "Task Created" lines print unconditionally, so a failed task creation is invisible — unlike the commandQueue failure ten lines earlier, which hard-reboots**

*Category:* `error-handling`

All five task creations discard the BaseType_t result, and three of them print a success message that is emitted whether or not the task exists:

  7439  xTaskCreatePinnedToCore(serialCommandTask, "Serial Command Task", 4096, NULL, 1, NULL, 1);
  7443      xTaskCreatePinnedToCore(KyberLocalTask, "Kyber Local Task", 4096, NULL, 1, NULL, 1);
  7444      Serial.println("Kyber_Local Task Created");
  7447      xTaskCreatePinnedToCore(KyberRemoteTask, "Kyber Remote Task", 4096, NULL, 1, NULL, 1);
  7448      Serial.println("Maestro_Remote Task Created");
  7459        xTaskCreatePinnedToCore(PWMTask, "PWM Task", 4096, NULL, 2, NULL, 0);
  7460        Serial.println("PWM Task Created");
  7464  xTaskCreatePinnedToCore(RawSerialForwardingTask, "Raw Serial Task", 4096, NULL, 1, NULL, 1);
  7465  Serial.println("Raw Serial Forwarding Task Created");

This contradicts the policy the same function establishes for the other single-point-of-failure resource, ten lines up:

  7186  // Create the command queue. If this fails, every subsequent path that
  7187  // routes through enqueueCommand / parseCommandsAndEnqueue ... short-circuits silently
  7189  // and the board appears alive but accepts no commands. Drive a hard
  7190  // visible error and reboot rather than continuing as a brick.
  7192  if (!commandQueue) { ... ESP.restart(); }

That reasoning applies verbatim to serialCommandTask: it is the only reader of the USB/S1/S2 command ports (it is the sole caller of processIncomingSerial for those streams — WCB.ino:6612-6622), so if it fails to spawn the board comes up, prints its full banner, joins the mesh, and accepts nothing typed at it. RawSerialForwardingTask is the only driver of every rawMode serial mapping (WCB.ino:6643-6646). Neither failure produces any diagnostic; :7465 actively reports success.

Setup is also the worst moment for an unchecked allocation: the five tasks want 4096+4096+4096+4096+4096 bytes of stack, and the queues created earlier at :7127 (64 x 220 = ~14 KB), :7131, :7136, :7148 and :7191 have already been taken off the heap.

**Failure scenario.** On a board where boot-time heap is tight (a large learned-peer/PWM/serial-mapping config plus the ~14 KB rcJsonRelayQueue and ~2.4 KB commandQueue), xTaskCreatePinnedToCore at :7439 returns errCOULD_NOT_ALLOCATE_REQUIRED_MEMORY. setup() ignores it, prints "Raw Serial Forwarding Task Created" at :7465 anyway, disarms the boot guard at :7473 and enters loop(). The board heartbeats, answers ETM/WDP over the mesh and looks completely healthy in the Wizard, but every character typed at the USB console and every byte arriving on S1/S2 is discarded, because nothing ever calls processIncomingSerial. The operator has no message pointing at the cause and the console is the tool they would use to diagnose it.

**Suggested fix.** Capture the return of each xTaskCreatePinnedToCore and gate the "... Task Created" print on pdPASS. For serialCommandTask specifically, treat a failure the same way commandQueue failure is treated at :7192-7203 (loud message + ESP.restart(), so the boot guard/auto-retry path can recover); for the optional tasks (Kyber, PWM, RawSerial) a persistent error line is enough, but it must be an error line and not the current unconditional success line.


## S4 (part 2)

### F-341 · S4 · `Code/WCB/WCB.ino:231`

**PACKET_TYPE_MGMT_FRAG_UNICAST and PACKET_TYPE_CONFIG_REQ are both 5; PACKET_TYPE_STATS_REQ and PACKET_TYPE_REMOTE_TERM are both 7**

*Category:* `protocol`

The packet-type table at WCB.ino:224-250 assigns two values twice:
  WCB.ino:228: `#define PACKET_TYPE_MGMT_FRAG_UNICAST 5`
  WCB.ino:231: `#define PACKET_TYPE_CONFIG_REQ  5`
and
  WCB.ino:233: `#define PACKET_TYPE_STATS_REQ   7`
  WCB_RemoteTerm.h:42: `#define PACKET_TYPE_REMOTE_TERM  7`

Nothing misroutes today, because espNowReceiveCallback dispatches by packet SIZE first (WCB.ino:3607-3675) and the colliding types live in differently-sized structs: espnow_struct_mgmt is 226 bytes, espnow_struct_config_req is 43, espnow_struct_remote_term is 204. Every packetType comparison is therefore already scoped to one size class — handleMgmtPacket tests 3/5 on 226-byte frames only (WCB.ino:2641-2642), drainMgmtReqs switches on 5/7/8/13 over 43- and 59-byte frames only (WCB.ino:3283-3294).

But the safety is incidental, not enforced, and the surrounding comments read as though the numbering space is unique — WCB.ino:239 states "11 = ETM_BOOT, 12 = WDP (declared above with the ETM-struct types), 20+ = OTA", and WCB.ino:2637-2640 says "Dispatch by packetType (the size-based router can't): FRAG (3) = wizard-relay origin, FRAG_UNICAST (5) = WCB_Client direct unicast. Anything else sharing this struct size (e.g. a future MGMT_ACK) must NOT be consumed as a chunk". That comment explicitly anticipates new types sharing a struct size — which is exactly when a duplicate number becomes a live misroute. There are also static_asserts guarding struct-SIZE uniqueness (WCB.ino:3620-3648, 3651-3662) but nothing guarding type-VALUE uniqueness.

Note this is a wire-format constant shared with WCB_Client, so renumbering requires both sides.

**Failure scenario.** No current misbehaviour. It bites when someone adds a type to an existing size class — e.g. the "future MGMT_ACK" the comment at WCB.ino:2637 names. A new 43-byte request numbered 5 would be routed by drainMgmtReqs' `case PACKET_TYPE_CONFIG_REQ` into handleConfigReqPacket, which would fragment and broadcast an entire config dump in reply; a new 226-byte type numbered 5 would be consumed as an MGMT_FRAG_UNICAST chunk and corrupt an in-progress session.

**Suggested fix.** Give each type a unique value, or at minimum document the collision at the definitions ("5 is reused across the 226-byte MGMT and 43-byte REQ size classes — size routing keeps them apart; never compare packetType without first pinning the struct size"). A `static_assert(PACKET_TYPE_CONFIG_REQ != PACKET_TYPE_MGMT_FRAG_UNICAST, ...)` cannot be added as-is, but a comment plus a note in docs/WDP_DESIGN.md's packet table would stop the next addition from landing on a live number. Any renumber must be coordinated with WCB_Client.

### F-342 · S4 · `Code/WCB/WCB.ino:447`

**BoardStatus carries four counter fields that are never read or written anywhere**

*Category:* `dead-code`

WCB.ino:444-452:

  `struct BoardStatus {`
  `  bool online;`
  `  unsigned long lastSeenMs;`
  `  unsigned long messagesSent;`
  `  unsigned long messagesAckd;`
  `  unsigned long totalRetries;`
  `  unsigned long totalFailed;`
  `};`
  `BoardStatus boardTable[MAX_WCB_COUNT];`

A repo-wide grep for `messagesSent`, `messagesAckd`, `totalRetries`, `totalFailed` finds these four declarations and nothing else that touches a boardTable member — the only other hits are unrelated LOCAL accumulators in buildStatsString (WCB.ino:1645-1658), which sum the separate arrays `etmStatsSent/Ackd/Retries/Failed[MAX_WCB_COUNT]` declared at WCB.ino:525-528. All 30 `boardTable[...]` uses in the firmware touch only `.online` and `.lastSeenMs`.

The comment above the etmStats arrays confirms the split is intentional and that these fields are the leftovers: `// ETM - Per-board stats (boardTable already has online/lastSeen from Layer 2)` (WCB.ino:524).

**Failure scenario.** No runtime effect; 4 * 4 * 20 = 320 bytes of dead .bss. The hazard is maintenance: a reader adding per-board accounting sees the fields on the struct and increments boardTable[i].messagesSent, whose value is then never surfaced anywhere, while ?STATS keeps reporting the etmStats* arrays that were not updated.

**Suggested fix.** Remove messagesSent / messagesAckd / totalRetries / totalFailed from BoardStatus, leaving online + lastSeenMs, and let etmStats*[] remain the single home for per-board counters.

### F-343 · S4 · `Code/WCB/WCB.ino:499`

**etmSequenceNumber is dead and its comment is false — the live ETM counter is etmSequenceCounter; six more globals are never read**

*Category:* `dead-code`

  WCB.ino:499  `uint16_t etmSequenceNumber = 0;        // global sequence counter, Layer 3 uses this`

Repo-wide grep finds this line and nothing else — the variable is never read or written, and no "Layer 3" reads it. The counter actually used is the near-identically-named one 23 lines below:

  WCB.ino:522  `uint16_t etmSequenceCounter = 0;`
  WCB.ino:2313 `etmMsg.structSequenceNumber = ++etmSequenceCounter;`  (the only increment; the only send-path seq source)

So the comment asserts a role the variable does not have, and the two names are one character apart — a shadowing trap.

The same sweep found six more globals defined in WCB.ino with no reader anywhere in Code/:
  WCB.ino:158  `bool maestroEnabled = false;`               — only extern'd (WCB_Maestro.h:19, WCB_Maestro.cpp:14), never read or assigned
  WCB.ino:184-186 `espnowSendAttempts / espnowSendSuccess / espnowSendFailed` — never incremented; buildStatsString uses espnowCommand*/espnowPWM*/espnowRaw* instead
  WCB.ino:498  `bool etmBootHeartbeatSent = false;`         — never read or set; the live boot-announce state is etmBootAnnouncesLeft (WCB.ino:504)
  WCB.ino:564  `int etmCharTargetWCB = 0;  // 0 = not set`  — never read or set
  WCB.ino:657  `const uint32_t basicColors[9] = {...};`     — never indexed

And two write-only ones: `espnowCommandDelivered` (WCB.ino:589) is incremented at 2319 and zeroed at 5359 but never displayed — buildStatsString reports totalAckd instead; `trackCommandDelivery` (WCB.ino:592) is set by `?TRACK,ON/OFF` (WCB.ino:5020/5023 and 5295/5298) and read only by printTrackingStatus (WCB.ino:5382), gating nothing, so `?TRACK` is a toggle with no effect on delivery tracking.

**Failure scenario.** A maintainer adding ETM sequencing work (a wider seq space, a per-target counter, wraparound handling) edits `etmSequenceNumber` because its comment says it is the sequence counter. The change compiles, links, and has zero effect on the wire — every packet still carries `++etmSequenceCounter` from WCB.ino:2313 — and the sequence/dedup behaviour is unchanged while appearing to have been modified. Separately, an operator who runs `?TRACK,ON` expecting per-command delivery tracking gets only a status string.

**Suggested fix.** Delete WCB.ino:499 (or, if the name is preferred, rename etmSequenceCounter to it and update WCB.ino:2313 — do not leave both). Delete the six unreferenced globals listed above. For `?TRACK`, either wire trackCommandDelivery into a real gate or remove the command and its two dispatcher branches so the help stops advertising a no-op.

### F-344 · S4 · `Code/WCB/WCB.ino:499`

**etmSequenceNumber is unused and its comment falsely claims Layer 3 uses it**

*Category:* `dead-code`

WCB.ino:499:

  `uint16_t etmSequenceNumber = 0;        // global sequence counter, Layer 3 uses this`

`grep -rn etmSequenceNumber` over the tree matches only this line. The counter Layer 3 actually uses is the near-identically-named `uint16_t etmSequenceCounter = 0;` declared 23 lines later (WCB.ino:522) and consumed at WCB.ino:2304 (`etmMsg.structSequenceNumber = ++etmSequenceCounter;`).

So the file carries two nearly identical global names, one live and one dead, and the dead one's comment asserts it is the live one.

**Failure scenario.** No runtime effect. It is a correctness trap during maintenance: someone adding sequence handling (or debugging why a seq looks wrong) reads or increments etmSequenceNumber on the strength of its comment and changes nothing on the wire, because the packet is stamped from etmSequenceCounter.

**Suggested fix.** Delete WCB.ino:499. If a name is wanted for the wire counter, rename etmSequenceCounter rather than keeping both.

### F-345 · S4 · `Code/WCB/WCB.ino:575`

**Comment on etmCharBoardResults says "[board index 0-7]" but the array is [3][MAX_WCB_COUNT] (20)**

*Category:* `doc-drift`

  WCB.ino:575  `// [phase 0-2][board index 0-7]`
  WCB.ino:576  `ETMCharBoardResult etmCharBoardResults[3][MAX_WCB_COUNT];`

The second dimension is MAX_WCB_COUNT = 20 (WCB.ino:126), not 8. Every loop over it correctly walks the full range — WCB.ino:1433 `for (int b = 0; b < MAX_WCB_COUNT; b++)` in startETMChar, WCB.ino:1480 and 1518 in processETMChar — so the code is right and the comment is the bug.

This matters beyond cosmetics: it is a surviving marker of the hard-coded-8-boards era that the repo has been systematically purging (see the fixed instances at WCB.ino:5107 `// Was hard-capped at 8, which silently rejected a high-numbered relay` and WCB.ino:3530 `// mesh supports up to MAX_WCB_COUNT boards, not 8`). The line immediately below it, WCB.ino:577 `unsigned long etmCharPhaseSentTimes[3][200];`, is the same era's sizing and is still genuinely under-sized for 20 peers — see the separate S1 finding on WCB.ino:1470. A reader who trusts line 575 will conclude the ETM-char arrays are consistently 8-board-scoped and miss that one of the two was widened and the other was not.

**Failure scenario.** A maintainer auditing ETM-char capacity reads line 575, concludes the per-board dimension is 8, and either (a) adds a redundant `boardIdx < 8` guard that would then drop results for WCB9-WCB20, or (b) assumes the neighbouring [200] is likewise 8-board-scoped and therefore adequate, leaving the real out-of-bounds write at WCB.ino:1470 unfixed.

**Suggested fix.** Update WCB.ino:575 to `// [phase 0-2][board index 0..MAX_WCB_COUNT-1]`, and while there add the msgIndex ceiling to line 577's comment (`// [phase][msgIndex] — msgIndex capacity 200, see the clamp in processETMChar`) so the two dimensions are documented against the same board count.

### F-346 · S4 · `Code/WCB/WCB.ino:575`

**colorWipeStatus stale comment: the [board index 0-7] annotation describes an array that is [3][MAX_WCB_COUNT=20]**

*Category:* `doc-drift`

WCB.ino:575-576:

  `// [phase 0-2][board index 0-7]`
  `ETMCharBoardResult etmCharBoardResults[3][MAX_WCB_COUNT];`

The second dimension is MAX_WCB_COUNT, which is 20 (WCB.ino:120), and every loop over it iterates the full range — `for (int b = 0; b < MAX_WCB_COUNT; b++)` at WCB.ino:1432, :1480, :1524, and `etmCharBoardResults[2][peers[b] - 1].sent++` at :1508 where peers[] holds ids up to 20. The "0-7" annotation is a leftover from the 8-board era.

This matters more than a typical stale comment in this repo: hard-coded 8/9-board caps are a documented past bug class here (the ?RTERM / STATS / ETM / PWM / Storage caps that had to be raised to MAX_WCB_COUNT), and a comment that says a board index tops out at 7 is exactly the kind of thing that gets copied into a new loop bound.

**Failure scenario.** No runtime effect today. A maintainer trusting the annotation writes a `for (b = 0; b < 8; b++)` aggregation over etmCharBoardResults and silently omits boards 9-20 from the characterization report — the same class of defect this repo has already had to fix repeatedly.

**Suggested fix.** Change the comment to `// [phase 0-2][board index 0..MAX_WCB_COUNT-1]`.

### F-347 · S4 · `Code/WCB/WCB.ino:575`

**Comment on etmCharBoardResults says "board index 0-7" but the array is [3][MAX_WCB_COUNT] = [3][20]**

*Category:* `doc-drift`

WCB.ino:575-576:
  `// [phase 0-2][board index 0-7]`
  `ETMCharBoardResult etmCharBoardResults[3][MAX_WCB_COUNT];`
MAX_WCB_COUNT is 20 (WCB.ino:120), so the second dimension is 0-19, not 0-7. Every loop over the array in this file already iterates the real bound — WCB.ino:1432-1433 (`for (int b = 0; b < MAX_WCB_COUNT; b++) etmCharBoardResults[p][b].latencyMin = 999999;`), :1480-1481, :1524-1526 — so the code is right and only the comment is wrong.

This is the legacy 8-board assumption that this repo has repeatedly had to hunt down (the same stale "1-9" phrasing survives at WCB.ino:881). A stale comment claiming a 0-7 board range sitting directly above an ETM-characterization array is precisely the kind of note that leads someone to re-introduce a max-8 cap while "matching the existing convention".

**Failure scenario.** No runtime effect. The hazard is a future edit: a developer adding a per-board loop near this declaration trusts the comment and writes `for (int b = 0; b < 8; b++)`, silently excluding boards 9-20 from ETM characterization results — the exact class of regression the project has already had to fix in the RTERM/STATS/ETM/Maestro/PWM/Storage caps.

**Suggested fix.** Update WCB.ino:575 to `// [phase 0-2][board index 0..MAX_WCB_COUNT-1]`. While there, fix the same stale range in the comment at WCB.ino:881 if those arrays are kept rather than deleted.

### F-348 · S4 · `Code/WCB/WCB.ino:675`

**colorWipeStatus's else branch is unreachable, and its `if (debugEnabled){ }` is an empty block so the printf below it is unconditional**

*Category:* `dead-code`

Two defects stacked in the same block:

  WCB.ino:667  `if (statusled1 == "ES"){`
  WCB.ino:674  `else {`
  WCB.ino:675  `  if (debugEnabled){ }`
  WCB.ino:676  `  Serial.printf("No LED was chosen \n");`
  WCB.ino:677  `};`

(a) `if (debugEnabled){ }` has an EMPTY body, so the guard controls nothing and `Serial.printf("No LED was chosen")` at 676 executes unconditionally — a debug line that ignores the debug flag. This is the only empty `if` body in the file (verified by regex sweep over all 7520 lines).

(b) That whole else branch is unreachable anyway. Every one of the 17 call sites passes the literal "ES":
  WCB.ino:686, 697, 707, 715, 724, 741, 3848, 3860, 3870, 3885, 3932, 3984, 4000, 4161, 4165, 4208, 4229 — all `colorWipeStatus("ES", ...)`.
There is no other caller in the sketch, in any WCB_*.cpp, or in any header (repo-wide grep). So `statusled1 == "ES"` at 667 is a guard that is always satisfied, the `statusled1` parameter is inert, and 674-677 is dead code.

**Failure scenario.** If any future call ever passes a non-"ES" tag, the board prints "No LED was chosen" to the console on every LED update — including from espNowReceiveCallback (WiFi task, WCB.ino:3848/3885/4229), i.e. an unthrottled per-packet Serial write from the receive callback, which is precisely the cross-core Serial contention the rcJsonRelayQueue exists to avoid (WCB.ino:311-330). Today it simply cannot fire, so the empty-if bug is latent rather than live.

**Suggested fix.** Drop the dead else (674-677) and the now-inert `statusled1` parameter, or — if the tag is meant to grow — fix the guard to `if (debugEnabled) Serial.printf("No LED was chosen\n");` and route it through enqueueRcJsonRelay/otaRelayPrint rather than a direct Serial write, since colorWipeStatus is called from the WiFi task.

### F-349 · S4 · `Code/WCB/WCB.ino:675`

**colorWipeStatus's else branch has an empty debugEnabled guard followed by an unconditional printf, and is unreachable**

*Category:* `dead-code`

WCB.ino:674-677:

  `    else {`
  `      if (debugEnabled){ }`
  `      Serial.printf("No LED was chosen \n");`
  `    };`

Two defects in three lines. First, the `if (debugEnabled)` guards an empty block — the printf that was clearly meant to be inside it sits outside, so on any hardware revision in {21,23,24,31,32} a call with statusled1 != "ES" would spam "No LED was chosen" to USB serial regardless of the debug flag.

Second, it is unreachable: every one of the 17 call sites in the tree passes the literal "ES" (WCB.ino:686, 697, 707, 715, 724, 741, 3848, 3860, 3870, 3885, 3932, 3984, 4000, 4161, 4165, 4208, 4229 — verified by grep; there is no other caller in any .cpp). The `statusled1` parameter itself only ever takes one value.

**Failure scenario.** No runtime effect while the branch stays unreachable. It becomes one the moment anyone adds a second LED name or passes a variable: the board then prints an unguarded line to USB serial on every LED update — and colorWipeStatus is called from the ESP-NOW receive callback (WCB.ino:3848, :3860, :3870 on the WiFi task), which is exactly the context the rcJsonRelayQueue exists to keep off Serial (WCB.ino:309-320).

**Suggested fix.** Delete the else branch and the unused `statusled1` parameter, or move the printf inside the `if (debugEnabled)` block if the branch is meant to be kept.

### F-350 · S4 · `Code/WCB/WCB.ino:681`

**Eight functions in WCB.ino are defined but never called anywhere in the firmware**

*Category:* `dead-code`

Repo-wide grep across Code/ (WCB.ino, every WCB_*.cpp/.h, wcb_pin_map, command_timer) finds exactly ONE reference to each of these — the definition itself:

  WCB.ino:681   `void turnOnLEDESPNOW(){`            — no caller (turnOnLEDforBoot/turnOffLED ARE called; these three are not)
  WCB.ino:704   `void turnOnLEDSerial(){`            — no caller
  WCB.ino:712   `void turnOnLEDSerialOut(){`         — no caller
  WCB.ino:2416  `esp_err_t sendESPNowRawToSpecificWCB(const uint8_t *data, size_t len, uint8_t targetWCB) {` — no caller
  WCB.ino:2453  `void sendESPNowRawToPort(const uint8_t *data, size_t len, uint8_t targetWCB, uint8_t targetPort) {` — no caller (the live Kyber-targeted path is the receive-side handler at WCB.ino:4039, which decodes packets nothing in this firmware sends)
  WCB.ino:5506  `void clearSerialLabelCommand(const String &message) {` — no caller; the ?SLC dispatcher at WCB.ino:5280 calls `clearSerialLabel(message.substring(4).toInt())` inline instead
  WCB.ino:5792  `void verifyBackupChecksum(const String &message) {` — no caller; checksum verification actually happens in parseCommandsAndEnqueue (WCB.ino:1961-1994) and processSerialCommandHelper (WCB.ino:6448-6469)
  WCB.ino:6217  `void printPinSettings() {` — no caller

sendESPNowRawToPort is the notable one: it is the only writer of the `structTargetID[0] == 'K'` wire format that espNowReceiveCallback still decodes at WCB.ino:4039-4085 (with a chunkLen bounds guard added at 4050). That entire receive path is therefore unreachable from any WCB in the fleet — nothing in this firmware, and no sender in WcbCmd/WCB_Client that WCB.ino references, emits target 'K'.

**Failure scenario.** No runtime failure — these are pure dead weight. The concrete cost is misdirection during maintenance: turnOnLEDESPNOW/turnOnLEDSerial/turnOnLEDSerialOut read as the live LED state machine (they are not — the live one is colorWipeStatus called directly from espNowReceiveCallback), and the 'K'-target receive handler at WCB.ino:4039 reads as a supported protocol with no live sender, so effort spent hardening it buys nothing.

**Suggested fix.** Delete the seven unused helpers. For sendESPNowRawToPort/the 'K' target, decide explicitly: either delete both the sender (2453) and the receive branch (WCB.ino:4039-4085), or keep the receive branch for WCB_Client compatibility and add a comment at 4039 saying no WCB firmware emits it and which external sender does.

### F-351 · S4 · `Code/WCB/WCB.ino:881`

**commandsToSend[10] / commandsToReceive[10] are dead — 4,980 bytes of .bss for arrays nothing references**

*Category:* `dead-code`

WCB.ino:880-882:

  `// ESP-NOW messages`
  `espnow_struct_message commandsToSend[10]; // Includes 1-9 and broadcast`
  `espnow_struct_message commandsToReceive[10];`

`grep -rn "commandsToSend\|commandsToReceive"` over Code/WCB (*.ino, *.cpp, *.h) returns only these two definition lines — no reads, no writes, no extern declarations anywhere in the firmware. Every send path builds its packet as a local (`espnow_struct_message msg;` WCB.ino:2330, `espnow_struct_message_etm etmMsg;` :2282) and every receive path memcpy's into a local.

sizeof(espnow_struct_message) = 249 (40+4+4+1+200, packed), so these two arrays occupy 2 * 10 * 249 = 4,980 bytes of permanently-resident .bss on a part where the WCB already holds ~15 KB of MgmtSession/ConfigPullSession buffers plus a ~14 KB RC-JSON queue.

The trailing comment "Includes 1-9 and broadcast" is also stale in its own right: it describes a 1-9 board world, while MAX_WCB_COUNT is 20 (WCB.ino:120) — exactly the max-8/9 cap pattern this codebase has been burned by before. Anyone who "revives" these arrays by indexing them with a target WCB id would write out of bounds for ids 10-20.

**Failure scenario.** Not a runtime failure today (nothing touches them). The cost is 4,980 bytes of RAM unavailable to the heap for the ESP-NOW/WiFi stack, plus a live trap: a future change that indexes commandsToSend[targetWCB] — which the array name and the "Includes 1-9" comment invite — overruns by 11 elements for a 20-board fleet.

**Suggested fix.** Delete both arrays and the stale comment.

### F-352 · S4 · `Code/WCB/WCB.ino:881`

**commandsToSend[10] / commandsToReceive[10] and three espnowSend* counters are declared but never referenced, costing ~500 bytes of BSS**

*Category:* `dead-code`

Several globals in WCB.ino have zero uses anywhere in the firmware (verified by grepping every .ino/.cpp/.h in Code/WCB):
  - WCB.ino:881-882: `espnow_struct_message commandsToSend[10];` and `espnow_struct_message commandsToReceive[10];` — sizeof(espnow_struct_message) is 249, so these two arrays consume 4980 bytes of BSS for nothing. The trailing comment "// Includes 1-9 and broadcast" also encodes the obsolete 9-board assumption that MAX_WCB_COUNT=20 replaced.
  - WCB.ino:184-186: `espnowSendAttempts`, `espnowSendSuccess`, `espnowSendFailed` — never incremented or read. The counters actually used are espnowCommandAttempts/Success/Failed and the PWM/Raw variants.
  - WCB.ino:657: `const uint32_t basicColors[9] = {...}` — never indexed.
  - WCB.ino:177: `bool debugPWMEnabled` — never assigned true anywhere; its only read is `if (debugPWMEnabled) Serial.println("Invalid PWM output command");` at WCB.ino:6194, so that diagnostic can never print. The PWM debug flag actually wired to ?DEBUG,PWM,ON is debugPWMPassthrough (WCB.ino:4437).

Also in this family: WCB_Storage.h:72 declares `extern String storedCommands[MAX_STORED_COMMANDS];` but its definition in WCB.ino:907 is commented out — it links only because nothing references it.

**Failure scenario.** No runtime misbehaviour. ~5 KB of BSS is wasted on an ESP32 that already runs the Wizard config pull, ESP-NOW queues and the 64-slot RC-JSON relay (~14 KB) out of the same heap/RAM budget. debugPWMEnabled specifically makes a diagnostic unreachable, so an operator who turns on PWM debugging never sees "Invalid PWM output command" for a malformed ;P.

**Suggested fix.** Delete WCB.ino:881-882, :184-186, :657 and the commented-out :907 (plus the matching extern at WCB_Storage.h:72). For WCB.ino:6194, change the guard to the flag that is actually settable: `if (debugPWMPassthrough) Serial.println("Invalid PWM output command");`, then delete debugPWMEnabled at :177.

### F-353 · S4 · `Code/WCB/WCB.ino:923`

**Forward declaration `int etmAddToPendingTable(uint16_t, const char*)` has no matching definition — the real function takes three arguments**

*Category:* `dead-code`

The forward-declaration block at the top of the sketch declares a two-argument overload:

  WCB.ino:923   `int etmAddToPendingTable(uint16_t seqNum, const char* cmd);`

but the only definition in the file takes three:

  WCB.ino:1145  `int etmAddToPendingTable(uint16_t seqNum, const char* cmd, int targetWCB) {`

These are distinct overloads, not a redeclaration, so the compiler accepts both. The two-argument form has no body anywhere in the repo (grep over Code/ returns only WCB.ino:923, the definition at 1145, the single three-argument call at WCB.ino:2311 `etmAddToPendingTable(etmMsg.structSequenceNumber, message, target);`, and four comment mentions at 1100, 1233, 3764, 6873).

It links today only because nothing calls it. Every other declaration in that block (WCB.ino:911-922) does have a matching definition: writeSerialString→1862, sendESPNowMessage→2273 (default arg correctly on the declaration only), enqueueCommand→1912, checkConfigPullTimeout→3563, buildConfigString→2938, processIncomingSerial→6361, etmSendAck→1384, etmProcessAck→1222, and the PWM three resolve into WCB_PWM.cpp/.h (addPWMOutputPort's arity note at 916 correctly matches WCB_PWM.h:58).

**Failure scenario.** A future edit that calls `etmAddToPendingTable(seq, cmd)` — the natural shape for a broadcast, since `targetWCB == 0` means broadcast — compiles cleanly against this declaration and then fails at link with an undefined-reference error whose cause (a stale declaration 200 lines from anything related) is non-obvious. If instead someone "fixes" the link error by adding a two-arg definition, they get a second pending-table inserter with a default target, silently bypassing the expectAckFrom snapshot logic at WCB.ino:1183-1216.

**Suggested fix.** Change WCB.ino:923 to `int etmAddToPendingTable(uint16_t seqNum, const char* cmd, int targetWCB);` so the declaration matches the definition at 1145.

### F-354 · S4 · `Code/WCB/WCB.ino:923`

**Forward declaration of etmAddToPendingTable has 2 parameters; the only definition takes 3**

*Category:* `dead-code`

In the Forward Declarations block, WCB.ino:923:

  `int etmAddToPendingTable(uint16_t seqNum, const char* cmd);`

The actual function is `int etmAddToPendingTable(uint16_t seqNum, const char* cmd, int targetWCB)` (WCB.ino:1145) and the sole call site passes three arguments: `etmAddToPendingTable(etmMsg.structSequenceNumber, message, target);` (WCB.ino:2311). A repo-wide grep for the symbol returns only line 923, the definition at 1145, the call at 2311, and three prose comments.

The 2-arg declaration is therefore an overload that is declared but has no definition anywhere. It compiles today only because nobody calls it. It also misdocuments the API in the block a reader consults precisely to learn the signatures — and the missing parameter is `targetWCB`, the one that drives the unicast/broadcast ACK-expectation split at WCB.ino:1199-1207.

**Failure scenario.** A future caller writes `etmAddToPendingTable(seq, cmd);` on the strength of this prototype. It compiles cleanly and fails at link with an undefined-reference to the 2-arg overload; if a stub were ever added to satisfy the linker, the entry would be created with an uninitialised targetWCB and mis-track its expected ACKs.

**Suggested fix.** Update the declaration to `int etmAddToPendingTable(uint16_t seqNum, const char* cmd, int targetWCB);` (or delete it — the definition at :1145 precedes the only call at :2311).

### F-355 · S4 · `Code/WCB/WCB.ino:4023`

**Dead MAC-group re-check in the normal receive path; the ETM branch's comment already documents removing this duplicate and cites a stale line number**

*Category:* `dead-code`

The MAC-group filter is enforced unconditionally at the very top of the callback:

```
3593  if (info->src_addr[1] != umac_oct2 || info->src_addr[2] != umac_oct3) return;
```

The normal receive path then re-tests the identical condition on the same unchanged `info` pointer:

```
4023  if (info->src_addr[1] != umac_oct2 || info->src_addr[2] != umac_oct3) {
4024    if (debugEnabled) { Serial.println("Received message from an unrelated WCB group, ignoring."); }
4025    return;
4026  }
```

Nothing between 3593 and 4023 mutates `info` or `umac_oct2/umac_oct3`, so the branch body — including its debug message — is unreachable. The ETM path already recognised this and deleted its copy, leaving a note:

```
3721  // MAC-octet check was redundant here — already enforced at the top of
3722  // espNowReceiveCallback (line ~2600).  Removed to avoid duplicate work.
```

That comment is also stale: the top-of-function check is at line 3593, not "~2600". A reader chasing line 2600 lands in the middle of handleMgmtPacket.

**Failure scenario.** A maintainer debugging an inter-installation crosstalk complaint enables ?DEBUG,ON and looks for "Received message from an unrelated WCB group, ignoring." That line can never print, so the absence of it is read as evidence that no foreign traffic is arriving — when in fact foreign traffic is being dropped silently 430 lines earlier. Following the comment's "line ~2600" pointer lands in unrelated code and compounds the confusion.

**Suggested fix.** Delete WCB.ino:4023-4026, and correct the line reference in the comment at WCB.ino:3722 to point at the actual guard (WCB.ino:3593) — or drop the numeric reference entirely, since it will drift again.

### F-356 · S4 · `Code/WCB/WCB.ino:4323`

**The Maestro_Remote branch inside forwardMaestroDataToLocalKyber() is unreachable — Kyber_Local and Maestro_Remote are mutually exclusive**

*Category:* `dead-code`

forwardMaestroDataToLocalKyber() (WCB.ino:4307) contains:
  4323: `if (Maestro_Remote && remoteLen < (int)sizeof(remoteBuf)) remoteBuf[remoteLen++] = b;`
  4327: `if (Maestro_Remote && remoteLen > 0) sendESPNowRaw(remoteBuf, remoteLen);`
plus the `static uint8_t remoteBuf[64];` at WCB.ino:4310 that exists only to serve them.

The function has exactly one caller — WCB.ino:6579, inside KyberLocalTask — and that task is created only under `if (Kyber_Local)` (WCB.ino:7481-7484). Kyber_Local and Maestro_Remote are set as a mutually exclusive pair in both storage paths: WCB_Storage.cpp:1156-1157 (`Kyber_Local = true; Maestro_Remote = false;`), :1179-1180 (`Kyber_Local = false; Maestro_Remote = true;`), :1186-1187 (both false), and the same three-way assignment at WCB_Storage.cpp:1455-1465. Nothing else writes either flag. So whenever this function runs, Kyber_Local is true and Maestro_Remote is therefore false, and both guarded statements are dead.

The corresponding live version is forwardMaestroDataToRemoteKyber() (WCB.ino:4330), reached from KyberRemoteTask under `if (Maestro_Remote)` (WCB.ino:7485-7488). The dead branch is a leftover from before the two functions were split, and it masks the fact that the 64-byte drop it contains is only a real defect in the sibling.

**Failure scenario.** No runtime misbehaviour — the branch simply never executes. It costs 64 bytes of BSS and, more importantly, misleads the next reader into believing this path forwards to the mesh (it does not), which is exactly the kind of confusion that produces a wrong fix in the Kyber/Maestro forwarding area.

**Suggested fix.** Delete lines 4310, 4323 and 4327 from forwardMaestroDataToLocalKyber(), leaving the function to do only what its name says (drain local Maestro ports into the local Kyber port). Add a one-line comment noting that mesh forwarding for the Maestro_Remote case lives in forwardMaestroDataToRemoteKyber().

### F-357 · S4 · `Code/WCB/WCB.ino:5506`

**clearSerialLabelCommand() and verifyBackupChecksum() are defined but never called from anywhere in the firmware**

*Category:* `dead-code`

Two full function bodies in WCB.ino have no callers anywhere in the sketch or in any of the WCB_*.cpp/.h files:
  - `void clearSerialLabelCommand(const String &message)` — WCB.ino:5506-5527. Parses the `?SLCSx` form and calls clearSerialLabel(). The live ?SLC path instead inlines `clearSerialLabel(message.substring(4).toInt())` at WCB.ino:5281, so this function's port validation (`port < 1 || port > 5`, WCB.ino:5519) never runs.
  - `void verifyBackupChecksum(const String &message)` — WCB.ino:5792-5803. Its own body admits it does nothing ("Note: The actual verification happens during restore processing"); real verification lives in parseCommandsAndEnqueue (WCB.ino:1953-2000) and processSerialCommandHelper (WCB.ino:6438-6472).

Verified by grepping the whole firmware tree for each identifier — the only hits are the definitions themselves.

This is worth recording because clearSerialLabelCommand() is the *validated* version of a path that is currently done unvalidated inline, so its presence makes the ?SLC handling look safer than it is.

**Failure scenario.** No runtime effect — dead code only. The risk is maintenance: a reader tracing ?SLC will find clearSerialLabelCommand(), assume it is the handler, and reason about the wrong code (including its bounds check, which is not what actually protects the live path).

**Suggested fix.** Either delete both functions, or wire clearSerialLabelCommand() into the ?SLC dispatch at WCB.ino:5281 in place of the inline `clearSerialLabel(message.substring(4).toInt())` so the validated version is the one that runs. Delete verifyBackupChecksum() outright.

### F-358 · S4 · `Code/WCB/WCB.ino:5655`

**updateSerialSettings' "baud" branch is unreachable, making updateSerialBaudRate() dead code**

*Category:* `dead-code`

`updateSerialSettings` has exactly one caller, and it is prefix-gated on 's'/'S':

  WCB.ino:5343  `} else if (message.startsWith("s") || message.startsWith("S")) {`
  WCB.ino:5344  `    updateSerialSettings(message);`

(verified by repo-wide grep: only the definition at WCB.ino:5644 and this one call site.)

Inside it:

  WCB.ino:5655  `} else if (message.startsWith("baud") || message.startsWith("BAUD")) {`
  WCB.ino:5656  `    updateSerialBaudRate(message.substring(4));`
  WCB.ino:5657  `    return;`

A string that starts with 's' or 'S' can never also start with "baud" or "BAUD", so line 5655 is never true and line 5656 never runs. `updateSerialBaudRate` (WCB.ino:5668) therefore has no reachable caller — its only reference in the entire repo is line 5656.

The `?BAUDSx,yyyyy` form users are actually told to use (the deprecation notices at WCB.ino:5649 and 5661 both point at it) is served by a different, earlier branch: WCB.ino:5336 `} else if (message.startsWith("bauds") || message.startsWith("BAUDS")) {` which parses the port/baud inline at 5337-5341. So the feature works; this copy of it is orphaned.

**Failure scenario.** No runtime failure today — the branch and the function are simply unreachable. The hazard is maintenance: `updateSerialBaudRate` looks like the live ?BAUDS handler (its own comment at WCB.ino:5669 says `// Format: Sx,yyyyy  (e.g., S1,57600)`), so a fix applied there — a new port range, a baud validation, a different NVS key — silently has no effect while the real handler at WCB.ino:5336-5341 keeps the old behaviour.

**Suggested fix.** Delete the unreachable branch at WCB.ino:5655-5657 and the orphaned `updateSerialBaudRate` at WCB.ino:5668-5691, or — if the intent was to accept `?Sbaud1,57600` — change the test to inspect the string after the leading 'S' (`message.substring(1).startsWith("baud")`) and delete the duplicate parser at WCB.ino:5336 so there is one implementation.

### F-359 · S4 · `Code/WCB/WCB.ino:6194`

**debugPWMEnabled is never set true — ?DEBUG,PWM,ON sets a different flag, so the PWM-output error message can never print**

*Category:* `dead-code`

Two near-identical PWM debug flags are declared adjacently:

  WCB.ino:177  `bool debugPWMEnabled = false;`
  WCB.ino:178  `bool debugPWMPassthrough = false;  // Debug flag for PWM passthrough operations`

Repo-wide grep shows `debugPWMEnabled` has exactly two references — the initialiser at 177 and one read:

  WCB.ino:6194 `if (debugPWMEnabled) Serial.println("Invalid PWM output command");`

Nothing ever assigns it. Both user-facing toggles write the OTHER flag:
  WCB.ino:4441/4444  `?DEBUG,PWM,ON` / `?DEBUG,PWM,OFF` → `debugPWMPassthrough = true/false`
  WCB.ino:5165/5168  legacy `dpwmoff` / `dpwmon`        → `debugPWMPassthrough = false/true`

So `debugPWMEnabled` is permanently false and line 6194 is unreachable. `processPWMOutput` (WCB.ino:6187) therefore rejects out-of-range input completely silently:

  WCB.ino:6193 `if (port < 1 || port > 5 || pulseWidth < 500 || pulseWidth > 2500) {`
  WCB.ino:6194 `    if (debugPWMEnabled) Serial.println("Invalid PWM output command");`
  WCB.ino:6195 `    return;`

**Failure scenario.** A user sends `;P6 1500` (bad port) or `;P1 3000` (pulse outside 500-2500), turns on `?DEBUG,PWM,ON` to find out why nothing moved, and still sees nothing — the flag they enabled is debugPWMPassthrough, while the only diagnostic on this path is gated on debugPWMEnabled, which no command can set. The command is dropped at WCB.ino:6195 with zero output in every debug configuration.

**Suggested fix.** Replace `debugPWMEnabled` at WCB.ino:6194 with `debugPWMPassthrough` (the flag the ?DEBUG,PWM commands actually set) and delete the now-unreferenced declaration at WCB.ino:177. Alternatively make the range rejection unconditional — a malformed `;P` is a user error worth reporting regardless of debug state, matching how processSerialMessage reports a bad port at WCB.ino:5886.

### F-360 · S4 · `Code/WCB/WCB.ino:7195`

**The commandQueue-failure block's comment and user-facing message both describe code that is not there: it waits 1.5 s not 3 s, never blinks anything, and claims statusLED may be uninitialised when it was initialised 17 lines earlier**

*Category:* `doc-drift`

The fatal-path block reads:

  7193      Serial.println("FATAL: Failed to create command queue!");
  7195      Serial.println("       Rebooting in 3 seconds...");
  7196      // Try to give the user a visible LED hint if the status LED is alive.
  7197      // colorWipeStatus is defined later; statusLED may not be initialized
  7198      // yet — use a tight blink loop with the on-board GPIO if available.
  7199      for (int i = 0; i < 6; i++) {
  7200        delay(250);  // half-second cadence × 6 = 3 s total
  7201      }
  7202      ESP.restart();

Three separate contradictions with the code as written:

1. 6 iterations x delay(250) is 1500 ms, not 3 s. Both the inline comment ("half-second cadence × 6 = 3 s total" — the cadence is 250 ms) and the operator-facing line at :7195 ("Rebooting in 3 seconds...") are wrong by 2x.
2. The comment promises "a tight blink loop with the on-board GPIO". The loop body contains only delay(250); there is no digitalWrite, no colorWipeStatus, no LED activity of any kind. The visible hint the comment justifies the loop with does not exist, which makes the loop itself equivalent to a single delay(1500).
3. "statusLED may not be initialized yet" is false at this point. statusLED is initialised by initStatusLEDWithRetry(5, 100) at :7174 and the result is explicitly reported at :7176-7180, seventeen lines before this block. colorWipeStatus (:660) is also already usable and carries its own nullptr guard at :663.

This matters more than a normal stale comment because it is the one path a user hits when the board is genuinely broken: the printed timing is the only cue they have that the board restarted deliberately rather than crashed.

**Failure scenario.** commandQueue allocation fails (heap exhaustion at :7191). The console prints "Rebooting in 3 seconds...", the board restarts after 1.5 s, and nothing on the status LED changes at any point despite the comment asserting a blink hint. A maintainer reading :7196-7198 while diagnosing a dark board looks for a blink that was never implemented and concludes the LED hardware is dead; a maintainer trusting :7195 measures a 1.5 s gap and concludes the board crashed rather than restarted on purpose.

**Suggested fix.** Pick one: either implement the hint the comment describes (statusLED is valid here, so `colorWipeStatus("ES", red, 255)` / off alternating inside the loop, or digitalWrite(ONBOARD_LED, ...) for hw_version 1), or delete the loop and the stale rationale and use a single `delay(3000);` to match the printed message. Either way drop the "statusLED may not be initialized yet" clause — it is contradicted by :7174.

### F-361 · S4 · `Code/WCB/WCB.ino:7196`

**Command-queue-failure block in setup(): the comment contradicts the code three ways and the "blink loop" only delays**

*Category:* `doc-drift`

  WCB.ino:7195  `Serial.println("       Rebooting in 3 seconds...");`
  WCB.ino:7196  `// Try to give the user a visible LED hint if the status LED is alive.`
  WCB.ino:7197  `// colorWipeStatus is defined later; statusLED may not be initialized`
  WCB.ino:7198  `// yet — use a tight blink loop with the on-board GPIO if available.`
  WCB.ino:7199  `for (int i = 0; i < 6; i++) {`
  WCB.ino:7200  `  delay(250);  // half-second cadence × 6 = 3 s total`
  WCB.ino:7201  `}`
  WCB.ino:7202  `ESP.restart();`

Three separate contradictions:

1. "colorWipeStatus is defined later" is false — colorWipeStatus is defined at WCB.ino:660, roughly 6500 lines BEFORE setup() (WCB.ino:7106). It is callable here. Likewise `statusLED may not be initialized yet` is false: the NeoPixel is brought up at WCB.ino:7172 via initStatusLEDWithRetry, and `turnOnLEDforBoot()` is already called at WCB.ino:7180 — both before this block at 7192.

2. "use a tight blink loop with the on-board GPIO if available" describes code that does not exist — the loop body is a bare `delay(250)`. There is no digitalWrite, no ONBOARD_LED reference, no LED activity of any kind. The user gets no visible hint at all, only a dark board.

3. `delay(250)` × 6 = 1500 ms, i.e. 1.5 s — not the "half-second cadence" the inline comment claims, and not the "3 seconds" the operator was just told at line 7195. Both the comment and the user-facing message are wrong about the actual timing.

**Failure scenario.** commandQueue allocation fails (heap exhausted at boot — plausible given the ~14 KB rcJsonRelayQueue at 7146 plus five 4096-byte task stacks). The board prints "Rebooting in 3 seconds", shows no LED indication despite the comment promising one, and reboots after 1.5 s. On a board with no serial console attached — the normal deployed state — the operator sees a silent 1.5 s reboot loop with a dark or last-state LED and nothing that distinguishes it from the cold-boot stall the boot guard at WCB.ino:6789 exists to handle.

**Suggested fix.** Either implement the promised hint — `turnOnLEDforBoot(); for (int i=0;i<6;i++){ colorWipeStatus("ES", i&1 ? red : off, 255); delay(500); }` — or delete the three-line comment and correct the message to match the real 1.5 s. In both cases fix the false "defined later / not initialized yet" claim, since both colorWipeStatus (660) and the LED init (7172) precede this block.

### F-362 · S4 · `Code/WCB/WCB.ino:7485`

**loop()'s comment on drainLearnedPeerMaintenance() advertises a "post-boot prune" the function does not contain**

*Category:* `doc-drift`

The loop() service list documents this call as doing two things:

  7485  drainLearnedPeerMaintenance();  // flush learned-peer NVS writes + post-boot prune

The function does exactly one:

  7088  // Called every loop(): flush debounced learned-membership changes to NVS.
  7089  void drainLearnedPeerMaintenance() {
  7090    if (learnedPeersDirty && (long)(millis() - learnedPeersFlushMs) >= 0) {
  7091      saveLearnedPeers();
  7092      learnedPeersDirty = false;
  7093    }
  7094  }

There is no prune, no boot-window timer, and no eviction of any kind — the function's own header comment at :7088 correctly describes it as flush-only. Grepping the peer-management block confirms the only eviction paths are the TEMPORARY-peer TTL reaper inside processETMHeartbeats (:7071-7089) and the explicit ?WDP,FORGET / ?WDP,CLEAR commands via removeActivePeer (:6984) / clearAllLearnedPeers (:7097). Learned (permanent) peers are never pruned, which loadLearnedPeers's own comment states explicitly: "membership never self-evicts (only ?WDP,FORGET / ?WDP,CLEAR remove)" (:7057-7058).

The two comments therefore disagree with each other, and the one in loop() is the one a reader scanning the service list will see first.

**Failure scenario.** An operator reports that a WCB that was removed from the droid months ago still shows in ?WDP,LIST and still holds an ESP-NOW peer slot. A maintainer reads the loop() service list, sees "+ post-boot prune" at :7485, and goes hunting for why the prune is not firing — a timer bug, a dirty-flag bug, a millis() window. There is no prune to fix; the actual answer (learned membership is permanent by design, remove it with ?WDP,FORGET) is documented at :7057-7058, in a comment the reader had no reason to open.

**Suggested fix.** Trim the loop() comment to what the call does — `// flush debounced learned-peer NVS writes` — matching the function's own header at :7088. If a post-boot prune was actually intended (e.g. dropping learned peers never re-heard within N seconds of boot), it needs to be written; in that case add it to drainLearnedPeerMaintenance and record the eviction rule in docs/WDP_DESIGN.md, since it would change documented "membership never self-evicts" behaviour.
