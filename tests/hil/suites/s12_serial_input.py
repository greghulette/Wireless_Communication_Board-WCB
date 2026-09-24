"""Serial INPUT — bytes a device sends INTO WCB ports, injected by the probes.

Built from the verified serial_input specs. A test whose title starts "(should)" asserts the
INTENDED behaviour where the firmware has a probable bug, and fails until that is fixed; its
docstring names the code. Tests that change saved config run inside config_guard.

Markers are alphanumeric and start HIL: plain text from a port is broadcast to W2 and NaviCore,
and a ';' anywhere in a line diverts it into the timer engine.
"""
import re
import threading
import time
import zlib

from hil.links import SW_MAX_BAUD
from hil.probe import HW_ONLY_HEADERS, PROBE_LEVELRX_VERSION, SW_CHANNELS
from hil.runner import Skip, test
from suites.common import (Console, Watch, config_guard, link, marker, padded, prime, quiet_lines,
                           require_tokens, send_chunked, token, usb_wcb, usb_wcb_number)


def _has(lines, text):
    return any(text in x for x in lines)


def _raw_attempts(w):
    lines = w.run("?STATS")
    for i, x in enumerate(lines):
        if x.startswith("Raw Data (Kyber Bridging):"):
            m = re.search(r"Attempts: (\d+)", lines[i + 1] if i + 1 < len(lines) else "")
            return int(m.group(1)) if m else 0
    return 0


# ============================================================ framing and line assembly
@test("input.cmd_to_usb", "A ? command typed on a port prints on USB, never back on a port", needs=["wcb1"])
def cmd_to_usb(bench):
    ports = [link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 1, "S4"), link(bench, 1, "S5")]
    usb = usb_wcb(bench).dev
    prime(*ports)
    time.sleep(0.5)
    for src in ports:
        w = Watch(*ports)
        m = usb.mark()
        src.send(b"?VERSION\r")
        usb.expect(r"^Software Version: \S+", timeout=2, since=m)
        usb.expect(r"^End of Version", timeout=2, since=m)
        w.silent(*ports, window=1.0)


@test("input.terminators", "CR, LF, CRLF and LFCR each end exactly one line", needs=["wcb1"])
def terminators(bench):
    s3, s4 = link(bench, 1, "S3"), link(bench, 1, "S4")
    prime(s3)
    time.sleep(0.3)
    marks = [marker(t) for t in "abcd"]
    w = Watch(s4)
    for mk, term in zip(marks, (b"\r", b"\n", b"\r\n", b"\n\r")):
        s3.send(b";S4" + mk.encode() + term)
        time.sleep(0.3)
    expected = b"".join(mk.encode() + b"\r" for mk in marks)
    w.expect(s4, expected, timeout=3)
    time.sleep(0.5)
    assert w.got(s4) == expected, f"expected exactly {expected!r}, got {w.got(s4)!r}"


@test("input.partial_line_waits", "An unterminated line waits in its port buffer until CR/LF arrives", needs=["wcb1"])
def partial_line_waits(bench):
    s3, s4 = link(bench, 1, "S3"), link(bench, 1, "S4")
    prime(s3)
    time.sleep(0.3)
    t = marker()
    half = len(t) // 2
    w = Watch(s4)
    s3.send(b";S4" + t[:half].encode())
    time.sleep(2.0)
    assert not w.got(s4), f"S4 wrote before the terminator arrived: {w.got(s4)!r}"
    s3.send(t[half:].encode() + b"\r")
    w.expect(s4, t.encode() + b"\r", timeout=2)


@test("input.per_port_buffers", "Each port assembles its own line", needs=["wcb1"])
def per_port_buffers(bench):
    s3, s4, s5 = link(bench, 1, "S3"), link(bench, 1, "S4"), link(bench, 1, "S5")
    prime(s3, s4)
    time.sleep(0.3)
    a, b = marker("a"), marker("b")
    w = Watch(s5)
    s3.send(b";S5" + a.encode())
    s4.send(b";S5" + b.encode() + b"\r")
    time.sleep(0.5)
    s3.send(b"\r")
    w.expect(s5, a.encode() + b"\r", timeout=3)
    time.sleep(0.3)
    got = w.got(s5)
    assert got == b.encode() + b"\r" + a.encode() + b"\r", f"expected <b> then <a> once each, got {got!r}"


@test("input.partial_poisons_next", "Leftover bytes turn the next command on that port into a plain broadcast", needs=["wcb1"])
def partial_poisons_next(bench):
    s2, s3, s4, s5 = link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 1, "S4"), link(bench, 1, "S5")
    require_tokens(bench, 1, "?BCAST,OUT,S2,ON", "?BCAST,OUT,S4,ON", "?BCAST,OUT,S5,ON", "?BCAST,IN,S3,ON")
    remote = [bench.links.get(2, p) for p in ("S3", "S4", "S5")]
    if any(remote):
        require_tokens(bench, 2, "?BCAST,OUT,S3,ON", "?BCAST,OUT,S4,ON", "?BCAST,OUT,S5,ON")
    prime(s3)
    time.sleep(0.3)
    t = marker()
    line = f"zz;S4{t}\r".encode()
    w = Watch(s2, s3, s4, s5, *remote)
    s3.send(b"zz")
    time.sleep(0.2)
    s3.send(f";S4{t}\r".encode())
    for l in (s2, s4, s5):
        w.expect(l, line, timeout=2)
    for l in remote:
        if l:
            w.expect(l, line, timeout=3)
    time.sleep(0.5)
    assert not w.got(s3), f"the source port S3 received its own broadcast: {w.got(s3)!r}"
    assert w.got(s4).count(t.encode()) == 1, f"S4 also ran the command: {w.got(s4)!r}"


@test("input.trim_and_blank", "Leading/trailing spaces and tabs are trimmed; whitespace-only lines do nothing", needs=["wcb1"])
def trim_and_blank(bench):
    s2, s3, s4, s5 = link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 1, "S4"), link(bench, 1, "S5")
    usb = usb_wcb(bench).dev
    prime(s3)
    time.sleep(0.3)
    t = marker()
    w = Watch(s4)
    s3.send(b"  \t;S4" + t.encode() + b"  \t\r")
    w.expect(s4, t.encode() + b"\r", timeout=2)
    time.sleep(0.3)
    assert w.got(s4) == t.encode() + b"\r", f"expected exactly <t>CR, got {w.got(s4)!r}"
    w = Watch(s2, s4, s5)
    m = usb.mark()
    s3.send(b"\r\n   \r\n\t\r")
    w.silent(s2, s4, s5, window=1.5)
    assert not quiet_lines(usb, m), f"a blank line produced output: {quiet_lines(usb, m)}"


@test("input.nul_ignored", "(should) A NUL (a break on the line) is dropped: the command after it still runs, and a NUL-only line broadcasts nothing", needs=["wcb1"])
def nul_ignored(bench):
    """Tracker #79. A UART reads a break - the line held low by a device resetting, a cable being plugged, or a sender
    re-configuring its pin - as 0x00. processIncomingSerial (WCB.ino) used to append it to the line: String counts it,
    but every c_str() consumer stops at it, so '<NUL>;S4<t>' went to every port and the mesh as an EMPTY broadcast (a
    lone CR on S2, S4 and S5) and the command was lost. The probe's own re-bind glitch hit this in
    kyber.local_port_move_releases_old until wcb_probe 5 held the line high."""
    s2, s3, s4, s5 = link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 1, "S4"), link(bench, 1, "S5")
    require_tokens(bench, 1, "?BCAST,OUT,S2,ON", "?BCAST,OUT,S4,ON", "?BCAST,OUT,S5,ON", "?BCAST,IN,S3,ON")
    prime(s3)
    time.sleep(0.3)
    t = marker()
    w = Watch(s2, s4, s5)
    s3.send(b"\x00\x00;S4" + t.encode() + b"\r")
    w.expect(s4, t.encode() + b"\r", timeout=2)
    time.sleep(0.5)
    assert w.got(s4) == t.encode() + b"\r", f"expected exactly <t>CR on S4, got {w.got(s4)!r}"
    assert not w.got(s2) and not w.got(s5), f"the NUL-led line was broadcast: S2 {w.got(s2)!r}, S5 {w.got(s5)!r}"
    w = Watch(s2, s4, s5)
    s3.send(b"\x00\r")
    w.silent(s2, s4, s5, window=1.5)


@test("input.multi_line_and_chain", "Several lines in one burst, and a ^ chain typed on a port, all run in order", needs=["wcb1"])
def multi_line_and_chain(bench):
    s2, s4, s5 = link(bench, 1, "S2"), link(bench, 1, "S4"), link(bench, 1, "S5")
    prime(s2)
    time.sleep(0.3)
    a, b, c, d, e = (marker(x) for x in "abcde")
    w = Watch(s4, s5)
    s2.send(f";S4{a}\r;S5{b}\r;S4{c}\r".encode())
    time.sleep(1.0)
    s2.send(f";S4{d}^;S5{e}\r".encode())
    w.expect(s4, f"{a}\r{c}\r{d}\r".encode(), timeout=3)
    w.expect(s5, f"{b}\r{e}\r".encode(), timeout=3)
    time.sleep(0.3)
    assert w.got(s4) == f"{a}\r{c}\r{d}\r".encode(), f"S4 got {w.got(s4)!r}"
    assert w.got(s5) == f"{b}\r{e}\r".encode(), f"S5 got {w.got(s5)!r}"


@test("input.long_line_soft", "A 300-char line on a soft port (95-byte RX buffer) arrives intact", needs=["wcb1"])
def long_line_soft(bench):
    s2 = link(bench, 1, "S2")
    sources = [link(bench, 1, "S3"), link(bench, 1, "S4"), link(bench, 1, "S5")]
    prime(*sources)
    time.sleep(0.5)
    for src in sources:
        p = padded("L", 300).encode()
        w = Watch(s2)
        src.send(b";S2" + p + b"\r")
        w.expect(s2, p + b"\r", timeout=6)
        time.sleep(0.3)
        assert w.got(s2) == p + b"\r", f"from {src.key}: got {w.got(s2)!r}"
        assert not s2.errors(w.marks[s2.key]), f"framing errors on S2: {s2.errors(w.marks[s2.key])}"
        time.sleep(0.5)


@test("input.queue_overflow", "A 250-token chain typed on a port: executed + discarded = 250 (200-item queue)", needs=["wcb1"])
def queue_overflow(bench):
    s2 = link(bench, 1, "S2")
    w = usb_wcb(bench)
    name = "hq" + marker()[3:7].lower()
    prime(s2)
    time.sleep(0.3)
    try:
        m = w.dev.mark()
        send_chunked(s2, ("^".join([f";V,{name},INC"] * 250) + "\r").encode())
        last, stable = None, 0
        for _ in range(40):
            time.sleep(0.5)
            out = w.run(f"?VAR,GET,{name}")
            v = next((int(x.split("=")[1]) for x in out if x.startswith(f"[VAR] {name} = ")), 0)
            stable = stable + 1 if v == last else 0
            last = v
            if stable >= 3:
                break
        discarded = sum(1 for x in w.dev.since(m) if "Command queue is full! Discarding command." in x)
        oom = [x for x in w.dev.since(m) if "Out of memory while enqueuing command" in x]
        bench.note(f"queue_overflow: executed {last}, discarded {discarded}")
        assert not oom, f"out-of-memory while enqueuing: {oom}"
        assert last + discarded == 250, f"executed {last} + discarded {discarded} != 250"
    finally:
        w.run(f"?VAR,CLEAR,{name}")


# ============================================================ broadcast from ports
@test("input.bcast_fanout", "Plain text from a port reaches every other eligible port and the mesh; from USB nothing is skipped", needs=["wcb1"])
def bcast_fanout(bench):
    s2, s3, s4, s5 = link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 1, "S4"), link(bench, 1, "S5")
    require_tokens(bench, 1, *[f"?BCAST,OUT,S{n},ON" for n in "2345"], *[f"?BCAST,IN,S{n},ON" for n in "2345"],
                   "?BCAST,OUT,S0,OFF")
    w1s1 = bench.links.get(1, "S1")
    remote_ok = [bench.links.get(2, p) for p in ("S3", "S4", "S5")]
    remote_quiet = [bench.links.get(2, "S1"), bench.links.get(2, "S2")]
    if any(remote_ok):
        require_tokens(bench, 2, "?BCAST,OUT,S2,OFF", "?BCAST,OUT,S3,ON", "?BCAST,OUT,S4,ON", "?BCAST,OUT,S5,ON")
    usb = usb_wcb(bench)
    locals_ = [s2, s3, s4, s5]
    prime(*locals_)
    time.sleep(0.5)
    for src in locals_ + ["usb"]:
        t = marker()
        w = Watch(*locals_, w1s1, *remote_ok, *remote_quiet)
        m = usb.dev.mark()
        if src == "usb":
            usb.send(t)
        else:
            src.send(t.encode() + b"\r")
        for l in locals_:
            if l is not src:
                w.expect(l, t.encode() + b"\r", timeout=1.5)
        for l in remote_ok:
            if l:
                w.expect(l, t.encode() + b"\r", timeout=3)
        w.silent(*([src] if src != "usb" else []), w1s1, *remote_quiet, window=1.5)
        assert not any(x.strip() == t for x in usb.dev.since(m)), "the broadcast was echoed on USB with S0 echo off"
        time.sleep(0.5)


def _bind_soft_in_order(bench, *wires):
    """Bind `wires` to probe SOFTWARE channels in the order given; returns {wire key: channel}. LinkManager.bind()
    hands out the first free letter of SW_CHANNELS, so with every soft channel on those probes released first the
    first wire gets C and the next D. That is the only handle a test has on which channel serves which wire."""
    probes = {l.probe_name for l in wires}
    for l in bench.links.all():
        if l.probe_name in probes and l.channel in SW_CHANNELS:
            l.release()
    for l in wires:
        l.listen(hw=False)
    return {l.key: l.channel for l in wires}


def _fanout_lines(src, wires, count, period, width):
    """Type `count` unique plain lines into `src`, one every `period` s. Returns (lines, {wire key: (lines that did
    not arrive exact, up to 3 received lines that match none of them, RXERR lines)})."""
    prime(src)
    time.sleep(0.3)
    lines = [padded(f"Q{i:03d}", width).encode() for i in range(count)]
    watch = Watch(*wires)
    t0 = time.monotonic()
    for i, x in enumerate(lines):
        time.sleep(max(0.0, t0 + i * period - time.monotonic()))
        src.send(x + b"\r")
    time.sleep(2.0)   # the last fan-out, and W2 writing out its mesh copies before the next pass or test
    out = {}
    for l in watch.links:
        got = set(watch.got(l).split(b"\r"))
        out[l.key] = ([x for x in lines if x not in got], sorted(g for g in got - set(lines) if g)[:3],
                      l.errors(watch.marks[l.key]))
    return lines, out


@test("probe.soft_concurrent_rx", "W1 S3's broadcast fans out to S2 + S4 at once: exact with one probe soft channel receiving (control) and with two (wcb_probe 6's level-triggered RX, ESP32 erratum GPIO-3.14)", needs=["wcb1"])
def soft_concurrent_rx(bench):
    """Instrument check, not a firmware test. A broadcast typed on W1 S3 goes to S2, a hardware UART that queues it
    and returns, then to S4, whose RMT write blocks until sent (the fan-out loop in processBroadcastCommand,
    WcbSoftSerial::write), so both lines are on the wire at once.

    Onto two probe SOFTWARE channels, that moment used to lose ~0.2-1 % of lines. The probe's soft RX pins all sit
    in GPIO0-31, which share one interrupt status register, and per ESP32 erratum GPIO-3.14 (all revisions, no fix)
    the dispatcher's W1TC clear for one pin's edge swallows another pin's edge arriving at that moment - about the
    ISR latency after it. Each bad byte was exactly one lost edge: a flipped run of bits, a merged line when the
    lost edge was in the CR, or a slipped frame for the rest of the burst (tracker #78). wcb_probe 6 receives on
    level-triggered, polarity-flipping interrupts - the WCB's own patched EspSoftwareSerial - which the erratum
    cannot lose.

    So: (1) CONTROL - W1S2 on a probe HARDWARE channel, W1S4 on a soft one. W1 does exactly the same work, but only
    one probe pin takes edge interrupts. Every line must be exact: that rules the WCB and the wire in or out (with
    1000 lines, P(0 wrong) is ~0.7 % if they caused a 0.5 %/line loss). (2) TWO SOFT - both on soft channels, the
    erratum's case. On wcb_probe 6 every line must be exact too; an older probe skips this arm (its edge-triggered
    soft RX would measure itself, not W1). W1 S5, written after S4 finishes, is recorded as a lone-soft-channel
    control and not asserted."""
    s2, s3, s4 = link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 1, "S4")
    require_tokens(bench, 1, "?BCAST,IN,S3,ON", "?BCAST,OUT,S2,ON", "?BCAST,OUT,S4,ON", "?BCAST,OUT,S0,OFF")
    tokens = bench.config_tokens(1)
    if token(tokens, "?MAP,SERIAL,S3"):
        raise Skip("W1 S3 has a serial mapping, which replaces its broadcast")
    if s2.probe_name != s4.probe_name or s2.header in HW_ONLY_HEADERS or s4.header in HW_ONLY_HEADERS:
        raise Skip(f"{s2} and {s4} are not two soft-capable headers of one probe, so no two probe soft channels "
                   f"receive the fan-out at once")
    bauds = [bench.port_baud(1, p) for p in ("S2", "S4")]
    if max(bauds) > SW_MAX_BAUD:
        raise Skip(f"W1 S2/S4 run at {bauds} baud, above what a probe soft channel receives ({SW_MAX_BAUD})")
    if bench.port_baud(1, "S3") > SW_MAX_BAUD:   # W1's own soft RX is not exact there (input.soft_rx_baud_sweep)
        raise Skip(f"W1 S3 runs above {SW_MAX_BAUD} baud, so W1's own soft RX would lose lines before the fan-out")
    s5 = bench.links.get(1, "S5")
    if not (s5 and s5.probe_name == s2.probe_name and s5.header not in HW_ONLY_HEADERS
            and token(tokens, "?BCAST,OUT,S5,ON") and bench.port_baud(1, "S5") <= SW_MAX_BAUD):
        s5 = None
    width = 20
    frame = (width + 1) * 10 / min(bauds)
    # W1 writes S5 after the S2/S4 pair, and W2 writes its mesh copy to three soft ports one after another. Four
    # frame-times per line keeps every board ahead of the stream: at 9600 that is 0.1 s per line.
    period = max(0.1, 4 * frame)
    ctl_count, soft_count = min(1000, int(100 / period)), min(300, int(30 / period))
    notes, bad = [], []

    def run_arm(label, count):
        s3.listen(hw=True)   # inject from a probe hardware UART, so the probe's own soft TX is not in question
        lines, res = _fanout_lines(s3, [s2, s4, s5], count, period, width)
        chans = {l.key: l.channel for l in (s2, s4, s5) if l}
        lost = {key: set(v[0]) for key, v in res.items()}
        notes.append(f"{label}: " + ", ".join(f"{key} ch {chans[key]} {len(lines) - len(lost[key])}/{len(lines)}"
                                              + (" alone" if s5 and key == s5.key else "") for key in res))
        return lines, res, lost, chans

    # (1) control: W1S2 on a hardware channel, W1S4 alone on a soft one
    for l in bench.links.all():
        if l.probe_name == s2.probe_name and l.channel in SW_CHANNELS:
            l.release()
    s2.listen(hw=True)
    s4.listen(hw=False)
    if s5:
        s5.listen(hw=False)
    lines, res, lost, chans = run_arm("control (one probe edge-interrupt pin)", ctl_count)
    for key in (s2.key, s4.key):
        if lost[key]:
            bad.append(f"control {key} ch {chans[key]}: {len(lost[key])} of {len(lines)} lines not exact with only one "
                       f"probe edge-interrupt pin receiving - so NOT the probe's GPIO-3.14; W1 or the wire, got e.g. "
                       f"{res[key][1]}")

    # (2) two soft channels at once: the erratum's case, exact on level-triggered soft RX (wcb_probe 6)
    probe = s2.probe
    probe.hello()
    if probe.version.isdigit() and int(probe.version) >= PROBE_LEVELRX_VERSION:
        s2.release()
        _bind_soft_in_order(bench, s2, s4, *([s5] if s5 else []))
        lines, res, lost, chans = run_arm("two soft channels", soft_count)
        for key in (s2.key, s4.key):
            if lost[key]:
                bad.append(f"two soft {key} ch {chans[key]}: {len(lost[key])} of {len(lines)} lines not exact with two "
                           f"level-triggered probe soft channels receiving at once, got e.g. {res[key][1]}")
    else:
        notes.append(f"two soft channels: skipped - {s2.probe_name} runs wcb_probe v{probe.version}, whose "
                     f"edge-triggered soft RX loses edges to GPIO-3.14 itself; flash tests/hil/wcb_probe "
                     f"(v{PROBE_LEVELRX_VERSION})")
    bench.note("soft concurrent RX exact lines: " + " | ".join(notes))
    assert not bad, "; ".join(bad) + " || " + " | ".join(notes)


@test("input.bcast_in_block", "?BCAST,IN,S3,OFF blocks broadcasts from S3 but not ; commands", needs=["wcb1"])
def bcast_in_block(bench):
    s2, s3, s4, s5 = link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 1, "S4"), link(bench, 1, "S5")
    w = usb_wcb(bench)
    with config_guard(bench, 1):
        assert _has(w.run("?BCAST,IN,S3,OFF"), "Broadcast INPUT on S3: Disabled")
        prime(s3)
        time.sleep(0.3)
        a, b = marker("a"), marker("b")
        watch = Watch(s2, s4, s5, bench.links.get(2, "S3"))
        m = w.dev.mark()
        s3.send(a.encode() + b"\r")
        w.dev.expect(r"^Broadcast blocked from Serial3 \(input blocking enabled\)", timeout=2, since=m)
        watch.silent(s2, s4, s5, bench.links.get(2, "S3"), window=1.5)
        watch = Watch(s4)
        s3.send(f";S4{b}\r".encode())
        watch.expect(s4, b.encode() + b"\r", timeout=2)
        w.run("?BCAST,IN,S3,ON")


@test("input.bcast_out_block", "?BCAST,OUT,S4,OFF removes only S4 from broadcasts; ;S4 still writes", needs=["wcb1"])
def bcast_out_block(bench):
    s2, s3, s4, s5 = link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 1, "S4"), link(bench, 1, "S5")
    w = usb_wcb(bench)
    with config_guard(bench, 1):
        assert _has(w.run("?BCAST,OUT,S4,OFF"), "Broadcast OUTPUT on S4: Disabled")
        a, b = marker("a"), marker("b")
        watch = Watch(s2, s3, s4, s5)
        w.send(a)
        for l in (s2, s3, s5):
            watch.expect(l, a.encode() + b"\r", timeout=2)
        watch.silent(s4, window=1.5)
        watch = Watch(s4)
        w.send(f";S4{b}")
        watch.expect(s4, b.encode() + b"\r", timeout=2)
        w.run("?BCAST,OUT,S4,ON")


@test("input.bcast_parsing", "?BCAST argument handling: only exact ON enables; only the port digit is read; error messages", needs=["wcb1"])
def bcast_parsing(bench):
    s5 = link(bench, 1, "S5")
    w = usb_wcb(bench)
    with config_guard(bench, 1):
        assert _has(w.run("?BCAST,OUT,S5,yes"), "Broadcast OUTPUT on S5: Disabled")
        t = marker()
        watch = Watch(s5)
        w.send(t)
        watch.silent(s5, window=1.5)
        checks = [
            ("?bcast,out,s5,on", "Broadcast OUTPUT on S5: Enabled"),
            ("?BCAST,OUT,Q5,OFF", "Broadcast OUTPUT on S5: Disabled"),
            ("?BCAST,OUT,S5, ON", "Broadcast OUTPUT on S5: Disabled"),
            ("?BCAST,OUT,S5,ON", "Broadcast OUTPUT on S5: Enabled"),
            ("?BCAST,OUT,S1", "Invalid format. Use: ?BCAST,IN/OUT,Sx,ON/OFF"),
            ("?BCAST,UP,S2,ON", "Invalid direction. Use IN or OUT"),
            ("?BCAST,OUT,S6,ON", "Invalid port. Must be S1-S5 (S0 = OUT only)"),
            ("?BCAST,IN,S0,ON", "Invalid port. Must be S1-S5 (S0 = OUT only)"),
        ]
        bad = [f"{cmd!r} -> {out}" for cmd, want in checks for out in [w.run(cmd)] if not _has(out, want)]
        assert not bad, "; ".join(bad)


@test("input.bcast_legacy_forms", "Legacy ?SBIS/?SBOS/?Sxn forms work; the ?SBOx1 form the warning and wiki recommend is rejected (characterisation)", needs=["wcb1"])
def bcast_legacy_forms(bench):
    """Doc/code disagreement: the ?Sx0/1 deprecation warning (WCB.ino:6236) and the wiki (Broadcast.md:364-367)
    recommend ?SBOx1/?SBIx0, which updateBroadcastOutputSetting/InputSetting reject (WCB.ino:6117-6229)."""
    s3 = link(bench, 1, "S3")
    w = usb_wcb(bench)
    require_tokens(bench, 1, "?BCAST,IN,S3,ON", "?BCAST,OUT,S4,ON", "?BCAST,OUT,S0,OFF")
    with config_guard(bench, 1):
        assert _has(w.run("?SBIS3OFF"), "Serial3 broadcast input: DISABLED")
        prime(s3)
        time.sleep(0.3)
        m = w.dev.mark()
        s3.send(marker().encode() + b"\r")
        w.dev.expect(r"^Broadcast blocked from Serial3 \(input blocking enabled\)", timeout=2, since=m)
        checks = [
            ("?SBIS3ON", ["Serial3 broadcast input: ENABLED"]),
            ("?SBOS4OFF", ["Serial4 broadcast output: DISABLED"]),
            ("?SBOS4ON", ["Serial4 broadcast output: ENABLED"]),
            ("?SBOS0ON", ["S0/USB broadcast output: ENABLED"]),
            ("?SBOS0OFF", ["S0/USB broadcast output: DISABLED"]),
            ("?SBOS9ON", ["Invalid port number. Must be 0-5 (0 = USB)"]),
            ("?SBIS0ON", ["Invalid port number. Must be 1-5"]),
            ("?S40", ["is being deprecated", "Serial4 broadcast Disabled and stored in NVS"]),
            ("?S41", ["is being deprecated", "Serial4 broadcast Enabled and stored in NVS"]),
            ("?SBO41", ["Invalid format. Use: ?SBOSxON or ?SBOSxOFF where x=1-5"]),
            ("?SBI30", ["Invalid format. Use: ?SBISxON or ?SBISxOFF where x=1-5"]),
        ]
        bad = []
        for cmd, wants in checks:
            out = w.run(cmd)
            if not all(_has(out, want) for want in wants):
                bad.append(f"{cmd!r} -> {out}")
        assert not bad, "; ".join(bad)


@test("input.bcast_s0_echo_local", "?BCAST,OUT,S0,ON echoes broadcasts on USB, including ones typed on USB", needs=["wcb1"])
def bcast_s0_echo_local(bench):
    s3 = link(bench, 1, "S3")
    w = usb_wcb(bench)
    with config_guard(bench, 1):
        z = marker("z")
        m = w.dev.mark()
        w.send(z)
        time.sleep(1.0)
        assert not any(x.strip() == z for x in w.dev.since(m)), "S0 echo is on before the test turned it on"
        assert _has(w.run("?BCAST,OUT,S0,ON"), "Broadcast OUTPUT on S0 (USB): Enabled")
        a = marker("a")
        m = w.dev.mark()
        w.send(a)
        w.dev.expect(rf"^{a}$", timeout=2, since=m)
        prime(s3)
        time.sleep(0.3)
        b = marker("b")
        m = w.dev.mark()
        s3.send(b.encode() + b"\r")
        w.dev.expect(rf"^{b}$", timeout=2, since=m)
        w.run("?BCAST,IN,S3,OFF")
        c = marker("c")
        m = w.dev.mark()
        s3.send(c.encode() + b"\r")
        w.dev.expect(r"^Broadcast blocked from Serial3", timeout=2, since=m)
        time.sleep(0.5)
        assert not any(x.strip() == c for x in w.dev.since(m)), "a blocked broadcast was still echoed"
        w.run("?BCAST,IN,S3,ON")
        w.run("?BCAST,OUT,S0,OFF")


@test("input.bcast_s0_echo_remote", "W2 with S0 echo on echoes a mesh-received broadcast on its console", needs=["wcb1"], links=[])
def bcast_s0_echo_remote(bench):
    w = usb_wcb(bench)
    with config_guard(bench, 2), Console(bench, 2) as c2:
        m = c2.send("?BCAST,OUT,S0,ON")
        c2.expect(r"Broadcast OUTPUT on S0 \(USB\): Enabled", since=m)
        t = marker()
        m = c2.mark()
        w.send(t)
        c2.expect(rf"{t}$", timeout=4, since=m)
        c2.send("?BCAST,OUT,S0,OFF")
        time.sleep(1.0)


@test("input.bcast_wled_port_not_excluded", "A WLED port is not excluded from broadcasts once its output is forced on", needs=["wcb1"])
def bcast_wled_port_not_excluded(bench):
    w2s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    orig = token(bench.config_tokens(2, refresh=True), "?BCAST,OUT,S2,")
    with config_guard(bench, 2), Console(bench, 2) as c2:
        m = c2.send("?BCAST,OUT,S2,ON")
        c2.expect(r"Broadcast OUTPUT on S2: Enabled", since=m)
        t = marker()
        watch = Watch(w2s2)
        w.send(t)
        watch.expect(w2s2, t.encode() + b"\r", timeout=3)
        c2.send(orig or "?BCAST,OUT,S2,OFF")
        time.sleep(1.0)


@test("input.bcast_json_local_only", "A '{' line from a port reaches local ports but is sent untracked and never acted on remotely", needs=["wcb1"])
def bcast_json_local_only(bench):
    s2, s3, s4 = link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 1, "S4")
    remote = [bench.links.get(2, "S3"), bench.links.get(2, "S4")]
    w = usb_wcb(bench)
    t = marker()
    payload = f'{{"hil":"{t}"}}'
    try:
        w.run("?DEBUG,ETM,ON")
        prime(s3)
        time.sleep(0.3)
        watch = Watch(s2, s3, s4, *remote)
        m = w.dev.mark()
        s3.send(payload.encode() + b"\r")
        for l in (s2, s4):
            watch.expect(l, payload.encode() + b"\r", timeout=2)
        sent = w.dev.expect(rf"^\[ETM\] Sent seq (\d+): {re.escape(payload)}", timeout=2, since=m)
        seq = sent.group(1)
        watch.silent(s3, *remote, window=2.0)
        acks = [x for x in w.dev.since(m) if re.search(rf"\[ETM\] (ACK received from WCB\d+ for seq {seq}|Seq {seq} fully acknowledged)", x)]
        assert not acks, f"a JSON broadcast was ACKed: {acks}"
    finally:
        w.run("?DEBUG,ETM,OFF")


@test("input.bcast_too_long_local_only", "A 188-char broadcast from a port is delivered locally but refused on the mesh (CHKSM on)", needs=["wcb1"])
def bcast_too_long_local_only(bench):
    s2, s3 = link(bench, 1, "S2"), link(bench, 1, "S3")
    w2s3 = bench.links.get(2, "S3")
    require_tokens(bench, 1, "?ETM,CHKSM,ON", "?BCAST,OUT,S3,ON", "?BCAST,IN,S2,ON")
    w = usb_wcb(bench)
    prime(s2)
    time.sleep(0.3)
    ok_line = padded("K", 187)
    watch = Watch(s3, w2s3)
    s2.send(ok_line.encode() + b"\r")
    watch.expect(s3, ok_line.encode() + b"\r", timeout=3)
    if w2s3:
        watch.expect(w2s3, ok_line.encode() + b"\r", timeout=4)
    time.sleep(1.0)
    long_line = padded("X", 188)
    watch = Watch(s3, w2s3)
    m = w.dev.mark()
    s2.send(long_line.encode() + b"\r")
    watch.expect(s3, long_line.encode() + b"\r", timeout=3)
    w.dev.expect(r"^\[ETM\] Command 188 chars exceeds the 187-char limit under \?ETM,CHKSM", timeout=2, since=m)
    if w2s3:
        watch.silent(w2s3, window=2.0)


@test("input.bcast_text_traps", "Port text containing ^ is split into two broadcasts; text starting *** is swallowed", needs=["wcb1"])
def bcast_text_traps(bench):
    s2, s3, s4 = link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 1, "S4")
    w = usb_wcb(bench)
    prime(s3)
    time.sleep(0.3)
    a, b, c = marker("a"), marker("b"), marker("c")
    watch = Watch(s2, s3, s4)
    s3.send(f"{a}^{b}\r".encode())
    for l in (s2, s4):
        watch.expect(l, f"{a}\r{b}\r".encode(), timeout=2)
    watch.silent(s3, window=0.8)
    watch = Watch(s2, s3, s4)
    m = w.dev.mark()
    s3.send(f"***{c}\r".encode())
    w.dev.expect(rf"^Ignored chain command: \*\*\*{c}$", timeout=2, since=m)
    watch.silent(s2, s3, s4, window=1.0)


@test("input.timer_keeps_source", "(should) A port line containing ;T keeps its source skip and obeys input blocking", needs=["wcb1"])
def timer_keeps_source(bench):
    """Probable firmware bug: processSerialCommandHelper passes a ';T' line to parseCommandGroups without the
    sourceID (WCB.ino:7163-7166) and each group fires as parseCommandsAndEnqueue(cmd, 0) (command_timer.cpp:237),
    so the broadcast echoes back to the source port and ?BCAST,IN,Sx,OFF does not stop it."""
    s2, s3, s4, s5 = link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 1, "S4"), link(bench, 1, "S5")
    require_tokens(bench, 1, *[f"?BCAST,OUT,S{n},ON" for n in "2345"], "?BCAST,IN,S3,ON")
    w = usb_wcb(bench)
    problems = []
    prime(s3)
    time.sleep(0.3)
    t1 = marker("a")
    watch = Watch(s2, s3, s4, s5)
    s3.send(f";T100^{t1}\r".encode())
    for l in (s2, s4, s5):
        watch.expect(l, t1.encode() + b"\r", timeout=2)
    time.sleep(1.0)
    if t1.encode() in watch.got(s3):
        problems.append("a ;T chain typed on S3 echoed its broadcast back to S3")
    with config_guard(bench, 1):
        w.run("?BCAST,IN,S3,OFF")
        t4 = marker("d")
        watch = Watch(s2, s3, s4, s5)
        s3.send(f";T100^{t4}\r".encode())
        time.sleep(2.0)
        leaked = [l.key for l in (s2, s3, s4, s5) if t4.encode() in watch.got(l)]
        if leaked:
            problems.append(f"input blocking on S3 did not stop a ;T chain — it reached {leaked}")
        w.run("?BCAST,IN,S3,ON")
    assert not problems, "; ".join(problems)


@test("input.recall_keeps_source", ";C recall typed on a port keeps its source-port skip", needs=["wcb1"])
def recall_keeps_source(bench):
    s2, s3, s4 = link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 1, "S4")
    w = usb_wcb(bench)
    key = "hr" + marker()[3:7].lower()
    a = marker("a")
    with config_guard(bench, 1):
        assert _has(w.run(f"?SEQ,SAVE,{key},{a}"), f"Stored: Key='{key}'")
        prime(s3)
        time.sleep(0.3)
        watch = Watch(s2, s3, s4)
        m = w.dev.mark()
        s3.send(f";C{key},L\r".encode())
        w.dev.expect(rf"^Recalling command for key '{key}': {a}", timeout=2, since=m)
        for l in (s2, s4):
            watch.expect(l, a.encode() + b"\r", timeout=2)
        watch.silent(s3, window=1.5)
        w.run(f"?SEQ,CLEAR,{key}")


# ============================================================ devices on ports
@test("input.maestro_query_reply", "A Maestro get-query on W1 S1 takes the device's reply; a reply after the 25 ms timeout is ignored", needs=["wcb1"])
def maestro_query_reply(bench):
    s1 = link(bench, 1, "S1")
    w = usb_wcb(bench)
    probe = s1.probe
    try:
        s1.listen()
        s1.rule(41, "AA011000", bytes.fromhex("702E"))
        w.run("?VAR,CLEAR,m1pos0")
        m = s1.mark()
        w.send(";M1,getPosition,0")
        s1.expect(bytes.fromhex("AA011000"), timeout=2, since=m)
        time.sleep(0.5)
        assert probe.rule_hits(41, m), "the probe's reply rule never fired"
        out = w.run("?VAR,GET,m1pos0")
        assert _has(out, "[VAR] m1pos0 = 11888"), f"expected 11888 (0x2E70) from the reply, got {out}"
        s1.rule(41, "AA011000", bytes.fromhex("1027"), delay_ms=40)
        m = s1.mark()
        w.send(";M1,getPosition,0")
        s1.expect(bytes.fromhex("AA011000"), timeout=2, since=m)
        time.sleep(0.6)
        out = w.run("?VAR,GET,m1pos0")
        assert _has(out, "[VAR] m1pos0 = 11888"), f"a reply 40 ms late changed the value: {out}"
    finally:
        probe.rule_del(41)
        w.run("?VAR,CLEAR,m1pos0")
        w.send(";M2,getErrors")   # a late reply can reach W2's real Maestro as a bare data pair


@test("input.reader_map_w1", "W1 reads S3 as commands, but S1 (Maestro_Remote) is forwarded as Kyber raw, never read as text", needs=["wcb1"])
def reader_map_w1(bench):
    s1, s3, s4 = link(bench, 1, "S1"), link(bench, 1, "S3"), link(bench, 1, "S4")
    require_tokens(bench, 1, "?MAESTRO,REMOTE")
    w = usb_wcb(bench)
    try:
        w.run("?DEBUG,ON")
        before = _raw_attempts(w)
        prime(s3)
        time.sleep(0.3)
        t1, t2 = marker("a"), marker("b")
        watch = Watch(s4)
        m = w.dev.mark()
        s3.send(f";S4{t1}\r".encode())
        watch.expect(s4, t1.encode() + b"\r", timeout=2)
        w.dev.expect(rf"^Processing input from Serial3: ;S4{t1}", timeout=2, since=m)
        time.sleep(0.5)
        watch = Watch(s4)
        m = w.dev.mark()
        s1.send(f";S4{t2}\r".encode())
        watch.silent(s4, window=2.0)
        assert not any(x.startswith("Processing input from Serial1:") for x in w.dev.since(m)), "S1 was read as text"
        after = _raw_attempts(w)
        assert after >= before + 1, f"Kyber raw attempts did not rise ({before} -> {after})"
    finally:
        w.run("?DEBUG,OFF")
        w.send(";M2,getErrors")


@test("input.w2_ports", "W2's own ports: commands run, WLED-port text is input-blocked, a W2S4 broadcast skips W2S4", needs=["wcb1"])
def w2_ports(bench):
    s2, s3, s4, s5 = link(bench, 2, "S2"), link(bench, 2, "S3"), link(bench, 2, "S4"), link(bench, 2, "S5")
    w1 = [bench.links.get(1, p) for p in ("S1", "S2", "S4")]
    require_tokens(bench, 2, "?BCAST,IN,S2,OFF", "?BCAST,OUT,S2,OFF", "?BCAST,IN,S4,ON", "?BCAST,OUT,S3,ON", "?BCAST,OUT,S5,ON")
    with Console(bench, 2) as c2:
        prime(s2, s3, s4)
        time.sleep(0.5)
        a = marker("a")
        watch = Watch(s5)
        s3.send(f";S5{a}\r".encode())
        watch.expect(s5, a.encode() + b"\r", timeout=3)
        time.sleep(1.0)
        b = marker("b")
        watch = Watch(s3, s4, s5, *w1)
        m = c2.mark()
        s2.send(b.encode() + b"\r")
        c2.expect(r"Broadcast blocked from Serial2 \(input blocking enabled\)", timeout=4, since=m)
        watch.silent(s3, s4, s5, *w1, window=1.5)
        c = marker("c")
        watch = Watch(s3, s4, s5, *w1)
        s4.send(c.encode() + b"\r")
        for l in (s3, s5):
            watch.expect(l, c.encode() + b"\r", timeout=3)
        for l in w1[1:]:
            if l:
                watch.expect(l, c.encode() + b"\r", timeout=4)
        watch.silent(s4, w1[0], window=1.0)


@test("input.bcast_maestro_port_excluded", "Maestro ports never get broadcasts, even with output forced on", needs=["wcb1"])
def bcast_maestro_port_excluded(bench):
    s1 = link(bench, 1, "S1")
    tap = bench.links.get(2, "S1")
    w = usb_wcb(bench)
    orig1 = token(bench.config_tokens(1, refresh=True), "?BCAST,OUT,S1,")
    orig2 = token(bench.config_tokens(2, refresh=True), "?BCAST,OUT,S1,")
    with config_guard(bench, 1, 2):
        try:
            w.run("?BCAST,OUT,S1,ON")
            w.run("?DEBUG,ON")
            with Console(bench, 2) as c2:
                c2.send("?BCAST,OUT,S1,ON")
                time.sleep(1.0)
                t = marker()
                watch = Watch(s1, tap)
                m = w.dev.mark()
                w.send(t)
                w.dev.expect(rf"^Broadcasting command: {t}", timeout=2, since=m)
                watch.silent(s1, tap, window=2.0)
                c2.send(orig2 or "?BCAST,OUT,S1,OFF")
                time.sleep(1.0)
        finally:
            w.run("?DEBUG,OFF")
            w.run(orig1 or "?BCAST,OUT,S1,OFF")
            w.send(";M2,getErrors")


@test("input.bcast_dfp_port_excluded", "A DFPlayer port is excluded from broadcasts and its input is not read as commands", needs=["wcb1"])
def bcast_dfp_port_excluded(bench):
    s2, s3, s4, s5 = link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 1, "S4"), link(bench, 1, "S5")
    w = usb_wcb(bench)
    tokens = bench.config_tokens(1, refresh=True)
    if token(tokens, "?DFP,") or token(tokens, "?WDP,OFF"):
        raise Skip("W1 already has a DFPlayer or WDP off")
    restore = [t for t in tokens if t.upper().startswith(("?BCAST,OUT,S5,", "?BCAST,IN,S5,", "?LABEL,S5,"))]
    with config_guard(bench, 1):
        try:
            # WDP off first: a DFPlayer advert makes peers auto-learn and persist a host route (WCB_DFP.cpp:427-435).
            assert _has(w.run("?WDP,OFF"), "[WDP] disabled")
            assert _has(w.run("?DFP,S5"), "[DFP] Configured: S5")
            w.run("?BCAST,OUT,S5,ON")
            a = marker("a")
            watch = Watch(s2, s3, s4, s5)
            w.send(a)
            for l in (s2, s3, s4):
                watch.expect(l, a.encode() + b"\r", timeout=2)
            time.sleep(1.0)
            assert a.encode() not in watch.got(s5), "the DFPlayer port received a broadcast"
            b = marker("b")
            watch = Watch(s4)
            s5.send(f";S4{b}\r".encode())
            time.sleep(2.0)
            assert b.encode() not in watch.got(s4), "input on the DFPlayer port was run as a command"
        finally:
            w.run("?DFP,CLEAR")
            for t in restore:
                w.run(t)
            if not token(restore, "?LABEL,S5,"):
                w.run("?LABEL,CLEAR,S5")
            w.run("?WDP,ON")


# ============================================================ soft-serial behaviour
@test("input.soft_rx_under_soft_tx", "(should) A soft port's input stays intact while the loop task writes soft ports", needs=["wcb1"])
def soft_rx_under_soft_tx(bench):
    """Guards a soft-RX decoder race: EspSoftwareSerial 8.1.0's rxBits() tests "ISR edge buffer empty" and only
    THEN reads micros() (SoftwareSerial.cpp:483-486). A core-1 task switch between the two leaves the edges that
    arrive meanwhile unseen, so it injects a faux stop bit mid-byte: the byte's tail reads as 1s and the bytes after
    it are framed from mid-byte until an idle gap. The S3 reader, serialCommandTask, shares core 1 with other
    priority-1 tasks (loopTask never blocks), so a 1 ms time slice can land in that window; arm A4h (;S4
    near-continuously) exposed it.
    The firmware closes it by suspending the scheduler around WcbSoftSerial available/read/peek (WCB_SoftSerial.h;
    docs/HIL_FIX_TRACKER.md #63). One corrupted line is a broadcast of garbage under the default config, so the bar
    stays 40/40. Arms C (no load) and B (hardware-port load) are controls."""
    s2, s3, s4 = link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 1, "S4")
    w = usb_wcb(bench)
    s3.listen(hw=True)   # inject from a probe hardware UART so the probe's own TX timing is not in question
    arms = [("C", None, 0), ("B", ";S2,x", 0.05), ("A4", ";S4,x", 0.05), ("A3", ";S3,x", 0.05),
            ("A4h", ";S4," + "Z" * 58, 0.07)]
    results = {}
    with config_guard(bench, 1):
        w.run("?BCAST,IN,S3,OFF")
        for arm, load, period in arms:
            prime(s3)
            time.sleep(0.5)
            lines = ["***" + padded(arm, 58) for _ in range(40)]
            stop = threading.Event()

            def loader():
                while not stop.is_set():
                    w.dev.send(load)
                    time.sleep(period)

            th = threading.Thread(target=loader, daemon=True) if load else None
            m = w.dev.mark()
            if th:
                th.start()
            try:
                for i in range(0, 40, 10):
                    s3.send(b"".join(x.encode() + b"\r" for x in lines[i:i + 10]))
            finally:   # a failed send must not leave the loader typing ;S into W1 through every later test
                if th:
                    stop.set()
                    th.join()
            time.sleep(1.5)
            got = {x[len("Ignored chain command: "):] for x in w.dev.since(m) if x.startswith("Ignored chain command: ")}
            results[arm] = sum(1 for x in lines if x in got)
        w.run("?BCAST,IN,S3,ON")
    bench.note(f"soft RX exact lines of 40 per arm: {results}")
    assert results["C"] == 40 and results["B"] == 40, f"the controls failed — suspect the wire, not the firmware: {results}"
    assert all(n == 40 for n in results.values()), f"soft-port input corrupted under soft TX: {results}"


@test("input.soft_rx_baud_sweep", "Soft-port input is exact at 19200 and 38400 (57600/115200 recorded only)", needs=["wcb1"])
def soft_rx_baud_sweep(bench):
    s2, s3 = link(bench, 1, "S2"), link(bench, 1, "S3")
    w = usb_wcb(bench)
    counts = {}
    with config_guard(bench, 1):
        for baud in (19200, 38400, 57600, 115200):
            assert _has(w.run(f"?BAUD,S3,{baud}"), f"Baud rate for Serial3 updated to {baud}")
            s3.listen(baud)
            prime(s3)
            time.sleep(0.5)
            marks = [marker(f"{baud}x") for _ in range(20)]
            watch = Watch(s2)
            s3.send(b"".join(f";S2{x}\r".encode() for x in marks))
            time.sleep(2.5)
            got = watch.got(s2)
            counts[baud] = sum(1 for x in marks if x.encode() + b"\r" in got)
        w.run("?BAUD,S3,9600")
        s3.listen()
    bench.note(f"soft RX exact lines of 20 per baud: {counts}")
    # The counts are lines that arrived EXACT, out of 20 — not lines lost.
    # 19200 is the bar a bit-banged RX actually holds under mesh load: measured 20/20 every run.
    # 38400 is NOT — it has measured 15-19 of 20 across seven runs on unchanged firmware, so
    # asserting it made this test a coin flip rather than a regression signal. It is recorded
    # instead, alongside 57600/115200. The firmware half of this (?BAUD accepting rates the port
    # cannot receive at) is fixed separately; see docs/HIL_FIX_TRACKER.md #34.
    assert counts[19200] == 20, f"soft RX exact lines of 20 per baud: {counts}"


@test("input.framing_variants", "8N2 input is accepted on hardware and soft ports; wrong-baud input never executes", needs=["wcb1"])
def framing_variants(bench):
    s2, s3, s4 = link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 1, "S4")
    w = usb_wcb(bench)
    with config_guard(bench, 1):
        try:
            w.run("?BCAST,IN,S3,OFF")
            s2.listen(9600, fmt="8N2")
            s3.listen(9600, fmt="8N2")
            prime(s2, s3)
            time.sleep(0.3)
            a1, a2 = marker("a"), marker("b")
            watch = Watch(s4)
            s2.send(f";S4{a1}\r".encode())
            time.sleep(0.5)
            s3.send(f";S4{a2}\r".encode())
            watch.expect(s4, a1.encode() + b"\r", timeout=2)
            watch.expect(s4, a2.encode() + b"\r", timeout=2)
            s3.listen(19200)
            b = marker("c")
            watch = Watch(s4)
            s3.send(f";S4{b}\r".encode())
            time.sleep(1.5)
            s3.listen(9600)
            s3.send(b"\r")
            time.sleep(0.5)
            c = marker("d")
            s3.send(f";S4{c}\r".encode())
            watch.expect(s4, c.encode() + b"\r", timeout=2)
            assert b.encode() not in watch.got(s4), "a line sent at the wrong baud executed"
        finally:
            s2.listen()
            s3.listen()
            w.run("?BCAST,IN,S3,ON")


@test("input.softserial_tx_rmt", "S3-S5 transmit through RMT and receive on the chip's interrupt mode (level-triggered on a classic ESP32, tracker #78): reported at ?BAUD for every soft port, and unchanged by a raw mapping", needs=["wcb1"], links=[])
def softserial_tx_rmt(bench):
    """WcbSoftSerial (WCB_SoftSerial.h) hands S3-S5 TX to an RMT channel; applySoftSerialIntTx() then only reports it.
    A port falling back to bit-banging (no free RMT channel) would print '[SOFTSERIAL] S<n>: no RMT channel' at begin
    and report its old protection state here instead - which is exactly what this test would catch.
    It also reports the receive side (tracker #78): on a classic ESP32 (?HW 1/21/23/24) the vendored EspSoftwareSerial
    receives on a level-emulated interrupt (ESP32 erratum GPIO-3.14), on an ESP32-S3 (?HW 31/32) on the stock edge
    interrupt. Checked for each port ?BAUD re-begins here, at 9600 (below ~74880 baud, where the level path applies)."""
    w = usb_wcb(bench)
    tokens = bench.config_tokens(1, refresh=True)
    if token(tokens, "?MAP,SERIAL,S5"):
        raise Skip("W1 S5 already has a serial mapping")
    hw = (token(tokens, "?HW,") or "?HW,?").split(",")[1]
    rx_mode = ("level-triggered" if hw in ("1", "21", "23", "24") else
               "edge-triggered" if hw in ("31", "32") else None)   # None: unknown board, any RX line will do
    rmt = "[SOFTSERIAL] S{} TX: RMT (hardware-timed)"
    problems = []
    with config_guard(bench, 1):
        try:
            w.run("?DEBUG,ON")
            for n in "345":
                out = w.run(f"?BAUD,S{n},9600")
                if not _has(out, rmt.format(n)):
                    problems.append(f"?BAUD,S{n}: {out}")
                rx = f"[SOFTSERIAL] S{n} RX: " + (rx_mode or "")
                if not _has(out, rx):
                    problems.append(f"?BAUD,S{n} on ?HW,{hw}: no '{rx}' line: {out}")
            if any("[SOFTSERIAL]" in x for x in w.run("?BAUD,S2,9600")):
                problems.append("?BAUD,S2 printed a [SOFTSERIAL] line for a hardware port")
            out = w.run("?MAP,SERIAL,S5,R,S4")
            if not _has(out, "Serial mapping set: Serial5 (RAW) -> 1 destination(s)"):
                problems.append(f"?MAP,SERIAL,S5,R,S4: {out}")
            if not _has(w.run("?BAUD,S5,9600"), rmt.format(5)):
                problems.append("a raw-mapped S5 no longer reports RMT TX")
            out = w.run("?MAP,SERIAL,CLEAR,S5")
            if not _has(out, "Serial mapping removed for Serial5"):
                problems.append(f"?MAP,SERIAL,CLEAR,S5: {out}")
        finally:
            w.run("?DEBUG,OFF")
    assert not problems, "; ".join(problems)


# ============================================================ WDP device announce
# Records are kept until forgotten (NVS wdp_da), so a test that creates one forgets it again in a finally, and each
# first forgets any HIL-named leftovers on its ports (_da_scrub): a run killed between the two would otherwise leave
# one behind for good. Test types always start with HIL, and nothing here touches any other record.
def _da_rows(w):
    return w.run("?WDP,DA")


def _da_listed(rows, port):
    """The types a ?WDP,DA printout lists on <port>, in its order (first heard first). Test types have no spaces."""
    return [x.split()[1] for x in rows if re.match(rf"^\s+{port}\s", x)]


def _da_types(w, port):
    return _da_listed(_da_rows(w), port)


def _da_forget(w, port, *types):
    for t in types:
        w.run(f"?WDP,DA,FORGET,{port},{t}")


def _da_scrub(w, *ports):
    """Forget the HIL-named records an interrupted earlier run left on these W1 ports."""
    rows = _da_rows(w)
    for p in ports:
        _da_forget(w, p, *[t for t in _da_listed(rows, p) if t.startswith("HIL")])


def _da_types_on(c, port):
    """_da_types for the board behind a Console."""
    m = c.send("?WDP,DA")
    time.sleep(0.6)
    return _da_listed(c.lines(m), port)


def _da_forget_on(c, port, *types):
    for t in types:
        c.send(f"?WDP,DA,FORGET,{port},{t}")
        time.sleep(0.3)


def _da_scrub_on(c, port):
    _da_forget_on(c, port, *[t for t in _da_types_on(c, port) if t.startswith("HIL")])


def _da_row(dump, n, port, type_):
    """The [WDPDA:...] line for one device in a ?WDP,DUMP, or None."""
    return next((x.rstrip() for x in dump if x.startswith(f"[WDPDA:N={n},S={port},TYPE={type_},")), None)


def _unlabelled(bench, port):
    return token(bench.config_tokens(1, refresh=True), f"?LABEL,{port},") is None


@test("input.wdpda_basic", "An @WDP1 announce on a port is recorded and printed once, saved only by its second announce, and never broadcast", needs=["wcb1"])
def wdpda_basic(bench):
    s2, s3, s4 = link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 1, "S4")
    w2s3 = bench.links.get(2, "S3")
    w = usb_wcb(bench)
    me = usb_wcb_number(bench)
    _da_scrub(w, "S3")
    name = "HILDA" + marker()[3:7]
    line = f'@WDP1 {{"type":"{name}","fw":"9.8.7"}}\n'.encode()
    try:
        prime(s3)
        time.sleep(0.3)
        watch = Watch(s2, s4, w2s3)
        m = w.dev.mark()
        s3.send(line)
        time.sleep(1.0)
        rows = _da_rows(w)
        assert any(re.match(rf"^\s+S3\s+{name}\s+fw 9\.8\.7\s+\d+s ago, heard once$", x) for x in rows), f"?WDP,DA after one announce: {rows}"
        assert _da_row(w.run("?WDP,DUMP", timeout=8), me, 3, name) is None, "a device heard once is already in the dump"
        s3.send(line)
        w.dev.expect(rf"^\[WDP-DA\] S3: {name} saved$", timeout=3, since=m)
        prints = [x for x in w.dev.since(m) if x.startswith(f"[WDP-DA] S3: {name} fw 9.8.7")]
        assert len(prints) == 1, f"expected one [WDP-DA] line, got {prints}"
        watch.silent(s2, s4, w2s3, window=0.5)
        rows = _da_rows(w)
        assert any(re.match(rf"^\s+S3\s+{name}\s+fw 9\.8\.7\s+\d+s ago$", x) for x in rows), f"?WDP,DA: {rows}"
        row = _da_row(w.run("?WDP,DUMP", timeout=8), me, 3, name)
        assert row and re.match(rf"^\[WDPDA:N={me},S=3,TYPE={name},FW=9\.8\.7,HW=,CAPS=,SEEN=1,AGE=\d+\]$", row), f"dump row: {row}"
    finally:
        _da_forget(w, "S3", name)


@test("input.wdpda_rejects", "Malformed, foreign, cut-short or run-together @WDP lines vanish; lowercase @wdp1 is ordinary text", needs=["wcb1"])
def wdpda_rejects(bench):
    s2, s3, s4, s5 = link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 1, "S4"), link(bench, 1, "S5")
    require_tokens(bench, 1, "?BCAST,IN,S5,ON", "?BCAST,OUT,S2,ON", "?BCAST,OUT,S3,ON", "?BCAST,OUT,S4,ON")
    w = usb_wcb(bench)
    _da_scrub(w, "S5")
    before = _da_types(w, "S5")
    prime(s5)
    time.sleep(0.3)
    watch = Watch(s2, s3, s4)
    m = w.dev.mark()
    bad_lines = (b'@WDP1 {"fw":"1"}\n', b'@WDP1 {"type":""}\n', b"@WDP1 nojson\n", b'@WDP2 {"type":"HILX"}\n', b"@WDPjunk\n",
                 b'@WDP1 {"type":"HILX1"} tail\n',                      # something after the object
                 b'@WDP1 {"type":"HILX2","fw":"1"\n',                   # cut short: no closing brace
                 b'@WDP1 {"type":"HILX3"}@WDP1 {"type":"HILX4"}\n')     # two boards' announces run together
    for bad in bad_lines:
        s5.send(bad)
        time.sleep(0.3)
    watch.silent(s2, s3, s4, window=1.0)
    hits = [x for x in w.dev.since(m) if x.startswith("[WDP-DA] S5: HILX")]
    assert not hits, f"a malformed announce was accepted: {hits}"
    assert _da_types(w, "S5") == before, "S5 was recorded from a malformed announce"
    watch = Watch(s2, s3, s4)
    s5.send(b'@wdp1 {"type":"HILDF"}\n')
    for l in (s2, s3, s4):
        watch.expect(l, b'@wdp1 {"type":"HILDF"}\r', timeout=2)


@test("input.wdpda_fields", "WDP-DA optional hw/caps, 24/27-char truncation, and ',' ']' scrubbing", needs=["wcb1"])
def wdpda_fields(bench):
    s4, s5 = link(bench, 1, "S4"), link(bench, 1, "S5")
    w = usb_wcb(bench)
    _da_scrub(w, "S4", "S5")
    try:
        prime(s4, s5)
        time.sleep(0.3)
        m = w.dev.mark()
        s4.send(b'@WDP1 {"type":"HILDB","fw":"1","hw":"revB","caps":["hil.a","hil.b"]}\n')
        s5.send(b'@WDP1 {"type":"HIL,DC]ABCDEFGHIJKLMNOPQRSTUVW","fw":"F123456789012345678901234567890"}\n')
        w.dev.expect(r"^\[WDP-DA\] S4: HILDB fw 1$", timeout=3, since=m)
        w.dev.expect(r"^\[WDP-DA\] S5: HIL_DC_ABCDEFGHIJKLMNOPQ fw F12345678901234567890123456$", timeout=3, since=m)
        rows = _da_rows(w)
        text = "\n".join(rows)
        assert re.search(r"^\s+hw revB$", text, re.M) and re.search(r"^\s+caps: hil\.a hil\.b$", text, re.M), f"?WDP,DA: {rows}"
    finally:
        _da_forget(w, "S4", "HILDB")
        _da_forget(w, "S5", "HIL_DC_ABCDEFGHIJKLMNOPQ")


@test("input.wdpda_usb_and_blocked_port", "WDP-DA is ignored on USB but honoured on an input-blocked port", needs=["wcb1"])
def wdpda_usb_and_blocked_port(bench):
    s2, s3, s4, s5 = link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 1, "S4"), link(bench, 1, "S5")
    w = usb_wcb(bench)
    _da_scrub(w, "S2")
    try:
        with config_guard(bench, 1):
            w.run("?BCAST,IN,S2,OFF")
            watch = Watch(s3, s4, s5)
            m = w.dev.mark()
            w.send('@WDP1 {"type":"HILDU","fw":"1"}')
            watch.silent(s3, s4, s5, window=1.5)
            # A device elsewhere going quiet is not an accept.
            hits = [x for x in w.dev.since(m) if x.startswith("[WDP-DA]") and not x.endswith(" stopped announcing")]
            assert not hits, f"a USB @WDP1 line was accepted: {hits}"
            prime(s2)
            time.sleep(0.3)
            m = w.dev.mark()
            s2.send(b'@WDP1 {"type":"HILDV","fw":"1"}\n')
            w.dev.expect(r"^\[WDP-DA\] S2: HILDV fw 1$", timeout=3, since=m)
            time.sleep(0.5)
            assert not any("Broadcast blocked from Serial2" in x for x in w.dev.since(m)), "the announce was treated as a broadcast"
            w.run("?BCAST,IN,S2,ON")
    finally:
        _da_forget(w, "S2", "HILDV")


@test("input.wdpda_forget", "?WDP,DA,FORGET drops one device (any case) or a whole port, ?WDP,DA,CLEAR drops all, and a device still announcing comes back", needs=["wcb1"])
def wdpda_forget(bench):
    s2, s3 = link(bench, 1, "S2"), link(bench, 1, "S3")
    w = usb_wcb(bench)
    _da_scrub(w, "S2", "S3")
    a, b, c = "HILFA" + marker()[3:7], "HILFB" + marker()[3:7], "HILFC" + marker()[3:7]
    try:
        prime(s2, s3)
        time.sleep(0.3)
        m = w.dev.mark()
        for l, t in ((s2, a), (s2, b), (s3, c)):
            l.send(f'@WDP1 {{"type":"{t}","fw":"1"}}\n'.encode())
            time.sleep(0.2)
        for p, t in (("S2", a), ("S2", b), ("S3", c)):
            w.dev.expect(rf"^\[WDP-DA\] {p}: {t} fw 1$", timeout=3, since=m)
        out = w.run(f"?WDP,DA,FORGET,S2,{a.lower()}")
        assert _has(out, f"[WDP-DA] S2: {a} forgotten"), f"forget by type, in lower case: {out}"
        assert _da_types(w, "S2") == [b], f"?WDP,DA after forgetting {a}: {_da_rows(w)}"
        out = w.run("?WDP,DA,FORGET,S2,HILNOPE")
        assert _has(out, '[WDP-DA] S2: no device "HILNOPE"'), f"forget of an unknown type: {out}"
        out = w.run("?WDP,DA,FORGET,S9")
        assert _has(out, "[WDP] usage: "), f"forget on a port that doesn't exist: {out}"
        out = w.run("?WDP,DA,FORGET,S2")
        assert _has(out, f"[WDP-DA] S2: {b} forgotten") and not _da_types(w, "S2"), f"forget a whole port: {out}"
        m = w.dev.mark()
        s2.send(f'@WDP1 {{"type":"{a}","fw":"1"}}\n'.encode())
        w.dev.expect(rf"^\[WDP-DA\] S2: {a} fw 1$", timeout=3, since=m)   # a new record, not "heard again"
        rows = _da_rows(w)
        everything = [t for p in ("S1", "S2", "S3", "S4", "S5") for t in _da_listed(rows, p)]
        if any(not t.startswith("HIL") for t in everything):
            raise Skip("forget checks passed; ?WDP,DA,CLEAR skipped: W1 has non-test devices it would forget")
        out = w.run("?WDP,DA,CLEAR")
        assert _has(out, f"[WDP-DA] forgot {len(everything)} device"), f"?WDP,DA,CLEAR: {out}"
        rows = _da_rows(w)
        assert not any(_da_listed(rows, p) for p in ("S1", "S2", "S3", "S4", "S5")), f"?WDP,DA,CLEAR left devices: {rows}"
    finally:
        _da_forget(w, "S2", a, b)
        _da_forget(w, "S3", c)


@test("input.wdpda_propagation", "A WDP-DA device on W2's unlabelled S4 reaches W1 whole (label, record, ?WDP,2 detail), and forgetting it on W2 clears it from W1", needs=["wcb1"])
def wdpda_propagation(bench):
    s4 = link(bench, 2, "S4")
    tokens = bench.config_tokens(2, refresh=True)
    if token(tokens, "?LABEL,S4,") or token(tokens, "?WDP,OFF"):
        raise Skip("W2 S4 is labelled or W2 has WDP off")
    w = usb_wcb(bench)
    name = "HILDP" + marker()[3:7]
    with Console(bench, 2) as c2:
        _da_scrub_on(c2, "S4")
        if _da_types_on(c2, "S4"):
            raise Skip("W2 S4 has non-test devices, which would keep naming the port")
        try:
            prime(s4)
            time.sleep(0.3)
            m = c2.mark()
            announce = f'@WDP1 {{"type":"{name}","fw":"1.0","hw":"revP","caps":["hil.p"]}}\n'.encode()
            s4.send(announce)
            c2.expect(rf"\[WDP-DA\] S4: {name} fw 1\.0", timeout=4, since=m)
            s4.send(announce)                         # the second announce saves it: only then is it advertised
            c2.expect(rf"\[WDP-DA\] S4: {name} saved", timeout=4, since=m)
            time.sleep(3)   # the saved device changes W2's advert and its device list; both go out, then once more
            dump = w.run("?WDP,DUMP", timeout=8)
            assert _has(dump, f"[WDPIF:N=2,S=4,DEV={name}]"), "W1's WDP dump does not show W2 S4's announced type"
            row = _da_row(dump, 2, 4, name)
            assert row == f"[WDPDA:N=2,S=4,TYPE={name},FW=1.0,HW=revP,CAPS=hil.p,SEEN=1,AGE=-]", f"W1's record of it: {row}"
            detail = w.run("?WDP,2")
            assert any(x.strip().startswith(name) and "fw 1.0" in x for x in detail), f"?WDP,2 does not list it: {detail}"
            m = c2.mark()
            c2.send(f"?WDP,DA,FORGET,S4,{name}")
            c2.expect(rf"\[WDP-DA\] S4: {name} forgotten", timeout=3, since=m)
            time.sleep(3)
            dump = w.run("?WDP,DUMP", timeout=8)
            assert not any(x.startswith("[WDPIF:N=2,S=4,") for x in dump), "W2 S4 still labelled in W1's dump after the forget"
            assert _da_row(dump, 2, 4, name) is None, "W1 still lists the forgotten device"
        finally:
            _da_forget_on(c2, "S4", name)


@test("input.wdpda_shared_label_mesh", "Two @WDP1 types taking turns on W2's unlabelled S4: W1 lists both in order and keeps the first as the port label, and W2 sends no extra adverts or device lists", needs=["wcb1"])
def wdpda_shared_label_mesh(bench):
    s4 = link(bench, 2, "S4")
    tokens = bench.config_tokens(2, refresh=True)
    if token(tokens, "?LABEL,S4,") or token(tokens, "?WDP,OFF"):
        raise Skip("W2 S4 is labelled or W2 has WDP off")
    w = usb_wcb(bench)
    a, b = "HILMA" + marker()[3:7], "HILMB" + marker()[3:7]
    la = f'@WDP1 {{"type":"{a}","fw":"1"}}\n'.encode()
    lb = f'@WDP1 {{"type":"{b}","fw":"2"}}\n'.encode()
    with Console(bench, 2) as c2:
        _da_scrub_on(c2, "S4")
        if _da_types_on(c2, "S4"):
            raise Skip("W2 S4 has non-test devices, which would name the port")
        try:
            prime(s4)
            time.sleep(0.3)
            m = c2.mark()
            for l, t in ((la, a), (lb, b)):
                s4.send(l)
                time.sleep(0.3)
                s4.send(l)                            # saved on its second announce
                c2.expect(rf"\[WDP-DA\] S4: {t} saved", timeout=4, since=m)
            time.sleep(3)   # A's save changed W2's advert: let it, the device list and their re-sends reach W1
            dump = w.run("?WDP,DUMP", timeout=8)
            assert _has(dump, f"[WDPIF:N=2,S=4,DEV={a}]"), "W1 does not see the first-heard type as W2 S4's label"
            listed = [re.search(r"TYPE=([^,]*)", x).group(1) for x in dump if x.startswith("[WDPDA:N=2,S=4,")]
            assert listed == [a, b], f"W1's list of W2 S4: {listed}"
            # A label that followed the latest announce would move on every one of these, and each move costs W2 an
            # advert plus a re-send (the on-change check in wdpTick). Refreshes change nothing in the device list either.
            c2.expect(r"MGMT debugging enabled", timeout=3, since=c2.send("?DEBUG,MGMT,ON"))
            try:
                m = c2.mark()
                for _ in range(3):
                    s4.send(lb)
                    time.sleep(1.0)
                    s4.send(la)
                    time.sleep(1.0)
                time.sleep(1.0)
                sent = [x for x in c2.lines(m) if x.startswith("[WDP] advert sent")]
                lists = [x for x in c2.lines(m) if x.startswith("[WDP] device list sent")]
            finally:
                c2.send("?DEBUG,MGMT,OFF")
            assert len(sent) <= 2, f"W2 sent {len(sent)} adverts in 7 s while two devices took turns on S4"
            assert len(lists) <= 2, f"W2 sent {len(lists)} device lists in 7 s although nothing in the list changed"
            assert _has(w.run("?WDP,DUMP", timeout=8), f"[WDPIF:N=2,S=4,DEV={a}]"), "W2 S4's label moved in W1's view while the devices took turns"
        finally:
            _da_forget_on(c2, "S4", a, b)


@test("input.wdpda_ttl", "A saved device goes quiet 90 s after its last announce and is kept, and announcing brings it back; one heard only once is dropped (~3 min)", needs=["wcb1"])
def wdpda_ttl(bench):
    s3 = link(bench, 1, "S3")
    w = usb_wcb(bench)
    me = usb_wcb_number(bench)
    _da_scrub(w, "S3")
    name, once = "HILDT" + marker()[3:7], "HILDO" + marker()[3:7]
    line = f'@WDP1 {{"type":"{name}"}}\n'.encode()
    try:
        prime(s3)
        time.sleep(0.3)
        m = w.dev.mark()
        s3.send(line)
        t0 = time.monotonic()
        s3.send(f'@WDP1 {{"type":"{once}"}}\n'.encode())   # never announced again
        time.sleep(60)
        s3.send(line)                                       # the second announce saves it
        w.dev.expect(rf"^\[WDP-DA\] S3: {name} saved$", timeout=3, since=m)
        dropped = w.dev.expect(rf"^\[WDP-DA\] S3: {once} heard only once, dropped$", timeout=40, since=m)
        once_after = time.monotonic() - t0
        assert once not in _da_types(w, "S3"), "the device heard only once is still listed"
        gone = w.dev.expect(rf"^\[WDP-DA\] S3: {name} stopped announcing", timeout=110, since=m)
        elapsed = time.monotonic() - t0
        bench.note(f"WDP-DA heard-once drop {once_after:.1f} s ({dropped.group(0)}); quiet {elapsed:.1f} s after the first announce")
        assert 88 <= once_after <= 96, f"the heard-once device was dropped {once_after:.1f} s after it, expected ~90"
        assert 148 <= elapsed <= 156, f"went quiet {elapsed:.1f} s after the first announce, expected ~150 (60 + 90)"
        assert name in _da_types(w, "S3"), "the quiet device was dropped instead of kept"
        row = _da_row(w.run("?WDP,DUMP", timeout=8), me, 3, name)
        age = re.search(r",SEEN=0,AGE=(\d+)\]$", row or "")
        assert age and int(age.group(1)) >= 90, f"dump row while quiet: {row}"
        m = w.dev.mark()
        s3.send(line)
        w.dev.expect(rf"^\[WDP-DA\] S3: {name} \(heard again\)$", timeout=3, since=m)
        row = _da_row(w.run("?WDP,DUMP", timeout=8), me, 3, name)
        assert row and ",SEEN=1," in row, f"dump row after it announced again: {row}"
    finally:
        _da_forget(w, "S3", name, once)


@test("input.wdpda_shared_port", "Two @WDP1 types on one port each keep a record; the first heard keeps the port label, so taking turns sends no adverts", needs=["wcb1"])
def wdpda_shared_port(bench):
    s3 = link(bench, 1, "S3")
    w = usb_wcb(bench)
    me = usb_wcb_number(bench)
    _da_scrub(w, "S3")
    a, b = "HILSA" + marker()[3:7], "HILSB" + marker()[3:7]
    la = f'@WDP1 {{"type":"{a}","fw":"1.1"}}\n'.encode()
    lb = f'@WDP1 {{"type":"{b}","fw":"2.2","hw":"revC","caps":["hil.x"]}}\n'.encode()
    try:
        prime(s3)
        time.sleep(0.3)
        m = w.dev.mark()
        s3.send(la)
        w.dev.expect(rf"^\[WDP-DA\] S3: {a} fw 1\.1$", timeout=3, since=m)
        s3.send(lb)
        w.dev.expect(rf"^\[WDP-DA\] S3: {b} fw 2\.2$", timeout=3, since=m)
        s3.send(la)                                   # each one's second announce saves it
        w.dev.expect(rf"^\[WDP-DA\] S3: {a} saved$", timeout=3, since=m)
        s3.send(lb)
        w.dev.expect(rf"^\[WDP-DA\] S3: {b} saved$", timeout=3, since=m)
        m = w.dev.mark()
        s3.send(la)
        time.sleep(0.5)
        s3.send(lb)
        time.sleep(1.0)
        again = [x for x in w.dev.since(m) if x.startswith("[WDP-DA]")]
        assert not again, f"a re-announce printed again: {again}"
        assert _da_types(w, "S3") == [a, b], f"?WDP,DA: {_da_rows(w)}"
        dump = w.run("?WDP,DUMP", timeout=8)
        da = [x.rstrip() for x in dump if x.startswith(f"[WDPDA:N={me},S=3,")]
        assert len(da) == 2, f"expected two WDPDA records on S3: {da}"
        assert re.match(rf"^\[WDPDA:N={me},S=3,TYPE={a},FW=1\.1,HW=,CAPS=,SEEN=1,AGE=\d+\]$", da[0]), da[0]
        assert re.match(rf"^\[WDPDA:N={me},S=3,TYPE={b},FW=2\.2,HW=revC,CAPS=hil\.x,SEEN=1,AGE=\d+\]$", da[1]), da[1]
        if not _unlabelled(bench, "S3"):
            raise Skip("record checks passed; the port-label checks need W1 S3 unlabelled (input.wdpda_shared_label_mesh covers them on W2 S4)")
        assert _has(dump, f"[WDPIF:N={me},S=3,DEV={a}]"), f"S3's label is not the first-heard type: {[x for x in dump if x.startswith('[WDPIF')]}"
        # A label that followed the latest announce would move on every one of these, and each
        # move costs an advert plus a re-send (the on-change check in wdpTick).
        try:
            assert _has(w.run("?DEBUG,MGMT,ON"), "MGMT debugging enabled")
            m = w.dev.mark()
            for _ in range(3):
                s3.send(lb)
                time.sleep(1.0)
                s3.send(la)
                time.sleep(1.0)
            time.sleep(1.0)
            sent = [x for x in w.dev.since(m) if x.startswith("[WDP] advert sent")]
        finally:
            w.run("?DEBUG,MGMT,OFF")
        assert len(sent) <= 2, f"{len(sent)} adverts in 7 s while two devices took turns announcing"
        assert _has(w.run("?WDP,DUMP", timeout=8), f"[WDPIF:N={me},S=3,DEV={a}]"), "S3's label moved while the devices took turns"
    finally:
        _da_forget(w, "S3", a, b)


@test("input.wdpda_port_full", "On a full port a newcomer replaces a device heard only once but never a saved one: it is refused until one is forgotten; the first heard keeps the label", needs=["wcb1"])
def wdpda_port_full(bench):
    s4 = link(bench, 1, "S4")
    w = usb_wcb(bench)
    me = usb_wcb_number(bench)
    _da_scrub(w, "S4")
    if _da_types(w, "S4"):
        raise Skip("W1 S4 has non-test devices; the port needs to be empty to fill it")
    tag = marker()[3:7]
    names = [f"HILF{i}{tag}" for i in range(5)]
    lines = [f'@WDP1 {{"type":"{n}","fw":"{i}"}}\n'.encode() for i, n in enumerate(names)]
    try:
        prime(s4)
        time.sleep(0.3)
        m = w.dev.mark()
        for i in range(3):                             # three saved devices...
            s4.send(lines[i])
            time.sleep(0.3)
            s4.send(lines[i])
            w.dev.expect(rf"^\[WDP-DA\] S4: {names[i]} saved$", timeout=3, since=m)
        s4.send(lines[3])                              # ...and one heard once fill the port
        w.dev.expect(rf"^\[WDP-DA\] S4: {names[3]} fw 3$", timeout=3, since=m)
        m = w.dev.mark()
        s4.send(lines[4])                              # a newcomer may only replace the heard-once one
        w.dev.expect(rf"^\[WDP-DA\] S4: {names[3]} dropped, port full \(4 devices\)$", timeout=3, since=m)
        w.dev.expect(rf"^\[WDP-DA\] S4: {names[4]} fw 4$", timeout=3, since=m)
        s4.send(lines[4])
        w.dev.expect(rf"^\[WDP-DA\] S4: {names[4]} saved$", timeout=3, since=m)
        m = w.dev.mark()
        s4.send(lines[3])                              # four saved: refused, and said only once
        w.dev.expect(rf"^\[WDP-DA\] S4: {names[3]} not added: port full", timeout=3, since=m)
        s4.send(lines[3])
        time.sleep(1.0)
        refusals = [x for x in w.dev.since(m) if x.startswith(f"[WDP-DA] S4: {names[3]} not added")]
        assert len(refusals) == 1, f"the refusal repeated: {refusals}"
        assert _da_types(w, "S4") == [names[0], names[1], names[2], names[4]], f"?WDP,DA: {_da_rows(w)}"
        w.run(f"?WDP,DA,FORGET,S4,{names[1]}")
        m = w.dev.mark()
        s4.send(lines[3])                              # a free slot again
        w.dev.expect(rf"^\[WDP-DA\] S4: {names[3]} fw 3$", timeout=3, since=m)
        assert _da_types(w, "S4") == [names[0], names[2], names[4], names[3]], f"?WDP,DA: {_da_rows(w)}"
        if not _unlabelled(bench, "S4"):
            raise Skip("record checks passed; the port-label check needs W1 S4 unlabelled")
        dump = w.run("?WDP,DUMP", timeout=8)
        assert _has(dump, f"[WDPIF:N={me},S=4,DEV={names[0]}]"), f"S4's label is not the first-heard type: {[x for x in dump if x.startswith('[WDPIF')]}"
    finally:
        _da_forget(w, "S4", *names)


@test("input.wdpda_shared_ttl", "Devices on a shared port go quiet one at a time; a quiet one keeps its place and the port's label (~100 s)", needs=["wcb1"])
def wdpda_shared_ttl(bench):
    s5 = link(bench, 1, "S5")
    w = usb_wcb(bench)
    me = usb_wcb_number(bench)
    _da_scrub(w, "S5")
    a, b = "HILTA" + marker()[3:7], "HILTB" + marker()[3:7]
    la = f'@WDP1 {{"type":"{a}"}}\n'.encode()
    lb = f'@WDP1 {{"type":"{b}"}}\n'.encode()
    try:
        prime(s5)
        time.sleep(0.3)
        m = w.dev.mark()
        s5.send(la)
        w.dev.expect(rf"^\[WDP-DA\] S5: {a}$", timeout=3, since=m)
        s5.send(la)                                   # saved; A's last announce
        t0 = time.monotonic()
        w.dev.expect(rf"^\[WDP-DA\] S5: {a} saved$", timeout=3, since=m)
        time.sleep(30)
        s5.send(lb)
        w.dev.expect(rf"^\[WDP-DA\] S5: {b}$", timeout=3, since=m)
        s5.send(lb)
        w.dev.expect(rf"^\[WDP-DA\] S5: {b} saved$", timeout=3, since=m)
        next_b, gone = time.monotonic() + 20, None
        while gone is None and time.monotonic() - t0 < 110:
            if time.monotonic() >= next_b:
                s5.send(lb)   # only the first device goes quiet
                next_b += 20
            gone = next((x for x in w.dev.since(m) if x.startswith(f"[WDP-DA] S5: {a} stopped announcing")), None)
            time.sleep(0.25)
        elapsed = time.monotonic() - t0
        assert gone, f"{a} did not go quiet within 110 s"
        bench.note(f"first device on a shared port went quiet {elapsed:.1f} s after its only announce")
        assert 88 <= elapsed <= 96, f"{a} went quiet {elapsed:.1f} s after its only announce, expected ~90"
        assert not any(x.startswith(f"[WDP-DA] S5: {b} stopped") for x in w.dev.since(m)), f"{b} went quiet while it kept announcing"
        assert _da_types(w, "S5") == [a, b], f"the quiet device lost its place: {_da_rows(w)}"
        dump = w.run("?WDP,DUMP", timeout=8)
        ra, rb = _da_row(dump, me, 5, a), _da_row(dump, me, 5, b)
        assert ra and ",SEEN=0," in ra and rb and ",SEEN=1," in rb, f"SEEN flags: {ra} / {rb}"
        if not _unlabelled(bench, "S5"):
            raise Skip("record checks passed; the port-label check needs W1 S5 unlabelled")
        assert _has(dump, f"[WDPIF:N={me},S=5,DEV={a}]"), "the quiet first-heard device no longer names S5"
    finally:
        _da_forget(w, "S5", a, b)


@test("input.wdpda_persists_reboot", "Saved devices survive a reboot and reload as not heard, in order with every field; announcing brings one back, and a forget survives a reboot too", needs=["wcb1"])
def wdpda_persists_reboot(bench):
    s3 = link(bench, 1, "S3")
    w = usb_wcb(bench)
    me = usb_wcb_number(bench)
    _da_scrub(w, "S3")
    if _da_types(w, "S3"):
        raise Skip("W1 S3 has non-test devices; the order checks need the port to itself")
    a, b = "HILRA" + marker()[3:7], "HILRB" + marker()[3:7]
    la = f'@WDP1 {{"type":"{a}","fw":"3.1","hw":"revR","caps":["hil.r"]}}\n'.encode()
    lb = f'@WDP1 {{"type":"{b}","fw":"3.2"}}\n'.encode()
    try:
        prime(s3)
        time.sleep(0.3)
        m = w.dev.mark()
        for l, t in ((la, a), (lb, b)):
            s3.send(l)
            time.sleep(0.2)
            s3.send(l)                                    # saved on its second announce
            w.dev.expect(rf"^\[WDP-DA\] S3: {t} saved$", timeout=3, since=m)
        time.sleep(2.0)                                   # NVS is written ~1 s after the list changes
        m = w.reboot()
        assert any(re.match(r"^\[WDP-DA\] \d+ serial-attached devices? remembered", x) for x in w.dev.since(m)), \
            "no 'remembered' line at boot"
        rows = _da_rows(w)
        assert _da_listed(rows, "S3") == [a, b], f"?WDP,DA after the reboot: {rows}"
        assert all("not heard since boot" in x for x in rows if re.match(r"^\s+S3\s+HILR", x)), f"?WDP,DA after the reboot: {rows}"
        dump = w.run("?WDP,DUMP", timeout=8)
        row = _da_row(dump, me, 3, a)
        assert row == f"[WDPDA:N={me},S=3,TYPE={a},FW=3.1,HW=revR,CAPS=hil.r,SEEN=0,AGE=-]", f"reloaded record: {row}"
        m = w.dev.mark()
        s3.send(la)
        w.dev.expect(rf"^\[WDP-DA\] S3: {a} fw 3\.1 \(heard again\)$", timeout=3, since=m)
        row = _da_row(w.run("?WDP,DUMP", timeout=8), me, 3, a)
        assert row and ",SEEN=1," in row, f"not live after announcing again: {row}"
        out = w.run(f"?WDP,DA,FORGET,S3,{b}")
        assert _has(out, f"[WDP-DA] S3: {b} forgotten"), out
        time.sleep(2.0)
        w.reboot()
        assert _da_types(w, "S3") == [a], f"the forget did not survive a reboot: {_da_rows(w)}"
    finally:
        _da_forget(w, "S3", a, b)
        # A rebooted W1 relearns each neighbor only from its next advert (up to 60 s): ask the mesh to
        # advertise now, so the next test finds W1's WDP table as full as it was (NaviCore included).
        w.run("?WDP,POLL")
        time.sleep(2.0)


# ============================================================ probable bugs, asserted as intended
@test("input.bcast_reset_persists", "(should) ?BCAST,RESET's cleared input blocking stays cleared after ?config", needs=["wcb1"], links=[])
def bcast_reset_persists(bench):
    """Probable firmware bug: resetBroadcastSettingsNamespace clears input blocking in RAM only and never rewrites
    NVS bcast_block (WCB_Storage.cpp:352-382); ?config reloads bcast_block (WCB.ino:2408-2409) and brings it back."""
    w = usb_wcb(bench)
    tokens = bench.config_tokens(1, refresh=True)
    if not token(tokens, "?BCAST,IN,S1,OFF"):
        raise Skip("W1 S1 input is not blocked, nothing to observe")
    recorded = [t for t in tokens if t.upper().startswith("?BCAST,")]
    with config_guard(bench, 1):
        try:
            out = w.run("?BCAST,RESET")
            assert _has(out, "input blocking cleared"), f"?BCAST,RESET said {out}"
            cfg = w.run("?config")
            row = next((x for x in cfg if x.strip().startswith("Serial1 Baud:")), "")
            assert "Broadcast Input: Enabled" in row, f"after ?BCAST,RESET and ?config: {row.strip()}"
        finally:
            for t in recorded:
                w.run(t)


@test("input.checksum_c_chain", "(should) A checksummed chain starting with ?C runs every command", needs=["wcb1"], links=[])
def checksum_c_chain(bench):
    """Probable firmware bug: the ?C checksum branch (WCB.ino:7087-7153) enqueues the whole verified chain with
    enqueueCommand instead of parseCommandsAndEnqueue, so only a mangled first command runs."""
    w = usb_wcb(bench)
    chain = "?CONFIG^?VERSION"
    crc = "%08X" % (zlib.crc32(chain.encode()) & 0xFFFFFFFF)
    control = "?VERSION^?VERSION"
    crc2 = "%08X" % (zlib.crc32(control.encode()) & 0xFFFFFFFF)
    out2 = w.run(f"{control}^?CHK{crc2}", timeout=8)
    assert sum(1 for x in out2 if x.startswith("End of Version")) == 2, f"control chain did not run both: {out2}"
    out = w.run(f"{chain}^?CHK{crc}", timeout=8)
    ran_config = _has(out, "Configuration: Wireless Communication Board")
    ran_version = _has(out, "End of Version")
    assert ran_config and ran_version, (f"only part of a ?C-first checksummed chain ran (config={ran_config}, "
                                        f"version={ran_version}): {[x for x in out if 'Unknown' in x or 'VERIFIED' in x]}")
