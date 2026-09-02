# WLED Integration — Possibilities & Proposal

**Status:** Draft — Jarrod's feedback received and incorporated (see "Locked decisions" below + §8); serial path confirmed, preset‑firing centric.
**Context:** Came out of the Discord thread — WLED is increasingly common in droids, and the question was raised whether a WCB could drive it. Two ideas surfaced: (1) the WCB "poses" as a WLED ESP‑NOW remote, and (2) WLED's serial JSON API, which "a WCB feature module could really compact for our purposes." This doc lays out the full option space, the tradeoffs, and a recommended shape.

---

## TL;DR

- **Recommended v1: drive WLED over a wired serial port using WLED's JSON API**, packaged as a WCB **feature module** — a direct sibling of the existing HCR and MP3 modules.
- It gives the **full** WLED control surface (color, brightness, effects, palettes, presets, segments…), needs **no custom WLED firmware**, and completely sidesteps the wireless‑channel headaches that the ESP‑NOW path runs into.
- The ESP‑NOW "pose as a remote" idea is real and documented, but it's **coarse** (a fixed ~8‑button vocabulary), **not actually zero‑touch** on the WLED side, and **fights the WCB's own mesh radio**. Captured below for completeness; not the v1 path.
- The big design win (Jarrod's instinct): a compact `;L,<verb>` command set that translates to WLED JSON, with a raw‑JSON escape hatch so we never have to chase WLED's entire schema.

---

## Locked decisions (Jarrod's feedback, 2026‑06)

- **Serial, full stop.** ESP‑NOW emulation and WiFi are off the table.
- **Preset‑firing is the primary workflow.** Author the looks in WLED's web UI; the WCB just *picks* between presets. v1 centers on `;L,ON/OFF` + `;L,PS,<n>` (+ `;L,BRI`). Direct color/effect/palette stay reachable via raw passthrough but aren't the focus — no "scene development" over JSON.
- **One WLED per WCB**, but **multiple per droid network** (R2 + BB8 driven together; a "droid head" idea, each with its own LED controller). Cross‑node addressing is by **targeting the WCB** (`;W3,;L,PS,2`); "controlled together" is a **multi‑target/broadcast** of one command — no per‑board multi‑drop needed.
- **Fire‑and‑forget.** No state readback / shadowing.
- **Boards:** ESP32‑S3, recent WLED, **LEDs on GPIO2/16 → UART0 free** (BB8 + R2) — so the serial‑pin gotcha is a non‑issue on his rigs.

---

## 1. The goal

Make a WLED node a **first‑class "device" the WCB drives** — fire lighting cues, set colors/brightness/effects, recall presets — from WCB sequences, the controller (NaviCore/Kyber), or the config tool, **without flashing custom firmware onto the WLED board.**

WLED is its own ESP32 firmware; we are not modifying it. We're choosing how a WCB *talks to* a stock WLED.

---

## 2. The three transport possibilities

| | **Serial JSON** ✅ rec. | **ESP‑NOW (WiZmote emul)** | **WiFi (HTTP / UDP)** |
|---|---|---|---|
| **Control ceiling** | Full `/json/state` — anything | Fixed: on/off/night, bri ±, presets 1‑7 | Full (HTTP) + live pixels (UDP) |
| **Raw color / effect / palette** | ✅ direct | ❌ only via pre‑saved presets | ✅ direct |
| **WLED‑side setup** | wire one UART, no toggle | enable ESP‑NOW + paste WCB MAC per node | join WiFi, IP/discovery |
| **Wireless?** | ❌ wired | ✅ | ✅ |
| **Multi‑node addressing** | ❌ 1 port ↔ 1 WLED | broadcast‑ish (MAC‑gated) | ✅ per‑IP |
| **Fits WCB architecture** | ✅ exactly (HCR/MP3 pattern) | ⚠️ fights the mesh radio | ⚠️ awkward (ESP‑NOW‑first) |
| **Custom WLED firmware** | none | none | none |

### 2a. Serial JSON  — *recommended*

WLED accepts its **full JSON state API over the UART**, not just over HTTP.

- **How it works:** the WCB writes a one‑line JSON object (newline‑terminated) to the WLED board's RX pin. The schema is identical to `http://wled/json/state` — i.e. *everything*. Sending `{"v":true}` makes WLED reply with its full state + info (so readback is possible).
- **No "enable serial JSON" toggle** — the listener is built into stock WLED. Default **115200 8N1**.
- **Full vocabulary:** power, master brightness, **arbitrary RGB/RGBW color**, **effect by index** (~100+), **palette by index**, per‑segment control, presets & playlists, nightlight, transitions, even per‑pixel. Orders of magnitude beyond the remote's buttons.
- **The one hardware gotcha:** serial shares **UART0 (GPIO1 TX / GPIO3 RX)** with WLED's LED‑output options. If the board's LED data pin is assigned to **GPIO1 or GPIO3**, serial breaks (TX or RX dies). So this wants a WLED board whose UART0 is free — common, since most builds put LEDs on GPIO2/16 — but it's the thing to check first.
- **Point‑to‑point:** one WCB serial port drives exactly one WLED; there's no address field in the frame.

**Pros:** full control; no wireless channel issues at all (it's a wire); maps 1:1 onto the proven HCR/MP3 feature‑module pattern; deterministic; optional bidirectional readback.
**Cons:** needs a physical UART run to each WLED (fine inside one body, awkward across a gap); consumes one of the WCB's scarce hardware UARTs (S1/S2); one WLED per port.

### 2b. ESP‑NOW — pose as a WiZmote  *(documented, not v1)*

WLED natively supports the **WiZ "WiZmote"** ESP‑NOW remote (since 0.14). A WCB can emulate that remote with stock WLED — it's a tiny 13‑byte packet (`program` byte, a 32‑bit incrementing sequence counter, and a `button` keycode). Community projects (DedeHai/WLED‑ESPNow‑Remote and others) already do exactly this.

But three things make it the wrong default for a droid:

1. **Coarse, fixed vocabulary.** Only: ON, OFF, NIGHT, brightness ±, and preset slots 1‑7. **No raw color, effect, or JSON.** To get a specific look you must **pre‑author it as a WLED preset on each node** and then fire its preset code. (Also: it's the *WiZmote* protocol — the physical remote is 4‑button → presets 1‑4; codes 5‑7 exist only in firmware.)
2. **Not zero‑touch on WLED.** Per node you must enable ESP‑NOW (off by default) **and paste the WCB's transmit MAC into WLED's "Hardware MAC" field** — WLED ignores packets from any other MAC. Plus pre‑staging presets for any real control.
3. **The channel problem (the real killer).** ESP‑NOW only delivers when sender and receiver are on the **same 2.4 GHz channel**. A WLED joined to house WiFi is pinned to the *router's* channel (which can auto‑change); the WCB mesh runs on its own channel. To bridge them you'd have to either pin *everything* to one channel (WLED in AP mode defaults to channel 1) or **channel‑hop 1‑13 on every send** — and hopping means the WCB leaves its mesh channel during each WLED send, causing droid‑command latency/jitter. That's a poor trade for a real‑time mesh.

*Verdict:* clever, genuinely wireless, but coarse + fiddly + disruptive to the mesh. Possible future "wireless coarse‑control for a WLED prop you can't wire," not the primary path.

### 2c. WiFi — HTTP / UDP  *(not recommended)*

WLED's richest native surface: HTTP `POST /json/state` (full control, per‑IP addressing) and UDP realtime (DDP / E1.31 / Art‑Net / WLED Sync) for live per‑pixel streaming. Great **if** the WLEDs already live on a house network for a static display.

For a droid it's architecturally awkward: the WCB is ESP‑NOW‑first (station mode, no AP join, no IP stack). A real WiFi association reintroduces the same channel‑lock problem *and* adds a TCP/IP stack, discovery, and a router dependency — heavy, with more failure modes, for what is really just "fire a lighting cue." Skip for now; revisit only if someone genuinely keeps WLEDs on a network.

---

## 3. Recommended design — a WCB "WLED" feature module (serial)

This drops into the **existing device‑feature‑module pattern** the WCB already uses for HCR and MP3 — no new architectural concepts.

**Config (claim a port):**
```
?WLED,PORT,S1:115200      # reserve S1 for WLED at 115200
?WLED,PORT,CLEAR          # release
?WLED,STATUS              # show config
```
- Use **S1 or S2** — the real hardware UARTs that do 115200 reliably (S3‑S5 are software serial, unreliable above 9600).
- Reuses the same **port‑conflict guard** we just added for HCR/MP3 — WLED can't claim a port already owned by HCR/MP3/Maestro/Kyber/PWM, and vice versa.

**Operate (compact verbs → one line of WLED JSON + newline):**

| WCB command | → sent to WLED |
|---|---|
| `;L,ON` / `;L,OFF` | `{"on":true}` / `{"on":false}` |
| `;L,TOGGLE` | `{"on":"t"}` |
| `;L,BRI,128` | `{"bri":128}` |
| `;L,COL,FF0000` | `{"seg":[{"col":[[255,0,0]]}]}` |
| `;L,FX,73` | `{"seg":[{"fx":73}]}` |
| `;L,FX,73,200,128` | `{"seg":[{"fx":73,"sx":200,"ix":128}]}` (speed/intensity) |
| `;L,PAL,11` | `{"seg":[{"pal":11}]}` |
| `;L,PS,3` | `{"ps":3}` (recall preset) |
| `;L,JSON,{…}` | raw passthrough, verbatim |

**The `;L,JSON,{…}` raw escape hatch is the key design move.** The compact verbs cover the 90% you'll actually fire from sequences (cues: power, brightness, color, effect, preset); raw passthrough covers *everything else WLED can do* without us ever modeling its full schema. Same philosophy as the existing `;H,RAW` for HCR.

**Targeting works exactly like every other WCB command** — `;W3,;L,PS,2` fires preset 2 on the WLED attached to WCB 3, over the existing mesh. The lighting board doesn't have to be the one you're typing on.

---

## 4. Driving multiple WLEDs

Per Jarrod's setup: **one WLED per WCB**, but **several across a droid network** (R2/BB8 together; a "droid head" with its own controller). Serial being point‑to‑point is fine — addressing happens at the **mesh** layer, not the serial layer:
- **One WLED per WCB port** — the normal case. No serial multi‑drop, no address field in the frame.
- **Reach any WLED by targeting its WCB:** `;W3,;L,PS,2` fires preset 2 on WCB 3's WLED over the existing mesh.
- **"Controlled together" (R2 + BB8):** multi‑target / broadcast the same command so every WCB's WLED fires the same preset in lockstep — reuses the existing fan‑out, no special support.
- *(Several WLEDs on one WCB, or per‑node addressing within a single board, isn't needed — deferred.)*

---

## 5. How this relates to WDP (the discovery protocol)

Minimal and indirect. WLED nodes aren't WCBs and don't run our mesh discovery, so they're never "discovered." The only tie‑in: a WCB that has a WLED configured can **advertise a "drives WLED on Sx" capability flag** in its normal discovery advert — so the mesh and the config tool know *which board owns lighting*, and a controller can route cues to the right place. It's metadata about the WCB, not a control channel to WLED.

---

## 6. Scope for v1 (deliberately small)

**In:** serial transport; the compact verb set — core `ON/OFF/PS/BRI`, plus `COL/FX/PAL` for convenience — and the `;L,JSON,` raw passthrough; one WLED per port; S1/S2; reuse of the existing config/NVS/conflict‑guard machinery.

**Out (defer):** ESP‑NOW emulation; WiFi/HTTP/UDP; per‑segment typed wrappers, playlists, nightlight modes, per‑pixel arrays (all reachable via `;L,JSON,` if needed); **state shadowing/readback (Jarrod: not needed)**; serial multi‑drop addressing. No custom WLED firmware, ever — if a feature needs it, it's out of scope.

---

## 7. Prototype‑first checklist (before committing)

1. **UART0 free?** On the actual WLED board/LED‑pin config, confirm GPIO1/GPIO3 aren't taken by LED output. Quick test: send `{"on":true,"bri":128}\n` at 115200 and read back `{"v":true}` state.
2. **Spare WCB UART?** Confirm S1 or S2 is actually free on a real droid wiring (alongside HCR/MP3/Maestro).
3. **Framing under load:** one JSON object per newline; verify back‑to‑back commands behave.
4. **Conflict guard:** prove `?WLED,PORT` can't steal a port HCR/MP3/etc. already own, and that reconfiguring releases the old port cleanly.

---

## 8. Answers from Jarrod (resolved)

1. **Boards:** ESP32‑S3, recent WLED builds. **UART0 is free** — LEDs on **GPIO2/16** on both BB8 and R2. → the serial‑pin gotcha doesn't apply to his rigs; GPIO2/16 is the known‑good LED config to document.
2. **Cue vocabulary:** mostly **on/off + preset firing**. WLED's web UI is great for authoring presets interactively; the WCB just picks between them. **No** scene development via JSON. → v1 centers on `ON/OFF/PS` (+ `BRI`); direct color/effect via raw passthrough only.
3. **Multiple WLEDs:** **one per droid/WCB** for now, but **more than one per WCB network** — R2/BB8 controlled together, or the "droid head" idea each with its own LED controller. → one WLED per WCB; address across the mesh by targeting the WCB; "together" via multi‑target. No on‑board multi‑drop needed.
4. **Readback:** fire‑and‑forget is fine. ("How would readback even work? …nah.") → **dropped** from v1.
5. **ESP‑NOW / wireless‑to‑unwired‑prop:** not needed — **"serial is the way to go."** → ESP‑NOW path stays shelved.

---

*Companion docs: `WDP_DESIGN.md` (the mesh discovery protocol this would advertise into). The WLED module would mirror the existing `WCB_HCR.*` / `WCB_MP3.*` modules.*
