"""A localhost JSON bridge into the Bench, for a Wizard (Playwright) test running as a subprocess.

While a Wizard test runs, Chrome owns that board's COM port and the harness owns everything else: the probes, the
other boards, the session log. The browser test reaches those through this bridge instead of opening a port it
would fight the harness for. Every route is a POST with a JSON body; a reply is JSON with either the result or
{"error": ...} (HTTP 409 for a failed expectation, 500 for anything else) or {"skip": ...} (HTTP 424, a wire the
bench does not have). Routes run one at a time: the Bench is not thread-safe, and the harness's own test thread
is parked in subprocess.wait() for the duration.

    /context                               what the harness told this run: device, com, wcb, vid, pid, pipe, args
    /note        {text}                    a line in session.log, tagged 'wizard'
    /config      {wcb}                     comparable config tokens (?backup / ?MGMT,PULL) — not of the board
                                           Chrome is holding
    /wire/mark   {wcb, port}               {"mark": n}; bytes after it are what the next calls see
    /wire/received {wcb, port, since}      {"hex": ...}
    /wire/expect {wcb, port, text|hex, since, timeout}
    /wire/send   {wcb, port, text|hex}     inject into the WCB port through the probe
    /hook        {name, ...}               a bench action the harness test offers its spec mid-run, by name
                                           (hil/intellex.py run_intellex_test hooks=): move the PC's WiFi adapter,
                                           mark or count a console's lines, judge the host's flash log. Its reply is
                                           what the hook returns; a name the run offers none of is a 409.

A pipe run (hil/wizard.py run_wizard_test(..., pipe=True)) keeps the device's port open in the harness and gives the
page a FAKE Web Serial port (tests/wizard/lib/navicore/shim.js + pipe.js) whose bytes go through these, for the
context's device only (docs/hil_plan/NAVICORE.md §5.2, INF7):

    /serial/mark    {device}               {"mark": n}; the lines after it are what /serial/read returns
    /serial/read    {device, since}        {"lines": [...], "next": n}: every line the device printed since `since`
    /serial/write   {device, text}         one line to the device (paced as the config tool paces it past 512 bytes)
    /serial/signals {device, dtr, rts}     a setSignals() call: recorded in session.log, NEVER applied (DTR/RTS reset
                                           NaviCore's native-USB S3)
    /sbus           {t, ...}               one RAM-only SBUS controller verb (a, sw, sl, tr, btn, lua, ping), for the
                                           live-grid spec; the saving verbs are refused (hil/sbus.py)

The /serial routes and /sbus run OUTSIDE the one-at-a-time lock: the pipe polls /serial/read every 20 ms while a
paced /serial/write of a 14 KB SET_CONFIG can take ~120 ms, and SerialDevice has its own locks (hil/serialdev.py:36-41).
The lines pass through SerialDevice, so Bench.log's credential filter (REDACT_KINDS) applies to both directions.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import read_config
from .runner import Skip

SBUS_VERBS = {"a", "sw", "sl", "tr", "btn", "lua", "ping"}     # RAM only; never mode/cfg/wificfg (they save)
UNLOCKED = ("/serial/", "/sbus")


def _data(body):
    if "hex" in body:
        return bytes.fromhex(body["hex"])
    return body["text"].encode()


class Bridge:
    def __init__(self, bench, context=None, hooks=None):
        self.bench = bench
        self.context = context or {}
        self.hooks = dict(hooks or {})    # /hook: {name: fn(body) -> a JSON-able reply}, run under the lock
        self.signals = []                 # every /serial/signals call, in order, for the harness test to check
        self._lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self._thread = None

    def __enter__(self):
        self._thread = threading.Thread(target=self.server.serve_forever, name="wizard-bridge", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    # ------------------------------------------------------------ routes
    def _link(self, body):
        return self.bench.links.require(int(body["wcb"]), body["port"])

    def _pipe_dev(self, body):
        """The device a /serial route may touch: the one this run piped, and only in a pipe run."""
        name = body.get("device") or self.context.get("device")
        if not self.context.get("pipe") or name != self.context.get("device"):
            raise AssertionError(f"no pipe to {name!r} in this run (pipe={bool(self.context.get('pipe'))}, "
                                 f"device={self.context.get('device')!r})")
        return name, self.bench.dev(name)

    def handle(self, path, body):
        b = self.bench
        if path == "/context":
            return self.context
        if path == "/note":
            b.log("wizard", "#", str(body.get("text", "")))
            return {}
        if path == "/config":
            return {"tokens": read_config(b, int(body["wcb"]))}
        if path == "/wire/mark":
            return {"mark": self._link(body).mark()}
        if path == "/wire/received":
            return {"hex": self._link(body).received(int(body["since"])).hex()}
        if path == "/wire/expect":
            got = self._link(body).expect(_data(body), timeout=float(body.get("timeout", 3.0)),
                                          since=body.get("since"))
            return {"hex": got.hex()}
        if path == "/wire/send":
            self._link(body).send(_data(body))
            return {}
        if path == "/hook":
            name = str(body.get("name", ""))
            if name not in self.hooks:
                raise AssertionError(f"this run offers no hook {name!r} (it has {sorted(self.hooks)})")
            return self.hooks[name](body) or {}
        if path == "/serial/mark":
            return {"mark": self._pipe_dev(body)[1].mark()}
        if path == "/serial/read":
            dev = self._pipe_dev(body)[1]
            since = int(body["since"])
            lines = dev.since(since)
            return {"lines": lines, "next": since + len(lines)}
        if path == "/serial/write":
            dev = self._pipe_dev(body)[1]
            text = str(body["text"])
            if "\n" in text or "\r" in text:
                raise AssertionError("/serial/write takes one line (NaviCore ends a line at CR or LF)")
            if len(text.encode()) + 1 > 512:
                dev.send_paced(text)      # 512-byte writes 4 ms apart, as sendLine does (index.html:5313-5321)
            else:
                dev.send(text)
            return {}
        if path == "/serial/signals":
            name, _ = self._pipe_dev(body)
            sig = {"dtr": body.get("dtr"), "rts": body.get("rts")}
            self.signals.append(sig)
            b.log("wizard", "#", f"{name}: page setSignals dtr={sig['dtr']} rts={sig['rts']} (recorded, not applied)")
            return {}
        if path == "/sbus":
            from .sbus import SbusCtl
            if body.get("t") not in SBUS_VERBS:
                raise AssertionError(f"/sbus refuses {body.get('t')!r}: only the RAM-only verbs {sorted(SBUS_VERBS)}")
            SbusCtl(b.dev("sbus")).send(body)
            return {}
        raise KeyError(path)

    def _handler(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                    if self.path.startswith(UNLOCKED):
                        code, reply = 200, bridge.handle(self.path, body)
                    else:
                        with bridge._lock:
                            code, reply = 200, bridge.handle(self.path, body)
                except Skip as e:
                    code, reply = 424, {"skip": str(e)}
                except AssertionError as e:
                    code, reply = 409, {"error": str(e)}
                except KeyError as e:
                    code, reply = 404, {"error": f"no route or field {e}"}
                except Exception as e:  # noqa: BLE001 — reported to the browser test, which fails with it
                    code, reply = 500, {"error": f"{type(e).__name__}: {e}"}
                data = json.dumps(reply).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):   # keep the console quiet; session.log has the 'wizard' notes
                pass

        return Handler
