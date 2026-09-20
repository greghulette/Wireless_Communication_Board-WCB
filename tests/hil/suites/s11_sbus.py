"""SBUS controller -> NaviCore SBUS input, checked on NaviCore's decoded channel table (#L09).

SBUSController has no mesh; its only output is SBUS on GPIO9 (+4 PWM). Every serial command
must start with '{' — stray characters outside JSON are single-key commands, some of which
write flash (SBUSController.ino handleSerialCommands)."""
import json
import time

from hil.navicore import NaviCore
from hil.runner import Skip, test


def _sbus(bench):
    return bench.dev("sbus")


def _send(dev, obj):
    dev.send(json.dumps(obj, separators=(",", ":")))


def _ping(dev, timeout=60.0):
    """Opening the port can reset the controller, and its WiFi cascade can hold boot ~50 s."""
    deadline = time.monotonic() + timeout
    while True:
        m = dev.mark()
        _send(dev, {"t": "ping"})
        try:
            return dev.expect(r'^\{"t":"pong".*"fwver":"([^"]+)"', timeout=3, since=m).group(1)
        except AssertionError:
            if time.monotonic() > deadline:
                raise


@test("sbus.ping", "SBUS controller answers ping with its firmware version", needs=["sbus"])
def ping(bench):
    bench.note(f"SBUS firmware {_ping(_sbus(bench))}")


@test("sbus.to_navicore", "Moving a virtual stick changes NaviCore's decoded SBUS channel, and releasing restores it",
      needs=["sbus", "navicore"])
def to_navicore(bench):
    dev = _sbus(bench)
    _ping(dev)
    nc = NaviCore(bench.dev("navicore"))
    base = nc.sbus_state()
    if base["fps"] == 0:
        raise Skip("NaviCore sees no SBUS frames — controller not wired to NaviCore's SBUS input")
    # The stick's exact values come from the controller's own calibration (getcfg answers only within 5 s of a ping,
    # SBUSController.ino:1001-1029): lx drives channel cfg["lx"] with limits aMin/aMax/aRev index 3, full deflection is
    # aMax (aMin when reversed) and centre is (int)((aMax-aMin)*0.5 + aMin + 0.5) (SBUSController.ino:1103-1116).
    m = dev.mark()
    _send(dev, {"t": "getcfg"})
    cfg = json.loads(dev.expect(r'^\{"e":"cfg"', timeout=5, since=m).string)
    lx, lo, hi, rev = cfg["lx"], cfg["aMin"][3], cfg["aMax"][3], cfg["aRev"][3]
    centre_value = int((hi - lo) * 0.5 + lo + 0.5)
    centre = {"t": "a", "lx": 0, "ly": 0, "rx": 0, "ry": 0}
    try:
        _send(dev, {"t": "a", "lx": 1, "ly": 0, "rx": 0, "ry": 0})
        time.sleep(0.6)
        moved = nc.sbus_state()["channels"]
        bench.note(f"SBUS CH1-4 base {base['channels'][:4]} -> stick {moved[:4]}; lx is CH{lx}")
        assert moved[lx - 1] == (lo if rev else hi), f"lx full: CH{lx} = {moved[lx - 1]}, expected {lo if rev else hi}"
    finally:
        _send(dev, centre)
    time.sleep(0.6)
    after = nc.sbus_state()["channels"]
    assert after[lx - 1] == centre_value, f"lx centred: CH{lx} = {after[lx - 1]}, expected {centre_value}"
    drift = [i + 1 for i in range(4) if abs(after[i] - base["channels"][i]) > 20]
    assert not drift, f"CH{drift} did not return to centre: {base['channels'][:4]} -> {after[:4]}"
