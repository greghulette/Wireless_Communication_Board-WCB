"""A localhost JSON bridge into the Bench, for a Wizard (Playwright) test running as a subprocess.

While a Wizard test runs, Chrome owns that board's COM port and the harness owns everything else: the probes, the
other boards, the session log. The browser test reaches those through this bridge instead of opening a port it
would fight the harness for. Every route is a POST with a JSON body; a reply is JSON with either the result or
{"error": ...} (HTTP 409 for a failed expectation, 500 for anything else) or {"skip": ...} (HTTP 424, a wire the
bench does not have). Routes run one at a time: the Bench is not thread-safe, and the harness's own test thread
is parked in subprocess.wait() for the duration.

    /context                               what the harness told this run: device, com, wcb, vid, pid, args
    /note        {text}                    a line in session.log, tagged 'wizard'
    /config      {wcb}                     comparable config tokens (?backup / ?MGMT,PULL) — not of the board
                                           Chrome is holding
    /wire/mark   {wcb, port}               {"mark": n}; bytes after it are what the next calls see
    /wire/received {wcb, port, since}      {"hex": ...}
    /wire/expect {wcb, port, text|hex, since, timeout}
    /wire/send   {wcb, port, text|hex}     inject into the WCB port through the probe
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import read_config
from .runner import Skip


def _data(body):
    if "hex" in body:
        return bytes.fromhex(body["hex"])
    return body["text"].encode()


class Bridge:
    def __init__(self, bench, context=None):
        self.bench = bench
        self.context = context or {}
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
        raise KeyError(path)

    def _handler(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
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
