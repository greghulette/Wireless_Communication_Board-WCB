"""Physical links: every wired WCB port's output lands byte-exact on its probe.

The foundation for every device-byte test — if a link fails here, check the wire before
believing any Maestro/HCR/WLED result.
"""
from hil.runner import Skip, test
from hil.wcb import WCB
from suites.common import marker


@test("link.out", "Every wire carries its WCB port's output byte-exact (local ;S, or ;W<n>;S over the mesh)",
      needs=["wcb1"], links=[])
def link_out(bench):
    links = bench.links.all()
    if not links:
        raise Skip("no wires discovered")
    bad = []
    for l in links:
        if bench.links.device_only(l.wcb, l.port):
            bench.note(f"{l.key}: a real device is on this port and has no port_stimulus — not transmitting into it")
            continue
        if f"W{l.wcb}{l.port}" in bench.cfg.get("port_stimulus", {}):
            console, cmd, expected = bench.links.stimulus(l.wcb, l.port)   # a real device's port: its own frame
        else:
            text = marker()
            console, cmd, _ = bench.links.stimulus(l.wcb, l.port, text=text)
            expected = text.encode() + b"\r"
        try:
            m = l.mark()
            WCB(bench.dev(console)).send(cmd)
            l.expect(expected, timeout=3, since=m)
        except AssertionError as e:
            bad.append(f"{l.key} via {console}: {e}")
    assert not bad, "; ".join(bad)
