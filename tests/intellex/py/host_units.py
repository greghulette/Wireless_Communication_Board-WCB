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
from bridge_units import _STATE, _board_class, _gate_class, _host, _script, _server  # noqa: E402

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


if __name__ == "__main__":
    sys.exit(_ixpy.main())
