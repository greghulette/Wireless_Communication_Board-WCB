"""What to wire: the recommended plan from bench.json, checked against the wires actually found.

Any probe header can be wired to any WCB port, on any WCB — one probe can cover both boards. The
plan only suggests: header S<n> of the WCB's planned probe for port S<n>. When that header is
already taken by a wire you made elsewhere, the suggestion moves to the next free header (that
probe first, then any probe), so the list always follows what is really on the bench.

A port with a real device on it (bench.json "port_devices") gets no probe suggestion unless it is
also marked as a listen-only tap: the device owns that line.
"""
from .probe import HEADERS
from .runner import REGISTRY, describe_device, links_of

# Header pads 1-4 on V2.4 and V3.2 boards, from the KiCad PCB files. The HW 1.0 carrier is not in
# the repo, so its order is unknown — auto-detect fixes a crossed/straight cable either way.
PAD_ORDER = "pad 1 5V · pad 2 GND · pad 3 TX · pad 4 RX"


def unlocks(key):
    return [t["id"] for t in REGISTRY if any(key in alt.split("|") for alt in links_of(t))]


def plan(bench):
    """One row per WCB port: which probe header to wire, how, and what that wire unlocks."""
    taps = {(t["wcb"], t["port"]): t for t in bench.cfg.get("taps", [])}
    devices = bench.cfg.get("port_devices", {})
    probe_for = bench.cfg.get("wiring_plan", {})
    probes = bench.probe_names()
    used = {(l.probe_name, l.header) for l in bench.links.all()}
    rows = []
    for wcb in bench.wcb_numbers():
        planned_probe = probe_for.get(str(wcb)) or (probes[0] if probes else None)
        for port in HEADERS:
            key = f"W{wcb}{port}"
            tap = taps.get((wcb, port))
            dev = devices.get(key)
            what = describe_device(dev) if dev else ""
            link = bench.links.get(wcb, port, raw=True)
            wants_probe = link is not None or dev is None or tap is not None
            if link:
                probe, header = link.probe_name, link.header
            elif wants_probe:
                probe, header = planned_probe, port
                if (probe, header) in used:
                    order = [planned_probe] + [p for p in probes if p != planned_probe]
                    free = next(((p, h) for p in order for h in HEADERS if (p, h) not in used), None)
                    probe, header = free if free else (None, None)
                if probe:
                    used.add((probe, header))
            else:
                probe, header = None, None
            if not wants_probe:
                how = (f"{what} is wired to WCB{wcb} {port}. The tool never makes this port transmit, auto-detect "
                       f"leaves it alone, and tests that need a probe here skip and say so. To watch the device's "
                       f"traffic too, click 'Listen-only tap on/off' and wire a free probe header's RX pin to the "
                       f"WCB{wcb} {port} TX line, plus GND.")
            elif probe is None:
                how = "Every probe header is already wired — free one up, or add another probe."
            elif tap or dev:
                why = f"{what} is on this line." if dev else tap.get("why", "")
                lead = "Optional listen-only tap" if dev and not link else "Listen-only tap"
                how = (f"{lead}. {probe} header {header}: GND to WCB{wcb} GND, and the WCB{wcb} {port} TX "
                       f"line to the probe's RX pin, alongside the device already on that line. Leave the probe's "
                       f"TX and 5V unconnected. {why}".strip())
            if dev and bench.links.device_only(wcb, port):
                how += (" Tests never use a wire on this port: bench.json has no port_stimulus for the device, so "
                        "nothing may be sent there.")
            else:
                how = (f"{probe} header {header} to WCB{wcb} {port}: GND to GND, WCB TX to probe RX, WCB RX to probe TX. "
                       f"Leave 5V unconnected (both boards have their own supply). A straight-through cable is "
                       f"fine — auto-detect notices.")
            if dev and bench.links.device_only(wcb, port):
                status = "device"   # a wire here is never used by tests, so it must not read as green "connected"
            elif link and link.verified:
                status = "connected"
            elif link:
                status = "found, not verified"
            elif dev:
                status = "device"
            else:
                status = "to do"
            rows.append(dict(key=key, wcb=wcb, port=port, probe=probe, header=header, tap=bool(tap or dev), device=what,
                             how=how, status=status, actual=repr(link) if link else "", unlocks=unlocks(key)))
    return rows
