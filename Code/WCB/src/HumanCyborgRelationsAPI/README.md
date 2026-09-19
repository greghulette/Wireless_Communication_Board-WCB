> ## ⚠️ WCB vendored + patched copy — do not replace with stock upstream
>
> This is the [HumanCyborgRelationsAPI](https://github.com/roy86/HumanCyborgRelationsAPI)
> by roy86, **vendored directly into the WCB sketch** and patched for the
> Wireless Communication Board. It lives at `Code/WCB/src/HumanCyborgRelationsAPI/`
> and is pulled in with a relative `#include "src/HumanCyborgRelationsAPI/hcr.h"`,
> so **building the WCB firmware needs no library install** — and any HCR library
> already installed in your IDE is ignored (the bundled copy always wins).
>
> **WCB patches vs. upstream:**
> - **I2C/TwoWire removed** — the WCB drives the HCR over a serial UART only.
>   `hcr.h` `#define`s `TwoWire_h` before `#include <Wire.h>` (neutering it) and
>   every I2C constructor / transfer in `hcr.cpp` is commented out.
> - **`<String.h>` → `<string.h>`** (lowercase) so the build works on
>   case-sensitive filesystems (Linux CI), not just Windows/macOS.
> - **`PlayWAV()` debounce** is per channel and 150 ms (upstream: one global 5 s).
> - **`GetVolume(ch)`** added — the cached volume, no transmit. `getVolume()`
>   still sends `<QVx>` and returns the value cached before the reply.
> - **Dataframe key `QD`** accepted (the documented reply to `<QD>`); upstream
>   matched only `DF`, so the dataframe never parsed. Needs exactly 11 values.
> - **Receive framing:** `HCR_BUFFER_SIZE` 32 → 64, `<` resets the frame, only `>`
>   commits one (a CR/LF/NUL before it discards the partial frame), and an
>   over-long frame is dropped instead of parsed truncated.
> - **Reply validation:** every value field must be numeric, the field count must
>   match the key (2; `QE` 5; `QD`/`DF` 12), and `QVx` must be 0-100; anything else
>   is rejected (`onFrame(..., false)`) without touching the cache.
> - **`receive()` drains** up to `HCR_BUFFER_SIZE` waiting bytes per `update()`
>   (upstream: one byte), so a full reply set can't overflow the soft-serial buffer.
> - **`onFrame(frame, parsed)`** — protected virtual hook called after every
>   received frame. The library's own `Serial` prints never reach USB on the WCB.
> - **Virtual destructor** — the class now has a virtual hook, so it gets one.
>
> Upstream license is preserved in `LICENSE`. To pull a newer upstream, re-apply
> the patches above rather than dropping a stock copy in place.

---

![Banner](/img/hcr-banner.png?raw=true "Employee Data title")

# Human Cyborg Relations C++ API
C++ based API for interfacing with Michael Perl's [Human Cyborg Relations](https://humancyborgrelations.com/r2d2/) Teensy Based Vocalizers

## Overview

[Human Cyborg Relations](https://humancyborgrelations.com/r2d2/), created by Michael Perl, has set a new standard for Droid Vocalization through a fully functional AI, powered by advanced research into the speech patterns and emotional logic of R2-D2.

Each vocalization is unique. A sophisticated algorithm assembles R2’s speech "phoneme" by "phoneme." Billions upon billions of rules dictate how each sound should be selected, processed, timed, and sequenced to achieve emotional and canonical accuracy.

The [Human Cyborg Relations R2 Vocalizer](https://humancyborgrelations.com/r2d2/) has been ported to the Teensy 4.1 Hardware for stand-alone integration into custom droids build for the Astromech community usage. The WIKI and CODE here relates to the C++ Library created for Arduino/Teensy/ESP projects to allow people to interface their boards with the HCR R2 Vocalizer programmed Teensy 4.1 + Audio Shield.

Please when using this library, we encourage everyone to make a small donation directly to and support [Force for Change](https://www.starwars.com/force-for-change) and [FIRST®](https://www.firstinspires.org/)

## Documentation
### [API WIKI](https://github.com/roy86/HumanCyborgRelationsAPI/wiki)


