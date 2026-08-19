# WCB Code Review — Wave 2 findings (firmware subsystems)

Companion to [CODE_REVIEW_2026-08.md](CODE_REVIEW_2026-08.md). Generated 2026-08-18 from the
wave-2 review run (13 subsystem scopes, all completed).

> **STATUS: ALL UNVERIFIED.** The adversarial verification pass was killed by a session limit
> before a single verifier ran. The workflow labelled these `CONFIRMED` only because its rule is
> *"confirmed if no verifier refuted it"* and **zero verifiers ran** — that label is an artefact,
> not a judgement. Treat every finding here as a **lead to confirm**, not a fact. Historically a
> large share of raw review findings die under verification.

**Totals:** 98 findings — S1: 3 · S2: 34 · S3: 39 · S4: 22

Numbering continues from the main log (F-001…F-036 live there).


---

## S1

### F-037 · S1 · `Code/WCB/command_timer.cpp:187`

**processCommandGroups() holds a reference into commandGroups across an explicit yield while serialCommandTask can clear() the vector — use-after-free**

*Category:* `race`

`processCommandGroups()` runs on the Arduino `loopTask` (called from `WCB.ino:7481`). It takes a raw reference into the vector and then yields while still holding it:

```cpp
187:  CommandGroup &group = commandGroups[currentGroupIndex];
...
203:  for (const String &cmd : group.commands) {
207:      parseCommandsAndEnqueue(cmd, 0);
208:      vTaskDelay(pdMS_TO_TICKS(1)); // ← Give queue time to breathe
209:  }
```

The writers are on a DIFFERENT FreeRTOS task. `serialCommandTask` is created at `WCB.ino:7439` (`xTaskCreatePinnedToCore(serialCommandTask, ..., 4096, NULL, 1, NULL, 1)` — Core 1, priority 1, i.e. equal priority to `loopTask`, preemptively scheduled). It calls `processIncomingSerial` (only call sites: `WCB.ino:6598,6603-6605,6613,6618,6621`) → `processSerialCommandHelper` (`WCB.ino:6411`) → **`parseCommandGroups(data)` at `WCB.ino:6490`** and **`stopTimerSequence()` at `WCB.ino:6431`**.

Both of those begin with `commandGroups.clear()` — `command_timer.cpp:85` and `command_timer.cpp:54` — which destroys every `CommandGroup` (and its `std::vector<String>`) and frees the backing store. When `loop()` resumes from the `vTaskDelay` on line 208, `group` and the range-for iterators over `group.commands` are dangling.

The bounds check is equally unprotected: `command_timer.cpp:183` tests `currentGroupIndex >= commandGroups.size()` and line 187 then indexes — a preemption in between with a `clear()` gives `commandGroups[0]` on an empty vector, followed by `group.commands[0]` at line 189 on a destroyed `String`.

The rationale comment at `WCB.ino:390-399` explains why the ESP-NOW path was moved behind `pendingTimerChainQueue`, then claims *"The local-serial entry point and SEQ playback both run inside loop() already, so they keep calling parseCommandGroups() directly."* That claim is false for the local-serial entry point — `processSerialCommandHelper` is reachable only from `serialCommandTask`, never from `loop()`. (SEQ playback is genuinely loop-only: `recallCommandSlot`→`parseCommandGroups` at `WCB_Storage.cpp:615` is reached from `handleSingleCommand` in the `loop()` queue drain at `WCB.ino:7512`.)

There is no mutex, critical section, or queue guarding `commandGroups`. Note this was catalogued as F-006 in `docs/CODE_REVIEW_2026-08.md:144` and is still unfixed on this branch.

**Failure scenario.** A `;t` timer chain is mid-flight (e.g. `;t500,A^;t500,B^;t500,C` running). While `processCommandGroups` is inside the for-loop at line 203 sitting in `vTaskDelay(1)`, any byte-complete line arrives on USB or S1-S5 — the Wizard pushing config, a controller sending a command, or the operator typing `?STOP`. `serialCommandTask` wakes (it polls every 5 ms), reaches `WCB.ino:6490` or `:6431`, and calls `commandGroups.clear()`. `loop()` resumes and dereferences `group.commands` / `cmd` on freed heap → heap corruption, garbage command bytes written to serial ports/ESP-NOW, or a LoadProhibited panic and watchdog reboot. The yield is taken once per command in the group, so a multi-command group opens the window repeatedly.

**Suggested fix.** Route the serial path through the deferral that already exists: at `WCB.ino:6490` call `enqueuePendingTimerChain(data)` instead of `parseCommandGroups(data)`, and set a flag at `WCB.ino:6431` that `loop()` acts on instead of calling `stopTimerSequence()` inline. Then correct the false claim in the comment at `WCB.ino:399`. As defence-in-depth in this file, copy the group out (`CommandGroup group = commandGroups[currentGroupIndex];`) rather than holding a reference across the yield, or guard all `commandGroups` access with a portMUX.

### F-038 · S1 · `Code/WCB/wcb_pin_map.cpp:14`

**hw_version 0 (out-of-box default) maps all ten serial pins to GPIO5, driving GPIO5 as an output on HW 3.1/3.2 where it is the S1_RX input header pin**

*Category:* `logic`

`wcb_pin_map.cpp:14-23` assigns **every** serial pin to GPIO5 for `wcb_hw_version == 0`:

```
SERIAL1_TX_PIN = 5;  SERIAL1_RX_PIN = 5;
SERIAL2_TX_PIN = 5;  SERIAL2_RX_PIN = 5;
SERIAL3_TX_PIN = 5;  SERIAL3_RX_PIN = 5;
SERIAL4_TX_PIN = 5;  SERIAL4_RX_PIN = 5;
SERIAL5_TX_PIN = 5;  SERIAL5_RX_PIN = 5;
```

This is the state of **every freshly flashed board**: `loadHWversion()` (`WCB_Storage.cpp:101-106`) does `wcb_hw_version = preferences.getInt("hw_version", 0)` then calls `updatePinMap()`, so an unconfigured board takes this branch.

setup() then uses those pins with **no hw_version gate at all** (`WCB.ino:7224-7280`): `Serial1.begin(baudRates[0], SERIAL_8N1, 5, 5)`, `Serial2.begin(..., 5, 5)`, and `Serial3/4/5.begin(..., SWSERIAL_8N1, 5, 5, false, 95)`. The only guards there are Kyber/PWM/baud, none of which apply on a default board (baudRates default to 9600, `WCB.ino:622-626`).

Traced consequences, all verified:

1. **Output-driver contention on a real header pin.** On HW 3.1/3.2, GPIO5 is `S1_RX` — verified in the hardware, not assumed: `PCB/Wireless Communication Board (WCB)V3.2/...kicad_sch` maps symbol pin `GPIO5` to pad `J1_5`, and `...kicad_pcb` pad `J1_5` is `(net 2 "S1_RX")`; V3.1 is identical. `uart_set_pin` for `Serial1`/`Serial2` connects a UART **TX out-signal** to GPIO5 and enables its output driver, and EspSoftwareSerial additionally drives it (see 2). So the ESP32-S3 push-pull-drives the pin that an attached Maestro/HCR/WLED node is push-pull-driving as its TX output — two drivers fighting on one net.
2. **Three SoftwareSerials silently share one ISR slot.** `SoftwareSerial.cpp:85` sets `m_oneWire = (m_rxPin == m_txPin);`, so Serial3/4/5 all enter one-wire half-duplex mode on GPIO5, and each `begin()` calls `attachInterruptArg(digitalPinToInterrupt(5), ...)` (`SoftwareSerial.cpp:189`). The Arduino-ESP32 3.3.4 core keeps exactly one handler per pin — `__pinInterruptHandlers[pin].fn/.arg` are overwritten at `cores/esp32/esp32-hal-gpio.c:228-229` — so Serial5's begin clobbers Serial4's, which clobbered Serial3's. Later, `applyLiveBaud()` calling `Serial3.end()` (`WCB.ino:1892`) detaches GPIO5's interrupt and kills Serial5's RX too.
3. **Self-echo command loop.** `broadcastCommand()` writes the command to S1-S5 (`WCB.ino:6343`) and TX is physically the same pin the RX side samples; `processIncomingSerial()` (`WCB.ino:6361-6400`) reads those ports and re-enqueues/re-broadcasts whatever appears there.
4. `attachPWMInterrupt()`/`detachPWMInterrupt()` (`WCB_PWM.cpp:144-165`) index the same `SERIALn_RX_PIN` globals, so all five PWM ports resolve to GPIO5 and clobber each other's ISR.

The adjacent comment does not document this as deliberate — it is factually wrong: `wcb_pin_map.cpp:12` claims "This pinout is for a Version 1.0 board (LilyGo T7 V1.5 ESP32)", but the actual v1.0 map is the `wcb_hw_version == 1` branch at lines 31-42 (5/32, 26/18, 27/25, 4/21, 22/23), which shares nothing with this one. So no deliberate-comment exemption applies.

**Failure scenario.** A user flashes a new WCB HW 3.2 board from the published release with a Pololu Maestro already wired to the S1 header and no `?HW,32` set yet. `loadHWversion()` returns 0, `updatePinMap()` takes the `== 0` branch, and setup() calls `Serial1.begin(9600, SERIAL_8N1, 5, 5)` → UART1's TX out-signal is routed to GPIO5, which is the S1_RX header pin the Maestro's TX line is connected to. Both the ESP32-S3 and the Maestro drive that net push-pull. Nothing works, the three SoftwareSerial ports share a single clobbered ISR, and the pin contention is a hardware-damage risk on a board the user has just wired up.

**Suggested fix.** Make the unset/unknown map inert rather than aliased: set every SERIALn_*_PIN to -1 in the `wcb_hw_version == 0` branch, and gate the Serial1/2 and Serial3/4/5 `begin()` calls in `WCB.ino:7224-7280` on a recognised hardware version (a helper such as `bool hwVersionKnown()`), printing the existing "SET YOUR HARDWARE VERSION" warning instead. Also fix the comment at wcb_pin_map.cpp:12 — it describes the v1.0 branch, not this one.

### F-039 · S1 · `Code/WCB/WCB_RemoteTerm.cpp:111`

**RTERM mirroring + espNowSendCallback form a self-sustaining infinite ESP-NOW/UART loop when the relay is unreachable**

*Category:* `concurrency`

`_sendPacket()` ends in an unconditional `esp_now_send(WCBMacAddresses[_relayWCB - 1], (uint8_t *)&pkt, sizeof(pkt));` (WCB_RemoteTerm.cpp:111). Every `esp_now_send` on this board completes into the globally-registered send callback (`esp_now_register_send_cb(espNowSendCallback)` — WCB.ino:7436). That callback is:

```
void espNowSendCallback(const wifi_tx_info_t *tx_info, esp_now_send_status_t status) {
    if (status != ESP_NOW_SEND_SUCCESS) {
        if (debugETM || debugEnabled) {
            Serial.printf("[SEND CB] MAC-layer FAILED to: %02X:%02X:%02X:%02X:%02X:%02X\n", ...);
```
(WCB.ino:4231-4238)

`Serial` is `#define`d to `WCBDebugSerial` (WCB_RemoteTerm.h:104). `Print::printf` -> `Print::vprintf` -> `write((uint8_t*)temp, len)` — an *unqualified virtual* call (core 3.3.4, Print.cpp:66) — dispatches into `WCBSerial::write(const uint8_t*, size_t)` (WCB_RemoteTerm.cpp:55). With a session armed, line 57-59 feeds every byte to `_bufChar()`; the trailing `\n` triggers `_flushLine()` (line 73) -> `_sendPacket()` -> another `esp_now_send` to the same unreachable relay -> another MAC-layer failure -> the send callback prints again. Strictly one new packet per failed packet, forever. Nothing in the module suppresses forwarding while inside an ESP-NOW callback, and `_flushLine`/`_sendPacket` have no re-entrancy guard or rate limit.

The loop body executes inside the WiFi task's send-callback context and does a blocking ~57-byte UART write each pass — exactly the hazard this file's own comment records at WCB_RemoteTerm.cpp:140-144 ("MUST NOT do blocking serial I/O here — doing so starves the WiFi stack and triggers the watchdog, crashing the board"). Even a single transient MAC failure on a marginal link is self-amplifying: one dropped frame permanently converts into a new frame that may also drop.

**Failure scenario.** Wizard manages WCB5 remotely through relay WCB2; it sends `?RTERM,START,2` (Wizard/app.js:6072), arming `_relayWCB=2` on WCB5. The user enables debug on WCB5 (`debugEnabled`/`debugETM` — the normal reason to open a relayed terminal; Wizard/app.js:7997 enables the debug buttons for remote boards). The relay board is then powered off / unplugged / reflashed, so the Wizard's best-effort `?RTERM,STOP` (Wizard/app.js:6087) never reaches WCB5. WCB5's next mirrored line fails at the MAC layer -> `[SEND CB] MAC-layer FAILED...` printed -> mirrored -> fails -> printed... WCB5 now spins forever in the WiFi send callback, spamming its USB console and the ESP-NOW channel, with the WiFi task blocked ~5 ms per pass on UART. Only a power cycle stops it.

**Suggested fix.** Add a re-entrancy guard in the forwarding path: a `static volatile bool s_inFlush` (or a per-instance `_forwarding` flag) set across `_flushLine()`/`_sendPacket()` so that any Serial output produced *by* the send path is written to USB only and never re-buffered. Additionally, do not initiate the ESP-NOW transmit synchronously from `write()` at all — push the completed line onto a small TX queue and drain it from `loop()`, mirroring what `rtermRelayDrain()` already does for the receive direction. Either fix alone breaks the cycle; both together also fix finding on esp_now_send from the recv callback.


---

## S2

### F-040 · S2 · `Code/WCB/command_timer.cpp:207`

**processCommandGroups hardcodes sourceID=0, so every timer-group command loses its origin serial port — source echo, blockBroadcastFrom bypass, and serial-monitor-mapping bypass**

*Category:* `logic`

Line 207 fires each group command as `parseCommandsAndEnqueue(cmd, 0);` — the source port is hardcoded to 0 (USB). It is not a lost value in this function: `parseCommandGroups(const String &input)` (line 74, and its declaration in `command_timer_queue.h:22`) takes no `sourceID` at all, and `CommandGroup` (`command_timer_queue.h:7-10`) has no field for it, so both callers discard the real source — `processSerialCommandHelper` at `WCB.ino:6490` (which HAS `sourceID` and passes it to `parseCommandsAndEnqueue` on every other path, `WCB.ino:6476`, `:6495`) and `recallCommandSlot` at `WCB_Storage.cpp:615` (which passes `sourceID` correctly on the non-timer branch at `:617`).

`sourceID` is load-bearing in `processBroadcastCommand` (`WCB.ino:6234`) in three places, all of which a hardcoded 0 defeats:

1. `WCB.ino:6243` — `if (sourceID >= 1 && sourceID <= 5)` looks up `serialMonitorMappings[].inputPort == sourceID`. With 0 no mapping is found, so the `return` at `WCB.ino:6284` ("Exit early - mapping replaces all broadcast behavior") never happens and the command goes out as a normal full broadcast instead of only to the mapped destinations.
2. `WCB.ino:6288` — `if (sourceID >= 1 && sourceID <= 5 && blockBroadcastFrom[sourceID - 1]) return;` never fires, so a port the operator explicitly blocked with `?SBI…` still broadcasts.
3. `WCB.ino:6296` — the port loop `for (int i = 1; i <= 5; i++)` skips `i == sourceID`. With `sourceID == 0` nothing is skipped, so the command is written straight back out the port it arrived on.

So the *same* command behaves differently depending only on whether the chain happened to contain a `;t`.

**Failure scenario.** A controller wired to WCB Serial1 sends `;t500,PANELALLOPEN`. Without the `;t` this goes through `parseCommandsAndEnqueue(data, 1)` and `processBroadcastCommand` skips Serial1 (`WCB.ino:6296`). With the `;t`, the group fires with `sourceID = 0` and `PANELALLOPEN` is written back out Serial1 into the controller that sent it — an echo, and a feedback loop for any attached device that re-parses what it reads. Likewise, a board with `?SBIS1OFF` (broadcast-in blocked on S1) or with a serial-monitor mapping on S1 has both settings silently ignored for any timer chain, sending broadcast text to Maestro/DFP/HCR-free ports the operator deliberately walled off.

**Suggested fix.** Add `int sourceID` to `parseCommandGroups()` and to `CommandGroup`, capture it at parse time, and pass `group.sourceID` at line 207. Update the two callers (`WCB.ino:6490`, `WCB_Storage.cpp:615`) to forward their existing `sourceID`, and the ESP-NOW deferral slot in `WCB.ino:405-411` to carry 0 as it does today.

### F-041 · S2 · `Code/WCB/WCB_DFP.cpp:58`

**The DFPlayer's configured/persisted volume is never sent to the module — ?DFP,...:V<n> is inert and does not survive a power cycle**

*Category:* `logic`

`dfpBindCodec()` is the only place the configured volume reaches the codec:

```
static void dfpBindCodec() {
  if (dfpConfig.configured && dfpConfig.serialPort > 0)
    dfpCodec.begin(getSerialStream(dfpConfig.serialPort), &Serial);
  dfpCodec.setVolume(dfpVolume);
}
```

`DfPlayerCodec::setVolume()` is shadow-only — "shadow only, emits nothing (config load)" (`WcbCmd/src/WcbDfPlayer.h:62`). Grepping the codec, the DFPlayer SET-VOLUME frame `frame(0x06, _volume)` is emitted at exactly three places — `WcbDfPlayer.cpp:109`, `:118`, `:124` — all inside the `VOL,` / `VOLUP` / `VOLDN` branches of `handle()`. There is no emit at bind, at boot, or at configure, and `dfpCodec` is `static` to `WCB_DFP.cpp` so nothing else can drive it.

So `configureDFP` (`:316` `dfpVolume = (uint8_t)volume;` → `:320` `dfpBindCodec()`) and `loadDFPSettings` (`:405` → `:424`) both set only the shadow. The module keeps whatever volume it powered up with.

This is the asymmetry with the MP3 module: `Mp3Codec::handle("PLAY,...")` emits `raw('v', _volume)` before every `raw('t', track)` (`WcbMp3.cpp:46-47`), so `?MP3,...:V<n>` genuinely takes effect. The DFPlayer codec deliberately does not re-send volume per play (`WcbDfPlayer.h:26-31` — "the DFPlayer keeps its volume across tracks"), which is correct *within* a power cycle but leaves the initial application to the host, and the host never does it. `WCB_Help.cpp:592` tells the user the opposite: "Current volume tracked in NVS and persists across reboots".

**Failure scenario.** `?DFP,S2:9600:V5` (quiet), reboot or power-cycle the droid, then `;D,PLAY,1`. `loadDFPSettings()` restores `dfpVolume = 5` and prints "[DFP] Loaded: S2 at 9600 baud default vol=5 current vol=5", `?DFP,LIST` reports "Current Volume: 5" — and the track plays at the module's own power-on default (30 = loudest on a stock DFPlayer Mini), full blast, until someone issues an explicit `;D,VOL,5`. Same on a fresh `?DFP,S2:9600:V5` with the module already running at a different level: the console says "Configured: ... default volume=5" and nothing changes on the wire.

**Suggested fix.** After binding, actually emit the volume — e.g. in `dfpBindCodec()` (and/or at the end of `configureDFP`) call `dfpCodec.handle((String("VOL,") + dfpVolume).c_str())` once the port is bound, guarded on `configured`. Note the ~1.5-3 s DFPlayer power-on window the module itself documents at `WCB_DFP.cpp:324`: a frame sent from `loadDFPSettings()` during `setup()` may be ignored, so the boot-time push should be deferred (a one-shot re-send from `processDFPResponses()` a few seconds after boot, or on the 0x3F "init complete" frame the codec already decodes at `WcbDfPlayer.cpp:200`). Either way, take the `onVolumeChanged` re-entry into account so the deferred push does not re-write NVS.

### F-042 · S2 · `Code/WCB/WCB_DFP.cpp:388`

**The dfp_cfg NVS namespace is not cleared by ?ERASE,NVS — a factory reset leaves the DFPlayer configured and its port seized**

*Category:* `nvs`

`saveDFPSettings()` opens a namespace that no other module knows about:

```
preferences.begin("dfp_cfg", false);
```

`eraseNVSFlash()` (`WCB_Storage.cpp:941`, reached from `?ERASE,NVS` at `WCB.ino:5071` and `WCB_ERASE` at `WCB.ino:5209`) is a hand-maintained list of `preferences.begin(ns)/clear()/end()` calls — there is no blanket `nvs_flash_erase()` anywhere in the sketch (grep for it returns nothing). That list clears `serial_baud, bdcst_set, mac_config, wcb_config, espnow_config, command_config, stored_cmds, kyber_settings, kyber_targets, hw_version, serial_labels, serial_map, serial_monitor, bcast_block, maestro_cfg, etm_config, mp3_cfg, led_config, wdp_cfg, learned_peers, hcr_cfg, wled_cfg, wcb_vars` (`WCB_Storage.cpp:942-1027`). **`dfp_cfg` is absent** — `mp3_cfg` is cleared at `:1006`, its DFP twin is not.

The comment immediately above the last three entries (`WCB_Storage.cpp:1022-1023`) records this exact bug being fixed once already: "Previously MISSED by factory reset — HCR, WLED, and user-variable configs survived an erase and re-seized their ports / reappeared after reboot." The DFPlayer module, added later in commit dac753e, repeats it.

**Failure scenario.** A user hands a board on with a DFPlayer on S2 to someone else, or is troubleshooting, and runs `?ERASE,NVS`. The board prints "NVS cleared. Restarting..." and reboots. `loadDFPSettings()` (`WCB.ino:7142`) reads `dfp_cfg` — still intact — and restores `configured=true, serialPort=2`, printing "[DFP] Loaded: S2 at 9600 baud". S2 is immediately re-seized: `processIncomingSerial` drops all RX on it (`WCB.ino:6370`) and the broadcast fan-out skips it (`WCB.ino:6328`), so a device plugged into S2 after the "factory reset" is deaf and mute with no visible cause. The board also keeps advertising `WDP_CAP_DFPLAYER` (`WCB_WDP.cpp:109`), so every peer re-learns it as the `;D` host (`WCB_WDP.cpp:735`) and persists that route. A restored `dfp_cfg.remoteWCB` survives the same way on a client board.

**Suggested fix.** Add `preferences.begin("dfp_cfg", false); preferences.clear(); preferences.end();` to `eraseNVSFlash()` next to the `mp3_cfg` block (WCB_Storage.cpp:1006) and extend the existing "Previously MISSED" comment. The durable fix is to stop hand-maintaining the list — enumerate the namespaces from one shared table that each module registers into, so the next `?XYZ` subsystem cannot repeat this for a third time.

### F-043 · S2 · `Code/WCB/WCB_HCR.cpp:311`

**;H,SETEMOTION accepts value 100 but the HCR library silently drops it — the command does nothing and reports success**

*Category:* `bounds`

```
  if (vU == "SETEMOTION") {
    int e = hcrEmotion(hcrField(body, 1));
    int v = hcrField(body, 2).toInt();
    if (e < 0 || e > SCARED || v < 0 || v > 100) {
      Serial.println("[HCR] Usage: ;H,SETEMOTION,<H|S|M|C>,<0-100>"); return;
    }
    _hcr->SetEmotion(e, v); _hcr->update();
```

The accepted upper bound is 100, but the device/library range is 0–99: `HCRVocalizer::SetEmotion` at `Code/WCB/src/HumanCyborgRelationsAPI/hcr.cpp:430-436` is

```
    if (e < 0 || e > 3) return;
    if (v < 0 || v > 99) return;
```

so `v == 100` returns before `sendCommand()` — no bytes leave the board, and WCB_HCR.cpp prints nothing because it already passed its own check. The user sees silence and assumes it worked.

The rest of the module already uses 99 as the ceiling: `hcrSetVol()` (`WCB_HCR.cpp:86`) does `constrain(v, 0, 99)`, the header comment at `WCB_HCR.cpp:76-78` states "range 0-99 — the device/library range: HCRVocalizer SetEmotion/SetVolume cap at 99", and the shared codec rejects >99 for fn 2 (`WcbCmd/src/WcbHcr.cpp:10`). The numeric equivalent even reports the error correctly: `;H,FN,2,0,100` goes through `hcrCodec.emit()` (WCB_HCR.cpp:279), normalize() rejects it, and line 282 prints "FN 2,0,100 rejected". Only the readable verb fails silently. The usage string on line 312 also advertises the wrong range.

**Failure scenario.** `;H,SETEMOTION,M,100` (a natural way to ask for maximum anger, and exactly what the printed usage text tells the user to type). WCB_HCR.cpp validates it, calls `_hcr->SetEmotion(2, 100)`, the library's `v > 99` check returns immediately, zero bytes are written to the HCR, and no error is printed on either side. The emotion never changes. `;H,SETEMOTION,M,99` works, so the failure looks random.

**Suggested fix.** Change the check to `v < 0 || v > 99` and the usage string to `<0-99>` on line 312. (Same one-off exists for `;H,VOL` at line 375: `v > 100` is accepted and then silently clamped to 99 by `hcrSetVol`; less harmful but worth aligning.)

### F-044 · S2 · `Code/WCB/WCB_HCR.cpp:649`

**?HCR,PORT port-conflict guard omits the DFPlayer, so HCR can be bound to a UART a DFPlayer already owns**

*Category:* `protocol`

The guard reads:

```
  if (isSerialPortPWMOutput(serialPort) || isSerialPortUsedForPWMInput(serialPort) ||
      isSerialPortUsedForMP3(serialPort) || isSerialPortUsedForWLED(serialPort) ||
      (serialPort == 1 && (Kyber_Local || Maestro_Remote)) ||
      (serialPort == 2 && Kyber_Local)) {
    Serial.printf("[HCR] S%d already in use by PWM/Kyber/MP3/WLED - config blocked\n", serialPort);
```

It never calls `isSerialPortUsedForDFP()` (declared at `Code/WCB/WCB_DFP.h:61`, defined at `Code/WCB/WCB_DFP.cpp:63`). The DFPlayer module *does* check HCR — `Code/WCB/WCB_DFP.cpp:268` includes `isSerialPortUsedForHCR(serialPort)` — so the guard is asymmetric and the DFP-then-HCR order slips through. DFP accepts S1–S5 at 9600 (`Code/WCB/WCB_DFP.cpp:243-252`), exactly overlapping HCR's range (`WCB_HCR.cpp:628`), so the overlap is fully reachable.

After `hcrReservePort()` runs, both drivers own the port: `dfpCodec.begin(getSerialStream(dfpConfig.serialPort), &Serial)` (`WCB_DFP.cpp:57`) and `_hcrPort = &SerialN` / `new WcbHCR(...)` (`WCB_HCR.cpp:132-136`). Both then read it every loop pass — `processDFPResponses()` → `dfpCodec.poll()` (`WCB_DFP.cpp:101`) and `processHCRTick()` → `_hcr->update()` → `receive()` (`WCB_HCR.cpp:155`, `src/HumanCyborgRelationsAPI/hcr.cpp:191-208`) — and both write it. The two per-port skip guards in WCB.ino (`:6328` DFP, `:6336` HCR, and `:6370`/`:6374`) only keep the *broadcast/serial task* off the port; they do nothing about two device drivers on the same UART.

The adjacent comment at `WCB_HCR.cpp:645-648` explicitly claims this guard exists "so two subsystems can't silently share one UART" — the DFPlayer, added later (commit dac753e), was never added to it.

**Failure scenario.** On one board: `?DFP,S3` (DFPlayer on S3 at 9600), then `?HCR,PORT,S3:9600`. The HCR config is accepted with no warning. From then on every loop pass `dfpCodec.poll()` and `_hcr->update()` race for the same RX bytes — each steals half of the other's frames, so DFPlayer ONFIN/error decoding (0x3D/0x40 10-byte frames) breaks and HCR `<QD>` status parsing returns garbage into emote_*/state_*. On TX, an HCR `<PVA50>\n` interleaves with a DFPlayer 10-byte command frame, so both devices receive corrupt commands.

**Suggested fix.** Add `extern bool isSerialPortUsedForDFP(int port);` next to the existing `isSerialPortUsedForWLED` extern (WCB_HCR.cpp:25) and include `isSerialPortUsedForDFP(serialPort)` in the guard at line 649, updating the message to "PWM/Kyber/MP3/WLED/DFP". (The same omission exists in WCB_MP3.cpp:314 and WCB_WLED.cpp:326 — out of this scope but the same fix.)

### F-045 · S2 · `Code/WCB/WCB_Maestro.cpp:215`

**Mesh-received ;M<n> falls through to the legacy S1 write when id n is configured as REMOTE and n == this board's WCB number**

*Category:* `logic`

`sendMaestroCommand()` sets `handled` only inside the two per-slot branches. The remote branch is gated on `!lastReceivedViaESPNOW` (WCB_Maestro.cpp:139: `if (config.remoteWCB > 0 && !lastReceivedViaESPNOW) {`), and `handled = true;` sits INSIDE that `if` (line 152). So for a command that arrived over the mesh and whose only matching slot is a remote proxy, the loop leaves `handled == false`, `if (handled) return;` (line 158) does not fire, and control reaches:

```
  // Legacy: targeting this board's own WCB number with nothing configured for
  // that ID — keep the old hardcoded S1 write for backward compatibility.
  if (maestroID == WCB_Number) {
    uint8_t command[4]; WcbMaestro::buildSubroutineFrame(maestroID, scriptNumber, command);
    Serial1.write(command, sizeof(command));
```

The comment asserts "nothing configured for that ID", but the guard is only `handled == false` — there is no `isMaestroConfigured(maestroID)` test, so the branch also fires when a REMOTE slot for that id exists. The comment is factually wrong about the code on this path.

The slot that triggers this is created automatically: `maestroAutoAddRemote()` (WCB_Maestro.cpp:723) rejects `hostWCB == WCB_Number` but never checks `maestroID == WCB_Number`, so a WDP advert from another board hosting Maestro id N installs `{id N, serialPort 0, remoteWCB host}` on board N itself (WCB_WDP.cpp:710 calls it for every advertised Maestro when auto-join is on).

`lastReceivedViaESPNOW` is genuinely true here: it is restored per queue item in the loop() drain (WCB.ino:7510) from the origin snapshot taken at enqueue.

The identical defect exists in the verb path at WCB_Maestro.cpp:359-360 (`if (dev == WCB_Number) { Serial1.write(frame, n); }`). Contrast WCB_Maestro.cpp:423, where the get-query path gets the guard right: `if (localPort == 0 && hostWCB == 0 && dev == WCB_Number) localPort = 1;` — it requires that nothing at all is configured.

**Failure scenario.** Droid with WCB2 and WCB3; Maestro Pololu id 2 is physically wired to WCB3 (two Maestros on that board). WDP auto-join is on, so WCB2 hears WCB3's advert and auto-adds slot {id 2, port 0, remote 3}. A controller broadcasts `;M25`. WCB2 receives it over ESP-NOW: the remote branch is skipped (would bounce), `handled` stays false, and WCB2 writes AA 02 27 05 out its own Serial1 — a port that on that board is the HCR / MP3 Trigger / WLED link. WCB3 also runs the command correctly, so the operator sees the servo move and never suspects the stray 4 bytes going to the wrong device on WCB2.

**Suggested fix.** Gate both legacy-S1 fallbacks on the id genuinely having no configuration, not just on `handled`: `if (maestroID == WCB_Number && !isMaestroConfigured(maestroID))` at WCB_Maestro.cpp:215 and the same at :359. (Alternatively set `handled = true` whenever a matching slot exists, before the `!lastReceivedViaESPNOW` gate, so a suppressed forward is still "handled".)

### F-046 · S2 · `Code/WCB/WCB_Maestro.cpp:658`

**configureMaestro() is the only device-config path with no port-ownership guard — it seizes and re-bauds a UART already owned by HCR/MP3/DFPlayer/WLED/PWM/Kyber**

*Category:* `logic`

Every other serial-device config path in this firmware refuses a port another subsystem already owns:
- HCR: WCB_HCR.cpp:649 `if (isSerialPortPWMOutput(serialPort) || isSerialPortUsedForPWMInput(...) || isSerialPortUsedForMP3(...) || isSerialPortUsedForWLED(...) ...) { "config blocked"; return; }`
- MP3: WCB_MP3.cpp:314, DFPlayer: WCB_DFP.cpp:267, WLED: WCB_WLED.cpp:326, PWM: WCB_PWM.cpp:71
- Kyber: WCB_Storage.cpp:1144-1150, whose comment states the rule outright: "Don't seize a UART another subsystem already owns. Symmetric with the HCR/MP3/WLED/PWM guards so two subsystems can't silently share one port."

`configureMaestro()`'s LOCAL branch has no such check (`grep -c 'isSerialPortUsed\|isSerialPortPWM' WCB_Maestro.cpp` = 0). It validates only the port number and the software-serial baud ceiling, then unconditionally mutates the port:

```
      // Update baud rate if needed
      if (baudRate != baudRates[serialPort - 1]) {
        updateBaudRate(serialPort, baudRate);   // line 660 — applies LIVE + persists to NVS
      }
      ... serialBroadcastEnabled[serialPort - 1] = false;  (line 665)
      ... blockBroadcastFrom[serialPort - 1]   = true;      (line 672)
```

`updateBaudRate()` (WCB_Storage.cpp:146) calls `applyLiveBaud()`, which for S1/S2 reprograms the divisor and for S3-S5 does `SerialN.end(); SerialN.begin(newBaud, ...)` (WCB.ino:1891-1905) — a live teardown of the other subsystem's port — and writes the new value to the `serial_baud` NVS namespace so it survives reboot.

The reverse direction is just as unguarded: `_clearMaestroSlot()` (WCB_Maestro.cpp:794) and `clearAllMaestroConfigs()` (line 915) force the freed port back to `updateBaudRate(port, 9600)` and re-enable broadcast, again without asking whether another subsystem now owns it.

**Failure scenario.** HCR is configured on S2 at 9600 (`?HCR,S2:9600`). The operator then adds a Maestro and mistypes/reuses the port: `?MAESTRO,M3:W1S2:57600`. The command is accepted ("✓ Maestro 3: Local S2 at 57600 baud"), S2 is switched live to 57600 and persisted, and the HCR goes mute — with no error printed anywhere and no indication in `?HCR,LIST`, which still reports S2:9600. The same command issued in the opposite order is refused by WCB_HCR.cpp:649, so the failure depends purely on config ordering.

**Suggested fix.** Add the same guard the other five subsystems use, immediately before the software-serial warning at WCB_Maestro.cpp:642, for the LOCAL branch only: reject when `isSerialPortPWMOutput(serialPort) || isSerialPortUsedForPWMInput(serialPort) || isSerialPortUsedForHCR(serialPort) || isSerialPortUsedForMP3(serialPort) || isSerialPortUsedForWLED(serialPort) || (Kyber_Local && serialPort == kyberLocalPort)`, printing the same style of "S%d already in use by …  - config blocked" message. Mirror it in `_clearMaestroSlot()` so the 9600 reset is skipped when another subsystem has since claimed the freed port.

### F-047 · S2 · `Code/WCB/WCB_MP3.cpp:314`

**?MP3 port-conflict guard omits the DFPlayer — MP3 can be configured onto the DFPlayer's UART, wedging the DFP RX state machine**

*Category:* `logic`

The guard in `configureMP3` reads:

```
if (isSerialPortPWMOutput(serialPort) || isSerialPortUsedForPWMInput(serialPort) ||
    isSerialPortUsedForHCR(serialPort) || isSerialPortUsedForWLED(serialPort) ||
    (serialPort == 1 && (Kyber_Local || Maestro_Remote)) ||
    (serialPort == 2 && Kyber_Local)) {
```

`isSerialPortUsedForDFP()` is missing. The reverse guard IS present — `WCB_DFP.cpp:269` calls `isSerialPortUsedForMP3(serialPort)` — so the hole is one-directional and depends purely on which device the user configures second. `git show --stat dac753e` ("DFPlayer Mini support") confirms `WCB_DFP.cpp/.h`, `WCB.ino`, `WCB_Help.cpp`, `WCB_WDP.cpp` were touched but `WCB_MP3.cpp` never was.

The guard's own comment at `WCB_MP3.cpp:310-313` claims it exists "so two subsystems can't silently share one UART" — that claim is false as written. (`isSerialPortUsedForDFP` is referenced nowhere except `WCB.ino:6328` and `:6370` and its own file; the HCR/WLED/PWM/Kyber guards at `WCB_HCR.cpp:650`, `WCB_WLED.cpp:327`, `WCB_PWM.cpp:71`, `WCB_Storage.cpp:1151` are missing it too, and none of them — nor `configureMaestro` — check a locally-configured Maestro port either.)

Nothing else catches the collision. After `?MP3,S2:...` on a DFP-owned S2, both `mp3Codec` and `dfpCodec` are `begin()`-bound to `Serial2` (`WCB_MP3.cpp:60`, `WCB_DFP.cpp:57`), and `loop()` polls MP3 first (`WCB.ino:7496`) then DFP (`WCB.ino:7497`). `Mp3Codec::poll()` drains with `while (_out->available())`, so the DFPlayer's reply frames are consumed by the wrong codec before `processDFPResponses()` ever runs.

**Failure scenario.** `?DFP,S2` then `?MP3,S2:9600:V25` — accepted with no warning; the S2 label is silently overwritten from "DFPlayer" to "MP3 Trigger" (`WCB_MP3.cpp:365`). Now play a track: `;D,PLAY,1`. When it ends the DFPlayer emits `7E FF 06 3D 00 00 02 FE BC EF`. `Mp3Codec::poll()` reads it first; byte 0x3D is ASCII '=', which is the MP3 Trigger's "status string follows" marker, so the MP3 codec sets `_inStatus = true` and then swallows every subsequent byte waiting for a CR/LF that a DFPlayer never sends — it latches in status mode and eats all further RX forever. `;D,...,ONFIN,<key>` callbacks and DFPlayer error callbacks stop firing permanently, and `processDFPResponses()` sees an empty stream. A checksum byte that happens to be 0x45 ('E') also fires the MP3 codec's `onError`, running the unrelated `?MP3,ONERR` stored command. On the TX side `;A,PLAY,5` writes 'v',vol,'t',5 into the DFPlayer's UART. Finally `?MP3,CLEAR` re-enables broadcast output AND input on S2 and resets its baud (`WCB_MP3.cpp:166-177`) while the DFPlayer still owns the port, flooding it with broadcast text.

**Suggested fix.** Add `isSerialPortUsedForDFP(serialPort) ||` to the guard at WCB_MP3.cpp:314 and declare `extern bool isSerialPortUsedForDFP(int port);` alongside the HCR/WLED externs at WCB_MP3.cpp:22-23; update the message at :318 to name DFP. While there, the same predicate belongs in the HCR/WLED/PWM/Kyber guards, and the guard comment at :310-313 should stop claiming completeness it does not have (no guard anywhere checks a locally-configured Maestro port).

### F-048 · S2 · `Code/WCB/wcb_pin_map.cpp:114`

**updatePinMap() has no final else — an unrecognised stored hw_version leaves every pin global at 0, binding all five ports to GPIO0**

*Category:* `error-handling`

The if/else-if chain in `updatePinMap()` covers 0, 1, 21, 23, 24, 31 and 32 and then simply ends at `wcb_pin_map.cpp:114` with no `else`. For any other value of `wcb_hw_version` the function **writes nothing at all**, so all twelve globals keep their values — and since they are file-scope `int`s with no initialiser in `WCB.ino:605-616`, that value is `0`.

Setup then does `Serial1.begin(baudRates[0], SERIAL_8N1, 0, 0)` and `Serial2.begin(..., 0, 0)` (`WCB.ino:7225-7226`) plus `Serial3/4/5.begin(..., SWSERIAL_8N1, 0, 0, false, 95)` (`WCB.ino:7253/7263/7273`) — five UARTs on GPIO0, the boot strapping pin, with the same one-wire and shared-ISR consequences described in the GPIO5 finding (`SoftwareSerial.cpp:85`, `esp32-hal-gpio.c:228-229`). `attachPWMInterrupt()` likewise does `attachInterrupt(digitalPinToInterrupt(0), ...)` (`WCB_PWM.cpp:145-154`).

This state is reachable on an upgrade, not just in theory. The comment on the current `saveHWversion()` (`WCB_Storage.cpp:69-73`) records that the validation is new — "The old order wrote the value first, so an invalid ?HW,0 … clobbered a previously-saved hardware version in NVS and the board came up with no pin map after the next reboot." Any board that ran that older firmware and received a typo'd `?HW,3`, `?HW,2` or `?HW,25` still has that integer in the `hw_version` NVS namespace; the new validation only guards future writes, and `loadHWversion()` (`WCB_Storage.cpp:101-106`) reads the stored value back with no range check.

GPIO0 is not a harmless park pin on either target: on V2.4 it is a live net (`kicad_pcb` U1 pad 23 → `(net 16 "GPIO0")`, the auto-program/boot circuit), and on V3.1/V3.2 it is the module's BOOT input. A UART TX or one-wire SoftwareSerial driving it low across a reset puts the chip into ROM download mode.

Note also the asymmetry: `WCB.ino:7167-7181` explicitly special-cases `wcb_hw_version == 0` for the LED, but nothing anywhere handles "stored value we do not recognise" for the pin map.

**Failure scenario.** A board that was configured on an older firmware with a mistyped `?HW,3` (stored unconditionally by the pre-validation `saveHWversion`) is updated to this release. `loadHWversion()` reads 3, `updatePinMap()` matches no branch and returns without assigning anything, so all ten SERIALn pins stay 0. setup() brings up two hardware UARTs and three one-wire SoftwareSerials on GPIO0. All five serial ports are dead, the PWM ISRs attach to GPIO0, and the only clue is the generic "SET YOUR HARDWARE VERSION BEFORE PROCEEDING!!" line from `printHWversion()`.

**Suggested fix.** Add a terminating `else` that assigns the same inert map as the unset case (all serial pins -1) and prints an explicit `Serial.printf("Unrecognised hw_version %d — no pin map loaded\n", wcb_hw_version);`, so an unknown stored value is reported and can never silently become GPIO0.

### F-049 · S2 · `Code/WCB/WCB_PWM.cpp:71`

**canUsePWMOnPort() omits local Maestro and DFPlayer, so WDP can live-steal a Maestro's UART and kill it permanently**

*Category:* `protocol`

The port-reservation guard is:

```c
// Reject ports already claimed by a serial-device module (HCR/MP3/WLED).
// Symmetric with those modules' own guards, which reject PWM ports — so two
// subsystems can never silently drive the same UART.
if (isSerialPortUsedForHCR(port) || isSerialPortUsedForMP3(port) || isSerialPortUsedForWLED(port)) {
```

Two claimants are missing:

1. **Local Maestro.** There is no `isSerialPortUsedForMaestro()` anywhere in the tree (`grep -rn isSerialPortUsedForMaestro Code/WCB` → no hits), and `configureMaestro()` (`WCB_Maestro.cpp:595-680`) has no PWM guard either — unlike HCR (`WCB_HCR.cpp:649`), MP3 (`WCB_MP3.cpp:314`), WLED (`WCB_WLED.cpp:326`) and DFP (`WCB_DFP.cpp:267`), which all reject PWM ports. So the Maestro↔PWM pair is unguarded in **both** directions.
2. **DFPlayer.** `isSerialPortUsedForDFP()` exists (`WCB_DFP.cpp:63`, declared `WCB_DFP.h:61`) and DFP rejects PWM ports at `WCB_DFP.cpp:267`, but this guard never calls it — the guard is one-way.

The adjacent comment (`:68-70`) asserts the relationship is symmetric "so two subsystems can never silently drive the same UART". That is factually false for DFPlayer (guarded one way) and for Maestro (guarded neither way).

This matters most on the WDP auto-config path, which is *unattended*: `WCB_WDP.cpp:745-751` does `if (isSerialPortPWMOutput(prt)) continue; if (!canUsePWMOnPort(prt)) continue; addPWMOutputPort(prt, senderWCB);`. `canUsePWMOnPort` is the only gate, so a peer's PWMTARGET TLV can claim a port this board is actively using for a Maestro or DFPlayer.

The damage is immediate and persistent: `addPWMOutputPort` → `configureRemotePWMOutput` (`:757-759`) does `pinMode(txPin, OUTPUT); digitalWrite(txPin, LOW)` — a UART TX line held LOW is a continuous BREAK on the device's RX. It then persists to NVS (`:818`). After the next reboot, `WCB.ino:7251/7261/7271` skip `SerialN.begin()` entirely for any port where `isSerialPortPWMOutput()` is true, so the Maestro/DFPlayer port is never even initialised — `WCB_Maestro.cpp:311` then writes frames into an un-begun stream. Nothing in the PWM module ever auto-clears a port for this reason.

**Failure scenario.** Board 2 has a Maestro configured on S3 (`?MAESTRO,M1:W2S3:9600`). Board 1 has a PWM mapping `?MAP,PWM,S1,W2S3` (a legal command — nothing on either board cross-checks). Board 1 advertises WDP TLV 0x10 PWMTARGET (2,3). Board 2's `wdpOnAdvertReceived` → `canUsePWMOnPort(3)` returns true (S3 is neither 1 nor 2 for the Kyber check, and HCR/MP3/WLED are all unconfigured) → `addPWMOutputPort(3, 1)` → SERIAL3_TX_PIN driven LOW and written to NVS. The Maestro stops responding instantly (permanent break on its RX), and after the next reboot Serial3 is never begun at all. No user action was taken on board 2 and no error is printed there beyond one informational `[WDP] WCB1 drives our S3` line. The same thing happens on the manual path with `?MAP,PWM,OUT,S3`, and identically for a DFPlayer port.

**Suggested fix.** Add the two missing claimants to the guard: `if (isSerialPortUsedForHCR(port) || isSerialPortUsedForMP3(port) || isSerialPortUsedForWLED(port) || isSerialPortUsedForDFP(port) || isSerialPortUsedForMaestro(port))` — declaring `extern bool isSerialPortUsedForDFP(int)` alongside the existing externs at `:19-21`, and adding an `isSerialPortUsedForMaestro(int port)` helper in `WCB_Maestro.cpp` that scans `maestroConfigs[]` for `configured && remoteWCB == 0 && serialPort == port`. Add the reciprocal PWM guard to `configureMaestro()` (`WCB_Maestro.cpp` local branch, ~:637) so the conflict is rejected from both directions, and correct the `:68-70` comment to name the modules actually checked.

### F-050 · S2 · `Code/WCB/WCB_PWM.cpp:254`

**A mapping output written as W<this board>S<port> bypasses the local port-conflict guard but is still driven as a local pin**

*Category:* `logic`

The config-time guard only fires for the `S<port>` form:

```c
if (serialPort >= 1 && serialPort <= 5 && wcbNum >= 0 && wcbNum <= MAX_WCB_COUNT) {
    // Validate local output ports aren't in use by Kyber
    if (wcbNum == 0 && !canUsePWMOnPort(serialPort)) {      // :254 — only wcbNum == 0
```

But the runtime path treats a self-addressed output as local:

```c
if (targetWCB == 0 || targetWCB == WCB_Number) {           // :705
    ... digitalWrite(txPin, HIGH); delayMicroseconds(pulseWidth); digitalWrite(txPin, LOW);
```

and `isSerialPortPWMOutput()` agrees, matching `wcbNumber == 0 || wcbNumber == WCB_Number` (`:779-780`). So the code knows `W<self>S<n>` means "local" in two places and forgets it in the one place that validates. `loadPWMMappingsFromPreferences()` repeats the identical mistake at `:582`, so the bad mapping also survives every reboot.

The result is that all the reservation checks in `canUsePWMOnPort()` — Kyber on S1/S2, Maestro_Remote on S1, HCR/MP3/WLED — are trivially side-stepped by spelling the destination with the board's own number. `processPWMPassthrough` will then bit-bang the reserved port's TX pin at up to 200 Hz.

**Failure scenario.** WCB3 is the Kyber host (`Kyber_Local` true, so S1 and S2 are reserved). The user configures `?MAP,PWM,S4,W3S2` on WCB3 — using the board picker and selecting its own board rather than typing `S2`. `:254` skips `canUsePWMOnPort(2)` because `wcbNum == 3 != 0`, and `:252` accepts it since `3 <= MAX_WCB_COUNT`. The mapping is saved and reloaded on every boot. `processPWMPassthrough` at `:705` sees `targetWCB == WCB_Number` and drives SERIAL2_TX_PIN HIGH/LOW for 1-2 ms on every input pulse — on top of the live Kyber/Maestro serial stream on the same pin. Kyber sabre data is corrupted with no diagnostic at all; the direct form `?MAP,PWM,S4,S2` would have been rejected with "Cannot use PWM on Serial2 - reserved for Kyber".

**Suggested fix.** Normalise self-addressed outputs to local at parse time in both places, then apply the guard: at `:252`, `if (wcbNum == WCB_Number) wcbNum = 0;` before the `:254` check — and the identical two lines in `loadPWMMappingsFromPreferences()` at `:581-582`. This also makes the stored form canonical so `isSerialPortPWMOutput()` and `savePWMMappingsToPreferences()` agree.

### F-051 · S2 · `Code/WCB/WCB_PWM.cpp:267`

**addPWMMapping() zeroes a live mapping before validating, so a rejected command silently guts the running mapping and re-opens its output port to broadcast traffic**

*Category:* `logic`

`addPWMMapping()` reuses the existing slot for the same input port (`:196-204`), then destroys its contents *before* it knows the new command parses:

```c
PWMMapping &mapping = pwmMappings[slot];
mapping.inputPort = inputPort;
mapping.outputCount = 0;          // :227 — old outputs gone
```

`mapping.active` is deliberately left alone (unlike `loadPWMMappingsFromPreferences()`, which sets `active = false` at `:555` and only re-raises it at `:597` after a successful parse). If the parse yields nothing:

```c
if (mapping.outputCount == 0) {
    Serial.println("No valid outputs specified");
    return;                        // :269 — active is still true, outputCount is 0
}
```

the function returns *before* `savePWMMappingsToPreferences()` at `:296`. So RAM now holds `{active: true, outputCount: 0}` while NVS still holds the good mapping — the two disagree until the next reboot.

Two observable consequences:

1. `processPWMPassthrough()` (`:701`) iterates `j < outputCount == 0` — the mapping passes nothing through. PWM output stops.
2. `isSerialPortPWMOutput()` (`:775-786`) finds the port through the mapping's `outputs[]`, which is now empty, so it returns false. `processBroadcast` at `WCB.ino:6297` skips PWM output ports — with the mapping gutted it no longer skips, and ordinary broadcast serial traffic starts going out the TX pin that is wired to a servo/PWM device.

Zero-output parses are easy to hit: `serialPort` must be 1-5 and `wcbNum` 0-`MAX_WCB_COUNT` (`:252`), and the `W`-form needs `sPos > 0` (`:246`), so `W2` (no `S`), `S6`, `S0`, `W99S3` and `WS3` all produce zero valid outputs.

**Failure scenario.** Board has a working `?MAP,PWM,S1,S3` (RC input on S1 → servo on S3). The user types `?MAP,PWM,S1,W2` (forgetting the `S<port>`). `WCB.ino:4533-4542` builds `"PMS1,W2"`; the parse at `:241-249` sets `sPos = -1`, so `wcbNum`/`serialPort` stay 0 and the entry is rejected at `:252`. `addPWMMapping` prints "No valid outputs specified" and returns. The board does not reboot, keeps `pwmMappings[0].active == true` with `outputCount == 0`: the servo on S3 stops receiving pulses, and because `isSerialPortPWMOutput(3)` now returns false, every subsequent broadcast command is transmitted as serial data out SERIAL3_TX_PIN into the servo signal line. `?MAP,PWM,LIST` shows `Input: Serial1 -> Outputs:` with an empty list. A reboot restores the mapping from NVS, hiding the cause.

**Suggested fix.** Build the new mapping in a local `PWMMapping` and only commit it to `pwmMappings[slot]` after `outputCount > 0`; or, minimally, snapshot the slot at `:214` (it already snapshots `oldRemote[]` there) and restore it on the `:269` early return. Either way the slot must never be left `active` with `outputCount == 0`.

### F-052 · S2 · `Code/WCB/WCB_PWM.cpp:339`

**addPWMMapping() always reboots — no caller passes autoReboot=false — so a config restore with two or more PWM mappings loses every one after the first**

*Category:* `logic`

```c
if (autoReboot) {
    Serial.println("Rebooting in 3 seconds to apply PWM configuration...");
    delay(3000);
    ESP.restart();
}
```

`autoReboot` defaults to `true` (`WCB_PWM.h:38`) and **no caller anywhere passes `false`** — `grep -rn addPWMMapping Code/WCB` gives exactly two call sites, `WCB.ino:4542` (`?MAP,PWM,Sx,dest`) and `WCB.ino:5254` (`?PMS...`), both relying on the default. The `false` branch is dead, which is itself the tell that the suppression the parameter was added for was never wired up.

That breaks config restore. `collectConfigCommands()` emits one `MAP,PWM,S<in>,<outs>` line per active mapping (`WCB.ino:2919-2929`), and its own comment at `:2916` acknowledges the hazard: *"mappings LAST — restore triggers a reboot"*. Ordering them last protects the rest of the config, but does nothing for a second, third, … PWM mapping in the same chain. The Wizard pushes the whole chain as one string (`Wizard/app.js:8194` merely flags `MAP,PWM,S\d` as reboot-requiring); the board parses and enqueues all of it, and the loop-task drain at `WCB.ino:7503` executes items in order. The first `MAP,PWM,S…` blocks the drain for 3 s inside `delay(3000)` and then restarts — every queued command behind it is lost with the queue.

Up to 5 mappings can be active (one per input port 1-5), and two RC channels through one board is an ordinary configuration.

**Failure scenario.** A board is configured with `?MAP,PWM,S1,W2S1` and `?MAP,PWM,S2,W2S2` (two RC channels relayed to the body board), then backed up with the Wizard. Restoring that backup to a replacement board pushes the chain; the board applies everything up to and including `MAP,PWM,S1,W2S1`, then `addPWMMapping` prints "Rebooting in 3 seconds…", blocks the drain, and restarts. `MAP,PWM,S2,W2S2` never executes. The replacement board comes up with one of the two PWM mappings, no error shown, and the Wizard reports the push as sent. The loss is silent and repeatable on every restore.

**Suggested fix.** Give the restore path a non-rebooting entry: have the `?MAP,PWM,Sx,dest` handler at `WCB.ino:4542` call `addPWMMapping(pwmConfig, false)` and instead set a single deferred-reboot flag that the drain honours once the queue is empty (the pattern the `autoReboot` parameter was clearly added for). Alternatively, replace `delay(3000); ESP.restart()` at `:340-342` with a scheduled restart that lets the command queue finish draining first — the reboot only needs to happen before the next boot's UART init, not immediately.

### F-053 · S2 · `Code/WCB/WCB_PWM.cpp:469`

**clearAllPWMMappings() indexes remotePorts[wcb][] with an unbounded per-board counter — 6+ outputs on one board writes past the row, 6+ on WCB20 writes past the stack array**

*Category:* `bounds`

```c
int remotePorts[MAX_WCB_COUNT + 1][5];              // :456 — 21 x 5 ints, 420 bytes of stack
int remotePortCounts[MAX_WCB_COUNT + 1] = {0};      // :457
...
int wcb = pwmMappings[i].outputs[j].wcbNumber;      // :467 — validated 1..MAX_WCB_COUNT at parse time
remoteBoards[wcb] = true;
remotePorts[wcb][remotePortCounts[wcb]++] = pwmMappings[i].outputs[j].serialPort;   // :469
```

`wcb` is bounds-checked (`:252` / `:581` clamp it to `0..MAX_WCB_COUNT`, and `MAX_WCB_COUNT` is 20 via `WCB_Storage.h:12`), but the **second** index is not. The row holds 5 entries; nothing caps `remotePortCounts[wcb]`.

There is no dedup and no cap on how many outputs may name the same board. `addPWMMapping` permits at most one active mapping per input port (`:196-204`), so up to 5 mappings × 5 outputs each (`:230` caps `outputCount` at 5) = **25 remote outputs, all of which may name the same WCB** — nothing rejects `?MAP,PWM,S1,W20S1,W20S2,W20S3,W20S4,W20S5` followed by `?MAP,PWM,S2,W20S1,...`.

For `wcb < 20` the overflow lands in the next row, corrupting a *different* board's port list, which is then read back at `:480` and sent as `?PX<garbage>`. For `wcb == 20` (the last row) it writes past the end of the 420-byte stack array entirely — into whatever the compiler laid out next, plausibly `remotePortCounts[]` or `remoteBoards[]`, at which point the `p < remotePortCounts[wcb]` loop bound at `:480` is itself corrupt and the function reads arbitrary stack words as port numbers.

This runs on the loop task (queue drain at `WCB.ino:7503` → `processLocalCommand` → `WCB.ino:4514`).

**Failure scenario.** A dome board (WCB1) feeds six PWM channels to the body board WCB20: `?MAP,PWM,S1,W20S1,W20S2,W20S3` then `?MAP,PWM,S2,W20S4,W20S5,W20S1`. Both are accepted. The user then runs `?MAP,PWM,CLEAR,ALL`. The loop at `:463-471` writes 6 entries into `remotePorts[20][0..5]`; index 5 is one past the last row of a 21x5 array, i.e. past the end of the 420-byte stack object. With `remotePortCounts[]` laid out immediately after, `remotePortCounts[0]` (or whichever word follows) becomes 1, and the `for (int p = 0; p < remotePortCounts[wcb]; p++)` loop at `:480` then walks whatever `remotePortCounts[20]` now contains, snprintf'ing arbitrary stack ints into `?PX<n>` commands broadcast to the mesh — followed by `?REBOOT`. In the milder `wcb < 20` case the same overflow silently rewrites another board's clear list, so the wrong ports are cleared on the wrong board.

**Suggested fix.** Bound the write and dedup: `if (remotePortCounts[wcb] < 5) { bool dup = false; for (int k = 0; k < remotePortCounts[wcb]; k++) if (remotePorts[wcb][k] == port) { dup = true; break; } if (!dup) remotePorts[wcb][remotePortCounts[wcb]++] = port; }`. Dedup alone caps the row at 5 naturally, since only ports 1-5 exist.

### F-054 · S2 · `Code/WCB/WCB_PWM.cpp:483`

**?MAP,PWM,CLEAR,ALL sends its remote ?PX / ?REBOOT without ETM, so every ETM-enabled board silently ignores them**

*Category:* `protocol`

`clearAllPWMMappings()` notifies remote output boards with:

```c
snprintf(remoteCmd, sizeof(remoteCmd), "?PX%d", remotePorts[wcb][p]);
sendESPNowMessage(wcb, remoteCmd);          // :483 — useETM defaults to false
...
sendESPNowMessage(wcb, "?REBOOT");          // :491 — same
```

The extern at `WCB_PWM.cpp:9` declares `bool useETM = false`, so both sends take the 249-byte non-ETM path (`WCB.ino:2281` only enters the ETM branch when `etmEnabled && useETM`).

The receiver drops them. `WCB.ino:3688-3709` is the ETM-mismatch guard: when `!isETMPacket && etmEnabled`, the packet survives only if it is raw/Kyber-targeted, `isPWMPacket` (`structCommand[0] == ';' && structCommand[1] == 'P'|'p'`), `isMaestroPacket` (`structCommand[1] == 'M'|'m'`), or `isRcJsonPacket` (`structCommand[0] == '{'`). `"?PX3"` has `[0] == '?'` so `isPWMPacket` is false; `[1] == 'P'` so `isMaestroPacket` is false. `"?REBOOT"` has `[1] == 'R'` — also false. Both hit `return` at `:3708`.

Per `CLAUDE.md` rule 4, WDP requires ETM enabled, so every board on a fleet using discovery is in exactly this state. The sibling function `removePWMMapping()` gets this right — `WCB_PWM.cpp:358` passes `true` — and `addPWMMapping()` does too (`:313`, `:330`). Only the clear-all path is wrong.

**Failure scenario.** A fleet with ETM on (required for WDP). Board 1 has `?MAP,PWM,S1,W2S3,W3S4`. The user runs `?MAP,PWM,CLEAR,ALL` on board 1. Board 1 wipes its own mappings and NVS and reboots, printing "Sent PWM output removal to WCB2: ?PX3". Boards 2 and 3 drop both packets at `WCB.ino:3708` (logged only if `debugEnabled`/`debugETM`), so their `pwm_outputs` NVS entries survive. S3 on board 2 and S4 on board 3 stay held LOW as GPIO outputs, and `SerialN.begin()` continues to be skipped for them on every subsequent boot (`WCB.ino:7251`+). The user believes the fleet is clean; two ports are permanently dead until they manually issue `?MAP,PWM,CLEAR,OUT,S<n>` on each board.

**Suggested fix.** Pass `true` for `useETM` on both sends, matching `removePWMMapping()` at `:358`: `sendESPNowMessage(wcb, remoteCmd, true);` and `sendESPNowMessage(wcb, "?REBOOT", true);`.

### F-055 · S2 · `Code/WCB/WCB_RemoteTerm.cpp:57`

**Per-received-packet ESP-NOW amplification: mirrored Serial output calls esp_now_send from inside the ESP-NOW receive callback**

*Category:* `concurrency`

`WCBSerial::write(const uint8_t *buf, size_t size)` (WCB_RemoteTerm.cpp:55-61) feeds every byte to `_bufChar()` whenever `_relayWCB` is set, from whatever context called `Serial.print`. Because `Serial` is `#define`d to this object for the entire sketch (WCB_RemoteTerm.h:104), that includes every print made from inside `espNowReceiveCallback()` — which this file's own comment (lines 140-144) identifies as the context that must never do this kind of work.

Concrete callers inside the receive callback:
- WCB.ino:3775 `Serial.printf("[ETM] WCB%d came ONLINE%s (src MAC: ...)")` — **unconditional**, no debug gate.
- WCB.ino:3708 `Serial.println("[ETM] Dropped non-ETM packet ...")` — unconditional.
- WCB.ino:4133-4154, the raw-serial mapping branch: with `debugMaestro` or `debugRawSerial` on, it emits a hex dump of up to 177 bytes (~531 chars) followed by `Serial.println()` — the comment at WCB.ino:4131 confirms this code runs on the WiFi task ("flushing here blocks the WiFi task (ESP-NOW)").

With an RTERM session armed, each of those prints reaches `_flushLine()` (line 73) -> `_sendPacket()` -> `esp_now_send()` (line 111) synchronously *inside* `esp_now_recv_cb`. A 531-char hex dump exceeds `LINEBUF_SIZE` (160) three times, so a single inbound packet emits **four** outbound ESP-NOW frames from within the receive callback, with no pacing — note the firmware paces its own multi-frame bursts with `delay(20)` between frags elsewhere (WCB.ino:3035, :3119).

**Failure scenario.** WCB5 is a raw-serial/Kyber passthrough target with `debugRawSerial` enabled (a normal debugging setup) and has an armed RTERM session to relay WCB2. A Maestro/Kyber stream arrives at, say, 40 chunks/s. Each inbound frame runs the hex-dump branch at WCB.ino:4137-4154 on the WiFi task, producing ~4 `esp_now_send` calls from inside `esp_now_recv_cb` — ~160 outbound frames/s issued re-entrantly from the receive path, on top of ~5 ms of blocking UART per line. Inbound passthrough traffic is stalled, ESP-NOW frames are dropped, and the board is a strong watchdog candidate — the exact starvation this file's comment at lines 140-144 says crashes the board.

**Suggested fix.** Do not transmit from `write()`. Queue the completed line (a small `xQueueSend`-with-0-timeout ring, symmetric with the receive-side `s_rtermQueue`) and add an `rtermTargetDrain()` called from `loop()` alongside `rtermRelayDrain()` (WCB.ino:7478) that performs the `esp_now_send`. That keeps all ESP-NOW transmission out of the WiFi callbacks, bounds burst depth, and also removes the send-callback feedback loop.

### F-056 · S2 · `Code/WCB/WCB_RemoteTerm.cpp:118`

**Terminal session never expires — `_relayWCB` latches forever when the subscriber goes away**

*Category:* `protocol`

`_relayWCB` is set only in `startSession()` (WCB_RemoteTerm.cpp:121) and cleared only in `stopSession()` (line 129). Those are the *only* two callers in the tree — WCB.ino:5108 and WCB.ino:5113, both reachable only from an explicit `?RTERM,START,<n>` / `?RTERM,STOP` command. There is no keepalive, no last-seen timestamp, no consecutive-failure counter, and no periodic tick anywhere in this 186-line module that could age a session out. `esp_now_send`'s return value at line 111 is discarded, and the send callback result is never fed back here, so the module has no notion at all of whether the relay is still alive.

The on-demand `esp_now_add_peer()` at lines 105-110 also never gets a matching `esp_now_del_peer()` when the session ends — `stopSession()` (lines 126-132) leaves the peer registered. Elsewhere the firmware does pair peer add/delete (WCB.ino:1083-1084, :6999-7000), so this one is an outlier that consumes an ESP-NOW peer slot permanently.

The Wizard only sends `?RTERM,STOP` on a graceful teardown (Wizard/app.js:6079-6091), and that command has to travel *through the relay* — so in every case where the relay itself is what disappeared, STOP is structurally undeliverable.

**Failure scenario.** User opens a relayed terminal for WCB7 through relay WCB2, then closes the browser tab (or the laptop sleeps, or the relay board is unplugged/reflashed). No `?RTERM,STOP` is delivered. WCB7 keeps `_relayWCB=2` set indefinitely and unicasts an ESP-NOW frame for every single line its firmware prints — boot logs, heartbeat/debug output, every command echo — to a MAC that is no longer listening, for as long as the board stays powered. The only recovery is a reboot of WCB7 or a new relay session that can reach it. With debug enabled this also feeds the infinite loop in the espNowSendCallback finding.

**Suggested fix.** Give the session a liveness contract: record `_sessionStartMs`/`_lastOkMs` and have the send callback (or a counter of consecutive `esp_now_send` non-`ESP_OK` returns plus MAC-layer failures) auto-`stopSession()` after N consecutive failures; and/or require the Wizard to re-arm periodically and expire the session after e.g. 5 minutes of no re-arm — the Wizard already re-issues `?RTERM,START` idempotently on every ETM-online edge and config pull (Wizard/app.js:7723, :8001), so a refresh-driven lease costs nothing on that side. Also call `esp_now_del_peer()` in `stopSession()` for a peer that this module added and that no other subsystem still needs.

### F-057 · S2 · `Code/WCB/WCB_RemoteTerm.cpp:183`

**`HardwareSerial::printf` does NOT bypass WCBSerial forwarding — the comment at rtermRelayDrain is factually wrong, so relayed output is re-forwarded**

*Category:* `logic`

WCB_RemoteTerm.cpp:182-183:
```
      // Use parent-class printf to avoid re-triggering the WCBSerial forwarding path
      WCBDebugSerial.HardwareSerial::printf("[TERM:%d]%s\n", (int)item.sourceWCB, item.text);
```
The comment is provably false against the core sources on this machine. `HardwareSerial` does not declare `printf` at all (checked `esp32/3.3.4/cores/esp32/HardwareSerial.h` — it declares only the `write` overloads at lines 334-352); `printf` is `Print::printf`, which is non-virtual, so the `HardwareSerial::` qualification selects the same single function either way. `Print::printf` -> `Print::vprintf`, and `Print.cpp:66` is `len = write((uint8_t *)temp, len);` — an **unqualified virtual call on `this`**, which dispatches to `WCBSerial::write(const uint8_t*, size_t)` (WCB_RemoteTerm.cpp:55). Qualifying the *outer* call does nothing to the *inner* virtual dispatch.

So the loop-prevention this line claims does not exist. The same mistake makes `stopSession()`'s `HardwareSerial::println` (line 131) go through the wrapper too — `Print::println(const char[])` -> `Print::print(const char[])` (Print.cpp:94) -> `Print::write(const char*)` -> virtual `write(buf,len)`. (Note the author's own comment at line 122 states the opposite and correct behaviour for `startSession`: "forwarding is now active so the relay sees it too" — the two comments contradict each other.)

Consequence: any board that is simultaneously a relay (draining `[TERM:n]` packets) and an RTERM target (`_relayWCB != 0`) re-forwards every relayed line onward over ESP-NOW.

**Failure scenario.** Two USB-connected boards where each is armed as the other's relay target (WCB1 has `_relayWCB=2`, WCB2 has `_relayWCB=1` — reachable via the Wizard's per-relay `startRemoteTermSession(relaySlot, n)` at Wizard/app.js:522 when both slots are managed). WCB1 prints one line -> WCB2's `rtermRelayDrain()` prints `[TERM:1]<line>` -> that print is forwarded back to WCB1 -> WCB1 prints `[TERM:2][TERM:1]<line>` -> forwarded to WCB2... Each round trip adds ~9 bytes, and once the text crosses the 160-byte `LINEBUF_SIZE` cap at line 73 the single line splits into two packets, each of which is independently re-forwarded — packet count grows without bound until both UARTs and the ESP-NOW channel are saturated. In the non-cyclic case (relay R is itself mirrored to Q), R silently leaks every managed board's terminal output onward to Q, doubling airtime and producing nested `[TERM:a][TERM:b]` prefixes that the Wizard's `[TERM:<n>]` demux mis-routes.

**Suggested fix.** Qualification cannot bypass a virtual `write`. Guard the state instead: in `rtermRelayDrain()`, snapshot and clear the forwarding flag around the print (`uint8_t save = _relayWCB; _relayWCB = 0; ...printf...; _relayWCB = save;` via a small friend/member helper), or add a `bool _suppressForward` member that `_bufChar()` checks at line 68. Fix the comment at line 182 to state what actually happens, and drop the misleading `HardwareSerial::` qualification at lines 123, 131 and 183.

### F-058 · S2 · `Code/WCB/WCB_Storage.cpp:643`

**A stored-sequence key longer than 15 chars is silently discarded but still registered in key_list and reported as saved**

*Category:* `nvs`

`saveStoredCommandsToPreferences()` takes the key from user input with no length check and ignores the write result:

```
632:  String key = message.substring(0, commaIndex);
...
642:  preferences.begin("stored_cmds", false);
643:  preferences.putString(key.c_str(), value);      // return discarded
...
656:  if (!alreadyExists) {
657:    existingKeys += key + ",";
658:    preferences.putString("key_list", existingKeys);
659:  }
...
663:  Serial.printf("Stored: Key='%s', Value='%s'\n", key.c_str(), value.c_str());
```

An NVS key is capped at 15 chars. I confirmed the core behaviour in the pinned toolchain: `Preferences::putString` (esp32 core 3.3.4, libraries/Preferences/src/Preferences.cpp:265-279) calls `nvs_set_str`, which returns `ESP_ERR_NVS_KEY_TOO_LONG`; the error goes only to `log_e`, which is compiled out at the default core debug level, and `putString` returns 0. The 0 is discarded at line 643. The key is then appended to `key_list` at line 657 (that write uses a 8-char key and succeeds), and line 663 prints unconditional success.

The same silent-failure path applies to any other `putString` failure — a full NVS partition (min_spiffs gives NVS 20 KB, and sequence values are hundreds of bytes each) or a value over the 4000-byte NVS string cap.

The Wizard caps sequence names at 15 (`Wizard/app.js:4220`, `:4549`), which is why this has not surfaced there — but the console `?C<key>,<value>` (WCB.ino:5408-5410) and `?SEQ,SAVE,<key>,<value>` (WCB.ino:4995), and any WCB_Client/NaviCore host sending either, are entirely unchecked.

**Failure scenario.** User types `?SEQ,SAVE,dome_panel_wave,;MP1^;t500^;MP2`. Board prints "Stored: Key='dome_panel_wave', Value='…'" (15 chars is fine; make it `dome_panels_wave`, 16 chars, and the NVS write fails). `?SEQ,LIST` then shows `Key: 'dome_panels_wave' -> Value: ''`, `;Cdome_panels_wave` prints "No command stored under key", the config backup emits `?SEQ,SAVE,dome_panels_wave,` with an empty value (WCB.ino:2910), and restoring that backup hits the `value.length() == 0` reject at line 637-640 — so the phantom key propagates but the sequence is gone. `sequenceInventoryHash()` also folds the empty value in, so peers pull an inventory that lists a sequence with no body.

**Suggested fix.** Validate before writing: `key.trim(); if (key.length() > 15) { Serial.printf("Sequence key '%s' is %u chars — NVS keys are limited to 15.\n", key.c_str(), key.length()); return; }`. Also check the return of `putString` at line 643 and skip the `key_list` append (and print a failure, not "Stored:") when it returns 0, so a full-NVS failure cannot leave a phantom entry either.

### F-059 · S2 · `Code/WCB/WCB_Storage.cpp:643`

**saveStoredCommandsToPreferences ignores the putString failure, so a sequence key longer than the 15-char NVS limit is listed but stores nothing**

*Category:* `nvs`

```
preferences.begin("stored_cmds", false);
preferences.putString(key.c_str(), value);          // return value discarded
... key_list updated unconditionally ...
Serial.printf("Stored: Key='%s', Value='%s'\n", …);  // always claims success
```
(:642-663). There is no bound on `key` anywhere upstream — WCB.ino:4995 (`?SEQ,SAVE`) and WCB.ino:5409 (`?C<key>,<value>`) both hand the raw user string straight in.

An ESP32 NVS key is capped at 15 usable characters (`NVS_KEY_NAME_MAX_SIZE 16` "including null terminator", idf-release_v5.5 nvs.h:61). `Preferences::putString` returns 0 and only `log_e`s on failure (Preferences.cpp:264-278 of the pinned core esp32@3.3.4) — invisible at the default core debug level. So an over-long key writes nothing, yet :656-658 still appends it to `key_list` and :663 still prints "Stored:".

The 15-char limit is clearly known elsewhere — the Wizard caps the sequence-key input at exactly `maxlength="15"` (Wizard/app.js:4220, :4549) — but the firmware itself, which is reachable from the console, from NaviCore/WCB_Client over the mesh, and from any restored backup file, has no guard. The same unchecked return also hides a full or exhausted NVS partition (min_spiffs gives NVS 20 KB), which is a realistic ceiling given the ~27 namespaces this firmware uses.

The ghost entry is not inert: it is in `key_list`, so `listStoredCommands()` prints it (:701-737), `buildSequenceNamesString()` publishes it in the `?SEQ,NAMES` / SEQ_REQ inventory (:786-829), and `sequenceInventoryHash()` folds it into the WDP `SEQHASH` advert (:748-782) that peers use to decide whether to re-pull.

**Failure scenario.** User types `?SEQ,SAVE,startup_sequence,;S1^;MD2` (key is 16 chars). `nvs_set_str` returns ESP_ERR_NVS_KEY_TOO_LONG, nothing is written, but the console prints "Stored: Key='startup_sequence', Value='…'" and the name is appended to key_list. `?SEQ,LIST` shows `Key: 'startup_sequence' -> Value: ''`, NaviCore's command picker lists it, and `;Cstartup_sequence` prints "No command stored under key" (:560-563). The user re-saves it repeatedly with the same result and no error text anywhere.

**Suggested fix.** Reject over-long keys up front (`if (key.length() > 15) { Serial.println("Sequence key too long (max 15 characters)"); preferences.end(); return; }`) and check the write: `if (preferences.putString(key.c_str(), value) == 0) { Serial.println("Save FAILED — key rejected or NVS full"); preferences.end(); return; }` — do not touch `key_list` or print "Stored" unless the value write succeeded.

### F-060 · S2 · `Code/WCB/WCB_Storage.cpp:834`

**Clearing all sequences wipes the seq_mig_done flag, so the legacy migration re-imports every deleted sequence on the next boot**

*Category:* `nvs`

`clearAllStoredCommands()` (:832-837) does `preferences.begin("stored_cmds", false); preferences.clear();` — an `nvs_erase_all` on the whole namespace. The one-time migration guard lives *inside that same namespace*: `migrateOldStoredCommands()` reads `preferences.getBool("seq_mig_done", false)` from `stored_cmds` at :876-880 and writes it back at :925-927.

So `clear()` deletes the "migration already ran" flag along with the sequences. On the next boot `migrateOldStoredCommands()` (called from setup, WCB.ino:7214) sees `already == false` and re-runs Case 1 (:884-898), which reads keys `CMD1`..`CMD80` out of the **legacy `stored_commands` namespace** and calls `saveStoredCommandsToPreferences()` for each one. Nothing in the firmware ever clears `stored_commands` — it is not in `eraseNVSFlash()`'s list (:941-1026) and `clearAllStoredCommands()` only touches `stored_cmds`. The legacy copies are therefore still there and get written straight back.

`eraseNVSFlash()` has the same problem and is worse, because it calls `ESP.restart()` at :1032 — the migration runs seconds later, unattended.

**Failure scenario.** A board that was ever flashed with the oldest firmware (so `stored_commands` holds CMD1..CMDn). User runs `?SEQ,CLEAR,ALL` (WCB.ino:4989 → clearAllStoredCommands) to wipe a stale sequence set, confirms `?SEQ,LIST` is empty, and reboots. `migrateOldStoredCommands()` re-imports every legacy CMD# key, prints "[MIGRATION] Recovered from 'stored_commands'", and the deleted sequences are back in key_list, back in `?SEQ,LIST`, back in the WDP `SEQHASH` inventory, and back in `;C` recall. Same on a factory reset: `?ERASENVS` → auto-restart → sequences reappear on a supposedly virgin board.

**Suggested fix.** Store the migration flag outside the namespace it protects (e.g. a `seq_mig` key in `wcb_config`), and have `clearAllStoredCommands()` / `eraseNVSFlash()` either preserve it or also clear the legacy `stored_commands` namespace. Simplest correct fix: after `preferences.clear()` in `clearAllStoredCommands()`, re-write `putBool("seq_mig_done", true)`, and add `stored_commands` to the `eraseNVSFlash()` list (which then genuinely retires the legacy store).

### F-061 · S2 · `Code/WCB/WCB_Storage.cpp:1026`

**Factory reset (?WCB_ERASE) leaves the DFPlayer config in NVS — it re-seizes its serial port after the reboot**

*Category:* `nvs`

`eraseNVSFlash()` clears an explicit list of namespaces. I enumerated every namespace actually opened anywhere in the firmware (`grep -o 'begin("[a-z_0-9]*"'` over Code/WCB) — 27 in total — and diffed it against the list in this function plus the two that `clearAllPWMMappings()` handles (`pwm_mappings`, `pwm_outputs`, WCB_PWM.cpp:499/503). Exactly two are missing:

1. **`dfp_cfg`** — written by `saveDFPSettings()` (WCB_DFP.cpp:388-396) and read at boot by `loadDFPSettings()` (WCB.ino:7142). It stores `port`, `en`, `baud`, `rwcb`, `defvol`, and `onErr`. A configured DFPlayer reserves its UART: `isSerialPortUsedForDFP()` (WCB_DFP.cpp:63-65) makes `processIncomingSerial()` return early for that port (WCB.ino:6370). This is exactly the failure the adjacent comment at WCB_Storage.cpp:1022-1023 records for HCR/WLED/vars — *"Previously MISSED by factory reset — HCR, WLED, and user-variable configs survived an erase and re-seized their ports / reappeared after reboot."* — and `mp3_cfg`, `hcr_cfg`, `wled_cfg` were all added. `dfp_cfg` was not.

2. **`stored_commands`** — the legacy sequence namespace. `stored_cmds` IS cleared (line 966-968), and that namespace holds the `seq_mig_done` flag (written at line 926). So after an erase, `migrateOldStoredCommands()` (called at boot, WCB.ino:7214) reads `already = false` at line 877 and re-runs Case 1 (lines 885-898), which walks CMD1..CMD80 in the still-populated `stored_commands` namespace and re-imports every legacy sequence into the freshly-wiped store.

**Failure scenario.** Board has `?DFP,S3` configured (DFPlayer on Serial3). User runs `?WCB_ERASE` to factory-reset before handing the board to someone else or repurposing S3. Board reboots; `loadDFPSettings()` restores `configured=true, serialPort=3`; `isSerialPortUsedForDFP(3)` returns true, so `processIncomingSerial(Serial3, 3)` returns immediately (WCB.ino:6370) and Serial3 no longer accepts commands — on a board the user believes is blank. `?D` volume and the ONFIN sequence key also survive. Separately, on any board that once ran pre-key_list firmware, `?WCB_ERASE` + reboot silently resurrects every legacy CMD1..CMDn sequence.

**Suggested fix.** Add `preferences.begin("dfp_cfg", false); preferences.clear(); preferences.end();` and `preferences.begin("stored_commands", false); preferences.clear(); preferences.end();` alongside the hcr_cfg/wled_cfg/wcb_vars trio at line 1024-1026. Better: make the erase list derive from a single shared table of namespace names so a new subsystem cannot be forgotten again — this is the third time this list has been found incomplete.

### F-062 · S2 · `Code/WCB/WCB_Storage.cpp:1026`

**Factory reset (?ERASENVS) never clears the dfp_cfg namespace — the DFPlayer config survives and re-seizes its serial port**

*Category:* `nvs`

`eraseNVSFlash()` (WCB_Storage.cpp:941-1032) enumerates every NVS namespace by hand. I extracted the full set of namespaces the firmware actually opens (`grep -o '.begin("…"' over Code/WCB`): bcast_block, bdcst_set, command_config, **dfp_cfg**, espnow_config, etm_config, hcr_cfg, hw_version, kyber_settings, kyber_targets, learned_peers, led_config, mac_config, maestro_cfg, mp3_cfg, pwm_mappings, pwm_outputs, serial_baud, serial_labels, serial_map, serial_monitor, stored_cmds, **stored_commands**, wcb_config, wcb_vars, wdp_cfg, wled_cfg. `eraseNVSFlash` clears 23 of them plus pwm_mappings/pwm_outputs via `clearAllPWMMappings()` at :1028. `dfp_cfg` is missing.

`dfp_cfg` is not cosmetic — it is a persistent serial-port claim exactly like mp3_cfg/hcr_cfg/wled_cfg: `saveDFPSettings()` writes `port` and `en` (WCB_DFP.cpp:388-396), `loadDFPSettings()` restores them at boot (called from setup, WCB.ino:7142), and `isSerialPortUsedForDFP()` (WCB_DFP.cpp:63-65) returns true for that port, which makes `processIncomingSerial()` skip it entirely (WCB.ino:6370 — "processDFPResponses() owns those bytes").

This is the identical defect the in-file comment at :1022-1023 records as already fixed for three other subsystems: "Previously MISSED by factory reset — HCR, WLED, and user-variable configs survived an erase and re-seized their ports / reappeared after reboot." DFPlayer was added after that fix and was not added to the list.

**Failure scenario.** Board has a DFPlayer configured on S4 (`?DFP,S4:9600:V20`). User runs the factory-reset command; `eraseNVSFlash()` wipes everything else and calls `ESP.restart()` at :1032. After the reboot `loadDFPSettings()` restores `configured=true, serialPort=4`, so `isSerialPortUsedForDFP(4)` is true, S4 is still silently excluded from `processIncomingSerial` (WCB.ino:6370), and `;D` commands still route to a device the user believes was erased. The board reports a clean config everywhere else, which makes the stuck port very hard to diagnose.

**Suggested fix.** Add `preferences.begin("dfp_cfg", false); preferences.clear(); preferences.end();` next to the hcr_cfg/wled_cfg/wcb_vars lines at :1024-1026. Better: replace the hand-maintained list with a single `nvs_flash_erase()` (or drive it from one array of namespace names shared with the load path) so the next subsystem added cannot repeat this.

### F-063 · S2 · `Code/WCB/WCB_Storage.cpp:1051`

**reconcileKyberTargetsFromMaestroConfigs matches by Maestro ID alone, so a second host for an already-targeted ID never gets a Kyber target**

*Category:* `logic`

The "already have a target?" test is keyed on the ID only and breaks on the first hit:
```
bool have = false;
for (int j = 0; j < MAX_KYBER_TARGETS; j++) {
  if (kyberTargets[j].enabled && kyberTargets[j].maestroID == id) { have = true; break; }
}
if (have) continue;
```
(:1049-1053). But the maestroConfigs[] it walks are keyed by `(maestroID, serialPort, remoteWCB)`, and duplicates of one ID across hosts are explicitly legal — `maestroAutoAddRemote()` (WCB_Maestro.cpp:723-758) creates a **separate slot per (id, host)** and its comment at :738-741 states "Intentionally NO 'already configured elsewhere' guard: duplicate Maestro ids are legal (same id local AND/OR on multiple hosts) … each host gets its OWN proxy."

`kyberTargets[]` can represent that fan-out perfectly well (two entries with the same maestroID and different targetWCB; `forwardDataFromKyber` at WCB.ino:4263-4280 loops all enabled entries without breaking). The ID-only guard here is the only thing preventing it.

That defeats the single purpose of the function. Its own call site says so: WCB_WDP.cpp:713-722 — "If THIS board is the Kyber host forwarding by target, a newly auto-learned remote Maestro must land in kyberTargets[] too — otherwise forwardDataFromKyber never broadcasts the sabre stream to the just-discovered board". The call is made precisely when `maestroAutoAddRemote()` returned true for a newly learned host, and if that host's Maestro ID already appears in kyberTargets (which it does whenever the ID is also hosted locally, or was learned from a different board first) the function adds nothing and returns 0, so `saveKyberTargets()` is never even called.

**Failure scenario.** Kyber-LOCAL board WCB1 with targeting on and Maestro 2 local on S1 (kyberTargets[0] = {id 2, W1, S1}). WCB3 comes online advertising that it also hosts Maestro 2 (legal per the documented slot identity). WDP calls `maestroAutoAddRemote(2, 3, baud)` which creates a second maestroConfigs slot {id 2, port 0, remoteWCB 3} and returns true, so WCB_WDP.cpp:719 calls `reconcileKyberTargetsFromMaestroConfigs()`. The loop reaches that new slot, finds an enabled kyberTargets entry with maestroID==2, sets `have=true`, and skips it. Return value 0 → no save, no log. Because `kyberUseTargeting` is true, `forwardDataFromKyber` takes the targeted branch (WCB.ino:4258) and the Kyber/sabre stream never reaches WCB3's Maestro 2 — the exact outcome the call site exists to prevent — and there is no console output telling the user why.

**Suggested fix.** Match the full slot identity, not the ID: `if (kyberTargets[j].enabled && kyberTargets[j].maestroID == id && kyberTargets[j].targetWCB == (maestroConfigs[i].remoteWCB > 0 ? maestroConfigs[i].remoteWCB : WCB_Number)) { have = true; break; }` — i.e. a target already exists only when it points at *this* host.

### F-064 · S2 · `Code/WCB/WCB_Storage.cpp:1118`

**?KYBER,CLEAR,Sx does not clear Kyber targets — it repopulates them from maestroConfigs and persists them with kyberUseTargeting=true**

*Category:* `logic`

The auto-populate block runs before the branch dispatch and is gated only on `kyberUseTargeting && params.length() == 0`:

```
1118:  if (kyberUseTargeting && params.length() == 0) {
1119:      int targetIndex = 0;
1120:      for (int i = 0; i < MAX_KYBER_TARGETS; i++) kyberTargets[i].enabled = false;
1123:      for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
1124:          if (maestroConfigs[i].configured && targetIndex < MAX_KYBER_TARGETS) { ... enabled = true; ... }
```

`kyberUseTargeting` is set true at line 1106 for ANY `<verb>,Sx` form, including CLEAR, and `params` is emptied at line 1103 when there is no second comma. So `?KYBER,CLEAR,S2` takes this branch, wipes the targets, then immediately rebuilds an enabled target for every configured Maestro and prints "Auto-populated N Kyber targets…". The CLEAR branch at line 1184 then calls `saveKyberTargets()` at line 1210 before returning at 1212, persisting `use_target = true` plus the rebuilt table.

The bare `?KYBER,CLEAR` form (WCB.ino:5239, and what the Wizard emits — Wizard/parser.js:1355) takes the `firstComma == -1` path at line 1078, which correctly sets `kyberUseTargeting = false` and zeroes every target at lines 1084-1089. So the two spellings of the same command do opposite things.

**Failure scenario.** User with Maestros configured runs `?KYBER,CLEAR,S2` (the natural inverse of the `?KYBER,LOCAL,S2` they typed to set it up) intending to remove the Kyber configuration. `Kyber_Location` becomes " " and `kyberLocalPort` becomes 0, but NVS now holds `kyber_targets/use_target = true` with up to 9 enabled targets. `printKyberList()` reports "Kyber is not configured" (line 2117-2121) while `loadKyberTargets()` restores `kyberUseTargeting = true` on every subsequent boot, so the config backup and the Wizard's verify-pull see state the user was told was cleared, and re-enabling Kyber later inherits stale targets instead of a clean slate.

**Suggested fix.** Gate the auto-populate on the command actually being LOCAL: `if (baseCommand.equals("local") && kyberUseTargeting && params.length() == 0)`. In the CLEAR branch, explicitly set `kyberUseTargeting = false` and zero every `kyberTargets[]` entry before `saveKyberTargets()` at line 1210, matching what the bare-CLEAR path at lines 1081-1089 already does.

### F-065 · S2 · `Code/WCB/WCB_Storage.cpp:1191`

**?KYBER,CLEAR always acts on Serial2, not on the port Kyber was actually using — every full config push resets S2 to 9600 baud and re-enables its broadcast flags**

*Category:* `logic`

In `storeKyberSettings()`, when the message has no comma the code sets a hard-coded `kyberPort = 2` (:1078-1082). `?KYBER,CLEAR` reaches this function as the bare string `"clear"` (WCB.ino:4654-4655 dispatches `storeKyberSettings(args)` with args=="CLEAR"; also WCB.ino:5239 `storeKyberSettings("clear")`), so `kyberPort` is **always 2** on the clear path regardless of the real port, which is already available in the global `kyberLocalPort` (:1159, loaded at :1458).

The clear branch then acts on that bogus port:
  :1191-1194  `if (kyberPort > 0) { updateBaudRate(kyberPort, 9600); }`  → forces S2 to 9600 live **and in NVS** (`updateBaudRate` calls `applyLiveBaud` + writes `serial_baud`, :156-170)
  :1200-1204  `if (!serialBroadcastEnabled[kyberPort-1]) { …= true; saveBroadcastSettingsToPreferences(); }` → re-enables broadcast OUT on S2
  :1205-1209  `if (blockBroadcastFrom[kyberPort-1]) { …= false; saveBroadcastBlockSettings(); }` → unblocks broadcast IN on S2

This is not a corner case — `KYBER,CLEAR` is emitted by **both** config-push paths for every board that isn't running Kyber: `collectConfigCommands()` at WCB.ino:2859 (`emit("KYBER,CLEAR", true)`, includeInLive=true, so it is in the restore chain) and the Wizard at Wizard/parser.js:1355. In both orderings the baud and broadcast commands are emitted **before** Kyber (WCB.ino:2806 BAUD / :2812-2818 BCAST vs :2859 KYBER; parser.js:1309-1336 BAUD/BCAST vs :1355 KYBER), so the ?KYBER,CLEAR at the end silently overwrites what the push just applied.

Secondary defect in the same block: :1200 and :1205 index `serialBroadcastEnabled[kyberPort-1]` / `blockBroadcastFrom[kyberPort-1]` with no range check, unlike the local branch which guards with `if (kyberPort > 0 && kyberPort <= 5)` at :1162. `?KYBER,CLEAR,S0` (or any non-numeric `Sx`, since `portStr.toInt()` yields 0) reaches :1200 with `kyberPort == 0` and reads/writes `blockBroadcastFrom[-1]` — an out-of-bounds write into whatever precedes the array.

**Failure scenario.** A board with no Kyber has S2 wired to a device at 57600 with broadcast output turned off. User does a full push from the Wizard (or restores the board's own `?BACKUP` chain). Sequence executed: `?BAUD,S2,57600` (applied) → `?BCAST,OUT,S2,OFF` (applied) → `?KYBER,CLEAR` → S2 forced to 9600 and persisted, broadcast OUT re-enabled and persisted, broadcast IN unblocked and persisted. The device on S2 stops communicating. The Wizard's verify-pull then reads back 9600/ON, sees a delta against its baseline, and re-pushes the same change on every save — the exact loop the comment at :162-166 says the in-memory baud sync exists to prevent. Separately, on a board where Kyber really was local on S3, `?KYBER,CLEAR` leaves S3 stuck at 115200 with broadcast blocked while molesting S2.

**Suggested fix.** In the clear branch use the real port: capture `int clearPort = (kyberPort >= 1 && kyberPort <= 5) ? kyberPort : kyberLocalPort;` *before* `kyberLocalPort = 0` at :1188, bail out of the baud/broadcast restoration when it is 0, and guard every `[clearPort-1]` index with `clearPort >= 1 && clearPort <= 5` the way :1162 already does.

### F-066 · S2 · `Code/WCB/WCB_Storage.cpp:1200`

**?KYBER,CLEAR,S0 writes serialBroadcastEnabled[-1] and blockBroadcastFrom[-1] (out-of-bounds write of a global)**

*Category:* `bounds`

`storeKyberSettings()` validates `kyberPort` only for the LOCAL branch:

```
1112:  if (baseCommand.equals("local") && kyberUseTargeting && (kyberPort < 1 || kyberPort > 5)) {
1113:    Serial.println("Invalid Kyber port. Must be S1-S5");
1114:    return;
```

The CLEAR branch has no such guard. It guards only the baud call:

```
1191:    if (kyberPort > 0) {
1192:      updateBaudRate(kyberPort, 9600);
...
1200:    if (!serialBroadcastEnabled[kyberPort - 1]) {
1201:      serialBroadcastEnabled[kyberPort - 1] = true;
...
1205:    if (blockBroadcastFrom[kyberPort - 1]) {
1206:      blockBroadcastFrom[kyberPort - 1] = false;
```

Lines 1200-1209 are outside that `kyberPort > 0` block. `serialBroadcastEnabled` and `blockBroadcastFrom` are both `[5]` (WCB.ino:629 and WCB.ino:600).

Reachability: WCB.ino:4654 dispatches on `argsUpper.startsWith("CLEAR")` and passes the raw args through, so `?KYBER,CLEAR,S0` arrives here as `message == "CLEAR,S0"`. Trace: `firstComma == 5` → `baseCommand = "clear"`, `params = "S0"` → line 1096 `params.startsWith("S")` true, `secondComma == -1` → `portStr = "0"`, `kyberPort = portStr.toInt() = 0`. Line 1112's guard does not apply (baseCommand != "local"). Line 1191 skips `updateBaudRate`. Line 1200 then evaluates `serialBroadcastEnabled[-1]`. `?KYBER,CLEAR,S7` gives the mirror case, `serialBroadcastEnabled[6]` / `blockBroadcastFrom[6]`, past the end.

**Failure scenario.** User types `?KYBER,CLEAR,S0` (or `?KYBER,CLEAR,S` / `?KYBER,CLEAR,S7`) at the console or over the mesh. The read at line 1200 loads a byte one before `serialBroadcastEnabled[0]`; if it reads as 0 the code writes `true` into that adjacent global (WCB.ino:600-629 region holds other bools such as `blockBroadcastFrom[]`, `mirrorToKyber`, `serialMonitorEnabled[]`), then calls `saveBroadcastSettingsToPreferences()` and persists a broadcast table built from corrupted state. Line 1205-1208 does the same for `blockBroadcastFrom[-1]`. Result is a silently corrupted neighbouring flag that persists to NVS.

**Suggested fix.** Hoist the port validation out of the LOCAL-only condition, e.g. right after `kyberPort` is parsed: `if (kyberUseTargeting && (kyberPort < 1 || kyberPort > 5)) { Serial.println("Invalid Kyber port. Must be S1-S5"); return; }` — and additionally wrap lines 1200-1209 in the same `kyberPort >= 1 && kyberPort <= 5` test that already guards line 1191.

### F-067 · S2 · `Code/WCB/WCB_Storage.cpp:1752`

**Toggling a serial mapping's RAW mode does not persist when the destination list is unchanged — and the one function that would persist it is never called**

*Category:* `logic`

`addSerialMonitorMapping()` applies the R-suffix raw-mode change to the in-memory mapping unconditionally:

```
1663:    if (mapping->rawMode != inputRawMode) {
1664:        mapping->rawMode = inputRawMode;
1665:    }
```

but the NVS write is gated on a *destination* having been added:

```
1752:    if (outputsAdded > 0) {
1753:        saveSerialMonitorMappings();
...
1756:    } else {
1757:        Serial.println("No new destinations added (all were duplicates or invalid)");
1758:    }
```

When the mapping already exists with the same destinations, every destination hits the duplicate `continue` at line 1728, `outputsAdded` stays 0, and `saveSerialMonitorMappings()` is skipped — so `rawMode` changes in RAM but not in `serial_map`. This is the only reachable way to change raw mode: `setSerialMappingRawMode()` (line 1818), which *does* call `saveSerialMonitorMappings()`, is declared at WCB_Storage.h:239 and defined at line 1818 but has zero callers anywhere in Code/WCB (verified by grep across `*.cpp *.h *.ino`).

The RAM-vs-NVS split is immediately observable because `RawSerialForwardingTask` (WCB.ino:6643-6644) and `serialCommandTask` (WCB.ino:6603-6605, via `isSerialPortRawMapped`) both read `rawMode` live, on a different task from the one that wrote it.

**Failure scenario.** User has `?SMS3,W2S1` (line mode) forwarding S3 to WCB2's S1 and now needs binary passthrough, so they type `?SMS3R,W2S1`. W2S1 is already in the destination list, so `outputsAdded == 0`; the board prints "No new destinations added (all were duplicates or invalid)" but raw forwarding does start working (RAM flag flipped). At the next reboot `loadSerialMonitorMappings()` reads `sm0_raw = false` and raw mode silently reverts — binary data on S3 is line-parsed and corrupted again, with the user's earlier command apparently still in effect.

**Suggested fix.** Persist the raw-mode change where it is made — call `saveSerialMonitorMappings()` inside the `if (mapping->rawMode != inputRawMode)` block at lines 1663-1665, or track a `bool changed` and OR it into the line 1752 condition. Either remove `setSerialMappingRawMode()` as dead code or wire it to a `?SMRAW` command.

### F-068 · S2 · `Code/WCB/WCB_Storage.cpp:1918`

**?SMCLEAR never re-enables broadcast OUTPUT on the ports it un-maps, unlike ?SMRSx**

*Category:* `logic`

`addSerialMonitorMapping()` disables BOTH directions when a mapping is created:

```
1739:  if (!blockBroadcastFrom[inputPort - 1]) { blockBroadcastFrom[inputPort - 1] = true;  saveBroadcastBlockSettings(); ... }
1744:  if (serialBroadcastEnabled[inputPort - 1]) { serialBroadcastEnabled[inputPort - 1] = false; saveBroadcastSettingsToPreferences(); ... }
```

`removeSerialMonitorMapping()` correctly reverses both (lines 1887-1898). `clearAllSerialMonitorMappings()` reverses only the input side:

```
1914:    if (serialMonitorMappings[i].active) {
1915:        int port = serialMonitorMappings[i].inputPort;
1918:        if (blockBroadcastFrom[port - 1]) blockBroadcastFrom[port - 1] = false;
1919:    }
...
1929:    saveBroadcastBlockSettings();
1930:    saveSerialMonitorMappings();
```

`serialBroadcastEnabled[]` is never touched and `saveBroadcastSettingsToPreferences()` is never called, so every port that had a mapping stays broadcast-output-disabled in RAM and in NVS, permanently, with nothing in the output mentioning it. Reached from WCB.ino:5246 (`?SMCLEAR`).

Secondary, in the same loop: `port` comes straight from `serialMonitorMappings[i].inputPort`, which `loadSerialMonitorMappings()` fills from NVS with a default of 0 (line 1547) while reading `active` from a separate key (line 1540). An `active` slot with `inputPort == 0` indexes `blockBroadcastFrom[-1]` at line 1918 — `removeSerialMonitorMapping()` validates 1-5 (line 1865) but this path does not.

**Failure scenario.** User maps S3 and S4 for monitoring (`?SMS3,W2S1`, `?SMS4,W2S2`), which auto-disables broadcast output on S3 and S4. Later they run `?SMCLEAR` to tear the mappings down. `?BAUD` now shows "Broadcast Output: Disabled" on S3 and S4 with no mapping to explain it, and a `;` broadcast is no longer echoed to those devices — a silent, persistent regression that survives reboot because it is written to the `bdcst_set` namespace by the earlier `saveBroadcastSettingsToPreferences()` call.

**Suggested fix.** Mirror `removeSerialMonitorMapping()` inside the loop: guard `if (port >= 1 && port <= 5)`, clear `blockBroadcastFrom[port-1]`, and set `serialBroadcastEnabled[port-1] = true`. After the loop call `saveBroadcastSettingsToPreferences()` alongside the existing `saveBroadcastBlockSettings()` at line 1929.

### F-069 · S2 · `Code/WCB/WCB_Storage.cpp:1918`

**clearAllSerialMonitorMappings restores broadcast INPUT but not broadcast OUTPUT, so every previously mapped port stays silently non-broadcasting**

*Category:* `logic`

`addSerialMonitorMapping()` disables **both** directions when a mapping is created (:1739-1748): `blockBroadcastFrom[inputPort-1] = true` + `saveBroadcastBlockSettings()`, and `serialBroadcastEnabled[inputPort-1] = false` + `saveBroadcastSettingsToPreferences()`.

`removeSerialMonitorMapping()` correctly undoes both (:1887-1898) — blockBroadcastFrom cleared *and* serialBroadcastEnabled re-enabled, each with its own save.

`clearAllSerialMonitorMappings()` only undoes half:
```
if (serialMonitorMappings[i].active) {
    int port = serialMonitorMappings[i].inputPort;
    if (blockBroadcastFrom[port - 1]) { blockBroadcastFrom[port - 1] = false; }
}
...
saveBroadcastBlockSettings();
saveSerialMonitorMappings();
```
(:1912-1931). `serialBroadcastEnabled[]` is never touched and `saveBroadcastSettingsToPreferences()` is never called, so the broadcast-output disable that `addSerialMonitorMapping` applied is left latched in RAM and in NVS with nothing left in the config to explain it.

Secondary: `int port = serialMonitorMappings[i].inputPort;` at :1915 is used as `blockBroadcastFrom[port - 1]` with no range check, so an entry loaded as active with inputPort 0 (possible via :1547, see the unclamped-load finding) indexes element -1.

**Failure scenario.** User maps S3 and S4 to remote destinations (`?SMS3,W2S1`, `?SMS4,W2S2`) — broadcast in and out are auto-disabled on both. Later they clear everything with `?SMCLEAR` (WCB.ino:4486 / :5246 → clearAllSerialMonitorMappings). Broadcast INPUT comes back on S3/S4, but broadcast OUTPUT stays off and stays off across reboots. Nothing in `?SM` or the mapping list mentions those ports any more, so the only visible symptom is that broadcast commands stop reaching the devices on S3/S4 and the user has to guess that `?BCAST,OUT,S3,ON` is needed. Clearing the same two mappings one at a time with `?SMRS3` / `?SMRS4` behaves correctly, which makes the difference look random.

**Suggested fix.** Mirror `removeSerialMonitorMapping`: inside the `if (active)` block also do `if (!serialBroadcastEnabled[port-1]) serialBroadcastEnabled[port-1] = true;`, guard `port` to 1..5 first, and call `saveBroadcastSettingsToPreferences()` alongside `saveBroadcastBlockSettings()` at :1929.

### F-070 · S2 · `Code/WCB/WCB_Storage.cpp:2229`

**loadETMSettings() defaults etmMissedHeartbeats to 3, silently reverting the deliberately-widened 5 on every board that has never run ?ETM,MISS**

*Category:* `nvs`

The compile-time default carries an explicit rationale:

```
WCB.ino:267:  int etmMissedHeartbeats = 5;   // (etmHeartbeatSec+1)*5 = 55s offline window — widened from 3/33s so a
                                 // few lost broadcast heartbeats on a busy mesh don't flap a live board
                                 // (mirrors WCBClient _missedBeforeOffline)
```

but the NVS read default was not updated with it:

```
2229:    etmMissedHeartbeats = preferences.getInt("etmMiss", 3);
```

`loadETMSettings()` is called unconditionally at boot (WCB.ino:7416), so it overwrites the 5 with 3 on any board whose `etm_config` namespace lacks an `etmMiss` key. `saveETMSettings()` is only reached from the `?ETM,…` handlers (WCB.ino:4673-4714, 5304-5334), so a fresh board, or any board after `?WCB_ERASE` (which clears `etm_config` at WCB_Storage.cpp:1002-1004), never has that key.

Every other default in this pair matches: `etmEnabled` true/true, `etmBoot` 2/2, `etmHB` 10/10, `etmTimeout` 500/500, `etmCharCount` 20/20, `etmCharDelay` 100/100, `etmChksm` true/true (WCB.ino:264-271 vs WCB_Storage.cpp:2226-2233). `etmMissedHeartbeats` is the only one that disagrees — and WCB.ino:264's comment ("NVS default is also true") shows the intent was for them to mirror.

**Failure scenario.** Out-of-the-box board, or any board after a factory reset, boots with `etmMissedHeartbeats = 3` instead of 5. The offline window is `(etmHeartbeatSec + 1) * etmMissedHeartbeats = 33 s`, not the 55 s the comment says shipped. On a busy mesh three consecutive lost broadcast heartbeats mark a live board OFFLINE, which is precisely the flapping the widening was introduced to stop; the fix only takes effect on boards where someone happened to run `?ETM,MISS`. `?SETTINGS` prints "Offline after: 3 missed heartbeats (33 sec max)" (WCB.ino:2254-2255), so the discrepancy is visible but reads as intentional.

**Suggested fix.** Change line 2229 to `preferences.getInt("etmMiss", 5);` so the NVS default mirrors WCB.ino:267. Consider deriving both from a single `#define ETM_MISSED_DEFAULT 5` so they cannot drift again.

### F-071 · S2 · `Code/WCB/WCB_WDP.cpp:483`

**WDP flipping Maestro_Remote at runtime leaves Serial1 RX read by no task until reboot**

*Category:* `concurrency`

`wdpEvaluateMaestroRemote()` is called from the advert-receive path (WCB_WDP.cpp:636) and, when a controller is on the mesh and this board has a local Maestro, does:

```cpp
  if (why) {
    Serial.printf("[WDP] %s — enabling Maestro remote (reboot to fully apply)\n", why);
    storeKyberSettings("remote");
  }
```

`storeKyberSettings("remote")` sets the live global `Maestro_Remote = true` immediately (WCB_Storage.cpp:1179-1183). Two consumers read that global with opposite lifetimes:

- `serialCommandTask` re-reads it every iteration and, once true, stops calling `processIncomingSerial(Serial1, 1)` entirely — it only services S2 in that branch (WCB.ino:6606-6613 vs the normal branch :6615-6621).
- `KyberRemoteTask`, the task that would drain the Maestro ports instead (`forwardMaestroDataToRemoteKyber`, WCB.ino:4330), is created **only in setup()** and only if `Maestro_Remote` was already true at boot (WCB.ino:7446-7449).

So between the flip and the next reboot, nothing reads Serial1 at all. `RawSerialForwardingTask` only owns raw-mapped ports, so it does not cover this either. The adjacent comment (WCB_WDP.cpp:466-467) acknowledges the task-spawn gap ("reboot to fully apply") but the printed message reads as "the relay isn't active yet", not "S1 input is now dead", and nothing warns about the serial-ownership handover.

**Failure scenario.** WCB4 has a Maestro on S2 (so `wdpLocalMaestroIds()` returns 1 entry) and a Marcduino / HP controller on S1 that sends `?`/`;` command lines to the WCB. `Kyber_Local` and `Maestro_Remote` are both false. A NaviCore joins the mesh and sends its first WDP advert. WCB4 prints `[WDP] controller (NaviCore/Sabé) on mesh — enabling Maestro remote (reboot to fully apply)`, sets `Maestro_Remote=true`, and from the next `serialCommandTask` iteration onward every byte arriving on S1 is left in the UART ring buffer forever — commands from the S1 device stop working, and any `@WDP1` device announce on S1 ages out of `?WDP,DA` (and out of this board's advertised port label) after the 90 s TTL. The user sees an S1 device that silently went dead with no error, and there is no indication a reboot is required for anything other than the Maestro relay.

**Suggested fix.** Either create `KyberRemoteTask` on demand when `Maestro_Remote` transitions to true (mirroring how the PWM task is conditionally created), or do not apply the flip live from the WDP path — record the intent and require an explicit operator confirm/reboot. At minimum change the log line at :482 to state that Serial1 input stops until the board is rebooted.

### F-072 · S2 · `Code/WCB/WCB_WDP.cpp:704`

**?WDP,AUTOJOIN,OFF silently disables ALL WDP auto-configuration, not just peer membership**

*Category:* `logic`

The entire auto-configuration block is gated on `wdpAutoJoin`:

```cpp
  bool isControllerPeer = (specialPeerEnabled && senderWCB == WCB_SPECIAL_PEER_ID);
  if (wdpAutoJoin && !wcbPeerTemporary[senderWCB - 1] &&
      (isControllerPeer || (!nb.isClient && wcbPeerActive[senderWCB - 1]))) {
```

Everything inside that brace is skipped when auto-join is off: `maestroAutoAddRemote` (:709), the Kyber-target reconcile (:717-722), `wledAutoAddRemote` (:726), `hcrAutoAddRemote`/`mp3AutoAddRemote`/`dfpAutoAddRemote` (:731-733), the receiver-side PWM output self-config `addPWMOutputPort` (:747) and the PWM self-heal `reconcileWdpAutoPWMOutputs` (:761).

The block's own comment (WCB_WDP.cpp:688-702) explains the `wcbPeerActive` and `!isClient` conjuncts in detail but never mentions the `wdpAutoJoin` conjunct — it is not documented as deliberate anywhere. The docs treat the two as independent: `?WDP,AUTOJOIN[,ON|,OFF]` is described as "auto-join learned peers" (docs/WDP_DESIGN.md:196, and §6 which is entirely about *membership*), while §9 "Auto-configuration (shipped behaviors)" describes the PWM/Maestro/WLED/HCR behaviours unconditionally (docs/WDP_DESIGN.md:258, :280) and ends with "Plus the membership auto-join of §6" — i.e. auto-join is presented as one *additional* behaviour, not the master switch for the rest.

None of these actions actually need auto-join: for a WCBQ-floor fleet the peers are already `wcbPeerActive[]` from boot, so the only thing standing between the advert and the configuration is this flag.

**Failure scenario.** Fleet of 4 boards, WCBQ=4 (so boards 1-4 are floor peers, `wcbPeerActive[]` true from boot). Operator runs `?WDP,AUTOJOIN,OFF` on WCB1 because they do not want strangers joining the mesh — a documented, supported setting. WCB2 hosts Maestro id 3 on S2 and advertises MAESTRO_CFG; WCB3 has a PWM mapping whose output is `W1S4` and advertises PWMTARGET [1,4]. On WCB1: no remote proxy is ever created for Maestro 3, so `;M3` finds no slot and is dropped; S4 is never configured as a PWM output, so every PWM frame WCB3 sends to WCB1 S4 is discarded on arrival; `;H`/`;A`/`;D` never learn a persisted host. Nothing is logged — the boards look discovered in `?WDP,LIST` and the user has no signal that auto-config was suppressed.

**Suggested fix.** Drop `wdpAutoJoin &&` from the condition at :704 and keep the reachability gate (`isControllerPeer || (!nb.isClient && wcbPeerActive[...])`), which already provides the >=2-advert vetting for learned peers and blocks unreachable hosts. If suppressing auto-config with auto-join off is genuinely intended, say so in the block comment and in docs/WDP_DESIGN.md §7/§9.

### F-073 · S2 · `Code/WCB/WCB_WLED.cpp:326`

**configureWLED's port-conflict guard omits DFPlayer and local Maestro — WLED silently steals the port and re-bauds it**

*Category:* `logic`

The local-config guard in `configureWLED` checks PWM out/in, MP3, HCR and Kyber/Maestro_Remote, but not the DFPlayer and not a local Maestro slot:

```cpp
  if (isSerialPortPWMOutput(serialPort) || isSerialPortUsedForPWMInput(serialPort) ||
      isSerialPortUsedForMP3(serialPort) || isSerialPortUsedForHCR(serialPort) ||
      (serialPort == 1 && (Kyber_Local || Maestro_Remote)) ||
      (serialPort == 2 && Kyber_Local)) {
```

`isSerialPortUsedForDFP(int port)` exists and is exported (`Code/WCB/WCB_DFP.cpp:63`, declared `Code/WCB/WCB_DFP.h:61`). DFP's own guard *does* call `isSerialPortUsedForWLED` (`WCB_DFP.cpp:268`), so the reservation was meant to be mutual — the WLED side was simply never updated. Git history confirms the direction of the miss: DFPlayer support landed 2026-08-05 (`dac753e`), WLED was last touched 2026-07-22 (`e01501f`).

Once the guard falls through, `wledReserveLocalPort` (`WCB_WLED.cpp:164-179`) unconditionally takes the port: `updateBaudRate(port, baud)` reprograms the live UART (`WCB_Storage.cpp:146`, `applyLiveBaud` at `WCB.ino:1885`), forces `serialBroadcastEnabled=false` / `blockBroadcastFrom=true`, and relabels the port "WLED <id>". `DFP_BAUD` is a hard 9600 and `configureDFP` rejects anything else (`WCB_DFP.cpp:28`, `:248`), while WLED's normal baud is 115200.

The same expression also omits local Maestro slots. There is no `isSerialPortUsedForMaestro()` predicate, but `WCB.ino:6303-6315` walks `maestroConfigs[]` to skip Maestro ports on the broadcast path, so a Maestro port *is* treated as owned elsewhere. `configureMaestro` has no port-conflict guard at all (`WCB_Maestro.cpp:592-700`), so nothing on either side stops the collision. `docs/WLED_INTEGRATION.md:91` asserts WLED "can't claim a port already owned by HCR/MP3/Maestro/Kyber/PWM" — the Maestro half of that is not true of the code.

The Wizard filters these ports in its dropdown (`Wizard/app.js:2702-2704`), so this is reachable from the serial console / a chained config restore, not from the Wizard UI.

**Failure scenario.** Board has a DFPlayer on S2: `?DFP,S2` → dfpConfig{port=2, baud=9600}, port label "DFPlayer". Operator then adds lighting on the same port by mistake or by re-using a freed harness: `?WLED,3:W1S2:115200`. The guard passes (no DFP check), `wledReserveLocalPort(2, 115200, 3)` calls `updateBaudRate(2, 115200)` → `Serial2` divisor is reprogrammed live. From that instant every `;D,PLAY,1` writes a 10-byte 0x7E DFPlayer frame at 115200 into a module expecting 9600 — the DFPlayer stops responding entirely and `processDFPResponses()` never decodes another ONFIN/error frame, so any sequence chained on track-finish stalls. Nothing is printed except the normal "[WLED] WLED 3: local S2 at 115200 baud" success line, and `?DFP,STATUS` still reports the DFPlayer as configured on S2. Identical shape with `?MAESTRO,M1:W1S1:57600` followed by `?WLED,2:W1S1:115200` — the Maestro's UART is silently re-bauded to 115200 and every `;M1…` becomes line noise.

**Suggested fix.** Add `extern bool isSerialPortUsedForDFP(int port);` next to the existing `isSerialPortUsedForHCR` extern (WCB_WLED.cpp:25) and include it in the guard at line 326, updating the blocked-message text. For Maestro, either add an `isSerialPortUsedForMaestro(int)` predicate in WCB_Maestro.cpp (mirroring `isSerialPortUsedForWLED`) and call it from all four device modules, or inline the `maestroConfigs[]` scan the way WCB.ino:6303 does. Add the reciprocal WLED/HCR/MP3/DFP checks to `configureMaestro` in the same pass so the reservation is genuinely mutual.


---

## S3

### F-074 · S3 · `Code/WCB/command_timer.cpp:149`

**Timer delay is parsed with String::toInt() and never range-checked, so a negative delay wraps to ULONG-huge and is capped to the maximum 30-minute wait**

*Category:* `wraparound`

Line 149 converts the delay text with a signed parse into an unsigned variable, with no validity or sign check:

```cpp
149:  unsigned long parsedDelay = delayStr.toInt();
150:  unsigned long parsedDelayLimit = 1800000; // 30 Minutes
...
158:  if (parsedDelay > parsedDelayLimit) {
159:    parsedDelay = parsedDelayLimit;
160:    if (debugEnabled) { Serial.printf("⏱️ Delay capped to %i ms ..."); }
161:  }
```

`String::toInt()` returns a signed `long`. For `;t-500` (line 146 gives `delayStr = "-500"`), `toInt()` yields `-500`, which converts to `4294966796` on assignment to `unsigned long`. That is greater than `parsedDelayLimit`, so the clamp on line 158 turns it into the **maximum** 1 800 000 ms — the exact opposite of the "no wait / small wait" a user typing a negative number expects. The clamp notice on line 161 and the warning on line 154 are both gated behind `debugEnabled` (default `false`, `WCB.ino:175`), so at stock settings nothing is printed at all.

The same silence applies to any oversized delay: `;t3600000,cmd` is clamped to 30 minutes with no output on a default board. And because line 168 accumulates (`currentGroup.delayAfterPrevious += parsedDelay`) the per-token clamp is not a total cap, so consecutive tokens can still exceed 30 minutes.

**Failure scenario.** A user (or a generated/edited config chain) contains `;t-100,SOMECMD`, or a stray hyphen in `;t -100`. The board accepts the chain without a word of complaint, `commandTimerModeEnabled` goes true, and the command sits pending for 1 800 000 ms — half an hour. The board looks hung for that command and there is no console output explaining it unless `?DEBUG` happens to be on.

**Suggested fix.** Validate `delayStr` before converting: reject (or treat as 0, with an unconditional `Serial.printf`) any string that is empty or contains a character outside `0-9`, and make the over-limit clamp message on lines 154-162 print unconditionally rather than only under `debugEnabled`.

### F-075 · S3 · `Code/WCB/command_timer.cpp:189`

**In-chain ?STOP is positional and ignores its own delay: it aborts the sequence instantly at commands[0], and is an "Unknown command" anywhere else**

*Category:* `logic`

Line 189 tests only the current group's *first* command, and does so *before* the `waitingForNextGroup` / delay check on line 194:

```cpp
187:  CommandGroup &group = commandGroups[currentGroupIndex];
189:  if (checkForTimerStopRequest(group.commands[0])) {
190:    stopTimerSequence();
191:    return;
192:  }
194:  if (waitingForNextGroup) {
195:    if (millis() - lastGroupTime >= group.delayAfterPrevious) {
```

Two consequences, both verified:

1. **The delay is never honoured.** `checkForTimerStopRequest` (line 61) matches `?STOP` exactly. The moment `currentGroupIndex` reaches a group whose `commands[0]` is `?STOP`, the sequence is torn down on that very `loop()` tick — `group.delayAfterPrevious` is read only on line 195, which is unreachable past the `return` on line 191. `;t1000,A^;t5000,?STOP` stops immediately after A, not 5 s later.
2. **Any other position is dead.** A `?STOP` at index ≥ 1 in a group (e.g. `;t1000,A^?STOP`, which the parser puts in `commands[1]` via line 173) is never seen by line 189. It is enqueued and dispatched through `handleSingleCommand` → `processLocalCommand` (`WCB.ino:4395`), which has **no `STOP` branch** — the only `STOP` in that whole function is `?RTERM,STOP` at `WCB.ino:5112`. It falls to the final `else` at `WCB.ino:5346`, printing `Unknown command: STOP`, and the timer sequence keeps running.

The only other `?STOP` handler is `WCB.ino:6430`, which matches the *entire* incoming serial line, so a bare `?STOP` typed on its own works; it is only the in-chain form that is broken.

**Failure scenario.** An operator writes a self-terminating sequence such as `;t2000,LIGHTSON^;t30000,?STOP` expecting the chain to end 30 s later. `?STOP` lands in a group of its own as `commands[0]`, so the first `processCommandGroups()` call after LIGHTSON fires tears the sequence down immediately. Moving it to `;t30000,LIGHTSOFF^?STOP` puts it at `commands[1]`, where it is never checked and prints `Unknown command: STOP` instead — the same token behaves two different wrong ways depending on position.

**Suggested fix.** Move the stop test inside the fired-commands loop (scan every `cmd` in the group at line 203 and break out of the sequence when `checkForTimerStopRequest(cmd)` is true), so the group's `delayAfterPrevious` is honoured and any position works. Alternatively drop the line 189 special case entirely and add a `STOP` branch to `processLocalCommand` that calls `stopTimerSequence()`, giving one handler for all paths.

### F-076 · S3 · `Code/WCB/command_timer.cpp:201`

**lastReceivedViaESPNOW / inSequenceBody are set once at :201-202 but relied on across a 1 ms yield per command; the ESP-NOW receive callback on Core 0 rewrites them mid-group**

*Category:* `race`

Lines 199-211 save the two origin globals, overwrite them with the sequence's captured origin, enqueue every command in the group, then restore:

```cpp
201:  lastReceivedViaESPNOW = commandGroupsEspnowOrigin;
202:  inSequenceBody        = commandGroupsSequenceBody;
203:  for (const String &cmd : group.commands) {
207:      parseCommandsAndEnqueue(cmd, 0);
208:      vTaskDelay(pdMS_TO_TICKS(1));
209:  }
210:  lastReceivedViaESPNOW = _savedEspNowOrigin;
```

The comment at :196-198 is correct that `enqueueCommand` snapshots the global (`WCB.ino:1920-1923`) — which is exactly why the value must still be this sequence's origin at *each* snapshot, not just the first. But `vTaskDelay(pdMS_TO_TICKS(1))` on line 208 is an unconditional yield of at least one tick between every pair of enqueues, and both globals are plain `bool`s written with no synchronization from `espNowReceiveCallback` (`WCB.ino:3584`), which runs on the **WiFi task, Core 0**:
- `WCB.ino:3883-3884` — `lastReceivedViaESPNOW = !wizardOrigin; inSequenceBody = false;` (ETM command path)
- `WCB.ino:4159-4160` — `lastReceivedViaESPNOW = true; inSequenceBody = false;` (normal command path)

Being on the other core, those writes need no yield to land — but the 1 ms yield per command guarantees a wide window regardless. Whatever the callback leaves behind is what `enqueueCommand` snapshots for every remaining command in the group, and line 210-211 then stamps a stale value back over the callback's write.

The consumer is real: `sendESPNowMessage` gates its broadcast on this flag — `WCB.ino:2275`, `if (target == 0 && lastReceivedViaESPNOW) return;` — reached from `processBroadcastCommand` at `WCB.ino:6356`, using the per-item snapshot restored at `WCB.ino:7510-7511`. Same for `inSequenceBody`, consumed by the recall fan-out gate at `WCB.ino:6119`.

**Failure scenario.** A locally-authored timer sequence (`commandGroupsEspnowOrigin == false`) fires a group of several broadcast commands, which must reach the other WCBs. Between command 1 and command 2, during the `vTaskDelay(1)` on line 208, a command packet from another board arrives; `espNowReceiveCallback` sets `lastReceivedViaESPNOW = true` at `WCB.ino:4159`. Command 2 onward are enqueued with `espnowOrigin = true`, so at drain `sendESPNowMessage` returns at `WCB.ino:2275` and they never leave this board — half the timer sequence runs locally only, silently. Conversely, a peer-triggered sequence (`origin == true`) whose flag is cleared re-broadcasts its remaining commands to the mesh; and an `inSequenceBody` clobbered to false lets a nested `;C` in a timer group re-fan the recall trigger to the whole fleet (`WCB.ino:6119`), firing the sequence twice on every board.

**Suggested fix.** Stop routing origin through globals across a yield. Pass the origin explicitly — e.g. an `enqueueCommandWithOrigin(cmd, sourceID, espnowOrigin, sequenceBody)` overload used by this loop — or, at minimum, re-assign `lastReceivedViaESPNOW`/`inSequenceBody` from `commandGroupsEspnowOrigin`/`commandGroupsSequenceBody` at the top of each for-loop iteration (immediately before `parseCommandsAndEnqueue`) so a yield cannot leave a foreign value in place for the next snapshot.

### F-077 · S3 · `Code/WCB/WCB_HCR.cpp:142`

**beginHCR() arms the auto-poll timer with a 0 sentinel that a signed millis() compare reads as "in the future" past 24.8 days uptime**

*Category:* `wraparound`

`beginHCR()` ends with:

```
  _hcrNextPollMs = 0;  // first tick will fire poll immediately if pollSec>0
```

and `processHCRTick()` gates on a signed difference (line 160):

```
  if (hcrConfig.pollSec > 0 && (long)(now - _hcrNextPollMs) >= 0) {
```

`long` is 32-bit on both ESP32 targets, so with `_hcrNextPollMs == 0` the expression is `(long)millis()`. Once `millis() >= 0x80000000` (24.855 days of uptime) that cast is **negative**, the condition is false, and the auto-poll never fires — for up to another 24.85 days, until `millis()` wraps through 0.

The sentinel is only safe because every other write of this variable is a real deadline: line 163 `_hcrNextPollMs = now + pollSec*1000` and line 579 `_hcrNextPollMs = millis()`. Line 142 is the only place a raw 0 is stored, and it is exactly the value the signed compare cannot represent as "now or earlier" late in the millis() epoch. The adjacent comment ("first tick will fire poll immediately") is therefore false on a long-running board.

Note this is not a boot-time problem — setup() calls `beginHCR()` at `WCB.ino:7146` when millis() is tiny. It is a *reconfiguration*-time problem: `hcrReservePort()` calls `beginHCR()` at WCB_HCR.cpp:520 whenever `?HCR,PORT,...` is processed.

**Failure scenario.** A WCB left powered for 25+ days (bench/static display). Operator issues `?HCR,PORT,S1:9600` (re-seating the HCR, or fixing a baud). `beginHCR()` sets `_hcrNextPollMs = 0`; `(long)(3000000000 - 0)` = -1294967296, so the `<QD>` auto-poll never runs again. `?HCR,STATUS` and the Wizard's HCR Status modal keep reporting the emotion/duration/playing values frozen at whatever was last parsed, with `age` climbing forever. A full Wizard config push happens to mask it (printHCRBackup emits `?HCR,POLL,<n>` right after `?HCR,PORT`, and line 579 re-arms with millis()), so the bug only shows on a bare hand-typed PORT command.

**Suggested fix.** Replace line 142 with `_hcrNextPollMs = millis();` — the same value line 579 already uses to mean "poll now" — and drop the 0 sentinel.

### F-078 · S3 · `Code/WCB/WCB_HCR.cpp:171`

**debugHCR status throttle uses a non-wrap-safe millis() compare — floods the HCR port every loop pass for up to an hour before the millis() wrap**

*Category:* `wraparound`

```
  if (debugHCR) {
    static unsigned long _dbgNext = 0;
    if (now >= _dbgNext) {
      uint32_t everyMs = (hcrConfig.pollSec > 0 ? hcrConfig.pollSec : 2) * 1000UL;
      if (everyMs < 2000UL) everyMs = 2000UL;
      _dbgNext = now + everyMs;
```

The comparison is a plain unsigned `>=` against an absolute deadline, not the wrap-safe signed-difference form used four lines above it at line 160. When `now + everyMs` overflows 32 bits, `_dbgNext` wraps to a small value while `now` is still near 0xFFFFFFFF, so `now >= _dbgNext` is true on **every** subsequent loop pass — and each pass re-computes the same wrapped deadline, so the throttle never re-arms. The flood runs for `everyMs` milliseconds (2 s at the default poll, up to 3,600,000 ms with `?HCR,POLL,3600`) until millis() actually wraps.

Each flooded iteration runs `Serial.print(...)` plus `printHCRStatus()`, and `printHCRStatus()` is not free: it calls `_hcr->getVolume()` three times, each of which transmits a `<QV?>\n` frame (`Code/WCB/src/HumanCyborgRelationsAPI/hcr.cpp:565`). That is ~18 bytes pushed at the HCR port per loop iteration against a 9600-baud link (960 B/s), so the TX path saturates and `write()` blocks — on S3–S5 that is EspSoftwareSerial's blocking bit-banged write inside loop().

**Failure scenario.** A board left running with `?DEBUG,HCR,ON` and `?HCR,POLL,3600` reaches 49.7 days uptime. In the final hour before the millis() wrap, `_dbgNext` is a wrapped small value while `now` is near 0xFFFFFFFF, so every loop pass prints a ~140-char `[HCR-DBG] status [HCR:...]` line to Serial and pushes three `<QV?>` frames at the HCR. The UART backpressures, `loop()` throttles down to serial speed for an hour, and queued ESP-NOW commands drain at that rate. Preconditions are narrow (debugHCR is not persisted — `WCB.ino:260`), but the compare is wrong independently of them.

**Suggested fix.** Use the same wrap-safe form as line 160: `if ((long)(now - _dbgNext) >= 0) { ... }`.

### F-079 · S3 · `Code/WCB/WCB_HCR.cpp:279`

**;H,FN,... and ;H,STOP never cancel an in-flight fade, so a later fade step stomps them (kills a just-started track)**

*Category:* `logic`

Every readable audio verb cancels the channel's fade before acting: `;H,PLAY` (line 348 and 353), `;H,STOPWAV` (line 361), `;H,VOL` (line 383), `;H,VOLUP/VOLDN` (line 407) all call `hcrCancelFade(c)` first.

The numeric FN path does not:

```
    if (fn == 14) {
      ...
      if (_hcr) { _hcr->PlayWAV(chan, track); _hcr->update(); }
    } else if (_hcrPort && hcrCodec.emit(*_hcrPort, (uint8_t)fn, chan, track)) {
```

No `hcrCancelFade()` anywhere in the block (lines 263-286), even though fn 14 = PlayWAV, fn 16 = StopWAV and fn 17/18/19 = SetVolume/step are precisely the actions the readable verbs guard. `;H,STOP` (line 306, `_hcr->Stop()`) has the same omission.

`HcrFade` state is per-channel and survives independently (`WcbCmd/src/WcbHcrFade.h:47`), and `HcrFade::tick()` unconditionally emits on completion — including `codec.emit(out, 16, ch, 0)` (StopWAV) and a volume restore when `stopAtEnd` is set (`WcbCmd/src/WcbHcrFade.cpp:49-56`). So a FADEOUT armed by `;H,FADEOUT` (WCB_HCR.cpp:419, `stopAtEnd=true`) will fire its StopWAV on whatever is playing when its timer expires, regardless of what the FN path did in between. This is the RC-Controller's command convention (per the comment at line 262), so it is the path an external controller uses.

**Failure scenario.** Crossfade, the use case documented in the header at WCB_HCR.h:20. `;H,FADEOUT,A,8` arms an 8-second ramp on channel A with stopAtEnd=true and restoreTo=cur. Two seconds later the RC-Controller sends `;H,FN,14,1,7` to start file 0007 on channel A. PlayWAV is emitted and the track starts. At t=8 s `hcrStepFades()` → `HcrFade::tick()` completes the stale fade: SetVolume(A,0), StopWAV(A), SetVolume(A,restoreTo) — the newly started track is silently killed six seconds in. The identical sequence written as `;H,PLAY,A,7` works, because line 353 cancels the fade.

**Suggested fix.** In the FN block, cancel the fade for the affected channel before emitting for the audio-channel fns: for fn 14/16 and fn 17 (`chan` 0-2, or all three when `chan == 3`), and for fn 18/19 (all three), call `hcrCancelFade()`. Add `hcrCancelFade(0); hcrCancelFade(1); hcrCancelFade(2);` to the `;H,STOP` branch at line 306.

### F-080 · S3 · `Code/WCB/WCB_HCR.cpp:702`

**Status/GET volume fields report the value from the previous query — the first ?HCR,STATUS or ?HCR,GET,VOL after boot always reads 0**

*Category:* `logic`

`printHCRStatus()` builds the `[HCR:...]` line from

```
    _hcr->getVolume(CH_V), _hcr->getVolume(CH_A), _hcr->getVolume(CH_B));
```

and `?HCR,GET,VOL,<ch>` does the same at line 601-602. `HCRVocalizer::getVolume()` (`Code/WCB/src/HumanCyborgRelationsAPI/hcr.cpp:559-576`) is not an accessor — it **sends** `<QV?>` and then returns the *currently cached* `Volume_V/A/B`:

```
    String msg = "QV" + ToString((char) channel[ch]);
    sendCommand(msg);
    float volume = 0;
    ... volume = Volume_A; ...
    return volume;
```

Those members are only written when the reply is parsed later, one RX byte per `processHCRTick()` pass (`hcr.cpp:331-345`, reached via `_hcr->update()` at WCB_HCR.cpp:155). Critically, the bulk `<QD>` reply does **not** carry volumes — the `DF` branch (`hcr.cpp:346-359`) parses only the 4 emotions, override, musing, files, duration and chv/cha/chb — so auto-poll never refreshes them. And `WcbHCR::_init()` seeds them to zero (`WCB_HCR.cpp:61`: `Volume_V = Volume_A = Volume_B = 0;`).

Result: the reported volumes are always one query stale, and the first read after every `beginHCR()` reports 0. The module has an accurate value available (`hcrCodec.getVol(ch)`, the commanded shadow the fades already trust — WCB_HCR.cpp:91-94) and does not use it. Secondary effect: `printHCRStatus()` is not a pure printer — it transmits three frames to the HCR every time it is called, including from the debug tick at line 176.

**Failure scenario.** Board boots, HCR configured on S3, volumes set to 50/50/50 on the device. User clicks "HCR Status" in the Wizard (`Wizard/app.js:11920` sends `?HCR,STATUS`, `:11894` renders `Volume V:.. A:.. B:..`). The first render shows `Volume V:0 A:0 B:0` — the HCR reads as fully muted. The modal repolls at 3 s (`Wizard/app.js:11867`) and the second render shows 50/50/50. From a serial terminal there is no repoll, so `?HCR,GET,VOL,A` must be typed twice to get a real number, and any volume changed between two invocations is reported one query behind.

**Suggested fix.** Report `hcrCodec.getVol(ch)` (falling back to `_hcr->getVolume(ch)` only if the shadow is unseeded) for the vV/vA/vB fields and for `?HCR,GET,VOL`, or issue the three `<QV?>` queries once from `beginHCR()`/each auto-poll so the cache is warm before it is read. Either way, note in the comment that `getVolume()` transmits.

### F-081 · S3 · `Code/WCB/WCB_Maestro.cpp:118`

**Subroutine number is never range-checked to 0-127, so ;M<id>128..255 emits a Pololu frame with a data byte that has bit 7 set**

*Category:* `protocol`

`sendMaestroCommand(uint8_t maestroID, uint8_t scriptNumber)` passes `scriptNumber` straight through:

```
        uint8_t command[4]; WcbMaestro::buildSubroutineFrame(maestroID, scriptNumber, command);
```

and `WcbMaestro::buildSubroutineFrame` (WcbCmd/src/WcbMaestro.cpp) does no validation at all:
```
    out[0] = POLOLU_LEAD; out[1] = id; out[2] = CMD_RESTART_SUB; out[3] = seq; return 4;
```

In the Pololu protocol every byte after the 0xAA lead must have bit 7 clear; a data byte >= 0x80 is not a valid data byte and the frame is discarded / re-syncs the device parser. The caller admits 0-255: WCB.ino:6151 `if (seq < 0 || seq > 255) return;` and WCB.ino:6172 `if (dev >= 0 && dev <= 9 && seq >= 0 && seq <= 255)`. Neither the caller nor `sendMaestroCommand` clamps to 127.

Every other builder in the shared library does validate its range (e.g. `buildSetTarget` returns 0 for `ch > 127 || target > 16383`), and `sendMaestroServoVerb` relies on exactly that ("WcbMaestro::build validates every arg (returns 0 on a bad verb/range), so a malformed verb never reaches the servo wire", WCB_Maestro.cpp:250). The subroutine-trigger path bypasses `build()` entirely and inherits no such check. The bad value is also propagated over the mesh unchecked at WCB_Maestro.cpp:145 (`"M" + String(maestroID) + String(scriptNumber)`), so every remote board reproduces the same corrupt frame.

**Failure scenario.** Operator sends `;M1200` (Maestro 1, subroutine 200) or the equivalent `;M1,200`. Both parse cleanly and `sendMaestroCommand(1, 200)` puts AA 01 27 C8 on the wire. 0xC8 has bit 7 set, so the Maestro rejects the frame: the subroutine never runs and nothing is logged locally or on any forwarded board — it looks like a dead Maestro rather than a rejected command.

**Suggested fix.** Reject out-of-range subroutine numbers at the entry to `sendMaestroCommand()` (`if (scriptNumber > 127) { log; return; }`), matching the way `sendMaestroServoVerb()` relies on `WcbMaestro::build()` returning 0. Tightening WCB.ino:6151/6172 to `seq > 127` as well keeps the error message at the parse site.

### F-082 · S3 · `Code/WCB/WCB_Maestro.cpp:158`

**A slot holding reserved Maestro id 9 (creatable via ?KYBER) permanently shadows the target-9 "all local Maestros" fan-out**

*Category:* `logic`

WCB_Maestro.h:14-16 states the invariant the routing code depends on: "Maestro device IDs are 1-8; id 9 (all local) and id 0 (all Maestros) are RESERVED routing targets, never stored as a device." `sendMaestroCommand()` relies on it structurally — the reserved-target branches for id 0 (line 160) and id 9 (line 190) sit AFTER the slot-match loop and after `if (handled) return;` (line 158), so they only run because no slot can ever carry those ids.

Both entry points inside this file do enforce it (`configureMaestro` line 561: `if (maestroID < 1 || maestroID > 8)`, `maestroAutoAddRemote` line 724: `if (maestroID < 1 || maestroID > 8) return false;`) — but they are not the only writers of `maestroConfigs[]`. `storeKyberSettings()` also creates slots (WCB_Storage.cpp:1277-1290, using this file's `findSlotByMaestroIDPortTarget` / `findEmptySlot`), and its range check is WCB_Storage.cpp:1248: `if (maestroID >= 1 && maestroID <= 9 && ...` — id 9 is admitted.

Once such a slot exists, `sendMaestroCommand(9, seq)` matches it in the loop at line 108, writes one frame addressed to a non-existent Pololu device 9, sets `handled = true`, and returns at line 158 — the target-9 branch at line 190 is never reached again. Same for the verb path: the loop at WCB_Maestro.cpp:298 and `if (handled) return;` at line 326 shadow the dev-9 branch at line 348. The routing code has no defence of its own invariant.

**Failure scenario.** Operator configures the sabre stream with `?KYBER,LOCAL,S2,M9:W1S1:57600` (a natural reading of "9 = all local Maestros"). The command is accepted and persists a maestroConfigs slot with maestroID 9. From then on `;M91` no longer fires every local Maestro on the board — it silently writes AA 09 27 01 to S1, which no device answers. The behaviour survives reboot (saveMaestroSettings) and is not visible in `?MAESTRO,LIST` as anything unusual.

**Suggested fix.** Make the routing code enforce the invariant it documents: skip reserved ids in the slot-match loops, i.e. add `if (maestroID == 0 || maestroID == 9) { /* fall through to the reserved-target branches */ }` before the loop at WCB_Maestro.cpp:108 (and :298), so a stray slot can never shadow them. Separately tighten WCB_Storage.cpp:1248 to `maestroID <= 8`.

### F-083 · S3 · `Code/WCB/WCB_Maestro.cpp:170`

**The ;M0 broadcast's legacy fallback tests isMaestroConfigured(WCB_Number) instead of "no local Maestro configured", writing a phantom device-N frame to Serial1**

*Category:* `logic`

In the `maestroID == 0` broadcast branch, after writing to every configured local port (lines 161-168), the code adds:

```
    if (!isMaestroConfigured(WCB_Number)) {
      uint8_t command[4]; WcbMaestro::buildSubroutineFrame(WCB_Number, scriptNumber, command);
      Serial1.write(command, sizeof(command));
    }
```

`isMaestroConfigured(x)` is `findSlotByMaestroID(x) >= 0` (WCB_Maestro.cpp:83) — it asks "is a Maestro with Pololu id == this board's WCB NUMBER configured", not "does this board have any local Maestro". Those differ whenever the site does not follow the legacy "Maestro id == WCB number" convention. The intent is stated in the sendMaestroServoVerb comment at line 329-331: "including its default-Maestro-on-S1 fallback so an un-mapped board still actuates its S1 Maestro" — but a board mapped as `M5:W3S3` is not un-mapped, yet still takes the fallback.

The correct test is used 20 lines below, in the target-9 branch (lines 191-207), which tracks `bool wroteLocal` across the slot scan and only falls back `if (!wroteLocal)`. The dev-0 branch simply never got that treatment.

Same defect in the verb path at WCB_Maestro.cpp:336-337 (`if (!isMaestroConfigured(WCB_Number)) writeVerbFrameTo(Serial1, WCB_Number, payload);`), immediately above the correct `wroteLocal` version at line 352.

**Failure scenario.** WCB3 hosts one Maestro with Pololu id 5 on S3 (`?MAESTRO,M5:W3S3:57600`), and its S1 carries the MP3 Trigger. Any `;M0<seq>` all-Maestros broadcast writes the correct AA 05 27 <seq> to S3, then — because no slot has id 3 — also writes AA 03 27 <seq> to S1, into the MP3 Trigger. The stray bytes are sent on every broadcast, forever, and nothing logs them.

**Suggested fix.** Replace the fallback test in both dev-0 branches with the `wroteLocal` pattern already used by the target-9 branches: track whether the preceding loop wrote to any local port and only do the `Serial1` legacy write when it wrote none.

### F-084 · S3 · `Code/WCB/WCB_Maestro.cpp:610`

**configureMaestro accepts baud 0, which updateBaudRate() then rejects — the slot persists a baud the port never adopts**

*Category:* `logic`

`configureMaestro()`'s validator explicitly whitelists zero:

```
    if (!(baudRate == 0 || baudRate == 110 || baudRate == 300 || ...
```

but the function it then calls does not. `updateBaudRate()` (WCB_Storage.cpp:155-160) has no `0` in its whitelist and bails with `Serial.println("Invalid baud rate"); return;`. So for `:0` the sequence at WCB_Maestro.cpp:658-660 prints "Invalid baud rate", leaves the port at its previous rate, and execution continues to line 681 which commits `maestroConfigs[slot].baudRate = 0` and then line 684 reports success: `"✓ Maestro %d: Local S%d at %d baud"` → "at 0 baud".

The bogus value is then durable and self-propagating: `saveMaestroSettings()` persists it (WCB_Storage.cpp:1982), and `printMaestroBackup()` (WCB_Maestro.cpp:947) emits `?MAESTRO,M<id>:W<n>S<p>:0`, which restore re-accepts under the same whitelist. Nothing anywhere treats 0 as "leave the baud alone" — if that were the intent, line 659 would have to skip the `updateBaudRate` call, not make it and ignore the failure.

**Failure scenario.** A restore chain or a hand-typed `?MAESTRO,M1:W1S1:0` (or a Wizard field left blank that serialises as 0) is pushed. The board prints "Invalid baud rate" followed immediately by "✓ Maestro 1: Local S1 at 0 baud". S1 stays at whatever it was — 9600 by default — while `?MAESTRO,LIST` and the config backup both claim the Maestro is configured, so a Maestro wired for 57600 silently never responds and the config that would explain it reads as valid.

**Suggested fix.** Drop `baudRate == 0 ||` from the whitelist at WCB_Maestro.cpp:610 so the entry is rejected with "Invalid baud rate" and no slot is written — matching `updateBaudRate()`'s contract and the `?KYBER` path (WCB_Storage.cpp:1251), which requires 9600-115200.

### F-085 · S3 · `Code/WCB/WCB_Maestro.cpp:954`

**printMaestroBackup() picks a remote slot's port from kyberTargets by maestroID alone, ignoring targetWCB and breaking on the first match**

*Category:* `logic`

A remote slot stores no port (`maestroConfigs[slot].serialPort = 0`, WCB_Maestro.cpp:689 and :751), so the backup has to invent the S-field:

```
            int targetPort = 1;  // fallback
            for (int j = 0; j < MAX_KYBER_TARGETS; j++) {
                if (kyberTargets[j].enabled &&
                    kyberTargets[j].maestroID == maestroConfigs[i].maestroID) {
                    targetPort = kyberTargets[j].targetPort;
                    break;
                }
            }
```

The match key omits `kyberTargets[j].targetWCB` (the field exists — WCB_Storage.h:99) and the loop `break`s on the first hit. Two things follow:
1. `maestroAutoAddRemote()` deliberately creates one proxy PER HOST for the same id (WCB_Maestro.cpp:715-722: "each advertising host needs its OWN proxy"). Both proxies then emit the SAME port in the backup, taken from whichever kyberTargets row happens to be first.
2. When the same id is also hosted LOCALLY (explicitly supported — same comment block), the local slot's kyberTargets row is reached first (reconcileKyberTargetsFromMaestroConfigs, WCB_Storage.cpp:1063-1064, matches by id only too), so the remote line reports this board's local S-port as the remote board's port.

This is the one lookup in the file that both matches on the id alone and stops at the first hit — every other slot lookup here either uses the full `(id, serialPort, remoteWCB)` key or fans out.

**Failure scenario.** WCB1 hosts Maestro id 2 on S3 and WDP also auto-adds a proxy for id 2 on WCB4. `?MAESTRO,LIST` / the config backup pulled by the Wizard emit `?MAESTRO,M2:W1S3:57600` and `?MAESTRO,M2:W4S3:57600` — the second line reports S3 for WCB4, a port WCB4 may not even use for that Maestro. The operator reading the backup (or the Wizard's board table) is told the wrong physical wiring for the remote board, and printMaestroSettings() (WCB_Storage.cpp:2047-2055) prints the same wrong port.

**Suggested fix.** Match the full destination: `kyberTargets[j].enabled && kyberTargets[j].maestroID == maestroConfigs[i].maestroID && kyberTargets[j].targetWCB == maestroConfigs[i].remoteWCB`. When there is no exact match, emit S1 as today (the field is documentary — configureMaestro discards it for remote slots) rather than borrowing another board's port.

### F-086 · S3 · `Code/WCB/WCB_MP3.cpp:236`

**?MP3/?DFP,ONERR slices the key out of the untrimmed args while the prefix test uses the trimmed copy — a leading space stores a corrupt key**

*Category:* `logic`

`configureMP3` trims into a local before testing the prefix but then indexes the *original* parameter:

```
String a = args;
a.trim();
String aUpper = a; aUpper.toUpperCase();
...
if (aUpper.startsWith("ONERR,")) {
    String key = args.substring(6);
```

`aUpper` is derived from the trimmed `a`, but `args.substring(6)` assumes "ONERR," occupies `args[0..5]`. Any leading whitespace in `args` shifts the slice by that many characters. `args` is not trimmed upstream: `WCB.ino:4414` builds it as `message.substring(firstComma + 1)` — the outer line is trimmed, but whitespace *after* the comma survives.

`WCB_DFP.cpp:187` has the identical line (`String key = args.substring(6);` under `aUpper.startsWith("ONERR,")` at `:186`). The subsequent `key.trim()` cannot repair it because the lost/extra characters are not whitespace.

**Failure scenario.** `?MP3, ONERR,siren` (one space after the comma — legal-looking, and easy to produce in a hand-edited config chain). `args` = " ONERR,siren", so `a`/`aUpper` = "ONERR,siren" and the prefix test passes, but `args.substring(6)` = ",siren". `key.trim()` leaves ",siren", which is 6 chars so it passes the 1-23 length check, and the board prints the reassuring "[MP3] Error callback → ;C,siren" and persists ",siren" to NVS. When the MP3 Trigger later reports 'E', `onError` calls `recallCommandSlot(",siren")`, which misses and prints "No command stored under key: ',siren'". The error handler is silently dead, and it round-trips through backup/restore in that broken state (`printMP3Backup` :424).

**Suggested fix.** Slice from the trimmed copy that the prefix test actually matched: `String key = a.substring(6);` in both WCB_MP3.cpp:236 and WCB_DFP.cpp:187 (`a` preserves case, so nothing else changes).

### F-087 · S3 · `Code/WCB/WCB_MP3.cpp:242`

**ONERR accepts keys up to 23 chars, but a stored-command key is an NVS key capped at 15 — a 16-23 char callback can never resolve**

*Category:* `nvs`

The ONERR validator bounds the key by the size of the storage struct field, not by what the recall path can actually look up:

```
} else if (key.length() == 0 || key.length() > 23) {
    Serial.println("[MP3] ONERR: key must be 1-23 characters");
```

(23 is `sizeof(mp3Config.onErrCmd) - 1`, `WCB_MP3.h:21`.) The key is consumed by `recallCommandSlot()` (`WCB_MP3.cpp:51`), which uses it directly as an ESP32 Preferences KEY:

```
preferences.begin("stored_cmds", true);
String recalledCommand = preferences.getString(key.c_str(), "");
```
(`WCB_Storage.cpp:557-558`)

An NVS key name is limited to 15 characters; anything longer fails inside `nvs_get_str`/`nvs_set_str` and Arduino's `Preferences` returns the default with no visible error. So a 16-23 character key passes this validator, prints a success line, and persists — but can never be stored (`saveStoredCommandsToPreferences`, `WCB_Storage.cpp:625-663`, has no length check either) nor recalled. `WCB_DFP.cpp:193` carries the identical bound.

**Failure scenario.** `?Cmp3_error_recovery,;A,STOP` — the intended stored sequence (20 chars) silently fails to write to NVS. `?MP3,ONERR,mp3_error_recovery` is then accepted and prints "[MP3] Error callback → ;Cmp3_error_recovery". Both `?MP3,LIST` and the config backup show the callback as configured. When the MP3 Trigger reports 'E', `onError` fires and the only output is "No command stored under key: 'mp3_error_recovery'". Nothing in the UI or the backup ever indicates the key length is the problem.

**Suggested fix.** Bound the ONERR key at 15 in both WCB_MP3.cpp:242 and WCB_DFP.cpp:193 ("key must be 1-15 characters (NVS key limit)"), and — the real fix — add the same 15-char check with an explicit error to `saveStoredCommandsToPreferences` (WCB_Storage.cpp:637) so an unusable key is rejected at the point it is created rather than at the point it silently fails to fire.

### F-088 · S3 · `Code/WCB/WCB_MP3.cpp:323`

**Moving the MP3 Trigger to a different port leaves the old port stuck at the MP3's baud, while ?MP3,CLEAR resets it**

*Category:* `logic`

`configureMP3`'s release-old-port block undoes the broadcast reservation and the serial label but not the baud rate:

```
if (mp3Config.configured && mp3Config.serialPort > 0 &&
    mp3Config.serialPort != (uint8_t)serialPort) {
  uint8_t oldPort = mp3Config.serialPort;
  ... serialBroadcastEnabled[oldPort - 1] = true ...
  ... blockBroadcastFrom[oldPort - 1] = false ...
  saveSerialLabelToPreferences(oldPort, "");
  Serial.printf("  ✓ Released S%d (old MP3 port)\n", oldPort);
}
```

The module's other release path — `clearMP3Config()` at `WCB_MP3.cpp:176` — does `updateBaudRate(freedPort, 9600)` and announces "✓ Reset S%d baud rate to 9600". So the same file restores the baud on CLEAR but not on MOVE, and the "Released S%d" message implies a full release.

This matters only for MP3, because `?MP3` is the one of the two that accepts a non-default baud (9600 or 38400, `WCB_MP3.cpp:287`); `?DFP` is fixed at 9600 (`WCB_DFP.cpp:248`). `updateBaudRate` persists — it writes `baudRates[port-1]` and the `serial_baud`/`Serial<n>` NVS key (`WCB_Storage.cpp:166-176`) — so the stale rate survives reboots. (`hcrReservePort`, WCB_HCR.cpp:475-490, has the same shape, so this is a pattern rather than a one-off, but the internal inconsistency and the user-visible consequence are real here.)

**Failure scenario.** `?MP3,S1:38400:V20` (Serial1 persisted to 38400), then the user rewires and issues `?MP3,S2:9600:V20`. The board prints "✓ Released S1 (old MP3 port)" and re-enables broadcast output on S1 — at 38400 baud. Every broadcast command now goes out S1 at 38400 instead of the 9600 the rest of the fleet uses, so whatever is plugged into S1 receives framing garbage. It survives reboot (persisted in NVS), and the only cure is a manual `?BAUD,S1,9600`, which nothing tells the user to do.

**Suggested fix.** Add `updateBaudRate(oldPort, 9600);` inside the release block (after the label clear, before the "Released S%d" print) so the MOVE path matches `clearMP3Config()` at :176 — and print the same "✓ Reset S%d baud rate to 9600" line so the user can see it happened.

### F-089 · S3 · `Code/WCB/WCB_OTA.cpp:104`

**otaBegin's supersede-then-reject path strands the USB baud at 921600 with no session to restore it**

*Category:* `error-handling`

`otaBegin` starts with `if (ota.active) otaTeardown();   // supersede any stale session` (`:104`). `otaTeardown` (`:75-86`) deliberately does NOT call `otaRestoreLocalBaud()` — correct for a successful supersede. But `otaBegin` can then reject and `return false` at three points (`:110` chip-family mismatch, `:116` no inactive partition, `:121` size out of range) and at `:128` (`esp_ota_begin` failed), each leaving `ota.active == false` while `s_otaXferBaud` is still set.

Every other restore path is gated on the session: `checkOtaTimeout()` (`:96-100`) only runs `if (ota.active && ...)`; `otaWrite`'s restores (`:151`, `:159`) and `otaEnd`'s (`:181`, plus the driver's `:315`) all require an active session. `otaAbortSession` (`:88-94`) restores unconditionally but only runs on an explicit `?OTALOCAL,ABORT` / OTA_ABORT packet / the `ota.active`-gated timeout.

This is exactly the state the BAUD guard's own comment declares impossible — `:241-244`: "REQUIRE an active session: the only restore paths are tied to the session ... A bump with no session in flight would have nothing to undo it and could strand the baud, so reject it." The guard closes the entry door but `otaBegin` re-opens it. Confirmed there is no other caller: `grep otaRestoreLocalBaud` yields only `:53, :93, :151, :159, :181, :315`.

**Failure scenario.** USB OTA of WCB2 is running with the baud bumped to 921600 (`s_otaXferBaud = 921600`). A relay-OTA `?OTA,BEGIN,2,<sess>,<size>,1` from another board (or a second shared-port tab issuing `?OTALOCAL,BEGIN` with the wrong family — the Wizard's serial-hub lets two tabs share one port) reaches WCB2. `otaBegin` tears the live session down at :104, then rejects at :110 for the family mismatch. `ota.active` is now false, so `checkOtaTimeout()` never fires. WCB2's UART0 stays at 921600 indefinitely; the terminal is unreadable at 115200 and the board only recovers on a power cycle or a lucky `?OTALOCAL,ABORT` sent at the bumped rate.

**Suggested fix.** Call `otaRestoreLocalBaud()` on each `return false` in `otaBegin` (after the supersede teardown), or — simpler and covers every future path — relax `checkOtaTimeout()` to also restore an orphaned bump: `if (!ota.active && s_otaXferBaud) otaRestoreLocalBaud();`.

### F-090 · S3 · `Code/WCB/WCB_OTA.cpp:284`

**A base64-decode failure on ?OTALOCAL,DATA emits no [OTA:NAK,<cursor>] marker, so the host burns its full 6 s timeout per bad chunk**

*Category:* `protocol`

The local USB driver's DATA branch:

```c
284:    if (rc != 0) {
285:      Serial.printf("[OTA] DATA base64 error %d (chunk too big? max %u B decoded)\n",
286:                    rc, (unsigned)sizeof(s_otaChunk));
287:      return;
288:    }
```

Every other DATA outcome emits a machine-readable marker — `[OTA:ACK,<cursor>]` (`:299`) on success and `[OTA:NAK,<cursor>]` (`:293`) on a gap/dup, documented as the host's resync point at `docs/WCB_OTA_TECHNICAL.md:120`. The base64-error path emits only a human line, and critically it is prefixed `"[OTA] "` (space) not `"[OTA:"` (colon).

The host's sentinel match is a plain substring test — `Wizard/app.js:5248`: `if (line.includes(sentinel)) finish();` with `sentinel = '[OTA:'` from `Wizard/app.js:1609`. `"[OTA] DATA base64 error…"` does not contain `[OTA:`, so `sendAndCollect` never finishes early and blocks for the full 6000 ms, then falls into the stall branch (`Wizard/app.js:1626`) with `cursor = -1`, so it cannot even resync — it just resends the same chunk after a backoff.

The same omission exists on the two usage-error lines (`:266` and `:277`), which also start `"[OTA] "`.

This is reachable in normal operation, not just on malformed input: the transfer runs at 921600 (`OTA_LOCAL_DEFAULT_BAUD` bumped via the BAUD subcommand, `:249-259`), and the doc itself flags a marginal bridge at that rate (`docs/WCB_OTA_TECHNICAL.md:112`). A single dropped or mangled character makes the base64 length non-multiple-of-4 or introduces an invalid character, and `mbedtls_base64_decode` returns `MBEDTLS_ERR_BASE64_INVALID_CHARACTER`.

**Failure scenario.** USB OTA at 921600 over a marginal CH340 bridge. One byte of a 1368-char DATA line is dropped. `mbedtls_base64_decode` returns non-zero; the board prints `[OTA] DATA base64 error -0x002C` and nothing else. The Wizard sits for the full 6 s, gets no `[OTA:` line, cannot resync (cursor -1), backs off and resends. On a link that corrupts a line every few hundred chunks, a ~1.9 MB image (1850 chunks) spends minutes idling, and a link that corrupts more often walks straight into the 60-stall cap and aborts with "OTA stalled at N (no progress after retries — board not keeping up)" — a message that blames the board's loop() rather than the serial link.

**Suggested fix.** Emit the resync marker on every DATA failure path, not just the gap/dup one: add `Serial.printf("[OTA:NAK,%u]\n", otaWrittenOffset());` after the base64 error print at :286 (and after the usage errors at :266/:277 with the corresponding `[OTA:BEGIN,ERR,…]` / `[OTA:NAK,…]` forms), so the host fails or resyncs immediately instead of waiting out the timeout.

### F-091 · S3 · `Code/WCB/WCB_OTA.cpp:413`

**Relay OTA DATA ACK reports OTA_ST_OK on the very frame whose write tore the session down (pre-write snapshot)**

*Category:* `error-handling`

`handleOtaDataPacket` captures the session flag BEFORE the write:

```c
400:  const bool inSession = (ota.active && pkt.sessionId == ota.sessionId);
401:  if (inSession) ota.lastActivityMs = millis();
...
405:  otaWrite(pkt.sessionId, pkt.fragOffset, pkt.data, len);
...
413:  sendOtaAck(pkt.sourceWCB, pkt.sessionId, inSession ? OTA_ST_OK : OTA_ST_ERR,
414:             otaWrittenOffset());
```

The comment directly above line 406 says "Report the session's REAL state. If a failed write already tore the session down (image overrun / esp_ota_write error / idle abort), otaWrittenOffset() reads 0 ... OTA_ST_ERR on a no-longer-active session makes it fail loudly instead." That is not what the code does — `inSession` is a snapshot taken before `otaWrite`, so it is still `true` for the frame that caused the teardown.

`otaWrite` tears the session down on both failure paths it owns (`WCB_OTA.cpp:147-153` image overrun → `otaTeardown()`, and `:155-161` `esp_ota_write` error → `otaTeardown()`), which sets `ota.active = false` and `ota.written = 0`. `otaWrittenOffset()` (`:71`) then returns 0. So the ACK for that frame is exactly the `OTA_ST_OK` + `ackedOffset = 0` combination the comment, `docs/WCB_OTA_TECHNICAL.md:176`, `docs/WCB_OTA_TECHNICAL.md:268` and the doc's Revision-log row (`:300`) all claim is prevented. The status only becomes ERR on the *next* frame, once `ota.active` has been observed false.

Consequence traced into the host: `Wizard/app.js:1774` fires its collapse-to-0 backstop first (`ack.offset === 0 && peak > CHUNK`) and throws "target lost the OTA session at N bytes (it likely timed out or rebooted)". The `status !== 0` branch at `Wizard/app.js:1784`, whose comment says it exists precisely to name "an image overrun, a flash-write error", is never reached for the frame that carries the real cause. On the relay path the target's own `[OTA] esp_ota_write failed @N` line goes only to the target's (unconnected) USB, so the true cause is lost entirely.

Note `NaviCore/navicore_ota.h:326,339` carries the identical pre-write snapshot, so "matches navicore_ota.h" (WCB_OTA.cpp:412) is true — both copies have the same gap.

**Failure scenario.** Relay OTA to WCB3. At ~600 KB an `esp_ota_write` returns non-OK (marginal flash / brownout). `otaWrite` tears the session down; the ACK for that frame is `[OTA:ACK,3,<sess>,0,0]` — status OK. The Wizard hits its collapse-to-0 backstop and reports "target lost the OTA session at 599808 bytes (it likely timed out or rebooted) — retry the OTA". The user retries repeatedly and is never told a flash write failed, because the ERR status the doc promises is only sent on the following frame, which the Wizard never sends (it already threw).

**Suggested fix.** Re-evaluate the session state after the write instead of reusing the pre-write snapshot, e.g. `bool ok = otaWrite(...); uint8_t st = (ota.active && pkt.sessionId == ota.sessionId) ? OTA_ST_OK : OTA_ST_ERR;` and pass `st` to `sendOtaAck`. Keep `inSession` for the lastActivityMs keep-alive only. Apply the same change to `NaviCore/navicore_ota.h:339` so the two targets stay identical.

### F-092 · S3 · `Code/WCB/WCB_OTA.cpp:423`

**Relay OTA sends the END success ACK exactly once, so one lost ESP-NOW frame reports a completed, boot-switched update as failed**

*Category:* `protocol`

`handleOtaEndPacket`:

```c
422:  bool ok = otaEnd(pkt.sessionId);
423:  sendOtaAck(pkt.sourceWCB, pkt.sessionId, ok ? OTA_ST_OK : OTA_ST_ERR, otaWrittenOffset());
424:  if (ok) {
425:    Serial.println("[OTA] remote update verified — rebooting into new firmware…");
426:    delay(300);   // let the ACK actually transmit before the radio drops
427:    ESP.restart();
```

By the time this single ACK goes out, `otaEnd` (`:185-202`) has already run `esp_ota_end` (SHA verify) AND `esp_ota_set_boot_partition` — the update is committed and irreversible from the host's point of view. The ACK is a bare unicast; `otaUnicast` (`:351-358`) does no retransmission, and ESP-NOW gives no application-level retry here (unlike ETM, which this path does not use). The host has no way to re-ask: the board reboots 300 ms later, and after reboot the session is gone so a resent `?OTA,END` would answer `OTA_ST_ERR`.

Host side: `Wizard/app.js:1808-1809` — `if (!endAck || endAck.status !== 0) throw new Error('OTA verify/finalize failed on the target')`. A single dropped frame therefore reports a *successful* update as a failure.

The sibling implementation fixed exactly this and says why — `NaviCore/navicore_ota.h:360-364`: "Re-send the success ACK a few times: a single dropped ESP-NOW frame here would make the browser report the OTA FAILED even though the image is verified and the boot slot already switched", implemented as `for (int r = 0; r < 3; r++) { delay(80); sendOtaAck(...); }`. The WCB firmware never got that redundancy. Note the WCB's own boot-announce uses the same reasoning (`WCB.ino:966` — sent redundantly *because* it is unacknowledged), so this is the established pattern in this repo.

**Failure scenario.** Wireless OTA of WCB4 completes; WCB4 verifies SHA, flips otadata to the new slot, unicasts one OTA_ACK, and reboots. That frame collides with mesh traffic (a NaviCore rc_ch burst) and is lost. The Wizard's 12 s wait at `Wizard/app.js:1808` expires and it reports "OTA verify/finalize failed on the target" plus an error toast, then sends ABORT to a board that is already rebooting. WCB4 is in fact running the new firmware. The user re-runs the whole ~1.9 MB wireless OTA believing it failed.

**Suggested fix.** Mirror `navicore_ota.h:364`: after `sendOtaAck` on the `ok` path, resend the success ACK 2-3 more times with a short delay before `ESP.restart()` (the existing `delay(300)` budget already covers ~3 x 80 ms), e.g. `for (int r = 0; r < 3; r++) { delay(80); sendOtaAck(pkt.sourceWCB, pkt.sessionId, OTA_ST_OK, otaWrittenOffset()); }`.

### F-093 · S3 · `Code/WCB/wcb_pin_map.cpp:111`

**ONBOARD_LED is set to GPIO32 for HW 3.1/3.2, which on the ESP32-S3 is the SPI-flash SPID line and is not bonded out on the module**

*Category:* `logic`

`wcb_pin_map.cpp:111` sets `ONBOARD_LED = 32;` in the branch that serves `wcb_hw_version == 31 || wcb_hw_version == 32` — the ESP32-S3 targets (`Code/bin/build.sh:20-21`: v3.1/v3.2 build as `esp32:esp32:esp32s3`).

GPIO32 does not exist as a usable pin on the ESP32-S3 module these boards use. The V3.1 and V3.2 schematics list the module's complete pin set and it goes GPIO0-GPIO18, GPIO21, then jumps to GPIO35-38 and GPIO45-48 plus the MTxx/U0xxD/USB aliases — there is no GPIO22-GPIO34 (`PCB/Wireless Communication Board (WCB)V3.2/Wireless Communication Board (WCB)V3.2.kicad_sch:594-1140`). On the ESP32-S3 die, GPIO26-32 are the in-package SPI-flash bus and GPIO32 specifically is SPID.

The value is inert **today** only because every write path is gated to hardware version 1: `digitalWrite(ONBOARD_LED, ...)` at `WCB.ino:683`, `:694`, `:722`, `:739`, and the sole `pinMode(ONBOARD_LED, OUTPUT)` at `WCB.ino:7170` all sit behind `if (wcb_hw_version == 1)`. So the pin is never actually driven — but the global is loaded with a flash-bus GPIO on an S3 build, and any future code path that touches ONBOARD_LED without re-deriving that gate reconfigures a live SPI-flash pin and takes the chip down on the next cache miss. It is also reported to the operator as fact: `WCB.ino:6225` prints `"  Onboard LED: %d\n", ONBOARD_LED` in `?CONFIG`, telling a 3.x user their onboard LED is on GPIO32.

The adjacent comment says "Not Used in this board but defined to match the Version 1.0 board's onboard Green LED", which explains *why* a value is present but not why it is a flash pin; a value that cannot be a GPIO is not "matching" anything. (The same `32` in the 2.3/2.4 branches is fine — IO32 is a real, unconnected pad on the ESP32-PICO-MINI-02, `PCB/...V2.4.kicad_pcb` U1 pad 12.)

**Failure scenario.** Any change that drops or widens the `wcb_hw_version == 1` guard around ONBOARD_LED — for example extending the identify blink at `WCB.ino:738-741` to boards whose NeoPixel init failed and left `statusLED == nullptr` — executes `pinMode(32, OUTPUT); digitalWrite(32, ...)` on an ESP32-S3, reconfiguring the SPID flash data line and crashing the board on the next flash read. Meanwhile `?CONFIG` on every shipped 3.1/3.2 board already reports a nonexistent "Onboard LED: 32".

**Suggested fix.** Set `ONBOARD_LED = -1;` in the 31/32 branch (the pin genuinely does not exist on this hardware) and have `WCB.ino:6225` skip or print "n/a" when it is negative, so the value can never be handed to pinMode/digitalWrite and ?CONFIG stops reporting a flash pin as an LED.

### F-094 · S3 · `Code/WCB/WCB_PWM.cpp:482`

**clearAllPWMMappings() hardcodes '?' for its remote commands while the rest of the module uses LocalFunctionIdentifier**

*Category:* `logic`

```c
snprintf(remoteCmd, sizeof(remoteCmd), "?PX%d", remotePorts[wcb][p]);   // :482
...
sendESPNowMessage(wcb, "?REBOOT");                                       // :491
```

Every other remote command in this file builds its prefix from the runtime setting — `"%cMAP,PWM,CLEAR,OUT,S%d", LocalFunctionIdentifier, …` at `:311` and `:356`, and `"%cMAP,PWM,OUT,S%d"` at `:328`. `LocalFunctionIdentifier` is a mutable global (`WCB.ino:155`, default `'?'`, changed by `?FUNCCHAR` at `WCB.ino:5047` and persisted); it is part of the backup chain (`WCB.ino:2800`).

The receiver dispatches strictly on its own value: `WCB.ino:4380` `if (cmd.startsWith(String(LocalFunctionIdentifier))) processLocalCommand(cmd.substring(1));`. A board whose identifier is not `'?'` falls through to the `else` at `WCB.ino:4386` and treats `?PX3` / `?REBOOT` as broadcast payload — the literal text is emitted out its serial ports as data. The `?PX` and `?REBOOT` handlers (`WCB.ino:5270`, `:5193`) are never reached.

This is the second, independent reason the same two lines fail; the first is the missing ETM flag reported separately.

**Failure scenario.** A builder sets `?FUNCCHAR,#` across the fleet (a documented, backed-up setting). Board 1 has `?MAP,PWM,S1,W2S3` and the user runs `#MAP,PWM,CLEAR,ALL`. Board 1 sends the literal `?PX3` and `?REBOOT` to board 2. Board 2's `LocalFunctionIdentifier` is `'#'`, so neither string matches at `WCB.ino:4380`, nor `CommandCharacter` at `:4384`; both fall to `processBroadcastCommand()` and the strings `?PX3` and `?REBOOT` are written out board 2's enabled serial ports as literal bytes to whatever devices are attached. S3 stays configured as a PWM output on board 2 indefinitely.

**Suggested fix.** Use the configured identifier, as the rest of the file does: `snprintf(remoteCmd, sizeof(remoteCmd), "%cPX%d", LocalFunctionIdentifier, remotePorts[wcb][p]);` and build the reboot string the same way into a small buffer instead of the `"?REBOOT"` literal. Better still, send `"%cMAP,PWM,CLEAR,OUT,S%d"` to match `removePWMMapping()` at `:356`, since that handler already reboots the remote itself (`WCB.ino:4521-4523`) and the separate `?REBOOT` becomes unnecessary.

### F-095 · S3 · `Code/WCB/WCB_PWM.cpp:909`

**loadPWMOutputPortsFromPreferences() writes pwmOutputPorts[pwmOutputCount++] with no MAX_PWM_OUTPUT_PORTS bound**

*Category:* `bounds`

```c
int savedCount = preferences.getInt("count", 0);   // :889 — NVS-supplied, unvalidated
...
for (int i = 0; i < savedCount; i++) {             // :899
    ...
    if (canUsePWMOnPort(port)) {
        pwmOutputAutoSrc[pwmOutputCount] = autoSrc;  // :908
        pwmOutputPorts[pwmOutputCount++] = port;     // :909
```

`pwmOutputPorts[]` and `pwmOutputAutoSrc[]` are `MAX_PWM_OUTPUT_PORTS` (5) elements (`WCB_PWM.h:51`, `:56`; definitions at `WCB_PWM.cpp:39`, `:41`). The loop trusts `savedCount` completely — there is no `pwmOutputCount < MAX_PWM_OUTPUT_PORTS` condition anywhere in the function.

Today this stays in range only by luck of the write side: `addPWMOutputPort()` caps at `:808`, and `savePWMOutputPortsToPreferences()` only ever writes the current `pwmOutputCount` (`:877`). That is exactly the "unchecked bound that today happens to stay in range" class — and it guards *global* arrays, so an overrun corrupts adjacent BSS rather than a stack frame. `MAX_PWM_OUTPUT_PORTS` has been edited before (`git log -S MAX_PWM_OUTPUT_PORTS -- Code/WCB/WCB_PWM.h` → `c0a8d93`, `7327091`), so a firmware that shrinks it while leaving a larger `count` in NVS is a live downgrade hazard, and the function runs on every boot from `initPWM()` (`WCB.ino:7151`) before anything else is up.

Related: `savePWMOutputPortsToPreferences()` never `remove()`s `port<i>`/`auto<i>` keys above the current count, so stale higher-index entries persist in NVS and are readable by exactly this loop.

**Failure scenario.** NVS carries `pwm_outputs/count = 8` — e.g. from a build where `MAX_PWM_OUTPUT_PORTS` was larger, or a downgrade — with `port0..port7` all set to valid ports 1-5. The boot-time `initPWM()` loop writes `pwmOutputPorts[0..7]` and `pwmOutputAutoSrc[0..7]`, running 3 ints and 3 bytes past the ends of two globals. `pwmOutputAutoSrc[]` is declared immediately after `pwmOutputPorts[]` in the same TU (`:39`, `:41`), so the port overrun lands directly in the provenance array and every WDP self-heal decision afterwards is made against garbage tags — with the further writes landing in whatever the linker placed next.

**Suggested fix.** Clamp before the loop and inside it: `if (savedCount > MAX_PWM_OUTPUT_PORTS) savedCount = MAX_PWM_OUTPUT_PORTS;` at `:892`, and change the loop condition to `for (int i = 0; i < savedCount && pwmOutputCount < MAX_PWM_OUTPUT_PORTS; i++)`. While there, have `savePWMOutputPortsToPreferences()` `remove()` `port<i>`/`auto<i>` for `i` from `pwmOutputCount` to `MAX_PWM_OUTPUT_PORTS-1` so stale keys can never be read back.

### F-096 · S3 · `Code/WCB/WCB_RemoteTerm.cpp:28`

**`xQueueCreate` result is never checked — a failed allocation makes the WiFi task call xQueueSend on a NULL handle**

*Category:* `error-handling`

```
static void ensureQueue() {
  if (!s_rtermQueue)
    s_rtermQueue = xQueueCreate(RTERM_QUEUE_DEPTH, sizeof(RtermQueueItem));
}
```
(WCB_RemoteTerm.cpp:26-29). The return is stored but never tested. `ensureQueue()` is called only from `rtermRelayHandlePacket()` (line 154), i.e. lazily, on the WiFi task, on receipt of the first RTERM packet — and line 167 then unconditionally does `xQueueSend(s_rtermQueue, &item, 0);`. If `xQueueCreate` returned `NULL`, FreeRTOS's `xQueueGenericSend` immediately does `configASSERT(pxQueue)` and then `taskENTER_CRITICAL(&(pxQueue->xQueueLock))` — an abort or a `LoadProhibited` NULL dereference, either way a panic inside the WiFi task.

The allocation is not trivial: `RtermQueueItem` is 1+1+161 = 163 bytes, x `RTERM_QUEUE_DEPTH` 16 = ~2.6 KB of internal DRAM, requested from a callback rather than at boot, so it is attempted at the worst possible moment for heap availability (an active mesh with OTA/config-frag buffers in flight).

**Failure scenario.** A board is acting as a relay while a wireless OTA and a config pull are in flight (both allocate large buffers — WCB.ino enqueues OTA packets and reassembles 230-byte frags). Internal DRAM is momentarily fragmented below 2.6 KB when the first `[TERM:n]` packet from a managed board arrives. `xQueueCreate` returns NULL, `ensureQueue()` swallows it, and line 167 panics the WiFi task — the relay board resets mid-OTA, taking the transfer down with it.

**Suggested fix.** Create the queue once in `setup()` (next to the other one-time init in WCB.ino) rather than lazily from the callback, and check the handle: `if (!s_rtermQueue) return;` before line 167, plus a one-shot warning printed from `loop()` (never from the callback) so a failed allocation is visible instead of silent.

### F-097 · S3 · `Code/WCB/WCB_RemoteTerm.cpp:71`

**`_lineBuf` / `_lineLen` are shared mutable state written from multiple FreeRTOS tasks on both cores with no lock**

*Category:* `race`

`_bufChar()` does an unsynchronised read-modify-write of the shared session buffer:
```
  _lineBuf[_lineLen++] = (char)c;
  if (c == '\n' || _lineLen >= LINEBUF_SIZE) {
    _flushLine();
  }
```
(WCB_RemoteTerm.cpp:68-76). `_flushLine()` (lines 79-83) reads `_lineBuf` for the whole duration of `_sendPacket()` — which does a `memset`, `strncpy`, `memcpy`, an `esp_now_is_peer_exist`, possibly an `esp_now_add_peer`, and an `esp_now_send` (lines 89-111) — and only *afterwards* sets `_lineLen = 0` (line 82). There is no portMUX, mutex, or critical section anywhere in the module.

The writers are genuinely concurrent on two cores. `Serial` is this object sketch-wide, and prints come from: `loop()`/`loopTask` and `serialCommandTask` (WCB.ino:7439, core 1), `RawSerialForwardingTask` (:7464, core 1), `KyberLocalTask`/`KyberRemoteTask` (:7443, :7447, core 1), `PWMTask` (:7459, **core 0, priority 2**), `identifyTask` (:5082), and the WiFi task via `espNowReceiveCallback`/`espNowSendCallback`. The underlying `HardwareSerial::write` is mutex-protected by the core, so only the wrapper's own state is exposed.

**Failure scenario.** An RTERM session is armed on a board doing PWM passthrough. `PWMTask` (core 0) prints a status line at the same instant `serialCommandTask` (core 1) prints a command echo. Both read `_lineLen`, both write the same slot, one character is lost and the two lines are interleaved in the relayed terminal while the USB console shows them correctly — a relayed terminal that disagrees with the local console. In the narrower window where task A has incremented `_lineLen` to `LINEBUF_SIZE` and is still inside `_sendPacket()` (an `esp_now_send`, microseconds to milliseconds) before line 82 resets it, task B evaluates `_lineBuf[_lineLen++]` with `_lineLen == 160` and writes one byte past the 160-byte array, into the object's trailing padding / `_lineLen` storage.

**Suggested fix.** Guard the buffer with a `portMUX_TYPE` taken across `_bufChar()` and `_flushLine()`, or restructure so `write()` only appends under a spinlock and copies the line out to a local before releasing it, then transmits outside the lock (which pairs naturally with deferring the send to `loop()`). At minimum reorder `_flushLine()` to copy the line and reset `_lineLen` *before* calling `_sendPacket()`, and bound-check with `if (_lineLen < LINEBUF_SIZE)` before the store at line 71.

### F-098 · S3 · `Code/WCB/WCB_RemoteTerm.cpp:121`

**`startSession()` accepts the board's own WCB number, arming a session that unicasts to its own MAC and can never succeed**

*Category:* `logic`

`startSession()` (WCB_RemoteTerm.cpp:118-124) stores `relayWCB` with no validation at all:
```
  _relayWCB = relayWCB;
```
The only caller, WCB.ino:5104-5111, checks `if (relayWCB >= 1 && relayWCB <= MAX_WCB_COUNT)` — a range check, not an identity check. Nothing rejects `relayWCB == WCB_Number`. `_sendPacket()`'s own guard at line 87 (`if (!_relayWCB || _relayWCB > MAX_WCB_COUNT) return;`) is likewise range-only.

With `_relayWCB == WCB_Number`, line 111 unicasts to `WCBMacAddresses[WCB_Number - 1]` — the board's own MAC (WCB.ino:773). ESP-NOW does not loop a station's own transmission back to itself, so `rtermRelayHandlePacket()` never fires and the terminal stays dead, while every send fails at the MAC layer. Lines 105-110 will also happily `esp_now_add_peer()` the board's own address, burning an ESP-NOW peer slot on a peer that can never work.

**Failure scenario.** Two boards in the mesh are misconfigured to the same WCB number — a recurring real-world mistake in this ecosystem, and one the firmware already defends against elsewhere (WCB.ino:3757 rejects id-spoofed senders). The Wizard computes `relayWcb = boardConfigs[relayN].wcbNumber` and targets `boardConfigs[targetN].wcbNumber` (Wizard/app.js:6069-6072); with duplicate numbers it sends `?RTERM,START,<n>` to the board that *is* WCB n. That board arms a self-relay, the terminal pane stays permanently empty with no error anywhere, and every mirrored line fails at the MAC layer — which, with debug enabled, hands straight into the espNowSendCallback feedback loop.

**Suggested fix.** Reject self-relay in `startSession()`: `if (relayWCB == 0 || relayWCB > MAX_WCB_COUNT || relayWCB == (uint8_t)WCB_Number) { HardwareSerial::printf("[RTERM] Invalid relay WCB%d\n", relayWCB); return; }` before assigning `_relayWCB`, so the guard holds regardless of which caller invokes it.

### F-099 · S3 · `Code/WCB/WCB_Storage.cpp:1106`

**storeKyberSettings turns targeting ON purely from the presence of an S-port, so a Kyber-LOCAL board in broadcast mode cannot round-trip through its own config backup**

*Category:* `logic`

`kyberUseTargeting` is a persisted setting (`saveKyberTargets()` :2078 `putBool("use_target", …)`, `loadKyberTargets()` :2098) with two genuinely different runtime behaviours in `forwardDataFromKyber` (WCB.ino:4258-4288): true = write only to `kyberTargets[]`; false = write to *every* locally configured Maestro port and broadcast to remotes.

But the parser sets it from syntax, not from intent:
  :1078-1082  no comma at all → `kyberUseTargeting = false`
  :1096-1106  `params` starts with S → `kyberUseTargeting = true`, unconditionally

So `?KYBER,LOCAL` yields {local, broadcast mode} — a reachable, persisted state — while `?KYBER,LOCAL,S2` yields {local, targeted}. The config emitter has no way to express the first: `collectConfigCommands()` always writes the port, `String kyberCmd = "KYBER,LOCAL,S" + String(kPort);` (WCB.ino:2837), appending targets only for enabled entries (:2839-2853). The Wizard does the same (Wizard/parser.js:1347).

On restore, that string flips targeting on, and with no targets in it the empty-params auto-populate at :1118-1140 first wipes all targets (`for (…) kyberTargets[i].enabled = false;`) then rebuilds from `maestroConfigs[]` — which on a fresh board is still empty, because `?MAESTRO` is emitted *after* Kyber in the chain (WCB.ino:2864 `emitHelpers()` vs :2857).

**Failure scenario.** Board is set up with `?KYBER,LOCAL` (broadcast mode: Kyber data reaches every locally configured Maestro port). Its backup emits `?KYBER,LOCAL,S2`. Restoring that backup onto a replacement board runs `storeKyberSettings("LOCAL,S2")` → `kyberUseTargeting = true`, params empty → all targets disabled and re-populated from `maestroConfigs[]`, which is empty at that point in the chain → "Warning: No Maestro configs found to auto-populate Kyber targets" and `use_target=true` with zero enabled targets is persisted at :1438. `forwardDataFromKyber` then takes the targeted branch, iterates zero enabled targets, and forwards nothing at all — Kyber is completely dead on the restored board even though the Maestro commands later in the same chain configured the Maestros correctly.

**Suggested fix.** Make the mode explicit on the wire rather than inferred. Emit the broadcast case distinctly (e.g. `KYBER,LOCAL,S<port>,BCAST` or omit the port as the source board does) and, in the parser, only set `kyberUseTargeting = true` when targets are actually present or the explicit targeted form was used — leave a port-only `?KYBER,LOCAL,Sx` in whatever mode the auto-populate actually achieved (`targetIndex > 0`).

### F-100 · S3 · `Code/WCB/WCB_Storage.cpp:1548`

**loadSerialMonitorMappings() writes outputs[j] for an outputCount read straight from NVS, with no clamp to the 10-element array**

*Category:* `bounds`

```
1547:            serialMonitorMappings[i].inputPort   = preferences.getUChar(keyInput.c_str(), 0);
1548:            serialMonitorMappings[i].outputCount = preferences.getUChar(keyCount.c_str(), 0);
1549:            serialMonitorMappings[i].rawMode     = preferences.getBool(keyRaw.c_str(), false);
1550:
1551:            for (int j = 0; j < serialMonitorMappings[i].outputCount; j++) {
1554:                serialMonitorMappings[i].outputs[j].wcbNumber = preferences.getUChar(keyWCB.c_str(), 0);
1555:                serialMonitorMappings[i].outputs[j].serialPort = preferences.getUChar(keyPort.c_str(), 0);
```

`outputs` is `SerialMonitorOutput outputs[10]` (WCB_Storage.h:92) and `outputCount` is a `uint8_t`, so a stored value of up to 255 drives 255 two-byte writes into a 20-byte array — 490 bytes past the end of `serialMonitorMappings[i]`, straight through the following array elements and into whatever follows the global. `serialMonitorMappings` is a file-scope global defined at WCB_Storage.cpp:52 and this runs at boot (WCB.ino:7219).

Today the value always stays in range: the only writer is `saveSerialMonitorMappings()` (line 1843), fed by `addSerialMonitorMapping()`, which caps at 10 (`if (mapping->outputCount < 10)`, line 1732). So this is latent — but it is an unvalidated length read out of persistent storage into a fixed buffer, on the boot path, with no cheap way to notice it went wrong.

Same function, same read: `inputPort` (line 1547) is likewise unvalidated, which is what makes the `blockBroadcastFrom[port - 1]` index at line 1918 reachable with `port == 0` (see the `?SMCLEAR` finding).

**Failure scenario.** `serial_map` holds `sm2_act = true` and `sm2_cnt` = a value above 10 — from a flash bit-flip in the single-byte count, an interrupted `saveSerialMonitorMappings()` that committed `sm2_cnt` before the corresponding outputs, or any future firmware that raises the destination cap and is then downgraded. At boot `loadSerialMonitorMappings()` walks past `outputs[9]` and overwrites `serialMonitorMappings[3]` and `[4]` (and beyond), producing bogus active mappings that `RawSerialForwardingTask` (WCB.ino:6643) and `isSerialPortMonitored()` then act on — or corrupting adjacent globals outright. The board does not fault at the write; it misbehaves later.

**Suggested fix.** Clamp on read: `uint8_t cnt = preferences.getUChar(keyCount.c_str(), 0); if (cnt > 10) cnt = 10; serialMonitorMappings[i].outputCount = cnt;`. Also validate `inputPort` — force the slot inactive if it is not 1-5 — so a corrupt record is discarded rather than half-honoured.

### F-101 · S3 · `Code/WCB/WCB_Storage.cpp:1548`

**loadSerialMonitorMappings trusts outputCount and inputPort straight from NVS with no clamp, writing past outputs[10]**

*Category:* `bounds`

```
serialMonitorMappings[i].inputPort   = preferences.getUChar(keyInput.c_str(), 0);
serialMonitorMappings[i].outputCount = preferences.getUChar(keyCount.c_str(), 0);
serialMonitorMappings[i].rawMode     = preferences.getBool(keyRaw.c_str(), false);

for (int j = 0; j < serialMonitorMappings[i].outputCount; j++) {
    ...
    serialMonitorMappings[i].outputs[j].wcbNumber = preferences.getUChar(keyWCB.c_str(), 0);
    serialMonitorMappings[i].outputs[j].serialPort = preferences.getUChar(keyPort.c_str(), 0);
}
```
(:1547-1556). `outputs[]` is `SerialMonitorOutput outputs[10]` (WCB_Storage.h:92) and `outputCount` is a uint8_t read verbatim from NVS — anything up to 255. The write side does enforce the cap (`if (mapping->outputCount < 10)` at :1732), so today's firmware cannot produce a bad value, which is exactly what makes this the "unchecked bound that happens to stay in range" class: nothing revalidates it on the way back in, and `serialMonitorMappings[]` is a plain global struct array (:52), so an overrun writes into whatever the linker placed after it — `kyberTargets[]` is declared immediately below at :54.

The same read is unclamped for `inputPort` (0..255 accepted for a 1..5 field), and that value is used unguarded as `blockBroadcastFrom[port - 1]` in `clearAllSerialMonitorMappings()` (:1915-1919) and as an index-free comparison elsewhere.

Relevant given this is a released-firmware upgrade path: the load reads namespace `serial_map` while the commented-out predecessor loader read the same key layout from `serial_monitor` (:1772-1796), so a layout/namespace change here has happened before.

**Failure scenario.** Any NVS content not written by this exact firmware version — a partially-completed write interrupted by a brown-out mid-`saveSerialMonitorMappings()`, a flash bit-flip in the `sm0_cnt` entry, or a future/older layout that stored a different cap — leaves `sm0_cnt` above 10. On the next boot `loadSerialMonitorMappings()` writes `outputs[10]` upward, corrupting the adjacent `kyberTargets[]`/globals; the board then boots with garbage Kyber routing or crashes in a completely unrelated subsystem, with nothing pointing back at the mapping loader.

**Suggested fix.** Clamp on load: `uint8_t c = preferences.getUChar(keyCount.c_str(), 0); if (c > 10) c = 10; serialMonitorMappings[i].outputCount = c;` and validate `inputPort` (`if (p < 1 || p > 5) { mark the entry inactive; continue; }`) before trusting it.

### F-102 · S3 · `Code/WCB/WCB_Storage.cpp:2154`

**printKyberList() indexes baudRates[-1] and copy-constructs serialPortLabels[-1] when an enabled Kyber target has targetPort 0**

*Category:* `bounds`

Three sites dereference `targetPort - 1` with no range check, guarded only by `enabled`:

```
2149:    for (int i = 0; i < MAX_KYBER_TARGETS; i++) {
2150:      if (kyberTargets[i].enabled) {
2154:                   ":" + String(baudRates[kyberTargets[i].targetPort - 1]);
...
2183:      for (int i = 0; i < MAX_KYBER_TARGETS; i++) {
2184:        if (kyberTargets[i].enabled) {
2188:                 ":" + String(baudRates[kyberTargets[i].targetPort - 1]);
...
2193:      for (int i = 0; i < MAX_KYBER_TARGETS; i++) {
2194:        if (kyberTargets[i].enabled && kyberTargets[i].targetWCB == wcb) {
2195:          String label = serialPortLabels[kyberTargets[i].targetPort - 1];
```

`baudRates` is `[5]` and `serialPortLabels` is `String[5]` (WCB.ino:631). `loadKyberTargets()` (lines 2100-2110) reads `enabled` and `targetPort` from two independent NVS keys with independent defaults (`getBool(keyEn, false)` and `getUChar(keyPort, 0)`), so nothing in the load path enforces that an enabled target has a non-zero port.

That this state is considered real is established by the firmware itself — the runtime forwarding path guards for exactly this value, with a comment:

```
WCB.ino:4268:  // Guard the port so a corrupt targetPort of 0 doesn't fall through to the debug Serial console.
WCB.ino:4269:  if (kyberTargets[i].targetPort >= 1 && kyberTargets[i].targetPort <= 5)
```

Line 2195 is the dangerous one: `String label = serialPortLabels[-1]` copy-constructs an Arduino `String` from the 16 bytes preceding the array, taking whatever lies there as `buffer`/`len`/`cap` and then memcpy-ing `len` bytes from that pointer.

**Failure scenario.** A board whose `kyber_targets` namespace holds `kt<i>_en = true` with `kt<i>_port` absent or 0 (legacy NVS, a partial write, or a flash bit-flip — the state WCB.ino:4268 exists to survive) runs `?KYBER,LIST` while Kyber is local with targeting on. Line 2154 reads `baudRates[-1]` and prints garbage into the copy-paste setup command; line 2195 copy-constructs a String from out-of-bounds memory, which at best emits garbage into the `?SLS` command and at worst dereferences a bogus pointer and panics — turning a read-only diagnostic command into a crash.

**Suggested fix.** Apply the same guard the forwarding path uses. At each of lines 2154, 2188 and 2195, skip or clamp: `uint8_t p = kyberTargets[i].targetPort; if (p < 1 || p > 5) continue;` before indexing `baudRates[p-1]` / `serialPortLabels[p-1]`. Alternatively, sanitise in `loadKyberTargets()` — force `enabled = false` for any slot whose loaded `targetPort` is outside 1-5.

### F-103 · S3 · `Code/WCB/WCB_Storage.cpp:2195`

**printKyberList indexes baudRates[] and serialPortLabels[] with targetPort-1 and no guard, so targetPort 0 reads element -1**

*Category:* `bounds`

`printKyberList()` builds its copy-paste helper text with three unguarded `targetPort - 1` indexes on enabled targets:
  :2154  `":" + String(baudRates[kyberTargets[i].targetPort - 1])`
  :2188  same expression in the per-other-board loop
  :2195  `String label = serialPortLabels[kyberTargets[i].targetPort - 1];`

`kyberTargets[i].targetPort` is a uint8_t loaded verbatim from NVS with default 0 and no validation (`loadKyberTargets()` :2108: `kyberTargets[i].targetPort = preferences.getUChar(keyPort.c_str(), 0);`), independently of `enabled` (:2109).

That a corrupt `targetPort == 0` can coexist with `enabled == true` is not my speculation — the runtime path already defends against it and says so: WCB.ino:4266-4269, "Local — write immediately, byte by byte. Guard the port so a corrupt targetPort of 0 doesn't fall through to the debug Serial console", followed by `if (kyberTargets[i].targetPort >= 1 && kyberTargets[i].targetPort <= 5)`. The display path in this file has no such guard.

:2195 is the dangerous one: `serialPortLabels` is `String[5]`, so index -1 constructs a String copy from the 16 bytes preceding the array — an arbitrary pointer/length pair that `label.length()` and the subsequent concatenation then dereference.

**Failure scenario.** A board whose `kyber_targets` NVS entries predate the current validation (or was corrupted by an interrupted write) holds `kt0_en=true` with `kt0_port=0`. `loadKyberTargets()` accepts it at boot; `forwardDataFromKyber` silently skips it thanks to its guard, so nothing looks wrong. The user then runs `?KYBER,LIST` to work out why that target is dead — `printKyberList()` reaches :2195, copy-constructs a String from out-of-bounds memory and dereferences its buffer pointer, so the diagnostic command itself panics or prints garbage. :2154/:2188 read `baudRates[-1]` and print a nonsense baud into the copy-paste setup commands the user is being told to run on other boards.

**Suggested fix.** Skip or clamp non-1..5 ports in the display loops, matching WCB.ino:4268 — e.g. `if (kyberTargets[i].targetPort < 1 || kyberTargets[i].targetPort > 5) continue;` at the top of each `if (kyberTargets[i].enabled)` body in `printKyberList()`. Better still, validate on load at :2108 (`if (port < 1 || port > 5) { enabled = false; }`) so no consumer has to.

### F-104 · S3 · `Code/WCB/WCB_Variables.cpp:45`

**Table-full eviction silently deletes the oldest volatile variable — including user ;V variables — with no log, contradicting the documented "Set #101 -> error"**

*Category:* `logic`

`evictOneRamSlot()` (WCB_Variables.cpp:43-47) reclaims the FIRST non-persistent slot in index order and prints nothing:

```
  for (int i = 0; i < WCB_MAX_VARIABLES; i++)
    if (vars[i].used && !vars[i].persist) { vars[i].used = false; varCount--; return i; }
```

Its call site comment (:140-141) frames it as recycling "a RAM/telemetry slot", but the code has no notion of telemetry — it evicts any non-persistent variable, and `;V` is the DEFAULT, documented user-facing set command that creates exactly such variables (WCB_Variables.h:10, docs/VARIABLES_DESIGN.md:10-11). Only `;VP` / `?VAR,SET` variables are protected.

Worse, index order makes the victim selection backwards from the intent. Persistent vars loaded from NVS occupy the low slots at boot (`loadVariables` uses `findFreeSlot`, WCB_Variables.cpp:117), so the first RAM slot is the OLDEST-created volatile variable — typically the long-lived user flag set once at boot — while the churny telemetry that caused the pressure sits at higher indices and survives.

The telemetry namespace really can exhaust the 100-slot table: `maestroGetInfo` (WCB_Maestro.cpp:385-388) mints `m<dev>pos<ch>` per device per channel, `m<dev>moving` and `m<dev>err`, with dev 1-8 (WCB_Maestro.cpp:397) and no channel bound — 8 devices x 24 channels is 192 distinct names against `WCB_MAX_VARIABLES` = 100 (WCB_Variables.h:27).

docs/VARIABLES_DESIGN.md:16 still states "Cap: 100 variables (`WCB_MAX_VARIABLES`). Set #101 -> error." That is now false — set #101 succeeds by destroying an existing variable, and the doc never mentions eviction.

**Failure scenario.** A droid where NaviCore polls Maestro positions (`;M1,getPosition,<ch>` … across 4+ devices/24 channels) plus a user flag `;V,domeanimations,1` set once at boot. Telemetry names accumulate past 100 slots. Each new `m<dev>pos<ch>` calls `evictOneRamSlot()`, which walks from index 0 and takes `domeanimations` (the oldest volatile slot) with no message on the terminal. Every later `IF,domeanimations=1^…` now reads the undefined-variable default of 0 (WCB_Variables.cpp:184) and silently skips its command for the rest of the session. `?VAR,LIST` simply no longer shows the variable, with nothing in the log explaining where it went.

**Suggested fix.** At minimum, log the eviction: `Serial.printf("[VAR] table full — evicted volatile '%s' to make room for '%s'\n", vars[i].name, …)`. Better, make the policy match the stated intent: prefer evicting a name in the telemetry namespace (`m` + digit 1-8, the same test `handleMaestroResult` already applies at WCB_Maestro.cpp:501) and only fall back to a user volatile var — or track a per-slot last-set millis and evict least-recently-set instead of lowest-index. Then correct docs/VARIABLES_DESIGN.md:16 to describe the real behaviour.

### F-105 · S3 · `Code/WCB/WCB_Variables.cpp:149`

**vars[] table is read from the ESP-NOW receive callback (WiFi task) with no synchronisation, and a new slot is published (used=true) before its value is written**

*Category:* `race`

`setVariableImpl` publishes a freshly-claimed slot in this order (WCB_Variables.cpp:147-150, then :164-165):

```
    strncpy(vars[idx].name, name.c_str(), WCB_VAR_NAME_MAX);
    vars[idx].name[WCB_VAR_NAME_MAX] = '\0';
    vars[idx].used = true;     // <-- line 149: slot is VISIBLE here
    varCount++;
  }
  vars[idx].value   = value;   // <-- line 164: value written AFTER
  vars[idx].persist = persist;
```

The table has no portMUX / mutex anywhere in the file, and it is genuinely read from another task. Traced read path on the WiFi task (Core 0): `espNowReceiveCallback` (WCB.ino:3584) -> `parseCommandsAndEnqueue(etmCmd, 0)` (WCB.ino:3997; the non-ETM twin is WCB.ino:4227) -> `ifGateConsumeToken` (WCB.ino:2105) -> `evaluateIfCondition` (WCB_Variables.cpp:412) -> `evalOneCondition` (:456) -> `getVariable` (:359) -> `findVarSlot` (:183-184), which reads `vars[i].used`, `vars[i].name` and then `vars[idx].value`.

The writers run elsewhere: `processSetVariable` -> `setVariableImpl` is reached only from the command-queue drain in `loop()` (WCB.ino:7512 `handleSingleCommand`), and `setVariableRAM` from WCB_Maestro.cpp:457/:504 on the same loop task. So a Core-0 reader and a Core-1 writer touch the same array with nothing between them.

Because `used` is set at :149 but `value` only at :164, a slot recycled by `findFreeSlot`/`evictOneRamSlot` is momentarily visible under its NEW name carrying the PREVIOUS occupant's value. The same window makes `persist` stale.

This is not a theoretical concern in this codebase — the surrounding code defers cross-core work precisely for this class of hazard: WCB.ino:3991-3994 pushes timer chains to `pendingTimerChainQueue` because "parseCommandGroups mutates a std::vector iterated by loop()", and WCB.ino:3913-3917 queues RC JSON "we're on the WiFi task (Core 0) and Serial isn't atomic across cores". The variable table was left out of that discipline.

**Failure scenario.** WCB2 has ~40 variables. NaviCore broadcasts the chain `IF,mode=1^;M2,goHome` over ESP-NOW. It is decoded on the WiFi task and `evaluateIfCondition("mode=1")` runs inline in the receive callback. At that instant the loop task is executing a queued `;V,mode,7` that creates `mode` in a slot previously occupied by a cleared/evicted variable whose value was 1. The WiFi task passes `vars[i].used == true` and `name.equals("mode")` (name already copied at :147) but reads `vars[idx].value` before line 164 executes, getting 1. The IF passes and `;M2,goHome` fires even though mode is 7. The inverse (reading a stale non-matching value and silently dropping a command that should have run) is equally reachable, and both are one-shot and unreproducible.

**Suggested fix.** Two parts. (1) Publish safely: move `vars[idx].value = value; vars[idx].persist = persist;` to BEFORE `vars[idx].used = true;` in the new-slot branch so a slot is never visible with a stale value (cheap, fixes the worst case with no locking). (2) Add a `static portMUX_TYPE varMux = portMUX_INITIALIZER_UNLOCKED;` and wrap the table walks/mutations in `findVarSlot`/`getVariable`/`setVariableImpl`/`clearVariable`/`clearAllVariables` with `portENTER_CRITICAL`/`portEXIT_CRITICAL`. Keep `saveVarsToNVS()` OUTSIDE the critical section (it does a flash write and builds an Arduino String) — snapshot under the lock, write after.

### F-106 · S3 · `Code/WCB/WCB_Variables.cpp:328`

**?VAR,CLEAR,<name> wipes the entire variable table (and the NVS blob) when the name is "all" in any case**

*Category:* `logic`

`processVarConfig`'s CLEAR branch (WCB_Variables.cpp:326-331) tests the wildcard case-insensitively and BEFORE any exact-name lookup:

```
    String target = vField(a, 1);
    String targetU = target; targetU.toUpperCase();
    if (targetU == "ALL") { clearAllVariables(); Serial.println("[VAR] All variables cleared"); return; }
```

But `all`, `All`, `aLL`, `ALL` are all legal, DISTINCT variable names: `isValidVariableName` (:63-73) accepts any 1-15 char `[A-Za-z0-9_]` string, and the store is explicitly case-sensitive (`findVarSlot` at :31 uses `name.equals`, documented at WCB_Variables.h:18 and docs/VARIABLES_DESIGN.md:19). So a variable literally named `all` can be created by `;V,all,1` or `?VAR,SET,all,1`, can be read by `IF,all=1`, but can never be deleted individually — and the attempt destroys everything else instead. `clearAllVariables()` (:198-202) marks every slot unused and immediately commits the emptied blob via `saveVarsToNVS()`, so the persistent variables are gone from NVS too, not just RAM.

**Failure scenario.** A user creates `?VAR,SET,all,1` (e.g. an "apply to all" flag) alongside a dozen other persistent variables. Later they type `?VAR,CLEAR,all` intending to delete just that flag. `targetU` becomes "ALL", the wildcard branch fires, every variable is dropped from RAM and the NVS blob is overwritten empty. The board prints "[VAR] All variables cleared" and the whole persistent variable set is unrecoverable short of a backup restore. The same one-character trap exists for a user who simply names a variable `All`.

**Suggested fix.** Look for an exact variable match first and only treat the token as the wildcard if no such variable exists — or require the wildcard to be the exact uppercase literal `ALL` (`if (target == "ALL" && !variableExists(target))`). Either way, reject `ALL`/`all` in `isValidVariableName` if it is meant to be reserved, and say so in docs/VARIABLES_DESIGN.md:62-63.

### F-107 · S3 · `Code/WCB/WCB_Variables.cpp:360`

**IF right-hand side silently coerces non-numeric/empty text to 0 via toInt(), so a malformed condition can evaluate TRUE — the opposite of the documented fail-safe**

*Category:* `error-handling`

`evalOneCondition` parses the RHS with no validation (WCB_Variables.cpp:355-366):

```
  String vStr = cond.substring(p + op.length()); vStr.trim();
  …
  int32_t lhs = getVariable(name, 0);
  int32_t rhs = (int32_t)vStr.toInt();   // line 360 — "ON" -> 0, "" -> 0, "1x" -> 1
```

Arduino's `String::toInt()` returns 0 for any non-numeric text and never signals failure. Combined with the undefined-variable default of 0 (`getVariable(name, 0)` at :359, documented at WCB_Variables.h:19), a condition whose RHS is not an integer evaluates `0 == 0` and returns TRUE.

That directly contradicts the contract this file publishes for the evaluator, WCB_Variables.h:54: "On a malformed expression it returns false (fail-safe: skip) and prints why." The function does implement that fail-safe for every OTHER malformed input — missing operator (:353), missing variable (:357), misplaced/trailing AND-OR (:452, :469), no condition (:470) — all print and return false. The RHS is the one hole, and it fails OPEN rather than closed. docs/VARIABLES_DESIGN.md:77 likewise only states "Right-hand side is a literal integer" without saying a non-integer is accepted as 0.

Note this is also reachable via `evaluateIfCondition` from `ifGateConsumeToken` (:412), i.e. the live gating path for every chain, on both the loop task and the ESP-NOW receive callback.

**Failure scenario.** A user writes `IF,safemode=OFF^;M2,fullSpeed` intending "only when safemode is off", or `IF,mode=` after a chain-building slip drops the value. `vStr.toInt()` yields 0; `safemode`/`mode` is unset or genuinely 0, so `lhs == rhs` is true, the gate opens and `;M2,fullSpeed` executes — the exact command the IF was written to withhold. Nothing is printed to indicate the condition was malformed, so the chain looks like it "works" until the variable is set to a non-zero value and the gate then closes, which reads as random behaviour.

**Suggested fix.** Validate the RHS before comparing: require an optional `+`/`-` followed by at least one digit and nothing else (`for each char: isdigit`, sign only at index 0, reject empty). On failure do what every other malformed branch does — `Serial.printf("[VAR] IF: right-hand side '%s' is not an integer in '%s'\n", vStr.c_str(), cond.c_str()); return false;`. Then document in docs/VARIABLES_DESIGN.md §2 that a non-integer RHS is a malformed condition and skips.

### F-108 · S3 · `Code/WCB/WCB_WDP.cpp:475`

**wdpEvaluateMaestroRemote consults never-expiring neighbour rows, so it re-imposes Maestro-remote after an operator clears it**

*Category:* `logic`

`wdpEvaluateMaestroRemote()` decides from `wdpMeshHasControllerClient()` (:475), which scans for `wdpNeighbors[i].valid && isClient && wdpIsControllerType(alias)` (WCB_WDP.cpp:449-455). `valid` is set on the first advert (:499) and is **never** cleared by ageing — `wdpTick()` only clears `confirmed` after `WDP_TTL_MS` (:387-391); `valid` is cleared only by `?WDP,FORGET`/`?WDP,CLEAR`/temporary eviction (`wdpForgetNeighbor`, :1088). The same is true of `wdpMeshHasCap()` (:429-436), which also ignores `confirmed`.

Combined with the fact that `wdpEvaluateMaestroRemote()` runs on **every** advert from **any** sender (:636, outside the auto-join gate), a controller that was heard once this boot permanently arms the flip: as soon as the guard `if (Kyber_Local || Maestro_Remote) return;` (:469) stops holding, the next advert from any board re-enables it. The "Sticky + idempotent: only ever ENABLES" comment (:465) describes the intent but does not anticipate that the operator can clear the flag.

**Failure scenario.** A NaviCore is powered on briefly during setup and is heard once by WCB4 (row stays `valid` for the rest of the boot even after the NaviCore is unplugged). The operator decides WCB4 should drive its Maestro locally and runs `?KYBER,CLEAR`, which sets `Kyber_Local=false, Maestro_Remote=false` and persists (WCB_Storage.cpp:1186-1190). Within 60 s any board on the mesh sends its periodic advert; `wdpOnAdvertReceived` calls `wdpEvaluateMaestroRemote()`, the stale NaviCore row still satisfies `wdpMeshHasControllerClient()`, and Maestro-remote is re-enabled and re-persisted — silently undoing the operator's explicit configuration (and, per the previous finding, killing Serial1 again).

**Suggested fix.** Gate `wdpMeshHasControllerClient()`/`wdpMeshHasCap()` on `nb.confirmed` as well as `nb.valid` so a source that has gone quiet past the TTL no longer counts, and add a persisted "operator cleared this" sentinel (or run the evaluation only on `!wasValid` first-learn, like the controller-adopt at :625) so a manual `?KYBER,CLEAR` is not re-fought on the next advert.

### F-109 · S3 · `Code/WCB/WCB_WDP.cpp:837`

**wdpDaDevices[] is written from serialCommandTask and read from loop() with no synchronization**

*Category:* `race`

`wdpDaHandleLine()` rewrites a whole `WdpDaDevice` record in place:

```cpp
  WdpDaDevice &d = wdpDaDevices[port - 1];
  bool wasPresent = d.present;
  memset(&d, 0, sizeof(d));
  d.present    = true;
  d.lastSeenMs = millis();
  strncpy(d.type, type, sizeof(d.type) - 1);
```

It is reached only from `processIncomingSerial()` (WCB.ino:6393), which runs on **serialCommandTask** (WCB.ino:6594-6626, created at WCB.ino:7439). The same records are read concurrently from the **loop task**: `wdpDaTick()` (:851-861, called at WCB.ino:7484), `wdpDaType()` (:862-865) via `wdpBuildPayload()` (:298) and `printWdpDump()` (:1120), and `wdpDaPrint()` (:867). Both tasks are pinned to core 1 at priority 1, so FreeRTOS time-slicing can preempt between any two of the statements above. There is no queue, mutex or portMUX anywhere in this path — unlike the ESP-NOW advert path, which is explicitly deferred through `wdpPktQueue` for exactly this reason (WCB.ino:984-1002).

The struct stays NUL-terminated (memset precedes the strncpy), so this cannot overrun — but the intermediate states are observable and one of them is destructive: `d.present == true` with `d.lastSeenMs == 0`.

**Failure scenario.** serialCommandTask executes `d.present = true` (:837) and is preempted before `d.lastSeenMs = millis()` (:838). loop() runs `wdpDaTick()`, which sees `d.present == true` and `now - 0 > WDP_DA_TTL_MS` (true for any uptime past 90 s), prints a spurious `[WDP-DA] S2:  stopped announcing` (with an empty type, since :836 just zeroed it) and sets `d.present = false`. serialCommandTask resumes and writes `lastSeenMs`/`type` into a record that is now marked absent, so the just-announced device disappears from `?WDP,DA` and its detected type is dropped from this board's advertised PORTLABEL until the device's next announce 25-30 s later. A narrower variant (preemption between :836 and :839) makes `wdpDaType()` return "" so `wdpBuildPayload` skips that port's label for one advert.

**Suggested fix.** Write `d.lastSeenMs = millis()` before `d.present = true` (closes the destructive ordering), and guard the record with a portMUX critical section — or, matching the ESP-NOW path, push the parsed `WdpDaDevice` through a small queue and apply it in `wdpDaTick()` on the loop task.

### F-110 · S3 · `Code/WCB/WCB_WLED.cpp:147`

**No remote-to-self repair for WLED slots: a renumbered board unicasts ;L<id> to itself forever and the slot can never self-heal**

*Category:* `logic`

`processWLEDRuntimeCommand` treats any `remoteWCB > 0` as "somewhere else":

```cpp
  if (cfg.remoteWCB == 0 && cfg.serialPort >= 1 && cfg.serialPort <= 5) {
    wledDispatchLocal(cfg.serialPort, rest);        // local device
  } else if (cfg.remoteWCB > 0) {
```

Nothing excludes `cfg.remoteWCB == WCB_Number`. The Maestro module — which this file explicitly mirrors (`WCB_WLED.h:7`) — has exactly that repair: `normalizeMaestroSelfSlots()` (`WCB_Storage.cpp:2021`) collapses `remoteWCB == WCB_Number` slots at boot, and `setup()` calls it at `WCB.ino:7158` specifically *after* `loadWCBNumberFromPreferences()` (`WCB.ino:7154`), with a comment naming the failure it prevents. `loadWLEDSettings()` is called at `WCB.ino:7144`, also before WCB_Number is known, and has no counterpart — no `normalizeWLEDSelfSlots()` exists anywhere in the repo.

The state is producible: `?WCB,<n>` applies the new number **immediately**, not on reboot (`WCB_Storage.cpp:194`, `WCB_Number = wcb_number_f; // take effect immediately`), while WDP-learned proxies already sit in `wledConfigs[]` and NVS.

Once created, the slot is unrecoverable by any automatic path:
- `sendESPNowMessage(cfg.remoteWCB, …)` at line 154 resolves `WCBMacAddresses[target-1]` (`WCB.ino:2312`) to this board's own MAC. There is no self-target guard; ESP-NOW does not loop the frame back, so under ETM the entry sits in the pending table, burns 3 retries and is marked failed.
- `wledAutoAddRemote` (line 424-434) finds the slot by id, sees `remoteWCB != hostWCB`, and returns false — so the real host's adverts can never re-home it ("first-host-wins").
- `printWLEDBackup` (line 402) emits `?WLED,<id>:W<self>S0:<baud>`; feeding that back through `configureWLED` takes the LOCAL branch (`targetWCB == WCB_Number`) and dies on `serialPort < 1` at line 311, so the entry is silently dropped from any config restore.
- The slot permanently consumes one of the 9 slots and permanently shadows that WLED id.

**Failure scenario.** Board X is WCB 3 and has auto-learned WLED 2 from neighbour WCB 7 (`wledAutoAddRemote(2, 7, 115200)` → slot {id=2, port=0, remoteWCB=7}, persisted). The old WCB 7 is retired and the operator renumbers board X to take its place: `?WCB,7`. `WCB_Number` becomes 7 in RAM at once. `;L2,PS,3` now hits line 147, builds `;L2,PS,3` and unicasts it to WCB 7 — itself. Nothing fires; the console shows an ETM retry/fail after 3 attempts. The slot survives reboot (NVS), so `;L2` is dead permanently, and even after a real WLED 2 host comes up on the mesh its adverts are ignored. `?WLED,LIST` reports "WLED 2 : remote on WCB7 @ 115200 baud" on a board that *is* WCB 7. A config backup/restore round-trip silently drops the line rather than repairing it. Manual `?WLED,CLEAR,2` is the only escape, and nothing tells the operator that.

**Suggested fix.** Add a `normalizeWLEDSelfSlots()` alongside `normalizeMaestroSelfSlots()` and call it from `setup()` right after `loadWCBNumberFromPreferences()` (WCB.ino:7154): for each configured slot with `remoteWCB == WCB_Number`, clear the slot (a self-proxy carries no port, so unlike Maestro there is nothing to collapse it *into* — dropping it lets WDP re-learn the correct host on the next advert), then `saveWLEDSettings()` once if anything changed. Also make the runtime branch defensive: change line 147 to `else if (cfg.remoteWCB > 0 && cfg.remoteWCB != WCB_Number)` with an explicit diagnostic in the fall-through, so a slot created between boots (live `?WCB,<n>`) can't silently unicast to self.

### F-111 · S3 · `Code/WCB/WCB_WLED.cpp:262`

**configureWLED silently drops every entry after the first in a chained ?WLED,a:…,b:… command**

*Category:* `logic`

`configureWLED` parses exactly one `<id>:W<wcb>S<port>:<baud>` triple and never looks past it:

```cpp
    wledID = a.substring(0, c1).toInt();
    String rest = a.substring(c1 + 1);                // "W<wcb>S<port>:<baud>"
    int c2 = rest.indexOf(':');
    if (c2 < 0) { … return; }
    dest     = rest.substring(0, c2);                 // "W<wcb>S<port>"
    baudRate = rest.substring(c2 + 1).toInt();
```

For `?WLED,1:W1S1:115200,2:W1S2:115200`, `args` is the whole tail. `c1 = 1`, `wledID = 1`, `rest = "W1S1:115200,2:W1S2:115200"`, `c2 = 4`, `dest = "W1S1"`, and `baudRate = String("115200,2:W1S2:115200").toInt()`. Arduino's `String::toInt()` is `atol()`, which stops at the first non-digit and yields **115200** — a *valid* baud. So `wledBaudValid()` passes at line 282, WLED 1 is configured, the success line prints, and WLED 2 vanishes with no error at all.

This diverges from the module WLED says it mirrors: `configureMaestro` explicitly supports the chained form and loops over comma-separated entries (`WCB_Maestro.cpp:534` "Chained: ?MAESTRO,M1:W1S2:115200,M2:W2S1:57600,M3:W3S3:115200", loop at `:545-548`). The Wizard's parser also documents chaining as legal for WLED and parses it that way — `Wizard/parser.js:756` "New ?WLED,<id>:W<wcb>S<port>:<baud>  (chained entries allowed)" with `for (let i = 1; i < parts.length; i++)` at `:757`.

The firmware's own emitters are safe (`printWLEDBackup` writes one `?WLED,…` per slot; `Wizard/parser.js:1424` emits one command per row), so this only bites hand-typed or third-party-generated commands — but a user reading the Wizard grammar or transposing the documented Maestro form gets a silent partial config.

**Failure scenario.** Operator configures two WLED nodes in one line the way the Maestro docs show: `?WLED,1:W1S1:115200,2:W1S2:115200`. Firmware prints `[WLED] WLED 1: local S1 at 115200 baud (slot 1)` and nothing else. WLED 2 is not configured, S2 is not reserved (broadcast stays enabled on it, so mesh text is written into the WLED node), and `;L2,PS,1` answers `[WLED] WLED 2 not configured`. Because the truncated baud still parses to 115200 there is no error, no warning, and no partial-failure indication anywhere.

**Suggested fix.** Either loop over comma-separated entries the way `configureMaestro` does (split on ',' before the `<id>:` parse, processing each token independently and `continue`-ing on a bad one), or — if single-entry is the intended grammar — reject trailing junk explicitly: after `baudRate = rest.substring(c2 + 1).toInt();`, verify the baud substring is all digits and print `[WLED] One entry per command — use a separate ?WLED,<id>:… for each` when it is not. Whichever is chosen, fix `Wizard/parser.js:756` so its comment and the firmware agree.

### F-112 · S3 · `Code/WCB/WCB_WLED.cpp:452`

**saveWLEDSettings/loadWLEDSettings share one global Preferences object across two FreeRTOS tasks with no lock**

*Category:* `concurrency`

`saveWLEDSettings()` opens the shared global `preferences` (declared `extern Preferences preferences;` at WCB_WLED.cpp:18) with no serialization:

```cpp
void saveWLEDSettings() {
  preferences.begin("wled_cfg", false);
  for (int i = 0; i < MAX_WLED_PER_WCB; i++) { … 5 puts per slot … }
```

It is reached from two different tasks:
- **serialCommandTask (Core 1, prio 1)** — `processIncomingSerial` → `processSerialCommandHelper` → `processLocalCommand` → `configureWLED` → `saveWLEDSettings()` (and, before it, `wledReserveLocalPort` → `updateBaudRate`, which opens `preferences` on namespace `"serial_baud"` at `WCB_Storage.cpp:175`). Task created at `WCB.ino:7439`.
- **loopTask (Core 1, prio 1)** — `loop()` → `drainWdpPackets()` (`WCB.ino:7494`) → `wdpOnAdvertReceived` → `wledAutoAddRemote` (`WCB_WDP.cpp:728`) → `saveWLEDSettings()` at WCB_WLED.cpp:429/443.

Same priority on the same core means FreeRTOS time-slicing can preempt either one, and NVS calls block on the flash/NVS mutex, so a switch *inside* a begin/end pair is not exotic. The Arduino `Preferences` class makes that unsafe by construction — `Preferences::begin()` returns false and leaves `_handle` pointing at the previous namespace when already open (pinned core esp32@3.3.4, `libraries/Preferences/src/Preferences.cpp:30-32`: `if (_started) { return false; }`), and `end()` closes the handle regardless of who opened it. The return value is discarded at line 452 and line 472. There is no mutex anywhere in `Code/WCB` (`grep -rn "SemaphoreHandle_t\|prefsMutex" Code/WCB` → no hits).

This is a shared-object hazard rather than a WLED-only one — `maestroAutoAddRemote`, `hcrAutoAddRemote`, `mp3AutoAddRemote`, `dfpAutoAddRemote` and `addActivePeer` all write NVS from the same `drainWdpPackets()` call — but WLED's pair is the concrete one in scope and shows the whole shape.

**Failure scenario.** A Wizard config push is in flight while WDP adverts are arriving (the normal state right after a fleet reboot). serialCommandTask is in `configureWLED` → `wledReserveLocalPort` → `updateBaudRate(1, 115200)`, which has done `preferences.begin("serial_baud", false)` and is blocked inside `nvs_set_*`. loopTask time-slices in, drains a WDP advert carrying a newly-powered board's WLED, and calls `wledAutoAddRemote` → `saveWLEDSettings()`. Its `preferences.begin("wled_cfg", false)` returns false (ignored) and the stale `"serial_baud"` handle stays live, so all 45 `w0_id`…`w8_baud` keys are written into the **serial_baud** namespace, then `preferences.end()` closes the handle. serialCommandTask resumes and its `preferences.putString("Serial1", …)` runs on a closed handle (ESP_ERR_NVS_INVALID_HANDLE, also unchecked) — the baud never persists, so S1 reverts to the old rate on the next boot while `baudRates[0]` in RAM says 115200. The WLED slot table is never persisted to `wled_cfg` either, and `serial_baud` is left permanently polluted with 45 stray keys that only a factory reset clears.

**Suggested fix.** Serialize NVS access. Minimum viable: add a single recursive `SemaphoreHandle_t` guarding every `preferences.begin(...)`/`end()` pair firmware-wide, taken at the top of `saveWLEDSettings`/`loadWLEDSettings` and their siblings. Cheaper and more targeted: route the WDP auto-config writes through the same task that owns config writes — have `wledAutoAddRemote`/`maestroAutoAddRemote` mutate RAM and set a dirty flag (the pattern `learnedPeersDirty` already uses at WCB.ino:7102) and flush it from one place. Either way, stop discarding the `begin()` return value: `if (!preferences.begin("wled_cfg", false)) return;` turns the silent wrong-namespace write into a no-op.


---

## S4

### F-113 · S4 · `Code/WCB/command_timer.cpp:64`

**printTimerDebugInfo is dead code and leaves an empty `if (debugEnabled) { }` block**

*Category:* `dead-code`

`printTimerDebugInfo` is defined at line 64 with external linkage but has no callers anywhere in the tree — a repo-wide grep for the symbol returns exactly three hits, all in this file: the definition at `:64` and two commented-out call sites at `:152` and `:156`. It is not declared in `command_timer_queue.h` either, so nothing outside can reach it. (Commit `d8b7078` says as much: "added a debug function to test timers but function not used anywhere at the moment.")

Because the only statement inside it was commented out, lines 151-153 are now an empty conditional:

```cpp
151:  if (debugEnabled) {
152:    // printTimerDebugInfo(delayStr, parsedDelay, parsedDelayLimit);
153:  }
```

The compiler emits nothing for the block but does emit the whole unused function into the image (external linkage, so `-Wunused-function` never fires) — dead flash in a firmware whose partition budget is tracked (`min_spiffs`).

**Failure scenario.** Not a runtime failure. Cost is a maintenance trap: the next person reading `parseCommandGroups` sees an empty debug branch and a debug printer that looks live, and either wires it up not knowing why it was disabled or wastes time deciding whether it is safe to remove. The unused function also occupies flash in every shipped image.

**Suggested fix.** Either delete `printTimerDebugInfo` (lines 64-72) together with the empty `if (debugEnabled) { }` at lines 151-153 and the two commented call sites, or uncomment line 152 so the branch does something and mark the function `static`.

### F-114 · S4 · `Code/WCB/WCB_HCR.cpp:11`

**Unused externs in WCB_HCR.cpp, one with a comment that contradicts the validation actually performed**

*Category:* `dead-code`

```
extern int           Default_WCB_Quantity;   // for validating a ?HCR,REMOTE,W<n> host
```

`Default_WCB_Quantity` appears nowhere else in the file (grepped: line 11 is the only hit). The `?HCR,REMOTE,W<n>` validation at lines 555-559 deliberately uses `MAX_WCB_COUNT` instead, and the comment right above it explains why:

```
    // Bound MUST match what auto-learn + routing accept (1..MAX_WCB_COUNT, incl.
    // learned peers ABOVE the WCBQ floor) — else an auto-learned host above the
    // floor round-trips into the backup but is rejected on restore, silently
    // dropping the persisted route.
    if (host < 1 || host > MAX_WCB_COUNT || host == WCB_Number) {
```

So the line-11 comment describes a rule the code deliberately does *not* follow, and it names the exact variable whose use would reintroduce the bug the line-551 comment warns about. A cold session reading the extern list first would take it as the intended bound.

`commandDelimiter` (line 15) is likewise declared and never used — `printHCRBackup()` receives the delimiter as its `delimiter` parameter (line 708) rather than reading the global.

**Failure scenario.** No runtime effect. The hazard is editorial: someone touching the REMOTE validation reads line 11, concludes the host should be bounded by `Default_WCB_Quantity` (the WCBQ floor), changes line 555 accordingly, and silently drops any WDP-auto-learned host above that floor on config restore — precisely the regression the comment at lines 551-554 was written to prevent.

**Suggested fix.** Delete both externs (lines 11 and 15). If `Default_WCB_Quantity` is being kept for a future use, replace its comment with one that states it is *not* the REMOTE bound and points at MAX_WCB_COUNT.

### F-115 · S4 · `Code/WCB/WCB_Maestro.cpp:45`

**findSlotByMaestroIDAndTarget() is dead code — declared, defined, never called**

*Category:* `dead-code`

`int8_t findSlotByMaestroIDAndTarget(uint8_t maestroID, uint8_t remoteWCB)` is defined at WCB_Maestro.cpp:45 and exported at WCB_Maestro.h:56. A repo-wide grep over `Code/WCB/*.{ino,cpp,h}` returns only those two lines — no call site anywhere (the config paths all use the three-part key `findSlotByMaestroIDPortTarget`, WCB_Maestro.cpp:624, :727, :853 and WCB_Storage.cpp:1277).

It is also the one helper whose key is a strict subset of the documented slot identity: its comment claims it exists "so that two entries with the same ID but different targets… each get their own slot", but ignoring `serialPort` means it would collapse two local slots for the same id on different ports — exactly the arrangement WCB_Maestro.cpp:57-62 and the CLEAR help text (line 809) say must stay distinct. Leaving it exported invites a future caller to pick the wrong key.

**Failure scenario.** A maintainer adding a new config path autocompletes to `findSlotByMaestroIDAndTarget(id, 0)` for a local Maestro. It matches the existing `M1:S2` slot, and the new `M1:S1` config overwrites it instead of taking its own slot — silently dropping the S2 Maestro that the fan-out in sendMaestroCommand is designed to keep firing.

**Suggested fix.** Delete the definition (WCB_Maestro.cpp:41-54) and the declaration (WCB_Maestro.h:56).

### F-116 · S4 · `Code/WCB/WCB_Maestro.h:50`

**WCB_Maestro.h comment says maestroAutoAddRemote is "First-host-wins"; the implementation is explicitly per-host**

*Category:* `doc-drift`

The header's contract for the WDP auto-add entry point reads:

```
// Auto-add (or baud-refresh) a REMOTE Maestro proxy learned from a WDP advert:
// slot = {id, serialPort:0, remoteWCB:hostWCB, baud}. First-host-wins + idempotent
// + persisted.
```

The implementation states the opposite in its own block comment (WCB_Maestro.cpp:715-722): "…EVEN IF this id is already hosted locally or proxied to a DIFFERENT host… This is per-host, NOT first-host-wins…", and the code matches: `findSlotByMaestroIDPortTarget(maestroID, 0, hostWCB)` (line 727) keys on the host, so a second advertising host falls through to `findEmptySlot()` (line 738) and gets its own slot. The repo CLAUDE.md documents the same per-host behaviour.

The header is what a caller reads. The stale claim has already propagated: WCB_WLED.cpp:415 says "Mirrors maestroAutoAddRemote: first-host-wins" — and WLED genuinely is one-slot-per-id, so the two functions are described as matching when their slot policies are opposites.

**Failure scenario.** A maintainer sizing MAX_MAESTROS_PER_WCB, or reasoning about why `;M2` fans out to two boards, reads the header, concludes at most one proxy per id can exist, and treats a second proxy as corruption — or "fixes" maestroAutoAddRemote to actually be first-host-wins, silently breaking multi-host Maestro fan-out.

**Suggested fix.** Replace "First-host-wins" in WCB_Maestro.h:50 with "per-host (one proxy per advertising host; a proxy may coexist with a local slot of the same id)", and correct the cross-reference in WCB_WLED.cpp:415 to say WLED differs (strictly one slot per id).

### F-117 · S4 · `Code/WCB/WCB_MP3.cpp:8`

**Comment on the Default_WCB_Quantity extern contradicts the code — REMOTE is validated against MAX_WCB_COUNT and the symbol is unused**

*Category:* `doc-drift`

```
extern int          Default_WCB_Quantity;   // for validating a ?MP3,REMOTE,W<n> host
```

The `?MP3,REMOTE,W<n>` host is not validated against `Default_WCB_Quantity`. `WCB_MP3.cpp:222` reads `if (host < 1 || host > MAX_WCB_COUNT || host == WCB_Number)`, and the comment directly above it at `:220-221` states the opposite of this one on purpose: "Bound matches auto-learn + routing (1..MAX_WCB_COUNT, incl. learned peers above the WCBQ floor) so a persisted route always round-trips through a backup." `MAX_WCB_COUNT` is 20 (`WCB_Storage.h:12`).

Grepping the translation unit, `Default_WCB_Quantity` occurs exactly once — this declaration. Same for `debugEnabled` (declared `:9`) and `commandDelimiter` (declared `:11`), neither of which is referenced in the file. `WCB_DFP.cpp:8` carries a byte-identical stale comment ("for validating a ?DFP,REMOTE,W<n> host") with the same three unused externs at `:8`, `:9`, `:11`.

This matters because the two comments sit ~210 lines apart and say contradictory things about the bound on a persisted routing value — and the WCBQ-vs-MAX_WCB_COUNT distinction is exactly the hard-coded-cap bug class this repo has been burned by before.

**Failure scenario.** No runtime effect. A maintainer reading the top of either file concludes REMOTE routing is capped at the board's configured WCB quantity and 'fixes' :222 to use `Default_WCB_Quantity` — which would reject a learned peer above the WCBQ floor and break the backup round-trip that the comment at :220-221 was written to protect.

**Suggested fix.** Delete the three unused externs (`Default_WCB_Quantity`, `debugEnabled`, `commandDelimiter`) from WCB_MP3.cpp:8-11 and WCB_DFP.cpp:8-11, or at minimum correct the comment to say the REMOTE host is bounded by MAX_WCB_COUNT and cross-reference :220-222.

### F-118 · S4 · `Code/WCB/WCB_MP3.cpp:74`

**sendMP3Raw() is dead exported code that bypasses the codec's volume shadow and pending-ONFIN state**

*Category:* `dead-code`

`sendMP3Raw()` is defined here and declared in `WCB_MP3.h:34`, but a repo-wide grep for the symbol returns only its own definition, its own declaration, and two references inside comments (`WCB_MP3.cpp:39`, `:110`). It is called from nowhere — the WcbCmd migration moved every emit into `Mp3Codec::raw()` (`WcbMp3.cpp:16-20`), and `processMP3AudioCommand` now goes exclusively through `mp3Codec.handle()` (`:121`). The DFP module, written after the migration, has no equivalent function at all.

This is not just unused weight: it is a live, exported, plausible-looking API that writes straight to `getSerialStream(mp3Config.serialPort)`, bypassing the codec entirely — so anything that calls it emits bytes the codec does not know about, leaving `_volume` and `_pendingCb` out of sync with the device. The valuable comment it carries at `:82-88` (the `int8_t` sign bug that dropped tracks 128-255, and the deliberate no-`flush()` decision) applies to `Mp3Codec::raw()` now, not here.

**Failure scenario.** Not a runtime failure today (nothing calls it). The trap is the next change: a contributor adding a new `;A` verb or a boot chime sees a public `sendMP3Raw(uint8_t, int)` in WCB_MP3.h and uses it. The bytes go out correctly but the codec's volume shadow and pending-ONFIN key never move, so a subsequent `;A,PLAY` re-sends a stale 'v' value and a previously-armed ONFIN key fires against the wrong track.

**Suggested fix.** Delete `sendMP3Raw()` and its declaration in WCB_MP3.h:34. Move the two constraints its comment records — that byte2 must stay a full-range `int` with a -1 sentinel, and that there is deliberately no `flush()` — onto `Mp3Codec::raw()` in WcbCmd so they are not lost.

### F-119 · S4 · `Code/WCB/WCB_MP3.cpp:100`

**restUpper is built and upper-cased on every ;A command and never read**

*Category:* `dead-code`

In `processMP3AudioCommand`:

```
String restUpper = rest;
restUpper.toUpperCase();
```

`restUpper` appears nowhere else in the file (grep: 2 hits total, both on these two lines). It is a leftover from the pre-WcbCmd dispatcher, which compared upper-cased verbs inline; case-insensitivity now lives in the codec's `ip()`/`ieq()` helpers (`WcbCmd/src/WcbMp3.cpp:6-14`), which is why `mp3Codec.handle(rest.c_str())` at `:121` is given the original-case `rest`. `processDFPCommand` (`WCB_DFP.cpp:69-92`), written after the migration, correctly has no such copy.

Effect is a heap allocation, copy and in-place upper-case on every single `;A` command — small, but it is on the audio hot path (a sequence can fire these back to back) and it makes a reader think the dispatcher is case-folding when it is not.

**Failure scenario.** No wrong behaviour. Every `;A,...` command — including ones arriving over the mesh and drained from the command queue in `loop()` — performs one pointless String copy plus a full upper-case pass before dispatch. On a board running a long stored sequence of audio triggers, that is one avoidable heap alloc/free per trigger.

**Suggested fix.** Delete both lines (WCB_MP3.cpp:100-101), matching processDFPCommand.

### F-120 · S4 · `Code/WCB/wcb_pin_map.h:6`

**Include guard `#define wcb_pin_map.h` defines an object-like macro named wcb_pin_map that expands to `.h`**

*Category:* `logic`

`wcb_pin_map.h:5-6` is:

```c
#ifndef wcb_pin_map.h
#define wcb_pin_map.h
```

A macro name is a single identifier, so the preprocessor reads the name as `wcb_pin_map` and takes the remaining tokens `.` `h` as the **replacement list**. The directive therefore defines an object-like macro `wcb_pin_map` → `.h` for the rest of every translation unit that includes this header — which is `WCB.ino:95`, `wcb_pin_map.cpp:5` and `WCB_PWM.cpp:4`, i.e. the whole sketch. `#ifndef` likewise only tests `wcb_pin_map` and ignores the trailing tokens (GCC emits "extra tokens at end of #ifndef directive" but accepts it).

The guard still functions and nothing breaks today — I grepped `Code/WCB` and the token `wcb_pin_map` appears only inside `#include "wcb_pin_map.h"` string forms, where macro expansion is not performed on the header-name token. The defect is that the header leaves a stray macro with a plausible C++ identifier name defined project-wide, and the failure it produces is a preprocessor-level substitution error with no obvious connection to this file.

**Failure scenario.** Anyone who later adds a symbol named `wcb_pin_map` anywhere in the sketch — a struct `wcb_pin_map`, a namespace, a `static const PinMap wcb_pin_map[] = {...}` table replacing these globals — gets it textually rewritten to `.h` at every use, producing errors like `expected primary-expression before '.' token` in a file that never included anything suspicious.

**Suggested fix.** Use a valid identifier for the guard: `#ifndef WCB_PIN_MAP_H` / `#define WCB_PIN_MAP_H` / `#endif  // WCB_PIN_MAP_H`, or just `#pragma once`.

### F-121 · S4 · `Code/WCB/WCB_PWM.cpp:47`

**espNowInitialized extern is declared and never used; canSendESPNow() gates only on millis()**

*Category:* `dead-code`

`extern bool espNowInitialized;` is declared at `:47`, immediately below the PWM ISR capture globals, and is referenced nowhere in the file (`grep -n espNowInitialized Code/WCB/WCB_PWM.cpp` → one hit, the declaration). The obvious intended consumer, `canSendESPNow()` at `:50-52`, ignores it entirely:

```c
bool canSendESPNow() {
    return (millis() > 5000);
}
```

So the three call sites (`:299`, `:324`, `:477`) gate remote PWM config sends on a wall-clock heuristic rather than on the radio actually being up. Also unused in this TU: the `serialPortLabels[5]` and `getSerialLabel(int)` externs declared in `WCB_PWM.h:9-10`, which no PWM code references.

`canSendESPNow()` additionally has external linkage with a very generic name and lives in a subsystem file — the kind of symbol that collides on a future merge.

**Failure scenario.** No runtime failure today: `initPWM()` runs at `WCB.ino:7151` and the only sends happen from interactive commands well past 5 s. The defect is that the guard reads as "is ESP-NOW ready" while checking something else, and the variable that would answer that question is declared right there unused — a maintainer adding an early-boot PWM config path would reasonably trust the name and send into an uninitialised radio.

**Suggested fix.** Either use the declared flag — `return espNowInitialized && millis() > 5000;` — or delete the unused `extern` at `:47` and the unused `serialPortLabels`/`getSerialLabel` externs at `WCB_PWM.h:9-10`. Mark `canSendESPNow()` `static`, since all three callers are in this file.

### F-122 · S4 · `Code/WCB/WCB_RemoteTerm.h:62`

**Header claims a runtime size assertion that does not exist, and sizeof(espnow_struct_remote_term) is never pinned against the other size-routed packets**

*Category:* `doc-drift`

WCB_RemoteTerm.h:62 reads `// static_assert done at runtime via setup() Serial.printf`. Grepping the whole `Code/WCB` tree for `sizeof(espnow_struct_remote_term)` returns exactly four hits: WCB.ino:3602, :3608, :3619 (the OTA/seqval static_asserts, where it appears only as the *other* operand) and WCB.ino:3672 (the router dispatch). There is no `Serial.printf` of that size in `setup()` or anywhere else, and no static_assert on it. The comment describes code that does not exist.

The substantive gap behind the stale comment: the ESP-NOW receive path routes **purely by packet size** (WCB.ino:3631-3676), and the sibling structs each carry an explicit static_assert pinning them against every other size (WCB.ino:3599-3612 for OTA, :3615-3625 for seqval, with the reasoning spelled out at WCB.ino:3596-3598 and WCB.ino:3813-3815). `espnow_struct_remote_term` has no such assert against `espnow_struct_message` (249), `espnow_struct_message_etm` (252), `espnow_struct_mgmt` (226), `espnow_struct_config_frag` (230) or `espnow_struct_config_req` (43). The header's own list at lines 46-51 is also stale — it predates `espnow_struct_ota_ctrl` (55), `espnow_struct_ota_data` (243) and `espnow_struct_seqval_req` (59) and does not mention them. Today 204 is unique, so nothing is broken; the invariant simply is not enforced.

**Failure scenario.** A future change bumps `RTERM_TEXT_SIZE` (WCB_RemoteTerm.h:52) from 160 to 186 to carry longer lines. `sizeof(espnow_struct_remote_term)` becomes 230 — identical to `espnow_struct_config_frag`. The build succeeds silently (no assert fires), and at runtime WCB.ino:3661-3668 matches the config-frag branch first, so every remote-terminal packet is fed to `handleConfigFragPacket()` and the RTERM branch at WCB.ino:3672 becomes unreachable: the relayed terminal goes silent fleet-wide while config pulls are corrupted by terminal text, with nothing logged.

**Suggested fix.** Replace the false comment at line 62 with a real compile-time check next to the existing ones in `espNowReceiveCallback` (WCB.ino:3599), asserting `sizeof(espnow_struct_remote_term)` differs from `espnow_struct_message`, `_etm`, `_mgmt`, `_config_req`, `_config_frag`, `_ota_ctrl`, `_ota_data` and `_seqval_req`, and update the stale size list at WCB_RemoteTerm.h:46-51 to include the three structs added since.

### F-123 · S4 · `Code/WCB/WCB_Storage.cpp:137`

**printHWversion() has a duplicated `wcb_hw_version == 31` branch — the second is unreachable**

*Category:* `dead-code`

```
135:  } else if (wcb_hw_version == 31){
136:    Serial.println("HW Version: 3.1");
137:  } else if (wcb_hw_version == 31){
138:    Serial.println("HW Version: 3.1");
139:  } else if (wcb_hw_version == 32){
140:    Serial.println("HW Version: 3.2");
```

Lines 137-138 can never execute: any value reaching them has already failed the identical test at line 135.

The shape of the mistake matters more than the dead branch. `saveHWversion()` at lines 74-75 accepts exactly `{1, 21, 23, 24, 31, 32}`, and this printer is the human-readable mirror of that set. A copy-paste that duplicated 31 is how a *missing* version gets introduced — and the guard at line 74 and this printer must stay in lockstep, since a version accepted by `saveHWversion()` but unhandled here falls through to "SET YOUR HARDWARE VERSION BEFORE PROCEEDING!!" (line 142) on a board that is in fact configured. Both lists are hand-maintained duplicates of the set encoded in `wcb_pin_map.cpp`.

**Failure scenario.** No runtime failure — `printHWversion()` produces correct output for all six accepted versions today. The finding is the latent duplication: the next hardware revision added to `saveHWversion()`'s accept-list at lines 74-75 but not here prints "SET YOUR HARDWARE VERSION BEFORE PROCEEDING!!" on a correctly-configured board, which is an alarming and actively misleading diagnostic.

**Suggested fix.** Delete lines 137-138. Better, replace both the accept-list at lines 74-75 and this if-chain with one table (`static const struct { int v; const char *name; } HW_VERSIONS[]`) walked by both functions, so the accepted set and the printed set cannot diverge.

### F-124 · S4 · `Code/WCB/WCB_Storage.cpp:259`

**saveBaudRatesToPreferences() is dead code — no declaration, no callers**

*Category:* `dead-code`

```
258: // Save baud rates to preferences
259: void saveBaudRatesToPreferences() {
260:     preferences.begin("serial_baud", false);
261:     for (int i = 0; i < 5; i++) {
262:         String key = "Serial" + String(i + 1);
263:         preferences.putInt(key.c_str(), baudRates[i]);
264:     }
265:     preferences.end();
266: }
```

It has no prototype in WCB_Storage.h (the header declares `updateBaudRate`, `loadBaudRatesFromPreferences`, `printBaudRates` at lines 133-136 but not this one), and a grep for the symbol across the repo — `grep -rn "saveBaudRatesToPreferences" Code/WCB Wizard` — returns only this definition. Every baud write in the firmware goes through `updateBaudRate()` (line 146), which validates the port and the rate, applies the change live via `applyLiveBaud()`, and syncs `baudRates[]` before writing NVS.

The risk in leaving it is not the wasted flash — it is that it is a plausible-looking bulk-save helper that bypasses every one of those guards (no port validation, no rate validation, no `applyLiveBaud`), so a future caller reaching for it would silently reintroduce the exact bugs the comments at lines 147-171 record as already fixed.

**Failure scenario.** No runtime failure today — the function is never executed. The defect is latent: a maintainer adding a "save all baud rates" path finds this helper, calls it, and the live UART/SoftwareSerial re-init that `updateBaudRate()` performs at line 166 (added specifically because S3-S5 baud changes previously did nothing until reboot) is skipped.

**Suggested fix.** Delete lines 258-266. If a bulk save is genuinely wanted later, implement it as a loop over `updateBaudRate()` so the validation and live re-init cannot be bypassed.

### F-125 · S4 · `Code/WCB/WCB_Storage.cpp:323`

**printBaudRates() and printKyberSettings() still hard-code Kyber on Serial2 / Serial1+Serial2, contradicting the configurable kyberLocalPort**

*Category:* `doc-drift`

Kyber's port is runtime-configurable and stored — `kyberLocalPort` (declared at WCB_Storage.h:51, loaded at line 1458 from the `K_Port` NVS key, set at line 1159 from `?KYBER,LOCAL,Sx`) — and the whole runtime path keys off it: `forwardDataFromKyber()` opens `getSerialStream(kyberLocalPort)` (WCB.ino:4244-4254) and `RawSerialForwardingTask` skips `inputPort == kyberLocalPort` (WCB.ino:6655). Two print paths in this file were never updated:

```
323:        else if (i == 1 && Kyber_Local) {
324:            Serial.print(" (Kyber)");
325:        }
```
`i == 1` is Serial2. So `?BAUD` labels Serial2 "(Kyber)" regardless of where Kyber actually is, and never labels the real port.

```
1481:  if (Kyber_Local) {
1482:    Serial.println("Initialized Serial1 & Serial2 for Kyber Local mode");
1483:  } else if (Maestro_Remote) {
1484:    Serial.println("Initialized Serial1 for Kyber Remote mode");
```
Neither line is true of the current code: nothing special is done to Serial1 for Kyber-local, and `storeKyberSettings()` itself prints the correct port two hundred lines earlier (`"Kyber is LOCAL on Serial%d"`, line 1160). `printKyberSettings()` is called right after every `?KYBER,…` command (WCB.ino:4656, 5225, 5234, 5237, 5240), so the contradiction appears in the same output block.

**Failure scenario.** User configures `?KYBER,LOCAL,S3`. The board correctly answers "Kyber is LOCAL on Serial3", then two lines later `printKyberSettings()` prints "Initialized Serial1 & Serial2 for Kyber Local mode", and `?BAUD` shows "(Kyber)" beside Serial2 with nothing beside Serial3. A user debugging a silent Kyber link reads the diagnostics, concludes the firmware is using S2, and re-wires the Kyber brain onto the wrong header — chasing a hardware fault that does not exist.

**Suggested fix.** At line 323, key off the real port: `else if (Kyber_Local && kyberLocalPort == i + 1) Serial.print(" (Kyber)");`. At lines 1481-1485, print the actual configuration — `Serial.printf("Kyber local on Serial%d\n", kyberLocalPort)` — or drop the two lines entirely, since `storeKyberSettings()` and `printKyberList()` already report it accurately.

### F-126 · S4 · `Code/WCB/WCB_Storage.cpp:1563`

**saveSerialMonitorSettings() is never called anywhere — serialMonitorEnabled[] / mirrorToUSB / mirrorToKyber are dead state and their status display always prints defaults**

*Category:* `dead-code`

`saveSerialMonitorSettings()` (:1563-1572) writes `mon1..mon5`, `mirror_usb` and `mirror_kyber` into the `serial_monitor` namespace. A recursive grep across all of `Code/WCB` finds exactly two references: the definition here and the declaration at WCB_Storage.h:226. Nothing calls it.

The read side runs every boot — `loadSerialMonitorSettings()` (:1761-1770) is called from setup at WCB.ino:7217 — but because nothing ever writes the namespace, it always returns the hard-coded defaults: `serialMonitorEnabled[i] = false`, `mirrorToUSB = true`, `mirrorToKyber = false`. Cross-checking the only consumers confirms the flags are never set anywhere else either: `serialMonitorEnabled[]` is written only at its definition (WCB.ino:595) and by this loader; its sole reader is the `?SM`-style status block at WCB.ino:2152-2163, so that block unconditionally prints "Serial Monitoring:" / "None" and never reaches the `Mirror to USB` / `Mirror to Kyber` lines at :2165-2166.

The live serial-monitoring feature is entirely the separate `serialMonitorMappings[]` / `serial_map` mechanism (:1535, :1830), which works. This is a leftover parallel design: three globals, two NVS functions, a namespace that `eraseNVSFlash()` dutifully clears (:990-992), and a status display that can never report anything but the default.

**Failure scenario.** Not a runtime failure — it is unreachable state. The concrete cost is misleading diagnostics and maintenance risk: a user reading the config dump sees "Serial Monitoring: None" on a board that *is* actively monitoring ports via `serialMonitorMappings`, and the next maintainer who wires a `?SM,ON` command to `serialMonitorEnabled[]` will find the setting silently fails to persist because the save function it would need is already written but never invoked.

**Suggested fix.** Delete `saveSerialMonitorSettings()`, `loadSerialMonitorSettings()`, the three globals (WCB.ino:595-597), the `serial_monitor` namespace entry in `eraseNVSFlash()` (:990-992), the header declarations (WCB_Storage.h:218-220, :225-226) and the dead status block at WCB.ino:2152-2166 — or, if the flags are meant to exist, wire a command to them and call the save. Either way they should not stay half-implemented into a release.

### F-127 · S4 · `Code/WCB/WCB_Storage.h:72`

**extern String storedCommands[MAX_STORED_COMMANDS] declares an object that no longer exists**

*Category:* `dead-code`

`WCB_Storage.h:72` declares `extern String storedCommands[MAX_STORED_COMMANDS];` and `WCB_Storage.cpp:23` repeats the same extern. A grep for `storedCommands` across all of `Code/WCB` returns only those two declarations plus two commented-out lines — the definition itself is commented out at WCB.ino:907 (`// String storedCommands[MAX_STORED_COMMANDS];`) and the only code that ever used it is the commented-out `loadStoredCommandsFromPreferences()` at WCB_Storage.cpp:543-553.

So the array is declared but not defined anywhere in the program; it links today only because nothing references it. `MAX_STORED_COMMANDS` (80) itself now has exactly one live use — the legacy key-scan bounds `for (int i = 1; i <= MAX_STORED_COMMANDS; i++)` at :885 and :901, which build the key names `"CMD1"`..`"CMD80"`. It is no longer any kind of capacity limit: `saveStoredCommandsToPreferences()` (:625-664) enforces no count at all, so the real ceiling on stored sequences is NVS space and the 4000-byte NVS string limit on `key_list`, neither of which is checked.

**Failure scenario.** Not a runtime failure — the extern is unreferenced, so no link error occurs. The trap is for the next maintainer: the header advertises an 80-slot in-RAM sequence cache and a `MAX_STORED_COMMANDS` capacity constant, so writing `storedCommands[i] = …` (the obvious thing to do given the declaration) produces an undefined-reference link failure, and sizing/validation work against `MAX_STORED_COMMANDS` would be guarding a limit the save path does not enforce.

**Suggested fix.** Delete the `extern String storedCommands[…]` at WCB_Storage.h:72 and WCB_Storage.cpp:23 along with the commented-out block at WCB_Storage.cpp:543-553 and WCB.ino:907. Keep `MAX_STORED_COMMANDS` but retitle its comment to what it now means (the legacy CMD#-key scan range), or introduce a real, enforced cap in `saveStoredCommandsToPreferences()`.

### F-128 · S4 · `Code/WCB/WCB_Variables.h:28`

**WCB_VAR_NAME_MAX comment says the 15-char limit is an "NVS key length limit", but variable names are never used as NVS keys**

*Category:* `doc-drift`

WCB_Variables.h:28 reads:

```
#define WCB_VAR_NAME_MAX     15    // NVS key length limit
```

and docs/VARIABLES_DESIGN.md:19 repeats it: "**Names:** 1–15 chars (NVS key limit)".

The implementation does not key NVS by variable name. `saveVarsToNVS` serialises the whole table into one newline-delimited `name=value` string and stores it under the single fixed key `"blob"` (WCB_Variables.cpp:76-93, `varPrefs.putString("blob", blob)` at :89), and `loadVariables` reads that one key back (:104). The file's own header comment at :17-19 says so explicitly: "Persisted to NVS as a single newline-delimited \"name=value\" blob under key \"blob\" in namespace \"wcb_vars\" (simple, no NVS iteration needed)." The only real NVS-key constraints in play are `"blob"` (4 chars) and the namespace `"wcb_vars"` (8 chars), both well inside the 15-char limit.

The 15-char limit is still a fine choice (it bounds `char name[16]` at :22 and keeps the worst-case blob at 100 x 28 = 2800 bytes, safely under the ~4000-byte NVS string cap), but the stated REASON is wrong. Per the repo convention that comments are constraints recording why a choice was made, a wrong reason misleads the next change.

**Failure scenario.** Someone asked to support longer variable names reads the comment, believes 15 is a hard NVS platform limit, and either refuses the change or (worse) 'fixes' persistence by switching to one NVS key per variable name — which would then genuinely hit the 15-char key cap and silently fail to persist, exactly the trap the current single-blob design avoids. Conversely, someone raising the cap on the comment's authority alone would not check the real constraint, the ~4000-byte NVS string limit against WCB_MAX_VARIABLES x (name+value+2).

**Suggested fix.** Change the comment to state the real constraints, e.g. `#define WCB_VAR_NAME_MAX 15  // bounds char name[16] and the single NVS "blob" string: WCB_MAX_VARIABLES*(15+11+2) must stay under the ~4000-byte NVS string cap`. Update docs/VARIABLES_DESIGN.md:19 to match and add a Revision log row.

### F-129 · S4 · `Code/WCB/WCB_WDP.cpp:359`

**Boot-burst advert timer uses a non-rollover-safe millis() comparison, unlike the rest of wdpTick**

*Category:* `wraparound`

In `wdpTick()` the periodic and dirty-check timers use the wraparound-safe idiom, but the boot-burst / solicited-advert timer does not:

```cpp
  if (wdpBootLeft > 0 && now >= wdpNextBootMs) {      // :359  — NOT rollover-safe
  ...
  if ((long)(now - wdpNextAdvertMs) >= 0) {           // :364  — safe
  ...
  if ((long)(now - wdpNextDirtyCheckMs) >= 0) {       // :371  — safe
```

`wdpNextBootMs` is set to `now + jitter` / `now + 1300` / `now + 800` at :348-350, :361, :381. When `millis()` wraps (~49.7 days), `now + delta` wraps to a small value while `now` is near `0xFFFFFFFF`, so `now >= wdpNextBootMs` is immediately true and the delay is skipped. `wdpArmSolicitedAdvert()` (:346-352) also uses the safe form for its "pull earlier" comparison, which makes the :359 line an outlier rather than a deliberate choice.

**Failure scenario.** A board that has been up ~49.7 days receives a SOLICIT (an operator hits `?WDP,POLL` from the Wizard). `wdpArmSolicitedAdvert()` sets `wdpNextBootMs = now + jitter`, which wraps past zero. The very next `wdpTick()` fires the advert with zero stagger instead of the intended 0-600 ms board-number jitter, so a whole fleet that wraps around the same time answers a poll in lockstep — the collision the jitter exists to prevent. The same window collapses a 3-advert boot burst into consecutive loop iterations.

**Suggested fix.** Change :359 to `if (wdpBootLeft > 0 && (long)(now - wdpNextBootMs) >= 0)`, matching :364 and :371.

### F-130 · S4 · `docs/VARIABLES_DESIGN.md:165`

**docs/VARIABLES_DESIGN.md §7 claims consecutive IFs do not AND — contradicting both the code and §3 of the same document**

*Category:* `doc-drift`

docs/VARIABLES_DESIGN.md:165-166 states:

> - No nesting: `IF,a=1^IF,b=1^M23` does **not** mean "a AND b" (the first IF just gates the second IF). Use compound `IF,a=1,AND,b=1` instead.

The code does exactly the opposite. In `ifGateConsumeToken` (WCB_Variables.cpp:402-417): the first IF evaluates and sets `ifSkipping = !pass` (:416); a following IF token when `ifSkipping` is already true is consumed WITHOUT clearing the flag (:405-410, "Already skipping … Net effect: the conditions AND together"); when the first IF passed, the second IF evaluates normally and sets the flag itself. So `IF,a=1^IF,b=1^M23` runs `M23` only when a=1 AND b=1 — the AND semantics.

The same document already says so correctly at §3, lines 110-112: "**Consecutive IFs AND together**: `IF,a=1^IF,b=2^cmd` runs `cmd` only when both hold (same result as the compound `IF,a=1,AND,b=2`)." WCB_Variables.h:64 states the AND semantics too. Line 165 is a leftover from the pre-shared-helper design and is now the only statement in the repo that disagrees; a reader who reaches §7 first will write a chain expecting the wrong gating.

**Failure scenario.** A user reads §7 "Limits & notes", concludes `IF,a=1^IF,b=1^;M2,goHome` only gates the second IF, and rewrites working chains — or, believing chained IFs are unsupported, avoids a form that in fact works and mis-diagnoses a chain that behaves 'unexpectedly' correctly. Any cold session (human or agent) reading the doc top-to-bottom gets two mutually exclusive statements about the same construct with no way to tell which is current.

**Suggested fix.** Delete the stale bullet at lines 165-166, or replace it with a pointer to §3: "- Consecutive IFs AND together (see §3); there is no nesting/precedence — compound `IF,a=1,AND,b=1` is equivalent and clearer." Add a dated row to the Revision log (§ starting line 182) noting the correction.

### F-131 · S4 · `docs/WCB_OTA_TECHNICAL.md:189`

**docs/WCB_OTA_TECHNICAL.md §4 'Relay side' describes a bare esp_now_send and omits the on-demand peer registration that relay OTA depends on**

*Category:* `doc-drift`

§4 says:

> "- `processOtaRelayCommand(\"?OTA,<SUB>,<target>,<session>,…\")` builds the matching struct and `esp_now_send`s it to `WCBMacAddresses[target-1]`."

The code no longer does that on either leg. Every OTA send now goes through `otaUnicast` (`Code/WCB/WCB_OTA.cpp:351-358`), used by the relay legs (`:492`, `:513`, `:524`) and by the target's ACK leg via `sendOtaAck` (`:372`). `otaUnicast` first calls `otaEnsurePeer` (`:336-344`), which registers the peer with `esp_now_add_peer` if `esp_now_is_peer_exist` is false, and then logs a non-OK `esp_now_send` result instead of dropping it silently.

The comment at `:330-335` records the failure this fixed: "esp_now_send() only reaches a REGISTERED peer. Normal unicasts register the target via addActivePeer(); the OTA send paths did not — so a target (or relay) that wasn't already mutually peered couldn't unicast the OTA ACK / frame, the send returned ESP_ERR_ESPNOW_NOT_FOUND and was silently lost, and the Wizard saw 'no response … via relay'." This is the substance of commits `992df4d` ("register the ESP-NOW peer before relay OTA sends (fixes no-ACK via relay)") and `cb9f29e` ("route every relay-OTA unicast through otaUnicast()").

The doc is the NaviCore porting reference (`:3`, checklist `:282`), so a port written from §4 as it stands reproduces the original bug rather than the fix. §9's checklist likewise says only "implement the target-side handlers + the size-based dispatch" with no mention of peering the relay before ACKing.

**Failure scenario.** A NaviCore port is written from §4/§9: the target-side handler ACKs with a bare `esp_now_send(relayMac, …)`. NaviCore and the relay WCB were never mutually peered (the relay only learned NaviCore from a WDP advert), so every ACK returns ESP_ERR_ESPNOW_NOT_FOUND and is dropped with no log. The relay's BEGIN reaches NaviCore and the erase runs, but no ACK ever comes back, and the Wizard reports 'no response from WCB<t> via relay — is it online & on this firmware?' — sending the engineer to hunt a firmware-version mismatch that does not exist.

**Suggested fix.** Update §4 'Relay side' to describe `otaUnicast()` = ensure-peer + unicast + log-on-failure, state that BOTH legs (relay→target frames and target→relay ACKs) must register the peer on demand because `esp_now_send` only reaches a registered peer, and note the 20-slot peer-table cap as the one remaining hard failure. Add the same item to the §9 porting checklist, plus a dated Revision-log row.

### F-132 · S4 · `docs/WCB_OTA_TECHNICAL.md:201`

**docs/WCB_OTA_TECHNICAL.md §5 states the relay ACK is printed inline with Serial.printf — the code deliberately defers it, and the doc's advice would reintroduce the cross-core hazard on a port**

*Category:* `doc-drift`

§5 ("THE critical rule: defer flash writes out of the receive callback") ends with:

> "- Only the **target-side** packets (BEGIN/DATA/END/ABORT) are deferred. The **relay-side** ACK (`handleOtaAckRelay`) is lightweight (just a `Serial.printf`) and stays inline."

The code does the opposite for the print. `handleOtaAckRelay` (`Code/WCB/WCB_OTA.cpp:442-456`) runs inline in the callback but explicitly does NOT print there — its comment at `:448-451` is a constraint recording a real hazard: "This runs in the ESP-NOW receive callback (WiFi task). ESP32 Serial isn't atomic across cores, and this `[OTA:ACK,...]` line is the browser's flow-control token — a direct write here can interleave with Core-1 output and garble it. Defer the print to loop() via the shared relay queue." It formats into a stack `char line[64]` and calls `otaRelayPrint(line)` (`:455`), which is `WCB.ino:387` → `enqueueRcJsonRelay(String(line))` → the 64-slot `rcJsonRelayQueue` drained by `drainRcJsonRelay()` in `loop()` (`WCB.ino:7479`).

The doc's stated purpose is "so it can be reproduced on **NaviCore**" (`:3`), and §5 is the section a porter is told to follow verbatim (`:234`). Following line 201 as written produces a WiFi-task `Serial.printf` of the browser's flow-control token — the exact defect the source comment was written to prevent.

(Related, same section: the doc does not mention that the relay-ACK output shares `rcJsonRelayQueue` with RC-JSON telemetry, or the `otaRelayForwardUntilMs` suppression added because a chatty controller could evict an OTA ACK from that queue — `WCB.ino:338-349`.)

**Failure scenario.** An engineer ports OTA to NaviCore following §5, implements `handleOtaAckRelay` with a direct `Serial.printf` inside the WCB_Client receive callback as instructed, and the `[OTA:ACK,…]` flow-control lines interleave with Core-1 output. The host's regex `\[OTA:ACK,<t>,<s>,(\d+),(\d+)\]` (Wizard/app.js:1714) fails to match the garbled line, every ACK reads as a lost frame, and relay OTA stalls to the 60-retry cap with no visible cause.

**Suggested fix.** Rewrite the bullet to state what the code does: the relay-side ACK stays inline in the callback only long enough to authenticate and format the line, then defers the Serial write to loop() via `otaRelayPrint()` / the shared relay queue — and note that a direct Serial write from the WiFi task garbles the flow-control token. Add a dated row to the Revision log.

### F-133 · S4 · `docs/WDP_DESIGN.md:211`

**docs/WDP_DESIGN.md DUMP format omits the EN= field the firmware actually emits in [WDPCFG:...]**

*Category:* `doc-drift`

The documented dump format shows

```
[WDPCFG:AUTOJOIN=..,PEERS=..]
```

but `printWdpDump()` emits three fields (WCB_WDP.cpp:1189-1190):

```cpp
  Serial.printf("[WDPCFG:EN=%d,AUTOJOIN=%d,PEERS=%d]\n",
                wdpEnabled ? 1 : 0, wdpAutoJoin ? 1 : 0, activePeerCount());
```

and the comment immediately above it (:1187-1188) explains why `EN` exists: "EN lets the Wizard distinguish 'WDP disabled here' from 'mesh just empty'." The Wizard already parses it, with `EN` optional for older firmware — `Wizard/app.js:12325`: `/^\[WDPCFG:(?:EN=(\d),)?AUTOJOIN=(\d),PEERS=(\d+)\]$/`. So the code and the tool agree and only the doc is stale.

**Failure scenario.** Someone writing a third-party consumer of `?WDP,DUMP` (or the WCB_Client twin) from docs/WDP_DESIGN.md §7 anchors a regex on `[WDPCFG:AUTOJOIN=` and fails to match every real firmware line, silently losing the auto-join/peer-count summary and the 'WDP disabled here' signal.

**Suggested fix.** Update line 211 to `[WDPCFG:EN=..,AUTOJOIN=..,PEERS=..]` and note that `EN` is absent on pre-6.x firmware (which is why the Wizard treats it as optional). Add a dated row to the §12 Revision log.

### F-134 · S4 · `docs/WDP_DESIGN.md:282`

**docs/WDP_DESIGN.md says Maestro remote-proxy auto-add is "first-host-wins" — the shipped behaviour is per-host**

*Category:* `doc-drift`

§9 states:

> On **first learn** of a peer-hosted Maestro or WLED, its `id -> board @ baud` is auto-added as a remote proxy (`maestroAutoAddRemote` / `wledAutoAddRemote`) from the `MAESTRO_CFG` / `WLED_CFG` TLVs — **first-host-wins**, persisted.

Both halves are wrong for Maestro. `maestroAutoAddRemote()` (WCB_Maestro.cpp:723-758) deliberately gives *every* advertising host its own `(id, host)` slot — its own comment says "This is per-host, NOT first-host-wins, and a proxy alongside a local Maestro of the same id is intended (multicast)" (WCB_Maestro.cpp:716-718), and WCB_WDP.cpp:689-691 repeats it ("Per-HOST (not first-host-wins)"). Only WLED is one-slot-per-id / first-host-wins (WCB_WLED.cpp:421-434). The repo CLAUDE.md rule 5 also states the per-host rule explicitly.

"On **first learn**" is also inaccurate: the loop at WCB_WDP.cpp:707-712 runs on every advert from a peer (it is idempotent, and it baud-refreshes an existing slot), not only on the first.

This matters because the doc's version implies the 9-slot Maestro table cannot be exhausted by duplicate ids across hosts — but it can (WCB_Maestro.cpp:743-749 logs and skips when full), which is the whole reason the per-host rule is written down elsewhere.

**Failure scenario.** A reader sizing a mesh (or debugging ";M3 fires on two boards") trusts the doc, concludes only the first advertising host gets a proxy, and either mis-diagnoses the intended multicast fan-out as a bug or fails to anticipate the shared 9-slot cap being exhausted by duplicate ids across several hosts.

**Suggested fix.** Rewrite the sentence as: "On every advert from a reachable peer, each Maestro it hosts is auto-added as its own `(id, host)` remote-proxy slot — **per-host, not first-host-wins** (duplicate ids across boards are legal and `;M<id>` fans out to all of them, capped at the 9 shared slots). WLED, by contrast, is strictly one slot per id (first-host-wins)." Add a dated row to the §12 Revision log.
