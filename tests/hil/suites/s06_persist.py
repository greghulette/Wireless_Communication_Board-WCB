"""Saved settings: change, verify on the wire / in the config chain, restore, and prove the
board came back byte-identical (config_guard). Runs on W1 (USB) and W2 (over the mesh)."""
import time

from hil.runner import Skip, test
from suites.common import config_guard, marker, snapshot, token, usb_wcb, wire


def _restore_label(send, before, port):
    orig = token(before, f"?LABEL,{port},")
    send(orig if orig else f"?LABEL,CLEAR,{port}")


@test("persist.label", "?LABEL sets a port label in W1's saved config", needs=["wcb1"])
def label(bench):
    w = usb_wcb(bench)
    with config_guard(bench, 1) as before:
        t = marker()
        try:
            lines = w.run(f"?LABEL,S5,{t}")
            assert any(f"Serial5 label set to: '{t}'" in x for x in lines), f"got {lines}"
            assert f"?LABEL,S5,{t}" in snapshot(bench, 1), "label missing from ?backup"
        finally:
            _restore_label(w.run, before[1], "S5")


@test("persist.label_remote", ";W2,?LABEL changes W2's saved config over the mesh", needs=["wcb1"])
def label_remote(bench):
    w = usb_wcb(bench)
    with config_guard(bench, 2) as before:
        t = marker()
        try:
            w.send(f";W2,?LABEL,S4,{t}")
            time.sleep(1.0)   # let W2 drain the command before the pull is answered
            assert f"?LABEL,S4,{t}" in snapshot(bench, 2), "label missing from W2's ?MGMT,PULL"
        finally:
            _restore_label(lambda c: w.send(f";W2,{c}"), before[2], "S4")
            time.sleep(1.0)


@test("persist.baud_live", "?BAUD changes S1's rate live; bytes follow the new rate", needs=["wcb1", "probe1"])
def baud_live(bench):
    w = usb_wcb(bench)
    with config_guard(bench, 1) as before:
        orig = token(before[1], "?BAUD,S1,")
        orig_rate = int(orig.split(",")[2])
        new_rate = 115200 if orig_rate != 115200 else 57600
        try:
            lines = w.run(f"?BAUD,S1,{new_rate}")
            assert any(f"Baud rate for Serial1 updated to {new_rate}" in x for x in lines), f"got {lines}"
            probe, ch = wire(bench, 1, "S1", baud=new_rate)
            t = marker()
            m = probe.dev.mark()
            w.send(f";S1{t}")
            probe.expect_bytes(ch, t.encode() + b"\r", timeout=2, since=m)
        finally:
            w.run(orig)
        probe, ch = wire(bench, 1, "S1", baud=orig_rate)
        t = marker()
        m = probe.dev.mark()
        w.send(f";S1{t}")
        probe.expect_bytes(ch, t.encode() + b"\r", timeout=2, since=m)


@test("persist.label_reboot", "A saved label survives a W1 reboot", needs=["wcb1"])
def label_reboot(bench):
    w = usb_wcb(bench)
    with config_guard(bench, 1) as before:
        t = marker()
        try:
            w.run(f"?LABEL,S5,{t}")
            w.reboot()
            assert f"?LABEL,S5,{t}" in snapshot(bench, 1), "label lost across reboot"
        finally:
            _restore_label(w.run, before[1], "S5")


@test("persist.legacy_label_baud", "Legacy ?SLS<n>,<label>, ?SLCS<n> and ?BAUDS<n>,<rate> reach the ?LABEL / ?BAUD setters, with their own error lines", needs=["wcb1"], links=[])
def legacy_label_baud(bench):
    """WCB.ino's legacy block: ?SLS passes the whole message to updateSerialLabel (its own port and length checks), ?SLC
    takes the port from the fourth character on (so the help's ?SLCSx form is the working one), ?BAUDS calls
    updateBaudRate. The baud form is sent at S5's current rate, so the port never changes speed."""
    w = usb_wcb(bench)
    with config_guard(bench, 1) as before:
        rate = token(before[1], "?BAUD,S5,")
        if not rate:
            raise Skip("W1 config lacks ?BAUD,S5")
        rate = rate.split(",")[2]
        t = marker()
        bad = []
        try:
            for cmd, want in ((f"?SLS5,{t}", f"Serial5 label set to: '{t}'"), ("?SLCS5", "Serial5 label cleared"),
                              ("?SLS9,x", "Invalid port. Must be 1-5"), ("?SLS5", "Invalid format. Use: ?SLSx,label"),
                              (f"?BAUDS5,{rate}", f"Baud rate for Serial5 updated to {rate}")):
                out = w.run(cmd)
                if not any(want in x for x in out):
                    bad.append(f"{cmd}: {out}")
        finally:
            _restore_label(w.run, before[1], "S5")
    assert not bad, "; ".join(bad)
