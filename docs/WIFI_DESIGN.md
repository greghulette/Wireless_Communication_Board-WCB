# WCB WiFi — hosting an access point, or joining one

Status: **released in 7.0.1** (AP, JOIN and the WebSocket endpoint). The WebSocket token (§7) came after 7.0.1, and
it changes JOIN: a JOIN board with no token keeps WiFi off.

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
- JOIN scans `meshChannel` alone for its SSID first (`WiFi.scanNetworks`, asynchronous, 120 ms) and calls
  `WiFi.begin(ssid, pass, meshChannel, bssid)` only for an AP heard there. `WiFi.begin`'s channel does not pin
  anything: it is where the driver's own connect scan *starts* (`wifi_sta_config_t.channel`, a "channel hint"),
  and with the AP absent that scan swept every channel on each attempt, taking the board off the mesh.
- JOIN asks the driver whether it is associated (`esp_wifi_sta_get_ap_info`), never `WiFi.status()`, and
  **verifies** the AP's own channel from that record: an AP elsewhere is disconnected and the join settles, since
  being deaf is worse than having no WiFi. `WiFi.status()` can lie both ways: the core leaves `WL_CONNECTED`
  standing after some disconnects, and it reconnects once on the first disconnect after every boot whatever
  `setAutoReconnect(false)` says, with a connect scan that sweeps. Reading the radio's channel mid-sweep, the old
  check took a lost AP for an off-mesh association and stopped rejoining for good. On a loss JOIN prints it, cancels
  that reconnect (`WiFi.disconnect()`) and goes back to scanning the mesh channel; an association that gets no
  address within 20 s is dropped and started over.

**The one exception: channel sync moves the channel at runtime** ([CHANNEL_SYNC.md](CHANNEL_SYNC.md)). It never
separates WiFi from the mesh — it moves `meshChannel` and the whole mesh together. A board with no interface up switches
live; a board hosting an access point, or associated with one, saves the new channel and restarts, so the interface
comes up there. And **JOIN follows its network**: with channel sync on, a JOIN board that cannot hear its network on the
mesh channel also looks on every channel (a full-band scan at once - but not inside channel sync's 40 s boot grace,
nor while the board is busy - then 1, 2, 4, 8, 16 and 30 minutes after the network went missing, then every 30
minutes; about 1.5 s off the mesh each). Found on another channel, it moves the mesh there
first (it is about to be pinned, so it leads), then calls `WiFi.begin` with the BSSID on that channel — unless a board it
hears that is joined to another network outranks it, or the channel is outside the region, which it says once. With
channel sync on, the off-mesh check above no longer settles the join for good: it starts the full-band scans over, so a
network that changed channel under an association is found where it went. The 30 s check that warns of a radio off the
mesh channel (`wcbWifiService`) is skipped while a search or a move has the radio.

---

## 3. Modes

| Mode | Behaviour |
|---|---|
| `OFF` | ESP-NOW only. The default. Byte-for-byte the previous behaviour. |
| `AP` | Host a SoftAP on `meshChannel`, `WIFI_AP_STA` so ESP-NOW keeps its interface. Self-contained: no infrastructure, and nothing external can move the radio. The field mode. A WebSocket token (§7) is optional: the WPA2 passphrase decides who reaches the endpoint, and a token, when set, is enforced too. |
| `JOIN` | Associate with an existing AP (a NaviCore's, typically). One network reaches everything and the phone keeps its internet. **Needs a WebSocket token (§7)**: with none, the boot refuses to start WiFi and says why. |

**JOIN never falls back to hosting.** A board that quietly became an access point
because it could not find its network is precisely the rogue-AP surprise this design
avoids — and with several boards configured that way you would get several. It
scans the mesh channel for its network every 5 s indefinitely (and, with channel sync on, every channel on the §2
schedule), logs occasionally, and stays a station.

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
   it runs at priority 2 against the others' 1, so this needs measuring, not flipping. Its
   biggest cost is gone: output pulses are RMT-clocked (`pwmPulse()`, tracker #94), so
   `PWMTask` no longer busy-waits up to 2.5 ms for each one.
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
?WIFI                       Status: mode, SSID, IP, radio channel, token set/none, free heap
?WIFI,OFF                   ESP-NOW only (default)
?WIFI,AP,<ssid>,<pass>      Host an access point
?WIFI,JOIN,<ssid>,<pass>    Join an existing one (needs a token, §7)
?WIFI,TOKEN,<token>         The WebSocket token (§7)
?WIFI,TOKEN,CLEAR           No token
```

All changes persist immediately. A mode applies on the next reboot; a token applies at once, to WebSocket sessions
opened after it (§7).

- The **AP password is mandatory, 8 characters minimum, and fails closed.**
  `WiFi.softAP()` with an empty passphrase creates an *open* network, and with no
  token this interface accepts commands for the whole mesh with no credential of its
  own — a bare `?RESTART` carries none. It is rejected at both the command and at bring-up.
- `?WIFI,JOIN` with no token is **saved, with a warning**, and the boot refuses it. Refusing the command instead
  would break a config push or a restore that sets the token after the JOIN line.
- **Default AP SSID** is `WCB-<alias>`, falling back to `WCB-<number>`. Two droids in
  one room must not advertise the same name.
- The password field is **not** trimmed: a trailing space is legal in a WPA2
  passphrase, and eating it would lock the operator out of their own AP.

### NVS

Namespace `wifi_cfg`: `mode` (u8), `ap_ssid`, `ap_pass`, `jn_ssid`, `jn_pass`, `token` (absent = none; only
`?WIFI,TOKEN` writes it).

### Config round-trip

`?WIFI` is emitted in the config dump (`WIFI,AP,<ssid>,<pass>` / `WIFI,JOIN,…` /
`WIFI,OFF`) so a backup captures it and a restore replays it. The password is
included deliberately: a restore that brought the AP back *without* its passphrase
would fail closed at boot, and the board would come up with no AP and nothing saying
why. `OFF` is emitted too, so restoring a WiFi-off backup actively turns it off
rather than leaving whatever was there.

A token set is emitted as `WIFI,TOKEN,<token>` **right ahead of** the mode line (`collectConfigCommands`, WCB.ino),
in clear, as `?EPASS` already travels: a restore has it before `?WIFI,JOIN`, which warns without one, and before the
reboot that starts JOIN, which refuses to. With no token nothing is emitted, so a chain from a board with none leaves
the target's token as it is: a restore never takes a lock off.

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
back into the tee), no allocation, no TCP send. Only a signed-in session is in the tee (§7): with a token set, a
session that has not sent the right `?AUTH` is sent no console output, and a board whose only client is signed out
does no tee work at all (`wcbWsSinkLive` counts signed-in clients).

**The sender never blocks** (2026-10-14, HIL row `ws.burst_closes_live_client`). `sinkPump` runs on `loop()` and
writes with `send(MSG_DONTWAIT)`: what a socket takes now goes, the rest waits for a later pass. Every client is
sent the same frame and keeps its own place in it; the frame's bytes leave the 2 KB sink only when the slowest
client has sent them. "No room" (`EWOULDBLOCK`) and "no memory" (`ENOMEM` - with the AP up the heap runs within a
few hundred bytes of empty, #111) are not failures; a hard socket error is. A slow client loses whole lines,
reported as `[WS] dropped N output line(s) — client too slow`, instead of being closed. The reports are summed and
printed at most once a second, and a report the full sink cannot take is not itself counted as a drop - with a
stalled client each report otherwise caused the next, one per `loop()` pass. Only a bulk reply on the
loop task (the sink full inside one command) waits, a tick at a time, for a client that took bytes in the last
300 ms (`WS_FRESH_MS`), and for at most 600 ms per pass in all (`WS_PASS_WAIT_MS`). Before, each flush was a
blocking send of up to `send_wait_timeout` (2 s) and the first failure closed the client: a 10-16 KB burst closed a
healthy client on W1's access point and stalled `loop()` 3.5 s, and a 5 s grace window made the stall 9.4 s. A
partial send that timed out was also taken for a whole one, which could cut a frame short.

**Only this file writes to a WebSocket after the handshake.** `/ws` is registered with
`handle_ws_control_frames`, so httpd no longer answers PING and CLOSE itself with a blocking send from its task,
which could land inside a frame the sender had half written. `wsHandler` reads every frame's payload, queues a
PONG that `sinkPump` sends at the next frame boundary, and echoes a CLOSE when no frame is part-way out. A console
frame part-way out finishes first, whatever is queued behind it (a PONG, a session-layer line, §7): the sender used to
hold the frame back for a PONG that was itself waiting for the frame's end, and the client sat stalled until
`WS_STUCK_MS` closed it.

**A heap floor for console output** (`WS_HEAP_FLOOR`, 8 KB). Output handed to lwIP sits in its send buffer and the WiFi driver's dynamic TX buffers until the client ACKs it, and the driver takes a dynamic RX buffer from the same heap for every packet it receives. A client that stopped reading took W1 to 1.4 KB free: its window update could not be received, it was closed as stuck, and the heap stayed short long enough that the next client could not join. Below the floor `sinkPump` hands lwIP nothing more (PONG/CLOSE excepted). A close that httpd cannot queue (its control socket needs a little heap too) keeps the slot `closing` and is asked again every pass, instead of leaving the session open with its whole send buffer.

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

**Two clients at once** (`WS_MAX_CLIENTS`, httpd's `max_open_sockets`). A third
connection still succeeds: `lru_purge_enable` closes the least recently used session
to take it. Where a newcomer finds both of this file's slots still held, it takes one that is closing or failed
first, then one not signed in, and only then the oldest signed-in one; httpd's own purge picks by last activity and is
not ours to steer. Each session costs heap beyond its static 1.5 KB accumulator: its socket,
TCP buffers and httpd's session state. With the AP up a classic ESP32 has about 17 KB
of heap, and with three sessions a `?backup` streamed to them took it to within 48 bytes
of empty (HIL tracker #111). It was three until 2026-10-07.

**A client that leaves without closing is let go at once** (HIL tracker #113). A phone out of range or a PC moved
to another network leaves its socket open while TCP retries it, and everything teed to it waits in lwIP, up to a
TCP send buffer (~5.7 KB) per session. So a hard send error closes the session (`httpd_sess_trigger_close`), not
just the tee, and so does 10 s in which a client that owes output takes none of it (`WS_STUCK_MS`, reported as
`[WS] closed N client(s) that took no output for 10 s`); `wsClose` resets the socket (`SO_LINGER` 0) so its buffers
are freed at once; and when the AP's last station leaves, every session from the AP's /24 is closed on the next
`loop()`. No send blocks `loop()` (see the sender above); httpd's own `send_wait_timeout` of 2 s now covers only its
handshake replies.

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

**No default gateway.** The AP's DHCP server hands out an address and a subnet but
**no router** (DHCP option 3). This AP is a local management link with no upstream,
so advertising itself as the default gateway only invites a client to send
internet-bound traffic to a board that cannot deliver it: a laptop with a second
adapter ends up with two competing default routes, and a phone may decide the whole
network is broken. With no router offered, the client reaches `192.168.4.1` on-link
and keeps its existing default route for everything else.

It is done by clearing `OFFER_ROUTER` from the DHCP server's offer mask with
`esp_netif_dhcps_option(…, ESP_NETIF_ROUTER_SOLICITATION_ADDRESS, …)`. That option is
only accepted while the server is stopped, so the sequence is stop → set → start — the
same one the core's own `APClass::enableDhcpCaptivePortal()` uses. **The server is
restarted unconditionally, even if the set fails**: a stopped DHCP server is exactly
the dead-AP failure `softAPConfig()` produced, and nothing about that symptom points at
the cause. If the current mask cannot be read, the step is skipped entirely rather
than guessed at, leaving the stock behaviour in place.

Verified on hardware (WCB1, `6.2.1_101034RSEP2026`): after a full DHCP exchange the
client's gateway is `(none)` and its default route via the AP is gone, while the lease
itself is still a normal `192.168.4.2` and the WebSocket still connects.

**A client that was already connected keeps the old gateway.** A lease *renewal* is a
REQUEST/ACK, and Windows retained the previous default gateway when the ACK simply
omitted option 3 — `ipconfig /renew` after flashing still showed `192.168.4.1`. Only a
fresh DISCOVER (reconnect, `ipconfig /release` then `/renew`, or lease expiry) picks up
the gateway-free lease. Right after an upgrade, a gateway that is still there means the
client hasn't re-leased yet — not that the firmware didn't apply.

## 7. The WebSocket token

**Why.** `ws://<ip>/ws` is a command channel to the whole mesh: `?ERASE,NVS`, `?reboot`, an OTA, `?EPASS` all run
from it. In AP mode the WPA2 passphrase decides who reaches it. In JOIN mode the board is on someone else's network -
a NaviCore's access point, a home router, a convention's WiFi - and every device on that network reaches the endpoint
with no credential at all. The token is that credential. **JOIN requires one; AP mode works without one** (its
passphrase is the lock) **and enforces one when it is set.**

**The protocol** - the same on every WebSocket that takes commands: this one, NaviCore's, and the `MgmtRelay`
example's in `WCBClient`. Lines are newline-delimited UTF-8 as everywhere else on the socket.

| Board sends | When |
|---|---|
| `[AUTH:REQUIRED]` | A session opens while a token is set; and for every line that session sends before it signs in, at most 2 owed at once (`WS_REQ_OWED_MAX`). That line is dropped, never run. A session that has not signed in 15 s after it opened is closed (`WS_SIGNIN_MS`, `[WS] closed N client(s) that did not sign in within 15 s`): it would otherwise hold one of the two slots for good. |
| `[AUTH:OK]` | `?AUTH,<token>` matched: the session is signed in for its lifetime, gets the console and its lines run. With no token set, any `?AUTH,<anything>` is answered `[AUTH:OK]`, so a client can always send it. |
| `[AUTH:FAIL]`, then a CLOSE (status 1008) | `?AUTH` did not match, or came within 1 s of a failed one on **any** session (each failure starts the second again). The session closes when the client echoes the CLOSE, or 1 s after. |

- A session that has not signed in gets **nothing else**: no console output and no reply to anything it sends.
- A session's state is decided when it opens. Setting, changing or clearing the token takes effect for **new**
  sessions; a session already signed in keeps going (the one that sent `?WIFI,TOKEN` included).
- With no token set nothing is sent on open and nothing changes from before the token existed.
- `?AUTH` is answered by the session layer and never reaches the command dispatcher. Typed on USB or arriving over
  the mesh, where there is nothing to sign in to, it is a no-op (never "Unknown command").
- The tools never send `?AUTH` themselves: Intellex, the desktop host that bridges a tool's page to a droid's
  WebSocket, signs in when the droid says `[AUTH:REQUIRED]`, from the token it saved for that droid.

**The token.** `?WIFI,TOKEN,<token>`: 8-32 printable ASCII characters, none of space `,` `^` `;` `?` `"` `\`. Each
of those means something in the config chain or the line grammar the token travels in (`,` separates fields, `^`
separates commands, `;T` starts a timer chain, `?` is the function character and a trailing one asks for help, `"`
and `\` are quoted by the tools' JSON). `?WIFI,TOKEN,CLEAR` removes it. Stored in NVS `wifi_cfg` key `token`; `?WIFI`
prints `Token : set` or `none`, never the value; `?backup` and a config pull carry it (§6 Config round-trip). A token
NVS refuses is not applied: RAM keeps what NVS holds.

**JOIN with no token does not start.** `wcbWifiStart` refuses at every boot, settles the JOIN state machine as a
missing SSID does, and prints the reason and the fix:

```
[WIFI] JOIN REFUSED: no WebSocket token is set, so WiFi stays OFF.
...
[WIFI] Fix:  ?WIFI,TOKEN,<token>   then reboot
```

**This breaks existing JOIN boards.** JOIN shipped in 7.0.1 without a token, so a 7.0.1 board in JOIN mode that is
updated keeps WiFi off until it is given a token (over USB, or a config push through the mesh) and rebooted. Greg's
decision: an open command channel on a shared network is not a state to preserve. The boot message says so every
boot. `?WIFI,JOIN` itself is still accepted and saved, with a warning, so a push or restore that sets the token after
the JOIN line still ends up whole.

**Implementation** (`WCB_WS.cpp`):

- `wsAdmit` decides every whole line on the httpd task, **before** the command queue: a signed-out session's lines
  never take one of its six slots. It only compares and records: no print, no allocation, no send (the handler rule,
  §6a). The comparison is constant-time over all 32 bytes (`tokenMatches`).
- A session's state lives in its sender slot (`authed`, `failed`, the lines it is owed), under `txLock`, which the
  httpd task (`sinkAdd`, `wsAdmit`) and the loop's sender already share. The token is copied in and out under its own
  spinlock (`tokenMux`): `wcbWsSetToken` runs on the loop task. The lockout's timestamp is touched only by the httpd
  task.
- The answers are owed, not sent: `sinkPump` sends them between data frames, as it does a PONG, so a session that has
  just signed in gets `[AUTH:OK]` ahead of the first console frame it joins. They wait at the heap floor like console
  output; the CLOSE does not.
- **Why a CLOSE and a wait, not a plain close.** `wsClose` resets the socket (`SO_LINGER` 0, tracker #113), and a reset
  can throw away what the client has not read yet - Windows does - so `[AUTH:FAIL]` would vanish and the client would
  see only a dropped connection. Sending a CLOSE and closing on its echo (`WS_BYE_WAIT_MS`, 1 s, if none comes) gets
  the line read first.
- Cost: no heap at all, per session or otherwise; about 16 bytes of static RAM per session slot, and the two copies of
  the token (WCB_WiFi's, WCB_WS's) and the lockout: +104 B static and +5.4 KB flash on the ESP32 (§9).

**Known limit.** A signed-out session still holds one of the two session slots, and httpd's LRU purge closes the least
recently used session to take a new connection (§6a), so anyone who can reach the endpoint can push a signed-in
client off it by connecting. The token stops commands and output, not that. The purge stays: it is what keeps a client
that vanished without closing from locking everyone out (#111, #113).

---

## 8. Wizard integration

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

## 9. Measured cost

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

The static growth is the per-socket accumulators (three, 3 × 1536 B, when this was
measured; two now), the 2 KB output sink and the 3 KB static receive buffer. The command queue is *heap*-allocated
(6 × 1540 B ≈ 9.2 KB) and so comes out of runtime free heap, not this figure.

The token (§7) on top, against `1614c2a`: ESP32 1,455,039 → 1,460,543 flash (**+5.4 KB**) and 113,048 → 113,152
static RAM (**+104 B**); ESP32-S3 1,419,099 → 1,424,599 (**+5.4 KB**) and 111,288 → 111,384 (**+96 B**). No heap: the
session state is in the static sender slots, and the answers are built on the stack as they are sent.

**Measured on hardware**, WCB1 with the AP up and no client attached:

```
Free heap : 110588 bytes (min since boot 109828)
```

That figure was `ESP.getFreeHeap()`, which on a classic ESP32 also counts the ~39 KB IRAM
heap that a byte buffer cannot use (`MALLOC_CAP_INTERNAL`, as #58 found for
`getMaxAllocHeap()`), and it predates most of today's firmware. `?WIFI` now reads the heap
a `String` or a socket buffer comes from (`heap_caps_get_free_size(MALLOC_CAP_8BIT)`,
`WCB_WiFi.cpp`): about 17 KB on a classic ESP32 with the AP up and nobody attached, and
within a few hundred bytes of empty while a client is busy (HIL tracker #111). An
ESP32-S3 keeps about 100 KB.

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
| 2026-10-21 | — | §4: the JOIN state machine runs only on a board that BOOTED in JOIN (`wifiRunMode`, `WCB_WiFi.cpp`), not on the saved mode. A `?WIFI,JOIN` typed into an AP board started joining at once, under the AP still running until the reboot: "lost", scans of the mesh channel, and with channel sync the full-band scans too (HIL `wifi.join_needs_token` caught it; 7.0.1 did the same). `?WIFI,OFF` on a JOIN board likewise leaves the association to the reboot. |
| 2026-10-21 | — | Review fixes. §7: at most 2 `[AUTH:REQUIRED]` owed at once (a flooding client was owed up to 65535), a session not signed in within 15 s is closed, and a newcomer at full slots evicts a closing, failed or signed-out session before a signed-in one. §2: channel sync's JOIN full-band scans wait for the 40 s boot grace and an idle board; a scan running when `?WIFI` changes the mode is still finished and the radio put back; what channel sync reads (associated, AP up, can hop) is the running interface, not the saved mode. |
| 2026-10-21 | — | §2, §3: channel sync ([CHANNEL_SYNC.md](CHANNEL_SYNC.md)) is the one thing that moves the channel at runtime, the whole mesh with it (live, or by a restart with an interface up). JOIN follows its network: full-band scans on a schedule when the mesh-channel scan misses it, a MOVE to the channel it is found on, then `WiFi.begin` there with its BSSID; with channel sync on the off-mesh check no longer settles the join, and the drift warning is skipped while channel sync has the radio. |
| 2026-10-21 | — | **§7 (new): the WebSocket token** (Greg: joining a router exposes the command channel to everyone on that network). `?WIFI,TOKEN,<token>` / `CLEAR` (NVS `wifi_cfg/token`), `[AUTH:REQUIRED]` / `?AUTH,<token>` / `[AUTH:OK]` / `[AUTH:FAIL]` + a 1008 CLOSE, a 1 s lockout after any failure, signed-out sessions outside the tee and their lines dropped before the queue (`wsAdmit`). **JOIN requires a token**: a JOIN board with none keeps WiFi off at boot - a break for 7.0.1 JOIN boards, Greg's decision. §3, §6 (commands, NVS, the token ahead of the mode line in the chain), §6a and §9 (+5.4 KB flash, +~100 B static, no heap) follow; §7-§8 became §8-§9. §6a also: a console frame part-way out now finishes ahead of a queued PONG, which used to hold it until `WS_STUCK_MS` closed the client. Compiled both targets; not run on hardware. |
| 2026-10-14 | `f5348c5` | §6a: `WS_HEAP_FLOOR` (8 KB) - no console output into lwIP below it; a session close httpd could not queue is retried every pass (full run `20261014-025548`: a stalled client starved W1's heap, the close was lost, the next client could not join). |
| 2026-10-14 | `4fd7698` | §6a: the WebSocket sender never blocks - `send(MSG_DONTWAIT)`, a place in the frame per client, `ENOMEM`/`EWOULDBLOCK` retried on a later pass, a slow client losing reported lines instead of its session, at most 600 ms of inline wait per `loop()` pass for a bulk reply, and a client that takes no output for 10 s closed. PING/CLOSE are handled by `wsHandler` so httpd never writes into a half-sent frame. HIL row `ws.burst_closes_live_client` (Greg: "Do it"). |
| 2026-10-07 | `41ce60b` | §6a: a WebSocket client that leaves without closing is let go at once - closed on a failed send and when the AP's last station leaves, reset so lwIP frees its buffers; send timeout 2 s (HIL tracker #113). |
| 2026-10-07 | `48da52a` | §6a: two WebSocket clients at once, not three (HIL tracker #111, Greg's call); a third evicts the least recently used. The memory section no longer reads the old 110 KB `ESP.getFreeHeap()` figure, which counted the IRAM heap, as headroom. |
| 2026-09-28 | `e55ba82` | §2: JOIN reads its association and the AP's channel from the driver, not `WiFi.status()` and the radio's momentary channel, cancels the core's forced reconnect on a loss, and starts over after 20 s associated without an address (tracker #103: the lost network was never noticed and never rejoined). |
| 2026-09-28 | `5770675` | §2: JOIN scans the mesh channel alone before each `WiFi.begin` (tracker #103): the channel argument was only the start of the driver's connect scan, which swept the band while the AP was absent. §3's retry line follows. |
| 2026-09-27 | `1f629a8` | §5 item 1: PWM output pulses are RMT-clocked (tracker #94), so `PWMTask` no longer busy-waits for each pulse; re-pinning it to core 1 costs less than it did. |
| 2026-09-10 | `b898088` | **The AP no longer offers a default gateway.** DHCP option 3 is cleared through `ESP_NETIF_ROUTER_SOLICITATION_ADDRESS`, so a client reaches `192.168.4.1` on-link and keeps its real default route — a board with no upstream advertising itself as the router gives a two-adapter laptop competing default routes and can make a phone reject the network. Stop → set → start with an unconditional restart, and skipped entirely if the offer mask can't be read; see §6a. **Verified on hardware** on `6.2.1_101034RSEP2026` (`b898088`): gateway `(none)` after a full DHCP exchange, lease and WebSocket unaffected — but an already-connected client kept the old gateway through a plain renew. |
| 2026-09-10 | `ba94d3a` | **Code review — five defects fixed, three of which defeated their own purpose.** (1) `WiFi.disconnect(true)` in the JOIN off-mesh guard: the bool is `wifioff`, and in STA-only mode it runs `STA.end()` → `WIFI_MODE_NULL`, stopping WiFi and **taking ESP-NOW with it** — the guard that exists to keep the board on the mesh was the one thing reliably killing it. Now `WiFi.disconnect()`. (2) The output sink was mutated from the printing task (including the ESP-NOW callback) while `sinkPump()` memmoved it on the loop task, with no lock — torn output indistinguishable from a real reply. Now guarded by a `portMUX`, with the critical sections deliberately never spanning the TCP send. (3) JOIN latched `joinSettled` on **success**, so with auto-reconnect off nothing ever retried after the AP rebooted or the droid drove out of range, and `wifiUp` never cleared so `?WIFI` reported "up" with no address. Only the off-mesh case settles now. (4) An oversized inbound frame returned without draining the payload, leaving it to be parsed as the next frame header and desynchronising the session permanently; it now returns `ESP_FAIL` so httpd closes cleanly. (5) `WIFI,` was missing from `dataBearingVerb`, so a passphrase ending in `?` was eaten by the trailing-`?` help shortcut and never saved. Also: the WS drain now `trim()`s like the Serial0 reader (a leading space made a line unprefixed, i.e. a mesh-wide broadcast), and bring-up prints `ws://…/ws` instead of a `http://…/` that was always a 404. |
| 2026-09-07 | `7586a19` | **Bulk replies were being truncated over the socket.** `wcbWsSinkWrite()` dropped whole lines once the 2 KB staging buffer filled, and it only drained once per `loop()` pass — but a config dump is a tight run of `Serial.println()` with no `loop()` iteration between them, so most of a ~3 KB reply was discarded. It now flushes **inline, but only while running on the loop task** (handle captured in `wcbWsService()`); off that task — above all the ESP-NOW callback — it still drops, because a TCP send there would stall the WiFi task. Verified: `WCB_WEBTOOL_CONFIG_PULL` over the socket returns 3,024 bytes / 67 lines with **0 drops**, ending in its checksum, `WIFI,` line included. |
| 2026-09-07 | `7586a19` | **`softAPConfig()` removed entirely — it breaks DHCP in either position.** See §6a; the board stays on the stock `192.168.4.1` and identity moves into the protocol. |
| 2026-09-07 | `7586a19` | **VERIFIED ON HARDWARE.** `softAPConfig()` was being called *before* `softAP()`, which leaves the DHCP server stopped — clients associated, got no lease, fell back to `169.254.x` and could not reach the board. Moved it after. Confirmed end to end on WCB1 (ESP32-D0WD): lease `192.168.4.2`, WebSocket connected to `ws://192.168.4.1/ws`, `?WIFI` and `?VERSION` answered over the socket, and an unsolicited `[ETM] WCB19 came ONLINE` streamed through — so the output tee carries events, not just command replies. Free heap 77,044 with a client attached (min 61,236). |
| 2026-09-06 | `eec12d5` | **Added the WebSocket endpoint** (`WCB_WS.{h,cpp}`, `ws://<ip>/ws`), which is what makes the AP useful — until now the radio was up with nothing listening. Feeds `processSerialCommandHelper()`, so it is the same command surface as USB. Handler copy-enqueue-returns on Core 0 and never prints; commands run from `loop()`; output tees through `WCBSerial::write()`. Line cap 1536 B, not MgmtRelay's 384, because `?OTALOCAL` uses 1 KB chunks. The AP also moved to `192.168.4.<board number>` so it stops colliding with NaviCore's default `.1`. Measured free heap before this landed: 110,588 B. |
| 2026-09-05 | `766f52d` | **`WCB_WiFi.cpp` was printing into an unopened port.** It omitted `#include "WCB_RemoteTerm.h"`, which ends in `#define Serial WCBDebugSerial`; `setup()` only calls `begin()` on that wrapper, so the core's raw `Serial` is never opened. `?WIFI` matched, ran and printed nothing — a *dead command* with no error, because the command was known. Recorded as rule 12 in `CLAUDE.md`, since it applies to any new subsystem file. |
| 2026-09-03 | `867addf` | WiFi moved into the Wizard board card (advanced-only) and the standalone `wifi-tool.html` removed — `?WIFI` is in the restorable config chain, so the Wizard has to own it or a push would silently undo an external tool. Wired through all four `parser.js` points, and `WIFI,` added to `commandStringNeedsReboot()`; without it a push applied the setting and never asked for the reboot that makes it real. |
| 2026-09-02 | `55df332` | Initial design and implementation on branch `WIFI`. `?WIFI` with OFF/AP/JOIN modes, channel locked to `?WCBCH`, fail-closed AP password, non-blocking JOIN with channel verification and no AP fallback, NVS `wifi_cfg`, config round-trip, and `Wizard/wifi-tool.html`. Section 5 records the open soft-serial interaction, which gates enabling this on a board with PWM / Kyber / raw-mapped / local-Maestro ports. |
