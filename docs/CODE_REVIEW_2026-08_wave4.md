# WCB Code Review — Wave 4 findings (cross-cutting sweeps, CI, cross-repo)

Companion to [CODE_REVIEW_2026-08.md](CODE_REVIEW_2026-08.md). Generated 2026-08-18.
**10/10 sweeps completed.** Sweeps: legacy 8/9 board caps · NVS 15-char keys ·
config round-trip completeness · command-verb collisions · help coverage · docs drift ·
memory/stack/heap · CI + release pipeline · the WDP test itself · cross-repo consistency.

> **STATUS: ALL UNVERIFIED** — reviewers-only run by design.

**Totals:** 81 findings — S1: 1 · S2: 29 · S3: 35 · S4: 16


---

## S1

### F-363 · S1 · `Code/WCB/command_timer.cpp:203`

**Use-after-free: processCommandGroups() holds references into commandGroups across a vTaskDelay() that serialCommandTask can clear**

*Category:* `memory-safety`

`processCommandGroups()` runs on the Arduino loopTask (called at `WCB.ino:7481`). It binds a reference into the vector's heap storage and then iterates a reference into a nested heap container:

```
command_timer.cpp:187   CommandGroup &group = commandGroups[currentGroupIndex];
command_timer.cpp:203   for (const String &cmd : group.commands) {
command_timer.cpp:206     parseCommandsAndEnqueue(cmd, 0);
command_timer.cpp:208     vTaskDelay(pdMS_TO_TICKS(1)); // ← Give queue time to breathe
command_timer.cpp:209   }
```

The `vTaskDelay` on line 208 is an explicit yield executed *inside* the range-for, while `group` and `cmd` are live.

`serialCommandTask` is a SEPARATE FreeRTOS task (`WCB.ino:7439`, `xTaskCreatePinnedToCore(serialCommandTask, "Serial Command Task", 4096, NULL, 1, NULL, 1)` — core 1, priority 1, i.e. the same core and priority as loopTask). Its call chain `processIncomingSerial` (`WCB.ino:6411`) → `processSerialCommandHelper` reaches TWO writers of that same vector:

- `WCB.ino:6430-6431` — `if (checkForTimerStopRequest(data)) { stopTimerSequence(); }` → `command_timer.cpp:54` `commandGroups.clear();`
- `WCB.ino:6490` — `parseCommandGroups(data);` → `command_timer.cpp:85` `commandGroups.clear();` then `push_back` at `:123`/`:177`

`clear()` destroys every `CommandGroup`, which destroys the inner `std::vector<String> commands` and frees every `String` buffer. When loop() resumes from the `vTaskDelay`, the range-for's iterators point into the freed inner vector and `cmd` names a destroyed `String`. `parseCommandsAndEnqueue(cmd, 0)` on the next iteration then reads freed heap. The `parseCommandGroups` variant is worse: after `clear()` it `push_back`s into the retained outer buffer, so `group` now aliases a *different* group while the loop still walks the old group's stale begin/end.

A secondary observable symptom on the `parseCommandGroups` path: it resets `currentGroupIndex = 0` (`command_timer.cpp:86`), and the stale loop then runs `currentGroupIndex++` (`command_timer.cpp:213`), so the newly-parsed sequence silently skips its first group.

The comment that authorises this is factually wrong about the code — `WCB.ino:398-399`:

```
// The local-serial entry point and SEQ playback both run inside loop()
// already, so they keep calling parseCommandGroups() directly.
```

SEQ playback is correct (`WCB_Storage.cpp:615` is reached from the loop() queue drain at `WCB.ino:7501-7510`), but the local-serial entry point does NOT run inside loop() — it runs in `serialCommandTask`. The `pendingTimerChainQueue` was built (`WCB.ino:389-393`) precisely to keep `parseCommandGroups` off a task that races loop(); the ESP-NOW callback was routed through it (`WCB.ino:3992-3993`, `:4223`) but the serial task was not.

**Failure scenario.** A timer sequence with more than one command in a group is running — e.g. `;t1000,cmd1^cmd2^cmd3^;t1000^cmd4`. loop() enters `processCommandGroups`, binds `group` to group 0, and begins the range-for; after dispatching `cmd1` it hits `vTaskDelay(pdMS_TO_TICKS(1))` at command_timer.cpp:208 and yields. Within that 1 ms window the operator types `?STOP` on USB (the documented way to stop a timer sequence) or a new `;t…` chain arrives on any of Serial1-5. serialCommandTask runs `stopTimerSequence()` → `commandGroups.clear()`, freeing every String buffer and the inner vector storage. loop() resumes and evaluates `cmd` — a destroyed String — then calls `parseCommandsAndEnqueue(cmd, 0)` on freed heap. Result is either a LoadProhibited panic, or a garbage command string being enqueued and executed on the mesh, depending on what the allocator has reused the block for.

**Suggested fix.** Route the serial entry point through the same deferral the ESP-NOW callback already uses: at WCB.ino:6490 call `enqueuePendingTimerChain(data)` instead of `parseCommandGroups(data)`, and at WCB.ino:6430-6431 defer the `?STOP` likewise (or set a `stopRequested` flag that loop() honours) so every mutation of `commandGroups` happens on the loop task. Independently, harden `processCommandGroups` to copy the group it is about to run (`CommandGroup group = commandGroups[currentGroupIndex];`) rather than holding a reference across the yield, and re-validate `currentGroupIndex` against `commandGroups.size()` after each `vTaskDelay`. Fix the comment at WCB.ino:398-399, which currently states the opposite of what the code does.


---

## S2

### F-364 · S2 · `c:/Users/ghulette/Documents/GitHub/WCBClient/src/WCB_Client.cpp:2007`

**WCB_Client decodes a WDP SOLICIT as an advert, wiping the sending board's neighbor record**

*Category:* `wire-format-divergence`

The WCB firmware added TLV `WDP_TLV_SOLICIT 0x11` (Code/WCB/WCB_WDP.cpp:87) and guards it with an explicit pre-pass BEFORE the advert decode. WCB_WDP.cpp:494-506:

```
  // Solicitation check FIRST — a bare SOLICIT carries no facts, so decoding it as an
  // advert (memset + rebuild below) would erase the sender's real neighbor record. If
  // this packet contains a SOLICIT TLV, arm a prompt advert and return without touching
  // the table.
  {
    int s = 2;
    while (s + 2 <= 200) {
      uint8_t ty = cmd[s];
      if (ty == WDP_TLV_END) break;
      int ln = cmd[s + 1];
      if (s + 2 + ln > 200) break;
      if (ty == WDP_TLV_SOLICIT) { wdpArmSolicitedAdvert(); return; }
      s += 2 + ln;
    }
  }
```

WCB_Client — the other implementer of this shared wire format — has NO such guard and no knowledge of 0x11 at all. I grepped the entire library for `0x11`/`SOLICIT` across `src/WCB_Client.cpp` and `src/WCB_Client.h`: zero hits. `_handleWdpAdvert` (WCB_Client.cpp:2002) goes straight to the wholesale replace at WCB_Client.cpp:2007:

```
    WCBNeighbor& nb = _neighbors[senderWCB - 1];
    memset(&nb, 0, sizeof(nb));            // a board is the sole authority for its facts — replace wholesale
    nb.valid      = true;
```

The solicit packet passes every gate: `wdpSendSolicit()` (WCB_WDP.cpp:332-341) writes magic `'W'` + proto `0x01` + `[0x11][0x00]` + END and hands it to `wdpBroadcast()` (WCB.ino:968-981), which sends a full `espnow_struct_message_etm` with `structPacketType = PACKET_TYPE_WDP` and `structCommandIncluded = 1` to the broadcast MAC. The client routes it at WCB_Client.cpp:2695-2697 (`case WCB_PACKET_WDP`, which is 12, matching PACKET_TYPE_WDP at WCB.ino:222) into `_handleWdpAdvert`. Magic/proto match (WCB_Client.cpp:1819-1820 define `'W'`/`0x01`, identical to WCB_WDP.cpp:66 and WCB_WDP.h:29), so the `return` at WCB_Client.cpp:2003 does not fire. TLV 0x11 then falls to `default: break;` (WCB_Client.cpp:2059) and is skipped by length, so the record is left fully zeroed.

The wipe is not cosmetic — it clears `name`, `fw`, `hwVer`, `hwRev`, `capFlags`, `capTags`, `ctrlId`, `maestroIds`/`maestroCount`, `portLabels` and `seqHash`. It then increments `_advertCount` and can set `_pendingJoin` (WCB_Client.cpp:2086-2090) on a packet that is not an advert, and finally fires the consumer callback with the blank record: `if (_neighborCallback) _neighborCallback(nb);` (WCB_Client.cpp:2092).

docs/WDP_DESIGN.md:95 states the contract the client violates: "`0x11` | SOLICIT | len 0 — a bare 'advertise now' request (`?WDP,POLL`); carries no facts, receivers reply with a jittered advert and **never record it**".

Recovery is slow because the soliciting board does not answer its own solicit (`wdpArmSolicitedAdvert` is only reachable from the receive path, WCB_WDP.cpp:503), and `?WDP,POLL` sends its own advert BEFORE the solicit (WCB_WDP.cpp:1213-1214), so the good advert is immediately overwritten by the wipe. The entry stays blank until that board's next periodic backstop — `WDP_PERIOD_MS = 60000UL` (WCB_WDP.cpp:52).

Downstream, NaviCore reports `nb->seqHash` at rc_telemetry.h:1943; a wiped record yields 0, which docs/WDP_DESIGN.md:97 and WCB_WDP.h define as "firmware predates the TLV / unknown", so the consumer's re-pull decision is also corrupted.

**Failure scenario.** Operator clicks "Poll mesh" in the Wizard (Wizard/app.js:12395, handler `wdpPollMesh` at Wizard/app.js:12514-12518), which sends `?WDP,POLL`. The connected WCB broadcasts its advert then a SOLICIT. Every WCB_Client on the mesh (e.g. NaviCore) receives the SOLICIT, zeroes that board's `_neighbors[]` entry, and fires `onNeighbor` with an all-blank record — the board's alias, firmware, capabilities, Maestro IDs, port labels and seqHash all vanish from NaviCore for up to 60 seconds, until the board's next periodic advert. The one action intended to refresh the mesh is the action that blanks it.

**Suggested fix.** Port the firmware's pre-pass into WCB_Client::_handleWdpAdvert. Add `#define WCB_WDP_TLV_SOLICIT 0x11` beside the other TLV defines (near WCB_Client.cpp:1826) and, immediately after the magic/proto check at WCB_Client.cpp:2003 and BEFORE the `memset` at :2007, scan the TLV chain for 0x11 and `return` without touching `_neighbors[]`, `_advertCount`, `_pendingJoin` or the callback — mirroring WCB_WDP.cpp:494-506. This is a receive-side-only change and does not alter bytes on the wire, so it is safe against older peers.

### F-365 · S2 · `c:/Users/ghulette/Documents/GitHub/Wireless_Communication_Board-WCB/Code/WCB/WCB_Storage.cpp:643`

**?SEQ,SAVE accepts a key longer than the 15-char NVS limit — value silently discarded but the name is recorded**

*Category:* `data-loss`

`saveStoredCommandsToPreferences()` is the single write path for stored sequences (reached from `?SEQ,SAVE` at WCB.ino:4994-4995, and from the Wizard and WCB_Client alike). Its only validation is emptiness — WCB_Storage.cpp:637-640:
```
  if (key.length() == 0 || value.length() == 0) {
    Serial.println("Key or value cannot be empty.");
    return;
  }
```
There is no length check. It then writes the value and, separately, appends the name to the index — WCB_Storage.cpp:642-659:
```
  preferences.begin("stored_cmds", false);
  preferences.putString(key.c_str(), value);

  String existingKeys = preferences.getString("key_list", "");
  ...
  if (!alreadyExists) {
    existingKeys += key + ",";
    preferences.putString("key_list", existingKeys);
  }
```
The return value of `putString` at :643 is discarded. On ESP32 NVS a key over 15 characters cannot be stored, so `putString` fails and writes nothing — but line 657 still appends the name to `key_list`, and line 663 prints `Stored: Key='...', Value='...'` as if it succeeded.

Every *other* participant in this same contract enforces the limit, which is what makes the omission visible only across files:
- WCB.ino:3492 — `if (key.length() == 0 || key.length() > 15) {   // 15 = the NVS key limit` (the `?MGMT,SEQGET` relay path).
- WCBClient/src/WCB_Client.cpp:1209-1214 — `wcbSeqKeyValid()` rejects `n > WCB_SEQ_KEY_MAX`, where WCB_Client.h:223 defines `#define WCB_SEQ_KEY_MAX 15 // NVS key limit — a longer key cannot be stored at all`. Both `saveSequence` (WCB_Client.cpp:1320) and `deleteSequence` (WCB_Client.cpp:1334) gate on it.
- The firmware's own `espnow_struct_seqval_req` reserves `char key[16]` (WCB.ino:820) — "15 chars = the NVS key limit".
- Wizard/app.js:4220 sets `maxlength="15"` on the key input (browser-side only).

The corruption is durable and propagates: `buildSequenceNamesString()` (WCB_Storage.cpp:798-816) builds the inventory purely from `key_list`, so the phantom name is reported by `?SEQ,NAMES`, by the `PACKET_TYPE_SEQ_FRAG` mesh reply, and in the config pull. `invalidateSequenceInventoryHash()` (WCB_Storage.cpp:662) moves the WDP `SEQHASH` TLV, so every consumer caching on that fingerprint (docs/WDP_DESIGN.md:97) re-pulls and receives a name it can never resolve — `?SEQ,GET,<key>` returns `NOTFOUND` (WCB.ino:4976-4977) because `preferences.isKey()` is false.

**Failure scenario.** A user names a sequence `DomePanelOpen1` (14 chars — fine) and later `DomePanelsOpenAll` (17 chars) from the serial console or from any client that does not pre-validate, e.g. `?SEQ,SAVE,DomePanelsOpenAll,;MD1^;t500^;MD2`. The board prints `Stored: Key='DomePanelsOpenAll', ...`, the name appears in `?SEQ,NAMES` and in the Wizard's sequence list, and the WDP SEQHASH changes so NaviCore re-pulls the inventory. Recalling it does nothing and `?SEQ,GET,DomePanelsOpenAll` reports NOTFOUND — the sequence is permanently a ghost entry in the index, and it survives reboot because key_list was written.

**Suggested fix.** Add the same guard the peer paths already use, at the top of `saveStoredCommandsToPreferences()` right after the empty check (WCB_Storage.cpp:637-640):
```
  if (key.length() > 15) {   // 15 = the NVS key limit; a longer key cannot be stored at all
    Serial.printf("Key '%s' is %d chars — max 15 (NVS key limit).\n", key.c_str(), key.length());
    preferences.end();  // only if already begun; otherwise return before begin()
    return;
  }
```
Place it BEFORE `preferences.begin()` so nothing is written. Additionally, check the result of `putString` at :643 and skip the `key_list` append when it returns 0, so the index can never record a key whose value failed to store. Consider factoring the literal 15 into a shared constant alongside the WCB.ino:3492 check.

### F-366 · S2 · `c:/Users/ghulette/Documents/GitHub/Wireless_Communication_Board-WCB/Wizard/serial-hub.js:356`

**Wizard/serial-hub.js is missing two lock-squat fixes that NaviCore's required-lockstep copy has**

*Category:* `cross-repo-lockstep`

`Wizard/serial-hub.js` and `NaviCore/config_tool/serial-hub.js` are required to stay in lockstep. They have diverged. A `diff -u` of the two files (519 lines NaviCore vs 548 Wizard, md5 fb9675b8… vs a80702f2…) shows the Wizard copy is missing two fixes NaviCore carries — both of which prevent a tab from squatting the exclusive Web Lock with an unusable port.

**(a) Open-failure path.** NaviCore/config_tool/serial-hub.js:314-318:
```
          // Out of retries: we hold the exclusive lock and a port we cannot open. Do NOT stop
          // here — squatting is the very deadlock _stepDownPortless() exists to prevent (a tab
          // queued behind us with a WORKING port would never be served). Step down and re-queue.
          if (i === attempts - 1) { this._log('open failed: ' + msg); this._stepDownPortless(); return; }
```
Wizard/serial-hub.js:356 instead does:
```
          if (i === attempts - 1) { this._log('open failed: ' + msg); this._announceState(); return; }
```
The Wizard returns still holding leadership and the Web Lock, having never opened the port. `_stepDownPortless()` exists in the Wizard copy (Wizard/serial-hub.js:301) and is called on the adjacent no-port branch (Wizard/serial-hub.js:292), so the omission is inconsistent within the file itself.

**(b) Read-loop end / unplug path.** NaviCore/config_tool/serial-hub.js:373-381:
```
      // The port died under us (unplug, or the empty-session cap tripped). Do NOT hold the
      // exclusive lock with a dead port: close the stale handle so a promoted tab's open() can
      // succeed, then step down — that announces portOpen:false, releases the lock and re-queues
      // on the jittered backoff, so the device is picked up again when it comes back. Merely
      // announcing here left the board unreachable from EVERY tab on the origin (they all sit in
      // the lock queue behind us) until the user disconnected this tab by hand.
      if (this._portOpen && !this._leaving && this._role === 'leader') {
        this._log('port read loop ended (unplug?)');
        await this._closePort();       // clears _portOpen and frees the OS handle
        this._stepDownPortless();
      }
```
Wizard/serial-hub.js:406-410 is exactly the "merely announcing" behaviour that comment identifies as the bug:
```
      if (this._portOpen && !this._leaving && this._role === 'leader') {
        this._portOpen = false;
        this._announceState();
        this._log('port read loop ended (unplug?)');
      }
```
No `_closePort()`, no `_stepDownPortless()` — the lock and the stale OS handle are both retained. `_closePort()` is defined in the Wizard copy at Wizard/serial-hub.js:430, so the fix is directly portable.

**(c) Backoff-reset placement**, a consequence of (a). NaviCore resets the escalation counter after a *successful open* (NaviCore/config_tool/serial-hub.js:332) with the comment "Reset the step-down backoff HERE, not on promotion: a leader that holds a port it can never open also steps down through _stepDownPortless(), and resetting on promotion would pin that retry at the 800 ms floor." The Wizard resets it on promotion instead (Wizard/serial-hub.js:291: `if (this._port)  { this._portlessRetries = 0; await this._openAndRead(); }`), which is the pinned-at-the-floor case NaviCore's comment warns against.

Direction is confirmed by history: NaviCore's fixes landed in `f72878a "Apply 28 confirmed medium-severity review fixes"`; the Wizard copy's last touch is `581d939 "Shared hub: conditional campaigning — a portless tab never squats the lock"`. The Wizard file is the older one (mtime 2026-08-05 vs 2026-08-18).

Separately, the Wizard copy adds `adoptPort()` (Wizard/serial-hub.js:175) and `releasePortKeepOpen()` (Wizard/serial-hub.js:227), which NaviCore lacks. Those are genuinely Wizard-only (used at Wizard/app.js:5490, :5765, :7032) and I confirmed NaviCore's config_tool references neither, so they are additive rather than a behavioural conflict — but they are why a plain file copy cannot be used to re-sync.

**Failure scenario.** User has the Wizard open in two tabs on the same origin with `?share=1`. Tab A wins the Web Lock, then the WCB is unplugged (or the USB handle is taken by another app). Tab A's read loop ends: it sets `_portOpen = false` and announces, but keeps the exclusive Web Lock and the dead OS handle forever. Tab B — which the user then grants a working port to — sits queued behind Tab A's lock and is never promoted, so the board is unreachable from every tab on the origin until the user manually disconnects Tab A. The same squat occurs via path (a) when a promoted tab holds a port whose `open()` keeps failing.

**Suggested fix.** Re-sync the two copies. Apply NaviCore's three changes to Wizard/serial-hub.js: at :356 replace `this._announceState(); return;` with `this._stepDownPortless(); return;`; at :406-410 replace the announce-only block with `this._log(...); await this._closePort(); this._stepDownPortless();`; and move the `_portlessRetries = 0` reset from :291 to just after `this._portOpen = true;` in `_openAndRead`. Then port `adoptPort()`/`releasePortKeepOpen()` into NaviCore's copy (they are inert there) so the two files become byte-identical and can be diffed as a lockstep check in future.

### F-367 · S2 · `Code/bin/build.sh:181`

**build.sh deletes the committed firmware binaries and exits 0 when the branch name contains a "/" — CI then commits and pushes the deletion as a green build**

*Category:* `release-pipeline`

`BRANCH` is taken from the git ref verbatim (`Code/bin/build.sh:48-51` — `git rev-parse --abbrev-ref HEAD`, falling back to `${GITHUB_REF_NAME:-unknown}`), so on any branch whose name contains a slash it becomes e.g. `fix/raw-serial-bidirectional`. The publish block then does, in order:

- `:181` `rm -f "$OUTPUT_DIR"/WCB_*_ESP32*.bin`  ← deletes every committed version-named binary
- `:183` `cp "$TMP_ESP32/WCB.ino.bin" "$OUTPUT_DIR/WCB_${VERSION}_${BRANCH}_ESP32.bin"` → target dir `Code/bin/WCB_<ver>_fix/` does not exist → `cp: cannot create regular file ... No such file or directory`

The script has no `set -e` (there is no `set` line anywhere in the file) and **no `cp` exit code is checked** (`:183-202`), so all eight version-named copies fail one after another and execution falls through to `:213 exit 0`. Only the fixed-name `Wizard/bin/*` copies (`:186-188`, `:192-193`, `:197-198`, `:202`) succeed, so the final `ls -lh` at `:211` still prints files (the two custom bootloaders) and the run looks healthy.

`.github/workflows/build.yml` then treats that as success: `:121-124` runs the script (exit 0 → step passes), `:134` `git add Code/bin/ Wizard/bin/ Code/WCB/WCB.ino` stages the eight deletions (verified: `git add <dir>` stages removals, git 2.55), `:135-139` sees a non-empty staged diff and commits, `:144-152` pushes it. Green check, binaries gone.

This is not hypothetical — it already happened and is in the history. Commit `e746e56` on `origin/fix/raw-serial-bidirectional`, message "Auto-build: update firmware binaries [skip ci]", is:
```
 Code/bin/WCB_6.0_012113RAPR2026_main_ESP32.bin   | Bin 1120224 -> 0 bytes
 Code/bin/WCB_6.0_012113RAPR2026_main_ESP32S3.bin | Bin 1085072 -> 0 bytes
 ...6 Code/bin files deleted...
 Wizard/bin/WCB_ESP32.bin                         | Bin 1120224 -> 1120832 bytes
```
and every later auto-build commit on that branch (`1c83fa5`, `5cf8c01`) touches **only** `Wizard/bin/` — the version-named `cp`s never land. `git ls-tree origin/fix/raw-serial-bidirectional Code/bin/` today returns only `build.ps1` and `build.sh`: no firmware at all.

Three aggravating factors:
1. `build.sh:25-35` states the opposite as a guarantee — "If ANY build fails it touches nothing and exits NON-ZERO… a failed CI build silently wiped Code/bin while reporting success" is described as a fixed past bug. The `cp` half of that failure mode was never fixed; the comment is factually wrong about the code.
2. `build.yml:5` triggers on `branches: [ '**' ]` with no exclusion, while `pages-deploy.yml:34` deliberately excludes `'!claude/**'` — so every slash-bearing branch (`claude/*` worktrees, `fix/*`) runs this and pushes the wipe.
3. `Wizard/flasher.js:44` + `:118-131` fetch firmware from `Code/bin` for the branch the Wizard was served from, so a branch-preview Wizard for such a branch cannot flash at all (`No file ending with "_ESP32.bin" found in Code/bin/`). A wiped `Code/bin` merged forward to `main` would break the production flasher for every user until a `main` build succeeds.

**Failure scenario.** Push any commit touching `Code/**` on a branch whose name contains a slash (e.g. `fix/raw-serial-bidirectional`, or a merged `claude/*` worktree branch). Both targets compile fine; `build.sh:181` deletes all 8 committed binaries; every `cp` at `:183-201` fails with "No such file or directory" because `Code/bin/WCB_<ver>_fix/` is not a directory; the script exits 0; `build.yml:134-152` commits the 8 deletions and pushes them to the branch with a green ✓. Confirmed in history at commit `e746e56`.

**Suggested fix.** Two independent fixes, both cheap: (a) sanitize the branch for filenames — `BRANCH="${BRANCH//\//-}"` right after `Code/bin/build.sh:51`; (b) make failures loud — add `set -o pipefail` and either `set -e` or an explicit `cp … || { echo "✗ publish failed"; exit 1; }` on every copy at `:183-202`, and verify all expected outputs exist before `exit 0`. Also consider excluding `claude/**` from `build.yml:5` the way `pages-deploy.yml:34` already does.

### F-368 · S2 · `Code/WCB/WCB_Help.cpp:128`

**?BAUD? help tells the user to run ?MAESTRO,ENABLE, which is not implemented**

*Category:* `documented-command-missing`

The `?BAUD?` topic's Notes block says (WCB_Help.cpp:128):

```cpp
        Serial.println(F("  - Use ?MAESTRO,ENABLE as a shortcut for S1 Maestro setup"));
```

No `ENABLE` sub-verb exists. The `?MAESTRO` dispatcher (WCB.ino:4897-4915) handles exactly four recognised sub-commands and falls through everything else to the config parser:

```cpp
    if (rootUpper == "MAESTRO") {
        if (argsUpper == "LIST") {
            printMaestroSettings();
        } else if (argsUpper == "CLEAR,ALL") {
            clearAllMaestroConfigs();
        } else if (argsUpper.startsWith("CLEAR,")) {
            clearMaestroByID(args.substring(args.indexOf(',') + 1));
        } else if (argsUpper == "REMOTE") {
            ...
        } else {
            configureMaestro(args);
        }
```

So `?MAESTRO,ENABLE` reaches `configureMaestro("ENABLE")`, which rejects it at WCB_Maestro.cpp:538-542 because the argument must begin with `M`:

```cpp
  if (remaining.isEmpty() || !remaining.startsWith("M") && !remaining.startsWith("m")) {
    Serial.printf("Invalid format. Use: %cMAESTRO,M<maestroID>:W<wcb>S<port>:<baud>\n", LocalFunctionIdentifier);
```

(`"ENABLE"` does start with `E`, not `M`, so the guard fires.) I grepped the whole firmware for `ENABLE` as a command token — `Code/WCB/*.cpp`, `*.h`, `*.ino` — and every hit is either an unrelated status string (`"ENABLED"` in WCB.ino:2221, :2249, :4891, :5382, :5580, :5627, :7431) or a comment (WCB_Maestro.cpp:894). There is no `ENABLE` verb anywhere.

The `?MAESTRO?` topic itself (WCB_Help.cpp:406-470) correctly lists only `Mx:WxSx:baud`, `LIST`, `CLEAR,x`, `CLEAR,ALL` — so this is a stale cross-reference in `?BAUD?` pointing at a shortcut that was either never built or was removed.

**Failure scenario.** A user setting up their first Maestro reads `?BAUD?`, sees the shortcut, and types `?MAESTRO,ENABLE`. They get "Invalid format. Use: ?MAESTRO,M<maestroID>:W<wcb>S<port>:<baud>" — an error that does not mention ENABLE at all and reads as if they mistyped, rather than as "this command does not exist". They have no way to tell the help is wrong.

**Suggested fix.** Delete WCB_Help.cpp:128, or replace it with a pointer to the command that actually does the job:

```cpp
Serial.println(F("  - Maestro setup: ?MAESTRO,M1:W1S1:57600 (sets the port baud too)"));
```

`configureMaestro()` already applies the baud as part of the mapping, so the cross-reference is redundant either way.

### F-369 · S2 · `Code/WCB/WCB_Help.cpp:364`

**?WCB? help caps the board number at 9; the parser accepts 1-20**

*Category:* `help-syntax-mismatch`

The `?WCB?` topic states the range twice, both times as 1-9 (WCB_Help.cpp:364 and :369):

```cpp
        Serial.println(F("  Sets this board's WCB number (1-9). The WCB number determines"));
...
        Serial.println(F("  number        1-9, must be unique in the system"));
```

The parser accepts 1..MAX_WCB_COUNT. WCB.ino:4765-4772:

```cpp
    if (rootUpper == "WCB") {
        int newWCB = args.toInt();
        if (newWCB >= 1 && newWCB <= MAX_WCB_COUNT) {
            saveWCBNumberToPreferences(newWCB);
        } else {
            Serial.printf("Invalid WCB number %d. Valid range: 1-%d.\n", newWCB, MAX_WCB_COUNT);
        }
```

and `MAX_WCB_COUNT` is 20 (WCB.ino:120). The legacy `?WCBx` path agrees — WCB.ino:5695 uses the same `newWCB <= MAX_WCB_COUNT` bound — and `saveWCBNumberToPreferences()` (WCB_Storage.cpp:190-198) applies no bound of its own.

This is not a global doc convention: the adjacent `?WCBQ?` topic was updated and states the correct modern range (WCB_Help.cpp:392, `"  quantity      Baseline number of WCB boards (1-20)"`), matching `saveWCBQuantityPreferences()`'s `quantity > MAX_WCB_COUNT` check at WCB_Storage.cpp:437. So `?WCB?` is simply stale relative to its own sibling.

The same stale 1-9 appears in the `WCB_Help.h` reference block, lines 21-22:

```
//   ?WCB,x                   Set this board's WCB number (1-9)
//   ?WCBQ,x                  Set total number of WCB boards in system (1-9)
```

This is the board-count cap bug class that was already swept out of the executable paths (see the RTERM comment at WCB.ino:5104-5106 recording exactly that fix) — it survived in the help text.

**Failure scenario.** A builder standing up an 11-board droid reads `?WCB?`, believes 9 boards is the ceiling, and either renumbers their fleet to squeeze under 9 or concludes the firmware cannot support their build. Nothing in the help hints that `?WCB,11` is accepted and works, and the ?WCBQ? page they might read next says 1-20, leaving them with two contradictory limits and no way to know which is real.

**Suggested fix.** Update both lines in `?WCB?` to the real bound, and match `?WCBQ?`'s wording:

```cpp
Serial.println(F("  Sets this board's WCB number (1-20). The WCB number determines"));
...
Serial.println(F("  number        1-20, must be unique in the system"));
```

Also fix `WCB_Help.h:21-22`. Consider printing `MAX_WCB_COUNT` via `Serial.printf` in these two lines so the help can never drift from the constant again.

### F-370 · S2 · `Code/WCB/WCB_Help.cpp:669`

**?WLED? help documents only the legacy single-device form — the canonical ID-addressed config and ;L<id> addressing are undocumented**

*Category:* `help-coverage`

The `?WLED?` help topic's "Configuration Commands" block lists exactly four verbs (WCB_Help.cpp:669-672):

```
  PORT,Sx:baud      Reserve & configure the WLED port
  CLEAR             Release the WLED port
  LIST              Show WLED configuration and link status
  STATUS            Show status  [WLED:...]
```

The parser `configureWLED()` accepts strictly more, and labels the documented form as the legacy one. `WCB_WLED.cpp:242-253`:

```cpp
  if (aU.startsWith("PORT,")) {
    // Legacy: ?WLED,PORT,S<port>:<baud>  ->  WLED id 1, local to this board.
    ...
    wledID   = 1;
    dest     = "W" + String(WCB_Number) + spec.substring(0, colon);   // "W<self>S<port>"
```

The canonical form is at `WCB_WLED.cpp:254-267`:

```cpp
  } else {
    // New: ?WLED,<id>:W<wcb>S<port>:<baud>   (e.g. 1:W2S1:115200)
```

and a per-id clear at `WCB_WLED.cpp:236`: `if (aU.startsWith("CLEAR,")) { clearWLEDByID(a.substring(6)); return; }`.

The consequences of documenting only the legacy branch are concrete, because the legacy branch hard-codes `wledID = 1` and `W<self>`:
- IDs 2-9 are unreachable. `WCB_WLED.cpp:270` validates `wledID` as 1-9 and `MAX_WLED_PER_WCB` slots exist, but `PORT,` can only ever write id 1.
- A REMOTE WLED proxy (a WLED hosted on another board, `WCB_WLED.cpp:288-307`) cannot be configured at all, because `dest` is forced to this board's own number.

The "Runtime Actions" block (WCB_Help.cpp:675-682) has the matching gap: every line is written as bare `;L,...` (`;L,PS,<n>`, `;L,COL,RRGGBB`, …). The runtime parser explicitly supports an id — `WCB_WLED.cpp:112` comments the accepted shapes as `"L,PS,2"   "L3,PS,2"   "L,ON"   "L2,JSON,{...}"`, and `WCB_WLED.cpp:117-119` parses the leading digits into `id`. Bare `;L` is documented at `WCB_WLED.cpp:126-137` as "act on THIS board's own (lowest-id) LOCAL WLED" — so the help teaches only the fallback path and never mentions that `;L<id>` is how you address a specific or remote WLED.

By contrast the sibling `?DFP?` topic does document its full surface including `REMOTE,W<n>` (WCB_Help.cpp:557-558), so this is a gap specific to WLED, not a house style.

**Failure scenario.** A user with two WLED nodes — one on this board, one on WCB4 — reads `?WLED?`, sees only `PORT,Sx:baud`, and runs `?WLED,PORT,S2:115200` on each board. Both get id 1 local to themselves. They then try `;L2,PS,3` to fire preset 3 on the second node and get `[WLED] WLED 2 not configured` (WCB_WLED.cpp:142), with nothing in the help explaining that `?WLED,2:W4S1:115200` is the command they needed. There is no way to reach the remote-proxy path in WCB_WLED.cpp:288 from anything the help shows.

**Suggested fix.** Rewrite the `?WLED?` "Configuration Commands" block to lead with the canonical form and demote PORT to the Legacy section that every other topic already has:

```
Configuration Commands (?WLED,...):
  <id>:W<wcb>S<port>:<baud>  Configure WLED <id> (1-9)
                               W<wcb> = this board -> LOCAL
                               W<wcb> = another board -> REMOTE proxy
  CLEAR             Release all WLED config
  CLEAR,<id>        Release one WLED by id
  LIST / STATUS     ...

Legacy commands:
  ?WLED,PORT,Sx:baud   - shorthand for id 1 local to this board
```

and change the runtime lines to `;L<id>,...` with a note that bare `;L` targets this board's lowest-id local WLED. Add `?WLED,2:W4S1:115200` and `;L2,PS,3` to the Examples block.

### F-371 · S2 · `Code/WCB/WCB_Help.cpp:925`

**?CMDCHAR? documents `;Px,width` but the parser rejects the comma, and the rejection is silent**

*Category:* `help-syntax-mismatch`

The `?CMDCHAR?` topic's "Command character prefixes" list ends with (WCB_Help.cpp:925):

```cpp
        Serial.println(F("  ;Px,width     Output PWM pulse on port x"));
```

This is the only place in the live help system where the `;P` runtime command is documented at all (the top-level menu's ROUTING COMMANDS block, WCB_Help.cpp:1058-1065, omits `;P` entirely, and `WCB_Help.h`'s reference block never mentions it). The documented comma separator is wrong.

`processPWMOutput()` at WCB.ino:6187-6196:

```cpp
void processPWMOutput(const String &message) {
    // Format: ;Pxnnnn where x=port, nnnn=pulse width in microseconds

    int port = message.substring(1, 2).toInt();
    unsigned long pulseWidth = message.substring(2).toInt();

    if (port < 1 || port > 5 || pulseWidth < 500 || pulseWidth > 2500) {
        if (debugPWMEnabled) Serial.println("Invalid PWM output command");
        return;
    }
```

`message` arrives with the CommandCharacter stripped (WCB.ino:4384-4385 → `processCommandCharcter(cmd.substring(1), sourceID)`), routed to this handler by the `p`/`P` branch at WCB.ino:5857-5858. So for the documented `;P4,1500`: `message` is `"P4,1500"`, `substring(2)` is `",1500"`, and Arduino `String::toInt()` (atol) on a string with a leading comma returns 0. `pulseWidth == 0 < 500` → the command returns having done nothing. The code's own adjacent comment (WCB.ino:6188) states the real grammar: `;Pxnnnn`, i.e. `;P41500`, no separator.

The failure is completely silent. The only diagnostic is gated behind `debugPWMEnabled`, and that flag is never set anywhere in the firmware — WCB.ino:177 (`bool debugPWMEnabled = false;`) is its sole assignment, and the only other reference is the read at WCB.ino:6194. (`?DEBUG,PWM,ON` sets a different variable, `debugPWMPassthrough`, at WCB.ino:4437.) No serial output, no error, no LED.

**Failure scenario.** A user reads `?CMDCHAR?` and stores a sequence `?SEQ,SAVE,liftup,;P4,1800` to drive a servo/ESC on S4. `;Cliftup` runs, prints the normal recall banner, and the pulse is never emitted — no error line appears on USB. The user concludes the wiring or the pin map is at fault and debugs hardware, because the only way to learn the real syntax is to read WCB.ino:6188.

**Suggested fix.** Correct the help line to match the parser and show a worked example, e.g. at WCB_Help.cpp:925:

```cpp
Serial.println(F("  ;Px<width>    Output PWM pulse on port x, NO comma"));
Serial.println(F("                  e.g. ;P41500 = 1500us pulse on S4 (500-2500)"));
```

Separately, un-gate the rejection message (or gate it on `debugPWMPassthrough`, which `?DEBUG,PWM,ON` actually sets) so a malformed `;P` is not silent. `debugPWMEnabled` is otherwise dead — WCB.ino:177 and :6194 are its only occurrences.

### F-372 · S2 · `Code/WCB/WCB_Help.cpp:1015`

**Ten implemented ? commands have no help topic and no menu entry; asking for help on them silently prints the generic menu**

*Category:* `help-coverage`

I enumerated every new-format root from `processLocalCommand()` (all 37 `rootUpper == "..."` comparisons, WCB.ino:4423-5128) and every topic in `printCommandHelp()` (all 26 `c == "..."` branches, WCB_Help.cpp:16-987). Ten implemented roots have no topic AND no line in the top-level menu (WCB_Help.cpp:1016-1080):

| Command | Dispatch | What it does |
|---|---|---|
| `?WCBCH,x` | WCB.ino:4790 | **ESP-NOW mesh channel 1-11, persisted, applies on reboot** |
| `?ALIAS,<text>/CLEAR/LIST` | WCB.ino:4839 | friendly per-board name, persisted, shown in Wizard header |
| `?CONTROLLER,ON[,id]/OFF` (alias `?SPECIAL`) | WCB.ino:4861 | registers the out-of-band controller peer (NaviCore) |
| `?TRACK,ON/OFF/STATUS` | WCB.ino:5018 | delivery tracking toggle |
| `?IDENTIFY` | WCB.ino:5079 | 5 s red/green blink to find a board physically |
| `?RTERM,START,<n>/STOP` | WCB.ino:5099 | relayed terminal mirroring |
| `?MGMT,...` | WCB.ino:5090 | mesh mgmt (PULL/STATS/SEQ/SEQGET/ETM/FRAG) |
| `?OTA,...` | WCB.ino:4826 | relay a firmware stream to a target board |
| `?OTALOCAL,...` | WCB.ino:4812 | local USB firmware OTA |
| `?PEERSLIVE` | WCB.ino:4803 | live peer count |

I verified absence by grepping WCB_Help.cpp for each token: WCBCH 0, ALIAS 0, CONTROLLER 0, SPECIAL 0, TRACK 0, IDENTIFY 0, RTERM 0, OTA 0. `PEERSLIVE` gets one passing mention inside the `?WDP?` notes (WCB_Help.cpp:862) but has no topic. `MGMT` appears only as two examples inside `?SEQ?` (`?MGMT,SEQ,3`, `?MGMT,SEQGET,3,wave` — both of which I confirmed do parse, at WCB.ino:2551 and :2555).

The discovery failure is worse than a plain omission because of the fall-through at WCB_Help.cpp:1015:

```cpp
    } else {
        // Top-level help menu
```

Every unrecognised topic lands here. `?WCBCH?` does not print "no help for WCBCH" — it prints the entire general menu, which looks like a successful response, so the user cannot distinguish "undocumented" from "I mistyped".

`?WCBCH` is the standout. It is a fleet-wide setting that must be pushed to every board and rebooted together (its own comment, WCB.ino:4785-4789, warns that a live switch would strand the board mid-config), and there is no help anywhere describing that procedure. `?ALIAS`, `?CONTROLLER`, `?TRACK` and `?IDENTIFY` are ordinary user-facing settings. `?OTA`/`?OTALOCAL`/`?MGMT`/`?RTERM` are Wizard-driven and arguably machine-facing, so they can stay out of the menu — but they should still return something better than the generic dump.

**Failure scenario.** Two droids at a con are interfering on the same ESP-NOW channel. The owner needs to move one fleet to another channel. `??` shows no channel command; `?WCBCH?` prints the general menu as though nothing is wrong; `?CHANNEL?` does the same. The feature exists and is exactly what they need (WCB.ino:4790-4798), but it is undiscoverable from the device, and the ordering constraint — push to the whole fleet, then reboot everyone together — is recorded only in a source comment.

**Suggested fix.** Two changes:

1. Add topics for at least the six user-facing commands (`WCBCH`, `ALIAS`, `CONTROLLER`, `TRACK`, `IDENTIFY`, `RTERM`) and menu lines for the first five. `?WCBCH?` must carry the fleet-wide/reboot-together constraint from WCB.ino:4785-4789.

2. Make the fall-through honest — before printing the menu at WCB_Help.cpp:1015, emit a line when `c` was non-empty:

```cpp
    } else {
        if (c.length()) Serial.printf("No detailed help for '%s'. General menu:\n", c.c_str());
```

That one line turns every future help gap into a visible message instead of a misleading menu dump.

### F-373 · S2 · `Code/WCB/WCB_Storage.cpp:352`

**?BCAST,RESET does not reset broadcast INPUT blocking at all and leaves RAM stale, so the next unrelated save re-persists the pre-reset values**

*Category:* `config-roundtrip`

`resetBroadcastSettingsNamespace()` (WCB_Storage.cpp:352-371) is the whole implementation of `?BCAST,RESET` (dispatched at WCB.ino:4604-4605). It does exactly three things: clear the `bdcst_set` namespace, re-create keys `S1`..`S5` = true, and print "Done. Please reboot."

Three gaps, all verifiable in that function body:

1. **Input blocking is never reset.** `blockBroadcastFrom[]` lives in a *different* namespace, `bcast_block` (written by `saveBroadcastBlockSettings()` at WCB_Storage.cpp:1584-1589, read by `loadBroadcastBlockSettings()` at :1574-1580). `resetBroadcastSettingsNamespace()` never opens it. So a port that `?MAP,SERIAL`, `?MAESTRO`, `?KYBER,LOCAL` or `?MP3` auto-blocked (each of those sets `blockBroadcastFrom[p-1] = true` and saves — e.g. WCB_Maestro.cpp:668-671, WCB_Storage.cpp:1738-1742) stays blocked forever after a "reset all defaults".
2. **The S0 key is not re-created.** `saveBroadcastSettingsToPreferences()` writes `S0` (WCB_Storage.cpp:337) and `loadBroadcastSettingsFromPreferences()` reads it (:348); the reset's `preferences.clear()` deletes it and the re-create loop only covers S1..S5.
3. **RAM is never updated.** `serialBroadcastEnabled[]`, `blockBroadcastFrom[]` and `broadcastToS0` all keep their pre-reset values. Any subsequent call to `saveBroadcastSettingsToPreferences()` — from `?KYBER,CLEAR` (WCB_Storage.cpp:1200-1203), `?MP3,CLEAR` (WCB_MP3.cpp:166-170), `?MAESTRO,...` (WCB_Maestro.cpp:662-665), `?MAP,SERIAL` (WCB_Storage.cpp:1743-1747) — writes the whole stale RAM array straight back over the freshly-reset NVS.

The help text calls it unqualified: `?BCAST,RESET  - Restore all defaults` (WCB_Help.cpp:187), and the shipped bench fixtures rely on it — TestConfigs/WCB1_01_base.txt:64 chains `...?BCAST,RESET^?MAP,CLEAR,ALL^?MAESTRO,CLEAR,ALL^?KYBER,CLEAR^?MP3,CLEAR^?config`, i.e. exactly the re-persist sequence in gap 3. I found no prior finding covering this (grep for `BCAST,RESET` / `resetBroadcastSettings` across docs/CODE_REVIEW*.md returns nothing).

**Failure scenario.** Operator pastes TestConfigs/WCB1_01_base.txt's PASTE line onto a board whose S2 output was disabled and S2 input blocked by an earlier Maestro config. `?BCAST,RESET` wipes bdcst_set and writes S1..S5=true to NVS. Three tokens later `?KYBER,CLEAR` runs, sees `serialBroadcastEnabled[1] == false` still in RAM, flips it true and calls `saveBroadcastSettingsToPreferences()` — which re-writes ALL five RAM values, restoring the pre-reset S1/S3/S4/S5 flags into the namespace that was just reset. `blockBroadcastFrom[]` was never touched at any point, so S2 input stays blocked. `?config` and the next reboot both show a board that was never actually reset, with no error anywhere.

**Suggested fix.** Make the reset operate on RAM and both namespaces, then persist through the normal writers: `for (int i=0;i<5;i++){ serialBroadcastEnabled[i]=true; blockBroadcastFrom[i]=false; } broadcastToS0=false; saveBroadcastSettingsToPreferences(); saveBroadcastBlockSettings();`. That fixes all three gaps at once and removes the need for the "Please reboot" caveat.

### F-374 · S2 · `Code/WCB/WCB_Storage.cpp:643`

**A stored-sequence key longer than 15 chars is silently discarded by NVS but still registered in key_list and reported as saved**

*Category:* `config-roundtrip`

`saveStoredCommandsToPreferences()` (WCB_Storage.cpp:624-664) uses the user-supplied sequence name directly as an NVS key and ignores the write result:
```
642:  preferences.begin("stored_cmds", false);
643:  preferences.putString(key.c_str(), value);
...
657:    preferences.putString("key_list", existingKeys);
...
663:  Serial.printf("Stored: Key='%s', Value='%s'\n", key.c_str(), value.c_str());
```
There is no length guard anywhere on the path — dispatch is WCB.ino:4993-4994 (`?SEQ,SAVE` → `saveStoredCommandsToPreferences(seqArgs)`) with no validation.

I confirmed the core behaviour in the pinned toolchain: `Preferences::putString` (esp32@3.3.4, libraries/Preferences/src/Preferences.cpp:264-279) calls `nvs_set_str`, which returns ESP_ERR_NVS_KEY_TOO_LONG for a key over 15 chars; the error goes only to `log_e` (compiled out at the default core debug level) and `putString` returns 0. That 0 is discarded at :643. The `key_list` append at :656-658 uses an 8-char key and succeeds, and :663 prints unconditional success.

The 15-char limit is clearly known in this codebase — `WCB_VAR_NAME_MAX 15 // NVS key length limit` (WCB_Variables.h:27), enforced by `isValidVariableName()` (WCB_Variables.cpp:63-73) — and the Wizard caps the sequence-key input at exactly `maxlength="15"` (Wizard/app.js:4220, :4549). Only the firmware path, which is reachable from the console, from WCB_Client/NaviCore over the mesh, and from any restored backup chain, is unguarded.

NOTE: recorded previously as docs/CODE_REVIEW_2026-08_wave2.md:569-621; re-verified UNFIXED at current OTA head.

**Failure scenario.** User types `?SEQ,SAVE,startup_sequence,;S1^;MD2` (key is 16 chars). Nothing is written, but the console prints "Stored: Key='startup_sequence', ..." and the name is appended to key_list. `?SEQ,LIST` then shows `Key: 'startup_sequence' -> Value: ''`, `;Cstartup_sequence` prints "No command stored under key", `collectConfigCommands` emits `?SEQ,SAVE,startup_sequence,` with an empty value (WCB.ino:2910), and restoring that backup hits the `value.length() == 0` reject at :637-640 — the phantom key propagates while the sequence is gone. `sequenceInventoryHash()` folds the empty value in, so peers pull an inventory listing a body-less sequence.

**Suggested fix.** Reject over-long keys before the write and check the return: `if (key.length() > 15) { Serial.println("Sequence key too long (max 15 characters)"); return; }` and `if (preferences.putString(key.c_str(), value) == 0) { Serial.println("Save FAILED — key rejected or NVS full"); preferences.end(); return; }` — do not touch key_list or print "Stored" unless the value write succeeded.

### F-375 · S2 · `Code/WCB/WCB_Storage.cpp:643`

**A stored-sequence key longer than 15 chars is silently discarded by NVS but still registered in key_list and reported as "Stored:" — the sequence is unrecallable and the phantom name propagates to peers**

*Category:* `bounds`

`saveStoredCommandsToPreferences()` (WCB_Storage.cpp:625-664) takes the key verbatim from user input and validates only that it is non-empty (:637). This is the ONLY unbounded NVS key in the entire firmware — every other computed key is a fixed prefix plus a bounded loop index (full table in scope_notes, max 8 chars).

I verified the failure against the pinned toolchain rather than assuming it:
- `NVS_KEY_NAME_MAX_SIZE 16` "including null terminator" — idf-release_v5.5-8410210c-v2/esp32/include/nvs_flash/include/nvs.h:61. So 15 usable characters.
- `Preferences::putString` → `nvs_set_str`; on error it calls only `log_e` (compiled out at the default core debug level) and returns 0 — esp32@3.3.4 libraries/Preferences/src/Preferences.cpp (same shape as `putChar` at :103-115, which I read).

The return value at WCB_Storage.cpp:643 is discarded. Execution continues to :645-659, which appends the key to `key_list` — that write uses an 8-char key and succeeds — and :663 prints "Stored: Key='%s', Value='%s'" unconditionally. Nothing anywhere else in the firmware checks a `put*` return either; `WCB_Variables.cpp:89-92` is the single exception, and its own header comment shows the author knew this was needed.

The 15-char limit is clearly known in this project — the Wizard caps the sequence-key input at exactly `maxlength="15"` and renders a `${key.length}/15` counter (Wizard/app.js:4186, :4220). The firmware, which is also reachable from the USB console, from a relayed `?SEQ,SAVE` over the mesh, from NaviCore/WCB_Client, and from a restored backup chain, has no guard at all.

The same unchecked return also hides two other real failure modes: a value over the 4000-byte NVS string cap (nvs.h:294-295), and an exhausted NVS partition (min_spiffs gives NVS 0x5000 = 20 KB; I confirmed the partition table). Nothing bounds the number of stored sequences either — `MAX_STORED_COMMANDS` (80) is used only as the legacy CMD#-scan range at :885 and :901, never as a cap on this path.

**Failure scenario.** User types `?SEQ,SAVE,dome_panels_wave,;MP1^;t500^;MP2` (16-char key). Board prints "Stored: Key='dome_panels_wave', Value='…'". `?SEQ,LIST` then shows `Key: 'dome_panels_wave' -> Value: ''`; `;Cdome_panels_wave` prints "No command stored under key". `buildSequenceNamesString()` (WCB_Storage.cpp:797-827) emits the phantom name in the `?SEQ,NAMES` / SEQ_REQ inventory and `sequenceInventoryHash()` folds the empty value into WDP_TLV_SEQHASH, so every peer and NaviCore's command picker lists a sequence with no body. `printBackupConfig` emits `?SEQ,SAVE,dome_panels_wave,` with an empty value (WCB.ino:2910), which a restore then rejects at the `value.length() == 0` check (:637) — so the name propagates but the sequence never does.

**Suggested fix.** Validate before the write and check the result. After the trim at WCB_Storage.cpp:635: `if (key.length() > 15) { Serial.printf("Sequence key '%s' is %u chars — NVS keys are limited to 15.\n", key.c_str(), key.length()); return; }`. Then at :643: `if (preferences.putString(key.c_str(), value) == 0) { Serial.println("Save FAILED — key rejected or NVS full"); preferences.end(); return; }` so `key_list` is never touched and "Stored:" is never printed on a failed write. This also closes the full-NVS and oversized-value cases in one place.

### F-376 · S2 · `Code/WCB/WCB_Storage.cpp:834`

**The legacy stored_commands namespace is never cleared and the migration guard lives inside the namespace that IS cleared — ?SEQ,CLEAR,ALL or ?ERASE,NVS resurrects deleted sequences on the next boot**

*Category:* `logic`

`migrateOldStoredCommands()` guards itself with a flag it stores in the same namespace it manages: it reads `preferences.getBool("seq_mig_done", false)` from `stored_cmds` at WCB_Storage.cpp:876-877 and writes it back at :925-926.

Both wipe paths destroy that flag:
- `clearAllStoredCommands()` (WCB_Storage.cpp:832-837, reached from `?SEQ,CLEAR,ALL` at WCB.ino:4986-4988) does `begin("stored_cmds", false); clear();` — an `nvs_erase_all` over the whole namespace, `seq_mig_done` included.
- `eraseNVSFlash()` clears `stored_cmds` at WCB_Storage.cpp:966-968, same effect.

Neither clears the LEGACY namespace `stored_commands`. It appears exactly once in the firmware — the read at WCB_Storage.cpp:887 — and is not in the `eraseNVSFlash()` list (:942-1026); my full enumeration confirms nothing anywhere writes or erases it.

So on the next boot `migrateOldStoredCommands()` (called from setup at WCB.ino:7214) sees `already == false` at :880, falls into Case 1 (:885-898), walks `CMD1`..`CMD80` out of the still-populated `stored_commands` namespace, and calls `saveStoredCommandsToPreferences()` for each hit (:895) — writing every legacy sequence straight back into the freshly-wiped store, and re-registering each in `key_list`.

**Failure scenario.** A board that was ever migrated up from the oldest firmware still has CMD1..CMDn in `stored_commands`. The user runs `?SEQ,CLEAR,ALL`; `?SEQ,LIST` correctly reports nothing. They reboot (or the Wizard's config push ends in `?REBOOT`) and every legacy sequence is back, printed as "[MIGRATION] Recovered from 'stored_commands'". The same happens after `?ERASE,NVS`. There is no way to permanently delete them from the console — the cycle repeats on every clear+reboot — and each cycle re-fires `invalidateSequenceInventoryHash()`, so peers re-pull an inventory the operator believes they deleted.

**Suggested fix.** Two independent fixes, both wanted: (1) store the migration flag OUTSIDE the namespace it protects — e.g. `putBool("seq_mig", true)` in `wcb_config` — or re-write `seq_mig_done` immediately after `preferences.clear()` in `clearAllStoredCommands()` (WCB_Storage.cpp:834); and (2) add `preferences.begin("stored_commands", false); preferences.clear(); preferences.end();` to `eraseNVSFlash()`, which genuinely retires the legacy store instead of leaving a hidden shadow copy on every upgraded board.

### F-377 · S2 · `Code/WCB/WCB_Storage.cpp:978`

**?ERASE,NVS clears the hw_version namespace, so a factory reset reboots the board into the hw_version==0 pin map that binds all ten serial pins to GPIO5**

*Category:* `logic`

The namespace sweep shows `hw_version` is treated by `eraseNVSFlash()` exactly like a configuration namespace: `preferences.begin("hw_version", false); preferences.clear(); preferences.end();` at WCB_Storage.cpp:978-980. It is not configuration — it is the board's hardware identity, the only input to the pin map, and it cannot be re-derived at runtime.

After the reset, `loadHWversion()` (WCB_Storage.cpp:101-106) reads `preferences.getInt("hw_version", 0)` → 0 and calls `updatePinMap()`. I read `wcb_pin_map.cpp:10-25`: the `wcb_hw_version == 0` branch assigns GPIO5 to ALL TEN serial pins —
```
SERIAL1_TX_PIN = 5;  SERIAL1_RX_PIN = 5;
SERIAL2_TX_PIN = 5;  SERIAL2_RX_PIN = 5;
... through SERIAL5_TX_PIN = 5;  SERIAL5_RX_PIN = 5;
```
and `updatePinMap()` has no final `else`, so those are the values every port then gets. setup() calls `Serial1.begin(baudRates[0], SERIAL_8N1, 5, 5)` and `Serial3/4/5.begin(..., SWSERIAL_8N1, 5, 5, ...)` with no hw_version gate at all (WCB.ino:7224-7280) — the only guards there are Kyber/PWM/baud, none of which apply on a just-erased board (baud defaults are non-zero).

The board is recoverable — Serial0/USB still works, so `?HW,32` restores it — but nothing warns the user before or after, and the only visible hint is "No LED was setup. Define HW version" from WCB.ino:7167. Every one of the other 22 cleared namespaces holds a setting the user chose; this one holds a fact about the PCB in their hand.

(The consequences of the hw_version==0 map itself — UART TX driving the S1_RX header pin, three SoftwareSerials sharing one clobbered GPIO5 ISR — are documented separately as F-038; what the sweep adds is that `?ERASE,NVS` is a supported user command that deterministically produces that state on a previously-working board.)

**Failure scenario.** A user troubleshooting a config problem on a working HW 3.2 board with a Maestro on S1 runs `?ERASE,NVS` as the standard "start clean" step. The board reboots with hw_version 0, so `Serial1.begin(9600, SERIAL_8N1, 5, 5)` routes UART1's TX out-signal onto GPIO5 — the S1_RX header pin the Maestro is driving as its own output. No serial port on the board works, all five are aliased onto one pin, and the user has no indication that the erase removed the hardware version rather than just the settings.

**Suggested fix.** Either preserve `hw_version` across `?ERASE,NVS` (read it before the loop, re-write it after — it is hardware identity, and the backup chain already carries `HW,xx` for the case where the user genuinely wants to change it), or keep clearing it but print an explicit post-erase warning naming the required `?HW,xx` command, and make the `wcb_hw_version == 0` branch in wcb_pin_map.cpp:14-23 inert (all pins -1) with the `SerialN.begin()` calls at WCB.ino:7224-7280 gated on a recognised version.

### F-378 · S2 · `Code/WCB/WCB_Storage.cpp:1024`

**?ERASE,NVS never clears the dfp_cfg namespace — the DFPlayer config and its port claim survive a factory reset, and the adjacent WCB_DFP comment claims otherwise**

*Category:* `logic`

I enumerated every NVS namespace opened anywhere in Code/WCB (27 total, listed in scope_notes) and diffed it against the hand-maintained list in `eraseNVSFlash()` (WCB_Storage.cpp:941-1033) plus the two that `clearAllPWMMappings()` handles (`pwm_mappings` WCB_PWM.cpp:499, `pwm_outputs` WCB_PWM.cpp:503). `dfp_cfg` is absent.

`saveDFPSettings()` opens it at WCB_DFP.cpp:388 and writes `port`, `baud`, `en`, `vol`, `defvol`, `onErr`, `rwcb` (WCB_DFP.cpp:389-395). `loadDFPSettings()` (WCB_DFP.cpp:400-407) is called unconditionally at boot from setup (WCB.ino:7142), so every one of those values comes straight back after the "factory reset" reboot.

The sibling namespaces are all cleared: `mp3_cfg` at WCB_Storage.cpp:1006, `hcr_cfg` at :1024, `wled_cfg` at :1025, `wcb_vars` at :1026. The comment immediately above those three (WCB_Storage.cpp:1022-1023) records that this exact class of bug already shipped once — "Previously MISSED by factory reset — HCR, WLED, and user-variable configs survived an erase and re-seized their ports / reappeared after reboot." DFP was added to the firmware after that fix and never joined the list.

Separately, the comment at WCB_DFP.cpp:108-110 is factually wrong about the code: "?DFP,CLEAR removes the LOCAL host only; the auto-learned remote route is a separate axis (?DFP,REMOTE,OFF / factory reset)." There is no factory-reset path that clears `dfpConfig.remoteWCB` — it is only ever written by `saveDFPSettings()` into the one namespace the erase misses. A comment stating a cleanup path that does not exist is itself a finding.

Note the Wizard's own factory reset is NOT equivalent: it blanks the entire 20 KB NVS partition with 0xFF (Wizard/flasher.js:632-634), so the two "factory resets" produce different boards.

**Failure scenario.** A DFPlayer is configured on S2 (`?DFP,S2`). The user runs `?ERASE,NVS` (WCB.ino:5071) to hand the board to someone else. The board reboots; `loadDFPSettings()` restores `configured=true, serialPort=2`. `isSerialPortUsedForDFP(2)` is true, so `processIncomingSerial()` returns early for S2 (WCB.ino:6370) — a device newly plugged into S2 is deaf and mute with no diagnostic. The board also keeps advertising WDP_CAP_DFPLAYER, so every peer re-learns it as the `;D` host via `dfpAutoAddRemote()` (WCB_WDP.cpp:735, WCB_DFP.cpp:429-436) and persists that route into its own dfp_cfg — where the same erase gap means it survives their factory reset too.

**Suggested fix.** Add `preferences.begin("dfp_cfg", false); preferences.clear(); preferences.end();` alongside the hcr_cfg/wled_cfg/wcb_vars trio at WCB_Storage.cpp:1024-1026 and extend the "Previously MISSED" comment. Then fix WCB_DFP.cpp:109 so it names a path that exists. The durable fix is to stop hand-maintaining the list: put the namespace names in one `static const char* NVS_NAMESPACES[]` table that both `eraseNVSFlash()` and each module reference, so a new subsystem cannot be forgotten a third time.

### F-379 · S2 · `Code/WCB/WCB_Storage.cpp:1028`

**eraseNVSFlash() never reaches its own confirmation or restart, and ?ERASE,NVS silently reboots OTHER boards in the fleet**

*Category:* `logic`

`eraseNVSFlash()` ends its namespace list and then calls `clearAllPWMMappings()` at WCB_Storage.cpp:1028 to cover the two PWM namespaces. That callee does not return: `clearAllPWMMappings()` (WCB_PWM.cpp:454-512) finishes with `Serial.println("Rebooting in 3 seconds to apply changes..."); delay(3000); ESP.restart();` at WCB_PWM.cpp:509-511.

Two consequences, both verified by reading both functions end to end:

1. WCB_Storage.cpp:1030-1032 — `Serial.println("NVS cleared. Restarting..."); delay(2000); ESP.restart();` — is unreachable. A user who runs `?ERASE,NVS` (WCB.ino:5071) or `WCB_ERASE` (WCB.ino:5209) never sees the erase confirmed; the last thing on the console is the PWM module's "All PWM mappings cleared / Rebooting in 3 seconds", which reads as a PWM operation, not a factory reset.

2. More seriously, `clearAllPWMMappings()` is not a local operation. For every remote board named as a PWM output it sends `?PX<port>` and then `?REBOOT` over ESP-NOW (WCB_PWM.cpp:477-497, specifically the `sendESPNowMessage(wcb, remoteCmd)` at :483 and `sendESPNowMessage(wcb, "?REBOOT")` at :491). That is correct behaviour for `?MAP,PWM,CLEAR,ALL`, which is what the function was written for — but `eraseNVSFlash()` inherits it, so a factory reset of ONE board strips PWM output ports from and hard-reboots every peer board that was receiving PWM from it. Nothing in the `?ERASE,NVS` help text or in `eraseNVSFlash()` mentions fleet-wide side effects.

**Failure scenario.** A user has `?MAP,PWM,S1,W2S3` on WCB1 (RC channel forwarded to WCB2's S3). They factory-reset WCB1 with `?ERASE,NVS`. WCB2 — mid-show, unrelated to the reset — receives `?PX3` and `?REBOOT` and drops offline for its full boot cycle, losing its ETM heartbeat, WDP neighbour state and any in-flight sequence. WCB1's console never prints "NVS cleared", so the operator cannot tell from the log whether the erase completed or whether the board just rebooted for a PWM reason.

**Suggested fix.** Split the NVS wipe out of the reboot/notify behaviour: add a `clearPWMMappingNVS()` that only does the two `begin/clear/end` pairs (WCB_PWM.cpp:499-505) plus the local `detachPWMInterrupt` housekeeping, and call THAT from `eraseNVSFlash()`; leave `clearAllPWMMappings()` (peer `?PX`/`?REBOOT` + self-restart) for the `?MAP,PWM,CLEAR,ALL` path at WCB.ino:4514/4546/5256. `eraseNVSFlash()` then reaches its own :1030-1032 and confirms the erase before restarting.

### F-380 · S2 · `Code/WCB/WCB_Storage.cpp:1078`

**?KYBER,CLEAR is emitted in every non-Kyber board's config chain and always resets Serial2's baud + broadcast flags, silently overwriting settings the same push just applied**

*Category:* `config-roundtrip`

`storeKyberSettings()` takes the no-comma path for the bare string "clear": `if (firstComma == -1) { baseCommand = message; ... kyberPort = 2; }` (WCB_Storage.cpp:1076-1082). `kyberPort` is therefore hard-coded to 2 on the clear path, regardless of which port Kyber was actually on (the real value is in the global `kyberLocalPort`, set at :1159 and loaded at :1458).

The clear branch then acts on that port unconditionally:
```
1191:    if (kyberPort > 0) { updateBaudRate(kyberPort, 9600); ... }
1200:    if (!serialBroadcastEnabled[kyberPort - 1]) { serialBroadcastEnabled[kyberPort-1] = true; saveBroadcastSettingsToPreferences(); }
1205:    if (blockBroadcastFrom[kyberPort - 1])      { blockBroadcastFrom[kyberPort-1] = false; saveBroadcastBlockSettings(); }
```
This is on the config-restore path, not a corner case. `collectConfigCommands()` emits `KYBER,CLEAR` with includeInLive=true for every board that is neither Kyber_Local nor Maestro_Remote (WCB.ino:2859), and the Wizard emits the identical token (Wizard/parser.js:1355). In BOTH emitters the baud and broadcast commands come first — WCB.ino:2804 (`BAUD,S<n>`) and :2811-2814 (`BCAST,OUT/IN`) vs :2859; parser.js:1314/1330-1336 vs :1355 — so the trailing `?KYBER,CLEAR` runs after and overwrites what was just restored. Dispatch is confirmed at WCB.ino:4651-4657 (`rootUpper == "KYBER"` → `argsUpper.startsWith("CLEAR")` → `storeKyberSettings(args)` with args == "CLEAR").

NOTE: this is the same defect recorded in docs/CODE_REVIEW_2026-08_wave2.md:721-738; I re-verified it against the current OTA head and it is UNFIXED. It is the single most damaging config-round-trip defect still open before the release.

**Failure scenario.** A board with no Kyber has a Maestro on S2 at 57600 with broadcast output off. Operator does a Wizard full push (or pastes the board's own ?backup chain). Sequence executed: ?BAUD,S2,57600 (applied+persisted) → ?BCAST,OUT,S2,OFF (applied+persisted) → ?KYBER,CLEAR → S2 forced to 9600 and persisted, broadcast OUT re-enabled and persisted, broadcast IN unblocked and persisted. The Maestro on S2 stops responding. The Wizard's verify-pull reads back 9600/ON, sees a delta against its baseline, and re-pushes the same change on every subsequent save.

**Suggested fix.** In the clear branch capture the real port before zeroing it: `int clearPort = (kyberPort >= 1 && kyberPort <= 5) ? kyberPort : kyberLocalPort;` taken BEFORE `kyberLocalPort = 0` at :1188; skip the baud/broadcast restoration entirely when clearPort is 0; and guard every `[clearPort-1]` index with `clearPort >= 1 && clearPort <= 5`, matching the guard the local branch already has at :1162.

### F-381 · S2 · `Code/WCB/WCB_Storage.cpp:1636`

**?MAP,SERIAL appends destinations rather than replacing them, so a destination removed or changed in the Wizard stays live on the board**

*Category:* `config-roundtrip`

`addSerialMonitorMapping()` reuses an existing mapping for the input port without resetting its destination list:
```
1635:    for (int i = 0; i < MAX_SERIAL_MONITOR_MAPPINGS; i++) {
1636:        if (serialMonitorMappings[i].active && serialMonitorMappings[i].inputPort == inputPort) {
1637:            mapping = &serialMonitorMappings[i];
1638:            break;
```
`mapping->outputCount = 0` is executed ONLY when a brand-new slot is allocated (:1649). Destinations are then appended at :1732-1735 (`mapping->outputs[mapping->outputCount].wcbNumber = wcbNum; ... mapping->outputCount++;`), with duplicates merely skipped at :1719-1727. Nothing in the function ever removes an output.

The Wizard's save path sends only the MAP command with no preceding clear: `let cmd = \`${lfi}MAP,${type.toUpperCase()},S${src}\`; if (type === 'Serial' && raw) cmd += ',R'; for (const dest of destinations) cmd += ...` (Wizard/app.js:3837-3841). `removeMappingRow()` does send `?MAP,SERIAL,CLEAR,S<src>` (app.js:3641) but only when the WHOLE row is deleted. `buildCommandString()` also re-emits mappings with no clear (parser.js:1509-1522).

This is specific to SERIAL. The PWM handler REPLACES — `mapping.outputCount = 0;` before its parse loop (WCB_PWM.cpp:227) — and app.js:3883-3897 sends explicit `?MAP,PWM,CLEAR,OUT,S<p>` for removed remote PWM destinations.

NOTE: recorded previously as docs/CODE_REVIEW_2026-08_wave3.md:784-794; re-verified UNFIXED at current OTA head.

**Failure scenario.** A mapping exists on the board: S1 → S2, W2S3. The user opens the row, deletes the S2 destination (leaving only W2S3) and clicks Save. The Wizard sends `?MAP,SERIAL,S1,W2S3`; the firmware finds S1 already mapped, sees W2S3 as a duplicate, skips it, and prints "No new destinations added". The board still fans S1 out to S2. Worse on an edit: replacing S2 with S4 sends `?MAP,SERIAL,S1,S4`, which APPENDS S4 — S1 now feeds both S2 and S4, double-routing traffic to a port the user removed. The Wizard shows a green "Mapping saved" toast either way, and the auto-pull 2 s later silently repopulates the deleted destination.

**Suggested fix.** Give the serial path replace semantics like PWM: when an existing mapping for the input port is reused, zero `outputCount` before the destination parse loop, and drop the `outputsAdded > 0` gate on the save so an emptied list persists. Alternatively have the Wizard emit `?MAP,SERIAL,CLEAR,S<src>` immediately before every `?MAP,SERIAL,S<src>,...` re-push.

### F-382 · S2 · `Code/WCB/WCB_Storage.cpp:1663`

**Toggling raw mode on an existing serial mapping changes it in RAM but is never persisted — it silently reverts on reboot**

*Category:* `config-roundtrip`

`addSerialMonitorMapping()` (WCB_Storage.cpp:1593) applies the R-suffix change unconditionally to the in-memory mapping:
```
1663:    if (mapping->rawMode != inputRawMode) {
1664:        mapping->rawMode = inputRawMode;
1665:    }
```
but the only NVS write in the function is gated on new destinations having been added:
```
1752:    if (outputsAdded > 0) {
1753:        saveSerialMonitorMappings();
```
When the mapping already exists with the same destinations, every destination hits the duplicate `continue` at :1720-1728, `outputsAdded` stays 0, and `saveSerialMonitorMappings()` is skipped — so `sm<i>_raw` in the `serial_map` namespace (written at :1844, read at :1549) keeps the old value.

This is the only reachable way to change raw mode: `setSerialMappingRawMode()` (:1818-1828) DOES call `saveSerialMonitorMappings()`, but a grep across `Code/WCB/*.cpp *.h *.ino` finds only its definition and its declaration at WCB_Storage.h:239 — zero callers.

The Wizard hits this on the normal path. `saveMappingRow()` builds `?MAP,SERIAL,S<src>[,R],<dests>` (Wizard/app.js:3837-3841) with no preceding clear, and the raw checkbox at app.js:3499 feeds `rawMode` into that same command — so a user who ticks "raw" on an existing mapping and clicks Save gets exactly the outputsAdded==0 case. WCB.ino:4502-4506 translates `?MAP,SERIAL,S1,R,S2` into the legacy `S1R,S2` form before calling this function, so the R does arrive.

NOTE: recorded previously as docs/CODE_REVIEW_2026-08_wave2.md:775-806; re-verified UNFIXED at current OTA head.

**Failure scenario.** S3 already forwards to W2S1 in line mode. User needs binary passthrough and sends `?MAP,SERIAL,S3,R,W2S1` (or ticks the Raw box in the Wizard and clicks Save). W2S1 is a duplicate so outputsAdded == 0; the board prints "No new destinations added (all were duplicates or invalid)" but raw forwarding DOES start working, because `RawSerialForwardingTask` (WCB.ino:6643) and `isSerialPortRawMapped` read the RAM flag live. At the next reboot `loadSerialMonitorMappings()` reads `sm0_raw = false` and raw mode silently reverts — binary data on S3 is line-parsed and corrupted again.

**Suggested fix.** Persist the change where it is made: call `saveSerialMonitorMappings()` inside the `if (mapping->rawMode != inputRawMode)` block at :1663-1665, or track a local `bool changed` and OR it into the :1752 condition. Then either delete `setSerialMappingRawMode()` or wire it to a command.

### F-383 · S2 · `Code/WCB/WCB.ino:1953`

**Backup checksum is written with the live function identifier but both verifiers hardcode "?CHK" — a custom-funcChar board's own backup can never be verified**

*Category:* `command-dispatch`

`printBackupConfig` builds the checksum token from the LIVE identifier:
```
5772:    String checksumCmd = lfi + "CHK" + String(crcStrBuf);      // lfi = String(LocalFunctionIdentifier)
5779:    String chainedWithChecksum = chainedConfig + String(commandDelimiter) + checksumCmd;
```
Both restore-side verifiers search for a hardcoded `'?'`:
```
1953:    int chkPos = data.lastIndexOf(String(commandDelimiter) + "?CHK");
1955:      chkPos = data.lastIndexOf(String(commandDelimiter) + "?chk");
6440:        String chkPattern = String(commandDelimiter) + "?CHK";
6441:        int chkPos = data.indexOf(chkPattern);
```
Note both correctly use the live `commandDelimiter` on the same line — the `'?'` is the only literal, so this is a straight omission, not a deliberate fixed grammar. `buildConfigString` (WCB.ino:2957) also hardcodes `"^?CHK"`, but that path is the always-'?' Wizard grammar and is documented as such at :2939-2942.

On a board where `?FUNCCHAR,x` has been applied, the emitted chain ends `…^xCHK1A2B3C4D`. `lastIndexOf("^?CHK")` returns -1, the whole verification block at :1958-2007 is skipped, the chain is split normally on `^`, and the final token is dispatched: `handleSingleCommand:4380` matches 'x', `processLocalCommand("CHK1A2B3C4D")` finds no `rootUpper` match and no legacy-chain match, and prints "Unknown command: CHK1A2B3C4D".

**Failure scenario.** A user has set `?FUNCCHAR,x` (a supported, backed-up, NVS-persisted setting — emitted by `collectConfigCommands` at WCB.ino:2799). They run `xbackup`, copy the "For Configured Boards" line into a text file, and later paste it back after a board swap. Not one of the ~40 config commands is checksum-verified: neither "✓ Command checksum VERIFIED" nor "✗ Command checksum FAILED" is printed, the restore proceeds regardless of paste truncation or corruption, and the only anomaly is a trailing "Unknown command: CHK…" that reads like harmless noise. A chain truncated by a terminal paste buffer is applied silently and partially — exactly what the checksum exists to prevent.

**Suggested fix.** Build the search pattern from the live identifier in both verifiers: at WCB.ino:1953/1955 and :6440/:6443 use `String(commandDelimiter) + String(LocalFunctionIdentifier) + "CHK"` (and the lowercase variant), keeping the literal `"^?CHK"` fallback so a factory-chain paste onto a custom-funcChar board still verifies. Add a matching note to the comment block at :1949-1952.

### F-384 · S2 · `Code/WCB/WCB.ino:2053`

**Factory-reset backup chain: tokens after ?FUNCCHAR carry the NEW func char, but the ?SEQ,SAVE / ?CS / ?MGMT splitter matches on the RECEIVING board's LocalFunctionIdentifier — stored sequences are split on ^ and their tails executed as live commands**

*Category:* `config-roundtrip`

`printBackupConfig()`'s emitter flips the factory chain's prefix mid-string once ?FUNCCHAR has been emitted:
```
5744: auto emit = [&](const String &c, bool includeInLive) {
5748:     chainedConfigDefault += (chainedConfigDefault.isEmpty() ? String("") : defaultSep) + defaultFunc + c;
5749:     if (c.startsWith("FUNCCHAR,")) defaultFunc = String(LocalFunctionIdentifier);
```
`FUNCCHAR` is emitted at WCB.ino:2800 (`if (LocalFunctionIdentifier != '?') emit("FUNCCHAR," + String(LocalFunctionIdentifier), false);`) — early in `collectConfigCommands`. Stored sequences are emitted much later, at WCB.ino:2910 (`emit("SEQ,SAVE," + key + "," + value, true)`). So in the "For Factory Reset/Fresh Boards" chain every SEQ,SAVE token is prefixed with the SOURCE board's custom func char, e.g. `#SEQ,SAVE,wave,;M1,1^;t500^;M1,2`.

The restore splitter special-cases SEQ,SAVE precisely because the value contains bare `^`:
```
2053:      if (restUpper.startsWith(String(LocalFunctionIdentifier) + "SEQ,SAVE,")) {
2058:        String nextFuncBoundary = String(commandDelimiter) + String(LocalFunctionIdentifier);
```
Both the match and the boundary search use `LocalFunctionIdentifier`, which on the fresh target board is still `'?'` — `parseCommandsAndEnqueue` splits the ENTIRE pasted string up front (WCB.ino:1945-2114), before any queued command executes, so the `?FUNCCHAR,#` in the chain has not taken effect yet. `#SEQ,SAVE,...` therefore fails the startsWith at :2053, falls through to the generic branch at :2095-2113, and is split on every `^`. The identical mismatch applies to the `?CS` branch (:2015-2016) and the `?MGMT,` branch (:2082).

The LIVE ("For Configured Boards") chain is unaffected — every token there uses `lfi` (:5745-5747), which equals the receiving board's LocalFunctionIdentifier. This is factory-chain-only, and is distinct from the already-recorded custom-FUNCCHAR checksum gap (docs/CODE_REVIEW_2026-08_wave1.md:475) — different function, different consequence.

**Failure scenario.** A board has `?FUNCCHAR,#` set (supported and documented — WCB_Help.cpp:886-898) and a stored sequence `wave` = `;M1,1^;t500^;M1,2`. Operator runs ?BACKUP and pastes the factory-reset chain onto a replacement board. The splitter does not recognise `#SEQ,SAVE,wave,;M1,1` as a SEQ,SAVE token, so it enqueues that fragment plus `;t500` and `;M1,2` as three separate commands. Result: the sequence is stored truncated to `;M1,1`, and the tail (`;t500`, `;M1,2`) is EXECUTED live during the restore — moving a real servo mid-restore. The board reports each as a normal command; nothing indicates the sequence was mangled.

**Suggested fix.** Make the three special-case matchers accept either the board's current LocalFunctionIdentifier or a literal '?', since the factory chain is defined to start with '?': test `startsWith(String(LocalFunctionIdentifier) + "SEQ,SAVE,") || startsWith("?SEQ,SAVE,")` at :2053 (and the equivalent at :2015 and :2082), and search for both `commandDelimiter+LocalFunctionIdentifier` and `commandDelimiter+'?'` when locating the next boundary at :2058, taking whichever comes first. Alternatively stop flipping `defaultFunc` at :5749 and keep the whole factory chain on '?' — the func char is dispatched by the token's own content, not its prefix.

### F-385 · S2 · `Code/WCB/WCB.ino:3088`

**Config/stats/seq/ETM fragment reassembly does multi-KB String building and a blocking Serial.printf inside the ESP-NOW receive callback**

*Category:* `concurrency`

Five fragment reassemblers are dispatched INLINE from `espNowReceiveCallback` (WiFi task) at `WCB.ino:3653-3657`:

```
if      (ptype == PACKET_TYPE_CONFIG_FRAG) handleConfigFragPacket(incomingData);
else if (ptype == PACKET_TYPE_STATS_FRAG)  handleStatsFragPacket(incomingData);
else if (ptype == PACKET_TYPE_SEQ_FRAG)    handleSeqFragPacket(incomingData);
else if (ptype == PACKET_TYPE_SEQVAL_FRAG) handleSeqValFragPacket(incomingData);
else if (ptype == PACKET_TYPE_ETM_FRAG)    handleETMFragPacket(incomingData);
```

On the final fragment each one does, in WiFi-task context (`WCB.ino:3082-3089` for CONFIG; the same shape at `:3324-3329`, `:3359-3364`, `:3394-3399`, `:3429-3434`):

```
String fullConfig = "";
for (int i = 0; i < pkt.totalChunks; i++) fullConfig += String(pullSession.chunks[i]);
...
Serial.printf("[MGMT:CONFIG,%d]%s\n", srcWCB, fullConfig.c_str());
```

Two distinct problems, both cross-file:

1. **Blocking UART from the WiFi task.** `MGMT_MAX_CHUNKS`=16 and `CONFIG_PAYLOAD_SIZE`=183 (`WCB.ino:252`, `:2645-ish`), so `fullConfig` reaches 2912 bytes. At 115200 baud that `Serial.printf` blocks for ~253 ms. `Serial` is `WCBDebugSerial` (`WCB_RemoteTerm.h:106-107`), and the repo states this rule explicitly at `WCB_RemoteTerm.cpp:140-142`: "Called from the ESP-NOW receive callback (WiFi task context). MUST NOT do blocking serial I/O here — doing so starves the WiFi stack and triggers the watchdog, crashing the board." The same rule is restated at `WCB.ino:3244-3250` (why CONFIG_REQ/STATS_REQ/ETM_REQ are deferred via `mgmtReqQueue`) and at `WCB.ino:363-366` (why RC JSON goes through `rcJsonRelayQueue`: "we're on the WiFi task (Core 0) and Serial isn't atomic across cores"). The REQUEST side was deferred; the RESPONSE side never was.

2. **~32 heap operations growing to 2912 bytes on the WiFi stack.** Each `String(pullSession.chunks[i])` is a 184-byte temporary (well past the 14-char SSO limit in `WString.h:322-346`, so all heap), and each `+=` reallocs the accumulator. This is the largest single heap burst in the firmware and it happens in the one context that must stay short.

Because `Serial` output is not atomic across cores, this 2912-byte write can also interleave with core-1 output and garble the `[MGMT:CONFIG,n]…` line the Wizard parses — the exact failure mode `otaRelayPrint` (`WCB.ino:352`) exists to prevent for OTA ACK tokens.

**Failure scenario.** The Wizard issues `?MGMT,PULL,<n>` against a remote board over a relay. The target broadcasts 16 CONFIG_FRAG packets twice (WCB.ino:3013-3037). On the relay, the frame completing the session lands in `espNowReceiveCallback`; `handleConfigFragPacket` builds a 2912-byte String and blocks the WiFi task in `Serial.printf` for ~250 ms. Every ESP-NOW frame arriving in that window — the second pass's fragments, ETM heartbeats, ETM ACKs, OTA DATA packets if a transfer is concurrent — is dropped by the driver. In the OTA case a dropped ACK stalls the browser until the session timeout, which is the '~70% hang' the comment at WCB.ino:344-348 already describes for the queue-overflow variant of this same problem.

**Suggested fix.** Give the response side the same treatment the request side already has. Either (a) push the completed `fullResult` through `enqueueRcJsonRelay()` / `otaRelayPrint()` so loop() emits it on core 1, or (b) add the reassemblers to a loop-drained queue alongside `drainMgmtReqs()` (WCB.ino:7491). Option (a) needs the relay slot widened past `RC_JSON_MAX_LEN`=220 (WCB.ino:328) or the line emitted in pieces; option (b) is closer to the existing `mgmtReqQueue` pattern and avoids the String accumulation in the callback entirely, since the reassembly buffers are already global.

### F-386 · S2 · `Code/WCB/WCB.ino:3099`

**sendResultFrags silently drops any relayed result over 2912 chars — ?MGMT,STATS and ?MGMT,ETM both exceed it on a full 20-board mesh**

*Category:* `capacity`

`sendResultFrags` (`WCB.ino:3095`) is the single transport for every relayed query result. Its ceiling is `MGMT_MAX_CHUNKS * (CONFIG_PAYLOAD_SIZE - 1)` = 16 × 182 = 2912 chars, and exceeding it is a silent no-op:

```
WCB.ino:3099   if (totalChunks > MGMT_MAX_CHUNKS) {
WCB.ino:3100     if (debugMGMT) Serial.printf("[MGMT] Result too large (%d chars) — cannot relay\n", totalLen);
WCB.ino:3101     return;
WCB.ino:3102   }
```

The diagnostic is gated behind `debugMGMT`, which is off by default, so the requester simply times out with no local or remote trace.

Two producers feed it, and both scale with `MAX_WCB_COUNT` = 20 (`WCB.ino:120`):

**`buildStatsString()` (WCB.ino:1639), reached via `?MGMT,STATS,<n>` → `handleStatsReqPacket` (WCB.ino:3126-3134) → `sendResultFrags(result, …, PACKET_TYPE_STATS_FRAG)`.** Its two per-board loops both run `for (int b = 0; b < MAX_WCB_COUNT; b++)`:
- ETM per-board row (`WCB.ino:1696-1702`): `"WCB12: Sent: 45231, ACKd: 45102, Retries: 331, Failed: 129, Online (last seen 12s ago)\n"` ≈ 87 chars × up to 19 peers ≈ 1653
- Reported-by-other-nodes row (`WCB.ino:1729-1733`): `"WCB12: Sent: 45231, ACKd: 45102, Retries: 331, Failed: 129, Unguaranteed: 812, Bcast: 9022, Recv: 51003  (7s ago)\n"` ≈ 115 chars × up to 20 reporters ≈ 2300
- headers/summary/footers ≈ 320

With 19 active peers the base is already ≈1970 chars, leaving room for only about 7 `?STATS,RPT` reporter rows before the 2912 ceiling is hit.

**`buildETMCharResultsString()` (WCB.ino:1791), reached via `?MGMT,ETM,<n>` → `printETMCharResults` (WCB.ino:1839) → `sendResultFrags(results, etmCharRelayRequesterWCB, PACKET_TYPE_ETM_FRAG)` at WCB.ino:1853.** Its row (`WCB.ino:1815-1817`) is ≈55 chars (74 with " (retries detected)") and it is emitted for 3 phases × peerCount: 3 × 19 × 55 ≈ 3135 chars of rows alone, before the ~460 chars of phase headers and the recommendation trailer. It crosses 2912 at roughly 15 peers.

This is the previously-shipped 'silently excluded above some index' class, one layer up: the loops correctly iterate all 20 boards, but the transport cannot carry the result they produce.

**Failure scenario.** A 20-board mesh with ETM enabled and several boards pushing `?STATS,RPT`. The operator opens the Wizard, selects a remote board, and requests its ESP-NOW statistics. The target's `handleStatsReqPacket` builds a ~4500-char string, `sendResultFrags` computes totalChunks=25 > 16, logs nothing (debugMGMT off) and returns. No STATS_FRAG packets are ever sent; the relay's `statsRelaySession` never opens, `checkConfigPullTimeout` eventually discards nothing, and the Wizard's stats pane spins until its own timeout. The same board answers fine on a 6-board bench mesh, so the failure only appears at fleet scale. `?MGMT,ETM,<n>` fails identically at ~15+ peers.

**Suggested fix.** Either raise the transport ceiling (increase `MGMT_MAX_CHUNKS` from 16 and widen `receivedMask` from `uint16_t` to `uint32_t` at WCB.ino:855 and WCB.ino:869 — the reassemblers already validate `chunkIdx < MGMT_MAX_CHUNKS`), or make the producers respect the budget: have `buildStatsString`/`buildETMCharResultsString` take a max-length argument and emit a truncation marker. At minimum, promote the WCB.ino:3100 diagnostic out from behind `debugMGMT` and send a short `TOOBIG` reply frag — `handleSeqValReqPacket` already does exactly this at WCB.ino:3217-3224, so the pattern exists in-file.

### F-387 · S2 · `Code/WCB/WCB.ino:4402`

**The endsWith("?") help intercept makes every "set the character back to ?" command impossible — including the exact examples the built-in help prints**

*Category:* `command-dispatch`

`processLocalCommand` begins with an unconditional help intercept, before the comma split and before any verb test:
```
4402:    if (message.endsWith("?")) {  // no space required
4403:        String cmd = message.substring(0, message.length() - 1);
4404:        cmd.trim();
4405:        printCommandHelp(cmd);
4406:        return;
4407:    }
```
`cmd.trim()` strips whitespace only, so `"FUNCCHAR,?"` becomes `"FUNCCHAR,"` — which matches none of `printCommandHelp`'s `c == "…"` tests (`WCB_Help.cpp:886` tests `c == "FUNCCHAR"`, comma included in the input) — and the function returns without ever reaching the setter at WCB.ino:5045.

This kills, unconditionally and regardless of which prefix character carries the command:
- `?FUNCCHAR,?` — printed verbatim as an example at `WCB_Help.cpp:898` ("Set to ? (default)")
- `?LF?` — printed as the legacy form at `WCB_Help.cpp:905`
- `?CMDCHAR,?` and `?CC?` — the `?CMDCHAR` legacy form at `WCB_Help.cpp:933`
- `?D?` (legacy `?DELIM`) — reaches the length-2 `'d'` branch at WCB.ino:5202 only if it survives :4402, which it does not
- any `?SEQ,SAVE,<key>,<value>` / `?LABEL,Sx,<text>` / `?ALIAS,<text>` whose value ends in `?`

(The `?FUNCCHAR,?` half of this is already logged in `docs/CODE_REVIEW_2026-08.md:258-274`; the legacy `?LF?`/`?CC?`/`?D?` forms — all three printed as help examples — and the value-ending-in-`?` class are not.)

**Failure scenario.** A board was set to a custom identifier with `?FUNCCHAR,x`. The operator reads `?FUNCCHAR ?`, sees the help line `?FUNCCHAR,?    - Set to ? (default)` at WCB_Help.cpp:898, and types `xFUNCCHAR,?`. handleSingleCommand:4380 matches the 'x' prefix, calls `processLocalCommand("FUNCCHAR,?")`, :4402 fires, generic help scrolls past, and the identifier is still 'x'. Every documented spelling of the operation fails the same way. Separately: `?SEQ,SAVE,ask,;S1Are you there?` is silently discarded as a help request rather than stored.

**Suggested fix.** Make the help intercept require that the `?` is the whole argument or is preceded by whitespace, e.g. only fire when `message == "?"`, `message.endsWith(" ?")`, or the trimmed remainder contains no comma. A minimal version: `if (message.endsWith("?") && message.indexOf(',') == -1) { … }` plus an explicit whitespace form (`"FUNCCHAR ?"`) for topic help, which still leaves `?FUNCCHAR,?` reaching :5045 and fixes the value-ending-in-? class at the same time.

### F-388 · S2 · `Code/WCB/WCB.ino:5046`

**?FUNCCHAR accepts ';' with no conflict check, and handleSingleCommand tests the function identifier first — the entire ';' command family is permanently shadowed**

*Category:* `command-dispatch`

`handleSingleCommand` dispatches strictly in this order (WCB.ino:4380-4390):
```
if (cmd.startsWith(String(LocalFunctionIdentifier))) { processLocalCommand(cmd.substring(1)); }
else if (cmd.startsWith(String(CommandCharacter))) { processCommandCharcter(cmd.substring(1), sourceID); }
else { processBroadcastCommand(cmd, sourceID); }
```
The `LocalFunctionIdentifier` test wins whenever the two characters are equal. `?FUNCCHAR,x` (WCB.ino:5045-5053) assigns `LocalFunctionIdentifier = args.charAt(0)` on the sole condition `args.length() == 1` — no check against `CommandCharacter` or `commandDelimiter` — and immediately persists it via `saveLocalFunctionIdentifierAndCommandCharacter()` (`WCB_Storage.cpp:518-522`, `putString("LocalFunc", …)`), which `loadLocalFunctionIdentifierAndCommandCharacter()` (`WCB_Storage.cpp:503-513`) restores at every boot. `?CMDCHAR,x` at :5057-5065 is equally unchecked.

The constraint is documented but not enforced: `WCB_Help.cpp:901` prints `"  - Must not conflict with CommandCharacter (;)"` under `?FUNCCHAR`, and `WCB_Help.cpp:930` prints the mirror note under `?CMDCHAR`. Nothing in the firmware acts on it. `?FUNCCHAR,^` (equal to the default `commandDelimiter`) is accepted the same way, which additionally makes every chained restore split on the prefix character.

**Failure scenario.** Operator issues `?FUNCCHAR,;` (a plausible typo for `?CMDCHAR,;`, or a hand-edited config chain). From that instant `;S1open`, `;M11`, `;Cwave`, `;A,PLAY,3`, `;w2,…` and every stored sequence body are routed to `processLocalCommand` instead of `processCommandCharcter`: `;M11` becomes root `M11`, falls through the legacy chain and is answered with "Unknown command"; `;S1open` reaches the `startsWith("s")` catch-all at :5343 and is swallowed by `updateSerialSettings` with no output at all. The setting is in NVS, so a power cycle does not clear it, and every WDP/mesh-delivered runtime command from other boards dies the same way. Recovery requires knowing to type `;ERASE,NVS` or `;FUNCCHAR,x` — and `;FUNCCHAR,?` cannot work either (see the endsWith("?") finding).

**Suggested fix.** Enforce the note the help already prints. In `?FUNCCHAR` (WCB.ino:5046) reject `args.charAt(0) == CommandCharacter || args.charAt(0) == commandDelimiter`, and in `?CMDCHAR` (:5058) reject `args.charAt(0) == LocalFunctionIdentifier || args.charAt(0) == commandDelimiter`, printing which character it collides with. Add the same guard to the legacy `?LFx`/`?CCx` paths (`updateLocalFunctionIdentifier` WCB.ino:5388, `updateCommandCharacter` :5398) and to `setCommandDelimiter` (`WCB_Storage.cpp:` `?DELIM`).

### F-389 · S2 · `Code/WCB/WCB.ino:5894`

**;S<port>,<msg> sends a literal leading comma — the separator every sibling runtime verb strips**

*Category:* `command-dispatch`

`processSerialMessage` (WCB.ino:5880) takes the port from `message.substring(1, 2)` (:5887) and then the payload from `message.substring(2)` (:5894) with only `serialMessage.trim()` (:5895) — Arduino `String::trim()` strips `isspace()` only, never a comma. `writeSerialString` (:1862-1867) appends `'\r'` and writes the string byte-for-byte, so `;S1,TEST` puts `,TEST\r` on the wire, not `TEST\r`.

Every other comma-form runtime verb in the same dispatcher explicitly strips the separator:
- `;A` → `WCB_MP3.cpp:98`  `if (rest.startsWith(",")) rest = rest.substring(1);`
- `;D` → `WCB_DFP.cpp:74`  same line
- `;L` → `WCB_WLED.cpp:122` same line
- `;H` → `WCB_HCR.cpp` `processHCRRuntimeCommand` strips `"H,"` vs `"H"` explicitly (`body[1] == ','` test)
- `;V` → `WCB_Variables.cpp:288` requires `m[1] == ','` and slices from index 2

`;S` and `;P` are the only two that do not. The comma form is what the shipped help and the test plan tell users to type: `WCB_Help.cpp:920` `";Sx,msg       Send msg to serial port x"`, `WCB_Help.cpp:1058` same, `TestConfigs/TestPlan.md:115` `"From WCB2: ;S1,TEST"` with expected result `"TEST appears at device on WCB2 S1"`, and `docs/SEQUENCE_INVENTORY.md:25` stores `;S5,<CA1021>` as a worked example. Meanwhile the firmware's own internal forwarder builds the comma-less form: `WCB.ino:6275` `String espnowCmd = ";S" + String(destPort) + cmd;`. So the two conventions are in the same codebase and only one of them works.

**Failure scenario.** A Marcduino/Stealth-style device is on S5. The user follows `docs/SEQUENCE_INVENTORY.md:25` and stores `?SEQ,SAVE,wave,;M1,1^;S5,<CA1021>`. On recall, S5 receives `,<CA1021>\r`. The device's line parser sees `,` as the first character, does not recognise `<` as the command opener, and discards the whole line. Nothing is logged on the WCB (the write succeeded), so the user concludes the wiring or the device is at fault. Typing `;S5<CA1021>` (no comma) works, which is not the documented form.

**Suggested fix.** Strip one optional separator comma in `processSerialMessage`, matching the four siblings: after `String serialMessage = message.substring(2);` at WCB.ino:5894 add `if (serialMessage.startsWith(",")) serialMessage = serialMessage.substring(1);` before the `trim()`. If instead the comma is meant to be payload, fix `WCB_Help.cpp:920`/`:1058`, `TestConfigs/TestPlan.md:115-117,190` and `docs/SEQUENCE_INVENTORY.md:25` to show `;Sxmsg` — but the four siblings make the strip the consistent choice.

### F-390 · S2 · `Code/WCB/WCB.ino:6191`

**;Px,width as documented cannot work, and its rejection is silent because debugPWMEnabled is never assigned anywhere**

*Category:* `command-dispatch`

`processPWMOutput` parses a comma-less grammar:
```
6188:    // Format: ;Pxnnnn where x=port, nnnn=pulse width in microseconds
6190:    int port = message.substring(1, 2).toInt();
6191:    unsigned long pulseWidth = message.substring(2).toInt();
6193:    if (port < 1 || port > 5 || pulseWidth < 500 || pulseWidth > 2500) {
6194:        if (debugPWMEnabled) Serial.println("Invalid PWM output command");
6195:        return;
6196:    }
```
The built-in help documents the comma form: `WCB_Help.cpp:925` prints `"  ;Px,width     Output PWM pulse on port x"`. For `;P4,1500`, `message.substring(2)` is `",1500"` and `String::toInt()` (atol) yields 0, so the `pulseWidth < 500` test rejects it.

The rejection produces no output at all. `debugPWMEnabled` is declared at WCB.ino:177 and read at exactly one place, :6194 — a full-tree grep (`grep -rn debugPWMEnabled Code/WCB/`) finds no assignment anywhere. `?DEBUG,PWM,ON` (:4436-4438) and the legacy `?DPWMON` (:5167-5168) both set the *different* flag `debugPWMPassthrough`, which is only consulted in `WCB_PWM.cpp:696,718,726` (the passthrough path), while printing "PWM debugging enabled".

**Failure scenario.** A user wires a servo/ESC to S4 and follows `?CMDCHAR ?` help, sending `;P4,1500`. Nothing happens on the pin and nothing prints. They enable `?DEBUG,PWM,ON`, which reports "PWM debugging enabled", and retry — still nothing, because that command set `debugPWMPassthrough`, not the flag guarding line 6194. The same silent path also hides genuine operator errors (`;P6,1500`, `;P4,3000`). The undocumented working spelling is `;P41500`.

**Suggested fix.** Accept the documented separator: after `int port = …` at WCB.ino:6190, take the width from a comma-tolerant slice, e.g. `String w = message.substring(2); if (w.startsWith(",")) w = w.substring(1); unsigned long pulseWidth = w.toInt();`. Separately, either delete `debugPWMEnabled` (WCB.ino:177) and gate :6194 on `debugPWMPassthrough`, or have `?DEBUG,PWM,ON`/`?DPWMON` set both flags — the current state means one command claims to enable two things and enables one.

### F-391 · S2 · `Wizard/app.js:3784`

**Wizard mapping-destination WCB dropdown is built from the WCBQ floor, so any destination above it is silently rewritten to "Local"**

*Category:* `board-count-cap`

`appendMappingDestination()` builds the destination-board `<select>` from the WCBQ floor only:

```js
3782:  const wcbQty    = parseInt(document.getElementById('g-wcbq')?.value) || systemConfig?.general?.wcbQuantity || 1;
3783:  const localWCB  = boardConfigs[n]?.wcbNumber || n;
3784:  const wcbOptions = Array.from({length: wcbQty}, (_,i) => {
3785:    const num = i + 1;
3786:    if (num === localWCB) return `<option value="0" ${dest.wcbNumber===0?'selected':''}>Local</option>`;
3787:    return `<option value="${num}" ${dest.wcbNumber===num?'selected':''}>WCB ${num}</option>`;
3788:  }).join('');
```

The firmware accepts a mapping destination anywhere in 1..MAX_WCB_COUNT — `Code/WCB/WCB_Storage.cpp:1717` rejects only `wcbNum > MAX_WCB_COUNT` — and MAX_WCB_COUNT is 20 (`Code/WCB/WCB.ino:120`). WDP auto-join deliberately admits boards **above** the WCBQ floor (`Code/WCB/WCB.ino:6918-6924` `addActivePeer(id, learned)`, and the Wizard itself renders them: `Wizard/app.js:433-437` `desiredBoardNumbers()` unions `_meshBoards` into the grid). So `wcbQty` is not the board-number domain.

When `dest.wcbNumber > wcbQty` no `<option>` carries `selected`, so the browser falls back to the first option — `Local` (value 0) when `localWCB === 1`, otherwise `WCB 1`.

The value is then read straight back out of the DOM and committed to the config:

```js
3607:    const destinations = [];
3608:    row.querySelectorAll('[id^="map-dest-"]').forEach(destRow => {
3609:      const wcb  = parseInt(destRow.querySelector('[id$="-wcb"]')?.value)  || 0;
...
3614:    config.mappings.push({ type, sourcePort: src, rawMode: raw, bidir, destinations });
```

`syncMappingsToConfig()` (Wizard/app.js:3584) unconditionally rebuilds `config.mappings` from the DOM, and it is called from `onMappingChange` (`:3572`), `removeMappingRow` (`:3635`), `_removeBidirRows` for **every** board 1..20 (`:3667-3676`), `applyBidirMapping` (`:3725`) and `saveMappingRow` (`:3827`). `saveMappingRow` then emits the command from those same reads (`:3829-3839`): `cmd += dest.wcbNumber === 0 ? ",S${dest.port}" : ",W${dest.wcbNumber}S${dest.port}"`.

That the correct pattern was known is visible three thousand lines away — `populateUIFromConfig` explicitly widens the range for the same class of dropdown, with a comment saying so:

```js
2927:    const qty = Math.max(
2928:      systemConfig?.general?.wcbQuantity || 0,
2929:      parseInt(document.getElementById('g-wcbq')?.value) || 0,
2930:      config.wcbQuantity || 0,
2931:      selectedWcb   // always include the board's self-reported number
2932:    );
```

`appendMappingDestination` never got that treatment.

**Failure scenario.** WCBQ floor is 2; WDP auto-join has learned WCB 12, so the Wizard renders sections 1, 2 and 12. WCB1's firmware holds `?MAP,SERIAL,S3,W12S1` (raw serial from S3 relayed to WCB12's S1). Pulling WCB1 parses the destination correctly (`Wizard/parser.js:961` `/^W(\d+)S(\d+)$/`) into `{wcbNumber:12, port:1}`, but `appendMappingDestination` renders only options `Local` and `WCB 2`, so the select displays `Local`. The user then toggles the bidir checkbox on an unrelated mapping row — `_removeBidirRows` (:3667) runs `syncMappingsToConfig(n)` for every board, which reads `0` out of that select and rewrites the destination to `{wcbNumber:0, port:1}`. Save/Push emits `?MAP,SERIAL,S3,S1`: the S3 stream is now looped to this board's own S1 instead of WCB12, and the original mapping is gone from both the config and the baseline. Nothing is logged or toasted.

**Suggested fix.** Widen the option list the same way `populateUIFromConfig` does — `const wcbQty = Math.max(parseInt(g-wcbq)||0, systemConfig?.general?.wcbQuantity||0, dest.wcbNumber||0, ...Object.keys(boardConfigs).map(Number))`, or simply build 1..WCB_MAX (the constant already exists at Wizard/app.js:1081) since the firmware accepts the whole range. At minimum, always append an option for `dest.wcbNumber` when it is outside the generated range so a pulled value can round-trip.

### F-392 · S2 · `Wizard/parser.js:902`

**The Wizard drops ?WDP,OFF / ?WDP,AUTOJOIN,OFF from a pulled config and never re-emits them — an export/restore silently re-enables discovery**

*Category:* `config-roundtrip`

The firmware deliberately puts the WDP opt-outs into every config chain, and says why:
```
WCB.ino:2886-2893
  // WDP settings — both default ON, so only the OFF states need to ride the
  // config chain (otherwise a restore silently reverts a deliberate ?WDP,OFF /
  // ?WDP,AUTOJOIN,OFF back to ON on the replacement board)...
  if (!wdpEnabled)  emit("WDP,OFF", true);
  if (!wdpAutoJoin) emit("WDP,AUTOJOIN,OFF", true);
```
`parseToken()` in Wizard/parser.js has no `case 'WDP'`. The complete case list is HW, RELAY, LED, WCB, WCBQ, WCBCH, PEERSLIVE, CONTROLLER/SPECIAL, ALIAS, EPASS, MAC, DELIM, FUNCCHAR, CMDCHAR, BAUD, LABEL, BCAST, KYBER, MP3, HCR, DFP, WLED, MAESTRO, ETM, SEQ, VAR, MAP, CHK (parser.js:471-901). `?WDP,OFF` therefore falls into `default:` at parser.js:902-908 and is discarded to `console.debug`. `grep -n 'WDP' Wizard/parser.js` returns only line 506, a comment. There is no field in `createDefaultBoardConfig()` to hold it and `buildCommandString()` (parser.js:1219-1539) never emits a WDP token — I enumerated every `add(\`...\`)` call in that function and WDP appears in none of the 36.

The firmware accepts both spellings on input (`?WDP,ON`/`?WDP,OFF` at WCB_WDP.cpp:1218-1219, `?WDP,AUTOJOIN,ON|OFF` at :1223-1232, persisted by `saveWdpSettings()` at :1287-1291), so the break is entirely on the Wizard side.

NOTE: recorded previously as docs/CODE_REVIEW_2026-08_wave3.md:1556-1590; re-verified UNFIXED at current OTA head.

**Failure scenario.** Operator runs ?WDP,OFF on a board to stop it advertising and auto-adopting neighbours, then pulls it into the Wizard and clicks Export System File as a backup. The exported file contains no WDP line. When that board dies and the file is restored onto a replacement, WDP and auto-join come up ON (the firmware defaults at WCB_WDP.cpp:48-49), the replacement starts advertising and auto-joining peers, and the deliberate opt-out is gone with no warning.

**Suggested fix.** Add `wdpEnabled: true, wdpAutoJoin: true` to `createDefaultBoardConfig()`, a `case 'WDP':` in `parseToken()` that sets them from `WDP,OFF` / `WDP,AUTOJOIN,OFF` (and back from the ON forms), and mirror the firmware's emit in `buildCommandString()` — send only the OFF forms so a default board's chain is unchanged.


---

## S3

### F-393 · S3 · `.github/workflows/build.yml:18`

**build.yml has no concurrency group; two main pushes race the auto-version and one release is lost with main's bins built from the older source**

*Category:* `release-pipeline`

`build.yml` declares no `concurrency:` key anywhere (grep over `.github/workflows/*.yml` returns `concurrency` only at `pages-deploy.yml:43`, where the trade-offs are carefully reasoned out in a 10-line comment). Nothing serializes firmware builds.

Two pushes to `main` close together produce two overlapping runs. Both check out a tree whose `WCB.ino:181` still says the old semver, so both compute the *same* `NEW_SEMVER` at `:110` (`PATCH+1`) and, if they land in the same minute, the *same* DTG at `:111` and therefore the same tag at `:118`. The first to reach `:145` pushes its bins commit. The second is rejected, falls into the rebase branch at `:149-151`, and hits a guaranteed conflict — both commits rewrite the single `String SoftwareVersion = "…";` line at `WCB.ino:181` — so `git rebase` fails, `:151` exits 1, and the tag/release step is skipped by its implicit `success()`.

Net result for the second (newer) commit: no release, no tag, and `main`'s committed `Code/bin/*` are the *first* run's binaries — built from the older source — while `main`'s source is the newer commit. Since `Wizard/flasher.js:44` and `Wizard/app.js:127-133` both serve users straight out of `Code/bin` on `main`, users flash firmware that does not correspond to `main` until someone notices the red run and pushes again. The failure is loud (red X) but the recovery is manual and the intermediate state is silently wrong.

The same race on a feature branch is quieter: whichever run wins leaves bins that were compiled from a commit that is no longer the tip, with a green check and nothing recording the mismatch.

**Failure scenario.** Two commits pushed to `main` within a few minutes (e.g. a fix immediately after a merge). Run A auto-versions 6.2.0→6.2.1, builds, pushes bins, tags, releases. Run B (checked out at the later commit, source still 6.2.0) also computes 6.2.1, builds, is push-rejected, rebases onto A's commit, conflicts on `WCB.ino:181`, and exits 1 at `build.yml:151`. The second commit never gets a release and `main`'s published binaries stay A's.

**Suggested fix.** Add a per-branch concurrency group with `cancel-in-progress: false` (mirroring `pages-deploy.yml:43-53`) so builds on the same branch queue instead of racing: `concurrency: { group: fw-build-${{ github.ref_name }}, cancel-in-progress: false }`. Consider also re-reading the semver from `origin/main` after the rebase rather than from the stale checkout.

### F-394 · S3 · `.github/workflows/build.yml:83`

**WcbCmd is cloned unpinned at build time and no record of which commit shipped is kept anywhere in the release**

*Category:* `supply-chain`

`:82-83` `rm -rf "$LIBDIR/WcbCmd"` then `retry git clone --depth 1 https://github.com/greghulette/WcbCmd.git "$LIBDIR/WcbCmd"` — the default branch tip at build time, with no ref, tag or SHA. That the clone happens (rather than `arduino-cli lib install`) is deliberate and documented at `:74-77`, and I am not disputing it; the gap is that **nothing records what was pulled**. `arduino-cli lib list` at `:84` prints WcbCmd's `library.properties` version, not its commit, and the release is created with `--generate-notes` (`:181`) which only summarizes this repo's commits.

WcbCmd is the `;`-verb → device-bytes translator compiled into this firmware and into NaviCore (CLAUDE.md rules 1 and 6), so it directly determines the bytes that reach a Maestro/HCR/MP3 device. Consequence for a release audit: the binaries attached to tag `vX.Y.Z` are **not** reproducible from the tagged source of this repo, and a bad or half-finished push to WcbCmd master is silently baked into the next WCB release with no way — from the tag, the release notes, or the committed binaries — to tell which WcbCmd commit is in the image a user flashed. Re-running the same workflow on the same commit a week later produces different firmware.

**Failure scenario.** A WcbCmd translator change is pushed on Monday and reverted on Tuesday. Any WCB release built on Monday ships the reverted translator. A user reports wrong bytes on the wire; nothing in the tag, the Release, or `Code/bin` identifies which WcbCmd commit is in that image, so the only way to reproduce the reported behaviour is to guess at WcbCmd's history by date.

**Suggested fix.** Capture and surface the SHA: after the clone, `WCBCMD_SHA=$(git -C "$LIBDIR/WcbCmd" rev-parse HEAD); echo "wcbcmd_sha=$WCBCMD_SHA" >> "$GITHUB_OUTPUT"`, then append it to the release body (`--notes` alongside `--generate-notes`, or a `--notes-file`). For `main` specifically, consider cloning a tag (`--branch "$WCBCMD_TAG"`) so a release is reproducible; feature branches can keep tracking master.

### F-395 · S3 · `.github/workflows/build.yml:173`

**The release tag is pushed before the assets are proven to exist, and the "tag already exists" guard can never fire because checkout fetches no tags**

*Category:* `release-pipeline`

Step `Tag & publish release` (`:165-183`) does, in order: `:173` `if ! git rev-parse "$TAG" >/dev/null 2>&1;` → `:174-175` `git tag "$TAG"; git push origin "$TAG"` → `:177` `if ! gh release view "$TAG";` → `:178-182` `gh release create … Code/bin/WCB_${VERSION}_main_*.bin`.

Two defects:

**1. The `git rev-parse` guard is inoperative.** `actions/checkout@v4` (`:24`) runs with the default `fetch-depth: 1` and `fetch-tags: false`, i.e. it fetches with `--no-tags`, so the workspace has no tags at all. `git rev-parse "$TAG"` therefore *always* fails for a tag that exists only on the remote, the guard always falls through, and the recovery path is `git push origin "$TAG"` being rejected with "already exists" — which under `set -euo pipefail` (`:172`) hard-fails the step. The comment at `:162-163`, "Guards make it a no-op if the tag/release already exists (e.g. a manual re-run)", is wrong about the code: it is a hard error, not a no-op. (The second guard, `gh release view` at `:177`, does work — it queries the API.)

**2. The tag is pushed before anything validates the assets.** `:182` globs `Code/bin/WCB_${VERSION}_main_*.bin`. If that matches nothing — e.g. `build.sh`'s independently-computed `VERSION` (`Code/bin/build.sh:42`) disagrees with `steps.ver.outputs.version`, or the `cp`s failed the way finding #1 describes — bash passes the literal pattern to `gh`, `gh release create` errors, and the step dies *after* `:175` already pushed the tag. The repo is then left with a permanent `v6.2.x_…` tag pointing at a commit that has no Release, the version-shield badge on the README/wiki reads a version users cannot download, and clearing it requires manually deleting the remote tag.

**Failure scenario.** Any failure at `build.yml:177-182` after the tag push — the asset glob matching nothing being the realistic one — leaves `v6.2.1_181430RAUG2026` pushed to the remote with no GitHub Release attached. A re-run of that same workflow in the same minute then fails at `:175` (`tag already exists`) because `git rev-parse` at `:173` cannot see the remote-only tag, and the run can never self-heal without manual tag deletion.

**Suggested fix.** Validate before tagging: `shopt -s nullglob; assets=(Code/bin/WCB_${VERSION}_main_*.bin); [ ${#assets[@]} -ge 2 ] || { echo "::error::no release assets for ${VERSION}"; exit 1; }`, then create the release with `gh release create --target "$GITHUB_SHA"` (which creates the tag server-side atomically with the release) instead of `git tag`+`git push`. If the local guard is kept, either add `fetch-tags: true` to the checkout at `:25` or replace `git rev-parse` with `git ls-remote --exit-code --tags origin "$TAG"`, and fix the comment at `:162-163`.

### F-396 · S3 · `.github/workflows/pages-deploy.yml:103`

**Wizard Pages deploy dies silently (set -e + pipefail) the moment the Wizard stops referencing ../Images/**

*Category:* `ci-robustness`

The step runs under `set -euo pipefail` (`:67`). Line 103 is:
```
refs="$(grep -rhoE '\.\./Images/[A-Za-z0-9._-]+' "$WIZ" 2>/dev/null | sed 's#\.\./Images/##' | sort -u)"
```
`grep` exits 1 when it matches nothing. `sed` and `sort` exit 0, but `pipefail` makes the pipeline return grep's 1, the command substitution assignment therefore returns 1, and `set -e` terminates the step. The `2>/dev/null` only suppresses stderr, so the job fails with **no message at all** — the last thing in the log is the `cp -a` from `:95`.

Verified experimentally with the identical construct:
```
$ bash -c 'set -euo pipefail; echo before; refs="$(grep -rhoE NOMATCH /tmp 2>/dev/null | sed s#x#y# | sort -u)"; echo after'
before
(exit 1)
```
The author clearly did not intend this: `:104` is `if [ -n "$refs" ]; then` — an explicit empty-result guard that `pipefail` makes unreachable. Today there are exactly 6 `../Images/…` references in `Wizard/`, so the bug is latent; deleting the last logo reference (or renaming `Images/`) turns a cosmetic Wizard edit into a total, silent stop of production Pages deploys — the `/Wizard` slice simply freezes at its last good state while every push reports a red X with an empty log.

**Failure scenario.** Someone removes or inlines the last `../Images/<file>` reference in `Wizard/` (e.g. switching the logos to data: URIs). The next Wizard push runs pages-deploy, `grep` finds nothing, exits 1, `pipefail` propagates it, `set -e` kills the step before `git add -A`/`git push`. The production Wizard at `/Wizard` silently stops updating and the failure log shows no error line.

**Suggested fix.** Make the no-match case explicit: `refs="$(grep -rhoE … | sed … | sort -u || true)"`, or `refs="$( … )" || refs=""`. The existing `if [ -n "$refs" ]` at `:104` then does its job.

### F-397 · S3 · `c:/Users/ghulette/Documents/GitHub/WCBClient/src/WCB_Client.cpp:1337`

**deleteSequence(n, "all") wipes a board's entire sequence store — the client's key validator does not reserve the token**

*Category:* `data-loss`

WCB_Client::deleteSequence builds a plain `?SEQ,CLEAR,<key>` command (WCB_Client.cpp:1332-1339):
```
bool WCB_Client::deleteSequence(uint8_t wcbNumber, const char* key) {
    if (!_started)                                    return false;
    if (wcbNumber < 1 || wcbNumber > WCB_MAX_BOARDS)  return false;
    if (!wcbSeqKeyValid(key))                         return false;

    String cmd = "?SEQ,CLEAR,";
    cmd += key;
    return send(wcbNumber, cmd.c_str());
}
```
`wcbSeqKeyValid` (WCB_Client.cpp:1209-1214) rejects only empty keys, keys over 15 chars, and keys containing a comma:
```
static bool wcbSeqKeyValid(const char* key) {
    if (!key) return false;
    size_t n = strlen(key);
    if (n == 0 || n > WCB_SEQ_KEY_MAX) return false;
    return strchr(key, ',') == nullptr;
}
```
On the firmware side `?SEQ,CLEAR` case-insensitively reserves `ALL` as the wipe-everything token — WCB.ino:4984-4993:
```
        } else if (seqCmdUpper == "CLEAR") {
            String seqArgsUpper = seqArgs;
            seqArgsUpper.toUpperCase();
            if (seqArgsUpper == "ALL") {
                clearAllStoredCommands();
                Serial.println("All stored sequences cleared");
            } else {
                eraseStoredCommandByName(seqArgs);
            }
        }
```
The two ends disagree about the key namespace: the client treats `all` as an ordinary key and will happily send it, while the firmware treats it as a command. Nothing on the write side prevents a sequence from being named `all` in the first place — `saveStoredCommandsToPreferences` (WCB_Storage.cpp:625-664) has no reserved-name check either, so `?SEQ,SAVE,all,<value>` stores a real sequence under a key that can never be individually deleted.

**Failure scenario.** A board has a sequence stored under the key `all` (legal to create via `?SEQ,SAVE,all,...` — nothing rejects it). A controller built on WCB_Client enumerates the board's keys with `requestSequenceNames()` and calls `deleteSequence(n, "all")` to remove just that one. `wcbSeqKeyValid` passes it, the firmware uppercases the argument, matches `ALL`, and calls `clearAllStoredCommands()` — every stored sequence on that board is erased instead of the one requested, with no confirmation and no way to undo.

**Suggested fix.** Reserve the token on both sides. In WCB_Client, extend `wcbSeqKeyValid` (or add a check in `deleteSequence`) to reject a key that case-insensitively equals `"all"`, and document in the header that `all` is a reserved firmware token. In the firmware, reject `all` as a key in `saveStoredCommandsToPreferences` (WCB_Storage.cpp:637) so the ambiguous state cannot be created. A stricter alternative is to give the wipe its own unambiguous verb (e.g. `?SEQ,CLEARALL`) and keep `?SEQ,CLEAR,<key>` purely key-addressed, leaving `ALL` accepted only for backward compatibility.

### F-398 · S3 · `c:/Users/ghulette/Documents/GitHub/WCBClient/src/WCB_Client.cpp:1955`

**WCB_Client emits the TEMPORARY flag TLV last, so a full advert drops it and the WCB persists the peer permanently**

*Category:* `wire-format-divergence`

Both encoders share the same append helper, which silently SKIPS a TLV that does not fit rather than truncating: WCB_Client.cpp:1866-1868 and the identical WCB_WDP.cpp:216-218 —
```
    if (o + 2 + len > max) return o;   // no room — skip this TLV
```
Because of that, TLV ORDER decides what survives a full 200-byte payload. The WCB firmware states and follows a policy of putting functional TLVs before cosmetic ones — WCB_WDP.cpp:283-286:
```
  // PWM remote-output targets ... Advertised BEFORE the (cosmetic) port
  // labels so this functional config wins the payload budget.
```
WCB_Client's own comment claims to mirror it (WCB_Client.cpp:1946-1948: "Emitted AFTER the identity TLVs so, if the advert runs tight on space, wcbPutTLV drops trailing labels rather than the identity (mirrors the WCB firmware's policy)") — but the TEMPORARY flag is emitted AFTER those labels, at WCB_Client.cpp:1955-1958:
```
    if (_wdpTemporary) {   // tell WCBs to adopt this device as a TEMPORARY (not permanent) peer
        uint8_t flags = WCB_WDP_ADVFLAG_TEMPORARY;
        o = wcbPutTLV(buf, o, max, WCB_WDP_TLV_FLAGS, &flags, 1);
    }
```
So FLAGS is the last thing appended and the first functional TLV to be dropped — the opposite of the stated policy, and it is not cosmetic: it is the single bit that decides persistence.

Budget: `max` is `sizeof(pkt.structCommand)` = 200 (WCB_Client.cpp:1970, struct at WCB_Client.h:159). Worst-case fields are DEVTYPE `_wdpType[25]`, FWVER `_wdpFw[28]`, HWREV `_wdpHwRev[16]`, CAPTAGS `_wdpCaps[49]` (WCB_Client.h:1098-1101), MAESTRO (2+9), MAESTRO_CFG (2+18), and five PORTLABELs at 2+1+24 = 27 each (`_wdpPortLabels[5][25]`, WCB_Client.h:1102). That totals 2+26+29+17+50+11+20+135 = 290 bytes before FLAGS — far past 200. FLAGS needs only 3 bytes, so it is dropped whenever the running offset exceeds 197, which the five port labels alone can reach.

On the receiving WCB the missing bit flips the adoption branch — WCB_WDP.cpp:657-676: `if (nb.temporary)` takes `addTemporaryPeer()` (live, not persisted, evicted on silence), while the `else if` takes `addActivePeer((uint8_t)senderWCB, true)` — the permanent, NVS-persisted learned peer. WCB_WDP.h:20-24 and the header's `temporary` field document that a learned peer is never auto-evicted.

I confirmed the shipped `setTemporary(true)` user, examples/MgmtRelay/MgmtRelay.ino:737-738, calls only `setIdentity(RELAY_ALIAS, RELAY_FW)` with no hwRev, caps, labels or Maestros, so its advert is ~61 bytes and is not affected today. This is a latent trap for any temporary device that also labels its ports, not a currently-shipping failure.

**Failure scenario.** Someone builds a temporary device — a bench mesh monitor or a second management relay — that calls `setTemporary(true)` and also `setPortLabel()` on four or five ports with descriptive names (as NaviCore does at NaviCore.ino:3929). The port labels consume the payload, `wcbPutTLV` silently skips the 3-byte FLAGS TLV, and every WCB on the mesh takes the `else if` branch at WCB_WDP.cpp:673 and writes the device into its persisted learned-peer list. The device is unplugged; because learned peers never auto-evict, every board keeps a dead NVS peer entry forever, consuming an ESP-NOW peer slot (max 20) until someone runs `?WDP,FORGET` / `?WDP,CLEAR` on each board by hand.

**Suggested fix.** Move the FLAGS emission ahead of the PORTLABEL loop in `_buildWdpPayload` — emit it immediately after the MAESTRO/MAESTRO_CFG block (i.e. before WCB_Client.cpp:1945) so it sits with the functional TLVs, matching the firmware's documented ordering policy at WCB_WDP.cpp:283-286. It is 3 bytes and parse order is irrelevant on both receivers (the WCB handles FLAGS in a switch, WCB_WDP.cpp:539-541), so the change is purely defensive and cannot break an older peer.

### F-399 · S3 · `Code/bin/build.sh:42`

**build.sh's VERSION is unguarded and independently re-derived; an empty or mismatched value silently produces unversioned binaries after the tag is pushed**

*Category:* `release-pipeline`

The version string is computed **twice, independently**, and nothing cross-checks the two:
- `build.yml:106` `CUR=$(grep 'String SoftwareVersion =' "$INO" | head -1 | grep -oE '"[^"]*"' | head -1 | tr -d '"')` → bumped → `steps.ver.outputs.version`, used for the tag (`:118`) and the asset glob (`:182`).
- `Code/bin/build.sh:42` re-greps the same line for `VERSION`, which is what actually goes into every filename (`:183-201`).

`build.sh` has no `set -u` and no guard on `VERSION`. If the grep ever misses — a reformatted declaration, a `#define`, an editor writing CRLF — `VERSION` becomes the empty string and the script cheerfully emits `Code/bin/WCB__main_ESP32.bin`, exits 0, and CI commits it. Downstream:
- `build.yml:182`'s glob `Code/bin/WCB_6.2.1_…_main_*.bin` matches nothing → `gh release create` fails, **after** `:175` already pushed the tag (see the related finding).
- `Wizard/app.js:135` matches `/WCB_([\d.]+_\d{6}R[A-Z]{3}\d{4})_/` against the filename to populate `latestFirmwareVersion`; an unversioned name fails the match and `:136 return` leaves the whole "↑ update available" indicator permanently silent for every board — a pure no-op with no error path.
- `Wizard/flasher.js:127` matches by **suffix only** (`f.name.endsWith('_ESP32.bin')`), so users would still be served and flashed that unversioned binary quite happily.

Same class: nothing verifies the `sed` at `build.yml:113` actually changed anything. `sed -i` exits 0 on zero substitutions, so a declaration the pattern no longer matches yields a firmware that reports the old version while being tagged and released as the new one.

**Failure scenario.** `Code/WCB/WCB.ino:181`'s declaration is reformatted (e.g. `String SoftwareVersion="6.2.1";` with no spaces, or a stray CR). `build.yml:113`'s sed silently matches nothing and `build.sh:42`'s grep returns empty. The build succeeds, `Code/bin/WCB__main_ESP32.bin` is committed and pushed, the tag `v6.2.1_…` is pushed, then `gh release create` at `:178` fails on the unmatched glob. Result: a tag with no Release, a firmware image reporting the previous version, and a dead update indicator in the Wizard — all from a green compile.

**Suggested fix.** In `Code/bin/build.sh` after line 42: `[ -n "$VERSION" ] || { echo "✗ ERROR: could not read SoftwareVersion from WCB.ino"; exit 1; }`. In `build.yml`, after the sed at `:113` assert the stamp landed (`grep -q "String SoftwareVersion = \"${NEW_VER}\";" "$INO" || exit 1`), and before tagging assert `build.sh`'s output filenames actually carry `${VERSION}`.

### F-400 · S3 · `Code/bin/build.sh:53`

**build.sh uses fixed /tmp build directories and deletes them unconditionally at startup**

*Category:* `concurrency`

`:53-54` hard-code `TMP_ESP32="/tmp/wcb_esp32"` and `TMP_S3="/tmp/wcb_s3"`, and `:67` `rm -rf "$TMP_ESP32" "$TMP_S3"` runs unconditionally before either compile. The paths carry no run, branch or PID component.

In GitHub Actions each job gets its own VM, so CI is safe. But the header explicitly presents this as a developer script to "run from anywhere" (`:7-10`), and two concurrent local invocations — two branches in two worktrees, or a re-run started while the first is still compiling — collide destructively: the second run's `:67` deletes the first run's `--output-dir` mid-compile, so the first run's `arduino-cli` writes into an unlinked directory and its `cp`s at `:183-201` then fail (unchecked, see the first finding) or, worse, copy the *other* run's artifacts into this branch's version-named files. This repo has already been bitten by shared build state (the recorded rule: never run `arduino-cli` while a compile is in flight, because the shared sketch cache corrupts), so the class of failure is a known one here.

**Failure scenario.** Greg runs `Code/bin/build.sh` for branch OTA, then starts a second shell and runs it in another worktree for main. The second run's `rm -rf /tmp/wcb_esp32 /tmp/wcb_s3` at `:67` removes the first run's output dir while its ESP32 compile is still writing. The first run then either fails its unchecked `cp`s (exiting 0 with no binaries published) or publishes artifacts from the wrong branch under this branch's filenames.

**Suggested fix.** Derive the temp dirs per run: `TMPROOT="$(mktemp -d)"; TMP_ESP32="$TMPROOT/esp32"; TMP_S3="$TMPROOT/s3"`, and clean up with a `trap 'rm -rf "$TMPROOT"' EXIT` instead of the unconditional `rm -rf` at `:67` and the three scattered cleanups at `:126`, `:161`, `:171`, `:205`.

### F-401 · S3 · `Code/bin/build.sh:153`

**A missing python interpreter is reported as a corrupt S3 bootloader**

*Category:* `misleading-failure`

`:153` `PY_BIN="$(command -v python3 || command -v python)"` with no check. If neither interpreter exists, `PY_BIN` is empty and `check_boot` (`:156-174`) executes `if ! "$PY_BIN" - "$1" "$2" <<'PYEOF'` → the shell tries to run the empty string, prints `: command not found`, returns 127, the `!` inverts it, and the script takes the failure branch at `:169-173`:
```
✗ ERROR: <path> failed header sanity check (magic 0xE9 / 16MB size nibble 4).
```
Verified by running the exact construct:
```
$ PY_BIN="$(command -v python3ZZZ || command -v pythonZZZ)"; if ! "$PY_BIN" - f 4 <<'E'
…
t.sh: line 4: : command not found
BRANCH: header sanity check failed path taken
```
So the script does fail loudly (good) but with a diagnosis that points at the wrong artifact — and not a harmless one. The comment immediately above at `:143-148` explains that these are the hand-verified custom short-WDT bootloaders and that "a bootloader whose header declares the WRONG flash size silently corrupts NVS (the documented 4MB trap)". A developer who hits this on a bench machine is told his NVS-critical, hand-built bootloader is corrupt, when in fact `python` is simply not on PATH (a documented condition on this machine — CLAUDE.md notes Python is not currently on PATH, which is why the `wizard-static` launch config does not start).

**Failure scenario.** Greg runs `Code/bin/build.sh` locally in Git Bash with no python on PATH. Both targets compile fine, then the script prints `✗ ERROR: …/WCB_S3_custom_bootloader_16MB_wdt3s.bin failed header sanity check (magic 0xE9 / 16MB size nibble 4)` and exits 1, sending him to re-verify or rebuild a bootloader that is perfectly fine.

**Suggested fix.** Guard it explicitly after line 153: `[ -n "$PY_BIN" ] || { echo "✗ ERROR: no python3/python on PATH — cannot verify the S3 bootloader headers"; exit 1; }`. Alternatively drop the python dependency entirely and read the two header bytes with `od -An -tx1 -N4 "$1"`, which is available everywhere the rest of the script already runs.

### F-402 · S3 · `Code/WCB/WCB_Help.cpp:25`

**?DEBUG? topic omits four implemented debug subsystems and two legacy aliases**

*Category:* `help-coverage`

The `?DEBUG?` topic's Commands block (WCB_Help.cpp:25-35) lists six toggles: `ON`, `OFF`, `KYBER,ON/OFF`, `PWM,ON/OFF`, `ETM,ON/OFF`. The dispatcher at WCB.ino:4423-4470 implements ten:

```cpp
        } else if (argsUpper == "MAESTRO,ON" || argsUpper == "KYBER,ON") {   // :4430
            debugMaestro = true;
...
        } else if (argsUpper == "HCR,ON") {                                   // :4448
            debugHCR = true;
...
        } else if (argsUpper == "MGMT,ON") {                                  // :4454
            debugMGMT = true;
...
        } else if (argsUpper == "RAW,ON") {                                   // :4460
            debugRawSerial = true;
```

Missing from the topic: `MAESTRO,ON/OFF` (the canonical spelling — `KYBER` is merely its alias, both setting `debugMaestro`), `HCR,ON/OFF`, `MGMT,ON/OFF`, `RAW,ON/OFF`.

The top-level menu is inconsistent with the topic rather than a substitute for it — WCB_Help.cpp:1070-1071 advertises `?DEBUG,MAESTRO` and `?DEBUG,RAW`, neither of which the dedicated `?DEBUG?` page mentions, while neither surface mentions `HCR` or `MGMT`.

The topic's Legacy block (WCB_Help.cpp:45-48) has the matching gap — it lists `?DON/?DOFF`, `?DKON/?DKOFF`, `?DPWMON/?DPWMOFF`, `?DETMON/?DETMOFF` but omits `?DHCRON`/`?DHCROFF`, which are implemented at WCB.ino:5176-5181:

```cpp
    } else if (message.startsWith("dhcroff") || message.startsWith("DHCROFF")) {
        debugHCR = false;
...
    } else if (message.startsWith("dhcron") || message.startsWith("DHCRON")) {
        debugHCR = true;
```

(`?HCR?` does mention `dhcron`/`dhcroff` in passing at WCB_Help.cpp:655-657, so the legacy pair is documented — just not where a user looking for debug commands would find it.)

RAW is the most consequential omission: `debugRawSerial` gates the byte-level `[RAW]` tracing at WCB.ino:4145, :6689 and :6702, which is the only visibility into raw serial mapping and remote raw forwarding. A user debugging a raw `?MAP,SERIAL,Sx,R,dest` route has no documented way to turn it on from the page that is supposed to list debug commands.

**Failure scenario.** A user debugging a silent raw serial mapping opens `?DEBUG?` — the page whose entire purpose is enumerating debug toggles — and sees only main/KYBER/PWM/ETM. They conclude no byte-level tracing exists and resort to a logic analyser, when `?DEBUG,RAW,ON` (WCB.ino:4460) would have printed every forwarded byte at WCB.ino:6689.

**Suggested fix.** Add the four missing pairs to the Commands block, leading with MAESTRO as canonical and noting KYBER is its alias:

```cpp
Serial.println(F("  MAESTRO,ON/OFF    Maestro/Pololu serial packet debug (hex)"));
Serial.println(F("                      (KYBER,ON/OFF is an alias — same flag)"));
Serial.println(F("  HCR,ON/OFF        HCR command/status debug"));
Serial.println(F("  MGMT,ON/OFF       Remote-management protocol debug"));
Serial.println(F("  RAW,ON/OFF        Raw serial byte-level forwarding debug (hex)"));
```

and add `?DHCRON / ?DHCROFF  - HCR debug` to the Legacy block at WCB_Help.cpp:48.

### F-403 · S3 · `Code/WCB/WCB_Help.cpp:100`

**?PMSSx,dest — the legacy PWM-map form printed in the built-in help — is rejected by addPWMMapping**

*Category:* `command-dispatch`

`WCB_Help.cpp:100` prints `"  ?PMSSx,dest   - PWM map"` in the `?MAP` legacy-commands block, alongside `?SMSx,dest` (:96) and `?SMRSx` (:97), which do take an `S<port>` token.

The PWM dispatcher does not. WCB.ino:5253-5254:
```
} else if (message.startsWith("pms") || message.startsWith("PMS")) {
    addPWMMapping("PMS" + message.substring(3));
```
This is a pure case-normalising round trip: for `?PMSS3,S4` it hands `addPWMMapping` the string `"PMSS3,S4"`. `addPWMMapping` (`WCB_PWM.cpp:170-186`) does `String working = config.substring(3);` → `"S3,S4"`, then `int inputPort = working.substring(0, commaPos).toInt();` → `"S3".toInt()` → 0, and rejects with "Input port must be 1-5".

The function's own usage string disagrees with the help: `WCB_PWM.cpp:178` prints `"Invalid format. Use: ?PMSx,Sy,WySz..."`. So does the modern path, which synthesises the canonical string with a bare digit — WCB.ino:4540 `String pwmConfig = "PMS" + String(port) + "," + dest;`. The working legacy spelling is `?PMS3,S4`; `?PMSSx` never worked. Verified by trace against every caller of `addPWMMapping`.

**Failure scenario.** An operator restoring a board by hand reads `?MAP ?`, copies `?PMSSx,dest` from the legacy list, and sends `?PMSS3,S4`. The board answers "Input port must be 1-5" — pointing at the port number, which is correct — and the mapping is not created. Every other legacy form on that same help screen (`?SMSx`, `?SMRSx`, `?POSx`, `?PRSx`) does work, so the natural conclusion is that port 3 is unusable rather than that the documented syntax is wrong.

**Suggested fix.** Fix the help: change `WCB_Help.cpp:100` to `"  ?PMSx,dest    - PWM map (e.g. ?PMS3,S4)"`, matching `WCB_PWM.cpp:178` and the canonical string built at WCB.ino:4540. Alternatively make the dispatcher tolerant at WCB.ino:5254 by stripping an optional leading 'S' from `message.substring(3)` before re-prefixing.

### F-404 · S3 · `Code/WCB/WCB_Help.cpp:160`

**?LABEL? legacy example `?SLC1` is a silent no-op; only `?SLCS1` clears a label**

*Category:* `help-syntax-mismatch`

The `?LABEL?` topic's Legacy block is internally inconsistent — the syntax and the example disagree, and the example is the broken one (WCB_Help.cpp:160):

```cpp
        Serial.println(F("  ?SLCSx        - Clear label (e.g. ?SLC1)"));
```

The syntax `?SLCSx` is correct; the parenthesised example `?SLC1` is not. The handler strips a fixed four characters (WCB.ino:5279-5280):

```cpp
    } else if (message.startsWith("slc") || message.startsWith("SLC")) {
        clearSerialLabel(message.substring(4).toInt());
```

`message` here has the LocalFunctionIdentifier already removed (WCB.ino:4380-4381). Tracing both spellings:
- `?SLCS1` → `message = "SLCS1"`, `substring(4)` = `"1"` → `clearSerialLabel(1)` → works.
- `?SLC1` → `message = "SLC1"` (length 4), `substring(4)` = `""` → `toInt()` = 0 → `clearSerialLabel(0)`.

And `clearSerialLabel()` returns without a word for port 0 (WCB_Storage.cpp:1515-1519):

```cpp
void clearSerialLabel(int port) {
    if (port < 1 || port > 5) return;
    saveSerialLabelToPreferences(port, "");
    Serial.printf("Serial%d label cleared\n", port);
}
```

No error, no output at all — the user sees the same blank response they would see from a successful command that prints nothing.

The `WCB_Help.h` reference block propagates the broken form as the sole syntax, line 188:

```
//   ?SLCx                    Clear serial label
```

So a user reading either help source is steered to the non-working spelling. Note the sibling legacy set-label command is consistent — `?SLSx,label` is handled by `message.substring(3)` at WCB.ino:5278 and matches its help at WCB_Help.cpp:159.

**Failure scenario.** A user restoring an old config or following the legacy section types `?SLC1` to clear the S1 label. Nothing is printed and nothing changes. They retry, then run `?config`, see the label still present, and conclude label clearing is broken in this firmware — when `?SLCS1` (or the modern `?LABEL,CLEAR,S1`) would have worked. Because the command produces zero output, there is no clue that the port argument parsed as 0.

**Suggested fix.** Fix the example at WCB_Help.cpp:160 and the reference block at WCB_Help.h:188:

```cpp
Serial.println(F("  ?SLCSx        - Clear label (e.g. ?SLCS1)"));
```

Optionally make the legacy path forgiving, since `?SLC1` is the natural typo — accept both by skipping an optional `S`:

```cpp
} else if (message.startsWith("slc") || message.startsWith("SLC")) {
    String p = message.substring(3);
    if (p.length() && (p[0] == 'S' || p[0] == 's')) p = p.substring(1);
    clearSerialLabel(p.toInt());
```

Either way, `clearSerialLabel()` should print a rejection for an out-of-range port instead of returning silently (WCB_Storage.cpp:1516).

### F-405 · S3 · `Code/WCB/WCB_Help.cpp:481`

**?MP3,REMOTE and ?HCR,REMOTE are implemented but undocumented, while the identical ?DFP,REMOTE is documented**

*Category:* `help-coverage`

All three audio/vocalizer subsystems implement the same `REMOTE,W<n>` / `REMOTE,OFF` routing verb, with near-identical code and comments. Only one of the three documents it.

Implemented:
- MP3 — `WCB_MP3.cpp:209-232`, `if (aUpper.startsWith("REMOTE"))`, comment at :207-208 "route ;A to the MP3 on another board … Persisted routing (auto-learned from WDP, set by hand, or restored)."
- HCR — `WCB_HCR.cpp:540-565`, `if (aU.startsWith("REMOTE"))`, comment at :537-539 "route ;H to the HCR on another board".
- DFP — `WCB_DFP.cpp:160-183`, same shape.

Documented:
- `?DFP?` lists it (WCB_Help.cpp:557-558):
```cpp
        Serial.println(F("  REMOTE,W<n>            Route ;D to the DFPlayer on WCB<n>"));
        Serial.println(F("  REMOTE,OFF             Stop routing ;D to a remote host"));
```
- `?MP3?` Configuration Commands (WCB_Help.cpp:481-489) lists only `S<port>:<baud>:V<vol>`, `LIST`, `CLEAR`, `ONERR,<key>`, `ONERR,CLEAR`.
- `?HCR?` Configuration Commands (WCB_Help.cpp:607-620) lists only `PORT,Sx:baud`, `CLEAR`, `LIST`, `POLL,<sec>`, `STATUS`, `REFRESH`, `GET,<field>`.

This matters more than a missing convenience verb, because `REMOTE` is the manual override for a value that is otherwise only auto-learned. `routeStoredOrCap()` (WCB.ino:5827-5845) resolves `;A`/`;D`/`;H` by consulting the stored host first and only then falling back to the live WDP capability owner:

```cpp
  uint8_t target = storedHost;
  bool pinGenuinelyDead = (target != 0) && wcbPeerEverSeen(target) && !wcbPeerOnline(target);
  if (target == 0 || pinGenuinelyDead) {
    int owner = wdpCapOwner(capBit);
```

So a stale `mp3Config.remoteWCB` / `hcrConfig.remoteWCB` pins routing at a board that may no longer host the device, and `REMOTE,OFF` is the documented-for-DFP, undocumented-for-MP3/HCR way to clear it. The `?MP3?` and `?HCR?` pages give the user no way to inspect or reset that pin.

**Failure scenario.** A user moves their MP3 Trigger from WCB3 to WCB5. WCB2 learned `mp3Config.remoteWCB = 3` from WDP earlier and persisted it. WCB3 is still powered and online, so `pinGenuinelyDead` is false at WCB.ino:5834 and the WDP failover never engages — `;A,PLAY,5` keeps unicasting to WCB3, which no longer has the device. The fix is `?MP3,REMOTE,OFF` (or `?MP3,REMOTE,W5`), but `?MP3?` never mentions REMOTE exists; only `?DFP?` does, for a different device.

**Suggested fix.** Add the two lines to each topic, mirroring the DFP wording. In `?MP3?` after WCB_Help.cpp:485:

```cpp
Serial.println(F("  REMOTE,W<n>            Route ;A to the MP3 Trigger on WCB<n>"));
Serial.println(F("  REMOTE,OFF             Stop routing ;A to a remote host"));
```

and in `?HCR?` after WCB_Help.cpp:613, the same with `;H` / HCR. Add a Notes line to both stating that the remote host is normally auto-learned from WDP and that `REMOTE,OFF` clears a stale pin — that is the part a user cannot deduce.

### F-406 · S3 · `Code/WCB/WCB_RemoteTerm.cpp:71`

**WCBSerial::_bufChar increments _lineLen non-atomically from three concurrent tasks, allowing a one-byte write past _lineBuf**

*Category:* `concurrency`

`WCB_RemoteTerm.h:106-107` redefines `Serial` to the single global `WCBDebugSerial`, so *every* `Serial.print/printf/write` in the whole sketch funnels through `WCBSerial::write` (`WCB_RemoteTerm.cpp:49-61`), which calls `_bufChar` for each byte whenever an RTERM session is active:

```
WCB_RemoteTerm.cpp:68   void WCBSerial::_bufChar(uint8_t c) {
WCB_RemoteTerm.cpp:69     if (c == '\r') return;
WCB_RemoteTerm.cpp:70
WCB_RemoteTerm.cpp:71     _lineBuf[_lineLen++] = (char)c;
WCB_RemoteTerm.cpp:72
WCB_RemoteTerm.cpp:73     if (c == '\n' || _lineLen >= LINEBUF_SIZE) {
WCB_RemoteTerm.cpp:74       _flushLine();
WCB_RemoteTerm.cpp:75     }
WCB_RemoteTerm.cpp:76   }
```

The buffer is `char _lineBuf[LINEBUF_SIZE]` with `LINEBUF_SIZE = RTERM_TEXT_SIZE = 160` (`WCB_RemoteTerm.h:52`, `:86-88`), immediately followed by `size_t _lineLen`. There is no mutex, critical section or atomic anywhere in this class.

Single-threaded the arithmetic is sound: the post-increment writes at the old index, so the highest index written is 159 and the `>= 160` test then flushes and resets. But three contexts call it concurrently — loopTask on core 1, `serialCommandTask` on core 1 (`WCB.ino:7439`), and the WiFi task via the many `Serial.printf` calls inside `espNowReceiveCallback` (e.g. `WCB.ino:3745`, `:3757`, `:4016`, `:4054`). The read-modify-write of `_lineLen` on line 71 is not atomic, and a preemption between the store and the bounds test on line 73 lets a second writer index at 160.

That the class knows about cross-task hazards makes the omission visible: the relay side deliberately queues instead of printing, with the reason spelled out at `WCB_RemoteTerm.cpp:140-144`, and `rtermRelayHandlePacket` uses allocation-free `strncmp` at `:150`. Only the transmit side is unguarded.

**Failure scenario.** An RTERM session is active on a target board (`?RTERM,START,<relay>`) — the debugging scenario in which Serial output is heaviest. loop() is mid-way through a long `Serial.printf` (a `?STATS` dump or a config listing) with `_lineLen` at 159; it executes `_lineBuf[159] = c; _lineLen = 160;` and is preempted before evaluating the `if` on line 73. An ESP-NOW frame arrives and `espNowReceiveCallback` prints a debug line on the WiFi task; that writer executes `_lineBuf[160] = c`, one byte past the 160-byte array, clobbering the low byte of the adjacent `_lineLen` member. `_lineLen` then holds an arbitrary value, and the subsequent `_lineBuf[_lineLen++]` writes at that offset — an unbounded out-of-bounds write into whatever follows the WCBSerial object.

**Suggested fix.** Guard `_bufChar`/`_flushLine` with a `portMUX_TYPE` critical section, following the pattern already used for the ETM ACK ring at WCB.ino:1114 and 1119-1129. Also reorder the bounds check ahead of the store (`if (_lineLen >= LINEBUF_SIZE) _flushLine(); _lineBuf[_lineLen++] = c; if (c == '\n') _flushLine();`) so the write index can never reach the array bound even if the lock is later removed.

### F-407 · S3 · `Code/WCB/WCB_Storage.cpp:642`

**Stored-sequence keys have no reserved-name guard — ?SEQ,SAVE,key_list,<value> overwrites the sequence index and orphans every stored sequence**

*Category:* `logic`

`saveStoredCommandsToPreferences()` writes the user-supplied key directly into the `stored_cmds` namespace (WCB_Storage.cpp:642-643) with no check against the two internal keys that live in that same namespace: `key_list` (the sequence index, written at :658 and read at :645, :716, :763, :800, :878, and from WCB.ino:2225/:2897) and `seq_mig_done` (the migration flag, :877/:926).

Because both are 8 and 12 characters, they are valid NVS keys and a user-supplied key of the same name collides exactly. `?SEQ,SAVE,key_list,;S1,hello` executes `putString("key_list", ";S1,hello")` at :643, then reads `existingKeys` back at :645 — now `";S1,hello"` — finds no `key_list,` substring in the duplicate check at :649-654, and appends, leaving the index as `";S1,hellokey_list,"`.

Every other stored sequence's value is still in NVS but is now unreachable: `listStoredCommands()` (:712-743), `buildSequenceNamesString()` (:797-827), `sequenceInventoryHash()` (:759-796) and the backup emitter (WCB.ino:2896-2913) all walk `key_list` and nothing else. There is no rebuild-from-NVS path — the commented-out `loadStoredCommandsFromPreferences()` at :543-553 was the only one and it is dead — and NVS iteration is never used anywhere in the firmware, so the orphaned values cannot be recovered without reflashing.

The same shape applies to `seq_mig_done`: writing a string over that Bool key removes the migration guard, which interacts with the resurrection bug reported separately.

**Failure scenario.** A user (or an imported config chain, or a `?SEQ,SAVE` relayed from NaviCore) names a sequence `key_list`. The board prints "Stored: Key='key_list'". `?SEQ,LIST` immediately afterwards shows one bogus entry and none of the real sequences; `;C<anything>` prints "No command stored under key"; `?CONFIG` and the config backup emit nothing for sequences, so a Wizard verify-pull concludes the board has none and a subsequent full push cannot restore them either. All the values are still in flash and permanently orphaned.

**Suggested fix.** Reject the reserved names alongside the length check at WCB_Storage.cpp:637: `if (key == "key_list" || key == "seq_mig_done") { Serial.printf("'%s' is a reserved internal key.\n", key.c_str()); return; }`. Apply the same guard in `eraseStoredCommandByName()` (:667) so `?SEQ,CLEAR,key_list` cannot delete the index either. Longer term, prefix every user sequence key on disk (e.g. `s_<name>`, which also gives the length check a clean 13-char user budget) so the user keyspace and the internal keyspace cannot overlap by construction.

### F-408 · S3 · `Code/WCB/WCB_WDP.cpp:195`

**WDP PWMTARGET advert is capped at 10 (board,port) pairs while a board can configure 25 — truncated receivers have their auto-configured PWM output torn down by the self-heal**

*Category:* `board-count-cap`

`wdpLocalPwmTargets()` packs this board's remote PWM outputs as `[targetWCB][port]` pairs, hard-capped at `WDP_MAX_PWMTARGET`:

```c
193: static int wdpLocalPwmTargets(uint8_t *rec) {
194:   int n = 0;
195:   for (int i = 0; i < MAX_PWM_MAPPINGS && n < WDP_MAX_PWMTARGET; i++) {
196:     if (!pwmMappings[i].active) continue;
197:     for (int j = 0; j < pwmMappings[i].outputCount && n < WDP_MAX_PWMTARGET; j++) {
```

`WDP_MAX_PWMTARGET` is 10 (`Code/WCB/WCB_WDP.h:37`). The configurable domain is larger: `addPWMMapping` allows one active mapping per input port (`Code/WCB/WCB_PWM.cpp:194-204`) with up to 5 outputs each (`:228` `while (startIdx < working.length() && mapping.outputCount < 5)`), i.e. up to 5 x 5 = **25** distinct remote (board,port) targets, and each output accepts any board 1..MAX_WCB_COUNT (`:252`). Targets past index 10 are dropped from the advert with no log and no counter.

The truncation is not merely a missed auto-config. The receive side treats the advert as authoritative and self-heals against it:

```c
761:    reconcileWdpAutoPWMOutputs((uint8_t)senderWCB, nb.pwmSelfPorts, nb.pwmSelfCount);
```

```c
832:    for (int i = 0; i < pwmOutputCount; ) {
833:        if (pwmOutputAutoSrc[i] == srcWCB) {
834:            int p = pwmOutputPorts[i];
835:            bool wanted = false;
836:            for (int k = 0; k < wantCount; k++) if (wantPorts[k] == p) { wanted = true; break; }
837:            if (!wanted) {
...
840:                removePWMOutputPort(p);
```

`removePWMOutputPort` also re-enables broadcast on the freed port (`Code/WCB/WCB_PWM.cpp:862-867`), so the pin goes back to spraying serial text. The comment block at `:823-830` enumerates one known limitation of this reconcile (two sources on one port) but not truncation — the reconcile has no way to tell "the source dropped this mapping" from "the source's advert could not carry it."

Contrast the payload-budget handling for the same TLV, which *was* thought through: `wdpBuildPayload` deliberately emits PWMTARGET before the cosmetic port labels (`Code/WCB/WCB_WDP.cpp:272-281`) so the functional TLV wins the 200-byte budget. The 10-entry cap on the list itself is the gap.

**Failure scenario.** A body WCB fans RC channels out to servo boards: `?MAP,PWM,S2,W3S1,W3S2,W4S1,W4S2,W5S1` and `?MAP,PWM,S3,W6S1,W6S2,W7S1,W7S2,W8S1` — 10 targets, all advertised, all ten receiver ports auto-configured and tagged `pwmOutputAutoSrc = 1`. The user later clears the S2 mapping (`?MAP,PWM,CLEAR,S2`, freeing slot 0) and adds `?MAP,PWM,S1,W9S1,W9S2,W10S1` — which lands in the now-free slot 0 and is therefore iterated FIRST. The advert's ten slots now hold W9/W10 plus the first seven of the old set; W8S1 (and two others) fall past index 10. On WCB8's next received advert from WCB1, `reconcileWdpAutoPWMOutputs(1, ...)` finds S1 tagged to source 1 but absent from `wantPorts`, prints "[WDP] WCB1 no longer drives our S1 - clearing auto-configured PWM output", removes the port, and re-enables broadcast on it. WCB1 keeps streaming `;P` frames to a port that is no longer a PWM output while broadcast serial text is now written onto the same servo signal line. Neither board reports a truncation.

**Suggested fix.** Either raise `WDP_MAX_PWMTARGET` to cover the real domain (`MAX_PWM_MAPPINGS * 5` = 25, costing 50 bytes of the 200-byte payload — still ahead of the port labels in the budget order), or make truncation visible and safe: have `wdpLocalPwmTargets` return a `truncated` flag, log it once, and carry a WDP advert flag (e.g. a bit in `WDP_TLV_FLAGS`) meaning "this PWMTARGET list is partial" so `reconcileWdpAutoPWMOutputs` skips the tear-down branch for that source. The list-fits case keeps the self-heal; the partial case degrades to add-only, which is the pre-self-heal behaviour.

### F-409 · S3 · `Code/WCB/WCB_WDP.cpp:557`

**TLV order is load-bearing on a wire format shared with WCB_Client, but is unstated in the spec and only implicitly tested**

*Category:* `correctness`

The decoder is last-writer-wins across TLVs that touch the same fields. `Code/WCB/WCB_WDP.cpp:557-570`:

```
case WDP_TLV_MAESTRO: {
  int L = len > WDP_MAX_MAESTRO ? WDP_MAX_MAESTRO : len;
  memcpy(nb.maestroIds, val, L); nb.maestroCount = (uint8_t)L;
  for (int i = 0; i < L; i++) nb.maestroBaudCode[i] = 0xFF;  // unknown unless MAESTRO_CFG overrides
  break;
}
case WDP_TLV_MAESTRO_CFG: {   // [id][baudCode] pairs — supersedes the id-only TLV above
```

There is no ordering guard: whichever TLV appears *later* in the payload wins. If a sender emits 0x0E before 0x06, the legacy TLV resets every `maestroBaudCode[i]` to 0xFF. The consumer then discards all of them — WCB_WDP.cpp:708-709: `uint32_t baud = wdpCodeToBaud(nb.maestroBaudCode[i]); if (baud == 0) continue;` (`wdpCodeToBaud(0xFF)` returns 0 via the `c < WDP_BAUD_N` test at :138). Remote-Maestro auto-config for that host silently stops, with no log line.

Both shipping encoders happen to agree — WCB_WDP.cpp:257 emits MAESTRO then :260 emits MAESTRO_CFG; WCB_Client.cpp:1935 then :1941 — so nothing is broken today. But the constraint is written nowhere a third implementer would find it: the TLV table in `docs/WDP_DESIGN.md:78-97` lists 0x06 and 0x0E as independent rows with no ordering note, and §3 states no general "later TLVs supersede earlier" rule. The only thing pinning it is `test_maestro_cfg_supersedes` (tests/wdp_wire_test.cpp:242-252), which encodes MAESTRO at :246 and MAESTRO_CFG at :247 — i.e. it asserts the shipping order produces the right answer, never that the reverse order is invalid or handled. This is exactly the class of change CLAUDE.md rule 10 warns about ("New TLVs need both sides, and must not break an older peer's parse").

**Failure scenario.** A new WCB_Client release, or NaviCore, reorders its advert builder — a natural thing to do when adding a TLV, since 0x0E sorts after 0x06 numerically and someone tidies the emit block into type order. Every WCB that hears it decodes MAESTRO_CFG first, then MAESTRO overwrites all nine baud codes with 0xFF. `maestroAutoAddRemote` is never called (WCB_WDP.cpp:708-710), so `;M<id>` stops routing to that host across the whole mesh. Nothing logs, the neighbor still appears healthy in `?WDP,LIST`, and the WDP test suite passes because it only ever encodes the good order.

**Suggested fix.** Make the decoder order-independent: track a `bool sawMaestroCfg` in the decode loop and have `case WDP_TLV_MAESTRO` skip the `maestroBaudCode[i] = 0xFF` reset (or skip entirely) once the rich TLV has been seen. Then add the negative test — encode 0x0E before 0x06 and assert the bauds survive. Whichever way it is resolved, state the rule explicitly in the docs/WDP_DESIGN.md §3 TLV table, since WCB_Client is a separate repo implementing the same format.

### F-410 · S3 · `Code/WCB/WCB.ino:267`

**etmMissedHeartbeats: the source comment documents a deliberate widening to 5 that the boot-time NVS default silently reverts to 3**

*Category:* `stale-comment`

WCB.ino:267 sets the compile-time default and records the reasoning:

```cpp
int etmMissedHeartbeats = 5;   // (etmHeartbeatSec+1)*5 = 55s offline window — widened from 3/33s so a few lost broadcast heartbeats on a busy mesh don't flap a live board (mirrors WCBClient _missedBeforeOffline)
```

That value never survives boot on a board that has not explicitly run `?ETM,MISS`. `loadETMSettings()` (WCB_Storage.cpp:2224-2233) reads the key with a hard-coded fallback of 3:

```cpp
void loadETMSettings() {
    preferences.begin("etm_config", true);
    etmEnabled          = preferences.getBool("etmEnabled", true);
    etmBootHeartbeatSec = preferences.getInt("etmBoot", 2);
    etmHeartbeatSec     = preferences.getInt("etmHB", 10);
    etmMissedHeartbeats = preferences.getInt("etmMiss", 3);
```

and it is called unconditionally during startup at WCB.ino:7416. Since `?ETM,MISS` is the only writer of the `etmMiss` key (WCB.ino:4688 and legacy :5325, both followed by `saveETMSettings()`), any board that has never had that command run comes up with 3, not 5.

The offline window that results is computed at WCB.ino:1040:

```cpp
  unsigned long offlineThresholdMs = (unsigned long)(etmHeartbeatSec + 1) * etmMissedHeartbeats * 1000UL;
```

(10+1) x 3 = 33 s — precisely the value the comment says was rejected — instead of the intended 55 s.

The comment is factually wrong about what the code does, which is the finding. Note the neighbouring declaration got this right: WCB.ino:264 says `bool etmEnabled = true;   // NVS default is also true — overwritten at boot by loadETMSettings()`, i.e. the author was aware the initialiser is overwritten and kept the two defaults in sync there. Only `etmMiss` diverged.

Secondary consequence: `?ETM?` help states `Default: 3` (WCB_Help.cpp:265). That currently matches observed behaviour but contradicts the stated design intent, so whichever value is chosen, help and code must move together. The Wizard/backup path is unaffected — `collectConfigCommands()` emits the live value (WCB.ino:2869, `emit("ETM,MISS," + String(etmMissedHeartbeats), true)`), so a restored board keeps whatever it had.

**Failure scenario.** A fresh board, or any board restored via ?ERASE,NVS, boots with a 33 s offline window rather than the 55 s the comment says was chosen after boards were seen flapping on a busy mesh. Under heavy PWM/raw traffic a peer that misses three consecutive broadcast heartbeats is marked offline at WCB.ino:1040, its pending ETM entries are cleared (etmClearPeerFromPending), and it drops out of routing until the next heartbeat — the exact flap the widening was meant to prevent. Nobody investigating will suspect the default, because the source plainly says 5.

**Suggested fix.** Make the two defaults agree. Since the comment states the design intent, change the NVS fallback at WCB_Storage.cpp:2229 to match:

```cpp
    etmMissedHeartbeats = preferences.getInt("etmMiss", 5);
```

and update `?ETM?` help (WCB_Help.cpp:265) from `Default: 3` to `Default: 5` in the same commit. Better still, hoist the defaults into named constants shared by WCB.ino:267 and loadETMSettings so the pair cannot drift again — the same latent split exists for etmBoot/etmHB/etmTimeout, which happen to agree today by coincidence.

### F-411 · S3 · `Code/WCB/WCB.ino:2013`

**parseCommandsAndEnqueue copies the whole remaining chain three times per token, and runs that O(n²) churn inside the ESP-NOW receive callback**

*Category:* `efficiency`

Every iteration of the chain-splitting loop in `parseCommandsAndEnqueue` makes three full copies of the unparsed remainder:

```
WCB.ino:2012   while (startIdx < data.length()) {
WCB.ino:2013     String restOfString = data.substring(startIdx);
...
WCB.ino:2049       String restUpper = restOfString;
WCB.ino:2050       restUpper.toUpperCase();
...
WCB.ino:2082       String restUpper = restOfString;
WCB.ino:2083       restUpper.toUpperCase();
```

`restOfString` at 2013 is unconditional; the two `restUpper` copies at 2049 and 2082 are in separate scopes and both execute for any token that is not a `?CS` command. For a chain of length L split into N tokens that is ~3N heap allocations and ~1.5·L·N bytes copied — quadratic in the chain length. All three are used only for prefix tests (`startsWith`) that need no copy at all.

The cost matters because of where this runs. It is called directly from `espNowReceiveCallback` (WiFi task) on both receive paths — `WCB.ino:3997` (ETM) and `WCB.ino:4227` (normal) — and, worse, from `handleMgmtPacket` at `WCB.ino:2731` on a reassembled payload that can be `MGMT_MAX_CHUNKS × MGMT_PAYLOAD_SIZE` = 16 × 180 = 2880 bytes (`WCB.ino:854-855`, `:2707`).

Everywhere else the firmware goes to real trouble to keep the WiFi callback short — the OTA queue (`WCB_OTA.cpp:532-537`), the WDP queue (`WCB.ino:964`), the mgmt-request queue (`WCB.ino:3244-3250`), the timer-chain queue (`WCB.ino:389-393`) — each with a comment explaining that hundreds of milliseconds in the callback drops inbound frames. This path is the outlier.

**Failure scenario.** The Wizard pushes a full configuration to a board via `?MGMT,FRAG,...`. The final fragment arrives in `espNowReceiveCallback`, `handleMgmtPacket` reassembles a ~2880-char chain (WCB.ino:2707) and calls `parseCommandsAndEnqueue(fullCmd, 0)` at WCB.ino:2731. A restore chain of that size holds roughly 40 `^`-separated tokens, so the splitter performs ~120 heap allocations averaging ~1.4 KB each and copies on the order of 170 KB — all on the WiFi task inside the receive callback. ESP-NOW frames arriving during that window are dropped, and the transient allocation storm fragments the heap on a board that has just been asked to write its entire configuration to NVS.

**Suggested fix.** Delete the copies. Replace `restOfString` with index-based comparison against `data` — `data.startsWith(prefix, startIdx)` exists on Arduino String and takes an offset — and replace the two `restUpper` blocks with a case-insensitive length-limited compare (`strncasecmp(data.c_str() + startIdx, "?SEQ,SAVE,", 10)`) rather than upper-casing a multi-kilobyte copy twice per token. Separately, consider routing the `handleMgmtPacket` completion at WCB.ino:2731 through the loop-drained path the ETM timer chains already use, so no multi-kilobyte parse ever runs in the callback.

### F-412 · S3 · `Code/WCB/WCB.ino:2020`

**?CS and ?SEQ,SAVE are documented as the same command but the chain splitter gives their values opposite boundaries**

*Category:* `command-dispatch`

`WCB_Help.cpp:788` lists `?CSkey,value` as the legacy spelling of `?SEQ,SAVE,key,value` (`"  ?CSkey,value   - Save sequence  (e.g. ?CSwave,CMD1^CMD2)"`). `parseCommandsAndEnqueue` gives the two forms different value boundaries:

`?CS` — runs to the next `<delim><funcChar>CS`, or to the END OF THE CHAIN if there is no second `?CS`:
```
2020:      int nextCSPos = data.indexOf(String(commandDelimiter) + String(LocalFunctionIdentifier) + "CS", startIdx + 1);
2026:      int endPos = (nextCSPos == -1) ? data.length() : nextCSPos;
2027:      String csCommand = data.substring(startIdx, endPos);
```
`?SEQ,SAVE` — stops at the next `<delim><funcChar>` of any kind:
```
2057:        String nextFuncBoundary = String(commandDelimiter) + String(LocalFunctionIdentifier);
2059:        int nextFuncPos = data.indexOf(nextFuncBoundary, startIdx + 1);
2062:        if (nextFuncPos != -1 && nextFuncPos < endPos) endPos = nextFuncPos;
```
Consequences run in opposite directions. A `?CS` in a chain absorbs every following `?`-command into the stored sequence value (nothing after it is ever executed). A `?SEQ,SAVE` whose sequence body legitimately contains a `?`-command is truncated at that point and the remainder of the body is executed live instead of stored — `?`-prefixed steps are valid inside a body, since `handleSingleCommand:4380` dispatches them on recall via `recallCommandSlot` → `parseCommandsAndEnqueue` (`WCB_Storage.cpp:615-618`).

The comment at :2047-2048 asserts *"The only real config-chain boundary after a ?SEQ,SAVE command is delimiter+funcChar (e.g. \"^?\"), which begins the NEXT config-level command"* — that is the part which is factually wrong about the code it guards, because a stored body may itself contain `^?`.

**Failure scenario.** (a) User saves a sequence that sets a variable as one of its steps: `?SEQ,SAVE,arm,;V,armed,1^?VAR,SET,mode,2^;M11`. The stored value is only `;V,armed,1`; `?VAR,SET,mode,2` and `;M11` execute immediately on the console instead. `?SEQ,LIST` shows the truncated body and the user cannot tell why. (b) User pastes the help's legacy form into a longer restore line: `?CSwave,CMD1^CMD2^?BAUD,S1,57600^?WCB,3`. The whole tail is stored as the value of `wave`; the baud and board-number commands are never executed, and re-running the sequence later fires them.

**Suggested fix.** Make the two forms agree on `"^" + funcChar` as the boundary and document the restriction. Change the `?CS` end at WCB.ino:2020-2026 to search for `String(commandDelimiter) + String(LocalFunctionIdentifier)` (same as :2057) so a `?CS` in a config chain stops at the next config command. Then fix the shared limitation both would still have: reject a `?SEQ,SAVE`/`?CS` value containing `<delim><funcChar>` with an explicit message ("a stored sequence cannot contain a ^? step — use ;V / IF instead"), and correct the comment at :2047-2048 to state that this is a grammar restriction, not a property of sequence values.

### F-413 · S3 · `Code/WCB/WCB.ino:2779`

**?LED,PIN is settable and persisted on every hardware revision but is emitted in the config chain only for HW 3.1/3.2, so a custom pin is unrepresentable in a backup and a stale override survives an HW change**

*Category:* `config-roundtrip`

Set side — no HW restriction: `?LED,PIN,<n>` accepts any GPIO 0-48 and persists it for any hardware revision (WCB.ino:5128-5140 → `saveStatusLEDPin(pin)`, WCB_Storage.cpp:108-113, key `led_pin` in namespace `led_config`).

Restore side — no HW restriction either: `loadStatusLEDPin()` (WCB_Storage.cpp:115-123) applies the NVS value for ANY hw version, and setup() calls it at WCB.ino:7140, i.e. AFTER `loadHWversion()` at :7139 (which calls `updatePinMap()` and sets `STATUS_LED_PIN` from the board revision — wcb_pin_map.cpp:57, :74, :91, :110 give 19/19/19/38 for HW 2.1/2.3/2.4 and 3.x). So the NVS override always wins over the pin map.

Emit side — restricted: 
```
WCB.ino:2778-2780
  // LED pin (HW 3.1/3.2 only — always include so config pull captures current value)
  if (wcb_hw_version == 31 || wcb_hw_version == 32)
    emit("LED,PIN," + String(STATUS_LED_PIN), true);
```
The Wizard applies the same restriction (`if (hwValid && (config.hwVersion === 31 || config.hwVersion === 32))`, Wizard/parser.js:1243-1250).

Two consequences. (a) On HW 1.0/2.1/2.3/2.4 a deliberately re-routed status-LED pin is saved and honoured at boot but is absent from ?backup and from every Wizard push — it cannot be restored onto a replacement board. (b) Because no `LED,PIN` token is emitted for those revisions, there is also no config-chain command that can RESET a stale override; a board previously configured as 3.2 (led_pin = 38 in NVS) that is re-flashed and restored as HW 2.4 comes up with STATUS_LED_PIN = 38 instead of the pin map's 19, and the full restore chain contains nothing to correct it. Only `?ERASE,NVS` (which clears `led_config` at WCB_Storage.cpp:1010) or a manual `?LED,PIN,19` recovers it.

The emit-side comment ("HW 3.1/3.2 only") is factually wrong about the code it guards: the setting is neither set-restricted nor load-restricted to those revisions.

**Failure scenario.** A HW 3.2 board is repurposed and reconfigured as HW 2.4 (or a 2.4 board inherits a 3.x NVS image). Operator pastes the 2.4 board's full backup chain. `?HW,24` is applied; nothing in the chain mentions the LED pin. After reboot `loadHWversion()`→`updatePinMap()` sets STATUS_LED_PIN = 19, then `loadStatusLEDPin()` overrides it back to 38 from the leftover led_config entry. GPIO38 is not the 2.4 board's NeoPixel line, so the status LED is dead (and GPIO38 is driven) with no diagnostic beyond the boot line "LED pin override from NVS: GPIO38".

**Suggested fix.** Either emit the pin unconditionally when it differs from the pin-map default for the current revision (`if (STATUS_LED_PIN != pinMapDefaultForHw(wcb_hw_version)) emit("LED,PIN," + String(STATUS_LED_PIN), true);`) so a backup can both carry and reset it, or restrict the setter and `loadStatusLEDPin()` to HW 3.1/3.2 so the code matches the comment. Mirror whichever choice is made in Wizard/parser.js:1243-1250.

### F-414 · S3 · `Code/WCB/WCB.ino:3720`

**Every received ESP-NOW packet builds two heap Strings just to compare the 40-byte password field**

*Category:* `efficiency`

Both receive paths in `espNowReceiveCallback` authenticate by constructing two temporary Arduino Strings:

```
WCB.ino:3720     if (String(etmReceived.structPassword) != String(espnowPassword)) return;   // ETM path
WCB.ino:4011   if (String(received.structPassword) != String(espnowPassword)) return;       // normal path
```

`espnowPassword` is `char[40]` (`WCB.ino:148`) and its shipped default is `"change_me_or_risk_takeover"` — 26 characters. The ESP32 Arduino String has SSO but only up to 14 characters (`WString.h:315-323` `SSOSIZE = sizeof(struct _ptr) + 4 - 1` = 15; `WString.h:349` `capacity() = SSOSIZE - 1`), so any password longer than 14 chars puts both temporaries on the heap. That is two `malloc` + two `free` per received ESP-NOW frame, executed on the WiFi task inside the receive callback, purely to compare two NUL-terminated char buffers.

The same pattern repeats on every other packet handler reached from the callback: `WCB.ino:2643`, `:2973`, `:3046`, `:3130`, `:3144`, `:3183`, `:3238`, `:3305`, `:3340`, `:3375`, `:3410`, and `WCB_OTA.cpp:377`, `:446`.

The allocation-free form is already present in the same codebase, in a function reached from the same callback — `WCB_RemoteTerm.cpp:150`:

```
if (strncmp(pkt.structPassword, espnowPassword, sizeof(pkt.structPassword) - 1) != 0) return;
```

Beyond the wasted cycles and heap-lock contention with the WiFi driver, the failure mode is unpleasant: if the heap is exhausted or badly fragmented, `String::reserve` fails and `invalidate()` leaves the object empty, so the comparison fails and the board silently drops every ESP-NOW packet, including the ones that would let an operator diagnose it.

**Failure scenario.** A board sits on a busy mesh — ETM heartbeats from 20 peers plus NaviCore's rc_ch channel stream, which the comment at WCB.ino:3921-3924 confirms is a high-rate broadcast whenever a config tool is subscribed. At a few hundred frames per second the callback performs roughly a thousand malloc/free pairs per second on the WiFi task, each contending for the heap lock with the WiFi driver's own buffer allocations. Under concurrent heap pressure — an OTA transfer, or the ~2.9 KB MGMT reassembly burst at WCB.ino:3083 — a String allocation fails, `String(received.structPassword)` becomes empty, the comparison at WCB.ino:4011 fails, and the packet is dropped with no log at all. The board appears to go deaf on the mesh while still transmitting normally.

**Suggested fix.** Replace the String comparisons with the `strncmp` form already used at WCB_RemoteTerm.cpp:150. A single shared helper — `static inline bool espnowAuth(const char *pw) { return strncmp(pw, espnowPassword, sizeof(espnowPassword) - 1) == 0; }` — applied at WCB.ino:3720 and :4011 first (the two per-packet sites), then across the handler sites listed above and WCB_OTA.cpp:377/:446, removes all per-packet heap traffic from the receive path.

### F-415 · S3 · `Code/WCB/WCB.ino:6417`

**processIncomingSerial grows a static String one byte at a time with no length cap**

*Category:* `resource-exhaustion`

`processIncomingSerial` accumulates each port's line into a function-static `String` and only ever resets it on `\r`/`\n`:

```
WCB.ino:6380   static String serialBuffers[6];  // one for each serial port (0 = Serial, 1–5 = Serial1-5)
WCB.ino:6381   String &serialBuffer = serialBuffers[sourceID];
WCB.ino:6382   while (serial.available()) {
...
WCB.ino:6416     } else {
WCB.ino:6417       serialBuffer += c;
WCB.ino:6418     }
```

There is no upper bound on `serialBuffer.length()`. Any byte stream on a port that `processIncomingSerial` owns and that never contains 0x0A or 0x0D grows this String without limit. Past the 14-char SSO threshold (`WString.h:322` `SSOSIZE = sizeof(struct _ptr) + 4 - 1`, `WString.h:349` `capacity() = SSOSIZE - 1`) every growth step is a heap realloc, so the buffer walks up through the heap re-allocating as it goes, and — because `serialBuffer = ""` (`WCB.ino:6414`) does not shrink capacity — each port keeps its high-water allocation for the life of the boot.

The ports that reach this code are exactly the ones with no other owner: `WCB.ino:6363-6379` skips MP3, DFPlayer, HCR and mid-query Maestro ports, and `WCB.ino:6603-6605`/`:6612-6621` skip raw-mapped ports. Everything else — including a port whose raw mapping was cleared, or one wired to a binary device that was never mapped — lands here.

Contrast the sibling paths, which are all bounded: `RawSerialForwardingTask` uses `static uint8_t buffer[64]` with an explicit `len < sizeof(buffer)` guard (`WCB.ino:6637`, `:6668`), and the Kyber forwarders use `static uint8_t remoteBuf[64]` with flush-on-full (`WCB.ino:4252`, `:4275`, `:4311`).

**Failure scenario.** A device that speaks a binary or non-line-terminated protocol is wired to Serial2 and the port is not raw-mapped — e.g. a Maestro configured on that port is cleared, or a user attaches a device before creating its mapping, or a port is left at the wrong baud so incoming framing produces bytes but never 0x0A/0x0D. Every call to `processIncomingSerial` appends more bytes and never flushes. Over minutes the String climbs into the tens of kilobytes, consuming the ~241 KB of free heap the build reports and repeatedly reallocating a large block, which fragments what remains. ESP-NOW receive-path allocations (WCB.ino:3720, 4011), the command-queue mallocs (WCB.ino:1927) and the MGMT reassembly Strings (WCB.ino:3083) start failing; commands are dropped with 'Out of memory while enqueuing command!' and packets are silently discarded, with no indication that a serial port is the cause.

**Suggested fix.** Cap the accumulator. Add a guard alongside the append at WCB.ino:6417 — e.g. `if (serialBuffer.length() >= MAX_SERIAL_LINE) { Serial.printf("[SERIAL] S%d: line exceeded %d bytes — discarding\n", sourceID, MAX_SERIAL_LINE); serialBuffer = ""; continue; } serialBuffer += c;` — with `MAX_SERIAL_LINE` set above the longest real line (the `?OTALOCAL,DATA,<base64>` line is the largest at roughly 1400 chars for a 1024-byte chunk, per WCB_OTA.cpp:40 `OTA_LOCAL_MAX_CHUNK`). Logging the discard turns a silent heap leak into a diagnosable event.

### F-416 · S3 · `Code/WCB/WCB.ino:6465`

**The ?C-prefixed checksum branch is a stale duplicate of the main verifier: first-occurrence indexOf, unbounded checksum tail, and no chain splitting**

*Category:* `command-dispatch`

`processSerialCommandHelper` special-cases any line whose first two characters are `<funcChar>C` (:6436-6437) and runs its own checksum verifier. It diverges from the general verifier in `parseCommandsAndEnqueue` on three points, each of which is a bug the general path already fixed and documented:

1. **First vs last occurrence.** :6441 `int chkPos = data.indexOf(chkPattern);` — while :1949-1953 uses `lastIndexOf` with the comment *"Use lastIndexOf: the real checksum is the FINAL token, and a stored sequence value can legitimately contain an earlier '^?CHK' — matching the first occurrence truncated the chain and aborted the whole restore."*
2. **Unbounded tail.** :6451 `String providedChecksum = data.substring(chkPos + chkPattern.length());` takes everything to end of line, where :1962-1966 bounds it at the next `commandDelimiter`.
3. **No chain splitting.** On success :6465 calls `enqueueCommand(cmdWithoutChecksum, sourceID)` — one queue item for the whole chain. The sibling else-branch six lines later calls `parseCommandsAndEnqueue` and its comment (:6473-6475) states exactly why: *"so that chained strings like '?CMDCHAR,;^?SEQ,SAVE,key,val^…' split into separate commands instead of being swallowed whole by processLocalCommand."* The checksummed branch does the thing that comment warns against.

Because `data.startsWith(funcChar + "C")` matches `?CONFIG`, `?CONTROLLER`, `?CMDCHAR`, `?CS`, `?CE`, `?CCLEAR` and `?CHK` alike, any of those can lead such a chain. A full `?backup` chain begins with `?HW,…` (`collectConfigCommands`, WCB.ino:2777), so the standard restore does not hit this branch — the defect is latent, reachable via a hand-assembled or partial chain.

**Failure scenario.** An operator assembles a partial restore line by hand, leading with the command-character setting exactly as the code's own comment illustrates: `?CMDCHAR,;^?SEQ,SAVE,wave,;M11^;t500^;M12^?BAUD,S1,57600^?CHK<crc>`. The `?C` branch matches, the CRC verifies, and :6465 enqueues the entire remainder as ONE command. `processLocalCommand` splits on the first comma, gets `rootUpper == "CMDCHAR"` and `args = ";^?SEQ,SAVE,wave,…"`, fails the `args.length() == 1` test at :5058, prints "Invalid format. Use: ?CMDCHAR,x where x is one character" and returns. The console shows "✓ Command checksum VERIFIED" immediately above it, so the restore reads as successful while the sequence and the baud change were never applied.

**Suggested fix.** Delete the bespoke verifier at WCB.ino:6435-6479 and let every line fall through to `parseCommandsAndEnqueue`, which already handles the `?CS` boundary (:2015-2042), the `?SEQ,SAVE` boundary (:2053) and the checksum (:1947-2007) correctly. If the branch must stay, at minimum change :6441/:6444 to `lastIndexOf`, bound the checksum at the next delimiter as :1963-1966 does, and replace the `enqueueCommand` at :6465 with `parseCommandsAndEnqueue(cmdWithoutChecksum, sourceID)`.

### F-417 · S3 · `docs/WCB_KYBER_PASSTHROUGH_PARAMS.md:25`

**Kyber pass-through doc still says >64 B/poll is "silently discarded" — the primary path now flushes instead**

*Category:* `docs-drift`

The doc's central design constraint is wrong for the path it describes.

docs/WCB_KYBER_PASSTHROUGH_PARAMS.md:24-27: "**Design to a 64-byte effective MTU.** If more than 64 bytes arrive at the source UART between two 1 ms polls, the excess is **read and silently discarded** (see §5)."
docs/WCB_KYBER_PASSTHROUGH_PARAMS.md:22: "| Poll cadence | source port is drained every ~1 ms; ≤1 frame per poll |"
docs/WCB_KYBER_PASSTHROUGH_PARAMS.md:60: "1. **64-byte batch cap** — > 64 bytes arriving at the source port between two 1 ms polls: excess dropped, no error."
docs/WCB_KYBER_PASSTHROUGH_PARAMS.md:84-88 (TL;DR): "Don't burst above 64 bytes/ms ... or bytes are dropped silently".

The doc's stated data path is "local serial (Maestro/droidnet) → WCB batches the port → one ESP-NOW frame (broadcast, target ID 98 'Kyber')" (docs/WCB_KYBER_PASSTHROUGH_PARAMS.md:8-10). That is `forwardDataFromKyber()` (Code/WCB/WCB.ino:4244), which no longer drops:

  Code/WCB/WCB.ino:4271-4276
    // Remote — accumulate once; broadcast reaches all remote boards. If the
    // buffer is full, FLUSH and keep going — don't silently drop bytes past
    // 64, which truncated a large Maestro burst into a corrupt frame.
    if (!addedToRemote) {
      if (remoteLen >= (int)sizeof(remoteBuf)) { sendESPNowRaw(remoteBuf, remoteLen); remoteLen = 0; }
      remoteBuf[remoteLen++] = b;

and identically at Code/WCB/WCB.ino:4288-4290 for the non-targeted branch. The adjacent comment states the drop was a real corruption bug that was fixed, so the doc is documenting a behaviour the source explicitly calls out as removed. Because the loop can flush more than once per drain, "≤1 frame per poll" (line 22) is also false — `forwardDataFromKyber` can emit ceil(N/64) frames in one 1 ms tick (Code/WCB/WCB.ino:6577-6583, `KyberLocalTask`, `vTaskDelay(pdMS_TO_TICKS(1))`).

The 64-byte truncation the doc describes DOES still exist, but only on the *other* two functions: `forwardMaestroDataToLocalKyber()` (Code/WCB/WCB.ino:4324 — `if (Maestro_Remote && remoteLen < (int)sizeof(remoteBuf))`) and `forwardMaestroDataToRemoteKyber()` (Code/WCB/WCB.ino:4343 — `if (remoteLen < (int)sizeof(remoteBuf))`). So the doc attributes the drop to the wrong direction.

**Failure scenario.** A droidnet/third-party integrator reading this page throttles their stream to 64 B/ms unnecessarily, and — worse — when they see real data loss they look at §5 point 1 ("64-byte batch cap") which is not the cause on their path, instead of the two places that still truncate (the Maestro→remote directions) or the egress-UART backpressure in §4.

**Suggested fix.** Rewrite §1's "Poll cadence" row and §5 point 1: state that `forwardDataFromKyber()` flushes a full 64-byte buffer and continues (so a burst becomes ceil(N/64) frames in one poll, not one frame with truncation), and that the 64-byte truncation now applies only to the Maestro-source batches at WCB.ino:4324 and :4343. Drop the "≤1 frame per poll" and "don't burst above 64 bytes/ms" guidance, or restate it as a pacing recommendation rather than a drop threshold. Add a Revision log to the page (it has none).

### F-418 · S3 · `docs/WDP_DESIGN.md:282`

**WDP_DESIGN says Maestro remote-proxy auto-add is "first-host-wins"; the code deliberately gives every host its own slot**

*Category:* `docs-drift`

docs/WDP_DESIGN.md:280-283: "On **first learn** of a peer‑hosted Maestro or WLED, its `id → board @ baud` is auto‑added as a remote proxy (`maestroAutoAddRemote` / `wledAutoAddRemote`) from the `MAESTRO_CFG` / `WLED_CFG` TLVs — **first‑host‑wins**, persisted."

That is true for WLED but false for Maestro. `maestroAutoAddRemote()` (Code/WCB/WCB_Maestro.cpp:723) looks up the slot by the full tuple, not by id:

  Code/WCB/WCB_Maestro.cpp:727
    int8_t slot = findSlotByMaestroIDPortTarget(maestroID, 0, hostWCB);

and then falls through to claim a NEW slot for any host it has not already proxied, with a comment that says so in as many words:

  Code/WCB/WCB_Maestro.cpp:738-741
    // Intentionally NO "already configured elsewhere" guard: duplicate Maestro ids are legal
    // (same id local AND/OR on multiple hosts), and sendMaestroCommand fans ;M<id> to every
    // matching slot, so each host gets its OWN proxy. The exact (id,host) duplicate was already
    // handled above; fall through to claim a fresh slot for this new host.
  Code/WCB/WCB_Maestro.cpp:742-753  (findEmptySlot() → new (id, hostWCB) proxy)

WLED, by contrast, really is one-slot-per-id and really is first-host-wins:
  Code/WCB/WCB_WLED.cpp:421-423 comment "One slot per id ... never shadow a local WLED or re-home a proxy that points at a different host (first-host-wins)", implemented at Code/WCB/WCB_WLED.cpp:424 `findWLEDSlotByID(wledID)`.

The repo's own CLAUDE.md:74-82 already states the correct Maestro behaviour ("**Auto-add is per-host, not first-host-wins**"), so docs/WDP_DESIGN.md contradicts both the code and the sibling doc.

**Failure scenario.** A reader sizing the 9-slot Maestro table (MAX_MAESTROS_PER_WCB, Code/WCB/WCB_Maestro.h:14) from this page assumes one proxy per Maestro id and concludes 8 ids fit comfortably. In a mesh where three boards each host Maestro id 1, one board accumulates three slots for id 1 alone, `;M1` multicasts to all three hosts (Code/WCB/WCB_Maestro.cpp:108, :304, :414 fan out over every matching slot), and the table can exhaust — the exhaustion is only logged under `debugEnabled` (Code/WCB/WCB_Maestro.cpp:744-746), so it is silent by default.

**Suggested fix.** Split the sentence in §9: WLED = one slot per id, first-host-wins; Maestro = one slot per (id, host), so `;M<id>` multicasts to every advertising host and the shared 9-slot table can exhaust. Note that proxies are never auto-evicted (`?MAESTRO,CLEAR` is the only cleanup) and add the row to the page's Revision log.

### F-419 · S3 · `docs/WLED_INTEGRATION.md:87`

**WLED_INTEGRATION.md documents `?WLED,PORT,CLEAR`, which is a no-op, and still describes the superseded one-WLED-per-board design**

*Category:* `docs-drift`

CLAUDE.md:12 routes "Working on WLED" to this page, but its command surface and its core architectural claims no longer match the firmware.

1. docs/WLED_INTEGRATION.md:87 documents `?WLED,PORT,CLEAR          # release`. `configureWLED()` recognises CLEAR only at the top level:
   Code/WCB/WCB_WLED.cpp:235-236
     if (aU == "CLEAR")  { clearWLEDConfig();   return; }
     if (aU.startsWith("CLEAR,")) { clearWLEDByID(a.substring(6)); return; }
   `PORT,CLEAR` instead enters the legacy branch at Code/WCB/WCB_WLED.cpp:242, where `specU` = "CLEAR" fails `specU.startsWith("S")` and the function prints a usage line and returns without clearing anything (Code/WCB/WCB_WLED.cpp:246-250). The command silently does nothing.

2. docs/WLED_INTEGRATION.md:119: "*(Several WLEDs on one WCB, or per‑node addressing within a single board, isn't needed — deferred.)*" and :43 "| **Multi‑node addressing** | ❌ 1 port ↔ 1 WLED |" and :21 "**One WLED per WCB**". Both shipped: Code/WCB/WCB_WLED.h:13-18 — "Every WLED in the system has a unique ID (1-9). A WCB may host several WLED nodes (one per serial port) and/or address WLED nodes physically wired to OTHER boards — a ;L<id> command routes to whichever board hosts that ID over ESP-NOW"; Code/WCB/WCB_WLED.h:51 `#define MAX_WLED_PER_WCB 9`; runtime dispatch `;L<id>,VERB` at Code/WCB/WCB_WLED.h:28.

3. docs/WLED_INTEGRATION.md:125 (§5): "The **only** tie‑in: a WCB that has a WLED configured can **advertise a 'drives WLED on Sx' capability flag** ... It's metadata about the WCB, not a control channel to WLED." WDP now carries a full `WLED_CFG` TLV of `[id][baudCode]` pairs (Code/WCB/WCB_WDP.cpp:74, built at Code/WCB/WCB_WDP.cpp:263-271) which drives automatic remote-proxy creation on the receiver (`wledAutoAddRemote`, Code/WCB/WCB_WLED.cpp:417-447) — i.e. it is actionable auto-config, not just metadata.

4. The real configure syntax is `?WLED,<id>:W<wcb>S<port>:<baud>` (Code/WCB/WCB_WLED.h:24, parsed at Code/WCB/WCB_WLED.cpp:255-268); the doc's §3 block only shows the legacy `?WLED,PORT,S1:115200` form.

**Failure scenario.** A user follows §3 and types `?WLED,PORT,CLEAR` to release a port before reassigning it. The board prints "[WLED] Legacy usage: ?WLED,PORT,S<port>:<baud>" and leaves the config intact; the port stays claimed, the subsequent HCR/MP3/PWM config on that port is rejected by the conflict guard (Code/WCB/WCB_WLED.cpp:323-329 and siblings), and the user has no indication which command actually clears it.

**Suggested fix.** Replace §3's config block with the shipped surface: `?WLED,<id>:W<wcb>S<port>:<baud>`, `?WLED,CLEAR`, `?WLED,CLEAR,<id>`, `?WLED,LIST`, `?WLED,STATUS` (Code/WCB/WCB_WLED.h:24-27). Update §4/§2 to state that multiple WLEDs per WCB and cross-board id addressing shipped, and rewrite §5 to describe the `WLED_CFG` TLV and receiver-side auto-add. Change the page's "Status: Draft — proposal" header to reflect that it now documents shipped behaviour, and add a Revision log.

### F-420 · S3 · `tests/wdp_wire_test.cpp:39`

**Mirror decoder has no FLAGS (0x12) case — the temporary-peer bit that decides NVS persistence is entirely untested**

*Category:* `test-coverage`

`tests/wdp_wire_test.cpp:39` declares the constant — `static const uint8_t T_FLAGS = 0x12, T_SEQHASH = 0x13;` — and then never uses it. The mirror's `switch` (tests:106-127) has no `case T_FLAGS`, the `Neighbor` struct (tests:60-70) has no `temporary` member, and no test function references it. Declaring the constant alongside T_SEQHASH (which *is* fully covered by `test_seqhash`, tests:204-240) makes the mirror read as complete when it is not.

The firmware decodes it, `Code/WCB/WCB_WDP.cpp:539-541`:

```
case WDP_TLV_FLAGS:   // advert flags bitmap — currently just the "temporary" bit
  if (len >= 1) nb.temporary = (val[0] & WDP_ADVFLAG_TEMPORARY) != 0;
  break;
```

and `nb.temporary` is the branch selector for whether a discovered device is written to flash. WCB_WDP.cpp:658-677: if set, `addTemporaryPeer()` (live, never persisted, evicted on silence); if clear, `addActivePeer(id, true)` — a *persisted* learned peer. It also gates the persisted auto-config block at WCB_WDP.cpp:704-705 (`!wcbPeerTemporary[senderWCB - 1]`, commented "never derive PERSISTED config from a temp peer"). This is a wire bit shared with the external WCB_Client library, which emits it at WCB_Client.cpp:1957 and decodes it at WCB_Client.cpp:2044-2046 — so both sides of a cross-repo contract ride on a bit the only test in the ecosystem does not touch. The TLV is documented in docs/WDP_DESIGN.md:96 and §6 (:161-169).

**Failure scenario.** A regression flips the mask polarity or the length guard at WCB_WDP.cpp:540 — e.g. `(val[0] & WDP_ADVFLAG_TEMPORARY) == 0`, or a future second flag bit is added and the bit test is rewritten as `val[0] == WDP_ADVFLAG_TEMPORARY`, which breaks the moment a device sets two bits. Every management relay and NaviCore advertising TEMPORARY is then adopted as a *permanent* learned peer and written to the `learned_peers` NVS blob. It survives reboot, is never evicted on silence, permanently consumes one of the 20 ESP-NOW peer slots, and under ETM leaves every broadcast pending an ACK it will never get (CLAUDE.md rule 2). The suite reports nine passing checks throughout.

**Suggested fix.** Add `bool temporary=false;` to the mirror `Neighbor` (tests:60-70) and `case T_FLAGS: if(len>=1) nb.temporary=(val[0]&0x01)!=0; break;` to the switch, plus a `test_flags_temporary()` asserting: bit set → temporary true; bit clear → false; absent TLV → false; and a value with an unrelated high bit (e.g. 0x03) still reads temporary true, which pins the bit-test-not-equality semantics.

### F-421 · S3 · `tests/wdp_wire_test.cpp:81`

**Test hardcodes MAX_WCB_COUNT and the 200-byte payload from files the workflow does not watch, and never exercises the range gate**

*Category:* `test-coverage`

The mirror hardcodes two firmware constants that are defined outside the workflow's path filter.

`tests/wdp_wire_test.cpp:81`: `if (senderWCB < 1 || senderWCB > 20) return DK_REJECTED;` — the firmware writes `if (senderWCB < 1 || senderWCB > MAX_WCB_COUNT) return;` (WCB_WDP.cpp:489), and `MAX_WCB_COUNT` is `20` at `Code/WCB/WCB_Storage.h:12`. The header is watched by the workflow only as `WCB_WDP.h`; `WCB_Storage.h` is not in `.github/workflows/wdp-tests.yml:9-13`.

The literal `200` appears eight times in the mirror (tests:89, 90, 93, 102, 105, and the `putTLV` max arguments) and corresponds to `char structCommand[200]` at `Code/WCB/WCB.ino:209` — also unwatched.

Worse, the range gate is never actually asserted. Every `decode()` call in the suite passes a sender of 2-6 (tests:169, 190, 197, 218, 229, 238, 249, 262, 273, 282-283). There is no CHECK for senderWCB=0, senderWCB=21, or a negative — the one gate standing between a wire-supplied integer and the array index `wdpNeighbors[senderWCB - 1]` (WCB_WDP.cpp:508, over a `WdpNeighbor wdpNeighbors[MAX_WCB_COUNT]` at :50). This matters disproportionately in this repo: hard-coded board caps of 8/9 are a previously-shipped bug family here, and a mirror that silently pins the old number is the exact shape that class takes.

**Failure scenario.** The fleet outgrows 20 boards and `MAX_WCB_COUNT` at WCB_Storage.h:12 is raised to, say, 24. The commit touches WCB_Storage.h and WCB.ino but not the three watched WDP files, so wdp-tests.yml never runs. The mirror keeps asserting the stale `> 20` boundary, so the suite claims to cover a gate whose real bound has moved — boards 21-24 are silently excluded from the guard the test documents. Symmetrically, if the gate at WCB_WDP.cpp:489 were ever weakened to `senderWCB > 0` alone, no test would catch the resulting out-of-bounds write at WCB_WDP.cpp:508 because no test ever passes an out-of-range sender.

**Suggested fix.** Add `test_sender_range()` asserting `decode(0,...)`, `decode(21,...)` and `decode(-1,...)` all return DK_REJECTED while `decode(20,20,...)` is accepted — that pins both ends of the bound. Define the mirror's limit as a single named constant with a comment tying it to WCB_Storage.h:12, and add `Code/WCB/WCB_Storage.h` (and ideally `Code/WCB/WCB.ino`, see the MAC-binding finding) to both `paths:` lists in wdp-tests.yml.

### F-422 · S3 · `tests/wdp_wire_test.cpp:83`

**The MAC-binding gate the test asserts lives in WCB.ino, a file wdp-tests.yml does not watch — and the test cites the wrong file for it**

*Category:* `test-coverage`

`tests/wdp_wire_test.cpp:83-85` mirrors the anti-spoof gate and cites it as living in the decoder:

```
// sender-MAC binding (WCB_WDP.cpp §8): claimed id must equal the source MAC's
// last octet. A rogue can't claim another board's id.
if (srcMacLastOctet != (uint8_t)senderWCB) return DK_REJECTED;
```

`test_mac_binding()` (tests:277-284) is one of the nine named checks, so the suite reads as if this gate is covered. It is not in `Code/WCB/WCB_WDP.cpp` at all. I grepped the whole file: `wdpOnAdvertReceived` (WCB_WDP.cpp:487-490) gates only on `wdpEnabled`, `senderWCB < 1 || senderWCB > MAX_WCB_COUNT`, and magic/version. The comment at WCB_WDP.cpp:646-647 says so outright — "The sender id was bound to its source MAC in the receive callback, so senderWCB is trustworthy." The real gate is a single line in the ESP-NOW callback, `WCB.ino:3756`: `if (info->src_addr[5] != senderWCB) { ... return; }`. The "§8" in the citation is a section of `docs/WDP_DESIGN.md` (line 227, "## 8. Security", item 4 at :230-234), not of any .cpp — so the citation points a maintainer at a file that does not contain the code.

The consequence is mechanical: `.github/workflows/wdp-tests.yml:9-13` and `:15-19` filter to exactly `tests/wdp_wire_test.cpp`, `Code/WCB/WCB_WDP.cpp`, `Code/WCB/WCB_WDP.h`, and the workflow file. `Code/WCB/WCB.ino` is not in the list. I verified WDP has exactly one ingress (`enqueueWdpPacket` is defined at WCB.ino:986 and called only at WCB.ino:3785, downstream of the check), so WCB.ino:3756 is the whole gate. Deleting or weakening it leaves the suite green *and* does not even trigger the workflow to run.

**Failure scenario.** A maintainer refactoring `espNowReceiveCallback` drops or reorders `if (info->src_addr[5] != senderWCB)` at WCB.ino:3756 — plausible, since the adjacent comment at WCB.ino:3721-3722 already records a previous removal of a MAC check as "redundant". The commit touches only WCB.ino, so wdp-tests.yml never fires; even a manual run passes because the test re-implements the check locally. The release ships with WDP id-spoofing re-opened: a rogue in-group node claims another board's id, gets auto-joined (WCB_WDP.cpp:657-677), and can spoof a controller advert to trigger `enableControllerPeer` (WCB_WDP.cpp:625-628) — the exact three attacks docs/WDP_DESIGN.md:230-234 claims are closed.

**Suggested fix.** Two independent fixes, both cheap. (1) Correct the citation at tests:83 to `WCB.ino:3756 / docs/WDP_DESIGN.md §8` so the mirror points at real code. (2) Add `Code/WCB/WCB.ino` to both `paths:` lists in .github/workflows/wdp-tests.yml (:9-13, :15-19) so a change to the gate at least re-runs the suite. Better still, move the binding assertion out of the mirror: have the test `grep`-assert the literal `info->src_addr[5] != senderWCB` exists in WCB.ino, so deletion is a hard failure rather than a silent one.

### F-423 · S3 · `tests/wdp_wire_test.cpp:118`

**Mirror decoder omits the PORTLABEL 24-byte clamp — the only guard against a 255-byte label overrunning portLabels[5][25]**

*Category:* `test-coverage`

The mirror's PORTLABEL case, `tests/wdp_wire_test.cpp:118`:

```
case T_PORTLABEL: if(len>=1){ int p=val[0]; if(p>=1&&p<=5) nb.portLabels[p-1].assign((const char*)(val+1), len-1);} break;
```

The firmware's, `Code/WCB/WCB_WDP.cpp:581-591`:

```
case WDP_TLV_PORTLABEL: {
  if (len >= 1) {
    int port = val[0];
    if (port >= 1 && port <= 5) {
      int L = len - 1; if (L > 24) L = 24;
      memcpy(nb.portLabels[port - 1], val + 1, L);
      nb.portLabels[port - 1][L] = '\0';
```

`if (L > 24) L = 24;` at WCB_WDP.cpp:585 is absent from the mirror. The destination is `char portLabels[5][25]` (WCB_WDP.h:67), and the wire length byte is unvalidated up to 255 — so that clamp is the entire buffer-overrun guard on this TLV. The mirror writes into a `std::string` (tests:67), which grows, so removing the equivalent clamp in the mirror produces no observable difference and no test failure.

This is the only TLV where the mirror drops a bound. I diffed all fourteen decoded cases: ALIAS 24 (tests:107 / cpp:525), FWVER 27 (:108/:528), HWVER len>=1 (:109/:531), CAPFLAGS len>=2 (:110/:534), CTRLID len>=1 (:111/:537), DEVTYPE 24 (:112/:548), HWREV 15 (:113/:552), CAPTAGS 48 (:114/:555), MAESTRO 9 (:115/:558), MAESTRO_CFG 9 recs (:116/:564), WLED_CFG 9 recs (:117/:573), PWMTARGET dedup + cap 5 (:119-123/:592-604), SEQHASH len>=4 (:124/:543) — all match exactly. PORTLABEL is the single divergence, and it sits precisely on the buffer bound. The shipping firmware is correct today (I also confirmed WCB_Client clamps identically, WCB_Client.cpp:2054), so this is a guard gap, not a live overrun. Note also that the test's own header sells this coverage: `test_unknown_and_truncated` (tests:182-198) is titled "truncation safety" but only exercises an over-long *length byte*, never an over-long *label*.

**Failure scenario.** Someone simplifies WCB_WDP.cpp:585 to `memcpy(nb.portLabels[port-1], val+1, len-1)` (it looks redundant next to the `o+2+len>200` bounds check at :522, which bounds the read but not the write). A neighbor — or a hostile in-group node past the MAC gate — sends a PORTLABEL TLV with len=200, port=1. 199 bytes are memcpy'd into a 25-byte array inside the global `wdpNeighbors[MAX_WCB_COUNT]`, smashing 174 bytes of the adjacent neighbor rows (pwmSelfPorts, isClient, temporary, seqHash and the next record). The suite passes; the release ships. The corrupted `temporary`/`pwmSelfCount` fields then drive NVS writes via the auto-config block at WCB_WDP.cpp:704-761.

**Suggested fix.** Add the clamp to the mirror so it can drift-detect: `int L = len-1; if (L>24) L=24; nb.portLabels[p-1].assign((const char*)(val+1), L);` — and add a CHECK to `test_unknown_and_truncated` that feeds a PORTLABEL with a 200-byte value and asserts the decoded label is exactly 24 chars.

### F-424 · S3 · `tests/wdp_wire_test.cpp:308`

**test_autojoin_vetting is a tautology — it tests a lambda defined two lines above it, not the firmware**

*Category:* `test-coverage`

`tests/wdp_wire_test.cpp:308-316` is the entire auto-join test:

```
static void test_autojoin_vetting(){
  printf("auto-join needs >=2 adverts...\n");
  // Mirror the wcbPeerAdvertCount>=2 gate (WCB_WDP.cpp:573): joining only after a
  // second advert so one stray/echoed packet can't inject a peer.
  int advertCount=0; bool joined=false;
  auto onAdvert=[&](){ if(++advertCount>=2) joined=true; };
  onAdvert(); CHECK(!joined, "one advert does not join");
  onAdvert(); CHECK(joined,  "second advert joins");
}
```

The lambda is declared on line 313 and asserted on lines 314-315. Nothing here derives from the firmware — it cannot fail unless `++` or `>=` break. Unlike the other eight tests, it does not even route through the mirror `decode()`; it shares no code path with anything.

The real gate it names is `Code/WCB/WCB_WDP.cpp:652-678`, and the parts that carry actual decision logic are all unexercised: the special-peer skip (`bool isSpecialPeer = specialPeerEnabled && senderWCB == WCB_SPECIAL_PEER_ID;` :655, used at :657), the temporary-vs-permanent fork and the learned→temporary downgrade (`if (nb.temporary) { if (vetted && !wcbPeerTemporary[idx]) ... }`, :658-670), the already-active short-circuit (`else if (!wcbPeerActive[idx] && vetted)`, :671), and the saturating counter (`if (wcbPeerAdvertCount[idx] < 255) wcbPeerAdvertCount[idx]++;`, :654). Of the nine checks the suite prints, this one contributes zero coverage while occupying a named slot in the summary.

**Failure scenario.** The vetting threshold is changed to `>= 1` (or the counter reset at WCB.ino:1078/:6991 is dropped so a forgotten peer re-joins instantly on its next advert). `test_autojoin_vetting` passes unchanged — it never reads the firmware constant — and the suite still prints "auto-join needs >=2 adverts... All checks passed." A single stray or echoed advert injects a persisted learned peer, which is exactly the attack the comment at WCB_WDP.cpp:656 says the gate exists to prevent.

**Suggested fix.** Either delete the test (an empty slot is more honest than a green one), or make it real: lift the decision into a mirrored pure function `bool wdpShouldJoin(uint8_t advertCount, bool active, bool temporary, bool isSpecial, bool alreadyTemp)` mirroring WCB_WDP.cpp:654-677 and table-test the fork — special peer never joins; temporary+vetted → temporary; permanent+vetted+!active → learned; already-learned + temporary advert → downgrade; count==1 → nothing.

### F-425 · S3 · `Wizard/app.js:621`

**addBoardSection sizes a board's WCB-number dropdown from the WCBQ floor, so a WDP-discovered board above the floor displays — and can only be set to — the wrong number**

*Category:* `board-count-cap`

When a section is created for a board, the WCB-number `<select>` is populated with a range taken from the WCBQ floor while the *selected* value is the board's own number:

```js
616:  if (!boardConfigs[n]) {
617:    boardConfigs[n] = WCBParser.createDefaultBoardConfig();
618:    boardConfigs[n].wcbNumber = n;
619:  }
620:  const wcbNumSel = document.getElementById(`b${n}-wcb-number`);
621:  const qty = systemConfig?.general?.wcbQuantity || n;
622:  if (wcbNumSel) populateWCBDropdown(wcbNumSel, qty, n, true);
```

`populateWCBDropdown` only emits options `1..max` and only restores the selection when it is inside that range (`Wizard/app.js:1086-1100`: `if (selectedVal && selectedVal <= limit) sel.value = selectedVal;`). Sections are created for boards above the floor by design — `desiredBoardNumbers()` unions the WDP-discovered `_meshBoards` set into the grid (`Wizard/app.js:429-438`) and `reconcileBoardGrid` calls `addBoardSection(n)` for each (`:462`). So for a discovered board 12 with `wcbQuantity` 2, the dropdown offers only 1 and 2 and settles on `1`, while `boardConfigs[12].wcbNumber` is 12 and the card header (which reads the config, `updateBoardAliasUI`) says WCB 12.

The user cannot correct it from the UI either: `onWCBNumberChange` applies the same floor bound to whatever is picked —

```js
1881:  const qty = systemConfig?.general?.wcbQuantity || WCB_MAX;
1882:  if (boardConfigs[n] && val >= 1 && val <= qty) boardConfigs[n].wcbNumber = val;
```

— and the option for 12 does not exist to pick.

This is the one board-number dropdown that missed the fix applied to its sibling: `populateUIFromConfig` widens the range explicitly and says why (`Wizard/app.js:2927-2932`, `selectedWcb   // always include the board's self-reported number`). So the field self-corrects the moment a config is pulled or the board connects, which is exactly why the defect survives — it is visible only in the pre-pull window and for boards that never get pulled.

**Failure scenario.** WCBQ floor is 2. WCB12 is auto-joined and advertising, so the Wizard renders a section for it (`_meshBoards` -> `desiredBoardNumbers` -> `addBoardSection(12)`), but it is reachable only over a relay and has not been pulled yet. Its card header reads "WCB 12" while the WCB Number field on the same card reads "1". An operator checking board identity before a push sees a card claiming to be two different boards, and opening the dropdown to fix it offers only 1 and 2 — selecting either silently rewrites `boardConfigs[12].wcbNumber` to a number already owned by another board, which then collapses two boards into one on a config-file save/reload.

**Suggested fix.** Use the same widening as `populateUIFromConfig`: `const qty = Math.max(systemConfig?.general?.wcbQuantity || 0, parseInt(document.getElementById('g-wcbq')?.value) || 0, n);` before `populateWCBDropdown(wcbNumSel, qty, n, true)`. Apply the same `Math.max(..., val)` widening to the bound in `onWCBNumberChange` (:1881) so a legitimately out-of-floor number can be selected and stored.

### F-426 · S3 · `Wizard/app.js:4406`

**Wizard updateSequence() has no key-length validation — the maxlength="15" attribute does not constrain keys loaded from a config pull or an imported backup, so an over-long key round-trips back to the firmware**

*Category:* `validation`

The Wizard's only guard on a sequence key is the `maxlength="15"` attribute and the `${key.length}/15` counter that `appendSequenceRow()` renders (Wizard/app.js:4217-4221, counter updated by `updateSeqKeyCount()` at :4181-4187).

`maxLength` constrains user editing only — it does not apply to a value written into the element by the page. `appendSequenceRow()` sets the field programmatically at :4217 (`value="${escHtml(key)}"`, plus `data-original-key`), so any key that arrives from a board's config pull or from an imported backup file populates the row at full length and the counter simply displays e.g. `18/15`.

`updateSequence()` (:4400-4414) then validates only non-empty key (:4406), non-empty value (:4412) and `validateSequenceValue(value)` (:4414). There is no length check anywhere on the push path, so the over-long key is sent back out as `?SEQ,SAVE,<longkey>,<value>` — where the firmware silently fails to persist it (see the WCB_Storage.cpp:643 finding) while reporting success.

This is the second half of the same trap: the firmware has no guard and relies on the Wizard; the Wizard's guard covers typing but not the two paths by which a bad key actually enters the tool.

**Failure scenario.** A board carries a phantom over-long name in `key_list` (created earlier from the console or by an older tool). The user connects the Wizard, which pulls the config and calls `appendSequenceRow(n, 'dome_panels_wave', '')` — the row shows the full 16-char key and `16/15`. The user types a body and clicks SAVE/UPDATE. The Wizard sends `?SEQ,SAVE,dome_panels_wave,...`, the board answers "Stored:", the Wizard marks the row saved and clean, and the next verify-pull still shows an empty value — an unbreakable loop in which the tool reports success and the sequence never exists. The same happens on importing a backup file containing that line.

**Suggested fix.** Add the check to the push path, not just the input: in `updateSequence()` after :4406, `if (key.length > 15) { showToast('Sequence key must be 15 characters or fewer (NVS key limit)', 'error'); return; }`. Also flag it at load time — have `appendSequenceRow()` (:4201) mark the row invalid when `key.length > 15` so an imported or pulled phantom key is visibly bad rather than silently unsavable, and colour the `${keyLen}/15` counter accordingly.

### F-427 · S3 · `Wizard/parser.js:1281`

**The Wizard drops a custom controller (NaviCore) peer ID whenever the controller is disabled, defeating the firmware's deliberate two-command preservation**

*Category:* `config-roundtrip`

The firmware goes out of its way to keep a non-default controller id across a restore:
```
WCB.ino:2876-2886
  if (specialPeerEnabled) {
    emit("CONTROLLER,ON," + String(WCB_SPECIAL_PEER_ID), true);
  } else if (WCB_SPECIAL_PEER_ID != 20) {
    // Preserve a CUSTOM controller id even while the controller is disabled — set
    // it then turn it back off — so a restore doesn't silently revert it to the
    // default 20.
    emit("CONTROLLER,ON," + String(WCB_SPECIAL_PEER_ID), true);
    emit("CONTROLLER,OFF", true);
  }
```
The Wizard PARSES that pair correctly — `case 'CONTROLLER'` at parser.js:511-522 sets `config.specialPeerId` from the ON token and the guard `if (config.specialPeer && parts[2])` at :517 stops the following OFF token from wiping it — so after a pull the model holds `{ specialPeer: false, specialPeerId: 17 }`.

But `buildCommandString()` collapses that back to a single token:
```
parser.js:1277-1281
  const curSpecial  = !!config.specialPeer;
  ...
  add(curSpecial ? `CONTROLLER,ON,${config.specialPeerId ?? 20}` : `CONTROLLER,OFF`);
```
When `specialPeer` is false the id is never emitted, in a full push or in an exported system file (`buildSystemFile` routes the general section through the same builder — parser.js:1568-1572 copies `specialPeer`/`specialPeerId` into the synthetic general board). On the receiving board `?CONTROLLER,OFF` calls `saveSpecialPeerPreferences(false)` (WCB.ino:4880-4884 → WCB_Storage.cpp:404-411) and never touches `special_peer_id`, which stays at its default 20 (`loadSpecialPeerIDFromPreferences`, WCB_Storage.cpp:414-418).

The id range is genuinely operator-chosen 1-20 (`saveSpecialPeerIDToPreferences` bounds check at WCB_Storage.cpp:423-426; the Wizard's own step-3b picker offers 1-20 at app.js:9984-9985). I found no prior finding covering this emit-side gap.

**Failure scenario.** A fleet pins its controller to ID 17 (`?CONTROLLER,ON,17`), then disables it for bench work (`?CONTROLLER,OFF`). The board's own ?backup still restores 17, but the operator uses the Wizard: pull → Export System File → later restore onto a replacement board. The file contains only `?CONTROLLER,OFF`, so the replacement keeps `special_peer_id = 20`. When the operator re-enables the controller from the Wizard it emits `CONTROLLER,ON,20` (the model's `specialPeerId` was reset by the fresh parse), the ESP-NOW peer for WCB17 is never registered, and the controller is unreachable with nothing in the logs pointing at the id.

**Suggested fix.** Mirror the firmware: when `!curSpecial && (config.specialPeerId ?? 20) !== 20`, emit `CONTROLLER,ON,${config.specialPeerId}` followed by `CONTROLLER,OFF`; otherwise keep the current single-token behaviour.


---

## S4

### F-428 · S4 · `.github/workflows/build.yml:140`

**build.yml's rebase-retry comment cites a file that does not exist and contradicts the path filter rule it sits next to**

*Category:* `stale-comment`

Lines 140-141 read:
```
# Rebase-and-retry: every commit stamps fw_version.h (under Code/), so even
# Wizard-only commits trigger this build. Concurrent runs then race to push…
```
Both clauses are false:
1. **There is no `fw_version.h` in this repository.** `grep -rl fw_version .` returns exactly one file — `build.yml` itself, i.e. only this comment.
2. The real stamper is the local hook `.git/hooks/pre-commit.sh`, and it is scoped: `:7-8` set `WCB_CHANGED` only when the staged paths match `Code/WCB/`, and `WIZARD_CHANGED` only for `Wizard/`; `:37-58` then stamp `Code/WCB/WCB.ino` **only if** `WCB_CHANGED`, and `Wizard/app.js` only if `WIZARD_CHANGED`. A Wizard-only commit therefore touches nothing under `Code/**` and does **not** trigger this workflow.

So the comment asserts precisely the thing this file's own header (`:6-13`) and CLAUDE.md rule 9 exist to prevent — "Wizard-only / docs-only pushes must NOT trigger a rebuild". The path filter at `:11-13` is correct and does hold (verified: the git log shows `Auto-build` commits following `Code/**` commits such as `bbd6bdf`, and none following Wizard-only commits such as `3d74fa8`/`6f3ad75`). But anyone reading `:140-141` while debugging the retry loop is told the filter is ineffective, which invites exactly the "fix" (widening or removing the filter) that would reintroduce binary churn on every unrelated push.

**Failure scenario.** A future maintainer investigating push races reads line 140, concludes the `Code/**` filter is not doing its job, and either removes it or adds `Wizard/**` to it. Every Wizard or docs push then recompiles both ESP32 targets — which always produce differing images because the build stamps a fresh timestamp — and pushes new binaries, restoring the exact churn rule 9 was written to stop.

**Suggested fix.** Replace lines 140-141 with the true rationale: the retry exists because two pushes to `Code/**` in quick succession produce overlapping runs that both try to push a binary commit. Delete the `fw_version.h` claim entirely, and note the real stamping mechanism (`.git/hooks/pre-commit.sh`, which stamps `WCB.ino` only when `Code/WCB/` is staged).

### F-429 · S4 · `.github/workflows/wdp-tests.yml:22`

**wdp-tests.yml declares no permissions block, so the test job runs with the repository's default token scope**

*Category:* `permissions`

`build.yml:15-16` and `pages-deploy.yml:40-41` each declare `permissions: contents: write` because they genuinely push. `wdp-tests.yml` declares no `permissions:` key at all (grep over `.github/workflows/*.yml` shows `permissions` only at `build.yml:15` and `pages-deploy.yml:40`), so its `GITHUB_TOKEN` inherits whatever the repository default is — and `pages-deploy.yml:25-26` records that this repo has Actions write access enabled. The job only checks out (`:26`), compiles `tests/wdp_wire_test.cpp` with g++ (`:28`) and runs the produced binary (`:30`); it needs `contents: read` and nothing more.

On the second half of the question — whether this workflow gates anything — it does not, and that is deliberate and documented at `:5` ("Independent of the firmware build workflow so it can never block a firmware push"), so I am not reporting it as a defect. Worth recording as context: `build.yml` has no dependency on it, and the test is a hand-maintained *mirror* of the wire format rather than a link against `WCB_WDP.cpp` (documented at `tests/wdp_wire_test.cpp:11-20`). I spot-checked the mirror against the source and found no drift — TLV ids `0x00/0x01/0x03/0x04/0x05/0x06/0x09/0x0A/0x0B/0x0C/0x0D/0x0E/0x0F/0x10/0x11/0x12/0x13` (`WCB_WDP.cpp:67-97`), `WDP_MAGIC 'W'` (`:66`), `WDP_PROTO_VERSION 0x01` (`WCB_WDP.h:34`), the 14-entry baud table (`WCB_WDP.cpp:129-132`) and `putTLV`'s clamps (`:216-224`) all match the mirror at `tests/wdp_wire_test.cpp:33-58` byte for byte.

**Failure scenario.** A compromised or malicious change to `tests/wdp_wire_test.cpp` (the workflow compiles and executes it) runs with a token that can write repository contents, because no `permissions:` block narrows it — even though the job needs only read access. The same job also runs on `pull_request` (`:14`), widening who can influence what it compiles.

**Suggested fix.** Add `permissions: { contents: read }` at the top of `.github/workflows/wdp-tests.yml` (workflow level, above `jobs:`). While there, consider a `timeout-minutes` on the job — `pages-deploy.yml:58` sets one and `build.yml` does not, so a wedged run there can burn the 360-minute default.

### F-430 · S4 · `CLAUDE.md:31`

**CLAUDE.md rules 2 and 3 cite six WCB.ino line numbers that no longer point at the code they quote**

*Category:* `docs-drift`

CLAUDE.md's two ETM broadcast rules are the ones it flags as "easy to break", and both anchor on stale lines. Each was checked with `sed -n '<line>p' Code/WCB/WCB.ino`:

- CLAUDE.md:31 — "(`WCB.ino:1484` \"send to broadcast but track per-board via ACKs\")". Line 1484 is `(max((unsigned long)etmCharDelayMs, 20UL) + etmTimeoutMs) + 3000;`. The quoted comment is at Code/WCB/WCB.ino:1549, inside `advanceETMCharPhase()` — and it describes the ETM *characterization* test's phase 2, not the general per-board broadcast ACK machinery, which lives in `PendingMessage` / `etmPendingTable` at Code/WCB/WCB.ino:509-519 and the retry sweep at Code/WCB/WCB.ino:1280-1341.
- CLAUDE.md:33 — "(`WCB.ino:6497`, `:6521`)" for where a pending broadcast times out or is removed. 6497 is blank; 6521 is `case ESP_RST_DEEPSLEEP: Serial.println("Deep Sleep Reset"); break;` (a reset-reason printer). The retry/expiry logic is at Code/WCB/WCB.ino:1280-1345 (`entry.retryCount[b] < 3` at :1300, increment at :1335).
- CLAUDE.md:36 — boot announce "(`WCB.ino:966`)". Line 966 is blank; the boot-announce send is at Code/WCB/WCB.ino:945-954 (`bp.structPacketType = PACKET_TYPE_ETM_BOOT;` at :954) and the cadence comment at Code/WCB/WCB.ino:1022.
- CLAUDE.md:37 — rc_hb/rc_ch telemetry "(`WCB.ino:2170`)". Line 2170 is `uint8_t baseMac[6];`. The relevant comment is at Code/WCB/WCB.ino:2306: "Best-effort JSON telemetry broadcasts (rc_hb / rc_ch etc.) are never ACKed".
- CLAUDE.md:38 — the `emit*` helpers "(`WCB.ino:3276`)". Line 3276 is `xQueueSend(mgmtReqQueue, &slot, 0);`. The `emit(...)` helper family is at Code/WCB/WCB.ino:2763-2800.
- CLAUDE.md:39 — "any explicit `sendESPNowMessage(0, …, false)` (`WCB.ino:1549`)". Line 1549 is the phase-2 comment quoted by rule 2 above; the actual un-ACKed broadcast sends are at Code/WCB/WCB.ino:1495 (`sendESPNowMessage(0, "ETMLOAD", false)`) and Code/WCB/WCB.ino:1614 (`sendESPNowMessage(0, payload.c_str(), false);  // broadcast, not ETM`).

The rules' *substance* is correct — I confirmed the 3-retry ETM behaviour (Code/WCB/WCB.ino:1300), the un-ACKed boot announce, and the un-ACKed telemetry path. Only the anchors are wrong. For contrast, rule 5's Maestro citations are still accurate: WCB_Maestro.cpp:100-107 is the dedup comment and :108/:304/:414 are the `for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++)` fan-out loops; WCB_WDP.h:20-21 does carry the "WDP requires ETM enabled" statement.

**Failure scenario.** A session onboarding via CLAUDE.md follows :31 to WCB.ino:1484 to understand broadcast ACK fan-out, lands mid-expression in ETM characterization timing, and either concludes the rule is about the `?ETM,CHAR` test only (it is not) or gives up and reasons from the prose alone — the failure mode CLAUDE.md:130-133 exists to prevent ("Never assert a constant, TLV, field name, or pin from a doc alone — open the file and cite file:line").

**Suggested fix.** Repoint rule 2 at Code/WCB/WCB.ino:509-519 (PendingMessage / per-board retryCount) and :1280-1345 (retry + expiry sweep) rather than the characterization-test comment; repoint rule 3 at :945-954 (boot announce), :2306 (rc_hb/rc_ch), :2763-2800 (emit helpers) and :1495 / :1614 (explicit `sendESPNowMessage(0, …, false)`). Since these drift on every WCB.ino edit, prefer naming the function (`advanceETMCharPhase`, `checkETMPendingMessages`) with the line as a hint.

### F-431 · S4 · `Code/bin/build.sh:22`

**Wizard/bin is rebuilt and committed on every build but nothing reads it; the comment claiming the Wizard flashes from it is wrong**

*Category:* `dead-artifact`

`build.sh:22-23` document the `Wizard/bin/` outputs as "stable name for wizard auto-flash", and the script writes eight files there (`:186-188`, `:192-193`, `:197-198`, `:202`); `build.yml:134` commits them on every build. Nothing consumes them:
- `Wizard/flasher.js:44` `const GITHUB_BIN_PATH = 'Code/bin';` and `:118` builds `https://api.github.com/repos/…/contents/${GITHUB_BIN_PATH}?ref=${GITHUB_BRANCH}`, then fetches each file's `download_url` (`:127-131`). The flasher never reads a relative path.
- `Wizard/app.js:127-133` (`fetchLatestFirmwareVersion`) uses the same Contents API on `Code/bin`.
- `grep -rn "Wizard/bin\|bin/WCB_\|WCB_ESP32" Wizard/*.js Wizard/index.html` returns nothing — the only references to `Wizard/bin` in the whole repo are `build.sh`, `build.yml:134`, and a prior code-review doc.

Costs: ~2.5 MB of duplicate binaries added to git history on every firmware build (`Wizard/bin/WCB_ESP32.bin` alone is 1,284,464 bytes), and `pages-deploy.yml:95` `cp -a "$WIZ/." "$GHP/$DEST/"` copies the whole directory — bins included — into gh-pages for **every** branch slice, where old branch previews are never garbage-collected. Note also that because the auto-build commit message is `[skip ci]` (`build.yml:132`), the freshly written `Wizard/bin` never triggers `pages-deploy.yml` anyway, so the copies published to Pages are always one Wizard-push behind — harmless only because nothing reads them.

**Failure scenario.** Not a runtime failure. A maintainer reading `build.sh:22-23` believes the Wizard auto-flashes from `Wizard/bin/`, and 'fixes' a stale-firmware complaint by re-copying files there instead of looking at `Code/bin` on the branch the Wizard is served from — where the flasher actually looks (`flasher.js:44`, `:118`). Meanwhile every release permanently adds ~2.5 MB to the repo and to each gh-pages branch slice.

**Suggested fix.** Either delete the `Wizard/bin` copies from `Code/bin/build.sh:186-202` and drop `Wizard/bin/` from `build.yml:134` (plus `git rm -r Wizard/bin`), or, if they are wanted as an offline fallback, keep them but correct the comment at `:22-23` and add an explicit `.gitignore`/exclusion so `pages-deploy.yml:95` stops republishing them into every preview slice.

### F-432 · S4 · `Code/WCB/WCB_Help.cpp:727`

**?VAR? example `;PP100` is not a valid command and is silently discarded**

*Category:* `help-syntax-mismatch`

The `?VAR?` topic demonstrates compound conditions with (WCB_Help.cpp:727):

```cpp
        Serial.println(F("  IF,mode>2,AND,armed=1^;PP100   - compound condition"));
```

The gated command `;PP100` is malformed. `processCommandCharcter()` routes any `p`/`P` body to the PWM output handler (WCB.ino:5857-5858), so `processPWMOutput()` receives `message = "PP100"` and parses (WCB.ino:6190-6191):

```cpp
    int port = message.substring(1, 2).toInt();
    unsigned long pulseWidth = message.substring(2).toInt();
```

`substring(1,2)` is `"P"`, and `toInt()` on `"P"` returns 0. The guard at WCB.ino:6193 (`port < 1`) rejects it, and the diagnostic is gated behind `debugPWMEnabled`, which is never assigned true anywhere in the firmware — WCB.ino:177 is its only assignment and WCB.ino:6194 its only read. The command vanishes with no output.

Even read charitably as a typo for `;P100`, that still fails: port would be 1 and `substring(2)` = `"00"` = 0, below the 500 minimum. There is no reading of `;PP100` that produces a pulse. The valid form is `;P<port><width>` with width 500-2500, e.g. `;P41500`, per the code comment at WCB.ino:6188.

This is the same `;P` grammar confusion as the `?CMDCHAR?` `;Px,width` line — two different help pages, two different wrong spellings, and the correct one documented nowhere. The rest of the `?VAR?` topic checks out: I verified `;V`/`;VP` against `processSetVariable()` (WCB_Variables.cpp:209-249), the 1-15 char name limit against `WCB_VAR_NAME_MAX` (WCB_Variables.h:28) and `isValidVariableName()` (WCB_Variables.cpp:65), the 100-variable cap against `WCB_MAX_VARIABLES` (WCB_Variables.h:27), the six IF operators against `evalOneCondition()` (WCB_Variables.cpp:341-352), and the "?VAR,SET creates as PERSISTENT" claim against `setVariable()` → `setVariableImpl(..., true)` (WCB_Variables.cpp:171-173).

**Failure scenario.** A user copies the compound-condition example as a template, substitutes their own condition, and keeps `;PP100` believing it is a valid PWM action. The IF evaluates correctly, the gated command is dispatched, and nothing happens — no pulse, no error. They debug the IF logic (which is fine) rather than the example command.

**Suggested fix.** Replace the payload with a command that exists and is already used elsewhere in the same topic — e.g. reuse the `;M11` form from WCB_Help.cpp:726:

```cpp
Serial.println(F("  IF,mode>2,AND,armed=1^;M21     - compound condition"));
```

If a PWM example is wanted specifically, spell it correctly as `;P41500` and fix the `?CMDCHAR?` grammar line (WCB_Help.cpp:925) in the same commit so the two pages agree.

### F-433 · S4 · `Code/WCB/WCB_Help.cpp:845`

**?WDP? help omits the POLL and DETAIL,<n> subcommands**

*Category:* `help-coverage`

The `?WDP?` topic enumerates subcommands across three blocks — View (WCB_Help.cpp:845-850), Enable/discovery (:852-855), Peer membership (:857-859) — listing `LIST`, `<n>`, `STATUS`, `DUMP`, `DA`, `ON`, `OFF`, `AUTOJOIN[,ON|,OFF]`, `ADD,<id>`, `FORGET,<id>`, `CLEAR`.

`processWdpCommand()` accepts two more (WCB_WDP.cpp:1201-1275):

```cpp
  // ?WDP,POLL — force convergence now: advertise ourselves + broadcast a SOLICIT so
  // every other board advertises within ~1 s instead of waiting for the 60 s backstop.
  if (au == "POLL") {                                          // :1211
    if (!wdpEnabled || !etmEnabled) { Serial.println("[WDP] POLL needs WDP + ETM enabled"); return; }
    wdpSendAdvert();
    wdpSendSolicit();
```

and the explicit detail form at :1266:

```cpp
  // ?WDP,DETAIL,<n>  or  ?WDP,<n>  → drill into one neighbor
  if (au.startsWith("DETAIL")) {
```

`DETAIL,<n>` is a minor omission — the bare `?WDP,<n>` shorthand is documented at WCB_Help.cpp:848 and reaches the same `printWdpDetail()` via WCB_WDP.cpp:1271-1272. `POLL` is the one with real value: without it, a user waiting for a newly-powered board to appear in the neighbor table has to wait out the periodic backstop (`WDP_PERIOD_MS`, scheduled at WCB_WDP.cpp:1301) rather than forcing convergence in about a second.

Mitigating: the parser's own unknown-subcommand error does list both, at WCB_WDP.cpp:1274-1275:

```cpp
  Serial.printf("[WDP] unknown subcommand '%s' (LIST | <n> | DETAIL,n | STATUS | DUMP | DA | "
                "POLL | ON | OFF | AUTOJOIN[,ON|,OFF] | ADD,<id> | FORGET,<id> | CLEAR)\n", a.c_str());
```

so a user who guesses wrong is shown the full set — which is why this is S4 and not higher. But a user who reads the help and never mistypes will not discover POLL. The rest of the topic verified clean against the parser: ON/OFF, AUTOJOIN query-vs-set semantics (:1223-1232), ADD/FORGET bounds `1..MAX_WCB_COUNT` (:1238, :1249), CLEAR (:1258), and the "default ON" claims for both flags against `loadWdpSettings()` (WCB_WDP.cpp:1282-1283).

**Failure scenario.** A user adds a board to the mesh and it does not show in `?WDP,LIST`. They re-check wiring, toggle `?WDP,OFF`/`?WDP,ON`, and reboot boards, waiting out the periodic advert cycle each time — because the one command that forces immediate convergence, `?WDP,POLL`, appears nowhere in the help they are reading.

**Suggested fix.** Add POLL to the Enable/discovery block after WCB_Help.cpp:852, and DETAIL alongside the bare-number form at :848:

```cpp
Serial.println(F("  ?WDP,DETAIL,<n>      Detail for one neighbor (same as ?WDP,<n>)"));
...
Serial.println(F("  ?WDP,POLL                     Force convergence now: advertise + solicit"));
Serial.println(F("                                  (~1 s instead of the 60 s backstop;"));
Serial.println(F("                                   needs WDP + ETM enabled)"));
```

### F-434 · S4 · `Code/WCB/WCB_Help.h:12`

**WCB_Help.h's "COMPLETE COMMAND REFERENCE" is stamped v6.0 and is missing 13 shipped subsystems**

*Category:* `stale-doc`

`WCB_Help.h` lines 11-206 carry a block comment titled "WCB COMPLETE COMMAND REFERENCE", stamped at line 12:

```
// Last Updated: v6.0_251308FEB2026
```

The firmware is now 6.2.0 (WCB.ino:181, `String SoftwareVersion = "6.2.0_172045RAUG2026";`). I grepped the header for each subsystem token; every one of these returns zero hits, yet all are implemented and dispatched:

- `DFP` (WCB.ino:4923) — DFPlayer Mini config + the whole `;D` runtime family
- `HCR` (WCB.ino:4929) — Vocalizer config + `;H` runtime
- `WLED` (WCB.ino:4935) — WLED config + `;L` runtime
- `VAR` (WCB.ino:4949) — variables, plus `;V`/`;VP` and the entire `IF,` conditional system
- `WDP` (WCB.ino:4941) — discovery/neighbor table
- `LED` (WCB.ino:5128), `OTA` (:4826), `OTALOCAL` (:4812), `ALIAS` (:4839), `CONTROLLER` (:4861), `WCBCH` (:4790), `RTERM` (:5099), `IDENTIFY` (:5079), `TRACK` (:5018)

Grep counts for `;V`, `;D`, `;H`, `;L`, `IF,` in WCB_Help.h are all 0 — so five of the eleven runtime command-character verbs are absent from a document that calls itself complete. The header's "COMMAND CHARACTER COMMANDS" section (lines 145-163) lists only `;Sx`, `;Wx`, `;Ckey`, `;SEQkey`, `;M...`, `;Tms`.

The block also carries the two stale board-range lines noted separately (lines 21-22, `(1-9)`) and the broken `?SLCx` legacy form (line 188).

Because this is a comment block rather than executed code, it costs nothing at runtime — but it is the only single-page command inventory in the repo, it is the first thing a reader opening the help module sees, and its own title asserts completeness. A maintainer diffing accepted-vs-documented commands against it would conclude a third of the firmware's command surface does not exist.

**Failure scenario.** A contributor (or a future session) opens WCB_Help.h to check whether a `;L` verb already exists before adding one, sees a document titled COMPLETE that has no WLED section at all, and either duplicates the existing implementation under a new prefix or files the absence as a missing feature. Nothing in the block warns that it stopped tracking the code two minor versions ago.

**Suggested fix.** Either bring the block up to date against the 37 roots in `processLocalCommand()` and the 11 verbs in `processCommandCharcter()` (WCB.ino:5847-5875), or — since `printCommandHelp()` is now the maintained surface — replace the block with a short pointer:

```
// The authoritative command reference is printCommandHelp() in WCB_Help.cpp:
//   ??          top-level menu
//   ?COMMAND?   per-command detail
// Keeping a second copy here has drifted before; don't reintroduce one.
```

That removes a whole class of drift rather than resetting the clock on it. If the block is kept, fix lines 21-22 (1-9 → 1-20) and line 188 (?SLCx → ?SLCSx) at the same time.

### F-435 · S4 · `Code/WCB/WCB_Storage.h:78`

**SerialMonitorOutput.wcbNumber is documented as "1-9 = remote WCB" while the parser accepts 1..MAX_WCB_COUNT**

*Category:* `stale-comment`

The struct that carries a serial-mapping destination still documents the pre-MAX_WCB_COUNT board range:

```c
77: struct SerialMonitorOutput {
78:     uint8_t wcbNumber;    // 0 = local, 1-9 = remote WCB
79:     uint8_t serialPort;   // 0 = USB, 1-5 = Serial ports
80: };
```

The code that fills it accepts the full peer range — `Code/WCB/WCB_Storage.cpp:1717` rejects only `wcbNum > MAX_WCB_COUNT`, and MAX_WCB_COUNT is 20 (`Code/WCB/WCB_Storage.h:12`, mirrored at `Code/WCB/WCB.ino:120`). The forwarding path (`Code/WCB/WCB.ino:6261-6280`) and the backup emitter (`Code/WCB/WCB.ino:2821-2827`) both handle two-digit board numbers correctly. Only the comment is wrong.

The `serialPort` comment on the next line is accurate, which makes the stale one read as deliberate rather than left over. This is the same 9-board fossil family the repo already knows about (`Code/WCB/WCB.ino:881` "Includes 1-9 and broadcast"; `Code/WCB/WCB.ino:575` "[phase 0-2][board index 0-7]"), both already logged in docs/CODE_REVIEW_2026-08_wave1.md — this one is not. Note the header also contains a commented-out earlier version of the mapping struct at `:82-87` that duplicates the live one at `:88-94`, so a reader has two struct definitions and one wrong range comment to reconcile.

**Failure scenario.** A maintainer sizing or validating serial-mapping destinations reads WCB_Storage.h — the only place the field's domain is documented — concludes the destination range is 1-9, and either adds a `> 9` guard (silently excluding boards 10-20 from serial mappings, reintroducing the exact bug class that was fixed once in the relayed-terminal path) or leaves a 9-wide lookup table in a new consumer. No runtime failure today; the cost is a wrong constraint handed to the next change.

**Suggested fix.** Change the comment to `// 0 = local, 1-MAX_WCB_COUNT = remote WCB` (matching the phrasing already used at Code/WCB/WCB.ino:452 `// sized to max, indexed by (wcbNumber-1)`), and delete the commented-out duplicate struct at :82-87 so there is one definition of the shape.

### F-436 · S4 · `Code/WCB/WCB_WDP.cpp:1301`

**WDP advert stagger uses `% 16` over a 1..20 board domain — boards 17-20 always advertise in the same slot as boards 1-4**

*Category:* `board-count-cap`

Both WDP stagger computations take the board number modulo 16, but the board-number domain is 1..MAX_WCB_COUNT = 20 (`Code/WCB/WCB.ino:120`):

```c
1301:  wdpNextAdvertMs = millis() + WDP_PERIOD_MS + (unsigned long)((WCB_Number % 16) * 500);
```

```c
349:  unsigned long jitter = (unsigned long)((WCB_Number % 16) * 40);   // 0..600 ms stagger
```

The stated purpose is stated in the adjacent comments: "Per-board phase stagger so periodic backstops from boards that booted together don't all fire in the same tick" (`:1299-1300`) and "staggered by board number so a fleet doesn't answer in lockstep (a broadcast storm)" (`:344-345`). With a modulus of 16 that property does not hold across the supported range: WCB16 and WCB1 both land on offset 0 (16 % 16 == 0, and note 16 collides with 32 only nominally — the real collisions are 16/32 aside, WCB17->1, WCB18->2, WCB19->3, WCB20->4). Four of twenty boards share a slot with another board, and WCB16 shares slot 0 with nothing else but is itself offset 0 rather than 15.

The modulus is a leftover sized to a smaller fleet; nothing else in WDP uses 16 (the neighbor table, the peer arrays and every loop are `MAX_WCB_COUNT`, e.g. `WCB_WDP.cpp:50` `static WdpNeighbor wdpNeighbors[MAX_WCB_COUNT];`).

**Failure scenario.** A 20-board fleet is powered from one switch. WCB1 and WCB17 compute an identical 0 ms periodic offset and an identical 40 ms solicit jitter, so from the first `?WDP,POLL` onward they transmit their ~150-byte adverts in the same tick, every 60 s, for as long as the fleet is up — the exact lockstep the stagger exists to break. ESP-NOW/CSMA absorbs it, so the visible symptom is only an occasional missing neighbor row in `?WDP,DUMP` on a busy mesh; there is no correctness failure, but the mitigation is silently a no-op for 20% of the fleet.

**Suggested fix.** Use the real domain: `(WCB_Number % MAX_WCB_COUNT)` at both sites, or better `((WCB_Number - 1) % MAX_WCB_COUNT)` so WCB1 gets offset 0 and WCB20 gets the largest. Update the `// 0..600 ms stagger` comment at :349 to match whatever span results (with MAX_WCB_COUNT and a 40 ms step it becomes 0..760 ms).

### F-437 · S4 · `Code/WCB/WCB.ino:5853`

**;SEQ recall matches only the exact spellings "SEQ" and "seq" while its ;C alias is case-insensitive**

*Category:* `command-dispatch`

`processCommandCharcter` dispatches the recall verb with four literal tests:
```
5849:      if ((message.startsWith("s") || message.startsWith("S")) && (message.length() > 1 && message.charAt(1) != 'e' && message.charAt(1) != 'E')) {
5850:         processSerialMessage(message);
5853:     } else if (message.startsWith("c") || message.startsWith("C") || message.startsWith(String("SEQ")) || message.startsWith(String("seq"))) {
5854:         recallStoredCommand(message, sourceID);
```
The `;S` guard at :5849 correctly excludes anything with 'e'/'E' at index 1 so `;SEQ…` and `;seq…` can fall through — verified clean, since serial ports are digits 0-5. But :5853 then accepts only the all-upper and all-lower spellings. `;Seq wave`, `;sEQwave` and `;SEqwave` match none of `"c"`, `"C"`, `"SEQ"`, `"seq"`, are excluded from the serial branch by the 'e' guard, match no later branch (`m/p/a/d/h/l/v`), and fall to `Serial.println("Invalid Serial Command")` at :5873. `recallStoredCommand`'s own `isSeq` test at :6090 repeats the same two-spelling comparison.

By contrast the documented alias `;Ckey` works in either case (`startsWith("c") || startsWith("C")`), and every other runtime verb in this dispatcher is tested in both cases. `WCB_Help.cpp:761` documents the pair as interchangeable: `";Ckey  (or ;SEQkey)  Run sequence 'key' MESH-WIDE"`.

**Failure scenario.** A NaviCore/WCB_Client host or a user with autocapitalisation sends `;Seqwave`. The board prints "Invalid Serial Command" and the sequence does not run — on that board or anywhere in the mesh, since the fan-out at WCB.ino:6118-6121 is inside `recallStoredCommand` and is never reached. The identical `;Cwave` works, and `;SEQwave` works, so the failure looks intermittent and tied to the sender rather than to letter case.

**Suggested fix.** Test case-insensitively in both places: at WCB.ino:5853 use `message.equalsIgnoreCase(...)`-style logic, e.g. hoist `String mU = message; mU.toUpperCase();` at the top of `processCommandCharcter` and branch on `mU.startsWith("SEQ")`; apply the same at WCB.ino:6090 where `isSeq` is recomputed. The `;S`-branch 'e'/'E' guard at :5849 already covers every case variant and needs no change.

### F-438 · S4 · `docs/SEQUENCE_INVENTORY.md:48`

**SEQUENCE_INVENTORY.md's file:line citations are stale — 13 of them land on unrelated code**

*Category:* `docs-drift`

This page is written as a navigational map with `file:line` anchors (the convention CLAUDE.md:135-139 mandates), but the line numbers have drifted — WCB.ino references are off by 40-310 lines. Every one below was checked with `sed -n '<line>p'`:

| Doc line | Doc claims | Actual |
|---|---|---|
| :21 | `listStoredCommands()` (WCB_Storage.cpp:712) | blank; function is at WCB_Storage.cpp:714 |
| :34 | "No stored commands." (WCB_Storage.cpp:717) | `preferences.end();`; string is at WCB_Storage.cpp:720 |
| :46 | `?SEQ,SAVE` tokens in the config pull (WCB.ino:2855-2871) | Kyber/ETM `emit()` calls; the `emit("SEQ,SAVE,"...)` is at WCB.ino:2910 |
| :48 | `MGMT_MAX_CHUNKS` (WCB.ino:803) | blank; `#define MGMT_MAX_CHUNKS 16` is at WCB.ino:837 |
| :51 | "Config too large" under debugMGMT (WCB.ino:2959) | a `snprintf` for a checksum; the message is at WCB.ino:3003 |
| :76 | ~500 ms dirty re-advert (WCB_WDP.cpp:352) | a SOLICIT debug print; `wdpNextDirtyCheckMs = now + 500` is at WCB_WDP.cpp:374 |
| :80 | fixed 200-byte payload (WCB_WDP.cpp:303) | a `memcpy` inside the PORTLABEL loop; `uint8_t payload[200]` is at WCB_WDP.cpp:321 |
| :84 | labels dropped gracefully on overflow (WCB_WDP.cpp:276) | a PWMTARGET comment; the label-overflow comment is at WCB_WDP.cpp:295 |
| :97 | `espnow_struct_config_req`, 43 bytes (WCB.ino:783) | `} espnow_struct_message;`; config_req is at WCB.ino:797-802 |
| :117 | `espnow_struct_config_frag`, 230 bytes (WCB.ino:792) | `uint8_t totalChunks;` inside mgmt; config_frag is at WCB.ino:825-834 |
| :118 | `sendResultFrags()` (WCB.ino:3050) | blank; function is at WCB.ino:3095 |
| :119 | why 182 not 183 (WCB.ino:2950) | a user-variables comment; the 182/183 note is at WCB.ino:2999 |
| :123 | 43-vs-226 size dispatch (WCB.ino:3316) | a bare `}`; the size router is at WCB.ino:3627-3648 |
| :219 | `?SEQ,SAVE` delimiter branch (WCB.ino:2015-2048) | `?CS` handling; the SEQ,SAVE branch is at WCB.ino:2044-2060 |

The substantive technical content of the page is correct — I verified the 1.5 s SEQ_REQ dedup (Code/WCB/WCB.ino:3151-3160), the (requester, key) SEQVAL dedup (Code/WCB/WCB.ino:3196-3206), the 182-byte chunk stride and 16-chunk refusal (Code/WCB/WCB.ino:3096-3101), the explicit TOOBIG/NOTFOUND statuses (Code/WCB/WCB.ino:3211-3227) and the 59-byte struct (Code/WCB/WCB.ino:816-822). Only the anchors are wrong.

**Failure scenario.** A cold session follows docs/SEQUENCE_INVENTORY.md:48 to "WCB.ino:803" to check the 2912-char config-pull ceiling, lands on a blank line inside an unrelated struct comment, and either re-derives the constant from scratch or — worse — trusts the doc's arithmetic without confirming, which is exactly what CLAUDE.md:130-133 forbids.

**Suggested fix.** Re-resolve all 14 anchors against the current tree (the correct lines are listed above). Longer term, prefer symbol names over line numbers in prose ("`sendResultFrags()` in WCB.ino") and keep line numbers only where the exact line is load-bearing, since WCB.ino is 8000+ lines and every edit above a citation invalidates it.

### F-439 · S4 · `docs/VARIABLES_DESIGN.md:16`

**VARIABLES_DESIGN says the 101st variable errors; the store silently evicts a volatile variable instead**

*Category:* `docs-drift`

docs/VARIABLES_DESIGN.md:16: "**Cap:** 100 variables (`WCB_MAX_VARIABLES`). Set #101 → error."

`setVariableImpl()` reclaims a volatile slot before it will report an error:

  Code/WCB/WCB_Variables.cpp:137-146
    int idx = findVarSlot(name);
    if (idx < 0) {
      idx = findFreeSlot();
      if (idx < 0) idx = evictOneRamSlot();   // table full → recycle a RAM/telemetry slot so a
                                              // persistent var (or fresh telemetry) is never starved
      if (idx < 0) {
        Serial.printf("[VAR] table full (%d persistent vars) — cannot create '%s'\n", ...);
        return false;

and `evictOneRamSlot()` (Code/WCB/WCB_Variables.cpp:43-47) drops the FIRST slot in array order with `!vars[i].persist` — it does not distinguish a user's `;V` variable from Maestro get-telemetry, because both are stored volatile (`setVariableRAM`, Code/WCB/WCB_Variables.cpp:177-179, and the volatile default at Code/WCB/WCB_Variables.cpp:265-266). So set #101 succeeds and a previously-set volatile variable disappears with no message; the error the doc promises only fires when all 100 slots are persistent (which is what the log line at :143 actually says).

The doc also never mentions `setVariableRAM`/telemetry sharing this same 100-slot table (§8's touch-point table, docs/VARIABLES_DESIGN.md:174, lists only the ;V/;VP/?VAR surface).

**Failure scenario.** A board with heavy Maestro get-query telemetry plus ~90 user variables reaches 100 slots. The user sets a 101st with `;V,newflag,1`; it reports success, but an existing volatile flag (say `domeanimations`) is silently deleted. The next `IF,domeanimations=1` reads the undefined-default 0 (docs/VARIABLES_DESIGN.md:22) and the gated command never runs, with no error anywhere.

**Suggested fix.** Correct §1's Cap bullet: the table is shared with RAM-only telemetry vars, and on overflow the oldest-indexed VOLATILE entry is evicted silently; the hard error only occurs when all 100 slots are persistent. Document `setVariableRAM` in §8. Add a dated row to the page's Revision log (docs/VARIABLES_DESIGN.md:186).

### F-440 · S4 · `docs/WCB_OTA_TECHNICAL.md:170`

**OTA doc's "every other packet on the mesh" size roster is missing 59 (seqval_req); two source comments carry a differently-incomplete roster**

*Category:* `docs-drift`

docs/WCB_OTA_TECHNICAL.md:169-170 (§5, "Size-based dispatch"): "So the OTA struct sizes (`55`, `243`) must stay **unique** against every other packet on the mesh (`{43,204,226,230,249,252}`). Two `static_assert`s lock them. If you change a struct, re‑check uniqueness."

The roster is stale. `espnow_struct_seqval_req` is 59 bytes and is routed by size like every other family:
  Code/WCB/WCB.ino:816-822 (struct definition, `char key[16]`)
  Code/WCB/WCB.ino:3642 `if (len == sizeof(espnow_struct_seqval_req)) { ... }`
So the true set of non-OTA sizes today is {43, 59, 204, 226, 230, 249, 252}.

The same incompleteness appears in reverse in two other places, both of which omit 204 (`espnow_struct_remote_term`, Code/WCB/WCB_RemoteTerm.h:45 "Total size = 40+1+1+1+1+160 = 204 bytes"):
  Code/WCB/WCB.ino:813-814 — "59 bytes is deliberately distinct from every other packet size (43/55/226/230/243/249/252)"
  docs/SEQUENCE_INVENTORY.md:165 — "59 is deliberately distinct from every other packet size (43/55/226/230/243/249/252)"

No live collision exists: the two static_asserts together do cover the full matrix — Code/WCB/WCB.ino:3598-3613 pins ota_ctrl/ota_data against mgmt/config_req/config_frag/remote_term/etm/message, and Code/WCB/WCB.ino:3615-3624 pins seqval_req against all of those plus ota_ctrl and ota_data. The defect is that all three prose rosters are wrong, and the OTA doc is written specifically as the checklist a porter uses when adding or resizing a struct.

**Failure scenario.** A developer porting the OTA transport to NaviCore (the doc's stated purpose, docs/WCB_OTA_TECHNICAL.md:3) sizes a new packet at 59 bytes after checking it against the doc's {43,204,226,230,249,252} and finding no conflict. On the WCB side the static_assert catches it at compile time; on the NaviCore/WCB_Client side, where the doc is the only guidance, the packet routes into `handleSeqValReqPacket` or nowhere, "with no diagnostic at either end" (docs/SEQUENCE_INVENTORY.md:124-125).

**Suggested fix.** Correct docs/WCB_OTA_TECHNICAL.md:170 to `{43,59,204,226,230,249,252}`, and fix the two 204-omitting rosters at Code/WCB/WCB.ino:813-814 and docs/SEQUENCE_INVENTORY.md:165 to `43/55/204/226/230/243/249/252`. Better: point all three at the static_assert blocks (Code/WCB/WCB.ino:3598-3624) as the single authority instead of restating a list that must be hand-maintained in three places.

### F-441 · S4 · `docs/WDP_DESIGN.md:211`

**WDP_DESIGN's `?WDP,DUMP` format block omits the `EN=` field and the whole `[WDPSEQ:]` record the same doc says it added**

*Category:* `docs-drift`

docs/WDP_DESIGN.md:205-213 presents the DUMP wire format. Two lines are wrong against `printWdpDump()`:

1. docs/WDP_DESIGN.md:211 — `[WDPCFG:AUTOJOIN=..,PEERS=..]`
   Code/WCB/WCB_WDP.cpp:1189-1190:
     Serial.printf("[WDPCFG:EN=%d,AUTOJOIN=%d,PEERS=%d]\n",
                   wdpEnabled ? 1 : 0, wdpAutoJoin ? 1 : 0, activePeerCount());
   The `EN=` field is emitted first and is missing from the doc. The source comment at Code/WCB/WCB_WDP.cpp:1187-1188 explains why it exists ("EN lets the Wizard distinguish 'WDP disabled here' from 'mesh just empty'"), and the Wizard parses it as optional (Wizard/app.js:12325, `/^\[WDPCFG:(?:EN=(\d),)?AUTOJOIN=(\d),PEERS=(\d+)\]$/`).

2. The block lists WDP / WDPIF / WDPX / WDPPWM / WDPCFG / WDP:END but not `[WDPSEQ:N=..,HASH=..]`, which the firmware emits for the self row unconditionally (Code/WCB/WCB_WDP.cpp:1137) and for each neighbour with a non-zero hash (Code/WCB/WCB_WDP.cpp:1177-1178). The same document's own Revision log says it was added — docs/WDP_DESIGN.md:328: "new `[WDPSEQ:N=,HASH=]` record in `?WDP,DUMP`" — so §7 and §12 of the same page contradict each other, and docs/SEQUENCE_INVENTORY.md:281-283 documents the record too.

**Failure scenario.** Someone writing a second consumer of `?WDP,DUMP` (a script, or NaviCore's mesh view) codes the WDPCFG regex from §7 as `^\[WDPCFG:AUTOJOIN=` — it never matches a current board, so auto-join state and the live peer count silently read as unknown; and they have no parser for the interleaved [WDPSEQ:] lines, which then fall through as unrecognised output.

**Suggested fix.** Update the §7 code block to `[WDPCFG:EN=..,AUTOJOIN=..,PEERS=..]` and insert the `[WDPSEQ:N=..,HASH=..]` record with a note on its position (after WDPX, before WDPPWM) and its omission semantics (absent = neighbour advertised no hash — never `HASH=00000000`, per Code/WCB/WCB_WDP.cpp:1174-1178). Add a Revision-log row.

### F-442 · S4 · `tests/wdp_wire_test.cpp:19`

**Every firmware source anchor in the test file is stale — all seven point at the wrong line, one at the wrong file**

*Category:* `stale-comment`

The test's design contract is stated in its own header, `tests/wdp_wire_test.cpp:16-20`: "**If you change the wire format or any rule in WCB_WDP.cpp, update the mirror below and these expectations in the same commit** — the value is catching accidental drift... Firmware source anchors are cited next to each mirrored piece." Since the test is a re-implementation by deliberate design (tests:11-20, and I confirmed the stated reason: WCB_WDP.cpp includes Arduino.h and Preferences.h and references maestroConfigs/wledConfigs/pwmMappings/Serial, so it genuinely cannot compile off-target), those anchors are the *only* mechanism keeping the mirror honest. Every one of them is wrong:

| test line | cited | actual |
|---|---|---|
| :43 | `WCB_WDP.cpp:113` (baud table) | `WCB_WDP.cpp:129` |
| :49 | `WCB_WDP.cpp:200` (putTLV) | `WCB_WDP.cpp:216` |
| :75 | `WCB_WDP.cpp:431+` (wdpOnAdvertReceived) | `WCB_WDP.cpp:487` |
| :83 | `WCB_WDP.cpp §8` (MAC binding) | `WCB.ino:3756` — different file (see separate finding) |
| :87 | `WCB_WDP.cpp:434+` (SOLICIT block) | `WCB_WDP.cpp:496-506` |
| :133 | `WCB_WDP.cpp:381` (wdpCapOwner) | `WCB_WDP.cpp:437` |
| :310 | `WCB_WDP.cpp:573` (advert-count gate) | `WCB_WDP.cpp:654-656` |

The drift ranges from 16 to 81 lines. `WCB_WDP.cpp:381` and `:431` land in the middle of `wdpMeshHasCap`/`wdpEvaluateMaestroRemote`, and `:573` lands inside the WLED_CFG decode case — so following an anchor puts the reader on plausible-looking but wrong code rather than obviously nothing.

**Failure scenario.** A maintainer follows the test's own instruction to diff the mirror against the firmware after a WDP change. Every jump lands on unrelated code; two of them (`:381`, `:573`) land inside real functions that look adjacent enough to accept. The reviewer either gives up on the comparison or concludes the mirror matches something it does not — which is how the PORTLABEL clamp and the FLAGS case (separate findings) came to be missing without anyone noticing.

**Suggested fix.** Re-anchor all seven to current lines, and fix :83 to name `WCB.ino:3756` / `docs/WDP_DESIGN.md §8`. Because line numbers rot on every edit, prefer anchoring by symbol — `wdpOnAdvertReceived()`, `wdpCapOwner()`, `WDP_BAUD_TABLE`, `putTLV()` — which survives refactoring and is greppable.

### F-443 · S4 · `tests/wdp_wire_test.cpp:41`

**Three decoded TLVs and six of nine capability bits are never asserted by any check**

*Category:* `test-coverage`

Enumerating the suite against what the decoder handles leaves a defined set of holes.

**Decoded in the mirror but never asserted.** `T_DEVTYPE` (tests:112) is the TLV that marks a sender as a client device (`nb.isClient=true`), which drives the controller auto-adopt at WCB_WDP.cpp:625-628 and excludes the sender from capability election at WCB_WDP.cpp:441. No test ever builds a DEVTYPE packet — `test_cap_election` sets the flag by hand instead (`tbl[0].isClient=true;`, tests:304), so the decode side of `isClient` is unexercised. Same for `T_HWREV` (tests:113, clamp 15, firmware WCB_WDP.cpp:552) and `T_CAPTAGS` (tests:114, clamp 48, firmware :555) — decoded, never checked, and both feed the `?WDP,DUMP` line the Wizard regex-parses at `Wizard/app.js:12303`.

**Capability bits.** `tests:41` mirrors three of nine: `CAP_HCR = 0x0001, CAP_MP3 = 0x0002, CAP_WLED = 0x0004`. Missing are `WDP_CAP_KYBER_LOCAL 0x0008`, `MAESTRO_REM 0x0010`, `PWM 0x0020`, `CONTROLLER 0x0040`, `MAESTRO_LOC 0x0080`, `DFPLAYER 0x0100` (WCB_WDP.h:40-48). These are live wire values consumed cross-repo — 0x0008 gates `wdpEvaluateMaestroRemote` (WCB_WDP.cpp:477), and 0x0001/0x0002/0x0100 drive the persisted `hcrAutoAddRemote`/`mp3AutoAddRemote`/`dfpAutoAddRemote` calls at WCB_WDP.cpp:733-735.

**Also unexercised**, for completeness: `wdpScrub` (WCB_WDP.cpp:400-403), the `,`/`]`/control-char sanitiser standing between wire strings and the Wizard's dump parser; and the `0xFF` → "unknown baud" → skip semantic (WCB_WDP.cpp:138 with :708-709), which is what makes a legacy id-only MAESTRO advert safe to ignore.

**Failure scenario.** A capability bit is reassigned or a new one is inserted at 0x0008 during a feature merge, desynchronising this firmware from WCB_Client and NaviCore. Nothing fails: the three mirrored bits still round-trip, so the suite passes. In the field, boards mis-elect capability owners — `wdpCapOwner` (WCB_WDP.cpp:437) hands `;H`/`;A` to a board that does not host the device, and `wdpEvaluateMaestroRemote` flips Maestro-remote on from a bit that now means something else, writing the wrong value to NVS via `storeKyberSettings("remote")` (WCB_WDP.cpp:485).

**Suggested fix.** Mirror all nine CAP_* values from WCB_WDP.h:40-48 and assert the full 16-bit bitmap survives a CAPFLAGS round-trip (one CHECK covers every bit). Add a DEVTYPE packet to the round-trip test asserting `isClient==true` and that alias is populated from it, plus HWREV/CAPTAGS with over-length values to pin their 15/48 clamps. A `wdpScrub` mirror test (alias containing `,` and `]` → `_`) is three lines and guards the Wizard's dump parse.


---

## Sweep coverage notes (what each sweep enumerated / verified clean)

- ## What I did Built and ran the test exactly as CLAUDE.md:100 documents, then diffed the test's mirrored encoder/decoder/election against the shipping implementation line by line, then audited the workflow that runs it. ## Documented workflow — VERIFIED WORKING `g++ -std=c++11 -Wall -Wextra -O2 tests/wdp_wire_test.cpp -o /tmp/wdp_wire_test && /tmp/wdp_wire_test` - g++ is present (`C:/Users/ghulette/tools/w64devkit/bin/g++`, GCC 16.1.0). No finding about the documented workflow. - **Build: exit 0, zero warnings** under `-Wall -Wextra`. Nothing to report. - **Run: exit 0**, all nine sections pass: roundtrip, unknown-TLV/truncation, seqhash, MAESTRO_CFG supersede, PWMTARGET self-only, SOLICIT, MAC binding, cap election, auto-join. - Sanitiser build (`-fsanitize=address,undefined`) is unavailable in this w64devkit toolchain (`cannot find -lasan`) — noted, not a finding; the CI runner has them if wanted. - **Failure path verified empirically**, not assumed: I mutated one CHECK in a scratch copy; the harness printed `1 CHECK(s) FAILED` and **exited 1**. So `run: ./wdp_wire_test` at `.github/workflows/wdp-tests.yml:30` does fail the job. The workflow *does* run the test and *does* fail on failure — the question in the assignment is answered affirmatively; the defects are in what it is wired to watch (findings 1, 4). ## Mirror vs. firmware — full side-by-side, what is CLEAN Do not redo these; I checked each byte-for-byte against `Code/WCB/WCB_WDP.cpp`. - **All 17 TLV type constants** (tests:35-39) match the `#define`s at WCB_WDP.cpp:67-97 exactly: END 0x00, ALIAS 0x01, FWVER 0x03, HWVER 0x04, CAPFLAGS 0x05, MAESTRO 0x06, PORTLABEL 0x09, CTRLID 0x0A, DEVTYPE 0x0B, HWREV 0x0C, CAPTAGS 0x0D, MAESTRO_CFG 0x0E, WLED_CFG 0x0F, PWMTARGET 0x10, SOLICIT 0x11, FLAGS 0x12, SEQHASH 0x13. Burned draft ids 0x02/0x07/0x08 correctly absent. - **BAUD_TABLE identical**, 14 entries in the same order (tests:44 vs WCB_WDP.cpp:129-132) — the on-wire codes match. I additionally verified the comment at WCB_WDP.cpp:127-128 claiming sync with `configureMaestro()`: the whitelist at **WCB_Maestro.cpp:610-614** is the same 14 values. **The comment is accurate.** WLED's narrower whitelist (`wledBaudValid`, WCB_WLED.cpp:32-34: 9600/19200/38400/57600/115200) is a strict subset of the table, so every valid WLED baud has a wire code. Clean. - **`putTLV` mirrors exactly** (tests:50-57 vs WCB_WDP.cpp:216-224) — clamp order, the `o+2+len>max` skip-and-return-unchanged, all identical. - **Decode loop bounds identical**: `while(o+2<=200)` and `if(o+2+len>200) break` (tests:102/105 vs :517/522). No OOB read possible in either; `structCommand` is `char[200]` (WCB.ino:209). - **12 of 14 TLV cases match exactly** including every clamp — see finding 2 for the enumeration. The two divergences are PORTLABEL (missing clamp) and FLAGS (missing entirely). - **`capOwner` mirror is faithful** to `wdpCapOwner` (tests:136-147 vs WCB_WDP.cpp:437-447): self-inclusion, `!valid || isClient` skip, `wcbPeerOnline` gate, `owner==0 || nb.wcbNumber < owner`. Correct. - **Election ties are impossible by construction** — `wdpNeighbors` is indexed `senderWCB-1` (WCB_WDP.cpp:508) so two rows cannot share a number. The assignment listed ties as a possible gap; it is not one. No finding. ## Firmware VERIFIED CLEAN (checked while building the comparison) - **No buffer overrun in the real decoder.** Every write is bounded against its declared size in WCB_WDP.h:52-92: ALIAS 24/[25], FWVER 27/[28], HWREV 15/[16], CAPTAGS 48/[49], MAESTRO 9/[9], MAESTRO_CFG & WLED_CFG 9 recs/[9], PORTLABEL 24/[25] with `[L]='\0'` at index ≤24, PWMTARGET capped at 5. SEQHASH len≥4. - **WDP has exactly one ingress.** `enqueueWdpPacket` defined WCB.ino:986, called only at WCB.ino:3785 — downstream of password (:3720), sender-range (:3739-3745) and MAC-binding (:3756). `PACKET_TYPE_WDP` appears only at :222/:978/:3784. No bypass path. - **`wcbPeerAdvertCount` lifecycle is correct** — I specifically checked for a re-join-after-forget bug: it is reset at WCB.ino:1078 (eviction, commented "re-vet if it returns") and WCB.ino:6991 (forget). A forgotten peer is properly re-vetted. Not a bug. - **`wdpScrub` is adequate for the dump format.** It strips `,`, `]` and <0x20 (WCB_WDP.cpp:400-403); the Wizard regex at `Wizard/app.js:12303` uses `[^,]*` fields anchored `^\[WDP:N=` … `\]$`, so a stray `[` in an alias cannot break the parse. No finding. - **`wdpBuildPayload` overflow handling is safe** — `putTLV` returns `o` unchanged when a TLV does not fit (WCB_WDP.cpp:219), so an over-long label set drops labels gracefully; the terminator is conditional (`if (o < max)`, :307) but `wdpBroadcast` memsets the struct first (WCB.ino:970), so the tail is zero = END either way. ## Cross-repo spot-checks (WCBClient, outside this repo — noted, not reported as findings) - `WCB_Client.cpp:2050-2059` **clamps PORTLABEL identically** (`if (L > 24) L = 24`) — both real implementations are safe; only the test mirror is not. - `WCB_Client.cpp:2044-2046` decodes FLAGS/TEMPORARY the same way; `:1957` emits it. - `WCB_Client.cpp:1935` then `:1941` emit MAESTRO before MAESTRO_CFG — **same order as WCB_WDP.cpp:257/:260**, so the ordering fragility in finding 6 is latent, not live. ## Not reported, and why - **The test being a re-implementation rather than a link against the firmware is deliberate and correctly justified** (tests:11-20). I verified the stated reason is factually true — WCB_WDP.cpp includes `Arduino.h`/`Preferences.h` and depends on `maestroConfigs`, `wledConfigs`, `pwmMappings`, `Serial`, `wdpBroadcast`, `addActivePeer`. Per the review rules I did not report it; I reported the *drift* (findings 2, 3) and the *broken anchor mechanism* (finding 7) that the header itself names as the safeguard. - **wdp-tests.yml being separate from build.yml so it "can never block a firmware push"** is documented as deliberate at wdp-tests.yml:3-5. Not reported. (The path-filter findings are about which *files* it watches, which is a different question.) - `docs/CODE_REVIEW_2026-08.md:595` claims the test "builds warning-free under `-Wall -Wextra`; all 9 spec checks pass" — **I reproduced this exactly.** The doc is accurate. - `WCB.ino:3721-3722` carries a mildly stale comment ("already enforced at the top of espNowReceiveCallback (line ~2600)" — the group-octet check is actually at WCB.ino:3593). Too minor to spend a slot on; recording it here.

- SWEEP: docs/ vs code drift. I enumerated every concrete factual claim in all 7 engineering pages plus CLAUDE.md and Code/WCB/.claude/launch.json, then checked each against the current OTA-branch source. Firmware version in tree: `String SoftwareVersion = "6.2.0_172045RAUG2026"` (Code/WCB/WCB.ino:181). FILES ENUMERATED (docs/): SEQUENCE_INVENTORY.md (353 L), VARIABLES_DESIGN.md (188), WCB_KYBER_PASSTHROUGH_PARAMS.md (88), WCB_OTA_TECHNICAL.md (300), WDP_DESIGN.md (328), WDP_DEVICE_ANNOUNCE.md (182), WLED_INTEGRATION.md (156). Plus CLAUDE.md (153) and Code/WCB/.claude/launch.json. I deliberately did NOT audit docs/CODE_REVIEW_2026-08*.md (4 files, ~7300 lines) — those are review transcripts, not specifications, and are expected to be point-in-time. === VERIFIED GOOD (do not re-check) === WCB_OTA_TECHNICAL.md — every constant confirmed. `OTA_TIMEOUT_MS = 30000` (WCB_OTA.cpp:38); `OTA_LOCAL_MAX_CHUNK = 1024` + static scratch buffer (WCB_OTA.cpp:40, :224); `OTA_ESPNOW_PAYLOAD = 192` (WCB_OTA.h:75); packet types 20/21/22/23/24 (WCB_OTA.h:69-73); `OTA_ST_OK=0`/`OTA_ST_ERR=1` (WCB_OTA.h:78-79); struct sizes 55 and 243 pinned by static_assert (WCB_OTA.cpp:20-21); both struct field layouts match the doc byte-for-byte (WCB_OTA.h:82-104); BAUD whitelist 115200/230400/460800/921600/1000000 (WCB_OTA.cpp:250-251) with the ACK-at-old-baud-then-`updateBaudRate` handshake (WCB_OTA.cpp:255-258) and restore-to-default on abort (WCB_OTA.cpp:56); `xQueueCreate(12, sizeof(OtaPktSlot))` and drop-if-full (WCB_OTA.cpp:546, :552); enqueue length guard (WCB_OTA.cpp:544). The §5 defer-out-of-callback rule matches WCB.ino:3661-3670. §1's min_spiffs partition rationale matches Code/bin/build.sh:75-85. ONLY §5's packet-size roster is wrong (finding 6). WDP_DESIGN.md — TLV registry verified against WCB_WDP.cpp:66-99: END 0x00, ALIAS 0x01, FWVER 0x03, HWVER 0x04, CAPFLAGS 0x05, MAESTRO 0x06, PORTLABEL 0x09, CTRLID 0x0A, DEVTYPE 0x0B, HWREV 0x0C, CAPTAGS 0x0D, MAESTRO_CFG 0x0E, WLED_CFG 0x0F, PWMTARGET 0x10, SOLICIT 0x11, FLAGS 0x12 (bit0 TEMPORARY), SEQHASH 0x13 — all correct, and the "0x02/0x07/0x08 burned" note is honoured. CAPFLAGS bit values all match WCB_WDP.h:40-48 including 0x0100 DFPLAYER. String caps match the struct: alias 24 (advert clamp WCB_WDP.cpp:232, `alias[25]`), FWVER 27 (WCB_WDP.cpp:237, `fwVer[28]`), HWREV 15 (`hwRev[16]`), CAPTAGS 48 (`capTags[49]`). Cadence: 3× boot burst at +1600 ms (WCB_WDP.cpp:1297-1298), 60 s backstop with `WCB_Number`-based phase (WCB_WDP.cpp:52, :1301), SOLICIT jitter `(WCB_Number % 16) * 40` = 0..600 ms (WCB_WDP.cpp:349). `WDP_TTL_MS = 180000` (WCB_WDP.cpp:53). Temporary-peer eviction `TEMPORARY_PEER_TTL_MS = 50000` — doc's "~50 s" is right (WCB.ino:475, sweep at :1073). Auto-join requires ≥2 adverts (WCB_WDP.cpp:654-656). Scrub set `,`/`]`/<0x20 → `_` (WCB_WDP.cpp:400-403). learned_peers NVS namespace + 20-bit mask (WCB.ino:7046-7047; namespace is 13 chars, under the 15-char NVS limit). `?WDP` subcommand table (§7) all present and correct in `processWdpCommand` (WCB_WDP.cpp:1200-1275) including POLL/ADD/FORGET/CLEAR/AUTOJOIN query-vs-set. `?WDP,STATUS` output string matches (WCB_WDP.cpp:1196-1197). `?PEERSLIVE` exists (WCB.ino:2795, :4803) and the Wizard parses it (Wizard/parser.js:504). §9 capability routing verified: `routeStoredOrCap` (WCB.ino:5827-5845) wired to `;A`/`;D`/`;H` at WCB.ino:5860/:5863/:5866 with pinned-host-offline failover; `;L`/`;M` correctly NOT elected. §9 PWM self-heal verified: `addPWMOutputPort(prt, senderWCB)` tags the source (WCB_WDP.cpp:751, WCB_PWM.cpp:791-813) and `reconcileWdpAutoPWMOutputs` clears stale ones (WCB_WDP.cpp:761). §6 "learned peer not counted for broadcast completion until it ACKs" verified at WCB.ino:1212, :1234. Wrong: §7 DUMP block (finding 5) and §9 "first-host-wins" (finding 2). SEQUENCE_INVENTORY.md — all substantive claims correct. SEQ_REQ dedup 1500 ms on requester (WCB.ino:3151-3160); SEQVAL dedup 1500 ms on (requester, key) (WCB.ino:3196-3206); `chunkStride = CONFIG_PAYLOAD_SIZE - 1` = 182 with `MGMT_MAX_CHUNKS`=16 refusal (WCB.ino:3096-3101, WCB.ino:252, :837); explicit TOOBIG/NOTFOUND (WCB.ino:3211-3227); 59-byte keyed struct with `key[16]` (WCB.ino:816-822) and its size-dispatch (WCB.ino:3642); `?SEQ,NAMES`/`?SEQ,GET`/`?MGMT,SEQGET` all present (WCB.ino:4980, :4964, :2552, :3478); SEQHASH always advertised, before PORTLABEL (WCB_WDP.cpp:283-292 vs :294-306); dump omits `[WDPSEQ:]` for hash==0 (WCB_WDP.cpp:1177). Only the anchors are stale (finding 7). VARIABLES_DESIGN.md — verified: `WCB_MAX_VARIABLES=100`, `WCB_VAR_NAME_MAX=15` (WCB_Variables.h:27-28); namespace `wcb_vars`, single "blob" key (WCB_Variables.cpp:85, :101, :77-91); name charset `[A-Za-z0-9_]`, 1-15, case-sensitive (WCB_Variables.cpp:64-73, :31-35); undefined reads 0 (WCB_Variables.cpp:184-187); `;V` vs `;VP` comma-required parse and the never-demote rule (WCB_Variables.cpp:210-222, :151-155, :262-266); all six IF operators `= != < > <= >=` (WCB_Variables.cpp:341-367); `?VAR,LIST` shows [persistent]/[volatile] (WCB_Variables.cpp:279); `?VAR,SET/GET/CLEAR,<name>/CLEAR,ALL` all present (WCB_Variables.cpp:290-332); the single shared `ifGateConsumeToken()` gate (WCB_Variables.cpp:401+); bare-IF drop in `handleSingleCommand` (WCB.ino:4356-4363). Only the cap claim is wrong (finding 4). WDP_DEVICE_ANNOUNCE.md — CLEAN. `@WDP1` marker check, `{` scan, required `type`, optional `fw`/`hw`/`caps` array flattened to space-separated, per-port record, scrub, 90 s TTL (`WDP_DA_TTL_MS`), `?WDP,DA` print, and label fallback to detected type when unlabeled — all match WCB_WDP.cpp:790-885 and WCB_WDP.cpp:295-306. Field sizes match `WdpDaDevice` (WCB_WDP.h:133-140). No findings. CLAUDE.md — rule 5 (Maestro) citations still accurate: WCB_Maestro.cpp:100-107 is the dedup comment, :108/:304/:414 are the fan-out loops, `MAX_MAESTROS_PER_WCB 9` at WCB_Maestro.h:14. Rule 4's WCB_WDP.h:20 anchor is correct. `MAX_WCB_COUNT 20` at WCB.ino:120. The `wizard-static` / port 8777 launch config claim (CLAUDE.md:113) is CORRECT — it is in the repo-root .claude/launch.json:5,14 (Code/WCB/.claude/launch.json is a different config, `wizroot` on 8778 — not what CLAUDE.md refers to). build.sh FQBNs and the min_spiffs/3.3.4 pin match Code/bin/build.sh:88-107 and :14. Only rules 2/3 line anchors are stale (finding 8). === NOT COVERED BY THIS SWEEP === CI workflow contents (.github/workflows/build.yml path filters, version stamping, release publication) — I read only build.sh, not build.yml; another sweep owns that. Wizard JS correctness beyond the two dump-parsing regexes I checked. tests/wdp_wire_test.cpp. NVS key-length audit across modules. The four docs/CODE_REVIEW_2026-08*.md transcripts.

- HELP / DOCUMENTATION COVERAGE SWEEP — full enumeration, both directions. === (A) COMMANDS THE FIRMWARE ACCEPTS === --- A1. New-format `?` roots — all 37, from `processLocalCommand()` (WCB.ino:4394-5355), enumerated by grepping every `rootUpper == "..."`. Entry point: handleSingleCommand WCB.ino:4380-4381 strips the LocalFunctionIdentifier. --- DEBUG 4423 | MAP 4473 | BAUD 4555 | LABEL 4570 | BCAST 4603 | KYBER 4651 | ETM 4664 | MAC 4726 | EPASS 4750 | WCB 4765 | WCBQ 4776 | WCBCH 4790 | PEERSLIVE 4803 | OTALOCAL 4812 | OTA 4826 | ALIAS 4839 | CONTROLLER|SPECIAL 4861 | MAESTRO 4897 | MP3 4917 | DFP 4923 | HCR 4929 | WLED 4935 | WDP 4941 | VAR 4949 | SEQ 4955 | STATS 5003 | TRACK 5018 | DELIM 5034 | FUNCCHAR 5045 | CMDCHAR 5057 | ERASE 5069 | IDENTIFY 5079 | MGMT 5090 | RTERM 5099 | HW 5121 | LED 5128 Pre-dispatch special cases: bare `?`/`??`/`HELP` -> menu (4396); trailing `?` -> topic (4401); `WCB_WEBTOOL_CONFIG_PULL` (4374, fixed string, funcChar-independent). --- A2. Legacy `?` forms — from the else-if chain WCB.ino:5145-5348 --- Exact-match: don doff reboot config backup version wcb_erase reset_broadcast stats statsreset cclear etmchar etmon etmoff maestro_list maestro_default maestro_remote kyber_local kyber_remote kyber_clear kyber_list Prefix-match: dmon/dmoff dkon/dkoff dpwmon/dpwmoff detmon/detmoff dhcron/dhcroff lf cc cs ce m2 m3 wcbq wcb epass maestro_clear maestro hw smclear smlist smr sm pms pclear plist prs pos po px sls slc sbi sbo track_all_on track_all_off track_status etmcharcount etmchardelay etmtimeout etmboot etmhb etmmiss bauds s<x> Two-char: `?D<x>` delimiter (length==2 guard, 5203). --- A3. Runtime `;` verbs — 11, from `processCommandCharcter()` (WCB.ino:5847-5875) --- ;S<port> 5849 (guarded charAt(1)!='e'/'E' so ;SEQ isn't stolen) | ;W 5851 | ;C / ;SEQ 5853 | ;M 5855 | ;P 5857 | ;A 5859 | ;D 5862 | ;H 5865 | ;L 5868 | ;V / ;VP 5870 Plus ;T (timer) — not in this dispatcher; detected by `isTimerCommand()` (command_timer.cpp:48) upstream. Plus `IF,...` gating, resolved in the chain splitters (WCB.ino:4361-4367). --- A4. Sub-verbs verified against their parsers --- ?SEQ: SAVE GET NAMES CLEAR,<key> CLEAR,ALL LIST (4955-4999) | ?STATS: (bare) RESET RPT (5003-5016) | ?WDP: ""|LIST STATUS DUMP DA POLL ON OFF AUTOJOIN[,ON|OFF] ADD,<id> FORGET,<id> CLEAR DETAIL,<n> <n> (WCB_WDP.cpp:1205-1272) | ?VAR: LIST SET GET CLEAR (WCB_Variables.cpp:290-336) | ?MP3: LIST CLEAR REMOTE ONERR S<p>:<b>:V<v> (WCB_MP3.cpp:195-302) | ?DFP: LIST CLEAR REMOTE ONERR S<p>[:b][:Vv] (WCB_DFP.cpp:156-234) | ?HCR: LIST CLEAR STATUS REFRESH REMOTE POLL GET PORT (WCB_HCR.cpp:528-640) | ?WLED: LIST STATUS CLEAR CLEAR,<id> PORT,<legacy> <id>:W<w>S<p>:<b> (WCB_WLED.cpp:233-268) | ?MAESTRO: LIST CLEAR,ALL CLEAR,<x> REMOTE M<id>:W<w>S<p>:<b> (WCB.ino:4897) | ?MGMT: PULL STATS SEQ SEQGET ETM,CHAR FRAG (WCB.ino:2540-2566) | ?DEBUG: ON OFF MAESTRO|KYBER PWM ETM HCR MGMT RAW (WCB.ino:4424-4466) | ?ETM: ON OFF TIMEOUT HB MISS BOOT COUNT DELAY CHAR CHKSM (WCB.ino:4671-4719) === (B) COMMANDS THE HELP SYSTEM DOCUMENTS === --- B1. `printCommandHelp()` topics — all 26, WCB_Help.cpp --- DEBUG 16 | MAP 51 | BAUD 107 | LABEL 135 | BCAST 163 | KYBER 199 | ETM 244 | MAC 308 | EPASS 335 | WCB 359 | WCBQ 382 | MAESTRO 406 | MP3 471 | DFP|DFPLAYER 534 | HCR 596 | WLED 658 | VAR 694 | SEQ 743 | STATS 793 | WDP 834 | DELIM 865 | FUNCCHAR 886 | CMDCHAR 908 | ERASE 936 | LED 964 | HW 987 Fall-through else 1015 -> top-level menu (1016-1080). --- B2. `WCB_Help.h` block comment, lines 11-206 --- "COMPLETE COMMAND REFERENCE", stamped v6.0 (line 12) vs shipping 6.2.0 (WCB.ino:181). === (C) THE DIFF === ACCEPTED BUT NO TOPIC (10): WCBCH, PEERSLIVE, OTALOCAL, OTA, ALIAS, CONTROLLER/SPECIAL, TRACK, IDENTIFY, MGMT, RTERM. Grep counts in WCB_Help.cpp: WCBCH 0, ALIAS 0, CONTROLLER 0, SPECIAL 0, TRACK 0, IDENTIFY 0, RTERM 0, OTA 0; PEERSLIVE 1 (passing mention, :862); MGMT 2 (examples inside ?SEQ?). -> Finding 5. Sub-verb gaps: ?MP3,REMOTE + ?HCR,REMOTE (F7); ?DEBUG MAESTRO/HCR/MGMT/RAW + legacy ?DHCRON/OFF (F9); ?WDP POLL/DETAIL (F12); ?WLED id-form + CLEAR,<id> + ;L<id> (F1). Runtime `;P` documented only in ?CMDCHAR? and wrongly (F2); `;T` has no topic (in WCB_Help.h only, absent from live menu). DOCUMENTED BUT NOT IMPLEMENTED: `?MAESTRO,ENABLE` (F3) — only true instance. I machine-diffed every `?TOKEN` in WCB_Help.cpp against the 37 roots; all other residue is legacy forms, each traced to a live handler in the A2 chain (incl. ?MAESTRO_REMOTE 5223, ?MAESTRO_DEFAULT 5222, ?PMSSx 5253, ?SMSxR 5251, ?LF? 5182, ?D^ 5203, ?CC; 5187). SYNTAX MISMATCHES: `;Px,width` vs `;Px<width>` (F2); `?WCB` 1-9 vs 1-20 (F4); `?SLC1` vs `?SLCS1` (F8); `;PP100` invalid (F11). === VERIFIED CLEAN — do not re-check === - ?BAUD valid-rate list (Help:117-118) == updateBaudRate() (WCB_Storage.cpp:155-157). Exact 13-value match. - ?HW version list (Help:993-998) == saveHWversion() (WCB_Storage.cpp:74-75). Exact: 1/21/23/24/31/32. Validate-before-write, comment at :70-73 accurate. - ?STATS,RPT 8-field order (Help:829) == storeReportedStats() (WCB.ino:1751-1782). from,sent,ackd,retries,failed,unguaranteed,bcast,recv; "short report dropped" claim true (found<8 at :1770); strtoul not toInt, comment accurate. - ?VAR entire topic (Help:694-742) == WCB_Variables.cpp. 1-15 names (WCB_VAR_NAME_MAX, .h:28 + :65); 100 cap (.h:27); 6 IF operators (:341-352); ;V volatile / ;VP persistent (:212-223); "?VAR,SET = PERSISTENT" == setVariable->setVariableImpl(...,true) (:171-173); no-demote rule (:150-155). - ?SEQ topic == WCB.ino:4955-4999 + recallStoredCommand (:6085-6126). SAVE/LIST/GET/NAMES/CLEAR all present; ",L"/",LOCAL" suffix (:6095-6107); mesh fan-out + nested-stays-local (:6118-6122); ?MGMT,SEQ,3 and ?MGMT,SEQGET,3,wave both parse (WCB.ino:2551,:2555). - ?MAESTRO topic verbs == WcbCmd. setTarget/setSpeed/setAccel/goHome/stopScript/sub/getPosition/getMovingState/getErrors — stopScript confirmed real at WcbCmd/src/WcbMaestro.cpp:159 (0xAA d 0x24). get* -> RAM vars m<dev>pos<ch>/moving/err (WCB_Maestro.cpp:380-387). - ?DFP topic == configureDFP (WCB_DFP.cpp:144-234) incl. optional-field forms ?DFP,S2 / S2:9600:V20 / S3:V25 (mid-field baud-or-volume logic :225-231); ;D verb list == WcbCmd/src/WcbDfPlayer.cpp (RANDOM :101, LOOPALL :130, LOOPFOLDER :136, MP3FOLDER :87, DEVICE :158). - ;A verb list == WcbCmd/src/WcbMp3.cpp (VOLUP :76, VOLDN :82, COUNT :89, VER :90). - ?KYBER topic == storeKyberSettings (WCB_Storage.cpp:1072-1115) incl. LOCAL,Sx,M...:W..S..:baud combined form and the ?MAESTRO,REMOTE canonical/alias note. - ?WCBQ topic — correctly says 1-20 (Help:392) == saveWCBQuantityPreferences (WCB_Storage.cpp:437). This is the sibling that proves ?WCB?'s 1-9 is stale, not house style. - ?BCAST/?LABEL/?MAP/?MAC/?EPASS/?DELIM/?FUNCCHAR/?ERASE/?LED topics match their parsers (WCB.ino:4603-4649, 4570-4601, 4473-4553, 4726-4748, 4750-4763, 5034-5077, 5128-5142). Only defect found in this group is ?LABEL's ?SLC1 example (F8). - Legacy substring offsets: updateLocalFunctionIdentifier/updateCommandCharacter charAt(2) (WCB.ino:5388,5399); storeCommand/eraseSingleCommand substring(2) (:5408,:5412); updateCommandDelimiter charAt(1) (:5416); ?SLSx substring(3) (:5278); ?PRSx/?POSx substring(3) (:5259,:5262). All correct for their documented spellings. - Legacy-chain ordering audited for shadowing (cclear-before-cc, smclear/smlist-before-smr-before-sm, pms/pclear/plist-before-prs-before-pos-before-po, wcbq-before-wcb, maestro_clear-before-maestro, etmcharcount/etmchardelay-before-etmchar, statsreset-before-stats). No prefix steals a longer command. - ?BCAST,OUT,S0 (broadcastToS0, WCB.ino:4629-4634, persisted) is undocumented in ?BCAST? — noted, not filed: per project memory this is deliberately Wizard-round-trip-only with no UI. - debugPWMEnabled (WCB.ino:177) is dead apart from the gated print at :6194 — flagged inside F2 rather than as its own row. NOT COVERED by this sweep (other sweeps' territory): Wizard/, .github/workflows/, tests/wdp_wire_test.cpp, docs/*.md page-vs-code accuracy, WDP wire compatibility with WCB_Client, NVS key-length audit.

- CROSS-REPO CONSISTENCY SWEEP. All four sibling repos were present on disk at c:/Users/ghulette/Documents/GitHub/ — WcbCmd, WCBClient, Arduino-Code/libraries, NaviCore. Nothing was guessed for absence. === (2) WDP WIRE FORMAT: Code/WCB/WCB_WDP.cpp vs WCBClient/src/WCB_Client.cpp — the highest-value check === Compared every TLV id, length rule, field order, framing constant, and unknown-TLV policy. VERIFIED CLEAN: - TLV ids identical where both define them: END 0x00, ALIAS 0x01, FWVER 0x03, HWVER 0x04, CAPFLAGS 0x05, MAESTRO 0x06, PORTLABEL 0x09, CTRLID 0x0A, DEVTYPE 0x0B, HWREV 0x0C, CAPTAGS 0x0D, MAESTRO_CFG 0x0E, FLAGS 0x12, SEQHASH 0x13. (WCB_WDP.cpp:67-99 vs WCB_Client.cpp:1821-1837.) - Firmware-only TLVs WLED_CFG 0x0F, PWMTARGET 0x10, SEQHASH-adjacent — the client skips them via `default: break;` (WCB_Client.cpp:2059) with correct length-advance. Forward-compatible both directions; both parsers advance `o += 2 + len` and both bound with `while (o + 2 <= 200)` + `if (o + 2 + len > 200) break;` (WCB_WDP.cpp:515-522 / WCB_Client.cpp:2013-2018). No parse overrun on either side. - Header framing identical: magic 'W', proto 0x01 (WCB_WDP.cpp:66 + WCB_WDP.h:29 vs WCB_Client.cpp:1819-1820). - putTLV/wcbPutTLV are byte-identical skip-if-no-room implementations (WCB_WDP.cpp:216-224 vs WCB_Client.cpp:1866-1874). - Maestro baud wire-code table byte-identical, both APPEND-ONLY with matching warnings: {0,110,300,600,1200,2400,9600,14400,19200,38400,57600,115200,128000,256000} (WCB_WDP.cpp:128-131 vs WCB_Client.cpp:1846-1849). Index-as-wire-code semantics match; both return 0xFF for unknown. - Field clamps match exactly on both sides: ALIAS/DEVTYPE 24, FWVER 27, HWREV 15, CAPTAGS 48, PORTLABEL port 1-5 + label 24, CAPFLAGS uint16 LE, SEQHASH uint32 LE. - Capability bitmap values identical, only names differ: 0x0001 HCR, 0x0002 MP3, 0x0004 WLED, 0x0008 KYBER_LOCAL, 0x0010 MAESTRO_REM/REMOTE, 0x0020 PWM, 0x0040 CONTROLLER, 0x0080 MAESTRO_LOC/HOST, 0x0100 DFPLAYER (WCB_WDP.h:32-40 vs WCB_Client.h:368-376). - Encoder TLV ORDER for MAESTRO-then-MAESTRO_CFG matches on both sides, which matters because the parsers let CFG overwrite the id-only record (WCB_WDP.cpp:557-571). - Transport struct byte-identical: espnow_struct_message_etm (WCB.ino:204-212) vs wcb_packet_etm_t (WCB_Client.h:154-168) — both `__attribute__((packed))`, same field order/sizes, 252 B. PACKET_TYPE_WDP = WCB_PACKET_WDP = 12 (WCB.ino:222 vs WCB_Client.h:52). - Board-count caps agree: MAX_WCB_COUNT 20 (WCB.ino:120) vs WCB_MAX_BOARDS 20 (WCB_Client.h:77). No 8/9-style cap found in either WDP path. - Controller-type vocabulary matches: wdpIsControllerType accepts "NaviCore"/"Sabé"/"Sabe" (WCB_WDP.cpp:408-413) and NaviCore advertises setIdentity("NaviCore", ...) (NaviCore.ino:4230). Auto-adopt fires. FOUND: SOLICIT 0x11 unhandled by WCB_Client (finding 1); TEMPORARY FLAGS TLV emitted last in the client's encoder (finding 4). === OTHER SHARED-WIRE SURFACES (not in the brief, checked anyway; all CLEAN) === - Sequence packets: WCB_PACKET_SEQ_REQ/SEQ_FRAG/SEQVAL_REQ/SEQVAL_FRAG = 13/14/15/16 match PACKET_TYPE_* (WCB_Client.h:217-220 vs WCB.ino:238-245). Structs field-for-field identical: seq_req 43 B, seqval_req 59 B with char key[16], seq_frag/config_frag 230 B with payload 183 (WCB_Client.h:224-253 vs WCB.ino:796-833). WCB_SEQ_PAYLOAD_SIZE 183 == CONFIG_PAYLOAD_SIZE (WCB.ino:252). MGMT chunk 16 × 179 vs firmware MGMT_MAX_CHUNKS 16 / MGMT_PAYLOAD_SIZE 180 — client is conservative by the NUL, safe. - Firmware receive path routes by size then packetType, with static_asserts pinning non-collision (WCB.ino:3598-3624); PACKET_TYPE_CONFIG_REQ and MGMT_FRAG_UNICAST both being 5 is disambiguated by struct size (43 vs 226). Verified the 43-byte handler explicitly admits PACKET_TYPE_SEQ_REQ (WCB.ino:3639). - CRC32 byte-identical (poly 0xEDB88320, init 0xFFFFFFFF, final invert): WCB.ino:752-764 vs WCB_Client.cpp:2714-2724. - CHKSM default agrees on both sides — firmware `etmChecksumEnabled = true` (WCB.ino:271) and NVS default true (WCB_Storage.cpp:2233); client `_checksumEnabled = true` (WCB_Client.h:1088). A mismatch here would silently reject all traffic; it is correct. - MAC derivation scheme agrees: 02:oct2:oct3:00:00:ID and broadcast FF:oct2:oct3:FF:FF:FF (WCB_Client.cpp:1593-1603) matches the firmware's gate `src_addr[1]/[2]` vs umac_oct2/oct3 (WCB.ino:3593, :4023). - Client-sent commands exist in the firmware: `?SEQ,SAVE,<key>,<value>` and `?SEQ,CLEAR,<key>` (WCB_Client.cpp:1324/1337) are both implemented (WCB.ino:4984-4995). The firmware's special ?SEQ,SAVE delimiter-boundary parsing (WCB.ino:2044-2053) is the path these take. === (1) WcbCmd API USAGE BY THIS FIRMWARE — CLEAN === Enumerated every public signature in WcbCmd 0.8.0 (src/WcbMaestro.h:74-125, WcbMp3.h:22-46, WcbWled.h:33-37, WcbHcr.h, WcbHcrFade.h:29-36, WcbDfPlayer.h:45-73) and grepped every firmware call site (WCB_DFP.cpp:42-101, WCB_HCR.cpp:80-279, WCB_MP3.cpp:45-138, WCB_Maestro.cpp:117-409, WCB_WLED.cpp:105, WCB.ino:87/6181/7210). Every call matches a declared signature; no use of a removed or changed API. WCBCMD_VERSION is 0.8.0 and library.properties agrees. NaviCore compiles the same library and uses the same codecs (NaviCore.ino:50, 625, 837-838, 1277-1294, 1588, 1713) — the "NaviCore must still adopt HcrFade/vol±" state in older notes is now DONE. - Cross-checked the one place bounds are DUPLICATED rather than shared: NaviCore's dfpFormatCommand (NaviCore.ino:1656-1697) vs WcbCmd DfPlayerCodec::handle (WcbDfPlayer.cpp:63-160). All bounds match exactly — PLAY 1-2999, FOLDER 1-99/1-255, MP3FOLDER 1-9999, VOL 0-30 (DFP_VOL_MAX), LOOPALL 0-1, LOOPFOLDER 1-99, LOOP 1-2999, EQ 0-5, DEVICE 1-5. VERIFIED GOOD — no drift. - Minor, not reported: WcbCmd.h:24-26 says "Both sketches should assert WCBCMD_VERSION"; neither asserts — the WCB only prints it (WCB.ino:7210). The wording is aspirational ("should"), so it is not a false comment. === (4) SKETCHBOOK COPIES THAT SHADOW THE REAL LIBRARIES — CLEAN, and worth not re-checking === - Arduino-Code/libraries/WcbCmd/src vs WcbCmd/src: `diff -rq` flags all 10 non-DFPlayer files as differing, but this is CRLF vs LF ONLY. `diff --strip-trailing-cr` returns 0 differing lines for every file (WcbHcr.h/.cpp, WcbHcrFade.cpp, WcbMaestro.h/.cpp, WcbMp3.cpp, WcbWled.cpp verified individually); WcbCmd.h and WcbDfPlayer.* are byte-identical. A local compile and CI therefore produce the same device bytes. Do not re-chase the noisy diff -rq output. - Arduino-Code/libraries/WCB_Client/src vs WCBClient/src: byte-identical, no differences at all. - Only real drift: Arduino-Code/libraries/WCB_Client/library.properties says version=1.13.2 while canonical is 1.15.0 (plus a shorter paragraph). There is NO version constant in the client source (grep for WCB_CLIENT_VERSION returns nothing), so this is Library-Manager metadata only and cannot change compiled behaviour. Not reported. - CI clone verified: .github/workflows/build.yml:82-83 does `rm -rf "$LIBDIR/WcbCmd"` then `retry git clone --depth 1 https://github.com/greghulette/WcbCmd.git` with no `-b`, i.e. the default branch; WcbCmd's local branch is `main` and is 0 ahead / 0 behind origin/main with a clean tree. Code/bin/build.sh does NOT clone — local builds use the sketchbook copy, which is content-identical, so local and CI agree. - Release-blocking push state: WcbCmd clean and in sync with origin/main at 168f1e5 (0.8.0); WCBClient clean on master at b8a26f8 (1.15.0) tracking origin/master with nothing ahead. The "push WcbCmd before the firmware" rule is satisfied — no unpushed shared-library change would be missed by CI. === (3) serial-hub.js LOCKSTEP — DIVERGED (finding 2) === Full `diff -u` performed. NaviCore/config_tool/serial-hub.js 519 lines (md5 fb9675b8e7a9107c27afdf9c6b9ce02b) vs Wizard/serial-hub.js 548 lines (md5 a80702f21f141af8d165f14fc0f54c8f). Every divergence enumerated: two missing lock-squat fixes + the backoff-reset relocation (reported), and two Wizard-only additions `adoptPort`/`releasePortKeepOpen` (used at Wizard/app.js:5490/5765/7032; confirmed NaviCore's config_tool references neither, so additive only). === DOCS CHECKED AGAINST CODE === docs/WDP_DESIGN.md TLV table (lines 81-104) matches the firmware source exactly — every id, length and CAPFLAGS bit verified against WCB_WDP.cpp/WCB_WDP.h. The doc is accurate; it is WCB_Client that violates the SOLICIT contract the doc states at line 95. No S4 doc-drift finding. === NOT COVERED BY THIS SWEEP === Single-repo firmware logic (ETM retry/pending, OTA, PWM, Variables, Storage beyond the sequence path), Wizard UI logic other than serial-hub.js and the sequence-key/WDP-poll call sites, tests/wdp_wire_test.cpp internals, and CI behaviour beyond the WcbCmd clone step and the path-filter premise. Also not chased: the WCBClient MgmtRelay ?OTA-handler question from prior session notes — WCBClient's tree is clean and committed, but I did not verify the relay OTA handler's presence since it is a WCBClient-repo concern, not a WCB release artifact.

- SWEEP: CI, build and release pipeline. Read in full: .github/workflows/build.yml (183 lines), pages-deploy.yml (133), wdp-tests.yml (30), Code/bin/build.sh (213). Also read for cross-checking: .git/hooks/pre-commit + pre-commit.sh (the real version stamper), Wizard/flasher.js:1-200, Wizard/app.js:105-145, Wizard/watch-version.js, tests/wdp_wire_test.cpp:1-60, Code/WCB/WCB_WDP.cpp:60-140/216-224, Code/WCB/WCB_WDP.h, .gitignore. Shell semantics were verified by running them, not assumed (empty PY_BIN; cp into a missing dir; grep+pipefail under set -e; git add <dir> staging deletions on git 2.55). ENUMERATION (each item judged; VERIFIED GOOD unless it became a finding): A. PATH FILTERS / RULE 9 - build.yml:11-13 paths = Code/**, .github/workflows/build.yml. VERIFIED GOOD: a Wizard-only or docs-only push cannot trigger the firmware build. Corroborated two ways: (1) .git/hooks/pre-commit.sh:7-8 scopes its stamping — it rewrites Code/WCB/WCB.ino only when Code/WCB/ is staged and Wizard/app.js only when Wizard/ is staged, so a Wizard-only commit touches nothing under Code/**; (2) git log shows Auto-build commits following Code commits (bbd6bdf) and none following Wizard-only commits (3d74fa8, 6f3ad75, e62eab7). - The rationale comment at build.yml:140-141 is nonetheless false (no fw_version.h exists anywhere) → REPORTED S4. - pages-deploy.yml:34-37 branches ['**','!gh-pages','!claude/**'] + paths Wizard/**. VERIFIED GOOD ordering (negations after positives) and correct self-exclusion of the publish branch. - wdp-tests.yml:9-19 paths cover the test + WCB_WDP.{h,cpp} + itself. VERIFIED GOOD. - Note (not a defect): the auto-build commit message carries [skip ci] (build.yml:132), which suppresses ALL workflows for that commit, so the Wizard/bin files it writes never reach Pages. Harmless only because nothing reads Wizard/bin (→ S4 finding). B. VERSION BUMP / DTG / TAG / RELEASE - build.yml:106-119 auto-version: semver parse, PATCH+1, TZ='America/New_York' DTG, two seds. VERIFIED GOOD against the source: WCB.ino:181 `String SoftwareVersion = "6.2.0_172045RAUG2026";` matches the sed at :113, and the banner at WCB.ino:29 (`***** ... Version 6.2.0_172045RAUG2026 *****`) matches the address-scoped sed at :116 (6 digits / R / 3 uppercase / 4 digits). - VERIFIED GOOD: binaries do match the tagged source by construction — the version is stamped BEFORE build.sh runs (:100-124), the bins commit is pushed (:144-152), and the tag is applied to that same local HEAD (:174). - VERIFIED GOOD: `git push origin HEAD:<branch>` (:145) is never forced, so the retry loop cannot clobber another run's commits; a conflicting rebase exits 1 (:151) rather than resolving arbitrarily. The loop is bounded at 5 and cannot spin. - REPORTED: the tag-exists guard is inoperative (checkout fetches --no-tags) and the tag is pushed before assets are validated (S3); duplicate/mismatched VERSION sources with no cross-check (S3); no concurrency group (S3). - Duplicate-tag analysis: two same-minute main runs would compute an identical tag, but the loser is stopped earlier by a guaranteed rebase conflict on WCB.ino:181, so a duplicate Release cannot actually be published. Recorded, not reported. - `gh release create --latest --generate-notes` (:178-182) and the `gh release view` guard (:177): VERIFIED GOOD (the API-backed guard works, unlike the git one). C. build.sh FAILURE SEMANTICS - The both-targets-or-nothing gate at :119-128 is REAL for the compile step: on a compile failure it removes the temp dirs and exits 1, and build.yml:121-124 (default `bash -e`) then skips the commit step. VERIFIED GOOD. - The gate is NOT real for the publish step: no set -e, no checked cp, `exit 0` at :213 regardless → REPORTED S2 (top finding), with historical proof at commit e746e56. - check_boot (:156-177) verifying magic 0xE9 + flash-size nibble on both custom S3 bootloaders before publishing, and the deliberately narrow `rm -f WCB_*_ESP32*.bin` at :181 that spares WCB_S3_custom_bootloader_*.bin: VERIFIED GOOD (I confirmed the glob does not match the custom bootloader names). - FQBNs at :89 and :105 both carry PartitionScheme=min_spiffs, and build.yml:52 pins esp32:esp32@3.3.4 — both match CLAUDE.md rules. VERIFIED GOOD. - Library pinning: EspSoftwareSerial@8.1.0 and Adafruit NeoPixel@1.12.5 are version-pinned (build.yml:66-67); only WcbCmd is unpinned → REPORTED S3 (provenance, not the clone itself, which is documented as deliberate at :74-77). - The `retry` helper is correctly redefined in each step that uses it (:42-50, :57-65) — shell functions do not cross step boundaries. VERIFIED GOOD. - `Install Arduino CLI` (:28-31) has no pipefail, but `arduino-cli version` on the next line turns a silent curl failure into a hard 127. VERIFIED GOOD. D. UNQUOTED / UNGUARDED SHELL VARIABLES (traced individually, as asked) - VERSION empty → REPORTED S3 (traced through filenames, the release glob, and Wizard/app.js:135's regex). - BRANCH with a "/" → REPORTED S2 (traced; already occurred on origin/fix/raw-serial-bidirectional). - PY_BIN empty → REPORTED S3 (fails loud, but misdiagnosed as a corrupt bootloader). - BRANCH empty / detached HEAD: VERIFIED GOOD — `:49-51` falls back to GITHUB_REF_NAME then "unknown", and origin/main's committed filenames (WCB_6.1.5_290119RJUN2026_main_*.bin) confirm the branch resolves to "main" in CI, so build.yml:182's literal `_main_` glob is correct. - All variable expansions in build.sh are double-quoted; `rm -rf ""` would be a no-op. VERIFIED GOOD. - build.yml:133 `[ -n "${NEWVER:-}" ] && MSG=...` under `bash -e`: does NOT abort on a non-main branch (the failing test is not the last command of the AND-list); empirically confirmed by the existence of "Auto-build" commits on the OTA branch. VERIFIED GOOD. E. pages-deploy.yml - REPORTED S3: the grep/pipefail assignment at :103. - VERIFIED GOOD: per-branch concurrency group with the reasoning at :44-51; timeout-minutes: 10 (:58); slice-only rewrite via `rm -rf "${GHP:?}/$DEST"` (:93) where DEST derives from GITHUB_REF_NAME (git ref names cannot contain ".."); the token only appears inside a mktemp'd clone's remote URL and is never echoed. - Recorded, not reported: the retry loop's `git rebase … || true` (:130) leaves a conflicted rebase in progress, after which the remaining `git push origin gh-pages` attempts push the un-rebased branch ref and all three fail — the run still ends loud at :132-133, just with a misleading cause. Also, gh-pages preview slices are never garbage-collected when a branch is deleted, and each slice includes the unused 2.5 MB Wizard/bin (folded into the S4 finding). F. SECRETS / PERMISSION SCOPE - build.yml:15-16 contents: write — required by the bins push and `gh release create`. Correctly scoped, no other secrets referenced. VERIFIED GOOD. - pages-deploy.yml:40-41 contents: write — required. VERIFIED GOOD. - wdp-tests.yml has no permissions block → REPORTED S4. - No workflow uses pull_request_target, no untrusted checkout, no secret is echoed. VERIFIED GOOD. G. wdp-tests.yml GATING - It gates nothing (no dependency from build.yml; branch protection is not visible from here), which its own comment at :5 declares deliberate — not reported as a defect. - The test is a hand-maintained mirror, not a link against the firmware (documented at tests/wdp_wire_test.cpp:11-20). I spot-checked the mirror for drift and found NONE: TLV ids 0x00/01/03/04/05/06/09/0A/0B/0C/0D/0E/0F/10/11/12/13 (WCB_WDP.cpp:67-97) vs the mirror at wdp_wire_test.cpp:35-40; WDP_MAGIC 'W' (WCB_WDP.cpp:66); WDP_PROTO_VERSION 0x01 (WCB_WDP.h:34); the 14-entry baud table (WCB_WDP.cpp:129-132) vs :44; putTLV's len clamps and bounds check (WCB_WDP.cpp:216-224) vs :50-58. VERIFIED GOOD. - Minor and unreported: the test's firmware line anchors have drifted (it cites WCB_WDP.cpp:200 for putTLV which is at :216, :113 for the baud table which is at :129, :381 for wdpCapOwner which is at :437, :431 for wdpOnAdvertReceived which is at :487, :573 for the advert-count gate which is at :654). The referenced code is correct; only the line numbers are stale. H. THINGS I DELIBERATELY DID NOT REPORT - The unpinned WcbCmd *clone* itself (documented as deliberate, build.yml:74-77) — I reported only the missing provenance record. - wdp-tests.yml not gating the build (documented as deliberate, :5). - S3 `_boot` artifacts being the hand-built custom bootloaders rather than the compiled ones (documented at build.sh:136-150, and check_boot enforces the header). - The legacy unsized `_ESP32S3_boot.bin` duplicate (documented at build.sh:199-201; flasher.js:155-157 uses it only as the 16 MB fallback). - Recorded but too marginal to spend a slot on: during a merge window Code/bin can transiently hold two version sets, and Wizard/flasher.js:127 `files.find(...endsWith(...))` takes the first by name-sort — which yields a self-consistent but OLDER set (e.g. 6.1.5 over 6.2.0) until the auto-build commit lands, and permanently if that build fails. Also note string-sort would order a future 6.10.x before 6.2.x. 

- SWEEP: config round-trip completeness. Method: enumerated every `?` verb in processLocalCommand (Code/WCB/WCB.ino:4394-5355) plus every subsystem handler, then traced each through (2) its put* writer, (3) its get* reader AND whether that reader is reached from setup(), and (4) whether it appears in collectConfigCommands (WCB.ino:2762-2932) / buildConfigString (:2938) / printBackupConfig (:5717) — and additionally (5) whether the Wizard's parser.js parseToken() ingests it and buildCommandString() re-emits it, since the Wizard is the primary config path. === COLUMN-3 MECHANICAL CHECK: CLEAN === Extracted every `void load*` / `begin*` definition across Code/WCB/*.cpp and grepped setup() (WCB.ino:7106-7480) for each. All are reached. The three that are not called by name from setup() are called indirectly and were verified: loadPWMMappingsFromPreferences + loadPWMOutputPortsFromPreferences via initPWM() (WCB_PWM.cpp:125-133, called WCB.ino:7152), loadWdpSettings via wdpBegin() (WCB_WDP.cpp:1281-1303, called WCB.ino:7151). loadMP3Settings/loadDFPSettings/loadHCRSettings/loadWLEDSettings/loadVariables/loadMaestroSettings/loadLearnedPeers all confirmed present. **No setting is persisted-but-never-restored.** === COLUMN-2 NVS KEY-LENGTH CHECK: CLEAN except sequences === Extracted every literal key from `preferences.put*("...")` / `get*` across Code/WCB — ALL are <=15 chars (longest: "special_peer_id" = 15, "etmCharCount"/"etmCharDelay" = 12, "mirror_kyber" = 12). Every dynamically-built key was read and bounded: "Serial<n>", "S<n>", "label<n>", "mon<n>", "blk<n>", "sm<i>_act|_in|_cnt|_raw", "sm<i>_<j>w|p", "m<i>_id|_port|_wcb|_en|_baud", "w<i>_id|_port|_wcb|_en|_baud", "map<i>", "port<i>", "auto<i>" — all <=9 chars. Variables use ONE key ("blob", WCB_Variables.cpp:89) and enforce WCB_VAR_NAME_MAX=15 in isValidVariableName (:63-73). The ONLY unbounded NVS key in the firmware is a stored-sequence name (WCB_Storage.cpp:632/643) — reported. Namespaces in use (all <=15): hw_version, led_config, serial_baud, wcb_config, bdcst_set, mac_config, espnow_config, command_config, stored_cmds, stored_commands(legacy), kyber_settings, kyber_targets, serial_labels, serial_map, serial_monitor, bcast_block, maestro_cfg, etm_config, mp3_cfg, dfp_cfg, wdp_cfg, learned_peers, hcr_cfg, wled_cfg, wcb_vars, pwm_mappings, pwm_outputs. === THE FOUR-COLUMN TABLE === Legend: (2)=persisted (3)=loaded at boot (4)=in firmware config chain (5)=Wizard parse/emit. OK = verified clean. | Setting (verb) | (2) NVS | (3) boot load | (4) chain | (5) Wizard | |---|---|---|---|---| | ?HW,x | hw_version/hw_version (guards invalid, Storage.cpp:70-79) | loadHWversion :7139 | :2777 emit HW | parse 471 / emit 1241 (guards HW,0) — OK | | ?LED,PIN,x | led_config/led_pin, any HW | loadStatusLEDPin :7140 | **:2780 only HW 31/32** | **1243-1250 only 31/32** — **GAP (reported)** | | ?MAC,2/3 | mac_config/umac_oct2,3 | loadMACPreferences :7161 | :2782-2783 | parse 547 / emit 1285-1288 — OK | | ?WCB,n | wcb_config/WCB_Number | :7150 | :2784 | 485 / 1253 — OK | | ?ALIAS | wcb_config/alias | loadWCBAlias :7154 | :2785 (non-empty only) | 524 / 1256-1264 incl. ALIAS,CLEAR — OK | | ?WCBQ,n | wcb_config/wcb_quantity | :7155 | :2786 | 492 / 1266 — OK | | ?WCBCH,n | wcb_config/mesh_channel | :7156 | :2787 | 496 / 1272 — OK | | ?EPASS | espnow_config/password | :7330 | :2794 | 542 / 1291 — OK | | ?DELIM | command_config/delimiter | loadCommandDelimiter :7412 | :2799 (includeInLive=false) | 553 / 1295 — OK | | ?FUNCCHAR | command_config/LocalFunc | :7413 | :2800 (false) | 557 / 1301-1303 (deliberately skips '?') — OK | | ?CMDCHAR | command_config/CmdChar | :7413 | :2801 | 561 / 1307 — OK | | ?BAUD,Sx | serial_baud/Serial<n> | :7215 | :2804 | 566 / 1314 — OK | | ?LABEL,Sx | serial_labels/label<n> | :7216 | :2806 (non-empty) | 577 / 1322 — **clear never emitted (already recorded wave3:1635)** | | ?BCAST,OUT/IN Sx | bdcst_set/S<n>, bcast_block/blk<n> | :7220,:7218 | :2811-2814 | 588 / 1330-1332 — OK | | ?BCAST,OUT,S0 | bdcst_set/S0 | same | :2812 | 594 / 1336 — OK | | **?BCAST,RESET** | — | — | — | — | **GAP (reported): incomplete + stale RAM** | | ?MAP,SERIAL | serial_map/sm* | loadSerialMonitorMappings :7219 | :2817-2830 | 894 / 1509-1522 — **add-only + rawMode not persisted (both reported)** | | ?MAP,PWM / OUT | pwm_mappings, pwm_outputs | initPWM :7152 | :2915-2929 | 877-893 / 1500-1506 — OK (PWM replaces, unlike SERIAL) | | ?KYBER,LOCAL,Sx[+targets] | kyber_settings K_Location/K_Port, kyber_targets | loadKyberSettings :7151, loadKyberTargets :7153 | :2836-2855 | 607 / 1346-1350 — OK | | ?MAESTRO,REMOTE | kyber_settings K_Location | same | :2857 | 780 / 1352 — OK | | **?KYBER,CLEAR** | same | same | :2859 | 1355 | **GAP (reported): clobbers S2** | | ?MAESTRO,M<id>:W..S..:baud | maestro_cfg/m<i>_* | loadMaestroSettings :7141 + normalizeMaestroSelfSlots :7153 | printMaestroBackup Maestro.cpp:924 | 775 / 1479 — OK (remote slots force serialPort=0 at Maestro.cpp:688, so the emitted S-value is harmless) | | ?MP3,S..:V.. / ONERR / REMOTE | mp3_cfg | loadMP3Settings :7142 | printMP3Backup MP3.cpp:398 | 647 / 1364-1367 — OK | | ?DFP,... | dfp_cfg | loadDFPSettings :7143 | printDFPBackup DFP.cpp:351 | 702 / 1399-1402 — OK (note: dfp_cfg missing from eraseNVSFlash — already recorded wave2:172) | | ?HCR,PORT / POLL / REMOTE | hcr_cfg | loadHCRSettings :7144 | printHCRBackup HCR.cpp:706 | 675 / 1382-1385 — OK | | ?WLED,<id>:W..S..:baud | wled_cfg/w<i>_* | loadWLEDSettings :7145 | printWLEDBackup WLED.cpp:387 | 730 / 1421-1424 — OK; remote proxies parsed display-only (deliberate, WDP re-learns) | | ?VAR,SET / CLEAR | wcb_vars/blob | loadVariables :7146 | printVariablesBackup Variables.cpp:475 (persist-only) | 852 / 1436-1441 — OK | | ?SEQ,SAVE / CLEAR | stored_cmds/<key>+key_list | read on demand + migrateOldStoredCommands :7214 | :2903-2913 | 838 / 1531-1536 — **key>15 chars silently lost (reported)** | | ?ETM,ON/OFF/TIMEOUT/HB/MISS/BOOT/COUNT/DELAY/CHKSM | etm_config (9 keys) | loadETMSettings :7414 | :2862-2870 (all 9, unconditional) | 821 / 1486-1497 — sub-settings gated on `if (etm.enabled)`, so an ETM-off board's tuning is dropped by a Wizard push (already recorded wave3:2598) | | ?CONTROLLER,ON/OFF[,id] | wcb_config special_peer/special_peer_id | :7157,:7158 | :2876-2886 (ON,id + OFF pair preserves a custom id) | 511 parses / **1281 drops custom id when OFF — GAP (reported)** | | ?WDP,ON/OFF, AUTOJOIN | wdp_cfg en/autojoin | wdpBegin :7151 | :2892-2893 | **no case, no emit — GAP (reported)** | | ?PEERSLIVE | derived | — | :2792 includeInLive=false | 504 read-only — OK by design | | learned peers | learned_peers ver/o2/o3/mask | loadLearnedPeers :7392 (after esp_now_init) | deliberately NOT emitted (comment :2888-2891) | — OK by design | | ?DEBUG,* / ?TRACK,* / ?STATS,* | not persisted | — | — | — | correct: runtime diagnostics | | ?OTA / ?OTALOCAL / ?MGMT / ?RTERM / ?IDENTIFY / ?ERASE,NVS | actions, no state | — | — | — | OK | === VERIFIED GOOD (do not re-check) === - Every emitter's ORDERING is safe for the settings it feeds: WCB,<n> precedes MAESTRO/WLED/KYBER in both emitters, so the local-vs-remote `W<n>` comparison in parser.js:759/:788 and configureMaestro resolves against the right board number. - buildConfigString (WCB.ino:2938) and printBackupConfig (:5717) both drive collectConfigCommands and both call the same six sub-emitters (Maestro, MP3, DFP, HCR, WLED, Variables) — verified line by line at :2945-2951 vs :5755-5760. No serial-vs-relay drift. - CRC: both writers use `%08X` (:2958-2960, :5768-5774) and the leading-zero legacy pad is handled at :1971. - parseCommandsAndEnqueue's ?SEQ,SAVE / ?CS / ?MGMT special-case splitting works correctly for the LIVE backup chain and for the Wizard's all-'?' relay pull; it is only the FACTORY chain after ?FUNCCHAR that breaks (reported). - saveWCBQuantityPreferences, saveSpecialPeerIDToPreferences, saveMeshChannelToPreferences, saveHWversion all range-check BEFORE the NVS write. - MAX_WCB_COUNT=20 is used consistently in the config-path bound checks I touched: ?WCB (WCB.ino:4766), ?HCR,REMOTE (HCR.cpp:554), ?MP3,REMOTE (MP3.cpp:222), ?MAP,SERIAL destination (Storage.cpp:1717), Kyber target WCB (Storage.cpp:1249). No legacy 8/9 cap found on the config-round-trip path. (The `// 0 = local, 1-9 = remote WCB` comment on SerialMonitorOutput at WCB_Storage.h:79 is a stale comment only — the code accepts 1..MAX_WCB_COUNT.) === DEAD CONFIG STATE (S4, already recorded wave2:2222 — not re-reported) === serialMonitorEnabled[5], mirrorToUSB, mirrorToKyber are persisted by saveSerialMonitorSettings() (Storage.cpp:1563) and loaded every boot (:1761, called WCB.ino:7217), but saveSerialMonitorSettings() has ZERO callers and no command sets any of the three. Their only reader is the status print at WCB.ino:2152-2166, which therefore always prints "None". setSerialMappingRawMode() (Storage.cpp:1818) likewise has zero callers. === DEDUPE NOTE FOR THE PARENT === This repo already contains four prior review documents (docs/CODE_REVIEW_2026-08*.md, 7266 lines). Findings 1, 4, 5, 6, 7 above were previously recorded there and are re-verified UNFIXED at the current OTA head — each cites its prior location in its description. Findings 2, 3, 8, 9 are NEW (grepped the prior corpus for each: `BCAST,RESET`/`resetBroadcastSettings`, `defaultFunc`+SEQ,SAVE splitter, `CONTROLLER,OFF` emit side, `LED,PIN` config-chain gating — no prior hits). The four cross-cutting sweeps listed as "still to review" in CODE_REVIEW_2026-08.md:413 include "config round-trip"; this sweep discharges that one.

- MEMORY / STACK / HEAP SWEEP — Code/WCB/, branch OTA. Built the full enumeration first, then judged each item. Measured, not estimated where possible: compiled the ESP32 target (`arduino-cli compile --fqbn esp32:esp32:esp32:PartitionScheme=min_spiffs`, core 3.3.4, isolated --build-path) — **flash 1,289,711 B (65% of 1,966,080); static RAM 86,592 B (26% of 327,680), leaving 241,088 B for locals.** Nothing is unexpectedly large; the headroom is comfortable. === (1) GLOBAL FIXED-SIZE ARRAYS / STRUCTS — ENUMERATED, ALL ACCOUNTED FOR === Largest statics, computed from the struct definitions: - 5 × `ConfigPullSession` (WCB.ino:872-876: pullSession, statsRelaySession, etmRelaySession, seqRelaySession, seqValRelaySession), each `chunks[16][184]` ≈ 2.96 KB → **~14.8 KB**. Five separate copies is the single biggest static consumer; deliberate (one relay session per query type) but the only candidate for consolidation if RAM ever gets tight. - `rcJsonRelayQueue` 64 × 220 = **~14 KB heap** (WCB.ino:7127) — sizing rationale documented at WCB.ino:322-327, verified accurate. - `wdpNeighbors[MAX_WCB_COUNT]` (WCB_WDP.cpp:50), WdpNeighbor ≈ 328 B → **~6.5 KB**. - `commandsToSend[10]` + `commandsToReceive[10]` @ 249 B = **4,980 B** (WCB.ino:881-882). - `MgmtSession mgmtSession` `chunks[16][181]` ≈ **2.9 KB** (WCB.ino:858). - `etmPendingTable[10]` × 272 B = **2,720 B** (WCB.ino:519) — the only MAX_WCB_COUNT-sized member arrays inside it are three `[20]` byte arrays, cheap. - `etmCharPhaseSentTimes[3][200]` = **2,400 B** (WCB.ino:577); `etmCharBoardResults[3][MAX_WCB_COUNT]` × 20 B = **1,200 B** (WCB.ino:576). These are the two multi-dimensional MAX_WCB_COUNT arrays — both small, no concern. - `s_otaChunk[1024]` (WCB_OTA.cpp:224) — deliberately static to avoid per-call malloc, comment accurate. - Small fry, all fine: boardTable[20]=480 B, reportedStats[20]=720 B, 4× etmStats*[20]=320 B, etmSeqHistory[20][8]=320 B, WCBMacAddresses[20][6]=120 B, vars[100] (WCB_Variables.cpp:27), maestroConfigs[9], wledConfigs[9], pwmMappings[10], serialMonitorMappings[5], kyberTargets[9], wdpDaDevices[5]. - Heap queues created in setup ≈ 26 KB total: commandQueue 200×12 (WCB.ino:7191), rcJsonRelay 64×220, pendingTimerChain 8×222, wdpPkt 8×252, mgmtReq 8×~64, otaPkt 12×246 (lazy, WCB_OTA.cpp:546), rterm 16×162 (lazy, WCB_RemoteTerm.cpp:28). - DEAD: `extern String storedCommands[MAX_STORED_COMMANDS]` (WCB_Storage.cpp:23) has no definition — WCB.ino:907 is commented out. Only referenced from a comment (WCB_Storage.cpp:550). Links only because nothing uses it. S4, not reported. === (2) TASK STACKS vs WORK — ALL SIX ENUMERATED === serialCommandTask 4096 (WCB.ino:7439) · KyberLocalTask 4096 (:7443) · KyberRemoteTask 4096 (:7447) · PWMTask 4096 (:7459) · RawSerialForwardingTask 4096 (:7464) · identifyTask 2048 (:5082). No `SET_LOOP_TASK_STACK_SIZE` anywhere, so loopTask = default 8192. **No stack is undersized for what its task does** — the heavy work (processLocalCommand, printBackupConfig, NVS) all runs on loopTask via the command queue, not on the 4 KB tasks. serialCommandTask's deepest chain (processIncomingSerial → processSerialCommandHelper → parseCommandsAndEnqueue → enqueueCommand, plus wdpDaHandleLine) uses String objects (12 B each on stack, buffers on heap) and no large stack arrays. identifyTask at 2048 only calls colorWipeStatus + vTaskDelay — thin but adequate. **Verified: no task stack finding.** === (6) STACK BUFFERS IN WiFi-CALLBACK CONTEXT — ENUMERATED, CLEAN === Worst-case frame in `espNowReceiveCallback` (WCB.ino:3584): `espnow_struct_message peek` 249 + `espnow_struct_message received` 249 + `espnow_struct_message_etm etmReceived` 252 (mutually exclusive scopes), plus at most one queue slot — `PendingTimerChainSlot` 222 (WCB.ino:434), `RcJsonRelaySlot` 220 (WCB.ino:368), `OtaPktSlot` 246 (WCB_OTA.cpp:543), `RtermQueueItem` 162 (WCB_RemoteTerm.cpp:156), `espnow_struct_mgmt` 226 (WCB.ino:2632). Sum stays well under the WiFi task's 6656 B default. Other large stack buffers checked and bounded: WCB.ino:1312 withCRC[210], :1640 buf[192], :1792 buf[256], :2296 withCRC[210], WCB_HCR.cpp:688 line[256], WCB_WDP.cpp:321/375 [200], :1037 caps[128]. **No stack-overflow finding.** === (4) RECURSION — ENUMERATED === Exactly one self-recursive function in the firmware: `parseCommandsAndEnqueue` → itself at WCB.ino:1989. Depth is bounded by the number of `^?CHK` tokens whose checksums each verify (a mismatch returns at :1997), so realistically depth 1. Not reported. No other self- or mutual recursion found. === (5) UNCHECKED ALLOCATIONS — ENUMERATED, CLEAN === Only one `malloc` in the firmware: WCB.ino:1927, **null-checked** at :1928, freed on queue-full at :1940 and after dispatch at :7505 — no leak on either path. `new` sites: `statusLED` (WCB.ino:6740, matched delete at :6754), `_hcr` (WCB_HCR.cpp:132-136, deleted at :122 and :444), and the vendored `SoftwareSerial` at src/HumanCyborgRelationsAPI/hcr.cpp:63. None null-check, but all are one-shot config-time allocations, not per-packet — not worth a finding. === VERIFIED GOOD (do not re-audit) === - **All six wire-fragment reassemblers bounds-check the attacker-supplied chunkIdx/totalChunks before indexing `chunks[16][...]`**: WCB.ino:2644-2645 (MGMT), 3048-3049 (CONFIG), 3307-3308 (STATS), 3342-3343 (SEQ), 3377-3378 (SEQVAL), 3412-3413 (ETM). Each checks both `totalChunks == 0 || > MGMT_MAX_CHUNKS` and `chunkIdx >= MGMT_MAX_CHUNKS || >= totalChunks`. This was my highest-suspicion overflow candidate; it is clean. - Kyber / raw-serial wire length fields bounds-checked before the write: WCB.ino:4041 (chunkLen > 196), :4085 (> 180), :4125 (> 177), each with the arithmetic justified in an adjacent comment that matches the code. - PWM ISRs (WCB_PWM.cpp:80-125) are `IRAM_ATTR` and touch only volatile scalars — no String, Serial, or malloc in interrupt context. - ETM ACK ring (WCB.ino:1108-1145): correct SPSC ring with `portMUX` on both ends; the WiFi task never touches `etmPendingTable` directly. The race the comment describes is genuinely fixed. - Deferral queues all correctly drained in loop(): OTA flash writes (WCB.ino:7492 → WCB_OTA.cpp:554), WDP decode (:7494), MGMT REQ responses (:7491), timer chains (:7480), RC JSON (:7479), RTERM (:7478). - `RawSerialForwardingTask` uses `static uint8_t buffer[64]` with a `len < sizeof(buffer)` guard (WCB.ino:6637, 6668); Kyber forwarders use `static uint8_t remoteBuf[64]` with flush-on-full rather than silent truncation (WCB.ino:4252/4275, 4311, 4331). No String in any per-byte path. - `WCBSerial::_bufChar` cannot overflow single-threaded — max index written is 159 of 160. (It can overflow multi-threaded; that IS reported.) - `sequenceInventoryHash()` (WCB_Storage.cpp:759) is properly cached against the 2 Hz WDP dirty-check; invalidation call sites at WCB_Storage.cpp:662, 703, 836, 931 cover the writers at :642, :673, :833, :925. The fifth writer (:966) is inside `eraseNVSFlash()`, which calls `ESP.restart()` at :1032 — stale cache is unreachable. Clean. - `handleSingleCommand(String cmd, …)` (WCB.ino:4354) and `writeSerialString(Stream&, String)` (WCB.ino:1862) take String by value, costing one copy each; per-command not per-byte, so noted but not reported. === NOT COVERED BY THIS SWEEP === Wizard/, .github/workflows/, tests/wdp_wire_test.cpp, NVS key-length limits, WDP wire-format compatibility, and `;`-verb semantics were out of scope for the memory sweep and were not examined.

- SWEEP: legacy board-count caps across Code/WCB/*.{ino,cpp,h}, plus the Wizard where the same domain is mirrored. Method: full grep for `[8]`/`[9]`/`[10]` as array dimensions, `< / <= / > / >= / == / !=` against 8/9/10, every `for` bound of 8/9/10/11/16, every array declared `[MAX_WCB_COUNT]` and every non-loop-variable index into one, plus every `-1` index derived from a board number. Each hit was opened and judged. === CONSTANTS CONFIRMED (read, not assumed) === MAX_WCB_COUNT 20 — WCB.ino:120, mirrored WCB_Storage.h:12, WCB_OTA.h:64. WCB_SPECIAL_PEER_ID = 20 (uint8_t, WCB.ino:129). MAX_MAESTROS_PER_WCB 9 (WCB_Maestro.h:14). MAX_WLED_PER_WCB 9 (WCB_WLED.h:51). MAX_KYBER_TARGETS 9 (WCB_Storage.h:104). MAX_PWM_MAPPINGS 10 / MAX_PWM_OUTPUT_PORTS 5 (WCB_PWM.h:6-7). WDP_MAX_MAESTRO 9 / WDP_MAX_WLED 9 / WDP_MAX_PWMTARGET 10 (WCB_WDP.h:35-37). ETM_PENDING_MAX 10 / ETM_SEQ_HISTORY 8 / MGMT_MAX_CHUNKS 16 / MGMT_RECENT_COUNT 8. Wizard WCB_MAX = 20 (app.js:1081). === THE SPECIFIC REVERSE CHECK REQUESTED: WCB_SPECIAL_PEER_ID as an index — VERIFIED SAFE === Every use is `WCB_SPECIAL_PEER_ID - 1` (WCB.ino:1055, 1652, 1703, 2201, 4882-4884, 6824-6825, 7369) into arrays of dimension MAX_WCB_COUNT = 20. Default 20 -> index 19, in bounds. Both writers clamp: `enableControllerPeer` returns early on `id < 1 || id > MAX_WCB_COUNT` (WCB.ino:6814), the `?CONTROLLER,ON,<id>` handler rejects outside 1-20 (WCB.ino:4868-4871), and `saveSpecialPeerIDToPreferences` rejects outside 1-20 (WCB_Storage.cpp:424-427). ONLY residual gap: `loadSpecialPeerIDFromPreferences` (WCB_Storage.cpp:415-419) reads the NVS value with no clamp, so a corrupt/foreign blob of 0 or >20 would index out of bounds — ALREADY LOGGED as part of docs/CODE_REVIEW_2026-08_wave1.md ("?CONTROLLER,ON,<id> bounds-checks against a literal 20 instead of MAX_WCB_COUNT"), whose suggested fix names this exact function. Not re-reported. === VERIFIED CLEAN — board id validated before use as an index === - ETM receive: senderWCB validated once for all packet types (WCB.ino:3736-3746) AND bound to the source MAC's last octet (:3757-3761). boardTable/etmSeqHistory/etmStats indexing downstream is safe. - etmProcessAck: `senderWCB < 1 || > MAX_WCB_COUNT` return (WCB.ino:1221-1222). - storeReportedStats: `from < 1 || from > MAX_WCB_COUNT` return (WCB.ino:1775-1779) before `reportedStats[from-1]`. - processETMCharAck: boardIdx and mIdx both bounds-checked (WCB.ino:1559-1571). - addActivePeer / addTemporaryPeer / removeActivePeer / wcbPeerOnline / wcbPeerEverSeen: all `id < 1 || id > MAX_WCB_COUNT` guarded (WCB.ino:6902, 6947, 6981, 6868, 6887). - processWCBMessage `;w` target parse (WCB.ino:5983-5996): two-digit targets ARE supported via the comma form, single-digit only in the documented legacy no-comma form; the comment at :5930-5947 is factually correct about the code. Target validated `>= 1 && <= MAX_WCB_COUNT` at :6044. - ?WCB,x and updateWCBNumber both validate 1..MAX_WCB_COUNT (WCB.ino:4767, 5695); saveWCBQuantityPreferences validates (WCB_Storage.cpp:437). - Per-module host validation all uses MAX_WCB_COUNT: WCB_HCR.cpp:555, WCB_MP3.cpp:222, WCB_DFP.cpp:173, WCB_WLED.cpp:291, WCB_Maestro.cpp:487/516/597, WCB_OTA.cpp:337/352/362/476, WCB_RemoteTerm.cpp:87, WCB_Storage.cpp:1249/1264/1324/1717, WCB_PWM.cpp:252/581. - WDP: `wdpOnAdvertReceived` guards senderWCB 1..MAX_WCB_COUNT (WCB_WDP.cpp:489) before `wdpNeighbors[senderWCB-1]`; every TLV decode clamps its length (MAESTRO/MAESTRO_CFG/WLED_CFG to WDP_MAX_*, PORTLABEL to port 1-5, PWMTARGET to pwmSelfCount<5); the TLV walk cannot run off `structCommand[200]` (`o + 2 + len > 200` break at :523). - ESP-NOW peer budget: worst case is Default_WCB_Quantity-1 = 19 unicast (WCB.ino:7349) + 1 broadcast (:7395) = exactly the 20-peer hardware cap; controller and learned/temporary peers all live inside the same 1..20 id space so they cannot push past it. addActivePeer/addTemporaryPeer/otaEnsurePeer are transactional and log a full table. Tight but not overrunnable — no headroom, worth remembering. - WCB_TARGET_RAW_SERIAL 97 and WCB_TARGET_KYBER 98 (WCB_Storage.h:14-15) carry `formerly 8`/`formerly 9` comments; grep confirms NO remaining comparison of a target id against literal 8 or 9 anywhere. That earlier fix is complete. - Wizard board loops are all 1..20 or WCB_MAX: app.js:139, 437, 590, 3151, 3667, 6575, 8956, 9025, 9052, 12539. Wizard/parser.js parses every board number with `\d+` (:620, 665, 692, 720, 758, 793, 961, 1174) — no single-digit regex anywhere. === VERIFIED LEGITIMATELY NOT A BOARD COUNT === WCB.ino:759 `j < 8` (CRC32 bit loop). :1752/:1756/:1770 `v[8]` / `found < 8` (eight ?STATS,RPT fields). :1980/:2958/:5769/:6459 `char[9]` (8 hex CRC + NUL). :657 basicColors[9] (colour table). ETM_PENDING_MAX 10 = concurrent in-flight messages, each holding its own `[MAX_WCB_COUNT]` bitsets (WCB.ino:508-519). ETM_SEQ_HISTORY 8 = per-sender dup ring depth, array is `[MAX_WCB_COUNT][8]`. MGMT_MAX_CHUNKS 16 vs uint16_t receivedMask — `(1 << 16) - 1` promotes to int then truncates to 0xFFFF correctly (:2702, :3080). MGMT_RECENT_COUNT 8 = dedup ring. MAX_MAESTROS_PER_WCB 9 / MAX_WLED_PER_WCB 9 / MAX_KYBER_TARGETS 9 = device-ID slot capacities (ids 1-8, 9=all-local, 0=all), all loops correct and the shared-slot exhaustion caveat is already documented in CLAUDE.md rule 5. WDP_MAX_MAESTRO/WLED 9 = per-board local device ids. WCB_Storage.cpp:1732/1879 and WCB_Storage.h:92 `outputs[10]` = destinations per mapping, not boards. WCB_PWM.cpp `outputs[5]` writes ARE bounded (`mapping.outputCount < 5` in the while condition at :228) — my first read suspected an unbounded write; it is guarded. `sentRemoteWCBs` uint32_t bit set guards `remoteWCB <= 31` (WCB_Maestro.cpp:145). WCB_WDP.cpp:333 `uint8_t p[8]` (5-byte solicit). :937 `char t[8]` (dot + <=3 digits). Wizard app.js:2668 `id < 9` and index.html:699/918 = WLED/Maestro device IDs. - WCB_Storage.cpp:1717 `wcbNum > MAX_WCB_COUNT` has no lower bound, but `wcbNum` is declared `uint8_t` at :1688 so a negative parse wraps to >20 and is rejected. NOT a finding. - wdpBuildPayload TLV order (WCB_WDP.cpp:272-300): PWMTARGET and SEQHASH are deliberately emitted BEFORE the port labels so the functional TLVs win the 200-byte budget; worst-case core payload computes to ~146 B, so PWMTARGET always fits. Comment is accurate. === ALREADY LOGGED IN docs/CODE_REVIEW_2026-08*.md — verified still present, deliberately NOT re-reported === - F-001 S1 `etmCharPhaseSentTimes[3][200]` global overrun (WCB.ino:577, 1470, 1506, 1513): `totalMessages = etmCharMessageCount * peerCount` (:1456) is unbounded against the 200 dimension; default count 20 x 11+ online peers overruns, and the READ side at :1571 is guarded with `mIdx >= 200` while the writes are not. I re-derived this independently and confirm it is real and reachable via the documented `?ETMCHAR`. - clearAllPWMMappings `remotePorts[wcb][remotePortCounts[wcb]++]` unbounded per-board counter (WCB_PWM.cpp:469) — wave2. Re-confirmed: no dedup, no cap, 25 entries can target one board's 5-wide row; on wcb==20 it writes past the whole stack array. Note `wdpLocalPwmTargets` (WCB_WDP.cpp:193-212) does the dedup+cap correctly, so the fix pattern already exists in-tree. - F-028 S2 `?WCB` help states range 1-9 (WCB_Help.cpp:364, :369, :1027 and WCB_Help.h:21-22) while the firmware accepts 1-20 (WCB.ino:4767, :5695) — still unfixed; the adjacent `?WCBQ` help correctly says 1-20 (WCB_Help.cpp:391). - `commandsToSend[10]` / `commandsToReceive[10]` dead + "Includes 1-9 and broadcast" fossil comment (WCB.ino:881-882) — wave1. Confirmed zero other references. - `// [phase 0-2][board index 0-7]` stale above `etmCharBoardResults[3][MAX_WCB_COUNT]` (WCB.ino:575) — wave1. - loadSerialMonitorMappings outputCount read from NVS unclamped into `outputs[10]` (WCB_Storage.cpp:1548-1556) — wave2, twice. - Maestro ID dropdown offering 9 vs `configureMaestro`'s 1-8 reject (Wizard/app.js:3246 vs WCB_Maestro.cpp:578); Kyber parser accepting 9 (WCB_Storage.cpp:1248) — wave3. - exportSystemFile walking only 1..wcbQuantity (Wizard/app.js:8418) — wave3. My finding #1 is the same *shape* in a different function (mapping destinations) and is NOT covered by it. - wcb_pin_map.h:5-6 malformed include guard (`#ifndef wcb_pin_map.h` / `#define wcb_pin_map.h` — tokenises as macro `wcb_pin_map` with body `.h`) — F-003. === NOT PURSUED / OUT OF THIS SWEEP === CI workflows, NVS key-length (15 char) audit, packet-size routing table, WcbCmd translator bytes, tests/wdp_wire_test.cpp. `sendESPNowRawToPort` (WCB.ino:2453) and `sendESPNowRawToSpecificWCB` (:2416) have zero callers — dead code, unindexed-target risk is therefore unreachable; not reported (wave findings already cover dead code broadly). `baudRates[kyberTargets[i].targetPort - 1]` at WCB_Storage.cpp:2154/2188 and `serialPortLabels[... - 1]` at :2195 lack the `targetPort >= 1 && <= 5` guard that the equivalent backup emitter at WCB.ino:2846-2848 HAS; only reachable if NVS yields an enabled target with port 0 (`loadKyberTargets` WCB_Storage.cpp defaults port to 0 without clamping), which no current writer produces. Recorded here rather than reported.

- SWEEP: every Preferences namespace and key in Code/WCB (excluding src/ vendored libs). Method: grepped all `.begin(`, `put*(`, `get*(`, `remove(`, `isKey(`, `clear()` (211 accessor call sites), then machine-built a (namespace, key, type, file:line) table with awk to catch type mixes and over-length literals, then hand-read every computed-key construction site. === NAMESPACES: 27, ALL <=15 CHARS — VERIFIED CLEAN === bcast_block(11) bdcst_set(9) command_config(14) dfp_cfg(7) espnow_config(13) etm_config(10) hcr_cfg(7) hw_version(10) kyber_settings(14) kyber_targets(13) learned_peers(13) led_config(10) mac_config(10) maestro_cfg(11) mp3_cfg(7) pwm_mappings(12) pwm_outputs(11) serial_baud(11) serial_labels(13) serial_map(10) serial_monitor(14) stored_cmds(11) stored_commands(15) wcb_config(10) wcb_vars(8) wdp_cfg(7) wled_cfg(8). Note `stored_commands` is exactly 15 — legal but at the hard ceiling. The console messages in resetBroadcastSettingsNamespace (WCB_Storage.cpp:353, :360) still say "broadcast_settings", an 18-char name that could never have opened — harmless, but misleading if someone greps for it. === LITERAL KEYS: ALL <=15 — VERIFIED CLEAN === Longest is `special_peer_id` (wcb_config) at EXACTLY 15 — WCB_Storage.cpp:417/:429. Any future rename/suffix breaks it silently. Next longest: etmCharCount/etmCharDelay/mirror_kyber/mesh_channel/seq_mig_done/special_peer/wcb_quantity (12). === COMPUTED KEYS: maxima hand-derived from each domain — ALL <=8 EXCEPT ONE === bcast_block "blk"+1..5 =4 · bdcst_set "S"+1..5 =2 · kyber_targets "kt"+0..8+"_port" =8 (MAX_KYBER_TARGETS 9, WCB_Storage.h:104) · maestro_cfg "m"+0..8+"_baud" =7 (MAX_MAESTROS_PER_WCB 9, WCB_Maestro.h:14) · pwm_mappings "map"+0..9 =4 (MAX_PWM_MAPPINGS 10, WCB_PWM.h:6) · pwm_outputs "port"/"auto"+0..4 =5 · serial_baud "Serial"+1..5 =7 (port guarded 1-5 at WCB_Storage.cpp:151) · serial_labels "label"+1..5 =6 (guarded :1500) · serial_map "sm"+0..4+"_act|_in|_cnt|_raw" =6 and "sm"+i+"_"+j+"w|p" =6 · serial_monitor "mon"+1..5 =4 · stored_cmds/stored_commands "CMD"+1..80 =5. **The ONLY unbounded key in the firmware is the user-supplied stored-sequence key** (WCB_Storage.cpp:632) — reported. === TYPE CONSISTENCY — VERIFIED CLEAN === Machine-checked every (namespace, key) pair for a put/get type mix across all files: ZERO mismatches. The `port`/`baud`/`en`/`rwcb` key names repeat across dfp_cfg/hcr_cfg/mp3_cfg/wled_cfg but are namespace-isolated and consistently typed. wled_cfg's legacy bare `port`/`baud`/`en` are read with the same types the old scheme wrote and purged on every save (WCB_WLED.cpp:465-467). === begin()/end() PAIRING — VERIFIED CLEAN === Counts balance per file (WCB.ino 10/10, WCB_Storage.cpp 87/87, WCB_PWM 7/7, WDP 2/2, WLED 2/2, HCR 2/2, MP3 2/2, DFP 2/2, Variables 2/2 on varPrefs). Scripted every `return`/`continue`/`break` inside a begin..end window: all hits are loop control inside the window (WCB_PWM.cpp:546/549/591, WCB_Storage.cpp:687) or a `return` on a begin() that FAILED (WCB_Variables.cpp:87, :102 — nothing to close). Scripted every function call inside a begin..end window: only canUsePWMOnPort (WCB_PWM.cpp:55), attachPWMInterrupt (:135) and configureRemotePWMOutput (:742) — I read all three; none touches Preferences. No leaked handles on any path. === READ-ONLY OPEN THEN WRITE — VERIFIED CLEAN === Every `begin(ns, true)` site does gets only. The two near-misses are correct: loadPWMOutputPortsFromPreferences closes at WCB_PWM.cpp:917 BEFORE the cleanup save at :920; loadWLEDSettings closes at WCB_WLED.cpp:490 before the legacy-migration `saveWLEDSettings()` at :496. === CROSS-TASK NVS ACCESS — VERIFIED CLEAN; THIS REFUTES F-112 === F-112 (wave2) claims saveWLEDSettings/loadWLEDSettings race on the shared global `preferences` "across two FreeRTOS tasks". I traced every task and the premise does not hold. serialCommandTask (WCB.ino:6595) only calls processIncomingSerial → wdpDaHandleLine (WCB_WDP.cpp:824-849, no NVS) or processSerialCommandHelper → parseCommandsAndEnqueue/parseCommandGroups, which ONLY enqueue (verified: the only calls in parseCommandsAndEnqueue's body are enqueueCommand at WCB.ino:2032 and :2068). The ESP-NOW receive callback defers everything — enqueueMgmtReq (:3638, :3645), enqueueOtaPacket (:3664, :3668), enqueueWdpPacket (:986) — and the corresponding drains all run in loop() (:7492-7494). KyberLocal/KyberRemote/PWM/RawSerialForwarding/identify task bodies contain zero `save*`/`preferences.` references (scripted over WCB.ino:6575-6720 and the callee bodies). All NVS access is confined to setup() and loop(). The shared global is a latent hazard if any future work moves an NVS write into a task, but it is not live today. === LOAD-AT-BOOT COVERAGE — VERIFIED CLEAN === Cross-referenced every load*/migrate* function against setup(). All present: WCB.ino:7138-7164 (HW, LED pin, Maestro, MP3, DFP, HCR, WLED, variables, WDP via wdpBegin, Kyber, PWM via initPWM at :7151 → WCB_PWM.cpp:131-132, Kyber targets, WCB number, alias, quantity, mesh channel, special peer, MAC), :7214-7220 (migrate, baud, labels, monitor settings, bcast block, monitor mappings, broadcast settings), :7332 (ESP-NOW password), :7411-7413 (delimiter, func/cmd char, ETM). Ordering is correct where it matters — loadKyberSettings (:7150) and the HCR/MP3/WLED loads precede initPWM (:7151), which is what canUsePWMOnPort depends on. === OTHER SPOT CHECKS THAT CAME BACK CLEAN === · espnowPassword: char[40] (WCB.ino:148); BOTH write paths bound it below sizeof (WCB.ino:4755 comma form, :5704 legacy form), and every struct's structPassword is also [40] (WCB.ino:205, 778, 787, 798, 817, 826) — so no NVS-longer-than-RAM divergence and no truncated-compare. The `getString(key, buf, maxLen)` overload is bounds-checked in the core (Preferences.cpp:476-496) and leaves the buffer untouched on miss, so the "use the external espnowPassword as the default" comment at WCB_Storage.cpp:484 is accurate. · learned_peers: 32-bit mask for MAX_WCB_COUNT 20 (WCB.ino:7043-7051) — no 8/9-board cap, MAC-fingerprinted, correctly invalidated (:7069-7073). · WDP auto-add NVS writes are all idempotent — maestroAutoAddRemote (WCB_Maestro.cpp:728-737), wledAutoAddRemote (WCB_WLED.cpp:424-435), hcr/dfpAutoAddRemote (WCB_HCR.cpp:769-777, WCB_DFP.cpp:429-437) write only on a real change, so no per-advert flash wear. · WCB_Variables stores all variables in ONE `blob` key via its OWN Preferences object (WCB_Variables.cpp:15, :89, :104) — deliberately immune to both the key-length and the shared-object problems, and the only place in the firmware that checks a put* return (:89-92). · Wizard factory-reset NVS image is 0xFF-filled and exactly 0x5000 @ 0x9000 (Wizard/flasher.js:632-634), matching min_spiffs.csv — correct, and notably a SUPERSET of what `?ERASE,NVS` clears. === OVERLAP WITH THE EXISTING REVIEW LOG === docs/CODE_REVIEW_2026-08_wave2.md already logs three of my seven as UNVERIFIED: F-042/F-061/F-062 (dfp_cfg), F-058/F-059 (15-char key), F-060 (seq_mig_done clear). I re-derived all three independently from the namespace enumeration and hand-verified them against the pinned esp32@3.3.4 core source and IDF nvs.h — per the log's own "hand-verify the S1/S2s" plan, treat them as CONFIRMED now, not leads. My findings 4, 5, 6 and 7 (unreachable eraseNVSFlash tail + fleet reboot; hw_version cleared by factory reset; no reserved-key guard; Wizard push-path key length) are NEW and appear in none of wave1/wave2/wave3. F-126 (dead serial_monitor namespace) and F-127 (MAX_STORED_COMMANDS not enforced) I confirmed as accurate while sweeping; F-112 I refute above.

- ## What I enumerated **Full dispatch-order enumeration built first, then judged. Line numbers are 1-indexed against branch `OTA` @ 6c0f24b.** ### 1. `handleSingleCommand` (WCB.ino:4354-4391) — top-level prefix router Order: `WCB_WEBTOOL_CONFIG_PULL` (exact, :4373) → `startsWith(LocalFunctionIdentifier)` (:4380) → `startsWith(CommandCharacter)` (:4384) → broadcast fallback (:4389). **DEFECT FOUND** — LFI is tested first and is settable to `;` (finding #2). Otherwise structurally sound. ### 2. `processLocalCommand` (WCB.ino:4394-5355) — two sections **(a) New-format section, :4423-5143.** 36 root verbs, ALL tested with exact `rootUpper == "…"` after a comma split, each with its own `return`. Enumerated in order: DEBUG, MAP, BAUD, LABEL, BCAST, KYBER, ETM, MAC, EPASS, WCB, WCBQ, WCBCH, PEERSLIVE, OTALOCAL, OTA, ALIAS, CONTROLLER|SPECIAL, MAESTRO, MP3, DFP, HCR, WLED, WDP, VAR, SEQ, STATS, TRACK, DELIM, FUNCCHAR, CMDCHAR, ERASE, IDENTIFY, MGMT, RTERM, HW, LED. **VERIFIED CLEAN** — exact equality means no prefix shadowing is possible here. Specifically checked and cleared: `OTALOCAL` is tested before `OTA` (:4812 vs :4826) and both are exact; `WCB`/`WCBQ`/`WCBCH` (:4765/:4776/:4790) cannot shadow each other; `CONTROLLER`/`SPECIAL` share one block; `MGMT` sub-verbs `SEQ` vs `SEQGET` (WCB.ino:2547/:2551) are both exact `==` so no collision. **VERIFIED CLEAN — empty-argument safety of the setters** (I suspected a bug and traced each one): `?HW` → `saveHWversion(0)` is rejected by an explicit validator (`WCB_Storage.cpp` `saveHWversion`, which validates *before* the NVS write and says so in its comment); `?WCB` → rejected at :4767; `?WCBQ` → `saveWCBQuantityPreferences` rejects <1; `?WCBCH` → rejected at :4792; `?LED`/`?ALIAS`/`?TRACK`/`?MAC`/`?ERASE` all print status or usage. **No setter silently applies 0.** Do not re-raise this. **(b) Legacy chain, :5150-5350.** 70 literals. I extracted them programmatically in dispatch order and ran an exhaustive pairwise prefix test (earlier `startsWith` literal vs every later literal). **RESULT: ZERO shadow pairs.** The chain is correctly ordered longest-first everywhere it matters — `cclear` before `cc`; `wcb_erase`, `wcbq` before `wcb`; `maestro_list`/`maestro_clear`/`maestro_default`/`maestro_remote` before `maestro`; `smclear`/`smlist`/`smr` before `sm`; `pos` before `po`; `statsreset` before `stats`; `etmcharcount`/`etmchardelay` before `etmchar`; `track_all_on` vs `track_all_off` (neither is a prefix of the other); and the `s`/`S` catch-all is last. **This is the single most valuable clean result of the sweep — the hypothesis in the brief does not hold for this chain, and a later session should not redo it.** Residual (not reported, low value): the whole legacy chain is all-lower-or-all-upper only, so `?Wcb2`/`?Cclear` fall through, while the new-format section uppercases and accepts `?Wcb,2`. Systemic, consistent, visible ("Unknown command"). ### 3. `processCommandCharcter` (WCB.ino:5847-5875) — the `;` runtime family Order: `s|S` (with an `charAt(1) != 'e'/'E'` escape hatch) → `w|W` → `c|C|SEQ|seq` → `m|M` → `p|P` → `a|A` → `d|D` → `h|H` → `l|L` → `v|V`. **VERIFIED CLEAN on first-character collisions** — all ten branches key on distinct first characters; `;VP` (persistent variable) starts with 'V' not 'P' so branch 5 cannot steal it; the `;S` 'e'-guard is safe because serial ports are digits 0-5, so no real `;S<port>` command can have 'e' at index 1. **DEFECTS FOUND:** `;SEQ` case-sensitivity (finding #9); `;S`'s missing comma strip (finding #1); `;P`'s missing comma strip (finding #5). **VERIFIED CLEAN — argument-boundary handling of the sibling verbs:** `;A` (`WCB_MP3.cpp:98`), `;D` (`WCB_DFP.cpp:74`), `;L` (`WCB_WLED.cpp:122`) each strip an optional leading comma; `;H` (`processHCRRuntimeCommand`) strips `"H,"` vs `"H"` explicitly; `;V`/`;VP` (`WCB_Variables.cpp:287-293`) require the comma and slice past it; `;M` (`WCB.ino:6128-6185`) handles both the no-comma and comma spellings deliberately, with `;M!` and `;MG` wire forms tested first and unreachable by a digit-addressed device command. `;w` (`WCB.ino:5915-6083`) — the digit-run/comma/alias parse is heavily commented and traced correct. ### 4. Subsystem entry points (`grep startsWith Code/WCB/*.cpp` — all 51 hits reviewed) - **`processWdpCommand`** (`WCB_WDP.cpp:1201-1276`): `""|LIST`, `STATUS`, `DUMP`, `DA`, `POLL`, `ON`, `OFF` all exact; then `startsWith` on `AUTOJOIN`, `ADD`, `FORGET`, `CLEAR`(exact), `DETAIL`; numeric fallback last. **CLEAN** — `DA` (exact) precedes `DETAIL` (startsWith) with no overlap; no literal is a prefix of another. - **`configureHCR`** (`WCB_HCR.cpp:524-650`): `LIST`/`CLEAR`/`STATUS`/`REFRESH` exact, then `REMOTE`, `POLL`, `GET,`, `PORT,`. **CLEAN.** - **`configureWLED`** (`WCB_WLED.cpp:229-…`): `LIST`/`STATUS`/`CLEAR` exact before `CLEAR,` startsWith — correct order. **CLEAN.** - **`configureMP3` / `configureDFP`**: `REMOTE`, `ONERR,`, `S…`. **CLEAN for ordering** (the untrimmed-slice bug in `ONERR` is already logged in wave 2, F-…). - **`processVarConfig`** (`WCB_Variables.cpp:284-336`): `LIST` exact, then `SET`/`GET`/`CLEAR` startsWith — no prefix relation. **CLEAN.** - **`addPWMMapping`** (`WCB_PWM.cpp:170`), **`addSerialMonitorMapping`** (`WCB_Storage.cpp:1595`), **`removeSerialMonitorMapping`** (`:1859`), **`storeKyberSettings`** (`:1071`), **`configureMaestro`** (`WCB_Maestro.cpp:531`): all single-shape parsers, no verb chain. Traced each legacy dispatch's `substring()` offset against the callee's expectation — `?SMSx`→`substring(2)`✓, `?SMRSx`→`substring(3)`✓, `?POSx`→`substring(3)`✓, `?PRSx`→`substring(3)`✓, `?PO`/`?PX`→`substring(2)`✓, `?SLCSx`→`substring(4)`✓. **Only `?PMSSx` mismatches** (finding #6). (`?SLSx` and `?EPASS` offset bugs are already in wave 1 — not re-reported.) - **`WCB_OTA.cpp`** `processOtaLocalCommand`/`processOtaRelayCommand`: sub-verbs are exact `sub == "…"` on an uppercased token. **CLEAN.** Confirmed base64 payloads cannot contain `^` or `;`, so chain splitting and `isTimerCommand` cannot corrupt an OTA DATA frame. - **`WCB_RemoteTerm.cpp`**: no text-command dispatch (packet-level only). **N/A.** ### 5. `processSerialCommandHelper` (WCB.ino:6423-6496) and the mesh receive paths Order: `checkForTimerStopRequest` (exact `equalsIgnoreCase(funcChar+"STOP")`, `command_timer.cpp:62`) → the `?C` checksum branch (:6436) → timer-group parse gated on `!startsWith(funcChar)` (:6488) → `parseCommandsAndEnqueue`. **DEFECT FOUND** in the `?C` branch (finding #7). `espNowReceiveCallback` ETM path (:3893-4000) and normal path (:4175-4229) mirror the same dispatch. **VERIFIED CLEAN**: `ETMCHAR_`/`ETMLOAD` are handled only on the normal path (:4175/:4183) and deliberately swallowed on the ETM path (:3893) — correct, because `ETMCHAR_*` is sent with `useETM=true` purely as an ACK target and `ETMLOAD` is sent with `useETM=false` (:1495), so each lands on the path that handles it. `processETMCharAck`'s `ETMCHAR_P1/P2/P3` tests (:1563-1565) match the `"ETMCHAR_P" + String(etmCharPhase)` payload with `etmCharPhase` 1-based from `startETMChar` (:1421). No collision. `isTimerCommand` (`command_timer.cpp:49-52`) DOES use `indexOf(";t") != -1` rather than a prefix test — **not reported**, because the adjacent comment at WCB.ino:6483-6490 documents the containment semantics as deliberate and correctly explains the `?`-prefix exemption. ### 6. Verbs handled in two places — all enumerated and traced `?HW` (new :5121 / legacy :5243) ✓ both correct · `?WCB` (:4765 / :5212) ✓ · `?WCBQ` (:4776 / :5210) ✓ · `?EPASS` (:4750 / :5214) — legacy half already known-broken (wave 1) · `?ETM,*` (:4664 / :5302-5334) ✓ · `?MAESTRO,REMOTE` = `?KYBER,REMOTE` = legacy `maestro_remote` — all three funnel to `storeKyberSettings("remote")` ✓ · `?STATS` (:5003 / :5290-5292) ✓ · `?TRACK` (:5018 / :5294-5300) ✓ · `?SEQ,SAVE` vs `?CS` — **DEFECT** (finding #8) · `?SEQ,CLEAR` vs `?CE` ✓ · `?BCAST,OUT/IN` vs `?SBO`/`?SBI` ✓ (the `indexOf("ON")`/`indexOf("OFF")` containment tests at :5551-5553 and :5600-5602 are safe — "OFF" contains no "ON" and the ordering test handles both) · `?CHK` verify (:1953 / :6440 / dead `verifyBackupChecksum` :5792) — **DEFECTS** (findings #4, #7; the dead function itself is already in wave 1) · `?STOP` (only `command_timer.cpp:62`, in-chain behaviour already in wave 2). ### 7. Deliberately NOT reported (checked, judged out of scope or already covered) - Sequence keys >15 chars silently failing the NVS write — real, but already `CODE_REVIEW_2026-08_wave2.md:569` and `:601`. - `?SLSx` dead, `?EPASS` off-by-one, `updateSerialSettings`'s unreachable `baud` branch, orphaned `clearSerialLabelCommand`/`verifyBackupChecksum`, `?PX`/`?REBOOT` hardcoding `'?'` in `WCB_PWM.cpp` — all already in wave 1/2. - `?FUNCCHAR,?` alone — already `CODE_REVIEW_2026-08.md:258-274`; my finding #3 reports the *uncovered* extent (`?LF?`, `?CC?`, `?D?`, `?CMDCHAR,?`, and any argument ending in `?`), and note that `CODE_REVIEW_2026-08_wave3.md:112` **refutes it incorrectly** — that entry claims `xFUNCCHAR,?` reaches WCB.ino:5045, which it cannot, because `processLocalCommand:4402` returns first. - `;Sxyz` with a non-digit port silently printing to USB (`processSerialMessage:5887`, `"x".toInt()` → 0 → USB branch). Cosmetic; no data loss. - `isIfChainToken` (`WCB_Variables.cpp:372`) treating any token beginning `IF,` as a gate, including a raw broadcast word. No realistic device emits it.
