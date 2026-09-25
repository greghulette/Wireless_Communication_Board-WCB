# WCB — Working Notes

The Wireless Communication Board: ESP32 / ESP32-S3 firmware that meshes droid boards over
ESP-NOW and bridges each one to its locally-attached serial devices (Maestro, MP3 Trigger,
WLED, HCR). Ships with the **Wizard**, a browser configuration tool, and the KiCad hardware.

**Engineering documentation is in [`docs/`](docs/). Read the page for the area you are
changing before editing.**

| Working on | Read |
|---|---|
| WDP — discovery, device announce, election | [docs/WDP_DESIGN.md](docs/WDP_DESIGN.md), [docs/WDP_DEVICE_ANNOUNCE.md](docs/WDP_DEVICE_ANNOUNCE.md) |
| Stored variables | [docs/VARIABLES_DESIGN.md](docs/VARIABLES_DESIGN.md) |
| Stored sequences — nested recall and the cycle guard, `?SEQ,NAMES`, `?MGMT,SEQ` | [docs/SEQUENCE_INVENTORY.md](docs/SEQUENCE_INVENTORY.md) |
| OTA | [docs/WCB_OTA_TECHNICAL.md](docs/WCB_OTA_TECHNICAL.md) |
| WiFi — `?WIFI`, hosting/joining an AP (branch `WIFI`) | [docs/WIFI_DESIGN.md](docs/WIFI_DESIGN.md) |
| WLED | [docs/WLED_INTEGRATION.md](docs/WLED_INTEGRATION.md) |
| Kyber passthrough | [docs/WCB_KYBER_PASSTHROUGH_PARAMS.md](docs/WCB_KYBER_PASSTHROUGH_PARAMS.md) |
| Remote config pull — `?MGMT,PULL[,P]`, parts, `[MGMT:CFGERR`, relay reassembly | [docs/MGMT_RELAY.md](docs/MGMT_RELAY.md) |
| Device command translation | it's in [`WcbCmd`](https://github.com/greghulette/WcbCmd), not here |
| Hardware-in-the-loop tests — `tests/hil`, the `wcb_probe` firmware | [docs/HIL_TESTING.md](docs/HIL_TESTING.md) |

---

## Rules that are easy to break

1. **`;`-command translation lives in `WcbCmd`, not in this firmware.** `WcbCmd` is compiled
   by this firmware *and* by NaviCore, deliberately, so the same command produces the same
   device bytes on both paths. Fixing a device byte sequence here instead of there splits
   them. Push `WcbCmd` first, then rebuild.
2. **Under ETM, a broadcast is acknowledged — per board, not once.** Ensured Transmission Mode
   ACKs *every unicast and broadcast* command and retries up to 3 times before marking it
   failed. One broadcast send fans out into one pending ACK **per known board**
   (`WCB.ino:1484` "send to broadcast but track per-board via ACKs"), so a single unreachable
   board leaves the broadcast pending until it times out or is removed
   (`WCB.ino:6497`, `:6521`). Sizing timeouts or reading "why is this slow" without knowing
   this leads you to the wrong place.
3. **But several broadcast paths deliberately bypass ETM and are never ACKed:** PWM
   passthrough and raw Kyber data (latency), boot announce (`WCB.ino:966` — sent redundantly
   *because* it is unacknowledged), RC JSON telemetry `rc_hb`/`rc_ch` (`WCB.ino:2170`), the
   `emit*` helpers which broadcast over WCBClient without ETM wrapping (`WCB.ino:3276`), and
   any explicit `sendESPNowMessage(0, …, false)` (`WCB.ino:1549`). "Broadcast is
   unacknowledged" and "every broadcast is acknowledged" are both true here, of different
   paths — check which one you're on before assuming delivery.
   **Loop prevention gates every broadcast on one global.** `sendESPNowMessage()` drops a
   broadcast outright when `lastReceivedViaESPNOW` is set (`WCB.ino:2722`), and a queued command
   carries the value it snapshotted at enqueue (`:2286`). Only a received *command* may set it:
   JSON telemetry is consumed without ever running, so both receive paths skip the flag for a `{`
   payload (`:4520`, `:4815`). Set it from a packet that never executes and a controller's 5 Hz
   `rc_ch` silences locally-typed broadcasts at random — local ports still print them.
   A broadcast the board **originates from `loop()`** (no command around it) reads whatever the
   last received command left, so it must clear the flag for its own send — `sendOwnBroadcast()`
   in `WCB.ino`. `?ETM,CHAR` phase 3 lost its whole broadcast third to this.
4. **ETM is all-or-nothing across the fleet.** Every board must have ETM set the same way or
   messages are **silently ignored** — same for `?ETM,CHKSM`, where a mismatch rejects all
   packets. **WDP requires ETM enabled**: it rides the 252-byte ETM struct and gate
   (`WCB_WDP.h:20`), so turning ETM off also kills discovery. ETM packets are 252 bytes vs 249
   normal; CHKSM adds 12 bytes and drops the max command to 187 chars (`ETM_MAX_CMD_WITH_CRC`,
   `WCB.ino:225`).
5. **The same Maestro ID can legitimately exist more than once in the mesh** — on one WCB
   (different serial ports) or across several WCBs. **Slot identity is
   `(maestroID, serialPort, remoteWCB)`, not the ID alone** (`WCB_Maestro.h`), so the same ID
   on a different port or a different board gets its *own* slot rather than replacing the
   existing one. Consequences:
   - **Lookups fan out — never `break` on the first match.** A command addressed to an ID
     walks every slot and fires all of them (`WCB_Maestro.cpp:108`, `:304`, `:414`). Code that
     stops at the first hit silently drops the duplicates.
   - **Dedup is by destination, not by ID** (`WCB_Maestro.cpp:100–107`): one write per physical
     port, one unicast per target WCB — because the same ID on the same board *is* the same
     device. Distinct ports and distinct boards each still fire.
   - **The id-0 broadcast path is deliberately exempt from that dedup**, since each write there
     carries a different device id and Maestros can be daisy-chained on one serial line.
   - **IDs 1–8 are devices; id 9 (all local) and id 0 (all Maestros) are reserved routing
     targets**, never stored as a slot. `MAX_MAESTROS_PER_WCB` is 9 — slot capacity, not an ID.
   - **Auto-add is per-host, not first-host-wins:** `maestroAutoAddRemote()` (remote proxies
     learned from WDP adverts) gives *each* advertising host its own `(id, host)` slot, and will
     add a remote proxy even when the same ID is hosted locally — so `;M<id>` fans out to the
     local Maestro *and* every remote host (multicast). Exact `(id, host)` duplicates are
     idempotent (baud-refresh only). **Caveats:** proxies are never auto-evicted — one to a host
     that goes offline or whose Maestro physically moves stays until cleared (`?MAESTRO,CLEAR` /
     clear-by-id); and the 9-slot cap is shared, so many duplicates can exhaust it (logged, then
     new adverts are skipped). WLED, by contrast, stays strictly one-slot-per-ID.
6. **CI clones `WcbCmd` from GitHub** — not `arduino-cli lib install` — so builds pick up the
   latest push with no Library Manager indexing lag. Nothing here pins a `WcbCmd` version.
7. **`HumanCyborgRelationsAPI` and `EspSoftwareSerial` are vendored, not installed.** WCB-patched
   copies sit at `Code/WCB/src/HumanCyborgRelationsAPI` (serial-only) and
   `Code/WCB/src/EspSoftwareSerial` (8.1.0, level-triggered RX on the classic ESP32, rule 13), and
   are pulled in by relative `#include` precisely so no differently-patched copy on a build machine
   can shadow them. Each has a README listing its patches. Don't "fix" this by adding either as a
   library dependency. CI installs neither, so a stray angle-bracket `SoftwareSerial.h` include
   fails the build there; locally it would compile the sketchbook's stock copy beside the patched
   one. This must print nothing (sources only: the READMEs name the include):
   `grep -rn --include="*.h" --include="*.cpp" --include="*.ino" "<SoftwareSerial.h>" Code/WCB`.
   The probe firmware bundles a byte-identical copy (`tests/hil/wcb_probe/src/EspSoftwareSerial`;
   `tests/hil/selftest.py` fails if they differ) - change both together. The Arduino-Code sketchbook
   keeps the stock library.
8. **A push to `main` touching `Code/**` publishes a release.** It auto-bumps the patch,
   stamps `String SoftwareVersion` in `WCB.ino` with a DTG (`6.1.1_241530RJUN2026`, US
   Eastern), compiles both targets, commits binaries, tags, and publishes a GitHub Release
   marked *Latest*. **Users flash from there.** Confirm before pushing to main.
   To start a new minor line, edit the semver in `WCB.ino` to e.g. `6.2.0` — the next release
   publishes `6.2.1`.
9. **Wizard-only and docs-only pushes must not rebuild firmware.** The ESP32 build stamps a
   fresh timestamp into the image every compile, so the binaries always differ and the
   auto-commit step would push new bins on every unrelated change. That's why `build.yml` is
   path-filtered to `Code/**` and Wizard deploys go through `pages-deploy.yml`. Preserve those
   filters.
10. **WDP is a wire format shared with `WCB_Client`.** New TLVs need both sides, and must not
    break an older peer's parse. See also rule 4 — WDP does not work with ETM off.
11. **Never restart the board from inside a command handler, and never block the UART from
    the ESP-NOW receive callback.** Both look local and are not. A config push is a *stream*
    of commands, so `ESP.restart()` inside one destroys every command still queued behind it —
    and the pusher cannot tell, because the boot banner satisfies its "did the board answer"
    test, so nothing retries and the push is scored as fully ACKed. Set a deferred flag and let
    `loop()` restart once the queue is empty **and quiet** (`PWM_REBOOT_QUIET_MS`) — quiet
    matters, because an ACK-paced push empties the queue between every pair of commands. A command
    that changes nothing is not activity (`quietWindowExempt`: a store-only `?STATS,RPT`, a re-arm
    of the running `?RTERM` session), and `RESTART_MAX_DEFER_MS` (20 s) restarts anyway once the
    request is that old — a client re-arming a terminal once a second held a `?reboot` off for
    good (tracker #93). The cap is sized to outlast the longest push tail the window protects (the
    commands that ask for a restart come last in a push); re-derive that before shrinking it. Once the
    restart is imminent (`restartImminent`) the ETM path neither ACKs nor queues a command, so nothing is
    ACKed and then lost in the last ~150 ms. Two flags
    feed that one restart point: `pwmRebootPending` (`WCB_PWM.cpp`) and the general `rebootPending`
    (`WCB.ino`), which `reboot()` and `eraseNVSFlash()` set. **Every restart reached from a command
    handler uses one of them** — `?reboot`, `?ERASE,NVS`, `?MAP,PWM,CLEAR,OUT` and `?PX` each took
    it inline until 2026-09-20, and an ETM-received one is ACKed on the WiFi task before `loop()`
    ever dequeues it, so a whole delay window of commands was ACKed and then thrown away. The OTA
    post-flash restarts (`WCB_OTA.cpp`) and the boot guard are not command handlers and stay inline.
    Both flags also wait for a running config-pull reply (`!configPullJobActive()`), which a restart
    would cut in half (docs/MGMT_RELAY.md). Likewise `espNowReceiveCallback` runs on the WiFi
    task: UART0 has no TX buffer, so a multi-KB `Serial.printf` there blocks for ~235 ms and,
    with HAL locks on, holds the UART0 mutex against `loop()` too. That is what fired the WiFi
    watchdog before (`WCB_RemoteTerm.cpp:14`). Queue the line and print it in `loop()`
    (`mgmtQueueOut` / `drainMgmtOut`).

12. **Every new `WCB_<area>.cpp` must `#include "WCB_RemoteTerm.h"` FIRST, or its output
    vanishes silently.** That header ends with `#define Serial WCBDebugSerial`, and
    `setup()` only ever calls `begin()` on the wrapper — the core's raw `Serial` object is
    never opened. A subsystem file that omits the include still compiles, still runs, and
    prints every line into an unopened port. The failure looks like a **dead command**: the
    dispatcher matches, the handler executes, state changes, and the operator sees nothing
    at all — not even "Unknown command", because the command *was* known. Cost a debugging
    session on `?WIFI`. Every existing subsystem file has the include on line 1; the only
    exemptions are `WCB_RemoteTerm.cpp` (it *is* the wrapper) and `wcb_pin_map.cpp` (all its
    prints are commented out). Check with:
    `for f in Code/WCB/*.cpp; do head -3 "$f" | grep -q WCB_RemoteTerm.h || echo "$f"; done`

13. **S3-S5 have no UART: RX is EspSoftwareSerial (vendored, rule 7), TX is an RMT channel** (`WcbSoftSerial`,
    `WCB_SoftSerial.{h,cpp}`). Don't put TX back on the library. Bit-banged TX was the root of a
    whole family of bugs: an interrupt mid-byte compressed the remaining bits (a user measured 45 %
    of H-CR commands on S5 arriving *cut short* under mesh load). The fix for that,
    `enableIntTx(false)`, masked core 1's interrupts for ~90 % of every byte, which blinded
    soft-serial RX on **all three** ports while any of them transmitted (tracker #16), starved the
    UART0 ISR into reading stale FIFO slots (#57), and needed a core-affinity rule for every writer.
    RMT clocks the bits out in hardware, so none of that applies. Measured: every TX arm 200/200 exact
    under mesh load, including the port that used to be "unprotected" (60-73/200), and soft RX
    while soft TX runs, 40/40.
    - `write()` blocks until the bytes are on the wire, but it sleeps on the driver instead of
      busy-waiting. It sends in chunks sized to fit the channel memory, so a transmission never
      waits on the RMT refill ISR. That ISR isn't IRAM-safe and is held off during every NVS
      write; a starved channel would put stale symbols on the wire.
    - **Channel budget:** ESP32 has 8 TX-capable RMT channels, ESP32-S3 has 4. The status LED
      (NeoPixel) takes one and S3-S5 one each, which **fills the S3 exactly**. Anything else that
      wants RMT on the S3 makes a soft port fall back to bit-banged TX. It prints
      `[SOFTSERIAL] S<n>: no RMT channel` at boot, and the old `applySoftSerialIntTx()` rules then
      govern that port again.
    - `;P` borrows a soft TX pin with `pinMode`; `noteTxPinBorrowed()` makes the next serial write
      route it back to RMT. A declared PWM-output port drops serial writes.
    - **Receive is a GPIO ISR**, which decodes from the time each edge's interrupt
      starts. So the GPIO ISR service is installed at **level 3** in `setup()`, ahead of any
      `attachInterrupt`, to pre-empt the level-1 UART/RMT interrupts. That took 19200 and 38400 from
      15-19/20 lines exact to 20/20. 57600 loses about 1% of lines in back-to-back bursts: it has only
      about 8.7 µs of ISR-latency margin. 115200 is still 0/20 (`?BAUD` warns). Keep it first. **Never
      add `ESP_INTR_FLAG_IRAM`** to it. Every handler is reached through Arduino's `__onPinInterrupt`,
      which is in flash (and so is the `micros()` the ISRs call), so an IRAM service runs flash code
      with the cache off during any NVS write and panics. A PWM input pulses every 20 ms, so that
      board would crash on its next save.
    - **On the classic ESP32, S3-S5 RX and PWM inputs are level-triggered** (tracker #78). ESP32
      erratum GPIO-3.14 (every revision, no fix) loses an *edge* interrupt on GPIO0-31 when the GPIO
      ISR's STATUS/W1TC handling of another pin lands on it. Measured on W1 with the edge ISR: 227 of
      10125 lines lost when two or three soft ports took edges ~1.5-3 µs apart, every single-port line
      exact. So the vendored library's `rxBitISR` and `pwmEdge()` (`WCB_PWM.cpp`) run on
      `ONLOW`/`ONHIGH` armed for the level the line is *not* at. They re-arm the opposite level first,
      record a transition only when the level changed, and re-read the pad before returning. **Never
      attach a `CHANGE`/`RISING`/`FALLING` interrupt on GPIO0-31 on the classic ESP32**: it is lost
      the same way and can steal the others' edges. The ESP32-S3 keeps edges, and has no such erratum.
      A soft port above ~74880 baud (115200) runs `rxBitSyncISR` on its FALLING edge on both chips.
      It busy-waits a frame, so a level trigger there would re-fire forever on a line held low. It
      stays exposed. `?DEBUG` prints each soft port's mode at begin (`[SOFTSERIAL] S<n> RX: ...`).
    - **A level arm survives `ESP.restart()` and a panic** (CPU-only resets), and armed with no handler it
      interrupt-WDT boot-loops the board, so `clearStaleGpioInterrupts()` disarms every pin *before*
      `gpio_install_isr_service` in `setup()` - keep it first (tracker #78; it looped wcb_probe 6).
    - **Reconfigure S3-S5 RX and PWM-input pins only from core 1** (`setup()`, `loop()`, the
      command handlers, `applyLiveBaud()`). The level ISR rewrites `GPIO_PINn_REG`'s `int_type`
      without the IDF spinlock, a read-modify-write that a core-0 `attachInterrupt`/`detachInterrupt`
      /`pinMode`/`gpio_config` on the same pin could interleave with. Never from `PWMTask`, the ESP-NOW
      callback or any other core-0 task.
    - **Soft-port reads suspend the scheduler** (`WcbSoftSerial::available/read/peek`).
      EspSoftwareSerial's `rxBits()` checks that its edge buffer is empty, and only then reads `micros()`.
      A core-1 time slice landing between the two injects a faux stop bit mid-byte: the byte's tail reads
      as 1s, and the bytes after it are framed from mid-byte. That lost 3 of 40 lines while S4 transmitted
      (tracker #63). Call these from task context only, and don't read S3-S5 with `readBytes()`, which
      isn't wrapped.
    - **Keep the UART0 (USB) interrupt on core 0** (`beginUsbSerialOnCore0()`). That was #57's fix,
      and it stays correct for the fallback path.
    - Some older mitigations still stand and are now merely harmless: `meshSerialWrite()` queueing
      S3-S5 chunks to core 1, and the HCR poll as one container frame (`HCR_POLL_FRAME`). Keep the
      poll as one frame anyway: status readers must never transmit, because a reply that overlaps a
      query is still lost on the device side.

14. **The heap is small, and a failed `String` allocation is silent.** With WiFi in AP mode a
    classic-ESP32 WCB has about 19 KB of byte-addressable heap, largest block 16-17 KB (`?STATS`
    prints it; `ESP.getMaxAllocHeap()` measures the wrong heap, #58). An Arduino `String` whose
    allocation fails comes back empty, and a failed append leaves it unchanged, so building or
    copying anything config-sized loses output with no error: `?backup` printed its restore
    chains as a bare `^?CHK<crc>` from about 2.5 KB (#90), and a long `?SEQ,SAVE` read as empty
    (#58). Stream big output in pieces (`printBackupConfig`), or walk the config once per piece
    and check each walk (the config pull's `configPullWalk`, which also counts a token whose
    String failed and reports it as out of memory instead of shipping the config without it).

15. **`[MGMT:CONFIG,<n>]` (packet type 6) carries a board's whole config or nothing.** Every Wizard
    from before 2026-09-24, including the copies frozen inside Intellex installs, stores ANY non-empty
    body under that tag as the board's config and baseline, without checking its `^?CHK`, and the next
    push writes it back. Parts and errors are packet type 18, printed as `[MGMT:CFGPART,` and
    `[MGMT:CFGERR,` (they differ before the comma, so old Wizards ignore them), and parts go only to a
    request that asked for them (`?MGMT,PULL,<n>,P`, type 19). Nothing else may ever travel as type 6
    or under that tag. See [docs/MGMT_RELAY.md](docs/MGMT_RELAY.md).

## Verifying

```bash
# Both targets, same as CI
Code/bin/build.sh
#   ESP32     : esp32:esp32:esp32:PartitionScheme=min_spiffs
#   ESP32-S3  : esp32:esp32:esp32s3:PartitionScheme=min_spiffs
#   core pinned to esp32:esp32@3.3.4

# Host tests — WDP wire format + election; config-pull parts (split, framing, UTF-8, error texts)
g++ -std=c++11 -Wall -Wextra -O2 tests/wdp_wire_test.cpp -o wdp_wire_test && ./wdp_wire_test
g++ -std=c++11 -Wall -Wextra -O2 tests/config_parts_test.cpp -o config_parts_test && ./config_parts_test

# Hardware-in-the-loop — real boards on Greg's bench, pyserial only (docs/HIL_TESTING.md)
python tests/hil/gui.py                  # the GUI Greg uses: devices, wiring, tests, log
python tests/hil/run.py                  # everything; or globs: "maestro.*" "wled.*"
python tests/hil/run.py --discover       # re-detect which probe header is wired to which WCB port
python tests/hil/run.py --list | --plan
python tests/hil/run.py "wizard.*"       # the Wizard in Chrome over real Web Serial (tests/wizard, Playwright)
python tests/hil/run.py --resume          # continue the last paused run (Ctrl+C once pauses)
python tests/hil/selftest.py             # harness self-test, no hardware

# Wizard parser — no browser, no board; also run in CI by .github/workflows/wizard-tests.yml
cd tests/wizard && npm run unit
```

The host tests are independent of the firmware build so they can never block a push. The HIL
suite needs the physical bench (`tests/hil/bench.json`), so no CI runs it — it changes saved
config, moves servos and reboots boards, and fails any test that leaves a board's config
different from how it found it. Close the Arduino IDE monitor and Wizard tabs first: a COM
port has one owner. What neither covers is left to the compiler and `TestConfigs/` (a manual
bench plan — `TestPlan.md`, `steps.md`, and per-board config fixtures).

The Wizard has no build step, so a syntax slip silently breaks all event wiring. All its JS
loads through `src=`, so `jscheck.js Wizard/index.html` finds no inline block and checks nothing;
check the files themselves, then run the no-board browser specs CI runs:

```bash
node --check Wizard/app.js && node --check Wizard/parser.js
cd tests/wizard && WIZ_HEADLESS=1 WIZ_NO_AUTHORIZE=1 npx playwright test specs/smoke.spec.js specs/kyber.spec.js specs/remote_pull_fake.spec.js
```

A static Wizard server is configured as launch config `wizard-static` (port 8777). It runs
`python -m http.server` (Python 3.14 with pyserial is on PATH via PyManager).

## Layout

| Path | What |
|---|---|
| `Code/WCB/` | the firmware sketch — one `WCB_<area>.{h,cpp}` pair per subsystem |
| `Code/WCB/src/` | vendored libraries (see rule 3) |
| `Code/bin/` | `build.sh` + committed release binaries |
| `Wizard/` | browser config tool — `app.js`, `parser.js`, `flasher.js`, `device-labels.js` |
| `PCB/` | KiCad hardware, V2.4 / V3.1 / V3.2 |
| `TestConfigs/` | bench test plan and fixtures |
| `tests/` | `wdp_wire_test.cpp` (host) · `hil/` hardware-in-the-loop harness, `hil/gui.py`, and `hil/wcb_probe/` instrument firmware · `wizard/` Playwright specs the harness runs (docs/HIL_TESTING.md §8) |

`wcb_pin_map.{h,cpp}` maps hardware revisions to pins — hardware differences belong there,
not in `#ifdef`s scattered through the subsystems.

## Conventions

- **The code is the source of truth.** Read `docs/` to orient, then confirm against the
  source before you act. Never assert a constant, TLV, field name, or pin from a doc alone —
  open the file and cite `file:line`. If a doc and the code disagree, the code is right and
  the doc is a bug.
- **The CI workflows are documentation.** They carry long comments explaining why each step
  is shaped the way it is (retry logic, path filters, rebase-and-retry on push). Read them
  before changing build behaviour, and keep the comments current. Comments in the source
  outrank the docs too — treat them as constraints.
- **Keep `docs/` current — same commit as the code.** A change to a **WDP TLV, an OTA step, a
  stored-variable shape, a passthrough parameter, a new `;`-verb, or the mesh/election
  behaviour** updates its page in the same commit, with a dated row in that page's **Revision
  log**. Doc bodies stay present-tense. A bug fix that restores documented behaviour needs
  nothing — but a fix whose *cause* is a trap (a race, a buffer limit, a core-affinity rule)
  earns a line recording the constraint. Full test in
  `~/.claude/docs/CONVENTIONS.md` § What earns a doc update.
  *The six existing `docs/` pages predate this rule and have no Revision log yet — add one to
  a page the first time you change it.*
- **A user-visible change likely needs a wiki edit** — `Wireless_Communication_Board-WCB.wiki`
  is a separate repo on branch `master`. **The wiki carries no changelog** — present tense,
  current state only.
