"""Maestro: byte-exact Pololu frames on W1 S1 (probe1), fan-out to NaviCore, and a real
servo round trip on W2's Maestro 2 (setTarget, then getPosition read back over the mesh).

Frames are built by WcbCmd's WcbMaestro (Pololu protocol, 14-bit values low-7 then high-7)."""
import json
import time

from hil.navicore import NaviCore
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
    probe, ch = wire(bench, 1, "S1")
    w = usb_wcb(bench)
    m = probe.dev.mark()
    for bad in ("bogus", "setTarget,128,6000", "setTarget,0,16384", "setAccel,0,256", "sub,256"):
        w.send(f";M1,{bad}")
    probe.expect_silence(ch, window=1.5, since=m)


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
    got = nc.json_cmd({"type": "GET_CONFIG"}, r'^\{"type":"CONFIG","data":', timeout=10)
    local = {s.get("device") for s in json.loads(got.string)["data"].get("maestros", []) if s.get("type") == 1}
    nc.set_debug_flags(1)   # DBG_MAESTRO (NaviCore.ino:495)
    try:
        m = nc.dev.mark()
        _frame(bench, ";M11", bytes.fromhex("AA012701"))
        if 1 not in local:
            time.sleep(2)
            stale = any("inbound device 1 matches no local slot" in x for x in nc.dev.since(m))
            raise Skip("NaviCore hosts no Maestro device 1" + ("; W1's M1:W20 proxy is stale" if stale else ""))
        nc.dev.expect(r"^\[DISPATCH\] Maestro slot \d+ \(device 1\) <- mesh  cmd 0x27$", timeout=3, since=m)
    finally:
        nc.set_debug_flags(0)


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
    w = usb_wcb(bench)
    for target in (6400, 6000):
        w.send(f";M2,setTarget,0,{target}")
        time.sleep(1.5)
        pos = _read_pos(w, 2, 0)
        bench.note(f"Maestro 2 ch0 target {target} -> reads {pos}")
        assert pos == target, f"Maestro 2 ch0: set {target}, read back {pos}"
