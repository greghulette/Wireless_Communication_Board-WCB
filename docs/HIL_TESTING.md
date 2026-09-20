# Hardware-in-the-loop tests (`tests/hil`)

A Python harness, with a GUI, that drives real boards over USB and checks what they actually do —
the bytes a WCB puts on a serial port, the frames a Maestro receives, what comes back over the
mesh. It complements the compiler and `tests/wdp_wire_test.cpp`; it does not replace them, and
no CI runs it, because it needs the physical bench.

The one dependency is `pyserial` (the GUI uses tkinter, which ships with Python). On this PC the
`python` on PATH (PyManager) has it. **VS Code may run the file with a different interpreter** — a
uv-managed Python without pyserial — and the GUI then shows a message saying so. Double-clicking
`tests/hil/WCB Bench.cmd` always uses the PATH Python.

```bash
tests/hil/WCB Bench.cmd                      # double-click: the GUI
python tests/hil/gui.py                      # the GUI: devices, wiring, tests, log
python tests/hil/run.py                      # every test, command line
python tests/hil/run.py "maestro.*" "wled.*" # ids matching globs
python tests/hil/run.py --discover           # re-detect the wires first
python tests/hil/run.py --list | --plan | --links
```

Each run writes `tests/hil/results/<stamp>/report.md` and `session.log` — every line sent and
received on every port, timestamped, with `# =====` markers per test. The wires found last time
are in `results/links.json`. The directory is gitignored: the log carries the mesh password
(`?backup`, `?MGMT,PULL`).

**Before a run, close anything holding a bench port** — the Arduino IDE serial monitor, Wizard
or NaviCore tool tabs. A COM port has one owner. The GUI's *Close all ports* hands them back.

---

## 1. What a run does to the bench

The suite is not read-only. It **changes saved config** (labels, baud, sequences, variables),
**moves servos** (including Maestro subroutine restarts), **plays NaviCore animations**, moves
the SBUS controller's virtual sticks, and **reboots WCBs**.

Every test that changes saved config runs inside `config_guard` (`suites/common.py`): it
snapshots each affected board's config chain first and **fails the test if the board does not
end byte-identical**, even when the test body itself failed, naming the missing and extra
tokens. The snapshot is `?backup`'s configured-boards chain for the USB board and
`?MGMT,PULL,<n>` for a board reached over the mesh, CRC-checked in both cases. `?PEERSLIVE` is
excluded (it moves with learned peers on its own), and a `?SEQ,SAVE` value that itself contains
`^` is re-joined the way restore parses it (`WCB.ino:2326-2349`). After the guard, the harness
refreshes its config cache and re-binds any wire that follows the port's baud.

Two things are deliberately never done, because they cannot be cleanly undone:

- **No test adds a local Maestro id while WDP is on.** A local id is advertised over WDP, every
  other board auto-adds a remote proxy for it, and proxies are never evicted (`CLAUDE.md` rule 5).
  The tests that add a local id, or clear and rebuild the slot table, send `?WDP,OFF` first — it
  stops both the board's adverts and its handling of other boards' adverts (`WCB_WDP.cpp:356`,
  `:488`) — and `?WDP,ON` only once the table is back. Remote proxies are never advertised (only
  local slots are, `WCB_WDP.cpp:142-167`), so adding and clearing one is restorable.
- **No test sends free text to the SBUS controller.** Outside a `{…}` line its serial parser
  treats single characters as commands, and `m` and `w` write flash.

---

## 2. The bench and its wires

### `tests/hil/bench.json`

| Key | Meaning |
|---|---|
| `devices.<name>` | `port` (COM name), `kind` (`wcb` / `probe` / `navicore` / `sbus`); a `wcb` also has `wcb` (its board number), a `probe` its `mac`. `wcb1` is the **main console**, which sends every mesh command; any other WCB with its own USB cable is `wcb<N>`, and its config is read with `?backup` on that port instead of a `?MGMT,PULL`. |
| `mesh_only_wcbs` | Board numbers with no USB cable, reached only through the mesh from `wcb1`. |
| `wiring_plan` | Which probe each WCB's ports are suggested to go to (`{"1": "probe1"}`), header S*n* to port S*n*. Only a suggestion: any header can go to any port on any WCB, and when a header is already used the "what to connect" list suggests the next free one. |
| `taps` | WCB ports where a real device already sits on the line, so the probe only listens (`RXONLY`) and never transmits. |
| `port_stimulus` | For a port with a real device, the command that makes it transmit safely and the exact bytes expected, instead of text (e.g. `;M2,getErrors` → `AA 02 21` for a Maestro). |
| `opt_in` | Tests that are skipped unless named here, because they wear flash or wipe a board's data: `nvs_wear` (fills W1's 100-variable table with persistent variables), `seq_wipe` (clears every W1 sequence, then replays them), `ota_erase` (OTA sessions, each erasing at least 4 KB of a board's inactive app slot), `ota_full` (full-image OTA on W1, two passes), `ota_full_wcb2` (the same, relayed to W2), `ota_wrong_chip` (an S3 image streamed to W1; attended), `navicore_clip` (replays a saved NaviCore clip: servo motion and recorded actions), `sbus_reset` (reboots the SBUS controller). |
| `discover_skip` | Port keys (`"W2S1"`) that discovery leaves out. |
| `maestro_safe_sub` | A Maestro subroutine number that is safe to run on Maestro 2 and on every NaviCore Maestro. Without it, `maestro.m0_broadcast` checks only `;M0,stopScript` and skips the subroutine broadcasts. |
| `port_devices` | Real devices wired to WCB ports instead of a probe, keyed by port: `"W1S5": {"kind": "navicore", "detail": "S1"}`. Kinds: `maestro`, `navicore`, `hcr`, `mp3`, `dfp`, `wled`, `wcb`, `other`. Set from the Wiring tab. A probe wire on such a port is always a listen-only tap. Unless `port_stimulus` names a safe command for the port, the port is closed to test traffic: tests never see a wire on it (`links.get` hides it); a test that names the port (as a wire, as a literal `;S<n>`, `;W<n>;S<p>` or `W<n>S<p>` in its source, or in `drives=[...]`) skips and names the device; auto-detect, *Verify* and `link.out` send nothing to it; and a wire added there by hand survives auto-detect. Broadcast fan-out still follows the board's own `?BCAST` settings, so turn broadcast output off for that port on the WCB. |

The wires themselves are **not** listed. They are discovered and saved to `results/links.json`.

### Discovering the wires

Every probe arms edge counters, with pull-ups, on every free header pin. Then each WCB port
transmits in turn: sixteen `U` (0x55, an edge on every bit), or the port's `port_stimulus`. Every
WCB is swept. One with its own USB cable is driven there with `;S<n>`; one without goes through
`wcb1` as `;W<w>;S<n>`. A pin that stayed quiet during a baseline window and
toggled during the stimulus is the wire. If it is the header's TX pin, the cable is
straight-through and the link is bound with `SWAP`. Each wire is then confirmed by the exact
bytes at the WCB port's configured baud (`verified`). When several pins toggle — parallel wires
in one cable crosstalk — the busiest pin wins, and the run log says so.

In the GUI a wire can also be set by hand (click a WCB port, then a probe header). Setting one
verifies it and tries both cable orientations.

A test uses `link(bench, 1, "S3")` (or `wire(...)`). If that wire is not on the bench the test is
**skipped**, saying which wire it needs. The runner reads the wires a test uses from its source,
one level into module helpers, so the GUI can say which wire unlocks which test. A test that
picks ports at runtime declares them with `@test(..., links=[...])`.

### Channels and baud

A probe has five channels: A and B are hardware UARTs, C, D and E are EspSoftwareSerial. A wire
is bound only while a test uses it. Above 38 400 baud it gets a hardware channel; at or below it
a software one; when a probe runs out, its least-recently-used wire gives its channel up. Probe
headers S1 and S2 always take a hardware channel: EspSoftwareSerial refuses their pins on the
PSRAM-equipped PICO-V3-02 (GPIO 6–11, 20, and 16–17, `SoftwareSerial.h:51-55`), so a software bind
there fails with `software serial begin failed` (`HW_ONLY_HEADERS` in `hil/probe.py`). With
no explicit baud, a wire follows the WCB's configured baud for that port (`?BAUD,S<n>` from the
config chain, cached).

**A probe's channel table is cleared in place, never replaced.** `bind()` holds a reference to it
while it binds, and resolving `link.probe` can reset the probe on its first use, which lands in
`forget_probe`. While that popped the table, the first bind on a probe registered into an orphan:
the next wire was handed the same channel, its `BIND` silently re-pointed that channel at its own
header, and the first test went on reading another port's traffic — seen as a wire that received
nothing, or every marker "lost" on the other one.

**Opening a port never resets the board.** `SerialDevice` deasserts DTR and RTS *before*
`open()`. On the CP210x / CH9102 auto-reset circuits those lines are EN and GPIO0, so the
default pyserial open would reset the board. NaviCore's native USB-Serial/JTAG port also stays
up with this open. The SBUS controller may still reset, so its tests wait up to 60 s for `pong`.

---

## 3. The probe (`tests/hil/wcb_probe`)

Instrument firmware for a spare **WCB V2.4 board** (ESP32-PICO-V3-02). It is not WCB firmware.
The PC configures it at runtime, so **a new test never needs new probe firmware**.

```bash
arduino-cli compile --fqbn esp32:esp32:esp32 --build-path <dir> tests/hil/wcb_probe
arduino-cli upload  --fqbn esp32:esp32:esp32 --input-dir <dir> -p COMx
```

It needs EspSoftwareSerial and WCB_Client, both from the Arduino-Code sketchbook. The code is in
`probe_main.cpp`, not the `.ino`: the Arduino preprocessor puts generated prototypes above the
struct definitions in an `.ino`, so functions taking a `Channel&` would not compile.

- **Pins** are the V2.4 map, copied from `wcb_pin_map.cpp` (`wcb_hw_version == 24`). If that map
  changes, `HDR_TX` / `HDR_RX` must follow.
- **Serial:** any header, any baud, formats 7/8 data bits with none/even/odd parity and 1/2 stop
  bits, optional inversion. `SWAP` listens on the header's TX pin. `RXONLY` never transmits (taps).
  Framing, parity and overflow errors are reported (`RXERR`), so a bit-banged WCB port that
  mis-times a byte shows up as an error, not as silently wrong bytes.
- **Reply rules** (`RULE ADD`): when bytes matching a pattern (`??` = any byte) arrive on a channel,
  the probe writes a reply itself, optionally delayed. A USB round trip is too slow for devices
  like a Maestro, whose get-query answer must arrive within 25 ms.
- **PWM** measure and generate on any header, several at once. **Edge counting** on every free
  pin (discovery). **Pin levels**: read, drive 0/1, release.
- **Mesh mode** (`MESH JOIN`): the probe becomes a WCB_Client with id, MAC octets, password,
  channel and checksum sent at runtime, temporary by default so no WCB persists it as a peer. It
  sends, broadcasts, `sendRaw`s and reports what it receives. WCB_Client has no `end()`, so
  `MESH LEAVE` reboots the probe.
- USB is 921600 baud, one message per line. **The header comment of `probe_main.cpp` is the
  protocol reference**; `hil/probe.py` is its host side.

**Wiring.** GND to GND, the WCB port's TX to the probe header's RX, and the WCB's RX to the probe's
TX — or a straight-through cable, which discovery detects. Leave the header's 5V pin unconnected
when both boards have their own supply. Both sides are 3.3 V: V2.4 and V3.x have no level shifters
between the ESP32 and the S-headers. Header pad order on V2.4 and V3.2 is 5V, GND, TX, RX; the
HW 1.0 carrier is not in the repo. A listen-only **tap** wires only GND and the watched line.

---

## 4. The GUI (`tests/hil/gui.py`)

| Tab | What it does |
|---|---|
| **Devices** | *Find devices* identifies every ESP32 USB port and writes the COM numbers into `bench.json`, adding a second USB WCB as `wcb<N>` rather than skipping it. *Add a USB device* identifies one port you pick. Each device has its own port picker, *Check*, and (except `wcb1`) *Remove*; removing a WCB makes it mesh-only again. |
| **Wiring** | A diagram of every WCB port and probe header with the wires drawn (green verified, amber unverified, dashed tap, dotted grey planned). *What to connect* lists every WCB port with how to wire it and how many tests it unlocks. *Auto-detect wires*, or click a WCB port then a probe header, or use the by-hand form. Per-wire *Verify*, *Remove* and *Listen-only tap*. *A real device on a WCB port* records what else is wired to a port (a Maestro, a NaviCore port, …) in `port_devices`; the port turns blue in the diagram. |
| **Tests** | Every test grouped by area with its result, time and what it needs. Double-click runs one test; *Run selected* runs a test or a whole area; *Run everything*; *Run what failed*; *Stop after this test*. Every skip, a missing wire included, is written to `session.log` with its reason. |
| **Log** | Every serial line, live. |

All bench work runs on one worker thread, in order, so two actions never share a COM port. Above the tabs, an elapsed timer
shows how long the current job has run and, during a test run, how many tests are done and how long the current one has taken.

*Find devices* never resets a board and never sends free text. At 115200 it sends `?VERSION`: a WCB
and NaviCore both answer `Software Version:` (NaviCore's version starts with `v`; a WCB's board
number then comes from `?config`), and the SBUS controller treats `?` as its status key and
prints `[SBUS] Mode:`. Ports that stay silent are tried at 921600 with `HELLO`; probes are told
apart by MAC. An unprefixed line is never used, because a WCB would broadcast it to the mesh.

---

## 5. Writing a test

```python
@test("area.name", "One-line description", needs=["wcb1"])
def name(bench):
    l = link(bench, 1, "S3")                # the wire on W1 S3 (skips if not wired)
    t = marker()                            # unique HIL<nonce> per assertion
    m = l.mark()                            # binds the wire; only bytes after this count
    usb_wcb(bench).send(port_cmd(bench, 1, "S3", t))
    l.expect(t.encode() + b"\r", timeout=2, since=m)
```

A test passes by returning, fails by raising `AssertionError` (every `expect*` helper raises it
with the recent lines attached), and skips by raising `Skip`.

- **Injecting into a WCB port:** `l.send(bytes)`. **Emulating a device reply:**
  `l.rule(id, "AA0110??", b"\x70\x17")`.
- **End of a command's output.** `WCB.run(cmd)` sends the command and then `;S0,HILEND<random>`,
  and returns everything printed before that echo. USB commands drain one at a time from a single
  queue (`WCB.ino:8241-8258`), so a synchronous command's output always lands first. The echo is
  unique per call on purpose: a fixed sentinel (`?VERSION` → `End of Version`) can be satisfied by
  a stale line from an earlier command that arrives just after the mark, and from then on every
  read is one command behind.
- **Timing** is measured on the probe's clock (`l.time_of`) for both ends of an interval, so USB
  latency cancels.
- **Remote output.** `WCB.remote_terminal(n, self_id)` opens a `?RTERM` session so a remote board's
  console arrives as `[TERM:n]` lines; `WCB.remote_run()` waits for one.
- **Reboots.** `WCB.reboot()` waits for `Raw Serial Forwarding Task Created` (the last line of
  `setup()`, `WCB.ino:8200`) plus 1.5 s. Input in the first ~1 s after reset is flushed
  (`WCB.ino:7825-7826`) and the USB reader starts 500 ms later (`WCB.ino:7270`).
- **Config changes** go inside `config_guard(bench, <wcb>...)`.
- **Several wires at once.** `Watch(l1, l2, …)` marks them together; `watch.expect(l, data)`,
  `watch.got(l)`, `watch.silent(...)`.
- **Another board's console.** `Console(bench, n)` is that board's own USB port when it has one,
  and an `?RTERM` session through `wcb1` otherwise. `send()` returns the mark; `expect()` and
  `lines()` drop the `[TERM:n]` prefix. Prefer it to `remote_terminal`: RTERM splits lines at
  160 bytes and drops empty ones.
- **Pulses and line levels.** `l.pwm_in()`, `l.pwm_out(us)`, `l.pulses(mark)`, `l.line_level()`
  and `l.pwm_stop()` use the header pins directly, so they release the wire's serial channel; the
  next `listen()` or `send()` re-binds it.
- **Real devices on ports.** `bench.links.get()` hides a port that has a device and no `port_stimulus`, and the runner
  skips a test that names such a port. A test that picks a port at run time and puts traffic on it uses
  `bench.links.usable(wcb, port, send=...)`, which also refuses listen-only wires and every device port. A test that
  makes a port transmit with no wire or literal port reference for it (a mesh broadcast of `;S3`, say) declares
  `drives=[...]` in `@test`.
- **Probe reboots.** After `MESH LEAVE`, or any probe reset, the ESP32 ROM banner (115200) arrives at 921600 as
  garbage with no newline in front of `BOOT wcb_probe`, so never anchor that line with `^`.
- **Pinned bauds.** `listen(<baud>)` pins a wire. The runner releases pinned wires after every test, because
  `config_guard` only re-binds wires that follow the port's configured baud.
- **Long injections.** The probe's `TX` verb holds 1024 bytes (`probe_main.cpp:654`) and answers more with a
  misleading `ERR bad hex`; `send_chunked` splits at 1000.
- **A port left LOW** (after `;P`, or a PWM output port) misframes a whole following burst on the probe, not just its
  first byte. Send a throwaway line before checking bytes exactly.
- **Delimiter changes** apply to lines read after they run: a USB line is split when it is read, so wait for each
  reply before sending a line that depends on the new delimiter.
- **NaviCore holds a lone debug line.** One `[DISPATCH]`-style line can sit unsent on NaviCore's USB
  until more output follows it, so a test that sends a single mesh command and waits times out no matter
  how long it waits — the line then appears the moment the next command goes out. Send `#L12` on
  NaviCore's own console after the command (the flush the `?MAE` markers already need), then wait.
- **Opt-in tests** raise `Skip` unless `bench.json` `opt_in` names them.
- **`(should)` tests** assert the intended behaviour where code review found a probable firmware
  bug (§6), so they fail until the firmware changes. The docstring cites the lines.
- **The probe as a mesh client.** `with probe_in_mesh(bench, "probe2", 15) as probe:` releases that
  probe's wires and joins it as a temporary WCB_Client with the mesh settings from W1's config chain
  (`mesh_params`). Leaving reboots the probe; a test that must time the leave calls
  `probe.mesh_leave()` itself. Ids 1, 2, 6, 9, 19 and 20 are refused (the WCBs, W1's persisted learned
  peers, the old MgmtRelay, NaviCore), as is any id W1 lists as a learned peer: a temporary advert from a
  learned id downgrades it and persists the removal. `forget=True`, the default, sends `?WDP,FORGET` to
  every WCB after leaving, so no stale temporary peer waits on broadcast ACKs for 50 s. A WCB clears a
  client's duplicate ring only on a boot announce, which clients never send, so each test that sends
  commands has its own id (`MESH_IDS` in `s19_client_mesh.py`). Every usable id is now taken, so `s22`
  reuses one and first sends 17 no-op commands (`_burn_ring`): the ring holds 8 seqs per sender
  (`WCB.ino:591`), and 17 always push a stale ring out.
- **Changing the USB rate.** `dev.set_baud(n)` reconfigures the open port in place, leaving DTR/RTS alone. The
  OTA tests switch only after the board's `[OTA:BAUD,OK,<rate>]` plus a margin, and never use `WCB.run()`
  across a switch: its echo would cross it.

---

## 6. Firmware behaviour the harness is shaped around

Each of these is current firmware behaviour that a test, or a tool, walks into.

| Behaviour | Where | What the harness does |
|---|---|---|
| A config pull is ignored if the same requester's previous pull was answered under 1500 ms ago. The dedup is by requester and time, not session, so a genuine re-pull that soon gets no reply and simply times out. | `WCB.ino:3351-3359` | `WCB.mgmt_pull` spaces pulls to the same board 1.7 s apart. |
| A `?` command whose body ends in `?` prints help and does not run. | `WCB.ino:4932-4937` | `wcb.help_trap` checks it changes nothing. |
| `;W<n>,a^b` sends only `a` to board n; the sender splits the chain and runs `b` locally. | `WCB.ino:2368-2388` | Remote chains go through `?MGMT,FRAG`. |
| Maestro **query** verbs (`get*`) are served by a single slot, local port first; they do not fan out. **Move** verbs fan out to every slot with that id. | `WCB_Maestro.cpp:294-296`, `:413-418`; `:304-325` | `maestro.fanout_remote` uses `;M11`, not a query. |
| WcbMaestro range-checks against the Pololu protocol (channel and device 0–127, 14-bit values, accel 0–255), not against a particular Maestro's channel count. | WcbCmd `WcbMaestro.cpp:35-63` | `maestro.bad_verb` uses out-of-protocol values. |
| `;S<n>` appends CR (0x0D) only; WLED JSON is newline-terminated. | `WCB.ino:2000-2003`; WcbCmd `WcbWled.cpp` `emit()` | Byte assertions include the terminator. |
| **`?ETM,CHAR` phase 3 never transmits its broadcasts** on a mesh with inbound ETM traffic. `sendESPNowMessage` silently returns for target 0 while the global `lastReceivedViaESPNOW` is true; every inbound ETM command sets it on the WiFi task, and `processETMChar()` runs from `loop()` outside the command snapshot that would restore it. So neither `ETMLOAD` nor the every-3rd-message broadcasts leave the board, the phase measures an idle mesh, and it reports exactly 30 % loss with "Check RF environment". Verified: the target logged every phase-3 unicast and none of the broadcasts. Only the characterisation and its load generator broadcast from outside the command queue (`:1608`, `:1622`, `:1730`); PWM passthrough is a unicast to the mapped board (`WCB_PWM.cpp:739`), and user broadcasts and `;C` recalls run inside the drain snapshot (`:7003`, `:6718`). **Fixed 2026-09-15, and the last clause above was wrong:** the snapshot is taken at enqueue, so it captured a flag that a controller's 5 Hz JSON telemetry had just set (next row), and user broadcasts lost their mesh copy at random too. With telemetry no longer touching the flag, phase 3 misses 5 % instead of 30 % and `etm.char_loaded` passes. | `WCB.ino:2549`, `:4332`, `:1614-1622`, `:8223` | `etm.char_loaded` fails until this is fixed; `etm.char_unicast` covers phases 1–2 separately. |
| Under a `?ETM,CHKSM` mismatch the target ACKs **before** its CRC check and then discards, so `?STATS` shows the command delivered while nothing ran. More generally an ETM ACK never proves execution. | `WCB.ino:4269-4272`, `:4304-4311` | `etm.chksm_mismatch_silent_loss` records it; delivery is always asserted on a probe wire, never on ACK lines. |

### Probable firmware bugs pinned by `(should)` tests

Found by code review while writing the suites. None has run on the bench yet: a failing test
confirms the finding, a passing one means the review misread the code.

| Test | Probable bug | Where |
|---|---|---|
| `input.timer_keeps_source` | A `;T` line typed into a port loses its source, so its broadcast echoes back to that port and `?BCAST,IN,Sx,OFF` does not stop it. | `WCB.ino:7163-7166`, `command_timer.cpp:237` |
| `input.soft_rx_under_soft_tx` | Soft-serial TX critical sections mask the soft-RX edge interrupt on S3–S5; `;S3` also flushes unread S3 input. | `WCB.ino:2056-2068`, `:6506` |
| `input.bcast_reset_persists` | `?BCAST,RESET` clears input blocking in RAM only; `?config` reloads it from NVS. | `WCB_Storage.cpp:352-382`, `WCB.ino:2408-2409` |
| `input.checksum_c_chain` | A `?C` chain carrying `^?CHK` is enqueued whole instead of parsed, so only a mangled first command runs. | `WCB.ino:7087-7153` |
| `map.text_self_wcb` | A text mapping to the board's own `W<n>S<p>` goes out as ESP-NOW to itself and is dropped. | `WCB.ino:6911-6927` |
| `map.failed_update_keeps_mapping` | A failed mapping update leaves a zero-destination mapping that swallows the port until reboot. | `WCB_Storage.cpp:1719`, `:1833-1842` |
| `map.bare_raw_flag` | `?MAP,SERIAL,S2,R` creates a raw mapping with no outputs, and the port goes deaf. | `WCB.ino:5032-5034`, `:7295` |
| `map.raw_remote_bursts_no_loss` | A raw chunk whose `esp_now_send` fails is skipped, not retried. | `WCB.ino:2855-2866` |
| `map.raw_remote_s0_refused` | Raw `W<n>S0` is accepted, but the receiver rejects port 0. | `WCB_Storage.cpp:1797-1800`, `WCB.ino:4589` |
| `map.clear_one_restores_flags` | Clearing one mapping forces broadcast IN and OUT on, whatever they were. | `WCB_Storage.cpp:1970-1982` |
| `map.clear_all_restores_output` | `?MAP,SERIAL,CLEAR,ALL` never restores broadcast output. | `WCB_Storage.cpp:1996-2016` |
| `map.timer_line_obeys_mapping` | A `;T` line skips its port's mapping and broadcasts mesh-wide. | `WCB.ino:7163-7166`, `command_timer.cpp:237` |
| `map.remote_leading_comma` | A remote text destination loses the line's leading comma. | `WCB.ino:6922`, `:6487-6492` |
| `pwm.p_refused_on_maestro_port` | `;P` is not guarded on a Maestro port and silences that UART until reboot. | `WCB.ino:6837-6843` |
| `pwm.clear_out_restores_flags` | Clearing a PWM output port forces its broadcast flags on. | `WCB_PWM.cpp:876-881` |
| `pwm.clear_out_keeps_queue` | `?MAP,PWM,CLEAR,OUT` and `?PX` restart inline, even when nothing changed, dropping queued commands (rule 11). | `WCB.ino:5045-5053`, `:5848-5855` |
| `pwm.invalid_remap_keeps_mapping` | An invalid re-map zeroes the live mapping's outputs before validating. | `WCB_PWM.cpp:226-228` |
| `pwm.input_only_advertises_cap` | `activePWMCount` is never assigned, so a board with only PWM input mappings does not advertise the cap. | `WCB_WDP.cpp:112`, `WCB_PWM.cpp:24` |
| `pwm.remote_refusal_logged_once` | WDP auto-config re-logs a refused PWM port on every advert. | `WCB_WDP.cpp:746-748`, `WCB_PWM.cpp:72-74` |
| `pwm.clear_all_reaches_remote` | `?MAP,PWM,CLEAR,ALL` sends its remote clears non-ETM, so ETM boards drop them. | `WCB_PWM.cpp:9` |
| `pwm.remove_clears_all_remote_outputs` | Removing a mapping with two outputs on one board clears only the first; the receiver restarts inline. | `WCB_PWM.cpp:356-369` |
| `pwm.self_wcb_output` | A PWM output addressed to the board's own `W<n>S<p>` never drives its pin. | `WCB_PWM.cpp:279-291`, `:616-629` |
| `hcr.setemotion_100_not_silent` | `;H,SETEMOTION` accepts 100, and the library drops it silently. | `WCB_HCR.cpp:311`, `hcr.cpp:432` |
| `hcr.input_validation_gaps` | `;H,VOL,X,40` mutes every channel; `;H,FN` values above 255 alias; FN 14 skips the track range check. | `WCB_HCR.cpp:264-287`, `:370-386` |
| `hcr.stop_cancels_fade` | `;H,STOP` does not cancel a fade in flight. | `WCB_HCR.cpp:306` |
| `dfp.device_verb` | `;D,DEVICE,n` is rejected: the leading `D` is stripped twice. WcbCmd is shared with NaviCore. | `WCB_DFP.cpp:72-74`, WcbCmd `WcbDfPlayer.cpp:54` |
| `dfp.partial_frame_resync` | A truncated DFPlayer frame swallows the good frame after it. | WcbCmd `WcbDfPlayer.cpp:177-179` |
| `conflict.dfp_port_unguarded` | The HCR, MP3, WLED and PWM port guards do not check DFPlayer ports. | `WCB_HCR.cpp:649-652`, `WCB_MP3.cpp:314-317`, `WCB_WLED.cpp:326-329`, `WCB_PWM.cpp:72` |
| `var.clear_all_name_collision` | A variable named `all` cannot be cleared alone: `?VAR,CLEAR,all` wipes every variable. | `WCB_Variables.cpp:327-328` |
| `if.malformed_or_passes` | A malformed condition OR'd with a true one runs the gated command. | `WCB_Variables.cpp:456-459` |
| `if.timer_split_trap` | An IF-gated `?SEQ,SAVE` whose value holds `;T` is timer-split: the value is truncated and its tail runs. | `WCB.ino:7155-7163` |
| `seq.cycle_guard_case` | The cycle guard compares keys case-insensitively, so a missing different-case key reads as recursion. | `WCB.ino:6736` |
| `legacy.cs_caret_lfi_roundtrip` | `?CS` stores a value containing `^?`, which `?backup` cannot restore. | `WCB.ino:2295-2301`, `:2330-2335` |
| `etm.unvalidated_setters` | A bare `?ETM,TIMEOUT`, `?ETM,BOOT` or `?ETM,DELAY` persists 0, and a negative timeout is accepted. | `WCB.ino:5209-5212`, `:5243-5254` |
| `etm.legacy_miss0_rejected` | Legacy `?ETMMISS0` skips the 1–100 check: every peer goes offline and ensured sends become fire-once. | `WCB.ino:5912-5915` |
| `etm.dedup_ring_covers_pending` | The receiver's duplicate ring (8) is smaller than the sender's pending table (10), so 9–10 ensured commands with lost ACKs re-execute on every retry. | `WCB.ino:541`, `:591` |
| `etm.acked_not_executed_reboot` | `?reboot` restarts inline, so a command the board has already ACKed behind it is lost (rule 11). | `WCB.ino:825-829` |
| `stats.delivered_not_above_attempts` | `?STATS` counts ACKs nobody expected (the controller ACKs broadcasts), so Delivered can exceed Transmission Attempts. | `WCB.ino:1301-1303` |
| `chars.legacy_lf_guard` | Legacy `?LF<c>` skips the `?FUNCCHAR` collision guards and accepts the command character. | `WCB.ino:5976-5984` |
| `chars.cmdchar_lfi_guard` | `?CMDCHAR` and `?CC` accept the function identifier, which silently kills the `;` family. | `WCB.ino:5635-5644`, `:5986-5994` |
| `client_mesh.json_broadcast_once` | Every WCB processes a client's ensured JSON broadcast twice: the WCB never ACKs a JSON broadcast, while `WCB_Client::broadcast()` defaults to ensured and retries as a unicast. NaviCore's `WCB_SEND` target 0 hits this. | `WCB.ino:4257-4268`; WCBClient `WCB_Client.h:621`, `WCB_Client.cpp:2208-2218` |
| `client_mesh.rejoin_seq_reuse` | A client that re-joins (NaviCore or a MgmtRelay after a reboot, or this probe) has its first commands ACKed and silently dropped: it restarts its sequence numbers and never sends the boot announce that clears a WCB's duplicate ring. | `WCB.ino:4219-4228`; WCBClient `WCB_Client.cpp:77`, `:2183` |
| `ota.help_matches_parser` | The `?OTA` help omits the `<session>` field the relay parser requires, so following it shifts every argument; the unknown-subcommand hint omits BAUD. | `WCB_Help.cpp:713-715`, `WCB_OTA.cpp:324` |
| `ota.local_baud_rejected_rebegin_restores` | A rejected re-BEGIN after a baud bump tears the session down without restoring the rate, stranding USB at 921600 until ABORT or a reboot. | `WCB_OTA.cpp:106`, `:109-131` |
| `ota.relay_teardown_frame_err` | The relay DATA frame that tears a session down is ACKed OK with offset 0 (the status is captured before the write); only later frames get ERR. NaviCore orders it the same way. | `WCB_OTA.cpp:402-415`; NaviCore `navicore_ota.h:367-380` |
| `navicore.maestro_skip_not_logged_as_dispatch` | NaviCore skips a mesh `;M` channel above 31 (the WCB's WcbMaestro accepts 0–127 and writes it) and then logs the skipped write as dispatched. | NaviCore `NaviCore.ino:925-929`, `:5065-5083` |
| `navicore.wcb_send_ack_reflects_refusal` | USB `WCB_SEND` ignores the library's return value and answers ok:true for a broadcast it refused. | NaviCore `NaviCore.ino:4035-4040`; WCBClient `WCB_Client.cpp:388-393` |
| `navicore.test_action_bad_target_not_ok` | `TEST_ACTION` answers ok:true for a `wcb_unicast` whose board id is outside 1–20, although nothing is sent. | NaviCore `NaviCore.ino:2043-2052`, `:2144-2150` |
| `navicore.l1_names_board_profile` | `#L1` always prints "WCB HW 3.2", whatever the configured board profile is. | NaviCore `NaviCore.ino:3599` |
| `maestro.get_moving_state_normalised` | `getMovingState` stores the raw reply byte, while its comments and WcbCmd's decoder (which NaviCore uses) give 0/1. | `WCB_Maestro.cpp:455`; WcbCmd `WcbMaestro.cpp` `decodeReply` |
| `maestro.get_var_name_from_parsed_channel` | `getPosition` names its variable from the raw channel text: `,05` stores `m1pos05`, and `,0,99` sends a valid frame but its store fails silently. | `WCB_Maestro.cpp:389`, `:457` |
| `maestro.get_fallback_unicast_within_wcbq` | A get for an unconfigured id within WCBQ is broadcast to every board, while a subroutine or verb for the same id is unicast to WCB<id>. | `WCB_Maestro.cpp:223-226`, `:368`, `:432` |
| `maestro.legacy_clear_suffix_keeps_slots` | The legacy `?MAESTRO_CLEAR` handler prefix-matches, so a typo such as `?MAESTRO_CLEAR,M5` wipes every slot, remote proxies included. | `WCB.ino:5796-5797` |
| `maestro.local_baud_zero_rejected` | `?MAESTRO` accepts baud 0: the port refuses it, but the local slot is saved, printed and backed up at 0 baud. | `WCB_Maestro.cpp:610`, `:677-685`; `WCB_Storage.cpp:155-160` |
| `kyber.config_negatives` | A refused `?KYBER,LOCAL,S6` or `S0` has already switched targeting on in RAM. | `WCB_Storage.cpp:1154`, `:1160-1162` |
| `kyber.local_rejected_port_keeps_forwarding` | On a Kyber_Local board in broadcast mode that leak leaves an empty target table, and every Kyber byte is dropped until reboot. | `WCB_Storage.cpp:1154`; `WCB.ino:4738-4772` |
| `kyber.local_port_s3_frees_s2` | With the Kyber on S3–S5, S2 has no reader and still misses broadcasts, and `?KYBER` forces 115200 onto a software-serial port that `?MAESTRO` would refuse. | `WCB.ino:7289-7292`, `:4734-4736`, `:6943`; `WCB_Maestro.cpp:643` |
| `kyber.clear_warns_reboot_single_reader` | `?KYBER,CLEAR` prints no reboot notice, and until a reboot S1 is read by both the serial parser and the still-running Kyber task. | `WCB_Storage.cpp:1237-1277`; `WCB.ino:8177-8184`, `:7298-7302` |
| `kyber.maestro_s2_single_reader` | A local Maestro on S2 of a Maestro_Remote board, or on S3–S5, has its bytes split between the serial parser and the Kyber bridge: `processIncomingSerial` never skips a Maestro port. | `WCB.ino:7293-7297`, `:7008-7030`, `:4828-4845` |
| `kyber.target_id9_rejected` | `?KYBER` stores Maestro id 9 as a slot; `;M9…` then goes out as device 9, and no clear form removes it. | `WCB_Storage.cpp:1313`; `WCB_Maestro.cpp:108-158`, `:817`, `:844` |

### Firmware bugs found by the first full run (2026-09-15)

Not predicted by a `(should)` test; each was traced from a run's `session.log` to the code. All six are
fixed in the working tree (WCBClient 1.17.1 and the WCB sources, 2026-09-15). The first four were confirmed
by the 18:10 run on the reflashed boards — 39 tests that had failed in the morning passed. The fifth was
found by the re-run between them and confirmed after it: `input.softserial_core0_contention` goes from 0 of
10 raw blocks exact to 9 or 10 of 10. The last block is the residual below, not a regression.

| Bug | Where | Seen in |
|---|---|---|
| A WCB_Client keeps each sender's duplicate window across that WCB's reboot. A client that stays up (NaviCore) then silently drops the rebooted WCB's commands whose restarted sequence numbers fall in the old window, so some `;W20` commands work and others vanish. The client has no handler for the WCB boot announce. | WCBClient `WCB_Client.cpp:864-885` (`_cmdSeqDup`); boot announce is packet type 11 (`WCB.ino:235`) | `navicore.cli_over_mesh`, `navicore.trigger`; very likely `navicore.set_mode` (NaviCore took the first mesh `SET_MODE` and ignored the next three, so it was left in mode 2), and after it `navicore.trigger_bounds_usb_vs_mesh`, `sbus.mode_sets_trigger_mode` and `peers.controller_off_on` |
| Every `;S1`/`;S2` write is followed by `flush()`, which on the pinned core busy-waits under the UART mutex and starves the serial reader task on the same core, so lines arriving on S3 are lost while S2 output drains. Removing the flush fixed 19200 but not 38400: `Serial1`/`Serial2` also had no TX buffer (core default `_txBufferSize(0)`, `HardwareSerial.cpp:123`), and `uartWriteBuf` holds the UART lock while `uart_write_bytes` waits for FIFO space (`esp32-hal-uart.c:1165-1167`), so a burst that filled the 128-byte FIFO still stalled the reader. | `WCB.ino:6505-6506`; the `Serial1`/`Serial2` begin block in `setup()` | `input.soft_rx_baud_sweep` |
| A sequence key longer than 15 characters is refused only when saved. Get, recall and clear pass it to NVS, which compares 15 characters, so the 16-character key returns the 15-character key's value. | save check `WCB_Storage.cpp:652-661`; unchecked get/recall paths in `WCB.ino` | `seq.key_len_15` |
| Remote PWM configuration and clears are skipped silently during the first 5 s of uptime, while local state and NVS still change and the command prints success. | `WCB_PWM.cpp:51-53`, `:326`, `:483` | `pwm.clear_all_reaches_remote` (the test now waits past it) |
| Any inbound ESP-NOW packet sets the global `lastReceivedViaESPNOW`, **including JSON telemetry** that the relay branch consumes and never runs as a command. A controller on the mesh broadcasts `rc_ch` at 5 Hz, so the flag is true almost continuously on every board; a locally-typed broadcast snapshots it when it is enqueued (`WCB.ino:2204`) and `sendESPNowMessage()`'s loop-prevention gate then drops the mesh copy silently. The board's own ports still print the line, so it looks like "the other board randomly missed it". Both receive paths now leave the flags alone for a `{` payload. | `WCB.ino:4409` (ETM path), `:4700` (plain path); gate at `:2618` | `input.bcast_fanout`, `input.bcast_too_long_local_only`, `input.partial_poisons_next` — each passes alone and fails inside a full run; the same gate causes `etm.char_loaded`'s phase-3 loss |
| The ESP-NOW receive callback bit-bangs S3-S5 itself (Maestro and Kyber passthrough, raw-serial mapping). A remote board's raw mapping can target any port, which `softSerialLoopTaskOnly` cannot see, so core 0 waits on EspSoftwareSerial's static TX mux while core 1 holds it — and `lazyDelay()` re-anchors the bit clock *before* `disableInterrupts()` waits, so the waiting port's next start bit comes out short by the wait and **both** ports mis-frame. The callback now writes no soft port: `meshSerialWrite()` queues the chunk and `MeshSerialOutTask` (core 1, beside the Kyber and raw-forwarding tasks) writes it, which also takes a ~200 ms bit-bang off the WiFi task. | the four write sites in `espNowReceiveCallback`; `softSerialLoopTaskOnly` / `applySoftSerialIntTx`; `SoftwareSerial.cpp:265-292` | `input.softserial_core0_contention` (0 of 10 raw blocks exact, and the `;S4` lines written alongside them corrupted too) |
| **Residual, not fixed.** Two core-1 writers on different soft ports still cost about one byte boundary per run: the scheduler can preempt one between `preciseDelay()` re-anchoring the bit clock and `writePeriod()` driving the pin, and that run comes out short — a swallowed stop bit mis-frames the byte. Measured at ~1 bad boundary in 29 000 protected bytes on an idle-ish port, and ~1 per run when `loop()` writes a 68 ms `;S4` line into the middle of a mesh chunk. Closing it needs one lock held across *every* soft-serial write (`writeSerialString`, `MeshSerialOutTask`, `RawSerialForwardingTask`, the Kyber tasks, and the HCR/WLED/MP3 libraries writing their own streams) — or the durable fixes CLAUDE.md rule 13 already names: a hardware UART, or less traffic on the wire. | `SoftwareSerial.cpp:285-334`; `WCB.ino` soft-serial writers | `input.softserial_core0_contention` at 9 of 10; one frame of 300 in `hcr.softserial_integrity_mesh_load` |

Unconfirmed: in `seq.cycle_guard` W1 expanded a 9th nested recall without the depth-limit line (`WCB.ino:6729-6749`); re-run it before treating it as a bug.

### Documentation that disagrees with the code

| The doc says | The code does | Test |
|---|---|---|
| An empty sequence inventory hashes to `811C9DC5` (`docs/SEQUENCE_INVENTORY.md:137`, `:249`; `docs/WDP_DESIGN.md:109`; comments at `WCB_Storage.h:164`, `WCB_WDP.h:89`, `WCB_WDP.cpp:1176`; `tests/wdp_wire_test.cpp:69`; WCBClient `WCB_Client.h:404`, `README.md:536`, `examples/SequenceInventory`). | `7A0B824E`: the 0xFF separator is applied even to an empty key list (`WCB_Storage.cpp:792-798`). | `seq.clear_all_empty_hash` (opt-in) |
| Setting variable #101 is an error (`docs/VARIABLES_DESIGN.md:16`). | The lowest volatile slot is recycled; only a table of 100 persistent variables errors (`WCB_Variables.cpp:140-145`). | `var.cap_volatile_evicts` |
| Consecutive IFs are not an AND (`docs/VARIABLES_DESIGN.md:165-166`). | They AND (`WCB_Variables.cpp:391-409`; the same doc at `:110-112`). | `if.gate_scope` |
| An IF is evaluated before any command of its chain runs (`docs/VARIABLES_DESIGN.md:116-118`). | Usually; `loop()` shares core 1 with the serial task and can apply an earlier `;V` first (`WCB.ino:8174`). | `if.same_chain_stale` |
| A broadcast `;V` sets the variable on every board (`docs/VARIABLES_DESIGN.md:129`). | Only a mesh client can originate that broadcast; on a WCB console a `;` verb runs locally. | `var.per_board` |
| App rollback is not enabled (`docs/WCB_OTA_TECHNICAL.md:271`). | The pinned core enables it (its sdkconfig, `:289`, `:3354`), but `initArduino` marks the new app valid before `setup()`, so rollback only rescues an image that dies earlier (`esp32-hal-misc.c:277-292`). | `ota.local_full_same_image_wcb1` |
| The relay target prints its ACK inline in the receive callback (`docs/WCB_OTA_TECHNICAL.md:224`). | It is deferred through the relay queue (`WCB_OTA.cpp:450-457`, `WCB.ino:409-413`). | `ota.relay_crc_ok_and_nocrc_no_session` |
| A write that tears a relay session down is ACKed ERR (`docs/WCB_OTA_TECHNICAL.md:176`, `:209`, `:291`). | It is ACKed OK; only later frames get ERR. | `ota.relay_teardown_frame_err` |
| A chip-family mismatch is rejected at BEGIN (`docs/WCB_OTA_TECHNICAL.md:288`). | Only the host-declared family is compared (`WCB_OTA.cpp:108-113`); a wrong image declared with the right family is stopped only by IDF verify at END. | `ota.local_wrong_chip_image` |
| The baud bump comes after BEGIN so the 30 s idle timeout owns the restore (`docs/WCB_OTA_TECHNICAL.md:110-111`). | A rejected re-BEGIN leaves no session, so the timeout never runs. | `ota.local_baud_rejected_rebegin_restores` |
| Maestro ids 9 and 0 are routing targets, never stored as a slot (`CLAUDE.md` rule 5; `WCB_Maestro.h:15-16`). | `?KYBER` accepts target id 9 and stores it as a slot (`WCB_Storage.cpp:1313`). | `kyber.target_id9_rejected` |
| `maestroAutoAddRemote` is first-host-wins (`WCB_Maestro.h:49`). | It adds a proxy per host, as `CLAUDE.md` rule 5 says (`WCB_Maestro.cpp:723-757`). | — |
| Query verbs write only the request frame, and the reply rides the Kyber return path (`WCB_Maestro.cpp:250-251`). | Gets are intercepted and their reply read synchronously on the port (`WCB_Maestro.cpp:287-297`, `:437-448`). | `maestro.get_local_replies` |
| Maestro script triggers are sent without ETM (`WCB.ino:4136`). | Every `;M` send is ETM: unicast, broadcast and fallback alike (`WCB_Maestro.cpp:133-148`, `:175-178`). | `maestro.m0_broadcast` |
| The Kyber task drains S1 and S2 whatever `kyberLocalPort` is (`WCB.ino:7290-7292`). | It reads only `kyberLocalPort` and the Maestro ports (`WCB.ino:4734-4736`, `:4813-4820`), so with the Kyber on S3–S5, S2 has no reader. | `kyber.local_port_s3_frees_s2` |

---

## 7. Coverage

| Group | Checks |
|---|---|
| `probe.*`, `link.*` | Probe firmware answers; every discovered wire carries its port's output byte-exact, locally and over the mesh. |
| `wcb.*` | Backup and remote-pull CRCs, firmware match across boards, unknown-command and help-trap parsing. |
| `serial.*` | `;S` comma form, `;S0` to USB, `^` chains in order, bad port, `;T` delays, summed delays, `?STOP`. |
| `mesh.*` | Unicast delivery and ETM ACK accounting, alias routing, unreachable target, chain split, `?MGMT,FRAG`, `?RTERM`. |
| `persist.*` | Label (local, remote, across reboot) and live baud change, each restore-verified. |
| `input.*` | Bytes injected into WCB ports: CR/LF terminators, partial lines and per-port buffers, long lines and queue overflow, `?BCAST` IN/OUT filtering and fan-out, Maestro reply routing, soft-serial RX under load and across bauds, framing variants, `@WDP` device announces. |
| `map.*` | `?MAP,SERIAL` text and raw mappings: local, multi-destination, remote, S0 and soft ports, validation, persistence across reboot, the CLEAR paths, legacy `?SM`. |
| `pwm.*` | `;P` pulse widths and guards, PWM output ports, the deferred mapping reboot, local and mesh passthrough measured on the probe, WDP PWMTARGET auto-config and self-heal. |
| `hcr.*`, `mp3.*`, `dfp.*`, `mp3dfp.*`, `route.*`, `conflict.*`, `backup.*`, `port.*`, `wdp.hcr_*` | Exact device bytes for every `;H`, `;A` and `;D` verb with no device attached, emulated device replies (HCR status, MP3/DFPlayer finish and error callbacks), HCR poll timing and fades, config validation, WDP route learning and how to undo it, port moves, backup round trip. |
| `var.*`, `if.*` | Variable verbs, limits, names, persistence and backup position, `;M!`, per-board scope; every IF operator, AND/OR, gating scope, timers, rejections, IFs run on W2 and inside stored sequences. |
| `seq.*`, `legacy.*`, `inv.*` | `?SEQ` formats, the 15-character key limit, NAMES order and its FNV-1a hash, recall forms, comment stripping, the save boundary, the cycle guard, `,L` and nested fan-out; legacy `?CS`/`?CE` and `?CHK`; the `?MGMT,SEQ`/`SEQGET` relay, its dedup, multi-chunk values and TOOBIG. |
| `maestro.*` | Exact Pololu frames for every verb, invalid-argument rejection, target 9, fan-out to a remote host, and a real servo `setTarget` → `getPosition` round trip over the mesh; LIST in every spelling; get-query decoding, timeouts, short and extra replies, and rejections; the `;M0` broadcast; fallback broadcast, and fallback unicast to the probe as client 5; stale proxies; a client's get answered back to it, and the `;M!` name guard; `CLEAR,ALL` with legacy routing and an ordered rebuild; the slot map and 9-slot cap; every CLEAR form and add-path refusal. |
| `kyber.*` | The Maestro_Remote bridge both ways, with a real Maestro reply; `?KYBER,LOCAL` on S2 across the pre-reboot deaf window, targeted and broadcast forwarding, explicit targets and the Maestro-to-Kyber return; the Kyber on S3; `?KYBER,CLEAR` without a reboot; a local Maestro on S2; target id 9; parse errors; WDP reverting a board to Maestro_Remote. |
| `wled.*` | Exact JSON for every `;L` verb routed to the WLED host, unknown-verb silence. |
| `navicore.*` | JSON ping, mesh membership and WDP table, `;W20` remote CLI, `WCB_SEND` to both WCBs, mesh `TRIGGER`; its Maestro inventory against W1's proxies, `?MAE` markers, mesh setTarget with read-back, mesh queries answered into W1's variables, inbound `;M` guards and 0/9 fan-out; serial routing, plain broadcasts both ways, `WCB_SEND` edges and fragments, JSON bridged through W1, declined mesh JSON, exact mesh stats, safe `FORGET_PEER`, `SET_MODE`, `TEST_ACTION`, `TRIGGER` bounds and dispatch traces, record/replay markers (opt-in clip playback), `#L` diagnostics, and a temporary probe it never learns. |
| `sbus.*` | Controller ping; a virtual stick moves NaviCore's decoded channel to the exact calibrated value and back to centre; the safe channels NaviCore binds nothing to; matrix button single, double, triple and held taps with rc_trig timing; tap mode after `SET_MODE`; exact switch, slider and trim values; opt-in signal loss by resetting the controller. |
| `etm.*`, `stats.*`, `misc.*` | Reboot announce; `?ETM,CHAR` phases; ETM settings (set, persist, validate, legacy spellings); retries and failure with ACKs lost, the duplicate-ring and pending-table limits, offline detection and retry cancel, CHKSM mismatch and the 187-character limit, ETM off, per-board broadcast ACKs, the receive CRC gate from a probe; `?STATS,RPT` locally and over the mesh; `?IDENTIFY`, `?LED`. |
| `peers.*`, `wdp.*`, `alias.*`, `chars.*` | Live peer counts, live `?WCBQ`, controller off/on and auto-enable; WDP DUMP fields against config, LIST/DETAIL, POLL, OFF/ON, AUTOJOIN, ADD/FORGET, a temporary probe's join and eviction, device announces; alias sanitising and propagation; changing and restoring the delimiter, function identifier and command character. |
| `client_mesh.*`, and the mesh-mode `input.*`, `map.*`, `var.*` and `seq.*` tests in `s19` | The probe as a temporary WCB_Client: adoption and its WDP row, unicast and broadcast delivery and ACKs, sendRaw (including onto the real Maestro line), fragments and the 187-character limit, `?WHOAMI`, WCB broadcasts reaching a client, checksum and password mismatches, re-joining, the stale-peer window; client broadcasts into ports, the Maestro return path, soft-serial TX under mesh load and core-0 contention, raw chunk bounds, client-set variables, sequence fan-out seen from the mesh. |
| `ota.*` | `?OTALOCAL` status, parsing and help, session-less and usage errors, BEGIN guards; relay-side rejects and CRC handling, relay BEGIN rejects on W2, the NaviCore brick guard. Opt-in: local sessions (cursor, supersede, verify failures, bad magic, overrun, base64 errors, idle timeouts), the USB baud bump, relay sessions on W2 (cursor, teardown, END, keep-alive), a cross-transport abort, and full-image transfers (a corrupt image, a same-image re-flash of W1 and of W2 in two passes each, a wrong-chip image). |

`input`, `map`, `pwm`, the HCR/MP3/DFPlayer groups, `var`/`if`, `seq`/`legacy`/`inv`, `client_mesh`, `ota`, the `s22` Maestro tests, `kyber`, every
ETM/WDP group test except `etm.remote_reboot`, `etm.w1_reboot_sees_peers` and `etm.char_*`, and the
NaviCore and SBUS tests beyond ping/online/WDP/stick were written from code-reviewed specs and have not
yet run on the bench.

**Coverage audit (2026-09-15).** Every user-facing function in the firmware — each `?` command and sub-command, legacy alias,
`;` verb form, mesh path, and boot or periodic behaviour — was listed from the code and matched to the tests that assert it, and
each area's mapping was re-checked by a second reviewer. Of 1,064 functions, 484 are tested, 198 partly (some forms untested),
343 untested but testable on this bench, and 39 not testable here (dead code, or what the bench lacks: a real Kyber brain, a
third WCB, an ESP32-S3 WCB, a light sensor on the status LEDs, fault injection). The largest gaps:

- **WiFi and the WebSocket endpoint** (`?WIFI`, `WCB_WS.cpp`): 58 functions, no tests. Needs the PC's WiFi adapter.
- **Legacy spellings** (`?ETMOFF`, `?PCLEAR`, `?DON`, `?WCB_ERASE`, `?KYBER_LOCAL`, …): mostly untested; the canonical forms are.
- **Identity and radio settings**, which are risky to change on a live bench: `?WCB`, `?WCBCH`, `?MAC`, `?EPASS`, `?HW`, `?ERASE,NVS`.
- **`?WLED` configuration** (local and remote add, LIST, STATUS, CLEAR, validation) — only `;L` output is tested — and `?TRACK`.
- **Timer corners** (`?STOP` forms, the 30-minute cap, a `;T` chain received over the mesh), `?LABEL` and `?BAUD` validation,
  the `?MGMT` and `?RTERM` error branches, and the RC telemetry relay to USB.
- **Boot and persistence**: each subsystem's NVS restore at boot, the boot banner blocks, factory defaults.

The Wizard UI is not covered. In Kyber, only what the bench cannot reach is untested: a real Kyber brain, and the WDP
Kyber-local ruleset, since NaviCore's controller rule is evaluated first (`WCB_WDP.cpp:475-480`). Most tested functions have
not yet passed on the bench: only the first 53 tests have run.

---

## Revision log

| Date | Commit | Change |
|---|---|---|
| 2026-09-15 | _(pending)_ | **Bench run of the fixes, and two more firmware bugs.** Final full run, every board on tonight's images: 285 pass / 62 fail / 51 skip in 51:22, from 249 / 96 / 53 in the morning. 53 of the 62 failures are `(should)` tests pinning known bugs. Of the nine others, four are §6 entries (`input.soft_rx_baud_sweep`, `map.baud_on_raw_mapped_port`, `input.softserial_core0_contention` at 9 of 10, and one rule-13 mis-frame in `input.bcast_fanout`), one is a test bug (`etm.offline_detection_timing`), one is NaviCore telemetry landing in a console the test reads (`maestro.list_and_legacy_spellings`), and three are NaviCore's USB console stalling for seconds at a time (`navicore.cli_codes`, `sbus.slider_exact`, `sbus.btn_double_triple_tap`) — a NaviCore-side issue, worth a ~1 Hz `Serial.flush()` tick in its `loop()`. New firmware findings, both fixed in the working tree: the ESP-NOW callback bit-banging S3-S5 (those chunks are now queued for `MeshSerialOutTask` on core 1 — `input.softserial_core0_contention` went from 0 of 10 blocks exact to 9-10 of 10, the rest being the documented residual), and JSON telemetry clobbering `lastReceivedViaESPNOW`, which cost locally-typed broadcasts their mesh copy at random. Harness fixes: a probe's channel table is cleared in place rather than popped (a bind in flight registered into an orphan, so two wires shared one channel and a test read another port's traffic — the cause of `hcr` and `bcast` wire failures that passed when run alone); `wdp.autojoin_off_ignores_probe` waits for id 15's 22.5 s advert instead of 17 s; `navicore.maestro_mesh_rejects` flushes NaviCore's console with `#L12` per case instead of sleeping on it. `input.soft_rx_baud_sweep` stays red: bit-banged RX under mesh load is at its limit, and the counts in its message are lines RECEIVED, not lost — worth rewording. |
| 2026-09-15 | _(pending)_ | **Fixes for the four new firmware bugs** (§6), not yet run on the bench: WCBClient 1.17.1 forgets a WCB's anti-replay window on its boot announce; `;S` no longer calls `flush()` (a live `?BAUD` on S1/S2 flushes instead); every sequence-key path refuses keys over 15 characters (`SEQ_KEY_MAX_LEN`); remote PWM configuration is gated on ESP-NOW having started rather than on 5 s of uptime, and says so when it cannot send. Review follow-ups: a boot announce clears a sender's duplicate window once per boot, in the WCB firmware and in WCB_Client, so a command retried between its three announces cannot run twice; erasing by an over-long key still drops it from the key list; `ONERR` callback keys are capped at 15 characters; S1 and S2 get a 1024-byte TX buffer before `begin()`, the second half of the S3 input loss (the first re-run after removing the flush still lost 3 of 20 lines at 38400). |
| 2026-09-15 | _(pending)_ | **First full run and its triage.** 398 tests in 50:48: 249 pass, 96 fail, 53 skip. Every failure was traced to its cause. 47 of the 53 `(should)` failures confirmed their predicted bug; the other six failed for harness or test reasons below. Harness bugs: `probe.mesh_leave` anchored `BOOT wcb_probe` with `^`, which never matches after the ROM banner, so all 21 probe-mesh tests failed on leaving (and a failed leave skipped the `?WDP,FORGET` cleanup, which skipped three more); a baud pinned by `map.raw_remote_115200_exact` outlived it and garbled every later injection into W1 S2 (8 failures); `send_chunked` sent 1500 bytes into the probe's 1024-byte `TX` buffer. Test bugs: a LOW-parked port misframes a whole burst (2 PWM tests), `pwm.passthrough_local` marked after the pulses, `pwm.clear_all_reaches_remote` cleared inside the 5 s send hold-off, `conflict.dfp_port_unguarded` missed that `?HCR,CLEAR` re-enables the poll, `etm.offline_detection_timing` matched NaviCore's WCB20 heartbeats as WCB2, and `chars.delim_comma_restore_path` sent lines back to back across a delimiter change. All fixed. New firmware bugs are listed in §6. |
| 2026-09-15 | _(pending)_ | **GUI: elapsed timer, and devices on ports.** An elapsed timer above the tabs shows the job's time, tests done of the total, and the current test's time; `report.md` records the run time. `bench.json` `port_devices` records real devices wired to WCB ports, set from the Wiring tab: such a port is never made to transmit without a `port_stimulus`, a probe wire on it is forced listen-only, auto-detect, *Verify* and `link.out` skip it, and a test that needs a probe there skips naming the device. Missing-wire skips are now written to `session.log`; they were shown only in the GUI, which hid why 46 tests skipped once W1 S5 went to NaviCore S1 and the W2 S1 tap was not found. A review then closed the gaps in that rule: a listen-only wire on such a port still counted as a wire, so its tests ran; `links.json` loaded an old two-way wire unchanged; tests that pick a port at run time, or drive one without a wire, bypassed the check; and auto-detect deleted a wire added there by hand. `links.get()` now hides the port, `load()` forces the tap, `usable()` and `drives` cover the rest, and auto-detect keeps such wires. The Wiring tab's right pane scrolls (the device form was off-screen at Windows display scaling), and queuing a run no longer cancels a pending *Stop after this test*. Coverage audit added to §7. |
| 2026-09-15 | _(pending)_ | **Suites s12–s22** (349 tests): serial input, serial mapping, PWM, HCR/MP3/DFPlayer, variables and IF, sequences and the inventory relay, ETM/peers/WDP/command characters, the probe as a mesh client, OTA, NaviCore and the SBUS controller, Maestro routing and the Kyber bridge. Written from code-reviewed specs; not yet run on the bench. 60 `(should)` tests pin probable firmware bugs, and fifteen doc/code disagreements are listed (§6). `CLAUDE.md` rule 4's CHKSM command limit corrected from 188 to 187 (`ETM_MAX_CMD_WITH_CRC`, `WCB.ino:225`). **Discovery fix:** it swept only `mesh_only_wcbs`, which emptied when WCB2 got its own USB cable, so WCB2's wires were never found; it now sweeps every WCB and drives each on its own console. **Probe fix:** headers S1/S2 always bind a hardware channel, since EspSoftwareSerial rejects their pins. New helpers `Watch`, `Console`, `probe_in_mesh` / `mesh_params` and the link PWM/level methods; `opt_in` in `bench.json`. Probe mesh ids avoid W1's persisted learned peers (6 and 9, found in the session logs), and every command-sending test has its own id, because a WCB clears a client's duplicate ring only on a boot announce. `SerialDevice.set_baud` for the OTA baud bump; opt-in keys `ota_erase`, `ota_full`, `ota_full_wcb2`, `ota_wrong_chip`, `navicore_clip` and `sbus_reset`. `s22` rebuilds a Maestro slot table only under `?WDP,OFF` and reuses probe ids behind a duplicate-ring burn; the §1 Maestro rule was narrowed to match; new `bench.json` key `maestro_safe_sub`. **Existing tests fixed:** `maestro.fanout_remote` passed falsely — its pattern also matched NaviCore's "matches no local slot" line — and now requires NaviCore's exact dispatch line when it hosts device 1, skipping with a stale-proxy note when it does not; `sbus.to_navicore` now asserts the exact calibrated stick values instead of "moved by more than 500". |
| 2026-09-14 | _(pending)_ | **Probe v2, wire discovery and the GUI.** The probe became a runtime-configured instrument (five channels, formats, taps, reply rules, multi-header PWM, edge counting, pin levels, mesh mode) so tests never need new probe firmware. Wires are no longer hand-listed in `bench.json`: they are discovered by edge counting and saved to `results/links.json`, and tests skip — naming the wire — when theirs is missing. Added `gui.py` (devices, wiring plan and diagram, tests, log). The probe resets itself on first attach, because bindings survive between host runs and one leftover binding made `probe.scan` read `busy`. **Harness race fixed:** `WCB.run()` used `End of Version` as its end-of-output sentinel; after a reboot, `wait_boot`'s own trailing `End of Version` landed just after the next mark, so `?backup` parsed nothing and every later read was one command behind. `persist.label_reboot` and `seq.local` then skipped their restores and left a label and a stored sequence on WCB1 — caught by `config_guard`, cleaned up, and both WCBs re-verified identical to their pre-test config. The sentinel is now a unique `;S0` echo per call, and the guard retries a failed re-read and reports it with the original failure. Full run afterwards: 48 of 49 pass; the one failure is `etm.char_loaded`, the §6 firmware bug. Probe identification in *Find devices* sends a lone newline before `HELLO`, because the earlier 115200 `?VERSION` reaches a probe as newline-less garbage that would otherwise prefix it. More than one WCB can now have a USB cable (`wcb<N>`, added from the Devices tab), and the wiring suggestions follow the wires actually made. |
| 2026-09-14 | _(pending)_ | Initial harness, probe firmware and suites (50 tests), built against firmware `6.2.1_101034RSEP2026` on branch `WIFI`. Full bench run: 48 pass. `etm.char_loaded` fails on the firmware bug recorded in §6. Both §6 firmware findings came from this run: the 1500 ms pull dedup (two back-to-back pulls, the second silently unanswered) and the phase-3 broadcast suppression (the target logged none of the broadcasts). |
