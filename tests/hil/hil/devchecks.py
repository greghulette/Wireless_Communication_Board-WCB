"""The device-to-device wires (bench.json device_links) that the probe sweep cannot see.

Auto-detect makes each WCB port transmit and finds the probe header that hears it, so it proves WCB -> probe wires
only. A wire from one device into another - the SBUS controller into NaviCore or the Kyber, NaviCore's J3 into Maestro
1, the Kyber's MarcDuino and Maestro ports into WCB3 - has no probe on it, or only a listen-only tap, and nothing in the
sweep sends on it. On 2026-10-09 the Kyber's MarcDuino wire into W3 S5 came off in a bench rebuild: auto-detect found
and verified 19 of 19 wires, and only kyber.device_pad_serial, hours into the full run, failed on it.

Each check here drives one link the way a test does and reads its far end, and says which wire to look at when it
fails. They run after the GUI's Auto-detect wires and as probe.device_links at the start of every run. A check whose
devices are not on this bench is skipped, never failed.

The Kyber checks route the SBUS controller's output B to the Kyber for two pad presses (suites/s52_kyber_device.py
KyberPad): one MarcDuino command, one Maestro 1 script (restartScript, which W1 S1's probe stands in for). While routed
the Kyber may stream its CH1-3 pass-through setTargets, so W2's Maestro 2 may centre a servo (hil/servos.py lists
probe.device_links).
"""
import time

from .navicore import NaviCore, SBUS_FULL_FPS

FPS_CLEAR_S = 2.2      # #L09's fps is the last whole second loop() timed: read again once a clean one has passed


class Check:
    """One device link's outcome: ok True / False, or None when skipped; `detail` says what was seen."""

    def __init__(self, name, ok, detail):
        self.name, self.ok, self.detail = name, ok, detail

    @property
    def word(self):
        return "ok" if self.ok else ("skipped" if self.ok is None else "FAILED")

    def __str__(self):
        return f"{self.name}: {self.word} - {self.detail}"


def _sbus_a(bench):
    name = "SBUS A: controller S5 -> NaviCore SBUS IN"
    if not (bench.has("sbus") and bench.has("navicore")):
        return Check(name, None, "no SBUS controller or NaviCore in bench.json")
    nc = NaviCore(bench.dev("navicore"))
    d = nc.sbus_dump()
    if d["fps"] < SBUS_FULL_FPS:                          # the window may hold an earlier stall: one clean second more
        time.sleep(FPS_CLEAR_S)
        d = nc.sbus_dump()
    seen = f"fps {d['fps']}, {d['variant']}, lost={d['lost']}, failsafe={d['failsafe']}"
    ok = d["fps"] >= SBUS_FULL_FPS and d["variant"] == "SBUS-24" and d["lost"] == "no" and d["failsafe"] == "no"
    return Check(name, ok, seen if ok else f"{seen}: check the controller's S5 TX to NaviCore's SBUS IN (GPIO4) and GND")


def _nc_maestro(bench):
    name = "NaviCore J3 <-> Maestro 1"
    if not bench.has("navicore"):
        return Check(name, None, "no NaviCore in bench.json")
    nc = NaviCore(bench.dev("navicore"))
    slots = NaviCore.local_slots(nc.config())
    if not slots:
        return Check(name, None, "NaviCore has no local Maestro slot")
    slot = slots[0][0]
    lines = nc.cli(f"?MAE,GET,{slot},0", until=r"^\[MAE:\d+\]")
    got = next((x for x in lines if x.startswith("[MAE:")), "")
    if '"val"' in got:
        return Check(name, True, f"?MAE,GET,{slot},0 answered {got}")
    return Check(name, False, f"?MAE,GET,{slot},0 answered {got or 'nothing'}: check NaviCore J3 TX (GPIO6) to the "
                              f"Maestro's RX, the Maestro's TX to J3 RX (GPIO7), GND, and the Maestro's power")


def _kyber(bench):
    """Both Kyber links into WCB3, from two routed pad presses (as kyber.device_pad_serial and _maestro do): a button
    whose MarcDuino line is a WCB port command (into W3 S5, run by WCB3, read on that port's probe wire; heard on a W3 S5
    tap too when there is one), and a button that runs a Maestro 1 script (its restartScript into W3 S2, bridged by
    WCB3 to W1 S1's probe wire). Which of the two arrives names the wire: both missing, the Kyber heard nothing."""
    from hil.runner import Skip
    from suites import s52_kyber_device as k
    names = ("SBUS B -> Kyber -> MarcDuino -> W3 S5", "Kyber Maestro -> W3 S2 -> W1 S1")
    if not (bench.has("sbus") and bench.has("wcb3")) or "W3S2" not in (bench.cfg.get("port_devices") or {}):
        return [Check(n, None, "no SBUS controller, WCB3 or Kyber in bench.json") for n in names]
    try:
        cfg = k.kyber_config()
        ch, released, values = k.pad_ladder(cfg)
        if not (ch and released):
            raise Skip("the Kyber config names no pad channel or Released value")
        cases = [c for c in k.port_cmd_cases(cfg) if bench.links.get(c[3], c[4])]
        scripts = [(n, ms) for n, ms in k.maestro_script_buttons(cfg).items() if ms[0] == 1 and n in values]
        s1 = bench.links.get(1, "S1")
        if not cases and not (scripts and s1):
            raise Skip("no Kyber pad button sends a WCB port command to a probe-wired port or a Maestro 1 script")
        k._wcb3_kyber_local(bench)
        ctl = k._ctl(bench)
    except Skip as e:
        return [Check(n, None, str(e)) for n in names]
    tap = bench.links.get(3, "S5")                       # a listen-only tap on the Kyber's MarcDuino TX, if wired
    if tap:
        tap.listen(9600)
    marc_got = marc_sent = maestro_frames = None
    with k.KyberPad(bench, ctl, ch, released, k._pad_rest(bench, ch) or released) as pad:
        if cases:
            n, value, cmd, board, port, text = cases[0]
            out = bench.links.get(board, port)
            out.listen()
            want = text.encode() + b"\r"
            m, tm = out.mark(), (tap.mark() if tap else None)
            pad.press(value)
            deadline = time.monotonic() + k.REPLY_S
            while time.monotonic() < deadline and want not in out.received(m):
                time.sleep(0.05)
            marc_got = out.received(m)
            marc_sent = tap.received(tm) if tap else None
        if scripts and s1:
            sn, (maestro, script) = scripts[0]
            s1.listen()
            m = s1.mark()
            pad.press(values[sn])
            time.sleep(k.MAESTRO_S)
            maestro_frames = k.script_frames(s1.received(m), maestro)
    maestro_ok = None if maestro_frames is None else bool(maestro_frames)
    if maestro_ok is None:
        maestro = Check(names[1], None, "no Maestro 1 script button, or no wire on W1 S1")
    elif maestro_ok:
        maestro = Check(names[1], True, f"button {sn}'s Maestro {maestro} script {script} reached W1 S1 {maestro_frames}")
    else:
        maestro = Check(names[1], False, f"button {sn}'s Maestro {maestro} script {script}: no restartScript reached W1 "
                                         f"S1" + ("" if marc_got else " (and no MarcDuino line either)")
                                         + ": check the Kyber's Maestro TX to W3 S2 RX (GPIO7) and GND")
    if marc_got is None:
        marc = Check(names[0], None, "no pad button sends a WCB port command to a probe-wired port")
    elif want in marc_got:
        marc = Check(names[0], True, f"button {n} {cmd!r} came out W{board}{port}")
    elif tap and marc_sent:
        marc = Check(names[0], False, f"the Kyber sent {marc_sent!r} (the W3 S5 tap heard it) but W{board}{port} got "
                                      f"{marc_got!r}: WCB3's end of the wire into S5, or W3 S5's baud (9600)")
    elif maestro_ok:
        marc = Check(names[0], False, f"button {n} {cmd!r}: W{board}{port} got {marc_got!r}, while the Kyber's Maestro "
                                      f"script went through, so it heard the pad: check the Kyber's MarcDuino TX to W3 "
                                      f"S5 RX and GND" + (" (the W3 S5 tap heard nothing either)" if tap else ""))
    else:
        marc = Check(names[0], False, f"button {n} {cmd!r}: W{board}{port} got {marc_got!r}"
                                      + ("" if maestro_ok is None else " and no Maestro script went through either")
                                      + ": did the Kyber hear the pad? Check SBUS B (controller S4 TX to the Kyber's "
                                        "SBUS IN), the Kyber's power, then its MarcDuino TX to W3 S5 RX")
    if not maestro_ok and maestro_ok is not None and not (marc_got and want in marc_got):
        maestro.detail += "; with the MarcDuino line missing too, start with SBUS B and the Kyber's power"
    return [marc, maestro]


def run_all(bench, log=print):
    """Every device-link check -> [Check]; each is logged as it finishes. An error in one is that link's failure."""
    out = []
    for fn in (_sbus_a, _nc_maestro, _kyber):
        try:
            got = fn(bench)
        except Exception as e:  # noqa: BLE001 - a check that cannot run is a finding about the bench, not a crash
            got = Check(fn.__name__.strip("_"), False, f"the check raised {type(e).__name__}: {e}")
        for c in (got if isinstance(got, list) else [got]):
            out.append(c)
            log(f"  device link {c}")
    return out
