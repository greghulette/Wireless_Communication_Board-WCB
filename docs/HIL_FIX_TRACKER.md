# HIL fix tracker — the 2026-09-16 bench run

Working document, not a design doc. Delete it when the list is done.

**What this is.** The 2026-09-16 full run (`tests/hil/results/20260916-122250`) failed 57 tests. Each was
traced to source by a subsystem agent and then attacked by two independent skeptics; 55 distinct defects
came out, and all but one survived. This file is the work list, with enough of each diagnosis to resume
cold. Full analysis transcripts are gone with the session — what is here is what remains.

**Verdict split.** 55 of the 57 failures are real defects (A). One is a test bug (B):
`input.soft_rx_baud_sweep`'s 38400 bar is above what a bit-banged port can do — but the same test also
found a real `?BAUD` range-check gap, so split it rather than just relaxing it. One cannot pass as
written (C): `hcr.softserial_integrity_mesh_load` asserts 300/300 frames exact against a stochastic
~0.3 % fault, so it needs a residual budget plus a signature check. `sbus.btn_single_tap` failed only
because the SBUS controller's USB handle had died; it passed once the GUI was restarted.

**Ownership.** Most of the list is this repo. Flagged otherwise: `WcbCmd` (2 — push it BEFORE the
firmware, CLAUDE.md rule 1), NaviCore (3), `WCBClient` (2).

## How to work this list

1. Fix in tracker order (high impact first, small effort first within a tier).
2. Compile after each: `arduino-cli compile --fqbn esp32:esp32:esp32:PartitionScheme=min_spiffs
   --build-path <scratch> --output-dir tests/hil/results/builds/wcb-esp32-meshq Code/WCB`
   (never run arduino-cli while the Arduino IDE is compiling — use an isolated build path).
3. Flash both boards, then run the defect's tests:
   `arduino-cli upload --fqbn esp32:esp32:esp32:PartitionScheme=min_spiffs
   --input-dir tests/hil/results/builds/wcb-esp32-meshq -p COM6` (and `-p COM15`), then
   `cd tests/hil && python run.py "<test id>"`. Close the GUI first — one owner per COM port.
4. Update the row's status here, and add a dated line to the session log below.

Status values: `TODO`, `WIP`, `FIXED (unverified)`, `VERIFIED` (test green on hardware), `DEFERRED`.

## Session log

| Date | What happened |
|---|---|
| 2026-09-25 | No-servo full run `20260924-234056` on the F20/F21 image: 459 pass, 3 fail, 37 skip; all three test-side (docs/HIL_TEST_AUDIT.md §5), none from #91-#93. Test fixes to the offline-timing floor (plus #80's alternation check), the `?backup` chain parser and `?ETM,CHAR`'s peer wait; verified `20260925-015720` (21/21). Recorded audit F22 (a rebooted WCB sees its peers offline for one heartbeat), not filed here: the recommendation is to leave it. |
| 2026-09-24 | **#92 and #93 VERIFIED (20260924-233628, 43/43).** Greg: "Fix both". A cleared serial mapping frees its NVS keys; RTERM re-arms and `?STATS,RPT` no longer hold off a deferred restart, which also goes anyway after 20 s. A review found three small defects, fixed before flashing: `_act` written last, a slot whose count could not be stored keeps its output keys, and no ETM command is ACKed once the restart is imminent. W1/W2 flashed 23:36. The harness gained `--no-servos` (`hil/servos.py`, 27 tests, cross-checked by a second list). |
| 2026-09-24 | **#92, #93 FIXED (unverified)** on Greg's "Fix both": a serial-mapping save removes the keys no active mapping uses (`removeUnusedSerialMapKeys`), and a deferred restart ignores a re-arm of the running RTERM session and `?STATS,RPT`, with a 20 s cap (`RESTART_MAX_DEFER_MS`; sized in #93, not the 15 s proposed). Tests `map.nvs_keys_freed`, `etm.reboot_rterm_rearm`, `etm.reboot_stats_rpt`, `etm.reboot_defer_cap`. ESP32 70 %, S3 68 %, no new warnings; host tests and `selftest.py` pass; not flashed. |
| 2026-09-24 | Full run `20260924-190733` on the F13 image: 478 pass, 8 fail, none from F13 (docs/HIL_TEST_AUDIT.md §5). Filed #92 (a cleared serial mapping's NVS keys leak, audit F20) and #93 (RTERM re-arms starve a deferred reboot, F21), both TODO for Greg's decision; five harness fixes; reruns `20260924-205621`, `20260924-213158`. |
| 2026-09-24 | **#91 VERIFIED (20260924-190431).** F13 on Greg's word: a `?MGMT,PULL,<n>,P` of a config over 2912 characters arrives in parts, refusals are coded `[MGMT:CFGERR` lines, the target's reply is a non-blocking job, and the Wizard serialises pulls per relay (docs/MGMT_RELAY.md). Designed and reviewed by agent workflows (45 design issues, 15 code findings), implemented in the firmware, the Wizard, the harness and both WCBClient relays; W1/W2 flashed 19:04, backup and pull output byte-identical to before. Audit F14-F19 recorded, not fixed. |
| 2026-09-24 | **#90 VERIFIED (20260924-133332).** Greg took the recommendation: `?backup` streams its chains with a running CRC, and the mesh pull is measured, reserved once and never sent partial. The first build (20260924-131417) wrote 256 bytes at a time, and a peer's came-ONLINE line split a chain after a reboot; the writer now buffers 2 KB and flushes per line. W1/W2 flashed at 13:33 from `tests/hil/results/builds/wcb-esp32-meshq`; their `?backup` and the W2 pull are byte-identical to before. probe2's COM11 had been held by `Intellex.exe`, stopped with Greg's OK. Found audit F13: a pull over 2912 characters still gives the requester nothing. |
| 2026-09-24 | **#84-#89 VERIFIED, #90 filed.** Greg's decisions on the audit's F5-F10: `?TRACK` removed (#85), a negative `;T` delay refuses its chain (#86), `?LABEL` names a bad port (#87), `?ERASE,NVS` clears every namespace (#88), the sequence-list writes are checked (#84), and `?NVS` reports storage use, recorded by the harness per run (#89). Verified in 20260924-120036 and, for #84, 20260924-120958; W1/W2 run that image from `tests/hil/results/builds/wcb-esp32-meshq`. Filling W1's store showed `?backup` printing only a checksum where its one-line chains belong once the heap cannot copy them (#90, audit F12, Greg's decision). |
| 2026-09-24 | **Full run 20260924-092602 (servos on): 459 PASS / 6 FAIL / 8 SKIP in 1:45:28.** Three storage failures (`var.cap_persistent_full`, `inv.seqget_largest`, `seq.cycle_guard_reuse`) were a full NVS on W1 and pass once it is wiped (`20260924-112513`); nothing reports NVS usage (`docs/HIL_TEST_AUDIT.md` F10). The empty `?SEQ,SAVE,HILL1,` left behind is a firmware bug, filed as #84. `input.wdpda_port_full` lost one announce on W1 S4. `sbus.signal_loss_controller_reset` and `wifi.pc_joins_ap_ws` were test bugs, fixed and re-run to pass. |
| 2026-09-24 | **#81-#83 VERIFIED.** W1 and W2 flashed from `tests/hil/results/builds/wcb-esp32-meshq` (both report `6.2.1_232316RSEP2026`; the session's flash was refused by the auto-mode permission check until Greg switched the permission mode). The seven tests that depend on the fixes all pass (run 20260924-092423). The CI release of the same version name in `Code/bin` is a different image, so the bench build now lives in `wcb-esp32-meshq`, which the OTA tests stream; the previous build is kept in `wcb-esp32-meshq-231928`. Full run 20260924-092602 started, servos allowed. |
| 2026-09-24 | **#81-#83 filed and fixed from the HIL test audit** (`docs/HIL_TEST_AUDIT.md` F1-F3): the HCR/MP3/WLED soft-serial guards allow up to 57600 like `?BAUD`, the help's legacy PWM spelling, and `?HW` updating the running version. Compiled locally (ESP32 69 %, S3 68 %) into `tests/hil/results/builds/wcb-esp32-meshq`; NOT flashed - the session may not write the bench boards - so the three stay FIXED (unverified) until the image is on W1/W2 and `hcr.config_rejects mp3.config_rejects wled.config_rejects devices.soft_ports_fast_baud wled.soft_port_57600 ident.hw_setter pwm.legacy_forms` run. The audit's suites s24-s28 and the Wizard tests landed the same night (451 tests). |
| 2026-09-23 | **Full run 20260923-192959 on the pushed images (WIFI 95dd811/078a907, 6.2.1_231928RSEP2026 built locally; wcb_probe 7): 415 PASS / 0 FAIL / 5 SKIP in 1:59:25**, no probe panic, no host outage. Skips: the three WDP-DA label checks (need an unlabelled W1 port) and the opt-ins left off (`ota_wrong_chip`, `w1s4_soak`). Before it: #80 verified (`etm.offline_detection_timing` 10/10, no race fingerprint in 45 OFFLINE events), the probe crash loop reproduced on v6 (6 panics in 3 W1 reboots) and gone on v7 (0 in 10), and the three other failures of 20260923-154611 pass. CI on the push: firmware build (EspSoftwareSerial bundled, not installed), Wizard tests, WDP wire tests, Wizard preview - all green. |
| 2026-09-23 | **Full run 20260923-154611 (probe 6): 4 FAIL, all diagnosed and adversarially verified.** `etm.offline_detection_timing` = a pre-existing cross-core race on `boardTable` presence, filed and fixed as **#80** (spinlock helpers). `peers.controller_off_on` = test bug (an ACK during the OFF window re-adds WCB20; now excused only when that ACK is logged). `client_mesh.rejoin_seq_reuse` + `input.mesh_bcast_to_ports` = the probe: wcb_probe 6 boot-looped on interrupt-WDT panics after a CPU-only reset left a soft-RX level arm with no handler, and the harness kept reading its lost channel - #78's new failure mode, fixed in wcb_probe 7, hardened in WCB `setup()` (`clearStaleGpioInterrupts`), and the harness now re-binds after any probe restart (a dropped-and-reopened port included) and fails the test in which an unplanned one happens; a review pass made the forget selective and the end-of-test incident `RESET` the probe, so probe and host never disagree on which headers are held. ESP32, S3 and probe compile; `selftest.py` 39/39. Nothing flashed. |
| 2026-09-23 | #78 CONFIRMED and fixed (FIXED, unverified; compiled, not flashed). `softrx.erratum_pairs` on W1 (run 20260923-113109): 227 of 10125 two/three-port lines lost at -1.5 to -3 µs skew, all 10125 single-port lines exact; every attributable one-byte corruption is exactly one lost edge. Fix: EspSoftwareSerial 8.1.0 vendored at `Code/WCB/src/EspSoftwareSerial` with level-triggered, polarity-flipping RX on the classic ESP32 (S3 untouched), PWM input ISRs converted the same way, CI no longer installs the library, `?DEBUG` reports each soft port's RX mode. New test `softrx.level_irq_stuck_line`; `input.softserial_tx_rmt` asserts the RX mode. ESP32 69 % / S3 67 %, ISRs in IRAM, no stock library in the build. Next: flash W1/W2, run `softrx.*` and `input.softserial_tx_rmt`, then the soft-port and PWM suites. |
| 2026-09-23 | Targeted run 20260923-095057 on the 09:49 images (#47, #73 D4-D9, probe 4): **139/140**. The one failure, `kyber.local_port_move_releases_old` step (e), was not the Kyber change: every probe channel re-bind pulled the WCB's RX line low, W1 read a NUL at the front of the next line, and its parser broadcast that line as an EMPTY command (a lone CR on S4) and lost it. Fixed on both sides (#79): wcb_probe 5 holds a re-bound channel's TX high, and the WCB line reader drops NUL bytes. New (should) test `input.nul_ignored` failed on the 09:49 image as predicted; with probe 5 and the new image `input.*` is 50/50 (3 known WDP-DA label skips) and the Kyber test passes (20260923-111854). #47 and #73 D4-D9 VERIFIED. |
| 2026-09-23 | #78 measurement built (harness only; not run, nothing flashed): probe verb `TXSKEW` (wcb_probe 4, compiled) sets a sub-microsecond skew between up to three bit-banged lines, and new opt-in suite s23 has `softrx.erratum_pairs` (does W1 lose soft-port input to GPIO-3.14? pairs and triples at a swept skew against single-port controls) and `soak.w1s4_wire` (the W1S4 episode: hardware vs soft receiver, S4 alone vs with S2, crosstalk into idle S4). The level-triggered EspSoftwareSerial fix waits for a "confirmed". To run: flash probe1, add `softrx_erratum` / `w1s4_soak` to `opt_in`. |
| 2026-09-23 | #47 applied (FIXED, unverified; compiled, not flashed): the cycle guard's never-popped `activeChainKeys` set is replaced by a per-item lineage (seqDepth + key hashes in front of the queued text, restored into `seqCurPath` at drain, pushed in `recallCommandSlot`, carried across `;t` by `commandGroupsPath`), plus a 16-slot queue reserve for nested expansions. New (should) tests `seq.cycle_guard_reuse` (should fail on the current images: DEPTH HILL8, REFUSE HILL1-3) and `seq.reuse_queue_reserve`; `seq.cycle_guard` unchanged. ESP32 compiles at 69% / 108120 B RAM. Wiki self-loop / state-machine examples rewritten. |
| 2026-09-23 | #73 D4 applied (FIXED, unverified; compiled, not flashed): `kyberModeReservesPort()` - Kyber LOCAL reserves only its own port, REMOTE still S1 - in the HCR/MP3/WLED/DFP guards, `canUsePWMOnPort` and `;P`; the Kyber_Local boot skips the other port's UART when PWM owns it; the targeted Kyber bridge skips a device/PWM-owned port; the Wizard claims only the Kyber port and releases REMOTE's S1 (`KYBER,CLEAR`) before the device lines of a REMOTE -> local push. New (should) tests `kyber.local_free_port_takes_pwm` (should fail at the WLED S2 arm on the current images) and `wizard.kyber_local_frees_other_port` (no board). D4b filed (device guards lack a Maestro term; a skipped PWM mapping comes back). ESP32 compiles at 69%. |
| 2026-09-23 | #73 D5 + D9 applied (FIXED, unverified; compiled, not flashed): one `kyberReleasePort()` for CLEAR, a `?KYBER,LOCAL` port move and `?MAESTRO,REMOTE`; REMOTE zeroes `kyberLocalPort`; the bare `?KYBER,LOCAL` keeps the current port; `?backup` and the Wizard release before BAUD/BCAST (Wizard also re-sends the released port's rows and asks for a reboot on MAESTRO,REMOTE). #28's note on a refused `?MAESTRO` during a restore corrected. New (should) tests `kyber.local_port_move_releases_old` (fails at step b on the current images, before any injection) and `wizard.kyber_release_order` (no board; passes standalone, and failed on the old parser.js - which is how the Wizard tests' persistent Chrome profile was found serving a stale cached parser.js; `openWizard()` now disables the HTTP cache); `kyber.local_rejected_port_keeps_forwarding` compares only the `?KYBER,LOCAL` line. ESP32 compiles at 69%. |
| 2026-09-23 | #73 D6-D8 applied (FIXED, unverified; compiled, not flashed): the legacy S1 Maestro fallbacks skip an owned S1, a local Kyber target follows its Maestro slot (no broadcast skip, by decision), and ?KYBER/?config name the real Kyber port. New (should) tests `kyber.local_s1_legacy_fallback_skips_kyber_port` and `kyber.local_clear_maestro_drops_target` (both should fail on the current images); `kyber.local_mode_s2`'s needle updated. D6b and D7b filed open. ESP32 compiles at 69%. |
| 2026-09-23 | Reconciled: 14 FIXED items promoted to VERIFIED because every named test passes on the current images (runs from 20260922-211114 on): #14, #28, #58, #61-#63, #65-#72. Still open: #47 (cycle guard), #64 (IRAM flag - no test reproduces it; the whole full run ran on the fix), #73 (Kyber D4-D9), #78 (soft RX). Design workflow for #47/#73/#78 running. |
| 2026-09-23 | probe.soft_concurrent_rx residual: a verified diagnosis blamed ESP32 erratum GPIO-3.14 (lost edges, pins 0-31 share one interrupt status register). The rewritten self-check's control arm then caught an INTERMITTENT ~3.5 % W1S4 corruption with only one probe edge pin active (20260923-012123), which rules the erratum out for that episode. Minutes later everything was clean: W1S4 on a hardware UART 500/500, the control repeated 500/500, the full self-check 1000/1000 + 300/300 (20260923-012735). Filed #78 (open: bench check of the W1S4 lead/ground; inferred WCB exposure to the erratum) and corrected #66. |
| 2026-09-23 | **Full run 20260922-215719 (21:01/21:02 images): 401 PASS / 2 FAIL / 4 SKIP in 1:39:11** - skips = 3 WDP-DA label checks (need an unlabelled W1 port) + ota_wrong_chip (opt-out). Both FAILs were NaviCore test bugs (#76). Then: W1/W2 flashed with the #75 heap hardening (23:58); regression 20260922-235913 (172 tests on the changed paths): 2 FAIL - inv.mgmt_seq_remote (lost once-sent SEQ reply, #77) and probe.soft_concurrent_rx (probe instrument, ~0.17% of lines when two soft channels receive at once; diagnosis running). Verification 20260923-002254: 18/18 PASS incl. sbus.trim_exact then the NaviCore readback (#76), mesh_stats_counts, all inv.* (#77), kyber.remote_roundtrip (retried once: try 1 lost one 6-byte frame, #74). #74-#77 VERIFIED. |
| 2026-09-22 | **Rerun 20260922-193306 (69 tests): 64 PASS / 4 FAIL / 1 SKIP** — all 9 input.wdpda_* PASS (session wcb-ce's issue #19). Greg decided #14 (apply) and the #28 follow-up (refuse a local-Maestro port); both applied, and a verified review closed two bypasses (the backup/Wizard target form, a later ?MAESTRO) and the push-order regression (?backup and Wizard now claim KYBER,LOCAL after the Maestros; the Wizard sends KYBER,CLEAR first on a port move and re-sends the released port's BAUD/BCAST). #70 OTA teardown ACK (WCB + NaviCore), #71 seqget retry, #72 host hibernate on critical battery (laptop on battery since 15:59) + awake-time detector, #73 Kyber follow-ups filed, not applied. W1/W2/NaviCore reflashed 21:01; targeted GUI rerun of 62 tests started. |
| 2026-09-22 | **Triage of full run 20260922-151421 applied** (8 diagnoses, all adversarially verified; test rewrites by a per-file workflow, each diff adversarially reviewed). Firmware: #63 soft-port reads suspend the scheduler, #64 GPIO ISR service without ESP_INTR_FLAG_IRAM (crash risk found by two agents, confirmed from the map: __onPinInterrupt is in flash), #58 heap metric MALLOC_CAP_8BIT (explained), #28 reopened and fixed (refusal before any state change); NaviCore #65; probe #66 (wcb_probe 3). Tests #67-#69, plus inv.seqget_largest and kyber.local_port_s1_frees_s2. W1/W2 had been flashed by session wcb-ce at 19:25 from the same tree (it added WDP-DA per-port-and-type, issue #19, and three input.wdpda_* tests); NaviCore and both probes flashed 19:3x. Combined GUI rerun of 69 tests started. Awaiting Greg: #14 (who reads S1/S2 on Kyber_Local boards) and the #28 follow-up (refuse ?KYBER,LOCAL on a local-Maestro port). |
| 2026-09-22 | **Opt-in run 20260922-185238** (Greg enabled ota_erase, ota_full, ota_full_wcb2, nvs_wear, seq_wipe, sbus_reset, navicore_clip): 8 PASS / 2 FAIL / 15 SKIP. The 15 OTA skips were the image lookup (Code/bin only; the bench build keeps the committed version string): _image now prefers results/builds/wcb-esp32-meshq. Opened and fixed #61 (firmware: a rejected re-BEGIN strands USB at 921600) and #62 (test: an RTS-only pulse never reset the SBUS controller on Windows). Both diagnoses adversarially verified. Not yet flashed or rerun. |
| 2026-09-22 | **Full run 20260922-151421 (GUI, post-rewire): 357 PASS / 16 FAIL / 29 SKIP.** 12 of the 16 FAILs are one host event: Windows logged a power-source change at 15:59:03 and slept at 15:59:09; all seven bench ports errored in the same 12 ms; resumed 18:26 (so auth_mismatch_silent 'took' 8841 s). Harness now keeps the PC from idle-sleeping during a run, and reports an all-ports-at-once loss as ERROR 'NOT A RESULT' and waits for the ports (runner.host_usb_loss). Greg's rerun of the 16 (20260922-183613): 15 PASS; navicore.maestro_skip_not_logged_as_dispatch fails consistently (the first run in which it ran at all: every earlier run skipped it with 'NaviCore has no local Maestro slot that answers ?MAE,GET'). Remaining 4 real FAILs + 3 non-opt-in SKIPs under diagnosis (workflow). Skips: 26 of 29 are opt-ins. |
| 2026-09-22 | Greg rewired probe2 (W2S1 tap -> header S2, W2S5 -> header S5); --discover found all 10 wires verified. navicore.plain_broadcast_both_ways + input.bcast_fanout PASS, and every tap-dependent test still PASSes (20260922-150647): #60 VERIFIED. The three timing tests that skipped now wait and PASS on their own (20260922-145506). All three failures of full run 20260922-125537 are resolved. |
| 2026-09-22 | Kyber single-reader fix (#59) + probe hand-off guard (#60). Workflow diagnosis of the last 3 full-run failures, adversarially verified: two firmware (a local Maestro port on a Kyber board read by both the parser and the bridge task; ?KYBER,CLEAR leaving S1 double-read and printing no reboot notice), one harness (probe2 needs 3 hardware channels at once, and LRU eviction silently misattributed bytes). W1+W2 flashed; run 20260922-144915: every kyber.* and maestro.* PASS except two SKIPs (local_port_s3_frees_s2 by design; wdp_auto_remote_revert timing, being changed to wait). plain_broadcast_both_ways + bcast_fanout now fail with the guard's rewire message. |
| 2026-09-22 | Tracker reconciled against the bench: every FIXED item whose named tests all PASS in full run 20260922-125537 or a later rerun is now VERIFIED (47 promoted). Totals: 52 VERIFIED, 1 FIXED (#28 - its test skips by design since ?KYBER,LOCAL,S3 is refused), 2 DEFERRED (#14, #47), 2 OBSOLETE (#33, #55 - bit-banged TX is gone), 1 OPEN (#58). |
| 2026-09-22 | #52 decided (Greg): Maestro get with no slot unicasts to WCB<dev> within WCBQ, like the other two fallbacks. Both targets compile (69% / S3 67%), W1+W2 flashed. Maestro+Kyber suites 20 PASS / 12 SKIP (skips = bench wiring), NaviCore-hosted Maestro inventory PASS. |
| 2026-09-22 | **Full run 20260922-125537: 347 PASS / 3 FAIL / 52 SKIP, 0 serial events** (best yet; every NaviCore test passed). input.bcast_out_block = W1 S4 config drift from the aborted run (and a double-click-queued rerun, closed 1 s after starting, re-left S4 OUT OFF) - restored W1 to baseline, test PASS x2. maestro.list_and_legacy_spellings = relayed {"sys":1} rc_ch line inside ?KYBER,LIST output - WCB.run() now filters sys lines, PASS x2. maestro.get_fallback_unicast_within_wcbq = #52, still DEFERRED (needs Greg's call). |
| 2026-09-22 | GUI: dark theme by default (--light restores the bright one; palettes at the top of gui.py, DWM dark title bar) and a guard so a double-click during a run no longer queues a second run / clears results. Full run 20260922-125139 was aborted after 6 failures that were all one loose wire (W1S2 -> probe1 S4, knocked out during the unplug); rediscovered and restarted as 20260922-125537. |
| 2026-09-22 | NaviCore USB output stall: HWCDC (esp32 3.3.4) drops to 'disconnected' after one 50 ms TX stall and only host INPUT reconnects it - replies arrived a command late (~2 % of back-to-back commands). NaviCore loop() now kickUsbCdcTx(): txfifo_flush + re-arm IN_EMPTY every 20 ms (0/800 stalls). NaviCore docs ARCHITECTURE/TROUBLESHOOTING updated. navicore.probe_temp_peer_not_learned ran for the first time (index guard) and found WCB_Client marking ANY heartbeat/boot-announce sender online while the offline sweep only covers floor/learned/special - a temporary client stayed 'known' forever (boot half introduced by my #11). Both handlers now gate on tracked senders; both WCB_Client copies identical; README 1.17.1 changelog. Test PASS. Two navicore tests (mesh_json_declined lost-output, mesh_stats_counts extra send) fail only on a SECOND consecutive navicore+sbus run; pass 4/4 alone and after sbus.* - open. |
| 2026-09-22 | Bench contention: 4 orphaned headless Edge instances (from 2026-09-20) killed; sessions wcb-4f/-cf/-b7 asked to stay off the ports (all acked, none had touched them today). Real cause of the SBUS hang: bench.json sbus had been changed to COM19 (Bluetooth SPP) at 09:11 - likely the GUI port dropdown (lists all ports, wheel-scroll saves); restored COM4, dropdowns now USB-only / read-only / wheel-off. Own regression caught: setting write_timeout per send re-ran pyserial's SetCommState under the reader and delayed GET_CONFIG ~10 s on NaviCore - now set once at open (GET_CONFIG 10/10 in 0.1 s). sbus.* 9 PASS / 2 expected SKIP, twice. |
| 2026-09-22 | Harness: SBUS rerun hung 10 min holding COM4 - its second ping's write() blocked forever (no write_timeout) while the controller was not draining its USB endpoint; a concurrent wizard.* run from another session was also on the bench. hil/serialdev.py now bounds every write (raises ExpectTimeout) and reopens a vanished port from the reader thread (the cause of the 8 sbus.* ERRORs in 20260921-143814). Verified offline against a fake pyserial: drop->reopen after 2 failed opens, send after reopen, stalled write fails in 0.1 s, close stops the reader. |
| 2026-09-22 | Full run 20260921-143814 on the RMT image: 332 PASS / 9 FAIL / 8 ERROR, all classified - 8 sbus.* ERROR = the SBUS board's USB dropped mid-run and the harness never reopens a vanished port (not firmware); navicore x2 late replies; 3 pwm tests asserted bit-bang quirks (first ;P invisible; ;S bytes onto a live PWM output) - updated; inv.seqget_toobig opened #58; soft_rx 1 byte/~1000 lines once (3/3 clean after). Reflashed W1/W2 (the bench had gone offline mid-flash). Rerun 20260922-085933: 11 PASS + 1 expected skip, incl. every RMT test, the updated pwm tests, rejoin_seq_reuse and both navicore. **ESP32-S3 (WCB 3.2 / SBUS board) RMT check**: full 16 MB flash backed up, probe flashed: GPIO ISR L3 ESP_OK; S3/S4/S5 all rmt=yes alongside the NeoPixel (the whole 4-channel budget); 100-byte writes 104.7 ms @9600 (104.2 theoretical), 9.2 ms @115200 (8.7 - chunk gaps, 9 bytes/chunk on the S3's 48-word channels), 417.4 ms @2400; LED updated after. Backup written back, SBUS answers fwver 20260811-fae0af6. |
| 2026-09-21 | **Option 4 (Greg): S3-S5 TX on RMT.** New WcbSoftSerial (subclass of the library: RX stays EspSoftwareSerial, write() goes to an RMT channel via a simple encoder, 10 MHz >= 4800 baud / 1 MHz below, chunked to fit channel memory so the non-IRAM refill ISR is never needed mid-frame, falls back to bit-bang if no channel). ;P reclaims the pin via rmt_tx_switch_gpio. Results: soft_rx_under_soft_tx PASS, baud_on_raw_mapped_port PASS, core0_contention PASS, tx_integrity 200/200 on every arm incl. the old unprotected port (was 60-73/200). Then GPIO ISR service at level 3 (setup, before any attachInterrupt): soft_rx_baud_sweep 19200/38400/57600 20/20 x3 (was 15-19). ?BAUD S3-S5 now warns only above 57600. Tests rewritten: input.softserial_inttx_state -> input.softserial_tx_rmt; hcr.softserial_integrity_mesh_load and input.softserial_tx_integrity assert RMT (all arms). CLAUDE.md rule 13 rewritten. Both targets compile (69% / S3 67%). Full run 20260921-143814 started. |
| 2026-09-21 | **#57 fixed**: UART0 driver installed from a core-0 task (beginUsbSerialOnCore0). A/B on W1: protected S3 mangled USB lines in ~half of runs; unprotected 0/6 (but 0/10 S3 blocks); protected + core-0 ISR 0/8 and 8/8 PASS. ?STATS prints serialRxOverflows when non-zero. Both targets compile (69% / S3 67%); W1+W2 flashed. Regression run 20260921-121337 (input/ota/chars/map/wcb, GUI): 94 PASS, 3 known #16 fails, plus input.softserial_tx_integrity 1/200 wrong once (S3 output burst mis-framed, the #55 residual) - then 3/3 PASS, 1200/1200 protected lines exact. CLAUDE.md rule 13 records the core-0 constraint. |
| 2026-09-21 | Rerun 20260921-115338: chars.funcchar_change_restore + if.stored_seq_recall_time PASS. input.softserial_core0_contention reproduces (3 of 4 runs, GUI and CLI): opened **#57** - a USB command arriving during a mesh raw block to protected S3 loses its ';' ('7S4,…'/'784,…') and is broadcast; mechanism not proven, next steps listed there. |
| 2026-09-21 | **Full run 20260921-105657 (GUI): 6 FAIL / 341 PASS / 51 SKIP** (morning: 27 FAIL). Known: input.soft_rx_under_soft_tx + map.baud_on_raw_mapped_port (#16), maestro.get_fallback_unicast_within_wcbq (#52). New: chars.funcchar_change_restore - my shared prefixCharOk validator reworded the collision refusal; original wording restored ('— … Pick a different function identifier.'), compiled. if.stored_seq_recall_time - harness race (the [IF] true echo printed 93 ms after the command reached S1; test now waits for it). input.softserial_core0_contention - 8/10: R9 two corrupt bytes (#55 residual) and R4 interleaved with a BROADCAST line: W1's USB input received ';S4,HILL12…' as '7S4,…' (0x3B->0x37, one bit), so the line lost its prefix and was broadcast as text to every port. Single occurrence, not reproduced; recorded, no fix. soft_rx_baud_sweep passed this run. Wiki draft (7 pages) done, uncommitted. |
| 2026-09-21 | Decision (Greg): ?BCAST,RESET opens every port, device-owned ones included. Removed the device-port re-block (#41); message now says '(device ports included)'. Reflashed W1/W2; full run 20260921-105657 started in the GUI. hcr.status_reply_parse PASS after its age= wildcard stopped swallowing vage=. |
| 2026-09-21 | Reruns on the final image: CLI 20260921-102109 (63 tests: 61 PASS; only bcast_reset_persists [awaiting decision] and baud_on_raw_mapped_port [#16]) + GUI 20260921-102817 (36 tests: 35 PASS). Every regression fix, #56, #32, all 6 hcr.* test updates, the ota/#L1/maestro/client_mesh/pwm test updates PASS; wcb.mgmt_pull, wdp.controller_auto_enable and dfp.config_frames pass on rerun (one-off misses). Last red: hcr.status_reply_parse - STATUS gained rx=/vage= (other session); test updated, not yet rerun. gui.py gained --run <globs> (opens + auto-starts); run.py forces UTF-8 stdout. |
| 2026-09-21 | Rerun 20260921-100408 crashed on a cp1252 console encode (runner, not a test; rerun with PYTHONIOENCODING=utf-8). Up to the crash: mgmt_pull + map.text_* + clear_all_reaches_remote PASS. Found: #46 CLEAR,ALL never restored OUTPUT (fixed); W1 **S3** flags also left OFF by the 09-21 #46 regression (restored ON/ON - caused bcast_json_local_only + input_port_loses_output + a set_flags skip); pwm.wdp_autoconfig: CLEAR,ALL now clears W2 directly, so the self-heal step accepts either route. WcbCmd.h WCBCMD_VERSION still said 0.8.0 after the 0.9.0 commit - bumped in both copies. Reflashed W1/W2. |
| 2026-09-21 | **Full run 20260921-085938: 27 FAIL / 312 PASS / 59 SKIP** (09-16: 66 fail+error; (should) fails 53 -> 4). Classified all 27. **Firmware:** #56 new (ETM CHAR loop-time broadcasts gated by lastReceivedViaESPNOW), #32 re-fixed (quiet param was never applied). **Tests out of date with deliberate changes:** 6 hcr.* (one-frame poll <QM,QD,QVV,QVA,QVB>, 3 s unseeded retries, STATUS/debug never transmit, volume 0-100 - the other session's HCR/WcbCmd 0.9.0 work), ota.local_parse_help_unknown (#30 hint), navicore.cli_codes (#L1 profile), maestro.get_edge_cases (#29 normalises ',00000000005' to channel 5), client_mesh.adopt_identity_route ('(boot)' since #11), pwm.wdp_autoconfig_and_selfheal (deferred reboot opens a window for W1's PWMTARGET advert - setup now waits + retries). **Already fixed mid-run:** 5 map.* + input.bcast_json_local_only (#46/#37/#50 regressions). **Known/deferred:** soft_rx_under_soft_tx, soft_rx_baud_sweep, map.baud_on_raw_mapped_port (#16), get_fallback_unicast_within_wcbq (#52). **Open:** input.bcast_reset_persists (needs Greg's call), wcb.mgmt_pull + wdp.controller_auto_enable + dfp.config_frames (single misses, rerunning). Reflashed W1/W2, restored W1 S2 BCAST ON/ON. |
| 2026-09-21 | **Bench run on the new images caught three of my own fixes.** #46 (config-corrupting): re-issuing a mapping recorded the mapping's own disabled flags as 'previous', so CLEAR restored OFF and left the port dark - now records only when the slot is first claimed. #37: the separator was added to every remote line, changing wire bytes and spending a CHKSM char - now only when the payload starts with ','. #50 WCB half REVERTED: the WCB already sends target-0 JSON as an untracked ETM frame (bestEffortTelemetry); the double-processing was WCB_Client-only. Open: input.bcast_reset_persists is a design choice (device ports kept blocked vs RESET = all open) - Greg to decide; wcb.mgmt_pull got no reply at t=3.8 s but passed in 4 prior runs and MGMT works later in the same run, so re-run alone before calling it. |
| 2026-09-21 | Worked the whole list. **49 of 55 fixed**, 6 DEFERRED with reasons (#14, #16, #33, #47, #52, #55 - each needs the bench or a decision, see its entry). All three repos compile clean: WCB 69% flash / 106032 B RAM, NaviCore 6% of 16 MB, WcbCmd + WCB_Client via both. **Nothing hardware-verified - the bench was powered down for this session and the previous one.** Cross-repo: WcbCmd (#2, #21) and WCB_Client (#11, #50) are changed in BOTH the canonical repo and the Arduino-Code sketchbook copy; per CLAUDE.md rule 1 push WcbCmd before the firmware, since CI clones it. |
| 2026-09-20 | Third pass: #13 (ETM setters validated in both spellings + load-time clamp) and #18 (token-aware timer-splitter exemption, documented in SEQUENCE_INVENTORY.md). Thirteen defects fixed, all compile clean (68% flash / 106008 B RAM). Bench still powered down all session - nothing hardware-verified. |
| 2026-09-20 | Second pass, bench still offline: fixed #4 (one prefixCharOk validator for all four prefix setters), #5 + #6 (Kyber ids 1-8; port validated before kyberPort/kyberUseTargeting are assigned, REMOTE included), #17 (staged serial mapping + bare ,R refused) and #15 (staged PWM mapping). Eleven defects fixed in total, all compile clean at 68% flash / 106008 B RAM. Still none hardware-verified. |
| 2026-09-20 | Also fixed #1 (DFP port guard, five sites + three missing externs) and updated CLAUDE.md rule 11 to name both deferred-restart flags. Final build: 68% flash / 106008 B RAM, zero warnings. Six defects fixed, none hardware-verified. |
| 2026-09-20 | Fixed #9, #8, #10, #12, #3 — all compile clean (68% flash, 106008 B RAM). **None verified on hardware:** the bench was powered down (all five COM ports FileNotFoundError), so the flash failed silently and a `pwm.*` run errored on port-open. Boards still run pre-session firmware. When the bench is back: flash both from `tests/hil/results/builds/wcb-esp32-meshq`, then run `pwm.*` (#8/#9/#10) and `etm.acked_not_executed_reboot`, `etm.dedup_ring_covers_pending`, `etm.pending_table_evict` (#12/#3). The harness changed with the firmware in three places (`_inline_clear_out`, `clear_out_keeps_queue`, `hil/wcb.py reboot()`), so those fail against old firmware until the boards are flashed. |
| 2026-09-20 | Resumed. Greg had committed the harness plus the half-done #9 (WCB_PWM.{h,cpp} returned bool, but WCB.ino still rebooted inline) as `eb82f29`, and fixed HCR readback/graceful stop in `0fed82b` + `6aeee6a` — recheck #19/#20/#24 against those before touching them. Completed #9. |
| 2026-09-16 | Analysis complete: 57 failures → 55 defects, ranked below. Nothing fixed yet. |


## The list


### High impact (18)


#### 1. A configured DFPlayer's serial port is unprotected — HCR, MP3, WLED, PWM and Kyber will all take it, and both configs then persist

| | |
|---|---|
| **Status** | VERIFIED — conflict.dfp_port_unguarded PASS on the bench (20260922-125537) |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `conflict.dfp_port_unguarded` |
| **Subsystem** | devices-hcr-dfp |

**Mechanism.** `isSerialPortUsedForDFP()` is defined at WCB_DFP.cpp:63 and declared at WCB_DFP.h:61, and the runtime paths do use it (`;P` passthrough WCB.ino:6936, broadcast-out skip :7074, `processIncomingSerial` skip :7116). But it appears in NONE of the five config-time port guards: WCB_HCR.cpp:649-652, WCB_MP3.cpp:314-317, WCB_WLED.cpp:326-329, WCB_PWM.cpp:78 (`canUsePWMOnPort`) and WCB_Storage.cpp:1203-1206 (Kyber LOCAL). DFP's own guard (WCB_DFP.cpp:267-271) checks all five of the others, so the asymmetry is entirely one-directional. `hcrReservePort` then calls `saveHCRSettings()` (WCB_HCR.cpp:520) while `dfpConfig` is already saved, so both configs land in NVS and both are emitted into `?backup` (the DFP token is built at WCB_DFP.cpp:367).

**How it is reached.** `?DFP,S4` then `?HCR,PORT,S4:9600` — the second is accepted. Session log 5492.606-5492.792: `[DFP] Configured: S4 at 9600 baud` … `[HCR] Configured on S4 at 9600 baud`, no complaint. Any builder who moves an HCR, MP3 strip, WLED or PWM output onto a port a DFPlayer already occupies (or forgets which port it is on) hits this on the first command.

**Why it matters.** Two drivers end up on one UART, persisted. Both write (HCRVocalizer text frames interleaved with 10-byte DFPlayer frames = garbage to both devices) and both read: `processDFPResponses()` → `dfpCodec.poll()` and `processHCRTick()` → `_hcr->update()` race for the same RX bytes, so ONFIN callbacks and HCR status parsing both break. Worse, the intruder's CLEAR then re-enables broadcast IN and OUT and wipes the label on the still-configured DFPlayer port (log 5493.787: `✓ Re-enabled broadcast output on S4` / `input on S4` / `Serial4 label set to: ''`), leaving the saved config internally inconsistent — and it survives reboot because both halves are in NVS. Recovery is a dead end for the user: `?D …

**Fix.** Add `isSerialPortUsedForDFP(serialPort) ||` to each of the five guards and add DFP to each message string: WCB_HCR.cpp:649-653, WCB_MP3.cpp:314-318, WCB_WLED.cpp:326-330, WCB_PWM.cpp:78-79, WCB_Storage.cpp:1203-1207. The declaration is already in WCB_DFP.h:61; WCB_Storage.cpp externs the peers itself at :28, so add one matching `extern bool isSerialPortUsedForDFP(int port);` there. Five one-line edits plus five string edits.

**Risk.** A board that ALREADY has the overlapping config saved keeps it — these are config-time guards only, and WCB_Storage.cpp:1201-1202 documents that loadKyberSettings() reads NVS directly and never replays the guard. So the change cannot reject an existing saved config at boot and cannot brick a board on upgrade; the overlap just stops being creatable. Watch that `?DFP,S<same port>` re-config still works (the DFP guard deliberately excludes itself — keep that property when mirroring it into the others).


#### 2. A truncated DFPlayer frame swallows the next good one — the ONFIN track-finished callback is lost with no diagnostic

| | |
|---|---|
| **Status** | VERIFIED — dfp.partial_frame_resync PASS on the bench (20260922-125537) |
| **Owner** | `WcbCmd` |
| **Effort** | S |
| **Tests** | `dfp.partial_frame_resync` |
| **Subsystem** | devices-hcr-dfp |

**Mechanism.** `DfPlayerCodec::poll()` in WcbCmd/src/WcbDfPlayer.cpp:170-206. The comment at :177-178 states the requirement verbatim — "a partial or corrupt frame must not swallow the next good one (a DFPlayer emits unsolicited init/finish frames at will)" — but the implementation at :179 is `if (_rxLen == 0 && b != 0x7E) continue;`, which only resyncs while the buffer is empty. `_rxLen` is a member (WcbDfPlayer.h:82) that persists across poll() calls with no inter-byte timeout. After a partial `7E 01 02`, `_rxLen` is 3; the next real finish frame's first 7 bytes fill the buffer to 10, `_rx[9]` is 0x05 rather than 0xEF so the frame is dropped at :184, and the finish frame's remaining 3 bytes are then discarded by the resync. Nothing is printed — `[DFP] Track n finished` (:191) never runs, and `onFinished` (:192, wired at WCB_DFP.cpp:47 to `recallCommandSlot`) never fires.

**How it is reached.** A DFPlayer sends init (0x3F) and track-finish (0x3D/0x3C) frames unsolicited, so the WCB is always mid-stream on a line it does not control. One lost or spurious byte — module power-up, a re-plug, a noisy line, or a soft-serial RX drop on S3-S5 under mesh load (CLAUDE.md rule 13; `hcr.softserial_integrity_mesh_load` in this same suite shows real corruption on W2 S5 at 300 frames) — leaves `_rxLen` stuck non-zero forever, because there is no timeout that clears it. Proven directly: probe sends `7E0102` then `7EFF063D000005FEB9EF` (log 5467.605 / 5467.740) and the finish is never reported.

**Why it matters.** Every `;D,PLAY,n,ONFIN,<key>` chain depends on that one frame. When it is eaten, the chained sequence silently never runs — no error, no console line, nothing in `?DFP,LIST`. The builder sees "the sequence sometimes stops after the sound" and goes looking at the sequence, the mesh or the recall key, none of which are at fault. It is also self-perpetuating in the sense that once armed, the parser eats the next frame no matter how long later it arrives.

**Fix.** In WcbCmd/src/WcbDfPlayer.cpp:170-206, validate the fixed header as bytes arrive and re-anchor on 0x7E, so a bad byte cannot hold the buffer hostage: ```cpp if (_rxLen == 0) { if (b != 0x7E) continue; } else if ((_rxLen == 1 && b != 0xFF) || (_rxLen == 2 && b != 0x06)) { _rxLen = 0; if (b != 0x7E) continue; // this byte may itself start the next frame } _rx[_rxLen++] = b; ``` That alone fixes the observed case. Optionally add a stale-partial timeout for the `7E FF 06 <silence>` case: a `uint32_t _lastRxMs` next to `_rxLen` (WcbDfPlayer.h:82) and `if (_rxLen && millis() - _lastRxMs > 200) _rxLen = 0;` at the top of the byte loop. 200 ms is ~20x a 10-byte frame at 9600 baud and far below the gap between unsolicited frames. `Arduino.h` is already included (WcbDfPlayer.h:2). Push WcbCmd to GitHub first, then rebuild the WCB (CI clones the repo — rule 6).

**Risk.** The header check assumes every device→host frame is `7E FF 06 …`, which holds for all four frames the switch at :188-203 handles (0x3C/0x3D/0x3F/0x40) and for the ACK/query replies it already ignores. If some clone module (YX5300/MH2024K) uses a different version byte, its frames would now be dropped instead of parsed — worth a bench check against Greg's actual module before pushing, since the codec explicitly claims clone compatibility (WcbDfPlayer.h:5-6). NaviCore compiles this file but never calls `poll()` (NaviCore.ino:1878 says so explicitly), so its blast radius is WCB-only. `dfp.rx_frames` must stay green — it deliberately feeds a bad-checksum frame and junk (`0011227EFF063C0000050000EF`) and expects it to parse, which the header check still allows since the checksum is never verified.


#### 3. The receiver's duplicate ring (8) is smaller than the sender's pending table (10), so a burst of 9+ ensured commands re-executes on every retry

| | |
|---|---|
| **Status** | VERIFIED — etm.dedup_ring_covers_pending PASS on the bench (20260922-125537) |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `etm.dedup_ring_covers_pending` |
| **Subsystem** | etm-mesh |

**Mechanism.** `ETM_PENDING_MAX` is 10 (WCB.ino:541) — ten ensured commands can be in flight at once, each retried up to 3 times reusing its original seq (WCB.ino:1351-1420). The receiver dedups against a per-sender ring of only `ETM_SEQ_HISTORY = 8` (WCB.ino:591-592, checked at WCB.ino:4355-4359, written at 4363-4367). With 9 distinct seqs alive from one sender, pushing the 9th evicts the 1st, so the 1st's retry reads as new and executes again — and since each retry re-inserts its own seq, the ring stays permanently one short and every round re-executes every command. The bench measured exactly that: 8 in flight -> [1,1,1,1,1,1,1,1] (the control passes); 9 in flight -> [4,4,4,4,4,4,4,4,4], i.e. original plus all three retries, nine times over. Nothing in the source ties the two constants together — the ring's comment (WCB.ino:586-590) explains why it is a ring rather than a single slot but never mentions the pending table's depth. Important: this does not need total ACK loss. One lost ACK inside a 9-command burst is enough — all 9 seqs are already in the ring, the oldest has been evicted, and that one command's retry executes a second time.

**How it is reached.** Any '^'-chain or stored sequence with 9 or more steps addressed to one remote board — a `;W2,...^;W2,...` chain from the console or Wizard, a `?SEQ` body that drives a remote board, a Maestro or HCR burst — combined with ordinary RF ACK loss. The duplicate execution is the damage: a `;S2` payload written twice to a Magic Panel, an MP3 retriggered, a Maestro script fired 2-4 times.

**Why it matters.** Corrupts commands in normal use, and does it *because* of the reliability layer — the retry that exists to guarantee one delivery produces several. It is also silent: the sender reports a clean ensured delivery, and the only symptom is a device that did the thing more than once.

**Fix.** Raise `ETM_SEQ_HISTORY` (WCB.ino:591) to 16 and tie it to the sender's depth so this cannot silently reopen: `static const int ETM_SEQ_HISTORY = ETM_PENDING_MAX + 6;` plus `static_assert(ETM_SEQ_HISTORY > ETM_PENDING_MAX, "dedup ring must outlast every retryable in-flight seq")`, with a comment saying why the two are coupled. Cost is 20*16*2 = 640 B of static RAM (up from 320 B), no wire change, no behaviour change below 9 in flight. Durable alternative if Greg wants it: adopt WCB_Client's window instead of a ring — `cmdSeqHigh` (uint16) + `cmdSeqMask` (uint32) per sender (WCB_Client.h:272-277) gives a 32-deep backlog in 6 bytes per peer and is already proven on the other side of the same protocol.

**Risk.** Low. A larger window holds a seq longer, so a genuinely NEW command reusing a seq within 16 (only after the uint16 counter wraps at 65535, or across an unannounced sender restart — defect 1) would be dropped instead of run; the 8-entry ring has the identical exposure, half as wide. Re-run etm.pending_table_evict, which asserts exact retry and failure counts at the 11-in-flight boundary, and the 8-command control inside etm.dedup_ring_covers_pending.


#### 4. ?CMDCHAR, legacy ?CC and legacy ?LF set the prefix characters with none of ?FUNCCHAR's guards — a natural typo (?CMDCHAR,?) kills the whole ';' family and persists to NVS

| | |
|---|---|
| **Status** | VERIFIED — chars.cmdchar_lfi_guard, chars.legacy_lf_guard PASS on the bench (20260922-125537) |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `chars.cmdchar_lfi_guard`, `chars.legacy_lf_guard` |
| **Subsystem** | input-chars |

**Mechanism.** ?FUNCCHAR is the only one of the four prefix setters that validates. WCB.ino:5695-5721 rejects a value equal to CommandCharacter (:5702-5707) and rejects control/space characters (:5710-5713). Its three siblings assign blind: * ?CMDCHAR — WCB.ino:5724-5733: `CommandCharacter = args.charAt(0); saveLocalFunctionIdentifierAndCommandCharacter();` with no checks at all. * legacy ?LF<c> — dispatched at WCB.ino:5849-5850 into updateLocalFunctionIdentifier (WCB.ino:6065-6073): `LocalFunctionIdentifier = message.charAt(2)` + save. * legacy ?CC<c> — dispatched at WCB.ino:5854-5855 into updateCommandCharacter (WCB.ino:6075-6083): same shape. All three write NVS, so the state survives reboot. What that state does: handleSingleCommand tests the function identifier FIRST (WCB.ino:4970), then the command character (:4974), then falls through to broadcast (:4979). So `?LF;` (LFI := ';') routes every ';' device command into processLocalCommand's legacy catch-alls, and `?CMDCHAR,?` (CmdChar := '?') leaves nothing matching ';' at all — every `;M1…`, `;L1…`, `;S3…`, `;C<key>` line drops through to processBroadcastCommand and is emitted as plain text out every serial port and over the mesh. The firmware's own help already states the contract the code does not enforce: WCB_Help.cpp:995 "Must not conflict with LocalFunctionIdentifier (?)" for CMDCHAR, and WCB_Help.cpp:970 / :998 advertise the legacy `?LFx` / `?CCx` forms as supported. Run evidence (session.log:82451-82453): `?LF;` → "LocalFunctionIdentifier updated to ';'"; (session.log:83070-83071) `?CMDCHAR,?` → "Command character updated to '?'".

**How it is reached.** ?CMDCHAR and ?FUNCCHAR are documented commands with their own help pages, and the legacy ?LFx/?CCx forms are documented there too. The sharpest path is a plain typo: a user who types `?CMDCHAR,?` meaning "what is my command character?" gets it SET to '?' instead of a help dump — because CMDCHAR is on the dataBearingVerb exemption list (WCB.ino:5010-5011) that deliberately disables the trailing-'?' help shortcut for it. `?CMDCHAR ?` and `?CMDCHAR?` both print help; `?CMDCHAR,?` silently breaks the board. Same for `?LF;` typed by anyone following the legacy-command line in `?FUNCCHAR ?`.

**Why it matters.** Every ';' command on the board stops working, persists across reboot, and there is no error message — under ?CMDCHAR,? the commands are not even rejected, they are broadcast as text to every serial port and every peer, so a droid silently starts spraying `;M11` at its Teeces and Magic Panel instead of moving a servo. Recovery exists (`;FUNCCHAR,?` / `?CMDCHAR,;`) but requires knowing the trick; otherwise it reads as a dead board and ends in ?ERASE,NVS.

**Fix.** Extract the two ?FUNCCHAR guards (WCB.ino:5702-5713) into one validator and run all four entry points through it: `?CMDCHAR` (WCB.ino:5724-5733) must reject `nc == LocalFunctionIdentifier` and `nc <= ' ' || nc == 0x7F`; updateLocalFunctionIdentifier (WCB.ino:6065-6073) and updateCommandCharacter (WCB.ino:6075-6083) must call the same validated setters instead of assigning directly. Keep '?' legal for FUNCCHAR — it is the documented recovery (`?FUNCCHAR,?`, WCB_Help.cpp:963) and does not collide with ';'.

**Risk.** A restore chain sets FUNCCHAR before CMDCHAR (printBackupConfig, WCB.ino:3240-3241), so the new CMDCHAR check validates against the just-applied funcChar — I checked that ordering and it is safe. The one behaviour change is that a board already in the broken state (CmdChar == LFI in NVS) will now refuse the command that would re-create it; that is the point, but a board already stuck there still needs the existing recovery command, so do not also add a guard that rejects the recovery path. Watch that ?CMDCHAR,? now prints a rejection rather than being swallowed.


#### 5. ?KYBER accepts Maestro id 9 and writes it as a real slot, which permanently breaks every ;M9 ("all local Maestros") command and cannot be cleared

| | |
|---|---|
| **Status** | VERIFIED — kyber.target_id9_rejected PASS on the bench (20260922-135805) |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `kyber.target_id9_rejected` |
| **Subsystem** | maestro-kyber |

**Mechanism.** `storeKyberSettings` validates Kyber target ids as `maestroID >= 1 && maestroID <= 9` (WCB_Storage.cpp:1319, with the matching diagnostic at :1402-1403) and then writes a full Maestro slot at :1362-1396. `configureMaestro` refuses 9 (WCB_Maestro.cpp:578, "9=all local, 0=all Maestros are reserved"), and so does WDP auto-add (:724). Once an id-9 slot exists, both `;M9<n>` and `;M9,<verb>` match it in the config-matched loop (WCB_Maestro.cpp:107-110 and :304-306) and `return` at :156 / :329 BEFORE the target-9 fan-out branches at :190 and :344 — so the board writes a literal device-9 Pololu frame to one port instead of re-addressing to each local Maestro's own id. Bench evidence: after `?KYBER,REMOTE,S1,M9:W1S1:57600`, `;M95` put `aa 09 27 05` on W1 S1 where `aa 01 27 05` was expected, and `;M9,goHome` put `aa 09 22` instead of `aa 01 22`. Worse, the slot is unremovable: `clearMaestroByID` rejects id 9 on both the bare-id path (WCB_Maestro.cpp:817) and the targeted path (:844), so `?MAESTRO,CLEAR,M9` and `?MAESTRO,CLEAR,M9:W1S1` both refuse. Only `?MAESTRO,CLEAR,ALL` gets rid of it, and that wipes everything.

**How it is reached.** A builder hand-typing a `?KYBER,LOCAL,S2,M9:W1S1:57600` target line, reasonably assuming 9 means 'all local' as it does in every `;M` command and in `?MAESTRO`'s own error text. The Wizard computes Kyber target lines from Maestro rows (see the comment at WCB_Storage.cpp:1325) so it would not emit 9 — this is a hand-typed / pasted-config path.

**Why it matters.** Silently destroys `;M9` board-wide: no local Maestro answers a device-9 frame, so every sequence that uses the 'all local Maestros' target stops actuating anything, with no error anywhere. And the user cannot undo it without wiping the whole Maestro map.

**Fix.** WCB_Storage.cpp:1319 — change `maestroID <= 9` to `maestroID <= 8`, and update the skip message at :1402-1403 to say 1-8. Nothing else creates a target with id 9 (`reconcileKyberTargetsFromMaestroConfigs` :1101 and the auto-populate at :1177-1189 both copy from `maestroConfigs[]`, which is already 1-8), so the narrowing is safe. Optional follow-up for the same root cause: have `storeKyberSettings` route its slot writes through `configureMaestro`'s validation instead of duplicating it.

**Risk.** Any existing board already carrying an id-9 slot keeps it — the fix stops new ones but does not clean up old ones, and clearMaestroByID still refuses 9. If you want those recoverable, also relax WCB_Maestro.cpp:817/:844 to allow a CLEAR of an out-of-range id that is actually present (clear-only, never add).


#### 6. A rejected ?KYBER,LOCAL,Sx still switches targeting on in RAM — on a Kyber_Local board that silently drops every sabre byte until reboot

| | |
|---|---|
| **Status** | VERIFIED — kyber.config_negatives PASS on the bench (20260922-135805) |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `kyber.config_negatives` |
| **Subsystem** | maestro-kyber |

**Mechanism.** `storeKyberSettings` sets `kyberUseTargeting = true` at WCB_Storage.cpp:1160, inside the params-parsing block, and only afterwards checks the port range at :1166-1169 (`Invalid Kyber port. Must be S1-S5` → `return`). The flag is left true. Bench evidence: after both `?KYBER,LOCAL,S6` and `?KYBER,LOCAL,S0` were refused, `?KYBER,LIST` reported `Targeting mode: Enabled` on a board that had been in broadcast mode. Consequence on a board that actually hosts the Kyber: `forwardDataFromKyber` takes the targeted branch at WCB.ino:4825 and iterates `kyberTargets[]` at :4837 — which the rejected command never populated (the auto-populate at WCB_Storage.cpp:1172-1196 sits after the `return`) — so every byte is read at :4823 and written nowhere. The sabre bridge goes dead with no message. The sibling test kyber.local_rejected_port_keeps_forwarding was written for exactly this and SKIPped this run (bench wiring), so that half is confirmed in source only. It is also not merely 'until reboot': `saveKyberTargets()` persists the flag (WCB_Storage.cpp:2168), and it is called from the end of any later successful `?KYBER` (:1518) and from WDP auto-add on a Kyber_Local board (WCB_WDP.cpp:718-720) — either can make the leaked true permanent.

**How it is reached.** A typo (`S6`, `S0`) or a stale pasted config line while setting up the Kyber port. On a Maestro_Remote board it is invisible except in `?KYBER,LIST` (it is not in `?backup`); on the board that actually has the sabre it kills the bridge.

**Why it matters.** A refused command changes live behaviour, and on the Kyber host it silently stops forwarding the sabre stream while the board reports itself as configured — the 'reports success for something that did not happen' category, inverted.

**Fix.** WCB_Storage.cpp:1150-1169 — parse into locals (`int wantPort; bool wantTargeting = true;`), run the port-range check against `wantPort`, and only assign `kyberPort` / `kyberUseTargeting` after every validation has passed. While there, apply the range check to the REMOTE branch too — :1166 is gated on `baseCommand.equals("local")`, so `?KYBER,REMOTE,S9,...` skips it entirely.

**Risk.** Low. The one thing to preserve is the no-comma path at :1132-1143, which deliberately sets `kyberUseTargeting = false` + `kyberPort = 2` for bare `?KYBER,LOCAL` / `?KYBER,REMOTE` — keep that branch's assignments unconditional.


#### 7. NaviCore's WCB_SEND answers ok:true even when the WCB_Client library refused to send anything

| | |
|---|---|
| **Status** | VERIFIED — navicore.wcb_send_ack_reflects_refusal PASS on the bench (20260922-125537) |
| **Owner** | `NaviCore` |
| **Effort** | S |
| **Tests** | `navicore.wcb_send_ack_reflects_refusal` |
| **Subsystem** | navicore-ota |

**Mechanism.** The direct-USB WCB_SEND handler calls the library and throws away its bool: NaviCore.ino:4036 `wcb->broadcast(cmd);` and NaviCore.ino:4039 `wcb->send((uint8_t)target, cmd);`, each followed unconditionally by `Serial.println("{\"type\":\"ACK\",\"ok\":true}")`. Both library calls return bool and have several real false paths: WCB_Client.cpp:388-393 refuses an over-length BROADCAST outright (fragmentation is unicast-only); WCB_Client.cpp:449/452/460 refuse a fragmented unicast that is too long, that hits an already-in-flight frag job, or that cannot malloc; WCB_Client.cpp:2277 returns false when the ETM pending table is saturated; and _transmit (WCB_Client.cpp:2311 `return esp_now_send(...) == ESP_OK`) returns false whenever the target MAC is not a registered ESP-NOW peer. That last one is the quiet killer: _registerPeers (WCB_Client.cpp:1628-1637) only registers ids 1.._quantity, plus learned and special peers — a valid-looking id above quantity that has never been learned is simply not a peer, esp_now_send returns ESP_ERR_ESPNOW_NOT_FOUND, nothing goes on the air, and NOTHING is printed. The session log (results/20260916-122250/session.log:93963-93964) shows the contradiction directly: `[WCB_Client] broadcast: command too long (188 > 187 chars)` immediately followed by `{"type":"ACK","ok":true}`. The bridged twin has the same hole — rc_telemetry.h:1521-1522 discards the return of broadcast()/send() and sets `ok = true` at :1523 — although it does at least validate the target range and empty cmd first, which the USB branch does not (NaviCore.ino:4031 takes `hdr["cmd"] | ""` and never checks for empty).

**How it is reached.** Config tool → WCB Send panel (config_tool/index.html:18651-18657), described in its own comment as the bring-up test path. It validates only 0-20, not against the configured quantity, so sending to a board id above quantity that has not been learned this boot is a normal typo/bring-up mistake: the command never leaves the board, no diagnostic is printed, and the terminal shows ok:true. Also hit by the ETM pending-saturation path under a burst, by a second long unicast issued while a fragmented send is still in flight (~0.5 s window, WCB_Client.cpp:452), and by any scripted/JSON client. The specific 188-char case the test uses is NOT reachable from the shipped UI — qs-cmd carries maxlength="6 …

**Why it matters.** It reports success for something that did not happen, and in the unregistered-peer case it does so with zero other output — the board is silent, the ACK says ok, and the operator goes off hunting the far end (wiring, power, the WCB's own config) for a command that never reached the radio. That is the exact 'silently reports success' bucket. Tempered honestly: the shipped tool does not display the ACK today, so the current visible cost is to the raw terminal, scripted clients and the HIL harness rather than to a GUI badge.

**Fix.** NaviCore.ino:4034-4042 — capture the bool and report it: `bool sent = (target == 0) ? wcb->broadcast(cmd) : wcb->send((uint8_t)target, cmd);` then `Serial.printf("{\"type\":\"ACK\",\"ok\":%s}\n", sent ? "true" : "false");`. Add the empty-cmd guard the bridged path already has (`if (!cmd[0]) → ok:false`) so the two transports agree. Apply the identical change at rc_telemetry.h:1521-1523: `ok = (to == 0) ? wcb->broadcast(_pendingWcbSendCmd) : wcb->send(to, _pendingWcbSendCmd);`. Keep the SUCCESS ACK byte-identical to `{"type":"ACK","ok":true}` — do not add a msg field to it. Optionally add a msg only on the false branch.

**Risk.** Two things to watch. (a) ok:false does not always mean 'nothing went out': WCB_Client.cpp:2270-2278 transmits best-effort and THEN returns false on pending-table saturation, so a client that auto-retries on ok:false can double-fire a command. Do not add retry-on-false to the tool without also distinguishing 'refused' from 'sent unguaranteed'. (b) Several passing HIL tests pin the success ACK as an exact string (s21_navicore_sbus.py:358, 374, 410, 434, 777) — any change to the true-branch text breaks them, so change only the false branch's content.


#### 8. ?MAP,PWM,CLEAR,ALL's remote commands are sent non-ETM and every ETM board silently drops them — the user is told it worked

| | |
|---|---|
| **Status** | VERIFIED — pwm.clear_all_reaches_remote PASS on the bench (20260922-125537) |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `pwm.clear_all_reaches_remote` |
| **Subsystem** | pwm |

**Mechanism.** WCB_PWM.cpp:9 re-declares the shared sender as `extern void sendESPNowMessage(uint8_t target, const char *message, bool useETM = false)` — but the real declaration, WCB.ino:977, defaults useETM to **true**. A default argument is per-translation-unit, so the two 2-argument calls inside clearAllPWMMappings — `sendESPNowMessage(wcb, remoteCmd)` at WCB_PWM.cpp:495 (the `?PX<n>`) and `sendESPNowMessage(wcb, "?REBOOT")` at :506 — go out as plain 249-byte packets. WCB.ino:4203-4225 drops any non-ETM packet on an ETM-enabled receiver unless it is raw serial, `;P`, `;M` or `{`-JSON; `?PX3` and `?REBOOT` match none of those, so the receiver never sees them. W1 nonetheless prints "All PWM mappings cleared" (WCB_PWM.cpp:523). WCB_PWM.cpp is the ONLY file with the divergent default — WCB_Maestro.cpp:24 and WCB_WLED.cpp:26 both use `= true`. Secondary: :494 and :506 hardcode the literal `'?'` while every other remote PWM command in the same file builds the prefix from LocalFunctionIdentifier (:319, :336, :367).

**How it is reached.** Any ETM-on fleet — which is effectively all of them, since CLAUDE.md rule 4 says WDP does not work with ETM off. Make a mesh PWM mapping (`?MAP,PWM,S3,W2S3`), then `?MAP,PWM,CLEAR,ALL` from the Wizard or a terminal. session.log:31224-32396 shows it exactly: at 4819.692 W1 prints "All PWM mappings cleared", W2 prints nothing at all, and 24 s later W2's ?backup still carries `?MAP,PWM,OUT,S3`.

**Why it matters.** Silent success for something that did not happen, and the leftover is permanent. The remote port stays reserved: broadcasts are skipped on it (WCB.ino:7043) and its UART is never begun at boot (WCB.ino:8083-8113), so the device wired there is dead. Because addPWMMapping declared the port with an explicit command, the slot is tagged manual (pwmOutputAutoSrc = 0, WCB_PWM.cpp:833), and reconcileWdpAutoPWMOutputs (WCB_PWM.cpp:842-865) only ever clears WDP-tagged ports — so self-heal will never recover it. Only a hand-typed `;W<n>,?MAP,PWM,CLEAR,OUT,S<p>` fixes it, and nothing tells the user it is needed.

**Fix.** Pass `true` explicitly at WCB_PWM.cpp:495 and :506, and delete the divergent default on WCB_PWM.cpp:9 — either match WCB.ino:977 (`= true`) or drop the default entirely so every call site in this file is explicit (:745 already passes `false` for the ;P hot path, so nothing else changes). While there, replace the `"?PX%d"` at :494 with `"%cMAP,PWM,CLEAR,OUT,S%d"` + LocalFunctionIdentifier so it matches :319/:336/:367 and does not depend on the legacy ?PX alias.

**Risk.** `?REBOOT` becomes an ACKed ETM command, so per CLAUDE.md rule 2 it now occupies a pending-ACK slot per board and retries up to 3x — send it AFTER the CLEAR,OUTs land, and note it becomes redundant once D2 is fixed (the receiver will restart itself via pwmRebootPending). This fix is also what makes D2's queue behaviour reachable: today the remote gets neither command, so fix D2 in the same change or the ?REBOOT and the second CLEAR,OUT will race.


#### 9. ?MAP,PWM,CLEAR,OUT and ?PX restart the board inline and unconditionally, destroying every command queued behind them — including the sibling CLEAR,OUT a multi-output mapping sends

| | |
|---|---|
| **Status** | VERIFIED — pwm.clear_out_keeps_queue, pwm.remove_clears_all_remote_outputs PASS on the bench (20260922-125537) |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `pwm.clear_out_keeps_queue`, `pwm.remove_clears_all_remote_outputs` |
| **Subsystem** | pwm |

**Mechanism.** Two handlers still take the restart inside the command handler that CLAUDE.md rule 11 forbids: WCB.ino:5137-5140 (`?MAP,PWM,CLEAR,OUT,S<n>` → removePWMOutputPort, then `Serial.println("Rebooting in 3 seconds…"); delay(3000); ESP.restart();`) and WCB.ino:5941-5944 (the `?PX<n>` alias, identical). Neither checks whether anything was actually removed — removePWMOutputPort is void and just prints "Serial%d was not configured as PWM output" (WCB_PWM.cpp:892) on a miss. Every other PWM path was already converted to the deferred `pwmRebootPending` flag honoured in loop() (WCB_PWM.cpp:313-315, :384-386, :528-531 → WCB.ino:8385-8392); these two were missed. The cross-board case: removePWMMapping ETM-sends one `?MAP,PWM,CLEAR,OUT,S<p>` per remote output back to back with no delay (WCB_PWM.cpp:362-375 — note addPWMMapping's equivalent loop at :337 does have a `delay(50)`); the receiver ACKs each, but the first one's inline restart kills the rest.

**How it is reached.** Local: any chained restore or config push containing the token — session.log 4578.999 `?MAP,PWM,CLEAR,OUT,S4^;S0,HILC866D8` printed "Serial4 was not configured as PWM output" and rebooted anyway, and the chained `;S0` never ran; `?PX4` at 4588.516 did the same. Cross-board: `?MAP,PWM,CLEAR,S3` for a mapping with two remote outputs — session.log 4906.795 shows W2 clearing S3 and rebooting, and S4 never being touched, leaving `?MAP,PWM,OUT,S4` on W2 permanently (manual tag, so no WDP self-heal). The Wizard hits the same thing: app.js:4119-4157 sends one CLEAR,OUT per removed remote destination in a tight await loop.

**Why it matters.** Loses commands in normal use with no error anywhere. The sender sees an ETM ACK (the receiver ACKs before running the command) and the boot banner satisfies the pusher's "did the board answer" test, so the push is scored fully delivered — exactly the failure mode the pwmRebootPending comment at WCB_PWM.cpp:306-312 was written to prevent. It also reboots a board for a no-op, which in a config-push stream costs a reboot plus everything behind it.

**Fix.** Make `removePWMOutputPort` return bool (true when a port was actually removed) — WCB_PWM.cpp:868-893, returning true before the `return` at :889 and false at :893. Then at WCB.ino:5137-5140 and :5941-5944 replace the inline restart with `if (removePWMOutputPort(port)) { pwmRebootPending = true; Serial.println("PWM output cleared — rebooting once the command queue drains."); }`. That single change fixes both tests: the no-op case stops rebooting, and the multi-output case lets the second CLEAR,OUT drain before loop() takes the restart. Optionally also add a `delay(50)` between the sends at WCB_PWM.cpp:369 to match addPWMMapping:338.

**Risk.** Callers that relied on the restart happening within 3 s now wait for PWM_REBOOT_QUIET_MS = 4000 ms of queue quiet (WCB.ino:968, :8386) — the Wizard's `setTimeout(() => boardPull(removedSlot), 10000)` at app.js:4152 still covers that, but re-check the HIL helpers `_inline_clear_out` and `_w2_reboot_wait` (tests/hil/suites/s14_pwm.py:35-49), which expect the literal "Rebooting in 3 seconds" / an ONLINE(boot) inside 40 s. Also confirm removePWMOutputPort's two other callers (removePWMMapping:364 and reconcileWdpAutoPWMOutputs:860) ignore the new return value — the self-heal path must keep NOT rebooting (WCB_PWM.cpp:758-760).


#### 10. ;P has no Maestro-port guard, so a PWM stream aimed at a board's Maestro port silently kills that Maestro until reboot while ;M keeps reporting success

| | |
|---|---|
| **Status** | VERIFIED — pwm.p_refused_on_maestro_port PASS on the bench (20260922-125537) |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `pwm.p_refused_on_maestro_port` |
| **Subsystem** | pwm |

**Mechanism.** processPWMOutput's owned-port guard (WCB.ino:6936-6942) covers MP3, DFP, HCR, WLED, raw-mapped ports and `Kyber_Local && port == kyberLocalPort` — but has no term for a Maestro port and no term for `Maestro_Remote` on S1. Execution falls through to the unconditional `pinMode(txPin, OUTPUT)` at WCB.ino:6954, which detaches the UART peripheral from that pin. canUsePWMOnPort (WCB_PWM.cpp:62-84), which DOES reject S1 under Kyber_Local/Maestro_Remote, is never called on the ;P path. There is no isSerialPortUsedForMaestro() helper anywhere (WCB_Maestro.h has none) — but the exact loop needed already exists inline at WCB.ino:7049-7056 in the broadcast path.

**How it is reached.** Two ways, and the second needs no typo. (a) Type `;P1<width>` on a board with a local Maestro on S1 — session.log 4503.567: after `;P11500`, the next `;M11` still prints "→ Maestro 1: Local S1, Script 1" but the probe sees no `AA012701` frame at all; the port stays dead until the reboot at 4505.635. (b) Map PWM at a remote board's Maestro port, e.g. `?MAP,PWM,S3,W2S1` — the Wizard's destination port dropdown offers a plain S1..S5 for any board (Wizard/app.js:4014-4017). W2 refuses the *declaration* (canUsePWMOnPort prints the refusal), but processPWMPassthrough sends `;P1<width>` to the target regardless of whether the declaration was accepted (WCB_PWM.cpp:743-745 has no such check), so the  …

**Why it matters.** Bricks a port until reboot AND reports success for something that did not happen: the ;M dispatcher matches, prints its routing line, and writes into a detached UART. A builder debugging "my dome Maestro stopped responding" has no signal at all pointing at PWM — the same class of failure CLAUDE.md rule 12 describes. The remote-mapping path (b) makes it recur on every boot, so it presents as intermittent hardware.

**Fix.** Add a Maestro term to the guard at WCB.ino:6936-6938, mirroring the loop already at WCB.ino:7049-7056: `bool isMaestroPort = false; for (int m = 0; m < MAX_MAESTROS_PER_WCB; m++) if (maestroConfigs[m].configured && maestroConfigs[m].serialPort == port) { isMaestroPort = true; break; }` and OR it in together with `(Maestro_Remote && port == 1)`. Keep the print gated on debugPWMPassthrough exactly as the existing branch is (:6939-6940) — this runs at passthrough rates. Better still, factor that loop into `bool isSerialPortUsedForMaestro(int port)` in WCB_Maestro.cpp, use it from BOTH WCB.ino:7049 and here, and add it to canUsePWMOnPort (WCB_PWM.cpp:78) so a Maestro on S3/S4/S5 also refuses the declaration.

**Risk.** The Maestro slot table holds remote proxies with serialPort == 0 (WCB_Maestro.h:48-52), so match on `serialPort == port` only — a `port` of 0 cannot reach here (WCB.ino:6921 rejects port < 1). Do NOT convert the guard into a call to canUsePWMOnPort: that function prints ungated (WCB_PWM.cpp:65, :71, :79) and would flood UART0 at passthrough rates. Also preserve the deliberate design recorded in the comment at WCB.ino:6931-6935 — the guard must keep blocking only OWNED ports and must never require a positive PWM declaration, or every remote-driven output stops working (pwm.p_forwarded_stats and pwm.passthrough_mesh would regress).


#### 11. A WCB_Client host that reboots has its first commands ACKed and silently thrown away as duplicates [owner: WCBClient repo]

| | |
|---|---|
| **Status** | VERIFIED — client_mesh.rejoin_seq_reuse PASS on the bench (20260922-125537) |
| **Owner** | `unclear` |
| **Effort** | M |
| **Tests** | `client_mesh.rejoin_seq_reuse` |
| **Subsystem** | etm-mesh |

**Mechanism.** OWNER IS THE WCBClient REPO (C:/Users/ghulette/Documents/GitHub/WCBClient/src/WCB_Client.cpp) — the schema's owner enum has no value for it. WCB_Client restarts its command sequence counter at every begin(): `_seqCounter = 0` (WCB_Client.cpp:77), first command therefore seq 1 (WCB_Client.cpp:2183-2184, declared WCB_Client.h:1089). The WCB keeps an 8-entry per-sender ring of recently-seen seqs (WCB.ino:591-592) and clears a sender's ring in exactly ONE place: on receipt of a PACKET_TYPE_ETM_BOOT announce (WCB.ino:4301-4305). WCB_Client defines that packet type (WCB_Client.h:52) and DECODES it from WCBs (WCB_Client.cpp:2704) but never sends one — grep for WCB_PACKET_ETM_BOOT in WCB_Client.cpp returns only the receive case. So after a client reboot its reused low seqs match stale ring entries, and WCB.ino:4369-4376 ACKs them and returns without executing ('Duplicate seq %d ... ACKd but not executed'). The ACK at WCB.ino:4348-4349 fires BEFORE the dup check, so the client's pending slot completes and it reports success. The loss window is arithmetic: the ring holds the last 8 seqs from that sender, {N-7..N}. A new session's seqs 1..8 collide iff N <= 15 — i.e. when the PREVIOUS session sent 15 or fewer commands to THAT board. Because it is per-sender per-receiver, a host that spreads 40 commands over 3 WCBs still leaves each board's ring in the bite zone. Log proof (results/20260916-122250/session.log:88187+): session A sends 3 MSENDs, all three land on probe1 (t=6395.809, 6396.496, 6397.121). Probe reboots and rejoins as the same id 18. Session B sends 4; the probe prints 'OK MSEND 1' for all four, and only the 4th (B3) reaches probe1 at t=6405.684. Three commands ACKed, zero executed.

**How it is reached.** Every time a WCB_Client host restarts while the WCBs stay up: re-flashing NaviCore, power-cycling it, a crash, MgmtRelay rebooting, SBUSController. Greg hits this on the bench constantly. The user sees 'the first couple of things I do after a reboot just don't happen, then it starts working' — and nothing anywhere logs a failure.

**Why it matters.** Loses commands in normal use AND reports success for them. It is self-healing after a few commands, which is worse than a hard failure: it trains the user to distrust the mesh generally instead of pointing at a cause. The design intent is unambiguous — the WCB's ring-clear-on-boot-announce exists precisely for this case (WCB.ino:4295-4300 spells out the scenario), the client's own receive-side window has the mirror clear (WCB_Client.h:277 seqResetMs), and only the client's SEND side was never wired up.

**Fix.** In the WCBClient repo: add `_sendBootAnnounce()` as a copy of `_sendHeartbeat()` (WCB_Client.cpp:1800-1815) with `structPacketType = WCB_PACKET_ETM_BOOT` and `structSequenceNumber = 0`, and send it redundantly in the first seconds after begin() — mirror the firmware exactly: 3 announces ~1200 ms apart (WCB.ino:1096-1105, `etmBootAnnouncesLeft` / 1200 ms; broadcast is unacknowledged, which is why it is sent more than once). Drive it from update() with a counter+deadline pair like the firmware's, not from begin(), so it does not block the join. Belt-and-braces second half: seed `_seqCounter` from `esp_random()` at WCB_Client.cpp:77 instead of 0 (keeping the skip-0 rule at 2183-2184) so a host whose announce is lost still misses the stale ring by ~65535/8. Do NOT try to fix this on the WCB side — the failing case leaves the peer continuously registered (the probe rejoined inside the 50 s temporary-peer TTL, so addTemporaryPeer's `!esp_now_is_peer_exist` branch at WCB.ino:7741 never re-runs), and WDP carries no boot or session counter, so the firmware has no signal to clear on.

**Risk.** The announce only clears the ring when the WCB already treats the sender as a peer (`wcbPeerActive[senderIdx] || isSpecialPeer`, WCB.ino:4281) — three spaced announces cover the case where the first arrives before the WDP advert is digested; consider also re-announcing once the first WCB heartbeat is heard. A client in a crash loop announcing more often than every 4 s will clear the ring repeatedly, which could let a genuine in-flight retry double-fire — the 4 s hold at WCB.ino:4301 bounds it, and WCBs already carry the identical exposure. Cross-repo release discipline applies: push WCBClient master, sync Arduino-Code/libraries/WCB_Client (it shadows locally), then rebuild NaviCore and tests/hil/wcb_probe.


#### 12. A remote ?reboot ACKs and then destroys every command queued behind it — the CLAUDE.md rule-11 trap, still live in four places

| | |
|---|---|
| **Status** | VERIFIED — etm.acked_not_executed_reboot PASS on the bench (20260922-125537) |
| **Owner** | `WCB_firmware` |
| **Effort** | M |
| **Tests** | `etm.acked_not_executed_reboot` |
| **Subsystem** | etm-mesh |

**Mechanism.** `reboot()` is `Serial.println(...); delay(2000); ESP.restart();` (WCB.ino:827-831) and is called straight out of the command handler at WCB.ino:5861. An ETM-received command is ACKed on the WiFi task (WCB.ino:4348-4349) and only ENQUEUED for loop() (WCB.ino:4549). `delay()` runs on core 1; `espNowReceiveCallback` runs on the WiFi task on core 0, so during the whole 2 s window the board keeps accepting commands, ACKing them, and recording their seqs in the dedup ring (WCB.ino:4363-4367) — then `ESP.restart()` discards the queue. The sender sees 'Seq N fully acknowledged' and never retries. The test proves both halves: `;W2,?reboot` -> fully acknowledged; 0.3 s later `;W2,;S2<marker>` -> also fully acknowledged, and the marker never appears on W2's S2, while the post-boot control command does. Three more sites take an inline restart from inside a command handler, two of them with a 3-second window: `?MAP,PWM,CLEAR,OUT,S<n>` (WCB.ino:5138-5140) and its legacy alias `?PX<n>` (WCB.ino:5942-5944), plus `eraseNVSFlash()` (WCB_Storage.cpp:1084-1086). The PWM one is the nastiest — the comment at WCB.ino:5133 says the Wizard sends it 'when a remote dest is deleted', i.e. in the middle of a config push, which is exactly the stream-of-commands case rule 11 was written about. The correct pattern is already in the tree and already carries the post-mortem comment: `pwmRebootPending` (WCB_PWM.cpp:25, WCB_PWM.h:38-42) honoured by loop() at WCB.ino:8385-8392 once the queue is empty AND quiet for PWM_REBOOT_QUIET_MS (WCB.ino:968). WCB_Storage.cpp:1074-1078 even records that clearAllPWMMappings' inline restart was already moved onto that flag once.

**How it is reached.** Rebooting a remote board from the Wizard terminal or the USB console (`;W2,?reboot`) is routine — after a config change, after a firmware setting, as the standard 'make it take effect'. Anything sent in the next 2 s is ACKed and lost. The PWM variant is worse because the Wizard itself issues it mid-push with a 3 s window, and the push is scored fully ACKed.

**Why it matters.** Silently reports success for something that did not happen, and loses queued commands — the exact failure CLAUDE.md rule 11 documents as having already cost a debugging session ('the pusher cannot tell, because the boot banner satisfies its did-the-board-answer test, so nothing retries and the push is scored as fully ACKed'). The user's config push looks clean and the board comes up with part of it missing.

**Fix.** Add a general `rebootPending` volatile beside `pwmRebootPending` (or reuse `pwmRebootPending` and generalise the loop() message) and set it instead of restarting inline: WCB.ino:827-831 `reboot()` becomes `Serial.println("Reboot queued — restarting once the command queue is quiet"); pwmRebootPending = true;`; same substitution at WCB.ino:5138-5140, WCB.ino:5942-5944, and WCB_Storage.cpp:1084-1086. loop() at WCB.ino:8385-8392 already does the quiet-window wait, the Serial.flush() and the 150 ms UART drain — no new machinery. Leave WCB_OTA.cpp:310 and :429 alone (post-flash restarts, not command handlers) and leave the boot guard at WCB.ino:7580.

**Risk.** `?reboot` stops being 'restarts in exactly 2 s' and becomes 'restarts after 4 s of quiet' — anything waiting a fixed time for the boot banner needs slack: the HIL harness's `w.reboot()` (tests/hil/suites/common.py), the Wizard's reboot button, and any OTA or config flow that reboots then immediately resumes. Second-order: a board being hammered with commands defers its restart indefinitely; that is intended, but the console line should say so or it reads as a hang. Also ensure `eraseNVSFlash()` cannot be re-entered while the flag is set.


#### 13. ETM numeric setters have no validation and no query form: a bare ?ETM,TIMEOUT / BOOT / DELAY and every legacy ?ETM<key> spelling persist 0 to NVS

| | |
|---|---|
| **Status** | VERIFIED — etm.unvalidated_setters, etm.legacy_miss0_rejected PASS on the bench (20260922-125537) |
| **Owner** | `WCB_firmware` |
| **Effort** | M |
| **Tests** | `etm.unvalidated_setters`, `etm.legacy_miss0_rejected` |
| **Subsystem** | etm-mesh |

**Mechanism.** One root cause, two spellings — the range checks were retrofitted key-by-key and most keys were missed. That is why these two tests collapse into one defect and one fix. New form (WCB.ino:5281-5360): `TIMEOUT` (5296-5299), `BOOT` (5330-5333) and `DELAY` (5338-5341) each do `etmVal.toInt(); saveETMSettings();` with no length test and no range test. A bare `?ETM,TIMEOUT` leaves etmVal == "", toInt() gives 0, and 0 goes to NVS. `?ETM,TIMEOUT,-5` stores -5. `HB` (5305-5316) and `MISS` (5318-5329) are the same code with a query form and a range check bolted on, and the comment at WCB.ino:5301-5304 states in plain words that this exact trap already bit once ('written straight to NVS and turned the board into a ~4 Hz heartbeat emitter that survived reboots'). `COUNT` is `constrain(...,10,200)` (5335) and is correct — the test asserts that half passes, deliberately. Legacy form (WCB.ino:5973-6004): `?ETMHB<n>` got the guard (5991-6000, with its own comment 'Same 0-writes-to-NVS trap as ?ETM,HB above'), and its four neighbours did not — `?ETMCHARDELAY` (5977-5980), `?ETMTIMEOUT` (5983-5986), `?ETMBOOT` (5987-5990) and `?ETMMISS` (6001-6004) all take `message.substring(n).toInt()` unchecked. Bare `?ETMMISS` gives substring("") -> 0 just like `?ETMMISS0`. What the bad values do: - etmMissedHeartbeats = 0 -> `offlineThresholdMs = (etmHeartbeatSec+1) * 0 * 1000 = 0` (WCB.ino:1114), so `millis()-lastSeenMs > 0` is true for every peer on every sweep: the whole fleet is marked OFFLINE (the bench log shows '[ETM] WCB2 went OFFLINE (no heartbeat for 0s)'), and `etmAddToPendingTable` skips every offline board at WCB.ino:1278, so no expectAckFrom bit is ever set — every ensured send silently degrades to fire-once with no retry, no ACK tracking and no failure report. - etmTimeoutMs = 0 -> t …

**How it is reached.** Typing the bare command to ASK what a setting is. There is no query form for TIMEOUT/BOOT/DELAY and no `?ETM,TIMEOUT?` — `?ETM,HB` answers ('ETM heartbeat is 10 sec') precisely because someone already did this, so the bare form reads as the house style. `?ETMMISS` / `?ETMMISS0` are in the legacy vocabulary that older configs and the wiki still use.

**Why it matters.** MISS=0 silently removes ETM's delivery guarantee fleet-wide while every board reports OFFLINE — the user is now debugging a 'dead mesh' whose commands mostly still arrive. TIMEOUT=0 makes every ensured command report FAILED. Both survive reboot, both ride into ?backup, and the trigger is a query the user believed was read-only. This is 'corrupts saved config' plus 'silently reports success for something that did not happen' in one command.

**Fix.** Route every ETM numeric key through one validated setter instead of repeating the pattern. In the `?ETM` block (WCB.ino:5296-5341) give TIMEOUT / BOOT / DELAY the shape HB and MISS already have: empty etmVal -> print the current value and return; otherwise parse, range-check (TIMEOUT 50-10000 ms, BOOT 1-30 s, DELAY 0-5000 ms — an explicit DELAY 0 is legitimate, it is only the *bare* form that must not write it), reject with the existing `Invalid ETM <name> '<val>'. Use <lo>-<hi>.` wording, and only then save. Mirror it into the legacy branch for `?ETMTIMEOUT` (5983), `?ETMBOOT` (5987), `?ETMCHARDELAY` (5977) and `?ETMMISS` (6001), copying the `?ETMHB` guard at 5991-6000 including its 'currently %d' phrasing so etm.legacy_forms keeps passing. Leave COUNT's constrain alone. Add a load-time clamp in WCB_Storage.cpp:2317-2326 so a board already holding 0 from this bug heals on the next boot — mirroring the defensive clamp the heartbeat path already carries (WCB.ino:1080-1084, 'belt-and-braces for a value restored from an NVS blob written by older firmware'). This one earns a docs line: it is a trap someone has now walked into twice.

**Risk.** Rejecting out-of-range values changes what a restore does with an already-corrupted backup chain — it will now print errors instead of silently re-applying 0, which is the point, but the console line should say so. etm.legacy_forms asserts the exact strings for ?ETMHB0 / ?ETMHB12 / ?ETMTIMEOUT650 / ?ETMBOOT3 / ?ETMCHARCOUNT5 / ?ETMCHARDELAY150 / ?etmmiss6 and the ?config rendering — keep those byte-identical. A TIMEOUT floor set too high would break etm.offline_detection_timing and the ETM characterization tool, which use short timeouts deliberately; 50 ms is safely below anything the suite sets (the bench runs 500).


#### 14. With the Kyber on S3-S5, ports S1 and S2 go completely dead — no command reader and no broadcasts — because Kyber_Local still means "S1+S2" in two places

| | |
|---|---|
| **Status** | VERIFIED — kyber.local_port_s1_frees_s2 (the replacement test) PASS in 20260922-211114 and 20260922-215719 |
| **Owner** | `WCB_firmware` |
| **Effort** | M |
| **Tests** | `kyber.local_port_s1_frees_s2` (replaces `kyber.local_port_s3_frees_s2`, which can never run now that #28 refuses S3-S5) |
| **Subsystem** | maestro-kyber |

**Mechanism.** `serialCommandTask` computes the right predicate at WCB.ino:7378 (`const int kyberOwned = Kyber_Local ? kyberLocalPort : 0;`) and uses it correctly for S3-S5 at :7383-7385 — but the S1/S2 branch at :7388-7391 is still a bare `if (Kyber_Local) { /* skip both */ }`. Its comment ("the Kyber task drains both regardless of kyberLocalPort") is factually wrong: `forwardDataFromKyber` returns at WCB.ino:4812 unless `kyberLocalPort` is set and then reads only that one port (:4821-4822), and `forwardMaestroDataToLocalKyber` reads only configured LOCAL Maestro ports (:4900-4910). So with `kyberLocalPort == 3` and no Maestro on S2, S2 has no reader at all: serialCommandTask skips it, KyberLocalTask never touches it, RawSerialForwardingTask only handles raw-mapped ports (:7425-7426). Separately, `processBroadcastCommand` skips `(i <= 2 && Kyber_Local)` at WCB.ino:7042, so broadcast OUTPUT to S1 and S2 is suppressed too — overriding the user's persisted `?BCAST,OUT,S2,ON`, which `?backup` still reports as ON. Bench evidence: results/20260916-122250/session.log lines 99363-100219 — `?KYBER,LOCAL,S3` accepted, then a `;S4` typed into W1 S2 never executed and a USB broadcast never reached S2, while the S4 control paths both worked. Same code has a sibling hole: :7383-7385 skips only `kyberOwned`, not local Maestro ports, so a Maestro on S4 with the Kyber on S3 is drained by BOTH serialCommandTask and forwardMaestroDataToLocalKyber (`processIncomingSerial` filters MP3/DFP/HCR/query/reconfig at WCB.ino:7112-7128 but not Maestro ports) — byte-stealing, confirmable in source, not exercised by this run.

**How it is reached.** Any builder who puts the Kyber/sabre on something other than S2 — `?KYBER,LOCAL,S3` is an advertised form and the command accepts S1-S5 — and who also has a device on S1 or S2 that sends `;` commands or expects broadcasts (W1's own bench config has 'Magic Panel(IA)' labelled on S2 with BCAST ON). No error, no warning; the port simply stops existing.

**Why it matters.** Bricks up to two ports until the Kyber mode is changed, with no diagnostic and while `?backup` keeps reporting them enabled. A dead command port reads as 'the WCB is broken' — the exact failure mode CLAUDE.md rule 12 calls out as costing a debugging session.

**Fix.** WCB.ino:7388-7391 — delete the `Kyber_Local` special case for S1/S2 and gate those two ports the same way S3-S5 already are: skip when `kyberOwned == port`, and skip when the port hosts a configured local Maestro (the Kyber/Maestro-remote tasks own those bytes). Then apply the same Maestro-port skip to :7383-7385 so S3-S5 Maestros stop being double-read. WCB.ino:7042 — change `(i <= 2 && Kyber_Local)` to `(Kyber_Local && i == kyberLocalPort)`; configured Maestro ports are already excluded generically at :7048-7060, so nothing is lost. Fix the false comment at :7389-7391 in the same commit.

**Risk.** This changes who reads S1/S2 on every Kyber_Local board in the field, so the single-reader invariant must hold exactly: verify on the bench with the Kyber on S2 (the common case) that S2 is still read only by the Kyber task and S1 only by the Maestro bridge, before and after. The Maestro-port skip added to S3-S5 is the riskier half — get the 'is this a local Maestro port' helper right or you will silently stop parsing commands on a port that used to work.

**Update (2026-09-22).** #28 now refuses S3-S5, but #14 is still reachable through the other hardware port. With `?KYBER,LOCAL,S1` and no Maestro on S2, serialCommandTask still skips S1 AND S2 for any Kyber_Local board (WCB.ino ~:7620-7624, whose comment is still wrong). KyberLocalTask reads only `kyberLocalPort` and the local Maestro ports, so nothing reads S2. `processBroadcastCommand` also skips every port <= 2 whenever Kyber_Local is set (~:7240), overriding a persisted `?BCAST,OUT,S2,ON`. The mirror case is live too: Kyber on S2 with the Maestro on S3-S5 leaves S1 unread. **Designed fix (not applied, needs Greg's OK):** read S1/S2 when `kyberOwned != port` and the port is not raw-mapped (processIncomingSerial already drops bridge-owned Maestro ports, #59), fix the false comment, and change the broadcast skip to `(Kyber_Local && i == kyberLocalPort)`. The per-slot Maestro exclusion in the broadcast loop still protects configured Maestro ports. **Caveat:** a board with a real Maestro on S1 but NO `?MAESTRO` slot would start receiving ASCII broadcasts on S1. The new test `kyber.local_port_s1_frees_s2` pins the bug and fails until this is fixed.

**Applied (2026-09-22, Greg: "apply it").** serialCommandTask reads S1/S2 on a Kyber_Local board unless the port is `kyberLocalPort` or raw-mapped, and processBroadcastCommand skips only `kyberLocalPort`. A review confirmed the reader logic in every mode and transition, including `kyber.local_mode_s2`'s pre-reboot deaf window (the Kyber port stays unread until the reboot). Follow-ups it found are #73.

#### 15. An invalid re-map of a live PWM input wipes its output list in RAM: passthrough stops, ?MAP,PWM,LIST goes blank, and ?backup emits an unrestorable token

| | |
|---|---|
| **Status** | VERIFIED — pwm.invalid_remap_keeps_mapping PASS on the bench (20260922-125537) |
| **Owner** | `WCB_firmware` |
| **Effort** | M |
| **Tests** | `pwm.invalid_remap_keeps_mapping` |
| **Subsystem** | pwm |

**Mechanism.** addPWMMapping resolves the slot, then mutates it in place BEFORE validating: `PWMMapping &mapping = pwmMappings[slot]; mapping.inputPort = inputPort; mapping.outputCount = 0;` (WCB_PWM.cpp:232-234). If no destination parses, it prints "No valid outputs specified" and returns at :274-277 — with `mapping.active` still true from the previous mapping and outputCount now 0. savePWMMappingsToPreferences() is only reached at :303, so NVS still holds the good list. Three consequences follow directly: processPWMPassthrough iterates `j < outputCount` (WCB_PWM.cpp:721) so nothing is driven; listPWMMappings prints "Input: Serial3 -> Outputs:" with nothing after it (:390-400); and the backup emitter at WCB.ino:3359-3370 skips only INACTIVE slots, so it emits the bare `?MAP,PWM,S3`, which on replay hits WCB.ino:5150-5153 and prints "Invalid format. Use: ?MAP,PWM,Sx,dest".

**How it is reached.** Any typo in a re-map of an existing input: `?MAP,PWM,S3,X9`, `?MAP,PWM,S3,W2` (no port), `?MAP,PWM,S3,W21S3` (board above MAX_WCB_COUNT) — all of which pwm.map_validation already confirms print "No valid outputs specified". Report row: LIST showed `['PWM Mappings:', 'Input: Serial3 -> Outputs:', ...]`, ?backup held `?MAP,PWM,S3`, and passthrough S3 -> S4 stopped.

**Why it matters.** A typo silently stops a live signal path (an RC channel, a dome-servo feed) with a message that reads like nothing happened, and the state survives until a reboot rescues it from NVS — so a builder may never reboot and will simply believe the PWM feed died. Worse, the corrupt state is exportable: a ?backup or Wizard pull taken in that window records a mapping that cannot be restored. And any LATER PWM command that calls savePWMMappingsToPreferences (another addPWMMapping, a removePWMMapping) persists the slot as the value `"3,"` (:539-548), which loadPWMMappingsFromPreferences then discards entirely (:616 requires outputCount > 0) — the mapping is gone for good.

**Fix.** Build into a local and commit only on success. In addPWMMapping (WCB_PWM.cpp:231-277) declare `PWMMapping cand; cand.inputPort = inputPort; cand.outputCount = 0; cand.active = false;`, parse the destinations into `cand`, and only after the `if (cand.outputCount == 0) return;` check at :274 do `pwmMappings[slot] = cand; pwmMappings[slot].active = true;`. The oldRemote[] snapshot at :215-229 is taken before any mutation so it still works unchanged, and the local-pin loop at :284-301 and the ETM sends at :316-345 then operate on committed state.

**Risk.** The slot-selection loop at :209-214 reuses the active slot for the same input port, so the copy must land in that same slot index — do not change the selection logic. Re-run pwm.map_deferred_reboot and pwm.passthrough_local (they exercise the success path through the same code) and pwm.map_validation's `?MAP,PWM,CLEAR,S3` case, which must still report "No PWM mapping found for Serial3".


#### 16. Interrupt-protected soft-serial TX (enableIntTx(false)) blanks the RX edge ISR of every soft port — any write to S3/S4/S5 destroys input arriving on S3/S4/S5

| | |
|---|---|
| **Status** | VERIFIED — input.soft_rx_under_soft_tx, map.baud_on_raw_mapped_port PASS on the bench (20260922-125537) |
| **Owner** | `WCB_firmware` |
| **Effort** | M |
| **Tests** | `input.soft_rx_under_soft_tx`, `map.baud_on_raw_mapped_port` |
| **Subsystem** | real-failures |

**Mechanism.** applySoftSerialIntTx() (WCB.ino:2068-2081) calls Serial3/4/5.enableIntTx(false) for every port softSerialLoopTaskOnly() (WCB.ino:2054-2065) calls loop-task-only. In EspSoftwareSerial 8.1.0 that makes UARTBase::write() call disableInterrupts() = taskENTER_CRITICAL(&m_interruptsMux) (SoftwareSerial.cpp:34-40, :359-361) for the WHOLE buffer, reopening interrupts only inside lazyDelay() at each stop bit (:265-283) — and on ESP32 optimistic_yield() is an empty macro (esp32-hal.h:80), so the window is just the remaining stop-bit time. Net: ~90% of every transmitted byte-time has core-1 interrupts masked. The soft-serial RX path is an edge ISR that timestamps with micros() at ISR entry (rxBitISR, SoftwareSerial.cpp:559-570) and all three ports' ISRs sit on core 1, so a masked interrupt is a lost/late edge and the byte mis-frames. The library README says this outright: enableIntTx(false) improves send timing 'at the expense of blocking concurrent full duplex receives' (README.md:11-16). The 40-line comment block at WCB.ino:2018-2053 reasons carefully about TX-vs-TX and core affinity and never mentions RX at all — that is the gap. HIL evidence, W1 with no mapping and all three soft ports at 9600: arm C (no load) 40/40 exact lines into S3, arm B (load on HARDWARE S2) 40/40, arm A4 (';S4,x' every 50 ms) 0/40, arm A3 0/40, arm A4h 0/40 (session.log:8267). Second, decisive proof with NO mesh load at all: map.baud_on_raw_mapped_port maps S3 raw->S4; isSerialPortRawMapped() only tests inputPort (WCB_Storage.cpp:1897-1905) so S3 is unprotected but the DESTINATION S4 is still protected. RawSerialForwardingTask reads S3 and writes S4 in the same core-1 loop (WCB.ino:7455-7466), so its own S4 write blinds its own S3 read: 32-byte chunks came out as 8-20 bytes of garbage, and a lone 12-byt …

**How it is reached.** Two everyday paths, neither needing mesh load. (1) Anything that feeds commands INTO a soft port — a controller, a NaviCore, a second WCB, a Teeces/Stealth board answering — while the WCB writes any soft port. On a stock board serialCommandTask drains all of S3-S5 (WCB.ino:7383-7385) and ?BCAST,OUT is ON for S3/S4/S5, so EVERY broadcast line is bit-banged to all three ports: a 60-char broadcast is ~190 ms of transmission of which ~170 ms is interrupts-off, i.e. the board is deaf on all soft ports for a sixth of a second per broadcast. (2) Any raw serial mapping whose destination is a soft port (?MAP,SERIAL,S3,R,S4) — 100% corrupt with the board otherwise idle.

**Why it matters.** Loses and silently corrupts commands in normal use, and corrupts raw binary forwarding completely with no error anywhere — the operator sees a device that 'randomly ignores things'. It is also a regression against the pre-fix firmware: with the library default m_intTxEnabled=true there were no critical sections and soft RX worked while soft TX happened. The change traded a 45% soft-TX failure rate under mesh load for a ~100% soft-RX failure rate whenever any soft port transmits.

**Fix.** Two layers. (a) Immediate and narrow: WCB.ino:2059 only unprotects raw-mapping INPUTS. Add a destination test — walk serialMonitorMappings[] for any active rawMode mapping whose inputPort is 3-5 and unprotect every soft port it touches at either end (or simply: if any soft port is a raw-mapping input, protect none). That alone makes map.baud_on_raw_mapped_port pass. (b) The real fix: make the decision BOARD-LEVEL, because the interrupt mask and the library's m_interruptsMux are both global. Replace softSerialLoopTaskOnly(port) with: softPortNeedsAsyncRx(p) = true if p is drained by serialCommandTask (the default — WCB.ino:7383-7385), is a raw-mapping input, or is the local Kyber port; false if p is an MP3/DFP/HCR/WLED port (those get only solicited replies, which never overlap our own TX) or is PWM-reserved. Then protect ALL soft ports only when NO soft port needs async RX; otherwise protect none. Add a persisted operator override '?SOFTTX,ON|OFF|AUTO' with the resulting state printed at boot and on every change, because only the operator knows a soft port is genuinely idle. Note this makes AUTO resolve to OFF on a stock board, which is correct — see the cross-cutting note.

**Risk.** Layer (b) turns protection OFF by default, so a builder with an HCR/MP3/Teeces on S3-S5 who still has another soft port drained for commands goes back to the rule-13 TX truncation until they either move the device to S1/S2 or set ?SOFTTX,ON. That must be in the wiki and in the ?BAUD/?HCR output, not just in docs/. It also invalidates hcr.softserial_integrity_mesh_load's 'assert protected' precondition and the premise of input.softserial_inttx_state, both of which need updating in the same commit. CLAUDE.md rule 13 and docs/HIL_TESTING.md §6 need rewriting — rule 13 currently presents enableIntTx(false) as the fix with no mention of the RX cost.


#### 17. A ?MAP,SERIAL that adds no valid destination leaves an ACTIVE zero-destination mapping — the port swallows everything and ?backup emits a token that cannot be replayed

| | |
|---|---|
| **Status** | VERIFIED — map.failed_update_keeps_mapping, map.bare_raw_flag PASS on the bench (20260922-125537) |
| **Owner** | `WCB_firmware` |
| **Effort** | M |
| **Tests** | `map.failed_update_keeps_mapping`, `map.bare_raw_flag` |
| **Subsystem** | serial-mapping |

**Mechanism.** addSerialMonitorMapping mutates the LIVE mapping before it parses anything: it finds the existing slot and zeroes outputCount (WCB_Storage.cpp:1725) or allocates a new slot with active=true (WCB_Storage.cpp:1735). The destination loop then `continue`s past every invalid entry (WCB_Storage.cpp:1793-1807), and the commit gate only saves when outputsAdded>0 (WCB_Storage.cpp:1839-1848). Nothing rolls back, so the slot is left active with outputCount==0. Text mode: processBroadcastCommand finds the active mapping (WCB.ino:6990-6997), loops over zero outputs and returns (WCB.ino:7006-7030) — every line on that port is discarded. Raw mode: isSerialPortRawMapped (WCB_Storage.cpp:1897-1906) makes serialCommandTask stop reading the port (WCB.ino:7394, :7399-7404) while RawSerialForwardingTask drains it into zero destinations (WCB.ino:7426-7498) — the port stops executing commands entirely. '?MAP,SERIAL,S2,R' reaches the same state via the dispatcher, which turns a bare 'R' into "S2R," with an empty destination list (WCB.ino:5119-5121). printBackupConfig then emits the empty mapping as '?MAP,SERIAL,S2' (WCB.ino:3257-3269), which on replay hits 'Invalid format' (WCB.ino:5109-5111) and is dropped, or as '?MAP,SERIAL,S2,R', which recreates the deaf mapping. The bad state is not saved at the time, but saveSerialMonitorMappings writes every slot including count=0 (WCB_Storage.cpp:1920-1946), so the next CLEAR (WCB_Storage.cpp:1995) or any later successful ?MAP persists it across reboot. Log: results/20260916-122250/session.log:15020-15347 and :15348-15596.

**How it is reached.** One typo in a destination while editing a live mapping — '?MAP,SERIAL,S2,S4' then '?MAP,SERIAL,S2,X9' — silences S2 immediately, and the console says only 'No new destinations added'. '?MAP,SERIAL,S2,R' (the natural way to try to turn raw on for an existing mapping) kills the port's command handling outright. Both are hand-typed or pasted-config paths a builder hits routinely; the Wizard's own round-trip preserves the broken '?MAP,SERIAL,S2' token (parser.js:978-1005, app.js:4063).

**Why it matters.** A mistyped destination makes a working port deaf with no error that says so, the damage is not confined to the failed command (the previously-good list is gone from RAM), the resulting ?backup/?MGMT,PULL token cannot be restored, and any later mapping save makes the deaf state permanent. This is command loss plus saved-config corruption.

**Fix.** Stage the parse. In addSerialMonitorMapping (WCB_Storage.cpp:1673-1849) build a local SerialMonitorMapping (or a local outputs[10]+count) from the destination list, and only after outputsAdded>0 copy it over the live slot / allocate a slot, then apply the broadcast-flag auto-disable (currently inline at WCB_Storage.cpp:1826-1835) and save. On outputsAdded==0 leave the existing mapping untouched and print e.g. 'No valid destinations — Serial2 mapping left unchanged'. Separately reject the empty list at the dispatcher: at WCB.ino:5119, when remainderUpper=="R" (no destination follows), print the usage line instead of building "S2R,". Optional belt-and-braces: treat outputCount==0 as not-active in isSerialPortMonitored/isSerialPortRawMapped (WCB_Storage.cpp:1888-1906) and skip it in printBackupConfig (WCB.ino:3258).

**Risk.** The auto-flag disable must move with the commit or a failed update will still have flipped the port's broadcast flags. Re-issuing a mapping whose destinations are all duplicates must still count as success (map.duplicate_destinations, and the idempotent re-issue asserted by map.set_flags_list_backup). The replace-not-append semantics (WCB_Storage.cpp:1715-1729, deliberate — see its comment) must be preserved: stage, then replace.


#### 18. A chain that does not START with '?' but contains ?SEQ,SAVE is handed to the naive timer splitter — the sequence value is cut at the first ;T, stored truncated, and its tail executed

| | |
|---|---|
| **Status** | VERIFIED — if.timer_split_trap PASS on the bench (20260922-125537) |
| **Owner** | `WCB_firmware` |
| **Effort** | M |
| **Tests** | `if.timer_split_trap` |
| **Subsystem** | vars-if-seq |

**Mechanism.** Two chain walkers exist. parseCommandsNoChecksum knows where a ?SEQ,SAVE value ends (WCB.ino:2395-2418 — only delimiter+funcChar closes the value, '^;' belongs to it). parseCommandGroups does not: it splits on the bare delimiter (command_timer.cpp:113, walk at :126-191) and treats any ';t'/';T' token as an inter-group delay. Which walker gets the line is decided by ONE test — WCB.ino:7262 `if (!data.startsWith(String(LocalFunctionIdentifier)) && isTimerCommand(data))`. That exemption is a FIRST-CHARACTER test on the whole line, while isTimerCommand (command_timer.cpp:62-65) searches the whole line, value included. So any chain whose first token is not a '?'-command, but which carries a ?SEQ,SAVE whose value contains ;t, is routed to the naive walker. Bench evidence (results/20260916-122250/session.log:68088-68092): `IF,hilu=0^?SEQ,SAVE,HILTS,;S1<a>^;T400^;S1<b>` printed `Stored: Key='HILTS', Value=';S1<a>'` — value truncated at the ;T — and 400 ms later probe1 received `<b>` on S1, i.e. the rest of the VALUE executed as live commands. The identical control line starting with '?' stored the whole value (session.log:68099). The same one-character exemption guards the two ESP-NOW receive paths (WCB.ino:4540, :4788); the sequence-recall path has NO exemption at all (WCB_Storage.cpp:629 `if (isTimerCommand(stripped)) parseCommandGroups(stripped);`), so a stored body is split even when it does start with '?'. ?CS is exposed the same way mid-chain (its own guard, WCB.ino:7186, only matches a line that STARTS with ?C).

**How it is reached.** Hand-routing a save to another board is the common one: `;W2,?SEQ,SAVE,03,;m11^;t4000^;w2testing` (that exact sequence is in W1's real config — session.log:68071). The line starts with ';', so W1 splits it before routing: WCB2 stores `?SEQ,SAVE,03,;m11` and prints a normal `Stored:` line, while W1 fires `;w2testing` 4 s later. Also any IF-gated save (`IF,cond^?SEQ,SAVE,...`), any chain with a device command before the save (`;S1x^?SEQ,SAVE,...`), a mesh-delivered chain not starting with '?', and a stored sequence whose body saves another sequence. The Wizard is NOT affected: remote pushes go through ?MGMT,FRAG, whose single-chunk payload has its SOH stripped before the gate (WCB.ino:4408) an …

**Why it matters.** Silent config corruption reported as success — the board prints `Stored:` with a half-sequence, so the user only finds out when the sequence plays wrong, and on the ;W path the corruption lands on a board they are not even watching. The discarded tail is not dropped, it is EXECUTED: servo/audio/serial commands fire at save time, and under a false IF they escape the gate entirely (the gate is consumed by the truncated save, so the ;T and the tail run ungated).

**Fix.** Make the exemption token-aware instead of first-character. Add a non-static helper next to tokenHasVerb (WCB.ino:2238) — `bool chainCarriesValueVerb(const String &data)` — that walks token starts (index 0 and every index after a commandDelimiter) and returns true if tokenHasVerb(data, idx, LocalFunctionIdentifier, "SEQ,SAVE,") or "CS" or "MGMT," matches; declare it in WCB_Storage.h beside isTimerCommand (:143). Then gate all four sites on it: WCB.ino:7262, :4540, :4788 become `if (!chainCarriesValueVerb(data) && !data.startsWith(String(LocalFunctionIdentifier)) && isTimerCommand(data))`, and WCB_Storage.cpp:629 becomes `if (!chainCarriesValueVerb(stripped) && isTimerCommand(stripped))`. Put the new call last so it costs nothing on lines with no timer token. Durable alternative if you want both behaviours: teach parseCommandGroups the same value boundary by factoring the ?CS / ?SEQ,SAVE / ?MGMT end-of-token logic out of parseCommandsNoChecksum into a shared helper — the same medicine ifGateConsumeToken was created to apply to the gate semantics.

**Risk.** A chain that deliberately mixes a real timer with a ?SEQ,SAVE (e.g. `;S1a^;t500^?SEQ,SAVE,K,v`) loses its inter-group timing: the ;t tokens go to parseCommandsAndEnqueue as ordinary tokens. That is a behaviour change, but it trades a rare timing case for silent NVS corruption. Watch that seq.* and timer/group tests still pass (seq.cycle_guard uses ;t inside recalled bodies with no SEQ,SAVE present, so it is unaffected). The four sites must be changed together — leaving WCB_Storage.cpp:629 out leaves the recall path corrupting bodies that begin with '?'.


### Medium impact (29)


#### 19. `;H,VOL,<not-a-channel>,<n>` silently mutes all three HCR channels — `;H,VOL,ALL,40` sets everything to 0

| | |
|---|---|
| **Status** | VERIFIED — hcr.input_validation_gaps PASS on the bench (20260922-125537) |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `hcr.input_validation_gaps` |
| **Subsystem** | devices-hcr-dfp |

**Mechanism.** WCB_HCR.cpp:370-378. `hcrChan(f1)` returns -1 for anything that is not V/A/B (:217-223). The handler treats -1 as "no channel given" (`all = true`, :372) and then re-reads field 1 as the VALUE (`vStr = all ? f1 : hcrField(body, 2)`, :373). `String("X").toInt()` is 0, `vStr.length()` is non-zero and `0 <= 0 <= 100`, so the guard at :375 passes, the usage line at :376 never prints, and the loop at :381-386 writes 0 to V, A and B. Proven: `;H,VOL,X,40` → `<PVV0>\n<PVA0>\n<PVB0>\n` (log 5053.599/5053.662). The same shape is in VOLUP/VOLDN at :393-399: a non-channel field 1 becomes the step, `toInt()` gives 0, and `if (step <= 0) step = 5` (:399) papers over it — so `;H,VOLDN,X,10` steps ALL three channels by 5 instead of one channel by 10.

**How it is reached.** `;H,VOL,ALL,40`. The help says "no channel = all" (WCB_Help.cpp:633), so ALL is the obvious thing to try — and it mutes the droid instead of setting 40. So does any typo in the channel letter. Nothing is printed on any of the three consoles, so the only symptom is that the HCR went quiet.

**Why it matters.** One plausible keystroke silences the vocalizer and both audio channels, with no error line anywhere, and recovering needs a `;H,VOL,<n>` the user has to think of. It is not data loss and it is one command wide, which is why it is medium rather than high — but it is exactly the "bad diagnostic sends someone debugging the wrong thing" case: the natural next step is to suspect the HCR, the wiring or the soft-serial port.

**Fix.** Add a digits-only test and use it in both blocks. `static bool hcrAllDigits(const String &s) { if (!s.length()) return false; for (unsigned i = 0; i < s.length(); i++) if (!isdigit((unsigned char)s[i])) return false; return true; }`. At WCB_HCR.cpp:375 change the guard to `if (!hcrAllDigits(vStr) || v < 0 || v > 100)` — the existing usage line is already the right message. At :397-399 do the same for the step: when `all` is true and `f1.length()` but `!hcrAllDigits(f1)`, print `[HCR] Usage: ;H,VOLUP|VOLDN[,<V|A|B>][,<step>]` and return instead of falling through to the default 5.

**Risk.** Both currently-green volume tests must stay green, and they do: `hcr.bad_args` sends `;H,VOL,101` (digits, caught by `v > 100`) and `;H,VOL,A` (real channel, caught by empty field 2); `hcr.verbs_audio_volume` sends `;H,VOL,40`, `;H,VOL,100`, `;H,VOLUP,7`, `;H,VOLUP,0` — all digits or real channels. The only behaviour change is that a non-numeric, non-channel argument now prints usage instead of being read as 0.


#### 20. `;H,STOP` does not cancel an in-flight fade, and neither does any `;H,FN` verb — a stopped fade fires a delayed StopWAV and volume write ~1 s later

| | |
|---|---|
| **Status** | VERIFIED — hcr.stop_cancels_fade PASS on the bench (20260922-125537) |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `hcr.stop_cancels_fade` |
| **Subsystem** | devices-hcr-dfp |

**Mechanism.** WCB_HCR.cpp:306 — `if (vU == "STOP") { _hcr->Stop(); return; }` — with no `hcrCancelFade`. Every readable audio verb does cancel first, precisely because `HcrFade::tick` would otherwise overwrite them within STEP_MS: PLAY at :348 and :353, STOPWAV at :361, VOL at :383, VOLUP/VOLDN at :407. Those are the only five `hcrCancelFade` call sites in the file. The entire numeric FN branch (:263-287) is also missing them: fn 14 goes to `_hcr->PlayWAV` (:278) and fn 16/17/18/19 go to `hcrCodec.emit` (:279) with no cancel at all. The orphaned fade keeps ticking in `HcrFade::tick` and completes normally, emitting `SetVolume(to)` then, if stopAtEnd, `StopWAV(ch)` and a restore (WcbCmd/src/WcbHcrFade.cpp:49-56).

**How it is reached.** `;H,FADEOUT,A,3` … `;H,STOP` … `;H,PLAY,A,12` inside one sequence — the delayed `StopWAV(A)` lands after the new track has started and kills it, and the restore stomps whatever volume was set in between. Proven at log 5092.550: `;H,FADEOUT,A,1` + `;H,STOP` → the STOP burst goes out, then the ramp continues `<PVA26> <PVA21> <PVA17> <PVA12> <PVA8> <PVA3> <PVA0>`, then a SECOND `<PSA,QPA>` and `<PVA30>` land ~1 s after the user stopped everything. The FN half is reached by any RC controller or stored sequence driving volume numerically while a fade is running — the fade overwrites the FN volume within STEP_MS.

**Why it matters.** Timing-dependent and only bites when a fade overlaps another audio command, so it is narrower than D1/D3 — but it is the worst kind to debug, because the symptom (a track cut off ~1-3 s in, or a volume that snaps back) depends on where in the ramp the STOP landed and looks random. The FN half is the broader one: NaviCore's equivalent path DOES cancel (NaviCore.ino:1681-1689, with a comment at :1679-1681 claiming it mirrors the WCB), so the same RC action behaves differently depending on which board owns the HCR.

**Fix.** WCB_HCR.cpp:306 — `if (vU == "STOP") { hcrCancelFade(0); hcrCancelFade(1); hcrCancelFade(2); _hcr->Stop(); return; }` (Stop() stops all three channels, so cancel all three). In the FN branch, mirror NaviCore.ino:1681-1689 before the emit at :273: for fn 14/16/17 cancel `chan` (and all three when fn 17 chan == 3, which HcrCodec treats as ALL — WcbHcr.cpp:81); for fn 8/18/19 cancel all three. Note the WCB's `;H,FN,8` (Stop) has both gaps at once. A matching NaviCore fn-8 fix is a separate, NaviCore-owned item — its fn list at :1681 omits 8 the same way.

**Risk.** The two green fade tests must stay green and will: `hcr.fade_instant_and_timed` and `hcr.play_fadein_and_cancel` never issue STOP or an FN verb mid-fade, and they already assert that VOL and STOPWAV cancel. `hcr.fn_codec` sends FN volume verbs with no fade running. The one thing to think about is whether a user wants `;H,STOP` to abort a FADEOUT's final restore — the test asserts they do, and it matches STOPWAV's existing behaviour, so this makes the verbs consistent rather than changing a deliberate choice.


#### 21. `;D,DEVICE,n` is double-stripped to "EVICE,n" and rejected — the same verb works on NaviCore's local port and fails over the mesh

| | |
|---|---|
| **Status** | VERIFIED — dfp.device_verb PASS on the bench (20260922-125537) |
| **Owner** | `WcbCmd` |
| **Effort** | S |
| **Tests** | `dfp.device_verb` |
| **Subsystem** | devices-hcr-dfp |

**Mechanism.** Two strips of the same leading letter. `processDFPCommand` removes the leading 'D' and an optional comma (WCB_DFP.cpp:72-74), then `DfPlayerCodec::handle` removes an optional leading 'D' again (WcbCmd/src/WcbDfPlayer.cpp:54). `;D,DEVICE,2` → `rest` is `DEVICE,2` → handle sees `EVICE,2`, matches no prefix, and falls through to `return false` at :167. DEVICE is the only verb in the set that starts with 'D', so it is the only casualty. Log 5425.447-5425.940 proves both halves: `;D,DDEVICE,2` → `[DFP] cmd=0x09 param=2`, `;D,DEVICE,2` → `[DFP] Unknown or invalid command: DEVICE,2` — and the rejection then prints its own help line advertising `DEVICE,1-5` (WCB_DFP.cpp:90). MP3 has the identical shape (WCB_MP3.cpp:96-98 pre-strips, WcbMp3.cpp:35 strips again) but no MP3 verb starts with 'A', so it is latent, not reachable.

**How it is reached.** Type `;D,DEVICE,2` on any console, or bind a NaviCore RC action to DFP_DEVICE. That is the cross-repo sting: `dfpFormatCommand` emits `;D,DEVICE,<n>` (NaviCore.ino:1862); on the LOCAL path NaviCore calls `g_dfp.handle(cmd.c_str() + 1)` = `handle("D,DEVICE,2")` (:1914), where the 'D' is followed by a comma so the single strip lands correctly and the command works. The SAME action routed over the mesh to a WCB is silently rejected. One button, two outcomes, depending only on where the DFPlayer is wired.

**Why it matters.** DEVICE selects the playback source (1 USB, 2 SD, 3 AUX, 4 sleep, 5 flash). A builder with media on a USB stick or on the module's internal flash cannot switch to it over the mesh, and the failure message actively misleads — it says "Unknown or invalid command" and then lists DEVICE as valid three lines later. Narrow verb, but it is advertised in the help, documented in the header (WcbDfPlayer.h:50) and works on the other transport, so anyone who hits it loses real time.

**Fix.** WcbCmd/src/WcbDfPlayer.cpp:54-55 — strip the optional leading verb letter only when it is followed by a comma or end of string: `if ((*r == 'D' || *r == 'd') && (r[1] == ',' || r[1] == '\0')) r++;` then the existing `if (*r == ',') r++;`. NaviCore always passes "D,<VERB>" so it is unaffected; the WCB pre-strips so it passes "DEVICE,2", where 'D' is followed by 'E' and is correctly left alone. Apply the same one-line shape to WcbMp3.cpp:35 to close the latent twin. Push WcbCmd first, then rebuild the WCB (rule 6 — CI clones the repo). Cheaper WCB-only alternative if you do not want a WcbCmd release right now: pass `message.c_str()` instead of the pre-stripped `rest` at WCB_DFP.cpp:85, keeping `rest` only for the error printf — but that leaves the trap armed for the next 'D' verb and does not help any other caller, so I would not prefer it.

**Risk.** The conditional strip changes behaviour only for a body of the form `D<VERB>` with no comma (e.g. "DSTOP"), which would stop being stripped. Nothing emits that: the WCB pre-strips before calling, and NaviCore's dfpFormatCommand always emits `;D,<VERB>` with the comma (NaviCore.ino:1837-1866, every case). Verify against WcbCmd's GoldenVectors example (examples/GoldenVectors/GoldenVectors.ino:95,101) before pushing — it calls dfp.handle() directly and is the regression net for exactly this.


#### 22. ?STATS counts ACKs it never asked for: Delivered exceeds Attempts and the success rate reads 200%

| | |
|---|---|
| **Status** | VERIFIED — stats.delivered_not_above_attempts PASS on the bench (20260922-125537) |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `stats.delivered_not_above_attempts` |
| **Subsystem** | etm-mesh |

**Mechanism.** `etmAddToPendingTable` deliberately does NOT expect the controller/special peer's ACK on a BROADCAST — the exception at WCB.ino:1273-1274 is scoped to `targetWCB == WCB_SPECIAL_PEER_ID`, with a nine-line comment (1267-1272) explaining that expecting it on every broadcast costs a fleet-wide ACK plus retry burst. `etmStatsSent[b]` is incremented only inside that gate (WCB.ino:1288-1289), so a broadcast credits the controller no `sent`. NaviCore's WCB_Client ACKs every COMMAND packet it receives regardless. `etmProcessAck` then increments `etmStatsAckd[boardIdx]` for any ACK whose seq matches an active entry (WCB.ino:1303-1305) with no `expectAckFrom[boardIdx]` check. `buildStatsString` sums the special-peer slot into the totals (WCB.ino:1769-1773) and prints them as Attempts vs Delivered (1775-1776) with a derived success rate (1778-1781). The bench log shows it directly (session.log:79444+): after one broadcast, 'Transmission Attempts: 1, Delivered: 2, Delivery Success Rate: 200.00%' with a per-board row reading `WCB20 (special): Sent: 0, ACKd: 1`. After six broadcasts, '6 / 11 / 183.33%'. The correct implementation already exists on the other side of the same protocol: WCB_Client.cpp:2746-2749 gates its `ackd++` on `_statExpects(...)`, with a comment describing exactly this class of over-count.

**How it is reached.** Any fleet with NaviCore on the mesh as the controller — i.e. the documented architecture and Greg's own bench. Every `?STATS` and every `?MGMT,STATS,<n>` is wrong from the first broadcast onward, and the `Sent: 0, ACKd: 1` row is permanently visible.

**Why it matters.** Bad diagnostic in the one tool you reach for when the mesh is misbehaving. A >100% success rate is obviously nonsense once you notice it, but the per-board rows are not — 'WCB20 Sent 0, ACKd 1' invites the conclusion that the controller is receiving things nobody sent, and the inflated Delivered can mask a real per-board delivery shortfall by pulling the total up. No delivery behaviour is affected.

**Fix.** Gate ONLY the counter at WCB.ino:1305: `if (etmPendingTable[i].expectAckFrom[boardIdx]) etmStatsAckd[boardIdx]++;`. Leave `receivedAckFrom[boardIdx] = true` (1304) and the `wcbPeerReciprocated` promotion (1308) outside the gate — see risk. Add a comment pointing at WCB.ino:1267-1274 so the asymmetry reads as deliberate rather than as a bug someone will 'fix' by expecting the controller's broadcast ACK, which is the expensive direction the existing comment argues against.

**Risk.** The one real trap: do not gate the whole block. `wcbPeerReciprocated[boardIdx] = true` at WCB.ino:1308 is how a learned peer earns the right to count toward broadcast completion (the mixed-fleet guard at WCB.ino:1286 skips learned-but-never-reciprocated peers), and it is reached on first contact precisely when `expectAckFrom` for a broadcast is false — gating it would strand every learned peer as non-reciprocating forever. Re-check stats.rpt_local, stats.rpt_remote, etm.pending_table_evict (asserts exact 'Sent: 11, ACKd: 0, Retries: 30, Failed: 11') and the broadcast-controller-ACK test that asserts 'WCB2: Sent: 1, ACKd: 1'.


#### 23. A line containing ;t / ;T loses its source port: the timer path parses without a sourceID and every group fires as source 0, defeating the source-port skip, ?BCAST,IN,Sx,OFF and ?MAP,SERIAL,Sx

| | |
|---|---|
| **Status** | VERIFIED — input.timer_keeps_source PASS on the bench (20260922-125537) |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `input.timer_keeps_source` |
| **Subsystem** | input-chars |

**Mechanism.** processSerialCommandHelper routes any non-'?' line containing the timer token to `parseCommandGroups(data)` (WCB.ino:7262-7264) — and parseCommandGroups takes no sourceID at all (command_timer.cpp:89, declared command_timer_queue.h:22). When the group fires, processCommandGroups dispatches each command as `parseCommandsAndEnqueue(cmd, 0)` (command_timer.cpp:237). sourceID 0 means USB, and processBroadcastCommand keys three separate behaviours off it: * the ?MAP,SERIAL mapping lookup, `if (sourceID >= 1 && sourceID <= 5)` (WCB.ino:6989-6997) — a mapping on the real source port is never found, so the line broadcasts everywhere instead of going only to its mapped destinations; * the input-block check `if (sourceID >= 1 && sourceID <= 5 && blockBroadcastFrom[sourceID-1])` (WCB.ino:7034) — ?BCAST,IN,Sx,OFF is bypassed; * the source-port skip `i == sourceID` (WCB.ino:7044) — the broadcast is echoed back out the port that sent it. The same drop exists on the stored-sequence recall path, and there the asymmetry is visible in one `if`: WCB_Storage.cpp:629-633 passes `sourceID` to parseCommandsAndEnqueue in the non-timer branch (:632) but calls `parseCommandGroups(stripped)` with no source in the timer branch (:630). That is why input.recall_keeps_source passes and input.timer_keeps_source fails — same feature, one branch carries the source and one does not. Run evidence: `;T100^<marker>` sent on W1 S3 came back on S3, and with ?BCAST,IN,S3,OFF set it still reached S2/S3/S4/S5 (report.md:458-461).

**How it is reached.** Any `;t`/`;T` chain typed on or fed into a serial port — a controller on S4 sending `;t500^;M11`, or a stored sequence containing `;t` recalled from a port. Greg's own bench config has one: `?SEQ,SAVE,03,;m11^;t4000^;w2testing` (session.log:72803). Recall it from a port and the same loss applies.

**Why it matters.** Commands go to ports they were explicitly routed away from. A ?MAP,SERIAL,Sx mapping — the mechanism for keeping one device's traffic off the rest of the bus — is silently ignored for any timer-bearing line, and ?BCAST,IN,Sx,OFF likewise. The source echo can also feed a line back into a device that re-emits it. Not data loss, but misdelivery in a documented, commonly used path, and nothing on the console says the routing was skipped.

**Fix.** Give parseCommandGroups a `int sourceID = 0` parameter (command_timer.cpp:89, command_timer_queue.h:22), snapshot it into a new `commandGroupsSourceID` global next to the two that already exist for exactly this reason (commandGroupsEspnowOrigin / commandGroupsSequenceBody, command_timer.cpp:23,27), and use it at command_timer.cpp:237: `parseCommandsAndEnqueue(cmd, commandGroupsSourceID)`. Update the two callers that know the source — WCB.ino:7263 → `parseCommandGroups(data, sourceID)` and WCB_Storage.cpp:630 → `parseCommandGroups(stripped, sourceID)`. Leave the ESP-NOW deferred-chain drain (WCB.ino:461) at the 0 default; a mesh-received chain genuinely has no local source port.

**Risk.** A single global is correct here only because one timer sequence runs at a time — parseCommandGroups already replaces an in-flight chain and says so (command_timer.cpp:95-99), and commandGroupsGeneration already guards a mid-flight swap (command_timer.cpp:41, :246), so this adds no new concurrency surface. The behaviour change is real though: a timer chain from a blocked or mapped port will now stop delivering where it used to. If anyone has been relying on `;t` as an accidental way around ?BCAST,IN, that breaks — which is the intent, but it will look like a regression.


#### 24. Legacy ?CS stores a sequence value containing '^?', which neither ?backup/restore nor the Wizard can round-trip — the value is truncated and its tail executes as commands

| | |
|---|---|
| **Status** | VERIFIED — legacy.cs_caret_lfi_roundtrip PASS on the bench (20260922-125537) |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `legacy.cs_caret_lfi_roundtrip` |
| **Subsystem** | input-chars |

**Mechanism.** Two chain walkers disagree about where a stored-sequence value ends. The ?CS branch in parseCommandsNoChecksum ends the value only at the NEXT `^?CS` (WCB.ino:2364-2367; endPos = data.length() when there is none, :2370), so `?CSHILLQ,;S1<m>^?VERSION` is handed to saveStoredCommandsToPreferences whole and the literal `^?VERSION` is written into NVS (WCB_Storage.cpp:640-694 validates the key length but never the value). The ?SEQ,SAVE branch four lines below ends its value at the first `^` + funcChar (WCB.ino:2395-2404) — the strictly tighter rule. ?backup emits the stored value verbatim as `SEQ,SAVE,<key>,<value>` (WCB.ino:3350), so the backup chain now contains a `^?` inside a value. On restore that chain goes through the ?SEQ,SAVE branch, which cuts the value at the `^?`: the sequence comes back as `;S1<m>` and `?VERSION` runs as a separate command. The Wizard has the same boundary rule — `commandString.split('^?')` (Wizard/parser.js:422, with the comment at :419-421 explaining that '^' alone cannot be used because SEQ values embed it) — so the Wizard also tears the value apart and emits a spurious extra token. Run evidence (session.log:72831): stored `'[MGMT:SEQVAL,1]HILLQ,OK,;S1HILa35EE98^?VERSION'`, and it appeared in ?backup as `?SEQ,SAVE,HILLQ,;S1HILa35EE98^?VERSION`.

**How it is reached.** Only via the legacy `?CS<key>,<value>` form (WCB.ino:5856-5857 → storeCommand → saveStoredCommandsToPreferences) with a `^?` in the value. Typing the same thing as `?SEQ,SAVE,…` is already self-consistent — that form splits at `^?` at store time too, so what you get back is what you stored. The Wizard and WCB_Client both emit ?SEQ,SAVE, so this is hand-typed / legacy-config territory, not the shipping tooling path.

**Why it matters.** Config corruption that only shows up on restore: the sequence silently comes back shorter than it was saved, and the truncated tail is executed as a config command during the restore. If the tail happened to be something like `^?ERASE,NVS` or `^?WCB,2` the restore does real damage from text the user thought was data. While the value is stored it also breaks every consumer that splits the backup chain on '^?' — the firmware's own restore, the Wizard's parser, and the HIL harness's tokenizer all disagree with what is in NVS.

**Fix.** Make the ?CS value terminator identical to ?SEQ,SAVE's. In parseCommandsNoChecksum, replace the `^?CS` / `^?cs` search at WCB.ino:2364-2367 with the same `commandDelimiter + curFunc` boundary the SEQ,SAVE branch uses at WCB.ino:2399-2404. That makes `?CS` and `?SEQ,SAVE` store exactly the same bytes for the same input, which is what printBackupConfig (WCB.ino:3350) and Wizard/parser.js:422 both already assume. Optionally belt-and-braces: reject a value containing `commandDelimiter + LocalFunctionIdentifier` in saveStoredCommandsToPreferences (WCB_Storage.cpp:652-665, next to the existing key-length guard) so no path can create an unrestorable value.

**Risk.** A hand-typed `?CS` chain whose value deliberately contains '^?' changes meaning: the tail now executes at store time instead of being stored. That is the intended semantic and matches ?SEQ,SAVE, but it is a visible change for anyone with such a chain in a saved config file. The fix also does not repair values already in NVS — a board that has one keeps it until the sequence is re-saved, so consider a one-time warning in ?SEQ,LIST rather than a silent migration.


#### 25. A checksummed chain whose first command starts with ?C is enqueued whole instead of split — it prints "checksum VERIFIED" and then runs one mangled command

| | |
|---|---|
| **Status** | VERIFIED — input.checksum_c_chain PASS on the bench (20260922-125537) |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `input.checksum_c_chain` |
| **Subsystem** | input-chars |

**Mechanism.** processSerialCommandHelper has a second, duplicate checksum verifier that fires for anything beginning with funcChar + 'C' (WCB.ino:7186-7252). That prefix catches far more than the `?CS` store it was written for: ?CONFIG, ?CMDCHAR, ?CC, ?CLEAR, ?CONTROLLER. It verifies the CRC correctly, prints "✓ Command checksum VERIFIED", and then hands the whole verified chain to `enqueueCommand(cmdWithoutChecksum, sourceID)` (WCB.ino:7238) — a single-command enqueue that never splits on the delimiter. The real gate one screen up does this properly: parseCommandsAndEnqueue verifies (WCB.ino:2266-2332) and then hands off to the walker, `parseCommandsNoChecksum(dataWithoutChecksum, …)` (WCB.ino:2318). Any chain NOT starting with ?C falls through to WCB.ino:7268 and takes that correct path. Run evidence (session.log:10428-10431): `?VERSION^?VERSION^?CHKB54F27DF` printed two version banners; `?CONFIG^?VERSION^?CHKEDFCA6A5` printed "✓ Command checksum VERIFIED" followed by "Unknown command: CONFIG^?VERSION". The `?C` branch's own comment (WCB.ino:7189-7198) already records that this duplicate verifier drifted from its sibling once before and was fixed late ("the sibling gate in parseCommandsAndEnqueue was fixed in a32e9cb and this copy was left behind") — this is the same duplication biting a third time.

**How it is reached.** Send a CRC'd chain whose first token starts with ?C: `?CMDCHAR,;^?SEQ,SAVE,k,v^?CHK…` (the exact shape the no-checksum branch's own comment at WCB.ino:7247 says must split), or a hand-trimmed restore chain beginning at ?CONFIG or ?CONTROLLER. Not hit by the Wizard's normal restore — printBackupConfig emits chains starting `?HW,1^?MAC,…` (session.log:72803) — and not hit by a ?MGMT,FRAG config push, which reassembles and calls parseCommandsAndEnqueue directly (WCB.ino:3160).

**Why it matters.** The board confirms the integrity check passed and then silently drops every command after the first, so a partial config lands with a success message on screen. Worse in the ?CC case: `?CC;^…^?CHK…` enqueued whole reaches updateCommandCharacter (WCB.ino:6075) via the legacy prefix match, which applies charAt(2) and discards the rest — partial application rather than a clean failure. It does print "Unknown command" for most shapes, which is what keeps this out of the high band.

**Fix.** Stop the duplicate branch from bypassing the walker. Minimal change: drop the `static` on parseCommandsNoChecksum (WCB.ino:2264 declaration, :2334 definition), declare it alongside parseCommandsAndEnqueue, and replace WCB.ino:7238 with `parseCommandsNoChecksum(cmdWithoutChecksum, sourceID, -1, -1)`. Do NOT call parseCommandsAndEnqueue there — re-entering the checksum gate is exactly the recursion the comment at WCB.ino:2258-2263 warns about for payloads containing an earlier `^?CHK`. The ?CS case stays correct because parseCommandsNoChecksum's own ?CS branch (WCB.ino:2360-2386) still swallows the whole value.

**Risk.** Deleting the whole ?C branch (WCB.ino:7186-7252) is the cleaner end state — parseCommandsAndEnqueue already covers both cases — but its printed strings differ ("✓ Command checksum VERIFIED" vs "…VERIFIED - Configuration is intact"; "✗ Command checksum FAILED!" + " Provided:"/" Calculated:" vs "…FAILED - Configuration may be corrupted!"), and legacy.cs_checksum (s17_seq_inventory.py:503-528, currently PASSING) asserts the "FAILED!" spelling exactly. Deleting the branch breaks that passing test and any host tool matching those strings; the parseCommandsNoChecksum call keeps them. Either way, re-run legacy.cs_checksum and legacy.cs_caret_lfi_roundtrip together — they exercise the same branch.


#### 26. The legacy ?MAESTRO_CLEAR handler prefix-matches, so '?MAESTRO_CLEAR,M5' wipes the entire Maestro table

| | |
|---|---|
| **Status** | VERIFIED — maestro.legacy_clear_suffix_keeps_slots PASS on the bench (20260922-135805) |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `maestro.legacy_clear_suffix_keeps_slots` |
| **Subsystem** | maestro-kyber |

**Mechanism.** WCB.ino:5885 is `message.startsWith("maestro_clear") || message.startsWith("MAESTRO_CLEAR")` → `clearAllMaestroConfigs()`. Its neighbours are exact matches (`maestro_list` :5883, `maestro_default` :5887), so the prefix match here is an outlier, not a pattern. Anything beginning with those 13 characters — `?MAESTRO_CLEAR,M5`, `?MAESTRO_CLEARALL`, `?MAESTRO_CLEAR_ANYTHING` — clears every slot, local and remote-proxy alike, resets the freed ports' baud to 9600 and re-enables their broadcast flags (WCB_Maestro.cpp:774-797), and persists it. The modern `?MAESTRO,CLEAR,M5` is a different dispatcher entry (WCB.ino:5545-5551) and is unaffected — this only bites the legacy underscore spelling. Bench evidence: `?MAESTRO_CLEAR,M5` left 0 of 5 slots and printed 'All Maestro configurations cleared'.

**How it is reached.** A builder who knows the legacy `?MAESTRO_CLEAR` and reaches for the obvious 'clear just M5' extension, or who half-remembers the modern `?MAESTRO,CLEAR,M5` and types the underscore. Not emitted by the Wizard (grep of Wizard/*.js finds no `MAESTRO_CLEAR`), so it is a hand-typed hazard only.

**Why it matters.** Destroys the persisted Maestro map including WDP-learned remote proxies, and resets port baud/broadcast state — recovery needs a Wizard restore. Held at medium rather than high only because it does print what it did, so the user is not left guessing.

**Fix.** WCB.ino:5885 — make it exact, matching the lines above and below it: `} else if (message == "maestro_clear" || message == "MAESTRO_CLEAR") {`. Mixed-case spellings already fall through today, so this is behaviour-preserving for every documented form.

**Risk.** Essentially none. Worth one line in docs as a 'this is why it is an exact match' comment so it does not get widened again.


#### 27. ?MAESTRO accepts baud 0 for a LOCAL slot: the baud change is refused but the slot is saved and reported as a success, so ?backup describes a rate the port never gets

| | |
|---|---|
| **Status** | VERIFIED — maestro.local_baud_zero_rejected PASS on the bench (20260922-135805) |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `maestro.local_baud_zero_rejected` |
| **Subsystem** | maestro-kyber |

**Mechanism.** `configureMaestro`'s baud validator explicitly whitelists 0 (WCB_Maestro.cpp:610, `baudRate == 0 || ...`). For a LOCAL target it then calls `updateBaudRate(port, 0)` at :659-661, which refuses and prints 'Invalid baud rate' (WCB_Storage.cpp:155-160) and returns without touching the port or `baudRates[]`. Execution falls straight through to :663-675 (which still flips the port's broadcast-out and broadcast-in flags and persists them) and :677-685, which stores `baudRate = 0` in the slot, calls `saveMaestroSettings()` and prints '✓ Maestro 1: Local S1 at 0 baud (slot 1)'. Bench evidence: the board printed both lines, and `?backup` then emitted `?MAESTRO,M1:W1S1:0`. Zero is a legitimate REMOTE-proxy sentinel meaning 'baud unknown' (WCB_Maestro.cpp:729 `if (baud != 0 && ...)`, :772, and WCB_WDP.cpp:709 'unknown / garbled / baud-0 — nothing to configure'), which is presumably why :610 allows it at all — the bug is that the LOCAL branch does not exclude it.

**How it is reached.** A typo or a hand-edited config line. The damage is deferred: the backup now claims 0 baud, so restoring it onto a fresh board leaves that Maestro port at the default rate with the slot claiming 0 and no error — and the Wizard's verify-pull keeps seeing 0 and never converges.

**Why it matters.** Corrupts the saved config in a way that only shows up on restore, and prints a success line one line after printing the matching error — a builder reading the terminal cannot tell which one won.

**Fix.** WCB_Maestro.cpp — in the LOCAL branch (`targetWCB == WCB_Number`), reject `baudRate == 0` before the software-serial block at :643, with the same shape as the other refusals (message + `startIdx = nextComma + 1; continue;`). Leave :610 and the REMOTE branch alone: 0 must stay legal there. Better still, restructure so the slot write at :677-685 is conditional on `updateBaudRate` having succeeded (return a bool) — that closes the general 'apply refused, slot saved anyway' hole rather than just the 0 case.

**Risk.** Low, but confirm no saved `?MAESTRO,M<id>:W<self>S<n>:0` line exists in anyone's stored backup before tightening — a restore of such a line would now be refused rather than silently accepted. That is the right outcome, but it will print a new error on replay.


#### 28. ?KYBER,LOCAL,S3-S5 forces 115200 onto a software-serial port that ?MAESTRO explicitly blocks above 57600

| | |
|---|---|
| **Status** | VERIFIED — all named tests PASS on the current images (20260922-215719); was: FIXED (unverified) — refusal before any state change (2026-09-22) + Greg's follow-up: ?KYBER,LOCAL is also refused on S1 |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `kyber.config_negatives`, `kyber.local_rejected_port_keeps_forwarding` |
| **Subsystem** | maestro-kyber |

**Mechanism.** `storeKyberSettings` calls `updateBaudRate(kyberPort, 115200)` for any port 1-5 (WCB_Storage.cpp:1220-1222) and prints 'Set Serial%d to 115200 baud (Kyber standard)'. 115200 is on `updateBaudRate`'s whitelist (WCB_Storage.cpp:157), so it is really applied — `applyLiveBaud` tears down and re-begins the SoftwareSerial at :166. `configureMaestro` blocks exactly this for the same ports: 'S%d is SOFTWARE SERIAL / Baud rate (%d) is TOO HIGH for software serial / ❌ CONFIGURATION BLOCKED!' at WCB_Maestro.cpp:643-656, with `>57600` as the threshold. Bench evidence (session.log, kyber.local_port_s3_frees_s2 slice): `?KYBER,LOCAL,S3` printed 'Kyber is LOCAL on Serial3' and 'Set Serial3 to 115200 baud (Kyber standard)' with no warning of any kind, and also flipped S3's broadcast out/in flags (:1224-1233) on the way. CLAUDE.md rule 13 is the standing constraint here: S3-S5 are bit-banged, TX busy-waits per bit, and an ESP-NOW interrupt mid-byte mis-frames the receiver — at 115200 the per-bit budget is ~8.7 µs.

**How it is reached.** Anyone putting the sabre on S3, S4 or S5 — the command advertises S1-S5 and there is no hint that three of the five cannot run the Kyber's own standard rate. The port is left configured-looking and the broadcast flags are already changed, so backing out is manual.

**Why it matters.** The Kyber link on that port will not work reliably, and the user is given an explicit success message rather than the refusal the firmware issues for the identical situation on the ?MAESTRO path. Sends someone debugging wiring or the sabre.

**Fix.** WCB_Storage.cpp:1198-1222 — refuse S3-S5 as the Kyber port in the same place the HCR/MP3/WLED/PWM ownership guard already sits (:1203-1209), with a message modelled on WCB_Maestro.cpp:643-656 ('S%d is SOFTWARE SERIAL — the Kyber runs at 115200, use a hardware port: ?KYBER,LOCAL,S1 or S2'). Refusing is the right call rather than lowering the baud: the Kyber's rate is fixed, so a soft-serial Kyber port has no working configuration.

**Risk.** It is a new refusal. If anyone in the field has a Kyber on S3-S5 today it stops being accepted — but at 115200 on bit-banged serial it cannot have been working, so the refusal is the diagnosis. Note the refusal must come BEFORE the Kyber_Local/kyberLocalPort assignment at :1210-1213, or you reproduce defect 3's leak in a new place.

**Reopened (2026-09-22).** The #28 soft-serial refusal, and the older ownership refusal, ran only after `storeKyberSettings` had set `kyberUseTargeting = true` and auto-populated `kyberTargets[]`. The bare `?KYBER,LOCAL` form had also already wiped them. So a refused `?KYBER,LOCAL,S3` still switched a broadcast-mode Kyber_Local board to targeting, which is #6's leak again (session.log 101489: "Auto-populated 5 Kyber targets" printed before the refusal at 101490). It was invisible on W1, a Remote board. **Fix:** both refusals moved into `kyberLocalPortRefused()` (WCB_Storage.cpp), which runs before any live state changes: on the bare path before the target wipe, and on the S path right after the range check. The tests now cover S3/S4/S5 in `kyber.config_negatives` (no "Auto-populated" allowed) and an S3 arm in `kyber.local_rejected_port_keeps_forwarding`. **Open decision for Greg:** also refuse `?KYBER,LOCAL,S1` while S1 hosts a local Maestro, and reword the refusal to name only a free hardware port. Today it is accepted, S1's baud goes to 115200, and KyberLocalTask echoes S1 back into itself.

**Follow-up applied (2026-09-22, Greg: "refuse if already configured on s1").** `?KYBER,LOCAL` is refused on S1/S2 when a LOCAL Maestro slot is there. The refusal names only a hardware port that is actually free. A review then found two bypasses, both closed: (1) the explicit target form `?KYBER,LOCAL,S1,M1:W<self>S1:<baud>`, which is what `?backup` and the Wizard emit, created the slot itself after the check (now `kyberTargetOnKyberPort` refuses it first); (2) a later `?MAESTRO` onto the Kyber port (`configureMaestro` now refuses it). The refusals made push order matter, because `KYBER,LOCAL` used to go out before the Maestro lines. So `?backup` and the Wizard now emit it after the Maestro and device lines, and the Wizard sends `KYBER,CLEAR` first when the Kyber port changes. `?backup` now also releases first (`KYBER,CLEAR` / `MAESTRO,REMOTE` ahead of the `BAUD` lines, #73 D5/D9), so on either path Kyber_Local is off while the Maestro lines run and the `?MAESTRO` refusal does not fire on a restore.

#### 29. getPosition names its RAM variable from the raw channel text, not the parsed channel — so ',05' fills m1pos05 and a trailing field produces an invalid name that is silently dropped

| | |
|---|---|
| **Status** | VERIFIED — maestro.get_var_name_from_parsed_channel PASS on the bench (20260922-135805) |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `maestro.get_var_name_from_parsed_channel` |
| **Subsystem** | maestro-kyber |

**Mechanism.** `maestroGetInfo` builds the name as `"m" + dev + "pos" + ch` where `ch` is the raw substring after the second comma (WCB_Maestro.cpp:389; `ch` comes from `payload.substring(pc + 1)` at :296). The Pololu frame, by contrast, is built by `WcbMaestro::build` from the reconstructed verb body at :412-414, which parses the channel as an integer. So `;M1,getPosition,05` puts the correct frame `AA 01 10 05` on the wire (the bench confirmed the expected frame arrived) but stores `m1pos05`, while the file's own comment at :380 documents `getPosition,<ch> -> m<dev>pos<ch>` and every `IF,m1pos5=…` looks for `m1pos5`. `;M1,getPosition,0,99` builds a valid channel-0 frame but names the variable `m1pos0,99`; `setVariableRAM` → `setVariableImpl` → `isValidVariableName` rejects the comma (WCB_Variables.cpp:63-72, :136) and returns false — which :457 ignores — while the debug line at :458 prints `-> m1pos0,99=6000` as if it had stored. The same raw `ch` is also spliced into the cross-board reply at :471, producing `:MQR,1,0,99,POS,6000` — a six-field payload that NaviCore's comma-walking parser (NaviCore.ino:801-806) reads as chan=0, KIND='99' and discards.

**How it is reached.** `;M1,getPosition,05` is a natural spelling for anyone who pads channel numbers; the variable then never fills and the sequence's IF never fires, with the debug output claiming it did. The trailing-field form is a typo path. Neither is exotic, but neither is the common case.

**Why it matters.** A get that appears to succeed (frame on the wire, debug line says stored) but leaves the variable at 0 sends someone debugging the Maestro, the wiring, or the IF logic — none of which are wrong. Bad-diagnostic category.

**Fix.** Normalise the channel once, at the top of `handleMaestroGet`: `String chNorm = ch.length() ? String(ch.toInt()) : String();` (or reject a non-numeric `ch` outright with a printed reason). Use `chNorm` at WCB_Maestro.cpp:389 for the variable name and at :471 for the `:MQR` chan field; keep passing the original to `WcbMaestro::build` or rebuild the verb body from `chNorm` — either way the name and the frame then agree. Also honour the return of `setVariableRAM` at :457 so the debug line at :458 cannot claim a store that was refused.

**Risk.** Changes the variable name for a leading-zero channel (m1pos05 → m1pos5). Anyone whose sequence already reads m1pos05 would break — unlikely, but it is a user-visible rename, so it belongs in the docs/wiki edit alongside the comment at :380.


#### 30. The WCB's own ?OTA help prints a relay grammar the relay parser does not accept, and its unknown-subcommand hint omits BAUD

| | |
|---|---|
| **Status** | VERIFIED — ota.help_matches_parser PASS on the bench (20260922-125537) |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `ota.help_matches_parser` |
| **Subsystem** | navicore-ota |

**Mechanism.** Two stale strings, one cause: the help text drifted from the parser. (a) Code/WCB/WCB_Help.cpp:713-715 prints `BEGIN,<target>,<imageSize>,<fam>` / `DATA,<target>,<offset>,<base64>` / `END,<target> / ABORT,<target>`, but processOtaRelayCommand parses a fixed `<target>,<session>` prefix before anything else — WCB_OTA.cpp:472 takes target from the first field and WCB_OTA.cpp:475 takes session from the second, with the remainder handed to the per-sub parse at :481-486. Its own header comment at WCB_OTA.cpp:460-462 states the real grammar (`BEGIN,<target>,<session>,<size>,<family>`), and the Wizard sends exactly that (Wizard/app.js:1913 `cmd(`BEGIN,${targetWcb},${session},${total},${family}`)`). Following the help shifts every argument: `?OTA,BEGIN,2,4096,0` parses as target 2, session 4096, size 0, family 0. (b) WCB_OTA.cpp:324 prints `(use STATUS|BEGIN|DATA|END|ABORT)` while BAUD is a fully implemented local subcommand at WCB_OTA.cpp:237 — and the help block two lines up (WCB_Help.cpp:707) does list BAUD, so the hint alone is stale. Both halves are visible in the run's own transcript (results/20260916-122250/session.log:93983-93985 and :93430). The docs are NOT wrong here: docs/WCB_OTA_TECHNICAL.md:212 and :279, and the wiki's Command-Reference.md:416, all carry the correct `?OTA,<SUB>,<target>,<session>,…`. Only the firmware's own help lies.

**How it is reached.** Any operator who types `?OTA?` — the help explicitly bills these lines as 'the wire protocol it speaks', i.e. the hand-driven recovery path used precisely when the Wizard is unavailable and a board must be recovered over the mesh. Nothing destructive follows: the shifted BEGIN arrives with size 0 and is rejected before any erase (`[OTA] BEGIN rejected: image 0 B exceeds partition`, guards at WCB_OTA.cpp:108-124 precede esp_ota_begin at :126); the shifted DATA falls into the usage-error line; the shifted END carries session 0 and is rejected by otaEnd's session check.

**Why it matters.** Bad diagnostic in the one path people reach for when the normal one has already failed. It cannot brick a board, but it sends someone chasing 'my board rejects OTA BEGIN' — chip family? partition table? — when the actual cause is the help text they followed. The missing BAUD is what makes a hand-driven local OTA take minutes instead of seconds, since the operator never learns the option exists.

**Fix.** Code/WCB/WCB_Help.cpp:713-715 — insert the session field: `BEGIN,<target>,<session>,<imageSize>,<fam>`, `DATA,<target>,<session>,<offset>,<base64>`, `END,<target>,<session> / ABORT,<target>,<session>`. (Optionally note the DATA offset's `[:<crc32>]` suffix documented at WCB_OTA.cpp:498-513; the test does not require it.) Code/WCB/WCB_OTA.cpp:324 — `(use STATUS|BAUD|BEGIN|DATA|END|ABORT)`. Then update tests/hil/suites/s20_ota.py:153, which pins the old hint string verbatim and passes today. No doc or wiki edit: docs/WCB_OTA_TECHNICAL.md and the wiki already describe the parser correctly, so per CLAUDE.md this is a fix that restores already-documented behaviour and earns no revision-log row.

**Risk.** Effectively none — four literal strings in help output, no parsing change. Two cautions: keep the help wording aligned with the parser rather than 'fixing' the parser to match the help, since the Wizard and both docs already speak the parser's grammar; and do not take the opportunity to make ABORT session-strict in the same commit — handleOtaAbortPacket (WCB_OTA.cpp:433-441) currently ignores sessionId, so the help's `ABORT,<target>` form happens to work today and tightening it is a behaviour change needing its own test, not a doc fix.


#### 31. NaviCore's #L1 diagnostic always names WCB HW 3.2, whatever board profile actually booted

| | |
|---|---|
| **Status** | VERIFIED — navicore.l1_names_board_profile PASS on the bench (20260922-125537) |
| **Owner** | `NaviCore` |
| **Effort** | S |
| **Tests** | `navicore.l1_names_board_profile` |
| **Subsystem** | navicore-ota |

**Mechanism.** NaviCore.ino:3599 is a literal: `case 1: Serial.println("NaviCore — WCB HW 3.2"); break;`. The firmware ships two pin profiles selected at boot by rcConfig.boardType in applyBoardProfile (NaviCore.ino:3180-3199): BOARD_WCB_HW_32 = 1 and BOARD_NAVICORE_V2 = 0, the default (enum at NaviCore.ino:177). The two profiles differ on every aux pin — SBUS_RX 5 vs 4, SBUS_OUT 4 vs 5, S3 15/16 vs 8/9, S4 17/18 vs 10/21, S5 9/10 vs 38/47 — and applyBoardProfile itself prints the correct name at boot (`[BOARD] NaviCore v2 pin profile`, NaviCore.ino:3196). That this is an oversight rather than a choice is settled by auxPortLabel (NaviCore.ino:274-279), which already branches on `rcConfig.boardType == 0` to pick the right silkscreen numbering. The bench NaviCore reports "boardType":0 (13 occurrences in results/20260916-122250/session.log) and #L1 still printed `NaviCore — WCB HW 3.2`.

**How it is reached.** Type `#L1` on any NaviCore running the default v2 profile — which is the default, so this is the common case, not the edge one. It is the first line of the diagnostic menu and the natural 'what am I talking to' check. The correct name is only in the boot banner, which is gone by the time anyone is debugging.

**Why it matters.** Wrong hardware named in the identity diagnostic, on a board where the two candidate profiles have entirely different pin maps. Someone checking which board they have before tracing an SBUS or aux-serial wiring fault gets told 'WCB HW 3.2' and then measures GPIO 5 for SBUS IN on a board where the firmware is actually driving GPIO 4. That is the textbook bad-diagnostic-sends-you-to-the-wrong-place case.

**Fix.** NaviCore.ino:3599 — derive the name from the applied profile, matching the strings applyBoardProfile already prints: `Serial.printf("NaviCore — %s\n", rcConfig.boardType == BOARD_WCB_HW_32 ? "WCB HW 3.2" : "NaviCore v2");`. Prefer the module-static `appliedBoardType` (NaviCore.ino:3178) over rcConfig.boardType if you want it to report what actually booted rather than what a since-saved config asks for — pins are assigned only at boot and SET_CONFIG deliberately defers a live boardType change, so the two can legitimately disagree until reboot. Update tests/hil/suites/s21_navicore_sbus.py:868 in the same change (see cross_cutting).

**Risk.** One line, no behaviour change. The only trap is the contradicting sibling test: navicore.cli_codes (s21_navicore_sbus.py:868) currently passes by requiring the literal 'NaviCore — WCB HW 3.2' and will fail the moment this is fixed. Keep the `NaviCore — ` prefix intact; both tests match on it.


#### 32. WDP PWM auto-config re-logs a refusal on every advert, forever — the code does the opposite of the comment directly above it

| | |
|---|---|
| **Status** | VERIFIED — pwm.remote_refusal_logged_once PASS 2026-09-21 (20260921-102817) |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `pwm.remote_refusal_logged_once` |
| **Subsystem** | pwm |

**Mechanism.** The receiver-side auto-config loop calls `if (!canUsePWMOnPort(prt)) continue;` under a comment that explicitly promises the skip is silent — "skip SILENTLY so we don't re-log/re-attempt every advert (the guard above would never flip)" (WCB_WDP.cpp:746-748). But canUsePWMOnPort prints ungated on all three refusal branches (WCB_PWM.cpp:65, :71, :79). The loop runs on every advert from every neighbour naming this board as a PWM target, and the condition can never change on its own, so the line repeats for as long as the misconfiguration stands. Confirmed in session.log at 4762.172 / 4762.234 / 4763.042 / 4765.160 — four "❌ Cannot use PWM on Serial2 - reserved for HCR/MP3/WLED" on W2 in about three seconds, one per advert plus the initial command-driven refusal.

**How it is reached.** Point a PWM mapping at a port the target board has claimed for HCR/MP3/WLED/Kyber — `?MAP,PWM,S3,W2S2` where W2 hosts `?WLED,1:W2S2`. The mapping stays configured on the source (it is legal there), so the source keeps advertising the target port and the receiver keeps refusing it, indefinitely.

**Why it matters.** A bad diagnostic that sends someone debugging the wrong thing: the line reads like a live error recurring every few seconds on a board the user did not touch, it buries surrounding console output (and the relayed terminal, which is how most people read a non-USB board), and it names Serial2 rather than the source board that is actually misconfigured. It is NOT a watchdog hazard — drainWdpPackets runs in loop() (WCB.ino:8347, and the note at WCB_WDP.cpp:395), so CLAUDE.md rule 11's UART concern does not apply here.

**Fix.** Give canUsePWMOnPort a quiet mode: `bool canUsePWMOnPort(int port, bool quiet = false)` in WCB_PWM.h:55, guard the three Serial.print* calls at WCB_PWM.cpp:65, :71 and :79 on `if (!quiet)`, and pass `true` from WCB_WDP.cpp:746 so the code finally matches its own comment. A single default argument in the header is safe here — unlike sendESPNowMessage (see the cross-cutting note), this function is declared exactly once. If a first-time notice is wanted, latch a per-source/per-port bit and log only on the transition.

**Risk.** Every other caller must keep printing — attachPWMInterrupt (WCB_PWM.cpp:144), addPWMMapping (:224, :261), addPWMOutputPort (:818), configureRemotePWMOutput (:764) and loadPWMOutputPortsFromPreferences (:927) all rely on the message to explain a refusal, and pwm.out_port_lines_and_guards and pwm.map_validation assert those exact strings. Default the parameter to false so only the WDP call site changes behaviour.


#### 33. Soft-port TX protection is computed only at boot and at ?BAUD, so it goes stale the moment a port's role changes

| | |
|---|---|
| **Status** | OBSOLETE — bit-banged TX is gone from S3-S5 (RMT, see #16); the path this defect lived in now runs only as a no-RMT-channel fallback |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `input.softserial_inttx_state` |
| **Subsystem** | real-failures |

**Mechanism.** applySoftSerialIntTx() has exactly two call sites: setup() (WCB.ino:8086, :8097, :8108) and applyLiveBaud() (WCB.ino:2110, :2117, :2124) — grep confirms no others. Nothing recomputes it when the inputs to softSerialLoopTaskOnly() (WCB.ino:2054-2065) change at runtime: adding or clearing a raw serial mapping, ?KYBER,LOCAL,Sx, adding/clearing a local Maestro on a soft port, configuring HCR/MP3/DFP, or adding a PWM mapping. input.softserial_inttx_state PASSES today and pins this as intended: it asserts that '?MAP,SERIAL,S5,R,S4' prints no [SOFTSERIAL] line, and that S5 is only reported 'unprotected' after a subsequent ?BAUD,S5,9600.

**How it is reached.** Push a config that adds a raw mapping or a Kyber/Maestro port without also changing that port's baud — a Wizard config push, ?MGMT,PUSH, or a restored backup where the baud already matches. The port keeps whatever protection it had until the next reboot or baud change. Same in reverse: clearing a mapping leaves the port unprotected.

**Why it matters.** On its own it is a narrow window (most config pushes also set a baud, which refreshes it), but it makes the defect above unpredictable — the same command sequence produces a different interrupt-masking state depending on whether a ?BAUD happened to follow. That is exactly the kind of state that makes an intermittent corruption impossible to reproduce.

**Fix.** Make applySoftSerialIntTx take no port and reapply all three soft ports from one board-level decision, and call it at the end of every role-changing handler: addSerialMonitorMapping / clear, setSerialMappingRawMode (WCB_Storage.cpp:1908), ?KYBER,LOCAL, Maestro add/clear, HCR/MP3/DFP configure/clear, PWM mapping changes — plus once after config load in setup(). Keep the existing debug print so the state change is visible.

**Risk.** Low. The only behavioural change is that the printed [SOFTSERIAL] line now appears on role-change commands too; input.softserial_inttx_state asserts the opposite and must be updated in the same commit.


#### 34. ?BAUD accepts and confirms baud rates on S3-S5 that a bit-banged port cannot receive at — including 128000 and 256000, above the library's stated maximum

| | |
|---|---|
| **Status** | VERIFIED — input.soft_rx_baud_sweep PASS on the bench (20260922-125537) |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `input.soft_rx_baud_sweep` |
| **Subsystem** | real-failures |

**Mechanism.** updateBaudRate() (WCB_Storage.cpp:146-181) validates against one 13-entry whitelist for all five ports (:156-159) — 110 … 115200, 128000, 256000 — with no distinction between the hardware UARTs (S1/S2) and the bit-banged S3-S5, then prints 'Baud rate for Serial%d updated to %d' unconditionally (:180). EspSoftwareSerial's README states 'Speed up to 115200 baud is supported' (README.md:19-20), so 128000 and 256000 are outside the library entirely. Measured on the bench with NO concurrent soft TX (the readback goes to hardware S2, injection comes from a probe hardware UART), exact lines of 20 received on S3: 19200 -> 20, 38400 -> 17, 57600 -> 18, 115200 -> 0 (session.log:8581). At 115200 the library also switches RX strategy — begin() attaches rxBitSyncISR, which busy-samples a whole byte inside the ISR, instead of rxBitISR (SoftwareSerial.cpp:189-191) — which is why it is total rather than partial failure. The Wizard offers the same list for every port (Wizard/app.js:7, Wizard/index.html:693-694).

**How it is reached.** A builder wires a 115200 device (an H-CR at its default rate, a WLED controller, a NaviCore link) to S3, S4 or S5, sets ?BAUD,S3,115200 in the Wizard, gets a success line, and then debugs the device, the cable and the mesh — the port simply cannot receive at that rate and is marginal above 19200.

**Why it matters.** A confirmation message for something that cannot work. It does not corrupt anything by itself, but it sends someone down the wrong debugging path for hours, and it is the single most likely reason a builder puts a fast device on a soft port in the first place.

**Fix.** In updateBaudRate(), split the whitelist by port class: S1/S2 keep the full list; S3-S5 accept up to 115200 (the library's max) and print a warning above 19200, e.g. 'S3 is a bit-banged software UART — input above 19200 baud is unreliable (measured 85% of lines exact at 38400, 0% at 115200). Use S1 or S2 for this device.' Reject 128000/256000 on S3-S5 outright with a reason. Mirror the same gate in Wizard/app.js:7 so the dropdown for S3-S5 stops at 115200 and marks anything above 19200. Record the measured numbers in docs/HIL_TESTING.md.

**Risk.** Rejecting 128000/256000 on S3-S5 could fail a ?config restore from an existing backup that contains one — make the restore path warn and clamp rather than abort the chain, and check TestConfigs fixtures. Low otherwise.


#### 35. A text mapping to this board's own W<n>S<p> is sent over the air to the board's own MAC and never delivered — only the raw path treats self as local

| | |
|---|---|
| **Status** | VERIFIED — map.text_self_wcb PASS on the bench (20260922-125537) |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `map.text_self_wcb` |
| **Subsystem** | serial-mapping |

**Mechanism.** processBroadcastCommand tests only destWCB==0 for a local destination (WCB.ino:7010); anything else, including this board's own number, goes out as ';S<port>'+line via sendESPNowMessage (WCB.ino:7021-7022). sendESPNowMessage has no self check and picks WCBMacAddresses[target-1] (WCB.ino:2667), which for self is a MAC that was never added as an ESP-NOW peer, so esp_now_send returns 12393 = ESP_ERR_ESPNOW_NOT_FOUND — exactly the '[ETM] Send failed seq 70, error: 12393' in the failure message (printed ungated at WCB.ino:2677). RawSerialForwardingTask gets this right: 'output.wcbNumber == 0 || output.wcbNumber == WCB_Number' (WCB.ino:7463), which is why map.raw_self_wcb_local passes. Under ETM there is a second cost: etmAddToPendingTable runs before the failed send (WCB.ino:2664-2665), so every mapped line books a pending entry that burns three retries and then counts as failed (WCB.ino:1374-1422), and the retry path even calls esp_now_add_peer on the board's own MAC (WCB.ino:1401-1406), consuming a peer slot.

**How it is reached.** The wiki's own example is this: '?MAP,SERIAL,S4,W1S1,W2S1,W3S1' (Wireless_Communication_Board-WCB.wiki/Command-Reference.md:188) — on WCB1 the W1S1 leg silently does nothing. Also hit by renumbering a board: after '?WCB,2', every mapping on that board that pointed at W2 becomes a self-reference and stops delivering. The Wizard is not a source — it writes the local board as 'Local' (value 0) in the destination dropdown (Wizard/app.js:4009).

**Why it matters.** Every line to that destination is lost, and the mapping reported 'Serial mapping set'. Not fully silent — the console prints an ESP-NOW/ETM send failure per line — but the diagnostic points at the mesh, not at the mapping, so it sends the user debugging radio problems. The ETM pending-table and retry churn is collateral on a busy port.

**Fix.** WCB.ino:7010: `if (destWCB == 0 || destWCB == WCB_Number)` — one line, mirroring the raw path at WCB.ino:7463. Optionally also normalise at parse time (WCB_Storage.cpp:1786-1795: map wcbNum==WCB_Number to 0) so LIST and ?backup print 'S<p>' and the config round-trips cleanly; do that in addition, not instead, because a saved mapping made before a renumber still needs the runtime check.

**Risk.** Very low. Behaviour only changes for a destination that currently fails 100% of the time. If the parse-time normalisation is added too, a ?backup diff will show W<self>S<p> rewritten to S<p> — expected, worth a note in the wiki example.


#### 36. A raw mapping to a remote S0 is accepted and reported as set, but every chunk is rejected at the receiver — it can never deliver

| | |
|---|---|
| **Status** | VERIFIED — map.raw_remote_s0_refused PASS on the bench (20260922-125537) |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `map.raw_remote_s0_refused` |
| **Subsystem** | serial-mapping |

**Mechanism.** The destination parser accepts port 0 for any WCB number — range check is `wcbNum > MAX_WCB_COUNT || serialPort < 0 || serialPort > 5` at WCB_Storage.cpp:1804, where `serialPort < 0` is dead code on a uint8_t. sendESPNowRawSerial puts the port in structCommand[0] with no validation (WCB.ino:2917). The receiving board's WCB_TARGET_RAW_SERIAL branch drops every chunk with `targetPort < 1` (WCB.ino:4674) and the only diagnostic is behind debugMaestro (WCB.ino:4675-4677). Text mode does support a remote S0 (';S0'+line → processSerialMessage's target==0 branch, WCB.ino:6584-6589 — map.text_remote_s0 passes) and local raw S0 works (map.raw_s0_local passes), so raw+remote+S0 is the one dead combination. Confirmed by the log: '?MAP,SERIAL,S2,R,W2S0' answered 'Serial mapping set: Serial2 (RAW) -> 1 destination(s)'.

**How it is reached.** '?MAP,SERIAL,S2,R,W2S0' — a plausible 'stream this device's bytes to the other board's console so I can watch them'. It reports success AND auto-disables S2's broadcast in/out (WCB_Storage.cpp:1826-1835), so the user also loses the port's normal behaviour in exchange for nothing.

**Why it matters.** Silent success for something that never happens, plus a side effect (broadcast flags off) the user did not ask for. Narrower than the zero-output defect because it takes a deliberate, unusual destination choice.

**Fix.** Refuse it at the parser, which is what the test asserts: in addSerialMonitorMapping, when the mapping is rawMode and a destination has wcbNum != 0 && serialPort == 0, print e.g. 'Invalid destination: raw mappings cannot target a remote S0 (W%dS0)' and skip it (WCB_Storage.cpp:1796-1807). Also apply the check when an existing mapping is toggled to raw so a stored W<n>S0 destination cannot become undeliverable behind the user's back.

**Risk.** The alternative remedy — supporting it by accepting targetPort 0 at WCB.ino:4674 — would make map.raw_remote_s0_refused wrong as written, and must NOT be done naively: meshSerialWrite falls through to getSerialStream(0) i.e. UART0 for any port outside 3-5 (WCB.ino:2148-2152, :2007-2016), and that write happens on the WiFi task inside espNowReceiveCallback — 177 bytes at 115200 blocks it ~15 ms per chunk, which is exactly the trap CLAUDE.md rule 11 records. It would have to queue like mgmtQueueOut. Refusing is the cheap, safe option; a board that already has such a mapping saved will see it refused on restore, so print the reason.


#### 37. A mapped text line that starts with ',' loses that comma on the remote leg — the same line arrives differently locally and remotely

| | |
|---|---|
| **Status** | VERIFIED — map.remote_leading_comma PASS on the bench (20260922-125537) |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `map.remote_leading_comma` |
| **Subsystem** | serial-mapping |

**Mechanism.** The mapping's remote leg wraps the line in the user-facing serial verb: `";S" + destPort + cmd` (WCB.ino:7021). The receiver's processSerialMessage then strips exactly one ',' after the port digit and trims (WCB.ino:6579-6581) — a deliberate change with a comment explaining that every sibling runtime verb accepts ';V<id>,<payload>' and strips the separator. So ',HILAFF7D2' becomes ';S2,HILAFF7D2' on the wire and 'HILAFF7D2' at the far port, while the local destination is written verbatim (WCB.ino:7016). Report evidence: remote got b'HILAFF7D2\r', local got b',HILAFF7D2\r'.

**How it is reached.** Any mapped remote text destination whose payload is comma-led — CSV-style telemetry or a device protocol that begins its frames with ','. The user sees the local copy arrive correctly and the remote copy arrive one character short, which reads as a link problem rather than a framing convention.

**Why it matters.** Silent single-character corruption on one leg only, with no error anywhere; the device at the far end either mis-parses or silently ignores the frame. Narrow in that it only bites comma-led payloads, but when it bites, the mapping is useless for that device and the cause is very hard to find.

**Fix.** Fix the sender, not the receiver: WCB.ino:7021 → `String espnowCmd = ";S" + String(destPort) + "," + cmd;`. The receiver's one-comma strip then consumes the separator and the payload survives intact — ',X' becomes ';S2,,X' and arrives as ',X'; 'X' becomes ';S2,X' and arrives as 'X'. Do NOT touch WCB.ino:6580 — sibling verbs and hand-typed ';S3,hello' depend on it (see the comment at :6575-6578).

**Risk.** Three things to watch. (1) Mixed-version mesh: a peer running firmware older than the comma-strip change would print a spurious leading comma — check how far back the strip goes before pushing. (2) map.text_remote_etm asserts the literal wire form '^\[ETM\] Sent seq \d+: ;S2<text>$' (tests/hil/suites/s13_serial_mapping.py:176) and must be updated in the same commit. (3) One character less payload headroom under ?ETM,CHKSM, where the limit is 187 (ETM_MAX_CMD_WITH_CRC, WCB.ino:225) — map.text_chksm_limit's 184/185 boundary shifts by one.


#### 38. A malformed IF term is folded in as plain false instead of failing the expression, so OR'ing it with a true term runs the gated command — the opposite of the documented fail-safe

| | |
|---|---|
| **Status** | VERIFIED — if.malformed_or_passes PASS on the bench (20260922-125537) |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `if.malformed_or_passes` |
| **Subsystem** | vars-if-seq |

**Mechanism.** WCB_Variables.h:54 states the contract: "On a malformed expression it returns false (fail-safe: skip) and prints why." evalOneCondition (WCB_Variables.cpp:341-367) cannot express "malformed" — it prints the reason and returns plain false at :353 (no operator) and :357 (missing variable), indistinguishable from a condition that legitimately evaluated false. evaluateIfCondition then folds that false into the boolean chain at :455-459 (`else if (pendingOp == "OR") result = result || c;`), so `hila=1 OR junk` = true||false = true. Bench evidence (session.log:67593-67595): `[VAR] IF: no operator in 'junk'` printed, then `[IF] hila=1,OR,junk -> true`, and probe1 received the gated marker at 5659.467. The AND direction is the mirror image: `IF,hila=1,AND,junk` is silently always false, so the command never runs. Structural errors (misplaced/trailing AND, two conditions with no operator, empty) DO return false correctly at :452, :461, :469-470 — only the per-term errors leak.

**How it is reached.** Any typo in an IF expression — a dropped `=1`, a renamed variable written without its comparison, a stray space that splits a term. The user sees a single `[VAR] IF:` reason line scroll past and a command that ran (OR) or never runs (AND); nothing says the gate was not evaluated.

**Why it matters.** A fail-open gate fires the command the user wrote the condition to prevent — a servo move or an audio cue in a state that was meant to block it — and does so while printing a diagnostic that says the expression was true. The AND direction is a dead command with a misleading `-> false` echo, which is exactly the 'sends someone debugging the wrong thing' case. Not high: nothing is persisted or corrupted, and it takes a malformed expression to reach.

**Fix.** Propagate the term error. Change the signature to `static bool evalOneCondition(const String &cond, bool &bad)` (WCB_Variables.cpp:341), set `bad = true` alongside the two existing Serial.printf error returns (:353, :357), and in evaluateIfCondition (:456) do `bool bad = false; bool c = evalOneCondition(tok, bad); if (bad) return false;` before the haveFirst/AND/OR fold. Every existing reason line still prints unchanged, so if.malformed_fails_closed keeps passing verbatim. No doc change needed — this restores what WCB_Variables.h:54 and docs/VARIABLES_DESIGN.md already promise.

**Risk.** Behaviour changes only for expressions that already print a `[VAR] IF:` reason line, and only in the direction of skipping. The one case someone could be depending on is a deliberately bogus term used as a constant-false in an AND chain — it still skips, so nothing breaks. Keep the reason-print BEFORE the early return or if.malformed_fails_closed's ordered reason list fails.


#### 39. ?VAR,CLEAR,all wipes the whole variable table — 'all' is also a legal variable name, and CLEAR matches the keyword case-insensitively

| | |
|---|---|
| **Status** | VERIFIED — var.clear_all_name_collision PASS on the bench (20260922-125537) |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `var.clear_all_name_collision` |
| **Subsystem** | vars-if-seq |

**Mechanism.** isValidVariableName (WCB_Variables.cpp:63-73) accepts any 1-15 char [A-Za-z0-9_] string, so `all` / `ALL` / `All` are creatable by ;V, ;VP and ?VAR,SET, and they list and GET like any other variable. But the CLEAR handler uppercases the target first — `String targetU = target; targetU.toUpperCase(); if (targetU == "ALL") { clearAllVariables(); ... return; }` (WCB_Variables.cpp:326-328) — so the keyword wins before findVarSlot is ever consulted. clearAllVariables (:198-202) marks all 100 slots unused and calls saveVarsToNVS(), which rewrites the wcb_vars blob from the now-empty table (:76-93): persistent variables are gone from RAM and from NVS. Bench evidence (session.log:66031): `'all': GET '[VAR] all = 1', CLEAR printed ['[VAR] All variables cleared'], hila afterwards "[VAR] 'hila' is not set"`.

**How it is reached.** A user creates a variable named `all` (a natural name for a mode/scope flag — `;V,all,1`), sees it in ?VAR,LIST, and types `?VAR,CLEAR,all` to remove it. Every persistent variable on the board goes with it, including the ones ?backup would have restored. Narrow, but it is a plain reading of the command reference, which documents `?VAR,CLEAR,<name>` and `?VAR,CLEAR,ALL` side by side with no note that the name is reserved (docs/VARIABLES_DESIGN.md:62-63).

**Why it matters.** Destroys saved config (persistent variables are ?backup content) in one keystroke, unrecoverably on that board. Held below high only by reachability: it needs a variable deliberately named 'all', and the board does print '[VAR] All variables cleared' rather than doing it silently.

**Fix.** Close both halves. (1) Reserve the keyword at creation: add `static bool isReservedVariableName(const String &n) { String u = n; u.toUpperCase(); return u == "ALL"; }` and reject in the two set paths — processSetVariable after the name check (WCB_Variables.cpp:231-235) and processVarConfig's SET branch (:299-303) — with a message naming the reason. Keep it OUT of isValidVariableName, or loadVariables (:118) will silently drop a legacy 'all' at boot and you lose the chance to clear it. (2) Make CLEAR prefer an existing exact name: at :328, `if (findVarSlot(target) < 0 && targetU == "ALL") { clearAllVariables(); ... }` so a legacy board can still delete the variable it already has, after which the ambiguity is gone for good. Add the reserved name to docs/VARIABLES_DESIGN.md:55-64 and a Revision log row (the page has none yet — first change adds one), plus the wiki's variable page if it lists names.

**Risk.** A board that already stores a variable literally named 'all' will get a rejection on the `?VAR,SET,all,<v>` line a ?backup restore emits (printVariablesBackup, WCB_Variables.cpp:475-490) — one noisy line, no data loss, and only for a name nobody should have. If you take fix (2) alone without (1), `?VAR,CLEAR,ALL` stops wiping the table for any user who has such a variable, which is the surprise the test author explicitly allowed for by accepting either shape.


#### 40. The sequence recall cycle guard compares keys case-insensitively while every other sequence path is case-sensitive, so a non-existent different-case key is reported as recursion

| | |
|---|---|
| **Status** | VERIFIED — seq.cycle_guard_case PASS on the bench (20260922-125537) |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `seq.cycle_guard_case` |
| **Subsystem** | vars-if-seq |

**Mechanism.** The guard walks its chain set with `activeChainKeys[i].equalsIgnoreCase(key)` (WCB.ino:6835). Everything else in the sequence subsystem is exact: recallCommandSlot reads NVS with the raw key (WCB_Storage.cpp:573), saveStoredCommandsToPreferences dedups key_list with == / startsWith (:679-684), eraseStoredCommandByName filters with `k != name` (:721). So `;Chilrx` inside sequence HILRX matches the guard but could never have recalled HILRX. Bench evidence (report.md:227): the body's S1 marker was sent, then `Recall stored command request received:Chilrx` followed by `Sequence 'hilrx' recalls itself (directly or in a loop) — refusing to expand it again`, where the truthful answer is `No command stored under key: 'hilrx'`. The guard over-refuses only — it can never miss a real cycle, since the lookup it protects is case-sensitive.

**How it is reached.** Typing or storing a nested `;C<key>` in the wrong case, which is easy because keys are free text and the rest of the command language is case-insensitive. Also blocks the legitimate case of two sequences whose keys differ only in case (both storable): the second is refused as a self-recall.

**Why it matters.** Purely a diagnostic lie in the common case, and the rubric's 'bad diagnostic that sends someone debugging the wrong thing' fits exactly — the operator is told their sequence loops and goes hunting a cycle that does not exist, when the real fault is a one-character case typo the honest message would have named. No data is lost and the queue keeps working.

**Fix.** WCB.ino:6835 — `if (activeChainKeys[i] == key)` (String::equals, case-sensitive), matching the NVS lookup at WCB_Storage.cpp:573. One word. The existing seq.cycle_guard test still passes: HILRR/HILRS/HILRA/HILRB and the HILD1..HILD9 depth chain all recall themselves in the exact case they were stored. No doc change (docs/SEQUENCE_INVENTORY.md does not specify guard matching), though a source comment recording 'sequence keys are case-sensitive everywhere — keep this compare exact' is worth the line.

**Risk.** Nil for cycle detection. The only behaviour that disappears is the accidental refusal of a case-varying recall, which was never protecting anything. Note the guard has a SECOND, unfixed sharp edge in the same block — see the separate entry — so do not read this one-word change as making the guard correct.


#### 41. ?BCAST,RESET reports "input blocking cleared" but never writes the bcast_block namespace — ?config or a reboot brings the blocking straight back, and in the meantime the RAM clear un-blocks device-owned ports

| | |
|---|---|
| **Status** | VERIFIED — input.bcast_reset_persists PASS 2026-09-21 (20260921-105657) with RESET opening every port |
| **Owner** | `WCB_firmware` |
| **Effort** | M |
| **Tests** | `input.bcast_reset_persists` |
| **Subsystem** | input-chars |

**Mechanism.** resetBroadcastSettingsNamespace (WCB_Storage.cpp:352-382, reached from ?BCAST,RESET at WCB.ino:5220-5222 and legacy `?reset_broadcast` at WCB.ino:5960) clears and rewrites the `bdcst_set` namespace for the OUTPUT flags (:354-369), then fixes up the RAM globals — including `blockBroadcastFrom[i] = false` (:377, whose own comment says "input blocking was never reset at all") — and prints "…input blocking cleared…" (:381). But input blocking lives in a different namespace, `bcast_block` (WCB_Storage.cpp:1654-1670), and the reset never touches it: saveBroadcastBlockSettings() is not called. The next reload overwrites the RAM clear from stale NVS — printConfigInfo calls loadBroadcastBlockSettings() at WCB.ino:2478, before printBaudRates() prints the row at WCB_Storage.cpp:302-306, and boot does the same at WCB.ino:8025. Run evidence (report.md:476-479): after ?BCAST,RESET the very next ?config printed `Serial1 Baud: 57600, Broadcast Input: Disabled`. The half-state is worse than it looks in the other direction too. Device drivers set blockBroadcastFrom[] when they claim a port — Maestro (WCB_Maestro.cpp:671-672), HCR (WCB_HCR.cpp:506-507), MP3 (WCB_MP3.cpp:351-352), DFPlayer (WCB_DFP.cpp:305-306), WLED (WCB_WLED.cpp:173-174), Kyber (WCB_Storage.cpp:1229-1230). The RAM clear at :377 drops all of those, so between ?BCAST,RESET and the next ?config/reboot a Maestro's or HCR's reply bytes on a claimed port are treated as a broadcast line and fanned out to every other port and over the mesh. The accidental NVS reload is currently the only thing that stops it.

**How it is reached.** Type ?BCAST,RESET — a documented command whose whole purpose is "put broadcast settings back to defaults". The operator sees the success line, then runs ?config to confirm and finds the blocking still there, with nothing explaining the contradiction.

**Why it matters.** Reports success for something that did not happen, which is the worst kind of diagnostic: it sends the user looking for a stuck port or a bad save. The window where device-owned ports are un-blocked in RAM is the sharper edge — a Maestro or HCR on a claimed port will have its reply traffic broadcast — but it is short and self-healing, and nobody has hit it because ?config is the natural next command.

**Fix.** Three things in resetBroadcastSettingsNamespace (WCB_Storage.cpp:352-382), in this order: (1) after clearing the RAM flags at :375-379, re-assert `blockBroadcastFrom[i] = true` for every port a device owns — walk ports 1-5 against isSerialPortUsedForMP3 / isSerialPortUsedForDFP / isSerialPortUsedForHCR / isSerialPortUsedForWLED, a configured local Maestro, and the Kyber-local ports, mirroring the setters listed above; (2) call saveBroadcastBlockSettings() (WCB_Storage.cpp:1663) so `bcast_block` matches RAM; (3) change the message at :381 so it names what was kept ("input blocking cleared except on device-owned ports S1, S4") rather than claiming a blanket clear.

**Risk.** The re-assert list is a duplicate of ownership knowledge spread across six files — if it drifts, a reset either leaves a device port broadcastable or blocks a port the user wanted open. Prefer factoring a single `portIsDeviceOwned(int)` helper over copying the six predicates. Also note this makes ?BCAST,RESET genuinely destructive to persisted input blocking for the first time: anyone who ran it before and rebooted got their settings back, and after the fix they will not.


#### 42. NaviCore's TEST_ACTION ok flag means 'the JSON parsed', not 'the action fired'

| | |
|---|---|
| **Status** | VERIFIED — navicore.test_action_bad_target_not_ok PASS on the bench (20260922-125537) |
| **Owner** | `NaviCore` |
| **Effort** | M |
| **Tests** | `navicore.test_action_bad_target_not_ok` |
| **Subsystem** | navicore-ota |

**Mechanism.** rcTestAction (NaviCore.ino:2143-2151) returns false only for a null object or a failed actionFromJson; once parsing succeeds it calls `rcExecuteActionNow(a)` — which is declared void (NaviCore.ino:2035) — and returns true regardless. So the ACK at NaviCore.ino:3992 (`ok:%s`) and the bridged one at NaviCore.ino:2166 both report parse success. rcExecuteActionNow has several branches that dispatch nothing: RA_WCB_UNICAST wraps its whole body in `if (boardId >= 1 && boardId <= WCB_MAX_BOARDS)` (NaviCore.ino:2044) and falls straight to `break` otherwise, with no log line at all — note that the neighbouring RA_MAESTRO_REMOTE case does log a WARN for an invalid id (NaviCore.ino:2072-2074), so the silence here is an inconsistency, not a convention. The same handler also breaks out on `!wcb || !wcbReady` (NaviCore.ino:2045 and :2053) and on the skipRunning gate (:2046). actionFromJson accepts any target string for wcb_unicast without validating it (rc_config.h:1066-1072: `strlcpy(a.target, obj["target"] | "", ...); ok = true;`), so an empty target stores "", atoi()s to 0, and dies at the same silent gate. The run's log (session.log:94367-94368) shows `{"type":"TEST_ACTION","action":{..."target":"25"...}}` answered `{"type":"ACK","of":"TEST_ACTION","ok":true}` with no other output.

**How it is reached.** Not through the shipped config tool's normal editor — the WCB dropdown is generated from quantity/known and only ever emits 1-20 (config_tool/index.html:13307-13324), so the test's literal target 25 needs a hand-edited or imported config JSON, or a direct JSON client. The genuinely common route is the `!wcb || !wcbReady` branch: if wcb->begin() failed at boot (NaviCore.ino:4784-4791, the steady-orange-LED fault), EVERY wcb/maestro/hcr/mp3/wled Test button answers ok:true while nothing leaves the board. An empty or stale target in an imported config is the second route. Be honest that the exact scenario the test exercises is the narrowest instance of this.

**Why it matters.** A Test button is a diagnostic — its whole job is to tell you whether the wiring and routing work. Reporting success for a dispatch that was skipped inverts that: the operator concludes NaviCore is fine and goes to check the WCB, the servo, or the cable. It does not lose or corrupt anything, and the WCB_SEND handler four hundred lines away (NaviCore.ino:4032-4034) already gets the wcbReady case right and answers ok:false with a message — so the standard exists in the same file and this handler falls short of it.

**Fix.** Make the dispatch report. Change rcExecuteActionNow (NaviCore.ino:2035, forward decl :2015) to return bool: `return false` on each silent/skip exit (the RA_WCB_UNICAST range gate at :2044, both `!wcb || !wcbReady` guards at :2045 and :2053, the invalid Maestro id at :2072), `return true` at the end of a branch that actually dispatched; have rcTestAction (NaviCore.ino:2146-2150) return that value. While in there, add the missing else-log to RA_WCB_UNICAST so a bad board id is at least visible with DBG_WCB on, matching RA_MAESTRO_REMOTE's WARN at :2073. A cheaper S-sized alternative — validate the target inside rcTestAction before dispatching — is NOT recommended: it duplicates the dispatcher's rules in a second place and leaves the wcbReady case still lying.

**Risk.** Changing the signature touches every caller of rcExecuteActionNow — checkPendingActions' queue-full fallback (NaviCore.ino:2027), the delayed-action drain, and the record/replay player callbacks around NaviCore.ino:2172 — so a missed return path would report ok:false for an action that DID fire, which is worse than today's failure mode. Verify every switch branch has an explicit return before trusting it. Also note the config tool never reads this ACK (config_tool/index.html:15541-15542 flashes the button on send), so the firmware change alone is invisible to users; the tool must consume ok to make the Test button honest.


#### 43. A PWM output addressed as this board's own W<n>S<p> leaves the port completely dead — UART skipped, pin never enabled as an output

| | |
|---|---|
| **Status** | VERIFIED — pwm.self_wcb_output PASS on the bench (20260922-125537) |
| **Owner** | `WCB_firmware` |
| **Effort** | M |
| **Tests** | `pwm.self_wcb_output` |
| **Subsystem** | pwm |

**Mechanism.** Three places disagree on what "local" means. processPWMPassthrough treats it as local: `if (targetWCB == 0 || targetWCB == WCB_Number)` then digitalWrite (WCB_PWM.cpp:725-741). isSerialPortPWMOutput also counts it: `wcbNumber == 0 || wcbNumber == WCB_Number` (WCB_PWM.cpp:799-801), so the UART init is skipped at boot (WCB.ino:8094-8103). But the two places that actually enable the pin test `wcbNumber == 0` only — addPWMMapping's local-output loop (WCB_PWM.cpp:284-301, the test at :285) and loadPWMMappingsFromPreferences's (:621-636, the test at :622). So pinMode(txPin, OUTPUT) is never called and digitalWrite drives nothing. Separately, addPWMMapping classifies it as remote at :298-300 and ETM-sends `?MAP,PWM,OUT,S4` to the board's own MAC — sendESPNowMessage (WCB.ino:2616) has no self-target short-circuit, which is the `[ETM] Send failed seq 2, error: 12393` at session.log 4933.572.

**How it is reached.** Only by typing the command (or restoring a backup that contains it) — the Wizard cannot produce it: appendMappingDestination renders the local board number as `<option value="0">Local</option>` (Wizard/app.js:4009), so it always emits `S4`, never `W1S4`. But `?MAP,PWM,S3,W1S4` on WCB 1 reads perfectly correctly to a human and the firmware accepts it without a word of complaint. It can also arise from renumbering a board (`?WCB,<n>`) into its own mapping's destination.

**Why it matters.** The port is doubly dead — no UART (boot banner says "Serial4 reserved for PWM - skipping UART init", session.log 4940.393) and no PWM output — while every surface reports it configured: ?config and the boot banner both show "Serial4: Reserved for PWM Output", ?MAP,PWM,LIST shows the mapping, and the only hint anything failed is a one-line ETM send error at the moment of configuration. Not high only because the Wizard cannot generate it.

**Fix.** Normalize at parse time in addPWMMapping — after the destination is decoded (WCB_PWM.cpp:250-266, just before the range check at :252) add `if (wcbNum == WCB_Number) wcbNum = 0;`. Everything downstream then agrees: pin init runs, the self ETM send disappears, savePWMMappingsToPreferences writes `S4`, and the backup token is restorable. Add the same normalization in loadPWMMappingsFromPreferences (:601) for configs already stored as `W1S4`. Optionally print a note so the user learns `S4` is the canonical form.

**Risk.** ORDERING TRAP: initPWM() runs at WCB.ino:7957 and loadWCBNumberFromPreferences() at WCB.ino:7961, so at load time WCB_Number is still the default 1 — a load-time normalization would mis-fire on any board not numbered 1. Either move loadWCBNumberFromPreferences() above initPWM() (it is a pure NVS read and does not touch pins, so the "drive PWM output pins LOW ASAP" intent in the :7957 comment is preserved) or add a re-normalize + pin-init pass right after :7961. The addPWMMapping-side fix has no such problem — commands always run after WCB_Number is loaded.


#### 44. Clearing a PWM output port force-enables both broadcast flags and saves them, wiping a user's deliberate OFF — and WDP self-heal does it with no user action at all

| | |
|---|---|
| **Status** | VERIFIED — pwm.clear_out_restores_flags PASS on the bench (20260922-125537) |
| **Owner** | `WCB_firmware` |
| **Effort** | M |
| **Tests** | `pwm.clear_out_restores_flags` |
| **Subsystem** | pwm |

**Mechanism.** removePWMOutputPort unconditionally writes `serialBroadcastEnabled[port-1] = true` + saveBroadcastSettingsToPreferences() and `blockBroadcastFrom[port-1] = false` + saveBroadcastBlockSettings() (WCB_PWM.cpp:882-887). The comment at :878-881 justifies it as undoing a Push Config that wrote the flags OFF while the port was claimed — but nothing in the firmware ever turns those flags off for a PWM output port: addPWMOutputPort (:811-840) does not touch them, and broadcasts are suppressed on a PWM port by the runtime check `isSerialPortPWMOutput(i)` at WCB.ino:7043, not by flipping the flags. The Wizard side the comment defends against is also already fixed: app.js:3097-3106 refuses to read the toggles back for a hard claim ('pwm'), precisely so a push cannot write spurious BCAST,OFF. The restore is therefore unmatched — it can only destroy state, never repair it.

**How it is reached.** Set `?BCAST,OUT,S4,OFF` / `?BCAST,IN,S4,OFF` deliberately (a Teeces or magic panel that must not see the broadcast stream), later declare and then clear S4 as a PWM output — report row shows the flags coming back as `['?BCAST,OUT,S4,ON', '?BCAST,IN,S4,ON']`. The more troubling path needs no user action at all: reconcileWdpAutoPWMOutputs (WCB_PWM.cpp:842-865) calls removePWMOutputPort at :860 whenever a neighbour stops advertising a port it drove, so a remote board's mapping being deleted silently re-enables broadcasts on this board's port and persists it to NVS.

**Why it matters.** Corrupts a saved config setting and persists it, so it survives reboot and propagates into ?backup and the next Wizard pull. The visible symptom is a device suddenly receiving every broadcast again — which for a device with an overlapping command set means spurious behaviour that looks nothing like a broadcast-flag problem. Not high because it only bites users who deliberately turned a port's broadcasts off.

**Fix.** Record provenance instead of forcing. Add two arrays parallel to pwmOutputAutoSrc (WCB_PWM.cpp:42) capturing serialBroadcastEnabled/blockBroadcastFrom at the moment addPWMOutputPort claims the port (:833-834), persist them in savePWMOutputPortsToPreferences (:895-905, keys alongside "port"/"auto"), and have removePWMOutputPort restore those recorded values rather than ON/false at :882-887. If that is more machinery than it is worth, the simpler correct change is to delete :882-887 entirely and rewrite the comment to record why — nothing sets the flags off, so nothing needs to set them back.

**Risk.** If you delete the restore, read the comment at WCB_PWM.cpp:878-881 first: it encodes a real historical failure (a Push Config written while the port was claimed). Confirm the Wizard guard at app.js:3097-3106 is in the deployed Pages build before relying on it. Note that ?MAP,SERIAL,CLEAR has the same force-ON shape (WCB_Storage.cpp:1977-2010) but IS matched by a corresponding set (:1826-1835) — do not 'fix' that one the same way. Per the repo doc rule this earns a line recording the constraint, since the trap is re-walkable.


#### 45. A line containing ';T' loses its source port: it skips the port's mapping, skips input blocking, echoes back to the source port, and broadcasts mesh-wide

| | |
|---|---|
| **Status** | VERIFIED — map.timer_line_obeys_mapping, input.timer_keeps_source PASS on the bench (20260922-125537) |
| **Owner** | `WCB_firmware` |
| **Effort** | M |
| **Tests** | `map.timer_line_obeys_mapping`, `input.timer_keeps_source` |
| **Subsystem** | serial-mapping |

**Mechanism.** processIncomingSerial routes any non-'?' line containing ';t'/';T' to parseCommandGroups(data) with no sourceID (WCB.ino:7262-7264; isTimerCommand is a bare substring test, command_timer.cpp:62-65). Each group then fires as parseCommandsAndEnqueue(cmd, 0) (command_timer.cpp:237), so processBroadcastCommand is entered with sourceID 0 (WCB.ino:4979). At sourceID 0: the mapping lookup is gated on 1..5 and never runs (WCB.ino:6989), so the mapping does not claim the line; the blockBroadcastFrom gate is skipped for the same reason (WCB.ino:7034); the 'i == sourceID' self-skip never matches so the line is written back out the source port (WCB.ino:7044); and it is broadcast to the whole mesh (WCB.ino:7102). The suite saw exactly that: both markers reached S4 (as a plain broadcast) and leaked to S3 and S5. parseCommandGroups already snapshots the other per-command origin state for its groups (commandGroupsEspnowOrigin / commandGroupsSequenceBody, command_timer.cpp:106-107, re-applied at :229-232) — the source port is the one piece it does not carry.

**How it is reached.** Any un-prefixed line that contains ';t' or ';T' arriving on a mapped port or on a port with ?BCAST,IN,Sx,OFF — e.g. a delayed chain typed into a mapped private channel, or a device whose payload happens to contain the command char followed by t/T. The wiki states a mapping 'completely overrides normal broadcast behavior for that port' (wiki/Broadcast.md, 'Serial Mappings Take Priority'), and input.timer_keeps_source shows the same hole defeats input blocking on an unmapped port.

**Why it matters.** Data the user deliberately confined to one destination is sprayed to every local port and every board in the mesh, where another board may act on it; and the source port gets its own line echoed back, which can loop with a device that re-echoes. The intended destination still receives the line, so nothing is lost — it goes to too many places, not too few.

**Fix.** Carry the source port through the timer path the way the other origin state already is. Add a sourceID parameter to parseCommandGroups (declaration command_timer_queue.h:22, definition command_timer.cpp:89), store it in a file-scope commandGroupsSourceID beside commandGroupsEspnowOrigin (command_timer.cpp:106), and use it at the enqueue: command_timer.cpp:237 `parseCommandsAndEnqueue(cmd, commandGroupsSourceID)`. Update the three callers: WCB.ino:7263 passes the real sourceID; WCB.ino:461 (deferred queue drain) passes the source captured with the queued chain; WCB_Storage.cpp:630 (SEQ playback) passes 0 as today.

**Risk.** Timer groups begin honouring source-port suppression and input blocking, so a bench habit that relied on seeing the echo on the source port will change; a SEQ body that recalls a timer chain must not inherit a stale port (pass 0 there explicitly). Per the existing memory rule, the captured source must be snapshotted per chain and reset at every source, not left latched in a global across drains.


#### 46. Removing a serial mapping does not restore the port's broadcast flags faithfully: CLEAR,S<n> forces them back ON whatever they were, CLEAR,ALL never restores broadcast output at all

| | |
|---|---|
| **Status** | VERIFIED — map.set_flags_list_backup, replace_not_append, clear_one_restores_flags, clear_all_restores_output, input_port_loses_output PASS 2026-09-21 (20260921-102109) |
| **Owner** | `WCB_firmware` |
| **Effort** | M |
| **Tests** | `map.clear_one_restores_flags`, `map.clear_all_restores_output` |
| **Subsystem** | serial-mapping |

**Mechanism.** Creating a mapping force-disables both flags for the input port and persists that (blockBroadcastFrom=true, serialBroadcastEnabled=false — WCB_Storage.cpp:1826-1835) without recording what they were. removeSerialMonitorMapping then unconditionally sets blockBroadcastFrom=false and serialBroadcastEnabled=true (WCB_Storage.cpp:1977-1988) — so a port the user had deliberately silenced comes back broadcasting. clearAllSerialMonitorMappings clears blockBroadcastFrom (WCB_Storage.cpp:2008-2010) but never touches serialBroadcastEnabled and never calls saveBroadcastSettingsToPreferences (WCB_Storage.cpp:2002-2022), so every formerly-mapped port is left broadcast-output-OFF in RAM and in NVS, with only 'All serial mappings cleared' printed. Entry points: ?MAP,SERIAL,CLEAR,ALL (WCB.ino:5103), ?MAP,CLEAR,ALL (WCB.ino:5162) and legacy ?SMCLEAR (WCB.ino:5913), the last of which is advertised in help (WCB_Help.cpp:99).

**How it is reached.** CLEAR,ALL: a builder tearing down mappings to start over — after it, the formerly-mapped port silently stops receiving broadcast commands and stays that way across reboots; the HIL suite's own module docstring already warns 'never CLEAR,ALL (it leaves broadcast output off)' (tests/hil/suites/s13_serial_mapping.py:9). CLEAR,S<n>: any port whose broadcast was deliberately turned off (a device that chokes on broadcast text, or a chatty input kept off the mesh) — setting and then clearing a mapping re-enables both directions, so that device starts getting broadcast text and its own input starts going mesh-wide.

**Why it matters.** Both halves leave the board in a state the user did not ask for and was not told about; the CLEAR,ALL half is persisted and makes a port look configured while ignoring every broadcast, which is a classic wrong-place debugging session. It is visible in ?config/?backup and one ?BCAST command fixes it, which is what keeps it below high.

**Fix.** Give the mapping a memory of the flags it overrode: add `bool prevBroadcastOut; bool prevBlockIn;` to SerialMonitorMapping (WCB_Storage.h:94-100), record the pre-mapping values at the point the auto-disable fires (WCB_Storage.cpp:1826-1835), persist them with two new keys in saveSerialMonitorMappings (WCB_Storage.cpp:1920-1946) defaulting to ON/ON so existing saved mappings behave as today, and restore from them in BOTH removeSerialMonitorMapping (WCB_Storage.cpp:1977-1988) and clearAllSerialMonitorMappings (WCB_Storage.cpp:2002-2022) — the latter must also call saveBroadcastSettingsToPreferences. Minimal fix for the CLEAR,ALL half alone: mirror WCB_Storage.cpp:1983-1988 inside the clearAll loop and add the save.

**Risk.** New NVS keys; loadSerialMonitorMappings must default them to true/false (today's behaviour) for mappings saved by older firmware. map.set_flags_list_backup keeps passing because the port it uses is ON/ON before the mapping, so 'restore previous' prints the same two Auto- lines it asserts. The same class of bug exists in the PWM path (WCB_PWM.cpp:883-885) — fix them together or the two subsystems drift.


#### 47. The same cycle guard also refuses a second, non-recursive call to the same sub-sequence within one body — its key set is never popped

| | |
|---|---|
| **Status** | VERIFIED — `seq.cycle_guard_reuse` and `seq.reuse_queue_reserve` failed on the 09-22 image (baseline 20260923-094621) and pass on the 09:49 image, with every other `seq.*`, `if.*` and `legacy.*` test (targeted run 20260923-095057) |
| **Owner** | `WCB_firmware` |
| **Effort** | M |
| **Tests** | `seq.cycle_guard_reuse`, `seq.reuse_queue_reserve`, `seq.cycle_guard` (must stay green, unchanged) |
| **Subsystem** | vars-if-seq |

**Mechanism.** activeChainKeys/activeChainDepth (WCB.ino:6829-6850 when filed; :7014-7050 before the fix) was a membership set over every key expanded since the last top-level recall, not a stack: depth is reset only when !inSequenceBody (:6831-6832) and is never decremented on the way back out. Every token a body enqueues carries sequenceBody=true (WCB_Storage.cpp:628, snapshotted per item at WCB.ino:2207 and restored at dispatch at :8366), so a body like `;CBEEP^;t500^;CBEEP` takes the else-branch both times: the first call adds BEEP, the second matches at :6835 and returns at :6839 with the 'recalls itself (directly or in a loop)' message. Nothing about the second call is recursive — BEEP had already finished expanding. Two more failures from the same set: every expansion in a run counted toward the "nesting deeper than 8" limit, so a flat body calling 8 different sub-sequences had its 8th refused as too deep; and MP3/DFPlayer ONFIN/ONERR bodies (recallCommandSlot called directly, WCB_MP3.cpp:51-52, WCB_DFP.cpp:47-48) checked their nested ;C against a stale set left by the last top-level recall.

**How it is reached.** Reusing a sub-sequence twice in one sequence — a beep, a head-turn, a light flash used at two points in a routine. It is the documented sub-sequence pattern (WCB.ino:6766) applied twice.

**Why it matters.** The second call is silently dropped from the routine (only a refusal line on the console, which a droid running untethered has nobody reading), and the message blames a loop the user does not have. Half a sequence runs and the diagnostic points at the wrong thing.

**Fix (applied 2026-09-23).** Each queued command carries its own lineage: `CommandQueueItem::seqDepth` (in the struct's existing padding, still 12 bytes × 200 slots, static_assert) plus that many FNV-1a 32 key hashes in front of the command text in the same malloc (`commandItemBlock()` is the block start; both free sites use it). `enqueueCommand` stamps `seqCurPath` only on the loop task (`seqOnLoopTask()`, handle captured first in `setup()`), so serialCommandTask/WiFi-callback commands are always top-level. The drain restores the item's lineage into `seqCurPath` like `inSequenceBody`; `recallStoredCommand` refuses a key already on it, or depth 8, with the unchanged refusal texts; `recallCommandSlot` pushes the key while it enqueues the body (so ONFIN/ONERR bodies are fresh roots); `commandGroupsPath` carries it across `;t` groups (off the loop task `parseCommandGroups` writes only the depth byte). New containment the old guard never needed: a NESTED expansion that would leave fewer than `SEQ_QUEUE_RESERVE` (16) of the 200 queue slots free is refused whole with one "Command queue nearly full" line — back-to-back reuse expands every call before any leaf drains (FIFO), and the per-token "queue is full" flood on UART0 would stall loop(). docs/SEQUENCE_INVENTORY.md §3c, wiki Stored-Commands + Timer-Commands (the self-loop and state-machine ring examples were refused before and after).

**Rejected: "pop when that expansion's items have drained".** The queue is FIFO: in `;CBEEP^;CBEEP` the second call dispatches while the first BEEP's body is still queued BEHIND it, so a global stack popped on drain (or a key→generation table) still refuses it. The stack has to belong to each queued item.

**Original proposal (superseded).** Make it a real stack: push the key before recallCommandSlot (WCB.ino:6849-6850) and pop it when that expansion's items have drained — or, since recallCommandSlot enqueues rather than recursing, track (key, generation) and clear an entry once the queue no longer holds items from that expansion. The cheap interim that keeps cycle protection and unblocks reuse is to cap repeats instead of forbidding them (refuse only on the Nth re-expansion of the same key within one chain, N small), since a true cycle re-enters unboundedly while legitimate reuse does not. Do not just delete the check — it is what stops the endlessly-refilled queue described at WCB.ino:6820-6824. Verify against seq.cycle_guard, which asserts the refusal fires for HILRR/HILRS and the A→B→A ring.

**Risk.** Any loosening risks reopening the runaway-queue failure the guard was written for, so it needs the seq.cycle_guard test run on the bench before and after. I found this by reading the guard, not from a failing test — no HIL test covers a body calling one sub-sequence twice, which is worth adding alongside the fix. (Now `seq.cycle_guard_reuse`.) Remaining exposure: a user-built wide, deep tree of distinct keys is finite but can do far more work than the old 8-expansion cap; paced loops and rings that sometimes ran by accident under the old guard (any top-level recall between groups cleared the set) are now refused every time.


### Low impact (8)


#### 48. `;H,SETEMOTION,<e>,100` is inside the advertised range, accepted, then dropped by the library with no output at all

| | |
|---|---|
| **Status** | VERIFIED — hcr.setemotion_100_not_silent PASS on the bench (20260922-125537) |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `hcr.setemotion_100_not_silent` |
| **Subsystem** | devices-hcr-dfp |

**Mechanism.** WCB_HCR.cpp:311 accepts `v <= 100`, and both the usage line (:312) and the help (WCB_Help.cpp:623 `;H,SETEMOTION,e,0-100`) advertise 0-100. The call at :314 goes to the vendored `HCRVocalizer::SetEmotion`, which returns early on `if (v < 0 || v > 99) return;` (Code/WCB/src/HumanCyborgRelationsAPI/hcr.cpp:432). Nothing is written and nothing is printed. Confirmed in the log at 5048.790: `;H,SETEMOTION,H,100` produced zero bytes on W2 S4 and zero console lines. The value 100 behaves three different ways across the three entry points: SETEMOTION drops it silently, `;H,VOL,100` clamps to 99 (`hcrSetVol` → `constrain(v, 0, 99)`, WCB_HCR.cpp:86), and `;H,FN,2,1,100` prints `[HCR] FN 2,1,100 rejected` (HcrCodec::normalize case 2 caps track at 99, WcbCmd/src/WcbHcr.cpp:10).

**How it is reached.** Type the top of the documented range. `;H,SETEMOTION,H,100` is the obvious way to say "maximum happy", and 101 correctly prints usage — so the user learns the range is 0-100 and then finds that exactly 100 does nothing.

**Why it matters.** One dead value at the end of one verb's range, and 99 works. It rates a mention rather than a fix-first because the cost is entirely diagnostic: total silence (not even "Unknown command") is the same signature as a dead port or a mis-wired HCR, which is the failure mode CLAUDE.md rule 12 was written about — so the user's first suspicion is the wiring, not the value.

**Fix.** WCB_HCR.cpp:314 — `_hcr->SetEmotion(e, constrain(v, 0, 99)); _hcr->update();`. Clamping (rather than rejecting) matches `;H,VOL,100` → 99 and keeps the advertised 0-100 honest, so no help text changes. The alternative — tighten :311 to `v > 99` and change both WCB_HCR.cpp:312 and WCB_Help.cpp:623 to `0-99` — also satisfies the test but leaves VOL and SETEMOTION describing the same 0-100 input differently.

**Risk.** None meaningful. `;H,SETEMOTION,H,101` still falls into the usage branch at :311 and `hcr.bad_args` stays green; the emitted bytes for 0-99 are unchanged. Note the fix only touches the readable verb — `;H,FN,2,<c>,100` keeps printing its rejection, which is correct behaviour and already covered by `hcr.fn_codec`.


#### 49. `;H,FN` truncates the function number to 8 bits (fn 260 silently executes fn 4) and skips the track range check on fn 14

| | |
|---|---|
| **Status** | VERIFIED — hcr.input_validation_gaps PASS on the bench (20260922-125537) |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `hcr.input_validation_gaps` |
| **Subsystem** | devices-hcr-dfp |

**Mechanism.** Two independent holes in the FN branch. (a) WCB_HCR.cpp:264 parses `fn` as `int` with no range check, and :279 passes `(uint8_t)fn` to `hcrCodec.emit` — so 260 wraps to 4 (Stimulate), normalize accepts it, the emit succeeds, and the rejection printf at :282 never runs. Proven: `;H,FN,260,0,50` → `<SH50,QEH,QT>` (log 5054.400/5054.472), byte-identical to `;H,FN,4,0,50` in the green `hcr.fn_codec` test. (b) fn 14 is deliberately routed around the codec to keep the library's 150 ms per-channel debounce (:270-278, and hcr.cpp:462-463), but the guard at :274 only checks the channel — so `HcrCodec::normalize`'s `track >= 0 && track <= 9999` for case 14 (WcbCmd/src/WcbHcr.cpp:25) is skipped entirely. `;H,FN,14,1,12345` emits `<CA12345,QPA>` (log 5055.201/5055.281): a 5-digit track in the HCR's 4-digit file field. The readable verb does check this — `fl < 0 || fl > 9999` at :341.

**How it is reached.** FN is the numeric RC-controller dispatch path, so fn/chan/track arrive as stored data from a NaviCore RC action or a saved sequence rather than as typed text. A mis-set or off-by-a-lot value does not error — it aliases into a different, VALID command and executes it, which is strictly worse than being rejected. Narrower than the VOL gap because it needs an out-of-range number rather than a plausible typo.

**Why it matters.** Needs a deliberately or accidentally out-of-range value to trigger, and the readable verbs (which is what most people type) are correct. The reason it is worth fixing at all is the failure MODE: fn 260 does not fail, it does something else, and the console prints the `[HCR-DBG]` line for the number the user asked for (:285), so even with debug on the log agrees with the user and disagrees with the wire.

**Fix.** WCB_HCR.cpp:264 — range-check before the cast so the existing rejection printf at :282 fires: reject `fn < 2 || fn > 19` up front (that is the whole set HcrCodec::normalize handles, WcbHcr.cpp:8-35), or at minimum `fn < 0 || fn > 255`. WCB_HCR.cpp:274 — extend the fn-14 guard to the track: `if (chan < 0 || chan > 2) { …existing… } if (track < 0 || track > 9999) { Serial.printf("[HCR] FN 14 track %d out of range (0-9999)\n", track); return; }` — matching the codec's case-14 range and the readable PLAY verb's.

**Risk.** `hcr.fn_codec` asserts an exact list of rejection strings, including `[HCR] FN 14 channel 3 out of range (0-2)`. Adding a new message for the track case does not disturb it (its FN-14 rejects are channel-only), but the exact wording of any NEW line must not collide with the `FN %d,%d,%d rejected (unknown fn or out-of-range)` format it also asserts. If you take the tighter `fn < 2 || fn > 19` bound, check it against the reject list in that test — `;H,FN,1` and `;H,FN,12` are expected to produce the generic rejected line, and they still will.


#### 50. An ensured broadcast whose payload starts with '{' is never ACKed but IS retried as unicast, so every board processes it twice [owner: WCBClient repo, plus a smaller WCB-side twin]

| | |
|---|---|
| **Status** | VERIFIED — client_mesh.json_broadcast_once PASS on the bench (20260922-125537) |
| **Owner** | `unclear` |
| **Effort** | S |
| **Tests** | `client_mesh.json_broadcast_once` |
| **Subsystem** | etm-mesh |

**Mechanism.** PRIMARY OWNER IS THE WCBClient REPO (C:/Users/ghulette/Documents/GitHub/WCBClient) — the schema's owner enum has no value for it. There is a smaller twin in the WCB firmware, described below. The WCB deliberately refuses to ACK a target-0 frame whose payload starts with '{' — `bestEffortTelemetry` at WCB.ino:4346-4349, with a long comment (4337-4345) explaining that ACKing a 5 Hz telemetry stream saturates its own TX queue. It also deliberately keeps that frame's seq OUT of the duplicate ring (WCB.ino:4360-4367, "don't let best-effort telemetry churn the ring"). Both decisions assume the sender never retries. Both senders do. WCB_Client::broadcast() defaults to `ensured = true` (WCB_Client.h:627, documented 615-626 with no JSON caveat) and tracks the slot (WCB_Client.cpp:2186-2193); update() then retransmits as PER-BOARD UNICAST reusing the seq (WCB_Client.cpp:283-296). The retry arrives with `targetWCB == WCB_Number`, so it is no longer bestEffortTelemetry: the WCB ACKs it, finds no ring entry for the seq (the broadcast never made one), and processes it a second time. The bench counted exactly 2 '[ETM] Received seq N from WCB18: {...}' lines for one broadcast. WCB-side twin: the generic broadcast fan-out ends in `sendESPNowMessage(0, cmd.c_str())` with useETM defaulting to true (WCB.ino:7102, declared 977), so a '{'-leading line typed on the console or arriving on a broadcast-input serial port goes out ensured, is ACKed by nobody, and is retried once as unicast to each online board — one extra execution per board plus an ACK, before the second and third retries finally dedup against the ring entry the first retry created.

**How it is reached.** Not by any shipped caller today, which is why this is rated low. I checked all of them: NaviCore's rc_hb / rc_ch / rc_trig / rc_mode all pass ensured=false explicitly (rc_telemetry.h:1690, 1723, 1741, 1753), its serial fan-out passes false (NaviCore.ino:3714), and MgmtRelay explicitly drops JSON before broadcasting (MgmtRelay.ino:1236, "if (line[0] == '{') return;"). What IS reachable: a JSON string typed into the config tool's WCB_SEND with target 0 (rc_telemetry.h:1521 and NaviCore.ino:4036 both use the ensured default), a '{'-leading line arriving on a WCB broadcast-input port, and any builder writing their own WCB_Client sketch — the API documentation invites exactly this and warns about …

**Why it matters.** Real duplicate execution and 4x airtime when hit, but no shipped caller hits it, so it costs a droid builder nothing today. It is a loaded trap rather than a live failure: the WCB_Client header presents ensured broadcast as the safe default, and the one payload shape the firmware silently exempts is the one shape that default breaks on. Worth fixing while the two one-liners are cheap, not worth scheduling ahead of anything above.

**Fix.** Symmetric one-liners on both send sides, mirroring the receiver's own predicate. WCBClient (primary, since that is where the misleading default lives): in WCB_Client::broadcast() (WCB_Client.cpp:383-394), force `ensured = false` when `command[0] == '{'`, with a comment citing WCB.ino:4346-4349, and update the ensured= documentation at WCB_Client.h:615-626 to say a JSON broadcast is best-effort by protocol. WCB firmware (secondary): at WCB.ino:7102 pass `useETM = false` when `cmd[0] == '{'` for the same reason. Do NOT fix this by making the WCB ACK or ring target-0 JSON — the comments at WCB.ino:4337-4345 and 4360-4362 each record a specific failure that change would reintroduce.

**Risk.** Changes a '{'-leading broadcast from ensured to best-effort. That is a no-op in practice because the WCB never runs a '{' payload as a command (it is consumed by the JSON relay branch at WCB.ino:4416, 4439-4468) and no shipped caller relies on the retry. Anyone genuinely needing guaranteed JSON delivery must unicast it, which is already the only path that works — worth one line in the header doc. If Greg would rather not silently downgrade, a one-time printed warning instead of a silent flip is equally cheap.


#### 51. getMovingState stores the raw reply byte instead of 0/1, diverging from its own comment and from WcbCmd's decoder

| | |
|---|---|
| **Status** | VERIFIED — maestro.get_moving_state_normalised PASS on the bench (20260922-135805) |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `maestro.get_moving_state_normalised` |
| **Subsystem** | maestro-kyber |

**Mechanism.** WCB_Maestro.cpp:455 decodes by hand: `int32_t value = (expectedBytes == 1) ? reply[0] : (reply[0] | (reply[1] << 8));` — the 1-byte case is the raw byte. The comment at :380 says the variable is 0/1, and `WcbMaestro::decodeReply` in the shared library normalises it (`value = bytes[0] ? 1 : 0`, WcbCmd/src/WcbMaestro.cpp:207) — the decoder NaviCore uses for its own local reads (NaviCore.ino:869). Bench evidence: a probe rule answering 0x05 to `AA 01 13` left `m1moving = 5`. Note what this does NOT break: NaviCore re-normalises anything arriving via `:MQR` at NaviCore.ino:807 (`c.moving = (val != 0)`), so the divergence is confined to the WCB's own RAM variable.

**How it is reached.** Barely. A conformant Pololu Maestro only ever answers 0x00 or 0x01 to Get Moving State, and :440 flushes stale bytes before the request, so with real hardware the raw byte already is 0/1 — the test had to inject 0x05 synthetically. The one genuine path is a desynchronised reply stream (a previous getPosition's low byte arriving after the flush), which would store e.g. 112 and make both `IF,m1moving=1` and `IF,m1moving=0` fail, stalling the sequence.

**Why it matters.** Real divergence, effectively unreachable with correct hardware, and NaviCore is unaffected. Worth fixing for the one-decoder principle rather than for field impact.

**Fix.** WCB_Maestro.cpp:455 — replace the hand-rolled decode with the shared one: pick the `WcbMaestro::ReplyKind` from the verb (POS/MOV/ERR, the same mapping :468-470 already computes for the `:MQR` KIND) and call `WcbMaestro::decodeReply(kind, reply, got, v)`. WcbCmd needs no change — it is already correct and is the reference.

**Risk.** None functionally. If you also lift the KIND selection out of :468-470 to share it, keep `maestroGetInfo`'s expectedBytes and `WcbMaestro::replyLen` consistent or a reply length changes under the timed read.


#### 52. A get for an unconfigured id inside WCBQ broadcasts where the subroutine and verb fallbacks unicast — real inconsistency, but the code documents the broadcast as deliberate

| | |
|---|---|
| **Status** | VERIFIED — Greg 2026-09-22: switch to unicast. A get with no slot now unicasts to WCB<dev> inside WCBQ (broadcast only above it), same rule as the subroutine and verb fallbacks (WCB_Maestro.cpp handleMaestroGet); debug line no longer prints WCB0. maestro.get_fallback_unicast_within_wcbq PASS; navicore.maestro_inventory PASS; maestro.* + kyber.* 20 PASS / 12 SKIP |
| **Owner** | `unclear` |
| **Effort** | S |
| **Tests** | `maestro.get_fallback_unicast_within_wcbq` |
| **Subsystem** | maestro-kyber |

**Mechanism.** The three fallback paths disagree. Subroutine: `if (maestroID >= 1 && maestroID <= Default_WCB_Quantity) sendESPNowMessage(maestroID, …)` (WCB_Maestro.cpp:223-226). Verb: `uint8_t target = (dev >= 1 && dev <= Default_WCB_Quantity) ? dev : 0;` (:368). Get: no WCBQ test at all — `if (hostWCB > 0) unicast; else sendESPNowMessage(0, fwd)` (:431-432), with the comment 'unconfigured → let the host find it'. Bench evidence with WCBQ raised to 5 and no M5 slot: `;M5,goHome` was not ACKed by WCB2 (unicast), `;MG5,1,getErrors` was (broadcast). Behaviourally the outcome is the same — a board receiving a `;MG` it cannot service drops it at :428 (`if (lastReceivedViaESPNOW) return;`) — so nothing wrong is actuated. The cost is ETM fan-out: per CLAUDE.md rule 2 one broadcast becomes one pending ACK per known board with up to 3 retries, so a polled get costs N round-trips instead of 1 and stays pending until timeout if any board is offline. Against that, the broadcast is strictly more capable: it reaches a WCB_Client host (a NaviCore at id 20) that happens to host Maestro 5, which a unicast to WCB5 never would.

**How it is reached.** `;M<id>,get*` for an id inside WCBQ with no slot for it — i.e. before WDP has advertised the host, or on a board where auto-join is off. Normal but transient.

**Why it matters.** No lost or misdirected commands; the cost is extra ETM ACK traffic per poll and a longer failure window when a board is down. The surprise value of `;M5,goHome` and `;M5,getErrors` taking different routes is the stronger argument, and it is an argument about consistency, not correctness.

**Fix.** If you want the test to pass: WCB_Maestro.cpp:432 — mirror :368, `uint8_t target = (dev >= 1 && dev <= Default_WCB_Quantity) ? (uint8_t)dev : 0; sendESPNowMessage(target, fwd.c_str());`. If you keep the broadcast, tighten the comment at :432 to say explicitly that a get deliberately broadcasts inside WCBQ where the sub/verb paths unicast, and why — and retire the test. I lean toward keeping the broadcast and documenting it; the discovery property is worth more than N-1 ACKs. Either way, fix the debug line at :433, which prints 'WCB0' on the broadcast path.

**Risk.** Switching to unicast loses the ability to find a host whose WCB number is not the Maestro id — including a NaviCore hosting that id — which is the case the comment was written for. Do not change this without checking against a NaviCore-hosted Maestro on the bench.


#### 53. activePWMCount is read but never assigned, so a board whose PWM is input mappings only never advertises the PWM capability

| | |
|---|---|
| **Status** | VERIFIED — pwm.input_only_advertises_cap PASS on the bench (20260922-125537) |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `pwm.input_only_advertises_cap` |
| **Subsystem** | pwm |

**Mechanism.** `int activePWMCount = 0;` is defined at WCB_PWM.cpp:24 and declared at WCB_PWM.h:24, and the only read in the whole tree is `if (pwmOutputCount > 0 || activePWMCount > 0) f |= WDP_CAP_PWM;` at WCB_WDP.cpp:112. A grep across Code/ finds no assignment anywhere — it is dead, permanently 0. So WDP_CAP_PWM reflects only output-port declarations; a board that is purely a PWM *source* (input mappings driving remote outputs) never sets the bit. Report row: `W1 (input mapping only) CAP=208 lacks 0x0020` — 208 = 0x10 MAESTRO_REM | 0x40 CONTROLLER | 0x80 MAESTRO_LOC (WCB_WDP.h:44-47), exactly W1's other capabilities and no 0x20.

**How it is reached.** Configure a mesh PWM mapping (`?MAP,PWM,S3,W2S3`) on a board with no local output ports, then read `?WDP,DUMP` on any peer — the source board shows no 'P' flag (WCB_WDP.cpp:899) and no "PWM" in the long listing (:918).

**Why it matters.** Diagnostic only — nothing routes on this bit. A grep for WDP_CAP_PWM across Code/ finds only the setter at WCB_WDP.cpp:112 and the two display tables at :899 and :918; there is no routeStoredOrCap call using it (unlike WDP_CAP_MP3 / WDP_CAP_DFPLAYER at WCB.ino:6541 and :6544), and WCBClient only names WCB_CAP_PWM in a display table in its NeighborDiscovery example. So the cost is a ?WDP listing that under-reports what a board does — the PWMTARGET rows still show the real driving relationship, which is why pwm.passthrough_mesh and pwm.bcast_skips_remote_output_port both pass.

**Fix.** Either delete activePWMCount (WCB_PWM.cpp:24, WCB_PWM.h:24) and change WCB_WDP.cpp:112 to count live mappings directly — `bool anyPwmMapping = false; for (int i = 0; i < MAX_PWM_MAPPINGS; i++) if (pwmMappings[i].active) { anyPwmMapping = true; break; }` — or keep the variable and maintain it at the four places mapping activation changes: addPWMMapping :279, removePWMMapping :377, clearAllPWMMappings :485 and loadPWMMappingsFromPreferences :617. The counting helper is preferable: it cannot drift, and wdpCapFlags is not a hot path.

**Risk.** Very low. The cap bitmap is on the WDP wire (CLAUDE.md rule 10), but this only starts SETTING a bit that is already defined (WDP_CAP_PWM 0x0020, WCB_WDP.h:45) and that older peers already parse — no TLV change, no layout change, no older-peer parse break. Worth a dated row in docs/WDP_DESIGN.md recording that the PWM cap now means "this board does PWM as source or sink".


#### 54. The test's bar for 38400 soft RX is above what the hardware can deliver — that assertion, not the firmware, is what failed

| | |
|---|---|
| **Status** | VERIFIED — input.soft_rx_baud_sweep PASS on the bench (20260922-125537) |
| **Owner** | `test_is_wrong` |
| **Effort** | S |
| **Tests** | `input.soft_rx_baud_sweep` |
| **Subsystem** | real-failures |

**Mechanism.** input.soft_rx_baud_sweep (tests/hil/suites/s12_serial_input.py:712-731) asserts counts[19200] == 20 and counts[38400] == 20. 19200 was perfect; 38400 gave 17 of 20 with no soft TX anywhere on the board, no mesh load created by the test, a probe hardware UART doing the injection and a hardware WCB port (S2) doing the readback — so there is no firmware lever left that would turn 17 into 20. The library's own README says so: 'due to the fact that the ESPs always have other activities ongoing, there will be some inexactness in interrupt timings. This may lead to inevitable, but few, bit errors when having heavy data traffic at high baud rates' (README.md:26-29). 57600 scoring 18 against 38400's 17 confirms the numbers are noise around an ~85-90% plateau, not a rate-dependent firmware effect.

**How it is reached.** Not user-reachable — it is a test bar, not behaviour. A builder reaches the underlying reality only by running a soft port above 19200, which defect 4 addresses.

**Why it matters.** Costs nothing in the field. It costs a red row in every future HIL run, which is how a real regression gets missed.

**Fix.** Move the assertion to 19200 only and record 38400/57600/115200 as metrics, matching what the test title already promises ('exact at 19200 and 38400 (57600/115200 recorded only)' -> 'exact at 19200; 38400 and up recorded only'). Or keep 38400 as a soft floor with a tolerance (>= 18 of 20) and a bench.note. Do this only after defect 1 lands, so the 19200 arm is exercised against the fixed firmware.

**Risk.** Loosening a bar can hide a future regression at 38400 — keep the recorded counts in bench.note so a drift from ~17 to ~5 is still visible in the report.


#### 55. Residual ~0.3% soft-serial TX mis-frame under mesh load: EspSoftwareSerial re-anchors the bit clock before it re-disables interrupts, so the start bit of each byte can come out short

| | |
|---|---|
| **Status** | OBSOLETE — bit-banged TX is gone from S3-S5 (RMT, see #16); the path this defect lived in now runs only as a no-RMT-channel fallback |
| **Owner** | `WCB_firmware` |
| **Effort** | M |
| **Tests** | `hcr.softserial_integrity_mesh_load` |
| **Subsystem** | real-failures |

**Mechanism.** With S5 protected, 300 HCR frames under ~1457 concurrent mesh commands gave '300 frames, 1 wrong, RXERR S5 0, S2 lost 0/1457' (session.log:51453) — frame 207 came out as 3C 43 41 30 32 30 37 2C 54 15 F9 0A instead of <CA0207,QPA>\n: correct through byte 8, then three wrong bytes and one byte short (session.log, probe2 RX B at t=5241.286). The write is a single Print::write() call (WCB_HCR.cpp:255, _hcrPort->print(payload)), so the corruption is INSIDE one critical section. The one interrupt window inside a protected write is lazyDelay() (SoftwareSerial.cpp:265-283): it calls restoreInterrupts(), waits, then preciseDelay() — which re-anchors m_periodStart to the ACTUAL current tick (:284-290) — and only AFTER that calls disableInterrupts() (:282). An interrupt taken between preciseDelay() returning and the following level write makes the next start bit short by the ISR latency while the bit clock has already been re-anchored, so the receiver's sample points drift inside the byte. This is the same re-anchor race the firmware comment at WCB.ino:2046-2049 identifies for mux contention; it applies equally to a plain ESP-NOW ISR. The remaining possibility I could NOT rule out from source is a cache stall from an SPI-flash op on either core (NVS commits), which taskENTER_CRITICAL does not prevent.

**How it is reached.** An H-CR, MP3 Trigger, DFPlayer or WLED on S3-S5 on a board carrying normal mesh traffic. The measured rate is roughly one wrong frame per 300 — a droid plays a wrong sound or drops a command every few minutes of heavy use. It is a 140x improvement on the 45% pre-fix rate, not a regression.

**Why it matters.** Rare, self-limiting (the next command is usually right), and already far better than before. It matters because the failure is silent and the symptom — an H-CR occasionally playing the wrong track — reads as a device fault, sending the builder to the wrong place.

**Fix.** Do not chase this in WCB code — the race is in the library. Two honest options. (a) Accept it and document the number: soft ports are ~99.7% per frame under mesh load, hardware S1/S2 are exact; put that in docs/HIL_TESTING.md §6 and the wiki so nobody re-debugs it. (b) If Greg wants it closed: vendor EspSoftwareSerial 8.1.0 under Code/WCB/src/ the way HumanCyborgRelationsAPI is vendored (CLAUDE.md rule 7) and reorder lazyDelay() so disableInterrupts() runs BEFORE the final preciseDelay() rather than after it — the sub-millisecond remainder is then busy-waited with interrupts already off, costing at most one extra bit period of blackout per byte. Then re-run the test to see whether the rate actually moves. The durable structural answer, if soft ports are ever to be first class, is RMT-driven TX on S3-S5: hardware-timed waveforms need no interrupt blackout at all and would fix this AND the defect above together, at the cost of a real project.

**Risk.** Vendoring EspSoftwareSerial means the sketchbook copy no longer shadows it for the WCB build but still does for tests/hil/wcb_probe and every other sketch — two copies to keep in step, and build.sh's prerequisite comment (Code/bin/build.sh:15) needs updating. The lazyDelay reorder lengthens the interrupt blackout slightly, which makes defect 1 marginally worse on any port that is protected. Do defect 1 first.

#### 56. ?ETM,CHAR phase 3 drops its own broadcasts whenever a peer's load traffic has just arrived

| | |
|---|---|
| **Status** | VERIFIED — etm.char_loaded PASS 2026-09-21 (20260921-102817) |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `etm.char_loaded` |
| **Subsystem** | etm |

**Mechanism.** `processETMChar()` and `processETMLoad()` send from `loop()`, outside any command snapshot, so their broadcasts go through `sendESPNowMessage`'s loop-prevention gate (`if (target == 0 && lastReceivedViaESPNOW) return;`) with whatever value the last received command left. Phase 3 is the one phase where peers send commands back — the load generator's `LOAD_*` unicasts — and each one sets the flag, so the characterisation's broadcast third was dropped and reported as a 30 % miss. Passed on 09-16 on timing alone.

**Fix.** `sendOwnBroadcast()` (WCB.ino, above `startETMChar`) clears the flag for the one send and restores it, the same pattern `recallStoredCommand` uses (WCB_Storage.cpp). Used by the ETMLOAD kick-off, the phase-3 broadcasts and the load generator's own broadcasts.

**Risk.** None beyond the three call sites: these payloads are board-originated test traffic, never a relayed command, so there is no loop to prevent.

#### 57. USB commands arriving while W1 bit-bangs a mesh raw block onto S3 are corrupted — the line loses its ';' and is broadcast as text

| | |
|---|---|
| **Status** | VERIFIED — input.softserial_core0_contention PASS on the bench (20260922-125537) |
| **Owner** | `WCB_firmware` (or hardware — see below) |
| **Effort** | M (needs a bench session) |
| **Tests** | `input.softserial_core0_contention` |
| **Subsystem** | soft-serial / UART0 input |

**Evidence.** 2026-09-21: 3 failures in 4 runs of the test (the GUI full run, a GUI rerun, and 1 of 2 CLI single runs; it passed in the 09-16 and 09-21 morning full runs). Every time, the FIRST `;S4,<64 chars>` sent over W1's USB right after the probe's MRAW (a 151-byte raw block W1 writes to protected S3 at 9600, ~157 ms) arrives mangled: `;S4,HILL21…` became `7S4,HILL21…`, another `;S4,HILL24…` became `784,HILL24…`, and the line before each is one byte short (63 of 64). With no leading `;` the line is plain text, so W1 broadcasts it to every port — which is what corrupts the S3 block (interleaved) and shows up on S2/S5. In the CLI failure S3's own output was also badly mistimed (bytes doubled: `11 11 15 15 19 19 …`).

**What is known.** `;` 0x3B -> `7` 0x37 is the same bit pattern shifted by one position, which looks like a mistimed sample rather than noise. But W1's console is hardware UART0, and EspSoftwareSerial re-enables interrupts after every stop bit (`lazyDelay`, SoftwareSerial.cpp:265), so the UART0 RX ISR is serviced about every millisecond — well inside its 128-byte FIFO. So the obvious "interrupts masked for the whole write" explanation (CLAUDE.md rule 13's README note) does NOT fit on its own.

**Next steps.** (1) Repeat with S3 UNprotected (`enableIntTx(true)`) — if the USB corruption vanishes, the masking is involved after all. (2) Repeat with the USB lines sent slower (e.g. 20 ms apart) — distinguishes loss from corruption. (3) Capture W1's UART0 RX with ?DEBUG to see the raw line as parsed. (4) Check whether the pattern follows W1's hardware (swap the roles of W1/W2).

**Why it matters.** Any host (Wizard, NaviCore bridge, a Pi) feeding a WCB over USB while that WCB is driving a soft port from the mesh can have a command silently turned into a broadcast to every port.

**Resolution (2026-09-21).** Proven by A/B on W1 with the contention test: protected S3 (normal) mangled USB lines in 2 of 5 runs; S3 UNprotected, 0 of 6 (but 0/10 S3 blocks exact — the rule-13 trade); protected S3 with the UART0 driver installed from a core-0 task, **0 of 8, 10/10 blocks, 8/8 PASS**. Mechanism: the missing byte in the line after each mangled one sat at index 55-58, exactly the '7'/'8' that replaced ';S' — stale FIFO slots, not bit errors. The UART0 RX ISR was allocated on core 1 (uart_driver_install intr_alloc_flags 0 = calling core; setup() is on core 1), the core where every protected soft-serial byte masks interrupts; the classic ESP32's rxfifo count is "not credible" (the IDF's words, uart_ll_get_rxfifo_len rebuilds it from rd/wr pointers) and a held-off ISR reads stale slots. Fix: beginUsbSerialOnCore0() in WCB.ino. ?STATS also prints the previously invisible serialRxOverflows counter when non-zero.

#### 58. A ~2950-character ?SEQ,SAVE value fails to copy even with a 38.9 KB contiguous block free

| | |
|---|---|
| **Status** | VERIFIED — EXPLAINED; inv.seqget_largest (the replacement test) PASS in 20260922-215719 and 20260923-002254; heap metric fixed |
| **Owner** | `WCB_firmware` |
| **Effort** | S to report (done), M to explain |
| **Tests** | `inv.seqget_largest` (replaces `inv.seqget_toobig`, whose premise was unreachable) |
| **Subsystem** | storage |

**Evidence (2026-09-21).** `?SEQ,SAVE,HILTB,<2950 chars>` intermittently reached saveStoredCommandsToPreferences with `message.substring(commaIndex + 1)` coming back EMPTY, and printed "Key or value cannot be empty". The line itself arrived intact (debug: 2966 characters parsed) and long chains (268 commands, 2679 chars) run fine, so it is not USB input. Arduino String's copy() empties the string when reserve()/realloc fails — but ?STATS (new heap line) showed the largest free block at 38,900 bytes at the moment of failure, so plain exhaustion does not explain it. 2600-character values never fail; they reach NVS and are rejected there (as are 1500+).

**Done.** The save path now reports "Out of memory: could not copy a <n>-character value (largest free block <m> bytes)" instead of the misleading empty-value line; ?STATS prints free heap, largest block and minimum since boot.

**Why it is low priority.** NVS already rejects sequence values from ~1500 characters, so a value large enough to hit this can never be stored anyway.

**Resolution (2026-09-22, run 20260922-151421 triage, adversarially verified).** The "38,900-byte largest block" was the wrong heap. `ESP.getMaxAllocHeap()` measures `MALLOC_CAP_INTERNAL`, which on the classic ESP32 includes the ~39 KB IRAM heap. That heap is 32-bit only, and malloc/String never use it. The value was exactly 38,900 in all 105 Heap lines across every run and boot. The byte-addressable DRAM actually free was about 26.8 KB, fragmented, and the ?SEQ,SAVE path holds about 7 full-length copies of a ~2,960-character line when it asks for an 8th. So this is ordinary exhaustion, not a bug. NVS then rejects values from about 1,500 characters on a configured board anyway, so a value over 2,903 characters (the TOOBIG threshold) cannot be stored on this bench. **Fix:** ?STATS, the out-of-memory line and ?WIFI now report byte-addressable heap (`heap_caps_*(MALLOC_CAP_8BIT)`). The comment at the ?STATS Heap line records the trap. The test became `inv.seqget_largest`: it walks down a length ladder to the largest value that stores, requires a clean refusal (never "cannot be empty") and no phantom key on each step, round-trips the stored value, and checks TOOBIG only if the length ever allows it.

#### 59. A Maestro port on a Kyber board has two readers: the serial parser takes bytes the Kyber bridge should forward

| | |
|---|---|
| **Status** | VERIFIED — both tests PASS 2026-09-22 (run 20260922-144915), with kyber.local_mode_s2, remote_roundtrip, local_rejected_port_keeps_forwarding and all 21 maestro.* still PASS |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `kyber.maestro_s2_single_reader`, `kyber.clear_warns_reboot_single_reader` |
| **Subsystem** | maestro-kyber |

**Evidence (full run 20260922-125537 and the 2026-09-22 rerun).** With a local Maestro on S2 of a Maestro_Remote board, W1 logged `Processing input from Serial2: H9138DHMH2413789EJN` and `MA16AFKL015AFKN` — Maestro reply bytes parsed as command text. After `?KYBER,CLEAR` on a Maestro_Remote board, the same S1 traffic was split: 6 fragments parsed and 567 bytes bridged. `?KYBER,CLEAR` printed no reboot notice.

**Mechanism.** `forwardMaestroDataToRemoteKyber()` and `forwardMaestroDataToLocalKyber()` read EVERY local Maestro port, not one fixed port. But `serialCommandTask` skipped only S1/S2, and only by mode flag, so a Maestro on another port was also read by `processIncomingSerial`. Two readers split each frame between them. Separately, `KyberRemoteTask` is created in `setup()` and never deleted. `?KYBER,CLEAR` clears `Maestro_Remote`, so the parser's normal-mode branch reads S1 again while the still-running task keeps draining it.

**Fix.** WCB.ino: `setup()` records which bridge tasks it created (`kyberLocalTaskStarted` / `kyberRemoteTaskStarted`) before `serialCommandTask` starts. `maestroPortOwnedByKyberBridge(port)` is true for a local Maestro port while a started task's live gate is open, and `processIncomingSerial` returns early for such a port. That is one reader per port, and no port is left unread. `forwardMaestroDataToRemoteKyber()` returns when neither Kyber mode is live, so after a CLEAR the port goes back to the parser. It is gated on `!Maestro_Remote && !Kyber_Local`, not on `Maestro_Remote` alone, because a runtime `?KYBER,LOCAL` clears `Maestro_Remote` while the task must keep bridging until the reboot (`kyber.local_mode_s2`, which a workflow verify caught). WCB_Storage.cpp: `?KYBER,CLEAR` prints the reboot notice, but only on a board that was in a Kyber mode, because every plain config push sends `?KYBER,CLEAR`. Both targets compile (ESP32 69% / S3 67%).

**Risk.** It changes who reads a Maestro port on every Kyber board. `kyber.local_mode_s2`, `kyber.remote_roundtrip` and `kyber.wdp_auto_remote_revert` cover the paths that must still bridge. **Not fixed, same family:** a runtime LOCAL -> REMOTE switch leaves `kyberLocalPort` set, so `KyberLocalTask` keeps draining the Kyber port until reboot. That window already requires a reboot and says so.

#### 60. Harness: a probe with more hardware-only wires than hardware channels silently reads one wire's bytes under another's name

| | |
|---|---|
| **Status** | VERIFIED — probe2 rewired (W2S1 tap -> header S2, W2S5 -> header S5) and rediscovered; both tests PASS with the six tap-dependent tests (20260922-150647) |
| **Owner** | `harness` |
| **Effort** | S (guard) + bench rewire |
| **Tests** | `navicore.plain_broadcast_both_ways`, `input.bcast_fanout` |
| **Subsystem** | hil/links.py |

**Evidence.** In `navicore.plain_broadcast_both_ways`, W2S2 "received" 1 copy where silence was expected, and W2S5 "received" 0 where 1 was expected, in both directions.

**Mechanism.** Probe2 has two hardware channels (A/B). It carries three wires that each need one at once: the W2S1 tap at 115200 (above `SW_MAX_BAUD` 38400), W2S2 on header S1 and W2S5 on header S2 (both in `HW_ONLY_HEADERS`). `LinkManager.bind()` evicts the least recently used link. But the probe keeps RX history per channel LETTER, so a read with a `since` older than the eviction returned the previous owner's bytes as this wire's.

**Fix.** `hil/links.py`: `LinkManager.handoff` records the probe log mark when a channel passes to a DIFFERENT link. A re-bind of the same link (a baud change) is not a hand-off. `Link._since_ok()` raises a named `AssertionError` when a read's `since` predates that mark, and every `since` read goes through it. **Bench:** on probe2, move the W2S1 tap to header S2 and W2S5 to header S5, then run `python tests/hil/run.py --discover`. W2S5 then sits on a soft-capable header, which leaves A/B for the tap and W2S2.

#### 61. A rejected local OTA re-BEGIN at a raised rate leaves W1's USB stuck at 921600 with no session

| | |
|---|---|
| **Status** | VERIFIED — all named tests PASS on the current images (20260922-215719); was: FIXED (unverified) |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `ota.local_baud_rejected_rebegin_restores` |
| **Subsystem** | ota |

**Evidence (opt-in run 20260922-185238).** BEGIN,4096,0 OK, then BAUD,921600 OK, then BEGIN,4096,1 is rejected by the chip-family guard with `[OTA:BEGIN,ERR,0]`. 0.3 s later a ?VERSION sent at 921600 is answered at 921600: the board is stranded. Only an explicit ABORT sent at 921600 recovered it.

**Mechanism.** `otaBegin` tears down a live session before any of its guards run. The guard then rejects, and the BEGIN handler printed ERR and returned. The session is gone, so neither the idle reaper (gated on `ota.active`) nor `?OTALOCAL,BAUD` (refused without a session) can restore the rate. Every other session-ending failure already restored it. The same stranding follows the other rejects after the teardown: no inactive slot, bad size, `esp_ota_begin` failure. WCB_OTA_TECHNICAL.md claimed "the 30 s idle timeout owns the restore", which was false on this path.

**Fix.** `processOtaLocalCommand`, BEGIN branch: `if (!ok) otaRestoreLocalBaud();` AFTER the ERR marker, so the marker still goes out at the rate the host is on (the END,ERR contract). It is a no-op at 115200, which covers every Wizard BEGIN (the Wizard bumps only after BEGIN,OK). A re-BEGIN that succeeds keeps the rate. The test flushes the junk its 921600 probe leaves on a board already back at 115200. Diagnosis adversarially verified (workflow wf_c426e268-fb7).

**Risk.** Low: only a host that sends BEGIN at a raised rate sees a change, and no shipped host does. **Left alone on purpose:** a rejected relay BEGIN that supersedes a raised local session still leaves the rate raised. The Wizard recovers from that cleanly: it gets a readable NAK at its rate and then ABORTs, which restores the rate. Restoring on the board instead would garble its next DATA.

#### 62. The SBUS signal-loss test never reset the controller: an RTS-only pulse does not reach a native-USB board on Windows

| | |
|---|---|
| **Status** | VERIFIED — all named tests PASS on the current images (20260922-215719); was: FIXED (unverified) |
| **Owner** | `test_is_wrong` |
| **Effort** | S |
| **Tests** | `sbus.signal_loss_controller_reset` |
| **Subsystem** | hil / s21 |

**Evidence (opt-in run 20260922-185238, its first run: every earlier run skipped it as opt-in).** "NaviCore's fps never dropped to 0 after the reset". NaviCore's frame counter ran 179999 -> 181138 over 10.3 s, 110.7 frames/s against a nominal 111.1, with ageMs 0-7 throughout. The SBUS port never dropped, and the ping after the test got an immediate pong with no boot banner. The controller did not reboot.

**Mechanism.** The test wrote DTR=0 and then toggled RTS. On the ESP32-S3's USB-Serial/JTAG port, RTS=1/DTR=0 is the chip reset. But Windows' usbser.sys sends SET_CONTROL_LINE_STATE only on a DTR write, and pyserial's `rts` setter only calls EscapeCommFunction. So both RTS changes were dropped. esptool works around exactly this (`_setRTS` in reset.py). NaviCore is not at fault: its fps is a tumbling one-second frame count, and a real reboot of this controller is about 6 s of silence (its STA WiFi timeout, then the AP fallback).

**Fix.** The test re-writes DTR (still 0) after each RTS change, through a local handle, because the reader may swap `dev._ser` if the USB re-enumerates. It holds the pulse 0.2 s, like esptool. It reads the controller's `bootlog` before and after the pulse and asserts the reset happened (boot count up, or uptime shorter than the time since the pulse) before it judges NaviCore. It does not assert the RTC reason code, which is unverified on this board. HIL_TESTING.md §2 records the trap. Diagnosis adversarially verified; the verifier corrected the first fix, which could crash on a re-enumeration.

#### 63. Soft-serial RX (S3-S5) injects a faux stop bit when a core-1 time slice lands inside EspSoftwareSerial's rxBits()

| | |
|---|---|
| **Status** | VERIFIED — all named tests PASS on the current images (20260922-235913); was: FIXED (unverified) |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `input.soft_rx_under_soft_tx` |
| **Subsystem** | soft-serial RX |

**Evidence (run 20260922-151421).** The A4h arm got 37 of 40 lines exact: 40 lines of 61 bytes at 9600 into W1 S3 from a probe hardware UART, while S4 transmitted about 87 % of the time. One line decoded `2` as 0xF2 followed by a spurious 0x82, and two others each lost one byte. 9600 is well inside the documented reliable range.

**Mechanism.** In `UARTBase::rxBits()` (EspSoftwareSerial 8.1.0, SoftwareSerial.cpp:483), a byte in progress is closed when the ISR edge buffer is empty AND `micros() - m_isrLastTick > detectionTicks`. The emptiness test comes first and the clock read second, and the two are not atomic. If the reader is preempted between them for more than about a bit, edges queue up unseen, but on resume the check sees a stale "empty" and a fresh clock. It then injects a faux stop bit (:486): the byte's remaining bits read as 1s, and the queued real edges play back with a negative delta, so the bytes that follow are framed from mid-byte. A hand trace of 0x32 -> 0xF2 + 0x82 fits exactly. No interrupt-latency model produces both bytes. Several priority-1 tasks share core 1 with serialCommandTask (loopTask, KyberRemoteTask, MeshSerialOutTask), and FreeRTOS time-slices at 1 kHz.

**Fix.** `WcbSoftSerial` overrides `available()`, `read()` and `peek()` to run the library call with the scheduler suspended (`vTaskSuspendAll` / `xTaskResumeAll`). ISRs keep running, so no edge is lost. Recorded in CLAUDE.md rule 13. The root-cause fix belongs upstream: read the clock before testing the buffer. That would also cover preemption by a long ISR and `readBytes()`, which is not wrapped.

**Risk.** The scheduler is held for microseconds per call. Suspend/resume briefly masks interrupts on the core, so edges are delayed, not lost. Re-check `input.soft_rx_baud_sweep` at 57600. Verify with about 20 repeats of the test (800 A4h lines, zero allowed).

#### 64. The level-3 GPIO ISR service was installed with ESP_INTR_FLAG_IRAM although every handler goes through a flash-resident dispatcher

| | |
|---|---|
| **Status** | FIXED (unverified) — self-inflicted this session, never shipped |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | none reproduces it yet (it needs an S3-S5 RX or PWM-input edge during an NVS write) |
| **Subsystem** | interrupts |

**Mechanism.** setup() installed the GPIO ISR service with `ESP_INTR_FLAG_LEVEL3 | ESP_INTR_FLAG_IRAM`, on the belief that every handler is IRAM-resident. `rxBitISR` and `pwmISR*` are (0x4008....). But `attachInterrupt` registers Arduino's `__onPinInterrupt` dispatcher, which is in flash (0x401a68b0 in the map; no `IRAM_ATTR` unless CONFIG_ARDUINO_ISR_IRAM). An IRAM service stays live while the flash cache is off, so an edge during any NVS write would run flash code: "Cache disabled but cached memory region accessed". A board with an RC PWM input takes an edge every 20 ms, so any save could panic it. Found by two independent triage agents; confirmed from the core source and the linker map, not reproduced on the bench.

**Fix.** Level 3 without the IRAM flag. Edges wait out a flash write, as they did before this service existed. The level-3 benefit for soft-serial RX timing stays. The comment and CLAUDE.md rule 13 record why the flag must never come back.

#### 65. NaviCore logs a Maestro dispatch for a write its channel guard skipped

| | |
|---|---|
| **Status** | VERIFIED — all named tests PASS on the current images (20260923-002254); was: FIXED (unverified) |
| **Owner** | `NaviCore` |
| **Effort** | S |
| **Tests** | `navicore.maestro_skip_not_logged_as_dispatch` |
| **Subsystem** | NaviCore / Maestro |

**Evidence.** It failed deterministically in the full run and in both reruns. `[DISPATCH] Maestro 1: channel 32 out of range (0-31) — skipped` was followed 0 ms later by `[DISPATCH] Maestro slot 1 (device 1) <- mesh  cmd 0x04`. Every earlier run skipped this test ("NaviCore has no local Maestro slot that answers ?MAE,GET"). NaviCore's own Maestro started answering between 13:56 and 15:14 on 09-22 (a physical change; the firmware and config were identical), so this is the first time it ran.

**Mechanism and fix.** `maeInboundActuate` called the void wrappers `maestroSetTarget/Speed/Accel`, which return early on `maestroChanOk`, and then printed the dispatch line unconditionally. It now runs the guard first in each channel-bearing case and returns on a skip. The guard prints its one "skipped" line, and nothing reaches Serial2, as before. No wire bytes change and WcbCmd is untouched. The test now requires exactly one of the two lines, so it cannot pass vacuously.

#### 66. A probe soft channel mis-decodes a byte when two probe soft channels receive at once

| | |
|---|---|
| **Status** | VERIFIED — all named tests PASS on the current images (20260922-235913, 20260923-012735); was: FIXED (unverified) |
| **Owner** | `probe firmware` |
| **Effort** | S |
| **Tests** | `input.bcast_fanout` (where it showed); `probe.soft_concurrent_rx` (new: reproduces it at volume) |
| **Subsystem** | hil / wcb_probe |

**Evidence.** Probe1 ch D (a soft channel on header S3 = W1S4, 9600) read `HILFC394` for `HILFC3294`, while ch C (W1S2) received the same line in the same millisecond, intact. An earlier run (20260921-085938 :8453) had ch D read 0x15 as 0x1D in the same situation. Both are late-edge signatures: an edge timestamped 52-104 us late, respectively a dropped frame and a flipped bit. Out of 227 such concurrent C+D pairs across all runs, those are the only 2 mismatches. The WCB side is ruled out: its line is one RMT transaction, and that build's RMT TX was 600/600 exact on a hardware receiver.

**Mechanism and fix.** EspSoftwareSerial decodes from the time each edge's ISR starts, and the probe's GPIO ISR service ran at Arduino's default level 1, beside the UART and RMT interrupts. The probe now installs it at level 3 first thing in setup() (without the IRAM flag; see #64), which is the WCB's own fix, and mutes the gpio log tag for good because the re-install error would land mid-protocol. PROBE_VERSION 2 -> 3 (the protocol is unchanged). The new `probe.soft_concurrent_rx` makes the concurrency happen about 300 times and must be exact on both channels. If it still loses bytes, move W1S2 onto a hardware-only header.

**Correction (2026-09-23).** Level 3 did not cure these mis-decodes. The two recorded bytes are LOST edges, not late ones, which fits ESP32 erratum GPIO-3.14 (see #78); a late edge could not produce them. Level 3 stays, since it is harmless. The letter-swap reasoning in the original self-check was wrong: this kind of loss follows the lagging wire, not the channel letter.

#### 67. input.wdpda_rejects counted another port's normal WDP-DA expiry as a malformed announce being accepted

| | |
|---|---|
| **Status** | VERIFIED — all named tests PASS on the current images (20260922-235913); was: FIXED (unverified) |
| **Owner** | `test_is_wrong` |
| **Effort** | S |
| **Tests** | `input.wdpda_rejects`, `input.wdpda_usb_and_blocked_port` |
| **Subsystem** | hil / s12 |

**Mechanism.** The check was `any(x.startswith("[WDP-DA]") ...)` over the whole window. In the full run, the S2 entry that `wdpda_usb_and_blocked_port` created expired at 90 s (`[WDP-DA] S2: HILDV stopped announcing`) inside the window. None of the five malformed S5 announces was accepted: the firmware rejects all five. It is a race on the poll phase of the new wait loop (this session's change), at about a 40 % chance per full run. **Fix:** scope the check to `[WDP-DA] S5:` (a real accept on an absent port always prints one) and ignore expiry lines in the sibling test's check. `wdpda_rejects` now runs before `wdpda_fields`, so in a full run S5 is empty and the wait costs nothing.

#### 68. wdp.controller_auto_enable rests on one unacknowledged solicit/advert exchange

| | |
|---|---|
| **Status** | VERIFIED — all named tests PASS on the current images (20260922-215719); was: FIXED (unverified) |
| **Owner** | `test_is_wrong` |
| **Effort** | S |
| **Tests** | `wdp.controller_auto_enable` |
| **Subsystem** | hil / s18 |

**Mechanism.** The test forgets NaviCore, turns the controller off and sends `?WDP,POLL`, then gives NaviCore 3 s to answer. The solicit and NaviCore's one solicited advert are each a single broadcast. ESP-NOW broadcasts get no MAC ACK or retry, and neither side repeats. NaviCore's next unsolicited advert can be up to 60 s away. So one lost frame leaves the window empty (full runs 09-21 and 09-22). The firmware state was correct both times. **Fix:** up to three 3 s waits, re-polling after each miss. Re-polling cannot mask a regression, because WCB20 stays forgotten and the controller stays off until an advert is actually decoded. On a final miss the test notes W2's N=20 AGE, which tells which direction lost the frame. Not done: optional firmware hardening, a second solicit about 800 ms after POLL (it would need WDP_DESIGN.md changes).

#### 69. sbus.trim_exact could never run: every trim on this bench is a button on NaviCore's matrix channel

| | |
|---|---|
| **Status** | VERIFIED — all named tests PASS on the current images (20260923-002245); was: FIXED (unverified) |
| **Owner** | `test_is_wrong` |
| **Effort** | M |
| **Tests** | `sbus.trim_exact` |
| **Subsystem** | hil / s21 |

**Mechanism.** The test only accepted a trim on a channel NaviCore leaves unbound. All six controller trims are on CH7 in button mode, which is the X18 layout, and CH7 is NaviCore's matrix channel, so it skipped in every recorded run. Moving a trim needs the controller's `cfg` command, which saves to flash, or NaviCore SET_CONFIG. Both are forbidden. **Fix:** if no trim sits on an unbound channel, the test uses a button-mode matrix trim under a mode where both of its slots are unmapped. From mode 1 today that is T6 in mode 2 (slots 16/15). It switches the mode with a mesh SET_MODE, exactly as `sbus.mode_sets_trigger_mode` does. It asserts the exact values, and that the only rc_trig lines are the two intended taps, then restores the mode and asserts it. The step-mode half remains unreachable on this bench without a flash write, and the description says so.

#### 70. The relay DATA frame that tears an OTA session down is ACKed OK+0; only the frames after it get ERR

| | |
|---|---|
| **Status** | VERIFIED — all named tests PASS on the current images (20260922-215719); was: FIXED (unverified) |
| **Owner** | `WCB_firmware` (+ `NaviCore` twin) |
| **Effort** | S |
| **Tests** | `ota.relay_teardown_frame_err` |
| **Subsystem** | ota |

**Evidence (rerun 20260922-193306; the first run ever, since it was opt-in until today).** W2 printed "write overruns image (0 + 192 > 100) — aborting", and that same frame was ACKed `[OTA:ACK,2,22388,0,0]` (status OK). Only the resend got `...,0,1`.

**Mechanism and fix.** `handleOtaDataPacket` captured `inSession` before `otaWrite`, and reused it for the ACK status. `otaWrite` itself tears the session down on an image overrun or an `esp_ota_write` error, so the tearing frame was ACKed OK with offset 0, which a host cannot tell from a stale duplicate. The 2026-08-06 fix only covered frames after the teardown. The status is now read after the write. Dup and gap frames still ACK OK with the cursor, and the keep-alive still uses the pre-write flag. `navicore_ota.h` had the identical pattern and got the same one-expression fix. Hosts recovered one round trip late before (the Wizard resent, NaviCore's tool treated OK+0 as stale), and now fail at once. No wire-format change.

#### 71. inv.seqget_largest fetched once, but the SEQVAL reply is sent once and unacknowledged

| | |
|---|---|
| **Status** | VERIFIED — all named tests PASS on the current images (20260923-002254); was: FIXED (unverified) |
| **Owner** | `test_is_wrong` |
| **Effort** | S |
| **Tests** | `inv.seqget_largest`, `inv.seqget_multichunk` |
| **Subsystem** | hil / s17 |

**Evidence.** W1 walked the ladder correctly: 2950 was refused on heap (largest block 1012 B), 2400-1400 were refused by NVS, and 1200 was stored and read back exactly. W2's relayed `?MGMT,SEQGET` then printed nothing. The reply is ceil(1209/182) = 7 raw broadcast frames sent once, with no ACK, no second pass and no resend (`sendResultFrags`). W2 prints only when every frame is in. The command did reach W2 (no ETM failure line), and neither size-refusal line appeared. Earlier runs show the same thing on 5-6 frame bursts: about 1 lost in 4 on this bench.

**Fix.** The test retries the fetch like a real requester: up to 4 tries, 3 s apart to clear the 1.5 s dedup, each miss noted. On a final miss, W1's MGMT debug lines tell a lost request from lost frames. `inv.seqget_multichunk` now tells a lost reply (re-fetch) from a lost push (re-push). SEQUENCE_INVENTORY.md §3a records the trap. **Not done (optional, cross-repo):** a second pass in `sendResultFrags`, which would need a `lastDeliveredSession` guard in WCB_Client too.

#### 72. A critical-battery hibernate mid-run was scored as a firmware failure

| | |
|---|---|
| **Status** | VERIFIED — all named tests PASS on the current images (20260922-215719); was: FIXED (unverified) |
| **Owner** | `harness` / bench |
| **Effort** | S |
| **Tests** | `ota.relay_full_same_image_wcb2` (where it showed) |
| **Subsystem** | hil/runner.py |

**Evidence.** The relayed full OTA of W2 ran cleanly to offset 226176 (16.6 %). Then Windows logged Kernel-Power 524 "Critical Battery Trigger Met" at 19:58:43 and "entering sleep" at 19:58:47; the laptop's critical-battery action is Hibernate. It resumed at 20:08:16 when AC came back. W2's 30 s idle reaper ended the session as designed, and the next frame got the designed "no session" answer, ERR + offset 0. The laptop had been on battery since the 15:59:03 power-source change, the same event behind run 20260922-151421's sleep. After the resume only W1's CH9102 port errored, so `host_usb_loss()` (all ports within 2 s) did not fire, and the result was scored FAIL.

**Fix.** The runner compares each test's monotonic duration with Windows' sleep-excluding interrupt time (`_awake_s()`, QueryUnbiasedInterruptTime). A FAIL or ERROR during which the host slept more than 5 s becomes ERROR "NOT A RESULT", followed by the usual wait for the ports. A run that starts on battery logs a WARNING. **Bench:** keep the laptop on AC. The END/reboot half of this test has still never run on hardware.

#### 73. Kyber_Local follow-ups from the #14 / #28 review (D4-D9 applied)

| | |
|---|---|
| **Status** | OPEN — D4-D9 VERIFIED on hardware: all five new kyber tests failed on the 09-22 image (baseline 20260923-094621) and pass on the 09:49 image with the rest of `kyber.*`, `maestro.*`, `pwm.*` and the device guards (20260923-095057; `kyber.local_port_move_releases_old` once probe 5 fixed #79, 20260923-111854). The two `wizard.kyber_*` tests run in the next full run. D4b, D6b, D7b open. Pre-existing or consistency items; none blocks a test |
| **Owner** | `WCB_firmware` (+ Wizard) |
| **Effort** | M |
| **Tests** | `kyber.local_s1_legacy_fallback_skips_kyber_port` (D6, D8), `kyber.local_clear_maestro_drops_target` (D7, plus D6's free-S1 control), `kyber.local_mode_s2` (D8 needle), `kyber.local_port_move_releases_old` (D5, D9), `wizard.kyber_release_order` (D5, D9 push order), `kyber.local_free_port_takes_pwm` (D4), `wizard.kyber_local_frees_other_port` (D4 claims and REMOTE -> local push order); `kyber.local_rejected_port_keeps_forwarding` now compares only the `?KYBER,LOCAL` line |
| **Subsystem** | maestro-kyber |

From the adversarially verified review of the #14/#28 change (workflow wf_3dab4584-264):
- **D4. FIXED (unverified).** The HCR/MP3/WLED/DFP guards, `canUsePWMOnPort` and the Kyber_Local boot UART init reserved BOTH S1 and S2, although since #14 the other hardware port is a working command port. It was not even consistent: devices load before the Kyber settings and were kept, a PWM output saved on the other port was dropped from NVS at the next boot (`loadPWMOutputPortsFromPreferences`), and `;P` (already per port) still drove that pin. **Fix:** one helper, `kyberModeReservesPort()` (WCB_Storage.cpp): Kyber LOCAL reserves only `kyberLocalPort`, Maestro REMOTE still S1. The four device guards, `canUsePWMOnPort` (so `?MAP,PWM`, both boot loaders and WDP PWMTARGET auto-config) and the `;P` guard all call it; every refusal message is unchanged except PWM on a local Kyber's port, which now says "reserved for Kyber" on S1 too. The Kyber_Local boot branch always begins the Kyber port and begins the other one unless a PWM input/output owns it, like the no-Kyber branch (beginning it anyway re-attached the UART over the PWM pin). Bundled, because D4 lets a device sit on the freed port: in targeted mode `forwardDataFromKyber` skips a local target on a port an HCR/MP3/DFP/WLED or PWM output owns - a target can still lack a slot after D7 (a table saved by older firmware, or an explicit `?KYBER,LOCAL` target list with all nine slots full, which adds the target and no slot). Wizard: evaluatePortClaims claims only the Kyber's port (a bare/legacy `?KYBER,LOCAL` = S2) and keeps `kyber-reserved` for REMOTE's S1 alone; a delta push from REMOTE to local now sends `KYBER,CLEAR` early (the verdict's A), because REMOTE's S1 reservation otherwise refused a device the Wizard now offers on S1 while the baseline recorded it. `?backup` needed nothing: since D5 a Kyber_Local board's backup emits `KYBER,CLEAR` ahead of the device lines, which also ends Maestro REMOTE on the board being restored. Help, WCB_KYBER_PASSTHROUGH_PARAMS.md §7, WDP_DESIGN.md §9 and the wiki updated. ESP32 compiles at 69%.
- **D4b (open, accepted).** (1) The HCR/MP3/DFP/WLED guards never check a local Maestro (on every board). The old S1+S2 reservation hid that on the usual Kyber layout (Kyber S2, Maestro S1); now a typed `?HCR,PORT,S1` there is accepted and re-bauds the Maestro's port. The Wizard hides the port behind its `maestro` claim, so it is a CLI-only trap. Adding the term needs care: the Wizard pushes devices before the Maestro block, so a delta push that moves a device onto a port a Maestro is leaving would then be refused. (2) A PWM mapping saved on the other port before the board went Kyber local was skipped at every boot but never removed from NVS (`loadPWMMappingsFromPreferences` only skips); it loads again at the first boot of this firmware - visible in `?MAP,PWM,LIST` and the boot banner, removable with `?MAP,PWM,CLEAR,S<n>`. (3) Switching a board that has a device or PWM on S1 to `?MAESTRO,REMOTE` makes S1 reserved while occupied (same root as D6b).
- **D5. FIXED (unverified).** Moving the Kyber from one port to the other never released the old port: it stayed at 115200 with broadcasts off both ways; only `?KYBER,CLEAR` gave a port back. The bare `?KYBER,LOCAL` hard-coded S2, so with the Kyber on S1 it re-pointed the bridge at S2 (refused outright with a Maestro there) and left S1 claimed. **Fix:** one release helper, `kyberReleasePort()` (WCB_Storage.cpp), holds CLEAR's old code (9600, `?BCAST` in/out on) and skips a port a local Maestro or another subsystem owns at release time. A `?KYBER,LOCAL` move re-points `kyberLocalPort` first (a running KyberLocalTask follows on its next pass, so an S1<->S2 move on a Kyber_Local-booted board is live) and releases the old port after the target loop, so a swap list that puts a Maestro there leaves it alone. The bare form keeps the current S1/S2 port (S2 when not Kyber local yet). Because a release now follows every way out of LOCAL, `?backup` emits `KYBER,CLEAR` / `MAESTRO,REMOTE` ahead of the `BAUD`/`BCAST` lines (KYBER,LOCAL still claims late), and the Wizard sends the leave-LOCAL release there too and re-sends the released port's rows - otherwise the release undid the chain's own settings for that port.
- **D6. FIXED (unverified).** The legacy Maestro fallbacks (no slot for the own id: `;M<own id>`, `;M9`, the `;M0` extra frame, a get on the own id) wrote Serial1 even when S1 was the Kyber's or another device's port, and the get read its "reply" out of that device. **Fix:** `legacyS1Owner()` (WCB_Maestro.cpp) gates all seven sites; an owned S1 (Kyber port, HCR/MP3/DFP/WLED, PWM in/out) gets nothing, the own-id and get paths still return without an ESP-NOW self-forward, and `?MAESTRO,CLEAR,ALL` no longer claims legacy routing on an owned S1. The bench exercises only the Kyber-on-S1 owner; the HCR/MP3/DFP/WLED/PWM owners are covered by code review.
- **D6b (open).** WCB.ino:4813 (`espNowReceiveCallback`), the Maestro_Remote raw fallback to Serial1 (target-98 data with no local Maestro slot, on the WiFi task), has the same shape. Reachable when a device was put on S1 before `?MAESTRO,REMOTE`; the cleaner fix is for `?MAESTRO,REMOTE` to refuse an owned S1.
- **D7. FIXED (unverified).** A local Kyber target outlived its Maestro slot after `?MAESTRO,CLEAR`: forwardDataFromKyber kept writing the freed port (now at 9600), and `?backup`'s KYBER,LOCAL target list re-created the cleared slot on restore. **Decision:** no broadcast skip keyed on kyberTargets - it would override the freed port's `?BCAST` flags the way #14 did, and would stop neither the Kyber writes nor the restore. **Fix:** on a Kyber_Local board in targeted mode local targets follow local slots. `_clearMaestroSlot` / `clearAllMaestroConfigs` drop the (id, self, port) target, or every local target on the port once no slot is left there; a newly claimed `?MAESTRO` local slot adds it (a re-issued existing slot does not, so an explicit `?KYBER,LOCAL` list survives a baud change). Keyed (id, port), never the id alone (rule 5). processBroadcastCommand carries a comment recording the non-fix.
- **D7b (open).** Remote targets are not dropped with their proxy, so `?backup` re-creates a cleared remote slot, and `reconcileKyberTargetsFromMaestroConfigs` (WCB_Storage.cpp:1164) is keyed by id only.
- **D8. FIXED (unverified).** `printBaudRates` hard-wired S1 = "(Maestro)" / S2 = "(Kyber)", and `printKyberSettings` printed "Initialized Serial1 & Serial2" whatever `kyberLocalPort` was. **Fix:** with no user label the row says "(Kyber)" on `kyberLocalPort` and "(Maestro)" on any port with a local slot; `printKyberSettings` prints "Kyber port: Serial<n> (<baud> baud)", or on Maestro_Remote "Maestro data from the mesh goes to: S<n>[, ...]" (else "S1 (no local Maestro configured - legacy default)"). The stale boot comment (WCB.ino Serial1/Serial2 begin) is corrected. Only `kyber.local_mode_s2`'s needle changed.
- **D9. FIXED (unverified).** The REMOTE branch of `storeKyberSettings` did not zero `kyberLocalPort`, which KyberLocalTask gates on alone, so `?MAESTRO,REMOTE` on a Kyber_Local-booted board kept the task bridging the old Kyber port (S2 then had two readers with the Maestro_Remote parser branch) and echoing the local Maestro ports into it until reboot; the port also kept 115200 / broadcasts off. **Fix:** REMOTE releases the port through `kyberReleasePort()` and zeroes `kyberLocalPort`, so `kyberLocalPort != 0` only while Kyber_Local and `maestroPortOwnedByKyberBridge` still mirrors the task gates. The Wizard now asks for a reboot on `MAESTRO,REMOTE` (commandStringNeedsReboot), and sends it only on a real mode change or a full push.

#### 74. kyber.remote_roundtrip asserted exact delivery on a best-effort channel

| | |
|---|---|
| **Status** | VERIFIED — PASS in 20260923-002254 (after sbus.trim_exact armed NaviCore's ch 0 release, for #76) |
| **Owner** | `test_is_wrong` |
| **Effort** | S |
| **Tests** | `kyber.remote_roundtrip` (and optionally a loss-rate floor in `kyber.maestro_s2_single_reader`) |
| **Subsystem** | hil / s22 |

**Evidence (run 20260922-211114).** 64 bytes into W1 S1 arrived on W2's Maestro line as 28 bytes, a >4 ms gap, then 30 bytes: exactly one ~6-byte frame (1d..22) of ~11 was missing. W1 counted 12 raw sends accepted and 0 failed, but for a broadcast "Success" only means the driver queued the frame. The same run's `kyber.maestro_s2_single_reader` delivered 733 of 740 bytes, with the losses scattered single bytes in different lines. Across three runs that is 21 of ~2,081 frames, about 1 %, on the older build as well. At 1 % per frame an 11-frame burst loses a piece about 10 % of the time, which matches this test's record today (5 passes, 1 fail). W2's receive path has nowhere to drop a good frame, and none of today's firmware changes is on this path. The Kyber doc has always said the channel is best-effort (§2).

**Fix (to apply).** Up to 3 attempts, each with a fresh Watch about 0.5 s apart; stop at the first exact delivery. A lossy attempt must still be an in-order subsequence of the payload (the bytes are all distinct, so a duplicate, reordered or foreign byte, which is what a firmware bug looks like, fails at once), and each lossy attempt is noted. Fail if that check fails or if all 3 attempts lose bytes (~0.1 %). Keep the Failed == 0 and getErrors checks, and update the docstring's stale line refs. **To decide where the frame dies:** a per-sender receive counter for target-98 frames on W2, compared with W1's accepted sends.

#### 75. Heap hardening: a command copied on an exhausted heap, a by-value copy of every command, and a line buffer that kept a long line's heap

| | |
|---|---|
| **Status** | VERIFIED — flashed 23:58; regression run 20260922-235913: 172 tests on the changed paths (input/map/var/if/seq/inv/maestro), the only failures unrelated (#77, probe instrument) |
| **Owner** | `WCB_firmware` |
| **Effort** | S |
| **Tests** | `inv.seqget_largest` (drives W1 to the OOM refusal), `seq.*`, `if.*`, `maestro.*` for regressions |
| **Subsystem** | command dispatch / heap |

**Evidence.** W1's ?STATS read "min free since boot 280" (byte-addressable heap). The low point came from `inv.seqget_largest`'s deliberate 2,966-character ?SEQ,SAVE: about 7 full-length copies were live when the 8th allocation failed and was reported cleanly (#58). Nothing crashed, and all 307 marker lines echoed. "Min free since boot" is the sum of each heap region's own low point (esp_heap_caps.h), so 280 is a worst-case bound, not one moment's free heap.

**Latent paths found and fixed.** (1) A queue item copied into a String while the heap was exhausted came back empty with a NULL `c_str()`. It then fell through to processBroadcastCommand and `sendESPNowMessage`, which reads `message[0]`. The drain now drops it with "Out of memory: dropped a N-character command", and `sendESPNowMessage` returns on a NULL or empty message. (2) `handleSingleCommand` took the command by value, one more full-length copy of every command; it now takes a const reference. (3) `processIncomingSerial`'s per-port line buffer kept its capacity through `= ""`, so one long line (a Wizard push chain, a ?SEQ,SAVE, a pasted backup) pinned that much heap for the rest of the boot. After any line over 512 characters it is now released (`= (const char*)nullptr`, which frees it). **Deliberately not done:** a line-length cap. A pasted `?backup` restore line can legitimately exceed 3 KB, so any cap needs a measured maximum first. Also not done: cutting the dispatcher's copies of `args`/`argsUpper`/`seqArgs`, which touches every command path and needs a full run. SEQUENCE_INVENTORY.md §3b records the ~7x cost and what "min free" means.

#### 76. Two NaviCore tests assumed NaviCore sends nothing on its own and that a Maestro channel holds a mesh setTarget

| | |
|---|---|
| **Status** | VERIFIED — PASS in 20260923-002254 (after sbus.trim_exact armed NaviCore's ch 0 release, for #76) |
| **Owner** | `test_is_wrong` |
| **Effort** | S |
| **Tests** | `navicore.maestro_mesh_settarget_readback`, `navicore.mesh_stats_counts` |
| **Subsystem** | hil / s21 |

**Evidence (full run 20260922-215719: 401 PASS / 2 FAIL).** (1) The readback test's mesh setTarget of slot 1 ch 0 was dispatched once. NaviCore printed "[RELEASE] Maestro 1 ch 0 idle 1500ms → servo off" 1.495 s later, and every readback returned 0 promptly. (2) W1's row read sent 5 where the test expected 4. The fifth message was NaviCore's `?STATS,RPT,20,...` mesh-stats report, which it sends to its configured target (W1) every 30 s.

**Mechanism.** (1) Knob J2 is a mode-aware Maestro passthrough on slot 1 ch 0 with releaseIdleMs 1500. A global mode change re-dispatches it, and that arms the release for that channel in RAM until reboot or SET_CONFIG. From then on, any move of ch 0, mesh ones included, restarts the 1.5 s idle timer. The previous run's `sbus.trim_exact` SET_MODE armed it, and NaviCore was not rebooted between runs. The same test passed earlier in that boot, before the mode flip. (2) The ~7 s window catches the 30 s report about a fifth of the time. NaviCore's 60 s `;V,MODE` report to W2 is the same kind of hazard, and it explains the earlier "WCB2 row [2, 2, 2, ...]" failures (09-22 12:24, 12:45). Neither is a firmware change: the NaviCore changes tonight (#65, #70) are not on either path.

**Fix.** (1) The readback test uses a channel no Maestro-passthrough knob output drives on that slot (`_undriven_channel`; on this bench slot 1 ch 1). (2) The stats test reads W1's ?STATS "Reported by Other Nodes" age for WCB20 and starts its window right after a report. The modeReport target, whose phase can't be read, may show exactly one extra, ACKed send. HIL_TESTING.md §6 records both NaviCore behaviours, and the `sbus.trim_exact` docstring no longer calls SET_MODE side-effect free.

#### 77. inv.mgmt_seq_remote fetched W2's inventory once, but the SEQ reply is sent once and unacknowledged

| | |
|---|---|
| **Status** | VERIFIED — PASS in 20260923-002254 (after sbus.trim_exact armed NaviCore's ch 0 release, for #76) |
| **Owner** | `test_is_wrong` |
| **Effort** | S |
| **Tests** | `inv.mgmt_seq_remote`, `inv.hash_matches_wdp`, `inv.mgmt_seq_w2_relay` |
| **Subsystem** | hil / s17 |

**Evidence (regression run 20260922-235913 on the 23:58 hardening image).** W1 sent `?MGMT,SEQ,2` at 970.462 and printed nothing in 3 s: no reply, no error. The test passed in the full run just before and in every earlier run, and nothing in #75 is on this path: the request and reply use raw packets, not `sendESPNowMessage`, and no out-of-memory line appeared.

**Mechanism and fix.** The SEQ request is sent 3 times, but W2's reply goes out once as unacknowledged broadcast frags through `sendResultFrags` (WCB.ino `handleSeqReqPacket`). That is the same pattern as the SEQVAL reply in #71, so a lost frag means silence, and SEQUENCE_INVENTORY.md §3a already says the requester must retry. The shared `_relay` helper now tries up to 3 times, spaced past W2's 1.5 s duplicate filter, and `inv.mgmt_seq_w2_relay` retries the same way.

#### 78. Soft-serial RX loses edges when two soft pins receive at once (ESP32 erratum GPIO-3.14) - probe and WCB alike - and one intermittent W1S4 corruption episode

| | |
|---|---|
| **Status** | VERIFIED on W1 - the erratum was CONFIRMED by `softrx.erratum_pairs` on the edge-triggered image (227 of 10125 multi-port lines lost, run 20260923-113109); on the level-triggered image (flashed 14:36) it lost **0 of 10125** (95 % upper bound 0.03 %/line), `softrx.level_irq_stuck_line` and `input.softserial_tx_rmt` pass, and every soft-port, map, HCR/MP3/DFP, WLED and PWM test passes (run 20260923-144022, 139 PASS). The W1S4 episode (2 below) did not recur in a 20-minute `soak.w1s4_wire` (38 blocks exact). The probe gets the same RX in wcb_probe 6 (below). **New failure mode, FIXED (unverified; compiled, not flashed): the stale level arm after a CPU-only reset** (below) - wcb_probe 7, the WCB boot-time clear, and the harness now fails a test when a probe restarts unplanned |
| **Owner** | `WCB_firmware` (vendored EspSoftwareSerial, `WCB_PWM.cpp`) / `probe firmware` / `hil` / bench |
| **Effort** | M (level-triggered soft RX, done) / S (bench inspection of W1S4) |
| **Tests** | `softrx.erratum_pairs` (opt-in; the fix's regression guard), `softrx.level_irq_stuck_line` (new), `input.softserial_tx_rmt` (RX-mode line added), `probe.soft_concurrent_rx` (rewritten as a control arm + a rated two-soft arm), `input.bcast_fanout`, `soak.w1s4_wire` (opt-in) |
| **Subsystem** | soft-serial RX / PWM input / hil / wcb_probe / s23 |

**Two different things, measured apart (2026-09-23).**
1. **Rare lost edges, two soft channels at once.** Earlier runs saw ~0.2 % of lines wrong on the wire that lags (W1S4) when W1's S3 fan-out lands on two probe soft channels together: 0x35 read as 0x37, and 0x34 read as 0xF4 with the next byte's stop edge lost. A decoder replay (EspSoftwareSerial rxBits) shows each is exactly one or two LOST edges. ESP32 erratum GPIO-3.14 (all revisions, no fix scheduled) explains that: pins 0-31 share one interrupt status register, and the W1TC clear of one pin's edge can swallow another pin's edge in the same clock. The probe's soft RX pins are all in that group. A diagnosis and its verifier agreed; the erratum was not reproduced on demand.
2. **An intermittent episode (run 20260923-012123, 01:21).** For a few minutes W1S4 lost ~3.5 % of lines (35 of 1000) EVEN WITH W1S2 on a probe hardware channel, so only one probe pin was taking edge interrupts. The errors were spread evenly over time, mostly 1->0 flips and dropped bytes, and hit W1S2 too when both were soft. That rules out the erratum for that episode. Minutes later every arrangement was clean: W1S4 on a hardware UART 500/500, a repeat of the control 500/500, then the full self-check 1000/1000 and 300/300. The cause is unknown: W1's S4 output, or the W1S4 wire or its ground (W1S4 goes to probe1 header S3, next to W1S2's header S4). It only showed while S2 and S4 transmitted together. W1S5, written alone, stayed clean throughout.

**Done.** The self-check has a control arm, with W1S2 on a hardware channel, that must be exact (1000 lines). It has a two-soft arm that allows the erratum's rare loss up to a 2 % ceiling, but fails a line lost on both wires, or any mangled line that is not a near-miss of a sent one. So it catches the episode above if it comes back. The probe comment (`probe_main.cpp`, setup) now names the erratum.

**For Greg at the bench.** Reseat the W1S4 -> probe1 header S3 lead and its ground, and check it isn't bundled tightly against W1S2's lead. If the self-check's control arm ever fails again, a scope on W1S4 during the S2+S4 fan-out decides W1 against the wire. To run the measurements: flash probe1 with `tests/hil/wcb_probe` (v4, `TXSKEW`), add `softrx_erratum` and `w1s4_soak` to `bench.json` `opt_in` (optionally `soak_minutes`), then `python run.py "softrx.*" "soak.*"`.

**Measured: the WCB is exposed (run 20260923-113109).** `softrx.erratum_pairs` on W1 (HW 1.0, S3-S5 RX on GPIO 25/21/23, 9600 baud, the edge-triggered image) lost **227 of 10125** two- and three-port lines (2.2 %) while **every one of 10125** single-port control lines arrived exact. By the nearest other line's skew (0.5 µs bins) the losses sit at -1.5 to -3 µs (57/84/57 lines at -2.5/-2.0/-1.5, a handful elsewhere): the second pin's edge arrives while the GPIO ISR is still handling the first pin's, which is the erratum's STATUS/W1TC mechanism. Every one-byte corruption whose line could be attributed (55 of 55) is exactly ONE lost edge, a whole bit run taking its neighbours' level, rising (39) and falling (16) alike, at every bit boundary from data bit 0 to data bit 6 (`softrx_pairs.csv`). A PWM input shares the same status register, so it is exposed too.

**Fix (2026-09-23, FIXED unverified: compiled, not flashed).** Espressif's workaround for GPIO-3.14: emulate edges with level interrupts and flip the level after each one.
- **EspSoftwareSerial 8.1.0 is vendored** at `Code/WCB/src/EspSoftwareSerial` (the way HumanCyborgRelationsAPI is, CLAUDE.md rule 7), with the patches listed in its README and the LGPL `LICENSE`. Every include in `Code/WCB` is the relative path; CI (`build.yml`) no longer installs the library, and `build.sh`'s prerequisites say so.
- **Classic ESP32 only** (`ESPSWSERIAL_LEVEL_RX` from `CONFIG_IDF_TARGET_ESP32`): `enableRx()` attaches `rxBitISR` as `ONLOW`/`ONHIGH` for the level the line is not at. `rxBitISR` re-arms the opposite level first (`gpio_ll_set_intr_type`), pushes a transition only when the level differs from the last one recorded (the same level is a spurious entry), then re-reads the pad and loops, capped, which narrows the window in which the pin can be left armed for the level it is at; that window (up to the IDF's post-handler status clear) and the cap exit rely on the level status re-asserting while its condition holds, which it does. The ESP32-S3 compiles the stock edge path and class layout.
- **PWM inputs the same way** (`WCB_PWM.cpp` `pwmEdge()` behind `pwmISR1-5`, armed by `attachPWMInterrupt`); the S3 keeps the stock CHANGE body.
- **Left exposed:** `rxBitSyncISR` (a soft port above ~74880 baud, i.e. 115200) stays on its FALLING edge on both chips: it busy-waits a frame in the ISR, so a level trigger would re-fire forever on a line held low. `?BAUD` already warns that 115200 soft input is unreliable.
- **New constraint (CLAUDE.md rule 13):** the ISR rewrites `GPIO_PINn_REG`'s `int_type` without the IDF spinlock, so S3-S5 RX and PWM-input pins are (re)configured only from core 1. Today `applyLiveBaud`, `attachPWMInterrupt` and `detachPWMInterrupt` all run from `setup()` or command handlers on core 1.
- **Checks:** `grep -rn --include="*.h" --include="*.cpp" --include="*.ino" "<SoftwareSerial.h>" Code/WCB` prints nothing; neither compile lists a stock EspSoftwareSerial under "Used library"; `rxBitISR`, `rxBitSyncISR`, `pwmEdge` and `pwmISR1-5` are at 0x40081xxx (IRAM) in the ESP32 ELF. `micros()` (0x4010ffe8) and `__onPinInterrupt` are in flash, as before, which is why the GPIO ISR service must stay without `ESP_INTR_FLAG_IRAM` (#64). ESP32 69 % / 108136 B RAM, S3 67 %, no new warnings.
- `?DEBUG` prints `[SOFTSERIAL] S<n> RX: level-triggered GPIO interrupt (ESP32 erratum GPIO-3.14)` / `edge-triggered GPIO interrupt` / `off` at every soft-port begin (`applySoftSerialIntTx`); `input.softserial_tx_rmt` asserts the mode against `?HW`. The new `softrx.level_irq_stuck_line` holds W1's S4 RX low for 2 s: W1 must keep answering `?VERSION` within 1 s, must not reboot, and S4 must read text exactly afterwards. A wrong arm would show there as an interrupt storm.
- **Related:** #63's root cause (rxBits reads the clock after testing the buffer) could now be fixed in the vendored copy. It is not done here, and the scheduler-suspend wrapper stays. The sketchbook keeps the stock library.

**The probe (wcb_probe 6).** With W1 fixed, `probe.soft_concurrent_rx` still failed (run 20260923-144022): its control arm was exact (1000/1000 on every wire, so W1 and the wires are clean), but the probe's own two soft channels lost 3 of 300 lines each, and the test's near-miss rule rejected two of them - a lost edge in a CR merges two lines, and one mid-byte slips the frame for the rest of the burst. That is the same erratum in the instrument. wcb_probe 6 bundles a byte-identical copy of the WCB's patched library at `tests/hil/wcb_probe/src/EspSoftwareSerial` (Arduino copies a sketch into its build folder, so a relative include of the WCB's cannot work); `selftest.py` fails if the two copies differ. `probe.soft_concurrent_rx` now requires both arms exact on wcb_probe 6, and on an older probe runs the control arm and skips the two-soft one. Flashed to both probes at 15:38: `probe.soft_concurrent_rx` read 300/300 on every wire in the two-soft arm (it was 297/300) and 1000/1000 in the control arm, and every `probe.*` and soft-channel input test passed (run 20260923-154032, 7/7).

**The stale level arm after a CPU-only reset (full run 20260923-154611; VERIFIED).** On wcb_probe 6, channel C bound on header S3 then MESH LEAVE, three W1 reboots gave 6 probe panics; on wcb_probe 7 the same sequence (5 W1 reboots) and two bound channels (5 more) gave none. W1 on the 19:17 image prints "[BOOT] Cleared a stale GPIO interrupt left armed by the restart on GPIO 21 23 25" (its S3-S5 RX pins) after every `?reboot` - the WCB carried the same state. `client_mesh.rejoin_seq_reuse` and `input.mesh_bcast_to_ports` PASS (20260923-192715). Level-triggered RX added a boot trap. `ESP.restart()` and a panic reset only the CPU; the GPIO peripheral keeps each pin's interrupt TYPE and ENABLE. A soft-RX (or ESP32 PWM-input) pin armed at that moment stays armed through the boot with no handler behind it. Once `gpio_install_isr_service()` routes the GPIO interrupt, the IDF dispatcher finds no callback, nothing flips the level, the status re-asserts at once, and the interrupt watchdog panics - another CPU reset, so it loops until the wire changes level. Arduino's `pinMode` re-arms it on its own: `__pinMode` copies the pin's current `int_type` into `gpio_config` (esp32-hal-gpio.c:134), which enables it.
- **What it did (wcb_probe 6).** probe1's pin 4 was left armed by a `MESH LEAVE` (5480.033, `ESP.restart()` without `resetAll()`) while channel C was bound. Each later W1 reboot (5570.624, 5596.038) let W1's TX line drop and set off three interrupt-WDT panics on probe1 (5571.441-5573.187, 5596.868-5598.558), which stopped only once W1 booted and drove the line again. probe2 did the same after W2's two OTA restarts (7388.183, 8144.171). The panicked PCs decode (ELF SHA 970036c40) into the IDF GPIO dispatcher (`gpio_isr_loop` gpio.c:491, `gpio_intr_service` gpio.c:514/519), not `rxBitISR`. About 40 W1 reboots with only handler-attached soft RX caused none; runs on probe 2, 3 and 5 (edge-triggered) had none. The harness never noticed (`links.py` `bind()` returned early on the cached channel), so `client_mesh.rejoin_seq_reuse` read [0,0,0] and `input.mesh_bcast_to_ports` read `b''` from a channel the probe no longer had - two of the run's four FAILs, both HARNESS.
- **Probe fix (wcb_probe 7).** `setup()` disarms every header pin (`disarmPinIrq`: `gpio_intr_disable` + `gpio_set_intr_type(DISABLE)`) before `gpio_install_isr_service` and before its `pinMode` loop; `MESH LEAVE` calls `resetAll()` before `ESP.restart()`; `releasePin()` disarms too. `UNBIND` was already clean: `end()` -> `enableRx(false)` -> `detachInterrupt`, which removes the handler (disabling the pin) and sets the type to DISABLE (esp32-hal-gpio.c `__detachInterrupt`), so the vendored library needs no change.
- **WCB hardening (not observed on a WCB, same trap).** `setup()` calls `clearStaleGpioInterrupts()` before `gpio_install_isr_service`: every valid GPIO with a type or enable set is disarmed (nothing has attached one yet, so any such pin is stale), and `[BOOT] Cleared a stale GPIO interrupt left armed by the restart on GPIO <n>` is printed. It clears all pins, not this revision's RX pins, because the pin map is not loaded yet and `?HW` moves them. Before this, any software restart (`?reboot`, OTA, a config push's deferred restart, a PWM mapping change) that caught a soft-RX or PWM-input pin armed could boot-loop the board if that line changed level before its handler was attached again - and a PWM input pulses every 20 ms. The runtime paths leave no armed pin without a handler: `applyLiveBaud` ends S3-S5 through `detachInterrupt`, `detachPWMInterrupt` is `detachInterrupt`, `WcbSoftSerial::end()` is the library's `end()`, and `;P` borrows TX pins, which never carry an interrupt.
- **Harness.** A bind records a mark; `bind()`, and any read or send on a bound link, re-binds when the probe has printed `BOOT wcb_probe` or `Guru Meditation` since (substring match: the ROM banner lands glued to the BOOT line), or its port dropped and reopened (`<<reopened`; the probe runs on USB power alone, so a drop resets it and its BOOT line lands while the port is closed). A read whose mark is older than the restart fails with the reason. The re-bind forgets only the bindings older than the restart, each with a best-effort `UNBIND`: a wire bound after it stays bound on the probe, and forgetting it host-side made the next bind pick a letter whose header was still held (`ERR pin in use`, found in review with a pin-ownership simulation). After each test the runner scans every open probe for a restart nothing planned (`MESH LEAVE`, the first-use `RESET` and the resume/outage reopen are planned), fails THAT test with `probe<n> panicked at t=...`, then `RESET`s the probe and forgets all its channels (only the lost ones if the `RESET` fails). `selftest.py` covers all of it with a fake probe that enforces pin ownership.
- **Bench check.** Bind a soft channel on probe1 to a W1 TX wire, `MESH LEAVE` while bound (v6's trigger), then `?reboot` W1 five times: no `Guru Meditation` on probe1. Then `client_mesh.rejoin_seq_reuse` and `input.mesh_bcast_to_ports`.

**Measurement (built 2026-09-23; `softrx.erratum_pairs` run 20260923-113109, `soak.w1s4_wire` not yet run).** Two opt-in tests in `tests/hil/suites/s23_softrx_measure.py`, harness only.
1. `softrx.erratum_pairs` (`softrx_erratum`, ~15 min). The probe's new `TXSKEW` verb (wcb_probe 4) bit-bangs two or three lines at once onto W1's S3-S5 RX pins from an IRAM loop on the cycle counter, the later lines starting a chosen number of nanoseconds after the first. The hardware channels cannot do this: each UART starts a frame on its own baud tick, so one re-bind gives one skew for a whole batch. 4500 rounds rotate the pairs (S3,S4), (S4,S5), (S3,S5), with every 4th a triple, and each is followed by one single-port line per port as the control. Skews are seeded, 60 % within +-15 us, 40 % within half a bit, ~5 % zero. Lines start `***` and W1 only prints "Ignored chain command", with `?BCAST,IN` off on S3-S5, so W1 transmits nothing. Verdict: any single lost -> inconclusive (W1's one-port RX, see 2); >= 6 multi-port lines lost with every single exact -> **confirmed**, which is the go for the fix; none lost with >= 2500 pairs inside +-15 us -> refuted at the stated bound (PASS). After the fix the same test must PASS.
2. `soak.w1s4_wire` (`w1s4_soak`, `soak_minutes`, default 20; probe v3 is enough). It repeats the episode's load (S3 fan-out, so S2 and S4 transmit together; `;S4` alone; `;S2` alone with W1S4/W1S5 checked for induced bytes). It alternates W1S4 between a probe hardware and soft receiver in 30 s blocks, zooming to 10 s after an error, and reads the W1 heap every 5 min. A recurrence is classified as W1's content (same corruption on S2 and S4), W1's S4 waveform or lead (the hardware receiver sees it too, or FRAME errors), sub-bit glitches (soft receiver only), needing S2 activity, or S2 coupling.

#### 79. A NUL on a port's line (a break) made the whole next line an empty broadcast; the probe's own channel re-bind produced one

| | |
|---|---|
| **Status** | VERIFIED - probe 5 + the 11:14 W1/W2 image: `input.nul_ignored` and `kyber.local_port_move_releases_old` PASS in 20260923-111854 (50/50 of `input.*`, the 3 known WDP-DA label skips aside) |
| **Owner** | `WCB_firmware` + `probe firmware` |
| **Effort** | S |
| **Tests** | `input.nul_ignored` (new, should), `kyber.local_port_move_releases_old` |
| **Subsystem** | serial input / wcb_probe |

**Evidence (targeted run 20260923-095057 on the 09:49 images, 139/140).** `kyber.local_port_move_releases_old` step (e): after the live `?KYBER,LOCAL,S2` on a board booted Kyber_Local on S1, `;S0,<marker>` typed into the released S1 printed nothing on USB, and W1 sent a lone CR out of S4. With `?DEBUG` on W1 printed `Processing input from Serial1: ` (empty) and `Broadcasting command: ` (empty). Diagnostics, all on the bench: the Kyber forwarders never read S1 after the move; W1 in plain mode with S1 booted at 9600 read every line (3/3) until probe1 re-bound channel A (9600 -> 19200 -> 9600), after which it read 0/3; a CR sent first cleared it. W2 had also received `00 00 00` bytes during the test's Kyber control check.

**Two causes.**
1. **Probe (harness).** `unbindChannel` ended the UART and left the TX pin a floating input until the next `begin()`. That pulled the WCB's RX line low (a break), which the WCB's UART reads as 0x00. Every re-bind (any baud change) did it, on the hardware channels at least. wcb_probe 5 latches the pin's GPIO output high before `end()` and leaves a released TX pin pulled up. With probe 5 and the unchanged W1 image the same re-binds read 3/3, and the Kyber test passes.
2. **WCB (firmware).** `processIncomingSerial` appended the 0x00 to the port's line buffer. `String` counts it, but every `c_str()` consumer stops at it, so the line was neither empty nor a command: it went to every port and the mesh as an EMPTY broadcast, and the real command was lost. Anything that breaks the line does this in a droid, not just the probe: a device resetting, a cable plugged in, a sender re-configuring its pin. The reader now skips NUL bytes (WCB.ino `processIncomingSerial`), which can never be part of a command. Raw-mapped and device-owned ports never reach this reader, so binary pass-through is unaffected.

**Test.** `input.nul_ignored` sends `<NUL><NUL>;S4<t>` then `<NUL>` + CR into W1 S3: S4 must get exactly `<t>`, and S2/S4/S5 nothing else. On the 09:49 image it failed as predicted (S4 got a lone CR).

#### 80. The ETM offline sweep races the receive callback on boardTable: a peer coming back is logged OFFLINE the moment it arrives and left offline until its next packet

| | |
|---|---|
| **Status** | VERIFIED - flashed 19:17; `etm.offline_detection_timing` 10/10 PASS, and across its 45 OFFLINE events not one second OFFLINE without an ONLINE between (the race's fingerprint; 1 in 5 in the full run before the fix). The two ~9 s gaps measured from the last printed heartbeat were refreshed by unprinted WCB2 packets, not the race. `peers.controller_off_on` PASS (20260923-192715) |
| **Owner** | `WCB_firmware` (`WCB.ino`) |
| **Effort** | S |
| **Tests** | `etm.offline_detection_timing` (unchanged - its assertion is sound and it is the only thing that catches this) |
| **Subsystem** | ETM presence / boardTable |

**Evidence (full run 20260923-154611).** The test sets W1 to HB 4 / MISS 1 (offline after 5 s) while W2 keeps HB 10, so W2 goes offline and comes back every heartbeat, and asserts the median heartbeat-to-OFFLINE gap is at most 6.5 s. Gaps: 4.99 and 5.01 s (correct), then at 5081.112 and 5090.777 `[ETM] WCB2 went OFFLINE`, `WCB2 came ONLINE` and `Heartbeat from WCB2` arrived in the same batch, with no online edge since the previous OFFLINE (gaps 10.44 and 9.66 s). At 5096.709 `came ONLINE` printed with no OFFLINE before it, although the threshold was still 5 s: W2 had been left marked offline with a fresh timestamp. Pre-existing (the code dates from 2026-02-24 / 05-27 / 06-26; today's diff does not touch it): the same same-batch pattern is in 6 of the 14 recorded runs of this test, 8 edges in all (20260915-174757, -194910, -222653 x2, 20260916-122250, 20260922-151421, today x2), and the test fails exactly when 2 of its 4 gaps are hit (20260915-222653 and today). About 1 edge in 7.

**Mechanism.** `espNowReceiveCallback` (WiFi task, core 0) did `wasOffline = !online; online = true; lastSeenMs = millis();` and `processETMHeartbeats` (loop(), core 1) did an unlocked `if (online && millis() - lastSeenMs > thr) online = false`. When an offline peer's packet arrived, the sweep could land between the callback's two stores, read `online == true` with the old `lastSeenMs`, print a false OFFLINE and clear `online` - then the callback stored the fresh time and printed ONLINE. Two rarer forms: a lost update (the sweep clears a just-set `online` with no print), and an unsigned wrap (`millis()` read before a concurrent, later `lastSeenMs` store makes the age ~49 days). Consequence outside the test: for up to one heartbeat interval a live peer is offline, so `etmAddToPendingTable` skips it and ensured sends to it drop to fire-once with no ACK tracking, and `?config` shows it offline. The temporary-peer eviction could also wrap and evict a live temporary peer.

**Fix.** One `portMUX_TYPE boardTableMux` (WCB.ino, beside `boardTable`) and helpers that are the only read-modify-writers of `online` / `lastSeenMs`: `boardMarkSeen` (the callback: edge test and both stores), `boardSweepOffline` (both sweeps: check-and-clear), `boardMarkOffline` (the eviction, `removeActivePeer`, `syncActivePeerRegistrations`), `boardStampSeen` (temporary-peer adoption) and `boardPresence` (a consistent snapshot for the eviction TTL and the `?STATS` / `?config` rows). `millis()` is read before the lock, ages use a signed difference, and every print is outside it. Reordering the two stores alone would leave the lost update and the wrap. Single-field reads (`wcbPeerOnline`, `etmAddToPendingTable`) stay unlocked; an aligned 32-bit load is atomic.

**Bench check.** `python run.py "etm.offline_detection_timing"` about 10 times: every gap 4.95-5.05 s, no `went OFFLINE` in the same batch as `came ONLINE`, and every OFFLINE preceded by its own online edge.

**Also from that run (TEST_BUG, fixed in the test).** `peers.controller_off_on` failed because `?CONTROLLER,ON,20` printed no `registered (live)`: NaviCore's 30 s `?STATS,RPT` landed inside the 0.64 s OFF window, W1 ACKed it, and `etmSendAck` re-added WCB20 as an ESP-NOW peer on demand, so ON found the peer present. The firmware ended correct (NaviCore answered `;W20,?version`). The test now runs with `?DEBUG,ETM,ON` and excuses the missing line only when an `[ETM] Sent ACK seq N to WCB20` falls between OFF's confirmation and ON; otherwise it still fails, since that line is its only check that OFF frees the slot. The `?CONTROLLER` comment in WCB.ino that said a disabled controller is "ignored" now says what happens.


#### 81. The HCR, MP3 and WLED guards refused every soft-serial rate above 9600 while ?BAUD allowed 57600

| | |
|---|---|
| **Status** | VERIFIED - flashed to W1/W2 2026-09-24 (`6.2.1_232316RSEP2026` + the fix, from `tests/hil/results/builds/wcb-esp32-meshq`); `devices.soft_ports_fast_baud`, `wled.soft_port_57600`, `hcr.config_rejects`, `mp3.config_rejects`, `wled.config_rejects` all PASS (run 20260924-092423) |
| **Owner** | `WCB_firmware` (`WCB_HCR.cpp`, `WCB_MP3.cpp`, `WCB_WLED.cpp`, `WCB_Help.cpp`) |
| **Effort** | S |
| **Tests** | `devices.soft_ports_fast_baud`, `wled.soft_port_57600`, `hcr.config_rejects`, `mp3.config_rejects`, `wled.config_rejects` |
| **Subsystem** | devices / soft serial |

**Evidence.** `docs/HIL_TEST_AUDIT.md` F1: `?BAUD,S3-S5` accepts up to 57600 (received exactly through 57600, 0/20 lines at 115200 - CLAUDE.md rule 13), but `?HCR,PORT,S4:19200`, `?MP3,S3:38400` and a WLED above 9600 on a soft port were refused as "unreliable above 9600", a limit left over from the bit-banged TX. An MP3 Trigger at its usual 38400 could not live on S3-S5.

**Fix.** The HCR and WLED guards refuse above 57600 (115200 stays refused); the MP3 block is gone, since 9600 and 38400 are its only rates. The help text, the wiki (HCR-Vocalizer, MP3-Trigger, Command-Reference, Configuration-Guide) and the Wizard's HCR and WLED baud pickers (capped at 57600 on S3-S5, as the MP3 one already was) follow.

#### 82. The help advertised the legacy PWM map as ?PMSSx,dest; the handler only takes ?PMSx,dest

| | |
|---|---|
| **Status** | VERIFIED - `pwm.legacy_forms` PASS on the flashed boards (run 20260924-092423), including its check that the `?MAP?` help page shows `?PMSx,dest` |
| **Owner** | `WCB_firmware` (`WCB_Help.cpp`) |
| **Effort** | XS |
| **Tests** | `pwm.legacy_forms` |
| **Subsystem** | help / legacy PWM |

**Evidence.** `docs/HIL_TEST_AUDIT.md` F2: `?PMSS3,S4` reads its port from `S3` as 0 and prints `Input port must be 1-5`; `?PMS3,S4` is the string `?MAP,PWM,S3,S4` builds itself (WCB.ino:6037).

#### 83. ?HW wrote NVS only, so ?backup reported the running hardware version until the next boot

| | |
|---|---|
| **Status** | VERIFIED - `ident.hw_setter` PASS on the flashed boards (run 20260924-092423): the chain reports a saved `?HW` at once |
| **Owner** | `WCB_firmware` (`WCB_Storage.cpp`) |
| **Effort** | XS |
| **Tests** | `ident.hw_setter` |
| **Subsystem** | identity / storage |

**Evidence.** `docs/HIL_TEST_AUDIT.md` F3 (bench `20260924-005924`): after `?HW,32` the chain still said `?HW,1`, so a Wizard pull right after a push showed the old version and its diff pushed it again.

**Fix.** The running value follows the saved one. The pin map is still applied at boot only (`updatePinMap`), and only the status-LED branches read the value live.

#### 84. On a full NVS, ?SEQ,CLEAR leaves a ghost empty sequence, and ?SEQ,SAVE can hide one

| | |
|---|---|
| **Status** | VERIFIED - both `key_list` writes checked (`WCB_Storage.cpp`); `seq.nvs_full_consistency` PASS (20260924-120958) |
| **Owner** | `WCB_firmware` (`WCB_Storage.cpp`) |
| **Effort** | S |
| **Tests** | `seq.nvs_full_consistency` (opt-in `nvs_fill`); `seq.cycle_guard_reuse` saw it first |
| **Subsystem** | stored sequences / NVS |

**Evidence (run 20260924-092602).** With W1's NVS full, `?SEQ,CLEAR,HILL1` printed "Deleted stored command key: 'HILL1'",
a second clear printed "No stored value found ... removed from list if present", and `?backup` still listed
`?SEQ,SAVE,HILL1,` with an empty value; config_guard's own clear then removed it.

**Mechanism.** `eraseStoredCommandByName` removes the value key, then rewrites `key_list` with `putString` and never
checks the result (`WCB_Storage.cpp:804`); the save does the same after writing the value (`:761-762`). A failed list
write on clear leaves the name listed with no value; on save it leaves a value no list names, which nothing backs up,
recalls by inventory or clears.

**Fix.** Both `key_list` writes are checked. A failed save removes the value it just stored and prints "Not stored";
a clear removes the value first (on a full store that frees the room the rewrite needs) and, if the list still cannot
be written, says the key is still listed instead of "Deleted".

#### 85. ?TRACK set a flag nothing read; removed with its legacy spellings

| | |
|---|---|
| **Status** | VERIFIED - misc.track_removed PASS (20260924-120036) |
| **Owner** | `WCB_firmware` (`WCB.ino`) |
| **Effort** | S |
| **Tests** | `misc.track_removed` |
| **Subsystem** | commands |

**Evidence.** `?TRACK,ON/OFF/STATUS` and `?TRACK_ALL_ON/_OFF/_STATUS` set or printed `trackCommandDelivery`, which no other code read; no help page, tool or wiki page listed them (`docs/HIL_TEST_AUDIT.md` F5).

**Fix.** The command, its legacy spellings, the flag and `printTrackingStatus` are gone; each spelling now answers "Unknown command".

#### 86. A negative ;T delay waited 30 minutes; the cap was silent without ?DEBUG

| | |
|---|---|
| **Status** | VERIFIED - serial.timer_cap_negative PASS (20260924-120036) |
| **Owner** | `WCB_firmware` (`command_timer.cpp`) |
| **Effort** | S |
| **Tests** | `serial.timer_cap_negative` |
| **Subsystem** | command timer |

**Evidence.** `parseCommandGroups` stored `String::toInt()` in an `unsigned long`, so `;T-500` became the 1800000 ms cap and held the rest of the chain for half an hour (F6).

**Fix.** A delay starting with '-' refuses the whole chain ("Timer refused: ... Nothing in this chain was run."); the cap line now prints without `?DEBUG`.

#### 87. ?LABEL with a port outside 1-5 was ignored with no reply

| | |
|---|---|
| **Status** | VERIFIED - persist.baud_label_validation, persist.legacy_label_baud PASS (20260924-120036) |
| **Owner** | `WCB_firmware` (`WCB.ino`) |
| **Effort** | S |
| **Tests** | `persist.baud_label_validation` |
| **Subsystem** | serial labels |

**Evidence.** `?LABEL,S9,x`, `?LABEL,CLEAR,S9` and the legacy `?SLCS9` reached setters that return silently on a bad port (F7).

**Fix.** The `?LABEL` handler prints `Invalid serial port <n> (must be 1-5)` like `?BAUD`; the legacy branch calls `clearSerialLabelCommand`, which validates.

#### 88. ?ERASE,NVS kept the WiFi settings and the saved serial devices

| | |
|---|---|
| **Status** | VERIFIED - nvs.erase_defaults_restore, nvs.wcb_erase_alias PASS (20260924-120036) |
| **Owner** | `WCB_firmware` (`WCB_Storage.cpp`, `WCB_Help.cpp`) |
| **Effort** | S |
| **Tests** | `nvs.erase_defaults_restore, nvs.wcb_erase_alias` |
| **Subsystem** | NVS |

**Evidence.** The erase cleared a fixed list of namespaces that missed `wifi_cfg` and `wdp_da`, although its help said it erases all settings; the Wizard's Factory Reset blanks the whole partition (F8).

**Fix.** `eraseAllNvsNamespaces` clears every namespace the iterator finds, older firmware's and the radio's included, and the erase prints how many.

#### 89. Nothing reported how full NVS is

| | |
|---|---|
| **Status** | VERIFIED - wcb.nvs_report PASS (20260924-120036); that run's report shows W1/W2 at start and end |
| **Owner** | `WCB_firmware` (`WCB_Storage.cpp`, `WCB.ino`) + harness (`tests/hil/hil/nvs.py`) |
| **Effort** | S |
| **Tests** | `wcb.nvs_report` |
| **Subsystem** | NVS |

**Evidence.** Run 20260924-092602 failed three tests on a full NVS on W1 that nothing could show (F10).

**Fix.** `?NVS` prints the usage line and one line per namespace; `hil/nvs.py` records it for each USB board at the start and end of every run of 5+ tests (session.log, report.md, `results/nvs_history.csv`).

#### 90. ?backup prints only a checksum where a one-line chain belongs when the heap cannot copy the chain

| | |
|---|---|
| **Status** | VERIFIED - wcb.backup_large_config, wcb.pull_size_limit, seq.nvs_full_consistency PASS (20260924-133332) |
| **Owner** | `WCB_firmware` (`WCB.ino` `printBackupConfig`, `buildConfigString` - since replaced by `configPullWalk`, #91) |
| **Effort** | M |
| **Tests** | `wcb.backup_large_config`, `wcb.pull_size_limit`, `seq.nvs_full_consistency` |
| **Subsystem** | backup / heap |

**Evidence (20260924-120036, then measured).** With W1's store filled, `?backup` printed `^?CHK<crc>` alone under both chain headers. Measured on W1 with WiFi in AP mode (about 19 KB heap free, largest block 16-17 KB), adding 500-character sequences to its 899-character chain: the factory-reset chain printed empty at 2.5 KB and as the checksum alone at 3.5 and 4 KB; at 4.5 KB the configured chain did the same; the heap's minimum since boot fell to 2.5 KB. `printBackupConfig` builds both chains as Strings and copies each to append the checksum (`WCB.ino:6789-6790`); a String whose allocation fails comes back empty (#58). The test only notes it.

**Fix.** `printBackupConfig` runs `collectConfigCommands` once per output and streams each chain, CRC-32 running over the bytes printed (`crc32Update`), so nothing grows with the config; the sub-emitters became token producers (`emit*Backup`). Output goes through a 2 KB write buffer flushed per line: with 256-byte writes, a peer's `[ETM] WCBn came ONLINE` (printed on the WiFi task) landed inside a chain after a reboot and broke its checksum (`persist.bcast_flags_reboot`, 20260924-131417). `buildConfigString` (the mesh pull) is measured first (`configStringLength`), reserved once and filled in place; out of memory, it returns "" and the target sends an empty reply, which the Wizard reports as an error, never a partial config. A pull over 2912 characters is refused before it is built, and both refusals print on the target, ungated (F13 has since replaced this pull with the config-pull job, `configPullWalk`, #91). Output byte-identical to before on W1 and W2.

#### 91. A config pull over 2912 characters gave the requester nothing

| | |
|---|---|
| **Status** | VERIFIED - wcb.pull_size_limit, wcb.pull_plain_over_limit, wcb.pull_parts_many, wcb.pull_error_oom_legacy, wcb.pull_error_oom_parts, wcb.pull_nonblocking, navicore.mgmt_pull, navicore.pull_over_limit, wizard.remote_pull, wizard.remote_pull_parts and the ten wizard.remote_pull_fake_* PASS (20260924-190431) |
| **Owner** | `WCB_firmware` + Wizard + harness + WCBClient |
| **Effort** | L |
| **Tests** | the ids above; host `tests/config_parts_test.cpp`; `tests/wizard/unit/pull.test.js` |
| **Subsystem** | MGMT relay / config pull |

**Evidence.** One pull reply carries at most 16 frags of 182 bytes. A larger config was refused on the target with a
debug-only (then, after #90, an ungated but local) line, so the requester only saw its pull time out. An error text
in the reply was not an option: every released Wizard stores any non-empty `[MGMT:CONFIG,<n>]` body as the board's
config and baseline without checking its CRC (audit F13).

**Fix.** A `,P` pull (type 19 request) gets a config over 2912 characters as type-18 parts, `P<id>,<k>,<K>:<data>~`,
grid-split at 2880 bytes without cutting a UTF-8 character, each built by its own walk with PEERSLIVE frozen and the
CRC re-checked; refusals are `E<CODE>,<detail>` (NOMEM, CHANGED, NOPARTS, TOOBIG). The reply is a non-blocking job in
`loop()` (one frag per step); relays key reassembly on (sessionId, type, source), let a whole reply beat a part, and
print each line with one write; the Wizard collects, joins and verifies parts, queues pulls per relay and decodes
serial input as a stream. docs/MGMT_RELAY.md.

#### 92. Clearing a serial mapping leaves its NVS keys behind

| | |
|---|---|
| **Status** | VERIFIED - map.nvs_keys_freed and every map.* test PASS (20260924-233628); Greg: "Fix both" (2026-09-24) (`docs/HIL_TEST_AUDIT.md` F20) |
| **Owner** | `WCB_firmware` (`WCB_Storage.cpp`) |
| **Effort** | S |
| **Tests** | `map.nvs_keys_freed` (new: counts `serial_map` in `?NVS` around a clear and a `CLEAR,ALL`); `var.cap_persistent_full`, `inv.seqget_largest` failed on it mid-run |
| **Subsystem** | serial mapping / NVS |

**Evidence (run 20260924-190733).** `saveSerialMonitorMappings` never removes a key: a cleared slot keeps its input, count, raw,
prior-flag and per-output keys. The map suite left about 44 `serial_map` entries on W1 (up to ~130 possible), and the
persistent-variable table and a 900-character sequence then could not be written. `?ERASE,NVS` later in the run hid
it from the end-of-run `?NVS` reading.

**Fix.** `saveSerialMonitorMappings` (`WCB_Storage.cpp`) writes an active slot only, then `removeUnusedSerialMapKeys`
erases every key no active mapping uses: all of an inactive slot's (`_act` first, so a restart part-way leaves it
inactive) and an active slot's output pairs past its count, after the puts so the new count is stored first. Raw
`nvs_erase_key`, because `Preferences::remove()` logs an error for each key that was never written; an erase needs
no free space, so it works on a full store. `loadSerialMonitorMappings` already reads a missing `_act` as false, and
`?backup` is built from RAM, so neither changes. From the review: an active slot's `_act` is written last (a restart part-way
through a new slot leaves it inactive), and when its count cannot be stored (a full store) its output keys are left
alone and a warning prints, because NVS still holds the old count and erasing the pairs it names would reload them
as extra USB destinations.

#### 93. A client that keeps re-arming a remote terminal blocks a deferred reboot

| | |
|---|---|
| **Status** | VERIFIED - etm.reboot_rterm_rearm, etm.reboot_stats_rpt, etm.reboot_defer_cap, etm.w1_reboot_sees_peers, pwm.map_deferred_reboot PASS (20260924-233628); Greg: "Fix both" (2026-09-24) (`docs/HIL_TEST_AUDIT.md` F21) |
| **Owner** | `WCB_firmware` (`WCB.ino`) |
| **Effort** | S |
| **Tests** | new: `etm.reboot_rterm_rearm`, `etm.reboot_stats_rpt`, `etm.reboot_defer_cap`; failed on it: `etm.w1_reboot_sees_peers`, `input.wdpda_persists_reboot`, `pwm.wdp_selfheal_missed_clear` |
| **Subsystem** | deferred restart / RTERM |

**Evidence (runs 20260924-190733, 20260924-213158).** The quiet window before a deferred restart (4 s with no command processed) is
reset by every queue item. Intellex, connected to NaviCore's AP, made NaviCore send `?RTERM,START,20` to W1 about once
a second, so `?reboot` and a PWM reboot never ran; NaviCore's `?STATS,RPT` (every 30 s) alone stretches it to ~8 s.

**Fix.** `loop()` clears `quietWindowExempt` (`WCB.ino`) before each queue item and stamps the quiet-window clock only
when no handler set it; the `?STATS,RPT` branch sets it, and so does `?RTERM,START,<n>` when a session to relay `<n>`
is already running (it still re-announces the session). `RESTART_MAX_DEFER_MS` = 20 s after `loop()` first sees a
pending flag, the restart goes anyway (still never under a running config-pull reply), after an ungated
`Restart held off <n> s by commands that kept arriving - restarting anyway`. Greg approved about 15 s; 20 s is the
smallest round value that cannot cut a push the window protects: the commands that ask for a restart come last in a
Wizard USB push (`buildCommandString`) and in the `?backup` chain the harness replays (`collectConfigCommands`), so
at most 4 mappings follow the first (5 input ports), each within 4 s of the one before or the window restarts the
board anyway, each taking up to ~0.6 s: the last starts under 18.4 s after the request. A relay push is one MGMT
session, queued whole and drained in one `loop()` pass. `hil.wcb` waits 25 s for a `?reboot` now. From the
review: `restartImminent` is set just before the restart line, and after it the ETM receive path neither ACKs nor
queues a command, so one arriving in the last ~150 ms is retried by its sender instead of being ACKed and lost
(the cap fires exactly while commands are still arriving).
