"""Intellex over WiFi, through NaviCore's access point (docs/hil_plan/INTELLEX.md IX-WP9; docs/HIL_TESTING.md §10).

For each test the PC's spare WiFi adapter joins NaviCore's access point (suites/s45_navicore_wifi.py _on_ap: hil/wlan.py
pc_on_ap under a temporary HIL- profile, the SSID and password read from NaviCore's GET_CONFIG over USB; a spare adapter
only, D-NC14) and goes back to its own network afterwards. On this bench the spare adapter's own network IS NaviCore's
access point, so the join is a re-association under the temporary profile and the way back returns it to Windows' own.
Off Windows (the Mac) the harness joins nothing: the spare adapter is already on NaviCore's access point and stays there
(hil/wlan.py _prejoined), so the AP hop and the bounce skip there.
A staged, leashed Intellex host (no COM port at all, offline, no discovery host) is then attached the way Intellex's
chooser attaches a droid it identified: POST /_api/attach {kind: ws, host: 192.168.4.1, role: navicore}.

Gates: navicore_wifi (ticked on this bench) - the same act as s45's tests, so the same key (INTELLEX.md DX34); the link
loss also needs intellex_reboot (a NaviCore restart), and the AP hop and the bounce are behind the attended
intellex_wifi_join.

What the harness owns here, besides the join:
- the credential rule: NaviCore's SSID and password go only to Windows' temporary profile; the host's lines and log
  files have the SSID taken out (run_intellex_test hide=, copy_logs), because a host attached over WiFi records the SSID
  it asks Windows for (Intellex src/host.py api_attach, discover.ssid_for_host) and names both networks when the adapter
  moves (_reidentify_if_moved); a spec is never handed a network name, and /_api/status reaches it scrubbed
  (status_view);
  a venv script gets a SHA-256 of the SSID, or the SSID in its environment (the bounce), never in argv or its results;
- nothing reset NaviCore unless the test restarts it: its uptime over USB kept counting and W1 heard no boot announce
  from WCB 20 (DX20), beside the rawLink's boot check;
- afterwards: NaviCore's monitor stopped and debug flags 0 (the tool's connect sets both, RAM only), and every WCB's
  remote terminal stopped when the Wizard ran (it arms ?RTERM,START,20 on each board it manages through NaviCore).

The Wizard through NaviCore (wifi_wizard_via_navicore, wifi_rterm_rate) pulls every WCB NaviCore hears, so it runs in
config_guard over all of them (DX30). Nothing here moves a servo but the link loss's NaviCore restart (hil/servos.py).
A restore by hand, if a run dies mid-test: `netsh wlan delete profile name=HIL-<NaviCore's SSID> interface=<adapter>`,
reconnect the adapter; ?RTERM,STOP on each WCB's USB console.
"""
import contextlib
import hashlib
import threading
import time

from hil import optin, wlan
from hil.intellex import (NAVICORE_BOOT_MARKERS, IntellexHost, LinkTap, boot_check, copy_logs, require, run_intellex_py,
                          run_intellex_test, running_intellex, stage)
from hil.navicore import NaviCore
from hil.nc_guard import nc_guard
from hil.runner import Skip, test
from hil.wcb import WCB
from suites.common import config_guard, usb_wcb
from suites.s33_intellex_bench import _boot_edges
from suites.s34_intellex_tools import _nc_quiet
from suites.s45_navicore_wifi import NC_AP_IP, _navicore_ap, _on_ap, _spare_adapter, ap_of
from suites.s46_navicore_boot import _line1, _restartable, _restarted, _uptime_ms

NC_WS = {"kind": "ws", "host": NC_AP_IP, "role": "navicore"}   # what Intellex's chooser attaches for a NaviCore
RATE_S = 60                 # wifi_rterm_rate's window (INTELLEX.md IX-WP9)
RATE_BOUND = 6              # DX10: at most 6 ?RTERM,START a minute on a board, one every 10 s
NOTICE_S = 15               # Intellex's own figure for noticing a vanished AP (ws_transport.py:66-76; its probe ~9 s)
CLOSE_STALL_S = 10          # finding 19: dropping the dead link stalls the host for websockets' close_timeout
NOTICE_LIMIT_S = NOTICE_S + CLOSE_STALL_S + 5   # wifi_link_loss: noticed at all; the 15 s is link_drop_no_stall's
REATTACH_S = 30             # from the PC's link carrying a connect: the loop retries every ~1 s, 5 s per open
PING = '{"type":"PING"}\n'
PONG = b'"type":"PONG"'
STARTED = "[RTERM] Session started"      # WCB_RemoteTerm.cpp startSession, printed for every START, re-arms included


# ------------------------------------------------------------------ helpers (selftest.py feeds the pure ones)
def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def status_view(st, hide=()):
    """/_api/status as a spec or a message may see it: the keys that say what the host is attached to, with lastError
    scrubbed - a host held off a move says 'now on "<a>", not "<b>"' there, both network names (host.py
    _reidentify_if_moved)."""
    st = st if isinstance(st, dict) else {}
    out = {k: st.get(k) for k in ("attached", "kind", "role", "relayId", "wantsLink", "reconnecting", "target")}
    out["lastError"] = wlan.scrub(str(st.get("lastError") or ""), *hide)
    return out


def _wait_status(host, pred, timeout, step=0.25):
    """(the /_api/status that satisfied `pred`, seconds it took) or (the last one read, None) after `timeout`."""
    t0 = time.monotonic()
    last = {}
    while True:
        try:
            st, body = host.json("GET", "/_api/status", timeout=5)
            if st == 200 and isinstance(body, dict):
                last = body
                if pred(body):
                    return body, time.monotonic() - t0
        except OSError:
            pass
        if time.monotonic() - t0 >= timeout:
            return last, None
        time.sleep(step)


def _dead_lines(host):
    return sum(1 for x in host.lines if "link is dead" in x)       # host.py reconnect_loop's verdict


def _wait_notice(host, dead0, timeout, step=0.25):
    """The host noticing a lost link -> (the last /_api/status, seconds, how): the status turning detached ('status'),
    or a 'link is dead' line beyond the `dead0` it had printed before ('line'). Off Windows the spare adapter rejoins
    by itself within seconds of the access point coming back, so the host can drop the link and attach again between
    two status polls: run 20261006-172611's reattached 48 ms after its verdict. (last status, None, None) after
    `timeout`."""
    t0 = time.monotonic()
    last = {}
    while True:
        try:
            st, body = host.json("GET", "/_api/status", timeout=5)
            if st == 200 and isinstance(body, dict):
                last = body
                if not body.get("attached"):
                    return body, time.monotonic() - t0, "status"
        except OSError:
            pass
        if _dead_lines(host) > dead0:
            return last, time.monotonic() - t0, "line"
        if time.monotonic() - t0 >= timeout:
            return last, None, None
        time.sleep(step)


def rterm_starts(lines, relay):
    """How many '[RTERM] Session started -> relay WCB<relay>' lines a WCB console printed: one for every
    ?RTERM,START,<relay> it ran, a re-arm of the running session included (WCB.ino, the ?RTERM handler)."""
    return sum(1 for x in lines if x.startswith(STARTED) and x.rstrip().endswith(f"WCB{relay}"))


def move_order_problems(lines):
    """A host's printed lines around an access-point move -> [problem]: each 'network changed' note must come before
    the next 'reconnected' line (host.py reconnect_loop: _reidentify_if_moved, then the attach - rule 10: the role is
    corrected before the page can read it), and at least one move must have been noted. Lines arrive scrubbed."""
    out = []
    moves = [i for i, x in enumerate(lines) if "network changed" in x]
    if not moves:
        return ["the host printed no 'network changed' line: it reattached without re-identifying the board"]
    for i in moves:
        nxt = next((j for j in range(i + 1, len(lines)) if lines[j].startswith("reconnected")), None)
        if nxt is None:
            out.append("after a 'network changed' line the host printed no 'reconnected' line")
    first_re = next((j for j, x in enumerate(lines) if x.startswith("reconnected")), None)
    if first_re is not None and first_re < moves[0]:
        out.append("the host printed 'reconnected' before its first 'network changed' line: it attached the new "
                   "board under the old role first")
    return out


def _stop_rterm_all(bench, skip=()):
    """?RTERM,STOP on every WCB (RAM only): on its own USB console when it has one, relayed from W1 otherwise. The Wizard
    arms ?RTERM,START,<relay> on each board it manages through a doorway (setRemoteConnected ->
    startRemoteTermSession, Wizard/app.js), and a session left running mirrors every line that board prints."""
    usb = bench.usb_wcbs()
    for n in bench.wcb_numbers():
        if n in skip:
            continue
        try:
            if n in usb:
                WCB(bench.dev(usb[n])).run("?RTERM,STOP")
            else:
                usb_wcb(bench).send(f";W{n},?RTERM,STOP")
        except AssertionError as e:
            bench.note(f"?RTERM,STOP on WCB{n} failed: {(str(e).splitlines() or [repr(e)])[0]}")


def _free_of_others(bench):
    require(bench)
    others = running_intellex()
    if others:
        raise Skip(f"another Intellex is running (PID {others[0][0]}); one attached to NaviCore's access point re-arms "
                   f"W1's terminal and holds the socket - close it first")


def _nc_over_wifi(bench, test_id, nc, cfg, args=None, hooks=None, timeout=360.0):
    """Run spec `test_id` against a leashed host attached to NaviCore over WiFi (NC_WS), inside the caller's _on_ap
    block -> [problem] from the off-stream witnesses. The spec gets version (NaviCore's PONG over USB), host, relay
    (NaviCore's mesh id), boards (the bench's WCBs), plus `args`. Skipped while NaviCore's recorder holds anything
    (D33): a regression could restart it. Afterwards NaviCore's monitor and debug flags are reset, and its uptime must
    have kept counting and W1 must not have heard it boot."""
    _restartable(nc)
    ssid, _ = ap_of(cfg)
    version = nc.ping()
    nid = nc.wcb_status()["self"]
    up0, t0 = nc.mesh_stats()["upMs"], time.monotonic()
    w1 = WCB(bench.dev("wcb1")) if bench.has("wcb1") else None
    m1 = w1.dev.mark() if w1 else None
    base = {"version": version, "host": NC_AP_IP, "relay": nid, "boards": bench.wcb_numbers()}
    try:
        run_intellex_test(bench, test_id, attach=dict(NC_WS), args=dict(base, **(args or {})),
                          link_check=boot_check(NAVICORE_BOOT_MARKERS, "NaviCore"), hide=(ssid,), hooks=hooks,
                          timeout=timeout)
    finally:
        _nc_quiet(bench)
    nc = NaviCore(bench.dev("navicore"))
    up1, gone = nc.mesh_stats()["upMs"], (time.monotonic() - t0) * 1000
    problems = []
    if up1 < up0 + gone - 3000:
        problems.append(f"NaviCore's uptime went from {up0} to {up1} ms across {gone:.0f} ms: it restarted")
    if w1:
        edges = _boot_edges(w1.dev, m1, nid)
        if edges:
            problems.append(f"W1 heard WCB{nid} boot {len(edges)} time(s)")
    return problems


# ============================================================ discovery
@test("intellex.wifi_discover", "On NaviCore's access point (the PC's spare adapter joined for the test), Intellex's "
      "discovery calls the host a NaviCore: scan and probe give kind navicore with NaviCore's USB version, reached from "
      "the adapter's 192.168.4.x lease; ssid_for_host names NaviCore's network (compared by hash); the host's "
      "/_api/discover?hosts= says the same, and with its leash's empty host list probes nothing (opt-in navicore_wifi; "
      "IX-WP9)", needs=["navicore"], opt_in="navicore_wifi")
def wifi_discover(bench):
    """Intellex src/discover.py probe and scan (:263-454): PING, ?RELAY,WIFI and ?WDP,DUMP pipelined on one socket, an
    id-less PONG decisive; ssid_for_host (:475-532) asks netsh which adapter holds an address on the host's /24 and what
    it is associated with (read-only, Intellex's own calls). /_api/discover (host.py :393-402): an explicit ?hosts= wins,
    and with INTELLEX_DISCOVER_HOSTS set but empty (the harness's leash, H3) a bare call probes nothing."""
    _free_of_others(bench)
    problems = []
    with _on_ap(bench, problems) as (nc, cfg, name):
        ssid, _ = ap_of(cfg)
        version = nc.ping()
        sd = stage(bench, "intellex.wifi_discover", tools="none")
        run_intellex_py(bench, "intellex.wifi_discover", "wifi_units.py", stage_dir=sd, timeout=120,
                        args={"group": "discover", "host": NC_AP_IP, "version": version, "ssid_sha": _sha(ssid),
                              "ssid_len": len(ssid)})
        host = IntellexHost(bench, sd, hide=(ssid,))
        try:
            host.start()
            st, body = host.json("GET", f"/_api/discover?hosts={NC_AP_IP}", timeout=30)
            rows = body.get("candidates") if isinstance(body, dict) else None      # host.py api_discover
            if st != 200 or not isinstance(rows, list) or len(rows) != 1:
                problems.append(f"/_api/discover?hosts={NC_AP_IP}: {st}, "
                                f"{len(rows) if isinstance(rows, list) else 'no candidates list:'} row(s), not 1")
            else:
                r = rows[0]
                shape = {k: r.get(k) for k in ("kind", "isNaviCore", "isMesh", "reachable", "version")}
                if r.get("kind") != "navicore" or not r.get("isNaviCore") or r.get("isMesh"):
                    problems.append(f"/_api/discover calls NaviCore's access point {shape}")
                if r.get("version") != version:
                    problems.append(f"/_api/discover's version {r.get('version')!r}, NaviCore's PONG over USB {version!r}")
                if str(r.get("via") or "").rsplit(".", 1)[0] != NC_AP_IP.rsplit(".", 1)[0]:
                    problems.append(f"/_api/discover reached it from {r.get('via')!r}, not from an address on "
                                    f"{NC_AP_IP}'s network (the adapter's lease)")
            st, body = host.json("GET", "/_api/discover", timeout=30)
            rows = body.get("candidates") if isinstance(body, dict) else None
            if st != 200 or rows != []:
                problems.append(f"/_api/discover with no hosts= under the leash (INTELLEX_DISCOVER_HOSTS empty): {st}, "
                                f"{f'{len(rows)} row(s)' if isinstance(rows, list) else 'no candidates list'}, not "
                                f"none")
        finally:
            host.stop()
            copy_logs(bench, sd, "intellex.wifi_discover", (ssid,))
    assert not problems, "; ".join(problems)


# ============================================================ the NaviCore config tool over WiFi
@test("intellex.wifi_nc_tool", "The config tool through Intellex attached to NaviCore's access point (the PC's spare "
      "adapter joined for the test): it connects on its own and directly, the label reads '· WiFi 192.168.4.1 ▾', the "
      "OTA button 'Update over WiFi (OTA)' with the WiFi title, both flash buttons are disabled with the not-USB "
      "message, and the host refuses /_api/flash and /_api/flash-wcb with 409; only reads sent, NaviCore never "
      "restarted (opt-in navicore_wifi; IX-WP9)", needs=["navicore"], opt_in="navicore_wifi")
def wifi_nc_tool(bench):
    """intellex_shim.js autoConnect (a role 'navicore' link is direct: no Via WCB), labelFor ('ws://<ip>/ws' -> 'WiFi
    <ip>'), wireNativeFlash (a 'ws' link disables both buttons with NOT_USB_MSG) and relabelOtaButton (OTA_WORDS,
    OTA_WIFI_TITLE); host.py _claim_port_for_flash refuses anything but a serial link before any flash starts. The spec
    posts the two flash routes itself from Node: over WiFi the host has no port to hand esptool."""
    _free_of_others(bench)
    problems = []
    with _on_ap(bench, problems) as (nc, cfg, _):
        problems += _nc_over_wifi(bench, "intellex.wifi_nc_tool", nc, cfg)
    assert not problems, "; ".join(problems)


# ============================================================ the Wizard through NaviCore
@test("intellex.wifi_wizard_via_navicore", "The Wizard through Intellex attached to NaviCore's access point: NaviCore's "
      "backup files it as a relay card at its own id, and every bench WCB is managed and pulled through it - a "
      "baseline naming its number, filed under NaviCore's slot - while NaviCore itself is never a numbered board "
      "(opt-in navicore_wifi; IX-WP9)", needs=["navicore", "wcb1"], opt_in="navicore_wifi")
def wifi_wizard_via_navicore(bench):
    """Intellex docs/WCB_WIZARD.md 'still to verify on hardware': NaviCore advertises ?RELAY,1 (WCB_Client WCB_Mgmt.h
    printBackup), so the Wizard files it as a relay card at its DEVICE_ID (app.js applyRelayRole) while its WDP sweep
    also lists it as a client; relayRouteAll skips clients and its own id. The shim runs routeMeshThroughRelay (wait for
    the card, then relayRouteAll) and routeMeshThroughBoard (which stands down once a card exists) whatever the host
    says it attached to (intellex_shim.js autoConnectWcb). The pulls cross NaviCore's WcbMgmt relay (?MGMT,PULL,<n>,P;
    a config over 2912 characters comes back in [MGMT:CFGPART] parts). config_guard on every WCB; every WCB's ?RTERM
    stopped afterwards."""
    _free_of_others(bench)
    problems = []
    with config_guard(bench, *bench.wcb_numbers()):
        try:
            with _on_ap(bench, problems) as (nc, cfg, _):
                problems += _nc_over_wifi(bench, "intellex.wifi_wizard_via_navicore", nc, cfg, timeout=540)
        finally:
            _stop_rterm_all(bench)
    assert not problems, "; ".join(problems)


def _rterm_hooks(bench, relay):
    """The rate test's hooks -> (hooks, state). rterm_mark marks every WCB's USB console; rterm_count counts the
    '[RTERM] Session started -> relay WCB<relay>' lines each printed since (rterm_starts) and the seconds between."""
    usb = bench.usb_wcbs()
    state = {}

    def mark(host, body):
        state["marks"] = {n: bench.dev(d).mark() for n, d in usb.items()}
        state["t0"] = time.monotonic()
        return {"boards": sorted(usb)}

    def count(host, body):
        if "marks" not in state:
            raise AssertionError("rterm_count before rterm_mark")
        secs = time.monotonic() - state["t0"]
        counts = {str(n): rterm_starts(bench.dev(d).since(state["marks"][n]), relay) for n, d in usb.items()}
        state.update(counts=counts, secs=secs)
        return {"counts": counts, "seconds": round(secs, 1)}
    return {"rterm_mark": mark, "rterm_count": count}, state


@test("intellex.wifi_rterm_rate", "With the Wizard managing the mesh through Intellex attached to NaviCore's access "
      "point, and every board pulled, no WCB is sent more than 6 ?RTERM,START,<NaviCore> in the next 60 s - counted on "
      "each WCB's own USB console, beside the re-arms the page itself wrote (opt-in navicore_wifi; IX-WP9, DX10)",
      needs=["navicore", "wcb1"], opt_in="navicore_wifi")
def wifi_rterm_rate(bench):
    """The open F21 question (docs/HIL_TEST_AUDIT.md F21, tracker #93): with Intellex attached to NaviCore's access point
    W1 was sent ?RTERM,START,20 about once a second, which held its deferred reboots off until F21 exempted a re-arm of
    the running session from the quiet window. Only the Wizard sends it: startRemoteTermSession, three times a call
    (sendMgmtReliable), from setRemoteConnected, a pull that succeeded, an ETM came-ONLINE edge through the relay and
    relayRouteAll's re-arm of a board already managed - which the shim's routeMeshThroughRelay calls every 2 s for as
    long as its relay card shows a board unmanaged. Measured after the pulls settle; the bound is DX10's."""
    _free_of_others(bench)
    problems, state = [], {}
    with config_guard(bench, *bench.wcb_numbers()):
        try:
            with _on_ap(bench, problems) as (nc, cfg, _):
                hooks, state = _rterm_hooks(bench, nc.wcb_status()["self"])
                problems += _nc_over_wifi(bench, "intellex.wifi_rterm_rate", nc, cfg, hooks=hooks, timeout=660,
                                          args={"window": RATE_S, "bound": RATE_BOUND})
        finally:
            _stop_rterm_all(bench)
    if "counts" in state:
        bench.note(f"intellex.wifi_rterm_rate: ?RTERM,START per WCB in {state['secs']:.0f} s: {state['counts']}")
        over = {n: c for n, c in state["counts"].items() if c > RATE_BOUND}
        if over:
            problems.append(f"WCBs sent more than {RATE_BOUND} ?RTERM,START in {state['secs']:.0f} s (DX10): {over}")
    assert not problems, "; ".join(problems)


# ============================================================ the link lost: a NaviCore restart
@test("intellex.wifi_link_loss", "A NaviCore REBOOT through an Intellex host attached to its access point: the host "
      "notices the lost link (its /_api/status within 30 s: its own 15 s plus finding 19's 10 s stall), never "
      "re-associates the adapter itself (--no-auto-bounce: no 're-associating' line), attaches again within 30 s of "
      "the PC's link carrying a connect to the access point again, and a page on the link gets a PONG again (opt-in "
      "intellex_reboot, with navicore_wifi; 1 NaviCore restart; IX-WP9)", needs=["navicore"],
      opt_in="intellex_reboot")
def wifi_link_loss(bench):
    """Intellex src/ws_transport.py (a ping every 5 s, 10 s to answer) and host.py reconnect_loop (an idle-gated TCP
    probe; with --no-auto-bounce never wifi_bounce): a restart takes NaviCore's access point down, and the host must
    notice, keep the target and attach again by itself. REBOOT goes through the link (a raw /_link page), is ACKed, and
    restarts NaviCore 250 ms later (NaviCore.ino); proved by its uptime over USB. Inside nc_guard, skipped while the
    recorder holds anything (D33), as every NaviCore restart.

    Run 20260929-202852 set the shape. Noticing: the probe declared the link dead 11.8 s after the REBOOT, then the
    host answered nothing for 10 s while it closed the dead WebSocket on its event loop (INTELLEX.md finding 19), so
    /_api/status showed the loss at 21.9 s. That stall is pinned by the board-free (should) test
    intellex.link_drop_no_stall; here the notice only has to come within NOTICE_LIMIT_S, and over NOTICE_S is noted.
    What brings the PC's link back is not Intellex's here (its bounce is leashed off), and "connected with a lease" did
    not: REBOOT deauthenticates nobody, and Windows kept the old association for 90 s with no WLAN event while the
    restarted AP dropped its frames. So hil/wlan.py rejoin(reach=) proves a TCP connect to 192.168.4.1:80 and
    re-associates the adapter when there is none, and the host is timed from that proof."""
    if "navicore_wifi" not in optin.enabled(bench.cfg):
        raise Skip(optin.skip_reason("navicore_wifi"))
    _free_of_others(bench)
    tid = "intellex.wifi_link_loss"
    problems, facts = [], {}
    with nc_guard(bench) as g:
        _restartable(g.nc)
        _spare_adapter(bench)                  # a PC with no spare adapter skips before NaviCore restarts
        with _on_ap(bench, problems) as (nc, cfg, name):
            ssid, _ = ap_of(cfg)
            sd = stage(bench, tid, tools="none")
            host = IntellexHost(bench, sd, hide=(ssid,))
            a = b = None
            try:
                host.start()
                host.attach(dict(NC_WS))
                a = LinkTap(host.port, name="A")
                time.sleep(0.5)                # the host binds a page just after its 101
                a.send(PING)
                a.wait_for(PONG, 6)
                m = g.nc.dev.mark()
                dead0 = _dead_lines(host)
                t_cmd = time.monotonic()
                a.send('{"type":"REBOOT"}\n')
                st, took, how = _wait_notice(host, dead0, NOTICE_LIMIT_S)
                if took is None:
                    problems.append(f"the host still called the link attached {NOTICE_LIMIT_S} s after the REBOOT, and "
                                    f"printed no 'link is dead'")
                else:
                    facts["noticed_s"] = round(took, 1)
                    facts["noticed_by"] = how       # 'line': it reattached before a status poll saw the gap
                    facts["verdict"] = _dead_lines(host) > dead0     # the probe's line (host.py reconnect_loop)
                    if took > NOTICE_S:
                        facts["over_notice"] = (f"{took - NOTICE_S:.1f} s over Intellex's own {NOTICE_S} s: its close "
                                                f"stall (INTELLEX.md finding 19, intellex.link_drop_no_stall)")
                closing = time.monotonic() + 5.0          # the close frame follows the drop by a moment
                while a.closed is None and time.monotonic() < closing:
                    time.sleep(0.1)
                if a.closed is None:
                    problems.append("the host kept the page's /_link open across the lost link (host.py _drop closes "
                                    "every page, so a tool re-handshakes)")
                try:
                    g.nc.wait_boot(since=m, timeout=40)
                except AssertionError as e:
                    problems.append(f"NaviCore did not come back from the REBOOT over USB: {_line1(e)}")
                g.nc = NaviCore(bench.dev("navicore"))      # nc_guard restores through the port as it is now
                up = _uptime_ms(g.nc)
                if not _restarted(up, (time.monotonic() - t_cmd) * 1000):
                    problems.append(f"NaviCore's uptime {up} ms: the REBOOT through the link did not restart it")
                facts["rejoin"] = wlan.rejoin(bench, name, ssid, "NaviCore's", reach=(NC_AP_IP, 80))
                st, took = _wait_status(host, lambda s: bool(s.get("attached")), REATTACH_S)
                if took is None:
                    problems.append(f"the host did not attach again within {REATTACH_S} s of {NC_AP_IP}:80 taking a "
                                    f"connect from this PC ({status_view(st, (ssid,))})")
                else:
                    facts["reattached_s"] = round(took, 1)
                    b = LinkTap(host.port, name="B")
                    time.sleep(0.5)
                    b.send(PING)
                    try:
                        b.wait_for(PONG, 8)
                    except AssertionError as e:
                        problems.append(f"a page on the reattached link: {e}")
                bounced = [x for x in host.lines if "re-associating" in x or "giving up on re-associating" in x]
                if bounced:
                    problems.append(f"the host re-associated the adapter itself {len(bounced)} time(s) under "
                                    f"--no-auto-bounce")
            finally:
                for tap in (a, b):
                    if tap is not None:
                        tap.close()
                host.stop()
                copy_logs(bench, sd, tid, (ssid,))
    bench.note(f"{tid}: {facts}")
    assert not problems, "; ".join(problems)


# ============================================================ attended: a hop to W1's access point, and the bounce
@test("intellex.wifi_ap_hop_reidentify", "(attended) With Intellex attached to NaviCore's access point, the PC's spare "
      "adapter moved to W1's: the host re-identifies before it attaches - /_api/status turns role wcb with relayId 1, "
      "its 'network changed' note before its 'reconnected' line - and the config tool reloaded there is in Via WCB; "
      "moved back, role navicore and a direct tool again (opt-in intellex_wifi_join; IX-WP9)",
      needs=["navicore", "wcb1"], opt_in="intellex_wifi_join")
def wifi_ap_hop_reidentify(bench):
    """Intellex src/host.py _reidentify_if_moved: every access point here is 192.168.4.1, so a reattach by address lands
    on whatever the adapter joined since, and the role must be corrected BEFORE the attach, which the page reads at
    once (CLAUDE.md rule 10: a WCB doorway taken for a direct NaviCore sends ?OTALOCAL to the WCB). The spec asks the
    harness to move the adapter (hooks hop and hop_back: s28 _pc_on_w1_ap nested inside _on_ap, so hop_back returns it
    to NaviCore's temporary profile); the host's lines are read with both network names taken out. Off Windows the
    adapter moves through the Realtek menu (bench.json wifi_switch, hil/wlan.py realtek_on_ap; D82), and without it the
    test skips (the harness moves no adapter there, _prejoined)."""
    if not wlan.ON_WINDOWS and not wlan.realtek_enabled(bench):
        raise Skip(f"{wlan.NOT_WINDOWS_SKIP}: the hop moves the PC's spare adapter to W1's access point and back")
    from suites.s28_wifi import _pc_on_w1_ap, _status as _w1_wifi, _wifi_token
    _free_of_others(bench)
    _, mode, w1_ssid, w1_pw = _wifi_token(bench.config_tokens(1, refresh=True))
    if mode != "AP" or not w1_ssid or not w1_pw:
        raise Skip("W1 does not host a named access point with a password")
    w1st = _w1_wifi(usb_wcb(bench))
    if w1st.get("Interface") != "up" or not w1st.get("WS endpoint", "").startswith("ws://"):
        raise Skip(f"W1's access point is not up: Interface {w1st.get('Interface')!r}")
    tid = "intellex.wifi_ap_hop_reidentify"
    problems, seen = [], {}
    with _on_ap(bench, problems) as (nc, cfg, _):
        ssid, _ = ap_of(cfg)
        hide = (ssid, w1_ssid)
        stack = contextlib.ExitStack()

        def hop(host, body):
            stack.enter_context(_pc_on_w1_ap(bench, problems))
            st, took = _wait_status(host, lambda s: s.get("attached") and s.get("role") == "wcb", 90)
            seen["hop"] = None if took is None else round(took, 1)
            return {"status": status_view(st, hide), "seconds": seen["hop"]}

        def hop_back(host, body):
            stack.close()
            st, took = _wait_status(host, lambda s: s.get("attached") and s.get("role") == "navicore", 90)
            seen["back"] = None if took is None else round(took, 1)
            seen["lines"] = [x for x in host.lines if "network changed" in x or x.startswith("reconnected")
                             or "not reattaching" in x]
            return {"status": status_view(st, hide), "seconds": seen["back"]}
        try:
            version = nc.ping()
            run_intellex_test(bench, tid, attach=dict(NC_WS), hide=hide, timeout=960,
                              hooks={"hop": hop, "hop_back": hop_back},
                              args={"version": version, "host": NC_AP_IP, "w1": bench.usb_wcb_number()})
        finally:
            stack.close()
            _nc_quiet(bench)
    if "lines" in seen:
        problems += move_order_problems(seen["lines"])
        bench.note(f"{tid}: re-identified in {seen.get('hop')} s after the hop and {seen.get('back')} s after the hop "
                   f"back; the host's lines: {seen['lines']}")
    assert not problems, "; ".join(problems)


class _AdapterWatch:
    """The state of each WiFi adapter named, every `period` s on its own thread (netsh wlan show interfaces, read-only)
    -> stop() returns [(t, {name: state})]. States only: never an SSID or a profile name."""

    def __init__(self, names, period=0.5):
        self.names, self.period = list(names), period
        self.samples = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="wlan-watch", daemon=True)

    def _run(self):
        while not self._stop.is_set():
            try:
                if wlan.ON_WINDOWS:
                    now = {i["name"]: i.get("state", "") for i in wlan.wlan_interfaces() if i["name"] in self.names}
                else:                                   # the Mac: ifconfig's status line, per interface
                    now = {n: wlan.iface_state(n) for n in self.names}
                self.samples.append((time.monotonic(), now))
            except Exception:  # noqa: BLE001 - a failed read is a missing sample, not a failure
                pass
            self._stop.wait(self.period)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread.join(5)
        return list(self.samples)


def bounce_problems(samples, inet, spare):
    """Adapter-state samples around a wifi_bounce (_AdapterWatch) -> ([problem], facts): the adapter carrying the PC's
    internet must read 'connected' in every sample; how often the spare adapter read anything else is a fact (a bounce
    can fall between two samples)."""
    problems = []
    if not samples:
        return ["no adapter state was read during the bounce"], {}

    def state(s, name):
        return str(s.get(name, "absent")).strip().lower()
    inet_bad = [state(s, inet) for _, s in samples if state(s, inet) != "connected"]
    if inet_bad:
        problems.append(f"{inet}, the adapter carrying the PC's internet, left 'connected' in {len(inet_bad)} of "
                        f"{len(samples)} samples ({sorted(set(inet_bad))})")
    spare_down = sum(1 for _, s in samples if state(s, spare) != "connected")
    return problems, {"samples": len(samples), "spare_not_connected": spare_down}


@test("intellex.wifi_bounce_scoped", "(attended) Intellex's own wifi_bounce, asked for NaviCore's network, disconnects "
      "and reconnects only the spare adapter on it: the adapter carrying the PC's internet stays connected in every "
      "sample and its default route stays, and the spare adapter is back on NaviCore's network with a lease (opt-in "
      "intellex_wifi_join; IX-WP9)", needs=["navicore"], opt_in="intellex_wifi_join")
def wifi_bounce_scoped(bench):
    """Intellex src/discover.py wifi_bounce (:535-608): it must touch the ONE interface associated with the SSID - a bare
    'netsh wlan disconnect' drops every adapter - and it reconnects through the profile Windows keeps under the
    network's own name (no password needed). Run from its venv (tests/intellex/py/wifi_units.py group bounce), the SSID
    in its environment only, inside _on_ap so the adapter goes back to its own network afterwards whatever the bounce
    did. Off Windows (bench.json wifi_switch, D82) Intellex's macOS bounce (_wifi_bounce_macos) finds the adapter by
    route and cycles its network service: the watch reads ifconfig's status line, and 'back' is the adapter proved on
    NaviCore's access point by its PONG within 45 s, which nothing here helps - the bounce's own way back is the
    subject. Without the switch the test skips."""
    if not wlan.ON_WINDOWS and not wlan.realtek_enabled(bench):
        raise Skip(f"{wlan.NOT_WINDOWS_SKIP}: the adapter watch and the way back are netsh's")
    _free_of_others(bench)
    inet = wlan.internet_adapter()
    if not inet:
        raise Skip("no adapter carries the PC's default route here: there is nothing the bounce must leave alone")
    tid = "intellex.wifi_bounce_scoped"
    problems, facts = [], {}
    with _on_ap(bench, problems) as (nc, cfg, name):
        ssid, _ = ap_of(cfg)
        routes0 = wlan.default_routes()
        watch = _AdapterWatch([inet, name]).start()
        try:
            run_intellex_py(bench, tid, "wifi_units.py", timeout=120, env={"IX_SSID": ssid},
                            args={"group": "bounce", "host": NC_AP_IP})
            if wlan.ON_WINDOWS:
                back = wlan.wait(lambda: wlan.joined(name, ssid), 45)
                addr, secs, renewed = wlan.address_wait(name) if back else (None, 0, "")
            else:
                t0 = time.monotonic()
                ident = _navicore_ap(bench)
                back = wlan.wait(lambda: wlan._realtek_there(name, (NC_AP_IP, 80), ident), 45, step=2.0)
                addr = next((a for a in wlan.ipv4(name) if a.startswith("192.168.4.")), None) if back else None
                secs = round(time.monotonic() - t0, 1)
        finally:
            samples = watch.stop()
        p, f = bounce_problems(samples, inet, name)
        problems += p
        facts.update(f)
        if not wlan.ON_WINDOWS and f.get("spare_not_connected"):
            # Intellex's macOS bounce leaves an adapter macOS does not count as Wi-Fi alone (its own utility joins it;
            # cycling its service left it off for minutes, 2026-10-07): it must not have dropped at all.
            problems.append(f"{name} left 'connected' in {f['spare_not_connected']} of {f['samples']} samples, though "
                            f"the bounce was to leave it alone")
        if not back:
            problems.append(f"{name} was not back on NaviCore's network 45 s after the bounce")
        elif not addr:
            problems.append(f"{name} is back on NaviCore's network but holds no 192.168.4.x lease after {secs} s")
        else:
            facts["lease_s"] = secs
        routes1 = wlan.default_routes()
        if routes0 is not None and routes1 is not None:
            was = [r for r in routes0 if r.startswith(inet + "|")]
            now = [r for r in routes1 if r.startswith(inet + "|")]
            if was != now:
                problems.append(f"{inet}'s default route changed across the bounce ({len(was)} -> {len(now)})")
    bench.note(f"{tid}: {facts}")
    assert not problems, "; ".join(problems)
