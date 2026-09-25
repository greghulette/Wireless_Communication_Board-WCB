# HIL test audit — are the tests valid, and do they cover everything?

Working document, like `HIL_FIX_TRACKER.md`. Started 2026-09-23 at Greg's request: review `tests/hil` (and the
`tests/wizard` layer it drives) and record, test by test, whether each is a *valid* test — it can only pass when the
behaviour it names works — and where the firmware is left untested. Written to survive a context cut: every
section below is updated as the review goes, so a cold session can pick up at **§5 Resume here**.

Read `docs/HIL_TESTING.md` first (what the harness is), then this file. Nothing here changes a test; each finding
says what a fix would be, and the fix is separate work.

**Status 2026-09-23: the read-through is complete.** All 28 suites (422 tests), the harness core and `tests/wizard`
were read; 23 test findings (A1-A23) and one firmware observation (F1) are recorded, and §4 holds the coverage
diff. Nothing in `tests/hil` was changed and the bench was not used. `python tests/hil/selftest.py` passed 39/39 on
this tree.

## 1. Method

**Validity.** Each test was read against the harness primitives (`hil/*.py`, `suites/common.py`) and, where it
asserts a firmware specific (a byte sequence, a message, a limit), against the firmware source. A test is marked
with one or more of these codes when it does not hold up:

| Code | Meaning |
|---|---|
| V1 | **Vacuous** — can pass with the feature broken (e.g. asserts only that *something* printed, `expect` on a substring a stale line satisfies, a loop that asserts nothing when the list is empty). |
| V2 | **Asserts the wrong thing** — the assertion does not match the test's title/docstring, or checks a proxy (an ACK, a console echo) where delivery on the wire is the claim. |
| V3 | **State not restored** — changes saved config, RAM state, a peer table, a device, or a bench setting outside `config_guard`/its own restore, or restores in a way that can leave residue on failure. |
| V4 | **Timing luck** — a fixed sleep or a window that races the firmware, so a pass or a fail depends on load. |
| V5 | **Unchecked precondition** — depends on bench state (a config token, a wire's baud, WDP on, a peer online) it neither checks nor establishes, so a false FAIL or a silent SKIP. |
| V6 | **Stale vs firmware** — asserts a message or behaviour the current source no longer has (cite `file:line`). |
| V7 | **Helper misuse** — mark/since ordering, a read across a probe restart, `WCB.run()` across a delimiter/CMDCHAR change, `expect` where exact bytes are meant. |
| V8 | **Skips where it should fail** — a `Skip` raised for a condition that is the feature under test being broken. |
| V9 | **Redundant / mis-titled** — duplicates another test, or the id/area is misleading. |
| OK | Read; holds up. A note may still follow. |

**Completeness.** §4 lists firmware behaviour with no test, from the dispatcher and help text (`WCB_Help.cpp`,
`WCB.ino` command handlers, each `WCB_<area>.cpp`), compared with the registry (`grep '^@test(' suites/*.py`). The
2026-09-15 coverage audit in `HIL_TESTING.md` §7 is the baseline; §4 records what a fresh diff shows.

**What was not done.** Nothing ran on the bench during this audit (another session held the boards). Findings are
from reading; a finding that needs a bench run to confirm says so. The WcbCmd byte tables (HCR, MP3, DFPlayer, WLED,
Maestro) were spot-checked by hand (Maestro and DFPlayer frames re-derived), not re-derived in full.

## 2. Progress

Status: `read` (findings recorded) → `verified` (the finding's firmware claims checked against source).

| Suite | Tests | Status | Findings |
|---|---|---|---|
| harness core (`hil/*.py`, `suites/common.py`) | — | verified | A1-A4 |
| `s00_probe.py` | 2 | read | A5 |
| `s01_links.py` | 1 | read | OK |
| `s02_navicore.py` | 3 | read | OK |
| `s03_wcb.py` | 4 | verified | A6 |
| `s04_serial.py` | 7 | verified | OK |
| `s05_mesh.py` | 6 | verified | A7 |
| `s06_persist.py` | 4 | verified | A1 |
| `s07_seqvar.py` | 4 | verified | A1 |
| `s08_maestro.py` | 7 | verified | A8, A9 |
| `s09_wled.py` | 2 | read | A9 |
| `s10_navicore_mesh.py` | 3 | read | OK |
| `s11_sbus.py` | 2 | read | OK |
| `s12_serial_input.py` | 47 | verified | A1, A10-A13 |
| `s13_serial_mapping.py` | 36 | verified | A14, A15 |
| `s14_pwm.py` | 23 | verified | A16-A18 |
| `s15_hcr_mp3_dfp.py` | 35 | verified | A19, F1 |
| `s16_vars_if.py` | 31 | read | OK |
| `s17_seq_inventory.py` | 28 | read | OK |
| `s18_etm_config_wdp.py` | 43 | verified | A20, A21 |
| `s19_client_mesh.py` | 21 | read | A22 |
| `s20_ota.py` | 32 | read | A21 |
| `s21_navicore_sbus.py` | 38 | read | A21, A23 |
| `s22_maestro_kyber.py` | 29 | read | OK |
| `s23_softrx_measure.py` | 3 | read | OK |
| `s30_wizard.py` + `tests/wizard` | 7 | read | OK (gaps in §4) |
| `s99_etm.py` | 4 | verified | OK |
| coverage (§4) | — | done | help-text diff + dispatcher diff |

499 tests registered (2026-09-24, branch `WIFI`): 422 at the review on 2026-09-23 (uncommitted s12/s18 edits from the WDP-DA work included), the rest are the §5 additions, the §7 suites s24-s29 and s31, and the F20/F21 tests.

## 3. Findings

Numbered `A<n>` so the fix tracker can cite them. Severity: **S1** a test that can pass with the behaviour broken
or that damages the bench; **S2** a test that fails or skips for the wrong reason, or asserts something other than
its title; **S3** a nit (naming, a doc line, a redundant check). `F<n>` is a firmware or doc observation the review
turned up that is not a test defect. No S1 was found.

**Overall verdict.** The suites are valid tests in the sense that matters: they assert bytes on the wire and exact
console lines, never ACKs; every `(should)` test cites the code it guards; state changes are restored, usually in a
`finally`; preconditions are declared with `require_tokens` or a `Skip`. The substantive problems are three: the
harness detects a leaked config change but never restores it and 27 tests would leave one behind on failure (A1);
one test under-asserts against a bar the firmware has since raised (A10); and one documented firmware path, WDP PWM
self-heal, is never exercised (A17). Everything else is a nit.

### 3.1 Harness core

**A1 (S2) `config_guard` detects a leak but never restores it, and 27 tests restore outside a `finally`.**
`suites/common.py` `config_guard` snapshots, runs the body, re-snapshots and *fails* on a difference — it does not
put the board back. A test whose restore is the last statement of the body (not in `finally`) leaves the change
behind whenever an assertion before it fails. The runner then re-baselines (`_refresh_config_ref(after_leak)` in
`hil/runner.py`), so the resume check accepts the dirty state, and every later test's `config_guard` snapshot
includes it. Seen already: the 2026-09-21 rows "W1 S4 config drift from the aborted run" and "W1 S3 flags also left
OFF" in the `HIL_FIX_TRACKER.md` session log. A `?BCAST,IN,S3,OFF` left behind makes every later
`require_tokens(... "?BCAST,IN,S3,ON")` test SKIP, so one failure quietly silences a dozen `input.*` tests.
Tests with `config_guard` and no `finally` (found by AST over the suites): `wcb.help_trap`, `persist.label`,
`persist.label_remote`, `persist.baud_live`, `persist.label_reboot`, `seq.local`, `seq.timer`,
`input.bcast_in_block`, `input.bcast_out_block`, `input.bcast_parsing`, `input.bcast_legacy_forms`,
`input.bcast_s0_echo_local`, `input.bcast_s0_echo_remote`, `input.bcast_wled_port_not_excluded`,
`input.timer_keeps_source`, `input.recall_keeps_source`, `input.soft_rx_baud_sweep`, `map.parse_errors` (nothing to
restore), `pwm.p_kills_hw_uart` (the reboot precedes the assert), `pwm.map_validation` (nothing to restore),
`hcr.config_rejects` (nothing to restore), `etm.query_and_validation` (nothing to restore),
`etm.dedup_ring_covers_pending` (`_deaf_w1` has its own `finally`), `map.probe_mesh_raw_chunk_bounds` (nothing to
restore), `ota.relay_full_same_image_wcb2` (an aborted pass re-raises after `?OTA,ABORT`; W2 may end on the other
slot, documented), `navicore.trigger_dispatch_trace` (nothing to restore), `maestro.config_negatives` (clears its
placeholder before asserting), `wizard.pull` (nothing to restore). *Fix:* move each real restore into `finally`;
and consider a diff-restore in `config_guard` for a safe prefix set (`?BCAST,`, `?LABEL,`, `?BAUD,`,
`?SEQ,SAVE,`/`?SEQ,CLEAR,`, `?MAP,SERIAL,`, `?VAR,`): replay the missing tokens, issue the CLEAR form for the extra
ones, log what it did, then raise as now.

**A2 (S3) No probe-version floor that matches the doc.** `PROBE_MIN_VERSION = 2` (`hil/probe.py:8`) and
`probe.hello` accepts any v2+. `HIL_TESTING.md` §3 says run v7, not v6 (v6 boot-loops when a wired WCB reboots).
Only `probe.soft_concurrent_rx` looks at the version, for its own arm. *Fix:* raise the floor to 5 (the TX-idle-high
fix, tracker #79, which every byte-exact test depends on) and have `probe.hello` fail on exactly 6 with the reason.

**A3 (S3) `expect` is a containment check.** `Probe.expect_bytes` returns when `expected in got`. Tests that mean
"exactly these bytes" and only call `expect` pass with extra bytes before or after. The byte-exact suites already
follow `expect` with `received() == expected` after a settle (`s08 _frame`, `wled.verbs`, `input.terminators`,
`map.raw_local_verbatim`, `s15 _steps`, `s22 _exact`); the ones that do not are order/contiguity checks where
containment is the intent (`serial.chain`, `mesh.frag_chain`, `seq.local`, `navicore.wcb_send`). No change needed;
noted so a new test copies the right pattern.

**A4 (S3) `_infer_links` reads only literal `link(bench, N, "Sx")` / `wire(...)` calls.** A test that picks its
port at run time must declare `links=[...]`, where each list entry is required and `"A|B"` inside one entry means
any one. `map.text_inbound_from_w2`, `map.raw_remote_soft_port` and `navicore.wcb_send` declare correctly;
`bench.links.get()` is used only for optional wires. Three declarations are wrong the other way: A21.

### 3.2 Per suite

#### s00_probe, s01_links

| Test | Verdict | Note |
|---|---|---|
| `probe.hello` | OK | See A2. |
| `probe.links` | **A5 (S3)** | Asserts the `verified` flag saved by the last discovery, not a live check; the title reads as if it re-verifies. `link.out` is the live check, so the pair is fine — say "as discovered" in the title. |
| `link.out` | OK | Every wire, byte-exact, local or over the mesh; the right foundation test. |

#### s02_navicore, s03_wcb, s04_serial

| Test | Verdict | Note |
|---|---|---|
| `navicore.ping`, `online`, `wdp` | OK | |
| `wcb.backup_crc`, `wcb.mgmt_pull`, `wcb.unknown` | OK | `Unknown command:` and the CRC-32 verified in `WCB.ino`. |
| `wcb.help_trap` | **A6 (S3)** | Asserts only that the label was *not* set. The title says it "prints help": add an assert on `Usage: ?LABEL,Sx,<text>` (`WCB_Help.cpp:138`). |
| `serial.comma`, `s0`, `chain`, `bad_port` | OK | `Invalid serial port number` verified in `WCB.ino`. |
| `serial.timer`, `timer_sum`, `timer_stop` | OK | Timed on the probe clock; 1400-1800 / 1100-1500 ms windows are sane. `Timer sequence manually stopped` verified (`command_timer.cpp:82`). |

#### s05_mesh, s06_persist, s07_seqvar

| Test | Verdict | Note |
|---|---|---|
| `mesh.unicast_acked` | **A7 (S3)** | Title says "ACKed exactly once"; the assert is `ackd >= before + 1` and `sent` is not checked. Nothing else unicasts W1→W2 in that second, so `sent == +1 and ackd == +1` would hold and match the title. |
| `mesh.alias`, `unreachable`, `chain_split`, `frag_chain`, `rterm` | OK | `is not a reachable target` verified. |
| `persist.label`, `label_remote`, `label_reboot` | OK | Restore outside `finally` (A1). `label set to:` verified (`WCB_Storage.cpp:1809`). |
| `persist.baud_live` | OK | Pins the wire at the new rate; the runner releases pinned wires after the test. A1: a failure at the new rate leaves S1, the Maestro port, at 115200 or 57600. `Baud rate for Serial%d updated to` verified (`WCB_Storage.cpp:196`). |
| `seq.local`, `seq.timer`, `seq.mesh` | OK | The first two clear the key outside `finally` (A1); `seq.mesh` does it right. `Stored: Key=` verified (`WCB_Storage.cpp:763`). |
| `var.if` | OK | Bare `IF,` line at the console; restore in `finally`. |

#### s08_maestro, s09_wled, s10_navicore_mesh, s11_sbus

| Test | Verdict | Note |
|---|---|---|
| `maestro.sub`, `comma_int`, `verbs`, `target9` | OK | Every expected frame re-derived by hand from the Pololu protocol (14-bit low7/high7): all correct. Exact compare after a settle. |
| `maestro.bad_verb` | **A9 (S3)** | Silence-only: a dead wire or a dead Maestro path passes. Add a positive control inside the test (`;M11` → `AA 01 27 01`) before the bad forms. Same for `wled.bad_verb`. |
| `maestro.fanout_remote` | OK | Skips when NaviCore hosts no device 1 — a bench-state skip, right. |
| `maestro.remote_readback` | **A8 (S3)** | Moves the real servo on W2's Maestro 2 to 6400 then 6000 without reading where it was; 6000 is assumed to be rest. Read `getPosition` first and finish on that value, as `navicore.maestro_mesh_settarget_readback` does. |
| `wled.verbs` | OK | Exact JSON with settle. |
| `wled.bad_verb` | A9 | See above. |
| `navicore.cli_over_mesh`, `wcb_send`, `trigger` | OK | |
| `sbus.ping`, `sbus.to_navicore` | OK | Expected values from the controller's own `getcfg`; centre restored in `finally`. |

#### s99_etm, s30_wizard (+ `tests/wizard`)

| Test | Verdict | Note |
|---|---|---|
| `etm.remote_reboot`, `etm.w1_reboot_sees_peers` | OK | "answers again" is the `snapshot(bench, 2)` pull. |
| `etm.char_unicast`, `etm.char_loaded` | OK | One `?ETM,CHAR` per process, parsed once. |
| `wizard.parser` | OK | The 8 unit tests are the right shape: round trip, empty diff, own-only diff, CLEAR forms, system file. |
| `wizard.smoke`, `pull`, `push_label`, `terminal_wire` | OK | Harness owns the before/after; Playwright only drives the page. `push_label` restores in `finally`. |
| `wizard.kyber_release_order`, `kyber_local_frees_other_port` | OK | Generator logic in the real page, no board. |

#### s12_serial_input (47)

| Test | Verdict | Note |
|---|---|---|
| `input.cmd_to_usb`, `terminators`, `partial_line_waits`, `per_port_buffers`, `partial_poisons_next`, `trim_and_blank`, `nul_ignored`, `multi_line_and_chain`, `long_line_soft` | OK | Exact-byte compares after `expect`; preconditions via `require_tokens`. |
| `input.queue_overflow` | OK | Polls `?VAR,GET` to a stable value; `Command queue is full! Discarding command.` verified (`WCB.ino:2435`). |
| `input.bcast_fanout`, `bcast_in_block`, `bcast_out_block`, `bcast_legacy_forms`, `bcast_s0_echo_local`, `bcast_s0_echo_remote`, `bcast_wled_port_not_excluded`, `bcast_json_local_only`, `bcast_too_long_local_only`, `bcast_text_traps`, `timer_keeps_source`, `recall_keeps_source` | OK | Messages verified (`Broadcast blocked from Serial%d` `WCB.ino:7398`, `Invalid format. Use: ?BCAST` `:5515`, `Ignored chain command:` `:2681`, `Recalling command for key` `WCB_Storage.cpp:636`). Restores outside `finally` (A1). |
| `input.bcast_parsing` | **A11 (S3)** | Ends with S5 output *enabled* and never reads the original; add `require_tokens(bench, 1, "?BCAST,OUT,S5,ON")` so the assumption is explicit, or restore from the guard's `before`. |
| `probe.soft_concurrent_rx` | OK | An instrument check living in the input suite under area `probe`; fine, but the file header says "Serial INPUT". |
| `input.maestro_query_reply`, `reader_map_w1`, `w2_ports`, `bcast_maestro_port_excluded` | OK | Cleanups in `finally`, including the `;M2,getErrors` flush. |
| `input.bcast_dfp_port_excluded` | **A12 (S3)** | Restore covers `?BCAST,*,S5` and `?LABEL,S5` but assumes S5 is at 9600: `?DFP,CLEAR` resets the freed port to 9600 (`WCB_DFP.cpp:138`) and `?DFP,S5` re-bauds it when it is not (`:294`). Add `require_tokens(bench, 1, "?BAUD,S5,9600")`, as s15's `_require_free` does. |
| `input.soft_rx_under_soft_tx`, `framing_variants` | OK | Loader thread stopped in `finally`. |
| `input.soft_rx_baud_sweep` | **A10 (S2)** | Asserts only `counts[19200] == 20`. Its comment says 38400 "has measured 15-19 of 20" — that predates the level-3 GPIO ISR (tracker, 2026-09-21): CLAUDE.md rule 13, `HIL_TESTING.md` §6 and the firmware's own warning (`WCB_Storage.cpp:168-175`: "measured exact through 57600, none at 115200") all say 38400 and 57600 are 20/20 now. A regression to 15/20 at 38400 or 57600 passes this test, and the title ("exact at 19200 and 38400") promises more than it checks. *Fix:* assert 38400 and 57600 == 20, keep 115200 recorded; restore S3's baud from the guard's `before` token instead of a hard-coded `?BAUD,S3,9600`. |
| `input.softserial_tx_rmt` | **A13 (S3)** | Sends `?BAUD,S3/S4/S5/S2,9600` unconditionally. Re-issue each port's own `?BAUD` token from the config so the test is config-independent; the guard would otherwise fail it on a bench with another rate. `[SOFTSERIAL] S%d TX: RMT (hardware-timed)` verified (`WCB.ino:2268`). |
| `input.wdpda_*` (9) | OK | Thorough; each forgets its records in `finally` and scrubs `HIL*` leftovers first. `wdpda_ttl` has ±4 s windows on 90/150 s firmware timers (fine). `wdpda_shared_port` raises Skip after the record checks when S3 is labelled — a documented partial. Session `wcb-ce` owns these; not re-verified against its uncommitted firmware. |

#### s13_serial_mapping (36)

| Test | Verdict | Note |
|---|---|---|
| all 36 | OK | The strongest suite: every test clears its mapping in `finally`, exact-byte checks with error-line checks, `(should)` docstrings cite code. `Serial mapping set:` / `removed for` verified (`WCB_Storage.cpp:2089`, `:2248`). |
| `map.text_multi_dest` | **A14 (S3)** | Title says "in list order"; the test only notes the S3/S4/S5 burst times, it does not assert their order. Either assert `times` is sorted or drop "in list order". |
| `map.raw_remote_115200_exact`, `raw_remote_bursts_no_loss`, `raw_remote_soft_port`, `baud_on_raw_mapped_port` | **A15 (S3)** | Hard-coded `?BAUD,S2,9600` / `?BAUD,S3,9600` restores (same as A13). Use the `before` token. |
| file header | S3 | "never CLEAR,ALL (it leaves broadcast output off)" is stale: tracker #46 fixed it and `map.clear_all_restores_output` guards it. |

#### s14_pwm (23)

| Test | Verdict | Note |
|---|---|---|
| all 23 | OK | Every mapping test holds the PWM input LOW (`pwm_out(0)`) and clears in `finally`; reboots are waited on by banner. |
| `pwm.map_deferred_reboot` | **A16 (S3)** | Title: "reboots only after ... 4 s of quiet"; assert is `gap < 12`. `PWM_REBOOT_QUIET_MS` is 4000 (`WCB.ino:1104`) and `gap` is measured from the last `w.version()`, so tighten to `3.5 <= gap <= 7`: a reboot that fires early, or a quiet time that doubled, both fail. |
| `pwm.wdp_autoconfig_and_selfheal` | **A17 (S2, coverage)** | "either route passes": the explicit remote CLEAR (tracker #8) always wins, so the WDP self-heal branch (`[WDP] WCB%d no longer drives our S%d - clearing auto-configured PWM output`, `WCB_PWM.cpp:905`, verified present) is never exercised by any test, and its one assertion ("self-heal rebooted W2") never runs. The bench cannot make W2 miss the ETM clear. *Idea:* the probe, as a temporary WCB_Client, advertises a PWMTARGET TLV for W2 S3 and then leaves — W2 should auto-configure, then self-heal when the advert stops. Needs a probe verb (`MESH ADVERT PWMTARGET …`) and a WCB_Client hook. |
| file header | **A18 (S3)** | "?MAP,PWM,CLEAR,OUT / ?PX restart INLINE ... sent with send()" is stale (deferred since tracker #9; `_inline_clear_out` says so). |

#### s15_hcr_mp3_dfp (35)

| Test | Verdict | Note |
|---|---|---|
| all 35 | OK | Exact device bytes via `_steps` (exact compare per step), replies played by probe rules, fixtures `_hcr_w2` / `_w2_audio` restore in `finally` and `_unlearn` undoes the persisted WDP route in the only order that works. `[DFP] Configured:` verified (`WCB_DFP.cpp:321`). DFP frame checksum re-derived: correct. |
| `hcr.status_reply_parse` | **A19 (S3)** | `for va in (42,):` is a one-element loop left over from an earlier shape; harmless. |
| `hcr.config_rejects`, `mp3.config_rejects` | OK, but see F1 | They pin the current "SOFTWARE SERIAL — unreliable above 9600 baud / CONFIGURATION BLOCKED" refusals (`WCB_HCR.cpp:868`, `WCB_MP3.cpp:295`, and `WCB_WLED.cpp:315`), which the firmware itself no longer believes elsewhere. |
| `hcr.setemotion_100_not_silent` | OK | `assert bad or lines`: passes on either "sent" or "rejected", which is the claim. |
| `hcr.softserial_integrity_mesh_load` | OK | 300 frames + ~1800 mesh commands, hardware probe channel so the probe is not the suspect. |

#### s16_vars_if (31)

| Test | Verdict | Note |
|---|---|---|
| all 31 | OK | Every variable cleared in `finally`; persistent-table tests replay the saved `?VAR,SET` tokens; `var.cap_volatile_evicts` restores by reboot to avoid 100 NVS writes. IF tests measure on the probe clock. `var.v_set_get_list` / `v_errors` send `?DEBUG,OFF` unconditionally (RAM-only, harmless). |

#### s17_seq_inventory (28)

| Test | Verdict | Note |
|---|---|---|
| all 28 | OK | Every key cleared in `finally`; the cycle-guard tests check the board still answers and reboot it if not; the relay tests retry the once-sent frags as a real requester would and say why when every try misses. `inv.dedup` is timing-sensitive by its own title. |

#### s18_etm_config_wdp (43)

| Test | Verdict | Note |
|---|---|---|
| all 43 | OK | ETM/MAC/CHKSM changes are W1-only, restored first in `finally`, and W1 is never reset while deafened. Delivery is asserted on the wire, never on ACKs. The `chars.*` tests use `dev.send` while `WCB.run()` would break. |
| `wdp.temp_probe_lifecycle`, `client_mesh.adopt_identity_route`, `navicore.probe_temp_peer_not_learned` | **A20 (S3)** | Assert the probe's WDP row has `FW=wcb_probe-2`. Verified: that is a fixed identity literal in `probe_main.cpp:1031` (`setIdentity(meshType, "wcb_probe-2")`), independent of `PROBE_VERSION` (`:73`), so the tests are right today. Name it (`PROBE_MESH_FW`) and have the harness read it from `HELLO`, or a future probe bump that "fixes" the literal breaks three tests at once. |
| `wdp.da_announce_propagates` | **A21 (S3)** — fixed here | Declared `links=["W2S3", "W2S4", "W2S5"]`, which `missing()` reads as *all three* required, while the body uses one unlabelled port among them. **Fixed 2026-09-23 by session `wcb-ce`** (uncommitted with its WDP-DA work): `s18_etm_config_wdp.py:1509` now declares `["W2S3\|W2S4\|W2S5"]`, verified in the file. Still open: the same over-declaration in `navicore.plain_broadcast_both_ways` (all five listed; `_expected_ports` tolerates any subset) and `ota.relay_full_same_image_wcb2` (`W2S1` listed; the tap is optional in the body). |

#### s19_client_mesh (21)

| Test | Verdict | Note |
|---|---|---|
| all 21 | OK | One mesh id per command-sending test (`MESH_IDS`), `probe_in_mesh` forgets the temporary peer on exit, ACK-before-execute and "not re-broadcast" are checked on the console with ETM debug, delivery on the wire. `rejoin_seq_reuse` reboots W1 before and after so the duplicate ring starts clean. |
| `client_mesh.sendraw_real_maestro_tap` | **A22 (S3)** | Moves the real servo (Maestro 2 ch 0 → 1500 µs) and never reads or restores its position — the same shape as A8. |
| `map.probe_mesh_raw_chunk_bounds` | OK | `config_guard(1, 2)` with nothing to restore (A1 list). |

#### s20_ota (32)

| Test | Verdict | Note |
|---|---|---|
| all 32 | OK | Exact `[OTA…]` line lists (`got != want`), `_local()` always leaves W1 idle at 115200, relay sessions abort in `finally`, every erasing test is opt-in, `_image()` refuses an image that is not the running firmware, full transfers run twice so a board ends on its original slot. `ota.help_matches_parser` guards the help text against the parser. |
| `ota.relay_full_same_image_wcb2` | A21 | Over-declared `links=["W2S1"]`; see above. A failed first pass leaves W2 on the other slot with the same image (documented, W2's USB is the recovery). |

#### s21_navicore_sbus (38)

| Test | Verdict | Note |
|---|---|---|
| all 38 | OK | Read-only wherever NaviCore would write flash; the mode, servo positions and matrix buttons are restored in `finally`; SBUS channels are moved only where NaviCore binds nothing; `_undriven_channel` avoids passthrough knobs (tracker #76). `mesh_stats_counts` starts its window after NaviCore's stats report and tolerates the mode report. |
| `sbus.btn_single_tap`, `navicore.set_mode` | **A23 (S3)** | Titles claim "also relayed to W1" / "emits rc_mode once", but the relayed `rc_trig` and the `rc_mode` count are only `bench.note`d (best-effort broadcasts); only the *repeat* suppression is asserted. Either assert with a retry, or reword the titles. |
| `navicore.plain_broadcast_both_ways` | A21 | Over-declared links; see above. |
| `navicore.trigger_dispatch_trace` | OK | Fires a real mapping's WCB actions; `config_guard(1, 2)` catches a mapping whose `cmd` changes config. |

#### s22_maestro_kyber (29)

| Test | Verdict | Note |
|---|---|---|
| all 29 | OK | The most careful suite: WDP off around every slot-table change so no proxy is ever advertised, tables rebuilt in backup order, every Kyber mode change restored with `?KYBER,CLEAR` + `?MAESTRO,REMOTE` + the port tokens + a reboot that must show the Maestro_Remote task, a "restore if aborted" note per test, real-Maestro traffic limited to `getErrors`/`stopScript`. `kyber.remote_roundtrip` tolerates the documented ~1 % best-effort loss with three tries while still failing on any reorder or duplicate. |

#### s23_softrx_measure (3)

| Test | Verdict | Note |
|---|---|---|
| `softrx.erratum_pairs` (opt-in), `soak.w1s4_wire` (opt-in) | OK | Measurements with an explicit verdict rule (confirmed / refuted with a coverage floor / inconclusive) and a CSV; a reboot mid-run is "not a result". |
| `softrx.level_irq_stuck_line` | OK | A clean-port miss is a FAIL, not a Skip (the fix's own regression); the restore waits out a reset without gating on `rebooted_since()`. |

### 3.3 Firmware and doc observations

**F1 — the device guards and `?BAUD` disagree about soft-serial baud.** `?BAUD,S3-S5` accepts up to 57600 with no
warning and warns only above it ("measured exact through 57600", `WCB_Storage.cpp:168-175`, matching CLAUDE.md
rule 13), but `?HCR,PORT,S4:19200`, `?MP3,S3:38400`, a WLED on a soft port and `?MAESTRO,M1:W1S3:115200` are still
refused as "SOFTWARE SERIAL — unreliable above 9600 baud / CONFIGURATION BLOCKED" (`WCB_HCR.cpp:868`,
`WCB_MP3.cpp:295`, `WCB_WLED.cpp:315`, `WCB_Maestro.cpp:643-656`). One of the two is wrong; `hcr.config_rejects`,
`mp3.config_rejects` and `maestro.config_negatives` currently pin the refusal, so they need updating when the
guards are reconciled.
**Done 2026-09-24 (tracker #81):** the HCR and WLED guards refuse above 57600 and the MP3 block is gone (the Maestro
guard already sat at 57600); the help, the wiki and the Wizard's HCR/WLED baud pickers follow. `devices.soft_ports_fast_baud`
and `wled.soft_port_57600` prove the accepted rates on the wire, the reject tests the 115200 refusal. Verified on the bench
(20260924-092423).

**F2 — the help text's legacy PWM form is wrong.** `WCB_Help.cpp:100` advertises `?PMSSx,dest`, but the legacy handler builds
`"PMS" + message.substring(3)` (`WCB.ino`, the `pms` branch), so `?PMSS3,S4` reads its input port from `S3` as 0 and is refused
(`Input port must be 1-5`); the working spelling is `?PMS3,S4`, the string the modern `?MAP,PWM,S3,S4` builds itself
(`WCB.ino:6037`). Pinned by `pwm.legacy_forms`. **Done 2026-09-24 (tracker #82):** the help reads `?PMSx,dest`.

**F3 — `?HW` writes NVS only, so `?backup` keeps reporting the running version until a boot.** `saveHWversion`
(`WCB_Storage.cpp:72-98`) validates, writes `hw_version` and prints "Reboot to take effect!", but never touches the live
`wcb_hw_version`, and the chain emits the live value (`collectConfigCommands`, `WCB.ino:3439-3446`). A Wizard pull right
after a `?HW` push therefore shows the old version, and the Wizard's diff wants to push it again; the saved value can only
be read back by booting on it. Harmless on a board that is rebooted after the push, confusing on one that is not. Pinned
by `ident.hw_setter`, which read the old value back on purpose (bench `20260924-005924` found it). **Done 2026-09-24
(tracker #83):** `saveHWversion` updates the running value too (the pin map stays boot-only; only the status-LED branches
read it live), and `ident.hw_setter` now asserts the chain follows. Verified on the bench (20260924-092423).

**F4 — the Wizard's Kyber target fallback is keyed on the Maestro id alone.** `autoComputeKyberTargets` (`Wizard/app.js:9504`)
rebuilds a local-Kyber board's targets from every connected board's Maestros, then keeps a remembered target (from the last
`?backup`) only when no connected board has a Maestro with that id. The same id on two boards is legal (CLAUDE.md rule 5),
so a remembered `M1` on an unconnected W3 is dropped as soon as a connected board reports its own `M1`, and the Kyber board
loses that route until W3 connects. **Done 2026-09-24:** the fallback keeps a remembered target unless its board reported a
live Maestro list, so the M1 on W3 survives and a remembered target on a board that is connected is dropped;
`wizard.kyber_auto_targets` asserts both (3 of 3 Kyber Playwright tests pass headless).

**F5 — `?TRACK` is inert.** `?TRACK,ON` / `OFF` and the legacy `?TRACK_ALL_ON` / `_OFF` set `trackCommandDelivery`
(`WCB.ino:742`), which nothing reads except `printTrackingStatus` (`WCB.ino:6401`), and no help page lists `?TRACK`. Wire it
to something or remove it with its legacy spellings. Pinned by `misc.track_command` and `misc.track_legacy`. **Done 2026-09-24 (tracker #85):** `?TRACK` and its three old
spellings are removed and answer "Unknown command"; `misc.track_removed` replaces both tests.

**F6 — a negative `;T` delay waits 30 minutes.** `parseCommandGroups` (`command_timer.cpp`) stores `String::toInt()` in an
`unsigned long`, so `;T-1` wraps to 4294967295 and is capped at the 1800000 ms limit instead of being refused, and the
warning prints only with `?DEBUG` on. A typo such as `;T-500` silently holds the rest of a chain for half an hour.
Pinned by `serial.timer_cap_negative`. **Done 2026-09-24 (tracker #86):** a negative delay refuses the whole chain
("Timer refused: ... Nothing in this chain was run."), and the 30-minute cap is reported with or without `?DEBUG`.

**F7 — an out-of-range `?LABEL` port gets no reply.** `?LABEL,S9,x` and `?LABEL,CLEAR,S9` reach
`saveSerialLabelToPreferences` / `clearSerialLabel` (`WCB_Storage.cpp:1801`, `:1817`), which return before printing
anything; `?BAUD` says `Invalid serial port 9 (must be 1-5)` for the same mistake. Pinned by `persist.baud_label_validation`. **Done 2026-09-24 (tracker #87):** `?LABEL` and
`?LABEL,CLEAR` answer `Invalid serial port <n> (must be 1-5)`, and the old `?SLCS<n>` goes through the validating
handler nothing had called (`Invalid port. Must be 1-5`).

**F8 — the NVS erase keeps the WiFi settings and the saved serial devices.** `eraseNVSFlash` (`WCB_Storage.cpp:1053`)
clears every namespace except `wifi_cfg` and `wdp_da`, so after `?ERASE,NVS` (or `?WCB_ERASE`) a board still hosts, or
still joins, its old WiFi network with the old password, and still lists its saved serial-attached devices. The help
(`WCB_Help.cpp:1039-1054`) says the command erases ALL settings and returns factory defaults, and its list of what gets
erased names neither. Keeping WiFi may be wanted (a board reached over WiFi stays reachable after an erase) or not (a
reset board keeps a credential); either the erase or the help should change. Pinned as it is by
`nvs.erase_defaults_restore` and `nvs.wcb_erase_alias`. **Done 2026-09-24 (tracker #88):** the erase now also clears
every namespace it finds (`eraseAllNvsNamespaces`), the radio's data and older firmware's leftovers included - the
same end state as the Wizard's Factory Reset - and prints how many; the help lists what goes.

**F9 — the sequence list is rewritten without checking the write.** Both the save and `eraseStoredCommandByName`
(`WCB_Storage.cpp:761-762`, `:804`) call `preferences.putString("key_list", ...)` and ignore the result. When NVS cannot
take the write, `?SEQ,CLEAR` deletes the value, prints "Deleted stored command key", and leaves the key listed, so
`?backup` then carries an empty `?SEQ,SAVE,<key>,` (run `20260924-092602`, `seq.cycle_guard_reuse`: `HILL1`, two clears).
The save can do the reverse: store a value the list never names - a sequence nothing lists, backs up or clears, which
keeps using NVS. Check both writes, print the failure, and when the save's list update fails, remove the value it just
stored. Tracker #84. **Done 2026-09-24:** both writes are checked; a failed save takes its value back out and says
"Not stored"; a clear that cannot update the list says so instead of "Deleted". `seq.nvs_full_consistency` (opt-in
`nvs_fill`) fills W1's store until a save is refused and checks nothing is left behind; both refusals occur in it
("NVS write rejected" for a value, "could not update the sequence list" for the list).

**F10 — nothing reports how full NVS is.** In run `20260924-092602` W1's NVS could not take a 38-variable table
(`var.cap_persistent_full`) or a sequence of 900 characters or more (`inv.seqget_largest`). Both passed in the two previous
full runs, and all three storage failures passed again once `nvs.erase_defaults_restore` had wiped W1 (`20260924-112513`).
What filled it cannot be told from outside: no command reports NVS usage, and F9 can hide sequence values from the
backup. A read-only `?NVS` (`nvs_get_stats`: used, free and total entries; per-namespace counts with `nvs_entry_find`)
would let the harness check headroom before the storage-heavy tests and name what fills it. The partition is 20 KB
(`min_spiffs`: NVS at 0x9000, 0x5000 long). Whether a well-used board in a droid can fill it is open. **Done
2026-09-24 (tracker #89):** `?NVS` prints `NVS: used= free= available= total= namespaces=` and one line per
namespace; the harness records it for each USB board at the start and end of every run of 5+ tests (session.log,
report.md, and a cumulative `results/nvs_history.csv`), and `wcb.nvs_report` checks it adds up. First readings, on
the restored bench: W1 404 of 630 entries used (64%), 100 available; W2 367 (58%), 137 available. After the erase
tests rebuilt W1 from its chain, W1 used 342 (54%), 162 available: the erase also cleared leftovers. See F11.

**F11 — the radio keeps its own data in NVS, part of it a copy of the WiFi settings.** Before the erase tests, `?NVS`
on W1 showed the WiFi driver's namespace `nvs.net80211` at 94 entries and the PHY calibration `phy` at 66, together
25% of the 630. After `?ERASE,NVS` and a restore from W1's chain, `nvs.net80211` held 36. The fixed-size tables
`maestro_cfg` (45), `wled_cfg` (45) and `kyber_targets` (37) take another 20%. The firmware never calls
`WiFi.persistent(false)`, so the driver keeps its own copy of the WiFi settings beside the WCB's `wifi_cfg`; turning
that off would free about 1 KB on a freshly written board. A larger NVS partition is the other lever, but it needs a
new partition table and a full reflash of every board. **Deferred 2026-09-24 (Greg): left as it is;** revisit it
if `results/nvs_history.csv` shows the store creeping towards full.

**F12 — `?backup` loses its one-line chains when the heap is tight, and still prints a checksum.** `printBackupConfig`
builds both chains as Strings (`WCB.ino:6758-6759`), then copies each to append the checksum (`:6789-6790`). An
Arduino String whose allocation fails comes back empty instead of failing (tracker #58), so a chain can print as an
empty line, or as `^?CHK<crc>` alone: the checksum of a chain that was never printed. Seen in `seq.nvs_full_consistency`
(20260924-120036), then measured on W1 with WiFi in AP mode (about 19 KB of byte-addressable heap free, largest block
16-17 KB) by adding 500-character sequences to its 899-character chain: the factory-reset chain printed empty at a
2.5 KB chain and as the checksum alone at 3.5 and 4 KB; at 4.5 KB the configured chain did the same. It is not
deterministic (a 3 KB chain printed whole between those failures), and the heap's minimum since boot fell to 2.5 KB
during these backups. The per-command lines above the chains stay complete, and the Wizard reads a live board from
those, so its view is unaffected; what fails is the line a user copies to restore a board. A failed append while
building (`:6758`) would drop a token and still print a matching checksum; that was not observed.
`buildConfigString` (`:3623`, the mesh config pull) holds one chain and one copy (`:3649`), so it would fail at a
larger config, the same way, by sending an empty config; not measured. Measured with WiFi in AP mode only; with WiFi
off the heap is larger and the limit higher. Recommendation: print each chain by running `collectConfigCommands`
again and writing each token as it comes, with a running CRC-32, so nothing grows with the config; make the mesh
pull check its copy and say it is out of memory. Tracker #90. **Done 2026-09-24 as recommended:**
`printBackupConfig` streams each chain from its own pass over `collectConfigCommands`, with the CRC-32 running
over the bytes printed (`crc32Update`); the six sub-emitters are plain token producers (`emit*Backup`). Output
goes through a 2 KB write buffer flushed per line, so a typical chain, and every per-command line, leaves in one
write that no other task's print can split. The mesh pull measures the config first (`configStringLength`),
refuses one over 2912 characters before building it, reserves once, and sends an empty reply when out of
memory; both refusals print on the target's console, ungated (F13 has since replaced this pull with the
config-pull job: `configPullWalk`, parts and CFGERR codes, tracker #91). The output is byte-identical to before on W1 and
W2. `wcb.backup_large_config`, `wcb.pull_size_limit`, and `seq.nvs_full_consistency`, which now requires both
chains whole on a full store, pass (20260924-133332).

**F13 — a config pull over 2912 characters still gives the requester nothing.** The target now says why on its
own console, but the requester, usually the Wizard through a relay, only sees its pull time out after its retries.
An error text in the reply is not safe with today's Wizard: `parseBackupString` finds no `?` tokens in it and
returns a board with default settings, which `remoteBoardPull` stores as that board's config and baseline
(`Wizard/app.js`). A requester-visible error needs a Wizard that recognizes one, shipped first. Raising
`MGMT_MAX_CHUNKS` instead costs RAM on every board: the reassembly buffers are sized from it (`MGMT_OUT_BUFSZ`,
three static slots of 2960 bytes, plus the pull session's chunks), on a heap with about 19 KB free in AP mode.
Greg's decision. **Done 2026-09-24 on Greg's word ("put an error, but ... could we break it up and resend in
chunks?"), tracker #91:** a requester that asks with `?MGMT,PULL,<n>,P` gets a config over 2912 characters in
parts (packet type 18, `[MGMT:CFGPART,<n>]P<id>,<k>,<K>:<data>~`), and every refusal is a coded error
(`[MGMT:CFGERR,<n>]NOMEM|CHANGED|NOPARTS|TOOBIG,<detail>`). Old Wizards never see a part: the parts request
(type 19) is sent only by a new relay for a `,P` pull, and neither new tag starts `[MGMT:CONFIG,`. The target's
reply became a non-blocking job, relay lines are one write each, and the Wizard serialises pulls per relay.
Designed, reviewed (4 critics, 45 issues) and re-reviewed after implementation (5 lenses, 15 findings, all
fixed or recorded below); the protocol is [MGMT_RELAY.md](MGMT_RELAY.md).

Found while doing F13, recorded for Greg's decision (none is fixed):

**F14 — a relay PUSH is capped at 2864 characters of changes.** The Wizard's relay push (`boardGoRemote`,
`Wizard/app.js`) sends at most 16 x 179 characters per push, including an appended `?reboot`, and checks the size
only after the network-group confirm and the character-set bootstrap have run. A board pulled in parts may need a
push split in several, or USB. Proposed: check the size first, then split pushes, keeping network-group commands,
PWM input mappings and `?reboot` in the last one.

**F15 — the other mgmt handlers still compare passwords as Strings.** STATS, ETM, SEQ and SEQVAL (and the relay's
STATS/ETM/SEQ reassembly) use `String(pkt.structPassword) != String(espnowPassword)`; with the heap exhausted both
allocations fail, the two empty Strings compare equal and a wrong password passes. From code reading, never
observed. F13 converted the config handlers to strncmp on the fixed field; the rest is the same one-line change.

**F16 — NaviCore's WsSink can write past its buffer.** `WsSink::write` (NaviCore `navicore_wsserver.h`) calls
`pump()` at 2048 bytes and stores the next byte even when `pump()` could not drain (a failed `ps_malloc` or
`httpd_queue_work`). From code reading, not reproduced; long lines such as config replies are what reach it.

**F17 — one WCBClient example does not use the placeholder credentials the others share.** It is handled in the
WCBClient repo; the details are deliberately kept out of this doc.

**F18 — `[ETM] WCBn came ONLINE` is printed from the WiFi task.** The receive callback prints it directly
(`WCB.ino`, `espNowReceiveCallback`), against rule 11. On the UART it can only land between whole lines now, but
the WebSocket and RTERM tees copy a line a byte at a time, so there it can land inside a `?backup` chain or a pull
part (the requester then drops that line and retries). Since F12, `printBackupConfig` prints each section header,
then builds that chain through a whole `collectConfigCommands` pass before writing it, so the line can also land
between a header and its chain (10-20 ms per header). It did, in `wizard.remote_pull` (run `20260924-234056`), whose
parser took the line after the header as the chain; the harness now searches the section. The Wizard reads a live
board's per-command lines, which end before the first header (`extractIndividualCommandLines`, `parser.js`), so a
live read is not affected; only a pasted fragment starting at the factory header would be. Proposed: queue the
print for `loop()` in a small ring of its own (say 8 x 96 bytes, drained beside `drainMgmtOut`), not the MGMT out
ring, which holds two lines and drops when full, so a fleet power-up would cost config replies or `came ONLINE`
lines that the Wizard and the harness wait for. `?backup` runs in `loop()`, so the line would then print after
`End of Backup`. A cheaper partial fix: put each header into its chain's `BackupWriter`, so header and chain leave
in one write under 2 KB, as they did before F12.

**F19 — some config lines can still be lost silently to the heap.** Inside `collectConfigCommands`, a failed
in-place `cmd += ...` (the MAP,SERIAL / MAP,PWM output loops, the KYBER,LOCAL target loop) leaves a line truncated,
and a failed read of `key_list` or a key substring skips a sequence; neither is visible to `?backup` or to the
pull's walk check (which does catch a token whose String failed outright, as NOMEM). The same class as #90/#58.

Found by full run `20260924-190733` (see §5):

**F20 — clearing a serial mapping leaves its NVS keys behind.** `saveSerialMonitorMappings` (`WCB_Storage.cpp`)
only ever writes: a cleared slot keeps `sm<i>_in/_cnt/_raw/_pbo/_pbi` and one `sm<i>_<j>w/p` pair per output it
ever had, until `?ERASE,NVS`. `removeSerialMonitorMapping` and `clearAllSerialMonitorMappings` just re-save with
`_act` false. After heavy use that is up to ~130 of the 630 entries (5 slots x 6 keys + 10 outputs x 2 keys each);
the map suite alone left about 44 on W1, which is what failed `var.cap_persistent_full` and `inv.seqget_largest`
later in the run (and in `20260924-092602`). MP3 and DFP keep their 8 keys after a clear the same way (a fixed
cost, not a leak). The start and end `?NVS` readings could not show it, because the run's own `?ERASE,NVS` wiped it.
Proposed: remove an inactive slot's keys, and an active slot's output keys past its count, in the same save (an
erase needs no free space, so it works on a full store); `loadSerialMonitorMappings` already treats a missing
`_act` as false.
*Fixed 2026-09-24 on Greg's decision (tracker #92), verified on the bench (20260924-233628):* the save writes an active slot only, then
`removeUnusedSerialMapKeys` (`WCB_Storage.cpp`) erases every key no active mapping uses (all of an inactive slot's,
`_act` first; an active slot's output pairs past its count) with raw `nvs_erase_key`, so a cleared mapping costs
nothing and a board carrying old leftovers sheds them on its next mapping save. `?backup` is built from RAM and
unchanged. Test: `map.nvs_keys_freed` (a 10-destination mapping takes 26 `serial_map` entries and its clear frees
26; `CLEAR,ALL` frees two mappings' 34).

**F21 — a client that keeps re-arming a remote terminal blocks a deferred reboot forever.** A queued `?reboot`
waits for 4 s with no command processed (`PWM_REBOOT_QUIET_MS`), and every queue item counts, including
`?RTERM,START` re-arms and NaviCore's store-only `?STATS,RPT` (every 30 s). With Intellex connected to NaviCore's
AP, W1 received `?RTERM,START,20` about once a second, and neither `?reboot` nor a PWM reboot ever happened
(`20260924-213158`: `input.wdpda_persists_reboot`, `pwm.wdp_selfheal_missed_clear`). Proposed: let an idempotent re-arm
of the same relay and a store-only report not reset the quiet window, and cap the deferral (restart anyway after,
say, 15 s). Separately, why Intellex re-arms every second is a question for Intellex.
*Fixed 2026-09-24 on Greg's decision (tracker #93), verified on the bench (20260924-233628):* a handler sets `quietWindowExempt`
(`WCB.ino`) for a `?STATS,RPT` and for a `?RTERM,START` naming the relay of the session already running, and `loop()`
then leaves the quiet-window clock alone. `RESTART_MAX_DEFER_MS` restarts anyway 20 s after the request, with an
ungated `Restart held off <n> s by commands that kept arriving - restarting anyway` line. 20 s, not 15: the
commands that ask for a restart come last in a Wizard push and in the `?backup` chain, so at most 4 mappings follow
the first one, each within 4 s of the one before and taking up to ~0.6 s to run - under 18.4 s - and a relay push is
one MGMT session drained in one `loop()` pass. `hil.wcb`'s `?reboot` wait is 25 s to match. Tests:
`etm.reboot_rterm_rearm` (W2 re-arms W1's terminal every second; W1 restarts within 10 s), `etm.reboot_stats_rpt`
(the same with a report a second) and `etm.reboot_defer_cap` (a command a second; the restart comes at the cap).

Found by the no-servo full run `20260924-234056` (see §5):

**F22 — a WCB that has just booted treats every peer as offline until that peer's next packet.** Nothing answers
a boot announce (the heartbeat branch of `espNowReceiveCallback`, `WCB.ino`), so a peer is marked online only by its
next heartbeat, advert or command: up to HB+1 s (11 s at the default HB 10). Meanwhile `etmAddToPendingTable`
skips it (`WCB.ino`, `if (!boardTable[b].online) continue;`), so a unicast to it goes out once with no ETM retry
(the packet itself is still sent); `?ETM,CHAR` aborts with no online peer; `?STATS` and `?config` show it offline.
Capability routing already honours a pin across the window (`wcbPeerEverSeen`). Pre-existing; F13, F20 and F21 do
not touch it. `etm.char_unicast` ran `?ETM,CHAR` 1.6 s after W1's reboot and aborted. Possible fix: a board that
hears a peer's first boot announce pulls its own next heartbeat forward by a random few hundred ms. Not a boot-time
WDP SOLICIT: an older WCB_Client decodes one as an empty advert and blanks the sender (`WDP_DESIGN.md`).
Recommendation: leave it. ESP-NOW still retries a unicast at the MAC layer, and the window is one heartbeat long.
Revisit if commands sent right after a reboot go missing.

## 4. Coverage gaps

*Status 2026-09-24: closed by the §5 additions and the §7 suites (s24-s29, s31). `?ERASE,NVS` / `?WCB_ERASE`,
the factory defaults and the WebSocket endpoint have attended tests that pass; only `?EPASS`'s
(`ident.epass_live`) has not run yet.
The lists below are the review's inventory.*

Baseline: `HIL_TESTING.md` §7's 2026-09-15 audit (1,064 functions; 484 tested, 198 partly, 343 untested but
testable, 39 not testable here) and its list of the largest gaps. Two fresh mechanical diffs on 2026-09-23:

- **Help text** (`WCB_Help.cpp`): of 177 `?VERB` / `?VERB,SUB` keys advertised, the suites mention 126.
- **Dispatcher** (`startsWith("…")` / `== "…"` literals in `WCB.ino`, `WCB_*.cpp`, `command_timer.cpp`): of 235
  command literals, 37 appear in no suite or Playwright spec.

After removing the examples the help embeds (`?ETMHB10`, `?S157600`, `?WCB2`, `?HW31` …) and the internal ETM
messages (`ETMLOAD`, `ETMCHAR_P1-3`, which are mesh payloads, not commands), the untested surface is:

**Features with no test at all**

- **`?WIFI` (`?WIFI,AP`, `?WIFI,JOIN`, `?WIFI,OFF`, the status form) and the WebSocket endpoint** —
  `docs/WIFI_DESIGN.md`, `WCB_WS.cpp`. Needs the PC's WiFi adapter joining the WCB's AP; a first test could be
  `?WIFI` status only, then `?WIFI,AP` + a `?WIFI,OFF` restore with a ping from the PC.
- **`?TRACK_ALL_ON` / `?TRACK_ALL_OFF` / `?TRACK_STATUS`** (`WCB.ino:6279-6285`) — nothing exercises them.
- **`?WDP,CLEAR`** — sent only by `wdp.clear_learned_relearn` (s27), which re-adds the learned peers it drops; elsewhere never, on purpose (`wdp.list_detail_errors` notes it forgets every learned peer). It can
  be tested with auto-join off, then `?WDP,POLL` to relearn the floor peers; W1's persisted learned peers 6 and 9
  would be lost, so only after Greg decides they can be re-added (`?WDP,ADD,6` / `?WDP,ADD,9`).
- **WDP PWMTARGET self-heal** (A17).
- **HCR word aliases**: the emotion words `HAPPY` / `SAD` / `MODERATE` / `SCARED` for `H` / `S` / `M` / `C`
  (`WCB_HCR.cpp:353-356`) and the stop style `;H,STOP,GRACEFUL` / `G` / `SOFT` (`:384`, `:498`). The suites use only
  the letter forms and a bare `;H,STOP`. One step per alias in `hcr.verbs_emote_raw` closes this.

**Legacy spellings with no test** (the canonical forms are tested; a single characterisation test per family,
"legacy spelling reaches the same handler", as `map.legacy_sm` and `etm.legacy_forms` already do, would close most
of this cheaply):

- Debug toggles: `?DON` / `?DOFF`, `?DMON` / `?DMOFF`, `?DKON` / `?DKOFF`, `?DPWMON` / `?DPWMOFF`, `?DETMON` /
  `?DETMOFF`, `?DHCRON` / `?DHCROFF`.
- Config: `?KYBER_LOCAL` / `?KYBER_REMOTE` / `?KYBER_CLEAR`, `?KYBER,ON` = `?MAESTRO,ON` (`WCB.ino:5323`, not in the
  help), `?PMS<n>,dest` / `?PRS<n>` / `?PLIST` / `?PCLEAR` (legacy PWM), `?SLS<n>,label` / `?SLC<n>` (legacy label),
  `?BAUDS<n>,rate`, `?SMCLEAR`, `?ETMOFF`, `?ETMCHAR`, `?ETMCHARCOUNT`.
- Destructive: `?RESET_BROADCAST` (now `bcast.reset_legacy`, the baseline flags replayed after it); `?WCB_ERASE` and
  `?ERASE,NVS` are left alone (attended-only, §7 WP4).

**Known untested by design at the review (closed since by §7, except `?EPASS` and `?ERASE,NVS`):** identity and radio settings (`?WCB`, `?WCBCH`, `?MAC`
beyond the octet-3 flip, `?EPASS`, `?HW`); `?WLED` configuration (local and remote add, LIST, STATUS, CLEAR,
validation — only `;L` output and the two WLED-port arms in `kyber.local_free_port_takes_pwm` are tested);
boot-time NVS restore per subsystem and factory defaults (only labels, ETM settings, mappings, variables and PWM are
checked across a reboot); the RC telemetry relay to USB beyond what `bridged_json_ack` and `btn_single_tap` see.

**Wizard (`tests/wizard`):** covered are page load, one pull, one label push, one terminal line, the Kyber generator
rules, `autoComputeKyberTargets` with two boards (WP8), and the parser round trip including the device cards, sequences
and variables (WP8). Not covered: a multi-board push, the flasher, the sequence/variable editors and the
HCR/MP3/DFP/WLED cards as UI (their parser side is), importing a `?backup` pasted from a mesh-only board, and the
shared-port hub (`serial-hub.js`).

## 5. Fix log

Applied 2026-09-23 (evening), after the review. "bench" = verified by the targeted run named in the revision log;
"unverified" = compiles, `run.py --list` registers it and `selftest.py` passes, but no board has run it yet.

| Finding | Status | Where |
|---|---|---|
| A1 | bench 20260923-234530 | `config_guard` auto-restore: `AUTO_RESTORE`, `NO_AUTO_RESTORE_WHILE`, `_undo`, `_auto_restore` in `suites/common.py`, covered by `selftest.py` `t_config_guard_auto_restore`; `finally` restores in `persist.label`, `label_remote`, `baud_live`, `label_reboot`, `seq.local`, `seq.timer`, ten `input.*` tests and `maestro.config_negatives`. |
| A2 | applied | `PROBE_MIN_VERSION = 5`, `PROBE_BAD_VERSIONS = ("6",)`, `require_version` refuses 6 (`hil/probe.py`); `HIL_TESTING.md` §3. |
| A5, A6, A7 | bench 20260923-234530 | `probe.links` title; `wcb.help_trap` asserts the `Usage: ?LABEL` line; `mesh.unicast_acked` asserts exactly +1 sent and +1 ACKed. |
| A8, A22 | bench 20260923-234530 | `maestro.remote_readback` and `client_mesh.sendraw_real_maestro_tap` read the servo's rest position first and put it back in `finally`. |
| A9 | bench 20260923-234530 | positive controls in `maestro.bad_verb` and `wled.bad_verb`. |
| A10 | bench 20260923-234530 | `input.soft_rx_baud_sweep` asserts 20/20 at 19200, 38400 and 57600; restores S3 from the baseline token. |
| A11, A12, A13 | bench 20260923-234530 | `require_tokens` preconditions in `input.bcast_parsing`, `bcast_dfp_port_excluded`, `softserial_tx_rmt`. |
| A14, A15 | bench 20260923-234530 | `map.text_multi_dest` asserts the write order; four `map.raw_*` / `baud_on_raw_mapped_port` restore the baseline `?BAUD` token. |
| A16, A18, A19 | bench 20260923-234530 | `pwm.map_deferred_reboot` window 3.5-7 s; s14 header; the one-element loop in `hcr.status_reply_parse`. |
| A17 | bench 20260924-003531 | No probe verb needed: `pwm.wdp_selfheal_missed_clear` deafens W2 (its `?MAC,3` filter) while W1 sends the explicit clear, restores it, and W1's next advert makes W2 clear the auto-configured output live. |
| A20 | open (nit) | Probe firmware literal `wcb_probe-2` (`probe_main.cpp:1031`); no harness change. |
| A21 | applied | `wdp.da_announce_propagates` (session `wcb-ce`), `navicore.plain_broadcast_both_ways`, `ota.relay_full_same_image_wcb2` declarations. |
| A23 | applied | `sbus.btn_single_tap` and `navicore.set_mode` titles say what is asserted. |
| F1 | verified 2026-09-24, bench 20260924-092423 (tracker #81) | Guards raised to 57600 (MP3 block removed); help, wiki, Wizard pickers follow; `hcr.config_rejects`, `mp3.config_rejects`, `wled.config_rejects` updated; new `devices.soft_ports_fast_baud`, `wled.soft_port_57600`. |
| F2 | verified 2026-09-24, bench 20260924-092423 (tracker #82) | `WCB_Help.cpp:100` reads `?PMSx,dest`; `pwm.legacy_forms` pins both spellings and checks the `?MAP?` help page. |
| §4 | closed except `?EPASS` (attended, not yet run); the `?ERASE,NVS`/`?WCB_ERASE` and `wifi.pc_joins_ap_ws` attended tests have since passed (§6) | New tests `persist.legacy_label_baud`, `pwm.legacy_forms`, `chars.legacy_debug_toggles`, `misc.track_legacy` (since replaced by `misc.track_removed`, F5), `wifi.status`; extended `hcr.verbs_emote_raw` (word emotions, NOW/GRACEFUL), `hcr.bad_args`, `map.legacy_sm` (`?SMCLEAR`), `etm.legacy_forms` (`?ETMOFF`), `kyber.config_negatives` (`?KYBER_LOCAL,S6`). `wifi.status` bench 20260924-003531. `?WDP,CLEAR`, `?PCLEAR`/`?POS`, `?KYBER_REMOTE`/`_CLEAR`, `?RESET_BROADCAST` and the Wizard gaps: the WP rows below. Still untested: `?WIFI,AP`/`JOIN` mode changes, `?WCB_ERASE`. |
| WP1 | bench 20260924-003531 | `s24_wled_config.py`, 7 tests: LIST/STATUS formats, config rejects, a local add, move and clear on W1 with its flags and label, the legacy `?WLED,PORT` form, the remote proxy route, WDP auto-add. |
| WP2 | bench 20260924-004307 + 20260924-005924 | `s26_boot_restore.py`, 8 tests: HCR/MP3/DFP restore on W2, a local WLED and a Maestro proxy at boot on W1, `?ALIAS`, `?BCAST` flags, `?DELIM`, `?WDP,OFF` and `?WCBQ` across a W1 reboot. `boot.wled_local_restore` first errored on a helper mismatch (s15's `_relabel` wants a Console; s24's takes a WCB): fixed, pass. |
| WP3 | bench 20260924-005924 + 20260924-011003 | `ident.wcb_number_reboot` and `ident.mac_octet2_live` pass first time. `ident.channel_reboot` taught two bench facts: one channel away is not off-mesh at bench range (W2 on channel 1 decoded W1 on channel 2 and W1 heard the ACKs), so the test moves five channels away; there the send fails at the MAC layer (`[SEND CB] MAC-layer FAILED`, `WCB.ino:5052`) and the ETM entry resolves at once with no retry line, which is what it now asserts. `ident.hw_setter` found F3 and asserts the running version instead. `?EPASS` attended-only. |
| WP4 | bench 20260924-005924 | `bcast.reset_legacy`, `wdp.clear_learned_relearn` (learned peers enumerated by asking `;W<n>` above the floor, `?PEERSLIVE` counts, `?WDP,ADD` restores), `pwm.legacy_pos_pclear`, `kyber.legacy_clear_remote`: all pass first time. `?ERASE,NVS`/`?WCB_ERASE` attended-only. |
| WP5, WP6 | bench 20260924-003531 | `s25_mesh_side_effects.py`: `pwm.wdp_selfheal_missed_clear` (A17 closed), `navicore.rc_relay_window`. |
| WP8 | unit 16/16; Playwright pass (headless, no board) | `tests/wizard/unit/devices.test.js`, 8 tests: the MP3/HCR/DFP host-and-route axes, WLED slots and proxies, Maestro slots and targeted clears, sequences (commas and `^` kept), variables, an all-devices round trip. `diffConfigs` now compares `dfp` like `mp3`/`hcr` (test support: nothing in `app.js` calls it). `wizard.kyber_auto_targets` in `kyber.spec.js`, registered in `s30_wizard.py`; F4 found. |
| F3 | verified 2026-09-24, bench 20260924-092423 (tracker #83) | `saveHWversion` sets the running value (pin map still boot-only); `ident.hw_setter` asserts the chain reports the saved version at once. |
| F4 | fixed 2026-09-24 (Playwright 3/3) | `autoComputeKyberTargets` keeps a remembered target unless its board reported a live list; `wizard.kyber_auto_targets` covers the shared-id case and a stale remembered target on a live board. |
| WP7 | bench 20260924-024317 (`wifi.join_w2_ap`), bench 20260924-024515 (`wifi.off_and_back`); `wifi.pc_joins_ap_ws` attended, since passed (20260924-112730) | `s28_wifi.py` (opt-in `wifi_modes`): W1's AP off and back, W1 joining W2's AP and returning, the mesh delivering throughout, four W1 reboots; `wifi.pc_joins_ap_ws` (opt-in `wifi_pc`): the PC joins W1's AP through a temporary Windows profile, `hil/ws.py` opens the WebSocket endpoint and gets `?VERSION` answered, the PC returns to its network. This PC's internet is that adapter, so the last one needs someone at the keyboard. |
| WP10 | bench 20260924-074119 + 20260924-074259 | `s29_leftovers.py`, 12 tests, all pass on their first run: `misc.track_command` (since replaced by `misc.track_removed`, F5), `maestro.legacy_no_comma`, `ident.legacy_mac_short`, `etm.char_legacy`, `wcb.help_forms`, `persist.baud_label_validation`, `serial.timer_cap_negative`, `mesh.timer_chain`, `mesh.mgmt_errors`, `mesh.rterm_errors`, `boot.banner_w1`, `boot.banner_w2`. The banner tests build every expected line from the board's own chain. F5-F7 found. |
| F5 | fixed 2026-09-24 (tracker #85), bench 20260924-120036 | `?TRACK` and its old spellings removed; `misc.track_removed`. |
| F6 | fixed 2026-09-24 (tracker #86), bench 20260924-120036 | A negative `;T` delay refuses the chain; the cap is always reported. |
| F7 | fixed 2026-09-24 (tracker #87), bench 20260924-120036 | `?LABEL` / `?SLCS` name a port outside 1-5. |
| WP11 | `backup.replay_idempotent` bench 20260924-075445; `nvs.erase_defaults_restore` and `nvs.wcb_erase_alias` since passed (20260924-120036); `ident.epass_live` attended, not yet run | `s31_password_erase.py`: `backup.replay_idempotent` proves the erase tests' restore path without erasing (W1's chain replayed one line at a time is accepted and changes nothing); `ident.epass_live` (opt-in `mesh_password`), `nvs.erase_defaults_restore` and `nvs.wcb_erase_alias` (opt-in `nvs_erase`) are for Greg to run. F8 found. |
| F8 | fixed 2026-09-24 (tracker #88), bench 20260924-120036 | The erase clears every namespace; the erase tests check WiFi is off and no serial device is saved. |
| Run 20260924-092602 | 459 pass, 6 fail, 8 skip in 1:45:28 (servos on; `wifi_pc`, `nvs_erase` ticked) | All six triaged and re-run on the bench. `var.cap_persistent_full`, `inv.seqget_largest`, `seq.cycle_guard_reuse`: W1's NVS was full (F10; the empty sequence is F9); they pass once W1 is wiped (`20260924-112513`). `input.wdpda_port_full`: W1 missed the second of two announces on its S4 soft port, a lost line; the record checks pass on the re-run, which then skips on S4's label. `sbus.signal_loss_controller_reset`: asserted on NaviCore's fps reaching 0 and on a poll after recovery, both timing-dependent (fps is a one-second average; a stray frame can hold it at 1; the controller can be back within a second); it now checks the longest window on one frame count (ageMs rising past a second, fps at most half) and reads the post-reset boot log twice; 4 of 4 pass (`20260924-113029` to `20260924-113147`). `wifi.pc_joins_ap_ws`: this PC has two WiFi adapters, and netsh refuses a connect that names none; the test now uses the adapter that does not carry the internet (the TP-Link "Wi-Fi 2") under a temporary `HIL-<ssid>` profile, and passes (`20260924-112730`); the stray `WCB1` profile the old version left on "Wi-Fi" was deleted. `nvs.*`, `backup.replay_idempotent` passed in the run. |
| F9 | fixed 2026-09-24 (tracker #84), bench 20260924-120958 | Both `key_list` writes checked; `seq.nvs_full_consistency` (opt-in `nvs_fill`). |
| F10 | fixed 2026-09-24 (tracker #89), bench 20260924-120036 | `?NVS`; the harness records it per run (report.md, `results/nvs_history.csv`); `wcb.nvs_report`. |
| F11 | deferred 2026-09-24 (Greg) | `WiFi.persistent(false)` would drop the WiFi driver's duplicate settings (36 entries, about 1 KB, on an AP-mode board); left as it is while NVS has room, watched through `results/nvs_history.csv`. |
| F12 | fixed 2026-09-24 (tracker #90), bench 20260924-133332 | `?backup` streams its chains through a 2 KB per-line write buffer with a running CRC; the mesh pull measures, reserves once, never sends a partial config, and says on the target when it is too large. `wcb.backup_large_config`, `wcb.pull_size_limit`. |
| F13 | fixed 2026-09-24 (tracker #91), bench 20260924-190431 | Parts for `,P` pulls over 2912 characters; coded CFGERR replies; non-blocking target job; one-write relay lines; per-relay Wizard queue. [MGMT_RELAY.md](MGMT_RELAY.md). |
| F14 | open (Wizard decision) | Check the relay push's size before the confirm and bootstrap, then split pushes over 2864 characters. |
| F15 | open (firmware) | strncmp for the remaining mgmt password checks. |
| F16 | open (NaviCore) | Drop, don't store, when `WsSink::pump()` cannot drain. |
| F17 | open (Greg) | Put that WCBClient example back on the shared placeholder credentials (details kept out of this doc). |
| F18 | open (firmware) | Queue the WiFi-task `came ONLINE` print for `loop()`. |
| F19 | open (firmware) | Make `collectConfigCommands` report a failed append or NVS read. |
| F20 | fixed 2026-09-24 (tracker #92), bench 20260924-233628 | The save removes every `serial_map` key no active mapping uses; `map.nvs_keys_freed`. |
| F21 | fixed 2026-09-24 (tracker #93), bench 20260924-233628 | RTERM re-arms of the running session and `?STATS,RPT` do not reset the quiet window; `RESTART_MAX_DEFER_MS` = 20 s; `etm.reboot_rterm_rearm`, `etm.reboot_stats_rpt`, `etm.reboot_defer_cap`. |
| F22 | open (firmware; recommend leave) | A rebooted WCB sees no peer online until its next packet (up to HB+1 s); unicasts meanwhile are not ETM-retried. |
| Run 20260924-234056 | 459 pass, 3 fail, 37 skip (27 servo, 10 opt-in/attended/label) with `--no-servos` (F20/F21 image) | None is F13, F20 or F21 (one analyst per failure plus a skeptic, both reading the session log and the code). `etm.offline_detection_timing` measured 4.93 s against a 4.95 s floor. It was host timing: a line is stamped when SerialDevice splits it out of a read, so one with more output behind it in the same read (here NaviCore's `rc_hb`) is stamped up to one read late. The floor is now 4.85 s, and the test checks the edges alternate, which is #80's real fingerprint. Replayed over all 31 recorded runs, that fails every run the race hit (5 of which the old test passed) and passes every run since the #80 fix. `wizard.remote_pull`: W1's third boot announce after Chrome returned COM6 printed `[ETM] WCB1 came ONLINE (boot)` on W2 between the factory header and its chain (F18, a window F12 opened), and the parser took the line after the header; it now searches the section, and `_factory_reply` re-reads a chain that fails its CRC. `etm.char_unicast`: `etm.reboot_defer_cap` had just rebooted W1, which saw no peer online yet (F22) and aborted; `_char` now polls until every bench WCB is online and fails at once on an abort, and the reboot tests leave W1 seeing its peers. Rerun `20260925-013055` (unchanged tests, different order) passed all three; the fixed tests in `20260925-015720`: 21/21. |
| Run 20260924-190733 | 478 pass, 8 fail, 9 skip in 1:47:50 (F13 image, servos on) | None is F13 (triaged by one analyst per failure plus a skeptic). Four came from W1's NVS: the map suite's leftover keys (F20) filled it for `var.cap_persistent_full` and `inv.seqget_largest`, and a WDP-DA save scheduled 1 s after the previous test's forget held off soft-serial RX mid-line for the two `input.wdpda_*` tests (the forget helpers now wait it out). `input.soft_rx_baud_sweep` hit 57600's ~1% loss (3 of the last 19 runs; now a floor of 18/20). `wdp.poll_refreshes_age` lost one unacknowledged broadcast (now retried once). `pwm.wdp_selfheal_missed_clear` raced its own mark (reordered). `etm.w1_reboot_sees_peers` missed a restart deferred past 10 s (now 20 s; see F21). Reruns `20260924-205621` and `20260924-213158`: all pass except the two reboot waits blocked by Intellex (F21); with Intellex closed, `20260924-223915` passes those too (3/3). |
| Run 20260924-131417 | 103 pass, 1 fail, 19 errors in 14:28 (123 tests of the first F12 build) | The 19 errors: probe2's COM11 was held by `Intellex.exe` (Greg's app, running since 2026-09-22), stopped with Greg's OK. The fail, `persist.bcast_flags_reboot`: a peer's `[ETM] WCBn came ONLINE`, printed on the WiFi task right after W1's reboot, landed between two of that build's 256-byte writes and split the configured chain, so its CRC failed. The writer now buffers 2 KB and flushes per line; everything re-ran in 20260924-133332. |

## 6. Resume here

- **Done:** the review (all 28 suites); the §5 fixes; every §7 work package that can run here, as the suites
  `s24`-`s29` and `s31`, `tests/wizard/unit/devices.test.js` and `wizard.kyber_auto_targets`; F1-F10, F12, F13, F20
  and F21 fixed on Greg's decisions. 499 tests registered; `selftest.py` 47/47; Wizard unit tests 36/36; host tests
  `wdp_wire_test` and `config_parts_test` pass; every no-board Playwright spec passes.
- **Verified on the bench:** the runs in §5; full run `20260924-092602` with servos (459 pass, 6 fail, 8 skip, every
  failure triaged in §5); the F5-F10 runs `20260924-120036` and `20260924-120958`; and the F12 runs `20260924-131417`
  and `20260924-133332`; the F13 runs `20260924-181517` and `20260924-190431` (27/27); the full run `20260924-190733` on the
  F13 image (478 pass, 8 fail, none F13, triaged in §5) and its reruns `20260924-205621` and `20260924-213158`; the
  F20/F21 run `20260924-233628` (43/43, with `--no-servos`); the no-servo full run `20260924-234056` (459 pass,
  3 fail, all test-side, triaged in §5), its rerun `20260925-013055` and the fixed tests in `20260925-015720` (21/21).
- **Flashed:** W1 and W2 run the image in `tests/hil/results/builds/wcb-esp32-meshq` (`6.2.1_232316RSEP2026` + F1-F10,
  F12, F13, F20 and F21, built 23:32 and flashed 23:36 on 2026-09-24; the CI release of that name is a different
  image, see the folder's FLASHED.md). Tracker #81-#93 are VERIFIED. NaviCore (COM5) still runs the WCBClient library from
  before F13; the updated library compiles in NaviCore and the MgmtRelay example but has not been flashed.
- **COM11:** `Intellex.exe` held probe2's port from some time after 09:26 on 2026-09-24 until Greg had it stopped at
  13:28; every test that needs probe2 errored meanwhile (`20260924-120036`, `20260924-131417`) and re-ran in `20260924-133332`. If
  a probe port says "Access is denied", look for another program first.
- **Attended:** `wifi.pc_joins_ap_ws` and both `nvs.*` tests pass (the erase tests again on the F8 image);
  `ident.epass_live` (opt-in `mesh_password`) has not run yet.
- **Not covered:** the Wizard beyond its parser (the flasher, a multi-board push, the editor screens, the shared-port
  hub), and what the bench lacks (§7 WP9).
- **Open decisions:** F14-F19 (found during F13: relay push cap, String password checks, NaviCore WsSink, the
  WCBClient example's credentials, the WiFi-task came-ONLINE print, silent config-line loss); F22 (a rebooted WCB
  sees its peers offline for up to one heartbeat; recommend leave); A20 (probe literal,
  nit). F11 is deferred on Greg's word: revisit it if `results/nvs_history.csv` shows NVS filling.
- **Everything is uncommitted.** Greg commits his own work.
- **Bench:** free when this was written; `ListAgents` first.

## 7. Full-coverage plan (2026-09-24)

Greg: "I want it all to be tested." Work packages, in the order they are done. Each is verified on the bench before
the next starts; a row's status is `todo` / `written` / `bench <run>` / `blocked <why>`. No test here moves a servo.

| WP | Gap | Approach | Status |
|---|---|---|---|
| WP1 | `?WLED` configuration | New suite `s24_wled_config.py`: local add on a free W1 port, remote proxy, LIST / STATUS / CLEAR forms, validation (id, port, occupied port, soft-serial baud), `;L` bytes on the local port, port flags and label around add/clear, WDP off around a local add so W2 never persists a proxy. | bench 20260924-003531 (7 tests, `s24_wled_config.py`) |
| WP2 | Boot-time restore per subsystem | `backup.roundtrip_all_three` gains a W2 reboot between configure and verify; new `persist.alias_reboot`, `persist.bcast_flags_reboot`, `chars.delim_reboot`, `wdp.off_reboot`, `peers.wcbq_reboot` (all restore, USB consoles on both boards). | bench 20260924-004307 + 20260924-005924 (`s26_boot_restore.py`, 8 tests pass) |
| WP3 | Identity and radio setters | `ident.*`: `?WCB` (W1 → 3 and back, two reboots, WDP off so W2 learns nothing, W2's WDP checked and `?WDP,FORGET,3` in `finally`), `?WCBCH` (W1 off-channel and back, W2 unreachable meanwhile), `?MAC,2` (live receive filter, like `_deaf_w1`), `?EPASS` (wrong password: deaf and mute, restored from the chain token, never printed), `?HW` (setter and chain token only, no reboot: the pin map applies at boot). | bench 20260924-005924 + 20260924-011003 (`s27_identity_destructive.py`: `ident.wcb_number_reboot`, `channel_reboot`, `mac_octet2_live`, `hw_setter`, all pass; F3); `?EPASS` stays attended-only |
| WP4 | Destructive commands | Opt-in `nvs_erase` (attended): `?ERASE,NVS` on W1, defaults checked at boot, the factory chain replayed with `?CHK`, learned peers re-added from the pre-erase `?WDP,DUMP`, WDP-DA records noted as lost; `?RESET_BROADCAST` (flags replayed from the baseline); `?WDP,CLEAR` (auto-join off, POLL relearns the floor, learned peers re-added); `?PCLEAR`/`?POS` (legacy PWM output + clear-all, two deferred reboots); `?KYBER_CLEAR`/`?KYBER_REMOTE` (legacy, WDP off, reboot after). `?WCB_ERASE` is checked as the alias of `?ERASE,NVS` by its usage text only. | bench 20260924-005924 (`bcast.reset_legacy`, `wdp.clear_learned_relearn`, `pwm.legacy_pos_pclear`, `kyber.legacy_clear_remote` in s27, all pass); `?ERASE,NVS` stays attended-only |
| WP5 | WDP PWM self-heal (A17) | No probe change: deafen W2 (`?MAC,3` flip on its own console) while W1 clears the mapping, so W2 misses the explicit clear; restore W2's octet; W1's next advert without the PWMTARGET must make W2 clear the auto-configured output without a reboot. Needs the trigger confirmed in `WCB_PWM.cpp` / `WCB_WDP.cpp` first. | bench 20260924-003531 (`pwm.wdp_selfheal_missed_clear`, `s25_mesh_side_effects.py`): A17 closed |
| WP6 | RC telemetry relay to USB | `navicore.rc_relay_window`: a `;W20,{json}` opens W1's 20 s window; `rc_hb`/`rc_ch` `{"sys":1` lines arrive on W1 USB at ~5 Hz; none after the window closes. | bench 20260924-003531 (`navicore.rc_relay_window`) |
| WP7 | WiFi AP / JOIN and the WebSocket endpoint | `s28_wifi.py`: opt-in `wifi_modes` (`wifi.off_and_back`, `wifi.join_w2_ap`: W1 joins W2's AP, no PC adapter needed) and opt-in `wifi_pc` (`wifi.pc_joins_ap_ws`, attended: the PC joins W1's AP through a temporary Windows profile and `hil/ws.py` drives the WebSocket endpoint). Credentials are read from the boards' chains at run time and never written to a note, message or lasting file. | bench 20260924-024317 (`wifi.join_w2_ap`), bench 20260924-024515 (`wifi.off_and_back`); `wifi.pc_joins_ap_ws` passes (attended, 20260924-112730) |
| WP8 | Wizard | Parser unit tests for the device cards, sequences and variables (`buildCommandString` diffs); a no-board Playwright test for `autoComputeKyberTargets` with two board configs. The flasher and the shared-port hub stay manual (they need a second authorised board in Chrome and a real flash). | done: `tests/wizard/unit/devices.test.js` (8 tests; 16/16 with `parser.test.js`), `wizard.kyber_auto_targets` (no board, passes headless); F4 found |
| WP9 | Not testable here | A real Kyber brain, a third WCB, an ESP32-S3 WCB, the status LED, fault injection. Listed so nobody looks for them. | blocked: hardware |
| WP10 | Coverage-scan leftovers | A fresh scan of help keys, dispatcher roots and legacy spellings against the tests (2026-09-24) left `?TRACK`, `?MAESTROM…`, `?M3xx`, `?ETMCHAR` and `?HELP`; the testing doc's gap list added the timer cap and a `;T` chain over the mesh, `?BAUD` / `?LABEL` validation, the `?MGMT` / `?RTERM` error replies and the boot banner. All in `s29_leftovers.py`. | bench 20260924-074119 + 20260924-074259 (12 tests pass); F5-F7 found |
| WP11 | The mesh password and the NVS erase | `s31_password_erase.py`: the erase tests restore W1 from its own chain, one line at a time, after `?HW` and a boot; `backup.replay_idempotent` proves that path on a live board first. Credentials are read from the chain at run time and only ever handed back to W1. | written; `backup.replay_idempotent` bench 20260924-075445; `nvs.erase_defaults_restore`, `nvs.wcb_erase_alias` pass (20260924-120036); `ident.epass_live` attended, not yet run; F8 found |

## Revision log

| Date | Commit | Change |
|---|---|---|
| 2026-09-23 | _(pending)_ | Created: method, progress table, empty findings and coverage sections. |
| 2026-09-23 | _(pending)_ | Harness core and suites s00-s18, s30, s99 reviewed: A1-A21, F1; help-text coverage diff in §4. |
| 2026-09-23 | _(pending)_ | Bench run `20260923-234530`: 43 pass / 1 fail / 1 skip; A6 assert corrected and re-run (pass); `wifi.status_off` generalised to `wifi.status` (W1 runs an AP here); auto-restore exercised live on W1 and it restored. §5 rows flipped to "bench". |
| 2026-09-23 | _(pending)_ | Fixes applied for A1, A2, A5-A16, A18, A19, A21-A23 and the cheap §4 gaps (§5 fix log); F2 found. `selftest.py` 40/40 with the new auto-restore check; 427 tests registered. Targeted bench run started; results pending. |
| 2026-09-23 | _(pending)_ | A21, first item: `wdp.da_announce_propagates` now declares `links=["W2S3|W2S4|W2S5"]` (session `wcb-ce`, uncommitted; verified at `s18_etm_config_wdp.py:1509`). The s21 and s20 items of A21 stay open. |
| 2026-09-23 | _(pending)_ | Suites s19-s23 reviewed (A22, A23); A17 and A20 verified against `WCB_PWM.cpp:905` and `probe_main.cpp:1031`; dispatcher coverage diff added to §4; overall verdict and the order of work in §5. `selftest.py` 39/39. Review complete. |
| 2026-09-24 | _(pending)_ | Full-coverage plan §7 (Greg: "I want it all to be tested"). WP1 `s24_wled_config.py` and WP5/WP6 `s25_mesh_side_effects.py` written and bench-verified (`20260924-003531`, 11 pass, `wifi.status` included); A17 closed without a probe change. |
| 2026-09-24 | _(pending)_ | WP2 `s26_boot_restore.py` (bench `20260924-004307`, 7 of 8; the error was a helper mismatch, fixed and passed in `20260924-005924`). WP3/WP4 `s27_identity_destructive.py` (bench `20260924-005924`, 6 of 8: F3 found by `ident.hw_setter`; `ident.channel_reboot` re-designed twice, five channels away instead of one, then the MAC-layer failure asserted instead of ETM retries; the re-runs `20260924-010523` and `20260924-011003` pass). WP8: `tests/wizard/unit/devices.test.js`, `wizard.kyber_auto_targets`, `diffConfigs` compares `dfp`; F4 found. 446 tests registered. §2, §3.3, §4, §5, §6, §7 updated. |
| 2026-09-24 | _(pending)_ | Greg's decisions on F1-F4 and WP7 applied. F1: HCR/WLED guards to 57600, MP3 block removed, help + wiki + Wizard pickers; F2: help text; F3: `saveHWversion` updates the running value; F4: the Kyber target fallback keeps a remembered target unless its board reported a live list. Firmware compiled (ESP32 69 %, S3 68 %) but not flashed (the session may not write the bench boards), so F1/F3's bench checks wait; tracker #81-#83 filed. WP7: `s28_wifi.py`, `hil/ws.py`, opt-ins `wifi_modes` / `wifi_pc`; `wifi.join_w2_ap` bench 20260924-024317 pass, `wifi.off_and_back` bench 20260924-024515 (its first run misread the Mode line, which shows the saved setting at once), `wifi.pc_joins_ap_ws` attended, not run. 451 tests; `selftest.py` 41/41. |
| 2026-09-24 | _(pending)_ | WP10: a fresh coverage scan (help keys, dispatcher roots, legacy spellings) plus the testing doc's own gap list became `s29_leftovers.py`, 12 tests, all passing on their first bench run (`20260924-074119`, `20260924-074259`), with no servo moved. Found F5 (`?TRACK` is inert), F6 (a negative `;T` delay waits 30 minutes) and F7 (an out-of-range `?LABEL` port gets no reply). `?SMR` turned out to be covered already by `map.legacy_sm`; factory defaults stay manual, since only an erased board shows them. 463 tests. |
| 2026-09-24 | _(pending)_ | WP11: `s31_password_erase.py`. `backup.replay_idempotent` (W1's chain replayed a line at a time changes nothing; bench `20260924-075445`) proves the restore path; `ident.epass_live` (opt-in `mesh_password`), `nvs.erase_defaults_restore` and `nvs.wcb_erase_alias` (opt-in `nvs_erase`) are attended and written for Greg to run. Found F8: the NVS erase keeps `wifi_cfg` and `wdp_da`, although its help says it erases all settings. 467 tests; `selftest.py` 41/41. |
| 2026-09-24 | _(pending)_ | The session's flash of W1/W2 was refused by the permission check twice, so the boards still run `6.2.1_231928RSEP2026` (run `20260924-090528`: the F1/F3 tests fail against it as expected). The new image now sits in `tests/hil/results/builds/wcb-esp32-meshq`, the folder the OTA tests stream as the running image; left elsewhere, a full run's OTA tests would have streamed the CI release of the same name and silently replaced the fixes. The old build is kept in `wcb-esp32-meshq-231928`. `devices.soft_ports_fast_baud` fixed (it called s15's `_in_order` with the wrong arguments); `pwm.legacy_forms` now also checks the `?MAP?` help page for F2. |
| 2026-09-24 | _(pending)_ | W1/W2 flashed with the F1-F3 image (`6.2.1_232316RSEP2026`); the seven dependent tests pass (`20260924-092423`), so F1-F3 are verified and tracker #81-#83 flipped. Full run `20260924-092602` started with servos and the `wifi_pc` / `nvs_erase` opt-ins. |
| 2026-09-24 | _(pending)_ | Full run `20260924-092602` (servos on): 459 pass, 6 fail, 8 skip. Triage in §5: three storage failures were a full NVS on W1 (F10) and pass after the erase test wiped it; the sequence clear leaves a ghost empty sequence when NVS is full (F9, tracker #84); one lost announce on a soft port; `sbus.signal_loss_controller_reset` and `wifi.pc_joins_ap_ws` fixed (timing-dependent checks; an unnamed adapter on a two-adapter PC) and re-run to pass. |
| 2026-09-24 | _(pending)_ | Greg's decisions on F5-F10 applied and verified on the bench (20260924-120036; F9 in 20260924-120958): `?TRACK` removed, a negative `;T` refuses its chain and the cap is always reported, `?LABEL` names a bad port, `?ERASE,NVS` clears every namespace, the sequence-list writes are checked, and `?NVS` reports storage use, which the harness now records per run. New tests `misc.track_removed`, `wcb.nvs_report`, `seq.nvs_full_consistency` (opt-in `nvs_fill`); 474 registered. Found F11 (from the first `?NVS` readings) and F12 (`?backup` chains lost on a tight heap, measured on W1). Tracker #84-#90. |
| 2026-09-24 | _(pending)_ | F12 fixed on Greg's decision and verified on the bench (20260924-133332): `?backup` streams its chains through a 2 KB per-line write buffer with a running CRC, and the mesh pull measures, reserves once and never sends a partial config; output byte-identical to before on W1 and W2. The first build's 256-byte writes let a came-ONLINE line split a chain (20260924-131417). New tests `wcb.backup_large_config`, `wcb.pull_size_limit`; `seq.nvs_full_consistency` now requires whole chains; 476 registered. Found F13. F11 deferred on Greg's word. COM11 was held by `Intellex.exe`. Tracker #90. |
| 2026-09-24 | _(pending)_ | F13 done on Greg's word: config pulls over 2912 characters in parts for `,P` requests, coded CFGERR replies, a non-blocking target job, one-write relay lines and a per-relay Wizard queue (MGMT_RELAY.md, tracker #91). Bench 20260924-190431: 27/27; W1/W2 flashed 19:04. Recorded F14-F19. 495 tests. |
| 2026-09-24 | _(pending)_ | Full run `20260924-190733` on the F13 image: 478 pass, 8 fail, 9 skip, none F13; triaged per failure with a skeptic each (§5). Five test fixes (57600 floor, WDP-DA forget waits out its save, poll retried once, self-heal race, 20 s reboot wait). Recorded F20 (serial-mapping NVS keys leak) and F21 (RTERM re-arms starve a deferred reboot). |
| 2026-09-24 | _(pending)_ | F20 and F21 fixed on Greg's decision (tracker #92, #93; §3): a serial-mapping save removes the keys no active mapping uses, and a deferred restart ignores RTERM re-arms of the running session and `?STATS,RPT`, with a 20 s cap (sized in §3, not the 15 s proposed). Four tests (`map.nvs_keys_freed`, `etm.reboot_rterm_rearm`, `etm.reboot_stats_rpt`, `etm.reboot_defer_cap`); both targets compile, not yet flashed. |
| 2026-09-24 | _(pending)_ | F20 and F21 verified on the bench (20260924-233628, 43/43), after three review fixes (`_act` last, keep a slot's output keys when its count was not stored, no ETM ACK once a restart is imminent). W1/W2 flashed 23:36. Harness: `--no-servos` (27 tests). |
| 2026-09-25 | _(pending)_ | No-servo full run `20260924-234056`: 459 pass, 3 fail, 37 skip, all three test-side (§5). Test fixes: the offline-timing floor is 4.85 s plus an edge-alternation check (#80's fingerprint); the `?backup` parser searches each section for its chain; `?ETM,CHAR` waits for its peers and fails fast on an abort. F18 gains the header window and the right ring for its fix; F22 recorded (recommend leave). Verified `20260925-015720` (21/21); `selftest.py` 47/47. |
