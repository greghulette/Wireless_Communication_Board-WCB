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
from hil.wcb import WCB
from suites.common import (Console, Watch, absent_wcbs, config_guard, link, marker, probe_in_mesh, quiet_lines,
                           require_tokens, snapshot, token, usb_wcb)


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


def _step_problem(got, us, where=""):
    """None when a pulse within 50 us of `us` arrived AND the last one did: the last is the width a digital servo holds.
    The probe reports only the last pulse of each 200 ms window, so a right pulse followed by a stretched one shows as the
    stretched one; before tracker #94 made pulses hardware-timed, 1000 us came out as 1830 us (run 20260925-092255)."""
    if not any(abs(wd - us) <= 50 for wd, _ in got):
        return f"step {us}{where}: {got}"
    if abs(got[-1][0] - us) > 50:
        return f"step {us}{where}: held (last) pulse {got[-1][0]} us: {got}"
    return None


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
                problem = _step_problem(s4.pulses(pm), us)
                if problem:
                    problems.append(problem)
            prev = 2000     # the last step above
            for us, passes in ((450, False), (2550, False), (600, True), (2400, True)):
                # Mark before the change: passthrough only pulses when the input changes (processPWMPassthrough),
                # so a width that passes has finished pulsing within ~0.3 s and a later mark sees nothing.
                pm = probe.dev.mark()
                s3.pwm_out(us)
                time.sleep(1.4)
                got = s4.pulses(pm)
                if passes and _step_problem(got, us):
                    problems.append(f"{us} was not passed through right: {got}")
                # A filtered width must produce nothing of its own. A pulse at the PREVIOUS width is that step's late
                # pulse: input jitter of 6 us or more re-sends a steady input now and then (a third pulse in the steady
                # 3 s in 11 of 30 runs), and the probe's 200 ms report straddles the mark, so one sent just before the
                # change can be reported after it (run 20260927-131152: 450 saw two 1997 us pulses). A filter that failed
                # would put out the filtered width itself.
                if not passes and any(abs(wd - prev) > 50 for wd, _ in got):
                    problems.append(f"{us} was not filtered: {got}")
                prev = us
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
                problem = _step_problem(w2s3.pulses(pm), us, " on W2S3")
                if problem:
                    problems.append(problem)
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


@test("pwm.w2_output_to_w1_survives_boot", "A mapping saved on W2 to an output on W1 comes back from W2's boot aimed at W1, not at W2's own port: listing, chain and pulses; W2's S4 stays a serial port (re-scan #1; W2 x2, W1 x1 reboots)", needs=["wcb1", "wcb2"])
def w2_output_to_w1_survives_boot(bench):
    """WCB coverage re-scan #1 (docs/hil_plan/WCB.md WCB-WP15). The load path turns a saved output's W<n> into a
    local port when n is this board (initPWM, WCB_PWM.cpp), and setup() used to run it before loading WCB_Number,
    which still held its default of 1. So on every board but WCB1 a mapping to W1 came back from the next boot as a
    LOCAL port: pulses left the board's own pin, W1 got none, and the chain (built from RAM) reported the local form,
    so a Wizard pull and push saved the damage. W1 is WCB 1, which is why the W1-side tests never saw it; this one
    maps on W2. The boot after the mapping is the test."""
    w1, w2 = usb_wcb(bench), WCB(bench.dev("wcb2"))
    src, dst, own = link(bench, 2, "S3"), link(bench, 1, "S4"), link(bench, 2, "S4")
    _no_pwm(bench, 1, 2)
    require_tokens(bench, 1, "?BCAST,OUT,S4,ON", "?BCAST,IN,S4,ON")
    with config_guard(bench, 1, 2):
        mapped = False
        try:
            src.pwm_out(0)
            w2.run("?DEBUG,ETM,ON")
            m = w2.send("?MAP,PWM,S3,W1S4")
            mapped = True
            seq = w2.dev.expect(r"^\[ETM\] Sent seq (\d+): \?MAP,PWM,OUT,S4", timeout=3, since=m).group(1)
            w2.dev.expect(rf"^\[ETM\] Seq {seq} fully acknowledged", timeout=3, since=m)
            _pwm_reboot(w2, m)
            problems = []
            boot = [x.rstrip() for x in w2.dev.since(m)]
            if "Input: Serial3 -> Outputs: W1S4" not in boot:
                problems.append(f"W2's boot banner lists {[x for x in boot if x.startswith('Input: Serial')]}")
            listing = [x.rstrip() for x in w2.run("?MAP,PWM,LIST")]
            if "Input: Serial3 -> Outputs: W1S4" not in listing:
                problems.append(f"W2's ?MAP,PWM,LIST after its boot: {[x for x in listing if x.startswith('Input:')]}")
            chain = snapshot(bench, 2)
            if "?MAP,PWM,S3,W1S4" not in chain:
                problems.append(f"W2's chain: {[x for x in chain if x.upper().startswith('?MAP,PWM')]}")
            w2.run("?WDP,POLL")       # a rebooted board counts W1 offline until W1's next packet (F22)
            dst.pwm_in()
            own.pwm_in()
            time.sleep(1.0)
            for us in (1000, 1500, 2000):
                pd, po = dst.probe.dev.mark(), own.probe.dev.mark()
                src.pwm_out(us)
                time.sleep(1.0)
                problem = _step_problem(dst.pulses(pd), us, " on W1S4")
                if problem:
                    problems.append(problem)
                if own.pulses(po):
                    problems.append(f"step {us}: pulses on W2S4, W2's own port: {own.pulses(po)}")
            own.pwm_stop()
            text = marker()
            om = own.mark()
            w2.send(f";S4{text}")
            try:
                own.expect(text.encode() + b"\r", timeout=2, since=om)
            except AssertionError:
                problems.append(f"W2's S4 no longer transmits serial: got {own.received(om)!r}")
            assert not problems, "; ".join(problems)
        finally:
            src.pwm_out(0)
            dst.pwm_stop()
            own.pwm_stop()
            if mapped:
                m1 = w1.dev.mark()
                m2 = w2.send("?MAP,PWM,CLEAR,S3")
                for w, since, who in ((w2, m2, "W2"), (w1, m1, "W1")):
                    try:
                        _pwm_reboot(w, since)
                    except AssertionError:
                        bench.note(f"{who} did not reboot after W2's ?MAP,PWM,CLEAR,S3")
                if any(x.upper() == "?MAP,PWM,OUT,S4" for x in bench.config_tokens(1, refresh=True)):
                    _inline_clear_out(w1, "S4")   # an output the boot turned local sends W1 no clear
            src.pwm_stop()
            w2.run("?DEBUG,ETM,OFF")


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
    k = _unused_boards(bench, w, 1)[0]
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


@test("pwm.clear_all_many_remote_outputs", "?MAP,PWM,CLEAR,ALL with six outputs aimed at W2 and one at an absent board (WCB3 here): each W2 port's clear is sent once, the absent board's still names its own port, and W2 is left with no PWM outputs (re-scan #12; W1 x2, W2 x1 reboots)", needs=["wcb1", "wcb2"], links=["W1S3", "W1S4", "W1S5"])
def clear_all_many_remote_outputs(bench):
    """WCB coverage re-scan #12 (docs/hil_plan/WCB.md WCB-WP15 row 5). clearAllPWMMappings (WCB_PWM.cpp) listed each
    remote output in remotePorts[board][5] with no bound, one entry per mapping output. Two mappings with three W2
    outputs each put six entries in W2's row, and the sixth landed in WCB3's first slot (for WCB20, past the end of
    the array). WCB3's mapping is made first, so its slot is filled before the spill; it then printed the spilled W2
    port, S5, instead of its own S4. The list is a per-board set now, so each W2 port's clear goes out once. The third
    board must be one this bench does not have, so its sends fail as in pwm.remote_unreachable_failed: absent_wcbs'
    first number. That is 3 on a bench of W1 and W2, the row W2's spill lands in. With a real WCB3 it is 4, and a spill
    would land in the empty WCB3 row instead: no own port to overwrite, but still a repeated W2 clear or an extra
    'removal to WCB3' line, either of which the exact list of removal lines fails."""
    ins = [link(bench, 1, p) for p in ("S3", "S4", "S5")]
    w = usb_wcb(bench)
    _no_pwm(bench, 1, 2)
    require_tokens(bench, 2, *[f"?BCAST,{d},S{p},ON" for d in ("OUT", "IN") for p in (3, 4, 5)])
    gone = absent_wcbs(bench)[0]
    if gone in {int(n) for x in w.run("?WDP,DUMP", timeout=8) for n in re.findall(r"^\[WDP:N=(\d+),", x)}:
        raise Skip(f"WCB{gone} is on this mesh; the spill test needs it absent")
    problems = []
    with config_guard(bench, 1, 2):
        cleared = False
        try:
            for l in ins:
                l.pwm_out(0)
            m = w.send(f"?MAP,PWM,S3,W{gone}S4")
            w.send("?MAP,PWM,S4,W2S3,W2S4,W2S5")
            w.send("?MAP,PWM,S5,W2S3,W2S4,W2S5")
            _pwm_reboot(w, m)
            listing = [x.rstrip() for x in w.run("?MAP,PWM,LIST")]
            want = [f"Input: Serial3 -> Outputs: W{gone}S4", "Input: Serial4 -> Outputs: W2S3 W2S4 W2S5",
                    "Input: Serial5 -> Outputs: W2S3 W2S4 W2S5"]
            if [x for x in listing if x.startswith("Input:")] != want:
                raise AssertionError(f"setup: W1's mappings are {listing}")
            time.sleep(3.0)          # canSendESPNow: no remote PWM send in W1's first 5 s (pwm.clear_all_reaches_remote)
            w.run("?DEBUG,ON")
            m = w.send("?MAP,PWM,CLEAR,ALL")
            w.dev.expect(r"All PWM mappings cleared", timeout=10, since=m)
            cleared = True
            sent = sorted(x.rstrip() for x in w.dev.since(m) if x.startswith("Sent PWM output removal to WCB"))
            expect = sorted([f"Sent PWM output removal to WCB2: ?MAP,PWM,CLEAR,OUT,S{p}" for p in (3, 4, 5)] +
                            [f"Sent PWM output removal to WCB{gone}: ?MAP,PWM,CLEAR,OUT,S4"])
            if sent != expect:
                problems.append(f"removal lines {sent}, expected {expect}")
            _pwm_reboot(w, m)
            time.sleep(15)           # W2 restarts on the ?REBOOT that follows its clears
            left = [x for x in snapshot(bench, 2) if x.upper().startswith("?MAP,PWM")]
            if left:
                problems.append(f"W2 still has {left} after CLEAR,ALL")
        finally:
            for l in ins:
                l.pwm_out(0)
            if not cleared and any(x.upper().startswith("?MAP,PWM") for x in snapshot(bench, 1)):
                m = w.send("?MAP,PWM,CLEAR,ALL")
                try:
                    _pwm_reboot(w, m)
                except AssertionError:
                    pass
                time.sleep(15)
            for p in (3, 4, 5):
                if f"?MAP,PWM,OUT,S{p}" in snapshot(bench, 2):
                    _clear_remote_out(w, f"S{p}")
            w.run("?DEBUG,OFF")
            for l in ins:
                l.pwm_stop()
    assert not problems, "; ".join(problems)


@test("pwm.map_clear_all_without_pwm", "?MAP,CLEAR,ALL on a board with no PWM mapping clears the serial maps and does not reboot (re-scan #32)", needs=["wcb1"], links=[])
def map_clear_all_without_pwm(bench):
    """WCB coverage re-scan #32 (docs/hil_plan/WCB.md WCB-WP50 row 1). clearAllPWMMappings set pwmRebootPending whenever
    its autoReboot argument was true, so ?MAP,CLEAR,ALL - often run just to clear serial maps - rebooted the board with
    nothing mapped. It reboots now only when a PWM mapping or output existed. W1's serial maps are replayed after."""
    w = usb_wcb(bench)
    _no_pwm(bench, 1)
    with config_guard(bench, 1) as before:
        maps = [x for x in before[1] if x.upper().startswith("?MAP,SERIAL")]
        m = w.send("?MAP,CLEAR,ALL")
        try:
            w.dev.expect(r"^All serial and PWM mappings cleared", timeout=4, since=m)
            time.sleep(7.0)                    # the 4 s quiet window and more
            rebooted = w.rebooted_since(m) or any(x.startswith("Rebooting now") for x in w.dev.since(m))
        finally:
            if w.rebooted_since(m) or any(x.startswith("Rebooting now") for x in w.dev.since(m)):
                w.wait_boot(m, timeout=30)
            for x in maps:
                w.run(x)
    assert not rebooted, "W1 rebooted after ?MAP,CLEAR,ALL although it had no PWM mapping"


@test("pwm.serial_mapped_input_refused", "A port a serial mapping reads is refused for PWM when PWM is configured: ?MAP,PWM,OUT on it, a mapping with it as input, and a mapping with it as a local output; nothing is saved and nothing reboots (re-scan #15)", needs=["wcb1"], links=[])
def serial_mapped_input_refused(bench):
    """WCB coverage re-scan #15 (docs/hil_plan/WCB.md WCB-WP51 row 1). ?MAP,SERIAL refuses a PWM port as its input,
    and canUsePWMOnPort had no term the other way: PWM declared on a port a mapping reads was accepted, and a PWM
    output port skips its UART at boot, so the mapping then read nothing. The rule, decided (docs/HIL_WEEK_DECISIONS.md):
    PWM refuses a port a mapping reads as its INPUT, at configure time only (serialMapOwnsPort, WCB_PWM.cpp) - the boot
    loaders would erase a pair saved by older firmware. A mapping's destinations are checked by neither side."""
    w = usb_wcb(bench)
    _no_pwm(bench, 1)
    problems = []
    with config_guard(bench, 1) as before:
        if any(x.upper().startswith("?MAP,SERIAL,S5") for x in before[1]):
            raise Skip("W1 already maps S5")
        mapped = False
        try:
            w.run("?MAP,SERIAL,S5,R,S4")
            mapped = True
            refusal = "❌ Cannot use PWM on Serial5 - a serial mapping reads that port"
            for cmd, extra in (("?MAP,PWM,OUT,S5", None),
                               ("?MAP,PWM,S5,S3", "PWM mapping blocked - a serial mapping reads the input port"),
                               ("?MAP,PWM,S3,S5", "Skipping output Serial5 - a serial mapping reads it")):
                m = w.dev.mark()
                out = [x.rstrip() for x in w.run(cmd)]
                if extra is None and refusal not in out:
                    problems.append(f"{cmd} printed {out}")
                if extra is not None and not _has(out, extra):
                    problems.append(f"{cmd} printed {out}")
                time.sleep(5.0)                           # a mapping that took would ask for a restart after 4 s
                if any(x.startswith("Rebooting") for x in w.dev.since(m)):
                    problems.append(f"{cmd} rebooted W1")
                    w.wait_boot(m, timeout=30)
            left = [x for x in snapshot(bench, 1) if x.upper().startswith("?MAP,PWM")]
            if left:
                problems.append(f"PWM tokens saved: {left}")
        finally:
            if mapped:
                w.run("?MAP,SERIAL,CLEAR,S5")
    assert not problems, "; ".join(problems)


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


# ============================================================ coverage re-scan: WCB-WP15 rows 2-4, WCB-WP51
# docs/hil_plan/WCB.md. Each docstring names its row; where a row disagrees with the code, the test follows the code
# and says so.
def _in_order(lines, wanted):
    """The first of `wanted` not found, in order, as a whole (rstripped) line of `lines`; None when all are."""
    i = 0
    for x in lines:
        if i < len(wanted) and x.rstrip() == wanted[i]:
            i += 1
    return None if i == len(wanted) else wanted[i]


def _unused_boards(bench, w, n):
    """n board numbers 3-18 no board on this mesh uses: outputs nobody answers, as in pwm.remote_unreachable_failed.
    Neither in W1's WDP table nor one of the bench's own boards: W1 rebooted by the test before relearns W3 a few
    seconds after its boot, and an empty row then let W3 be picked, its sends ACKed (20261007-183330)."""
    seen = {int(k) for x in w.run("?WDP,DUMP", timeout=8) for k in re.findall(r"^\[WDP:N=(\d+),", x)}
    seen |= set(bench.wcb_numbers())
    free = [k for k in range(3, 19) if k not in seen]
    if len(free) < n:
        raise Skip(f"fewer than {n} unused board numbers 3-18")
    return free[:n]


def _no_rmt(lines):
    """'[PWM] S<n>: no RMT channel ...' (pwmPulseStart, WCB_PWM.cpp) or '[SOFTSERIAL] S<n>: no RMT channel ...'
    (WCB_SoftSerial.cpp): a port whose output fell back to bit-banging."""
    return [x for x in lines if "no RMT channel" in x]


def _owners(tokens, wcb, port):
    """The saved lines that give W<wcb> <port> to a device, a mapping or the Kyber. Matched by line prefix only: a
    sequence body or the alias can hold 'S3' too, and ?EPASS / ?WIFI must never reach a Skip message."""
    pats = (rf"^\?MAESTRO,M\d:W{wcb}{port}:", rf"^\?HCR,PORT,{port}\b", rf"^\?MP3,{port}\b", rf"^\?DFP,{port}\b",
            rf"^\?WLED,\d+:W{wcb}{port}\b", rf"^\?MAP,(PWM|SERIAL),(OUT,)?{port}\b",
            rf"^\?MAP,(PWM|SERIAL),S\d(,R)?(,[^,]+)*,(W{wcb})?{port}(,|$)", rf"^\?KYBER,LOCAL,{port}\b")
    return [t for t in tokens if any(re.match(p, t, re.I) for p in pats)]


def _require_free_ports(bench, wcb, *ports):
    """Skip unless no device, mapping or Kyber owns any of W<wcb>'s <ports>; the owning lines are named."""
    toks = bench.config_tokens(wcb, refresh=True)
    busy = [t for p in ports for t in _owners(toks, wcb, p)]
    if busy:
        raise Skip(f"W{wcb} {'/'.join(ports)} in use: {busy}")


def _line_time(dev, since, pattern):
    """The host time (time.monotonic()) the first line matching `pattern` after `since` arrived, or None."""
    rx = re.compile(pattern)
    for t, text in list(dev.lines[since:]):
        if rx.search(text):
            return t
    return None


def _usb_reader_up(w, timeout=5.0):
    """Send ?VERSION until the board's USB reader answers: it starts ~0.5 s after serialCommandTask (WCB.ino), and a
    fixed wait would spend the seconds pwm.remote_config_right_after_boot is about."""
    deadline = time.monotonic() + timeout
    while True:
        m = w.send("?VERSION")
        try:
            w.dev.expect(r"^Software Version: ", timeout=0.3, since=m)
            return
        except AssertionError:
            if time.monotonic() > deadline:
                raise AssertionError(f"W1's USB reader did not answer ?VERSION within {timeout:.0f} s of its boot")


def _nvs_counts(w):
    """{namespace: used entries} from ?NVS (printNvsUsage, WCB_Storage.cpp: '  <namespace> <count>' per line)."""
    out = {}
    for x in w.run("?NVS", timeout=8):
        m = re.match(r"^  (\S+)\s+(\d+)$", x.rstrip())
        if m:
            out[m.group(1)] = int(m.group(2))
    return out


_PWM_SEND = re.compile(r"^\[PWM\] Input S3: (\d+) \S+ -> (\d+) output\(s\)")


def _change_rule_problems(values):
    """Problems in a run of passthrough sends (the widths '[PWM] Input S3: <w> ...' printed, in order) against
    shouldTransmitPWM_Port (WCB_PWM.cpp): a send 6 us or more from the one before it is a change and may be followed by
    ONE smaller send (the settle send, which also needs a difference, so never an equal value); a second small send
    before the next change breaks the rule. The run's first sends are not judged until a change is seen: the send
    before the first one printed is unknown."""
    problems, settled = [], None
    for prev, v in zip(values, values[1:]):
        if abs(v - prev) >= 6:
            settled = False
        elif settled is None:
            continue
        elif v == prev:
            problems.append(f"a send equal to the one before it ({v} us)")
        elif settled:
            problems.append(f"a second small send {prev} -> {v} us since the last change")
        else:
            settled = True
    return problems


@test("pwm.valid_remap_updates_remote", "Re-mapping an input from W2S3 to W2S4 sends W2 a CLEAR,OUT for the dropped output, then an OUT for the new one; W2 restarts once holding OUT,S4 and not S3, with S3's broadcast flags as they were (W1 x2, W2 x2 reboots)", needs=["wcb1", "wcb2"], links=["W1S3"])
def valid_remap_updates_remote(bench):
    """WCB-WP15 row 2. addPWMMapping (WCB_PWM.cpp) snapshots the slot's remote outputs before it parses the new list,
    then sends ?MAP,PWM,CLEAR,OUT,S<p> (ETM) to each one the new list dropped and ?MAP,PWM,OUT,S<p> to every remote
    output it now holds; under ?DEBUG,ON each prints 'Sent PWM output clear/config to WCB<n>: <command>'. On W2 the
    clear arms the deferred restart (the ?MAP,PWM,CLEAR,OUT handler, WCB.ino) and the OUT that follows lands in the same
    quiet window, so W2 restarts once, with S4 declared and S3 released: removePWMOutputPort puts back the broadcast
    flags S3 had when it was declared. The first mapping and the re-map share W1's one deferred restart; W2 must hold
    OUT,S3 before the re-map goes out, or the clear would find nothing to remove."""
    s3 = link(bench, 1, "S3")
    w, w2 = usb_wcb(bench), WCB(bench.dev("wcb2"))
    _no_pwm(bench, 1, 2)
    require_tokens(bench, 2, *[f"?BCAST,{d},S{p},ON" for d in ("OUT", "IN") for p in (3, 4)])
    _require_free_ports(bench, 2, "S3", "S4")
    problems = []
    with config_guard(bench, 1, 2):
        mapped = False
        try:
            s3.pwm_out(0)
            m2 = w2.dev.mark()
            m = w.send("?MAP,PWM,S3,W2S3")
            mapped = True
            w.dev.expect(r"^PWM configuration stored", timeout=3, since=m)
            w2.dev.expect(r"^Serial3 configured as PWM output port", timeout=5, since=m2)
            w.run("?DEBUG,ON")
            m2 = w2.dev.mark()
            m = w.send("?MAP,PWM,S3,W2S4")
            w.dev.expect(r"^PWM configuration stored", timeout=3, since=m)
            try:
                w.dev.expect(r"^Sent PWM output config to WCB2: \?MAP,PWM,OUT,S4", timeout=3, since=m)
            except AssertionError:
                pass
            sent = [x.rstrip() for x in w.dev.since(m) if x.startswith("Sent PWM output ")]
            want = ["Sent PWM output clear to WCB2: ?MAP,PWM,CLEAR,OUT,S3", "Sent PWM output config to WCB2: ?MAP,PWM,OUT,S4"]
            if sent != want:
                problems.append(f"W1 printed {sent}, expected {want}")
            _pwm_reboot(w, m)
            w2.wait_boot(m2, timeout=30)
            lines2 = [x.rstrip() for x in w2.dev.since(m2)]
            miss = _in_order(lines2, ["Serial3 removed from PWM output ports; broadcasts re-enabled",
                                      "PWM output cleared - rebooting once the command queue is quiet...",
                                      "Serial4 configured as PWM output port", "Rebooting now to apply PWM configuration..."])
            if miss:
                problems.append(f"W2's console lacks (in order) {miss!r}")
            boots = sum(1 for x in lines2 if x.startswith("Booting up the "))
            if boots != 1:
                problems.append(f"W2 booted {boots} times after the re-map, expected once")
            toks2 = snapshot(bench, 2)
            if "?MAP,PWM,OUT,S4" not in toks2 or "?MAP,PWM,OUT,S3" in toks2:
                problems.append(f"W2's PWM tokens after its restart: {[t for t in toks2 if t.upper().startswith('?MAP,PWM')]}")
            flags = sorted(t for t in toks2 if re.match(r"^\?BCAST,(OUT|IN),S3,", t))
            if flags != ["?BCAST,IN,S3,ON", "?BCAST,OUT,S3,ON"]:
                problems.append(f"W2 S3's broadcast flags after the release: {flags}")
        finally:
            s3.pwm_out(0)
            if mapped:
                w.run("?WDP,POLL")            # a just-booted W1 counts W2 offline until W2's next packet (F22)
                time.sleep(1.0)
                m = w.send("?MAP,PWM,CLEAR,S3")
                try:
                    _w2_reboot_wait(w, m)
                except AssertionError:
                    pass
                try:
                    w.wait_boot(m, timeout=30)
                except AssertionError:
                    pass
                for p in ("S3", "S4"):
                    if f"?MAP,PWM,OUT,{p}" in snapshot(bench, 2):
                        _clear_remote_out(w, p)
            w.run("?DEBUG,OFF")
            s3.pwm_stop()
    assert not problems, "; ".join(problems)


@test("pwm.p_guard_device_ports", ";P is ignored, its pin untouched, on the ports an MP3 Trigger (S3), an HCR (S4) and a DFPlayer (S5) own, and each device's bytes still go out afterwards (W1, WDP off; no reboot)", needs=["wcb1"], links=["W1S3", "W1S4", "W1S5"])
def p_guard_device_ports(bench):
    """WCB-WP15 row 3, the MP3 / DFP / HCR terms of processPWMOutput's owned-port guard (WCB.ino): ;P on a port
    isSerialPortUsedForMP3 / ForDFP / ForHCR claims prints '[PWM] Ignoring ;P on S<n> - the port is in use by another
    device' under ?DEBUG,PWM,ON and never reaches pinMode. The WLED and Maestro terms have their own tests
    (pwm.p_guard_wled_port_remote, pwm.p_refused_on_maestro_port); the raw-mapping term is pwm.p_guard_raw_mapped_input
    and the Kyber term kyber.normal_mode_s1. The plan put the devices on W2 behind ;W2,;P; they sit on W1 here, with
    W1's WDP off so no board learns and persists a route to them, and the forwarded ;P path is the one
    pwm.p_guard_wled_port_remote covers. Hosting a device locally drops the board's remote route for it (hcrReservePort
    and the MP3/DFP twins set remoteWCB = 0), so each baseline route is re-sent, which also releases the local port
    (the REMOTE branch of configureHCR / configureMP3 / configureDFP); a kind with no baseline route is cleared."""
    from suites.s15_hcr_mp3_dfp import _dfp, _lf, _steps
    from suites.s22_maestro_kyber import _wdp_off
    ls = {p: link(bench, 1, p) for p in ("S3", "S4", "S5")}
    kinds = {"S3": "MP3", "S4": "HCR", "S5": "DFP"}
    w = usb_wcb(bench)
    _no_pwm(bench, 1)
    for p in ls:
        require_tokens(bench, 1, f"?BAUD,{p},9600", f"?BCAST,OUT,{p},ON", f"?BCAST,IN,{p},ON")
    _require_free_ports(bench, 1, *ls)
    hosted = [t for t in bench.config_tokens(1) if t.upper().startswith(("?HCR,", "?MP3,", "?DFP,"))
              and not re.match(r"^\?(HCR|MP3|DFP),REMOTE,W\d+$", t, re.I)]
    if hosted:
        raise Skip(f"W1 already hosts a device: {hosted}")
    configs = (("?MP3,S3:9600:V64", r"^\[MP3\] Configured: S3 at 9600 baud"),
               ("?HCR,PORT,S4:9600", r"^\[HCR\] Configured on S4 at 9600 baud"),
               ("?DFP,S5:9600:V0", r"^\[DFP\] Configured: S5 at 9600 baud"))
    device_bytes = {"S3": (";A,PLAY,5", bytes.fromhex("76407405")), "S4": (";H,OVERLOAD", _lf("<SE,QT>")),
                    "S5": (";D,PLAY,5", _dfp(0x03, 5))}
    problems = []
    with config_guard(bench, 1) as before:
        with _wdp_off(w, before[1]):
            try:
                if not _has(w.run("?HCR,POLL,OFF"), "[HCR] Poll interval = 0s (off)"):
                    raise AssertionError("?HCR,POLL,OFF did not confirm")   # before PORT: a poll would go out at once
                for cmd, want in configs:
                    out = w.run(cmd)
                    if not any(re.search(want, x) for x in out):
                        raise AssertionError(f"setup: {cmd} printed {out}")
                w.run("?DEBUG,PWM,ON")
                for l in ls.values():
                    l.pwm_in()
                time.sleep(0.6)
                for p, l in ls.items():
                    pm, wm = l.probe.dev.mark(), w.dev.mark()
                    w.send(f";P{p[1]}1500")
                    time.sleep(0.8)
                    if not any(re.search(rf"^\[PWM\] Ignoring ;P on {p} .* the port is in use by another device", x)
                               for x in w.dev.since(wm)):
                        problems.append(f";P{p[1]}1500 on the {kinds[p]} port printed no 'Ignoring ;P' line")
                    if l.pulses(pm):
                        problems.append(f";P{p[1]}1500 pulsed the {kinds[p]} port: {l.pulses(pm)}")
                for l in ls.values():
                    l.pwm_stop()
                for p, (cmd, want) in device_bytes.items():
                    problems += [f"after ;P on the {kinds[p]} port: {x}" for x in _steps(ls[p], w.send, [(cmd, want)])]
            finally:
                for l in ls.values():
                    l.pwm_stop()
                w.run("?DEBUG,PWM,OFF")
                for kind in ("DFP", "MP3", "HCR"):   # HCR last: its release leaves S4 at 9600, ON/ON, no label
                    route = token(before[1], f"?{kind},REMOTE,")
                    w.run(route if route else f"?{kind},CLEAR")
                for p in ls:
                    orig = token(before[1], f"?LABEL,{p},")
                    w.run(orig if orig else f"?LABEL,CLEAR,{p}")
    assert not problems, "; ".join(problems)


@test("pwm.p_guard_raw_mapped_input", ";P is ignored on a raw serial mapping's input port and bytes into it still reach the mapping's output raw; the output port itself takes ;P, as the configure-time rule leaves destinations alone (no reboot)", needs=["wcb1"], links=["W1S4", "W1S5"])
def p_guard_raw_mapped_input(bench):
    """WCB-WP15 row 3, the raw-mapping term: processPWMOutput refuses ;P on a port isSerialPortRawMapped names
    (WCB.ino) - RawSerialForwardingTask owns its bytes, and a pulse would take its TX pin. The guard reads the
    mapping's INPUT, as serialMapOwnsPort does at configure time (WCB_PWM.cpp; docs/HIL_WEEK_DECISIONS.md D23 checks no
    destination), so the destination S4 is an ordinary soft port and pulses. The pulse leaves S4 LOW, so a throwaway
    line follows it (HIL_TESTING.md §5)."""
    s4, s5 = link(bench, 1, "S4"), link(bench, 1, "S5")
    w = usb_wcb(bench)
    _no_pwm(bench, 1)
    require_tokens(bench, 1, "?BAUD,S4,9600", "?BAUD,S5,9600")
    _require_free_ports(bench, 1, "S4", "S5")
    problems = []
    with config_guard(bench, 1):
        mapped = False
        try:
            out = w.run("?MAP,SERIAL,S5,R,S4")
            if not _has(out, "Serial mapping set: Serial5 (RAW) -> 1 destination(s)"):
                raise AssertionError(f"?MAP,SERIAL,S5,R,S4 printed {out}")
            mapped = True
            w.run("?DEBUG,PWM,ON")
            s5.pwm_in()
            time.sleep(0.6)
            pm, wm = s5.probe.dev.mark(), w.dev.mark()
            w.send(";P51500")
            time.sleep(0.8)
            if not any(re.search(r"^\[PWM\] Ignoring ;P on S5 .* the port is in use by another device", x) for x in w.dev.since(wm)):
                problems.append(";P51500 on the raw-mapped input printed no 'Ignoring ;P' line")
            if s5.pulses(pm):
                problems.append(f";P51500 pulsed the raw-mapped input S5: {s5.pulses(pm)}")
            s5.pwm_stop()
            data = marker("RAW").encode() + b"\r"
            watch = Watch(s4)
            s5.send(data)
            try:
                watch.expect(s4, data, timeout=3)
            except AssertionError:
                problems.append(f"bytes into W1 S5 no longer reach S4 raw after the refused ;P: got {watch.got(s4)!r}")
            s4.pwm_in()
            time.sleep(0.6)
            pm = s4.probe.dev.mark()
            w.send(";P41500")
            time.sleep(0.8)
            got = s4.pulses(pm)
            if len(got) != 1 or abs(got[0][0] - 1500) > 40:
                problems.append(f";P41500 on the mapping's destination S4 gave {got}, not one 1500 us pulse")
        finally:
            s4.pwm_stop()
            s5.pwm_stop()
            w.run("?DEBUG,PWM,OFF")
            if mapped:
                w.run("?MAP,SERIAL,CLEAR,S5")
            w.send(";S4U")                       # S4 idles LOW after the pulse: one line puts it back HIGH
            time.sleep(0.5)
    assert not problems, "; ".join(problems)


@test("pwm.multi_output_and_multi_input", "One PWM input drives two local outputs to the same width, a second input drives W2's S3 on its own, a sixth output in one mapping is neither kept nor sent its remote config, and no port falls back to bit-banging (W1 x2, W2 x1 reboots)", needs=["wcb1", "wcb2"], links=["W1S2", "W1S3", "W1S4", "W1S5", "W2S3"])
def multi_output_and_multi_input(bench):
    """WCB-WP15 row 4. processPWMPassthrough (WCB_PWM.cpp) walks every active mapping and gives each output the measured
    width - a local one as one RMT pulse (pwmPulse), a remote one as a non-ETM ;P - with a stability tracker per INPUT
    port, so two inputs never mix. addPWMMapping's parse loop stops at five outputs (outputs[5], WCB_PWM.h) without a
    word: a sixth is neither listed nor sent ?MAP,PWM,OUT. Every local output port takes its own RMT channel at its
    first pulse (tracker #94); the classic ESP32 has 8, and here the two pulse ports need two beside the status LED's
    (a NeoPixel's; HW 1.0's LED is a plain GPIO) while S3-S5 hold none, since a PWM port never begins its soft UART -
    so nothing may print 'no RMT channel'. The plan's count ('the LED and three soft-TX ports leave 4') is the budget
    of a board whose S3-S5 all run serial. The cap arm aims its extra outputs at board numbers no board uses, as
    pwm.remote_unreachable_failed does, so W2 only ever has S3 declared; W2 S1 (the real Maestro) is never mapped.
    ?MAP,PWM,CLEAR,ALL ends it and restarts W2 (?REBOOT)."""
    ins = {"S2": link(bench, 1, "S2"), "S3": link(bench, 1, "S3")}
    s4, s5, w2s3 = link(bench, 1, "S4"), link(bench, 1, "S5"), link(bench, 2, "S3")
    w = usb_wcb(bench)
    _no_pwm(bench, 1, 2)
    _require_free_ports(bench, 1, "S2", "S3", "S4", "S5")
    _require_free_ports(bench, 2, "S3")
    require_tokens(bench, 2, "?BCAST,OUT,S3,ON", "?BCAST,IN,S3,ON")
    k1, k2, k3 = _unused_boards(bench, w, 3)
    problems = []
    with config_guard(bench, 1, 2):
        mapped = cleared = False
        try:
            for l in ins.values():
                l.pwm_out(0)
            w.run("?DEBUG,ETM,ON")
            m0 = w.dev.mark()
            w.send("?MAP,PWM,S3,S4,S5")
            m = w.send("?MAP,PWM,S2,W2S3")
            mapped = True
            seq = w.dev.expect(r"^\[ETM\] Sent seq (\d+): \?MAP,PWM,OUT,S3", timeout=3, since=m).group(1)
            w.dev.expect(rf"^\[ETM\] Seq {seq} fully acknowledged", timeout=3, since=m)
            _pwm_reboot(w, m)
            boot = [x.rstrip() for x in w.dev.since(m)]
            for want in ("Input: Serial3 -> Outputs: S4 S5", "Input: Serial2 -> Outputs: W2S3", "PWM Task Created") + \
                    tuple(f"Serial{p} reserved for PWM - skipping UART init" for p in (2, 3, 4, 5)):
                if want not in boot:
                    problems.append(f"W1's boot banner lacks {want!r}")
            s4.pwm_in()
            s5.pwm_in()
            w2s3.pwm_in()
            time.sleep(0.6)
            for us in (1000, 1500, 2000):              # one input, two local outputs
                p1, p2 = s4.probe.dev.mark(), w2s3.probe.dev.mark()
                ins["S3"].pwm_out(us)
                time.sleep(1.0)
                for l in (s4, s5):
                    problem = _step_problem(l.pulses(p1), us, f" on {l.key}")
                    if problem:
                        problems.append(problem)
                if w2s3.pulses(p2):
                    problems.append(f"W2S3 pulsed while its input (W1 S2) was held LOW: {w2s3.pulses(p2)}")
            for a, b in ((1200, 1800), (1800, 1200)):   # two inputs, each with its own outputs
                p1, p2 = s4.probe.dev.mark(), w2s3.probe.dev.mark()
                ins["S3"].pwm_out(a)
                ins["S2"].pwm_out(b)
                time.sleep(1.2)
                for l, us, pm in ((s4, a, p1), (s5, a, p1), (w2s3, b, p2)):
                    problem = _step_problem(l.pulses(pm), us, f" on {l.key} (S3 at {a}, S2 at {b})")
                    if problem:
                        problems.append(problem)
            fallbacks = _no_rmt(w.dev.since(m0))
            if fallbacks:
                problems.append(f"a PWM port fell back to bit-banged output: {fallbacks}")
            for l in ins.values():
                l.pwm_out(0)
            time.sleep(0.5)
            w.run("?DEBUG,ON")
            m = w.send(f"?MAP,PWM,S3,S4,S5,W2S3,W{k1}S3,W{k2}S3,W{k3}S4")
            w.dev.expect(r"^PWM configuration stored", timeout=5, since=m)
            listing = [x.rstrip() for x in w.run("?MAP,PWM,LIST") if x.startswith("Input: Serial3")]
            want = f"Input: Serial3 -> Outputs: S4 S5 W2S3 W{k1}S3 W{k2}S3"
            if listing != [want]:
                problems.append(f"after a seven-output ?MAP,PWM the listing is {listing}, expected [{want!r}]")
            sent = [x.rstrip() for x in w.dev.since(m) if x.startswith("Sent PWM output config to WCB")]
            if any(f"WCB{k3}:" in x for x in sent):
                problems.append(f"the dropped sixth output was sent its remote config: {sent}")
            for k in (k1, k2):
                if f"Sent PWM output config to WCB{k}: ?MAP,PWM,OUT,S3" not in sent:
                    problems.append(f"no remote config for the kept output W{k}S3: {sent}")
            m = w.send("?MAP,PWM,CLEAR,ALL")
            w.dev.expect(r"All PWM mappings cleared", timeout=10, since=m)
            cleared = True
            _pwm_reboot(w, m)
            time.sleep(15)                              # W2 restarts on the ?REBOOT that follows its clear
            left = [x for x in snapshot(bench, 2) if x.upper().startswith("?MAP,PWM")]
            if left:
                problems.append(f"W2 still has {left} after CLEAR,ALL")
        finally:
            for l in ins.values():
                l.pwm_out(0)
            for l in (s4, s5, w2s3):
                l.pwm_stop()
            if mapped and not cleared:
                m = w.send("?MAP,PWM,CLEAR,ALL")
                try:
                    _pwm_reboot(w, m)
                except AssertionError:
                    pass
                time.sleep(15)
            if "?MAP,PWM,OUT,S3" in snapshot(bench, 2):
                _clear_remote_out(w, "S3")
            w.run("?DEBUG,OFF")
            w.run("?DEBUG,ETM,OFF")
            for l in ins.values():
                l.pwm_stop()
    assert not problems, "; ".join(problems)


@test("pwm.declare_on_serial_map_destination", "A raw serial mapping's DESTINATION takes a PWM output declaration, since only a mapping's input is refused (D23): ?MAP,PWM,OUT,S4 is accepted beside ?MAP,SERIAL,S5,R,S4, S4 then pulses for ;P, and the mapping's bytes for it are dropped (1 reboot)", needs=["wcb1"], links=["W1S4", "W1S5"])
def declare_on_serial_map_destination(bench):
    """WCB-WP51 row 1, the open half of coverage re-scan #15, decided in docs/HIL_WEEK_DECISIONS.md D23: PWM is refused
    on a port a ?MAP,SERIAL reads (serialMapOwnsPort, WCB_PWM.cpp; pwm.serial_mapped_input_refused pins that), and a
    mapping's destinations are checked by neither side. So an output declared on a destination is accepted, and from
    then on WcbSoftSerial::write drops every byte for that port (a declared PWM output carries pulses,
    WCB_SoftSerial.cpp) - the mapping's included. The mapping stays set and delivers nothing there, with no warning at
    either command. That is the decided behaviour, pinned here as the firmware has it; a configure-time warning would be
    kinder. The declaration asks for no restart; its clear does (the ?MAP,PWM,CLEAR,OUT handler, WCB.ino)."""
    s4, s5 = link(bench, 1, "S4"), link(bench, 1, "S5")
    w = usb_wcb(bench)
    _no_pwm(bench, 1)
    require_tokens(bench, 1, "?BAUD,S4,9600", "?BAUD,S5,9600")
    _require_free_ports(bench, 1, "S4", "S5")
    problems = []
    with config_guard(bench, 1):
        mapped = declared = False
        try:
            out = w.run("?MAP,SERIAL,S5,R,S4")
            if not _has(out, "Serial mapping set: Serial5 (RAW) -> 1 destination(s)"):
                raise AssertionError(f"?MAP,SERIAL,S5,R,S4 printed {out}")
            mapped = True
            before_decl = marker("RAWA").encode() + b"\r"
            watch = Watch(s4)
            s5.send(before_decl)
            try:
                watch.expect(s4, before_decl, timeout=3)
            except AssertionError:
                raise AssertionError(f"control: bytes into W1 S5 never reached W1 S4 through the raw mapping: {watch.got(s4)!r}")
            out = [x.rstrip() for x in w.run("?MAP,PWM,OUT,S5")]
            if "❌ Cannot use PWM on Serial5 - a serial mapping reads that port" not in out:
                problems.append(f"?MAP,PWM,OUT,S5 on the mapping's input printed {out}")
            out = w.run("?MAP,PWM,OUT,S4")
            declared = _has(out, "Serial4 configured as PWM output port")
            if not declared:
                raise AssertionError(f"?MAP,PWM,OUT,S4 on the mapping's destination printed {out} (D23 accepts it)")
            toks = snapshot(bench, 1)
            for t in ("?MAP,SERIAL,S5,R,S4", "?MAP,PWM,OUT,S4"):
                if t not in toks:
                    problems.append(f"the chain lacks {t}")
            s4.listen()
            time.sleep(0.5)                  # S4 went LOW at the declaration: that break is not a byte of this check
            after_decl = marker("RAWB").encode() + b"\r"
            watch = Watch(s4)
            s5.send(after_decl)
            time.sleep(2.0)
            got = watch.got(s4)
            if got:
                problems.append(f"the raw mapping still wrote {got!r} onto the declared PWM output S4")
            bench.note("a raw mapping's bytes for a destination declared a PWM output are dropped silently (D23)")
            s4.pwm_in()
            time.sleep(0.6)
            pm = s4.probe.dev.mark()
            w.send(";P41500")
            time.sleep(0.8)
            pulses = s4.pulses(pm)
            if len(pulses) != 1 or abs(pulses[0][0] - 1500) > 40:
                problems.append(f";P41500 on the declared destination gave {pulses}, not one 1500 us pulse")
        finally:
            s4.pwm_stop()
            if mapped:
                w.run("?MAP,SERIAL,CLEAR,S5")
            if declared:
                _inline_clear_out(w, "S4")
    assert not problems, "; ".join(problems)


@test("pwm.input_on_hw_uart_pin", "PWM capture on S2's hardware-UART RX pin drives a local output, a PWM output on S2's TX pin follows its own input, and an input held LOW stops the pulses (2 reboots)", needs=["wcb1"], links=["W1S2", "W1S3", "W1S4"])
def input_on_hw_uart_pin(bench):
    """WCB-WP51 row 2. attachPWMInterrupt (WCB_PWM.cpp) arms S2's RX pin like any input (level-emulated on the classic
    ESP32, pwmEdge), and setup()'s Maestro_Remote branch skips Serial2.begin when a PWM input or output owns S2
    (WCB.ino), so the pin is the ISR's alone; an output on S2 is pulsed through pwmPulse like a soft port's. S2 is one
    mapping's input and another's output at once - different pins - and both share one deferred restart. The other PWM
    tests use the soft ports S3-S5 only."""
    s2, s3, s4 = link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 1, "S4")
    w = usb_wcb(bench)
    _no_pwm(bench, 1)
    _require_free_ports(bench, 1, "S2", "S3", "S4")
    problems = []
    with config_guard(bench, 1):
        mapped = False
        try:
            s2.pwm_out(0)
            s3.pwm_out(0)
            m = w.send("?MAP,PWM,S2,S4")
            w.send("?MAP,PWM,S3,S2")
            mapped = True
            _pwm_reboot(w, m)
            boot = [x.rstrip() for x in w.dev.since(m)]
            for want in ("Serial2 reserved for PWM - skipping UART init", "Input: Serial2 -> Outputs: S4",
                         "Input: Serial3 -> Outputs: S2", "Maestro_Remote Task Created"):
                if want not in boot:
                    problems.append(f"W1's boot banner lacks {want!r}")
            s4.pwm_in()
            s2.pwm_in()                      # S2's TX line; the probe keeps driving S2's RX with pwm_out
            time.sleep(0.6)
            for us in (1000, 1500, 2000):
                pm = s4.probe.dev.mark()
                s2.pwm_out(us)
                time.sleep(1.0)
                problem = _step_problem(s4.pulses(pm), us, " on W1S4, from S2's RX pin")
                if problem:
                    problems.append(problem)
            s2.pwm_out(0)
            time.sleep(0.4)
            pm = s4.probe.dev.mark()
            time.sleep(1.0)
            if s4.pulses(pm):
                problems.append(f"S4 kept pulsing with S2's input held LOW: {s4.pulses(pm)}")
            for us in (1000, 1500, 2000):
                pm = s2.probe.dev.mark()
                s3.pwm_out(us)
                time.sleep(1.0)
                problem = _step_problem(s2.pulses(pm), us, " on W1S2's TX pin, from S3")
                if problem:
                    problems.append(problem)
        finally:
            s2.pwm_out(0)
            s3.pwm_out(0)
            s4.pwm_stop()
            if mapped:
                m = w.send("?MAP,PWM,CLEAR,ALL")
                try:
                    _pwm_reboot(w, m)
                except AssertionError:
                    pass
            s2.pwm_stop()
            s3.pwm_stop()
    assert not problems, "; ".join(problems)


@test("pwm.pulse_accuracy_under_mesh_load", "Local passthrough S3 -> S4 under mesh load - a mesh client broadcasting JSON at ~50 Hz and W1 sending ETM unicasts - puts every output width within 20 us of its input step (~40 s; 2 reboots)", needs=["wcb1", "probe2"], links=["W1S3", "W1S4"])
def pulse_accuracy_under_mesh_load(bench):
    """WCB-WP51 row 3, now a regression check: since tracker #94 (docs/HIL_WEEK_DECISIONS.md D5) a passthrough pulse is
    one RMT symbol (pwmPulse, WCB_PWM.cpp), so nothing the CPUs do stretches it, and the error left is input capture and
    the probe's own measurement. Before it, the bit-banged pulse came out as 1830 us for 1000 us under load (run
    20260925-092255). The load is input.softserial_tx_integrity's: probe2 joins as a client and broadcasts unensured
    JSON, which every WCB consumes without running (the id is s19's JSON id: unensured sends never enter a duplicate
    ring), plus a ;W2 unicast from W1 every fourth step. The probe reports only the last pulse of each 200 ms window, so
    the input alternates between 1200 and 1800 us and each report is held to the nearer of the two; that gives about
    150 widths for the plan's 500, in 40 s instead of two minutes."""
    steps = 150
    s3, s4 = link(bench, 1, "S3"), link(bench, 1, "S4")
    w = usb_wcb(bench)
    _no_pwm(bench, 1)
    _require_free_ports(bench, 1, "S3", "S4")
    widths = []
    with config_guard(bench, 1):
        mapped = False
        try:
            s3.pwm_out(0)
            m = w.send("?MAP,PWM,S3,S4")
            mapped = True
            _pwm_reboot(w, m)
            s4.pwm_in()
            time.sleep(0.6)
            with probe_in_mesh(bench, "probe2", 12) as probe:
                pm = s4.probe.dev.mark()
                for i in range(steps):
                    s3.pwm_out(1200 if i % 2 == 0 else 1800)
                    if i % 4 == 0:
                        w.send(f";W2,;S0,{marker('LD')}")
                    end = time.monotonic() + 0.25
                    while time.monotonic() < end:
                        probe.mesh_broadcast('{"hil":1}', ensured=False)
                        time.sleep(0.02)
                time.sleep(0.6)
                widths = [wd for wd, _ in s4.pulses(pm)]
        finally:
            s3.pwm_out(0)
            s4.pwm_stop()
            if mapped:
                _clear_local_mapping(w, "S3")
            s3.pwm_stop()
    errors = sorted(min(abs(wd - 1200), abs(wd - 1800)) for wd in widths)
    bench.note(f"{len(widths)} S4 widths under mesh load; error median {errors[len(errors) // 2] if errors else '-'} us, "
               f"max {errors[-1] if errors else '-'} us")
    assert len(widths) >= steps // 2, f"only {len(widths)} output widths reported for {steps} input steps"
    bad = [wd for wd in widths if min(abs(wd - 1200), abs(wd - 1800)) > 20]
    assert not bad, f"{len(bad)} of {len(widths)} widths more than 20 us off their step: {bad[:10]}"


@test("pwm.boot_conflict_cleanup", "A PWM output and a mapping input saved on S1 while W1 had no Kyber mode meet Maestro_Remote at the next boot: the output is skipped and dropped from NVS, the input is refused but stays in NVS, and neither reaches the chain (WDP off; 1 reboot)", needs=["wcb1"], links=[])
def boot_conflict_cleanup(bench):
    """WCB-WP51 row 4. setup() loads the Kyber mode before initPWM (WCB.ino), so both PWM loaders see S1 reserved on a
    Maestro_Remote board (kyberModeReservesPort, WCB_Storage.cpp): loadPWMOutputPortsFromPreferences (WCB_PWM.cpp) skips
    the saved output with 'Skipping PWM output port Serial1 - conflicts with Kyber' and rewrites pwm_outputs ('Cleaning
    up conflicting PWM output ports from NVS...'), while loadPWMMappingsFromPreferences refuses the saved input (its
    canUsePWMOnPort call prints the refusal) and never erases it - docs/hil_plan/WCB.md §3 lists that as untidy but
    harmless; the ?NVS count shows it. ?MAESTRO,REMOTE has no PWM guard, which is how the pair gets saved: after
    ?KYBER,CLEAR and with Maestro 1 cleared off S1, S1 takes both, then ?MAESTRO,REMOTE and the mapping's own deferred
    restart. The plan expected a lower ?NVS pwm_outputs count; savePWMOutputPortsToPreferences rewrites 'count' and
    never removes the old port<i>/auto<i>/pbo<i>/pbi<i> keys, so that count is only noted. ?MAP,PWM,CLEAR,ALL then
    clears both namespaces with no restart (nothing was loaded), and the rebuild puts Maestro 1 back on S1."""
    from suites.s22_maestro_kyber import (_m_lines, _port_tokens, _rebuild, _require_kyber_broadcast_remote,
                                          _require_remote_pair, _require_replayable, _wdp_off)
    w = usb_wcb(bench)
    _no_pwm(bench, 1)
    _require_remote_pair(bench)
    _require_kyber_broadcast_remote(w)
    _require_free_ports(bench, 1, "S4")
    refusal = "❌ Cannot use PWM on Serial1 - reserved for Maestro/Kyber"
    problems = []
    with config_guard(bench, 1) as before:
        lines = _m_lines(before[1])
        _require_replayable(lines)
        shared = [t for t in lines if re.match(r"^\?MAESTRO,M[2-9]:W1S1:", t, re.I)]
        if shared:
            raise Skip(f"W1 S1 carries more Maestros than M1: {shared}")
        ports = _port_tokens(before[1], "S1")
        remote_booted = False
        with _wdp_off(w, before[1]):
            try:
                if not _has(w.run("?KYBER,CLEAR", timeout=6), "Kyber is Not used"):
                    raise AssertionError("?KYBER,CLEAR did not print 'Kyber is Not used'")
                out = w.run("?MAESTRO,CLEAR,M1:W1S1")
                if not _has(out, "Cleared Maestro M1:W1S1"):
                    raise AssertionError(f"?MAESTRO,CLEAR,M1:W1S1 printed {out}")
                out = w.run("?MAP,PWM,OUT,S1")
                if not _has(out, "Serial1 configured as PWM output port"):
                    raise AssertionError(f"?MAP,PWM,OUT,S1 with no Kyber mode and no Maestro there printed {out}")
                m = w.send("?MAP,PWM,S1,S4")
                w.dev.expect(r"^PWM configuration stored", timeout=3, since=m)
                if not _has(w.run("?MAESTRO,REMOTE", timeout=6), "Kyber is REMOTE (on another WCB)"):
                    raise AssertionError("?MAESTRO,REMOTE did not print 'Kyber is REMOTE (on another WCB)'")
                _pwm_reboot(w, m)
                boot = [x.rstrip() for x in w.dev.since(m)]
                remote_booted = "Maestro_Remote Task Created" in boot
                if not remote_booted:
                    raise AssertionError("W1 did not boot with the Maestro_Remote task")
                for want in ("⚠️  Skipping PWM output port Serial1 - conflicts with Kyber",
                             "Cleaning up conflicting PWM output ports from NVS...", "No input mappings configured"):
                    if want not in boot:
                        problems.append(f"the boot banner lacks {want!r}")
                n = sum(1 for x in boot if x == refusal)
                if n != 2:
                    problems.append(f"the boot printed {refusal!r} {n} times, expected twice (the input and the output)")
                for bad in ("Input: Serial1 -> Outputs:", "PWM Task Created", "Serial1 reserved for PWM"):
                    if _has(boot, bad):
                        problems.append(f"the boot still loaded the conflicting PWM: {bad!r}")
                lst = [x.rstrip() for x in w.run("?MAP,PWM,LIST")]
                if "No input mappings configured" not in lst or _has(lst, "Configured outputs:"):
                    problems.append(f"?MAP,PWM,LIST after the boot: {lst}")
                left = [t for t in snapshot(bench, 1) if t.upper().startswith("?MAP,PWM")]
                if left:
                    problems.append(f"the chain still carries {left}")
                nvs = _nvs_counts(w)
                bench.note(f"after the boot cleanup ?NVS reports pwm_mappings={nvs.get('pwm_mappings', 0)}, "
                           f"pwm_outputs={nvs.get('pwm_outputs', 0)} (stale output keys are never removed)")
                if nvs.get("pwm_mappings", 0) < 1:
                    problems.append("the refused mapping input is gone from NVS (docs/hil_plan/WCB.md §3 says it is kept)")
                mc = w.dev.mark()
                out = [x.rstrip() for x in w.run("?MAP,PWM,CLEAR,ALL")]
                if "All PWM mappings cleared" not in out or _has(out, "Rebooting once"):
                    problems.append(f"?MAP,PWM,CLEAR,ALL with nothing loaded printed {out}")
                time.sleep(6.0)
                if w.rebooted_since(mc):
                    problems.append("?MAP,PWM,CLEAR,ALL restarted W1 with nothing loaded")
                    w.wait_boot(mc, timeout=30)
                nvs = _nvs_counts(w)
                if nvs.get("pwm_mappings", 0) or nvs.get("pwm_outputs", 0):
                    problems.append(f"?MAP,PWM,CLEAR,ALL left NVS entries: {nvs.get('pwm_mappings', 0)} mapping(s), "
                                    f"{nvs.get('pwm_outputs', 0)} output key(s)")
            finally:
                try:
                    mc = w.dev.mark()
                    out = w.run("?MAP,PWM,CLEAR,ALL")        # idempotent; restarts only if something is loaded
                    if _has(out, "Rebooting once"):
                        _pwm_reboot(w, mc)
                        remote_booted = remote_booted or _has(w.dev.since(mc), "Maestro_Remote Task Created")
                except AssertionError as e:
                    problems.append(f"restore: {e}")
                w.run("?MAESTRO,REMOTE", timeout=6)
                problems += _rebuild(w, lines)
                for t in ports:
                    w.run(t)
                if not remote_booted:
                    bm = w.reboot()
                    if not _has(w.dev.since(bm), "Maestro_Remote Task Created"):
                        problems.append("W1 did not boot with the Maestro_Remote task after the restore")
    assert not problems, "; ".join(problems)


@test("pwm.passthrough_debug_and_settle", "?DEBUG,PWM,ON traces every passthrough send - input width and output count, the local pin, the remote ;P - one trace per pulse on the wire, and the sends keep the change rule: after a change of 6 us or more, at most one smaller settle send (2 reboots)", needs=["wcb1"], links=["W1S3", "W1S4"])
def passthrough_debug_and_settle(bench):
    """WCB-WP51 row 5. processPWMPassthrough (WCB_PWM.cpp) prints '[PWM] Input S<n>: <w> μs -> <k> output(s)' for each
    send, then '[PWM] Local output -> S<p> pin <pin>: <w> μs' or '[PWM] Remote output -> WCB<n> S<p>: ;P<p><w>' per
    output. shouldTransmitPWM_Port sends a reading 6 us or more from the last one sent, plus ONE settle send once five
    readings agree within 6 us and differ from the last sent at all. The plan's '1500 to 1503 and hold gives exactly
    one more pulse' holds only while no settle send has gone out since the last big change - capture jitter decides
    that - so the test holds every send to the rule and notes what the small steps did. The remote output is a board
    number no board uses (pwm.remote_unreachable_failed), so W2 is untouched and each remote send also prints
    'ESP-NOW send failed'."""
    s3, s4 = link(bench, 1, "S3"), link(bench, 1, "S4")
    w = usb_wcb(bench)
    _no_pwm(bench, 1)
    _require_free_ports(bench, 1, "S3", "S4")
    k = _unused_boards(bench, w, 1)[0]
    problems = []
    with config_guard(bench, 1):
        mapped = False
        try:
            s3.pwm_out(0)
            m = w.send(f"?MAP,PWM,S3,S4,W{k}S3")
            mapped = True
            _pwm_reboot(w, m)
            pin = next((g.group(1) for g in (re.match(r"^  Serial4: Reserved for PWM Output  Pins: Tx:(\d+)", x)
                                             for x in w.dev.since(m)) if g), None)
            if pin is None:
                raise AssertionError("W1's boot banner lacks the S4 PWM output row")
            w.run("?DEBUG,PWM,ON")
            s4.pwm_in()
            s3.pwm_out(1500)
            time.sleep(2.0)
            wm, pm = w.dev.mark(), s4.probe.dev.mark()
            s3.pwm_out(2000)
            time.sleep(1.5)
            lines = [x.rstrip() for x in w.dev.since(wm)]
            sends = [(int(g.group(1)), int(g.group(2))) for g in (_PWM_SEND.match(x) for x in lines) if g]
            first = next((s for s in sends if abs(s[0] - 2000) <= 20), None)
            if first is None or first[1] != 2:
                problems.append(f"the step to 2000 us traced {sends}, expected a send near 2000 to 2 outputs")
            else:
                v = first[0]
                if not any(re.match(rf"^\[PWM\] Local output -> S4 pin {pin}: {v} ", x) for x in lines):
                    problems.append(f"no '[PWM] Local output -> S4 pin {pin}: {v} ...' line")
                if not any(x.startswith(f"[PWM] Remote output -> WCB{k} S3: ;P3{v}") for x in lines):
                    problems.append(f"no '[PWM] Remote output -> WCB{k} S3: ;P3{v}' line")
            small = {}
            for us in (2003, 1990, 1993, 1500):          # +3 after a settled input, a big change, +3 right after it
                wm2 = w.dev.mark()
                s3.pwm_out(us)
                time.sleep(1.5)
                small[us] = [g.group(1) for g in (_PWM_SEND.match(x) for x in w.dev.since(wm2)) if g]
            bench.note("sends after each step (us): " + "; ".join(f"{us}: {v}" for us, v in small.items()))
            values = [int(g.group(1)) for g in (_PWM_SEND.match(x) for x in w.dev.since(wm)) if g]
            # Seeded with the settled 1500: the last value sent before the mark, to within the jitter that matters here.
            problems += [f"change rule: {p}" for p in _change_rule_problems([1500] + values)]
            local = sum(1 for x in w.dev.since(wm) if x.startswith("[PWM] Local output -> S4 "))
            pulses = sum(n for _, n in s4.pulses(pm))
            if abs(pulses - local) > 1:
                problems.append(f"{local} local sends traced but {pulses} pulses on W1 S4")
        finally:
            s3.pwm_out(0)
            s4.pwm_stop()
            w.run("?DEBUG,PWM,OFF")
            if mapped:
                _clear_local_mapping(w, "S3")
            s3.pwm_stop()
    assert not problems, "; ".join(problems)


@test("pwm.remote_config_right_after_boot", "A PWM mapping sent within 5 s of W1's boot still configures its remote output: ?MAP,PWM,OUT,S3 goes to W2, is acknowledged, and W2's chain gains it (W1 x3, W2 x1 reboots)", needs=["wcb1", "wcb2"], links=["W1S3"])
def remote_config_right_after_boot(bench):
    """WCB-WP51 row 6. canSendESPNow (WCB_PWM.cpp) gates remote PWM configuration on espNowInitialized, set right after
    esp_now_init() in setup(). It used to be 'uptime > 5 s', which bit only commands arriving in the first five seconds
    after a boot - a config push, a test - and changed, saved and reported the local mapping while the remote board was
    never told (HIL_TESTING.md §6, constraints). The mapping goes out as soon as W1's USB reader answers after a
    ?reboot, and its uptime is taken from the 'Rebooting now' line; one sent after 5 s would not show the old gate is
    gone, so that fails too. A just-booted board counts W2 offline until W2's next packet and does not retry the send
    (HIL_TEST_AUDIT.md F22); W2's ACK still resolves it."""
    s3 = link(bench, 1, "S3")
    w = usb_wcb(bench)
    _no_pwm(bench, 1, 2)
    require_tokens(bench, 2, "?BCAST,OUT,S3,ON", "?BCAST,IN,S3,ON")
    _require_free_ports(bench, 2, "S3")
    problems = []
    with config_guard(bench, 1, 2):
        mapped = False
        try:
            m = w.send("?reboot")
            w.dev.expect(r"^Reboot queued", timeout=3, since=m)
            w.dev.expect(r"^Rebooting now", timeout=WCB.REBOOT_DEFER_S, since=m)
            t_reset = _line_time(w.dev, m, r"^Rebooting now")
            w.dev.expect(r"^Raw Serial Forwarding Task Created", timeout=20, since=m)
            _usb_reader_up(w)
            s3.pwm_out(0)                                   # the new input must not float (module rules)
            w.send("?DEBUG,ETM,ON")
            t_send = time.monotonic()
            m = w.send("?MAP,PWM,S3,W2S3")
            mapped = True
            uptime = t_send - t_reset
            bench.note(f"?MAP,PWM,S3,W2S3 sent {uptime:.1f} s after W1's restart")
            seq = w.dev.expect(r"^\[ETM\] Sent seq (\d+): \?MAP,PWM,OUT,S3", timeout=3, since=m).group(1)
            w.dev.expect(rf"^\[ETM\] Seq {seq} fully acknowledged", timeout=5, since=m)
            if _has(w.dev.since(m), "remote PWM configuration was NOT sent"):
                problems.append("W1 still skipped the remote send")
            _pwm_reboot(w, m)
            if "?MAP,PWM,OUT,S3" not in snapshot(bench, 2):
                problems.append("W2's chain lacks ?MAP,PWM,OUT,S3")
            if uptime >= 5.0:
                problems.append(f"the mapping went out {uptime:.1f} s after W1's restart - too late to show the old 5 s gate is gone")
        finally:
            s3.pwm_out(0)
            if mapped:
                w.run("?WDP,POLL")                        # W1 counts W2 offline until W2's next packet (F22)
                time.sleep(1.0)
                m = w.send("?MAP,PWM,CLEAR,S3")
                try:
                    _w2_reboot_wait(w, m)
                except AssertionError:
                    pass
                try:
                    w.wait_boot(m, timeout=30)
                except AssertionError:
                    pass
                if "?MAP,PWM,OUT,S3" in snapshot(bench, 2):
                    _clear_remote_out(w, "S3")
            w.run("?DEBUG,ETM,OFF")
            s3.pwm_stop()
    assert not problems, "; ".join(problems)


@test("pwm.baud_on_pwm_ports_skipped", "?BAUD on a PWM input (S3) or output (S4) port stores the rate but re-begins no soft UART there: no soft-serial begin is logged (S5 is, as the control), passthrough keeps following, and S4 still idles LOW (2 reboots)", needs=["wcb1"], links=["W1S3", "W1S4"])
def baud_on_pwm_ports_skipped(bench):
    """WCB-WP51 row 7. updateBaudRate (WCB_Storage.cpp) always calls applyLiveBaud, which ends and re-begins Serial3-5
    only when the port is neither a PWM input nor a PWM output (WCB.ino): a re-begin would put the soft UART's RX
    interrupt on the input's pin and its TX on the output's. Under ?DEBUG,ON every soft-port begin logs
    '[SOFTSERIAL] S<n> RX: ...' (applySoftSerialIntTx), so a skipped one logs nothing; ?BAUD,S5 at its own rate is the
    control that shows the line would print. The rates go back before the mapping is cleared."""
    s3, s4 = link(bench, 1, "S3"), link(bench, 1, "S4")
    w = usb_wcb(bench)
    _no_pwm(bench, 1)
    require_tokens(bench, 1, "?BAUD,S3,9600", "?BAUD,S4,9600", "?BAUD,S5,9600")
    _require_free_ports(bench, 1, "S3", "S4", "S5")
    problems = []
    with config_guard(bench, 1):
        mapped = changed = False
        try:
            s3.pwm_out(0)
            m = w.send("?MAP,PWM,S3,S4")
            mapped = True
            _pwm_reboot(w, m)
            s4.pwm_in()
            time.sleep(0.6)
            for us in (1000, 2000):
                pm = s4.probe.dev.mark()
                s3.pwm_out(us)
                time.sleep(1.0)
                problem = _step_problem(s4.pulses(pm), us, " (before ?BAUD)")
                if problem:
                    problems.append(problem)
            w.run("?DEBUG,ON")
            wm = w.dev.mark()
            changed = True
            for p in ("S3", "S4"):
                if not _has(w.run(f"?BAUD,{p},19200"), f"Baud rate for Serial{p[1]} updated to 19200"):
                    problems.append(f"?BAUD,{p},19200 did not confirm")
            begun = [x for x in w.dev.since(wm) if re.match(r"^\[SOFTSERIAL\] S[34] ", x)]
            if begun:
                problems.append(f"?BAUD re-began a soft UART on a PWM port: {begun}")
            if not any(x.startswith("[SOFTSERIAL] S5 RX: ") for x in w.run("?BAUD,S5,9600")):
                problems.append("control: ?BAUD,S5,9600 under ?DEBUG,ON logged no soft-serial begin, so the S3/S4 check proves nothing")
            w.run("?DEBUG,OFF")
            s4.pwm_stop()
            if s4.line_level() != 0:
                problems.append("W1 S4 no longer idles LOW after ?BAUD,S4: a soft UART took the PWM pin back")
            s4.pwm_in()
            time.sleep(0.6)
            for us in (1500, 1000, 2000):
                pm = s4.probe.dev.mark()
                s3.pwm_out(us)
                time.sleep(1.0)
                problem = _step_problem(s4.pulses(pm), us, " (after ?BAUD)")
                if problem:
                    problems.append(problem)
        finally:
            s3.pwm_out(0)
            s4.pwm_stop()
            w.run("?DEBUG,OFF")
            if changed:
                for p in ("S3", "S4"):
                    w.run(f"?BAUD,{p},9600")
            if mapped:
                _clear_local_mapping(w, "S3")
            s3.pwm_stop()
    assert not problems, "; ".join(problems)
