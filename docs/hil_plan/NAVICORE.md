# NaviCore full HIL coverage plan

> Written 2026-09-27 by planning agents that read the code (and, for WCB, a verified coverage re-scan), for the
> away week's goal: a complete HIL tool for the WCBs, NaviCore and Intellex. It is a plan, not a spec: line numbers
> drift, and any claim is to be confirmed against the code before acting on it. Progress is tracked in
> [../HIL_TEST_AUDIT.md](../HIL_TEST_AUDIT.md) §6 and decisions in [../HIL_WEEK_DECISIONS.md](../HIL_WEEK_DECISIONS.md).

*2026-09-27. Phase 2 of the away-week plan (`docs/HIL_TEST_AUDIT.md` §6): the NaviCore coverage plan, written for
the WCB repo's harness (`tests/hil`) and the Playwright specs beside it (decision D3: every test lives in this repo's
`tests/`). Input: `navicore_map.json` (358 features in five areas, the 59 NaviCore-related tests and their run
history, the bench survey).*

*How this was written: read-only. No COM port was opened, nothing was built, flashed or committed; the only harness
command run was `python tests/hil/run.py --list` (499 tests registered, 59 touch NaviCore). NaviCore line numbers
are its working tree, which carries uncommitted changes that are not this session's (tracker #7, #31, #42, #65,
the #70 twin and `kickUsbCdcTx`); after about `NaviCore.ino:2150` they sit a few lines below HEAD `dda37f6`. The bench
facts in §1.4 come from the latest run's GET_CONFIG (`results/20260927-174702/session.log`), parsed with every
password field deleted before anything was printed. Every `file:line` below was opened and checked unless it says
"per the map".*

## 0. Summary

- **Scope.** 358 NaviCore features (135 high, 165 medium, 58 low risk): the USB command surface and config
  persistence, the RC engine (taps, switches, knobs, dispatch gates, record/replay), hardware I/O (SBUS, the Maestro
  bus, aux serial, devices), the mesh (bridge, WDP, management relay, OTA, SoftAP and WebSocket, failure modes) and
  the config tool. Today 20 of them are covered well enough to need nothing new; the plan maps the other 338 to
  about 165 new or strengthened harness tests (7 of them wait for wiring) and about 70 config-tool tests (node and
  Playwright), in 14 work packages.
- **Three enablers come first**, because nearly every gap waits on one of them:
  1. a NaviCore driver with a verified config snapshot/restore (`nc_guard`), so tests may write NaviCore's config at
     all (no HIL test does today);
  2. a flash path that cannot brick the board: `?OTALOCAL` over the harness's own COM5 handle, never
     `arduino-cli upload`, with a recovery ladder proven before it is needed;
  3. a config-tool rig: a fake `navigator.serial` injected before the page's scripts, backed by an in-Node NaviCore
     emulator (no board, CI) or by the harness's own COM5 handle (with the board, unattended).
- **Two observation windows** make most of the engine testable with no new wiring and without moving a servo:
  NaviCore's remote Maestro slots 3-8 (devices nobody hosts, whose raw Pololu frames reach the W1S1 probe and the
  W2S1 tap byte-exact), and actions aimed at W1/W2 ports that probes already watch.
- **Blocked this week:** every byte on NaviCore's own pins (S3-S5, SBUS OUT, the Maestro bus), because no probe
  header is free and nobody can wire one (§6); the failsafe path waits for RAM-only test verbs in SBUSController
  (D-NC8).
- **45 decisions** are listed in §7, each with a recommendation. Those that change firmware or tool behaviour get a
  `(should)` test first, as the WCB review did.

## 1. Approach

NaviCore is driven only through its real interfaces, and every effect is observed where a user's hardware would
see it, or as close to it as this bench allows.

### 1.1 The interfaces

| Interface | How the harness drives it | Anchors |
|---|---|---|
| USB JSON + CLI (COM5, the S3's native USB-Serial/JTAG) | `SerialDevice` opens with DTR and RTS low, so attaching never resets NaviCore (`hil/serialdev.py:54-55`). Only lines starting `?`, `{` or `#`, or exactly `WCB_WEBTOOL_CONFIG_PULL`, are acted on (`NaviCore.ino:3773-3800`); a bare `PING` is silently dropped. The USB RX ring is 8 KB (`:4500`) and a line is capped at 98304 characters (`:4291`), so anything over 8 KB is written in 512-byte chunks 4 ms apart, as the tool does (`config_tool/index.html:5313-5321`). A lone debug line is held until more output follows; `#L12` flushes it (`docs/HIL_TESTING.md` §5). | `hil/navicore.py:7-57` |
| SBUS from the `sbus` device (COM4, SBUSController on WCB HW 3.2) | JSON verbs `a`, `sw`, `sl`, `tr`, `btn`, `lua`, plus `ping`, `getcfg`, `bootlog` (`SBUSController.ino:1048-1210`). Never `mode`, `cfg`, `wificfg`, or a bare `m`/`w` outside JSON: each saves to its flash (`:1213-1225`, `:1238`, `:1352`, `:1886-1892`). It sends SBUS-24 every 9 ms with a constant flags byte 0x00 (`:112`, `:120`, `:757-771`). | `suites/s21_navicore_sbus.py:1149-1207` |
| The mesh, from W1 and W2 | `;W20,<cmd>` typed on W1: CLI lines come back as `[TERM:20]`; JSON is bridged into `rcTelemetry::handle()` and answered as `{"sys":1,...}` lines on W1's USB inside its 20 s relay window. W2's own console for the far end. `_deaf_w1` (`s18_etm_config_wdp.py:293`) and the same `?MAC,3` flip on W2's console take a board off the air. | `s21:473-545` |
| The probe as a mesh client | `probe_in_mesh` (`suites/common.py:304`): MESH JOIN with any `PASS`, `CHK` or `TEMP`, `MSEND` of up to 511 characters, which the library fragments (`wcb_probe/probe_main.cpp:1062-1080`). Ids 1, 2, 6, 9, 19 and 20 are refused (`common.py:300`). NaviCore drops a sender's command whose sequence number falls in its 32-wide anti-replay window (`WCBClient/src/WCB_Client.cpp:879-898`), so a reused probe id first sends 33 no-ops, or uses an id NaviCore has not heard this boot. | |
| NaviCore's SoftAP and WebSocket | The PC's spare WiFi adapter joins NaviCore's AP through a temporary Windows profile, exactly as `wifi.pc_joins_ap_ws` joins W1's (`s28_wifi.py:187-266`); `hil/ws.py` (`:51-110`) speaks RFC 6455 to `ws://192.168.4.1/ws` (`navicore_wsserver.h:501-507`). The AP is up on the bench (`wifiEnabled` true); its SSID and password are read from GET_CONFIG at run time and never logged. | |
| NaviCore's serial ports into probes | None wired: all ten probe headers serve W1 and W2 (`results/links.json`). Designed now, run later (NC-WP14). | §6 |
| The config tool | Playwright with a fake `navigator.serial` defined before the page's own scripts, the pattern Intellex ships (`Intellex/src/intellex_shim.js:1-30`, `:270-300`). Behind it: an in-Node emulator (L1, no board) or the harness's COM5 handle through the bridge (L2, the real board). Real Web Serial only when someone is at the bench (L3). | §5 |

### 1.2 Where effects are observed

1. **Probe wires on W1/W2 ports.** A `wcb_unicast` action carrying `;S2HIL<tier><nonce>` to W1 lands byte-exact on
   the W1S2 probe, timestamped on the probe's clock so USB latency cancels (`docs/HIL_TESTING.md` §5, "Timing").
   Every tap, tier, delay, switch, calibration and replay test uses such markers instead of servo motion.
2. **The remote Maestro stream.** On this bench slots 3-8 are type 2 (remote) with devices 3-8, and nothing hosts
   those devices. `maestroWrite` puts a remote slot's raw Pololu frame into the broadcast `WCBStream(0,0)`
   (`NaviCore.ino:635-677`; the stream is created at `:4907`). W1 is Maestro_Remote whenever NaviCore is on the mesh
   (`kyber.wdp_auto_remote_revert`), so it forwards the frame to its S1, where probe1 reads it, and the W2S1 tap sees
   it too; real Maestro 2 ignores devices 3-8. Passthrough, easing speed/accel, idle release, goHome/stop/restart,
   subParam and replay interpolation are therefore byte-exact and servo-free: `?MAE,4,5,6000` must show exactly
   `AA 04 04 05 70 2E` (6000 = 0x70 + 0x2E<<7).
3. **The dispatch trace.** `SET_DEBUG_FLAGS` bits 0-6 (MAESTRO, WCB, WLED, HCR, MP3, SERIAL, DFP) print
   `[DISPATCH]` lines through a writer that never blocks (bits at `NaviCore.ino:495-501`; the writer at `:1557-1586`, per the map). They
   prove a local transport's payload text; the bytes themselves need NC-WP14 or the HIL-only `DBG_WIRE` dump
   (D-NC7).
4. **NaviCore's own Maestro 1** (J3, 57600 baud): `?MAE,GET/MOVING/ERR` read the real device back
   (`NaviCore.ino:3396-3439`). It moves the dome's pie and panel servos: allowed this week, and every such test is
   listed in `hil/servos.py`.
5. **W1's view of NaviCore:** the `?WDP,DUMP` row N=20, `?STATS` (including "Reported by Other Nodes"), `?VAR`
   values written by `;M!` replies, and `[TERM:20]` lines.

### 1.3 Rules every NaviCore test follows

- **Leave the bench as found.** A test that writes NaviCore's config runs inside `nc_guard` (§3, INF3), which
  restores and then proves the saved config byte-identical, as `config_guard` does for WCBs (`common.py:232-280`).
  Clips, the command library, learned peers, debug flags, the monitor, CALIB and the mode are put back the same way.
- **Credentials are never logged.** GET_CONFIG carries `wifiPassword` (`rc_config.h:1226`),
  `wcbNetwork.password` (`:1354`) and `wcbProfiles[].password` (`:1367`) in clear; `?backup` prints `?EPASS`; the
  controller's getcfg carries its WiFi networks. They are compared in memory or by hash, and never reach a note, an
  assertion message, a Playwright report or a committed fixture (D-NC5).
- **Servo motion is allowed** this week; every new test that can move one is added to `SERVO_TESTS`
  (`hil/servos.py:41-81`). Remote slots 3-8 move nothing.
- **Opt-ins** gate whatever reboots NaviCore, erases a flash slot, writes NVS or the clips partition, joins the PC
  to its AP, or routes a device to NaviCore's own pins (§3, the opt-in table; D-NC14).
- **`(should)` tests** for code-review findings, failing until the behaviour is decided and fixed
  (`docs/HIL_TESTING.md` §6).
- **Never cold power-cycle; never `arduino-cli upload` NaviCore.** A software restart (REBOOT, OTA END, `#L02`)
  does not re-sample the boot strap, but a cold power-up can land the S3 in ROM download mode, and nobody can
  replug it this week. Flash by `?OTALOCAL` (D-NC4).
- **One owner per COM port.** The bench is shared with the WCB and Intellex plans (`ListAgents` first). NaviCore
  suites are numbered s40-s49 so the WCB plan's new suites cannot collide (D-NC1).

### 1.4 Bench facts the tests are shaped around

From the latest GET_CONFIG, secrets removed:

- boardType 0 ("NaviCore v2" pins), txModel 0, `sbusOutEnabled` true, `wifiEnabled` true.
- `wcbNetwork`: deviceId 20, quantity 1, channel 1; two `wcbProfiles`.
- matrixChannel 7, matrixDebounceFrames 1, tapWindowMs 500, holdMs 750, switchSettleMs 80, chRateHz 5, maeGateMs
  250; `funcBindings.mode` 4 (switch SE on CH12).
- `auxBaud` S3 115200, S4 9600, S5 9600, Maestro 57600. `serialLabels` absent; `serialBcast` all off.
- `hcrDest` `{wcb, target 2}`; `mp3Dest` and `dfpDest` off; `wledSlots` id 1 on WCB 2.
- `modeReport` `;V,MODE,{mode}` to WCB 2; `statsReport` to WCB 1; `peerEvent` alert on, no actions.
- `maestros`: slot 1 local device 1 (24 channel entries), slots 2-8 remote devices 2-8.
- 34 mappings (101-118, 201, 207-213, 218, 304, 305, 311-315); slot 136 is free. Logical bands 22-36 are all 0/0.
- Knobs: J2 (CH24) to slot 1 ch 0 and J4 (CH4) to slots 2-7, both mode-aware with a 1500 ms release; RS (CH19) to
  slot 1 ch 2-9; S1/S2 are HCR-volume knobs.
- Switches: SC (CH10) has setEasing tiers on slots 1 and 2; SI (CH16) and SJ (CH17) broadcast `;W1;S3`, `;W2;S2`
  and `;W1;S1` text onto probe-wired ports.
- SBUS channels NaviCore binds nothing to: 1, 2, 14, 20, 21, 22. The controller's rx and ry sticks are CH1 and CH2
  (`hil/servos.py:68-69`), which gives two free, finely steerable inputs.
- The image on the board is `results/builds/navicore` (2026-09-22 21:02, HEAD plus the working tree; FLASHED.md). It
  predates the F13 WCB_Client, so `navicore.pull_over_limit` always takes its pre-F13 branch, and its PING reports
  the committed DTG `v0.2.0_102105QSEP26`, which cannot tell images apart.

## 2. Coverage matrix

Every one of the 358 features in `navicore_map.json` appears exactly once below (a generator checked it). A row may
group features that one test covers; the low-risk features of each area are collapsed into its last row. Feature ids
are the map's; the risk letter after each is the highest in its row.

**Legend.** *Covered today*: `strong`, `partial` and `smoke` are the audit's strengths for the existing test named;
`gap` means nothing asserts it. *Needs*: `w1`/`w2` a WCB's own USB console; `W1S2`, `W2S3`... a probe wire that exists
today; `W2S1 tap` the listen-only wire on real Maestro 2; `guard` NaviCore's `nc_guard` (a config write, restored);
`cguard W2` the existing `config_guard` on a WCB; `probe-mesh` `probe_in_mesh`; `sbus` the controller; `ctl-verbs`
the controller's RAM-only test verbs (INF8); `hook:DBG_WIRE`, `hook:FAULT` a `NAVICORE_HIL_HOOKS` build (INF9);
`opt:<key>` an opt-in (§3); `wire:probe3` blocked until a probe is wired to NaviCore; `servo` moves a real servo;
`L0`-`L3` the config-tool layers (§5). *WP*: the work package of §4.

**Where the 358 land:**

| Area | Features | Covered, nothing new | New or stronger, unattended | New, opt-in or attended | Needs a hook build / the controller verbs | Blocked by wiring or out of scope | Low risk, folded into another test |
|---|---|---|---|---|---|---|---|
| Command surface | 61 | 1 | 33 | 10 | 1 / 0 | 0 | 16 |
| RC engine | 65 | 4 | 41 | 13 | 1 / 1 | 0 | 5 |
| Hardware I/O | 63 | 6 | 25 | 11 | 1 / 6 | 7 | 7 |
| Mesh and remote | 88 | 9 | 45 | 22 | 0 / 0 | 1 | 11 |
| Config tool | 81 | 0 | 69 | 2 | 0 / 0 | 0 | 10 |
| **Total** | **358** | **20** | **213** | **58** | **3 / 7** | **8** | **49** |

A feature counts once, by its main test; one whose main test is unattended but has an opt-in part counts as opt-in.
"Unattended" for the config tool means no bench at all (L0/L1).

### Command surface, config schema and persistence (`command-surface`, 61 features)

| Feature (risk) | Covered today | Proposed test(s) | Needs | WP |
|---|---|---|---|---|
| `nc.io.line_framing` (H) | gap | `nccfg.line_framing_paced` (~20 KB SET_CONFIG paced 512 B / 4 ms gets its ACK; a 100 KB line gets ERROR rxLen 98304) | guard | WP1 |
| `nc.json.parse_error` (M) | partial: `navicore.test_action_rejects` (one truncated line) | `nccfg.json_errors` (IncompleteInput rxLen 14, InvalidInput, truncated SET_CONFIG gets ERROR not ACK) | - | WP1 |
| `nc.json.filter_whitelist` (H) | partial: indirectly via wcb_send / forget_peer / test_action | `nccfg.filter_whitelist` (one probe per key: target cmd on flags mode btn tap id all wcb key) | w1 (GET_WCB_SEQVAL to W1) | WP1 |
| `nc.json.ping` (M) | smoke: `navicore.ping` (version only) | `ncengine.calibration_gate` (PING clears CALIB) | guard, W1S2 | WP4 |
| `nc.json.get_config_shape` (H) | gap | `nccfg.get_config_shape` (key set; arrays 36/8/4/6/3; label keys; action type names) | - | WP1 |
| `nc.json.get_config_overflow_guard` (M) | untestable today | `nccfg.hook_get_config_overflow` | hook:FAULT, opt:navicore_fault | WP1 (late) |
| `nc.cfg.set_ack_shapes` (H) | gap | `nccfg.set_ack_shapes` (saveId echo; missing data; parse failed via a 20000-element array; save-fail via hook) | guard; the save-fail branch hook:FAULT | WP1 |
| `nc.cfg.diff_toplevel` (H) | gap | `nccfg.diff_toplevel` (one key changes only that key, plus the holdMs clamp caveat) | guard | WP1 |
| `nc.cfg.mappings_per_key` (H) | gap | `nccfg.mappings_per_key` (slot 136: add, {} clears, keys 436/137 ignored) | guard | WP1 |
| `nc.cfg.branch_semantics` (H) | gap | `nccfg.branch_semantics` (switches, knobs, thresholds, maestros, wledSlots, serialLabels, wcbProfiles, smoothProfiles, serialBcast, reports) | guard | WP1 |
| `nc.cfg.snapshot_restore_trap` (H) | n/a (driver design) | `nccfg.guard_selftest` (adds a mapping, channels and serialLabels; the guard restores byte-identical) | guard | WP1 |
| `nc.cfg.clamps_timing` (M) | gap | `nccfg.clamps_timing` | guard | WP1 |
| `nc.cfg.clamps_baud` (H) | gap | `nccfg.clamps_baud` (S5 0 and 230400 read 9600; board stays up; the sanBaud second clamp) | guard | WP1 |
| `nc.cfg.clamps_mesh` (M) | gap | `nccfg.clamps_mesh_channel` (channel 13 and 0 read 1; macOct masking); deviceId 21 boot failure: `ncboot.bad_device_id` | guard; deviceId part opt:navicore_identity (attended, D-NC14) | WP1/9 |
| `nc.cfg.string_truncation` (M) | gap | `nccfg.string_truncation` (120-char cmd reads 95; note 19; label 23; tmpl 47) | guard | WP1 |
| `nc.cfg.persist_reboot` (H) | gap | `nccfg.persist_reboot` (GET, REBOOT, GET byte-identical; saved N == len(data)) | opt:navicore_reboot | WP1 |
| `nc.cfg.load_fallbacks` (H) | untestable today | `nccfg.hook_config_unreadable` (corrupt /config.json, reboot, defaults this boot, file kept, restore) | hook:FAULT, opt:navicore_fault, opt:navicore_reboot | WP1 (late) |
| `nc.cfg.side_effects_live` (H) | gap | `nccfg.side_effects_live` (one re-open line per changed baud; label to W1 [WDPIF:N=20,S=3,DEV=HIL..]); `nccfg.sbus_out_toggle` | guard, w1 | WP1 |
| `nc.cfg.boardtype_change` (M) | gap | `nccfg.boardtype_change_no_reboot` (INFO then ACK; no re-open while flipped; restored before any reboot) | guard | WP1 |
| `nc.cfg.mesh_creds_reboot_only` (M) | gap | `nccfg.mesh_creds_live_split` (should, D-NC17): ;W20,?version loses [TERM:20] while ;W20,{TRIGGER} still runs | guard, w1 | WP1 |
| `nc.cfg.reset_defaults_ram_only` (H) | gap | `nccfg.reset_defaults_ram` (defaults in RAM, REBOOT brings the save back); `nccfg.reset_defaults_keeps_identity` (should, D-NC16) | guard, opt:navicore_reboot | WP1 |
| `nc.cfg.defaults_values` (M) | gap | `nccfg.reset_defaults_ram` (every default checked; the password only compared, never read from wcb_config.h) | as above | WP1 |
| `nc.cfg.wifi_fields` (H) | gap | `ncwifi.refuse_short_password` (REFUSED line, no AP seen by the PC, restored) | opt:navicore_reboot, opt:navicore_wifi | WP8 |
| `nc.cmdlib.set_get` (M) | gap | `nccfg.cmdlib_roundtrip` (ACK size/hash = host FNV-1a; GET bytes equal; META agrees; survives REBOOT with opt-in) | cmdlib guard (skips when none is stored, D-NC13) | WP1 |
| `nc.json.monitor` (H) | gap | `nccfg.monitor_stream` (~40 frames in 2 s; channels == #L09; modeCh; none 200 ms after STOP) | sbus (channels live) | WP1 |
| `nc.json.calib` (H) | gap | `ncengine.calibration_gate`; `ncrec.calib_drops_take` | guard, W1S2 | WP4/12 |
| `nc.json.reboot` (M) | gap | `ncboot.reboot_resets_ram_state` (ACK, re-enumeration, Reset reason 3, flags 0) | opt:navicore_reboot | WP9 |
| `nc.boot.banner` (M) | gap | `ncboot.banner_order` (14 lines in order; bauds and ids equal GET_CONFIG; bootloader kind recorded) | opt:navicore_reboot | WP9 |
| `nc.version.consistency` (M) | partial: `navicore.ping`, cli_over_mesh | `nccfg.version_surfaces` (PONG, mesh PONG, rc_hb fw, ?version, both WDP rows, ?OTALOCAL,STATUS; format regex) | w1 | WP1 |
| `nc.mgmt.backup_and_version` (M) | gap (identify.py reads it implicitly) | `nccfg.backup_block` (?WCB/?WCBQ/?MAC equal wcbNetwork; ?EPASS compared in memory only) | - | WP1 |
| `nc.mgmt.relay_verbs` (M) | strong: `navicore.mgmt_pull`; partial: pull_over_limit (pre-F13 branch only) | `ncmesh.mgmt_stats_etm_frag`; pull_over_limit's post-F13 branch after WP2 | w2 console, cguard W2 | WP6 |
| `nc.json.test_action` (M) | strong: test_action_usb, test_action_bad_target_not_ok; partial: test_action_rejects | `ncengine.test_action_matrix` | W1S2 | WP4 |
| `nc.json.trigger` (M) | strong: trigger_bounds_usb_vs_mesh; partial: trigger_dispatch_trace | `ncengine.tiers_exclusive_cumulative` (tap-4 exclusivity, cumulative t1+t2) | guard, W1S2 | WP4 |
| `nc.json.wcb_send` (M) | strong: wcb_send, wcb_send_edges, wcb_send_ack_reflects_refusal, wcb_send_fragmented_unicast | `ncmesh.online_tracking_flip` (11+ sends to a deaf W2: ok:false 'send refused') | cguard W2 | WP6 |
| `nc.json.wcb_status` (M) | partial: online[] in `navicore.online`, cli_codes | `ncmesh.wcb_status_content` (known, clients, temporary, aliases, portLabels, seqHash against each board) | w1, w2 console | WP6 |
| `nc.json.wcb_seq` (M) | gap | `ncmesh.seq_pull` (names/hash equal W1's; SEQVAL status 0/1/2; every error text; 'no reply' after 6 s) | w1 (s17 fixtures), cguard W1 | WP6 |
| `nc.telemetry.rc_stream` (M) | partial: `navicore.rc_relay_window` (types only) | `ncmesh.telemetry_fields_rates` | w1, guard (chRateHz) | WP6 |
| `nc.bridge.surface_divergence` (M) | partial: bridged_json_ack, mesh_json_declined, set_mode, trigger_bounds_usb_vs_mesh | `ncmesh.bridged_usb_only_types`; `ncmesh.bridged_reset_defaults`; `ncmesh.bridged_set_config_strip` | w1, guard | WP6 |
| `nc.bridge.set_config_strip` (H) | gap | `ncmesh.bridged_set_config_strip` (deviceId/quantity unchanged, ACK still from 20; channel stripped (should, D-NC18)) | w1, guard | WP6 |
| `nc.ws.surface_parity` (M) | gap | `ncwifi.ws_parity` (PING, GET_CONFIG hash, TRIGGER, TEST_ACTION, ?REC,LS over WS equal USB) | opt:navicore_wifi, spare WiFi adapter | WP8 |
| `nc.rec.cli_surface` (M) | partial: rec_info_list, rec_play_clip | `ncrec.record_save_list_rm` | opt:navicore_clip_write | WP12 |
| `nc.mae.cli` (M) | partial: mae_cli_local (smoke, accepts timeout); strong: maestro_mesh_settarget_readback | `ncdev.mae_local_frames_readback`; `ncdev.mae_remote_read`; fix mae_cli_local (D-NC15) | servo (local), w2 (remote read) | WP7 |
| `nc.cli.relayed_parity` (M) | partial: `navicore.cli_over_mesh` | `ncmesh.remote_cli_order_and_drop` | w1 | WP6 |
| `nc.io.usb_tx_stall_recovery` (H) | implicit only (every NaviCore test) | `nccfg.usb_no_late_reply` (800 back-to-back PINGs, each answered before the next) | the WP2 image (kickUsbCdcTx) | WP1/2 |
| Low risk, collapsed (17) | - | `nc.io.prefix_routing`: `nccfg.json_errors` (bare PING and 'hello' get silence within 1 s; ping JSON gets PONG); `nc.json.unknown_type`: `nccfg.json_errors` ({} 5 get_config GET_WCB_META SET_MODE wcb_alias each 'unknown type'); partial today: `navicore.set_mode`; `nc.cfg.set_nonobject_data`: `nccfg.set_nonobject_data` (USB data:5 ACKs ok and saves; bridged is dropped with no ACK); `nc.cfg.unvalidated_fields`: `nccfg.unvalidated_echo` (stored as sent); boardType 2 identity mismatch in `ncboot.boardtype2_mismatch` (D-NC19); `nc.cfg.dest_default_hazard`: `nccfg.dest_null_hazard` (should, D-NC22: {} must read off, not wcb/2); `nc.cmdlib.empty_and_errors`: `nccfg.cmdlib_errors` (scalar/unbalanced/absent data gets ok:false 0/0); `nc.cmdlib.bridge_divergence`: `ncmesh.bridged_cmdlib` (should: keys after data store data only); `nc.cli.unknown_and_prefix_quirks`: `nccfg.json_errors` (the ?OTALOCAL/?REC/?FORGET/?WDP/?MGMT/# quirks; silent ?MGMT errors pinned); `nc.cli.hash_codes`: partial today: cli_codes, l1_names_board_profile, sbus_live_dump_toggle; #L12 format in `nccfg.json_errors`; #L02 in `ncboot.reboot_resets_ram_state`; #L20/#L21 in `ncdev.cli_hcr_test_codes` (opt:navicore_aux_tx); `nc.json.forget_peer`: partial today: forget_peer_safe; a real forget in `ncmesh.wdp_learn_forget` (opt:navicore_nvs); `nc.json.debug_flags`: `nccfg.debug_flag_bits` (per-bit gating); reset at boot in `ncboot.reboot_resets_ram_state`; `nc.json.mesh_stats`: covered: `navicore.mesh_stats_counts`, bridged_json_ack; `nc.cfg.stats_report`: `ncengine.stats_report_content` (W1's 'Reported by Other Nodes' row equals GET_MESH_STATS agg; wcb 0 sends none); `nc.cfg.mode_report`: `ncengine.mode_report_content`; `nc.cfg.peer_event`: `ncboot.new_peer_after_boot`; `nc.status.transition_lines`: `ncmesh.online_tracking_flip` (OFFLINE then ONLINE lines); `nc.ota.local_status`: `ncota.local_status_parse` | - | - |

### RC engine: taps, switches, knobs, dispatch, record/replay (`engine-rc`, 65 features)

| Feature (risk) | Covered today | Proposed test(s) | Needs | WP |
|---|---|---|---|---|
| `nc.matrix.band_decode` (H) | partial: `sbus.btn_single_tap` (one physical slot) | `sbus.matrix_logical_band` | sbus, guard (a logical band 22-36 and an overlap) | WP5 |
| `nc.matrix.debounce_arm` (H) | partial: `sbus.btn_*` at debounce 1 only | `sbus.matrix_debounce_n`; `sbus.one_frame_dip`; `sbus.boot_quiet` (button held across boot) | sbus, guard; the dip needs ctl-verbs; the boot part opt:navicore_reboot | WP5/11/9 |
| `nc.taps.single_deferred` (H) | strong: `sbus.btn_single_tap` | - | - | - |
| `nc.taps.multi_tap` (H) | strong: `sbus.btn_double_triple_tap` (2 and 3 taps) | `sbus.tap_saturation_4` | sbus | WP5 |
| `nc.taps.other_button_commits` (M) | gap | `sbus.other_button_commits` | sbus (two unmapped matrix buttons) | WP5 |
| `nc.taps.mode_latched_at_press` (M) | partial: `sbus.mode_sets_trigger_mode` (mode set before the press) | `sbus.mode_latched_at_press` | sbus, w1 (mesh SET_MODE inside the window), servo | WP5 |
| `nc.taps.exclusive_vs_cumulative` (H) | partial: `navicore.trigger_dispatch_trace` (tap 1, DBG lines only) | `ncengine.tiers_exclusive_cumulative` | guard (slot 136), W1S2 | WP4 |
| `nc.taps.long_press` (H) | partial: `sbus.btn_hold_unconfigured_long` (no t4) | `sbus.long_press_configured`; `ncengine.tiers_exclusive_cumulative` (USB tap 4) | sbus, guard, W1S2 | WP5/4 |
| `nc.trigger.usb` (M) | strong: `navicore.trigger_bounds_usb_vs_mesh`; partial: trigger_dispatch_trace | `ncengine.tiers_exclusive_cumulative` (tap 4 over USB) | guard, W1S2 | WP4 |
| `nc.trigger.mesh` (M) | smoke: `navicore.trigger`; strong: trigger_bounds_usb_vs_mesh | `ncengine.mesh_trigger_burst` (more than 8 in one loop pass) | w1 | WP4 |
| `nc.dispatch.rc_trig_event` (M) | partial: the USB line in `sbus.btn_*` and `navicore.trigger`; the mesh copy is only noted | `ncengine.rc_trig_emit` (empty tier, under CALIB, USB and relayed copies equal) | w1 (relay window) | WP4 |
| `nc.switch.position_decode` (H) | gap (`sbus.switch_exact` checks decode, not tiers) | `sbus.switch_tiers_settle_seed` | sbus, guard (SA rebound to CH1, the rx axis; tiers ;S2HILS&lt;n>), W1S2 | WP5 |
| `nc.switch.settle` (H) | gap | `sbus.switch_tiers_settle_seed` (settle >= switchSettleMs; a 0 to 2 sweep never fires p1) | as above | WP5 |
| `nc.switch.seed_no_fire` (H) | gap | `sbus.switch_tiers_settle_seed` (no fire after SET_CONFIG); `sbus.boot_quiet` (none after REBOOT) | as above; the boot part opt:navicore_reboot | WP5/9 |
| `nc.switch.easing_seed` (M) | gap | `sbus.switch_easing_seed` (setEasing on remote slot 4: AA 04 07/09 on W1S1, tier not run) | sbus, guard, W1S1 | WP5 |
| `nc.mode.switch_decode` (H) | gap (only mesh SET_MODE is tested) | `sbus.mode_switch_decode` (SE on CH12: #L12, PWM_UPDATE mode, rc_mode; 2-position gives 1/3; deadband 5) | sbus, w1, servo (mode-aware J2/J4 snap) | WP5 |
| `nc.mode.set_mode_mesh` (M) | strong: `navicore.set_mode` | - (rc_telemetry.h:2231 comment is wrong: doc fix) | - | - |
| `nc.mode.report` (M) | gap (mesh_stats_counts uses its timing only) | `ncengine.mode_report_content` (;S2HILM{mode} to W1, after SET_MODE and again ~60 s later) | guard, W1S2, servo (SET_MODE) | WP4 |
| `nc.knob.passthrough_map` (H) | gap (`sbus.to_navicore` decodes a knob with no outputs) | `sbus.knob_passthrough_remote` (knob to remote slot 4: exact AA 04 04 ch lo hi per the formula) | sbus, guard, W1S1; no servo (device 4 is hosted by nobody) | WP5 |
| `nc.knob.deadband` (M) | gap | `sbus.knob_passthrough_remote` (a 4-count move sends nothing, 5 sends one frame) | as above | WP5 |
| `nc.knob.boot_prime` (H) | gap | `sbus.boot_quiet` (no AA .. 04 frames during boot with the stick off-centre) | sbus, guard, W1S1, opt:navicore_reboot | WP9 |
| `nc.knob.reverse_midclosed` (M) | gap | `sbus.knob_passthrough_remote` (reverse and midClosed variants) | as above | WP5 |
| `nc.knob.mode_aware` (H) | gap | `sbus.knob_mode_aware` (outputs/2/3 to three slot-4 channels; an override knob is not re-armed by SET_MODE) | sbus, guard, w1, W1S1, servo (SET_MODE moves J2) | WP5 |
| `nc.knob.hcr_volume` (M) | gap | `sbus.knob_hcr_volume` (;H,VOL,A,&lt;v> at W2, at most one per 80 ms, the last value lands) | sbus, guard (knob function 2), cguard W2 (?HCR on a probe port) | WP5 |
| `nc.knob.easing_resolve` (M) | gap | `sbus.knob_easing_resolve` (07/09 frames once per change, not per frame) | sbus, guard, W1S1 | WP5 |
| `nc.knob.auto_release` (H) | gap | `sbus.knob_auto_release` (AA 04 04 ch 00 00 after releaseIdleMs; re-arm; reset on apply) | sbus, guard, W1S1 | WP5 |
| `nc.dispatch.delay_queue` (M) | gap | `ncengine.delay_queue` (parallel delays on the probe clock; the 9th fires at once with the WARN line) | guard, W1S2 | WP4 |
| `nc.dispatch.calibration_gate` (H) | gap | `ncengine.calibration_gate`; `sbus.calibration_mutes_knobs`; the replay exemption in `ncrec.replay_gate` | guard, W1S2; the knob part sbus | WP4/5 |
| `nc.dispatch.replay_gate` (H) | gap | `ncrec.replay_gate` | opt:navicore_clip_write, W1S2 | WP12 |
| `nc.action.wcb_unicast`, `nc.action.wcb_broadcast` (H) | strong: `navicore.test_action_usb`, wcb_send_broadcast_exec | - | - | - |
| `nc.action.skip_running_via_wcb` (M) | gap | `ncengine.skip_running_fail_open` (stale cache: ;M4,getMovingState to the target, the verb still goes) | w1 (?DEBUG ETM), W1S1 | WP4 |
| `nc.action.maestro_frames` (H) | gap for the action path (only inbound ;M is tested) | `ncdev.mae_remote_verbs` (every verb, the clamps, ch 32 refused, the 35-character cmd cut) | W1S1 (and the W2S1 tap) | WP7 |
| `nc.action.maestro_skip_running_slot` (M) | gap | `ncengine.maestro_skip_running_slot` (local slot skipped while a slow move runs; remote fails open) | servo (local slot 1, low setSpeed) | WP4 |
| `nc.action.easing_verbs` (M) | gap | `ncdev.easing_repeat_frames` (bursts at t0, +500 and +1000 ms on the probe clock) | W1S1 | WP7 |
| `nc.action.serial` (M) | partial: `navicore.test_action_usb` (DBG line for S3) | `ncdev.serial_action_bytes` (DBG_WIRE hex; an invalid port is silent) | hook:DBG_WIRE; the wire itself: wire:probe3 | WP7/14 |
| `nc.action.hcr_remote` (H) | gap | `ncdev.hcr_remote_verbs` (every verb; W2's HCR bytes on a probe port) | cguard W2 (?HCR on a probe-wired port); hcrDest already goes to W2 | WP7 |
| `nc.action.hcr_local` (M) | gap | `ncdev.hcr_local_payload` (the DBG_HCR payload equals W2's bytes for the same verb) | guard (hcrDest serial S4), opt:navicore_aux_tx, hook:DBG_WIRE for exact bytes | WP7 |
| `nc.action.mp3` (M) | gap | `ncdev.mp3_remote`; `ncdev.local_device_bytes` | guard (mp3Dest), cguard W2 (?MP3 on a probe port); local: opt:navicore_aux_tx and hook | WP7 |
| `nc.action.dfplayer` (M) | gap | `ncdev.dfp_remote` (with the DEVICE fn 17 regression 7E FF 06 09 00 00 02 FE F0 EF); `ncdev.local_device_bytes` | guard (dfpDest), cguard W2; local as above | WP7 |
| `nc.action.wled` (M) | gap | `ncdev.wled_remote` (slot id 1 already goes to W2); `ncdev.local_device_bytes` | cguard W2 (?WLED,1 on a probe port); local as above | WP7 |
| `nc.action.parse_limits` (M) | gap | `nccfg.mappings_per_key` (5 per tier, retired type dropped, 95/19/5 cuts) | guard (slot 136) | WP1 |
| `nc.action.test_action` (M) | strong: test_action_usb, test_action_bad_target_not_ok; partial: test_action_rejects | `ncengine.test_action_matrix` (ok:true for a skipped action: pinned or (should), D-NC20) | W1S2 | WP4 |
| `nc.rec.record_toggle` (M) | gap | `ncrec.record_save_list_rm` | opt:navicore_clip_write, W1S2 | WP12 |
| `nc.rec.capture_scope` (M) | gap | `ncrec.record_save_list_rm` (TEST_ACTION and delayed actions captured; setSpeed captured, a doc bug) | as above | WP12 |
| `nc.rec.play_toggle` (M) | partial: `navicore.rec_play_clip` (CLI PLAY, opt-in) | `ncrec.play_timing_markers` (the play action toggles) | opt:navicore_clip_write, W1S2 | WP12 |
| `nc.rec.stop_semantics` (M) | gap | `ncrec.stop_semantics` (the action saves; ?REC,STOP keeps the take for ?REC,SAVE) | as above | WP12 |
| `nc.rec.replay_timing` (H) | partial: `navicore.rec_play_clip` (duration only) | `ncrec.play_timing_markers` (marker spacing on the probe clock) | as above | WP12 |
| `nc.rec.replay_interpolation` (H) | gap | `ncrec.replay_interpolation_remote` (uploaded clip on slot 4: 07/09 = 0 first, then a dense ramp on W1S1) | opt:navicore_clip_write, W1S1 | WP12 |
| `nc.rec.clip_files` (M) | partial: `navicore.rec_info_list` (LS only) | `ncrec.record_save_list_rm` (RENAME refuses to overwrite, RM); v1 migration: a host test of navicore_record.h | as above | WP12 |
| `nc.rec.editor_transport` (M) | gap | `ncrec.replay_interpolation_remote` (EDITBEGIN/EV/END upload, ranged EDITLOAD download) | as above | WP12 |
| `nc.safety.failsafe_freeze` (H) | gap (untestable today: the flags byte is a constant 0x00) | `sbus.failsafe_flag_freeze`; `sbus.failsafe_deferred_tap` (should, D-NC21) | ctl-verbs (D-NC8) | WP11 |
| `nc.safety.frame_stop` (H) | partial: `sbus.signal_loss_controller_reset` (no press in flight) | `sbus.frame_stop_held_press` (should, D-NC21) | ctl-verbs stream off (or opt:sbus_reset) | WP11 |
| `nc.safety.boot_quiet` (H) | gap | `sbus.boot_quiet` | sbus, guard, W1S1, W1S2, opt:navicore_reboot | WP9 |
| `nc.safety.reconfig_live` (H) | gap | `sbus.reconfig_live` (a parked mapped switch does not fire on save; a delayed copy survives; USB vs mesh matrix reset) | sbus, guard, W1S2 | WP5 |
| `nc.safety.reset_defaults` (M) | gap | `nccfg.reset_defaults_ram` (USB); `ncmesh.bridged_reset_defaults` (the mesh runs the side effects) | guard; the reboot part opt:navicore_reboot | WP1/6 |
| `nc.config.timing_clamps` (M) | gap | `nccfg.clamps_timing` | guard | WP1 |
| `nc.config.partial_object_traps` (H) | gap | `nccfg.branch_semantics` (whole-object replace per branch) | guard | WP1 |
| `nc.config.engine_json_shape` (M) | gap (s21 reads GET_CONFIG, asserts no shape) | `nccfg.get_config_shape` | - | WP1 |
| `nc.event.peer_new` (M) | gap | `ncboot.new_peer_after_boot` (with guarded peerNewActions ;S2HIL on W1S2) | opt:navicore_reboot, guard, W1S2 | WP9 |
| Low risk, collapsed (5) | - | `nc.taps.held_later_tap_fires_midhold`: `sbus.held_second_tap_midhold` (pin today's mid-hold fire; fix the comment and ARCHITECTURE.md:304); `nc.action.maestro_local_legacy`: one maestro_local case in `ncdev.mae_remote_verbs` (slot-1 routing); `nc.action.hcr_validation`: `ncdev.hcr_remote_verbs` (chan 4 refused; fn 20/21 fall through to ;H,FN); `nc.rec.backstop`: `ncrec.backstop_60s` (61 s of recording, opt-in); `nc.rec.cli`: partial today (INFO, LS, missing PLAY); the rest in `ncrec.record_save_list_rm` | - | - |

### Hardware I/O: SBUS, Maestro, aux serial, devices, board (`hardware-io`, 63 features)

| Feature (risk) | Covered today | Proposed test(s) | Needs | WP |
|---|---|---|---|---|
| `nc.sbus.uart` (H) | partial: `sbus.discover` (fps and variant) | `ncboot.banner_order` (the '[SBUS] IN+OUT share Serial1/UART1 - RX GPIO4 / TX GPIO5, 100k 8E2 inverted' line) | opt:navicore_reboot | WP9 |
| `nc.sbus.autodetect` (H) | partial: SBUS-24 only (`sbus.discover`) | `sbus.sbus16_autodetect` (variant, chCount 16, CH17-24 = 992, #L13 25 bytes) | ctl-verbs (mode without save) | WP11 |
| `nc.sbus.lock` (H) | partial: `sbus.signal_loss_controller_reset` (relock after a reset) | `sbus.lock_after_glitch` (truncate/garbage/double/gap reset the streak; nothing decodes before lock) | ctl-verbs | WP11 |
| `nc.sbus.partial_never_flushed` (H) | gap (every sbus.* test runs NaviCore idle) | `sbus.lock_under_load` (DBG 0x7F, monitor, ?MAE bursts, GET_CONFIG: fps >= 100, variant constant, counter monotonic) | sbus | WP5 |
| `nc.sbus.prefix_ambiguity` (H) | gap | `sbus.prefix_ambiguity_ch17` (the controller control on CH17, found in getcfg, at 172 with CH18 resting at 992 makes frame[24]=0x00; 20 s under load, no phantom failsafe); `sbus.prefix_ambiguity_raw` | sbus (SJ's p0 tier sends text to W2S2, a probe wire); raw form ctl-verbs | WP5/11 |
| `nc.sbus.backlog_split` (M) | gap | `sbus.lock_under_load` (forced 30-60 ms stalls: counter keeps rising, values unchanged) | sbus | WP5 |
| `nc.sbus.overflow_misalign` (H) | gap (hypothesis) | `sbus.stall_no_phantom` (20 no-op SET_CONFIG stalls of 100+ ms: no rc_trig, no switch dispatch, no PWM_UPDATE excursion) | sbus, guard | WP5 |
| `nc.sbus.decode` (H) | strong: `sbus.switch_exact`, slider_exact, trim_exact | - (0 and 2047 unreachable: the controller clamps 172-1811) | - | - |
| `nc.sbus.flags` (H) | gap (the controller always sends flags 0x00) | `sbus.failsafe_flag_freeze`; `sbus.lost_frame_flag_no_gate` | ctl-verbs | WP11 |
| `nc.sbus.failsafe_freeze` (H) | gap | `sbus.failsafe_flag_freeze` (no rc_trig, knob or switch output; neutral needed after; ?MAE positions hold; rc_hb sbusFail) | ctl-verbs, W1S1, w1 | WP11 |
| `nc.sbus.signal_loss` (M) | partial: `sbus.signal_loss_controller_reset` (opt:sbus_reset) | `sbus.frame_stop_held_press` (stream-off verb, no reset needed); the PWM_UPDATE ok and ageMs fields in `nccfg.monitor_stream` | ctl-verbs | WP11 |
| `nc.sbus.monitor_stream` (M) | gap (tests read #L09) | `nccfg.monitor_stream` | sbus | WP1 |
| `nc.map.mode_switch` (H) | partial: `navicore.set_mode`, `sbus.mode_sets_trigger_mode` (mesh path) | `sbus.mode_switch_decode` | sbus, servo | WP5 |
| `nc.map.switch_decode` (M) | gap | `sbus.switch_tiers_settle_seed` | sbus, guard, W1S2 | WP5 |
| `nc.map.matrix_decode` (M) | strong: `sbus.btn_*` | `sbus.matrix_logical_band` | sbus, guard | WP5 |
| `nc.map.knob_passthrough` (H) | gap | `sbus.knob_passthrough_remote`; `ncdev.knob_local_readback` (J2-style local output read with ?MAE,GET) | sbus, guard, W1S1; the local variant servo | WP5 |
| `nc.map.hcr_volume_knob` (M) | gap | `sbus.knob_hcr_volume` (the knob clamps 99 while the codec allows 100: pinned) | sbus, guard, cguard W2 | WP5 |
| `nc.map.calibration_mute` (H) | gap | `sbus.calibration_mutes_knobs` (no frames under CALIB; PING resumes; idle release NOT gated, pinned) | sbus, guard, W1S1 | WP5 |
| `nc.mae.serial2` (H) | partial: mae_cli_local, maestro_mesh_settarget_readback | `ncboot.banner_order` ('[Serial2] Local Maestro open @ 57600 baud TX=GPIO6'); `nccfg.side_effects_live` (maestro baud change re-opens once) | opt:navicore_reboot; guard | WP9/1 |
| `nc.mae.boot_tx_high` (M) | gap | `sbus.boot_quiet` (?MAE,GET and ?MAE,ERR unchanged across a reboot) | opt:navicore_reboot, servo | WP9 |
| `nc.mae.frames` (H) | partial: maestro_mesh_settarget_readback (inbound setTarget) | `ncdev.mae_local_frames_readback` (16384 reads 16383; ch 32 refused; ERR stays 0); `ncdev.mae_remote_verbs` (exact bytes) | servo (local), W1S1 (remote) | WP7 |
| `nc.mae.subroutine_msb` (M) | gap (finding) | `ncdev.mae_subroutine_msb` (should, D-NC23: restartScript,200 must not put 0xC8 on the wire) | W1S1; the local variant sets Maestro 1's error bit, cleared by ?MAE,ERR | WP7 |
| `nc.mae.query` (M) | smoke: mae_cli_local; strong: maestro_mesh_query_reply | `ncdev.mae_remote_read` (type-2 slot 2: ;M2,getPosition broadcast, :MQR, async [MAE:2]) | w2 (real Maestro 2 answers) | WP7 |
| `nc.mae.skip_gate` (M) | gap | `ncengine.maestro_skip_running_slot` | servo | WP4 |
| `nc.mae.auto_release` (M) | gap | `sbus.knob_auto_release` (remote); `ncdev.knob_local_readback` (a released channel reads 0: assumption checked) | sbus, guard; local servo | WP5 |
| `nc.mae.easing` (M) | gap | `ncdev.mae_local_easing_timing` (?MAE,MOVING 1 and an intermediate GET ~100 ms after a step); `ncdev.easing_repeat_frames` | servo; W1S1 | WP7 |
| `nc.mae.inbound_mesh` (H) | strong: `navicore.maestro_mesh_*` (fanout_0_9 partial) | - (add ?MAE,GET readback to fanout_0_9) | servo | WP7 |
| `nc.mae.inbound_query` (M) | strong: `navicore.maestro_mesh_query_reply` | - | - | - |
| `nc.mae.wdp_advert` (M) | strong: `navicore.maestro_inventory` | - (never change slot devices: W1/W2 proxies are never evicted) | - | - |
| `nc.mae.remote_raw` (H) | gap | `ncdev.mae_remote_stream_exact` (?MAE,4,5,6000 gives AA 04 04 05 70 2E on W1S1 and the W2S1 tap; 16 channels frame-aligned across packets) | W1S1, W2S1 tap; no servo | WP7 |
| `nc.aux.binding` (H) | partial: serial_route_dbg (labels) | `ncboot.banner_order` ('[AUX] S3/S4/S5 open @' lines); `ncwire.aux_bytes` | opt:navicore_reboot; wire:probe3 | WP9/14 |
| `nc.aux.baud_apply` (M) | gap | `nccfg.side_effects_live`; `nccfg.boardtype_change_no_reboot` | guard | WP1 |
| `nc.aux.soft_tx_interrupts` (H) | blocked (finding) | `ncwire.soft_tx_integrity` (200 long lines on S4/S5 under mesh and SBUS load: 0 RXERR, 0 mismatches) | wire:probe3 | WP14 |
| `nc.aux.uart0_console_leak` (M) | blocked (plausible) | `ncwire.s3_console_quiet` (nothing on S3 TX at boot or on an IDF error) | wire:probe3, hook:FAULT | WP14 |
| `nc.aux.ra_serial` (M) | partial: test_action_usb (DBG line) | `ncdev.serial_action_bytes` (DBG_WIRE hex; the #L09 fps dip during a 95-char S4 write at 9600) | hook:DBG_WIRE | WP7 |
| `nc.aux.mesh_forward` (M) | partial: serial_route_dbg | `ncdev.mesh_forward_burst` (the 5th of a burst dropped: depth 4; order kept) | w1 | WP7 |
| `nc.aux.tx_interleave` (M) | blocked (plausible) | `ncwire.tx_interleave` | wire:probe3 | WP14 |
| `nc.hcr.local` (H) | gap | `ncdev.hcr_local_payload` | guard, opt:navicore_aux_tx, hook:DBG_WIRE | WP7 |
| `nc.hcr.wcb` (M) | gap | `ncdev.hcr_remote_verbs` (V PlayWAV/StopWAV numeric forms; fade only A/B) | cguard W2 | WP7 |
| `nc.hcr.fade` (M) | gap (bytes blocked) | `ncdev.hcr_local_payload` (start line; transport change cancels); steps on a wire: `ncwire.aux_bytes` | opt:navicore_aux_tx; wire:probe3 | WP7/14 |
| `nc.mp3.local` (M) | gap | `ncdev.local_device_bytes` (v vol then t track; VOLUP/DN step 5; shadow 20 after boot) | guard, opt:navicore_aux_tx, hook:DBG_WIRE | WP7 |
| `nc.mp3.wcb` (M) | gap | `ncdev.mp3_remote` | guard (mp3Dest wcb 2), cguard W2 | WP7 |
| `nc.dfp.local` (M) | gap | `ncdev.local_device_bytes` (10-byte frames and checksum; VOL 0-30, step 2) | guard, opt:navicore_aux_tx, hook:DBG_WIRE | WP7 |
| `nc.wled.route` (M) | gap | `ncdev.wled_remote`; `ncdev.local_device_bytes` (JSON plus newline on a local port) | cguard W2; local opt:navicore_aux_tx | WP7 |
| `nc.sbusout.tee` (M) | blocked | `ncwire.sbus_out_tee` (probe HW channel 100000 8E2 INV RXONLY: bytes equal #L13, ~9 ms period); the enable/disable log lines in `nccfg.sbus_out_toggle` | wire:probe3; guard | WP14/1 |
| `nc.board.profile` (H) | partial: l1_names_board_profile, cli_codes | `ncboot.banner_order` ('[BOARD] NaviCore v2 pin profile'); `ncmesh.wdp_identity_fields` (HWREV); the WCB 3.2 profile is out of scope | opt:navicore_reboot | WP9/6 |
| `nc.timing.nonblocking_logs` (H) | gap | `sbus.lock_under_load` (flags 0x7F plus the monitor: fps >= 100) | sbus | WP5 |
| `nc.timing.stall_sources` (M) | gap | `sbus.stall_no_phantom`; `ncdev.serial_action_bytes` (fps dip) | sbus, guard | WP5/7 |
| `nc.replay.outputs` (M) | partial: `navicore.rec_play_clip` | `ncrec.replay_interpolation_remote` | opt:navicore_clip_write, W1S1 | WP12 |
| `nc.sbus.fault_injection` (H) | enabler | INF8: SBUSController RAM-only test verbs (D-NC8) | flash COM4 | WP11 |
| Low risk, collapsed (13) | - | `nc.sbus.cli_diag`: covered: cli_codes, sbus_live_dump_toggle; `nc.mae.restart_script_easing`: `ncdev.easing_repeat_frames` (restartScript,n,pN,o bytes on W1S1); partial today: maestro.fanout_remote; `nc.aux.bcast_out`: `ncdev.bcast_out_opt_in` (guarded serialBcast S4 out; DBG_SERIAL line; '{' never fanned); `nc.aux.rx_monitor`: blocked: `ncwire.rx_monitor_bcast_in`; `nc.aux.bcast_in`: blocked: `ncwire.rx_monitor_bcast_in`; `nc.aux.port_labels`: `nccfg.side_effects_live`, `ncmesh.wdp_port_labels`; `nc.hcr.overload_shortcut`: `ncdev.hcr_local_payload` (chan 4 gives 'bad/unsupported', no bytes); fix the stale comments; `nc.hcr.cli_test`: `ncdev.cli_hcr_test_codes` (#L20/#L21, opt:navicore_aux_tx); `nc.dfp.wcb`: `ncdev.dfp_remote`; `nc.board.l1`: covered: `navicore.l1_names_board_profile` ((should), passes on the working-tree image); `nc.led.states`: out of scope (no optical sensor; §6); `nc.led.gpio_conflict`: out of scope until Greg reads the DevKitC revision (D-NC28); `nc.cfg.reset_defaults_drift`: `nccfg.reset_defaults_ram` (USB: no re-open lines) vs `ncmesh.bridged_reset_defaults` (mesh: lines); (should) per D-NC16 | - | - |

### Mesh, bridge, management relay, OTA, WiFi/WebSocket, failure (`mesh-remote`, 88 features)

| Feature (risk) | Covered today | Proposed test(s) | Needs | WP |
|---|---|---|---|---|
| `nc.mesh.join_boot` (H) | gap | `ncboot.banner_order` ('[WCB] Joined network as device ID 20 (quantity=1)'); `ncboot.wcbs_see_reboot` | opt:navicore_reboot | WP9 |
| `nc.mesh.channel` (H) | implicit: `navicore.online` | `ncboot.banner_order` (SoftAP line names channel 1); a channel change is never made (D-NC14) | opt:navicore_reboot | WP9 |
| `nc.mesh.heartbeat_out` (H) | implicit: `navicore.online`, peers.controller_off_on | `ncmesh.w1_tracks_20` (W1 deafened with _deaf_w1 (s18:293): WCB20 OFFLINE after ~50 s, ONLINE on restore) | w1 | WP6 |
| `nc.mesh.online_tracking` (H) | partial: `navicore.online`, cli_codes (healthy mesh) | `ncmesh.online_tracking_flip`; `ncmesh.wcb_status_content` | w2 console (?MAC,3 flip, as WP5 of the audit) | WP6 |
| `nc.mesh.status_transitions` (M) | gap | `ncmesh.online_tracking_flip` ('[WCB] WCB2 . &lt;alias> OFFLINE', then 'ONLINE' on the first heartbeat) | w2 console | WP6 |
| `nc.mesh.dedup_boot_announce` (H) | gap for NaviCore (s19 covers the probe) | `ncmesh.dedup_after_w1_reboot`; `ncboot.wcbs_see_reboot` | w1; the NaviCore half opt:navicore_reboot | WP6/9 |
| `nc.mesh.etm_ack_in` (H) | gap (etm.broadcast_per_board_ack only notes it) | `ncmesh.etm_ack_sender_side` (W1 ?STATS row WCB20: Sent n = ACKd n, 0 retries, 0 failed) | w1 | WP6 |
| `nc.mesh.crc_always_on` (M) | gap | `ncmesh.crc_namespace_gates` (probe CHK=0: no ACK, not run; CHK=1: runs) | probe-mesh (probe_in_mesh, burn the 32-seq window, WCB_Client.cpp:879-898) | WP6 |
| `nc.mesh.namespace_gate` (M) | gap | `ncmesh.crc_namespace_gates` (wrong PASS: silence, no ACK) | probe-mesh | WP6 |
| `nc.mesh.ensured_degrade` (H) | partial: wcb_send_ack_reflects_refusal (a refused broadcast) | `ncmesh.online_tracking_flip` (11+ WCB_SEND to deaf W2: ok:false, MESH_STATS fail/ung rise) | w2 console | WP6 |
| `nc.wdp.advert_identity` (M) | smoke: wdp.dump_fields (vacuous without the row, s18:1172) | `ncmesh.wdp_identity_fields` (row REQUIRED: CLIENT=1, ALIAS=NaviCore, FW=PONG, HWREV 'NaviCore v2', CAPTAGS) | w1 | WP6 |
| `nc.wdp.port_labels` (M) | gap | `ncmesh.wdp_port_labels` (S=1..4 labels equal the effective routing; re-advertised within ~1 s of a USB or bridged save) | w1, guard | WP6 |
| `nc.wdp.maestro_tlv` (H) | strong: `navicore.maestro_inventory` | - | - | - |
| `nc.wdp.solicit` (M) | partial: wdp.poll_refreshes_age, `navicore.wdp` | `ncmesh.wdp_solicit_keeps_rows` (after W1's POLL NaviCore still shows W1's ALIAS and [WDPIF]) | w1 | WP6 |
| `nc.wdp.neighbour_table` (M) | partial: `navicore.wdp` (N, CLIENT, FW) | `ncmesh.wdp_neighbour_table` (each row equals that WCB's SELF row; a label with quote/backslash/comma still gives parseable WCB_STATUS) | w1, cguard W2 | WP6 |
| `nc.wdp.autojoin_learned` (M) | partial: forget_peer_safe (non-learned ids only) | `ncmesh.wdp_learn_forget` (a non-temporary probe above the floor is learned (PEER=2), then forgotten) | probe-mesh, opt:navicore_nvs | WP6 |
| `nc.wdp.new_peer_event` (M) | gap (finding: 8 s grace vs 60 s adverts) | `ncboot.new_peer_after_boot` (read 70 s of log; (should) none for boards online at boot, D-NC25) | opt:navicore_reboot | WP9 |
| `nc.tx.action_wcb`, `nc.tx.wcb_send_usb` (H) | strong: test_action_usb, wcb_send*, trigger_dispatch_trace | - | - | - |
| `nc.tx.w_route_chains` (M) | gap | `ncmesh.w_route_chains` (WCB_SEND 1 ';w2;s3&lt;t>' gives exactly one line on W2S3; an implicitly routed W2 verb in a unicast to W1 is dropped) | W2S3 | WP6 |
| `nc.tx.hcr_remote` (H) | gap | `ncdev.hcr_remote_verbs` | cguard W2 | WP7 |
| `nc.tx.mp3_remote` (H) | gap | `ncdev.mp3_remote` | guard, cguard W2 | WP7 |
| `nc.tx.dfp_remote` (M) | gap | `ncdev.dfp_remote` | guard, cguard W2 | WP7 |
| `nc.tx.wled_remote` (M) | gap | `ncdev.wled_remote` | cguard W2 | WP7 |
| `nc.tx.maestro_remote_stream` (H) | gap | `ncdev.mae_remote_stream_exact` | W1S1, W2S1 tap | WP7 |
| `nc.tx.maestro_remote_read` (M) | gap | `ncdev.mae_remote_read` (and bridged: [TERM:20][MAE:2]; checks the TROUBLESHOOTING row) | w1, w2 | WP7 |
| `nc.tx.skip_running_gate` (M) | gap | `ncengine.skip_running_fail_open` | w1 | WP4 |
| `nc.tx.serial_bcast_in` (M) | blocked (no wire into NaviCore RX) | `ncwire.rx_monitor_bcast_in` | wire:probe3 | WP14 |
| `nc.tx.seq_pull` (M) | gap | `ncmesh.seq_pull` | w1 | WP6 |
| `nc.telem.rc_hb` (M) | partial: rc_relay_window (types only) | `ncmesh.telemetry_fields_rates` (every 2 s; fw == PONG; mode == #L12; sbusFps ~ #L09) | w1 | WP6 |
| `nc.telem.rc_ch_subscription` (M) | partial: rc_relay_window | `ncmesh.telemetry_fields_rates` (ch[] == #L09 at chRateHz; 2 vs 10 applied live; NaviCore's own 15 s gate measured between 15 and 20 s) | w1, guard (chRateHz) | WP6 |
| `nc.telem.rc_trig` (M) | partial: the USB line (strong); the mesh copy noted | `ncengine.rc_trig_emit` | w1 | WP4 |
| `nc.rx.remote_cli` (M) | partial: `navicore.cli_over_mesh` (one ?version) | `ncmesh.remote_cli_order_and_drop` (multi-line order, 'Unknown command:' over the mesh, 5-burst gives at most 3 replies) | w1 | WP6 |
| `nc.rx.long_command_truncation` (M) | gap (finding) | `ncmesh.long_command_truncation` (should, D-NC26: probe MSEND 20 ';s1'+300 chars must not reach the port cut to 200) | probe-mesh | WP6 |
| `nc.rx.plain_and_targeted` (M) | strong: serial_route_dbg, plain_broadcast_both_ways, mesh_json_declined | - | - | - |
| `nc.rx.maestro_inbound` (H) | strong: `navicore.maestro_mesh_*`  | - | - | - |
| `nc.rx.trigger_setmode` (H) | strong: `navicore.trigger`, trigger_bounds_usb_vs_mesh, set_mode, `sbus.mode_sets_trigger_mode` | - | - | - |
| `nc.rx.reset_defaults_password_split` (M) | gap (finding) | `ncmesh.bridged_reset_defaults` (after ;W20,{RESET_DEFAULTS}: text runs, [TERM:20] and relay pulls die) and (should) per D-NC16/17 | w1, guard | WP6 |
| `nc.bridge.fragment_reassembly` (H) | gap | `ncmesh.fragment_reassembly_edges` (out of order, duplicate, 'of' mismatch, 5 s expiry, pool exhaustion at 4 sessions) | w1, W1S2 (payload is a TEST_ACTION ;S2HIL) | WP6 |
| `nc.bridge.set_config` (H) | gap | `ncmesh.bridged_set_config_strip` | w1, guard | WP6 |
| `nc.bridge.get_config` (H) | gap | `ncmesh.bridged_get_config` (reassembled == USB data by hash; single-flight; #L09 fps unaffected) | w1 | WP6 |
| `nc.bridge.cmdlib` (M) | gap | `ncmesh.bridged_cmdlib` (META == USB; GET stream; SET; bulk bb/bc/bd publish and hash-mismatch discard) | w1, cmdlib guard | WP6 |
| `nc.bridge.wcb_status` (M) | gap | `ncmesh.bridged_status_meta_stats` (&lt;= 185 B; sparse rows once a probe joins as 16; relay fields) | w1, probe-mesh | WP6 |
| `nc.bridge.wcb_send` (M) | partial: bridged_json_ack | `ncmesh.bridged_wcb_send_findings` (should, D-NC27: ok reflects the send; a fragmented WCB_SEND gets an ACK) | w1 | WP6 |
| `nc.bridge.test_action` (M) | partial: bridged_json_ack (single packet) | `ncmesh.fragment_reassembly_edges` (the fragmented TEST_ACTION) | w1, W1S2 | WP6 |
| `nc.mgmt.backup_version` (H) | gap | `nccfg.backup_block`; `ncmesh.remote_cli_order_and_drop` (?backup over RTERM) | - | WP1/6 |
| `nc.mgmt.wdp_dump` (M) | partial: `navicore.wdp` | `ncmesh.wdp_neighbour_table` (SELF row PEER=3, PEER flags, [WDPCFG], count, scrub) | w1 | WP6 |
| `nc.mgmt.pull` (H) | strong: `navicore.mgmt_pull`; partial: pull_over_limit (pre-F13 library) | - (pull_over_limit takes its post-F13 branch once WP2 flashes the F13 library) | WP2 image | WP2 |
| `nc.mgmt.stats_etm` (M) | gap | `ncmesh.mgmt_stats_etm_frag` ([MGMT:STATS,2] equals W2's ?STATS; ETM,CHAR once and alone) | w2 console | WP6 |
| `nc.mgmt.frag_push` (H) | gap | `ncmesh.mgmt_stats_etm_frag` (?MGMT,FRAG single and multi-part to W2; read back) | cguard W2 | WP6 |
| `nc.mgmt.rterm_relay` (H) | gap | `ncmesh.mgmt_rterm_relay` ([TERM:2] through NaviCore; STOP in finally) | w2 | WP6 |
| `nc.mgmt.raw_hook_composition` (H) | gap | `ncmesh.raw_hook_interleave` (a relay pull of W2 interleaved with relay-OTA ACK traffic, both complete) | w2 | WP6 |
| `nc.rterm.target` (M) | partial: `navicore.cli_over_mesh` | `ncmesh.remote_cli_order_and_drop` (a >160-char [WDP:..] row split at 160, nothing lost) | w1 | WP6 |
| `nc.ota.local_nondestructive` (M) | gap on NaviCore | `ncota.local_status_parse`; `ncota.local_nosession_errors` | - | WP10 |
| `nc.ota.local_full` (H) | gap | `ncota.local_full_same_image` (twice, ends on the original slot) | opt:navicore_ota_full | WP10 |
| `nc.ota.target_relay_nondestructive` (H) | partial: ota.relay_navicore_brick_guard (BEGIN only) | `ncota.relay_target_nosession` (DATA ACK ERR 0, END 'no matching active session', ABORT OK 0) | w1 | WP10 |
| `nc.ota.target_relay_full` (H) | gap | `ncota.relay_full_via_w1` (W1 relays the bench image to 20, twice) | opt:navicore_ota_relay_full | WP10 |
| `nc.ota.relay_to_wcb` (H) | gap | `ncota.navicore_as_relay_nonerasing` (BEGIN family 1 to W2 rejected [OTA:ACK,2,s,0,1]; bad CRC dropped; DATA no session); full: `ncota.relay_full_to_w2` | w2; full opt:ota_full_wcb2 | WP10 |
| `nc.ota.timeout_reaper` (M) | gap | `ncota.local_begin_abort_timeout` (BEGIN, 31 s idle, '[OTA] aborted: session timed out') | opt:navicore_ota_erase | WP10 |
| `nc.wifi.ap_bringup` (H) | gap | `ncwifi.ap_boot_lines` (SoftAP, DHCP and [WS] lines); `ncwifi.refuse_short_password` | opt:navicore_reboot, opt:navicore_wifi | WP8 |
| `nc.wifi.dhcp_no_gateway` (M) | gap | `ncwifi.pc_joins_ws_ping` (the adapter gets 192.168.4.x and no default route via .1) | opt:navicore_wifi | WP8 |
| `nc.wifi.mesh_coexist` (H) | gap | `ncwifi.mesh_coexist` (with a WS session streaming, W1 sees no missed heartbeats from 20; `navicore.online`, wcb_send, cli_over_mesh re-run) | opt:navicore_wifi | WP8 |
| `nc.ws.dispatch` (H) | gap | `ncwifi.pc_joins_ws_ping` (PONG over WS; 6 PINGs in one frame give 6 PONGs) | opt:navicore_wifi | WP8 |
| `nc.ws.console_mirror` (H) | gap | `ncwifi.ws_console_mirror` (PWM_UPDATE and [DISPATCH] reach the socket) | opt:navicore_wifi | WP8 |
| `nc.ws.multi_client` (M) | gap | `ncwifi.ws_multi_client` (interleaved half-lines per client; a 4th evicts the oldest) | opt:navicore_wifi | WP8 |
| `nc.ws.line_framing` (M) | gap | `ncwifi.ws_line_framing` (1391-char ?OTALOCAL,DATA in 512-B frames decodes: NAK, not 'base64 error') | opt:navicore_wifi | WP8 |
| `nc.ws.utf8_and_latch` (M) | gap | `ncwifi.ws_utf8_and_latch` | opt:navicore_wifi, guard (a multi-byte label) | WP8 |
| `nc.ws.ping_serialisation` (M) | gap | `ncwifi.ws_ping_soak` (5 min under PWM_UPDATE, no 1002 close) | opt:navicore_wifi (nightly only) | WP8 |
| `nc.ws.large_reply` (H) | gap | `ncwifi.ws_parity` (GET_CONFIG ~14 KB over WS equals USB by hash) | opt:navicore_wifi | WP8 |
| `nc.ws.wizard_over_ws` (H) | gap | `ncwifi.ws_wizard_surface` (?backup, ?WDP,DUMP, ?MGMT,PULL,2, relay OTA ACK over WS) | opt:navicore_wifi | WP8 |
| `nc.ws.intellex_client` (M) | gap (Intellex side is phase 3) | `ncwifi.pc_joins_ws_ping` (the discover PING contract); the concurrent-session part in phase 3 | opt:navicore_wifi | WP8 |
| `nc.fail.wcb_offline`, `nc.fail.wcb_return` (H) | gap | `ncmesh.online_tracking_flip` (W2 deaf > 50 s: OFFLINE line, online[1]=0, fail>0, #L09 fps steady; restored: ONLINE, next send lands) | w2 console | WP6 |
| `nc.fail.mesh_loss` (H) | gap | `ncmesh.mesh_loss_local_survives` (W1 and W2 deaf in turn: SBUS full rate, local ?MAE,GET answers, WCB_SEND returns fast) | w1, w2 consoles | WP6 |
| `nc.fail.navicore_reboot_seen` (M) | gap | `ncboot.wcbs_see_reboot` (W1 WDP AGE resets; ;W20,?version answered within seconds; WCB_SEND lands) | opt:navicore_reboot | WP9 |
| Low risk, collapsed (13) | - | `nc.mesh.roll_call`: `ncboot.roll_call_missing_board` (W2 deaf across the boot: 'WCB2 ... never heard from'); `nc.wdp.temporary_peer`: covered: probe_temp_peer_not_learned; the learned-turns-temporary case in `ncmesh.wdp_learn_forget`; `nc.wdp.alias_whoami`: `ncmesh.alias_whoami` (W2 with an empty ?ALIAS: at most 4 ?WHOAMI); `nc.tx.mode_report`: `ncengine.mode_report_content`; `nc.tx.stats_report`: `ncengine.stats_report_content`; `nc.telem.rc_mode`: `ncmesh.telemetry_fields_rates` (one per change, none on a repeat); `nc.rx.counters`: covered: mesh_stats_counts; the bridged RESET_MESH_STATS in `ncmesh.bridged_status_meta_stats`; `nc.rx.reboot_mesh`: `ncboot.mesh_reboot` ((should) per D-NC29: ACK, then a deferred restart); `nc.rx.json_misc`: `ncmesh.bridged_usb_only_types`; `nc.bridge.wcb_meta`: `ncmesh.bridged_status_meta_stats`; `nc.bridge.mesh_stats`: `ncmesh.bridged_status_meta_stats` (pages merge to the USB reply); `nc.mgmt.overlong_line`: `ncmesh.mgmt_stats_etm_frag` ('[mgmt] line too long' on the WP2 library); `nc.ws.capture_slot`: `ncwifi.ws_capture_slot` (low; needs the remote read of `ncdev.mae_remote_read`) | - | - |

### Config tool (config_tool/index.html) (`config-tool`, 81 features)

| Feature (risk) | Covered today | Proposed test(s) | Needs | WP |
|---|---|---|---|---|
| `nct.load.parse_and_wiring` (H) | gap (pages-deploy.yml has no check) | `nctool.static` (L0: inline blocks compile, node --check); `nctool.load_smoke` (L1: no pageerror, modals open) | L0, L1 | WP3 |
| `nct.load.cmdlib_autoload` (M) | gap | `nctool.load_smoke` (NC_COMMAND_LIBRARY boards, wcb-native hidden) | L1 | WP3 |
| `nct.conn.direct` (H) | gap | `nctool.connect_direct_sequence` (PING..., GET_CONFIG, GET_CMDLIB_META, START_MONITOR, SET_DEBUG_FLAGS; DTR/RTS false right after open) | L1; L2 `nctool.board_connect_config` | WP3/13 |
| `nct.conn.pong_epoch` (H) | gap | `nctool.pong_epoch_slow_direct` (a PONG after 3.5 s leaves viaWcbActive false) | L1 | WP3 |
| `nct.conn.any_pong_misdetect` (H) | gap (finding) | `nctool.doorway_pong_misdetect` (should, D-NC30: a relayed PONG {sys,id} is not a direct link) | L1 | WP3 |
| `nct.conn.via_wcb_hub` (H) | gap | `nctool.via_wcb_hub` (';w20,{"sys":1,"type":"PING"}'; WCB chip; no-port toast); L2 `nctool.board_via_wcb` | L1; L2 (W1's port through the pipe) | WP3/13 |
| `nct.conn.transport_flag_reset` (H) | gap | `nctool.transport_flag_reset` (button states after Via WCB, disconnect, USB) | L1 | WP3 |
| `nct.conn.disconnect` (H) | gap | `nctool.disconnect_teardown` | L1 | WP3 |
| `nct.conn.link_loss` (M) | gap | `nctool.link_loss_reconnect` (NetworkError tears down and reconnects; BreakError keeps the session) | L1 | WP3 |
| `nct.conn.keepalive` (M) | gap | `nctool.keepalive_via_wcb` (page.clock: PING every 10 s) | L1 | WP3 |
| `nct.rx.framing`, `nct.rx.markers` (M) | gap | `nctool.rx_framing_markers` | L1 | WP3 |
| `nct.rx.frag_reassembly` (H) | gap | `nctool.rx_fragments` | L1 | WP3 |
| `nct.rx.config_error` (H) | gap | `nctool.config_error_no_baseline` | L1 | WP3 |
| `nct.config.apply` (H) | gap | `nctool.static` (every rcConfigToJSON key is read by applyConfig); `nctool.apply_all_keys` | L0, L1 | WP3 |
| `nct.config.no_phantom_diff` (H) | gap | `nctool.no_phantom_diff` (bench fixture: Save sends nothing after opening every tab and modal); L2 against the real config | L1, L2 | WP3/13 |
| `nct.save.diff` (H) | gap | `nctool.unit` (the diff functions in node); `nctool.save_diff_payload` (the SET_CONFIG a Save sends) | L0, L1; L2 `nctool.board_save_one_field` | WP3/13 |
| `nct.save.ack_correlation` (H) | gap | `nctool.save_ack_correlation` (ok, NACK, timeout, stale saveId) | L1 | WP3 |
| `nct.save.reset_defaults` (M) | gap | `nctool.reset_defaults_flow` (bridged alert; direct order; toast only after CONFIG) | L1 | WP3 |
| `nct.save.refresh_overwrites_edits` (M) | gap | `nctool.refresh_overwrites_edits` (pinned, or (should) prompt per D-NC31) | L1 | WP3 |
| `nct.save.close_prompt` (M) | gap | `nctool.close_prompt` | L1 | WP3 |
| `nct.save.push_budget` (M) | gap | `nctool.push_budget_prediction` (readout vs the ';w20,{"f":' lines Save emits; >192 parts and >187 B refused) | L1 | WP3 |
| `nct.send.sendjson` (H) | gap | `nctool.unit` (_fragChunks byte-exact over a corpus); `nctool.sendjson_fragments_exact` (quotes, backslashes, CJK, surrogates, controls; each envelope &lt;= 187 UTF-8 B; no poll between parts) | L0, L1 | WP3 |
| `nct.send.sendline` (H) | gap | `nctool.sendline_chunking` (512-B chunks >= 4 ms apart; no interleave) | L1 | WP3 |
| `nct.edit.button_modal`, `nct.edit.action_roundtrip`, `nct.edit.switch_modal`, `nct.edit.knob_modal` (H) | gap (findings: skipRunning true vs 1, unknown type rewritten, knob defaults) | `nctool.noop_apply_every_editor` (should, D-NC32); `nctool.button_modal_trigger` | L1 | WP3 |
| `nct.edit.test_action` (M) | gap | `nctool.test_action_button` (TEST_ACTION minus delay/note; ok:false never shown, D-NC20); L2 `nctool.board_test_action_wire` | L1, L2 (W1S2) | WP3/13 |
| `nct.edit.command_view` (M) | gap | `nctool.command_view_limits` (maxLength 95 with the prefix; chain warning) | L1 | WP3 |
| `nct.edit.channels_tab`, `nct.edit.logical_buttons` (M) | gap | `nctool.channels_tab` (Rebuild wipes logical bands with no confirm: pinned) | L1 | WP3 |
| `nct.edit.tx_model` (M) | gap | `nctool.tx_model_switch` | L1 | WP3 |
| `nct.edit.wcb_network`, `nct.edit.wcb_profiles` (H) | gap | `nctool.wcb_network_profiles` (badge; USB save carries wcbNetwork; bridged strips; a 7th profile refused) | L1 | WP3 |
| `nct.edit.peer_event`, `nct.edit.audio_dests`, `nct.edit.wled_slots`, `nct.edit.smoothing`, `nct.edit.serial_ports`, `nct.edit.general`, `nct.edit.hidden_wifi` (M) | gap | `nctool.editors_misc` (one no-op and one real edit per pane; the audio-dest stale-field merge pinned) | L1 | WP3 |
| `nct.edit.maestro_locations` (M) | gap | `nctool.maestro_locations_xml` (Control Center XML fixture; ?MAE read lines and readout) | L1 | WP3 |
| `nct.live.pwm_update`, `nct.live.stale_resubscribe` (M) | gap | `nctool.live_monitor` (scripted frames; stale at 4 s re-sends START_MONITOR); L2 `nctool.board_live_grid` | L1; L2 (sbus moves a safe channel) | WP3/13 |
| `nct.live.rc_telemetry` (M) | gap | `nctool.live_rc_telemetry` (lit tier rows: cumulative, exclusive, long press; stale watchdog) | L1 | WP3 |
| `nct.wcb.status`, `nct.wcb.forget_peer` (M) | gap | `nctool.wcb_status_panel` (dense and sparse rows; forget flow; bounded WCB_META) | L1 | WP3 |
| `nct.clips.list`, `nct.clips.record_rename_delete` (M) | gap | `nctool.clips_list_forms`; `nctool.clips_record_rename_delete`; L2 `nctool.board_clips_list` | L1, L2 (read-only) | WP3/13 |
| `nct.clips.download_verified` (H) | gap | `nctool.clip_download_verified` (fc != count, fp change, missing events refused) | L1 | WP3 |
| `nct.clips.backup_bundle` (H) | gap | `nctool.clip_backup_bundle` | L1 | WP3 |
| `nct.clips.restore` (H) | gap (finding: mode not restored) | `nctool.clip_restore` (collision prompts, ACK retries, bridged 180-B refusal; (should) mode, D-NC33) | L1 | WP3 |
| `nct.timeline.editor` (H) | gap | `nctool.timeline_editor_save` (partial loads read-only; upload == model; close mid-save aborts) | L1 | WP3 |
| `nct.cmdlib.catalog`, `nct.cmdlib.encode_decode` (M) | gap | `nctool.unit` (encode then decode every library command); `nctool.cmdlib_catalog_codec` (groups, hidden boards, search) | L0, L1 | WP3 |
| `nct.cmdlib.seq_source` (M) | gap (CONFIG_TOOL.md:427-430 claims a test that does not exist) | `nctool.unit` (_seqValueToLines equals the Wizard's seqValueToLines, Wizard/app.js:4404, over a corpus); `nctool.seq_source_parity` (the dropdown from WCB_SEQ/SEQVAL replies) | L0, L1 | WP3 |
| `nct.cmdlib.custom_sync` (M) | gap | `nctool.cmdlib_sync` (SET_CMDLIB data intact, sys first; bulk envelopes &lt;= 187 B; hash == host FNV-1a); L2 `nctool.board_cmdlib_load` | L1, L2 | WP3/13 |
| `nct.fw.flash_update` (H) | gap | `nctool.fw_flash_mocked` (esptool-js and GitHub mocked: images, addresses, otadata reset, reconnect); L3 `nctool.webserial_flash_same_image` | L1; L3 attended | WP3/13 |
| `nct.fw.wipe_misleading` (H) | gap (finding) | `nctool.fw_wipe_text` (should, D-NC34: the text must not promise the config is erased) | L1 | WP3 |
| `nct.fw.ota_usb` (H) | gap | `nctool.ota_usb_state_machine` (coalesced markers, lost ACK rewind, END,OK never a failure); L2 `nctool.board_ota_usb_same_image` | L1; L2 opt:navicore_ota_full | WP3/13 |
| `nct.fw.ota_wcb` (H) | gap | `nctool.ota_wcb_state_machine` (192-B chunks, window 8, session/src filter, END offset 0, ABORT) | L1 | WP3 |
| `nct.fw.button_gating` (H) | gap | `nctool.transport_flag_reset` | L1 | WP3 |
| `nct.calib.wizard` (M) | gap | `nctool.calibration_wizard` (CALIB on/off; scripted PWM_UPDATE to thresholds and channels) | L1 | WP3 |
| `nct.io.export_json`, `nct.io.import` (H) | gap | `nctool.export_import_json` (export, reset, import: stable-equal; Wizard export and '{}' rejected; old file wipes the full-replace branches: pinned) | L1 | WP3 |
| `nct.io.csv` (M) | gap | `nctool.unit` (parseCsv on fixtures); `nctool.csv_roundtrip` | L0, L1 | WP3 |
| `nct.io.cloud_backup` (M) | gap | `nctool.cloud_backup_mocked` (page.route on the Worker's /cfg/ routes; never the live KV) | L1 | WP3 |
| `nct.hub.shared_port` (H) | gap | `nctool.shared_hub_two_pages` (leader/follower, relay, failover, DTR never true; Wizard + tool on one origin) | L1 | WP3 |
| `nct.hub.multi_tab_save` (M) | gap (plausible) | `nctool.multi_tab_save` (should, D-NC35: a per-tab random saveId base) | L1 | WP3 |
| `nct.xrepo.intellex_contract` (H) | gap | `nctool.static` (byte identity with Intellex/src/webui; the shim's globals and ids exist); `nctool.intellex_contract` (page loads under a shim-like navigator.serial) | L0, L1 | WP3 |
| Low risk, collapsed (10) | - | `nct.load.redirect`: `nctool.load_smoke` (GET /NaviCore/ lands on config_tool/); `nct.conn.unload_release`: `nctool.link_loss_reconnect` (pagehide: setSignals false then close recorded); `nct.edit.config_tabs`: `nctool.no_phantom_diff` (rcConfigLastTab restore; stale name falls back to channels); `nct.wcb.mesh_stats`: `nctool.wcb_status_panel` (paged merge; a dropped page keeps the last snapshot); `nct.term.panes`: `nctool.rx_framing_markers` (panes, caps; the CONFIG echo is never dumped: D-NC5); `nct.term.debug_chips`: `nctool.connect_direct_sequence` (flags value per chip set); `nct.fw.check_latest`: `nctool.fw_flash_mocked` (api.github.com mocked); `nct.io.cheat_sheet`: `nctool.editors_misc` (popup HTML lists each mapped control); `nct.misc.ui_prefs`: `nctool.editors_misc` (SBUS/us toggle round-trips); `nct.docs.drift`: fix CONFIG_TOOL.md with the first NaviCore push (D-NC36) | - | - |

## 3. Infrastructure first

Nine pieces, in the order they unblock tests. Effort is agent-hours of writing plus a first bench check.

| # | Piece | Unblocks | Effort |
|---|---|---|---|
| INF1 | `hil/navicore.py` grows into the shared driver | every NaviCore suite | 5 h |
| INF2 | `hil/sbus.py`, the SBUS controller driver and frame codec | NC-WP5, NC-WP11, NC-WP14 | 3 h |
| INF3 | `nc_guard`: NaviCore snapshot/restore, plus redaction | every test that writes NaviCore | 6 h |
| INF4 | `hil/ncflash.py`: build, flash by OTA, recovery | NC-WP2, NC-WP10, every firmware fix | 5 h |
| INF5 | `hil/ncws.py` and `hil/wlan.py` | NC-WP8 | 3 h |
| INF6 | `hil/ncmesh.py`: bridged JSON, fragment builder, deafening | NC-WP4, NC-WP6 | 3 h |
| INF7 | The config-tool rig in `tests/wizard` | NC-WP3, NC-WP13 | 10 h |
| INF8 | SBUSController RAM-only test verbs (D-NC8) | NC-WP11 | 4 h |
| INF9 | NaviCore test hooks (D-NC7) | the hook rows of NC-WP1 and NC-WP7 | 5 h |

### INF1 — `hil/navicore.py`, the shared driver

> **Status 2026-09-28: bench-verified** (every NaviCore test of full run `20260928-005805` and each NaviCore run since goes through it). s21's twelve helpers are driver methods (`config`, `debug`,
> `cli` for both `_flushed` and `_cli`, `mae_get`, `local_slots`, `undriven_channel`, `ack_line` for `_ack`, `mode`,
> `rec_info`, `clips`, `sbus_dump` for `_l09`), with `usable_slot`, `lines` and `rc_events` beside them; s08's and s22's
> GET_CONFIG reads use `config()`. Every method below exists; `ota_status` and `ota_stream` are functions of
> `hil/ncflash.py` that take the driver (INF4). `selftest.py` feeds each parser firmware-format lines. The moved
> helpers send exactly what they did. Needs a first bench check: the writes
> (`set_config`, `reset_defaults`, `set_cmdlib`: after INF3), `rec_download`/`rec_upload`, `reboot`/`wait_boot`/
> `hard_reset` (no run has seen NaviCore boot), `monitor`, `seq`/`seqval`, `version_surfaces`. Read in the source
> while writing it:
> - Only SET_CONFIG, SET_CMDLIB, TEST_ACTION, FORGET_PEER and RESET_MESH_STATS name themselves in their ACK; the rest
>   answer a bare `{"type":"ACK","ok":...}` (NaviCore.ino:3914-4217), so `of` is checked only for those. `ack()` returns
>   the dict, and `ack_line()` the exact line the existing tests compare byte for byte.
> - The config tool paces every line over 512 bytes, not only those over the 8 KB ring (`sendLine`, index.html:5313-
>   5321); `send_paced` does the same.
> - setup() ends `[NaviCore] Firmware <v> — setup complete.` in three writes (:4916-4918). A restart re-enumerates the
>   USB port, so the banner can be lost; `wait_boot` also accepts a PONG after the port reopens, and fails at once on
>   the ROM's `waiting for download`.
> - The USB rc_trig and PWM_UPDATE frames are dropped, not queued, when the TX ring is short (:2233-2266, :3150-3158),
>   and STOP_MONITOR also ends a calibration (:3965-3970).
> - `_clipPath` keeps only `[A-Za-z0-9_-]` and 32 characters of a clip name (navicore_record.h:522-533), so another
>   name addresses a different clip: the driver refuses it. BEGIN and END also echo `nm`, the clip in the buffer,
>   checked like `fp`.
> - navicore_record.h's protocol comment (:695-697) still documents `?REC,EDITEV,<json>` answered `[CLIPUL:ACK,<count>]`;
>   the code takes `<index>,<json>` and ACKs the index (NaviCore.ino:3575-3586). NaviCore doc drift, not fixed here.
> - The `[MAE:n]` markers end in a newline (NaviCore.ino:741-756); the `#L12` after a `?MAE` query is for the held-line
>   behaviour, not a missing line end (s21's docstring said otherwise and is corrected).

Today it has six methods in 57 lines: `json_cmd` (:11-14), `ping` (:16-18), `wcb_status`/`online_ids` (:20-26),
`wdp_dump` (:28-37), `sbus_state` (:39-54, which lacks the `#L12` flush that s21's own `_l09` adds) and
`set_debug_flags` (:56-57). Every suite re-implements the rest; s21 alone has `_config` (:40), `_debug` (:51),
`_flushed` (:59), `_mae_get` (:68), `_local_slots` (:77), `_undriven_channel` (:91), `_ack` (:332), `_mode` (:646),
`_rec_info` (:954), `_clips` (:961), `_cli` (:1021) and `_l09` (:1192). They move here, and s21 imports them. New
methods, each returning parsed data and raising `AssertionError` with redacted text:

```
# transport
send_paced(line, chunk=512, gap_s=0.004)        # any line over the 8 KB USB RX ring (NaviCore.ino:4500)
cli(cmd, until=None, flush=True)                # a CLI line, with the '#L12' flush
ack(obj, of=None, timeout=3.0) -> dict          # the {"type":"ACK"...} reply, 'of' checked when given
# config (INF3 builds the guard on these)
config(raw=False) -> dict | str                 # GET_CONFIG 'data'; raw=True gives the exact text for byte compares
set_config(data, save_id=None) -> dict          # paced; the ACK with the saveId echo (NaviCore.ino:3917-3958)
reset_defaults() -> dict                        # NaviCore.ino:3989-3992
cmdlib() -> bytes; cmdlib_meta() -> (size, hash); set_cmdlib(raw) -> dict    # NaviCore.ino:3861-3915
fnv1a32(data) -> int                            # static: rcCmdlibHash (rc_config.h:2159)
# live state
monitor(seconds) -> [dict]                      # START_MONITOR ... STOP_MONITOR, PWM_UPDATE frames (:3960-3970)
calib(on); debug(flags) (context manager); mode() -> int (#L12)
trigger(mode, btn, tap) -> (ack, rc_trig); test_action(action) -> ack; wcb_send(target, cmd) -> ack
rc_events(since, kind="rc_trig") -> [(t, obj)]
# Maestro (NaviCore.ino:3396-3439)
mae_get(slot, ch); mae_moving(slot); mae_err(slot); mae_set(slot, ch, pos); mae_free(slot, ch)
# record/replay (NaviCore.ino:3444-3596)
rec_info(); rec_ls() -> [(name, bytes, dur, n)]; rec_rm(name)
rec_download(name) -> events                    # ranged EDITLOAD with ,B; refuses on fc != count or an fp change
rec_upload(name, events)                        # EDITBEGIN / EDITEV,i (per-index ACK, 3 retries) / EDITEND
# mesh views
wdp_dump() (exists) + self_row(), wdpcfg(); mesh_stats() (pages merged); backup() (?EPASS kept as a hash only)
seq(wcb); seqval(wcb, key); version_surfaces(w1) -> {surface: version}
# lifecycle
reboot(how="json" | "l02") -> boot lines; wait_boot(timeout=20)   # to '— setup complete.' (NaviCore.ino:4918), then PING
hard_reset()                                    # RTS=1/DTR=0, then RTS=0 and a DTR write (HIL_TESTING.md §2)
ota_status(nc) -> dict; ota_stream(nc, data)   # INF4: functions of hil/ncflash.py
```

`selftest.py` gains a no-hardware case per parser, fed with captured lines: PWM_UPDATE, `[MAE:n]`, `[CLIPITEM]`,
MESH_STATS pages, the boot banner, FNV-1a against a known vector, and INF6's fragment builder.

### INF2 — `hil/sbus.py`

> **Status 2026-09-28: bench-verified** (`sbus.*` in full run `20260928-005805` and in `20260928-123843`; its waits now ping every second, D29). `SbusCtl` replaces the controller JSON in s11, s21 and
> `hil/resume.py`, and in gui.py's Devices *Check*, a fourth copy (`job_check_device`) the list below missed. The
> suites send the same bytes; `cfg()` now hashes `wifiNets` in session.log, which s11 and s21 logged in clear.
> `safe_channels`, `band` and `matrix_button` moved too. `encode`/`decode` pass `selftest.py` against a real frame
> NaviCore dumped with `#L13` in run 20260922-120804, which decodes to the `#L09` values printed around it. The
> controller's default is SBUS-24: 36-byte frames, 24 channels, flags at byte 34 and the footer at 35
> (SBUSController.ino:122-129, :757-772); a 25-byte frame is the 16-channel variant. The INF8 verbs wait for INF8.

The controller's JSON is duplicated in `s11_sbus.py:17-33`, `s21:1152-1207` and `hil/resume.py:568-641`. One class,
`SbusCtl(dev)`: `ping(timeout=60)` (the controller may reset when its port opens), `cfg()` (the getcfg line carries
its WiFi credentials: parsed in memory, never noted), `bootlog(tries)`, `axes(lx, ly, rx, ry)`, `switch(i, pos)`,
`slider(i, pct)`, `trim(i, d, pressed)`, `button(i, pressed)`, `lua(i, pressed)`, `center_all()` (the resume
clean-up) and `reset_rts()` (the pulse `sbus.signal_loss_controller_reset` uses, s21:1510-1535). Once INF8 exists:
`flags(v)`, `stream(on)`, `sbus16(on)`, `glitch(kind, n)`, `channel(c, v)`. Also moved from s21: `safe_channels`,
`band` and `matrix_button` (:1210-1260). A frame codec, `encode(channels, flags, n=24)` and `decode(data)`, is
unit-tested in `selftest.py` now and used when a probe taps SBUS OUT (NC-WP14).

### INF3 — `nc_guard`: NaviCore snapshot and restore, and redaction

> **Status 2026-09-28: built and bench-verified** (`nccfg.guard_selftest` in `20260928-064402` and `-070951`: the diff path, byte-identical, no credential in session.log). `hil/nc_guard.py` holds the guard (`nc_guard`, `NcGuard`) and its
> parts: `take_snapshot`, `restore_config` (the ladder), `restore_state`, `restore`, `persist`, `load_snapshot` and
> `reconcile`. `hil/checkpoint.py` gains the JSON password patterns in `_SECRET_TEXT`, `redacted_diff`, `SECRET_KEY`
> and `Checkpoint.set_navicore_ref`. In `hil/runner.py`, `Bench.log` filters the device kinds in `REDACT_KINDS`
> (navicore, sbus), and `bench.ckpt` holds the running checkpoint. `hil/resume.py` gains `check_navicore`, the second
> half of step 7. `suites/s40_navicore_config.py` holds `nccfg.guard_selftest`. `selftest.py` has five cases against
> `FakeNaviBoard`, a fake NaviCore whose GET_CONFIG and SET_CONFIG follow the firmware's sparse printing and merge. A
> mutation check backs them: 14 deliberate breaks of the guard, the filter, the persistence and the resume, each
> caught. The first bench check is `python tests/hil/run.py nccfg.guard_selftest`. Read in the source while building
> it, and where the code differs from the plan below:
> - The three `rc_config.h` ranges are right (:1580-1603, :1708-1721, :1854-1864, NaviCore working tree), and so is
>   the trap. The printer leaves out an empty mapping (:1262), a slot with no channels (:1340-1341) and empty labels
>   (:1419-1426), so a re-sent snapshot cannot undo them. RESET_DEFAULTS empties all three (:869, :927, :944) and
>   reloads the compile-time mesh password (:957), in RAM only (`NaviCore.ino:3989-3992`).
> - NaviCore's two-advert learn is at `WCB_Client.cpp:2166-2168` (WCBClient working tree). The plan's :1688-1720 is
>   `_addLearnedPeer`, which persists the peer at once (:1717-1718).
> - `?WDP,DUMP` prints only the neighbours NaviCore hears now (`WCB_Mgmt.h:232-233`), so "rows with PEER=2" misses a
>   learned peer that is silent. The guard also records `[WDPCFG:...,PEERS=n]`, which counts the floor plus every
>   learned peer (:231-232), and treats a fall in it as a loss.
> - A peer NaviCore learns during a test is reported (`NAVICORE STATE NOT RESTORED`), not forgotten: FORGET_PEER is
>   itself an NVS write. The plan says nothing about this case.
> - SET_DEBUG_FLAGS 0 and STOP_MONITOR go first, before the config ladder rather than after the peers, to quiet
>   NaviCore's console before the 14 KB transfers.
> - `_SECRET_TEXT`'s JSON pattern takes any key ending in `password`, not only `password` and `wifiPassword`, and an
>   escaped copy inside a JSON string. It leaves an empty value alone and hashes the decoded string, so the mesh
>   password hashes alike in GET_CONFIG and in a WCB's `?EPASS`. `_SECRET_TEXT` is no longer at `checkpoint.py:397-406`.
> - The snapshot file and the checkpoint record both carry a sequence number. A GUI closed with *No* freezes the
>   checkpoint while the test runs on, so the file can be the newer of the two; `reconcile` then takes the file's state.
> - The week-start copy (D-NC3) is written by the first guard that runs, and never overwritten.
> - Not done here: the one-off scrub of `results/20260922-095852/report.md`, which lives in the bench checkout's
>   gitignored `results/`. The bridge, WebSocket and Playwright paths (INF5, INF7) do not exist yet, and will need the
>   same `redact_text` filter when they do.

**Snapshot:** GET_CONFIG `data` (text and dict); GET_CMDLIB bytes when GET_CMDLIB_META reports a size; the
`?REC,LS` names; the learned peers (`?WDP,DUMP` rows with PEER=2); the mode (`#L12`).

**Restore**, the trap recorded in the map as `nc.cfg.snapshot_restore_trap` (`rc_config.h:1580-1603`,
`:1708-1721`, `:1854-1864`, per the map): re-sending a snapshot does not undo a mapping key added since, channels
given to a Maestro slot that had none, or serialLabels set when there were none, because absent keys are left alone.
So:

1. If GET_CONFIG already equals the snapshot byte for byte, do nothing (every SET_CONFIG rewrites ~14 KB of LittleFS).
2. Otherwise send the snapshot **plus explicit clears**: `{}` for each mapping key that is new, `channels: []` for
   each slot whose snapshot had none, `serialLabels: {}` when the snapshot had none. Verify: the `data` text is
   byte-identical.
3. If it is not, RESET_DEFAULTS and then SET_CONFIG(snapshot): the defaults empty all three. Verify again.
4. Still different: fail `NAVICORE CONFIG NOT RESTORED — <key paths>`, with any secret value shown only as
   `<redacted:sha12>`, and note it for the resume check.
5. Then SET_CMDLIB(snapshot bytes) if the hash differs; `?REC,RM` every `HIL*` clip the snapshot lacked; re-learn a
   lost learned peer with two `?WDP,POLL` from W1 (NaviCore learns after two adverts, `WCB_Client.cpp:1688-1720`
   per the map), else report it; `SET_DEBUG_FLAGS 0`, `STOP_MONITOR`, `PING` (clears CALIB,
   `NaviCore.ino:3835-3838`); a mesh `SET_MODE` back to the snapshot's mode.

The diff path comes first because RESET_DEFAULTS reloads the compile-time mesh password into RAM, which cuts the
raw-packet paths (RTERM, OTA, WcbMgmt) off from the ETM stack until the SET_CONFIG lands
(`nc.rx.reset_defaults_password_split`). The diff path never touches the password.

**Persistence (D-NC3):** the exact snapshot is written to `results/<run>/navicore_snapshot.json` (gitignored, like
`session.log`, which already carries the CONFIG line) and a redacted hash goes into the checkpoint. The resume then
checks NaviCore too (today it does not, `docs/HIL_TESTING.md:774-775`) and restores a run killed mid-test.

**Redaction (D-NC5):**

- `_SECRET_TEXT` (`hil/checkpoint.py:397-406`) gains `("(?:password|wifiPassword)"\s*:\s*")(…)(")`; the controller's
  `"wifiNets"` pattern is already there.
- A per-device filter in `Bench.log` (`hil/runner.py:239-246`), which every serial line passes through
  (`serialdev.py:148-154`), hashes those JSON fields and the `?EPASS` value on NaviCore and SBUS lines, in both
  directions. The harness itself sends the password on every restore, so without this the guard would write it to
  `session.log` again and again.
- `redacted_diff(a, b)` for failure messages; bridge, WebSocket and Playwright-side notes go through the same filter.
- One file is already dirty: `results/20260922-095852/report.md` holds an unredacted CONFIG line in a failure tail
  (gitignored). A one-off scrub.

`nccfg.guard_selftest` proves all of it on the bench before any other test writes: add mapping 136, channels on
slot 3 and a serialLabel; restore; byte-identical, and nothing secret in `session.log`.

### INF4 — `hil/ncflash.py`: build, flash, recover

> **Status 2026-09-28: built and bench-verified.** The first flash (07:09) re-wrote the running image over `?OTALOCAL` from `app0` into `app1`: 1143 chunks in 79 s, no NAK, no resync, two nudges, END OK, PONG `v0.2.0_102105QSEP26` from the new slot; the reset rung was proven just before it (a USB chip reset, a flash boot, PONG at 2.7 s), and the NaviCore smoke tests and `nccfg.guard_selftest` passed afterwards (`20260928-070951`). The esptool rungs were then proven on the healthy board by running the real `recover(allow_esptool=True)` with rungs 1 and 2 stubbed to fail: rung 3 found no download mode, rung 4 wrote the known-good image into `app0` and `boot_app0.bin` into otadata, and NaviCore came back on `app0` with the same version; the smoke tests passed again. Every rung of the ladder is proven before a new image is flashed. `hil/ncflash.py` holds `build`,
> `check_image`, `ota_status`, `ota_begin`, `ota_stream`, `ota_abort`, `flash`, `recover` and `record_flash`, and a
> command line (`python -m hil.ncflash build|check|libs|status|reset|flash|recover`, run from `tests/hil`);
> `docs/HIL_TESTING.md` §5 says how a test or a session uses them. `selftest.py` has seven cases: synthetic ESP32-S3
> images, a scripted arduino-cli, a fake NaviCore that speaks `?OTALOCAL` (a damaged line, a lost or held ACK, the
> idle reaper, a chunk written short, END's verify, the restart, the old slot, no return) and a scripted esptool.
> `build("inf4")` compiled NaviCore's working tree (`dda37f6` plus 5 uncommitted files) into
> `results/builds/navicore-inf4` in 126 s at below-normal priority: 1,170,256 B, 59.5 % of the 0x1E0000 slot, ELF
> SHA-256 `c6415851db1ef241...`, and arduino-cli reports WCB_Client 1.17.1 and WcbCmd 0.9.1 taken from the sketchbook
> copies the pre-check compared. The image on the board (`results/builds/navicore`, 1,170,096 B, ELF `5c31d8b4...`)
> passes `check_image` too. Read in the source while building it, and where the code differs from the plan below:
> - A DATA line whose base64 does not decode gets no marker at all (`navicore_ota.h:278-281`), and a NAK's cursor
>   reads 0 once the session is gone (`:119`, `:283-284`), so a NAK means "rewind" only when it names the chunk's own
>   offset. The sender resends then, and settles everything else with `?OTALOCAL,STATUS`, whose
>   `Session: ACTIVE id=1 <written> / <size> B` is the cursor; a cursor inside the chunk means a damaged line was
>   written, and the session is aborted. One chunk is in flight. The config tool keeps eight (`otaUpdateOverUsb`,
>   `config_tool/index.html:17412`) on the same protocol.
> - There is no SHA line to match yet. `otaPrintStatus` prints Chip, Firmware, Running, Next and Session only
>   (`navicore_ota.h:226-238`), and nothing in NaviCore prints `esp_app_get_elf_sha256` (INF9 a). `flash` checks an
>   `App SHA256:` line when the board prints one, in STATUS or the boot banner. Until then the proof is the verified
>   END plus STATUS showing the other slot running; the version string cannot tell two images of one commit apart.
> - The image's app descriptor at 0x20 carries the Arduino library builder's version (`45c1b25`, project
>   `arduino-lib-builder`), not NaviCore's, so "the version string" is FW_VERSION found in the image, exactly once.
> - Recovery rung 3 is one esptool connection, `write-flash 0x10000 <known good> 0xe000 boot_app0.bin` with
>   `--after watchdog-reset`, instead of `--after hard-reset` and a separate `erase-region 0xe000 0x2000`.
>   `boot_app0.bin` is what every Arduino upload writes at 0xe000 (`platform.txt:180`, `:297`) and selects app0 as the
>   erase would, and one connection needs no second entry into download mode. `watchdog-reset`, because on these
>   native-USB S3 devkits a DTR/RTS reset after esptool can land back in download mode, which is why Greg's flasher
>   leaves every S3 flash that way (`ESP-Flasher-Companion/src/esp_flasher_companion.py:1005-1008`). D-NC4's "otadata
>   erase" is this write.
> - A rung before it, still writing nothing: esptool `--before no-reset --after watchdog-reset chip-id` connects only
>   to a chip already in ROM download mode, and leaves it by the RTC watchdog (the flasher's `_exit_download_mode`,
>   `:918-953`). Both esptool rungs need `recover(..., allow_esptool=True)`; without it the ladder stops after the
>   USB-Serial/JTAG reset and names both commands.
> - The library pre-check: `BUILD_AND_RELEASE.md` §3 covers `WCB_Client`'s `src/` only; the check compares `src/` and
>   `library.properties` of both libraries. Today `WCB_Client` equals the WCBClient working tree (1.17.1, `188bd4f`
>   plus 7 uncommitted files), and five of `WcbCmd`'s `src/` files differ from the repo (0.9.1, `0ab4af5`) only in
>   line endings (CRLF in the sketchbook), which compiles the same.
> - `--build-path` isolates the sketch, but arduino-cli's core cache (`build_cache.path`, `%LOCALAPPDATA%/arduino`) is
>   shared, so `build` holds a lock (`results/builds/.ncflash-build.lock`) and runs at below-normal priority, which the
>   compiler processes inherit.
> - NaviCore doc drift, not fixed here: `navicore_ota.h:9-11` says NaviCore builds with `PartitionScheme=min_spiffs`;
>   it builds with `custom` (`partitions.csv`, NaviCore `CLAUDE.md:113`). The config tool's OTA comment says the
>   board's serial RX buffer is 4 KB (`config_tool/index.html:17488`); it is 8 KB (`NaviCore.ino:4500`), the drift
>   D-NC36 found in PROTOCOLS.md.
> - FLASHED.md's rows go into a table of their own at the end of the file, "NaviCore flashes (hil/ncflash.py)", so
>   the hand-kept table above it is never rewritten; a row also carries the NaviCore commit and dirty flag, how the
>   image was written and the result.

**Build**, into a private build folder (never the IDE's cache):

```
arduino-cli compile \
  --fqbn "esp32:esp32:esp32s3:USBMode=hwcdc,CDCOnBoot=cdc,PartitionScheme=custom,FlashSize=16M,PSRAM=opi" \
  --build-path tests/hil/results/builds/navicore-<tag> \
  --build-property "compiler.cpp.extra_flags=-DNAVICORE_HIL_HOOKS=1" \
  C:/Users/ghulette/Documents/GitHub/NaviCore/NaviCore.ino
```

The FQBN is NaviCore's (`NaviCore/CLAUDE.md`; `PSRAM=opi` is load-bearing); `compiler.cpp.extra_flags` is in the
esp32 3.3.4 recipe (`platform.txt:70`, `:152`), and a build without the hooks simply omits that line. Pre-checks:
the sketchbook's `WCB_Client` equals `WCBClient/src` (checked today: identical) and its `WcbCmd` equals the WcbCmd
repo (`BUILD_AND_RELEASE.md` §3).

**Image check.** Magic 0xE9; the version string; size under the 0x1E0000 slot (`partitions.csv`); the ELF SHA-256
that elf2image patches at image offset 0xB0 (`--elf-sha256-offset 0xb0`, `platform.txt:167`), which the board will
print once INF9(a) lands.

**Flash = `?OTALOCAL` over the harness's COM5 handle** (`navicore_ota.h:244-305`): `?OTALOCAL,STATUS` (session
idle; note Next); `BEGIN,<size>,1` (`[OTA:BEGIN,START]` before the blocking erase, then `[OTA:BEGIN,OK,0]`);
DATA lines of 1024 decoded bytes (`OTA_LOCAL_MAX_CHUNK`, `:106`), each answered `[OTA:ACK,<off>]`, or
`[OTA:NAK,<cursor>]` to rewind; `END` gives `[OTA:END,OK]` and a restart 2 s later. Then wait for the port to
re-enumerate, the banner and PING; STATUS must show the old Next as Running, and the SHA line must match. The image
is SHA-verified by `esp_ota_end` before the boot pointer moves, and any failure or 30 s idle leaves the running app
(`navicore_ota.h:1-11`, `:104`). No ROM download mode, no DTR/RTS dance, the bootloader untouched; about 1150 ACK
round trips for the 1.17 MB image. It is the same path the config tool's "Update over USB" drives.

**Recovery ladder**, each step proven before it is needed (`ncota.recovery_hard_reset`, NC-WP2):

1. PING.
2. `hard_reset()`: the USB-Serial/JTAG chip reset (RTS=1 with DTR=0, then a DTR write so Windows' usbser.sys sends
   the line state). The SBUS reset test has used it on the controller, the same S3, in every opt-in run since
   2026-09-24 and the controller booted its app each time.
3. If NaviCore still does not answer, or prints the ROM's download-mode banner: esptool 5.1.0 from the core's tools,
   `--chip esp32s3 -p COM5 --before usb-reset --after hard-reset write-flash 0x10000 <known-good NaviCore.ino.bin>`,
   then `erase-region 0xe000 0x2000` (otadata, so the bootloader takes app0). Never write 0x0 (the bootloader;
   `arduino-cli upload` would replace the custom short-WDT one with the stock one, FLASHED.md, map gap 13) or 0x8000
   unless the partition table is gone. Known good: `results/builds/navicore`, the image on the board now.

Every flash appends a row to `results/builds/FLASHED.md` (folder, ELF SHA, what is in it).

### INF5 — `hil/ncws.py` and `hil/wlan.py`

`_netsh`, `_wlan_interfaces`, `_internet_adapter`, `_pick_adapter` and `_profile_xml` move out of `s28_wifi.py`
(:187-266) into `hil/wlan.py`, so s28 and the NaviCore suite share one tested copy. `NcWs(ip)` wraps `WsClient`
(`hil/ws.py:51`) with the SerialDevice-style `mark`/`expect`/`since` over split lines, so the INF1 driver runs over
either transport: `NaviCore(dev)` or `NaviCore(NcWs(ip))`. The temporary Windows profile is `HIL-<ssid>` on the spare
adapter only, and is deleted after; the PC keeps its internet on the other adapter.

### INF6 — `hil/ncmesh.py`

> **Status 2026-09-28: built and bench-verified** through its users in NC-WP1 (`20260928-123843`). `hil/ncmesh.py` holds `bridged` (returns `Reply(match, lines, sys)`:
> the first W1 line matching the pattern or None, every W1 line, the parsed `{"sys":1` ones; silence is returned, not
> raised), `fragments` with `chunks`, `envelope` and `esc_bytes` (the tool's `_fragChunks` and envelopes, code point for
> code point), `pace_s` and `send_fragments` (the tool's pacing, any order, repeats), `reassemble` (the tool's receive
> side, for NaviCore's fragmented replies on W1), `deaf`, `cmd_seq_dup` (a port of NaviCore's duplicate window),
> `burn_window` and `probe_peer`. `selftest.py` has four cases: the chunker against cuts recorded from the real
> `_fragChunks` run in node on six payloads (quotes, backslashes, control characters, 2- to 4-byte UTF-8), the byte
> limits, escaping, envelope shape, refusals and pacing; `bridged` and `reassemble` against a scripted W1; the burn
> against the window's port for every earlier-session high up to 66, dense and sparse; `deaf` (restored on a failure,
> an abort, a refused flip) and `probe_peer` against fakes. Read in the source, and where the code differs from the plan
> below:
> - 33 no-ops do not clear NaviCore's window in general. It keeps the highest number H it heard from a sender and the
>   32 below it (WCBClient `WCB_Client.cpp:879-898`, applied at `:2891-2895`); a probe restarts at 1 on every join, so
>   after 33 sends a later command is still dropped when an earlier session's H was 34-65. `burn_window` watches which
>   burn commands NaviCore printed (plain text under DBG_MAESTRO, `NaviCore.ino:3112-3113`), always sends at least 66,
>   and goes 34 past the last one dropped (H itself is always dropped when reached). A high just above the burn is still
>   unseen; its docstring says when.
> - With W1's quantity the probe could not unicast NaviCore at all: WCB_Client transmits only to a registered peer
>   (`WCB_Client.cpp:2349-2354`), registers boards 1..quantity at `begin()` (`:1643-1652`), and the probe sets no special
>   peer (`wcb_probe/probe_main.cpp:1026-1031`); it would learn NaviCore after two adverts and keep it in its NVS for
>   every later session. `probe_peer` joins with quantity 20.
> - A current WCB_Client clears the window on the sender's boot announce (`:2744-2769`) and a client sends three at
>   join (`:1819-1833`), but both are in WCBClient's uncommitted working tree, so neither end of the bench can be assumed
>   to carry them.
> - A NaviCore reply too long for one packet reaches W1 as fragment envelopes with no `"sys"` (`rc_telemetry.h:556-595`),
>   so `bridged` keeps every W1 line and `reassemble` joins the envelopes.
> - `_fragChunks` drops a carry its last flush could not place (`index.html:5561-5593`); its size estimate equals
>   JSON.stringify byte for byte for any text without a lone surrogate, so the carry never forms, and `fragments`
>   refuses a lone surrogate.
> - `deaf` skips a board with no USB console of its own: a `?MAC,3` flip sent over the mesh would cut off the way back.
> - `probe_peer` also skips while NaviCore has serialBcast out on for an aux port: it writes every unprefixed mesh
>   command out such a port, a unicast included (`NaviCore.ino:3100-3101`), so the burn would put 66 or more lines on
>   whatever is wired there. A test that needs the flag sets it after the burn.

- `bridged(w1, obj, pattern)`: sends `;W20,{json}` and collects W1's `{"sys":1` lines in its relay window.
- `fragments(payload, sid)`: mirrors the tool's `_fragChunks` (`index.html:5556`): envelopes
  `{"f":i,"of":n,"sid":s,"s":"..."}`, each at most 187 escaped UTF-8 bytes (NaviCore `CLAUDE.md` rule 2), paced at
  least 100 ms apart. Used to drive the bridge's reassembly edge cases by hand.
- `deaf(bench, n)`: flips `?MAC,3` on that board's own console and always restores it (the s18:293 pattern).
- `probe_peer(bench, id, **mesh)`: `probe_in_mesh` plus the 33-message anti-replay burn.

### INF7 — the config-tool rig

> **Status 2026-09-28: built and bench-verified.** L0 and L1 run with no bench (83 expected headless, 15 of them `(should)`), and L2's `nctool.board_connect_config` passed on the bench (`20260928-134028`): the tool connected through the harness's own COM handle, applied the real config, and a Save right after sent nothing.
> `docs/HIL_TESTING.md` §8 ("The NaviCore config tool") says how the rig works and how to run it. In `tests/wizard`:
> `serve.js`'s `/NaviCore/` alias (`lib/navicore/paths.js` finds the sibling repo by walking up, so a worktree works;
> `NAVICORE_REPO` overrides); `lib/navicore/shim.js` (the fake `navigator.serial` and `FakeSerial`, its Node side),
> `emulator.js` (direct, via-WCB and doorway modes), `model.js` (`rcConfigLoadDefaults`/`FromJSON`/`ToJSON` in JS, with
> ArduinoJson 7's type-strict `|`), `pipe.js` (the bridge-backed port), `fixtures.js` and `tool.js` (page helpers);
> `unit/navicore/` (`extract.js`, `static.test.js`, `unit.test.js`, `cmdlib.js`, and `rig.test.js`, which holds the
> model and the emulator to the firmware's rules); `fixtures/navicore/config.bench.json`; `tools/make_nc_fixture.js`
> and `tools/scrub_nc_config.py`. In `tests/hil`: `hil/bridge.py`'s `/serial/mark|read|write|signals` and `/sbus`,
> `hil/wizard.py`'s `run_wizard_test(..., pipe=True)`, the NaviCore PING in `_reacquire` and `run_unit_tests(files=)`,
> the suite `suites/s49_navicore_tool.py`, and `selftest.py`'s `t_nctool_pipe_bridge`. Where the build differs from the
> plan below, and why:
> - **Origin 8779, not 8778.** The specs never touch the Wizard's 8778 origin and its Web Serial grants (the plan's
>   reason for sharing it). `playwright.config.js` starts a second `serve.js` on 8779 serving the same tree, so the
>   shared-hub spec still has the tool and the Wizard on one origin.
> - **Lines, not bytes, through the pipe.** `SerialDevice` is line-oriented (`hil/serialdev.py:98-108`), so
>   `/serial/read` returns lines since a mark and `pipe.js` gathers the page's writes into whole lines. That also keeps
>   `Bench.log`'s credential filter working: a 512-byte piece of a SET_CONFIG could split a password field.
> - **The fixture is bench-shaped, not the bench's.** No bench this session, so `config.bench.json` is built from §1.4's
>   facts through the model; `scrub_nc_config.py --from <run>/navicore_snapshot.json` replaces it with the real config,
>   one placeholder per distinct secret value, and refuses to write a leak. The specs read the file, not its content.
> - **Headless L1.** A fake port needs no grant, so nobody has to see Chrome (`NCTOOL_HEADED=1` shows it).
> - **`(should)` specs are `test.fail()`** in Playwright (green standalone and in CI, red once fixed) and FAIL in the
>   harness; the node layer uses `todo` tests, which `run_unit_tests` notes by name.
> - **The L2 proof is `nctool.board_connect_config`**, read-only: it refuses to press Save while a diff exists.
> - Code facts the specs found that the plan or the tool's comments do not say (each pinned by a spec, NC-WP3):
>   the PONG epoch does not separate the probe phases: the handler stamps the epoch current when a reply LANDS
>   (`config_tool/index.html:9515-9517`), so a late direct PONG satisfies the Via-WCB probe; `readActionFromFid` writes
>   `skipRunning: 1` (`:15433`, `:15446`, `:15458`, and `_cmdlibUse` `:14166`) and `rcConfigFromJSON` reads `obj["skipRunning"] | false`, which in
>   ArduinoJson 7.4.3 returns the default for any non-boolean (`VariantOperators.hpp:34-40`, `ConverterImpl.hpp:124-127`),
>   so an Apply clears the gate on the board (D-NC32 is worse than a phantom diff); `_fragChunks` drops the tail of an
>   input with lone surrogates (its last `flush()` leaves the carry unflushed, `:5556-5594`), latent because its callers
>   pass `JSON.stringify` output; a bridged Save over 192 fragments is refused by `sendJSON` after `saveConfigToBoard`
>   has latched the pending save (`:5663-5668`, `:16940`), so the tool shows "Saving…" for 12 s; `_appendCommandView`
>   sets the field cap while the row is detached, so on open it is 95 instead of 95 minus the `;W<n>;S<p>` prefix
>   (`:15108-15123`, reached from `sync()` at `:15192` and the render-time call at `:15235`); `_releaseSerialOnUnload`'s `try { p.close() } catch` cannot catch the promise's
>   rejection (`:4501`); the WCB channel hint says "1-13" while the field caps at 11 (`:2862-2864`). The Wizard's
>   `seqValueToLines` is at `Wizard/app.js:4485`, not :4404.
> - **The Firmware tab and the clips.** `lib/navicore/ota.js` gives the emulator `?OTALOCAL` (`navicore_ota.h:244-305`)
>   and, in via-WCB mode, the tethered WCB's `?OTA` relay with its CRC-32 check (`WCB_OTA.cpp:486-593`) in front of
>   NaviCore's target handlers (`navicore_ota.h:346-411`); faults are a lost DATA line, a lost ACK, a mangled line,
>   coalesced and split markers, stale and foreign ACKs and a held window. A verified END restarts the emulated board:
>   off the bus (`FakeSerial` refuses `open()` and writes while the device's `present` is false), deaf while it boots,
>   then back on the other slot as `otaNewVersion`; via WCB only NaviCore restarts, and the relay pauses its RC-JSON
>   passthrough for 8 s after each `?OTA` line (`WCB.ino:395-402`, `:6607-6613`). `lib/navicore/clips.js` gives it
>   `?REC` over a clip store (`NaviCore.ino:3440-3595`, `navicore_record.h`): the list, record/save/play, rename and
>   delete, the ranged and batched EDITLOAD with `fp`/`fc`/`nm`, and the indexed upload; relayed replies go back as
>   `[TERM:20]` packets of at most 160 bytes (`navicore_rterm.h:48-53`). `lib/navicore/firmware.js` serves GitHub's
>   `firmware/` listing (with a decoy for each wrong pick `flasher.js` guards against), the raw downloads, and stand-ins
>   for CryptoJS and esptool-js (`fake_cryptojs.js`, `fake_esptool.mjs`) by `page.route`; the esptool-js stand-in
>   records the regions, bytes and options the tool hands `writeFlash`.
> - Code facts from those specs: the Full Wipe texts promise the saved configuration is erased (`config_tool/index.html:3250`,
>   `:3266-3269`, `:17957`, `:18057`, `:18124-18127`) while `flasher.js` erases only NVS and otadata (`flasher.js:363-370`)
>   and `/config.json` lives in LittleFS at 0x3D0000 (`partitions.csv:19`), loaded first at boot (`NaviCore.ino:4565-4583`)
>   (D-NC34 confirmed); the button title that warns clips are not erased (`:3250`) is replaced at load by one that does not
>   (`:18057`); `runFirmwareFlash` disconnects before it downloads anything (`:17908-17917`) and reconnects only after a
>   completed flash (`:17966-17970`), so a refused or failed download leaves the user disconnected from an untouched board;
>   `otaUpdateOverUsb` rewinds after `WINDOW` cursor-stuck markers (`:17535`), but one lost chunk leaves only `WINDOW - 1`
>   chunks behind it to NAK, so every loss waits out the 10 s marker timeout (`:17514-17523`; measured 11.0 s) where
>   `otaUpdateOverWcb` counts to `WINDOW - 1` (`:17808`); `clipRecordToggle` waits for a `[CLIPUL:REC` marker
>   (`:6469-6475`) that no firmware prints (`NaviCore.ino:3451` answers "[REC] recording…" or "[REC] busy / no buffer"), so
>   a refused START is taken as started and the Stop & Save after it SAVEs the replayed buffer under the typed name;
>   `clipRestoreOne` never sends the clip's mode (`:6834-6869`) and `editBegin` keeps whatever was resident
>   (`navicore_record.h:883-888`) (D-NC33 confirmed); the flasher takes the first `NaviCore_*_ESP32S3.bin` the listing
>   names (`flasher.js:151-153`, `:174`), so a kept older build beside a newer one (the `-KeepOld` case its comment
>   mentions) is picked by GitHub's listing order; the "Latest on GitHub" check does not retry a throttled listing
>   (`flasher.js:66-79`) while the flash's own listing does (`_fetchRetry`, `:106-129`).
> - **Two tabs.** `FakeSerial` tags every open, write and setSignals with the page that made it (`pg`), so the shared-hub
>   specs can tell the owner tab from the follower. The fixture's switch SI carried its Up action on `p1`; a 2-position
>   switch reads as position 0 or 2 (`NaviCore.ino:555`, `t[1]` unused, `rc_config.h:206`) and the tool's editor shows
>   Down/Up as `p0`/`p2`, so the action was dead config. `make_nc_fixture.js` now writes it on `p2`.
> - Code facts from the Export/Import and two-tab specs: an Export, a board reset, an Import of that file and a Save put
>   the board back exactly as it was; the legacy CSV importer rebuilds every button band as center ±10
>   (`config_tool/index.html:19432-19433`) where the tool's defaults and the firmware's are ±12 (`:20600-20605`,
>   `rc_config.h:831-850`), relabels the physical buttons (`:19431`) and turns `exclusive:false` into an absent key
>   (`:18914`, `:19438`), so a CSV round trip followed by a Save narrows every band on the board; D-NC35 confirmed (a tab
>   takes another tab's ACK for its own save, `_saveSeq` `:17006`, `:16948`, matched at `:9598`, `:9616`), and both tabs
>   also number their fragment sessions from 1 (`_nextOutSid`, `:5508`), so two tabs' bridged saves at once would share
>   a sid in NaviCore's reassembly pool; Save re-reads the General tab's tap window, hold, settle and channel-rate inputs
>   into `config` first (`:16846-16872`).

In `tests/wizard` (D-NC10); details in §5. `serve.js` maps `/NaviCore/` to the sibling repo; new files
`lib/navicore/shim.js` (the fake `navigator.serial`), `lib/navicore/emulator.js`, `lib/navicore/pipe.js` (the
bridge-backed port) and a scrubbed fixture. `hil/bridge.py` gains `/serial/mark`, `/serial/read`, `/serial/write`
and `/serial/signals` for one named device (and `/sbus`, which forwards one controller verb, for the live-grid spec), served outside the bridge's lock (`bridge.py:37`, `:85`), because
SerialDevice has its own (`serialdev.py:36-41`). `hil/wizard.py run_wizard_test(..., pipe=True)` keeps the device
open instead of closing it (`wizard.py:201-202`), and `_reacquire` PINGs a NaviCore instead of returning at once
(`:105-106`).

### INF8 — SBUSController test verbs (D-NC8)

RAM-only additions to `processCommandJson` (`SBUSController.ino:1035`), none of which saves:

| Verb | Effect | Where |
|---|---|---|
| `{"t":"flags","v":n}` | a global replaces the constant `SBUS_FLAGS` in every frame (0x04 lost frame, 0x08 failsafe) | `:112`, `:762` |
| `{"t":"stream","on":b}` | gates `transmitSbus()` in `loop()`: frames stop without a reset | `:2001-2004` |
| `{"t":"mode","sbus24":b,"save":false}` | the SBUS-16/24 switch without `saveConfig()` | `:1213-1225` |
| `{"t":"glitch","kind":k,"n":i}` | one malformed burst: `truncate`, `garbage`, `double`, `gap`, or `dip` (one neutral frame on a named channel) | new |
| `{"t":"ch","c":n,"v":val}` | a raw channel value, until a control writes that channel again | new |

The controller has no OTA path, so it is flashed with esptool, app only, on COM4 (the S3 USB-Serial/JTAG reset,
like NaviCore's). The change lives on a local branch and is not pushed: pushing its `main` publishes firmware
(`SBUSController/CLAUDE.md` rule 4). Rollback: the fae0af6 build the bench runs today.

### INF9 — NaviCore test hooks (D-NC7)

> **Status 2026-09-28: built, flashed and bench-verified.** NaviCore branch `hil-week` (local, not pushed; D43 in `docs/HIL_WEEK_DECISIONS.md`): `0a66ce0` Greg's working tree taken as-is, `1e15601` the `App SHA256` line (16 hex from `esp_app_get_description()->app_elf_sha256`; `esp_app_get_elf_sha256()` gives at most 9 in core 3.3.4), `703a0e7` the hooks in `navicore_hil.h` (all under `NAVICORE_HIL_HOOKS`; a hook-free build of the same tree differs only in its ELF SHA, build string and checksum), `6925773` docs. `results/builds/navicore-hil1` (`v0.2.0_281426QSEP26`, App SHA256 `529503cd35f1e5e5`) was flashed over `?OTALOCAL` at 15:46 (app0 -> app1, 79 s, no NAK); `ncflash` verified the App SHA. On it the NaviCore suite (`nccfg.*`, `navicore.*`, `sbus.*`) passes but for the five `(should)` tests, and the three hook tests pass (`20260928-154700`, `-155919`). `results/builds/navicore` stays the rollback image. `ncflash build --source` compiles a worktree.

- **(a) Production:** `App SHA256: <16 hex>` from `esp_app_get_elf_sha256()` in `otaPrintStatus`
  (`navicore_ota.h:225-238`) and in the boot banner. It ends the image-identity problem (the DTG only changes on
  commit) and matches the ELF used to decode a crash.
- **(b) `NAVICORE_HIL_HOOKS` builds only**, compiled out of CI: `DBG_WIRE` (bit 7): a hex line, through the
  non-blocking writer, for every block written to S3, S4, S5, Serial2 and the WCBStream; `#L90,<ms>` stalls
  `loop()`; `#L91` corrupts `/config.json` (keeping a copy) for the load-fallback test; `#L92` makes the next
  GET_CONFIG overflow; `#L93` makes the next LittleFS save fail. All RAM-only, cleared at boot. A test needing one
  skips with "needs a NAVICORE_HIL_HOOKS build" when `#L90` answers `Unknown #L code`.

### The opt-in keys (hil/optin.py)

| Key | What it does to the bench | This week (D-NC14) |
|---|---|---|
| `navicore_reboot` | software restarts of NaviCore (REBOOT, `#L02`, the mesh REBOOT): USB re-enumerates, the mesh sees 20 drop for ~5 s | on |
| `navicore_wifi` | the PC's spare adapter joins NaviCore's AP for ~30 s per test | on; the test skips when the only adapter carries the default route |
| `navicore_nvs` | learns and forgets a peer (NaviCore's NVS peer mask) | on |
| `navicore_clip_write` | writes and removes `HIL*` clips in the 12 MB clips partition | on |
| `navicore_aux_tx` | routes HCR/MP3/DFPlayer/WLED to NaviCore's own S3-S5 (bytes out J4-J6, where nothing is recorded as attached), and `#L20`/`#L21` | on |
| `navicore_ota_erase` | small OTA sessions that erase part of the inactive slot | on |
| `navicore_ota_full` | full same-image OTA over USB, twice (a few minutes) | on |
| `navicore_ota_relay_full` | the same image relayed through W1, twice (~12 min a pass, like W2's twin: `optin.py:44-50`) | off; run once by hand |
| `navicore_esptool` | the recovery ladder's esptool rungs on the healthy board (`ncota.recovery_esptool`): download mode, the bench image into app0, `boot_app0.bin` into otadata | off: watched runs only (added 2026-09-28 with NC-WP2) |
| `navicore_fault` | the HIL-hook faults (corrupt then restore `/config.json`, a failed save) | on once the hook image is flashed |
| `navicore_identity` | persisted deviceId/channel/password changes that take NaviCore off the mesh until restored over USB | off: attended only |
| `navicore_webserial` | the config tool over real Web Serial and esptool-js (L3): needs Chrome's one-time grant | off: attended only |

## 4. Work packages

Ordered by value, risk and dependency. Each is verified on the bench before the next starts, in the §7 table style
of `docs/HIL_TEST_AUDIT.md` (status `todo` / `written` / `bench <run>` / `blocked <why>`). Suites: s40-s49 (D-NC1).

| WP | Scope | Tests | Prerequisites | Effort | Runs |
|---|---|---|---|---|---|
| NC-WP1 | Config surface and persistence | `nccfg.*`, ~30 | INF1, INF3, INF6 | 10 h | unattended; 3 behind `navicore_reboot`; 3 hook tests late |
| NC-WP2 | This week's image, flash and recovery | `ncota.recovery_hard_reset`, `ncota.image_identity`, `nccfg.usb_no_late_reply` | INF4, INF9, D-NC6 | 4 h | the agent watches the first flash; then `navicore_reboot` |
| NC-WP3 | Config tool without a board (L0, L1) | `nctool.static`, `nctool.unit` + ~55 specs | INF7 | 14 h | no bench; CI |
| NC-WP4 | Engine through TRIGGER and TEST_ACTION | `ncengine.*`, ~12 | NC-WP1 | 7 h | unattended |
| NC-WP5 | Engine through SBUS | `sbus.*`, ~20 | INF2, NC-WP1 | 10 h | unattended; servo |
| NC-WP6 | Mesh, bridge, WDP, telemetry, relay, failure | `ncmesh.*`, ~30 | INF6, NC-WP1 | 12 h | unattended; one `navicore_nvs` |
| NC-WP7 | Device transports and the Maestro | `ncdev.*`, ~18 | NC-WP1; local parts NC-WP2 (DBG_WIRE) | 9 h | unattended; local devices `navicore_aux_tx` |
| NC-WP8 | SoftAP and WebSocket | `ncwifi.*`, ~12 | INF5 | 5 h | `navicore_wifi` |
| NC-WP9 | Boot, reboot, failure | `ncboot.*`, ~10 | NC-WP2 (recovery proven) | 6 h | `navicore_reboot` |
| NC-WP10 | OTA | `ncota.*`, ~9 | INF4 | 6 h | non-erasing unattended; the rest opt-in |
| NC-WP11 | SBUS faults | `sbus.*`, ~8 | INF8 (D-NC8) | 5 h | unattended once the verbs exist |
| NC-WP12 | Record and replay | `ncrec.*`, ~9 | NC-WP1 | 7 h | `navicore_clip_write` |
| NC-WP13 | Config tool with the board (L2) and attended (L3) | `nctool.board_*`, `nctool.webserial_*` | NC-WP3, NC-WP1 | 7 h | L2 unattended; L3 attended |
| NC-WP14 | NaviCore's own pins | `ncwire.*`, ~7 | a probe wired to NaviCore (D-NC37) | 6 h | blocked: hardware |

**The first 48 hours**, in order:

1. No bench needed: INF1's parsers with their `selftest.py` cases, INF3's redaction, INF6, and INF7's skeleton (the
   shim, the emulator's USB mode, the scrubbed fixture); `nctool.static` and `nctool.unit` pass in CI.
2. A short bench slot: `nccfg.guard_selftest` alone, then the rest of NC-WP1 as one targeted run.
3. `ncota.recovery_hard_reset`, then NC-WP2's build and OTA flash, then `navicore.* sbus.*` on the new image.
4. NC-WP4 and NC-WP6 as targeted runs; from then on the nightly full run (D6) carries every NaviCore test that has
   passed once.

Infrastructure ~44 h plus tests ~102 h is more than one week beside the WCB and Intellex phases, so there is a cut
line (D-NC41): **must** NC-WP1, 2, 3, 4, 6; **should** NC-WP5, 7, 8, 9, 10; **could** NC-WP11, 12, 13; NC-WP14
waits for hardware. The nightly full run (D6) picks up each package once it passes its targeted run; the new tests
add roughly 30-40 minutes, most of it the 60 s mode-report wait, the 50 s offline detections and the WebSocket soak
(nightly only).

### NC-WP1 — config surface and persistence (`s40_navicore_config.py`, `nccfg.*`)

> **Status 2026-09-28: written and bench-verified** (`20260928-123843`, `-124821`; image `v0.2.0_102105QSEP26`). Every normal `nccfg` test passes, and the existing `navicore.*` and `sbus.*` suites pass on the changed driver. The five `(should)` tests fail as designed, reproducing D-NC42, D-NC43, D-NC22, D-NC17 and D-NC16 on the board. The three hook tests wait for INF9's build (`navicore_fault`, not ticked). `nccfg.monitor_stream` first skipped on a healthy 99 fps: every "full rate" check now uses `SBUS_FULL_FPS` (90) in `hil/navicore.py`.

> **Status 2026-09-28: bench-verified** (`20260928-123843`, `-124821`; see the NC-WP1 note above). `suites/s40_navicore_config.py` holds 34 tests beside
> `nccfg.guard_selftest`: 23 normal; five `(should)`: `string_truncation_utf8` (D-NC42) and `hold_exceeds_tap_window`
> (D-NC43), both found while writing these, `dest_null_hazard` (D-NC22), `mesh_creds_live_split` (D-NC17) and
> `reset_defaults_keeps_identity` (D-NC16); three behind the new opt-in `navicore_reboot` (`persist_reboot`,
> `reset_defaults_ram`, `reset_defaults_keeps_identity`); and three that need INF9's hook build and the new opt-in
> `navicore_fault`, which skip until it exists (`hook_save_fail`, `hook_get_config_overflow`,
> `hook_config_unreadable`). `nccfg.usb_no_late_reply` (NC-WP2 below) is here as well: the image on the board already
> has `kickUsbCdcTx` (results/builds/FLASHED.md). The D-NC15 fixes are in: `navicore.bench_health` (s02), and s21's
> route check reads the routing the firmware reads (`NaviCore.local_devices`). `sbus_out_toggle` and the restarting
> tests are in `hil/servos.py` `SERVO_TESTS` (SBUS OUT stops). `selftest.py` runs every nccfg test through the runner
> against `NaviModel`, a port of NaviCore's config handling (merge, printer, defaults, USB handlers, CLI quirks, RAM
> against flash, the boot copy of the mesh identity) and a W1 that relays to it: each normal test passes, each
> `(should)` test fails, the hook tests skip, the model ends as it began, and ten deliberate breaks of the model are
> each caught. Where the code differs from the plan below:
> - `parse failed` is not reached by a 20000-element array: NaviCore builds on ArduinoJson 7.4.3, where
>   `DynamicJsonDocument`'s capacity is ignored (ArduinoJson `compatibility.hpp:124-139`), so the array parses; a data
>   that is not an object then saves and answers ok:true (`nccfg.set_nonobject_data`). An invalid escape inside `data`
>   reaches it: the filtered header parse skips that string without checking escapes (`JsonDeserializer.hpp:478-493`),
>   the full parse rejects it (`:396-445`).
> - `nccfg.mesh_creds_live_split` makes the split with RESET_DEFAULTS, which loads the compile-time password into RAM
>   and saves nothing, instead of saving a throwaway password: no test changes a board's mesh password, and this way the
>   flash never holds another one. It observes the arrival on NaviCore's own console (the RTERM tee) instead of a
>   TRIGGER. It skips once D-NC16's fix keeps the identity through a reset.
> - A RESET_DEFAULTS restored by SET_CONFIG, not a restart, is only safe while the defaults decode nothing from the live
>   SBUS input. USB RESET_DEFAULTS skips the matrix re-arm SET_CONFIG does (`NaviCore.ino:3989-3992` against
>   `:3942-3952`), so if the default matrix channel 7 reads inside a default band, the defaults see a held button; they
>   have no long-press tier to consume it, so its tap stays parked (`:2347-2351`, `:2392-2415`), and the SET_CONFIG
>   restore clears the debounce but not that tap: the release it causes fires the RESTORED mapping of that button
>   (`:2367-2390`). A default mode switch (SE on CH12) reading another mode re-arms the mode-aware knobs (`:2787-2798`).
>   `mesh_creds_live_split` checks both first and skips (`_defaults_live_effects`); the restarting tests need no check,
>   since tap and mode state are RAM. The bench matches the defaults on both counts (matrix CH7, default bands, SE on
>   CH12). The firmware side is D-NC44.
> - The map's "SET_CMDLIB with unbalanced data gets ok:false 0/0" cannot happen over USB: the header parse fails first
>   and answers `ERROR ... (InvalidInput)`; `nccfg.cmdlib_errors` checks that.
> - `nccfg.filter_whitelist` does not probe `all`: its only positive probe forgets every learned peer, an NVS write
>   (`ncmesh.wdp_learn_forget`, `navicore_nvs`).
> - `nccfg.unvalidated_echo` leaves out matrixChannel, funcBindings.mode and the knob and switch channels, which index
>   SBUS arrays live, and boardType (`boardtype_change_no_reboot`, `ncboot.boardtype2_mismatch`).
> - `nccfg.clamps_baud` probes only ports already at 9600: an out-of-range baud falls back to the port's default, not
>   its current rate (`rc_config.h:1495-1497`), so a probe of S3 (115200) or the Maestro (57600) would re-open it. The
>   apply-time clamp (`NaviCore.ino:3229-3234`) needs INF9's `#L91`.
> - The save-failure branch of `nccfg.set_ack_shapes` is its own test, `nccfg.hook_save_fail`.
> - `nccfg.string_truncation` leaves out wifiSsid, wifiPassword and the mesh password (credentials and identity).

Every write goes to fields with no live effect, to slot 136 (unmapped in every mode), or is restored at once;
everything inside `nc_guard`.

- `nccfg.guard_selftest`: INF3's proof (above). Runs first. Written 2026-09-28 (`s40_navicore_config.py`); passes on the bench (`20260928-064402`, `-070951`, `-123843`)
  yet.
- `nccfg.get_config_shape`: the key set of `rcConfigToJSON` (`rc_config.h:1211-1480`, per the map); arrays 36/8/4/6/3;
  switch and knob label keys; action type names.
- `nccfg.set_ack_shapes`: `saveId` echoed on ok (`NaviCore.ino:3953`); `missing data` with saveId (`:3925`); `parse failed` without
  saveId (`:3923`), reached with a `data` holding a 20000-element array (the filtered header parses, the 98 KB
  document does not); the save-failure branch via `#L93` (hook).
- `nccfg.diff_toplevel`, `nccfg.mappings_per_key`, `nccfg.branch_semantics`, `nccfg.string_truncation`,
  `nccfg.clamps_timing`, `nccfg.clamps_baud`, `nccfg.clamps_mesh_channel`: the SET_CONFIG semantics of the map's
  `nc.cfg.*` rows (tables in §2), each read back with GET_CONFIG.
- `nccfg.side_effects_live`: exactly one `[AUX] S4 re-open @ 19200` for one changed baud, none for a no-op save; a
  label on S5 appears in W1's `?WDP,DUMP` as `[WDPIF:N=20,S=3,DEV=HIL…]` within a few seconds.
- `nccfg.sbus_out_toggle`: `[SBUS] OUT disabled`/`enabled` (whatever is on J2 loses SBUS for about a second; §6).
- `nccfg.boardtype_change_no_reboot`: the INFO line, then the ACK; no re-open while flipped; boardType restored before
  anything could reboot.
- `nccfg.cmdlib_roundtrip`, `nccfg.cmdlib_errors`: ACK size and hash equal a host FNV-1a; GET bytes equal; META
  agrees; skips unless a library is stored (D-NC13).
- `nccfg.filter_whitelist`: one probe per whitelisted key (`NaviCore.ino:3809-3821`).
- `nccfg.json_errors`: parse errors with `rxLen`, `unknown type`, the prefix quirks, bare PING silent.
- `nccfg.line_framing_paced`: a ~20 KB SET_CONFIG paced gets its ACK; a 100 KB `{` line gets `ERROR ... rxLen 98304`.
- `nccfg.monitor_stream`: about 40 PWM_UPDATE frames in 2 s; `channels` equal `#L09`; nothing 200 ms after STOP.
- `nccfg.version_surfaces`, `nccfg.backup_block`, `nccfg.debug_flag_bits`, `nccfg.unvalidated_echo`,
  `nccfg.set_nonobject_data`, `nccfg.dest_null_hazard` (should).
- `nccfg.mesh_creds_live_split` (should, D-NC17): a throwaway password without a reboot; `;W20,?version` loses its
  `[TERM:20]` while `;W20,{TRIGGER}` still runs; restored within the test.
- Opt-in `navicore_reboot`: `nccfg.persist_reboot` (GET, REBOOT, GET byte-identical; `RC config saved to LittleFS
  (N bytes)` with N equal to the data length), `nccfg.reset_defaults_ram` (defaults in RAM, no re-open lines on
  USB, REBOOT brings the save back), `nccfg.reset_defaults_keeps_identity` (should, D-NC16).
- Late, hook build and `navicore_fault`: `nccfg.hook_config_unreadable`, `nccfg.hook_get_config_overflow`.
- Test fixes that belong here (D-NC15): the key names in `s21:312` (`mp3`/`dfp`/`wled` should be
  `mp3Dest`/`dfpDest`/`wledSlots`), so the guard in `navicore.serial_route_dbg` can see a locally routed device; and
  `navicore.bench_health`, which FAILS when NaviCore sees no SBUS or its local Maestro does not answer (today both
  show up only as skips).

### NC-WP2 — this week's image, flash and recovery

> **Status 2026-09-28: written and bench-verified** (`20260928-212818`: `ncota.image_identity` and `ncota.recovery_hard_reset` pass; `suites/s47_navicore_ota.py`). Steps 2-4
> were done by INF9 (D45: `navicore-hil1` is on the board). `ncota.recovery_hard_reset` (`navicore_reboot`) runs the
> real `ncflash.recover()` with its PING rung withheld (`_Withheld`), so the ladder climbs to rung 2 as it would for a
> hung app. The proof the chip restarted is its uptime: GET_MESH_STATS `upMs` below the time since the reset, exact
> whatever the uptime was before. `recover()` alone cannot show it: its fallback PING calls rung 2 a success whenever the
> app answers, which a reset that did nothing also passes (a `selftest.py` mutation caught this). `ncota.image_identity`
> (read only) finds the build folder whose image carries the board's App SHA256 (`ncflash.builds_with_sha`) and requires
> the ELF beside it, PONG and STATUS on its version, FLASHED.md's newest successful write (`ncflash.last_written`) naming
> it, and `ncflash.BENCH_IMAGE` (`navicore-hil1`, one constant for the week's image); anything else fails with the
> commands that put it back (`ncflash.put_back`). New and off by default: `ncota.recovery_esptool` (opt-in
> `navicore_esptool`, watched runs only) repeats D35's proof through the real ladder with the bench image as the known
> good, so NaviCore ends on `navicore-hil1` in app0. `nccfg.usb_no_late_reply` was already in s40. `selftest.py` runs
> every s46 and s47 test but `sbus.boot_quiet` against `NaviBootModel`, a port of `hil-week`'s restart, OTA and mesh
> behaviour (each normal and opt-in test passes, each `(should)` fails), and nine mutations of it (six breaks, each
> caught; the three D-NC fixes, each turning its `(should)` test into a pass). Step 6 waits (D43).

1. Prove the ladder: `ncota.recovery_hard_reset` (opt-in `navicore_reboot`): `hard_reset()` reboots NaviCore
   (`Reset reason` names the USB peripheral), PING answers.
2. Build `navicore-hil1` from the trees chosen in D-NC6, with INF9 (a) and (b).
3. Flash it with INF4 while the agent watches the session log; keep `results/builds/navicore` as the fallback.
4. Run `navicore.* sbus.* ota.relay_navicore_brick_guard`: the four `(should)` tests still pass, and
   `navicore.pull_over_limit` now takes its post-F13 branch (CFGERR NOPARTS for a plain pull, joinable parts for
   `,P`).
5. New: `ncota.image_identity` (the SHA line equals the image), `nccfg.usb_no_late_reply` (800 back-to-back PINGs,
   each answered before the next: `kickUsbCdcTx`).
6. Per D-NC6, push WCBClient master, then commit and push NaviCore main; CI's build then carries the same fixes.

### NC-WP3 — the config tool without a board

§5. `nctool.static` and `nctool.unit` (L0) and the ~55 L1 specs of the config-tool table in §2. No bench time;
runs in CI, and can be written while the bench is busy with other plans.

> **Status 2026-09-28: written, all run headless with no board** (`suites/s49_navicore_tool.py`; how to run them:
> `docs/HIL_TESTING.md` §8). L0: `nctool.static` (9 checks) and `nctool.unit` (the page's pure functions, 10 checks,
> one a `todo` for the latent `_fragChunks` tail drop; plus the rig's own 6). L1, 58 specs by file in `tests/wizard/specs/navicore`:
> `load` (`load_smoke`, `connect_direct_sequence`); `transport` (`transport_flag_reset`, `disconnect_teardown`,
> `link_loss_reconnect`, `link_loss_ambiguous_ports`, `via_wcb_hub`, `keepalive_via_wcb`, and the `(should)`
> `pong_epoch_slow_direct` and `doorway_pong_misdetect`); `rx` (`rx_framing_markers`, `rx_fragments`,
> `config_error_no_baseline`); `save` (`apply_all_keys`, `no_phantom_diff`, `save_diff_payload`, `save_ack_correlation`,
> `reset_defaults_flow`, `close_prompt`, the `(should)` `refresh_overwrites_edits`); `bridge` (`push_budget_prediction`,
> `sendjson_fragments_exact`, `sendline_chunking`, the `(should)` `push_refused_not_pending`); `editors`
> (`button_modal_trigger`, `test_action_button`, `command_view_limits`, `wcb_network_profiles`,
> `wcb_network_bridged_strip`, and the `(should)` `noop_apply_every_editor`, `skip_running_saved`,
> `test_action_refusal_shown`, `command_view_cap_on_open`); `firmware` (`fw_flash_mocked`, `fw_flash_partial_refused`,
> `fw_wipe_regions`, `ota_usb_state_machine`, `ota_usb_failures`, `ota_wcb_state_machine`, `ota_wcb_failures`, and the
> `(should)` `fw_refused_flash_keeps_session`, `fw_wipe_text` (D-NC34), `ota_usb_lost_chunk`); `clips`
> (`clips_list_forms`, `clips_record_rename_delete`, `clip_download_verified`, `clip_backup_bundle`, `clip_restore`,
> `clip_restore_bridged`, `timeline_editor_save`, and the `(should)` `clip_record_refused`, `clip_restore_mode`
> (D-NC33)); `io` (`export_import_json`, the `(should)` `csv_roundtrip`); `hub` (`shared_hub_two_pages`, the `(should)`
> `multi_tab_save` (D-NC35)); `live` (`live_monitor`, `wcb_status_panel`). The fifteen `(should)` specs each fail at
> their own assertion; the INF7 note lists what they found. Not yet written from the §2 table: the command-library
> catalog, sync and sequence-source specs, rc telemetry, calibration, cloud backup, channels/transmitter and
> misc-editor, Maestro XML, and the Intellex contract, which needs Intellex's own transport stubbed (its shim is a
> 1,600-line HTTP/WebSocket client); `nctool.static` already holds the byte identity and the shim's ids.

### NC-WP4 — the engine through TRIGGER and TEST_ACTION (`s41_navicore_engine.py`, `ncengine.*`)

> **Status 2026-09-28: written and bench-verified** (`20260928-204259`: 9 pass; the two `(should)` tests fail as designed, D-NC20 and D-NC45; `maestro_skip_running_slot` skips, no undriven local Maestro channel reading a position in range). `suites/s41_navicore_engine.py`
> holds 12 tests: the ten below, plus two `(should)`: `ncengine.test_action_skipped_not_ok` (D-NC20) and
> `ncengine.skip_not_traced_as_sent` (D-NC45, found writing these). `maestro_skip_running_slot`, `mode_report_content`
> and `mesh_trigger_burst` are in `hil/servos.py`. Line numbers in the docstrings are the `hil-week` tree, the image on
> the bench. Where the code differs from the plan below:
> - `ncengine.skip_running_fail_open` gates on the remote slot whose device a WCB really hosts (Maestro 2 on W2), with
>   query verbs (`;M2,getErrors`), not device 4 on W1. Nothing hosts device 4, and W1 neither forwards nor answers a `;M4`
>   verb or get that arrived over the mesh (WCB `WCB_Maestro.cpp:490-497`, `:566`), so the warm-up is never answered and
>   the cached half cannot be shown. `maeGateMs` is raised to 5000 inside the guard, so the fresh window outlasts the
>   harness's round trip. The host must carry `?CONTROLLER,ON,20` (both WCBs do).
> - `ncengine.mesh_trigger_burst` needs `loop()` stalled while the triggers land. It uses INF9's `#L90` (it skips on
>   another image), and first makes the SBUS side of the engine inert (s41 `_engine_inert`: no bands, no mode function,
>   every knob off), because a stall past ~100 ms overflows the SBUS UART (NC-WP5's `sbus.stall_no_phantom`). Pinned:
>   of 12 triggers, exactly 8 dispatch, in order; the rest are dropped with no log line.
> - `ncengine.test_action_matrix` asserts what each action does (the bytes, the skip line), not the `ok` of a skipped one,
>   so D-NC20's fix will not break it; `ncengine.test_action_skipped_not_ok` asserts `ok:false`.
> - `ncengine.stats_report_content` cannot compare equal values: the counters keep moving. It brackets W1's reported row
>   between GET_MESH_STATS read just before and just after the report (the counters only rise).
> - `ncengine.calibration_gate`: a delayed action that comes due under CALIB is dropped, not deferred (its slot is freed
>   either way, `NaviCore.ino:2202-2220`).
> - `ncengine.maestro_skip_running_slot` leaves the moved channel at speed 0 ("no limit"): Pololu has no speed readback,
>   so a Control Center speed limit on that channel, if it had one, is gone until the Maestro resets.

A guarded temporary mapping on slot 136 whose actions are `wcb_unicast` to W1 with `;S2HIL<tier><nonce>`, read on
the W1S2 probe:

- `ncengine.tiers_exclusive_cumulative`: a USB TRIGGER bypasses the tap engine, so tap 2 on a non-exclusive mapping
  fires t1 then t2 at once; exclusive fires t2 only; tap 4 fires t4 alone; the order is checked on the wire. (The
  SBUS-driven timing, all tiers together after the window, is `sbus.long_press_configured`'s and
  `sbus.other_button_commits`' job.)
- `ncengine.delay_queue`: delays measured from the trigger on the probe clock, in parallel; t1 and t2 with five
  delayed actions each overflow the 8 slots: the 9th and 10th fire at once, each with `WARN: pendingActions full (8
  slots)`; a
  delayed copy still fires after a SET_CONFIG removes its mapping.
- `ncengine.calibration_gate`: under CALIB, `rc_trig` still appears but no dispatch line and no probe bytes;
  TEST_ACTION still sends; a pending delayed action is muted when due; PING or STOP_MONITOR resumes; CALIB over the
  mesh mutes nothing.
- `ncengine.rc_trig_emit`, `ncengine.mesh_trigger_burst`, `ncengine.test_action_matrix`,
  `ncengine.skip_running_fail_open`, `ncengine.maestro_skip_running_slot` (servo),
  `ncengine.mode_report_content` (`;S2HILM{mode}` to W1, after SET_MODE and again ~60 s later; servo),
  `ncengine.stats_report_content`.

### NC-WP5 — the engine through SBUS (`s42_navicore_sbus_engine.py`, `sbus.*`, all servo-listed)

> **Status 2026-09-28: written and bench-verified** (`20260928-212848`, `-215752`: every test passes but the `(should)` `reconfig_parked_tap_cleared`, which fails as designed, D-NC44. The first pass, `20260928-204259`, skipped 17 of them on a stale one-second fps window after a config restore, now waited out by `NaviCore.sbus_full_rate`; a stale `#L12` reply ended `#L13`'s read early, now consumed by `_eat_poke`; `knob_hcr_volume` moves 1500/1510, since this controller's rx stick tops out at 1606). `suites/s42_navicore_sbus_engine.py` holds 21 tests: the twenty below,
> plus one `(should)`, `sbus.reconfig_parked_tap_cleared` (D-NC44, which had none). All but `sbus.lock_under_load`
> (reads only, nothing moves) are in `hil/servos.py`. The sticks are driven to exact counts: `axis_arg` inverts the
> controller's `axisToSbusRange`, checked offline against a float32 model of it for every count on all four axes, and
> #L09 confirms the count where a test depends on it. Knob frames are read on W1 S1 (the hook image's `DBG_WIRE` copy is
> quoted when they disagree). Where the code differs from the plan below:
> - `sbus.matrix_logical_band` keeps the matrix on CH7 and presses the controller's lua buttons, whose values (274,
>   376, ...) fall between the physical bands, instead of rebinding the matrix to a stick.
> - `sbus.matrix_debounce_n`: the controller cannot send a one-frame press on demand (INF8's `dip` does not exist yet),
>   so the test presses for 4-12 ms on the host, two frames at most: at debounce 1 some of these commit, at 4 none may,
>   and real taps 1-3 still resolve at 4. Tries whose host-side hold ran past 12 ms are not counted.
> - `sbus.switch_easing_seed` also needs a passthrough knob with an output on the slot: `reapplyMaestroEasing` drives
>   only knob-driven channels. The knob sits on channel 0, never dispatched but counted by the re-apply.
> - `sbus.mode_switch_decode`: the decode reads the value only; `positions` plays no part (a 2-position switch gives
>   modes 1 and 3 because it only sends 173 and 1811). The deadband is `> 5`: 586 after 581, and 1405 after 1400, are
>   not looked at.
> - `sbus.knob_mode_aware` rebinds the override knob's switch to the resting ry stick. An override switch with no
>   channel reads -1, and the knob then follows the global mode after all (`NaviCore.ino:2622-2623`), so it would
>   re-dispatch at every SET_MODE like a plain mode-aware knob.
> - `sbus.knob_hcr_volume` reads NaviCore's own DBG_HCR trace, on audio channel B (the bench's volume knobs drive V and
>   A, and the 80 ms throttle is per channel). W2's bytes are left to NC-WP7's `ncdev.hcr_remote_verbs`. The knob clamps
>   at 99 while the codec takes 100: pinned.
> - `sbus.prefix_ambiguity_ch17`: this bench sits in the ambiguous state at rest (CH17 = 173 from switch SJ, CH18 = 992,
>   so frame byte 24 is 0x00 and byte 23 is 0xAD); nothing needs moving.
> - `sbus.stall_no_phantom`: on the hook image, 16 `#L90` stalls of 110-485 ms plus 4 no-op saves (20 saves on another
>   image). Everything that could act on a phantom frame is made inert first, and free knobs on the matrix, mode and
>   Maestro-knob channels become detectors passing through to remote slot 4. Serial1 keeps the core's 256-byte RX ring
>   (`HardwareSerial.cpp:123`, core 3.3.4) and no UART event task flushes it. If the code is read right, a stall that
>   overflows leaves one seam, which passes the reader's header/length/footer check for about 3 of the 36 possible
>   offsets (the resting frame has 0x00 at bytes 24, 34 and 35), so this test should fail on most runs if the hypothesis
>   holds. No unexplained rc_trig appeared across the ~30 saves of run `20260928-123843`, so a save's own stall may be
>   too short to overflow.
> - `sbus.reconfig_live`: a matrix button held across a USB save and across a bridged save behaves the same (one tap at
>   most), so the plan's "USB vs mesh matrix reset" difference is not observable that way; the parked tap itself is
>   D-NC44's `(should)`. "A delayed copy survives" is in `ncengine.delay_queue`.
> - `sbus.lock_under_load` uses `SBUS_FULL_FPS` (90), not 100 (the harness's rule since NC-WP1).

Inputs are the free channels: the rx and ry sticks (CH1, CH2), fine-steerable through `{"t":"a"}`. Outputs go to
remote slot 4 (bytes on W1S1, nothing moves) or to W1S2 markers.

- Matrix and taps: `sbus.matrix_logical_band`, `sbus.matrix_debounce_n`, `sbus.tap_saturation_4`,
  `sbus.other_button_commits`, `sbus.mode_latched_at_press`, `sbus.long_press_configured`,
  `sbus.held_second_tap_midhold` (pins today's mid-hold fire).
- Switches: `sbus.switch_tiers_settle_seed` (SA rebound to CH1: each settled position fires after at least
  switchSettleMs, a fast 0-to-2 sweep never fires p1, a save re-seeds without firing), `sbus.switch_easing_seed`,
  `sbus.mode_switch_decode`.
- Knobs: `sbus.knob_passthrough_remote` (the formula `posMin + (v-172)(posMax-posMin)/1639`, deadband 5, reverse,
  midClosed), `sbus.knob_mode_aware`, `sbus.knob_auto_release`, `sbus.knob_easing_resolve`, `sbus.knob_hcr_volume`,
  `sbus.calibration_mutes_knobs`.
- Decoder under load: `sbus.lock_under_load` (flags 0x7F, the monitor, `?MAE,GET` bursts and GET_CONFIG: fps stays at
  100 or more, the variant never changes, the frame counter only rises), `sbus.prefix_ambiguity_ch17` (the controller
  control on CH17, found in getcfg, set to 172 while CH18 rests at 992, so frame byte 24 is 0x00 and byte 23 is 0xAC:
  20 s under load with no phantom failsafe, CH17 exactly 172, and a matrix press still dispatches; NaviCore's SJ
  binding on CH17 fires its p0 tier on the way, text to W2S2, a probe wire), `sbus.stall_no_phantom` (20 no-op SET_CONFIG stalls of 100 ms or more with steady channels: no rc_trig, no
  switch dispatch, no PWM_UPDATE excursion; the `nc.sbus.overflow_misalign` hypothesis), `sbus.reconfig_live`.

### NC-WP6 — mesh, bridge, WDP, telemetry, relay, failure (`s43_navicore_mesh.py`, `ncmesh.*`)

> **Status 2026-09-29: bench runs `20260929-025701` and `20260929-042105`; the four failures of the second fixed, not
> yet re-run** (`suites/s43_navicore_mesh.py`, 36 tests). The first run failed eight tests on their own faults (the
> five bullets before the last); after those fixes the second passed five of them and ran for the first time what they
> check after their guard, which found D-NC48 (`mgmt_frag_multichunk`, split out of `mgmt_stats_frag`) and three more
> test-side faults: `mgmt_etm_char` read the bare `[MGMT:ETM,2]` tag line as the whole reply, `seq_pull` lost one W2
> reply on the air (tracker #109), and `bridged_cmdlib`'s SBUS gate re-read for 1.2 s instead of 4 (the last bullet).
> Seven `(should)`: `bridged_set_config_strip` (D-NC18), `bridged_reset_keeps_identity` (D-NC16),
> `bridged_cmdlib_keys_after_data` (D-NC47), `bridged_wcb_send_findings` (D-NC27), `long_command_truncation` (D-NC26),
> `seqval_verbatim` (D-NC46) and `mgmt_frag_multichunk` (D-NC48); every one that has run fails as designed.
> `wdp_learn_forget` sits behind the opt-in `navicore_nvs` (ticked for the nightly runs, D56).
> `bridged_reset_defaults`, `bridged_reset_keeps_identity` and `remote_cli_order_and_drop` (its `#L90` stall) are in
> `hil/servos.py`. No test restarts NaviCore or sends SET_MODE. `selftest.py` runs the 13 bridge tests against
> `NaviMeshModel` and 12 mutations of it. The other 23 need timing or hardware the model does not keep. Where the code
> differs from the plan:
> - `ncmesh.deaf` stops a board's reception, not its transmission. A deaf W2 keeps heartbeating, so NaviCore keeps it
>   online. `online_tracking_flip` stretches W2's `?ETM,HB` to 60 s instead and restores it in a finally. The plan's
>   11+ refused sends move to a new test, `ensured_degrade`: a deaf W2 never ACKs, NaviCore's 10 ETM slots fill, and the
>   sends after that answer `ok:false` with `send refused by WCB_Client`. `mesh_loss_local_survives` deafens W1 and W2
>   together, not in turn.
> - A CRC-less command is ACKed before its CRC is checked, on purpose (`WCB_Client.cpp:2825-2845`). So in
>   `crc_namespace_gates` the probe sends it once and it is rejected with `Missing CRC`. The plan expected no ACK.
> - `long_command_truncation` (D-NC26) uses the CLI path: `?HILT...` at 300 characters answers `Unknown command:` with
>   the first 199 (`NaviCore.ino:90`, `:2925-2930`). It does not send `;s1` to a port: no one watches NaviCore's aux
>   ports on this bench, and `;M` already refuses an over-long line.
> - `bridged_set_config_strip` (D-NC18) flips `wifiEnabled` rather than sending a channel. A valid other channel would be
>   saved and take NaviCore off the mesh at its next boot.
> - W1's console reaches session.log unredacted, so two things never cross W1. The plan's `bridged_get_config` is not
>   run: a CONFIG reply carries the mesh password, the AP password and the AP's name. `bridged_wcb_meta` exercises the
>   same fragment sender with a payload that holds no secret; GET_CONFIG's ERROR guard and single-flight line are not
>   exercised. The `?backup` over RTERM in `remote_cli_order_and_drop` is not sent either.
> - W1 opens its 20 s relay window for any `;W20,` payload that starts with `{`, fragment envelopes included
>   (`WCB.ino:8019-8021`). `hil/ncmesh.py`'s `send_fragments` docstring said otherwise and is corrected.
> - `mgmt_stats_etm_frag` is three tests: `mgmt_stats_frag`, `mgmt_etm_char` (the characterization loads the mesh for
>   about 10 s an ask) and `mgmt_frag_multichunk` (the multi-chunk push, D-NC48).
> - The plan puts `nc.cmdlib.bridge_divergence` inside `bridged_cmdlib` as a `(should)`. It is a test of its own,
>   `bridged_cmdlib_keys_after_data` (D-NC47), so `bridged_cmdlib` stays a normal test.
> - The 5-line burst of `remote_cli_order_and_drop` needs INF9's `#L90` stall, so that part skips on a stock image.
>   `dedup_after_w1_reboot` reboots W1 twice: after one reboot the new sequence numbers are too old to be remembered and
>   pass anyway.
> - `alias_whoami` empties NaviCore's alias cache with a `wcb_alias` message from W1, as W2 would answer. A WCB with no
>   alias advertises no ALIAS TLV. The 4-query budget re-arms only on an offline-to-online edge, so the test skips when
>   the budget was spent earlier.
> - `wdp_identity_fields`: a WCB's row for NaviCore reads `PEER=0` only where NaviCore is that board's controller
>   (`?CONTROLLER,ON,20`) and `PEER=2` where auto-join learned it. `HW` is 0, because NaviCore sends no HWVER TLV.
> - Left out: `rc_mode` (`nc.telem.rc_mode`), because a mode change moves the dome (`ncengine.mode_report_content` and
>   `navicore.set_mode` change modes, servo-listed). Also left out: `WCB_SEQVAL` status 2 (a WCB answers TOOBIG only
>   when the key, the value and a 4-character header pass 2912 characters, `WCB.ino:4748-4754`) and the source-MAC
>   namespace check (a probe on other octets could not address NaviCore at all).
> - A TEST_ACTION's ACK reaches W1 about 0.1 s after its marker reaches W1 S2, so `fragment_reassembly_edges` waits for
>   each case's ACKs and 0.8 s more. Counted at the marker, each ACK landed one case late (the reversed and the `of`
>   cases read 0 and the cases after them 2), while NaviCore ran every message exactly once: no NaviCore finding.
> - `probe_peer`'s burn ends by setting NaviCore's debug flags to 0, so `crc_namespace_gates` sets DBG_MAESTRO inside
>   the `probe_peer` block. Set around it, the control command printed no `[WCB RX]` line, and the negative checks
>   after it could see nothing either.
> - `#L09`'s one-second fps can read low around a busy `loop()` while no frame is lost: 88 while the frame counter rose
>   118 in 1.06 s. The fragment-sender tests judge the counter across the transfer instead (at least `SBUS_FULL_FPS`
>   frames a second on host time) and lost/failsafe in every reading (`_sbus_kept_up`).
> - W2 sends its `ETM,CHAR` reply once, as broadcast frames 20 ms apart, while the 10 s load its run started on W1 is
>   still on the air (`WCB.ino:4611-4650`, `:2044-2053`); a config reply has a second pass, this one does not. In the
>   run all three frags left W2 and NaviCore printed nothing. `mgmt_etm_char` waits for W2's own send line and asks once
>   more when the reply never came.
> - Each test undoes its own WCB writes in a finally (`_put_back`, `_seq_clear`): config_guard fails a test whose board
>   does not end as it began, even when it puts the token back itself.
> - A WCB answers every relayed STATS, ETM,CHAR and sequence request once, in broadcast frames (tracker #109), and
>   NaviCore's sequence request is three broadcast frames: in the second run one W2 names reply was lost and NaviCore
>   said `no reply`. `mgmt_stats_frag`, `seq_pull` and `seqval_verbatim` ask once more after a lost reply, with
>   `?DEBUG,MGMT` on the WCB so the note says which leg was lost (`_seq_ask`, `_reply_leg`), as `mgmt_etm_char` does.
>   The ETM,CHAR text starts with a newline (`WCB.ino:2298-2344`), so the relayed block follows the bare tag line; the
>   test compares it with the block W2 printed. An SBUS gate uses `NaviCore.sbus_full_rate`'s full 4 s: nc_guard's
>   snapshot reads the whole command library and left one-second windows of 27 and 77 fps with no frame lost.

- Bridge: `ncmesh.bridged_get_config` (fragments on W1 reassemble to the USB `data`, compared by hash;
  single-flight; `#L09` fps unaffected), `ncmesh.bridged_set_config_strip`, `ncmesh.bridged_reset_defaults` (the
  mesh path runs the side effects the USB path skips, and the password split shows; restored over USB),
  `ncmesh.bridged_cmdlib` (with the bulk `bb`/`bc`/`bd` transfer), `ncmesh.fragment_reassembly_edges` (out of order,
  duplicate, `of` mismatch, 5 s expiry, a 4th concurrent session exhausts the pool of 3),
  `ncmesh.bridged_status_meta_stats`, `ncmesh.bridged_wcb_send_findings` (should, D-NC27),
  `ncmesh.bridged_usb_only_types`.
- Relay and terminal: `ncmesh.mgmt_stats_etm_frag`, `ncmesh.mgmt_rterm_relay`, `ncmesh.raw_hook_interleave`,
  `ncmesh.remote_cli_order_and_drop`.
- WDP: `ncmesh.wdp_identity_fields` (the N=20 row is required; this also fixes the vacuous `wdp.dump_fields`,
  `s18:1172`), `ncmesh.wdp_port_labels`, `ncmesh.wdp_solicit_keeps_rows`, `ncmesh.wdp_neighbour_table`,
  `ncmesh.alias_whoami`, `ncmesh.wdp_learn_forget` (`navicore_nvs`).
- Membership and failure: `ncmesh.etm_ack_sender_side`, `ncmesh.w1_tracks_20`, `ncmesh.online_tracking_flip` (W2 deaf
  over 50 s: the OFFLINE line, `online[1]=0`, 11+ WCB_SEND give `ok:false` and MESH_STATS fail/ung rise, `#L09` fps
  steady; restored: ONLINE, the next send lands), `ncmesh.mesh_loss_local_survives`, `ncmesh.dedup_after_w1_reboot`,
  `ncmesh.crc_namespace_gates` and `ncmesh.long_command_truncation` (should, D-NC26), both through the probe.
- Content: `ncmesh.wcb_status_content`, `ncmesh.seq_pull`, `ncmesh.telemetry_fields_rates` (rc_hb every 2 s with
  fw, mode and fps right; rc_ch at chRateHz, 2 against 10 applied live; NaviCore's own 15 s gate measured between 15
  and 20 s, where W1's 20 s window cannot mask it), `ncmesh.w_route_chains`.

### NC-WP7 — device transports and the Maestro (`s44_navicore_devices.py`, `ncdev.*`)

> **Status 2026-09-29: written and bench-verified** (`20260929-025701`: all 14 normal tests pass; the four `(should)` tests that run fail as designed, D-NC23, D-NC56, D-NC58, D-NC60; the five behind `navicore_aux_tx` skip, D56; `navicore.mae_cli_local` and `navicore.maestro_mesh_fanout_0_9` pass as changed). `suites/s44_navicore_devices.py` holds 23 tests: the 18 below, plus
> five `(should)` for findings made writing them: `ncdev.mae_verb_no_alias` (D-NC56), `ncdev.hcr_local_volstep_cap`
> (D-NC57), `ncdev.serial_action_paced` (D-NC58), `ncdev.hcr_level_same_both_ways` (D-NC59) and
> `ncdev.wled_forward_normalised` (D-NC60); `ncdev.mae_subroutine_msb` is D-NC23's. In `hil/servos.py`: the three
> local-Maestro tests, and `serial_action_paced` and `mesh_forward_burst`, which stall `loop()` (so SBUS OUT pauses)
> with the engine made inert first (s41 `_engine_inert`). Opt-in `navicore_aux_tx`, registered in `hil/optin.py` and
> off in `bench.json`: `hcr_local_payload`, `local_device_bytes`, `cli_hcr_test_codes`, `hcr_local_volstep_cap`,
> `hcr_level_same_both_ways`. `hcr_local_payload`, `local_device_bytes`, `serial_action_bytes` and
> `mesh_forward_burst` skip on an image without the hooks. In s21, `navicore.mae_cli_local` now needs a value, not
> `timeout` (D-NC15), and `navicore.maestro_mesh_fanout_0_9` reads back the target on each slot. `selftest.py` runs
> every test but `knob_local_readback` (it needs the controller) against `NaviDevModel`, a port of the paths they
> drive: the normal and opt-in tests pass, each `(should)` fails naming its D-NC; six breaks of the model each make
> their test fail, and each D-NC fix makes its `(should)` test pass. Line numbers are the `hil-week` tree and the
> WcbCmd 0.9.1 it compiles. Where the code differs from the plan below:
> - The local 'a target of 16384 reads 16383' (§2 `nc.mae.frames`) is asserted as bytes on the remote slot
>   (`mae_remote_verbs`: `setTarget,5,20000` sends 16383). A Maestro clamps a target to the channel's own range, so on
>   a real channel the test would read that range's end, after a full-travel move.
> - The local tests use `NaviCore.undriven_channel` (ch 1 of Maestro 1, the channel
>   `navicore.maestro_mesh_settarget_readback` already moves). It is off at rest (it reads 0), so the tests work around
>   6000 and turn it off again; the easing test first puts it at 6000 with speed 0, since a channel switched on by a
>   target jumps there.
> - `mae_remote_read` uses whichever remote slot's device a WCB hosts (s41 `_hosted_remote_slot`: slot 2, Maestro 2 on
>   W2) and also checks W2's RAM copy of each value (`?VAR,GET,m2pos0`, `m2moving`, `m2err`), which it clears after.
> - `mae_remote_verbs` and the `(should)` Maestro tests give restartScript a profile with no entries for the slot, so
>   the switch easing never adds frames before the subroutine frame. `mae_subroutine_msb` is remote only: the local
>   variant would set Maestro 1's serial-error bit.
> - `easing_repeat_frames` needs a passthrough knob with an output on the slot, as NC-WP5's `sbus.switch_easing_seed`
>   does: a free knob on SBUS channel 0, never dispatched but counted by the re-apply, and two borrowed profiles with
>   random speeds.
> - Every config save re-applies easing and schedules two repeats 500 ms apart on every slot (processSwitches'
>   seed, `NaviCore.ino:2466-2478`), which on this bench puts J4's Snappy speed on slot 2 into the stream. So
>   `mae_remote_stream_exact` waits 1.5 s at its start and after its own save, `serial_action_bytes` checks only the
>   aux ports for stray writes, and `mae_local_frames_readback` compares only its own channel's frames on Serial2.
> - The device tests compare each transport with one byte table (s15's and s09's, WcbCmd's vectors) instead of running
>   a W2 leg beside every local test: both transports equal the same table. Only `hcr_level_same_both_ways` runs both
>   legs, which keeps W1's route learning and W2's config writes down.
> - `serial_action_bytes` sends its 106-character line to S3, the hardware UART. On a bit-banged S4/S5 a long line
>   blocks `loop()` for its whole length (D-NC58), which `serial_action_paced` measures through the delay of the USB
>   ACK; the §2 'fps dip' is a note in that test, not asserted.
> - `mesh_forward_burst` makes the plan's 'the 5th dropped' deterministic with INF9's `#L90`: of 6 forwards sent
>   during a 3 s stall exactly the first 4 go out, in order, and one sent after the burst goes out.
> - `bcast_out_opt_in`'s device-owned-port part (an MP3 Trigger routed to S4) runs only with `navicore_aux_tx`.
> - `hcr_local_payload` pins what a save that moves `hcrDest` to a WCB does to a local fade: the ramp stops in that
>   pass, no StopWAV or restore goes out, and the local HCR is left at the ramp's level (`NaviCore.ino:5548-5552`).
>   Fade steps on a real wire stay NC-WP14's.
> - Not checked: the MP3 shadow's 20 after boot (it needs a restart; each device table sets its volume first). The new
>   read-back in `maestro_mesh_fanout_0_9` is weak on this bench: ch 0 of every slot is off at rest, so the target
>   sent and the value read are both 0.
> - Doc drift found in NaviCore, for D-NC36's docs commit: `docs/TROUBLESHOOTING.md:55` says the WCB-side remote
>   Maestro read has not shipped (it has: `mae_remote_read`); the comments at `NaviCore.ino:1435-1447` and `:1500`
>   still describe the emotion-4 Overload shortcut that WcbCmd 0.9 removed; the `#L20`/`#L21` comment (`:3678-3679`)
>   names GPIO15/GPIO17, while v2's S3/S4 TX are GPIO8/GPIO10 (`:183-186`); `hcrFormatWcbCommand`'s default branch is
>   commented unreachable, but fn 20 and 21 reach it (`:1551`); `rc_config.h:117` lists RA_SERIAL's ports as S3/S4
>   (S5 works too).

- Remote Maestro, servo-free: `ncdev.mae_remote_stream_exact`, `ncdev.mae_remote_verbs`, `ncdev.easing_repeat_frames`,
  `ncdev.mae_subroutine_msb` (should, D-NC23). Every frame is predicted from `WcbMaestro` and checked on W1S1 and the
  W2S1 tap.
- Remote read: `ncdev.mae_remote_read` (slot 2 is real Maestro 2: `;M2,getPosition,0` goes out, W2 answers `:MQR`,
  `[MAE:2]` prints later; also over the bridge).
- Local Maestro 1 (servo): `ncdev.mae_local_frames_readback`, `ncdev.mae_local_easing_timing`,
  `ncdev.knob_local_readback`; fixes `navicore.mae_cli_local`, which passes on `timeout` (D-NC15).
- Remote devices, each inside `config_guard(bench, 2)` with the device on a probe-wired W2 port:
  `ncdev.hcr_remote_verbs` (hcrDest already goes to W2), `ncdev.mp3_remote`, `ncdev.dfp_remote` (with the DEVICE
  fn 17 regression `7E FF 06 09 00 00 02 FE F0 EF`), `ncdev.wled_remote`. The expected bytes are the WcbCmd bytes the
  s15/s24 suites already assert for the same verbs on W2, so a NaviCore regression and a WcbCmd regression are told
  apart.
- Local devices (`navicore_aux_tx`, `DBG_WIRE` for exact bytes): `ncdev.hcr_local_payload`,
  `ncdev.local_device_bytes` (MP3, DFPlayer, WLED), `ncdev.serial_action_bytes`, `ncdev.cli_hcr_test_codes`. Each
  compares NaviCore's local bytes with W2's bytes for the same verb: the WcbCmd dual-compile rule (`WCB CLAUDE.md`
  rule 1) checked end to end for the first time.
- Aux routing: `ncdev.mesh_forward_burst`, `ncdev.bcast_out_opt_in`.

### NC-WP8 — SoftAP and WebSocket (`s45_navicore_wifi.py`, `ncwifi.*`, opt-in `navicore_wifi`)

`ncwifi.pc_joins_ws_ping` (join, DHCP gives 192.168.4.x with no default route, PONG; six PINGs in one frame give six
PONGs), `ncwifi.ws_parity` (PING, GET_CONFIG by hash, TRIGGER, TEST_ACTION, `?REC,LS` equal USB),
`ncwifi.ws_console_mirror`, `ncwifi.ws_multi_client`, `ncwifi.ws_line_framing`, `ncwifi.ws_utf8_and_latch`,
`ncwifi.ws_wizard_surface` (`?backup`, `?WDP,DUMP`, `?MGMT,PULL,2`, a relay-OTA ACK over the socket),
`ncwifi.mesh_coexist`, `ncwifi.ws_capture_slot` (low), `ncwifi.ws_ping_soak` (5 minutes, nightly only); with
`navicore_reboot` too: `ncwifi.ap_boot_lines`, `ncwifi.refuse_short_password` (a 3-character password: the REFUSED
line and no AP; restored; the AP is back).

### NC-WP9 — boot, reboot, failure (`s46_navicore_boot.py`, `ncboot.*`, opt-in `navicore_reboot`)

> **Status 2026-09-28: written and bench-verified** (`20260928-212848`, `-215752`: `banner_order`, `reboot_resets_ram_state`, `wcbs_see_reboot`, `roll_call_missing_board` and `sbus.boot_quiet` pass; the three `(should)` tests fail as designed, D-NC25, D-NC29, D-NC19; `bad_device_id` is attended. `mesh_reboot` no longer requires the `[RC] Remote REBOOT` line: it is printed 100 ms before the restart, which usually takes it). (`suites/s46_navicore_boot.py`). Under `navicore_reboot`:
> `ncboot.banner_order`, `reboot_resets_ram_state` (REBOOT and `#L02`), `wcbs_see_reboot` (with the healthy roll
> call), `new_peer_after_boot` (should, D-NC25), `roll_call_missing_board`, `mesh_reboot` (should, D-NC29),
> `boardtype2_mismatch` (should, D-NC19) and `sbus.boot_quiet`; `ncboot.bad_device_id` under `navicore_identity`
> (registered, off). Each skips while the recorder holds anything (`NaviCore.restart_blocker`, D33) and runs inside
> `nc_guard`; a test that saves what only a restart applies writes the snapshot back and restarts again however its body
> ended (`_put_back`). Where the code differs from the plan below:
> - The roll call covers only the floor 1..quantity (`NaviCore.ino:5343-5362`), quantity 1 here, so W2 is never in it,
>   and `ncmesh.deaf` stops a board's reception, not its heartbeats. The test raises the floor to the lowest unused id
>   (3) inside the guard, restarts, and restarts again on the snapshot; `_loadLearnedPeers` skips a learned id the floor
>   covers and saves nothing (`WCB_Client.cpp:1745-1774`), so NVS is untouched.
> - The map's 14 banner lines become up to 23 anchors read from `setup()` (`banner_anchors`): bauds, pin profile, SBUS
>   OUT, the SoftAP block and the mesh join from GET_CONFIG, App SHA256 from STATUS, the hook line from `#L90`. The bench
>   board runs the stock IDF bootloader (recorded, not asserted).
> - The USB port does not re-enumerate across a software restart or a USB-Serial/JTAG reset on this bench
>   (`results/builds/ncflash-logs`, session logs): noted, not asserted. A restart is proven by the uptime.
> - `new_peer_after_boot` waits out the 8 s grace, then W1's `?WDP,POLL` makes W1 and W2 advertise at once, instead of
>   reading 70 s of log (WCB_Client never solicits at boot, so their adverts otherwise come within 60 s).
> - `sbus.boot_quiet`: the stick is J4 (CH4; remote slots 2-7 only, Maestro 2 moves), the switch a tier whose own marker
>   lands on a wired port (SI's `;W2;S2HILSIB`), the button an unmapped matrix slot. Setting the easing at boot
>   (setSpeed/setAccel frames) is by design; only a setTarget frame on W1S1 fails it.
> - A banner's SoftAP line names the AP: `hil/checkpoint.py` `redact_text` now hashes it (NaviCore's lines in
>   session.log, failure tails), as ncflash's logs already did. GET_CONFIG's `wifiSsid` still reaches session.log in
>   clear: hashing it would change the redacted hash of every stored NaviCore snapshot a paused run must match.

`ncboot.banner_order` (the 14 lines of the map's `nc.boot.banner` in order; bauds, deviceId and quantity equal
GET_CONFIG; the bootloader kind recorded), `ncboot.reboot_resets_ram_state`, `ncboot.wcbs_see_reboot`,
`ncboot.new_peer_after_boot` (70 s of log; `(should)` per D-NC25), `ncboot.roll_call_missing_board` (W2 deaf across
the boot), `ncboot.mesh_reboot` (`(should)` per D-NC29), `ncboot.boardtype2_mismatch` (D-NC19), `sbus.boot_quiet`
(controller parked off-centre and a switch on a mapped position: no rc_trig, no marker, no slot-4 frame, `?MAE`
positions and errors unchanged). `ncboot.bad_device_id` stays behind `navicore_identity` (off).

### NC-WP10 — OTA (`s47_navicore_ota.py`, `ncota.*`)

> **Status 2026-09-28: written and bench-verified** (`20260928-212818`, `-212848`: every ticked test passes - the five unattended ones, `local_begin_abort_timeout`, `local_full_same_image` (two USB flashes, 161 s) and `relay_full_to_w2` (two W2 flashes through NaviCore, 940 s); `recovery_esptool` and `relay_full_via_w1` stay off). (`suites/s47_navicore_ota.py`). Unattended: `local_status_parse`,
> `local_nosession_errors`, `relay_target_nosession`, `navicore_as_relay_nonerasing`. Opt-in: `local_begin_abort_timeout`
> (`navicore_ota_erase`), `local_full_same_image` (`navicore_ota_full`), `relay_full_via_w1` (`navicore_ota_relay_full`,
> off), `relay_full_to_w2` (`ota_full_wcb2`, ticked, so it runs nightly beside its W1-relayed twin). Where the code
> differs from the plan below:
> - NaviCore's `?OTALOCAL` has no BAUD sub-command (the WCB's has), and `execCliLine` matches the `?OTALOCAL,` prefix
>   case-sensitively with its comma: a bare `?OTALOCAL` and `?otalocal,status` answer 'Unknown command' (a WCB prints
>   STATUS for both).
> - BEGIN's guards (family 0, size 0, the Next slot's size + 1 read from STATUS) refuse before any erase
>   (`navicore_ota.h:143-154`), so they are in the unattended `local_nosession_errors`; `_refused_begin` refuses to build
>   any other BEGIN, and `_relay_begin` any relayed one without its family (a missing family is 0, which W2 accepts).
> - The erase test's 4 KB land on the head of the inactive slot, which holds the rollback image since D45 (the
>   bootloader's fallback copy); `python -m hil.ncflash flash results/builds/navicore` writes it back. Both full-image
>   tests leave both slots holding the bench image.
> - The reaper test's window is REAPER_S - 0.5 to REAPER_S + REAPER_S/12 (2.5 s at 30 s): a rejected chunk that
>   refreshed the session would move it 20 s.
> - `relay_full_to_w2` relays only `results/builds/wcb-esp32-meshq`, and only when W2 runs its version (no `Code/bin`
>   fallback, unlike s20's `_image`).

Unattended, nothing erased: `ncota.local_status_parse`, `ncota.local_nosession_errors`, `ncota.relay_target_nosession`
(W1 relays DATA/END/ABORT with no session: ERR 0, `END: no matching active session`, ABORT OK 0),
`ncota.navicore_as_relay_nonerasing` (NaviCore relays BEGIN family 1 to classic-ESP32 W2: `[OTA:ACK,2,<s>,0,1]` on
NaviCore's USB; a bad CRC dropped; DATA with no session ACKed ERR 0). Opt-in: `ncota.local_begin_abort_timeout`
(`navicore_ota_erase`), `ncota.local_full_same_image` (`navicore_ota_full`: the bench image twice, ending on the
original slot), `ncota.relay_full_via_w1` (`navicore_ota_relay_full`), `ncota.relay_full_to_w2` (the existing
`ota_full_wcb2`).

### NC-WP11 — SBUS faults (`sbus.*`, needs INF8)

`sbus.failsafe_flag_freeze`, `sbus.lost_frame_flag_no_gate`, `sbus.failsafe_deferred_tap` and
`sbus.frame_stop_held_press` (both `(should)` per D-NC21), `sbus.sbus16_autodetect`, `sbus.lock_after_glitch`,
`sbus.prefix_ambiguity_raw`, `sbus.one_frame_dip`.

### NC-WP12 — record and replay (`s48_navicore_rec.py`, `ncrec.*`, opt-in `navicore_clip_write`)

`ncrec.record_save_list_rm` (record through TEST_ACTION `{"type":"record","cmd":"HILrec<n>"}`, three spaced W1S2
markers, stop and save, `?REC,LS` n=3, ranged download, RENAME refuses to overwrite, RM), `ncrec.play_timing_markers`,
`ncrec.stop_semantics`, `ncrec.replay_gate`, `ncrec.replay_interpolation_remote` (a clip uploaded with EDITBEGIN/EV/END
holding slot-4 keyframes: speed and accel 0 first, then a dense ramp, the last keyframe at the end),
`ncrec.calib_drops_take`, `ncrec.backstop_60s`.

### NC-WP13 — the config tool with the board (L2) and attended (L3)

The L2 and L3 tables of §5.4, each L2 spec wrapped in `nc_guard` by its harness test.

### NC-WP14 — NaviCore's own pins (blocked: hardware)

Designed now so it runs the day a probe is wired (D-NC37): `ncwire.aux_bytes`, `ncwire.soft_tx_integrity` (200 long
lines on S4/S5 under mesh and SBUS load: 0 RXERR, 0 mismatches; the map's finding that S4/S5 bit-bang with interrupts
on), `ncwire.s3_console_quiet`, `ncwire.tx_interleave`, `ncwire.rx_monitor_bcast_in`, `ncwire.sbus_out_tee`
(a hardware channel at 100000 8E2 INV RXONLY: bytes equal `#L13`, period ~9 ms), `ncwire.maestro_bus_tap`. The harness
side: link keys for NaviCore ports (`N20S3`, `N20S4`, `N20S5`, `N20MAE`, `N20SBO`), a discovery stimulus through
TEST_ACTION serial or `;W20,;s<n>`, bauds from GET_CONFIG `auxBaud`, and the `port_devices` pattern widened
(`links.py:25-46`, `:532-554`, per the map).

## 5. The config tool (`NaviCore/config_tool`)

`config_tool/index.html` is about 20,700 lines with two inline script blocks, plus `flasher.js` and `serial-hub.js`
(`index.html:3371-3374`, per the map). It has no build step and no test: `pages-deploy.yml` publishes `main` to
production with no syntax check, and Intellex ships the same bytes. The map lists 81 tool features, 32 of them high
risk; none is tested today.

### 5.1 Four layers

| Layer | What runs | Board | Where it runs |
|---|---|---|---|
| **L0** | Node only (`node --test`): syntax, cross-file constants, key coverage, the Intellex byte identity, the hub protocol, and the config round trip through the page's own pure functions | none | CI and `run.py` (`nctool.static`, `nctool.unit`) |
| **L1** | Playwright; the real page with a fake `navigator.serial` backed by an in-Node NaviCore **emulator** | none | CI and `run.py` |
| **L2** | Playwright; the same fake port backed by the **harness's own COM5 handle** through the bridge (a "pipe") | the real NaviCore (or W1 for Via-WCB specs) | `run.py`, unattended |
| **L3** | Playwright with **real Web Serial** (Chrome's grant in `.profiles/navicore`) and esptool-js | the real NaviCore | `run.py`, attended |

### 5.2 How Chrome gets the board: the existing pattern, and the pipe

The Wizard's board specs work like this: `tests/wizard/playwright.config.js` fixes the origin at
`http://127.0.0.1:8778` because Web Serial grants are stored per origin (:6-8), and `serve.js` serves the WCB repo
root there (:11). Each bench device has its own persistent Chrome profile, `.profiles/<device>`, holding only that
board's grant (`lib/fixtures.js:15-26`). For a board test, `hil/wizard.py` closes the harness's handle on the
device (`run_wizard_test`, :201-202), starts a `Bridge` (`hil/bridge.py`: `/context`, `/note`, `/config`,
`/wire/mark|received|expect|send`) so the spec can reach the probes and other boards, runs
`node cli.js test --grep '(^|\s)<id>(\s|$)'` (:198), and afterwards takes the port back (`_reacquire`, :93-113). The
spec finds its port among `navigator.serial.getPorts()` by the VID/PID the harness read (`lib/wizard.js:26-33`); a
first run needs a person at Chrome's port dialog (`authorize`, :43-88), and `WIZ_NO_AUTHORIZE=1` turns that into a
skip. `.profiles/` holds only `no-board` and `wcb1` today.

For NaviCore that pattern alone does not work unattended this week:

- There is no `.profiles/navicore` grant, and making one needs Greg at Chrome's chooser.
- Chrome asserts DTR/RTS when it opens a port, and on NaviCore's native USB that resets the board every time
  (measured and documented at `index.html:4482-4491`: `Reset reason: 11 - USB peripheral`).
- While Chrome holds COM5 the harness cannot log or redact NaviCore's traffic, and the CONFIG line the tool echoes
  into its terminal (`appendTerminalLine('< ' + line, 'rx')`, `index.html:9512`) carries the passwords.
- NaviCore and the SBUS controller are both ESP32-S3 USB-Serial/JTAG ports, so a VID/PID match cannot tell them apart
  (both almost certainly report the chip's default 303A:1001; not read on the bench).

So L1 and L2 use **a fake `navigator.serial`**, installed with `context.addInitScript` before the page's own
scripts, the way Intellex does it without forking the page (`intellex_shim.js:1-30`; it must use
`Object.defineProperty`, because `navigator.serial` is a getter on `Navigator.prototype` and a plain assignment throws
under strict mode, `:282-300`). The port object implements `open`, `close`, `readable`, `writable`, `setSignals`
(recorded, never applied to COM5), `getInfo` (`{usbVendorId: 0x303a, usbProductId: 0x1001}`) and `forget`;
`requestPort()` and `getPorts()` return it. Bytes flow through `page.exposeBinding('__hilSerialWrite', ...)` into
Node, and back by a 20 ms poll that calls `window.__hilSerialPush(b64)`:

- **L1:** Node hands them to `lib/navicore/emulator.js`.
- **L2:** Node forwards them to the bridge's new `/serial/write`, and polls `/serial/read` since a mark (INF7): the
  harness keeps COM5 open the whole time (no reset, full logging, the redaction filter applies), and `nc_guard` wraps
  the harness test. A Via-WCB spec pipes W1's COM6 instead; the tool then auto-detects the bridge by itself.
- **L3** keeps the existing real-Web-Serial flow, with `device="navicore"`.

An alternative to the binding is `page.routeWebSocket` acting as Intellex's `/_link` server, which would let phase 3
reuse Intellex's own shim; the binding is simpler and enough here.

### 5.3 The emulator and the fixtures

`lib/navicore/emulator.js` answers like the firmware, each branch citing the code it copies:

- **USB mode:** PING to `{"type":"PONG","version":...}` (`NaviCore.ino:3835-3843`); GET_CONFIG to the fixture;
  SET_CONFIG applied per key and branch as `rc_config.h` does, ACKed with the saveId echo (`:3953`), with switches for
  NACK, timeout and a stale saveId; GET_CMDLIB, GET_CMDLIB_META, SET_CMDLIB with FNV-1a; START/STOP_MONITOR streaming
  a scripted PWM_UPDATE sequence; CALIB; RESET_DEFAULTS; TEST_ACTION; TRIGGER (the `[TRIGGER]` line, `rc_trig`, the
  ACK); GET_WCB_STATUS (dense), GET_MESH_STATS pages, GET_WCB_SEQ/SEQVAL; `?REC,LS` in both marker forms; ranged
  `?REC,EDITLOAD` with `EVB`; EDITBEGIN/EV/END with `[CLIPUL:ACK,i]`; `?MAE` markers; the `?OTALOCAL` state machine
  (`[OTA:BEGIN,START]`, `[OTA:BEGIN,OK,0]`, `[OTA:ACK,off]`, NAK, `[OTA:END,OK]`) with coalesced markers and lost ACKs
  on demand.
- **Via-WCB mode:** accepts `;w20,<json>`, answers `{"sys":1,...,"id":20}` (the mesh PONG shape,
  `rc_telemetry.h:2186-2195`), reassembles `{"f","of","sid","s"}` envelopes (5 s timeout, 192 parts), sends a
  fragmented CONFIG, streams rc_hb/rc_ch/rc_trig, answers relay OTA with `[OTA:ACK,src,session,offset,status]`.
- **Doorway mode:** a "direct" port that answers PING with the relayed PONG shape, for `nctool.doorway_pong_misdetect`.
- **Faults:** a late PONG, dropped lines, UTF-8 split across chunks, NetworkError or BreakError on read.

The emulator is kept honest by `nctool.emulator_contract` (L2): the harness sends its read-only request set (PING,
GET_CONFIG, GET_CMDLIB_META, GET_WCB_STATUS, GET_MESH_STATS, `?REC,LS`, `?OTALOCAL,STATUS`) to the real NaviCore and to
the emulator and compares reply types and key sets, never values. A firmware change the emulator misses fails a test.

**Fixtures** (`tests/wizard/fixtures/navicore/`, D-NC11): `config.bench.json` is the bench's GET_CONFIG with
`wcbNetwork.password`, every `wcbProfiles[].password`, `wifiPassword` and `wifiSsid` replaced by fixed `HIL...`
strings. `tests/wizard/tools/scrub_nc_config.py` produces it from the live config (read through the harness, in
memory) and refuses to write if any original secret appears anywhere in its output. Also: a few small clips, a
Maestro Control Center XML, a CSV export, PWM_UPDATE scripts, and WCB_STATUS/WCB_META/MESH_STATS pages.

**Other page dependencies, mocked in L1** with `page.route`: `api.github.com` (the firmware list, 60 unauthenticated
requests an hour), the esptool-js CDN, and the cloud-backup Worker (never the live KV; the page embeds an upload
token, which is never logged). Each page opens with the HTTP cache off through CDP, as `openWizard` does
(`lib/wizard.js:6-22`, after a stale script cost a run on 2026-09-23), and `page.clock` drives the 10 s keepalive, the
4 s stale monitor, the 5 s fragment expiry and the 12 s save watchdog. No spec reads `#terminal-output` wholesale (it
holds the CONFIG echo), and board specs keep Playwright's trace, screenshot and video off; the harness hands L2 specs
only hashes and lengths, never a token, as `remote_pull.spec.js:1-7` does.

### 5.4 The specs

**L0** (`tests/wizard/unit/navicore/*.test.js`; the harness test `nctool.static`):

1. Syntax: every inline `<script>` of `config_tool/index.html` compiles under `new vm.Script` (what `jscheck.js`
   does, without depending on `~/tools`); `node --check` on `flasher.js` and `serial-hub.js`.
2. Constant pairs from `CONFIG_SCHEMA.md` §6 (`:254-290`): tap tiers 4, knob outputs 10, Maestro slots 8, WLED slots
   4, WCB profiles 6, `FRAG_CHUNK_BYTES` 80, `FRAG_MAX_PARTS` 192, receive parts 512, `FRAG_MAX_ENV_BYTES` 187,
   `FRAG_TIMEOUT_MS` 5000, `BULK_CHUNK_RAW` 96, `BULK_MAX_CHUNKS` 512, device id 20, the `DBG_*` bits against
   `DEBUG_CATEGORIES`; and the map's extra pairs: 5 actions per tier, note 19, cmd 95, label 23, password 39, template
   47, 6 smoothing profiles. Each value is read by regex from `rc_config.h`, `rc_telemetry.h`, `NaviCore.ino` or the
   WCBClient headers, and from `index.html`; a pair that cannot be found fails, so a rename cannot switch the check off.
3. Key coverage: every key `rcConfigToJSON` writes (`rc_config.h:1211-1480`) is read by `applyConfig`
   (`index.html:9632`).
4. Intellex identity: `Intellex/src/webui/{index.html,flasher.js,serial-hub.js}` equal the tool's byte for byte
   (skips without the Intellex repo, as in CI).
5. The hub protocol: the tool's and the Wizard's `serial-hub.js` agree on the lock and channel names and the message
   types (hello, claim, yield, tx, rx, state, bye); their other differences are expected (the map's
   `nct.hub.shared_port`).
6. The Intellex shim's contract, statically: `connectDirect`, `onViaWcbToggle` and the ids `btn-connect`,
   `btn-fw-flash`, `btn-fw-wipe`, `btn-fw-ota`, `btn-fw-ota-wcb` exist.
7. **The config round trip, in node** (`nctool.unit`). The tool has no separate parser module like the Wizard's
   `parser.js`, so `unit/navicore/extract.js` lifts named top-level functions and constants out of the inline script
   by brace matching into a `vm` sandbox, and fails loudly when a name is missing, so a rename cannot silently skip a
   test. The pure ones need nothing else: `_stringifyStable` (`index.html:16526`), `_diffConfigBranches` (`:16547`),
   `_diffMappings` (`:16581`), `_fragChunks` (`:5556`, with `_fragEscBytes` and the `FRAG_*` constants),
   `ncEncodeCommand`/`ncDecodeCommand` (`:13445`, `:13516`, with the manifests as the catalog), `_seqValueToLines`
   (`:13106`), `parseCsv` (`:19591`) and `_cfgExtractConfig` (`:20261`). Against the bench fixture: a config diffed
   with itself is empty; one edit ships exactly its branch; a deleted button ships `{}` (the firmware's per-key clear,
   `rc_config.h:1580-1603` per the map); a baseline-only branch ships `null` (pinned, and the reason D-NC22 matters);
   every fragment of a corpus with quotes, backslashes, CJK, lone surrogates and control characters is at most 187
   UTF-8 bytes as an envelope and the parts join back byte-exact; every library command encodes and decodes to
   itself; `_cfgExtractConfig` takes the wrapper and a bare config and rejects a Wizard export and `{}`. The
   DOM-bound half of the round trip (`applyConfig`, `readActionFromFid`, `saveConfigToBoard`) is L1's.

**L1** (`tests/wizard/specs/navicore/*.spec.js`): the ~55 `nctool.*` specs in the config-tool table of §2. Every spec
ends with no uncaught page error. The ones that assert today's known defects are `(should)` specs: the doorway PONG
(D-NC30), the no-op Apply (D-NC32), the clip-restore mode (D-NC33), the wipe text (D-NC34), the two-tab saveId
(D-NC35).

**L2** (harness wrappers in `s49_navicore_tool.py`, each inside `nc_guard`):

| Spec | What it proves |
|---|---|
| `nctool.board_connect_config` | the tool connects to the real board, applies its real CONFIG, and a Save right after sends nothing |
| `nctool.board_save_one_field` | a serial-port label typed in the Serial Ports tab and saved is the only change in GET_CONFIG |
| `nctool.board_test_action_wire` | a row's Test button on `;S2HIL<n>` puts those bytes on W1S2 (read through `/wire/expect`) |
| `nctool.board_live_grid` | the harness moves the rx stick (CH1) through a new `/sbus` bridge route; the grid shows the exact value |
| `nctool.board_via_wcb` | through W1's port the tool detects Via WCB, applies the bridged CONFIG and lands a fragmented one-field save |
| `nctool.board_clips_list`, `nctool.board_cmdlib_load` | the clips list and "Load from NaviCore" against the real board (read-only) |
| `nctool.emulator_contract` | the emulator's replies have the real board's shapes |
| `nctool.board_ota_usb_same_image` | opt-in `navicore_ota_full`: the tool's "Update over USB" with the bench image, served by `page.route` |

**L3** (attended, opt-in `navicore_webserial`): `nctool.webserial_connect_reset` (the real open resets NaviCore and
the connect still succeeds; the grant is confirmed by the PONG, since a VID/PID match cannot separate NaviCore from the
controller), `nctool.webserial_flash_same_image` (esptool-js writes bootloader, table and app as the tool does; the
config survives, which is what `nct.fw.wipe_misleading` predicts).

### 5.5 Where the specs live: `tests/wizard` (D-NC10)

Recommended: inside `tests/wizard`, in NaviCore subfolders: `specs/navicore/`, `lib/navicore/`, `unit/navicore/`,
`fixtures/navicore/` and `tools/`, with ids prefixed `nctool.`. Rather than a new `tests/navicore_tool/`, because:

1. **One origin.** The shared-hub spec needs the tool and the Wizard on the same origin (Web Locks and
   BroadcastChannel are per origin), which is also how production serves them (NaviCore's `BUILD_AND_RELEASE.md` §6). Keeping
   port 8778 keeps every existing Web Serial grant valid. `serve.js` gains a `/NaviCore/` alias to the sibling repo
   (and `/Intellex/` later), so the page loads from `/NaviCore/config_tool/index.html` and its relative `cmdlib/`
   fetches work.
2. **One Playwright install.** A spec outside `tests/wizard` resolves `@playwright/test` from its own folder upward
   and would not find `tests/wizard/node_modules`; a second install risks two copies of the package in one run.
3. **One CI job and one bridge** (`wizard-tests.yml`, `hil/wizard.py`). `testDir: './specs'` already recurses; the
   `unit` script becomes `node --test "unit/*.test.js" "unit/navicore/*.test.js"`.

The cost is a folder named "wizard" that holds more than the Wizard. Rename it to `tests/web` when Intellex's specs
join (`WIZARD_TESTS` in `hil/wizard.py:23`, the workflow's paths, and the untracked `.profiles/` folder move with it).

**Harness wrappers:** `suites/s49_navicore_tool.py` registers each `nctool.*` id the way `s30_wizard.py` does:
`run_wizard_test(bench, id, device=None)` for L1, `device="navicore", pipe=True` for L2, `device="navicore"` for L3.

**CI:** `wizard-tests.yml` checks out this repo and `greghulette/NaviCore` as siblings inside the workspace (two
`actions/checkout` steps with `path:`, which must stay under the workspace, so the WCB checkout moves into a
subfolder too), and the L0 and L1 NaviCore specs run there; the L0 tests skip cleanly when the sibling is missing. NaviCore's
own `pages-deploy.yml` gets the L0 syntax gate before it publishes (D-NC12).

## 6. Out of scope, and the risks

### 6.1 Out of scope: what this bench cannot exercise

| What | Why | What would unblock it |
|---|---|---|
| Bytes on NaviCore's own pins: aux S3-S5 TX and RX, SBUS OUT (J2), the Maestro bus (J3) as bytes | No probe header is free (all ten serve W1/W2, `results/links.json`) and nobody can wire one this week | A third probe (D-NC37); NC-WP14 is written for it |
| The status LED (GPIO48; whether the module is a DevKitC-1 v1.1, which wires its RGB LED to GPIO38) | No optical sensor on the bench | A photodiode or a camera; Greg reading the module revision (D-NC28) |
| The WCB HW 3.2 pin profile (boardType 1) | Needs a second ESP32-S3 board running NaviCore | A spare HW 3.2 board |
| Cold power-up, and the custom bootloader's auto-retry | Cannot power-cycle remotely; a cold boot may land the S3 in ROM download mode | A switchable USB hub, with Greg present |
| Real HCR, MP3 Trigger, DFPlayer and WLED devices on NaviCore's ports | None attached; unknown what, if anything, is on J4-J6 | Byte-level emulation only: through W2's probe-wired ports (remote) and `DBG_WIRE` (local) |
| A real receiver's failsafe and SBUS-16 | The controller sends SBUS-24 with flags 0x00 only | Emulated by the controller's test verbs (INF8) |
| The legacy NVS migration (`rcfg`), a LittleFS mount failure | Unreachable without seeding NVS or breaking the filesystem | A host test of `rc_config.h:2394-2620` (per the map) |
| The v1 clip migration (136-byte stride), a truncated clip file | Needs crafted `.ncr` files | A host test of `navicore_record.h` |
| Intellex as a concurrent WebSocket client | Phase 3 of the week | Phase 3 |
| The live cloud-backup Worker, GitHub, the esptool CDN | Must not be touched by tests | Mocked in L1; real only in attended L3 |

### 6.2 Risks and how the plan handles them

| Risk | Mitigation |
|---|---|
| NaviCore stuck in ROM download mode or a boot loop, with nobody to replug it | Flash by `?OTALOCAL` only: the image is verified before the boot pointer moves and any failure leaves the running app; never a cold power-cycle; the recovery ladder (INF4) proven by `ncota.recovery_hard_reset` before the first flash; the known-good image kept in `results/builds/navicore`. The esptool path goes through the S3's ROM and USB-Serial/JTAG, so it works even when the app is broken. If USB itself is dead, stop NaviCore work and wait for Greg. |
| A restore fails and the droid's own config (the bench NaviCore drives the dome) is left changed | `nc_guard` verifies byte-identity after every restore and fails loudly; the exact snapshot is on disk per run, plus a week-start copy (D-NC3); the resume checks NaviCore; `nccfg.guard_selftest` runs first; identity fields are never changed unattended (D-NC14). |
| Credentials leak into a committed file or a report | The log filter and `redact_text` extension (INF3); scrubbed fixtures from a scrubber that refuses to write a leak (D-NC11); `results/` is gitignored; Playwright traces and screenshots off for board specs; specs are given hashes, never tokens. |
| The SBUSController flash fails (no OTA path) and every `sbus.*` test skips | Done after the must-have packages; app-only esptool write; rollback image kept; if the permission check refuses the flash (as W1/W2's did on 2026-09-24), NC-WP11 waits. |
| Servo motion and wear | Allowed this week; every such test is in `SERVO_TESTS`; most engine tests use remote slots 3-8 and move nothing. SBUS OUT re-emits every controller move to whatever is on J2, which nothing records (`hil/servos.py:14-15`). |
| Sound or light if something is attached to J4-J6 | `navicore_aux_tx` gates every local-device route; ASCII already reaches those pins on every run (`navicore.serial_route_dbg`). |
| NaviCore tests disturbing WCB tests in the same full run | NaviCore suites run after the WCB suites (s40+); every deafening restores in `finally`; mesh-counting tests keep the tracker #76 timing rule for NaviCore's 30 s and 60 s reports. |
| The emulator drifts from the firmware, so L1 passes while the tool fails on the board | `nctool.emulator_contract` (L2) compares reply shapes against the real board every run. |
| Flash wear from guarded writes | Each SET_CONFIG rewrites ~14 KB of the 128 KB LittleFS partition (`partitions.csv`, 0x3D0000); the guard skips no-op restores; about 60 writes per full run is negligible for NOR flash over a week. The relayed full OTA (~12 min a pass) stays out of nightly runs (D-NC14). |
| `(should)` tests fail every night until their fixes land | The nightly summary in `HIL_WEEK_DECISIONS.md` lists them separately from regressions. |
| A NaviCore push reaches users (the tool's Firmware tab reads `firmware/` on `main`) | Pushed only after the NaviCore suite passes on a build of exactly those trees (D-NC6). |
| The plan is larger than the week | The cut line (D-NC41); the no-board config-tool work runs while the bench is busy. |
| Bench contention (the WCB and Intellex plans, Greg's IDE, `Intellex.exe` holding a port) | `ListAgents` before any bench work; one COM owner; an "Access is denied" means another program first (`HIL_TEST_AUDIT.md` §6). |

## 7. Decisions to take

Greg is away and has delegated these. Each has a recommendation; once taken, it goes into
`docs/HIL_WEEK_DECISIONS.md` with how to undo it. D-NC1 to D-NC15 and D-NC37 to D-NC41 are about the work;
D-NC16 to D-NC36, D-NC42 to D-NC48 and D-NC56 to D-NC60 are behaviour findings, each with the `(should)` test
that pins it.

### 7.1 Process and infrastructure

| # | Decision | Recommendation | Why | Undo |
|---|---|---|---|---|
| D-NC1 | Test names and suite numbers | New id areas `nccfg`, `ncengine`, `ncdev`, `ncmesh`, `ncboot`, `ncota`, `ncwifi`, `ncrec`, `nctool`, `ncwire`; `navicore.*` and `sbus.*` stay. Suites s40-s49 reserved for NaviCore. | The runner and GUI group by the id's first segment (`hil/runner.py:167-168`); one `navicore` area would hold ~250 tests. s40+ cannot collide with the WCB plan's new suites. | Rename ids (checkpoints refer to them). |
| D-NC2 | May unattended runs write NaviCore's config? | Yes, only inside `nc_guard`, and only after `nccfg.guard_selftest` has passed on the bench. | Most of the command surface's 18 high-risk features are config writes and cannot be tested read-only; the guard proves byte-identity after each test. | Mark the `nccfg` writers opt-in. |
| D-NC3 | Where the exact snapshot lives | `results/<run>/navicore_snapshot.json` per run, plus a one-time `results/navicore_config_week_start.json`, both gitignored; a redacted hash in the checkpoint; the resume restores from it. | The bench NaviCore is the droid's own dome controller; a killed run must be able to put its config back. `session.log` already holds the same line. | Delete the files. |
| D-NC4 | How NaviCore is flashed | `?OTALOCAL` over the harness's COM5 handle; esptool (app at 0x10000 plus an otadata erase) only as recovery; never `arduino-cli upload`. | Brick-safe by construction, no download mode, keeps the custom bootloader. | None needed. |
| D-NC5 | Credentials in logs | Filter NaviCore and SBUS lines in `Bench.log` (both directions); extend `redact_text` with the JSON password keys; scrub `results/20260922-095852/report.md`; specs never dump the terminal pane; traces off for board specs. This departs from the WCB policy, where `session.log` keeps `?EPASS` raw; the WCB plan may adopt it too. | Greg's instruction that credentials are never logged; the guard would otherwise write the password on every restore. | Remove the filter. |
| D-NC6 | This week's NaviCore image, and pushes | Build from NaviCore HEAD plus its uncommitted working tree, the WCBClient working tree (1.17.1, F13) and the HIL hooks; run the NaviCore suite on it; then push WCBClient master first and commit and push NaviCore main (standing OK). If anything regresses, keep it all local. | A CI build of today's `main` would undo tracker #7, #31, #42 and #65 on the bench and lacks F13; the library-source rule (NaviCore `CLAUDE.md` rule 5) orders the pushes. | Revert the commits; earlier bins stay in git history. |
| D-NC7 | Firmware hooks for testing | (a) An `App SHA256` line in `?OTALOCAL,STATUS` and the boot banner, in production. (b) `DBG_WIRE` and the `#L90`-`#L93` fault verbs only under `NAVICORE_HIL_HOOKS`, off in CI. | (a) The DTG only changes on commit, so today two images look the same. (b) Testability without shipping fault injectors. | Drop the flag; the SHA line is harmless. |
| D-NC8 | SBUSController test verbs | Add the five RAM-only verbs of INF8; flash COM4 locally with esptool; keep the change on a local branch; do not push. | Failsafe is the most safety-relevant untested path, and the controller cannot produce it. Pushing its `main` publishes firmware. | Flash the fae0af6 build back. |
| D-NC9 | How board-backed tool specs reach NaviCore | Through the harness pipe and a fake `navigator.serial` (L2); real Web Serial only in attended L3. | Unattended, no reset per open, logging and redaction stay on (§5.2). | Run L3 only. |
| D-NC10 | Where the tool's specs live | In `tests/wizard`, in `navicore/` subfolders, ids `nctool.*`; `serve.js` aliases `/NaviCore/`. Rename the folder to `tests/web` when Intellex's specs arrive. | One origin (the shared-hub spec, existing grants), one Playwright install, one CI job (§5.5). | Move the subfolders. |
| D-NC11 | A committed config fixture | Yes: the bench config with every password and the SSID replaced, produced by a scrubber that refuses to write if an original secret remains. | L1 needs a realistic config; a hand-made one would miss real shapes. | Delete the fixture. |
| D-NC12 | CI for the tool | NaviCore's `pages-deploy.yml` runs the L0 syntax check before publishing; the WCB repo's `wizard-tests.yml` checks out NaviCore and runs L0 and L1. | Production and Intellex ship the page; today nothing checks it before publishing. | Remove the steps. |
| D-NC13 | Command-library write tests with no library stored | Skip them. | There is no delete verb: an empty store reads META 0/0, but the smallest library that can be written is `{}`, so "nothing" cannot be restored. | Add a delete verb and test it. |
| D-NC14 | Opt-ins this week | On: `navicore_reboot`, `navicore_wifi` (skips unless a spare adapter keeps the PC online), `navicore_nvs`, `navicore_clip_write`, `navicore_aux_tx`, `navicore_ota_erase`, `navicore_ota_full` (local passes only), `navicore_fault` (after the hook image). Off: `navicore_identity` (persisted deviceId, channel or password changes), `navicore_ota_relay_full` (run once by hand), `navicore_webserial` (attended). Never change NaviCore's mesh channel, deviceId or password persistently while nobody is at the bench. | Software restarts are safe (no strap re-sample); an identity change takes NaviCore off the mesh until restored over USB. | Untick in `bench.json` or the GUI. |
| D-NC15 | Test bugs and a health check | Fix `s21:312` (`mp3`/`dfp`/`wled` should be `mp3Dest`/`dfpDest`/`wledSlots`), the vacuous `wdp.dump_fields` (`s18:1172`) and `navicore.mae_cli_local` accepting `timeout`; add `navicore.bench_health`, which FAILS when NaviCore sees no SBUS or its Maestro 1 does not answer. | A dead SBUS input or Maestro today reads as skips for weeks (the Maestro skipped in every run until 2026-09-22). | Revert the test changes. |
| D-NC37 | A probe for NaviCore's pins | Ask Greg to wire a third V2.4 probe: S1 (hardware) taps SBUS OUT at 100000 8E2 INV RXONLY; S2 (hardware) NaviCore S3 at 115200; S3/S4 NaviCore S4/S5 at 9600; S5 the Maestro TX tap at 57600 (hardware channel, never in the same test as S1 and S2). Wire GND and the data lines one by one, 5 V off: NaviCore's J3-J6 are GND, 5V, RX, TX and the probe's headers 5V, GND, TX, RX, so a straight 4-pin cable shorts 5 V to GND. | The only way to test the local transports' bytes, the soft-TX finding and SBUS OUT. | None (hardware). |
| D-NC38 | Attended opt-ins already ticked that touch NaviCore | Keep `navicore_clip` and `sbus_reset` on for the nightly runs; `wifi_pc`, `nvs_erase`, `mesh_password` and `ota_wrong_chip` are the WCB plan's call. | `sbus_reset` has passed every opt-in run since 2026-09-24; `navicore_clip` only replays an existing clip, and servo motion is allowed. | Untick them. |
| D-NC39 | Which findings get fixed this week | Firmware: D-NC16 to D-NC22, D-NC25 to D-NC27, D-NC29. WcbCmd: D-NC23. Tool: D-NC30 to D-NC35. Each only after its `(should)` test exists and fails, and each bench-verified before it is pushed. | Small, well-understood changes with tests to prove them; the rest wait for Greg. | Revert per commit. |
| D-NC40 | NaviCore's Maestro slots | Never change a slot's device or type; use the existing remote slots 3-8 as the observation window. | W1 and W2 auto-add proxies from NaviCore's advert and never evict them (WCB `CLAUDE.md` rule 5), so a changed device leaves stale proxies. | None needed. |
| D-NC41 | Priority and the cut line | Must: NC-WP1, 2, 3, 4, 6. Should: NC-WP5, 7, 8, 9, 10. Could: NC-WP11, 12, 13. NC-WP14 waits for hardware. | Most risk per hour first; the no-board tool work fills bench-busy time. | Re-order. |

### 7.2 Behaviour findings: what the `(should)` tests assert

| # | Finding (from reading the code; not bench-run unless a test says so) | Recommendation | Test |
|---|---|---|---|
| D-NC16 | USB `RESET_DEFAULTS` is RAM-only and skips `applyConfigSideEffects`, unlike the mesh path; any later save then persists the whole default block, the compile-time mesh password, deviceId 20 and boardType 0 included (`NaviCore.ino:3989-3992`; `rc_telemetry.h:1538-1546`, per the map). The tool's comment that the mesh has no RESET_DEFAULTS branch is stale (`index.html:17010`, per the map). | Keep the network identity through a reset (`wcbNetwork`, `wcbProfiles`, `boardType`, the `wifi*` fields) on both paths, run the side effects on USB too, and fix the comments. | `nccfg.reset_defaults_keeps_identity`, `ncmesh.bridged_reset_keeps_identity` (the mesh path); `ncmesh.bridged_reset_defaults` pins the side effects |
| D-NC17 | An unrebooted password change (or a reset) splits OTA auth and ACKs, RTERM and WcbMgmt, which read the live value, from the ETM stack, which keeps the boot copy. | Those paths use the boot-time copy until a reboot, like the ETM stack. | `nccfg.mesh_creds_live_split` |
| D-NC18 | A bridged SET_CONFIG strips deviceId, MAC octets, password and quantity but not `channel` or the `wifi*` fields (`rc_telemetry.h:1137-1156`). | Strip them too; the tool already strips channel. | `ncmesh.bridged_set_config_strip` (sends `wifiEnabled`) |
| D-NC19 | `boardType` is stored unchecked; 2 gives the v2 pins but advertises "WCB 3.2". | Clamp to 0-1 on input. | `ncboot.boardtype2_mismatch` |
| D-NC20 | TEST_ACTION answers `ok:true` for an action the executor then skips (a disabled destination, an invalid slot, an unconfigured WLED id, a bad port); the tool never reads the ACK. | `ok:false` with a reason, and the tool shows it. | `ncengine.test_action_skipped_not_ok` (and the skip lines in `ncengine.test_action_matrix`), `nctool.test_action_button` |
| D-NC21 | A deferred tap still fires during failsafe, and a press in flight when frames stop resolves once they return (`NaviCore.ino:2770-2783`, `:2356-2390`, per the map). | Failsafe and frame loss cancel any pending tap or hold; the press must be made again. | `sbus.failsafe_deferred_tap`, `sbus.frame_stop_held_press` |
| D-NC22 | A null or empty `hcrDest`/`mp3Dest`/`dfpDest` object enables the device (serial S3, or WCB 2). | Read it as off. | `nccfg.dest_null_hazard` |
| D-NC23 | Subroutine numbers 128-255 go out unmasked (`restartScript`, `subParam`, inbound `;M<dev>,<n>`), a command-range byte inside a Pololu frame. Cross-repo: `WcbMaestro` accepts up to 255. | Refuse n > 127 in WcbCmd (both firmwares; push WcbCmd first, WCB rule 1). | `ncdev.mae_subroutine_msb` and a WCB twin |
| D-NC24 | S4/S5 transmit bit-banged with interrupts enabled (EspSoftwareSerial 8.1.0 defaults), while ARCHITECTURE §8 says they are masked. | Fix the docs now; change firmware only after a wire measures it (RMT TX, as WCB rule 13). | `ncwire.soft_tx_integrity` (blocked) |
| D-NC25 | The new-peer grace is 8 s (`NaviCore.ino:107`) but a WCB advertises every 60 s, so after a NaviCore boot every present WCB fires the alert and the peer actions. | Boards already online in the ETM table when the grace ends do not fire. | `ncboot.new_peer_after_boot` |
| D-NC26 | A reassembled inbound command over 200 characters is cut silently into `RemoteCliMsg.cmd[200]` or `SerialFwdMsg.text[201]` (`NaviCore.ino:90`, `:327`). | Refuse it with a log line; never truncate. | `ncmesh.long_command_truncation` |
| D-NC27 | A bridged WCB_SEND answers `ok:true` whatever `send()` returned (`rc_telemetry.h:1519-1524`: `ok = true` at `:1523`), and a fragmented one is dropped with no ACK. | Report the send result; handle and ACK the fragmented form. | `ncmesh.bridged_wcb_send_findings` |
| D-NC28 | The status LED is driven on GPIO48 on both profiles; a DevKitC-1 v1.1 has its RGB LED on GPIO38, which is S5 TX on the v2 profile. | No change until Greg reads the module revision. | none (out of scope) |
| D-NC29 | A mesh REBOOT restarts inline on the Core-0 receive callback after 100 ms, with no JSON ACK (`rc_telemetry.h:2405-2409`). | ACK, then restart from `loop()` once the queue is quiet (the WCB's rule 11). | `ncboot.mesh_reboot` |
| D-NC30 | The tool accepts any PONG, so connecting over USB to a doorway WCB is taken for a direct link and USB OTA stays enabled (the Intellex shim works around it, `intellex_shim.js:411-437`). A relayed PONG carries `sys` and `id` (`rc_telemetry.h:2189`); a direct one does not (`NaviCore.ino:3841-3843`). | Treat a PONG with `sys`/`id` as relayed: switch to Via WCB and gate the buttons. | `nctool.doorway_pong_misdetect` |
| D-NC31 | Refresh discards unsaved edits without asking. | Ask when there are unsaved edits. | `nctool.refresh_overwrites_edits` |
| D-NC32 | A no-op Apply rewrites the config: `skipRunning` true becomes 1, an unknown action type becomes `wcb_unicast`, knob defaults are added. | A no-op Apply changes nothing. | `nctool.noop_apply_every_editor` |
| D-NC33 | Clip restore never sends the clip's mode, so the restored clip takes whatever mode was last loaded. | Carry the mode (an `EDITBEGIN` argument). | `nctool.clip_restore` |
| D-NC34 | "Full Wipe & Flash" says it erases the saved configuration; the config lives in LittleFS at 0x3D0000, which the flasher never touches. | Correct the text (erasing the config silently would be the more dangerous fix). | `nctool.fw_wipe_text`, `nctool.webserial_flash_same_image` |
| D-NC35 | Two tool tabs both number saves from 1, so one tab's ACK can advance the other's baseline. | A random saveId base per tab. | `nctool.multi_tab_save` |
| D-NC36 | Doc drift found while mapping: bare PING claimed to work (PROTOCOLS.md:225, `NaviCore.ino:20`, the banner at `:4922`); RX buffer 4 KB vs 8 KB (PROTOCOLS.md:16); incomplete ACK shapes (PROTOCOLS §2); CALIB's exemptions (PROTOCOLS.md:233); cumulative tiers fire together (ARCHITECTURE.md:294-295); a held 2nd tap fires mid-hold (:304); remote Maestro writes are a raw broadcast (:364, WCB_NATIVE_MAESTRO_DESIGN §2); `RA_SMOOTH_OVERRIDE` retired (:368); RecEvent is 140 bytes (the setup comment at `NaviCore.ino:4661` still says 136); `REC_MAX_MS` is 60 s (RECORD_REPLAY_DESIGN §6); setSpeed/setAccel are captured (§5); NVS is migration-only (CONFIG_SCHEMA §3); `rc_telemetry.h:44-47`; CONFIG_TOOL.md §1, §2, §6, §8. Line numbers per the map. | One docs commit in NaviCore with the first push (D-NC6), each page's revision log updated. | none |
| D-NC42 | A string field is cut at its buffer size in bytes (`strlcpy`), so a cut through a multi-byte UTF-8 character keeps half of it: GET_CONFIG and `/config.json` then carry invalid UTF-8, and the tool writes U+FFFD back on its next save (`rc_config.h:1592` for a tier note; every note, label, name and command field alike). Found writing NC-WP1. | Cut back to a character boundary. | `nccfg.string_truncation_utf8` |
| D-NC43 | `holdMs` is raised to `tapWindowMs` + 250 and then capped at 5000, while `tapWindowMs` has no upper bound (`rc_config.h:1516-1528`), so a `tapWindowMs` above 4900 leaves `holdMs` below it and the long press can never be recognised (the comment at `:656-660` says it must stay above). Found writing NC-WP1. | Cap `tapWindowMs` at 4900, or let the `holdMs` cap give way to it. | `nccfg.hold_exceeds_tap_window` |
| D-NC44 | No config apply clears a parked tap or hold (`tapState`), and only the USB SET_CONFIG re-arms the matrix debounce (`NaviCore.ino:3942-3952`); the bridged SET_CONFIG (`rc_telemetry.h:1173-1190`) and both RESET_DEFAULTS paths (`NaviCore.ino:3989-3992`, `rc_telemetry.h:1538-1545`) do not. Defaults whose matrix channel reads inside a band register a press nobody made, and the release a later restore causes fires that button's restored mapping (`NaviCore.ino:2347-2415`). Found writing NC-WP1. | Every config apply, on either transport, clears `tapState` and re-arms the matrix. | `sbus.reconfig_parked_tap_cleared`; `nccfg.mesh_creds_live_split` steers clear of it (`_defaults_live_effects`) |
| D-NC45 | The dispatch trace reports sends that did not happen: a serial action prints `[DISPATCH] Serial TX [<port>]  <cmd>` before it looks at the port, and a port other than S3-S5 then writes nothing and says nothing (`NaviCore.ino:2093-2100`, the `hil-week` tree); a Maestro action prints `[DISPATCH] Maestro <slot>  <cmd>` before its skip-if-running gate, so a skipped one reads as sent until the next line (`:2088-2089`). The inbound `;M` case was fixed the same way (`navicore.maestro_skip_not_logged_as_dispatch`). Found writing NC-WP4. | Print the dispatch line after the checks, and a skip line with its reason otherwise. | `ncengine.skip_not_traced_as_sent` (the serial case; the Maestro one needs a moving servo) |
| D-NC46 | `GET_WCB_SEQ` and `GET_WCB_SEQVAL` strip every `"`, backslash and control character from a sequence's names and value instead of escaping them (`_seqAppendJsonSafe`, `rc_telemetry.h:340-346`, used by `buildWcbSeq` `:352-375` and `buildWcbSeqVal` `:380-393`). The comment above `buildWcbSeqVal` says nothing may reformat the value (`:377-379`). A sequence holding JSON, a `;L` WLED command's body for instance, reaches the config tool without its quotes, and one saved back would be stored altered. Found writing NC-WP6. | JSON-escape names and values. | `ncmesh.seqval_verbatim` |
| D-NC47 | A bridged SET_CMDLIB stores everything from after `"data":` to the message's last `}` (`rc_telemetry.h:1209-1216`), so a key after `data` is stored with the library. The USB handler matches brackets (`NaviCore.ino:3902-3935`), because the same bug once stored a trailing `,"sys":1`. The config tool avoids it by stamping `sys` first, and its comment says the firmware no longer depends on key order (`index.html:5611-5617`); that holds for USB only. Found writing NC-WP6. | One extraction for both paths: the USB path's bracket matching. | `ncmesh.bridged_cmdlib_keys_after_data` |
| D-NC48 | NaviCore's `?MGMT,FRAG` relay sends each chunk of a multi-chunk push as a 230-byte `config_frag` (WCB_Client `WCB_Mgmt.h:343-360`, the struct at `:97-106`), but a WCB takes a MGMT fragment only as its 226-byte `espnow_struct_mgmt` (`WCB.ino:1077-1086`, matched by size at `:5181-5183`); the 230-byte frame lands in the config-frag branch, which knows no type 3 and drops it without a word (`:5202-5213`). No chunk of a push through NaviCore reaches the target, while NaviCore prints `[relay] MGMT -> WCB<n> frag i/n` for each: a Wizard config push through a NaviCore relay loses everything over one chunk. WCB_Client's own MgmtRelay example builds the right frame (`wcb_packet_mgmt_t`, `WCB_Client.h:197-205`; `MgmtRelay.ino:859-869`). Found by NC-WP6's second bench run (`20260929-042105`); the first run showed the same. | Build the frame from `wcb_packet_mgmt_t` in `WcbMgmt::handleMgmtFrag`, as MgmtRelay does. | `ncmesh.mgmt_frag_multichunk` |
| D-NC56 | `executeMaestroCmd` casts every number before anything checks it: `(uint8_t)atoi` for the channel, the accel and the subroutine, `(uint16_t)atoi` for the target, the speed and subParam's parameter (`NaviCore.ino:1316`, `:1320`, `:1324`, `:1331`, `:1372`, `:1388`, the `hil-week` tree). So `setTarget,261,6000` moves channel 5, `setAccel,5,300` sends accel 44, `setTarget,5,70000` sends 4464 (under the clamp whose comment says a value is 'capped, not wrapped', `:1035-1036`), `setSpeed,5,65537` sends speed 1, `subParam,3,70000` sends 4464 and `restartScript,300` runs subroutine 44: each a valid command for something else. WcbCmd's `;M` parser casts the channel, the target and the speed the same way before `buildSetTarget`'s range check sees them (`WcbMaestro.cpp:141`, `:147`, `:153`, 0.9.1), so the inbound `;M` on both firmwares shares it. Found writing NC-WP7. | Parse into a long; refuse a channel, subroutine or accel out of range with a line; clamp a target, speed or parameter from the long. The same in WcbCmd's parser, pushed first (WCB rule 1). | `ncdev.mae_verb_no_alias` |
| D-NC57 | A per-channel HCR Volume Up/Down on NaviCore's own port (fn 18/19, chan 1-3) steps the codec's shadow in NaviCore and clamps it to 0-99 (`NaviCore.ino:1705-1714`, the 99 at `:1712`), while HcrCodec's SetVolume and all-channel steps take 0-100 (`WcbHcr.cpp:47`, `:113`; the 99 cap was WCB issue #16, fixed in WcbCmd 0.9.0) and so does a WCB's `;H,VOLUP,<ch>` (`WCB_HCR.cpp:609-632`). The same action sends `<PVA99>` locally and `<PVA100>` through a WCB, and a Volume Up on a channel at 100 turns it down. Found writing NC-WP7. | Clamp at 100. | `ncdev.hcr_local_volstep_cap` |
| D-NC58 | A serial action writes its whole line to S4 or S5 at once (`writeS4`/`writeS5`, `NaviCore.ino:1401-1402`, from `:2098-2099`). Those ports are bit-banged and a write returns when its last bit is out, so `loop()` stops for the line: about 100 ms for 95 characters and a CR at 9600, past the ~96 ms of SBUS-24 that Serial1 buffers. The mesh-to-serial path to the same ports hands them a few bytes a pass for exactly this reason (`auxTxPump`, `:5026-5086`). Found writing NC-WP7. | Send RA_SERIAL through the paced auxTx queue. | `ncdev.serial_action_paced` |
| D-NC59 | An HCR Trigger/Stimulate level of 2 or more goes out as the number on NaviCore's own port (HcrCodec formats it, `WcbHcr.cpp:65-67`; its golden vector pins `<SM30,QEM,QT>`) but as STRONG, level 1, through a WCB (`hcrFormatWcbCommand`, `NaviCore.ino:1516-1517`). The config tool offers Moderate (0) and Strong (1) and shows a legacy value of 2 or more as Strong (`config_tool/index.html:12640-12643`), so one saved action is two different commands by transport. Found writing NC-WP7. | Normalise the level to 0/1 before either transport, as the tool reads it, or send the numeric `;H,FN` form over the mesh. | `ncdev.hcr_level_same_both_ways` |
| D-NC60 | A WLED action written without its `;` (`L1,ON`) is routed by the id after the L (`NaviCore.ino:1962-1977`): on a local slot WcbWled gets the verb body (`:2009`), but to a remote slot the command is forwarded as written (`:2013`), and a WCB runs a unicast without its command character as plain broadcast text (`WCB.ino:6010-6020`). The WLED gets nothing, and `L1,ON` goes out every port with broadcast output on. The same saved action works or not by where the WLED is. Found writing NC-WP7. | Forward `;L<id>,<body>` rebuilt from what was parsed. | `ncdev.wled_forward_normalised` |

## Revision log

| Date | Commit | Change |
|---|---|---|
| 2026-09-29 | _(pending)_ | NC-WP6's second bench run (`20260929-042105`): five of the eight fixed tests pass and `seqval_verbatim` fails as designed. New finding D-NC48 (a multi-chunk `?MGMT,FRAG` push through NaviCore goes out in the wrong frame and never arrives) with its `(should)`, `ncmesh.mgmt_frag_multichunk`, split out of `mgmt_stats_frag`. Test fixes, not yet re-run: `mgmt_etm_char` compares the relayed block with W2's own (the tag line is bare); `mgmt_stats_frag`, `seq_pull` and `seqval_verbatim` ask once more after a reply lost on the air (tracker #109; `_seq_ask`, `_reply_leg`); the SBUS gate re-reads for 4 s. |
| 2026-09-29 | `bfe55e5` | NC-WP6's first bench run (`20260929-025701`): 21 of 35 passed, the six `(should)` failed as designed, and eight failed on the tests' own faults, fixed and not yet re-run: W2 writes undone by each test (`_put_back`, `_seq_clear`) in `mgmt_stats_frag`, `wdp_neighbour_table`, `alias_whoami`, `seq_pull` and `seqval_verbatim`; `fragment_reassembly_edges` waits for each case's ACKs (no NaviCore finding); `bridged_cmdlib` and `bridged_wcb_meta` judge SBUS by the frame counter (`_sbus_kept_up`); `mgmt_etm_char` waits for W2's send line and asks once more, because W2 sends that reply once under its own load; `crc_namespace_gates` sets DBG_MAESTRO after the burn, which clears it. `NaviMeshModel` gains a moving frame counter, an `sbus_starved` mutation and the ACK's 0.1 s lag. The status note lists what the run showed. |
| 2026-09-29 | `6da1180` | NC-WP7 bench-verified and NC-WP6's first bench pass (`20260929-025701`): 37 pass, the ten `(should)` tests that ran fail as designed, eight NC-WP6 tests being fixed. |
| 2026-09-29 | `ede3aca` | NC-WP7 written, not bench-run: `s44_navicore_devices.py` (23 `ncdev` tests, six `(should)`; five in `hil/servos.py`; five behind the new opt-in `navicore_aux_tx`, registered and off). In s21, `navicore.mae_cli_local` needs a value (D-NC15) and `navicore.maestro_mesh_fanout_0_9` reads each target back; s41's speed-4 figure corrected. New findings D-NC56 to D-NC60. `selftest.py` runs the suite against `NaviDevModel` and twelve mutations of it. The status note lists where the code differs from the plan, and NaviCore doc drift for D-NC36. |
| 2026-09-28 | `4170a24` | NC-WP6 written, not bench-run: `s43_navicore_mesh.py` (35 `ncmesh` tests, six `(should)`). Opt-in `navicore_nvs` registered (off); three tests in `hil/servos.py`. `hil/ncmesh.py` gains pure predictors (`wdp_scrub`, `json_strip`, `rterm_pieces`, `port_labels`, `local_maestro_ids`, `status_rows`, `stats_rows`, `bulk_frames`). `selftest.py` runs the 13 bridge tests against `NaviMeshModel` and 11 mutations of it. New findings D-NC46 (sequence names and values stripped, not escaped) and D-NC47 (a bridged SET_CMDLIB stores the keys after `data`). The status note lists where the code differed from the plan. |
| 2026-09-28 | `04e70d1` | NC-WP2, NC-WP4, NC-WP5, NC-WP9 and NC-WP10 bench-verified (`20260928-204259`, `-212818`, `-212848`, `-215752`): every normal test passes or skips for a stated reason, and the seven `(should)` tests fail as designed (D-NC19, D-NC20, D-NC25, D-NC29, D-NC44, D-NC45). |
| 2026-09-28 | `75d5e8d` | NC-WP2, NC-WP9 and NC-WP10 written, not bench-run: `s46_navicore_boot.py` (9 tests, 3 `(should)`) and `s47_navicore_ota.py` (11); opt-ins `navicore_ota_erase`, `navicore_ota_full`, `navicore_ota_relay_full`, `navicore_identity` registered and `navicore_esptool` added (off); `ncflash.BENCH_IMAGE`, `builds_with_sha`, `flash_rows`, `last_written`, `put_back`; `NaviCore.restart_blocker`; `redact_text` hashes a SoftAP name. `selftest.py` runs both suites against `NaviBootModel` and nine mutations of it. The three status notes list where the code differs from the plan: the roll call's floor (quantity 1 leaves W2 out; `deaf` stops reception only), NaviCore's `?OTALOCAL` without BAUD and with a case-sensitive prefix, and `recover()` calling a reset that did nothing a success. |
| 2026-09-28 | `863462d` | NC-WP4 and NC-WP5 written, not bench-run: `s41_navicore_engine.py` (12 `ncengine` tests, two `(should)`) and `s42_navicore_sbus_engine.py` (21 `sbus` tests, one `(should)`); 23 of them in `hil/servos.py`. New finding D-NC45 (a skipped action is traced as sent); D-NC44 gets its `(should)`, `sbus.reconfig_parked_tap_cleared`. The two status notes list where the code differed from the plan. |
| 2026-09-28 | `553236c` | NC-WP3's Export/Import, two-tab and live-panel specs (6, two `(should)`); `FakeSerial` tags events with their page; the fixture's switch SI Up action moves to `p2`. The INF7 note lists what they found: the CSV round trip narrows every button band; D-NC35 confirmed, and both tabs also share fragment-session numbers. |
| 2026-09-28 | `264583e` | NC-WP3's Firmware-tab and clip specs (19, five `(should)`): `lib/navicore/ota.js` (`?OTALOCAL`, the `?OTA` relay, the restart), `clips.js` (`?REC`, the clip store), `firmware.js` with the esptool-js and CryptoJS stand-ins, and `FakeSerial`'s absent device. The INF7 note lists the code facts they found: D-NC33 and D-NC34 confirmed; a refused flash leaves the session disconnected; USB OTA waits out 10 s per lost chunk; a refused Record is taken as started. |
| 2026-09-28 | `83684b3` | INF7 built (the config-tool rig: the `/NaviCore/` alias on its own origin 8779, the fake `navigator.serial`, the emulator and its model of `rc_config.h`, the bridge pipe and the bridge's `/serial` and `/sbus` routes, `run_wizard_test(..., pipe=True)`, a bench-shaped fixture and its scrubber, suite `s49_navicore_tool.py`) and NC-WP3 started: `nctool.static`, `nctool.unit` and 33 L1 specs, 8 of them `(should)`, all passing or failing as intended with no board; one L2 spec, `nctool.board_connect_config`, for the pipe. The INF7 note lists where the build and the tool's code differ from the plan. |
| 2026-09-28 | `0381b1f` | NC-WP1 and INF6 bench-verified; `SBUS_FULL_FPS` replaces four bare `>= 100` checks. |
| 2026-09-28 | `5a1957d` | INF6 built (`hil/ncmesh.py`, four `selftest.py` cases) and NC-WP1 written (34 `nccfg` tests in `s40_navicore_config.py`, opt-ins `navicore_reboot` and `navicore_fault`, `navicore.bench_health`, the s21 route check); `selftest.py` runs the whole suite against `NaviModel`, a port of NaviCore's config handling. No bench run yet. The two status notes list where the code differed from the plan: the burn, the probe's peer table, the 'parse failed' trigger, the password split made by RESET_DEFAULTS (only when the defaults decode no button or mode from the live SBUS input: a SET_CONFIG restore leaves a parked tap to fire the restored mapping). New findings D-NC42 (strings cut through a UTF-8 character), D-NC43 (holdMs left under tapWindowMs) and D-NC44 (no config apply clears a parked tap). |
| 2026-09-28 | `27c0822` | INF3 and INF4 bench-verified: `nccfg.guard_selftest` passes; `ncflash` proved its reset rung and flashed the running image into `app1` (79 s, no NAK). |
| 2026-09-28 | `5c68781` | INF4 built: `hil/ncflash.py` (build, image check, `?OTALOCAL` flash, the recovery ladder, FLASHED.md rows, a command line), seven `selftest.py` cases, and a real compile of NaviCore through `build()`; nothing flashed yet. The INF4 status note lists where the code differed from the plan: no SHA line on the board yet, NAK and base64-error semantics, one esptool connection with `boot_app0.bin` and `--after watchdog-reset` instead of an otadata erase, and a read-only download-mode rung. |
| 2026-09-28 | `c8ebe30` | INF3 built: `hil/nc_guard.py`, the credential filter in `Bench.log`, `redacted_diff`, the resume's NaviCore check, and `nccfg.guard_selftest` in the new `s40_navicore_config.py`. The INF3 status note lists where the code differed from the plan. |
