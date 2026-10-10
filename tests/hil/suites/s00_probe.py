"""Probes and wires — if these fail, nothing downstream is trustworthy."""
from hil.runner import Skip, test


@test("probe.hello", "Every probe answers and runs wcb_probe v8 or newer (PROBE_MIN_VERSION: v8 binds a listen-only tap on any soft channel)", links=[])
def probe_hello(bench):
    names = bench.probe_names()
    if not names:
        raise Skip("no probes in bench.json")
    for name in names:
        p = bench.probe(name)   # resets it, which enforces PROBE_MIN_VERSION and refuses PROBE_BAD_VERSIONS
        m = p.hello()
        bench.note(f"{name}: wcb_probe v{m.group(1)} mac={m.group(2)}")


@test("probe.links", "Every wire found by the last discovery verified then at its WCB port's configured baud (link.out re-checks live)", links=[])
def probe_links(bench):
    links = bench.links.all()
    if not links:
        raise Skip("no wires discovered — run discovery (GUI 'Auto-detect wires' or run.py --discover)")
    dev = [l for l in links if bench.links.device_only(l.wcb, l.port)]
    for l in links:
        bench.note(f"{l}  verified={l.verified}" + ("  (a real device is on this port and there is no port_stimulus: "
                                                   "not checked, and tests never use it)" if l in dev else ""))
    if len(dev) == len(links):
        raise Skip("every wire is on a port with a real device, so none can be checked")
    bad = [l.key for l in links if not l.verified and l not in dev]
    assert not bad, f"wires found but not verified: {bad} — re-run discovery or check the cable orientation"


@test("probe.device_links", "Every device-to-device wire answers end to end, which the probe sweep cannot see: SBUS A "
      "into NaviCore at full rate, NaviCore's J3 to Maestro 1 (?MAE,GET answers), and from one Kyber pad press its "
      "MarcDuino line into W3 S5 (run by WCB3) and its Maestro frames into W3 S2 (hil/devchecks.py; a link whose devices "
      "are not on the bench is skipped)", links=[])
def probe_device_links(bench):
    """2026-10-09: auto-detect verified 19 of 19 wires after a rebuild, and kyber.device_pad_serial found the Kyber's
    MarcDuino wire into W3 S5 off hours later. These checks run first, so a run stops on a device wire at once."""
    from hil import devchecks
    checks = devchecks.run_all(bench, log=bench.note)
    if all(c.ok is None for c in checks):
        raise Skip("no device-to-device link on this bench: " + "; ".join(c.detail for c in checks))
    bad = [str(c) for c in checks if c.ok is False]
    assert not bad, "; ".join(bad)
