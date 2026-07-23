# WCB System Test Plan

**Firmware branch under test:** _______________
**Tester:** _______________  **Date:** _______________

---

## Hardware Reference

| Board | HW Ver | Port | Connected Device | Notes |
|-------|--------|------|-----------------|-------|
| WCB1 | v2.1 | S1 | *(empty)* | |
| WCB1 | v2.1 | S2 | Kyber — Maestro port | 57,600 baud |
| WCB1 | v2.1 | S3 | Kyber — Marcduino | 9,600 baud |
| WCB1 | v2.1 | S4 | *(empty)* | |
| WCB1 | v2.1 | S5 | SparkFun MP3 Trigger | 9,600 baud · Raw serial ↔ W3S2 |
| WCB2 | v2.4 | S1 | Maestro ID1 | 57,600 baud |
| WCB2 | v2.4 | S2 | Maestro ID2 | 57,600 baud |
| WCB2 | v2.4 | S3 | PWM Input — Maestro ID2 Ch1 | |
| WCB2 | v2.4 | S4 | Servo 3 — PWM Output | Mapped from W2S3 |
| WCB2 | v2.4 | S5 | Servo Controller Board | 9,600 baud |
| WCB3 | v2.3 | S1 | Maestro ID3 | 57,600 baud |
| WCB3 | v2.3 | S2 | Marcduino Clone — MP3 source | 9,600 baud · Raw serial ↔ W1S5 |
| WCB3 | v2.3 | S3 | Marcduino Clone Serial Interface | 9,600 baud |
| WCB3 | v2.3 | S4 | *(empty)* | |
| WCB3 | v2.3 | S5 | Servo 4 — PWM Output | Mapped from W2S3 |

## Test Configurations

| Config File | Loads on | Used for Phases |
|-------------|----------|-----------------|
| `WCB1_01_base.txt` | WCB1 | 2, 3, 3B, 4, 10, 12, 13 |
| `WCB2_01_base.txt` | WCB2 | 2, 3, 3B, 4, 10, 12, 13 |
| `WCB3_01_base.txt` | WCB3 | 2, 3, 3B, 4, 10, 12, 13 |
| `WCB1_02_full.txt` | WCB1 | 5, 6, 7, 8, 11, 14 |
| `WCB2_02_full.txt` | WCB2 | 5, 9, 11, 14 |
| `WCB3_02_full.txt` | WCB3 | 5, 8, 9, 11, 14 |

> **How to load:** Open the board's terminal in the wizard, copy the `PASTE:` line from the config file, paste and press Enter. Verify with `?config`.

---

## Phase 1 — Wizard: Detect & Flash

**Load config:** None — wizard handles this

| # | Test | Expected | P/F |
|---|------|----------|-----|
| 1.1 | Open wizard with no boards connected. Click **Detect** on any slot. | "No authorized ports found" hint shown — no crash, no stuck spinner. | |
| 1.2 | Plug in WCB1 (COM19). Authorize port. Click **Detect** on slot 1. Wait for visible "Press RESET" prompt in terminal or panel — **not just console**. Press RESET **once**. | WCB1 detected and assigned to slot 1. No second press needed. | |
| 1.3 | Click **Detect** on slot 2 for WCB2. Press RESET once when prompted. | WCB2 detected and assigned to slot 2. | |
| 1.4 | Click **Detect** on slot 3 for WCB3. Press RESET once when prompted. | WCB3 detected and assigned to slot 3. | |
| 1.5 | With WCB1 already connected, click **Detect** on slot 1 again. | Detect starts immediately — no "port is already open / locked" error in console. "Press RESET" prompt appears. Press RESET — re-detects and reconnects without error. | |
| 1.6 | Flash WCB1 with LilyGO v2.1 firmware. | Flash completes. Post-flash: terminal shows reconnect → board boots → `End of Backup` received → "✓ Firmware ready" — **without** a manual reset. | |
| 1.7 | Flash WCB2. | Same post-flash auto-reconnect as 1.6. | |
| 1.8 | Flash WCB3. | Same post-flash auto-reconnect. | |

---

## Phase 2 — Configuration Verification

**Load config:** `WCB*_01_base.txt` on all three boards

| # | Test | Expected | P/F |
|---|------|----------|-----|
| 2.1 | Send `?config` to each board. Check HW version, WCB number, WCBQ=3, ETM=ENABLED, MAC addresses unique. | All match hardware reference table. | |
| 2.2 | Verify baud rates in `?config` output match hardware reference for each board. | S1/S2 on WCB2 and WCB3 show 57,600. WCB1 S2 shows 57,600. All MP3/Marcduino ports show 9,600. | |
| 2.3 | Verify port labels on all boards match hardware reference. | WCB1: S2=Kyber Maestro, S3=Kyber Marcduino, S5=MP3 Trigger. WCB2: S1=Maestro ID1, S2=Maestro ID2, etc. | |
| 2.4 | Send `?backup` to each board. | Output ends with `?CHK<hex>` then `End of Backup`. No truncation. | |

---

## Phase 3 — ETM Network

**Load config:** `WCB*_01_base.txt`

| # | Test | Expected | P/F |
|---|------|----------|-----|
| 3.1 | After all three boards boot with ETM enabled, check each terminal. | `[ETM] WCB2 came ONLINE` and `[ETM] WCB3 came ONLINE` appear in WCB1's boot output (and equivalent on each board). | |
| 3.2 | Wait 15 seconds (one full heartbeat cycle). Monitor all terminals. | No "missed heartbeat" or "OFFLINE" messages while all boards are powered. | |
| 3.3 | From WCB1 terminal: `;W2,?config` | WCB2 prints its config. WCB1 receives ETM ACK — no retry messages. | |
| 3.4 | From WCB1 terminal: `;W3,?config` | WCB3 responds. ETM ACK on WCB1. | |
| 3.5 | From WCB3 terminal: `;W1,?config` | WCB1 responds. ETM ACK on WCB3. | |
| 3.6 | From WCB1 terminal (plain broadcast, no prefix): `TEST_BROADCAST` | Message appears in WCB2 and WCB3 terminals on all broadcast-enabled ports. | |
| 3.7 | From WCB2: `TEST_FROM_WCB2` | Appears on WCB1 and WCB3. | |
| 3.8 | Send `?STATS` to each board after 3.3–3.7. | Non-zero sent/received counts, low or zero failed count. | |
| 3.9 | Send `?ETM,CHAR` from WCB1. | Three-phase characterization test runs and prints per-board latency and success rate. | |

---

## Phase 3B — Broadcast Filtering

**Load config:** `WCB*_01_base.txt` (all broadcasts reset to ON)

| # | Test | Expected | P/F |
|---|------|----------|-----|
| 3B.1 | Send `?config` to each board. | All five ports show Broadcast Input: Enabled and Output: Enabled. | |
| 3B.2 | On WCB1: `?BCAST,OUT,S1,OFF`. Send data from a device on WCB1 S1. | Data does **not** appear on WCB2 or WCB3. Restore: `?BCAST,OUT,S1,ON`. | |
| 3B.3 | On WCB1: `?BCAST,IN,S2,OFF`. Broadcast from WCB2. | Broadcast reaches WCB1 S1, S3, S4, S5 — but **not** S2. Restore: `?BCAST,IN,S2,ON`. | |
| 3B.4 | On WCB2: `?BCAST,IN,S4,OFF`. Broadcast from WCB1. | WCB2 S4 receives nothing. All other WCB2 ports still receive. Restore: `?BCAST,IN,S4,ON`. | |
| 3B.5 | Load full config (`WCB*_02_full.txt`). Send plain broadcast from WCB2. Enable `?DEBUG,ON` on WCB1. | Broadcast does **not** forward to WCB1 S2 or S3 (Kyber ports auto-excluded). Debug confirms skip. | |
| 3B.6 | On WCB3: `?BCAST,OUT,S2,OFF`. Have Marcduino Clone transmit on WCB3 S2. | Data flows through raw serial mapping to WCB1 S5 (raw mapping is independent of broadcast). Data does **not** appear as broadcast on WCB1/WCB2 other ports. Restore: `?BCAST,OUT,S2,ON`. | |
| 3B.7 | On WCB1: `?BCAST,IN,S5,OFF` and `?BCAST,OUT,S5,OFF`. (a) Send broadcast from WCB3. (b) Send via WCB3 S2 raw mapping. | (a) Does not arrive at WCB1 S5. (b) Still arrives at WCB1 S5 (raw mapping bypasses broadcast flags). Restore both to ON. | |
| 3B.8 | Disable broadcast on several ports across all boards, then `?BCAST,RESET` on each. | `?config` on each board shows all ports Broadcast Input: Enabled / Output: Enabled. | |
| 3B.9 | `?BCAST,OUT,S1,OFF` on WCB2. `?reboot` WCB2. After reconnect: `?config`. | S1 Broadcast Output still shows Disabled (persisted to NVS). Restore: `?BCAST,OUT,S1,ON`. | |

---

## Phase 4 — Serial Port Commands

**Load config:** `WCB*_01_base.txt`

| # | Test | Expected | P/F |
|---|------|----------|-----|
| 4.1 | From WCB2: `;S1,TEST` | "TEST" appears at device on WCB2 S1. | |
| 4.2 | From WCB1: `;S5,TEST` | Bytes arrive at WCB1 S5. No crash. | |
| 4.3 | From WCB1: `;S5,A^;W2,?config^;W3,?config` | All three execute in sequence — byte to S5, WCB2 config prints, WCB3 config prints. | |
| 4.4 | From WCB1: `;T2000,;W2,?config` | ~2 seconds later WCB2 config prints. Not immediate. | |
| 4.5 | From WCB1: `;T30000,;W3,?config` then immediately `;STOP` | WCB3 config never prints. No error. | |

---

## Phase 5 — Maestro Servo Controllers

**Load config:** `WCB*_02_full.txt`

| # | Test | Expected | P/F |
|---|------|----------|-----|
| 5.1 | From WCB2: `;M11` (Maestro ID1, subroutine 1) | Maestro ID1 executes script 1. Physical servo movement or Maestro LED confirms. | |
| 5.2 | From WCB2: `;M21` (Maestro ID2, subroutine 1) | Maestro ID2 executes script 1. | |
| 5.3 | From WCB3: `;M31` (Maestro ID3, subroutine 1) | Maestro ID3 executes script 1. | |
| 5.4 | From WCB1: `;W2,;M11` | Command routes WCB1→WCB2 via ESP-NOW. WCB2 fires Maestro ID1 script 1. Same result as 5.1. | |
| 5.5 | From WCB1: `;W3,;M31` | WCB3 fires Maestro ID3 script 1. | |
| 5.6 | From WCB1: `;M02` (Maestro 0 = broadcast all, subroutine 2) | All three Maestros execute Script 2 simultaneously. | |
| 5.7 | `?MAESTRO,LIST` on each board. | WCB2: IDs 1 and 2 local (S1/S2, 57,600). ID3 remote (W3S1). WCB3: ID3 local (S1, 57,600). IDs 1 and 2 remote. WCB1: all three remote. | |

**The subroutine trigger has two equivalent spellings:** `;M<id><seq>` (`;M11`) and the comma form `;M<dev>,<n>` (`;M1,1` — identical to `;M11`). A **verb name** after the comma instead selects native Pololu servo/query commands (`;M<dev>,verb`):

| # | Test | Expected | P/F |
|---|------|----------|-----|
| 5.8 | From WCB2: `;M1,setTarget,0,6000` (native verb: channel 0 → 6000 qtr-µs ≈ 1500 µs) | Maestro ID1 servo on channel 0 moves to ~center. Confirms comma = verb form. | |
| 5.9 | From WCB2: `;M1,goHome` | Maestro ID1 sends all channels to their configured home positions. | |
| 5.10 | From WCB2: `;M1,setSpeed,0,10` then `;M1,setTarget,0,8000` | Channel 0 slews to 8000 slowly (speed-limited) — demonstrates setSpeed. | |
| 5.11 | From WCB1: `;W2,;M1,setTarget,0,4000` | Verb routes WCB1→WCB2 via ESP-NOW; ID1 channel 0 moves. Verb args (commas) survive the `;W` wrap. | |
| 5.12 | From WCB2 with `?DEBUG,MAESTRO,ON`: `;M1,bogus` and `;M1,setTarget,0` (bad verb / missing arg) | Both rejected — **no** Pololu frame printed / sent. Malformed verbs never reach the servo wire. | |
| 5.13 | From WCB2: `;M1,1` (comma spelling of `;M11`) | Identical to 5.1 — Maestro ID1 runs subroutine 1. Confirms `;M1,1` ≡ `;M11`, and routes the same (test from WCB1 with `;W`/remote mapping to confirm it forwards like `;M11`). | |

---

## Phase 6 — Kyber Integration

**Load config:** `WCB*_02_full.txt`

| # | Test | Expected | P/F |
|---|------|----------|-----|
| 6.1 | `?KYBER,LIST` on WCB1. | Kyber LOCAL configured on S2 (Maestro port) and S3 (Marcduino). | |
| 6.2 | Send a Marcduino-format command from WCB1 terminal targeting S3. | Kyber device responds (audio/light sequence visible). | |
| 6.3 | From WCB2: `;W1,<kyber_command>` | WCB1 receives and routes to Kyber via S2/S3. | |
| 6.4 | Enable `?DEBUG,ON` on WCB1. Send plain broadcast from WCB2. | Debug confirms Kyber ports S2 and S3 are skipped for broadcast forwarding. | |

---

## Phase 7 — MP3 Trigger

**Load config:** `WCB*_02_full.txt`

| # | Test | Expected | P/F |
|---|------|----------|-----|
| 7.1 | `?MP3,LIST` on WCB1. | S5, 9,600 baud, volume 0, no error callback. | |
| 7.2 | From WCB1: `;A,PLAY,1` | MP3 Trigger plays track 1 (audio audible). | |
| 7.3 | While playing: `;A,STOP` | Playback stops. | |
| 7.4 | `;A,VOL,32` then play a track. Then `;A,VOL,0` and play same track. | Audible volume difference between the two plays. | |
| 7.5 | `;A,VOLUP` × 3, then `;A,VOLDN` × 3 | Volume increases then decreases in steps. No crash. | |
| 7.6 | Play a track, then `;A,NEXT` / `;A,PREV` | Correct adjacent tracks play. | |
| 7.7 | Pre-save: `?SEQ,SAVE,testcb,;W2,?config`. Then: `;A,PLAY,1,ONFIN,testcb` | When track 1 finishes, WCB1 auto-executes `testcb` → WCB2 config prints. | |
| 7.8 | `;A,COUNT` | MP3 Trigger returns track count. Value appears in WCB1 terminal as `[MP3] Status: ...` | |
| 7.9 | `;A,VER` | MP3 Trigger returns firmware version string in terminal. | |
| 7.10 | From WCB3 S2 (Marcduino Clone), send `;A,PLAY,2` command. | Data traverses W3S2 → W1S5 raw serial mapping → MP3 Trigger plays track 2. | |

---

## Phase 8 — Raw Serial Mapping (W1S5 ↔ W3S2, Bidirectional)

**Load config:** `WCB*_02_full.txt`

| # | Test | Expected | P/F |
|---|------|----------|-----|
| 8.1 | `?MAP,SERIAL,LIST` on WCB1 and WCB3. | WCB1: S5 mapped RAW to W3S2. WCB3: S2 mapped RAW to W1S5. | |
| 8.2 | WCB3 S2 → WCB1 S5 (forward). Have Marcduino Clone transmit a known string on S2. | Those exact bytes arrive at WCB1 S5 (MP3 Trigger effect observable). | |
| 8.3 | WCB1 S5 → WCB3 S2 (reverse). From WCB1: `;S5,TEST` | "TEST" bytes arrive at WCB3 S2 and forwarded to Marcduino Clone. | |
| 8.4 | Send a known binary pattern through the mapping. | No bytes dropped, no string-parsing corruption. Data arrives unchanged. | |
| 8.5 | WCB3 S2 transmitting continuously while WCB1 sends `;A,PLAY,1`. | Both directions work concurrently without data loss or lockup. | |

---

## Phase 9 — PWM Passthrough (W2S3 → W2S4 + W3S5)

**Load config:** `WCB*_02_full.txt`

| # | Test | Expected | P/F |
|---|------|----------|-----|
| 9.1 | `?MAP,PWM,LIST` on WCB2 and WCB3. | WCB2: S3=input mapped to S4 and W3S5. WCB3: S5=PWM output. | |
| 9.2 | Enable `?DEBUG,PWM,ON` on WCB2. Have Maestro ID2 generate PWM on Ch1 to WCB2 S3. | Debug output shows pulse width values (e.g. `PWM S3: 1500µs`). | |
| 9.3 | Observe Servo 3 (WCB2 S4) while Maestro ID2 drives PWM. | Servo 3 moves in response to PWM input changes. | |
| 9.4 | Observe Servo 4 (WCB3 S5) during same PWM input. | Servo 4 mirrors Servo 3 — PWM transmitted via ESP-NOW to WCB3 and output on S5. | |
| 9.5 | From WCB2: `;P4,1500` then `;P4,500` then `;P4,2500` | Servo 3 moves to center, full left, full right. | |
| 9.6 | From WCB1: `;W3,;P5,1500` | Servo 4 moves to center. | |
| 9.7 | Adjust Maestro ID2 Ch1 by <6µs. Enable `?DEBUG,PWM,ON`. | No ESP-NOW transmission triggered — stability threshold filters micro-jitter. | |

---

## Phase 10 — Sequence Storage

**Load config:** `WCB*_01_base.txt`

| # | Test | Expected | P/F |
|---|------|----------|-----|
| 10.1 | `?SEQ,SAVE,testseq,;W2,?config^;W3,?config` on WCB1. | No error, saved confirmation. | |
| 10.2 | `?SEQ,LIST` on WCB1. | `testseq` appears with its contents. | |
| 10.3 | `;Ctestseq` from WCB1. | WCB2 and WCB3 both print their configs. | |
| 10.4 | `;SEQtestseq` from WCB1. | Same result as 10.3. | |
| 10.5 | `?reboot` WCB1. After reconnect: `?SEQ,LIST`. | `testseq` still present (NVS persisted). | |
| 10.6 | `?SEQ,CLEAR,testseq`. Then `;Ctestseq`. | Removed from list. `;C` produces no output. | |
| 10.7 | Save multiple sequences. `?SEQ,CLEAR,ALL`. | All sequences removed. `?SEQ,LIST` shows empty. | |

---

## Phase 11 — Backup & Restore

**Load config:** `WCB*_02_full.txt`

| # | Test | Expected | P/F |
|---|------|----------|-----|
| 11.1 | `?backup` on WCB1, WCB2, WCB3. Copy each output string. | All end with `?CHK<hex>` then `End of Backup`. No truncation. | |
| 11.2 | **WCB3** — `?ERASE,NVS` → paste WCB3 backup string. | Board accepts all commands, CRC passes, prints `End of Backup`. `?config` confirms Maestro M3, raw mapping S2↔W1S5, PWM output S5 all restored. | |
| 11.3 | **WCB2** — erase and restore. | Maestro M1/M2 local, PWM mapping S3→S4+W3S5 all restored. | |
| 11.4 | **WCB1** — erase and restore (most complex). | Kyber LOCAL, MP3 S5:9600:V0, raw mapping S5↔W3S2, Maestro routing all restored. `?config`, `?MP3,LIST`, `?MAESTRO,LIST`, `?MAP,SERIAL,LIST` all verify clean. | |

---

## Phase 12 — Debug Commands

**Load config:** Either base or full

| # | Test | Expected | P/F |
|---|------|----------|-----|
| 12.1 | `?DEBUG,ON` on WCB1. Send a few commands. Then `?DEBUG,OFF`. | Verbose processing output visible during ON. Stops when OFF. | |
| 12.2 | `?DEBUG,ETM,ON` on WCB1. Send `;W2,?config`. | ETM packet detail visible (sequence number, retry count, ACK). | |
| 12.3 | `?DEBUG,PWM,ON` on WCB2. Wiggle Maestro ID2 Ch1 (full config loaded). | Pulse widths print as they change. | |
| 12.4 | `?DEBUG,MAESTRO,ON` on WCB2. Trigger `;M11`, then `;M1,goHome`. | Binary Pololu frame bytes visible for both the subroutine trigger and the verb. | |

---

## Phase 13 — Statistics

**Load config:** `WCB*_02_full.txt` — run after Phases 3–10

| # | Test | Expected | P/F |
|---|------|----------|-----|
| 13.1 | `?STATS` on each board. | Non-zero message counts, minimal failures, ACK rates consistent with tests run. | |
| 13.2 | `?STATS,RESET` on WCB2. Immediately `?STATS`. | All counters zeroed. | |

---

## Phase 14 — Edge Cases & Stress

**Load config:** `WCB*_02_full.txt`

| # | Test | Expected | P/F |
|---|------|----------|-----|
| 14.1 | Power-cycle all three boards simultaneously. Watch terminals. | All three come online, ETM heartbeats exchange, all show "came ONLINE" within ~15 seconds. No board stuck waiting. | |
| 14.2 | Unplug WCB2 USB. Run `;W2,?config` from WCB1. | ETM retries then reports failure gracefully — no crash on WCB1 or WCB3. | |
| 14.3 | Replug WCB2 after 14.2. Run `;W2,?config`. | WCB2 comes back online, heartbeat resumes, command succeeds. | |
| 14.4 | Send a 10+ command chain (separated by `^`) from WCB1. | All segments execute, no truncation or crash. | |
| 14.5 | Send `?NOTACOMMAND`, `;ZZZZZ`, `?ERASE` (wrong syntax) to WCB1. | Board does not crash. Prints error or ignores gracefully. | |
| 14.6 | Maestro ID2 generating continuous PWM (WCB2 S3) while sending rapid unicast commands between boards. | PWM passthrough continues without drops. ETM ACKs not blocked by PWM traffic. | |
| 14.7 | `;A,PLAY,1` on WCB1 S5 while WCB3 S2 Marcduino Clone is actively transmitting through raw mapping. | Audio plays, mapping data flows, no lockup or corruption on either path. | |

---

## Sign-off

| Phase | Description | Pass | Fail | Notes |
|-------|-------------|:----:|:----:|-------|
| 1 | Wizard Detect & Flash | ☐ | ☐ | |
| 2 | Configuration Verification | ☐ | ☐ | |
| 3 | ETM Network | ☐ | ☐ | |
| 3B | Broadcast Filtering | ☐ | ☐ | |
| 4 | Serial Port Commands | ☐ | ☐ | |
| 5 | Maestro Servo Controllers | ☐ | ☐ | |
| 6 | Kyber Integration | ☐ | ☐ | |
| 7 | MP3 Trigger | ☐ | ☐ | |
| 8 | Raw Serial Mapping | ☐ | ☐ | |
| 9 | PWM Passthrough | ☐ | ☐ | |
| 10 | Sequence Storage | ☐ | ☐ | |
| 11 | Backup & Restore | ☐ | ☐ | |
| 12 | Debug Commands | ☐ | ☐ | |
| 13 | Statistics | ☐ | ☐ | |
| 14 | Edge Cases & Stress | ☐ | ☐ | |

**Overall result:** ☐ PASS — ready to merge to main &nbsp;&nbsp;&nbsp;&nbsp; ☐ FAIL — see notes above

**Tester signature:** _________________________ &nbsp;&nbsp;&nbsp;&nbsp; **Date:** _____________
