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

from hil.runner import Skip, test
from suites.common import (Console, Watch, config_guard, link, marker, padded, prime, quiet_lines,
                           require_tokens, send_chunked, token, usb_wcb)


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
    """Probable firmware bug: applySoftSerialIntTx (WCB.ino:2056-2068) turns every S3-S5 TX byte into a core-1
    critical section, masking the soft-RX edge ISR; ;S3 also flush()es unread S3 input away (WCB.ino:6506).
    Arms C (no load) and B (hardware-port load) are controls."""
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
            for i in range(0, 40, 10):
                s3.send(b"".join(x.encode() + b"\r" for x in lines[i:i + 10]))
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
    assert counts[19200] == 20 and counts[38400] == 20, f"soft RX exact lines of 20 per baud: {counts}"


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


@test("input.softserial_inttx_state", "Soft-port TX protection is reported per port and recomputed at ?BAUD, not when a port's role changes", needs=["wcb1"], links=[])
def softserial_inttx_state(bench):
    w = usb_wcb(bench)
    if token(bench.config_tokens(1, refresh=True), "?MAP,SERIAL,S5"):
        raise Skip("W1 S5 already has a serial mapping")
    protected = "bit-bang TX: interrupt-protected (loop-task only)"
    problems = []
    with config_guard(bench, 1):
        try:
            w.run("?DEBUG,ON")
            for n in "345":
                out = w.run(f"?BAUD,S{n},9600")
                if not _has(out, f"[SOFTSERIAL] S{n} {protected}"):
                    problems.append(f"?BAUD,S{n}: {out}")
            if any("[SOFTSERIAL]" in x for x in w.run("?BAUD,S2,9600")):
                problems.append("?BAUD,S2 printed a [SOFTSERIAL] line for a hardware port")
            out = w.run("?MAP,SERIAL,S5,R,S4")
            if not _has(out, "Serial mapping set: Serial5 (RAW) -> 1 destination(s)") or _has(out, "[SOFTSERIAL]"):
                problems.append(f"?MAP,SERIAL,S5,R,S4: {out}")
            if not _has(w.run("?BAUD,S5,9600"), "[SOFTSERIAL] S5 bit-bang TX: unprotected (a core-0 task can write this port)"):
                problems.append("a raw-mapped S5 was not reported unprotected after ?BAUD")
            out = w.run("?MAP,SERIAL,CLEAR,S5")
            if not _has(out, "Serial mapping removed for Serial5"):
                problems.append(f"?MAP,SERIAL,CLEAR,S5: {out}")
            if not _has(w.run("?BAUD,S5,9600"), f"[SOFTSERIAL] S5 {protected}"):
                problems.append("S5 was not protected again after the mapping was cleared")
        finally:
            w.run("?DEBUG,OFF")
    assert not problems, "; ".join(problems)


# ============================================================ WDP device announce
def _da_rows(w):
    return w.run("?WDP,DA")


def _da_present(w, port):
    return any(re.match(rf"^\s+{port}\s", x) for x in _da_rows(w))


@test("input.wdpda_basic", "An @WDP1 announce on a port is recorded, printed once, and never broadcast", needs=["wcb1"])
def wdpda_basic(bench):
    s2, s3, s4 = link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 1, "S4")
    w2s3 = bench.links.get(2, "S3")
    w = usb_wcb(bench)
    if _da_present(w, "S3"):
        raise Skip("S3 still has a WDP-DA entry (90 s TTL)")
    name = "HILDA" + marker()[3:7]
    line = f'@WDP1 {{"type":"{name}","fw":"9.8.7"}}\n'.encode()
    prime(s3)
    time.sleep(0.3)
    watch = Watch(s2, s4, w2s3)
    m = w.dev.mark()
    s3.send(line)
    time.sleep(1.0)
    s3.send(line)
    time.sleep(1.0)
    prints = [x for x in w.dev.since(m) if x.startswith(f"[WDP-DA] S3: {name} fw 9.8.7")]
    assert len(prints) == 1, f"expected one [WDP-DA] line, got {prints}"
    watch.silent(s2, s4, w2s3, window=0.5)
    rows = _da_rows(w)
    assert any(re.match(rf"^\s+S3\s+{name}\s+fw 9\.8\.7", x) for x in rows), f"?WDP,DA: {rows}"


@test("input.wdpda_fields", "WDP-DA optional hw/caps, 24/27-char truncation, and ',' ']' scrubbing", needs=["wcb1"])
def wdpda_fields(bench):
    s4, s5 = link(bench, 1, "S4"), link(bench, 1, "S5")
    w = usb_wcb(bench)
    if _da_present(w, "S4") or _da_present(w, "S5"):
        raise Skip("S4/S5 still have WDP-DA entries (90 s TTL)")
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


@test("input.wdpda_usb_and_blocked_port", "WDP-DA is ignored on USB but honoured on an input-blocked port", needs=["wcb1"])
def wdpda_usb_and_blocked_port(bench):
    s2, s3, s4, s5 = link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 1, "S4"), link(bench, 1, "S5")
    w = usb_wcb(bench)
    if _da_present(w, "S2"):
        raise Skip("S2 still has a WDP-DA entry (90 s TTL)")
    with config_guard(bench, 1):
        w.run("?BCAST,IN,S2,OFF")
        watch = Watch(s3, s4, s5)
        m = w.dev.mark()
        w.send('@WDP1 {"type":"HILDU","fw":"1"}')
        watch.silent(s3, s4, s5, window=1.5)
        assert not any(x.startswith("[WDP-DA]") for x in w.dev.since(m)), "a USB @WDP1 line was accepted"
        prime(s2)
        time.sleep(0.3)
        m = w.dev.mark()
        s2.send(b'@WDP1 {"type":"HILDV","fw":"1"}\n')
        w.dev.expect(r"^\[WDP-DA\] S2: HILDV fw 1$", timeout=3, since=m)
        time.sleep(0.5)
        assert not any("Broadcast blocked from Serial2" in x for x in w.dev.since(m)), "the announce was treated as a broadcast"
        w.run("?BCAST,IN,S2,ON")


@test("input.wdpda_rejects", "Malformed or foreign @WDP lines vanish; lowercase @wdp1 is ordinary text", needs=["wcb1"])
def wdpda_rejects(bench):
    s2, s3, s4, s5 = link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 1, "S4"), link(bench, 1, "S5")
    require_tokens(bench, 1, "?BCAST,IN,S5,ON", "?BCAST,OUT,S2,ON", "?BCAST,OUT,S3,ON", "?BCAST,OUT,S4,ON")
    w = usb_wcb(bench)
    if _da_present(w, "S5"):
        raise Skip("S5 still has a WDP-DA entry (90 s TTL)")
    prime(s5)
    time.sleep(0.3)
    watch = Watch(s2, s3, s4)
    m = w.dev.mark()
    for bad in (b'@WDP1 {"fw":"1"}\n', b'@WDP1 {"type":""}\n', b"@WDP1 nojson\n", b'@WDP2 {"type":"HILX"}\n', b"@WDPjunk\n"):
        s5.send(bad)
        time.sleep(0.3)
    watch.silent(s2, s3, s4, window=1.0)
    assert not any(x.startswith("[WDP-DA]") for x in w.dev.since(m)), "a malformed announce was accepted"
    assert not _da_present(w, "S5"), "S5 was recorded from a malformed announce"
    watch = Watch(s2, s3, s4)
    s5.send(b'@wdp1 {"type":"HILDF"}\n')
    for l in (s2, s3, s4):
        watch.expect(l, b'@wdp1 {"type":"HILDF"}\r', timeout=2)


@test("input.wdpda_ttl", "A WDP-DA entry expires 90 s after the last announce; re-announcing refreshes it (~3 min)", needs=["wcb1"])
def wdpda_ttl(bench):
    s3 = link(bench, 1, "S3")
    w = usb_wcb(bench)
    deadline = time.monotonic() + 100
    while _da_present(w, "S3"):
        if time.monotonic() > deadline:
            raise Skip("S3 WDP-DA entry never expired")
        time.sleep(5)
    name = "HILDT" + marker()[3:7]
    line = f'@WDP1 {{"type":"{name}"}}\n'.encode()
    prime(s3)
    time.sleep(0.3)
    m = w.dev.mark()
    s3.send(line)
    t0 = time.monotonic()
    time.sleep(60)
    s3.send(line)
    gone = w.dev.expect(rf"^\[WDP-DA\] S3: {name} stopped announcing", timeout=110, since=m)
    elapsed = time.monotonic() - t0
    bench.note(f"WDP-DA expiry {elapsed:.1f} s after the first announce ({gone.group(0)})")
    assert 148 <= elapsed <= 156, f"expired {elapsed:.1f} s after the first announce, expected ~150 (60 + 90)"


@test("input.wdpda_propagation", "A WDP-DA type on W2's unlabeled S4 appears in W1's WDP dump and clears on expiry (~100 s)", needs=["wcb1"])
def wdpda_propagation(bench):
    s4 = link(bench, 2, "S4")
    tokens = bench.config_tokens(2, refresh=True)
    if token(tokens, "?LABEL,S4,") or token(tokens, "?WDP,OFF"):
        raise Skip("W2 S4 is labelled or W2 has WDP off")
    w = usb_wcb(bench)
    name = "HILDP" + marker()[3:7]
    with Console(bench, 2) as c2:
        prime(s4)
        time.sleep(0.3)
        m = c2.mark()
        s4.send(f'@WDP1 {{"type":"{name}","fw":"1.0"}}\n'.encode())
        c2.expect(rf"\[WDP-DA\] S4: {name} fw 1\.0", timeout=4, since=m)
        time.sleep(3)
        dump = w.run("?WDP,DUMP", timeout=8)
        assert any(f"[WDPIF:N=2,S=4,DEV={name}]" in x for x in dump), "W1's WDP dump does not show W2 S4's announced type"
        c2.expect(rf"\[WDP-DA\] S4: {name} stopped announcing", timeout=100, since=m)
        time.sleep(3)
        dump = w.run("?WDP,DUMP", timeout=8)
        assert not any("[WDPIF:N=2,S=4," in x for x in dump), "W2 S4 still labelled in W1's dump after expiry"


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
