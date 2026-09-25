# MGMT relay: the remote config pull

How a requester (the Wizard, Intellex, the HIL harness, or a person at a terminal) reads the saved config of a board
it has no cable to. The requester talks to a **relay** (a WCB, or a WCB_Client host running WcbMgmt such as NaviCore
or the MgmtRelay example) over USB serial or a WebSocket; the relay talks to the **target** over ESP-NOW.

The other `?MGMT` verbs (STATS, ETM, SEQ, SEQGET, FRAG) share the relay's fragment machinery; sequences have their own
page, [SEQUENCE_INVENTORY.md](SEQUENCE_INVENTORY.md). The Wizard's relay **push** is a separate path with its own
limit (see Limits).

## 1. The rule everything else serves

**A `[MGMT:CONFIG,<n>]` line carries a board's whole config or nothing.** Every Wizard released before 2026-09-24,
including the copies frozen inside each Intellex install, stores ANY non-empty body under that tag as the board's
config and baseline, without checking its `^?CHK`. A part, an error text or a truncated config printed under that
tag would be written back to the board on the next push. So:

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
the part count (at most 16). `data` is bytes `[split(k-1), split(k))` of the reply. The part data size D is 2880:
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
| TOOBIG | more than 16 parts (L > 46080; NVS cannot hold that) | stops |

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
  shim calls `window.remoteBoardPull(slot, n, 1, 3, cb)` behind a 45 s watchdog);
- one attempt on the air per relay (a per-relay FIFO), because every relay has a single reassembly session;
- a 6 s attempt timer that restarts on every new part; `PULL_DEADLINE_MS` (40 s) from the call caps a pull of up to
  3 attempts, queue wait included;
- NOMEM, CHANGED, NOPARTS, a CRC failure and an empty reply are retried; TOOBIG stops at once; a joined text that is
  not valid UTF-8 is retried once and stops on a second job's;
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
  receive masks and more static RAM on every board.
- A pull is at most 16 parts of 2880 bytes (46080 characters).
- The Wizard's relay **push** is still capped at 16 x 179 = 2864 characters of changes per push, including an
  appended `?reboot`; a board pulled in parts may need a push split into several, or USB.

## 9. Tests

Host: `tests/config_parts_test.cpp`. Wizard: `tests/wizard/unit/pull.test.js`, the no-board spec
`tests/wizard/specs/remote_pull_fake.spec.js` (`wizard.remote_pull_fake_*`), and `wizard.remote_pull`,
`wizard.remote_pull_parts` on the bench. HIL (`tests/hil/suites/s03_wcb.py`, `s21_navicore_sbus.py`):
`wcb.pull_size_limit`, `wcb.pull_plain_over_limit`, `wcb.pull_parts_many`, `wcb.pull_error_oom_legacy`,
`wcb.pull_error_oom_parts`, `wcb.pull_nonblocking`, `navicore.mgmt_pull`, `navicore.pull_over_limit`.

## Revision log

| Date | Commit | Change |
|---|---|---|
| 2026-09-24 | _(pending)_ | Created with F13 (`docs/HIL_TEST_AUDIT.md`, tracker #91): configs over 2912 characters are pulled in parts (packet types 18/19, `?MGMT,PULL,<n>,P`, `[MGMT:CFGPART,`), refusals are coded errors (`[MGMT:CFGERR,`), the target's reply is a non-blocking job, relay lines are one write each, and the Wizard serialises pulls per relay. Verified on the bench (W1 relay, W2 target, NaviCore on the old library) and by host, unit and no-board browser tests. |
