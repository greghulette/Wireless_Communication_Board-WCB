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
| OTA | [docs/WCB_OTA_TECHNICAL.md](docs/WCB_OTA_TECHNICAL.md) |
| WLED | [docs/WLED_INTEGRATION.md](docs/WLED_INTEGRATION.md) |
| Kyber passthrough | [docs/WCB_KYBER_PASSTHROUGH_PARAMS.md](docs/WCB_KYBER_PASSTHROUGH_PARAMS.md) |
| Device command translation | it's in [`WcbCmd`](https://github.com/greghulette/WcbCmd), not here |

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
4. **ETM is all-or-nothing across the fleet.** Every board must have ETM set the same way or
   messages are **silently ignored** — same for `?ETM,CHKSM`, where a mismatch rejects all
   packets. **WDP requires ETM enabled**: it rides the 252-byte ETM struct and gate
   (`WCB_WDP.h:20`), so turning ETM off also kills discovery. ETM packets are 252 bytes vs 249
   normal; CHKSM adds 12 bytes and drops the max command to 188 chars.
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
7. **`HumanCyborgRelationsAPI` is vendored, not installed.** A WCB-patched, serial-only copy
   sits at `Code/WCB/src/HumanCyborgRelationsAPI` and is pulled in by relative `#include`
   precisely so no differently-patched copy on a build machine can shadow it. Don't "fix"
   this by adding it as a library dependency.
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

## Verifying

```bash
# Both targets, same as CI
Code/bin/build.sh
#   ESP32     : esp32:esp32:esp32:PartitionScheme=min_spiffs
#   ESP32-S3  : esp32:esp32:esp32s3:PartitionScheme=min_spiffs
#   core pinned to esp32:esp32@3.3.4

# The only automated test in the ecosystem — WDP wire format + election, pure C++11
g++ -std=c++11 -Wall -Wextra -O2 tests/wdp_wire_test.cpp -o wdp_wire_test && ./wdp_wire_test
```

The WDP test is independent of the firmware build so it can never block a push. Everything
outside WDP is covered only by the compiler and by `TestConfigs/` (a manual bench plan —
`TestPlan.md`, `steps.md`, and per-board config fixtures).

The Wizard has no build step, so a syntax slip silently breaks all event wiring:

```bash
node C:\Users\ghulette\tools\jscheck.js Wizard/index.html
```

A static Wizard server is configured as launch config `wizard-static` (port 8777). It runs
`python -m http.server`, and **Python is not currently on PATH on this machine** — the config
needs a working interpreter before it will start.

## Layout

| Path | What |
|---|---|
| `Code/WCB/` | the firmware sketch — one `WCB_<area>.{h,cpp}` pair per subsystem |
| `Code/WCB/src/` | vendored libraries (see rule 3) |
| `Code/bin/` | `build.sh` + committed release binaries |
| `Wizard/` | browser config tool — `app.js`, `parser.js`, `flasher.js`, `device-labels.js` |
| `PCB/` | KiCad hardware, V2.4 / V3.1 / V3.2 |
| `TestConfigs/` | bench test plan and fixtures |
| `tests/` | `wdp_wire_test.cpp` |

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
