# WCB Code Review — FIX TRACKER

Work list for fixing the verified findings. Ordered by **verified** severity (not the
reviewer's claimed severity). Companion to [CODE_REVIEW_2026-08.md](CODE_REVIEW_2026-08.md);
full reasoning per item is in [_verdicts.md](CODE_REVIEW_2026-08_verdicts.md).

> **This will cross sessions.** Update the Status column *in place* as each item is done.
> Never renumber. `FIX-nnn` ids are stable.

**Status values:** `TODO` · `DONE` · `PARTIAL` · `SKIP` (with reason) · `BLOCKED` (with reason)

> ⚠ **51 % of the reviewers' proposed fixes were wrong.** Each item below links to a verdict
> that says whether the filed fix works. Where it does not, the verdict carries a corrected
> one. **Read the verdict before editing.** Items flagged ⚠FIX are the ones whose original
> remedy was rejected.

## Verification / build gate

After each batch of edits:

```bash
# both firmware targets, isolated build path (never share the IDE cache)
arduino-cli compile --fqbn esp32:esp32:esp32:PartitionScheme=min_spiffs   --build-path <tmp>/bp32 Code/WCB
arduino-cli compile --fqbn esp32:esp32:esp32s3:PartitionScheme=min_spiffs --build-path <tmp>/bps3 Code/WCB
# WDP host test
g++ -std=c++11 -Wall -Wextra -O2 tests/wdp_wire_test.cpp -o <tmp>/wdp && <tmp>/wdp
# Wizard (no build step — a syntax slip silently breaks all event wiring)
node --check Wizard/app.js && node ~/tools/jscheck.js Wizard/index.html
```

Baseline before any fixes: both targets exit 0 (ESP32 65 % flash / 26 % RAM, S3 63 % / 25 %),
WDP test 9/9 pass, all Wizard JS parses. Only warnings are the `wcb_pin_map.h` include guard
and the `volatile` increment — both are themselves findings.


---

## S1 — 5 items

| # | Status | File:line | ⚠ | Finding |
|---|---|---|---|---|
| FIX-001 | **DONE** | `Code/WCB/WCB.ino:577` |  | etmCharPhaseSentTimes is sized [3][200] but is indexed by etmCharMessageCount x peerCount — a fleet of 11+ online boards overruns it |
| FIX-002 | **DONE** | `Code/WCB/WCB.ino:1470` |  | etmCharPhaseSentTimes[3][200] is indexed by etmCharMessageCount × peerCount with no bound check — global out-of-bounds write |
| FIX-003 | **DONE** | `Code/WCB/WCB.ino:2617` | ⚠FIX | handleMgmtForward silently truncates every MGMT FRAG payload to 179 chars instead of rejecting or re-fragmenting |
| FIX-004 | **DONE** | `Wizard/app.js:4455` | ⚠FIX | updateSequence sends ?SEQ,SAVE as a single un-fragmented MGMT chunk — relay silently truncates it to 179 chars |
| FIX-005 | **DONE** | `Wizard/app.js:7852` |  | Remote config push fragments at 180 chars, but the relay only forwards 179 — one character is silently dropped from every full chunk |

---

## S2 — 36 items

| # | Status | File:line | ⚠ | Finding |
|---|---|---|---|---|
| FIX-006 | **DONE** | `Code/WCB/command_timer.cpp:203` | ⚠FIX | Use-after-free: processCommandGroups() holds references into commandGroups across a vTaskDelay() that serialCommandTask can clear |
| FIX-007 | **DONE** | `Code/WCB/WCB_Storage.cpp:352` |  | ?BCAST,RESET does not reset broadcast INPUT blocking at all and leaves RAM stale, so the next unrelated save re-persists the pre-reset values |
| FIX-008 | **DONE** | `Code/WCB/WCB_Storage.cpp:834` | ⚠FIX | The legacy stored_commands namespace is never cleared and the migration guard lives inside the namespace that IS cleared — ?SEQ,CLEAR,ALL or ?ERASE,NV |
| FIX-009 | **DONE** | `Code/WCB/WCB_Storage.cpp:1024` | ⚠FIX | ?ERASE,NVS never clears the dfp_cfg namespace — the DFPlayer config and its port claim survive a factory reset, and the adjacent WCB_DFP comment claim |
| FIX-010 | **DONE** | `Code/WCB/WCB_Storage.cpp:1078` | ⚠FIX | ?KYBER,CLEAR is emitted in every non-Kyber board's config chain and always resets Serial2's baud + broadcast flags, silently overwriting settings the  |
| FIX-011 | **DONE** | `Code/WCB/WCB.ino:398` | ⚠FIX | Pending-timer-chain queue only closes the WiFi-task half of the commandGroups race — the local-serial path still mutates the vector from serialCommand |
| FIX-012 | **DONE** | `Code/WCB/WCB.ino:1569` |  | processETMCharAck silently discards ACKs for message index >= 200, and the corresponding sentTime writes are unbounded |
| FIX-013 | **DONE** | `Code/WCB/WCB.ino:1585` | ⚠FIX | Phase 3 "Loaded Network" is never loaded — the ETMLOAD trigger is sent non-ETM and is dropped by every receiver's ETM-mismatch guard, making processET |
| FIX-014 | **DONE** | `Code/WCB/WCB.ino:1892` | ⚠FIX | applyLiveBaud tears down a live SoftwareSerial (end() then begin()) while other tasks may be reading it — end() leaves m_rxValid true with a freed buf |
| FIX-015 | TODO | `Code/WCB/WCB.ino:2053` | ⚠FIX | Factory-reset backup chain: tokens after ?FUNCCHAR carry the NEW func char, but the ?SEQ,SAVE / ?CS / ?MGMT splitter matches on the RECEIVING board's  |
| FIX-016 | **DONE** | `Code/WCB/WCB.ino:2298` | ⚠FIX | ETM checksum mode silently destroys the CRC for commands over 187 chars — target ACKs, then discards |
| FIX-017 | **DONE** | `Code/WCB/WCB.ino:3706` |  | ?ETM,CHAR phase 3 never loads the network: the ETMLOAD trigger is sent non-ETM and is dropped by the ETM-mismatch filter on every peer |
| FIX-018 | TODO | `Code/WCB/WCB.ino:4130` |  | Serial3/4/5 (ESPSoftwareSerial) are written from the WiFi callback and three other tasks with no mutual exclusion; the adjacent "write() drains async" |
| FIX-019 | TODO | `Code/WCB/WCB.ino:4159` | ⚠FIX | The WiFi task rewrites lastReceivedViaESPNOW mid-dispatch, so loop()'s per-item snapshot restore does not survive to the point it is read |
| FIX-020 | **DONE** | `Code/WCB/WCB.ino:4269` | ⚠FIX | forwardDataFromKyber writes each Kyber byte once PER TARGET, so two Maestros sharing one serial port receive every byte twice |
| FIX-021 | **DONE** | `Code/WCB/WCB.ino:5703` |  | updateESPNowPassword() strips 6 chars off a 5-char "EPASS" prefix — the legacy ?EPASS form silently drops the first password character |
| FIX-022 | TODO | `Code/WCB/WCB.ino:6193` | ⚠FIX | processPWMOutput will pinMode/drive ANY serial port's TX pin — it never checks the port is a declared PWM output, defeating canUsePWMOnPort's stated g |
| FIX-023 | **DONE** | `Code/WCB/WCB.ino:6490` | ⚠FIX | processSerialCommandHelper calls parseCommandGroups/stopTimerSequence from serialCommandTask, racing loop()'s live reference into the commandGroups ve |
| FIX-024 | **DONE** | `Code/WCB/WCB.ino:6598` | ⚠FIX | serialCommandTask parses timer chains off the loop task, racing loop()'s live iteration of the commandGroups std::vector |
| FIX-025 | **DONE** | `Wizard/app.js:1863` |  | Post-OTA ETM-edge replay fires every deferred remote pull in parallel on one relay |
| FIX-026 | TODO | `Wizard/app.js:3700` | ⚠FIX | Bidirectional-mapping code indexes slot-keyed maps (boardConfigs / boardConnections / boardPull) with a WCB number |
| FIX-027 | **DONE** | `Wizard/app.js:3784` | ⚠FIX | Wizard mapping-destination WCB dropdown is built from the WCBQ floor, so any destination above it is silently rewritten to "Local" |
| FIX-028 | **DONE** | `Wizard/app.js:3837` |  | `?MAP,SERIAL` appends destinations in firmware, so editing or removing a destination in the Wizard leaves the old one live |
| FIX-029 | TODO | `Wizard/app.js:7039` |  | Push All throws a TypeError on a MgmtRelay slot and silently aborts the remaining stages |
| FIX-030 | TODO | `Wizard/app.js:7447` | ⚠FIX | Changing Local Function Char back to '?' is never sent — the whole push is then mis-dispatched to the broadcast path (and out over ESP-NOW) |
| FIX-031 | TODO | `Wizard/app.js:7481` |  | A push containing a PWM input mapping makes the firmware auto-reboot mid-push; every command after it is silently lost |
| FIX-032 | TODO | `Wizard/app.js:7811` | ⚠FIX | boardGoRemote has no funcChar/delimiter/cmdChar bootstrap — changing a command character over a relay sends a payload the target cannot parse |
| FIX-033 | TODO | `Wizard/app.js:7853` |  | boardGoRemote never checks the 16-chunk MGMT ceiling — an oversized config is rejected wholesale by the relay while the Wizard reports success |
| FIX-034 | **DONE** | `Wizard/app.js:7938` |  | Remote-pull listener is not target-filtered — a config reply is written to every board with a pull in flight |
| FIX-035 | **DONE** | `Wizard/app.js:7946` |  | remoteBoardPull accepts ANY [MGMT:CONFIG,<n>] reply — two overlapping pulls write one board's config onto another board's slot |
| FIX-036 | **DONE** | `Wizard/app.js:8419` | ⚠FIX | exportSystemFile only walks slots 1..wcbQuantity — any board discovered or relayed at a higher number is silently dropped from the exported file |
| FIX-037 | TODO | `Wizard/app.js:11701` | ⚠FIX | A stale boardBaselines[n] satisfies the watcher's readiness gate — the push starts ~500 ms after port open and races the scheduled pull |
| FIX-038 | TODO | `Wizard/parser.js:1355` | ⚠FIX | `?KYBER,CLEAR` is sent with no port, so the firmware resets Serial 2 to 9600 and re-enables its broadcast — undoing settings from earlier in the same  |
| FIX-039 | **DONE** | `Wizard/parser.js:1531` |  | Stored sequences are pushed AFTER the PWM mapping command that reboots the board — they are silently discarded |
| FIX-040 | **DONE** | `Wizard/serial-hub.js:114` | ⚠FIX | `_leaderPortOpen` latches true forever — `portOpen` lies, so the Wizard shows "Connected" on a dead share and drops every send |
| FIX-041 | **DONE** | `Wizard/serial-hub.js:356` |  | Wizard/serial-hub.js is missing two lock-squat fixes that NaviCore's required-lockstep copy has |

---

## S3 — 78 items

| # | Status | File:line | ⚠ | Finding |
|---|---|---|---|---|
| FIX-042 | TODO | `c:/Users/ghulette/Documents/GitHub/WCBClient/src/WCB_Client.cpp:2007` |  | WCB_Client decodes a WDP SOLICIT as an advert, wiping the sending board's neighbor record |
| FIX-043 | **DONE** | `Code/bin/build.sh:181` | ⚠FIX | build.sh deletes the committed firmware binaries and exits 0 when the branch name contains a "/" — CI then commits and pushes the deletion as a green  |
| FIX-044 | **DONE** | `Code/WCB/WCB_Help.cpp:132` |  | ?BAUD help documents the legacy form `?Sx,rate`, which the parser rejects (it needs no comma) |
| FIX-045 | **DONE** | `Code/WCB/WCB_Help.cpp:669` | ⚠FIX | ?WLED? help documents only the legacy single-device form — the canonical ID-addressed config and ;L<id> addressing are undocumented |
| FIX-046 | **DONE** | `Code/WCB/WCB_Help.cpp:925` |  | ?CMDCHAR? documents `;Px,width` but the parser rejects the comma, and the rejection is silent |
| FIX-047 | **DONE** | `Code/WCB/WCB_Storage.cpp:643` |  | ?SEQ,SAVE accepts a key longer than the 15-char NVS limit — value silently discarded but the name is recorded |
| FIX-048 | **DONE** | `Code/WCB/WCB_Storage.cpp:1028` | ⚠FIX | eraseNVSFlash() never reaches its own confirmation or restart, and ?ERASE,NVS silently reboots OTHER boards in the fleet |
| FIX-049 | **DONE** | `Code/WCB/WCB_Storage.cpp:1636` | ⚠FIX | ?MAP,SERIAL appends destinations rather than replacing them, so a destination removed or changed in the Wizard stays live on the board |
| FIX-050 | **DONE** | `Code/WCB/WCB_Storage.cpp:1663` |  | Toggling raw mode on an existing serial mapping changes it in RAM but is never persisted — it silently reverts on reboot |
| FIX-051 | **DONE** | `Code/WCB/WCB_Storage.cpp:2229` |  | etmMissedHeartbeats initializer of 5 is overwritten at boot by an NVS fallback of 3, so the documented 55 s offline window is never in effect |
| FIX-052 | **DONE** | `Code/WCB/WCB.ino:267` |  | etmMissedHeartbeats default 5 is undone at boot by loadETMSettings()'s NVS fallback of 3 — the documented 55 s offline window never applies to a fresh |
| FIX-053 | **DONE** | `Code/WCB/WCB.ino:1011` | ⚠FIX | scheduleNextHeartbeat computes a wrapped negative interval when etmHeartbeatSec <= 0, causing a heartbeat broadcast every loop iteration |
| FIX-054 | **DONE** | `Code/WCB/WCB.ino:1396` |  | etmSendAck sends the ACK without ensuring the ESP-NOW peer exists, so ACKs to unregistered senders are silently dropped |
| FIX-055 | **DONE** | `Code/WCB/WCB.ino:1473` | ⚠FIX | ETM characterization phase 2 is labelled "Broadcast (no load)" but sends unicast — the broadcast measurement never happens |
| FIX-056 | **DONE** | `Code/WCB/WCB.ino:1798` | ⚠FIX | ETM characterization phase 2 sends unicast, but the code comment and the results table both report it as "Broadcast" |
| FIX-057 | TODO | `Code/WCB/WCB.ino:1920` | ⚠FIX | enqueueCommand snapshots lastReceivedViaESPNOW/inSequenceBody from unsynchronised globals while running on the WiFi (Core 0) task |
| FIX-058 | **DONE** | `Code/WCB/WCB.ino:1953` |  | Backup checksum is written with the live function identifier but both verifiers hardcode "?CHK" — a custom-funcChar board's own backup can never be ve |
| FIX-059 | TODO | `Code/WCB/WCB.ino:1989` |  | parseCommandsAndEnqueue recurses into itself and re-enters the ?CHK branch, re-introducing the embedded-checksum abort its own comment says lastIndexO |
| FIX-060 | TODO | `Code/WCB/WCB.ino:2599` |  | MGMT single-chunk relay prepends '\x01' but the non-ETM receive path never strips it — all single-chunk Wizard commands die when ETM is off |
| FIX-061 | **DONE** | `Code/WCB/WCB.ino:3002` | ⚠FIX | handleConfigReqPacket silently sends NOTHING when the config exceeds 2912 chars — reachable with ~80 stored variables alone |
| FIX-062 | TODO | `Code/WCB/WCB.ino:3088` | ⚠FIX | Config/stats/seq/ETM fragment reassembly does multi-KB String building and a blocking Serial.printf inside the ESP-NOW receive callback |
| FIX-063 | **DONE** | `Code/WCB/WCB.ino:3099` | ⚠FIX | sendResultFrags silently drops any relayed result over 2912 chars — ?MGMT,STATS and ?MGMT,ETM both exceed it on a full 20-board mesh |
| FIX-064 | **DONE** | `Code/WCB/WCB.ino:3245` |  | etmCharRelayRequesterWCB latches when startETMChar()/processETMChar() bail — a later LOCAL ?ETM,CHAR frag-sends its results to a board that never aske |
| FIX-065 | **DONE** | `Code/WCB/WCB.ino:3831` | ⚠FIX | etmSeqHistory is never cleared when a peer reboots, so the first commands from a restarted board are ACKed but silently not executed |
| FIX-066 | TODO | `Code/WCB/WCB.ino:3983` | ⚠FIX | ?WHOAMI reply is sent as a 249-byte non-ETM packet that WCB_Client (NaviCore) routes to the raw hook and never delivers |
| FIX-067 | **DONE** | `Code/WCB/WCB.ino:4342` | ⚠FIX | forwardMaestroDataToRemoteKyber reads and DISCARDS Maestro bytes past 64 — the exact drop the sibling function was fixed to stop doing |
| FIX-068 | TODO | `Code/WCB/WCB.ino:4402` | ⚠FIX | The endsWith("?") help intercept makes every "set the character back to ?" command impossible — including the exact examples the built-in help prints |
| FIX-069 | **DONE** | `Code/WCB/WCB.ino:4684` | ⚠FIX | ?ETM,HB with no value silently stores a 0-second heartbeat interval and turns the board into an ESP-NOW broadcast storm |
| FIX-070 | **DONE** | `Code/WCB/WCB.ino:5046` | ⚠FIX | ?FUNCCHAR accepts ';' with no conflict check, and handleSingleCommand tests the function identifier first — the entire ';' command family is permanent |
| FIX-071 | **DONE** | `Code/WCB/WCB.ino:5279` |  | ?SLS<x>,<label> is completely dead — the caller strips "SLS" and updateSerialLabel() strips it again |
| FIX-072 | **DONE** | `Code/WCB/WCB.ino:5772` |  | printBackupConfig emits the live-chain checksum as <funcChar>CHK, but both verifiers hard-code "?CHK" — restore integrity checking is silently skipped |
| FIX-073 | **DONE** | `Code/WCB/WCB.ino:5894` | ⚠FIX | ;S<port>,<msg> sends a literal leading comma — the separator every sibling runtime verb strips |
| FIX-074 | **DONE** | `Code/WCB/WCB.ino:6125` | ⚠FIX | Nested ;C / ;SEQ recalls have no depth or cycle guard — a self-referencing stored sequence loops forever inside loop()'s queue drain |
| FIX-075 | TODO | `Code/WCB/WCB.ino:6191` | ⚠FIX | ;Px,width as documented cannot work, and its rejection is silent because debugPWMEnabled is never assigned anywhere |
| FIX-076 | **DONE** | `Code/WCB/WCB.ino:6603` | ⚠FIX | serialCommandTask hard-codes the Kyber/Maestro port skip to S1/S2 while the Kyber and Maestro ports are configurable S1-S5, so both tasks drain the sa |
| FIX-077 | **DONE** | `Code/WCB/WCB.ino:6608` |  | Kyber_Local / Maestro_Remote are settable at runtime but KyberLocalTask / KyberRemoteTask are only created at boot, leaving S1 and S2 with no reader a |
| FIX-078 | **DONE** | `Code/WCB/WCB.ino:7225` |  | Serial1/Serial2 begin() has no baudRates>0 guard (Serial3-5 does) — a 0 in NVS blocks 20 s in the core's baud auto-detect and the boot guard reboots t |
| FIX-079 | **DONE** | `Wizard/app.js:454` | ⚠FIX | reconcileBoardGrid can delete a board slot that is mid-flash, killing the post-flash config restore |
| FIX-080 | TODO | `Wizard/app.js:628` | ⚠FIX | addBoardSection rebuilds a section with factory defaults when a config already exists but the board is not USB-connected |
| FIX-081 | **DONE** | `Wizard/app.js:737` | ⚠FIX | S3-S5 baud dropdown is capped at 57600, so any higher configured baud blanks the select and is silently rewritten to 9600 |
| FIX-082 | TODO | `Wizard/app.js:855` |  | MP3 / HCR / DFPlayer port dropdowns are never refreshed when another feature frees a port |
| FIX-083 | TODO | `Wizard/app.js:962` |  | Changing the Kyber serial port never marks the board unsaved — the only device port handler missing the dirty call |
| FIX-084 | TODO | `Wizard/app.js:1219` |  | ETM Message Count defaults to 3, below the firmware's clamp floor of 10 — the value can never round-trip |
| FIX-085 | TODO | `Wizard/app.js:1663` |  | boardOtaSerial's post-OTA reboot handling has no `_shared` guard, so a successful OTA on a shared-hub port leaves the board card stuck at "Not connect |
| FIX-086 | TODO | `Wizard/app.js:2003` |  | populateUIFromConfig calls onHWVersionChange, which silently rewrites the pulled LED pin (HW 3.2 @ GPIO38 → 48) and marks the board dirty |
| FIX-087 | **DONE** | `Wizard/app.js:2236` | ⚠FIX | MP3 baud picker lets the user set values the firmware rejects outright — the whole `?MP3,…` line is silently dropped |
| FIX-088 | **DONE** | `Wizard/app.js:2305` |  | syncMP3ToConfig re-reads the raw MP3 volume input, discarding onMP3VolChange's clamp — an out-of-range value makes the firmware reject the whole MP3 c |
| FIX-089 | **DONE** | `Wizard/app.js:3246` |  | Maestro ID dropdown offers 9, which the firmware rejects as a reserved routing target |
| FIX-090 | **DONE** | `Wizard/app.js:3594` |  | syncMappingsToConfig() wipes config.pwmOutputPorts and never repopulates it |
| FIX-091 | TODO | `Wizard/app.js:3866` |  | PWM mapping Save schedules a config pull at 2 s, but the board blocks 3 s and reboots |
| FIX-092 | TODO | `Wizard/app.js:4092` |  | seqValueToLines/seqTextareaToValue are not inverses: '^^***' standalone comments collapse, and a plain Push rewrites stored sequences |
| FIX-093 | TODO | `Wizard/app.js:5541` |  | A relay's transient USB drop tears down every relay-managed remote board and the successful reconnect never rebuilds them |
| FIX-094 | TODO | `Wizard/app.js:5771` | ⚠FIX | Cancelling the shared-port picker still installs a dead _shared connection, permanently latching _hasSharedPort() and blocking auto-share |
| FIX-095 | TODO | `Wizard/app.js:6136` | ⚠FIX | General-mismatch modal's "Use WCB<n> values" silently ignores the NaviCore rows it just displayed |
| FIX-096 | TODO | `Wizard/app.js:6800` | ⚠FIX | boardPull commits an empty or truncated backup as a successful pull, overwriting config AND baseline with factory defaults |
| FIX-097 | TODO | `Wizard/app.js:7140` |  | Failed post-flash / post-erase boot-wait leaves the Push Config button permanently disabled and labelled "Flashing…" |
| FIX-098 | TODO | `Wizard/app.js:7902` |  | remoteBoardPull leaks its _pullingBoards entry on a retry attempt, permanently locking that board out of all future pulls |
| FIX-099 | TODO | `Wizard/app.js:8282` | ⚠FIX | Push All includes MgmtRelay slots, whose board section (and Push button) was deleted — boardGo throws and Push All dies before the relay-reboot stage |
| FIX-100 | TODO | `Wizard/app.js:8966` | ⚠FIX | Kyber target baud picker uses the full `BAUD_RATES` list; the firmware's Kyber target parser accepts only six of those rates and silently skips the re |
| FIX-101 | **DONE** | `Wizard/app.js:9341` |  | Reducing the slot count leaves activeBoardTab out of range — the Board Identity step renders with no visible panel |
| FIX-102 | TODO | `Wizard/app.js:9520` | ⚠FIX | Re-entering the Connect step after Back discards every later edit and leaves the port panel dead |
| FIX-103 | TODO | `Wizard/app.js:10550` | ⚠FIX | wizardSwitchBoardTab uses the board index against positional DOM indices — clicking a Serial tab hides every panel |
| FIX-104 | **DONE** | `Wizard/app.js:10822` | ⚠FIX | Identity validation hardcodes ID 20 instead of the configurable NaviCore ID, allowing a controller/board ID collision |
| FIX-105 | TODO | `Wizard/app.js:10931` | ⚠FIX | wizardApplyConfig never writes boardConfigs[n] for Client slots — slot type and client alias are silently dropped |
| FIX-106 | TODO | `Wizard/app.js:11748` | ⚠FIX | Connect & Push always reports "✓ Done", even when the push never happened or the board never came back |
| FIX-107 | **DONE** | `Wizard/app.js:12610` |  | The 12 s mesh-discovery poll injects ?WDP,DUMP into an in-flight config push, faking ACKs for dropped settings |
| FIX-108 | **DONE** | `Wizard/flasher.js:414` |  | Recognized-but-unsupported chips (ESP32-S2/C2/C3/C5/C6/C61/H2/P4) are flashed with ESP32 or S3 firmware whenever a HW version is selected |
| FIX-109 | **DONE** | `Wizard/flasher.js:686` | ⚠FIX | Flash progress bar stalls at ~62% — esptool reports COMPRESSED bytes but the denominator is the uncompressed total |
| FIX-110 | **DONE** | `Wizard/index.html:640` |  | Kyber Maestro Baud <select> cannot represent 5 of the 13 firmware-legal baud rates — a pulled 19200 blanks the control and Push silently rewrites the  |
| FIX-111 | **DONE** | `Wizard/index.html:809` |  | MP3 baud picker offers values the firmware rejects outright, discarding the whole MP3 config |
| FIX-112 | **DONE** | `Wizard/parser.js:902` | ⚠FIX | The Wizard drops ?WDP,OFF / ?WDP,AUTOJOIN,OFF from a pulled config and never re-emits them — an export/restore silently re-enables discovery |
| FIX-113 | **DONE** | `Wizard/parser.js:905` |  | Parser drops `?WDP,OFF` and `?WDP,AUTOJOIN,OFF` from a board backup — a config export/restore silently re-enables WDP discovery |
| FIX-114 | TODO | `Wizard/parser.js:1143` | ⚠FIX | Two boards that share a WCB number collapse into one on config-file save → reload (whole board config lost) |
| FIX-115 | **DONE** | `Wizard/parser.js:1321` |  | Clearing a serial-port label never reaches the board, and the stale label resurrects a phantom Kyber Marcuino port on the next pull |
| FIX-116 | TODO | `Wizard/serial-hub-test.html:93` |  | serial-hub-test.html: "Pick Port" can never be enabled — the shipped test harness is unusable |
| FIX-117 | TODO | `Wizard/serial-hub.js:325` |  | `_adoptGrantedPort()` takes the first VID/PID match — failover to the WRONG physical WCB when two identical adapters are granted |
| FIX-118 | **DONE** | `Wizard/serial-hub.js:407` | ⚠FIX | Read loop death (unplug) leaves the lock held and the dead port handle open — board unreachable from every tab |
| FIX-119 | **DONE** | `Wizard/serial-hub.js:501` |  | A promoted leader broadcasts the PREVIOUS leader's port identity, defeating the Wizard's wrong-board guard |

---

## S4 — 5 items

| # | Status | File:line | ⚠ | Finding |
|---|---|---|---|---|
| FIX-120 | **DONE** | `Code/WCB/WCB_Help.cpp:128` | ⚠FIX | ?BAUD? help tells the user to run ?MAESTRO,ENABLE, which is not implemented |
| FIX-121 | **DONE** | `Code/WCB/WCB_Help.cpp:364` |  | ?WCB? help caps the board number at 9; the parser accepts 1-20 |
| FIX-122 | **DONE** | `Code/WCB/WCB_Help.cpp:1015` |  | Ten implemented ? commands have no help topic and no menu entry; asking for help on them silently prints the generic menu |
| FIX-123 | **DONE** | `Code/WCB/WCB.ino:5138` | ⚠FIX | ?LED,PIN re-inits the NeoPixel object while the WiFi task dereferences it, leaking the old object and opening a use-after-free |
| FIX-124 | **DONE** | `Wizard/flasher.js:720` |  | Post-flash hard reset never runs — `loader.afterFlash` does not exist in the vendored esptool-js 0.4.7 |

---

## Change log

Every edit, appended as it happens. One row per commit-worthy change.

| Date | FIX ids | Files touched | What changed | Build |
|---|---|---|---|---|
| 2026-08-19 | FIX-001, FIX-002 | `WCB.ino` | `etmCharPhaseSentTimes` OOB write: added named `ETM_CHAR_MAX_MSGS` (200), clamp **per board** (`perBoard = min(count, CAP/peerCount)`) so the phase index can never leave the array, one-shot operator notice when the sample is reduced, both read guards switched off the bare literal. Also fixes the never-satisfied `allDone` that made every phase run to full timeout. | ESP32 ✅ S3 ✅ |
| 2026-08-19 | FIX-003 | `WCB.ino` | `handleMgmtForward`: **reject** an over-long FRAG payload instead of silently `strncpy`-truncating it. Ungated print so a bad sender fails loudly rather than writing corrupt config to NVS. | ESP32 ✅ S3 ✅ |
| 2026-08-19 | FIX-005 | `Wizard/app.js` | `MGMT_CHUNK_SIZE` 180 → **179** to match what the relay actually carries; added `MGMT_MAX_CHUNKS = 16` mirroring the firmware. Both carry comments naming the firmware line so they cannot drift back. | n/a (JS) ✅ |
| 2026-08-19 | FIX-004 | `Wizard/app.js` | `?SEQ,SAVE` over a relay now fragments when it exceeds the relay ETM shortcut (198), refuses when it exceeds 16 chunks, and no longer claims success on the un-ACKed multi-chunk path. | n/a (JS) ✅ |
| 2026-08-19 | (with FIX-003/004/005) | `WCB.ino` | **Second hazard on the same boundary**: exempted `MGMT,`/`SEQ,` from the trailing-`?` help shortcut in `processLocalCommand`. A fragment boundary landing after a `?` used to make the whole FRAG line parse as a help request — silently dropped, push died at the 15 s session timeout. Changing 180→179 only relocates that boundary; this removes it. | ESP32 ✅ S3 ✅ |
| 2026-08-19 | FIX-006, FIX-011, +2 dupes | `command_timer.cpp`, `WCB.ino` | **commandGroups use-after-free.** Fixed the *victim* per the verdict rather than every mutator: `processCommandGroups()` now copies the group’s commands out before the `vTaskDelay`, holds no reference/iterator across the yield, and re-checks a new `commandGroupsGeneration` counter afterwards so a chain replaced mid-flight is abandoned instead of having a stale index applied to it. Also guards an empty group (was an unchecked `commands[0]`). Corrected the false invariant comment at `WCB.ino:398`. | ESP32 ✅ S3 ✅ WDP ✅ |
| 2026-08-19 | FIX-012 | `WCB.ino` | ETM-char ACKs for idx >= cap: resolved by the FIX-001 clamp (totalMessages can no longer exceed the cap, so the discard branch is now unreachable). Guard kept as defence. | ESP32 ✅ S3 ✅ |
| 2026-08-19 | (ETM phase 3) | `WCB.ino` | `ETMLOAD` trigger was sent **non-ETM** and dropped by every peer’s ETM-mismatch gate, so phase 3 “Loaded Network” measured an idle mesh and `processETMLoad` was unreachable. Now sent under ETM. | ESP32 ✅ S3 ✅ |
| 2026-08-19 | (?EPASS legacy) | `WCB.ino` | `updateESPNowPassword` did `substring(6)` on the 5-char `EPASS` prefix, eating the first character of the password on the legacy no-comma form. Now `substring(5)`. | ESP32 ✅ S3 ✅ |
| 2026-08-19 | FIX-008, FIX-009 | `WCB_Storage.cpp` | **Factory-reset namespace family.** `?ERASE,NVS` now also clears `dfp_cfg` (DFPlayer was re-seizing its UART after a reset) and the legacy `stored_commands` (whose survival re-imported deleted sequences on next boot). `clearAllStoredCommands()` re-stamps `seq_mig_done`, which lives in the namespace it wipes — that was what re-armed the migration. Erase farewell now tells the user to set `?HW,xx`. | ESP32 ✅ S3 ✅ |
| 2026-08-19 | (ETM CHKSM) | `WCB.ino` | Named `ETM_MAX_CMD_WITH_CRC` (187) and **refuse** to send a longer command under `?ETM,CHKSM` instead of emitting a frame whose CRC gets truncated — the receiver ACKed those before verifying, so the sender saw 100 % delivery for commands that never ran. Relay single-chunk gate now uses the reduced ceiling when CHKSM is on. | ESP32 ✅ S3 ✅ |
| 2026-08-19 | (Kyber dup bytes) | `WCB.ino` | `forwardDataFromKyber` wrote each byte once **per target**, so daisy-chained Maestros sharing one port got every byte twice (garbage frames). Added a per-port bitmask in BOTH branches, scoped **per byte** — the verdict warned that hoisting it per drain-pass would deliver only the first byte of a burst. | ESP32 ✅ S3 ✅ |
| 2026-08-19 | FIX (BCAST,RESET) | `WCB_Storage.cpp` | Reset now syncs the live globals, clears input blocking (never reset at all) and `broadcastToS0`. Previously the stale RAM copies were re-persisted over the defaults by the next unrelated save. | ESP32 ✅ S3 ✅ |
| 2026-08-19 | (remote-pull filter) | `Wizard/app.js` | `remoteBoardPull` listener now filters on the **source** WCB in `[MGMT:CONFIG,n]`. Every live listener on a relay used to consume whichever reply arrived first — one board’s config, baseline and wcbNumber written onto another board’s slot, after which a Push wrote the wrong settings to real hardware. | JS ✅ |
| 2026-08-19 | (post-OTA replay) | `Wizard/app.js` | Serialised the post-OTA reconciliation pulls (were fired in parallel on one relay, which has a single `pullSession`). | JS ✅ |
| 2026-08-19 | (board-number domain) | `Wizard/app.js` | Mapping-destination dropdown and `exportSystemFile` both walked only the WCBQ floor, silently retargeting mappings above it and omitting discovered boards from saved system files. Both now cover the real domain (floor ∪ discovered ∪ connected ∪ configured value). | JS ✅ |
| 2026-08-19 | FIX-051,052 + Wizard | `WCB_Storage.cpp`, `parser.js`, `app.js` | `etmMissedHeartbeats` NVS default 3 → **5** to match the firmware initialiser, AND the six Wizard defaults that wrote 3 straight back on every full push. The one-liner alone would have been undone by the tool. | ESP32 ✅ S3 ✅ |
| 2026-08-19 | FIX-053, FIX-069 | `WCB.ino` | `?ETM,HB` with no value wrote **0** to NVS (a bare query is the natural way to read a setting; help documents no query form), after which `scheduleNextHeartbeat` drew a negative interval that wrapped to a past deadline — ~4 Hz of unacked 252-byte broadcasts, persisted across reboots. Bare form now QUERIES, values are range-checked (1-3600), legacy `?ETMHB` likewise, and the scheduler clamps defensively for values restored from old NVS. Same treatment for `?ETM,MISS`. | ESP32 ✅ S3 ✅ |
| 2026-08-19 | FIX-054 | `WCB.ino` | `etmSendAck` now registers the sender as an ESP-NOW peer on demand. A relay above our WCBQ floor got no ACK, so the sender burned 3 retries and reported FAILED for a command that actually ran. | ESP32 ✅ S3 ✅ |
| 2026-08-19 | FIX-058, FIX-072 | `WCB.ino` | Backup checksum verifier matched a hard-coded `?CHK` while `printBackupConfig` emits `<funcChar>CHK` — on a custom-funcChar board the integrity check was silently skipped and a corrupted restore applied without complaint. Now matches the live identifier, still accepting `?` for factory-reset chains. | ESP32 ✅ S3 ✅ |
| 2026-08-19 | FIX-071 | `WCB.ino` | `?SLSx,label` was double-stripped (dispatcher removed “SLS”, then `updateSerialLabel` removed 3 more chars) so it failed its own format guard every time. Pass the full message. | ESP32 ✅ S3 ✅ |
| 2026-08-19 | FIX-078 | `WCB.ino` | Baud table sanitised before any `begin()`. **Amends an earlier refutation of mine**: a 0 baud IS reachable — firmware between 483b4d8 and f5d758f whitelisted `baud == 0` and NVS survives updates, so old boards still carry it. Serial3-5 had a guard; Serial1/2 did not, and baud 0 makes HardwareSerial auto-detect and block long enough for the boot guard to reset — an unexplained boot loop. Now repaired to 9600 with a console notice. | ESP32 ✅ S3 ✅ |
| 2026-08-19 | FIX-064 | `WCB.ino` | ETM-char “no online peers” abort now unwinds the run: restores `debugETM` and clears/answers `etmCharRelayRequesterWCB`, which used to latch so a later LOCAL run frag-sent its results to a board that never asked. | ESP32 ✅ S3 ✅ |
| 2026-08-19 | FIX-055, FIX-056 | `WCB.ino` | ETM-char phase 2 is unicast, not broadcast — relabelled in the live log and the results table so the report stops presenting unicast latencies under a broadcast heading. | ESP32 ✅ S3 ✅ |
| 2026-08-19 | F-003, F-004 | `wcb_pin_map.h`, `WCB.ino` | Fixed the include guard (`wcb_pin_map.h` is not a legal macro name) and the non-atomic `volatile` increment on the drop counter (now under its own portMUX). **Both targets now compile with ZERO warnings.** | ESP32 ✅ S3 ✅ |
| 2026-08-19 | FIX-070 | `WCB.ino` | `?FUNCCHAR` now rejects a collision with the command character (setting it to `;` routed the whole `;` device-command family into the local dispatcher, persisted to NVS) and rejects control/space characters. | ESP32 ✅ S3 ✅ |
| 2026-08-19 | FIX-073 | `WCB.ino` | `;S<port>,<msg>` stripped one optional `,` separator — every sibling verb strips it, so the comma was going out on the wire to the device. | ESP32 ✅ S3 ✅ |
| 2026-08-19 | FIX-074 | `WCB.ino` | Sequence cycle guard. A self-referencing (or A→B→A) sequence re-enqueued forever, so loop() drained an endlessly-refilled queue. A depth counter would NOT have caught it (no stack recursion — the nested recall runs from a later drain), so this tracks the keys expanded during the current chain instead. | ESP32 ✅ S3 ✅ |
| 2026-08-19 | FIX-076 | `WCB.ino` | Kyber-owned port skip in `serialCommandTask` was hard-coded to S1/S2 while `kyberLocalPort` is configurable S1-S5 — with the Kyber on S3/S4/S5 two tasks drained the same UART. Now skips the configured port. (Left the S1/S2 skip intact; the Kyber task drains both regardless.) | ESP32 ✅ S3 ✅ |
| 2026-08-19 | FIX-123 | `WCB.ino` | `?LED,PIN` re-init now deletes the previous NeoPixel object instead of leaking it. | ESP32 ✅ S3 ✅ |
| 2026-08-19 | FIX-010 | `WCB_Storage.cpp` | `?KYBER,CLEAR` acted on a hard-coded port 2. Since the config chain emits it for EVERY non-Kyber board, every full push reset Serial2 to 9600 and re-enabled its broadcast flags, undoing settings the same push had just applied. Now uses the port the Kyber actually held, and touches nothing when it was never configured (also removes the `[-1]` index on `?KYBER,CLEAR,S0`). | ESP32 ✅ S3 ✅ |
| 2026-08-19 | FIX-047 | `WCB_Storage.cpp` | `?SEQ,SAVE` now rejects a key over the 15-char NVS limit and checks the `putString` result, instead of listing and advertising a sequence whose value was never stored. | ESP32 ✅ S3 ✅ |
| 2026-08-19 | FIX-048 | `WCB_Storage.cpp`, `WCB_PWM.{h,cpp}` | `clearAllPWMMappings()` ended in `ESP.restart()`, so `eraseNVSFlash()` never reached its own confirmation or restart — and it broadcast `?REBOOT`, restarting the whole fleet because one board was factory-reset. Added an `autoReboot` parameter; erase passes false. | ESP32 ✅ S3 ✅ |
| 2026-08-19 | FIX-110, FIX-111, FIX-087 | `index.html` | **Baud pickers vs firmware.** MP3 offered all 13 rates but the firmware accepts only 9600/38400 and rejects the whole `?MP3` line otherwise — now two options. Kyber Maestro offered 4 of 13, so a pulled 19200/128000 blanked the select and the blank was read back as the setting — now the full legal set. | JS ✅ |
| 2026-08-19 | FIX-081 | `app.js` | S3-S5 baud dropdown capped at 57600 hid an already-configured higher rate, so the select rendered blank and `syncSerialUIToConfig` rewrote it to 9600 on the next push. The configured value is now always present (marked `(!)`), and the readback falls back to the configured value instead of 9600. | JS ✅ |
| 2026-08-19 | FIX-089 | `app.js` | Maestro ID dropdown offered **9**, which the firmware treats as the reserved all-local routing target and never stores as a slot. Devices are 1-8. | JS ✅ |
| 2026-08-19 | FIX-088 | `app.js` | `syncMP3ToConfig` re-read the raw volume input, discarding `onMP3VolChange`’s 0-64 clamp on every push. | JS ✅ |
| 2026-08-19 | FIX-090 | `app.js` | `syncMappingsToConfig` wiped `config.pwmOutputPorts`, which is not derived from local mapping rows at all — the parser fills it from what the BOARD reported about ports a REMOTE mapping drives. Any mapping edit dropped them from the config and the next export. | JS ✅ |
| 2026-08-19 | FIX-115 | `parser.js` | Clearing a serial label emitted nothing (`cur.label &&` guard), so the board kept the old one — leaving the port showing as claimed and able to resurrect a phantom Kyber Marcuino port. Now emits `LABEL,CLEAR,Sx` (verified accepted at `WCB.ino:4739`). | JS ✅ |
| 2026-08-19 | FIX-044,045,046,120,121,122 | `WCB_Help.cpp` | **Help-text cluster.** `?BAUD` no longer recommends the non-existent `?MAESTRO,ENABLE` or the comma form the parser rejects; `?WCB` range corrected 1-9 → 1-20; `?CMDCHAR` shows `;Pxnnnn` (the form the firmware itself emits) instead of the unparseable `;Px,width`; `?WLED` rewritten for the ID-addressed syntax (`?WLED,<id>:W<wcb>S<port>:<baud>`, `;L<id>,...`) replacing the superseded single-device form; **added a `?OTA`/`?OTALOCAL` topic** (the branch’s headline feature had none) and put `?DFP` + OTA in the top-level `??` menu. | ESP32 ✅ S3 ✅ |
| 2026-08-19 | FIX-043 | `Code/bin/build.sh` | **Release pipeline.** A branch name containing `/` made every `cp` target a non-existent dir; the exit codes were unchecked so the script still exited 0 *after* `rm -f` had deleted the committed binaries — CI then pushed the deletion as a green build. Branch is now flattened (`/`→`-`), every copy goes through `cp_or_die`, an unparseable VERSION aborts, and a missing python aborts with a real message instead of a bogus header-check failure. | `bash -n` ✅ |
| 2026-08-19 | FIX-041,118,040,119 | `Wizard/serial-hub.js` | **Reconciled with NaviCore’s required-lockstep copy.** Ported the three behavioural fixes the Wizard was missing: open-failure now `_stepDownPortless()` instead of squatting the exclusive lock; the backoff counter resets on a successful OPEN rather than on promotion; and read-loop death closes the stale handle and steps down instead of only announcing — which had left the board unreachable from every tab on the origin until the user disconnected by hand. Diff is now zero lines in NaviCore’s favour; the Wizard keeps `adoptPort`/`releasePortKeepOpen`, which `app.js` depends on. | JS ✅ |
| 2026-08-19 | FIX-067 | `WCB.ino` | `forwardMaestroDataToRemoteKyber` read bytes past its 64-byte buffer and **discarded** them, truncating any longer Maestro burst into a corrupt frame — the exact drop the sibling `forwardDataFromKyber` was already fixed to stop doing. Now flushes and continues. | ESP32 ✅ S3 ✅ |
| 2026-08-19 | FIX-061, FIX-063 | `WCB.ino` | `sendResultFrags` dropped any oversized relayed result with the only diagnostic behind `debugMGMT` (off by default), so `?MGMT,STATS` / `?MGMT,ETM` on a large fleet returned **nothing** and the requester just timed out. Now logs ungated and relays a short in-band `[ERROR] Result too large` so the operator gets a real answer. | ESP32 ✅ S3 ✅ |
| 2026-08-19 | FIX-112, FIX-113 | `Wizard/parser.js` | **WDP settings had no round-trip at all.** The firmware emits `WDP,OFF` / `WDP,AUTOJOIN,OFF` only when they are off (`WCB.ino:3023-3024`); the parser had no `WDP` case, so both were dropped on pull and never re-emitted — an export/restore silently re-enabled mesh discovery on a board where it had been turned off. Added defaults, a parse case accepting both spellings, and emit of the **ON** forms too (verified accepted at `WCB_WDP.cpp:1218-1223`) so re-enabling from the Wizard actually reaches the board. | JS ✅ |
| 2026-08-19 | FIX-039 | `Wizard/parser.js` | Stored sequences were emitted **after** the PWM/mapping block, and applying a PWM input mapping reboots the board — so every sequence in the same push was sent into a restarting board and silently lost. Sequences now precede that block, which is explicitly marked as must-stay-last. | JS ✅ |
| 2026-08-19 | FIX-049, FIX-050, FIX-028 | `WCB_Storage.cpp` | `?MAP,SERIAL` **appended** destinations instead of replacing them, so a destination removed or re-pointed in the Wizard stayed live on the board forever while the push reported success. The command carries the complete list, so it now replaces. That also persists a raw-mode toggle on an otherwise-unchanged mapping, which previously changed only RAM and reverted on reboot. | ESP32 ✅ S3 ✅ |
| 2026-08-19 | FIX-079 | `Wizard/app.js` | `reconcileBoardGrid` could delete a slot **mid-flash**. `boardGo` hands the port to esptool, so `isConnected()` is false for the whole 30-60 s window; evicting the slot deleted `boardConnections`/`boardConfigs` and the post-flash config restore then dead-ended on “Board not connected”, leaving a freshly-flashed board on factory-default NVS. Now honours `_boardFlashing`, the same guard `updateConnectionUI` already uses. | JS ✅ |
| 2026-08-19 | FIX-107 | `Wizard/app.js` | The 12 s mesh-discovery poll injected `?WDP,DUMP` into an in-flight config push — a dump line could satisfy a pending read and fake an ACK for a config command that was actually dropped. Added a real `_pushingBoards` in-flight set (released on the normal path **and** both early exits) plus a flash guard, alongside the existing OTA/stats guards. | JS ✅ |
| 2026-08-19 | FIX-014 | `WCB.ino` | `applyLiveBaud` called `end()`/`begin()` on a live SoftwareSerial while `serialCommandTask` (5 ms poll) and `RawSerialForwardingTask` were inside `available()`/`read()` on the same object — a read against a freed RX buffer, reachable from an ordinary `?BAUD,Sx` or any config push that changes a baud. Added a `serialReconfigPort` ownership flag that both drainers honour, plus a settle delay before teardown. | ESP32 ✅ S3 ✅ |
| 2026-08-19 | FIX-065 | `WCB.ino` | A rebooted peer restarts its sequence counter at 1, but our duplicate ring still held its pre-reboot seqs — so its first commands matched “already seen” and were **ACKed and silently not executed**. The boot-announce edge now clears that sender’s history. | ESP32 ✅ S3 ✅ |
| 2026-08-19 | FIX-077 | `WCB_Storage.cpp` | `?KYBER,LOCAL` / `?KYBER,REMOTE` change the mode at runtime, but `KyberLocalTask`/`KyberRemoteTask` are created only at boot — the newly-owned ports had no reader until a restart. Both paths now say a reboot is required instead of looking configured-but-deaf. | ESP32 ✅ S3 ✅ |
| 2026-08-19 | FIX-108 | `Wizard/flasher.js` | A **recognised but unsupported** ESP32 variant (S2/C2/C3/C5/C6/C61/H2/P4) produced `detectedType = null` and then fell through to the HW-version-mapped binary — writing ESP32 or S3 firmware onto silicon it was not built for. Now refuses outright; the fallback is reserved for a genuinely unreadable chip id. | JS ✅ |
| 2026-08-19 | FIX-109 | `Wizard/flasher.js` | Progress bar stalled around 62 %: with `compress:true` esptool reports **compressed** byte counts while the denominator was the sum of **uncompressed** image sizes. Per-file fraction is now weighted by that file’s uncompressed share, which is correct either way. | JS ✅ |
| 2026-08-19 | FIX-124 | `Wizard/flasher.js` | The post-flash hard reset silently did nothing — the vendored esptool-js 0.4.7 has neither `afterFlash` nor `after_flash`. Tries every known spelling, then falls back to a manual DTR/RTS pulse (both verified present on the vendored transport), and logs if it still could not reset. | JS ✅ |
| 2026-08-19 | FIX-101 | `Wizard/app.js` | Reducing the slot count left `activeBoardTab` past the end of the array, rendering the Board Identity step with an empty panel and no selected tab. Clamped in `wizardInitBoards`, where the array is resized. | JS ✅ |
| 2026-08-19 | FIX-104 | `Wizard/app.js` | Identity validation hardcoded reserved ID **20**, but the wizard lets the user set any NaviCore ID 1-20 — so it missed a real clash on a non-default id and spuriously rejected 20 when the controller had been moved. Matters because the firmware derives each peer’s MAC from its id, so two peers on one id share a MAC. Now validates against the configured id. | JS ✅ |
