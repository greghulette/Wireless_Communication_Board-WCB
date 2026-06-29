# WDP — Wireless Discovery Protocol (WCB Mesh)

**Status:** Draft / design — for discussion, not yet implemented.
**Scope:** ESP32/ESP32‑S3 WCB firmware (`Code/WCB`) + the Wizard/Config Tool (`Wizard/`).
**Author:** design notes, Greg + Claude.

---

## 1. What WDP is (and isn't)

WDP is a lightweight discovery/advertisement layer for the WCB ESP‑NOW mesh — conceptually **"Cisco CDP/LLDP, but for WCBs."** Each board periodically announces *its own* identity and capabilities; every board listens and builds a **neighbor table**. From that one mechanism we get friendly aliases, automatic Maestro placement, controller‑peer auto‑enable, and a live "what's on my mesh" view for the Wizard.

**Goals**
- **Zero‑config awareness** — boards learn the mesh without hand‑entering cross‑board maps.
- **Extensible** — add new advertised facts without breaking older firmware (TLV encoding).
- **Cheap** — negligible added airtime on a busy mesh (we've seen congestion with NaviCore traffic).
- **Backward compatible** — no wire‑size change, no packet‑router change; old firmware ignores it.
- **Authoritative‑by‑owner** — a board only ever advertises *its own* facts.

**Non‑goals**
- **Not config replication/sync.** WDP is *discovery*, not a control plane. A board's own saved config remains the source of truth **for itself**. (CDP is informational; so is WDP.)
- **Not a reliability layer.** It rides best‑effort presence broadcasts; the model is *eventual consistency*, not guaranteed delivery.
- **Not a new security boundary.** It inherits the existing ESP‑NOW gate (MAC‑group + password).

---

## 2. Relationship to ETM

ETM already gives us **presence**: heartbeats, online/offline tracking, and `boardTable[]`. WDP extends presence with **capability**.

> ETM answers *"is WCB 3 alive?"* — WDP answers *"who is WCB 3 and what can it do?"*

Same boards, same broadcast layer, same auth. WDP is an additive payload + a parallel descriptive table; it does not change how heartbeats or ETM reliability work.

---

## 3. Transport / carrier — the "free real estate"

WDP rides inside the **existing ETM message struct**, which is already 252 bytes:

```c
typedef struct __attribute__((packed)) {
  char     structPassword[40];
  char     structSenderID[4];
  char     structTargetID[4];
  uint8_t  structCommandIncluded;
  char     structCommand[200];     // ← EMPTY on heartbeats/boot/ack. WDP payload rides here.
  uint8_t  structPacketType;
  uint16_t structSequenceNumber;
} espnow_struct_message_etm;        // 252 bytes total
```

`structCommand[200]` is populated only for `PACKET_TYPE_COMMAND`; on heartbeats it's zeroed and wasted. WDP reuses it. Consequences:

- **No wire‑size change** → the size‑based ESP‑NOW receive router (`len == sizeof(espnow_struct_message_etm)`) is untouched.
- **Backward compatible** → a WDP packet lands in the ETM branch; old firmware doesn't match its packet type, falls through harmlessly (exact same behavior already validated for the boot‑announce, `PACKET_TYPE_ETM_BOOT`).
- **~198 bytes of usable payload** after a 2‑byte WDP header — plenty for compact TLVs.

### Packet type

Add one ETM‑struct packet type (these only need to be unique *within* the 252‑byte struct: currently `COMMAND=0`, `ACK=1`, `HEARTBEAT=2`, `ETM_BOOT=11`):

```
#define PACKET_TYPE_WDP  12   // ETM-struct advertisement carrying a WDP TLV payload
```

The boot announce (`ETM_BOOT`) may also carry a WDP payload for instant first‑contact, or the board can just emit a `WDP` packet right after boot. (Decision in §17.)

---

## 4. When advertisements are sent (cadence)

CDP/LLDP‑style split — **cheap liveness often, full advert rarely.** Heartbeats stay tiny; WDP adverts are paced:

| Trigger | Count | Why |
|---|---|---|
| **On boot** | 3× over first few seconds | fast initial population (alongside the existing boot announce) |
| **On change** | 1–3× immediately | alias set, Maestro added/removed, `?CONTROLLER` toggled, config edited (LLDP‑style event‑driven) |
| **Periodic backstop** | every ~60 s (or every Nth heartbeat) | late‑joiners + missed‑advert convergence |
| **On request** *(Phase 4)* | reply once | a `WDP_REQUEST` broadcast solicits immediate adverts (fast Wizard refresh) |

The regular ~10 s heartbeats carry **no** WDP payload — liveness only. This keeps the airtime cost near zero while the neighbor table stays fresh.

---

## 5. Wire format

WDP payload occupies `structCommand[200]`:

```
+----------------------+
| 'W'  (0x57)          |  magic
| version (0x01)       |  WDP protocol version
+----------------------+
| TLV | TLV | TLV | ...|  repeated to end, or until a 0x00 terminator type
+----------------------+

each TLV:  [ type : 1 ][ len : 1 ][ value : len bytes ]
```

- A board emits only the TLVs relevant to it.
- **Unknown TLV types are skipped** via their length prefix → forward compatible; new TLVs never break older WDP‑aware firmware.
- The 2‑byte header lets us bump the protocol version for an incompatible *header* change (rare); TLV additions never require a version bump.

### TLV type registry (initial)

| Type | Name | Value | Tier |
|---|---|---|---|
| `0x01` | **ALIAS** | board name, UTF‑8 (≤ ~16 B) | informational → feeds learned alias map |
| `0x02` | **ROLE** | enum (0=unset, 1=dome, 2=body, 3=utility, …) | informational |
| `0x03` | **FWVER** | firmware version string | informational |
| `0x04` | **HWVER** | hw id (e.g. 21, 31, 32) | informational |
| `0x05` | **CAPFLAGS** | bitmap: HCR, MP3, serial‑maps, PWM‑maps, Kyber, controller‑peer‑enabled | informational |
| `0x06` | **MAESTRO** | list of attached Maestro device IDs (+ optional port) | **actionable** → routing table |
| `0x07` | **CONTROLLER** | controller‑peer id + enabled flag | **actionable** → auto‑adopt *fallback* (primary path is direct‑hear, §8b) |
| `0x08` | **HEALTH** | uptime, last‑reset reason | informational |
| `0x40–0x7F` | *reserved* | vendor / experimental (e.g. non‑NaviCore controllers) | — |
| `0x80–0xFE` | *reserved* | future core types | — |

Reserved ranges so a non‑NaviCore controller (your friend's use) and future features don't collide.

---

## 6. Neighbor table

A descriptive table parallel to `boardTable[]`, sized to the mesh (`MAX_WCB_COUNT = 20`, plus the controller‑peer slot):

```c
struct WdpNeighbor {
  uint8_t  wcbNumber;
  char     alias[16];
  uint8_t  role;
  char     fwVer[24];
  uint8_t  hwVer;
  uint16_t capFlags;
  uint8_t  maestroIds[8];   // this board's advertised Maestro IDs (its own contribution)
  uint8_t  maestroCount;
  uint32_t lastAdvertMs;    // for TTL / staleness
  bool     valid;
};
```

- **Liveness** (online/offline, lastSeen) stays in `boardTable[]`. WDP holds the *descriptive* facts.
- **Persistence:** the descriptive map **persists to NVS**. A board wakes up already knowing the topology → the first command after a reboot just works instead of waiting a discovery cycle. On boot, load the saved table but mark everyone **"unconfirmed"** until a heartbeat/advert reconfirms them.

---

## 7. Processing rules

On receiving a WDP advert from board **N** (already MAC‑group + password gated upstream):

1. **Replace board N's own contributions wholesale.** The advert is N's authoritative, complete statement of its facts. This makes *removals* natural — if N stops advertising Maestro 2, it simply drops from N's contribution on the next advert.
2. Update the neighbor entry; **persist if changed**; bump `lastAdvertMs`.
3. Apply actionable effects (§8).

- **Fresh wins:** a newer advert overrides stored NVS for that board.
- **Age‑out:** if N hasn't advertised within `WDP_TTL` (e.g. ~3× the periodic interval, ~3 min) **and** is offline, flag its facts stale and **withdraw its actionable contributions** (e.g. its Maestro hosts) so commands don't black‑hole. The persisted record may remain as topology memory, flagged stale.

### 7a. Provenance & precedence (MANUAL > LEARNED > DEFAULT)

Every stored fact carries a **source tag** so conflicts resolve predictably — administrative‑distance style:

> **MANUAL > LEARNED > DEFAULT**

- A board is the **sole authority for its own facts** (its Maestros, its own alias, its own enables). Only it advertises them, so there is no *true* contradiction — a fresh self‑advert just corrects a stale cache (fresh‑wins). "Board 3 has Maestro 2" coming from board 3 *is* the truth about board 3.
- **Manual always wins and is sticky.** A value you explicitly set is pinned: WDP fills **DEFAULT** gaps and refreshes **LEARNED** values, but **never overrides MANUAL**. On a divergence it does not fight you — it **flags** it (serial log + a Wizard "you set X locally, the mesh says Y" warning) and leaves your value in place.
- The **source tag persists to NVS** alongside the value. Because the table is persisted, after a reboot the board must still know which values were your explicit choice vs. learned — otherwise precedence breaks on the next advert.

Examples: a `?CONTROLLER,OFF` you typed is MANUAL → controller auto‑enable will **not** flip it back on; a board you never touched is DEFAULT → auto‑enable may act. A local alias entry (MANUAL) always beats a learned `alias→#` (LEARNED).

---

## 8. Actionable TLVs — exact effects + safety rails

**Governing principle:** actionable adverts drive **derived routing / runtime state** (which *is* persisted, per design choice), but they **never silently rewrite the receiving board's own authored config.** And everything here inherits the existing MAC‑group + password gate, so only boards in *your* installation can trigger it.

### Activation cost: live vs reboot‑required

This firmware creates its worker tasks **once in `setup()`** from config flags, so many "enable a capability" flips can't take effect until the next boot. WDP must classify each effect:

| Effect | Activation | Why |
|---|---|---|
| Maestro command routing / placement (which board hosts an ID) | **live** | runtime table lookup at command time |
| Learned alias map, informational facts | **live** | pure data |
| Maestro **remote forwarding** (`Maestro_Remote` → `KyberRemoteTask`) | **reboot** | task created only in `setup()` (`WCB.ino` ~5888) |
| Kyber local (`Kyber_Local` → `KyberLocalTask`) | **reboot** | task created only in `setup()` (~5884) |
| PWM mappings (`PWMTask`) | **reboot** | task created only in `setup()` (~5900) |
| Controller peer (`esp_now_add_peer`) | **reboot** *(today)* | peer registered only in `setup()` (~5826) — see §8b |

**Hard rule: WDP NEVER auto‑reboots a board.** A board must not spontaneously reboot mid‑show because an advert arrived. For a reboot‑required effect, WDP:

1. applies + persists the setting (with its MANUAL/LEARNED tag),
2. marks it **"pending — reboot to activate,"**
3. surfaces that state (serial log + `?WDP,STATUS` + a Wizard "pending reboot" badge), and
4. leaves the reboot to the user's schedule (or a Wizard "reboot to apply" button).

### 8a. MAESTRO (`0x06`) — auto placement

- Builds a **multi‑valued** map: `maestroHosts[id] = { {board, port}, … }`.
- **Duplicate IDs across boards are valid and expected** — there can absolutely be more than one Maestro with the same device ID on the mesh. No "duplicate" warning.
- A command to Maestro `id` resolves to **all** hosts and fans out (one ESP‑NOW send per host board), reusing the existing Maestro routing. Locally attached Maestros behave as they do today; remote ones come from the learned hosts.
- Each board's advert replaces *its own* host contributions; an offline/aged board's hosts are withdrawn from routing.
- Wizard shows the live Maestro map across the mesh.

> **Two different "Maestro" mechanisms — don't conflate them.** This TLV covers **command routing / placement** (which board hosts ID N, for dispatching/fanning‑out commands) — that's a runtime table lookup, **live, no reboot.** It does **not** cover **Maestro remote data forwarding** (`Maestro_Remote`), which continuously pipes a local Maestro's serial stream out to the controller/Kyber via the boot‑created `KyberRemoteTask`. Newly enabling *that* path is **reboot‑required** (see the activation‑cost table above) and follows the "pending reboot" rule.

### 8b. CONTROLLER auto‑adopt — hear the controller, set yourself up

The "controller peer" is the extra pinned ESP‑NOW peer outside the normal numbered boards (the `?CONTROLLER` / legacy `?SPECIAL` peer, default id 20 — NaviCore, or any device). The goal: stop hand‑typing `?CONTROLLER,ON` on every board. There are two paths, and the **primary one isn't a TLV at all** — it's a presence‑layer behavior.

**Primary — direct adoption (ETM/presence, not WDP):** the controller already broadcasts ETM heartbeats as sender id 20, and broadcast **receive needs no peer registration**, so every WCB already *hears* it (the packet has passed the MAC‑group + password gate). So:

- When a WCB hears a gated packet whose `sender == controller id` **and** the controller isn't set up yet, it **auto‑adopts**: enable + register that peer + persist.
- **The trigger carries the MAC.** The receive callback's `info->src_addr` *is* the controller's MAC, so the WCB learns the address straight from the heartbeat — nothing to pre‑seed. (Today the peer MAC comes from the static `WCBMacAddresses` table; src_addr learning is more robust and handles a custom controller.)
- Plug in NaviCore → every board in mesh range wires itself up. This works off the heartbeat that already exists; it is independent of the WDP advert.

**Fallback — the `CONTROLLER` (`0x07`) TLV:** a WCB that *has* the controller enabled also advertises that fact, so a board which **can't hear the controller directly** but hears a neighbor that has it can still adopt. Only useful for sparse/large meshes where not every board is in range of the controller; for a NaviCore‑as‑hub rig the direct path covers everything.

**Rules (both paths):**
- **Provenance (§7a):** auto‑adopt only when the setting is **DEFAULT/untouched**. An explicit `?CONTROLLER,OFF` is MANUAL and stays off — a heartbeat must never override a deliberate choice.
- **Sticky, no auto‑disable:** once adopted, stay adopted even if the controller goes quiet (reboots, drifts out of range). Controller‑gone is a liveness event, not an un‑register — avoids the peer flapping.
- **Configurable id:** listen for the configured controller id (default 20). For a non‑NaviCore controller on a different id, the clean generalization is "adopt any sender in the reserved controller‑id range" (the high ids above the normal board count) — see open decisions.
- **Safety:** sits behind the same MAC‑group + password gate as everything else; only *your* mesh's controller can trigger it. Logged observably (`[CTRL] auto-adopted controller id 20 (MAC …)`).

**Hard dependency — runtime peer registration.** Direct adoption only "just works" if registering the peer happens **at runtime**, not only at boot. Today `esp_now_add_peer` for the controller runs only in `setup()` (`WCB.ino` ~5826), so without the refactor a WCB would *hear* the controller but couldn't *talk back* (unicast ETM ACKs) until a reboot — defeating the magic. So **auto‑adopt and "make the controller peer runtime‑registered" are the same feature**: hear id 20 → `esp_now_add_peer` with the src_addr MAC, live → two‑way comms immediately. Make this change in `saveSpecialPeerPreferences` (add `esp_now_add_peer`/`esp_now_del_peer` on the flag flip); it also makes a *manual* `?CONTROLLER,ON` take effect without a reboot. (Easy/safe for a known MAC, channel 0, unencrypted; verify on core 3.3.4.) The forwarding **tasks** (Maestro_Remote/Kyber/PWM) are far fiddlier to create/destroy at runtime, so those stay reboot‑required per the activation‑cost table.

- **Open decision:** auto‑adopt default‑on vs opt‑in via `?WDP,AUTOCTRL,ON` (§16).

### 8c. Reserved‑peer adoption — the general pattern

§8b's controller adoption is one instance of a general mechanism: **adopt a peer in the reserved high‑ID range when you hear it, with a per‑ID persistence policy.** The mechanism is identical for every reserved peer — hear a gated packet from a reserved ID you don't have → runtime `add_peer` (MAC from `src_addr`) → maybe persist — same MAC+password gate, same provenance rule (§7a), same runtime‑registration prerequisite. **The only knob that differs per reserved ID is how long the adoption lives.**

| Reserved ID | Entity | Presence | Persistence policy |
|---|---|---|---|
| 20 | Controller (NaviCore / `?CONTROLLER`) | always‑on | **persist** — instant readiness on reboot, zero cold‑start gap |
| 19 | Management / relay (diagnostics, Wizard‑side mgmt entity) | intermittent | **ephemeral + TTL** — build on hear, drop on silence/reboot |
| 17–18 | reserved for future aux devices | — | per‑device |

**Management (19) — ephemeral + TTL:**
- Build the unicast peer on first hearing 19; **do not persist.** Keeps the ESP‑NOW peer table lean (the cap is ~20 peers) — you don't carry a slot for something rarely present.
- **TTL `del_peer`:** drop the peer after 19 goes silent for the age‑out window, so the slot frees when a session ends *without* a reboot (e.g. the Wizard just disconnected).
- Naturally gone on reboot (not persisted) — and that's fine, because:

**Reboots during management are handled by the announce/adopt loop, NOT by persistence:**
- While a session is active, 19 heartbeats (it's actively managing). A board that reboots **re‑hears 19 within a heartbeat interval and re‑adopts** (runtime `add_peer`).
- The board's **boot‑announce** (`PACKET_TYPE_ETM_BOOT`, §13) already tells the management side "I rebooted," and the Wizard re‑establishes (re‑pull / re‑RTERM with retries — the relay‑reconnect path). Those retries mask the ~1‑heartbeat re‑adopt gap.
- If management has **ended** during the reboot, 19 isn't heartbeating → the board doesn't re‑adopt → correct, self‑cleaning, no stale peer.
- **Precondition:** whatever sits at 19 must heartbeat for the *duration* of a session (like the controller does). It may bump its heartbeat rate while active to shrink the gap.

**Escalation (only if needed):** if the re‑adopt gap proves too slow in practice, persist a lightweight **"was managed by 19" hint** (not the peer) so a board pre‑registers 19 from its static MAC on boot, pending re‑hearing it. Default to pure ephemeral — don't pre‑optimize a gap the retries already cover.

**Caveats:**
- Reserved IDs **shrink the normal addressable board range** (19 + 20 gone → normal boards top out at 18).
- Bootstrap is a non‑issue: broadcast *receive* needs no peer, so a reserved entity just has to announce first; boards hear it and adopt.
- Symmetry: the reserved entity building peers for the boards it talks to is its own mirror logic (the relay/dongle side).
- Shared prerequisite: runtime peer registration (§8b) unlocks all reserved‑peer adoption.

---

## 9. Aliases on WDP

Two layers, additive:

- **Phase 1 (local, no mesh):** each board has a **local alias table** (`alias → WCB#`) used only to resolve *its own* outbound `;W<alias>,…` commands. Resolution happens **at the origin** — `;WBC` becomes `;W3` *before* it goes on the wire, so the rest of the mesh stays numeric and untouched. Only the originating board needs the alias.
- **WDP layer (learned/global):** the **ALIAS TLV** populates a learned `alias → number` map in the neighbor table. The origin resolver consults the **local table first, then the learned map** — giving "global aliases" with zero extra config, since every board already names itself.
- **Collision** (two boards share a name): deterministic pick (lowest WCB#) + Wizard warning; the local table always wins over the learned map.

Parsing rules for `;W<token>`: if `<token>` parses as a valid board number → numeric target (unchanged, back‑compat). Otherwise → alias lookup, **case‑insensitive**, letter‑led names only (so a numeric token is never mistaken for an alias). Unknown alias → **drop + log** (`[ROUTE] unknown alias 'BC'`), never a blind broadcast.

---

## 10. Informational TLVs

`ROLE`, `FWVER`, `HWVER`, `CAPFLAGS`, `HEALTH` — display and awareness only, **no local behavior**. For mappings (serial/PWM), the flag just says *"this board does serial/PWM mappings"* — we do **not** care about the specifics. These power the Wizard neighbor view and version/health dashboards.

---

## 11. Wizard integration

- **`show wdp neighbors`** — read a connected (or relayed) board's neighbor table via `?WDP,LIST` and render the **whole mesh** — numbers, aliases, roles, firmware versions, capabilities, Maestro map — **without pulling each board's full config.**
- Auto‑populate the board grid / a topology view.
- Surface stale/offline boards, **firmware‑version drift**, and duplicate‑alias warnings.

This is the sleeper payoff and dovetails with the NaviCore‑as‑hub direction: the hub consumes the same table to know what it's conducting.

---

## 12. Firmware commands

| Command | Effect |
|---|---|
| `?WDP,LIST` | dump neighbor table to serial (machine‑readable for the Wizard) |
| `?WDP,STATUS` | proto version, advert cadence, table size, WDP on/off |
| `?WDP,ON` / `?WDP,OFF` | enable/disable WDP (default ON when ETM is on) |
| `?WDP,CLEAR` | wipe learned/persisted neighbor state (clean slate) |
| `?WDP,AUTOCTRL,ON\|OFF` | *(optional)* gate the controller auto‑enable behavior |

---

## 13. Backward compatibility

- New `PACKET_TYPE_WDP` rides the **existing** ETM struct size → an old board sees it in the ETM branch, matches none of `HEARTBEAT/ACK/COMMAND`, and falls through harmlessly (same analysis as the boot‑announce). It simply won't learn WDP facts until updated.
- Reusing `structCommand[200]` → **no struct‑size change** → the size‑based router is untouched.
- TLV skipping → new TLV types don't break older WDP‑aware firmware.

---

## 14. Security

Inherits the existing ESP‑NOW gate: **MAC‑octet group match + struct password**. Only boards in your installation can advertise or be trusted; WDP adds no attack surface beyond what heartbeats already expose. Note it broadcasts identity/capability in cleartext (like CDP) — acceptable for a hobby mesh, documented here.

---

## 15. Phased plan

| Phase | Deliverable | Mesh changes |
|---|---|---|
| **0 — done** | ETM presence + boot announce (`PACKET_TYPE_ETM_BOOT`) | — |
| **1** | Local alias table + `;W<alias>` origin resolution | none (purely local) |
| **2** | `PACKET_TYPE_WDP` + ALIAS/ROLE/FWVER/HWVER/CAPFLAGS TLVs + neighbor table + `?WDP,LIST`; learned‑alias fallback; Wizard neighbor view (read‑only) | +1 packet type |
| **3** | Actionable **MAESTRO** TLV (auto Maestro routing) + **CONTROLLER** auto‑enable | — |
| **4** | On‑request solicit, HEALTH/uptime, Wizard auto‑populate/topology, fw‑drift dashboard | — |

Each phase is independently useful and nothing gets rebuilt — the resolver, table, and packet are introduced once and only gain consumers.

---

## 16. Open decisions

**Settled** (now specified above): persistence to NVS (§6), provenance precedence MANUAL > LEARNED > DEFAULT (§7a), never‑auto‑reboot + pending‑reboot for reboot‑required effects (§8), duplicate Maestro IDs are valid (§8a).

**Also settled:** controller setup is now **direct auto‑adopt on hearing the controller id** (primary, presence‑layer), with the `CONTROLLER` TLV demoted to a sparse‑mesh fallback (§8b). Generalized to **reserved‑peer adoption** (§8c): one hear→adopt mechanism, per‑ID persistence policy — controller(20)=persist, management(19)=ephemeral+TTL.

**Still open:**
1. **Controller auto‑adopt:** default‑on, or opt‑in via `?WDP,AUTOCTRL`?
2. **Runtime peer registration** is now a **dependency** of direct auto‑adopt (not just a nice‑to‑have) — confirm we do the `saveSpecialPeerPreferences` refactor (add/del peer on flag flip). Do it as a standalone firmware win first, or bundle with auto‑adopt?
3. **Controller id matching:** listen only for the exact configured id (default 20), or adopt any sender in a reserved controller‑id range (for non‑NaviCore controllers on custom ids)?
4. **Cadence + TTL:** periodic interval (~60 s?) and age‑out window (~3 min?).
5. **Alias collisions:** lowest‑WCB# wins + warn, or warn‑only?
6. **Carrier:** dedicated `PACKET_TYPE_WDP` only, or also piggyback an initial advert on the boot announce?
7. **Persistence scope:** persist the full neighbor table? prune policy for long‑absent boards?
8. **Registry finalization:** role enum values, CAPFLAGS bit assignments, max alias length.
9. **Reserved‑ID allocation:** confirm 20=controller, 19=management, and whether to reserve 17–18 now (each reserved ID shrinks the normal board range).

---

## 17. Appendix — relevant existing pieces

- `espnow_struct_message_etm` (252 B) — the carrier; `structCommand[200]` is the WDP payload field. (`Code/WCB/WCB.ino`)
- ETM packet types: `COMMAND=0`, `ACK=1`, `HEARTBEAT=2`, `ETM_BOOT=11` → propose `WDP=12`.
- `boardTable[MAX_WCB_COUNT]` (`MAX_WCB_COUNT=20`) — existing presence table; WDP table sits alongside.
- Boot announce: `PACKET_TYPE_ETM_BOOT`, `sendETMBootAnnounce()`, drained in `processETMHeartbeats()`.
- Controller peer: `specialPeerEnabled` / `WCB_SPECIAL_PEER_ID` (default 20), command `?CONTROLLER,ON[,<id>]/OFF` (legacy alias `?SPECIAL`).
- Receive gate: MAC‑octet group check + struct password at the top of `espNowReceiveCallback()`.
