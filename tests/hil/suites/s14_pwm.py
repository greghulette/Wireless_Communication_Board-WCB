"""PWM — ;P pulses, ?MAP,PWM output ports, local and mesh passthrough, WDP PWMTARGET auto-config.

Built from the verified pwm specs. Most tests reboot: a ?MAP,PWM mapping reboots once the command
queue has been quiet for 4 s (pwmRebootPending, WCB.ino:966), and ?MAP,PWM,CLEAR,OUT / ?PX defer their
restart the same way (tracker #9), so all three are sent with send() and a boot wait, never run().

"(should)" tests assert intended behaviour where the firmware has a probable bug. Rules from the specs:
never ;P or map onto W2S1 (the real Maestro); hold a WCB PWM input with PWMOUT 0, never leave it
floating; restore a remote output explicitly with ;W<n>,?MAP,PWM,CLEAR,OUT,S<p> — ?MAP,PWM,CLEAR,ALL
never reaches an ETM-on board.
"""
import re
import time

from hil.runner import Skip, test
from suites.common import Console, Watch, config_guard, link, marker, quiet_lines, require_tokens, snapshot, token, usb_wcb


def _has(lines, text):
    return any(text in x for x in lines)


def _no_pwm(bench, *wcbs):
    for n in wcbs:
        if any(t.upper().startswith("?MAP,PWM") for t in bench.config_tokens(n, refresh=True)):
            raise Skip(f"W{n} already has PWM configuration")


def _pwm_reboot(w, since, timeout=30):
    """Wait for the deferred PWM reboot and the boot after it."""
    w.dev.expect(r"^Rebooting now to apply PWM configuration", timeout=timeout, since=since)
    w.wait_boot(since, timeout=30)


def _inline_clear_out(w, port):
    """Clear a PWM output port and wait out its reboot - deferred now, not taken inline."""
    m = w.send(f"?MAP,PWM,CLEAR,OUT,{port}")
    w.dev.expect(r"^PWM output cleared", timeout=5, since=m)
    _pwm_reboot(w, m)
    return m


def _w2_reboot_wait(w, since, timeout=40):
    w.dev.expect(r"^\[ETM\] WCB2 came ONLINE \(boot\)", timeout=timeout, since=since)
    time.sleep(3)


def _clear_remote_out(w, port):
    m = w.send(f";W2,?MAP,PWM,CLEAR,OUT,{port}")
    _w2_reboot_wait(w, m)


def _clear_local_mapping(w, port):
    m = w.send(f"?MAP,PWM,CLEAR,{port}")
    try:
        _pwm_reboot(w, m)
    except AssertionError:
        pass


def _stats_pwm(w):
    lines = w.run("?STATS")
    for i, x in enumerate(lines):
        if x.startswith("PWM Passthrough:"):
            m = re.search(r"Attempts: (\d+), Success: (\d+), Failed: (\d+)", lines[i + 1] if i + 1 < len(lines) else "")
            return tuple(int(v) for v in m.groups()) if m else None
    return None


def _dump_cap(w, wcb):
    for x in w.run("?WDP,DUMP", timeout=8):
        m = re.match(rf"^\[WDP:N={wcb},.*CAP=([0-9A-Fa-f]+)", x)
        if m:
            return int(m.group(1), 16)
    return None


# ============================================================ ;P pulses
@test("pwm.p_output_port", ";P on a declared PWM output gives one pulse of the commanded width; bad ;P forms are rejected (1 reboot)", needs=["wcb1"])
def p_output_port(bench):
    s4 = link(bench, 1, "S4")
    w = usb_wcb(bench)
    _no_pwm(bench, 1)
    require_tokens(bench, 1, "?BCAST,OUT,S4,ON", "?BCAST,IN,S4,ON")
    probe = s4.probe
    with config_guard(bench, 1):
        try:
            assert _has(w.run("?MAP,PWM,OUT,S4"), "Serial4 configured as PWM output port")
            s4.pwm_in()
            time.sleep(0.6)
            bad = []
            for width in (500, 1000, 1500, 2000, 2500):
                m = probe.dev.mark()
                wm = w.dev.mark()
                w.send(f";P4{width}")
                time.sleep(0.6)
                got = s4.pulses(m)
                if len(got) != 1 or got[0][1] != 1 or abs(got[0][0] - width) > 40:
                    bad.append(f"{width}: {got}")
                if quiet_lines(w.dev, wm):
                    bad.append(f"{width}: USB printed {quiet_lines(w.dev, wm)}")
            bench.note(f"p_output_port accuracy problems: {bad}")
            assert not bad, "; ".join(bad)
            w.run("?DEBUG,PWM,ON")
            for cmd in (";P4499", ";P42501", ";P01500", ";P61500", ";P4,1500", ";P4"):
                m = probe.dev.mark()
                wm = w.dev.mark()
                w.send(cmd)
                w.dev.expect(rf"^\[PWM\] Invalid ;P command '{re.escape(cmd[1:])}'", timeout=2, since=wm)
                time.sleep(0.5)
                assert not s4.pulses(m), f"{cmd} produced a pulse"
            for cmd, width in ((";p41500", 1500), (";P41500xyz", 1500)):
                m = probe.dev.mark()
                w.send(cmd)
                time.sleep(0.6)
                got = s4.pulses(m)
                assert len(got) == 1 and abs(got[0][0] - width) <= 40, f"{cmd}: {got}"
            assert _has(w.run("?DEBUG,PWM,OFF"), "PWM debugging disabled")
            wm = w.dev.mark()
            w.send(";P4499")
            time.sleep(0.6)
            assert not quiet_lines(w.dev, wm), "an invalid ;P printed with PWM debug off"
        finally:
            w.run("?DEBUG,PWM,OFF")
            s4.pwm_stop()
            _inline_clear_out(w, "S4")


@test("pwm.p_undeclared_softport", ";P on an ordinary soft-serial port gives a real pulse from the first call and parks the line LOW; the port still transmits afterwards", needs=["wcb1"])
def p_undeclared_softport(bench):
    s4 = link(bench, 1, "S4")
    w = usb_wcb(bench)
    _no_pwm(bench, 1)
    if s4.line_level() != 1:
        w.send(";S4U")
        time.sleep(0.5)
    assert s4.line_level() == 1, "W1 S4's TX line is not idling high"
    probe = s4.probe
    try:
        s4.pwm_in()
        time.sleep(0.6)
        m = probe.dev.mark()
        w.send(";P41500")
        time.sleep(0.6)
        first = s4.pulses(m)
        m = probe.dev.mark()
        w.send(";P41500")
        time.sleep(0.6)
        second = s4.pulses(m)
        # S3-S5 TX is RMT (WCB_SoftSerial.h): the pin's GPIO latch was never written, so pinMode drives it LOW and the
        # FIRST ;P is already a real pulse. (Bit-banged TX left the latch HIGH, so the first ;P had no rising edge.)
        assert len(first) == 1 and abs(first[0][0] - 1500) <= 40, f"first ;P: {first}"
        assert len(second) == 1 and abs(second[0][0] - 1500) <= 40, f"second ;P: {second}"
    finally:
        s4.pwm_stop()
    # ;P left S4 LOW, so the next burst's first start bit has no falling edge: the probe's receiver locks onto an
    # edge inside the data and, with bytes back to back, stays misframed for a data-dependent run of bytes (a whole
    # marker, on the bench). One throwaway line ends on a stop bit and leaves the line idling HIGH.
    w.send(";S4U")
    time.sleep(0.5)
    t = marker()
    watch = Watch(s4)
    w.send(f";S4,{t}")
    watch.expect(s4, t.encode() + b"\r", timeout=2)


@test("pwm.p_kills_hw_uart", ";P on a hardware-UART port detaches UART TX until reboot; ?BAUD does not revive it (1 reboot)", needs=["wcb1"])
def p_kills_hw_uart(bench):
    s2 = link(bench, 1, "S2")
    w = usb_wcb(bench)
    require_tokens(bench, 1, "?BAUD,S2,9600", "?MAESTRO,REMOTE")
    m1, m2, m3, m4 = (marker(x) for x in "abcd")
    with config_guard(bench, 1):
        watch = Watch(s2)
        w.send(f";S2,{m1}")
        watch.expect(s2, m1.encode() + b"\r", timeout=2)
        w.send(";P21500")
        time.sleep(0.5)
        watch = Watch(s2)
        w.send(f";S2,{m2}")
        time.sleep(2.0)
        w.run("?BAUD,S2,9600")
        w.send(f";S2,{m3}")
        time.sleep(2.0)
        dead = watch.got(s2)
        w.reboot()
        watch = Watch(s2)
        w.send(f";S2,{m4}")
        watch.expect(s2, m4.encode() + b"\r", timeout=3)
        assert m2.encode() not in dead and m3.encode() not in dead, f"S2 still transmitted after ;P: {dead!r}"


@test("pwm.p_refused_on_maestro_port", "(should) ;P is refused on a Maestro port instead of silencing that Maestro (1 reboot)", needs=["wcb1"])
def p_refused_on_maestro_port(bench):
    """Probable firmware bug: processPWMOutput's owned-port guard (WCB.ino:6837-6843) has no term for Maestro ports,
    so ;P1 detaches the Maestro's UART TX until reboot; canUsePWMOnPort (WCB_PWM.cpp:56-61) is never called for ;P."""
    s1 = link(bench, 1, "S1")
    w = usb_wcb(bench)
    require_tokens(bench, 1, "?MAESTRO,REMOTE", "?MAESTRO,M1:W1S1:57600")
    frame = bytes.fromhex("AA012701")
    try:
        watch = Watch(s1)
        w.send(";M11")
        watch.expect(s1, frame, timeout=2)
        w.send(";P11500")
        time.sleep(0.5)
        watch = Watch(s1)
        wm = w.dev.mark()
        w.send(";M11")
        w.dev.expect(r"Maestro 1: Local S1, Script 1", timeout=2, since=wm)
        time.sleep(1.5)
        assert frame in watch.got(s1), "the Maestro port stopped transmitting after ;P11500"
    finally:
        w.reboot()


@test("pwm.p_guard_wled_port_remote", "A ;P forwarded to W2's WLED port is refused before touching the pin, and the UART keeps working", needs=["wcb1"])
def p_guard_wled_port_remote(bench):
    w2s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    require_tokens(bench, 2, "?WLED,1:W2S2:115200")
    t = marker()
    with Console(bench, 2) as c2:
        try:
            w.run("?DEBUG,ETM,ON")
            m = c2.send("?DEBUG,PWM,ON")
            c2.expect("PWM debugging enabled", since=m)
            watch = Watch(w2s2)
            wm = w.dev.mark()
            cm = c2.mark()
            w.send(";W2,;P21500")
            c2.expect(r"\[PWM\] Ignoring ;P on S2 .* the port is in use by another device", timeout=4, since=cm)
            time.sleep(0.5)
            assert not w2s2.errors(watch.marks[w2s2.key]), "the WLED port's line was disturbed"
            assert not any(re.search(r"\[ETM\] Sent seq \d+: ;P21500", x) for x in w.dev.since(wm)), ";P went over ETM"
            w.send(f";W2,;S2,{t}")
            watch.expect(w2s2, t.encode() + b"\r", timeout=3)
        finally:
            c2.send("?DEBUG,PWM,OFF")
            w.run("?DEBUG,ETM,OFF")


@test("pwm.p_forwarded_stats", ";W2,;P travels non-ETM and is counted under PWM Passthrough", needs=["wcb1"])
def p_forwarded_stats(bench):
    w2s3 = link(bench, 2, "S3")
    w = usb_wcb(bench)
    _no_pwm(bench, 2)
    if w2s3.line_level() != 1:
        w.send(";W2;S3U")
        time.sleep(1.0)
    probe = w2s3.probe
    try:
        w.run("?DEBUG,ETM,ON")
        assert _has(w.run("?STATS,RESET"), "ESP-NOW statistics reset.")
        assert _stats_pwm(w) is None, "PWM Passthrough block present right after a reset"
        w2s3.pwm_in()
        time.sleep(0.6)
        wm = w.dev.mark()
        m = probe.dev.mark()
        w.send(";W2,;P31500")
        time.sleep(0.8)
        first = w2s3.pulses(m)
        m = probe.dev.mark()
        w.send(";W2,;P31500")
        time.sleep(0.8)
        second = w2s3.pulses(m)
        stats = _stats_pwm(w)
        sent = [x for x in w.dev.since(wm) if re.search(r"\[ETM\] Sent seq \d+: ;P31500", x)]
        # With RMT TX the first ;P is a real pulse too - see pwm.p_undeclared_softport.
        assert len(first) == 1 and abs(first[0][0] - 1500) <= 50, f"first ;P: {first}"
        assert len(second) == 1 and abs(second[0][0] - 1500) <= 50, f"second ;P: {second}"
        assert stats == (2, 2, 0), f"PWM Passthrough stats {stats}, expected Attempts 2 Success 2 Failed 0"
        assert not sent, f";P went over ETM: {sent}"
    finally:
        w2s3.pwm_stop()
        w.run("?DEBUG,ETM,OFF")
        w.send(";W2;S3U")


# ============================================================ output ports
@test("pwm.out_port_lines_and_guards", "?MAP,PWM,OUT listing, idempotence, legacy ?PO, guards and serial-map conflict (1 reboot)", needs=["wcb1"], links=[])
def out_port_lines_and_guards(bench):
    w = usb_wcb(bench)
    _no_pwm(bench, 1)
    require_tokens(bench, 1, "?BCAST,OUT,S4,ON", "?BCAST,IN,S4,ON", "?MAESTRO,REMOTE")
    with config_guard(bench, 1):
        try:
            assert _has(w.run("?MAP,PWM,OUT,S4"), "Serial4 configured as PWM output port")
            assert _has(w.run("?MAP,PWM,OUT,S4"), "Serial4 already configured as PWM output")
            assert _has(w.run("?PO4"), "Serial4 already configured as PWM output")
            lst = [x.rstrip() for x in w.run("?MAP,PWM,LIST")]
            for want in ("PWM Mappings:", "No input mappings configured", "PWM Output-Only Ports:",
                         "Configured outputs: S4", "--------------- End PWM Mappings ---------------"):
                assert want in lst, f"LIST lacks {want!r}: {lst}"
            cfg = w.run("?config")
            assert any(re.match(r"^  Serial4: Reserved for PWM Output  Pins: Tx:\d+ Rx:\d+", x) for x in cfg), "?config lacks the reserved row"
            assert _has(w.run("?MAP,PWM,OUT,S6"), "Invalid port number. Must be 1-5")
            assert _has(w.run("?MAP,PWM,OUT,S0"), "Invalid port number. Must be 1-5")
            assert _has(w.run("?MAP,PWM,OUT,S1"), "Cannot use PWM on Serial1 - reserved for Maestro/Kyber")
            assert _has(w.run("?MAP,SERIAL,S4,S5"), "Cannot create mapping: Port is configured as PWM output")
            tokens = snapshot(bench, 1)
            assert "?MAP,PWM,OUT,S4" in tokens and not any(t.startswith("?MAP,SERIAL,S4") for t in tokens)
        finally:
            _inline_clear_out(w, "S4")


@test("pwm.clear_out_restores_flags", "(should) Clearing a PWM output port restores its deliberately-OFF broadcast flags (1 reboot)", needs=["wcb1"], links=[])
def clear_out_restores_flags(bench):
    """Restore-fidelity bug: removePWMOutputPort forces serialBroadcastEnabled = true and blockBroadcastFrom = false
    (WCB_PWM.cpp:876-881), wiping a user's intentional OFF — same pattern as ?MAP,SERIAL,CLEAR."""
    w = usb_wcb(bench)
    _no_pwm(bench, 1)
    require_tokens(bench, 1, "?BCAST,OUT,S4,ON", "?BCAST,IN,S4,ON")
    with config_guard(bench, 1):
        try:
            w.run("?BCAST,OUT,S4,OFF")
            w.run("?BCAST,IN,S4,OFF")
            w.run("?MAP,PWM,OUT,S4")
            _inline_clear_out(w, "S4")
            tokens = snapshot(bench, 1)
            assert "?BCAST,OUT,S4,OFF" in tokens and "?BCAST,IN,S4,OFF" in tokens, \
                f"S4 broadcast flags after the clear: {[t for t in tokens if t.startswith('?BCAST') and ',S4,' in t]}"
        finally:
            if any(t.startswith("?MAP,PWM,OUT,S4") for t in snapshot(bench, 1)):
                _inline_clear_out(w, "S4")
            w.run("?BCAST,OUT,S4,ON")
            w.run("?BCAST,IN,S4,ON")


@test("pwm.bcast_and_serial_on_output_port", "Broadcasts skip a PWM output port; ;S never writes serial bytes onto it, live or after reboot, and it boots LOW (2 reboots)", needs=["wcb1"])
def bcast_and_serial_on_output_port(bench):
    s4, s5 = link(bench, 1, "S4"), link(bench, 1, "S5")
    w = usb_wcb(bench)
    _no_pwm(bench, 1)
    require_tokens(bench, 1, "?BCAST,OUT,S4,ON", "?BCAST,OUT,S5,ON", "?BCAST,IN,S4,ON")
    with config_guard(bench, 1):
        try:
            w.run("?MAP,PWM,OUT,S4")
            b = marker("b")
            watch = Watch(s4, s5)
            w.send(b)
            watch.expect(s5, b.encode() + b"\r", timeout=2)
            time.sleep(1.0)
            assert b.encode() not in watch.got(s4), "a broadcast reached the PWM output port"
            # A declared PWM output carries servo pulses, so serial bytes must never land on it. With bit-banged TX a
            # live-declared port still took ;S bytes until the next reboot; WcbSoftSerial::write drops them (RMT TX).
            m1 = marker("m")
            watch = Watch(s4)
            w.send(f";S4,{m1}")
            time.sleep(2.0)
            assert m1.encode() not in watch.got(s4), ";S4 wrote serial bytes onto a live PWM output port"
            m = w.reboot()
            boot = w.dev.since(m)
            assert any(re.match(r"^  Serial4: Reserved for PWM Output", x) for x in boot), "boot banner lacks the reserved row"
            assert _has(boot, "Serial4 reserved for PWM - skipping UART init")
            assert s4.line_level() == 0, "S4 did not boot LOW"
            m2 = marker("n")
            watch = Watch(s4)
            w.send(f";S4,{m2}")
            time.sleep(2.0)
            assert m2.encode() not in watch.got(s4), ";S4 wrote to a port whose UART was never begun"
        finally:
            _inline_clear_out(w, "S4")


@test("pwm.bcast_skips_remote_output_port", "A PWM output port declared on W2 over the mesh drops broadcasts and sets W2's PWM cap bit (1 W2 reboot)", needs=["wcb1"])
def bcast_skips_remote_output_port(bench):
    w2s3, w2s4 = link(bench, 2, "S3"), link(bench, 2, "S4")
    w = usb_wcb(bench)
    _no_pwm(bench, 2)
    require_tokens(bench, 2, "?BCAST,OUT,S3,ON", "?BCAST,IN,S3,ON", "?BCAST,OUT,S4,ON")
    with config_guard(bench, 1, 2):
        try:
            w.send(";W2,?MAP,PWM,OUT,S3")
            time.sleep(2.0)
            assert "?MAP,PWM,OUT,S3" in snapshot(bench, 2), "W2 config lacks the output declaration"
            w.run("?WDP,POLL")
            time.sleep(2.0)
            cap = _dump_cap(w, 2)
            assert cap is not None and cap & 0x0020, f"W2 CAP={cap} lacks the PWM bit 0x0020"
            r = marker("r")
            watch = Watch(w2s3, w2s4)
            w.send(r)
            watch.expect(w2s4, r.encode() + b"\r", timeout=3)
            time.sleep(1.0)
            assert r.encode() not in watch.got(w2s3), "a broadcast reached W2's PWM output port"
        finally:
            _clear_remote_out(w, "S3")


@test("pwm.clear_out_keeps_queue", "(should) ?MAP,PWM,CLEAR,OUT and ?PX do not reboot when nothing changed, nor drop the commands queued behind them", needs=["wcb1"], links=[])
def clear_out_keeps_queue(bench):
    """Probable firmware bug: both restart inline with delay(3000) + ESP.restart() even when nothing was removed
    (WCB.ino:5045-5053, 5848-5855; removePWMOutputPort only prints 'was not configured', WCB_PWM.cpp:886),
    destroying every queued command — what CLAUDE.md rule 11 forbids."""
    w = usb_wcb(bench)
    _no_pwm(bench, 1)
    problems = []
    for cmd in ("?MAP,PWM,CLEAR,OUT,S4", "?PX4"):
        t = marker()
        m = w.send(f"{cmd}^;S0,{t}")
        rebooted = False
        try:
            # Any reboot form fails this test: the old inline one, or a deferred one that should
            # never have been armed because nothing was removed.
            w.dev.expect(r"^(Rebooting in 3 seconds|PWM output cleared|Rebooting now to apply PWM)",
                         timeout=6, since=m)
            rebooted = True
            w.wait_boot(m, timeout=30)
        except AssertionError:
            pass
        time.sleep(2.0)
        if rebooted:
            problems.append(f"{cmd} rebooted although S4 was not a PWM output")
        if not any(x.strip() == t for x in w.dev.since(m)):
            problems.append(f"the ;S0 queued after {cmd} never ran")
    assert not problems, "; ".join(problems)


# ============================================================ mappings and passthrough
@test("pwm.map_deferred_reboot", "A PWM mapping reboots only after the queue drains and 4 s of quiet; banner and backup order (2 reboots)", needs=["wcb1"],
      drives=["W1S4"])   # ?MAP,PWM,S3,S4 makes W1 S4 a PWM output
def map_deferred_reboot(bench):
    s3 = link(bench, 1, "S3")
    w = usb_wcb(bench)
    _no_pwm(bench, 1)
    t = marker()
    with config_guard(bench, 1):
        try:
            s3.pwm_out(0)   # hold the new input LOW: a floating pin would feed noise pulses to S4
            m = w.send(f"?MAP,PWM,S3,S4^;S0,{t}")
            w.dev.expect(r"^PWM configuration stored .* rebooting once the command queue drains\.", timeout=3, since=m)
            w.dev.expect(rf"^{t}$", timeout=3, since=m)
            last = time.monotonic()
            end = time.monotonic() + 9
            while time.monotonic() < end:
                assert not any("Rebooting now to apply PWM configuration" in x for x in w.dev.since(m)), \
                    "rebooted while commands kept arriving"
                w.version()
                last = time.monotonic()
                time.sleep(1.5)
            hit = w.dev.expect(r"^Rebooting now to apply PWM configuration", timeout=15, since=m)
            gap = time.monotonic() - last
            bench.note(f"deferred PWM reboot {gap:.1f} s after the last command")
            w.wait_boot(m, timeout=30)
            boot = w.dev.since(m)
            for pattern in (r"^  Serial3: Reserved for PWM Input", r"^  Serial4: Reserved for PWM Output",
                            r"Serial3 reserved for PWM - skipping UART init", r"Serial4 reserved for PWM - skipping UART init",
                            r"^Input: Serial3 -> Outputs: S4", r"PWM Task Created"):
                assert any(re.search(pattern, x) for x in boot), f"boot banner lacks /{pattern}/"
            tokens = snapshot(bench, 1)
            assert tokens[-1] == "?MAP,PWM,S3,S4", f"the PWM token is not last in ?backup: {tokens[-3:]}"
            assert 3.5 <= gap <= 7, f"reboot came {gap:.1f} s after the last command (PWM_REBOOT_QUIET_MS is 4000, WCB.ino:1104)"
        finally:
            _clear_local_mapping(w, "S3")
            s3.pwm_stop()


@test("pwm.map_validation", "Invalid ?MAP,PWM forms change nothing and do not reboot", needs=["wcb1"], links=[])
def map_validation(bench):
    w = usb_wcb(bench)
    _no_pwm(bench, 1)
    checks = [
        ("?MAP,PWM,S3", ["Invalid format. Use: ?MAP,PWM,Sx,dest"]),
        ("?MAP,PWM,S6,S4", ["Input port must be 1-5"]),
        ("?MAP,PWM,S1,S4", ["Cannot use PWM on Serial1 - reserved for Maestro/Kyber", "PWM mapping blocked - input port in use by Kyber/Maestro"]),
        ("?MAP,PWM,S3,X9", ["No valid outputs specified"]),
        ("?MAP,PWM,S3,W21S3", ["No valid outputs specified"]),
        ("?MAP,PWM,S3,W2", ["No valid outputs specified"]),
        ("?MAP,PWM,S3,S1", ["Cannot use PWM on Serial1 - reserved for Maestro/Kyber", "No valid outputs specified"]),
        ("?MAP,PWM,CLEAR,S3", ["No PWM mapping found for Serial3"]),
    ]
    with config_guard(bench, 1):
        m = w.dev.mark()
        bad = [f"{cmd!r} -> {out}" for cmd, wants in checks for out in [w.run(cmd)] if not all(_has(out, x) for x in wants)]
        time.sleep(6.0)
        assert not any("Rebooting" in x for x in w.dev.since(m)), "an invalid ?MAP,PWM rebooted the board"
        assert not bad, "; ".join(bad)


@test("pwm.legacy_forms", "Legacy ?PLIST, ?PRS<n> and ?PMS<n>,<dest> reach the ?MAP,PWM handlers (characterisation; nothing gets mapped, no reboot)", needs=["wcb1"], links=[])
def legacy_forms(bench):
    """WCB.ino's legacy block: ?PLIST -> listPWMMappings, ?PRS<n> -> removePWMMapping(n), ?PMS<n>,<dest> ->
    addPWMMapping("PMS<n>,<dest>"), the same string ?MAP,PWM,S<n>,<dest> builds (WCB.ino:5446). The help once advertised a
    ?PMSSx form (fixed 2026-09-24, F2 / tracker #82); that spelling reads its 'S3' as port 0 and is refused. ?PCLEAR and ?POS<n> are never sent - both can reboot."""
    w = usb_wcb(bench)
    _no_pwm(bench, 1)
    with config_guard(bench, 1):
        m = w.dev.mark()
        checks = [("?PLIST", ["PWM Mappings:", "No input mappings configured"]),
                  ("?PRS3", ["No PWM mapping found for Serial3"]),
                  ("?PMS3,X9", ["No valid outputs specified"]),
                  ("?PMSS3,S4", ["Input port must be 1-5"])]
        bad = [f"{cmd!r} -> {out}" for cmd, wants in checks for out in [w.run(cmd)] if not all(_has(out, x) for x in wants)]
        page = w.run("?MAP?")                                  # the MAP help page lists the legacy spellings
        if not _has(page, "  ?PMSx,dest    - PWM map") or _has(page, "?PMSS"):
            bad.append("the ?MAP? help page does not advertise the working ?PMSx,dest spelling (F2, tracker #82)")
        time.sleep(6.0)
        assert not any("Rebooting" in x for x in w.dev.since(m)), "a legacy PWM command rebooted the board"
        assert not bad, "; ".join(bad)


@test("pwm.invalid_remap_keeps_mapping", "(should) An invalid re-map of an existing PWM input leaves its outputs and passthrough intact (2-3 reboots)", needs=["wcb1"])
def invalid_remap_keeps_mapping(bench):
    """Probable firmware bug: addPWMMapping zeroes outputCount on the live slot (WCB_PWM.cpp:226-228) before
    validating, then returns 'No valid outputs specified' with active still true — passthrough stops, ?backup emits an
    unrestorable '?MAP,PWM,S3', and a reboot restores the old list from NVS."""
    s3, s4 = link(bench, 1, "S3"), link(bench, 1, "S4")
    w = usb_wcb(bench)
    _no_pwm(bench, 1)
    with config_guard(bench, 1):
        try:
            s3.pwm_out(0)
            m = w.send("?MAP,PWM,S3,S4")
            _pwm_reboot(w, m)
            out = w.run("?MAP,PWM,S3,X9")
            assert _has(out, "No valid outputs specified"), out
            problems = []
            lst = [x.rstrip() for x in w.run("?MAP,PWM,LIST")]
            if "Input: Serial3 -> Outputs: S4" not in lst:
                problems.append(f"LIST lost the S4 output: {lst}")
            if "?MAP,PWM,S3" in snapshot(bench, 1):
                problems.append("?backup now holds the unrestorable token '?MAP,PWM,S3'")
            s4.pwm_in()
            time.sleep(0.5)
            pm = s4.probe.dev.mark()
            for us in (1000, 2000):
                s3.pwm_out(us)
                time.sleep(1.0)
            if not s4.pulses(pm):
                problems.append("passthrough S3 -> S4 stopped")
            assert not problems, "; ".join(problems)
        finally:
            s3.pwm_out(0)
            s4.pwm_stop()
            w.reboot()
            _clear_local_mapping(w, "S3")
            s3.pwm_stop()


@test("pwm.passthrough_local", "Local passthrough S3 -> S4: widths follow the input, pulses only on change, 500-2500 filter (2 reboots)", needs=["wcb1"])
def passthrough_local(bench):
    s3, s4 = link(bench, 1, "S3"), link(bench, 1, "S4")
    w = usb_wcb(bench)
    _no_pwm(bench, 1)
    probe = s4.probe
    with config_guard(bench, 1):
        try:
            s3.pwm_out(0)
            m = w.send("?MAP,PWM,S3,S4")
            _pwm_reboot(w, m)
            assert any("PWM Task Created" in x for x in w.dev.since(m))
            s4.pwm_in()
            time.sleep(0.6)
            problems = []
            pm = probe.dev.mark()
            s3.pwm_out(1500)
            time.sleep(3.0)
            steady = sum(n for _, n in s4.pulses(pm))
            bench.note(f"steady 1500 for 3 s: {steady} output pulses")
            if not 1 <= steady < 75:
                problems.append(f"steady input produced {steady} output pulses (expected change-driven, 1..74)")
            for us in (1000, 1250, 1500, 1750, 2000):
                pm = probe.dev.mark()
                s3.pwm_out(us)
                time.sleep(1.0)
                if not any(abs(wd - us) <= 50 for wd, _ in s4.pulses(pm)):
                    problems.append(f"step {us}: {s4.pulses(pm)}")
            for us, passes in ((450, False), (2550, False), (600, True), (2400, True)):
                # Mark before the change: passthrough only pulses when the input changes (WCB_PWM.cpp:659-693),
                # so a width that passes has finished pulsing within ~0.3 s and a later mark sees nothing.
                pm = probe.dev.mark()
                s3.pwm_out(us)
                time.sleep(1.4)
                got = s4.pulses(pm)
                if passes and not any(abs(wd - us) <= 50 for wd, _ in got):
                    problems.append(f"{us} was not passed through: {got}")
                if not passes and got:
                    problems.append(f"{us} was not filtered: {got}")
            s3.pwm_out(0)
            time.sleep(0.4)
            pm = probe.dev.mark()
            time.sleep(1.0)
            if s4.pulses(pm):
                problems.append(f"pulses continued with the input held LOW: {s4.pulses(pm)}")
            assert not problems, "; ".join(problems)
        finally:
            s3.pwm_out(0)
            s4.pwm_stop()
            _clear_local_mapping(w, "S3")
            s3.pwm_stop()


@test("pwm.passthrough_mesh", "Mesh passthrough W1 S3 -> W2 S3: explicit remote config, widths, stats, WDP row (W1 x2, W2 x1 reboots)", needs=["wcb1"])
def passthrough_mesh(bench):
    s3 = link(bench, 1, "S3")
    w2s3 = link(bench, 2, "S3")
    w = usb_wcb(bench)
    _no_pwm(bench, 1, 2)
    require_tokens(bench, 2, "?BCAST,OUT,S3,ON", "?BCAST,IN,S3,ON")
    with config_guard(bench, 1, 2):
        try:
            s3.pwm_out(0)
            w.run("?DEBUG,ETM,ON")
            m = w.send("?MAP,PWM,S3,W2S3")
            seq = w.dev.expect(r"^\[ETM\] Sent seq (\d+): \?MAP,PWM,OUT,S3", timeout=3, since=m).group(1)
            w.dev.expect(rf"^\[ETM\] Seq {seq} fully acknowledged", timeout=3, since=m)
            dump = w.run("?WDP,DUMP", timeout=8)
            assert _has(dump, "[WDPPWM:N=1,DST=2,S=3]"), "W1's dump lacks the WDPPWM row"
            assert "?MAP,PWM,OUT,S3" in snapshot(bench, 2), "W2 config lacks the output declaration"
            _pwm_reboot(w, m)
            w.run("?STATS,RESET")
            w2s3.pwm_in()
            time.sleep(0.6)
            problems = []
            for us in (1000, 1500, 2000):
                pm = w2s3.probe.dev.mark()
                s3.pwm_out(us)
                time.sleep(1.0)
                if not any(abs(wd - us) <= 50 for wd, _ in w2s3.pulses(pm)):
                    problems.append(f"step {us} on W2S3: {w2s3.pulses(pm)}")
            stats = _stats_pwm(w)
            if not stats or stats[0] < 3 or stats[1] != stats[0] or stats[2] != 0:
                problems.append(f"PWM Passthrough stats {stats}")
            assert not problems, "; ".join(problems)
        finally:
            s3.pwm_out(0)
            w2s3.pwm_stop()
            m = w.send("?MAP,PWM,CLEAR,S3")
            try:
                _w2_reboot_wait(w, m)
            except AssertionError:
                _clear_remote_out(w, "S3")
            try:
                w.wait_boot(m, timeout=30)
            except AssertionError:
                pass
            s3.pwm_stop()
            w.run("?DEBUG,ETM,OFF")


@test("pwm.input_only_advertises_cap", "(should) A board whose PWM is input mappings only advertises the PWM capability (W1 x2, W2 x1 reboots)", needs=["wcb1"], links=["W1S3"])
def input_only_advertises_cap(bench):
    """Probable firmware bug: wdpCapFlags sets WDP_CAP_PWM on pwmOutputCount > 0 || activePWMCount > 0
    (WCB_WDP.cpp:112), and activePWMCount is declared (WCB_PWM.cpp:24) but never assigned."""
    s3 = link(bench, 1, "S3")
    w = usb_wcb(bench)
    _no_pwm(bench, 1, 2)
    with config_guard(bench, 1, 2):
        try:
            s3.pwm_out(0)
            m = w.send("?MAP,PWM,S3,W2S3")
            _pwm_reboot(w, m)
            w.run("?WDP,POLL")
            time.sleep(2.0)
            cap = _dump_cap(w, 1)
            assert cap is not None and cap & 0x0020, f"W1 (input mapping only) CAP={cap} lacks 0x0020"
        finally:
            s3.pwm_out(0)
            m = w.send("?MAP,PWM,CLEAR,S3")
            try:
                _w2_reboot_wait(w, m)
            except AssertionError:
                _clear_remote_out(w, "S3")
            try:
                w.wait_boot(m, timeout=30)
            except AssertionError:
                pass
            s3.pwm_stop()


@test("pwm.remote_refusal_logged_once", "(should) A remote output aimed at W2's WLED port is refused and the refusal is not re-logged on every advert (W1 + W2 reboots)", needs=["wcb1"])
def remote_refusal_logged_once(bench):
    """Comment/code disagreement: the WDP auto-config loop calls canUsePWMOnPort under a comment saying it skips
    SILENTLY (WCB_WDP.cpp:746-748), but canUsePWMOnPort prints the refusal (WCB_PWM.cpp:72-74), so W2 re-logs it for
    every W1 advert, forever."""
    w2s2 = link(bench, 2, "S2")
    s3 = link(bench, 1, "S3")
    w = usb_wcb(bench)
    _no_pwm(bench, 1, 2)
    require_tokens(bench, 2, "?WLED,1:W2S2:115200")
    t = marker()
    with config_guard(bench, 1, 2), Console(bench, 2) as c2:
        try:
            s3.pwm_out(0)
            cm = c2.mark()
            w.send("?MAP,PWM,S3,W2S2")
            time.sleep(3.0)
            w.run("?WDP,POLL")
            time.sleep(3.0)
            refusals = [x for x in c2.lines(cm) if "Cannot use PWM on Serial2 - reserved for HCR/MP3/WLED" in x]
            # (the message now reads ".../WLED/Maestro/DFP" - the prefix above still matches it)
            assert "?MAP,PWM,OUT,S2" not in snapshot(bench, 2), "W2 accepted a PWM output on its WLED port"
            watch = Watch(w2s2)
            w.send(f";W2,;S2,{t}")
            watch.expect(w2s2, t.encode() + b"\r", timeout=3)
            assert len(refusals) <= 1, f"the refusal was logged {len(refusals)} times in 6 s"
        finally:
            s3.pwm_out(0)
            m = w.send("?MAP,PWM,CLEAR,S3")
            try:
                _w2_reboot_wait(w, m)
            except AssertionError:
                pass
            try:
                w.wait_boot(m, timeout=30)
            except AssertionError:
                pass
            s3.pwm_stop()


@test("pwm.remote_unreachable_failed", "A PWM mapping to a non-peer board counts every passthrough send as Failed (2 reboots)", needs=["wcb1"])
def remote_unreachable_failed(bench):
    s3 = link(bench, 1, "S3")
    w = usb_wcb(bench)
    _no_pwm(bench, 1)
    seen = {int(n) for x in w.run("?WDP,DUMP", timeout=8) for n in re.findall(r"^\[WDP:N=(\d+),", x)}
    k = next((n for n in range(3, 19) if n not in seen), None)
    if k is None:
        raise Skip("no unused board number 3-18")
    with config_guard(bench, 1):
        try:
            s3.pwm_out(0)
            m = w.send(f"?MAP,PWM,S3,W{k}S3")
            w.dev.expect(r"^\[ETM\] Send failed seq \d+, error: (-?\d+)", timeout=3, since=m)
            _pwm_reboot(w, m)
            w.run("?STATS,RESET")
            for us in (1000, 1500, 2000):
                s3.pwm_out(us)
                time.sleep(1.0)
            stats = _stats_pwm(w)
            assert stats and stats[0] >= 3 and stats[1] == 0 and stats[2] == stats[0], f"PWM Passthrough stats {stats}"
        finally:
            s3.pwm_out(0)
            _clear_local_mapping(w, "S3")
            s3.pwm_stop()


@test("pwm.clear_all_reaches_remote", "(should) ?MAP,PWM,CLEAR,ALL clears a remote PWM output on an ETM mesh (W1 + W2 reboots)", needs=["wcb1"], links=["W1S3"])
def clear_all_reaches_remote(bench):
    """Probable firmware bug: clearAllPWMMappings sends ?PX<n> and ?REBOOT with WCB_PWM.cpp's two-argument prototype,
    whose useETM default is false (WCB_PWM.cpp:9), so every ETM-on receiver drops them (WCB.ino:4129-4151)."""
    s3 = link(bench, 1, "S3")
    w = usb_wcb(bench)
    _no_pwm(bench, 1, 2)
    require_tokens(bench, 2, "?BCAST,OUT,S3,ON", "?BCAST,IN,S3,ON")
    with config_guard(bench, 1, 2):
        try:
            s3.pwm_out(0)
            m = w.send("?MAP,PWM,S3,W2S3")
            _pwm_reboot(w, m)
            assert "?MAP,PWM,OUT,S3" in snapshot(bench, 2), "setup: W2 lacks the output declaration"
            # canSendESPNow() (WCB_PWM.cpp:51-53) silently skips every remote PWM send until W1 has been up 5 s, and
            # wait_boot returns ~4.4 s after the reset: sent now, CLEAR,ALL never reaches the mesh at all.
            time.sleep(3.0)
            m = w.send("?MAP,PWM,CLEAR,ALL")
            w.dev.expect(r"All PWM mappings cleared", timeout=3, since=m)
            _pwm_reboot(w, m)
            time.sleep(15)
            assert "?MAP,PWM,OUT,S3" not in snapshot(bench, 2), "W2 still declares S3 a PWM output after CLEAR,ALL"
        finally:
            s3.pwm_out(0)
            if "?MAP,PWM,OUT,S3" in snapshot(bench, 2):
                _clear_remote_out(w, "S3")
            if any(t.startswith("?MAP,PWM") for t in snapshot(bench, 1)):
                _clear_local_mapping(w, "S3")
            s3.pwm_stop()


@test("pwm.wdp_autoconfig_and_selfheal", "WDP PWMTARGET auto-configures a remote output W2 lacks, and CLEAR,ALL leaves W2 without it (explicit clear or self-heal; several reboots)", needs=["wcb1"], links=["W1S3"])
def wdp_autoconfig_and_selfheal(bench):
    s3 = link(bench, 1, "S3")
    w = usb_wcb(bench)
    _no_pwm(bench, 1, 2)
    require_tokens(bench, 2, "?BCAST,OUT,S3,ON", "?BCAST,IN,S3,ON")
    with config_guard(bench, 1, 2):
        try:
            s3.pwm_out(0)
            m = w.send("?MAP,PWM,S3,W2S3")
            _pwm_reboot(w, m)
            assert "?MAP,PWM,OUT,S3" in snapshot(bench, 2)
            # W1 still drives W2 S3, so any W1 advert that lands in W2's quiet window before its deferred reboot
            # (rule 11) re-auto-configures the output - by design. Let W1's boot burst (~4.2 s after reset) finish
            # first, and retry if a periodic advert still wins the race.
            time.sleep(5.0)
            for _ in range(3):
                _clear_remote_out(w, "S3")
                if "?MAP,PWM,OUT,S3" not in snapshot(bench, 2):
                    break
            assert "?MAP,PWM,OUT,S3" not in snapshot(bench, 2), "setup: W2 still declares S3"
            with Console(bench, 2) as c2:
                cm = c2.mark()
                assert _has(w.run("?WDP,POLL"), "[WDP] polled")
                c2.expect(r"\[WDP\] WCB1 drives our S3 .* auto-configuring PWM output", timeout=5, since=cm)
                time.sleep(2.0)
                assert "?MAP,PWM,OUT,S3" in snapshot(bench, 2), "WDP auto-config did not declare W2 S3"
                wm = w.dev.mark()
                cm = c2.mark()
                m = w.send("?MAP,PWM,CLEAR,ALL")
                _pwm_reboot(w, m)
                # CLEAR,ALL now tells W2 directly (?MAP,PWM,CLEAR,OUT + ?REBOOT under ETM - tracker, and
                # pwm.clear_all_reaches_remote), so W2 normally loses S3 by that route and reboots. WDP self-heal
                # is the backstop for a target that missed the message; the bench cannot make W2 miss it without
                # breaking the mesh, so either route passes and the log says which one ran.
                deadline = time.monotonic() + 40
                while time.monotonic() < deadline and "?MAP,PWM,OUT,S3" in snapshot(bench, 2):
                    time.sleep(3.0)
                healed = any("no longer drives our S3" in x for x in c2.lines(cm))
                bench.note("W2 S3 cleared by " + ("WDP self-heal" if healed else "the explicit remote clear"))
                if healed:
                    assert not any("WCB2 came ONLINE (boot)" in x for x in w.dev.since(wm)), "self-heal rebooted W2"
                assert "?MAP,PWM,OUT,S3" not in snapshot(bench, 2), "W2 S3 still declared after CLEAR,ALL"
        finally:
            s3.pwm_out(0)
            if "?MAP,PWM,OUT,S3" in snapshot(bench, 2):
                _clear_remote_out(w, "S3")
            if any(t.startswith("?MAP,PWM") for t in snapshot(bench, 1)):
                m = w.send("?MAP,PWM,CLEAR,ALL")
                _pwm_reboot(w, m)
            s3.pwm_stop()


@test("pwm.remove_clears_all_remote_outputs", "(should) Removing a mapping with two outputs on W2 clears both there (W1 + W2 reboots)", needs=["wcb1"], links=["W1S3"])
def remove_clears_all_remote_outputs(bench):
    """Probable firmware bug: removePWMMapping ETM-sends one CLEAR,OUT per remote output back to back (WCB_PWM.cpp:
    356-369); the receiver ACKs each, but the first restarts the board inline (WCB.ino:5045-5053), so the rest are
    ACKed and never run, and the leftover manual output never self-heals — rule 11 crossing boards."""
    s3 = link(bench, 1, "S3")
    w = usb_wcb(bench)
    _no_pwm(bench, 1, 2)
    require_tokens(bench, 2, "?BCAST,OUT,S3,ON", "?BCAST,IN,S3,ON", "?BCAST,OUT,S4,ON", "?BCAST,IN,S4,ON")
    with config_guard(bench, 1, 2):
        try:
            s3.pwm_out(0)
            m = w.send("?MAP,PWM,S3,W2S3,W2S4")
            _pwm_reboot(w, m)
            pulled = snapshot(bench, 2)
            assert "?MAP,PWM,OUT,S3" in pulled and "?MAP,PWM,OUT,S4" in pulled, "setup: W2 lacks the outputs"
            m = w.send("?MAP,PWM,CLEAR,S3")
            _w2_reboot_wait(w, m)
            try:
                w.wait_boot(m, timeout=30)
            except AssertionError:
                pass
            pulled = snapshot(bench, 2)
            leftover = [t for t in pulled if t.startswith("?MAP,PWM,OUT,")]
            assert not leftover, f"W2 still declares {leftover}"
        finally:
            s3.pwm_out(0)
            for p in ("S3", "S4"):
                if f"?MAP,PWM,OUT,{p}" in snapshot(bench, 2):
                    _clear_remote_out(w, p)
            if any(t.startswith("?MAP,PWM") for t in snapshot(bench, 1)):
                _clear_local_mapping(w, "S3")
            s3.pwm_stop()


@test("pwm.self_wcb_output", "(should) A PWM output addressed to this board's own W<n>S<p> produces pulses (2 reboots)", needs=["wcb1"])
def self_wcb_output(bench):
    """Probable firmware bug: W<own>S<p> is treated as local by passthrough (WCB_PWM.cpp:719) and skips the UART init,
    but only wcbNumber == 0 outputs get pinMode(OUTPUT) (WCB_PWM.cpp:279-291, 616-629), so the pin never drives."""
    s3, s4 = link(bench, 1, "S3"), link(bench, 1, "S4")
    w = usb_wcb(bench)
    _no_pwm(bench, 1)
    me = bench.usb_wcb_number()
    with config_guard(bench, 1):
        try:
            s3.pwm_out(0)
            m = w.send(f"?MAP,PWM,S3,W{me}S4")
            _pwm_reboot(w, m)
            s4.pwm_in()
            time.sleep(0.6)
            pm = s4.probe.dev.mark()
            for us in (1000, 2000, 1500):
                s3.pwm_out(us)
                time.sleep(1.0)
            assert s4.pulses(pm), f"no pulses on W{me}S4 for a W{me}S4 destination"
        finally:
            s3.pwm_out(0)
            s4.pwm_stop()
            _clear_local_mapping(w, "S3")
            s3.pwm_stop()
