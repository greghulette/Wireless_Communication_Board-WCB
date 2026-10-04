# WCB HIL coverage re-scan: work plan WCB-WP12 to WCB-WP59

> Written 2026-09-27 by planning agents that read the code (and, for WCB, a verified coverage re-scan), for the
> away week's goal: a complete HIL tool for the WCBs, NaviCore and Intellex. It is a plan, not a spec: line numbers
> drift, and any claim is to be confirmed against the code before acting on it. Progress is tracked in
> [../HIL_TEST_AUDIT.md](../HIL_TEST_AUDIT.md) §6 and decisions in [../HIL_WEEK_DECISIONS.md](../HIL_WEEK_DECISIONS.md).

2026-09-27. Input: `scratchpad/wcb_rescan_gaps.json` (11 groups, 295 confirmed gap entries; the 5 refuted entries are
excluded). Code: HEAD `1f629a8`. Read-only: no port opened, nothing run.

**Line numbers.** The WP tables keep the scan's citations, which were taken at `c619dd6`. Commit `1f629a8` (tracker #94,
RMT PWM pulses) landed after the scan. It moved `WCB.ino` by -3 lines after :7949 and moved `WCB_PWM.cpp` by about
+80 lines after :155. §3 cites `1f629a8`, and every line there was re-checked by hand. For the full how, nearest test and
verifier notes of any row, look its id up in the JSON.

**Codes.** Mode: U = unattended; O = opt-in (bench.json key named); A = attended (per D7 these still run in the nightly
full runs when ticked). Effort: S = under half a day, M = about a day, L = several days, or reboot-heavy, or needs a
firmware fix first. **BUG-n** = the test is expected to fail until §3 item n is fixed. Write it as a `(should)` test.

---

## 1. How much of the WCB is not covered

The re-scan verified 295 gap entries against 1,941 checked behaviours (the two mechanical scans account for 644 of
those): 19 high, 98 medium and 178 low risk, of which 208 are untested and 87 partial. The two mechanical scans overlap the behaviour groups heavily. After merging
duplicates, **214 distinct behaviours** remain uncovered: **18 high, 75 medium, 121 low**. 177 are firmware
(9 H / 54 M / 114 L) and 37 are Wizard (9 H / 21 M / 7 L).

Testability of the 214:
- 176 can be tested unattended on today's bench.
- 25 need an opt-in or someone at the bench.
- 13 are blocked: 4 need a probe feature, 2 need hardware the bench lacks, and 7 need a firmware fault knob or are dead
  code.

The command surface itself is well pinned. What is missing falls into four areas:
- **Concurrency and ordering:** the loop-prevention race, restart versus config pull, and the boot load order.
- **Multi-actor mesh paths the bench never combines:** relay pushes, Wizard-origin FRAGs, permanent auto-join,
  controller-routed traffic, and daisy-chained Maestros.
- **Persistence:** non-default values across a reboot, and behaviour on a full NVS.
- **The Wizard's push side:** only a single one-label direct push is tested.

The scan also exposed **32 firmware findings, 3 high and 12 medium (§3). 26 are outright defects.** Among them:
- saved PWM mappings load before the board number;
- `?HW` accepts the other chip's pin map (a boot loop on a classic ESP32);
- a race drops typed broadcasts from the mesh;
- ETM characterisation phase 3 never loads the mesh.

---

## 2. Work packages

### Index (ordered: high-risk WPs first, then medium, then low; unattended before opt-in within a tier)

| WP | Suite / spec | Distinct gaps (entries) | Top risk | Mode | Effort |
|---|---|---|---|---|---|
| 12 | s31_password_erase.py (one-line restore) | 3 (5) | H | U | M |
| 13 | s05_mesh.py (?MGMT,FRAG target side) | 4 (8) | H | U | M |
| 14 | s19_client_mesh.py (origin under mesh traffic) | 5 (7) | H | U | M |
| 15 | s14_pwm.py (boot order, remap, guards, overrun) | 5 (6) | H | U | L |
| 16 | s27_identity_destructive.py (identity, learned peers) | 7 (10) | H | U | L |
| 17 | s28_wifi.py, no opt-in (?WIFI validation) | 4 (6) | H | U | S |
| 18 | s23_softrx_measure.py (level arms at restart) | 1 (2) | H | U | M |
| 19 | s18_etm_config_wdp.py (permanent auto-join) | 2 (4) | H | U | M |
| 20 | Wizard Playwright, no board (push side) | 6 (6) | H | U | L |
| 21 | Wizard Playwright on the bench (HIL bridge) | 6+2 halves | H/M | U (row 9 A once) | L |
| 22 | s28_wifi.py `wifi_pc` (WebSocket) | 6 (6) | H | A | M |
| 23 | Wizard destructive (erase, flash, OTA) | 3 (3) | H | A / O | M |
| 24 | s99_etm.py (ETM characterisation) | 4 (9) | M | U | M |
| 25 | s03_wcb.py (pull, restart gate, knobs) | 8 (17) | M | U | L |
| 26 | s15_hcr_mp3_dfp.py (routing, ownership, HCR) | 5 (8) | M | U | L |
| 27 | s22_maestro_kyber.py (Maestro/Kyber medium) | 3 (3) | M | U | L |
| 28 | s25_mesh_side_effects.py | 4 (7) | M | U | M |
| 29 | s04_serial.py (timers, ?STOP) | 4 (6) | M | U | M |
| 30 | s18_etm_config_wdp.py (ETM) | 9 (12) | M | U | L |
| 31 | s05_mesh.py (RTERM) | 3 (3) | M | U | M |
| 32 | s12_serial_input.py (soft TX table, line input) | 4 (4) | M | U | M |
| 33 | s16_vars_if.py ('all') | 1 (3) | M | U | S |
| 34 | s29_leftovers.py (legacy, labels, help) | 5 (10) | M | U | M |
| 35 | s18_etm_config_wdp.py (prefix characters) | 4 (7) | M | U | M |
| 36 | s26_boot_restore.py + s29 banner (boot timing) | 4 (4) | M | U | M |
| 37 | tests/hil/selftest.py (rule 12) | 1 (1) | M | U, no bench | S |
| 38 | s21_navicore_sbus.py (:MQR) | 1 (1) | M | U | S |
| 39 | s24_wled_config.py | 4 (4) | M | U | M |
| 40 | Wizard node unit tests (parser.js) | 10 (10) | M | U, no board | M |
| 41 | Wizard Playwright, no board (app.js logic, panels) | 12 (12) | M | U | L |
| 42 | s31 `nvs_fill` (full NVS) | 1 (4) | M | O | M |
| 43 | s31 `mesh_password` window | 2+2 halves | M | A | S |
| 44 | s17 `seq_wipe` (bookkeeping keys) | 2 (2) | M | O | S |
| 45 | s28 `wifi_modes` (JOIN robustness) | 2 (2) | M | O | M |
| 46 | new opt-in `etm_seq_wrap` | 1 (1) | M | O | M |
| 47 | s26_boot_restore.py (settings across reboot) | 3 (10) | L | U | L |
| 48 | s18_etm_config_wdp.py (WDP/controller/stats) | 9 (11) | L | U | M |
| 49 | s12_serial_input.py (WDP-DA) | 4 (4) | L | U | M |
| 50 | s13_serial_mapping.py | 4 (10) | L | U | M |
| 51 | s14_pwm.py leftovers | 7 (7) | L | U | M |
| 52 | s15_hcr_mp3_dfp.py leftovers | 7 (11) | L | U | M |
| 53 | s22_maestro_kyber.py leftovers | 9 (9) | L | U | L |
| 54 | s05_mesh.py (;W forms) | 1 (2) | L | U | S |
| 55 | s17_seq_inventory.py leftovers | 4 (4) | L | U | S |
| 56 | s20 `ota_erase` | 2 (3) | L | O | S |
| 57 | s31 `nvs_erase` | 2+1 half | L | A | S |
| 58 | attended characterisations | 3 (3) | L | A | M |
| 59 | **Blocked** | 13 (16) | M/L | B | per unblocker |

Totals: 214 distinct gaps, 295 entries, 48 WPs.

---

> **Wave 1 (2026-09-28): WCB-WP12, 13, 19, 25, 30, 31, 35 (rows 2-4), 42 (rows 2, 4), 43, 48, 54 and 57 are written and bench-verified** on `6.2.1_280150RSEP2026` (runs `20260928-064402`, `-070402`): 52 tests in s03, s05, s18 and s31. Rows not written, and why, are in each suite's docstrings and `docs/HIL_WEEK_DECISIONS.md` (D29-D34). Their finds: tracker #95, #96 (fixed) and #97 (deferred).

### WCB-WP12: One-line `?backup` restore through the `?CHK` gate (H)
s31_password_erase.py, beside backup.replay_idempotent. Reuse `_replayable` (skip if the chain holds `?MAP,PWM`) and its
secret redaction. **U, M.**

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| wcb.chain.one_line_restore_chk_gate, backup.chain_paste_restore, wcb.cmd.chk_gate_abort | The documented restore is a pasted `?backup` chain as one line. It starts with `?HW`, so it takes parseCommandsAndEnqueue's own `?CHK` gate, not the `?C` branch. A good chain verifies and applies. A bad CRC aborts all of it. Lowercase and unpadded CRCs are accepted. A custom FUNCCHAR's `^!CHK` is matched via lfiChk. The factory chain's mid-chain FUNCCHAR flip keeps later `!SEQ,SAVE` values whole. There is no backpressure past the 200-slot queue. | WCB.ino:2525-2530, :2541-2607, :2471-2474, :7330-7464 | Run under config_guard(1). (a) Send W1's configured chain as ONE USB line. Expect 'Command checksum VERIFIED - Configuration is intact', no refusal lines, no RX-overflow line in `?STATS`, and an unchanged chain. (b) Flip one byte in a `?LABEL` value. Expect 'Command checksum FAILED' and 'Aborting restore', with the label unchanged. (c) Send the CRC in lowercase, then with its leading zeros stripped: both verify. (d) Set `?FUNCCHAR,!` and `!SEQ,SAVE,HILFR,;S1a^;S1b`. The configured chain (ending `^!CHK`) verifies. Then send the factory chain under `?`: it verifies, flips the board to `!`, `!SEQ,GET,HILFR` comes back whole, and nothing reaches S1. Restore `?`. (e) Send `?LABEL,S5,HILX^?CHK00000000^?VERSION^?CHK<crc>`: it verifies (lastIndexOf). (f) Characterise a chain of more than 200 tokens: count the 'Command queue is full!' lines (BUG-24). Never log a chain: it holds `?EPASS` and `?WIFI`. | H (verifier: arguably M) |
| backup.c_branch_variants | `?C`-first branch variants: `<id>CHK` under a custom FUNCCHAR, the `^?CHK` fallback, and a checksum that ends at the next delimiter when a command follows it. | WCB.ino:8245-8317 | Under `?FUNCCHAR,!`, `!CSHILCK,;S1a^!CHK<crc>` gives VERIFIED and stores the value. `?CSHILCK2,;S1b^?CHK<crc>^;S0<m>` gives VERIFIED and prints `<m>`. A `?CS` value cannot hold `^?`, so test lastIndexOf as in row 1 (e). Clear the keys. | L |
| backup.factory_chain_custom_chars | After a FUNCCHAR and DELIM change, every factory-chain token after `?FUNCCHAR,!` carries `!`, the separator stays `^`, and both CRCs are valid. | WCB.ino:7427-7460 | Set `?FUNCCHAR,!` then `?DELIM,\|` and read WCB_WEBTOOL_CONFIG_PULL. Recompute both CRC-32s (give backup_chain_problems in s03 `\|` and `!` variants) and assert the prefixes. Restore with the chars.* raw-send pattern. | L |

### WCB-WP13: `?MGMT,FRAG` on the target: relay push, origin, `?` boundary, sessions (H)
s05_mesh.py, plus one arm in s19. **U, M.**

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| wcb.mgmt.relay_push_multichunk_chain | This is the Wizard's remote push. A multi-chunk FRAG (179 characters per chunk) carries a `^` chain of many commands, which the target applies in order. A trailing `?reboot` is deferred until after the last command (rule 11). Only single-token multi-chunk FRAGs are tested (inv.seqget_multichunk). | WCB.ino:3300-3336, :3339-3440 | Run under config_guard(2). Build about 450 characters: `?LABEL,S5,HILa<pad>^?LABEL,S4,HILb^?SEQ,SAVE,HILPU,;S2x^;S2y^?LABEL,S3,HILc`. Split it at 179 and send `?MGMT,FRAG,2,<sid>,i,3,<chunk>` 250 ms apart. W2's own `?backup` must show every token applied, with the sequence whole. Variant with `^?reboot` appended: exactly one 'Rebooting now...' at least 4 s after the last chunk, and every change in the chain after the boot. | H |
| wcb.rx.wizard_origin_rebroadcast, wcb.loopprev.wizard_origin_broadcasts, wcb.mgmt.frag_origin_rebroadcast, wcb.cmd.frag_wizard_origin_rebroadcast | Plain text or a recall that arrives through `?MGMT,FRAG` re-broadcasts from the target: a single chunk carries the SOH marker, and a type-3 session clears the flag. Board-to-board `;W` text does not re-broadcast, and neither does a client FRAG_UNICAST (type 5). | WCB.ino:3279-3296, :3424-3434, :5219-5236, :5553-5557 | (a) `?MGMT,FRAG,2,<sid>,0,1,HILbc<n>` must arrive once on W2S3 AND once on W1S2 (W2's re-broadcast). (b) The same as a 2-chunk FRAG over 179 characters. (c) Control: `;W2,HILbc<n2>` reaches W2S3 and never W1S2. (d) In s19, the probe (temporary client) sends W1 a 250-character plain-text ensured unicast: once on W1's ports, never on W2 or NaviCore. | M |
| wcb.cmd.mgmt_frag_trailing_q_exempt, wcb.cmd.mgmt_frag_trailing_q | A FRAG chunk that ends in `?` is forwarded, not eaten by the trailing-`?` help shortcut. That shortcut once killed pushes at the 15 s session timeout. | WCB.ino:5836-5872 | Send a two-chunk FRAG split right after a `?`: `…;S2<a>^?` then `VERSION^;S2<b>`. Expect `<a>` and `<b>` on W2S2 and no help text. Save a `?SEQ,SAVE,HILQF,…` value with a `?` at character 179 and split it there: `?MGMT,SEQGET,2,HILQF` must match byte for byte. A single chunk `…;S2<c>?` must put `<c>?` on W2S2. | M |
| wcb.mgmt.frag_session_busy_timeout | Target session rules: a different sessionId is rejected while the active session is under 2 s old. After 2 s it takes over. An incomplete session is discarded at 15 s. A session for another board is ignored. | WCB.ino:3352, :3378-3399, :3444-3450 | Turn on `?DEBUG,MGMT` on W2's USB. Interleave chunks of sessions A and B within 1 s: only A's markers reach W2S2, and W2 logs 'Session A busy'. Send chunk 0 of C only, wait 3 s, then a full D: D runs. Send chunk 0 of E only: W2 logs 'timed out' at about 15 s. A 2-chunk FRAG to target 3 puts nothing on W2S2. | L |

### WCB-WP14: Loop prevention and origin under live mesh traffic (H)
s19_client_mesh.py (the probe joins as a client). **U, M.**

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| wcb.loopprev.local_bcast_under_mesh_traffic, wcb.rx.json_does_not_latch_origin | Rule 3: a broadcast typed on W1 must reach the mesh even while W1 is receiving mesh traffic, and JSON telemetry must never set the origin flag. | WCB.ino:2444, :2896, :5233-5236, :5528, :8214 | Probe1 joins as a temporary client. Arm A: the probe floods unensured `{"hil":1}` at about 50 Hz (`_json_load`) while W1's USB types 50 marked plain broadcasts 150 ms apart. Each must arrive exactly once on W2S3, with an '[ETM] Sent seq' line for each. Arm B: the probe sends ensured `;V,hilq,INC` unicasts to W1 at about 50 Hz during the typing. Count the copies on W2S3: any loss is **BUG-3**. Add a control run with no traffic. | H |
| wcb.loopprev.mesh_timer_chain_origin, timer.mesh_origin_chain_no_rebroadcast | A `;T` chain from a mesh sender that is not the Wizard keeps its mesh origin across the delay: its groups reach W1's ports and are never re-broadcast. | WCB.ino:483-508, :5357-5362; command_timer.cpp:115, :256-271 | The probe (client) sends W1 an ensured `hilA<n>^;T400^hilB<n>`. Both must reach W1's broadcast ports about 400 ms apart. Neither may appear on W2's ports, on NaviCore or in `probe.mesh_received` from sender 1 (use require_tokens to get `?BCAST,OUT` ON). Repeat as a client broadcast. | M |
| wcb.timerchain.queue_overflow_silent | The 8-slot pending timer-chain queue drops silently when full. Chains drained together replace each other, which does print a line. | WCB.ino:469-508 | While W1 prints a large `?backup`, the probe sends 10 ensured `;S2hilT<k>^;T50^;S2hilU<k>` chains, each with its own marker. Silent loss = sent - 1 - count('Timer sequence replaced mid-run'). Characterisation (**BUG-22**). | L |
| wcb.rx.whoami_alias_escape | The `?WHOAMI` reply's JSON escapes `"` and `\` in the alias; saveWCBAlias keeps both characters. | WCB.ino:5334-5338; WCB_Storage.cpp:245-251 | Send `;W2,?ALIAS,HIL"q\x`, then have the probe send `?WHOAMI` to 2. The reply must json.loads to the alias `HIL"q\x`. Restore W2's alias from its chain. | L |
| softserial.tx_during_nvs_write | Soft-port RMT TX stays byte-exact during another task's NVS write: each chunk fits the channel memory, so the non-IRAM refill ISR is never needed mid-frame. | WCB_SoftSerial.cpp:59-71, :163-174 | W2 streams 40 lines of `;W1;S4<80-char marker>` while W1's USB sets and restores `?LABEL,S5` 20 times. Every W1S4 line must be byte-exact. | L |

### WCB-WP15: PWM: boot order, remap, guards, fan-out, clear-all overrun (H)
s14_pwm.py. Expect deferred reboots. **U, L.** Rows 1 and 5 fail until BUG-1 and BUG-12 are fixed.

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| pwm.boot_load_before_wcb_number | Saved PWM mappings are loaded before the board number, so on any board except WCB1 a saved output `W1S<p>` reloads as a local `S<p>`. | WCB.ino:9110 vs :9114; WCB_PWM.cpp:601-645 | On W2, send `?MAP,PWM,S3,W1S4` and wait out the reboot. `?MAP,PWM,LIST` and the chain must still show W1S4. Drive W2S3 from the probe: pulses must arrive on W1S4 and none on W2S4, and W2's S4 UART must still work. **BUG-1**. Clear the mapping and reboot. | H |
| pwm.valid_remap_updates_remote | Re-mapping an input to a different remote set sends CLEAR,OUT for each dropped output and OUT for each new one. | WCB_PWM.cpp:247-258, :346-385 | Send `?MAP,PWM,S3,W2S3` and wait out the reboot. Then, with `?DEBUG,ON`, send `?MAP,PWM,S3,W2S4`. Expect 'Sent PWM output clear to WCB2: ?MAP,PWM,CLEAR,OUT,S3' and '…config… OUT,S4'. After W2's reboot, W2's chain has OUT,S4 but not S3, and S3's flags are restored. | M |
| pwm.p_guard_other_owned_ports, pwm.p_guard_other_owners | `;P` is refused, with the pin untouched, on MP3, DFP, HCR, Kyber-reserved and raw-mapped input ports. | WCB.ino:7931-7940 | Turn on `?DEBUG,PWM,ON`. On W1, `?MAP,SERIAL,S5,R,S4` then `;P51500`: expect '[PWM] Ignoring ;P on S5', and probe text into S5 still arrives raw on S4. On W2, with a temporary MP3 on S3 and HCR on S4 (the boot.devices_restore_w2 pattern), `;W2,;P31500` and `;W2,;P41500` are both ignored and the device bytes still flow. The Kyber-reserved term alone needs a board outside Maestro_Remote (WP27 row 3). | M |
| pwm.multi_output_and_multi_input | One input drives several outputs, local and remote, and each gets the pulse. Two inputs are tracked independently. A 6th output in one `?MAP,PWM` is silently dropped (the cap is 5). | WCB_PWM.cpp:273, :689-797 | `?MAP,PWM,S3,S4,S5` (reboot): at 1000, 1500 and 2000 µs both probes read within ±50 µs. Add `?MAP,PWM,S2,W2S3` and drive the two inputs differently: each output follows its own input. `?MAP,PWM,S3,S4,S5,W2S2,W2S3,W2S4,W2S5`: LIST shows exactly 5. Never map W2S1. Since 1f629a8 each PWM port also takes an RMT channel. A classic ESP32 has 8: the LED and three soft-TX ports leave 4. Assert that no 'no RMT channel' line prints for up to 4 ports. | M |
| pwm.clear_all_many_remote_outputs | clearAllPWMMappings collects remote ports in `remotePorts[21][5]` with no bound. More than 5 outputs to one board overrun its row; for WCB20 the write goes past the array. | WCB_PWM.cpp:508-543 (HEAD :592-605) | With `?DEBUG,ON`, send `?MAP,PWM,S3,W2S2,W2S3,W2S4,W2S5` and `?MAP,PWM,S5,W2S3,W2S4` (6 W2 entries), then a mapping to W3S4 (W3 is not a peer, as in pwm.remote_unreachable_failed). `?MAP,PWM,CLEAR,ALL` must print 6 correct W2 removal lines, and W3's line must still name S4 (a spill renames it). No crash, and W2 is left with no OUT tokens. **BUG-12**. | M (verifier) |

### WCB-WP16: Identity setters, learned-peer persistence, sender binding (H)
s27_identity_destructive.py (reboots). **U, L.** Rows 1-3 need BUG-2, BUG-5 and BUG-6 fixed first.

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| pinmap.hw_chip_mismatch_accepted | `?HW` accepts the other chip family. On a classic ESP32, 3.1 and 3.2 put S2 on GPIO6/7 and S5 on GPIO9/10, which are SPI-flash pins. Nothing checks the chip, at save or at boot. | WCB_Storage.cpp:74-115; wcb_pin_map.cpp:97-113 | After the fix (**BUG-2**), as a `(should)` test on W1 (HW 1.0): `?HW,31` and `?HW,32` are refused and the chain keeps `?HW,1`. Never reboot while the chain holds a 3.x value. Also change ident.hw_setter (s27:263-284) to a same-family value (24): today it sets 32 on W1, and a run that dies before the restore boot-loops W1. | H |
| wcb.cmd.mac_hex_unvalidated, wcb.cmd.mac_newform_unvalidated | `?MAC,2\|3,<v>` goes through strtoul with no check. `ZZ` and an empty value become 0x00, `1FF` becomes 0xFF. The value is saved to NVS and the board goes deaf. The legacy `?M2`/`?M3` refuse the same input. | WCB.ino:6282-6301 vs :7035-7066 | Run under config_guard, with a finally that replays W1's own `?MAC` tokens. Send `?MAC,2,ZZ`, `?MAC,2,` and `?MAC,3,1FF`. Each should be refused like `?M2ZZ`, the chain should be unchanged, and a `;W2` round trip should still work. **BUG-5**. | M |
| wdp.peers.learned_set_survives_reboot, peers.learned_persist_reboot | Learned peers are restored at boot. The trap: a change followed within 5 s by `?reboot` is lost, because the save is debounced by 5 s while the restart fires after 4 s of quiet and does not flush first. | WCB.ino:628, :8906-8957, :9577-9600 | `_learned_peers` (s27:64) should read {6,9}. Reboot W1: the same set comes back, with '[PEER] restored 2 learned peer(s) from NVS'. Send `?WDP,ADD,<unused>^?reboot` as one line: the id must still be learned after the boot (lost today, **BUG-6**). `?WDP,FORGET,<id>`, wait 6 s, reboot: the id stays gone. Also add the cheap half as an assert in boot.banner_w1 (s29:467-472). | M |
| wdp.peers.fingerprint_discard | A learned-peer table saved under different MAC octets is discarded at boot. | WCB.ino:8931-8936 | Send `?MAC,3,<other>` and reboot. Expect 'saved under different MAC octets — discarding' and an empty `_learned_peers`. Restore the octet, reboot, send `?WDP,ADD,6` and `?WDP,ADD,9`, and wait 6 s. | L |
| wdp.peers.forget_learned_id | `?WDP,FORGET` on a learned (non-floor) peer unregisters it, makes it unreachable, drops its row and DA list, and persists. | WCB_WDP.cpp:1908-1917; WCB.ino:8847-8875 | `?WDP,FORGET,6` prints '[WDP] forgot WCB6' and '[PEER] WCB6 unregistered.'. `;W6,x` says 'not a reachable target', and `?PEERSLIVE` drops by 1. Wait 6 s and reboot: 6 stays gone. Restore with `?WDP,ADD,6` and wait 6 s. | L |
| wdp.security.sender_mac_binding | A packet whose sender id does not match its source MAC's last octet is dropped before WDP and ETM see it. | WCB.ino:5086-5089, :5125-5128 | This needs no probe verb (verifier). Send `?WCB,3` on W1 live, without a reboot, so its MAC stays ...:01. With `?DEBUG,ETM` on W2, expect '[ETM] Dropped id-spoof: senderWCB=3 but src MAC .1', no N=3 row, and no learn or join line. Restore `?WCB,1`. If the gate is broken, send `?WDP,FORGET,3` on W2 and NaviCore. | L |
| maestro.normalize_self_slots, boot.maestro_self_slot_repair | At boot, a remote-to-self Maestro slot is repaired into a local S1 slot and saved. | WCB_Storage.cpp:2535-2548; WCB.ino:9117 | Extend ident.wcb_number_reboot (s27:77). With WDP off, add `?MAESTRO,M8:W3S1:9600`, then `?WCB,3` and reboot. Expect '[MAESTRO] repaired legacy remote-to-self slot: M8 → local S1', and LIST shows M8 local on S1. Send `?MAESTRO,CLEAR,M8` before `?WCB,1`: S1 keeps its baud and flags because M1 still uses it. | L |

### WCB-WP17: `?WIFI` setter validation, with no opt-in and no reboot (H)
s28_wifi.py, as new tests without `opt_in` (or in s18 beside wifi.status). **U, S.**

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| wifi.cmd_refusals_fail_closed, help.wifi_ap_open_refusal, wcb.cmd.wifi_refusals | Four refusals save nothing: an AP password under 8 characters (the fail-closed gate against an open command AP), JOIN with no SSID, an SSID over 32 characters, and an unknown verb. | WCB_WiFi.cpp:390-420 | Run under config_guard(1). `?WIFI,AP,HILX,abc1234` prints 'AP password is 7 character(s); WPA2 requires at least 8.' and 'Refusing to configure an OPEN access point'. `?WIFI,AP,HILX` prints 'is 0 character(s)'. `?WIFI,JOIN` prints 'A network name is required…'. `?WIFI,JOIN,<33 x>,password1` prints 'SSID is 33 characters; the maximum is 32.'. `?WIFI,BOGUS` prints the Usage line. The `?WIFI` Mode and the chain token stay unchanged; compare the token by hash and never print it. | H |
| wifi.derived_ssid | `?WIFI,AP` with an empty SSID derives `WCB-<alias>` (or `WCB-<n>`), shows it as '(derived)', and uses it at boot. | WCB_WiFi.cpp:71-78, :108, :337-339, :433-435 | Send `?WIFI,AP,,<W1's own password from its chain>`. The confirmation shows SSID "WCB-…", and `?WIFI` shows 'AP SSID : WCB-… (derived)'. This one saves, so replay W1's own `?WIFI` line before any reboot. The boot half belongs under `wifi_modes`. | L |
| wcb.cmd.wifi_trailing_q_exempt | `WIFI,` is exempt from the trailing-`?` help shortcut, so a passphrase ending in `?` is saved. | WCB.ino:5856-5861 | `?WIFI,AP,<throwaway>,<throwaway ending ?>` gives the save confirmation, and the chain token ends in `?`. Replay W1's own token; no reboot is needed. | L |
| wifi.password_comma_and_trailing_space | The passphrase is everything after the SSID, commas included. The code promises to keep a trailing space too, but every line reader trims first. | WCB_WiFi.cpp:376-401; WCB.ino:2716, :8194; WCB_WS.cpp:386 | `?WIFI,JOIN,HIL-<n>,ab,cd,ef` stores a token ending `,ab,cd,ef`. Record that `…,abcdefgh ` loses its trailing space (**BUG-31**). Replay W1's own line. | L |

### WCB-WP18: Level-triggered GPIO arms across a restart and NVS writes (H)
s23_softrx_measure.py. **U, M.**

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| pwm.gpio_level_arm_vs_restart_and_nvs, boot.stale_gpio_clear_under_traffic | A restart taken while a PWM input pulses or soft-RX traffic arrives must boot exactly once: clearStaleGpioInterrupts runs before gpio_install_isr_service (tracker #78). NVS writes during 50 Hz PWM input must not panic: the ISR service is deliberately not IRAM. | WCB.ino:8997-9020, :9054-9077; WCB_PWM.cpp:183-187 | `?MAP,PWM,S3,S4` (reboot), then run `s3.pwm_out(1500)` continuously. Set and restore `?LABEL,S5` 10 times: no boot banner may appear. Send `?reboot` with the pulses still running: exactly one banner within 30 s (Boot attempts +1, no 'Interrupt wdt' or 'Guru Meditation'). The '[BOOT] Cleared a stale GPIO interrupt' line prints on every reboot, so assert it names S3's pin. `?VERSION` must answer, and S3 must then run a command exactly once. Repeat with probe text streaming into W1 S3 and S4. | H |

### WCB-WP19: Permanent auto-join from a real advert (H)
s18_etm_config_wdp.py, beside wdp.temp_probe_lifecycle. **U, M.**

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| wdp.autojoin.permanent_from_advert, peers.permanent_join_and_downgrade, wdp.autojoin.downgrade_to_temporary | A non-temporary device heard twice above the WCBQ floor is auto-joined as a PERSISTED learned peer. If it then advertises as TEMPORARY, it is downgraded and un-persisted. | WCB_WDP.cpp:679-698; WCB.ino:8768-8840 | Join with `probe.mesh_join(temporary=False)` at id 12 (not in FORBIDDEN_MESH_IDS, not 6 or 9). W1's log must show, in order, 'learned WCB12 HILProbe', '[PEER] WCB12 registered (live, learned).' and 'auto-joined WCB12'. Then check: DUMP PEER=2, `?PEERSLIVE` +1, and `;W12,<t>` reaches the probe and is ACKed. Wait more than 5 s, leave, and reboot W1: the restore count includes 12, and `;W12` is still a target. Next call `probe.mesh_join(temporary=True)` at 12 directly (probe_in_mesh refuses a PEER=2 id): expect '[WDP] downgraded to temporary WCB12' and PEER=4. Wait 6 s and reboot: 12 is not restored. Cleanup: `?WDP,FORGET,12` on W1 and W2, plus FORGET_PEER on NaviCore, which persists it too. | H |
| wcb.etm.learned_unreciprocated_not_expected | A learned peer that has never ACKed does not hold broadcast completion open. Its first ACK promotes it. | WCB.ino:619, :1466, :1493 | Use the same non-temporary join at a free id, with `?DEBUG,ETM`. The first plain broadcast resolves without waiting for the probe, and the probe's row stays at Sent 0. The second broadcast gives Sent 1, ACKd 1. | M |

### WCB-WP20: Wizard, Playwright with no board: the push side (H)
Add a new `tests/wizard/specs/push_fake.spec.js`. Reuse `setup()` from remote_pull_fake.spec.js: a fake BoardConnection whose
send and sendAndAwaitIdle record the command and resolve `{ok:true}`. Register it in s30_wizard.py; it also runs in CI
(wizard-tests.yml). **U, L.**

> **Status 2026-09-29: done, no board (run `20260929-101257`: every spec passes or fails as its `(should)` mark says).** `tests/wizard/specs/push_more_fake.spec.js` (fakes in
> `tests/wizard/lib/fake.js`): `wizard.push_fake_card_edits` (row 1's cards push_fake.spec.js left out),
> `wizard.push_fake_relay_reboot` (row 2's reboot, confirm-and-send and optimistic baseline), `wizard.push_fake_reboot_path`
> (row 3's timing: `?reboot` 1.5 s after the last ACK, the pull 3 s after the reconnect), `wizard.push_fake_all_staged`
> (row 4), `wizard.pull_fake_usb_guards` (row 5, a-f), `wizard.push_fake_general_fields` (row 6) and
> `wizard.push_fake_planned_verbs` (the bench specs' pre-flight), plus the `(should)` specs
> `wizard.push_fake_kyber_own_maestro` (W-20), `wizard.push_fake_all_shared_relay` (W-15),
> `wizard.push_fake_shared_reboot_repull` (W-14) and `wizard.push_fake_general_wcbq` (W-16). Row 3's matcher table and the
> W-4/W-9 cases were already `push_fake.spec.js`. All pass headless and each `(should)` spec fails on its defect. The plan
> against the code: a WCBQ edit reboots nothing (D28), so the reboot cases change the hardware version; the WDP state
> has no card field (its commands are the mesh panel's, WP41 row 4); the General modal has no WCBQ row (W-16); and Push
> All's four stages hold, but a relay on the shared port - the connection the first board gets - is reported lost (W-15).

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| wiz.dom.noop_push_rich_config | Through the real DOM (populateUIFromConfig, then sync*ToConfig, then buildCommandString against the baseline), a pulled board pushed unedited must send nothing, and one edit must send only that edit. Two known overwrites break this: syncMaestrosToConfig relabels the port 'Maestro <id>', and syncWLEDsToConfig forces the label, both BCAST flags OFF, and the baud. | app.js:3009-3057, 3115-3135, 3137-3378, 3630-3673, 7786-7823 | Fixture chains, one per card: MP3 at 38400 V3 with ONERR on S2; DFP on S3; HCR PORT S4:57600 POLL 0; WLED 3:W1S2:115200 with BCAST ON; a Maestro on S1 labelled 'Dome Maestro'; KYBER,LOCAL,S2 with targets and 'Kyber Marcuino'; MAP,SERIAL,S2,R,S3,W2S4 plus MAP,PWM,S1,S4 plus MAP,PWM,OUT,S5; WDP,OFF; WIFI,JOIN; HW,32 with LED,PIN,47; VAR,SET; sequences. For each, set `boardConfigs[1] = boardBaselines[1] = parse(chain)`, run `populateUIFromConfig(1)`, then `boardGo(1)`. Expect reason 'no changes to push' and zero sends. Then make one DOM edit per card and expect exactly its command. The Maestro and WLED cases fail today (W-4). | H |
| wiz.relay.push_boardGoRemote | The relay push. It builds the diff chain. MAC, EPASS and WCBCH changes need the network-group confirm. DELIM, CMDCHAR and FUNCCHAR changes are bootstrapped first, each in its own session. `^?reboot` is appended when needed. Chunks are framed at 179 characters. More than 16 chunks are refused, but only after the confirm and the bootstrap (F14). The baseline is advanced optimistically. | app.js:103-107, 8186-8375 | Seed `boardConfigs[2]` and `boardBaselines[2]`, edit a label, and run `boardGo(2)`. Expect one FRAG session with contiguous indices, target 2, chunks of at most 179 characters that join back into the diff, and no `?reboot`. A WCBQ edit appends `^?reboot`. An EPASS edit opens the network-group-change modal, and Cancel sends nothing. A diff over 2,864 characters must be refused before ANY send; today it is refused only after the confirm and bootstrap (W-9, F14). A FUNCCHAR change sends the bootstrap first. | H |
| wiz.push.reboot_path_vs_deferred_restart (half with no board) | commandStringNeedsReboot and commandStringChangesNetworkGroup match substrings. A label or sequence value containing `WCB,`, `HW,` (e.g. 'SHOW,'), `MAC,` or `KYBER,` asks for a reboot. | app.js:8924-8952 | Table test through page.evaluate. Expect true for the WCB, WCBQ, HW, MAC, WCBCH, WIFI, KYBER, MAESTRO,REMOTE and MAP,PWM,S forms. Expect false for `?LABEL,S1,My WCB, dome`, `?LABEL,S1,SHOW, x` and `?SEQ,SAVE,k,;S2HW,1`; these fail today (W-8). | M (verifier) |
| wiz.push.push_all_staged (half with no board) | Push All runs in stages: remote boards first (the reboot rides inside the session), then direct boards, then relays with skipReboot, and finally each relay is rebooted and re-pulled. MgmtRelay slots are excluded, and generalSettingsDirty is cleared. | app.js:8996-9084 | Use two fake connections plus one relayed slot. Record the order of sends and reboots. | H |
| wiz.pull.usb_guards | The USB pull guards. (a) A backup without 'End of Backup' keeps the existing config and baseline. (b) WCB_WEBTOOL_CONFIG_PULL is tried first, then `?backup`. (c) `?RELAY,1` renders a relay card and re-slots it, with no general baseline. (d) A board that reports another number migrates to that slot. (e) Duplicate numbers raise a warning. (f) The first pull seeds the general settings, and a later mismatch opens the modal. | app.js:7107-7318 | A fake connection whose sendAndCollect returns canned text. (a) The fixture cut before 'End of Backup': state stays deep-equal to before, with an error badge. (b) An empty bootstrap reply, then the backup. (c) Add `?RELAY,1`: expect `_relaySlots`, a relay card, no board section, and generalBaseline unset. (d) `?WCB,3` in slot 1 moves the board to slot 3. (f) A second board with a different EPASS makes the conflict modal list it. | H |
| wiz.general.multi_board_network_fields | The general fields (password, MAC octets, channel, characters, WCBQ) are read from the shared DOM into EVERY board's push. They are seeded from the first pulled board, and a mismatch opens a keep/use modal. | app.js:1255-1390, 6416-6524, 7815-7823, 8221-8229, 8808-8880 | Pull two fake connections whose chains differ in EPASS and WCBQ: the modal lists both fields. Keep: board 2's push sends board 1's `?EPASS` (plus the network-group confirm on the relay path). Use incoming: the DOM updates. Never run this against the real bench; it would split the mesh. | H |

### WCB-WP21: Wizard, Playwright on the bench through the HIL bridge (H/M)
New specs next to remote_pull.spec.js, run as `wizard.*` from s30_wizard.py. **U; row 9 needs one attended setup. L.**

> **Status 2026-09-29: done and bench-verified (`20260929-101257`, `-104602`, `-105553`: six pass, and `wizard.mapping_bidir_relay` fails as designed, W1's mapping cleared and W2's reverse left, W-21; the first run's failures were the harness's, fixed: `docs/HIL_TESTING.md` revision log).** `tests/wizard/specs/board_more.spec.js`, run by s30 as
> `wizard.push_reboot_path` and `wizard.push_reboot_path_direct` (row 1), `wizard.push_all_relay` (row 2),
> `wizard.mapping_bidir_relay` (row 3, `(should)`: W-21), `wizard.seq_var_editors` (row 4), `wizard.wdp_da_forget` (row 5)
> and `wizard.relay_terminal` (row 6). Every bench push first checks the verbs it will send (`tests/wizard/lib/wizard.js`
> plannedVerbs, itself pinned by `wizard.push_fake_planned_verbs`) and stops on anything it did not mean to change; the
> writes are throwaway labels, sequences, a variable and a serial mapping, each undone by the harness before
> config_guard's check, and `?HW` with W1's own version. Not written: row 7 on the bench (the hub protocol is serial-hub.js,
> in lockstep with NaviCore's and run two-page by `nctool.shared_hub_two_pages`; its refusal step would start a flash on W1
> if the guard regressed - the page's guard is `wizard.app_fake_hub_flash_refused`), row 8 on the bench (the device bytes
> are s09's, s15's and s24's; the page's command strings and HCR rendering are `wizard.editors_fake_live_controls`; the
> Network Test waits for WP24), row 9 (a Chrome profile holding both W1's and W2's grants, authorised at the keyboard) and
> row 3's PWM half (`wizard.editors_fake_mappings`; on the bench it costs two PWM reboots). The plan against the code: a
> WCBQ edit reboots nothing (D28), so the reboot push is `?HW` with the board's own version, which changes nothing; "one
> boot banner per push" holds on the shared port, which `connectBoard` gives the first board, while on a direct connection
> the reconnect pulses DTR and resets the board again by design (app.js:5437-5445), so that variant notes the count; on
> the shared port the Wizard never pulls after the reboot (W-14), so the spec pulls itself; Push All runs with W1 direct
> (W-15); "W1 and W2 on USB" is impossible with one grant per Chrome profile, so W2 is managed through W1; row 5 uses W2 S4
> (s12's unlabelled WDP-DA port), not S3; "Manage all" exists only on a MgmtRelay card, which the bench does not have
> (`wizard.app_fake_relay_card`). Each skips, saying why, when the bench cannot run it: W1 reports no hardware version
> the Wizard sends (the reboot pushes), W1 S2 is claimed by a device or either port already has a serial mapping
> (`mapping_bidir_relay`), W2 S4 is labelled or W2 has WDP off (`wdp_da_forget`).

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| wiz.push.reboot_path_vs_deferred_restart (bench half) | A push that needs a reboot sends `?reboot` 1.5 s after the last ACK, reconnects, and pulls 3 s later. The firmware now restarts only after 4 s of quiet, or at the 20 s cap, so the reconnect and the pull can land before or during the restart. | app.js:7910-7960; WCB.ino:1117, :1133, :9569-9600 | On W1, push WCBQ+1 through the card, then push it back. Expect one boot banner per push, a page that ends Connected with `boardBaselines[1]` equal to the harness's post-reboot `?backup`, and no 'config pull incomplete' toast. | M (verifier) |
| wiz.push.push_all_staged (bench half) | Push All with W1 on USB acting as the relay for W2. | app.js:8996-9084 | Edit a label on each board and click Push All. Check both boards' `?backup` with config_guard(1,2). W1's reboot (forced by a WCBQ edit) must come after W2's session. | H |
| wiz.editor.mappings_immediate | The mapping editor sends immediately. Save sends the mapping, and bidir writes the reverse on the destination board. Remove sends CLEAR. A remote PWM destination is cleared on its board. It works directly or through a relay. | app.js:3860-3966, 4060-4255 | With W1 and W2 on USB, add a serial mapping W1S2 → W2S4 with bidir and Save. Both `?backup`s show the forward and reverse mappings, and a probe line sent into W1S2 arrives on W2S4. Remove it: both are cleared (config_guard(1,2)). Repeat with a remote PWM destination, allowing s14's reboots. | M |
| wiz.editor.sequences_variables_immediate | The sequence editor handles save, rename (CLEAR of the old key first), delete, and TEST (`;SEQ<key>,L`). Over a relay, values over 198 characters go as multi-chunk FRAGs. The variable editor handles set, clear, temporary rows, and a `?VAR,LIST` refresh, directly or through `[TERM:n]`. An invalid name aborts the push. | app.js:4530-5193, 7806-7813, 8215-8221 | On W1 directly and on W2 through W1: save, rename and delete a throwaway sequence (one over 198 characters over the relay). `?SEQ,NAMES` and `?MGMT,SEQGET` must show the exact value, and TEST makes W1's probe see the `;S` output. Check variables with `?VAR,LIST`. The parseVarList part with no board is in WP41. | M |
| wiz.wdp.mesh_panel_and_da_forget (bench half) | Forget in the WDP panel sends `?WDP,DA,FORGET,S<s>,<type>`, as a FRAG in the target's funcChar when the device is on another board. | app.js:13305-13368 | The probe announces `@WDP1` on W2 S3, and the device appears in the W1-connected panel. Click Forget: the device is gone from the harness's `?WDP,DUMP`. | M |
| wiz.relay.manage_all_and_remote_terminal | Manage-all arms every WCB it hears (RTERM,START three times), re-arms stale sessions and pulls one board at a time. Typing into a relayed pane goes out as a MGMT FRAG. A disconnect clears the remote boards. (The [TERM:n] display is already covered by remote_pull_fake_display.) | app.js:534-588, 6271-6414, 9639-9694 | With W1 as the relay, type `;S2<m>` into W2's pane: the probe on W2S2 sees `<m>`. `?VERSION` typed into pane 2 shows its lines there. Disconnect W1: W2 shows not-managed. | M |
| wiz.hub.shared_port | The shared-port hub. Web Locks elect a leader and BroadcastChannel relays bytes to followers. Failover works, and a tab with no port never campaigns. A flash borrows the port but refuses while followers exist. | serial-hub.js:72-570; app.js:5895-6133, 7392-7420 | Open two pages in one browser context, with W1 shared from page A. Page B sees W1's lines, and its `;S1<m>` reaches probe1. Close A: B takes the port. With B following, a flash started from A shows the refusal toast. | M |
| wiz.live.device_controls_and_modals | WLED live controls, the HCR status modal (`?HCR,STATUS` every 3 s), the relayed Network Test (`?MGMT,ETM,CHAR,<n>`), and Identify. | app.js:3079-3114, 9835-9870, 12627-12860 | With a WLED on W1S2 (restored afterwards, as in s24), a preset click puts WLED JSON on the probe. With the probe emulating HCR status replies (the s15 pattern), the modal renders the volumes. Network Test waits for WP24. | L |
| wiz.connect.autodetect_and_modal | The connect modal (auto-detect, manual select, authorize). boardAutoDetect watches every granted port for 45 s, and the picker marks ports that other slots use. | app.js:6135-6259, 6596-7015 | This needs a dedicated Chrome profile holding both W1's and W2's grants, authorised once at the keyboard; the fixtures keep one grant per profile because the bridges are identical CP210x. Then Auto-detect in slot 1 connects the board that answers, and the slot-2 picker marks slot 1's port as in use. | L |

### WCB-WP22: WebSocket endpoint, with the PC joining W1's AP (H)
s28_wifi.py, opt-in `wifi_pc`. **A, M** (it runs in the nightly runs when ticked, per D7).

> **Status 2026-09-29: done and bench-verified (`20260929-055154`, `-055837`).** Row 1 is `ws.line_framing`; row 2 `ws.backup_over_ws` (the chain, strict UTF-8 per frame, and `?HELP` on the socket line for line as on USB); row 3 `ws.client_slots` (three slots, the oldest evicted, per-socket buffers, and the source flags: a socket broadcast after W2's `;W1` command reaches W2 S3); row 4 is in `wifi.join_w2_ap` (the ready line, and with `wifi_pc` too the PC on W2's AP answered over W1's joined address); row 5 `ws.ota_chunk` (also needs `ota_erase`); row 6 `wifi.ap_dhcp_no_gateway`, which reads the routes with `Get-NetRoute` rather than netsh. The join waits up to 60 s for a lease, which took 1.8 to 46.7 s on W1's AP (tracker #110), and every test notes W1's heap low-water mark, which reached 76 bytes (tracker #111). Not done: row 2's '?HELP line count' is the line-for-line check instead.

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| ws.inbound_line_framing_and_overrun | Line assembly on the WebSocket. `\r` and `\n` both end a line, several lines can share a frame, and one line can span frames. A line over 1,535 characters is dropped whole, so its tail never runs as an unprefixed broadcast. A leading space is trimmed. A frame over 3,072 bytes closes the socket and is reported. | WCB_WS.cpp:230-250, :285-288, :379-396 | Use WsClient (hil/ws.py). `?VERSION\r\n?VERSION\n` gives 2 blocks; `?VER` then `SION\n` gives 1. ` ;S4<m>` reaches W1S4 only. A 1,600-character `;S4AAA…<tail>` puts nothing on any probe port. A 4,000-byte frame closes the socket, and USB prints '[WS] dropped 1 inbound command(s)'. | H |
| ws.outbound_tee_integrity | Bulk replies arrive whole: the tee flushes inline when its 2 KB sink fills. No frame splits a UTF-8 character. Lines dropped off the loop task are reported. | WCB_WS.cpp:87-203, :393-394 | Send `?backup` over the socket: both chains are CRC-valid and identical to USB. Decode every frame strictly as UTF-8 and fail on any error (ws.py uses errors='replace' today). `?HELP`'s line count matches USB. | M |
| ws.clients_slots_and_source_flags | At most 3 clients; a 4th evicts the oldest. Each socket has its own accumulator, so lines never fuse, and a closed slot is freed. A WS command resets the origin flags, so a broadcast typed right after a mesh command still fans out. | WCB_WS.cpp:72-85, :206-228, :304-308, :373-378 | Open 4 clients: `?WIFI` counts 3, and the first client stops receiving. Two clients each send a half line (`?VER` and `?VERS`) and then complete it: 2 version blocks. W2 sends W1 a `;W1` command, then an unprefixed marker goes out over WS: it reaches W2's port. | M |
| ws.join_mode_endpoint_and_ready_line | In JOIN mode the WS server starts once associated and prints '[WS] command endpoint ready — ws://<ip>/ws' once; NaviLink watches for it. The line is not checked in AP mode either. | WCB_WS.cpp:311-366 | In wifi.join_w2_ap (`wifi_modes`), expect the ready line after 'joined'. With `wifi_pc` too, the PC joins W2's AP and `?VERSION` over ws://<W1's joined IP>/ws is answered. | L |
| ota.over_websocket | `?OTALOCAL,DATA` lines carrying a 1,024-byte chunk (about 1.4 KB) fit WS_LINE_MAX and are ACKed chunk by chunk. | WCB_WS.cpp:20-21; WCB_OTA.cpp:290-317 | With `ota_erase` too: send BEGIN,4096,0, then one 1,024-byte DATA with the image magic. Expect `[OTA:ACK,1024]`, then ABORT, and STATUS shows idle. | L |
| wifi.ap_dhcp_no_gateway | The SoftAP's DHCP offers no router option, so the PC keeps its real default route. | WCB_WiFi.cpp:122-158 | After association in pc_joins_ap_ws, netsh shows a 192.168.4.x address and no Default Gateway on that adapter, and `Get-NetRoute 0.0.0.0/0` is unchanged. | L |

### WCB-WP23: Wizard destructive paths: erase, flash, OTA (H)
Real board, driven through Playwright. **A / O, M.**

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| wiz.factory.erase_then_push_vs_deferred_restart | Factory Reset and the erase-then-push path send `?ERASE,NVS`, wait a fixed 4 s, reopen the port and probe `?backup` every 10 s, then full-push. The page comment 'firmware counts down ~3 s' is stale (W-12). The restart is deferred to 4 s of quiet or the 20 s cap, so the cap can restart the board in the middle of the push. | app.js:7028-7098, 7690-7780, 9901-9944; WCB_Storage.cpp:1156-1271 | Opt-in `nvs_erase` (attended). Run `boardGo(1,{mode:'erase'})` with the current config as the snapshot. Expect exactly one boot banner before the first pushed command, and a final `?backup` equal to the pre-test config (config_guard). | H (M on a quiet mesh) |
| wiz.flash.flash_update_factory_e2e | Flash, Update FW (app only) and Factory Reset through esptool-js. Each reconnects, restores the pre-flash config and general DOM, runs a full push and re-shares a borrowed port. Recovery skips the flash when the board already runs the target version. | app.js:7341-7363, 7422-7688; flasher.js:302-765 | Attended: only Greg may flash, and only the results/builds image. Run Update FW on W1 with a routed local binary. Expect the same version, a clean config_guard(1), and exactly one full-config verify pull. | H |
| wiz.ota.wizard_client | The Wizard's OTA client. Over USB it reads the chip family, raises the rate to 921600 (restoring it on error), sends ACK-paced chunks, and holds off the mesh tick while `_otaInProgress` is set. Over a relay it waits for relay ACKs, tries the post-OTA pull 12 times, and replays the ETM edges it suppressed. | app.js:1658-2049 | Opt-in `ota_full`. Run `boardOta(1)` with W1's own image from results/builds. Expect a reboot into the other slot with the same version, a clean config_guard(1), and USB back at 115200. The relay variant is `boardOta(2)` through W1 (`ota_full_wcb2`). | H |

### WCB-WP24: ETM characterisation: phase-3 load, relay, clamp, guards (M)
s99_etm.py. **U, M.** Needs BUG-4, BUG-17 and BUG-18 fixed; those arms fail today.

> **Status 2026-09-29: done and bench-verified.** BUG-4, 17 and 18 were fixed as re-scan #4, #17 and #18: row 1 is `etm.char_loaded` and `etm.char_load_on_peers`, and row 4's mid-run restart and relayed refusal are `etm.char_second_start_refused` and `etm.char_relay_refusal_reported`. New (runs `20260929-051753`, `-051938`): row 2 `etm.char_relay_roundtrip` (W1's relayed block recommends the same timeout W2 printed, 150 ms, first try), row 3 `etm.char_per_board_clamp` (one notice, 2 peers sampled 100 each, all three phases complete, 50 s), row 4's local guard `etm.char_guard_wcbq`. Not written: row 4's no-peer-online abort, which needs every peer off the air for W1's ~55 s offline window.

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| wcb.etm.char_remote_load_never_starts, wcb.etm.char_load_generator_never_starts | Phase 3 ('Loaded Network') is meant to start every peer's 10 s load generator through ETMLOAD. Today the ETM receive path ignores ETMLOAD, and the generator's own traffic is non-ETM, which ETM peers drop. | WCB.ino:1807, :1900-1950, :5017-5040, :5245, :5559-5566 | On Console(bench,2), send `?DEBUG,ON` and `?STATS,RESET`, then run `?ETM,CHAR` on W1 (s99 `_char`). During phase 3, W2 must print 'ETM load test started by remote board.', then 'ETM load test complete.' about 10 s later. W2's `?STATS` attempts must rise. No ETMCHAR_, ETMLOAD or LOAD_ text may reach a W2 port. **BUG-4**. | M |
| wcb.mgmt.etm_char_relay_e2e, wcb.etm.char_relay_roundtrip, wcb.mgmt.etm_char_relay, help.mgmt_etm_char_relay, wcb.cmd.mgmt_etm_char_relay | `?MGMT,ETM,CHAR,<n>`, the Wizard's per-board Network Test. The relay sends ETM_REQ. The target runs with the requester latched and returns ETM_FRAG, which prints as one `[MGMT:ETM,n]` line. A request that arrives mid-run is ignored. An abort sends a notice, and the latch is cleared afterwards. | WCB.ino:1741-1747, :2191-2198, :3242-3251, :4546-4560, :4719-4752, :4829-4840 | Turn on `?DEBUG,MGMT` on W1, then send `?MGMT,ETM,CHAR,2`. It is sent once, unACKed, so retry once. Expect 'ETM char request sent to WCB2'. Within about 90 s: exactly one `[MGMT:ETM,2]` holding 'WCB2 ETM Network Characterization', phase rows for WCB1 (WCB20 optional), and a 'Recommended ETM timeout' equal to W2's own USB output. No phase lines on W1. A second request mid-run makes W2 log 'already running — ignoring', and still only one result arrives. A later local `?ETM,CHAR` on W2 must not send anything to W1. Latch arm (**BUG-18**): `;W2,?WCBQ,1`, then the relay request gets no reply; restore WCBQ, and a local run on W2 must not frag-send to W1. Restore W2's `?ETM,COUNT` (NVS, floor 10). | M |
| wcb.etm.char_per_board_clamp | Messages per board are clamped so the phase index stays inside `etmCharPhaseSentTimes[3][200]`. A one-shot notice prints, and every phase still completes. | WCB.ino:726-727, :1752-1766 | Join probe2 as a temporary client, so there are 2 online peers. Set `?ETM,COUNT,200` and run `?ETM,CHAR`. Expect the cap notice with 'sampling 100 per board', rows for WCB2 and the probe in every phase, the recommendation line, and no reboot. It takes 2-3 min. Restore COUNT. | M |
| wcb.etm.char_guards_abort | The characterisation refuses when WCBQ < 2. It aborts when no peer is online, restoring `?DEBUG,ETM` and releasing the requester latch. It prints 're-enabled' on completion. A second local `?ETM,CHAR` mid-run restarts with no guard and loses the saved debug state. | WCB.ino:1687-1748, :2181-2199 | (a) Set AUTOJOIN,OFF and `?WCBQ,1`: expect 'ETM Char requires at least 2 WCBs'. (b) With `?DEBUG,ETM,ON`, HB 1 and MISS 1, deafen W1 (`_deaf_w1`) until its peers go OFFLINE, then run `?ETM,CHAR`: expect the abort line, and the heartbeat debug lines resume. (c) A full run with ETM debug on prints both 'Temporarily disabling' and 're-enabled'. (d) A second run started mid-run must still re-enable debug at the end (**BUG-17**). | L |

### WCB-WP25: Config pull: restart gate, CHANGED, dedup/park, tokens, knobs (M)
s03_wcb.py. **U, L.**

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| restart.waits_for_config_pull, wcb.restart.waits_for_config_pull, wcb.pull.restart_waits_for_job, loop.restart_waits_for_pull | A deferred restart (`?reboot`, a PWM change, `?ERASE`) waits for a running config pull, even past the 4 s quiet window or the 20 s cap. A pull request never resets the quiet clock. | WCB.ino:3800, :9573-9598 | On W2, set `?DEBUG,PULLPART,512` and grow the config past 2,912 characters (pull_parts_many's padding). Arm 1, the robust one: start `?MGMT,PULL,2,P` from W1, then send `?reboot` on W2's USB while the parts flow. All K parts must reach W1 and join with a valid CRC before W2's banner, and 'Rebooting now...' must follow the last part. Arm 2 catches a regression of the `!configPullJobActive()` term: `?reboot` first, then a pull landing 3.3, 3.6 or 3.9 s after it. | M |
| configparts.on_device_toobig_and_changed, wcb.pull.config_changed_restart, help.pull_changed_toobig (CHANGED half) | If the config changes between the measure walk and a part walk, the job restarts under a new part id, at most twice, and then answers ERROR CHANGED. The requester drops parts that carry the old id. | WCB.ino:3740, :3884-3931, :3971, :4017-4031 | Keep W2 over 2,912 characters at PULLPART,512. (a) Send one `?LABEL,S5,HIL<n>` on W2's USB after part 1. The reply still completes under a NEW id (PullCollector.restarts ≥ 1), CRC-valid, holding the new label. (b) Change the label every 150 ms. W1 gets `[MGMT:CFGERR,2]CHANGED`, W2 prints 'the config changed on every walk (3 tries)', and no joined reply mixes ids. Then a clean pull. The TOOBIG half is blocked (WP59). | M |
| wcb.pull.dedup_park | The target's scheduler. A repeat from the same requester within 1,500 ms is dropped. The running job's own requester gets 'reply in progress'. Another requester is parked and served oldest first; a parked request older than 5 s is dropped. A type-19 copy upgrades a parked type-5. The relay ignores frags meant for another requester. | WCB.ino:3834-3860, :4144-4190, :4330 | (a) Send `?MGMT,PULL,2` and resend it 1.0 s after the reply: no second `[MGMT:CONFIG,2]` within 4 s. Resent at 1.7 s it is answered (the harness spaces pulls 1.7 s apart, hil/wcb.py:91). (b) With W2 over 2,912 characters and `?DEBUG,MGMT` on W2, send `?MGMT,PULL,2,P` from W1 and from NaviCore's USB within 100 ms. Both replies complete with valid CRCs, W2 logs 'parked behind', and W1 prints no pull line for requester 20. | M |
| wcb.config.emit_partial_tokens | Chain tokens no test asserts: `?LED,PIN` (HW 3.x only); `?CONTROLLER,ON,<custom id>` followed by `?CONTROLLER,OFF`; `?PEERSLIVE` in the factory chain only; and the `?KYBER,LOCAL,S<n>,M<id>:W<w>S<p>:<baud>` target form after the `?MAESTRO` tokens (tracker #28). | WCB.ino:3487-3488, :3515, :3564-3620 | Extend four tests. hw_setter: `?LED,PIN` is present while `?HW,32` is set and gone after the restore (see WP16 row 1). `?CONTROLLER,ON,17` then OFF: the chain holds '…ON,17' then '…OFF'; restore ON,20, with AUTOJOIN off meanwhile. kyber.local_mode_s2: the exact target token comes after every `?MAESTRO`. And no `?PEERSLIVE` in the configured chain, one in the factory chain. | M |
| wcb.mgmt.session_timeout_reaping | Relay-side pull, SEQ-names and SEQVAL sessions are reaped after CONFIG_SESSION_TIMEOUT_MS. STATS and ETM sessions are not, which is harmless: a new sessionId resets them. | WCB.ino:4891-4910 | Set PULLPART,600 on W2 and `?DEBUG,MGMT` on W1. After the first `[MGMT:CFGPART,2]`, deafen W1 for about 2 s (`_deaf_w1`; calibrate from the frag lines, 20 ms per frag). Expect 'Config pull session XXXX timed out' about 10 s later, and a whole next pull. Repeat for `?MGMT,SEQ,2`. | L |
| wcb.cmd.debug_knob_validation, wcb.pull.knobs_validation, help.debug_kyber_alias, help.pull_knobs_disarm_and_range, wcb.cmd.debug_knob_edges | `?DEBUG` edge forms. KYBER,ON/OFF is an alias of MAESTRO. An invalid `?DEBUG,<x>` is refused. PULLPART refuses values under 512, over 2,880 or not numeric, and 0/OFF restore 2,880. PULLFAULT,OFF disarms. An armed OOM fault lapses after 60 s. | WCB.ino:3741, :3794-3823, :5883-5951 | On W2's USB: `?DEBUG,KYBER,ON` and OFF print 'Maestro debugging enabled/disabled'. `?DEBUG,FOO` prints 'Invalid DEBUG command. Use: ?DEBUG ?'. PULLPART 511, 2881 and abc each print 'Invalid PULLPART size. Use 512-2880…'. PULLPART,0 prints '…2880 bytes (default)'. Arm OOM, then OFF: 'disarmed', and the next pull is whole. Arm, wait 61 s, pull: whole. The range half is host-covered (config_parts_test.cpp:214-216). | L |
| wcb.mgmt.result_oversize_error | sendResultFrags replies '[ERROR] Result too large to relay (…)', and prints an ungated line on the target, when a STATS, SEQ-names or ETM result exceeds 16 × 182 characters. | WCB.ino:4392-4406 | Testable today (verifier). On W2's USB, send `?STATS,RPT,<k>,4294967295,…` (seven fields) for k = 1..20; at about 156 characters a row that passes 2,912. Then `?MGMT,STATS,2` on W1 gives `[MGMT:STATS,2]` with '[ERROR] Result too large to relay (', and W2 prints '[MGMT] Result too large to relay:'. Clean up with `?STATS,RESET` (RAM only). | L |
| wcb.mgmt.request_filters (reachable part) | A MGMT request addressed to another board is ignored. | WCB.ino:4144-4161, :4582-4611 | Send `?MGMT,PULL,3` on W1; the relay accepts 1-20. W2 (`?DEBUG,MGMT`) logs no 'Config request from WCB1', and W1 gets nothing. The wrong-password case is in WP43; the rest is blocked (WP59). | L |

### WCB-WP26: Capability routing, port ownership, HCR volume and FN 20/21 (M)
s15_hcr_mp3_dfp.py. **U, L.** Row 3 fails until BUG-15 is fixed.

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| route.stored_host_dead_failover, wdp.route.owner_offline_gate, route.pin_state_matrix | routeStoredOrCap and a pinned host. A pin never heard since boot is honoured. A pin seen and then gone dark fails over to the live owner, and the stored route is not rewritten. A dead pin with no online owner stays the target. With no pin, an owner that is ETM-offline is skipped, so the command gets a local miss instead of vanishing. | WCB.ino:7501-7523, :8731-8753; WCB_WDP.cpp:458-468 | Put the HCR on W2 S4 (`_hcr_w2`) and turn on `?DEBUG,ETM` on W1. (a) `?HCR,REMOTE,W7`, an unused id: `;H,MUSE` prints '[ROUTE] ;H,MUSE -> host WCB7' and nothing reaches W2S4. (b) Join the probe as temporary 7, MESH LEAVE, and wait out the ~50 s eviction: it now goes to 'host WCB2', `<MM>` appears on W2S4, and the chain keeps `?HCR,REMOTE,W7`. (c) Clear W2's HCR: it goes back to 'host WCB7'. (d) With no pin, AUTOJOIN off, and W2 deafened (the `?MAC,3` flip on W2's console, as in s25:57) until 'WCB2 went OFFLINE': expect '[HCR] Not configured — use ?HCR,PORT,Sx:baud first' and no [ROUTE] line. Restore. Repeat for `;A`/`;D` if cheap. | M |
| route.controller_pin | A capability pinned to the controller (`?HCR,REMOTE,W20`) is honoured while NaviCore heartbeats, because wcbPeerOnline counts the special peer. | WCB.ino:7509, :8731-8744 | With the HCR on W2 S4 as the alternate owner, set `?HCR,REMOTE,W20` and `?DEBUG,ETM`. `;H,MUSE` prints '-> host WCB20', nothing reaches W2S4, and NaviCore logs the inbound `;H`. Restore. | M |
| maestro.device_port_conflict_unguarded, devices.port_guard_refusals | Port ownership. The HCR, MP3, DFP and WLED guards never check for a local Maestro slot, and a local `?MAESTRO` add checks only the Kyber port. The pairs HCR/MP3, WLED vs HCR/MP3/DFP, DFP vs MP3, and each device vs a PWM input or output are untested. A refusal must change nothing. | WCB_Maestro.cpp:801-810; WCB_HCR.cpp:886-891; WCB_MP3.cpp:307-312; WCB_DFP.cpp:268-273; WCB_WLED.cpp:332-337 | WDP off. `(should)`: with `?MAESTRO,M5:W1S2:9600` in place, `?WLED,5:W1S2`, `?MP3,S2:9600:V20`, `?DFP,S2` and `?HCR,PORT,S2:9600` are each refused; all are accepted today (**BUG-15**). Reverse: `?WLED,5:W1S4` first, then `?MAESTRO,M5:W1S4`. On W2, declare `?MAP,PWM,OUT,S4` (and separately a PWM input on S4): each device on S4 prints 'S4 already in use by … - config blocked'. With the HCR on S4, `?MP3,S4` and `?WLED,6:W2S4` are refused; with the MP3 on S4, `?HCR,PORT,S4` and `?DFP,S4` are refused. Use config_guard on both boards, then `_unlearn`. | M |
| hcr.volume_shadow_seed_and_cache | The HCR volume shadow. It is seeded only after two agreeing QVx replies, never mid-fade and never after a pending write. VOLUP, VOLDN and fade restores step from the reported level. A commanded volume shows at once in STATUS and GET. A reply already in flight after a write neither overwrites nor confirms it. | WCB_HCR.cpp:105-135, :186-234, :819-831 | Use `_hcr_w2` with `?HCR,POLL,3`, and have the probe answer the poll with `<QVV,70>`, `<QVA,70>` and `<QVB,70>`. After two polls, `;H,VOLUP` must write exactly `<PVV75><PVA75><PVB75>`, not 55. `;H,VOL,A,20`: STATUS shows vA=20 at once and nothing is transmitted. Inject `<QVA,70>`: STATUS still shows 20, and a later VOLUP,A writes `<PVA25>`. On a fresh bind with a disagreeing pair (70, then 71), nothing is seeded, and VOLUP gives 55. | M |
| help.hcr_fn_20_21 | `;H,FN,20` gives `<PSG>` and `;H,FN,21` gives `<PSG>`, `<PSA,QPA>`, `<PSB,QPB>`. Both come from the shared WcbCmd HcrCodec, which NaviCore also compiles, and WcbCmd has no tests. | WCB_HCR.cpp:428-464; WcbCmd/src/WcbHcr.cpp:17, :76-85 | Add both to hcr.fn_codec's `_routed_verbs` (W1 to W2 S4). Assert the exact bytes, no RXERR, and no 'FN … rejected'. Also fix the fn list in the comment at WCB_HCR.cpp:440-442. | M |

### WCB-WP27: Maestro and Kyber: one id on two ports, Kyber receive dedup, normal-mode S1 (M)
s22_maestro_kyber.py (reboots, WDP off). **U, L.** Row 2 fails until BUG-11 is fixed.

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| maestro.fanout_same_id_two_local_ports | The same id on two local ports gets two slots. `;M<id><n>` and `;M<id>,<verb>` write once per port, and a verb fans out to the local port and the remote proxy together. Adding the second slot flips that port's baud and both broadcast flags, and saves them. | WCB_Maestro.cpp:198-246, :409-430, :541-545, :828-845 | Add `?MAESTRO,M1:W1S2:57600` beside M1:W1S1. Expect 'Baud rate for Serial2 updated', 'Disabled broadcast output/input on S2 (Maestro port)', and `?BCAST,OUT/IN,S2,OFF` in the chain. `;M11` puts AA 01 27 01 once on W1S1 and once on W1S2. `;M1,goHome` puts AA 01 22 on both and sends one ETM unicast `;M1,goHome` to WCB20. Note which port `;M1,getErrors` reads. `CLEAR,M1:W1S2` re-enables S2 at 9600, and the chain matches the baseline. | M |
| wcb.kyber.per_byte_port_dedup | Kyber bytes should reach each physical local port once. The send side has per-port masks. The target-98 RECEIVE side writes once per local Maestro SLOT, so two daisy-chained ids on one port get every chunk twice. | WCB.ino:5462-5478 (receive, HEAD); :5665-5707 (send) | (1) Send side, following kyber.local_mode_s2: `?KYBER,LOCAL,S2,M1:W1S1:<b>,M3:W1S1:<b>` and reboot. Probe bytes into W1S2 arrive once on the W1S1 tap. Repeat untargeted, with two local slots on S1. (2) Receive side: add a second local id on W2 S1 (`M4:W2S1`) and send bytes into W1 S1 (the Maestro_Remote bridge). Each byte must arrive once on the W2 S1 tap (**BUG-11**). Restore as local_mode_s2 does (two reboots). | M |
| boot.normal_mode_s1 | A board with no Kyber mode, which is how most deployed WCBs run, has never booted on the bench. It should open S1 at its saved baud, read S1 as a command port, and let S1 take broadcasts. Also untested: the Maestro_Remote branch that skips S2's UART for a PWM port. | WCB.ino:8058, :8470-8477, :9224-9243 | With WDP off, send `?KYBER,CLEAR` and reboot. The banner says 'Kyber is Not used', with no Maestro_Remote task line, and a `;S0<m>` injected on W1 S1 runs. Then, in Maestro_Remote mode, declare `?MAP,PWM,OUT,S2` and reboot: expect 'Serial2 reserved for PWM - skipping UART init'. Restore `?MAESTRO,REMOTE` and reboot. This setup also isolates the Kyber-reserved `;P` term for WP15 row 3. | M |

### WCB-WP28: WDP and relay side effects (M)
s25_mesh_side_effects.py. **U, M.**

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| maestro.wdp_auto_add, wdp.autoconfig.maestro_from_wcb_peer | A WCB peer's MAESTRO_CFG advert auto-adds a persisted proxy per host, even when the same id is also local. It refreshes the proxy's baud and skips when all 9 slots are full. It is gated on auto-join, a non-temporary sender, and wcbPeerActive or the controller. The advert lists only local slots. | WCB_Maestro.cpp:896-931; WCB_WDP.cpp:724-733 | With WDP on: (1) On W1, `?MAESTRO,CLEAR,M2:W2S1`, then `?WDP,POLL`. Expect '[WDP] auto-added remote Maestro 2 on WCB2 @ <b> baud (slot n)', and the chain regains `?MAESTRO,M2:W2S1:<b>`. (2) Re-issue it at 9600: the next advert prints 'Maestro 2 @ WCB2 baud updated to <b>'. (3) Per host: add a placeholder local M1 on a free W2 port, and W1 adds M1:W2 beside its own local M1. Clear both; the add flips that port's baud and flags, and KyberRemoteTask drains it. (4) Fill W1's 9 slots with W11+ placeholders: `?DEBUG,ON` shows 'heard but no free slot'. (5) W2's DUMP row carries MAESTRO=2 only. Use config_guard on both boards. | M |
| wcb.rx.rc_relay_paused_during_ota, wcb.rcrelay.ota_pause | Any `?OTA` or `?OTALOCAL` command pauses RC-JSON relay to USB for 8 s, so the `[OTA:ACK]` lines are not crowded out. Relay resumes afterwards. | WCB.ino:399-402, :5276, :5586, :6383, :6395 | Send `;W20,{"type":"PING"}`: the relayed PONG shows the window is open. Send `?OTALOCAL,STATUS`. A PING within the next 8 s gets no PONG or `rc_*` line for 6 s. After 9 s, a PING brings the PONG back. | M |
| wcb.rcrelay.queue_full_visible | The 64-slot RC relay queue counts and prints every drop ('[RCBRG] relay queue FULL …'). | WCB.ino:377-440 | With the window open, the probe (client) floods unensured JSON while W1 prints a large `?backup`. Relayed lines plus reported drops must equal lines sent. Relayed lines are re-tagged `{"sys":1,…}`, so match the "hil" key. The drop line prints on the WiFi task: deliberate, and a rule-11 exposure only under a flood. | L |
| wdp.pwm.autotag_persists_reboot, pwm.output_flags_and_provenance_persist | A PWM output's saved prior flags (pbo/pbi) and its WDP auto-source tag survive a reboot. A clear after the reboot restores deliberate OFF flags. Self-heal still removes an auto-configured output after the receiver reboots, and never removes a manual one. | WCB_PWM.cpp:867-881, :889-913, :949-987 | (1) `?BCAST,OUT/IN,S4,OFF`, `?MAP,PWM,OUT,S4`, reboot, then `?MAP,PWM,CLEAR,OUT,S4` and its reboot: the chain holds both OFF flags. (2) The s25 flow, with a W2 reboot after the auto-config and before W1's missed clear: `?WDP,POLL` makes W2 print '…no longer drives our S3 - clearing auto-configured PWM output'. (3) The same flow with W2's S3 declared manually first: it is not cleared. | L |

### WCB-WP29: Timer chains and `?STOP` semantics (M)
s04_serial.py. **U, M.** Rows 1 and 3 are `(should)` tests until BUG-10 and BUG-9 are fixed.

> **Status 2026-09-29: done and bench-verified.** Rows 1 and 3 were fixed as re-scan #10 and #9 and are covered by `serial.timer_after_func_command`, `mesh.timer_chain_two_chunks`, `serial.timer_stop_mesh` and `serial.timer_stop_in_chain`. Rows 2 and 4 are `serial.timer_replaced_mid_run` (typed and recalled arms) and `serial.timer_inline_payload` (run `20260929-050901`: both pass, the payload 601 ms after `a` and `c` 0 ms after it). Not written: row 2's mesh arm, which needs W2's console to type the local chain on; row 1's fragmented client unicast with a `;T`; and row 1's `;S1a^;T300^;S1b^?CHK00000000`, a `;`-first chain that takes the timer path (`isTimerChain` returns `isTimerCommand` for one) with its `?CHK` never verified: no backup or sequence produces such a chain (a restore chain starts `?`), so it is recorded here, not tested.

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| timer.chain_outside_splitter_loses_delays | A `;T` that reaches parseCommandsAndEnqueue instead of the timer splitter prints 'Invalid Serial Command' and loses its delay. That happens to `?`-first chains, to multi-chunk FRAGs and WCB_Client fragmented unicasts (reassembly has no timer gate), and to a `?`-first chain ending `^?CHK`. The reverse also happens: a `;`-first chain ending in `?CHK` takes the timer path, where the CRC is never checked. The same body recalled from a sequence keeps its delays. | WCB.ino:3439, :5357, :5606, :7521-7547, :8326; WCB_Storage.cpp:648 | (1) On W1's USB, `?VERSION^;S1{a}^;T800^;S1{b}` must leave about 800 ms between the markers on W1S1; today the gap is about 0 and 'Invalid Serial Command' prints. (2) A 2-chunk `?MGMT,FRAG` to W2 carrying `;S2{a}^;T800^;S2{b}`: time the gap on W2S2. (3) A fragmented client unicast over 188 characters with a `;T` in it. (4) Record whether `;S1{a}^;T300^;S1{b}^?CHK00000000` runs. | M |
| timer.replaced_mid_run, wcb.cmd.timer_replaced_midrun | Only one timer chain runs per board. A new chain (typed, a recalled sub-sequence containing `;t`, or from the mesh) replaces the running one, drops its remaining groups and prints 'Timer sequence replaced mid-run — N remaining group(s)'. | command_timer.cpp:98-113 | Typed: `;S1<a>^;T3000^;S1<b>`, then 500 ms later `;S1<c>^;T300^;S1<d>`. a, c and d arrive, b never does, and the line names 1 group. Recall: save `HILTB=;S1{b1}^;t300^;S1{b2}` and send `;S1{a1}^;T200^;CHILTB,L^;T1000^;S1{a2}`: a1, b1 and b2 arrive, a2 never does. Mesh: a single-chunk FRAG `;T` chain sent during a local chain. | M |
| timer.stop_scope, wcb.cmd.stop_not_whole_line | `?STOP` works only as a whole line typed locally. Over ESP-NOW, through a FRAG, or inside a non-timer chain, it falls into the `s` legacy catch-all, is ACKed, and is silently ignored. Inside a timer group it works. | WCB.ino:6955, :7253-7275, :8240; command_timer.cpp:85, :239 | Start `;S2<a>^;T3000^;S2<b>` on W2 with `?MGMT,FRAG,2,<sid>,0,1,…`. After 500 ms, send `;W2,?STOP`: `<b>` must never reach W2S2, and W2 must print 'Timer sequence manually stopped'. Today `<b>` arrives and nothing prints (**BUG-9**). Also try `?VERSION^?STOP` during a local chain, and a sequence whose body is `?STOP`. `;S1{a}^;T300^?STOP^;S1{b}` must never deliver b; this one already works. | M |
| timer.inline_payload_timing | `;T<ms>,<command>` starts a new group whose first command is the payload, run after the delay. | command_timer.cpp:151-213 | `;S1{a}^;T600,;S1{b}^;S1{c}`: b arrives about 600 ms after a, and c immediately after b. | L |

### WCB-WP30: ETM: controller unicast, ETM-off fleet, dedup ring, boot clear, debug lines (M)
s18_etm_config_wdp.py. **U, L.**

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| wcb.etm.special_peer_unicast_tracking | A unicast to the controller, which is never in wcbPeerActive, still expects an ACK, counts in the 'WCB20 (special)' `?STATS` row and is retried 3 times. A broadcast does not expect the controller's ACK. | WCB.ino:1453-1455, :1533-1625, :1983-2063 | Send `?STATS,RESET` and turn on `?DEBUG,ETM`. `;W20,?version` gives 'Sent seq N', 'ACK received from WCB20', 'fully acknowledged', and the row 'WCB20 (special): Sent: 1, ACKd: 1'. A plain broadcast leaves the row at 1. Deafen W1 (`_deaf_w1`) and send `;W20,?VERSION`: expect 'Retry 1-3 to WCB20' and 'WCB20 failed to ACK … after 3 retries'. NaviCore's USB shows it ran the command exactly once. | M |
| wcb.etm.special_peer_offline_sweep | The controller is swept against this board's own (HB+1)×MISS threshold. 'WCB20 (special peer) went OFFLINE' alternates with 'came ONLINE'. A unicast sent while it is offline gets one try, with no retry. | WCB.ino:1306-1313 | Extend etm.offline_detection_timing (HB 4, MISS 1). Collect the WCB20 edges: they must alternate (#80), each at least about 4.85 s after the previous WCB20 packet. Skip if NaviCore stays online throughout. | L |
| wcb.rx.etm_off_fleet_normal_path, wcb.stats.etm_off_counters | ETM off on every board is a supported mode (rule 4). Commands then arrive as 249-byte frames through the normal receive path: the target filter, the NUL terminator, the SOH strip for relayed FRAGs, timer deferral, the `;M` get rewrite and the JSON relay. `?STATS` shows the MAC-level counters instead. | WCB.ino:1998-2008, :3042-3048, :5376-5616 | Send `;W2,?ETM,OFF`, then `?ETM,OFF` (W2's USB is the fallback). Check: `;W2,;S2<a>` arrives on W2S2; a plain broadcast reaches W2S3; `?MGMT,FRAG,2,<sid>,0,1,;S2<b>` runs; a FRAG carrying `;S2<c>^;T500^;S2<d>` keeps its ~500 ms gap; `;W2,;M2,getErrors` reaches W2's Maestro. After `?STATS,RESET` and one send, expect 'Transmission Attempts: 1, Delivered: 1, Failed: 0' and 'Delivery Success Rate: 100.00%'. Restore `;W2,?ETM,ON`, then `?ETM,ON`, within 40 s; WDP is off meanwhile. | M |
| wcb.rx.maestro_whitelist_breadth | On a receiver with ETM on, the non-ETM "Maestro" whitelist tests only `structCommand[1] == 'M'`, so any non-ETM `?M…` or `xM…` payload passes the mismatch gate. | WCB.ino:5025-5027, :5036 | Characterise inside etm.off_local's W1 `?ETM,OFF` window (under 30 s). Record whether W2 executes `;W2,?MAESTRO,LIST` and prints a broadcast `xMhil<n>` (today it does both, **BUG-21**), against `;W2,?VERSION`, which is dropped. | L |
| wcb.etm.clear_peer_from_pending | A peer that leaves membership (FORGET, temporary eviction, a WCBQ reduction) is removed from every in-flight entry's expected-ACK set and counted failed once. | WCB.ino:1335-1341, :1631-1642, :8871-8874 | Probe2 joins as a temporary client, then leaves with forget=False. With `?DEBUG,ETM`, send a broadcast and, within 300 ms, `?WDP,FORGET,<id>`. Expect no 'WCB<id> failed to ACK', no further 'Retry n' lines, and a prompt 'resolved' or 'fully acknowledged'. Forget the id on W2 too. | L |
| wcb.rx.telemetry_not_in_dedup_ring, wcb.etm.json_not_in_dedup_ring | Best-effort JSON (target 0, starting with `{`) is neither ACKed nor added to the per-sender dedup ring, so telemetry cannot evict a command's seq before that command's retry arrives. | WCB.ino:5160-5184 | Deafen W1 (`_deaf_w1`) and set `?ETM,TIMEOUT,1500`. Send `;W2,;S2<m>`, and before retry 1 type 10-20 `{"hil":n}` lines on W1 S3. Restore the octet: `<m>` must arrive exactly once on W2S2. | L |
| wcb.etm.boot_clear_once_per_4s, wcb.etm.boot_ring_clear_once_per_boot | A boot announce clears the sender's dedup ring at most once per 4 s, so a retry that straddles announces 2 and 3 must not run twice. | WCB.ino:706, :1282-1286, :5111-5120, :9436-9437 | Reboot W2. When W1 prints 'WCB2 came ONLINE (boot)', send ONE line on W2's console, `?MAC,3,<other>^;W1,;S2<m>`, with the default 500 ms timeout. Its retries at +0.5, +1.0 and +1.5 s straddle announce 2. `<m>` must arrive once on W1S2. Restore `?MAC,3` in a finally. The timing is tight; a probe raw-ETM verb (WP59) would give a clean control arm. | L |
| wcb.rx.debug_visibility_lines | Under `?DEBUG,ON` the receiver prints 'Processing ETM input from WCB<n>: …' (ETM path), or 'Processing ESP-NOW input:' and 'Sender ID:' (non-ETM path). An inbound `?STATS,RPT` is printed only under `?DEBUG,MGMT`. | WCB.ino:5295-5300, :5389-5391, :5602-5603 | Send `?DEBUG,ON` on W2. `;W2,;S2<m>` prints the ETM line. `;W2,?STATS,RPT,9,…` prints no Processing line under DEBUG,ON but one under DEBUG,MGMT; clean up with `;W2,?STATS,RESET`. The non-ETM line needs the whitelist path (W1 `?ETM,OFF` plus `;W2,;M2,getErrors`). | L |
| wcb.mgmt.frag_single_chunk_chksm_boundary | Under `?ETM,CHKSM`, a single-chunk FRAG payload of 180-186 characters still goes as one SOH-marked ETM unicast. From 187 characters it is REJECTED as over 179. | WCB.ino:3288-3316 | Inside the CHKSM tests' window, `?MGMT,FRAG,2,<sid>,0,1,;S2<183-char marker>` arrives on W2S2. A 187-character payload prints '[MGMT] FRAG payload 187 > 179 chars — REJECTED' and nothing arrives. The CHKSM-off variant stays manual, because s18 forbids changing W2. | L |

> **Written 2026-09-28 (`8775921`, not yet run on the bench).** Row 4's "today it does both" predates #21's fix:
> only `<cmdChar>M` passes now. Row 2: NaviCore cannot go offline under a 5 s threshold, because its `rc_hb` every 2 s
> is an ETM frame that refreshes it, so the tests make a probe W1's controller (`?CONTROLLER,ON,15`) and silence it
> with `MESH LEAVE`. Rows 6 and 7, and row 1's "ran exactly once", need a lost ACK, which the plan gets by changing
> `?MAC,3`; the bench tests do not change a board's MAC, so those are not written.

### WCB-WP31: Remote terminal (RTERM): volume, peer registration, STOP (M)
s05_mesh.py. **U, M.**

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| rterm.long_output_and_long_lines | A mirrored line over 160 characters is split into several `[TERM:n]` lines. Burst output must survive two drop points: the target's unchecked esp_now_send per line, and the relay's 16-deep queue, which drops when full. | WCB_RemoteTerm.cpp:17, :78-86, :121, :177 | With `w.remote_terminal(2,1)`, run `?HELP` and `?backup` on W2. Compare the `[TERM:2]` lines on W1 with W2's own USB (COM15): same count after rejoining the 160-character splits, same text, and a CRC-valid `?backup` chain. Report every missing line; loss under bursts is plausible and would need a decision. | M |
| rterm.relay_outside_peer_table | `?RTERM,START` to a relay that is not yet an ESP-NOW peer registers it on demand (the Intellex path). | WCB_RemoteTerm.cpp:111-121; WCB.ino:6672-6683 | Send `;W2,?RTERM,START,20`. NaviCore's USB (COM5) shows '[TERM:2][RTERM] Session started', and `;W2,?VERSION` lines arrive prefixed `[TERM:2]`. Finish with `;W2,?RTERM,STOP`. WCB20 is normally already W2's peer (SPECIAL, WCB.ino:9379-9395), so record which branch ran (verifier). | M |
| rterm.stop_flush_and_password (STOP half) | `?RTERM,STOP` flushes a pending partial line, then stops mirroring, so no `[TERM:n]` line follows. | WCB_RemoteTerm.cpp:136-142 | With a W2→W1 session running, send `;W2,?RTERM,STOP`, then `;W2,?VERSION`: no `[TERM:2]` line within 3 s. The password half is in WP43. | L |

### WCB-WP32: Soft-serial TX baud table, line-buffer heap, live `?BAUD` on text ports (M)
s12_serial_input.py. **U, M.**

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| softserial.rmt_tx_baud_table | Soft-port RMT TX is byte-exact across the accepted baud table. Below 4800 the encoder uses 1 MHz resolution, 7 symbols per byte and 8-byte chunks. At 110 baud, long bit runs are split into several 15-bit halves. 14400 and 115200 are covered too ('Output is fine' at 115200). | WCB_SoftSerial.cpp:14, :61-120; WCB_Storage.cpp:165-181 | Under config_guard, run `?BAUD,S4,<b>` for 110, 300, 600, 1200, 2400, 9600, 14400 and 19200 (the probe's soft channels go only to 38400, links.py:22), and 57600 and 115200 on a probe hardware-UART channel. Send a `;S4` line of 0x00-heavy and 0xFF-heavy bytes through a hex-safe marker: byte-exact. `?BAUD` refuses 4800 (WCB_Storage.cpp:165-167), so 2400 and 9600 bracket the resolution switch. Restore 9600. | M |
| input.line_buffer_heap | A port's line buffer grows one byte per character with no cap until CR/LF arrives, for example from a device at the wrong baud or a binary stream. After a line over 512 characters, releaseLineBuffer returns the heap. | WCB.ino:8146-8149, :8227 | The probe sends about 20 KB with no CR/LF into W1 S3, sampling `?STATS` 'Heap:' along the way. Then CR: W1 answers, does not reboot, and prints no 'Out of memory'. A single 3 KB `?SEQ,SAVE` line on USB must leave 'Heap: free' within about 200 B of its value before. Characterisation (**BUG-23**). | M |
| input.live_baud_text_port | The command parser skips a soft port while applyLiveBaud tears it down and re-begins it, so it never reads a freed RX buffer. | WCB.ino:8181 | The probe streams text into W1 S3. Send `?BAUD,S3,19200`, then back. No reboot, and a `;S0<m>` sent into S3 at the restored rate runs. | M |
| wcb.baud.flush_before_rate_change | On S1 and S2, applyLiveBaud drains queued TX before reprogramming the divisor, so `;S2<text>^?BAUD,S2,<new>` sends the text whole at the old rate. | WCB.ino:2342-2344 | With the probe on W1 S2 at 9600, send one line: `;S2` + 120 characters + `^?BAUD,S2,19200`. The 120 characters arrive exact at 9600 with no RXERR. Restore `?BAUD,S2` from the chain. | L |

### WCB-WP33: Variables: the reserved name 'all' and `?VAR,CLEAR,ALL` (M)
s16_vars_if.py. This replaces a vacuous test. **U, S.**

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| var.reserved_all_and_clear_all, help.var_reserved_all_and_clear_all, wcb.cmd.var_clear_all_and_reserved | 'all', in any case, is reserved for `;V`, `;VP` and `?VAR,SET`. `?VAR,CLEAR,ALL` wipes every variable, persistent ones included, from RAM, NVS and `?backup`. var.clear_all_name_collision (s16:279) now passes vacuously: its assert at :302 is always true, and its docstring is stale. | WCB_Variables.cpp:63-70, :207-211, :245-249, :318-322, :344-351 | Under config_guard, first save W1's `?VAR,SET` tokens. `;V,all,1`, `;VP,ALL,1` and `?VAR,SET,All,1` each print "[VAR] 'all' is reserved: ?VAR,CLEAR,ALL clears the whole table", and GET all stays unset. Create `;V,hilv,1` and `;VP,hilp,2`, then send `?VAR,CLEAR,ALL`. Expect '[VAR] All variables cleared', LIST '(none)', and no `?VAR,SET` in `?backup`. Reboot: no '[VAR] Loaded' line. Replay the saved tokens, then replace or retire the vacuous test. | M |

### WCB-WP34: Legacy spellings, label length, help-page accuracy (M)
s29_leftovers.py. **U, M.** Needs BUG-8, BUG-27, BUG-28 and BUG-29 fixed.

> **Status 2026-09-29: done and bench-verified.** BUG-8, 27, 28 and 29 were fixed as re-scan #8, #27, #28 and #29, and rows 1, 3, 4 and 5 are their tests (`wcb.cmd.legacy_prefix_typos`, `wcb.cmd.legacy_s_baud_messages`, `persist.label_max_30`, `help.corrected_pages`). Row 2 is `wcb.cmd.legacy_wcb_wcbq_spellings` (run `20260929-051346`): `?WCBQ<current>` and `?WCB<own number>` re-save what is stored, 0 and 21 are refused by both, and W1's chain does not move.

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| wcb.cmd.legacy_prefix_typo_side_effects | Legacy prefix matches swallow near-miss typos. `?CCLEAR,ALL` saves 'L' as the command character. Any 2-character `?D<x>` sets the delimiter. An unmatched `?S…` (`?STAT`, `?SBAUDS1,9600`) is dropped silently. | WCB.ino:6759-6763, :6777, :6955, :7006-7015, :7253-7275 | `(should)`, under config_guard. `dev.send('?CCLEAR,ALL')`, then `?config` must still show the command character ';'; today it shows 'L' (restore with a raw `?CMDCHAR,;`). `?STAT` and `?SBAUDS1,9600` must print 'Unknown command'; today there is silence. Record what `?DA` does, then restore `?DELIM,^`. **BUG-8**. | M |
| wcb.cmd.legacy_wcb_wcbq_spellings, help.legacy_wcb_wcbq | The legacy `?WCBQ<n>` (with its live peer reconcile) and `?WCB<n>` reach the same setters as the comma forms. | WCB.ino:6785-6788, :7069-7074, :7302-7309 | `?WCBQ2` at the current value prints 'Saved WCB quantity: 2 … reconciled live' and leaves the chain unchanged. `?WCBQ21` is refused. `?WCB<own number>` prints the saved line with no identity change. `?WCB0` and `?WCB21` print 'Invalid WCB number'. | L |
| help.legacy_baud_sxrate, legacy.s_baud_and_w_edges (baud half), wcb.cmd.legacy_s_baud_and_numbers | The legacy `?S<port><baud>` prints a deprecation warning and calls updateBaudRate, but then says 'stored in NVS' even after a refusal. `?BAUDS<n>` without a comma prints nothing. `?S69600` is skipped silently. | WCB.ino:6948-6956, :7253-7274 | `?S5<current rate>` prints the deprecation warning, 'Baud rate for Serial5 updated to 9600' and '… stored in NVS', and `?BAUD,S5` is unchanged. After `?S54800`, 'Invalid baud rate' must not be followed by 'stored in NVS'; today it is (**BUG-28**). Pin the silence of `?BAUDS5`, and characterise `?S69600`. | L |
| help.label_max_30_unenforced, wcb.cmd.label_length_bounds | The help text and the legacy `?SLS` both cap a label at 30 characters. The canonical `?LABEL,Sx,<text>` stores any length. The Wizard caps its input at 30 (app.js:808). | WCB_Help.cpp:155; WCB.ino:6086-6100, :7107-7110; WCB_Storage.cpp:1909-1923 | `(should)`: `?LABEL,S5,<31 chars>` prints 'Label too long. Maximum 30 characters.' and leaves the token unchanged; today it is stored (**BUG-27**). `?SLS5,<31 chars>` pins the legacy check. Restore the S5 label. | L |
| help.page_accuracy, wcb.cmd.help_topic_pages | 29 topics have their own help page, and only LABEL, MAP, VAR, OTA and OTALOCAL are tested. Several pages contradict the parser. | WCB_Help.cpp:10-1180 (:28-29, :56, :138, :155, :170, :315, :348, :389, :490, :835, :1196) | Follow ota.help_matches_parser (s20:168). For each topic, `?<TOPIC>?` must print a section header and a line naming the verb, and run nothing else. Assert the corrected text (**BUG-29**). Where it is safe, send the advertised example (`?SLC1`; `?MAESTRO,1:S1:57600` with WDP off) and assert it is accepted, or that the help no longer offers it. | L |

### WCB-WP35: Prefix characters and format refusals (M)
s18_etm_config_wdp.py (the chars.* tests). **U, M.** The lockout arms of row 1 need the BUG-7 guard first.

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| chars.delim_collides_with_prefix | `?DELIM` accepts any character, and prefixCharOk never compares against the delimiter. `?DELIM,?` or `?FUNCCHAR,^` makes every config line split at its own prefix; only an erase-flash recovers the board. `?DELIM,;` and `?CMDCHAR,^` break the `;` family. | WCB.ino:950-965, :6601-6609; WCB_Storage.cpp:573-578 | Today, characterise only the pair the verifier showed to be recoverable: `?DELIM,;` (restore with `?DELIM,^`) and `?CMDCHAR,^` (restore with `?CMDCHAR,;`). After the guard (**BUG-7**), `?DELIM,?`, `?DELIM,;`, `?FUNCCHAR,^` and `?CMDCHAR,^` are refused with a prefixCharOk-style message, and `?config` and the chain are unchanged. Never send `?DELIM,?` or `?FUNCCHAR,^` to current firmware. | M (the scan marked it opt-in) |
| wcb.cmd.format_error_lines, wcb.cmd.prefix_char_format_refusals, wcb.chars.prefix_printable_guard | `?DELIM`, `?FUNCCHAR` and `?CMDCHAR` with anything other than one character print their Invalid lines. prefixCharOk refuses a space, a control character and DEL. The short legacy `?LF` and `?CC` forms are refused. | WCB.ino:950-965, :6601-6636, :6995-7015 | `?DELIM,ab`, an empty `?FUNCCHAR,`, `?CMDCHAR,xy`, `?LF` and `?CC` each print their exact refusal line. The raw bytes `?CMDCHAR,\x7f`, `?FUNCCHAR,\x7f` and `?CC\x7f` each print 'Prefix characters must be printable, non-space characters.'. `?config` and the chain stay unchanged; the fallback restore is `\x7fCMDCHAR,;`. | L |
| wcb.cmd.erase_bad_argument, nvs.erase_queued_tail_and_usage (usage half) | `?ERASE` with any argument other than NVS prints 'Invalid format. Use: ?ERASE,NVS' and erases nothing. | WCB.ino:6639-6646 | Gate the test on s31's `_replayable`: a regression here wipes NVS. `?ERASE`, `?ERASE,NV` and `?ERASE,NVSX` print the usage line; no 'Rebooting' follows within 6 s, and `?NVS` and the chain are unchanged. Never send `?ERASE,nvs`: the match is case-insensitive, so it erases. The queued-tail half is in WP57. | L |
| help.legacy_refusal_only (?LF success) | The legacy `?LF<c>` sets the function identifier; this is code separate from `?FUNCCHAR`, and only its refusal is tested. (The `?PMS` and `?KYBER_LOCAL` success paths are in effect covered, per the verifier.) | WCB.ino:6757-6758, :6995-7004 | `?LF!` prints "LocalFunctionIdentifier updated to '!'", and `!VERSION` answers. Restore with `!FUNCCHAR,?`. | L |

### WCB-WP36: Boot sequence timing and the boot guard (M)
s26_boot_restore.py, plus cheap asserts in s29 boot.banner_w1/w2. **U, M.**

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| boot.guard_and_boot_attempts | The boot guard restarts a setup() that has not finished within 10 s. The RTC boot-attempt counter rises by exactly 1 per `?reboot`; a hidden guard or panic restart would add 2 or more, and WCB.wait_boot passes such a restart today. | WCB.ino:8369-8376, :8633-8672, :9029, :9494 | Reboot W1 twice. The second 'Boot attempts since power applied: N' must be the first plus 1, with one 'Raw Serial Forwarding Task Created' per reboot. Time each boot from 'rst:' to that line, on W1 (AP mode, carrying backup_large_config's sequences) and on W2. Fail above about 7 s: the comment at :8645-8649 wants a measured worst case before the 10 s limit is lowered. | M |
| wdp.advert.boot_burst | A booting board sends 3 adverts starting at about 1.6 s, 1.3 s apart, plus its DA list, so its neighbours refresh without a POLL. | WCB_WDP.cpp:378-382, :1965-1966 | Reboot W1, with no POLL anywhere. Within 6 s of the banner, `?WDP,DUMP` on W2's USB shows N=1 with AGE ≤ 2 s. | L |
| wcb.etm.boot_heartbeat_and_announce_timing | After boot come three ETM_BOOT announces, the first at +1.5 s and the rest 1.2 s apart. The first heartbeat lands at a random 1 to ETM,BOOT s. Nothing is sent while ETM is off. | WCB.ino:1252-1292, :9434-9437 | With `?DEBUG,ETM,ON` on W2's USB, send `;W1,?reboot`. Expect exactly three '[ETM] Boot announce from WCB1' lines 1.1-1.4 s apart, and the first 'Heartbeat from WCB1' inside the ETM,BOOT window. Repeat with `?ETM,BOOT,5`, then restore it. | L |
| boot.wdp_stagger_order | The periodic advert offset is computed from WCB_Number before it is loaded, so every board gets WCB1's 500 ms. The 60 s reschedule adds no jitter, so boards powered on together advertise in lockstep. | WCB.ino:9108 vs :9114; WCB_WDP.cpp:385, :1969 | Reboot W2 from its own console and turn on `?DEBUG,MGMT` within 3 s. Time from '[WDP] discovery enabled' to the first '[WDP] advert sent' after 55 s: it should be 61.0 s (60 s + 2 × 500 ms), and the bug gives 60.5 s (**BUG-16**). Send no POLL and make no config change during the window. | L |

### WCB-WP37: Static rule-12 check (M)
tests/hil/selftest.py, and optionally a build.yml step. **U, needs no bench, S.**

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| rule12.no_automated_include_check | Every WCB_<area>.cpp must include WCB_RemoteTerm.h first, or its output vanishes silently (rule 12). Today only a manual grep checks this. | CLAUDE.md rule 12; WCB_RemoteTerm.h | For each Code/WCB/*.cpp other than WCB_RemoteTerm.cpp and wcb_pin_map.cpp, the first 3 lines must contain WCB_RemoteTerm.h. Prove the check fails on a planted violation in a temporary copy. The tree complies today. | M |

### WCB-WP38: Maestro get replies to the controller (`:MQR`) (M)
s21_navicore_sbus.py. **U, S.**

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| maestro.mqr_reply_to_controller | A plain `;M<dev>,getX` from the controller (NaviCore, id 20) is rewritten to `;MG<dev>,20,…` and answered in the controller's format, `:MQR,<dev>,<chan\|0>,POS\|MOV\|ERR,<value>`, not as `;M!`. | WCB_Maestro.cpp:612-625, :662-675; WCB.ino:5356, :5605 | Probe rules on W1 S1: AA011000 answers 7017, AA0113 answers 01, AA0121 answers 0400. NaviCore sends WCB_SEND `{target:1, cmd:';M1,getPosition,0'}`, then getMovingState and getErrors. With `?DEBUG,MAESTRO,ON`, W1 prints '[MAESTRO] get reply -> WCB20: :MQR,1,0,POS,6000', then ':MQR,1,0,MOV,1' and ':MQR,1,0,ERR,4', and stores m1pos0=6000. NaviCore's DBG_MAESTRO logs the parse. Clear the variables and send `;M2,getErrors` afterwards. | M |

### WCB-WP39: WLED: one-hop cap, clear-all, first host, bare `;L` (M)
s24_wled_config.py. **U, M.**

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| wled.one_hop_no_reforward | A `;L<id>` that arrives over the mesh at a board holding only a proxy is never re-forwarded (the lastReceivedViaESPNOW cap). This is what stops two proxies pointing at each other from ping-ponging a command. | WCB_WLED.cpp:148-157 | On W2's console, `?WLED,7:W1S0:115200`; on W1, `?WLED,7:W2S1:115200`; `?DEBUG,ETM` on both. `;L7,ON` from W1: W1 logs one 'Sent seq N: ;L7,ON', and W2 logs no 'Sent seq … ;L7' within 3 s. Clear id 7 on both. | M |
| wled.clear_all_releases_local | `?WLED,CLEAR` releases every local WLED port: back to 9600, both flags ON, label cleared, and a 'Released S<n>' line each. It also wipes the proxies. | WCB_WLED.cpp:184-213 | With WDP off, add `?WLED,3:W1S4:9600` and `?WLED,4:W1S5:9600`, then `?WLED,CLEAR`. Expect both Released lines and '[WLED] All WLED configuration cleared'. The chain holds `?BCAST,OUT/IN,S4/S5,ON`, with no WLED tokens or labels. Re-add the baseline proxies and relabel. | L |
| wled.autoadd_first_host_wins | A WLED advert never shadows a local WLED of the same id, never moves a proxy to another host, and is skipped when all 9 slots are full. | WCB_WLED.cpp:417-447 | On W2, set a manual `?WLED,3:W11S0:9600`. W1 adds a local WLED 3 with WDP on and runs `?WDP,POLL`: W2 prints no 'auto-added' and no 'baud updated', and keeps its token. Then W1 adds `?WLED,PORT,S4:9600` (a local WLED 1) while W2 hosts WLED 1 on S2: W2's slot stays local. Clear both. | L |
| wled.bare_L_lowest_local | A bare `;L,<verb>` acts on the lowest-id local WLED. | WCB_WLED.cpp:127-138 | With WDP off, add `?WLED,4:W1S4:9600` and `?WLED,3:W1S5:9600`. `;L,ON` puts `{"on":true}` on S5 only. Clear both and relabel. | L |

### WCB-WP40: Wizard, node unit tests of parser.js (M)
tests/wizard/unit/*.test.js (`node --test`, also run in CI). parser.js is the only Wizard file exported for node.
Behaviour that lives in app.js goes to WP41. **U, needs no board, M.**

> **Status 2026-09-29: done** (`wizard.parser`, `20260929-101257`). `tests/wizard/unit/model.test.js` (under `wizard.parser` and CI): row 3 as a walk over
> every field of the model, so a field added later without a `diffConfigs` line fails it (the six fields have been in
> `diffConfigs` since W-11); row 4 (the mapping and PWM-output round trip, and that an added or edited mapping re-sends
> the table while a removed mapping or output port builds nothing - left as found); row 8 (every claim); row 9 (WiFi,
> WDP); row 10 (Kyber local); and the parser halves of W-17 and W-20 as `(should)` node `todo`s. Rows 1, 2, 5, 6 and 7
> were already pinned (`roundtrip.test.js` W-1, W-2, W-3, W-6; `pull.test.js` W-7).

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| wiz.parser.etm_delay_zero | An ETM char delay of 0 is legal (WCB.ino:6245-6256), but the Wizard parses it as 100 (`\|\| 100`), so any full push writes 100. | parser.js:892; app.js:1363 | Parse `?ETM,DELAY,0`: expect etm.messageDelayMs === 0, and a full push containing ETM,DELAY,0. Fails today (W-1). | M |
| wiz.parser.controller_custom_id_disabled | A disabled controller keeps its custom id. The firmware emits `CONTROLLER,ON,<id>^CONTROLLER,OFF`, but a full push emits only CONTROLLER,OFF, which re-parses as id 20. | parser.js:545-556, 1346-1350; WCB.ino:3614-3621 | Parse `CONTROLLER,ON,15^CONTROLLER,OFF`, full-push it and re-parse: expect specialPeer false and specialPeerId 15. Fails today (W-3). | M |
| wiz.tests.diffconfigs_blind_fields | diffConfigs never compares alias, specialPeer, specialPeerId, wdpEnabled, wdpAutoJoin or statusLedPin, so a builder that drops them passes every round-trip test. | parser.js:1726-1768; unit/parser.test.js:21-25, 64-120 | Add the six fields to diffConfigs, or compare with a deep-equal minus the documented derived fields (kyber.targets, livePeerCount, source, claimedBy, fwVersion). Use a fixture with all six at non-default values. | M |
| wiz.parser.mappings_pwm (parser half) | Serial mappings (the R flag, local and W<n>S<n> destinations), PWM inputs and PWM OUT declarations: parse, full-push build and diff. A push never clears a removed mapping or OUT port, which builds as ''. | parser.js:938-1015, 1647-1669 | Round-trip `MAP,SERIAL,S2,R,S3,W2S4^MAP,PWM,OUT,S5^MAP,PWM,S1,S4,W2S3`, with diffs for add, edit and remove. Pin that a removed OUT port builds nothing. | M |
| wiz.chars.comma_delim_and_char_bootstrap (parser half) | `?DELIM,,` parses to '' (`parts[1] ?? '^'`), and buildCommandString then joins commands with ''. Per-line funcChar detection is untested. | parser.js:343-355, 586-597 | Parse backups captured under `?FUNCCHAR,!` and `?DELIM,,` (and with CMDCHAR changed from `;` to `/`). The characters must round-trip; ',' fails today (W-2). | M |
| wiz.pull.remote_legacy_reply_unverified (collector half) | A legacy `[MGMT:CONFIG,n]` body becomes the config and the baseline without its `^?CHK` being verified. | parser.js:1944-1950; app.js:8570-8572 | Feed the collector a legacy line with one byte changed, and one cut before `^?CHK`. Pin today's behaviour (stored), or expect a rejection once verification exists (rule 15 background). | M |
| wiz.file.system_file_io (parser half) | buildSystemFile writes every board as a `[WCB n]` chain with no client token, so a client slot reloads as a WCB slot without its clientAlias. | parser.js:1171-1273, 1679-1719 | Build a system file with client slot 20 and parse it back: expect its type and clientAlias preserved. Fails today (W-6). | M |
| wiz.claims.port_claims_ui (parser half) | evaluatePortClaims for MP3, HCR, DFP, WLED, a PWM source and local destination, PWM OUT, and the soft serial-map claim. | parser.js:1047-1168 | One config per device type, asserting claimedBy for every port. | M |
| wiz.card.wifi_wdp_fields (parser half) | WIFI,JOIN (with a comma in the password) and WIFI,OFF round-trip. Re-enabling from WDP,OFF or WDP,AUTOJOIN,OFF must emit the ON forms. | parser.js:532-541, 641-667, 1328-1336, 1428-1446 | Round-trip `WIFI,JOIN,ssid,pa,ss` and `WIFI,OFF`. Parse WDP,OFF and AUTOJOIN,OFF, then re-enable: expect WDP,ON and WDP,AUTOJOIN,ON. | M |
| wiz.card.kyber_local_parse_marc (parser half) | Parsing `?KYBER,LOCAL,S<n>,M<id>:W<w>S<p>:<b>`, and the Marcuino port inferred from the 'Kyber Marcuino' label. | parser.js:296-303, 673-716 | Parse `KYBER,LOCAL,S2,M1:W1S1:57600,M2:W2S1:115200` with `LABEL,S3,Kyber Marcuino`. Expect the mode, the port, marcduinoPort 3 and the targets. | L |

### WCB-WP41: Wizard, Playwright with no board: app.js logic and panels (M)
New specs that use page.evaluate plus remote_pull_fake's `setup()`; they run in CI. **U, L.**

> **Status 2026-09-29: done, no board (`20260929-101257`).** `tests/wizard/specs/app_fake.spec.js`, `editors_fake.spec.js` and
> `flasher_fake.spec.js`: row 2 `wizard.app_fake_controller`, 3 `_etm_listener`, 4 `_wdp_panel`, 5 `_mesh_tick`, 6
> `_setup_wizard`, 7 `_partial_ack`, 8 `wizard.flasher_fake_helpers` (a fake esptool served in place of the vendored
> bundle, `tests/wizard/lib/fake_esptool_wcb.mjs`), 9 `_serial_claims`, `_system_file` and `wizard.editors_fake_mappings`
> (W-1, W-2, W-5 and W-7 were already push_fake/remote_pull_fake), 10 `_identity`, 11 `_terminal`, 12 `_fw_check`, 13
> `_relay_card`; the no-board halves of WP21 rows 4, 7 and 8 (`wizard.editors_fake_seq_var`,
> `wizard.app_fake_hub_flash_refused`, `wizard.editors_fake_live_controls`); and the `(should)` specs
> `wizard.app_fake_pending_funcchar` (W-13), `_system_file_reload` (W-17), `_wcb_number_above_floor` (W-18),
> `_pull_leaves_nothing_pending` (W-19) and `wizard.editors_fake_bidir_remove` (W-21). Row 1 was
> `wizard.push_fake_seq_roundtrip`. The plan against the code: `_suppressedEtmEdges` is a Set of boards, so row 3's "2
> edges" are two boards'; row 4's dump is built from the firmware's printf formats (WCB_WDP.cpp), not captured from W1;
> row 12's check reads the Contents API listing of Code/bin and takes the version from the bin name, not from a release,
> and returns undefined rather than false when GitHub answers an error (app.js:170; its callers test truthiness only).
> Seen and left alone, no behaviour at fault: the ETM listener still toggles a `#b<n>-dot` the board template no longer
> has (app.js:8241, :8262); a push re-pulls every connected board any `W<n>S<p>` in its commands names, WLED and Maestro
> lines and sequence text included (app.js:8113-8124); a push with an unanswered command shows "Pull Failed" until its
> verify pull lands (app.js:8097); flasher.js's branch whitelist admits `../` though its comment says it keeps `/../` out
> (flasher.js:56-61), harmless because the branch is only ever a `?ref=` query value.

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| wiz.seq.editor_roundtrip_not_identity | Sending some firmware-legal values through seqTextareaToValue(seqValueToLines(v)) rewrites them once. Examples: ';w2 ***note' becomes ';w2***note', 'a^^b' becomes 'a^b', spaces around ^ are dropped, and 'x^***inline' becomes 'x***inline'. The result is one unrequested `?SEQ,SAVE` per affected key, after which it settles. | app.js:4364-4451, 4788-4798; parser.js:1631-1636 | Run a corpus through page.evaluate: IF groups with `;t`, `^^***` own-line comments, `^***` inline comments, a space before `***`, spaces around `^`, commas, and leading comments. Assert identity, or assert that appendSequenceRow, getSequencesFromUI and buildCommandString(cfg, baseline) emit no `?SEQ` for an untouched row. | L-M (verifier) |
| wiz.controller.selector_and_navicore_id | The Controller selector (None, NaviCore, Kyber) stashes and restores board roles on each flip, pushes CONTROLLER,ON,<id> to WCB slots only, and reconciles Kyber labels. deriveControllerFromBoards runs on load, and onNavicoreIdChange rewrites every board's id. | app.js:1396-1531 | Use three configs: Kyber local, remote, and a client. Flip NaviCore, Kyber, NaviCore: the roles come back as they were. Each board's build carries CONTROLLER,ON,<id> only on WCB slots, and nothing is left dirty after the flip back. | M |
| wiz.relay.etm_online_listener | On a relay's stream, '[ETM] WCBn came ONLINE' sets the dot and badge, starts remoteBoardPull and sends RTERM,START. 'went OFFLINE' starts a single verify pull. During an OTA both edges are deferred and replayed afterwards. | app.js:2030-2045, 8110-8176 | With the fake relay from `setup()`, call `installEtmListener(1)`. Feed 'came ONLINE (boot)': `?MGMT,PULL,2,P` and RTERM,START appear in `__sent`. Feed 'went OFFLINE' twice: one verify pull. With `_otaInProgress` set, nothing is sent and 2 edges wait in `_suppressedEtmEdges`. | M |
| wiz.wdp.mesh_panel_and_da_forget (half with no board) | parseWdpDump must read every firmware line: WDP with PEER, WDPIF, WDPDA with SEEN and AGE (including AGE=-), WDPX, WDPPWM, and WDPCFG with EN. renderWdpMesh must show saved-but-quiet DA devices. Forget, Clear, AutoJoin and Poll each send their command. | app.js:13087-13368; WCB_WDP.cpp:1215, 1522, 1619, 1786-1850 | Feed parseWdpDump a dump captured from W1 that includes a WDPDA line with SEEN=0 and AGE=-. Assert the node, da, pwm and cfg fields and the rendered rows. Forget on a W2 device sends `MGMT,FRAG,2,…,?WDP,DA,FORGET,S3,<type>`. | M |
| wiz.wdp.mesh_autodiscover_tick | Every 12 s the page sends `?WDP,DUMP`. Unknown WCBs appear as sections, with no auto-pull and the alias seeded from the advert. Client cards are created or updated. A temporary client above the floor vanishes, while other clients show Offline. The tick is skipped during a push, flash, OTA or stats capture, because a dump line could pass for an ACK. | app.js:13371-13512 | A fake connection answers `?WDP,DUMP` with a canned dump: assert the sections, the client card and the alias seed. With a slot in `_pushingBoards`, no `?WDP,DUMP` is sent. | M |
| wiz.setup_wizard.flow | The guided setup wizard. Steps are validated and saved. wizardApplyConfig sets Kyber local (the Marcuino port, and targets from every board's Maestros), remote mode on Maestro boards under NaviCore, the maestroTable, ETM and the controller. The exported file parses back. | app.js:10105-11893, 12441-12590 | Call splashGoWizard and set up two boards: Kyber on board 1 (S2, Marcuino S3) and Maestros on boards 1 and 2. After apply, assert boardConfigs and buildCommandString: the KYBER,LOCAL targets, the MAESTRO table, and remote mode on board 2. The export parses back to the same configs. | M |
| wiz.push.partial_ack_and_errors | ACK pacing retries a command once. No response after the retry gives ok:false, an error badge and a verify pull. 'Board not connected' is refused. Remote PWM destination boards are re-pulled after a push. | app.js:7373-7379, 7873-7905, 7978-8015 | A fake connection returns `{ok:false}` twice for one command. Expect that command sent twice, the rest still sent, `boardPushOutcome` `{ok:false, reason:'some settings got no response after retry'}`, and a scheduled boardPull. | M |
| wiz.flash.helpers | Flasher logic that needs no board. A detected chip overrides the HW selection, and S2 and C3 are refused. No chip id plus no HW version is refused. The S3 bootloader is picked by flash size (8 or 16 MB; null skips it). A partition-table mismatch turns an app-only flash into a full flash. getFirmwareBranch honours the `/dev/<branch>/Wizard` path and the localStorage override, and isValidFwBranch rejects URL operators. | flasher.js:62-81, 246-293, ~395-445 | Use page.evaluate with stub loaders: readFlash returning equal and differing bytes, getFlashSize in KB, readFlashId with size bytes 0x17 and 0x18, and CHIP_NAME 'ESP32-S2'. Call getFirmwareBranch with stubbed pathname and localStorage values such as 'main?x=1' and 'feature/x'. flasher.js is not exported for node, which is why this is not a unit test. | M |
| (the app.js halves of WP40's rows 1 and 4-8) | The General ETM input must keep a delay of 0. syncMappingsToConfig's `bidir` key re-sends every mapping (PWM inputs cause a reboot) after an unsaved edit, and a removal made while disconnected leaves the mapping on the board. boardGo bootstraps `?DELIM,^` from '', reverting a ',' board. The legacy reply's retry path. Export/import round-trips slots 1, 2 and client 20, refuses duplicate WCB numbers, and keeps a board above WCBQ. syncSerialUIToConfig must not read back hard-claimed ports' BCAST boxes. | app.js:1353-1390, 3124-3131, 3816-3898, 7820-7870, 8570-8768, 9099-9222, 11795-11893 | Run each gap's own how (see the JSON) through the page with a fake connection. W-1, W-2 and W-5 fail today. | M |
| wiz.card.identity_fields | The HW select shows the LED group for 3.x boards without rewriting the pin on load. The LED pin is a preset or a custom value from 0 to 48. The alias is cut at 24 characters. A WCB number change pushes WCB,<n> with a reboot, and the next pull migrates the slot. A client slot is never pushed. | app.js:2056-2214; parser.js:1293-1307 | Pull HW 32 with LED,PIN,47 into the DOM: a push with no edits sends nothing. Picking Other with 21 sends LED,PIN,21. Loading HW 31 does not mark the board dirty. Changing the number to 3 builds WCB,3, and commandStringNeedsReboot returns true. A client slot has no Push button and boardGoAll skips it. | L |
| wiz.terminal.debug_and_filter | The debug toggles send `?DEBUG,<mode>` and track the state per board. `_suppressTerminalLine` hides floods from config tools and telemetry. Timestamps, autoscroll and pane visibility work. | app.js:9319-9420, 9559-9610 | Feed rc_hb and rc_ch JSON and WDP dump lines to termLog: they are hidden, while ordinary lines show. | L |
| wiz.misc.fw_update_check | The latest-release fetch, parseWCBVersion on DTG-stamped versions, the update badge, and warnStaleFwBranchOverride. | app.js:156-348 | Route api.github.com to return release 6.2.3_…. A board on 6.2.1_… shows the badge, an equal version does not, and a malformed version does not crash the page. | L |
| wiz.relay.mgmtrelay_card | A `?RELAY,1` device gets a relay card outside the grid, anchored at its WCB number, and is left out of Push All and the export. | app.js:524-630, 7173-7207, 8998-9004, 9174 | A fake connection's backup includes `?RELAY,1` and `?WCB,19`. Expect `_relaySlots` to hold 19, no section-board-19, a rendered relay card, boardGoAll skipping it, and an export with no [WCB19]. | L |

### WCB-WP42: Full NVS (opt-in `nvs_fill`) (M)
s31_password_erase.py, run inside seq.nvs_full_consistency's fill. **O, M.** Row 1 needs BUG-13 fixed.

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| var.full_nvs_false_ack | On a full store, saveVarsToNVS fails but `?VAR,SET` still prints its '[persistent]' line, the ACK the Wizard waits for. The value is gone after a reboot, and a cleared persistent variable comes back at boot. | WCB_Variables.cpp:84-102, :175, :198-205, :332 | Once the store refuses a 40-character sequence, send `;VP,hilp,1` and `?VAR,SET,hilq,2`. Expect the ERROR line and, as a `(should)`, no '[persistent]' ACK (**BUG-13**). `?VAR,CLEAR` a persistent variable made before the fill. Free the store and reboot: hilp and hilq are absent, and the cleared variable does not come back. Replay the saved tokens. | M |
| map.count_not_stored_full_nvs | saveSerialMonitorMappings notices a failed sm<i>_cnt write and warns 'NVS could not store it (full?)'. It skips removing output keys past the new count, so no W0S0 destinations appear, and writes `_act` last. This is the F20 review fix, and it has never run on a board. | WCB_Storage.cpp:2290-2353 | Before filling, map S2 to 10 destinations. After filling, re-issue `?MAP,SERIAL,S2,S4` and add `?MAP,SERIAL,S3,W3S1`: expect 'could not store it'. Free the store and reboot: S2 comes back with its old 10 destinations and no S0/W0S0, and S3 is absent. Clear both and check that `?NVS` serial_map is back at baseline. | M |
| storage.full_nvs_other_setters | `?BAUD`, `?LABEL`, `?BCAST`, `?ALIAS`, `?WCB`, `?MAC`, `?ETM` and `?DELIM` ignore failed put*() calls and print success. `?config` then reloads serial_baud and bcast_block from NVS, silently reverting the RAM tables. | WCB_Storage.cpp:196-201, :234-266, :347-355, :1909-1922; WCB.ino:2752-2753 | With the store full, send `?LABEL,S5,HILFULL`, `?BAUD,S5,19200` and `?BCAST,OUT,S4,OFF`. Record each success line, then `?config` and the chain: does the baud revert? Free the store, reboot, and record which settings survived. Restore the baseline. Characterisation (**BUG-20**). | L |
| wdp.da.nvs_save_failure | A failed wdp_da save is reported and retried 60 s later. | WCB_WDP.cpp:1080-1091 | Save a HIL device while the store is full: expect '[WDP-DA] could not save the device list to NVS (full?) - retrying in 60 s'. Free the store; within 60 s the save lands, and a reboot reloads the device. | L |

### WCB-WP43: Mesh-password window (opt-in `mesh_password`) (M)
s31_password_erase.py, run inside ident.epass_live. **A, S.**

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| ota.packet_auth_gate | OTA packets from a board with a different ESP-NOW password, or addressed to another board, are ignored. This is the only credential on a path that can reflash a board. A relay ignores OTA ACKs not addressed to it. | WCB_OTA.cpp:392-394, :401-468 | Give W1 a throwaway `?EPASS`, then send `?OTA,BEGIN,2,<s>,4096,1`. Family 1 is refused by W2's brick guard even if auth fails open, so nothing is erased. Expect no `[OTA:ACK,2,<s>,…]` within 8 s and no '[OTA] BEGIN rejected' on W2. The another-board half needs a crafted packet (WP59). | M |
| wcb.cmd.epass_boundary_persist | A 39-character `?EPASS` is accepted; the limit is sizeof-1. Persistence across a reboot is already covered by nvs.erase_defaults_restore. | WCB.ino:6306-6317; WCB_Storage.cpp:531-537 | `?EPASS,<39-character throwaway>` prints the confirmation. Restore W1's own token, and never print the credential. | L |
| rterm.stop_flush_and_password (password half) | A relay drops remote-terminal packets that carry a different mesh password. | WCB_RemoteTerm.cpp:160 | With W2 on a throwaway `?EPASS` and mirroring to W1, no `[TERM:2]` line appears. | L |
| wcb.mgmt.request_filters (wrong password) | A MGMT request with the wrong password is ignored. | WCB.ino:4144-4156, :4582-4611 | During the window, W1's `?MGMT,PULL,2` and `?MGMT,SEQ,2` go unanswered. | L |

### WCB-WP44: Sequence bookkeeping keys (opt-in `seq_wipe`) (M)
s17_seq_inventory.py. **O, S.** Row 1 needs BUG-14 fixed.

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| seq.reserved_bookkeeping_keys | `?SEQ,SAVE`, `?SEQ,CLEAR` and `;C` accept the NVS bookkeeping keys. CLEAR,key_list unlists every sequence while the values stay in NVS. SAVE,key_list,x corrupts the list. CLEAR,seq_mig_done re-arms the legacy migration for the next boot. `;Ckey_list` runs the name list as broadcasts. | WCB_Storage.cpp:601-610, :707-839; WCB.ino:6524-6571 | After the guard (**BUG-14**), a `(should)` test: `?SEQ,SAVE,key_list,x`, `?SEQ,CLEAR,key_list`, `?SEQ,CLEAR,seq_mig_done` and `;Ckey_list,L` are each refused, and `?SEQ,NAMES`, the hash and `?SEQ,GET,key_list` stay unchanged. Until then, run it like seq_wipe: snapshot the `?SEQ,SAVE` tokens, send `?SEQ,CLEAR,key_list`, replay every token, and assert the chain is identical. | M |
| seq.clear_all_restamps_migration | `?SEQ,CLEAR,ALL` and `?CCLEAR` re-stamp seq_mig_done after preferences.clear(), so deleted sequences stay deleted at the next boot. | WCB_Storage.cpp:960-970 | Inside seq.clear_all_empty_hash, after each clear-all, `?SEQ,GET,seq_mig_done` must return `[MGMT:SEQVAL,1]seq_mig_done,OK,`. | L |

### WCB-WP45: WiFi JOIN robustness (opt-in `wifi_modes`) (M)
s28_wifi.py. **O, M.**

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| wifi.join_lost_and_rejoin | A JOIN whose AP disappears prints 'lost "<ssid>" — retrying every 5 s' and rejoins with 'joined … after N attempt(s)'. | WCB_WiFi.cpp:233-238, :262-288 | Inside join_w2_ap, once W1 has joined W2's AP, reboot W2. W1 prints 'lost …' and then 'joined …' within 45 s. `?WIFI` shows Association connected, and a unicast to W2 is delivered. Restore W1's AP line and reboot. | M |
| wifi.join_absent_ssid_keeps_mesh | A JOIN to an SSID nobody hosts retries forever on the pinned mesh channel, printing 'still looking' at attempt 1 and every 12th attempt, and never sweeps channels. | WCB_WiFi.cpp:191-201, :279-287 | Send `?WIFI,JOIN,HIL-NOSUCH-<n>,password1` and reboot. Expect the 'will join' line, then 'still looking (attempt 1)' and '(attempt 12)'. For 70 s, `;W2` unicasts are ACKed, with no retries or failures in `?STATS` and no MISMATCH on the `?WIFI` radio line. Restore the AP. | M |

### WCB-WP46: ETM sequence wrap (new opt-in, e.g. `etm_seq_wrap`) (M)
s18 or s99, with a new key in hil/optin.py for the roughly 10 min flood. **O, M.** Fixing the firmware first and then
adding a host test of the ring is cheaper.

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| wcb.etm.seq_wrap_zero | The uint16 sequence counter pre-increments, so after 65,535 sends (JSON broadcasts included) it reaches 0. The dedup ring uses 0 to mean an empty slot. A seq-0 command reaching a receiver whose ring for that sender still has an empty slot is ACKed and never executed. | WCB.ino:658, :686-703, :2937, :3041, :5173-5176 | Reboot W2 so its ring for W1 is empty. Drive W1's counter to 65,535 with about 65k untracked `{"h":n}` USB lines. Send `;W2,;S2<m>` with `?DEBUG,ETM` on both boards: the marker must arrive on W2S2. Today W2 prints '[ETM] Duplicate seq 0 from WCB1 - ACKd but not executed' (**BUG-19**). | M |

### WCB-WP47: Non-default settings across a reboot (L)
s26_boot_restore.py, one reboot per setting. **U, L.**

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| wdp.cmd.autojoin_off_persists_reboot, persist.chars_and_flags_reboot, chars.funcchar_cmdchar_persist_reboot, wcb.cmd.prefix_chars_reboot, bcast.s0_echo_persist_reboot, wcb.cmd.etm_settings_persist_all, etm.remaining_settings_persist, help.debug_not_persisted | These settings must survive a reboot: FUNCCHAR and CMDCHAR; the S0/USB broadcast echo (its default is OFF, unlike S1-S5); `?WDP,AUTOJOIN,OFF`; and ETM MISS, BOOT, COUNT and DELAY. The RAM-only `?DEBUG` flags must come back OFF. | WCB_Storage.cpp:353-364, :540-560, :2725-2765; WCB_WDP.cpp:1944-1957; WCB.ino:5883-5953, :9181, :9428-9438 | One W1 reboot per setting, each restored before any chain comparison. CMDCHAR: set `?CMDCHAR,:`, reboot; `:S0x` echoes and `?config` shows ':'; restore `?CMDCHAR,;`. FUNCCHAR: set `?FUNCCHAR,!` and reboot by sending `!reboot` through dev.send (`w.reboot()` sends `?reboot`); `!VERSION` then works and `?VERSION` is treated as broadcast text. Both are in NO_AUTO_RESTORE_WHILE. S0 echo: `?BCAST,OUT,S0,ON` still echoes after the boot. Auto-join: `?WDP,AUTOJOIN,OFF` shows WDPCFG AUTOJOIN=0 and '?WDP,AUTOJOIN' reports OFF; there is no banner line for it. ETM: `?ETM,MISS,6`, BOOT,3, COUNT,25 and DELAY,150 all show in `?config` and the chain. Debug: after `?DEBUG,ON` and `?DEBUG,ETM,ON` and a reboot, `;V,hilv,1` echoes nothing and a `;W2` prints no ETM lines. Rebooting with ETM or CHKSM off stays opt-in (rule 4). | L |
| peers.controller_persist_reboot | `?CONTROLLER,OFF` and `ON,<id>` persist. After OFF and a reboot, NaviCore's first advert re-enables the controller, because the WDP neighbour table lives in RAM only. | WCB_Storage.cpp:434-470; WCB.ino:6430-6465 | Keep AUTOJOIN off. Send `?CONTROLLER,OFF` and reboot: the banner has no 'Added ESP-NOW special peer', and then '[WDP] heard controller … auto-enabling controller peer' prints, a line that fires only when OFF was loaded. Send `?CONTROLLER,ON,17` and reboot: expect 'Added ESP-NOW special peer WCB17'; auto-adopt never overrides a set id. Restore ON,20. Use 17, not 19, which is in FORBIDDEN_MESH_IDS (common.py:296-297). | L |
| devices.onerr_volume_reboot | The MP3 and DFP ONERR keys, and the DFP current-volume shadow (NVS 'vol'), survive a reboot. | WCB_MP3.cpp:415-453; WCB_DFP.cpp:375-413 | On W2, save HILK and HILE markers. Configure MP3 on S3 and DFP on S5, each with ONERR,HILE, and send `;D,VOL,25`. Reboot with `;W2,?reboot`. Expect '[DFP] Loaded: … default vol=20  current vol=25' and 'On Error : HILE' in both LISTs. An injected 'E' on S3 and a 7E FF 06 40 … EF frame on S5 each recall `;S<marker>`. Clean up: CLEAR, remove the sequences, relabel, and `_unlearn`. | L |

### WCB-WP48: WDP table and dump, controller ids, alias, ETM queries, `?STATS,RPT` (L)
s18_etm_config_wdp.py. **U, M.** Row 4 needs the BUG-30 legend fix.

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| wdp.autojoin.two_advert_vetting_fw | Auto-join acts only on the second advert from an id, so a single stray packet cannot inject a peer. | WCB_WDP.cpp:675-677 | Timestamp the lines with `_timed` (s18:272): 'temporarily joined WCB15' must come at least about 1 s after 'learned WCB15', on the second advert of the boot burst. A strict version needs a probe verb that sends exactly one advert (WP59). | L |
| wdp.table.stale_after_ttl | A neighbour silent for more than 180 s goes stale but keeps its row: LIST shows 'stale', DUMP shows SEEN=0, and the detail view shows '(stale)'. | WCB_WDP.cpp:406-412, :1650, :1672, :1716, :1826 | Turn AUTOJOIN off and join the probe with `probe_in_mesh(temporary, forget=False)` at 15; it is learned but never joined, so never evicted. Leave and wait about 185 s. DUMP shows N=15 SEEN=0, LIST shows 'stale', and `?WDP,15` shows '(stale)'. Clean up with `?WDP,FORGET,15`. | L |
| wdp.dump.neighbor_fields | The DUMP's WDPX MB= and WL= id@baud lists, the MAESTRO= list, HW=, and the neighbour-side WDPPWM edge all match the advertising board's config. | WCB_WDP.cpp:1613-1620, :1785-1788, :1814, :1844-1846 | Extend wdp.dump_fields. For self and for W2: MB= equals the chain's local `?MAESTRO` ids@baud, WL= the local `?WLED` ones, MAESTRO= the same ids, and HW= the `?HW` token. In pwm.passthrough_mesh, read W2's DUMP for `[WDPPWM:N=1,DST=2,S=3]`. WCB20's MAESTRO= and a client's HW=0 are already covered. | L |
| wdp.cmd.list_detail_content | The `?WDP,LIST` Cap and Maestros columns, and the `?WDP,<n>` lines: Capabilities, '(controller ID n)', Maestros, WLED, and Interfaces. The legend lacks D=DFPlayer. | WCB_WDP.cpp:1537-1553, :1623-1654, :1679-1739 | W2's Cap column must equal the codes from `_expected_cap` (s18:1124), and its Maestros column must match. `?WDP,2` shows 'Capabilities: …', 'Maestros    : <id>@<baud>', 'WLED        : …', and '(not heard)' for a quiet DA device. Add D to the legend (**BUG-30**) and update s18:1190. | L |
| wdp.controller.no_readopt_no_override | Controller auto-adopt fires only on the FIRST learn of a controller-type client, and only when no controller is set. It never re-enables a controller that is OFF and never switches a pinned id. 'Sabe' and 'Sabé' also qualify. | WCB_WDP.cpp:429-434, :646-650 | Keep AUTOJOIN off throughout. (a) With `?CONTROLLER,OFF` and NaviCore's row still valid, `?WDP,POLL` and wait 3 s: AGE resets, no 'auto-enabling' line, and it stays DISABLED. (b) Set `?CONTROLLER,ON,17`, `?WDP,FORGET,20`, then POLL: 'learned WCB20' appears but the controller stays 17. (c) Set OFF and join the probe (temporary) at 14 with dev_type=Sabe: the controller auto-enables at 14. Restore `?CONTROLLER,ON,20`. | L |
| wcb.cmd.controller_other_id_persist, peers.controller_id_switch | `?CONTROLLER,ON,<id≠20>` moves the controller live and persists it. The switch deletes the old out-of-band peer and clears a learned bit held by the new id. Commands from a disabled controller are still ACKed and executed. | WCB.ino:5067, :6430-6465, :8677-8715 | With AUTOJOIN off, `?CONTROLLER,ON,17` prints '…WCB17 registered (live).'. `?CONTROLLER` reports ID 17, the chain holds `?CONTROLLER,ON,17`, `;W20` is unreachable, and `?STATS` shows WCB17. Reboot: all the same. `?CONTROLLER,ON,20` makes WCB20 reachable again, with `?PEERSLIVE` unchanged. With the controller OFF, NaviCore's TEST_ACTION wcb_unicast `;W1,;S2<m>` still runs on W1S2. | L |
| alias.utf8_truncation | `?ALIAS` truncates to 24 bytes and backs up to a UTF-8 boundary. | WCB_Storage.cpp:253-259 | `?ALIAS,` + 23 ASCII characters + 'é' gives 'WCB alias set to: <the 23 ASCII characters>'. 22 ASCII characters + 'é' keeps all 24 bytes. Restore the alias. | L |
| wcb.cmd.etm_query_and_range_forms, wcb.cmd.etm_bounds_and_query | Bare `?ETM,TIMEOUT`, BOOT and DELAY are queries. Out-of-range values are refused: TIMEOUT 50-10000, BOOT 1-30, DELAY 0-5000. The legacy `?ETMTIMEOUT`, `?ETMBOOT`, `?ETMCHARDELAY` and `?ETMMISS` have matching query and range forms. | WCB.ino:6182-6258, :6882-6939 | Under config_guard, the bare forms print their 'is' lines, matching the chain. TIMEOUT,49 and 10001, BOOT,0 and 31, and DELAY,-1 and 5001 print their exact Invalid lines. The bare legacy forms are queries, and legacy out-of-range values print '(currently N)'; try `?ETMBOOT40` and `?ETMCHARDELAY6000`. | L |
| wcb.stats.rpt_ram_only_and_large | `?STATS,RPT` rows live in RAM only, so a reboot clears them, and counters above 2^31 are stored exactly. | WCB.ino:668-685, :2089-2130 | `?STATS,RPT,7,3000000000,1,1,1,1,1,1` gives 'WCB7: Sent: 3000000000'. In etm.reboot_stats_rpt's `_finish_reboot`, assert that no WCB2 'Reported by Other Nodes' row survives, allowing one under 30 s old because NaviCore reports again. | L |

### WCB-WP49: WDP-DA edge cases (L)
s12_serial_input.py, beside the input.wdpda_* tests. **U, M.**

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| wdp.da.changed_facts_resave | A saved DA device that re-announces with a new fw, hw or caps updates its record, re-saves NVS, and re-sends the list. | WCB_WDP.cpp:1016-1024 | On W2 S4, save HILDP fw 1.0 with two announces, then announce fw 2.0. W2's `?WDP,DA` shows 2.0, and within about 3 s W1 shows `[WDPDA:N=2,S=4,TYPE=HILDP,FW=2.0,…]`. Reboot W2: it reloads with FW=2.0. Forget it in the finally block. | L |
| wdp.da.manual_label_wins | A manual `?LABEL` keeps naming the port in the advert and DUMP while DA devices are listed under it. Clearing the label lets the first-heard device type name the port. | WCB_WDP.cpp:312-316, :1777-1779 | On a labelled W1 port, save a HIL device. `[WDPIF:N=1,S=p,DEV=<label>]` is unchanged, and the `[WDPDA]` record is present. `?LABEL,CLEAR` that port (under config_guard): WDPIF becomes the device type. Relabel and forget. | L |
| wdp.da.multiframe_over_air | A device list longer than one frame goes out one frame every 25 ms and is assembled all or nothing, in list order. | WCB_WDP.cpp:1290-1363, :1440-1500 | On W2 S4, save four HIL types (24-character type, 27-character fw and hw, 48-character caps; about 120 B each). W1's DUMP lists all four, whole and in order, and `?WDP,2` shows them. Forget one: W1 shows three. Overflowing the 32-record pool needs more neighbours than the bench has. | L |
| wdp.clear.remote_da_lists_reset | `?WDP,CLEAR` drops the neighbours' DA lists and their assembly state but keeps this board's own. After a POLL, the neighbour's unchanged list (same hash) is accepted again. | WCB_WDP.cpp:1451, :1920-1927 | With a saved HIL device on W2 and AUTOJOIN off, `?WDP,CLEAR` on W1 removes every `[WDPDA:N=2]` line and leaves W1's own `?WDP,DA` unchanged. `?WDP,POLL`, then 3 s later the same record is back. | L |

### WCB-WP50: Serial mappings: combined clear, NVS keys, saved flags, mesh-to-soft-port queue (L)
s13_serial_mapping.py. **U, M.**

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| pwm.map_clear_all_combined, wcb.cmd.map_clear_all (2 entries), map.clear_all_root, help.map_clear_all_combined | `?MAP,CLEAR,ALL` clears both serial and PWM mappings and prints 'All serial and PWM mappings cleared'. It ALWAYS queues a deferred reboot (clearAllPWMMappings(true)), clears pwm_mappings and pwm_outputs, and sends CLEAR,OUT plus `?REBOOT` to remote targets. | WCB.ino:6040-6043; WCB_PWM.cpp:590-658 and WCB_PWM.h:52 (HEAD) | Run on W1, which has no pwm_outputs; skip, or replay, if the chain holds `?MAP,PWM`. Add `?MAP,SERIAL,S2,S4` (and optionally `?MAP,PWM,S3,W2S3` with its reboot), then `?MAP,CLEAR,ALL`. Expect 'All PWM mappings cleared', 'Rebooting once the command queue drains.' and 'All serial and PWM mappings cleared', then exactly one reboot. Afterwards both lists are empty, there are no `?MAP` tokens, S2's flags are restored, and W2 has no OUT,S3. Decide whether a serial-only clear should reboot (**BUG-32**). | L |
| map.shrink_frees_output_keys | Re-issuing a mapping with fewer destinations removes the surplus sm<i>_<j>w/p keys, two per dropped destination. | WCB_Storage.cpp:2167-2181, :2305-2310 | Read `?NVS` serial_map. Map S2 to map.max_ten's 10 absent-board destinations and read it again (N). `?MAP,SERIAL,S2,S4` leaves N-18. Clear: back to baseline. | L |
| map.prev_flags_survive_reboot | The broadcast flags a port had before it was mapped (sm<i>_pbo and _pbi) persist, so a CLEAR after a reboot restores a deliberately OFF port. | WCB_Storage.cpp:1961-1965, :2330-2338, :2356-2408 | `?BCAST,OUT,S2,OFF`, `?BCAST,IN,S2,OFF`, `?MAP,SERIAL,S2,S4`, reboot, then `?MAP,SERIAL,CLEAR,S2`: the chain holds both OFF tokens. Restore both to ON (1 reboot). | L |
| wcb.softserial.mesh_out_queue_drop, wcb.softserial.mesh_out_hold_during_rebegin, wcb.mesh.soft_out_queue | Mesh chunks bound for S3-S5 go through a 16-slot queue. When it is full the chunk is dropped, counted, and reported as '[SOFTSERIAL] mesh-to-port queue full: dropped N chunk(s)'. A chunk is held while `?BAUD` re-begins its port. | WCB.ino:2328-2425 | (a) The probe sends 30 back-to-back 177-byte sendRaw chunks to W1 S4 at 9600 (about 185 ms each on the wire). What arrives must be whole chunks in order, and the drop line must count the missing ones. (b) Alternatively, map W1 S2 (115200) raw to W2 S3 at 9600 and inject 4 KB: a drop line on W2, an in-order subsequence, and no reboot. (c) With traffic flowing, send `;W2,?BAUD,S3,9600` five times, or W1 `?BAUD,S4` five times 300 ms apart: no reboot, the final marker exact, and no RXERR afterwards. | L |

### WCB-WP51: PWM leftovers (L)
s14_pwm.py. **U, M.**

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| pwm.declare_on_serial_mapped_port | canUsePWMOnPort has no serial-mapping term, so PWM declared on a serial-mapped port is accepted. The reverse order is refused, and `;P` refuses raw inputs. The asymmetry needs characterising or fixing. | WCB_PWM.cpp:79-104 (HEAD :83-108); WCB.ino:7936 | `?MAP,SERIAL,S5,R,S4`, then `?MAP,PWM,OUT,S4` and `?MAP,PWM,OUT,S5`. Record whether each is accepted, and what S4 and S5 carry for probe text and for `;P`. Assert the rule once it is decided (part of BUG-15). | L |
| pwm.input_on_hw_uart_pin | PWM capture on a hardware-UART RX pin (S2 is GPIO18 on HW 1.0) through the level-emulated ISR, and passthrough to a hardware-UART TX pin. | WCB_PWM.cpp:148-191, :766-786 | `?MAP,PWM,S2,S4` (reboot). Step the probe's S2 pwm_out through 1000, 1500 and 2000 µs: S4 follows within ±50 µs, and holding the input LOW stops the pulses. Then `?MAP,PWM,S3,S2`: S2 pulses. Restore `?BAUD,S2,9600` and clear. | L |
| pwm.output_pulse_accuracy_under_load | The local passthrough width under mesh load. Commit 1f629a8 (tracker #94, D5) made each pulse an RMT symbol, so this is now a regression check, not a measurement. | WCB_PWM.cpp:157-231, :835-863 (HEAD) | Run S3→S4 at 1500 under input.softserial_tx_integrity's mesh load plus ETM traffic. Collect about 500 S4 widths: all within ±20 µs, and fail on any over +50. | L |
| pwm.boot_conflict_cleanup | A saved PWM output on a port that has since become Kyber/Maestro-REMOTE reserved is skipped at boot, and pwm_outputs is rewritten. A saved mapping INPUT there is refused with a message but never erased. `?MAESTRO,REMOTE` and `?KYBER,REMOTE` have no PWM guard. | WCB_PWM.cpp:696, :1044-1080 (HEAD); WCB_Storage.cpp:1551-1562 | On W1, with WDP off and its S1 Maestro slot saved: `?KYBER,CLEAR`, remove the S1 slot, `?MAP,PWM,OUT,S1`, `?MAESTRO,REMOTE`, reboot. Expect 'Skipping PWM output port Serial1 - conflicts with Kyber' and 'Cleaning up…', no `?MAP,PWM,OUT,S1` in the chain, and a lower `?NVS` pwm_outputs count. Restore the slot and REMOTE under config_guard. | L |
| pwm.passthrough_debug_and_settle | The `?DEBUG,PWM,ON` passthrough trace lines, and the settle send: a change under 6 µs that holds for 5 readings is sent once. | WCB_PWM.cpp:735-793 | With S3→S4,W2S3 mapped: stepping 1500 to 2000 gives '[PWM] Input S3: N μs -> k output(s)', 'Local output -> S4 pin P' and 'Remote output -> WCB2 S3'. Stepping 1500 to 1503 and holding gives exactly one more pulse, near 1503, within 0.5 s. | L |
| pwm.remote_config_right_after_boot | Remote PWM configuration sent within 5 s of boot reaches the remote board, because the old uptime gate was replaced by espNowInitialized. | WCB_PWM.cpp:64-73 | Reboot W1, and as its banner ends send `?MAP,PWM,S3,W2S3`. Expect '[ETM] Seq n fully acknowledged' for `?MAP,PWM,OUT,S3`, and W2's chain gains it. Clean up. | L |
| wcb.baud.pwm_port_skip | `?BAUD` on an S3-S5 port declared as a PWM input or output skips the soft-serial end/begin, so PWM keeps working and no RX interrupt is re-attached to a PWM pin. | WCB.ino:2345-2365; WCB_Storage.cpp:188 | Inside pwm.passthrough_local, send `?BAUD,S3,19200` and `?BAUD,S4,19200` mid-run. The widths still follow, and no serial bytes appear on S4. Restore the bauds. | L |

### WCB-WP52: HCR, MP3 and DFP leftovers (L)
s15_hcr_mp3_dfp.py. **U, M.**

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| mp3dfp.remote_route_manual, help.dfp_remote_manual, wcb.cmd.mp3_dfp_manual_route | A hand-pinned `?MP3,REMOTE,W<n>` or `?DFP,REMOTE,W<n>` stores, saves and emits the route and sends `;A`/`;D` there. On a board hosting the device it first releases the local port. CLEAR keeps the route ('still routing'). Auto-learn never overrides an existing route. | WCB_MP3.cpp:162-164, :210-233, :457-465; WCB_DFP.cpp:123-125, :160-193, :417-425 | Clear the learned route first (`_unlearn`), because the fixture's learned route masks a pin, and turn AUTOJOIN off. On W1 with no MP3, `?MP3,REMOTE,W2` prints '[MP3] Routing ;A to WCB2' and the token appears. `?MP3,CLEAR` prints 'still routing ;A to WCB2', and `;A,PLAY,5` lands on W2 S5. On W2 with a local MP3 on S5, `?MP3,REMOTE,W1` prints the release lines, then 'Routing ;A to WCB1'. With a route stored on W1, a new host's advert prints no 'host learned'. Repeat for DFP: `;D,STOP` puts the 0x16 frame on W2 S5. | L |
| dfp.onerr_backup_and_clear, help.dfp_onerr_clear | The DFP error callback is emitted in the chain right after `?DFP`. `?DFP,ONERR,CLEAR` removes it, after which error frames recall nothing. | WCB_DFP.cpp:186-202, :366-370 | With the `_w2_audio` DFP and `_recall_keys`, the chain holds `?DFP,S5:9600:V20` immediately followed by `?DFP,ONERR,HILE`. `?DFP,ONERR,CLEAR` prints '[DFP] Error callback cleared', and the token is gone. `_dfp(0x40,6)` then prints '[DFP] Error 0x06' with no recall and nothing on W2 S3. | L |
| dfp.port_move_release | Re-issuing `?DFP` on another port releases the old one: flags back ON, label cleared, and 'Released S<old>'. `;D` follows the device. | WCB_DFP.cpp:275-319 | `?DFP,S4`, then `?DFP,S5` on W2. Expect, in order, S4's re-enable, label and 'Released S4' lines, then S5's disable lines. The chain holds `?BCAST,OUT/IN,S4,ON` and `?LABEL,S5,DFPlayer`, and `;D,PLAY,5` from W1 lands only on S5. | L |
| mp3.bcast_skip_forced_on, bcast.mp3_port_forced_on | The MP3 Trigger port is skipped by broadcast output even with `?BCAST,OUT` forced ON, and its RX never runs as commands. | WCB.ino:8082-8087, :8162 | With the MP3 on W2 S5 (`_w2_audio`), send `;W2,?BCAST,OUT,S5,ON`, then broadcast a marker from W1 or W2 S3. The other ports get it, and W2 S5 never does. Restore through the fixture's CLEAR. | L |
| hcr.poll_scheduler_edges | While not all three volumes are seeded, the poll repeats every 3 s, capped at 10 fast polls, then falls back to pollSec. No poll is sent during a fade unless it is a whole pollSec overdue. | WCB_HCR.cpp:271-281, :302-317 | (a) `?HCR,POLL,10` with no reply rule: time the frames on W2 S4. Expect 10 gaps of about 3 s, then 10 s gaps. (b) POLL,3 with `;H,FADEOUT,A,2`: no `<QM,QD,QVV,QVA,QVB>` between the first `<PVA..>` and the `<PSA,QPA>` burst, and polling resumes within 3 s after the fade. | L |
| hcr.minor_verb_forms | `;H` spellings no test sends: VOLUME (= VOL), OVERRIDE,1, MUSE,1, STOP,N, STOP,SOFT, MUSE,GAP with missing bounds, and GET,EMOTION or PLAYING with a bad selector. OVERRIDE,OFF takes the already-tested else branch. | WCB_HCR.cpp:381-386, :523-541, :573, :809-818 | Through `_routed_verbs`: `;H,VOLUME,A,40` gives `<PVA40>`. OVERRIDE,1 gives `<O1,QO>`. MUSE,1 gives `<M1,QM>`. STOP,N gives STOP_BURST. STOP,SOFT gives `<PSG>`, `<PSA,QPA>` and `<PSB,QPB>`. On W2, `?HCR,GET,EMOTION,X` prints '= -1'. | L |
| help.mp3_playfs_callbacks | `;A,PLAYFS,<n>,ONFIN,<key>` and the implicit form `;A,PLAYFS,<n>,<key>` play by SD order with a finish callback. PLAYFS has its own setPending line in the codec. | WCB_MP3.cpp:110-124; WcbCmd WcbMp3*.cpp:59 | In mp3.rx_callbacks, `;A,PLAYFS,7,ONFIN,HILK` gives the bytes 76 14 70 07. Inject 'X': the HILK recall runs once, with its marker on W2 S3. Then `;A,PLAYFS,8,HILK` and inject 'x': the callback is cancelled. | L |

### WCB-WP53: Maestro and Kyber leftovers (L)
s22_maestro_kyber.py (reboots; the suite's WDP-off rule applies). **U, L.** Row 7 needs the BUG-29 help fix.

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| maestro.get_off_s1_ports | A get-query to a Maestro on a port other than S1 reads its reply from that port within 25 ms: S2 is a hardware UART, and S3-S5 use soft reads with the scheduler suspended. | WCB_Maestro.cpp:11-12, :580-605 | Add `?MAESTRO,M5:W1S3:9600`, with a probe rule on W1 S3 answering AA0521 with 0400. `;M5,getErrors` puts the frame on S3 and prints "get 5 'getErrors' -> m5err=4", and `?VAR,GET,m5err` returns 4, with no timeout and no 'Processing input from Serial3' line. Repeat getPosition,0 at 57600, and on S2. | L |
| maestro.m0_daisy_chain_same_port | The id-0 broadcast and id 9 write one frame per local slot, re-addressed to each slot's id, even when several ids share one port; this is deliberately not deduped. The per-id path writes once per port. | WCB_Maestro.cpp:204-206, :251-258, :286-295, :437-462 | Add `?MAESTRO,M5:W1S1:<S1 baud>` beside M1. `;M0,stopScript` gives exactly AA0124 then AA0524, and `;M9,goHome` gives AA0122 AA0522. `;M51` gives AA052701 once. Clear M5; S1 stays reserved. | L |
| maestro.legacy_s1_owner_other_devices | The legacy S1 fallbacks skip S1 when an HCR, MP3, DFP, WLED or PWM mapping owns it, and name the owner. CLEAR,ALL prints 'Legacy routing … is off - S1 is <owner>'. | WCB_Maestro.cpp:112-121, :260-320, :553-556, :1078-1081 | Expensive. Take W1 out of Maestro_Remote (`?KYBER,CLEAR`, reboot) and leave no slot for id 1; CLEAR,M1 also drops the M1:W20 proxy. Keep WDP off. Put an HCR on S1 with polling off. `;M11`, `;M9,stopScript` and `;M1,getErrors` put nothing on S1, and each prints '… S1 is the HCR port - not sent'. Repeat with a WLED owner. Restore `?MAESTRO,REMOTE` and the slots, then reboot. | L |
| maestro.backup_remote_port_from_kyber_target | The backup line for a remote proxy takes S<port> from the first Kyber target with the same Maestro id, matched on id alone, not on WCB; it falls back to S1. The effect is cosmetic, because a remote slot's key ignores the port. | WCB_Maestro.cpp:1139-1151 | The local target must be off S1 (verifier). Put the Kyber on S1 and M1 on S2 (the kyber.local_port_s1_frees_s2 setup) and keep the M1:W20 proxy. The chain shows `?MAESTRO,M1:W20S<local port>`, where it should show the proxy's own target or S1. Replaying the line maps to the same slot. The restore needs reboots. | L |
| wcb.kyber.owned_port_flush_fallback | A local Kyber target on a port a device owns is skipped. Remote buffers over 64 bytes are flushed, not dropped. A Maestro_Remote board with no local slot falls back to writing Serial1. | WCB.ino:5480-5483, :5649-5709, :5773 | Serial1 fallback: on W2, `?MAESTRO,CLEAR` its local slot, and a target-98 burst from W1 S1 must appear on the W2 S1 tap; rebuild the slot. Flush: send a burst of over 200 bytes at 115200 into a Kyber_Local S2; it arrives complete and in order at the remote tap (best effort). The owned-port mask needs a legacy NVS target, so skip it. | L |
| kyber.remote_byte_transparency | The Kyber remote bridge is 8-bit clean, including 0x00 and '^' (0x5E). | WCB.ino:8426-8431 | Send bytes 0x00-0x7F into W1 S1 in paced 64-byte chunks. They arrive in order on the W2 S1 tap, allowing the same ~1% frame loss the existing test allows. Bytes 0x80-0xFF are Maestro command bytes, so that half needs an opt-in run with the Maestro unplugged. | L |
| help.maestro_legacy_clear_default | The legacy `?MAESTRO_CLEAR` and `?MAESTRO_DEFAULT` clear every slot, and `?KYBER,CLEAR`'s own output tells users to run the latter. The help's `?MAESTRO_CLEAR,x` clear-by-id is refused. | WCB_Help.cpp:490; WCB.ino:6796-6805; WCB_Maestro.cpp:684-688 | Use the s22 pattern (`_m_lines`, `_require_replayable`, `_wdp_off`, `_rebuild`). `?MAESTRO_DEFAULT` prints 'All Maestro configurations cleared', LIST is empty, and no `?MAESTRO,M` token remains; rebuild. Repeat with `?MAESTRO_CLEAR`. For `?MAESTRO_CLEAR,M5`, assert the refusal (or the single clear, if the firmware changes) and fix the help (**BUG-29**). | L |
| help.maestro_remote_legacy_setup_line | The generated copy-paste setup line, `?MAESTRO_REMOTE^?MAESTRO,M…^?SLS1,Maestro 2^?REBOOT`, has never been run. | WCB_Help.cpp:261; WCB.ino:6800-6803; WCB_Storage.cpp:1757, :2690 | Capture W2's config, then paste the exact line W1 generated. Expect one deferred reboot. `?KYBER,LIST` says Remote, the slots are present, and the S1 label reads 'Maestro 2'. Restore W2. | L |
| wdp.autoconfig.kyber_targets_fold | On a Kyber-local host in targeting mode, a newly auto-learned remote Maestro is folded into kyberTargets and saved. | WCB_WDP.cpp:739-745 | Use s22's Kyber_Local targeted setup on W1, with its reboots, and turn WDP on for this one test only (s22's WDP-off rule). With `?DEBUG,ON`, clear W1's M<id>:W2 proxy and send `;W2,?WDP,POLL`. Expect 'auto-added remote Maestro', then '[WDP] Kyber targets updated for auto-learned remote Maestro', and `?KYBER` and the chain list the W2 target. Finish with the full Kyber restore. | L |

### WCB-WP54: `;W` routing forms (L)
s05_mesh.py. **U, S.**

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| wdp.alias.ambiguous_case_client, wcb.cmd.w_route_forms, legacy.s_baud_and_w_edges (the `;w` half) | Alias resolution is case-insensitive, resolves a client by its DEVTYPE, and refuses an ambiguous alias with 'is ambiguous (multiple boards)'. A bare `;W`, or `;W<alias>` without a comma, prints the usage line. The legacy one-digit form is `;w<n><digits>`. | WCB.ino:7603-7680; WCB_WDP.cpp:794-806 | `;W<W2's alias in lower case>,;S2<m>` arrives on W2S2. `;Wnavicore,?version` is answered as '[TERM:20]End of Version'. From W2's USB, with W1's alias set to NaviCore's name, `;W<name>,;S0x` prints the ambiguous line; from W1 the name resolves to W1 itself. Join the probe (temporary) with dev_type set to W2's alias: `;W<alias2>,;S2<m>` is ambiguous, and nothing reaches W2S2. `;W` and `;Wdome` print the usage line. `;w2` followed by an all-digit marker delivers exactly those digits to W2. Restore the aliases. | L |

### WCB-WP55: Sequence leftovers (L)
s17_seq_inventory.py. **U, S.**

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| seq.top_level_overflow | A top-level recall (`;C`, ONFIN, ONERR) has no queue reserve. A body longer than the free queue drops tokens, printing one UART0 line each, and is cut short: the #47 failure on the top-level path. | WCB_Storage.cpp:656-667; WCB.ino:2471-2474 | Save HILBIG as 250 `;S1q` tokens, about 1 KB; `marker()` is 10 characters, which would be too long. Send `;CHILBIG,L` and count the markers on W1S1 and the 'queue is full' lines; `?VERSION` must answer within 2 s. Record it as a characterisation, and add a `(should)` that the recall is refused whole or completes whole (**BUG-24**). | L |
| seq.clear_overlong_key_keeps_prefix | `?SEQ,CLEAR` and `?CE` with a key over 15 characters must not remove the sequence stored under the 15-character prefix. | WCB_Storage.cpp:793 | Save HILABCDEFGHIJKL. `?SEQ,CLEAR,HILABCDEFGHIJKLM` and `?CEHILABCDEFGHIJKLM` each print 'No stored value found…'. `?SEQ,GET` of the 15-character key still returns OK, and `;C…,L` still reaches W1S1. | L |
| wcb.chain.edge_forms | Chain-walker edge forms. A letter FUNCCHAR and a lowercase `?seq,save,` still keep the value boundary. A `?MGMT,` token that is not first swallows the rest of the line, by design (WCB.ino:2697-2706). `?CS` and `?MGMT` keep a line containing `;T` off the timer splitter. `, p` parses as a parts request. | WCB.ino:2491-2520, :2698-2708, :4859-4863 | (1) `?FUNCCHAR,x`, then `xseq,save,HILLC,;S1a^;S1b`: `xSEQ,GET` returns the value whole and nothing reaches S1; restore with `xFUNCCHAR,?`. (2) `?seq,save,HILLC2,;S1a^;S1b` is stored whole. (3) `;S0<m>^?MGMT,SEQ,2^?VERSION` prints the marker and `[MGMT:SEQ,2]` arrives, but `?VERSION` does not run. (4) `IF,hq=0^?CSHILT,;S1a^;T300^;S1b` stores the whole value and runs none of it. (5) `?MGMT,PULL,2, p` makes W2 log '(parts accepted)'. | L |
| wcb.config.info_sequences_section | `?config`'s 'Stored Sequences:' section lists key = value per sequence, or 'None'. The Serial Monitoring and Mirror lines are unreachable, because nothing sets them. | WCB.ino:2769-2783, :2842-2863 | `?SEQ,SAVE,HILCFG,;S0x`, then `?config` contains '  HILCFG = ;S0x'. After `?SEQ,CLEAR` the line is gone. The dead branch is in WP59 row 14. | L |

### WCB-WP56: OTA and the config pull, USB RX overflow (opt-in `ota_erase`) (L)
s20_ota.py. **O, S.**

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| ota.begin_aborts_config_pull, wcb.pull.ota_abort | An accepted OTA BEGIN aborts a running pull job and any parked requests, frees the job's buffer, and prints '[MGMT] Config pull for WCB<n> abandoned: OTA started.'. The requester can pull again afterwards. | WCB.ino:3862-3868; WCB_OTA.cpp:128-131 | Give W2 PULLPART,512 and more than 2,912 characters of config. Start `?MGMT,PULL,2,P` on W1, then send `?OTALOCAL,BEGIN,4096,0` on W2's USB. Expect the abandon line, and no further part and no joined reply on W1. `?OTALOCAL,ABORT`, then the next pull is whole. This also exercises WP59 row 7's reap arm (c). | L |
| wcb.stats.rx_overflow_line | USB/S0 RX overflows are counted, and `?STATS` prints 'USB/S0 input: N RX overflow(s) - bytes were lost'. | WCB.ino:918, :2015-2020, :9069-9071 | At a raised `?OTALOCAL,BAUD` inside an ota_erase session, send more than 8 KB while W1 is blocked in a long command. The line appears with N ≥ 1, and it was absent before the burst. | L |

### WCB-WP57: LED pin and the NVS-erase tails (opt-in `nvs_erase`) (L)
s31_password_erase.py, inside `_erase_cycle`. **A, S.**

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| wcb.cmd.led_pin_set, led.pin_set_persist, help.led_pin_set | `?LED,PIN,<0-48>` saves an NVS override, re-initialises the NeoPixel, and is re-applied at every boot. config_guard cannot see it on HW 1.0/2.4, because `?backup` emits it only on 3.x boards. Only `?ERASE,NVS` removes it. | WCB.ino:3486-3488, :6703-6720; WCB_Storage.cpp:118-133, :1225 | Before the erase, send `?LED,PIN,<the pin ?LED reports>`. Expect 'LED pin GPIO<n> saved to NVS.' and 'Reinitializing LED', and `?NVS` now lists led_config. After a reboot the banner shows 'LED pin override from NVS: GPIO<n>'. The erase then proves led_config is gone. The LED itself cannot be observed on this bench. | L |
| backup.hw_unset_warning | With the HW version unset, `?backup` prints 'WARNING: Hardware version not set!' outside the chains, and both chains are CRC-valid and start with `?HW,0`. | WCB.ino:7338-7341 | In the erase test, after the erase boot and before `?HW`, `?backup` shows the warning line, and both chains are valid and start with `?HW,0`. The chain part is already read at s31:226. | L |
| nvs.erase_queued_tail_and_usage (queued-tail half) | `?ERASE,NVS` defers its restart, so commands chained behind it still run first. | WCB_Storage.cpp:1268-1270 | In `_erase_cycle`, `?ERASE,NVS^;S0{t}` prints t before 'Rebooting now'. | L |

### WCB-WP58: Attended characterisations (L)
**A, M.**

| Gap id(s) | Behaviour | Where | How: steps and check | Risk |
|---|---|---|---|---|
| seq.cross_board_recall_ring (s17) | A recall ring across boards (W1's body `;W2,;CB`, W2's body `;W1,;CA`) is invisible to both cycle guards, and `?STOP` cannot reach it. This is a documented limitation. | WCB.ino:7804-7846; SEQUENCE_INVENTORY.md §3c | Put `;t300` in each body so the ring is rate-limited. Confirm it runs for 5 s, then break it with `?SEQ,CLEAR` on W1's console. It stops within one period and both boards answer; if not, reboot both. | L |
| wdp.maestro_remote.kyber_ruleset (s22) | With no controller known, a Kyber-local board on the mesh turns Maestro-remote on only on a board that hosts local Maestro 1 or 2. HIL_TESTING.md:559-560 calls this unreachable. | WCB_WDP.cpp:496-505 | A race: `?KYBER,CLEAR` and `?WDP,FORGET,20` on W1, `?KYBER,LOCAL,S<free>` on W2, then `;W2,?WDP,POLL` before NaviCore's next advert (within 60 s). W1 prints 'Kyber-local on mesh + local Maestro 1/2' only if it hosts id 1 or 2. Restore W2's Kyber config and reboot it. | L |
| boot.legacy_nvs_repairs (s20, `ota_full` plus an old image) | At boot, a stored baud of 0 becomes 9600 (it used to boot-loop), and sequences saved before key_list are migrated once. | WCB.ino:9174, :9190-9195 | OTA W1 to an image from 483b4d8..f5d758f, which accepts baud 0, store `?BAUD,S1,0`, then OTA back. Expect 'Serial1 had an invalid baud of 0 in NVS — falling back to 9600.' and Boot attempts +1. Only with Greg present (flash rules). | L |

### WCB-WP59: Blocked, and what would unblock each

| Gap id(s) | Behaviour | Where | Blocked by | Unblock with | Risk |
|---|---|---|---|---|---|
| pinmap.unverified_revisions | The pin maps for 2.1, 2.3, 3.1 and 3.2 have never run; the bench has only 1.0 and 2.4. HW 0 maps every port to GPIO5. | wcb_pin_map.cpp:9-115 | hardware | Put a 2.1/2.3 classic and a 3.1/3.2 S3 WCB on the bench, running link.out, input.soft_rx_baud_sweep and pwm.passthrough_local on all five ports. On the S3 this also covers the CHANGE-interrupt PWM branch and the 4-channel RMT budget, which 1f629a8's per-port PWM channels exhaust: every PWM port bit-bangs there. | M |
| boot.hw_specific_inits | The NeoPixel init retry and its failure message, the custom short-WDT bootloader identification on the S3, and the S3 RMT budget. | WCB.ino:8386-8412, :8583-8629, :9129-9142 | hardware (an S3, LED sensing) | Part is testable today: assert 'Bootloader: stock (…)' on classic boots, and W2's NeoPixel line, in boot.banner_w1/w2 (verifier). | L |
| wled.legacy_nvs_migration | The legacy singular wled_cfg keys are migrated once to WLED 1 local. | WCB_WLED.cpp:463-467, :485-496 | not testable: no command writes these keys | A host test of loadWLEDSettings and saveWLEDSettings with a Preferences shim, or an upgrade OTA from a release older than multi-WLED. | L |
| wdp.autoconfig.baud0_skip | A Maestro or WLED advertised with baud 0 or a code off the table is never auto-added, and a temporary peer never yields persisted config. | WCB_WDP.cpp:725-750 | probe feature: configureMaestro now refuses a local baud of 0 | A wcb_probe verb that calls WCB_Client::setMaestros with an off-table baud, with the probe pinned as W1's controller. | L |
| wcb.rx.spoof_sender_guards | An ETM packet whose sender id is outside 1-20, or equal to the receiver's own number, is dropped. The id-vs-MAC case is testable: WP16 row 6. | WCB.ino:5067-5090 | probe feature | A probe 'MESH INJECT' mode: a raw esp_now_send of a hand-built 252-byte ETM COMMAND with a chosen id, seq and type. | L |
| wcb.rx.malformed_frame_bounds | The receiver drops malformed frames: an unknown size, a bad packetType, a Kyber chunkLen of 0 or over 180, a 'K' frame with a bad chunkLen or port, a raw frame with a bad port or length. | WCB.ino:4964-4967, :5009-5010, :5410-5421, :5446-5449, :5492-5496 | probe feature | The same raw-frame injection. The target's ports must stay silent, with no reboot, and the rejection lines must print. | L |
| wcb.relay.pull_session_keying | The relay keeps one config session, keyed on (sessionId, packetType, sourceWCB). A type-18 part within 200 ms of a live type-6 session is dropped (rule 15). A type-18 body starting with neither P nor E is dropped. The session is reaped after 10 s. | WCB.ino:4320-4389, :4891-4910 | probe feature for (a) and (b) | A probe verb that emits raw 230-byte config frags. Arm (c) is reachable today under `ota_erase` (WP56 row 1). | M |
| wcb.mgmt.request_filters (the rest) | A SEQVAL_REQ shorter than 59 bytes is dropped. An unknown type is ignored. A requester above 20 uses dedup slot 0. | WCB.ino:3802-3804, :4161, :4582-4611 | probe feature | A probe verb that sends raw 43- and 59-byte requests. The wrong target is in WP25 and the wrong password in WP43. | L |
| help.pull_changed_toobig, configparts.on_device_toobig_and_changed, wcb.pull.toobig (the TOOBIG half) | A reply over 16 parts gets one ETOOBIG error and no parts, and the requester stops. | WCB.ino:3901-3905, :3983-3985 | not testable: it needs more than 8,192 characters, and the bench NVS holds about 5 KB (nvs_history.csv) | A RAM-only knob, such as `?DEBUG,PULLFAULT,TOOBIG` or a max-parts cap. The host side is covered by config_parts_test.cpp:202-261. | L |
| wcb.pull.send_fault_paths | ESP_ERR_ESPNOW_NO_MEM resends (up to 10). A frag that fails in both passes abandons the job. lostTokens gives NOMEM with need=0. The OOM retry gives up. The 3-slot mgmtOut ring drops. | WCB.ino:3932-3962, :4013-4017, :4086-4110, :4253-4256 | not testable | Send-failure and String-allocation fault knobs beside PULLFAULT. | L |
| wcb.relay.result_heap_and_auth | The STATS, SEQ, SEQVAL and ETM relays build their replies by String concatenation on the WiFi task, so a failed append prints a short line that looks whole. 11 handlers compare passwords as String != String. | WCB.ino:4636-4750; passwords at :3351-:4724, :5050, :5381 | not testable | A RAM fault knob that fails String growth. Better, fix it: mgmtQueueOutChunks plus strncmp (F15 is open; §3 item 26). | L |
| fault.oom_and_init_failures | `?backup`'s stack fallback, loop() dropping an empty OOM command, enqueue's OOM refusal, a reboot when the command queue cannot be created, and a failed esp_now_init leaving the boot guard armed. | WCB.ino:2458-2461, :7361-7368, :9151-9164, :9355-9358, :9539-9544 | not testable | One-shot fault knobs in the PULLFAULT style. | L |
| wcb.etm.ack_queue_and_peer_reregister | The 24-slot deferred ACK ring overflowing, a peer re-registering after eviction from the ~20-entry table, and bit-banged TX when no RMT channel is free. | WCB.ino:1363-1397, :1587-1596, :1655-1662, :2279-2305 | not testable: needs more than 20 peers, an ACK storm, or an S3 | Code review only. The RMT fallback becomes testable with an S3 WCB (row 1). | L |
| wcb.rx.dead_branches, wcb.cmd.dead_dispatch_code, wcb.send.dead_raw_helpers_and_untracked_nul, storage.unsettable_and_legacy_loads (plus the dead half of wcb.config.info_sequences_section) | Unreachable code: the non-ETM `?WHOAMI` reply, the second group check, the 'K' Kyber receive (sendESPNowRawToPort and sendESPNowRawToSpecificWCB have no caller), the normal-path ETMCHAR_ ignore, the legacy 'stats' branch (shadowed), the IF drop at the executor, verifyBackupChecksum, updateSerialSettings' baud branch and updateSerialBaudRate, updateCommandDelimiter's else branch, and processSerialMessage's length < 2 check. Also NVS paths that no command writes: migrations, the ETM range heal, the channel clamp, the serial_monitor namespace, and 'Maximum serial mappings'. | WCB.ino:3097-3171, :5349-5441, :5567-5569, :6876-6877, :7031, :7264-7300, :7466, :7555-7558; WCB_Storage.cpp:281-288, :490-497, :983-1070, :1979-1988, :2074-2077, :2210-2278, :2535-2547, :2756-2760 | not testable | Delete the code, or document it in a note rather than a test. The ' ' terminator in the untracked send is a real fix (§3 item 25). | L |

---

## 3. Likely firmware bugs

These are the gaps whose description says the firmware is *wrong*. I checked each one by hand at HEAD `1f629a8`. 32
findings: 3 high, 12 medium, 17 low severity. 26 are outright defects, 3 are latent (they need a condition the bench
never produces), 2 are hardening/design gaps, and 1 is questionable behaviour. "Fix → WP" names the test that proves the
fix; per the conventions, each fix also needs its doc row.

| # | Gap id(s) | What is wrong (file:line at `1f629a8`) | Verdict · severity | Fix → WP |
|---|---|---|---|---|
| 1 | pwm.boot_load_before_wcb_number | setup() calls initPWM() at WCB.ino:9107. It loads the saved mappings and normalises `if (wcbNum == WCB_Number) wcbNum = 0` (WCB_PWM.cpp:728), but WCB_Number still holds its default of 1 (WCB.ino:140); loadWCBNumberFromPreferences() only runs at :9111. On every board except WCB1, a saved output `W1S<p>` reloads as the local `S<p>`. Pulses then leave the source board's own pin (probably with that port's UART skipped as a PWM port), and the next save rewrites the chain. Tracker #43's risk note (docs/HIL_FIX_TRACKER.md:1010) named exactly this ordering trap; the load-time normalisation shipped without moving the load. | Real · **High** | Load WCB_Number (a pure NVS read) before initPWM() and wdpBegin() → WP15 row 1 |
| 2 | pinmap.hw_chip_mismatch_accepted | saveHWversion checks only the list of values (WCB_Storage.cpp:79-80). loadHWversion calls updatePinMap (:110-115, wcb_pin_map.cpp:97-113) with no chip check. On a classic ESP32, HW 31/32 put S2 on GPIO6/7 and S5 on GPIO9/10, which are SPI-flash pins. One wrong `?HW` (e.g. a Wizard push with the wrong HW selected) boot-loops the board until a USB reflash. ident.hw_setter (s27:263-284) itself sets 32 on W1 and relies on restoring it before any reboot. | Real · **High** | Refuse 31/32 unless CONFIG_IDF_TARGET_ESP32S3 (and 1/21/23/24 on an S3), at save and at load → WP16 row 1 |
| 3 | wcb.loopprev.local_bcast_under_mesh_traffic | The mesh copy of a broadcast is gated on the global lastReceivedViaESPNOW (WCB.ino:2897). loop() restores the item's origin into it (:9546). processBroadcastCommand then writes every local port first (:8103; an RMT soft-port write blocks until the bytes are on the wire), and only afterwards calls sendESPNowMessage(0,…) (:8120). Meanwhile the WiFi task sets the global on every non-JSON ETM command it receives (:5234). A command landing in that window silently drops the typed broadcast from the mesh, while the local ports still print it. There is a second, smaller window between the serial reset (:8211) and the enqueue snapshot (:2444). NaviCore's periodic unicasts to W1 already fall into it. | Real race · **High** | Carry the item's origin into sendESPNowMessage instead of reading the global. The ETM path already enqueues with an explicit origin (:5367), so the WiFi-task write may be removable → WP14 row 1 (arm B) |
| 4 | wcb.etm.char_remote_load_never_starts, wcb.etm.char_load_generator_never_starts | Phase 3 sends ETMLOAD under ETM (WCB.ino:1807). The ETM receive path skips it (:5245) and returns (:5372). The only handler is on the non-ETM path (:5559-5566), which an ETM frame never reaches, and which drops non-ETM frames while ETM is on (:5018-5040). `?ETM,CHAR` refuses to run with ETM off (:1688). Even if the generator started, it sends non-ETM traffic (:1929, :1944) that ETM peers drop. The comment at :1802-1806 claims the opposite. The result: phase 3 'Loaded Network' measures an idle mesh, and the recommended timeout is derived from that. | Real · Medium | Handle ETMLOAD in the ETM branch, send the load as untracked ETM, and fix the comment → WP24 row 1 |
| 5 | wcb.cmd.mac_hex_unvalidated, wcb.cmd.mac_newform_unvalidated | `?MAC,2\|3,<v>` does `strtoul(…, NULL, 16)` with no end-pointer or range check (WCB.ino:6290) and saves the result (:6291-6298). `ZZ` or an empty value gives 0x00, and `1FF` gives 0xFF. Each is persisted and takes the board off its mesh group. The legacy `?M2`/`?M3` handlers do validate (:7035-7066). | Real · Medium | Reuse the legacy check → WP16 row 2 |
| 6 | peers.learned_persist_reboot | A learned-membership change is flushed 5 s later (WCB.ino:628, :8949-8954). A deferred restart fires after 4 s of quiet (:1117, :9582-9598) and never calls saveLearnedPeers(). So `?WDP,ADD`, `?WDP,FORGET` or an auto-join followed by `?reboot` is lost. | Real · Medium | Flush dirty learned peers before ESP.restart() → WP16 row 3 |
| 7 | chars.delim_collides_with_prefix | `?DELIM` accepts any one character (WCB.ino:6601-6608), and setCommandDelimiter persists it (WCB_Storage.cpp:573-578). prefixCharOk compares FUNCCHAR with CMDCHAR only (WCB.ino:950-965), and the legacy `?D<x>` (:6777 → :7025-7031) has no check either. `?DELIM,?` or `?FUNCCHAR,^` then splits every config line at its own prefix; only an erase-flash recovers the board. | Real · Medium | Refuse a delimiter equal to either prefix, and a prefix equal to the delimiter → WP35 row 1 |
| 8 | wcb.cmd.legacy_prefix_typo_side_effects | `?CCLEAR,ALL` misses the exact match `cclear` (WCB.ino:6759), prefix-matches `cc` (:6762), and updateCommandCharacter saves charAt(2) = 'L' (:7006-7015); every `;` command then becomes broadcast text. Any 2-character `?D<x>` sets the delimiter (:6777). Any unmatched `?S…` (`?STAT`, `?SBAUDS1,9600`, or a `?STOP` from the mesh) lands in updateSerialSettings (:6955 → :7253-7274) and is dropped with no 'Unknown command'. | Real · Medium | Match the legacy roots exactly, and let everything else reach 'Unknown command' → WP34 row 1 |
| 9 | timer.stop_scope, wcb.cmd.stop_not_whole_line | `?STOP` is recognised only as a whole line, in processSerialCommandHelper (WCB.ino:8237). Over the mesh, via FRAG, or inside a non-timer chain it falls into the `s` catch-all (:6955), is ACKed, and is silently ignored. So a remote board's timer show cannot be stopped. Inside a timer group it does work (command_timer.cpp:239). | Real · Medium | Give processLocalCommand a STOP root that calls stopTimerSequence() → WP29 row 3 |
| 10 | timer.chain_outside_splitter_loses_delays | processCommandCharcter has no `T` branch (WCB.ino:7521-7547), so a `;T` that reaches parseCommandsAndEnqueue prints 'Invalid Serial Command', and the rest of the chain runs at once. Multi-chunk FRAGs and WCB_Client fragmented unicasts are reassembled straight into parseCommandsAndEnqueue (:3439), and `?`-first lines skip the timer path (:8323). The same body recalled from a sequence keeps its delays. | Real · Medium | Send reassembled chains through the same timer gate as :5357/:5606 → WP29 row 1 |
| 11 | wcb.kyber.per_byte_port_dedup | Target-98 Kyber frames are written once per local Maestro SLOT (WCB.ino:5462-5478; meshSerialWrite at :5466 and :5476), with no per-port mask. The send side has the mask (sentLocalPorts, :5671, :5698), and its comment describes the garbling this causes. Two daisy-chained ids on one port receive every remote chunk twice. | Real · Medium | Add the same per-port mask on the receive side → WP27 row 2 |
| 12 | pwm.clear_all_many_remote_outputs | `int remotePorts[MAX_WCB_COUNT + 1][5]` (WCB_PWM.cpp:592) is filled by `remotePorts[wcb][remotePortCounts[wcb]++]` (:605) with no bound. 10 mappings × 5 outputs can name one board 50 times, so a 6th output to one board spills into the next board's row, and on WCB20 it writes past the end of the stack array. | Real (memory safety) · Medium | Bound the count, or dedup per board → WP15 row 5 |
| 13 | var.full_nvs_false_ack | saveVarsToNVS returns void; it only prints an ERROR on a 0-byte write (WCB_Variables.cpp:85-102). setVariableImpl still returns true (:175-177), so `?VAR,SET` prints its '[persistent]' line (:332), which is the Wizard's push ACK. clearVariable (:198-205) does the same. On a full store, the value is lost at reboot, or a cleared variable comes back. | Real (only when NVS is full) · Medium | Return the save result, and print a failure instead of the ACK → WP42 row 1 |
| 14 | seq.reserved_bookkeeping_keys | User keys are saved into `stored_cmds`, the namespace that holds `key_list` and `seq_mig_done`, with no reserved-name check (WCB_Storage.cpp:707-772; erase at :782-839). `?SEQ,SAVE,key_list,x` corrupts the list, `?SEQ,CLEAR,key_list` orphans every sequence, and `?SEQ,CLEAR,seq_mig_done` re-arms the legacy migration. | Real (typed or relayed only) · Medium | Refuse both names in SAVE, CLEAR and recall → WP44 row 1 |
| 15 | maestro.device_port_conflict_unguarded, pwm.declare_on_serial_mapped_port | Only canUsePWMOnPort has a Maestro term (WCB_PWM.cpp:101-102). The HCR/MP3/DFP/WLED guards (WCB_HCR.cpp:886-891, WCB_MP3.cpp:307-312, WCB_DFP.cpp:268-273, WCB_WLED.cpp:332-337) never check for a local Maestro, and a local `?MAESTRO` add checks only the Kyber port (WCB_Maestro.cpp:801-810). So a Maestro and a device can share a UART, and one module's CLEAR resets baud and flags under the other. canUsePWMOnPort also has no serial-mapping term, although `?MAP,SERIAL` refuses PWM ports. | Real · Medium | One shared port-owner check in every configure path → WP26 row 3, WP51 row 1 |
| 16 | boot.wdp_stagger_order | wdpBegin (WCB.ino:9105) computes the advert phase from WCB_Number (WCB_WDP.cpp:1969) before it is loaded (:9111), so every board gets WCB1's 500 ms. The 60 s reschedule adds no jitter (:385), so boards powered together advertise in lockstep. | Real · Low | The same fix as #1 → WP36 row 4 |
| 17 | wcb.etm.char_guards_abort (case d) | Neither local entry point (WCB.ino:6260, :6895) nor startETMChar (:1687) checks etmCharRunning. :1699 then saves the already-suppressed debugETM (false), so a restarted run ends without re-enabling `?DEBUG,ETM`. | Real · Low | Refuse while running, as the relay path does (:4554) → WP24 row 4 |
| 18 | wcb.etm.char_relay_roundtrip (verifier case) | handleETMReqPacket latches etmCharRelayRequesterWCB (WCB.ino:4558) before calling startETMChar. Its early returns (ETM off :1688, WCBQ < 2 :1692) neither notify the requester nor clear the latch. The requester waits for nothing, and the next local `?ETM,CHAR` sends its results to that stale requester (:2193-2197). | Real · Low | Notify and clear on an early return → WP24 row 2 |
| 19 | wcb.etm.seq_wrap_zero | The uint16 counter (WCB.ino:658) is pre-incremented at :2937 and :3041, so it reaches 0 after 65,535 sends (JSON broadcasts count too). The dedup ring uses 0 to mean empty (:688-694, compared at :5173-5176). A seq-0 command reaching a receiver whose ring for that sender still has an empty slot is ACKed and never executed. | Real · Low | Skip 0 on wrap → WP46, or a host test |
| 20 | storage.full_nvs_other_setters | updateBaudRate (WCB_Storage.cpp:196-201), saveSerialLabelToPreferences (:1909-1918) and the other setters ignore put*() results and print success. `?config` then reloads serial_baud from NVS (WCB.ino:2752), which on a full store silently reverts the RAM table while the port keeps running at the new rate. | Latent · Low | Check the writes, as the sequence and mapping paths do → WP42 row 3 |
| 21 | wcb.rx.maestro_whitelist_breadth | The non-ETM "Maestro" exemption tests only `structCommand[1] == 'M'` (WCB.ino:5027), not `[0] == CommandCharacter`. Any non-ETM `?M…` or `xM…` payload therefore passes the ETM-mismatch gate, breaking rule 4. The password is still checked (:5381). | Real · Low | Match `<cmdChar>M` → WP30 row 4 |
| 22 | wcb.timerchain.queue_overflow_silent | enqueuePendingTimerChain ignores xQueueSend's result (WCB.ino:507; the queue has 8 slots, :469), and silently truncates chains over 219 characters (:503). | Real · Low | Count and report drops, as the RC relay does → WP14 row 3 |
| 23 | input.line_buffer_heap | processIncomingSerial appends every byte until CR/LF with no cap (WCB.ino:8224). A device that never sends a line end can take the ~19 KB AP-mode heap (rule 14) before an allocation fails. | Hardening gap · Low-Medium | Cap the line (e.g. 4 KB) and drop the excess with a message → WP32 row 2 |
| 24 | seq.top_level_overflow (+ backup.chain_paste_restore step 4) | The queue reserve applies only when depth > 0 (WCB_Storage.cpp:659), and the comment at :650-657 leaves the top level alone on purpose. A top-level `;C`, ONFIN or ONERR body longer than the free queue drops tokens, one UART0 line each (WCB.ino:2471-2474): the #47 stall on the top-level path. A pasted `?backup` of more than 200 tokens (queue created at :9148) is cut the same way. | Design gap · Low | Refuse whole (as for nested), or enqueue with backpressure → WP55 row 1, WP12 row 1 (f) |
| 25 | wcb.send.dead_raw_helpers_and_untracked_nul | sendEtmCommandUntracked writes ' ' into the last byte (WCB.ino:3039), where the tracked path writes '\0' (:2935). The ETM receive path builds `String(etmReceived.structCommand)` (:5195) without the forced terminator the normal path has (:5543). This is latent: the only caller is the short `?WHOAMI` reply, but a full 200-byte in-group frame would over-read the stack struct. | Latent · Low | Write '\0' at :3039, and terminate on receive → WP59 row 14 |
| 26 | wcb.relay.result_heap_and_auth | 11 handlers compare `String(pkt.structPassword) != String(espnowPassword)` (WCB.ino:3351, 4438, 4452, 4491, 4551, 4619, 4654, 4689, 4724, 5050, 5381). Two failed allocations compare equal and pass, as the comment at :4150-4151 says. Only handleConfigReqPacket uses strncmp (:4152). **Already filed as F15 (open).** | Latent · Low | strncmp everywhere → WP59 row 11 |
| 27 | help.label_max_30_unenforced, wcb.cmd.label_length_bounds | `?LABEL,Sx` stores a label of any length (WCB.ino:6086-6099 → WCB_Storage.cpp:1909). `?SLS` refuses anything over 30 (WCB.ino:7108), and the help says 30 (WCB_Help.cpp:155). | Real (inconsistency) · Low | Enforce 30 in `?LABEL` → WP34 row 4 |
| 28 | help.legacy_baud_sxrate, wcb.cmd.legacy_s_baud_and_numbers | The legacy `?S<port><baud>` prints 'Updated Serial<n> baud rate to <b> and stored in NVS' (WCB.ino:7272) even when updateBaudRate refused the value (WCB_Storage.cpp:165-170; e.g. 4800, which the table lacks). `?BAUDS<n>` without a comma prints nothing (:6948-6953). | Real (cosmetic) · Low | Print only on success → WP34 row 3 |
| 29 | help.page_accuracy, help.debug_kyber_alias, help.maestro_legacy_clear_default | WCB_Help.cpp contradicts the parser in eight places: (1) :138 advertises `?MAESTRO,<id>:S<port>:<baud>`, but the parser needs `M<id>:W<w>S<p>` (WCB_Maestro.cpp:683-688); (2) the `?SLC1` example at :170 is refused; (3) CHKSM '188' at :315, but ETM_MAX_CMD_WITH_CRC is 187 (WCB.ino:242); (4) :348 says a reboot is required, but `?MAC` applies live; (5) WCB '1-9' at :389 and :1196, but MAX_WCB_COUNT is 20 (WCB.ino:127); (6) the 'Kyber debug' at :28-29 and :56 sets debugMaestro (WCB.ino:5891-5896); (7) `?MAESTRO_CLEAR,x` at :490 is refused (WCB.ino:6796, :6804-6805); (8) `;PP100` at :835 is not a valid `;P`. | Real (doc) · Low | Fix the text, and pin it with a help-matches-parser test → WP34 row 5, WP53 row 7 |
| 30 | wdp.cmd.list_detail_content | The `?WDP,LIST` legend (WCB_WDP.cpp:1625) omits D=DFPlayer, although the Cap column emits D (:1542). s18:1190 pins the legend without it. | Real (cosmetic) · Low | Add D, and update s18:1190 → WP48 row 4 |
| 31 | wifi.password_comma_and_trailing_space | processWifiCommand deliberately keeps a trailing space in the passphrase (WCB_WiFi.cpp:400), but every line reader trims first (WCB.ino:2716, :8194; WCB_WS.cpp:386). A passphrase ending in a space can never be entered. | Real (the comment's promise is broken) · Low | Exempt WIFI lines from the trim, or fix the comment → WP17 row 4 |
| 32 | wcb.cmd.map_clear_all (and its duplicates) | `?MAP,CLEAR,ALL` (WCB.ino:6040-6043) calls clearAllPWMMappings() with the default autoReboot=true (WCB_PWM.h:52), which always sets pwmRebootPending (WCB_PWM.cpp:655-656). An operator who clears only serial maps gets a reboot. | Questionable · Low | Reboot only if a PWM mapping existed → WP50 row 1 |

**Checked, not defects:**
- STATS and ETM relay sessions are never reaped (WCB.ino:4891-4910), but a new sessionId resets them (:4623-4624,
  :4728-4729), so nothing leaks into a later reply.
- `?MGMT,` swallows the rest of its line, by design (WCB.ino:2697-2706).
- The RC relay prints its drop line on the WiFi task (:437). This is deliberate and commented; it is a rule-11 exposure
  only under a sustained flood.
- A Maestro proxy's backup port is taken from a Kyber target matched on id only (WCB_Maestro.cpp:1142-1144). The effect
  is cosmetic, because the remote key ignores the port.
- A saved PWM mapping INPUT on a now-reserved port is refused at boot with a message but never erased (WCB_PWM.cpp:696),
  unlike outputs (:1071-1080). Untidy, but harmless.
- Only one timer chain runs per board (command_timer.cpp:98-113). This is documented design.
- `?BAUD` does not accept 4800 (WCB_Storage.cpp:165-167), although the RMT encoder is sized around it (WCB_SoftSerial.cpp:11-14).
  An observation only.

**Wizard defects (not firmware, but in this repo; verified in `Wizard/`; each fails its WP20/40/41 test today):**

> **Status 2026-09-27: all twelve fixed in `Wizard/`** (W-12 is comments only), each pinned by a test that failed on the
> Wizard before the fix: `tests/wizard/unit/roundtrip.test.js` and `unit/pull.test.js` (the WP40 rows),
> `specs/push_fake.spec.js` (`wizard.push_fake_*`, WP20 rows 1-3 and the WP41 rows) and
> `wizard.remote_pull_fake_legacy_crc` (W-7). They run in CI, and `s30_wizard.py` registers them for the full run (`_FAKE_PUSH`, `_FAKE_PULL`). Two
> corrections to the rows above: **`?WCBQ` needs no reboot** - the firmware reconciles peers live (`?WCBQ`, WCB.ino;
> `saveWCBQuantityPreferences`, WCB_Storage.cpp) and the old substring check never matched `WCBQ,` either - so WP20's
> "a WCBQ edit appends `^?reboot`" and the WCBQ entry in its reboot table are wrong. And W-10 was fixable without
> changing what an edited row sends: a row keeps the value it was built from and returns it while its text is
> untouched. Left as found: a real edit to one mapping still re-sends every mapping (a PWM input one reboots the
> board), a mapping removed while disconnected is never cleared by a push (WP40 row 4), and on the direct path the `<func>DELIM,<new>` the push re-issues after a delimiter change is split at its own new delimiter, so the board prints `Invalid format` (harmless: the change has already landed).
>
> **2026-09-28, after a review of that commit (`b57fe2a`):** W-2's push check judged only the end state, but the board
> checks each character change against its live characters as it lands (delimCharOk, prefixCharOk), so a push now
> replays DELIM, CMDCHAR, FUNCCHAR from the board's own characters (`planCommandCharChange`, parser.js) and refuses a
> change that needs two pushes, saying which two. A board still on `,` takes nothing until its delimiter moves: the
> push sends the two-character `<func>D<x>` first, or refuses while General still says `,`. W-4's device syncs put a
> port back to what the board reported when the table matches it again (an edit taken back sent a lone `?BAUD` that
> left the port at a baud its WLED or Maestro does not use), and the Maestro table is compared by content, not key
> order.
- **W-1** ETM,DELAY 0 becomes 100 (parser.js:892, app.js:1363).
- **W-2** `?DELIM,,` parses to '' (parser.js:587-588), and boardGo then bootstraps `?DELIM,^`.
- **W-3** A disabled controller's custom id is dropped on a full push (parser.js:1346-1350).
- **W-4** syncMaestrosToConfig relabels the Maestro port (app.js:3659-3661), and syncWLEDsToConfig forces the label,
  the flags and the baud (app.js:3039-3043).
- **W-5** A `bidir` key the parser never produces makes the diff re-send every mapping (app.js:3849).
- **W-6** buildSystemFile has no client token (parser.js:1709-1716).
- **W-7** A legacy CONFIG body is stored without its CRC checked (parser.js:1948-1950 → app.js:8570-8571).
- **W-8** commandStringNeedsReboot matches substrings (app.js:8924-8939).
- **W-9** The 16-chunk relay cap is checked after the confirm and the bootstrap (app.js:8245, 8268-8280, 8320). This is
  F14, open.
- **W-10** The sequence textarea is not an identity round trip (app.js:4364-4451).
- **W-11** diffConfigs ignores six fields (parser.js:1726-1768).
- **W-12** The 'Firmware counts down ~3 s' comments are stale (app.js:7705, 9915).

> **2026-09-29, from the WCB-WP20/21/40/41 tests:** nine more, W-13 to W-21, found against `Wizard/` at `08aaeb6`.
> Each is pinned by a `(should)` test. While the defect stands, the test is `test.fail` in Playwright (CI stays green,
> the harness reports FAIL) or a node `todo` in `unit/model.test.js`; the fix removes the marker, so the test guards it.
> **Status 2026-10-04:** W-13 and W-19 are fixed (each row says how); W-14 to W-18, W-20 and W-21 are not.
- **W-13** A function identifier typed into General but not yet pushed is used at once for the board's immediate
  commands. onGeneralCmdCharChange writes it into every boardConfigs entry (app.js:1332-1339), and the immediate sends
  read boardConfigs[n].funcChar: sequence save and remove (app.js:4773, :4636; the save then records the value in the
  baseline as saved, :4856-4861), variables (:5006, :5072, :5132, :5166), the debug toggles (:9500), the mesh poll and
  panel (:13423), RTERM start and stop (:6491, :6505), HCR status (:12837), Identify (:9984, :10000), Push All's relay
  reboot (:9197), and the relay hops of a bidir mapping and a removed PWM output (:4305, :4247 - the case app.js:76-85
  says must never read boardConfigs). The board, still on its old character, broadcasts each line as text to its ports
  and the mesh (WCB.ino:6063-6066). `wizard.app_fake_pending_funcchar`.
  **Fixed:** every immediate send reads the board's live character, `_liveFuncChar` (`_liveChar`, app.js), as the relay
  hops already did: a switch seen since the baseline was stored (a boot banner, or a push's character switch, recorded
  in `boardBootChars` with the baseline it was seen over), else the baseline, else the banner. So a push that switches
  the characters and reboots is followed by the new ones until its verify pull. The command character's immediate
  sends (the `;L` WLED controls, a sequence's Test, a temporary variable) read `_liveCmdChar` the same way.
- **W-14** On the shared port - the connection the first board of a page gets (establishConnection) - a push that needs a
  reboot sends `?reboot` and stops: no reconnect, so no verify pull, and the baseline is not advanced (app.js:8038-8045).
  The next push sends every change of the first again and reboots the board again. `wizard.push_fake_shared_reboot_repull`.
- **W-15** Push All's last stage closes and reconnects every relay (app.js:9193-9218) with no shared-port branch. A shared
  relay has no port of its own, so reconnect() returns false at once (app.js:5393): "did not come back — reconnect
  manually", and the card goes Not connected while the hub still holds the port. `wizard.push_fake_all_shared_relay`.
- **W-16** The General conflict check leaves out the WCB quantity (extractGeneralFields, GENERAL_FIELD_LABELS,
  app.js:1555-1595), although every push writes the General WCBQ into the board (app.js:7933, :8340). A second board on
  another quantity opens no modal, and its next push - a label, say - silently sends `?WCBQ` with the first board's value.
  `wizard.push_fake_general_wcbq`.
- **W-17** A saved system file does not load back as it was saved. parseSystemFile raises the WCB quantity to the number
  of [WCB] sections, client slots and boards above the floor included (parser.js:1257-1260); loadSystemFileContent renders
  sections 1..that number (app.js:9270), adding default boards the file never held and none for a board above them; the
  next export writes such a board from its missing DOM, with no labels, sequences or variables (app.js:9309-9327).
  `wizard.app_fake_system_file_reload`; the parser half is a `todo` in `unit/model.test.js`.
- **W-18** A board above the WCB quantity cannot be renumbered past it: its dropdown offers numbers up to its own
  (app.js:3194-3202), but onWCBNumberChange takes only numbers up to the General quantity (app.js:2080-2081), so the pick
  shows and is dropped. `wizard.app_fake_wcb_number_above_floor`.
- **W-19** A first pull (and a file load) mirrors the board's values into General through the handlers for a user's edit
  (app.js:8971-8973), each of which toasts "Changes pending — push to all boards to apply" and sets
  generalSettingsDirty (app.js:1212-1216): Push All is flagged after every first connect, with nothing to push.
  `wizard.app_fake_pull_leaves_nothing_pending`.
  **Fixed:** syncGeneralFromConfig runs those handlers inside `_mirrorGeneral` (app.js), which holds
  `_notifyGeneralChanged`: they still copy the values into systemConfig, every board's config and the General
  baseline, and nothing toasts or flags Push All. A user's edit runs the same handlers unheld.
- **W-20** A no-edit push of a local-Kyber board with a Maestro of its own and a target on another board re-sends
  `?KYBER,LOCAL`, and a KYBER line asks for a reboot (app.js:9075). The backup lists the Maestro table before the KYBER
  line (WCB.ino:3810, :3817), so the parser files the other board's Maestro as a target first (parser.js:877-882,
  :688-701); autoComputeKyberTargets puts the live boards' Maestros first (app.js:9901-9916); and kyberChanged compares
  the lists as JSON, order included (parser.js:1424-1427). `wizard.push_fake_kyber_own_maestro`; a `todo` in
  `unit/model.test.js`.
- **W-21** Removing a bidirectional serial mapping clears only the source board (removeMappingRow, app.js:3939-3979).
  _removeBidirRows drops the destination's mirrored row from the page and sends nothing (app.js:3981-3992), and a push
  never clears a removed mapping (parser.js:1689-1702), so the reverse mapping stays on the destination board.
  `wizard.editors_fake_bidir_remove`, and on the bench `wizard.mapping_bidir_relay`.

**Doc drift found by the scan** (fix it in the same commit as the tests):
- WDP_DESIGN.md §9: Maestro auto-add is per-host (WCB_Maestro.cpp:911-914), and the Maestro-remote flip runs on every
  advert (WCB_WDP.cpp:657). Both are per the scan.
- WCB_HCR.cpp:440-442 omits fn 20 and 21.
- The var.clear_all_name_collision docstring and s16's module docstring are stale.
- s19 client_mesh.rejoin_seq_reuse's docstring ('never sends an ETM boot announce') is stale.
- HIL_TESTING.md:559-560 calls the Kyber ruleset unreachable.
- HIL_TEST_AUDIT §7 WP11 says ident.epass_live has never run; it passed in 20260925-092255.

**Since the scan:** `1f629a8` gave every PWM output port its own RMT channel. The pulse-accuracy gap is now a regression
check (WP51 row 3). The new channel budget is untested: the 5th PWM port on a classic board bit-bangs (WP15 row 4), and
on an S3 every port does (WP59 row 1).


### Status of the §3 items

Fixed 2026-09-27/28 on branch `WIFI`, one image for the batch (decisions D12-D28 in
[../HIL_WEEK_DECISIONS.md](../HIL_WEEK_DECISIONS.md)). "Verified" means its test passed on the bench with that image, in
run `20260928-004312` (31 tests: 28 passed, and the 3 whose own expectations were wrong passed in `20260928-004911`
once fixed). The three high items' tests also ran on the old image first, in `20260928-003915`, and each failed on
exactly its defect (#3: 0 of 30 typed lines reached the mesh under load).

| # | Status | The fix | Proved by |
|---|---|---|---|
| 1, 16 | verified | `WCB_Number` loads first in `setup()` | `pwm.w2_output_to_w1_survives_boot` |
| 2 | verified | `hwVersionFitsChip`, at save and at load | `ident.hw_other_chip_refused`; `ident.hw_setter` now uses a same-chip version |
| 3 | verified | only the loop task writes the origin flags; `processBroadcastCommand` decides the mesh send at entry | `input.bcast_mesh_under_traffic` |
| 4 | verified | ETMLOAD on the ETM path (`etmLoadRequested`); the load is untracked ETM `ETMCHAR_LOAD_*` | `etm.char_load_on_peers` |
| 5 | verified | `?MAC,2\|3` takes one or two hex digits | `ident.mac_hex_refused` |
| 6 | verified | learned peers flushed before the deferred restart; frozen after `?ERASE,NVS` | `wdp.peers.add_forget_survive_quick_reboot` |
| 7 | verified | `delimCharOk`; `prefixCharOk` refuses the delimiter; DELIM exempt from the `?` help shortcut | `chars.delim_collision_refused` |
| 8 | verified | `?CC`/`?LF` one character; `isLegacySerialSetting`; no letter or digit as delimiter | `wcb.cmd.legacy_prefix_typos` |
| 9 | verified | a `STOP` root in `processLocalCommand` | `serial.timer_stop_mesh`, `serial.timer_stop_in_chain` |
| 10 | verified | `isTimerChain` for the reader (incl. the `?C` branch), both receive paths and the FRAG reassembly | `serial.timer_after_func_command`, `mesh.timer_chain_two_chunks` |
| 11 | verified | the target-98 receive side writes once per port | `kyber.receive_one_write_per_port` |
| 12 | verified | `clearAllPWMMappings` keeps a port set per board | `pwm.clear_all_many_remote_outputs` |
| 13 | verified | `saveVarsToNVS` returns the result; a refused set or clear rolls RAM back and prints no ACK | `seq.nvs_full_consistency` (opt-in `nvs_fill`) |
| 14 | verified | `seqKeyReserved` in save, clear and recall (before the fan-out) | `seq.reserved_bookkeeping_keys` |
| 15 | verified | devices refuse a local Maestro's port and vice versa; PWM refuses a serial-mapped input at configure time (D23, D24) | `devices.maestro_port_refused`, `pwm.serial_mapped_input_refused` |
| 17, 18 | verified | `startETMChar` refuses a second start and returns why; the relay tells the requester and releases it | `etm.char_second_start_refused`, `etm.char_relay_refusal_reported` |
| 19 | fixed | `nextEtmSeq` never sends 0 | none on the bench (65,535 sends); code review |
| 20 | verified | `?config` prints the RAM tables; the baud, label and broadcast setters report a refused write | `seq.nvs_full_consistency` (opt-in `nvs_fill`) |
| 21 | fixed | the non-ETM Maestro exemption needs `<cmdChar>M` | `etm.nonetm_whitelist_scope` (written 2026-09-28, not yet run) |
| 22 | verified | a long received timer chain rides a heap buffer; refusals are reported (D14) | `mesh.timer_chain_long` |
| 23 | verified | a line is capped (4 KB on S1-S5, 32 KB on USB) and dropped whole (D16) | `input.line_cap_drops_whole` |
| 24 | verified | a top-level recall that cannot fit the queue is refused whole; the serial reader waits up to 2 s for queue room | `seq.top_level_too_big_refused`, `input.usb_long_chain_backpressure` |
| 25 | half not a defect | the "space" was a raw NUL byte (D20); the receive side now terminates the ETM command | code review |
| 26 | fixed | `strncmp` at all 13 password gates, `otaPktAuth` included | code review |
| 27 | verified | `?LABEL` caps at 30 | `persist.label_max_30` |
| 28 | verified | "stored in NVS" only when stored; `?BAUDS<n>` prints its usage | `wcb.cmd.legacy_s_baud_messages` |
| 29 | verified | eight help lines corrected; `?MAC` still says reboot (only the receive filter is live) | `help.corrected_pages` |
| 30 | fixed | the legend names `D` | `wdp.cmd.*` (s18 pins the legend) |
| 31 | comment fixed (D15) | | |
| 32 | verified | clear-all restarts only when a PWM mapping or output existed | `pwm.map_clear_all_without_pwm` |

The Wizard defects W-1 to W-12 are tracked with their specs (`tests/wizard`).

**Wiki edits due at the `WIFI` to `main` merge** (D21; the wiki describes released firmware, present tense only):
- `?HW` refuses the other chip's versions (3.1/3.2 on a classic ESP32 and the reverse), with the message.
- `?MAC,2|3,<xx>` takes one or two hex digits and refuses anything else.
- The delimiter (`?DELIM`, `?D<x>`) cannot be either prefix character, `,`, a letter or a digit; `?DELIM,?` now answers
  with that refusal instead of the help page.
- `?LABEL,Sx,<text>` caps at 30 characters, like `?SLS`.
- `?STOP` stops a timer chain from the mesh (`;W<n>,?STOP`), inside a chain and from a sequence body.
- A `;T` chain may start with a `?` command; long received timer chains run whole.
- The legacy spellings match exactly: `?CC<c>` and `?LF<c>` take one character, unknown `?S...` forms answer `Unknown
  command`, `?BAUDS<n>` without a rate prints its usage.
- A device (HCR, MP3, DFPlayer, WLED) and a local Maestro cannot share a port; PWM refuses a port a serial mapping reads.
- `key_list` and `seq_mig_done` are not usable as sequence names; a top-level sequence longer than the free command
  queue is refused whole.
- `?ETM,CHAR` phase 3 loads the mesh; each peer prints its load start and `complete: N frame(s) sent`.
- A pasted one-line `?backup` of more than 200 tokens runs whole; a device line over 4 KB (32 KB on USB) is dropped.
- A command's verb is matched in any case everywhere: `?Wifi,AP,...`, `?Seq,SAVE,...` and the other data-carrying
  verbs keep a value that ends in `?`, and `;Seq<key>` recalls like `;SEQ<key>` (tracker #95).

---

## 4. Well covered

The eleven covered_summaries agree that the command surface itself is pinned:
- **Commands:** every ETM setter and legacy form; every `?MAP` SERIAL/PWM form; `?BAUD`, `?LABEL` and `?BCAST`; every
  `?WDP` sub-verb, including WDP-DA forget/clear; `?OTALOCAL` and `?OTA`, exhaustively, with the erase paths opt-in; the
  `?MGMT` PULL/STATS/SEQ/FRAG error replies; `?SEQ`, `?VAR` and IF with every operator; the help trap; and nearly every
  legacy spelling.
- **Device verbs:** every `;A`, `;D`, `;H`, `;L` and `;M` verb, checked byte for byte.
- **ETM core (s18, s99):** unicast ACK, retry and failure; per-board broadcast ACKs; the dedup ring against the pending
  table; eviction; the offline sweep and the #80 race; the CHKSM limit and CRC gate; and the deferred restart's quiet
  window, 20 s cap and exemptions.
- **Config pull in parts (F13):** covered end to end on the bench, through NaviCore's relay, in the host test, and in
  ten Wizard specs that need no board.
- **Devices:** Maestro routing and the whole get-query engine; HCR codec, poll and fades; MP3 and DFP bytes and
  callbacks; WLED JSON, proxies and boot restore.
- **I/O:** PWM has 25 tests (pulse accuracy, guards, deferred reboot, local and mesh passthrough, WDP auto-config and
  self-heal). Soft serial has tests for RMT TX, level/edge RX, RX under TX, and core-0 contention.
- **WDP:** temporary peers and WDP-DA are covered exhaustively.
- **Sequences, variables and timers:** nearly complete (cycle guard, depth, fan-out rules, persistence).
- **Boot:** the banner is compared line by line with the chain.
- **Wizard:** the parser has unit tests, and the remote pull is covered with a fake relay and on the bench.
- **Refuted:** five scanned gaps turned out to be covered already: wdp.solicit.no_record_wipe_fw,
  wdp.pwm.manual_output_not_selfhealed, wcb.etm.boot_announce_clears_wcb_peer_ring, wcb.cmd.mac_octet_reboot_persist
  and ident.epass_setter_never_run.
