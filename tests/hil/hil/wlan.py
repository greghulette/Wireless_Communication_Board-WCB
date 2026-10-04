"""The PC's own WiFi, for the tests that put it on a board's access point: W1's or W2's (suites/s28_wifi.py, opt-in
wifi_pc) and NaviCore's (suites/s45_navicore_wifi.py, opt-in navicore_wifi). docs/hil_plan/NAVICORE.md INF5: one tested
copy, moved out of s28 unchanged. Windows only: netsh drives the adapter, PowerShell reads its addresses and routes.

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

What only the bench shows: that netsh's English output has these field names (parse_interfaces, parse_networks); the
selftest feeds both parsers text captured in that form.
"""
import contextlib
import os
import re
import socket
import subprocess
import tempfile
import time

from .checkpoint import redact_text
from .runner import Skip


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
    carries the default route."""
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
def pc_on_ap(bench, problems, ssid, pw, whose, spare_only=False):
    """The PC's chosen WiFi adapter on the access point `ssid` for the block, holding a 192.168.4.x lease from it ->
    the adapter's name; Skip where that cannot be done here. `whose` names the AP in messages ("W1's"); the SSID is
    never quoted. Windows side: a temporary profile named HIL-<ssid> on the chosen adapter only, never one of the
    PC's own profiles, and every netsh call names the adapter - with two adapters an unnamed `netsh wlan connect` is
    refused (run 20260924-092602 failed that way, and reused the PC's own WCB1 profile on the other adapter). The
    profile holds the AP's password from the board's chain and is deleted afterwards. netsh's replies are noted with
    every network name scrubbed; they carry no key. On the way out the adapter goes back to the network it was on, and
    failing to is added to `problems` (and noted, for when the block raised). No association in 30 s, or no lease in
    ADDR_WAIT_S, raises after the same cleanup. spare_only: Skip rather than take the adapter that carries the PC's
    default route (NaviCore's tests, D-NC14; s28's take it when it is the only one)."""
    if os.name != "nt":
        raise Skip("netsh (Windows) drives the PC's WiFi here")
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


def default_routes():
    """The PC's 0.0.0.0/0 routes as sorted 'InterfaceAlias|NextHop' strings, or None."""
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
    the network is never named."""
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
    scan, WlanScan is asked for and `settle_s` waited out, so the list is not a minute-old cache."""
    fresh = False
    if scan:
        fresh = request_scan((iface(name) or {}).get("guid"))
        if fresh:
            time.sleep(settle_s)
    return parse_networks(netsh("show", "networks", "mode=bssid", f"interface={name}")), fresh
