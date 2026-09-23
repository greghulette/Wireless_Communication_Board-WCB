> ## WCB vendored + patched copy — do not replace with stock upstream
>
> This is [EspSoftwareSerial](https://github.com/plerup/espsoftwareserial) **8.1.0** by Peter Lerup and
> Dirk O. Kaar, **vendored directly into the WCB sketch** and patched for the Wireless Communication Board.
> It lives at `Code/WCB/src/EspSoftwareSerial/` and is pulled in with a relative
> `#include "src/EspSoftwareSerial/SoftwareSerial.h"` (from `hcr.h`, `"../EspSoftwareSerial/SoftwareSerial.h"`),
> so **building the WCB firmware needs no EspSoftwareSerial install**, and a copy already installed in your IDE
> or sketchbook is ignored. No file under `Code/WCB` may include `SoftwareSerial.h` with angle brackets: that would compile the
> stock library alongside this one (the check is in `CLAUDE.md` rule 7).
>
> The WCB uses it for **receive only** on S3-S5. Transmit goes through an RMT channel (`WcbSoftSerial`,
> `Code/WCB/WCB_SoftSerial.{h,cpp}`); the library's bit-banged TX is only the fallback when no RMT channel is free.
>
> **WCB patches vs. upstream 8.1.0** (tracker #78, `docs/HIL_FIX_TRACKER.md`):
> - **Level-triggered receive on the classic ESP32** (`ESPSWSERIAL_LEVEL_RX`, set from `CONFIG_IDF_TARGET_ESP32`
>   in `SoftwareSerial.h`). ESP32 erratum GPIO-3.14 loses an **edge** interrupt on GPIO0-31 when the GPIO ISR's
>   STATUS / W1TC handling of another pin lands on it. The WCB measured it on its own S3-S5 pins: 227 of 10125
>   two- and three-port lines lost when the pins took edges ~1.5-3 µs apart, every single-port line exact
>   (HIL `softrx.erratum_pairs`, 2026-09-23). `enableRx()` now attaches `rxBitISR` as `ONLOW`/`ONHIGH` for the
>   level the line is *not* at, and `rxBitISR` re-arms the opposite level first (`gpio_ll_set_intr_type`, one
>   register write), then records a transition only when the level differs from the last one recorded, and
>   re-reads the pad and re-arms if the line moved while arming. What is left (a transition just before the IDF's
>   post-handler status clear, or the glitch-train cap exit) is caught because a level status re-asserts while its
>   condition holds. Espressif's documented workaround for the erratum.
> - **The ESP32-S3 is untouched:** `ESPSWSERIAL_LEVEL_RX` is 0 there, and `rxBitISR`, `enableRx()` and the class
>   layout compile exactly as upstream.
> - **`rxBitSyncISR` (above ~74880 baud, i.e. 115200) stays on its FALLING edge** on both chips. It busy-waits a
>   whole frame inside the ISR, so as a level interrupt a line held low would re-enter it forever.
> - **`rxLevelTriggered()`** added (public): true while RX runs on the level-emulated interrupt. The WCB prints it
>   at `?DEBUG` (`[SOFTSERIAL] S<n> RX: ...`).
> - **Include guard renamed** to `WCB_VENDORED_SOFTWARESERIAL_H`, with `__SoftwareSerial_h` left undefined and an
>   `#error` if it is already defined, so the stock header in the same build fails loudly instead of
>   linking two receivers.
> - **Copied:** `src/SoftwareSerial.{h,cpp}`, `src/circular_queue/{circular_queue.h, Delegate.h, ghostl.h}`
>   (`ghostl.h` is reached only on non-ESP builds; kept so every include resolves), `LICENSE`. **Not copied:** `examples/` (the
>   `circular_queue_mp_test` `.cpp` would be compiled by the sketch's `src/` build), `MultiDelegate.h`,
>   `circular_queue_mp.h` (unused).
>
> **Constraint the patch adds (CLAUDE.md rule 13):** `rxBitISR` rewrites the pin's `GPIO_PINn_REG` interrupt type
> from core 1's GPIO ISR without the IDF spinlock, so an S3-S5 RX pin is only ever (re)configured from core 1
> (`setup()`, `loop()`, `applyLiveBaud()`), never from a core-0 task.
>
> The probe firmware (`tests/hil/wcb_probe`) and the Arduino-Code sketchbook keep the **stock** library.
>
> Upstream license (GNU LGPL 2.1 or later) is preserved in `LICENSE`, and the copyright headers in the sources are
> unchanged; every modification is marked `WCB patch` in the source. To pull a newer upstream, re-apply the
> patches above rather than dropping a stock copy in place.
