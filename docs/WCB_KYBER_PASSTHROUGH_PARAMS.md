# WCB Kyber-Remote Serial Pass-Through — Integration Parameters

For streaming raw serial through a WCB acting as an ESP-NOW ↔ serial mediator
(e.g. droidnet ⇄ WCB ⇄ mesh ⇄ WCB ⇄ Maestro). Derived from firmware
`6.2.0_141455RJUL2026`. Values are what the firmware actually enforces — design
your side to these, not to the theoretical ESP-NOW maximums.

**Data path:** local serial (Maestro/droidnet) → WCB batches the port →
one ESP-NOW frame (broadcast, target ID 98 "Kyber") → remote WCB → remote
serial UART. Bytes are written straight to the far UART in arrival order.

---

## 1. Frame & payload

| Parameter | Value |
|---|---|
| **Max data bytes per ESP-NOW frame (this path)** | **64 bytes** — the forwarder batches each local port through a 64-byte buffer |
| Absolute chunk cap of the underlying sender | 180 bytes (never reached on this path — batch buffer caps first) |
| On-wire struct | 249 bytes (password + IDs + 200-byte command field) |
| Per-frame internal header | first 2 bytes of the payload = little-endian chunk length (transport-internal; you never set it) |
| Poll cadence | source port is drained every ~1 ms; a full 64-byte buffer is sent and reading continues, so one poll can produce several frames |

**Design to a 64-byte effective MTU.** If more than 64 bytes arrive at the
source UART between two 1 ms polls, the forwarder sends the full buffer and keeps
reading, so the write goes out as several frames (`forwardMaestroDataToRemoteKyber`
and `forwardDataFromKyber` in WCB.ino). Nothing is discarded at this layer. But each
frame is lost independently (§2), so a write spread over more frames is more likely
to lose a piece. Keep any single logical write ≤ 64 bytes where you can.

## 2. Reliability — best-effort, zero guarantees

This channel is a **fire-and-forget broadcast**. There is:

- **No MAC-layer ACK** (frames go to a group/broadcast address → 802.11 gives no ACK, no hardware retry)
- **No application ACK, sequence number, CRC, dedup, or retransmit** (the raw/Kyber path never enters the reliable "ETM" machinery)
- **No ordering guarantee across frames** (no reassembly / gap detection; in-order arrival relies solely on the RF layer under light load)
- **No error signal on drop** — a failed send only increments an internal counter; the serial reader gets no backpressure

> **Build your own framing, integrity, ordering, and retry above this channel.**
> Treat every frame as independently lossy and possibly reordered.

## 3. Byte transparency — fully 8-bit clean

- All **256 byte values pass unchanged, including `0x00`** (payload is length-counted, not string-terminated).
- No parsing, escaping, or substitution of your data — it is `memcpy`'d in and written out verbatim.
- Raw/Kyber frames bypass the WCB's `;`-command and JSON parsers entirely, so control bytes like `0x3B (';')`, `0x5E ('^')`, `0x2A ('*')` are **safe** in a binary stream (e.g. Pololu Maestro compact/Pololu protocol).

## 4. Throughput & pacing

- The binding limit is the **egress UART**, not the radio: `baud / 10` (8N1).
  - **57600 baud → 5,760 bytes/s**
  - 9600 baud → 960 bytes/s
- Serial format is fixed **8N1**.
- **Pace your sends to the egress baud.** If you stream faster than the far UART
  drains, the far-end TX ring fills and the write blocks *inside the WCB's WiFi
  receive callback* — which stalls ESP-NOW reception and drops subsequent inbound
  frames for that board. Overrunning is worse than just losing your own bytes.

## 5. Silent-drop points to avoid

1. **Frame loss** — every frame is an unacknowledged broadcast. On the HIL bench about 1 % of frames are lost (measured 2026-09-22: 21 of ~2,080). So a write that spans 11 frames loses at least one piece about 10 % of the time.
2. **256-byte UART RX FIFO** — if the poll task is delayed by scheduler jitter, the source port's default 256-byte hardware FIFO can overrun on top of the 64-byte cap.
3. **Egress UART backpressure** — see §4; overrunning the far UART stalls all mesh RX on that board.

All three lose data **without any error indication**.

## 6. What you can / cannot rely on

| Rely on | Do **not** rely on |
|---|---|
| 8-bit binary transparency (all 256 values incl. NUL) | Delivery (no ACK at any layer) |
| Fixed 8N1 framing | Retransmission / error recovery |
| In-arrival-order writes under light RF load | Ordering across frames |
| ≤ 64-byte frames at ≤ egress baud getting through cleanly | Gap / loss / drop detection or any error signal |

## 7. Prerequisites (config, not data constraints)

- Both boards must share the same **ESP-NOW password** and **WCB group** (frames failing either are dropped before reaching the pass-through).
- The pass-through serial port's **baud** is set at runtime (`?BAUDS<n>,<baud>`), e.g. `57600` for a Maestro in dual-port TTL mode. Match both ends.
- **Each port has exactly one reader.** While a Kyber mode is live, the bridge task reads every local Maestro port and the command parser skips them (`maestroPortOwnedByKyberBridge`, WCB.ino). Text sent into a bridged port is forwarded as data and never parsed as a `;` command. Two readers would split each frame between them.
- **A local Kyber owns only its own port.** `?KYBER,LOCAL` takes S1 or S2; S3-S5 are refused, because software-serial RX cannot run at the Kyber's 115200. It also refuses a port that hosts a local Maestro, including through its own target list (`?KYBER,LOCAL,S1,M1:W<self>S1:...`), and `?MAESTRO` refuses the current Kyber port. Otherwise the Maestro's baud would replace the Kyber's, and the bridge would write the Maestro's bytes back into the same port (`kyberLocalPortRefused`, `kyberTargetOnKyberPort`, WCB_Storage.cpp; `configureMaestro`, WCB_Maestro.cpp). The command parser and the broadcast fan-out skip only `kyberLocalPort`. The other hardware port is a normal command port and follows its `?BCAST` flags, and configured Maestro ports are still excluded one by one. A Maestro with no `?MAESTRO` slot on the other port therefore receives text broadcasts: give it a slot, or use `?BCAST,OUT,S<n>,OFF`. In targeted mode a local Maestro's Kyber target follows its slot: a newly created `?MAESTRO` slot adds it, and `?MAESTRO,CLEAR` removes it (`configureMaestro` / `_clearMaestroSlot`, WCB_Maestro.cpp). Re-issuing `?MAESTRO` for an existing slot, such as a baud change, leaves the target table alone. So the Kyber never writes a port the clear has handed back, and `?backup` cannot re-create a cleared Maestro. Remote targets are not affected. The broadcast fan-out deliberately does not consult Kyber targets: a freed port follows its `?BCAST` flags. The legacy Maestro fallbacks (no slot for the board's own id: `;M<own id>`, `;M9`, the `;M0` extra frame, a get on the own id) write S1 only while S1 is not the Kyber's port or an HCR/MP3/DFP/WLED/PWM port (`legacyS1Owner`, WCB_Maestro.cpp); otherwise they send nothing. `?backup` releases first and claims late. It emits `KYBER,CLEAR` (or `MAESTRO,REMOTE`) ahead of the `BAUD`/`BCAST` lines, and the `KYBER,LOCAL` line after the Maestro and device lines. A restore that moves or swaps the Kyber is therefore not refused, and does not undo its own port settings.
- **The other hardware port takes devices and PWM.** A Kyber mode reserves one port from HCR/MP3/DFP/WLED and PWM: the Kyber's own port on a Kyber_Local board, S1 on a Maestro_Remote board (`kyberModeReservesPort`, WCB_Storage.cpp, which the device guards, `canUsePWMOnPort` and `;P` all call). The other hardware port of a Kyber_Local board is configured like any port. At boot it begins its UART at the saved baud unless a PWM input or output owns its pins, as on a board with no Kyber, and the Kyber's own port always begins (`setup()`, WCB.ino). `?KYBER,LOCAL` refuses a port a device or PWM owns, so the Kyber never moves onto one. A WDP PWMTARGET advert that names the free port auto-configures a PWM output there, as on any board (WDP_DESIGN.md). In targeted mode the bridge never writes a Kyber target on a port an HCR/MP3/DFP/WLED or PWM output owns (`forwardDataFromKyber`): a target can outlive or never have a Maestro slot (a table saved by older firmware, or an explicit target list when all nine slots are full). Two traps remain. The device guards do not check for a local Maestro, so on the usual layout (Kyber on S2, Maestro on S1) a typed `?HCR,PORT,S1` is accepted and re-bauds the Maestro's port; the Wizard hides that port behind its Maestro claim, so only the CLI can do this. And a PWM mapping saved on the free port before the board went Kyber local, which older firmware skipped at every boot, loads again: check `?MAP,PWM,LIST` and remove it with `?MAP,PWM,CLEAR,S<n>` if it is stale. A restore needs no special order: `?backup` and the Wizard send the release (`KYBER,CLEAR`, which also ends Maestro REMOTE) ahead of the device lines and claim `KYBER,LOCAL` after them.
- **Leaving a port gives it back.** `?KYBER,CLEAR`, `?MAESTRO,REMOTE` (`?KYBER,REMOTE`) and a `?KYBER,LOCAL,S<n>` that moves the Kyber all release the old port the same way: 9600 baud, `?BCAST` input and output on (`kyberReleasePort`, WCB_Storage.cpp). A port a local Maestro or another subsystem uses at that moment keeps its settings, so a move whose target list puts a Maestro on the old port leaves that Maestro's baud alone. CLEAR and REMOTE zero `kyberLocalPort`, so KyberLocalTask, which gates on it alone, idles at once. A bare `?KYBER,LOCAL` switches to broadcast mode on the current port, or S2 if the board is not Kyber local yet. On a board that booted Kyber local, an S1↔S2 move takes effect immediately: the task follows `kyberLocalPort`, and the parser reads the released port.
- **A Kyber mode change needs a reboot.** `setup()` creates the bridge tasks from the saved mode, and nothing stops them later. After `?KYBER,CLEAR` the remote bridge idles and its ports go back to the parser (`forwardMaestroDataToRemoteKyber`, WCB.ino), but the S1/S2 setup is only chosen at boot, so `?KYBER,CLEAR` on a board that was in a Kyber mode says to reboot (`WCB_Storage.cpp`). `?MAESTRO,REMOTE` on a board that booted Kyber local stops the old Kyber task reading its port at once; the remote bridge starts at the reboot.

---

### TL;DR for droidnet

Stream in **≤ 64-byte chunks**, **paced to ≤ 5,760 B/s @ 57600** (baud/10), as
**8-bit-clean binary** — and layer your **own framing + integrity + ordering +
retry** on top, because the channel gives none. Don't burst above 64 bytes/ms or
above the egress baud, or bytes are dropped silently and (if you overrun the far
UART) mesh reception on that board stalls.

---

## Revision log

Newest first. One row per change that altered what this document describes.

| Date | Commit | Change | Why |
|---|---|---|---|
| 2026-09-23 | — | §7: devices and PWM are refused only on the port a Kyber mode owns (the Kyber's own port; S1 under Maestro REMOTE); the other hardware port boots like any port; the bridge skips a Kyber target on a device/PWM port | Tracker #73 D4: both hardware ports stayed reserved after #14, so a PWM output saved on the other one was dropped from NVS at the next boot while `;P` still drove the pin. HIL `kyber.local_free_port_takes_pwm`, `wizard.kyber_local_frees_other_port` |
| 2026-09-23 | — | §7: every way out of Kyber LOCAL releases its port unless a Maestro/device now uses it; REMOTE zeroes kyberLocalPort; bare ?KYBER,LOCAL keeps the current port; ?backup releases before BAUD/BCAST | Tracker #73 D5/D9: a move or ?MAESTRO,REMOTE left the old port at 115200 with broadcasts off, and REMOTE left KyberLocalTask reading it beside the parser until reboot. HIL `kyber.local_port_move_releases_old`, `wizard.kyber_release_order` |
| 2026-09-23 | — | §7: local Kyber targets follow their slots (a new `?MAESTRO` slot adds its target, `?MAESTRO,CLEAR` removes it); the legacy S1 fallbacks skip an owned S1; line cites refreshed | Tracker #73 D6/D7: a cleared Maestro's target kept the Kyber writing a freed port, and `?backup` re-created the slot; `;M0` wrote Maestro frames into the Kyber port and S1 devices. HIL `kyber.local_clear_maestro_drops_target`, `kyber.local_s1_legacy_fallback_skips_kyber_port` |
| 2026-09-22 | — | §1/§5: a burst over 64 bytes is sent as several frames, not discarded; §5 gives the measured ~1 % frame loss | The forwarder has flushed a full buffer and kept reading since that bug was fixed; the doc still described the drop. HIL `kyber.remote_roundtrip` lost one of 11 frames (tracker #74) |
| 2026-09-22 | — | §7: a local Kyber owns only its own port. `?KYBER,LOCAL` refuses a local-Maestro port (directly or through its target list), `?MAESTRO` refuses the Kyber port, and the backup emits `KYBER,LOCAL` after the Maestros | Tracker #14 (the other hardware port had no reader and got no broadcasts, `kyber.local_port_s1_frees_s2`) and Greg's #28 decision. The refusal was first written only for the direct form, which the backup/Wizard target form bypassed |
| 2026-09-22 | — | §7: one reader per port, and Kyber mode changes need a reboot | HIL `kyber.maestro_s2_single_reader` / `kyber.clear_warns_reboot_single_reader`: the parser also read a local Maestro port on a Kyber board, and after `?KYBER,CLEAR` S1 had two readers (tracker #59) |
