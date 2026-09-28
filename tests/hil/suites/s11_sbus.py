"""SBUS controller -> NaviCore SBUS input, checked on NaviCore's decoded channel table (#L09).

SBUSController has no mesh; its only output is SBUS on GPIO9 (+4 PWM). Every serial command
must start with '{' — stray characters outside JSON are single-key commands, some of which
write flash (SBUSController.ino handleSerialCommands). hil/sbus.py SbusCtl sends only JSON."""
import time

from hil.navicore import NaviCore
from hil.runner import Skip, test
from hil.sbus import SbusCtl


def _sbus(bench):
    return SbusCtl(bench.dev("sbus"))


@test("sbus.ping", "SBUS controller answers ping with its firmware version", needs=["sbus"])
def ping(bench):
    bench.note(f"SBUS firmware {_sbus(bench).ping()}")


@test("sbus.to_navicore", "Moving a virtual stick changes NaviCore's decoded SBUS channel, and releasing restores it",
      needs=["sbus", "navicore"])
def to_navicore(bench):
    ctl = _sbus(bench)
    ctl.ping()
    nc = NaviCore(bench.dev("navicore"))
    base = nc.sbus_state()
    if base["fps"] == 0:
        raise Skip("NaviCore sees no SBUS frames — controller not wired to NaviCore's SBUS input")
    # The stick's exact values come from the controller's own calibration (getcfg answers only within 5 s of a ping -
    # the one above - SBUSController.ino:1001-1029): lx drives channel cfg["lx"] with limits aMin/aMax/aRev index 3,
    # full deflection is aMax (aMin when reversed) and centre is (int)((aMax-aMin)*0.5 + aMin + 0.5)
    # (SBUSController.ino:1103-1116).
    cfg = ctl.cfg(ping=False)
    lx, lo, hi, rev = cfg["lx"], cfg["aMin"][3], cfg["aMax"][3], cfg["aRev"][3]
    centre_value = int((hi - lo) * 0.5 + lo + 0.5)
    try:
        ctl.axes(1, 0, 0, 0)
        time.sleep(0.6)
        moved = nc.sbus_state()["channels"]
        bench.note(f"SBUS CH1-4 base {base['channels'][:4]} -> stick {moved[:4]}; lx is CH{lx}")
        assert moved[lx - 1] == (lo if rev else hi), f"lx full: CH{lx} = {moved[lx - 1]}, expected {lo if rev else hi}"
    finally:
        ctl.axes(0, 0, 0, 0)
    time.sleep(0.6)
    after = nc.sbus_state()["channels"]
    assert after[lx - 1] == centre_value, f"lx centred: CH{lx} = {after[lx - 1]}, expected {centre_value}"
    drift = [i + 1 for i in range(4) if abs(after[i] - base["channels"][i]) > 20]
    assert not drift, f"CH{drift} did not return to centre: {base['channels'][:4]} -> {after[:4]}"
