# WCB Full Code Review — pre-`main` audit

**Started:** 2026-08-17 · **Branch:** `OTA` · **Target:** merge to `main` (which auto-publishes a
GitHub Release users flash).

Exhaustive review of the **entire** codebase — firmware, Wizard, CI, tests — not just recent
changes. This is a **working audit log**, not a design doc. It exists so the review can be paused
and resumed across sessions without re-doing analysis.

Severity: **S1** crashes / corrupts / bricks · **S2** wrong behaviour in a real scenario ·
**S3** latent race, bound, leak, overflow · **S4** dead code, stale comment, doc drift.

Confidence: **VERIFIED** = traced in the source by hand in-session · **UNVERIFIED** = a reviewer
agent reported it but the adversarial verification pass never ran (see *Session 1 outcome*).
**Treat UNVERIFIED as a lead, not a fact** — a large share of raw review findings normally die
under verification.

---

## ✅ REVIEW IS SCOPE-COMPLETE — 2026-08-18

All four waves finished. **53 review scopes, 407 findings.**

| Wave | Scope | Findings | File |
|---|---|---|---|
| 1 | `WCB.ino` (15 scopes: 12 ranges + 3 lenses) | **106** (F-006…F-036 partial, F-257…F-362) | [`_wave1.md`](CODE_REVIEW_2026-08_wave1.md) |
| 2 | 14 firmware subsystem modules | **98** (F-037…F-134) | [`_wave2.md`](CODE_REVIEW_2026-08_wave2.md) |
| 3 | The Wizard (14 scopes) | **122** (F-135…F-256) | [`_wave3.md`](CODE_REVIEW_2026-08_wave3.md) |
| 4 | 10 cross-cutting sweeps + CI + cross-repo | **81** (F-363…F-443) | [`_wave4.md`](CODE_REVIEW_2026-08_wave4.md) |

Severity spread: **11 S1 · 95 S2 · 109 S3 · 69 S4** (plus the 5 hand-verified F-001…F-005).

> **Read this before acting on anything.** All agent findings are **UNVERIFIED**. Of the ~20 I
> hand-verified, **3 were refuted or materially downgraded and 1 was promoted**. The *observations*
> have held up well; the *severity calls and proposed fixes* need checking. Verify before you fix.

### ✅ VERIFICATION COMPLETE — 128/128 — read this instead of the raw findings — [`_verdicts.md`](CODE_REVIEW_2026-08_verdicts.md)

Every S1/S2 finding was given to a fresh adversarial verifier told to assume it was wrong, open the
cited file, re-derive every constant itself, grep for guards and callers, and check comments and git
history for deliberate intent.

| Result | Count |
|---|---|
| CONFIRMED | 124 |
| REFUTED | 4 |
| **Severity revised** | **92 of 128 (72 %)** |
| **Proposed fix was WRONG** | **65 of 128 (51 %)** |

**Findings survive; severities and fixes do not.** 124 of 128 describe a real defect — the reviewers
were almost never hallucinating. But **72 % were mis-graded** and **51 % shipped a remedy that would
not have worked.** Work from the verdict, never from the original finding.

| Claimed | → S1 | → S2 | → S3 | → S4 | → REFUTED |
|---|---|---|---|---|---|
| **S1 (15)** | **4** | 8 | 2 | — | 1 |
| **S2 (113)** | 1 | 28 | **76** | 5 | 3 |

The mass migration is **S2 → S3: 76 of 113**. Most claimed "wrong behaviour in a real scenario"
findings are actually latent — unchecked bounds that today stay in range, races with millisecond
windows, robustness gaps. Genuine, worth fixing, not release-blocking.

### The four REFUTED — do not re-raise

| File:line | Why it fell |
|---|---|
| `Wizard/app.js:5361` | Code shape and crash propagation are both real, but the required state is unreachable — `boardAutoDetect()`, the finding's entire reachability argument, is **dead code**. |
| `WCB.ino:6417` | "Grows until the heap is exhausted" is false: `String::changeBuffer` refuses any allocation over `CAPACITY_MAX` (`WString.cpp:199`), so the buffer is bounded by the String implementation. |
| `WCB.ino:7214` | The ordering observation is true — `commandDelimiter` really is still `'^'` at migration time — but both claimed consequences are false: `recallCommandSlot` strips comments **delimiter-independently** via `commentDelimiter = "***"`. |
| `WCB_Storage.cpp:978` | See the correction to Tier 1 item 6 below. |

**Correction to my own Tier 1 item 6.** I elevated "`?ERASE,NVS` boots into a broken pin map"
(F-377) on the grounds that wave 4 had supplied the reachable path F-038 lacked. The verifier
confirmed **every mechanism link** — it even opened the V3.2 KiCad netlist and confirmed pad `J1_5`
is `GPIO5` on net `S1_RX` with a direct trace, no series resistor — and still refuted it, correctly:

- Clearing `hw_version` is **documented, deliberate behaviour**, stated in two user-facing places
  before the user commits: `?HELP,ERASE` lists "- Hardware version" (`WCB_Help.cpp:955`) and the
  Wizard's factory-reset modal names it explicitly (`Wizard/index.html:362-364`).
- The finding's "nothing warns the user" premise — **which I repeated** — is false.
  `printHWversion()` runs at `WCB.ino:7208`, ~16 lines before the `SerialN.begin()` block, and
  prints `SET YOUR HARDWARE VERSION BEFORE PROCEEDING!!` (`WCB_Storage.cpp:141`) on the port that
  still works.
- It is not a second defect: everything harmful belongs to the `hw_version == 0` branch, which
  fires on **every freshly flashed board with no erase at all**. That is F-038, already confirmed
  at S2. What the erase adds is *timing* (the board is already wired), which is a reachability note
  on F-038, not a separate finding.
- It is also self-recovering: only S1-S5 and the LED are affected — USB and the radio still work, so
  `?HW,32` + reboot restores it, and the Wizard's erase→push path does that automatically.

Residual value: **S4** — `eraseNVSFlash()`'s farewell line (`WCB_Storage.cpp:1030`) could read
"NVS cleared — set ?HW,xx before use. Restarting…". Take that string; drop the rest.

### The actual S1 list — 2 distinct bugs

Five findings survive as S1, but they collapse to **two root causes**:

1. **`etmCharPhaseSentTimes` out-of-bounds write** — `WCB.ino:577` and `:1470`.
   `etmCharMessageCount × peerCount` reaches 3800 against a 200-entry row. The *read* side already
   guards `mIdx < 200` (`:1249`), proving 200 was intended. Triggerable remotely via the Wizard's
   ETM button.
2. **MGMT relay chunk truncation (180 vs 179)** — three sites, one off-by-one:
   `Wizard/app.js:7852` (fragments at 180), `Wizard/app.js:4455` (`?SEQ,SAVE` sent unfragmented),
   and `Code/WCB/WCB.ino:2617` (relay `strncpy`s 179 and silently drops the rest).
   `WCBClient` already uses the correct 179.

**Everything else can ship.** That is the single most useful output of this whole review: 407 raw
findings reduce to two must-fix bugs, plus a long tail of real-but-latent work.

#### How to actually fix the two S1s — verified guidance

The verifiers went past confirming and found things the original findings got wrong or missed.
**Do not apply the fixes as filed.**

**S1-A · `etmCharPhaseSentTimes`**

- The trigger is **much wider than "11+ boards"**. The real condition is
  `etmCharMessageCount * peerCount > 200`, and `?ETM,COUNT` accepts up to 200 (`WCB.ino:4696`) — so
  `?ETM,COUNT,200` then `?ETM,CHAR` on a **3-board mesh** already overruns by 800 bytes. Triaging
  this as "needs a big fleet" would be wrong.
- Precise blast radius: the array is 2400 bytes (`unsigned long` is **4** bytes on xtensa, not 8).
  Phase 1 (row 0) and phase 2 (row 1) stay *inside* the object and cross-corrupt the other phases'
  timestamps — bad latency data, no memory corruption. Only **phase 3** (`:1506`, `:1513`) writes
  past the end. Worst case 200 × 19 peers reaches ~14.4 KB beyond the array.
- Which globals get hit **cannot** be asserted from source order — `.bss` layout is the linker's
  choice. Claim the OOB write, not a named casualty.
- **A write-guard alone is not enough.** Both read sites already discard `mIdx >= 200`
  (`:1250`, `:1569`), so ACKs beyond 200 never register, `totalAcked >= totalMessages` (`:1485`,
  `:1531`) can never become true, and **every phase runs to its full timeout** — count 200 with
  2 peers stalls characterization for ~11.6 minutes. Clamping fixes both:

  ```cpp
  int perBoard = min(etmCharMessageCount, 200 / peerCount);   // peerCount <= 20, floor 10
  int totalMessages = perBoard * peerCount;                   // provably <= 200
  ```
  Warn in `startETMChar()` when `perBoard < etmCharMessageCount`, and fix the "Messages per board
  per phase" header at `:1436` to report `perBoard`.

**S1-B · MGMT relay chunk truncation — my earlier "one-character fix" was incomplete**

I described this as a one-character change to `MGMT_CHUNK_SIZE`. **That is necessary but not
sufficient**, and it does not address a second hazard sitting on the same boundary:

- Scope is narrower than "every push": the relay short-circuits
  `totalChunks == 1 && payload.length() <= 198` into an ETM unicast carrying the **full** payload
  (`WCB.ino:2596-2603`), so small single-chunk diff pushes are fine. It hits every relayed push
  over 180 chars — full pushes after a flash, multi-setting diffs. The **final** chunk is also
  truncated when the total length is an exact multiple of 180.
- **The separate hazard:** `processLocalCommand` opens with
  `if (message.endsWith("?")) { printCommandHelp(cmd); return; }` (`WCB.ino:4402`). If a chunk
  boundary happens to land immediately after a `?`, that chunk's line ends in `?` and the whole
  `?MGMT,FRAG,…` is swallowed as a **help request** — the fragment never forwards, the session dies
  at the 15 s `MGMT_SESSION_TIMEOUT_MS`, and the entire push is silently lost. Changing 180→179
  only *relocates* the boundaries; it does not remove this. The durable fix is to hoist the
  `?MGMT,` test **above** the help shortcut.
- **Do not "fix" it by widening `payload[180]`.** The 226-byte `espnow_struct_mgmt` size is
  load-bearing for the size-first receive router (`WCB.ino:804-808`), and
  `WCBClient/src/WCB_Client.h:199` pins the same `char payload[180]`. Keep 179; fix the senders.
- Add a loud, ungated reject in `handleMgmtForward` so any future over-long sender fails visibly
  instead of corrupting NVS:
  ```cpp
  if (payload.length() > sizeof(pkt.payload) - 1) {
      Serial.printf("[MGMT] FRAG payload %u > %u — rejected\n",
                    payload.length(), (unsigned)(sizeof(pkt.payload) - 1));
      return;
  }
  ```
- Also unearned: the success toast at `app.js:4458` and the baseline patch at `:4468-4475` fire on
  the un-ACKed broadcast path, which hides the damage from the next diff push.

**One correction to my own earlier analysis.** I wrote that the `commandGroups` use-after-free could
be triggered by "*any* serial line arriving on USB or S1-S5 — the Wizard pushing a config…". **That
is wrong.** `WCB.ino:6489` guards the call:

```cpp
if (!data.startsWith(String(LocalFunctionIdentifier)) && isTimerCommand(data)) {
    parseCommandGroups(data);
```

Every `?`-prefixed line — the entire Wizard config push, `?SEQ`, `?MGMT`, `?OTA` — goes to
`parseCommandsAndEnqueue` and never touches `commandGroups`. Only an exact `?STOP` or a **non-`?`**
line containing `;t`/`;T` can mutate it from `serialCommandTask`. The defect is real and the six
derivations stand, but exposure is ~1 ms per command per group-fire against a 5 ms poll — order
0.5–1 % per operator action. **S2, not S1.** The verifier also found a better fix: repair the
*victim* (stop holding a reference across the yield in `processCommandGroups`) rather than deferring
every mutator.

**One correction to my own earlier analysis.** I wrote that the `commandGroups` use-after-free could
be triggered by "*any* serial line arriving on USB or S1-S5 — the Wizard pushing a config…". **That
is wrong.** `WCB.ino:6489` guards the call:

```cpp
if (!data.startsWith(String(LocalFunctionIdentifier)) && isTimerCommand(data)) {
    parseCommandGroups(data);
```

Every `?`-prefixed line — the entire Wizard config push, `?SEQ`, `?MGMT`, `?OTA` — goes to
`parseCommandsAndEnqueue` and never touches `commandGroups`. Only an exact `?STOP` or a **non-`?`**
line containing `;t`/`;T` can mutate it from `serialCommandTask`. The defect is real and the six
derivations stand, but the exposure is roughly a 1 ms window per command per group-fire against a
5 ms poll — on the order of 0.5–1 % per operator action, not "reproduces under any load". **S2, not
S1.** The verifier also proposed a better fix: repair the *victim* (stop holding a reference across
the yield in `processCommandGroups`) rather than deferring every mutator.

### Triage — what to deal with first

> **SUPERSEDED.** Written before verification. Useful only as a *grouping* of same-shape bugs that
> share one fix. For severity and for what blocks the release, use **The actual S1 list** above and
> [`_verdicts.md`](CODE_REVIEW_2026-08_verdicts.md). Of the six items here: 2 and 4 remain S1,
> 1/3/5 were downgraded, and **6 was refuted outright**.

**Tier 1 — fix before merging to `main`.** Multiply-derived, hand-verified, or trivially checkable:

1. **`commandGroups` use-after-free** — F-006/F-037/F-258/F-259/F-307/F-363, **six independent
   derivations** plus my own trace. `serialCommandTask` calls `parseCommandGroups()` →
   `commandGroups.clear()` while `loop()` holds a live reference across an explicit `vTaskDelay`.
2. **`etmCharPhaseSentTimes[3][200]` overrun** — F-001/F-007/F-257/F-304/F-305/F-306, **six**.
   Index reaches `etmCharMessageCount × peerCount` = up to 3800. Note the *read* side already
   guards `mIdx < 200` (`WCB.ino:1249`), proving 200 is the intended ceiling — only writes are
   unguarded. Reachable remotely via the Wizard's ETM button (`:3246`).
3. **F-260 · ETM CHKSM silently drops commands > 187 chars *and ACKs them anyway*** — hand-verified.
   `etmSendAck()` at `:3823` fires before CRC verification at `:3855`. Sender sees 100 % delivery.
4. **F-139 + F-262 · MGMT relay chunk off-by-one** — hand-verified from both sides; `WCBClient`
   already uses the correct 179. One-character fix in `Wizard/app.js:67`.
5. **F-367 · `build.sh` can push a green build that DELETES the released binaries** — if the branch
   name contains a `/`, every `cp` fails, unchecked, and the script still exits 0 so CI commits the
   deletion. *This is the release pipeline itself.*
6. **F-038 + F-377 · factory reset boots into a broken pin map.** `?ERASE,NVS` clears the
   `hw_version` namespace, so the board comes up at `hw_version == 0`, which maps **all ten serial
   pins to GPIO5**. Wave 4 supplied the reachable path that wave 2's finding lacked — these two are
   one bug and it is squarely on a normal user flow.

**Tier 2 — same-shape families; each is one fix, not many:**

- **Port-ownership guards** (F-044/046/047/049/073) — five device configurators each omit a
  different device. One shared `portOwner(port)` helper.
- **Factory-reset namespace list** (F-042/061/062/376/377/378) — `dfp_cfg` and `stored_commands`
  missed; the migration guard lives *inside* the namespace that gets cleared, so erasing
  resurrects deleted sequences. Third time this list has been found incomplete — derive it.
- **The `endsWith("?")` help intercept** (F-387, and the root cause behind F-138) — makes "set the
  character back to `?`" unexpressible, *including the exact examples the built-in help prints*.
- **Custom-funcChar breaks backup verification** (F-274/383) — backup emits `<funcChar>CHK`, both
  verifiers hard-code `"?CHK"`.
- **Help text drift** (F-025…F-036, F-368…F-372) — ~20 findings; mostly mechanical.

**Tier 3 — cross-repo, needs coordinated pushes:** F-364 (`WCB_Client` decodes a WDP SOLICIT as an
advert, wiping the sender's neighbour record) and F-366 (`Wizard/serial-hub.js` is missing two
lock-squat fixes that NaviCore's required-lockstep copy has).

### Where the agents were wrong

Recorded so nobody re-raises these:

- **F-048 — REFUTED.** `saveHWversion()` validates before writing; the missing `else` is unreachable.
- **F-273 — REFUTED.** `?EPASS,password` is handled correctly by `WCB.ino:4750`.
- **F-272 — downgraded S2 → S4.** Real double-strip, but off the config path (`?LABEL` is used).
- **F-322 — downgraded S2 → S3.** The S1/S2-vs-S3-5 baud-guard asymmetry is real, but a 0 baud is
  **not reachable**: `updateBaudRate` whitelists 13 values (`WCB_Storage.cpp:156-160`), the NVS load
  falls back to the compiled 9600 (`:251`), and `eraseNVSFlash` only `clear()`s the namespace
  (`:942`) which restores that same fallback. Worth adding for symmetry; not a blocker.
- **F-039 — downgraded S1 → S2.** Real infinite loop, but needs `debugETM`/`debugEnabled` on.
- **F-138 — symptom real, proposed fix wrong.** The firmware cannot parse `FUNCCHAR,?` at all.

---

## Session 2 status — 2026-08-18

**Wave 2 (firmware subsystems) COMPLETE — all 13 reviewers landed, 98 findings.**
Full text in **[CODE_REVIEW_2026-08_wave2.md](CODE_REVIEW_2026-08_wave2.md)** as **F-037…F-134**.

| Severity | Count |
|---|---|
| S1 | 3 |
| S2 | 34 |
| S3 | 39 |
| S4 | 22 |

> **All 98 are UNVERIFIED.** A second session limit killed every verifier before one ran. The
> workflow's own output labels them `CONFIRMED`, but that label is an **artefact** of the rule
> *"confirmed if no verifier refuted it"* evaluated against an empty verifier list. Do not read it
> as a judgement.

### Independent corroboration (raises confidence materially)

Two wave-2 reviewers, working from different scopes and with no knowledge of my hand-verification,
independently found what I had already traced by hand:

- **F-037** (`command_timer.cpp:187`) is the same defect as **F-006** — the `commandGroups`
  use-after-free across the explicit `vTaskDelay` yield. Two independent derivations plus my own
  source trace. **Treat as real.**
- **F-070** (`WCB_Storage.cpp:2229`) is the same defect as **F-002** — `etmMissedHeartbeats`
  reverting to 3. **Treat as real.**

### The two other S1s from wave 2 — read these first

- **F-038** `wcb_pin_map.cpp:14` — hw_version 0, the out-of-box default, maps all ten serial pins to
  **GPIO5**, driving GPIO5 as an output on HW 3.1/3.2 where it is the S1_RX *input* header pin.
  Pairs with **F-048**: `updatePinMap()` has no final `else`, so an unrecognised stored hw_version
  leaves every pin global at 0 and binds all five ports to GPIO0.
- **F-039** `WCB_RemoteTerm.cpp:111` — RTERM mirroring plus `espNowSendCallback` form a
  **self-sustaining infinite ESP-NOW/UART loop** when the relay is unreachable.

### A theme worth noting

Seven separate S2s are the *same shape*: **port-ownership guards that each omit a different
device.** `?HCR,PORT` omits DFPlayer (F-044); `?MP3` omits DFPlayer (F-047); `configureWLED` omits
DFPlayer and local Maestro (F-073); `canUsePWMOnPort()` omits local Maestro and DFPlayer (F-049);
and `configureMaestro()` has **no port guard at all** (F-046). Any one of these lets a device
silently seize and re-baud a UART another device already owns. This looks like one fix — a single
shared `portOwner(port)` helper — not seven.

Likewise **F-042/F-061/F-062** are three reports of one bug: `dfp_cfg` missing from the factory-reset
namespace list. The adjacent comment in `WCB_Storage.cpp` records that HCR, WLED and vars were
*already* missed once for exactly this reason. Third occurrence — the list wants to be derived, not
hand-maintained.

### Known duplicates to merge when triaging

F-042 ≡ F-061 ≡ F-062 · F-058 ≡ F-059 · F-068 ≡ F-069 · F-037 ≡ F-006 · F-070 ≡ F-002.

### Budget lesson, refined

Wave 2 cost **4.5M subagent tokens / 209 agents / 27 min**. The 13 *reviewers* were cheap; the
**196 verifier agents were what blew the limit**. For the remaining waves: **run reviewers only, no
verification pass**, then hand-verify just the S1/S2 findings. Hand-verification has been both
cheaper and sharper — it upgraded F-006 from "resize race" to "use-after-free" and killed nothing.

### Wave 1 — `WCB.ino` — PARTIAL (6 of 12)

**47 findings** in **[CODE_REVIEW_2026-08_wave1.md](CODE_REVIEW_2026-08_wave1.md)** as
**F-257…F-303** (3 S1 · 17 S2 · 17 S3 · 10 S4). A third session limit killed six scopes.

**Completed:** `seg05-send`, `seg06-configpull`, `seg08-dispatch`, `seg09-localcmd`,
`seg11-tasks`, `lens-concurrency`.
**KILLED — still unreviewed:** `seg02-tables`, **`seg07-rxcallback`**, `seg10-routing`,
`seg12-setup-loop`, `lens-bounds`, `lens-deadcode`.
`seg07-rxcallback` is the highest-value gap in the whole review — `espNowReceiveCallback` is the
function that parses untrusted packets straight off the air. **Run it first next session.**

#### Triangulation — my three top findings are now multiply-derived

Independent reviewers in *different waves*, with no shared context, re-derived the same defects.
This is the strongest confidence signal available without a bench test:

| Defect | Derivations |
|---|---|
| `commandGroups` use-after-free | **F-006** (wave 1 s1) · **F-037** (wave 2) · **F-258** + **F-259** (wave 1 s3) · my own source trace = **5** |
| `etmCharPhaseSentTimes[3][200]` overrun | **F-001** (hand) · **F-007** (wave 1 s1) · **F-257** (wave 1 s3) = **3** |
| MGMT relay chunk truncation | **F-139** (wave 3, Wizard side, hand-verified) · **F-262** (wave 1 s3, firmware side, found independently) = **2 sides of one bug** |

**F-262 deserves emphasis:** a wave-1 reviewer looking only at the firmware found that
`handleMgmtForward` truncates every FRAG payload to 179, while a wave-3 reviewer looking only at
the Wizard found it fragments at 180. Neither saw the other. Together with `WCBClient`'s
`WCB_MGMT_CHUNK_LEN 179`, the off-by-one is settled.

#### New high-value from this wave

- **F-260** `WCB.ino:2298` — with `?ETM,CHKSM,ON`, a command over **187 chars** has its CRC
  truncated by the `strncpy` into `structCommand[200]`, so the receiver rejects it — **but
  `etmSendAck()` fires at `:3823`, *before* CRC verification at `:3854-3873`**. The sender gets a
  valid ACK, `?STATS` shows 100% delivery, and the rejection is logged only under `debugETM`.
  Silent data loss that actively reports success.
- **F-261** `WCB.ino:2599` — `handleMgmtForward` prepends a `\x01` wizard-origin marker, but only
  the *ETM* receive path strips it (`:3881`). With `?ETM,OFF` every single-chunk relayed command
  arrives as `"\x01?SEQ,SAVE,…"` and is dropped as unrecognised — while multi-chunk push still
  works, so the relay looks healthy.
- **F-274** `WCB.ino:5772` — backup emits the checksum as `<funcChar>CHK`, but both verifiers
  hard-code `"?CHK"`; on a board with a custom FUNCCHAR, restore integrity checking is silently
  skipped.
- **F-276** `WCB.ino:6608` — `Kyber_Local` / `Maestro_Remote` are runtime-settable, but
  `KyberLocalTask` / `KyberRemoteTask` are created **only at boot**, so toggling them live leaves
  S1/S2 with no reader at all.

#### Hand-verified from this wave

**F-260 — CONFIRMED, and it is the most serious protocol defect found so far. Promote to S1.**
*Silent data loss that actively reports success.*

Send side (`WCB.ino:2294-2302`):

```cpp
uint32_t crc = calculateCRC32(String(message));
char withCRC[210];
snprintf(withCRC, sizeof(withCRC), "%s|CRC%08X", message, crc);
strncpy(etmMsg.structCommand, withCRC, sizeof(etmMsg.structCommand) - 1);  // 199
etmMsg.structCommand[sizeof(etmMsg.structCommand) - 1] = '\0';
```

`structCommand` is `char[200]`, so exactly **199** characters survive. The suffix `|CRC` + 8 hex
digits is **12** characters, so the CRC is intact only while `strlen(message) + 12 <= 199`, i.e.
**message ≤ 187 chars**. Longer messages get the hex digits — or the `|CRC` marker itself — sliced
off, and the receiver rejects the packet at `WCB.ino:3856` (missing CRC) or `:3867` (mismatch).
**There is no length guard anywhere on this path.**

The reason it is S1 rather than S2 is the ordering on the receive side. `etmSendAck()` fires at
**`WCB.ino:3823`**, while CRC verification does not happen until **`:3855-3874`** and `return`s on
failure. So the receiver **ACKs the packet and then silently discards it**:

- the sender clears its pending entry and counts a successful delivery;
- `?STATS` reports 100 %;
- ETM's retry machinery never fires, because from its point of view nothing failed;
- the only trace is a `debugETM`-gated log line on the receiver, and `debugETM` is off by default.

This is routinely reachable: `handleMgmtForward` gates its single-chunk shortcut at
`payload.length() <= 198` (`WCB.ino:2597`) and then prepends a 1-byte marker, handing
`sendESPNowMessage` strings of up to **199** chars — its comment budgets "200 − 1 marker − 1 null"
and never accounts for the 12 CRC bytes. The retry builder at `:1311-1314` repeats the identical
construction, so retries reproduce the truncation byte-for-byte instead of recovering.

*Fix:* reject or fragment instead of truncating — guard on `strlen(message) + 12 > 199` when
`etmChecksumEnabled`, name the limit as a constant, and use it at `:2597` in place of the
hard-coded 198. Separately, **move `etmSendAck()` to after CRC verification** (or send a distinct
NACK) so a rejected packet is never reported to the sender as delivered.

*Doc note:* `CLAUDE.md` rule 4 states CHKSM "drops the max command to 188 chars". The arithmetic
gives **187** as the last safe length (188 + 12 = 200 > 199). Off by one — worth correcting when
this is fixed, since the constant is about to become load-bearing.

**F-272 — CONFIRMED, but downgraded S2 → S4 (dead legacy alias, not a live config bug).**
The double-strip is real: the dispatcher passes `message.substring(3)` (`WCB.ino:5279`), so
`?SLS1,Dome` reaches `updateSerialLabel()` as `"1,Dome"`, which then fails its own
`startsWith("S")` guard at `:5475` and returns *"Invalid format"* — the command can never succeed.
(Same bug class as the `?SBO`/`?SBI` double-strip fixed previously.)
**But it is not on the config path:** the backup emits `LABEL,S<n>,<text>` (`WCB.ino:2807`) and the
Wizard emits the same (`parser.js:1322`), both handled by the modern `rootUpper == "LABEL"` branch
(`:4570`). So backup/restore is unaffected; only a user hand-typing the legacy form is bitten.
Pair it with **F-032** (help still advertises the equally-dead `?SLC1`) as one "retire the dead
legacy label commands" cleanup.

**F-273 — REFUTED for the documented form; downgraded S2 → S4.**
`?EPASS,password` never reaches the cited code: `rootUpper == "EPASS"` matches at `WCB.ino:4750`,
uses `args` correctly, and **returns**. The `substring(6)` at `:5703` is reached only by an
*undocumented* no-comma spelling (`?EPASSfoo` → rootCmd `"EPASSfoo"` ≠ `"EPASS"`), where it does
drop one character. Help documents the comma form and the backup emits it (`:2796`), so no
real user path loses a password character.

### Wave 3 — the Wizard — COMPLETE

**14/14 agents, zero failures, 122 findings.** Full text in
**[CODE_REVIEW_2026-08_wave3.md](CODE_REVIEW_2026-08_wave3.md)** as **F-135…F-256**.
Run reviewers-only (no verifier agents) — 2.6M tokens, 27 min, no limit trouble. That configuration
is the one to keep.

| Severity | Count |     | File | Count |
|---|---|---|---|---|
| S1 | 9 | | `app.js` | 84 |
| S2 | 58 | | `parser.js` | 14 |
| S3 | 36 | | `flasher.js` | 8 |
| S4 | 19 | | `serial-hub.js` | 7 |
| | | | `index.html` | 6 |

> All 122 are **UNVERIFIED** by design.

**The nine S1s, in one line each:**

| # | Where | What |
|---|---|---|
| F-135 | `app.js:454` | `reconcileBoardGrid` can delete a board slot **mid-flash** (it checks `isConnected()` but not `_boardFlashing`), killing the post-flash config restore |
| F-136 | `app.js:4455` | Sequence save over a relay exceeds the single-packet limit and is **truncated at 179 chars** — stored half a sequence, green toast |
| F-137 | `app.js:5361` | Boot-banner sniffer dereferences `boardConfigs[n]` unguarded → TypeError kills the read loop → **endless reset/reconnect loop** on any board without a card yet |
| F-138 | `app.js:7447` | Setting Local Function Char back to `?` is **never sent**; the rest of the push is then mis-dispatched and **broadcast to the whole mesh** |
| **F-139** | `app.js:67` | **VERIFIED — see below.** Relay push fragments at 180 but the relay forwards 179 |
| F-140 | `app.js:7938` | Remote-pull listener is **not target-filtered** — a config reply lands on *every* board with a pull in flight |
| F-141 | `app.js:7946` | Same defect, second site: two concurrent pulls cross-contaminate, and a later push writes one board's settings onto another |
| F-142 | `app.js:11748` | Guided-wizard "Connect & Push" reports **"✓ Done" even when the push never happened** |
| F-143 | `parser.js:1531` | (see wave-3 doc) |

**F-139 — HAND-VERIFIED, CONFIRMED S1. Silent config corruption on every relayed push > 180 chars.**

- Wizard: `const MGMT_CHUNK_SIZE = 180` (`Wizard/app.js:67`), used by
  `fragmentString(cmdToSend, MGMT_CHUNK_SIZE)` (`:7852`), which slices **exactly 180** chars per
  chunk (`:7761-7767`).
- Firmware relay: the packet field is `char payload[180]` (`WCB.ino:793`), and the forward does
  `strncpy(pkt.payload, payload.c_str(), sizeof(pkt.payload) - 1)` — i.e. **179** — then
  `pkt.payload[179] = '\0'` (`WCB.ino:2617-2618`).

So the 180th character of every full chunk is dropped. Only the final (short) chunk survives intact.
The target reassembles a string with a character missing at each chunk boundary — corrupting
whatever command straddles it — and the Wizard reports success.

**Cross-repo corroboration that 179 is the correct value:** the sibling `WCBClient` library already
has this right and documents the reason —
`WCBClient/src/WCB_Client.h:189`: `#define WCB_MGMT_CHUNK_LEN 179   // payload[180] minus NUL
(firmware strncpy)`. The Wizard is the only one of the three that uses 180.
*Fix:* `MGMT_CHUNK_SIZE = 179` in `Wizard/app.js:67`, with a comment naming `WCB.ino:2617` so it
cannot drift back.

**F-138 — CONFIRMED as a symptom, but the proposed fix is WRONG and the real fix is in the firmware.**

The suppression at `Wizard/app.js:7447` is real:

```js
if (config.funcChar !== curFuncChar && config.funcChar !== '?')
    bootstrap.push(`${curFuncChar}FUNCCHAR,${config.funcChar}`);
```

Going *from* a custom char *back to* `?` satisfies the first term and fails the second, so no
`FUNCCHAR` command is sent — while the rest of the push is built with `lfi = config.funcChar`
(`parser.js:1226`), i.e. `?`. The board is still on the old char, so every pushed `?…` command is
not recognised as a local function and falls through to the broadcast path — out the serial ports
and over ESP-NOW to the mesh. **That part of the finding stands.**

But the wave-3 reviewer's fix — "drop the `&& config.funcChar !== '?'` term" — **does not work**,
and the code comment right above the line explains why: *"Never send `?FUNCCHAR,?` — '?' suffix is
re-parsed as a new command invocation."* That is accurate. `processLocalCommand` opens with

```cpp
if (message.endsWith("?")) {          // WCB.ino:4402
    String cmd = message.substring(0, message.length() - 1);
    cmd.trim();
    printCommandHelp(cmd);
    return;
}
```

`FUNCCHAR,?` ends with `?`, so it is consumed as *help for `FUNCCHAR,`* and returns — **regardless
of which prefix character carries it**. `xFUNCCHAR,?` fails exactly the same way. The firmware
simply has no parseable way to express "set the function char back to `?`".

*Real fix — firmware side:* exempt `FUNCCHAR,` from the `endsWith("?")` help branch, or add an
explicit spelling such as `?FUNCCHAR,DEFAULT`. Then the Wizard guard can be relaxed.
*Interim Wizard mitigation:* refuse the change with a clear message rather than silently pushing a
mismatched prefix — a config push that sprays the mesh is much worse than a rejected edit.

### Hand-verification of wave-2 findings (session 3, ongoing)

Working the S1/S2 list by hand, since that has proved both cheaper and sharper than the agent
verifier pass. Verdicts are recorded here **in place** — including refutations, so nobody re-raises
them.

**F-038 — CONFIRMED reachable; severity corrected to S2 (with an unconfirmed S1 electrical risk).**
`loadHWversion()` (`WCB_Storage.cpp:100-106`) does `getInt("hw_version", 0)` and then calls
`updatePinMap()`. On a fresh or factory-erased board that has never had `?HW` set, `wcb_hw_version`
is **0**, which takes the branch at `wcb_pin_map.cpp:10-25` — setting **all ten serial pins to
GPIO5**. `Serial1` and `Serial2` (both `HardwareSerial`) plus the three `SoftwareSerial` instances
are then all `begin()`-ed on pin 5.
*Certain consequence:* all five serial ports are non-functional out of the box until `?HW` is set.
*Unconfirmed:* the reviewer's claim of electrical damage. On HW 3.1/3.2, GPIO5 **is** the real
`SERIAL1_RX_PIN` (`wcb_pin_map.cpp:102`), so the firmware does drive an input header pin as an
output — but I have not confirmed that produces damage rather than just contention, and I am not
going to assert it without a bench check.
*Also:* the branch's comment claims *"This pinout is for a Version 1.0 board (LilyGo T7 V1.5
ESP32)"* — copy-pasted and wrong; the real v1.0 map is the **next** branch (`:27-42`).
*Fix:* make hw_version 0 either refuse to bring up any serial port, or fall back to a real map, and
fix the comment.

**F-048 — REFUTED. Do not re-raise.**
The claim was that an unrecognised stored `hw_version` leaves every pin global at 0 because
`updatePinMap()` has no final `else`. The missing `else` is real, but it is **unreachable via any
command path**: `saveHWversion()` (`WCB_Storage.cpp:74-79`) validates against
`{1, 21, 23, 24, 31, 32}` *before* the NVS write and returns early otherwise. Its comment records
this exact bug already being found and fixed — *"The old order wrote the value first, so an invalid
?HW,0 … clobbered a previously-saved hardware version."* Only externally-corrupted NVS could store
an out-of-set value. (The `0` case is separately handled and is F-038.)

**Verified clean — `?HW` command parsing.** I suspected a serious defect here and it does not exist;
recording it so the next session does not chase it. `updateHWVersion()` (`WCB.ino:5712`) does
`message.substring(2).toInt()`, which on `"HW,32"` yields `",32"`, and `atol(",32")` is **0**
(confirmed by compiling it). That would have made every config restore inert, since both the
firmware backup (`WCB.ino:2776`, `emit("HW," + hwSuffix, …)`) and the Wizard (`parser.js:1241`,
`` add(`HW,${config.hwVersion}`) ``) emit the **comma** form. But the comma form never reaches that
function: a modern dispatch branch at **`WCB.ino:5121`** matches `rootUpper == "HW"`, uses
`args.toInt()` (`args` = `"32"`), and `return`s. `WCB.ino:5243` is only the legacy no-comma
`?HW32` path, where `substring(2)` is correctly `"32"`. **Both forms work.**

#### `WCB_RemoteTerm.cpp` — all four findings resolved (read together; they compound)

**F-057 — CONFIRMED, and it is the root cause of the other three.**
`WCB_RemoteTerm.cpp:183` calls `WCBDebugSerial.HardwareSerial::printf(...)` with the comment
*"Use parent-class printf to avoid re-triggering the WCBSerial forwarding path."* **That does not
work, and cannot.** In the ESP32 core's `Print.h`: `printf` is **not virtual** (`Print.h:71`) while
`write(uint8_t)` and `write(const uint8_t*, size_t)` **are** (`Print.h:57`, `:64`). Qualifying the
call suppresses virtual dispatch on `printf` — which was never virtual — but `Print::printf`
internally calls `write()`, and *that* still dispatches to `WCBSerial::write`
(`WCB_RemoteTerm.cpp:55`), which forwards whenever `_relayWCB` is set.

Independent corroboration that this is a genuine misunderstanding rather than my misreading: the
*same construct* at `:123` carries the **opposite** comment — *"(forwarding is now active so the
relay sees it too)"*. Two comments, one construct, contradictory claims. The `:123` one is correct.
*Fix:* to actually bypass, call `HardwareSerial::write(buf, len)` directly, or clear `_relayWCB`
around the call. Then correct the comment.

**F-039 — REAL but NOT unconditional; corrected from S1 to S2.** The reviewer stated the loop
occurs "when the relay is unreachable". It additionally requires debug output to be on.
`espNowSendCallback` (`WCB.ino:4231-4239`) only prints under `if (debugETM || debugEnabled)`. With
debug off there is no print and no loop. With debug **on** + an active RTERM session + an
unreachable relay, the cycle closes and is genuinely self-sustaining:

```
esp_now_send fails → espNowSendCallback → Serial.printf → WCBSerial::write
  → _bufChar → (on '\n') _flushLine → _sendPacket → esp_now_send → fails → …
```

One failed send produces exactly one more line, hence one more send — it never damps out, and
`_sendPacket` is re-entered from inside the send-callback context. The three preconditions are not
exotic: *someone debugging a failing relay session is precisely the person with `debugETM` on.*
Fixing F-057 breaks this loop.

**F-055 — CONFIRMED.** Any `Serial` write performed inside `espNowReceiveCallback` (WiFi task)
while `_relayWCB` is set reaches `WCBSerial::write` → `_sendPacket` → `esp_now_send`, i.e. an
ESP-NOW transmit issued from within the ESP-NOW receive callback, once per received packet that
produces a line. Note `WCB.ino:377`'s `[RCBRG] relay queue FULL` printf is **unconditional**, so
this is reachable even with all debug flags off. Fixing F-057 does not fix this one — the guard has
to be at the write path.

**F-056 — CONFIRMED.** `_relayWCB` is cleared only by `stopSession()` (`:126-132`); nothing in the
file expires it, and there is no timeout, heartbeat, or liveness check anywhere in the module. If
the subscriber goes away without sending a stop, the board mirrors **every** serial line over
ESP-NOW forever. This is the substrate the other three sit on: a latched session is what makes
F-039 and F-055 reachable at all.
*Fix:* expire `_relayWCB` after a silence window, the way the temporary-peer TTL already does
elsewhere.

---

## Session 1 outcome — READ FIRST

A 52-scope multi-agent review was launched across 4 waves. **The account session limit was hit
about 13 minutes in**, and 48 of 52 reviewers plus *every* verification agent were killed
mid-flight.

| Wave | Scopes | Completed | State |
|---|---|---|---|
| 1 · `WCB.ino` | 15 | **15 — DONE** | complete |
| 2 · Subsystems | 14 | **14 — DONE** (`WCB_Help` s1; other 13 in session 2) | complete |
| 3 · Wizard | 14 | **14 — DONE** (session 3) | complete |
| 4 · Cross-cutting + CI | 10 | **10 — DONE** | complete |

36 raw findings were salvaged from the 4 completed reviewers (F-006…F-036 below). **None went
through verification.** 5 further findings were verified by hand in-session (F-001…F-005).

### How to resume

Re-launch the remaining waves. The four workflow scripts are saved and re-runnable — edit the
`SEGMENTS` / `TARGETS` / `SWEEPS` array in each to drop the scopes already done, then invoke
`Workflow({ scriptPath: "<path>" })`.

Scripts live under
`C:\Users\ghulette\.claude\projects\c--Users-ghulette-...-WCB\ab549030-.../workflows/scripts/`:

| Wave | Script | Drop these already-done scopes |
|---|---|---|
| 1 · WCB.ino | `wcb-review-wcbino-wf_59fb4814-ccd.js` | **already patched** — 6 left: seg02, **seg07-rxcallback**, seg10, seg12, lens-bounds, lens-deadcode |
| 2 · Subsystems | — | **DONE, do not re-run** |
| 3 · Wizard | — | **DONE, do not re-run** |
| 4 · Cross-cutting | `wcb-review-crosscutting-wf_eccf29c0-4e6.js` | — none |

`resumeFromRunId` replay is **same-session only**, so after a limit reset the scripts must simply
be re-run with the completed scopes removed.

**Budget note:** the 4 waves burned ~5.2M subagent tokens in 13 minutes and still only got ~8%
through. Run **one wave at a time**, and consider dropping the verifier count from 3 lenses to 1-2.

### Still to review

**Nothing.** All 53 scopes are complete. What remains is *verification and triage*, not coverage.

---

## Baseline — verified 2026-08-17 on this machine

| Check | Result |
|---|---|
| Firmware **ESP32** `PartitionScheme=min_spiffs` | **exit 0** — flash 1 289 711 B = **65%** of 1 966 080; RAM 86 592 B = **26%** |
| Firmware **ESP32-S3** `PartitionScheme=min_spiffs` | **exit 0** — flash 1 255 695 B = **63%**; RAM 84 840 B = **25%** |
| WDP wire test (`-Wall -Wextra`) | no warnings, **all 9 checks pass** |
| Wizard JS syntax (`node --check` x6) | all parse |
| Wizard inline JS (`jscheck.js index.html`) | 0 errors (0 inline blocks — all JS is external) |

Both targets compile clean, but **not warning-free** — see F-003 and F-004.

Toolchain: arduino-cli 1.5.1 · g++ (GCC) 16.1.0 · node v24.18.0.

---

## Findings

Never renumber. Mark `FIXED` / `WONTFIX` / `REFUTED` in place so a later session does not
re-litigate.

### VERIFIED in-session

**F-001 · S1 · `Code/WCB/WCB.ino:577`, `:1470`, `:1506`, `:1513` — `etmCharPhaseSentTimes[3][200]` global buffer overrun**

`totalMessages = etmCharMessageCount * peerCount` (`WCB.ino:1456`). `etmCharMessageCount` is
`constrain(..., 10, 200)` with default 20 (`:269`, `:4696`, `:5303`); `peerCount` counts online
active peers and can reach `MAX_WCB_COUNT` = 20 (`:1445-1448`). The write at `:1470` indexes the
200-entry row with `etmCharPhaseMessageIndex`, which runs `0 .. totalMessages-1`.

**11 online peers at the default count = 220 > 200.** Worst case 200 x 20 = 4000. The writes run
off the end of the row and stomp adjacent globals. Phase 3 (`:1506`, `:1513`) has the identical
unbounded write. Trigger: `?ETM,CHAR` on a fleet of 11+ boards.
*Fix:* clamp the index, or size the array `[3][MAX_WCB_COUNT * 200]`, or cap `totalMessages` at
200 and say so in the output.

**F-002 · S2 · `Code/WCB/WCB.ino:267` + `Code/WCB/WCB_Storage.cpp:2229` — the widened ETM offline window never ships**

`etmMissedHeartbeats = 5`, with a comment explaining it was *deliberately* widened from 3 so a busy
mesh does not flap a live board. But the NVS load uses fallback **3**:
`etmMissedHeartbeats = preferences.getInt("etmMiss", 3)`. Any board that never explicitly ran
`?ETM,MISS` gets 3 at every boot — a 33 s offline window, not the documented 55 s. The comment
describes behaviour that does not occur.
*Fix:* make the NVS fallback 5.

**F-003 · S3 · `Code/WCB/wcb_pin_map.h:5-6` — malformed include guard**

`#ifndef wcb_pin_map.h` / `#define wcb_pin_map.h`. A `.` is not valid in a macro name, so this
defines `wcb_pin_map` with stray trailing tokens `.h`. It happens to function as a guard, but emits
two warnings in **every** translation unit on **both** targets (`extra tokens at end of #ifndef
directive`, `ISO C++11 requires whitespace after the macro name`).
*Fix:* `WCB_PIN_MAP_H`.

**F-004 · S3 · `Code/WCB/WCB.ino:377` — non-atomic read-modify-write on a `volatile` counter**

`rcJsonRelayDrops++` draws `'++' expression of 'volatile'-qualified type is deprecated
[-Wvolatile]`. `enqueueRcJsonRelay` is reached from the WiFi task *and* from `otaRelayPrint()`
(`:387`), so the RMW can interleave and lose counts — on a diagnostic whose whole purpose is that
drops must never be invisible.
*Fix:* `portENTER_CRITICAL` around it, or `std::atomic<uint32_t>`.

**F-006 · S1 · `Code/WCB/WCB.ino:6490` (+ `:6431`, `:399`) — use-after-free on `commandGroups`, from the serial path**
*(promoted from UNVERIFIED; the agent found it but under-stated it — it is a use-after-free, not just a resize race)*

The rationale comment at `WCB.ino:390-399` says the ESP-NOW path must defer `parseCommandGroups()`
through a queue because `loop()` concurrently iterates the vector, and then claims:
*"The local-serial entry point and SEQ playback both run inside loop() already, so they keep calling
parseCommandGroups() directly."* **That claim is false.**

- `serialCommandTask` is its own FreeRTOS task, `xTaskCreatePinnedToCore(..., 4096, NULL, 1, NULL, 1)`
  at `WCB.ino:7439` — Core 1, priority 1.
- It calls `processIncomingSerial` → `processSerialCommandHelper` → **`parseCommandGroups(data)` at
  `WCB.ino:6490`**, and `stopTimerSequence()` at `:6431`.
- `loop()` (the Arduino `loopTask`, also Core 1, priority 1) calls `processCommandGroups()` at `:7481`.

Two preemptively-scheduled equal-priority tasks. The kill shot is in
`command_timer.cpp:187-208` — `processCommandGroups()` takes **a reference into the vector** and
then **yields while still holding it**:

```cpp
CommandGroup &group = commandGroups[currentGroupIndex];   // reference into the vector
...
for (const String &cmd : group.commands) {
    parseCommandsAndEnqueue(cmd, 0);
    vTaskDelay(pdMS_TO_TICKS(1));      // explicit yield, reference still live
}
```

During that yield `serialCommandTask` can reach `parseCommandGroups()`, whose first act is
`commandGroups.clear()` (`command_timer.cpp:85`) — destroying every `CommandGroup` and freeing the
backing store. When `loop()` resumes, `group` and `cmd` are dangling. **Use-after-free on the heap.**

This is not a narrow window: the yield is explicit, deliberate, and taken once per command in the
group. Any `;t` timer chain that is mid-flight when *any* serial line arrives on USB or S1-S5 —
the Wizard pushing a config, a controller sending a command, a device echoing — can hit it.

*Fix:* route the serial path through the same deferral the ESP-NOW path already uses — call
`enqueuePendingTimerChain(data)` at `:6490` instead of `parseCommandGroups(data)`, and defer the
`:6431` `stopTimerSequence()` to `loop()` behind a flag. Then correct the comment at `:399`.
*(Independently: `processCommandGroups` also indexes `group.commands[0]` at `command_timer.cpp:189`
without checking the group is non-empty.)*

**F-023 · S4 · `Code/WCB/WCB.ino:499` — `etmSequenceNumber` is entirely dead**

`grep -n 'etmSequenceNumber' WCB.ino` returns **exactly one line — its own definition**. Never read,
never written. Its comment ("global sequence counter, Layer 3 uses this") is false; Layer 3 uses
`etmSequenceCounter` (`:522`). Two near-identical names, one of them dead, is a trap.
*Fix:* delete it.

**F-024 · S4 · `Code/WCB/WCB.ino:1177` — `PendingMessage::targetWCB` is write-only**

`entry.targetWCB = targetWCB;  // store target for retry logic` — the only reference in the file.
Every other `.targetWCB` hit belongs to `kyberTargets[]` or a packet struct, not the pending table.
The retry logic at `:1200`/`:1212` uses the *local* `targetWCB` parameter, never the stored field.
The comment describes behaviour that does not exist.
*Fix:* delete the field, or use it in `processETMAcksAndRetries`.

**F-005 · S3 · `Code/WCB/WCB.ino:228` + `:231` — two packet types share value 5**

`PACKET_TYPE_MGMT_FRAG_UNICAST 5` and `PACKET_TYPE_CONFIG_REQ 5`. Benign *today* only because the
receive path routes by packet **size** first (226 B vs 43 B) before ever reading the type byte. It
is a live trap: any future struct matching one of those sizes, or any handler that switches on type
before size, mis-routes silently.
*Fix:* give `CONFIG_REQ` an unused value.

### UNVERIFIED — `WCB.ino` segment reviewers

Reported by agents; **verification never ran**. Confirm before acting.

| # | Sev | File:line | Finding |
|---|---|---|---|
| F-006 | — | — | **PROMOTED to VERIFIED above (S1, use-after-free).** |
| F-007 | S1 | `WCB.ino:1470` | Same overrun as **F-001**, found independently. |
| F-008 | S2 | `WCB.ino:1396` | `etmSendAck` sends without ensuring the ESP-NOW peer is registered, so ACKs to unregistered senders are silently dropped. |
| F-009 | S2 | `WCB.ino:1011` | `scheduleNextHeartbeat` underflows to a past deadline when `etmHeartbeatSec` <= 0; `?ETM,HB` has no range validation. |
| F-010 | S2 | `WCB.ino:1495`, `:4175` | *Partly checked:* the send at `:1495` IS `sendESPNowMessage(0, "ETMLOAD", false)` (non-ETM) and the receiver at `:4175` IS in the 249-byte normal-packet path — so the claim hinges on whether that path is gated off when `etmEnabled`. **To finish: read the ETM gate in `espNowReceiveCallback`.** ETM characterization **Phase 3 "Loaded Network" is unreachable** — the `ETMLOAD` trigger is sent non-ETM and dropped by every receiver's ETM-mismatch guard. Separately **Phase 2 is reported as "Broadcast" but sends unicast** (it is Phase 1 with a different delay), in both the comment and the results table. |
| F-011 | S2 | `WCB.ino:1989` | `parseCommandsAndEnqueue` recurses into itself and re-enters the `?CHK` branch, re-introducing the embedded-checksum abort its own comment says `lastIndexOf` fixed. |
| F-012 | S2 | `WCB.ino:1920` | `enqueueCommand` snapshots `lastReceivedViaESPNOW` / `inSequenceBody` from unsynchronised globals while running on the WiFi task. Related to the known per-command-flag bug class. |
| F-013 | S2 | `WCB.ino:1892` | `applyLiveBaud` does `end()` then `begin()` on a live `SoftwareSerial` while other tasks may be reading it; `end()` reportedly leaves `m_rxValid` true with a freed buffer. |
| F-014 | S2 | `WCB.ino:1569` | `processETMCharAck` silently discards ACKs for message index >= 200 — the read-side half of F-001. |
| F-015 | S3 | `WCB.ino:306` | The signed-delta "survives wraparound" deadline checks flip back to true ~24.85 days *after* a deadline expires, spuriously enabling the RC-JSON relay and disabling the OTA-relay gate. |
| F-016 | S3 | `WCB.ino:1034` | Heartbeat scheduling compares absolute `millis()` deadlines, so the 49.7-day wrap causes a ~10 s broadcast storm. |
| F-017 | S3 | `WCB.ino:441` | `enqueuePendingTimerChain` discards `xQueueSend`'s return value — a full 8-slot queue silently swallows an entire ESP-NOW timer chain. |
| F-018 | S3 | `WCB.ino:369` | `enqueueRcJsonRelay` `strncpy`s from an unchecked `String::c_str()`, and `otaRelayPrint` manufactures a heap `String` **on the WiFi task** to reach it. |
| F-019 | S3 | `WCB.ino:1156` | `etmAddToPendingTable`'s full-table eviction always drops slot 0, which is not necessarily the oldest entry. |
| F-020 | S3 | `WCB.ino:1452`, `:1844` | `processETMChar`'s "no online peers" abort skips `printETMCharResults`, the **only** place ETM-char state is unwound — an aborted run permanently leaves `debugETM` suppressed and `etmCharRelayRequesterWCB` latched. |
| F-021 | S3 | `WCB.ino:1690` | `buildStatsString` outgrows the 16-fragment relay budget on a large fleet, so a relayed `?STATS` returns nothing and logs only under `debugMGMT`. |
| F-022 | S3 | `WCB.ino:1651` | `buildStatsString` double-counts and double-prints the controller peer when its id falls inside the WCBQ floor. |
| F-023 | — | — | **PROMOTED to VERIFIED above.** |
| F-024 | — | — | **PROMOTED to VERIFIED above.** |

### UNVERIFIED — `WCB_Help` reviewer

User-facing text, cheap to fix, worth doing before release.

| # | Sev | File:line | Finding |
|---|---|---|---|
| F-025 | S2 | `WCB_Help.cpp:669` | `?WLED` help documents only the **superseded** single-WLED syntax; the current ID-addressed form is absent and the "One WLED per WCB" note is false. |
| F-026 | S2 | `WCB_Help.cpp:128` | `?BAUD` help recommends `?MAESTRO,ENABLE` — **a command that does not exist**. |
| F-027 | S2 | `WCB_Help.cpp:132` | `?BAUD` help documents the legacy form `?Sx,rate`; the parser rejects it (it takes no comma). |
| F-028 | S2 | `WCB_Help.cpp:364` | Help states the WCB board range is **1-9**; the firmware accepts **1-20**. |
| F-029 | S4 | `WCB_Help.cpp:1071` | **No help topic for `?OTA` / `?OTALOCAL`** and no OTA entry in the top-level menu — on the branch whose headline feature is OTA. |
| F-030 | S4 | `WCB_Help.cpp:1015` | Nine implemented `?` commands have no help topic — including `?TRACK`, whose own error message tells the user to type `?TRACK ?`. |
| F-031 | S4 | `WCB_Help.cpp:25` | `?DEBUG` topic omits four of the eight debug switches, including two the top-level menu itself advertises. |
| F-032 | S4 | `WCB_Help.cpp:160` | `?LABEL` help gives a legacy clear example (`?SLC1`) that is silently a no-op. |
| F-033 | S4 | `WCB_Help.cpp:1038` | `?DFP` exists as a help topic but is absent from the top-level `??` menu, so DFPlayer support is undiscoverable. |
| F-034 | S4 | `WCB_Help.cpp:480` | `?MP3` and `?HCR` topics omit the `REMOTE,W<n>` subcommand both handlers implement (the `?DFP` topic documents it). |
| F-035 | S4 | `WCB_Help.cpp:851` | `?WDP` help omits `POLL` and `DETAIL,<n>`, both listed by the command's own unknown-subcommand message. |
| F-036 | S4 | `WCB_Help.h:13` | A 216-line "COMPLETE COMMAND REFERENCE" comment block that is two minor versions stale and misses 14 subsystems. |

---

## Verified clean

Checked and correct — do not re-check.

- Both firmware targets compile (exit 0) with headroom: 65% / 63% flash, 26% / 25% RAM.
- `tests/wdp_wire_test.cpp` builds warning-free under `-Wall -Wextra`; all 9 spec checks pass
  (round-trip, unknown-TLV skip + truncation safety, SEQHASH round-trip, MAESTRO_CFG precedence,
  PWMTARGET self-name dedup, SOLICIT short-circuit, sender-MAC binding, single-owner election,
  auto-join needs >= 2 adverts).
- All Wizard JavaScript parses; `index.html` carries no inline script blocks.
- The **only** compiler warnings on either target are F-003 (x6) and F-004 (x1). Nothing else.

---

## Revision log

| Date | Change | Commit |
|---|---|---|
| 2026-08-17 | Log created; baseline established; 52 review scopes launched across 4 waves | — |
| 2026-08-19 | **VERIFICATION COMPLETE — 128/128.** 124 confirmed, 4 refuted; 92 severities revised (72%), 65 proposed fixes wrong (51%). Final S1 set = 2 distinct bugs. Tier-1 item 6 (?ERASE,NVS pin map) refuted and merged into F-038. | — |
| 2026-08-18 | **REVIEW SCOPE-COMPLETE.** Wave 1 finished (15/15, +59 findings F-304…F-362) and wave 4 finished (10/10, 81 findings F-363…F-443). 407 findings total. F-322 downgraded to S3 (0 baud unreachable). Wave 4 supplied the reachable path for F-038 via `?ERASE,NVS` (F-377), and independently confirmed the `build.sh` branch-slash hazard (F-367). | — |
| 2026-08-18 | Wave 1 partial (6/12): 47 findings F-257…F-303 in `_wave1.md`. Triangulation: commandGroups UAF now 5 independent derivations, chunk off-by-one confirmed from both sides. F-272 downgraded to S4, F-273 refuted. | — |
| 2026-08-18 | Wave 3 (Wizard) complete: 14/14 agents, 122 findings F-135…F-256 in `_wave3.md`. F-139 hand-verified S1 (relay chunk off-by-one, corroborated against WCBClient). Wave 1 remainder launched. | — |
| 2026-08-18 | Wave 2 complete: 13 reviewers, 98 findings (F-037…F-134) in `CODE_REVIEW_2026-08_wave2.md`, all UNVERIFIED — verifiers killed by a 2nd limit. F-006/F-002 independently corroborated. | — |
| 2026-08-18 | Session limit truncated the run at 4/52 scopes. Salvaged 31 agent findings (F-006…F-036, UNVERIFIED); hand-verified F-001…F-005. Resume instructions recorded. | — |
