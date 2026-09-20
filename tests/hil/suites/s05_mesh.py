""";W routing, ETM accounting, MGMT fragments and the remote terminal (W1 -> W2)."""
import time

from hil.runner import test
from suites.common import marker, snapshot, token, usb_wcb, wire


@test("mesh.unicast_acked", ";W2 unicast is delivered and ACKed exactly once in W1's ETM stats",
      needs=["wcb1", "probe2"])
def unicast_acked(bench):
    w = usb_wcb(bench)
    probe, ch = wire(bench, 2, "S2")
    before = w.etm_board_stats()[2]
    t = marker()
    m = probe.dev.mark()
    w.send(f";W2,;S2{t}")
    probe.expect_bytes(ch, t.encode() + b"\r", timeout=3, since=m)
    time.sleep(1.0)
    after = w.etm_board_stats()[2]
    bench.note(f"W2 ETM stats before {before} after {after}")
    assert after["ackd"] >= before["ackd"] + 1, f"ACK count did not rise: {before} -> {after}"
    assert after["failed"] == before["failed"], f"failures rose: {before} -> {after}"


@test("mesh.alias", ";W<alias>,<cmd> routes by the remote board's alias", needs=["wcb1", "probe2"])
def alias(bench):
    alias_tok = token(snapshot(bench, 2), "?ALIAS,")
    if not alias_tok:
        from hil.runner import Skip
        raise Skip("W2 has no alias")
    name = alias_tok.split(",", 1)[1]
    probe, ch = wire(bench, 2, "S2")
    t = marker()
    m = probe.dev.mark()
    usb_wcb(bench).send(f";W{name},;S2{t}")
    probe.expect_bytes(ch, t.encode() + b"\r", timeout=3, since=m)


@test("mesh.unreachable", ";W to a board that is not a peer says so", needs=["wcb1"])
def unreachable(bench):
    lines = usb_wcb(bench).run(";W15,;S1x")
    assert any("WCB 15 is not a reachable target" in t for t in lines), f"got {lines}"


@test("mesh.chain_split", "Characterisation: ;W2,a^b sends only a to W2 and runs b locally",
      needs=["wcb1", "probe1", "probe2"])
def chain_split(bench):
    p2, c2 = wire(bench, 2, "S2")
    p1, c1 = wire(bench, 1, "S1")
    a, b = marker("a"), marker("b")
    m1, m2 = p1.dev.mark(), p2.dev.mark()
    usb_wcb(bench).send(f";W2,;S2{a}^;S1{b}")
    p2.expect_bytes(c2, a.encode() + b"\r", timeout=3, since=m2)
    p1.expect_bytes(c1, b.encode() + b"\r", timeout=3, since=m1)


@test("mesh.frag_chain", "?MGMT,FRAG runs a whole ^ chain on the target board", needs=["wcb1", "probe2"])
def frag_chain(bench):
    probe, ch = wire(bench, 2, "S2")
    a, b = marker("a"), marker("b")
    m = probe.dev.mark()
    usb_wcb(bench).send(f"?MGMT,FRAG,2,A1,0,1,;S2{a}^;S2{b}")
    probe.expect_bytes(ch, f"{a}\r{b}\r".encode(), timeout=4, since=m)


@test("mesh.rterm", "?RTERM mirrors W2's console to W1 as [TERM:2] lines", needs=["wcb1"])
def rterm(bench):
    w = usb_wcb(bench)
    fw = w.version()
    with w.remote_terminal(2, 1):
        lines = w.remote_run(2, "?VERSION", "End of Version")
    assert f"Software Version: {fw}" in lines, f"W2 terminal said {lines}, W1 runs {fw}"
