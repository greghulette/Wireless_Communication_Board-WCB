"""The harness's own guards, checked against a board put on purpose into the state they are meant to recognise.

hil/silence.py runs after every failed test: a USB-attached WCB that stopped answering is diagnosed - HELD IN RESET
(its USB-serial driver raised RTS by itself; writing RTS/DTR low again on the open handle releases it) or HUNG (that
changes nothing; a reset pulse does) - and brought back before the next test. W1 went silent twice in
intellex.flash_navicore_app (runs 20261014-173713, 20261015-091259) and every look by hand reset it first.
"""
import re
import time

from hil import silence
from hil.runner import test
from suites.common import usb_wcb


@test("bench.silent_wcb_held_in_reset", "A WCB held in reset through its own open console (RTS raised on the harness's "
      "handle, the state a USB-serial driver can leave by itself) is found by the after-failure check, called HELD IN "
      "RESET, and answers ?VERSION again afterwards; the other WCBs, answering, are left alone (hil/silence.py)",
      needs=["wcb1"])
def silent_wcb_held_in_reset(bench):
    w1 = usb_wcb(bench)
    dev = w1.dev
    s = dev._ser
    assert s is not None, f"{dev.name}'s port is not open"
    try:
        s.rts = True           # EN low through the auto-reset circuit, DTR low so IO0 is not strapped
        s.dtr = False
        time.sleep(1.0)
        assert not silence._answers(dev), (f"{dev.name} still answered ?VERSION with RTS raised: RTS is not this "
                                           f"board's reset line, so this test proves nothing here")
        lines = silence.check(bench)
    finally:
        try:
            s.rts = False
            s.dtr = False
        except Exception:  # noqa: BLE001
            pass
    mine = [x for x in lines if x.startswith(dev.name)]
    assert len(lines) == 1 and mine, f"the check reported {lines}; expected one line, for {dev.name}"
    assert "was HELD IN RESET" in mine[0] and "it answers ?VERSION now" in mine[0], mine[0]
    w1.version()


class _Ser:
    """pyserial's rts/dtr as a board's reset wiring sees them: every write reaches `board`."""

    def __init__(self, board):
        self.board, self._rts, self._dtr = board, False, False

    @property
    def rts(self):
        return self._rts

    @rts.setter
    def rts(self, v):
        self._rts = v
        self.board.lines_changed(self._rts, self._dtr)

    @property
    def dtr(self):
        return self._dtr

    @dtr.setter
    def dtr(self, v):
        self._dtr = v
        self.board.lines_changed(self._rts, self._dtr)


class _Board:
    """A WCB console with no hardware: 'held' (in reset until RTS is written low again), 'hung' (until a reset pulse:
    RTS high, then low) or 'slow' (silent for the first ?VERSION only)."""

    def __init__(self, kind):
        self.name, self.kind, self.lines, self.active = f"fake-{kind}", kind, [], True
        self.alive, self.asked, self.held_low_writes, self.was_high = False, 0, 0, False
        self._ser = _Ser(self)

    def lines_changed(self, rts, dtr):
        if rts:
            self.was_high = True
            self.alive = False
            return
        if self.kind == "held" and not self.alive:
            self._boot()
        elif self.kind == "hung" and self.was_high:
            self._boot()

    def _boot(self):
        self.alive, self.was_high = True, False
        self.lines += ["Reset reason: 1 - Power-on Reset", "Booting up the Wireless Communication Board 9 (W9)"]

    def mark(self):
        return len(self.lines)

    def since(self, m):
        return self.lines[m:]

    def send(self, cmd):
        self.asked += 1
        if self.kind == "slow" and self.asked >= 3:
            self.alive = True
        if self.alive and cmd == "?VERSION":
            self.lines.append("Software Version: 9.9.9_HIL")

    def expect(self, pattern, timeout=3, since=None):
        end = time.monotonic() + min(timeout, 0.3)
        rx = re.compile(pattern)
        while time.monotonic() < end:
            for x in self.lines[since or 0:]:
                m = rx.search(x)
                if m:
                    return m
            time.sleep(0.02)
        raise AssertionError(f"{self.name}: no line matching /{pattern}/")


class _Bench:
    def __init__(self, board):
        self.devs, self.notes = {"wcb9": board}, []

    def usb_wcbs(self):
        return {9: "wcb9"}

    def note(self, text):
        self.notes.append(text)


@test("bench.silence_unit", "hil/silence.py against boards with no hardware: one held in reset comes back when RTS/DTR "
      "are written low again (HELD IN RESET), a hung one only after a reset pulse (HUNG), a slow one is not called "
      "stuck, and an answering one is not touched (no board)")
def silence_unit(bench):
    saved = silence.QUIET_S
    silence.QUIET_S = 0.2
    problems = []
    try:
        for kind, want in (("held", "was HELD IN RESET"), ("hung", "HUNG: silent, RTS/DTR low again changed nothing"),
                           ("slow", "slow, not stuck")):
            b = _Board(kind)
            out = silence.check(_Bench(b))
            if len(out) != 1 or want not in out[0]:
                problems.append(f"{kind}: {out}, expected one line with {want!r}")
            elif kind != "slow" and "it answers ?VERSION now" not in out[0]:
                problems.append(f"{kind}: not brought back: {out[0]}")
        b = _Board("held")
        b.alive = True
        if silence.check(_Bench(b)) or b.held_low_writes:
            problems.append("an answering board was diagnosed")
    finally:
        silence.QUIET_S = saved
    assert not problems, "; ".join(problems)
