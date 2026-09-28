""";S serial writes, ^ chains and ;T timers, measured on the wire by probe1 (W1 S1).

Timer deltas use the probe's own clock for both markers, so USB latency cancels out.
"""
import time

from hil.runner import test
from hil.wcb import WCB
from suites.common import config_guard, link, marker, usb_wcb, wire

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


@test("serial.timer_after_func_command", "A ;T chain that starts with a ? command keeps its delay: ?VERSION^;S1a^;T800^;S1b and ?CONTROLLER^... (the ?C checksum branch) space the writes about 800 ms, with no 'Invalid Serial Command' (re-scan #10)", needs=NEEDS)
def timer_after_func_command(bench):
    """WCB coverage re-scan #10 (docs/hil_plan/WCB.md WCB-WP29 row 1, arm 1). A chain starting with the function
    identifier never reached the timer engine - it was refused outright, to keep ?SEQ,SAVE values away from it - so its
    ;T went to the plain splitter ('Invalid Serial Command') and both writes went out together. isTimerChain (WCB.ino)
    sends it to the timer engine when a token is a real ;T<digits> and nothing in the chain carries a value or a
    checksum. A chain starting ?C takes the console's checksum branch first (processSerialCommandHelper), which returned
    through the plain splitter before the timer gate: ?CONTROLLER^... is that arm. A ;T counts from the previous group's
    dispatch, not its end, so the arms use commands that print a line or two: ?CONFIG's ~200 ms of UART0 output made
    the gap 591 ms (run 20260928-004312)."""
    w = usb_wcb(bench)
    problems = []
    for head in ("?VERSION", "?CONTROLLER"):
        a, b = marker("a"), marker("b")
        wm = w.dev.mark()
        gap = _timer_gap(bench, f"{head}^;S1{a}^;T800^;S1{b}", a, b, timeout=6)
        time.sleep(0.5)
        bench.note(f"{head}: timer gap {gap} ms")
        if any("Invalid Serial Command" in x for x in w.dev.since(wm)):
            problems.append(f"{head}: the ;T was run as a plain command")
        if not 700 <= gap <= 1100:
            problems.append(f"{head}: gap {gap} ms, expected ~800")
    assert not problems, "; ".join(problems)


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


@test("serial.timer_stop_mesh", "?STOP from the mesh stops a running timer chain: W2's chain, typed on its own console, is stopped by ;W2,?STOP from W1 (re-scan #9)", needs=["wcb1", "wcb2"])
def timer_stop_mesh(bench):
    """WCB coverage re-scan #9 (docs/hil_plan/WCB.md WCB-WP29 row 3). ?STOP was caught only as a whole line typed on the
    board's own console (processSerialCommandHelper). Over the mesh, in a FRAG or inside a plain chain it reached
    processLocalCommand, fell into the legacy 's' catch-all, was ACKed and did nothing: a remote board's timer show
    could not be stopped. processLocalCommand has a STOP root now."""
    w1, w2 = usb_wcb(bench), WCB(bench.dev("wcb2"))
    far = link(bench, 2, "S2")
    a, b = marker("a"), marker("b")
    m = far.mark()
    w2.send(f";S2{a}^;T3000^;S2{b}")
    far.expect(a.encode(), timeout=2, since=m)
    m2 = w2.dev.mark()
    w1.send(";W2,?STOP")
    try:
        w2.dev.expect(r"Timer sequence manually stopped", timeout=3, since=m2)
        stopped = True
    except AssertionError:
        stopped = False
    time.sleep(3.5)
    got = far.received(m)
    assert b.encode() not in got, f"{b} reached W2S2 after ;W2,?STOP (W2 {'did' if stopped else 'did not'} print the stop line)"
    assert stopped, "W2 printed no 'Timer sequence manually stopped' for ;W2,?STOP"


@test("serial.timer_stop_in_chain", "?STOP stops the running timer chain from inside a plain chain (?VERSION^?STOP) and as a stored sequence's body (re-scan #9)", needs=NEEDS)
def timer_stop_in_chain(bench):
    """The same STOP root (re-scan #9): neither form is a whole line, so both used to reach the legacy 's' catch-all
    and do nothing. The sequence is recalled local-only (,L)."""
    probe, ch = wire(bench, 1, "S1")
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1):
        try:
            w.run("?SEQ,SAVE,HILSTOP,?STOP")
            for how, line in (("?VERSION^?STOP", "?VERSION^?STOP"), ("the sequence HILSTOP", ";CHILSTOP,L")):
                a, b = marker("a"), marker("b")
                m = probe.dev.mark()
                w.send(f";S1{a}^;T2500^;S1{b}")
                probe.expect_bytes(ch, a.encode(), timeout=2, since=m)
                wm = w.send(line)
                try:
                    w.dev.expect(r"Timer sequence manually stopped", timeout=2, since=wm)
                except AssertionError:
                    problems.append(f"{how} printed no stop line")
                time.sleep(3.0)
                if b.encode() in probe.received(ch, m):
                    problems.append(f"{b} arrived after {how}")
        finally:
            w.run("?SEQ,CLEAR,HILSTOP")
    assert not problems, "; ".join(problems)
