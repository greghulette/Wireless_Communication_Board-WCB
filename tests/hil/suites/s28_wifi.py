"""WiFi modes and the WebSocket endpoint (docs/WIFI_DESIGN.md; docs/HIL_TEST_AUDIT.md WP7). All opt-in:
- `wifi_modes`: W1 turns its access point off and back on, then joins W2's access point and returns to its own. A
  mode applies at boot, so each change is a W1 reboot (four in all). Nothing needs the PC's WiFi adapter.
- `wifi_pc` (attended): a WiFi adapter on the PC joins W1's access point, opens ws://192.168.4.1/ws and gets ?VERSION
  answered over it, then returns to the network it was on. It uses an adapter that does not carry the PC's internet
  when there is one (this bench's TP-Link "Wi-Fi 2"); with a single adapter the PC is offline for about 30 s.

Credentials: the AP and JOIN lines carry a password. It is read from the board's own chain at run time and given
back to a board or to a Windows WiFi profile that is removed afterwards; it never goes into a note, a message or a
file that stays. (?backup output, and so the session log, already carries it, as it always has.) A restore if aborted:
replay W1's own ?WIFI line from its chain on its USB console and reboot; on the PC, `netsh wlan delete profile
name=HIL-<W1's SSID> interface=<adapter>` and `netsh wlan connect name=<its network> interface=<adapter>`.
"""
import os
import re
import subprocess
import tempfile
import time

from hil.checkpoint import redact_text
from hil.runner import Skip, test
from hil.ws import WsClient
from suites.common import Console, Watch, config_guard, link, marker, token, usb_wcb


def _has(lines, text):
    return any(text in x for x in lines)


def _crun(console, cmd, wait=0.8):
    m = console.send(cmd)
    time.sleep(wait)
    return [x.rstrip() for x in console.lines(m)]


def _wifi_token(tokens):
    """(the whole ?WIFI token, MODE, ssid, password) from a chain. The password is only ever handed back to a board."""
    t = token(tokens, "?WIFI,")
    if t is None:
        raise Skip("the chain lacks a ?WIFI line")
    parts = t.split(",")
    mode = parts[1].upper()
    ssid = parts[2] if len(parts) > 2 else ""
    pw = ",".join(parts[3:]) if len(parts) > 3 else ""
    return t, mode, ssid, pw


def _status(w):
    """The ?WIFI block as {label: value} ('Mode', 'Interface', 'IP address', 'Radio channel', 'WS endpoint', ...)."""
    out = {}
    for x in w.run("?WIFI"):
        m = re.match(r"^([A-Za-z][A-Za-z ]*?)\s+: (.*)$", x.rstrip())
        if m:
            out[m.group(1)] = m.group(2)
    if "Mode" not in out:
        raise AssertionError("?WIFI printed no status block")
    return out


def _mesh_channel(tokens):
    t = token(tokens, "?WCBCH,")
    if t is None:
        raise Skip("the chain lacks ?WCBCH")
    return t.split(",")[1]


def _unicast_ok(w, w2s2, problems, when):
    t = marker()
    watch = Watch(w2s2)
    w.send(f";W2,;S2{t}")
    try:
        watch.expect(w2s2, t.encode(), timeout=5)
    except AssertionError:
        problems.append(f"a unicast to W2 was not delivered {when}")


def _restore_ap(w, tok, ssid, problems):
    """Replay W1's own ?WIFI,AP line and reboot; the SoftAP boot line proves it is back."""
    out = w.run(tok)
    if not _has(out, f'WiFi mode set to AP — SSID "{ssid}"'):
        problems.append("replaying W1's ?WIFI,AP line did not confirm")
    bm = w.reboot()
    if not _has(w.dev.since(bm), f'[WIFI] SoftAP "{ssid}" up on channel'):
        problems.append("W1's access point did not come back at boot")


# ============================================================ modes (no PC adapter)
@test("wifi.off_and_back", "?WIFI,OFF takes W1's access point down at the next boot (OFF status, interface down, no WS endpoint, no SoftAP boot line, radio still on the mesh channel) while the mesh keeps delivering; W1's own ?WIFI,AP line and a reboot bring it back (2 reboots)", needs=["wcb1"], links=["W2S2"], opt_in="wifi_modes")
def off_and_back(bench):
    """processWifiCommand / wcbWifiBegin (WCB_WiFi.cpp): a mode is saved to NVS (the Mode line shows it at once) and
    applied at boot only, so the interface stays as it was until the reboot; with WiFi off ESP-NOW still pins the
    radio to the mesh channel."""
    w2s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1) as before:
        tok, mode, ssid, _ = _wifi_token(before[1])
        if mode != "AP":
            raise Skip("W1 does not host an access point")
        ch = _mesh_channel(before[1])
        changed = False
        try:
            out = w.run("?WIFI,OFF")
            changed = True
            if not _has(out, "WiFi disabled — reboot to apply."):
                problems.append("?WIFI,OFF did not confirm")
            st = _status(w)
            if not st.get("Mode", "").startswith("OFF") or st.get("Interface") != "up":
                problems.append(f"right after ?WIFI,OFF the block should show the saved mode with the AP still up: "
                                f"Mode {st.get('Mode')!r}, Interface {st.get('Interface')!r}")
            bm = w.reboot()
            if _has(w.dev.since(bm), "[WIFI] SoftAP"):
                problems.append("WiFi is off, yet the boot brought a SoftAP up")
            st = _status(w)
            for k, want in (("Mode", "OFF (ESP-NOW only)"), ("Interface", "down"), ("WS endpoint", "NOT RUNNING")):
                if st.get(k) != want:
                    problems.append(f"with WiFi off, {k} is {st.get(k)!r}, expected {want!r}")
            radio = st.get("Radio channel", "")
            if not radio.startswith(f"{ch}  (mesh channel {ch})") or "MISMATCH" in radio:
                problems.append(f"with WiFi off the radio line is {radio!r}")
            _unicast_ok(w, w2s2, problems, "with WiFi off")
        finally:
            if changed:
                _restore_ap(w, tok, ssid, problems)
        st = _status(w)
        if st.get("Mode") != "AP" or st.get("Interface") != "up" or not st.get("WS endpoint", "").startswith("ws://"):
            problems.append(f"after the restore: Mode {st.get('Mode')!r}, Interface {st.get('Interface')!r}, WS {st.get('WS endpoint')!r}")
    assert not problems, "; ".join(problems)


@test("wifi.join_w2_ap", "W1 joins W2's access point: JOIN saved for the next boot, then W1 associates on the mesh channel (boot lines, the ?WIFI JOIN block with an address and a WS endpoint, W2 counting a client) and the mesh still delivers; W1's own access point is put back (2 reboots)", needs=["wcb1"], links=["W2S2"], opt_in="wifi_modes")
def join_w2_ap(bench):
    """wcbWifiJoinTry pins the association to meshChannel, and the JOIN state machine verifies the radio stayed there
    (WCB_WiFi.cpp). W2's AP is on the same mesh channel, so the join is expected to settle 'connected'."""
    w2s2 = link(bench, 2, "S2")
    w = usb_wcb(bench)
    problems = []
    with config_guard(bench, 1, 2) as before, Console(bench, 2) as c2:
        tok1, mode1, ssid1, _ = _wifi_token(before[1])
        tok2, mode2, ssid2, pw2 = _wifi_token(before[2])
        if mode1 != "AP" or mode2 != "AP":
            raise Skip("W1 and W2 must both host an access point (W2's is joined, W1's is put back)")
        if not pw2:
            raise Skip("W2's ?WIFI,AP line carries no password")
        ch = _mesh_channel(before[1])
        if _mesh_channel(before[2]) != ch:
            raise Skip("W1 and W2 are on different mesh channels")
        changed = False
        try:
            out = w.run(f"?WIFI,JOIN,{ssid2},{pw2}")
            changed = True
            if not _has(out, f'WiFi mode set to JOIN — joining "{ssid2}"'):
                problems.append("?WIFI,JOIN did not confirm")
            bm = w.reboot()
            if not _has(w.dev.since(bm), f'[WIFI] will join "{ssid2}" on channel {ch}'):
                problems.append("no 'will join' boot line")
            try:
                w.dev.expect(rf'^\[WIFI\] joined "{re.escape(ssid2)}" on channel {ch} after \d+ attempt\(s\) — ws://\S+/ws$',
                             timeout=45, since=bm)
            except AssertionError:
                problems.append("W1 did not report joining W2's access point within 45 s of booting")
            st = _status(w)
            if st.get("Mode") != "JOIN" or st.get("Join SSID") != ssid2:
                problems.append(f"Mode {st.get('Mode')!r}, Join SSID {st.get('Join SSID')!r}")
            if not (st.get("Association", "").startswith("connected (") and st.get("Interface") == "up"):
                problems.append(f"Association {st.get('Association')!r}, Interface {st.get('Interface')!r}")
            if not st.get("IP address", "").startswith("192.168.4."):
                problems.append(f"no address from W2's AP: {st.get('IP address')!r}")
            radio = st.get("Radio channel", "")
            if not radio.startswith(f"{ch}  (mesh channel {ch})") or "MISMATCH" in radio:
                problems.append(f"joined, the radio line is {radio!r}")
            if not st.get("WS endpoint", "").startswith("ws://192.168.4."):
                problems.append(f"WS endpoint {st.get('WS endpoint')!r}")
            clients = next((x for x in _crun(c2, "?WIFI", 1.5) if x.startswith("Clients       : ")), "")
            if not re.match(r"^Clients       : [1-9]", clients):
                problems.append(f"W2 counts no client while W1 is joined: {clients!r}")
            _unicast_ok(w, w2s2, problems, "while joined to W2's AP")
        finally:
            if changed:
                _restore_ap(w, tok1, ssid1, problems)
        if _status(w).get("Mode") != "AP":
            problems.append("W1 is not back in AP mode")
    assert not problems, "; ".join(problems)


# ============================================================ the PC on the AP (attended)
def _netsh(*args, timeout=30):
    r = subprocess.run(["netsh", "wlan", *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout)
    return (r.stdout or "") + (r.stderr or "")


def _wlan_interfaces():
    """Every WiFi adapter as {'name', 'description', 'state', 'ssid', 'profile'} (`netsh wlan show interfaces`)."""
    ifaces, cur = [], None
    for line in _netsh("show", "interfaces").splitlines():
        m = re.match(r"^\s*(Name|Description|State|SSID|Profile)\s*:\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1).lower(), m.group(2).strip()
        if key == "name":
            cur = {"name": val}
            ifaces.append(cur)
        elif cur is not None and key not in cur:
            cur[key] = val
    return ifaces


def _iface(name):
    return next((i for i in _wlan_interfaces() if i["name"] == name), None)


def _joined(name, ssid):
    i = _iface(name) or {}
    return i.get("state", "").lower() == "connected" and i.get("ssid") == ssid


def _internet_adapter():
    """The adapter carrying the PC's default route, or None."""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "(Get-NetRoute -DestinationPrefix '0.0.0.0/0' | Sort-Object RouteMetric | "
                            "Select-Object -First 1).InterfaceAlias"],
                           capture_output=True, text=True, timeout=20)
        return r.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _pick_adapter(bench):
    """(adapter, why): bench.json "wifi_test_interface" when set; otherwise a WiFi adapter that does not carry the
    PC's default route, so the PC stays online (this bench's TP-Link "Wi-Fi 2"); otherwise the only one, which takes
    the PC offline until the test puts it back."""
    ifaces = _wlan_interfaces()
    want = bench.cfg.get("wifi_test_interface")
    if want:
        return next((i for i in ifaces if i["name"] == want), None), "bench.json wifi_test_interface"
    inet = _internet_adapter()
    spare = [i for i in ifaces if i["name"] != inet]
    if spare:
        return spare[0], f"not the internet adapter ({inet or 'none found'})"
    return (ifaces[0], "the only WiFi adapter: the PC is offline until the test puts it back") if ifaces else (None, "")


def _wait(pred, timeout, step=1.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


def _profile_xml(name, ssid, pw):
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return ('<?xml version="1.0"?>\n<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">\n'
            f'  <name>{esc(name)}</name>\n  <SSIDConfig><SSID><name>{esc(ssid)}</name></SSID></SSIDConfig>\n'
            '  <connectionType>ESS</connectionType>\n  <connectionMode>manual</connectionMode>\n'
            '  <MSM><security>\n    <authEncryption><authentication>WPA2PSK</authentication><encryption>AES</encryption>'
            '<useOneX>false</useOneX></authEncryption>\n'
            f'    <sharedKey><keyType>passPhrase</keyType><protected>false</protected><keyMaterial>{esc(pw)}</keyMaterial></sharedKey>\n'
            '  </security></MSM>\n</WLANProfile>\n')


@test("wifi.pc_joins_ap_ws", "(attended) A WiFi adapter on the PC joins W1's access point, opens ws://192.168.4.1/ws and has ?VERSION answered over it while W1 counts the client, then returns to the network it was on; the spare adapter is used when there is one, so the PC stays online", needs=["wcb1"], links=[], opt_in="wifi_pc")
def pc_joins_ap_ws(bench):
    """WCB_WS.cpp: a text frame per line, the console output teed back as text frames. Read-only on the board.

    Windows side: a temporary profile named HIL-<ssid> on the chosen adapter only, never one of the PC's own
    profiles, and every netsh call names the adapter - with two adapters an unnamed `netsh wlan connect` is refused
    (run 20260924-092602 failed that way, and reused the PC's own WCB1 profile on the other adapter). The profile holds
    W1's AP password from its chain and is deleted afterwards; netsh's replies are logged, and they carry no key."""
    if os.name != "nt":
        raise Skip("netsh (Windows) drives the PC's WiFi here")
    w = usb_wcb(bench)
    _, mode, ssid, pw = _wifi_token(bench.config_tokens(1, refresh=True))
    if mode != "AP" or not pw or not ssid:
        raise Skip("W1 does not host a named access point with a password")
    st = _status(w)
    if st.get("Interface") != "up" or not st.get("WS endpoint", "").startswith("ws://"):
        raise Skip(f"W1's access point is not up: Interface {st.get('Interface')!r}, WS {st.get('WS endpoint')!r}")
    ip = st.get("IP address", "")
    adapter, why = _pick_adapter(bench)
    if not adapter:
        raise Skip("this PC has no WiFi adapter" if not why else "bench.json wifi_test_interface names no WiFi adapter here")
    name = adapter["name"]
    prev = adapter.get("profile") if adapter.get("state", "").lower() == "connected" else None
    tmp = f"HIL-{ssid}"
    bench.note(f"adapter {name} ({why}); it was on {adapter.get('ssid') or '(nothing)'}; temporary profile {tmp} for "
               f"{ssid} at {ip}")
    problems = []
    added = False
    fd, path = tempfile.mkstemp(prefix="wlan-", suffix=".xml")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(_profile_xml(tmp, ssid, pw))
        out = _netsh("add", "profile", f"filename={path}", f"interface={name}", "user=current")
        try:
            os.remove(path)
        except OSError:
            pass
        bench.note("netsh add profile: " + redact_text(" ".join(out.split()))[:200])
        added = "is added" in out
        if not added:
            raise AssertionError("netsh did not add the temporary profile")
        out = _netsh("connect", f"name={tmp}", f"ssid={ssid}", f"interface={name}")
        bench.note("netsh connect: " + " ".join(out.split())[:200])
        if not _wait(lambda: _joined(name, ssid), 30):
            now = _iface(name) or {}
            problems.append(f"{name} did not associate with W1's access point within 30 s "
                            f"(state {now.get('state')!r}, SSID {now.get('ssid')!r})")
        else:
            ws = None
            end = time.monotonic() + 25
            while ws is None and time.monotonic() < end:          # DHCP, then the endpoint
                try:
                    ws = WsClient(ip, timeout=4.0)
                except OSError:
                    time.sleep(1.5)
            if ws is None:
                problems.append(f"could not open ws://{ip}/ws within 25 s of associating")
            else:
                try:
                    ws.send_text("?VERSION\n")
                    text = ws.read_until("End of Version", timeout=6)
                    if "Software Version:" not in text:
                        problems.append(f"?VERSION over the WebSocket was not answered (got {text[:80]!r})")
                    ep = _status(w).get("WS endpoint", "")
                    if not re.search(r"\([1-9]\d* client\(s\) connected\)$", ep):
                        problems.append(f"W1 counts no WebSocket client while one is open: {ep!r}")
                finally:
                    ws.close()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
        _netsh("disconnect", f"interface={name}")
        if added:
            bench.note("netsh delete profile: " + " ".join(_netsh("delete", "profile", f"name={tmp}",
                                                                  f"interface={name}").split())[:200])
        if prev:
            _netsh("connect", f"name={prev}", f"interface={name}")
            if not _wait(lambda: (_iface(name) or {}).get("state", "").lower() == "connected", 45):
                problems.append(f"{name} did not reconnect to {prev} within 45 s: reconnect it by hand")
    time.sleep(2)
    ep = _status(w).get("WS endpoint", "")
    if not ep.endswith("(0 client(s) connected)"):
        problems.append(f"after the PC left, W1 still counts a client: {ep!r}")
    assert not problems, "; ".join(problems)
