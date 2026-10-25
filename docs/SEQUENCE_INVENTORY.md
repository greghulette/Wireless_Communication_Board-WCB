# Stored-Sequence Inventory

How a consumer — NaviCore, a browser UI, another controller — discovers **which stored
sequences exist** on the WCBs in a mesh, pulls any one of them in full, and pushes edits
and new sequences back.

The problem it solves: anything that offers "run a sequence" needs the list of sequences.
Hard-coding it goes stale silently the first time someone saves one from the Wizard.

Three operations, covered in §3 (list), §3a (fetch one), §3b (write). §3c covers how a
recalled sequence runs: nested recall and the cycle guard. §3d covers where sequences are
stored: a file of their own, not NVS.

---

## 1. Why the obvious answers don't work

There were already two ways to ask a board about its sequences. Neither is usable by a
remote consumer.

### `?SEQ,LIST` is not a wire format

`listStoredCommands()` (`WCB_Storage.cpp:712`) is a series of `Serial.printf` calls:

```
--- Stored Commands ---
Key: 'wave' -> Value: ';M1,1^;S5,<CA1021>^***wave and chirp'
--- End of Stored Commands ---
```

There is **no `?SEQ,LIST` packet and no mesh reply** — the full packet-type list is at
`WCB.ino:215-243` and nothing there carries sequence data. Three further traps if anyone is
tempted to scrape it anyway:

- The empty case prints `No stored commands.` with **no header or footer**
  (`WCB_Storage.cpp:717`), so a state machine keyed on the header never terminates.
- Values are `%s`-printed inside single quotes with **no escaping**.
- Output reaches **UART0 only** — `WCBSerial::write` (`WCB_RemoteTerm.cpp:49`) writes to
  `HardwareSerial` plus the optional RTERM mirror, never S1–S5. And over RTERM the line
  buffer flushes at 160 bytes *or* newline, whichever comes first
  (`WCB_RemoteTerm.cpp:68`, `RTERM_TEXT_SIZE` at `WCB_RemoteTerm.h:52`), with no
  continuation marker — so a long sequence value arrives torn into what look like separate
  lines. That failure only shows up on the sequences that matter.

### The config pull carries values, and overflows

`?MGMT,PULL,<n>` does return machine-readable text, and it *does* include sequences as
`?SEQ,SAVE,<key>,<value>` tokens (`collectConfigCommands`, `WCB.ino:3645`). But it carries every
sequence **value** plus the entire board config, and one reply caps at
`MGMT_MAX_CHUNKS` × 182 = **2912 characters** (`WCB.ino:1038`).

A board with ~20 real sequences exceeds that. A pull that asks for parts
(`?MGMT,PULL,<n>,P`, which the Wizard sends) then gets the config in parts of 2880 characters,
joined and CRC-checked by the requester ([MGMT_RELAY.md](MGMT_RELAY.md)). A plain pull gets a
`[MGMT:CFGERR,<n>]NOPARTS` line, and any pull through a relay or from a target that predates
parts still gets **nothing at all**, indistinguishable from a timeout. Either way the whole
config crosses the mesh just to read a list of names; that cost, and the silence on older
boards, are what this feature routes around.

---

## 2. The design: advertise a hash, pull the names

Two halves.

**A 4-byte fingerprint rides the WDP advert.** `WDP_TLV_SEQHASH` (`0x13`) is FNV-1a over
the board's key list (`k1,k2,...,` in save order) **and every stored value**. It changes
whenever a sequence is saved, edited, renamed or erased. Six bytes on the wire, which the
advert can afford.

It covers values, not just names, because editing a sequence in place never touches the
key list — a keys-only hash would leave every peer holding a stale copy while believing
it current, which is the exact failure the fingerprint exists to prevent. It is cached in
RAM and invalidated at each write path: the WDP dirty-check rebuilds the advert twice a
second, and hashing values uncached would mean a walk of the whole store at 2 Hz forever. A
store that cannot be read at that moment (§3d: it will not mount on a low heap) keeps the last
fingerprint and is tried again every 5 s; a value it could not read is never hashed as empty.

**The names are pulled on demand** with a dedicated request/response pair, only when the
hash a consumer sees differs from the hash it cached. A list longer than one reply (from about
180 names) comes a page per request, joined by the requester and delivered whole (§3).

A steady mesh therefore costs **zero** request traffic, and a sequence saved on any board
shows up within a couple of seconds — WDP re-advertises on content change within ~500 ms
(`WCB_WDP.cpp:352`), it does not wait for the 60 s backstop.

### Why names are not simply advertised

The WDP payload is a fixed 200 bytes (`WCB_WDP.cpp:303`) and is already oversubscribed:
alias 26 + fwver 24 + hwver/capflags/ctrlid/flags 13 + maestro & wled cfg 40 + pwmtarget 22
+ five port labels 135 + magic/version/end 4 = **263 bytes worst case**. That is why port
labels are built last and documented as dropped gracefully on overflow
(`WCB_WDP.cpp:276`).

Sequence keys are capped at 15 chars (the **NVS key limit**, which the store keeps), so ~20 names is ~320 bytes —
more than the entire payload. Advertising them would silently evict the port labels, which
drive Maestro/WLED auto-config. `SEQHASH` is placed **before** PORTLABEL in the build order
for the same reason PWMTARGET is: it is functional, labels are cosmetic.

---

## 3. Wire format

A list that fits one reply travels as it always has: one request (13), one reply (14). A list
that does not - from about 180 names, and the store holds a few hundred (§3d) - travels a page
per request (28 / 29), to a requester that asks for pages. The split, the page header and the
join are pure code in `Code/WCB/WCB_SeqNamesWire.h`, a byte-identical copy of which ships in
`WCB_Client` (`src/WCB_SeqNamesWire.h`), host-tested by `tests/seqnames_page_test.cpp`.

### Request — `PACKET_TYPE_SEQ_REQ` (13)

Reuses `espnow_struct_config_req`, 43 bytes (`WCB.ino:1105-1110`). Broadcast, not unicast, so it
works before the target is a registered peer.

```
char    structPassword[40]
uint8_t packetType       = 13
uint8_t targetWCB        // board being asked
uint8_t requesterWCB     // only this requester is answered
```

It has no spare byte, so it cannot name a page: it asks for the whole list in one reply.

Sent **three times** — a first broadcast frame to a given board is routinely dropped on a
busy mesh. The target dedups repeats inside 1.5 s (`answerSeqNames`, below), so three sends
still produce exactly one response.

The request reads the sequence store, so it is **deferred out of the WiFi callback** onto the
loop task via `enqueueMgmtReq` / `drainMgmtReqs`, exactly like CONFIG/STATS/ETM. Running it
inline stalls ESP-NOW and drops other inbound packets. A store it cannot read right then (§3d)
gets **no answer**, not an empty inventory: a `WCB_Client` reads any reply as the inventory. The
handler clears its dedup so the requester's retry is answered.

### Request — `PACKET_TYPE_SEQ_PAGE_REQ` (28): one page

`espnow_struct_seqpage_req`, **59 bytes** (`WCB.ino:1138-1149`) — the size of the
single-sequence request (§3a) on purpose: the page goes where that struct carries its key, and a
target already takes 59-byte requests onto the queue its names handler reads.

```
char    structPassword[40]
uint8_t packetType       = 28
uint8_t targetWCB
uint8_t requesterWCB
uint8_t page             // 0-based
uint8_t reserved[15]     // zero
```

A released target drops a 59-byte frame of any type but 15 at its receive filter (and firmware
before §3a drops every 59-byte frame), so **a requester asks for page 0 with three 28s and then
three 13s** behind them (`seqPullSend`, `WCB_Client::_seqSendNames`): an older target answers a
13; a target with paged names answers the first 28 and takes the 13s for copies of it. Later
pages are asked with 28s alone - only a target that pages sends a page.

### Responses — `PACKET_TYPE_SEQ_FRAG` (14) and `PACKET_TYPE_SEQ_PAGE` (29)

Both reuse `espnow_struct_config_frag`, 230 bytes (`WCB.ino:1152-1161`), through the shared
`sendResultFrags()` helper (`WCB.ino:4851`): at most 16 chunks of **182 usable payload bytes**
(the 183rd is reserved as a NUL terminator; an earlier version used all 183 and dropped one
character per chunk, which ate the comma in `SEQ,SAVE,key,value`), two passes 100 ms apart. So
**one reply carries at most 2,912 characters**, and `sendResultFrags()` refuses anything longer
with an `[ERROR] Result too large to relay (...)` text in its place.

> **Dispatch is by SIZE first, then packetType.** `CONFIG_REQ` and `MGMT_FRAG_UNICAST` are
> both type 5 and are told apart only by 43 vs 226 bytes (`espNowReceiveCallback`). Any
> reimplementation of these structs must match the byte count exactly or the packet routes
> into the wrong handler — or nowhere, with no diagnostic at either end. `WCB_Client` pins
> them with `static_assert`, and both sides assert the page request is 59 bytes with the page
> at byte 43.

### Payloads

The whole list, type 14:

```
<hash8hex>,<count>[,<key1>,<key2>,...]
```

One page, type 29:

```
<hash8hex>,<count>,<page>,<pages>,<keyBytes>[,<key>...]
```

`hash` and `count` are the whole list's in both (a page carries the same pair the one-reply form
would), `page` is 0-based, `pages` is how many there are, and `keyBytes` is the length of every
page's key text together, each key with its comma - so a requester sizes its buffer once, at page
0, and checks the join at the end. Joined, the pages' key text after `<hash>,<count>` is exactly
the type-14 payload.

Comma separation is safe: `saveStoredCommandsToPreferences()` takes a stored key as
everything **before the first comma**, so a key can never contain one.

The empty case is `811C9DC5,0` — the FNV basis and a zero count — so a parser needs no
special case for "board has no sequences".

**Sizing.** A key is 1-15 characters, 2-16 bytes in the list with its comma. The one-reply form
fits while `9 + digits(count) + keyBytes` ≤ 2,912: about 180 names of 15 characters, more when
they are shorter. The store holds 32,768 bytes of `<key>,<value>` lines (§3d): a few hundred
real sequences, and at the extreme - 15-character keys with 1-character values, 18 bytes a line
- 1,820 names, 29,120 bytes of key text, 11 pages.

**The split.** Greedy, in save order: a key goes on the current page while that page's key text
stays within 2,880 bytes (`PAGE_KEYS_MAX`; with the widest header, 28 characters, a page fits
one reply). The target walks the store's keys once per request (`seqStoreForEachKey`, no value
read and no copy of the list held) and keeps only the page asked for, so every page of one list
splits the same way, and the reply costs at most one reply's text twice in heap, whatever the
store holds (CLAUDE.md rule 14; the list built whole cost three copies of it).

### What the target answers (`answerSeqNames`, `buildSequenceNamesReply`)

| Request | The whole list fits one reply | It does not |
|---|---|---|
| 13 (one reply) | type 14, the whole list - as it always was | type 14, `[ERROR] Result too large to relay (<l> chars, max 2912). Reduce stored variables/sequences or query this board directly.` - as it always was - and the target prints `[MGMT] Result too large to relay: ...` naming the requester |
| 28, page 0 | type 14, the whole list - byte for byte the reply to a 13 | type 29, page 0 |
| 28, page *p* > 0 | type 29, page *p* (the list shrank since page 0: *p* past the last page has no keys, and the new hash or `pages` starts the requester over) | type 29, page *p* |

The 13 that does not fit gets the `[ERROR]` text it always got (Greg, 2026-10-23: keep the error):
a WCB relay prints it for the operator. Every `WCB_Client` up to 1.18.1 reads any type-14 reply as
the inventory, so there it reads as hash 0, count 0 - an **empty** list - and 1.19.0 drops it. The
real answer is paging: every requester on the paged firmware or WCB_Client 1.19.0 gets the whole
list however long, and no relay result should ever need the error. A list that fits one reply
but not one page (2,881-2,898 bytes of key text) goes whole to both requests.

**Dedup.** On (requester, page), an old request counting as page 0, within 1.5 s **of the end
of the answer** (a full page takes about 0.75 s in two passes, and the copies queued meanwhile are
still copies). So a page-0 burst of 3 + 3 requests is one answer, a requester walking the pages
back to back is never taken for a retry of the page before, and a retry 3 s on is answered.

### The requester (`?MGMT,SEQ` in `WCB.ino`, `WCB_Client::requestSequenceNames`)

1. Page 0: 28 x3, then 13 x3. A type-14 answer is the whole list: done (the WCB relay prints it
   as it always did; `WCB_Client` checks it whole - 8 hex digits, a comma, the count, exactly
   that many keys of 1-15 characters - and drops anything else, so the `[ERROR] Result too
   large` text of a WCB before paged names is not taken for a list). No answer in 4 s: the pull
   ends silently, as the one-reply request always did - a board that is not there costs no more.
2. A type-29 page 0: size one buffer for the whole list from its header, take its keys, ask for
   page 1 with 28 x3, and so on. A page that brings no frag for 3 s is asked again, at most 3
   times (the target may start answering ~0.5 s after the request, while it sends the last
   page's second pass, and ignores a repeat for 1.5 s after it finished: a retry at 3 s is past
   that). A page whose hash, count, `pages` or `keyBytes` differ from page 0's was built from
   another list (a sequence saved, edited or erased mid-pull): start over at page 0, at most
   twice. A stale copy (not the page asked for) is ignored. 60 s caps the pull.
3. The last page: the joined key text must come to `count` keys and `keyBytes` bytes, else start
   over. Then the list is delivered **once**, in the one-reply shape: the WCB relay prints one
   `[MGMT:SEQ,<n>]<hash>,<count>,<keys>` line with one write; `WCB_Client` fires
   `onSequenceNames()` once with the whole list.

About 0.8 s a page on a quiet mesh, so 300 names (2 pages) take under 2 s and the largest store
(11 pages) about 9 s. A WCB relay holds the list in one heap buffer of its exact size while it
reads it; a list that does not fit the heap is not printed, and it says so.

A pull that cannot finish says why, untagged, and prints no `[MGMT:SEQ,` line: from the WCB relay
`[MGMT] Sequence names from WCB<n>: page <k> never came (asked 3 times) - not printed. Ask
again.`, `... changed 3 times while they were read ...`, `... need <b> bytes, more than the heap
has free in one piece ...` or `... did not finish in 60 s ...`; from `WCB_Client` the same as
`[WCB_Client] sequence names from WCB<n>: ... - abandoned`, with no callback. A `[MGMT:SEQ,<n>]`
line from a relay is therefore always a whole list (or the text a target sent in its place).

One pull at a time on a WCB relay: a second `?MGMT,SEQ` replaces the first (a one-reply answer to
the first is still printed as it comes). A running pull holds a channel-sync search
(`chanSyncHostBusy`, `a sequence-names pull`), and `serviceSeqNamesPull()` runs only while the
radio is home (CLAUDE.md rule 18).

### Who talks to whom

| Requester | Target | List fits one reply | List does not fit |
|---|---|---|---|
| paged (this firmware, `WCB_Client` 1.19.0) | paged | the 28 is answered with type 14 | pages, joined: one line / one callback |
| paged | before paged names (7.0.1 and older) | the target drops the 28 and answers a 13 with type 14 | the target answers the 13 with `[ERROR] Result too large ...` as type 14: the WCB relay prints it as before; `WCB_Client` drops it, no callback |
| before paged names (WCB 7.0.1, `WCB_Client` ≤ 1.18.1, NaviCore main) | paged | a 13, type 14 - unchanged | **no answer**; the target prints why. Never a wrong list |
| before paged names | before paged names | unchanged | unchanged: the `[ERROR]` text, which `WCB_Client` ≤ 1.18.1 reads as an empty list |

Firmware older than the inventory itself answers neither request.

---

## 3a. Fetching ONE sequence

### Request — `PACKET_TYPE_SEQVAL_REQ` (15)

`espnow_struct_seqval_req`, **59 bytes** — its own struct because it must carry the
key, and the 43-byte request has no room.

```
char    structPassword[40]
uint8_t packetType       = 15
uint8_t targetWCB
uint8_t requesterWCB
char    key[16]          // NUL-terminated, ≤15 chars (the NVS key limit)
```

> It cannot instead be an ordinary `?SEQ,GET,<key>` command. A command arriving over
> ESP-NOW does not carry its sender into the firmware's handler — `sourceID` is a
> **serial port** (0 = USB, 1–5) and the ESP-NOW origin is only the
> `lastReceivedViaESPNOW` bool — so the target would not know who to answer.

59 is deliberately distinct from every other packet size (43/55/226/230/243/249/252)
because the receive path routes by size; a `static_assert` pins it on both sides.

Deduped target-side on **(requester, key)** within 1.5 s, not on requester alone —
otherwise a consumer walking a name list back to back would have its second request
mistaken for a retry of the first.

### Response — `PACKET_TYPE_SEQVAL_FRAG` (16)

Same 230-byte frag struct, through the same `sendResultFrags()`. Payload:

```
<key>,<status>,<value>
```

`status` is `OK`, `NOTFOUND`, or `TOOBIG`. The value may itself contain commas, so a
parser takes everything after the **second** comma verbatim.

**A value longer than one reply comes in parts** (`?MGMT,SEQGET` on this firmware, `WCB_Client`
1.20.0; [MGMT_RELAY.md](MGMT_RELAY.md) §10). The requester asks part 0 with a 59-byte
`SEQVAL_PART_REQ` (31: the part and the 15-byte key) three times and the old 15 three times
behind it; a target with parts answers the first 31 and takes the rest for copies, an older one
answers a 15. A reply that fits goes out as type 16, as it always has; a longer one (with a
15-character key, a value over 2,893 characters) goes as type-34 parts,
`<id>,<part>,<parts>,<len>,<crc>:<data>`, the target reading the value from its store again for
every part, the CRC in each header saying when it changed. The requester joins them in one
buffer of the exact length and prints ONE `[MGMT:SEQVAL,<n>]<key>,OK,<value>` line /
fires `onSequenceValue()` once. A requester that cannot take parts still gets `TOOBIG`. A store the target cannot read
right then (§3d) gets no answer at all, and the requester retries: `WCB_Client` reads any
status it does not know as `NOTFOUND`, which would be wrong.

**Nothing acknowledges the reply.** Every chunk is one ESP-NOW broadcast, 20 ms apart, sent
in two passes 100 ms apart (since 2026-10-06, HIL_FIX_TRACKER #109; once before). Lose one chunk
from both passes and the requester gets nothing: no partial value, no error. Its half-built
session is cleared silently after 10 s. So a requester must retry on timeout, at least 1.5 s
after its last request for the same key, or the dedup above eats the retry; a part of a value
in parts is asked again by the requester itself. On the HIL bench, before the second pass,
about 1 in 4 replies of 5-7 chunks was lost on the first try.

**`TOOBIG` is reported explicitly**, to a requester that cannot take parts. `sendResultFrags()`
refuses anything over 16 chunks, so the handler checks the budget first and answers with a
status instead of nothing. Silence is what makes a config pull through older firmware unusable
here (§1) and reintroducing it in the replacement would recreate the very bug this routes around.
One reply allows a 2,903-character value (with a 5-character key), and the store takes up to
3,999 (§3d): past one reply a requester with parts gets the value in parts, an older one
`TOOBIG`. Every board saves the whole 3,999 since 2026-10-24 (§3b: a classic ESP32 stopped near 2,400 characters
before), so a value past one reply is on any board. HIL
`inv.seqget_largest` finds the largest value a board actually stores and round-trips it;
`inv.seqget_parts` pulls a 3,999-character one from W3 (an ESP32-S3) whole, and the old requester's
`TOOBIG` with `?DEBUG,ONEREPLY`.

### Why one at a time

A single value is bounded — at most 3,999 characters, a reply or two. The whole set
is not. A consumer pulls the name list, then walks it:

```
requestSequenceNames(3)  →  "wave,park,scream"
  requestSequence(3, "wave")
  requestSequence(3, "park")
  …
```

No aggregate ceiling, fetch lazily (only the sequence someone actually opened), and
a board that drops mid-walk just resumes. At ~100 ms per round trip, mirroring a
20-sequence board takes 2–3 seconds.

There is deliberately **no** "give me everything" packet. That would need a real bulk
sender, which neither the firmware nor `WCB_Client` has — the library's bulk facility
is receive-only (it parses inbound `bb`/`bc`/`bd` and NACKs; there is no `sendBulk()`).

---

## 3b. Writing sequences

**No new wire format.** A write is an ordinary command — `?SEQ,SAVE,<key>,<value>` or
`?SEQ,CLEAR,<key>` — because everything needed already exists:

- `WCB_Client::send()` fragments anything over one packet (up to
  `WCB_MGMT_MAX_COMMAND_LEN`, 16 × 179 = 2864 chars).
- The firmware's parser has a dedicated `?SEQ,SAVE` branch that does **not** split on
  the `^` delimiters inside a sequence value — it treats only delimiter+funcChar
  (`^?`) as the boundary, because `^;` belongs to the value (`WCB.ino:2015-2048`).
- MGMT reassembly of a client-sent command keeps **unicast** semantics on 6.1.1+
  firmware, so a save lands on that board instead of fanning out.

A second, bespoke write path would drift from the one the Wizard and the console
already use. Don't add one.

**A save holds about 2× its length in heap while it runs**: the port's line buffer and its
queue item while it is queued, the dispatched command and the store's small write buffer while
it runs. `handleSingleCommand` sends a `?SEQ,SAVE` straight to `saveSequenceChars`, which reads the
key and value in place from the command's own text and writes the value to the file from there
(`seqStorePut(key, p, n)`), and the chain splitter queues a `?SEQ,SAVE` token as a slice of its line
(`enqueueCommand(p, n, ...)`). So the whole 3,999 saves on a classic ESP32 hosting its access point
(~19 KB free), over USB or pushed through a relay (it takes a 24-chunk session too: `MGMT_RX_MAX_CHUNKS`,
[MGMT_RELAY.md](MGMT_RELAY.md) §10). Until 2026-10-24 the line was copied at almost every layer of the
dispatcher (the IF check's trimmed copy, `cmd.substring(1)`, `args`, its upper-cased twin, the SEQ
arguments, the save's own), about 7× its length, and a classic board stopped near 2,400 characters with
"Out of memory: could not copy" (HIL `inv.push_long_sequence`, `mgmt.seqval_session_reaped`: "largest
free block 692 bytes" for 2,784).
`?STATS`'s "min free since boot" is the sum of each heap region's own low point, not one
moment's free heap. After such a save it can read near 0 until the next reboot, without the
board ever having been out of memory (tracker #75).

**Key rules** (enforced client-side so a bad write fails loudly instead of silently):
1–15 chars (the NVS key limit, which the store keeps so a sequence can move between the two),
and **no comma** — `saveStoredCommandsToPreferences()`
takes the key as everything before the first comma, so a comma would truncate it on
save and then never match on read. The firmware refuses a longer key on **every** path, not
only on save (`SEQ_KEY_MAX_LEN`, `WCB_Storage.h`): NVS compares only the first 15 characters of
a lookup, so `?SEQ,GET`, `;C` recall, the `SEQGET` relay and `?SEQ,CLEAR` would otherwise read,
run or erase the sequence stored under the 15-character prefix. A longer key is reported as not
found and never reaches the store; on the NVS fallback, erasing by such a name still drops it
from `key_list`, where firmware before d83042e could list a key it had failed to store. The MP3 and DFPlayer
`ONERR` callback key is held to the same 15 characters.

**Two names are reserved.** `key_list` and `seq_mig_done` are the NVS layout's own records, kept
in the `stored_cmds` namespace beside the sequences it holds (§3d: the fallback, and every
firmware before the store). `?SEQ,SAVE`, `?SEQ,CLEAR`, `?SEQ,GET` and a `;C` / `;SEQ` recall
refuse both (`seqKeyReserved`, `WCB_Storage.h`), a recall before its mesh fan-out; `GET` answers
`NOTFOUND`. In that layout a save by either name corrupted the list or the migration flag, a clear
unlisted every sequence or re-armed the legacy migration, and a recall ran the name list as
broadcast text. `?NVS` shows what `stored_cmds` holds (HIL `seq.get_internal_keys`,
`seq.reserved_bookkeeping_keys`).

**A stored value ends only at delimiter + function identifier.** `?SEQ,SAVE`, `?CS` and `?MGMT,`
carry values that may themselves contain the command delimiter and a `;t` token, so only the
splitter that knows that boundary (`parseCommandsNoChecksum`) may walk such a chain. The timer
splitter (`parseCommandGroups`) does not: it cuts on the bare delimiter and reads any `;t` as an
inter-group delay, which truncated the stored value and ran its tail as live commands. Chains
carrying one of those verbs are kept off the timer path by `chainCarriesValueVerb()` (`WCB.ino`) at
every routing gate: `isTimerChain()` for the console and WebSocket reader, both ESP-NOW receive
paths and the reassembly of a multi-chunk `?MGMT,FRAG` or fragmented client unicast, and the
recall's own test in `recallCommandSlot()`. The cost is deliberate: a chain mixing a real `;t`
timer with a `?SEQ,SAVE` loses its inter-group timing, traded against a truncated stored value.
`isTimerChain()` also sends a chain that starts with the function identifier to the timer engine,
but only when a token is a real `<cmdChar>T<digits>` and no token is a checksum (a verified chain
goes through the checksum gate, which the timer path skips). Such a chain used to be refused
outright, so `?VERSION^;S1a^;T800^;S1b` ran both writes together and printed `Invalid Serial
Command` for the `;T`. A received chain longer than a timer-queue slot (219 characters) rides a
heap buffer to `loop()`; one the heap or the full queue cannot take is reported there
(`[TIMER] ... not run`), never cut.

### Confirming a write

There is no per-save reply packet. Confirm either by reading the value back with a
`SEQVAL_REQ`, or — cheaper — by watching the target's `seqHash`: the board
re-advertises within ~500 ms of any write, and **the hash covers values**, so an
in-place edit moves it just as a new key does.

That last point is why the hash is content-aware. A keys-only fingerprint would not
change when a sequence was edited, and every other consumer on the mesh would keep
serving a stale copy while believing it current.

---

### `0` vs `0x811C9DC5`

| `seqHash` reads | Means |
|---|---|
| `0` | The board advertised **no** SEQHASH — firmware predates the TLV. Unknown, not empty. |
| `0x811C9DC5` | The board has **zero** stored sequences (FNV-1a of an empty string). |

Conflating these makes an old board look like an empty one. `?WDP,DUMP` omits the
`[WDPSEQ:...]` record entirely rather than emitting `HASH=00000000`, preserving the
distinction.

The hash is **order-sensitive** (the key list keeps save order). That is intended: it
answers "did *my* inventory change", not "do two boards match".

---

## 3d. Where sequences are stored

**In a file of their own, not NVS** (`WCB_SeqStore.{h,cpp}`). The sequences are the lines of
`/seqs` - `<key>,<value>`, one per line, in save order - in a LittleFS on the 128 KB `spiffs`
data partition that the min_spiffs table carries and nothing else on a WCB uses. NVS keeps
one entry for them, the `stored_cmds/seq_mig_done` marker.

| Limit | Value | Why |
|---|---|---|
| Key | 1-15 characters, no comma | The NVS key limit, kept so a sequence can move between the two stores |
| Value | 1-3,999 characters, no line break | What NVS held (4,000 with the NUL); the file is one sequence per line |
| Store | 32,768 bytes of lines (`SEQ_FILE_MAX`) | A config pull carries at most 16 parts of 2,880 bytes (46 KB, `WCB_ConfigParts.h`), every sequence riding it as a `?SEQ,SAVE` token beside the other settings: at 32 KB a full store still pulls. It is also under half the partition, which a replace needs (below) |

`?NVS` ends with one line on it: `Sequences: <n> in the sequence store, <bytes> of 32768 bytes
(partition spiffs: <used> of <total> bytes used)`.

**Writes.** A new key appends one line. LittleFS commits a write only when the file is
closed, and not at all once a write to it failed, so a reset mid-append leaves the file as it
was. Replacing or removing a sequence writes the whole file to `/seqs.tmp` and renames it over
`/seqs`: a reset leaves the old store or the new one, never half of each. That copy needs room
for a second file, so a full store can still remove a sequence. A re-save of the value already
stored writes nothing, so a re-pushed config costs reads only. A replace keeps the key's place;
a clear and re-save moves it last - the same order rules the NVS key list had, so the hash is
byte-identical across the move from NVS.

**Mounted for each call, never between calls.** A mount holds 1.7 KB of heap (measured on
the bench's classic boards, access point up) and each open file about 0.7 KB more, a tenth of
what such a board has free (CLAUDE.md rule 14). Each call mounts (about 12 ms) and unmounts as
it returns. Held for 3 s after a call instead, a `?backup` left it mounted under a 250-token
one-line restore, which lost four tokens to `Out of memory while enqueuing command!` (HIL
`backup.one_line_restore`). Below 6 KB of free heap it refuses to mount, and the caller says
so: a save prints `Failed to store sequence '<key>' (out of memory)`, a recall `Could not read
sequence`, a mesh request goes unanswered until its retry. Every call is from the loop task;
never from the ESP-NOW receive callback.

**Reads.** A recall, `?SEQ,GET`, `?SEQ,LIST`, `?config`, the config walk (`?backup`, the config
pull) and the hash are each a walk of the file, with no copy of it in RAM: a value is held only
while it is used, and the hash reads none. A value the heap cannot hold is a lost token to the
config walk (F19): `?backup` warns, a pull answers `NOMEM`. A line no key can address (no
comma, or a key over 15 characters) is skipped, and the next replace or remove drops it.
`?SEQ,NAMES` and the names reply (§3) walk the keys alone (`seqStoreForEachKey`): no value is
read and no copy of the key list is held. `?SEQ,NAMES` counts first, then prints the line from
one buffer of its exact size with one write (streamed in pieces if the heap has no such buffer).
On the NVS fallback the same walk skips an empty `key_list` entry and one longer than 15
characters, which no call can address.

**The settings store still decides when sequences are erased.** `?ERASE,NVS` and
`?SEQ,CLEAR,ALL` clear the file too. A factory reset that blanks only NVS - the Wizard's writes
0xFF over the NVS partition and leaves `spiffs` alone - takes the `seq_mig_done` marker with it,
and a boot that finds the file without the marker clears it: `[SEQ] The settings store was
erased since these sequences were saved (a factory reset): cleared them too.` The marker is
written at every boot that lacks it (`migrateOldStoredCommands`), so only a wipe removes it.

**The NVS fallback.** A boot that cannot mount the partition runs the store on the old NVS
layout (`key_list` plus one string per key) for that boot, and says why:
`[SEQ] Stored sequences are kept in the settings store this boot: <why>`. The reasons: no
`spiffs` partition (a build with another partition scheme), the last boot crashed while mounting
it, or `?DEBUG,SEQNVS` asked for it (a test knob, RTC memory, one boot). The crash check exists
because the core builds LittleFS with its asserts on: a damaged file system can `abort()` inside
the mount, and every later boot would mount it and crash the same way. An RTC word holds a
marker while a mount runs; a boot after a panic or a watchdog reset that still finds it skips
the mount. `?SEQ,CLEAR,ALL` on a fallback boot formats the file store, which is how a damaged
one is started over.

**Moving NVS sequences in.** Every boot that mounts moves whatever NVS still lists into the
file, in one rewrite, then reads each back before removing it from NVS: `[SEQ] Moved <n>
sequence(s) from the settings store into the sequence store.` That is how the first boot of this
firmware on a board moves every sequence it has, and how a fallback boot's saves come back. NVS
wins a key both hold, in the file's place: NVS only holds a sequence while the file exists when
a fallback boot or an older firmware saved it, which is newer. A listed name with no value is
dropped from the list; a value the file cannot take (a line break, or no room) stays in NVS,
unused, and the boot says so. A reset anywhere in the move loses nothing: the next boot moves
again.

**Going back to firmware before the store** shows no sequences: they are in the file, which
older firmware does not read. Restore them from a backup there. Coming forward again, the file
is still there, and what the older firmware saved in NVS moves in beside it.

## 3c. Running sequences: nested recall and the cycle guard

**A nested `;C` is enqueued, not run on the C++ stack.** `recallCommandSlot()` expands a body
into the command queue and returns, so a recall inside a body runs when its queue item is
dispatched, and the body it expands drains *behind* whatever was already queued. A body's
own commands therefore run before the bodies it calls: `;CBEEP^;S1x` prints `x` first.

**Each queued command carries its lineage**: the keys whose expansion produced it, outermost
first. `CommandQueueItem::seqDepth` counts them, and the entries themselves are FNV-1a 32
hashes of the exact key bytes (`seqKeyHash()`), stored in front of the command text in the
same allocation. The `loop()` drain restores an item's lineage into `seqCurPath` before
dispatching it. `recallStoredCommand()` then:

- refuses a key already on that lineage: `Sequence '<key>' recalls itself (directly or in a loop) — refusing to expand it again. Break the cycle in the stored sequence.`
- refuses at depth 8 (`SEQ_MAX_NESTING`): `Sequence nesting deeper than 8 — refusing to expand '<key>'.`

`recallCommandSlot()` pushes the key while it enqueues the body. A body containing `;t` goes
to the timer splitter, which captures the lineage in `commandGroupsPath` and re-applies it as
each group fires, so the lineage survives the delay.

As a result, `A→A`, `A→B→A` and a `;t`-paced self-recall (`;S1x^;t200^;CSELF`) are refused,
while the same sub-sequence can be called any number of times, at any depth. The key compare is
case-sensitive like the NVS lookup, and a hash collision can only over-refuse, never miss a cycle.
Every endless expansion either repeats a key on some lineage or passes depth 8, so the
guard also stops the queue refilling forever.

A top-level recall, a recall received over the mesh, and an MP3/DFPlayer `ONFIN`/`ONERR` body
each start a fresh lineage. A ring that crosses boards (`;W2,;C…` on both sides) is invisible
to any one board's guard.

**A nested expansion must fit, whole.** Back-to-back calls with no `;t` between them expand
every call before any leaf runs, because the queue is FIFO. `recallCommandSlot()` therefore
refuses a *nested* expansion (lineage depth > 0, no `;t` in the body) when its token count plus
`SEQ_QUEUE_RESERVE` (16) exceeds the free slots of the 200-slot queue. It prints one line,
`Command queue nearly full — not expanding nested sequence '<key>'. Space repeated calls with ;T.`,
and enqueues nothing. Without that check a full queue printed `Command queue is full!
Discarding command.` once per dropped token on UART0, which has no TX buffer. That stalled
`loop()`, dropped console commands and cut bodies short. A top-level recall (`;C`, `ONFIN`,
`ONERR`) must fit too, with no reserve: one whose token count exceeds the free slots is refused
whole, with one line (`Sequence '<key>' has <n> commands and the command queue has room for
<m> — not run.`), where it used to run its first part and drop the rest one line per token. A
body over 200 commands can therefore never run as one recall; split it with `;T`, whose groups
enqueue one at a time and are not checked. The serial reader, a task of its own, waits up to
2 s per command for queue room instead, so a pasted `?backup` of more than 200 tokens runs
whole; the loop task, which drains the queue, and the WiFi task never wait.

**Only one timer chain runs per board.** A called sequence that contains `;t` replaces the chain
that is running (`parseCommandGroups()`, "Timer sequence replaced mid-run"), so the caller's
remaining timed steps are dropped. Reused sub-sequences should not contain `;t`; the caller
should hold the delays.

Constraints:

- `seqCurPath` is **loop-task only**. `enqueueCommand()` stamps a lineage only when
  `seqOnLoopTask()` is true, and `seqLoopTask` is captured first thing in `setup()`. A command
  enqueued by `serialCommandTask` or the WiFi callback is top-level by construction. Off the
  loop task, `parseCommandGroups()` writes only `commandGroupsPath.depth` (0), so a concurrent
  copy on the loop task is always a consistent path. Moving the drain, the recall or timer firing
  off the loop task would empty every lineage, and `A→A` would run away again.
- Anything that dequeues `commandQueue` frees `commandItemBlock(item.cmd, item.seqDepth)`,
  **never** `item.cmd`, which points past the lineage.
  `grep -n "free(.*\.cmd)" Code/WCB/WCB.ino` must find nothing.
- Never go back to a global key set, and never pop one "when an expansion drains". In
  `;CBEEP^;CBEEP` the second call is dispatched while the first BEEP's body is still queued
  behind it, so it would still be refused. The stack has to belong to each queued item.

HIL: `seq.cycle_guard` (refusals and the depth limit), `seq.cycle_guard_case`,
`seq.cycle_guard_reuse` (reuse at every shape, plus a `;t`-paced self-recall), and
`seq.reuse_queue_reserve` (the nested-expansion reserve).

---

## 4. Using it

### From a WCB console

```
?SEQ,NAMES            # this board — one parseable line
?SEQ,GET,wave         # this board — one sequence
?MGMT,SEQ,3           # WCB3's names, over the mesh
?MGMT,SEQGET,3,wave   # WCB3's 'wave' sequence
?SEQ,SAVE,wave,…      # write (already existed)
?SEQ,CLEAR,wave       # delete (already existed)
```

Both produce the same shape, so a host parsing a board's console uses one parser for local
and remote:

```
[MGMT:SEQ,3]A1B2C3D4,3,wave,park,scream
[MGMT:SEQVAL,3]wave,OK,;M1,1^;S5,<CA1021>^***wave and chirp
```

A list that came in pages is printed the same way, one line, once the last page is in (§3,
about 0.8 s a page). A `?MGMT,SEQ` that cannot finish prints an untagged `[MGMT] Sequence names
from WCB<n>: ...` line saying why, and no `[MGMT:SEQ,` line. With `?DEBUG,MGMT` the target logs
`[MGMT] Sequence-names request from WCB<n>` for page 0 and `... (page <k>)` for each later one.

`?WDP,DETAIL,<n>` shows a neighbor's hash, and `?WDP,DUMP` emits it as its own record —
`[WDPSEQ:N=3,HASH=A1B2C3D4]` — deliberately **not** as a new field on the `[WDP:...]` line,
whose field order is load-bearing for older Wizard regexes.

### From `WCB_Client` (NaviCore, or any ESP32 on the mesh)

```cpp
// 1. What exists
wcb.onSequenceNames([](uint8_t n, uint32_t hash, uint16_t count, const char* names) {
    // names = "wave,park,scream" — fires on the LOOP task, real work is safe here
});

// 2. What's in one of them
wcb.onSequenceValue([](uint8_t n, const char* key, WCBSeqStatus st, const char* value) {
    if (st == WCB_SEQ_OK) { /* value = ";M1,1^;S5,…" */ }
    // else WCB_SEQ_NOTFOUND / WCB_SEQ_TOOBIG (only from a WCB older than values in parts) —
    // always check before trusting value
});
wcb.requestSequence(3, "wave");

// 3. Push edits back
wcb.saveSequence(3, "wave", ";M1,1^;S5,<CA1021>");
wcb.deleteSequence(3, "oldname");

// Refresh only when the advertised fingerprint moves:
const WCBNeighbor* nb = wcb.getNeighbor(3);
if (nb && nb->seqHash != 0 && nb->seqHash != cachedHash)
    wcb.requestSequenceNames(3);
```

Constraints worth knowing:

- **One request in flight at a time**, shared between `requestSequenceNames()` and
  `requestSequence()` — they use the same reassembly slot. A second call while one is
  pending returns `false` rather than corrupting it. Poll `sequenceNamesPending()`, which
  gates either kind. Issuing the next request *from inside* a callback is fine: the
  library clears its in-flight state before firing.
- **Writes are fire-and-forget** — no reply packet. Confirm via `seqHash` or by reading
  back.
- The ~3 KB reassembly buffer is **heap-allocated only while a request is in flight** and
  freed on delivery or timeout — a library this widely included shouldn't cost every sketch
  3 KB of BSS for a feature most don't use.
- A request with no answer within ~4 s is **abandoned silently**. No callback
  fires: a half-list delivered as a result would look like a real answer.
- **A list in pages still fires the callback once, with the whole list** (1.19.0). The library
  asks for each page itself, retries a page, starts over on a change (§3), and only then calls
  `onSequenceNames()`. Once, because every consumer treats a call as the inventory: NaviCore
  replaces its cached list on each one, so a page at a time would cache part of a list as all
  of it, and the callback's signature (`count`, `names`) already carries a whole list. The cost
  is the joined list in heap for the length of the pull, its exact size (about 5 KB for 300
  names; NaviCore has PSRAM). Use `sequenceNamesPending()`, not a fixed timeout, to tell a long
  pull from a lost one: NaviCore's `GET_WCB_SEQ` waits on it past its 6 s.
- A pull that cannot finish (a page asked 3 times that never comes, a list that changed 3
  times, no heap for the list, 60 s) is abandoned with a `[WCB_Client] sequence names ...` line
  on `Serial` and no callback; so is a one-reply answer that is not a list (an older WCB's
  `[ERROR] Result too large` text, which 1.18.1 and earlier delivered as an empty list).
- **A value longer than one reply comes in parts and the callback fires once** (1.20.0, §3a):
  `onSequenceValue()` gets `WCB_SEQ_OK` and the whole value, CRC-checked; the joined reply is
  held in heap for the pull, its exact size (about 4 KB at most). `sequenceNamesPending()` stays
  true while it pages. A pull that cannot finish is abandoned with a `[WCB_Client] sequence
  '<key>' ...` line and no callback.
- Interception of `SEQ_FRAG` is armed only once `onSequenceNames()` is registered. Until
  then those packets fall through to `onRawPacket()` exactly as before, so existing sketches
  (NaviCore's OTA raw hook) are unaffected.
- `requestSequenceNames()` requires the callback to be registered first, and returns `false`
  if it isn't.

Full worked example: `WCBClient/examples/SequenceInventory`.

---

## 5. Pointers into the code

- Inventory + hash (one key walk and one cached hash for both the console command and the mesh
  reply): `Code/WCB/WCB_Storage.cpp` — `buildSequenceNamesReply()` (the reply to a request: whole,
  one page, or none), `printSequenceNames()` (`?SEQ,NAMES`), `sequenceInventoryHash()`,
  `invalidateSequenceInventoryHash()`; `seqStoreForEachKey()` (`WCB_SeqStore.cpp`).
- The pages (§3): `Code/WCB/WCB_SeqNamesWire.h` — `seqn::Pager` (the split), `formatPageHeader` /
  `parsePageHeader`, `parseWhole` (the strict one-reply parse), `seqn::Assembly` (the join), the
  packet types, sizes and requester timing; byte-identical in `WCBClient/src/WCB_SeqNamesWire.h`.
  Host test: `tests/seqnames_page_test.cpp`. Target: `answerSeqNames()` (dedup, the decision),
  `handleSeqReqPacket()` / `handleSeqPageReqPacket()`; requester: `handleMgmtSeqRequest()`,
  `seqPullSend()`, `handleSeqFragPacket()` (types 14 and 29, WiFi task), `seqPullTakePage()` and
  `serviceSeqNamesPull()` (loop) in `Code/WCB/WCB.ino`; `WCB_Client::_seqSendNames`,
  `_maybeHandleSeqFrag`, `_seqNamesPage`, `_seqTick`. HIL: `inv.mgmt_seq_paged` (s17),
  `ncmesh.seq_pull_paged` (s43).
- Where sequences are stored (§3d): `Code/WCB/WCB_SeqStore.{h,cpp}` — `seqStoreBegin()` (mount,
  factory-reset check, `moveNvsSequences()`), `seqStoreGet/Put/Remove/Clear/ForEach/ForEachKey/KeyList/Hash`,
  `SeqLock` (the per-call unmount), `seqStoreReport()` (`?NVS`), the NVS fallback (`nvs*`).
  Callers: `recallCommandSlot()`, `saveStoredCommandsToPreferences()`,
  `eraseStoredCommandByName()`, `listStoredCommands()`, `clearAllStoredCommands()`
  (`WCB_Storage.cpp`); `collectConfigCommands()`, `printConfigInfo()`, the `?SEQ` handler,
  `answerSeqNames()`, `handleSeqValReqPacket()` (`WCB.ino`).
- Packets, handlers, `?MGMT,SEQ`, `?SEQ,NAMES`: `Code/WCB/WCB.ino`.
- TLV advertise/decode + `?WDP` display: `Code/WCB/WCB_WDP.cpp`, `WdpNeighbor::seqHash` in
  `WCB_WDP.h`.
- Client library: `WCBClient` repo, `src/WCB_Client.{h,cpp}` — `onSequenceNames`,
  `requestSequenceNames`, `sequenceNamesPending`, `WCBNeighbor::seqHash`.
- Wire test: `tests/wdp_wire_test.cpp` → `test_seqhash()`.
- Nested recall and the cycle guard (§3c): `Code/WCB/WCB.ino` — `recallStoredCommand()` (the
  guard), `enqueueCommand()` (stamps the lineage), `CommandQueueItem::seqDepth` +
  `commandItemBlock()`, the `commandQueue` drain in `loop()` (restores `seqCurPath`),
  `seqOnLoopTask()`; `Code/WCB/WCB_Storage.cpp` — `recallCommandSlot()` (push, queue reserve),
  `seqKeyHash()`; `Code/WCB/WCB_Storage.h` — `SeqPath`, `SEQ_MAX_NESTING`,
  `SEQ_QUEUE_RESERVE`; `Code/WCB/command_timer.cpp` — `commandGroupsPath`.

---

## 6. Revision log

| Date | Change | Commit |
|---|---|---|
| 2026-10-23 | §3a: a value longer than one reply comes in parts (`SEQVAL_PART_REQ` 31, `SEQVAL_PART` 34, `WCB_ResultWire.h`, shared with `WCB_Client` 1.20.0; [MGMT_RELAY.md](MGMT_RELAY.md) §10), joined by the requester and delivered once; `TOOBIG` only to a requester that cannot take parts. The SEQVAL dedup is on (requester, key, part), an old request counting as part 0. HIL `inv.seqget_parts`, `inv.push_long_sequence`; `inv.seqget_largest` expects the value whole past one reply. | — |
| 2026-10-23 | §3: an old one-reply request for a list too long for one reply gets the `[ERROR] Result too large to relay` text again instead of no answer (Greg: keep the error). | — |
| 2026-10-24 | §3a, §3b: a `?SEQ,SAVE` reads its key and value in place and writes the value from there (`saveSequenceChars`, `seqStorePut(key, p, n)`, the splitter's slice enqueue), about 2× its length in heap instead of 7×: the whole 3,999 saves on a classic ESP32 hosting its access point, over USB and through a relay (24-chunk sessions on both chips). Greg: "it should work as long as it has enough room to store it in the board". HIL `inv.push_long_sequence` now stores it on W2. | — |
| 2026-10-22 | §3: the names list in pages. One reply carries 2,912 characters, about 180 names, and the store holds a few hundred, so a longer list could not be listed over the mesh at all (`?MGMT,SEQ` printed `[ERROR] Result too large to relay`, which `WCB_Client` read as an empty list). A requester now asks for page 0 with a 59-byte `SEQ_PAGE_REQ` (28) ahead of the old 13; a list that fits still goes as type 14, one that does not goes a page per request as type 29 (`<hash>,<count>,<page>,<pages>,<keyBytes>,<keys>`, greedy pages of up to 2,880 bytes of key text, `WCB_SeqNamesWire.h`, shared with `WCB_Client` 1.19.0), and the requester joins them and prints one line / fires the callback once. An old one-reply request for a list that does not fit is not answered. The dedup is on (requester, page), timed from the end of the answer. The target and `?SEQ,NAMES` walk the keys alone (`seqStoreForEachKey`) instead of building the list three times in heap. §3's sizing (80 keys, `MAX_STORED_COMMANDS`) predated the file store and is replaced. Host test `tests/seqnames_page_test.cpp`; HIL `inv.mgmt_seq_paged`, `ncmesh.seq_pull_paged`. | — |
| 2026-09-29 | §3d: the store is mounted for each call and unmounted as it returns. Held for 3 s after a call, a `?backup` left its 1.7 KB in use under a 250-token one-line restore, which lost four tokens to out-of-memory (HIL `backup.one_line_restore`, run `20260929-194057`). Bench-verified: a full store (32,727 bytes, 36 sequences) still pulls whole, in 12 of 16 parts. | `8e5d24f` |
| 2026-09-29 | §3d: sequences move out of NVS into a file of their own, `/seqs` on the 128 KB `spiffs` partition (`WCB_SeqStore`), capped at 32 KB so a full store's config still pulls; NVS keeps the `seq_mig_done` marker, whose absence clears the file (a factory reset that blanks only NVS). Every boot that mounts moves what NVS lists into the file; a boot that cannot mount runs on the NVS layout (`?DEBUG,SEQNVS` forces one). `?SEQ,GET` answers `NOTFOUND` for `key_list` and `seq_mig_done`; a mesh request the store cannot answer right then gets no reply; a value that cannot be read is never hashed. §3a `TOOBIG` is reachable now that the store takes 3,999 characters. | `fecd481` |
| 2026-09-27 | §3c: a top-level recall that cannot fit the command queue is refused whole, and the serial reader waits for queue room instead of dropping tokens (WCB coverage re-scan #24). | `cc2a8a9` |
| 2026-09-27 | §2: `key_list` and `seq_mig_done` are refused as keys by save, clear and recall (WCB coverage re-scan #14). The timer-path paragraph names the gates as they are now: one `isTimerChain()` for four paths, which also takes a `?`-first chain with a real `;T` token and none with a checksum (#10), and a long received chain is carried whole or reported (#22). | `cc2a8a9` |
| 2026-09-24 | §1: a pull that asks for parts (`?MGMT,PULL,<n>,P`) now gets a config over 2912 characters in parts; a plain pull gets CFGERR NOPARTS; older relays and targets stay silent (F13, tracker #91, MGMT_RELAY.md). | `337f5df` |
| 2026-09-24 | §1: the config pull's too-large case is no longer silent on the target, which measures the config first and prints why, ungated; the requester still gets nothing (tracker #90). Line references in §1 updated. | `337f5df` |
| 2026-09-23 | New §3c: the cycle guard is a per-item lineage (`seqDepth` + hashes in front of the queued text, restored into `seqCurPath` at drain, pushed by `recallCommandSlot()`, carried across `;t` by `commandGroupsPath`). It replaces the never-popped `activeChainKeys` set, which refused every second call to a sub-sequence in one run and counted a flat body's calls toward the depth limit (tracker #47). A nested expansion that would leave fewer than 16 queue slots free is refused whole, with one line, rather than flooding UART0 with one "queue is full" line per token. Refusal texts and the 8-level limit are unchanged. HIL `seq.cycle_guard_reuse`, `seq.reuse_queue_reserve`. | `95dd811` |
| 2026-09-22 | §3a says the SEQVAL reply is sent once and unacknowledged, so a requester must retry on timeout. The first run of `inv.seqget_largest` lost a 7-chunk reply that way (tracker #71). §3a also notes how rarely `TOOBIG` can occur. NVS refuses values long before the 2,903-character relay budget, and a ~2,950-character `?SEQ,SAVE` fails even earlier, on heap. The "38,900-byte largest block" that made that look like a mystery was the classic ESP32's 32-bit-only IRAM heap. ?STATS and the out-of-memory line now report byte-addressable heap (tracker #58). | — |
| 2026-09-20 | A chain carrying `?SEQ,SAVE`, `?CS` or `?MGMT,` never reaches the timer splitter: the exemption is token-aware (`chainCarriesValueVerb`) instead of a first-character test, so a chain beginning with any other token no longer has its stored value cut at the first `;t` with the tail executed. |
| 2026-09-15 | Keys longer than 15 characters are refused on read, recall, the `SEQGET` relay and erase, not only on save. NVS matches a lookup on its first 15 characters, so `?SEQ,GET,HILABCDEFGHIJKLM` returned the value stored under `HILABCDEFGHIJKL`, and a clear by the long key would have erased it (found by the HIL test `seq.key_len_15`). | `eb82f29` |
| 2026-08-17 | Added the **read-one** and **write** halves: `PACKET_TYPE_SEQVAL_REQ`/`SEQVAL_FRAG` (15/16) with a 59-byte keyed request struct, `?SEQ,GET,<key>`, `?MGMT,SEQGET,<n>,<key>`, and `requestSequence`/`onSequenceValue`/`saveSequence`/`deleteSequence` in `WCB_Client`. `TOOBIG`/`NOTFOUND` are explicit statuses rather than silence. Writes reuse the existing `?SEQ,SAVE` command path — no new wire format. **SEQHASH now covers stored values**, so an in-place edit is visible to peers (see `WDP_DESIGN.md`). `MgmtReqSlot` widened to the largest request on that queue and given a `len`, since SEQVAL_REQ is 59 B against the previous 43. | `80f44d9` |
| 2026-08-17 | Initial version. `PACKET_TYPE_SEQ_REQ`/`SEQ_FRAG` (13/14), `?SEQ,NAMES`, `?MGMT,SEQ,<n>`, WDP TLV `0x13` SEQHASH, `[WDPSEQ:]` dump record, and the `WCB_Client` `onSequenceNames`/`requestSequenceNames` API. | `bbd6bdf` |
