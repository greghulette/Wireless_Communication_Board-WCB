"""Intellex's Bridge and its attach route with in-process fake transports, under Intellex's venv. No port is opened and no
board is touched: every transport here is a fake handed out where host.py would build a SerialTransport.

group "superseded" (intellex.bridge_superseded_attach): Intellex src/host.py Bridge.attach/detach (:214-251). An attach
whose generation is stale by the time open() returns - a newer attach or a detach started meanwhile - closes what it
opened and raises TransportError('superseded by a newer attach') instead of publishing over the newer link, and the
bridge's lock is never held across open(). A transport whose open() blocks on an Event stands in for a COM port or a
WebSocket handshake that takes its time. Then the same race through POST /_api/attach on Intellex's real build_app():
the first of two attaches answers 502, the second's target is the one attached.

group "identify" (intellex.attach_identify_role): host.py _identify_attached_serial (:1152-1231) and api_attach (:1304),
reconnect_loop (:1797). After a serial attach the host asks the port what it is through a bridge tap: the PING alone
first, and ?RELAY,WIFI and ?WDP,DUMP only when no direct PONG came; it records role navicore | relay | wcb and the WDP
self row's number as relayId (1..20, never for a NaviCore) before the attach answers, leaves the role empty when nothing
answers, and asks again on a reattach until the port has said what it is. A scripted fake COM port answers as each
kind of board would, its replies cut into small chunks as a serial read delivers them.
"""
import asyncio
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ixpy  # noqa: E402
from _ixpy import case, check  # noqa: E402

_STATE = {}


class _Tee:
    """sys.stdout that also keeps what is printed: the host's own lines ('<port> is a WCB', 'did not say what it is')."""

    def __init__(self, real):
        self.real, self.text = real, []

    def write(self, s):
        self.text.append(s)
        return self.real.write(s)

    def flush(self):
        self.real.flush()

    def reconfigure(self, **_k):        # host.main() asks for line buffering; the real stream keeps its own
        pass

    def since(self, k):
        return "".join(self.text[k:])


def _host():
    if "host" not in _STATE:
        import host
        _STATE["host"] = host
        _STATE["out"] = sys.stdout = _Tee(sys.stdout)
    return _STATE["host"]


def _transport_base():
    from transport import Transport, TransportError
    return Transport, TransportError


# ------------------------------------------------------------------ fakes
def _gate_class():
    Transport, TransportError = _transport_base()

    class Gate(Transport):
        """A transport whose open() waits for go (set at once unless block): a port or handshake that takes its time.
        Counts open() and close() calls; write() works only while open."""

        def __init__(self, name, block=True, on_data=None, on_lost=None):
            super().__init__(on_data or (lambda _c: None), on_lost)
            self.name, self.opened, self.closes, self.writes = name, 0, 0, []
            self.entered, self.go = threading.Event(), threading.Event()
            self._open = False
            if not block:
                self.go.set()

        def open(self):
            self.opened += 1
            self.entered.set()
            if not self.go.wait(15):
                raise TransportError(f"{self.name}: never released")
            self._open = True

        def write(self, data):
            if not self._open:
                raise TransportError(f"{self.name}: not open")
            self.writes.append(bytes(data))

        def close(self):
            self.closes += 1
            self._open = False

        @property
        def is_open(self):
            return self._open
    return Gate


def _board_class():
    Transport, TransportError = _transport_base()

    class Board(Transport):
        """A COM port with a board on it that answers the host's identify questions from a script: {line: reply bytes}.
        Every line written is kept (asked); a reply goes back a moment later in 7-byte pieces, as a serial reader hands
        them up. lose() is the port going away underneath the host (on_lost, as SerialTransport's reader reports it)."""

        def __init__(self, name, replies, on_data, on_lost):
            super().__init__(on_data, on_lost)
            self.name, self.replies, self.asked = name, dict(replies), []
            self._open, self._buf = False, b""
            import queue
            self._out = queue.SimpleQueue()       # one reader thread, so replies arrive whole and in order
            threading.Thread(target=self._deliver, daemon=True).start()

        def open(self):
            self._open = True

        def write(self, data):
            if not self._open:
                raise TransportError(f"{self.name}: not open")
            self._buf += bytes(data)
            while b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                line = line.decode("utf-8", "replace").strip()
                self.asked.append(line)
                out = self.replies.get(line)
                if out:
                    self._out.put(out)

        def _deliver(self):
            while True:
                out = self._out.get()
                time.sleep(0.03)
                for i in range(0, len(out), 7):
                    if not self._open:
                        break
                    self._on_data(out[i:i + 7])
                    time.sleep(0.002)

        def close(self):
            self._open = False

        @property
        def is_open(self):
            return self._open

        def lose(self, why="the port went away (HIL fake)"):
            self._open = False
            if self._on_lost:
                self._on_lost(why)
    return Board


class _Attach(threading.Thread):
    """bridge.attach(t, label, spec) on its own thread; error is what it raised, or None."""

    def __init__(self, bridge, t, label, spec):
        super().__init__(daemon=True)
        self.args_ = (bridge, t, label, spec)
        self.error, self.done = None, threading.Event()

    def run(self):
        b, t, label, spec = self.args_
        try:
            b.attach(t, label, spec)
        except Exception as e:      # noqa: BLE001 - what it raised is the result
            self.error = e
        finally:
            self.done.set()


# ------------------------------------------------------------------ Intellex's own app, in this process
class _Server:
    """Intellex's build_app() on 127.0.0.1:<free> in this process (its reconnect loop running, as on_startup starts it),
    with bridge.rebuild() handing out what make(spec, on_data, on_lost) returns for the target being attached."""

    def __init__(self):
        self.host = _host()
        self.make = None
        b = self.host.bridge
        b.rebuild = lambda: self.make(dict(b._spec or {}), b._on_transport_data, b._on_transport_lost)
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        if not self.ready.wait(20):
            raise AssertionError("Intellex's app did not start in-process within 20 s")

    def _run(self):
        from aiohttp import web
        asyncio.set_event_loop(self.loop)
        runner = web.AppRunner(self.host.build_app())
        self.loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "127.0.0.1", 0)
        self.loop.run_until_complete(site.start())
        self.port = site._server.sockets[0].getsockname()[1]
        self.ready.set()
        self.loop.run_forever()

    def req(self, method, path, body=None, timeout=20):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
                                   headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def status(self):
        return self.req("GET", "/_api/status")[1]

    def http(self, method, path, body=None, timeout=20):
        """(status, {header: value} with lower-case names, body bytes). body: a dict sent as JSON, or raw bytes."""
        data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
                                   headers={"Content-Type": "application/json"} if data is not None else {})
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read()


def _server():
    if "server" not in _STATE:
        _STATE["server"] = _Server()
    return _STATE["server"]


def _req_thread(s, method, path, body):
    """s.req on its own thread -> (thread, result dict: status, body, took)."""
    out = {}

    def go():
        t0 = time.monotonic()
        out["status"], out["body"] = s.req(method, path, body)
        out["took"] = time.monotonic() - t0
    th = threading.Thread(target=go, daemon=True)
    th.start()
    return th, out


# ------------------------------------------------------------------ group "superseded"
SPEC_A = {"kind": "serial", "port": "COMHILA"}
SPEC_B = {"kind": "serial", "port": "COMHILB"}


@case("control: an attach nobody races publishes its transport and records its target; the next attach closes it "
      "first", group="superseded")
def attach_control(ctx):
    host = _host()
    Gate = _gate_class()
    b = host.Bridge()
    a = Gate("A", block=False)
    t0 = time.monotonic()
    b.attach(a, "A", dict(SPEC_A))
    check(b._transport is a and b.attached and b.target_label == "A", f"A not published: {b._transport} {b.target_label}")
    check(b.wants_link and b._spec == SPEC_A, f"A's target not recorded: want={b.wants_link} spec={b._spec}")
    check(b.last_rx >= t0, "the idle clock did not start at the attach (last_rx before it)")
    nb = Gate("B", block=False)
    b.attach(nb, "B", dict(SPEC_B))
    check(a.closes == 1 and not a.is_open, f"the attach over A left it open (closes={a.closes})")
    check(b._transport is nb and b._spec == SPEC_B, "B is not the link after the second attach")
    b.detach()
    check(nb.closes == 1 and b._transport is None and not b.wants_link, "detach left B attached or wanted")


@case("an attach still inside open() when a newer attach completes closes what it opened and raises 'superseded'; "
      "the newer link stays, and the lock is free while open() blocks", group="superseded")
def superseded_by_attach(ctx):
    host = _host()
    from transport import TransportError
    Gate = _gate_class()
    b = host.Bridge()
    a, nb = Gate("A"), Gate("B", block=False)
    ta = _Attach(b, a, "A", dict(SPEC_A))
    ta.start()
    check(a.entered.wait(5), "attach A never reached open()")
    got = b._lock.acquire(timeout=2)
    check(got, "the bridge's lock is held while A blocks in open(): every other attach, detach and drop would wait "
               "for it (host.py:156-164 says never hold it across a blocking call)")
    b._lock.release()
    t0 = time.monotonic()
    b.attach(nb, "B", dict(SPEC_B))
    took = time.monotonic() - t0
    check(took < 1.0, f"attach B waited {took:.1f} s for A's open()")
    check(b._transport is nb and b.target_label == "B" and b.attached, "B is not the link while A is still opening")
    check(ta.is_alive() and not ta.done.is_set(), "attach A returned before its open() was released")
    a.go.set()
    check(ta.done.wait(5), "attach A did not return once its open() was released")
    check(isinstance(ta.error, TransportError) and "superseded" in str(ta.error),
          f"attach A raised {type(ta.error).__name__}: {ta.error!r}, expected TransportError('superseded ...')")
    check(a.opened == 1 and a.closes == 1 and not a.is_open,
          f"A was opened {a.opened}, closed {a.closes}: what a superseded attach opened must be closed (a leaked COM "
          f"port with its reader still running)")
    check(b._transport is nb and nb.is_open and nb.closes == 0 and b.target_label == "B",
          f"A's late return disturbed B: transport {getattr(b._transport, 'name', None)}, B closes {nb.closes}, "
          f"label {b.target_label!r}")
    check(b._spec == SPEC_B and b.wants_link, f"the target is {b._spec}, not B's")
    b.detach()


@case("an attach still inside open() when the user detaches closes what it opened and raises 'superseded'; nothing "
      "is attached or wanted afterwards", group="superseded")
def superseded_by_detach(ctx):
    host = _host()
    from transport import TransportError
    Gate = _gate_class()
    b = host.Bridge()
    a = Gate("A")
    b.set_target(dict(SPEC_A))                       # api_attach records the target first (host.py:1291)
    ta = _Attach(b, a, "A", dict(SPEC_A))
    ta.start()
    check(a.entered.wait(5), "attach A never reached open()")
    t0 = time.monotonic()
    b.detach()
    took = time.monotonic() - t0
    check(took < 1.0, f"detach waited {took:.1f} s for A's open()")
    a.go.set()
    check(ta.done.wait(5), "attach A did not return once its open() was released")
    check(isinstance(ta.error, TransportError) and "superseded" in str(ta.error),
          f"attach A raised {type(ta.error).__name__}: {ta.error!r}, expected TransportError('superseded ...')")
    check(a.closes == 1 and not a.is_open, f"A was closed {a.closes} time(s) after a detach superseded it")
    check(b._transport is None and not b.attached, "A was published over the detach")
    check(not b.wants_link and b._spec is None, f"the detach was undone: want={b._want} spec={b._spec}")


@case("through POST /_api/attach: of two attaches, the one still opening when the second lands answers 502 "
      "'superseded'; the second's target is attached and the first's transport is closed", group="superseded")
def superseded_over_http(ctx):
    s = _server()
    Gate = _gate_class()
    made = {}

    def make(spec, on_data, on_lost):
        g = Gate(spec["port"], block=spec["port"] == "COMHILA", on_data=on_data, on_lost=on_lost)
        made[spec["port"]] = g
        return g
    s.make = make
    th, first = _req_thread(s, "POST", "/_api/attach", dict(SPEC_A))
    deadline = time.monotonic() + 5
    while "COMHILA" not in made and time.monotonic() < deadline:
        time.sleep(0.02)
    check("COMHILA" in made and made["COMHILA"].entered.wait(5), "the first attach never reached open()")
    st = s.status()
    check(st.get("attached") is False and st.get("wantsLink") is True and st.get("target") == "",
          f"while the first attach opens, /_api/status: {st}")
    th2, second = _req_thread(s, "POST", "/_api/attach", dict(SPEC_B))
    # The second's open() is instant, then the host asks the port what it is (a silent fake: ~3 s) before answering.
    deadline = time.monotonic() + 3
    while not ("COMHILB" in made and made["COMHILB"].is_open) and time.monotonic() < deadline:
        time.sleep(0.02)
    check("COMHILB" in made and made["COMHILB"].is_open, "the second attach did not open its port")
    made["COMHILA"].go.set()
    th.join(10)
    th2.join(10)
    check(first.get("status") == 502 and "superseded" in str((first.get("body") or {}).get("error")),
          f"the first attach answered {first.get('status')} {first.get('body')}, expected 502 'superseded ...'")
    check(second.get("status") == 200 and (second.get("body") or {}).get("ok") is True
          and second["body"].get("target") == "serial COMHILB",
          f"the second attach answered {second.get('status')} {second.get('body')}")
    a, nb = made["COMHILA"], made["COMHILB"]
    check(a.closes == 1 and not a.is_open, f"the first attach's port was closed {a.closes} time(s)")
    st = s.status()
    check(st.get("attached") is True and st.get("target") == "serial COMHILB" and nb.is_open and nb.closes == 0,
          f"after both answered, /_api/status: {st}")
    ctx.note(f"bridge_superseded_attach: after the race /_api/status lastError is {st.get('lastError')!r} with B "
             f"attached (api_attach records the loser's 'superseded' after the winner cleared it)")
    s.req("POST", "/_api/detach", {})


# ------------------------------------------------------------------ group "identify"
def _probe_keys():
    import discover
    return [x.strip() for x in discover.PROBE_LINES]


def _self_row(n, alias):
    return (f"[WDP:N={n},CLIENT=0,ALIAS={alias},HW=31,HWREV=3.1,FW=6.2.1,CAP=0000,CTRL=0,CAPTAGS=,MAESTRO=-,AGE=0,"
            f"SEEN=1,PEER=3]\n")


def _peer_row(n):
    return f"[WDP:N={n},CLIENT=0,ALIAS=Peer{n},HW=31,HWREV=3.1,FW=6.2.1,CAP=0000,CTRL=0,CAPTAGS=,MAESTRO=-,AGE=1,SEEN=1,PEER=1]\n"


def _script(kind):
    """What each kind of board on a COM port says to PING, ?RELAY,WIFI and ?WDP,DUMP -> {line: reply bytes}."""
    ping, relay, wdp = _probe_keys()
    nav_pong = '{"type":"PONG","version":"v0.2.0_hil"}\n'
    mesh_pong = '{"sys":1,"type":"PONG","id":20,"version":"v0.2.0_hil"}\n'      # NaviCore's reply over the mesh
    if kind == "navicore":
        return {ping: nav_pong.encode(), relay: b"Unknown command: ?RELAY,WIFI\n", wdp: b"Unknown command: ?WDP,DUMP\n"}
    if kind == "wcb":
        return {relay: b"Unknown command\n", wdp: (_self_row(1, "W1hil") + _peer_row(2) + "[WDP:END,count=1]\n").encode()}
    if kind == "wcb_mirror":           # W1 inside its 20 s relay window: NaviCore's mesh PONG comes back too
        return {ping: mesh_pong.encode(), relay: b"Unknown command\n",
                wdp: (_self_row(1, "W1hil") + _peer_row(20) + "[WDP:END,count=1]\n").encode()}
    if kind == "relay":
        return {relay: b"[relay] wifi=AP clients=0\n", wdp: (_self_row(19, "Relayhil") + _peer_row(1)
                                                              + "[WDP:END,count=1]\n").encode()}
    if kind == "wcb_25":               # a self row whose number is no relay slot
        return {relay: b"Unknown command\n", wdp: (_self_row(25, "Odd") + "[WDP:END,count=0]\n").encode()}
    return {}                          # silent: nothing answers


def _attach_board(s, kind, port="COMFAKE"):
    """POST /_api/attach for a serial port with a `kind` board on it -> (reply status, reply body, seconds, the fake,
    /_api/status right after)."""
    Board = _board_class()
    made = []

    def make(spec, on_data, on_lost):
        b = Board(kind, _script(kind), on_data, on_lost)
        made.append(b)
        return b
    s.make = make
    t0 = time.monotonic()
    code, body = s.req("POST", "/_api/attach", {"kind": "serial", "port": port})
    took = time.monotonic() - t0
    return code, body, took, (made[0] if made else None), s.status()


@case("a NaviCore on the port answers the PING alone and is recorded as role navicore, no relayId; it is never sent "
      "?RELAY,WIFI or ?WDP,DUMP", group="identify")
def identify_navicore(ctx):
    s = _server()
    ping, _, _ = _probe_keys()
    k = len(_STATE["out"].text)
    code, body, took, fake, st = _attach_board(s, "navicore")
    try:
        check(code == 200 and body.get("ok") and body.get("role") == "navicore",
              f"attach answered {code} {body}, expected role navicore in the reply")
        check(st.get("role") == "navicore" and st.get("relayId") is None and st.get("attached"),
              f"/_api/status right after the attach: role {st.get('role')!r} relayId {st.get('relayId')!r}")
        check(fake.asked == [ping], f"the NaviCore was asked {fake.asked}: only the PING may go to a port whose "
                                    f"direct PONG settles it (host.py:1176-1180)")
        check(took < 2.0, f"the attach took {took:.1f} s: a direct PONG should end the questions at once")
        check("COMFAKE is a NaviCore" in _STATE["out"].since(k), "the host did not print 'COMFAKE is a NaviCore'")
    finally:
        s.req("POST", "/_api/detach", {})


@case("a WCB on the port (no direct PONG) gets ?RELAY,WIFI and ?WDP,DUMP and is recorded as role wcb with its WDP "
      "self number as relayId - also when NaviCore's mesh PONG is mirrored back", group="identify")
def identify_wcb(ctx):
    s = _server()
    keys = _probe_keys()
    for kind in ("wcb", "wcb_mirror"):
        k = len(_STATE["out"].text)
        code, body, took, fake, st = _attach_board(s, kind)
        try:
            check(code == 200 and body.get("role") == "wcb", f"{kind}: attach answered {code} {body}, expected role wcb")
            check(st.get("role") == "wcb" and st.get("relayId") == 1,
                  f"{kind}: /_api/status role {st.get('role')!r} relayId {st.get('relayId')!r}, expected wcb 1")
            check(fake.asked == keys, f"{kind}: the port was asked {fake.asked}, expected {keys}")
            check("COMFAKE is W1hil, a WCB" in _STATE["out"].since(k), f"{kind}: no 'COMFAKE is W1hil, a WCB' line")
        finally:
            s.req("POST", "/_api/detach", {})


@case("a MgmtRelay on the port says [relay] and is recorded as role relay with relayId 19; a self row numbered 25 "
      "is a WCB with no relayId", group="identify")
def identify_relay(ctx):
    s = _server()
    for kind, role, rid in (("relay", "relay", 19), ("wcb_25", "wcb", None)):
        code, body, _, fake, st = _attach_board(s, kind)
        try:
            check(code == 200 and body.get("role") == role, f"{kind}: attach answered {code} {body}")
            check(st.get("role") == role and st.get("relayId") == rid,
                  f"{kind}: /_api/status role {st.get('role')!r} relayId {st.get('relayId')!r}, expected {role} {rid}")
        finally:
            s.req("POST", "/_api/detach", {})


@case("a port where nothing answers stays role '' after all three questions, and the attach answers only once they "
      "have timed out", group="identify")
def identify_silent(ctx):
    s = _server()
    host = _host()
    keys = _probe_keys()
    k = len(_STATE["out"].text)
    code, body, took, fake, st = _attach_board(s, "silent")
    try:
        check(code == 200 and body.get("ok") and body.get("role") == "",
              f"attach answered {code} {body}, expected ok with role ''")
        check(st.get("role") == "" and st.get("relayId") is None and st.get("attached"),
              f"/_api/status: role {st.get('role')!r} relayId {st.get('relayId')!r}")
        check(fake.asked == keys, f"the silent port was asked {fake.asked}, expected {keys}")
        floor = host.SERIAL_PONG_WAIT_S + host.SERIAL_IDENTIFY_S - 0.3
        check(took >= floor, f"the attach answered after {took:.1f} s, before the questions could time out "
                             f"({floor + 0.3:.1f} s): the role would not be settled when the page reads it")
        check("COMFAKE did not say what it is" in _STATE["out"].since(k), "no 'did not say what it is' line")
    finally:
        s.req("POST", "/_api/detach", {})


@case("a port unidentified at its attach is asked again when the reconnect loop reattaches it, and recorded; once "
      "identified, a reattach asks nothing", group="identify")
def identify_on_reattach(ctx):
    s = _server()
    Board = _board_class()
    keys = _probe_keys()
    order = ["silent", "wcb", "navicore"]
    made = []

    def make(spec, on_data, on_lost):
        kind = order[min(len(made), len(order) - 1)]
        b = Board(kind, _script(kind), on_data, on_lost)
        made.append(b)
        return b
    s.make = make

    def wait_attached(n, timeout=12.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st = s.status()
            if len(made) >= n and made[n - 1].is_open and st.get("attached"):
                return st
            time.sleep(0.1)
        raise AssertionError(f"the reconnect loop did not reattach the port ({len(made)} transport(s) built)")
    try:
        code, body = s.req("POST", "/_api/attach", {"kind": "serial", "port": "COMFAKE"})
        check(code == 200 and body.get("role") == "", f"the first attach: {code} {body}, expected role ''")
        made[0].lose()
        wait_attached(2)
        deadline = time.monotonic() + 10
        st = s.status()
        while st.get("role") != "wcb" and time.monotonic() < deadline:
            time.sleep(0.1)
            st = s.status()
        check(st.get("role") == "wcb" and st.get("relayId") == 1,
              f"after the reattach /_api/status role {st.get('role')!r} relayId {st.get('relayId')!r}: an unidentified "
              f"port is asked again on a reattach (host.py:1797)")
        check(made[1].asked == keys, f"the reattached port was asked {made[1].asked}, expected {keys}")
        made[1].lose()
        wait_attached(3)
        time.sleep(1.5)                  # longer than a direct PONG takes to be asked for and answered
        st = s.status()
        check(made[2].asked == [], f"an identified port was asked again on a reattach: {made[2].asked} "
                                   f"(_identify_attached_serial returns at once for a spec with a role)")
        check(st.get("role") == "wcb" and st.get("relayId") == 1,
              f"after a second reattach the role is {st.get('role')!r} relayId {st.get('relayId')!r}, not wcb 1")
    finally:
        s.req("POST", "/_api/detach", {})


# ------------------------------------------------------------------ group "window"
class _ParkAfterPublish:
    """The bridge's lock, but a thread inside Bridge.attach() that reaches for it again once its transport is published
    - attach() between the locked section that publishes and the one that records the target - is parked until go is
    set, once. Everything else passes straight through to the real RLock, the parked thread too once released."""

    def __init__(self, real, published):
        self.real, self.published = real, published
        self.parked, self.go = threading.Event(), threading.Event()
        self.armed = True
        self.local = threading.local()

    def acquire(self, *a, **k):
        if self.armed and getattr(self.local, "attaching", False) and self.published():
            self.armed = False
            self.parked.set()
            self.go.wait(15)
        return self.real.acquire(*a, **k)

    def release(self):
        return self.real.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


def _window_race(b, attach_call, detach_call, transport):
    """Run attach_call() on its own thread with b._lock parked in attach() after its publish (b.attach wrapped to mark
    the thread inside it); land detach_call() at the park or, when attach() never reaches for the lock after
    publishing, once attach_call() has returned -> (parked, what attach_call raised or None, whether it returned)."""
    park = _ParkAfterPublish(b._lock, lambda: transport() is not None and b._transport is transport())
    real_attach = b.attach

    def attach(*a, **k):
        park.local.attaching = True
        try:
            return real_attach(*a, **k)
        finally:
            park.local.attaching = False
    b._lock, b.attach = park, attach
    out = {}

    def run():
        try:
            attach_call()
            out["error"] = None
        except Exception as e:          # noqa: BLE001 - what it raised is the result
            out["error"] = e
    th = threading.Thread(target=run, daemon=True)
    try:
        th.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not park.parked.is_set() and th.is_alive():
            time.sleep(0.01)
        detach_call()
        park.go.set()
        th.join(15)
    finally:
        park.go.set()
        b._lock = park.real
        del b.attach                    # the instance attribute: the class's attach() again
    return park.parked.is_set(), out.get("error"), not th.is_alive()


@case("a detach that lands after attach() has published its transport but before it records the target is not "
      "undone: nothing is attached or wanted afterwards, and what was opened is closed", group="window")
def window_bridge(ctx):
    host = _host()
    Gate = _gate_class()
    b = host.Bridge()
    g = Gate("A", block=False)
    parked, err, done = _window_race(b, lambda: b.attach(g, "A", dict(SPEC_A)), b.detach, lambda: g)
    check(done, "attach() never returned")
    ctx.note(f"bridge_attach_detach_window: attach parked after its publish: {parked}; it returned "
             f"{type(err).__name__ + ': ' + str(err) if err else 'normally'}")
    check(g.opened == 1 and g.closes >= 1 and not g.is_open, f"A was opened {g.opened}, closed {g.closes}: the "
                                                             f"detach must close what the attach published")
    check(b._transport is None and not b.attached, "A is still attached after the detach")
    check(not b.wants_link and b._spec is None and not b._want,
          f"the detach was undone: want={b._want} spec={b._spec} - attach() recorded its target after the detach had "
          f"cleared it (host.py Bridge.attach, the second locked section), so the reconnect loop reattaches a link the "
          f"user stopped")


@case("through the HTTP routes, with the reconnect loop running: a POST /_api/detach landing in that window leaves the "
      "host detached, and the loop does not reattach in the next 4 s", group="window")
def window_http(ctx):
    s = _server()
    host = _host()
    b = host.bridge
    Gate = _gate_class()
    made = []

    def make(spec, on_data, on_lost):
        g = Gate(f"T{len(made)}", block=False, on_data=on_data, on_lost=on_lost)
        made.append(g)
        return g
    s.make = make
    spec = {"kind": "ws", "host": "127.0.0.1", "role": "navicore"}
    first = {}

    def attach():
        first["reply"] = s.req("POST", "/_api/attach", dict(spec))

    def detach():
        first["detach"] = s.req("POST", "/_api/detach", {})
    try:
        parked, err, done = _window_race(b, attach, detach, lambda: made[0] if made else None)
        check(done and err is None, f"the attach request did not come back: {err}")
        ctx.note(f"bridge_attach_detach_window: over HTTP the attach parked after its publish: {parked}; it answered "
                 f"{first.get('reply')}; the detach {first.get('detach')}")
        t0 = time.monotonic()
        seen = []
        while time.monotonic() - t0 < 4.0:
            st = s.status()
            seen.append((st.get("attached"), st.get("wantsLink")))
            time.sleep(0.25)
        check(len(made) == 1, f"the reconnect loop built {len(made) - 1} more transport(s) after the detach: the "
                              f"detach was undone and the link the user stopped was reattached")
        check(all(a is False and w is False for a, w in seen),
              f"/_api/status after the detach: (attached, wantsLink) went {sorted(set(seen))}, expected only "
              f"(False, False)")
        check(made[0].closes >= 1 and not made[0].is_open, "the published transport was never closed")
    finally:
        s.req("POST", "/_api/detach", {})


if __name__ == "__main__":
    sys.exit(_ixpy.main())
