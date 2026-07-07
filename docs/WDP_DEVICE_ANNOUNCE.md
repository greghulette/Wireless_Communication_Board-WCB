# WDP‑DA — Device Announce

**A way for a device wired to a WCB serial port to tell the WCB what it is.**

**Status:** Draft / for builder review. WCB firmware + config‑tool support not yet implemented.
**Who this is for:** Anyone building a device (Arduino, ESP, PIC, …) that plugs into a WCB. You do **not** need to modify any WCB code — this is entirely on your device.

---

## What WDP is

Wireless Communication Boards talk to each other over a wireless (ESP‑NOW) mesh. **WDP — the Wireless Discovery Protocol — is how they automatically learn about each other.** Every WCB periodically broadcasts a short "here's who I am and what I've got" message; every other WCB listens and builds a live picture of the whole droid — which boards exist, what they're called, and what's attached to them. It's the same idea as network switches discovering their neighbors: you don't wire up a map by hand, the network learns itself.

The config tool (the "Wizard") reads that same picture, so plugging in a board and opening the tool shows you the whole droid with no manual setup.

## Why we want your device to use this

Today WDP knows about the WCBs and whatever a person has *typed in* about each serial port. Your device — a smart board with its own microcontroller — can do better: it can **introduce itself.** When it does:

- It shows up automatically, everywhere on the droid, with the **right name** — nobody has to know or type "port 2 is a Flthy HP Controller."
- Its **firmware version** becomes visible across the whole droid, so anyone maintaining it can see at a glance whether it's current — without unplugging anything or opening your device up.

A few lines in your sketch make your device a self‑describing part of the droid instead of an anonymous thing on a wire.

## What we're asking you to do

Send **one short line of text** to the WCB, over the serial connection you already use, **every 25–30 seconds.** That's the whole protocol. The rest of this document is the exact format, a copy‑paste example, and a few optional extras.

---

## The message

One line, ending in a newline:

```
@WDP1 {"type":"Flthy HP Controller","fw":"2.3.0"}
```

- It **must** begin with `@WDP1` — that marker is how the WCB picks your announcement out of ordinary serial traffic. `1` is the format version.
- One space, then a single JSON object.
- End with `\n` (a trailing `\r` is fine).
- Text is UTF‑8. Keep the whole line **200 characters or under**.
- Anything the WCB can't recognize as a `@WDP` line is simply ignored, so announcing is always safe.

## The fields

| Key | Required | Meaning |
|---|:---:|---|
| `type` | **yes** | What kind of device this is — a name from the shared list (see below). |
| `fw` | **yes** | Your firmware version, in whatever form you version it: `"2.3.0"`, `"v11"`, `"2026‑07‑01"`. |
| `hw` | no | Hardware **revision**, if your board has them: `"revB"`, `"1.2"`. |
| `caps` | no | Capability tags — a list of what your device does (see below). |

Only send facts your device is the authority on — what it is, its firmware, its hardware. **Don't send an instance name** like "Front HP": your device has no way to know whether it's the front or rear one. That's assigned by the person setting up the droid, in the config tool.

```
minimum:  @WDP1 {"type":"Flthy HP Controller","fw":"2.3.0"}
fuller:   @WDP1 {"type":"Flthy HP Controller","fw":"2.3.0","hw":"revB","caps":["hp.servo","hp.led"]}
```

## Device names

`type` should be one of the shared names, so a device is called the same thing on every droid. The current list:

```
Maestro · Marcduino · DroidNet · Magic Panel(IA) · Magic Panel(PRINTDRD) ·
Roam‑A‑Dome · MP3 Trigger · H‑CR · Stealth · Padawan · Shadow · Shadow RC ·
Penumbra · Sabé · NaviCore · Periscope · Uppity Spinner · Life Form Scanner ·
Leia Projector · Saber Launcher · Short Circuit · ARDS/K‑ARDS · Teeces ·
AstroPixels · Rseries Logics · PSI Front · PSI Rear · HP Controller ·
Flthy HP Controller · Data Panel/CBI · Benduino · WLED
```

Copy the spelling exactly. If your device isn't on the list, you can still announce a custom name — it's accepted as‑is. To get it added to the shared list, add a line to `Wizard/device-labels.js` and open a pull request.

## Capability tags (optional)

`caps` is an optional list describing *what your device does*. Nothing requires it, and nothing depends on it yet — including it just makes your device ready for future capability‑aware features. Use lowercase `domain.function` tags, for example:

`hp.servo` · `hp.led` · `audio.trigger` · `audio.play` · `led.wled` · `led.pixel` · `led.logic` · `servo.maestro` · `servo.pwm` · `panel.magic` · `panel.data` · `dome.spin` · `dome.periscope` · `sensor.lifeform`

## When to send it

Send the line **every 25–30 seconds**, picking a fresh random interval in that range each time.

- Repeating keeps the WCB's picture current and means a WCB that reboots — or that you plug your device into later — learns about you within half a minute.
- **Randomize the interval** so that devices which all powered on together don't transmit in lockstep. Important: seed your random generator from something that differs board‑to‑board, or every board running the same firmware will pick the *same* "random" timing and stay synchronized anyway.

Keep it non‑blocking (schedule with `millis()`, never `delay()`), so announcing never stalls your device's real work.

## Which serial port

Use **whatever serial connection your sketch already uses to talk to the WCB** — `Serial`, `Serial1`, a `SoftwareSerial`, whatever. The example below takes the port as a parameter so you just pass in the one you already have.

## Wiring & baud

- Your announcement travels **your TX → the WCB's RX**, so that pair must be wired. (A hookup that only wires your RX can't send an announcement.)
- **Match the baud rate** the WCB is configured to use for that port. There's no auto‑detect — if the rates differ, the WCB just sees garbage and ignores it.
- WCB serial pins are **3.3 V**. Don't drive a WCB RX pin from a 5 V output without a level shifter.

---

## Copy‑paste example (Arduino, no libraries)

```cpp
// ── Your device's identity — edit these two lines ────────────────────
const char* WDP_TYPE = "Flthy HP Controller";   // a name from the shared list
const char* WDP_FW   = "2.3.0";                  // your firmware version

// ── Build & send the announcement. Works with any serial port. ───────
void wdpAnnounce(Stream& wcb) {
  wcb.print(F("@WDP1 {\"type\":\""));
  wcb.print(WDP_TYPE);
  wcb.print(F("\",\"fw\":\""));
  wcb.print(WDP_FW);
  wcb.println(F("\"}"));
}

// ── Non-blocking scheduler: sends every 25–30 s, jittered. ───────────
unsigned long _wdpNextMs = 0;
void wdpTick(Stream& wcb) {
  if ((long)(millis() - _wdpNextMs) < 0) return;   // not time yet (rollover-safe)
  wdpAnnounce(wcb);
  _wdpNextMs = millis() + random(25000, 30001);     // next: 25.000–30.000 s
}

void setup() {
  Serial1.begin(115200);                 // ← the port + baud your sketch already uses
  randomSeed(analogRead(A0));            // seed from something board-varying so each
                                         //   board's jitter differs (ESP32: esp_random())
  _wdpNextMs = millis() + random(500, 3000);   // first announce a few seconds after boot
}

void loop() {
  wdpTick(Serial1);                      // ← pass YOUR serial port
  // ...the rest of your device's code...
}
```

**To add the optional fields**, extend `wdpAnnounce` — the values still live as clean constants up top:

```cpp
const char* WDP_HW = "revB";

void wdpAnnounce(Stream& wcb) {
  wcb.print(F("@WDP1 {\"type\":\""));  wcb.print(WDP_TYPE);
  wcb.print(F("\",\"fw\":\""));        wcb.print(WDP_FW);
  wcb.print(F("\",\"hw\":\""));        wcb.print(WDP_HW);
  wcb.print(F("\",\"caps\":[\"hp.servo\",\"hp.led\"]"));
  wcb.println(F("}"));
}
```

---

## What the WCB does with it (for reference)

You don't implement any of this — it's just so you know what to expect:

1. It records, for that port, what your device announced: its type, firmware, and any hardware rev / capabilities.
2. That information rides up into the mesh, so the config tool and other boards show, per port: `S2  Flthy HP Controller  fw 2.3.0`.
3. If nobody has manually labeled that port, your `type` fills it in automatically. If someone *has* set a label, theirs is shown, but your details are still recorded alongside it.

## Checklist

- [ ] On boot, and every 25–30 s after, send one `@WDP1 {…}` line.
- [ ] Include `type` (from the shared list) and `fw`.
- [ ] Randomize the interval, seeded per board.
- [ ] Send it on the port and baud your sketch already uses to reach the WCB.
- [ ] Keep it non‑blocking.

## Questions

**Do I need a JSON library?** No — the example builds the line with plain prints.

**My device only sends to the WCB (no receive line). OK?** Yes — announcing is transmit‑only.

**Two of my devices are the same type on different ports — is that a problem?** No. Each port is tracked separately; telling them apart ("front" vs "rear") is the user's job in the config tool.

**Will this break anything on an older WCB that doesn't know about this yet?** No — it just sees an unrecognized line and ignores it.
