# WDP — Wireless Discovery Protocol (WCB Mesh)

**Status:** Implemented and shipping — this document describes the protocol **as built**.
**Scope:** ESP32/ESP32‑S3 WCB firmware (`Code/WCB`), the Wizard/Config Tool (`Wizard/`), and the
`WCB_Client` Arduino library (v1.7.0+). Serial‑attached devices announce via the companion spec
[`WDP_DEVICE_ANNOUNCE.md`](WDP_DEVICE_ANNOUNCE.md).

---

## 1. What WDP is (and isn't)

WDP is a lightweight discovery/advertisement layer for the WCB ESP‑NOW mesh — conceptually
**"Cisco CDP/LLDP, but for WCBs."** Each board periodically announces *its own* identity and
capabilities; every board listens and builds a **neighbor table**. From that one mechanism the
mesh gets friendly aliases, a live "what's on my mesh" view in the Wizard, controller
auto‑configuration, and — with auto‑join — a peer table that builds itself.

**Goals (all met by the shipped implementation)**
- **Zero‑config awareness** — boards learn the mesh without hand‑entering cross‑board maps.
- **Extensible** — TLV encoding; new advertised facts never break older firmware.
- **Cheap** — a couple of 252‑byte broadcasts per minute per board.
- **Backward compatible** — no wire‑size change; pre‑WDP firmware ignores the packets harmlessly.
- **Authoritative‑by‑owner** — a board only ever advertises *its own* facts.

**Non‑goals**
- **Not config replication/sync.** WDP is *discovery*. A board's own saved config remains the
  source of truth for itself.
- **Not a reliability layer.** Adverts are best‑effort broadcasts; the model is eventual
  consistency.
- **Not a new security boundary.** It sits *behind* the existing ESP‑NOW gates (§8).

**Relationship to ETM:** ETM answers *"is WCB 3 alive?"* (heartbeats → `boardTable[]`);
WDP answers *"who is WCB 3 and what can it do?"* (adverts → `wdpNeighbors[]`). Same carrier,
same auth, parallel tables.

---

## 2. Transport

WDP rides the existing 252‑byte ETM struct (`espnow_struct_message_etm`) as packet type
`PACKET_TYPE_WDP = 12`, reusing the `structCommand[200]` field that is empty on non‑command
packets. No wire‑size change, so the size‑based receive router is untouched; old firmware sees an
unknown packet type in the ETM branch and drops it.

Decode is **deferred to `loop()`** (`enqueueWdpPacket` → `drainWdpPackets`) — the ESP‑NOW receive
callback runs on the WiFi task and must not do heavy work.

### Cadence

| Trigger | Count | Notes |
|---|---|---|
| Boot burst | 3× starting ~1.6 s after boot | fast initial population, follows the ETM boot announce |
| Periodic backstop | every 60 s | staggered per board (`WCB_Number`‑based phase) so co‑booted boards don't collide |
| Solicited | 1× on hearing a SOLICIT (or local `?WDP,POLL`) | jittered 0–600 ms by board number so a fleet doesn't reply in lockstep; lets an operator refresh the whole mesh on demand |

Facts go **stale** after `WDP_TTL_MS` = 180 s (~3 missed adverts). Staleness is display state —
the record stays in RAM until overwritten or `?WDP,CLEAR`.

---

## 3. Wire format

```
structCommand[200]:
+----------------------+
| 'W'  (0x57)          |  magic
| version (0x01)       |  WDP protocol version
+----------------------+
| TLV | TLV | TLV | ...|  until 0x00 terminator or end of field
+----------------------+

each TLV:  [ type : 1 ][ len : 1 ][ value : len bytes ]
```

Unknown TLV types are skipped via the length prefix — forward compatible in both directions.

### TLV registry (as shipped)

| Type | Name | Value | Sent by |
|---|---|---|---|
| `0x00` | END | terminator | — |
| `0x01` | ALIAS | board alias, ≤24 chars | WCB |
| `0x03` | FWVER | firmware version string, ≤27 | WCB + client |
| `0x04` | HWVER | numeric hw id (1, 21, 23, 24, 31, 32) | WCB |
| `0x05` | CAPFLAGS | uint16 LE capability bitmap (below) | WCB |
| `0x06` | MAESTRO | list of locally attached Maestro device IDs | WCB |
| `0x09` | PORTLABEL | `[port 1‑5][label bytes]`, one TLV per labeled serial port; unlabeled ports fall back to the WDP‑DA detected device type | WCB |
| `0x0A` | CTRLID | controller (special‑peer) id this board links to | WCB, only when linked |
| `0x0B` | DEVTYPE | device type name — marks the sender as a **client device**; doubles as its display name | client |
| `0x0C` | HWREV | hardware revision string, ≤15 | client |
| `0x0D` | CAPTAGS | space‑separated capability tags, ≤48 | client |
| `0x0E` | MAESTRO_CFG | `[id][baudCode]` pairs — Maestro id+baud, for remote-proxy auto-config | WCB |
| `0x0F` | WLED_CFG | `[id][baudCode]` pairs — WLED id+baud, for remote-proxy auto-config | WCB |
| `0x10` | PWMTARGET | `[targetWCB][port]` pairs — this board's REMOTE PWM outputs; each named board self-configures that output port | WCB |
| `0x11` | SOLICIT | len 0 — a bare "advertise now" request (`?WDP,POLL`); carries no facts, receivers reply with a jittered advert and never record it | WCB |
| `0x40–0xFE` | *reserved* | vendor / future | — |

*(Draft types `0x02 ROLE`, `0x07 CONTROLLER`, `0x08 HEALTH` were never shipped — see §10.)*

**CAPFLAGS bits:** `0x0001` HCR · `0x0002` MP3 · `0x0004` WLED · `0x0008` Kyber‑local ·
`0x0010` Maestro‑remote · `0x0020` PWM · `0x0040` Controller‑link · `0x0080` Maestro‑host.

Wire strings are scrubbed on receive (`,`/`]`/control chars → `_`) so they can't corrupt the
machine‑readable dump lines (§7).

---

## 4. Neighbor table

`wdpNeighbors[MAX_WCB_COUNT]` (RAM‑only, indexed by `wcbNumber‑1`): alias/devtype, fw, hw
version/revision, capFlags/capTags, controller link, Maestro IDs, port labels, `isClient`,
last‑advert age, confirmed/stale.

Processing rule: an advert **replaces the sender's record wholesale** — the board is the sole
authority for its own facts, so removals happen naturally on the next advert. The *descriptive*
table is deliberately **not persisted** (it re‑learns within one advert cycle); what persists is
peer **membership** (§6).

---

## 5. The three announcer types

1. **WCBs** — advertise automatically when WDP is on (default).
2. **WCB_Client devices** (NaviCore, Sabé, MaestroSim, …) — call `setIdentity(type, fw, hwRev,
   caps)` (library ≥1.7.0) and the same advert goes out over the mesh; consumers see
   `isClient=true` with DEVTYPE/HWREV/CAPTAGS. Since 1.8.0 clients also **decode** adverts
   (`onNeighbor`/`getNeighbor`/`neighborCount`), and since 1.9.0 they **auto‑join** boards they
   hear (§6) — the client twin of everything in this document.
3. **Serial‑attached devices** — anything wired to a WCB serial port announces with a one‑line
   `@WDP1 {json}` every 25–30 s (see `WDP_DEVICE_ANNOUNCE.md`). The WCB stores a per‑port record
   (TTL 90 s, `?WDP,DA` to inspect) and folds the detected type into its own PORTLABEL adverts —
   so a Maestro plugged into WCB 4 shows up mesh‑wide with zero config.

---

## 6. Dynamic peer membership + auto‑join

Historically the peer set was the contiguous range `1..WCBQ` and adding a board meant touching
every board's quantity. As built, each board maintains an explicit **membership set**:

```
active peers = {1..WCBQ}  ∪  {learned peers}  ∪  {controller/special peer}
               (the floor)    (WDP auto-join)     (tracked separately)
```

- **Auto‑join** (default **ON**, `?WDP,AUTOJOIN,ON|OFF`, persisted): when a board hears a regular
  WCB advertise **twice**, it registers it as an ESP‑NOW peer **live** and adds it to membership.
  Client devices and the controller id are never auto‑joined (clients are handled by the
  controller path, §9).
- **Learned peers are permanent.** Membership is persisted to NVS (`learned_peers`: schema
  version + MAC‑octet fingerprint + 20‑bit mask) and restored on every boot. Heartbeats drive
  online/offline as always, but membership never self‑evicts — a powered‑down board stays a
  member and simply shows offline until it returns. Cleanup is deliberately the operator's call:
  `?WDP,FORGET,<id>` (one) or `?WDP,CLEAR` (all learned). Changing the mesh MAC octets discards
  the saved membership (the fingerprint no longer matches).
- **WCBQ is the floor, not the ceiling.** It still pre‑registers `1..WCBQ` at boot (useful so a
  fresh board isn't peerless before the first advert), and a live `?WCBQ` change reconciles
  registrations without a reboot. Run WCBQ=1 for a purely learned mesh.
- All ETM machinery (offline sweep, broadcast expected‑ACK set, retries, stats, load test) runs
  off the membership set. In‑flight messages evaluate against their own send‑time ACK snapshot,
  so membership changes can't race a pending broadcast. A learned peer is not counted against
  broadcast completion until it has ACKed at least once (protects mixed fleets with pre‑WDP
  firmware).

This all works because WCB MAC addresses are **derived, not learned**: every board and client
forces its radio MAC to `02:<oct2>:<oct3>:00:00:<id>`. Knowing a board's number is knowing its
address, so "add a peer" is a pure local `esp_now_add_peer` — no handshake needed.

---

## 7. Commands + Wizard integration

| Command | Effect |
|---|---|
| `?WDP` / `?WDP,LIST` | human‑readable neighbor list |
| `?WDP,<n>` / `?WDP,DETAIL,<n>` | one neighbor in detail |
| `?WDP,STATUS` | `[WDP:en,autojoin,proto,neighbors,peers]` one‑liner |
| `?WDP,DUMP` | machine‑readable dump for the Wizard (below) |
| `?WDP,DA` | serial‑attached (WDP‑DA) devices per port |
| `?WDP,POLL` | advertise now + broadcast a SOLICIT so every board re‑advertises (fast convergence; skips the 60 s wait) |
| `?WDP,ON` / `?WDP,OFF` | enable/disable WDP (persisted; default ON) |
| `?WDP,AUTOJOIN[,ON\|,OFF]` | auto‑join learned peers (persisted; default ON) |
| `?WDP,ADD,<id>` | manually add a learned peer (same as hearing it) |
| `?WDP,FORGET,<id>` | drop one learned peer (RAM + ESP‑NOW + NVS) |
| `?WDP,CLEAR` | drop all learned peers + wipe the neighbor table |
| `?PEERSLIVE` | read‑only: live membership count (also emitted in config pulls) |

**DUMP format** (one line per neighbor, `PEER`: 0 = not a member, 1 = WCBQ floor, 2 = learned):

```
[WDP:N=..,CLIENT=..,ALIAS=..,HW=..,HWREV=..,FW=..,CAP=....,CTRL=..,CAPTAGS=..,MAESTRO=..,AGE=..,SEEN=..,PEER=..]
[WDPIF:N=..,S=<port>,DEV=<label>]          ← one per labeled/detected port
[WDPX:N=..,MB=<id@baud.…>,WL=<id@baud.…>]  ← per board hosting Maestro/WLED: the per-device baud the
                                             main line omits, plus the full WLED list (`-` = none)
[WDPPWM:N=<src>,DST=<dst>,S=<port>]         ← a remote-PWM drive edge: board <src> drives WCB <dst>'s S<port>
[WDPCFG:AUTOJOIN=..,PEERS=..]
[WDP:END,count=..]
```

The self row is emitted first, flagged `PEER=3`. `WDPX`/`WDPPWM` are separate lines (not new
fields on `[WDP:...]`) so older Wizards ignore them instead of failing to parse the record; only
edges *touching the queried board* are reconstructable from one dump (a board keeps just the
`PWMTARGET` entries that name itself), so the full graph is assembled across per‑board pulls.

**Wizard:** the *WDP Mesh* panel renders the dump — every neighbor with kind/platform/fw/caps/
Maestros (with baud)/remote WLED/remote‑PWM wiring/port devices/membership, plus an auto‑join
toggle, per‑row Forget for learned peers, and Clear‑learned. The General section shows a
read‑only "mesh: N live peers" badge next to WCB Quantity (from `PEERSLIVE` in the config pull).

---

## 8. Security

Everything WDP does sits behind the inbound gates, in order:

1. **MAC‑group check** — source octets 2/3 must match this installation (all packet types).
2. **Password** — the ETM struct password must match.
3. **Sender validity** — id in range, not self.
4. **Sender‑MAC binding** — the claimed sender id must equal the source MAC's last octet
   (possible because all MACs are derived). A rogue in‑group node can no longer claim another
   board's id to fake an ACK, flip online state, spoof a controller advert, or trigger auto‑join.
   The `WCB_Client` library (≥1.9.0) enforces the same rule.

Auto‑join additionally requires **two** adverts before acting, so a single stray packet can't
inject a peer, and a learned peer only carries *membership* — an advert can never supply a MAC or
redirect traffic (addresses are always derived locally).

Like CDP, adverts are cleartext identity/capability broadcasts — acceptable for a hobby mesh,
noted here for completeness.

---

## 9. Auto‑configuration (shipped behaviors)

On **first learn** of a client device whose DEVTYPE is a controller (`NaviCore`, `Sabé`/`Sabe`):

1. If no controller peer is configured (`specialPeerEnabled` false), enable it pointed at the
   sender — same effect as `?CONTROLLER,ON,<id>`, registered live, no reboot. A manually pinned
   controller is never overridden.
2. If this board has a physically attached Maestro (and isn't the Kyber‑local host), flip
   Maestro‑remote on so the controller can drive it over the mesh.

On **every advert** from a confirmed peer whose `PWMTARGET` TLV (§3) names this board as a
remote **PWM output** target: if that port isn't already a PWM output and isn't reserved
(e.g. Kyber), reserve/configure it live — pin set + persisted to NVS, no reboot. Receiver-side
(the target owns its own config), so a board that was powered off when the mapping was created
still self-configures the instant it hears the advert. Idempotent and **self-healing**: the
auto-configured port is tagged (persisted) with its driving board, and when that board's *fresh*
advert stops naming the port (the mapping was deleted) the receiver clears its stale output
config itself — dropping the port from the list + NVS and re-enabling that port's broadcasts — so
a deletion made while the receiver was offline no longer strands the port forever waiting on a
`;MAP,PWM,CLEAR,OUT` push it may have missed. As with the manual clear (which reboots), the pin is
fully returned to plain serial on the next reboot; the self-heal deliberately does **not** force
one — a board shouldn't reboot itself because a *peer* dropped a mapping. Only WDP-tagged ports
self-heal; a manually configured PWM output is never auto-removed.

On **every** `;H` / `;A` trigger, **single‑owner capability routing** (`routeStoredOrCap` →
`wdpCapOwner`) resolves the command to exactly one host: a persisted/pinned host if it is set and
online, otherwise the lowest‑numbered **online** board advertising the capability (HCR / MP3), so
a broadcast can't fire the trigger N times. A pinned host that is *offline* fails over to live
election instead of black‑holing. WLED and Maestro are **id‑addressed** (`;L<id>` / `;M<id>` route
per‑id to the board that advertised that id), so they need no single‑owner election.

On **first learn** of a peer‑hosted Maestro or WLED, its `id → board @ baud` is auto‑added as a
remote proxy (`maestroAutoAddRemote` / `wledAutoAddRemote`) from the `MAESTRO_CFG` / `WLED_CFG`
TLVs — first‑host‑wins, persisted. No port field is needed in the TLV: remote Kyber bytes are
broadcast and the target board owns its own port. A **Kyber‑local** host additionally folds a
newly‑learned remote Maestro into its live `kyberTargets` (add‑only), so it forwards to the
just‑discovered board with no manual `?KYBER,LOCAL` re‑issue.

Plus the membership auto‑join of §6.

---

## 10. Future work (designed, not built)

*(Single‑owner capability routing (HCR/MP3) and Maestro/WLED `id → board` auto‑config — once
listed here — have shipped; see §9.)*

- **Generic capability addressing** — a syntax to address any command to a *capability*
  (`"whoever has X"`). Today only the ID‑less HCR/MP3 triggers auto‑resolve to a single owner;
  there is no general `;@<cap>` addressing form, and the Wizard has no surface to view or pin
  which board currently owns a capability.
- Draft TLVs / ideas not shipped: ROLE (`0x02`) and HEALTH (`0x08`) TLVs, management‑peer (19)
  ephemeral adoption, and descriptive‑table persistence (deliberately a non‑goal — §4).

---

## 11. Pointers into the code

- Firmware: `Code/WCB/WCB_WDP.cpp` / `.h` (advertise, decode, neighbor table, WDP‑DA, commands),
  `Code/WCB/WCB.ino` (carrier structs, receive gates + queue, membership arrays,
  `addActivePeer`/`removeActivePeer`, `learned_peers` NVS, `?PEERSLIVE`).
- Client library: `WCBClient` repo, `src/WCB_Client.{h,cpp}` — `setIdentity`, `onNeighbor`,
  `getNeighbor`, `neighborCount`, `setAutoJoin`, `forgetPeer`, `clearLearnedPeers`;
  `examples/NeighborDiscovery`.
- Wizard: `Wizard/app.js` (`parseWdpDump`, `renderWdpMesh`, mesh actions), `Wizard/parser.js`
  (`PEERSLIVE`).
- Serial device announce: [`WDP_DEVICE_ANNOUNCE.md`](WDP_DEVICE_ANNOUNCE.md).
