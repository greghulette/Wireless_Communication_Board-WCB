# MGMT relay: the remote config pull, and everything else the relay carries

How a requester (the Wizard, Intellex, the HIL harness, or a person at a terminal) reads the saved config of a board
it has no cable to. The requester talks to a **relay** (a WCB, or a WCB_Client host running WcbMgmt such as NaviCore
or the MgmtRelay example) over USB serial or a WebSocket; the relay talks to the **target** over ESP-NOW.

The other `?MGMT` verbs (STATS, ETM, SEQ, SEQGET, FRAG) share the relay's fragment machinery; sequences have their own
page, [SEQUENCE_INVENTORY.md](SEQUENCE_INVENTORY.md). §10 lists every result and payload the relay carries, its limit
and the largest it can be on a full board, and how a ?STATS, an ETM characterization and one sequence longer than one
reply come in parts, and a Wizard push longer than one session goes as several.

## 1. The rule everything else serves

**A `[MGMT:CONFIG,<n>]` line carries a board's whole config or nothing.** Every Wizard released before 2026-09-24,
including the copies frozen inside each Intellex install, stores ANY non-empty body under that tag as the board's
config and baseline, without checking its `^?CHK`. A part, an error text or a truncated config printed under that
tag would be written back to the board on the next push. (The current Wizard checks it, §6, but the old ones stay in
the field.) So:

- Packet type 6 (CONFIG_FRAG) carries only a complete reply, or the empty reply that means "out of memory".
- Parts and errors travel as packet type 18 and are printed under `[MGMT:CFGPART,` / `[MGMT:CFGERR,`, which differ
  from `[MGMT:CONFIG,` before the comma, so old Wizards ignore them (they show them raw in the relay's pane).
- Parts are sent only when the requester asked for them (`?MGMT,PULL,<n>,P`, which only a new Wizard or the harness
  sends). An old requester never causes a part to be sent.

## 2. Requests

| Command at the relay | ESP-NOW request | Who sends it |
|---|---|---|
| `?MGMT,PULL,<n>` | CONFIG_REQ (type 5) x3, 15 ms apart | old Wizards, a person |
| `?MGMT,PULL,<n>,P` | CONFIG_REQ_PARTS (type 19) x3, then CONFIG_REQ (type 5) x3 | the Wizard since F13, the harness |

Both requests are the 43-byte packed `espnow_struct_config_req` (structPassword[40], packetType, targetWCB,
requesterWCB, in that order). The `,P`
flag is case-insensitive and tolerates trailing whitespace (`handleMgmtPullRequest`, `WCB.ino`; `WcbMgmt` in
WCBClient 1.17.1+ `src/WCB_Mgmt.h`). A relay from before F13 parses `<n>,P` as `<n>` (toInt/atoi) and sends type 5 only, so
the `,P` form is safe to send through any relay. The type-5 copies after the type-19 copies keep an old target
answering; a new target ignores them as duplicates.

## 3. Replies

The reply text is `[VER:<fw>]<chain>^?CHK<8 hex>`: the target's factory chain (every token as `?<token>` joined by
`^`, the same tokens `?backup` prints, from `collectConfigCommands`) behind its firmware version, with the CRC-32 of
the chain in upper-case hex. Its length is L.

| Case | Packet type | Relay prints |
|---|---|---|
| L <= 2912 | 6, one session | `[MGMT:CONFIG,<n>]<reply>` (unchanged since before F13) |
| L > 2912, request asked for parts | 18, one session per part | `[MGMT:CFGPART,<n>]P<id>,<k>,<K>:<data>~` per part |
| any refusal | 18, one session | `[MGMT:CFGERR,<n>]<CODE>,<detail>` |
| out of memory with L <= 2912 | 18 (the error) then 6 (empty) | the CFGERR line, then a bare `[MGMT:CONFIG,<n>]` |

A session is 1-16 frags of 182 payload bytes in the 230-byte `espnow_struct_config_frag`, sent twice (two passes),
20 ms apart; one session carries at most 2912 characters.

**Parts.** `id` is 4 upper-case hex digits, random per pull and the same on every part of it; `k` is 1-based; `K` is
the part count (at most 64, `CFGP_MAX_PARTS`; 16 before 2026-10-23, which a store of a few thousand short sequences
passed - every requester since F13 reads `k` and `K` as up to 2 digits). `data` is bytes `[split(k-1), split(k))` of the reply. The part data size D is 2880:
`split(k) = back(k*D)` for 0 < k < K, where back steps down over at most 3 UTF-8 continuation bytes (0x80-0xBF), so
no part cuts a character and K is always ceil(L/D). Every part therefore holds D-3..D+3 bytes (the last at least 1),
and a part message is at most 12 + 2883 + 1 = 2896 characters. The `~` closes the data so a part that ends in
whitespace survives the requester's line trim. The requester joins the data of parts 1..K and checks the `[VER:`
head, the `^?CHK` tail and the CRC; a failure is retried like a timeout. The WiFi passphrase and the mesh password
sit in the first ~300 characters of the chain, so they are always inside part 1 and never split across two lines.

**Errors** (the relay strips the message's leading `E`; texts from `cfgpErrorText`, `Code/WCB/WCB_ConfigParts.h`):

| Code | Meaning | Requester |
|---|---|---|
| NOMEM | the target could not allocate a reply buffer (or a config line) for 1 s | retries |
| CHANGED | the config changed while it was being sent, three times running | retries |
| NOPARTS | L > 2912 and no parts request arrived: every type-19 copy was lost, or the requester is too old to ask | retries (a ,P pull) |
| TOOBIG | more than 64 parts (L > 184,320), which no board's config reaches (§10) | stops |

An error never carries config text. The target also prints each refusal on its own console, ungated, e.g.
`[MGMT] Config pull from WCB<r>: the config is too large to relay (<L> chars, max 2912). ...`.

## 4. Target

`handleConfigReqPacket` (loop context, from `drainMgmtReqs`) checks packetType, targetWCB and the password
(strncmp on the fixed 40-byte field: two Strings that both failed to allocate compare equal), then:

- **Dedup:** one timestamp per requester, stamped when a request is accepted; a request from a requester accepted
  less than 1500 ms ago is ignored. This swallows the redundant copies of one request.
- **One job at a time.** A request from the running job's requester is ignored ("reply in progress"); a request from
  another requester is **parked** and served, oldest first, when the job ends; a parked request older than 5 s is
  dropped (its requester retries on its own).

The job (`serviceConfigPullJob`, called from `loop()`; one step per call, nothing blocks):

1. **Measure walk** (`configPullWalk` over `collectConfigCommands`, no allocation): L and the CRC. `PEERSLIVE` is
   sampled once and frozen for the whole job, so parts built by separate walks agree; `?backup` keeps the live value.
2. **Plan:** LEGACY (L <= 2912), PARTS (L > 2912 and the request asked for parts), NOPARTS or TOOBIG.
3. **Build** each message by walking again and copying only its window into a malloc'd buffer of at most 2912 bytes
   (never a static array: .bss comes out of the same DRAM as the ~19 KB AP-mode heap). The first build walk runs in
   the same step as the measure walk, so no command or WDP packet can change the config between them. Each build
   walk re-checks L and the CRC; a mismatch restarts the job with a new id, at most twice, then ERROR CHANGED.
4. **Out of memory** (the buffer malloc fails, or a token's String could not be allocated during a walk, which would
   otherwise ship a config missing a line under a valid checksum): retry every 200 ms for 1 s, then ERROR NOMEM.
5. **Send** one frag per step, at least 20 ms apart, pass 1 then pass 2, then the next message. Every message is its
   own session; sessionIds are never 0 or 0xFFFF and never equal to the previous one this target sent (relays keep a
   single `lastDeliveredSession`). `ESP_ERR_ESPNOW_NO_MEM` resends the same frag, up to 10 tries; a frag that fails
   in both passes abandons the job with a console line and no error message (the requester retries).
6. **End:** free the buffer, then serve the oldest parked request.

A deferred restart (`rebootPending`, `pwmRebootPending`) waits for `!configPullJobActive()`; an accepted OTA BEGIN
calls `configPullJobAbort()`. The whole pull of a 2-part config takes about 0.8 s on the bench, and the target keeps
answering its own console within 66 ms throughout (`wcb.pull_nonblocking`); a blocking send would hold `loop()` for
up to 640 ms.

The split and framing arithmetic lives in `Code/WCB/WCB_ConfigParts.h` with no Arduino dependency and is pinned by the
host test `tests/config_parts_test.cpp` (CI: `.github/workflows/wdp-tests.yml`).

**Test knobs** (RAM only, never saved, not in `?backup`; `?DEBUG?` lists them):

- `?DEBUG,PULLFAULT,OOM` makes the next accepted request's buffer allocation fail every time (one-shot, disarms after
  firing or after 60 s); `?DEBUG,PULLFAULT,OFF` disarms it.
- `?DEBUG,PULLPART,<D>` sets the part data size, 512-2880 (0 or OFF: 2880). The legacy/parts threshold stays 2912.

## 5. Relay

The 230-byte dispatch routes type 6 and type 18 to `handleConfigFragPacket` (WiFi task). It keeps ONE reassembly
session, keyed on (sessionId, packetType, sourceWCB):

- a frag of another key replaces the session, **except** that a type-18 frag never displaces a live non-18 session
  (one that received a frag in the last 200 ms): a whole reply always wins, and the part's requester retries;
- a completed session is assembled straight from its chunk array into a `mgmtQueueOut` slot, with no String and no
  heap on the WiFi task, and printed from `loop()` by `drainMgmtOut`: type 6 as `[MGMT:CONFIG,`, type 18 starting
  `P` as `[MGMT:CFGPART,`, starting `E` as `[MGMT:CFGERR,` (the `E` dropped); anything else is dropped;
- every relay line carries its own CRLF and leaves in ONE `Serial.write`, so another task's print lands between
  lines, never inside one. That holds on the UART only: `WCBSerial::write` copies the line into the WebSocket sink and
  the RTERM buffer a byte at a time, so on those transports a WiFi-task print (`[ETM] WCBn came ONLINE`) can still
  split a line; the requester then drops that line and retries.

WcbMgmt and MgmtRelay (WCBClient 1.17.1+) follow the same rules, print each line with one `printf` from `service()`/`loop()`
(on NaviCore only loop-task output reaches the WebSocket), and compact the chunks in place instead of using String.
A relay of any kind from before F13 drops a 230-byte frag of an unknown type silently. WCBClient 1.17.0 and older
are such relays: a config over 2912 characters does not come through them, and the Wizard reports a timeout.

## 6. Requesters

**Wizard** (`remoteBoardPull`, `Wizard/app.js`; pure parts in `Wizard/parser.js`: `crc32`, `makeLineSplitter`,
`parseConfigPart`, `parseConfigError`, `verifyConfigReply`, `createPullCollector`):

- sends `?MGMT,PULL,<n>,P`; keeps its 5-argument positional signature and fires `onComplete` exactly once (Intellex's
  shim calls `window.remoteBoardPull(slot, n, 1, 3, cb)` behind a 45 s watchdog that waits for the Wizard's own deadline
  while the Wizard still holds the pull);
- one attempt on the air per relay (a per-relay FIFO), because every relay has a single reassembly session;
- a 6 s attempt timer that restarts on every new part; `PULL_DEADLINE_MS` (40 s) from the call caps a pull of up to
  3 attempts, queue wait included, and moves out to `K x PULL_PART_MS` (1 s) + 6 s from the first part of each job when
  that is later, so a reply in parts gets as long as its parts take: a full sequence store of short sequences is ~35
  parts, and with one part lost the retry ran past 40 s;
- a single `[MGMT:CONFIG,<n>]` reply is checked like a join (`[VER:` head, `^?CHK` tail, CRC) before it is stored;
  its CRC may lack leading zeros, as 6.0.x printed it with `String(crc, HEX)`. A reply that fails, or has no `^?CHK`,
  never becomes the config or the baseline;
- NOMEM, CHANGED, NOPARTS, a CRC failure (of a reply or a join) and an empty reply are retried; TOOBIG stops at once;
  a reply or joined text that is not valid UTF-8 is retried once and stops on a second job's (every single reply
  counts as its own job);
- serial input is decoded with one streaming `TextDecoder` per connection (so a multi-byte character split across two
  reads is not replaced with U+FFFD); pull reply lines are summarised in the terminal, never shown raw, and kept out of
  STATS/ETM captures.

**Harness** (`tests/hil/hil/wcb.py`: `Pull`, `PullCollector`, `pull_config`, `mgmt_pull`, `PullRefused`): the same
rules; a `,P` pull that gets NOPARTS is re-sent once; `read_config` retries the retryable codes; refusal codes are
screened (`^[A-Z]{2,12}$`) and details withheld unless the code is known, so no config text reaches a report.

## 7. Compatibility

| Wizard | Relay | Target | Config <= 2912 | Config > 2912 |
|---|---|---|---|---|
| old | old | any | legacy line | nothing (times out) |
| old | new | old | legacy line | nothing |
| old | new | new | legacy line | a CFGERR NOPARTS line, ignored; times out |
| new | old | any | legacy line (the `,P` is read as a plain pull) | nothing (times out after 3 attempts) |
| new | new | old | legacy line | nothing (an old target drops type 19 and refuses over 2912 silently) |
| new | new | new | legacy line | parts, joined and verified |

Out of memory, a new target sends CFGERR NOMEM (and, for a legacy-size config, the empty CONFIG line an old Wizard
reports as "empty config response").

## 8. Limits

- One reply (and one part message) is at most 2912 characters: 16 frags of 182 bytes. More chunks would need wider
  receive masks and more static RAM on every board; a longer result goes in parts instead (§10).
- A pull is at most 64 parts of 2880 bytes (184,320 characters), more than any board's config (§10).
- One Wizard **push** session is at most 16 x 179 = 2864 bytes, including an appended `?reboot`; a longer push goes as
  several sessions, and one command longer than a session as one long session to an ESP32-S3 (§10). What cannot be
  carried at all is refused before its network-group confirm, its character bootstrap or any send (`boardGoRemote`).

## 9. Tests

Host: `tests/config_parts_test.cpp`. Wizard: `tests/wizard/unit/pull.test.js`, the no-board specs
`tests/wizard/specs/remote_pull_fake.spec.js` (`wizard.remote_pull_fake_*`; the legacy CRC check is
`wizard.remote_pull_fake_legacy_crc`) and `specs/push_fake.spec.js` (`wizard.push_fake_relay_sessions`,
`wizard.push_fake_relay_wcb_change`, `wizard.push_fake_relay_long_command`; the planner in `unit/push_plan.test.js`), and
`wizard.remote_pull`, `wizard.remote_pull_parts` on the bench. HIL (`tests/hil/suites/s03_wcb.py`, `s21_navicore_sbus.py`):
`wcb.pull_size_limit`, `wcb.pull_plain_over_limit`, `wcb.pull_parts_many`, `wcb.pull_error_oom_legacy`,
`wcb.pull_error_oom_parts`, `wcb.pull_nonblocking`, `wcb.pull_holds_restart` (a deferred restart waits for the reply),
`wcb.pull_changed_restart` (CHANGED), `wcb.pull_dedup_window`, `wcb.pull_park_second_requester` (W1 and NaviCore at
once), `wcb.pull_knobs` (the `?DEBUG` knobs), `wcb.pull_parts_over_16`, `wcb.mgmt_result_too_large` (an old requester,
`?DEBUG,ONEREPLY`), `wcb.mgmt_stats_parts`, `wcb.pull_wrong_target`, `wcb.pull_session_reaped`, `navicore.mgmt_pull`,
`navicore.pull_over_limit`. Results in parts (§10): host `tests/result_parts_test.cpp`; HIL `wcb.mgmt_stats_parts`,
`etm.char_relay_parts` (s99), `inv.seqget_parts`, `inv.push_long_sequence`, `inv.seqget_largest` (s17),
`ncmesh.mgmt_stats_parts`, `ncmesh.seqval_parts` (s43), `mesh.mgmt_errors` (s29: a FRAG of 25 chunks is refused).

## 10. Everything the relay carries, and how it gets across whole

**The rule:** whatever a board can store, a relay carries whole, in both directions. One relay session (16 frags of 182
bytes, 2,912 characters; or 16 push chunks of 179 bytes, 2,864) is not a limit on a result or a payload: a longer one
goes in parts, or in several sessions. The only "too large" answer left is the one a requester or target from before
this gets for a result past one reply, which it always got (Greg, 2026-10-23: keep the `[ERROR]` text).

### The audit (2026-10-23)

The largest each can be on a full board: 20 boards, 19 peers, a full 32,768-byte sequence store, 100 variables, every
counter at 2^32-1. The arithmetic for ?STATS and ETM is `buildStatsString` / `buildETMCharResultsString` (`WCB.ino`) at
those sizes.

| Path | Direction | Carried in | Limit | Largest on a full board | How it gets across |
|---|---|---|---|---|---|
| `?MGMT,PULL,<n>,P` config | target → requester | type 18 parts, each one session | `CFGP_MAX_PARTS` parts of 2,880 (`WCB_ConfigParts.h`): 64, 184,320 characters (16, 46,080, before) | under 142,000: sequences 98,488 (6,572 lines of the shortest keys and values, each its line + 10 as `^?SEQ,SAVE,`), variables 3,700, every other setting under 40,000 (they live in the 20 KB settings store) | in parts (§3); never TOOBIG |
| `?MGMT,PULL,<n>` config (no `,P`) | target → requester | type 6, one session | 2,912 | as above | as before: whole up to 2,912, else `CFGERR NOPARTS` - the answer of a requester that never asks for parts |
| `?MGMT,STATS,<n>` | target → requester | type 9, one session | 2,912 (the `[ERROR] Result too large` text past it) | 4,548 with 19 peers and 20 reported rows at 4-digit counts; 6,587 with every counter at 2^32-1 and a special peer | in parts, type 32 (below) |
| `?MGMT,ETM,CHAR,<n>` | target → requester | type 10, one session | 2,912 (the `[ERROR]` text) | 3,490 for 19 peers; 5,212 with 5-digit latencies and retries on every row | in parts, type 33 |
| `?MGMT,SEQGET,<n>,<key>` | target → requester | type 16, one session | 2,912: `TOOBIG` for a value over 2,912 - 4 - its key | `<key>,OK,<value>` of 15 + 4 + 3,999 = 4,018 (`SEQ_VALUE_MAX`) | in parts, type 34 |
| `?MGMT,SEQ,<n>` names | target → requester | type 14, or type 29 pages | 64 pages of 2,880 bytes of key text (`WCB_SeqNamesWire.h`) | 29,120 bytes (1,820 keys of 15 characters); 19,623 (6,572 of 1-2) | in pages ([SEQUENCE_INVENTORY.md](SEQUENCE_INVENTORY.md) §3); the WCB relay joins them in one heap buffer of the exact size and says so when the heap has no such block (a list over about 14 KB on an access-point W1) |
| `?MGMT,FRAG` push, one session | requester → target | 226-byte chunks of 179 bytes | 16 chunks, 2,864 bytes (a released relay and target take no more; the Wizard refused a longer push) | a push is the config's diff: up to the whole config, under 142,000 | the Wizard sends it as several sessions of whole commands (`planRelayPush`, below) |
| `?MGMT,FRAG`, one command longer than a session | requester → target | one session | 16 chunks | a `?SEQ,SAVE` of a 15-character key and 3,999 characters: 4,025 bytes, 23 chunks | one long session: a relay forwards 24 chunks (`MGMT_FWD_MAX_CHUNKS`), an ESP32-S3 target takes 24 (`MGMT_RX_MAX_CHUNKS`), and each says so in its WDP capability tags (classic `mgmt.fwd24`, S3 `mgmt.rx24`, which implies forwarding 24), which the Wizard reads; a classic ESP32 cannot run a command that long (a `?SEQ,SAVE` costs about 7 times its length in heap, so it stops near 2,400 characters, under 16 chunks) |
| `?MGMT,FRAG` chunk | requester → relay | one serial line | 179 BYTES (the relay refuses a longer chunk) | - | the Wizard cuts chunks by UTF-8 bytes (`utf8Chunks`); it cut them by JavaScript characters, so a chunk with an `é` in it was 180 bytes and refused. And no chunk but the last ends in whitespace: the chunk ends its relay line, which the relay trims (`processIncomingSerial`, the WebSocket reader), so a cut just after a space dropped it from the command |
| `?MGMT,FRAG` single-chunk ETM shortcut | relay → target | one ETM command | 198 bytes, 186 under `?ETM,CHKSM` | - | the Wizard sends one chunk only up to 186 bytes (`MGMT_SINGLE_MAX`); a longer single command goes as a session of 179-byte chunks |
| The relay's output ring (`s_mgmtOut`) | relay → console | 3 slots of 2,960 bytes | a tag, one reply or one part (2,912) and its CRLF | one reply or one part | bounded: a STATS / ETM part is printed from its session, a joined SEQVAL or names line from its own heap buffer, never the ring |
| WebSocket (`WCB_WS.cpp`) | requester ↔ relay | lines | inbound 1,536 bytes a line (`WS_LINE_MAX`); outbound a 2,048-byte staging buffer flushed inline on the loop task | a relay command line is under 220 bytes; an outbound reply line up to 4,038 (a joined SEQVAL) | bounded inbound; outbound a line longer than the buffer goes out in flushes from the loop task, and is dropped whole only for a client that stopped reading |
| WcbMgmt / MgmtRelay (WCB_Client) | target → requester | one 16-chunk slot | 2,912 a reply | the STATS and ETM sizes above | 1.20.0: `ResultPuller` takes STATS / ETM parts and prints them as the one reply; a `?MGMT` line over 400 bytes is refused (a FRAG line is under 220) |
| `WCB_Client::requestSequence` | target → requester | one 16-chunk reply | 2,912 (`WCB_SEQ_TOOBIG`) | 4,018 | 1.20.0: in parts, joined in one heap buffer of the exact size, CRC-checked, one callback |
| `WCB_Client::requestSequenceNames` | target → requester | pages | as `?MGMT,SEQ` | as `?MGMT,SEQ` | pages (1.19.0), joined in heap (NaviCore has PSRAM) |
| `WCB_Client::send` / `saveSequence` | requester → target | `FRAG_UNICAST` chunks | 16 x 179 = 2,864 (`WCB_MGMT_MAX_COMMAND_LEN`) | a 4,025-byte `?SEQ,SAVE` | not changed: `saveSequence` refuses (returns false) past 2,864. NaviCore never saves sequences; a consumer that does needs the long session too (a product call) |
| `WCB_Client` client-to-client reassembly (`_mgmtBuf`) | client → client | `FRAG_UNICAST` chunks | 16 chunks, 2,864 bytes of BSS | what `send()` sends: 2,864 at most | bounded: the sender's own cap |

### Results in parts (`WCB_ResultWire.h`, packet types 30-34)

`?MGMT,STATS`, `?MGMT,ETM,CHAR` and `?MGMT,SEQGET` share one mechanism, modeled on the names pages. The split, the part
header, the requester's checks and its clock are pure code in `Code/WCB/WCB_ResultWire.h`, a byte-identical copy of
which ships in `WCB_Client` (`src/WCB_ResultWire.h`), host-tested by `tests/result_parts_test.cpp`.

| Type | Size | Direction | What |
|---|---|---|---|
| 30 `RESULT_REQ` | 59 | requester → target | `kind` (1 STATS, 2 ETM), `part`, `id`: id 0 starts a result in parts; any other asks part `part` of the result held under it |
| 31 `SEQVAL_PART_REQ` | 59 | requester → target | `part` and the key (15 bytes, NUL-padded): part 0 is the first request |
| 32 / 33 / 34 | 230 (frags) | target → requester | one part of a STATS / ETM / sequence result, each its own reply (session) |

The 59-byte requests are the size of the single-sequence request, so a released target drops them at its receive
filter (59 bytes of any type but 15, and 28 since paged names) and a released relay drops the 230-byte part types it
does not know. A requester sends the new request three times, 15 ms apart, and the old one (7 / 8 / 15) behind it:

- **an old target** answers the old request, as it always did: whole, or the `[ERROR]` text, or `TOOBIG`;
- **a new target** answers the first new request; the copies and the old one are copies (from the same requester within
  1.5 s - of the END of the answer for STATS, of the request for a sequence, as it was; an ETM copy reaches the run that
  requester started and is dropped, `Duplicate ETM char request`);
- a result that fits one reply goes out as it always has (type 9 / 10 / 16, byte for byte); one that does not goes to a
  requester that asked with a new request as parts, and to an old one as before.

**Part payload:** `<id 4 hex>,<part>,<parts>,<len>,<crc 8 hex>:<data>`. `part` is 0-based, `len` the whole result's
bytes, `crc` its CRC-32 (the config pull's). Joined, the parts' data is exactly the text the one reply carries. A part
holds at most 2,880 bytes of data (26 of header: one reply). STATS and ETM are split just after a `\n` (every line is
under 200 characters), a sequence anywhere but inside a UTF-8 character.

**The target.** A STATS or ETM result is a moment's numbers - built again for each part, the parts would not agree - so
it is **held**, as built (the String the old reply was sent from), one per kind (`s_rjob`), and part 0 sent; a part
request names it by id. A result nobody asked a part of for 10 s is dropped (`serviceResultJobs`); a request for one no
longer held is not answered. An ETM result is held until the load the run started is off the air, as before, then part 0
goes unasked. A sequence is **not held**: each part request reads the value from the store again, and the CRC in every
header says when it changed. Each part is one blocking `sendResultFrags` (two passes, as before): at most 0.75 s of
`loop()` a part.

**The requester** (the WCB relay's `serviceResultPulls`; `WcbMgmt::ResultPuller`; `WCB_Client::requestSequence`), on the
clock in `rparts::Clock`: the first answer (one reply or part 0) is given 4 s, an ETM characterization 180 s; no answer
ends the pull in silence, as the one-reply request always did. Once a part frag is heard the target pages: a part with no
frag for 3 s is asked again, three times at most (part 0 by the id its first frag carries, so the target does not build
or run a new result); 60 s from part 0 caps the pull. Then:

- **STATS and ETM are printed as they come:** part 0 behind its tag, the next ones as they are, the last with the line
  end, one write each - byte for byte the one reply an old relay prints, and nothing held (rule 14). A line another task
  prints can land between two parts, never inside a line. The WCB relay prints each from the part's own session (the rows
  compacted in place, the tag written into the tail of the header); WcbMgmt the same, from its slot.
- **A sequence is joined** in one heap buffer of its exact size (at most 4,038 bytes on the WCB relay, with its tag and
  CRLF) and printed as ONE `[MGMT:SEQVAL,<n>]` line with one write; `WCB_Client` fires `onSequenceValue()` once with the
  whole value and `WCB_SEQ_OK`. A value edited mid-pull (another CRC) starts over, twice at most.
- A pull that cannot finish says why, untagged: `[MGMT] Stats from WCB<n>: part <k> of <K> never came (asked 3 times) -
  the rest is not printed. Ask again.`, `... did not finish in 60 s ...`, `[MGMT] Sequence '<key>' from WCB<n>: ...`;
  `WCB_Client` prints `[WCB_Client] sequence '<key>' from WCB<n>: ... - abandoned` and fires no callback.
- One pull of each kind at a time on a relay; a second replaces the first (`... stopped after part <k> of <K> for a new
  request.` when part of it is out). A paging pull holds a channel-sync search (`chanSyncHostBusy`, `a mesh result
  pull`); an ETM pull waiting minutes for its first part does not, as the one-reply request did not.

| Requester | Target | Fits one reply | Does not |
|---|---|---|---|
| new (this firmware, WCB_Client 1.20.0, NaviCore on it) | new | the old reply type, unchanged | parts, printed / delivered whole |
| new | old (7.0.1 and before) | the old reply | the old answer: `[ERROR] Result too large to relay (...)` / `TOOBIG` |
| old (WCB 7.0.1, WCB_Client 1.19.0 and before) | new | the old reply | the old answer, which the target still prints on its own console |
| old | old | unchanged | unchanged |

**Test knobs** (RAM only, never saved): `?DEBUG,RESULTPART,<n>` (512-2,880; 0 or OFF the default) makes this board, as
a target, send a result over `<n>` bytes in parts of `<n>` to a requester that takes parts - a bench of four boards makes
an ETM result of about 900 bytes; `?DEBUG,ONEREPLY,ON|OFF` makes this board, as a relay, ask the way a relay before
parts did (one old request), to see an old requester's answers.

### A push in sessions (`planRelayPush`, `Wizard/parser.js`)

A push that fits one 16-chunk session is one session, in its own order, as before. A longer one is split at command
boundaries (a stored value never holds `<delim><funcChar>`, so every one is a command boundary the target also sees) into
sessions of at most 16 chunks and 150 commands - the target queues a session's commands at once from its WiFi task,
which never waits for room in the 200-slot queue, so a session of more lost the rest. Across sessions order matters, so
these go in the **last** one, in their order: `?MAC,2|3` and `?EPASS` (a session after them is filtered out), every
`?MAP,PWM`, `?PX` and `?ERASE` (each queues a restart that runs once the queue is quiet for 4 s, which a gap between
sessions could give it), and the `?reboot`. A push is addressed to the number the board has now (its baseline's): it
went to the new one, and never arrived.

**A board-number change** (`?WCB,<n>` with a new n) in a push longer than one session goes in two steps, with a
restart between (Greg, 2026-10-23: "refusing would prevent [changing the number on purpose]"). The board takes
`?WCB,<new>` at once (`saveWCBNumberToPreferences` sets `WCB_Number` live) and from then on reassembles only a session
addressed to the new number (`handleMgmtPacket` checks the number in each chunk), while its radio address - from which
a relay's unicasts and every board's peers are derived - stays the old number's until it restarts (`esp_wifi_set_mac`
from `WCB_Number` at boot). So, through a WCB relay or a WcbMgmt one (NaviCore, MgmtRelay) alike:

- **step 1** is one session to the old number: `?WCB,<new>` and `?reboot` (a one-chunk session: an ETM unicast to the
  board as it is, ACKed);
- the Wizard then reads the relay's `?WDP,DUMP` every 2 s until it has a WCB row for the new number heard since step 1
  went (`_awaitBoardAs`; a board's adverts before its restart carry the new number from the old address, which every
  receiver drops by the sender-MAC binding, so such a row is the board back). The restart waits for 4 s of quiet (20 s
  at most), a boot takes about 5 s and its first advert follows; after 45 s with no such row the push stops, says that
  the rest was not sent and that the board is to be pulled (it is asked for under the new number), and the slot stays;
- **step 2** is the rest of the push as a push of its own to the new number: its own sessions, the late commands last, a
  `?reboot` only if the rest needs one. Every session goes as two chunks at least: a relay sends a one-chunk session as a
  unicast that needs the new number in its peer table (a WCB relay's ETM unicast, `sendESPNowMessage`; a WcbMgmt relay's
  `WCB_Client::send`), which it is only once WDP auto-join has learned the board, while two chunks go as broadcasts the
  board picks by the number in them (a WCB relay) or as raw frames that register the peer on demand (WcbMgmt
  `sendRawPacket`). Every command of step 2 runs under a number and an address that agree.

The one-session renumber is as before: one session to the old number, the `?reboot` at its end. Either way the push
ends with the board shown once, under its new number (`_moveRemoteBoardToNumber`): its config, baseline, relay route,
terminal pane and mesh records move to the new number's slot (the old slot is dropped, or left as an empty placeholder
inside the WCBQ floor), the ETM listener maps a number to its slot (`_slotForWcbNumber`), so `[ETM] WCB<new> came
ONLINE` pulls it, and the stale row every board keeps for the old number surfaces no section and counts as no board
until it is heard again (`_rowLeftByRenumber`). When the new number's slot holds another board, it stays where it is,
with a toast. A two-step push whose step 2 fails moves the board too (it was heard under the new number); one whose
board is never heard back does not. While a push runs, the ETM listener leaves its board alone: a pull then would
replace the config the push is sending, and the two-step restart announces the board mid-push.

**A new number another board already has** - the relay's, another slot's pulled or connected board's, or one the last
`?WDP,DUMP` showed live (`_renumberTaken`) - is not refused: the push asks first, by USB or through a relay, one session
or two (`confirmWcbNumberTaken`): the two boards will share one number and radio address until one is changed, and in
two steps the rest may reach the other board too. Cancel has the focus (Enter, Escape and a click outside cancel too);
Cancel sends nothing.

Between sessions the Wizard waits `max(2.5 s, 250 ms per ?SEQ,SAVE / CLEAR + 25 ms per other command)`: the target runs
what it queued, and a session that lost a chunk is given up (the target's 2 s busy guard) before the next arrives, so one
lost chunk costs that session, as it costs a one-session push today, never the next one too. A session is sent once,
unacknowledged, as before.

One command longer than a session (a `?SEQ,SAVE` of more than about 2,830 characters) goes as one long session of up to
24 chunks (the firmware's `MGMT_FWD_MAX_CHUNKS`, `MGMT_RX_MAX_CHUNKS`), only where the WDP capability tags say it gets
across ([WDP_DESIGN.md](WDP_DESIGN.md) §3): the target's `mgmt.rx<N>` (the ESP32-S3 build sends `mgmt.rx24`) and the
relay's `mgmt.fwd<N>` or `mgmt.rx<N>`, which implies it (the classic build sends `mgmt.fwd24`, the S3 `mgmt.rx24`; a
WcbMgmt relay forwards any count) - `WCBParser.relayLongChunks`, the smaller of the two, when it is past 16. The Wizard
reads the tags from every `?WDP,DUMP` it takes (the 12 s mesh poll, the panel's Refresh; `_meshSeen`): the target's from its row, the relay's from its own row or another board's. A board or relay whose tags it
has not seen yet, or whose firmware sends none, gets one session at most, and a longer command is refused before
anything is sent, as a push over one session was. (It read the firmware's build date until 2026-10-23.) A relay or
target from before forwards or takes 16 at most and drops a longer session whole; a target reassembles a command whole
or not at all (a failed String append used to run it with a chunk missing).

## Revision log

| Date | Commit | Change |
|---|---|---|
| 2026-10-23 | — | §10: a renumber in more than one session goes in two steps with a restart between, through a WCB or a WcbMgmt relay alike: `?WCB,<new>^?reboot` to the old number, then, once the relay's `?WDP,DUMP` shows the board under the new number (`_awaitBoardAs`, 45 s, else the push stops and says so), the rest as a push of its own to the new number, two chunks a session (a one-chunk session needs the new number in the relay's peer table). It replaces the same-day broadcast version, which could not cross a WcbMgmt relay and ran the rest under a number its address did not match. A number another board has is asked about, not refused (`confirmWcbNumberTaken`, Cancel the default), by USB too and for one session too. After a relay renumber the board shows once, under its new number (`_moveRemoteBoardToNumber`). The ESP32-S3 advertises `mgmt.rx24` alone (rx implies forwarding). |
| 2026-10-23 | — | §10: a board-number change too long for one session is no longer refused (Greg: "do in two steps"): `?WCB,<new>` opens the first session, addressed to the old number, and every later session goes to the new number, two chunks at least, since a WCB relay sends a one-chunk session to the address of the number it names, which the board takes only at its restart. Refused only through a WcbMgmt relay (it sends every chunk to that address) or onto a number another board has (every board on it would run the later sessions). The Wizard never ends a chunk but the last on whitespace: the relay trims the end of every line, and a cut just after a space dropped it from the command. |
| 2026-10-23 | — | §10: the long relay session is decided by a capability flag, not a build date (Greg): every WCB advertises WDP capability tags (`mgmt.fwd24`; `mgmt.rx24` on the ESP32-S3, WDP_DESIGN.md §3) and the Wizard sends one command longer than a session as one long session only where the target's `mgmt.rx` and the relay's `mgmt.fwd` cover it (`RELAY_LONG_SINCE` removed; a board whose tags it has not seen gets one session at most). |
| 2026-10-23 | — | §10 new: the audit of everything the relay carries, and every path that could not carry what a board stores now does (Greg: "make sure we can transport everything across the relay"). `?MGMT,STATS`, `?MGMT,ETM,CHAR` and `?MGMT,SEQGET` results longer than one reply come in parts (`WCB_ResultWire.h`, types 30-34, shared with WCB_Client 1.20.0; host test `tests/result_parts_test.cpp`): STATS and ETM held on the target as built and printed by the relay as they come, a sequence read again per part and joined; an old requester or target gets what it always got. The config pull carries 64 parts, not 16 (§3, §8; the largest config is under 142,000 characters, 16 parts were 46,080), its split points kept as back-offs. The Wizard pushes in sessions (`planRelayPush`): chunks by UTF-8 bytes, at most 150 commands a session, the network and restart commands last, a board-number change only in one session, addressed to the board's current number; one long `?SEQ,SAVE` as one session of up to 24 chunks to an ESP32-S3 (`MGMT_FWD_MAX_CHUNKS`, `MGMT_RX_MAX_CHUNKS`). A target runs a pushed command whole or not at all. Test knobs `?DEBUG,RESULTPART`, `?DEBUG,ONEREPLY`. |
| 2026-09-28 | `7ca7e5a` | §9: the WCB-WP25 tests (the restart gate, CHANGED, the dedup window and the parked second requester, the knobs, an oversized result, a wrong target, a reaped session). |
| 2026-09-24 | `337f5df` | Created with F13 (`docs/HIL_TEST_AUDIT.md`, tracker #91): configs over 2912 characters are pulled in parts (packet types 18/19, `?MGMT,PULL,<n>,P`, `[MGMT:CFGPART,`), refusals are coded errors (`[MGMT:CFGERR,`), the target's reply is a non-blocking job, relay lines are one write each, and the Wizard serialises pulls per relay. Verified on the bench (W1 relay, W2 target, NaviCore on the old library) and by host, unit and no-board browser tests. |
| 2026-09-27 | `b57fe2a` | Wizard (docs/hil_plan/WCB.md W-7, W-9): a single `[MGMT:CONFIG,<n>]` reply is CRC-checked before it becomes the config and baseline (unpadded 6.0.x CRCs accepted; a failure is retried, a second non-UTF-8 reply stops the pull), and a relay push over 16 chunks is refused before its network-group confirm and character bootstrap (F14, first half). Tests: `unit/pull.test.js`, `wizard.remote_pull_fake_legacy_crc`, `wizard.push_fake_relay_cap`. |
