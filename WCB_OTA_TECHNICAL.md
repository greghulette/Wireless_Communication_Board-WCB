# WCB OTA — Technical Reference

**Purpose:** how firmware OTA works on the WCB, at implementation detail, so it can be reproduced on **NaviCore** (ESP32‑S3, native USB‑Serial/JTAG, runs custom firmware + WCB_Client).
**Source of truth:** `Code/WCB/WCB_OTA.{h,cpp}`, the ESP‑NOW router/loop in `Code/WCB/WCB.ino`, and the host driver in `Wizard/{app.js,flasher.js}`.
**Scope:** the *target* side (a device receiving + applying an image) and the two transports (USB serial, ESP‑NOW relay). Brick‑safe by construction.

---

## 0. The one‑paragraph summary

A device updates itself by writing the **inactive** OTA app slot through the ESP‑IDF `esp_ota_ops` API, verifying the image (magic + SHA‑256) **before** flipping the boot pointer, and rebooting. Any failure or interruption leaves the currently‑running app untouched — it cannot brick. The image bytes get to the device over either **USB serial** (`?OTALOCAL,*`, base64, ACK‑paced) or **ESP‑NOW from a relay board** (`?OTA,*`, two packet types, ACK‑paced). Both transports drive the **same** `otaBegin/otaWrite/otaEnd` core. The single hard rule that makes the wireless path reliable: **never run the blocking flash calls inside the radio receive callback — defer them to the main loop.**

---

## 1. Prerequisite: a 2‑slot OTA partition table

`esp_ota_ops` needs a partition table with **two app (OTA) slots + an otadata sector**. The WCB uses `PartitionScheme=min_spiffs`:

| Partition | Offset | Size | Purpose |
|---|---|---|---|
| nvs | `0x9000` | `0x5000` (20 KB) | saved config |
| otadata | `0xE000` | `0x2000` (8 KB, two 4 KB sectors) | **boot selector** (which app slot boots) |
| ota_0 (app0) | `0x10000` | `0x1E0000` (~1.9 MB) | app slot A |
| ota_1 (app1) | `0x1F0000` | ~1.9 MB | app slot B |

- `esp_ota_get_next_update_partition()` returns the **inactive** slot; if the table has no spare OTA slot it returns `NULL` → OTA is impossible. **NaviCore must be on a 2‑OTA‑slot table** (one‑time full flash if it currently uses a single‑app/huge‑app scheme).
- App image ceiling ≈ each slot's size (~1.9 MB here).
- `nvs`/`otadata` offsets here are identical to the stock Arduino `default` scheme, so config survives switching schemes (as long as NVS isn't erased).

---

## 2. The brick‑safe core (transport‑agnostic)

One in‑flight session at a time (one `esp_ota_handle_t`). State struct:

```c
static struct {
  bool                   active;
  uint16_t               sessionId;
  uint32_t               imageSize;
  uint32_t               written;        // bytes committed == next expected offset
  uint8_t                chipFamily;
  esp_ota_handle_t       handle;
  const esp_partition_t *part;
  unsigned long          lastActivityMs;
  uint32_t               lastProgressPrint;
} ota = {0};
```

### Lifecycle — three calls + abort

```c
bool otaBegin(uint16_t sessionId, uint32_t imageSize, uint8_t chipFamily);
bool otaWrite(uint16_t sessionId, uint32_t offset, const uint8_t *data, uint16_t len);
bool otaEnd  (uint16_t sessionId);   // returns true → caller reboots
void otaAbortSession(const char *reason);
```

**`otaBegin`**
1. Tear down any stale session first (supersede).
2. **Chip‑family guard** (brick protection): `chipFamily` must equal this board's family (`0`=ESP32, `1`=ESP32‑S3, `0xFF`=unsupported). Reject mismatched images — flashing an S3 image to a classic ESP32 (or vice‑versa) would brick.
3. `part = esp_ota_get_next_update_partition(NULL)` → the inactive slot. Reject if `NULL`.
4. Size check: `0 < imageSize <= part->size`.
5. `esp_ota_begin(part, imageSize, &handle)` — **this erases the destination region (~1.2 MB); it BLOCKS for hundreds of ms to seconds.** (See §5.)
6. Latch session state, `written = 0`.

**`otaWrite`** — strictly in‑order streaming:
- Reject if no active session or `sessionId` mismatch.
- **`offset` MUST equal `ota.written`** (the running cursor). Out‑of‑order or duplicate → return `false` *without* advancing, so the sender rewinds to `written`. This is the whole flow‑control contract.
- Overrun guard: `written + len <= imageSize`.
- `esp_ota_write(handle, data, len)` (blocking flash write), then `written += len`, refresh `lastActivityMs`.

**`otaEnd`**
- Require the **full** image: `written == imageSize`.
- `esp_ota_end(handle)` — **validates image magic byte + SHA‑256.** Fail → image rejected, handle consumed, **current app still active**.
- `esp_ota_set_boot_partition(part)` — flips otadata to the new slot.
- Return `true` → caller reboots into the new image.

**`otaAbortSession` / teardown** — `esp_ota_abort(handle)` (safe on an in‑progress handle), clear state. **Never** switches the boot partition. Used on failure, explicit abort, and timeout.

### Brick‑safety invariants
1. All writes target the **inactive** slot — the running app is never overwritten.
2. The image is **verified (SHA) before** `set_boot_partition` — a corrupt/partial image never becomes bootable.
3. **Any** failure/timeout/interruption → `esp_ota_abort`, boot pointer unchanged → next boot = current app.
4. Power loss mid‑write corrupts only the inactive slot; the device still boots the current app.

### Timeout reaper
A periodic call (`checkOtaTimeout()`, from loop) aborts a session idle longer than `OTA_TIMEOUT_MS = 30000`. `lastActivityMs` is refreshed inside `otaBegin`/`otaWrite` (i.e. at *processing* time), so queued‑but‑not‑yet‑written packets still count as activity once drained (see §5).

---

## 3. Transport A — USB serial (`?OTALOCAL,*`)

A line‑oriented command driver over any `Serial` stream (UART bridge *or* native USB CDC — works the same). `processOtaLocalCommand(args)` where `args` is the text after `OTALOCAL,`:

| Command | Action |
|---|---|
| `?OTALOCAL,STATUS` | print partitions / running version / session |
| `?OTALOCAL,BEGIN,<imageSize>,<family>` | `otaBegin` (family 0=ESP32, 1=S3) |
| `?OTALOCAL,DATA,<offset>,<base64>` | base64‑decode → `otaWrite` (one in‑order chunk) |
| `?OTALOCAL,END` | `otaEnd` → on success, reboot |
| `?OTALOCAL,ABORT` | teardown, no boot switch |

- **Chunking:** base64 text per `DATA` line; decoded chunk ≤ `OTA_LOCAL_MAX_CHUNK = 1024` B into a static scratch buffer via `mbedtls_base64_decode` (no per‑call malloc). The host picks a chunk size ≤ 1024.
- **Machine‑readable markers** (the host parses these, not the human log):
  - `[OTA:BEGIN,OK|ERR,<cursor>]`
  - `[OTA:ACK,<cursor>]` — emitted after every accepted `DATA`
  - `[OTA:NAK,<cursor>]` — `DATA` rejected (gap/dup); host resends from `<cursor>`
  - `[OTA:END,OK|ERR]`
- **ACK‑paced flow control:** the host waits for `[OTA:ACK,<cursor>]` before sending the next `DATA`. USB serial has no application‑level backpressure, so without this the device's RX buffer overflows while a flash write is in progress. The ACK *is* the credit.
- **`END`:** prints `[OTA:END,OK]`, `delay(2000)`, `ESP.restart()`.

> **NaviCore note:** this path is the simplest port — it runs verbatim over NaviCore's USB CDC. A directly‑connected NaviCore needs nothing but this driver + the core + the partition table.

---

## 4. Transport B — ESP‑NOW relay (`?OTA,*`)

```
Browser ──USB──> Relay WCB ──ESP-NOW(BEGIN/DATA/END/ABORT)──> Target
Browser <──USB── Relay WCB <──ESP-NOW(ACK)──────────────────  Target
```

The **browser drives flow control end‑to‑end**; the relay is a stateless translator (USB line ⇄ ESP‑NOW unicast). The target runs the same core as the serial path.

### Wire format — two packed structs, sizes locked

Packet type constants (`packetType` byte): `OTA_BEGIN=20, OTA_DATA=21, OTA_ACK=22, OTA_END=23, OTA_ABORT=24`. Status: `OTA_ST_OK=0, OTA_ST_ERR=1`. Payload: `OTA_ESPNOW_PAYLOAD=192` firmware bytes per DATA packet.

```c
// 55 bytes — BEGIN / END / ABORT / ACK
typedef struct __attribute__((packed)) {
  char     structPassword[40];   // shared-secret gate
  uint8_t  packetType;
  uint8_t  targetWCB;            // addressee
  uint8_t  sourceWCB;            // sender (relay for cmds; target for ACK)
  uint8_t  chipFamily;           // BEGIN
  uint8_t  status;               // ACK/END result
  uint16_t sessionId;
  uint32_t imageSize;            // BEGIN
  uint32_t ackedOffset;          // ACK: highest contiguous byte written
} espnow_struct_ota_ctrl;

// 243 bytes — one firmware fragment
typedef struct __attribute__((packed)) {
  char     structPassword[40];
  uint8_t  packetType;           // OTA_DATA
  uint8_t  targetWCB;
  uint8_t  sourceWCB;            // relay (where the ACK goes back)
  uint16_t sessionId;
  uint16_t dataLen;              // valid bytes in data[]
  uint32_t fragOffset;           // ABSOLUTE offset into the image
  uint8_t  data[192];
} espnow_struct_ota_data;
```

### Size‑based dispatch (important architectural quirk)
The WCB's ESP‑NOW receive callback routes packets **by total `len`**, not by reading a type field at a fixed offset — different message families are different `sizeof`. So the OTA struct sizes (`55`, `243`) must stay **unique** against every other packet on the mesh (`{43,204,226,230,249,252}`). Two `static_assert`s lock them. If you change a struct, re‑check uniqueness. (NaviCore, on the same mesh, must dispatch the same way.)

### Target‑side handlers
Each: `memcpy` the struct, null‑terminate the password, gate, act, ACK.
- Gate `otaPktAuth(pw, targetWCB)` = `password == espnowPassword && targetWCB == myWCB`. (The mesh‑wide MAC‑group gate runs earlier in the callback.)
- `handleOtaBeginPacket` → `otaBegin` → ACK (OK/ERR, cursor).
- `handleOtaDataPacket` → `otaWrite(sessionId, fragOffset, data, dataLen)` (no‑op on gap/dup) → **ALWAYS** ACK the *current* cursor.
- `handleOtaEndPacket` → `otaEnd` → ACK → on success `delay(300)` (let the ACK transmit before the radio drops) → `ESP.restart()`.
- `handleOtaAbortPacket` → abort → ACK.

### Reliability model (no retransmit timers needed)
The target **always ACKs its current contiguous write cursor**, even on a duplicate or gap:
- **Lost DATA** → cursor doesn't advance → browser sees a stale cursor → resends from there.
- **Lost ACK** → re‑sent on the next (resent) DATA.
- esp_ota verifies the whole image before switching, so a silently‑wrong byte can't ship.

### Relay side
- `processOtaRelayCommand("?OTA,<SUB>,<target>,<session>,…")` builds the matching struct and `esp_now_send`s it to `WCBMacAddresses[target-1]`.
- `handleOtaAckRelay` receives the target's ACK and prints `[OTA:ACK,<src>,<session>,<offset>,<status>]` to USB for the browser.

---

## 5. THE critical rule: defer flash writes out of the receive callback

`esp_ota_begin` (erases ~1.2 MB), `esp_ota_write`, and `esp_ota_end` all **block**. The ESP‑NOW receive callback runs in the **WiFi task**. Running the flash calls there **stalls the WiFi task**, ESP‑NOW stops servicing, and the OTA session times out mid‑stream. (This was a real bug — the wireless stream stalled at offset 1728 under mesh load.)

**Pattern (mirrors how received commands are deferred to `loop()`):**
- The receive callback **enqueues** the raw OTA packet into a FreeRTOS queue and returns immediately.
- `drainOtaPackets()` runs in `loop()` and dispatches queued packets to the handlers — in safe loop context.
- Only the **target‑side** packets (BEGIN/DATA/END/ABORT) are deferred. The **relay‑side** ACK (`handleOtaAckRelay`) is lightweight (just a `Serial.printf`) and stays inline.

```c
typedef struct { uint8_t buf[244]; uint16_t len; } OtaPktSlot;   // 244 ≥ 243 (ota_data)
static QueueHandle_t otaPktQueue;          // xQueueCreate(12, sizeof(OtaPktSlot))

void enqueueOtaPacket(const uint8_t *raw, uint16_t len) {        // called from the WiFi callback
  if (len == 0 || len > 244) return;
  if (!otaPktQueue) otaPktQueue = xQueueCreate(12, sizeof(OtaPktSlot));
  OtaPktSlot s; memcpy(s.buf, raw, len); s.len = len;
  xQueueSend(otaPktQueue, &s, 0);          // drop if full → browser resends from cursor
}

void drainOtaPackets() {                    // called every loop(), before checkOtaTimeout()
  OtaPktSlot s;
  while (otaPktQueue && xQueueReceive(otaPktQueue, &s, 0) == pdTRUE) {
    if (s.len == sizeof(espnow_struct_ota_data))      handleOtaDataPacket(s.buf);
    else if (s.len == sizeof(espnow_struct_ota_ctrl)) { /* dispatch by packetType */ }
  }
}
```

Receive‑callback dispatch (in the size router):
```c
if (len == sizeof(espnow_struct_ota_ctrl)) {
  uint8_t pt = ((espnow_struct_ota_ctrl*)data)->packetType;
  if (pt == PACKET_TYPE_OTA_ACK) handleOtaAckRelay(data);   // relay side, inline
  else                           enqueueOtaPacket(data, len); // target side, deferred
  return;
}
if (len == sizeof(espnow_struct_ota_data)) { enqueueOtaPacket(data, len); return; }
```

> **NaviCore note:** WCB_Client owns the ESP‑NOW receive callback on NaviCore. Whatever hooks the OTA packet sizes there **must enqueue and defer** exactly like this — do not call `esp_ota_*` from the callback. ACK‑pacing alone is not enough; the WiFi task must stay free to keep ESP‑NOW flowing while the flash erase/write runs in loop context.

---

## 6. The boot selector (otadata) — making OTA coexist with esptool/IDE flashing

`set_boot_partition` flips otadata to the new slot. That means **after an OTA, otadata points at app1 (or whichever slot was inactive).** This interacts with conventional flashing:

- **Arduino IDE upload** writes `boot_app0.bin` to `0xE000` every time → otadata reset to **ota_0** → always boots the freshly‑uploaded app. ✅ no conflict.
- **GUI esptool flash that writes app‑only (to ota_0)** but *doesn't* touch `0xE000` → otadata still says "boot app1" → the board **keeps booting the old app** (or reboot‑loops if app1 is empty). ✗ — fix by erasing otadata (write `0xFF` to `0xE000`) on every esptool flash, decoupled from any NVS wipe. (This is what the WCB Wizard now does.)
- **OTA paths** set the boot slot honestly via `set_boot_partition`. ✅

**Rule for any device with multiple load paths: every path must set the boot selector to the slot it just wrote.** IDE does it; OTA does it; a custom esptool flasher must erase `0xE000`.

**Rollback:** the WCB build does **not** enable the IDF app‑rollback feature (Arduino default), so the new app does **not** need to call `esp_ota_mark_app_valid_cancel_rollback_reboot()`. **If NaviCore's sdkconfig enables `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`, the new image MUST call `esp_ota_mark_app_valid_cancel_rollback_reboot()` early in boot or the bootloader rolls it back on the next reset.** Decide this deliberately.

---

## 7. Host (browser) orchestration — reference

- **Image source:** fetch the app `.bin` (the ota_0 image, i.e. the file flashed at `0x10000`), pick `chipFamily` from the target's known hardware.
- **Serial path:** send `?OTALOCAL,BEGIN,<size>,<family>`, then `DATA` chunks (≤1024 B decoded) each gated on the `[OTA:ACK,<cursor>]` marker, then `END`.
- **Relay path:** send `?OTA,BEGIN,<t>,<s>,<size>,<family>`, then 192‑B `DATA` chunks, each awaiting the relay's `[OTA:ACK,<src>,<session>,<offset>,<status>]` line; advance the local offset to the ACKed cursor; **fast‑fail** if the cursor collapses to 0 after real progress (target lost the session). On any error, send `ABORT`.
- After `END`, the target reboots; re‑establish the management/terminal session after the reboot window (the WCB Wizard does a proactive re‑pull + RTERM re‑arm, plus a firmware boot‑announce so the relay re‑detects it).

---

## 8. Failure modes (all non‑bricking)

| Condition | Result |
|---|---|
| Chip‑family mismatch | `BEGIN` rejected (brick guard) — current app intact |
| No inactive OTA slot | `BEGIN` rejected — wrong partition table |
| Out‑of‑order / dup chunk | `otaWrite` no‑op; ACK stale cursor → sender resends |
| SHA/magic verify fail at `END` | image rejected; current app intact |
| 30 s idle | session aborted; current app intact |
| Power loss mid‑write | inactive slot corrupt; device still boots current app |
| Flash‑in‑callback (the mistake) | WiFi task stalls → mid‑stream timeout (why §5 exists) |

---

## 9. NaviCore porting checklist

1. **Partition table:** switch to a 2‑OTA‑slot scheme (min_spiffs or a custom table with `otadata` + `ota_0`/`ota_1`). Verify `esp_ota_get_next_update_partition()` returns non‑NULL.
2. **Core:** copy `otaBegin/otaWrite/otaEnd/otaAbortSession` (+ the session struct, `checkOtaTimeout`). It's pure IDF `esp_ota_ops` — portable as‑is. Set the chip‑family constant (S3 = 1).
3. **Transport:**
   - *Easiest:* the `?OTALOCAL,*` serial driver over NaviCore's USB CDC (directly‑connected updates). Reuse the markers + ACK‑pacing verbatim.
   - *Wireless:* implement the target‑side handlers + the size‑based dispatch in the (WCB_Client) receive callback, **with the enqueue/drain deferral (§5).**
4. **otadata coexistence (§6):** ensure every flash path NaviCore supports (IDE / GUI esptool / OTA) sets the boot slot to what it wrote.
5. **Rollback (§6):** if app‑rollback is enabled in sdkconfig, call `esp_ota_mark_app_valid_cancel_rollback_reboot()` on boot.
6. **Reboot timing:** `delay(2000)` (serial) / `delay(300)` after the ACK (wireless) before `ESP.restart()`, so the final marker/ACK actually leaves the device.
7. **Bootstrap floor:** a blank/bricked NaviCore still needs a conventional full flash first (OTA needs working firmware to run). OTA is for *updates*, not first‑flash.

---

*Companion: `WDP_DESIGN.md` (mesh discovery — how a board advertises capabilities) and the WCB Wizard (`Wizard/app.js` `boardOtaSerial`/`boardOtaRelay`, `Wizard/flasher.js`).*
