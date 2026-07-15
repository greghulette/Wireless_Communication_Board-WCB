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
| Poll cadence | source port is drained every ~1 ms; ≤1 frame per poll |

**Design to a 64-byte effective MTU.** If more than 64 bytes arrive at the
source UART between two 1 ms polls, the excess is **read and silently discarded**
(see §5). Keep any single logical write ≤ 64 bytes and pace so you don't queue
more than ~64 bytes per millisecond into the WCB's serial port.

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

1. **64-byte batch cap** — > 64 bytes arriving at the source port between two 1 ms polls: excess dropped, no error.
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

---

### TL;DR for droidnet

Stream in **≤ 64-byte chunks**, **paced to ≤ 5,760 B/s @ 57600** (baud/10), as
**8-bit-clean binary** — and layer your **own framing + integrity + ordering +
retry** on top, because the channel gives none. Don't burst above 64 bytes/ms or
above the egress baud, or bytes are dropped silently and (if you overrun the far
UART) mesh reception on that board stalls.
