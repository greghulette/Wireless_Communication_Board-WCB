"""flash._get's retry rules against a local HTTP server, under Intellex's venv (intellex.ghget_retry_unit, IX-WP3). No
GitHub: the URLs are http://127.0.0.1:<port>, and INTELLEX_OFFLINE does not gate _get itself (only reachable() asks it).

Intellex src/flash.py:223-263: GitHub's throttles (403, 429, 5xx) are retried, waiting what Retry-After asks (else
1.5 s x attempt); any other 4xx is final at once; a refused connection is "no network" (no_network, :159-184) and fails
fast so the offline cache is reached; the error names the URL.
"""
import http.server
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ixpy  # noqa: E402
from _ixpy import case, check  # noqa: E402


class _Server:
    """A threaded HTTP server whose paths answer a script: /seq/<name> answers seq[name] in turn, one per request."""

    def __init__(self, scripts):
        self.scripts, self.hits = scripts, {}
        srv = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                name = self.path.rsplit("/", 1)[-1]
                n = srv.hits.get(name, 0)
                srv.hits[name] = n + 1
                seq = srv.scripts[name]
                status, headers, body = seq[min(n, len(seq) - 1)]
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@case("429 and 403 with Retry-After are retried after the wait asked for; 404 is final at once; a refused connection "
      "fails fast", group="main")
def retries(ctx):
    import flash
    ok = (200, {}, b"firmware-bytes")
    srv = _Server({"throttle": [(429, {"Retry-After": "1"}, b"slow down"), ok],
                   "abuse": [(403, {"Retry-After": "1"}, b"abuse"), ok],
                   "gone": [(404, {}, b"nope"), ok],
                   "down": [(503, {"Retry-After": "1"}, b"later"), ok]})
    log = []
    try:
        for name in ("throttle", "abuse", "down"):
            t0 = time.monotonic()
            data = flash._get(f"{srv.url}/seq/{name}", log.append, attempts=4, timeout=5)
            dt = time.monotonic() - t0
            check(data == b"firmware-bytes", f"{name}: got {len(data)} bytes, not the second answer")
            check(srv.hits[name] == 2, f"{name}: {srv.hits[name]} requests, expected 2")
            check(0.9 <= dt < 3.0, f"{name}: the retry came after {dt:.2f} s; Retry-After asked for 1 s")
        check(any("GitHub said 429" in m for m in log), f"the 429 retry was not logged: {log}")
        t0 = time.monotonic()
        try:
            flash._get(f"{srv.url}/seq/gone", log.append, attempts=4, timeout=5)
            raise AssertionError("a 404 was returned as data")
        except flash.FlashError as e:
            check("/seq/gone" in str(e), f"the 404's error does not name the URL: {e}")
        check(srv.hits["gone"] == 1, f"404: {srv.hits['gone']} requests; a 404 must not be retried")
        check(time.monotonic() - t0 < 1.0, "404: took over a second")
    finally:
        srv.close()
    with socket.socket() as s:          # a port nothing listens on: connection refused
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    t0 = time.monotonic()
    try:
        flash._get(f"http://127.0.0.1:{dead}/x", log.append, attempts=4, timeout=5)
        raise AssertionError("a refused connection returned data")
    except flash.FlashError as e:
        check(f":{dead}/x" in str(e), f"the refused request's error does not name the URL: {e}")
    dt = time.monotonic() - t0
    # One refused connect to localhost costs about 2 s on Windows (measured 2.05 s on the bench PC, 2026-09-29); a
    # retry would add the 1.5 s backoff and a second connect (about 5.6 s in all). So under 3.5 s is one attempt.
    check(dt < 3.5, f"a refused connection took {dt:.2f} s: no_network() should break before the first 1.5 s backoff "
                    f"and a second attempt")


if __name__ == "__main__":
    sys.exit(_ixpy.main())
