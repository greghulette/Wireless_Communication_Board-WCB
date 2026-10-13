"""discover.identify_serial against a fake serial port, under Intellex's venv. No port is opened: pyserial's Serial is
replaced, in this process only, by a class that answers the PING from a script.

group "direct_pong_should" (intellex.identify_direct_pong, a (should) test for plan finding 3): identify_serial's own
docstring says ONLY A DIRECT PONG COUNTS (Intellex src/discover.py:154-158) - a USB-tethered doorway prints the mesh's
JSON, a relayed PONG included - and probe() tells the two apart (:351-363), but identify_serial takes any line whose type
is PONG (:221-222). Low impact while the launcher asks only Espressif native-USB ports (launcher.html:805), which a WCB
doorway is not. Controls that pass today: a direct PONG identifies the port, a busy port reads as busy, and a port
outside INTELLEX_SERIAL_ALLOW is never opened (H2, discover.py:167).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ixpy  # noqa: E402
from _ixpy import case, check  # noqa: E402


class _FakeSerial:
    """What identify_serial uses of serial.Serial: set lines and settings, open, write, read, flush, close. The reply to
    the first write comes from `reply`; `fail_open` raises from open()."""
    reply = b""
    fail_open = None
    opened = []

    def __init__(self, *a, **k):
        self.port = self.baudrate = self.timeout = None
        self.dtr = self.rts = True
        self._out = b""

    def open(self):
        _FakeSerial.opened.append(self.port)
        if _FakeSerial.fail_open:
            raise _FakeSerial.fail_open

    def write(self, data):
        if b'"PING"' in data:
            self._out += _FakeSerial.reply
        return len(data)

    def read(self, n):
        out, self._out = self._out[:n], self._out[n:]
        return out

    def reset_input_buffer(self):
        pass

    def flush(self):
        pass

    def close(self):
        pass


def _identify(port, reply=b"", fail_open=None):
    import serial
    import discover
    real = serial.Serial
    _FakeSerial.reply, _FakeSerial.fail_open, _FakeSerial.opened = reply, fail_open, []
    serial.Serial = _FakeSerial
    try:
        return discover.identify_serial(port, timeout=0.6), list(_FakeSerial.opened)
    finally:
        serial.Serial = real


@case("control: a direct PONG on the port identifies a NaviCore", group="direct_pong_should")
def direct(ctx):
    os.environ["INTELLEX_SERIAL_ALLOW"] = "COMFAKE"
    got, opened = _identify("COMFAKE", b'{"type":"PONG","version":"v0.2.0_hil"}\n')
    check(opened == ["COMFAKE"], f"opened {opened}")
    check(got == {"version": "v0.2.0_hil", "busy": False}, f"a direct PONG: {got}")


@case("(should) a PONG relayed over the mesh (it carries sys and id) does not identify the port as a NaviCore",
      group="direct_pong_should")
def relayed(ctx):
    os.environ["INTELLEX_SERIAL_ALLOW"] = "COMFAKE"
    got, _ = _identify("COMFAKE", b'{"sys":1,"type":"rc_hb","id":20,"fw":"v0.2.0_hil"}\n'
                                  b'{"sys":1,"type":"PONG","id":20,"version":"v0.2.0_hil"}\n')
    check(got.get("version") is None, f"a mesh-relayed PONG was taken for a NaviCore on the port: {got} "
                                      f"(discover.py:221-222 accepts any PONG; its docstring, :154-158, says only a "
                                      f"direct one counts, as probe() does, :351-363)")


@case("a port another program holds reads as busy; one outside INTELLEX_SERIAL_ALLOW is never opened",
      group="direct_pong_should")
def busy_and_leash(ctx):
    os.environ["INTELLEX_SERIAL_ALLOW"] = "COMFAKE"
    got, _ = _identify("COMFAKE", fail_open=PermissionError(13, "Access is denied."))
    check(got == {"version": None, "busy": True}, f"a port held elsewhere: {got}")
    got, opened = _identify("COMOTHER", b'{"type":"PONG","version":"v0"}\n')
    check(opened == [] and got == {"version": None, "busy": False},
          f"a port outside INTELLEX_SERIAL_ALLOW: opened {opened}, answered {got}")


# ------------------------------------------------------------------ group "bounce_macos"
# discover._wifi_bounce_macos (Intellex src/discover.py:641-771) with proc.run answering every command from a script:
# nothing runs, no adapter is touched. Each case is a Mac: what `route -n get`, `ifconfig`, `networksetup
# -listallhardwareports`, `-getairportnetwork`, `-getairportpower` and `-setairportpower` would say.
ROUTE_DEFAULT = "   route to: 192.168.4.1\ndestination: default\n       mask: default\n    gateway: 10.0.0.1\n  interface: en0\n"


def _route(iface):
    return f"   route to: 192.168.4.1\ndestination: 192.168.4.0\n       mask: 255.255.255.0\n  interface: {iface}\n"


def _ifconfig(iface, ip):
    return f"{iface}: flags=8863<UP,BROADCAST,RUNNING> mtu 1500\n\tinet {ip} netmask 0xffffff00 broadcast x\n"


HW_PORTS = ("\nHardware Port: Wi-Fi\nDevice: en0\nEthernet Address: aa\n\nHardware Port: 802.11ac NIC\nDevice: en6\n"
            "Ethernet Address: bb\n\nHardware Port: Thunderbolt Bridge\nDevice: bridge0\n")


class _Mac:
    """proc.run for the bounce: {argv tuple: (returncode, stdout, stderr)}; every call recorded in order."""

    def __init__(self, answers, raise_for=None):
        self.answers, self.calls, self.raise_for = answers, [], raise_for

    def run(self, args, **_k):
        import subprocess
        self.calls.append(tuple(args))
        if self.raise_for and self.raise_for(tuple(args)):
            raise FileNotFoundError(args[0])
        rc, out, err = self.answers.get(tuple(args), (1, "", f"HIL: no script for {' '.join(args)}"))
        return subprocess.CompletedProcess(args, rc, stdout=out, stderr=err)

    def touched(self):
        """Every networksetup call that changes something."""
        return [c for c in self.calls if c[0] == "networksetup" and any(
            x in c for x in ("-setairportpower", "-setnetworkserviceenabled", "-setairportnetwork"))]


def _bounce(mac, ssid="NaviCore", ip="192.168.4.1"):
    import discover
    import proc
    real = proc.run
    proc.run = mac.run
    try:
        return discover._wifi_bounce_macos(ssid, ip)
    finally:
        proc.run = real


@case("the routing table names the carrying interface: a built-in Wi-Fi port is power-cycled off then on; one macOS "
      "does not count as Wi-Fi is declined with nothing touched; a failing power-on is reported", group="bounce_macos")
def bounce_by_route(ctx):
    problems = []
    route = ("route", "-n", "get", "192.168.4.1")
    base = {route: (0, _route("en1"), ""), ("ifconfig", "en1"): (0, _ifconfig("en1", "192.168.4.2"), "")}
    mac = _Mac({**base, **{("networksetup", "-getairportpower", "en1"): (0, "Wi-Fi Power (en1): On", ""),
                             ("networksetup", "-setairportpower", "en1", "off"): (0, "", ""),
                             ("networksetup", "-setairportpower", "en1", "on"): (0, "", "")}})
    ok, msg = _bounce(mac)
    if (ok, msg) != (True, "en1 (carrying 192.168.4.1): power-cycled, reconnecting"):
        problems.append(f"a Wi-Fi port carrying the droid: {(ok, msg)}")
    if mac.touched() != [("networksetup", "-setairportpower", "en1", "off"),
                         ("networksetup", "-setairportpower", "en1", "on")]:
        problems.append(f"a Wi-Fi port carrying the droid: changed {mac.touched()}, expected en1 off then on")
    if ("networksetup", "-listallhardwareports") in mac.calls:
        problems.append("the SSID scan ran although the routing table had answered")

    mac = _Mac({**base, **{route: (0, _route("en6"), ""), ("ifconfig", "en6"): (0, _ifconfig("en6", "192.168.4.3"), ""),
                             ("networksetup", "-getairportpower", "en6"): (
                                 10, "", "en6 is not a Wi-Fi interface.\n** Error: Error obtaining wireless information.")}})
    ok, msg = _bounce(mac)
    if ok or not msg.startswith("en6 (carrying 192.168.4.1) is an adapter macOS does not count as Wi-Fi"):
        problems.append(f"the Realtek on en6: {(ok, msg[:90])}, expected declined")
    if mac.touched():
        problems.append(f"the Realtek on en6 was touched: {mac.touched()} (cycling its service froze a Mac, 2026-10-07)")

    mac = _Mac({**base, **{("networksetup", "-getairportpower", "en1"): (0, "Wi-Fi Power (en1): On", ""),
                             ("networksetup", "-setairportpower", "en1", "off"): (0, "", ""),
                             ("networksetup", "-setairportpower", "en1", "on"): (4, "", "HIL: could not power on en1\n")}})
    ok, msg = _bounce(mac)
    if (ok, msg) != (False, "HIL: could not power on en1"):
        problems.append(f"a power-on that fails: {(ok, msg)}, expected (False, its stderr)")
    check(not problems, "; ".join(problems))


@case("the default route is never the droid's: with only a default route, or a route whose interface holds no address "
      "on the droid's /24, the routing check declines and the SSID scan decides - by name, never by substring",
      group="bounce_macos")
def bounce_default_route(ctx):
    problems = []
    route = ("route", "-n", "get", "192.168.4.1")
    hw = ("networksetup", "-listallhardwareports")
    net0 = ("networksetup", "-getairportnetwork", "en0")
    for label, answers, ssid_reply in (
            ("only a default route", {route: (0, ROUTE_DEFAULT, "")}, "Current Wi-Fi Network: HomeNet\n"),
            ("a default route through an adapter that even holds a droid-subnet address",
             {route: (0, ROUTE_DEFAULT, ""), ("ifconfig", "en0"): (0, _ifconfig("en0", "192.168.4.5"), "")},
             "Current Wi-Fi Network: HomeNet\n"),
            ("a host route on an adapter off the droid's /24",
             {route: (0, _route("en0"), ""), ("ifconfig", "en0"): (0, _ifconfig("en0", "10.0.0.5"), "")},
             "Current Wi-Fi Network: HomeNet\n"),
            ("a house network merely containing the name", {route: (0, ROUTE_DEFAULT, "")},
             "Current Wi-Fi Network: NaviCore_Guest\n"),
            ("macOS hiding the SSID", {route: (0, ROUTE_DEFAULT, "")},
             "You are not associated with an AirPort network.\n")):
        mac = _Mac({**answers, **{hw: (0, HW_PORTS, ""), net0: (0, ssid_reply, "")}})
        ok, msg = _bounce(mac)
        if ok or not msg.startswith('nothing is carrying 192.168.4.1, and no Wi-Fi interface reports being on "NaviCore"'):
            problems.append(f"{label}: {(ok, msg[:80])}, expected the 'nothing is carrying' refusal")
        if mac.touched():
            problems.append(f"{label}: changed {mac.touched()} - the user's own network")
        if hw not in mac.calls:
            problems.append(f"{label}: the SSID scan never ran")
        if ("networksetup", "-getairportnetwork", "en6") in mac.calls:
            problems.append(f"{label}: the scan asked en6 ('802.11ac NIC' is not a Wi-Fi hardware port)")

    # The scan finds the droid by its default-named network ('NaviCore-20' is 'NaviCore'): that port is cycled.
    mac = _Mac({route: (0, ROUTE_DEFAULT, ""), hw: (0, HW_PORTS, ""), net0: (0, "Current Wi-Fi Network: NaviCore-20\n", ""),
                ("networksetup", "-getairportpower", "en0"): (0, "Wi-Fi Power (en0): On", ""),
                ("networksetup", "-setairportpower", "en0", "off"): (0, "", ""),
                ("networksetup", "-setairportpower", "en0", "on"): (0, "", "")})
    ok, msg = _bounce(mac)
    if (ok, msg) != (True, 'en0 (associated with "NaviCore"): power-cycled, reconnecting'):
        problems.append(f"the SSID scan finding NaviCore-20 on en0: {(ok, msg)}")
    mac = _Mac({}, raise_for=lambda a: a[0] == "route")
    if _bounce(mac) != (False, "networksetup not found"):
        problems.append("a Mac with no route/networksetup binaries did not say 'networksetup not found'")
    check(not problems, "; ".join(problems))



# ------------------------------------------------------------------ group "scan_rows" (intellex.discover_scan_unit)
NC_HINT_INFO = {"kind": "navicore", "version": "v0.2.0_hilscan", "relayId": None, "alias": None, "peers": []}


@case("discover._hint: a NaviCore by its version; a relay or a WCB doorway by alias (or its noun), its number and up "
      "to three boards it sees ('no boards seen yet' with none), a WCB alias naming WCB not repeated; something else "
      "on port 80; routed with no TCP; no route", group="scan_rows")
def hints(ctx):
    import discover
    peers = [{"alias": "Body", "fw": "6.2.1"}, {"alias": "Dome"}, {"alias": ""}, {"alias": "Legs", "fw": "6.2.0"},
             {"alias": "Fourth", "fw": "1"}]
    problems = []
    for label, via, reach, info, want in (
            ("navicore", "192.168.4.2", True, NC_HINT_INFO, "NaviCore v0.2.0_hilscan"),
            ("relay", "192.168.4.2", True, {"kind": "relay", "alias": "Mgmt Relay", "relayId": "19", "peers": peers},
             "Mgmt Relay (WCB #19) \u2014 sees Body 6.2.1, Dome, Legs 6.2.0"),
            ("relay, no alias, no peers", "192.168.4.2", True, {"kind": "relay", "alias": None, "relayId": None,
                                                                "peers": []},
             "mgmt relay \u2014 no boards seen yet"),
            ("wcb alias naming WCB", "192.168.4.2", True, {"kind": "wcb", "alias": "Body WCB", "relayId": "1",
                                                           "peers": []},
             "Body WCB (#1) \u2014 no boards seen yet"),
            ("wcb, no alias", "192.168.4.2", True, {"kind": "wcb", "alias": None, "relayId": "2",
                                                    "peers": [{"alias": "Dome", "fw": "6.2.1"}]},
             "WCB (#2) \u2014 sees Dome 6.2.1"),
            ("wcb alias without WCB", "192.168.4.2", True, {"kind": "wcb", "alias": "Vader", "relayId": "21",
                                                            "peers": []},
             "Vader (WCB #21) \u2014 no boards seen yet"),
            ("something else on 80", "192.168.4.2", True, discover._unknown(),
             "something answers on port 80, but it is not a NaviCore, a WCB or a management relay"),
            ("routed, no TCP", "192.168.4.2", False, discover._unknown(),
             "routed via 192.168.4.2 but no TCP \u2014 stale association or the AP is down. Toggle that adapter off/on, "
             "or check the board's serial log for '[WS] command endpoint ready'."),
            ("no route", None, False, discover._unknown(),
             "no route \u2014 this machine is not joined to the droid's WiFi")):
        got = discover._hint(via, reach, info)
        if got != want:
            problems.append(f"{label}: {got!r}, expected {want!r}")
    check(not problems, "; ".join(problems))


@case("discover.scan(): per host via, routable, reachable (TCP 80 asked only when routable, the probe only when "
      "reachable), kind and its flags (isMesh for a relay or a WCB), version, relayId, alias, peers and the hint; the "
      "hosts are INTELLEX_DISCOVER_HOSTS when set (empty probes nothing) and an explicit list wins", group="scan_rows")
def scan_rows(ctx):
    import discover
    asked = {"route": [], "tcp": [], "probe": []}
    route = {"nc": "192.168.4.2", "relay": "192.168.4.2", "wcb": "192.168.4.3", "dead": "192.168.4.2", "far": None}
    probes = {
        "nc": dict(NC_HINT_INFO),
        "relay": {"kind": "relay", "version": None, "relayId": "19", "alias": "Mgmt Relay",
                  "peers": [{"alias": "Body", "fw": "6.2.1"}]},
        "wcb": {"kind": "wcb", "version": None, "relayId": "1", "alias": "Body WCB", "peers": []},
    }
    real = discover.local_ip_for, discover.tcp_open, discover.probe
    discover.local_ip_for = lambda h: asked["route"].append(h) or route.get(h)
    discover.tcp_open = lambda h, *a, **k: asked["tcp"].append(h) or h in probes
    discover.probe = lambda h, *a, **k: asked["probe"].append(h) or dict(probes[h])
    saved = os.environ.get("INTELLEX_DISCOVER_HOSTS")
    problems = []
    try:
        rows = {r["host"]: r for r in discover.scan(["nc", "relay", "wcb", "dead", "far"])}
        if list(rows) != ["nc", "relay", "wcb", "dead", "far"]:
            problems.append(f"scan rows for {list(rows)}, expected the five hosts in order")
        if asked["tcp"] != ["nc", "relay", "wcb", "dead"] or asked["probe"] != ["nc", "relay", "wcb"]:
            problems.append(f"TCP asked of {asked['tcp']}, probe of {asked['probe']}: TCP only where a route exists, "
                            f"the probe only where TCP answered")
        flags = ("via", "routable", "reachable", "kind", "isNaviCore", "isRelay", "isWcb", "isMesh", "version",
                 "relayId", "alias", "peers")
        want = {
            "nc": ("192.168.4.2", True, True, "navicore", True, False, False, False, "v0.2.0_hilscan", None, None, []),
            "relay": ("192.168.4.2", True, True, "relay", False, True, False, True, None, "19", "Mgmt Relay",
                      [{"alias": "Body", "fw": "6.2.1"}]),
            "wcb": ("192.168.4.3", True, True, "wcb", False, False, True, True, None, "1", "Body WCB", []),
            "dead": ("192.168.4.2", True, False, "unknown", False, False, False, False, None, None, None, []),
            "far": (None, False, False, "unknown", False, False, False, False, None, None, None, []),
        }
        for h, w in want.items():
            r = rows.get(h, {})
            got = tuple(r.get(k) for k in flags)
            if got != w:
                problems.append(f"{h}: {dict(zip(flags, got))}, expected {dict(zip(flags, w))}")
            info = probes.get(h) or discover._unknown()
            if r.get("hint") != discover._hint(route.get(h), h in probes, info):
                problems.append(f"{h}: hint {r.get('hint')!r} is not _hint's")
        if rows.get("relay", {}).get("hint") != "Mgmt Relay (WCB #19) \u2014 sees Body 6.2.1":
            problems.append(f"the relay's hint: {rows.get('relay', {}).get('hint')!r}")
        for env, cands, expect in (("hA, hB ,", None, ["hA", "hB"]), ("", None, []), ("hA", ["hC"], ["hC"])):
            os.environ["INTELLEX_DISCOVER_HOSTS"] = env
            del asked["route"][:]
            out = discover.scan(cands)
            if asked["route"] != expect or [r["host"] for r in out] != expect:
                problems.append(f"INTELLEX_DISCOVER_HOSTS={env!r}, scan({cands}): looked at {asked['route']}, rows "
                                f"{[r['host'] for r in out]}, expected {expect}")
        os.environ.pop("INTELLEX_DISCOVER_HOSTS", None)
        del asked["route"][:]
        discover.scan()
        if asked["route"] != list(discover.DEFAULT_CANDIDATES):
            problems.append(f"with INTELLEX_DISCOVER_HOSTS unset scan() looked at {asked['route']}, expected "
                            f"DEFAULT_CANDIDATES {list(discover.DEFAULT_CANDIDATES)}")
    finally:
        discover.local_ip_for, discover.tcp_open, discover.probe = real
        if saved is None:
            os.environ.pop("INTELLEX_DISCOVER_HOSTS", None)
        else:
            os.environ["INTELLEX_DISCOVER_HOSTS"] = saved
    check(not problems, "; ".join(problems))


if __name__ == "__main__":
    sys.exit(_ixpy.main())
