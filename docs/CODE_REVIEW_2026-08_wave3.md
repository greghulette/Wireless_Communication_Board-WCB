# WCB Code Review — Wave 3 findings (the Wizard)

Companion to [CODE_REVIEW_2026-08.md](CODE_REVIEW_2026-08.md). Generated 2026-08-18 from the
wave-3 run: 14 scopes over `Wizard/` (7 `app.js` ranges, `parser.js`, `flasher.js`,
`serial-hub.js`, `index.html`, plus lenses for the firmware<->Wizard command contract,
async/lifecycle/leaks, and dead code / copy-paste drift). **All 14 agents completed.**

> **STATUS: ALL UNVERIFIED.** This wave was run reviewers-only by design — no adversarial
> verification pass. Treat each finding as a **lead to confirm**, not a fact.

**Totals:** 122 findings — S1: 9 · S2: 58 · S3: 36 · S4: 19


---

## S1

### F-135 · S1 · `Wizard/app.js:454`

**reconcileBoardGrid can delete a board slot that is mid-flash, killing the post-flash config restore**

*Category:* `race`

`reconcileBoardGrid()` decides whether to evict a rendered section with only two tests:

```js
452:  for (const [n, el] of have) {
453:    if (wantSet.has(n)) continue;
454:    if (boardConnections[n]?.isConnected?.()) continue;   // never yank a live board
455:    el.remove();
456:    delete boardConnections[n];
457:    delete boardConfigs[n];
```

During an esptool flash the port is handed to esptool: `boardGo` does `_boardFlashing[n] = true; await conn.closeForReconnect();` (app.js:7063-7064), and `isConnected()` (`return this._shared ? !!this._hub?.portOpen : this._connected;`) is therefore **false** for the whole 30-60 s flash window. The rest of the file knows this and guards on `_boardFlashing[n]` — `updateConnectionUI` does exactly that at app.js:7619 ("While a flash is in progress, suppress the 'Not connected' state"). `reconcileBoardGrid` does not, so the `_boardFlashing` guard is bypassed and the eviction branch runs.

A slot is in `want` via the `isConnected()` clause alone whenever it is above the WCBQ floor and not in `_meshBoards` (`desiredBoardNumbers`, app.js:429-438) — e.g. WCB Quantity is still 1 (the default set by `initSystemConfig` → `renderBoards(1)`, app.js:313) and a second board auto-migrated into slot 2 (app.js:6886-6899). Any `reconcileBoardGrid()` fired during the flash then deletes that slot. Reachable triggers inside the window: another board's `boardPull` (`renderBoards(config.wcbQuantity)`, app.js:6867), the 12 s mesh-discovery sweep (`removeClientCard` → `reconcileBoardGrid()`, app.js:12605; `addDiscoveredBoards` → app.js:592), the shared-port connect teardown (app.js:5761), and `onWCBQuantityChange` (app.js:1125). Note `meshAutoDiscoverTick` explicitly guards `_otaInProgress` (app.js:12610) but not `_boardFlashing`.

Once `boardConnections[n]` is deleted, the post-flash restore at app.js:7186-7189 (`boardConfigs[n] = preFlashConfigSnapshot; boardBaselines[n] = null; populateUIFromConfig(...); await boardGo(n, { mode: 'configure' })`) dead-ends: `boardGo` starts with `const conn = boardConnections[n]; if (!conn?.isConnected()) { showToast('Board not connected', 'error'); return; }`. The board was just fully flashed (NVS blank on `flash`/`factory`), the auto-push aborts on a toast, and the slot's card is gone from the UI.

**Failure scenario.** WCB Quantity left at the default 1. User connects a second board; it auto-migrates to slot 2 (slot 2 is in the grid only because `boardConnections[2].isConnected()` is true). User selects Flash on slot 2 and clicks Go. ~20 s into the 45 s flash the mesh-discovery tick removes a stale client and calls `reconcileBoardGrid()`; slot 2 is not in `want` and `isConnected()` is false, so section-board-2 is removed and `boardConnections[2]` / `boardConfigs[2]` are deleted. The flash completes, `conn.reconnect()` succeeds on the local `conn` object, then `boardGo(2,{mode:'configure'})` toasts "Board not connected" and returns. The user is left with a freshly flashed board holding factory-default NVS, no config pushed, and no card for it in the Wizard.

**Suggested fix.** Add the flash guard to the eviction test, mirroring updateConnectionUI: `if (boardConnections[n]?.isConnected?.() || _boardFlashing[n]) continue;` — and include `_boardFlashing` slots in `desiredBoardNumbers()` so the section is not merely spared but kept in `want`.

### F-136 · S1 · `Wizard/app.js:4455`

**updateSequence sends ?SEQ,SAVE as a single un-fragmented MGMT chunk — relay silently truncates it to 179 chars**

*Category:* `protocol-contract`

`updateSequence()` builds `const cmd = `${funcChar}SEQ,SAVE,${key},${value}`;` (app.js:4448) and, on the relay path, wraps it as `` `${relayFc}MGMT,FRAG,${seqSaveTargetWCB},${sessionId},0,1,${cmd}` `` (app.js:4455) — chunk 0 of 1, hard-coded, with no length check on `value`.

The relay firmware only takes the safe path for short payloads: `if (totalChunks == 1 && payload.length() <= 198)` sends it via ETM unicast (Code/WCB/WCB.ino:2597). Anything longer falls through to the broadcast struct, where `strncpy(pkt.payload, payload.c_str(), sizeof(pkt.payload) - 1)` (WCB.ino:2618) copies into `char payload[180]` (WCB.ino:795) — i.e. it is **cut at 179 characters, silently**. The target then sees chunkIdx 0 with totalChunks 1, its `receivedMask == expectedMask` immediately (WCB.ino:2702), and it executes the truncated string via `parseCommandsAndEnqueue(fullCmd, 0)` (WCB.ino:2731). `saveStoredCommandsToPreferences` (WCB_Storage.cpp:625) then writes the half-command into NVS.

The Wizard already owns the correct machinery and uses it on the push path — `const chunks = fragmentString(cmdToSend, MGMT_CHUNK_SIZE)` with `MGMT_CHUNK_SIZE = 180` (app.js:7852, :67) — but `updateSequence` bypasses it. Meanwhile the Wizard reports success (`showToast(`Sequence "${key}" updated on WCB ${n} (remote)`, 'success')`, app.js:4459) and patches the FULL value into `boardConfigs[n]`/`boardBaselines[n]` (app.js:4472-4477), so the local model and the board now permanently disagree and the diff push will never re-send it.

**Failure scenario.** Board WCB3 is reached through relay WCB1. In WCB3's Sequences table the user writes a normal choreography sequence, e.g. key `dome_open` with ~15 commands (`;M11^;t500^;M12^;t500^...^;L1,p3`), total value ~200 chars. Click SAVE/UPDATE. Payload = `?SEQ,SAVE,dome_open,<200 chars>` = 220 chars > 198, so the relay drops to the broadcast path and truncates at 179. WCB3 stores `dome_open` cut mid-token (e.g. ending `^;t5`). The Wizard shows a green "Sequence updated" toast. Running `;SEQdome_open` on the droid executes a partial sequence plus a malformed trailing token; pulling WCB3 later shows a value the user never typed.

**Suggested fix.** Reuse the push path's fragmentation: `const chunks = fragmentString(cmd, MGMT_CHUNK_SIZE); for (let i = 0; i < chunks.length; i++) await sendMgmtReliable(relayConn, `${relayFc}MGMT,FRAG,${target},${sessionId},${i},${chunks.length},${chunks[i]}`, relayN);` with the same `MGMT_CHUNK_DELAY` gap. At minimum, refuse to send and toast an error when `cmd.length > 198` on the relay path, and show a max in the value char counter (`updateSeqValCount`, app.js:4188).

### F-137 · S1 · `Wizard/app.js:5361`

**Boot-banner sniffer dereferences boardConfigs[n] unguarded — TypeError kills the read loop and starts an endless reset/reconnect loop**

*Category:* `undefined-ref`

In `BoardConnection._handleLine()` the three char-sniffer blocks set `charDetected = true` *unconditionally*, but only write into `boardConfigs[n]` behind a guard:

```js
5341:    if (delimMatch) {
5342:      boardBootChars[n] ??= {};
5343:      boardBootChars[n].delimiter = delimMatch[1];
5344:      if (boardConfigs[n]) boardConfigs[n].delimiter = delimMatch[1];   // guarded
5345:      charDetected = true;                                              // NOT guarded
5346:    }
```

The consumer is not guarded:

```js
5358:    if (charDetected) {
5361:      const fc = boardConfigs[n].funcChar;   // TypeError when boardConfigs[n] is undefined
```

`boardConfigs[n]` is only created in `addBoardSection()` (Wizard/app.js:617), and startup renders exactly ONE card (`initSystemConfig` → `renderBoards(1)`, Wizard/app.js:313). The first-time bulk auto-detect hands every authorized port a slot from `1..WCB_MAX` (Wizard/app.js:6574-6582) — `WCB_MAX` is 20 (Wizard/app.js:1081) — so slots 2..N get a live `BoardConnection` with **no** card and **no** `boardConfigs` entry. `updateConnectionUI()` (Wizard/app.js:7611) does not create a section, and the auto-pull is only scheduled at +3000 ms.

Meanwhile `connect()` asserts DTR (Wizard/app.js:4968), which the adjacent comment says fires the RC differentiator one-shot reset — so the board reboots and prints its banner ~1 s later. The firmware prints `Delimeter Character: %c` / `Local Function Identifier: %c` / `Command Character: %c` from `setup()` (Code/WCB/WCB.ino:7428-7430), matching the regexes at Wizard/app.js:5331-5333 exactly.

The throw lands in the read loop's own catch (Wizard/app.js:5523-5526), which logs `Port closed: …` and `break outer`. Because `this._connected` is still true, the post-loop block (Wizard/app.js:5538) runs the auto-reconnect, which closes and reopens the port — resetting the board again — and the new read loop hits the same banner and throws again.

**Failure scenario.** Fresh user opens the published Wizard, plugs in 3 WCBs, clicks Connect → Auto-Detect. The picker authorizes 3 ports. Slots 1/2/3 are claimed; only slot 1 has a card. Boards 2 and 3 reset on DTR, print `Delimeter Character: ^`, and `_handleLine` throws `Cannot read properties of undefined (reading 'funcChar')`. Their terminals show `Port closed: …` then `Board disconnected — attempting reconnect…`; each reconnect re-pulses DTR, resetting the board, and the banner throws again. Boards 2 and 3 never pull config, never get a card, and cycle through reset→reconnect indefinitely.

**Suggested fix.** Guard the consumer the same way the writers are guarded, e.g. change line 5358 to `if (charDetected && boardConfigs[n]) {`. Better still, set `charDetected = true` only inside the `if (boardConfigs[n])` branches, or create the slot's config lazily (`boardConfigs[n] ??= WCBParser.createDefaultBoardConfig()`) in `BoardConnection.connect()`. Separately, wrap the `_handleLine(line)` call at Wizard/app.js:5520 in try/catch so no parsing bug can ever kill the read loop and trigger a spurious reconnect.

### F-138 · S1 · `Wizard/app.js:7447`

**Changing Local Function Char back to '?' is never sent — the whole push is then mis-dispatched to the broadcast path (and out over ESP-NOW)**

*Category:* `protocol-contract`

The bootstrap block that switches the board's parser before the rest of the push runs:

```js
const curFuncChar = boardBaselines[n]?.funcChar  ?? '?';
...
if (config.funcChar !== curFuncChar && config.funcChar !== '?')
  bootstrap.push(`${curFuncChar}FUNCCHAR,${config.funcChar}`);   // app.js:7447-7448
```

The `config.funcChar !== '?'` guard means the target direction `'x' -> '?'` is never emitted. `parser.js:1302` has the identical guard (`if (config.funcChar !== '?') { ... add('FUNCCHAR,...') }`), so no command is emitted from either side.

The guard's stated reason (app.js:7444 "Never send ?FUNCCHAR,? — '?' suffix is re-parsed as a new command invocation") does not apply here: the command that *would* be sent is `xFUNCCHAR,?` (prefixed with the board's CURRENT char), and the firmware handles that fine — `WCB.ino:4380` dispatches on `cmd.startsWith(String(LocalFunctionIdentifier))` (= 'x'), then `WCB.ino:5045-5049` takes `args` = "?", `args.length()==1`, and assigns `LocalFunctionIdentifier = '?'`.

Consequence: the board stays on 'x', but three lines later the Wizard commits to the *target* char for the whole push:

```js
const funcChar = config.funcChar || '?';   // app.js:7463  -> '?'
const rawParts = cmdString.split(delim + funcChar);   // 7467
```

Every pushed command therefore arrives as `?WCB,3`, `?MAC,2,AB`, `?EPASS,...` at a board whose `LocalFunctionIdentifier` is 'x'. In `WCB.ino:4379-4388` that misses branch 1 (funcChar) and branch 2 (CommandCharacter ';'), and falls through to `processBroadcastCommand()`, which at `WCB.ino:6343` writes the raw text out serial ports S1-S5 and at `WCB.ino:6355` does `sendESPNowMessage(0, cmd.c_str())` — an unconditional ESP-NOW BROADCAST of every config command. Any peer still on the default `LocalFunctionIdentifier == '?'` (a board not yet migrated, a WCB_Client host) receives it via `WCB.ino:4222-4227 parseCommandsAndEnqueue()` and executes it locally — so `?WCB,3` / `?EPASS,...` land on the wrong board.

On the target board nothing is applied. Because `commandStringNeedsReboot()` will be true for a push containing MAC/WCB/HW/KYBER, boardGo takes the reboot branch at app.js:7495-7510, which never consults `_pushFullyAcked` — it sets `updateBoardStatusBadge(n,'configured')` and toasts "WCB n configured — rebooting…" for a push where not one command was applied.

**Failure scenario.** Board 4 was previously configured with Local Function Char = 'x' (pulled, so boardBaselines[4].funcChar === 'x'). In General Settings the user types '?' back into g-funcchar and also bumps MAC Octet 2, then clicks Push Config. No FUNCCHAR command is sent. Every command in the push goes out as '?…' and the board (still on 'x') broadcasts each one to all five serial ports and over ESP-NOW to the whole mesh. The board's MAC octet, func char and everything else remain unchanged, while the Wizard shows the green 'configured' badge and the toast "WCB 4 configured — rebooting…". A peer WCB or WCB_Client host still on the default '?' applies `?MAC,2,…` / `?WCB,…` to itself.

**Suggested fix.** Drop the `&& config.funcChar !== '?'` term at app.js:7447 (and the matching `if (config.funcChar !== '?')` wrapper at parser.js:1302) — the bootstrap already prefixes with `curFuncChar`, so `${curFuncChar}FUNCCHAR,?` is well-formed whenever `curFuncChar !== '?'`. Keep the suppression only for the genuinely degenerate case `curFuncChar === '?' && config.funcChar === '?'` (which the `!==` diff already excludes). Additionally, gate the reboot branch on `_pushFullyAcked` so a push where every command went unanswered cannot report 'configured'.

### F-139 · S1 · `Wizard/app.js:7852`

**Remote config push fragments at 180 chars, but the relay only forwards 179 — one character is silently dropped from every full chunk**

*Category:* `protocol-contract`

`boardGoRemote` splits the config command string with `const chunks = fragmentString(cmdToSend, MGMT_CHUNK_SIZE);` (Wizard/app.js:7852) where `const MGMT_CHUNK_SIZE = 180;` (Wizard/app.js:67). Every chunk except the last is therefore exactly 180 characters.

The relay copies that payload into a C string that can only hold 179 data bytes. `Code/WCB/WCB.ino:792` declares `char payload[180];` inside `espnow_struct_mgmt`, and `Code/WCB/WCB.ino:2617-2618` does:

```c
strncpy(pkt.payload, payload.c_str(), sizeof(pkt.payload) - 1);   // n = 179
pkt.payload[sizeof(pkt.payload) - 1] = '\0';                      // [179] = NUL
```

`strncpy` with n=179 copies src[0..178]; the NUL is then stamped at index 179. **The 180th character of each chunk is discarded.** The target reassembles the truncated chunks verbatim (`WCB.ino:2693-2694`, `:2707`) and executes them via `parseCommandsAndEnqueue(fullCmd, 0)` (`WCB.ino:2731`).

The sibling implementation of this exact wire format gets it right and documents the reason: `WCBClient/src/WCB_Client.h:189` — `#define WCB_MGMT_CHUNK_LEN 179 // payload[180] minus NUL (firmware strncpy)`. The Wizard is the only sender using 180.

I measured a real full push by loading `Wizard/parser.js` in node: `buildCommandString(createDefaultBoardConfig(), null, true)` on a default HW-3.2 board is **534 chars**; a modest board with 2 Maestros, 5 port labels, 2 sequences and 1 variable is **710 chars**. Both fragment into 3-4 chunks, so 2-3 characters are lost mid-string on every remote push.

Making this worse, the push is reported as a success (`Wizard/app.js:7870`) and the diff baseline is rewritten (`Wizard/app.js:7873` — `boardBaselines[n] = JSON.parse(JSON.stringify(config))`), so the mangled settings are never re-sent on any later diff push.

**Failure scenario.** User has WCB3 managed through a relay, edits its serial labels, clicks "Push Config". cmdToSend is 534 chars → chunks of [180, 180, 174]. The relay drops chunk 0's 180th char and chunk 1's 180th char. The target reassembles 532 chars in which two command boundaries are corrupted — e.g. `...?BAUD,S5,960?BCAST,OUT,S1,ON...` — so S5's baud is written as 960 and the following BCAST command is swallowed. Wizard shows "WCB 3: 12 changes sent via WCB 1" (success, green), updates the baseline, and the board is left with a wrong baud that no subsequent diff push will ever correct.

**Suggested fix.** Change `const MGMT_CHUNK_SIZE = 180;` to `179` at Wizard/app.js:67 to match `WCB_MGMT_CHUNK_LEN` in WCBClient/src/WCB_Client.h:189 and the firmware's `sizeof(payload)-1` copy at Code/WCB/WCB.ino:2617. Add a comment naming the firmware line so it can't drift back.

### F-140 · S1 · `Wizard/app.js:7938`

**Remote-pull listener is not target-filtered — a config reply is written to every board with a pull in flight**

*Category:* `protocol-contract`

`remoteBoardPull(relayN, targetN)` registers a per-call listener on the SHARED relay stream:

```js
const onLine = (line) => {
  if (done || !line.startsWith(prefixBase)) return;   // prefixBase = '[MGMT:CONFIG,'
  const closeIdx = line.indexOf(']', prefixBase.length);
  if (closeIdx < 0) return;
  done = true; ...
  const prefix = line.slice(0, closeIdx + 1);  // e.g. "[MGMT:CONFIG,3]"
```
(app.js:7938-7946). The WCB number is parsed out into `prefix` and then **never used again** — I grepped the whole `onLine` body (app.js:7938-8011) and `prefix` has exactly one occurrence. The listener matches ANY `[MGMT:CONFIG,*]` line and unconditionally does `boardConfigs[targetN] = config; boardBaselines[targetN] = deep-copy; populateUIFromConfig(targetN, config)` (app.js:7984-7991).

`BoardConnection._handleLine` fans every line to EVERY registered callback — `this._dataCallbacks.forEach(cb => cb(line))` (app.js:5313) — and `_pullingBoards` (app.js:7906) dedupes only per-target, so two pulls for DIFFERENT targets on the same relay are both live at once. When one `[MGMT:CONFIG,2]` line arrives, both listeners fire and both write board 2's config, one of them into slot 3.

The firmware does supply the discriminator: `Serial.printf("[MGMT:CONFIG,%d]%s\n", srcWCB, ...)` (Code/WCB/WCB.ino:3088), and the relay has only ONE reassembly buffer (`memset(&pullSession, 0, ...)`, WCB.ino:3060) so a second overlapping request destroys the first's session and only one reply is ever emitted.

The hazard is acknowledged in-tree — app.js:499-502 says "the pull's [MGMT:CONFIG,] listener isn't target-filtered — so overlapping pulls would cross-assign configs to the wrong board" — but the workaround (serialising) exists ONLY inside `relayRouteAll`. Every other caller is unserialised: app.js:494, 1838, 1863, 5945, 7716, 7741, 8058, 8066.

**Failure scenario.** WCB2 and WCB3 are both managed through relay WCB19. Both reboot after a Push All, so the relay prints "[ETM] WCB2 came ONLINE" and "[ETM] WCB3 came ONLINE" within a second of each other. `installEtmListener`'s callback fires `remoteBoardPull(19,2)` (app.js:7716) and then `remoteBoardPull(19,3)` — two listeners now live on relay 19's stream, both within the 6 s PULL_TIMEOUT_MS window. The relay emits ONE reply, `[MGMT:CONFIG,2]<blob>`. Both listeners match: slot 2 gets its own config (correct) and slot 3 gets WCB2's config, baseline, alias, serial-port map, Maestro/WLED slots and wcbNumber. Slot 3's listener set `done=true` and de-registered, so it never retries and the wrong config sticks. The user then edits and clicks Push Config on the WCB3 card; `buildCommandString` diffs against WCB2's baseline and pushes WCB2's settings onto WCB3.

**Suggested fix.** Filter on the parsed source number before consuming the line. In `onLine`, after computing `closeIdx`, parse `const src = parseInt(line.slice(prefixBase.length, closeIdx), 10);` and `if (Number.isFinite(src) && src !== (boardConfigs[targetN]?.wcbNumber || targetN)) return;` — i.e. use the value the dead `prefix` variable was clearly meant to carry. Additionally add a relay-level in-flight guard (one `[MGMT:CONFIG,…]` collector per relay connection at a time) so overlapping requests can't thrash the firmware's single `pullSession`.

### F-141 · S1 · `Wizard/app.js:7946`

**remoteBoardPull accepts ANY [MGMT:CONFIG,<n>] reply — two overlapping pulls write one board's config onto another board's slot**

*Category:* `race`

`remoteBoardPull`'s response listener matches on `const prefixBase = '[MGMT:CONFIG,';` and accepts any line starting with it (Wizard/app.js:7939): `if (done || !line.startsWith(prefixBase)) return;`. It then extracts the prefix — `const prefix = line.slice(0, closeIdx + 1);  // e.g. "[MGMT:CONFIG,3]"` (Wizard/app.js:7946) — and **never uses `prefix` again**. `prefix` is dead. The parsed config is stored unconditionally against the slot the closure was created for: `boardConfigs[targetN] = config;` / `boardBaselines[targetN] = ...` (Wizard/app.js:7984-7985).

The reply DOES carry the source board number — `Code/WCB/WCB.ino:3088`: `Serial.printf("[MGMT:CONFIG,%d]%s\n", srcWCB, fullConfig.c_str());` — and the Wizard already knows which number it asked for (`const pullWCBNum = boardConfigs[targetN]?.wcbNumber || targetN;`, Wizard/app.js:8032). The filter is available and simply isn't applied.

The hazard is documented at Wizard/app.js:500-502 as the reason `relayRouteAll` serialises its pulls ("the pull's [MGMT:CONFIG,] listener isn't target-filtered — so overlapping pulls would cross-assign configs to the wrong board"). But `relayRouteAll` is the ONLY caller that serialises. `_pullingBoards` (Wizard/app.js:7906-7911) dedups per-target, not per-relay, so it does not prevent two different targets pulling at once. The unserialised callers are `installEtmListener`'s online edge (Wizard/app.js:7716), its offline edge (:7741), `relayManageOne` (:494), `boardPullRemote` (:8058) and `boardRetryPull` (:8066).

Once boardConfigs[slotA] holds board B's config, the damage propagates to writes: `boardGoRemote` routes by `const pushTargetWCB = boardConfigs[n]?.wcbNumber || n;` (Wizard/app.js:7862), which is now B's number.

**Failure scenario.** Droid is powered on. The relay comes up, then WCB2 and WCB3 both announce within a second, so the relay prints two `[ETM] WCBn came ONLINE` lines. `installEtmListener` fires `remoteBoardPull(relay, 2)` and `remoteBoardPull(relay, 3)` — both allowed, different targets. WCB3 answers first: `[MGMT:CONFIG,3]?HW,32^?WCB,3^…`. Both live `onLine` closures are still in `relayConn._dataCallbacks`, so both consume it. Slot 2 now holds WCB3's config (including `wcbNumber: 3`) and the UI card labelled "WCB 2" displays WCB3's serial ports, Maestros and labels. The user edits that card and clicks Push Config → `pushTargetWCB` resolves to 3 → the edits are written to WCB3. WCB2 is never touched and its real config is gone from the tool.

**Suggested fix.** Filter on the source number inside `onLine`: after computing `closeIdx`, do `const srcWcb = parseInt(line.slice(prefixBase.length, closeIdx)); if (srcWcb !== pullWCBNum) return;` (hoist `pullWCBNum` above the listener). Use the now-live `prefix`/`srcWcb` instead of discarding it. Optionally also gate concurrent pulls per-relay, since the firmware relay keeps only ONE `pullSession` (Code/WCB/WCB.ino:3060) and has no busy guard on it.

### F-142 · S1 · `Wizard/app.js:11748`

**Connect & Push always reports "✓ Done", even when the push never happened or the board never came back**

*Category:* `error-handling`

In `wizardWatchForConnect` the success status is set unconditionally at the end of the try block:

```js
        await boardGo(n);
        if (!boardConnections[n]?.isConnected()) {
          wizardSetConnectStatus(n, 'busy', '⏳ Reconnecting…');
          for (let i = 0; i < 60 && !boardConnections[n]?.isConnected(); i++) await sleep(500);
        }
        if (boardConnections[n]?.isConnected()) { /* verify pulls */ }
        wizardSetConnectStatus(n, 'ok', '✓ Done');   // ← line 11748, unconditional
      } catch(e) {
        wizardSetConnectStatus(n, 'err', '✕ Push failed');
      }
```

Neither guarded branch feeds the outcome. Worse, the `catch` is effectively unreachable for real push failures because `boardGo` swallows everything: it returns silently on `if (!conn?.isConnected()) { showToast('Board not connected','error'); return; }` (Wizard/app.js:6997), on `if (!cmdString) { showToast('No changes to push'); return; }` (:7420), on invalid variables (:7405), and its whole configure path ends in `catch (e) { showToast(`Push failed: ${e.message}`); termLog(...); }` with no rethrow (:7601-7604). It also only sets an amber badge when `_pushFullyAcked` is false (:7572-7579) — the wizard never sees that either.

So every terminal state — board dropped before the push, config commands unACKed, board never reconnected after the reboot, all three verify `boardPull`s timing out with `boardBaselines[n]` still null — lands on green `✓ Done`. `wizardSetConnectStatus(…, 'ok', …)` then calls `wizardCheckAllDone()` and `wizardSeqAdvance(n)` (:11767-11770), which advances to the next board and, once every slot is resolved with `anyOk` true, auto-closes the wizard after 2.5 s (:11796-11799).

**Failure scenario.** User runs the Wizard on 2 boards. WCB 1's USB re-enumerates (CH340/CP2102 driver settle, or the DTR reset from the port open) right as the watcher's gate opens. `boardGo(1)` hits `!conn.isConnected()`, toasts "Board not connected" and returns. The 30 s reconnect poll fails. Verify is skipped. The row turns green "✓ Done", the wizard advances to WCB 2, and after WCB 2 finishes the wizard auto-closes. The user believes both boards were configured; WCB 1 received nothing — wrong password/MAC/WCB number, so it never joins the mesh, and the only evidence is a toast that scrolled away.

**Suggested fix.** Track the real outcome and report it: have `boardGo` return a success/failure result (or throw), then in `wizardWatchForConnect` set `'err'` when `boardGo` reports failure, when the reconnect poll exits with the board still disconnected, or when `boardBaselines[n]` is still null after the three verify pulls. Only set `'ok'` on a verified pull.

### F-143 · S1 · `Wizard/parser.js:1531`

**Stored sequences are pushed AFTER the PWM mapping command that reboots the board — they are silently discarded**

*Category:* `protocol-contract`

`buildCommandString` emits blocks in this order: PWM output ports (1499-1506), **Mappings** (1508-1522, `add(cmd)` at :1520), then **Stored Sequences** (1524-1541, `add(`SEQ,SAVE,${seq.key},${seq.value}`)` at :1531 and `SEQ,CLEAR` at :1536).

A `?MAP,PWM,S<n>,…` command makes the firmware **reboot**: `WCB.ino:4542` calls `addPWMMapping(pwmConfig)` and the declaration defaults to auto-reboot — `WCB_PWM.h:38  void addPWMMapping(const String &config, bool autoReboot = true);` — whose tail is `WCB_PWM.cpp:338-342`:
```
    if (autoReboot) {
        Serial.println("Rebooting in 3 seconds to apply PWM configuration...");
        delay(3000);
        ESP.restart();
    }
```
The local push loop (`app.js:7482`) is ACK-paced with `sendAndAwaitIdle(cmd, { quietMs: 250, hardTimeoutMs: 5000 })` — the board prints its lines and then goes quiet for 3 s inside `delay(3000)`, so the 250 ms quiet window elapses and the Wizard immediately sends the next commands into a board that is blocked in `delay()` and then calls `ESP.restart()`. Everything sent in that window is dropped with the RX buffer.

The firmware's own emitter has the opposite (correct) order and says so: stored sequences at `WCB.ino:2887-2905`, then `// PWM output ports + mappings (mappings LAST — restore triggers a reboot)` at `WCB.ino:2907`. parser.js inverted it.

Verified empirically: a delta push where the user adds one sequence produced exactly `?MAP,PWM,S3,S5^?SEQ,SAVE,wave,;M1,1`. (The mappings diff at :1510 also fires on essentially every save after a pull, because `app.js:3614 syncMappingsToConfig()` stores an extra `bidir` key that the parser never produces, so `JSON.stringify` never matches the parsed baseline.)

**Failure scenario.** Board has a PWM input mapping (e.g. RC receiver passthrough on S3). User adds a stored sequence in the Wizard and clicks Push Config. Wizard sends `?MAP,PWM,S3,S5` (board starts its 3 s reboot countdown) then `?SEQ,SAVE,wave,;M1,1` into the dead window. Push reports success; the sequence never reaches NVS. After the reboot the Wizard auto-pulls (`app.js:7538`), the pulled config has no `wave` sequence, and the UI list is overwritten — the user's sequence is gone with no error. Same on every full push / config-file restore of a board that has both a PWM mapping and sequences.

**Suggested fix.** Move the Stored Sequences block (1524-1541) — and the Variables/Maestro/ETM blocks if any are still after it — ahead of the `── Mappings ──` block, so the mapping commands are always the LAST thing emitted, matching `collectConfigCommands` (WCB.ino:2887 vs :2907). Alternatively emit `MAP,PWM,S…` last and have app.js treat it as terminal (it already flags it in `commandStringNeedsReboot`, app.js:8194).


---

## S2

### F-144 · S2 · `c:/Users/ghulette/Documents/GitHub/Wireless_Communication_Board-WCB/Wizard/serial-hub-test.html:93`

**serial-hub-test.html: "Pick Port" can never be enabled — the shipped test harness is unusable**

*Category:* `dead-code`

The button starts disabled (`<button id="pick" disabled>`, line 43) and the ONLY code that can enable it is the hub's state handler:

```js
$('pick').disabled = st.role !== 'leader';   // :93
```

But since the conditional-campaigning fix (WCB commit 581d939), a tab cannot become leader without already holding a port: `join()` calls `_setRole('follower')` then `_maybeCampaign()` (serial-hub.js:137-139), and `_maybeCampaign()` bails at `if (!this._port && !this._sawLeaderPort) return;` (serial-hub.js:260). `_port` is only set by `requestPort()`/`adoptPort()`, and this page's only call to `requestPort()` is behind the disabled button (line 116). `_sawLeaderPort` needs another tab already leading with an open port (serial-hub.js:502) — and even then this tab stays a *follower* while that leader lives, so `st.role !== 'leader'` still holds and the button still stays disabled.

Result: after Join, role is permanently 'follower', 'pick' is permanently disabled, `portOpen` is permanently false, so 'tx'/'send' (line 94-96) also stay disabled. Every control on the page except Join/Leave is dead. The page's own instructions (line 37-38, "One becomes LEADER — click Pick Port there") describe the pre-fix behaviour. `pages-deploy.yml` copies the whole Wizard directory (`cp -a "$WIZ/." "$GHP/$DEST/"`), so this broken page ships to GitHub Pages alongside the Wizard.

**Failure scenario.** Open https://<user>.github.io/Wireless_Communication_Board-WCB/Wizard/serial-hub-test.html in two tabs and click Join in both, exactly as the page instructs. Both tabs log "role → follower" and stop there. "Pick Port" stays greyed out in both, so no port is ever opened, no tab ever leads, and the tool the page exists to exercise cannot be exercised at all.

**Suggested fix.** Decouple the picker from leadership, since picking a port is what makes a tab eligible to lead: enable 'pick' whenever the hub has joined (e.g. `$('pick').disabled = st.role === 'idle';`) and let `hub.requestPort()` → `_maybeCampaign()` drive the election. Update the instructional copy at lines 37-39 to describe conditional campaigning ("click Pick Port in one tab; that tab becomes LEADER").

### F-145 · S2 · `c:/Users/ghulette/Documents/GitHub/Wireless_Communication_Board-WCB/Wizard/serial-hub.js:114`

**`_leaderPortOpen` latches true forever — `portOpen` lies, so the Wizard shows "Connected" on a dead share and drops every send**

*Category:* `logic`

`get portOpen() { return this._portOpen || this._leaderPortOpen; }` (serial-hub.js:114) is the value the whole Wizard trusts: `BoardConnection.isConnected()` is `this._shared ? !!this._hub?.portOpen : this._connected` (Wizard/app.js:4935), and `_emitState()` (serial-hub.js:534) feeds `_onHubState` → `updateConnectionUI(n, open)` (Wizard/app.js:5426-5433).

`_leaderPortOpen` is set true at serial-hub.js:500 from a leader's `state`, but grep across the file shows the ONLY place that ever clears it is `_relinquish()` at serial-hub.js:478 — and `_relinquish` is reachable only via the visibility-handoff subsystem, which is `visibilityHandoff: false` by default (serial-hub.js:58) and is never enabled by any caller (`grep -rn visibilityHandoff Wizard/` returns only the three definitions inside serial-hub.js itself). It is also not cleared on `case 'bye'` (serial-hub.js:518, an empty `break;`), not in `leave()`, not in `_becomeLeader()`, and not in `_stepDownPortless()`.

Consequence: once a tab has been a follower of a live leader, `hub.portOpen` can never go false again unless some OTHER live leader broadcasts `portOpen:false`. If the leader tab is closed or crashes it broadcasts nothing, and if this tab then promotes and fails to acquire a port, `_stepDownPortless()` → `_announceState()` → `_emitState()` still reports `portOpen: this.portOpen` === `false || true` === true. Meanwhile `send()` on a follower with no leader posts `{t:'tx'}` onto a bus nobody is listening on and returns `Promise.resolve()` (serial-hub.js:196-197) — the bytes vanish. The `send()` doc comment at serial-hub.js:188-189 explicitly depends on this field being honest: "bytes are just dropped (surface portOpen so the user knows why)."

**Failure scenario.** Wizard tab B follows NaviCore tab A, which leads the shared WCB. The user unplugs the board and closes tab A. Tab B is promoted, `_adoptGrantedPort()` finds no port (getPorts() is now empty, serial-hub.js:321), and `_stepDownPortless()` runs. Tab B's board card still reads "Connected" (green) because `portOpen` is latched true. The user clicks Pull / Push Config: `conn.send()` awaits `hub.send()`, which posts a 'tx' no leader receives, resolves successfully, and the Wizard sits waiting for a reply that never comes — no error, no disconnect, just a silently dead board that looks connected.

**Suggested fix.** Clear `this._leaderPortOpen = false` (a) in `case 'bye'` at serial-hub.js:518, (b) at the top of `_becomeLeader()` — once we lead, only `_portOpen` is authoritative — and (c) in `leave()` / `releasePortKeepOpen()` alongside `_portOpen = false`. Apply the same change to the NaviCore copy to keep the files in lockstep.

### F-146 · S2 · `c:/Users/ghulette/Documents/GitHub/Wireless_Communication_Board-WCB/Wizard/serial-hub.js:325`

**`_adoptGrantedPort()` takes the first VID/PID match — failover to the WRONG physical WCB when two identical adapters are granted**

*Category:* `logic`

On failover the promoted tab re-finds the port from the origin's grant list:

```js
pick = ports.find(p => {
  const i = (p.getInfo && p.getInfo()) || {};
  return i.usbVendorId === want.usbVendorId && i.usbProductId === want.usbProductId;
}) || null;
```

`SerialPort.getInfo()` exposes nothing but `usbVendorId`/`usbProductId`, so two WCBs using the same USB-serial bridge (the normal case in a droid — several WCBs, all CH340 or all CP2102) are indistinguishable here. `Array.prototype.find` returns the FIRST match and the code neither counts matches nor refuses on ambiguity; it just sets `this._port = pick` and logs the reassuring "adopted origin-granted port for failover" (serial-hub.js:334). The subsequent `_openAndRead()` opens it and `_announceState()` publishes it as THE shared port.

Wizard/app.js:5704-5706 acknowledges this ambiguity for its own join-time guard ("two identical-model boards share VID/PID, so this stays best-effort in that case") — but that guard is on a different code path and cannot see, let alone veto, what `_adoptGrantedPort` picked. The hub itself has no ambiguity handling at all: its comment (serial-hub.js:315-317) only mentions "Match the identity the old leader announced; fall back to the sole granted port if there's exactly one."

**Failure scenario.** A droid has WCB 1 (dome) and WCB 3 (body) both on CH340 adapters, both granted to the Wizard origin. WCB 1 is the shared port; a second tab follows. The leader tab is closed. The follower promotes, and `ports.find` returns WCB 3's SerialPort (whichever getPorts() lists first) because the VID/PID are identical. The hub opens WCB 3, announces portOpen:true, and the Wizard's WCB-1 board card goes green again. The next Push Config writes WCB 1's config — its board ID, MACs, serial device map — into WCB 3.

**Suggested fix.** Count the matches instead of taking the first: `const matches = ports.filter(...)`; adopt only when `matches.length === 1`. When it is ambiguous (or zero), log the reason and leave `this._port` null so `_becomeLeader()` falls into `_stepDownPortless()` and the user re-picks explicitly — a stalled share is far cheaper than a config written to the wrong board. Mirror into the NaviCore copy.

### F-147 · S2 · `c:/Users/ghulette/Documents/GitHub/Wireless_Communication_Board-WCB/Wizard/serial-hub.js:356`

**Leader that cannot open its port squats the exclusive Web Lock forever (fix present in NaviCore, missing here)**

*Category:* `protocol-contract`

`_openAndRead()` exhausts its 4 open() retries and does:

```js
if (i === attempts - 1) { this._log('open failed: ' + msg); this._announceState(); return; }
```

It returns while `this._role === 'leader'` and `this._releaseLock` is still held — i.e. this tab keeps the single exclusive Web Lock `wcb-shared-serial-owner` while holding a port it could not open. Every other same-origin tab is parked in `navigator.locks` behind it (`_campaign`, serial-hub.js:272) and can never be promoted. Nothing in the hub re-triggers: `_stepDownPortless()` (serial-hub.js:301) is only called from `_becomeLeader()` when `!this._port` (serial-hub.js:292), so this branch never re-queues.

The NaviCore copy of this identical file — c:/Users/ghulette/Documents/GitHub/NaviCore/config_tool/serial-hub.js:316, committed in f72878a and clean in the working tree — has the fix, with a comment naming this exact bug: "Out of retries: we hold the exclusive lock and a port we cannot open. Do NOT stop here — squatting is the very deadlock _stepDownPortless() exists to prevent (a tab queued behind us with a WORKING port would never be served). Step down and re-queue." The two copies are required to stay in lockstep (the NaviCore file's own comment at :323 says "(Reconciled with Wizard/serial-hub.js.)"), and this branch is the divergence.

Note app.js only papers over ONE entry into this branch: the auto-share path tears the hub down on failure (Wizard/app.js:5658-5659). The explicit "Share port" button (`modalSharedConnect`, Wizard/app.js:5797-5804) just toasts "no tab has opened the port yet" and leaves the squatting hub in place, and the failover entry (below) has no cleanup at all.

**Failure scenario.** Two tabs share WCB 1 (Wizard is leader, NaviCore tab is follower). The user closes the Wizard tab. The browser grants the lock to the NaviCore tab, which calls `_adoptGrantedPort()` then `_openAndRead()`; on Windows the CH340/CP2102 handle from the closed tab is not released within the 4x300ms retry budget, so open() fails 4 times. The hub logs "open failed: …", announces portOpen:false, and returns still holding the lock. From that moment the board is unreachable from EVERY tab on the origin: reopening the Wizard, it joins, campaigns, and sits in the lock queue forever behind the squatter. Only closing the NaviCore tab recovers it.

**Suggested fix.** Replace `this._announceState(); return;` with `this._stepDownPortless(); return;` (it already announces, releases the lock, and re-queues on the jittered backoff), matching NaviCore/config_tool/serial-hub.js:316.

### F-148 · S2 · `c:/Users/ghulette/Documents/GitHub/Wireless_Communication_Board-WCB/Wizard/serial-hub.js:407`

**Read loop death (unplug) leaves the lock held and the dead port handle open — board unreachable from every tab**

*Category:* `protocol-contract`

When `_readLoop()` exits — the `while` guard fails because `this._port.readable` went null on unplug, or the `emptySessions >= 6` cap trips (serial-hub.js:404) — the tail block does only:

```js
if (this._portOpen && !this._leaving && this._role === 'leader') {
  this._portOpen = false;
  this._announceState();
  this._log('port read loop ended (unplug?)');
}
```

It does NOT call `_closePort()` and does NOT step down. So after an unplug this tab (a) still holds the exclusive Web Lock, blocking every queued tab on the origin, and (b) still holds an un-`close()`d SerialPort handle, so even a promoted tab's `open()` (or a direct connect in another tab) would fail. There is no re-campaign path, so re-plugging the device recovers nothing.

The NaviCore copy at c:/Users/ghulette/Documents/GitHub/NaviCore/config_tool/serial-hub.js:373-381 has the fix and its comment records the observed failure verbatim: "Merely announcing here left the board unreachable from EVERY tab on the origin (they all sit in the lock queue behind us) until the user disconnected this tab by hand." This is the second lockstep divergence between the two required-identical copies.

**Failure scenario.** Wizard tab leads the shared port on WCB 1; a NaviCore tab follows. The user bumps the USB cable / the WCB browns out and re-enumerates. The Wizard's read loop ends, the board card flips to "Not Connected", and the WCB reappears in getPorts(). Neither tab ever gets it back: the Wizard still holds the Web Lock and the stale OS handle, so the NaviCore tab stays queued and a fresh direct connect in a third tab fails to open the port. The user must close the Wizard tab (or manually disconnect the board) to recover.

**Suggested fix.** Match NaviCore: inside that block do `this._log('port read loop ended (unplug?)'); await this._closePort(); this._stepDownPortless();` — closing frees the OS handle so a promoted tab can open, and the step-down releases the lock and re-queues on the jittered backoff so the device is picked up when it returns.

### F-149 · S2 · `c:/Users/ghulette/Documents/GitHub/Wireless_Communication_Board-WCB/Wizard/serial-hub.js:501`

**A promoted leader broadcasts the PREVIOUS leader's port identity, defeating the Wizard's wrong-board guard**

*Category:* `protocol-contract`

`_leaderPortInfo` is dual-purpose. It is set to OUR port's identity by `requestPort()` (serial-hub.js:162) and `adoptPort()` (serial-hub.js:177), but it is also unconditionally overwritten by any peer leader's announcement:

```js
case 'state':
  if (this._role !== 'leader') {
    ...
    if (msg.portInfo) this._leaderPortInfo = msg.portInfo;   // :501
```

`_openAndRead()` never refreshes it from `this._port.getInfo()` before announcing — it goes straight from `this._portOpen = true` (:367) to `_announceState()` (:368), which posts `portInfo: this._leaderPortInfo` (:524) and emits the same value to local listeners (:536). So a tab that picked port P1, was outvoted into follower, absorbed a leader's P2 identity, and is later promoted will open P1 while telling the whole origin the shared port is P2.

This directly poisons the Wizard's own wrong-board protection: Wizard/app.js:5700-5720 waits for `jhub._leaderPortOpen` and then compares the user's picked `port.getInfo()` against `jhub._leaderPortInfo`, aborting the join on a VID/PID mismatch specifically to avoid "silently binding this slot to a DIFFERENT board". With a poisoned announcement that comparison is made against a value that does not describe the port actually open. It also mis-seeds `_adoptGrantedPort()`'s `want` (serial-hub.js:322) in every other tab, so a later failover matches on the wrong identity.

**Failure scenario.** Tab A (NaviCore) already leads shared port P2. In the Wizard the user clicks "Share port" on WCB 3 and picks P1 in the picker — `sharedConnect()` calls `hub.requestPort()` (Wizard/app.js:5766) and only then `join()`, so the Wizard becomes a follower and tab A's 'state' overwrites `_leaderPortInfo` with P2's info. The user closes tab A. The Wizard promotes, opens P1 (the port it actually holds), and announces `portInfo = P2`. A third tab joining now believes the shared session is on P2; its wrong-board guard compares against P2 and happily binds a slot for the wrong physical board.

**Suggested fix.** Refresh the identity from the port we actually opened before announcing: in `_openAndRead()` just before `this._announceState()` (serial-hub.js:368) add `try { this._leaderPortInfo = this._port.getInfo ? this._port.getInfo() : this._leaderPortInfo; } catch (_) {}`. Better still, split the field into `_myPortInfo` (ours) and `_leaderPortInfo` (the peer's) so a peer announcement can never clobber our own identity. Mirror into the NaviCore copy.

### F-150 · S2 · `Wizard/app.js:628`

**addBoardSection rebuilds a section with factory defaults when a config already exists but the board is not USB-connected**

*Category:* `dom`

`addBoardSection` preserves an existing `boardConfigs[n]` (app.js:616-619) but only pushes it into the freshly built DOM when a **direct USB** connection is live:

```js
628:  if (boardConnections[n]?.isConnected?.()) {
629:    populateUIFromConfig(n, boardConfigs[n]);
630:    updateConnectionUI(n, true);
631:  }
```

A relay-managed board has no `boardConnections` entry at all — `setRemoteConnected` sets only `remoteRelayForBoard[n]` and touches DOM (app.js:7120-7180); it never constructs a BoardConnection. So the guard misses the remote case entirely, and any slot whose config exists while its section is being (re)built renders with `renderSerialTable`'s defaults (9600, both broadcasts checked, blank labels) plus the untouched template defaults for Kyber/Maestro/HCR/MP3/WLED.

This is reachable: `applyRelayRole(n)` removes the section directly (`document.getElementById('section-board-'+n)?.remove()`, app.js:480) while deliberately keeping `boardConfigs[n]` (renderRelayCard reads it at app.js:543). When that relay is later disconnected, `boardDisconnect` deletes `boardConnections[n]`, clears `_relaySlots`, and calls `reconcileBoardGrid()` (app.js:6212-6217) — the slot re-enters `want` via the WCBQ floor, `have` has no section for it, so `addBoardSection(n)` runs with `boardConfigs[n]` populated and no connection. The card shows factory defaults over a real config.

That divergence is not cosmetic, because the push path reads the DOM, not the config: `boardPushConfig` runs `syncSerialUIToConfig(n); syncMaestrosToConfig(n); syncKyberToConfig(n); syncMP3ToConfig(n); syncHCRToConfig(n); syncDFPToConfig(n); syncWLEDsToConfig(n);` (app.js:7388-7394) before building the command string.

**Failure scenario.** User connects a MgmtRelay board on slot 1; its config is pulled and it renders as a relay card (numbered section removed). User clicks Disconnect on the relay card. `reconcileBoardGrid()` re-creates section-board-1, which now shows 9600 on all five ports, no labels, Kyber = None, no Maestros — while `boardConfigs[1]` still holds the relay's real 115200/labelled/Maestro-bearing config and `boardBaselines[1]` holds the pulled state. The user reconnects that board over USB and clicks Push Config before the auto-pull lands: `syncSerialUIToConfig` reads the blank card, and the diff against the stale baseline emits `?BAUD,Sx,9600`, `?LABEL,CLEAR,...` and Maestro clears — writing the empty card over a board that was correctly configured.

**Suggested fix.** Populate unconditionally when a config already exists: replace the guard with `if (boardConfigs[n]) populateUIFromConfig(n, boardConfigs[n]);` and keep `updateConnectionUI(n, true)` behind the connection test, adding a `remoteRelayForBoard[n]` branch that calls `setRemoteConnected(n, remoteRelayForBoard[n])` so a relay-managed slot regains its remote UI too.

### F-151 · S2 · `Wizard/app.js:737`

**S3-S5 baud dropdown is capped at 57600, so any higher configured baud blanks the select and is silently rewritten to 9600**

*Category:* `round-trip-loss`

`renderSerialTable` builds each port's baud `<select>` once, with a hard cap for ports 3-5:

```js
737:    const maxBaud = p >= 3 ? 57600 : Infinity;
738:    const baudOptions = BAUD_RATES.filter(b => b <= maxBaud).map(b => ...
```

The firmware imposes no such per-port cap: `updateBaudRate()` (Code/WCB/WCB_Storage.cpp:146-180) accepts 110…256000 for **any** port 1-5 and re-begins S3-S5 software serial live (`applyLiveBaud`, WCB_Storage.cpp:166). So `serialPorts[2..4].baud` can legitimately hold 115200/128000/256000.

Two in-scope writers put such a value into that select. `onKyberBaudChange` mirrors the Kyber baud into the serial port and the DOM:

```js
972:    config.serialPorts[portVal - 1].baud = baud;
973:    const serialBaudEl = document.getElementById(`b${n}-s${portVal}-baud`);
974:    if (serialBaudEl) serialBaudEl.value = baud;
```

and the Kyber baud select in index.html:640-645 offers 115200 and has it `selected` by default, while `updateKyberPortDropdown` (app.js:979-999) offers Serial 1-5 with no cap. `populateUIFromConfig` does the same for a pulled config (app.js:2968: `if (el('baud')) el('baud').value = sp.baud;`).

Assigning a value with no matching `<option>` leaves the select with `selectedIndex === -1` and `.value === ''`. `syncSerialUIToConfig` then reads it back unconditionally — the hard-claim guard below it protects only the broadcast checkboxes, not baud:

```js
2908:    config.serialPorts[p - 1].baud = parseInt(document.getElementById(`b${n}-s${p}-baud`)?.value) || 9600;
```

`parseInt('')` is NaN, so the config is rewritten to 9600 and `?BAUD,S<p>,9600` is emitted (parser.js:1314). `syncSerialUIToConfig` runs on every serial-field edit (`onSerialFieldChange`, app.js:1872-1875) and unconditionally at push time (app.js:7388), and it loops all five ports — so editing an unrelated port's label destroys port 3's baud. Every other baud select in the file guards this with `Math.min(curBaud, maxBaud)` and a rebuild on port change (app.js:3251-3253, 3328-3336, 2682-2684, 10590-10593); `renderSerialTable` is the one that does not.

**Failure scenario.** Board card → Kyber → Local, Kyber Maestro Port = Serial 3, Kyber Baud = 115200. `onKyberBaudChange` sets `serialPorts[2].baud = 115200` and assigns 115200 to the S3 baud select, which has no such option — the S3 baud cell renders blank. Push succeeds (`?BAUD,S3,115200`). The user then types a label on Serial 1 and tabs out: `onSerialFieldChange(n)` → `syncSerialUIToConfig(n)` reads the blank S3 select and writes 9600 into the config. The next push sends `?BAUD,S3,9600`; the Kyber Maestro link on S3 drops to 9600 and stops responding. The same blanking happens on a plain Pull of any board whose S3-S5 baud is above 57600.

**Suggested fix.** Build the row's options from the actual configured value: clamp with `Math.min(sp.baud, maxBaud)` and include the configured baud as an option when it exceeds the cap (or drop the S3-S5 cap, since the firmware accepts the full table). Also make `syncSerialUIToConfig` fall back to `config.serialPorts[p-1].baud` rather than 9600 when the select reads empty.

### F-152 · S2 · `Wizard/app.js:855`

**MP3 / HCR / DFPlayer port dropdowns are never refreshed when another feature frees a port**

*Category:* `dom`

`updatePortClaimUI(n)` is the shared "a claim changed" hook — it is called from every device handler after `WCBParser.evaluatePortClaims(config)`. Its tail (app.js:853-856) refreshes only two of the six port pickers:

```
  // A claim change here may have freed or taken a port — re-filter the Maestro
  // and WLED dropdowns too so they stay consistent with HCR/MP3/Kyber/PWM.
  refreshAllMaestroPortDropdowns(n);
  refreshAllWLEDPortDropdowns(n);
```

Kyber is covered separately — `syncMaestrosToConfig` (app.js:3439) and `syncWLEDsToConfig` (app.js:2836) each call `updateKyberPortDropdown(n)` explicitly. But nothing calls `updateMP3PortDropdown`, `updateHCRPortDropdown` or `updateDFPPortDropdown` on these paths. Grepping every call site confirms it: `updateMP3PortDropdown` is referenced only at app.js:2196, 2208, 2248 (inside MP3's own three handlers) and app.js:3042 (the full `populateBoardUI` re-render). `updateHCRPortDropdown` only at 2564, 2577, 2603, 3084; `updateDFPPortDropdown` only at 2359, 2371, 2396, 3124. So a port freed by Maestro, WLED or Kyber never appears in the MP3/HCR/DFPlayer pickers until the user toggles that device's own mode or a full re-render happens.

The comment quoted above states the intent ("stay consistent with HCR/MP3") but the code does the opposite for exactly those two devices.

**Failure scenario.** Board has Kyber set to Local, which reserves S1 and S2 as `kyber-reserved` (parser.js:1096) so they are excluded from the MP3 picker by the `!claim || claim.type === 'mp3'` filter at app.js:2283. The user sets Kyber to None to free the hardware UARTs for an MP3 Trigger. `onKyberChange` re-evaluates claims and calls `updatePortClaimUI`, which refreshes Maestro and WLED but not MP3. The user opens the MP3 Trigger port dropdown and Serial 1 and Serial 2 are still missing, so the MP3 Trigger appears impossible to put on a hardware port. The same happens after deleting a Maestro row (removeMaestroRow -> onMaestroChange -> syncMaestrosToConfig -> updatePortClaimUI) to free a port for HCR or DFPlayer.

**Suggested fix.** Add `updateMP3PortDropdown(n); updateHCRPortDropdown(n); updateDFPPortDropdown(n);` next to the two existing refresh calls at app.js:855-856 (guarding on the elements existing, which those functions already do), so the single shared hook refreshes all six pickers and the comment becomes true.

### F-153 · S2 · `Wizard/app.js:962`

**Changing the Kyber serial port never marks the board unsaved — the only device port handler missing the dirty call**

*Category:* `logic`

`onKyberPortChange(n)` (app.js:928, bound to the Kyber port `<select>` at index.html:630) ends at app.js:958-962 with:

```
  WCBParser.evaluatePortClaims(config);
  updatePortClaimUI(n);
  updateKyberPortDropdown(n);
  updateKyberMarcPortDropdown(n);  // maestro port changed — re-filter marc dropdown
}
```

There is no `onBoardFieldChange(n)`. I checked every `on*Change` handler in app.js for a dirty-marking call and this is the only device handler that lacks one, directly or by delegation. Its own siblings all have it: `onKyberChange` app.js:925, `onKyberBaudChange` app.js:977, `onKyberMarcPortChange` app.js:1058. So do the parallel port handlers on every other device — `onMP3PortChange` app.js:2250, `onDFPPortChange` app.js:2398, `onHCRPortChange` app.js:2605, `onWLEDPortChange` -> `onWLEDChange` app.js:2791, `onMaestroPortChange` -> `onMaestroChange` app.js:3379.

`onBoardFieldChange` (app.js:1483) is the only thing that calls `_notifyBoardChanged(n)` (the "Changes pending — push to WCB n" toast) and `updateBoardStatusBadge(n, 'unsaved')`, which is in turn what `updatePushAllButton` (app.js:3158) reads to turn Push All amber. The value itself is still pushed if the user pushes anyway — `config.kyber.port` is set at app.js:945 and the `kyber` object is diffed at parser.js — so this is a lost dirty indication, not a lost value.

**Failure scenario.** User moves the Kyber Maestro from Serial 1 to Serial 2 using the Kyber port dropdown. The port claim and label update in the serial table, so the change clearly took effect visually, but no "Changes pending" toast appears, the board's unsaved badge stays hidden, and the Push All button stays green. A user who relies on the amber indicator to know what still needs pushing leaves without pushing, and the board keeps driving Kyber on the old port.

**Suggested fix.** Add `onBoardFieldChange(n);` as the last statement of `onKyberPortChange` at app.js:962, matching onKyberChange:925, onMP3PortChange:2250, onDFPPortChange:2398 and onHCRPortChange:2605.

### F-154 · S2 · `Wizard/app.js:1219`

**ETM Message Count defaults to 3, below the firmware's clamp floor of 10 — the value can never round-trip**

*Category:* `round-trip-loss`

`onGeneralETMChange` reads the message count with a fallback of 3:

```js
1219:  const count   = parseInt(document.getElementById('g-etm-count')?.value)    || 3;
```

and index.html:177 ships the input with `value="3"`. Nothing populates that field on a fresh load (`initSystemConfig` only calls `renderBoards`/`refreshControllerUI`, app.js:311-315), so 3 is what the field holds until a pull runs `set('g-etm-count', config.etm.messageCount)` (app.js:8114).

The firmware clamps this value to a minimum of 10 in both command paths:

```
Code/WCB/WCB.ino:4696   etmCharMessageCount = constrain(etmVal.toInt(), 10, 200);
Code/WCB/WCB.ino:5303   etmCharMessageCount = constrain(message.substring(12).toInt(), 10, 200);
```

and echoes the stored value back in the backup (`emit("ETM,COUNT," + String(etmCharMessageCount), true)`, WCB.ino:2871). The Wizard's own default is 20 everywhere else — `parser.js:124` and `parser.js:178` (`messageCount: 20`), `extractGeneralFields` at app.js:1426 (`config.etm?.messageCount ?? 20`), and the firmware's own default (`int etmCharMessageCount = 20;`, WCB.ino:269). Only this handler and the HTML say 3.

All six ETM inputs are wired `oninput="onGeneralETMChange()"` (index.html:173-178), so touching any one of them latches 3 into `systemConfig.general.etm.messageCount` and into every `boardConfigs[n].etm.messageCount` (app.js:1234), and refreshes `generalBaseline` from it (`updateGeneralBaseline`, app.js:1177-1180). `buildCommandString` then emits `ETM,COUNT,3` (parser.js:1493); the board stores 10 and reports 10. Because the Wizard's value (3) can never equal what the board can store (≥10), the diff never converges.

**Failure scenario.** Fresh Wizard load (no pull yet). User switches to Advanced, opens ETM, and nudges "Heartbeat Interval" from 10 to 8. `onGeneralETMChange` fires and reads Message Count as 3. User pushes to WCB 1. Firmware clamps and stores 10. The user pulls WCB 1 to verify: `getGeneralMismatches` compares baseline `etmCount: 3` against the board's 10 and pops the General Settings conflict modal for a network that is actually consistent; the board's Push Config button stays amber. Pushing again re-sends `ETM,COUNT,3`, the board clamps to 10 again, and the cycle repeats indefinitely.

**Suggested fix.** Change the fallback at app.js:1219 to `|| 20` and index.html:177 to `value="20"` (matching parser.js:124/178 and WCB.ino:269), and clamp the input to the firmware's range with `min="10" max="200"` so the UI cannot offer a value the board will silently rewrite.

### F-155 · S2 · `Wizard/app.js:1663`

**boardOtaSerial's post-OTA reboot handling has no `_shared` guard, so a successful OTA on a shared-hub port leaves the board card stuck at "Not connected"**

*Category:* `dom`

After `[OTA:END,OK]` the serial OTA tears its own port down and reconnects:

```js
conn._rebootManaged = true;
…
await conn.closeForReconnect();   // clean teardown so reconnect() reopens fresh
updateConnectionUI(n, false);
…
const reconnected = await conn.reconnect(_isWindows ? 14 : 12, 2000);
if (!reconnected) { updateConnectionUI(n, false); … showToast(`WCB ${n} did not come back after OTA…`, 'error'); return; }
```

A shared-hub board has **no SerialPort of its own** — `connectShared` (Wizard/app.js:5413-5440) sets `_shared`/`_hub` and never assigns `this.port`, which stays `null` from the constructor (:4920). `reconnect()` begins with `if (!this.port) return false;` (:4997), so it returns false *immediately*, before a single retry.

The result: `updateConnectionUI(n, false)` runs (twice), which per Wizard/app.js:7627-7641 sets the dot off, the label to "Not connected", and disables Pull / Push / Erase / ETM-char / Stats. Meanwhile `isConnected()` still returns true for a shared board (`return this._shared ? !!this._hub?.portOpen : this._connected;` — :4935), so the state is internally inconsistent and the OTA's follow-up `waitForBoardReady` / `boardPull` never run.

The config-push reboot path already handles exactly this and documents why (Wizard/app.js:7503-7521):

```js
if (conn._shared) {
  // … Running the direct closeForReconnect()+reconnect() here would operate on a portless
  // shared conn (this.port===null) and falsely report "did not come back". Stay Connected.
  conn._rebootManaged = false;
  termLog(n, 'Board rebooting on the shared port…', 'sys');
} else { … }
```

The OTA path was written later and never picked up that guard. Nothing gates the OTA button on `_shared` — `boardOta` only checks `remoteRelayForBoard[n]` (:1516) and `boardOtaSerial` only checks `conn?.isConnected()` (:1535), both of which pass for a shared board.

**Failure scenario.** Click "Share port across tabs" for WCB 1, then click "⬆ OTA" on that board. The transfer runs at 115200 (the baud raise is skipped because `conn.port` is null — :1581) and completes: `[OTA] ✅ verified …` and the success toast appear. Immediately afterwards the card flips to "Not connected", Pull/Push/Erase/Stats go grey, and an error toast reads "WCB 1 did not come back after OTA — reconnect manually" — even though the OTA succeeded and the hub still owns a live port. No post-OTA verify or config pull happens. The card stays wedged until the hub emits a fresh `state` event or the user re-shares.

**Suggested fix.** Mirror the push path: wrap :1663-1688 in `if (conn._shared) { conn._rebootManaged = false; termLog(n, 'Board rebooting on the shared port…', 'sys'); setTimeout(() => { waitForBoardReady(n, conn).then(ok => ok && boardPull(n).catch(()=>{})); }, 5000); } else { …existing closeForReconnect/reconnect… }`. A UART-bridge WCB keeps USB up through a software reboot, so the hub's read loop resumes on the same port and nothing needs closing.

### F-156 · S2 · `Wizard/app.js:1863`

**Post-OTA ETM-edge replay fires every deferred remote pull in parallel on one relay**

*Category:* `async`

`boardOtaRelay`'s `finally` replays the ETM edges it swallowed during the transfer:

```js
for (const bn of boards) {
  if (bn === n || bn === targetWcb) continue;
  const r = remoteRelayForBoard[bn];
  if (!r || !boardConnections[r]?.isConnected()) continue;
  ...
  _pullingBoards.delete(bn);
  remoteBoardPull(r, bn);          // no await, no serialisation
  startRemoteTermSession(r, bn);
}
```
(app.js:1857-1865). `remoteBoardPull` is `async` (app.js:7900) and is called with neither `await` nor `.catch`, inside a `finally` — so every deferred board's pull is launched simultaneously against the SAME relay connection.

Two consequences, both verified against the firmware:
1. It creates exactly the multi-listener condition of the unfiltered `[MGMT:CONFIG,` handler (app.js:7938) — with 3 deferred boards, all 3 slots receive whichever single config the relay manages to reassemble.
2. The relay firmware holds ONE `pullSession` struct; a new `CONFIG_REQ` `memset`s it (Code/WCB/WCB.ino:3060), so N simultaneous `?MGMT,PULL` requests destroy each other's reassembly and most produce no reply at all.

The deferral itself makes multiple entries the NORMAL case: the code that populates `_suppressedEtmEdges` comments that "Offline edges are EXPECTED noise while an OTA saturates the channel" (app.js:7727-7728), so a long wireless OTA routinely defers several boards.

**Failure scenario.** Four WCBs are managed through one relay. The user runs a wireless OTA on WCB4 (~2-4 minutes). During the transfer the mesh saturates and WCB2, WCB3 and WCB5 each emit an ETM offline edge, which `installEtmListener` defers into `_suppressedEtmEdges` (app.js:7730). When the OTA finishes, the `finally` at app.js:1854 fires `remoteBoardPull` for all three at once. The relay's single pullSession is reset by each incoming request; one config comes back and, via the unfiltered listener, is written into all three slots — and `startRemoteTermSession` is also fired three times back-to-back into the same relay. The user sees three boards that now claim identical configuration.

**Suggested fix.** Serialise the replay the same way `relayRouteAll` already does (app.js:531-537): make the surrounding scope able to await — e.g. `(async () => { for (const bn of boards) { ...; await new Promise(res => remoteBoardPull(r, bn, 1, MAX_PULL_ATTEMPTS, res).catch(() => res(false))); await sleep(MGMT_CHUNK_DELAY); startRemoteTermSession(r, bn); } })();` — so only one `?MGMT,PULL` is outstanding at the relay at any time.

### F-157 · S2 · `Wizard/app.js:2003`

**populateUIFromConfig calls onHWVersionChange, which silently rewrites the pulled LED pin (HW 3.2 @ GPIO38 → 48) and marks the board dirty**

*Category:* `round-trip-loss`

`onHWVersionChange` contains a *manual-edit* heuristic that re-defaults the status-LED pin when the user switches HW variants:

```js
const cur = boardConfigs[n].statusLedPin;
const def = (hwVal === 32 && (cur === 38 || cur == null)) ? 48
          : (hwVal === 31 && (cur === 48 || cur == null)) ? 38 : null;
if (def !== null) { boardConfigs[n].statusLedPin = def; ... }
```

`populateUIFromConfig` reuses that handler while *loading a pulled config* (Wizard/app.js:2946 — `if (hwSel) { hwSel.value = config.hwVersion || 0; onHWVersionChange(n); }`), and by then `boardConfigs[n] === config` (set at Wizard/app.js:6916, :6884, :7984).

The firmware's pin map sets `STATUS_LED_PIN = 38` for **both** HW 3.1 and 3.2 (Code/WCB/wcb_pin_map.cpp:112, the `wcb_hw_version == 32 || wcb_hw_version == 31` branch), and `loadStatusLEDPin()` only overrides it when NVS holds a value (Code/WCB/WCB_Storage.cpp:115-124). So a stock HW-3.2 board emits `LED,PIN,38` in its backup (Code/WCB/WCB.ino:2780). On pull the Wizard rewrites that to 48.

The baseline is deep-copied *before* populate runs (`boardBaselines[n] = JSON.parse(JSON.stringify(config))` at :6917, :6890, :7985), so baseline.statusLedPin stays 38 while config.statusLedPin becomes 48. `buildCommandString` then emits the delta (Wizard/parser.js:1245-1250: `if (fullPush || ledPin !== bLedPin) add(`LED,PIN,${ledPin}`)`).

`onHWVersionChange` also unconditionally calls `onBoardFieldChange(n)` (Wizard/app.js:2012), which fires `_notifyBoardChanged` → a spurious "Changes pending — push to WCB N" toast on *every* pull, plus an `unsaved` badge (later overwritten by the caller's `updateBoardStatusBadge(n,'connected')`).

The Wizard's own tooltip contradicts the heuristic too — index.html:552 says "Default 38. Common alternatives: 48, 47."

**Failure scenario.** Connect a stock HW 3.2 WCB (NeoPixel on GPIO38, no `?LED,PIN` ever sent) → click Pull Config. The board replies `?LED,PIN,38`; the LED-pin dropdown now shows 48 and a "Changes pending" toast appears. Click Push Config (or Push All). The Wizard sends `?LED,PIN,48`; the firmware saves 48 to NVS and re-inits the NeoPixel on GPIO48 (Code/WCB/WCB.ino:5135-5138), so the board's status LED goes dark. Every subsequent pull re-introduces the same drift.

**Suggested fix.** Do not run the interactive re-default from the load path. Either split the pin-defaulting out of `onHWVersionChange` into e.g. `onHWVersionChangeUser(n)` bound to the `<select>`, and have `populateUIFromConfig` call a pure `applyHWVersionUI(n)` that only toggles visibility; or gate the block on a flag (`onHWVersionChange(n, {fromLoad:false})`) so populate passes `fromLoad:true` and skips both the `statusLedPin` rewrite and the `onBoardFieldChange(n)` call.

### F-158 · S2 · `Wizard/app.js:2236`

**MP3 baud picker lets the user set values the firmware rejects outright — the whole `?MP3,…` line is silently dropped**

*Category:* `protocol-contract`

`onMP3PortChange` caps the MP3 baud at **57600** on the software-serial ports:

```js
// Cap baud at 57600 for software serial ports (S3–S5)
const isSoftSerial = portVal >= 3;
for (const opt of baudSel.options) { opt.hidden = isSoftSerial && parseInt(opt.value) > 57600; }
if (isSoftSerial && parseInt(baudSel.value) > 57600) { baudSel.value = '57600'; config.mp3.baud = 57600; }
```

The firmware disagrees on both counts (Code/WCB/WCB_MP3.cpp):
- line 287-290: `if (baudRate != 9600 && baudRate != 38400) { Serial.println("[MP3] MP3 Trigger only supports 9600 or 38400 baud"); return; }`
- line 292-303: `if (serialPort >= 3 && baudRate > 9600) { … "❌ CONFIGURATION BLOCKED!" … return; }`

Both are early `return`s — the *entire* `?MP3,S<p>:<baud>:V<v>` command is discarded, not just the baud. Yet index.html:810-822 offers 13 baud values (110 … 256000) and `onMP3BaudChange` (Wizard/app.js:2255) / `syncMP3ToConfig` (:2304) accept any of them, and `buildCommandString` emits them verbatim (Wizard/parser.js:1364).

The same wrong 57600 cap is duplicated in `populateUIFromConfig` (Wizard/app.js:3049-3053).

This is a copy-paste divergence: the HCR path is correct. `_hcrApplyBaudCap` (Wizard/app.js:2466-2489) clamps to **9600** on S3-S5, matching Code/WCB/WCB_HCR.cpp:635, and the HCR dropdown's five values exactly match the firmware's accept list at WCB_HCR.cpp:631-633. MP3 was never brought in line.

The push reports success regardless: `sendAndAwaitIdle` (Wizard/app.js:5265) returns `ok: lines.length > 0`, and the firmware's rejection *is* output, so the Wizard counts the command as delivered.

**Failure scenario.** Enable MP3 Trigger on a board, set Baud to 38400 (a value the firmware does accept), then move MP3 Port to Serial 3. `onMP3PortChange` leaves 38400 selected (38400 ≤ 57600, so no clamp). Push Config → the Wizard sends `?MP3,S3:38400:V0`; the firmware prints "CONFIGURATION BLOCKED!" and returns without configuring anything. The push shows green, the board card shows MP3 on S3, but the board has no MP3 configured and `;A` commands do nothing. Same outcome for 19200/57600/115200/128000/256000 on *any* port (rejected by the 9600-or-38400 gate).

**Suggested fix.** Reduce the `b{N}-mp3-baud` option list in index.html to the two values the firmware accepts (9600, 38400), and change the S3-S5 clamp in `onMP3PortChange` (:2233-2242) and in `populateUIFromConfig` (:3049-3053) from 57600 to 9600, mirroring `_hcrApplyBaudCap`. Also mirror the clamped baud into `config.serialPorts[portVal-1].baud` and `b{n}-s{port}-baud` as `_hcrApplyBaudCap` does, so a later `?BAUD` push can't contradict the `?MP3` line. Update the tooltip at index.html:808 ("Serial 3–5 are capped at 57,600").

### F-159 · S2 · `Wizard/app.js:2305`

**syncMP3ToConfig re-reads the raw MP3 volume input, discarding onMP3VolChange's clamp — an out-of-range value makes the firmware reject the whole MP3 config**

*Category:* `protocol-contract`

`onMP3VolChange` clamps into the config object but never writes the clamped value back to the input:

```js
let v = parseInt(document.getElementById(`b${n}-mp3-vol`)?.value);
if (isNaN(v)) v = 0;
v = Math.max(0, Math.min(64, v));
config.mp3.volume = v;   // DOM still holds the raw text
```

`syncMP3ToConfig` — which runs on *every* push, after `syncSerialUIToConfig` (Wizard/app.js:7391 and :7783) — then re-reads the un-clamped DOM value and overwrites the clamp:

```js
config.mp3.volume  = parseInt(document.getElementById(`b${n}-mp3-vol`)?.value) ?? 0;
```

Note `??`, not `||`: `parseInt('')` is `NaN`, and `NaN ?? 0` is `NaN`, so an emptied field yields `volume: NaN` (which `JSON.stringify` serialises as `null` in the exported system file and in the baseline diff).

The firmware rejects out-of-range volume with an early `return` that discards the whole command — Code/WCB/WCB_MP3.cpp:304-307: `if (volume < 0 || volume > 64) { Serial.println("[MP3] Volume must be 0-64 …"); return; }`.

This is a copy-paste divergence again: the DFPlayer sibling does it correctly — `syncDFPToConfig` (Wizard/app.js:2447-2449) re-applies `isNaN` + `Math.max(0, Math.min(30, v))`; and `onHCRPollChange` (:2621-2622) writes its clamp back to the DOM (`if (el && parseInt(el.value) !== v) el.value = v;`). Only the MP3 volume path has neither.

**Failure scenario.** Enable MP3 Trigger, type `200` into Default Volume and tab out. `onMP3VolChange` stores 64 in the config and shows no error; the box still reads 200. Click Push Config. `syncMP3ToConfig` reads 200 back over the 64, and the Wizard sends `?MP3,S2:9600:V200`. The firmware prints "Volume must be 0-64" and returns — MP3 is left completely unconfigured (or at its previous setting) while the push reports success and the board card shows "MP3 on S2". Clearing the field instead produces `?MP3,S2:9600:VNaN` and a `"volume": null` entry in any exported system file.

**Suggested fix.** Make `syncMP3ToConfig` mirror `syncDFPToConfig`: `let v = parseInt(document.getElementById(`b${n}-mp3-vol`)?.value); if (isNaN(v)) v = 0; config.mp3.volume = Math.max(0, Math.min(64, v));`. Additionally have `onMP3VolChange` write the clamped value back to the input (as `onHCRPollChange` does) so the user sees the correction.

### F-160 · S2 · `Wizard/app.js:3246`

**Maestro ID dropdown offers 9, which the firmware rejects as a reserved routing target**

*Category:* `protocol-contract`

`appendMaestroRow()` builds the ID select from 1..9:
```js
const idOptions = Array.from({length: 9}, (_, i) => i + 1).map(v => ...)
```
The firmware's `?MAESTRO,M<id>:W<wcb>S<port>:<baud>` parser rejects 9 outright:
```cpp
if (maestroID < 1 || maestroID > 8) {
  Serial.println("Invalid Maestro ID. Must be 1-8 (9=all local, 0=all Maestros are reserved)");
  startIdx = nextComma + 1;
  continue;
}
```
(Code/WCB/WCB_Maestro.cpp:578-581). ID 9 is the "all local Maestros" fan-out target at command time (`if (maestroID == 9)`, WCB_Maestro.cpp:190) and is never storable as a slot.

The row nevertheless flows all the way to the wire: `syncMaestrosToConfig` pushes `{ id, port, baud }` into `config.maestros` (app.js:3424), `rebuildMaestroRoutingTables` copies it into **every** board's `maestroTable` (app.js:3355, :3364), and the push emits it verbatim — `` .map(m => `M${m.id}:W${m.wcb}S${m.port}:${m.baud}`) `` (Wizard/parser.js:1476). Every board in the fleet receives an `M9:...` entry it discards with a console error.

The Wizard's own UI already documents the correct range: the column tooltip in Wizard/index.html:697 reads "Maestro device ID 1-8 (set in the Pololu Maestro Control Center)". The dropdown and the tooltip disagree.

**Failure scenario.** User adds a Maestro row on WCB1, picks ID 9 from the dropdown, picks Serial 2, 115200, and clicks Push Config. The Wizard shows the row, marks the board configured, and propagates the routing entry to every other board. The firmware prints "Invalid Maestro ID. Must be 1-8" and stores nothing. `;M9<script>` then fans out to whatever Maestros already exist rather than the intended device, and the next pull of WCB1 silently drops the row the user just created.

**Suggested fix.** Change to `Array.from({length: 8}, (_, i) => i + 1)` so the dropdown matches WCB_Maestro.cpp:578 and the index.html:697 tooltip. (The Kyber-target row builder at app.js:8953 and the row builder at app.js:10151 use the same 1..9 expression and should be checked against the same firmware limit.)

### F-161 · S2 · `Wizard/app.js:3246`

**Maestro ID dropdown offers 9, which `configureMaestro` rejects — the row saves in the UI but never reaches the board**

*Category:* `protocol-contract`

`appendMaestroRow()` builds the Maestro ID picker as `Array.from({length: 9}, (_, i) => i + 1)` — i.e. 1 through 9 (Wizard/app.js:3246-3248). The firmware rejects 9: `if (maestroID < 1 || maestroID > 8) { Serial.println("Invalid Maestro ID. Must be 1-8 (9=all local, 0=all Maestros are reserved)"); startIdx = nextComma + 1; continue; }` (Code/WCB/WCB_Maestro.cpp:578-582). ID 9 is a reserved routing target ("all local Maestros"), never a storable slot — the same rule the targeted-clear path enforces at WCB_Maestro.cpp:817-820.

`buildCommandString` always emits `?MAESTRO,<chain>` for local Maestros regardless of Kyber mode (Wizard/parser.js:1547-1551, with an explicit comment saying so), so the M9 entry rides the chain and is dropped by the `continue`. Because `configureMaestro` loops over comma-separated entries, only the M9 entry is skipped — the rest apply, so there is no visible failure, just a missing slot.

The Kyber-target ID picker has the same 1-9 range (Wizard/app.js:8953) and the Kyber parser DOES accept 9 (`maestroID >= 1 && maestroID <= 9`, Code/WCB/WCB_Storage.cpp:1249), so the two paths disagree about the same ID.

**Failure scenario.** User adds a Maestro row, picks ID 9 (offered by the dropdown), assigns it S1 @ 9600, and pushes. The board prints "Invalid Maestro ID. Must be 1-8" and stores nothing. The Wizard's Maestro table still shows the row. `;M9<n>` continues to mean "all local Maestros" rather than addressing this device, so the servo controller never responds and the next pull quietly drops the row.

**Suggested fix.** Change the Maestro ID options at app.js:3246 to `Array.from({length: 8}, (_, i) => i + 1)` and add a note that 9 = all-local / 0 = all-Maestros are reserved routing targets. Apply the same 1-8 limit to the Kyber-target ID picker at app.js:8953, since each Kyber target also creates a `?MAESTRO` slot.

### F-162 · S2 · `Wizard/app.js:3594`

**syncMappingsToConfig() wipes config.pwmOutputPorts and never repopulates it**

*Category:* `logic`

`syncMappingsToConfig()` clears both arrays up front:
```js
config.mappings       = [];
config.pwmOutputPorts = [];
```
It then rebuilds `config.mappings` from the DOM rows (`container.querySelectorAll('[id^="map-row-"]')`, app.js:3599-3618) but **nothing ever writes back into `config.pwmOutputPorts`**. A grep of the whole Wizard confirms the only other producer is the pull parser (`config.pwmOutputPorts.push(port)`, Wizard/parser.js:886) — so after any mapping edit the array is permanently empty until the next pull.

That array is load-bearing in three places:
1. `evaluatePortClaims()` claims those ports — `for (const port of config.pwmOutputPorts) { ... claimedBy = { type: 'pwm' } }` (parser.js:1060-1066). It is called on the very next line (app.js:3620), so the claim is released immediately and the UART shows as free.
2. `appendPWMOutputGhostRows()` reads it (`if (!config?.pwmOutputPorts?.length) return;`, app.js:4027) — the read-only ghost row that is the *only* UI explaining why the port is locked stops rendering.
3. The push diff: `const pwmChanged = ... JSON.stringify(baseline.pwmOutputPorts) !== JSON.stringify(config.pwmOutputPorts)` is now true, but the emit loop `for (const port of config.pwmOutputPorts) add(`MAP,PWM,OUT,S${port}`)` (parser.js:1500-1506) iterates an empty array — so the board is never told either. The declaration silently survives on the board while the Wizard believes it is gone.

This also reaches across boards: `applyBidirMapping` calls `syncMappingsToConfig(wcb)` (app.js:3725) and `_removeBidirRows` calls `syncMappingsToConfig(bn)` (app.js:3675), so editing a bidir mapping on one board wipes a *different* board's PWM output declarations.

**Failure scenario.** WCB2 has a PWM mapping driving WCB3's S4, so WCB3's pull yields `?MAP,PWM,OUT,S4` and its Mappings section shows the locked ghost row "PWM Output S4 ← Managed by WCB 2". On WCB3 the user adds any serial mapping row and picks a source port. `syncMappingsToConfig(3)` runs, `pwmOutputPorts` becomes `[]`, `evaluatePortClaims` releases S4, the ghost row disappears on the next render, and S4 is now offered in the HCR/MP3/Maestro port dropdowns. The user assigns HCR to S4 and pushes; the board is now both a PWM output and an HCR port on the same UART.

**Suggested fix.** Do not clear `config.pwmOutputPorts` in `syncMappingsToConfig` — it is not derived from the mapping rows, it is a separate board-declared fact. Delete the line, or snapshot and restore it around the rebuild (`const keepOut = config.pwmOutputPorts ?? [];` … `config.pwmOutputPorts = keepOut;`), and call `refreshPWMGhostRows(n)` after `updatePortClaimUI(n)` so the ghost rows stay in step.

### F-163 · S2 · `Wizard/app.js:3700`

**Bidirectional-mapping code indexes slot-keyed maps (boardConfigs / boardConnections / boardPull) with a WCB number**

*Category:* `logic`

`boardConfigs`, `boardConnections`, `boardBaselines` and `boardPull(n)` are keyed by board **slot**, not by the configured WCB number — the same function states this explicitly for the PWM path:
```js
// remoteRelayForBoard and boardConnections are keyed by board SLOT, not WCB number.
// Find the slot whose wcbNumber matches.
const removedSlot = parseInt(Object.keys(boardConfigs).find(idx => boardConfigs[idx]?.wcbNumber === removed.wcbNumber) ?? removed.wcbNumber);
```
(app.js:3890-3894; the same lookup is done again at app.js:4078-4081).

The bidir paths skip that lookup and use the raw destination WCB number as a slot key:
- `applyBidirMapping`: `if (!wcb || isNaN(port) || !boardConfigs[wcb]) return; const destCfg = boardConfigs[wcb];` then `destCfg.mappings.push(reverseMapping)`, `appendMappingRow(wcb, ...)`, `updateBoardStatusBadge(wcb, 'unsaved')` (app.js:3698-3726).
- `saveMappingRow`: `if (dest.wcbNumber > 0 && boardConfigs[dest.wcbNumber]) { const destCfg = boardConfigs[dest.wcbNumber]; const destWcb = dest.wcbNumber; ... const destRelayN = remoteRelayForBoard[destWcb]; const destConn = boardConnections[destWcb]; ... boardBaselines[destWcb].mappings = ...; setTimeout(() => boardPull(destWcb), 2000); }` (app.js:3928-3966).
- `saveMappingRow` PWM re-pull: `setTimeout(() => boardPull(dest.wcbNumber), 4000)` (app.js:3878).

Slot ≠ wcbNumber is a reachable, deliberately-supported state: the pull's conflict branch keeps a board in its original slot when the target slot is occupied — `showToast(`WCB ${detected} detected in slot ${n} — slot ${detected} is already connected, keeping here`)` followed by `boardConfigs[n] = config` where `config.wcbNumber === detected` (app.js:6875-6884) — and `onWCBNumberChange()` lets the user set `boardConfigs[n].wcbNumber = val` without moving slots (app.js:1882).

**Failure scenario.** Two boards are plugged in. The second self-identifies as WCB2 but slot 2 is already taken, so it stays in slot 3 with `wcbNumber = 2` (the documented "keeping here" branch). A different board occupies slot 2. On slot 1 the user creates a Serial mapping S1 → WCB2 S3 and ticks the bidir toggle. `applyBidirMapping` resolves `boardConfigs[2]` — the board in slot 2, NOT the board configured as WCB2 — pushes the reverse mapping into its config, renders the row on its card, and marks it unsaved. Push All then writes `?MAP,SERIAL,S3,W1S1` to the wrong physical board, while the intended board never gets its reverse mapping.

**Suggested fix.** Resolve slot from WCB number once at the top of each of these paths, exactly as app.js:3890-3894 does, and use the slot for `boardConfigs` / `boardConnections` / `boardBaselines` / `remoteRelayForBoard` / `boardPull` / `updateBoardStatusBadge` / `appendMappingRow`, keeping the raw WCB number only for the wire text (`W<n>S<p>`) and the MGMT target field. A small `slotForWcb(wcbNumber)` helper would let all five call sites share it.

### F-164 · S2 · `Wizard/app.js:3837`

**`?MAP,SERIAL` appends destinations in firmware, so editing or removing a destination in the Wizard leaves the old one live**

*Category:* `protocol-contract`

`saveMappingRow()` builds the full mapping command from the current DOM rows and sends it with no preceding clear: `let cmd = \`${lfi}MAP,${type.toUpperCase()},S${src}\`; … for (const dest of destinations) { cmd += dest.wcbNumber === 0 ? \`,S${dest.port}\` : \`,W${dest.wcbNumber}S${dest.port}\`; }` (Wizard/app.js:3837-3842), then `await conn.send(cmd + '\r')` (app.js:3857). `buildCommandString` does the same on Push Config (Wizard/parser.js:1512-1521) — no `MAP,…,CLEAR,S<src>` is ever emitted first.

But the firmware's serial-mapping handler is ADD-only. `addSerialMonitorMapping()` finds the existing mapping for that input port and reuses it without resetting the destination list: `for (…) { if (serialMonitorMappings[i].active && serialMonitorMappings[i].inputPort == inputPort) { mapping = &serialMonitorMappings[i]; break; } }` (Code/WCB/WCB_Storage.cpp:1636-1642) — `mapping->outputCount` is only zeroed when a brand-new slot is allocated (:1650). Destinations are then appended: `mapping->outputs[mapping->outputCount].wcbNumber = wcbNum; … mapping->outputCount++;` (WCB_Storage.cpp:1731-1736), with duplicates merely skipped (:1722-1729). Nothing removes an output.

This is specific to SERIAL. The PWM handler REPLACES — `mapping.outputCount = 0;` before the parse loop (Code/WCB/WCB_PWM.cpp:227) — and `saveMappingRow` does send explicit `?MAP,PWM,CLEAR,OUT,S<p>` for removed remote PWM destinations (app.js:3883-3897). The serial path has no equivalent. `removeMappingRow()` does send `?MAP,SERIAL,CLEAR,S<src>` (app.js:3641), but only when the whole row is deleted.

**Failure scenario.** A mapping exists on the board: S1 → S2, W2S3. The user opens the row, deletes the `S2` destination (leaving only W2S3), and clicks Save. The Wizard sends `?MAP,SERIAL,S1,W2S3`; the firmware sees S1 already mapped, finds W2S3 is a duplicate, skips it, and prints "No new destinations added". The board still fans S1 out to S2. Worse on a change: replacing `S2` with `S4` sends `?MAP,SERIAL,S1,S4`, which APPENDS S4 — S1 now feeds both S2 and S4, silently double-routing traffic to a port the user removed. The Wizard shows a green "Mapping saved" toast either way.

**Suggested fix.** Send `${lfi}MAP,SERIAL,CLEAR,S${src}` immediately before the rebuilt `?MAP,SERIAL,...` in `saveMappingRow()`, and have `buildCommandString` emit a `MAP,SERIAL,CLEAR,S<src>` for each serial mapping whose destination set differs from the baseline before re-adding it.

### F-165 · S2 · `Wizard/app.js:3866`

**PWM mapping Save schedules a config pull at 2 s, but the board blocks 3 s and reboots**

*Category:* `async`

After sending the `?MAP,<TYPE>,S<src>,...` command, `saveMappingRow()` unconditionally does:
```js
// Pull config back ~2 s after sending so the tools page reflects the new mapping
setTimeout(() => boardPull(n), 2000);
```
For `type === 'PWM'` this pull can never succeed. The firmware routes `?MAP,PWM,S<x>,<dest>` to `addPWMMapping(pwmConfig)` (Code/WCB/WCB.ino:4542) whose `autoReboot` parameter defaults to true (Code/WCB/WCB_PWM.h:38), and that function ends with:
```cpp
if (autoReboot) {
    Serial.println("Rebooting in 3 seconds to apply PWM configuration...");
    delay(3000);
    ESP.restart();
}
```
(Code/WCB/WCB_PWM.cpp:338-342). `delay(3000)` is a blocking busy-wait inside `loop()`, so from the moment the MAP command is processed the board services **no** serial input for 3 s, then reboots and loses whatever is in its RX buffer. The pull command sent at t≈2 s is therefore discarded, the pull times out, and `updateBoardStatusBadge(n,'error')` paints "✕ Pull Failed" on a board that in fact accepted the mapping.

The same function already knows this reboot exists and gets it right 50 lines later for the clear path: `// Board reboots ~3 s after receiving the clear then takes ~4-5 s to boot; pull after 10 s.` … `setTimeout(() => boardPull(removedSlot), 10000);` (app.js:3915-3917).

**Failure scenario.** User configures a PWM mapping on a connected WCB (source S1 → destination WCB2 S3) and clicks Save on the mapping card. Toast says "Mapping saved to WCB 1". Two seconds later the Wizard fires a pull that the board — mid `delay(3000)` and about to reboot — never answers. The board card flips to "✕ Pull Failed" and the tools page keeps stale port-claim state until the user manually reconnects and pulls.

**Suggested fix.** Make the pull delay type-aware, mirroring the clear path: `setTimeout(() => boardPull(n), type === 'PWM' ? 10000 : 2000);` — the `?MAP,SERIAL` path (`addSerialMonitorMapping`, WCB_Storage.cpp:1593) does not reboot, so 2 s remains correct there.

### F-166 · S2 · `Wizard/app.js:4092`

**seqValueToLines/seqTextareaToValue are not inverses: '^^***' standalone comments collapse, and a plain Push rewrites stored sequences**

*Category:* `round-trip-loss`

`seqTextareaToValue()` drops every empty line (`.filter(Boolean)`) before joining with the delimiter:
```js
return text.split('\n')
  .map(l => l.trim().replace(/\s+(\*\*\*)/, '$1'))
  .filter(Boolean)
  .join(delim);
```
But `seqValueToLines()` uses the *empty segment* produced by `^^` as the signal that a comment stands on its own line — its own header comment says so: "`^^***text` — standalone comment (double delimiter before ***): the empty segment produced by `^^` signals 'own line', so it is kept as a separate line" (app.js:4118-4120), and the implementation relies on `prevWasEmpty` (app.js:4141-4152). The empty segment is never regenerated on the way back, so the marker is destroyed.

I ran the two functions verbatim out of app.js (lines 4091-4096 and 4127-4175):
```
";M11^^***standalone note" => ";M11\n***standalone note" => ";M11^***standalone note"   LOSSY
";M11^***inline note"      => ";M11 ***inline note"      => ";M11***inline note"        LOSSY
";M11^^;M22"               => ";M11\n;M22"               => ";M11^;M22"                 LOSSY
```
The firmware deliberately preserves the distinction — `normaliseSeqComments()` protects `^^***` through the replacement and restores it, with the comment "`^^***` (standalone) is left untouched" (Code/WCB/WCB_Storage.cpp:849, :856-862).

This is not display-only. `getSequencesFromUI(n)` re-derives values from the textareas and assigns them to `config.sequences` before every push (app.js:4487-4497, called at app.js:7397, :7791, :8422), while the baseline holds the raw pulled value (Wizard/parser.js:838-846 stores it verbatim). The diff `baseSeqMap.get(seq.key) !== seq.value` (parser.js:1530) therefore fires on a board the user never edited, and `add(`SEQ,SAVE,${seq.key},${seq.value}`)` (parser.js:1531) rewrites the stored sequence.

**Failure scenario.** A board holds `wave` = `;M11^^*** raise the dome^;t500^;M12`. Pull it: the editor correctly shows `*** raise the dome` on its own line. Change nothing anywhere, click Push Config. The push emits `?SEQ,SAVE,wave,;M11^*** raise the dome^;t500^;M12` — the standalone comment is now an inline one. Pull again and the comment is displayed glued to `;M11`; push again and it degrades one more step to `;M11*** raise the dome`. The user's stored sequence has been rewritten twice without them touching it.

**Suggested fix.** Re-emit the standalone marker on the way out: in `seqTextareaToValue`, prefix a line that starts with `***` with an extra delimiter (or keep one empty segment for it) instead of relying on `.filter(Boolean)` — e.g. map a `***`-leading line to `delim + line` when it is not the first line, so `";M11\n***note"` returns `";M11^^***note"`. Add the three cases above as an assertion in the round-trip check so the pair stays an inverse.

### F-167 · S2 · `Wizard/app.js:5541`

**A relay's transient USB drop tears down every relay-managed remote board and the successful reconnect never rebuilds them**

*Category:* `logic`

When the read loop exits unexpectedly, `_startReading` unconditionally destroys the relay's whole remote-management state *before* it even attempts to reconnect:

```js
5538:    if (this._connected && !this._rebootManaged) {
5539:      this._connected = false;
5540:      updateConnectionUI(this.boardIndex, false);
5541:      clearRemoteBoardsForRelay(this.boardIndex);   // drop any remote boards using this as relay
5542:      termLog(this.boardIndex, 'Board disconnected — attempting reconnect…', 'sys');
5544:      const reconnected = await this.reconnect(10, 1500);
5545:      if (reconnected) {
5546:        updateConnectionUI(this.boardIndex, true);
...
5552:        if (!_wizardOpen) { ... setTimeout(() => boardPull(this.boardIndex), 3000); }
```

`clearRemoteBoardsForRelay` → `clearRemoteConnected` (Wizard/app.js:5995) deletes `remoteRelayForBoard[n]`, sends RTERM,STOP, re-enables the flash radios, and removes the relay's ETM listener (Wizard/app.js:6029-6031). The success branch at 5545-5555 restores **only** the relay's own UI and pulls the relay's own config — nothing re-arms the remote boards.

That this is a defect rather than a design choice is confirmed by the OTA-completion path, which had to work around exactly this: "setRemoteConnected (not a raw mapping write) so that if the relay's reader blipped mid-OTA (clearRemoteBoardsForRelay tore down the mapping, remote UI, and ETM listener) the WHOLE remote link is rebuilt" (Wizard/app.js:1830-1833). The general reconnect path never got the same fix.

**Failure scenario.** User manages WCB 3, 4 and 5 wirelessly through relay WCB 19. A USB cable jiggle (or the relay rebooting on its own) ends the relay's read loop. The Wizard logs `[Remote] WCB3 disconnected — relay WCB19 went offline` for each, reconnects the relay 1.5 s later, toasts "WCB 19 reconnected" — and boards 3/4/5 are now shown disconnected with dead terminals and disabled push buttons, with no ETM listener to bring them back. The user must re-arm each board through the relay card by hand.

**Suggested fix.** Snapshot the relay's managed slots before tearing them down (`const managed = Object.keys(remoteRelayForBoard).filter(k => remoteRelayForBoard[k] === this.boardIndex).map(Number);`) and, in the `if (reconnected)` branch at line 5545, re-arm each with `setRemoteConnected(slot, this.boardIndex)` (it is documented as idempotent at Wizard/app.js:1832) followed by `remoteBoardPull`.

### F-168 · S2 · `Wizard/app.js:5771`

**Cancelling the shared-port picker still installs a dead _shared connection, permanently latching _hasSharedPort() and blocking auto-share**

*Category:* `logic`

`sharedConnect()` swallows every failure of the port acquisition and then installs the shared connection regardless:

```js
5764:  try {
5765:    if (existingPort) await hub.adoptPort(existingPort);
5766:    else              await hub.requestPort();             // explicit "Share" button: pick a port now
5767:  } catch (_) { /* follower / cancelled — fine */ }
5768:  if (boardConnections[n]?.isConnected?.()) await boardDisconnect(n);
5769:  const conn = new BoardConnection(n);
5770:  conn.connectShared(hub);
5771:  boardConnections[n] = conn;
...
5777:  try { localStorage.setItem('wcbSharedSlot', String(n)); } catch (_) {}
```

The comment justifies the swallow for a *follower* ("cancelling the picker is the right move for a follower"), but `hub.requestPort()` also rejects with `NotFoundError` when the user simply dismisses the picker with no other tab present. Nothing distinguishes the two cases.

`modalSharedConnect()` does not clean up either — the non-open branch only toasts:

```js
5802:    } else {
5803:      showToast(`WCB ${n}: shared — no tab has opened the port yet`, 'warning', 6000);
5804:    }
```

The slot is now occupied by a `_shared` `BoardConnection` whose `isConnected()` is false (Wizard/app.js:4935 returns `!!this._hub?.portOpen`). Because `_hasSharedPort()` tests `c._shared` and not liveness (Wizard/app.js:5596-5598), it now returns true forever, so the auto-share branch in `establishConnection` (Wizard/app.js:5641) is skipped for the rest of the session. `wcbSharedSlot` is also left pointing at the dead slot, which is one of the two signals that lets `establishConnection`'s open-failure branch silently join another tab's share (Wizard/app.js:5690-5695). Reconnecting the slot directly overwrites `boardConnections[n]` at Wizard/app.js:5678 without calling `leaveShared()`, so the dead conn's `_onHubState` handler (Wizard/app.js:5432-5439) stays registered on the hub and keeps calling `updateConnectionUI(this.boardIndex, open)` for a slot it no longer owns.

**Failure scenario.** User clicks Connect on WCB 1 → "Share port across tabs", then presses Escape / Cancel in the browser's port picker. Toast: "WCB 1: shared — no tab has opened the port yet". `boardConnections[1]` is now a dead shared conn. The user then connects WCB 1 normally via USB and connects WCB 2: WCB 2 never auto-shares (so the NaviCore tab can no longer attach to anything), and the orphaned hub listener for slot 1 can flip WCB 1's header between Connected/Not connected whenever the hub's state changes.

**Suggested fix.** Distinguish cancellation from follower-join: if `existingPort` was null and `requestPort()` rejected while no other tab leads (`!(await _anotherTabLeadsShare())`), abort `sharedConnect` — do not create the conn, do not write `wcbSharedSlot`, and rethrow so `modalSharedConnect` reports it. Also have `modalSharedConnect`'s `hub.portOpen === false` branch tear the share down (`conn.leaveShared(); _sharedHub.leave(); _sharedHub = null; delete boardConnections[n];`) exactly as `establishConnection` does at Wizard/app.js:5658-5663, and make `_hasSharedPort()` ignore `_shared` conns whose hub has no open port.

### F-169 · S2 · `Wizard/app.js:6136`

**General-mismatch modal's "Use WCB<n> values" silently ignores the NaviCore rows it just displayed**

*Category:* `logic`

`GENERAL_FIELD_LABELS` (app.js:1392-1410) includes `navicoreEnabled: 'NaviCore'` and `navicoreId: 'NaviCore ID'`, and `extractGeneralFields` populates them from `config.specialPeer` / `config.specialPeerId` (app.js:1429-1430). `getGeneralMismatches` diffs every key of that map (app.js:1435-1437), so a NaviCore difference is rendered as a row in the conflict table (app.js:6109-6113) and the user is told "These must match across all boards — choose which values to keep".

The two branches are asymmetric:

- "Keep <src> values" (app.js:6124-6133) calls `applyGeneralFieldsToBoardConfig(newBoard, baselineFields)`, which DOES write NaviCore: `cfg.specialPeer = fields.navicoreEnabled; cfg.specialPeerId = fields.navicoreId;` (app.js:1476-1479).
- "Use <new> values" (app.js:6136-6170) sets `generalBaseline = { sourceBoard: newBoard, fields: newFields }` and then only writes g-password, g-mac2, g-mac3, g-meshch, g-delimiter, g-funcchar, g-cmdchar and the eight ETM fields. There is no write to the controller radio, no `set('g-navicore-id', …)`, no `systemConfig.general.specialPeer/specialPeerId`, no per-board `cfg.specialPeer`, and none of the follow-up calls (onGeneralPasswordChange / onGeneralMacChange / onMeshChannelChange / onGeneralCmdCharChange / onETMToggle / onETMChecksumToggle / onGeneralETMChange, app.js:6155-6161) touches the controller.

boardGo cannot recover it either: the push reads the general values out of the DOM (app.js:7411-7418) but never re-derives specialPeer, so `config.specialPeer` stays whatever the last pull left. `restoreGeneralDOMSnapshot` shows what the handler should have done — it sets `g-navicore-id`, `systemConfig.general.controller`, and calls `refreshControllerUI()` (app.js:8169-8171).

Net effect: the user is asked to resolve a NaviCore conflict, picks a side, is told "General settings updated to match WCB n", every other board goes amber — and nothing about NaviCore changed anywhere. Since `generalBaseline.fields` now holds the new values while the boards still hold the old ones, the same modal re-fires on the next pull of any other board.

**Failure scenario.** Baseline board WCB 1 has NaviCore ON with ID 20; WCB 5 is pulled and reports NaviCore OFF. The modal lists a 'NaviCore' row (true vs false). The user clicks "Use WCB 5 values", gets the success toast, and WCB 1/2/3/4 all go amber. Pushing WCB 1 re-sends `?CONTROLLER,ON,20` (unchanged from its own config), NaviCore is still ON everywhere, and pulling WCB 2 re-opens the identical conflict modal.

**Suggested fix.** In the 'Use <new> values' handler, mirror what applyGeneralFieldsToBoardConfig/restoreGeneralDOMSnapshot do: set `systemConfig.general.specialPeer = newFields.navicoreEnabled`, `systemConfig.general.specialPeerId = newFields.navicoreId`, write `g-navicore-id`, write `cfg.specialPeer/specialPeerId` into every non-client boardConfigs entry, and call `refreshControllerUI()` before closing the modal. Alternatively drop navicoreEnabled/navicoreId from GENERAL_FIELD_LABELS so the modal stops offering a choice it does not honour.

### F-170 · S2 · `Wizard/app.js:6800`

**boardPull commits an empty or truncated backup as a successful pull, overwriting config AND baseline with factory defaults**

*Category:* `round-trip-loss`

```js
let raw = await conn.sendAndCollect(BOOTSTRAP_CMD, 3000);          // app.js:6800
if (!raw.includes('End of Backup')) {
  ...
  raw = await conn.sendAndCollect(`${pullFuncChar}backup`, 8000);    // app.js:6804  (REPLACES raw)
}
...
const config = WCBParser.parseBackupString(raw);                    // app.js:6809
```

There is no check that the tier-2 result is complete before it is committed. Two things make that unsafe:

1. `sendAndCollect` NEVER rejects — `app.js:5232-5254` resolves `lines.join('\n')` from a `setTimeout(finish, timeoutMs)` and even converts a send failure into `resolve` via `this.send(...).catch(finish)`. So on a total timeout `raw` is `''`.
2. `parseBackupString` never throws on junk — `parser.js:244-247` logs `console.warn` and RETURNS `createDefaultBoardConfig()` (factory defaults: `wcbNumber: 1`, `espnowPassword: 'change_me_or_risk_takeover'`, `macOctet2/3: '00'`, empty maestros/sequences/variables/WLED/mappings — parser.js:20-45).

So an unanswered pull runs the normal success path: `boardConfigs[n] = config; boardBaselines[n] = JSON.parse(JSON.stringify(config)); populateUIFromConfig(n, config);` (app.js:6916-6918) and `showToast('Config pulled from WCB n', 'success')` (app.js:6922) plus the 'connected' badge. Because config === baseline, the next Push Config reports "No changes to push" (app.js:7421), so the wipe is invisible.

The author clearly intended a failure path here — the catch at app.js:6926-6928 says `isTimeout ? 'Pull timed out — board may have no firmware loaded. Try Flash mode first.'` — but that branch is unreachable dead code, because nothing in the pull ever rejects with a message matching `/timeout/i`.

The partial case is worse than the empty one: if tier-1 times out mid-stream, tier-2's collector picks up the *tail* of backup #1 and stops at its 'End of Backup'. That tail no longer contains the header string 'WCB Configuration Backup', so parseBackupString takes the `extractChainedTokens` branch instead of `extractIndividualCommandLines` (parser.js:238-242) — a different parser applied to per-line output.

The mismatch modal cannot warn either: the defaults have MAC 00/00, and `isDefaultNetworkSettings()` (app.js:1449-1452) suppresses the modal for exactly that.

**Failure scenario.** User loads a saved system file (populating slots 1-5 with maestros, sequences, labels, variables), then connects WCB 3 over USB. establishConnection schedules `setTimeout(() => boardPull(3), 3000)`. The board was just power-cycled and is still in WiFi/ESP-NOW init, so neither WCB_WEBTOOL_CONFIG_PULL (3 s) nor ?backup (8 s) is answered. boardPull parses '' into a default config, replaces boardConfigs[3] and boardBaselines[3], repaints the whole WCB 3 panel to factory defaults, and shows the green toast "Config pulled from WCB 3". Every file-loaded setting for slot 3 is gone, Push Config says "No changes to push", and no error is ever shown.

**Suggested fix.** After the two-tier request, require `raw.includes('End of Backup')` before parsing. If it is missing (or the board answered nothing), leave boardConfigs[n]/boardBaselines[n] untouched, log to the terminal, show the already-written 'Pull timed out — board may have no firmware loaded' error toast, and set the badge to an error state. Also reject rather than resolve on a hard timeout in sendAndCollect (or return a `{ok, raw}` pair) so the intent of the catch at 6926 is realised.

### F-171 · S2 · `Wizard/app.js:7039`

**Push All throws a TypeError on a MgmtRelay slot and silently aborts the remaining stages**

*Category:* `undefined-ref`

boardGo dereferences the Push button with no null guard:

```js
const btn = document.getElementById(`b${n}-btn-go`);   // app.js:7005
...
btn.disabled = true;                                    // app.js:7039  <-- throws when btn is null
```

Every other DOM touch on this path is guarded (`if (connBtn)`, `if (btn)` in boardPull at 6795, `?.` throughout updateConnectionUI) — this one is not.

A MgmtRelay slot has no board section: `applyRelayRole(n)` does `document.getElementById('section-board-' + n)?.remove()` (app.js:480) and `desiredBoardNumbers()` filters relay slots out permanently (`.filter(n => ... && !_relaySlots.has(n))`, app.js:437), so reconcileBoardGrid never re-creates it. But the relay's connection *is* live in `boardConnections[relaySlot]` (set in boardPull's relay branch at app.js:6832-6833).

boardGoAll enumerates connections, not sections:

```js
const directSlots = Object.keys(boardConnections)
  .filter(n => boardConnections[n]?.isConnected())
  .map(Number);                                        // app.js:8254-8256
...
for (const n of directNonRelays) await boardGo(n);      // app.js:8297
for (const n of directRelays) { const needsReboot = await boardGo(n, { skipReboot: true }); ... }   // app.js:8301
```

So the relay slot is passed to boardGo, `b<relaySlot>-btn-go` is null, and `btn.disabled = true` throws. boardGoAll has no try/catch, so the rejection propagates out of the onclick handler: stage 3 and stage 4 ("reboot relay boards last", app.js:8305-8320) never run, and the user gets nothing but a console error after the toast "Pushing to N boards…".

**Failure scenario.** User connects a MgmtRelay over USB (it lands as a relay card, slot 19) and manages WCB 2 and WCB 5 through it. They click ▶▶ Push All. Stage 1 pushes the two remote boards, stage 2/3 reaches boardGo(19), and `btn.disabled = true` throws 'Cannot set properties of null'. The relay's own config is never pushed and, critically, stage 4 never sends the deferred reboots the relay boards were promised at app.js:7549 ('Config applied — reboot queued by Push All…'), so those boards sit configured-but-not-rebooted with no indication of failure.

**Suggested fix.** Guard the button (`if (btn) { btn.disabled = true; btn.textContent = ... }` and every later `btn.*`, as boardPull already does), and skip relay slots explicitly in boardGoAll: `const directSlots = Object.keys(boardConnections).filter(n => boardConnections[n]?.isConnected() && !_relaySlots.has(+n)).map(Number)`. Wrapping each `await boardGo(n)` in boardGoAll in try/catch would also stop one bad slot from killing stages 3-4.

### F-172 · S2 · `Wizard/app.js:7039`

**boardGo() dereferences a null Push button for a MgmtRelay slot, aborting Push All mid-run**

*Category:* `undefined-ref`

`boardGo` looks up the button once and then dereferences it unguarded:

```js
const btn       = document.getElementById(`b${n}-btn-go`);   // app.js:7005
...
btn.disabled = true;                                          // app.js:7039
btn.textContent = (isFlash || isUpdate || isFactory) ? 'Flashing…' : ...
```

Every other `btn` use in the sibling remote path is null-guarded (`if (btn) { ... }` at app.js:7789, 7796, 7812, 7823, 7828, 7881), and `updateConnectionUI` guards each button too (app.js:7637-7642) — only `boardGo` does not.

A management relay HAS a live `boardConnections[slot]` entry but NO `section-board-<slot>`, therefore no `b<slot>-btn-go`: `boardPull`'s relay branch stores `boardConnections[relaySlot] = conn` (app.js:6828), adds the slot to `_relaySlots` (app.js:6844) and calls `applyRelayRole(relaySlot)`, which does `document.getElementById('section-board-'+n)?.remove()` and re-runs `reconcileBoardGrid()` whose `desiredBoardNumbers()` filters out `_relaySlots` (app.js:437, 479-484). `boardGo` itself expects to be reachable for a relay — `updateConnectionUI` ends with `if (_relaySlots.has(n)) renderRelayCard(n);` (app.js:7662).

`boardGoAll` builds `directSlots` from every connected `boardConnections` entry with no relay filter (app.js:8254-8256) and calls `await boardGo(n)` with no try/catch (app.js:8297, 8301), and its only caller is the inline `onclick="boardGoAll()"` at index.html:33 — so the TypeError becomes an unhandled rejection with no toast.

**Failure scenario.** A MgmtRelay (e.g. id 19) is connected over USB alongside WCB1 and WCB2, and no boards are routed through it yet, so `relayBoardSet` is empty and slot 19 lands in `directNonRelays`. The user clicks "▶▶ Push All". Stage 2 iterates `[1, 2, 19]`; slot 1 and 2 push fine, then `boardGo(19)` hits `btn.disabled = true` with `btn === null` and throws `TypeError: Cannot set properties of null`. `boardGoAll` rejects: stage 3/4 never run, `generalSettingsDirty = false; updatePushAllButton();` (app.js:8334-8335) never runs, and the user gets no error at all — only a console trace. With the relay ordered before a real board (e.g. relay id 3 with WCB4 also connected), WCB4 is silently never pushed.

**Suggested fix.** Guard the two writes — `if (btn) { btn.disabled = true; btn.textContent = ...; }` at app.js:7039-7040 and `if (btn) { btn.disabled = false; btn.textContent = 'Push Config'; }` at app.js:7286, 7372 and 7606 — matching `boardGoRemote`. Separately, skip relay slots in `boardGoAll`: `const directSlots = Object.keys(boardConnections).filter(n => boardConnections[n]?.isConnected() && !_relaySlots.has(+n)).map(Number);`, and wrap each stage's `await boardGo(n)` in a try/catch so one board's failure cannot abort the whole fleet push.

### F-173 · S2 · `Wizard/app.js:7140`

**Failed post-flash / post-erase boot-wait leaves the Push Config button permanently disabled and labelled "Flashing…"**

*Category:* `dom`

boardGo restores the Go button only on its normal exits — app.js:7286-7287 for the flash path and app.js:7371-7372 for the erase path:

```js
await _reshareAfterFlash(conn, n);
btn.disabled = false;
btn.textContent = 'Push Config';
return;
```

Three wizard-context branches bypass those lines with a bare `return`, after `btn.disabled = true` / `btn.textContent = 'Flashing…'` was set at app.js:7039-7040:

- app.js:7136-7141 (Update FW): `const ready = await waitForBoardReady(n, conn); if (!ready) { termLog…; showToast…; return; }`
- app.js:7177-7182 (Flash / Factory Reset): identical shape.
- app.js:7332-7337 (Erase NVS): identical shape.

On those exits the button keeps `disabled = true` and the text 'Flashing…' / 'Erasing…'. `updateConnectionUI` only ever sets `goBtn.disabled = !connected` (app.js:7645) and never rewrites the Go button's label, so even a subsequent disconnect/reconnect leaves the button reading 'Flashing…' forever. Note that `_boardFlashing[n]` was already deleted at app.js:7109 and `setFlashUI(n,false)` already ran at 7108, so nothing else is guarding this state.

This is exactly the situation in which the user most needs Push Config: the board came back but did not answer the 150 s boot-watch, so the config was never pushed and the terminal tells them 'cannot push config'.

**Failure scenario.** With the setup Wizard open, the user runs Update FW on WCB 2. The flash succeeds and the port reconnects, but the board takes longer than waitForBoardReady's 150 s totalTimeoutMs to answer ?backup (busy mesh / slow NVS load). The terminal prints '✕ Board did not respond after update — cannot push config', boardGo returns at line 7140, and the Push Config button for WCB 2 stays greyed out reading 'Flashing…'. The only way to push the config is a full page reload.

**Suggested fix.** Replace the three bare `return`s with a shared exit that restores the button, e.g. `{ btn.disabled = false; btn.textContent = 'Push Config'; return; }`, or restructure the flash/erase blocks so their tail (`_reshareAfterFlash` + button restore) runs in a `finally`.

### F-174 · S2 · `Wizard/app.js:7140`

**Wizard flash/erase early-returns skip the mandatory re-share + button restore, latching the shared-port borrow flag**

*Category:* `error-handling`

The flash and erase branches of `boardGo` each end with a cleanup pair the comment calls mandatory:

```js
await _reshareAfterFlash(conn, n);   // catch-all: re-share (or clear the borrow flag) on any flash exit
btn.disabled = false;
btn.textContent = 'Push Config';
return;
```
(app.js:7285-7288 for flash, app.js:7371-7374 for erase).

Three `return`s inside those branches bypass that pair entirely — app.js:7140 (wizard + Update FW, board didn't answer), app.js:7181 (wizard + Flash/Factory, board didn't answer) and app.js:7336 (wizard + Erase, board didn't answer). All three are plain `return`s inside the branch body, and the erase one sits inside a `try{}catch{}` with no `finally`, so control never reaches app.js:7371.

`_reshareAfterFlash` is the ONLY thing that clears `conn._wasSharedBeforeFlash`, and its own comment says the flag is cleared "on EVERY path so the borrow flag can't latch" (app.js:6969). That flag is read by `_hasSharedPort()` (app.js:5597: `c._shared || c._wasSharedBeforeFlash`), which gates auto-sharing for every subsequent connect in `establishConnection` (app.js:5641).

**Failure scenario.** A board is auto-shared as the cross-tab port (the first board connected always is — app.js:5641-5655). In the setup Wizard the user runs Update FW on it. `boardGo` borrows the port from the hub and sets `conn._wasSharedBeforeFlash = true` (app.js:7032-7035). The flash succeeds and the board reconnects, but the freshly-flashed S3 is slow and `waitForBoardReady` returns false at app.js:7136. Execution hits `return` at app.js:7140. Result: (a) the Push Config button for that board stays `disabled` with the text "Flashing…" for the rest of the session — the user cannot push config to that board without reloading the page; (b) the port is never handed back to a hub, so the NaviCore tab can never re-attach; (c) `_wasSharedBeforeFlash` stays true forever, so `_hasSharedPort()` returns true and NO later board will auto-share either.

**Suggested fix.** Replace each of the three bare `return`s with the same cleanup, or restructure so the cleanup is unavoidable: wrap the flash branch body and the erase branch body in `try { ... } finally { await _reshareAfterFlash(conn, n); if (btn) { btn.disabled = false; btn.textContent = 'Push Config'; } }`, then the `return`s can stay as-is.

### F-175 · S2 · `Wizard/app.js:7481`

**A push containing a PWM input mapping makes the firmware auto-reboot mid-push; every command after it is silently lost**

*Category:* `protocol-contract`

The ACK-paced push loop sends every command in order and only ever treats 'no response' as a problem:

```js
for (const cmd of commands) {                                        // app.js:7481
  let r = await conn.sendAndAwaitIdle(cmd, { quietMs: 250, hardTimeoutMs: 5000 });
  if (!r.ok) { ... retry once ... }
  if (!r.ok) { _pushFullyAcked = false; ... }
  reportPushProgress(n, ++_pushDone, _pushTotal);
}
```

But `?MAP,PWM,S<n>,<dest>` makes the board restart itself. `WCB.ino:4532-4543` routes it to `addPWMMapping(pwmConfig)` with the `autoReboot` argument defaulted, and `WCB_PWM.h:38` declares `void addPWMMapping(const String &config, bool autoReboot = true);`. `WCB_PWM.cpp:338-342` then does `Serial.println("Rebooting in 3 seconds to apply PWM configuration..."); delay(3000); ESP.restart();`.

The Wizard already knows this — app.js:8192-8194 comments "PWM INPUT mapping (MAP,PWM,Sx,...) — firmware auto-reboots, but we signal it too" — yet nothing in the loop stops or re-synchronises on it. Worse, `sendAndAwaitIdle` counts the "Rebooting in 3 seconds…" line as a valid ACK (`ok: lines.length > 0`, app.js:5281), so the loop immediately proceeds.

Ordering in the builder guarantees casualties: parser.js emits PWM output ports (1503-1505), then Mappings (1510-1521), then Stored Sequences (1527-1536). So every `SEQ,SAVE,…` — and any `MAP,SERIAL,…` after the PWM row — is transmitted into a board that is in `delay(3000)` and then restarts. `_pushFullyAcked` may still be true (boot-log lines answer the later sends), so the no-reboot branch at app.js:7563 reports 'configured'/success.

**Failure scenario.** WCB 2 has a PWM input mapping (S1 -> W3S4) and eight stored sequences. The user does a Factory Reset with 'push config after flash' checked; the post-flash push runs with `boardBaselines[n] = null`, i.e. fullPush, so parser.js emits `?MAP,PWM,S1,W3S4` followed by all eight `?SEQ,SAVE,…`. The board prints 'Rebooting in 3 seconds to apply PWM configuration...', the loop keeps sending, and the ESP32 restarts. All eight sequences are lost. The progress bar still walks to 100% and the board is badged 'configured'.

**Suggested fix.** Detect the reboot-inducing commands before sending and either (a) order them last — emit MAP,PWM,S* after sequences/variables in buildCommandString — or (b) treat 'Rebooting in' in `r.lines` as a stop condition in the loop: abort the remaining commands, wait for the reconnect, then re-push the tail. Option (a) is the smaller change and matches the existing 'char-change commands go first' precedent at app.js:7433-7460.

### F-176 · S2 · `Wizard/app.js:7811`

**boardGoRemote has no funcChar/delimiter/cmdChar bootstrap — changing a command character over a relay sends a payload the target cannot parse**

*Category:* `protocol-contract`

`boardGoRemote` builds the payload with `const cmdString = WCBParser.buildCommandString(config, boardBaselines[n] ?? null, fullPush);` (Wizard/app.js:7811) and ships it straight to the relay (Wizard/app.js:7863-7869). `buildCommandString` emits the whole chain using the config's NEW `funcChar`/`delimiter` (Wizard/parser.js:1226-1227), so if the user changed either one, the entire payload is written in a format the target board does not yet speak.

The target splits the reassembled string with its CURRENT characters: `parseCommandsAndEnqueue` scans for `commandDelimiter` and `LocalFunctionIdentifier` (Code/WCB/WCB.ino:1947, :2015-2020, :2057). With a changed delimiter, none of the new separators match, so the entire multi-hundred-character chain is treated as one command; `processLocalCommand` parses only the first root token and the rest is dropped.

The DIRECT push path solves exactly this and says so: Wizard/app.js:7427-7460 sends a bootstrap of `${curFuncChar}DELIM,…` / `${curFuncChar}CMDCHAR,…` / `${curFuncChar}FUNCCHAR,…` using the BASELINE characters first, ACK-paced with a retry, before sending the reformatted chain ("If the user changed funcChar / delimiter / cmdChar, the board still speaks the OLD char when the push starts, so the prefixed commands are silently ignored"). `boardGoRemote` has no equivalent — the words DELIM/FUNCCHAR/CMDCHAR do not appear anywhere between Wizard/app.js:7769 and :7885.

As with the other remote-push failures, the Wizard toasts success (Wizard/app.js:7870) and rewrites the diff baseline (Wizard/app.js:7873), so the change is never retried.

**Failure scenario.** A user standardises the fleet on `.` as the function character: they set g-funcchar to `.` in General Settings and click Push All. Stage 2/3 (direct boards) apply it correctly via the bootstrap at Wizard/app.js:7436-7459. Stage 1 (`boardGoAll` line 8292-8294 → `boardGoRemote`) sends the relayed boards a payload like `.HW,32^.WCB,3^.FUNCCHAR,.^…` — but those boards still parse `?` as the function identifier, so the chain matches nothing and is discarded. The fleet is left split: direct boards on `.`, relayed boards still on `?`, and the Wizard's baselines claim all of them are on `.`, so no later diff push will ever contain FUNCCHAR again.

**Suggested fix.** Factor the bootstrap block at Wizard/app.js:7436-7459 into a helper and run it from `boardGoRemote` before line 7811, sending each bootstrap command as its own single-chunk `?MGMT,FRAG,<wcb>,<sid>,0,1,<cmd>` (prefixed with the BASELINE funcChar), in the same order (DELIM, CMDCHAR, then FUNCCHAR last). Alternatively, refuse the remote push with a clear toast when `config.funcChar/delimiter/cmdChar` differ from `boardBaselines[n]`, and tell the user to connect that board over USB.

### F-177 · S2 · `Wizard/app.js:7853`

**boardGoRemote never checks the 16-chunk MGMT ceiling — an oversized config is rejected wholesale by the relay while the Wizard reports success**

*Category:* `protocol-contract`

`boardGoRemote` computes `const total = chunks.length;` (Wizard/app.js:7853) and sends every fragment with that count (Wizard/app.js:7865) without ever bounding it.

The relay hard-rejects the whole session when the count exceeds the reassembly array: `Code/WCB/WCB.ino:2583` — `if (targetWCB < 1 || targetWCB > MAX_WCB_COUNT || totalChunks == 0 || totalChunks > MGMT_MAX_CHUNKS) { … return; }` with `#define MGMT_MAX_CHUNKS 16` (Code/WCB/WCB.ino:837). Because the check is per-packet, EVERY fragment is dropped at the relay — the target never even opens a session, so there is no timeout log on the target either. The usable ceiling is 16 × 179 = 2864 chars (documented at docs/SEQUENCE_INVENTORY.md:216 and enforced in the sibling sender at WCBClient/src/WCB_Client.cpp:437-440, which logs and refuses).

The Wizard has no such guard. It proceeds to `showToast(\`WCB ${n}: ${changeLabel} sent via WCB ${relayN}\`, 'success')` (Wizard/app.js:7870) and `boardBaselines[n] = JSON.parse(JSON.stringify(config))` (Wizard/app.js:7873), so the entire push is recorded as applied.

Size context measured by loading Wizard/parser.js in node: a bare default full push is 534 chars and a modest board is 710. Each additional stored sequence adds `?SEQ,SAVE,<key>,<value>` — roughly `len(value)+12` — so ~15 sequences with 150-char values, or a full push on a board with a large sequence library, crosses 2864.

**Failure scenario.** WCB4 (managed via a relay) has 16 stored show sequences averaging 160 chars. The user does a first-ever pull-less push (no baseline → `fullPush = true`), producing a ~3.3 KB command string → 19 chunks. The relay logs nothing user-visible and discards all 19 packets at WCB.ino:2583. The Wizard toasts "WCB 4: full push sent via WCB 1", turns the badge green (`updateBoardStatusBadge(n,'configured')`, Wizard/app.js:7882), and stores the pushed config as the baseline. WCB4 has received nothing at all, and every subsequent diff push is computed against a baseline the board never had — so the settings are permanently unrecoverable through the UI.

**Suggested fix.** After line 7853 add `if (total > 16) { showToast(\`WCB ${n}: config is ${cmdToSend.length} chars — over the ${16*179}-char relay limit. Connect via USB to push this board.\`, 'error', 10000); if (btn) { btn.disabled=false; btn.textContent='Push Config'; } return false; }`. Mirror the constant from Code/WCB/WCB.ino:837 with a comment. Separately, do not set `boardBaselines[n]` on a path whose delivery was never acknowledged.

### F-178 · S2 · `Wizard/app.js:7902`

**remoteBoardPull leaks its _pullingBoards entry on a retry attempt, permanently locking that board out of all future pulls**

*Category:* `leak`

`remoteBoardPull` adds the in-flight marker only on the first attempt (Wizard/app.js:7905-7912):

```js
if (attempt === 1) {
  if (_pullingBoards.has(targetN)) { … return; }
  _pullingBoards.add(targetN);
}
```

but the relay-liveness guard runs BEFORE that block, on every attempt (Wizard/app.js:7901-7902):

```js
const relayConn = boardConnections[relayN];
if (!relayConn?.isConnected()) { showToast('Relay board not connected', 'error'); onComplete?.(false); return; }
```

On a retry (`attempt >= 2`, scheduled by the timeout handler at Wizard/app.js:8021) the marker is already set, and this early return exits without `_pullingBoards.delete(targetN)`. The entry is never cleared by anything else — `clearRemoteConnected` (Wizard/app.js:6022) and `clearRemoteBoardsForRelay` (:6012) do not touch `_pullingBoards`; the only deletes are inside `remoteBoardPull` itself (:7949, :8002, :8005, :8023, :8043) and the OTA path (:1834, :1862).

Every later pull for that board is then rejected by the dedup guard at Wizard/app.js:7906 — including the manual recovery button `boardRetryPull` (Wizard/app.js:8062-8067) and `relayRouteAll`'s `!_pullingBoards.has(n)` filter (Wizard/app.js:527). The board is stuck until a page reload.

The author's own comment at Wizard/app.js:8037-8042 shows this cleanup was intended ("Otherwise the slot is permanently locked until the next page reload") — it was only added to the send-throw path, not to this early return.

**Failure scenario.** WCB5 is managed through relay WCB1. A pull times out (6 s) and schedules a retry in 2.5 s. During that gap the user unplugs/replugs the relay's USB, or the relay's read loop errors and `boardDisconnect` runs. The retry fires, sees `relayConn.isConnected() === false`, toasts "Relay board not connected" and returns — leaving 5 in `_pullingBoards`. The relay reconnects seconds later, ETM reports WCB5 online, but `remoteBoardPull` logs "Pull for WCB5 already in progress — skipping duplicate" and returns. The user clicks the board's Retry button: same result. WCB5 shows an error badge with no config for the rest of the session.

**Suggested fix.** Move the `_pullingBoards.add` bookkeeping so the marker is released on every abort path, e.g. change line 7902 to `if (!relayConn?.isConnected()) { if (attempt > 1) _pullingBoards.delete(targetN); showToast('Relay board not connected', 'error'); onComplete?.(false); return; }`. Also clear `_pullingBoards` for boards behind a relay in `clearRemoteBoardsForRelay` (Wizard/app.js:6012) so a relay teardown can never strand a marker.

### F-179 · S2 · `Wizard/app.js:8282`

**Push All includes MgmtRelay slots, whose board section (and Push button) was deleted — boardGo throws and Push All dies before the relay-reboot stage**

*Category:* `undefined-ref`

`boardGoAll` builds its worklists from raw connection slots (Wizard/app.js:8254-8283):

```js
const directSlots  = Object.keys(boardConnections).filter(n => boardConnections[n]?.isConnected()).map(Number);
const relayBoardSet = new Set(Object.values(remoteRelayForBoard).map(Number));
const directRelays  = directSlots.filter(n =>  relayBoardSet.has(n));
const directNonRelays = directSlots.filter(n => !relayBoardSet.has(n));
```

Neither list excludes `_relaySlots` — the slots holding a **MgmtRelay** (a WCB_Client USB conduit, not a WCB). A MgmtRelay always lands in `relayBoardSet`, because `relayManageOne` (Wizard/app.js:489-495) sets `remoteRelayForBoard[target] = relaySlot` for every board managed through it. So it always ends up in `directRelays` and is passed to `boardGo` at Wizard/app.js:8300-8302.

But `applyRelayRole` deleted that slot's whole board section: `document.getElementById(\`section-board-${n}\`)?.remove();` (Wizard/app.js:480). The Push button lives inside it — `Wizard/index.html:415` opens `<div class="section open" id="section-board-{N}">` and `Wizard/index.html:496` is `<button … id="b{N}-btn-go" onclick="boardGo({N})">`. `boardGo` looks it up at Wizard/app.js:7005 and then dereferences it unguarded at **Wizard/app.js:7039**: `btn.disabled = true;` → `TypeError: Cannot set properties of null`.

`boardGoAll` wraps nothing in try/catch, so the rejection escapes: Stage 4 (relay reboots, Wizard/app.js:8305-8331) never runs, `generalSettingsDirty = false; updatePushAllButton();` (Wizard/app.js:8334-8335) never runs, and any later entries in `directRelays` are never pushed. Nothing is surfaced to the user — only a console error. (Even if the button existed, pushing a WCB config string to a WCB_Client relay is not something the tool intends.)

**Failure scenario.** Greg connects a MgmtRelay over USB at slot 1, clicks "Manage all" so WCB3 and WCB19 are armed through it, edits the ESP-NOW password in General Settings, and clicks Push All. Stage 1 pushes WCB3 and WCB19 (they reboot onto the new password). Stage 3 calls `boardGo(1, {skipReboot:true})`; `b1-btn-go` no longer exists, so line 7039 throws. Push All ends there with no toast: the Push All button stays lit as if nothing was pushed, and if a real WCB relay was also in `directRelays` after slot 1, it is never pushed at all — leaving it on the OLD password while the remotes moved to the new one, i.e. exactly the split-network state the network-group guard exists to prevent.

**Suggested fix.** Exclude relay conduits when building the worklists at Wizard/app.js:8254-8256: `.filter(n => boardConnections[n]?.isConnected() && !_relaySlots.has(n))`. Independently, harden `boardGo` by guarding the button dereference at Wizard/app.js:7039-7040 (`if (btn) { btn.disabled = true; btn.textContent = …; }`, matching how `boardGoRemote` guards every `btn` use), and wrap the stage loops in `boardGoAll` in try/catch so one board's failure cannot silently cancel the remaining stages.

### F-180 · S2 · `Wizard/app.js:8419`

**exportSystemFile only walks slots 1..wcbQuantity — any board discovered or relayed at a higher number is silently dropped from the exported file**

*Category:* `round-trip-loss`

`exportSystemFile` collects boards with a contiguous 1..qty loop (Wizard/app.js:8417-8433):

```js
systemConfig.boards = [];
const qty = systemConfig.general.wcbQuantity;      // = g-wcbq, line 8406
for (let n = 1; n <= qty; n++) {
  if (boardConfigs[n]) { … systemConfig.boards.push(boardConfigs[n]); }
}
```

But the board grid is deliberately **sparse** and not bounded by WCBQ. `desiredBoardNumbers()` unions the WCBQ floor with mesh-discovered peers and live connections and only clamps to `WCB_MAX` (Wizard/app.js:431-437), and `reconcileBoardGrid`'s comment at :440-442 says explicitly "Sparse-aware (sections are keyed by number, so gaps like {1,2,20} are fine)". `addDiscoveredBoards` (Wizard/app.js:587-594) adds any number 1..20 to `_meshBoards` **without** raising `_boardFloor` or `g-wcbq`, and `relayManageOne` (Wizard/app.js:489-495) calls it for every board heard through a MgmtRelay. `remoteBoardPull` then writes a full, real config into `boardConfigs[targetN]` for that number (Wizard/app.js:7984).

So a board can be visible, connected, pulled and fully editable in the UI, yet be invisible to the exporter. There is no warning — `buildSystemFile` faithfully writes whatever `systemConfig.boards` contains (Wizard/parser.js:1579-1584), and the success toast fires regardless (Wizard/app.js:8443).

The import side has the mirror-image gap: `loadSystemFileContent` does `renderBoards(system.general.wcbQuantity)` then `const n = board.wcbNumber || (i + 1); boardConfigs[n] = board; populateUIFromConfig(n, board);` (Wizard/app.js:8385-8391), so a board whose number exceeds the quantity gets a config object but no rendered section to display it in.

**Failure scenario.** Greg has a 3-board droid (g-wcbq = 3) plus a management relay whose mesh includes a spare board numbered 19 (the WCB19 setup in his notes). He clicks "Manage all", WCB19's config is pulled into boardConfigs[19] and its card renders, he configures its serial ports and pushes it. He then clicks "Export Config File" to back the droid up. The exported .txt contains [GENERAL], [WCB1], [WCB2], [WCB3] — WCB19 is absent, with a green "Config file exported" toast. Restoring from that file later silently drops the board.

**Suggested fix.** Iterate the actual keys instead of a range: `for (const n of Object.keys(boardConfigs).map(Number).filter(k => k >= 1 && k <= WCB_MAX).sort((a,b)=>a-b))`, skipping `_relaySlots`. Also raise `systemConfig.general.wcbQuantity` to at least the highest exported board number so the file re-imports into a grid that can show it, and fix the symmetric import gap by calling `addDiscoveredBoards(system.boards.map(b => b.wcbNumber))` before `populateUIFromConfig` at Wizard/app.js:8385.

### F-181 · S2 · `Wizard/app.js:8966`

**Kyber target baud picker uses the full `BAUD_RATES` list; the firmware's Kyber target parser accepts only six of those rates and silently skips the rest**

*Category:* `protocol-contract`

`appendKyberTargetRow()` populates the target baud picker from the global list: `const baudOptions = BAUD_RATES.map(b => …)` (Wizard/app.js:8966-8968), where `const BAUD_RATES = [110,300,600,1200,2400,9600,14400,19200,38400,57600,115200,128000,256000]` (Wizard/app.js:7).

The firmware's Kyber-target validator inside `storeKyberSettings()` accepts only six: `(baudRate == 9600 || baudRate == 14400 || baudRate == 19200 || baudRate == 38400 || baudRate == 57600 || baudRate == 115200)` (Code/WCB/WCB_Storage.cpp:1250-1252). Anything else falls to the else branch, which prints `"⚠️  Skipping target '%s': baud rate must be 9600/14400/19200/38400/57600/115200"` (WCB_Storage.cpp:1327-1328) and — critically — does NOT increment `targetIndex`, so `kyberTargets[…].enabled` stays false and that Maestro is dropped from Kyber's routing table.

This is reachable without ever touching the Kyber target rows, because `autoComputeKyberTargets()` copies the baud straight off the Maestro table: `config.kyber.targets.push({ id: m.id, wcb: wcbNum, port: m.port, baud: m.baud })` (Wizard/app.js:8057). The Maestro row's baud picker is also `BAUD_RATES` (filtered only to <= 57600 for S3-S5, app.js:3253-3256), and `configureMaestro` DOES accept 2400/128000/256000 (Code/WCB/WCB_Maestro.cpp:610-618). So the two commands in the same push disagree: `?MAESTRO,M1:W1S1:2400` succeeds while `?KYBER,LOCAL,S2,M1:W1S1:2400` skips that target.

**Failure scenario.** User has a Kyber sound board and a Maestro on S1, and sets the Maestro's baud to 2,400 (offered by the picker). Push Config sends `?MAESTRO,M1:W1S1:2400` (accepted, slot created) and `?KYBER,LOCAL,S2,M1:W1S1:2400`. The firmware prints "Skipping target 'M1:W1S1:2400': baud rate must be 9600/…" and leaves the Kyber target list empty. The Maestro shows up in `?MAESTRO,LIST` and responds to `;M1x`, but Kyber's serial stream is never forwarded to it — the servos do nothing when the sound board fires, with no error surfaced in the Wizard.

**Suggested fix.** Define a Kyber/Maestro-target baud list `[9600,14400,19200,38400,57600,115200]` and use it for both the Kyber target picker (app.js:8966) and the Maestro row picker (app.js:3253-3256), so a Maestro can never be given a baud that Kyber routing will reject.

### F-182 · S2 · `Wizard/app.js:9341`

**Reducing the slot count leaves activeBoardTab out of range — the Board Identity step renders with no visible panel**

*Category:* `logic`

`wizardInitBoards(qty)` clamps `kyberBoard` and prunes orphaned `maestros`, but never clamps `wizardState.activeBoardTab`:

```js
function wizardInitBoards(qty) {
  const prev = wizardState.boards;
  wizardState.boards = Array.from({length: qty}, (_, i) => prev[i] ?? wizardDefaultBoard(i + 1));
  if (wizardState.kyberBoard > qty) wizardState.kyberBoard = 1;   // ← line 9341, the only clamp
  ...
}
```

`activeBoardTab` is only written by `wizardDefaultState()` (0), by `wizardBuildStepHTML` for the serial step (`case 'serial': wizardState.activeBoardTab = 0;`, :9553), and by `wizardSwitchBoardTab` (:10547). The identity step has no such reset — `wizardHTMLIdentity` renders each panel with `class="wizard-tab-panel ${i === wizardState.activeBoardTab ? 'active' : ''}"` (:9695) and each tab likewise (:10562). With `activeBoardTab` past the end of the truncated `boards` array, no tab and no panel gets `active`, and `.wizard-tab-panel { display: none; }` (Wizard/styles.css:812) hides them all.

**Failure scenario.** User picks 6 slots, reaches Step 2 — Board Identity, clicks the "WCB 6" tab (activeBoardTab = 5), then clicks ← Back and changes the count to 2. `wizardSelectQty(2)` truncates `boards` to 2 and auto-advances back to Board Identity, which now renders two unhighlighted tabs, the board-label photo, and nothing else — no WCB #, no Hardware Version, no Alias fields. Clicking Next then fails validation with "Please select a hardware version for every WCB slot" for fields the user cannot see.

**Suggested fix.** Add `if (wizardState.activeBoardTab >= qty) wizardState.activeBoardTab = 0;` to `wizardInitBoards`, and/or clamp defensively in `wizardHTMLIdentity` the way `wizardHTMLSerial` already snaps the tab off a Client slot (:9879-9882).

### F-183 · S2 · `Wizard/app.js:9520`

**Re-entering the Connect step after Back discards every later edit and leaves the port panel dead**

*Category:* `logic`

`wizardRenderStep` only bootstraps the connect step when no mode is set yet:

```js
  if (key === 'connect') {
    nextBtn.style.display = 'none';
    if (!wizardState.connectMode) {          // ← line 9520
      setTimeout(() => wizardSelectConnectMode('seq'), 0);
    }
  }
```

`wizardSelectConnectMode` is the **only** caller of `wizardApplyConfig()` (:10429 — confirmed by grep, the sole call site in the file) and the only thing that opens the port panels (`_wizPortOpenPanel` at :10440 / :10446). The Back button is visible on the connect step (`visibility` is only hidden at `idx === 0`, :9498), so leaving and returning is a normal action.

On the second visit `connectMode` is already `'seq'`, so neither runs. Two consequences:

1. **Stale config is what gets pushed.** Everything the user changed after backing out (serial baud, ETM values, aliases, Maestro rows) is saved into `wizardState` by `wizardSaveStep` but never written to `boardConfigs` — the push uses the snapshot taken on the first visit.
2. **The panel is dead.** `wizardHTMLConnect` renders `<div id="wiz-port-panel-N">…Loading ports…</div>` (:10355-10357) and the post-render hook is `wizardStartConnectWatchers()`, whose body is comments only (:11802-11807). Nothing ever replaces the placeholder, so there is no 🔍 Detect / + Authorize / Select by COM# button.

**Failure scenario.** User reaches Connect & Push, realises the ETM timeout is wrong, clicks ← Back three times to the ETM step, fixes it, clicks Next three times back to Connect. The board rows now show "Loading ports…" forever with no buttons. The only escape is the ✕ (wizardCancelConnect) then 🔌 Reconnect — and that path pushes the config captured before the ETM edit, so the corrected value silently never reaches any board.

**Suggested fix.** In `wizardRenderStep`'s connect branch, drop the `!connectMode` guard: always call `wizardApplyConfig()` (it is idempotent) and re-open the port panel for the current `connectSeqN` slot on every entry to the step — or clear `wizardState.connectMode` in `wizardBack` when leaving the connect step so the existing bootstrap path re-runs.

### F-184 · S2 · `Wizard/app.js:10550`

**wizardSwitchBoardTab uses the board index against positional DOM indices — clicking a Serial tab hides every panel**

*Category:* `dom`

`wizardBoardTabs(step)` skips Client slots for every step except identity (`if (!includeClients && (b.type || 'wcb') === 'client') return '';`, :10561), and `wizardHTMLSerial` likewise renders no panel for a client (`if ((b.type || 'wcb') === 'client') return '';`, :9887). But the click handler is emitted with the **board array index** `i` (:10563), and `wizardSwitchBoardTab` compares that against the **positional** index of the surviving elements:

```js
function wizardSwitchBoardTab(step, i) {
  wizardSaveStep(step);
  wizardState.activeBoardTab = i;
  document.querySelectorAll(`.wizard-board-tab`).forEach((t, ti) =>
    t.classList.toggle('active', ti === i));
  document.querySelectorAll(`[id^="wiz-panel-${step}-"]`).forEach((p, pi) =>   // ← line 10550
    p.classList.toggle('active', pi === i));
}
```

When any slot before a WCB slot is a Client, `ti`/`pi` are one or more less than `i`, so `pi === i` is false for every panel. `.wizard-tab-panel { display: none; }` and `.wizard-tab-panel.active { display: flex; }` (Wizard/styles.css:812-813) mean removing `active` from the previously-active panel with none gaining it hides the entire step body.

**Failure scenario.** 3 slots: slot 1 = WCB_Client, slots 2 and 3 = WCB. On Step 5 — Serial Ports the strip shows two tabs. Click either one: `wizardSwitchBoardTab('serial', 1)` or `(…, 2)` runs against `querySelectorAll` results of length 2 (indices 0 and 1). For the slot-3 tab (`i = 2`) neither index matches — both the tab highlight and the serial table vanish, leaving the step with a title, a tab strip and blank space. The user can no longer set baud rates or labels for any board on that step.

**Suggested fix.** Emit the positional index alongside the board index (e.g. `onclick="wizardSwitchBoardTab('serial',${i})"` plus toggling by element id — `document.getElementById('wiz-panel-'+step+'-'+i)` — instead of by enumeration order), or key the toggles off the element id: `p.classList.toggle('active', p.id === `wiz-panel-${step}-${i}`)` and give each tab a matching `id`/`data-slot`.

### F-185 · S2 · `Wizard/app.js:10822`

**Identity validation hardcodes ID 20 instead of the configurable NaviCore ID, allowing a controller/board ID collision**

*Category:* `logic`

Step 3b lets the user pick any NaviCore ID from 1 to 20 (`wizardHTMLNavicoreConfig` builds `Array.from({length: 20}, (_, i) => i + 1)`, :9984-9985; saved by `wizardSaveStep` as `if (!isNaN(idv) && idv >= 1 && idv <= 20) wizardState.navicoreId = idv;`, :10663-10664). That value is pushed as the controller peer ID — `cfg.specialPeerId = ws.navicoreId || 20` (:10922) → `add(curSpecial ? `CONTROLLER,ON,${config.specialPeerId ?? 20}` : ...)` (Wizard/parser.js:1281).

But the duplicate check is written against the literal 20:

```js
      if (wizardState.useSpecialPeer && ids.includes(20))
        return 'ID 20 is reserved for the special slot — pick a different ID for that slot, or uncheck the special peer option in the previous step.';
```

So a NaviCore ID of, say, 3 alongside a board numbered 3 passes validation. In the firmware the controller shares the single ID-indexed peer/board table with real WCBs — `int spIdx = WCB_SPECIAL_PEER_ID - 1; ... boardTable[spIdx]` (Code/WCB/WCB.ino:1054-1062) and `bool isSpecialPeerSlot = (specialPeerEnabled && (b + 1) == WCB_SPECIAL_PEER_ID && targetWCB == WCB_SPECIAL_PEER_ID)` (:1199-1200) — so the two devices collide in one slot's online/heartbeat/ACK state.

The same literal appears in the user-facing copy: the identity step's panel header is `<strong>Special Slot — ID 20</strong>` (:10801) and the connect step's footer says `<li>ID 20 (special)…` (:10393) and `?CONTROLLER,ON` "so they can talk to ID 20" (:10389), all regardless of the chosen ID.

**Failure scenario.** User picks NaviCore in Step 3, changes the NaviCore ID to 5 in Step 3b (their NaviCore is configured for 5), and numbers one of their boards WCB 5. The identity step validates clean. Every WCB is pushed `?CONTROLLER,ON,5` while also holding WCB 5 as a normal peer — both map to `boardTable[4]`, so the controller's heartbeats mark the real WCB 5 online (and vice versa) and ETM ACK accounting for slot 5 is ambiguous. Meanwhile the wizard's own screens tell the user the special slot is "ID 20".

**Suggested fix.** Validate against the live value: `const spId = wizardState.navicoreId || 20; if (wizardState.useSpecialPeer && ids.includes(spId)) return `ID ${spId} is reserved for the controller…`;` and interpolate `wizardState.navicoreId` into the "Special Slot — ID 20" header (:10801) and the connect-step footer (:10389, :10393).

### F-186 · S2 · `Wizard/app.js:10931`

**wizardApplyConfig never writes boardConfigs[n] for Client slots — slot type and client alias are silently dropped**

*Category:* `round-trip-loss`

`wizardApplyConfig` builds a full `cfg` for every slot, including `cfg.type` / `cfg.alias` / `cfg.clientAlias` (Wizard/app.js:10919-10922), then returns out of the `forEach` callback for client slots:

```js
    // Kyber / Maestro routing only applies to WCB-type slots — Client slots
    // run their own sketch and don't have the firmware to drive either.
    if (cfg.type === 'client') return;      // ← line 10931
```

But `boardConfigs[n] = cfg; populateUIFromConfig(n, cfg); onBoardFieldChange(n);` are 74 lines further down at :11005-11007 — *after* that return. The comment says the skip is about Kyber/Maestro routing; the code also skips the assignment. So `boardConfigs[n]` keeps the blank default that `addBoardSection` created (`WCBParser.createDefaultBoardConfig()` with `type: 'wcb'`, Wizard/app.js:616-618, parser.js:35).

The sibling export path was fixed for exactly this and documents it: `wizardExportConfig` does `if (cfg.type === 'client') return cfg;` with the comment "Return the metadata-only cfg so it's still in the exported file" (:11087-11091). The apply path was not.

`boardConfigs[n].type` is what the whole main page keys off — `updateSlotTypeUI` (:1952-1969), `updateBoardAliasUI` (:1922-1945), and the many `if (cfg.type === 'client') continue;` guards at :1274, :1311, :1319, :1383, :2132, :3353, :3362. None of them ever see 'client' for a wizard-declared client slot.

**Failure scenario.** User sets slot 2's Type to WCB_Client and types the alias "Dome Sensor" in Step 2, finishes the wizard. The config page renders slot 2 as a normal WCB card with a full config body, header "WCB 2" (alias lost), and a blank default config (no password/MAC from the wizard). Pushing "Push All" or connecting anything to that slot targets a board that does not exist, and the slot's default network settings differ from every other board, which can also trip the General Settings Mismatch modal.

**Suggested fix.** Move the `boardConfigs[n] = cfg; populateUIFromConfig(n, cfg); onBoardFieldChange(n);` block above the client early-return (or change the return into an `if (cfg.type !== 'client') { …routing… }` wrapper) so client slots get their metadata-only cfg stored, mirroring `wizardExportConfig`.

### F-187 · S2 · `Wizard/app.js:11701`

**A stale boardBaselines[n] satisfies the watcher's readiness gate — the push starts ~500 ms after port open and races the scheduled pull**

*Category:* `race`

`wizardWatchForConnect` gates the push on:

```js
    // Wait until the board is connected AND the initial ?backup has completed.
    // boardBaselines[n] being non-null means boardPull finished — the board is
    // fully booted and responsive, so it's safe to push the full config.
    if (boardConnections[n]?.isConnected() && boardBaselines[n]) {   // ← line 11701
```

The stated invariant does not hold: nothing clears `boardBaselines[n]` on disconnect. `boardDisconnect` (Wizard/app.js:6196-6220) deletes `boardConnections[n]` and `remoteRelayForBoard[n]` but leaves `boardBaselines[n]`; `establishConnection` (:5634-5730) never touches it either. So whenever the slot was pulled before this connect, the gate is already true on the very first 500 ms tick — before the `setTimeout(() => boardPull(n), 3000)` that `wizPortComplete` scheduled two lines earlier (:11589) has even fired.

Two consequences: the full config push starts ~500 ms after `port.open()`, i.e. while the board is still in the DTR-reset boot the open triggered (exactly what the gate exists to prevent); and 2.5 s later the scheduled `boardPull(n)` runs concurrently with `boardGo`'s command stream. `_boardPullInFlight` guards pull-vs-pull only ("Drop overlapping pulls on the same slot", :6763-6775) — there is no push-vs-pull guard, so `?backup` output interleaves with the config commands and their ACK pacing on the same port.

**Failure scenario.** Common path: the user already had WCB 1 connected and pulled on the config page, then opens the Wizard. (Same on the wizard's own 🔌 Reconnect after a board shows ✓ Done — `wizardManualConnect` calls `boardDisconnect`, which preserves the baseline.) They pick the port on the Connect step; 500 ms later — with the board still printing its boot banner — the wizard restores the snapshot and starts a full push; at t+3 s the queued `boardPull` fires `WCB_WEBTOOL_CONFIG_PULL`/`?backup` into the middle of it. Commands sent during boot are dropped and `_pushFullyAcked` goes false, while the interleaved backup dump corrupts the collected reply — and per the finding above the row still reports ✓ Done.

**Suggested fix.** Clear the baseline as part of the reconnect so the gate means what its comment says: `delete boardBaselines[n]` in `boardDisconnect` (or in `wizPortComplete` right before `wizardWatchForConnect`), and hold the scheduled verify `boardPull` until the push completes rather than firing it on a fixed 3 s timer.

### F-188 · S2 · `Wizard/app.js:12610`

**The 12 s mesh-discovery poll injects ?WDP,DUMP into an in-flight config push, faking ACKs for dropped settings**

*Category:* `race`

`meshAutoDiscoverTick` runs on a page-lifetime `setInterval(meshAutoDiscoverTick, 12000)` (app.js:12672) and guards against exactly two kinds of collision:

```js
if (_meshDiscoverBusy) return;
if (_statsFetchBusy) return;   // don't inject ?WDP,DUMP into an in-flight ETM/stats capture
if (typeof _otaInProgress !== 'undefined' && _otaInProgress.size > 0) return;  // don't fight an OTA
const t = _wdpMeshConn();
...
const raw = await t.conn.sendAndCollect(`${t.fc}WDP,DUMP`, 4000, '[WDP:END');
```
(app.js:12609-12616). There is no equivalent guard for a config PUSH. `_wdpMeshConn()` returns the first connected board (app.js:12458-12466), which for a single-board setup — or for the first board of a Push All — is exactly the board being pushed.

`boardGo`'s push loop paces on `sendAndAwaitIdle` (app.js:7483-7491), whose acknowledgement test is purely "did ANY line arrive": `resolve({ ok: lines.length > 0, lines })` (app.js:5280), with the quiet timer restarted on every line (app.js:5287-5288). Because `_handleLine` fans every line to every registered callback (app.js:5313), the multi-line `?WDP,DUMP` reply lands inside whatever push command's window is open at the time.

A full push is easily 40+ commands at ~0.3-1 s each, so a 12 s tick is guaranteed to fire inside one.

**Failure scenario.** One WCB is connected on slot 1 and the user clicks Push Config after a wizard run, producing a ~40-command full push. Twelve seconds in, `meshAutoDiscoverTick` writes `?WDP,DUMP` to the same port between two config commands. The firmware answers with the roster (`[WDP:…]` rows plus `[WDP:END]`). Whichever config command was outstanding when those rows arrived is scored `ok: true` by `sendAndAwaitIdle` even though the board never processed it — so the retry at app.js:7486 does not fire, `_pushFullyAcked` stays true, and the board is badged 'configured' (app.js:7572). The setting is silently missing from NVS; the user only discovers it when the behaviour is wrong on the bench.

**Suggested fix.** Add a push-busy flag in the same style as `_statsFetchBusy`: set it at the top of `boardGo`'s push section (before the `for (const cmd of commands)` loop at app.js:7481) and clear it in a `finally`, then add `if (_pushBusy) return;` alongside the existing guards at app.js:12610. Optionally also tighten `sendAndAwaitIdle` to require a line that is not obviously foreign (e.g. ignore lines matching the `[WDP…]` dump prefixes when scoring `ok`).

### F-189 · S2 · `Wizard/flasher.js:414`

**Recognized-but-unsupported chips (ESP32-S2/C2/C3/C5/C6/C61/H2/P4) are flashed with ESP32 or S3 firmware whenever a HW version is selected**

*Category:* `logic`

Step 3a's allowlist deliberately leaves `detectedType === null` for every non-ESP32/non-S3 part (`Wizard/flasher.js:385-389`), and its comment at `:381-383` states the intent explicitly: "Strict allowlist: only the two families we ship firmware for. Variants (S2, C3, C6, H2, P4, …) must NOT fall through to the classic ESP32 binary."

But the handler for `detectedType === null` only refuses when **no** HW version was selected:

```js
} else if (!detectedType) {
    if (!hwVersion) { ... throw new Error(msg); }
    onLog(`Could not determine chip family from "${chipName}" — falling back to the HW version selection (${binaryType})`);
}
```

With a HW version selected — the normal flow, since the Wizard's board card carries `boardConfigs[n].hwVersion` and `Wizard/app.js:7052` treats it as a hint — execution simply continues and `binaryType` stays the `WCBParser.HW_VERSION_MAP[...]` guess from `:327`. I verified the regexes against the actual `CHIP_NAME` constants in the vendored bundle (`ESP32, ESP32-C3, ESP32-C2, ESP32-C6, ESP32-C61, ESP32-C5, ESP32-H2, ESP32-S3, ESP32-S2, ESP8266, ESP32-P4`): every one of the unsupported parts falls into this branch, so the classic-ESP32 or S3 image set is written to it. The code conflates "I could not parse this chip string" (a safe fall-back) with "I parsed it and it is definitely a chip we ship no firmware for" (must abort).

The offsets make it worse: on a C3/C6/S2/H2 the bootloader offset is 0x0, but `bootAddr` at `:426` is 0x1000 for anything that is not `ESP32S3`, so the ESP32 bootloader lands at 0x1000 and 0x0 is left blank.

**Failure scenario.** User has HW 2.4 selected on a board card (or picks it because the dropdown must be filled), then plugs in an ESP32-C3 or ESP32-S2 dev board and presses Flash. Terminal logs "Could not determine chip family from \"ESP32-C3\" — falling back to the HW version selection (ESP32)", the full ESP32 bootloader+partition+app set is written at 0x1000/0x8000/0x10000, esptool reports success, and the board no longer boots at all (nothing at 0x0) — recoverable only with BOOT+RST and an external esptool.

**Suggested fix.** Split the null case in two. Add a known-unsupported test before the fall-back, e.g. `const isKnownUnsupported = /^ESP(8266|32[-_](S2|C\d+|H\d+|P\d+))/i.test(chipName);` and, when it matches, log + toast + `await transport.disconnect()` + `throw` exactly like the `!hwVersion` branch at `:405-412`. Keep the fall-back only for chip strings that match neither the allowlist nor the unsupported list.

### F-190 · S2 · `Wizard/flasher.js:686`

**Flash progress bar stalls at ~62% — esptool reports COMPRESSED bytes but the denominator is the uncompressed total**

*Category:* `logic`

`totalBytes` is computed from the raw image sizes (`Wizard/flasher.js:644`: `imagesToFlash.reduce((sum, img) => sum + img.buf.byteLength, 0)`), but with `compress: true` esptool-js reports progress in *compressed* bytes. In the vendored bundle's `writeFlash`:

```js
if (t.compress) { const A=this.bstrToUi8(e); e=this.ui8ToBstr(Ce(A,{level:9})), h=await this.flashDeflBegin(n, e.length, i) } ...
let r=0, g=0; const B = e.length;              // B = COMPRESSED length (e was reassigned above)
t.reportProgress && t.reportProgress(s, 0, B);
... g += E.length; ... t.reportProgress && t.reportProgress(s, g, B);
```

So both `written` and `total` handed to the callback at `:684` are compressed counts, while `onProgress(bytesWritten + written, totalBytes)` divides them by the uncompressed total. `Wizard/app.js:updateFlashBar` renders that straight as `Math.round(written/total*100)` into the bar width and into the status text ("Flashing… NN%").

Measured with the committed binaries and pako-equivalent deflate level 9: `WCB_ESP32.bin` 1284464 → 803458 (0.626), `WCB_ESP32S3.bin` 1250352 → 789256 (0.631), `WCB_ESP32_boot.bin` 25184 → 16515, `WCB_ESP32S3_part.bin` 3072 → 145, the 8 KB 0xFF otadata blank → 31. A full ESP32 flash therefore reports ≈820149 of 1320912 = **62%** at completion; an app-only update reports ≈803489 of 1292656 = **62%**.

**Failure scenario.** User clicks Flash. The bar climbs to 62% over ~40 s and then stops moving while esptool finishes the MD5 verify and the final `flashDeflFinish` — during exactly the window where `:656` told them "DO NOT DISCONNECT THE BOARD until flashing completes!". It then snaps to 100% only at `:717`. A user who reads the stalled 62% as a hang and unplugs mid-write gets the half-written bootloader/partition state the same warning exists to prevent.

**Suggested fix.** Track progress per file against the per-file `total` esptool supplies rather than against a precomputed byte total: keep a `filesDone` counter and report `onProgress(filesDone + (total ? written/total : 0), imagesToFlash.length)` — i.e. make the unit "fraction of images" — or, if byte units are wanted, weight each file's fraction by `imagesToFlash[_fileIdx].buf.byteLength` so the ratio is compressed/compressed within a file and uncompressed-weighted across files.

### F-191 · S2 · `Wizard/flasher.js:720`

**Post-flash hard reset never runs — `loader.afterFlash` does not exist in the vendored esptool-js 0.4.7**

*Category:* `undefined-ref`

Step 5 reads:

```js
// esptool-js 0.4.x renamed after_flash → afterFlash
const afterFlashFn = loader.afterFlash ?? loader.after_flash;
try { if (afterFlashFn) await afterFlashFn.call(loader, 'hard_reset'); } catch (_) {}
```

Neither name exists on `ESPLoader` in the vendored bundle. I enumerated every async method in `Wizard/vendor/esptool-js/esptool-js-0.4.7.bundle.js`: `_connectAttempt, changeBaud, changeBaudRate, checkCommand, command, connect, detectChip, disconnect, eraseFlash, flashBegin, flashBlock, flashDeflBegin, flashDeflBlock, flashDeflFinish, flashFinish, flashId, flashMd5sum, flashSpiAttach, flushInput, getBlock2Version, ... hardReset, ... writeFlash, writeReg`. Grepping the bundle for the literal strings `afterFlash` and `after_flash` returns false for both; the real method is `hardReset()` (`async hardReset(){await this.transport.setRTS(!0),await this._sleep(100),await this.transport.setRTS(!1)}`, and `_sleep` does exist on ESPLoader, so it is callable).

So `afterFlashFn` is always `undefined`, the `if` guard is always false, and **no reset is ever issued** — while `Wizard/flasher.js:715` logs "Resetting board into firmware…" and `:724` logs "Flash complete — board rebooting". The board is left sitting in the ROM/stub download mode that `loader.main()` ("default_reset") put it in. The only thing that actually reboots it is the incidental DTR/RTS transition from `Wizard/app.js:7113-7116` closing and re-opening the port — which app.js's own comment describes as a CH9102F DTR workaround, not a reset, and which depends entirely on the USB-bridge driver's line-state defaults.

**Failure scenario.** Flash any WCB from the Wizard on a board/driver combination where re-opening the port does not produce an EN pulse (e.g. a bridge whose driver leaves RTS deasserted on open, or the S3's native USB path). Terminal prints "Resetting board into firmware… / Flash complete — board rebooting", but the chip stays in the ESP download stub. app.js then reconnects and `waitForBoardReady`/the post-flash pull gets no firmware banner and no reply to any `?` command, so the flash reports success and the board appears dead until the user power-cycles it manually.

**Suggested fix.** Call the method that exists: `const resetFn = loader.hardReset ?? loader.afterFlash ?? loader.after_flash;` and invoke it with no argument (`await resetFn.call(loader)`), or `await transport.setRTS(true); await sleep(100); await transport.setRTS(false);` directly. Also log the reset only when one was actually performed, so a future library rename cannot silently reintroduce a lying success message.

### F-192 · S2 · `Wizard/index.html:640`

**Kyber Maestro Baud <select> cannot represent 5 of the 13 firmware-legal baud rates — a pulled 19200 blanks the control and Push silently rewrites the port to 115200**

*Category:* `round-trip-loss`

`<select id="b{N}-kyber-baud" onchange="onKyberBaudChange({N})">` (index.html:640-645) offers only four options: 9600 / 38400 / 57600 / 115200.

This select is the ONLY one of the four Kyber controls that app.js never repopulates. `b{N}-kyber-port` and `b{N}-kyber-marc-port` are rebuilt at runtime (`updateKyberPortDropdown`, app.js:979; `updateKyberMarcPortDropdown`, app.js:1000) so their static options are just placeholders — but nothing ever rewrites `b{N}-kyber-baud`'s option list (grep for `kyber-baud` in app.js returns only `.value` reads/writes at 907-908, 954-955, 967, 2044, 2998-2999, plus the `-wrap` display toggles).

The value written into it is not restricted to those four. app.js:2994-2999 does:
```js
// Derive kyber baud from the serial port baud (populated from ?BAUD commands in backup)
const kyberBaud = config.serialPorts[config.kyber.port - 1]?.baud
               ?? config.kyber.baud
               ?? 115200;
const baudSel = document.getElementById(`b${n}-kyber-baud`);
if (baudSel) baudSel.value = kyberBaud;
```
`config.serialPorts[i].baud` comes straight off the board's `?BAUD` line — parser.js:566-572 does `config.serialPorts[portIdx].baud = parseInt(parts[2]) || 9600` with no whitelist. The firmware accepts thirteen rates (Code/WCB/WCB_Storage.cpp:155-157: `110, 300, 600, 1200, 2400, 9600, 14400, 19200, 38400, 57600, 115200, 128000, 256000`), and S1/S2 in the Wizard's own serial table offer all thirteen (app.js:7 `BAUD_RATES`, app.js:737-739 with `maxBaud = Infinity` for p<3).

Setting `select.value` to a value with no matching `<option>` makes `selectedIndex` −1 and `value` the empty string. The control renders blank, and the next read coerces it to the default:
- app.js:2044 `const baud = parseInt(document.getElementById(`b${n}-kyber-baud`)?.value) || 115200;`
- app.js:2048 `config.serialPorts[portVal - 1].baud = baud;`  ← also overwrites the real serial-port baud

`syncKyberToConfig()` runs unconditionally on every push — app.js:7390 (direct/USB `boardGo`) and app.js:7782 (relay push) — so the corruption is committed on the very next Push, not only if the user touches the field.

**Failure scenario.** A board runs a Kyber Maestro on Serial 2 at 19200 baud (`?BAUD,S2,19200` + `?KYBER,LOCAL,S2` — both accepted by the firmware; WCB_Storage.cpp:155 lists 19200 as valid). User connects it in the Wizard and clicks Pull Config. parser.js records serialPorts[1].baud = 19200; app.js:2998 sets `b1-kyber-baud`.value = 19200, which has no option, so the Kyber Maestro Baud dropdown renders EMPTY. The user changes something unrelated (e.g. a sequence) and clicks Push Config. syncKyberToConfig (app.js:7390 → 2044) reads `parseInt('') || 115200` → 115200, writes it to config.kyber.baud AND to config.serialPorts[1].baud (app.js:2048). parser.js:1314 then emits `?BAUD,S2,115200`. The board's Serial 2 is reprogrammed to 115200, the Kyber Maestro link goes silent, and nothing in the UI ever showed the value being changed.

**Suggested fix.** Populate `b{N}-kyber-baud` from the shared `BAUD_RATES` list at render time the way the serial table does (app.js:737-739), or at minimum add the missing `<option>` values (110, 300, 600, 1200, 2400, 14400, 19200, 128000, 256000) so every firmware-legal rate is representable. Additionally, make app.js:2998 fall back safely — after `baudSel.value = kyberBaud`, check `if (baudSel.selectedIndex < 0)` and append an option for the actual value rather than letting it silently blank.

### F-193 · S2 · `Wizard/index.html:809`

**MP3 baud picker offers values the firmware rejects outright, discarding the whole MP3 config**

*Category:* `protocol-contract`

The MP3 baud `<select>` (index.html:809-822) offers all 13 values from `BAUD_RATES`: 110, 300, 600, 1200, 2400, 9600, 14400, 19200, 38400, 57600, 115200, 128000, 256000. The firmware accepts exactly two. `Code/WCB/WCB_MP3.cpp:287` reads `if (baudRate != 9600 && baudRate != 38400) { Serial.println("[MP3] MP3 Trigger only supports 9600 or 38400 baud"); return; }` and `Code/WCB/WCB_MP3.cpp:292` reads `if (serialPort >= 3 && baudRate > 9600) { ... "CONFIGURATION BLOCKED!" ... return; }`. Both `return` before any state is applied (the assignments do not start until `WCB_MP3.cpp:341`), so the ENTIRE `?MP3,S<p>:<b>:V<v>` line built at parser.js:1364 is discarded — port and volume included, not just the baud.

The Wizard's soft-serial cap is also the wrong number. `app.js:2236` (`opt.hidden = isSoftSerial && parseInt(opt.value) > 57600`) and `app.js:3051` cap S3-S5 at 57600, and `app.js:2240` clamps a too-high selection to `baudSel.value = '57600'`. The firmware caps S3-S5 at 9600. So the Wizard's own clamp produces 57600 — a value `WCB_MP3.cpp:287` rejects on every port. The tooltip at index.html:808 states "Serial 3-5 are capped at 57,600", which contradicts WCB_MP3.cpp:292.

This is drift, not a shared gap: the sibling HCR section was reconciled with its firmware. index.html:811-815 offers exactly 9600/19200/38400/57600/115200, matching `WCB_HCR.cpp:631` (`if (baudRate != 9600 && baudRate != 19200 && baudRate != 38400 && ...)`), and `_hcrApplyBaudCap` (app.js:2545) caps S3-S5 at 9600, matching `WCB_HCR.cpp:635`. DFPlayer is also correct (fixed 9600 = `DFP_BAUD`, WCB_DFP.cpp:28). MP3 is the only one that was not updated.

**Failure scenario.** User enables MP3 Trigger on Serial 2, sets Baud Rate to 19200 (offered by the dropdown), clicks Push Config. The Wizard sends `?MP3,S2:19200:V0`. The firmware prints "[MP3] MP3 Trigger only supports 9600 or 38400 baud" and returns without configuring anything. The Wizard does not parse that reply, clears the unsaved badge and updates its baseline, so the UI shows the MP3 Trigger as configured on S2 while the board has no MP3 device at all. Same outcome for 57600 — which is also the value the Wizard auto-clamps to when the user moves MP3 onto Serial 3, 4 or 5.

**Suggested fix.** Replace the MP3 baud option list in index.html:810-822 with only 9600 and 38400, mirroring how the HCR list mirrors WCB_HCR.cpp:631. Change the soft-serial cap in app.js:2236, app.js:2239-2241 and app.js:3051 from 57600 to 9600 to match WCB_MP3.cpp:292, and update the tooltip at index.html:808 to say Serial 3-5 are limited to 9600. Ideally factor the clamp into an `_mp3ApplyBaudCap(n)` helper alongside `_hcrApplyBaudCap` and call it from onMP3Change as well as onMP3PortChange.

### F-194 · S2 · `Wizard/index.html:809`

**MP3 baud picker offers 13 rates; firmware accepts only 9600/38400 — a non-matching pick silently drops the entire MP3 config**

*Category:* `protocol-contract`

The MP3 Trigger baud `<select id="b{N}-mp3-baud">` (Wizard/index.html:809-822) offers `110,300,600,1200,2400,9600,14400,19200,38400,57600,115200,128000,256000`. The firmware's `configureMP3()` rejects everything except two values: `if (baudRate != 9600 && baudRate != 38400) { Serial.println("[MP3] MP3 Trigger only supports 9600 or 38400 baud"); return; }` (Code/WCB/WCB_MP3.cpp:287-290), and separately blocks anything above 9600 on S3-S5: `if (serialPort >= 3 && baudRate > 9600) { ... "CONFIGURATION BLOCKED!" ... return; }` (Code/WCB/WCB_MP3.cpp:292-304). Both `return` BEFORE `mp3Config` is written, so nothing is stored.

The damage is compounded by push ordering. `syncMP3ToConfig()` copies the chosen baud onto the underlying port — `config.serialPorts[config.mp3.port - 1].baud = config.mp3.baud` (Wizard/app.js:2311-2313) — and `buildCommandString` emits the `?BAUD,S<p>,<rate>` block (Wizard/parser.js:1309-1315) BEFORE the `?MP3,...` block (parser.js:1358-1370). `updateBaudRate()` accepts all 13 rates (Code/WCB/WCB_Storage.cpp:155-160), so the port baud IS applied while the MP3 device config is silently rejected.

The inline hint is also wrong: it says "Serial 3–5 are capped at 57,600" (index.html:808) and `onMP3PortChange()` enforces exactly that cap (Wizard/app.js:2229-2240) — but the firmware's soft-serial ceiling for MP3 is 9600, not 57600.

**Failure scenario.** User sets MP3 Trigger on Serial 2 and picks 19,200 baud (a rate the dropdown offers), then Push Config. The board applies `?BAUD,S2,19200` and prints "[MP3] MP3 Trigger only supports 9600 or 38400 baud" for the `?MP3,S2:19200:V0` that follows. The Wizard reports the push succeeded. The MP3 Trigger is never registered, `;A` commands do nothing, and S2 is now running at a baud the MP3 Trigger cannot speak. The next pull shows no MP3 configured, so the diff re-sends the same rejected command on every subsequent push.

**Suggested fix.** Restrict the MP3 baud `<select>` to 9600 and 38400 only, and change the soft-serial filter in `onMP3PortChange()` (app.js:2231-2240) from `> 57600` to `> 9600` so S3-S5 offers 9600 alone. Update the tooltip at index.html:808 to match the firmware ("MP3 Trigger supports 9600 or 38400; Serial 3-5 are limited to 9600").

### F-195 · S2 · `Wizard/parser.js:905`

**`?WDP,OFF` / `?WDP,AUTOJOIN,OFF` are dropped by the parser and never re-emitted — a restore silently turns discovery back on**

*Category:* `round-trip-loss`

`collectConfigCommands` deliberately puts the WDP disable states into the restorable config chain (WCB.ino:2874-2882):
```
  // WDP settings — both default ON, so only the OFF states need to ride the
  // config chain (otherwise a restore silently reverts a deliberate ?WDP,OFF /
  // ?WDP,AUTOJOIN,OFF back to ON on the replacement board)...
  if (!wdpEnabled)  emit("WDP,OFF", true);
  if (!wdpAutoJoin) emit("WDP,AUTOJOIN,OFF", true);
```
`parseToken` has no `WDP` case, so both fall into `default:` (:905-910) and are only `console.debug`'d as "Unknown token"; nothing is stored on the BoardConfig, and `buildCommandString` has no WDP emitter. The firmware accepts these on input (`WCB.ino:4941 if (rootUpper == "WDP") processWdpCommand(args);`), so the round trip is broken purely on the Wizard side.

Verified with a full backup round trip: `WDP,OFF` and `WDP,AUTOJOIN,OFF` were logged as unknown tokens on parse and are absent from the rebuilt command string. The exact regression the firmware comment warns about is what the Wizard produces.

**Failure scenario.** Operator disables discovery on a board (`?WDP,OFF`, or `?WDP,AUTOJOIN,OFF` to stop it adopting new peers). They pull the board into the Wizard and export a config file / do a post-flash full push. The restore never contains the WDP lines, so the replacement board comes up with WDP and auto-join ON — the board starts advertising and auto-adopting peers again, and the deliberate setting is lost with no indication.

**Suggested fix.** Add `wdpEnabled: true, wdpAutoJoin: true` to `createDefaultBoardConfig`, a `case 'WDP':` in `parseToken` handling `WDP,OFF`/`WDP,ON` and `WDP,AUTOJOIN,OFF`/`,ON`, and emit `WDP,OFF` / `WDP,AUTOJOIN,OFF` (only for the OFF states, mirroring the firmware) from `buildCommandString`.

### F-196 · S2 · `Wizard/parser.js:905`

**Parser drops `?WDP,OFF` and `?WDP,AUTOJOIN,OFF` from a board backup — a config export/restore silently re-enables WDP discovery**

*Category:* `round-trip-loss`

The firmware deliberately puts the WDP opt-outs in every config backup: `if (!wdpEnabled) emit("WDP,OFF", true); if (!wdpAutoJoin) emit("WDP,AUTOJOIN,OFF", true);` (Code/WCB/WCB.ino:2892-2893). The comment two lines above states the reason outright: "both default ON, so only the OFF states need to ride the config chain (otherwise a restore silently reverts a deliberate ?WDP,OFF / ?WDP,AUTOJOIN,OFF back to ON on the replacement board)" (WCB.ino:2886-2890).

`parseToken()` has no `case 'WDP'`. The full case list is HW, RELAY, LED, WCB, WCBQ, WCBCH, PEERSLIVE, CONTROLLER/SPECIAL, ALIAS, EPASS, MAC, DELIM, FUNCCHAR, CMDCHAR, BAUD, LABEL, BCAST, KYBER, MP3, HCR, DFP, WLED, MAESTRO, ETM, SEQ, VAR, MAP, CHK (Wizard/parser.js:471-903). `?WDP,OFF` therefore hits `default: … console.debug('[WCB Parser] Unknown token:', body);` (parser.js:905-910) and is discarded. `grep -in 'autojoin|wdpenab' Wizard/parser.js` returns nothing — there is no field in `createDefaultBoardConfig()` to hold it, and `buildCommandString` never re-emits it.

The firmware accepts both on input (`?WDP,ON`/`?WDP,OFF` at Code/WCB/WCB_WDP.cpp:1218-1219, `?WDP,AUTOJOIN,ON|OFF` at :1223-1232), so the round trip is broken only on the Wizard side. There is also no Wizard control for WDP enable/disable at all — `wdpAutoJoinToggle()` (app.js:2497) sends a live `?WDP,AUTOJOIN,…`, but that is a one-shot mesh-panel action, not part of the saved/pushed config.

**Failure scenario.** User deliberately turns WDP off on a board (`?WDP,OFF`) — for example to stop auto-adopting a neighbour — then uses the Wizard to pull the config and Export System File as a backup. The exported file contains no WDP line. When that board dies and the user restores the file onto a replacement, WDP and auto-join come up ON (the firmware defaults), the replacement starts auto-joining learned peers, and the deliberate opt-out is lost with no warning. The same loss happens on any pull → push cycle within one session, because the baseline never carried the setting.

**Suggested fix.** Add `wdpEnabled: true, wdpAutoJoin: true` to `createDefaultBoardConfig()`, a `case 'WDP':` in `parseToken()` that sets them from `WDP,OFF` / `WDP,AUTOJOIN,OFF`, and matching emits in `buildCommandString` that send the OFF forms (and `WDP,ON` / `WDP,AUTOJOIN,ON` on a change back). An advanced-only checkbox pair would then also give the setting a control.

### F-197 · S2 · `Wizard/parser.js:1143`

**Two boards that share a WCB number collapse into one on config-file save → reload (whole board config lost)**

*Category:* `round-trip-loss`

`buildSystemFile` names each section purely by board number — `lines.push(`[WCB${board.wcbNumber}]`)` (:1580) — while `parseSystemFile` stores sections in a plain object keyed by that name: `sections[currentSection] = currentLines.join('\n')` (:1143 and :1152). A repeated section name silently overwrites the earlier one, and the `[WCB<n>]` loop at :1172-1182 then produces one board instead of two.

Board number is NOT unique across UI slots — app.js states this explicitly at :7857 ("Route by the board's configured WCB NUMBER, not its UI slot — the two can differ"), and `exportSystemFile` (app.js:8418-8432) pushes `boardConfigs[n]` for every populated slot regardless of duplicate `wcbNumber`.

Verified: a two-board system where both boards report WCB 1 exports two `[WCB1]` blocks, and `parseSystemFile` returns `boards.length === 1` (only the last one, alias 'Body', hw 31 — the first board's alias, HW version and its `?MAESTRO,M1:W1S1:57600` are gone). Nothing warns.

**Failure scenario.** User is setting up a new droid: two freshly flashed boards both still report `?WCB,1`. They connect both, pull both, enter aliases/Maestros, and click Export Config to save their work before renumbering. Reloading that file (Import Config) shows a single board — the first board's entire configuration is silently gone, while `wcbQuantity` still says 2.

**Suggested fix.** Make section names unique on write (e.g. `[WCB1]`, `[WCB1#2]`, or index-based `[BOARD1]` with a `?WCB,n` inside), and on read accumulate instead of overwrite: `if (sections[currentSection]) sections[currentSection] += '\n' + …` or push into an array of sections. At minimum, detect a duplicate `wcbNumber` in `buildSystemFile` and refuse/warn rather than emitting a file that cannot round-trip.

### F-198 · S2 · `Wizard/parser.js:1321`

**Clearing a serial-port label never reaches the board, and the stale label resurrects a phantom Kyber Marcuino port on the next pull**

*Category:* `round-trip-loss`

`buildCommandString` only emits a label when the new value is non-empty:
```
1321:    if (cur.label && (fullPush || !base || base.label !== cur.label))
1322:      add(`LABEL,S${i+1},${cur.label}`);
```
Going from "Dome Maestro" to "" therefore emits nothing at all, even though the firmware has a clear verb — `WCB.ino:4570-4591` handles `?LABEL,CLEAR,S<n>` and `?LABEL,CLEAR,ALL` (`clearSerialLabel(...)`). Verified: parse a board with `?LABEL,S2,Kyber Marcuino`, clear the label in the model, build a delta push → the result is the empty string, so app.js:7420 reports "No changes to push".

This is worse than a cosmetic loss because parser.js:277-282 derives Kyber's Marcduino port from that very label:
```
  if (config.kyber.mode === 'local') {
    const marcIdx = config.serialPorts.findIndex(p => p.label?.toLowerCase() === 'kyber marcuino');
    config.kyber.marcduinoPort = marcIdx >= 0 ? marcIdx + 1 : null;
```
and app.js clears that auto-label locally when the port is deselected (app.js:1028-1030, :1296-1297 "orphaned auto-label — clear so it isn't pushed"). Because the clear is never transmitted, the board keeps `LABEL,S3,Kyber Marcuino`, and the next pull re-derives `kyber.marcduinoPort = 3` and re-claims S3 as `kyber-marc` in `evaluatePortClaims` (:999-1003).

**Failure scenario.** User has Kyber local with a Marcduino on S3, then removes the Marcduino (sets the Marcduino port to none) and pushes. The push contains nothing for S3's label; the board still stores "Kyber Marcuino". On the next Pull, S3 comes back marked as the Kyber Marcuino port and is locked out of the device dropdowns, so the user cannot assign S3 to an MP3/HCR/WLED device — the edit is silently undone. Same for any hand-typed port label the user deletes.

**Suggested fix.** In the label loop, emit a clear when the value went from set to empty, mirroring the alias handling at :1258-1265: `if (!cur.label) { if (base?.label) add(`LABEL,CLEAR,S${i+1}`); } else if (…) add(`LABEL,S…`)`.

### F-199 · S2 · `Wizard/parser.js:1321`

**Clearing a serial port label never reaches the board — `?LABEL,CLEAR,Sx` is never emitted**

*Category:* `round-trip-loss`

`buildCommandString` guards the label emit on the label being non-empty: `if (cur.label && (fullPush || !base || base.label !== cur.label)) add(\`LABEL,S${i+1},${cur.label}\`)` (Wizard/parser.js:1321-1322). There is no branch that emits a clear when a label goes from set to empty — grep for `LABEL` across Wizard/parser.js returns only lines 276 (a comment), 577 (the parse case) and 1322 (this emit), and `grep -n 'LABEL' Wizard/app.js` finds no clear path either.

The firmware fully supports the clear: `?LABEL,CLEAR,Sx` and `?LABEL,CLEAR,ALL` are handled at Code/WCB/WCB.ino:4576-4586 (`if (labelArg1Upper == "CLEAR") { … clearSerialLabel(target.substring(1).toInt()); … }`).

The UI reads the emptied field back into the model — `config.serialPorts[p - 1].label = document.getElementById(\`b${n}-s${p}-label\`)?.value ?? ''` (Wizard/app.js:2917) — so the model correctly says "no label", but nothing is ever sent. The board's backup only emits a label when non-empty (`if (serialPortLabels[i].length() > 0) emit("LABEL,S" + ... )`, Code/WCB/WCB.ino:2806-2807), so the stale label comes straight back on the next pull.

**Failure scenario.** User pulls a board that has `?LABEL,S3,Periscope` stored, deletes the text in the S3 Label field, and clicks Push Config. No command is generated for it. The board still reports "Periscope" for S3, and the automatic re-pull after the push repopulates the field — the deletion visibly undoes itself with no error shown.

**Suggested fix.** Add a clear branch alongside the emit, mirroring how ALIAS already handles it (parser.js:1255-1263): when `base?.label` is non-empty and `cur.label` is empty, `add(\`LABEL,CLEAR,S${i+1}\`)`.

### F-200 · S2 · `Wizard/parser.js:1355`

**Full push emits `KYBER,CLEAR` after the BAUD/BCAST lines, and the firmware's argument-less CLEAR resets S2 to 9600 baud and re-enables S2 broadcast**

*Category:* `protocol-contract`

When `config.kyber.mode` is neither 'local' nor 'remote', `buildCommandString` emits a bare `add('KYBER,CLEAR')` (:1355). On a full push (`fullPush === true`, i.e. `!boardBaselines[n]` — post-flash and config-file restore) `kyberChanged` (:1340) is unconditionally true, so `?KYBER,CLEAR` is ALWAYS sent — and it is emitted at :1355, i.e. AFTER the baud loop (:1309-1315) and the broadcast loop (:1325-1336).

`?KYBER,CLEAR` reaches `storeKyberSettings("CLEAR")` (WCB.ino:4655 → WCB_Storage.cpp:1072). With no comma in the argument, `WCB_Storage.cpp:1078-1081` hardcodes `kyberPort = 2`, and the clear branch then does:
```
    if (kyberPort > 0) {
      updateBaudRate(kyberPort, 9600);          // WCB_Storage.cpp:1195
      ...
    if (!serialBroadcastEnabled[kyberPort - 1]) { serialBroadcastEnabled[...] = true;  // :1200-1203
    if (blockBroadcastFrom[kyberPort - 1])      { blockBroadcastFrom[...]      = false; // :1205-1208
```
So the S2 baud rate and both S2 broadcast flags the Wizard pushed seconds earlier are overwritten and persisted to NVS. Confirmed by the generated order in a full-push round trip: `?BAUD,S2,57600 … ?BCAST,OUT,S2,OFF … ?KYBER,CLEAR`.

**Failure scenario.** Board has a Maestro on S2 at 57600 with broadcast disabled, and no Kyber. User flashes the board and does the post-flash full push (or restores a saved WCB_system.txt). Push reports success, but S2 is left at 9600 baud with broadcast IN/OUT re-enabled: the Maestro stops responding to `;M…`, and S2 serial traffic starts flooding the mesh. The next pull shows 9600, so the UI silently agrees with the wrong value.

**Suggested fix.** Emit the Kyber block BEFORE the baud/label/broadcast blocks (so any firmware side-effects are overwritten by the explicit values that follow), and/or skip `KYBER,CLEAR` when there is nothing to clear (`fullPush && config.kyber.mode === 'none'` on a board whose baseline also had no Kyber). Sending `KYBER,CLEAR,S<port>` with the port the UI actually considers free would also stop the hardcoded S2 side-effect.

### F-201 · S2 · `Wizard/parser.js:1355`

**`?KYBER,CLEAR` is sent with no port, so the firmware resets Serial 2 to 9600 and re-enables its broadcast — undoing settings from earlier in the same push**

*Category:* `protocol-contract`

`buildCommandString` emits a bare `add('KYBER,CLEAR')` with no port argument (Wizard/parser.js:1355) whenever `config.kyber.mode` is neither `'local'` nor `'remote'` — which is the default (`mode: 'none'`, parser.js:65) and therefore fires on EVERY full push.

In the firmware, `storeKyberSettings("clear")` takes the no-comma branch and hardcodes the port: `if (firstComma == -1) { ... kyberPort = 2; ... }` (Code/WCB/WCB_Storage.cpp:1078-1082). The clear branch then acts on that assumed port: `if (kyberPort > 0) { updateBaudRate(kyberPort, 9600); ... }` (WCB_Storage.cpp:1191-1193), followed by `if (!serialBroadcastEnabled[kyberPort - 1]) { serialBroadcastEnabled[kyberPort - 1] = true; saveBroadcastSettingsToPreferences(); }` and `if (blockBroadcastFrom[kyberPort - 1]) { blockBroadcastFrom[kyberPort - 1] = false; saveBroadcastBlockSettings(); }` (WCB_Storage.cpp:1199-1208). So a bare CLEAR always rewrites S2's baud and both S2 broadcast flags, regardless of which port Kyber actually used.

`?KYBER,CLEAR` is emitted AFTER the `?BAUD,S2,*` and `?BCAST,*,S2,*` commands in the same chain (parser.js:1309-1341 vs :1355). I confirmed the generated order by running `buildCommandString` under node with S2 at 115200 and both S2 broadcast flags off: the chain is `?BAUD,S2,115200 … ?BCAST,OUT,S2,OFF ?BCAST,IN,S2,OFF … ?KYBER,CLEAR`. The commands are sent one at a time (app.js:7482-7495), so the last writer wins and S2 lands at 9600 with broadcast on. Full push is a mainline flow, not an edge case: `boardBaselines[n] = null; // force full push — board NVS is blank` after the Wizard erases NVS (Wizard/app.js:7341 and :7357).

**Failure scenario.** User has no Kyber (mode 'none'), sets Serial 2 to 115200 with broadcast in/out off (e.g. a WLED or Marcduino wired there), and runs the factory-reset-with-firmware flow (erase NVS, then push). The push sends `?BAUD,S2,115200`, `?BCAST,OUT,S2,OFF`, `?BCAST,IN,S2,OFF`, then `?KYBER,CLEAR`. The board ends up with S2 at 9600 and broadcast re-enabled in both directions. The device on S2 stops working, and the next pull shows 9600/on — so the Wizard's own screen silently reverts what the user set.

**Suggested fix.** Emit the port with the clear so the firmware acts on the right one — `add(\`KYBER,CLEAR,S${config.kyber.port || 2}\`)` — or, better, only emit `KYBER,CLEAR` when the baseline actually had Kyber configured (`baseline?.kyber?.mode === 'local' || 'remote'`) instead of on every full push. A firmware-side guard (skip the baud/broadcast reset when no port was ever stored) would also close it.


---

## S3

### F-202 · S3 · `c:/Users/ghulette/Documents/GitHub/Wireless_Communication_Board-WCB/Wizard/serial-hub.js:283`

**`_becomeLeader()`'s leaving-path calls `_releaseLock()` without nulling it, permanently blocking `_maybeCampaign()`**

*Category:* `race`

```js
async _becomeLeader() {
  if (this._leaving) { this._releaseLock && this._releaseLock(); return; }
```

It invokes the release function but leaves `this._releaseLock` set to it. `_maybeCampaign()` guards with `if (this._lockAbort || this._releaseLock) return;` (serial-hub.js:259), reading a non-null `_releaseLock` as "already leading". So after this branch the hub instance is idle, holds no lock, yet can never enter the election again — a permanently mute hub. Every other exit path is careful about this: `_stepDownPortless` (:304), `_relinquish` (:479) and `leave()` (:215) all capture-then-null.

The window is real: `leave()` aborts a queued lock request at :214 and clears `_releaseLock` at :215 while it is still null, but per the Web Locks spec an `AbortSignal` only cancels a request that has not yet been granted. If the grant is already queued when `abort()` runs, the lock callback still fires afterwards, re-assigns `this._releaseLock = release` (:274), and `_becomeLeader()` then takes this `_leaving` branch — leaving the stale non-null behind.

Reachability in the Wizard today is limited because app.js discards and rebuilds `_sharedHub` on every shared connect (Wizard/app.js:5488, 5659, 5716, 5760), but `leaveShared()` (Wizard/app.js:5444, called from boardDisconnect at :6202) calls `hub.leave()` WITHOUT nulling `_sharedHub`, so the poisoned instance does survive a disconnect until the next `sharedConnect()`.

**Failure scenario.** User clicks Disconnect on a shared board at the instant the browser grants it the Web Lock (a promotion racing the disconnect). `leave()` aborts too late; the grant callback runs, sees `_leaving`, releases and returns, but leaves `_releaseLock` set. The hub singleton is now permanently ineligible: if anything calls `join()` on it again before a full `sharedConnect()` rebuild, it sits as a follower forever and never campaigns even when it holds a port.

**Suggested fix.** Capture and null before releasing, like every other exit path: `if (this._leaving) { const r = this._releaseLock; this._releaseLock = null; if (r) { try { r(); } catch (_) {} } return; }`. Mirror into the NaviCore copy.

### F-203 · S3 · `Wizard/app.js:457`

**reconcileBoardGrid deletes boardConfigs but leaves boardBaselines, so a re-added slot pushes an incremental diff against a different board's state**

*Category:* `logic`

When a slot is evicted, only two of the parallel per-slot maps are cleaned up:

```js
455:    el.remove();
456:    delete boardConnections[n];
457:    delete boardConfigs[n];
```

`boardBaselines[n]` (declared app.js:44, "last pulled from board") survives, as do `boardBootChars[n]` (app.js:45), `boardFlashMode[n]` (app.js:47) and `postFlashGeneralSnapshot[n]` (app.js:50). When the same slot number is later re-rendered, `addBoardSection` creates a blank `WCBParser.createDefaultBoardConfig()` (app.js:616-619) but the stale baseline is still there.

The push path keys its entire behaviour off that map:

```js
7417:    const fullPush  = !boardBaselines[n];
7418:    const cmdString = WCBParser.buildCommandString(config, boardBaselines[n] ?? null, fullPush);
```

So instead of the full push a genuinely-unknown board should get, the Wizard emits a delta computed against the *previous* occupant of that slot number: settings that happen to match the stale baseline are omitted entirely (parser.js:1284-1288, 1493 and the `*Changed` guards throughout `buildCommandString`), and entries present only in the stale baseline generate destructive clears (`MAESTRO,CLEAR`, `WLED` targeted clears, `HCR,REMOTE,OFF`, etc.).

The eviction path itself is user-reachable from `onWCBQuantityChange` → `renderBoards(qty)` (app.js:1125) and from `syncGeneralFromConfig`/`boardPull` (app.js:6867, 8383).

**Failure scenario.** WCB Quantity = 4. User connects board 3 over USB; the auto-pull sets `boardBaselines[3]` to that board's real config (Maestro 5 on S1 @115200, HCR on S2, labels set). User disconnects board 3, then drops WCB Quantity to 2 — `reconcileBoardGrid` removes sections 3 and 4 and deletes `boardConfigs[3]`, leaving `boardBaselines[3]` intact. User sets WCB Quantity back to 4; slot 3 is rebuilt with a blank default config. User now plugs in a *different, unconfigured* board, and pushes before the auto-pull returns. `fullPush` is false because `boardBaselines[3]` exists, so mesh channel / MAC / command chars / ETM values that coincide with the old board's baseline are never sent, while `MAESTRO,CLEAR` and `HCR,CLEAR` derived from the old board's baseline are. The new board ends up partially configured and the Wizard reports success.

**Suggested fix.** Delete the whole slot's state in the eviction branch: add `delete boardBaselines[n]; delete boardBootChars[n]; delete boardFlashMode[n]; delete postFlashGeneralSnapshot[n]; delete remoteRelayForBoard[n];` alongside the existing two deletes, so a re-created slot always starts as a full push.

### F-204 · S3 · `Wizard/app.js:1116`

**Changing WCB Quantity does not mark general settings dirty, unlike every other general handler**

*Category:* `logic`

`onWCBQuantityChange()` (app.js:1116, bound to `g-wcbq`) sets `systemConfig.general.wcbQuantity = qty` (app.js:1124), calls `renderBoards(qty)` and repopulates the per-board WCB-number dropdowns, but never calls `_notifyGeneralChanged()`. Every other general-settings handler does: `onGeneralPasswordChange`, `onGeneralMacChange`, `onMeshChannelChange`, `onGeneralCmdCharChange`, `onETMToggle`, `onETMChecksumToggle`, `onGeneralETMChange` — I checked all of them.

`_notifyGeneralChanged` (app.js:1069) is the only setter of `generalSettingsDirty`, which `updatePushAllButton` (app.js:3158) reads to turn Push All amber, and it is the only source of the "Changes pending — push to all boards" toast.

The quantity is a genuinely pushed per-board setting, so this is not cosmetic-only: the push path reads it from the DOM at app.js:7415 (`config.wcbQuantity = parseInt(document.getElementById('g-wcbq').value) || 1`) and parser.js:1266-1267 emits `WCBQ,<n>` whenever it differs from the baseline.

**Failure scenario.** User changes WCB Quantity from 3 to 5 to add two boards to the mesh. New board sections render, so the change looks applied, but no toast fires and Push All stays green. The user does not push, and the three existing boards keep `WCBQ,3` in NVS — so they never expect boards 4 and 5 and the mesh sizing stays wrong until something else marks a board dirty.

**Suggested fix.** Call `_notifyGeneralChanged();` at the end of `onWCBQuantityChange` (after the dropdown-repopulation loop, app.js:1134), matching onMeshChannelChange and the other general handlers. Keep it out of the `_more` early-return branch at app.js:1118-1122, which has not yet produced a real value.

### F-205 · S3 · `Wizard/app.js:1131`

**Lowering WCB Quantity silently re-selects each board's WCB-number dropdown without updating boardConfigs[n].wcbNumber**

*Category:* `logic`

```js
1127:   for (let n = 1; n <= qty; n++) {
1128:     const numSel = document.getElementById(`b${n}-wcb-number`);
1129:     if (numSel) {
1130:       const cur = parseInt(numSel.value) || n;
1131:       populateWCBDropdown(numSel, qty, Math.min(cur, qty), true);
1132:     }
1133:   }
```

`populateWCBDropdown` rebuilds the option list 1..qty and marks `Math.min(cur, qty)` selected (app.js:1094-1109). When the board's current number exceeds the new quantity the displayed selection is silently changed, but nothing writes that back: `boardConfigs[n].wcbNumber` is only ever set by `onWCBNumberChange` (app.js:1877-1885), which is an `onchange` handler (index.html:521) and does not fire for a programmatic re-populate.

The result is a persistent three-way disagreement: the dropdown shows the clamped number, `boardConfigs[n].wcbNumber` still holds the original, and the card header — rendered by `updateBoardAliasUI(n)` from the config — still reads the original ("WCB {wcbNumber} (Alias)", app.js:1884). Since `buildCommandString` uses `config.wcbNumber`, a push writes the number the dropdown is *not* showing.

The loop also stops at `qty`, so any slot above the new quantity that survived reconcile (a live USB board or a WDP-discovered peer — both kept by `desiredBoardNumbers`, app.js:429-437) keeps a dropdown built for the old range.

**Failure scenario.** User loads an 8-board config; slot 1 holds a board whose `wcbNumber` is 8. They change WCB Quantity from 8 to 4. Slot 1's WCB-number dropdown now displays "4" while the card header still reads "WCB 8" and `boardConfigs[1].wcbNumber` is still 8. The user connects that board and pushes: `?WCB,8` (from the config) is sent, contradicting the "4" the dropdown showed them. If they later touch the dropdown at all, `onWCBNumberChange` commits whatever is displayed, renumbering the board without them intending it.

**Suggested fix.** When the clamp actually changes the selection, commit it: after `populateWCBDropdown(...)` call `onWCBNumberChange(n)` (or set `boardConfigs[n].wcbNumber = Math.min(cur, qty)` and `updateBoardAliasUI(n)`) so the dropdown, header and config never disagree — and iterate `desiredBoardNumbers()` instead of `1..qty` so surviving out-of-range slots are refreshed too.

### F-206 · S3 · `Wizard/app.js:1171`

**validateMacOctet's single-digit pad is unreachable, and a one-character MAC octet is never normalized so it can never match the board**

*Category:* `round-trip-loss`

```js
1162: function validateMacOctet(input) {
1163:   const val = input.value.trim().toUpperCase();
1164:   const valid = /^[0-9A-F]{0,2}$/.test(val);
1165:   if (!valid || (val.length > 0 && val.length < 2)) {
1166:     input.style.borderColor = 'var(--red)';
1167:     showToast(`Invalid hex value "${val}" — must be 00–FF`, 'error');
1168:   } else {
1169:     input.style.borderColor = '';
1170:     // Pad single digit to two chars
1171:     if (val.length === 1) input.value = '0' + val;
1172:   }
1173: }
```

The `else` branch is entered only when `valid` is true **and** `(val.length === 0 || val.length >= 2)`. Combined with `/^[0-9A-F]{0,2}$/`, the surviving lengths are exactly 0 and 2 — `val.length === 1` at line 1171 can never be true. The pad is dead code, and a single hex digit always takes the error branch: red border plus a toast, but the value is left as-is in both the DOM and the model.

That matters because the field is wired `oninput="onGeneralMacChange()" onblur="validateMacOctet(this)"` (index.html:86, 92), so `systemConfig.general.macOctet2` and every `boardConfigs[n].macOctet2` already hold the unpadded `"5"` (app.js:1144-1152), and `updateGeneralBaseline()` copies it into `generalBaseline.fields`. `boardPushConfig` reads the same unpadded DOM value (app.js:7409-7410) and emits `?MAC,2,5`. The firmware parses with `strtoul(hexVal.c_str(), NULL, 16)` (WCB.ino:4734) so the board stores 0x05 correctly — but it echoes the octet zero-padded: `sprintf(hexBuffer, "%02X", umac_oct2); emit("MAC,2," + String(hexBuffer), true);` (WCB.ino:2783-2784). The parser stores that string verbatim (parser.js:548-549), so `"05" !== "5"` forever.

**Failure scenario.** In General Settings the user types a single character in MAC Octet 2 (e.g. `5`, meaning 0x05) and tabs away. The field turns red and an "Invalid hex value" toast appears, but nothing corrects it — `systemConfig.general.macOctet2` and `generalBaseline.fields.macOctet2` are `"5"`. The user pushes; the board correctly stores 0x05 and reports `?MAC,2,05`. On the next Pull, `getGeneralMismatches` (app.js:1440-1444) sees `"5"` vs `"05"` and raises the General Settings conflict modal claiming MAC Octet 2 differs across boards, and `compareConfigs` (parser.js:1609) keeps the board flagged unsaved. Re-pushing sends `?MAC,2,5` again and the mismatch returns on every subsequent pull.

**Suggested fix.** Normalize instead of just complaining: for a valid 1-character value write `input.value = '0' + val` and then re-run `onGeneralMacChange()` so the model and baseline pick up the padded form; reserve the red-border/toast branch for genuinely invalid (non-hex or >2 char) input. Alternatively normalize at the read site in `onGeneralMacChange` with `.padStart(2,'0')`.

### F-207 · S3 · `Wizard/app.js:1702`

**boardOtaSerial never drains `_suppressedEtmEdges`, so ETM online/offline edges deferred during a USB OTA are lost permanently**

*Category:* `async`

`installEtmListener` defers — rather than drops — ETM edges while any OTA is in flight, and the comment states the contract explicitly (Wizard/app.js:7704-7710):

```js
// DEFER the edge (record it) rather than drop it — a board that reboots mid-OTA
// still gets reconciled when the transfer finishes (ETM edges are one-shot;
// nothing replays them).
if (_otaInProgress.size > 0) {
  if (remoteRelayForBoard[bn] === relayN) _suppressedEtmEdges.add(bn);
  return;
}
```
(identical block for the offline edge at :7729-7732)

The replay half of that contract exists in only one of the two OTA drivers. `boardOtaRelay`'s `finally` (Wizard/app.js:1854-1866) drains the set. `boardOtaSerial`'s `finally` does not:

```js
} finally {
  _otaInProgress.delete(n);   // release the port for the mesh-discovery tick again
  setFlashUI(n, false);
  if (btn) { btn.disabled = false; btn.textContent = '⬆ OTA'; }
}
```

But `boardOtaSerial` *does* populate `_otaInProgress` (`_otaInProgress.add(n)` at :1548), so it suppresses edges just as effectively. Any edge recorded during a USB OTA sits in `_suppressedEtmEdges` unreplayed until some unrelated *relay* OTA happens to finish — at which point it is replayed with arbitrarily stale information.

**Failure scenario.** Two boards on USB: WCB 1 (acting as relay for remote WCB 2 and WCB 3) and WCB 4. Start a USB OTA on WCB 4 (~1-3 minutes). Mid-transfer, remote WCB 3 loses power. The relay prints "[ETM] WCB3 went OFFLINE"; the listener records `3` in `_suppressedEtmEdges` and returns. The OTA finishes; nothing replays the edge. WCB 3's dot stays green, no "ETM timeout — verifying reachability" toast fires, and no re-pull is attempted — the Wizard shows an offline board as connected indefinitely.

**Suggested fix.** Factor the drain block from `boardOtaRelay`'s `finally` (:1854-1866) into a helper such as `_replaySuppressedEtmEdges(skipA, skipB)` and call it from `boardOtaSerial`'s `finally` too (with no skips, since the serial path's target is not a relayed board).

### F-208 · S3 · `Wizard/app.js:1882`

**onWCBNumberChange silently ignores a selection above systemConfig.general.wcbQuantity even though the dropdown offers it**

*Category:* `logic`

```js
function onWCBNumberChange(n) {
  const sel = document.getElementById(`b${n}-wcb-number`);
  if (!sel) return;
  const val = parseInt(sel.value);
  const qty = systemConfig?.general?.wcbQuantity || WCB_MAX;
  if (boardConfigs[n] && val >= 1 && val <= qty) boardConfigs[n].wcbNumber = val;
  updateBoardAliasUI(n);   // header reads "WCB {wcbNumber} (Alias)"
  onBoardFieldChange(n);
}
```

The accept range is `1..wcbQuantity`, but the dropdown is populated with a strictly wider range. `populateUIFromConfig` (Wizard/app.js:2927-2933) builds it as:

```js
const qty = Math.max(
  systemConfig?.general?.wcbQuantity || 0,
  parseInt(document.getElementById('g-wcbq')?.value) || 0,
  config.wcbQuantity || 0,
  selectedWcb   // always include the board's self-reported number
);
populateWCBDropdown(wcbNumSel, qty, selectedWcb, true);
```

So whenever a board self-reports a number above the mesh quantity, options in `(wcbQuantity, selectedWcb]` are offered but silently rejected by the handler. There is no toast, no error, and no revert of `sel.value` — the `<select>` keeps showing the rejected number while `boardConfigs[n].wcbNumber` and the side-nav label (`updateBoardAliasUI`, :1926) keep the old one. `onBoardFieldChange(n)` still fires, so the board is marked unsaved with nothing actually changed.

**Failure scenario.** A board pulled from a previous droid reports `?WCB,7` while the current mesh is `?WCBQ,4`. Its WCB Number dropdown is populated 1-7. The user picks 6 to re-number it. Nothing happens: the dropdown displays 6, but `6 > 4` so the assignment is skipped; the side-nav button still reads "WCB 7", and Push Config emits no `WCB,` command (or re-emits `WCB,7`). The user believes the board was renumbered.

**Suggested fix.** Use the same ceiling the dropdown was built with rather than `wcbQuantity` alone — e.g. `const qty = Math.max(systemConfig?.general?.wcbQuantity || 0, parseInt(document.getElementById('g-wcbq')?.value) || 0, boardConfigs[n]?.wcbQuantity || 0, WCB_MAX)` capped at `WCB_MAX` — or, if the restriction is intentional, revert `sel.value` to `boardConfigs[n].wcbNumber` and show a toast explaining that WCB Quantity must be raised first.

### F-209 · S3 · `Wizard/app.js:2664`

**addWLEDRow derives the next free ID from config.wleds, which excludes port-less rows — adding two rows before assigning ports yields duplicate WLED IDs**

*Category:* `logic`

```js
function addWLEDRow(n) {
  // Default to the lowest free ID 1-9. WLED IDs are network-unique, so avoid ids
  // already used by LOCAL rows AND by remote (auto-learned) WLEDs on this board.
  const cfg  = boardConfigs[n];
  const used = new Set([
    ...(cfg?.wleds ?? []).map(w => w.id),
    ...(cfg?.wledRemotes ?? []).map(w => w.id),
  ]);
  let id = 1; while (used.has(id) && id < 9) id++;
```

The comment says "already used by LOCAL rows", but it reads `cfg.wleds`, not the rows. `cfg.wleds` is rebuilt by `syncWLEDsToConfig` (Wizard/app.js:2798-2845), which only pushes a row into the array when it has a valid port:

```js
if (id >= 1 && id <= 9 && port >= 1 && port <= 5) { config.wleds.push({ id, port, baud }); … }
```

A freshly-added row is created with `port: null` (:2669) and its dropdown starts on the `— Select —` placeholder (:2693), so it contributes nothing to `cfg.wleds`. The very next `addWLEDRow` therefore sees an empty `used` set and picks id 1 again. Compare `refreshWLEDPortDropdown` (:2712-2718), which correctly derives its exclusion set by walking the live `<tr>` elements.

WLED IDs are one-slot-per-id in the firmware (`findWLEDSlotByID`, Code/WCB/WCB_WLED.cpp:41-45), unlike Maestros where duplicates are legal. Two `?WLED,1:…` lines therefore collapse to one slot, and the second `wledReserveLocalPort` releases the first port (Code/WCB/WCB_WLED.cpp:335-344), restoring its broadcast flags — while the Wizard's `config.serialPorts[]` still shows that port claimed by WLED with broadcast off.

**Failure scenario.** On a board with no WLEDs, click "+ Add WLED" twice in a row, then set the first row's port to Serial 1 and the second row's port to Serial 2. Both rows still show ID 1 (the ID dropdown silently defaulted to 1 both times). Push Config → `?WLED,1:W1S1:115200` then `?WLED,1:W1S2:115200`. The firmware ends with a single WLED 1 on S2 and S1 released; the Wizard's UI and port-claim state still show two WLEDs and S1 reserved. `;L1` reaches only the second strip.

**Suggested fix.** Build `used` from the DOM rows the way `refreshWLEDPortDropdown` does, e.g. collect `parseInt(row.querySelector('[id$="-id"]')?.value)` over `#b{n}-wled-tbody tr` (plus the read-only remote rows' ids) before falling back to `cfg.wleds`/`cfg.wledRemotes`. Optionally also flag duplicate ids in `syncWLEDsToConfig` so the condition is visible rather than silent.

### F-210 · S3 · `Wizard/app.js:3675`

**_removeBidirRows mutates other boards' configs without flagging them unsaved**

*Category:* `logic`

`_removeBidirRows()` deletes the auto-generated reverse rows from every other board's mapping container and re-derives that board's config from the remaining DOM:
```js
toRemove.forEach(el => el.remove());
const headers = document.getElementById(`b${bn}-mapping-headers`);
if (headers && container.children.length === 0) headers.style.display = 'none';
syncMappingsToConfig(bn);
```
(app.js:3672-3675). `syncMappingsToConfig(bn)` rewrites `boardConfigs[bn].mappings` (app.js:3593), so board `bn`'s in-memory config now differs from its baseline — but there is no `updateBoardStatusBadge(bn, 'unsaved')` call.

The add direction is asymmetric and does flag it: `appendMappingRow(wcb, reverseMapping, { bidirFrom: rowId }); syncMappingsToConfig(wcb); updateBoardStatusBadge(wcb, 'unsaved');` (app.js:3724-3726). So turning bidir ON tells the user the other board needs a push, and turning it OFF does not — no ⚠ Unsaved badge, and `updatePushAllButton()` (app.js:3154-3161, which scans `[id$="-unsaved-badge"]:not([style*="none"])`) leaves the Push All button green.

`_removeBidirRows` is also called from `removeMappingRow` (app.js:3630) and from `applyBidirMapping`'s own "clear previous auto-rows first" step (app.js:3683), so this fires on the two most common ways a user undoes a bidir mapping.

**Failure scenario.** User has a bidir mapping WCB1 S1 ↔ WCB2 S3. They untick the bidir toggle on WCB1. WCB1 is correctly marked unsaved; WCB2's reverse row vanishes from its card and from `boardConfigs[2].mappings`, but WCB2 shows no ⚠ Unsaved badge and Push All stays green. The user pushes WCB1 only and walks away believing the pair is undone — WCB2 is still mirroring traffic back on S3, and nothing in the UI says so.

**Suggested fix.** Add `updateBoardStatusBadge(bn, 'unsaved');` after `syncMappingsToConfig(bn)` in the loop, mirroring app.js:3726 — and note that clearing the row alone will not remove the mapping from the board's NVS, since `generatePushCommands` emits no `MAP,...,CLEAR` for removed mappings (Wizard/parser.js:1509-1521); an explicit `?MAP,SERIAL,CLEAR,S<port>` to that board is what actually undoes it.

### F-211 · S3 · `Wizard/app.js:4218`

**Sequence key is never validated for the comma the firmware splits on**

*Category:* `protocol-contract`

The sequence key input is created with only a length cap — `<input class="seq-key-input" type="text" ... maxlength="15">` (app.js:4218-4221) — and `updateSequence()` uses it raw: `const key = keyInput?.value?.trim();` … `const cmd = `${funcChar}SEQ,SAVE,${key},${value}`;` (app.js:4405, :4448).

The firmware splits key from value on the **first comma**:
```cpp
int commaIndex = message.indexOf(',');
String key   = message.substring(0, commaIndex);
String value = message.substring(commaIndex + 1);
```
(Code/WCB/WCB_Storage.cpp:626-631, reached via `saveStoredCommandsToPreferences(seqArgs)` at WCB.ino:4993). A key containing a comma is therefore truncated at that comma and the remainder is prepended to the stored value.

The Wizard already knows this hazard class and guards it everywhere else: variables are checked against `const VAR_NAME_RE = /^[A-Za-z0-9_]{1,15}$/` (app.js:4506), and the alias field is sanitised with the explicit note "Mirror the firmware's saveWCBAlias() sanitization: any character that would corrupt the chained backup ('^', ',', ';', '?')" (app.js:1889-1892). Sequence keys get neither. `,`, `^` and the funcChar are all accepted.

**Failure scenario.** User names a sequence `dome,open` and clicks SAVE/UPDATE. The Wizard sends `?SEQ,SAVE,dome,open,;M11^;t500^;M12` and toasts success, and records `{key:'dome,open'}` in `boardConfigs`/`boardBaselines`. The board stores key `dome` with value `open,;M11^;t500^;M12` — a value whose first token `open` is not a command. Recalling `;SEQdome,open` does nothing; the Wizard's key list and the board's disagree; the next pull replaces the row with a key the user never typed.

**Suggested fix.** Validate the key on save (and ideally on input) with a regex mirroring the firmware's storage constraints — e.g. `/^[A-Za-z0-9_-]{1,15}$/` — and reject with a toast naming the offending character, the same way `VAR_NAME_RE` is used for variables at app.js:4506.

### F-212 · S3 · `Wizard/app.js:4389`

**playSequence has no error handling — a failed TEST is invisible and raises an unhandled rejection**

*Category:* `error-handling`

`playSequence()` is bound straight to an inline handler — `onclick="playSequence(${n},'${rowId}')"` (app.js:4234) — and has no `try`/`catch` anywhere in its body (app.js:4360-4398), unlike every sibling in the file (`updateSequence` app.js:4422/4479, `saveMappingRow` app.js:3846/3979, `removeMappingRow` app.js:3643/3660, `removeSequenceRow` app.js:4274/4292).

Two distinct problems:
1. Relay path: `await sendMgmtReliable(relayConn, mgmtCmd, relayN);` (app.js:4389). `sendMgmtReliable` awaits `relayConn.send(...)` with no internal catch (app.js:7682-7688), so a WebSerial write failure (port revoked, device unplugged between the `isConnected()` check and the write) rejects the returned promise. Nothing awaits `playSequence`, so this surfaces only as an `Uncaught (in promise)` in the console — and the success toast on the next line never runs, so the user gets **no feedback at all**.
2. Direct path: `conn.send(cmd + '\r');` (app.js:4394) is not awaited even though the function is `async` and the relay branch awaits — so a rejection here is likewise unhandled, and the `showToast(`Sent: ${cmd} ...`)` on the following line fires before the bytes are known to have gone out.

**Failure scenario.** User is testing a sequence on a relayed board, pulls the relay's USB cable, then clicks TEST on a sequence row. `isConnected()` still reports true (the disconnect event has not been processed), the write rejects, and the button appears to do nothing — no toast, no terminal line, no error. The user clicks TEST several more times assuming the board is ignoring the sequence.

**Suggested fix.** Wrap the body in `try { ... } catch (e) { showToast(`Test failed: ${e.message}`, 'error'); termLog(relayN ?? n, `SEQ test error: ${e.message}`, 'err'); }` matching `updateSequence` (app.js:4479-4482), and `await conn.send(cmd + '\r')` on the direct path.

### F-213 · S3 · `Wizard/app.js:4440`

**Renaming a sequence deletes the old key from config AND baseline even when the board is unreachable**

*Category:* `error-handling`

In `updateSequence()`'s rename branch the `?SEQ,CLEAR,<originalKey>` send is guarded by a connection check, but the in-memory bookkeeping that follows is not:
```js
if (relayN) {
  const relayConn = boardConnections[relayN];
  if (relayConn?.isConnected()) { ... await sendMgmtReliable(...); }
} else {
  const conn = boardConnections[n];
  if (conn?.isConnected()) { await conn.send(clearCmd + '\r'); termLog(n, clearCmd, 'in'); }
}
// Remove old key from in-memory stores
for (const store of [boardConfigs[n], boardBaselines[n]]) {
  if (!store) continue;
  store.sequences = store.sequences.filter(s => s.key !== originalKey);
}
```
(app.js:4425-4442). When the connection is down the CLEAR is silently skipped, yet `originalKey` is stripped from **both** `boardConfigs[n]` and `boardBaselines[n]`. Removing it from the baseline is what makes it unrecoverable: the push diff emits a clear only for keys present in the baseline and absent from the config — `for (const [key] of baseSeqMap) { if (!curSeqMap.has(key)) add(`SEQ,CLEAR,${key}`); }` (Wizard/parser.js:1534-1537). With the key gone from the baseline, no push will ever clear it.

Execution then falls through to the connection guard a few lines later (`if (!relayConn?.isConnected()) { showToast(...); return; }`, app.js:4453) so the new key is not saved either — the rename leaves nothing behind but a stale sequence on the board.

**Failure scenario.** User renames sequence `wave` to `wave_v2` on a board whose USB cable was just unplugged (or whose relay dropped). The Wizard drops `wave` from its config and baseline, shows "Board not connected", and saves nothing. `wave` still lives in the board's NVS but is now invisible to the Wizard, and neither Push Config nor Push All will ever clear it — it keeps firing on any mesh-wide `;SEQwave` until someone runs `?SEQ,CLEAR,wave` by hand.

**Suggested fix.** Move the connectivity check to the top of the rename branch and bail out before any store mutation, or only strip `originalKey` from the stores once the CLEAR was actually handed to a connection — i.e. capture the boolean returned by the send branch (the pattern `sendVariableCommand` already uses at app.js:4515-4530) and gate the `filter` on it.

### F-214 · S3 · `Wizard/app.js:4800`

**collectVarList reports a failed send as an empty variable list, wiping the live rows and telling the user the board has 0 variables**

*Category:* `error-handling`

```js
4786:    const finish = () => {
4787:      if (done) return; done = true;
4788:      clearTimeout(timer);
4789:      conn._dataCallbacks = conn._dataCallbacks.filter(cb => cb !== onLine);
4790:      resolve(lines);            // always RESOLVES — never rejects
4791:    };
...
4800:    Promise.resolve(sendPromise).catch(finish);
```

`finish` is wired directly as the send's rejection handler, so a transport failure resolves the promise with the empty `lines` array instead of rejecting. `sendPromise` is `conn.send(...)` for the direct path (which throws `'Not connected'` at Wizard/app.js:5211, or any WritableStream write error) or `sendVariableCommand()` → `sendMgmtReliable` → `relayConn.send()` for the relay path (Wizard/app.js:4520).

`refreshVariablesFromBoard` therefore treats a send failure as a successful empty answer:

```js
4866:    const lines  = await collectVarList(n);
4867:    const parsed = parseVarList(lines);
4868:    renderLiveVariables(n, parsed);          // removes every .var-row-live row
4871:    showToast(`WCB ${n}: ${parsed.length} variable(s) on board — ${p} persistent, ${t} temporary`, 'info');
```

`renderLiveVariables` starts by deleting all live rows (`tbody.querySelectorAll('.var-row-live').forEach(r => r.remove())`, Wizard/app.js:4831), so the previously-shown temporary variables vanish and the user is told, positively, that the board has none. The `catch` at 4872 that would have shown "Variable refresh failed" is never reached.

**Failure scenario.** User is watching runtime variables on a relay-managed board and unplugs the relay (or the write fails mid-`?VAR,LIST`). They click ↻ Refresh: every temporary row disappears and a neutral info toast says "WCB 4: 0 variable(s) on board — 0 persistent, 0 temporary". The user concludes the firmware lost their runtime variables, when in fact the command was never delivered.

**Suggested fix.** Reject rather than resolve on a send failure: `Promise.resolve(sendPromise).catch(err => { if (!done) { done = true; clearTimeout(timer); conn._dataCallbacks = conn._dataCallbacks.filter(cb => cb !== onLine); reject(err); } });`. Optionally also distinguish a timeout with zero lines from a genuine "(none)" reply so `refreshVariablesFromBoard` can warn instead of wiping the panel.

### F-215 · S3 · `Wizard/app.js:5182`

**BoardConnection.disconnect() omits the explicit reader.releaseLock() its siblings require, and nulls this.port only on a successful close**

*Category:* `race`

```js
async disconnect() {
  this._connected = false;
  try {
    if (this.reader) {
      await this.reader.cancel();
      this.reader = null;          // <- no releaseLock(), and this.reader is cleared
    }
  } catch (_) {}
  try {
    if (this.port) {
      await this.port.close();
      this.port = null;            // <- only reached when close() succeeds
    }
  } catch (_) {}
}
```
(app.js:5182-5196).

Both sibling teardown methods do it differently and say why. `closeForReconnect` (app.js:4979-4988): "Cancel unblocks any pending reader.read(), then explicitly release the lock before closing the port. **Without the explicit releaseLock() there is a microtask gap** between cancel() resolving and _startReading's finally block calling releaseLock(), **during which port.close() (and a subsequent flash's getReader()) can fail with 'ReadableStream is locked to a reader'**." `becomeShared` does the same pair (app.js:5482-5486). That comment records a real past failure.

`disconnect()` reproduces the documented race AND makes the fallback worse: setting `this.reader = null` at app.js:5187 means `_startReading`'s own `finally` — `try { this.reader?.releaseLock(); } catch (_) {}` (app.js:5528) — becomes a no-op if it has not already run. And because `this.port = null` sits *after* the awaited `close()` inside the try, a rejected close leaves `this.port` pointing at a still-open, still-locked port with the failure swallowed.

**Failure scenario.** The user clicks Disconnect on a board (`boardDisconnect` -> `_c.disconnect()`, app.js:6203). The read loop happens not to have resumed before `disconnect()` nulls `this.reader`, so nothing releases the stream lock; `port.close()` rejects with "ReadableStream is locked to a reader", the catch at app.js:5195 swallows it, and `this.port = null` is skipped. The UI shows Not Connected but the OS handle is still open and locked. The user clicks Connect and picks the same port: `conn.connect()` calls `port.open()`, gets "already open", falls into the recovery branch `this.port.readable && !this.port.readable.locked` (app.js:4954-4955) — readable IS locked — so it rethrows and the toast reads "Connection failed: Failed to open serial port". The board cannot be reconnected until the page is reloaded.

**Suggested fix.** Match the sibling methods: `if (this.reader) { try { await this.reader.cancel(); } catch (_) {} try { this.reader.releaseLock(); } catch (_) {} this.reader = null; }`, and move the port null-out out of the success path: `if (this.port) { try { await this.port.close(); } catch (_) {} this.port = null; }`.

### F-216 · S3 · `Wizard/app.js:5192`

**disconnect() can leave the SerialPort open and referenced, orphaning the OS handle and breaking a later flash**

*Category:* `error-handling`

```js
5182:  async disconnect() {
5183:    this._connected = false;
5184:    try {
5185:      if (this.reader) {
5186:        await this.reader.cancel();
5187:        this.reader = null;         // clears the handle the read loop's finally uses
5188:      }
5189:    } catch (_) {}
5190:    try {
5191:      if (this.port) {
5192:        await this.port.close();
5193:        this.port = null;           // only reached when close() SUCCEEDS
5194:      }
5195:    } catch (_) {}
5196:  }
```

Two problems, both of which the codebase already documents elsewhere:

1. No `releaseLock()`. The sibling `closeForReconnect()` spells out why that matters: "Without the explicit releaseLock() there is a microtask gap between cancel() resolving and _startReading's finally block calling releaseLock(), during which port.close() (and a subsequent flash's getReader()) can fail with 'ReadableStream is locked to a reader'" (Wizard/app.js:4982-4986). Worse, line 5187 nulls `this.reader`, so if `disconnect()` resumes first the read loop's `this.reader?.releaseLock()` (Wizard/app.js:5528) becomes a no-op and the lock is never released. The hub does it correctly at Wizard/serial-hub.js:428-436.

2. `this.port = null` is *inside* the try after the await, so any `close()` rejection leaves `this.port` non-null and the port OPEN — while `boardDisconnect()` unconditionally does `delete boardConnections[n]` (Wizard/app.js:6210) and reports success. That close() genuinely fails here is documented in `reconnect()`: "On Windows, esptool-js transport.disconnect() sometimes fails silently, leaving the COM handle stuck 'open'" — which is why that path retries close 4× (Wizard/app.js:5004-5008).

The orphaned open port then breaks flashing: `flashFirmware` hands the raw port to `new Transport(port, false)` (Wizard/flasher.js:351), and esptool-js opens it itself — which throws on an already-open port.

**Failure scenario.** On Windows, user finishes a session, clicks Disconnect on WCB 1 (port.close() rejects, swallowed at line 5195), then selects Flash mode and clicks Flash for that board. esptool-js's Transport.connect() fails to open the still-open COM port and the user gets "Bootloader connection failed: Failed to open serial port" with no indication that the Wizard itself is holding it — and no in-app way to release it short of reloading the tab.

**Suggested fix.** Mirror `closeForReconnect()`: `if (this.reader) { try { await this.reader.cancel(); } catch(_){} try { this.reader.releaseLock(); } catch(_){} this.reader = null; }` then `try { if (this.port) await this.port.close(); } catch(_){} finally { this.port = null; }` so the reference is dropped on every path (or, better, keep the reference and report the close failure so the caller can retry).

### F-217 · S3 · `Wizard/app.js:5515`

**Direct read loop decodes each USB chunk with a fresh non-streaming TextDecoder, corrupting multi-byte UTF-8 split across chunks; _readBuffer is also unbounded**

*Category:* `logic`

```js
5512:          const { value, done } = await this.reader.read();
5515:          this._readBuffer += new TextDecoder().decode(value);
5516:          let nl;
5517:          while ((nl = this._readBuffer.indexOf('\n')) !== -1) {
```

A new decoder per chunk with the default `{stream:false}` means an incomplete trailing multi-byte sequence is flushed as U+FFFD, and the leading continuation bytes of the next chunk are flushed as U+FFFD too. The sibling shared-hub path, doing the identical job on the identical byte stream, gets this right:

```js
5420:    this._sharedDecoder = new TextDecoder();
5422:      this._sharedBuf += this._sharedDecoder.decode(u8, { stream: true });
```

The firmware emits plenty of multi-byte UTF-8 on Serial — e.g. `Serial.println("⚠️ statusLED is nullptr, skipping LED operation")`, `Serial.println("✓ Command checksum VERIFIED - Configuration is intact")`, `Serial.printf("[MGMT] Duplicate session %04X — discarding\n", …)` (Code/WCB/WCB.ino) — and USB CDC delivers 64-byte packets, so boundaries land mid-character regularly.

Separately, `this._readBuffer` has no size cap: any stretch of board output without a `\n` accumulates without bound for the life of the connection (the shared path's `_sharedBuf` has the same gap, and `leaveShared()` at 5444 never resets it).

**Failure scenario.** During a config pull or an ETM characterization the board prints `[MGMT] Duplicate session 3F2A — discarding` and the USB packet boundary falls inside the em-dash. The terminal shows `[MGMT] Duplicate session 3F2A ��� discarding`. The same corruption applies to every ✓/✗/⚠ status line the firmware prints, making saved terminal logs and screenshots misleading.

**Suggested fix.** Give `BoardConnection` one persistent decoder (allocate `this._decoder = new TextDecoder()` in the constructor, reset it alongside `this._readBuffer = ''` in `connect`/`reconnect`) and decode with `this._decoder.decode(value, { stream: true })`. Add a guard after the append, e.g. `if (this._readBuffer.length > 1<<20) this._readBuffer = this._readBuffer.slice(-65536);` and do the same for `_sharedBuf`.

### F-218 · S3 · `Wizard/app.js:5643`

**establishConnection's auto-share block has a finally but no catch — a hub failure aborts the connect instead of falling back to direct USB**

*Category:* `error-handling`

```js
5641:  if (allowShare && !_hasSharedPort() && !_autoShareClaimed && !(await _anotherTabLeadsShare())) {
5642:    _autoShareClaimed = true;
5643:    try {
5644:      const conn = await sharedConnect(n, port);
...
5658:      try { conn.leaveShared(); } catch (_) {}
5663:      try { if (localStorage.getItem('wcbSharedSlot') === String(n)) localStorage.removeItem('wcbSharedSlot'); } catch (_) {}
5664:    } finally {
5665:      _autoShareClaimed = false;
5666:    }
5667:  }
5668:  const conn = new BoardConnection(n);   // the direct-connect fallback — skipped on any throw above
```

The surrounding comments promise a fallback ("tear the share down and fall through to a direct connect so the caller surfaces the real port error, not a phantom connect", lines 5648-5649), but only the *non-throwing* failure is handled. Any exception out of `sharedConnect()` propagates past line 5668 and out of `establishConnection`.

`sharedConnect()` calls `getSharedHub()` at line 5763, which throws by design:

```js
5583: function getSharedHub() {
5584:   if (!_sharedHub) {
5585:     if (typeof WcbSerialHub === 'undefined' || !WcbSerialHub.supported)
5586:       throw new Error('Port sharing needs a Chromium browser (WebSerial + Web Locks + BroadcastChannel)');
```

`WcbSerialHub` comes from a plain `<script src="serial-hub.js">` (Wizard/index.html:1052) with no integrity/failure handling, and `connectShared` → `hub.join()` throws the same way (Wizard/serial-hub.js:129). So a single missing/blocked/partially-cached script turns every first board connect into a misleading browser-compatibility error, with no direct-USB path at all.

**Failure scenario.** serial-hub.js fails to load (CDN/Pages hiccup, an extension blocking it, or a truncated cache entry). The user opens the Wizard on Chrome, clicks Connect → Select port manually, picks the right COM port, and gets "Connect failed: Port sharing needs a Chromium browser (WebSerial + Web Locks + BroadcastChannel)". No board can be connected at all, even though direct USB — which needs none of Web Locks or BroadcastChannel — would have worked.

**Suggested fix.** Change `try { … } finally { … }` at 5643-5666 into `try { … } catch (shareErr) { /* log and fall through to direct */ try { delete boardConnections[n]; } catch(_){} } finally { _autoShareClaimed = false; }` so control always reaches the direct connect at line 5668.

### F-219 · S3 · `Wizard/app.js:6741`

**waitForBoardReady fires both probes every tick, so a booted board emits two full backups and the second streams through the config push that follows**

*Category:* `async`

```js
if (connected && hasReader) {
  const pullFuncChar = ...;
  try {
    await conn.send('WCB_WEBTOOL_CONFIG_PULL\r');     // app.js:6741
    await sleep(200);
    if (!done) await conn.send(`${pullFuncChar}backup\r`);   // app.js:6743
  }
  ...
}
```

Unlike boardPull — which only falls back to the funcChar form when the bootstrap produced no 'End of Backup' (app.js:6801-6805) — this sends BOTH unconditionally, 200 ms apart. Both commands reach the same handler: `WCB.ino:4374-4376` maps `WCB_WEBTOOL_CONFIG_PULL` to `printBackupConfig()`, and `?backup` reaches the same dump. A full dump takes well over 200 ms at 115200, so `done` is still false at the `if (!done)` check and the second request is essentially always sent.

The listener resolves on the FIRST 'End of Backup' (app.js:6702-6709), i.e. the end of dump #1 — while dump #2 is still queued and about to stream. The wizard callers then immediately start the push: app.js:7147 / 7189 / 7345 all do `await boardGo(n, { mode: 'configure' })` right after `waitForBoardReady` resolves.

That pollutes the push's acknowledgement logic. `sendAndAwaitIdle` restarts its 250 ms quiet window on EVERY received line (app.js:5285-5290) and decides `ok: lines.length > 0` (app.js:5281). With an unrelated backup dump streaming, the first several push commands can only end at the 5000 ms hard timeout, and each is reported `ok: true` on the strength of backup output rather than a real response — so a genuinely dropped command in that window is counted as acknowledged and `_pushFullyAcked` stays true.

**Failure scenario.** After a Factory Reset in wizard mode, waitForBoardReady's first probe gets a response; the board is mid-way through emitting the second full backup when boardGo(configure) starts. The push's opening commands (?HW, ?WCB, ?WCBQ, ?WCBCH, ?CONTROLLER) each burn the full 5 s hard timeout and are all marked acknowledged from backup lines. The push takes ~25 s longer than it should, and if the board actually dropped one of those commands the Wizard reports 'configured' instead of the intended 'some settings may not have applied'.

**Suggested fix.** Mirror boardPull's two-tier shape: send only `WCB_WEBTOOL_CONFIG_PULL` on the first probe and add the `${pullFuncChar}backup` fallback on the SECOND tick onward (or only when the first tick produced no lines at all). Additionally, after waitForBoardReady resolves, drain/settle the link (wait for a quiet window on conn) before boardGo starts sending.

### F-220 · S3 · `Wizard/app.js:7161`

**Update FW captures preFlashGeneralSnapshot but never stashes it, so the forced full push writes whatever the DOM holds after the flash**

*Category:* `logic`

app.js:7055-7059 captures the snapshot for all three esptool modes, with an explicit rationale:

```js
// The flash takes 30–60 s; during that window other boardPulls can
// overwrite boardConfigs[n] and the shared general DOM fields (password,
// MACs, WCBQ), so we must capture here while everything is still correct.
const preFlashConfigSnapshot  = ...;
const preFlashGeneralSnapshot = captureGeneralDOMSnapshot();
```

`postFlashGeneralSnapshot[n] = preFlashGeneralSnapshot;` is then assigned only in the Flash/Factory branch (app.js:7161) and the flash-failed-but-version-matches branch (app.js:7263). The `isUpdate` branch (app.js:7130-7156) never assigns it, so when it calls `await boardGo(n, { mode: 'configure' })` (app.js:7147) the restore at app.js:7383-7386 is a no-op and boardGo reads the *current* DOM instead (app.js:7411-7418).

That matters more on this path than elsewhere because the update branch sets `boardBaselines[n] = null` (app.js:7144), and `const fullPush = !boardBaselines[n]` (app.js:7429) makes buildCommandString emit `MAC,2`, `MAC,3`, `EPASS`, `WCBCH`, `DELIM`, `CMDCHAR` unconditionally (parser.js:1283-1307). So any drift in the shared general DOM during the 30-60 s flash is written straight to the board's NVS — the exact outcome the 7055 comment exists to prevent.

**Failure scenario.** Two boards are connected. The user starts Update FW on WCB 1. Mid-flash the periodic/auto pull of WCB 4 completes; because WCB 4 is the first board pulled in this session, syncGeneralFromConfig writes WCB 4's ESP-NOW password and MAC octets into g-password/g-mac2/g-mac3 (app.js:8091-8093). WCB 1 reconnects, waitForBoardReady succeeds, and boardGo(mode:'configure') runs a full push using the now-drifted DOM — writing WCB 4's credentials into WCB 1's NVS.

**Suggested fix.** Assign `postFlashGeneralSnapshot[n] = preFlashGeneralSnapshot;` on the isUpdate path too (before the `boardGo(n, { mode: 'configure' })` call at 7147, and in the non-wizard IIFE at 7149-7155), matching what the flash/factory branch already does at 7161.

### F-221 · S3 · `Wizard/app.js:8049`

**remoteBoardPull rethrows on a failed relay send, but 9 of its 10 call sites neither await nor catch it**

*Category:* `async`

`remoteBoardPull` deliberately rethrows after cleaning up:

```js
} catch (e) {
  _pullingBoards.delete(targetN);
  cleanup();
  if (timer) clearTimeout(timer);
  termLog(relayN, `[Remote] Send to relay failed: ${e.message}`, 'err');
  updateBoardStatusBadge(targetN, 'error');
  onComplete?.(false);
  throw e;
}
```
(app.js:8034-8050), and the throw is reachable whenever `relayConn.send()` rejects — `BoardConnection.send()` throws `'Not connected'` at app.js:5213.

I grepped every call site. Only ONE handles the rejection — app.js:533, `remoteBoardPull(...).catch(() => res(false))`. The other nine are bare fire-and-forget: app.js:494 (`relayManageOne`), 1838 and 1863 (post-OTA), 5945 (`modalRemoteConnect`), 7716 and 7741 (the ETM online/offline listener), 8021 (its own retry `setTimeout`), 8058 and 8066 (`boardPullRemote`). None is inside a try/catch — 7716/7741 sit in a plain listener callback invoked from `_handleLine`'s `forEach` (app.js:5313), and 8021 is inside a `setTimeout` where a rejection cannot be caught by any caller.

**Failure scenario.** A relay's USB cable is bumped loose while three boards are managed through it. The relay's `_connected` flips false, then an in-flight retry timer fires `remoteBoardPull(relayN, targetN, 2, ...)` at app.js:8021. `relayConn.send()` throws 'Not connected', `remoteBoardPull` rethrows into a `setTimeout` callback, and the browser surfaces an uncaught-in-promise error. Nothing in the UI reports it beyond the `termLog`, and on a page with an error-reporting overlay or `window.onunhandledrejection` handler it becomes user-visible noise.

**Suggested fix.** Either stop rethrowing (the function already performs all its own cleanup and calls `onComplete?.(false)`, so the throw adds nothing for the nine fire-and-forget callers), or append `.catch(() => {})` at each of the nine unhandled call sites. Removing the `throw e;` at app.js:8049 is the smaller change and keeps app.js:533's `.catch` harmless.

### F-222 · S3 · `Wizard/app.js:8103`

**Every config pull and file load raises the "unsaved general settings" flag and fires three spurious "Changes pending" toasts**

*Category:* `logic`

`syncGeneralFromConfig` ends by calling the three general-field handlers to mirror the DOM into the model (Wizard/app.js:8103-8105):

```js
onGeneralPasswordChange();
onGeneralMacChange();
onGeneralCmdCharChange();
```

Each of those ends in `_notifyGeneralChanged()` (Wizard/app.js:1141, :1151, :1185), which is not a silent sync — it is the user-edit notifier (Wizard/app.js:1073-1077):

```js
function _notifyGeneralChanged() {
  generalSettingsDirty = true;
  updatePushAllButton();
  showToast('Changes pending — push to all boards to apply', 'info', 3000);
}
```

So three identical toasts stack and `generalSettingsDirty` is set to true purely as a side effect of a **read**. `generalSettingsDirty` is what drives the unsaved-changes indicator (Wizard/app.js:3158 — `const anyUnsaved = generalSettingsDirty || …`) and is only cleared by a completed Push All (Wizard/app.js:8334).

The same triple call sits in `restoreGeneralDOMSnapshot` (Wizard/app.js:8173-8175), which runs mid-push after a flash or NVS erase (Wizard/app.js:7344, :7383), and `syncGeneralFromConfig` is also invoked by `loadSystemFileContent` (Wizard/app.js:8371) and by the direct-pull path (Wizard/app.js:6856).

**Failure scenario.** User opens the Wizard, connects WCB1 over USB and clicks "Pull Config" — a purely read-only operation. Three overlapping "Changes pending — push to all boards to apply" toasts appear and the Push All button lights up as if there were unpushed edits. The same three toasts fire again in the middle of a post-flash config push (restoreGeneralDOMSnapshot), interleaved with the real progress toasts. A user who trusts the indicator now cannot distinguish "I have real pending general edits" from "I just read a board".

**Suggested fix.** Give the three handlers a silent variant (e.g. `onGeneralPasswordChange(notify = true)` that skips `_notifyGeneralChanged()` when false), or extract the model-sync half into `_applyGeneralDomToModel()` and call that from `syncGeneralFromConfig` (Wizard/app.js:8103-8105) and `restoreGeneralDOMSnapshot` (:8173-8175), leaving `_notifyGeneralChanged()` on the genuinely user-driven `oninput`/`onchange` paths only.

### F-223 · S3 · `Wizard/app.js:8109`

**syncGeneralFromConfig updates the ETM DOM fields but not systemConfig.general.etm — a pulled board's ETM tuning is exported as stale defaults**

*Category:* `round-trip-loss`

`syncGeneralFromConfig` pushes the pulled ETM tuning into the DOM only (Wizard/app.js:8107-8119):

```js
if (config.etm.enabled) {
  set('g-etm-timeout', config.etm.timeoutMs);
  set('g-etm-hb',      config.etm.heartbeatSec);
  … set('g-etm-delay', config.etm.messageDelayMs);
}
```

The function's own sync-back block only covers two fields — `if (systemConfig?.general) systemConfig.general.wcbQuantity = …` / `… .meshChannel = …` (Wizard/app.js:8101-8102) — and it never calls `onGeneralETMChange()` (Wizard/app.js:1214-1239), which is the only thing that copies the six ETM tuning fields from the DOM into `systemConfig.general.etm` and `boardConfigs[*].etm`. Assigning `el.value` fires no `oninput`, as the comment at Wizard/app.js:8099-8100 already notes for the other fields.

`exportSystemFile` reads only two ETM fields from the DOM (Wizard/app.js:8414-8415: `.enabled`, `.checksumEnabled`); `buildSystemFile` then emits the [GENERAL] block from `systemConfig.general.etm` via `applyGeneralToBoard` (Wizard/parser.js:1567, :1207), i.e. from the untouched factory defaults in `createDefaultSystemConfig` — `timeoutMs: 500, heartbeatSec: 10, missedHeartbeats: 3, bootHeartbeatSec: 2, messageCount: 20, messageDelayMs: 100` (Wizard/parser.js:172-181).

Secondary defect in the same block: the `if (config.etm.enabled)` guard at line 8109 means a pull from a board with ETM disabled leaves the six DOM fields showing whatever the previous board had.

**Failure scenario.** A fleet is tuned to ETM timeout 1500 ms / heartbeat 5 s. The user opens the Wizard fresh, connects WCB1, pulls. `syncGeneralFromConfig` runs (first pull, generalBaseline is null) and the ETM fields correctly display 1500 and 5. The user clicks "Export Config File" without touching any ETM input. The exported [GENERAL] block reads `?ETM,TIMEOUT,500^?ETM,HB,10^…`. Re-importing that file shows 500/10 in the UI; the moment the user nudges any ETM input, `onGeneralETMChange` fans 500/10 out to every `boardConfigs[*].etm`, and the next Push All writes the wrong ETM tuning to the whole fleet.

**Suggested fix.** In `syncGeneralFromConfig`, after the `set('g-etm-*')` calls, invoke `onGeneralETMChange()` (and `onETMToggle()` / `onETMChecksumToggle()`), the same way it already invokes `onGeneralPasswordChange()` / `onGeneralMacChange()` / `onGeneralCmdCharChange()` at lines 8103-8105 — or assign `systemConfig.general.etm = { ...config.etm }` directly. Also move the six `set(...)` calls out of the `if (config.etm.enabled)` guard so the fields always reflect the pulled board.

### F-224 · S3 · `Wizard/app.js:8805`

**Terminal panes grow without bound — termLog appends a div per line forever with no cap**

*Category:* `leak`

`termLog`'s `addLine` helper creates a `<div>` per line and appends it unconditionally (Wizard/app.js:8794-8807), ending with `output.appendChild(line);` at Wizard/app.js:8805. Nothing anywhere trims the pane: grepping app.js for `removeChild`, `firstChild`, `children.length` and any MAX-lines constant turns up only `migrateTerminalPane`'s move loop (Wizard/app.js:8753-8754) and two unrelated empty-state checks (:3634, :3674). The only pruning is the user-initiated `clearTerminalPane` (Wizard/app.js:8725) / `clearAllTerminals` (:8735).

Every received board line reaches this path via `_handleLine` (Wizard/app.js:5391, :5400), and the terminal is precisely where debug output goes: the six DEBUG toggles (Wizard/app.js:8556-8563 — `DEBUG`, `DEBUG,MAESTRO`, `DEBUG,PWM`, `DEBUG,HCR`, `DEBUG,ETM`, `DEBUG,MGMT`) exist to make boards emit continuously. `_suppressTerminalLine` (Wizard/app.js:8776-8792) filters routine poll/telemetry noise but explicitly passes everything else through, and `?TERMDEBUG,ON` (`_termVerbose`, Wizard/app.js:8777) disables the filter entirely.

With auto-scroll on (default), each append also triggers `output.scrollTop = output.scrollHeight` (Wizard/app.js:8807), forcing a layout against an ever-growing node list.

**Failure scenario.** Greg leaves the Wizard open on a bench session with `DEBUG,MAESTRO` and `DEBUG,ETM` enabled on two boards while a controller drives servos — a few hundred lines per second across panes. After an hour the terminal panes hold on the order of a million detached-free DOM nodes; the tab's memory climbs into the GB range and every subsequent append re-lays-out the whole list, so the UI (including the flash progress bar and the Push buttons) becomes visibly laggy. Nothing recovers it short of clicking Clear on each pane or reloading, which also loses the connection state.

**Suggested fix.** Cap each pane in `addLine`: after `output.appendChild(line)`, add `while (output.childElementCount > TERM_MAX_LINES) output.removeChild(output.firstChild);` with e.g. `const TERM_MAX_LINES = 3000;`. Trim in batches (drop 500 at a time once over the cap) so the common path stays a single append.

### F-225 · S3 · `Wizard/app.js:8805`

**Terminal panes grow without bound — no line cap on termLog**

*Category:* `leak`

`termLog`'s `addLine` appends a fresh `<div>` per line and never trims:

```js
const addLine = (output, idx) => {
  const line = document.createElement('div');
  line.className = `terminal-line-${type}`;
  ...
  output.appendChild(line);
  if (boardAutoScroll[idx] !== false) output.scrollTop = output.scrollHeight;
};
```
(app.js:8794-8808). I grepped the whole Wizard for any trimming — `removeChild`, `firstElementChild`, `children.length`, `MAX_LINES`, `maxLines`, `TERM_MAX` — and the only matches are in unrelated mapping-row code (app.js:601, 3634, 3674). The sole way to shrink a pane is the manual `clearTerminalPane` (app.js:8725) behind the ✕ button, or `clearAllTerminals` (app.js:8759).

Every serial line from every connected board reaches this: `_handleLine` calls `termLog(this.boardIndex, displayed, 'out')` for anything not suppressed (app.js:5400-5401), plus `[TERM:N]` relayed lines (app.js:5391), plus `_startReading`'s own `[sr]` diagnostics per reader acquisition (app.js:5500, 5504), plus every push/pull command echo. `?TERMDEBUG,ON` sets `_termVerbose` (app.js:8780) which disables the whole suppression filter, admitting rc_hb/rc_ch telemetry at up to 20 Hz.

**Failure scenario.** A user leaves the Wizard open on a bench droid for an afternoon with DEBUG modes toggled on (the per-pane DEBUG buttons at app.js:8663-8666), or types `?TERMDEBUG,ON` to chase an RC issue and forgets to turn it back off. rc_ch streams at up to 20 lines/s; after an hour that is ~72,000 DOM nodes in one pane, and `output.scrollTop = output.scrollHeight` forces a full layout on every single append. The tab progressively slows, then Chrome shows "Page unresponsive" — mid-session, potentially while a board is connected for a flash.

**Suggested fix.** Cap each pane in `addLine`: after `output.appendChild(line)`, add `while (output.childElementCount > TERM_MAX_LINES) output.removeChild(output.firstElementChild);` with `TERM_MAX_LINES` around 2000-5000. Keep the trim before the `scrollTop` assignment so only one layout is forced.

### F-226 · S3 · `Wizard/app.js:9302`

**Wizard pushes ETM missedHeartbeats = 3, reverting the firmware's deliberately widened default of 5**

*Category:* `protocol-contract`

`wizardDefaultState()` seeds the ETM block with `missedHeartbeats: 3`:

```js
    etmConfig:     { timeoutMs:500, heartbeatSec:10, missedHeartbeats:3,
                     bootHeartbeatSec:2, messageCount:20, messageDelayMs:100,
                     checksumEnabled:true },
```

Every other value matches the firmware (`etmHeartbeatSec = 10`, `etmTimeoutMs = 500`, Code/WCB/WCB.ino:266-268; `etmBootHeartbeatSec` default 2, Code/WCB/WCB_Storage.cpp:2227). `missedHeartbeats` does not — the firmware default is 5, and the comment on that line records why:

```cpp
int etmMissedHeartbeats = 5;   // (etmHeartbeatSec+1)*5 = 55s offline window — widened from 3/33s so a few lost broadcast heartbeats on a busy mesh don't flap a live board (mirrors WCBClient _missedBeforeOffline)
```

The wizard's value is emitted on every full push — `if (fullPush || !betm || betm.missedHeartbeats !== etm.missedHeartbeats) add(`ETM,MISS,${etm.missedHeartbeats}`)` (Wizard/parser.js:1491) — and `wizardApplyConfig` copies `ws.etmConfig` into every board's `cfg.etm` (:10999-11003) plus `systemConfig.general.etm` (:11024). So a wizard-configured fleet is put back on the 33 s window the firmware comment says was causing live boards to flap.

**Failure scenario.** User configures a 4-board fleet with the Wizard, accepting the ETM defaults. Every board is pushed `?ETM,MISS,3`, narrowing the offline threshold from `(10+1)*5 = 55 s` to `(10+1)*3 = 33 s`. On a busy mesh (an OTA, a config pull, a Kyber/Maestro burst) three consecutive unacknowledged broadcast heartbeats are lost and a healthy board is marked OFFLINE — the exact flapping the firmware default was widened to stop. Under ETM, commands to a board marked offline are then dropped.

**Suggested fix.** Change the wizard default to `missedHeartbeats: 5` to match Code/WCB/WCB.ino:267. The same stale 3 also lives in `WCBParser.createDefaultBoardConfig` (Wizard/parser.js:122, :176) and the `MISS` parse fallback (:828) — align all four so a pull/push round trip can't reintroduce it.

### F-227 · S3 · `Wizard/app.js:11669`

**wizardCancelConnect tears down the connection without updating the main page UI**

*Category:* `dom`

`wizardCancelConnect` disconnects and drops the connection object:

```js
  try { await boardConnections[n]?.disconnect(); } catch (_) {}
  delete boardConnections[n];          // ← line 11669
  // Show the Reconnect button so user can re-open the panel
  wizardEnableConnectBtns(n);
  wizardSetConnectStatus(n, '', 'Cancelled');
```

Unlike `boardDisconnect`, which ends with `updateConnectionUI(n, false)` (:6218), this path never refreshes the board card behind the modal. `updateConnectionUI` is what clears the green dot, resets the label to "Not connected", flips the Connect button back from "Disconnect", re-disables Pull/Push/Erase/Identify/Stats and sets the status badge (:7611-7660) — none of which happens. It also leaves `remoteRelayForBoard[n]` in place, which `boardDisconnect` deletes (:6211).

**Failure scenario.** On the Connect step the user picks the wrong port for WCB 2, the connection comes up, then they click ✕ to cancel. They close the Wizard. The WCB 2 card on the config page still shows a green dot, "Connected", a red Disconnect button and enabled Pull Config / Push Config, while `boardConnections[2]` no longer exists. Clicking Push Config produces a bare "Board not connected" toast on a card that claims to be connected.

**Suggested fix.** Call `updateConnectionUI(n, false)` (and `delete remoteRelayForBoard[n]`) at the end of `wizardCancelConnect`, or just route through `boardDisconnect(n)` the way `wizardManualConnect` does (:11683).

### F-228 · S3 · `Wizard/app.js:12028`

**Relay stats/ETM fetch fires an un-awaited, un-caught send inside a Promise executor — hangs for up to 120 s**

*Category:* `async`

The relay branch of `fetchStatsData` builds its own collector promise and then sends without awaiting or catching:

```js
relayConn._dataCallbacks.push(onLine);
relayConn.send(mgmtCmd + '\r');
```
(app.js:12027-12028, inside the `new Promise((resolve, reject) => {...})` opened at app.js:11990).

`BoardConnection.send()` is `async` and throws `new Error('Not connected')` when `!this._connected || !this.port?.writable` (app.js:5213), and can also reject from `writer.write()`. Both sibling collectors handle this — `sendAndCollect` does `this.send(command + '\r').catch(finish)` (app.js:5253) and `sendAndAwaitIdle` does the same (app.js:5294) — this one does not.

The only other way out of the promise is the timeout at app.js:12000-12006, which is `relayTimeout` = 120000 ms for ETM and 10000 ms for stats (app.js:11972).

**Failure scenario.** The user opens ETM Characterization on a relay-managed board and the relay's USB is unplugged (or its port write fails) at that instant. `relayConn.send()` rejects; nothing catches it, so the browser logs an unhandled promise rejection and the collector promise stays pending. The stats modal sits on "Running characterization on WCB n via WCB r — this can take 30–60s…" for a full two minutes before the timeout rejects with a misleading "Timed out waiting for ETM response", and `_statsFetchBusy` (app.js:11973) stays true that whole time, suppressing mesh discovery.

**Suggested fix.** Mirror the sibling collectors: `relayConn.send(mgmtCmd + '\r').catch(e => { if (done) return; clearTimeout(timer); finish(); reject(e); });`

### F-229 · S3 · `Wizard/flasher.js:86`

**`loadScript` treats a previously FAILED script tag as a cache hit, poisoning every later flash attempt**

*Category:* `error-handling`

```js
function loadScript(src) {
  return new Promise((resolve, reject) => {
    if (document.querySelector(`script[src="${src}"]`)) { resolve(); return; }
    const s = document.createElement('script');
    s.src = src;
    s.onload = resolve;
    s.onerror = () => reject(new Error(`Failed to load CDN script: ${src}`));
    document.head.appendChild(s);
  });
}
```

On `onerror` the promise rejects but the `<script>` element is left in `document.head`. The next call to `loadScript(CRYPTOJS_SRC)` matches that dead element and resolves immediately, so `flashFirmware` proceeds past `:308-312` with `CryptoJS` still undefined. The failure then surfaces inside `calculateMD5Hash` at `:689-690` (`CryptoJS.MD5(...)`), which the vendored `writeFlash` invokes per file (`t.calculateMD5Hash&&(a=t.calculateMD5Hash(e))`) — that throw is swallowed by the retry wrapper at `:694-712`, retried once, and finally rethrown at `:710` with the `willWriteBootOrPart` recovery text appended.

(The rejection message also still says "Failed to load CDN script" even though this file's own header comment at `:12-13` records that the deps were deliberately moved off the CDN into `Wizard/vendor/` — the message points users at the wrong problem.)

**Failure scenario.** A GitHub Pages hiccup makes `vendor/crypto-js/crypto-js-4.2.0.min.js` fail once. The user sees "Could not load CryptoJS — try a hard refresh" and clicks Flash again without hard-refreshing. This time the load "succeeds" instantly, the bootloader handshake runs, then the write dies twice with `ReferenceError: CryptoJS is not defined` and the user is shown "Flash write failed after retry: CryptoJS is not defined ⚠️ The board may be in an unbootable state. To recover: 1. Hold the BOOT button…" — a brick warning for a board that was never written to at all (the MD5 callback runs before `flashDeflBegin`).

**Suggested fix.** Remove the element on failure and mark successful loads explicitly: `s.onerror = () => { s.remove(); reject(new Error(`Failed to load ${src}`)); };` and gate the cache hit on a success flag (e.g. `s.dataset.loaded = '1'` in `onload`, and only short-circuit on `script[src="…"][data-loaded="1"]`). Additionally, sanity-check `typeof CryptoJS !== 'undefined'` right after `await loadScript(CRYPTOJS_SRC)` so the failure is reported before the bootloader handshake instead of mid-write.

### F-230 · S3 · `Wizard/flasher.js:168`

**A successfully fetched partition image is discarded when an unrelated bootloader fetch fails, silently disabling the migration check**

*Category:* `error-handling`

In `getBinaryData` the partition image is fetched first but only pushed into `images` on the **last** line of the try block:

```js
const partBuf = await fetchBySuffix(`_${binaryType}_part.bin`);   // :149 — succeeded
if (binaryType === 'ESP32S3') {
  try { boot['16MB'] = await fetchBySuffix(`_${binaryType}_boot_16MB.bin`); }
  catch (_) { boot['16MB'] = await fetchBySuffix(`_${binaryType}_boot.bin`); }  // :156 — throws here escape
  ...
} else {
  boot.single = await fetchBySuffix(`_${binaryType}_boot.bin`);                  // :166
}
images.unshift({ buf: partBuf, address: 0x8000 });                              // :168 — never reached
```

Any bootloader-fetch failure (`:156` for S3, `:166` for classic — GitHub raw rate-limit or a network blip, exactly what the catch comment at `:170-173` says this branch means) jumps to `:169` and throws away `partBuf`, which was already in hand. `hasBootPart` goes false and the partition image is absent from `fw.images` even though it downloaded cleanly.

Downstream, `flashFirmware` then reports "No partition image available — migration check skipped" (`:491`) or "Existing firmware detected — will flash app only" (`:541`) and writes app-only *without ever comparing the on-board table*, on a run where that comparison was fully possible.

**Failure scenario.** GitHub rate-limits one of the six sequential `download_url` fetches (six requests per flash, so this is the common transient). The Wizard logs "⚠️ Could not fetch bootloader/partition files … flashing app only; the partition-table migration check was SKIPPED this run", then flashes app-only onto a board still on the old default partition scheme — the one case `comparePartitionTable` exists to catch — even though the partition image needed for that comparison had already been downloaded successfully.

**Suggested fix.** Move `images.unshift({ buf: partBuf, address: 0x8000 })` to immediately after `:149`, and track the bootloader availability separately from the partition availability (e.g. keep `hasBootPart` for the abort guards at `:588` but let the migration check run whenever a partition image is present). Alternatively wrap only the bootloader fetches in their own try/catch.

### F-231 · S3 · `Wizard/flasher.js:540`

**Auto-detect "Flash" path drops the legacy-slot size guard that the "Update FW" path has**

*Category:* `logic`

The `appOnly` branch treats an unverifiable partition table as dangerous and refuses when the app has outgrown the legacy `ota_0` slot (`Wizard/flasher.js:476-487`):

```js
const OLD_OTA0_SIZE = 0x140000;  // default-scheme ota_0 slot size
const appImg = flashImages.find(img => img.address === 0x10000);
if (appImg && appImg.buf.byteLength > OLD_OTA0_SIZE) { ... throw new Error(msg); }
```

The auto-detect branch has no equivalent. When the on-board bootloader magic is 0xE9 and no partition image is available, it just logs and falls through to app-only:

```js
const partImg = flashImages.find(img => img.address === 0x8000);
if (!partImg) {
  onLog('Existing firmware detected — will flash app only (NVS preserved)');   // :541
} else { ...comparePartitionTable... }
```

`imagesToFlash` then becomes the 0x10000-only filter at `:576`, so neither the `!fw.hasBootPart` guard at `:588` (identity check fails) nor the `bootBlockReason` guard at `:602` fires. The result is an unverified app-only write on the Flash path, with no size check at all.

The headroom is already thin: the committed `Wizard/bin/WCB_ESP32.bin` is 1284464 bytes against `OLD_OTA0_SIZE` = 1310720 — 26 KB, and the app grows every release. `Code/bin/build.sh:79-83` records why this matters: "Boards still on the old table MUST be full-flashed (boot+part+app) once — an app-only update would overrun the old 1.25MB slot."

**Failure scenario.** After the app crosses 1.25 MB (26 KB away today), a user with a board still on the pre-min_spiffs table hits Flash during a transient GitHub fetch failure. `partImg` is undefined, the 0xE9 branch takes the app-only path with no size check, and esptool writes ~1.3 MB at 0x10000 straight through the end of the old 0x140000 ota_0 slot into app1 — leaving a corrupt image the bootloader will roll back or refuse, on a board the user asked to fully flash.

**Suggested fix.** Hoist the `OLD_OTA0_SIZE` check into a shared helper and apply it at `:540` too: when `!partImg` and the app exceeds 0x140000, either force `forceFull = true` (safe here, since the `!fw.hasBootPart` guard at `:588` will then abort loudly rather than write app-only) or throw the same 'use the full Flash path' error the appOnly branch raises.

### F-232 · S3 · `Wizard/index.html:177`

**ETM "Message Count" field has no range and defaults below the firmware's floor of 10, so the value never round-trips; its label also describes the wrong setting**

*Category:* `protocol-contract`

The field is `<input type="number" id="g-etm-count" value="3" oninput="onGeneralETMChange()">` (Wizard/index.html:177) — no `min`, no `max`, and a default of 3. `onGeneralETMChange()` reads it with only a falsy fallback: `const count = parseInt(document.getElementById('g-etm-count')?.value) || 3;` (Wizard/app.js:1219) and writes it to every board (app.js:1236).

The firmware hard-clamps it: `etmCharMessageCount = constrain(etmVal.toInt(), 10, 200);` (Code/WCB/WCB.ino:4696; the legacy alias does the same at :5303). Anything below 10 or above 200 is silently rewritten. The board then reports the clamped value in its backup (`emit("ETM,COUNT," + String(etmCharMessageCount), true)`, WCB.ino:2871), so the baseline says 10 while the model still says 3 — and `if (fullPush || !betm || betm.messageCount !== etm.messageCount) add(\`ETM,COUNT,${etm.messageCount}\`)` (Wizard/parser.js:1495) re-emits the rejected value on every subsequent push.

Note `createDefaultBoardConfig()` uses `messageCount: 20` (Wizard/parser.js:125), which matches the firmware default `int etmCharMessageCount = 20;` (WCB.ino:269) — but the DOM default of 3 wins, because `onGeneralETMChange()` re-reads all six ETM inputs whenever ANY of them is edited, and `syncGeneralFromConfig()` only writes the DOM when the pulled board has ETM enabled (`if (config.etm.enabled) { … set('g-etm-count', config.etm.messageCount); … }`, Wizard/app.js:8109-8116).

Separately, the tooltip is wrong. It reads "How many times to repeat the offline alert message when a board is detected as unresponsive." The variable is `etmCharMessageCount`, used only by the ETM characterization run: `Serial.printf("Messages per board per phase: %d\n", etmCharMessageCount);` (WCB.ino:1436) and `int totalMessages = etmCharMessageCount * peerCount;` (WCB.ino:1456). It has nothing to do with offline alerts. The same wrong wording is mirrored in `GENERAL_FIELD_LABELS.etmCount` (Wizard/app.js:1406).

**Failure scenario.** User opens Advanced, expands ETM, and changes only the Timeout from 500 to 800. `onGeneralETMChange()` also reads `g-etm-count`, which still shows its HTML default of 3, and writes messageCount=3 to every board. The push sends `?ETM,COUNT,3`; the board stores 10. The next pull sets the baseline to 10 while the model holds 3, so every future push re-sends `?ETM,COUNT,3` forever. Meanwhile a user who wanted a longer network characterization run and typed 500 gets 200 with no indication it was clamped.

**Suggested fix.** Add `min="10" max="200"` to the input, change its default `value` to 20 to match both `createDefaultBoardConfig()` and the firmware default, and clamp in `onGeneralETMChange()` (`Math.max(10, Math.min(200, count))`) the way `onHCRPollChange()` already does at app.js:4232-4237. Fix the tooltip and `GENERAL_FIELD_LABELS.etmCount` to say "Network-test messages sent per board per phase (10-200)".

### F-233 · S3 · `Wizard/parser.js:1281`

**A custom controller-peer ID is lost whenever the controller is disabled**

*Category:* `round-trip-loss`

`buildCommandString` collapses the disabled case to a bare OFF:
```
1281:    add(curSpecial ? `CONTROLLER,ON,${config.specialPeerId ?? 20}` : `CONTROLLER,OFF`);
```
The firmware goes out of its way to preserve the id in exactly this case (WCB.ino:2865-2872):
```
  } else if (WCB_SPECIAL_PEER_ID != 20) {
    // Preserve a CUSTOM controller id even while the controller is disabled — set
    // it then turn it back off — so a restore doesn't silently revert it to the default 20.
    emit("CONTROLLER,ON," + String(WCB_SPECIAL_PEER_ID), true);
    emit("CONTROLLER,OFF", true);
```
The parser reads that pair correctly (`CONTROLLER,ON,15` then `CONTROLLER,OFF` → `specialPeer=false, specialPeerId=15`, verified), so the value survives the pull and is then thrown away on the way back out. The firmware accepts the two-command form on input (WCB.ino:4861-4886).

**Failure scenario.** Board uses a non-default controller peer ID (e.g. 15) with the controller currently disabled. Export a config file or do a post-flash full push: only `?CONTROLLER,OFF` is written, so the restored board reverts to ID 20. When the operator later re-enables the controller, it registers the wrong peer and NaviCore never appears in the peer table.

**Suggested fix.** Mirror the firmware: when `!curSpecial && (config.specialPeerId ?? 20) !== 20`, emit `CONTROLLER,ON,<id>` followed by `CONTROLLER,OFF`.

### F-234 · S3 · `Wizard/parser.js:1346`

**Remote Maestro proxies parsed into `kyber.targets` are never re-emitted unless Kyber mode is 'local'**

*Category:* `round-trip-loss`

`parseToken`'s MAESTRO case routes any entry whose W-number differs from this board into `config.kyber.targets` (:809-814), which is where the firmware's remote Maestro proxy slots land — `printMaestroBackup` emits them as `MAESTRO,M<id>:W<remoteWCB>S<port>:<baud>` (WCB_Maestro.cpp:947-957), and they exist independently of Kyber (auto-learned by `maestroAutoAddRemote`, per CLAUDE.md rule 5).

But `buildCommandString` only writes `kyber.targets` back inside the LOCAL branch (:1346-1350). With `kyber.mode === 'none'` or `'remote'`, the targets are silently dropped and `KYBER,CLEAR` / `MAESTRO,REMOTE` is emitted instead; the `MAESTRO,<chain>` built at :1466-1469 comes from `config.maestros` (locals only) plus `config.maestroTable`, neither of which contains them.

Verified: a board with `?KYBER,CLEAR` + `?MAESTRO,M1:W2S1:57600` + `?MAESTRO,M4:W5S2:57600` parses to `maestros:[{id:1,…}] , kyber.targets:[{id:4,wcb:5,port:2,…}]`, and the full-push rebuild contains `?KYBER,CLEAR` and `?MAESTRO,M1:W2S1:57600` — `M4:W5S2` is gone.

**Failure scenario.** A non-Kyber board has learned a remote Maestro proxy for M4 hosted on WCB5 (so `;M4` routes across the mesh). Export a system file and restore it to a replacement board: the proxy is missing, so `;M4` does nothing on that board until WDP happens to re-advertise it (and never, if WCB5's adverts are off or WDP is disabled).

**Suggested fix.** Emit remote targets as `MAESTRO,M<id>:W<wcb>S<port>:<baud>` entries in the MAESTRO chain (:1466-1469) whenever `kyber.mode !== 'local'`, instead of only folding them into `KYBER,LOCAL`.

### F-235 · S3 · `Wizard/parser.js:1365`

**Clearing an MP3/DFP error-callback sequence never reaches the board**

*Category:* `round-trip-loss`

The onError callback is only ever emitted when non-empty:
```
1364:      add(`MP3,S${config.mp3.port}:${config.mp3.baud}:V${config.mp3.volume}`);
1365:      if (config.mp3.onError) add(`MP3,ONERR,${config.mp3.onError}`);
```
and identically for DFP at :1399-1400. The firmware supports removal — `WCB_MP3.cpp:235-241`:
```
  if (aUpper.startsWith("ONERR,")) {
    String key = args.substring(6); key.trim();
    if (key.equalsIgnoreCase("CLEAR")) { memset(mp3Config.onErrCmd, 0, sizeof(mp3Config.onErrCmd)); saveMP3Settings(); …
```
(`WCB_DFP.cpp` mirrors it), and the parser reads the value back at :659-660 / :714-715, so it is a genuine UI-settable field. Verified: baseline with `MP3,ONERR,seqA`, clear it in the model, delta push = `?MP3,S5:9600:V20` only — the board keeps `seqA`, and the next pull restores it into the UI.

**Failure scenario.** User configures an MP3 error callback sequence, later deletes that stored sequence and blanks the "on error" field, then pushes. The board still fires the now-missing sequence key on every MP3 error, and the next Pull silently re-populates the field the user just cleared.

**Suggested fix.** Emit `MP3,ONERR,CLEAR` (and `DFP,ONERR,CLEAR`) when the baseline had an onError and the current value is empty, the same shape as the MP3/HCR/DFP `REMOTE,OFF` handling a few lines below (:1371, :1385).

### F-236 · S3 · `Wizard/parser.js:1488`

**ETM tuning parameters are never emitted when ETM is disabled, so a restore reverts them to firmware defaults**

*Category:* `round-trip-loss`

Every ETM sub-setting is gated on the CURRENT enabled state:
```
1486:  if (fullPush || !betm || betm.enabled !== etm.enabled) add(etm.enabled ? 'ETM,ON' : 'ETM,OFF');
1488:  if (etm.enabled) {
1489-1496:   … ETM,TIMEOUT / HB / MISS / BOOT / COUNT / DELAY / CHKSM …
```
The firmware emits them unconditionally (WCB.ino:2854-2862: `emit(etmEnabled ? "ETM,ON" : "ETM,OFF"); emit("ETM,TIMEOUT,"…); … emit("ETM,CHKSM,"…)`) and the parser reads them all back regardless of enabled state (:821-835). Verified: a board with `ETM,OFF / ETM,TIMEOUT,900 / ETM,HB,7 / ETM,CHKSM,OFF` parses with `timeoutMs:900, heartbeatSec:7`, but the full-push rebuild contains only `?ETM,OFF` — the 900/7 are dropped. The `ETM,CHKSM,ON` self-heal at :1496 is inside the same guard, so a board found with checksum disabled is also never corrected while ETM is off.

**Failure scenario.** Operator runs a board with ETM temporarily off but with tuned timings (timeout 900 ms, heartbeat 7 s) saved. They export a config file / reflash and full-push it: the restored board gets `ETM,OFF` only and keeps the firmware defaults (500 ms / 10 s). When ETM is turned back on, the tuning is silently gone.

**Suggested fix.** Move the sub-parameter emits out of the `if (etm.enabled)` guard (keep only the ON/OFF line conditional), matching `collectConfigCommands` at WCB.ino:2854-2862.

### F-237 · S3 · `Wizard/styles.css:1254`

**WDP Mesh / RC Controllers panels are unreadable in light theme — hardcoded dark background from an undefined --card-bg, with theme-aware near-black text on top**

*Category:* `dom`

`.rc-devices-section { background: var(--card-bg, #1a1d28); }` (styles.css:1254) references `--card-bg`, which is defined nowhere in the file — the `:root` block (styles.css:4-27) and the `:root.light` block (styles.css:29-48) between them define exactly 25 custom properties and `--card-bg` is not one of them. The fallback `#1a1d28` therefore always wins, in both themes.

The text drawn on that background is theme-aware:
- `.rc-devices-section h2 { color: var(--text, #e0e0e0); }` (styles.css:1262) — `--text` IS defined, and `:root.light` sets it to `#111827` (styles.css:37).
- `.wdp-mesh-table td` (styles.css:1415-1421) declares no `color`, so it inherits `body { color: var(--text) }` (styles.css:70) → also `#111827` in light mode.

There is no `:root.light .rc-devices-section` override anywhere in the file (verified by grep). So in light theme the panel paints near-black `#111827` text on near-black `#1a1d28` — a contrast ratio of roughly 1.03:1.

This is not a rarely-seen panel: `<div id="wdp-mesh-section" class="rc-devices-section">` (index.html:216) has NO `style="display:none"` — unlike `#rc-devices-section` (index.html:204) — so the WDP Mesh card is rendered on every page load. Light theme is a shipped, persisted user choice (`toggleTheme()`, app.js:318-322, toggles `.light` on `documentElement` and stores it in localStorage).

The table's `<th>` cells escape this because styles.css:1422 gives them `var(--text-secondary, #9aa3b0)` (also an undefined var, but with a light-grey fallback), and `.rc-device-card` escapes it because styles.css:1274 uses `var(--bg, #0f1115)` where `--bg` IS theme-aware. Only the section chrome and the WDP table body rows are affected.

**Failure scenario.** User clicks the 🌙 theme toggle in the header (index.html:38) to switch to light mode. The whole page turns light except the '🕸️ WDP Mesh' card, which stays dark #1a1d28. Its heading '🕸️ WDP Mesh' and every row of the mesh inventory table (WCB number, alias, firmware, capabilities) render in #111827 on that dark card and are effectively invisible. The user clicks Refresh, sees the panel apparently produce nothing, and concludes WDP discovery is broken.

**Suggested fix.** Define `--card-bg` in both `:root` and `:root.light` alongside the other tokens (e.g. dark `#1a1d28`, light `#ffffff`), or change styles.css:1254 to use an existing theme-aware token: `background: var(--bg2);`. The same applies to the `var(--text-secondary, …)` and `var(--muted, …)` fallbacks scattered through this block (styles.css:1422, 1425, 1311, 1463) — they are all imported names this stylesheet never defines.


---

## S4

### F-238 · S4 · `c:/Users/ghulette/Documents/GitHub/Wireless_Communication_Board-WCB/Wizard/serial-hub.js:291`

**`_portlessRetries` reset sits on promotion rather than on a successful open — diverges from NaviCore and will re-introduce leader/follower churn once the step-down fixes land**

*Category:* `logic`

This copy resets the escalating step-down backoff the moment the tab is promoted and merely *has* a port object:

```js
if (this._port)  { this._portlessRetries = 0; await this._openAndRead(); }   // :291
```

The NaviCore copy moved the reset into `_openAndRead()` after `this._portOpen = true` (c:/Users/ghulette/Documents/GitHub/NaviCore/config_tool/serial-hub.js:329) with a comment stating exactly why: "Reset the step-down backoff HERE, not on promotion: a leader that holds a port it can never open also steps down through _stepDownPortless(), and resetting on promotion would pin that retry at the 800 ms floor — the leader↔follower churn the escalation prevents."

Today this is inert in the Wizard only because the Wizard's `_openAndRead()` never calls `_stepDownPortless()` (that is the divergence reported separately at :356). The two divergences are coupled: applying the :356 fix without also moving this reset converts a permanently-unopenable port into a ~1 Hz leader↔follower flap, because every promotion would zero the counter and `_stepDownPortless()` would always compute the 800 ms floor delay (serial-hub.js:310). The files are required to stay in lockstep and this is the third behavioural difference between them.

**Failure scenario.** After adopting the step-down fix at serial-hub.js:356 (required to fix the lock-squat deadlock), a WCB left open in the Arduino IDE makes every open() fail. The hub promotes, fails 4 opens (~1.2s), steps down with `_portlessRetries` reset to 1 → 800ms delay, re-campaigns, wins again immediately (only candidate), resets again — an endless ~2 s cycle spamming '[hub] became LEADER' / 'open failed' into the Wizard terminal instead of decaying to the intended 30 s poll.

**Suggested fix.** Move the reset: drop `this._portlessRetries = 0;` from line 291 and place it in `_openAndRead()` immediately after `this._portOpen = true;` (serial-hub.js:367), matching NaviCore/config_tool/serial-hub.js:329.

### F-239 · S4 · `Wizard/app.js:2651`

**Superseded WLED section comment left stacked above its replacement, describing removed single-slot behaviour**

*Category:* `dead-code`

app.js:2651-2657 carries two section headers back to back, the first describing a design that no longer exists:

```
// ─── WLED (serial lighting) ───────────────────────────────────────
// Mirrors the HCR device pattern. WLED speaks JSON at 115200, so the picker
// offers the hardware ports S1/S2 (the firmware rejects WLED above 9600 baud on
// the software-serial ports S3-S5). A port CLI-configured on S3-S5 @9600 is
// still preserved in the dropdown so a pulled config round-trips instead of
// collapsing to '' and getting wiped by a spurious WLED,CLEAR on the next push.
// ─── WLED (ID-addressed, multi-row — mirrors Maestro) ─────────────
const WLED_BAUD_RATES = [9600, 19200, 38400, 57600, 115200];
```

The first block's claims no longer hold. WLED does not "mirror the HCR device pattern" — it is a multi-row ID-addressed table like Maestro (`addWLEDRow` app.js:2660, `appendWLEDRow` app.js:2672, rows keyed `wled-row-<n>-<counter>`), which is what the second header correctly says. And the picker does not "offer the hardware ports S1/S2": `refreshWLEDPortDropdown` (app.js:2705) walks `for (let p = 1; p <= 5; p++)` and offers any port that is free or already this row's, with the S3-S5 constraint expressed as a baud cap (`const maxBaud = (wled.port >= 3) ? 9600 : Infinity`, app.js:2682) rather than as port exclusion.

Two smaller instances of the same drift: app.js:2512 documents `_populateRouteHostDropdown` as "Reused by both devices", but it now has three callers — MP3 (app.js:2186), HCR (app.js:2559) and DFPlayer (app.js:2352), plus three more in the render path (3057, 3097, 3134). And parser.js:1084-1086 explains the `kyber-reserved` guard as mirroring "WCB_WLED.cpp / WCB_HCR.cpp / WCB_MP3.cpp" without mentioning WCB_DFP.cpp, although the reservation does correctly cover DFPlayer (a `kyber-reserved` claim is filtered out by `updateDFPPortDropdown`'s `!claim || claim.type === 'dfp'` test at app.js:2424).

**Failure scenario.** No runtime failure. A maintainer reading app.js:2651 to change WLED port handling is told the picker only offers S1/S2 and follows the single-slot HCR pattern, then writes a guard or a dropdown filter against a model the code abandoned — the exact mistake this audit found in the MP3 baud picker.

**Suggested fix.** Delete the superseded comment block at app.js:2651-2656, keeping only the accurate header at app.js:2657. Change "Reused by both devices" at app.js:2512 to name MP3/HCR/DFPlayer, and add WCB_DFP.cpp to the firmware-guard list at parser.js:1086.

### F-240 · S4 · `Wizard/app.js:3237`

**appendMaestroRow's readOnly branch is dead, and findKyberLocalBoard() runs per row for it**

*Category:* `dead-code`

`function appendMaestroRow(n, maestro, readOnly = false)` (app.js:3237) is called from exactly two places — `addMaestroRow` (app.js:3234) and `populateMaestrosFromConfig` (app.js:3464) — and neither passes a third argument, so `readOnly` is always `false`. That makes `dis` (3243), `dimStyle` (3244), the `kyber-lock` span in `deleteBtn` (3258-3259) and the `data-readonly="1"` case (3264) unreachable.

The consequence worth noting is line 3257: `const kyberBoard = findKyberLocalBoard();` executes on **every** Maestro row append even though `kyberBoard` is only interpolated into the dead read-only branch. `findKyberLocalBoard` scans all of `boardConfigs`, so a bulk populate pays that scan once per row for a string that is never rendered.

The intent is documented at the call site — "Each board's local maestros are always editable — never grey them out" (app.js:3459-3462) — which confirms the parameter is leftover rather than a pending feature.

**Failure scenario.** No runtime failure. Reported because the residual plumbing implies a read-only Maestro mode that does not exist, and the per-row `findKyberLocalBoard()` scan makes `populateMaestrosFromConfig` O(rows × boards) for output that is discarded.

**Suggested fix.** Drop the `readOnly` parameter and the branches that depend on it (including the `findKyberLocalBoard()` call at 3257 and the `data-readonly` attribute at 3264), or move `findKyberLocalBoard()` inside the `readOnly` branch if the parameter is being kept for a planned feature.

### F-241 · S4 · `Wizard/app.js:3615`

**Dead PWM pre-claim in syncMappingsToConfig — overwritten by evaluatePortClaims three lines later**

*Category:* `dead-code`

At the end of the mapping rebuild loop:
```js
if (src >= 1 && src <= 5 && !config.serialPorts[src-1].claimedBy) {
  config.serialPorts[src - 1].claimedBy = { type: 'pwm' };
}
```
(app.js:3615-3617). This runs for **every** mapping row, Serial as well as PWM, so it transiently mislabels a serial-mapping source as a hard `pwm` claim. It has no effect either way: the next statement in the function is `WCBParser.evaluatePortClaims(config);` (app.js:3620), which opens with "Reset all claims first" — `for (const port of config.serialPorts) { port.claimedBy = null; }` (Wizard/parser.js:987-989) — and then re-derives every claim from scratch, giving PWM sources `{type:'pwm'}` (parser.js:1043-1046) and Serial sources the soft `{type:'serial-map'}` (parser.js:1069-1076). Nothing reads `claimedBy` between the two.

**Failure scenario.** No user-visible failure — the assignment is unreachable in effect. It is reported because it contradicts the documented claim model that `refreshMappingSourceDropdown` depends on (app.js:3533-3536 explicitly distinguishes 'pwm' from the soft 'serial-map' claim), so a future edit that moves or removes the `evaluatePortClaims` call would suddenly start hard-claiming serial-mapping sources and drop them out of the source dropdown.

**Suggested fix.** Delete lines 3615-3617; `evaluatePortClaims` is the single source of truth for `claimedBy` and already covers both mapping types.

### F-242 · S4 · `Wizard/app.js:4588`

**Temporary-variable SET is labelled "runtime only — not saved", but the firmware's ;V keeps an existing variable persistent and writes NVS**

*Category:* `protocol-contract`

`updateTempVariable` sends the runtime form and reports it as unconditionally volatile:

```js
4584:    const sent = await sendVariableCommand(n, `${cmdChar}V,${name},${value}`);
4588:    showToast(`Temporary "${name}" set to ${value} on WCB ${n} (runtime only — not saved)`, 'info');
```

The header comment makes the same claim: "SET sends \";V,name,value\" (cmdChar), which updates the board's RAM value WITHOUT persisting it or touching the config" (Wizard/app.js:4533-4534), and so does the button tooltip: `title="Add a temporary variable — runtime-only (set with ;V), not saved with the config, gone on reboot"` (Wizard/index.html:755).

The firmware says otherwise. Code/WCB/WCB_Variables.cpp:255-261:

```cpp
  // Persistence rule: ;VP forces persistent (creating OR promoting a volatile var).
  // ;V keeps an EXISTING variable's current persistence and makes NEW ones volatile —
  // so a plain ;V never silently drops a persistent variable's NVS backing …
  int slot = findVarSlot(name);
  bool effPersist = cmdPersist ? true : (slot >= 0 ? vars[slot].persist : false);
```

So `;V,<name>,<value>` on a name that already exists as persistent updates the **persistent** variable and rewrites NVS. "+ Temporary" lets the user type any name (Wizard/app.js:4548 leaves the input editable when `live === false`), and `updateTempVariable` performs no existence check. `getVariablesFromUI` then deliberately excludes the row from pushes (Wizard/app.js:4898), so the Wizard's config/baseline never learn the new value either.

**Failure scenario.** Board has persistent variable `MODE = 0`. User clicks "+ Temporary", types `MODE`, value `3`, clicks SET. Toast: 'Temporary "MODE" set to 3 on WCB 1 (runtime only — not saved)'. The firmware actually writes MODE=3 to NVS — it survives the reboot the user was told it would not, and the Wizard's baseline still holds 0, so the change is invisible to the next diff push and to the flash-wear reasoning the volatile/persistent split exists to protect.

**Suggested fix.** Either make the claim true by refusing the SET when the name already exists as persistent (the `?VAR,LIST` data from `collectVarList`/`renderLiveVariables` is already available for that check), or soften the toast and the tooltip to match the firmware rule — e.g. "set via ;V — stays volatile for new names, keeps an existing variable's persistence". Update the block comment at Wizard/app.js:4533-4535 and Wizard/index.html:755 in the same change.

### F-243 · S4 · `Wizard/app.js:4941`

**Duplicate-port guard in BoardConnection.connect() is unreachable — every call site passes a port, so usedPorts is never consulted**

*Category:* `dead-code`

```js
4937:  async connect(existingPort = null, usedPorts = new Set()) {
4939:    if (existingPort) {
4940:      this.port = existingPort;
4941:    } else {
4942:      this.port = await navigator.serial.requestPort();
4943:      if (usedPorts.has(this.port)) {
4944:        this.port = null;
4945:        throw new Error('That port is already connected to another WCB. Please select a different port.');
4946:      }
4947:    }
```

There is exactly one call site in the whole Wizard — `await conn.connect(port, usedPorts);` at Wizard/app.js:5670 — and `establishConnection` is only ever invoked with a concrete port (Wizard/app.js:5927, 6512, 6582, 6621, 11585, 11605, 11629). `port` is always truthy, so the `else` branch never runs and `usedPorts` is dead.

Meanwhile four call sites go to real trouble to build that set, e.g. Wizard/app.js:5920-5924:

```js
    const usedPorts = new Set(
      Object.entries(boardConnections)
        .filter(([k, c]) => parseInt(k) !== n && c?.isConnected() && c.port)
        .map(([, c]) => c.port)
    );
```

and `_wizPortUsedByOthers(n)` is computed for the wizard paths (Wizard/app.js:11583, 11602, 11626) — all of it discarded. The only thing actually stopping a second slot from binding the same physical board is `port.open()` rejecting with "already open" (Wizard/app.js:4949-4959), which surfaces a raw DOMException message instead of the written-for-this explanation.

**Failure scenario.** User has WCB 1 connected on COM7. On WCB 2 they click Connect → "Select port manually" and pick COM7 again. Instead of "That port is already connected to another WCB. Please select a different port.", they get "Connect failed: Failed to execute 'open' on 'SerialPort': The port is already open." and no hint that the conflict is with their own WCB 1.

**Suggested fix.** Move the check out of the dead branch and into `establishConnection` before the connect, e.g. at Wizard/app.js:5668: `if (usedPorts.has(port)) throw new Error('That port is already connected to another WCB. Please select a different port.');` — then either delete the unreachable `else` branch at 4941-4947 or drop the `usedPorts` parameter from `connect()` entirely.

### F-244 · S4 · `Wizard/app.js:6485`

**boardManualConnect, boardAutoDetect and the entire port-picker modal are unreachable dead code**

*Category:* `dead-code`

`grep -rn` across the whole Wizard directory (app.js, index.html, parser.js, flasher.js, serial-hub.js, device-labels.js, watch-version.js) finds no caller for any of these:

- `boardManualConnect` (app.js:6485) — 0 references outside its own definition.
- `boardAutoDetect` (app.js:6524) — 0 references; the only textual hit is a comment at app.js:7614 ("e.g. boardAutoDetect timing out").
- `showPortPickerModal` (app.js:6254) — called only from boardManualConnect (app.js:6505).
- `portPickerDetect` / `portPickerCancelDetect` / `portPickerSelect` (app.js:6251, 6252, 6469) — referenced only from the HTML that showPortPickerModal itself generates (app.js:6308, 6311, 6316).
- `_portPickerPorts` / `_portPickerResolve` / `_portPickerStopMonitors` / `_portPickerDetectFn` / `_portPickerCancelDetectFn` / `PORT_VENDOR_NAMES` / `portVendorLabel` — all confined to that block.

The live connect flow is `boardConnect(n)` (index.html:428) -> `openConnectModal(n)` (app.js:5810), whose options are `modalAutoDetect()` -> `wizPortDetect(n)`, `modalManualSelect()`, `modalAuthorize()`, `modalRemoteConnect()` and `modalSharedConnect()` — none of which touch the port-picker code.

That is roughly app.js:6222-6685 (~460 lines) plus the `port-picker-modal` markup at index.html:322-331, whose only reachable behaviour is `closePortPickerModal()` closing a modal that is never opened. This matters beyond tidiness on a build-step-free tool: it is a second, divergent implementation of port selection and detect (its own DTR handling, its own Windows already-open fallback at app.js:6363-6372, its own 20 s reset timeout) sitting next to the one that actually ships, so a future fix applied here would appear to work and change nothing.

**Failure scenario.** A maintainer traces a port-selection or reset-detect bug to showPortPickerModal/startDetect (it is the most complete-looking implementation in the file), fixes it there, sees no change in the browser, and re-debugs — the real path is wizPortDetect at app.js:11228. Equally, `closePortPickerModal` remains wired from index.html:322/326/330 to a modal no code opens.

**Suggested fix.** Delete app.js:6222-6685 (the PORT_VENDOR_NAMES table, portVendorLabel, the _portPicker* state, portPickerDetect/portPickerCancelDetect, showPortPickerModal, portPickerSelect, closePortPickerModal, boardManualConnect, boardAutoDetect) and the `port-picker-modal` block in index.html — or, if the picker is meant to ship, wire it into openConnectModal as an explicit option. Either way it should not stay as a parallel unreferenced implementation.

### F-245 · S4 · `Wizard/app.js:6524`

**boardAutoDetect() is dead code — ~150 lines with its own setInterval, readers and open ports**

*Category:* `dead-code`

`async function boardAutoDetect(n)` spans app.js:6524-6673 and is never invoked. I grepped both `app.js` and `index.html` for the identifier: the only hits are the definition at app.js:6524 and a stale comment at app.js:7614 ("this guard only fires for unintended callers (e.g. boardAutoDetect timing out)"). It is not wired to any inline `onclick`/`onchange` handler — I verified all 81 inline handlers in index.html and all 64 handlers generated from app.js template strings resolve to live functions, and none of them is this one.

It carries a full, independent port-monitoring implementation that duplicates and contradicts the live `wizPortDetect` path (app.js:11229): its own `setInterval(..., 300)` cancel poller (app.js:6653), a 45 s `setTimeout` (app.js:6664), an `openReaders` Map, a `Promise.all` bulk-connect (app.js:6579) and a `monitorPort` fire-and-forget loop (app.js:6672). None of that machinery is reachable, but a future edit to "the detect path" could easily land here and appear to do nothing.

**Failure scenario.** Not user-visible today. A maintainer chasing an auto-detect bug greps for "auto-detect", lands in `boardAutoDetect`, fixes the reset-monitor logic there, verifies it compiles, ships it — and the bug is unchanged because the live path is `wizPortDetect`/`wizPortComplete` (app.js:11229/11570). The stale reference at app.js:7614 actively reinforces the belief that this function still runs.

**Suggested fix.** Delete `boardAutoDetect` (app.js:6524-6673) and update the comment at app.js:7614 to name the real caller class ("e.g. a detect job timing out"). If the older bulk first-time-connect behaviour at app.js:6569-6592 is still wanted, lift just that block into `wizPortDetect` rather than keeping the whole function alive as an unreachable duplicate.

### F-246 · S4 · `Wizard/app.js:8938`

**Dead Kyber-targets table: five functions still drive a #b<n>-kyber-target-tbody element that no longer exists in index.html**

*Category:* `dead-code`

`addKyberTargetRow` (Wizard/app.js:8938), `appendKyberTargetRow` (:8944), `removeKyberTargetRow` (:8988), `onKyberTargetsChange` (:8998) and `syncKyberTargetsToConfig` (:9003) form a closed, unreachable cluster. `addKyberTargetRow` is the only entry point and has **zero** callers — no call in Wizard/app.js and no `onclick` in Wizard/index.html (verified by grepping both files for each name). The other four are reachable only from `addKyberTargetRow` or from markup that only `appendKyberTargetRow` generates.

They all operate on `document.getElementById(\`b${n}-kyber-target-tbody\`)` (Wizard/app.js:8945, :8990, :9008), and that element is not in the template: Wizard/index.html contains only `b{N}-kyber-targets-wrap` (line 661) and `b{N}-kyber-targets-info` (line 663). The manual table was replaced by the auto-computed info div rendered by `populateKyberTargetsFromConfig` (Wizard/app.js:9019-9036, whose own comment says "The Kyber targets info div is auto-computed … we always scan boardConfigs[] for live accuracy") and the target list itself is derived by `autoComputeKyberTargets` (Wizard/app.js:9042).

The cluster also carries a live landmine if it is ever re-wired: `syncKyberTargetsToConfig` wipes the target list *before* its null-guard —

```js
config.kyber.targets = [];                                          // :9007
const tbody = document.getElementById(`b${n}-kyber-target-tbody`);  // :9008
if (!tbody) return;                                                 // :9009
```

— so any future caller reached with the tbody absent destroys the auto-computed targets rather than leaving them alone.

**Failure scenario.** No runtime failure today (the code is unreachable). The cost is maintenance: a reader tracing Kyber target handling finds two competing implementations — a manual table and the auto-computed one — and cannot tell from the source which is live. If someone re-adds an "Add target" button wired to `addKyberTargetRow`, `appendKyberTargetRow` returns silently at line 8946 (no tbody) while `syncKyberTargetsToConfig` at line 8940 has already emptied `config.kyber.targets`, so the very next push emits `?KYBER,LOCAL,S2` with no targets and the Kyber board stops driving every Maestro in the droid.

**Suggested fix.** Delete Wizard/app.js:8937-9017 (`addKyberTargetRow` through `syncKyberTargetsToConfig`) along with the now-unused `BAUD_RATES`/`_rowIdCounter` uses in that block. If the manual table is intended to come back, at minimum move the `config.kyber.targets = []` assignment (line 9007) below the `if (!tbody) return;` guard so a missing table is a no-op rather than a wipe.

### F-247 · S4 · `Wizard/app.js:10187`

**ETM step hint text states three default values that contradict what the Wizard actually fills in**

*Category:* `logic`

`wizardHTMLEtm` renders each field's value from `wizardState.etmConfig` while its `wizHint` tooltip quotes a different number:

- Timeout: `<input id="wiz-etm-timeout" … value="${etmConfig.timeoutMs}">` shows **500**, hint says "Default: 250 ms." (:10187-10188)
- Heartbeat: shows **10**, hint says "Default: 5 sec." (:10191-10192)
- Boot heartbeat: shows **2**, hint says "Default: 30 sec." (:10199-10200)

The field values are the correct ones — they match the firmware (`etmTimeoutMs = 500`, `etmHeartbeatSec = 10` at Code/WCB/WCB.ino:266-268; `etmBootHeartbeatSec` default 2 at Code/WCB/WCB_Storage.cpp:2227). Only the hints are stale. "Missed before action" is the inverse case: its hint says "Default: 3" and the field does show 3, but the firmware default is 5 (see the separate finding).

**Failure scenario.** A user opening the ⓘ on Timeout reads "Default: 250 ms", sees 500 in the box, assumes the wizard has pre-loaded a non-default value and "corrects" it to 250 — halving the ETM retry window across the whole fleet based on wrong help text. Same for Heartbeat (10 → 5) and Boot heartbeat (2 → 30).

**Suggested fix.** Update the three tooltip strings to the real defaults — 500 ms, 10 sec, 2 sec — and keep them sourced from the same constants the fields render, so a future firmware default change touches one place.

### F-248 · S4 · `Wizard/app.js:10576`

**Dead Kyber-target and port-picker code, plus tip text advertising a "Use →" button that no longer exists**

*Category:* `dead-code`

Several wizard entry points have no remaining callers (verified by grepping both Wizard/app.js and Wizard/index.html):

- `wizardAddKyberTarget()` (:10576) and `wizardRemoveKyberTarget(ti)` (:10580) — the only references to `wizardState.kyberTargets` besides its declaration at :9298. `wizardHTMLKyberConfig` (:9995-10058) renders no targets table and no "+ Add target" button, and `wizardSaveStep('kyber-config')` (:10736-10741) never reads or writes `kyberTargets`. The array stays `[]` for the life of the wizard; Kyber targets are instead derived from `ws.maestros` at apply time (:10956-10962).
- `wizPortUse(n, portIdx)` (:11518) — never called. `wizPortRenderPanel` (:11171-11215) only ever emits Detect / + Authorize / Select by COM# / ✕ Cancel. The connect-step help text still tells users "Or click <strong>Use →</strong> to pick a port directly." (:10372), naming a control that is not rendered anywhere.
- `wizardInitBoards` filters `wizardState.kybers` (:9351-9352), a property `wizardDefaultState()` never creates and nothing else references — the `Array.isArray` guard makes it a permanent no-op.

**Failure scenario.** A user reads the "💡 How to connect & authorize ports" panel on the Connect step, looks for the promised "Use →" button next to each row to pick a port directly, and cannot find it — the row only offers 🔍 Detect, + Authorize… and Select by COM#…. Separately, a maintainer reading `wizardState.kyberTargets` / `wizardState.kybers` reasonably assumes Kyber targets are user-editable in the wizard and edits the wrong code path.

**Suggested fix.** Delete `wizardAddKyberTarget`, `wizardRemoveKyberTarget`, the `kyberTargets` state field and the `wizardState.kybers` filter block; delete `wizPortUse` (or wire it back into `wizPortRenderPanel`); and remove the "Or click Use → to pick a port directly." sentence from the connect tip at :10372.

### F-249 · S4 · `Wizard/app.js:11518`

**Six unreachable functions left in app.js from removed UI (manual Kyber targets, per-port wizard list)**

*Category:* `dead-code`

Each of these has exactly one whole-word occurrence across app.js, parser.js, flasher.js, serial-hub.js, device-labels.js, index.html, watch-version.js and serial-hub-test.html — its own definition. None is reachable from an inline handler either; I extracted every identifier invoked from `on*=` attributes in index.html and from app.js template literals and none of these appears.

- `wizPortUse(n, portIdx)` app.js:11518 — takes an index into `navigator.serial.getPorts()` and calls `wizPortComplete`. It is a click handler for a per-port list that `wizPortRenderPanel` (app.js:11448) no longer renders; that function now emits only Detect / Authorize / Select-by-COM# buttons.
- `wizardDisableConnectBtns(n)` app.js:11642 — its counterpart `wizardEnableConnectBtns` (app.js:11648) is live with four call sites (11594, 11671, 11704, 11755); the disable half is not called anywhere.
- `addKyberTargetRow(n)` app.js:8938, `wizardAddKyberTarget()` app.js:10576, `wizardRemoveKyberTarget(ti)` app.js:10580 — leftovers from the manual Kyber-target editor. Targets are now derived automatically by `autoComputeKyberTargets` (app.js:9040) and rendered read-only by `populateKyberTargetsFromConfig` (app.js:9019).
- `boardManualConnect(n)` app.js:6485 — superseded by the connect-modal flow (`modalManualSelect` / `wizPortComPicker`, both live from index.html).

**Failure scenario.** No runtime failure — none of these can execute. The cost is that `wizPortUse` and `boardManualConnect` both read as live connection paths when tracing how a board gets connected, which is the most intricate area of the tool, and `addKyberTargetRow` implies a manual-edit path for Kyber targets that no longer exists.

**Suggested fix.** Delete all six. If `wizardDisableConnectBtns` is wanted for symmetry with `wizardEnableConnectBtns`, wire it into the connect path instead of leaving it uncalled.

### F-250 · S4 · `Wizard/flasher.js:194`

**`pickBinaryFile` is dead code and its comment claims a fallback that does not exist**

*Category:* `dead-code`

`Wizard/flasher.js:193` says "File picker fallback — only used if GitHub fetch fails", but `getBinaryData`'s catch at `:186-190` unconditionally rethrows (`throw new Error(\`GitHub fetch failed: ${e.message}\`)`) and `flashFirmware:333-337` rethrows again — there is no fallback. Grepping the whole Wizard for `pickBinaryFile` finds only the definition at `Wizard/flasher.js:194`; no call site in `app.js`, `parser.js`, `serial-hub.js`, or `index.html`. The function's error text at `:336` ("'No file selected' or fetch error") is the leftover of the same removed path.

It also carries a latent bug if it were ever revived: `document.body.removeChild(input)` runs synchronously at `:220`, immediately after `input.click()` and long before the user picks a file, detaching the element the `change`/`cancel` listeners are attached to.

**Failure scenario.** No runtime failure — the code cannot execute. The cost is the misleading comment: a maintainer reading `:193` and `:336` will believe a manual .bin fallback exists when GitHub is unreachable, and will not add one; a user offline or rate-limited simply cannot flash.

**Suggested fix.** Delete `pickBinaryFile` (`:193-222`) and fix the stale comment at `:336`, or actually wire it in as the documented fallback in `flashFirmware`'s catch — in which case also move `document.body.removeChild(input)` into the `change`/`cancel` handlers.

### F-251 · S4 · `Wizard/flasher.js:301`

**NVS size in the `flashFirmware` doc comment is wrong (24 KB vs the actual 20 KB) and 'prepend' misdescribes the code**

*Category:* `dead-code`

The parameter doc says:

```js
// eraseNvs — if true, prepend a blank (0xFF) image at the NVS partition address
//            (0x9000, 24 KB) so esptool erases+rewrites it, producing a factory-fresh NVS
```

Both details are wrong against the code directly below and against the shipped partition tables. The implementation at `:632-635` allocates `new ArrayBuffer(0x5000)` and logs "NVS (0x9000, 20 KB)" — 0x5000 is 20480 bytes, not 24 KB. I dumped `Wizard/bin/WCB_ESP32_part.bin` and `Wizard/bin/WCB_ESP32S3_part.bin`: both have `nvs off=0x9000 size=0x5000`, confirming 20 KB, matching the correct comment block at `:613`. Also the image is *appended* (`imagesToFlash = [...imagesToFlash, {...}]` at `:634`) and then position is decided by the sort at `:642`, not prepended.

**Failure scenario.** No runtime failure. A maintainer sizing or auditing the factory-reset erase from the function's own doc comment gets 24 KB, which would overlap otadata at 0xE000 (0x9000 + 0x6000 = 0xF000) if acted on — precisely the kind of off-by-a-partition mistake the detailed layout comment at `:611-626` was written to prevent.

**Suggested fix.** Change `:301` to `(0x9000, 20 KB / 0x5000)` and `prepend` to `append` so the doc matches `:632-635` and the partition tables.

### F-252 · S4 · `Wizard/index.html:209`

**'edit Config Tool URL' link uses an inline var(--text-secondary) with no fallback — the custom property is never defined, so the declaration is dropped and the link renders in the browser's default link colour**

*Category:* `dom`

`<a href="#" id="rc-tool-url-edit" style="color:var(--text-secondary)">edit Config Tool URL</a>` (index.html:209).

`--text-secondary` is not defined anywhere in the project — the only custom-property definitions are the 25 tokens in `:root` (styles.css:4-27) and the 18 overrides in `:root.light` (styles.css:29-48), plus per-board `--bc`/`--bc-dim` (styles.css:254-303) and the inline `--rail` values. The name appears nine other times in styles.css (1422, 1425, 1436, 1439, 1440, 1444, 1463, …) and every one of those supplies a fallback: `var(--text-secondary, #9aa3b0)`. This one does not.

A `var()` reference to an undefined property with no fallback makes the declaration invalid at computed-value time, so `color` resolves to `unset` → inherit for an `<a>`… except the UA stylesheet's `a:-webkit-any-link { color: -webkit-link }` still applies, and styles.css declares no global `a { color: … }` rule (the only anchor rule is `.rc-devices-note a { text-decoration: underline; cursor: pointer; }`, styles.css:1315). The link therefore paints in Chrome's default link blue / visited purple instead of the muted secondary grey the author intended, and that colour is not theme-aware.

**Failure scenario.** Load the Wizard and scroll to the RC Controllers footnote. The 'edit Config Tool URL' link renders in default browser blue (purple once clicked) rather than the intended muted grey, on both the dark and light themes — the one element on the page whose colour ignores the theme entirely.

**Suggested fix.** Change the inline style to an existing token: `style="color:var(--text2)"` (or `var(--text3)` to match `.rc-devices-note`), or add the `#9aa3b0` fallback the rest of styles.css uses: `color:var(--text-secondary, #9aa3b0)`. Better still, define `--text-secondary` once in `:root`/`:root.light` and drop the fallbacks — the name is used ten times and defined zero.

### F-253 · S4 · `Wizard/index.html:421`

**b{N}-fw-version span in the board header is dead markup — no code ever writes it, so it is permanently empty**

*Category:* `dead-code`

`<span class="fw-version" id="b{N}-fw-version"></span>` sits in the board card's `.section-status` block (index.html:421), between the alias heading and the connection badge, and starts with no text content.

Nothing ever fills it. Grepping `fw-version` across app.js, parser.js, flasher.js and serial-hub.js returns zero hits — the only other occurrence in the whole Wizard is the styling rule `.fw-version { font-size: 11px; color: var(--text3); }` (styles.css:1110). There is no `getElementById('b…-fw-version')`, no `querySelector('.fw-version')`, and no `.textContent`/`innerHTML` assignment reaching it.

The firmware version is actually rendered into a different element: `b{N}-sw-version` (index.html:576), written by `updateBoardSwVersionDisplay()` via `document.getElementById(`b${n}-sw-version`)` at app.js:200 and app.js:253. So `b{N}-fw-version` is a leftover from an earlier header layout — it is cloned once per board section by `addBoardSection()` (app.js:597-599, which does `template.innerHTML.replace(/\{N\}/g, n)`), producing one dead, permanently-empty span per WCB card.

Harmless at runtime, but it is a trap: it looks like the header's firmware-version slot, so the next person adding a version indicator to the card header will wire it up and find the value never appears where they expect (the header) because the real display lives in the Board Identity subsection.

**Failure scenario.** A maintainer wants the running firmware version shown next to the connection badge in the collapsed board header. They find `id="b{N}-fw-version"` sitting exactly there, assume it is already wired, and can't work out why it stays blank — the code that fetches the version writes to `b{N}-sw-version` instead (app.js:200, app.js:253).

**Suggested fix.** Delete the span from the board template, or wire it up in `updateBoardSwVersionDisplay()` (app.js:196-260) alongside the `b{N}-sw-version` write so the header shows the version too.

### F-254 · S4 · `Wizard/parser.js:1231`

**Five exported parser APIs and two internal helpers are dead code**

*Category:* `dead-code`

`buildCommandString` defines a diff helper that is never called — `function changed(key, value) { … return getNestedValue(baseline, key) !== value; }` (:1231-1234); every comparison in the function body is written inline instead. `getNestedValue` (:1666) exists solely to serve it.

The export surface at :1678-1705 also ships five functions that nothing in the Wizard references. Grepping every `.js`/`.html` in Wizard/ for `WCBParser.<name>` yields hits only for `HW_VERSION_MAP`, `createDefaultBoardConfig`, `createDefaultSystemConfig`, `parseBackupString`, `parseSystemFile`, `buildCommandString`, `buildSystemFile` and `evaluatePortClaims` — `diffConfigs` (:1618), `collectAllMaestros` (:1645), `getAvailablePorts` (:1657), `hwValueToDisplay` (:205) and `hwValueToBinary` (:209) have zero call sites (flasher.js:327 and app.js:32/:9685 use `WCBParser.HW_VERSION_MAP` directly).

Worth noting because `diffConfigs` reads as the tool's change-detector but has drifted badly: it never checks `dfp`, `alias`, `statusLedPin`, `specialPeer/specialPeerId` or `wledRemotes`. If it is ever wired up as-is, DFPlayer and alias edits would not register as changes.

**Failure scenario.** No runtime failure today. The hazard is maintenance: a future change wires `diffConfigs` into the 'unsaved changes' indicator and DFPlayer/alias edits are reported as 'no changes', or someone edits the unused `changed()` helper believing it governs the diff push.

**Suggested fix.** Delete `changed()`/`getNestedValue()` and the unused exports, or — if `diffConfigs` is meant to be used — add the missing `dfp`, `alias`, `statusLedPin` and `specialPeer/specialPeerId` checks and actually call it.

### F-255 · S4 · `Wizard/parser.js:1595`

**Five exported parser.js helpers are entirely unused, and the dead diffConfigs has already drifted (no dfp)**

*Category:* `dead-code`

`diffConfigs` (parser.js:1595), `collectAllMaestros` (parser.js:1642), `getAvailablePorts` (parser.js:1656), `hwValueToDisplay` (parser.js:205) and `hwValueToBinary` (parser.js:209) are each referenced exactly three times across the whole Wizard: their own definition plus the two export lists at parser.js:1675-1689 and parser.js:1692-1706. `grep -c 'WCBParser\.<name>'` over app.js and index.html returns 0 for all five, and none is called internally within parser.js.

That the dead code is unmaintained is demonstrable: `diffConfigs` checks `mp3` (parser.js:1615), `hcr` (:1616) and `wleds` (:1617) but has no `check('dfp', …)`, even though DFPlayer support was added to `createDefaultBoardConfig` (parser.js:92), `parseCommand` (parser.js:702), `evaluatePortClaims` (parser.js:1021) and `buildCommandString` (parser.js:1394-1405). The DFPlayer commit updated every live path and missed the dead one — with no runtime symptom, precisely because nothing calls it.

The export block's header comment at parser.js:1671 reads "Exports — available to app.js and parser.test.js", but no `parser.test.js` (or any `*.test.js`) exists anywhere in the repository, so the Node branch at parser.js:1673 is unreachable in practice.

**Failure scenario.** No runtime failure — this is dead weight. The concrete cost is review risk: a future maintainer adding a device follows the pattern in diffConfigs, or trusts its field list as the authoritative set of per-board config keys, and inherits the missing dfp check into code that does run. The stale parser.test.js reference also implies a test suite that a cold session would waste time looking for.

**Suggested fix.** Delete `diffConfigs`, `collectAllMaestros`, `getAvailablePorts`, `hwValueToDisplay` and `hwValueToBinary` along with their entries in both export lists (parser.js:1682-1684, 1687-1688 and 1699-1701, 1704-1705). If any is being kept deliberately for a planned test suite, add `check('dfp', configA.dfp, configB.dfp);` to diffConfigs and correct the parser.js:1671 comment to note that parser.test.js does not exist yet.

### F-256 · S4 · `Wizard/watch-version.js:43`

**watch-version.js reports 'No UI_VERSION pattern found in app.js' whenever two stamps land in the same minute, because formatVersion() has only minute resolution**

*Category:* `logic`

`formatVersion()` (watch-version.js:26-31) builds the stamp from day, hour and minute only:
```js
return `${pad(now.getDate())}.${pad(now.getHours())}:${pad(now.getMinutes())}.R.${months[now.getMonth()]}.${now.getFullYear()}`;
```
No seconds. (Confirmed against the current value in app.js:77: `const UI_VERSION = '17.19:54.R.AUG.2026';`.)

`stampVersion()` (watch-version.js:33-50) then uses string equality as its 'did the regex match?' test:
```js
const next = src.replace(/const UI_VERSION = '[^']*';/, `const UI_VERSION = '${version}';`);
if (next === src) {
  console.log(`[watch-version] No UI_VERSION pattern found in app.js`);
  return;
}
```
`next === src` is true in two distinct situations: (a) the regex genuinely found nothing, and (b) the regex matched but produced the identical string because the minute has not ticked over. Case (b) is the common one — the watcher debounces at only 150 ms (watch-version.js:53-56), so any two saves within the same wall-clock minute hit it, and so does the very first `stampVersion()` call at startup (watch-version.js:59) when app.js was last stamped in the current minute.

The early `return` is correct behaviour (nothing to write), but the message is a false alarm that points the developer at a non-existent problem — 'the stamper can't find UI_VERSION in app.js' reads as a broken regex or a renamed constant.

**Failure scenario.** Developer runs `node watch-version.js`, then saves index.html twice while polishing a tooltip. The first save stamps '18.09:15.R.AUG.2026' and logs the normal 'UI_VERSION →' line. The second save, twenty seconds later, produces the identical string, so `next === src` and the console prints '[watch-version] No UI_VERSION pattern found in app.js'. The developer stops to check whether `const UI_VERSION = '…';` still exists in app.js — it does, and the stamper was working correctly the whole time.

**Suggested fix.** Separate the two cases: test for the match explicitly before replacing, e.g. `const re = /const UI_VERSION = '[^']*';/; if (!re.test(src)) { console.log('[watch-version] No UI_VERSION pattern found in app.js'); return; } const next = src.replace(re, …); if (next === src) return;  // same minute — nothing to write`. Optionally add seconds to `formatVersion()` so consecutive saves produce distinct stamps.
