# WCB WiFi — hosting an access point, or joining one

Status: **in development on branch `WIFI`.** Not in a release.

A WCB can optionally bring up an IP interface — hosting its own access point, or
joining an existing one — so the mesh can be managed from a phone with no USB cable
and no extra hardware. It is **off by default**, and with it off the firmware behaves
exactly as it did before.

---

## 1. Why this lives on the boards

A phone cannot open a serial port. The Web Serial API is desktop-only — not Android
Chrome, never iOS — so a mobile client has no way to reach the mesh except over a
network. That single platform limit is what forces this design.

The alternatives were both rejected:

| Option | Why not |
|---|---|
| A dedicated relay board (`MgmtRelay` in `WCBClient`) | An extra ESP32, an extra power tap and an extra thing to fail, in every droid, whose only job is management. Fine as a bench tool; not something to ask every builder to install. |
| Require a NaviCore | Only a small subset of WCB users have one. Its AP cannot be assumed to exist. |

So the boards have to be able to carry it themselves. The relay and NaviCore paths
still work and are still useful — they are simply not the answer for most builders.

---

## 2. The one rule: the radio has one channel

The ESP32 has a **single radio**. An access point we host, or one we associate with,
shares its channel with ESP-NOW. Therefore:

> **The WiFi channel is always `meshChannel` (`?WCBCH`) and is deliberately not
> separately configurable.**

This is not a simplification for convenience. A WiFi channel that disagrees with the
mesh is a *silent, total* blackout: every ESP-NOW packet dropped, nothing logged on
the other boards, no fault indication anywhere. Offering the choice would mean
offering a way to brick the mesh with no symptom. Every entry point passes
`meshChannel` explicitly:

- `WiFi.softAP(ssid, pass, meshChannel, …)` — the channel argument **defaults to 1**.
  Once an AP owns the radio nothing later moves it, so a defaulted channel against a
  mesh on any other channel is exactly the blackout above.
- `WiFi.begin(ssid, pass, meshChannel)` — pins the association attempt to one channel.
- After associating, JOIN mode **verifies** the channel with `esp_wifi_get_channel()`
  and disconnects if it landed elsewhere. Being deaf is worse than having no WiFi.

---

## 3. Modes

| Mode | Behaviour |
|---|---|
| `OFF` | ESP-NOW only. The default. Byte-for-byte the previous behaviour. |
| `AP` | Host a SoftAP on `meshChannel`, `WIFI_AP_STA` so ESP-NOW keeps its interface. Self-contained: no infrastructure, and nothing external can move the radio. The field mode. |
| `JOIN` | Associate with an existing AP (a NaviCore's, typically). One network reaches everything and the phone keeps its internet. |

**JOIN never falls back to hosting.** A board that quietly became an access point
because it could not find its network is precisely the rogue-AP surprise this design
avoids — and with several boards configured that way you would get several. It
retries every 5 s indefinitely, logs occasionally, and stays a station.

---

## 4. Ordering constraints

Two orderings are load-bearing, and they pull in opposite directions.

**`wcbWifiStart()` is called from `setup()` after `esp_wifi_set_mac()` and before
`esp_now_init()`.** That is the only point that satisfies both:

- *After* `esp_wifi_set_mac()`, so the ESP-NOW MAC is stamped on a plain `WIFI_STA`
  interface before an AP is added alongside it.
- *Before* `esp_now_init()`, because in AP mode the SoftAP must own the radio channel
  first. Once ESP-NOW is initialised nothing later moves the radio, and an AP raised
  afterwards fights it.

This mirrors what `WCB_Client` had to handle when NaviCore began hosting an AP:
`WCB_Client::begin()` inspects `WiFi.getMode()` and preserves an existing AP as
`WIFI_AP_STA` rather than forcing `WIFI_STA` and tearing it down.

**Settings apply at the next boot, not live.** Same discipline as
`saveMeshChannelToPreferences()`. Bringing an interface up or down re-enters the WiFi
driver underneath a running `esp_now`, and a config push arrives as a *stream* of
commands — losing the radio midway would strand everything still queued behind it.

---

## 5. Interaction with bit-banged serial (S3–S5) — **open**

This is the unresolved risk, and it is tracked here so it is not rediscovered.

S3–S5 are `EspSoftwareSerial`. TX busy-waits each bit period, so an interrupt landing
mid-byte compresses the remaining bits and the **receiver mis-frames** — the command
arrives cut short, not garbled. `enableIntTx(false)` fixes this by taking a critical
section, bounded to one byte (~0.94 ms at 9600) because `lazyDelay()` restores
interrupts at every stop bit.

`softSerialLoopTaskOnly()` (`WCB.ino`) only enables that protection where **no core-0
task writes the port**, because the library's interrupt mux is a single `static`
spinlock shared by all three ports — core 0 would otherwise spin on it with its own
interrupts disabled, inside the WiFi task. Ports excluded today:

- PWM input or output (`PWMTask` is pinned to core 0)
- the local Kyber port
- raw-mapped ports
- ports hosting a locally-attached Maestro

…all four because `espNowReceiveCallback` writes them directly from the WiFi task.

**Hosting an AP adds radio interrupt load** — beacons, associated stations, TCP — on
exactly those unprotected ports. Telling users "only enable WiFi on a board with none
of those configured" is not a deployable constraint.

The fix is to remove core-0 writers so every port qualifies for protection:

1. **`PWMTask`** is pinned to core 0 by choice (`WCB.ino`, `xTaskCreatePinnedToCore(…, 0)`).
   Every other task is already on core 1. Re-pinning removes the PWM exclusion — but
   it runs at priority 2 against the others' 1, so this needs measuring, not flipping.
2. **`espNowReceiveCallback`** cannot move; it is the WiFi task. Its direct writes to
   Maestro, Kyber and raw-mapping ports must be handed to core 1 instead. The pattern
   and the consumer already exist — `mgmtQueueOut`/`drainMgmtOut`, and
   `RawSerialForwardingTask` polls every 2 ms — so the added latency is ≤2 ms.

Doing both also closes the residual noted in the source: a raw-serial mapping on a
*remote* board can target a port this board believes is idle, which is not locally
knowable and so cannot be handled by the predicate.

**Sequencing:** close the soft-serial timing first and confirm it on the bench, then
enable the AP on top. Doing them together leaves you unable to tell which change
caused a regression.

---

## 6. Commands

```
?WIFI                       Status: mode, SSID, IP, radio channel, free heap
?WIFI,OFF                   ESP-NOW only (default)
?WIFI,AP,<ssid>,<pass>      Host an access point
?WIFI,JOIN,<ssid>,<pass>    Join an existing one
```

All changes persist immediately and apply on the next reboot.

- The **AP password is mandatory, 8 characters minimum, and fails closed.**
  `WiFi.softAP()` with an empty passphrase creates an *open* network, and this
  interface accepts commands for the whole mesh with no credential of its own — a
  bare `?RESTART` carries none. It is rejected at both the command and at bring-up.
- **Default AP SSID** is `WCB-<alias>`, falling back to `WCB-<number>`. Two droids in
  one room must not advertise the same name.
- The password field is **not** trimmed: a trailing space is legal in a WPA2
  passphrase, and eating it would lock the operator out of their own AP.

### NVS

Namespace `wifi_cfg`: `mode` (u8), `ap_ssid`, `ap_pass`, `jn_ssid`, `jn_pass`.

### Config round-trip

`?WIFI` is emitted in the config dump (`WIFI,AP,<ssid>,<pass>` / `WIFI,JOIN,…` /
`WIFI,OFF`) so a backup captures it and a restore replays it. The password is
included deliberately: a restore that brought the AP back *without* its passphrase
would fail closed at boot, and the board would come up with no AP and nothing saying
why. `OFF` is emitted too, so restoring a WiFi-off backup actively turns it off
rather than leaving whatever was there.

---

## 6a. The WebSocket endpoint (`WCB_WS.{h,cpp}`)

`ws://<board-ip>/ws` — a second mouth for the same line protocol the USB port
speaks. Lines go to `processSerialCommandHelper()`, the identical dispatcher the
Serial0 reader feeds, so the two transports cannot drift. Starts automatically on
the first `loop()` pass where `wcbWifiReady()` is true, latched so a failed start
is not retried forever.

**Concurrency.** The httpd task is Core 0, beside the ESP-NOW receive callback. The
handler may only copy, enqueue and return — `processSerialCommandHelper()` writes
NVS, drives bit-banged SoftwareSerial and blocks on ETM retries. `wcbWsService()`
runs one command per pass from `loop()` on Core 1. The handler also **never
prints**: UART0 has no TX buffer, so a print there blocks the httpd task and holds
the UART0 mutex against `loop()`. Drops are counted and reported from `loop()`.

**Output tee.** `WCBSerial::write()` gains a third destination alongside USB and the
mesh relay. Teeing at that one point rather than at each print site is what keeps
the transports identical — handlers go on printing exactly as they always have.
`wcbWsSinkWrite()` only appends to a buffer: no print (that would recurse straight
back into the tee), no allocation, no TCP send.

**Per-command flags.** The drain resets `lastReceivedViaESPNOW` and `inSequenceBody`
before dispatching, exactly as the Serial0 reader does. They are snapshotted per
queue item, so a value left from a prior mesh recall's body drain would otherwise
latch and suppress this command's fan-out.

**Line cap is 1536, not MgmtRelay's 384.** The relay's socket only carries
`?OTA,DATA` with a 192-byte firmware chunk. This endpoint is the one an OTA over
WiFi uses, and that is the *direct* path — `?OTALOCAL,DATA,<base64>` with a
1024-byte chunk (`const CHUNK = 1024` in the Wizard), which encodes to ~1368 chars
plus prefix. At 384 every DATA frame would hit the line-too-long guard, and the
transfer would ACK the BEGIN and then move nothing.

**AP address: the stock `192.168.4.1`. Do not call `softAPConfig()`.**

The plan was `192.168.4.<board number>`, so a host could tell a WCB from a NaviCore
by address — every ESP32 SoftAP defaults to `.1`, so probing it finds *something*
and cannot say what. It was tried **both before and after `softAP()` and breaks DHCP
either way**: the AP comes up, clients associate, `?WIFI` reports the correct IP, and
no lease ever arrives, so every client falls back to `169.254.x` and cannot reach the
board at all.

Measured repeatedly on hardware — Windows reports *"unable to contact your DHCP
server"* — and it is **not** a startup race: it still failed on a board that had been
up for minutes. It appeared to work once, which makes it worse than useless. An
intermittent DHCP failure costs far more to own than a duplicate default address.

Nothing about the symptom points at the cause: the address looks right in `?WIFI`
either way, and the AP is plainly up because clients associate to it.

**Identity belongs in the protocol, not the address.** A host should ask — one
command over the socket names the board — rather than infer from an IP.

## 7. Wizard integration

WiFi is configured from the **board card** in the Setup Wizard, under an
`advanced-only` subsection — most builders never need it, and putting an access point
on a droid is a decision worth making deliberately.

It is a **per-board** field, unlike `meshChannel` which is system-wide, so it lives in
`boardConfigs[n].wifi` rather than in general settings. Four integration points in
`Wizard/parser.js`, all of which must move together:

| Point | What |
|---|---|
| defaults | `wifi: { mode, apSsid, apPass, joinSsid, joinPass }` |
| `case 'WIFI'` | parses `WIFI,OFF` / `WIFI,AP,<ssid>,<pass>` / `WIFI,JOIN,…` |
| command generation | emits one `WIFI,…` line, only when something changed |
| `check('wifi', …)` | so the diff engine sees an edit as unsaved |

The password is split off manually rather than taken from `parts[]`: a WPA2 passphrase
may contain commas, so everything after the SSID field is password.

`commandStringNeedsReboot()` in `app.js` lists `WIFI,`. Without that entry a push
applies the setting and never prompts for the restart that makes it take effect — the
board would silently keep its old WiFi state. The generator only emits the line when
something actually changed, so unrelated edits do not nag for a reboot.

**The channel is deliberately absent from this UI.** Offering it would be offering a
way to take the board off the mesh with no visible symptom. The note line instead
states which channel WiFi will run on, read from the live `meshChannel`.

### Why there is no separate WiFi tool

An earlier cut of this work shipped `Wizard/wifi-tool.html`, a standalone Web Serial
page. It was removed. Two reasons, both concrete:

- `?WIFI` is already in the **restorable config chain**, so the Wizard has to
  understand it regardless — a tool that set it independently would be silently
  undone by the next config push or restore.
- Two same-origin tabs cannot both hold a serial port.

### The transport constraint, and why it is already solved

A page served over **HTTPS cannot open a plaintext `ws://` or `http://`** connection to
a board. Mixed content is a hard block with no click-through, so the Wizard's published
GitHub Pages URL can never talk to a board over WiFi.

**NaviLink already routes around this** by serving the config-tool UI itself over
`http://127.0.0.1:PORT` and putting its WebSocket on that same origin. The host process
decides whether the far end is a COM port or the droid's AP, so serial and WiFi become
one code path in the page. Any browser-based network management has to be served that
way — from the host, not from Pages.

---

## 8. Measured cost

Baseline is 6.2.1 (`66845b9`), `PartitionScheme=min_spiffs`, core `esp32:esp32@3.3.4`.

| Target | Flash before | Flash after | Static RAM before | after |
|---|---|---|---|---|
| ESP32 | 1,299,367 (66%) | 1,309,559 (66%) | 95,664 (29%) | 95,744 (29%) |
| ESP32-S3 | 1,265,247 (64%) | 1,275,559 (64%) | 93,912 (28%) | 94,000 (28%) |

So the WiFi module itself costs ~10 KB of flash and under 100 bytes of static RAM on
both targets — neither percentage moved.

Adding the WebSocket endpoint (`WCB_WS`) on top:

| Target | Flash | Static RAM |
|---|---|---|
| ESP32 | 1,345,847 (68%) — **+36.3 KB** | 105,552 (32%) — **+9.8 KB** |
| ESP32-S3 | 1,311,099 (66%) — **+35.5 KB** | 103,808 (31%) — **+9.8 KB** |

The static growth is the three per-socket accumulators (3 × 1536 B), the 2 KB output
sink and the 3 KB static receive buffer. The command queue is *heap*-allocated
(6 × 1540 B ≈ 9.2 KB) and so comes out of runtime free heap, not this figure.

**Measured on hardware**, WCB1 with the AP up and no client attached:

```
Free heap : 110588 bytes (min since boot 109828)
```

A 760-byte spread between current and minimum means a flat, unfragmented heap — this
is headroom, not a lucky sample. Against ~9 KB for the queue plus httpd's own socket
and TCP allocations, that leaves comfortable margin on the *classic* ESP32, which is
the tighter of the two targets.

That is the WiFi module only — no web server yet. For reference, the WebSocket
endpoint in `MgmtRelay` measured **+37 KB flash / +4.7 KB static RAM**, which would
take the ESP32 to roughly 68%.

**Runtime heap is the number that actually decides this**, and it needs hardware —
`httpd` plus the TCP stack want tens of KB, and AP+STA costs more than STA alone.
`?WIFI` reports `ESP.getFreeHeap()` and `getMinFreeHeap()` so it can be measured on a
board rather than guessed at. Static analysis says it fits; that is not the same
claim.

---

## Revision log

| Date | Commit | Change |
|---|---|---|
| 2026-09-10 | _(pending)_ | **Code review — five defects fixed, three of which defeated their own purpose.** (1) `WiFi.disconnect(true)` in the JOIN off-mesh guard: the bool is `wifioff`, and in STA-only mode it runs `STA.end()` → `WIFI_MODE_NULL`, stopping WiFi and **taking ESP-NOW with it** — the guard that exists to keep the board on the mesh was the one thing reliably killing it. Now `WiFi.disconnect()`. (2) The output sink was mutated from the printing task (including the ESP-NOW callback) while `sinkPump()` memmoved it on the loop task, with no lock — torn output indistinguishable from a real reply. Now guarded by a `portMUX`, with the critical sections deliberately never spanning the TCP send. (3) JOIN latched `joinSettled` on **success**, so with auto-reconnect off nothing ever retried after the AP rebooted or the droid drove out of range, and `wifiUp` never cleared so `?WIFI` reported "up" with no address. Only the off-mesh case settles now. (4) An oversized inbound frame returned without draining the payload, leaving it to be parsed as the next frame header and desynchronising the session permanently; it now returns `ESP_FAIL` so httpd closes cleanly. (5) `WIFI,` was missing from `dataBearingVerb`, so a passphrase ending in `?` was eaten by the trailing-`?` help shortcut and never saved. Also: the WS drain now `trim()`s like the Serial0 reader (a leading space made a line unprefixed, i.e. a mesh-wide broadcast), and bring-up prints `ws://…/ws` instead of a `http://…/` that was always a 404. |
| 2026-09-07 | _(pending)_ | **Bulk replies were being truncated over the socket.** `wcbWsSinkWrite()` dropped whole lines once the 2 KB staging buffer filled, and it only drained once per `loop()` pass — but a config dump is a tight run of `Serial.println()` with no `loop()` iteration between them, so most of a ~3 KB reply was discarded. It now flushes **inline, but only while running on the loop task** (handle captured in `wcbWsService()`); off that task — above all the ESP-NOW callback — it still drops, because a TCP send there would stall the WiFi task. Verified: `WCB_WEBTOOL_CONFIG_PULL` over the socket returns 3,024 bytes / 67 lines with **0 drops**, ending in its checksum, `WIFI,` line included. |
| 2026-09-07 | _(pending)_ | **`softAPConfig()` removed entirely — it breaks DHCP in either position.** See §6a; the board stays on the stock `192.168.4.1` and identity moves into the protocol. |
| 2026-09-07 | _(pending)_ | **VERIFIED ON HARDWARE.** `softAPConfig()` was being called *before* `softAP()`, which leaves the DHCP server stopped — clients associated, got no lease, fell back to `169.254.x` and could not reach the board. Moved it after. Confirmed end to end on WCB1 (ESP32-D0WD): lease `192.168.4.2`, WebSocket connected to `ws://192.168.4.1/ws`, `?WIFI` and `?VERSION` answered over the socket, and an unsolicited `[ETM] WCB19 came ONLINE` streamed through — so the output tee carries events, not just command replies. Free heap 77,044 with a client attached (min 61,236). |
| 2026-09-06 | _(pending)_ | **Added the WebSocket endpoint** (`WCB_WS.{h,cpp}`, `ws://<ip>/ws`), which is what makes the AP useful — until now the radio was up with nothing listening. Feeds `processSerialCommandHelper()`, so it is the same command surface as USB. Handler copy-enqueue-returns on Core 0 and never prints; commands run from `loop()`; output tees through `WCBSerial::write()`. Line cap 1536 B, not MgmtRelay's 384, because `?OTALOCAL` uses 1 KB chunks. The AP also moved to `192.168.4.<board number>` so it stops colliding with NaviCore's default `.1`. Measured free heap before this landed: 110,588 B. |
| 2026-09-05 | _(pending)_ | **`WCB_WiFi.cpp` was printing into an unopened port.** It omitted `#include "WCB_RemoteTerm.h"`, which ends in `#define Serial WCBDebugSerial`; `setup()` only calls `begin()` on that wrapper, so the core's raw `Serial` is never opened. `?WIFI` matched, ran and printed nothing — a *dead command* with no error, because the command was known. Recorded as rule 12 in `CLAUDE.md`, since it applies to any new subsystem file. |
| 2026-09-03 | _(pending)_ | WiFi moved into the Wizard board card (advanced-only) and the standalone `wifi-tool.html` removed — `?WIFI` is in the restorable config chain, so the Wizard has to own it or a push would silently undo an external tool. Wired through all four `parser.js` points, and `WIFI,` added to `commandStringNeedsReboot()`; without it a push applied the setting and never asked for the reboot that makes it real. |
| 2026-09-02 | _(pending)_ | Initial design and implementation on branch `WIFI`. `?WIFI` with OFF/AP/JOIN modes, channel locked to `?WCBCH`, fail-closed AP password, non-blocking JOIN with channel verification and no AP fallback, NVS `wifi_cfg`, config round-trip, and `Wizard/wifi-tool.html`. Section 5 records the open soft-serial interaction, which gates enabling this on a board with PWM / Kyber / raw-mapped / local-Maestro ports. |
