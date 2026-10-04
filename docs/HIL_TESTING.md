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
python tests/hil/run.py --no-servos          # everything except the tests that can move a real servo (§2 no_servos)
python tests/hil/run.py --discover           # re-detect the wires first
python tests/hil/run.py --list | --plan | --links   # --list: each test's wires, expected time and opt-in
python tests/hil/run.py --resume [<run>]     # continue a paused or interrupted run (Ctrl+C once pauses a run)
python tests/hil/run.py --paused             # list the runs that can be resumed
python tests/hil/gui.py --resume [<run>]     # open the GUI and resume
python tests/hil/selftest.py                 # harness self-test: checkpoint and runner loop, CLAUDE.md rule 12 (t_rule12_remoteterm_first), no hardware
```

Each run writes `tests/hil/results/<stamp>/report.md` and `session.log` — every line sent and
received on every port, timestamped, with `# =====` markers per test. Next to them are
`checkpoint.json`, the run's resume state, written atomically when each test starts and ends, and
`run.lock`, an OS lock that marks the run as live in some process. Once a test has written NaviCore's
config, `navicore_snapshot.json` holds NaviCore's exact config from before that test (§5, §9). `report.md` is rebuilt
from the checkpoint after every test. A run of five or more tests also reads `?NVS` on each WCB with its own USB cable at
its start and end: the settings-storage use goes into session.log, a line per board under the report's summary,
and `tests/hil/results/nvs_history.csv`, so a store creeping towards full shows across runs (`hil/nvs.py`). Creating a file named `PAUSE` in the run folder, or `results/PAUSE`,
pauses the run at the next test boundary (§9). The wires found last time are in
`results/links.json`. The directory is gitignored: the log carries the mesh password in the WCBs' lines
(`?backup`, `?MGMT,PULL`), and `navicore_snapshot.json` and `results/navicore_config_week_start.json` hold
NaviCore's. NaviCore's and the SBUS controller's own lines in the log have every credential hashed (§5). The
checkpoint and the report store the password and WiFi credentials only as hashes, including where a failure message
quotes them.

**Before a run, close anything holding a bench port** — the Arduino IDE serial monitor, Wizard
or NaviCore tool tabs. A COM port has one owner. The GUI's *Close all ports* hands them back.

---

## 1. What a run does to the bench

The suite is not read-only. It **changes saved config** (labels, baud, sequences, variables),
**moves servos** (including Maestro subroutine restarts), **plays NaviCore animations**, moves
the SBUS controller's virtual sticks, and **reboots WCBs**. `run.py --no-servos`, or `no_servos` in
`bench.json` (§2), runs everything except the tests that can move a real servo.

Every test that changes saved config runs inside `config_guard` (`suites/common.py`): it
snapshots each affected board's config chain first and **fails the test if the board does not
end byte-identical**, even when the test body itself failed, naming the missing and extra
tokens. The snapshot is `?backup`'s configured-boards chain for the USB board and
`?MGMT,PULL,<n>` for a board reached over the mesh, CRC-checked in both cases. `?PEERSLIVE` is
excluded (it moves with learned peers on its own), and a `?SEQ,SAVE` value that itself contains
`^` is re-joined the way restore parses it (`WCB.ino:2326-2349`). After the guard, the harness
refreshes its config cache and re-binds any wire that follows the port's baud. Before it reports a leak, the guard
puts back what it safely can: a leaked `?BAUD`, `?BCAST`, `?LABEL`, `?SEQ,SAVE`, `?MAP,SERIAL`, `?VAR,SET` or `?ALIAS`
token is replayed from the baseline, or its CLEAR form sent when the baseline never had it, and the failure says what
went out (`AUTO_RESTORE`, `suites/common.py`). Identity, radio, ETM, WDP, device and `?MAP,PWM` tokens are never
touched — a replay there can reboot a board or persist a peer — and a leaked delimiter or command character stops the
auto-restore altogether. The test still fails: leaking is a test bug, but the bench is left clean.

A test that writes NaviCore's config runs inside `nc_guard` (`hil/nc_guard.py`, §5) the same way: it snapshots
NaviCore first and fails the test unless NaviCore's config ends byte-identical. The `nccfg` suite (`s40_navicore_config.py`) makes about 45 such saves a run, each rewriting NaviCore's `/config.json` (~14 KB). It changes only what has no live effect on this bench (tool metadata, mappings on matrix slots no SBUS value decodes, a disabled knob, a switch with no tiers, reports to WCB 0), never NaviCore's mesh password, WCB number or channel, and restarts NaviCore only under the `navicore_reboot` opt-in. `nccfg.boardtype_change_no_reboot` keeps a flipped boardType saved for a few seconds: NaviCore must not restart in that window.

Two things are deliberately never done, because they cannot be cleanly undone:

- **No test adds a local Maestro id while WDP is on.** A local id is advertised over WDP, every
  other board auto-adds a remote proxy for it, and proxies are never evicted (`CLAUDE.md` rule 5).
  The tests that add a local id, or clear and rebuild the slot table, send `?WDP,OFF` first — it
  stops both the board's adverts and its handling of other boards' adverts (`WCB_WDP.cpp:356`,
  `:488`) — and `?WDP,ON` only once the table is back. The one exception is `maestro.wdp_auto_add_beside_local`, whose subject is that auto-add: it clears the id on W2 first, then W1's proxy, and a POLL proves W1 does not learn it again. Remote proxies are never advertised (only
  local slots are, `WCB_WDP.cpp:142-167`), so adding and clearing one is restorable.
- **No test sends free text to the SBUS controller.** Outside a `{…}` line its serial parser
  treats single characters as commands, and `m` and `w` write flash. Nor a verb that saves: its frame format
  (`mode`) goes out only as the RAM-only INF8 form with `"save":false`, and only once the controller has answered the
  INF8 probe, since an image without the test verbs saves `mode` whatever it says (§5).

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
| `opt_in` | Tests that are skipped unless named here, because they wear flash or wipe a board's data: `nvs_wear` (fills W1's 100-variable table with persistent variables), `seq_wipe` (clears every W1 sequence, then replays them), `ota_erase` (OTA sessions, each erasing at least 4 KB of a board's inactive app slot), `ota_full` (full-image OTA on W1, two passes), `ota_full_wcb2` (the same, relayed to W2), `ota_wrong_chip` (an S3 image streamed to W1; attended), `navicore_clip` (replays a saved NaviCore clip: servo motion and recorded actions), `navicore_clip_write` (NaviCore takes recorded, saved, uploaded, renamed and deleted as `HIL<nonce>` clips on its clips partition, and replayed; one take runs 60 s to the recorder's backstop; each test fails unless `?REC,LS` ends as it began), `navicore_reboot` (restarts NaviCore in software: its USB re-enumerates, and the mesh and SBUS OUT lose it for about 5 s), `navicore_fault` (NaviCore's config-storage fault hooks `#L91`-`#L93`: a corrupted `/config.json`, an overflowing GET_CONFIG, a failed save; the tests skip on an image built without `NAVICORE_HIL_HOOKS`), `navicore_ota_erase` (4 KB `?OTALOCAL` sessions on NaviCore, ended by ABORT and by the 30 s idle reaper: each erases the head of its inactive app slot), `navicore_ota_full` (NaviCore re-flashed over USB with the bench image it runs, twice: two restarts, back on its slot), `navicore_ota_relay_full` (the same through W1's `?OTA` relay, ~12 min a pass; run by hand), `navicore_esptool` (watched: the recovery ladder's esptool rungs on the healthy NaviCore, writing the bench image into app0 and `boot_app0.bin` into otadata), `navicore_identity` (attended: a saved invalid NaviCore deviceId, written back over USB), `navicore_nvs` (NaviCore learns a permanent probe client as a mesh peer, forgets it with FORGET_PEER, learns it again and drops it when it comes back temporary: four writes of its learned-peer list to NVS; W1 and W2 learn and forget the probe too), `navicore_aux_tx` (routes an HCR, MP3 Trigger, DFPlayer or WLED to NaviCore's own S3-S5 for a few seconds and runs `#L20`/`#L21`: device-protocol bytes go out J4-J6, where nothing is recorded as attached; a hook image's `DBG_WIRE` log reads them back), `navicore_wifi` (a spare WiFi adapter on the PC joins NaviCore's access point for ~30 s to 3 minutes in each of fifteen `ncwifi` tests and six Intellex ones - `intellex.ws_transport_navicore` and `s35`'s - under a temporary profile with the SSID and password read from NaviCore's config; a PC whose only adapter carries its internet skips them; `ncwifi.ws_ping_soak` streams for 5 minutes; with `navicore_reboot` ticked too, two tests restart NaviCore and one may, D-NC62), `navicore_webserial` (attended: NaviCore's own port goes to Chrome over real Web Serial, `tests/wizard/.profiles/navicore`, whose first run asks someone to pick the port; the open restarts NaviCore, and `nctool.webserial_flash_same_image` runs the config tool's Full Wipe & Flash with esptool-js: the custom bootloader, the partition table and the bench image into 0x0, 0x8000 and app0, NVS and otadata erased, then a check that the config and clips came through), `intellex_reboot` (a NaviCore REBOOT sent through Intellex, over its own serial transport or, with `navicore_wifi` ticked too, over WiFi through NaviCore's access point: its USB port re-enumerates, and the mesh and SBUS OUT lose it for about 5 s; inside `nc_guard`), `intellex_nc_save` (the NaviCore config tool inside Intellex saves NaviCore's config twice, `chRateHz` one step away and back: two rewrites of its `/config.json`, inside `nc_guard`), `intellex_flash` (W2 re-flashed through Intellex's esptool path with the bench image it runs, from a seeded offline cache: the Wizard's Update, Flash and an Update with a second flash refused, and a full flash the host refuses; W2 is off the mesh about a minute a flash and ends on its version and config), `intellex_flash_factory` (attended: the Wizard's Factory Reset through Intellex erases W2's NVS, which the harness restores from W2's chain), `intellex_flash_navicore` (the config tool's Update Firmware through Intellex rewrites NaviCore's app0 with the bench image it runs and restarts it; inside `nc_guard`), `intellex_wifi_join` (attended: with Intellex attached to NaviCore's access point, the PC's spare adapter hops to W1's and back, and Intellex's own `wifi_bounce` disconnects and reconnects it), `sbus_reset` (reboots the SBUS controller), `wifi_modes` (W1's access point off and back, joining W2's, its access point up under its derived name, W2's access point lost and rejoined, an absent network looked for: up to ten W1 and two W2 reboots across its tests), `wifi_pc` (attended: a WiFi adapter on the PC joins W1's access point for ~30 s in each of six tests, the spare one when there is one), `mesh_password` (attended: W1 runs on a throwaway mesh password for a few seconds), `nvs_erase` (attended: erases all of W1's settings and restores them from its chain, three reboots per test), `nvs_fill` (boots W1 once on the sequence store's NVS fallback, `?DEBUG,SEQNVS`, fills its settings storage with throwaway sequences until one is refused, then removes them and boots back: two extra reboots per test). Two long measurements are opt-in for their length: `softrx_erratum` (`softrx.erratum_pairs`, ~15 min of soft-port input into W1 S3-S5; needs wcb_probe 4) and `w1s4_soak` (`soak.w1s4_wire`, W1's S2/S4 fan-out for `soak_minutes`), `etm_seq_wrap` (drives W1's ETM sequence counter past 65,535 with about 65,000 untracked JSON broadcasts typed on its console, its port broadcasts off meanwhile, then checks a unicast sent right after the wrap still runs on W2: several minutes of mesh flood, one W2 reboot). Every key, with its title, one line on what it does and a per-test time estimate, is registered in `hil/optin.py` (`OPT_INS`). The runner skips a gated test before it starts, with `opt-in: add "<key>" to bench.json "opt_in" (<why>)`, unless the key is here. The GUI's Tests tab has one checkbox per key (§4), which writes this list. |
| `soak_minutes` | How long `soak.w1s4_wire` runs, in minutes (default 20). |
| `no_servos` | `true` skips every test that can move a real servo, for a run with nobody watching the bench. `hil/servos.py` lists them by id, each with its reason, and names the tests it read and left out. On this bench the servos are Maestro 2 on W2 S1 and NaviCore's local Maestro 1, the dome's pie and panel servos. W1 holds an `M1:W20` proxy beside its local Maestro 1 on the W1 S1 probe, so every `;M1` subroutine or move verb typed on W1 also runs on the dome (`maestro.fanout_remote` proves the dispatch); `maestro.*` move tests are not probe-only. Also listed: Maestro 2 moves, NaviCore's `SET_MODE` (J2 moves), `TRIGGER` and clip replay, a stick on a bound channel, and every test that moves a controller channel at all, because NaviCore re-emits each channel on SBUS OUT (`sbusOutEnabled` is on) and nothing records what is wired there. The runner skips each one before it starts with `moves a servo (--no-servos)`, after a missing wire or device and before an opt-in. `run.py --no-servos` does the same for one run and leaves `bench.json` alone. The flag is kept in the run's checkpoint, so `--resume` and the GUI's *Resume* keep it, and `run.py --resume --no-servos` turns it on for the rest of a paused run. `run.py --list` tags these tests `[servo]`, or `[servo: skipped]` under the flag or the key. The key fails safe: any value but a missing key, `null`, `false`, `0` or a string saying no counts as on, so a hand-typed `"true"` cannot let a servo move. The GUI's *No moving servos* box (§4) writes it. A new test that can move a servo goes into `hil/servos.py`; `selftest.py` fails on a listed id that no suite registers. |
| `navicore_repo` | The NaviCore tree the config tool's board tests (`nctool.board_*`, `nctool.webserial_*`) serve, passed to them as `NAVICORE_REPO` (`hil/wizard.py` `navicore_repo_env`): the tree the bench's NaviCore image was built from, so the tool and the firmware under test are one pair. This bench sets the `hil-week` worktree. Unset, and for every no-board spec, the tool is served as it is on disk in the sibling NaviCore checkout (D-NC10). |
| `wifi_test_interface` | The WiFi adapter the tests that join an access point use (`hil/wlan.py` `pc_on_ap`: s28's `wifi_pc` tests, s45's `navicore_wifi` ones and Intellex's WiFi tests in s33 and s35), by its `netsh wlan show interfaces` name. Unset, a test takes an adapter that does not carry the default route, so the PC stays online; with only one adapter s28's tests use that one and the PC is offline for the test. NaviCore's tests never take the adapter that carries the default route, named here or not: they skip (`NAVICORE.md` D-NC14). |
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
esptool works around the same thing (`_setRTS`, esptool `reset.py`). `usb_jtag_reset` (`hil/serialdev.py`) pulses
this way, for `SbusCtl.reset_rts` and `NaviCore.hard_reset`. `sbus.signal_loss_controller_reset` uses it on the
controller, and reads the controller's boot record to prove the reset really happened.

---

## 3. The probe (`tests/hil/wcb_probe`)

Instrument firmware for a spare **WCB V2.4 board** (ESP32-PICO-V3-02). It is not WCB firmware.
The PC configures it at runtime, so **a new test almost never needs new probe firmware**. The exception is
timing between two lines finer than a USB round trip can place, which is what `TXSKEW` (below) is for. The harness
needs wcb_probe 5 or newer and refuses 6 outright (`PROBE_MIN_VERSION`, `PROBE_BAD_VERSIONS` in `hil/probe.py`);
`softrx.erratum_pairs` needs 4 and skips on an older probe, and `probe.soft_concurrent_rx`'s
two-soft-channel arm needs 6 (flash `tests/hil/wcb_probe`). The current version is **7**; a probe on 6 fails `probe.hello` and every bind
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
| **Tests** | Every test grouped by area with its result, time and what it needs. Double-click runs one test; *Run selected* runs a test or a whole area; *Run everything*; *Run what failed*; *Stop after this test*. Every skip, a missing wire included, is written to `session.log` with its reason. During a run, *Pause after this test* (top bar, next to *Stop*) becomes *Cancel pause* once pressed; Stop and Pause replace each other, and the last one pressed wins. When a paused or interrupted run exists, a banner above the table offers it: *Resume*, *Other runs… (n)* (a picker that also lists stopped runs), *Open its report* and *Discard*. When only stopped runs exist, the banner is a one-line notice with just *Other runs… (n)*. A test re-queued after a host outage shows RETRY in amber. **Expected** shows each test's expected time: `0:12` is the median of its last five real results (PASS, FAIL or ERROR, never a SKIP or a NOT A RESULT), `~15:00` an estimate from `hil/optin.py` (no real result yet, or `soak.w1s4_wire`, whose length follows `soak_minutes`), `?` not known, and a grey `opt-in off` for a gated test whose key is not ticked. The running test's cell counts up as `elapsed / expected`; *Time* keeps the actual time of a finished test. An area row shows its sum, and *Run everything (~2 h 05 min)* and *Run selected (~12 min)* show the estimate before you start. A test the runner skips at once (a missing wire or device, or its opt-in off) counts 0 in every sum, and `+?` marks a sum with an unknown test in it. **Opt-in tests** (above the table, folded by default because open it takes most of the table's height; *Show* unfolds it) has one checkbox per `hil/optin.py` key, with its title, what it adds to a full run (`+15:40, 1 test`), and what it does. Its header line, shown folded too, says how many are on and what they add to *Run everything*, counting 0 for a test the runner would skip for a missing wire or device. Ticking one writes `bench.json` `opt_in` through the worker (atomically, every other key kept in its order). It re-reads `bench.json` first, so an edit made by hand while the GUI is open is kept rather than overwritten; if the file does not parse, nothing is saved and the box goes back. It also refuses, putting the box back, while a run is live in another window or terminal (its `run.lock` held): that run recorded `opt_in` at its start, and a change would turn its automatic outage recovery into a pause. The checkboxes are disabled while a run or a resume is going, because the runner reads `opt_in` before each test. *No moving servos*, on the same header line and so shown folded too, writes `bench.json` `no_servos` (§2) the same way, with the same refusals, and is disabled during a run for the same reason. With it on, or during a run started with `run.py --no-servos`, each test in `hil/servos.py` shows a grey `no servos` and counts 0 in every sum, and a selected test's details say why it moves a servo. |
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
  160 bytes and drops empty ones. A session nobody stops goes on for good (it has no lease): a board's own
  `?backup` is read past `[TERM:<n>]` lines (`WCB.backup_chain`), and a test that starts one stops it in its
  `finally`.
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
  `NaviCore.cli()` sends it after every CLI line unless told not to. **Whoever sends a poke waits for its own
  `Mode=` reply** (`NaviCore._eat_poke`) even after the line it wanted arrived: a reply left behind lands after the
  caller returns and ends the next `cli()`'s wait for *its* `Mode=` line early. That returned `#L13` with no dump
  (run `20260928-212848`), so `sbus_dump()` and `cli(until=...)` consume theirs.
- **NaviCore's `#L09` fps is a one-second average.** A stall just before a reading, such as the previous test's
  `nc_guard` restore saving the config, reads low for that window. A full-rate gate reads through
  `NaviCore.sbus_full_rate()`, which re-reads for up to 4 s. The first reading alone skipped 17 `s42` tests at 42 and
  15 fps between readings of 111.
- **NaviCore and the SBUS controller** are driven through `hil/navicore.py` `NaviCore(dev)` and `hil/sbus.py`
  `SbusCtl(dev)` (`docs/hil_plan/NAVICORE.md` INF1, INF2), never hand-built JSON. The NaviCore driver covers the
  JSON protocol (`ack`, `config`, `set_config` paced with its saveId, the command library by size and FNV-1a hash,
  `trigger`, `test_action`, `wcb_send`, `monitor`), the CLI (`?MAE` queries, `#L09`/`#L13`, `?REC` including ranged
  clip download and indexed upload), the mesh views (`?WDP,DUMP`, `GET_MESH_STATS`, `?backup` with `?EPASS` hashed,
  sequence pulls, `version_surfaces`) and restarts (`reboot`, `wait_boot`, `hard_reset`). `SbusCtl` sends only JSON
  lines (`m` and `w` save to flash) and has no method for the verbs that save (`cfg`, `wificfg`, and `mode` without
  `"save":false`), and its waits for a reply ping every second (§6). The RAM-only test verbs of SBUSController's
  `hil-week` image (NAVICORE.md INF8) are methods too - `flags`, `stream` (with a frame budget), `sbus16`, `glitch`,
  `channel` - probed with `test_state()` (None on an image without them, which ignores them) and put back as a reset
  would with `clear_faults()`; none goes out before a probe answered. `ReaderModel` is NaviCore's SBUS framing byte for
  byte (`sbus_reader.h`), so a test knows what a burst should decode to (`glitch_bursts`, `reader_decodes`). `Bench.log` passes every line NaviCore and the controller send or receive
  through `checkpoint.redact_text` (`runner.REDACT_KINDS`), so session.log and the GUI show their passwords, `?EPASS`
  values and `wifiNets` only as `<redacted:sha12>`; a WCB's lines are left as they are. `hil/sbus.py` also packs and unpacks SBUS frames (`encode`, `decode`: 25 bytes for SBUS-16, 36 for
  SBUS-24). No driver message quotes a config line or a secret; `selftest.py` feeds every parser lines in the
  firmware's own formats.
- **NaviCore config writes** go inside `with nc_guard(bench) as g:` (`hil/nc_guard.py`; `docs/hil_plan/NAVICORE.md`
  INF3). The guard takes the snapshot when the block starts: GET_CONFIG's exact text (`g.before_text`) and dict
  (`g.before`), the command library, the `?REC,LS` clip names, the learned peers and the mode. The block writes through
  `g.nc`. At its end the guard restores, and proves the config byte-identical. If GET_CONFIG already equals the
  snapshot, it writes nothing. Otherwise it sends the snapshot plus explicit clears: `{}` for each new mapping key,
  `"channels": []` for each Maestro slot that had none, `"serialLabels": {}` when there were none. A plain re-send
  cannot undo those three, because SET_CONFIG leaves alone what it does not name and GET_CONFIG leaves out what is
  empty. RESET_DEFAULTS then the snapshot's exact text comes only if that read back different, because RESET_DEFAULTS
  swaps the live mesh password until the snapshot lands. It then puts back the command library, deletes the `HIL*`
  clips the test added, clears the debug flags, the monitor and CALIB, re-learns a lost learned peer with two
  `?WDP,POLL` from W1, and sets the mode back with a mesh SET_MODE. A config that will not come back fails the test
  with `NAVICORE CONFIG NOT RESTORED — <key paths>`, after the block's own failure if it had one; anything else left
  over fails it with `NAVICORE STATE NOT RESTORED — ...`. Credentials appear only as `<redacted:sha12>`
  (`checkpoint.redacted_diff`). The snapshot is written to `navicore_snapshot.json` before the block runs, so a test
  killed inside it is restored by the resume (§9). The first snapshot ever taken is also kept, never overwritten, as
  `results/navicore_config_week_start.json`. A second Ctrl+C skips the restore and leaves it to the resume. Guards
  do not nest. `nccfg.guard_selftest` (`suites/s40_navicore_config.py`) proves the guard on the bench before any other
  test writes NaviCore.
- **Building, flashing and recovering NaviCore** go through `hil/ncflash.py` (`docs/hil_plan/NAVICORE.md` INF4), never
  `arduino-cli upload` or a hand-typed esptool line: a cold power-up can leave the S3 in ROM download mode, and an
  upload replaces NaviCore's custom short-watchdog bootloader.
  - `build(tag, hooks=False)` compiles NaviCore's working tree into `results/builds/navicore-<tag>`, which is also the
    build path, so the IDE's sketch cache is never used. It uses NaviCore's FQBN and refuses once `NaviCore/CLAUDE.md`
    no longer names it; it refuses a core other than esp32 3.3.4, and a sketchbook `WCB_Client` or `WcbCmd` (the copies
    every local compile uses) that differs from the WCBClient or WcbCmd repo, unless `allow_drift=True`, which records
    it. Line endings alone are not a difference. One build runs at a time, at below-normal priority, about two
    minutes. The folder gets `BUILD.json` (NaviCore's commit, whether its tree was dirty and a fingerprint of the
    change, both libraries, the image check) and `compile.log`. `hooks=True` adds `-DNAVICORE_HIL_HOOKS=1` (INF9).
    `source=` compiles another NaviCore tree than the checkout beside this repo, such as a git worktree of it on a
    branch. arduino-cli refuses a sketch folder not named after its main `.ino`, so a folder with another name is
    compiled from a copy of its sketch files alone (`stage_sketch`: the top-level code files, `partitions.csv`, `src/`),
    removed afterwards; the FQBN and version checks, the hooks scan and `BUILD.json`'s tree state all read the source
    itself, and FLASHED.md's line names its branch when that is not main.
  - `check_image(folder)` checks an image as the board will, before anything is sent: the magic, chip id 9, every
    segment, the checksum and appended SHA-256 that `esp_ota_end` verifies, exactly one FW_VERSION string, the ELF
    beside it hashing to the SHA-256 at image offset 0xB0 (the image's identity: the version string only changes on a
    NaviCore commit), and the size against the OTA slot in `partitions.csv` (0x1E0000).
  - `flash(nc, folder, what)` checks the image, PINGs (a NaviCore version, or nothing is sent), reads
    `?OTALOCAL,STATUS` (chip family 1, a Next slot the image fits, no other session streaming), then streams the image
    over NaviCore's own USB protocol one 1024-byte chunk at a time, paced as the config tool writes. An ACK naming the
    chunk's end moves on; a NAK at the chunk's own offset sends it again; anything else is settled by
    `?OTALOCAL,STATUS`. While it waits it writes a lone newline every 0.25 s, which NaviCore ignores but which releases
    a reply its USB is holding. After `[OTA:END,OK]` NaviCore restarts: `flash` waits for it (`NaviCore.wait_boot`),
    and STATUS must then show the other slot running with the image's version, and its `App SHA256` once the firmware
    prints one. About a minute for the 1.17 MB image. A failure before END leaves the running app: `FlashError` says
    the stage and offset where it stopped and what STATUS showed afterwards. `NotBack` means END was accepted but
    NaviCore did not come back; `recover` is next. session.log shows each DATA line as its offset and length, not the
    base64.
  - `recover(nc, allow_esptool=False)` is the ladder: PING; the USB-Serial/JTAG reset (`NaviCore.hard_reset`); then,
    only with `allow_esptool=True`, esptool from the core: a read-only probe that leaves ROM download mode by the RTC
    watchdog, and last the known-good image (`results/builds/navicore`) written into app0, with the core's
    `boot_app0.bin` into otadata, in one connection with `--after watchdog-reset`. It never writes 0x0 or 0x8000, and
    closes the harness's port while esptool holds it. Without the flag it stops after the reset and names both esptool
    commands.
  - `BENCH_IMAGE` (`navicore-hil1`, D45) names the week's image. A test that flashes NaviCore writes only it, only
    while NaviCore runs it, and ends on it, proven by the App SHA256 it prints; `ncota.image_identity` fails when
    the board runs anything else. `builds_with_sha(sha)` finds the build folders carrying a board's App SHA256,
    `flash_rows()` and `last_written()` read this module's FLASHED.md table, and `put_back()` returns the commands
    that put the bench image back, for a failure message.
  - Every flash that sent BEGIN, and every esptool write, adds a row to `results/builds/FLASHED.md` under "NaviCore
    flashes (hil/ncflash.py)": when, the folder, the ELF SHA-256, NaviCore's commit and dirty flag, how, the result and
    what is in it.
  - Outside a run, from `tests/hil`: `python -m hil.ncflash build <tag> [--hooks] [--source <NaviCore tree>]`,
    `check <folder>`, `libs`, `status`, `reset`
    (the ladder's second rung alone, to prove it before it is needed), `flash <folder> --what "<text>"` and
    `recover [--allow-esptool]`. The board commands open `bench.json`'s NaviCore
    port with DTR and RTS low, refuse while a run holds its `run.lock`, and log every line to
    `results/builds/ncflash-logs/` with passwords and the SoftAP name hashed.
- **NaviCore over the mesh** goes through `hil/ncmesh.py` (`docs/hil_plan/NAVICORE.md` INF6). `bridged(w1, obj,
  pattern)` types `;W20,<json>` on W1 (one line, at most 187 UTF-8 bytes) and returns `Reply(match, lines, sys)`;
  silence is returned (`match` None), not raised. The send opens W1's 20 s relay window, the only time W1 prints
  NaviCore's JSON. `fragments(payload, sid)` cuts a longer payload exactly as the config tool's `_fragChunks` does
  (`{"f","of","sid","s"}` envelopes of at most 187 escaped bytes, at most 192 parts); `send_fragments` sends them
  with the tool's pacing, in any order, repeats allowed; `reassemble` joins NaviCore's fragmented replies, which carry
  no `"sys"`. `with deaf(bench, n):` stops W<n> hearing the mesh by flipping its third MAC octet (it still transmits, heartbeats included, until its next boot) on its own USB console
  and flips it back however the block ends; a board with no console of its own is refused, and one that will not
  take the octet back fails the test with the command to type. `with probe_peer(bench, id) as probe:` is
  `probe_in_mesh` with quantity 20 plus a burn of NaviCore's duplicate window (§6); it refuses an id NaviCore
  knows, and a NaviCore with serialBcast out on. The burn ends by setting NaviCore's debug flags to 0, so a test that reads `[WCB RX]` lines sets
  DBG_MAESTRO inside the `probe_peer` block, not around it.
- **NaviCore's device transports and its Maestro** (`suites/s44_navicore_devices.py`; `docs/hil_plan/NAVICORE.md`
  NC-WP7) read bytes in four places. Remote Maestro slot 4's frames reach W1 S1 and the W2 S1 tap through both WCBs'
  Maestro_Remote forward (s44 `Wires`, `dev_stream`); the Kyber broadcast is unacknowledged, so a step the hook image's
  `[WIRE] WCBStream` copy shows NaviCore wrote is sent once more before a miss counts. Devices through a WCB land on
  W2's probe-wired ports under s15's fixtures (`_hcr_w2` S4, `_w2_audio` S5) or the bench's WLED 1 on S2, compared with
  the byte tables s15 and s09 assert for the same verbs typed on W1. Bytes on NaviCore's own S3-S5, Serial2 and
  WCBStream come only from a `NAVICORE_HIL_HOOKS` image's `DBG_WIRE` log (`wire_log` reports a lost log line as such,
  never as a wrong byte); routing a device to those ports is opt-in `navicore_aux_tx`. NaviCore's own Maestro 1 is read
  back with `?MAE,GET/MOVING/ERR` on `NaviCore.undriven_channel` (servo-listed). Every config save re-applies easing and
  repeats it twice 500 ms apart on every slot (§6), so a test that counts Kyber packets or Maestro-bus bytes waits 1.5 s
  after a save or keeps to its own device and channel.
- **NaviCore on the mesh** (`suites/s43_navicore_mesh.py`, `ncmesh.*`; `docs/hil_plan/NAVICORE.md` NC-WP6). Nothing
  that can carry a credential crosses W1, whose console lines reach session.log unredacted: no GET_CONFIG and no
  `?backup` over the bridge or RTERM, and no stored-sequence value the test did not write. A probe id comes from s43
  `_free_id` (the first candidate NaviCore's GET_WCB_STATUS does not know). To take a WCB off NaviCore's roster a
  test stretches that board's own `?ETM,HB` (`online_tracking_flip`, restored in a finally), because `deaf` leaves
  it heartbeating. `hil/ncmesh.py` predicts what NaviCore prints and advertises: `rterm_pieces` (the 160-byte RTERM
  wrap and the relay's empty-packet drop), `wdp_scrub` and `json_strip` (the two ways it cleans external text),
  `port_labels` and `local_maestro_ids` (its WDP labels and Maestro ids from GET_CONFIG), `status_rows`
  (WCB_STATUS, positional or sparse), `stats_rows` (a WCB's `?STATS` rows) and `bulk_frames` (a bulk `bb`/`bc`/`bd`
  transfer). Each test undoes its own WCB writes in a finally (s43 `_put_back`, `_seq_clear`): config_guard fails
  a test whose board does not end as it began, even when it puts the token back itself. A transfer's effect on
  SBUS is judged by s43 `_sbus_kept_up` (the frame counter across it, and lost/failsafe), not by `#L09`'s
  one-second fps (§6).
- **NaviCore's RC engine** (`suites/s41_navicore_engine.py`, `s42_navicore_sbus_engine.py`; `docs/hil_plan/NAVICORE.md`
  NC-WP4 and NC-WP5) is watched through two windows that move nothing. Actions aimed at W1 S2 (s41 `_act`, a
  `wcb_unicast` of `;S2HIL<tag>`) land on the W1S2 probe and are timed on the probe clock (`_probe_ms`). NaviCore's
  remote Maestro slot 4 (device 4, which nothing hosts; `_remote_slot`) reaches W1 S1 through W1's Maestro_Remote
  forward as raw Pololu frames (s42 `pololu_frames`, `Watch11`, which quotes the hook image's `DBG_WIRE` copy on a
  mismatch). The controller's rx and ry sticks, which NaviCore binds to nothing, are the SBUS inputs, set to exact
  counts (`Sticks`, `axis_arg`, the inverse of the controller's `axisToSbusRange`; `#L09` confirms). A test that
  stalls NaviCore's `loop()` past ~100 ms (INF9's `#L90`) first calls s41 `_engine_inert` inside `nc_guard` (every
  band 0/0, no mode function, every knob off), because the stall can overflow the SBUS UART (§6).
- **A test that restarts NaviCore** (`suites/s46_navicore_boot.py`, `s47_navicore_ota.py`) is behind
  `navicore_reboot` or a stronger opt-in, skips while `NaviCore.restart_blocker()` names recorder state a restart
  would lose (D33), and runs inside `nc_guard` even when it writes nothing. The restart is proven by NaviCore's
  uptime (GET_MESH_STATS `upMs` below the time since the command), not by the banner. A test that saves a value only
  a restart applies (boardType, the mesh quantity or deviceId) writes the snapshot back and restarts NaviCore onto
  it however its body ended (s46 `_put_back`). The boot banner names NaviCore's AP: `redact_text` hashes its
  `SoftAP "<name>"` line in session.log and in failure details.
- **NaviCore's access point and WebSocket** (`suites/s45_navicore_wifi.py`, `ncwifi.*`; `docs/hil_plan/NAVICORE.md`
  INF5, NC-WP8). The PC's side is `hil/wlan.py`, shared with s28: `with pc_on_ap(bench, problems, ssid, pw, whose,
  spare_only=True) as adapter:` joins under a temporary `HIL-<ssid>` profile, waits up to `ADDR_WAIT_S` for the lease
  (tracker #110), and puts the adapter back; `spare_only` skips rather than take the adapter carrying the default route
  (D-NC14). `networks(adapter)` lists what a fresh scan sees (WlanScan, then netsh). s45 `_on_ap` reads NaviCore's
  SSID and password from GET_CONFIG and hands them only to that profile. `NcWs(ip, name, log=bench.log)`
  (`hil/ncws.py`) is the socket as a line device with SerialDevice's `mark`/`since`/`expect`/`lines`, so
  `NaviCore(NcWs(...))` runs the INF1 driver over WiFi and `pull_config(ncws, 2)` pulls through it; it logs through
  `redact_text` itself, and a failed `expect` quotes no CONFIG line. The socket is a console mirror: every line
  NaviCore's loop prints goes to every client and to USB, so a read on one transport follows a barrier there (s45
  `_sync`: an unowned `?HILB<nonce>` line, whose `Unknown command:` echo NaviCore prints, then the `#L12` poke).
- **NaviCore's recorder** (`suites/s48_navicore_rec.py`, `ncrec.*`; `docs/hil_plan/NAVICORE.md` NC-WP12). A test
  holds the recorder through s48 `rec_guard`: it skips unless `?REC,INFO` shows it idle and empty (its one buffer may
  hold a take someone wants, D33), hands out clip names `HIL<tag><nonce>` (`rg.name`) and deletes the ones it made,
  empties the buffer (`?REC,STOP`, `?REC,EDITCANCEL`, `?REC,CLEAR`) and PINGs off a CALIB, and fails when `?REC,LS` does
  not end as it began: a clip gone or changed, or one the test never named. So a take starts with a record action
  carrying a HIL name (s48 `_rec_start`, a TEST_ACTION), never `?REC,START`, whose take has no name and is saved as the
  next free `rec_<N>` by any stop action, record toggle or the 60 s backstop; and a take not meant for flash ends with
  `?REC,STOP`, which never saves. A RAM take is replayed with `?REC,PLAY` and read with the bare `?REC,EDITLOAD` (s48
  `parse_resident`); only a saved or uploaded clip is behind `navicore_clip_write`. Every replay resets and re-targets
  every channel NaviCore has moved since boot (D-NC64), so every test that replays is in `hil/servos.py`, and a keyframe
  test judges only its own channel's frames.
- **NaviCore's SBUS faults** (`suites/s50_navicore_sbus_faults.py`, `sbus.*`; `docs/hil_plan/NAVICORE.md` NC-WP11) need
  the controller's INF8 verbs, and every test asks for them before anything else and skips, naming the image, without
  them. A glitch case stops the stream (s50 `_quiet`), sends one burst, then lets good frames through one at a time
  (`stream(True, frames=1)`), reading #L09's frame counter and #L13 after each: with the channels at rest every frame
  the controller sends is the same bytes, so anything else in #L13 is a frame nobody sent. Around a burst that could
  decode garbage NaviCore's engine is inert (s41 `_engine_inert`); around SBUS-16 its switches and knobs on CH17-24,
  which read 992 meanwhile, are unbound (s50 `_hi_inert`); both inside `nc_guard`. rc_trig is read through s42
  `_taps`, which gives `(host time, tap)` for one (mode, slot) - s41 `_rc_trigs` keeps `(mode, btn, tap)`. Every test
  ends in s50 `_put_back`:
  the test state as a reset leaves it, the sticks centred (a frame-format change resets them to 992), and every
  channel NaviCore reads as it was at the start.
- **Intellex under its venv.** `run_intellex_py(bench, id, script, args=, device=)` runs `tests/intellex/py/<script>`
  under Intellex's venv against a staged copy, leashed (offline, no discovery host, `INTELLEX_SERIAL_ALLOW` naming the
  device's port or nothing), and judges its JSON results file (`judge_py`: every failed case named; all skipped is a
  Skip). A script registers cases with `_ixpy.case(name, group=)` and `args["group"]` picks them, so one script serves
  several ids. Messages carry lengths, counts, key names and the test's own markers, never a config value.
- **A board behind Intellex.** `with handed_over(bench, device) as port:` releases the port and takes it back
  afterwards, checking the board answers (`_reacquire`); `with attached_host(bench, id, device) as host:` stages a host
  allowed that port alone and attaches it. `LinkTap(host.port)` is a raw `/_link` page on its own thread (answers the
  heartbeat, keeps every byte, never logs them): compare two taps with `window()` between a sync line both saw and an
  end line, or between the k-th and next of a repeated line (`line_spans`, NaviCore's PONG), by length and SHA-256
  only.
- **Proving nothing reset a board** takes more than its own stream, which a reset during a closed port never
  reaches: W2's `[ETM] WCB1 came ONLINE (boot)` (one per boot announce it hears), W1's for WCB 20, and NaviCore's
  `GET_MESH_STATS` `upMs`.
- **A tool page through Intellex on a board.** `run_intellex_test(bench, id, attach={"kind": "serial", "port": <port>},
  device=<name>, link_check=, recover=)` hands the board's port to a leashed host and runs the spec, whose page
  connects on its own (the shim replaces the port picker: no Chrome profile). `link_check` gets every byte the host fanned
  out, read by a raw `/_link` client of the harness's own (the rawLink), and its problems fail the test:
  `boot_check(markers, who, boots_of=)` for a boot line of the board's, or - through a WCB's console - `[ETM] WCB<n>
  came ONLINE (boot)` of another. `recover` gets a board that does not answer afterwards (`reset_into_app`: an EN pulse
  with GPIO0 high, for a WCB left in its ROM loader) and the test fails naming both. Specs use
  `tests/intellex/lib/board.js`: `boardGuard` (every route past the host answered, `/_api/signals` let through and
  recorded), `watchLink` (per line a page wrote, its JSON type or first word, and for a `?MGMT,FRAG` its target and
  inner command; first-seen times of the markers it read; never text), and the Wizard's and the config tool's state by
  name. `seed=seed(stage)` fills the stage before the host starts (a firmware cache); `hide=` names network names the
  host's lines and log files never carry (`IntellexHost` scrubs what it keeps and logs, `copy_logs` the stage's and the
  copied logs).
- **A step the spec must time with the bench** is a hook: `run_intellex_test(hooks={name: fn(host, body)})`, called by
  the spec as `hil.hook(name, body)` through the bridge's `/hook` route (one at a time, under the bridge's lock; a Skip
  reaches the spec as `{skip}`). The harness's test thread is parked while Playwright runs, so moving the PC's WiFi
  adapter, marking and counting a console over a window the page defines, and judging the host's flash log are hooks
  (`s35`, `s36`).
- **Intellex over WiFi** (`suites/s35_intellex_wifi.py`; `intellex.ws_transport_navicore` too): each test puts the PC's
  spare adapter on NaviCore's access point for its length (`s45` `_on_ap`, `hil/wlan.py` `pc_on_ap`) behind
  `navicore_wifi`, and attaches a host allowed no COM port to `{kind: ws, host: 192.168.4.1, role: navicore}`. A spec
  never gets a network name: the host's lines and logs are scrubbed (`hide`), `/_api/status` reaches it through
  `status_view`, and a venv script gets a SHA-256 of the SSID. The Wizard through NaviCore arms every WCB's remote
  terminal: stop them all afterwards (`s35` `_stop_rterm_all`).
- **After an access point's board restarts, connected with a lease is not back.** NaviCore's REBOOT deauthenticates
  nobody, and Windows keeps the old association: in run `20260929-202852` the spare adapter read connected, with its
  old lease, for 90 s with no WLAN AutoConfig event at all, while the restarted access point dropped its frames.
  `hil/wlan.py` `rejoin(reach=(host, port))` wants a TCP connect there within 10 s (`reach_wait`) and otherwise
  re-associates the adapter (netsh disconnect, then the temporary profile connected again, the adapter named); a test
  times whatever must follow from that proof. An adapter Windows did drop is connected again the same way (the
  temporary profile is manual).
- **A far end that vanishes, with no board:** `hil/ws.py` `WsEndpoint` is a stand-in `/ws` endpoint on a loopback
  address (s32 uses 127.0.0.2:80) that answers pings and a PING line, and on `go_silent()` closes its listener and
  falls silent on the open sockets, kept open: no FIN, no close frame, as a restarting access point.
- **Flashing through Intellex** (`suites/s36_intellex_flash.py`): W2 only, with the bench image it runs, read from
  `results/builds/wcb-esp32-meshq` (`hil/intellex.py` `builds_dir`: the main checkout's from a worktree) and seeded as
  `WCB_<version>_hilbench_ESP32*.bin` for branch `hil-bench`, the other product's branch `hil-none` (nothing cached).
  A test reads W2's version first and skips unless the image carries it, and releases the probe channels on W2's ports
  before esptool resets it (`s36` `park_probes`: bound, probe2's TX lines kept W2 out of its ROM loader in full run
  `20260929-203948`); afterwards W2 answers that version, runs app0
  and was heard booting by W1, inside `config_guard` on every WCB. The spec has the harness judge the host's flash log
  (`s36` `flash_log_problems`: the regions esptool wrote, each verified). A W2 that does not answer gets
  `reset_into_app`, then the image flashed again through the host's own API (`reflash_w2`). NaviCore's app flash runs in
  `nc_guard`, recovers through `ncflash.recover` and records a FLASHED.md row.
- **The Wizard through Intellex pulls the whole mesh.** Its shim routes every WCB that W1 hears through W1 and arms its
  remote terminal, whatever the test is about (`routeMeshThroughBoard`): guard every WCB with `config_guard`, and stop
  every other WCB's `?RTERM` afterwards (`s34` `_stop_rterm`).
- **The NaviCore config tool's connect writes nothing to NaviCore's flash** (the command library is compared, never
  synced). A spec that only connects fails on any JSON the tool sends outside `NC_READ_ONLY` instead of running in
  `nc_guard`, and the harness puts the monitor and debug flags back (`s34` `_nc_quiet`); a test that saves runs in
  `nc_guard`.
- **Intellex seed data** comes from `tests/intellex/fixtures` (a crafted wiki, `seed_wiki`; esptool 5.3.1-shaped
  flash-id output; a fake esptool package) or is written into the stage (`seed_firmware(stage, product, branch,
  files)`). Keep names short: stage paths come close to Windows' 260-character limit, and a git worktree adds about 42.
- **Opt-in tests** declare the gate on the decorator: `@test(..., opt_in="ota_full")`, and optionally
  `opt_in_why="..."` when this test's reason differs from the key's default. The key must be in `hil/optin.py`
  `OPT_INS` (title, one line on what it does, the default reason, `estimate_s` per test); an unknown key fails at
  import. A new kind of opt-in adds its entry there, and the GUI grows a checkbox for it. The runner skips the test
  before it starts unless `bench.json` `opt_in` names the key, so the body holds no check of its own.
- **A test that can move a real servo** goes into `hil/servos.py` `SERVO_TESTS` by id, with one line saying how.
  That covers any `;M1` move typed on W1, which W1's `M1:W20` proxy also sends to NaviCore's dome Maestro, and
  anything that moves an SBUS channel. The suites carry no marker of their own, so a test left out there moves
  its servo in a `--no-servos` run (§2 `no_servos`).
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
  reuses one and first sends 17 no-op commands (`_burn_ring`): the ring holds 16 seqs per sender
  (`ETM_SEQ_HISTORY`, `WCB.ino:748`), and 17 always push a stale ring out.
- **Changing the USB rate.** `dev.set_baud(n)` reconfigures the open port in place, leaving DTR/RTS alone. The
  OTA tests switch only after the board's `[OTA:BAUD,OK,<rate>]` plus a margin, and never use `WCB.run()`
  across a switch: its echo would cross it.

---

## 6. Firmware behaviour the harness is shaped around

Each of these is current firmware behaviour that a test, or a tool, walks into.

| Behaviour | Where | What the harness does |
|---|---|---|
| A config pull is ignored if the same requester's previous pull was accepted under 1500 ms ago, or while that requester's reply is still being sent; another requester's pull is parked (up to 5 s) and answered when the reply ends. A genuine re-pull that soon gets no reply and simply times out. | `handleConfigReqPacket` (`WCB.ino`) | `hil.wcb` spaces pulls to the same board `PULL_SPACING_S` (1.7 s) from the end of the previous reply. |
| A `,P` pull can get `[MGMT:CFGERR,n]NOPARTS` when every type-19 copy of its request was lost while a type-5 copy got through. `?DEBUG,PULLFAULT,OOM` is spent by the next accepted request, whatever its answer. | `handleMgmtPullRequest`, `cpjStart` (`WCB.ino`); [MGMT_RELAY.md](MGMT_RELAY.md) | `Pull` re-sends a `,P` pull once on NOPARTS; the OOM tests re-arm the fault if their first answer was NOPARTS. `navicore.pull_over_limit` turns `?DEBUG,MGMT` on W2 to see whether NaviCore's library sent a parts request. |
| Any NVS write on a classic ESP32 holds off S3-S5 soft-serial receive (the GPIO ISR service is not in IRAM), so a line arriving during it loses bytes. A WDP-DA confirm or forget schedules its list save 1 s later. | `wdpDaMarkDirty` (`WCB_WDP.cpp`); the ISR install in `setup()` | The WDP-DA forget helpers (`s12`) wait 1.5 s after a forget, so the save lands in the cleanup of the test that caused it. |
| A deferred restart (`?reboot`, a PWM reboot) waits for 4 s with no command processed. Every queue item resets that except a store-only `?STATS,RPT` and a `?RTERM,START` re-arm of the session already running to that relay; a re-arm to a different relay (two terminal clients) does reset it. Whatever arrives, the restart goes 20 s after the request, after an ungated `Restart held off <n> s ...` line (tracker #93). | `PWM_REBOOT_QUIET_MS`, `quietWindowExempt`, `RESTART_MAX_DEFER_MS`, the restart gate in `loop()` (`WCB.ino`) | `WCB.reboot` waits 25 s and notes the delay, and whether the cap was reached. `etm.reboot_rterm_rearm`, `etm.reboot_stats_rpt` and `etm.reboot_defer_cap` check both sides. |
| Soft-serial receive at 57600 loses about 1% of lines in back-to-back bursts (about 8.7 us of ISR-latency margin); 19200 and 38400 are exact. | vendored EspSoftwareSerial | `input.soft_rx_baud_sweep` requires 20/20 at 19200 and 38400 and at least 18/20 at 57600. |
| A WCB that has just booted sees every peer offline until that peer's next packet, up to HB+1 s: nothing answers a boot announce. Until then `?ETM,CHAR` aborts (`no online peers`) and a unicast to the peer is not ETM-retried (`HIL_TEST_AUDIT.md` F22). | the heartbeat branch of `espNowReceiveCallback`, `etmAddToPendingTable`, `processETMChar` (`WCB.ino`) | `_char` (`s99`) sends `?WDP,POLL` and waits until `?STATS` shows every bench WCB online; the reboot tests end the same way, so the bench is left as found. |
| A WCB_Client host (NaviCore) that has just booted has an empty WDP table until each WCB's next advert, about a minute away; it sees them online over ETM much sooner. | WDP advert interval (`WCB_WDP.cpp`); WCB_Client `_handleWdpAdvert` | `navicore.wdp` sends one `?WDP,POLL` from W1 when a row is missing and waits up to 15 s, noting it (run `20260925-091104`: NaviCore up about 27 s, no rows). |
| `?backup` prints each chain's section header, then builds the chain through a whole config pass before writing it, so a line printed on the WiFi task (`[ETM] WCBn came ONLINE`, F18) can land between the two. A chain over the 2 KB write buffer leaves in pieces, and the line can land between those too. | `printBackupConfig`, `espNowReceiveCallback` (`WCB.ino`) | `backup_chain_lines` (`s03`) searches each section for its checksummed line; `_factory_reply` re-reads a chain that fails its CRC, up to 3 times. |
| `Serial.println` is two writes (text, then CRLF), so a line another task prints (the WiFi task's `[ETM] WCBn came ONLINE`, F18) can glue onto the end of any println'd line, including the `;S0` echo `WCB.run` ends every read with (run `20260925-092255`). | the echo in `processSerialMessage` (`WCB.ino`); the core's `Print::println` | `WCB.run` accepts a sentinel that starts its line (the `^` stays: with `?DEBUG` on the token also appears mid-line); `_peers_online` waits 1 s after `?WDP,POLL`, whose adverts print exactly those lines; the reboot tests' cleanup never raises. |
| PWM passthrough re-sends a steady input now and then: an input reading 6 us or more off (capture jitter) is a change, and the stability pulse follows it (a third pulse in a steady 3 s in 11 of 30 runs). | `shouldTransmitPWM_Port` (`WCB_PWM.cpp`) | The probe reports in 200 ms windows that straddle a mark, so such a pulse can be counted against the next step. `pwm.passthrough_local`'s filter check ignores a pulse at the previous step's width; the step checks need a right pulse and a right LAST pulse (`_step_problem`). |
| Every line from a device is stamped with the host clock when SerialDevice splits it out of a read. Alone in its read, a line is good to one Windows timer tick (~16 ms). With more output behind it in the same read, it is stamped when that read ends, up to one read (60-95 ms) late. | `hil/serialdev.py` (50 ms read timeout) | Timing bounds leave room for one read: `etm.offline_detection_timing`'s floor is 4.85 s for a firmware floor of 5 s. |
| A `?` command whose body ends in `?` prints help and does not run. | `WCB.ino:4932-4937` | `wcb.help_trap` checks it changes nothing. |
| `;W<n>,a^b` sends only `a` to board n; the sender splits the chain and runs `b` locally. | `WCB.ino:2368-2388` | Remote chains go through `?MGMT,FRAG`. |
| Maestro **query** verbs (`get*`) are served by a single slot, local port first (of two local slots, the later one in slot order, `WCB_Maestro.cpp:546`); they do not fan out. **Move** verbs fan out to every slot with that id. | `WCB_Maestro.cpp:294-296`, `:413-418`; `:304-325` | `maestro.fanout_remote` uses `;M11`, not a query. |
| WcbMaestro range-checks against the Pololu protocol (channel and device 0–127, 14-bit values, accel 0–255), not against a particular Maestro's channel count. | WcbCmd `WcbMaestro.cpp:35-63` | `maestro.bad_verb` uses out-of-protocol values. |
| `;S<n>` appends CR (0x0D) only; WLED JSON is newline-terminated. | `WCB.ino:2000-2003`; WcbCmd `WcbWled.cpp` `emit()` | Byte assertions include the terminator. |
| Under a `?ETM,CHKSM` mismatch the target ACKs **before** its CRC check and then discards, so `?STATS` shows the command delivered while nothing ran. More generally an ETM ACK never proves execution. | `WCB.ino:4450-4453` (ACK), `:4484-4500` (CRC check) | `etm.chksm_mismatch_silent_loss` records it; delivery is always asserted on a probe wire, never on ACK lines. |
| A mesh `SET_MODE` to NaviCore saves nothing but is not free of side effects: every mode-aware knob re-dispatches. On this bench J2 moves the real servo on NaviCore's Maestro slot 1 ch 0, and that channel's 1500 ms idle auto-release stays armed in RAM until reboot or SET_CONFIG, so a later mesh `setTarget` there goes limp 1.5 s after it. NaviCore also sends two periodic tracked unicasts, its mesh-stats report (every 30 s, to `statsReport.wcb`) and `;V,MODE` (every 60 s, re-timed by a mode change, to `modeReport.wcb`). | NaviCore.ino `processKnobs`, `maestroIdleReleaseTick`; rc_telemetry.h `reportMeshStats`, `reportMode` | `navicore.maestro_mesh_settarget_readback` uses a channel no passthrough knob drives; `navicore.mesh_stats_counts` starts its window right after a stats report and allows the mode report's target one extra send (tracker #76). |
| A disabled controller is not ignored on receive: its ETM commands are still ACKed and run, and `etmSendAck` re-adds the sender as an ESP-NOW peer on demand. So NaviCore's 30 s stats report landing between `?CONTROLLER,OFF` and `ON,20` re-occupies the slot, and `ON,20` then prints no `registered (live)`. | `WCB.ino` `etmSendAck` (on-demand add), `enableControllerPeer` (prints only when the peer is absent) | `peers.controller_off_on` runs with ETM debug on and excuses the missing line only when an `[ETM] Sent ACK seq N to WCB20` falls inside the OFF window (tracker #80 notes). |
| A probe restart - `MESH LEAVE`, a panic, or a USB drop (the probe has no other supply) - forgets every channel binding (`setup()` starts unbound). A USB drop shows no BOOT line: it prints while the port is closed. | `probe_main.cpp:1087` (`setup`); `hil/probe.py:32` (`REBOOT_MARKERS`) | `LinkManager.bind()` and every read/send on a bound link re-bind when the probe printed `BOOT wcb_probe` / `Guru Meditation` / `<<reopened` since the binding (`hil/links.py:291`, `:468`); a read across the restart fails with the reason. The re-bind forgets only the bindings older than the restart, each with a best-effort `UNBIND` (`hil/links.py:316`), so a wire bound after it keeps a channel the probe really holds. After each test the runner fails that test with `probe<n> panicked at t=...` when a probe restarted unplanned, then `RESET`s it and forgets all its channels, or only the lost ones when the `RESET` fails (`hil/runner.py:422`; planned = `MESH LEAVE`, first-use `RESET`, resume reopen - `Probe.unplanned_reboots`). |
| The SBUS controller can hold a reply in its USB outbox until the host sends another byte: HWCDC latches `connected` false after 100 ms in which the host did not drain, the outbox pump then holds everything queued, and only the receive path re-arms it. The controller's own UI pings every 3 s, which lets it through there. | `SBUSController.ino` `serialOutboxPump` | `SbusCtl` waits for a reply (`cfg`, `bootlog`) with a ping every second; the pong queues behind the held reply, so it cannot split the line. Before that, a `getcfg` reply stalled past its 5 s wait in runs `20260915-085309`, `20260922-095341` (it arrived 5.2 s late, ahead of the next test's pong) and `20260928-005805`. |
| A PWM output declared on a raw serial mapping's destination is accepted (D23 checks only the input), and `WcbSoftSerial::write` then drops the mapping's bytes for that port without a word. | `serialMapOwnsPort` (`WCB_PWM.cpp`), `WcbSoftSerial::write` (`WCB_SoftSerial.cpp`) | `pwm.declare_on_serial_map_destination` pins it. |
| A PWM mapping input saved on a port that is now Kyber-reserved is refused at every boot but stays in NVS; a saved output there is dropped ('Cleaning up ...'), but its old keys stay, so `?NVS pwm_outputs` does not shrink. | `loadPWMMappingsFromPreferences`, `loadPWMOutputPortsFromPreferences`, `savePWMOutputPortsToPreferences` (`WCB_PWM.cpp`) | `pwm.boot_conflict_cleanup` |
| NaviCore's header parse skips a string its filter drops without checking escapes, its full parse checks them, and ArduinoJson 7's documents are elastic: `parse failed` is reached only by an invalid escape inside `data`. A `data` that is not an object saves the unchanged config and answers ok:true over USB; the bridge drops it with no ACK. | `NaviCore.ino:3809-3832`, `:3917-3958`; `rc_telemetry.h:1120-1124` | `nccfg.set_ack_shapes`; `nccfg.set_nonobject_data` pins both transports. |
| NaviCore drops a mesh command whose number it has seen from that sender, up to 32 below the highest heard, and a probe restarts at 1 on every join. | WCBClient `WCB_Client.cpp:879-898`, `:2891-2895` | `ncmesh.burn_window` sends at least 66 no-ops and goes 34 past the last one NaviCore dropped. |
| A WCB_Client transmits only to registered peers (boards 1..quantity at `begin()`), and the probe sets no special peer. | `WCB_Client.cpp:1643-1652`, `:2349-2354`; `probe_main.cpp:1026-1031` | `probe_peer` joins with quantity 20. |
| An unprefixed mesh command to NaviCore, a unicast included, goes out every aux port with serialBcast out on. | `NaviCore.ino:3100-3101` | `probe_peer` refuses while any port has it on. |
| USB RESET_DEFAULTS skips the matrix re-arm a USB SET_CONFIG does, and no config apply clears a parked tap: defaults that read a held button leave a tap the next restore releases into the restored mapping (NAVICORE.md D-NC44). `nc_guard`'s fallback restore (RESET_DEFAULTS, then the snapshot) has the same exposure. | `NaviCore.ino:2347-2415`, `:3942-3952`, `:3989-3992` | `nccfg.mesh_creds_live_split` checks the live SBUS input first; this bench's defaults read no held button. |
| `?CONTROLLER,OFF` holds only until a controller device is first heard after a boot (the WDP table is RAM only, and the first learn adopts the controller). | `wdpOnAdvertReceived`, `wdpBegin` (`WCB_WDP.cpp`); WDP_DESIGN.md §9 | `peers.controller_off_reboot_readopted` pins it; the tests that turn the controller off keep auto-join off and put `?CONTROLLER,ON,20` back. |
| NaviCore's SBUS UART keeps the core's 256-byte RX ring (only its TX buffer is set) and no UART event task drains it, so a `loop()` stall past ~100 ms loses bytes. Whether the locked reader then decodes one misaligned frame is the plan's `nc.sbus.overflow_misalign` hypothesis. | NaviCore `NaviCore.ino:4658`; core 3.3.4 `HardwareSerial.cpp:123`; `sbus_reader.h` (the eager flush) | `ncengine.mesh_trigger_burst` and `sbus.stall_no_phantom` make the engine inert first (`_engine_inert`); `sbus.stall_no_phantom` measures the hypothesis with detector knobs on remote slot 4. |
| Mesh TRIGGERs that land while NaviCore's `loop()` is busy wait in an 8-deep queue; the 9th on is dropped with no line. | `queueRemoteTrigger`, `NaviCore.ino:2989-2994`; `:4842` | `ncengine.mesh_trigger_burst` pins it. |
| NaviCore's idle auto-release is not gated by CALIB, and every config apply forgets pending releases until the knob next dispatches. The HCR-volume knob clamps at 99 while the codec takes 100. | `maestroIdleReleaseTick` `NaviCore.ino:2731-2737`; `:3355`; `:2698`, `:2536`; WcbCmd `src/WcbHcr.cpp:29-36` | `sbus.calibration_mutes_knobs`, `sbus.knob_auto_release` and `sbus.knob_hcr_volume` pin them. |
| A WCB drops a mesh `;M<dev>` verb or get for a device it does not host, so nothing answers NaviCore's skip-if-running warm-up for such a device. | `WCB_Maestro.cpp:490-497`, `:566` | `ncengine.skip_running_fail_open` gates on Maestro 2, which W2 hosts. |
| A mode-aware knob whose override switch has no channel follows the global mode after all. | NaviCore `NaviCore.ino:2622-2623` | `sbus.knob_mode_aware` binds its override switch to the resting ry stick. |
| W1 opens or renews its 20 s relay window for any `;W20,` payload that starts with `{`, fragment envelopes included, so NaviCore's ACK to a fragmented message prints on W1 without a bridged send first. | `WCB.ino:8019-8021` | `ncmesh.send_fragments` relies on it. |
| A WCB deafened by `ncmesh.deaf` still transmits, heartbeats included, so NaviCore keeps it online and tracks each unicast to it until three retries run out; NaviCore marks a board online only on a heartbeat or boot announce, offline 50 s after the last. | `WCB_Client.cpp:2716-2769`, `:2480-2508`, `:283-330`, `:2306-2323` | `ncmesh.online_tracking_flip` stretches W2's `?ETM,HB` to 60 s instead; `ncmesh.ensured_degrade` uses a deaf W2 to fill NaviCore's 10 ETM slots. |
| NaviCore ACKs a mesh command before its CRC check, so a CRC-less command is ACKed once and then rejected with `Missing CRC`. | `WCB_Client.cpp:2825-2845`, `:2856-2880` | `ncmesh.crc_namespace_gates` counts one `Missing CRC` line per command. |
| NaviCore's relayed CLI queues 3 lines and drops a 4th that arrives while `loop()` is busy, after ACKing it; its RTERM output is cut every 160 bytes, and a WCB relay drops an empty packet (an empty line, or the flush after a line of exactly 160 bytes). | `NaviCore.ino:4843`, `:2925-2930`; `navicore_rterm.h:48-62`; `WCB_RemoteTerm.cpp` (`rtermRelayHandlePacket`) | `ncmesh.rterm_pieces` predicts the pieces; `ncmesh.remote_cli_order_and_drop` needs the `#L90` stall for the queue. |
| A WCB's WDP row for NaviCore reads `PEER=0` only while NaviCore is that board's controller (the special peer is never auto-joined), else `PEER=2`; it reads `HW=0` (NaviCore sends no HWVER TLV). | `WCB_WDP.cpp:671-678`, `:1817-1822` | `ncmesh.wdp_identity_fields` reads each board's `?CONTROLLER` token. |
| NaviCore sends a board `?WHOAMI` at most 4 times per online session, only while its cached alias is empty, re-armed only by an offline-to-online edge; a WCB with no alias advertises no ALIAS TLV. | `rc_telemetry.h:1808-1831`; `WCB_WDP.cpp:248-251` | `ncmesh.alias_whoami` empties the cache with a `wcb_alias` message from W1 and skips when the budget is spent. |
| Resetting the SBUS controller through its port (`SbusCtl.reset_rts`: RTS and DTR written while the chip's USB re-enumerates) can block for tens of seconds on Windows, 30 s in run `20260928-220200`, longer than the whole signal outage it causes. | `hil/serialdev.py` `usb_jtag_reset` | `sbus.signal_loss_controller_reset` polls NaviCore from a thread started before the pulse. |
| The SBUS controller's frame-format verb saves unless it says `"save":false`, and an image without the INF8 verbs ignores `"save"` (and the four other test verbs, silently). A frame-format change, saved or not, resets the controller's channels: sticks and buttons to 992, every raw `ch` value gone. | SBUSController.ino `processCommandJson` `"mode"` (`hil-week` `:1263`) | `SbusCtl` sends `sbus16` only after `test_state()` answered; s50's `_put_back` centres the sticks and compares all 24 channels with the start. |
| NaviCore's `#L09` `frames=` and `fps` count the loop passes whose `read()` decoded a frame, once each, and the engine acts on the last frame decoded in the pass: two frames drained together - sent back to back, or a backlog after a stall - count once, and the first never reaches the matrix debounce. | NaviCore `processSbus`, `NaviCore.ino:2757-2764` (`sbus_reader.h` `read()` returns a bool) | s50's glitch bracket judges `#L13` and only notes counts, with its model counting one frame per pass (run `20260929-201950`: a double burst counted 1). |
| A frame cut short costs NaviCore one or two good frames: the partial is filled from the next frame until the 40-byte drop, and a 0x0F inside a frame (byte 32 of this bench's resting frame) starts another misaligned partial. A malformed burst never breaks the lock (D-NC72). | NaviCore `sbus_reader.h:132-181` | `sbus.lock_after_glitch` lets frames through one at a time and judges only that each one decoded is real; its note compares NaviCore's counts with `ReaderModel`'s. |
| Every NaviCore config save re-applies Maestro easing on every slot and re-sends each profile's non-zero speed/accel twice, 500 ms apart; on this bench J4's Snappy profile puts slot 2's speed on the WCBStream after every save. | NaviCore `NaviCore.ino:2466-2478` (the switch seed), `:1147-1250` | `ncdev.mae_remote_stream_exact` waits 1.5 s before counting packets; `ncdev.serial_action_bytes` checks only the aux ports for stray writes; `ncdev.mae_local_frames_readback` compares only its own channel's frames. |
| NaviCore's mesh-to-serial forwards wait in a 4-deep queue while `loop()` is busy; the 5th on is dropped with no line. | `queueSerialFwd`, `NaviCore.ino:2937-2943`; `:4845` | `ncdev.mesh_forward_burst` pins it. |
| `executeMaestroCmd` keeps 35 characters of an action's cmd (`char buf[36]`) and parses what is left. | NaviCore `NaviCore.ino:1308` | `ncdev.mae_remote_verbs` pins it. |
| A remote `?MAE` read prints nothing at once: the marker lands when the hosting WCB's `:MQR` arrives, and over the bridge it is mirrored to W1 as `[TERM:20][MAE:<slot>]`. | NaviCore `NaviCore.ino:703-745`, `:802-850` | `ncdev.mae_remote_read` pokes `#L12` while it waits (`_await_mae`). |
| A save that moves `hcrDest` off NaviCore's own port stops a local fade in that pass: no StopWAV or restore goes out, and the HCR is left at the ramp's level. | NaviCore `NaviCore.ino:5548-5552` | `ncdev.hcr_local_payload` pins it. |
| `#L09`'s `fps` is the frames NaviCore parsed in its last one-second window, taken in `loop()`, so a stall at a window's edge reads low while no frame is lost (88 while the frame counter rose 118 in 1.06 s, run 20260929-025701). | `trackSbusFps`, `NaviCore.ino:2885-2892`; `dumpSbusState` `:2897-2904` | s43 `_sbus_kept_up` judges the counter across a transfer (at least `SBUS_FULL_FPS` a second on host time) and lost/failsafe; `NaviCore.sbus_full_rate` re-reads a low window before a gate. |
| A TEST_ACTION's ACK reaches W1 about 0.1 s after its action's marker reaches the probe on W1 S2. | `drainTestAction`, `NaviCore.ino:2175-2190`; `rc_telemetry.h:1225-1235` | `ncmesh.fragment_reassembly_edges` waits for each case's ACKs and 0.8 s more before the next case. |
| A WCB answers a relayed `?MGMT,STATS`, `ETM,CHAR` or sequence request once, in broadcast frames 20 ms apart with no second pass (a config reply has one); the `ETM,CHAR` reply leaves while the 10 s load its run started on the peers is still on the air, so a relay can miss it (run 20260929-025701: three frags sent, none arrived). | `sendResultFrags`, `WCB.ino:4611-4650`, called at `:2358-2362`; the load generator `:2044-2053`; config's two passes `:4552-4556` | `ncmesh.mgmt_etm_char` waits for the target's `[MGMT] Sent result frags` line (`?DEBUG,MGMT`) and asks once more; `mgmt_stats_frag`, `seq_pull` and `seqval_verbatim` ask once more after a lost reply with `?DEBUG,MGMT` on the target, the note naming the lost leg (s43 `_seq_ask`, `_reply_leg`: run 20260929-042105 lost one W2 names reply); tracker #109. |
| Intellex's host drops a transport chunk while no page is bound, and binds a page just after its WebSocket handshake. | Intellex `host.py:329-331`, `:1292-1294` | `LinkTap` clients wait 0.5 s after connecting and compare only from a line both saw (`window`, `line_spans`). |
| NaviCore's PONG is the same line every time. | NaviCore's JSON protocol | `intellex.bridge_load_navicore` compares between the 2nd and 3rd PONG, both asked for after both clients connected. |
| A refused TCP connect to localhost costs about 2 s on Windows (measured 2.05 s). | Windows TCP | `intellex.ghget_retry_unit` bounds a fail-fast refused fetch at 3.5 s: one attempt, not the ~5.6 s a retry takes. |
| A staged Intellex host's `/_api/version` names this repo's commit: no build stamp, no `.git`, and the git fallback walks up. | Intellex `version.py:43-67` | Nothing compares it; `stage()` logs Intellex's real HEAD. |
| esptool 5.3.1 prints progress as `[===>  ]  25.0%` lines and warns `Deprecated:` on esptool 4 spellings. | esptool `logger.py:223-248`, `cli_util.py:35-44`, `:350-383` | The fake esptool prints exactly that; `intellex.flash_esptool5_output` pins what Intellex does with it. |
| A relayed `ETM,CHAR` reply's text starts with a newline, so NaviCore prints a bare `[MGMT:ETM,2]` line and the result block on the lines after it, as a `[MGMT:STATS,n]` reply's rows. | `buildETMCharResultsString`, `WCB.ino:2298-2344`; WcbMgmt `WCB_Mgmt.h:496-511` | `ncmesh.mgmt_etm_char` compares the block after the tag line with the block the target printed (s43 `_etm_block`). |
| NaviCore's WebSocket is a console mirror, not a reply channel: while any client is connected, every line the loop core prints goes to every client and to USB, so a socket command's reply also shows on USB and a USB command's on the socket. A line printed from core 0 (the httpd or WiFi task) reaches USB only. | NaviCore `navicore_wsserver.h:88-93`, `:286-290`, `:515-530`; `rc_serial.h:62-74` | s45 `_sync` takes a barrier on a transport before each read there; `ncwifi.pc_joins_ws_ping` reads `[WS] command queue full` on USB. |
| A record or play action only asks: loop() starts, stops, saves or plays at its next pass. A take saved with no name gets the next free `rec_<N>` (`rec_11` on this bench), and `?REC,START`'s take has none. | NaviCore `navicore_record.h` `pollControl` (:1025-1067), `_takeName` (:1005-1009), `_autoClipName` (:986-994); `NaviCore.ino:3463` | s48 polls `?REC,INFO` after a record action (`_await_state`); every take starts with a HIL-named record action, and `rec_guard` fails on any clip a test did not name. |
| A replay resets speed and accel and re-sends the last target on every channel NaviCore has moved since boot, not only the clip's (NAVICORE.md D-NC64). | NaviCore `_buildCurveIndex` (`navicore_record.h:365-399`) | Every test that replays is in `hil/servos.py`; `ncrec.replay_interpolation_remote` judges only its clip's channel. |

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
| `mesh.frag_trailing_q_mixed_case` | A ?Mgmt,FRAG line ending in '?' is forwarded like ?MGMT,FRAG, not taken for a help request | #95 |
| `kyber.list_setup_line_matches_local` | ?KYBER,LIST's copy-paste line for another board is the one ?KYBER,LOCAL printed for it: each remote Maestro at its own baud and labelled 'Maestro <id>' | #98 |
| `devices.serial_mapped_port_refused` | A port a serial mapping reads refuses an HCR, an MP3 Trigger and a DFPlayer, and a serial mapping refuses an HCR's port as its input, as both sides refuse PWM | #99 |
| `input.usb_line_over_heap_dropped_whole` | A USB line longer than the heap can hold (over the largest free block, under the 32 KB USB cap) is dropped whole with a '[SERIAL] S0: ... dropped' line, or runs whole and verified; never its head alone | #101 |
| `nctool.pong_epoch_slow_direct` | A direct NaviCore whose PONG comes 3.5 s late is still taken for a direct link, not "switched to Via WCB" (the NaviCore config tool, §8) | — |
| `nctool.doorway_pong_misdetect` | A PONG relayed through a WCB (it carries `sys` and `id`) is not taken for a direct link: Via WCB, and USB OTA disabled (NAVICORE.md D-NC30) | — |
| `nctool.refresh_overwrites_edits` | Refresh with unsaved edits asks first; declined, nothing is re-read (D-NC31) | — |
| `nctool.push_refused_not_pending` | A bridged Save over the 192-fragment cap is refused before a byte is sent and leaves no save pending | — |
| `nctool.noop_apply_every_editor` | Applying every button, switch and knob editor unchanged changes nothing, so Save sends nothing (D-NC32) | — |
| `nctool.skip_running_saved` | An action's skip-if-running survives Apply and Save: the tool sends `skipRunning: 1`, which ArduinoJson 7's `\|` reads as false | — |
| `nctool.test_action_refusal_shown` | A TEST_ACTION the board refuses (`ok:false`) is reported to the user (D-NC20) | — |
| `nctool.command_view_cap_on_open` | A command stored with a `;W<n>;S<p>` prefix opens with the prefix already reserved in the field cap | — |
| `nctool.fw_refused_flash_keeps_session` | An Update refused before anything is written (an incomplete firmware set on GitHub) leaves the live session connected | — |
| `nctool.fw_wipe_text` | Nothing the Full Wipe shows promises the saved configuration is erased: the flasher never writes the config LittleFS (NAVICORE.md D-NC34) | — |
| `nctool.ota_usb_lost_chunk` | OTA over USB resends a lost DATA line once the chunks behind it are NAKed, not after its 10 s stall timeout | — |
| `nctool.clip_record_refused` | A Record the board refuses because it is replaying does not arm Stop & Save, whose SAVE would store the replayed clip under the typed name | — |
| `nctool.clip_restore_mode` | A restored clip keeps the mode it was recorded in, not whichever clip was loaded last (D-NC33) | — |
| `nctool.csv_roundtrip` | The legacy CSV export imported straight back leaves nothing for Save to send (today every button band narrows from ±12 to ±10) | — |
| `nctool.multi_tab_save` | With two tabs on one WCB, one tab's save ACK does not confirm the other tab's save (NAVICORE.md D-NC35) | — |
| `nctool.via_wcb_nothing_bare` | Connecting Via a WCB writes only `;w20,`-wrapped lines to the WCB, so it never broadcasts one: today the first status poll leaves bare | — (NAVICORE.md D-NC71) |
| `nctool.board_usb_probe_no_broadcast` | L2 through W1: "Connect via USB" on a tethered WCB puts nothing out of that WCB's serial ports while the tool probes for a direct NaviCore: today its bare JSON PINGs are broadcast | — (D-NC70) |
| `nccfg.string_truncation_utf8` | A string cut at its struct size is never cut through a UTF-8 character | — (NAVICORE.md D-NC42) |
| `nccfg.hold_exceeds_tap_window` | holdMs stays at least tapWindowMs + 100: a tapWindowMs of 6000 must not leave holdMs at 5000 | — (D-NC43) |
| `nccfg.dest_null_hazard` | An empty or null mp3Dest/dfpDest object reads as off, not as an enabled device | — (D-NC22) |
| `nccfg.mesh_creds_live_split` | While NaviCore's RAM mesh password differs from its boot copy, `;W20,?version` still gets its `[TERM:20]` lines back | — (D-NC17) |
| `nccfg.reset_defaults_keeps_identity` | OPT-IN (navicore_reboot): RESET_DEFAULTS keeps wcbNetwork, wcbProfiles, boardType and the wifi fields | — (D-NC16) |
| `ncengine.test_action_skipped_not_ok` | TEST_ACTION answers ok:false for an action the executor then skips: an invalid Maestro slot, channel 32, a bad serial port, a disabled MP3/DFPlayer, an unconfigured WLED id | — (NAVICORE.md D-NC20) |
| `ncengine.skip_not_traced_as_sent` | The dispatch trace never reports a send that did not happen: a serial action to a port NaviCore does not have prints a skip line, not `[DISPATCH] Serial TX [S9] <cmd>` | — (D-NC45) |
| `sbus.reconfig_parked_tap_cleared` | A config save forgets a tap still parked on a held matrix button: releasing it after a save that changed that button's mapping fires nothing, neither the old mapping nor the new | — (D-NC44) |
| `sbus.failsafe_deferred_tap` | Failsafe cancels a matrix tap still waiting to fire: a tap released just before it and a press held into it fire nothing, during it or after | — (D-NC21) |
| `sbus.frame_stop_held_press` | A press in flight when the SBUS frames stop is forgotten: let go during the outage, it fires nothing when they return | — (D-NC21) |
| `sbus.truncated_frame_no_phantom` | A frame cut short never makes NaviCore decode a frame it was not sent: after a lone header byte, or a cut whose next frame closes a 36-byte window on 0x00, the last frame decoded is one the controller sent | — (NAVICORE.md D-NC72) |
| `sbus.sbus24_return_no_prefix_decode` | Back from SBUS-16 to SBUS-24, NaviCore never decodes an SBUS-24 frame's first 25 bytes as a frame of its own | — (D-NC73) |
| `ncboot.new_peer_after_boot` | OPT-IN (navicore_reboot): after a restart, WCBs online when the 8 s boot grace ended fire no `[PEER] New WCB` line and no new-peer action on their next advert | — (NAVICORE.md D-NC25) |
| `ncboot.mesh_reboot` | OPT-IN (navicore_reboot): a REBOOT relayed from W1 is answered with a JSON ACK on W1 before NaviCore restarts, once | — (D-NC29) |
| `ncboot.boardtype2_mismatch` | OPT-IN (navicore_reboot): boardType 2 is refused or clamped to 0-1 on input (today it boots the v2 pins and advertises 'WCB 3.2') | — (D-NC19) |
| `ncmesh.bridged_set_config_strip` | A bridged SET_CONFIG cannot change NaviCore's radio settings: `wifiEnabled` (like `wcbNetwork.channel`) is stripped as deviceId, the MAC octets, the password and quantity are | — (NAVICORE.md D-NC18) |
| `ncmesh.bridged_reset_keeps_identity` | A bridged RESET_DEFAULTS keeps wcbNetwork, wcbProfiles, boardType and the wifi fields | — (D-NC16) |
| `ncmesh.bridged_cmdlib_keys_after_data` | A bridged SET_CMDLIB whose `data` is not the last key stores the library alone, as USB does: its ACK size and hash and CMDLIB_META are the library's | — (D-NC47) |
| `ncmesh.bridged_wcb_send_findings` | A bridged WCB_SEND answers ok:false when the library refused the send (as USB does), and one too long for a packet runs and is ACKed | — (D-NC27) |
| `ncmesh.long_command_truncation` | A relayed command longer than 199 characters is refused with a line, never run cut short | — (D-NC26) |
| `ncmesh.seqval_verbatim` | GET_WCB_SEQVAL returns a stored value holding JSON quotes with them escaped, not stripped | — (D-NC46) |
| `ncmesh.mgmt_frag_multichunk` | A three-chunk `?MGMT,FRAG` push through NaviCore reaches W2 and is stored whole, as through a WCB relay | — (NAVICORE.md D-NC48) |
| `ncdev.mae_verb_no_alias` | A Maestro action's out-of-range number is refused or clamped, never wrapped onto a valid one: channel 261 must not move channel 5, accel 300 must not send 44, target 70000 must not send 4464, speed 65537 must not send 1, restartScript,300 must not run subroutine 44 | — (NAVICORE.md D-NC56) |
| `ncdev.mae_subroutine_msb` | A subroutine over 127 never puts a command-range byte inside a Pololu frame: restartScript,200 and subParam,200,5 must not send 0xC8 after `AA <dev> 27/28` | — (D-NC23) |
| `ncdev.hcr_local_volstep_cap` | OPT-IN (navicore_aux_tx): a per-channel HCR Volume Up on NaviCore's own port reaches 100, as a WCB's does; from 100 it does not send 99 | — (D-NC57) |
| `ncdev.hcr_level_same_both_ways` | OPT-IN (navicore_aux_tx): an HCR Trigger/Stimulate level of 2 or more sends the HCR the same bytes on NaviCore's own port as through a WCB | — (D-NC59) |
| `ncdev.serial_action_paced` | A 95-character serial action to bit-banged S4 at 9600 is answered within 50 ms of a 1-character one: it does not hold `loop()` for its line | — (D-NC58) |
| `ncdev.wled_forward_normalised` | A WLED action written without its `;` (`L1,ON`) reaches a WCB-hosted WLED as `{"on":true}`, and `L1,ON` is not printed out W2's broadcast ports | — (D-NC60) |
| `intellex.settings_branch_dotdot` | POST /_api/branch refuses a branch name with a `..` path segment, as settings.py's own comment says | — (INTELLEX.md finding 12) |
| `intellex.flash_update_partition_escalates` | Intellex's Update FW onto a board holding a different partition table escalates once to bootloader + table + app, NVS untouched, as the Wizard's flasher does; a matching table stays app-only | — (finding 5) |
| `intellex.flash_esptool5_output` | With esptool 5.3.1, `/_api/flash-status` follows the write's progress, and the NaviCore flash puts no `Deprecated:` warnings in the log | — (findings 13, 14) |
| `intellex.identify_direct_pong` | Only a direct PONG identifies a serial port as a NaviCore; a mesh-relayed one does not | — (finding 3) |
| `intellex.wiki_code_verbatim` | A `[[wiki link]]` inside a code block or inline code is shown as written | — (finding 15) |
| `intellex.ui_wizard_setup_images` | Every `../Images` file the Wizard references is bundled by Intellex, and the guided setup's identity and Maestro steps show their pictures | — (finding 2) |
| `intellex.ui_latest_fw_version` | After the host flashes a WCB, the Wizard's own `latestFirmwareVersion` names the build written | — (finding 1) |
| `wizard.app_fake_pending_funcchar` | A function identifier typed into General but not yet pushed is not used for the board's immediate commands (no board) | — (WCB.md W-13) |
| `wizard.push_fake_shared_reboot_repull` | A push that reboots a board on the shared port pulls it again afterwards, as a USB push does (no board) | — (WCB.md W-14) |
| `wizard.push_fake_all_shared_relay` | Push All with the relay on the shared port reboots it without reporting it lost, and its card stays connected (no board) | — (WCB.md W-15) |
| `wizard.push_fake_general_wcbq` | A second board whose WCB quantity differs is named in the keep/use modal, like every other General field every push writes (no board) | — (WCB.md W-16) |
| `wizard.app_fake_system_file_reload` | A saved system file loads back as it was saved: its WCB quantity, no board it did not hold, a board above the floor whole (no board) | — (WCB.md W-17) |
| `wizard.parser` (`unit/model.test.js` todo) | parseSystemFile keeps the WCB quantity a file was saved with when it holds a board above it or a client slot (noted, not failed) | — (WCB.md W-17) |
| `wizard.app_fake_wcb_number_above_floor` | A board above the WCB quantity can be renumbered to any number its dropdown offers (no board) | — (WCB.md W-18) |
| `wizard.app_fake_pull_leaves_nothing_pending` | Pulling a board leaves nothing pending in General: no 'push to all boards' toast, Push All not flagged (no board) | — (WCB.md W-19) |
| `wizard.push_fake_kyber_own_maestro` | A local-Kyber board with a Maestro of its own and one on another board, pulled and pushed with no edit, sends nothing (no board) | — (WCB.md W-20) |
| `wizard.parser` (`unit/model.test.js` todo) | The same Kyber targets in another order are not a change (noted, not failed) | — (WCB.md W-20) |
| `wizard.editors_fake_bidir_remove` | Removing a bidirectional serial mapping also clears its mirror on the destination board (no board) | — (WCB.md W-21) |
| `wizard.mapping_bidir_relay` | On the bench: Remove on W1 clears W2's reverse mapping too (the harness clears what is left) | — (WCB.md W-21) |
| `intellex.nc_via_usb_doorway` | The NaviCore config tool reached through W1 over USB is in Via WCB after every connect, a reload inside W1's relay window included, so "Update over USB (OTA)" cannot send `?OTALOCAL` to W1 | — (INTELLEX.md finding 4) |
| `intellex.wizard_reboot_w1_boots_app` | W1 restarted while the Wizard holds it through Intellex (the Wizard's DTR holding GPIO0 low) boots its app, not the ESP32 ROM loader | — (finding 16) |
| `intellex.wizard_mesh_relay_slot_number` | A board Intellex's shim routes through W1 is filed under relay slot 1 as a number, so a W1 link drop clears and re-arms it | — (finding 17) |
| `intellex.flash_refused_board_runs` | A WCB flash Intellex refuses after detecting the chip leaves the board running its app, answering on its own port with no reset from anyone (esptool's flash-id left it in the ROM loader) | — (finding 18) |
| `intellex.link_drop_no_stall` | A WebSocket link whose far end vanishes is dropped without stalling the host: `/_api/status` never goes unanswered over 2.5 s and shows the loss within 3 s of the host's own verdict (its drop runs on the event loop and waits websockets' 10 s close_timeout) | — (finding 19) |
| `ncwifi.ws_line_trim` | OPT-IN (navicore_wifi): a line with a leading or trailing space or tab runs over NaviCore's WebSocket as it does over USB, which trims every line | — (NAVICORE.md D-NC61) |
| `ncwifi.ws_stalled_client` | OPT-IN (navicore_wifi, navicore_reboot): a socket that stops reading for 15 s while NaviCore streams costs another client none of its replies and is not left open and deaf; NaviCore does not restart | — (D-NC62) |
| `ncwifi.usb_editload_with_socket` | OPT-IN (navicore_wifi): with a socket connected, a `?REC,EDITLOAD` range of 600 events over USB comes back whole, as with none | — (D-NC63) |
| `ncrec.replay_only_clip_channels` | A replay resets speed and accel and re-sends a target only on the channels its clip drives | — (NAVICORE.md D-NC64) |
| `ncrec.busy_load_not_missing` | A listed clip asked for while the recorder records or replays is answered busy, not 'not found' | — (D-NC65) |
| `ncrec.editcancel_empties` | `?REC,EDITCANCEL` empties the buffer, and `?REC,CLEAR` never answers 'cleared' while a take goes on | — (D-NC66) |

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
| A board relearns its neighbours' WDP-DA device lists only from their next advert: the 60 s backstop, unless something asks (`?WDP,POLL`, the Wizard's Poll mesh). The lists it held are RAM only, and Chrome's open resets W1, so right after a Wizard connects its mesh panel shows no remote devices. | `WCB_WDP.cpp` `wdpSendSolicit` (only `?WDP,POLL` sends one); `docs/WDP_DESIGN.md` Cadence | `wizard.wdp_da_forget` presses Poll mesh before it looks. |
| W1 prints mesh JSON on USB - NaviCore's unicast PONG to a bare PING included - only for 20 s after a `;W20,{...}` line from USB; a bare JSON line never opens the window. | `WCB.ino:8019-8021`, `:5513`; NaviCore `rc_telemetry.h:2186-2192` | `intellex.nc_via_usb_doorway` waits 21 s with nothing attached before its cold connect, and reloads inside the window on purpose. |
| Through Intellex the Wizard's connect asserts DTR alone, which the host applies to W1's CH9102: GPIO0 is held low while the Wizard is connected. | `Wizard/app.js:5364`; Intellex `intellex_shim.js:248-260`, `host.py:1239-1260` | Every W1 test through Intellex hands a W1 that does not answer afterwards to `reset_into_app`; `intellex.wizard_reboot_w1_boots_app` is finding 16's `(should)` test. |
| The NaviCore config tool's Save with no edits sends nothing, and its `#push-budget` readout leaves the numeric fields out. | NaviCore `config_tool/index.html:16911-16925`, `:16665-16676` | `intellex.nc_save_unchanged` makes its round trip with `chRateHz` and reads the ACKed baseline, not the readout. |
| This PC's spare WiFi adapter lives on NaviCore's access point (its own profile). A NaviCore test that takes that access point down gets no reconnect until NaviCore is back, and a socket whose PC end left with a temporary profile stays in NaviCore's WebSocket sink until a send to it fails - a gone peer never makes that happen - keeping NaviCore's tee armed (D-NC62, D-NC63). | NaviCore `navicore_wsserver.h:146-149`, `:288-289`; `hil/wlan.py` `pc_on_ap` | `pc_on_ap` notes a missed reconnect to the access point under test instead of failing; `ncwifi.usb_editload_with_socket` restarts NaviCore for a cut baseline. |

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
| `?WLED,PORT,CLEAR` releases the port; S3-S5 unreliable above 9600; several WLEDs per WCB deferred (`docs/WLED_INTEGRATION.md` §3-§4, fixed 2026-09-28). | It prints the legacy usage and changes nothing; the clears are `?WLED,CLEAR` / `?WLED,CLEAR,<id>`; soft ports take 57600; a board holds nine WLED slots (`WCB_WLED.cpp`, `WCB_WLED.h`). | `wled.clear_all_releases_local` |
| The controller flag and id take effect after a reboot (source comments, `WCB_Storage.cpp` `saveSpecialPeerPreferences` and its loader). | `?CONTROLLER,ON` registers the peer live and OFF removes it live (`WCB.ino`). The comments are corrected with the next firmware change (a comment-only change would re-stamp the image). | `peers.controller_off_on` |
| A held second tap dispatches its tier when the button comes up (NaviCore `NaviCore.ino:2345-2349`, the comment; `docs/ARCHITECTURE.md:308-309`). | Only a gesture's first press opens a hold (`:2350`), so `checkDeferredTap` fires tap 2 tapWindowMs after the press with the button still down (`:2412`), and the release does nothing (`rcMatrixRelease`, `:2380`). | `sbus.held_second_tap_midhold` |
| The WCB-side remote Maestro read has not shipped (NaviCore `docs/TROUBLESHOOTING.md:55`). | It has: W2 reads its Maestro and answers `:MQR` (`WCB_Maestro.cpp:521-627`), and NaviCore prints the marker (`NaviCore.ino:808-850`). | `ncdev.mae_remote_read` |
| Emotion 4 on Trigger/Stimulate is an Overload shortcut (NaviCore `NaviCore.ino:1435-1447`, `:1500`). | WcbCmd 0.9 dropped it: `HcrCodec::normalize` refuses emotion 4 on both transports (`WcbHcr.cpp:11-15`). | `ncdev.hcr_remote_verbs`, `ncdev.hcr_local_payload` |
| REBOOT deauthenticates the SoftAP's stations before it restarts (NaviCore `NaviCore.ino:4034-4047`, the comment). | `otaFarewellAP()` is empty on purpose: the deauth measured 5 s worse on Windows (`navicore_ota.h:184-204`). | `ncwifi.ap_boot_lines` (the PC joins after the restart) |
| A reply that arrives asynchronously, such as a remote Maestro read, reaches Serial only, not a WebSocket client (NaviCore `navicore_wsserver.h:553-557`); the sink is armed around `processInputLine()` (`docs/PROTOCOLS.md` §3). | The tee stays armed for a client's whole session (`navicore_wsserver.h:88-93`, `:526-530`), so the late `[MAE:<slot>]` marker of a read asked over the socket reaches it. | `ncwifi.ws_capture_slot` |
| `captureArmed()` is true while a WCB-relayed command runs (NaviCore `rc_serial.h:85-87`). | It is true for as long as any WebSocket client is connected, which is what EDITLOAD then reads (D-NC63). | `ncwifi.usb_editload_with_socket` |
| The WebSocket carries the same protocol as USB because it feeds the same dispatcher, with nothing to keep in step (NaviCore `navicore_wsserver.h:5-8`; `docs/PROTOCOLS.md` §1). | The framing differs: USB trims every line and the socket does not (D-NC61). | `ncwifi.ws_line_trim` |
| A recorded action is stored with delay 0; the replay schedules by time alone (NaviCore `docs/RECORD_REPLAY_DESIGN.md` §4). | A delayed action is captured when it fires, with its pending copy's delay kept in the record (`navicore_record.h:258-263`, from `checkPendingActions`, `NaviCore.ino:2202-2220`); the replay ignores it (`recCbDispatch` calls `rcExecuteActionNow`, `:2192`). | `ncrec.capture_scope` |
| Maestro setSpeed/setAccel are never captured (NaviCore `docs/RECORD_REPLAY_DESIGN.md` §5; NAVICORE.md D-NC36). | A setSpeed action is captured like any other: `captureAction` leaves out only record, play and stop (`navicore_record.h:260`). | `ncrec.capture_scope` |
| RA_RECORD's clip name is for a future clip library; the build keeps one in-RAM clip (NaviCore `rc_config.h:43-45`, `:1044`). | The take is saved under that name on the clips partition (`pollControl`, `navicore_record.h:1030-1042`). | `ncrec.record_save_list_rm` |
| A malformed frame breaks the SBUS reader's lock streak (NaviCore `sbus_reader.h:217-218`). | Both flushes take only a complete buffer, so `tryParseAndReset`'s malformed branch (`:240-243`) is never reached: a malformed burst leaves the lock as it was, and a partial frame is completed by the next frame's bytes (D-NC72). | `sbus.lock_after_glitch`, `sbus.truncated_frame_no_phantom` |
| The prefix check is what catches a 16-lock's eager flush misfiring on an SBUS-24 frame (NaviCore `sbus_reader.h:163-165`). | It drops the lock only after the misframed 25 bytes were decoded and handed to `processSbus` (D-NC73). | `sbus.sbus24_return_no_prefix_decode` |

---

## 7. Coverage

| Group | Checks |
|---|---|
| `probe.*`, `link.*` | Probe firmware answers; every discovered wire carries its port's output byte-exact, locally and over the mesh; two probe soft channels receiving at once decode exact (`probe.soft_concurrent_rx`). |
| `softrx.*` | Soft-port RX against ESP32 erratum GPIO-3.14 (tracker #78). `softrx.erratum_pairs` (opt-in): two- and three-port lines into W1 S3-S5 at a swept sub-bit skew from `TXSKEW`, against single-port lines on the same ports, observed on W1's USB console (W1 transmits nothing); it confirmed the loss on the edge-triggered image and must pass on the level-triggered one. `softrx.level_irq_stuck_line`: W1's S4 RX held low for 2 s causes no interrupt storm or reboot, and S4 reads text exactly afterwards. NVS saves under a 50 Hz PWM input, and a `?reboot` under PWM input or streaming text, boot once (boot attempts +1, a software reset) and name the stale level arms. |
| `soak.*` | Opt-in soak (tracker #78): W1's S2/S4 fan-out for `soak_minutes`, with W1S4 alternately on a probe hardware and soft receiver, `;S4` alone, and `;S2` alone with W1S4/W1S5 checked for induced bytes; CSV per block, heap every 5 min. |
| `wcb.*` | Backup and remote-pull CRCs, firmware match across boards, unknown-command and help-trap parsing; `?HELP`, `?help` and a bare `?`; `?NVS`: the usage line and one line per namespace, adding up; `?backup` of a config near 6 KB prints both chains whole and CRC-valid; a mesh pull (`?MGMT,PULL,<n>,P`) just under the 2912-character limit arrives as one line, one over it in parts, joined and CRC-checked, and in 6 parts with `?DEBUG,PULLPART,600`; a plain pull over it gets CFGERR NOPARTS and no part; out of memory (`?DEBUG,PULLFAULT,OOM`) gives CFGERR NOMEM, followed by the empty legacy line under the limit; a config line that could not be built (`?DEBUG,PULLFAULT,LINE`) is left out of `?backup` with a WARNING after each output, and a pull answers NOMEM for it (audit F19); the target keeps answering its own console within 150 ms while it sends ([MGMT_RELAY.md](MGMT_RELAY.md)). |
| `serial.*` | `;S` comma form, `;S0` to USB, `^` chains in order, bad port, `;T` delays, summed delays, `?STOP`; the 30-minute `;T` cap, reported with or without `?DEBUG`, and a negative delay, which refuses the whole chain.; one timer chain at a time (a typed chain or a recalled sequence with its own `;t` replaces the running one, which says how many groups it dropped), and the `;T<ms>,<command>` inline payload. |
| `mesh.*` | Unicast delivery and ETM ACK accounting, alias routing, unreachable target, chain split, `?MGMT,FRAG`, `?RTERM`; a `;T` chain delivered by `?MGMT,FRAG` keeping its delay on W2; the `?MGMT` error replies (most need `?DEBUG,MGMT,ON`) and the `?RTERM` ones. On the target: a three-chunk push applied whole and in order (and one ending in `?reboot` restarting once), plain text from a FRAG re-broadcast once, a line ending in `?` forwarded in any case of the verb, FRAG sessions (busy, stale, the 15 s drop); a long `?RTERM` mirror (`?HELP`, `?backup` in 160-byte pieces), a mirror relayed to NaviCore, and `?RTERM,STOP`; `;W` alias case, a client addressed by its device type, ambiguous aliases, the usage lines and the legacy digit forms. |
| `persist.*` | Label (local, remote, across reboot) and live baud change, each restore-verified; the legacy `?SLS` / `?SLCS` / `?BAUDS` spellings; `?ALIAS` and explicit `?BCAST` flags across a W1 reboot; `?BAUD` and `?LABEL` validation, the soft-port warning above 57600, and `?LABEL,CLEAR,ALL`. `?WDP,AUTOJOIN,OFF`, `?BCAST,OUT,S0,ON` and ETM MISS/BOOT/COUNT/DELAY across a W1 reboot, with `?DEBUG`/`?DEBUG,ETM` back off. |
| `input.*` | Bytes injected into WCB ports: CR/LF terminators, partial lines and per-port buffers, long lines and queue overflow, `?BCAST` IN/OUT filtering and fan-out, Maestro reply routing, soft-serial RX under load and across bauds, framing variants, `@WDP` device announces: one record per port and device type, saved only by its second announce (one heard once is dropped at 90 s), kept when it goes quiet and across a reboot (reloaded as not heard), `?WDP,DA,FORGET`/`CLEAR`, a full port refusing a newcomer rather than dropping a saved device, a shared port's label staying with the first type heard, cut-short and run-together lines rejected, and a W2 device reaching W1's dump and `?WDP,2` whole and leaving it on a forget. Soft-port TX byte-exact at every rate `?BAUD` takes for S3-S5, 110 to 115200; the heap staying flat under a 12 KB unterminated line, and a long USB line's buffer freed, or dropped whole when the heap cannot hold it (#101); live `?BAUD` re-begins under input, and the S1/S2 drain before a rate change; WDP-DA: a reflashed device's new facts saved, sent and reloaded after a W2 reboot, a manual `?LABEL` winning over a device type in the dump and the advert, a four-frame device list, and `?WDP,CLEAR` dropping only neighbours' lists. Every WDP-DA test forgets the records it makes and first forgets `HIL`-named leftovers: records are kept until forgotten. A typed broadcast keeps its mesh copy under a client's JSON flood; soft-port RMT TX byte-exact while the loop task writes NVS. |
| `map.*` | `?MAP,SERIAL` text and raw mappings: local, multi-destination, remote, S0 and soft ports, validation, persistence across reboot, the CLEAR paths, legacy `?SM`. `?MAP,CLEAR,ALL` with a PWM output port (its lines in order, one reboot, both lists empty); a shrunk mapping freeing its surplus NVS keys; pre-mapping broadcast flags kept across a reboot; the mesh-to-soft-port queue: drops counted exactly, chunks whole and in order, `?BAUD` re-begins under traffic. |
| `pwm.*` | `;P` pulse widths and guards, PWM output ports, the deferred mapping reboot, local and mesh passthrough measured on the probe, WDP PWMTARGET auto-config and self-heal, including the self-heal after a missed explicit clear (W2 made deaf for it, `pwm.wdp_selfheal_missed_clear`). The legacy `?PLIST` / `?PRS` / `?PMS` / `?POS` / `?PCLEAR` spellings. `;P` refused on MP3/HCR/DFP ports and on a raw mapping's input (its destination still pulses); a valid re-map clearing the dropped remote output; one input to two local outputs and two inputs kept apart, the silent 5-output cap, and no port short of an RMT channel; PWM on S2's hardware-UART pins; widths within 20 us under mesh load; a PWM output on a raw mapping's destination (accepted, the mapping's bytes dropped, D23); PWM saved on S1 meeting Maestro_Remote at boot (the output erased, the input refused but kept in NVS); the `?DEBUG,PWM` trace and the change/settle send rule; remote PWM config within 5 s of a boot; `?BAUD` on PWM ports re-beginning no soft UART. Prior broadcast flags and the WDP source tag surviving a reboot; self-heal sparing a manual output. |
| `hcr.*`, `mp3.*`, `dfp.*`, `mp3dfp.*`, `route.*`, `conflict.*`, `backup.*`, `port.*`, `wdp.hcr_*`, `devices.*` | Exact device bytes for every `;H`, `;A` and `;D` verb with no device attached, emulated device replies (HCR status, MP3/DFPlayer finish and error callbacks), HCR poll timing and fades, config validation, WDP route learning and how to undo it, port moves, backup round trip. `;H` also in its word emotion forms and the NOW / GRACEFUL stop styles; `backup.replay_idempotent`: W1's own chain replayed one line at a time is accepted and changes nothing, which is how the erase tests restore W1. Capability routing with a pinned host (`route.*`): a pin never heard since boot is honoured; a heard-then-lost pin fails over to the live owner without rewriting the route, and stays the target when no owner is live; a pin to the controller is honoured while it heartbeats; an ETM-offline owner is skipped. Port ownership (`devices.*`): each device refuses a port another device, a local Maestro, PWM (input, mapping output, output-only) or a serial mapping owns, and a refusal changes nothing. The HCR volume shadow (two agreeing replies seed it, commanded values show at once, stale replies are ignored), `;H,FN,20/21`, the poll's ten 3 s retries and its hold during a fade, rarer `;H` spellings; hand-pinned `?MP3`/`?DFP,REMOTE` routes, `?DFP,ONERR,CLEAR`, a DFPlayer port move, the MP3 port's broadcast skip with its flag forced on, `;A,PLAYFS` callbacks. |
| `var.*`, `if.*` | Variable verbs, limits, names, persistence and backup position, `;M!`, per-board scope; every IF operator, AND/OR, gating scope, timers, rejections, IFs run on W2 and inside stored sequences. The reserved name `all` and `?VAR,CLEAR,ALL` through RAM, NVS, `?backup` and boot. |
| `seq.*`, `legacy.*`, `inv.*` | `?SEQ` formats, the 15-character key limit, NAMES order and its FNV-1a hash, recall forms, comment stripping, the save boundary, the cycle guard and sub-sequence reuse, `,L` and nested fan-out; legacy `?CS`/`?CE` and `?CHK`; the `?MGMT,SEQ`/`SEQGET` relay, its dedup, multi-chunk values, and the longest value a board actually stores (TOOBIG checked once one is long enough); the sequence store, a file of its own (`seq.store_*`, [SEQUENCE_INVENTORY.md](SEQUENCE_INVENTORY.md) §3d): its `?NVS` line against `?SEQ,NAMES` and each save's bytes, filled to its 32 KB with W2 still pulling W1's whole config in parts, and sequences a `?DEBUG,SEQNVS` boot saved in NVS moving into it at the next boot, NVS winning a key both hold; opt-in `nvs_fill`: on that NVS fallback, W1's NVS filled with sequences until a save is refused, with nothing left half-saved or listed empty (and both one-line chains whole and CRC-valid on the full store). A 16-character CLEAR/CE key leaving the 15-character sequence; chain-walker edge forms (a letter function identifier, a lowercase verb, `?MGMT`, mid-line, an IF-first `?CS` holding `;T`, a ', p' pull); `?config`'s sequence section; `?SEQ,GET` answers NOTFOUND for the NVS layout's `key_list` and `seq_mig_done`; opt-in `seq_wipe` also checks that both clear-alls re-stamp `seq_mig_done` (`stored_cmds` keeps that one entry, read through `?NVS`). |
| `maestro.*` | Exact Pololu frames for every verb, invalid-argument rejection, target 9, fan-out to a remote host, and a real servo `setTarget` → `getPosition` round trip over the mesh; LIST in every spelling; get-query decoding, timeouts, short and extra replies, and rejections; the `;M0` broadcast; fallback broadcast, and fallback unicast to the probe as client 5; stale proxies; a client's get answered back to it, and the `;M!` name guard; `CLEAR,ALL` with legacy routing and an ordered rebuild; the slot map and 9-slot cap; every CLEAR form and add-path refusal; the legacy no-comma `?MAESTROM<id>:W<wcb>S<port>:<baud>` spelling. One id on two local ports (a frame per port, one unicast per remote host, a get reading the later slot); get-queries on S3 (9600, 57600) and S2; daisy-chained ids on one port for `;M0`, `;M9` and the per-id paths; the legacy S1 fallbacks naming an HCR or WLED owner; a proxy's backup port taken from a same-id Kyber target; `?MAESTRO_CLEAR` / `?MAESTRO_DEFAULT` in both cases (`,M5` and mixed case refused); the generated `?MAESTRO_REMOTE^...^?REBOOT` line run on W2. WDP auto-add: a cleared proxy learnt back into its own slot and re-bauded, 'heard but no free slot' with a full table, a proxy per host beside a local id, kept until cleared. |
| `kyber.*` | The Maestro_Remote bridge both ways, with a real Maestro reply; `?KYBER,LOCAL` on S2 across the pre-reboot deaf window, targeted and broadcast forwarding, explicit targets and the Maestro-to-Kyber return; the Kyber on S1 leaving S2 readable and free for a WLED or PWM output, and S3-S5 refused before any state changes; moving the Kyber between S1 and S2 and `?MAESTRO,REMOTE` releasing the old port; a Maestro clear dropping its Kyber target; the legacy S1 fallbacks skipping the Kyber port; the Kyber port named in `?KYBER`/`?config`; `?KYBER,CLEAR` without a reboot; a local Maestro on S2; target id 9; parse errors; WDP reverting a board to Maestro_Remote; the legacy `?KYBER_CLEAR` / `?KYBER_REMOTE` spellings. Kyber bytes written once per port with two ids on it (targeted and broadcast mode); a board with no Kyber mode booting S1 as a command port at its saved rate, and `;P1` refused by the Kyber term alone; the Maestro_Remote boot skipping a PWM S2; the Serial1 fallback on a remote board with no local slot; byte transparency 0x00-0x7F; a WDP-learned remote Maestro folded into the Kyber targets; `?KYBER,LIST`'s copy-paste line equal to `?KYBER,LOCAL`'s (tracker #98). |
| `wled.*` | Exact JSON for every `;L` verb routed to the WLED host, unknown-verb silence; `?WLED` configuration (`s24_wled_config.py`): LIST / STATUS formats, validation, a local add, move and clear on W1 with its port flags and label, the legacy `?WLED,PORT` form, the remote proxy route, WDP auto-add; a local WLED loaded at boot.; the one-hop cap between two proxies pointing at each other, `?WLED,CLEAR` releasing every local port (and `?WLED,PORT,CLEAR` doing nothing), WDP auto-add never re-homing a proxy or shadowing a local WLED, and a bare `;L` taking the lowest-id local WLED. |
| `navicore.*` | JSON ping, mesh membership and WDP table, `;W20` remote CLI, `WCB_SEND` to both WCBs, mesh `TRIGGER`; its Maestro inventory against W1's proxies, `?MAE` markers, mesh setTarget with read-back, mesh queries answered into W1's variables, inbound `;M` guards and 0/9 fan-out; serial routing, plain broadcasts both ways, `WCB_SEND` edges and fragments, JSON bridged through W1, declined mesh JSON, exact mesh stats, safe `FORGET_PEER`, `SET_MODE`, `TEST_ACTION`, `TRIGGER` bounds and dispatch traces, record/replay markers (opt-in clip playback), `#L` diagnostics, and a temporary probe it never learns; W1's 20 s RC telemetry relay window that a `;W20,{json}` opens (`navicore.rc_relay_window`); a config pull through its WCB_Client relay (one line, plain and `,P`), and an over-limit config never reaching it as a `[MGMT:CONFIG,2]` line (silent while it runs a library from before F13). `:MQR` replies to NaviCore's `;M1,get...`; `?OTALOCAL` pausing W1's relay for 8 s. |
| `sbus.*` | Controller ping; a virtual stick moves NaviCore's decoded channel to the exact calibrated value and back to centre; the safe channels NaviCore binds nothing to; matrix button single, double, triple and held taps with rc_trig timing; tap mode after `SET_MODE`; exact switch, slider and trim values; opt-in signal loss by resetting the controller; the signal-loss check takes the longest window on one frame count (fps is a one-second average and can miss 0). The engine through SBUS (`s42`): logical bands, the matrix debounce, tap saturation, another button committing a pending tap, the mode latched at the press, long press, a held second tap; switch tiers (settle, seed, setEasing seeding), the mode-switch decode and deadband; passthrough knobs (exact formula, deadband, reverse, midClosed), mode-aware knobs, auto-release, knob easing, HCR-volume throttling, CALIB muting; the reader under load, the CH17 prefix ambiguity, stalls with no phantom frame, and saves while inputs are live.; `sbus.boot_quiet` (opt-in `navicore_reboot`): a NaviCore restart with a stick, a switch and a matrix button held fires no setTarget frame, switch tier or rc_trig. The faults (`s50`, NC-WP11), through the controller's INF8 test verbs (each test skips without them): the verbs' acceptance, and a controller reset clearing them (opt-in `sbus_reset`); the failsafe flag freezing knobs, switches and the matrix (rc_hb sbusFail; values parked during it never acting, a button held across it needing a release) and the lost-frame flag gating nothing; SBUS-16 detected at full rate with CH17-24 at 992, and back; malformed bursts - noise, two frames back to back, a pause mid-frame, cuts - decoding nothing unsent; CH17 raw values that zero frame byte 24 under load; a one-frame dip against the matrix debounce; and `(should)`: a deferred or held tap cancelled by failsafe and a press in flight forgotten when the frames stop (D-NC21), a truncated frame never decoded misaligned (D-NC72), the return to SBUS-24 never decoding a prefix (D-NC73). |
| `ncengine.*` | NaviCore's RC engine driven over USB and the mesh (`s41`): tiers exclusive and cumulative, the 8-slot delay queue and its overflow, the CALIB gate (rc_trig still emitted, TEST_ACTION ungated, a due delayed action dropped), rc_trig on USB and relayed, the 8-deep mesh TRIGGER queue under a stall, TEST_ACTION per action type, the Maestro skip-if-running gate (local slot, and via-WCB fail-open with `:MQR`), and the contents of the mode and mesh-stats reports. |
| `ncboot.*` | NaviCore restarts (opt-in `navicore_reboot`): the boot banner in order against GET_CONFIG with App SHA256 equal to STATUS's; REBOOT and `#L02` clearing the debug flags and the monitor; W1's boot-announce line, a fresh WDP row, `[TERM:20]` and a NaviCore->W1 unicast after a restart; the boot roll call, healthy and with a raised floor naming an absent board; a relayed REBOOT, boardType 2 and new-peer events at boot (`(should)`); a saved invalid deviceId (attended, `navicore_identity`). |
| `ncota.*` | NaviCore's image and OTA: the running image named by its App SHA256 (the bench image, its ELF beside it, FLASHED.md's last write); the ladder's reset rung through `ncflash.recover()` (`navicore_reboot`) and its esptool rungs (`navicore_esptool`, watched); `?OTALOCAL` STATUS, parsing, session-less and usage errors and BEGIN guards; W1 relaying to NaviCore and NaviCore relaying to W2 with nothing erased; opt-in 4 KB sessions with ABORT and the reaper (`navicore_ota_erase`), the bench image twice over USB (`navicore_ota_full`) and through W1 (`navicore_ota_relay_full`), W2's image relayed through NaviCore (`ota_full_wcb2`). |
| `ncmesh.*` | NaviCore on the mesh (`s43`): the JSON bridge through W1 (GET_WCB_META through the fragment sender, SET_CONFIG in one packet and in fragments with the identity strip, RESET_DEFAULTS and its live side effects, the command library both ways and the bulk transfer, fragment reassembly edges and the 3-session pool, bridged WCB_STATUS, MESH_STATS and RESET_MESH_STATS, WCB_SEND, the USB-only types); the management relay (`?MGMT,STATS`, `ETM,CHAR`, FRAG single and multi-part, the over-long line, a remote terminal on W2, a config pull interleaved with OTA relay traffic); the relayed CLI (order, the 160-byte wrap, the 3-deep queue); WDP (NaviCore's advert on each WCB, port labels and their re-advert, the solicit, the neighbour table and its scrub, the `?WHOAMI` budget, learn and forget behind `navicore_nvs`); membership and failure (ETM ACKs to W1, W1's offline sweep of WCB20, W2 OFFLINE and ONLINE on NaviCore, the full ETM table, both WCBs deaf, the duplicate window across W1 reboots, the CRC and password gates, long commands); content (WCB_STATUS against each board, sequence pulls and every refusal text, telemetry fields and rates, the 15 s rc_ch gate, one-hop routing). |
| `ncdev.*` | NaviCore's device transports and Maestro (`s44`): remote Maestro slot 4's frames byte-exact on W1 S1 and the W2 S1 tap, packets cut only between frames (174 + 22 for a 33-frame burst); every Maestro verb with its clamps, channel 32 refused and the 35-character cmd cut; easing as frames (setEasing's two repeats 500 ms apart, restartScript's own/switch/override/Off rules); the remote `?MAE` read answered by W2's Maestro 2 (`:MQR`, W2's own copy, relayed as `[TERM:20]`); NaviCore's Maestro 1 read back after `?MAE`, an action and a knob (servo); the HCR, MP3 Trigger, DFPlayer and WLED through W2 on its probe ports, byte-exact, refusals included; the same tables on NaviCore's own S4 from the hook image's `DBG_WIRE` (opt-in `navicore_aux_tx`), `#L20`/`#L21`, a timed fade cut by a save; serial actions byte-exact and cut at 95 characters; the 4-deep mesh-to-serial queue under a stall; serialBcast out on S4 (plain and unprefixed once, JSON never, a device-owned port skipped); six `(should)` findings (D-NC23, D-NC56 to D-NC60). |
| `ncwifi.*` | NaviCore's SoftAP and WebSocket with the PC on its access point (`s45`, opt-in `navicore_wifi`, a spare adapter only; `hil/wlan.py` `pc_on_ap`, `hil/ncws.py` `NcWs`): a lease with no default route, PING answered bare as on USB, six PINGs in one frame, Intellex's discovery probe read as a NaviCore; the same replies over the socket as over USB (PING, GET_CONFIG byte for byte, TRIGGER and its rc_trig, TEST_ACTION, `?REC,LS`, `?version`, `?OTALOCAL,STATUS`, `#L12`); the console mirror both ways, and PWM_UPDATE, rc_trig and the dispatch trace with NaviCore's USB unread; three sockets, the least recently active evicted by a fourth, interleaved half-lines; line framing (CR, LF, CRLF, split frames, the tool's 1391-character DATA line in 512-byte frames, a closed client's unfinished line, the 98304-byte frame and the unended line that close a socket); every text frame whole UTF-8 across the sink's 2 KB flush, and a client dropped mid-reply costing the next nothing; the Wizard's surface (`?backup`, `WCB_WEBTOOL_CONFIG_PULL`, `?WDP,DUMP`, `?MGMT,PULL,2`, relayed `?OTA` ACKs); the mesh while the socket streams; the capture tee's single slot; a 5-minute ping soak; with `navicore_reboot` too, the access point's boot lines and a 3-character AP password refused at boot (fail closed) and put back; three `(should)` (D-NC61 to D-NC63). |
| `ncrec.*` | NaviCore's recorder (`s48`; opt-in `navicore_clip_write` for every clip written; `rec_guard` keeps Greg's clips and the buffer as found): a take of W1 S2 markers started by a TEST_ACTION record, stopped and saved by the second under the name the first carried, listed with its size, duration and count, downloaded at its probe-clock spacing, copied by `?REC,SAVE`, renamed (an existing name and a missing clip refused) and deleted; what a take captures (TEST_ACTION, a TRIGGERed tier, a delayed action at its fire time with its delay kept, a setSpeed; not a play action); a stop action saving, `?REC,STOP` keeping a take in RAM for `?REC,SAVE`, the toggle's latched name, `?REC,STOP` ending a replay; a replay's marker spacing and the play toggle; the replay gate (a live TRIGGER and a due delayed action dropped, TEST_ACTION and a mapped stop let through, CALIB not stopping the clip); an uploaded keyframe clip replayed on remote slot 4 as speed 0, accel 0 and a dense interpolated ramp; CALIB dropping a take; the 60 s backstop; the in-memory migration of Greg's 136-byte clips, read only. Three `(should)`: D-NC64 to D-NC66. |
| `nctool.*` | The NaviCore config tool (§8, `s49`). No board: its scripts compile, the firmware/tool constant pairs agree, its pure functions run in node, and about 60 specs drive the real page against an in-Node emulator (transports, line framing and fragments, saves and their ACKs, the editors, the Firmware tab's flash and both OTA state machines, clips, export and import, two tabs, the live panels). With the board, through the harness's own port (L2): the connect applies the real CONFIG with nothing to save; a port label saved changes GET_CONFIG by that label alone; an action's Test lands on W1 S2; the live channel grid follows the bench controller's stick; the clip list and "Load from NaviCore", read-only; the emulator answers in the board's shapes and its model prints the board's config back byte for byte; a same-image OTA over USB with the tool's own reconnect (`navicore_ota_full`); Via a WCB through W1, with a fragmented save; and the `(should)` probe broadcast through W1 (D-NC70). Attended, over real Web Serial (`navicore_webserial`): the open's restart, and a Full Wipe & Flash that leaves the config and the clips as they were. |
| `intellex.*` | Intellex (§10), 83 tests. No board (`s32`): a staged, leashed host; the tool routes and every GET API route, the Origin guards, attach validation, the ERROR frame, branches, the offline con floor and the GitHub proxy from a seeded cache; a link whose far end vanishes, dropped without stalling the host; the docs viewer's escaping, nonces, sandbox and traversal; Intellex's own smoke tools; its flash rules, the flash pipeline with a fake esptool and port, the GitHub retry rules, paths and logs (under its venv); both tools as working-tree and shipped bundles, the shim's glue, the shim-tool contract, the launcher, the shell and the docs in Chromium. Bench (`s33`): Intellex's SerialTransport on W1 and NaviCore (no reset on open/close or set_signals(False,False), bytes only, byte-exact UTF-8, raise after close or loss, the RTS reset), its WebSocketTransport on NaviCore's AP, and the bridge end to end (a /_link line out of W1 S1 once, two pages byte-identical under console, mesh and monitor load, CR/LF/CRLF, the port freed on detach and on a kill, a refused attach that never steals, a W1 reboot through the link). Bench (`s34`): the Wizard through Intellex on W1 with no port picker (a pull showing its bauds and labels, a label push, the terminal's CR framing on the wire, the shim's mesh routing - W2 pulled once, a client never - and W2's config in parts across the link, three reloads), the split shell with both tools on the one link, and the NaviCore config tool on NaviCore's own port (auto-connect with the host's flash wired, three F5 reloads without a reset, the CONFIG line byte-identical, setSignals, and behind `intellex_nc_save` a save round trip). Over WiFi (`s35`, the PC's spare adapter joined to NaviCore's access point, `navicore_wifi`): Intellex's discovery and `ssid_for_host`, the config tool (the WiFi label, OTA over WiFi, both flash buttons and routes refused), the Wizard managing W1 and W2 through NaviCore as its relay, the rate of `?RTERM,START` re-arms once settled, a NaviCore restart through the link (the host notices, never bounces the adapter, reattaches once the PC's link carries a connect again), and attended an AP hop that the host must re-identify before it reattaches and Intellex's own adapter bounce. Flashing (`s36`): W2 through the Wizard with the bench image it runs, offline from a seeded cache - Update, Flash, one flash at a time, a refused full flash that must leave W2 in its app, and attended the Factory Reset with W2 restored from its chain - and NaviCore's app0 through the config tool, each ending on the image and config it began with. Twelve `(should)` tests pin Intellex findings 1-5 and 12-19. |
| `etm.*`, `stats.*`, `misc.*` | Reboot announce; `?ETM,CHAR` phases; ETM settings (set, persist, validate, legacy spellings); retries and failure with ACKs lost, the duplicate-ring and pending-table limits, offline detection and retry cancel, CHKSM mismatch and the 187-character limit, ETM off, per-board broadcast ACKs, the receive CRC gate from a probe; `?STATS,RPT` locally and over the mesh; `?IDENTIFY`, `?LED`. The legacy `?ETMCHAR`; `?TRACK` and its old spellings, removed, answer "Unknown command". A unicast to the controller tracked and retried, the controller's offline sweep, an ETM-off fleet on the normal path, the non-ETM `<cmdChar>M` whitelist, a forgotten peer leaving the pending table, the receive debug lines, the CHKSM single-chunk FRAG boundary (186 goes, 187 refused), a learned peer counted after its first ACK, early or late. Opt-in `etm_seq_wrap`: the counter steps over 0 and the next unicast runs; `?STATS`' USB RX-overflow line.; `?ETM,CHAR` relayed WCB to WCB (the Wizard's Network Test: the same result both ends), the per-board clamp at `?ETM,COUNT,200`, and the WCBQ guard; a peer's `came ONLINE` line printed between commands, never inside a `?backup` (audit F18). |
| `peers.*`, `wdp.*`, `alias.*`, `chars.*` | Live peer counts, live `?WCBQ`, controller off/on and auto-enable; WDP DUMP fields against config, LIST/DETAIL, POLL, OFF/ON, AUTOJOIN, ADD/FORGET, a temporary probe's join and eviction, device announces; alias sanitising and propagation; changing and restoring the delimiter, function identifier and command character. The legacy `?DON`-family debug toggles; `?WDP,CLEAR` dropping every learned peer and the neighbour table with the floor peer still reachable, and `?WDP,ADD` putting them back; `?DELIM`, `?WDP,OFF` and `?WCBQ` across a W1 reboot. Two-advert auto-join vetting and the permanent-to-temporary downgrade across reboots, a neighbour going stale after its TTL, DUMP and LIST/DETAIL fields, a controller id not re-adopted or overridden, a non-default controller id persisting, a disabled controller's commands still running; UTF-8 alias truncation, `?ETM` query forms, a large `?STATS,RPT` kept in RAM only, the prefix-character format refusals, `?ERASE` with a bad argument, the legacy `?LF` setters. A learned peer's `?WDP,FORGET` across the 5 s flush and a reboot; a learned-peer table saved under other MAC octets discarded at boot (W1 off its group for one boot); the periodic advert phased by board number, 61.0 s on W2 (re-scan #16); `?CMDCHAR` and `?FUNCCHAR` across a W1 reboot; `?CONTROLLER,OFF` across a reboot, adopted again by NaviCore's first advert. |
| `client_mesh.*`, and the mesh-mode `input.*`, `map.*`, `var.*` and `seq.*` tests in `s19` | The probe as a temporary WCB_Client: adoption and its WDP row, unicast and broadcast delivery and ACKs, sendRaw (including onto the real Maestro line), fragments and the 187-character limit, `?WHOAMI`, WCB broadcasts reaching a client, checksum and password mismatches, re-joining, the stale-peer window; client broadcasts into ports, the Maestro return path, soft-serial TX under mesh load and core-0 contention, raw chunk bounds, client-set variables, sequence fan-out seen from the mesh. A `;T` chain keeping its mesh origin; ten chains while W1 is busy, every group accounted for; `?WHOAMI` escaping `"` and `\`; RC relay drops printed with consecutive totals. |
| `ota.*` | `?OTALOCAL` status, parsing and help, session-less and usage errors, BEGIN guards; relay-side rejects and CRC handling, relay BEGIN rejects on W2, the NaviCore brick guard. Opt-in: local sessions (cursor, supersede, verify failures, bad magic, overrun, base64 errors, idle timeouts), the USB baud bump, relay sessions on W2 (cursor, teardown, END, keep-alive), a cross-transport abort, and full-image transfers (a corrupt image, a same-image re-flash of W1 and of W2 in two passes each, a wrong-chip image). Opt-in: an accepted BEGIN abandons a running config pull, and the next pull is whole. |
| `ident.*` | Identity and radio settings, changed on W1's own console and put back the same way (`s27_identity_destructive.py`): `?WCB` (W1 is WCB 3 for one boot, WDP off so nothing learns it), `?WCBCH` (W1 five channels away for one boot: `?WIFI` shows the radio channel, a unicast to W2 fails at the MAC layer; one channel away is not off-mesh at bench range), `?MAC,2` (the live receive filter, both directions), `?HW` (validated and saved; the chain reports the saved version at once and the pin map applies at the next boot, `HIL_TEST_AUDIT.md` F3); each with its argument errors and legacy spelling. `?EPASS` and `?ERASE,NVS` are attended-only (`ident.epass_live`, `ident.epass_window_gates`: with a throwaway password W2 ignores W1's pull, sequence and OTA requests); the legacy `?M3xx` and the short `?M2` / `?M3` forms; opt-in `mesh_password`: `?EPASS` with a throwaway password takes W1 off the mesh both ways at once and its own password, read from its chain, puts it back. The Maestro self-slot repair on that WCB 3 boot: a proxy `M8 -> W3` comes back as a local S1 slot, cleared before W1's WDP is back on. A packet whose sender id is not its source MAC's last octet is dropped as an id-spoof: W1 renumbered to 3 live for ~3 s, WDP off, and W2 never ACKs or runs it, and learns no WCB 3 (`ident.sender_id_mac_bound`). |
| `boot.*` | What each subsystem loads from NVS at boot, read off the boot banner and proved by behaviour after the reboot (`s26_boot_restore.py`): HCR, MP3 and DFPlayer on W2 (the `[..] Loaded` lines, an identical chain, each device's bytes, W1's learned routes), a local WLED and a remote Maestro proxy on W1; the whole boot banner on W1 and W2 (`boot.banner_w1`, `boot.banner_w2`), every line built from the board's own config chain: identity, port rows, MAC and peers, controller, WiFi, general settings, loaded devices and routes, Kyber mode, the Maestro table, learned peers against `?PEERSLIVE`. One boot per `?reboot` on W1 and W2 (one rst:, a software reset, the RTC boot-attempt counter +1) within 7 s of rst:, against the 10 s boot guard; W1's three ETM boot announces ~1.2 s apart, its first heartbeat inside ETM,BOOT, and its WDP boot burst refreshing W2's row within 6 s of the banner; the MP3/DFPlayer ONERR keys and current volumes across a W2 reboot. |
| `nvs.*` | Opt-in `nvs_erase`, attended: `?ERASE,NVS` and the legacy `?WCB_ERASE` on W1, the factory defaults it boots with (identity, MAC, peers, ports, ETM, WDP, no devices or sequences), then a full restore from W1's own chain and learned peers. The erase clears every namespace: WiFi is off and no serial device is saved afterwards. `nvs.erase_led_and_tails` also checks the saved LED pin, a command chained behind the erase, and the erased board's `?backup`. Opt-in `nvs_fill`: `nvs.full_map_and_device_save` fills NVS (with sequences, on the sequence store's NVS fallback) and checks that a mapping or device save that cannot be stored says so and nothing comes back half-saved after a reboot. |
| `bcast.*` | `?RESET_BROADCAST` (the legacy `?BCAST,RESET`): every port open again, S1 included, S0 echo off; the baseline flags replayed afterwards. |
| `wifi.*` | The `?WIFI` status block for whatever mode the board is in (W1 hosts an AP on the bench): mode and interface lines, WS endpoint, radio channel against `?WCBCH`, free heap. Opt-in `wifi_modes` (`s28_wifi.py`): W1's access point off and back, and W1 joining W2's access point and returning, each applied at boot, the mesh delivering throughout. Opt-in `wifi_pc` (attended; `ws.*` and two `wifi.*`): for each test a WiFi adapter on the PC joins W1's access point under a temporary `HIL-<ssid>` profile (`hil/wlan.py` `pc_on_ap`, which s28 shares with the NaviCore tests) - the spare TP-Link "Wi-Fi 2" here, so the PC stays online; with a single adapter the PC is offline for about 30 s a test - waits up to 60 s for a lease (tracker #110), and returns to the network it was on; W1's heap line is noted after each (#111). Over `ws://192.168.4.1/ws` (`hil/ws.py`): `?VERSION` answered; line framing, the over-long line dropped whole and the oversized frame closing the socket (`ws.line_framing`); `?backup` whole and every frame valid UTF-8, `?HELP` line for line as on USB (`ws.backup_over_ws`); three client slots, the oldest evicted, per-socket line buffers, and a socket broadcast reaching the mesh right after a mesh-received command (`ws.client_slots`); a DATA-sized `?OTALOCAL` session, with `ota_erase` too (`ws.ota_chunk`); no gateway from W1's DHCP (`wifi.ap_dhcp_no_gateway`). `wifi.join_w2_ap` checks the `[WS] command endpoint ready` line, and with `wifi_pc` ticked too has the PC join W2's access point and reach W1's endpoint on its joined address. Setter validation with no opt-in and no reboot: the open-AP guard and the other refusals save nothing; an empty AP SSID saves the derived `WCB-<alias>`; a passphrase keeps a trailing '?' (in either case of the verb), its commas and a leading space, and loses a trailing space to the line trim; each replays W1's own `?WIFI` line and compares tokens by hash. Opt-in `wifi_modes` also brings W1's AP up under the derived name for one boot (`wifi.ap_derived_ssid_boot`). Opt-in `wifi_modes` also: losing W2's access point and rejoining it; looking for an absent SSID for 70 s without leaving the mesh channel. |

Every group has run on the bench except the attended opt-in `ident.epass_live` (`mesh_password`), and NC-WP13's two L3 `nctool.webserial_*` tests, which need someone at the bench (`navicore_webserial`). Current failures, and what each one is, are in
[HIL_FIX_TRACKER.md](HIL_FIX_TRACKER.md).

**Coverage audit (2026-09-15).** Every user-facing function in the firmware — each `?` command and sub-command, legacy alias,
`;` verb form, mesh path, and boot or periodic behaviour — was listed from the code and matched to the tests that assert it, and
each area's mapping was re-checked by a second reviewer. Of 1,064 functions, 484 are tested, 198 partly (some forms untested),
343 untested but testable on this bench, and 39 not testable here (dead code, or what the bench lacks: a real Kyber brain, a
third WCB, an ESP32-S3 WCB, a light sensor on the status LEDs, fault injection). The largest gaps:

- **WiFi and the WebSocket endpoint** (`?WIFI`, `WCB_WS.cpp`): the mode changes are tested (`wifi.*`, opt-in `wifi_modes`);
  the endpoint's traffic needs the PC on W1's access point (`ws.*`, `wifi.pc_joins_ap_ws`, `wifi.ap_dhcp_no_gateway`, opt-in `wifi_pc`, attended).
- **Legacy spellings**: every one has a characterisation test; `?WCB_ERASE` is in the attended `nvs_erase` pair.
- **Identity and radio settings**: `?WCB`, `?WCBCH`, `?MAC` and `?HW` are tested (`ident.*`); `?EPASS` and `?ERASE,NVS`
  have attended opt-in tests (`mesh_password`, `nvs_erase`) that read every credential from the board's own chain.
- **Factory defaults**: only an erased board shows them, so they are checked by the attended `nvs.*` tests. The boot
  banner and each subsystem's NVS restore are tested (`boot.*` and the `*_reboot` tests).

The Wizard UI is covered by the 71 `wizard.*` tests (§8; twelve need a board) and the parser unit tests. In Kyber, only what the bench cannot reach is untested: a real Kyber brain, and the WDP
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
4. takes the port back and waits for `?VERSION` (Chrome's open/close can reset the board). The device comes
   back as the same object (`Bench.dev` reopens the one `close_device` parked), so a `WCB` wrapper the harness
   test took before the hand-off still works after it;
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
a field the parser drops rewrites a good board. `npm run unit` in `tests/wizard`, or `wizard.parser` in the harness. `unit/devices.test.js` does the same for the
device cards (MP3 Trigger, HCR, DFPlayer, WLED, Maestro), stored sequences and variables: what each token parses to, what
a change pushes, and an all-devices round trip. `unit/roundtrip.test.js` pins the fields that once failed a round trip
(an ETM delay of 0, a `,` delimiter, a disabled controller's custom id, a client slot in a system file), diffs that
must ignore a mapping row's UI-only `bidir` key and a Maestro table's key order, and `planCommandCharChange`, the order
a push changes the command characters in: checked against a model of the firmware's `delimCharOk`/`prefixCharOk` for
every combination of seven characters, including every refusal's two-push advice (`docs/hil_plan/WCB.md` §3,
W-1 to W-11).
`unit/model.test.js` walks the rest of the model a push writes (WCB-WP40): `diffConfigs` must report a change to every field a push writes, serial and PWM mappings round-trip (an added or edited mapping re-sends the whole table; a removed one builds nothing, because no push clears a mapping), `evaluatePortClaims` for each device, mapping and Kyber mode, WiFi JOIN/AP/OFF (fake SSIDs only), the WDP OFF forms, and Kyber local. Two `todo` tests pin the parser halves of W-17 and W-20.
Note `kyber.targets` (Maestros on other boards) is derived — the Wizard recomputes it from every connected board at
push time — so the round-trip comparisons leave it out.

**CI.** `.github/workflows/wizard-tests.yml` runs the unit tests and every no-board spec (`wizard.smoke`, the Kyber specs, the eleven `wizard.remote_pull_fake_*` pulls against a fake relay in the page, and the ten `wizard.push_fake_*` pushes through fake connections: a pulled card pushed unedited sends nothing, one edit sends only itself and an edit taken back sends nothing, the command characters change in an order the board takes, the reboot and network-group checks read each command, the relay size cap comes before any confirm or send), and the fake-board specs below (`push_more_fake`, `app_fake`, `editors_fake`, `flasher_fake`) on every push touching
`Wizard/**` or `tests/wizard/**`. It is deliberately separate from `build.yml` and filtered to those paths, so a
Wizard change never rebuilds firmware or publishes binaries (CLAUDE.md rule 9). The board tests need hardware and
never run there; they skip themselves without `HIL_BRIDGE`. The page is served by `tests/wizard/serve.js`, Node's
own http server, so a runner with no `python` on PATH behaves the same as this PC. `suites/s30_wizard.py` registers
every no-board spec too (`_FAKE_PULL`, `_FAKE_PUSH`, `_FAKE_MORE`), so a full run includes them and each runs by id.
The names Intellex's shim reaches into the Wizard for (INTELLEX.md §1.5) are pinned by `intellex.ui_contract_wizard` (this repo's `Wizard/`) and `intellex.ui_contract_wizard_shipped` (Intellex's bundle): a Wizard change that breaks Intellex fails there, with no board.
The Wizard's board tests also run inside Intellex: `intellex.wizard_*` (`s34`, §10) port `wizard.pull`, `push_label`, `terminal_wire` and `remote_pull_parts` to Intellex's auto-connect, where the shim also pulls W2 through W1 by itself.

**Fake boards in the page** (WCB-WP20, WP41). `tests/wizard/lib/fake.js` replaces a slot's `BoardConnection` I/O (`send`, `sendAndAwaitIdle`, `sendAndCollect`, `closeForReconnect`, `reconnect`) with scripted replies (a `?backup`, ACKs, lines per command) and records every write under the slot the connection ends in, so the Wizard's own `boardPull`, `boardGo`, `boardGoAll`, editors and panels run unmodified. `install()` wraps `showToast`, answers `confirm()` yes and parks the WDP mesh connection until `meshOn()`. A shared fake keeps the class's real close/reconnect, which return false for a portless shared connection. `lib/fake_esptool_wcb.mjs` stands in for `vendor/esptool-js` (`page.route`) for the flasher's decisions. A `(should)` spec is `test.fail(true, '<W-row>')`: CI stays green while the defect is there, the harness reports FAIL, and CI turns red the day a fix lands, when the mark comes off. `selftest.py`'s `t_wizard_spec_ids` fails on a spec title not registered in s30, an s30 id with no spec, a duplicate, or a `test.fail` spec whose title lacks `(should)`.

**The push side on the bench** (WCB-WP21, `specs/board_more.spec.js`). Before any push a spec calls `plannedVerbs(page, n)` (`lib/wizard.js`): it runs what `boardGo` runs before building (the sync functions, `autoComputeKyberTargets`, the General fields) on a copy and returns only the verbs the push would send, and the test stops on anything it did not mean to change (`wizard.push_fake_planned_verbs` checks it against real pushes). A reboot is forced with `?HW` and the board's own version, which must reboot and changes nothing (a WCBQ edit reboots nothing, D28). `connectBoardDirect` connects without the shared hub, for the paths that close and reopen the port; `manageRemote` pulls a board through the connected one; `recordLines`, `linesMatching` and `lineMark` keep board lines in the page and return only those matching the test's pattern.

**Setup and running.** Once: `cd tests/wizard && npm install && npx playwright install chromium`. Then run
`python tests/hil/run.py "wizard.*"` or pick them in the GUI. Chrome runs headed; `WIZ_HEADLESS=1` tries headless,
which is untested with Web Serial. `npx playwright test` in `tests/wizard` runs standalone: the no-board specs run (`wizard.smoke`, the Kyber specs, `wizard.remote_pull_fake_*`, `wizard.push_fake_*`, `wizard.pull_fake_*`, `wizard.app_fake_*`, `wizard.editors_fake_*`, `wizard.flasher_fake_*`), and
the board tests skip. The page is served from the repo root on `http://127.0.0.1:8778` by `serve.js`, because the Wizard
loads `../Images/`. Never change that origin: grants are per origin, so every profile would need authorizing again.

`openWizard()` turns Chrome's HTTP cache off for the page. The persistent profile keeps that cache too, and a
server that sends `Last-Modified` (`python -m http.server`, which `reuseExistingServer` adopts if one is already on
8778; `serve.js` sends none) lets Chrome reuse a cached script without asking. On 2026-09-23 that ran the previous
`parser.js` after an edit, so a test passed or failed against code that was no longer on disk.

The Wizard's globals (`boardBaselines`, `_boardPullInFlight`, `boardPushOutcome`) are read by name from
`page.evaluate`. `pushConfig()` wraps `boardGo` and awaits its promise, because `boardGo` writes a provisional
`boardPushOutcome` before it marks the push in flight, so polling the outcome can read a stale "done".

### The NaviCore config tool (`nctool.*`)

The NaviCore config tool (`NaviCore/config_tool/index.html`) is tested from the same folder (`hil_plan/NAVICORE.md`
§5, D-NC10), in `navicore/` subfolders: `specs/navicore/`, `lib/navicore/`, `unit/navicore/`, `fixtures/navicore/`,
and `tools/`. `suites/s49_navicore_tool.py` registers every id. The tool is never copied: `serve.js` maps `/NaviCore/`
to the NaviCore repo beside this one (`lib/navicore/paths.js` walks up from the repo root, so a git worktree finds it
too; `NAVICORE_REPO` overrides), and every NaviCore test skips when it is not there. Its specs use their own origin,
`http://127.0.0.1:8779` (a second `serve.js`, started by `playwright.config.js`): nothing they do reaches the Wizard's
8778 grants, and since that server serves the same tree, the tool and the Wizard still share an origin there.

Four layers:

| Layer | What runs | Ids | Where |
|---|---|---|---|
| L0 | node only: `unit/navicore/static.test.js` (every inline script compiles, `flasher.js` and `serial-hub.js` pass `node --check`, the firmware/tool constant pairs of NaviCore's `CONFIG_SCHEMA.md` §6 and the field caps, every key `rcConfigToJSON` prints is read by `applyConfig`, Intellex's `src/webui` is byte-identical, the shared-hub protocol matches `Wizard/serial-hub.js`, what `intellex_shim.js` drives exists); `unit/navicore/unit.test.js` (the page's own pure functions, lifted into a `vm` sandbox by `extract.js`: the save diff, `_fragChunks`, the command-library codec over every command, `_seqValueToLines` against the Wizard's, `parseCsv`, `_cfgExtractConfig`); `unit/navicore/rig.test.js` (the rig's model of the firmware, and `lib/navicore/contract.js` against lines put together from the firmware's printf formats) | `nctool.static`, `nctool.unit` | `npm run unit`, CI, the harness |
| L1 | the real page, with a fake `navigator.serial` backed by an in-Node NaviCore emulator | `nctool.<spec>` | `npx playwright test specs/navicore`, CI, the harness (no bench) |
| L2 | the same fake port piped to the REAL NaviCore through the harness's own COM handle (W1's, for the two Via-WCB tests) | `nctool.board_*`, `nctool.emulator_contract` | the harness only, inside `nc_guard` |
| L3 | real Web Serial on NaviCore's own port, in the persistent profile `.profiles/navicore` (origin 8779), and esptool-js | `nctool.webserial_*` | the harness only, attended: opt-in `navicore_webserial` |

**The fake port** (`lib/navicore/shim.js`) is installed with `context.addInitScript`, before the page's scripts, as
`Object.defineProperty(navigator, 'serial', ...)` (a plain assignment throws under strict mode; Intellex's shim hit it).
It follows the Web Serial behaviour the tool relies on: `readable`/`writable` are made while the port is open; a
reader's cancel or a recoverable read error (BreakError, FramingError, ParityError, BufferOverrunError) leaves a fresh
stream, a fatal one (NetworkError) leaves `readable` null and every write failing; `close()` refuses while a stream is
locked; a `disconnect` event reaches `navigator.serial`'s listeners with the port as its target. `setSignals()` is
recorded and never applied. Bytes cross to Node through `exposeBinding` and back through `page.evaluate`, in order; one
page holds a device open at a time, as with a real port. `FakeSerial.events` and `window.__hilSerial.log` record every
open, close, write (with the page's own timestamp) and setSignals; `FakeSerial.events` also names the page (`pg`, in order
of first contact), so a two-tab spec (the shared hub) can tell the tab that owns the port from the one that follows.

**The emulator** (`lib/navicore/emulator.js`) answers as the firmware does, each branch citing the code it copies:
`direct` (NaviCore on USB: `processInputLine`), `via-wcb` (a tethered WCB with NaviCore at mesh id 20: `;w20,<json>`
reaches `rcTelemetry::handle`, replies come back as the bridge prints them, downloads fragmented as `_startFragSend`
slices them, `;w20,<cli>` as `[TERM:20]` lines) and `doorway` (a WCB the tool was told is a NaviCore: bare JSON is
answered in the relayed shapes). Its config is `lib/navicore/model.js`, a port of `rcConfigLoadDefaults`,
`rcConfigFromJSON` and `rcConfigToJSON` that keeps the firmware's sparse printing, its per-branch merge (a named mapping
is rebuilt from scratch, an omitted key is left alone) and ArduinoJson 7's type-strict `|` (an integer is not a bool,
a number is not a string). So "what the board holds after this Save" is checkable. A spec bends it with `hold`, `delay`,
`override`, `inject` and `injectRaw`; `rx` holds every line it received. `rig.test.js` holds the model and the emulator to
those rules. Two more parts of the firmware ride on it: `lib/navicore/ota.js` (`?OTALOCAL` direct; via WCB, the relay's
`?OTA` with its CRC-32 check in front of NaviCore's target; after a verified END the board restarts: off the bus, so
`FakeSerial` refuses `open()` while the device's `present` is false, then deaf while it boots, then on the other slot)
and `lib/navicore/clips.js` (`?REC` over a clip store, with the ranged and batched download and the indexed upload;
relayed replies wrapped at 160 bytes as the RTERM sink wraps them). Each has fault hooks (`emu.otaFault`,
`emu.clipFault`: lost lines and ACKs, mangled lines, coalesced and split markers, stale and foreign ACKs, a held window,
a truncating buffer) and a log (`otaLog`, `recLog`) the specs read.

**The fixture** `fixtures/navicore/config.bench.json` is shaped like the bench NaviCore's config (34 mappings, the
Maestro, knob and switch layout of `NAVICORE.md` §1.4) and generated by `tools/make_nc_fixture.js` through the model, so
it has the firmware's exact printed form. Every credential in it is a fixed `HIL...` placeholder. When the bench is
free, `tools/scrub_nc_config.py --from tests/hil/results/<run>/navicore_snapshot.json` replaces it with the real
config: one placeholder per distinct secret value (so a profile that holds the live mesh password still matches it),
and it refuses to write if any original secret, raw or JSON-escaped, is still in the output. Specs read the file rather
than assuming its content.

**Rules the specs keep.** Every request that leaves 127.0.0.1 is aborted and recorded (Google Fonts, api.github.com,
the esptool CDN, the cloud-backup Worker); a spec that needs one mocks it. `lib/navicore/firmware.js` mocks the Firmware
tab's: GitHub's `firmware/` listing (with a decoy for each wrong pick the flasher guards against) and raw downloads, and
stand-ins for CryptoJS and esptool-js (`fake_cryptojs.js`, `fake_esptool.mjs`) that record what the tool hands them.
Specs are headless (`NCTOOL_HEADED=1` shows Chrome), get 90 s each and a 15 s action timeout, and open the page with the HTTP cache off and Playwright's clock
installed, so the 4 s connect settle, the 12 s save watchdog or the 10 s keep-alive are stepped rather than waited
out. Nothing reads the terminal wholesale: it holds the CONFIG echo, and `termLines()` drops any CONFIG or password line.
L2 specs read counts, booleans and key names, never a value, and never press Save while a diff exists.

**`(should)` specs** assert what the tool ought to do and carry `test.fail()`: standalone and in CI they count as
expected failures (green), and they turn red the day the fix lands, which is the cue to remove the marker. The harness
reads the last result's status, so it reports them FAIL until then, like every `(should)` test (§6). In node the
equivalent is a `todo` test: it does not fail the run, and `run_unit_tests` notes it in `session.log` by name.

**L2, the pipe.** `run_wizard_test(bench, id, device="navicore", pipe=True)` keeps the device's port open in the harness
instead of handing it to Chrome, so the tool's own open cannot reset NaviCore's native-USB S3, and every line passes
through `SerialDevice` and `Bench.log`'s credential filter. The page's fake port is backed by `lib/navicore/pipe.js`,
which posts whole lines to the bridge's `/serial/write` (paced past 512 bytes, as the tool paces them) and polls
`/serial/read` every 20 ms; `/serial/signals` records a setSignals call and never applies it; `/sbus` sends one
RAM-only controller verb. These routes serve only the piped device, outside the bridge's one-at-a-time lock
(`SerialDevice` has its own). `_reacquire` PINGs a NaviCore afterwards. `nctool.board_connect_config` is the pipe's
proof: the tool connects to the real board, applies its CONFIG, and a Save right after sends nothing. `pipe.js` keeps
what the page wrote by kind and never by content: `sentTypes` (each JSON line's type), `sentLog` (`bare`, `wrapped`,
`frag`, `cli` or `text`, with the type), `sentFrags` (each fragment envelope's `f`/`of`/`sid` and size) and `sentCli`
(the first two comma fields of each `?`/`#` line).

**The L2 tests** (`specs/navicore/board.spec.js`, `board_wcb.spec.js`, `contract.spec.js`). The harness hands each spec
what it must not work out for itself in `run_wizard_test`'s args: a free button slot, a marker, a label, the bench
image's path, and what it read of the board (names, counts, a hash; never a config value). `board_save_one_field` types
a HIL label into a port-label key NaviCore has not set and saves; the harness then finds GET_CONFIG changed by that
label alone. `board_test_action_wire` fires a free slot's action row (`;S2<marker>` to WCB 1) with the row's Test;
W1 S2's probe must see the marker once and the config must be byte-identical without the guard's help.
`board_live_grid` moves the controller's rx stick through `/sbus` to counts the harness checked with `#L09` first and
reads them in the channel grid; the stick is centred in `finally` on both sides. `board_clips_list` and
`board_cmdlib_load` are read-only: the Clips panel against the harness's own `?REC,LS`, and "Load from NaviCore"
against GET_CMDLIB's board ids and FNV-1a ("Save to NaviCore" is never pressed, NAVICORE.md D-NC13).
`board_ota_usb_same_image` (`navicore_ota_full`) serves the bench image NaviCore runs as GitHub's whole firmware
listing, lets the tool update over USB and reconnect by itself across the restart, and puts the boot slot back with
hil/ncflash. `emulator_contract` opens no page: `lib/navicore/contract.js` sends the read-only request set to the pipe
and to the emulator and compares the replies by shape (types, key sets, the CLI markers' order), and the emulator's
config model must print the board's own GET_CONFIG back byte for byte; a difference is named by key path and value
type. Through W1 (`device="wcb1"`), `board_via_wcb` connects "Via a WCB", pulls the bridged CONFIG and saves every free
port label, 24 characters each, which sendJSON must send as fragment envelopes of at most 187 bytes; and the `(should)`
`board_usb_probe_no_broadcast` watches the W1 ports that a bare console line of the harness's reached while "Connect via
USB" probes the port (NAVICORE.md D-NC70).

**A bridged CONFIG never reaches session.log.** Through a WCB the tool pulls NaviCore's whole CONFIG, passwords and the
AP's name included, and the WCB prints it on USB as ~100 fragment envelopes; a WCB's lines are not redacted by kind,
and a password can straddle two envelopes. So while a device is piped, `run_wizard_test` wraps its log in
`hil/wizard.py` `PipeLog`: every envelope's slice is logged as its length, every line passes through `redact_text`, and
once the page has closed it waits until no envelope has come for 2 s (at most 30 s) before it lets go. The page's own
copy is untouched. For `nctool.*` ids it also sets `PLAYWRIGHT_NO_COPY_PROMPT`, Playwright's switch for the page
snapshot in a failed spec's `test-results/.../error-context.md`, which would otherwise hold the terminal's CONFIG echo.

**L3, real Web Serial** (`specs/navicore/webserial.spec.js`, fixtures in `lib/navicore/webserial.js`; attended, opt-in
`navicore_webserial`). NaviCore's port goes to Chrome as a Wizard board's does, in the persistent profile
`tests/wizard/.profiles/navicore` on the tool's origin 8779. The first run puts a *HIL: Pick COMx* button on the page and
opens Chrome's port dialog filtered to NaviCore's USB ids; pick the port it names. A cancel or three minutes without an
answer skips, and `WIZ_NO_AUTHORIZE=1` skips without the dialog. The spec then connects through the tool's own
`openPortAndStart(port, 4000)` on the granted port (the "Connect via USB" path minus Chrome's native chooser, which
Playwright cannot answer), with the real 4 s settle: Chrome's open asserts DTR/RTS and restarts NaviCore, and the boot
must fit inside it. The SBUS controller is the same ESP32-S3 USB-Serial/JTAG (303A:1001), so the harness holds its port
meanwhile: a grant of it can only fail to open. `webserial_connect_reset` proves the restart by NaviCore's uptime and
that the connect still comes up direct, on NaviCore's version, with nothing to save. `webserial_flash_same_image` runs
the tool's Full Wipe & Flash from that session, with GitHub's listing served as the bench image, the bench build's
partition table (which must be byte-identical to the one NaviCore publishes in its `firmware/`) and NaviCore's custom
bootloader, and esptool-js 0.4.7 and CryptoJS 4.2.0 served from this repo's `Wizard/vendor` copies; the harness requires
NaviCore to run the bench image and to have `partitions.csv`'s layout first. Afterwards the config, the command library
and the clips must be byte-for-byte what they were, and NaviCore runs the bench image from app0 (hil/ncflash puts the boot
slot back when it had been on app1). The NVS erase loses NaviCore's learned peers, which `nc_guard` re-learns through
W1's adverts. A flash that stops half-way leaves NaviCore in its ROM bootloader: `python -m hil.ncflash recover
--allow-esptool` (from `tests/hil`) writes the bench image back.

**Running.** `cd tests/wizard && npx playwright test specs/navicore` (L1, about five minutes; the L2 and L3 specs skip),
`npm run unit` (L0 with the Wizard's), or `python tests/hil/run.py "nctool.*"` (L0 and L1 need no bench;
`nctool.board_*` and `emulator_contract` need `navicore`, the two Via-WCB tests `wcb1` too, `board_live_grid` `sbus`;
L3 needs `navicore_webserial` and someone at the bench). `NCTOOL_ORIGIN=http://127.0.0.1:<port>` moves the L0-L2 specs
to another origin, for a second checkout whose no-board run must not share a server with a bench run on 8778/8779
(`reuseExistingServer` would serve the other tree); it needs a Playwright config whose servers use that port, and L3
never takes it. CI: `npm run unit` and `npx playwright test` already include them, but CI checks out only this repo, so
the NaviCore tests skip there (the rig's own node test runs); running them in CI needs a NaviCore checkout step and
`NAVICORE_REPO`.

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

- **A harness error** pauses the run by itself (checkpoint reason `harness_error`, with the exception in
  the reason text) instead of ending it: `run.py --resume --force` continues from the next test. Editing
  a suite file during a run once caused one. The runner reads each test's source with
  `inspect.getsource` to find its wires, and that re-reads the FILE: after a mid-run edit it read
  shifted lines, and run `20260927-174702` paused at 448/499 on a `TokenError`. `run_tests` now reads
  every test's source at the start of each segment (`hil/runner.py`), so a suite may be edited while a
  run goes; the edit takes effect at the next segment.

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
- `config_ref`, the saved-config reference;
- `navicore`, NaviCore's snapshot record, once a test has written NaviCore's config: a hash of its redacted config
  text, the guard's state (`guarding` while the test runs, then `restored` or `not_restored`), the test and a
  sequence number. The exact snapshot is `navicore_snapshot.json` beside the checkpoint. That file is written first,
  with the same sequence number, so the newer of the two always tells the state, even when a GUI closed with *No*
  froze the checkpoint while the test ran on.

In `config_ref`, the mesh password and WiFi credentials (`?EPASS,<pass>`,
`?WIFI,AP|JOIN,<ssid>,<pass>`) are stored as `<prefix><redacted:sha256[:12]>`. Comparisons and
diffs only ever use that form. The free-text fields quote board output: a result's or outage's
detail, the pause reason and the last resume error. They pass through the same redaction
(`redact_text`), which also covers `Password: <pw>` lines, the probe's `MESH JOIN ... PASS=<pw>`
(also masked in the probe's own error) and the SBUS controller's `wifiNets`.
So do the lines the pause and resume code adds to `session.log`, and every line NaviCore and the SBUS controller send
or receive, whose JSON password fields are hashed too (`Bench.log`, §5). A WCB's serial traffic in the log still
carries the plain password in its `?backup` lines, as it always has.

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
   Then **NaviCore's saved config** against the run's NaviCore snapshot (`resume.check_navicore`). When its record
   says a guarded test was cut off (`guarding`) or could not restore it (`not_restored`), the restore `nc_guard`
   would have made is offered, and recommended. The automatic resume makes it without asking. A restore that leaves
   the config different blocks, naming the key paths. Otherwise the config is compared, and a difference is soft, as
   above: *Yes* keeps NaviCore's config and makes it the run's reference. Nothing is restored from a snapshot
   another run took, one that fails its own hash, or an older one holding a config the checkpoint did not record.
   A run in which no test wrote NaviCore's config has no snapshot, and this half is skipped with a note.
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
    it. Switches and sliders are left as they are. On an image with the INF8 test verbs
    (NAVICORE.md INF8) its test state goes back first as a reset would - flags 0, the stream on,
    the saved frame format (`SbusCtl.clear_faults`) - and afterwards every switch is re-sent at
    its position, which ends a raw `ch` value on its channel; an older image gets only the probe.

After a clean pause nothing else needs re-establishing: a reboot clears the WCBs' RAM-only state
(`?DEBUG`, `?RTERM`, `;P` pins, queues). ETM and WDP rebuild from heartbeats, which step 5 waits
for. After a cut-off test, step 4 covers what that test may have left on. Two things are not
checked at all: a Maestro's servo positions or running script, and the SBUS controller's saved mode, switches and
sliders. Module-level caches keyed per Bench, such as
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
- NaviCore: close the GUI with *No*, or press Ctrl+C twice, during `nccfg.guard_selftest`. The resume restores NaviCore
  from `navicore_snapshot.json` ("NaviCore restored after the cut-off nccfg.guard_selftest (diff)"), and the re-run
  passes.

---

## 10. Intellex tests (`tests/intellex`)

Intellex (github.com/greghulette/Intellex) is the desktop companion for NaviCore: one aiohttp host that serves the
NaviCore config tool at `/` and the Wizard at `/wcb/Wizard/`, and bridges each page's `/_link` WebSocket to ONE
transport - a COM port or a droid's `ws://<ip>/ws`. `suites/s32_intellex.py` tests it through `hil/intellex.py`:

1. **Stage** (`stage()`). `<out>/ixstage/<id>/` gets a copy of Intellex's `src/` and `tools/` (never Greg's checkout),
   the two tool bundles, and its own `appdata/`, which the host gets as `LOCALAPPDATA`. Bundles: `worktree` (the
   default) copies NaviCore `config_tool/` and this repo's `Wizard/`, with the file lists read from Intellex's own
   `tools/fetch_webui.py`, so a Wizard change is tested inside Intellex before it ships; `shipped` copies Intellex's
   bundles; `none` leaves them out (the 503 pages). The path is short on purpose: Windows' 260-character limit.
2. **Leash** (`IntellexHost`). Every host starts with `INTELLEX_OFFLINE=1`, `INTELLEX_SERIAL_ALLOW` naming only the
   test's own port (usually none), `INTELLEX_DISCOVER_HOSTS` empty (Intellex's CLAUDE.md "A test host needs a leash")
   and `--no-auto-bounce`. No Intellex host touches another bench port, GitHub or the PC's WiFi, so the no-board ones
   run even while a bench run holds every COM port. The WiFi tests (`s35`) put the PC's spare adapter on NaviCore's
   access point through the harness's own `hil/wlan.py` `pc_on_ap`, never through Intellex, and take every network
   name out of what the host prints and logs (`hide`).
3. **Checks.** HTTP and `/_link` straight from the harness (urllib, and `hil/ws.py`, which keeps binary frames and can
   send an Origin header); Intellex's own smoke tools under its venv (`run_venv`: `python.exe`, never the venv's `.exe`
   shims); Playwright specs in `tests/intellex/specs` through `run_intellex_test()`, always headless - Intellex's pages
   talk to the host, not to Web Serial, so there is no port grant and no one needs to be at the keyboard. Specs use
   `lib/fixtures.js`, which records page errors and failed requests and answers the routes that would reach past the
   host (identify, discover, wifi-bounce, the updates) unless a spec allows them.
4. **Teardown.** Detach, kill the host's process tree (a host left running reopens its target every second, forever),
   copy its log next to the report (`intellex-logs/<id>`).

A test skips while another Intellex runs (Greg's app, or a host from a source checkout): one attached to NaviCore's AP
made NaviCore re-arm W1's terminal every second (tracker #93). `intellex.origin_guard` leaves `/_api/wifi-bounce` out
on purpose: were the Origin guard broken, that request would run `netsh` against the PC's WiFi.

The plan is IX-WP1 to IX-WP14 in [hil_plan/INTELLEX.md](hil_plan/INTELLEX.md). Done: the hooks (Intellex
`e9f95f2`), the plumbing, IX-WP3 (the API, guard, docs and proxy tests, and the venv checks in
`tests/intellex/py`) and IX-WP4 (the Playwright specs), all without a board, in `s32_intellex.py`; and, on the
bench (`20260929-043806`), IX-WP5 and IX-WP6 in `s33_intellex_bench.py` (Intellex's transports, and the bridge end
to end with raw `/_link` clients); and IX-WP7 and IX-WP8 in `s34_intellex_tools.py` (`20260929-110148`: both tools through
Intellex on the bench boards); IX-WP9 in `s35_intellex_wifi.py` (over WiFi through NaviCore's access point,
`20260929-202852`: five pass, the two attended skip, and `wifi_link_loss`, which found finding 19, reworked since and
not yet re-run) and IX-WP10 in `s36_intellex_flash.py` (flashing W2 with the image it runs, and NaviCore's app,
`20260929-203453`: four pass, finding 18's `(should)` fails as designed, the attended Factory Reset not run). A spec
that must time a step with the bench asks the harness through a bridge hook (§5). Next: OTA through Intellex
(IX-WP11).

## Revision log

| Date | Commit | Change |
|---|---|---|
| 2026-10-04 | _(pending)_ | **W-13 fixed** (`hil_plan/WCB.md` §3): the Wizard's immediate sends read the board's live function identifier (`_liveFuncChar`) and command character (`_liveCmdChar`), never the General value not yet pushed. `wizard.app_fake_pending_funcchar` passes and guards it (its `test.fail` is gone). |
| 2026-10-04 | _(pending)_ | `hil/wlan.py` `pc_on_ap(reach=)`: a join's lease must also carry a TCP connect to the access point, or the harness renews the lease (`ipconfig /release`, `/renew` on that adapter only) and then re-associates once; s45 `_on_ap` asks it of NaviCore's port 80, for every `ncwifi` and Intellex WiFi test. In `20261004-125412` three NaviCore joins held a lease and carried nothing; `20261004-130438` passed all six unattended Intellex WiFi tests with it. IX-WP9/10 bench-verified (`hil_plan/INTELLEX.md`). |
| 2026-10-04 | _(pending)_ | **Intellex: the full run's three W2 flash failures** (`20260929-203948`): esptool's write never reached W2's ROM loader ("Wrong boot mode detected (0x17)", 38 tries) while probe2 was bound to W2's ports, where the same flashes passed in `20260929-203453` with no probe bound. `s36` releases the probe channels on W2's ports before every esptool reset (`park_probes`), and `flash_board.spec.js` judges the host's flash before it waits for the re-pull (§5; INTELLEX.md DX47). |
| 2026-09-29 | `d402aba` | **Intellex IX-WP9 and IX-WP10 on the bench** (`hil_plan/INTELLEX.md`; runs `20260929-202852`, `20260929-203453`): 5 WiFi and 4 flash tests pass, the attended ones skip, `flash_refused_board_runs` fails as designed (finding 18 confirmed; findings 13 and 14 too). `wifi_link_loss` failed on two counts. Finding 19: the host dropped the dead WebSocket on its event loop and stalled 10 s, so the loss showed at 21.9 s; its board-free `(should)` is `intellex.link_drop_no_stall` (`s32`, `hil/ws.py` `WsEndpoint` on 127.0.0.2:80), and the test's notice limit is now 30 s. And the PC's adapter was never back: the REBOOT deauthenticated nobody and Windows kept the stale association 90 s with no WLAN event, while `rejoin` took "connected with a lease" for back. `hil/wlan.py` `rejoin(reach=)` now wants a TCP connect and re-associates when there is none (`reach_wait`), and the reattach is timed from that proof, 30 s (§5; INTELLEX.md DX45, DX46). `selftest.py`: `t_ws_endpoint_drop_stall`, and rejoin's reach cases in `t_intellex_wifi_flash_helpers`. |
| 2026-09-29 | `fecd481`, `8e5d24f` | **Stored sequences in a file of their own (`WCB_SeqStore`, [SEQUENCE_INVENTORY.md](SEQUENCE_INVENTORY.md) §3d, D73), bench-verified (`20260929-194057` (81 of 86; the five failures triaged), `-200710` (43 of 44) and `-201645` (4 of 4)).** New `seq.store_accounting`, `seq.store_full` (W2 pulls W1's whole config with the store full) and `seq.store_moves_from_nvs` (2 reboots). `seq.hash_algorithm` builds the key list from `?SEQ,NAMES`; `seq.get_internal_keys` expects NOTFOUND; `seq.clear_all_empty_hash` and `seq.reserved_bookkeeping_keys` read `stored_cmds` through `?NVS`; a save the store refuses for room skips as NVS's did. The `nvs_fill` tests fill NVS on the store's NVS fallback (`?DEBUG,SEQNVS`, two more reboots). The three erase tests expect the erase line that now names stored sequences, and `inv.seqget_formats` asks again for a SEQVAL reply that did not come (sent once, unacknowledged: it failed `-200710` that way). The first run's `backup.one_line_restore` failure was the store's: held mounted 3 s after a `?backup`, it cost the 250-token chain four tokens to out-of-memory, and `8e5d24f` unmounts at the end of every call. |
| 2026-09-29 | _(pending)_ | NC-WP11's first bench run (`20260929-201950`): five s50 tests read rc_trig as `(mode, btn, tap)` where s42 `_taps` gives `(host time, tap)`, now fixed (and `_taps` documented); `failsafe_deferred_tap`'s case A raises the flag 150 ms after the release. `selftest.py`'s `NaviSbusModel` runs NaviCore's SBUS engine, so those five run in `t_sbusfault_suite_against_model` too, with six more mutations. §5's s50 bullet (how rc_trig is read) and a §6 row: `#L09` counts loop passes, not frames. |
| 2026-09-29 | `153e46d` | **INF8 built and NC-WP11 written, neither flashed nor bench-run (`NAVICORE.md`).** SBUSController's RAM-only test verbs on its local `hil-week` branch (`de99467`, image `20260929-de99467-hil`). `hil/sbus.py`: `SbusCtl.test_state`, `test_verb`, `flags`, `stream`, `sbus16`, `glitch`, `channel`, `clear_faults`, `reassert_switches`; `ReaderModel`, `glitch_bursts`, `reader_decodes`. `NaviCore.sbus_dump` ends at the last row #L09 prints, so an SBUS-16 dump no longer times out. `hil/resume.py` clears the controller's test state after a cut-off `sbus.*` test (§9). New `s50_navicore_sbus_faults.py`: 12 `sbus` tests, four `(should)` (D-NC21 twice, D-NC72, D-NC73), all in `hil/servos.py`, `sbus.test_verbs_ram_only` behind `sbus_reset`. §1's controller rule, §5's `SbusCtl` text and s50 bullet, two §6 behaviour rows, four `(should)` rows, two doc-disagreement rows, §7's `sbus.*` row. `selftest.py`: `t_sbus_inf8_verbs`, `t_sbus_reader_model`, `t_sbus_dump_variants`, `t_sbusfault_helpers`, `t_sbusfault_old_image_skips`, `t_sbusfault_suite_against_model` (`SbusWorld`, `NaviSbusModel`) and `t_sbusfault_mutations` (seven); `t_sbus_released_after_cutoff` covers both images. |
| 2026-09-29 | _(pending)_ | **Intellex IX-WP9 and IX-WP10** (`hil_plan/INTELLEX.md`), written and dry-run, not yet run on the bench: 13 tests. `suites/s35_intellex_wifi.py`: discovery, the config tool and the Wizard through NaviCore over WiFi, the `?RTERM,START` re-arm rate (DX10), a NaviCore restart through the link (`intellex_reboot` with `navicore_wifi`), and the attended AP hop and adapter bounce (`intellex_wifi_join`); the spare adapter joins NaviCore's access point through `pc_on_ap` behind `navicore_wifi` (DX34), and `intellex.ws_transport_navicore` now joins too. `suites/s36_intellex_flash.py`: W2's Update, Flash and one-at-a-time and the `(should)` `intellex.flash_refused_board_runs` (finding 18: a flash refused after detection leaves the board in its ROM loader) behind `intellex_flash`, the attended Factory Reset (`intellex_flash_factory`), NaviCore's app (`intellex_flash_navicore`); identity-preserving with the bench image W2 runs, read at run time. `run_intellex_test` gains `seed`, `hide` and `hooks` (the bridge's new `/hook` route, `board.js` `hil.hook`); `IntellexHost` and `copy_logs` take network names out; `hil/intellex.py` `main_checkout`, `builds_dir`; `hil/wlan.py` `rejoin`. Specs `wifi_board.spec.js`, `flash_board.spec.js`; `tests/intellex/py/wifi_units.py`; `transport_ws.py` fails instead of skipping once the harness has joined. Four opt-ins registered, unticked (§2), and the `navicore_wifi` and `intellex_reboot` lines widened; two NaviCore restarts in `hil/servos.py`; a `(should)` row in §6. `selftest.py` gains `t_bridge_hooks` and `t_intellex_wifi_flash_helpers`. |
| 2026-09-29 | _(pending)_ | **The NaviCore config tool with the board (`NAVICORE.md` NC-WP13), bench-verified (`20260929-172500`, `-172549`, `-173044`: nine L2 pass, D-NC70 and D-NC71 confirmed; L3 attended). The board tests serve `bench.json` `navicore_repo` (D71, `hil/wizard.py` `navicore_repo_env`); the probe spec waits for the direct phase to end either way (a relay window left open made the tool take a mesh PONG for a direct link).** §8: four layers now; the L2 tests (`nctool.board_*` through NaviCore's port, `emulator_contract` with no page, `board_via_wcb` and the `(should)` `board_usb_probe_no_broadcast` through W1's) and the attended L3 `nctool.webserial_*` over real Web Serial, opt-in `navicore_webserial` (registered; §2). New `lib/navicore/contract.js`, `lib/navicore/webserial.js`, `specs/navicore/board_wcb.spec.js`, `contract.spec.js`, `webserial.spec.js`; `pipe.js` records the page's writes by kind; the emulator's MESH_STATS and `?OTALOCAL,STATUS` now follow the firmware; `NCTOOL_ORIGIN` moves the fake-port layers to another origin. `hil/wizard.py` `PipeLog` keeps a piped device's fragment envelopes out of session.log, and `nctool.*` specs run with `PLAYWRIGHT_NO_COPY_PROMPT`. Two `(should)` rows in §6 (NAVICORE.md D-NC70, D-NC71; `nctool.via_wcb_nothing_bare` is L1), an `nctool.*` row in §7, four tests in `hil/servos.py`. `selftest.py` gains `t_nctool_spec_ids` and `t_nctool_pipe_log_filter`. |
| 2026-09-29 | _(pending)_ | NaviCore's recorder (`NAVICORE.md` NC-WP12), bench-verified (`20260929-173541`, `-173612`, `-173628`: nine pass, D-NC64 to D-NC66 confirmed, Greg's clips unchanged): `s48_navicore_rec.py` (12 `ncrec` tests, three `(should)`: D-NC64 to D-NC66) with its `rec_guard`; opt-in `navicore_clip_write` (§2; registered, not ticked); the six tests that replay in `hil/servos.py`; a §5 bullet, two §6 rows, three `(should)` rows and three doc-disagreement rows, the §7 `ncrec.*` row (and §7's not-yet-run line, where `ncwifi.*` has run since). `selftest.py`: `t_ncrec_helpers`, `t_ncrec_suite_against_model` and `t_ncrec_mutations` (`NaviRecModel`, 15 mutations). |
| 2026-09-29 | `cf93348` | Audit F18 and F19 fixed in the firmware and bench-verified (`20260929-114235`): `etm.came_online_outside_backup`, `wcb.backup_lost_line_warns`, `wcb.pull_error_lost_line` (§7's `wcb.*` and `etm.*` rows); the RAM-only knob `?DEBUG,PULLFAULT,LINE`. |
| 2026-09-29 | _(pending)_ | **Intellex IX-WP7 and IX-WP8** (`hil_plan/INTELLEX.md`), bench-verified (`20260929-110148`: 13 of 15 pass; the finding 4 and 17 `(should)` tests fail as designed, and finding 16's passes: W1 restarted under the Wizard's DTR booted its app): 15 tests in new `s34_intellex_tools.py` (specs `wizard_board.spec.js`, `nc_board.spec.js`, `lib/board.js`); `run_intellex_test` gains `link_check` (the rawLink) and `recover`, with `boot_check` and `reset_into_app` (§5); opt-in `intellex_nc_save` (§2, ticked, D67); three §6 rows; three `(should)` rows (findings 4, 16, 17); the `intellex.*` coverage row (§7); a §8 line; §10's status. |
| 2026-09-29 | _(pending)_ | **Wizard tests WCB-WP20/21/40/41 (§8, `hil_plan/WCB.md`), bench-verified (`20260929-101257`, `-104602`, `-105553`).** No board: `specs/push_more_fake.spec.js`, `app_fake.spec.js`, `editors_fake.spec.js`, `flasher_fake.spec.js` (33 `wizard.*` ids, `_FAKE_MORE` in `s30_wizard.py`) on `lib/fake.js` and `lib/fake_esptool_wcb.mjs`; `unit/model.test.js` (nine tests, two `todo`). Bench: `specs/board_more.spec.js` with `wizard.push_reboot_path`, `push_reboot_path_direct`, `push_all_relay`, `mapping_bidir_relay`, `seq_var_editors`, `wdp_da_forget`, `relay_terminal`; each push checks its verbs first (`plannedVerbs` in `lib/wizard.js`, with `connectBoardDirect`, `manageRemote`, `recordLines`). Nine Wizard defects, W-13 to W-21, not fixed: twelve `(should)` rows in §6 (nine `test.fail` specs, two node `todo`s, `wizard.mapping_bidir_relay`). `selftest.py` gains `t_wizard_spec_ids`. 71 `wizard.*` tests: 61 pass, the ten `(should)` ones fail as designed. The bench found four harness faults, fixed: a device handed to Chrome came back as a new object, so a wrapper taken before the hand-off raised 'COM6 is gone' in a `finally` and skipped its `?RTERM,STOP`; the `?RTERM` session left running put W2's `?backup` on W1's console, read as W1's own (a CRC mismatch); two specs waited on a `b<n>-conn-label` no card has; the mapping spec removed a row the post-save pull had rendered again, and the device-list spec looked before W1, reset by Chrome's open, had heard W2's list (it now presses Poll mesh). `selftest.py` gains `t_parked_device_reused`. |
| 2026-09-29 | _(pending)_ | NaviCore's SoftAP and WebSocket (`NAVICORE.md` INF5, NC-WP8), bench-verified (`20260929-114842`, `-114920`, `-120603`, `-120840`: twelve pass, the three `(should)` fail as designed; two test-side fixes on the bench, a missed reconnect to the access point under test noted rather than failed, and a NaviCore restart for a baseline a stale socket cut): `hil/wlan.py` (s28's PC-side helpers moved unchanged, `pc_on_ap`'s `spare_only`, a scanned network list), `hil/ncws.py` (`NcWs`), `hil/ws.py` `rcvbuf`; `s45_navicore_wifi.py` (15 `ncwifi` tests, three `(should)`: D-NC61 to D-NC63) and opt-in `navicore_wifi` (§2, §5); a §6 console-mirror row, three `(should)` rows and four doc-disagreement rows; a §7 `ncwifi.*` row, and the `wifi.*` row names `pc_on_ap`. |
| 2026-09-29 | _(pending)_ | WCB-WP22 finished: `ws.line_framing`, `ws.backup_over_ws`, `ws.client_slots`, `ws.ota_chunk`, `wifi.ap_dhcp_no_gateway`, and `wifi.join_w2_ap`'s ready line and PC half (§7's `wifi.*` row). `_pc_on_ap` waits for the lease and scrubs network names from its notes; `hil/ws.py` keeps each text frame's bytes (`text_frames`). |
| 2026-09-29 | `7f80b87` | WCB-WP24 finished: `etm.char_relay_roundtrip`, `etm.char_per_board_clamp`, `etm.char_guard_wcbq` (§7's `etm.*` row). |
| 2026-09-29 | `66e9d15` | WCB-WP34 finished: `wcb.cmd.legacy_wcb_wcbq_spellings` (the legacy no-comma `?WCBQ<n>` and `?WCB<n>`). |
| 2026-09-29 | `38ea101` | WCB-WP29 finished: `serial.timer_replaced_mid_run` and `serial.timer_inline_payload` (§7's `serial.*` row). |
| 2026-09-29 | `f521d43` | NC-WP6's second bench run (`20260929-042105`): a `(should)` row for D-NC48 (`ncmesh.mgmt_frag_multichunk`); §6's relayed-reply row names the s43 re-asks, and a row for the `ETM,CHAR` reply's bare tag line. |
| 2026-09-29 | `c7af35b` | **Intellex IX-WP3 to IX-WP6** (`hil_plan/INTELLEX.md`): 35 tests. `s32` gains the API, docs, proxy and venv checks (`tests/intellex/py`: flash rules, a fake-esptool flash pipeline, GitHub retries, paths and logs) and nine Playwright specs; new `s33_intellex_bench.py` (Intellex's transports on W1 and NaviCore, the bridge end to end); opt-in `intellex_reboot` (§2); `hil/intellex.py` `run_intellex_py`, `handed_over`, `attached_host`, `LinkTap`, `github_dir` (§5); five §6 rows; seven `(should)` rows for Intellex findings 1-3, 5 and 12-15; the `intellex.*` coverage row (§7). |
| 2026-09-29 | `bfe55e5` | NC-WP6's first bench run (`20260929-025701`): §5's probe_peer debug-flag note and s43's undo rule; §6's `#L09` fps, TEST_ACTION-ACK lag and relayed-reply rows; the relay-window row's line numbers. Probe lines are now redacted in session.log (D57). |
| 2026-09-29 | `ede3aca` | NaviCore's device transports and Maestro (`NAVICORE.md` NC-WP7): `s44_navicore_devices.py` (23 `ncdev` tests, five in `hil/servos.py`); opt-in `navicore_aux_tx` (§2); §5's device observation windows; five §6 behaviour rows, six `(should)` rows (D-NC23, D-NC56 to D-NC60) and two doc-disagreement rows; a §7 `ncdev.*` row. `navicore.mae_cli_local` now needs a value (D-NC15). |
| 2026-09-29 | _(pending)_ | §6: resetting the SBUS controller can block its port write for tens of seconds, so the signal-loss test polls from before the pulse (full run `20260928-220200`). |
| 2026-09-28 | `4170a24` | NaviCore on the mesh (`NAVICORE.md` NC-WP6): `s43_navicore_mesh.py` (35 `ncmesh` tests, six `(should)`), opt-in `navicore_nvs` (§2); §5's mesh bullet and the `deaf` wording; §6's relay-window, deaf-WCB, CRC-ACK, remote-CLI, WDP PEER and `?WHOAMI` rows; six `(should)` rows (D-NC16, D-NC18, D-NC26, D-NC27, D-NC46, D-NC47); a §7 `ncmesh.*` row. |
| 2026-09-28 | _(pending)_ | §5: a NaviCore `#L12` poke's sender consumes its reply (`_eat_poke`), and a full-rate SBUS gate re-reads a stalled fps window (`sbus_full_rate`): both from the first bench pass of the NaviCore engine, boot and OTA suites (`20260928-204259`, `-212848`, `-215752`). |
| 2026-09-28 | `75d5e8d` | NaviCore boot and OTA (`NAVICORE.md` NC-WP2, NC-WP9, NC-WP10): `s46_navicore_boot.py`, `s47_navicore_ota.py`; opt-ins `navicore_ota_erase`, `navicore_ota_full`, `navicore_ota_relay_full`, `navicore_esptool`, `navicore_identity` (§2); `ncflash.BENCH_IMAGE` and helpers, the restart rules (§5); three `(should)` rows (§6); two coverage rows (§7). |
| 2026-09-28 | `eea29ca` | NaviCore's RC engine (`NAVICORE.md` NC-WP4, NC-WP5; `863462d`, `5b1afc6`): `s41_navicore_engine.py` (12 `ncengine` tests) and `s42_navicore_sbus_engine.py` (21 `sbus` tests), 23 of them in `hil/servos.py`; §5's engine observation windows; §6's SBUS-stall, trigger-queue and pinned-behaviour rows, three `(should)` rows (D-NC20, D-NC44, D-NC45) and a doc-disagreement row; a §7 `ncengine.*` row. |
| 2026-09-28 | `f84bacc` | WCB wave 3 group 2 (WCB-WP14, WP18, WP28, WP38, WP45, WP46, WP56; `69d03cc`): 21 tests, a new opt-in `etm_seq_wrap`; §1's one exception to the WDP-off rule; §5's ring holds 16 seqs, not 8. |
| 2026-09-28 | `fa0fa0f` | `hil/ncflash.py` `build(source=...)` and `build --source`: a NaviCore git worktree (INF9's `hil-week` branch) is compiled from a staged copy named NaviCore, since arduino-cli refuses a sketch folder not named after its main `.ino`; the checks and `BUILD.json` read the source. `tree_state` records the branch and `tree_line` names one other than main or master. `selftest.py` `ncflash_build_source`. |
| 2026-09-28 | `6823607` | WCB wave 3 group 1 (WCB-WP16, WP17, WP36, WP37, WP47; `33e1ddc`): identity (sender-id/MAC binding, learned-peer FORGET and fingerprint), `?WIFI` setter validation with no opt-in, boot timing and the announce/advert burst, the advert stagger, settings across a reboot, and the static CLAUDE.md rule-12 check in `selftest.py`. |
| 2026-09-28 | `553236c` | The NaviCore config tool's Export/Import, two-tab and live-panel specs (`NAVICORE.md` NC-WP3): `FakeSerial.events` names the page; two `(should)` rows in §6. |
| 2026-09-28 | `264583e` | The NaviCore config tool's Firmware-tab and clip specs (`NAVICORE.md` NC-WP3): §8 names the emulator's OTA and clip parts (`lib/navicore/ota.js`, `clips.js`) and the Firmware tab's mocks (`firmware.js`, the esptool-js and CryptoJS stand-ins); five `(should)` rows in §6. |
| 2026-09-28 | `83684b3` | **The NaviCore config tool's rig and no-board specs (`NAVICORE.md` INF7, NC-WP3).** New §8 subsection. `serve.js` maps `/NaviCore/` to the sibling repo; the NaviCore specs use their own origin, 8779 (`playwright.config.js` starts a second server). New `lib/navicore/` (the fake `navigator.serial`, the emulator and its model of `rc_config.h`, the bridge pipe, fixtures and page helpers), `unit/navicore/` (`nctool.static`, `nctool.unit`, the rig's self-test), `specs/navicore/`, `fixtures/navicore/config.bench.json` (bench-shaped, placeholders only) with `tools/make_nc_fixture.js` and `tools/scrub_nc_config.py`. `hil/bridge.py` gains `/serial/mark`, `/serial/read`, `/serial/write`, `/serial/signals` and `/sbus` for a piped device, outside the lock; `hil/wizard.py` gains `run_wizard_test(..., pipe=True)`, a NaviCore PING in `_reacquire`, and `run_unit_tests(files=...)`, which reports an all-skipped node run as SKIP and notes a failing `todo`. New suite `s49_navicore_tool.py`; eight `(should)` rows in §6. `selftest.py` gains `t_nctool_pipe_bridge` (75 cases). |
| 2026-09-28 | `0381b1f` | **NaviCore over the mesh (`NAVICORE.md` INF6) and its config surface (NC-WP1; `5a1957d`).** New `hil/ncmesh.py` (§5). `s40_navicore_config.py` grows to 35 `nccfg` tests, every write inside `nc_guard`: five `(should)` (§6), opt-ins `navicore_reboot` and `navicore_fault` (§2; the hook tests skip until INF9's hook build). `navicore.bench_health` (s02) fails when NaviCore sees no full-rate SBUS or its local Maestro does not answer; `navicore.serial_route_dbg` (s21) now skips when NaviCore routes a device to its own aux ports. `selftest.py` (79 cases) runs every `nccfg` test through the runner against `NaviModel`, a port of NaviCore's config handling. |
| 2026-09-28 | `ba433b7` | WCB-WP32, WP49, WP50, WP33, WP44 row 2, WP55 (s12, s13, s16, s17): 16 tests; `var.clear_all_name_collision` retired for `var.reserved_all_and_clear_all`; the `(should)` `input.usb_line_over_heap_dropped_whole` (tracker #101, fixed). |
| 2026-09-28 | `c496c46` | WCB-WP26, WP52, WP39 (s15, s24; `a3a00ec`, `3f6ff73`): 21 tests covering pinned-host routing, device port ownership (the `(should)` `devices.serial_mapped_port_refused`, tracker #99, fixed), the HCR shadow, FN 20/21, the poll scheduler and spellings, manual MP3/DFP routes, DFP ONERR and port move, the MP3 broadcast skip, PLAYFS; WLED one-hop cap, clear-all, first-host-wins, bare `;L`. |
| 2026-09-28 | `f3e1215` | 25 PWM, Maestro and Kyber tests from the coverage re-scan (WCB-WP15 rows 2-4, WP51, WP27, WP53; `e625889`), one `(should)` (tracker #98, fixed); §6 notes the two-slot get-query order and two PWM behaviours the tests pin. |
| 2026-09-28 | `0a9d8fc` | The WCB wave-1 tests (WCB-WP12, 13, 19, 25, 30, 31, 35, 42, 43, 48, 54, 57; 52 tests in s03, s05, s18, s31) are in the group rows, bench-verified on `6.2.1_280150RSEP2026` (`20260928-064402`, `-070402`). `wcb.pull_session_reaped` deafens W1 from a timer chain on W1's own clock, because a whole 15-frag pull reply lands inside one host read. |
| 2026-09-28 | `5c68781` | **NaviCore build, flash and recovery (`NAVICORE.md` INF4).** New `hil/ncflash.py`: `build` (NaviCore's working tree into `results/builds/navicore-<tag>`, the sketchbook libraries checked against their repos, `BUILD.json`), `check_image`, `flash` over `?OTALOCAL` with one chunk in flight and STATUS settling anything but a clean ACK, `recover` (PING, the USB-Serial/JTAG reset, then esptool only when allowed, writing app0 and otadata only), a `FLASHED.md` row per flash, and `python -m hil.ncflash`. §5 says how a test or a session uses it. `selftest.py` gains seven cases (74 in all) against synthetic images, a scripted arduino-cli and esptool, and a fake NaviCore that speaks `?OTALOCAL`. `build("inf4")` compiled NaviCore for real; nothing has been flashed with it yet. |
| 2026-09-28 | `7ca7e5a` | `checkpoint.redact_text` hashes the firmware's `ESP-NOW password updated to:` echo (both `?EPASS` setters print the new value), and a `Password:`/`Pass:` value runs to the end of its line, since a password may hold spaces. The WCB-WP12/25/42/43/57 tests (`wcb.pull_*`, `backup.one_line_restore`, `backup.chain_restore_funcchar`, `nvs.full_map_and_device_save`, `ident.epass_window_gates`, `nvs.erase_led_and_tails`) are in the group rows. |
| 2026-09-28 | `c8ebe30` | **`nc_guard`: NaviCore config snapshot and restore, and redaction (`NAVICORE.md` INF3).** New `hil/nc_guard.py`. A test that writes NaviCore's config runs inside `nc_guard` (§5), which restores by the snapshot plus explicit clears, and by RESET_DEFAULTS then the snapshot only when that fails, and proves the config byte-identical. It also restores the command library, `HIL*` clips, learned peers, the mode and the RAM toggles. The exact snapshot goes to `<run>/navicore_snapshot.json`, and a record with a redacted hash and the state into the checkpoint. The resume's step 7 checks NaviCore's config and restores it after a guarded test was cut off (§9). `Bench.log` hashes the credentials on NaviCore's and the SBUS controller's lines in both directions (`runner.REDACT_KINDS`). `redact_text` hashes any JSON `*password` field. New `checkpoint.redacted_diff`. New suite `s40_navicore_config.py` with `nccfg.guard_selftest`. `selftest.py` gains five cases against a fake NaviCore that merges SET_CONFIG as the firmware does. |
| 2026-09-28 | `a0098c2` | §6: the SBUS controller can hold a reply in its USB outbox until the host sends again; `SbusCtl` now pings every second while it waits (`sbus.to_navicore` failed on it in three full runs). `checkpoint.redact_text` also hashes the controller's `Pass:` boot line, which a failed test's last lines can quote. |
| 2026-09-28 | `975bcba` | **Review of `b57fe2a` (§8).** `wizard.push_fake_relay_chars` (new), edit-then-undo and add-then-remove cases in `wizard.push_fake_one_edit`, the character-order and `,`-board cases in `wizard.push_fake_delimiter`, and three unit tests in `unit/roundtrip.test.js` (the push-order planner, exhaustively against a model of the firmware's checks; a Maestro table in another key order). Each failed on `b57fe2a`. |
| 2026-09-27 | `cc2a8a9` | §9: a harness error pauses a run (`harness_error`); a suite edited mid-run caused one, because the runner found each test's wires by re-reading its file, and every test's source is now read at the start of a segment. |
| 2026-09-27 | `b8cf45c` | **NaviCore and SBUS drivers (`NAVICORE.md` INF1, INF2).** `hil/navicore.py` grows from 6 methods into the shared NaviCore driver, and the new `hil/sbus.py` holds `SbusCtl` and an SBUS frame codec; s21's NaviCore and controller helpers, s11's and `hil/resume.py`'s controller JSON, gui.py's controller ping and the GET_CONFIG reads in s08 and s22 now call them. A refactor: every suite sends the same lines with the same timeouts and assertions, `run.py --list` is byte-identical (518 tests) and every test's inferred wires and drives are unchanged. One deliberate difference: `SbusCtl.cfg()` hashes the controller's WiFi credentials in session.log, which s11 and s21 used to log in clear. `hil/serialdev.py` gains `send_paced` (512-byte writes 4 ms apart, as NaviCore's config tool) and `usb_jtag_reset`. The new methods (config writes, the recorder transfer, restarts) have not run on the bench. `selftest.py`: 12 new cases, 61 in all. |
| 2026-09-27 | `b57fe2a` | **Wizard defects W-1 to W-12 (`hil_plan/WCB.md` §3) fixed, with no-board tests (§8).** `unit/roundtrip.test.js` (10) and two W-7 tests in `unit/pull.test.js`; `specs/push_fake.spec.js` (9, `wizard.push_fake_*`: fake USB and relay connections driving the real `boardPull`/`boardGo`) and `wizard.remote_pull_fake_legacy_crc`. Each failed on the Wizard before the fix. They run in CI; registering them in `s30_wizard.py` is still to do. |
| 2026-09-27 | `a3a73ac` | **§10 Intellex tests.** `suites/s32_intellex.py`, `hil/intellex.py` (stage, a leashed host, Playwright and venv runners), `tests/intellex` (Playwright 1.63.0, headless); `hil/ws.py` keeps binary frames and sends an Origin header. 19 tests, none needing a board. |
| 2026-09-27 | `1f629a8` | Tracker #94 (F23): PWM output pulses are RMT-clocked. `pwm.passthrough_local` and `pwm.passthrough_mesh` check each step's held (last) pulse; the filter check allows a late pulse of the previous step (run 20260927-131152). A §6 row records the re-send trap. |
| 2026-09-25 | `b170342` | **Full run `20260925-092255` triaged (`HIL_TEST_AUDIT.md` §5).** `etm.reboot_defer_cap` passed its own check but its new cleanup failed: W2's `came ONLINE` glued onto the `;S0` sentinel. `WCB.run` now accepts a sentinel that starts its line, and the cleanup waits after its poll and never raises; a §6 row records the trap. `pwm.passthrough_local` found a firmware defect (audit F23). |
| 2026-09-25 | `b170342` | `navicore.wdp` failed in run `20260925-091104` because NaviCore had booted about 27 s before it and had learned no WCB over WDP yet. The test now polls with `?WDP,POLL` when a row is missing; a §6 row records the trap. |
| 2026-09-25 | `337f5df` | **No-servo full run `20260924-234056` triaged (`HIL_TEST_AUDIT.md` §5).** `etm.offline_detection_timing` allows one read of host timing error (floor 4.85 s) and checks that W2's edges alternate (tracker #80's fingerprint, now caught on a single hit); `backup_chain_lines` searches each section and `_factory_reply` re-reads a chain that fails its CRC; `?ETM,CHAR` waits for every bench WCB online and fails fast on an abort, and the reboot tests leave W1 seeing its peers. Three §6 rows record the traps. |
| 2026-09-24 | `337f5df` | **Runs with no moving servos.** New `hil/servos.py` lists the 27 tests that can move a real servo on this bench, by id with a reason each, from a read of every suite and NaviCore's config. `run.py --no-servos`, `bench.json` `no_servos` and the GUI's *No moving servos* box skip them with `moves a servo (--no-servos)`; the CLI flag lives in the run's checkpoint, so a resume keeps it. The trap it records: W1's `M1:W20` proxy sends every `;M1` move to NaviCore's dome Maestro, not only to the W1 S1 probe. Listed on doubt: `maestro.target9` and `kyber.target_id9_rejected` (`;M9` moves, W1-local by design), `maestro.clear_all_legacy_routing` (its `;M12` and `;M1,goHome` reach the dome only if `?MAESTRO,CLEAR,ALL` left the proxy, and the test does not stop when it did), and six SBUS tests that only move channels NaviCore re-emits on SBUS OUT. New selftest case `no_servos`. |
| 2026-09-24 | `337f5df` | **F20/F21 (tracker #92, #93).** §6: a deferred restart ignores a re-arm of the running RTERM session and `?STATS,RPT` and is capped at 20 s; `WCB.reboot` waits 25 s and notes a reached cap. New tests `map.nvs_keys_freed` (s13: `serial_map` entries in `?NVS` around a clear and a `CLEAR,ALL`) and, in s99, `etm.reboot_rterm_rearm`, `etm.reboot_stats_rpt` and `etm.reboot_defer_cap` (one W1 reboot each, nothing moves). |
| 2026-09-24 | `337f5df` | **Full run `20260924-190733` triaged (`HIL_TEST_AUDIT.md` §5).** Test fixes: `input.soft_rx_baud_sweep` takes 57600's ~1% loss (floor 18/20); the WDP-DA forget helpers wait out the save they schedule; `wdp.poll_refreshes_age` polls again once; `pwm.wdp_selfheal_missed_clear` takes its marks before W2 can hear; `WCB.reboot` waits 20 s. Three §6 rows record the traps behind them. |
| 2026-09-24 | `337f5df` | **Config pulls in parts (F13, [MGMT_RELAY.md](MGMT_RELAY.md)).** `hil.wcb` pulls with `?MGMT,PULL,<n>,P` and collects one line or K parts (`Pull`, `PullCollector`), raising `PullRefused` with a screened code on `[MGMT:CFGERR`; `read_config` retries NOMEM, CHANGED, NOPARTS and an empty reply. New tests `wcb.pull_plain_over_limit`, `wcb.pull_parts_many`, `wcb.pull_error_oom_legacy`, `wcb.pull_error_oom_parts`, `wcb.pull_nonblocking`, `navicore.mgmt_pull`, `navicore.pull_over_limit`, `wizard.remote_pull`, `wizard.remote_pull_parts` and the ten no-board `wizard.remote_pull_fake_*`; `wcb.pull_size_limit` reworked. 495 tests. Bench: 27/27 (run `20260924-190431`). |
| 2026-09-24 | `337f5df` | **`?backup` and the config pull at size (`HIL_TEST_AUDIT.md` F12, tracker #90).** New `wcb.backup_large_config` (both chains of a config near 6 KB whole and CRC-valid) and `wcb.pull_size_limit` (a pull just under 2912 characters arrives whole; one over it brings nothing, and the target says why); `seq.nvs_full_consistency` now requires whole chains. Verified on the bench (20260924-133332); 476 tests. |
| 2026-09-24 | `337f5df` | **The audit's F5-F10, and NVS use recorded per run.** Every run of five or more tests reads `?NVS` (new firmware command) on each USB-attached WCB at its start and end: session.log, a line per board in report.md, and `results/nvs_history.csv` (`hil/nvs.py`). New tests `misc.track_removed` (replaces `misc.track_command` and `misc.track_legacy`), `wcb.nvs_report`, and `seq.nvs_full_consistency` (opt-in `nvs_fill`); `serial.timer_cap_negative`, `persist.baud_label_validation` and the erase tests follow the firmware. Verified on the bench (20260924-120036, 20260924-120958); 474 tests. |
| 2026-09-24 | `337f5df` | **Full run `20260924-092602` with servos: 459 pass, 6 fail, 8 skip, all triaged (`HIL_TEST_AUDIT.md` §5).** Three failures were a full NVS on W1, gone once the erase test wiped it (F10; F9 is the ghost empty sequence it left). Two were test bugs, now fixed: `sbus.signal_loss_controller_reset` checks the longest frozen-counter window instead of fps = 0 and a poll after recovery, and reads the post-reset boot log twice; `wifi.pc_joins_ap_ws` names its adapter (netsh refuses an unnamed connect on a two-adapter PC), prefers the one without the internet, and uses a temporary `HIL-<ssid>` profile (new bench.json key `wifi_test_interface`). One announce was lost on a soft port. |
| 2026-09-24 | `337f5df` | **`s31_password_erase.py`: the mesh password and the NVS erase.** `backup.replay_idempotent` replays W1's chain a line at a time and proves nothing changes (bench `20260924-075445`); the erase tests restore W1 that way. Attended opt-ins, written and not yet run: `mesh_password` (`ident.epass_live`) and `nvs_erase` (`nvs.erase_defaults_restore`, `nvs.wcb_erase_alias`, which also check the factory defaults). Credentials are read from the board's chain at run time and only handed back to it. The erase keeps WiFi and the saved serial devices (`HIL_TEST_AUDIT.md` F8). 467 tests. |
| 2026-09-24 | `337f5df` | **`s29_leftovers.py`: what a fresh coverage scan still found (`HIL_TEST_AUDIT.md` WP10).** `?TRACK`, the legacy `?MAESTROM…`, `?M3xx` / short `?M2` `?M3`, `?ETMCHAR` and `?HELP`; the `;T` cap and a negative delay; a `;T` chain over `?MGMT,FRAG`; `?BAUD` / `?LABEL` validation; the `?MGMT` / `?RTERM` error replies; and the boot banner on W1 and W2 checked line by line against each board's config chain. 12 tests, all passing on their first bench run (`20260924-074119`, `20260924-074259`). The §7 gaps list loses its timer, validation and error-reply bullets; factory defaults remain, behind the manual erase. 463 tests. |
| 2026-09-24 | `337f5df` | **WiFi modes (`s28_wifi.py`, `hil/ws.py`), and the audit's F1-F4 fixes.** Opt-in `wifi_modes`: `wifi.off_and_back` and `wifi.join_w2_ap` (W1 joins W2's access point, so no PC adapter is needed; `20260924-024317` pass, `wifi.off_and_back` bench 20260924-024515); opt-in `wifi_pc`, attended: `wifi.pc_joins_ap_ws` drives the WebSocket endpoint through a minimal RFC 6455 client in `hil/ws.py` (self-tested on a local socket; not yet run on the AP, since this PC's internet is its WiFi adapter). Firmware (tracker #81-#83, compiled, not flashed by the session): the HCR/MP3/WLED soft-serial guards now allow up to 57600 like `?BAUD`, the help's `?PMSx` spelling, and `?HW` updating the running version; `hcr.config_rejects`, `mp3.config_rejects`, `wled.config_rejects`, `ident.hw_setter` follow, and `devices.soft_ports_fast_baud` / `wled.soft_port_57600` prove the new rates on the wire once the image is flashed. The Wizard's `autoComputeKyberTargets` keeps a remembered target unless its board reported a live list (F4). 451 tests; `selftest.py` 41/41 with the WebSocket client check. |
| 2026-09-24 | `337f5df` | **Full-coverage suites (`HIL_TEST_AUDIT.md` §7).** `s24_wled_config.py` (`?WLED` configuration), `s25_mesh_side_effects.py` (the WDP PWM self-heal after a missed clear, with W2 made deaf for it; the RC telemetry relay window), `s26_boot_restore.py` (each subsystem's NVS restore at boot), `s27_identity_destructive.py` (`?WCB`, `?WCBCH`, `?MAC,2`, `?HW`, `?RESET_BROADCAST`, `?WDP,CLEAR`, `?POS`/`?PCLEAR`, `?KYBER_CLEAR`/`_REMOTE`, every one restored on W1's own console); `tests/wizard/unit/devices.test.js` and `wizard.kyber_auto_targets`. All bench-verified without moving a servo (runs `20260924-003531` to `20260924-011003`). Two bench facts the tests now encode: one WiFi channel away is not off-mesh at bench range (W2 on channel 1 decoded W1 on channel 2), and a send to a board on another channel fails at the MAC layer with no ETM retry. 446 tests; the §7 table and gaps updated. |
| 2026-09-23 | `337f5df` | **Audit fixes (`docs/HIL_TEST_AUDIT.md` A1-A23; F1 open).** `config_guard` puts back leaked `AUTO_RESTORE` tokens before it reports (§1), and 27 tests restore in `finally`; the probe floor is v5 with v6 refused (§3); `input.soft_rx_baud_sweep` asserts 20/20 through 57600; positive controls in the silence tests, the real servo put back where it rested, tighter windows and honest titles. New tests: `persist.legacy_label_baud`, `pwm.legacy_forms`, `chars.legacy_debug_toggles`, `misc.track_legacy`, `wifi.status`, plus the HCR word and stop-style forms, `?SMCLEAR`, `?ETMOFF` and `?KYBER_LOCAL,S6` inside existing tests (§7). |
| 2026-09-23 | `ef4381d` | **WDP-DA tests follow the firmware's persistent, mesh-wide device records** (`WDP_DESIGN.md`, 2026-09-23). Records are kept until forgotten, so every `input.wdpda_*` test and `wdp.da_announce_propagates` forgets what it creates in a `finally` and first forgets `HIL`-named leftovers (`_da_scrub`); the 90 s waits that let an earlier test's record expire are gone. The expiry tests now assert "goes quiet, is kept, comes back" (`input.wdpda_ttl` also checks a device heard once is dropped at ~90 s), `input.wdpda_port_full` asserts refusal instead of eviction, and `input.wdpda_propagation` checks W1's `[WDPDA:N=2,…]` record and `?WDP,2` detail, then that a forget on W2 clears them (~12 s instead of ~95 s). New: `input.wdpda_forget`, `input.wdpda_persists_reboot` (two W1 reboots; it ends with a `?WDP,POLL`, because a rebooted board relearns each neighbor only from its next advert, up to 60 s later, and `wdp.controller_auto_enable` skipped on a W1 that hadn't heard NaviCore again yet). `input.wdpda_rejects` adds a line with text after the `}`, one cut short and two run together. `wdp.list_detail_errors` expects the usage text's new `DA,FORGET` / `DA,CLEAR`. |
| 2026-09-23 | `54f1f99` | **Full run `20260923-192959`: 415 pass, 0 fail, 5 skip in 1:59:25** on wcb_probe 7 and the images pushed to WIFI (the three WDP-DA label checks, which need an unlabelled W1 port, and the `ota_wrong_chip` / `w1s4_soak` opt-ins left off). The first full run on the pause/resume harness (`20260923-154611`, paused and resumed twice from the GUI) found four failures, all fixed: tracker #80 (ETM presence race), the wcb_probe 6 crash loop and the harness not noticing a probe restart (#78), and `peers.controller_off_on` requiring a line the firmware prints only conditionally. |
| 2026-09-23 | `078a907` | **Full run `20260923-154611`'s four failures fixed (compiled, not flashed).** wcb_probe 7 (§3): v6 boot-looped on interrupt-WDT panics when a CPU-only reset left a soft-RX level arm with no handler and the wired WCB rebooted; v7 disarms the header pins before the GPIO ISR service, unbinds before `MESH LEAVE`, and disarms a released pin. The WCB's `setup()` gets the same boot-time clear (§6 constraints row, tracker #78). The harness re-binds a link whose probe restarted since it was bound, fails a read across the restart with the reason, and fails the test in which a probe restarts unplanned (`probe<n> panicked at t=...`; §3, §6 behaviour row); A dropped and reopened probe port counts as a restart; a restart forgets only the bindings older than it (with a best-effort `UNBIND`), and the end-of-test incident `RESET`s the probe, since wiping the host side alone left the probe holding headers and the next bind hit `ERR pin in use`. `selftest.py` gains four checks with a fake probe that enforces pin ownership (39). New §6 constraints row for the `boardTable` presence lock (tracker #80, `etm.offline_detection_timing`). `peers.controller_off_on` runs with ETM debug on and accepts a missing `registered (live)` only when an ACK to WCB20 inside the OFF window explains it (§6 behaviour row). |
| 2026-09-23 | `078a907` | **Opt-in checkboxes and expected durations.** The opt-in keys are registered in `hil/optin.py` (title, what, default reason, per-test estimate; `w1s4_soak` follows `soak_minutes`). The 28 gated tests declare `@test(opt_in=...)` instead of checking in their bodies. The runner skips them before they start, after the missing-wire check, with the same message as before, so reports do not change. `selftest.py` pins every gated id and message. An unknown key fails at import. `hil/durations.py` takes the median of each test's last five real results from `checkpoint.json`, or from `report.md` for older runs, cached in `results/durations.json`. The GUI's Tests tab gains an *Expected* column (live `elapsed / expected` on the running test), area sums, estimates on *Run everything* / *Run selected*, the time left with a finish clock on a line of its own under the top timer (appended to the timer, it was clipped at the default width), and an *Opt-in tests* panel of checkboxes that write `bench.json` `opt_in` (§2, §4, §5). The panel starts folded: open, it left the table about six rows at 1320x860 and pushed the detail pane off the tab. A tick re-reads `bench.json` before writing, so a hand edit is not overwritten by the GUI's stale copy, and is refused while another process's run holds its `run.lock`. The time left counts only the running test once *Pause* or *Stop* is pressed, and the panel header's added time counts 0 for a missing-wire test, as *Run everything* does. A `checkpoint.json` of the wrong shape (a result row with no `id`) falls back to its `report.md`, and one unreadable run folder no longer empties the whole history. `run.py --list` shows each test's expected time and opt-in tag, and reads the cache without writing it. `selftest.py` (35 checks) covers the aggregation, the gate and `--list`; the GUI was checked on a hidden Tk root against a temp bench and results folder, not yet on screen. |
| 2026-09-23 | `078a907` | **Tracker #78 confirmed and fixed (firmware compiled, not flashed).** `softrx.erratum_pairs` on W1 (`20260923-113109`, edge-triggered image) lost 227 of 10125 two- and three-port lines at -1.5 to -3 µs skew, and every single-port line arrived exact: ESP32 erratum GPIO-3.14. The WCB now vendors EspSoftwareSerial 8.1.0 (`Code/WCB/src/EspSoftwareSerial`). On the classic ESP32 it receives on level interrupts flipped after every edge, and the PWM input ISRs work the same way; the S3 is unchanged. New test `softrx.level_irq_stuck_line`: W1's S4 RX held low for 2 s must not storm or reboot W1, and S4 must read text afterwards. `input.softserial_tx_rmt` also asserts each soft port's `[SOFTSERIAL] S<n> RX:` mode against `?HW`: level-triggered on 1/21/23/24, edge-triggered on 31/32. §6: `softrx.erratum_pairs` joins the `(should)` table, and a new constraints row covers level-triggered RX. §7 `softrx.*` updated. Both `softrx.*` tests skip only on a config that takes a port off the text path (the reasons `processIncomingSerial` skips a port); a clean port that cannot read one line on its own is a FAIL, since that is the fix broken. A stuck-line storm that resets W1 waits for the boot before restoring config, and keeps the storm message in front of any restore error. The probe keeps the stock library. |
| 2026-09-23 | `078a907` | **Tracker #78 verified on W1, and wcb_probe 6.** On the level-triggered image `softrx.erratum_pairs` lost 0 of 10125 multi-port lines (227 on the edge-triggered one), `softrx.level_irq_stuck_line` passed, and 139 soft-port, map, device and PWM tests passed (`20260923-144022`; started from `run.py`, paused with a PAUSE file after 42 tests and resumed in the GUI). `soak.w1s4_wire` ran 20 minutes clean (`20260923-113109`). `probe.soft_concurrent_rx` then failed on the probe's own edge-triggered soft channels (3 of 300 lines on each), so wcb_probe 6 bundles the WCB's patched EspSoftwareSerial (§3) and the test requires both of its arms exact on v6; on an older probe it skips the two-soft arm. `selftest.py` checks the two library copies are identical. |
| 2026-09-23 | `078a907` | wcb_probe 5 (§3): a channel's TX line stays high while it is unbound and re-bound. Every re-bind (any baud change) used to pull the WCB's RX low, W1 read a NUL at the front of its next line, and its parser broadcast that line as an empty command, which failed `kyber.local_port_move_releases_old` step (e). The WCB line reader now drops NUL bytes too (tracker #79), covered by the new `(should)` test `input.nul_ignored` (§6). Targeted run `20260923-095057` for #47 and #73 D4-D9: 139/140, the one failure being this; `input.*` 50/50 after the fix (`20260923-111854`). |
| 2026-09-23 | `078a907` | **Pause and resume (§9). `tests/hil/selftest.py` passes (31 checks, no hardware). On the bench from `run.py` (2026-09-23 12:08-12:14): a run never paused ends exactly as before (`20260923-120813`); a PAUSE file pauses at the next boundary with every COM port free, and `--resume` continues the same report as a second segment (`20260923-120834`); a run killed inside `input.bcast_in_block` is listed as interrupted, and its resume reboots the WCBs, refuses the config that test left (`?BCAST,IN,S3,OFF`) until it is restored, then re-runs that test first (`20260923-120933`). That bench run also found `identify_port`'s HELLO failing on every probe: the `?VERSION` it sends at 115200 reached the probe as garbage that prefixed HELLO, so a bare line end now goes first (the same fault was in the GUI's Find devices). The GUI's buttons and moving to other USB ports are not yet run on hardware.** Every run gets `checkpoint.json` and `run.lock`, and `report.md` is rebuilt from the checkpoint after every test. A single-segment run that finished keeps the old layout byte for byte (selftest compares it against the previous `write_report`). Runs pause from the GUI (*Pause after this test*, or *Yes* on close), with Ctrl+C in `run.py`, or with a `PAUSE` file. They resume from the Tests-tab banner, `gui.py --resume` or `run.py --resume`, after resume checks that pin every device by the USB serial recorded at the start and never adopt a different board unasked. A host outage no longer stops the run. The test is checkpointed as not run, and after 120 s of awake time for the ports it is re-run behind automatic checks. Otherwise the run pauses. A pre-test gate keeps a test from starting on dead ports or after a sleep between tests. The checkpoint and report store `?EPASS`, `?WIFI,AP|JOIN` and `Password:` values only as hashes, in `config_ref` and in every free-text field. `identify_port` moved from `gui.py` to `hil/identify.py`; it reads `?config` to its own lines instead of through `WCB.run()`, whose `;S0` sentinel a changed command character broadcasts, and a WCB whose board number does not print answers "a WCB, board number unknown" instead of "could not open". An adversarial review of the change was applied before any hardware run: loading prefers a newer `checkpoint.json.tmp`; a run whose tests all have results resumes to done without the checks; a changed LFI or command character blocks the resume at once, found with `WCB_WEBTOOL_CONFIG_PULL`; a silent NaviCore blocks on NaviCore; a wire that passes its resume check is marked verified; a cut-off `sbus.*` test's held inputs are released; Ctrl+C during a Wizard test or during the resume checks behaves as documented; `RunControl` takes an RLock (a Ctrl+C inside it deadlocked); a PAUSE file counts whatever its date; `run.py`'s questions treat stdin on NUL as no terminal; the GUI shows stopped runs' picker, records its selection, and a cancelled close-pause no longer closes the window later. A second review of those fixes was applied too: a delimiter left by a cut-off `chars.delim_*` test blocks the resume with `?D^`; `taskkill` runs in its own process group and is waited out, because a Ctrl+C mashed during an abort ended it and left node and Chrome holding the COM port; the GUI's close-now ends a Wizard test in flight; the probe's `PASS=` is redacted; a run whose only unfinished tests were renamed lists them as not run; and the start-of-run firmware record closes the ports it opened, so a run never holds NaviCore or a probe it does not use. `bench.json` and `links.json` are written atomically, a corrupt `links.json` is set aside, and the Wizard tests' Playwright runs in its own process group. The pre-test gate, not a change to `host_usb_loss()`'s window, catches ports lost between tests: the in-test classification is unchanged. |
| 2026-09-23 | `078a907` | **Measurement for tracker #78, not yet run.** New suite `s23_softrx_measure.py` with two opt-in tests. `softrx.erratum_pairs` (`softrx_erratum`) asks whether W1 loses soft-port input to ESP32 erratum GPIO-3.14: 4500 two- and three-port rounds into W1 S3-S5, each followed by single-port lines on the same ports as the control, at a seeded skew (60 % within +-15 us, 40 % within half a bit, ~5 % zero). W1 only prints "Ignored chain command" for them, with `?BCAST,IN` off on S3-S5. It needs the new probe verb `TXSKEW` (wcb_probe 4, §3): the hardware channels cannot set the skew, because each UART starts a frame on its own baud tick. `soak.w1s4_wire` (`w1s4_soak`, `soak_minutes`) repeats the load of the W1S4 episode (run `20260923-012123`) with W1S4 alternating between a probe hardware and soft receiver, and classifies a recurrence as W1's content, W1's S4 waveform or lead, sub-bit glitches, or S2 coupling. Both write a CSV into the run folder. §2 `opt_in` and `soak_minutes`, §3 and §7 updated. Probe 1 must be flashed with wcb_probe 4 before `softrx.erratum_pairs` can run. |
| 2026-09-23 | `078a907` | **Wizard parser tests and CI (§8).** `tests/wizard/unit/parser.test.js` runs `Wizard/parser.js` under `node --test` against a real scrubbed `?backup` fixture: full-push round trip, an unchanged board emits nothing, an edited one emits only its own commands, `CLEAR` forms, port claims, and a system-file round trip. Eight tests, no browser, ~0.3 s; `npm run unit`, or `wizard.parser` in the harness. New `.github/workflows/wizard-tests.yml` runs them plus `wizard.smoke` on `Wizard/**` and `tests/wizard/**` pushes only, never touching `build.yml`. The test server is now `serve.js` (Node) rather than `python -m http.server`, so CI needs no Python. Recorded while writing them: remote Maestro proxies parse into `kyber.targets` and remote WLEDs into `wledRemotes` (display-only, never pushed) — a full push emits neither unless the caller supplies a whole-system `maestroTable`. |
| 2026-09-23 | `078a907` | Two new `(should)` tests for tracker #47 in s17: `seq.cycle_guard_reuse` (13 HIL keys on W1 S1, all `,L`: eight distinct leaves then a repeat under one trigger, reuse across a `;t`, one leaf at depth 2 and 1, and a `;t`-paced self-recall that must be the only refusal; on the current images it fails with a depth refusal for HILL8 and cycle refusals for HILL1-3) and `seq.reuse_queue_reserve` (12 back-to-back calls of a 20-command sub-sequence: every call either expands whole or prints one "Command queue nearly full" line, and no "Command queue is full" line appears). `seq.cycle_guard` is unchanged; `seq.cycle_guard_case`'s docstring now names the exact-bytes compare. Not yet run on hardware. |
| 2026-09-23 | `078a907` | Two new `(should)` tests for tracker #73 D4: `kyber.local_free_port_takes_pwm` (Maestro 1 cleared off S1, WDP off, Kyber on S1: `?MAP,PWM,OUT,S1` and a WLED on S1 are refused, a WLED and then a PWM output on S2 are accepted, `?KYBER,LOCAL,S2` is refused onto the PWM port, and after a Kyber_Local boot S2 keeps its output, idles LOW with its UART skipped, and `;P2` gives one pulse; the S1 refusals match by prefix because their wording differs between images, so the current images fail first at the WLED S2 arm) and `wizard.kyber_local_frees_other_port` (no board: the claims a local Kyber and Maestro REMOTE leave, and a REMOTE -> local push releasing S1 before an HCR there). Not yet run. |
| 2026-09-23 | `078a907` | Two new `(should)` tests for tracker #73 D5/D9: `kyber.local_port_move_releases_old` (Maestro 1 cleared off S1, WDP off: a move S2 -> S1 releases S2, the bare `?KYBER,LOCAL` keeps S1, then after a Kyber_Local boot a live move back releases S1 and the task follows with no reboot, and `?MAESTRO,REMOTE` releases S2 so a `;S0` line typed there prints once and nothing reaches the mesh; it stops at the first missing release message, so the current images fail at step b before any injection) and `wizard.kyber_release_order` (no board: the push generator releases before BAUD/BCAST and re-sends the released port's rows). `kyber.local_rejected_port_keeps_forwarding` compares only the `?KYBER,LOCAL` line, since `?backup` now also emits an early `?KYBER,CLEAR`. `openWizard()` disables Chrome's HTTP cache (§8): the persistent profile had served a stale `parser.js`. `wizard.kyber_release_order` and `wizard.smoke` pass standalone; the kyber test has not run on hardware. |
| 2026-09-23 | `078a907` | Two new `(should)` tests for tracker #73: `kyber.local_s1_legacy_fallback_skips_kyber_port` (Kyber on S1, no own-id slot: the legacy S1 fallbacks send nothing into the Kyber port, `;M0` still broadcasts, and the `?config` row of the Kyber port says "(Kyber)" with W1's S1/S2 labels cleared for one read and replayed) and `kyber.local_clear_maestro_drops_target` (a local Maestro clear drops its Kyber target, the freed port follows its `?BCAST` flags, `;M9` still writes a free S1, and a re-add restores the target). `kyber.local_mode_s2` now expects "Kyber port: Serial2 (115200 baud)" in place of "Initialized Serial1 & Serial2". Not yet run on hardware. |
| 2026-09-23 | `078a907` | `probe.soft_concurrent_rx` rewritten. It has a control arm: W1S2 on a probe hardware channel, so only one probe pin takes edge interrupts, and 1000 lines must be exact. It has a rated two-soft arm: ESP32 erratum GPIO-3.14 can drop an edge when two soft channels receive at once, so up to 2 % of lines may be lost, but a line lost on both wires, or a mangled line that isn't a near-miss of a sent one, fails. The channel-letter swap is gone, because this loss follows the lagging wire, not the letter. Its first run (`20260923-012123`) caught an intermittent ~3.5 % corruption on W1S4 even in the control arm; minutes later it passed clean (`20260923-012735`). Tracker #78, including a bench check. |
| 2026-09-23 | `078a907` | Relay reads retry like a real requester. The SEQ and SEQVAL replies are unacknowledged broadcasts sent once, so s17's `_relay` helper and `inv.mgmt_seq_w2_relay` try up to 3 times, spaced past the 1.5 s duplicate filter (tracker #77; `inv.mgmt_seq_remote` lost one reply on the 23:58 image). Verification run `20260923-002254`: 18/18, including the NaviCore readback after `sbus.trim_exact`'s mode change (#76). |
| 2026-09-23 | `078a907` | **Full run `20260922-215719`: 401 pass, 2 fail, 4 skip in 1:39:11** (the skips are the three WDP-DA label checks, which need an unlabelled W1 port, and `ota_wrong_chip`, which is opt-in). Both failures were NaviCore test bugs (tracker #76; §6 has the behaviour): `navicore.maestro_mesh_settarget_readback` drove a knob-owned channel whose auto-release an earlier run's SET_MODE had armed, and `navicore.mesh_stats_counts` caught NaviCore's 30 s stats report in its window. `kyber.remote_roundtrip` now retries on its best-effort channel (#74). |
| 2026-09-22 | `078a907` | **Rerun `20260922-193306` (69 tests): 64 pass, 4 fail.** `ota.relay_full_same_image_wcb2` failed because the laptop, on battery since 15:59, hibernated at critical battery at 19:58 (Windows Kernel-Power 524/42). Only W1's CH9102 port errored after the resume, so the all-ports rule missed it. The runner now detects sleep inside a test from awake time and warns when a run starts on battery (§1). `ota.relay_teardown_frame_err` confirmed its predicted firmware bug (tracker #70; its "Documentation that disagrees" row is gone). `inv.seqget_largest` lost a 7-chunk unacknowledged reply, and now retries like a real requester (#71). `kyber.local_port_s1_frees_s2` failed as expected until #14 was applied, which also removed the Kyber row from "Documentation that disagrees". |
| 2026-09-22 | `078a907` | **Triage of full run `20260922-151421`.** All 8 diagnoses were adversarially verified; fixes are tracker #63-#69, and #14, #28 and #58 were updated. Tests: `input.wdpda_rejects` counts only S5 lines (another port's normal expiry had failed it). The WDP-DA tests run basic, rejects, fields, usb, propagation, ttl, so none waits out another's 90 s record in a full run. `wdp.controller_auto_enable` re-polls up to 3 times, because one lost broadcast frame used to empty its window. `sbus.trim_exact` can finally run: a matrix trim under a mode where both its slots are unmapped. `inv.seqget_toobig` became `inv.seqget_largest`, since no bench board can store a value over 2,903 characters. `kyber.local_port_s3_frees_s2` became `kyber.local_port_s1_frees_s2`, since S3-S5 are refused. New `probe.soft_concurrent_rx`. The probes run wcb_probe 3 (GPIO ISR at level 3, #66). The three new WDP-DA tests (`input.wdpda_shared_port`, `_port_full`, `_shared_ttl`) came from another session's issue #19 work. §7 no longer claims most groups have not run. |
| 2026-09-22 | `078a907` | **First opt-in run (`20260922-185238`), and three fixes from it.** (1) The OTA tests looked for their image only in `Code/bin`, by the running version string. A local bench build keeps the committed string, so 15 of them skipped. `_image` now prefers the image the harness flashes (`results/builds/wcb-esp32-meshq`), and only then a CI release. (2) `ota.local_baud_rejected_rebegin_restores` confirmed its predicted bug: the firmware now restores 115200 after a rejected BEGIN (tracker #61, `WCB_OTA_TECHNICAL.md`), and that row left "Documentation that disagrees". (3) `sbus.signal_loss_controller_reset` never reset the controller: an RTS-only pulse does not reach a native-USB board on Windows (§2). It now re-writes DTR after each RTS change and checks the controller's boot record (tracker #62). |
| 2026-09-22 | `078a907` | **A host sleep no longer fails the rest of a run.** Full run `20260922-151421` lost 12 tests to the laptop sleeping 44 min in. Windows logged a power-source change at 15:59:03 and "entering sleep" at 15:59:09, and all seven bench ports failed in the same 12 ms. It resumed at 18:26, so one test "took" 8841 s. The runner now keeps Windows from idle-sleeping during a run and detects the all-ports-at-once loss. The test it hits becomes ERROR `NOT A RESULT`, and the run waits for the ports or stops. §1 describes it. |
| 2026-09-22 | `078a907` | **§6 rewritten to current state.** Almost every `(should)` row described a bug that is now fixed as if it were current. The table now lists what each test checks (its own title) and its fix-tracker item, and status lives in the tracker. The first full run's six bugs became the rules they left. The fixed `?ETM,CHAR` row is gone, and the CHKSM row's line numbers are updated. `hil/links.py` has a hand-off guard (tracker #60): a read across a hardware-channel hand-off now fails and names the probe, instead of returning another wire's bytes. That is what failed `navicore.plain_broadcast_both_ways`, where probe2 needed three of its two hardware channels. `input.wdpda_rejects`, `etm.chksm_mismatch_silent_loss` (and the other five `_require_w2_online` callers) and `kyber.wdp_auto_remote_revert` now wait for their precondition instead of skipping: the WDP-DA TTL, W2 back online, and NaviCore's advert after a `?WDP,POLL`. |
| 2026-09-22 | `078a907` | `hil/wcb.py` `WCB.run()` drops `{"sys":1,...}` lines: relayed RC telemetry reaches W1's USB at up to 5 Hz while a relay window is open and interleaved with command output (`?KYBER,LIST` once began with an `rc_ch` line). Tests that want those lines read `w.dev` directly, as the NaviCore suite already does. |
| 2026-09-22 | `078a907` | Wizard tests: **all four pass on the bench** (full run `20260922-095852`; `wizard.pull` 32.8 s including the one-time port authorization, and the pushed label was read back from `?backup` and restored). An unauthorized board now **skips** instead of failing when the port dialog is cancelled or unanswered, or when `WIZ_NO_AUTHORIZE=1` is set — that was how the 09:28 run reported a collision with a concurrent bench session. |
| 2026-09-22 | `078a907` | `gui.py`: dark by default (`--light` for the old look) - two palettes at the top of the file, status colours per palette, and the Windows title bar set dark through DWM, since Tk does not draw it. A double-click on a test row while a run is going is now refused instead of queueing a SECOND run and blanking those tests' results in the table. |
| 2026-09-22 | `078a907` | `hil/serialdev.py`: every write is bounded (`write_timeout`, set ONCE at open - assigning it on an open port makes pyserial re-run `SetCommState` under the reader's in-flight read, and doing that per send lost/delayed replies on the S3 native-USB ports), raising a test failure, and a port that vanishes is reopened by the reader thread, DTR/RTS still deasserted. Before, a native-USB board that stopped draining its endpoint blocked `write()`+`flush()` forever, freezing a GUI run for ten minutes while it held COM4, and a USB drop (SBUS controller, 2026-09-21) left the reader dead so every later test on that device errored. The Devices tab's port dropdowns now list USB ports only, are read-only and ignore the mouse wheel: a wheel scroll over one saved a new port, and `sbus` ended up on a Bluetooth SPP port (COM19), which blocks writes forever. |
| 2026-09-22 | `078a907` | **Wizard tests (§8).** Playwright drives the Wizard over real Web Serial as harness tests (`suites/s30_wizard.py`, `hil/wizard.py`, `hil/bridge.py`, `tests/wizard/`): the harness hands one board's COM port to Chrome, serves a localhost bridge to the probes and other boards, and takes the port back. One Chrome profile per bench device holds its Web Serial grant, authorized once by hand. `wizard.smoke` passes standalone and through `run.py`; `wizard.pull`, `wizard.push_label` and `wizard.terminal_wire` have not yet run on the bench. |
| 2026-09-21 | `078a907` | `gui.py --run <globs>` opens the GUI with those tests queued and starts them (same globs as `run.py`), so a run started from a script can be watched live. `run.py` forces UTF-8 stdout: redirected to a file on Windows it was cp1252, and the first `→` in a failure message killed the run. |
| 2026-09-15 | `eb82f29` | **Bench run of the fixes, and two more firmware bugs.** Final full run, every board on tonight's images: 285 pass / 62 fail / 51 skip in 51:22, from 249 / 96 / 53 in the morning. 53 of the 62 failures are `(should)` tests pinning known bugs. Of the nine others, four are §6 entries (`input.soft_rx_baud_sweep`, `map.baud_on_raw_mapped_port`, `input.softserial_core0_contention` at 9 of 10, and one rule-13 mis-frame in `input.bcast_fanout`), one is a test bug (`etm.offline_detection_timing`), one is NaviCore telemetry landing in a console the test reads (`maestro.list_and_legacy_spellings`), and three are NaviCore's USB console stalling for seconds at a time (`navicore.cli_codes`, `sbus.slider_exact`, `sbus.btn_double_triple_tap`) — a NaviCore-side issue, worth a ~1 Hz `Serial.flush()` tick in its `loop()`. New firmware findings, both fixed in the working tree: the ESP-NOW callback bit-banging S3-S5 (those chunks are now queued for `MeshSerialOutTask` on core 1 — `input.softserial_core0_contention` went from 0 of 10 blocks exact to 9-10 of 10, the rest being the documented residual), and JSON telemetry clobbering `lastReceivedViaESPNOW`, which cost locally-typed broadcasts their mesh copy at random. Harness fixes: a probe's channel table is cleared in place rather than popped (a bind in flight registered into an orphan, so two wires shared one channel and a test read another port's traffic — the cause of `hcr` and `bcast` wire failures that passed when run alone); `wdp.autojoin_off_ignores_probe` waits for id 15's 22.5 s advert instead of 17 s; `navicore.maestro_mesh_rejects` flushes NaviCore's console with `#L12` per case instead of sleeping on it. `input.soft_rx_baud_sweep` stays red: bit-banged RX under mesh load is at its limit, and the counts in its message are lines RECEIVED, not lost — worth rewording. |
| 2026-09-15 | `eb82f29` | **Fixes for the four new firmware bugs** (§6), not yet run on the bench: WCBClient 1.17.1 forgets a WCB's anti-replay window on its boot announce; `;S` no longer calls `flush()` (a live `?BAUD` on S1/S2 flushes instead); every sequence-key path refuses keys over 15 characters (`SEQ_KEY_MAX_LEN`); remote PWM configuration is gated on ESP-NOW having started rather than on 5 s of uptime, and says so when it cannot send. Review follow-ups: a boot announce clears a sender's duplicate window once per boot, in the WCB firmware and in WCB_Client, so a command retried between its three announces cannot run twice; erasing by an over-long key still drops it from the key list; `ONERR` callback keys are capped at 15 characters; S1 and S2 get a 1024-byte TX buffer before `begin()`, the second half of the S3 input loss (the first re-run after removing the flush still lost 3 of 20 lines at 38400). |
| 2026-09-15 | `eb82f29` | **First full run and its triage.** 398 tests in 50:48: 249 pass, 96 fail, 53 skip. Every failure was traced to its cause. 47 of the 53 `(should)` failures confirmed their predicted bug; the other six failed for harness or test reasons below. Harness bugs: `probe.mesh_leave` anchored `BOOT wcb_probe` with `^`, which never matches after the ROM banner, so all 21 probe-mesh tests failed on leaving (and a failed leave skipped the `?WDP,FORGET` cleanup, which skipped three more); a baud pinned by `map.raw_remote_115200_exact` outlived it and garbled every later injection into W1 S2 (8 failures); `send_chunked` sent 1500 bytes into the probe's 1024-byte `TX` buffer. Test bugs: a LOW-parked port misframes a whole burst (2 PWM tests), `pwm.passthrough_local` marked after the pulses, `pwm.clear_all_reaches_remote` cleared inside the 5 s send hold-off, `conflict.dfp_port_unguarded` missed that `?HCR,CLEAR` re-enables the poll, `etm.offline_detection_timing` matched NaviCore's WCB20 heartbeats as WCB2, and `chars.delim_comma_restore_path` sent lines back to back across a delimiter change. All fixed. New firmware bugs are listed in §6. |
| 2026-09-15 | `eb82f29` | **GUI: elapsed timer, and devices on ports.** An elapsed timer above the tabs shows the job's time, tests done of the total, and the current test's time; `report.md` records the run time. `bench.json` `port_devices` records real devices wired to WCB ports, set from the Wiring tab: such a port is never made to transmit without a `port_stimulus`, a probe wire on it is forced listen-only, auto-detect, *Verify* and `link.out` skip it, and a test that needs a probe there skips naming the device. Missing-wire skips are now written to `session.log`; they were shown only in the GUI, which hid why 46 tests skipped once W1 S5 went to NaviCore S1 and the W2 S1 tap was not found. A review then closed the gaps in that rule: a listen-only wire on such a port still counted as a wire, so its tests ran; `links.json` loaded an old two-way wire unchanged; tests that pick a port at run time, or drive one without a wire, bypassed the check; and auto-detect deleted a wire added there by hand. `links.get()` now hides the port, `load()` forces the tap, `usable()` and `drives` cover the rest, and auto-detect keeps such wires. The Wiring tab's right pane scrolls (the device form was off-screen at Windows display scaling), and queuing a run no longer cancels a pending *Stop after this test*. Coverage audit added to §7. |
| 2026-09-15 | `eb82f29` | **Suites s12–s22** (349 tests): serial input, serial mapping, PWM, HCR/MP3/DFPlayer, variables and IF, sequences and the inventory relay, ETM/peers/WDP/command characters, the probe as a mesh client, OTA, NaviCore and the SBUS controller, Maestro routing and the Kyber bridge. Written from code-reviewed specs; not yet run on the bench. 60 `(should)` tests pin probable firmware bugs, and fifteen doc/code disagreements are listed (§6). `CLAUDE.md` rule 4's CHKSM command limit corrected from 188 to 187 (`ETM_MAX_CMD_WITH_CRC`, `WCB.ino:225`). **Discovery fix:** it swept only `mesh_only_wcbs`, which emptied when WCB2 got its own USB cable, so WCB2's wires were never found; it now sweeps every WCB and drives each on its own console. **Probe fix:** headers S1/S2 always bind a hardware channel, since EspSoftwareSerial rejects their pins. New helpers `Watch`, `Console`, `probe_in_mesh` / `mesh_params` and the link PWM/level methods; `opt_in` in `bench.json`. Probe mesh ids avoid W1's persisted learned peers (6 and 9, found in the session logs), and every command-sending test has its own id, because a WCB clears a client's duplicate ring only on a boot announce. `SerialDevice.set_baud` for the OTA baud bump; opt-in keys `ota_erase`, `ota_full`, `ota_full_wcb2`, `ota_wrong_chip`, `navicore_clip` and `sbus_reset`. `s22` rebuilds a Maestro slot table only under `?WDP,OFF` and reuses probe ids behind a duplicate-ring burn; the §1 Maestro rule was narrowed to match; new `bench.json` key `maestro_safe_sub`. **Existing tests fixed:** `maestro.fanout_remote` passed falsely — its pattern also matched NaviCore's "matches no local slot" line — and now requires NaviCore's exact dispatch line when it hosts device 1, skipping with a stale-proxy note when it does not; `sbus.to_navicore` now asserts the exact calibrated stick values instead of "moved by more than 500". |
| 2026-09-14 | `eb82f29` | **Probe v2, wire discovery and the GUI.** The probe became a runtime-configured instrument (five channels, formats, taps, reply rules, multi-header PWM, edge counting, pin levels, mesh mode) so tests never need new probe firmware. Wires are no longer hand-listed in `bench.json`: they are discovered by edge counting and saved to `results/links.json`, and tests skip — naming the wire — when theirs is missing. Added `gui.py` (devices, wiring plan and diagram, tests, log). The probe resets itself on first attach, because bindings survive between host runs and one leftover binding made `probe.scan` read `busy`. **Harness race fixed:** `WCB.run()` used `End of Version` as its end-of-output sentinel; after a reboot, `wait_boot`'s own trailing `End of Version` landed just after the next mark, so `?backup` parsed nothing and every later read was one command behind. `persist.label_reboot` and `seq.local` then skipped their restores and left a label and a stored sequence on WCB1 — caught by `config_guard`, cleaned up, and both WCBs re-verified identical to their pre-test config. The sentinel is now a unique `;S0` echo per call, and the guard retries a failed re-read and reports it with the original failure. Full run afterwards: 48 of 49 pass; the one failure is `etm.char_loaded`, the §6 firmware bug. Probe identification in *Find devices* sends a lone newline before `HELLO`, because the earlier 115200 `?VERSION` reaches a probe as newline-less garbage that would otherwise prefix it. More than one WCB can now have a USB cable (`wcb<N>`, added from the Devices tab), and the wiring suggestions follow the wires actually made. |
| 2026-09-14 | `eb82f29` | Initial harness, probe firmware and suites (50 tests), built against firmware `6.2.1_101034RSEP2026` on branch `WIFI`. Full bench run: 48 pass. `etm.char_loaded` fails on the firmware bug recorded in §6. Both §6 firmware findings came from this run: the 1500 ms pull dedup (two back-to-back pulls, the second silently unanswered) and the phase-3 broadcast suppression (the target logged none of the broadcasts). |
