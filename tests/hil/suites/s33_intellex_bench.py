"""Intellex against the bench boards (docs/hil_plan/INTELLEX.md IX-WP5 and IX-WP6; docs/HIL_TESTING.md §10).

IX-WP5, the transports: Intellex's own SerialTransport and WebSocketTransport, imported from a staged copy and run
under its venv (tests/intellex/py/transport_serial.py, transport_ws.py) against W1, W2's console and NaviCore. The harness
releases the one port a test names and takes it back afterwards, checking the board still answers (hil/intellex.py
handed_over); INTELLEX_SERIAL_ALLOW names that port alone, so nothing else on the bench can be opened.

IX-WP6, the bridge end to end: a staged, leashed Intellex host attached to W1's or NaviCore's COM port (attached_host),
with raw /_link clients standing in for the tool pages (hil/intellex.py LinkTap). They read every byte the host fans out
and never log it: through NaviCore that includes GET_CONFIG and its passwords. Streams are compared by length and
SHA-256 only.

The oracle for "nothing reset W1" is W2's own console: a WCB that boots sends three ETM boot announces, and W2 prints
'[ETM] WCB1 came ONLINE (boot)' for each one it hears (WCB.ino, the boot-announce branch of espNowReceiveCallback;
suites/s26_boot_restore.py boot.announce_advert_burst). NaviCore's is its uptime (GET_MESH_STATS upMs, as s46 proves a
restart) and W1's console, where its WCB_Client boot announces print the same line for WCB 20.
"""
import hashlib
import json
import time

from hil.intellex import (NAVICORE_BOOT_MARKERS, WCB_BOOT_MARKERS, IntellexHost, LinkTap, attached_host, copy_logs,
                          handed_over, line_spans, require, run_intellex_py, running_intellex, stage, window)
from hil.navicore import NaviCore
from hil.nc_guard import nc_guard
from hil.runner import Skip, test
from hil.wcb import WCB
from hil.wizard import _kill_tree, _reacquire
from suites.common import link, marker, usb_wcb
from suites.s99_etm import _peers_online

UTF8 = "é€😀"                                  # a 2-, a 3- and a 4-byte UTF-8 character
IDENTIFY_DOORWAY_S = 2.0          # under SERIAL_PONG_WAIT_S + SERIAL_IDENTIFY_S (3.1 s): a NaviCore asked the PING alone
PING = '{"type":"PING"}\n'
WCB_PROBE = {"probe": "?VERSION\r", "reply": "Software Version:"}
NC_PROBE = {"probe": PING, "reply": '"type":"PONG"'}


def _boot_edges(dev, since, wcb):
    """The '[ETM] WCB<wcb> came ONLINE (boot)' lines another board printed since mark `since`: one per boot announce it
    heard, three sent per boot."""
    return [x for x in dev.since(since) if x.startswith(f"[ETM] WCB{wcb} came ONLINE (boot)")]


def _w2(bench):
    """W2's console when the bench has one, else None: the optional witness that W1 did not reboot."""
    return WCB(bench.dev("wcb2")) if bench.has("wcb2") else None


def _sha(b):
    return hashlib.sha256(b).hexdigest()[:12]


# ============================================================================================ IX-WP5: the transports
@test("intellex.serial_no_reset_w1", "Intellex's SerialTransport opens and closes W1's port ten times without resetting "
      "it: no boot line on the stream, no boot announce heard by W2, and W1 still answers through it (IX-WP5)",
      needs=["wcb1", "wcb2"])
def serial_no_reset_w1(bench):
    """Intellex src/serial_transport.py:39-66 sets DTR and RTS low before open() - contract 3, the reason Intellex is
    native (transport.py:23-27). W1 is a CH9102 bridge whose auto-reset circuit takes those lines to EN and IO0."""
    w2 = WCB(bench.dev("wcb2"))
    w1 = bench.usb_wcb_number()
    m2 = w2.dev.mark()
    run_intellex_py(bench, "intellex.serial_no_reset_w1", "transport_serial.py", device="wcb1", timeout=180,
                    args={"group": "no_reset", "kind": "wcb", "cycles": 10, "dwell": 1.5,
                          "boot": list(WCB_BOOT_MARKERS), **WCB_PROBE})
    time.sleep(3.0)                           # a boot's first announce goes ~1.5 s after its ETM block
    edges = _boot_edges(w2.dev, m2, w1)
    assert not edges, f"W2 heard W1 boot {len(edges)} time(s) while Intellex opened and closed W1's port"


@test("intellex.serial_no_reset_navicore", "Intellex's SerialTransport opens and closes NaviCore's native-USB port ten "
      "times without resetting it: no banner on the stream, NaviCore's uptime unbroken, no boot announce on W1 (IX-WP5)",
      needs=["navicore", "wcb1"])
def serial_no_reset_navicore(bench):
    """The reason Intellex exists (Intellex CLAUDE.md 'Why native serial'): Chrome asserts DTR/RTS inside Web Serial's
    open() and the S3's USB-Serial/JTAG resets NaviCore on every page load. Skipped while NaviCore's recorder holds
    anything, as every test that could restart it is (D33): a regression here would restart it."""
    nc = NaviCore(bench.dev("navicore"))
    why = nc.restart_blocker()
    if why:
        raise Skip(why)
    nid = nc.wcb_status()["self"]
    w1 = WCB(bench.dev("wcb1"))
    m1 = w1.dev.mark()
    up0, t0 = nc.mesh_stats()["upMs"], time.monotonic()
    run_intellex_py(bench, "intellex.serial_no_reset_navicore", "transport_serial.py", device="navicore", timeout=180,
                    args={"group": "no_reset", "kind": "navicore", "cycles": 10, "dwell": 1.5,
                          "boot": list(NAVICORE_BOOT_MARKERS), **NC_PROBE})
    nc = NaviCore(bench.dev("navicore"))
    up1, gone = nc.mesh_stats()["upMs"], (time.monotonic() - t0) * 1000
    problems = []
    if up1 < up0 + gone - 3000:
        problems.append(f"NaviCore's uptime went from {up0} to {up1} ms across {gone:.0f} ms: it restarted")
    edges = _boot_edges(w1.dev, m1, nid)
    if edges:
        problems.append(f"W1 heard WCB{nid} boot {len(edges)} time(s)")
    assert not problems, "; ".join(problems)


@test("intellex.serial_contract_w1", "Intellex's SerialTransport contract on W1: only bytes reach on_data, ';S0,<marker>' "
      "with multi-byte UTF-8 comes back byte-exact, set_signals(False, False) resets nothing, a held port cannot be opened "
      "twice, and a write after close() raises (IX-WP5)", needs=["wcb1"])
def serial_contract_w1(bench):
    """Intellex src/transport.py:11-28 (the three contracts) and serial_transport.py:91-119. ;S0 echoes a line's bytes as
    they are, UTF-8 included (processSerialMessage, WCB.ino; suites/s12_serial_input.py LONG_RUNS)."""
    w2 = _w2(bench)
    m2 = w2.dev.mark() if w2 else None
    run_intellex_py(bench, "intellex.serial_contract_w1", "transport_serial.py", device="wcb1", timeout=120,
                    args={"group": "contract", "marker": marker("IXC"), "utf8": UTF8,
                          "boot": list(WCB_BOOT_MARKERS), **WCB_PROBE})
    if w2:
        time.sleep(2.0)
        edges = _boot_edges(w2.dev, m2, bench.usb_wcb_number())
        assert not edges, f"W2 heard W1 boot {len(edges)} time(s) during the contract checks"


@test("intellex.serial_signals_reset_w1", "Characterisation: Intellex's set_signals(rts=True) then (rts=False) resets W1 "
      "(its boot lines arrive through the transport), writing DTR before RTS (1 W1 reset; IX-WP5)", needs=["wcb1"])
def serial_signals_reset_w1(bench):
    """What a page's port.setSignals() does through Intellex (POST /_api/signals -> SerialTransport.set_signals,
    Intellex src/serial_transport.py:108-119): on W1's CH9102 every line write reaches the board. On a native-USB S3
    under Windows' usbser.sys an RTS change alone is not sent until the next DTR write (hil/serialdev.py
    usb_jtag_reset); set_signals writes DTR first, so a DTR+RTS call sends the OLD RTS state - recorded here, not
    exercised on NaviCore. W1 resets once and boots as from a ?reboot; the test ends with every peer online again."""
    try:
        run_intellex_py(bench, "intellex.serial_signals_reset_w1", "transport_serial.py", device="wcb1", timeout=120,
                        args={"group": "signals_reset", "boot": list(WCB_BOOT_MARKERS),
                              "ready": "Raw Serial Forwarding Task Created"})
    finally:
        try:
            _peers_online(bench, usb_wcb(bench), strict=False)
        except AssertionError as e:
            bench.note(f"after W1's reset the peer check failed: {(str(e).splitlines() or [repr(e)])[0]}")


@test("intellex.serial_device_loss_navicore", "A NaviCore REBOOT sent through Intellex's SerialTransport: either the COM "
      "handle dies, on_lost fires and a write raises, or it survives and NaviCore answers on it again - which one is "
      "recorded (opt-in intellex_reboot; 1 NaviCore restart; IX-WP5)", needs=["navicore"], opt_in="intellex_reboot")
def serial_device_loss_navicore(bench):
    """Contract 2 on a device that really goes away (Intellex src/transport.py:18-21, serial_transport.py:122-137: the
    reader closes the port before reporting the loss). REBOOT is ACKed, then ESP.restart() 250 ms later (NaviCore.ino,
    s46 _restart); the S3's native USB re-enumerates, so the handle is expected to die. Runs inside nc_guard and skips
    while the recorder holds anything, as every NaviCore restart does (docs/HIL_TESTING.md §5, D33); the restart is proved
    by NaviCore's uptime afterwards."""
    with nc_guard(bench) as g:
        why = g.nc.restart_blocker()
        if why:
            raise Skip(why)
        t0 = time.monotonic()
        try:
            run_intellex_py(bench, "intellex.serial_device_loss_navicore", "transport_serial.py", device="navicore",
                            timeout=150, args={"group": "device_loss", "boot": list(NAVICORE_BOOT_MARKERS),
                                               **NC_PROBE})
        finally:
            g.nc = NaviCore(bench.dev("navicore"))      # the guard restores through the port as it is now
        up = g.nc.mesh_stats()["upMs"]
        gone = (time.monotonic() - t0) * 1000
        assert up < gone + 5000, f"NaviCore's uptime is {up} ms {gone:.0f} ms after the REBOOT: it did not restart"


@test("intellex.ws_transport_navicore", "Intellex's WebSocketTransport on NaviCore's own access point, the PC's spare "
      "WiFi adapter joined to it for the test: PING gets a direct PONG, a GET_CONFIG over 10 KB is reassembled from "
      "frames and parses, a non-UTF-8 write and a write after close raise (opt-in navicore_wifi; IX-WP5)",
      needs=["navicore"], opt_in="navicore_wifi")
def ws_transport_navicore(bench):
    """INTELLEX.md DX34: the harness puts the PC's spare adapter on NaviCore's access point for the test (s45 _on_ap:
    hil/wlan.py pc_on_ap, a temporary HIL- profile with the SSID and password from NaviCore's GET_CONFIG, spare adapter
    only) and back afterwards; before IX-WP9 the test used the adapter as it was and skipped when it was elsewhere (run
    20260929-122112). The script still proves the AP is NaviCore's by a DIRECT PONG carrying the version NaviCore gives
    over USB (DX19; a WCB's AP at the same address mirrors a mesh PONG, which carries sys and id), and it never changes
    the network itself. No COM port is touched: the harness keeps NaviCore's (tests/intellex/py/transport_ws.py). The
    script's leash allows NaviCore's address alone (INTELLEX_WS_ALLOW, which WebSocketTransport.open checks)."""
    from suites.s45_navicore_wifi import NC_AP_IP, _on_ap
    require(bench)
    others = running_intellex()
    if others:
        raise Skip(f"another Intellex is running (PID {others[0][0]}) and may hold NaviCore's WebSocket - close it first")
    problems = []
    with _on_ap(bench, problems) as (nc, _, _name):
        version = nc.ping()
        run_intellex_py(bench, "intellex.ws_transport_navicore", "transport_ws.py", timeout=120,
                        args={"group": "navicore", "host": NC_AP_IP, "version": version, "joined": True},
                        ws_hosts=[NC_AP_IP])                    # NaviCore's access point alone (INTELLEX_WS_ALLOW)
    assert not problems, "; ".join(problems)


# ============================================================================================ IX-WP6: the bridge
@test("intellex.bridge_wire_w1", "Through an Intellex host on W1's COM port, a ;S1 line from one /_link client comes out "
      "of W1 S1 once, and two clients receive byte-identical streams through 30 s of console and mesh traffic (IX-WP6)",
      needs=["wcb1", "wcb2"])
def bridge_wire_w1(bench):
    """Intellex src/host.py:124-382: every transport chunk goes to every page, whole and in order (one FIFO, one pump),
    and a page's line is written to the transport as it came. Mesh traffic: W2 sends ';W1,;S0,<marker>' over ETM, which
    W1 prints on its USB (suites/s05_mesh.py _mirror). The streams are compared from a sync line both clients saw to an
    end line, by length and SHA-256."""
    wire = link(bench, 1, "S1")
    wire.listen()                              # bound before the host takes W1: binding reads W1's configured baud
    w2 = WCB(bench.dev("wcb2"))
    t = marker("IXW")
    with attached_host(bench, "intellex.bridge_wire_w1", "wcb1") as host:
        a, b = LinkTap(host.port, name="A"), LinkTap(host.port, name="B")
        try:
            time.sleep(0.5)                    # the host binds a page just after its 101 (host.py:1292-1294)
            a.send(f";S0,{t}-SYNC\r")
            for tap in (a, b):
                tap.wait_for(f"{t}-SYNC".encode(), 5)
            m = wire.mark()
            a.send(f";S1{t}\r")
            time.sleep(2.0)
            n = wire.received(m).count(t.encode() + b"\r")
            assert n == 1, f"W1 S1 carried the line from /_link {n} times, not once"
            console, mesh, i = [], [], 0
            end = time.monotonic() + 30.0
            while time.monotonic() < end:
                console.append(f"{t}-A{i:03d}")
                a.send(f";S0,{console[-1]}\r")
                if i % 4 == 0:
                    mesh.append(f"{t}-M{i:03d}")
                    w2.dev.send(f";W1,;S0,{mesh[-1]}")
                i += 1
                time.sleep(0.25)
            time.sleep(2.0)                    # the last mesh lines are an ETM hop behind
            a.send(f";S0,{t}-END\r")
            for tap in (a, b):
                tap.wait_for(f"{t}-END".encode(), 10)
            wa = window(a.snapshot(), f"{t}-SYNC".encode(), f"{t}-END".encode())
            wb = window(b.snapshot(), f"{t}-SYNC".encode(), f"{t}-END".encode())
            assert wa is not None and wb is not None, "a client lost the sync or end line"
            assert wa == wb, (f"the two /_link clients received different streams: {len(wa)} bytes (sha {_sha(wa)}) "
                              f"and {len(wb)} bytes (sha {_sha(wb)})")
            pos = [wa.find(s.encode()) for s in console]
            lost = [s for s, p in zip(console, pos) if p < 0]
            assert not lost, f"{len(lost)} of {len(console)} console lines never came through (first {lost[0]})"
            assert pos == sorted(pos), "the console lines came through out of order"
            heard = sum(1 for s in mesh if s.encode() in wa)
            bench.note(f"intellex.bridge_wire_w1: {len(wa)} identical bytes on both clients; {len(console)} console "
                       f"lines, {heard} of {len(mesh)} mesh lines")
            assert heard, "none of W2's mesh lines reached the /_link clients"
            assert a.closed is None and b.closed is None, f"a /_link closed: {a.closed or b.closed}"
        finally:
            a.close()
            b.close()


@test("intellex.bridge_terminators_w1", "Through an Intellex host on W1, ?VERSION ended with CR, with LF and with CRLF is "
      "answered exactly once each: a line's terminator reaches W1 as the page sent it (IX-WP6)", needs=["wcb1"])
def bridge_terminators_w1(bench):
    """The Wizard ends every command with CR and the NaviCore tool with LF (Intellex CLAUDE.md rule 8); the shim frames per
    line and keeps the terminator (intellex_shim.js:202-212) and the host writes the frame as it is (host.py:1296-1304).
    W1's USB reader takes either and skips the empty line a CRLF leaves (hil/wcb.py: 'End of Version' closes ?VERSION)."""
    t = marker("IXT")
    with attached_host(bench, "intellex.bridge_terminators_w1", "wcb1") as host:
        a = LinkTap(host.port, name="A")
        try:
            got, problems = {}, []
            for name, term in (("CR", "\r"), ("LF", "\n"), ("CRLF", "\r\n")):
                s0, s1 = f"{t}-{name}-0", f"{t}-{name}-1"
                a.send(f";S0,{s0}{term}")
                a.send(f"?VERSION{term}")
                a.send(f";S0,{s1}{term}")
                a.wait_for(s1.encode(), 8)
                seg = window(a.snapshot(), s0.encode(), s1.encode()) or b""
                got[name] = seg.count(b"End of Version")
                if b"Unknown command" in seg:
                    problems.append(f"{name}: W1 printed 'Unknown command'")
            bad = {k: v for k, v in got.items() if v != 1}
            assert not bad and not problems, f"'End of Version' per terminator {got}; {'; '.join(problems)}"
        finally:
            a.close()


@test("intellex.bridge_load_navicore", "Through an Intellex host on NaviCore's COM port, two /_link clients get a PONG and "
      "the whole GET_CONFIG, and through 20 s of the live monitor byte-identical streams (IX-WP6)", needs=["navicore"])
def bridge_load_navicore(bench):
    """The fan-out under load (Intellex src/host.py:322-372): PWM_UPDATE every 50 ms while the monitor runs. NaviCore's
    PONG is the same line each time, so the streams are compared between the 2nd and 3rd PONG, which both clients saw
    (line_spans). GET_CONFIG holds the AP and mesh passwords: it is parsed and compared by length and hash, never quoted.
    Afterwards the monitor is stopped and the debug flags put back to 0, the harness's baseline (RAM only)."""
    try:
        with attached_host(bench, "intellex.bridge_load_navicore", "navicore") as host:
            a, b = LinkTap(host.port, name="A"), LinkTap(host.port, name="B")
            try:
                time.sleep(0.5)
                a.send(PING)
                for tap in (a, b):
                    tap.wait_for(b'"type":"PONG"', 5)
                a.send('{"type":"GET_CONFIG"}\n')
                cfg = {}
                for tap in (a, b):
                    at = tap.wait_for(b'{"type":"CONFIG","data":', 12)
                    end = tap.wait_for(b"\n", 12, start=at)
                    line = tap.snapshot()[at - len(b'{"type":"CONFIG","data":'):end].rstrip(b"\r\n")
                    try:
                        json.loads(line)
                    except ValueError:
                        raise AssertionError(f"client {tap.name}: the {len(line)}-byte CONFIG line does not parse") \
                            from None
                    cfg[tap.name] = (len(line), _sha(line))
                assert cfg["A"] == cfg["B"], f"the two clients got different CONFIG lines: {cfg}"
                a.send(PING)                               # PONG 2: the start of the compared stretch
                for tap in (a, b):
                    _nth_pong(tap, 2, 5)
                a.send('{"type":"START_MONITOR"}\n')
                time.sleep(20.0)
                a.send('{"type":"STOP_MONITOR"}\n')
                time.sleep(0.5)
                a.send(PING)                               # PONG 3: its end
                spans = {}
                for tap in (a, b):
                    _nth_pong(tap, 3, 8)
                    data = tap.snapshot()
                    s = line_spans(data, b'"type":"PONG"')
                    spans[tap.name] = data[s[1][1]:s[2][0]]
                wa, wb = spans["A"], spans["B"]
                assert wa == wb, (f"through the monitor the clients received different streams: {len(wa)} bytes (sha "
                                  f"{_sha(wa)}) and {len(wb)} bytes (sha {_sha(wb)})")
                lines = wa.split(b"\n")
                frames = sum(1 for x in lines if x.startswith(b'{"type":"PWM_UPDATE"'))
                broken = 0
                for x in lines:
                    if x.strip().startswith(b"{"):
                        try:
                            json.loads(x)
                        except ValueError:
                            broken += 1
                bench.note(f"intellex.bridge_load_navicore: {len(wa)} identical bytes, {frames} PWM_UPDATE frames in "
                           f"20 s, CONFIG {cfg['A'][0]} bytes on both" + (f"; {broken} JSON line(s) cut short, identically "
                                                                          f"on both clients (upstream of the fan-out)"
                                                                          if broken else ""))
                assert frames >= 50, f"only {frames} PWM_UPDATE frames reached the clients in 20 s of monitor"
                assert a.closed is None and b.closed is None, f"a /_link closed: {a.closed or b.closed}"
            finally:
                try:
                    a.send('{"type":"STOP_MONITOR"}\n')
                except OSError:
                    pass
                a.close()
                b.close()
    finally:
        try:
            nc = NaviCore(bench.dev("navicore"))
            nc.ack({"type": "STOP_MONITOR"})
            nc.set_debug_flags(0)
        except AssertionError as e:
            bench.note(f"after intellex.bridge_load_navicore the monitor/debug reset failed: {str(e).splitlines()[0]}")


def _nth_pong(tap, n, timeout):
    """Wait until `tap` holds `n` whole PONG lines."""
    deadline = time.monotonic() + timeout
    while len(line_spans(tap.snapshot(), b'"type":"PONG"')) < n:
        if tap.closed is not None:
            raise AssertionError(f"{tap.name}: /_link ended ({tap.closed}) waiting for PONG {n}")
        if time.monotonic() > deadline:
            raise AssertionError(f"{tap.name}: PONG {n} did not arrive within {timeout} s")
        time.sleep(0.05)


def _reopen_within(bench, device, limit):
    """Seconds until the harness could open `device`'s port again and the board answered, or None past `limit`."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < limit:
        try:
            WCB(bench.dev(device)).version()
            return time.monotonic() - t0
        except Exception:  # noqa: BLE001 - still held, or not answering yet
            bench.close_device(device)
            time.sleep(0.1)
    return None


@test("intellex.bridge_release_w1", "An Intellex host lets W1's port go: after /_api/detach the harness reopens it within "
      "2 s, and killing an attached host frees it too, with W1 never reset (IX-WP6)", needs=["wcb1"])
def bridge_release_w1(bench):
    """A host that keeps a port is the COM11 incident (docs/HIL_TEST_AUDIT.md: Intellex.exe held probe2's port for hours).
    Detach closes the transport (host.py:240-268); a killed process's handles are closed by the OS."""
    require(bench)
    others = running_intellex()
    if others:
        raise Skip(f"another Intellex is running (PID {others[0][0]}) - close it first")
    w2 = _w2(bench)
    m2 = w2.dev.mark() if w2 else None
    port = bench.cfg["devices"]["wcb1"]["port"]
    sd = stage(bench, "intellex.bridge_release_w1", tools="none")
    host = IntellexHost(bench, sd, allow_ports=[port])
    problems = []
    try:
        bench.close_device("wcb1")
        host.start()
        host.attach({"kind": "serial", "port": port})
        host.detach()
        took = _reopen_within(bench, "wcb1", 2.0)
        if took is None:
            problems.append("after /_api/detach the harness could not reopen W1's port within 2 s")
        else:
            bench.note(f"intellex.bridge_release_w1: reopened {took:.2f} s after the detach")
        bench.close_device("wcb1")
        host.attach({"kind": "serial", "port": port})
        _kill_tree(host.proc)
        host.proc.wait(10)
        took = _reopen_within(bench, "wcb1", 5.0)
        if took is None:
            problems.append("after the attached host was killed the harness could not reopen W1's port within 5 s")
        else:
            bench.note(f"intellex.bridge_release_w1: reopened {took:.2f} s after the host was killed")
    finally:
        host.stop()
        copy_logs(bench, sd, "intellex.bridge_release_w1")
        _reacquire(bench, "wcb1")
    if w2:
        edges = _boot_edges(w2.dev, m2, bench.usb_wcb_number())
        if edges:
            problems.append(f"W2 heard W1 boot {len(edges)} time(s)")
    assert not problems, "; ".join(problems)


@test("intellex.bridge_failed_attach_w1", "While the harness holds W1's port an Intellex attach is refused (502) and "
      "keeps its target, lastError reads 'reconnect failed: cannot open ...' with that line printed once, detach "
      "forgets it, and once the port is released the host does not take it (IX-WP6; IX re-scan "
      "host.reconnect.retry_loop)",
      needs=["wcb1"])
def bridge_failed_attach_w1(bench):
    """Intellex src/host.py:1136-1204 records the target before trying (set_target), so a refused attach is retried
    every second by reconnect_loop (:1600-1638) until /_api/detach (:1234-1236, Bridge.detach :240-247). Each retry is a
    failed open of a port another process holds: nothing reaches W1. The plan's 'no steal' is the COM11 precedent."""
    require(bench)
    others = running_intellex()
    if others:
        raise Skip(f"another Intellex is running (PID {others[0][0]}) - close it first")
    port = bench.cfg["devices"]["wcb1"]["port"]
    w = usb_wcb(bench)                                  # the harness holds W1's port throughout the refusal
    w.version()
    sd = stage(bench, "intellex.bridge_failed_attach_w1", tools="none")
    host = IntellexHost(bench, sd, allow_ports=[port])
    problems = []
    try:
        host.start()
        st, body = host.json("POST", "/_api/attach", {"kind": "serial", "port": port}, timeout=20)
        if st != 502 or "cannot open" not in str((body or {}).get("error", "")):
            problems.append(f"attach to a held port: {st} {str(body)[:120]}, not a 502 'cannot open'")
        st, s = host.json("GET", "/_api/status")
        if not s.get("wantsLink") or s.get("attached"):
            problems.append(f"after the refusal: wantsLink {s.get('wantsLink')}, attached {s.get('attached')}")
        time.sleep(3.0)                                 # three reconnect attempts, each refused
        s = host.json("GET", "/_api/status")[1]
        if s.get("attached"):
            problems.append("the host attached a port the harness holds")
        # IX re-scan host.reconnect.retry_loop: lastError says why the reattach fails, and the host prints that reason
        # once however many times the loop meets it (reconnect_loop's failed_why).
        if not str(s.get("lastError", "")).startswith("reconnect failed: cannot open"):
            problems.append(f"while the loop is refused lastError reads {str(s.get('lastError'))[:80]!r}, not "
                            f"'reconnect failed: cannot open ...'")
        n = sum(1 for x in host.lines if x.startswith("reconnect failed:"))
        if n != 1:
            problems.append(f"the host printed 'reconnect failed:' {n} times over ~3 refused reattaches with one "
                            f"reason, expected once")
        w.version()                                     # W1 still answers the harness
        host.detach()
        if host.json("GET", "/_api/status")[1].get("wantsLink"):
            problems.append("detach did not forget the target")
        bench.close_device("wcb1")
        time.sleep(3.0)
        s = host.json("GET", "/_api/status")[1]
        if s.get("attached") or s.get("wantsLink"):
            problems.append(f"with the port free and no target, the host took it: {s}")
        if _reopen_within(bench, "wcb1", 3.0) is None:
            problems.append("the harness could not take W1's port back")
    finally:
        host.stop()
        copy_logs(bench, sd, "intellex.bridge_failed_attach_w1")
        _reacquire(bench, "wcb1")
    assert not problems, "; ".join(problems)


@test("intellex.bridge_reboot_w1", "A ?reboot sent through an Intellex host on W1's COM port: the CH9102 stays enumerated, "
      "so the link stays up and the page is not closed, W1's boot lines arrive through it, and W1 answers again (1 W1 "
      "reboot; IX-WP6)", needs=["wcb1"])
def bridge_reboot_w1(bench):
    """The link survives a board restart that does not drop the USB bridge: the host closes pages only when the transport
    drops (host.py:249-287), and a CPU reset behind a CH9102 is not a drop. The reboot is deferred until W1's queue is
    quiet (CLAUDE.md rule 11); the test ends with every peer online again."""
    w2 = _w2(bench)
    m2 = w2.dev.mark() if w2 else None
    try:
        with attached_host(bench, "intellex.bridge_reboot_w1", "wcb1") as host:
            a = LinkTap(host.port, name="A")
            try:
                a.send("?reboot\r")
                a.wait_for(b"Reboot queued", 5)
                seen_down = []
                ready = b"Raw Serial Forwarding Task Created"
                deadline = time.monotonic() + 60.0
                while ready not in a.snapshot():
                    s = host.json("GET", "/_api/status")[1]
                    if not s.get("attached"):
                        seen_down.append(s.get("lastError") or "detached")
                    if a.closed is not None:
                        raise AssertionError(f"the /_link was closed during the reboot ({a.closed})")
                    if time.monotonic() > deadline:
                        raise AssertionError("W1's boot did not come through /_link within 60 s of ?reboot")
                    time.sleep(0.5)
                data = a.snapshot()
                missing = [m for m in (b"Rebooting now", b"Booting up the Wireless Communication Board") if m not in data]
                assert not missing, f"through /_link W1's reboot lacked {missing}"
                assert not seen_down, f"the host's link dropped during the reboot: {seen_down[:2]}"
                time.sleep(2.0)
                mark = len(a.snapshot())
                a.send("?VERSION\r")
                a.wait_for(b"Software Version:", 6, start=mark)
            finally:
                a.close()
    finally:
        try:
            _peers_online(bench, usb_wcb(bench), strict=False)
        except AssertionError as e:
            bench.note(f"after W1's reboot the peer check failed: {(str(e).splitlines() or [repr(e)])[0]}")
    if w2:
        bench.note(f"intellex.bridge_reboot_w1: W2 heard {len(_boot_edges(w2.dev, m2, bench.usb_wcb_number()))} of W1's "
                   f"boot announces")


# ============================================================================================ the 2026-10-12 re-scan's
# low-risk rows, their real-board halves: W1's and NaviCore's own ports through the harness's handover
@test("intellex.ports_real_rows", "A host allowed W1's port (and NaviCore's) lists exactly those in /_api/ports with "
      "the shape the chooser reads: path, a description, vid and pid as integers - 0x1A86 for W1's CH9102 bridge, "
      "0x303A for NaviCore's native USB - and the serial number; no port is opened (IX re-scan host.api.ports)",
      needs=["wcb1"])
def ports_real_rows(bench):
    """Intellex src/host.py api_ports (:404-410) and transport.py list_serial_ports (:100-120), filtered by
    INTELLEX_SERIAL_ALLOW. Listing reads the OS's device list; the harness keeps both ports open throughout."""
    require(bench)
    w1 = bench.cfg["devices"]["wcb1"]["port"]
    want = {w1: 0x1A86}
    if bench.has("navicore"):
        want[bench.cfg["devices"]["navicore"]["port"]] = 0x303A
    sd = stage(bench, "intellex.ports_real_rows", tools="none")
    problems = []
    with IntellexHost(bench, sd, allow_ports=list(want)) as host:
        st, body = host.json("GET", "/_api/ports")
        rows = (body or {}).get("ports") if isinstance(body, dict) else None
        if st != 200 or not isinstance(rows, list):
            raise AssertionError(f"/_api/ports: {st} {str(body)[:200]}")
        paths = sorted(r.get("path") for r in rows)
        if paths != sorted(want):
            problems.append(f"/_api/ports listed {paths}, expected exactly the allowed {sorted(want)}")
        for r in rows:
            if r.get("path") not in want:
                continue
            shape = {k: type(r.get(k)).__name__ for k in ("path", "description", "vid", "pid", "serial_number")}
            if shape != {"path": "str", "description": "str", "vid": "int", "pid": "int", "serial_number": "str"}:
                problems.append(f"{r.get('path')}: field types {shape}")
            if r.get("vid") != want[r.get("path")]:
                problems.append(f"{r.get('path')}: vid {r.get('vid')!r}, expected {want[r.get('path')]:#06x}")
            bench.note(f"intellex.ports_real_rows: {r.get('path')} vid {r.get('vid'):#06x} pid {r.get('pid'):#06x} "
                       f"{r.get('description')!r}" if isinstance(r.get("vid"), int) and isinstance(r.get("pid"), int)
                       else f"intellex.ports_real_rows: {r}")
    assert not problems, "; ".join(problems)


@test("intellex.attach_role_w1", "Attached to W1's own COM port, the host asks it what it is before the attach answers: "
      "the reply and /_api/status say role wcb with W1's WCB number as relayId (from its ?WDP,DUMP self row), the host "
      "named it a WCB, and W1 was never reset (IX re-scan host.api.attach_serial_identify_role)", needs=["wcb1"])
def attach_role_w1(bench):
    """Intellex src/host.py _identify_attached_serial (:1161-1236) and api_attach (:1239-1311) on real hardware: the
    PING, then ?RELAY,WIFI and ?WDP,DUMP (W1 answers no direct PONG), read through a bridge tap; discover.classify takes
    the WDP self row's number. The questions and answers cannot be watched from a /_link client: the attach closes every
    page socket before it opens the port (Bridge.attach -> _drop), and the identify is over before it answers. The
    relayId can only have come from W1's self row. The witness that W1 did not reset is W2's console (_boot_edges)."""
    w2 = _w2(bench)
    m2 = w2.dev.mark() if w2 else None
    n = bench.usb_wcb_number()
    problems = []
    with attached_host(bench, "intellex.attach_role_w1", "wcb1", attach=False) as host:
        port = bench.cfg["devices"]["wcb1"]["port"]
        t0 = time.monotonic()
        reply = host.attach({"kind": "serial", "port": port}, timeout=20)
        took = time.monotonic() - t0
        st = host.json("GET", "/_api/status")[1]
        if reply.get("role") != "wcb" or reply.get("target") != f"serial {port}":
            problems.append(f"the attach answered {reply}, expected role wcb on serial {port}")
        got = {k: st.get(k) for k in ("attached", "kind", "role", "relayId")}
        if got != {"attached": True, "kind": "serial", "role": "wcb", "relayId": n}:
            problems.append(f"/_api/status after the attach: {got}, expected role wcb, relayId {n}")
        said = [x for x in host.lines if x.startswith(f"{port} is ")]
        if not said or "a WCB" not in said[-1]:
            problems.append(f"the host's identify line: {said[-1:] or 'none'}, expected '{port} is ..., a WCB'")
        bench.note(f"intellex.attach_role_w1: the attach with its identify took {took:.2f} s")
    if w2:
        time.sleep(2.0)
        edges = _boot_edges(w2.dev, m2, n)
        if edges:
            problems.append(f"W2 heard W1 boot {len(edges)} time(s) during the attach")
    assert not problems, "; ".join(problems)


@test("intellex.identify_navicore_port", "On NaviCore's own COM port: POST /_api/identify with no port is 400; a host "
      "allowed the port but not attached probes it - without restarting NaviCore - and answers its PONG version (busy "
      "false); attached, the host's identify asked the PING alone (the attach well under the 3 s the doorway questions "
      "take), /_api/status says role navicore with no relayId, a PING through the link gets a PONG and no 'Unknown "
      "command', and identify of that port answers {attached:true} without probing; NaviCore never restarted (IX "
      "re-scan host.api.identify, host.api.attach_serial_identify_role)", needs=["navicore"])
def identify_navicore_port(bench):
    """Intellex src/host.py api_identify (:1314-1338) and discover.identify_serial (:143-238: DTR/RTS deasserted before
    the open, a direct PONG only), then _identify_attached_serial (:1161-1236: the PING alone first). The version is
    compared with NaviCore's PONG over USB just before the handover; NaviCore's uptime must keep counting and W1 must
    not hear it boot. Skips while NaviCore's recorder holds anything (D33). Found writing it (GUI run 20261013-001422):
    on macOS identify_serial's 'both lines low before open' restarted NaviCore at every probe - every chooser scan -
    fixed in Intellex (discover.identify_serial now opens as SerialTransport does)."""
    nc = NaviCore(bench.dev("navicore"))
    why = nc.restart_blocker()
    if why:
        raise Skip(why)
    version = nc.ping()
    nid = nc.wcb_status()["self"]
    up0, t0 = nc.mesh_stats()["upMs"], time.monotonic()
    w1 = WCB(bench.dev("wcb1")) if bench.has("wcb1") else None
    m1 = w1.dev.mark() if w1 else None
    port = bench.cfg["devices"]["navicore"]["port"]
    problems = []
    with attached_host(bench, "intellex.identify_navicore_port", "navicore", attach=False) as host:
        st, body = host.json("POST", "/_api/identify", {})
        if st != 400:
            problems.append(f"identify with no port: {st} {body}, expected 400")
        st, body = host.json("POST", "/_api/identify", {"port": port}, timeout=20)
        if st != 200 or body != {"ok": True, "version": version, "busy": False}:
            problems.append(f"identify of NaviCore's port, unattached: {st} {body}, expected ok with the PONG's "
                            f"version {version!r}")
        t_att = time.monotonic()
        reply = host.attach({"kind": "serial", "port": port}, timeout=20)
        took = time.monotonic() - t_att
        if reply.get("role") != "navicore":
            problems.append(f"the attach answered {reply}, expected role navicore")
        st = host.json("GET", "/_api/status")[1]
        got = {k: st.get(k) for k in ("attached", "kind", "role", "relayId")}
        if got != {"attached": True, "kind": "serial", "role": "navicore", "relayId": None}:
            problems.append(f"/_api/status after the attach: {got}, expected role navicore, no relayId")
        # The PING alone: classify stops on the direct PONG. Had the doorway's questions followed, NaviCore would have
        # answered 'Unknown command' to each and the identify would have waited out SERIAL_PONG_WAIT_S (0.6 s) and
        # then SERIAL_IDENTIFY_S (2.5 s) for a self row that never comes - an attach of over 3 s. (No /_link client
        # can watch it: the attach closes every page socket before it opens the port.)
        if took >= IDENTIFY_DOORWAY_S:
            problems.append(f"the attach took {took:.2f} s: NaviCore was asked the doorway's questions after its "
                            f"direct PONG (a PING alone answers well under {IDENTIFY_DOORWAY_S:g} s)")
        tap = LinkTap(host.port, name="rawLink")
        try:
            time.sleep(0.5)
            mark = len(tap.snapshot())
            tap.send(PING)
            tap.wait_for(b'"type":"PONG"', 5, start=mark)
            if b"Unknown command" in tap.snapshot():
                problems.append("an 'Unknown command' line came through the link after the attach")
        except AssertionError as e:
            problems.append(f"a PING through the attached link: {e}")
        finally:
            tap.close()
        st, body = host.json("POST", "/_api/identify", {"port": port})
        if st != 200 or body != {"ok": True, "attached": True, "version": None}:
            problems.append(f"identify of the port the host holds: {st} {body}, expected {{ok, attached, version null}}")
        bench.note(f"intellex.identify_navicore_port: the attach with its identify took {took:.2f} s")
    nc = NaviCore(bench.dev("navicore"))
    up1, gone = nc.mesh_stats()["upMs"], (time.monotonic() - t0) * 1000
    if up1 < up0 + gone - 3000:
        problems.append(f"NaviCore's uptime went from {up0} to {up1} ms across {gone:.0f} ms: it restarted")
    if w1:
        edges = _boot_edges(w1.dev, m1, nid)
        if edges:
            problems.append(f"W1 heard WCB{nid} boot {len(edges)} time(s)")
    assert not problems, "; ".join(problems)


@test("intellex.host_cli_serial_w1", "host.py --serial <W1's port>: the target is attached and identified before the "
      "host serves - its first /_api/status is attached, role wcb with W1's number, and the 'attached' and identify "
      "lines come before 'serving' - and W1 was never reset (IX re-scan host.main.cli)", needs=["wcb1"])
def host_cli_serial_w1(bench):
    """Intellex src/host.py main() (:2020-2080): set_target, then attach and _identify_attached_serial before
    web.run_app. intellex.host_cli covers the refusals and --ws without a board; this is the row's W1 half, the port
    released to the host by the harness and taken back after (handed_over)."""
    require(bench)
    others = running_intellex()
    if others:
        raise Skip(f"another Intellex is running (PID {others[0][0]}) - close it first")
    w2 = _w2(bench)
    m2 = w2.dev.mark() if w2 else None
    n = bench.usb_wcb_number()
    tid = "intellex.host_cli_serial_w1"
    sd = stage(bench, tid, tools="none")
    problems = []
    with handed_over(bench, "wcb1") as port:
        host = IntellexHost(bench, sd, allow_ports=[port], args=["--serial", port])
        try:
            host.start()
            st, body = host.json("GET", "/_api/status")
            got = {k: body.get(k) for k in ("attached", "kind", "role", "relayId", "target")}
            want = {"attached": True, "kind": "serial", "role": "wcb", "relayId": n, "target": f"serial {port}"}
            if got != want:
                problems.append(f"the first /_api/status: {got}, expected {want}")
            lines = list(host.lines)
            at = next((i for i, x in enumerate(lines) if x == f"attached  serial {port}"), None)
            said = next((i for i, x in enumerate(lines) if x.startswith(f"{port} is ") and "a WCB" in x), None)
            serving = next((i for i, x in enumerate(lines) if x.startswith("serving")), None)
            if None in (at, said, serving) or not at < said < serving:
                problems.append(f"startup lines: 'attached' at {at}, '{port} is ..., a WCB' at {said}, 'serving' at "
                                f"{serving}; expected all three in that order")
        finally:
            host.stop()
            copy_logs(bench, sd, tid)
    if w2:
        time.sleep(2.0)
        edges = _boot_edges(w2.dev, m2, n)
        if edges:
            problems.append(f"W2 heard W1 boot {len(edges)} time(s)")
    assert not problems, "; ".join(problems)

