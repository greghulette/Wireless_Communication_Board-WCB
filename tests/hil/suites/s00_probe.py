"""Probes and wires — if these fail, nothing downstream is trustworthy."""
from hil.runner import Skip, test


@test("probe.hello", "Every probe answers and runs wcb_probe v2 or newer", links=[])
def probe_hello(bench):
    names = bench.probe_names()
    if not names:
        raise Skip("no probes in bench.json")
    for name in names:
        p = bench.probe(name)   # resets it, which requires v2
        m = p.hello()
        bench.note(f"{name}: wcb_probe v{m.group(1)} mac={m.group(2)}")


@test("probe.links", "Every discovered wire verified at its WCB port's configured baud", links=[])
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
