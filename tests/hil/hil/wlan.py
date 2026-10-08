"""The PC's own WiFi, for the tests that put it on a board's access point: W1's or W2's (suites/s28_wifi.py, opt-in
wifi_pc) and NaviCore's (suites/s45_navicore_wifi.py, opt-in navicore_wifi). docs/hil_plan/NAVICORE.md INF5: one tested
copy, moved out of s28 unchanged. On Windows netsh drives the adapter and PowerShell reads its addresses and routes.
Anywhere else (the Mac) the harness joins nothing: it uses a spare adapter already on the access point and only reads
ifconfig, route and netstat (_prejoined below).

The rules every caller keeps (s28's module docstring, docs/HIL_WEEK_DECISIONS.md D63):
- A network's name is compared, never quoted. pc_on_ap's notes say only whether the adapter was on a network, and
  scrub() takes every network name out of netsh's replies before they are noted.
- The access point's password is read from the board at run time and handed to Windows only, in a temporary profile
  named HIL-<ssid> on the chosen adapter, which pc_on_ap deletes on the way out. It is never noted or raised.
- Every netsh call names the adapter: with two adapters an unnamed `netsh wlan connect` is refused (run
  20260924-092602 failed that way, and reused the PC's own profile on the other adapter).
- A lease from an ESP32's access point has taken 1.8 to 46.7 s on this PC (tracker #110), so address_wait waits up to
  ADDR_WAIT_S and renews once (D61).
- 'Connected with a lease' does not prove the link carries anything. A board restart that deauthenticates nobody
  (NaviCore's REBOOT) leaves Windows' association in place while the restarted access point drops the station's
  frames: run 20260929-202852 sat like that for 90 s with no WLAN AutoConfig event at all. rejoin(reach=) proves a TCP
  connect and re-associates when there is none.
- Off Windows the harness joins nothing by default. The Mac bench's spare adapter is a USB TP-Link that macOS does not
  count as Wi-Fi, so no networksetup Wi-Fi verb reaches it; the user keeps it joined to NaviCore's access point, and the
  adapter carrying the default route (the user's internet) is never used (D-NC14). Every board's access point is
  192.168.4.1 and the SSID cannot be read there, so a caller proves whose access point it is (pc_on_ap identify=); one
  that cannot skips. With bench.json "wifi_switch": "realtek" (D82) pc_on_ap moves that adapter itself, through the
  Realtek utility's menu-bar menu - the only thing that drives it: the menu is opened by UI scripting and the network's
  item clicked with a real mouse event (realtek_pick), as the user would, then the caller's identify proves the board,
  and on the way out the adapter goes back to its resting network (home=, NaviCore's). It needs the Accessibility
  permission for VS Code, which runs the harness, and Python with pyobjc's Quartz (QUARTZ_PYTHON); it never picks the
  network the Mac's own Wi-Fi is on.

What only the bench shows: that netsh's English output has these field names (parse_interfaces, parse_networks), and
ifconfig's, route's and netstat's the layout parse_ifconfig, parse_route_get and parse_netstat_defaults read; the
selftest feeds each parser text captured in that form.
"""
import contextlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time

from .checkpoint import redact_text
from .runner import Skip

NOT_WINDOWS_SKIP = "netsh (Windows) drives the PC's WiFi here"
ON_WINDOWS = os.name == "nt"        # which path every function here takes; the selftest sets it to run both


def netsh(*args, timeout=30):
    r = subprocess.run(["netsh", "wlan", *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout)
    return (r.stdout or "") + (r.stderr or "")


def parse_interfaces(text):
    """`netsh wlan show interfaces` output -> every WiFi adapter as {'name', 'description', 'guid', 'state', 'ssid',
    'profile'}, each key present only when netsh printed it (a disconnected adapter has no SSID or profile)."""
    ifaces, cur = [], None
    for line in text.splitlines():
        m = re.match(r"^\s*(Name|Description|GUID|State|SSID|Profile)\s*:\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1).lower(), m.group(2).strip()
        if key == "name":
            cur = {"name": val}
            ifaces.append(cur)
        elif cur is not None and key not in cur:
            cur[key] = val
    return ifaces


def wlan_interfaces():
    """Every WiFi adapter as {'name', 'description', 'guid', 'state', 'ssid', 'profile'} (`netsh wlan show
    interfaces`)."""
    return parse_interfaces(netsh("show", "interfaces"))


def iface(name):
    return next((i for i in wlan_interfaces() if i["name"] == name), None)


def joined(name, ssid):
    i = iface(name) or {}
    return i.get("state", "").lower() == "connected" and i.get("ssid") == ssid


def internet_adapter():
    """The adapter carrying the PC's default route, or None."""
    if not ON_WINDOWS:
        return parse_route_get(_run(["route", "-n", "get", "default"]) or "")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "(Get-NetRoute -DestinationPrefix '0.0.0.0/0' | Sort-Object RouteMetric | "
                            "Select-Object -First 1).InterfaceAlias"],
                           capture_output=True, text=True, timeout=20)
        return r.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def choose_adapter(ifaces, want, inet):
    """(adapter, why) from the adapters `ifaces`: the one named `want` (bench.json "wifi_test_interface") when set;
    otherwise one that is not `inet`, the adapter carrying the PC's default route, so the PC stays online (this bench's
    TP-Link "Wi-Fi 2"); otherwise the only one, which takes the PC offline until the test puts it back."""
    if want:
        return next((i for i in ifaces if i["name"] == want), None), "bench.json wifi_test_interface"
    spare = [i for i in ifaces if i["name"] != inet]
    if spare:
        return spare[0], f"not the internet adapter ({inet or 'none found'})"
    return (ifaces[0], "the only WiFi adapter: the PC is offline until the test puts it back") if ifaces else (None, "")


def pick_adapter(bench):
    """(adapter, why): choose_adapter over this PC's adapters, bench.json "wifi_test_interface" and the adapter that
    carries the default route. Off Windows (where s45's _spare_adapter once ran netsh: FileNotFoundError, three ERRORs
    in run 20261005-221308) the spare adapter already on a 192.168.4.x network, as {'name', 'state'}, which the harness
    leaves where it is (_prejoined); Skip(PREJOIN_SKIP) when there is none."""
    if not ON_WINDOWS:
        spare = spare_leases()
        if spare:
            _remember_spare(spare[0][0])
            return {"name": spare[0][0], "state": "connected"}, ("off Windows: the spare adapter already on a "
                                                                 "192.168.4.x network, never the one carrying the "
                                                                 "default route")
        known = remembered_spare()
        if known is None or known == internet_adapter():
            raise Skip(PREJOIN_SKIP)
        return {"name": known, "state": "disconnected"}, ("off Windows: the spare adapter found before, with no "
                                                          "192.168.4.x address now (its access point restarting)")
    ifaces = wlan_interfaces()
    want = bench.cfg.get("wifi_test_interface")
    return choose_adapter(ifaces, want, None if want else internet_adapter())


def wait(pred, timeout, step=1.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


def profile_xml(name, ssid, pw):
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return ('<?xml version="1.0"?>\n<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">\n'
            f'  <name>{esc(name)}</name>\n  <SSIDConfig><SSID><name>{esc(ssid)}</name></SSID></SSIDConfig>\n'
            '  <connectionType>ESS</connectionType>\n  <connectionMode>manual</connectionMode>\n'
            '  <MSM><security>\n    <authEncryption><authentication>WPA2PSK</authentication><encryption>AES</encryption>'
            '<useOneX>false</useOneX></authEncryption>\n'
            f'    <sharedKey><keyType>passPhrase</keyType><protected>false</protected><keyMaterial>{esc(pw)}</keyMaterial></sharedKey>\n'
            '  </security></MSM>\n</WLANProfile>\n')


def scrub(text, *names):
    """`text` with each of `names` (an SSID, a profile named after one) replaced, for a note: netsh echoes profile
    names back, and a network's name is never quoted (the module docstring)."""
    for n in sorted((n for n in names if n), key=len, reverse=True):
        text = text.replace(n, "<network>")
    return text


def ps(command):
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True, text=True,
                           timeout=20)
        return r.stdout
    except (OSError, subprocess.SubprocessError):
        return None


def ipv4(name):
    """The adapter's IPv4 addresses."""
    if not ON_WINDOWS:
        return parse_ifconfig(_run(["ifconfig", name]) or "").get(name, [])
    esc = name.replace("'", "''")
    return (ps(f"Get-NetIPAddress -InterfaceAlias '{esc}' -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
               "ForEach-Object { $_.IPAddress }") or "").split()


ADDR_WAIT_S = 60          # a lease from a WCB's access point has taken 1.8 to 46.7 s here (tracker #110)
ADDR_RENEW_S = 20


def address_wait(name, subnet="192.168.4."):
    """Wait for the adapter to hold an address in `subnet` -> (the address or None, seconds, what happened). Windows
    gives itself a 169.254 address after about 6 s without a lease and then asks again only now and then, so one
    `ipconfig /renew` on this adapter alone is sent at ADDR_RENEW_S."""
    t0 = time.monotonic()
    renewed = ""
    while time.monotonic() - t0 < ADDR_WAIT_S:
        got = [a for a in ipv4(name) if a.startswith(subnet)]
        if got:
            return got[0], round(time.monotonic() - t0, 1), renewed
        if not renewed and time.monotonic() - t0 >= ADDR_RENEW_S:
            try:
                subprocess.run(["ipconfig", "/renew", name], capture_output=True, text=True, timeout=40)
            except (OSError, subprocess.SubprocessError):
                pass
            renewed = f"renewed at {ADDR_RENEW_S} s"
        time.sleep(1.0)
    return None, round(time.monotonic() - t0, 1), renewed


# The adapter that carries the PC's default route is only ever used by a caller that accepts taking the PC offline.
SPARE_ONLY_SKIP = ("the only WiFi adapter on this PC carries its default route, and this test runs only on a spare "
                   "one, so the PC stays online (docs/hil_plan/NAVICORE.md D-NC14)")


@contextlib.contextmanager
def pc_on_ap(bench, problems, ssid, pw, whose, spare_only=False, reach=None, identify=None, home=None):
    """The PC's chosen WiFi adapter on the access point `ssid` for the block, holding a 192.168.4.x lease from it ->
    the adapter's name; Skip where that cannot be done here. Off Windows: the spare adapter already on it, proved by
    `identify` (_prejoined), and nothing to undo. `whose` names the AP in messages ("W1's"); the SSID is
    never quoted. Windows side: a temporary profile named HIL-<ssid> on the chosen adapter only, never one of the
    PC's own profiles, and every netsh call names the adapter - with two adapters an unnamed `netsh wlan connect` is
    refused (run 20260924-092602 failed that way, and reused the PC's own WCB1 profile on the other adapter). The
    profile holds the AP's password from the board's chain and is deleted afterwards. netsh's replies are noted with
    every network name scrubbed; they carry no key. Off Windows with "wifi_switch": "realtek": realtek_on_ap, and
    home= says where the adapter goes back to. On the way out the adapter goes back to the network it was on, and
    failing to is added to `problems` (and noted, for when the block raised). No association in 30 s, or no lease in
    ADDR_WAIT_S, raises after the same cleanup. spare_only: Skip rather than take the adapter that carries the PC's
    default route (NaviCore's tests, D-NC14; s28's take it when it is the only one). reach ((host, port)): the lease
    must also carry a TCP connect there (_carries), as rejoin's must. In run 20261004-125412 three NaviCore joins held a
    lease and carried nothing (a connect to NaviCore timed out) while two others worked; in 20261004-130438, with this
    check, all fifteen carried a connect at once. The cause is not known; the check costs nothing when the link works."""
    if not ON_WINDOWS:
        if identify is not None and realtek_enabled(bench):
            with realtek_on_ap(bench, problems, ssid, whose, reach, identify, home) as name:
                yield name
            return
        yield _prejoined(bench, whose, reach, identify)      # the adapter stays where it was: nothing to put back
        return
    adapter, why = pick_adapter(bench)
    if not adapter:
        raise Skip("this PC has no WiFi adapter" if not why else "bench.json wifi_test_interface names no WiFi adapter here")
    name = adapter["name"]
    if spare_only and name == internet_adapter():
        raise Skip(SPARE_ONLY_SKIP)
    prev = adapter.get("profile") if adapter.get("state", "").lower() == "connected" else None
    tmp = f"HIL-{ssid}"
    hide = (tmp, ssid, prev, adapter.get("ssid"))
    bench.note(f"adapter {name} ({why}); it was {'on a network' if prev else 'on no network'}; a temporary profile "
               f"for {whose} access point")
    added = False
    fd, path = tempfile.mkstemp(prefix="wlan-", suffix=".xml")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(profile_xml(tmp, ssid, pw))
        out = netsh("add", "profile", f"filename={path}", f"interface={name}", "user=current")
        try:
            os.remove(path)
        except OSError:
            pass
        bench.note("netsh add profile: " + scrub(redact_text(" ".join(out.split())), *hide)[:200])
        added = "is added" in out
        if not added:
            raise AssertionError("netsh did not add the temporary profile")
        out = netsh("connect", f"name={tmp}", f"ssid={ssid}", f"interface={name}")
        bench.note("netsh connect: " + scrub(" ".join(out.split()), *hide)[:200])
        if not wait(lambda: joined(name, ssid), 30):
            now = iface(name) or {}
            on = whose if now.get("ssid") == ssid else ("another" if now.get("ssid") else "none")
            raise AssertionError(f"{name} did not associate with {whose} access point within 30 s "
                                 f"(state {now.get('state')!r}, network: {on})")
        addr, secs, renewed = address_wait(name)
        bench.note(f"{name}: " + (f"lease {addr} {secs} s after associating" if addr else f"no lease in {secs} s")
                   + (f" ({renewed})" if renewed else ""))
        if not addr:
            raise AssertionError(f"{name} associated with {whose} access point but got no 192.168.4.x lease in {secs} s "
                                 f"({renewed or 'no renew'}; it holds {ipv4(name) or 'no address'})")
        if reach is not None:
            _carries(bench, name, ssid, whose, addr, reach)
        yield name
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
        netsh("disconnect", f"interface={name}")
        if added:
            out = netsh("delete", "profile", f"name={tmp}", f"interface={name}")
            bench.note("netsh delete profile: " + scrub(" ".join(out.split()), *hide)[:200])
        if prev:
            netsh("connect", f"name={prev}", f"interface={name}")
            if not wait(lambda: (iface(name) or {}).get("state", "").lower() == "connected", 45):
                # This bench's spare adapter lives on NaviCore's access point, so the network it was on can be the one
                # under test - down on purpose when a test leaves the block with it off (ncwifi.refuse_short_password,
                # runs 20260929-114920 and -120247). Windows' own profile brings it back once that access point is up,
                # so that case is noted, not failed.
                msg = f"{name} did not reconnect to its previous network within 45 s"
                if adapter.get("ssid") == ssid:
                    bench.note(msg + f": it was on {whose} access point itself, which the test may have taken down; "
                                     "Windows reconnects it when that access point is back")
                else:
                    problems.append(msg + ": reconnect it by hand")
                    bench.note(problems[-1])


def _carries(bench, name, ssid, whose, addr, reach):
    """After pc_on_ap's lease: a TCP connect to `reach` within REACH_WAIT_S, or - once each - a fresh DHCP exchange on
    this adapter alone (ipconfig /release then /renew: a new lease, and the access point learns this station's address
    again), then a re-association of the temporary profile. Still nothing is noted, not raised: a test that expects the
    port closed decides for itself, and the others fail on their own first connect."""
    target = f"{reach[0]}:{reach[1]}"
    got = reach_wait(*reach)
    if got is not None:
        bench.note(f"{name}: {target} took a connect {got} s after the lease")
        return
    bench.note(f"{name} holds {addr} but {target} took no connect in {REACH_WAIT_S:.0f} s: the harness renews the lease")
    for args in (["ipconfig", "/release", name], ["ipconfig", "/renew", name]):
        try:
            subprocess.run(args, capture_output=True, text=True, timeout=40)
        except (OSError, subprocess.SubprocessError):
            pass
    addr2, secs, _ = address_wait(name)
    got = reach_wait(*reach) if addr2 else None
    if got is not None:
        bench.note(f"{name}: after the renew, lease {addr2} and {target} took a connect {got} s later")
        return
    bench.note(f"{name}: after the renew ({addr2 or 'no lease'} in {secs} s) {target} still took no connect: the "
               f"harness re-associates {name} once")
    netsh("disconnect", f"interface={name}")
    _connect_tmp(bench, name, ssid, whose, "re-associating a link that carried nothing")
    addr3, secs, _ = address_wait(name)
    got = reach_wait(*reach) if addr3 else None
    bench.note(f"{name}: re-associated, " + (f"lease {addr3} {secs} s after" if addr3 else f"no lease in {secs} s")
               + (f"; {target} took a connect {got} s later" if got is not None
                  else f"; {target} still took no connect in {REACH_WAIT_S:.0f} s"))


def default_routes():
    """The PC's 0.0.0.0/0 routes as sorted 'InterfaceAlias|NextHop' strings ('interface|gateway' off Windows,
    parse_netstat_defaults), or None."""
    if not ON_WINDOWS:
        out = _run(["netstat", "-rn", "-f", "inet"])
        return None if out is None else parse_netstat_defaults(out)
    out = ps("Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | "
             "ForEach-Object { $_.InterfaceAlias + '|' + $_.NextHop }")
    return None if out is None else sorted(x.strip() for x in out.splitlines() if x.strip())


REACH_WAIT_S = 10.0     # how long an adapter that reads connected, with a lease, may take to carry a TCP connect


def reach_wait(host, port=80, timeout=REACH_WAIT_S, step=1.0):
    """Seconds until a TCP connect to (host, port) succeeds (each try 1 s at most), or None after `timeout`: whether the
    link carries traffic at all, which an association and a lease do not prove (rejoin). Sends nothing but the
    handshake, as Intellex's own liveness probe (discover.tcp_open) does."""
    t0 = time.monotonic()
    while True:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return round(time.monotonic() - t0, 1)
        except OSError:
            pass
        if time.monotonic() - t0 >= timeout:
            return None
        time.sleep(step)


def _connect_tmp(bench, name, ssid, whose, why):
    """netsh connect of pc_on_ap's temporary profile on adapter `name`, then the association -> or AssertionError."""
    tmp = f"HIL-{ssid}"
    out = netsh("connect", f"name={tmp}", f"ssid={ssid}", f"interface={name}")
    bench.note("netsh connect: " + scrub(" ".join(out.split()), tmp, ssid)[:200])
    if not wait(lambda: joined(name, ssid), 30):
        now = iface(name) or {}
        raise AssertionError(f"{name} did not associate with {whose} access point within 30 s of the harness connecting "
                             f"its temporary profile ({why}; state {now.get('state')!r})")


def rejoin(bench, name, ssid, whose, wait_s=30.0, reach=None):
    """Adapter `name` back on `ssid` with a 192.168.4.x lease - and, given `reach` ((host, port)), carrying a TCP connect
    there - inside a pc_on_ap block whose access point went away and came back (the board hosting it restarted) -> what
    it took, for a note.

    Connected with a lease is not back. A restart that deauthenticates nobody (NaviCore's REBOOT: otaFarewellAP is empty
    on purpose, navicore_ota.h:184-204) leaves Windows' association in place: netsh reads connected, the old lease
    stays, and the restarted access point drops every frame from a station it no longer knows. On this bench's spare
    adapter under the temporary profile, run 20260929-202852 (intellex.wifi_link_loss) sat like that for 90 s with no
    WLAN AutoConfig event at all between the join and the harness's own disconnect. So with `reach`, a link that takes no
    connect in REACH_WAIT_S is re-associated - netsh disconnect, then a connect of the temporary profile, both naming the
    adapter (what Intellex's own bounce would do, which its --no-auto-bounce leash leaves to the harness) - and must
    then associate, take a lease and carry the connect. An adapter Windows did drop is connected again the same way
    after `wait_s` (the temporary profile is manual, so Windows would not). AssertionError when nothing brings it back;
    the network is never named. Off Windows the adapter must come back by itself (_back_on_its_own)."""
    if not ON_WINDOWS:
        return _back_on_its_own(name, whose, reach)
    if wait(lambda: joined(name, ssid), wait_s):
        how = [f"Windows reports {name} associated"]
    else:
        _connect_tmp(bench, name, ssid, whose, f"no association in {wait_s:.0f} s")
        how = [f"the harness connected {name}'s temporary profile again after {wait_s:.0f} s without an association"]
    addr, secs, renewed = address_wait(name)
    if not addr:
        raise AssertionError(f"{name} is back on {whose} access point but got no 192.168.4.x lease in {secs} s "
                             f"({renewed or 'no renew'})")
    how.append(f"lease {addr} {secs} s after associating" + (f" ({renewed})" if renewed else ""))
    if reach is None:
        return "; ".join(how)
    target = f"{reach[0]}:{reach[1]}"
    got = reach_wait(*reach)
    if got is not None:
        how.append(f"{target} took a connect {got} s later")
        return "; ".join(how)
    bench.note(f"{name} reads associated with lease {addr}, but {target} took no connect in {REACH_WAIT_S:.0f} s: a stale "
               f"association (the restart deauthenticated nobody); the harness re-associates {name}")
    netsh("disconnect", f"interface={name}")
    _connect_tmp(bench, name, ssid, whose, f"re-associating a stale association")
    addr, secs, renewed = address_wait(name)
    if not addr:
        raise AssertionError(f"{name} re-associated with {whose} access point but got no 192.168.4.x lease in {secs} s "
                             f"({renewed or 'no renew'})")
    got = reach_wait(*reach)
    if got is None:
        raise AssertionError(f"{name} re-associated with {whose} access point and holds {addr}, but {target} still took no "
                             f"connect in {REACH_WAIT_S:.0f} s")
    how.append(f"{target} took no connect in {REACH_WAIT_S:.0f} s (a stale association), so the harness re-associated "
               f"{name}: lease {addr} {secs} s after, and {target} took a connect {got} s later")
    return "; ".join(how)


# ------------------------------------------------------------------ off Windows: a spare adapter already joined
PREJOIN_WAIT_S = 90.0     # off Windows: how long a spare adapter may take to come back by itself (its board restarted)
PREJOIN_STEP_S = 1.0      # and how often it is looked at meanwhile
AP_REACH = ("192.168.4.1", 80)   # every board's access point here, and the endpoint it serves (WiFi.softAPIP() default)
PREJOIN_SKIP = ("off Windows the harness joins no network itself (netsh drives the PC's WiFi on Windows): it uses a "
                "spare WiFi adapter already on the access point, and no adapter here but the one carrying the default "
                "route holds a 192.168.4.x address - join a spare adapter to the access point with its own utility and "
                "leave it there")
_spare_seen = None        # the spare adapter last found off Windows, waited for when a restart took its address away
# ... and remembered for the next run on this computer (untracked, beside ports.json): a run whose first WiFi test
# came right after a NaviCore restart found no lease, knew no adapter, and skipped every test (run 20261006-172611).
SPARE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results", "wifi_spare.json")
# When a wait for the spare adapter ran out: it rejoins on its own schedule (the Mac bench's TP-Link: 5-25 s after a
# NaviCore restart, or many minutes; its menu-bar utility does the joining and cycling its network service does not
# bring it back, 2026-10-06), so later tests skip at once until it holds an address again, instead of waiting
# PREJOIN_WAIT_S each.
_spare_lost_at = None


def _remember_spare(name):
    global _spare_seen
    _spare_seen = name
    try:
        with open(SPARE_FILE, "w", encoding="utf-8") as f:
            json.dump({"adapter": name}, f)
    except OSError:
        pass


def remembered_spare():
    """The spare adapter found last, in this run or an earlier one on this computer, or None."""
    if _spare_seen:
        return _spare_seen
    try:
        with open(SPARE_FILE, encoding="utf-8") as f:
            name = json.load(f).get("adapter")
        return name if isinstance(name, str) and name else None
    except (OSError, ValueError, AttributeError):
        return None


def _run(args):
    """A read-only command's output (ifconfig, route, netstat), or None when it could not run."""
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def parse_ifconfig(text):
    """`ifconfig` output -> {interface: [its IPv4 addresses]}, every interface listed."""
    out, cur = {}, None
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z0-9.]+): flags=", line)
        if m:
            cur = out.setdefault(m.group(1), [])
            continue
        m = re.match(r"^\s+inet (\d+\.\d+\.\d+\.\d+)\s", line)
        if m and cur is not None:
            cur.append(m.group(1))
    return out


def parse_route_get(text):
    """`route -n get default` output -> the interface carrying the default route, or None (route answers 'not in
    table' when there is none)."""
    m = re.search(r"^\s*interface:\s*(\S+)\s*$", text, re.M)
    return m.group(1) if m else None


def parse_netstat_defaults(text):
    """`netstat -rn -f inet` output -> its default routes as sorted 'interface|gateway' strings, the interface-scoped
    ones (flag I) included: a router NaviCore's DHCP handed out would show as one on the adapter on its access point."""
    out = []
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 4 and f[0] == "default":
            out.append(f"{f[3]}|{f[1]}")
    return sorted(out)


def spare_leases(subnet="192.168.4."):
    """Off Windows: [(interface, its address in `subnet`)] for every interface holding one, except the interface that
    carries the default route, which is never used here (D-NC14)."""
    inet = internet_adapter()
    out = []
    for name, addrs in parse_ifconfig(_run(["ifconfig"]) or "").items():
        got = [a for a in addrs if a.startswith(subnet)]
        if got and name != inet:
            out.append((name, got[0]))
    return out


def _prejoined(bench, whose, reach, identify, wait_s=None):
    """pc_on_ap off Windows -> the name of a spare adapter already on `whose` access point, which the harness never
    joins, moves or disconnects (the module docstring). Every board's access point is 192.168.4.1 and the SSID cannot
    be read here, so `identify(name)` proves whose it is: None when it is `whose`, else what answered instead; without
    one this Skips. A spare adapter holding a 192.168.4.x address that carries a connect to `reach` (AP_REACH when
    None; one try) is identified at once, and one that proves to be another's raises. One that carries nothing yet, or
    the one last found (remembered_spare: this run, or an earlier one on this computer) when a restart has taken its
    address away, is waited for up to `wait_s`: it comes back by itself or not at all (wait_s None: PREJOIN_WAIT_S). No
    spare adapter at all Skips (PREJOIN_SKIP)."""
    wait_s = PREJOIN_WAIT_S if wait_s is None else wait_s
    if identify is None:
        raise Skip(f"{NOT_WINDOWS_SKIP}; off Windows a spare adapter already on an access point is used only where the "
                   f"test proves whose it is, and {whose} cannot be told from another board's here (every one is "
                   f"192.168.4.1)")
    global _spare_lost_at
    host, port = reach or AP_REACH
    t0 = time.monotonic()
    if _spare_lost_at is not None and not spare_leases():
        raise Skip(f"the spare adapter {remembered_spare() or ''} has been off {whose} access point since "
                   f"{time.strftime('%H:%M:%S', time.localtime(_spare_lost_at))}, after a {wait_s:.0f} s wait this run "
                   f"(it rejoins by itself, or through its own WiFi utility): not waited for again")
    while True:
        leases, state = spare_leases(), {}
        for name, addr in leases:
            if reach_wait(host, port, timeout=0) is None:
                state[name] = f"holds {addr}, but {host}:{port} takes no connect"
                continue
            other = identify(name)
            if other is not None:
                raise AssertionError(f"{name}, the spare adapter holding {addr}, did not prove to be on {whose} access "
                                     f"point: {other}")
            _remember_spare(name)
            _spare_lost_at = None
            bench.note(f"adapter {name}: off Windows the harness joins nothing, and this spare adapter is already on "
                       f"{whose} access point ({addr}, proved {time.monotonic() - t0:.1f} s after asking); it stays "
                       f"there")
            return name
        if not leases:
            known = remembered_spare()
            if known is None:
                raise Skip(PREJOIN_SKIP)
            state[known] = "holds no 192.168.4.x address"
        if time.monotonic() - t0 >= wait_s:
            _spare_lost_at = time.time()
            raise AssertionError(f"no spare adapter was back on {whose} access point within {wait_s:.0f} s, and off "
                                 f"Windows the harness re-associates nothing: "
                                 + "; ".join(f"{n} {s}" for n, s in state.items()))
        time.sleep(PREJOIN_STEP_S)


def _back_on_its_own(name, whose, reach, wait_s=None):
    """rejoin off Windows: adapter `name` holding a 192.168.4.x address again - and, given `reach`, carrying a connect
    there - by itself within `wait_s` (None: PREJOIN_WAIT_S) -> what it took, for a note; AssertionError otherwise. The
    harness re-associates nothing here (_prejoined)."""
    global _spare_lost_at
    wait_s = PREJOIN_WAIT_S if wait_s is None else wait_s
    t0 = time.monotonic()
    target = f"{reach[0]}:{reach[1]}" if reach is not None else ""
    while True:
        addr = next((a for a in ipv4(name) if a.startswith("192.168.4.")), None)
        if addr and (reach is None or reach_wait(*reach, timeout=0) is not None):
            _spare_lost_at = None
            return (f"{name} came back by itself (off Windows the harness re-associates nothing): it held {addr}"
                    + (f" and {target} took a connect" if reach is not None else "")
                    + f" {time.monotonic() - t0:.1f} s after asking")
        if time.monotonic() - t0 >= wait_s:
            _spare_lost_at = time.time()
            raise AssertionError(f"{name} was not back on {whose} access point by itself within {wait_s:.0f} s (off "
                                 f"Windows the harness re-associates nothing): it "
                                 + (f"holds {addr}, but {target} took no connect" if addr
                                    else "holds no 192.168.4.x address"))
        time.sleep(PREJOIN_STEP_S)


# ------------------------------------------------------------------ the Mac's TP-Link, through its Realtek utility
REALTEK_PROCESS = "StatusBarApp"     # the Realtek utility's menu-bar app (/Library/Application Support/WLAN)
QUARTZ_PYTHON = os.path.expanduser("~/.venvs/benchcam/bin/python")   # a Python with pyobjc's Quartz (the camera's venv)
REALTEK_JOIN_S = 45.0     # a click to a proved board: the TP-Link took ~5 s to WCB1, then the board must answer
# The menu's network items are custom views: a scripted AXPress, a System Events click at their position and type-to-
# select all left the adapter where it was (2026-10-07); a real mouse event at the item's centre joins.
_REALTEK_MENU = """on run argv
	set want to item 1 of argv
	tell application "System Events" to tell process "StatusBarApp"
		set mbi to menu bar item 1 of menu bar 2
		click mbi
		delay 1.0
		set m to menu 1 of mbi
		if want is "" then
			set out to {}
			repeat with mi in menu items of m
				set nm to name of mi
				if nm is not missing value then set end of out to nm
			end repeat
			key code 53
			set AppleScript's text item delimiters to linefeed
			return out as text
		end if
		if not (exists menu item want of m) then
			key code 53
			return "ABSENT"
		end if
		set {px, py} to position of menu item want of m
		set {sw, sh} to size of menu item want of m
		return "AT " & ((px + (sw div 2)) as text) & " " & ((py + (sh div 2)) as text)
	end tell
end run"""
_QUARTZ_CLICK = """import sys, time, Quartz
x, y = float(sys.argv[1]), float(sys.argv[2])
for t in (Quartz.kCGEventMouseMoved, Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateMouseEvent(None, t, (x, y), Quartz.kCGMouseButtonLeft))
    time.sleep(0.25 if t == Quartz.kCGEventMouseMoved else 0.08)
"""


def iface_state(name):
    """Off Windows: 'connected' when ifconfig says the interface is active, else 'disconnected' (or 'absent' when ifconfig
    knows no such interface) - the netsh state's counterpart for an adapter watch."""
    out = _run(["ifconfig", name])
    if not out:
        return "absent"
    return "connected" if re.search(r"^\s*status: active\s*$", out, re.M) else "disconnected"


def realtek_enabled(bench):
    """bench.json "wifi_switch": "realtek", off Windows: pc_on_ap may move the spare adapter through the Realtek menu."""
    return not ON_WINDOWS and (bench.cfg.get("wifi_switch") or "") == "realtek"


def realtek_ready():
    """None when the Realtek menu can be driven here, else why not."""
    if ON_WINDOWS or sys.platform != "darwin":
        return "not a Mac"
    if not (_run(["pgrep", "-x", REALTEK_PROCESS]) or "").strip():
        return "the Realtek WiFi utility (StatusBarApp) is not running"
    if not os.path.exists(QUARTZ_PYTHON):
        return f"no Python with pyobjc's Quartz at {QUARTZ_PYTHON}: the menu needs a real mouse event"
    return None


def _realtek_menu(want, timeout=20):
    p = subprocess.run(["osascript", "-", want], input=_REALTEK_MENU, capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        why = (p.stderr or p.stdout or "").strip().splitlines()[-1:] or ["no output"]
        raise AssertionError("the Realtek menu could not be read (VS Code needs the Accessibility permission): "
                             + why[0][-160:])
    return p.stdout.strip()


def realtek_networks():
    """The names the Realtek menu lists (its own items included). Opens the menu and closes it with Escape."""
    return [x for x in _realtek_menu("").splitlines() if x]


def own_wifi_network():
    """The network the Mac's own Wi-Fi (the user's internet, en0) is on, or None - never picked."""
    out = _run(["networksetup", "-getairportnetwork", "en0"]) or ""
    m = re.match(r"^Current Wi-Fi Network: (.+)$", out.strip())
    return m.group(1) if m else None


REALTEK_SCAN = "USB-WiFi: Scan Networks..."    # the menu's own items, never a network
REALTEK_OWN_ITEMS = (REALTEK_SCAN, "Turn USB-WiFi Off", "Turn USB-WiFi On", "Join Other Network...", "WPS...",
                     "Open Wireless Utility...")


def _realtek_click(item, what):
    """Open the Realtek menu and click `item` with a real mouse event; `what` names it in messages."""
    out = _realtek_menu(item)
    if out == "ABSENT":
        raise AssertionError(f"the Realtek menu does not list {what} (out of range, or its board's access point is down)")
    m = re.match(r"^AT (-?\d+) (-?\d+)$", out)
    if not m:
        raise AssertionError(f"the Realtek menu gave no position for {what}")
    p = subprocess.run([QUARTZ_PYTHON, "-c", _QUARTZ_CLICK, m.group(1), m.group(2)], capture_output=True, text=True,
                       timeout=20)
    if p.returncode:
        raise AssertionError("the click on the Realtek menu failed: " + (p.stderr.strip().splitlines() or ["?"])[-1][-160:])


REALTEK_APPEAR_S = 30.0   # how long a network the menu does not list gets, rescanning, before realtek_pick gives up
REALTEK_SCAN_SETTLE_S = 8.0


def realtek_pick(ssid, whose, appear_s=REALTEK_APPEAR_S):
    """Click `ssid` in the Realtek menu with a real mouse event. Refuses the Mac's own Wi-Fi network. A network the menu
    does not list gets another look - after a 'Scan Networks...' when the menu offers one - until `appear_s` is up, then
    raises: the menu lists the utility's last scan, and an access point that had just restarted was missing from it 3
    and 6 s after its SoftAP line (ncwifi.refuse_short_password's put-back, then ws_line_trim; run 20261007-183330).
    `whose` names it in messages: the network's name is never quoted."""
    if ssid == own_wifi_network():
        raise AssertionError(f"{whose} network is the one the Mac's own Wi-Fi is on: never picked for the spare adapter")
    if ssid in REALTEK_OWN_ITEMS:
        raise AssertionError(f"{whose} network has the name of one of the Realtek menu's own items")
    end = time.monotonic() + appear_s
    while True:
        try:
            _realtek_click(ssid, f"{whose} network")
            return
        except AssertionError as e:
            if "does not list" not in str(e) or time.monotonic() >= end:
                raise
        _realtek_scan_click()
        time.sleep(REALTEK_SCAN_SETTLE_S)


def _realtek_scan_click():
    """Click 'Scan Networks...' when the menu offers it -> True, else False. The menu's first item is the utility's
    status: 'USB-WiFi: Scan Networks...' only while it is idle. Once its adapter lost its network it read 'USB-WiFi: On',
    with no scan item, and stayed so for minutes after the adapter was back (2026-10-08, after a NaviCore restart;
    ncwifi.refuse_short_password, run 20261008-002857). The utility goes on scanning by itself, so its list is current
    without the click, if less fresh."""
    try:
        _realtek_click(REALTEK_SCAN, "its Scan Networks item")
        return True
    except AssertionError as e:
        if "does not list" not in str(e):
            raise
        return False


def realtek_scan(settle_s=REALTEK_SCAN_SETTLE_S):
    """A scan through the Realtek menu, then the networks it lists -> ([name], fresh): fresh when its 'Scan Networks...'
    was clicked, else the utility's own list (_realtek_scan_click) after the same settle. The menu shows names only:
    whether a network is open cannot be read there."""
    fresh = _realtek_scan_click()
    time.sleep(settle_s)
    return [x for x in realtek_networks() if x not in REALTEK_OWN_ITEMS and not x.startswith("USB-WiFi:")], fresh


def _realtek_there(name, reach, identify):
    """Adapter `name` holds a 192.168.4.x address, `reach` takes a connect, and identify(name) proves the board."""
    if not next((a for a in ipv4(name) if a.startswith("192.168.4.")), None):
        return False
    if reach_wait(*reach, timeout=0) is None:
        return False
    return identify(name) is None


REALTEK_LEAVE_S = 12.0    # after a click, how long to wait for the old access point to stop answering before proving


def _realtek_left(reach, timeout=REALTEK_LEAVE_S):
    """After a click: until `reach` takes no connect - the adapter has left the access point it was on - then back to
    the caller, or `timeout`. Proving the new board any sooner opened its socket on the OLD one, which the switch then
    stranded half-open: W1 went on counting a WebSocket client after the PC had left (wifi.pc_joins_ap_ws, run
    20261007-140804)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if reach_wait(*reach, timeout=0) is None:
            return True
        time.sleep(0.5)
    return False


def _realtek_move(bench, name, ssid, whose, reach, identify):
    """Click `ssid`, let the adapter leave the access point it was on, and wait for the proof; once more after half the
    wait -> seconds it took, or None."""
    t0 = time.monotonic()
    for wait_s in (REALTEK_JOIN_S / 2, REALTEK_JOIN_S):
        realtek_pick(ssid, whose)
        _realtek_left(reach)
        if wait(lambda: _realtek_there(name, reach, identify), wait_s, step=2.0):
            return time.monotonic() - t0
    return None


@contextlib.contextmanager
def realtek_on_ap(bench, problems, ssid, whose, reach, identify, home=None):
    """pc_on_ap off Windows with "wifi_switch": "realtek": the spare adapter on `whose` access point for the block,
    moved there through the Realtek menu when identify does not already prove it -> the adapter's name. home ((ssid,
    whose, identify)): where it goes back to afterwards, proved the same way; a failure is added to `problems`.
    Without a home it stays where the test left it."""
    reach = reach or AP_REACH
    name = remembered_spare() or next((n for n, _ in spare_leases()), None)
    if name is None:
        raise Skip(PREJOIN_SKIP)
    why = realtek_ready()
    if why:
        raise Skip(f"bench.json wifi_switch is realtek, but {why}")
    if _realtek_there(name, reach, identify):
        bench.note(f"adapter {name}: already on {whose} access point")
    else:
        took = _realtek_move(bench, name, ssid, whose, reach, identify)
        if took is None:
            raise AssertionError(f"{name} was not proved on {whose} access point within {REALTEK_JOIN_S:.0f} s of "
                                 f"choosing it in the Realtek menu (twice)")
        bench.note(f"adapter {name}: moved to {whose} access point through the Realtek menu, proved {took:.1f} s after "
                   f"the click")
    _remember_spare(name)
    try:
        yield name
    finally:
        if home is not None and home[0] != ssid:
            hssid, hwhose, hident = home
            try:
                took = _realtek_move(bench, name, hssid, hwhose, reach, hident)
            except AssertionError as e:
                took, err = None, str(e)
            else:
                err = f"not proved there within {REALTEK_JOIN_S:.0f} s of choosing it in the Realtek menu (twice)"
            if took is None:
                problems.append(f"{name} did not go back to {hwhose} access point: {err}")
                bench.note(problems[-1])
            else:
                bench.note(f"adapter {name}: back on {hwhose} access point, proved {took:.1f} s after the click")


# ------------------------------------------------------------------ what the adapter can see
def parse_networks(text):
    """`netsh wlan show networks mode=bssid` output -> [{'ssid', 'auth', 'channels'}], one per network, in the order
    listed; a hidden network's ssid is ''. Never note what this returns: it holds network names."""
    nets, cur = [], None
    for line in text.splitlines():
        m = re.match(r"^SSID \d+ : ?(.*)$", line)
        if m:
            cur = {"ssid": m.group(1).rstrip(), "auth": None, "channels": []}
            nets.append(cur)
            continue
        if cur is None:
            continue
        m = re.match(r"^\s+Authentication\s*:\s*(.*)$", line)
        if m and cur["auth"] is None:
            cur["auth"] = m.group(1).strip()
            continue
        m = re.match(r"^\s+Channel\s*:\s*(\d+)", line)
        if m:
            cur["channels"].append(int(m.group(1)))
    return nets


def request_scan(guid):
    """Ask Windows to scan on the adapter with interface GUID `guid` now (wlanapi WlanScan) -> True when the request was
    taken. netsh has no scan verb, and `show networks` lists the last scan's results, which can be a minute old; a scan
    finishes within about 4 s. No administrator right is needed, and a scan leaves any association alone."""
    if os.name != "nt" or not guid:
        return False
    try:
        import ctypes
        import uuid
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                        ("Data4", ctypes.c_ubyte * 8)]
        api = ctypes.windll.wlanapi
        handle, version = wintypes.HANDLE(), wintypes.DWORD()
        if api.WlanOpenHandle(2, None, ctypes.byref(version), ctypes.byref(handle)) != 0:
            return False
        try:
            g = GUID.from_buffer_copy(uuid.UUID(guid.strip("{}")).bytes_le)
            return api.WlanScan(handle, ctypes.byref(g), None, None, None) == 0
        finally:
            api.WlanCloseHandle(handle, None)
    except (OSError, ValueError, AttributeError):
        return False


def networks(name, scan=True, settle_s=5.0):
    """(the networks adapter `name` sees, as parse_networks returns them, whether a fresh scan was taken first). With
    scan, WlanScan is asked for and `settle_s` waited out, so the list is not a minute-old cache. Skip off Windows."""
    if not ON_WINDOWS:
        raise Skip(NOT_WINDOWS_SKIP)
    fresh = False
    if scan:
        fresh = request_scan((iface(name) or {}).get("guid"))
        if fresh:
            time.sleep(settle_s)
    return parse_networks(netsh("show", "networks", "mode=bssid", f"interface={name}")), fresh
