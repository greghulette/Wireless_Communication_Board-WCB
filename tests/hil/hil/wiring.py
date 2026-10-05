"""What to wire: the recommended plan from bench.json, checked against the wires actually found.

Any probe header can be wired to any WCB port, on any WCB — one probe can cover both boards. The
plan only suggests: header S<n> of the WCB's planned probe for port S<n>. When that header is
already taken by a wire you made elsewhere, the suggestion moves to the next free header (that
probe first, then any probe not reserved for NaviCore), so the list always follows what is really on the bench.

A port with a real device on it (bench.json "port_devices") gets no probe suggestion unless it is
also marked as a listen-only tap: the device owns that line.

bench.json "wiring_plan" maps a WCB number to its probe, and "navicore" to the probe on NaviCore's own pins
(D-NC37, docs/HIL_TESTING.md §2). A WCB or probe it names that is not on the bench yet is drawn and listed as
planned, so the wiring can be done before the board is plugged in; nothing else (discovery, tests) sees it.
"""
import os
import re

from .links import NAVICORE_ID, NC_TAPS, nc_key
from .probe import HEADERS
from .runner import REGISTRY, describe_device, links_of

# Header pads 1-4 on V2.4 and V3.2 boards, from the KiCad PCB files. The HW 1.0 carrier is not in
# the repo, so its order is unknown — auto-detect fixes a crossed/straight cable either way.
PAD_ORDER = "pad 1 5V · pad 2 GND · pad 3 TX · pad 4 RX"
NC_PAD_ORDER = ("NaviCore J3-J6: GND, 5V, RX, TX · probe header: 5V, GND, TX, RX — a straight 4-pin cable shorts "
                "5V to GND, so wire one lead at a time")

# D-NC37: the probe header each NaviCore pin goes to, and what the pin is (NaviCore.ino:170-187, its v2 profile).
NC_PLAN = (
    ("SBO", "S1", "SBUS OUT (J2, GPIO5)"),
    ("S3", "S2", "S3 'Serial 1' (J4: TX GPIO8, RX GPIO9)"),
    ("S4", "S3", "S4 'Serial 2' (J5: TX GPIO10, RX GPIO21)"),
    ("S5", "S4", "S5 'Serial 3' (J6: TX GPIO38, RX GPIO47)"),
    ("MAE", "S5", "the local Maestro bus (J3, NaviCore's TX on GPIO6)"),
)
NC_LABEL = {"S3": "S3  J4", "S4": "S4  J5", "S5": "S5  J6", "MAE": "Maestro  J3", "SBO": "SBUS OUT  J2"}


def unlocks(key):
    return [t["id"] for t in REGISTRY if any(key in alt.split("|") for alt in links_of(t))]


def planned_wcbs(bench):
    """WCB numbers the wiring plan names that are not on the bench yet."""
    have = set(bench.wcb_numbers())
    return sorted(int(k) for k in bench.cfg.get("wiring_plan", {}) if str(k).isdigit() and int(k) not in have)


def planned_probes(bench):
    """Probes the wiring plan names that bench.json has no device for yet."""
    have = set(bench.probe_names())
    return sorted({p for p in bench.cfg.get("wiring_plan", {}).values() if p and p not in have})


def navicore_probe(bench):
    return bench.cfg.get("wiring_plan", {}).get("navicore")


def plan(bench):
    """One row per WCB port: which probe header to wire, how, and what that wire unlocks."""
    taps = {(t["wcb"], t["port"]): t for t in bench.cfg.get("taps", [])}
    devices = bench.cfg.get("port_devices", {})
    probe_for = bench.cfg.get("wiring_plan", {})
    probes = bench.probe_names() + planned_probes(bench)
    reserved = navicore_probe(bench)
    # A header wired to one of NaviCore's own pins (hil/links.py NcLink) is taken too; nc_plan() lists those.
    used = {(l.probe_name, l.header) for l in bench.links.all() + bench.links.nc_all()}
    later = set(planned_wcbs(bench))
    rows = []
    for wcb in bench.wcb_numbers() + sorted(later):
        planned_probe = probe_for.get(str(wcb)) or next((p for p in probes if p != reserved), None)
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
                    order = [planned_probe] + [p for p in probes if p not in (planned_probe, reserved)]
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
            if wcb in later:
                how = (f"WCB{wcb} is planned (bench.json wiring_plan) but not plugged in yet: wire it now, then plug "
                       f"it in, click Find devices, and Auto-detect wires. " + how)
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
                             how=how, status=status, actual=repr(link) if link else "", unlocks=unlocks(key),
                             link=link, nc=False, planned=wcb in later))
    return rows


def nc_plan(bench):
    """One row per NaviCore pin, once bench.json has NaviCore and either its wiring plan names a probe for it or a
    NaviCore wire was found. Keys carry the mesh id the wires were found under (N20S3, ...)."""
    found = {l.port: l for l in bench.links.nc_all()}
    planned = navicore_probe(bench)
    if not bench.has("navicore") or not (planned or found):
        return []
    node = next(iter(found.values())).wcb if found else NAVICORE_ID
    rows = []
    for port, header, what in NC_PLAN:
        link = found.get(port)
        key = nc_key(node, port)
        probe, hdr = (link.probe_name, link.header) if link else (planned, header)
        tap = port in NC_TAPS
        if tap:
            how = (f"Listen-only tap. {probe} header {hdr}: GND to NaviCore GND, and {what} to the probe's RX pin, "
                   f"beside whatever is already on that line. Leave the probe's TX and 5V unconnected.")
        else:
            how = (f"{probe} header {hdr} to NaviCore {what}: GND to GND, NaviCore TX to the probe's RX, NaviCore RX "
                   f"to the probe's TX (crossed or not: auto-detect notices). Leave 5V unconnected: the probe runs on "
                   f"its USB.")
        how += (" NaviCore's connectors run GND, 5V, RX, TX and the probe's headers 5V, GND, TX, RX, so a straight "
                "4-pin cable shorts 5V to GND: wire one lead at a time. Auto-detect wires finds and confirms these "
                "(NaviCore sends the stimulus); there is no Verify for them here.")
        status = "connected" if link and link.verified else "found, not verified" if link else "to do"
        rows.append(dict(key=key, wcb=None, port=port, probe=probe, header=hdr, tap=tap, device="", how=how,
                         status=status, actual=repr(link) if link else "", unlocks=unlocks(key), link=link, nc=True,
                         planned=False))
    return rows


# ------------------------------------------------------------------ device wiring (a guide, not probed)
# bench.json "device_links": the wires made by hand between devices - the SBUS controller's outputs, the Kyber, the
# Maestros - which no probe sees, so auto-detect cannot find them. Each names its two ends ("W3S2", "navicore:SBUS_IN",
# "kyber:MAESTRO"; "fixtures" titles and labels the pins of anything that is not a WCB port), the signal, how to wire
# it, what to check when it misbehaves, and the tests that prove it ("proof"). Its status is the newest result of
# those tests in results/*/report.md: the Wiring tab is the bench's troubleshooting guide for these wires.
DEVICE_STATUS = ("connected", "failed", "not checked", "planned")


def last_results(results_root, ids, limit=80):
    """{test id: (PASS|FAIL|ERROR, run folder)} - the newest real result of each id in the last `limit` runs' reports.
    A SKIP proves nothing either way, so it is passed over."""
    want, found = set(ids), {}
    if not want or not results_root or not os.path.isdir(results_root):
        return found
    runs = sorted((d for d in os.listdir(results_root) if d[:1].isdigit()), reverse=True)[:limit]
    for d in runs:
        try:
            with open(os.path.join(results_root, d, "report.md"), encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        for m in re.finditer(r"^\| (PASS|FAIL|ERROR) \| ([\w.]+) \|", text, re.M):
            if m.group(2) in want and m.group(2) not in found:
                found[m.group(2)] = (m.group(1), d)
        if len(found) == len(want):
            break
    return found


def endpoint(ep):
    """("wcb", n, port) for "W3S2"; (owner, None, pin) for "navicore:SBUS_IN", "sbus:S5", "kyber:MAESTRO"."""
    m = re.match(r"^W(\d+)(S[1-5])$", ep)
    if m:
        return "wcb", int(m.group(1)), m.group(2)
    owner, _, pin = ep.partition(":")
    return owner, None, pin


def endpoint_label(bench, ep):
    kind, n, pin = endpoint(ep)
    if kind == "wcb":
        return f"WCB{n} {pin}"
    fx = bench.cfg.get("fixtures", {}).get(kind, {})
    title = fx.get("title") or {"navicore": "NaviCore", "sbus": "SBUS controller"}.get(kind, kind)
    return f"{title} {fx.get('pins', {}).get(pin, pin.replace('_', ' '))}"


def device_plan(bench):
    """One row per bench.json device_links entry, for the Wiring tab's diagram and list."""
    links = bench.cfg.get("device_links", [])
    proofs = {p for l in links for p in l.get("proof", [])}
    found = last_results(getattr(bench, "results_root", None), proofs)
    later = set(planned_wcbs(bench))
    rows = []
    for l in links:
        key = f"{l['from']}>{l['to']}"
        ends = [endpoint(l["from"]), endpoint(l["to"])]
        planned = any((k == "wcb" and n in later) or (k in ("navicore", "sbus") and not bench.has(k))
                      for k, n, _ in ends)
        results = sorted(((found[p][1], p, found[p][0]) for p in l.get("proof", []) if p in found), reverse=True)
        if planned:
            status = "planned"
        elif results:
            status = "connected" if results[0][2] == "PASS" else "failed"
        else:
            status = "not checked"
        rows.append(dict(key=key, wcb=None, port=None, probe=None, header=None, tap=False, device="", link=None,
                         nc=False, dev=True, planned=planned, status=status, unlocks=list(l.get("proof", [])),
                         kind=l.get("kind", "serial"), label=l.get("label", key), a=l["from"], b=l["to"],
                         a_label=endpoint_label(bench, l["from"]), b_label=endpoint_label(bench, l["to"]),
                         signal=l.get("signal", ""), how=l.get("how", ""), check=l.get("check", ""),
                         results=[(p, s, d) for d, p, s in results], actual=""))
    return rows
