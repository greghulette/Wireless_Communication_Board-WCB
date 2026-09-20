"""Saved settings: change, verify on the wire / in the config chain, restore, and prove the
board came back byte-identical (config_guard). Runs on W1 (USB) and W2 (over the mesh)."""
import time

from hil.runner import test
from suites.common import config_guard, marker, snapshot, token, usb_wcb, wire


def _restore_label(send, before, port):
    orig = token(before, f"?LABEL,{port},")
    send(orig if orig else f"?LABEL,CLEAR,{port}")


@test("persist.label", "?LABEL sets a port label in W1's saved config", needs=["wcb1"])
def label(bench):
    w = usb_wcb(bench)
    with config_guard(bench, 1) as before:
        t = marker()
        lines = w.run(f"?LABEL,S5,{t}")
        assert any(f"Serial5 label set to: '{t}'" in x for x in lines), f"got {lines}"
        assert f"?LABEL,S5,{t}" in snapshot(bench, 1), "label missing from ?backup"
        _restore_label(w.run, before[1], "S5")


@test("persist.label_remote", ";W2,?LABEL changes W2's saved config over the mesh", needs=["wcb1"])
def label_remote(bench):
    w = usb_wcb(bench)
    with config_guard(bench, 2) as before:
        t = marker()
        w.send(f";W2,?LABEL,S4,{t}")
        time.sleep(1.0)   # let W2 drain the command before the pull is answered
        assert f"?LABEL,S4,{t}" in snapshot(bench, 2), "label missing from W2's ?MGMT,PULL"
        _restore_label(lambda c: w.send(f";W2,{c}"), before[2], "S4")
        time.sleep(1.0)


@test("persist.baud_live", "?BAUD changes S1's rate live; bytes follow the new rate", needs=["wcb1", "probe1"])
def baud_live(bench):
    w = usb_wcb(bench)
    with config_guard(bench, 1) as before:
        orig = token(before[1], "?BAUD,S1,")
        orig_rate = int(orig.split(",")[2])
        new_rate = 115200 if orig_rate != 115200 else 57600
        lines = w.run(f"?BAUD,S1,{new_rate}")
        assert any(f"Baud rate for Serial1 updated to {new_rate}" in x for x in lines), f"got {lines}"
        probe, ch = wire(bench, 1, "S1", baud=new_rate)
        t = marker()
        m = probe.dev.mark()
        w.send(f";S1{t}")
        probe.expect_bytes(ch, t.encode() + b"\r", timeout=2, since=m)
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
        w.run(f"?LABEL,S5,{t}")
        w.reboot()
        assert f"?LABEL,S5,{t}" in snapshot(bench, 1), "label lost across reboot"
        _restore_label(w.run, before[1], "S5")
