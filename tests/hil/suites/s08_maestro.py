"""Maestro: byte-exact Pololu frames on W1 S1 (probe1), fan-out to NaviCore, and a real
servo round trip on W2's Maestro 2 (setTarget, then getPosition read back over the mesh).

Frames are built by WcbCmd's WcbMaestro (Pololu protocol, 14-bit values low-7 then high-7)."""
import time

from hil.navicore import DBG_MAESTRO, NaviCore
from hil.runner import Skip, test
from suites.common import usb_wcb, wire

NEEDS = ["wcb1", "probe1"]


def _frame(bench, command, expected, settle=0.4):
    probe, ch = wire(bench, 1, "S1")
    m = probe.dev.mark()
    usb_wcb(bench).send(command)
    probe.expect_bytes(ch, expected, timeout=2, since=m)
    time.sleep(settle)
    got = probe.received(ch, m)
    assert got == expected, f"{command}: expected exactly {expected.hex(' ')}, got {got.hex(' ')}"


@test("maestro.sub", ";M11 writes AA 01 27 01 (restart script at subroutine 1)", needs=NEEDS)
def sub(bench):
    _frame(bench, ";M11", bytes.fromhex("AA012701"))


@test("maestro.comma_int", ";M1,7 is the same as ;M17", needs=NEEDS)
def comma_int(bench):
    _frame(bench, ";M1,7", bytes.fromhex("AA012707"))


VERBS = [
    ("setTarget,0,6000", "AA010400702E"),
    ("setSpeed,0,20", "AA0107001400"),
    ("setAccel,0,5", "AA0109000500"),
    ("goHome", "AA0122"),
    ("stopScript", "AA0124"),
    ("sub,3,1000", "AA0128036807"),
    ("getMovingState", "AA0113"),
    ("getErrors", "AA0121"),
    ("getPosition,5", "AA011005"),
]


@test("maestro.verbs", "Every ;M1,<verb> produces its exact Pololu frame", needs=NEEDS)
def verbs(bench):
    for verb, hexbytes in VERBS:
        _frame(bench, f";M1,{verb}", bytes.fromhex(hexbytes))


@test("maestro.bad_verb", "An unknown verb or an out-of-range argument never reaches the servo wire",
      needs=NEEDS)
def bad_verb(bench):
    # Ranges are the Pololu protocol's, not a particular Maestro's: channel and device are
    # 7-bit (0-127), targets/speeds 14-bit (0-16383), accel 0-255 (WcbCmd WcbMaestro.cpp:35-63).
    _frame(bench, ";M11", bytes.fromhex("AA012701"))   # positive control: the wire carries frames before silence counts
    probe, ch = wire(bench, 1, "S1")
    w = usb_wcb(bench)
    m = probe.dev.mark()
    for bad in ("bogus", "setTarget,128,6000", "setTarget,0,16384", "setAccel,0,256", "sub,256"):
        w.send(f";M1,{bad}")
    probe.expect_silence(ch, window=1.5, since=m)


# Numbers that wrapped onto a valid frame when cast to their wire width (WcbCmd 0.9.1), and subroutines over 127, whose
# byte reads as a command byte, in each spelling (the short and comma forms are the WCB's own, WCB.ino).
NO_ALIAS = (";M1,setTarget,261,6000", ";M1,setTarget,5,70000", ";M1,setSpeed,5,65537", ";M1,setAccel,261,5",
            ";M1,getPosition,261", ";M1,sub,200", ";M1,200", ";M1200")


@test("maestro.no_alias", "(should) An out-of-range ;M number never wraps onto a valid frame: channel 261, target "
      "70000, speed 65537 and subroutine 200 (;M1200, ;M1,200, ;M1,sub,200) put nothing on the servo wire", needs=NEEDS)
def no_alias(bench):
    """NAVICORE.md D-NC76, and D-NC23's WCB twin. WcbCmd's build() cast the channel, target and speed to their wire
    widths before any check (WcbMaestro.cpp, 0.9.1), so ;M1,setTarget,261,6000 sent channel 5, a target of 70000 sent
    4464 and a speed of 65537 sent 1, each a valid command for something else; the builders' own range checks saw only
    the cast value. Subroutines 128-255 went out unmasked, from WcbCmd and from the WCB's own short (;M1200) and comma
    (;M1,200) forms: 0xC8 inside the frame reads as a new command. maestro.bad_verb's values sit just past each range
    and never wrapped. Passing: the positive control arrives, then not one byte reaches W1 S1 for any of these."""
    _frame(bench, ";M11", bytes.fromhex("AA012701"))   # positive control
    probe, ch = wire(bench, 1, "S1")
    w = usb_wcb(bench)
    sent = []
    for cmd in NO_ALIAS:
        m = probe.dev.mark()
        w.send(cmd)
        time.sleep(0.6)
        got = probe.received(ch, m)
        if got:
            sent.append(f"{cmd} sent {got.hex(' ')}")
    assert not sent, "(should, D-NC76) out-of-range numbers reached the wire: " + "; ".join(sent)


@test("maestro.target9", ";M9,goHome re-addresses to each local Maestro's own id", needs=NEEDS)
def target9(bench):
    _frame(bench, ";M9,goHome", bytes.fromhex("AA0122"))


@test("maestro.fanout_remote", ";M11 also reaches the remote proxy host (NaviCore) over the mesh",
      needs=NEEDS + ["navicore"])
def fanout_remote(bench):
    """W1 fans ;M11 out to every slot with id 1, its WCB20 proxy included. NaviCore's side is asserted on its exact
    dispatch line: a looser pattern also matched '[DISPATCH] Maestro: inbound device 1 matches no local slot' — what
    NaviCore prints when W1's proxy is stale because it hosts no device 1 — and passed falsely."""
    nc = NaviCore(bench.dev("navicore"))
    local = {dev for _, dev in nc.local_slots(nc.config())}
    with nc.debug(DBG_MAESTRO):
        m = nc.dev.mark()
        _frame(bench, ";M11", bytes.fromhex("AA012701"))
        if 1 not in local:
            time.sleep(2)
            stale = any("inbound device 1 matches no local slot" in x for x in nc.dev.since(m))
            raise Skip("NaviCore hosts no Maestro device 1" + ("; W1's M1:W20 proxy is stale" if stale else ""))
        nc.dev.expect(r"^\[DISPATCH\] Maestro slot \d+ \(device 1\) <- mesh  cmd 0x27$", timeout=3, since=m)


def _read_pos(w, dev, ch):
    w.run(f"?VAR,CLEAR,m{dev}pos{ch}")
    w.send(f";M{dev},getPosition,{ch}")
    time.sleep(1.2)
    for t in w.run(f"?VAR,GET,m{dev}pos{ch}"):
        if t.startswith(f"[VAR] m{dev}pos{ch} = "):
            return int(t.split("=")[1])
    return None


@test("maestro.remote_readback", "W2's real Maestro 2 moves to setTarget and reports it back over the mesh",
      needs=["wcb1"])
def remote_readback(bench):
    """Reads where the servo rests first and ends there (a getPosition on a Maestro returns its current target; 0
    means the channel is off). The move is 400 quarter-us (100 us) from rest, or to 6400 when the channel is off."""
    w = usb_wcb(bench)
    p0 = _read_pos(w, 2, 0)
    assert p0 is not None, "Maestro 2 ch0 did not answer getPosition before the move"
    away = 6400 if p0 == 0 else (p0 + 400 if p0 <= 7600 else p0 - 400)
    bench.note(f"Maestro 2 ch0 rests at {p0}; moving to {away} and back")
    try:
        for target in (away, p0):
            w.send(f";M2,setTarget,0,{target}")
            time.sleep(1.5)
            pos = _read_pos(w, 2, 0)
            bench.note(f"Maestro 2 ch0 target {target} -> reads {pos}")
            assert pos == target, f"Maestro 2 ch0: set {target}, read back {pos}"
    finally:
        w.send(f";M2,setTarget,0,{p0}")
        time.sleep(1.0)
        w.run("?VAR,CLEAR,m2pos0")


# ============================================================ coverage re-scan 2026-10-12 (low-risk rows)
def _quiet_s1_after(w, s1, ch, cmd, settle=0.8):
    """Send `cmd` on W1 -> (W1's console lines up to the end marker run() appends, bytes W1 S1 got meanwhile)."""
    m = s1.dev.mark()
    lines = [x.rstrip() for x in w.run(cmd)]
    time.sleep(settle)
    return lines, s1.received(ch, m)


@test("maestro.sub_comma_form_edges", "The comma subroutine form with no device (;M,5) is a bad verb: '[MAESTRO] bad verb' under ?DEBUG,ON, nothing on W1 S1 and nothing to the mesh; with a two-digit device (;M12,5) it takes the verb path: no slot for 12, so nothing on S1 and one fallback broadcast of the comma text ';M12,5', which W2's Maestro never gets", needs=NEEDS + ["wcb2"])
def sub_comma_form_edges(bench):
    """Coverage re-scan 2026-10-12, maestro.sub_comma_form. processMaestroCommand (WCB.ino) sends ;M<dev>,<n> down the
    subroutine path only for a one-digit device and a plain integer; an empty device field or a device of 10 or more
    falls through to sendMaestroServoVerb (WCB_Maestro.cpp), where WcbMaestro::build refuses ',5' (no device number) and
    builds AA 0C 27 05 for '12,5'. Device 12 has no slot anywhere (ids are 1-8), so the frame goes nowhere locally and
    the verb text is forwarded as an unconfigured fallback - broadcast, since 12 is above WCBQ - in the comma form
    maestroVerbForwardText keeps for a device above 9. ;M1,5 on W1's own Maestro 1 is the control that the wire carries
    frames. Subroutine 5 of device 12 addresses no Maestro on the bench."""
    probe, ch = wire(bench, 1, "S1")
    tap, tch = wire(bench, 2, "S1")
    w = usb_wcb(bench)
    bad = []
    try:
        for kind in ("", "MAESTRO,", "ETM,"):
            w.run(f"?DEBUG,{kind}ON")
        lines, got = _quiet_s1_after(w, probe, ch, ";M1,5")
        if got != bytes.fromhex("AA012705"):
            raise AssertionError(f"control: ;M1,5 should write AA 01 27 05 on W1 S1, got {got.hex(' ') or 'nothing'}")
        lines, got = _quiet_s1_after(w, probe, ch, ";M,5")
        if "[MAESTRO] bad verb, ignored: ,5" not in lines:
            bad.append(f";M,5 printed no bad-verb line: {[x for x in lines if 'MAESTRO' in x or 'Maestro' in x]}")
        if got:
            bad.append(f";M,5 put {got.hex(' ')} on W1 S1")
        sends = [x for x in lines if x.startswith("[ETM] Sent seq") and ";M" in x]
        if sends:
            bad.append(f";M,5 went to the mesh: {sends}")
        tm = tap.dev.mark()
        lines, got = _quiet_s1_after(w, probe, ch, ";M12,5", settle=1.5)
        if got:
            bad.append(f";M12,5 put {got.hex(' ')} on W1 S1 with no slot for 12")
        sends = [x for x in lines if x.startswith("[ETM] Sent seq") and ";M" in x]
        if len(sends) != 1 or not sends[0].endswith(": ;M12,5"):
            bad.append(f";M12,5 should go to the mesh once as ';M12,5', got {sends}")
        if "→ Maestro 12 verb '12,5': Fallback broadcast" not in lines:
            bad.append(f";M12,5 printed no 'Fallback broadcast' line: {[x for x in lines if 'Maestro' in x]}")
        if _has_bad_verb(lines):
            bad.append(";M12,5 was refused as a bad verb")
        if tap.received(tch, tm):
            bad.append(f"W2's Maestro port got {tap.received(tch, tm).hex(' ')} from ;M12,5")
    finally:
        for kind in ("", "MAESTRO,", "ETM,"):
            w.run(f"?DEBUG,{kind}OFF")
    assert not bad, "; ".join(bad)


def _has_bad_verb(lines):
    return any(x.startswith("[MAESTRO] bad verb") for x in lines)


@test("maestro.bare_or_letter_ignored", "A bare ;M and a ;M followed by a letter (;Mx) are ignored: no routing or bad-verb line, no 'Invalid Serial Command', nothing to the mesh and nothing on W1 S1", needs=NEEDS)
def bare_or_letter_ignored(bench):
    """Coverage re-scan 2026-10-12, maestro.bare_or_letter_ignored. processMaestroCommand (WCB.ino) returns at once for a
    ;M with no comma whose next character is not a digit ('bare/typo ;M -> ignore'). Without that return ;Mx would reach
    the subroutine path as device 0 - the broadcast id - and fire subroutine 0 on every Maestro. Every debug flag that
    could show a route is on; ;M11 is the control that W1 S1 carries frames."""
    probe, ch = wire(bench, 1, "S1")
    w = usb_wcb(bench)
    bad = []
    try:
        for kind in ("", "MAESTRO,", "ETM,"):
            w.run(f"?DEBUG,{kind}ON")
        lines, got = _quiet_s1_after(w, probe, ch, ";M11")
        if got != bytes.fromhex("AA012701"):
            raise AssertionError(f"control: ;M11 should write AA 01 27 01 on W1 S1, got {got.hex(' ') or 'nothing'}")
        for cmd in (";M", ";Mx", ";MX5"):
            lines, got = _quiet_s1_after(w, probe, ch, cmd)
            said = [x for x in lines if x.startswith(("→ Maestro", "[MAESTRO]", "Invalid", "Unknown command"))
                    or (x.startswith("[ETM] Sent seq") and ";M" in x)]
            if said:
                bad.append(f"{cmd} printed {said}")
            if got:
                bad.append(f"{cmd} put {got.hex(' ')} on W1 S1")
    finally:
        for kind in ("", "MAESTRO,", "ETM,"):
            w.run(f"?DEBUG,{kind}OFF")
    assert not bad, "; ".join(bad)
