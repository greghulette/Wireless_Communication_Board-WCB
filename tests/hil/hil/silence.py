"""A WCB that stopped answering on its own USB console: which kind of silence it is, and the board brought back.

W1 went silent twice in intellex.flash_navicore_app (runs 20261014-173713 and 20261015-091259): its last line the
test's opening ?backup, then nothing through NaviCore's reflash and after; off the mesh (NaviCore did not hear it for
3+ min), and not in the ROM bootloader (esptool --before no-reset found nothing to sync with). Any new open of the port
resets the board, so each look by hand destroyed the state being looked at, and the five tests after it failed on a
board nobody had brought back. Two explanations fit and this tells them apart without a new open:

- held in reset: W1's bridge is a WCH chip on macOS's own usbserial driver, and RTS is the auto-reset circuit's EN.
  A driver that raised RTS by itself (a USB event on the hub - NaviCore re-enumerates as esptool resets it) holds the
  chip in reset while the harness still believes RTS is low. Writing RTS and DTR low again on the handle already open
  (pyserial sends the ioctl whatever the remembered value) releases it, and the board boots at once.
- hung: the firmware stopped with no watchdog firing. Writing the same line state again changes nothing; only a reset
  pulse (RTS high, then low) brings it back.

check() runs after a test that failed or errored (runner._run_tests): one ?VERSION per USB-attached WCB, and only for
a silent one the diagnosis, the recovery, and a line saying which it was.
"""
import time

from .serialdev import usb_jtag_reset
from .wcb import WCB

BOOT_LINE = "Reset reason:"        # Code/WCB/WCB.ino's boot banner, printed early on every start
QUIET_S = 4.0                      # nothing at all on the console for this long, then a second ?VERSION unanswered
HANG_LOOKBACK = 400                # console lines searched for the board's own [HANG] line


def _answers(dev):
    try:
        WCB(dev).version()
        return True
    except Exception:  # noqa: BLE001 - silence is what is being looked for
        return False


def _boot_seen(dev, mark, wait_s):
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        hit = [x.strip() for x in dev.since(mark) if x.startswith(BOOT_LINE)]
        if hit:
            return hit[0]
        time.sleep(0.2)
    return None


def diagnose(dev):
    """-> one line: what kind of silence `dev` (an open WCB console that did not answer ?VERSION) was, and whether it
    answers now."""
    name = dev.name
    s = dev._ser
    if s is None:
        return f"{name} silent and its port not open: not diagnosed"
    # What the board said about itself before going quiet: WCB_LoopWatch prints '[HANG] loop() stuck N s in <step>'
    # through the ROM's UART print once loop() has made no pass for 15 s, even with every lock held.
    try:
        hang = [x.strip() for x in dev.since(max(0, dev.mark() - HANG_LOOKBACK)) if x.startswith("[HANG] loop() stuck")]
    except Exception:  # noqa: BLE001
        hang = []
    said = f" Its last word: {hang[-1][:240]!r}." if hang else " It printed no [HANG] line."
    m = dev.mark()
    try:
        s.rts = False          # the line state the harness opened it with (serialdev._open_port_now), written again
        s.dtr = False
    except Exception as e:  # noqa: BLE001
        return f"{name} silent; RTS/DTR could not be written again ({e}): not diagnosed"
    boot = _boot_seen(dev, m, 5.0)
    if boot:
        time.sleep(4.0)        # the rest of the banner, WiFi and ESP-NOW up
        ok = _answers(dev)
        return (f"{name} was HELD IN RESET: silent, and it booted the moment RTS/DTR were written low again on the "
                f"open port ({boot!r}) - its USB-serial driver had raised RTS by itself; "
                f"{'it answers ?VERSION now' if ok else 'it still does not answer ?VERSION'}.{said}")
    if _answers(dev):
        return (f"{name} did not answer ?VERSION, then did with nothing done but RTS/DTR written low again: slow, not "
                f"stuck.{said}")
    m = dev.mark()
    try:
        usb_jtag_reset(dev)   # RTS high with DTR low: EN on an auto-reset circuit as on the S3's USB-Serial/JTAG
    except Exception as e:  # noqa: BLE001
        return f"{name} HUNG (RTS/DTR low again changed nothing); the reset pulse failed: {e}.{said}"
    boot = _boot_seen(dev, m, 8.0)
    time.sleep(4.0)
    ok = _answers(dev)
    return (f"{name} HUNG: silent, RTS/DTR low again changed nothing, and a reset pulse "
            f"{'restarted it (' + repr(boot) + ')' if boot else 'showed no boot banner'}; "
            f"{'it answers ?VERSION now' if ok else 'it STILL does not answer ?VERSION'}.{said}")


def check(bench):
    """-> [line] for every USB-attached WCB whose open console does not answer ?VERSION, each diagnosed and, when it
    can be, brought back. [] when they all answer. Never raises."""
    out = []
    for _num, name in sorted(bench.usb_wcbs().items()):
        try:
            dev = bench.devs.get(name) if hasattr(bench, "devs") else None
            if dev is None or not getattr(dev, "active", False) or _answers(dev):
                continue
            # Quiet, not merely busy: a board the test left rebooting prints its banner here, and one that printed
            # anything in the last QUIET_S is not what this is for. Asked again after the wait.
            m = dev.mark()
            time.sleep(QUIET_S)
            if dev.since(m) or _answers(dev):
                continue
            line = diagnose(dev)
        except Exception as e:  # noqa: BLE001 - a check never breaks the run
            line = f"{name}: the silence check failed ({e})"
        bench.note(f"SILENT WCB: {line}")
        out.append(line)
    return out
