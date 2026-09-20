"""Stored sequences (;C recall, local and mesh-wide) and variables with IF gating."""
import time

from hil.runner import test
from suites.common import config_guard, marker, usb_wcb, wire


@test("seq.local", "?SEQ,SAVE then ;C<key>,L replays the chain locally", needs=["wcb1", "probe1"])
def seq_local(bench):
    w = usb_wcb(bench)
    probe, ch = wire(bench, 1, "S1")
    with config_guard(bench, 1):
        a, b = marker("a"), marker("b")
        lines = w.run(f"?SEQ,SAVE,HILSEQ,;S1{a}^;S1{b}")
        assert any("Stored: Key='HILSEQ'" in x for x in lines), f"got {lines}"
        m = probe.dev.mark()
        w.send(";CHILSEQ,L")
        probe.expect_bytes(ch, f"{a}\r{b}\r".encode(), timeout=3, since=m)
        w.run("?SEQ,CLEAR,HILSEQ")


@test("seq.timer", "A stored sequence keeps its ;T delay when recalled", needs=["wcb1", "probe1"])
def seq_timer(bench):
    w = usb_wcb(bench)
    probe, ch = wire(bench, 1, "S1")
    with config_guard(bench, 1):
        a, b = marker("a"), marker("b")
        w.run(f"?SEQ,SAVE,HILSEQT,;S1{a}^;T800^;S1{b}")
        m = probe.dev.mark()
        w.send(";CHILSEQT,L")
        probe.expect_bytes(ch, b.encode(), timeout=4, since=m)
        gap = probe.time_of(ch, b.encode(), m) - probe.time_of(ch, a.encode(), m)
        bench.note(f"recalled timer gap {gap} ms")
        w.run("?SEQ,CLEAR,HILSEQT")
        assert 700 <= gap <= 1100, f"gap {gap} ms, expected ~800"


@test("seq.mesh", "A top-level ;C<key> recalls the sequence on every board that has it",
      needs=["wcb1", "probe2"])
def seq_mesh(bench):
    w = usb_wcb(bench)
    probe, ch = wire(bench, 2, "S2")
    with config_guard(bench, 1, 2):
        t = marker()
        w.send(f";W2,?SEQ,SAVE,HILMS,;S2{t}")
        time.sleep(1.5)
        m = probe.dev.mark()
        w.send(";CHILMS")
        try:
            probe.expect_bytes(ch, t.encode() + b"\r", timeout=4, since=m)
        finally:
            w.send(";W2,?SEQ,CLEAR,HILMS")
            time.sleep(1.5)


@test("var.if", "IF,<var>=<n> gates the next command; INC updates the variable", needs=["wcb1", "probe1"])
def var_if(bench):
    w = usb_wcb(bench)
    probe, ch = wire(bench, 1, "S1")
    yes, no = marker("y"), marker("n")
    try:
        w.run(";V,hilv,5")
        m = probe.dev.mark()
        wm = w.send(f"IF,hilv=5^;S1{yes}")
        probe.expect_bytes(ch, yes.encode(), timeout=2, since=m)
        w.dev.expect(r"^\[IF\] .*-> true", timeout=2, since=wm)
        wm = w.send(f"IF,hilv=4^;S1{no}")
        w.dev.expect(r"^\[IF\] .*-> false", timeout=2, since=wm)
        time.sleep(1.0)
        assert no.encode() not in probe.received(ch, m), "gated command ran anyway"
        w.run(";V,hilv,INC,2")
        lines = w.run("?VAR,GET,hilv")
        assert any(x.startswith("[VAR] hilv = 7") for x in lines), f"got {lines}"
    finally:
        w.run("?VAR,CLEAR,hilv")
