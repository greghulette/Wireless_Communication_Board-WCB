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
python tests/hil/gui.py --run "hcr.*" etm.char_loaded   # opens and starts those tests, so a scripted run can be watched
python tests/hil/run.py                      # every test, command line
python tests/hil/run.py "maestro.*" "wled.*" # ids matching globs
python tests/hil/run.py --discover           # re-detect the wires first
python tests/hil/run.py --list | --plan | --links   # --list: each test's wires, expected time and opt-in
python tests/hil/run.py --resume [<run>]     # continue a paused or interrupted run (Ctrl+C once pauses a run)
python tests/hil/run.py --paused             # list the runs that can be resumed
python tests/hil/gui.py --resume [<run>]     # open the GUI and resume
python tests/hil/selftest.py                 # harness self-test: checkpoint and runner loop, no hardware
```

Each run writes `tests/hil/results/<stamp>/report.md` and `session.log` — every line sent and
received on every port, timestamped, with `# =====` markers per test. Next to them are
`checkpoint.json`, the run's resume state, written atomically when each test starts and ends, and
`run.lock`, an OS lock that marks the run as live in some process. `report.md` is rebuilt from the
checkpoint after every test. Creating a file named `PAUSE` in the run folder, or `results/PAUSE`,
pauses the run at the next test boundary (§9). The wires found last time are in
`results/links.json`. The directory is gitignored: the log carries the mesh password (`?backup`,
`?MGMT,PULL`). The checkpoint and the report do not: they store the password and WiFi credentials
only as hashes, including where a failure message quotes them.

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

**The host's own USB is part of the bench.** Every write to a bench port gives up after 2 s instead
of blocking. A port that vanishes is reopened, with DTR/RTS still low (`hil/serialdev.py`). While
tests run, the runner asks Windows not to idle-sleep (`SetThreadExecutionState`). That cannot stop
a lid close, the power button or a critical-battery hibernate, and a run started on battery is
flagged with a WARNING in the log. Two checks catch a host outage after the fact (`hil/runner.py`):
- A test that fails while the host was asleep for more than 5 s of it (`_awake_s()`, which compares
  Windows' sleep-excluding interrupt time with the monotonic clock). After a resume only some USB
  drivers drop their handles, so this is the reliable one.
- Every active bench port failing within 2 s of the others (`host_usb_loss()`): a hub reset or a
  pulled cable.

Either way the test is classified as an ERROR starting `NOT A RESULT`. It is checkpointed as not
run, not recorded as a result, and shows RETRY in the GUI. The run then waits up to 120 s of awake
time (`OUTAGE_GRACE_S`) for every port to come back. It then re-checks the bench: identity, mesh,
saved config and wires. The WCBs are rebooted first, since the cut-off test could not clean up.
After that the test runs again. If the ports do not come back, or a check fails, the run pauses
and can be resumed (§9). A second outage on the same test always pauses. Before each test a
further check runs. A bench port whose reader is waiting to reopen it, or more than 5 s of host
sleep since the last test ended, keeps that test from starting at all. Without it, the next test
and every one after it would fail on dead ports as genuine FAILs.

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
| `opt_in` | Tests that are skipped unless named here, because they wear flash or wipe a board's data: `nvs_wear` (fills W1's 100-variable table with persistent variables), `seq_wipe` (clears every W1 sequence, then replays them), `ota_erase` (OTA sessions, each erasing at least 4 KB of a board's inactive app slot), `ota_full` (full-image OTA on W1, two passes), `ota_full_wcb2` (the same, relayed to W2), `ota_wrong_chip` (an S3 image streamed to W1; attended), `navicore_clip` (replays a saved NaviCore clip: servo motion and recorded actions), `sbus_reset` (reboots the SBUS controller). Two long measurements are opt-in for their length: `softrx_erratum` (`softrx.erratum_pairs`, ~15 min of soft-port input into W1 S3-S5; needs wcb_probe 4) and `w1s4_soak` (`soak.w1s4_wire`, W1's S2/S4 fan-out for `soak_minutes`). Every key, with its title, one line on what it does and a per-test time estimate, is registered in `hil/optin.py` (`OPT_INS`). The runner skips a gated test before it starts, with `opt-in: add "<key>" to bench.json "opt_in" (<why>)`, unless the key is here. The GUI's Tests tab has one checkbox per key (§4), which writes this list. |
| `soak_minutes` | How long `soak.w1s4_wire` runs, in minutes (default 20). |
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

**No test may need more than two hardware channels on one probe at a time.** A wire on header S1 or
S2 needs A or B, and so does any wire above 38 400 baud, such as a tap on a 115200 Maestro line. If a
test binds a third, one of the others gives its channel up. The probe keeps RX history per channel
letter, so a read that started before that hand-off would get the new wire's bytes under the old
wire's name. `Link._since_ok()` (`hil/links.py`) refuses such a read with an `AssertionError` that
names the probe and channel. The fix is at the bench: move a wire that runs at 38 400 or less onto
header S3–S5, then run `--discover`.

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

**Resetting a native-USB board on purpose needs a DTR write after every RTS change.** On an
ESP32-S3's USB-Serial/JTAG port, RTS=1 with DTR=0 resets the chip. But Windows' usbser.sys sends
the line state to the device only when DTR is written, so an RTS-only pulse silently does nothing.
esptool works around the same thing (`_setRTS`, esptool `reset.py`). `sbus.signal_loss_controller_reset`
pulses this way, and it reads the controller's boot record to prove the reset really happened.

---

## 3. The probe (`tests/hil/wcb_probe`)

Instrument firmware for a spare **WCB V2.4 board** (ESP32-PICO-V3-02). It is not WCB firmware.
The PC configures it at runtime, so **a new test almost never needs new probe firmware**. The exception is
timing between two lines finer than a USB round trip can place, which is what `TXSKEW` (below) is for. The harness
needs wcb_probe 2 or newer; `softrx.erratum_pairs` needs 4 and skips on an older probe, and `probe.soft_concurrent_rx`'s
two-soft-channel arm needs 6 (flash `tests/hil/wcb_probe`). The current version is **7**; don't leave a probe on 6
(the boot loop below).

```bash
arduino-cli compile --fqbn esp32:esp32:esp32 --build-path <dir> tests/hil/wcb_probe
arduino-cli upload  --fqbn esp32:esp32:esp32 --input-dir <dir> -p COMx
```

It needs WCB_Client from the Arduino-Code sketchbook. EspSoftwareSerial is bundled in `wcb_probe/src/EspSoftwareSerial`:
a byte-identical copy of the WCB's patched one (`Code/WCB/src/EspSoftwareSerial`, tracker #78), because Arduino copies
a sketch into its build folder and a relative include of the WCB's copy cannot resolve. `selftest.py` fails if the two
copies differ. The code is in
`probe_main.cpp`, not the `.ino`: the Arduino preprocessor puts generated prototypes above the
struct definitions in an `.ino`, so functions taking a `Channel&` would not compile.

- **Pins** are the V2.4 map, copied from `wcb_pin_map.cpp` (`wcb_hw_version == 24`). If that map
  changes, `HDR_TX` / `HDR_RX` must follow.
- **Serial:** any header, any baud, formats 7/8 data bits with none/even/odd parity and 1/2 stop
  bits, optional inversion. `SWAP` listens on the header's TX pin. `RXONLY` never transmits (taps).
  Framing, parity and overflow errors are reported (`RXERR`), so a bit-banged WCB port that
  mis-times a byte shows up as an error, not as silently wrong bytes. A channel's TX line stays high
  while it is unbound and re-bound (v5): before that, every re-bind (any baud change) pulled the WCB's
  RX low for a moment, and the WCB read a NUL at the front of its next line (tracker #79). The soft
  channels C-E receive on level-triggered, polarity-flipping interrupts (v6), so two of them receiving
  at once lose nothing to ESP32 erratum GPIO-3.14; up to v5 they lost a few lines in a thousand.
  **Run v7, not v6.** A level arm survives a CPU-only reset (`MESH LEAVE`'s `ESP.restart()`, a panic)
  in the GPIO registers; v6 then re-enabled it at boot with no handler, and when the wired WCB rebooted
  and let its TX line drop, the probe panic-looped on the interrupt watchdog until the WCB booted
  again. v7 disarms every header pin before installing the GPIO ISR service, unbinds everything
  before `MESH LEAVE` restarts, and disarms a released pin (`disarmPinIrq`, tracker #78).
- **Reply rules** (`RULE ADD`): when bytes matching a pattern (`??` = any byte) arrive on a channel,
  the probe writes a reply itself, optionally delayed. A USB round trip is too slow for devices
  like a Maestro, whose get-query answer must arrive within 25 ms.
- **PWM** measure and generate on any header, several at once. **Edge counting** on every free
  pin (discovery). **Pin levels**: read, drive 0/1, release.
- **Skewed lines** (`TXSKEW`, v4): up to three 8N1 lines at once, bit-banged onto pins held high with
  `LEVEL .. 1`, each after the first starting a set number of nanoseconds after it (within one bit).
  Two hardware UARTs cannot do this: each starts a frame on its own baud tick, so their skew is
  whatever their phases happen to be. The probe builds the waveform as a list of level changes and
  plays it from IRAM with its interrupts masked, timed on the CPU cycle counter; changes on the same
  cycle share one register store. It is refused in mesh mode and while EDGES counts, and a probe
  channel still bound would lose what it receives meanwhile (up to 60 ms a call), so a test using it
  releases every wire on that probe first (`softrx.erratum_pairs`, tracker #78).
- **Mesh mode** (`MESH JOIN`): the probe becomes a WCB_Client with id, MAC octets, password,
  channel and checksum sent at runtime, temporary by default so no WCB persists it as a peer. It
  sends, broadcasts, `sendRaw`s and reports what it receives. WCB_Client has no `end()`, so
  `MESH LEAVE` reboots the probe.
- **A restart forgets every channel**, and the harness follows it. `LinkManager.bind()` records a
  probe log mark per binding and binds again when the probe has printed `BOOT wcb_probe` or
  `Guru Meditation` since (substring match: the ROM banner lands glued to the front of the BOOT
  line), or its port dropped and reopened (`<<reopened`: the probe runs on its USB power alone,
  and the BOOT line after a USB drop lands while the port is closed). A read whose mark is older
  than the restart fails with the reason instead of returning nothing. Only the bindings made
  before the restart are forgotten, each with a best-effort `UNBIND` (a re-enumeration without a
  reset keeps them on the probe); a wire bound after it keeps its channel, or the next bind can
  pick a letter whose header is still held (`ERR pin in use`). After every test the runner scans
  each open probe: a restart nothing planned (`MESH LEAVE`, the first-use `RESET` and the
  resume/outage reopen are planned) fails that test with `probe<n> panicked at t=...` (or
  `rebooted`, `lost its USB port`), then `RESET`s the probe and forgets all its channels.
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
| **Tests** | Every test grouped by area with its result, time and what it needs. Double-click runs one test; *Run selected* runs a test or a whole area; *Run everything*; *Run what failed*; *Stop after this test*. Every skip, a missing wire included, is written to `session.log` with its reason. During a run, *Pause after this test* (top bar, next to *Stop*) becomes *Cancel pause* once pressed; Stop and Pause replace each other, and the last one pressed wins. When a paused or interrupted run exists, a banner above the table offers it: *Resume*, *Other runs… (n)* (a picker that also lists stopped runs), *Open its report* and *Discard*. When only stopped runs exist, the banner is a one-line notice with just *Other runs… (n)*. A test re-queued after a host outage shows RETRY in amber. **Expected** shows each test's expected time: `0:12` is the median of its last five real results (PASS, FAIL or ERROR, never a SKIP or a NOT A RESULT), `~15:00` an estimate from `hil/optin.py` (no real result yet, or `soak.w1s4_wire`, whose length follows `soak_minutes`), `?` not known, and a grey `opt-in off` for a gated test whose key is not ticked. The running test's cell counts up as `elapsed / expected`; *Time* keeps the actual time of a finished test. An area row shows its sum, and *Run everything (~2 h 05 min)* and *Run selected (~12 min)* show the estimate before you start. A test the runner skips at once (a missing wire or device, or its opt-in off) counts 0 in every sum, and `+?` marks a sum with an unknown test in it. **Opt-in tests** (above the table, folded by default because open it takes most of the table's height; *Show* unfolds it) has one checkbox per `hil/optin.py` key, with its title, what it adds to a full run (`+15:40, 1 test`), and what it does. Its header line, shown folded too, says how many are on and what they add to *Run everything*, counting 0 for a test the runner would skip for a missing wire or device. Ticking one writes `bench.json` `opt_in` through the worker (atomically, every other key kept in its order). It re-reads `bench.json` first, so an edit made by hand while the GUI is open is kept rather than overwritten; if the file does not parse, nothing is saved and the box goes back. It also refuses, putting the box back, while a run is live in another window or terminal (its `run.lock` held): that run recorded `opt_in` at its start, and a change would turn its automatic outage recovery into a pause. The checkboxes are disabled while a run or a resume is going, because the runner reads `opt_in` before each test. |
| **Log** | Every serial line, live. |

All bench work runs on one worker thread, in order, so two actions never share a COM port. Past runs' durations are read on a
thread of their own at start and after each run (`hil/durations.py`, cached in `results/durations.json` by folder, file size and
mtime, so only new or changed runs are parsed). Above the tabs, an elapsed timer shows how long the current job has run and,
during a test run, how many tests are done and how long the current one has taken. The line under it shows the time left
with its finish clock: the expected time of every test in the run that has no result yet, less the running test's
elapsed time. Once *Pause* or *Stop after this test* is pressed it counts only the running test, since the run ends
with it.

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
- **Opt-in tests** declare the gate on the decorator: `@test(..., opt_in="ota_full")`, and optionally
  `opt_in_why="..."` when this test's reason differs from the key's default. The key must be in `hil/optin.py`
  `OPT_INS` (title, one line on what it does, the default reason, `estimate_s` per test); an unknown key fails at
  import. A new kind of opt-in adds its entry there, and the GUI grows a checkbox for it. The runner skips the test
  before it starts unless `bench.json` `opt_in` names the key, so the body holds no check of its own.
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
| Under a `?ETM,CHKSM` mismatch the target ACKs **before** its CRC check and then discards, so `?STATS` shows the command delivered while nothing ran. More generally an ETM ACK never proves execution. | `WCB.ino:4450-4453` (ACK), `:4484-4500` (CRC check) | `etm.chksm_mismatch_silent_loss` records it; delivery is always asserted on a probe wire, never on ACK lines. |
| A mesh `SET_MODE` to NaviCore saves nothing but is not free of side effects: every mode-aware knob re-dispatches. On this bench J2 moves the real servo on NaviCore's Maestro slot 1 ch 0, and that channel's 1500 ms idle auto-release stays armed in RAM until reboot or SET_CONFIG, so a later mesh `setTarget` there goes limp 1.5 s after it. NaviCore also sends two periodic tracked unicasts, its mesh-stats report (every 30 s, to `statsReport.wcb`) and `;V,MODE` (every 60 s, re-timed by a mode change, to `modeReport.wcb`). | NaviCore.ino `processKnobs`, `maestroIdleReleaseTick`; rc_telemetry.h `reportMeshStats`, `reportMode` | `navicore.maestro_mesh_settarget_readback` uses a channel no passthrough knob drives; `navicore.mesh_stats_counts` starts its window right after a stats report and allows the mode report's target one extra send (tracker #76). |
| A disabled controller is not ignored on receive: its ETM commands are still ACKed and run, and `etmSendAck` re-adds the sender as an ESP-NOW peer on demand. So NaviCore's 30 s stats report landing between `?CONTROLLER,OFF` and `ON,20` re-occupies the slot, and `ON,20` then prints no `registered (live)`. | `WCB.ino` `etmSendAck` (on-demand add), `enableControllerPeer` (prints only when the peer is absent) | `peers.controller_off_on` runs with ETM debug on and excuses the missing line only when an `[ETM] Sent ACK seq N to WCB20` falls inside the OFF window (tracker #80 notes). |
| A probe restart - `MESH LEAVE`, a panic, or a USB drop (the probe has no other supply) - forgets every channel binding (`setup()` starts unbound). A USB drop shows no BOOT line: it prints while the port is closed. | `probe_main.cpp:1087` (`setup`); `hil/probe.py:32` (`REBOOT_MARKERS`) | `LinkManager.bind()` and every read/send on a bound link re-bind when the probe printed `BOOT wcb_probe` / `Guru Meditation` / `<<reopened` since the binding (`hil/links.py:291`, `:468`); a read across the restart fails with the reason. The re-bind forgets only the bindings older than the restart, each with a best-effort `UNBIND` (`hil/links.py:316`), so a wire bound after it keeps a channel the probe really holds. After each test the runner fails that test with `probe<n> panicked at t=...` when a probe restarted unplanned, then `RESET`s it and forgets all its channels, or only the lost ones when the `RESET` fails (`hil/runner.py:422`; planned = `MESH LEAVE`, first-use `RESET`, resume reopen - `Probe.unplanned_reboots`). |

### `(should)` tests

Each was written from code review to catch a probable bug, so it failed until that bug was fixed, and it now
guards the fix. The middle column is the test's own statement of the correct behaviour. The bug, its mechanism
and the fix are in the [fix tracker](HIL_FIX_TRACKER.md) item in the last column, which is also where status
lives. A test with no item is not tied to a tracked defect. It either passes or has not run yet (an opt-in, or
the bench lacks its wiring).

| Test | Checks | Tracker |
|---|---|---|
| `input.timer_keeps_source` | A port line containing ;T keeps its source skip and obeys input blocking | #23, #45 |
| `input.nul_ignored` | A NUL (a break on the line) is dropped: the command after it still runs, and a NUL-only line broadcasts nothing | #79 |
| `input.soft_rx_under_soft_tx` | A soft port's input stays intact while the loop task writes soft ports | #16, #63 |
| `input.bcast_reset_persists` | ?BCAST,RESET's cleared input blocking stays cleared after ?config | #41 |
| `input.checksum_c_chain` | A checksummed chain starting with ?C runs every command | #25 |
| `map.text_self_wcb` | A text mapping to this board's own W<n>S<p> delivers locally | #35 |
| `map.failed_update_keeps_mapping` | An update with only invalid destinations leaves the existing mapping working | #17 |
| `map.bare_raw_flag` | '?MAP,SERIAL,S2,R' with no destination does not make S2 deaf | #17 |
| `map.raw_remote_bursts_no_loss` | Raw bursts to a remote port at 115200 lose no bytes | — |
| `map.raw_remote_s0_refused` | A raw mapping to a remote S0 is refused when it is configured | #36 |
| `map.clear_one_restores_flags` | CLEAR,S<n> restores the port's broadcast flags to what they were before the mapping | #46 |
| `map.clear_all_restores_output` | ?MAP,SERIAL,CLEAR,ALL re-enables broadcast output on formerly mapped ports | #46 |
| `map.timer_line_obeys_mapping` | A line containing ;T on a mapped port still goes only to the mapping | #45 |
| `map.remote_leading_comma` | A mapped line starting with ',' reaches a remote destination intact, like a local one | #37 |
| `pwm.p_refused_on_maestro_port` | ;P is refused on a Maestro port instead of silencing that Maestro (1 reboot) | #10 |
| `pwm.clear_out_restores_flags` | Clearing a PWM output port restores its deliberately-OFF broadcast flags (1 reboot) | #44 |
| `pwm.clear_out_keeps_queue` | ?MAP,PWM,CLEAR,OUT and ?PX do not reboot when nothing changed, nor drop the commands queued behind them | #9 |
| `pwm.invalid_remap_keeps_mapping` | An invalid re-map of an existing PWM input leaves its outputs and passthrough intact (2-3 reboots) | #15 |
| `pwm.input_only_advertises_cap` | A board whose PWM is input mappings only advertises the PWM capability (W1 x2, W2 x1 reboots) | #53 |
| `pwm.remote_refusal_logged_once` | A remote output aimed at W2's WLED port is refused and the refusal is not re-logged on every advert (W1 + W2 reboots) | #32 |
| `pwm.clear_all_reaches_remote` | ?MAP,PWM,CLEAR,ALL clears a remote PWM output on an ETM mesh (W1 + W2 reboots) | #8 |
| `pwm.remove_clears_all_remote_outputs` | Removing a mapping with two outputs on W2 clears both there (W1 + W2 reboots) | #9 |
| `pwm.self_wcb_output` | A PWM output addressed to this board's own W<n>S<p> produces pulses (2 reboots) | #43 |
| `hcr.setemotion_100_not_silent` | ;H,SETEMOTION,H,100 — inside the advertised 0-100 — is sent or rejected, never silently dropped | #48 |
| `hcr.input_validation_gaps` | ;H,VOL,X,40, ;H,FN,260,0,50 and ;H,FN,14,1,12345 are rejected, not muting, aliasing or sending a 5-digit track | #19, #49 |
| `hcr.stop_cancels_fade` | ;H,STOP cancels an HcrFade in flight, like STOPWAV and VOL do | #20 |
| `dfp.device_verb` | ;D,DEVICE,2 — advertised in the help — sends the select-device frame | #21 |
| `dfp.partial_frame_resync` | A truncated DFPlayer frame does not swallow the good finish frame after it | #2 |
| `conflict.dfp_port_unguarded` | HCR and MP3 refuse a port a DFPlayer already owns, as DFP refuses theirs | #1 |
| `var.clear_all_name_collision` | A variable named 'all' can be cleared on its own; ?VAR,CLEAR,all does not wipe every variable | #39 |
| `if.malformed_or_passes` | A malformed condition OR'd with a true one still fails closed and skips the command | #38 |
| `if.timer_split_trap` | An IF-gated ?SEQ,SAVE whose value holds ;T stores the whole value and runs none of it | #18 |
| `seq.cycle_guard_case` | A body recalling a different-case key that does not exist reports it missing, not a cycle | #40 |
| `seq.cycle_guard_reuse` | A sub-sequence reused in one run expands every time (back-to-back, across a ;t delay, at two depths, nine calls under one trigger); a ;t-delayed self-recall is still refused | #47 |
| `seq.reuse_queue_reserve` | Back-to-back reuse that would overflow the 200-slot command queue refuses whole nested expansions with one line each, never cuts a body short, and the board keeps answering | #47 |
| `legacy.cs_caret_lfi_roundtrip` | Legacy ?CS never stores a value containing '^?', which ?backup cannot restore | #24 |
| `etm.unvalidated_setters` | A bare ?ETM,TIMEOUT / BOOT / DELAY does not persist 0, and a negative timeout is rejected | #13 |
| `etm.legacy_miss0_rejected` | Legacy ?ETMMISS0 is rejected like ?ETM,MISS,0 instead of marking every peer offline | #13 |
| `etm.dedup_ring_covers_pending` | Nine in-flight unicasts to one board whose ACKs are lost each execute once, as eight do | #3 |
| `etm.acked_not_executed_reboot` | A command W2 ACKs while its ?reboot is pending is not silently lost (W2 reboot) | #12 |
| `stats.delivered_not_above_attempts` | ?STATS never reports more deliveries than transmissions: an ACK nobody expected is not counted | #22 |
| `chars.legacy_lf_guard` | Legacy ?LF; is refused like ?FUNCCHAR,; instead of making ';' the function identifier | #4 |
| `chars.cmdchar_lfi_guard` | ?CMDCHAR refuses the function identifier, as ?FUNCCHAR refuses the command character | #4 |
| `client_mesh.json_broadcast_once` | A client's ensured JSON broadcast is processed once per WCB, not once plus an ACKed unicast retry | #50 |
| `client_mesh.rejoin_seq_reuse` | A client that re-joins under the same id has its first commands executed, not ACKed and dropped as duplicates (2 W1 reboots) | #11 |
| `ota.help_matches_parser` | ?OTA help shows the <session> field the relay parser requires, and the unknown-subcommand hint lists BAUD | #30 |
| `ota.local_baud_rejected_rebegin_restores` | OPT-IN (ota_erase): a rejected re-BEGIN at a raised baud restores USB to 115200 instead of stranding it with no session | #61 |
| `ota.relay_teardown_frame_err` | OPT-IN (ota_erase): the relay DATA frame that tears W2's session down is ACKed ERR, not OK | #70 |
| `navicore.maestro_skip_not_logged_as_dispatch` | An inbound ;M for a channel NaviCore skips (> 31) is not also logged as dispatched | #65 |
| `navicore.wcb_send_ack_reflects_refusal` | WCB_SEND answers ok:false for a broadcast the library refused (188 characters under checksums) | #7 |
| `navicore.test_action_bad_target_not_ok` | TEST_ACTION answers ok:false for a wcb_unicast whose board id is outside 1-20 | #42 |
| `navicore.l1_names_board_profile` | #L1 names the configured board profile, not always 'WCB HW 3.2' | #31 |
| `maestro.get_moving_state_normalised` | getMovingState stores 0/1, as its own comments and WcbCmd's decoder (NaviCore) do, not the raw reply byte | #51 |
| `maestro.get_var_name_from_parsed_channel` | The RAM variable of getPosition uses the parsed channel like the frame does: ',05' fills m1pos5 and a trailing field still fills m1pos0 | #29 |
| `maestro.get_fallback_unicast_within_wcbq` | A get for an unconfigured id within WCBQ is unicast to WCB<id> like the subroutine and verb fallbacks, not broadcast to every board | #52 |
| `maestro.legacy_clear_suffix_keeps_slots` | A '?MAESTRO_CLEAR,M5' typo does not wipe every slot; the legacy handler prefix-matches and runs clearAllMaestroConfigs (WCB.ino:5796-5797) | #26 |
| `maestro.local_baud_zero_rejected` | ?MAESTRO refuses baud 0 for a local slot instead of printing 'Invalid baud rate' and then saving 'Local S1 at 0 baud' | #27 |
| `kyber.config_negatives` | ?KYBER parse errors leave the board as it was; a rejected port - out of range, or software serial S3-S5 (tracker #28) - does not switch RAM targeting on or rewrite the target table | #6, #28 |
| `kyber.local_rejected_port_keeps_forwarding` | On a Kyber_Local board in broadcast mode a rejected ?KYBER,LOCAL,S6 leaves forwarding working, instead of switching targeting on with an empty table that drops every Kyber byte until reboot; a refused ?KYBER,LOCAL,S3 leaves broadcast mode and the saved ?KYBER line alone | #28 |
| `kyber.local_port_s1_frees_s2` | With the Kyber on S1 (Maestro moved off it), W1 S2 still has a command reader and still gets broadcasts; S1 has one reader | #14 |
| `kyber.local_free_port_takes_pwm` | With the Kyber on S1, W1 S2 takes a WLED and a PWM output like any port and boots with its UART off; the Kyber port S1 refuses both, and ?KYBER,LOCAL cannot move onto the PWM port (3 reboots) | #73 |
| `kyber.local_port_move_releases_old` | Moving the Kyber between S1 and S2 gives the old port back (9600, broadcasts in and out on), a bare ?KYBER,LOCAL keeps the current port, a live move on a Kyber_Local board takes effect without a reboot, and ?MAESTRO,REMOTE releases the port and stops the Kyber task reading it (two reboots) | #73 |
| `wizard.kyber_release_order` | A push that moves or leaves a local Kyber sends ?KYBER,CLEAR / ?MAESTRO,REMOTE ahead of the BAUD/BCAST lines and re-sends the released port's rows; a remote board's targets-only diff sends no release (no board) | #73 |
| `wizard.kyber_local_frees_other_port` | A local Kyber claims only its own port (bare = S2), Maestro REMOTE keeps S1, and a push from REMOTE to local sends ?KYBER,CLEAR before an HCR on S1 (no board) | #73 |
| `kyber.local_s1_legacy_fallback_skips_kyber_port` | With the Kyber on S1 and no Maestro slot for W1's own id, the legacy S1 fallbacks (;M<own id>, ;M9, the ;M0 extra frame, a get on the own id) write nothing into the Kyber's port and never self-forward; ?KYBER and ?config name the real Kyber port | #73 |
| `kyber.local_clear_maestro_drops_target` | On a Kyber_Local board in targeted mode ?MAESTRO,CLEAR of a local Maestro drops its Kyber target, so the Kyber stops writing the freed port and ?backup no longer re-creates the slot; the freed port follows its ?BCAST flags; re-adding the Maestro restores the target | #73 |
| `kyber.clear_warns_reboot_single_reader` | ?KYBER,CLEAR on a Maestro_Remote board says a reboot is needed, and until then S1 is not read by both the serial parser and the still-running Kyber task | #59 |
| `kyber.maestro_s2_single_reader` | A local Maestro on S2 of a Maestro_Remote board has one reader: its bytes go to the Kyber bridge, not also to the serial parser | #59 |
| `kyber.target_id9_rejected` | ?KYBER refuses Maestro id 9 as ?MAESTRO does, so ;M9... still reaches each local Maestro instead of a stored device 9 | #5 |
| `softrx.erratum_pairs` | Two or three of W1's S3-S5 receiving at a sub-bit skew lose no more than single-port lines do (opt-in, ~15 min, wcb_probe 4). On the edge-triggered image it lost 227 of 10125 multi-port lines, every single exact (ESP32 erratum GPIO-3.14) | #78 |

### Constraints the bench runs established

The first full run (2026-09-15) found six firmware defects that no `(should)` test predicted. All six are fixed,
and each left a rule that is easy to break again. Later runs' finds are fix-tracker items, #56 onward.

| Rule | Where | Guarded by |
|---|---|---|
| A WCB_Client forgets a WCB's anti-replay window when that WCB's boot announce arrives. A WCB restarts its sequence numbers at boot, and a client that stayed up would otherwise drop the ones that fall in the old window. | WCBClient `WCB_Client.cpp` (`_cmdSeqDup`, the boot-announce handler) | `navicore.cli_over_mesh`, `navicore.trigger` |
| `;S` writes never `flush()`, and `Serial1`/`Serial2` have a 1 KB TX buffer. A writer that waits on the UART while holding its lock starves `serialCommandTask` on the same core, and input on the soft ports is lost. Only a live baud change flushes (`applyLiveBaud`). | `WCB.ino:6774-6778`, `:8323-8325` | `input.soft_rx_baud_sweep` |
| Every sequence-key path refuses keys over `SEQ_KEY_MAX_LEN` (15). NVS compares 15 characters, so a longer key reads the shorter key's value. | `WCB_Storage.h:157`, `WCB_Storage.cpp:593` | `seq.key_len_15` |
| Remote PWM configuration waits for `espNowInitialized`. A fixed uptime gate skips the remote send silently while local state, NVS and the success message still change. | `WCB_PWM.cpp:55-63` | `pwm.clear_all_reaches_remote` |
| JSON telemetry never sets `lastReceivedViaESPNOW` or `inSequenceBody`. It is consumed without running, and a controller's 5 Hz `rc_ch` would otherwise make the loop-prevention gate drop locally typed broadcasts at random (CLAUDE.md rule 3). | `WCB.ino:4520-4523` (ETM path), `:4815-4818` (plain path); gate `:2722` | `input.bcast_fanout`, `input.bcast_too_long_local_only`, `input.partial_poisons_next` |
| The ESP-NOW receive callback writes no soft port. `meshSerialWrite()` queues S3–S5 chunks for `MeshSerialOutTask` on core 1, because a remote raw mapping can target a port the receiver thinks is idle. | `WCB.ino:2110-2118`, `:2230`, `:2249` | `input.softserial_core0_contention` |
| On a classic ESP32, S3–S5 RX and PWM inputs run on **level** interrupts that the ISR flips after every edge. ESP32 erratum GPIO-3.14 loses an edge interrupt on GPIO0–31 while the GPIO ISR handles another pin: W1 lost 2.2 % of lines when two or three soft ports received together (tracker #78). Below ~74880 baud no edge-triggered `attachInterrupt` goes on those pins. The one exception is a soft port at 115200, which keeps `rxBitSyncISR` on a FALLING edge (RISING if inverted) on both chips. That ISR busy-waits a frame, so a level trigger would re-fire forever on a line held low. The port stays exposed to GPIO-3.14 and can steal the other pins' edges; `?BAUD` warns that 115200 soft input is unreliable. The pins are reconfigured only from core 1, because the ISR rewrites the pin's interrupt type without the IDF spinlock. The ESP32-S3 keeps edges. | vendored `src/EspSoftwareSerial/SoftwareSerial.cpp:577` (`rxBitISR`), `:191-198` (level arm), `:208` (115200 FALLING exception); `WCB_PWM.cpp:120` (`pwmEdge`), `:187`; CLAUDE.md rule 13 | `softrx.erratum_pairs`, `softrx.level_irq_stuck_line`, `input.softserial_tx_rmt` |
| A CPU-only reset (`ESP.restart()` from `?reboot`, OTA or a deferred restart, or a panic) keeps each pin's interrupt type and enable. A level arm that survives it, with no handler after the boot, re-fires as soon as the GPIO ISR service is installed, and the interrupt watchdog panics - a CPU reset again, so it loops until the line changes level. Arduino's `pinMode` re-enables it on its own (it copies the pin's current `int_type`). So `setup()` disarms every GPIO before `gpio_install_isr_service`; the probe does the same for its header pins. wcb_probe 6 hit this (full run `20260923-154611`, tracker #78). | `WCB.ino:8349` (`clearStaleGpioInterrupts`), `:8400`; `probe_main.cpp:95` (`disarmPinIrq`), `:1107`, `MESH LEAVE` `:1043` | No test reproduces it on a WCB; bench check in tracker #78. The harness fails a test when a probe restarts unplanned (table above). |
| `boardTable[].online` / `.lastSeenMs` are written by the ESP-NOW receive callback (WiFi task, core 0) and swept by `processETMHeartbeats` (loop, core 1). Every read-modify-write holds `boardTableMux` through the `board*` helpers, with `millis()` read before the lock and prints after it. Unlocked, a peer coming back was logged OFFLINE the moment it arrived and left offline until its next packet; merely reordering the stores leaves a lost update and an unsigned wrap. | `WCB.ino:513` (`boardTableMux` + helpers), `:4519` (callback), `:1249`, `:1258` (sweeps) | `etm.offline_detection_timing` (tracker #80) |

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
| A chip-family mismatch is rejected at BEGIN (`docs/WCB_OTA_TECHNICAL.md:288`). | Only the host-declared family is compared (`WCB_OTA.cpp:108-113`); a wrong image declared with the right family is stopped only by IDF verify at END. | `ota.local_wrong_chip_image` |
| `maestroAutoAddRemote` is first-host-wins (`WCB_Maestro.h:49`). | It adds a proxy per host, as `CLAUDE.md` rule 5 says (`WCB_Maestro.cpp:723-757`). | — |
| Query verbs write only the request frame, and the reply rides the Kyber return path (`WCB_Maestro.cpp:250-251`). | Gets are intercepted and their reply read synchronously on the port (`WCB_Maestro.cpp:287-297`, `:437-448`). | `maestro.get_local_replies` |
| Maestro script triggers are sent without ETM (`WCB.ino:4136`). | Every `;M` send is ETM: unicast, broadcast and fallback alike (`WCB_Maestro.cpp:133-148`, `:175-178`). | `maestro.m0_broadcast` |

---

## 7. Coverage

| Group | Checks |
|---|---|
| `probe.*`, `link.*` | Probe firmware answers; every discovered wire carries its port's output byte-exact, locally and over the mesh; two probe soft channels receiving at once decode exact (`probe.soft_concurrent_rx`). |
| `softrx.*` | Soft-port RX against ESP32 erratum GPIO-3.14 (tracker #78). `softrx.erratum_pairs` (opt-in): two- and three-port lines into W1 S3-S5 at a swept sub-bit skew from `TXSKEW`, against single-port lines on the same ports, observed on W1's USB console (W1 transmits nothing); it confirmed the loss on the edge-triggered image and must pass on the level-triggered one. `softrx.level_irq_stuck_line`: W1's S4 RX held low for 2 s causes no interrupt storm or reboot, and S4 reads text exactly afterwards. |
| `soak.*` | Opt-in soak (tracker #78): W1's S2/S4 fan-out for `soak_minutes`, with W1S4 alternately on a probe hardware and soft receiver, `;S4` alone, and `;S2` alone with W1S4/W1S5 checked for induced bytes; CSV per block, heap every 5 min. |
| `wcb.*` | Backup and remote-pull CRCs, firmware match across boards, unknown-command and help-trap parsing. |
| `serial.*` | `;S` comma form, `;S0` to USB, `^` chains in order, bad port, `;T` delays, summed delays, `?STOP`. |
| `mesh.*` | Unicast delivery and ETM ACK accounting, alias routing, unreachable target, chain split, `?MGMT,FRAG`, `?RTERM`. |
| `persist.*` | Label (local, remote, across reboot) and live baud change, each restore-verified. |
| `input.*` | Bytes injected into WCB ports: CR/LF terminators, partial lines and per-port buffers, long lines and queue overflow, `?BCAST` IN/OUT filtering and fan-out, Maestro reply routing, soft-serial RX under load and across bauds, framing variants, `@WDP` device announces: one record per port and device type, a shared port's label staying with the first type heard, a full port table replacing its least-recently-heard type, and records on a shared port expiring one at a time. |
| `map.*` | `?MAP,SERIAL` text and raw mappings: local, multi-destination, remote, S0 and soft ports, validation, persistence across reboot, the CLEAR paths, legacy `?SM`. |
| `pwm.*` | `;P` pulse widths and guards, PWM output ports, the deferred mapping reboot, local and mesh passthrough measured on the probe, WDP PWMTARGET auto-config and self-heal. |
| `hcr.*`, `mp3.*`, `dfp.*`, `mp3dfp.*`, `route.*`, `conflict.*`, `backup.*`, `port.*`, `wdp.hcr_*` | Exact device bytes for every `;H`, `;A` and `;D` verb with no device attached, emulated device replies (HCR status, MP3/DFPlayer finish and error callbacks), HCR poll timing and fades, config validation, WDP route learning and how to undo it, port moves, backup round trip. |
| `var.*`, `if.*` | Variable verbs, limits, names, persistence and backup position, `;M!`, per-board scope; every IF operator, AND/OR, gating scope, timers, rejections, IFs run on W2 and inside stored sequences. |
| `seq.*`, `legacy.*`, `inv.*` | `?SEQ` formats, the 15-character key limit, NAMES order and its FNV-1a hash, recall forms, comment stripping, the save boundary, the cycle guard and sub-sequence reuse, `,L` and nested fan-out; legacy `?CS`/`?CE` and `?CHK`; the `?MGMT,SEQ`/`SEQGET` relay, its dedup, multi-chunk values, and the longest value a board actually stores (TOOBIG checked once one is long enough). |
| `maestro.*` | Exact Pololu frames for every verb, invalid-argument rejection, target 9, fan-out to a remote host, and a real servo `setTarget` → `getPosition` round trip over the mesh; LIST in every spelling; get-query decoding, timeouts, short and extra replies, and rejections; the `;M0` broadcast; fallback broadcast, and fallback unicast to the probe as client 5; stale proxies; a client's get answered back to it, and the `;M!` name guard; `CLEAR,ALL` with legacy routing and an ordered rebuild; the slot map and 9-slot cap; every CLEAR form and add-path refusal. |
| `kyber.*` | The Maestro_Remote bridge both ways, with a real Maestro reply; `?KYBER,LOCAL` on S2 across the pre-reboot deaf window, targeted and broadcast forwarding, explicit targets and the Maestro-to-Kyber return; the Kyber on S1 leaving S2 readable and free for a WLED or PWM output, and S3-S5 refused before any state changes; moving the Kyber between S1 and S2 and `?MAESTRO,REMOTE` releasing the old port; a Maestro clear dropping its Kyber target; the legacy S1 fallbacks skipping the Kyber port; the Kyber port named in `?KYBER`/`?config`; `?KYBER,CLEAR` without a reboot; a local Maestro on S2; target id 9; parse errors; WDP reverting a board to Maestro_Remote. |
| `wled.*` | Exact JSON for every `;L` verb routed to the WLED host, unknown-verb silence. |
| `navicore.*` | JSON ping, mesh membership and WDP table, `;W20` remote CLI, `WCB_SEND` to both WCBs, mesh `TRIGGER`; its Maestro inventory against W1's proxies, `?MAE` markers, mesh setTarget with read-back, mesh queries answered into W1's variables, inbound `;M` guards and 0/9 fan-out; serial routing, plain broadcasts both ways, `WCB_SEND` edges and fragments, JSON bridged through W1, declined mesh JSON, exact mesh stats, safe `FORGET_PEER`, `SET_MODE`, `TEST_ACTION`, `TRIGGER` bounds and dispatch traces, record/replay markers (opt-in clip playback), `#L` diagnostics, and a temporary probe it never learns. |
| `sbus.*` | Controller ping; a virtual stick moves NaviCore's decoded channel to the exact calibrated value and back to centre; the safe channels NaviCore binds nothing to; matrix button single, double, triple and held taps with rc_trig timing; tap mode after `SET_MODE`; exact switch, slider and trim values; opt-in signal loss by resetting the controller. |
| `etm.*`, `stats.*`, `misc.*` | Reboot announce; `?ETM,CHAR` phases; ETM settings (set, persist, validate, legacy spellings); retries and failure with ACKs lost, the duplicate-ring and pending-table limits, offline detection and retry cancel, CHKSM mismatch and the 187-character limit, ETM off, per-board broadcast ACKs, the receive CRC gate from a probe; `?STATS,RPT` locally and over the mesh; `?IDENTIFY`, `?LED`. |
| `peers.*`, `wdp.*`, `alias.*`, `chars.*` | Live peer counts, live `?WCBQ`, controller off/on and auto-enable; WDP DUMP fields against config, LIST/DETAIL, POLL, OFF/ON, AUTOJOIN, ADD/FORGET, a temporary probe's join and eviction, device announces; alias sanitising and propagation; changing and restoring the delimiter, function identifier and command character. |
| `client_mesh.*`, and the mesh-mode `input.*`, `map.*`, `var.*` and `seq.*` tests in `s19` | The probe as a temporary WCB_Client: adoption and its WDP row, unicast and broadcast delivery and ACKs, sendRaw (including onto the real Maestro line), fragments and the 187-character limit, `?WHOAMI`, WCB broadcasts reaching a client, checksum and password mismatches, re-joining, the stale-peer window; client broadcasts into ports, the Maestro return path, soft-serial TX under mesh load and core-0 contention, raw chunk bounds, client-set variables, sequence fan-out seen from the mesh. |
| `ota.*` | `?OTALOCAL` status, parsing and help, session-less and usage errors, BEGIN guards; relay-side rejects and CRC handling, relay BEGIN rejects on W2, the NaviCore brick guard. Opt-in: local sessions (cursor, supersede, verify failures, bad magic, overrun, base64 errors, idle timeouts), the USB baud bump, relay sessions on W2 (cursor, teardown, END, keep-alive), a cross-transport abort, and full-image transfers (a corrupt image, a same-image re-flash of W1 and of W2 in two passes each, a wrong-chip image). |

Every group has run on the bench except `soak.*` (new, opt-in) and `softrx.level_irq_stuck_line` (new, needs the level-triggered image); `softrx.erratum_pairs` has run once, on the edge-triggered image (`20260923-113109`). Current failures, and what each one is, are in
[HIL_FIX_TRACKER.md](HIL_FIX_TRACKER.md).

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

The Wizard UI is covered only by the six `wizard.*` tests (§8). In Kyber, only what the bench cannot reach is untested: a real Kyber brain, and the WDP
Kyber-local ruleset, since NaviCore's controller rule is evaluated first (`WCB_WDP.cpp:475-480`).

## 8. Wizard tests (`tests/wizard`)

Playwright drives the real Wizard over real Web Serial, with the harness in charge. Each Wizard test is a harness
test in `suites/s30_wizard.py` with a Playwright test of the same id in `tests/wizard/specs/`. The harness decides
what the board should look like and checks it afterwards (`config_guard` as usual); the browser test only drives the
page. `run_wizard_test()` (`hil/wizard.py`):

1. reads the device's USB VID/PID from pyserial and releases its COM port (`bench.close_device`);
2. serves a `Bridge` (`hil/bridge.py`) on a free localhost port and passes its URL as `HIL_BRIDGE`;
3. runs `node …/@playwright/test/cli.js test --grep <id>` in `tests/wizard`, logging its output to `session.log` as
   `wizard`, with a 300 s watchdog that kills the whole process tree (a surviving Chrome would keep the port);
4. takes the port back and waits for `?VERSION` (Chrome's open/close can reset the board);
5. turns the Playwright JSON report into PASS / FAIL / SKIP.

While Chrome holds the board, the browser test reaches the rest of the bench through the bridge: `/wire/mark`,
`/wire/expect`, `/wire/received`, `/wire/send` on any probe wire, `/config` for another board, `/note` into the log.
Bind a wire in the harness test *before* the handoff (`link(bench, 1, "S1").listen()`), because binding follows
the port's baud, and reading that needs the board's own console.

**Finding the port.** Web Serial shows no COM numbers, only VID/PID. `wcb2` and both probes are identical CP210x
bridges (`10C4:EA60`), so each device gets its own persistent Chrome profile, `tests/wizard/.profiles/<device>`
(gitignored), holding only that board's grant. The first run for a profile puts a *HIL: Pick COMx* button on the
page and opens Chrome's port dialog: pick the COM port it names. The grant persists (every bench board reports a USB
serial number, which Chrome needs to keep one). A wrong pick is revoked with `port.forget()` and fails the test, but
cancelling the dialog, or leaving it unanswered for three minutes, **skips** — needing it at all means nobody has
authorized that board, which says no one is at the keyboard rather than that the Wizard is broken. `WIZ_NO_AUTHORIZE=1`
skips an unauthorized board without opening the dialog at all, for a run nobody is watching.
After that, tests connect through the Wizard's own port picker (`boardManualConnect`) with no dialog, and check the
pulled WCB number. To re-authorize, delete that profile folder.

**Parser tests, no browser at all.** `tests/wizard/unit/parser.test.js` runs `Wizard/parser.js` under
`node --test`: parse a real `?backup` (the fixture `fixtures/w1-backup.txt`, captured from W1 on the bench with its
two passwords replaced and its CRCs recomputed), rebuild it, parse it again, and check the config is unchanged; check
that an unchanged board emits **no** commands and an edited one emits **only** its own; check a system file survives
save and reload. That is where config loss would show: the Wizard pushes the diff against the baseline it pulled, so
a field the parser drops rewrites a good board. `npm run unit` in `tests/wizard`, or `wizard.parser` in the harness.
Note `kyber.targets` (Maestros on other boards) is derived — the Wizard recomputes it from every connected board at
push time — so the round-trip comparisons leave it out.

**CI.** `.github/workflows/wizard-tests.yml` runs the parser tests and `wizard.smoke` on every push touching
`Wizard/**` or `tests/wizard/**`. It is deliberately separate from `build.yml` and filtered to those paths, so a
Wizard change never rebuilds firmware or publishes binaries (CLAUDE.md rule 9). The board tests need hardware and
never run there; they skip themselves without `HIL_BRIDGE`. The page is served by `tests/wizard/serve.js`, Node's
own http server, so a runner with no `python` on PATH behaves the same as this PC.

**Setup and running.** Once: `cd tests/wizard && npm install && npx playwright install chromium`. Then run
`python tests/hil/run.py "wizard.*"` or pick them in the GUI. Chrome runs headed; `WIZ_HEADLESS=1` tries headless,
which is untested with Web Serial. `npx playwright test` in `tests/wizard` runs standalone: `wizard.smoke`, `wizard.kyber_release_order` and `wizard.kyber_local_frees_other_port` run, and
the board tests skip. The page is served from the repo root on `http://127.0.0.1:8778` by `serve.js`, because the Wizard
loads `../Images/`. Never change that origin: grants are per origin, so every profile would need authorizing again.

`openWizard()` turns Chrome's HTTP cache off for the page. The persistent profile keeps that cache too, and a
server that sends `Last-Modified` (`python -m http.server`, which `reuseExistingServer` adopts if one is already on
8778; `serve.js` sends none) lets Chrome reuse a cached script without asking. On 2026-09-23 that ran the previous
`parser.js` after an edit, so a test passed or failed against code that was no longer on disk.

The Wizard's globals (`boardBaselines`, `_boardPullInFlight`, `boardPushOutcome`) are read by name from
`page.evaluate`. `pushConfig()` wraps `boardGo` and awaits its promise, because `boardGo` writes a provisional
`boardPushOutcome` before it marks the push in flight, so polling the outcome can read a stale "done".

---

## 9. Pausing and resuming a run

A run can be paused between tests, the bench unplugged, moved and powered off, and the run resumed
later in the same results folder. `hil/checkpoint.py` holds the run's record, and `hil/resume.py`
holds the checks that re-establish the bench. `hil/runner.py` (`start_run`, `continue_run`) drives
both. `python tests/hil/selftest.py` tests the checkpoint and the runner loop without hardware.

### Pausing

- **GUI:** *Pause after this test*, next to *Stop*. The current test finishes, then the status
  reads "Paused - safe to disconnect". Closing the window during a run asks first. *Yes* pauses
  after the current test and then closes; *Cancel pause* after that cancels the close too. *No*
  closes now: the checkpoint is frozen, so the test in flight is never recorded, and the run is
  left interrupted. If the run ends while the question is open, the window closes on any answer
  but *Cancel*.
- **CLI:** Ctrl+C once pauses after the current test, and `run.py` exits 3. Ctrl+C twice aborts
  at once and exits 130. The test in flight is cut off, and it runs again first on resume.
  Windows only delivers the signal when the main thread's current wait returns, so the message
  can lag. The pause itself is still taken at the boundary. The Wizard tests' node and Chrome,
  and `wizard.parser`'s `node --test`, run in their own process group, so a first Ctrl+C does
  not kill a Wizard test in flight. During a Wizard test the harness waits in 0.5 s steps, so
  the second Ctrl+C lands at once, and it kills node and Chrome, freeing the board's port.
- **PAUSE file:** create `results/<run>/PAUSE` or `results/PAUSE`. The run pauses at the next
  boundary and deletes the file. This pauses a run started in the background or by Claude. A
  PAUSE file that was already there when a segment began is stale; it is deleted and logged.
  Any PAUSE file found after that counts, whatever its date: a copied or moved file keeps its
  source's. A stale one that cannot be deleted (read-only, held open) is ignored until it
  changes.

"Safe to disconnect" means the checkpoint and report are written, `session.log` is fsynced and
closed, every COM port is closed (no reader thread is left reopening a COM name that another board
may take), keep-awake is off, and the run lock is released. A pause asked for during the last test
does not happen: that run finishes as done.

### Resumable runs

`checkpoint.json` records a state: `running`, `paused`, `stopped`, `done` or `abandoned`. A run
that is `running` with its `run.lock` free was interrupted: power-off, crash, or a window closed
with *No*. It is resumable, and its test in flight runs again first. The GUI banner and
`run.py --resume` offer paused and interrupted runs, newest first. `run.py --paused` and the
picker also list stopped runs, which resume only when named. *Discard* marks a run `abandoned`.
`run.py --resume <run>` refuses a done or abandoned run (exit 2), and `--resume` takes no test
globs. The lock is an OS byte lock that the OS drops when the process dies. It means nothing
across machines. A run whose tests all have a result stopped after its last result and before
its final save; resuming it marks it done without the bench checks.

A save that another program blocks (antivirus, a sync client, a viewer holding the file) is left
in `checkpoint.json.tmp`, and loading prefers that file whenever it parses: a successful save
consumes it, so it is always the newest. Nothing retries the last saves of a run, so this is
what keeps a final result or a pause from being lost.

The checkpoint holds:

- the selection, frozen at the start, in order;
- one row per result;
- the host outages (`outages`) and dropped test ids;
- one entry per segment: start, end, active time, host, and the USB serial, VID and PID and the
  firmware of each device;
- the bench.json and links fingerprints, each stored in full so a change can be shown as a diff;
- the harness's git HEAD and source hashes;
- `config_ref`, the saved-config reference.

In `config_ref`, the mesh password and WiFi credentials (`?EPASS,<pass>`,
`?WIFI,AP|JOIN,<ssid>,<pass>`) are stored as `<prefix><redacted:sha256[:12]>`. Comparisons and
diffs only ever use that form. The free-text fields quote board output: a result's or outage's
detail, the pause reason and the last resume error. They pass through the same redaction
(`redact_text`), which also covers `Password: <pw>` lines, the probe's `MESH JOIN ... PASS=<pw>`
(also masked in the probe's own error) and the SBUS controller's `wifiNets`.
So do the lines the pause and resume code adds to `session.log`. The log's serial traffic still
carries the plain password in the `?backup` lines, as it always has.

The remaining tests are the selection minus those with a result, in selection order. A cut-off or
`NOT A RESULT` test has no result, so it comes first. A test renamed or removed since the start is
dropped and listed under *Not run*, also when it was the only one left and the run resumes
straight to done. Tests added to the suite since the start are not part of the
run. If the run was started from globs, an area, *Run selected* or *Run everything*, the report
lists the ones that selection would have matched, so they can be run separately.

### The resume checks, in order

A hard block keeps the run paused and names what to fix. A soft difference asks: a dialog in the
GUI, `[y/N]` at a terminal. Without a terminal it declines unless `--force` is given, and a
question with a default of yes (the reboot in step 4) takes it. stdin on NUL counts as no
terminal, although Windows reports it as one: the PowerShell tool and Task Scheduler run that
way. The automatic resume after a host outage treats every soft difference as a block. Stop,
Pause or Ctrl+C during the checks leaves the run paused, and that is not an error: `run.py`
exits 3.

1. **Offline.** Soft: another host, a bench.json change (COM ports and notes excluded), a wiring
   change, a missing `links.json` (*Yes* restores the run's wires from the checkpoint, and each
   one must then verify), changed test code, and dropped ids. Hard: a checkpoint format this
   harness does not read.
2. bench.json and links.json are reloaded. Find devices or Auto-detect may have run while the run
   was paused.
3. **Identity. It never guesses.**
   - A WCB is its board number plus the USB serial recorded at the start. NaviCore and the SBUS
     controller are their kind plus that serial. Each is looked up by serial with no traffic sent.
     A WCB's board number is then confirmed on that port with the fixed `WCB_WEBTOOL_CONFIG_PULL`,
     which answers whatever the LFI, command character and delimiter are, and only then `?VERSION`
     and `?config` (`hil/identify.py`). `?config` is read up to its board, "Delimiter Character:"
     and "Command Character:" lines, never with `WCB.run()`, whose `;S0,HILEND` sentinel a changed
     command character broadcasts and a `,` delimiter splits into a broadcast.
   - A WCB whose LFI, command character or delimiter is not the default is a hard block at once,
     with the restore commands, the delimiter's first (`?D^`, which has no `,` for a `,` delimiter
     to split). A cut-off `chars.*` test leaves this, and all three are saved in NVS, so a power
     cycle keeps them. There is no boot-wait retry: every later step sends lines that such a board
     would broadcast.
   - NaviCore and SBUS are proven by their ping in step 10. The SBUS controller may reset when
     its port opens.
   - A probe is its MAC, and the serial only says which port to try first.
   - A port that answers with the right identity but has a different serial is never adopted
     without asking. A second WCB1 at home is the case in mind.
   - A device with no usable serial falls back to Find devices' rule: the one port that answers
     with its identity. Two such ports are a hard block.
   - Hard blocks: a device on no port; a port held by the Arduino IDE, a Wizard tab or another
     WCB Bench; a probe with no `mac`.
   - A board on its own USB serial that does not answer is named, with the likely cause if a
     test was cut off: `chars.*` (command character, LFI or delimiter), `ota.*` (USB baud) or
     `etm.*`/`wdp.*` (ETM, channel or password).
   - Moved ports are saved to bench.json and listed in the report.
4. **After a cut-off test**, every WCB is rebooted and NaviCore's debug flags are zeroed. This is
   offered, and recommended. A test killed mid-way skipped its `finally`, so RAM-only state may
   still be on. After a clean pause nothing is rebooted.
5. **Mesh.** Every bench WCB must be online from WCB1 (`?STATS`) and from NaviCore within 45 s,
   then a 3 s settle for WDP adverts. This is the only presence check for a mesh-only WCB. A
   NaviCore that never answers (its port held, or ROM download mode after a cold boot) is blocked
   on NaviCore itself, not on the mesh.
6. **Firmware** versions against the last segment's (soft).
7. **Saved config** against `config_ref`, in `config_guard`'s "missing [...] / extra [...]"
   wording (soft). *Yes* continues with the boards as they are and records "config accepted with
   difference". *No* leaves the run paused, to fix by hand. The reference is taken at the start
   (runs of 5 tests or more), at every pause, after a `CONFIG NOT RESTORED` test, and every 15 min
   of active time. Without a reference this step is skipped, with a note in the log.
8. **Probes:** RESET. A probe still joined to the mesh leaves it, and `?WDP,FORGET` goes to every
   WCB, since RESET does not leave the mesh.
9. **Wires:** every wire verified at the pause is checked again, at its port's configured baud,
   in its recorded orientation (`LinkManager.check`). Nothing is rediscovered. A wire that passes
   is marked verified in links.json, since the check is the same proof *Verify* records; without
   that, a wire restored from the checkpoint would drop out of every later resume's check.
   Every failing wire is listed in one block, naming the cable to check.
10. **Controllers:** NaviCore PING (10 s), then the SBUS controller's JSON ping (60 s). After a
    cut-off `sbus.*` test, the controller's sticks are centred and its buttons and button-mode
    trims released. It keeps them in RAM with no timeout, and opening its port does not reset
    it. Switches and sliders are left as they are.

After a clean pause nothing else needs re-establishing: a reboot clears the WCBs' RAM-only state
(`?DEBUG`, `?RTERM`, `;P` pins, queues). ETM and WDP rebuild from heartbeats, which step 5 waits
for. After a cut-off test, step 4 covers what that test may have left on. Three things are not
checked at all: NaviCore's saved config, a Maestro's servo positions or running script, and the
SBUS controller's mode, switches and sliders. Module-level caches keyed per Bench, such as
`_CHAR` in `s99_etm.py`, survive a resume in the same GUI. If the firmware changed while the run
was paused, restart the GUI before resuming.

### The report and the timer

`report.md` is rebuilt from the checkpoint after every test and never appended to. A run with one
segment that finished with no host outage and no dropped test has the layout it always had. The
extras appear only when they apply:

- a status line (`RUNNING`, `PAUSED after N of M`, `STOPPED`, `ABANDONED`) with the last blocked
  resume;
- *Segments*, when there is more than one;
- *Re-run after a host outage*;
- *Not run*.

`took` is active time: the sum over segments of awake time (`QueryUnbiasedInterruptTime`), so time
paused, time between processes and time asleep never count. The GUI timer continues from the
paused value.

### Checking it on the bench

- A run with no pause gives today's report.
- Pause mid-run, then open a bench port in the Arduino monitor: it must open. Resume after 5 min;
  the timer excludes the 5 min.
- Pause, unplug everything, power off, and reconnect in a different USB order: the moves are
  listed and the run finishes.
- Blocks: a moved probe wire, an unplugged WCB, a port held open, NaviCore unplugged.
- Confirmations: an `opt_in` edit, reflashed firmware, a changed `?LABEL`, Auto-detect run while
  paused.
- A second board answering as WCB1 is asked about, never adopted.
- Unplanned stops: close the GUI with *No*, kill python.exe, pull the hub for 20 s and for over
  2 min, sleep the laptop mid-test and between tests.
- CLI: Ctrl+C once and twice, a PAUSE file, `--force` without a terminal, and Ctrl+C during a
  Wizard test and during the resume checks.
- Cut-off leftovers: close the GUI with *No* during `chars.cmdchar_change_restore` (the resume
  blocks at once with `?CMDCHAR,;`, and nothing reaches the mesh) and during `sbus.to_navicore`
  (the re-run passes). A Ctrl+C does not leave these, because the test's `finally` still runs.

---

## Revision log

| Date | Commit | Change |
|---|---|---|
| 2026-09-23 | _(pending)_ | **Full run `20260923-154611`'s four failures fixed (compiled, not flashed).** wcb_probe 7 (§3): v6 boot-looped on interrupt-WDT panics when a CPU-only reset left a soft-RX level arm with no handler and the wired WCB rebooted; v7 disarms the header pins before the GPIO ISR service, unbinds before `MESH LEAVE`, and disarms a released pin. The WCB's `setup()` gets the same boot-time clear (§6 constraints row, tracker #78). The harness re-binds a link whose probe restarted since it was bound, fails a read across the restart with the reason, and fails the test in which a probe restarts unplanned (`probe<n> panicked at t=...`; §3, §6 behaviour row); A dropped and reopened probe port counts as a restart; a restart forgets only the bindings older than it (with a best-effort `UNBIND`), and the end-of-test incident `RESET`s the probe, since wiping the host side alone left the probe holding headers and the next bind hit `ERR pin in use`. `selftest.py` gains four checks with a fake probe that enforces pin ownership (39). New §6 constraints row for the `boardTable` presence lock (tracker #80, `etm.offline_detection_timing`). `peers.controller_off_on` runs with ETM debug on and accepts a missing `registered (live)` only when an ACK to WCB20 inside the OFF window explains it (§6 behaviour row). |
| 2026-09-23 | _(pending)_ | **Opt-in checkboxes and expected durations.** The opt-in keys are registered in `hil/optin.py` (title, what, default reason, per-test estimate; `w1s4_soak` follows `soak_minutes`). The 28 gated tests declare `@test(opt_in=...)` instead of checking in their bodies. The runner skips them before they start, after the missing-wire check, with the same message as before, so reports do not change. `selftest.py` pins every gated id and message. An unknown key fails at import. `hil/durations.py` takes the median of each test's last five real results from `checkpoint.json`, or from `report.md` for older runs, cached in `results/durations.json`. The GUI's Tests tab gains an *Expected* column (live `elapsed / expected` on the running test), area sums, estimates on *Run everything* / *Run selected*, the time left with a finish clock on a line of its own under the top timer (appended to the timer, it was clipped at the default width), and an *Opt-in tests* panel of checkboxes that write `bench.json` `opt_in` (§2, §4, §5). The panel starts folded: open, it left the table about six rows at 1320x860 and pushed the detail pane off the tab. A tick re-reads `bench.json` before writing, so a hand edit is not overwritten by the GUI's stale copy, and is refused while another process's run holds its `run.lock`. The time left counts only the running test once *Pause* or *Stop* is pressed, and the panel header's added time counts 0 for a missing-wire test, as *Run everything* does. A `checkpoint.json` of the wrong shape (a result row with no `id`) falls back to its `report.md`, and one unreadable run folder no longer empties the whole history. `run.py --list` shows each test's expected time and opt-in tag, and reads the cache without writing it. `selftest.py` (35 checks) covers the aggregation, the gate and `--list`; the GUI was checked on a hidden Tk root against a temp bench and results folder, not yet on screen. |
| 2026-09-23 | _(pending)_ | **Tracker #78 confirmed and fixed (firmware compiled, not flashed).** `softrx.erratum_pairs` on W1 (`20260923-113109`, edge-triggered image) lost 227 of 10125 two- and three-port lines at -1.5 to -3 µs skew, and every single-port line arrived exact: ESP32 erratum GPIO-3.14. The WCB now vendors EspSoftwareSerial 8.1.0 (`Code/WCB/src/EspSoftwareSerial`). On the classic ESP32 it receives on level interrupts flipped after every edge, and the PWM input ISRs work the same way; the S3 is unchanged. New test `softrx.level_irq_stuck_line`: W1's S4 RX held low for 2 s must not storm or reboot W1, and S4 must read text afterwards. `input.softserial_tx_rmt` also asserts each soft port's `[SOFTSERIAL] S<n> RX:` mode against `?HW`: level-triggered on 1/21/23/24, edge-triggered on 31/32. §6: `softrx.erratum_pairs` joins the `(should)` table, and a new constraints row covers level-triggered RX. §7 `softrx.*` updated. Both `softrx.*` tests skip only on a config that takes a port off the text path (the reasons `processIncomingSerial` skips a port); a clean port that cannot read one line on its own is a FAIL, since that is the fix broken. A stuck-line storm that resets W1 waits for the boot before restoring config, and keeps the storm message in front of any restore error. The probe keeps the stock library. |
| 2026-09-23 | _(pending)_ | **Tracker #78 verified on W1, and wcb_probe 6.** On the level-triggered image `softrx.erratum_pairs` lost 0 of 10125 multi-port lines (227 on the edge-triggered one), `softrx.level_irq_stuck_line` passed, and 139 soft-port, map, device and PWM tests passed (`20260923-144022`; started from `run.py`, paused with a PAUSE file after 42 tests and resumed in the GUI). `soak.w1s4_wire` ran 20 minutes clean (`20260923-113109`). `probe.soft_concurrent_rx` then failed on the probe's own edge-triggered soft channels (3 of 300 lines on each), so wcb_probe 6 bundles the WCB's patched EspSoftwareSerial (§3) and the test requires both of its arms exact on v6; on an older probe it skips the two-soft arm. `selftest.py` checks the two library copies are identical. |
| 2026-09-23 | _(pending)_ | wcb_probe 5 (§3): a channel's TX line stays high while it is unbound and re-bound. Every re-bind (any baud change) used to pull the WCB's RX low, W1 read a NUL at the front of its next line, and its parser broadcast that line as an empty command, which failed `kyber.local_port_move_releases_old` step (e). The WCB line reader now drops NUL bytes too (tracker #79), covered by the new `(should)` test `input.nul_ignored` (§6). Targeted run `20260923-095057` for #47 and #73 D4-D9: 139/140, the one failure being this; `input.*` 50/50 after the fix (`20260923-111854`). |
| 2026-09-23 | _(pending)_ | **Pause and resume (§9). `tests/hil/selftest.py` passes (31 checks, no hardware). On the bench from `run.py` (2026-09-23 12:08-12:14): a run never paused ends exactly as before (`20260923-120813`); a PAUSE file pauses at the next boundary with every COM port free, and `--resume` continues the same report as a second segment (`20260923-120834`); a run killed inside `input.bcast_in_block` is listed as interrupted, and its resume reboots the WCBs, refuses the config that test left (`?BCAST,IN,S3,OFF`) until it is restored, then re-runs that test first (`20260923-120933`). That bench run also found `identify_port`'s HELLO failing on every probe: the `?VERSION` it sends at 115200 reached the probe as garbage that prefixed HELLO, so a bare line end now goes first (the same fault was in the GUI's Find devices). The GUI's buttons and moving to other USB ports are not yet run on hardware.** Every run gets `checkpoint.json` and `run.lock`, and `report.md` is rebuilt from the checkpoint after every test. A single-segment run that finished keeps the old layout byte for byte (selftest compares it against the previous `write_report`). Runs pause from the GUI (*Pause after this test*, or *Yes* on close), with Ctrl+C in `run.py`, or with a `PAUSE` file. They resume from the Tests-tab banner, `gui.py --resume` or `run.py --resume`, after resume checks that pin every device by the USB serial recorded at the start and never adopt a different board unasked. A host outage no longer stops the run. The test is checkpointed as not run, and after 120 s of awake time for the ports it is re-run behind automatic checks. Otherwise the run pauses. A pre-test gate keeps a test from starting on dead ports or after a sleep between tests. The checkpoint and report store `?EPASS`, `?WIFI,AP|JOIN` and `Password:` values only as hashes, in `config_ref` and in every free-text field. `identify_port` moved from `gui.py` to `hil/identify.py`; it reads `?config` to its own lines instead of through `WCB.run()`, whose `;S0` sentinel a changed command character broadcasts, and a WCB whose board number does not print answers "a WCB, board number unknown" instead of "could not open". An adversarial review of the change was applied before any hardware run: loading prefers a newer `checkpoint.json.tmp`; a run whose tests all have results resumes to done without the checks; a changed LFI or command character blocks the resume at once, found with `WCB_WEBTOOL_CONFIG_PULL`; a silent NaviCore blocks on NaviCore; a wire that passes its resume check is marked verified; a cut-off `sbus.*` test's held inputs are released; Ctrl+C during a Wizard test or during the resume checks behaves as documented; `RunControl` takes an RLock (a Ctrl+C inside it deadlocked); a PAUSE file counts whatever its date; `run.py`'s questions treat stdin on NUL as no terminal; the GUI shows stopped runs' picker, records its selection, and a cancelled close-pause no longer closes the window later. A second review of those fixes was applied too: a delimiter left by a cut-off `chars.delim_*` test blocks the resume with `?D^`; `taskkill` runs in its own process group and is waited out, because a Ctrl+C mashed during an abort ended it and left node and Chrome holding the COM port; the GUI's close-now ends a Wizard test in flight; the probe's `PASS=` is redacted; a run whose only unfinished tests were renamed lists them as not run; and the start-of-run firmware record closes the ports it opened, so a run never holds NaviCore or a probe it does not use. `bench.json` and `links.json` are written atomically, a corrupt `links.json` is set aside, and the Wizard tests' Playwright runs in its own process group. The pre-test gate, not a change to `host_usb_loss()`'s window, catches ports lost between tests: the in-test classification is unchanged. |
| 2026-09-23 | _(pending)_ | **Measurement for tracker #78, not yet run.** New suite `s23_softrx_measure.py` with two opt-in tests. `softrx.erratum_pairs` (`softrx_erratum`) asks whether W1 loses soft-port input to ESP32 erratum GPIO-3.14: 4500 two- and three-port rounds into W1 S3-S5, each followed by single-port lines on the same ports as the control, at a seeded skew (60 % within +-15 us, 40 % within half a bit, ~5 % zero). W1 only prints "Ignored chain command" for them, with `?BCAST,IN` off on S3-S5. It needs the new probe verb `TXSKEW` (wcb_probe 4, §3): the hardware channels cannot set the skew, because each UART starts a frame on its own baud tick. `soak.w1s4_wire` (`w1s4_soak`, `soak_minutes`) repeats the load of the W1S4 episode (run `20260923-012123`) with W1S4 alternating between a probe hardware and soft receiver, and classifies a recurrence as W1's content, W1's S4 waveform or lead, sub-bit glitches, or S2 coupling. Both write a CSV into the run folder. §2 `opt_in` and `soak_minutes`, §3 and §7 updated. Probe 1 must be flashed with wcb_probe 4 before `softrx.erratum_pairs` can run. |
| 2026-09-23 | _(pending)_ | **Wizard parser tests and CI (§8).** `tests/wizard/unit/parser.test.js` runs `Wizard/parser.js` under `node --test` against a real scrubbed `?backup` fixture: full-push round trip, an unchanged board emits nothing, an edited one emits only its own commands, `CLEAR` forms, port claims, and a system-file round trip. Eight tests, no browser, ~0.3 s; `npm run unit`, or `wizard.parser` in the harness. New `.github/workflows/wizard-tests.yml` runs them plus `wizard.smoke` on `Wizard/**` and `tests/wizard/**` pushes only, never touching `build.yml`. The test server is now `serve.js` (Node) rather than `python -m http.server`, so CI needs no Python. Recorded while writing them: remote Maestro proxies parse into `kyber.targets` and remote WLEDs into `wledRemotes` (display-only, never pushed) — a full push emits neither unless the caller supplies a whole-system `maestroTable`. |
| 2026-09-23 | _(pending)_ | Two new `(should)` tests for tracker #47 in s17: `seq.cycle_guard_reuse` (13 HIL keys on W1 S1, all `,L`: eight distinct leaves then a repeat under one trigger, reuse across a `;t`, one leaf at depth 2 and 1, and a `;t`-paced self-recall that must be the only refusal; on the current images it fails with a depth refusal for HILL8 and cycle refusals for HILL1-3) and `seq.reuse_queue_reserve` (12 back-to-back calls of a 20-command sub-sequence: every call either expands whole or prints one "Command queue nearly full" line, and no "Command queue is full" line appears). `seq.cycle_guard` is unchanged; `seq.cycle_guard_case`'s docstring now names the exact-bytes compare. Not yet run on hardware. |
| 2026-09-23 | _(pending)_ | Two new `(should)` tests for tracker #73 D4: `kyber.local_free_port_takes_pwm` (Maestro 1 cleared off S1, WDP off, Kyber on S1: `?MAP,PWM,OUT,S1` and a WLED on S1 are refused, a WLED and then a PWM output on S2 are accepted, `?KYBER,LOCAL,S2` is refused onto the PWM port, and after a Kyber_Local boot S2 keeps its output, idles LOW with its UART skipped, and `;P2` gives one pulse; the S1 refusals match by prefix because their wording differs between images, so the current images fail first at the WLED S2 arm) and `wizard.kyber_local_frees_other_port` (no board: the claims a local Kyber and Maestro REMOTE leave, and a REMOTE -> local push releasing S1 before an HCR there). Not yet run. |
| 2026-09-23 | _(pending)_ | Two new `(should)` tests for tracker #73 D5/D9: `kyber.local_port_move_releases_old` (Maestro 1 cleared off S1, WDP off: a move S2 -> S1 releases S2, the bare `?KYBER,LOCAL` keeps S1, then after a Kyber_Local boot a live move back releases S1 and the task follows with no reboot, and `?MAESTRO,REMOTE` releases S2 so a `;S0` line typed there prints once and nothing reaches the mesh; it stops at the first missing release message, so the current images fail at step b before any injection) and `wizard.kyber_release_order` (no board: the push generator releases before BAUD/BCAST and re-sends the released port's rows). `kyber.local_rejected_port_keeps_forwarding` compares only the `?KYBER,LOCAL` line, since `?backup` now also emits an early `?KYBER,CLEAR`. `openWizard()` disables Chrome's HTTP cache (§8): the persistent profile had served a stale `parser.js`. `wizard.kyber_release_order` and `wizard.smoke` pass standalone; the kyber test has not run on hardware. |
| 2026-09-23 | _(pending)_ | Two new `(should)` tests for tracker #73: `kyber.local_s1_legacy_fallback_skips_kyber_port` (Kyber on S1, no own-id slot: the legacy S1 fallbacks send nothing into the Kyber port, `;M0` still broadcasts, and the `?config` row of the Kyber port says "(Kyber)" with W1's S1/S2 labels cleared for one read and replayed) and `kyber.local_clear_maestro_drops_target` (a local Maestro clear drops its Kyber target, the freed port follows its `?BCAST` flags, `;M9` still writes a free S1, and a re-add restores the target). `kyber.local_mode_s2` now expects "Kyber port: Serial2 (115200 baud)" in place of "Initialized Serial1 & Serial2". Not yet run on hardware. |
| 2026-09-23 | _(pending)_ | `probe.soft_concurrent_rx` rewritten. It has a control arm: W1S2 on a probe hardware channel, so only one probe pin takes edge interrupts, and 1000 lines must be exact. It has a rated two-soft arm: ESP32 erratum GPIO-3.14 can drop an edge when two soft channels receive at once, so up to 2 % of lines may be lost, but a line lost on both wires, or a mangled line that isn't a near-miss of a sent one, fails. The channel-letter swap is gone, because this loss follows the lagging wire, not the letter. Its first run (`20260923-012123`) caught an intermittent ~3.5 % corruption on W1S4 even in the control arm; minutes later it passed clean (`20260923-012735`). Tracker #78, including a bench check. |
| 2026-09-23 | _(pending)_ | Relay reads retry like a real requester. The SEQ and SEQVAL replies are unacknowledged broadcasts sent once, so s17's `_relay` helper and `inv.mgmt_seq_w2_relay` try up to 3 times, spaced past the 1.5 s duplicate filter (tracker #77; `inv.mgmt_seq_remote` lost one reply on the 23:58 image). Verification run `20260923-002254`: 18/18, including the NaviCore readback after `sbus.trim_exact`'s mode change (#76). |
| 2026-09-23 | _(pending)_ | **Full run `20260922-215719`: 401 pass, 2 fail, 4 skip in 1:39:11** (the skips are the three WDP-DA label checks, which need an unlabelled W1 port, and `ota_wrong_chip`, which is opt-in). Both failures were NaviCore test bugs (tracker #76; §6 has the behaviour): `navicore.maestro_mesh_settarget_readback` drove a knob-owned channel whose auto-release an earlier run's SET_MODE had armed, and `navicore.mesh_stats_counts` caught NaviCore's 30 s stats report in its window. `kyber.remote_roundtrip` now retries on its best-effort channel (#74). |
| 2026-09-22 | _(pending)_ | **Rerun `20260922-193306` (69 tests): 64 pass, 4 fail.** `ota.relay_full_same_image_wcb2` failed because the laptop, on battery since 15:59, hibernated at critical battery at 19:58 (Windows Kernel-Power 524/42). Only W1's CH9102 port errored after the resume, so the all-ports rule missed it. The runner now detects sleep inside a test from awake time and warns when a run starts on battery (§1). `ota.relay_teardown_frame_err` confirmed its predicted firmware bug (tracker #70; its "Documentation that disagrees" row is gone). `inv.seqget_largest` lost a 7-chunk unacknowledged reply, and now retries like a real requester (#71). `kyber.local_port_s1_frees_s2` failed as expected until #14 was applied, which also removed the Kyber row from "Documentation that disagrees". |
| 2026-09-22 | _(pending)_ | **Triage of full run `20260922-151421`.** All 8 diagnoses were adversarially verified; fixes are tracker #63-#69, and #14, #28 and #58 were updated. Tests: `input.wdpda_rejects` counts only S5 lines (another port's normal expiry had failed it). The WDP-DA tests run basic, rejects, fields, usb, propagation, ttl, so none waits out another's 90 s record in a full run. `wdp.controller_auto_enable` re-polls up to 3 times, because one lost broadcast frame used to empty its window. `sbus.trim_exact` can finally run: a matrix trim under a mode where both its slots are unmapped. `inv.seqget_toobig` became `inv.seqget_largest`, since no bench board can store a value over 2,903 characters. `kyber.local_port_s3_frees_s2` became `kyber.local_port_s1_frees_s2`, since S3-S5 are refused. New `probe.soft_concurrent_rx`. The probes run wcb_probe 3 (GPIO ISR at level 3, #66). The three new WDP-DA tests (`input.wdpda_shared_port`, `_port_full`, `_shared_ttl`) came from another session's issue #19 work. §7 no longer claims most groups have not run. |
| 2026-09-22 | _(pending)_ | **First opt-in run (`20260922-185238`), and three fixes from it.** (1) The OTA tests looked for their image only in `Code/bin`, by the running version string. A local bench build keeps the committed string, so 15 of them skipped. `_image` now prefers the image the harness flashes (`results/builds/wcb-esp32-meshq`), and only then a CI release. (2) `ota.local_baud_rejected_rebegin_restores` confirmed its predicted bug: the firmware now restores 115200 after a rejected BEGIN (tracker #61, `WCB_OTA_TECHNICAL.md`), and that row left "Documentation that disagrees". (3) `sbus.signal_loss_controller_reset` never reset the controller: an RTS-only pulse does not reach a native-USB board on Windows (§2). It now re-writes DTR after each RTS change and checks the controller's boot record (tracker #62). |
| 2026-09-22 | _(pending)_ | **A host sleep no longer fails the rest of a run.** Full run `20260922-151421` lost 12 tests to the laptop sleeping 44 min in. Windows logged a power-source change at 15:59:03 and "entering sleep" at 15:59:09, and all seven bench ports failed in the same 12 ms. It resumed at 18:26, so one test "took" 8841 s. The runner now keeps Windows from idle-sleeping during a run and detects the all-ports-at-once loss. The test it hits becomes ERROR `NOT A RESULT`, and the run waits for the ports or stops. §1 describes it. |
| 2026-09-22 | _(pending)_ | **§6 rewritten to current state.** Almost every `(should)` row described a bug that is now fixed as if it were current. The table now lists what each test checks (its own title) and its fix-tracker item, and status lives in the tracker. The first full run's six bugs became the rules they left. The fixed `?ETM,CHAR` row is gone, and the CHKSM row's line numbers are updated. `hil/links.py` has a hand-off guard (tracker #60): a read across a hardware-channel hand-off now fails and names the probe, instead of returning another wire's bytes. That is what failed `navicore.plain_broadcast_both_ways`, where probe2 needed three of its two hardware channels. `input.wdpda_rejects`, `etm.chksm_mismatch_silent_loss` (and the other five `_require_w2_online` callers) and `kyber.wdp_auto_remote_revert` now wait for their precondition instead of skipping: the WDP-DA TTL, W2 back online, and NaviCore's advert after a `?WDP,POLL`. |
| 2026-09-22 | _(pending)_ | `hil/wcb.py` `WCB.run()` drops `{"sys":1,...}` lines: relayed RC telemetry reaches W1's USB at up to 5 Hz while a relay window is open and interleaved with command output (`?KYBER,LIST` once began with an `rc_ch` line). Tests that want those lines read `w.dev` directly, as the NaviCore suite already does. |
| 2026-09-22 | _(pending)_ | Wizard tests: **all four pass on the bench** (full run `20260922-095852`; `wizard.pull` 32.8 s including the one-time port authorization, and the pushed label was read back from `?backup` and restored). An unauthorized board now **skips** instead of failing when the port dialog is cancelled or unanswered, or when `WIZ_NO_AUTHORIZE=1` is set — that was how the 09:28 run reported a collision with a concurrent bench session. |
| 2026-09-22 | _(pending)_ | `gui.py`: dark by default (`--light` for the old look) - two palettes at the top of the file, status colours per palette, and the Windows title bar set dark through DWM, since Tk does not draw it. A double-click on a test row while a run is going is now refused instead of queueing a SECOND run and blanking those tests' results in the table. |
| 2026-09-22 | _(pending)_ | `hil/serialdev.py`: every write is bounded (`write_timeout`, set ONCE at open - assigning it on an open port makes pyserial re-run `SetCommState` under the reader's in-flight read, and doing that per send lost/delayed replies on the S3 native-USB ports), raising a test failure, and a port that vanishes is reopened by the reader thread, DTR/RTS still deasserted. Before, a native-USB board that stopped draining its endpoint blocked `write()`+`flush()` forever, freezing a GUI run for ten minutes while it held COM4, and a USB drop (SBUS controller, 2026-09-21) left the reader dead so every later test on that device errored. The Devices tab's port dropdowns now list USB ports only, are read-only and ignore the mouse wheel: a wheel scroll over one saved a new port, and `sbus` ended up on a Bluetooth SPP port (COM19), which blocks writes forever. |
| 2026-09-22 | _(pending)_ | **Wizard tests (§8).** Playwright drives the Wizard over real Web Serial as harness tests (`suites/s30_wizard.py`, `hil/wizard.py`, `hil/bridge.py`, `tests/wizard/`): the harness hands one board's COM port to Chrome, serves a localhost bridge to the probes and other boards, and takes the port back. One Chrome profile per bench device holds its Web Serial grant, authorized once by hand. `wizard.smoke` passes standalone and through `run.py`; `wizard.pull`, `wizard.push_label` and `wizard.terminal_wire` have not yet run on the bench. |
| 2026-09-21 | _(pending)_ | `gui.py --run <globs>` opens the GUI with those tests queued and starts them (same globs as `run.py`), so a run started from a script can be watched live. `run.py` forces UTF-8 stdout: redirected to a file on Windows it was cp1252, and the first `→` in a failure message killed the run. |
| 2026-09-15 | _(pending)_ | **Bench run of the fixes, and two more firmware bugs.** Final full run, every board on tonight's images: 285 pass / 62 fail / 51 skip in 51:22, from 249 / 96 / 53 in the morning. 53 of the 62 failures are `(should)` tests pinning known bugs. Of the nine others, four are §6 entries (`input.soft_rx_baud_sweep`, `map.baud_on_raw_mapped_port`, `input.softserial_core0_contention` at 9 of 10, and one rule-13 mis-frame in `input.bcast_fanout`), one is a test bug (`etm.offline_detection_timing`), one is NaviCore telemetry landing in a console the test reads (`maestro.list_and_legacy_spellings`), and three are NaviCore's USB console stalling for seconds at a time (`navicore.cli_codes`, `sbus.slider_exact`, `sbus.btn_double_triple_tap`) — a NaviCore-side issue, worth a ~1 Hz `Serial.flush()` tick in its `loop()`. New firmware findings, both fixed in the working tree: the ESP-NOW callback bit-banging S3-S5 (those chunks are now queued for `MeshSerialOutTask` on core 1 — `input.softserial_core0_contention` went from 0 of 10 blocks exact to 9-10 of 10, the rest being the documented residual), and JSON telemetry clobbering `lastReceivedViaESPNOW`, which cost locally-typed broadcasts their mesh copy at random. Harness fixes: a probe's channel table is cleared in place rather than popped (a bind in flight registered into an orphan, so two wires shared one channel and a test read another port's traffic — the cause of `hcr` and `bcast` wire failures that passed when run alone); `wdp.autojoin_off_ignores_probe` waits for id 15's 22.5 s advert instead of 17 s; `navicore.maestro_mesh_rejects` flushes NaviCore's console with `#L12` per case instead of sleeping on it. `input.soft_rx_baud_sweep` stays red: bit-banged RX under mesh load is at its limit, and the counts in its message are lines RECEIVED, not lost — worth rewording. |
| 2026-09-15 | _(pending)_ | **Fixes for the four new firmware bugs** (§6), not yet run on the bench: WCBClient 1.17.1 forgets a WCB's anti-replay window on its boot announce; `;S` no longer calls `flush()` (a live `?BAUD` on S1/S2 flushes instead); every sequence-key path refuses keys over 15 characters (`SEQ_KEY_MAX_LEN`); remote PWM configuration is gated on ESP-NOW having started rather than on 5 s of uptime, and says so when it cannot send. Review follow-ups: a boot announce clears a sender's duplicate window once per boot, in the WCB firmware and in WCB_Client, so a command retried between its three announces cannot run twice; erasing by an over-long key still drops it from the key list; `ONERR` callback keys are capped at 15 characters; S1 and S2 get a 1024-byte TX buffer before `begin()`, the second half of the S3 input loss (the first re-run after removing the flush still lost 3 of 20 lines at 38400). |
| 2026-09-15 | _(pending)_ | **First full run and its triage.** 398 tests in 50:48: 249 pass, 96 fail, 53 skip. Every failure was traced to its cause. 47 of the 53 `(should)` failures confirmed their predicted bug; the other six failed for harness or test reasons below. Harness bugs: `probe.mesh_leave` anchored `BOOT wcb_probe` with `^`, which never matches after the ROM banner, so all 21 probe-mesh tests failed on leaving (and a failed leave skipped the `?WDP,FORGET` cleanup, which skipped three more); a baud pinned by `map.raw_remote_115200_exact` outlived it and garbled every later injection into W1 S2 (8 failures); `send_chunked` sent 1500 bytes into the probe's 1024-byte `TX` buffer. Test bugs: a LOW-parked port misframes a whole burst (2 PWM tests), `pwm.passthrough_local` marked after the pulses, `pwm.clear_all_reaches_remote` cleared inside the 5 s send hold-off, `conflict.dfp_port_unguarded` missed that `?HCR,CLEAR` re-enables the poll, `etm.offline_detection_timing` matched NaviCore's WCB20 heartbeats as WCB2, and `chars.delim_comma_restore_path` sent lines back to back across a delimiter change. All fixed. New firmware bugs are listed in §6. |
| 2026-09-15 | _(pending)_ | **GUI: elapsed timer, and devices on ports.** An elapsed timer above the tabs shows the job's time, tests done of the total, and the current test's time; `report.md` records the run time. `bench.json` `port_devices` records real devices wired to WCB ports, set from the Wiring tab: such a port is never made to transmit without a `port_stimulus`, a probe wire on it is forced listen-only, auto-detect, *Verify* and `link.out` skip it, and a test that needs a probe there skips naming the device. Missing-wire skips are now written to `session.log`; they were shown only in the GUI, which hid why 46 tests skipped once W1 S5 went to NaviCore S1 and the W2 S1 tap was not found. A review then closed the gaps in that rule: a listen-only wire on such a port still counted as a wire, so its tests ran; `links.json` loaded an old two-way wire unchanged; tests that pick a port at run time, or drive one without a wire, bypassed the check; and auto-detect deleted a wire added there by hand. `links.get()` now hides the port, `load()` forces the tap, `usable()` and `drives` cover the rest, and auto-detect keeps such wires. The Wiring tab's right pane scrolls (the device form was off-screen at Windows display scaling), and queuing a run no longer cancels a pending *Stop after this test*. Coverage audit added to §7. |
| 2026-09-15 | _(pending)_ | **Suites s12–s22** (349 tests): serial input, serial mapping, PWM, HCR/MP3/DFPlayer, variables and IF, sequences and the inventory relay, ETM/peers/WDP/command characters, the probe as a mesh client, OTA, NaviCore and the SBUS controller, Maestro routing and the Kyber bridge. Written from code-reviewed specs; not yet run on the bench. 60 `(should)` tests pin probable firmware bugs, and fifteen doc/code disagreements are listed (§6). `CLAUDE.md` rule 4's CHKSM command limit corrected from 188 to 187 (`ETM_MAX_CMD_WITH_CRC`, `WCB.ino:225`). **Discovery fix:** it swept only `mesh_only_wcbs`, which emptied when WCB2 got its own USB cable, so WCB2's wires were never found; it now sweeps every WCB and drives each on its own console. **Probe fix:** headers S1/S2 always bind a hardware channel, since EspSoftwareSerial rejects their pins. New helpers `Watch`, `Console`, `probe_in_mesh` / `mesh_params` and the link PWM/level methods; `opt_in` in `bench.json`. Probe mesh ids avoid W1's persisted learned peers (6 and 9, found in the session logs), and every command-sending test has its own id, because a WCB clears a client's duplicate ring only on a boot announce. `SerialDevice.set_baud` for the OTA baud bump; opt-in keys `ota_erase`, `ota_full`, `ota_full_wcb2`, `ota_wrong_chip`, `navicore_clip` and `sbus_reset`. `s22` rebuilds a Maestro slot table only under `?WDP,OFF` and reuses probe ids behind a duplicate-ring burn; the §1 Maestro rule was narrowed to match; new `bench.json` key `maestro_safe_sub`. **Existing tests fixed:** `maestro.fanout_remote` passed falsely — its pattern also matched NaviCore's "matches no local slot" line — and now requires NaviCore's exact dispatch line when it hosts device 1, skipping with a stale-proxy note when it does not; `sbus.to_navicore` now asserts the exact calibrated stick values instead of "moved by more than 500". |
| 2026-09-14 | _(pending)_ | **Probe v2, wire discovery and the GUI.** The probe became a runtime-configured instrument (five channels, formats, taps, reply rules, multi-header PWM, edge counting, pin levels, mesh mode) so tests never need new probe firmware. Wires are no longer hand-listed in `bench.json`: they are discovered by edge counting and saved to `results/links.json`, and tests skip — naming the wire — when theirs is missing. Added `gui.py` (devices, wiring plan and diagram, tests, log). The probe resets itself on first attach, because bindings survive between host runs and one leftover binding made `probe.scan` read `busy`. **Harness race fixed:** `WCB.run()` used `End of Version` as its end-of-output sentinel; after a reboot, `wait_boot`'s own trailing `End of Version` landed just after the next mark, so `?backup` parsed nothing and every later read was one command behind. `persist.label_reboot` and `seq.local` then skipped their restores and left a label and a stored sequence on WCB1 — caught by `config_guard`, cleaned up, and both WCBs re-verified identical to their pre-test config. The sentinel is now a unique `;S0` echo per call, and the guard retries a failed re-read and reports it with the original failure. Full run afterwards: 48 of 49 pass; the one failure is `etm.char_loaded`, the §6 firmware bug. Probe identification in *Find devices* sends a lone newline before `HELLO`, because the earlier 115200 `?VERSION` reaches a probe as newline-less garbage that would otherwise prefix it. More than one WCB can now have a USB cable (`wcb<N>`, added from the Devices tab), and the wiring suggestions follow the wires actually made. |
| 2026-09-14 | _(pending)_ | Initial harness, probe firmware and suites (50 tests), built against firmware `6.2.1_101034RSEP2026` on branch `WIFI`. Full bench run: 48 pass. `etm.char_loaded` fails on the firmware bug recorded in §6. Both §6 firmware findings came from this run: the 1500 ms pull dedup (two back-to-back pulls, the second silently unanswered) and the phase-3 broadcast suppression (the target logged none of the broadcasts). |
