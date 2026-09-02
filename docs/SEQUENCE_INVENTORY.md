# Stored-Sequence Inventory

How a consumer — NaviCore, a browser UI, another controller — discovers **which stored
sequences exist** on the WCBs in a mesh, pulls any one of them in full, and pushes edits
and new sequences back.

The problem it solves: anything that offers "run a sequence" needs the list of sequences.
Hard-coding it goes stale silently the first time someone saves one from the Wizard.

Three operations, covered in §3 (list), §3a (fetch one), §3b (write).

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
`?SEQ,SAVE,<key>,<value>` tokens (`WCB.ino:2855-2871`). But it carries every sequence
**value** plus the entire board config, and the whole thing caps at
`MGMT_MAX_CHUNKS` × 182 = **2912 characters** (`WCB.ino:803`).

A board with ~20 real sequences exceeds that. When it does, the target sends **nothing at
all**, and the "Config too large" diagnostic is behind `debugMGMT` (`WCB.ino:2959`) — so
from the consumer's side it is indistinguishable from a timeout. This is the trap this
feature exists to route around.

---

## 2. The design: advertise a hash, pull the names

Two halves.

**A 4-byte fingerprint rides the WDP advert.** `WDP_TLV_SEQHASH` (`0x13`) is FNV-1a over
the board's NVS `key_list` **and every stored value**. It changes whenever a sequence is
saved, edited, renamed or erased. Six bytes on the wire, which the advert can afford.

It covers values, not just names, because editing a sequence in place never touches
`key_list` — a keys-only hash would leave every peer holding a stale copy while believing
it current, which is the exact failure the fingerprint exists to prevent. It is cached in
RAM and invalidated at each write path: the WDP dirty-check rebuilds the advert twice a
second, and hashing values uncached would mean N NVS reads at 2 Hz forever.

**The names are pulled on demand** with a dedicated request/response pair, only when the
hash a consumer sees differs from the hash it cached.

A steady mesh therefore costs **zero** request traffic, and a sequence saved on any board
shows up within a couple of seconds — WDP re-advertises on content change within ~500 ms
(`WCB_WDP.cpp:352`), it does not wait for the 60 s backstop.

### Why names are not simply advertised

The WDP payload is a fixed 200 bytes (`WCB_WDP.cpp:303`) and is already oversubscribed:
alias 26 + fwver 24 + hwver/capflags/ctrlid/flags 13 + maestro & wled cfg 40 + pwmtarget 22
+ five port labels 135 + magic/version/end 4 = **263 bytes worst case**. That is why port
labels are built last and documented as dropped gracefully on overflow
(`WCB_WDP.cpp:276`).

Sequence keys are capped at 15 chars by the **NVS key limit**, so ~20 names is ~320 bytes —
more than the entire payload. Advertising them would silently evict the port labels, which
drive Maestro/WLED auto-config. `SEQHASH` is placed **before** PORTLABEL in the build order
for the same reason PWMTARGET is: it is functional, labels are cosmetic.

---

## 3. Wire format

### Request — `PACKET_TYPE_SEQ_REQ` (13)

Reuses `espnow_struct_config_req`, 43 bytes (`WCB.ino:783`). Broadcast, not unicast, so it
works before the target is a registered peer.

```
char    structPassword[40]
uint8_t packetType       = 13
uint8_t targetWCB        // board being asked
uint8_t requesterWCB     // only this requester is answered
```

Sent **three times** — a first broadcast frame to a given board is routinely dropped on a
busy mesh. The target dedups repeats from the same requester inside 1.5 s
(`handleSeqReqPacket`), so three sends still produce exactly one response.

The request touches NVS, so it is **deferred out of the WiFi callback** onto the loop task
via `enqueueMgmtReq` / `drainMgmtReqs`, exactly like CONFIG/STATS/ETM. Running it inline
stalls ESP-NOW and drops other inbound packets.

### Response — `PACKET_TYPE_SEQ_FRAG` (14)

Reuses `espnow_struct_config_frag`, 230 bytes (`WCB.ino:792`), through the shared
`sendResultFrags()` helper (`WCB.ino:3050`). **182 usable payload bytes per chunk** — the
183rd is reserved as a NUL terminator. (`WCB.ino:2950` records why: an earlier version used
all 183 and dropped one character per chunk, which ate the comma in `SEQ,SAVE,key,value`.)

> **Dispatch is by SIZE first, then packetType.** `CONFIG_REQ` and `MGMT_FRAG_UNICAST` are
> both type 5 and are told apart only by 43 vs 226 bytes (`WCB.ino:3316`). Any
> reimplementation of these structs must match the byte count exactly or the packet routes
> into the wrong handler — or nowhere, with no diagnostic at either end. `WCB_Client` pins
> both with `static_assert`.

### Payload

```
<hash8hex>,<count>[,<key1>,<key2>,...]
```

Comma separation is safe: `saveStoredCommandsToPreferences()` takes a stored key as
everything **before the first comma**, so a key can never contain one.

The empty case is `811C9DC5,0` — the FNV basis and a zero count — so a parser needs no
special case for "board has no sequences".

Sizing: 80 keys (`MAX_STORED_COMMANDS`) × 16 bytes ≈ 1280 bytes → 8 of the 16 available
chunks. Comfortable, unlike the config pull.

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

**`TOOBIG` is reported explicitly.** `sendResultFrags()` silently refuses anything
over 16 chunks, so the handler checks the budget first and answers with a status
instead of nothing. Silence is what makes the config pull unusable here (§1) and
reintroducing it in the replacement would recreate the very bug this routes around.

### Why one at a time

A single value is bounded — ~1800 chars is about 10 of the 16 chunks. The whole set
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

**Key rules** (enforced client-side so a bad write fails loudly instead of silently):
1–15 chars (NVS key limit), and **no comma** — `saveStoredCommandsToPreferences()`
takes the key as everything before the first comma, so a comma would truncate it on
save and then never match on read.

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

The hash is **order-sensitive** (`key_list` preserves save order). That is intended: it
answers "did *my* inventory change", not "do two boards match".

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
    // else WCB_SEQ_NOTFOUND / WCB_SEQ_TOOBIG — always check before trusting value
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
- A request with no complete answer within ~4 s is **abandoned silently**. No callback
  fires: a half-list delivered as a result would look like a real answer.
- Interception of `SEQ_FRAG` is armed only once `onSequenceNames()` is registered. Until
  then those packets fall through to `onRawPacket()` exactly as before, so existing sketches
  (NaviCore's OTA raw hook) are unaffected.
- `requestSequenceNames()` requires the callback to be registered first, and returns `false`
  if it isn't.

Full worked example: `WCBClient/examples/SequenceInventory`.

---

## 5. Pointers into the code

- Inventory string + hash (single source of truth for both the console command and the mesh
  reply): `Code/WCB/WCB_Storage.cpp` — `buildSequenceNamesString()`,
  `sequenceInventoryHash()`, `invalidateSequenceInventoryHash()`.
- Packets, handlers, `?MGMT,SEQ`, `?SEQ,NAMES`: `Code/WCB/WCB.ino`.
- TLV advertise/decode + `?WDP` display: `Code/WCB/WCB_WDP.cpp`, `WdpNeighbor::seqHash` in
  `WCB_WDP.h`.
- Client library: `WCBClient` repo, `src/WCB_Client.{h,cpp}` — `onSequenceNames`,
  `requestSequenceNames`, `sequenceNamesPending`, `WCBNeighbor::seqHash`.
- Wire test: `tests/wdp_wire_test.cpp` → `test_seqhash()`.

---

## 6. Revision log

| Date | Change | Commit |
|---|---|---|
| 2026-08-17 | Added the **read-one** and **write** halves: `PACKET_TYPE_SEQVAL_REQ`/`SEQVAL_FRAG` (15/16) with a 59-byte keyed request struct, `?SEQ,GET,<key>`, `?MGMT,SEQGET,<n>,<key>`, and `requestSequence`/`onSequenceValue`/`saveSequence`/`deleteSequence` in `WCB_Client`. `TOOBIG`/`NOTFOUND` are explicit statuses rather than silence. Writes reuse the existing `?SEQ,SAVE` command path — no new wire format. **SEQHASH now covers stored values**, so an in-place edit is visible to peers (see `WDP_DESIGN.md`). `MgmtReqSlot` widened to the largest request on that queue and given a `len`, since SEQVAL_REQ is 59 B against the previous 43. | `80f44d9` |
| 2026-08-17 | Initial version. `PACKET_TYPE_SEQ_REQ`/`SEQ_FRAG` (13/14), `?SEQ,NAMES`, `?MGMT,SEQ,<n>`, WDP TLV `0x13` SEQHASH, `[WDPSEQ:]` dump record, and the `WCB_Client` `onSequenceNames`/`requestSequenceNames` API. | `bbd6bdf` |
