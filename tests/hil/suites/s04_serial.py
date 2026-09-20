""";S serial writes, ^ chains and ;T timers, measured on the wire by probe1 (W1 S1).

Timer deltas use the probe's own clock for both markers, so USB latency cancels out.
"""
import time

from hil.runner import test
from suites.common import marker, usb_wcb, wire

NEEDS = ["wcb1", "probe1"]


@test("serial.comma", ";S1,<text> strips the optional comma", needs=NEEDS)
def comma(bench):
    probe, ch = wire(bench, 1, "S1")
    t = marker()
    m = probe.dev.mark()
    usb_wcb(bench).send(f";S1,{t}")
    got = probe.expect_bytes(ch, t.encode() + b"\r", timeout=2, since=m)
    assert b"," + t.encode() not in got, f"comma leaked onto the wire: {got!r}"


@test("serial.s0", ";S0,<text> prints the text on USB", needs=["wcb1"])
def s0(bench):
    w = usb_wcb(bench)
    t = marker()
    m = w.send(f";S0,{t}")
    w.dev.expect(rf"^{t}$", timeout=2, since=m)


@test("serial.chain", "A ^ chain of ;S writes arrives complete and in order", needs=NEEDS)
def chain(bench):
    probe, ch = wire(bench, 1, "S1")
    a, b, c = marker("a"), marker("b"), marker("c")
    m = probe.dev.mark()
    usb_wcb(bench).send(f";S1{a}^;S1{b}^;S1{c}")
    probe.expect_bytes(ch, f"{a}\r{b}\r{c}\r".encode(), timeout=3, since=m)


@test("serial.bad_port", ";S7 is rejected with the port-range message", needs=["wcb1"])
def bad_port(bench):
    lines = usb_wcb(bench).run(";S7,x")
    assert any("Invalid serial port number" in t for t in lines), f"got {lines}"


def _timer_gap(bench, line, first, second, timeout):
    probe, ch = wire(bench, 1, "S1")
    m = probe.dev.mark()
    usb_wcb(bench).send(line)
    probe.expect_bytes(ch, second.encode(), timeout=timeout, since=m)
    t0 = probe.time_of(ch, first.encode(), m)
    t1 = probe.time_of(ch, second.encode(), m)
    assert t0 is not None, f"first marker {first} never arrived"
    return t1 - t0


@test("serial.timer", ";T1500 between two writes delays the second by ~1.5 s", needs=NEEDS)
def timer(bench):
    a, b = marker("a"), marker("b")
    gap = _timer_gap(bench, f";S1{a}^;T1500^;S1{b}", a, b, timeout=4)
    bench.note(f"timer gap {gap} ms")
    assert 1400 <= gap <= 1800, f"gap {gap} ms, expected ~1500"


@test("serial.timer_sum", "Consecutive ;T delays add up (600 + 600)", needs=NEEDS)
def timer_sum(bench):
    a, b = marker("a"), marker("b")
    gap = _timer_gap(bench, f";S1{a}^;T600^;T600^;S1{b}", a, b, timeout=4)
    bench.note(f"timer gap {gap} ms")
    assert 1100 <= gap <= 1500, f"gap {gap} ms, expected ~1200"


@test("serial.timer_stop", "?STOP cancels the rest of a running timer sequence", needs=NEEDS)
def timer_stop(bench):
    probe, ch = wire(bench, 1, "S1")
    w = usb_wcb(bench)
    a, b = marker("a"), marker("b")
    m = probe.dev.mark()
    w.send(f";S1{a}^;T2500^;S1{b}")
    probe.expect_bytes(ch, a.encode(), timeout=2, since=m)
    wm = w.send("?STOP")
    w.dev.expect(r"Timer sequence manually stopped", timeout=2, since=wm)
    time.sleep(3.5)
    got = probe.received(ch, m)
    assert b.encode() not in got, f"{b} was sent after ?STOP: {got!r}"
