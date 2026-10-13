"""Intellex's host routes, its reconnect loop and its command line, in-process under Intellex's venv, with every port,
network and OS question stood in for. Intellex's real build_app() runs on 127.0.0.1:<free> in this process
(bridge_units._Server, its reconnect loop running), its bridge's transports are fakes, and discover's OS questions are
patched per case - no COM port is opened, nothing is sent to a droid, no adapter is touched.

group "ws_ssid" (intellex.attach_ws_ssid): host.py api_attach (:1269-1285). A ws attach asks the OS which network
carries the host (discover.ssid_for_host, Windows only) and uses the body's ssid only when the OS cannot say; a blank or
non-string ssid leaves no key. The spec's ssid is what the bounce and the move check compare later.

group "identify_route" (intellex.identify_route): host.py api_identify (:1309-1333). No port, or a port that is not a
string, is 400; the port the host itself holds answers {ok:true, attached:true, version:null} without a probe; any
other port is probed through discover.identify_serial, here against a fake pyserial Serial: a direct PONG, a busy port,
a silent one, one outside INTELLEX_SERIAL_ALLOW (never opened).

group "update_frozen" (half of intellex.update_routes): host.py api_update_webui/_firmware/_wiki (:636-669, :828-854,
:912-965) in a frozen build re-invoke the app itself with --run-fetch-webui --tool <t>, --run-fetch-firmware and
--run-fetch-wiki --wiki all (sys.frozen patched, proc.run recorded); app.py main() hands each sentinel, and
--run-esptool, to the module it names with the rest of argv (stand-in modules).

group "bounce" (intellex.reconnect_auto_bounce): host.py reconnect_loop's self-heal (:1810-1871). A ws link whose
reattach fails BOUNCE_AFTER_FAILS (2) times in a row while the OS would route to the host off its subnet re-associates
the adapter (discover.wifi_bounce), asking the OS for the network first and falling back to the recorded one, at most
once per BOUNCE_COOLDOWN_S, giving up after BOUNCE_GIVE_UP (3) failed bounces with one line; a move to another network
or a bounce that works resets the count; on-subnet routing, a serial link and auto_bounce False never bounce. The
cooldown is shortened to 2.5 s for the run (a module global the loop reads each pass); everything else is as shipped.
The host is 192.0.2.1 (TEST-NET-1): nothing is ever sent there, every discover question is answered here.

group "cli" (half of intellex.host_cli): host.py main() (:2015-2074) in-process, web.run_app stood in for: --ssid rides
into a --ws target's spec (default NaviCore), --no-auto-bounce clears host.auto_bounce, a --serial target is attached
and asked what it is before serving, and the startup lines say '** OUT OF DATE **' when Pages has a newer config tool
(flash.reachable and urlopen answered here).

group "app" (half of intellex.app_browser_mode): app.py main() (:141-387) in-process, the window toolkit, the browser and
the Dock icon stood in for: a server that cannot start releases the --ws target attached before it and exits 1; with no
pywebview, or a window that cannot be made or started, the default browser gets the landing page ('/_launcher', or '/'
with a target); in window mode the window is 'Intellex' on that page, pywebview keeps its storage under the data
directory (private_mode off), --dev turns on its debug flag, --inspect sets WebView2's debugging port, and the
open-window hook makes a second window on a local path.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ixpy  # noqa: E402
from _ixpy import case, check  # noqa: E402
from bridge_units import _STATE, _board_class, _gate_class, _host, _req_thread, _script, _server  # noqa: E402

ABSENT = "<no ssid key>"


# ------------------------------------------------------------------ group "ws_ssid"
@case("a ws attach records the network the OS names for the host; the body's ssid only when the OS cannot say, "
      "stripped; a blank or non-string ssid leaves no key", group="ws_ssid")
def ws_ssid(ctx):
    import discover
    s = _server()
    host = _host()
    Gate = _gate_class()
    s.make = lambda spec, on_data, on_lost: Gate("WS", block=False, on_data=on_data, on_lost=on_lost)
    asked, os_says = [], {"v": None}

    def ssid_for_host(h):
        asked.append(h)
        return os_says["v"]
    real = discover.ssid_for_host
    discover.ssid_for_host = ssid_for_host
    problems = []
    try:
        for os_ssid, body_ssid, want in ((None, "HILBODY", "HILBODY"),     # macOS: the OS cannot say
                                         ("HILOS", "HILBODY", "HILOS"),    # the OS wins over the body
                                         ("HILOS", None, "HILOS"),
                                         ("", "HILBODY", "HILBODY"),       # an empty answer is no answer
                                         (None, "  HILPAD  ", "HILPAD"),
                                         (None, "", ABSENT), (None, "   ", ABSENT), (None, None, ABSENT),
                                         (None, 5, ABSENT)):
            os_says["v"] = os_ssid
            del asked[:]
            body = {"kind": "ws", "host": "127.0.0.1", "role": "navicore"}
            if body_ssid is not None:
                body["ssid"] = body_ssid
            code, reply = s.req("POST", "/_api/attach", body)
            spec = dict(host.bridge._spec or {})
            s.req("POST", "/_api/detach", {})
            got = spec.get("ssid", ABSENT)
            label = f"OS {os_ssid!r}, body {body_ssid!r}"
            if code != 200 or not (reply or {}).get("ok"):
                problems.append(f"{label}: the attach answered {code} {reply}")
            if got != want:
                problems.append(f"{label}: the spec's ssid is {got!r}, expected {want!r}")
            if asked != ["127.0.0.1"]:
                problems.append(f"{label}: ssid_for_host was asked {asked}, expected once for the attach's host")
    finally:
        discover.ssid_for_host = real
        s.req("POST", "/_api/detach", {})
    check(not problems, "; ".join(problems))


# ------------------------------------------------------------------ group "identify_route"
class _FakeSerial:
    """What discover.identify_serial uses of serial.Serial (as discover_units.py's): the PING's reply comes from
    `reply`, `fail_open` raises from open(); every port opened is recorded."""
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
        if not self._out:
            time.sleep(0.05)
        out, self._out = self._out[:n], self._out[n:]
        return out

    def reset_input_buffer(self):
        pass

    def flush(self):
        pass

    def close(self):
        pass


@contextlib.contextmanager
def _fake_pyserial(reply=b"", fail_open=None):
    import serial
    real = serial.Serial
    _FakeSerial.reply, _FakeSerial.fail_open, _FakeSerial.opened = reply, fail_open, []
    serial.Serial = _FakeSerial
    try:
        yield _FakeSerial.opened
    finally:
        serial.Serial = real


@case("POST /_api/identify: no port is 400; the port the host holds answers attached without a probe; another port "
      "is probed - a direct PONG, a busy port, a silent one, one outside INTELLEX_SERIAL_ALLOW never opened",
      group="identify_route")
def identify_route(ctx):
    import discover
    import serial
    s = _server()
    problems = []
    for body in ({}, {"port": ""}, {"port": 5}, {"port": None}, {"port": ["COMFAKE"]}):
        code, reply = s.req("POST", "/_api/identify", body)
        if code != 400 or reply != {"ok": False, "error": "port required"}:
            problems.append(f"identify {body}: {code} {reply}, expected 400 'port required'")
    code, _, raw = s.http("POST", "/_api/identify", b"not json")
    if code != 400:
        problems.append(f"identify with a body that is not JSON: {code} {raw[:80]!r}, expected 400")

    allow = os.environ.get("INTELLEX_SERIAL_ALLOW")
    os.environ["INTELLEX_SERIAL_ALLOW"] = "COMFAKE,COMOTHER,COMBUSY,COMQUIET"
    real_identify = discover.identify_serial
    Board = _board_class()
    s.make = lambda spec, on_data, on_lost: Board("navicore", _script("navicore"), on_data, on_lost)
    try:
        code, reply = s.req("POST", "/_api/attach", {"kind": "serial", "port": "COMFAKE"})
        check(code == 200 and s.status().get("attached"), f"the fake COMFAKE did not attach: {code} {reply}")
        probed = []
        discover.identify_serial = lambda port, *a, **k: probed.append(port) or {"version": "v0.0.0_probed",
                                                                                 "busy": False}
        code, reply = s.req("POST", "/_api/identify", {"port": "COMFAKE"})
        if code != 200 or reply != {"ok": True, "attached": True, "version": None}:
            problems.append(f"identify of the port the host holds: {code} {reply}, expected "
                            f"{{ok: true, attached: true, version: null}}")
        if probed:
            problems.append(f"identify of the port the host holds probed it ({probed}): it would read as 'in use by "
                            f"something else', the something else being this app")
        code, reply = s.req("POST", "/_api/identify", {"port": "COMOTHER"})
        if probed != ["COMOTHER"] or reply != {"ok": True, "version": "v0.0.0_probed", "busy": False}:
            problems.append(f"identify of another port while attached: probed {probed}, answered {reply}")
        discover.identify_serial = real_identify

        # The real probe, through the route, on a fake pyserial.
        for port, kw, want, opens in (
                ("COMOTHER", {"reply": b'{"type":"rc_hb"}\n{"type":"PONG","version":"v0.2.0_hilroute"}\n'},
                 {"ok": True, "version": "v0.2.0_hilroute", "busy": False}, ["COMOTHER"]),
                ("COMBUSY", {"fail_open": serial.SerialException("[Errno 16] could not open port: Resource busy")},
                 {"ok": False, "version": None, "busy": True}, ["COMBUSY"]),
                ("COMQUIET", {}, {"ok": False, "version": None, "busy": False}, ["COMQUIET"]),
                ("COMNOTALLOWED", {"reply": b'{"type":"PONG","version":"v9"}\n'},
                 {"ok": False, "version": None, "busy": False}, [])):
            with _fake_pyserial(**kw) as opened:
                t0 = time.monotonic()
                code, reply = s.req("POST", "/_api/identify", {"port": port})
                took = time.monotonic() - t0
            if code != 200 or reply != want:
                problems.append(f"identify {port}: {code} {reply}, expected {want}")
            if opened != opens:
                problems.append(f"identify {port} opened {opened}, expected {opens}")
            if port == "COMQUIET" and took < 2.0:
                problems.append(f"a silent port answered after {took:.1f} s, before identify_serial's 2.5 s wait")
        s.req("POST", "/_api/detach", {})
        with _fake_pyserial(reply=b'{"type":"PONG","version":"v0.2.0_free"}\n') as opened:
            code, reply = s.req("POST", "/_api/identify", {"port": "COMFAKE"})
        if opened != ["COMFAKE"] or reply != {"ok": True, "version": "v0.2.0_free", "busy": False}:
            problems.append(f"identify of COMFAKE once the host let it go: opened {opened}, answered {reply} - the "
                            f"short-circuit is for a port the host holds, not one it once held")
    finally:
        discover.identify_serial = real_identify
        if allow is None:
            os.environ.pop("INTELLEX_SERIAL_ALLOW", None)
        else:
            os.environ["INTELLEX_SERIAL_ALLOW"] = allow
        s.req("POST", "/_api/detach", {})
    check(not problems, "; ".join(problems))


# ------------------------------------------------------------------ group "update_frozen"
@case("a frozen build's update routes re-invoke the app with --run-fetch-webui --tool <t>, --run-fetch-firmware and "
      "--run-fetch-wiki --wiki all; their exit code and output make the reply", group="update_frozen")
def update_frozen(ctx):
    import proc
    s = _server()
    runs = []
    rc = {"v": 0}

    def run(argv, **k):
        runs.append(list(argv))
        return subprocess.CompletedProcess(argv, rc["v"], stdout=f"HIL stub {argv[1]} rc={rc['v']}\n", stderr="")
    real_run, had = proc.run, hasattr(sys, "frozen")
    proc.run, sys.frozen = run, True
    problems = []
    try:
        for path, want in (("/_api/update-webui?tool=wcb", [sys.executable, "--run-fetch-webui", "--tool", "wcb"]),
                           ("/_api/update-webui", [sys.executable, "--run-fetch-webui", "--tool", "all"]),
                           ("/_api/update-firmware", [sys.executable, "--run-fetch-firmware"]),
                           ("/_api/update-wiki", [sys.executable, "--run-fetch-wiki", "--wiki", "all"])):
            for code_want in (0, 1):
                rc["v"] = code_want
                del runs[:]
                code, reply = s.req("POST", path, {})
                if runs != [want]:
                    problems.append(f"POST {path} (frozen) ran {runs}, expected {want}")
                if bool(reply.get("ok")) != (code_want == 0):
                    problems.append(f"POST {path} with the fetcher exiting {code_want}: ok={reply.get('ok')}")
                text = reply.get("log", "") if code_want == 0 else reply.get("error", "")
                if f"rc={code_want}" not in text:
                    problems.append(f"POST {path} with the fetcher exiting {code_want}: its output is not in the "
                                    f"reply's {'log' if code_want == 0 else 'error'} ({text[:80]!r})")
        del runs[:]
        code, reply = s.req("POST", "/_api/update-webui?tool=bogus", {})
        if code != 400 or runs:
            problems.append(f"update-webui?tool=bogus (frozen): {code} {reply}, ran {runs}")
    finally:
        proc.run = real_run
        if had:
            pass
        else:
            del sys.frozen
    check(not problems, "; ".join(problems))


@case("app.py main() hands --run-esptool, --run-fetch-webui, --run-fetch-firmware and --run-fetch-wiki to the module "
      "each names, with the rest of argv, before anything else starts", group="update_frozen")
def app_sentinels(ctx):
    import app
    seen = []

    def stub(name, rc):
        m = types.ModuleType(name)
        m.main = (lambda *a: seen.append((name, list(sys.argv), list(a))) or rc)
        return m
    saved_argv = list(sys.argv)
    saved = {n: sys.modules.get(n) for n in ("esptool", "fetch_webui", "fetch_firmware", "fetch_wiki")}
    problems = []
    try:
        for sentinel, name, rest, want_argv, want_args in (
                ("--run-esptool", "esptool", ["--chip", "esp32", "chip-id"], ["esptool", "--chip", "esp32", "chip-id"],
                 [["--chip", "esp32", "chip-id"]]),
                ("--run-fetch-webui", "fetch_webui", ["--tool", "wcb"], ["fetch_webui", "--tool", "wcb"], []),
                ("--run-fetch-firmware", "fetch_firmware", [], ["fetch_firmware"], []),
                ("--run-fetch-wiki", "fetch_wiki", ["--wiki", "all"], ["fetch_wiki", "--wiki", "all"], [])):
            sys.modules[name] = stub(name, 7)
            del seen[:]
            sys.argv = ["Intellex", sentinel] + rest
            got = app.main()
            if seen != [(name, want_argv, want_args)] or got != 7:
                problems.append(f"{sentinel}: {seen} returned {got}, expected {name}.main() with sys.argv {want_argv} "
                                f"and its exit code 7")
    finally:
        sys.argv = saved_argv
        for n, m in saved.items():
            if m is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = m
    check(not problems, "; ".join(problems))


# ------------------------------------------------------------------ group "bounce"
HOST = "192.0.2.1"                   # TEST-NET-1: never contacted, every question about it is answered here
ON_NET, OFF_NET = "192.0.2.7", "10.9.8.7"


class _FakeOS:
    """discover's OS questions for the reconnect loop: the route, the network name, the bounce, the probe."""

    def __init__(self):
        self.via = OFF_NET
        self.os_ssid = None
        self.results = []                # what each bounce returns, in turn; then (False, ...)
        self.bounces = []                # (seconds since t0, ssid)
        self.probe = {"kind": "navicore", "version": "v0.2.0_hil", "relayId": None, "alias": None}
        self.t0 = time.monotonic()

    def local_ip_for(self, host):
        return self.via

    def ssid_for_host(self, host):
        return self.os_ssid

    def wifi_bounce(self, ssid):
        self.bounces.append((round(time.monotonic() - self.t0, 2), ssid))
        return self.results.pop(0) if self.results else (False, "HIL: no adapter is on that network")

    def do_probe(self, host, *a, **k):
        return dict(self.probe)


def _wait(pred, timeout, step=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return pred()


@case("the reconnect loop re-associates a ws link's adapter after 2 failed reattaches while routed off the host's "
      "subnet, once per cooldown, asking the OS for the network first; it gives up after 3 failed bounces with one "
      "line; a move or a bounce that works resets the count; on-subnet routing, a serial link and auto_bounce False "
      "never bounce", group="bounce")
def bounce(ctx):
    import discover
    from transport import TransportError
    s = _server()
    host = _host()
    out = _STATE["out"]
    Gate = _gate_class()
    fos = _FakeOS()
    built = []

    class Dead(Gate):
        def open(self):
            self.opened += 1
            raise TransportError("HIL: nothing answers there")

    def make(spec, on_data, on_lost):
        built.append(dict(spec))
        return Dead("dead", block=False, on_data=on_data, on_lost=on_lost)
    s.make = make
    real = {n: getattr(discover, n) for n in ("local_ip_for", "ssid_for_host", "wifi_bounce", "probe", "tcp_open")}
    discover.local_ip_for, discover.ssid_for_host = fos.local_ip_for, fos.ssid_for_host
    discover.wifi_bounce, discover.probe = fos.wifi_bounce, fos.do_probe
    discover.tcp_open = lambda *a, **k: False
    cooldown0, auto0 = host.BOUNCE_COOLDOWN_S, host.auto_bounce
    host.BOUNCE_COOLDOWN_S = 2.5
    cool = host.BOUNCE_COOLDOWN_S
    problems = []

    def gaps(rows):
        return [round(b[0] - a[0], 2) for a, b in zip(rows, rows[1:])]
    try:
        check(host.BOUNCE_AFTER_FAILS == 2 and host.BOUNCE_GIVE_UP == 3,
              f"BOUNCE_AFTER_FAILS {host.BOUNCE_AFTER_FAILS}, BOUNCE_GIVE_UP {host.BOUNCE_GIVE_UP}: this case is "
              f"written for 2 and 3")

        # ── off-subnet, every bounce failing: the OS names the network once, then cannot ───────────────────────
        # First, on the loop's fresh counts: its failure count and give-up count outlive a detach and a new attach
        # (they are reconnect_loop locals, reset only by a bounce, a move or a reattach that works).
        k = len(out.text)
        fos.os_ssid = None               # at the attach: the body's name is recorded
        n0 = len(built)
        code, _ = s.req("POST", "/_api/attach", {"kind": "ws", "host": HOST, "role": "navicore", "ssid": "HILNET"})
        check(code == 502, f"an attach to a transport that cannot open answered {code}")
        t_attach = time.monotonic() - fos.t0
        fos.os_ssid = "HILOS"            # the first bounce asks the OS, which can say

        def first_bounce_asked():
            if fos.bounces and fos.os_ssid is not None:
                fos.os_ssid = None       # ...and from then on cannot
            return len(fos.bounces) >= 3
        _wait(first_bounce_asked, 20, step=0.02)
        _wait(lambda: False, 2 * cool + 2)                 # long enough for a fourth, were the give-up broken
        a = list(fos.bounces)
        lines = out.since(k)
        if [b[1] for b in a] != ["HILOS", "HILNET", "HILNET"]:
            problems.append(f"off-subnet with every bounce failing the loop bounced {[b[1] for b in a]}, expected "
                            f"['HILOS', 'HILNET', 'HILNET']: the OS's name first, the recorded one when it cannot say, "
                            f"and no fourth after BOUNCE_GIVE_UP")
        if a and a[0][0] - t_attach < 1.5:
            problems.append(f"the first bounce came {a[0][0] - t_attach:.1f} s after the attach: before "
                            f"BOUNCE_AFTER_FAILS (2) failed reattaches, one a second")
        if any(g < cool - 0.05 for g in gaps(a)):
            problems.append(f"bounces {gaps(a)} s apart, inside the {cool} s cooldown")
        if lines.count("giving up on re-associating") != 1:
            problems.append(f"the give-up line was printed {lines.count('giving up on re-associating')} times after "
                            f"3 failed bounces, expected once")
        if "re-associating HILOS: FAILED" not in lines or "routed via 10.9.8.7 instead of 192.0.2.1" not in lines:
            problems.append("no 'routed via 10.9.8.7 instead of 192.0.2.1 - re-associating HILOS: FAILED' line")
        ctx.note(f"reconnect_auto_bounce: off-subnet bounces at {[round(b[0] - t_attach, 2) for b in a]} s after the "
                 f"attach, {len(built) - n0} reattaches built by then")

        # ── on-subnet on another network: a move (resets the give-up); a failing reattach there is never bounced ─
        fos.via, fos.os_ssid = ON_NET, "HILNET2"
        k = len(out.text)
        n_b = len(fos.bounces)
        _wait(lambda: 'network changed "HILNET" -> "HILNET2"' in out.since(k), 6)
        if 'network changed "HILNET" -> "HILNET2"' not in out.since(k):
            problems.append("on the new network, no 'network changed \"HILNET\" -> \"HILNET2\"' line: the move "
                            "check did not run")
        _wait(lambda: False, 2 * cool + 1.5)
        if len(fos.bounces) != n_b:
            problems.append(f"routed on the host's subnet the loop bounced {fos.bounces[n_b:]}")
        spec = dict(host.bridge._spec or {})
        if spec.get("ssid") != "HILNET2":
            problems.append(f"after the move the spec records ssid {spec.get('ssid')!r}, expected 'HILNET2'")
        s.req("POST", "/_api/detach", {})

        # ── a serial link failing, off-subnet: never bounced (no adapter carries a COM port) ───────────────────
        fos.via, fos.os_ssid = OFF_NET, None
        n0 = len(built)
        s.req("POST", "/_api/attach", {"kind": "serial", "port": "COMHILDEAD"})
        _wait(lambda: len(built) >= n0 + int(2 * cool) + 2, 3 * cool + 4)
        if len(fos.bounces) != n_b:
            problems.append(f"a failing serial link was bounced: {fos.bounces[n_b:]}")
        s.req("POST", "/_api/detach", {})

        # ── auto_bounce False (--no-auto-bounce): a ws link failing off-subnet is never bounced ─────────────────
        host.auto_bounce = False
        n0 = len(built)
        s.req("POST", "/_api/attach", {"kind": "ws", "host": HOST, "role": "navicore", "ssid": "HILNET2"})
        _wait(lambda: len(built) >= n0 + int(2 * cool) + 2, 3 * cool + 4)
        if len(fos.bounces) != n_b:
            problems.append(f"with auto_bounce False the loop bounced {fos.bounces[n_b:]}")

        # ── auto_bounce back on: the move above reset the give-up, so it bounces again, by the recorded name ────
        host.auto_bounce = True
        _wait(lambda: len(fos.bounces) >= n_b + 1, 3 * cool + 4)
        if len(fos.bounces) < n_b + 1:
            problems.append("after a move to another network the loop never bounced again: the give-up reached on "
                            "the old network still held (host.py resets bounce_fails on a move)")
        elif fos.bounces[n_b][1] != "HILNET2":
            problems.append(f"after the move the bounce asked for {fos.bounces[n_b][1]!r}, expected the recorded "
                            f"'HILNET2'")

        # ── one failed bounce so far here; a bounce that works resets the count, then three failures give up ──────
        fos.results = [(True, "HIL: power-cycled")]
        n_c = len(fos.bounces)
        k = len(out.text)
        _wait(lambda: "giving up on re-associating" in out.since(k), 6 * cool + 10, step=0.05)
        _wait(lambda: False, 2 * cool + 1)
        c = fos.bounces[n_c:]
        if len(c) != 4:
            problems.append(f"after one failed bounce, one that worked and then failures: {len(c)} more bounces before "
                            f"the give-up, expected 4 (the success resets the count to 0, then 3 fail) - {c}")
        if out.since(k).count("giving up on re-associating") != 1:
            problems.append(f"the give-up line after the reset was printed "
                            f"{out.since(k).count('giving up on re-associating')} times, expected once")
        if "re-associating HILNET2: ok (HIL: power-cycled)" not in out.since(k):
            problems.append("the working bounce's line 're-associating HILNET2: ok (HIL: power-cycled)' is missing")
    finally:
        host.BOUNCE_COOLDOWN_S, host.auto_bounce = cooldown0, auto0
        s.req("POST", "/_api/detach", {})
        for n, f in real.items():
            setattr(discover, n, f)
    ctx.note(f"reconnect_auto_bounce: {len(fos.bounces)} bounces in all: {fos.bounces}")
    check(not problems, "; ".join(problems))


# ------------------------------------------------------------------ group "cli"
class _Cap(io.StringIO):
    def reconfigure(self, **_k):
        pass


def _main(argv, bundled_dtg=None, published=None):
    """host.main() with sys.argv = ['host.py'] + argv, web.run_app answered here -> (what main returned, the spec and
    auto_bounce run_app saw, everything printed). bundled_dtg writes src/webui/index.html with that footer stamp;
    published: what Pages answers (flash.reachable True and urlopen answered), None for offline."""
    import flash
    import urllib.request
    host = _host()
    seen = {}

    def run_app(app, **k):
        seen["spec"] = dict(host.bridge._spec or {}) if host.bridge._spec else None
        seen["attached"] = host.bridge.attached
        seen["auto_bounce"] = host.auto_bounce
        seen["run_app"] = {x: k.get(x) for x in ("host", "port")}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *e):
            pass
    webui = host.WEBUI_DIR / "index.html"
    if bundled_dtg:
        webui.parent.mkdir(parents=True, exist_ok=True)
        webui.write_text(f'<html><body><span id="footer-dtg">{bundled_dtg}</span><script></script></body></html>',
                         encoding="utf-8")
    elif webui.exists():
        webui.unlink()
    saved = (sys.argv, sys.stdout, sys.stderr, host.web.run_app, flash.reachable, urllib.request.urlopen,
             host.auto_bounce)
    cap = _Cap()
    sys.argv, sys.stdout = ["host.py"] + list(argv), cap
    host.web.run_app = run_app
    if published is not None:
        flash.reachable = lambda *a, **k: True
        urllib.request.urlopen = lambda *a, **k: Resp(
            f'<span id="footer-dtg">{published}</span>'.encode())
    try:
        try:
            rc = host.main()
        except SystemExit as e:
            rc = ("exit", e.code)
    finally:
        (sys.argv, sys.stdout, sys.stderr, host.web.run_app, flash.reachable, urllib.request.urlopen,
         auto) = saved
        host.auto_bounce = auto
        host.bridge.detach()
    return rc, seen, cap.getvalue()


@case("host.py main(): --ssid rides into a --ws target's spec (default NaviCore) and the target is attached before "
      "serving; --no-auto-bounce clears auto_bounce; a --serial target is attached and identified before serving; "
      "--serial with --ws exits 2", group="cli")
def cli(ctx):
    host = _host()
    Gate = _gate_class()
    Board = _board_class()
    real_rebuild = host.bridge.rebuild
    host.bridge.rebuild = lambda: Gate("WS", block=False, on_data=host.bridge._on_transport_data,
                                       on_lost=host.bridge._on_transport_lost)
    problems = []
    try:
        for argv, want_spec, want_bounce in (
                (["--ws", HOST, "--ssid", "HILSSID"], {"kind": "ws", "host": HOST, "ssid": "HILSSID"}, True),
                (["--ws", HOST], {"kind": "ws", "host": HOST, "ssid": "NaviCore"}, True),
                (["--ws", HOST, "--no-auto-bounce"], {"kind": "ws", "host": HOST, "ssid": "NaviCore"}, False),
                ([], None, True)):
            rc, seen, text = _main(["--port", "18765"] + argv)
            if rc != 0 or seen.get("spec") != want_spec or seen.get("auto_bounce") is not want_bounce:
                problems.append(f"host.py {' '.join(argv) or '(no target)'}: returned {rc}, run_app saw spec "
                                f"{seen.get('spec')} auto_bounce {seen.get('auto_bounce')}; expected {want_spec}, "
                                f"{want_bounce}")
            if want_spec and (not seen.get("attached") or f"attached  ws://{HOST}/ws" not in text):
                problems.append(f"host.py {' '.join(argv)}: the target was not attached before serving "
                                f"(attached={seen.get('attached')})")
            if seen.get("run_app") != {"host": "127.0.0.1", "port": 18765}:
                problems.append(f"run_app was asked for {seen.get('run_app')}, expected 127.0.0.1:18765")
        host.bridge.rebuild = lambda: Board("navicore", _script("navicore"), host.bridge._on_transport_data,
                                            host.bridge._on_transport_lost)
        rc, seen, text = _main(["--port", "18765", "--serial", "COMHILCLI"])
        if seen.get("spec") != {"kind": "serial", "port": "COMHILCLI", "role": "navicore"} or not seen.get("attached"):
            problems.append(f"--serial COMHILCLI: run_app saw {seen.get('spec')} attached={seen.get('attached')}, "
                            f"expected the port attached and identified as a NaviCore before serving")
        if "COMHILCLI is a NaviCore" not in text:
            problems.append("--serial: no 'COMHILCLI is a NaviCore' line before serving")
        host.bridge.rebuild = real_rebuild
        rc, seen, text = _main(["--port", "18765", "--serial", "COMX", "--ws", HOST])
        if rc != ("exit", 2) or "spec" in seen:
            problems.append(f"--serial with --ws: {rc}, served={'spec' in seen}, expected exit 2 before serving")
    finally:
        host.bridge.rebuild = real_rebuild
    check(not problems, "; ".join(problems))


@case("host.py's startup lines: the bundled stamp, '** OUT OF DATE ** published is <stamp>' when Pages is newer, "
      "nothing out of date when it matches, 'NOT bundled' with no bundle, and the reason offline", group="cli")
def cli_startup_lines(ctx):
    problems = []
    for bundled, published, want, unwanted in (
            ("HILOLD", "HILNEWER", "ui        HILOLD  ** OUT OF DATE ** published is HILNEWER", None),
            ("HILSAME", "HILSAME", "ui        HILSAME", "OUT OF DATE"),
            ("HILOFF", None, "could not compare with Pages:", "OUT OF DATE"),
            (None, None, "ui        NOT bundled", "OUT OF DATE")):
        rc, seen, text = _main(["--port", "18765"], bundled_dtg=bundled, published=published)
        if want not in text or (unwanted and unwanted in text):
            problems.append(f"bundled {bundled!r}, published {published!r}: the startup lines "
                            f"{[x for x in text.splitlines() if x.startswith(('ui', '   '))][:4]}, expected {want!r}")
        if rc != 0 or "serving   http://127.0.0.1:18765/" not in text:
            problems.append(f"bundled {bundled!r}: returned {rc}, no 'serving   http://127.0.0.1:18765/' line")
    check(not problems, "; ".join(problems))


# ------------------------------------------------------------------ group "app"
def _free_port():
    import socket
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


class _FakeWebview(types.ModuleType):
    """pywebview: create_window and start recorded; either can be made to raise."""

    def __init__(self, fail_create=False, fail_start=False):
        super().__init__("webview")
        self.windows, self.starts = [], []
        self.fail_create, self.fail_start = fail_create, fail_start

    def create_window(self, title, url, **k):
        if self.fail_create:
            raise RuntimeError("HIL: no window toolkit")
        self.windows.append((title, url, k))

    def start(self, func=None, **k):
        if self.fail_start:
            raise RuntimeError("HIL: the backend failed")
        self.starts.append(k)


def _app_main(argv, webview="missing", fail_serve=False, transport=None):
    """app.main() with sys.argv = ['app.py'] + argv -> (returned, what it printed, the URLs the browser was asked for).
    webview: 'missing' (import fails), or a _FakeWebview. The idle loop's time.sleep(1) raises KeyboardInterrupt, the
    way Ctrl+C ends --browser; the Dock icon (appicon) is never touched; transport: what bridge.rebuild() hands out."""
    import webbrowser
    import app
    import appicon
    import winsize
    host = _host()
    opened = []
    screen = (winsize._mac_visible_frame, winsize._win_work_area)
    winsize._mac_visible_frame = winsize._win_work_area = lambda: None     # no screen asked: the requested size
    real_sleep = time.sleep

    def sleep(sec):
        if sec == 1:
            raise KeyboardInterrupt
        real_sleep(sec)
    cap = _Cap()
    saved = (sys.argv, sys.stdout, sys.stderr, sys.modules.get("webview", "absent"), webbrowser.open, time.sleep,
             appicon.prepare, appicon.attach, host.build_app, host.open_window_hook, host.auto_bounce,
             os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"))
    sys.argv, sys.stdout, sys.stderr = ["app.py"] + list(argv), cap, cap
    sys.modules["webview"] = None if webview == "missing" else webview
    webbrowser.open = lambda url, *a, **k: opened.append(url) or True
    time.sleep = sleep
    appicon.prepare = appicon.attach = lambda *a, **k: None
    if fail_serve:
        def broken():
            raise RuntimeError("HIL: the server could not start")
        host.build_app = broken
    if transport is not None:
        host.bridge.rebuild = lambda: transport
    del app._serve_error[:]
    try:
        try:
            rc = app.main()
        except SystemExit as e:
            rc = ("exit", e.code)
        env = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS")
        hook = host.open_window_hook
        _STATE["app_after"] = {"wants": host.bridge.wants_link, "attached": host.bridge.attached,
                               "closes": getattr(transport, "closes", None)}    # before the cleanup below detaches
    finally:
        (sys.argv, sys.stdout, sys.stderr, wv, webbrowser.open, time.sleep, appicon.prepare, appicon.attach,
         host.build_app, host.open_window_hook, host.auto_bounce, env0) = saved
        if wv == "absent":
            sys.modules.pop("webview", None)
        else:
            sys.modules["webview"] = wv
        if env0 is None:
            os.environ.pop("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", None)
        else:
            os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = env0
        if transport is not None:
            del host.bridge.rebuild
        winsize._mac_visible_frame, winsize._win_work_area = screen
        host.bridge.detach()
        del app._serve_error[:]
    return rc, cap.getvalue(), opened, env, hook


@case("app.py: a server that cannot start releases the --ws target attached before it and exits 1, opening nothing",
      group="app")
def app_failed_start(ctx):
    host = _host()
    Gate = _gate_class()
    g = Gate("WS", block=False, on_data=host.bridge._on_transport_data, on_lost=host.bridge._on_transport_lost)
    port = _free_port()
    rc, text, opened, _, _ = _app_main(["--port", str(port), "--ws", HOST, "--browser"], fail_serve=True, transport=g)
    problems = []
    if rc != 1:
        problems.append(f"returned {rc}, expected 1")
    if f"host did not start on 127.0.0.1:{port}: HIL: the server could not start" not in text:
        problems.append(f"no 'host did not start on 127.0.0.1:{port}: <why>' line: {text[-300:]!r}")
    after = _STATE.get("app_after") or {}
    if g.opened != 1 or not after.get("closes") or after.get("attached"):
        problems.append(f"the early --ws attach was opened {g.opened}, closed {after.get('closes')} time(s) by the time "
                        f"main() returned: a failed start must release it (app.py, the detach after _wait_until_up)")
    if after.get("wants"):
        problems.append("the bridge still wanted the target when main() returned after the failed start")
    if opened:
        problems.append(f"a failed start opened the browser on {opened}")
    check(not problems, "; ".join(problems))


@case("app.py --browser, and pywebview missing or failing: the default browser gets the landing page - the chooser, "
      "or the tool when a target was given", group="app")
def app_browser_fallbacks(ctx):
    host = _host()
    Gate = _gate_class()
    problems = []
    for label, argv, webview, page, words in (
            ("--browser", ["--browser"], _FakeWebview(), "/_launcher", "serving http://127.0.0.1:{p}/_launcher"),
            ("--browser with --ws", ["--browser", "--ws", HOST], _FakeWebview(), "/", "serving http://127.0.0.1:{p}/"),
            ("no pywebview", [], "missing", "/_launcher", "pywebview not installed"),
            ("a window that cannot be made", [], _FakeWebview(fail_create=True), "/_launcher",
             "could not create the window (RuntimeError: HIL: no window toolkit)"),
            ("a backend that fails to start", [], _FakeWebview(fail_start=True), "/_launcher",
             "the window backend failed (RuntimeError: HIL: the backend failed)")):
        port = _free_port()
        g = Gate("WS", block=False, on_data=host.bridge._on_transport_data, on_lost=host.bridge._on_transport_lost)
        rc, text, opened, _, _ = _app_main(["--port", str(port)] + argv, webview=webview, transport=g)
        want = f"http://127.0.0.1:{port}{page}"
        if rc != 0 or opened != [want]:
            problems.append(f"{label}: returned {rc}, the browser was asked for {opened}, expected [{want}]")
        if words.format(p=port) not in text:
            problems.append(f"{label}: no {words.format(p=port)!r} line")
        if label.startswith("--browser") and webview.windows:
            problems.append(f"{label}: a window was made: {webview.windows}")
    check(not problems, "; ".join(problems))


@case("app.py window mode: one 'Intellex' window on the chooser, pywebview's storage kept under the data directory, "
      "--dev for its debug flag, --inspect for WebView2's debugging port, and the open-window hook for a second window",
      group="app")
def app_window(ctx):
    problems = []
    data = os.environ.get("INTELLEX_DATA_DIR", "")
    for argv, debug, inspect in (([], False, None), (["--dev"], True, None), (["--inspect", "9444"], False, 9444)):
        wv = _FakeWebview()
        port = _free_port()
        rc, text, opened, env, hook = _app_main(["--port", str(port)] + argv, webview=wv)
        label = " ".join(argv) or "(no flags)"
        url = f"http://127.0.0.1:{port}/_launcher"
        if rc != 0 or opened:
            problems.append(f"{label}: returned {rc}, opened the browser on {opened}")
        if [(t, u, k.get("text_select")) for t, u, k in wv.windows] != [("Intellex", url, True)]:
            problems.append(f"{label}: windows made {[(t, u) for t, u, _ in wv.windows]}, expected one 'Intellex' on {url}")
        elif (wv.windows[0][2].get("width"), wv.windows[0][2].get("height")) != (1280, 880):
            problems.append(f"{label}: the window was sized {wv.windows[0][2]}, expected winsize.fit's 1280x880 with no "
                            f"screen to measure")
        want_start = {"debug": debug, "private_mode": False, "storage_path": os.path.join(data, "webview")}
        if wv.starts != [want_start]:
            problems.append(f"{label}: webview.start{wv.starts}, expected {want_start}")
        if not os.path.isdir(os.path.join(data, "webview")):
            problems.append(f"{label}: the webview storage directory was not made under the data directory")
        want_env = f"--remote-debugging-port={inspect}" if inspect else None
        if env != want_env:
            problems.append(f"{label}: WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS is {env!r}, expected {want_env!r}")
        if not callable(hook):
            problems.append(f"{label}: no open-window hook registered for the shell")
        else:
            hook("WCB Wizard", "/wcb/Wizard/")
            if [(t, u) for t, u, _ in wv.windows[1:]] != [("WCB Wizard", f"http://127.0.0.1:{port}/wcb/Wizard/")]:
                problems.append(f"{label}: the open-window hook made {[(t, u) for t, u, _ in wv.windows[1:]]}")
    check(not problems, "; ".join(problems))


# ------------------------------------------------------------------ group "wifi_bounce_route"
@case("POST /_api/wifi-bounce runs discover.wifi_bounce off the event loop with the body's ssid - 'NaviCore' with no "
      "body, a body that is not JSON or one without ssid - and answers {ok, message} as it returned them",
      group="wifi_bounce_route")
def wifi_bounce_route(ctx):
    import asyncio
    import discover
    host = _host()
    check(host.discover is discover, "host.py's discover is not the module patched here")
    calls = []
    ctl = {"sleep": 0.0, "result": (True, "HIL bounced")}

    def fake(ssid="NaviCore"):
        try:
            asyncio.get_running_loop()
            on_loop = True
        except RuntimeError:
            on_loop = False
        calls.append({"ssid": ssid, "on_loop": on_loop})
        time.sleep(ctl["sleep"])
        return ctl["result"]

    def refuse(*_a, **_k):
        raise AssertionError("the real macOS bounce was reached")
    real = discover.wifi_bounce, discover._wifi_bounce_macos
    discover.wifi_bounce, discover._wifi_bounce_macos = fake, refuse      # before the app exists: nothing real runs
    problems = []
    try:
        s = _server()
        for label, body, want in (("no body", None, "NaviCore"), ("{}", {}, "NaviCore"),
                                  ("{ssid: 'HILNET'}", {"ssid": "HILNET"}, "HILNET"),
                                  ("{other: 1}", {"other": 1}, "NaviCore"), ("a body that is not JSON", b"not json",
                                                                             "NaviCore")):
            n = len(calls)
            code, _, raw = s.http("POST", "/_api/wifi-bounce", body)
            try:
                reply = json.loads(raw or b"null")
            except ValueError:
                reply = raw[:80]
            got = calls[n:]
            if code != 200 or reply != {"ok": True, "message": "HIL bounced"}:
                problems.append(f"{label}: {code} {reply}, expected 200 {{ok: true, message: 'HIL bounced'}}")
            if [c["ssid"] for c in got] != [want]:
                problems.append(f"{label}: wifi_bounce was called with {[c['ssid'] for c in got]}, expected [{want!r}]")
            elif got[0]["on_loop"]:
                problems.append(f"{label}: wifi_bounce ran on the event loop, not in a thread")
        ctl["result"] = (False, "HIL: no adapter is on that network")
        code, reply = s.req("POST", "/_api/wifi-bounce", {"ssid": "HILNET"})
        if code != 200 or reply != {"ok": False, "message": "HIL: no adapter is on that network"}:
            problems.append(f"a bounce that failed: {code} {reply}, expected 200 with ok false and its message")
        # Off the loop, measured: a slow bounce leaves /_api/status answering.
        ctl["sleep"], ctl["result"] = 2.0, (True, "HIL slow")
        n = len(calls)
        th, slow = _req_thread(s, "POST", "/_api/wifi-bounce", {"ssid": "HILSLOW"})
        deadline = time.monotonic() + 5
        while len(calls) == n and time.monotonic() < deadline:
            time.sleep(0.02)
        t0 = time.monotonic()
        s.status()
        took = time.monotonic() - t0
        th.join(10)
        if took > 0.5:
            problems.append(f"/_api/status took {took:.2f} s while a 2 s bounce ran: the bounce holds the event loop")
        if slow.get("status") != 200 or (slow.get("body") or {}).get("message") != "HIL slow":
            problems.append(f"the slow bounce answered {slow}")
    finally:
        discover.wifi_bounce, discover._wifi_bounce_macos = real
    check(not problems, "; ".join(problems))


# ------------------------------------------------------------------ group "signals"
@case("POST /_api/signals: 409 'nothing attached' with no transport; on a serial link it hands dataTerminalReady and "
      "requestToSend to set_signals as dtr and rts (None when absent or the body is not JSON) and answers ok; a driver "
      "error is 502 with its words; 409 again after a detach", group="signals")
def signals_route(ctx):
    from transport import TransportError
    s = _server()
    Board = _board_class()
    rec, ctl = [], {"fail": None}

    class Lines(Board):
        def set_signals(self, *, dtr=None, rts=None):
            rec.append((dtr, rts))
            if ctl["fail"]:
                raise TransportError(ctl["fail"])
    s.make = lambda spec, on_data, on_lost: Lines("sig", _script("navicore"), on_data, on_lost)
    problems = []
    try:
        s.req("POST", "/_api/detach", {})
        code, reply = s.req("POST", "/_api/signals", {"dataTerminalReady": False, "requestToSend": False})
        if code != 409 or reply != {"ok": False, "error": "nothing attached"}:
            problems.append(f"signals with nothing attached: {code} {reply}, expected 409 'nothing attached'")
        code, reply = s.req("POST", "/_api/attach", {"kind": "serial", "port": "COMHILSIG"})
        check(code == 200, f"the fake port did not attach: {code} {reply}")
        for label, body, want in (("both false", {"dataTerminalReady": False, "requestToSend": False}, (False, False)),
                                  ("DTR only", {"dataTerminalReady": True}, (True, None)),
                                  ("RTS only", {"requestToSend": True}, (None, True)),
                                  ("not JSON", b"not json", (None, None))):
            n = len(rec)
            code, _, raw = s.http("POST", "/_api/signals", body)
            if code != 200 or json.loads(raw or b"null") != {"ok": True} or rec[n:] != [want]:
                problems.append(f"{label}: {code} {raw[:60]!r}, set_signals got {rec[n:]}, expected 200 ok and "
                                f"[{want}]")
        ctl["fail"] = "HIL: the driver refused DTR"
        code, reply = s.req("POST", "/_api/signals", {"dataTerminalReady": False})
        if code != 502 or reply != {"ok": False, "error": "HIL: the driver refused DTR"}:
            problems.append(f"a driver error: {code} {reply}, expected 502 with its words")
        ctl["fail"] = None
        s.req("POST", "/_api/detach", {})
        code, reply = s.req("POST", "/_api/signals", {})
        if code != 409:
            problems.append(f"signals after a detach: {code} {reply}, expected 409")
    finally:
        s.req("POST", "/_api/detach", {})
    check(not problems, "; ".join(problems))


# ------------------------------------------------------------------ group "retry_loop"
@case("while a reattach fails the reconnect loop keeps lastError at 'reconnect failed: <why>' and prints that line "
      "once per new reason (a reason that returns after another is new again); a reattach that works prints "
      "'reconnected', clears lastError and forgets the reason", group="retry_loop")
def retry_loop(ctx):
    from transport import TransportError
    s = _server()
    out = _STATE["out"]
    Board = _board_class()
    ctl = {"why": "HIL reason A"}
    made = []

    class Flaky(Board):
        def open(self):
            if ctl["why"]:
                raise TransportError(ctl["why"])
            self._open = True

    def make(spec, on_data, on_lost):
        b = Flaky("flaky", _script("navicore"), on_data, on_lost)
        made.append(b)
        return b
    s.make = make
    A, B = "reconnect failed: HIL reason A", "reconnect failed: HIL reason B"
    problems = []

    def retries(n, timeout=10):
        m = len(made)
        _wait(lambda: len(made) >= m + n, timeout)
        return len(made) - m
    try:
        k = len(out.text)
        code, reply = s.req("POST", "/_api/attach", {"kind": "serial", "port": "COMHILRETRY"})
        check(code == 502 and reply.get("error") == "HIL reason A", f"the first attach answered {code} {reply}")
        got = retries(4)
        st = s.status()
        lines = out.since(k)
        if got < 4:
            problems.append(f"only {got} reattaches in 10 s: the loop retries every second")
        if lines.count(A) != 1:
            problems.append(f"'{A}' printed {lines.count(A)} times over {got} failed reattaches, expected once")
        if st.get("lastError") != A or st.get("attached") or not st.get("reconnecting"):
            problems.append(f"while it fails, /_api/status {st}: lastError should read {A!r}")
        ctl["why"] = "HIL reason B"
        retries(3)
        lines, st = out.since(k), s.status()
        if lines.count(B) != 1 or lines.count(A) != 1:
            problems.append(f"after the reason changed: '{A}' x{lines.count(A)}, '{B}' x{lines.count(B)}, expected "
                            f"once each")
        if st.get("lastError") != B:
            problems.append(f"after the reason changed lastError is {st.get('lastError')!r}, expected {B!r}")
        ctl["why"] = "HIL reason A"
        retries(3)
        if out.since(k).count(A) != 2:
            problems.append(f"reason A back after B: '{A}' printed {out.since(k).count(A)} times in all, expected 2 - "
                            f"a reason is new whenever it differs from the last one")
        ctl["why"] = None
        _wait(lambda: s.status().get("attached"), 10)
        st, lines = s.status(), out.since(k)
        if not st.get("attached") or "reconnected  serial COMHILRETRY" not in lines:
            problems.append(f"once the port opens: attached {st.get('attached')}, a 'reconnected  serial "
                            f"COMHILRETRY' line: {'reconnected  serial COMHILRETRY' in lines}")
        if st.get("lastError") != "":
            problems.append(f"a reattach that worked left lastError {st.get('lastError')!r}")
        # The reason is forgotten once a reattach works: the same failure after a drop is printed again.
        k2 = len(out.text)
        ctl["why"] = "HIL reason A"
        made[-1].lose("HIL: the port went away")
        retries(3)
        st, lines = s.status(), out.since(k2)
        if lines.count(A) != 1:
            problems.append(f"after a reattach that worked and a drop, '{A}' printed {lines.count(A)} times, expected "
                            f"once (the loop forgets the reason when it reconnects)")
        if st.get("lastError") != A:
            problems.append(f"after the drop lastError is {st.get('lastError')!r}, expected {A!r} (the drop's own "
                            f"'HIL: the port went away' replaced by the failed reattach)")
    finally:
        ctl["why"] = None
        s.req("POST", "/_api/detach", {})
    check(not problems, "; ".join(problems))


# ------------------------------------------------------------------ group "version"
def _git(cwd, *args):
    p = subprocess.run(["git", "-c", "user.name=hil", "-c", "user.email=hil@example.invalid", "-c",
                        "commit.gpgsign=false"] + list(args), cwd=cwd, capture_output=True, text=True, timeout=30)
    check(p.returncode == 0, f"git {' '.join(args)} failed: {(p.stderr or p.stdout).strip()[:200]}")
    return p.stdout.strip()


@case("version.info(): from a checkout, git's short SHA, '+dirty' on a tree with an uncommitted change, built 'from "
      "source'; a build stamp wins over git, dirty or not; with neither, no commit; describe() and GET /_api/version "
      "say the same", group="version")
def version_info(ctx):
    import version
    scratch = os.path.join(ctx.stage, "scratch", "version")
    repo = os.path.join(scratch, "repo")
    src = os.path.join(repo, "src")
    os.makedirs(src, exist_ok=True)
    with open(os.path.join(src, "hil.txt"), "w", encoding="utf-8") as f:
        f.write("committed\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "hil")
    sha = _git(repo, "rev-parse", "--short", "HEAD")
    saved_file, saved_stamp = version.__file__, sys.modules.pop("build_stamp", None)
    saved_ceiling = os.environ.get("GIT_CEILING_DIRECTORIES")
    V = version.VERSION
    problems = []
    try:
        version.__file__ = os.path.join(src, "version.py")         # _from_git asks git about src/'s parent
        got = version.info()
        if got != {"version": V, "commit": sha, "built": "from source"}:
            problems.append(f"a clean checkout: {got}, expected commit {sha!r}, built 'from source'")
        with open(os.path.join(repo, "untracked.txt"), "w", encoding="utf-8") as f:
            f.write("uncommitted\n")
        got = version.info()
        if got.get("commit") != sha + "+dirty":
            problems.append(f"a tree with an uncommitted file: commit {got.get('commit')!r}, expected {sha + '+dirty'!r}")
        stamp = types.ModuleType("build_stamp")
        stamp.COMMIT, stamp.BUILT = "hilstamp1", "2026-10-12 23:59"
        sys.modules["build_stamp"] = stamp
        got = version.info()
        if got != {"version": V, "commit": "hilstamp1", "built": "2026-10-12 23:59"}:
            problems.append(f"a build stamp on a dirty tree: {got}, expected the stamp's commit and time")
        if version.describe() != f"v{V} (hilstamp1, 2026-10-12 23:59)":
            problems.append(f"describe() with a stamp: {version.describe()!r}")
        code, body = _server().req("GET", "/_api/version")
        if code != 200 or body != got:
            problems.append(f"GET /_api/version: {code} {body}, expected info() {got}")
        del sys.modules["build_stamp"]
        nogit = os.path.join(scratch, "nogit", "src")
        os.makedirs(nogit, exist_ok=True)
        os.environ["GIT_CEILING_DIRECTORIES"] = scratch               # git must not find the repo around the stage
        version.__file__ = os.path.join(nogit, "version.py")
        got = version.info()
        if got != {"version": V, "commit": "", "built": ""}:
            problems.append(f"no stamp and no git checkout: {got}, expected no commit and no build time")
        if version.describe() != f"v{V}":
            problems.append(f"describe() with neither: {version.describe()!r}, expected 'v{V}'")
    finally:
        version.__file__ = saved_file
        sys.modules.pop("build_stamp", None)
        if saved_stamp is not None:
            sys.modules["build_stamp"] = saved_stamp
        if saved_ceiling is None:
            os.environ.pop("GIT_CEILING_DIRECTORIES", None)
        else:
            os.environ["GIT_CEILING_DIRECTORIES"] = saved_ceiling
    check(not problems, "; ".join(problems))


# ------------------------------------------------------------------ group "tool_base"
WCB_PAGES = "https://greghulette.github.io/Wireless_Communication_Board-WCB"
NC_PAGES = "https://greghulette.github.io/NaviCore"


@case("settings.tool_base: main (or no branch) is the site's own tool, any other branch /dev/<branch>/<leaf>, and "
      "with no branch given the configured one; fetch_webui.base_for and the launcher's version check ask those same "
      "URLs", group="tool_base")
def tool_base(ctx):
    import flash
    import settings
    import urllib.request
    problems = []
    for args, want in ((("wcb", "WIFI"), f"{WCB_PAGES}/dev/WIFI/Wizard"),
                       (("navicore", "main"), f"{NC_PAGES}/config_tool"),
                       (("wcb", "main"), f"{WCB_PAGES}/Wizard"),
                       (("navicore", "feature/x"), f"{NC_PAGES}/dev/feature/x/config_tool"),
                       (("wcb", ""), f"{WCB_PAGES}/Wizard")):
        got = settings.tool_base(*args)
        if got != want:
            problems.append(f"tool_base{args}: {got!r}, expected {want!r}")
    asked = []

    class _Reply:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *_a):
            return b""

    def urlopen(url, *a, **k):
        u = url if isinstance(url, str) else url.full_url
        if not u.startswith("https://"):
            return real[1](url, *a, **k)            # this script's own requests to the in-process app
        asked.append(u)
        return _Reply()
    real = flash.reachable, urllib.request.urlopen
    try:
        check(settings.set_branch("wcb", "WIFI") is None and settings.set_branch("navicore", "main") is None,
              "could not set the branches")
        if settings.tool_base("wcb") != f"{WCB_PAGES}/dev/WIFI/Wizard" or settings.tool_base("navicore") != \
                f"{NC_PAGES}/config_tool":
            problems.append(f"with no branch given (wcb on WIFI): {settings.tool_base('wcb')!r}, "
                            f"{settings.tool_base('navicore')!r}")
        import fetch_webui
        for site, leaf, br in ((fetch_webui.WCB_SITE, "Wizard", "WIFI"), (fetch_webui.NAVICORE_SITE, "config_tool",
                                                                         "main")):
            prod = "wcb" if leaf == "Wizard" else "navicore"
            if fetch_webui.base_for(site, leaf, br) != settings.tool_base(prod, br):
                problems.append(f"fetch_webui.base_for({leaf}, {br}) {fetch_webui.base_for(site, leaf, br)!r} differs "
                                f"from tool_base's {settings.tool_base(prod, br)!r}")
        flash.reachable = lambda *a, **k: True
        urllib.request.urlopen = urlopen
        code, body = _server().req("GET", "/_api/webui-version")
        want = sorted([f"{NC_PAGES}/config_tool/index.html", f"{WCB_PAGES}/dev/WIFI/Wizard/app.js"])
        if code != 200 or sorted(asked) != want:
            problems.append(f"the version check asked {sorted(asked)}, expected {want}")
    finally:
        flash.reachable, urllib.request.urlopen = real
        settings.set_branch("wcb", "main")
        settings.set_branch("navicore", "main")
    check(not problems, "; ".join(problems))



# ------------------------------------------------------------------ group "branch_list" (half of intellex.branch_list_cached)
@case("branchlist online, GitHub stood in for: gh-pages is dropped from what GitHub lists and from the cache written; "
      "the first call of a run fetches even inside the TTL, later ones serve the cache until it expires or ?refresh=1; "
      "a set branch GitHub no longer lists is 'missing' and kept first; a total failure says why for each repo and "
      "does not end the once-per-run fetch; a partial one keeps the other product's cached list", group="branch_list")
def branch_list_online(ctx):
    import branchlist
    import flash
    import settings
    nc_repo, wcb_repo = branchlist.REPOS["navicore"][1], branchlist.REPOS["wcb"][1]
    lists = {nc_repo: ["main", "gh-pages", "hil-nc"], wcb_repo: ["main", "gh-pages", "WIFI"]}
    calls, fail = [], set()

    def fetch(owner, repo, timeout=8.0):
        calls.append(repo)
        if repo in fail:
            raise OSError("HIL: GitHub said no")
        return list(lists[repo])
    cache = branchlist._cache_file()

    def seed(at, nc, wcb):
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"navicore": nc, "wcb": wcb, "at": at}), encoding="utf-8")
    real = branchlist._fetch, flash.reachable, branchlist._fetched_this_run
    branchlist._fetch, flash.reachable = fetch, (lambda *a, **k: True)
    problems = []
    try:
        settings.set_branch("wcb", "main")
        settings.set_branch("navicore", "main")
        seed(time.time(), ["main", "old-nc"], ["main", "old-wcb"])          # inside its TTL, from an earlier run
        branchlist._fetched_this_run = False
        r = branchlist.branches()
        if sorted(calls) != sorted([nc_repo, wcb_repo]) or r.get("cached") is not False:
            problems.append(f"the first call of a run inside the TTL fetched {calls}, cached {r.get('cached')}: it must "
                            f"fetch once per run")
        if r.get("wcb") != ["main", "WIFI"] or r.get("navicore") != ["main", "hil-nc"] or r.get("error") != "":
            problems.append(f"the fetched lists: wcb {r.get('wcb')}, navicore {r.get('navicore')}, error "
                            f"{r.get('error')!r} - gh-pages must be dropped")
        written = json.loads(cache.read_text(encoding="utf-8"))
        if "gh-pages" in json.dumps(written) or written.get("wcb") != ["main", "WIFI"]:
            problems.append(f"the cache written: {written}")
        del calls[:]
        r = branchlist.branches()
        if calls or r.get("cached") is not True or r.get("wcb") != ["main", "WIFI"]:
            problems.append(f"a second call inside the TTL fetched {calls}, cached {r.get('cached')}")
        code, body = _server().req("GET", "/_api/branch-list?refresh=1")
        if code != 200 or sorted(calls) != sorted([nc_repo, wcb_repo]) or body.get("cached") is not False:
            problems.append(f"GET /_api/branch-list?refresh=1 fetched {calls}, answered {code} cached "
                            f"{body.get('cached')}")
        del calls[:]
        code, body = _server().req("GET", "/_api/branch-list")
        if calls:
            problems.append(f"GET /_api/branch-list (no refresh, inside the TTL) fetched {calls}")
        seed(time.time() - branchlist._TTL_S - 60, ["main", "hil-nc"], ["main", "WIFI"])
        r = branchlist.branches()
        if sorted(calls) != sorted([nc_repo, wcb_repo]):
            problems.append(f"past the TTL nothing was fetched ({calls})")
        settings.set_branch("wcb", "hil-gone")
        r = branchlist.branches(force=True)
        if r.get("missing") != ["wcb"] or r.get("wcb") != ["hil-gone", "main", "WIFI"]:
            problems.append(f"a set branch GitHub no longer lists: missing {r.get('missing')}, wcb {r.get('wcb')}, "
                            f"expected ['wcb'] and it kept first")
        settings.set_branch("wcb", "main")
        # A total failure: both repos named, the cache served, and the next call tries again.
        seed(time.time(), ["main", "c-nc"], ["main", "c-wcb"])
        branchlist._fetched_this_run = False
        fail.update({nc_repo, wcb_repo})
        del calls[:]
        r = branchlist.branches()
        err = r.get("error", "")
        if r.get("cached") is not True or r.get("wcb") != ["main", "c-wcb"] or nc_repo not in err or wcb_repo not in err:
            problems.append(f"both fetches failing: cached {r.get('cached')}, wcb {r.get('wcb')}, error {err!r}")
        n = len(calls)
        branchlist.branches()
        if len(calls) == n:
            problems.append("after a total failure the next call did not fetch again: it pinned the stale list for the "
                            "run")
        # A partial failure: NaviCore answers, the WCB list is kept from the cache.
        fail.discard(nc_repo)
        r = branchlist.branches(force=True)
        if r.get("navicore") != ["main", "hil-nc"] or r.get("wcb") != ["main", "c-wcb"] or r.get("cached") is not False \
                or wcb_repo not in r.get("error", "") or nc_repo in r.get("error", ""):
            problems.append(f"only the WCB fetch failing: navicore {r.get('navicore')}, wcb {r.get('wcb')}, cached "
                            f"{r.get('cached')}, error {r.get('error')!r}")
    finally:
        branchlist._fetch, flash.reachable, branchlist._fetched_this_run = real
        settings.set_branch("wcb", "main")
        settings.set_branch("navicore", "main")
        try:
            cache.unlink()
        except OSError:
            pass
    check(not problems, "; ".join(problems))


if __name__ == "__main__":
    sys.exit(_ixpy.main())
