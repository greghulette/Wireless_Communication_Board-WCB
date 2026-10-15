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


HANG_STUCK = re.compile(r"^\[HANG\] loop\(\) stuck (\d+) s in (.+?) \(loop task (\w+)\); WS lock held by (.+?); "
                        r"heap free (\d+), min (\d+)$")


@test("wcb.loop_hang_reported", "With loop() stopped on purpose for 20 s (?DEBUG,FAULT,LOOPSTALL,20, a test knob that "
      "blocks it inside that command), W1's loop watch prints '[HANG] loop() stuck 15 s in handleSingleCommand running "
      "?DEBUG (loop task blocked)' - the command's verb, never its arguments - once, then '[HANG] loop() running again' "
      "when it moves, and W1 answers ?VERSION after (Code/WCB/WCB_LoopWatch.cpp)", needs=["wcb1"])
def loop_hang_reported(bench):
    """What a board whose loop() stopped now says about itself: W1 went silent twice in long runs and nothing on its
    console said where. The watch is an esp_timer reading loop()'s pass count once a second; it prints with
    esp_rom_printf, which takes no lock a stuck task could hold. 15 s (LOOP_STUCK_MS), then every 30 s while stuck."""
    w1 = usb_wcb(bench)
    dev = w1.dev
    m = dev.mark()
    dev.send("?DEBUG,FAULT,LOOPSTALL,20")
    dev.expect(r"^\[FAULT\] LOOPSTALL: loop\(\) stops for 20 s now", timeout=4, since=m)
    hit = dev.expect(HANG_STUCK.pattern, timeout=22, since=m)
    secs, where, state, holder = int(hit.group(1)), hit.group(2), hit.group(3), hit.group(4)
    problems = []
    if not 15 <= secs <= 17:
        problems.append(f"reported stuck {secs} s, expected 15-17 (LOOP_STUCK_MS 15 s, a 1 s watch)")
    if where != "handleSingleCommand running ?DEBUG":
        problems.append(f"named {where!r}, expected 'handleSingleCommand running ?DEBUG' (the verb only)")
    if state != "blocked":
        problems.append(f"loop task {state!r}, expected 'blocked' (delay())")
    again = dev.expect(r"^\[HANG\] loop\(\) running again after (\d+) s$", timeout=12, since=m)
    dev.expect(r"^\[FAULT\] LOOPSTALL: loop\(\) runs again$", timeout=5, since=m)
    stuck_lines = [x for x in dev.since(m) if x.startswith("[HANG] loop() stuck")]
    if len(stuck_lines) != 1:
        problems.append(f"{len(stuck_lines)} stuck reports for a 20 s stall, expected 1 (the next only after 30 s)")
    if "LOOPSTALL" in where or ",20" in " ".join(stuck_lines):
        problems.append("the report carries the command's arguments")
    bench.note(f"wcb.loop_hang_reported: {hit.group(0)[:200]!r}; then {again.group(0)!r} (WS lock: {holder})")
    w1.version()
    assert not problems, "; ".join(problems)
