"""Why a GitHub firmware download failed, as Intellex tells it (src/ghstatus.py and its callers), under Intellex's venv.
No GitHub and no githubstatus.com: a local HTTP server stands in for both (ghstatus.STATUS_URL is pointed at it), and the
flashers' and the proxy's GitHub requests are answered from it or from stand-ins that raise what flash._get raises.

The rules (the same as the browser tools' "gh-explain" block in Wizard/flasher.js and config_tool/flasher.js):
  1. no internet  neither GitHub nor its status page answers -> "Can't reach the internet - ..."
  2. rate limit   403/429 with x-ratelimit-remaining: 0 (or GitHub's rate-limit message) -> the limit, its reset time
  3. GitHub down  githubstatus.com reports Git Operations / API Requests / Pages as not operational -> which, and that
                  it is GitHub's
  4. GitHub error a 5xx or no answer while the status page says all is well (or cannot be read) -> try again later
  5. not found    a 404 is ours: explain() is None and the caller's own message stands
and INTELLEX_OFFLINE (the leash the harness runs this under): offline mode is said, and the status page not fetched.

group "rules"  (intellex.gh_explain_unit): ghstatus.explain over real urllib errors from flash._get against the stand-in,
               the status page's cache, budget and component matching, and _get's own part (the cause kept, a spent hourly
               limit not retried).
group "callers" (intellex.gh_explain_callers_unit): flash.list_firmware / fetch_images and wcb_flash.fetch_images raise
               the explanation (plus the no-cached-copy note) with the technical detail in the log, keep a 404's message,
               and use a cached copy without asking anything; the host's /_api/gh routes answer 502 with `message` (the
               detail) and `explain` (the sentence), a GitHub 404 as a 404 without one, a refusal with its own words.
"""
import http.server
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ixpy  # noqa: E402
from _ixpy import case, check  # noqa: E402

OFFLINE = "Can't reach the internet - check this computer's connection."
TROUBLE_TAIL = ("The firmware download depends on GitHub, so this isn't something on your side or in the WCB tools - "
                "try again once GitHub recovers. Live status: githubstatus.com")
SERVER_TAIL = ("That's usually a short-lived problem on GitHub's side - try again in a few minutes. If it keeps "
               "happening, check githubstatus.com.")
RATE_HEAD = "GitHub's download limit for this network is used up (60 requests an hour without signing in)."
RATE_TAIL = "This is GitHub's limit, not a fault in the WCB tools."


def components(**down):
    """A githubstatus.com components.json: every part operational except those named (name_with_underscores=status),
    plus parts a download never touches."""
    names = ["Git Operations", "API Requests", "Webhooks", "Issues", "Pull Requests", "Actions", "Packages", "Pages",
             "Codespaces", "Copilot"]
    out = []
    for n in names:
        out.append({"id": n.lower().replace(" ", ""), "name": n, "status": down.get(n.replace(" ", "_"), "operational"),
                    "group": False})
    return {"page": {"id": "kctbh9vrtdwd", "name": "GitHub", "url": "https://www.githubstatus.com"}, "components": out}


class _Server:
    """A threaded HTTP server: /seq/<name> answers seq[name] in turn (status, headers, body[, delay s]); /status/<name>
    answers status[name] (a JSON object, or (code, body)); every request is counted by path."""

    def __init__(self):
        self.seq, self.status, self.hits = {}, {}, {}
        srv = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split("?")[0]
                srv.hits[path] = srv.hits.get(path, 0) + 1
                kind, name = path.strip("/").split("/", 1)
                if kind == "status":
                    got = srv.status[name]
                    code, body, delay = (200, got, 0) if isinstance(got, dict) else (got + (0,))[:3]
                    headers, body = {"Content-Type": "application/json"}, json.dumps(body).encode()
                else:
                    seq = srv.seq[name]
                    n = srv.hits[path] - 1
                    code, headers, body, *rest = seq[min(n, len(seq) - 1)]
                    delay = rest[0] if rest else 0
                if delay:
                    time.sleep(delay)
                try:
                    self.send_response(code)
                    for k, v in headers.items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass                      # the client gave up first (the timeout cases)

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def count(self, prefix):
        return sum(n for p, n in self.hits.items() if p.startswith(prefix))

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _dead_url():
    with socket.socket() as s:          # a port nothing listens on: connection refused
        s.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{s.getsockname()[1]}/x"


class _Online:
    """INTELLEX_OFFLINE cleared (the harness sets it) and ghstatus pointed at the stand-in's /status/<which>, its cache
    empty; everything put back on exit."""

    def __init__(self, srv, which="ok", offline=False):
        self.srv, self.which, self.offline = srv, which, offline

    def __enter__(self):
        import ghstatus
        self.saved = (os.environ.get("INTELLEX_OFFLINE"), ghstatus.STATUS_URL, ghstatus._status_cache)
        if self.offline:
            os.environ["INTELLEX_OFFLINE"] = "1"
        else:
            os.environ.pop("INTELLEX_OFFLINE", None)
        ghstatus.STATUS_URL = self.which if self.which.startswith("http") else f"{self.srv.url}/status/{self.which}"
        ghstatus._status_cache = None
        return ghstatus

    def __exit__(self, *exc):
        import ghstatus
        env, ghstatus.STATUS_URL, ghstatus._status_cache = self.saved
        if env is None:
            os.environ.pop("INTELLEX_OFFLINE", None)
        else:
            os.environ["INTELLEX_OFFLINE"] = env


def _raised(fn):
    try:
        fn()
    except Exception as e:      # noqa: BLE001 - what it raised is the result
        return e
    raise AssertionError("expected a failure, got none")


def _get_error(srv, name, attempts=1, timeout=5.0):
    import flash
    return _raised(lambda: flash._get(f"{srv.url}/seq/{name}", lambda _m: None, attempts=attempts, timeout=timeout))


# ------------------------------------------------------------------ group "rules"
@case("each failure gets its sentence: offline mode says so and asks nothing; no internet (GitHub and its status page "
      "both silent); a spent rate limit with its reset time, from the headers or GitHub's message; GitHub down names the "
      "download parts that are not operational and only those; a 5xx, a timeout or no connection while all is well; a "
      "404 is None", group="rules")
def rules(ctx):
    import flash
    import ghstatus
    srv = _Server()
    reset = int(time.time()) + 1800
    srv.seq.update({
        "down": [(503, {}, b"<html>Service Unavailable</html>")],
        "gone": [(404, {}, b'{"message":"Not Found"}')],
        "limit": [(403, {"x-ratelimit-limit": "60", "x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)},
                   b'{"message":"API rate limit exceeded for 203.0.113.9."}')],
        "limit_msg": [(429, {}, b'{"message":"API rate limit exceeded for 203.0.113.9. (But here\'s the good news...)"}')],
        "abuse": [(403, {}, b'{"message":"Must have admin rights to Repository."}')],
        "slow": [(200, {}, b"late", 3.0)],
    })
    srv.status.update({"ok": components(),
                       "pages": components(Pages="partial_outage", Git_Operations="degraded_performance",
                                           Copilot="major_outage"),
                       "maint": components(API_Requests="under_maintenance"),
                       "others": components(Copilot="major_outage", Actions="partial_outage"),
                       "broken": (500, {"error": "HIL"})})
    problems = []

    def expect(label, got, want):
        if got != want:
            problems.append(f"{label}: {got!r}, expected {want!r}")

    try:
        # Offline mode (the leash): said as such, and the status page is not asked.
        with _Online(srv, "ok", offline=True) as g:
            expect("offline mode, nothing sent", g.explain(flash.GitHubUnreachable()), g.OFFLINE_MODE)
            refused = _raised(lambda: flash._get(_dead_url(), lambda _m: None, attempts=1))
            expect("offline mode, a refused connection", g.explain(refused), g.OFFLINE_MODE)
        if srv.count("/status/"):
            problems.append(f"offline mode fetched the status page {srv.count('/status/')} time(s)")

        with _Online(srv, "ok") as g:
            e = _get_error(srv, "limit")
            expect("403 with x-ratelimit-remaining 0", g.explain(e),
                   f"{RATE_HEAD} It resets at {time.strftime('%H:%M', time.localtime(reset))}. {RATE_TAIL}")
            g._status_cache = None
            e = _get_error(srv, "limit_msg")
            expect("429 naming the rate limit (no headers)", g.explain(e), f"{RATE_HEAD} It resets within the hour. {RATE_TAIL}")
            if srv.count("/status/"):
                problems.append("a rate limit fetched the status page: the headers already said why")
            expect("404", g.explain(_get_error(srv, "gone")), None)
            expect("503, all operational", g.explain(_get_error(srv, "down")), f"GitHub's servers answered with an error (503). {SERVER_TAIL}")
            expect("403 that is not a rate limit, all operational", g.explain(_get_error(srv, "abuse")),
                   f"GitHub's servers answered with an error (403). {SERVER_TAIL}")
            g._status_cache = None
            expect("no connection, all operational", g.explain(_raised(lambda: flash._get(_dead_url(), lambda _m: None, attempts=1))),
                   f"GitHub's servers answered with an error (no response). {SERVER_TAIL}")
            e = _raised(lambda: flash._get(f"{srv.url}/seq/slow", lambda _m: None, attempts=1, timeout=0.5))
            expect("a timeout, all operational", g.explain(e), f"GitHub's servers answered with an error (timeout). {SERVER_TAIL}")
            expect("nothing sent (reachable() said no), all operational", g.explain(flash.GitHubUnreachable()),
                   f"GitHub's servers answered with an error (no response). {SERVER_TAIL}")

        with _Online(srv, "pages") as g:
            expect("503, Pages and Git Operations down (Copilot ignored)", g.explain(_get_error(srv, "down")),
                   f"GitHub is having problems right now (Git Operations: degraded performance; Pages: partial outage). "
                   f"{TROUBLE_TAIL}")
            g._status_cache = None
            expect("no connection, Pages and Git Operations down", g.explain(_raised(lambda: flash._get(_dead_url(), lambda _m: None, attempts=1))),
                   f"GitHub is having problems right now (Git Operations: degraded performance; Pages: partial outage). "
                   f"{TROUBLE_TAIL}")
        with _Online(srv, "maint") as g:
            expect("API Requests under maintenance", g.explain(_get_error(srv, "down")),
                   f"GitHub is having problems right now (API Requests: under maintenance). {TROUBLE_TAIL}")
        with _Online(srv, "others") as g:
            expect("only parts a download never touches are down", g.explain(_get_error(srv, "down")),
                   f"GitHub's servers answered with an error (503). {SERVER_TAIL}")

        # The status page cannot be read: rule 4 for an answer from GitHub, rule 1 for none.
        for which, label in (("broken", "answering 500"), (_dead_url(), "refusing the connection")):
            with _Online(srv, which) as g:
                expect(f"503, the status page {label}", g.explain(_get_error(srv, "down")),
                       f"GitHub's servers answered with an error (503). {SERVER_TAIL}")
                expect(f"no connection, the status page {label}",
                       g.explain(_raised(lambda: flash._get(_dead_url(), lambda _m: None, attempts=1))), OFFLINE)
                expect(f"nothing sent, the status page {label}", g.explain(flash.GitHubUnreachable()), OFFLINE)
        # Not a GitHub failure at all: None.
        expect("a FlashError of ours", ghstatus.explain(flash.FlashError("no NaviCore app image in firmware/")), None)
    finally:
        srv.close()
    check(not problems, "; ".join(problems))


@case("the status page is asked once a minute, never longer than its budget: a second explanation inside the minute "
      "asks nothing, an expired answer is asked again, a failed read is not remembered, and a status page that hangs is "
      "given up on in about 4 s", group="rules")
def status_cache_budget(ctx):
    import ghstatus
    srv = _Server()
    srv.seq["down"] = [(503, {}, b"down")]
    srv.status.update({"ok": components(), "broken": (500, {"x": 1}), "hung": (200, components(), 8.0)})
    problems = []
    try:
        with _Online(srv, "ok") as g:
            e = _get_error(srv, "down")
            g.explain(e)
            g.explain(e)
            if srv.count("/status/ok") != 1:
                problems.append(f"two explanations inside a minute asked the status page {srv.count('/status/ok')} times")
            g._status_cache = (time.monotonic() - g.STATUS_TTL_S - 1, [])
            g.explain(e)
            if srv.count("/status/ok") != 2:
                problems.append("an answer older than STATUS_TTL_S was used instead of asking again")
        with _Online(srv, "broken") as g:
            g.explain(e)
            g.explain(e)
            if srv.count("/status/broken") != 2:
                problems.append(f"a failed status read was remembered ({srv.count('/status/broken')} reads for two)")
        with _Online(srv, "hung") as g:
            t0 = time.monotonic()
            got = g.explain(e)
            took = time.monotonic() - t0
            if not 3.0 <= took < 6.0:
                problems.append(f"a status page that hangs held the explanation {took:.1f} s (budget {g.STATUS_TIMEOUT_S} s)")
            if got != f"GitHub's servers answered with an error (503). {SERVER_TAIL}":
                problems.append(f"after the hung status page: {got!r}")
    finally:
        srv.close()
    check(not problems, "; ".join(problems))


@case("flash._get keeps the urllib error as the cause (its code and headers reach ghstatus) and does not wait out a "
      "spent hourly limit (x-ratelimit-remaining: 0): one request, no backoff - a 429 with Retry-After is still retried",
      group="rules")
def get_cause_and_limit(ctx):
    import flash
    srv = _Server()
    srv.seq.update({"limit": [(403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(time.time()) + 600)},
                               b'{"message":"API rate limit exceeded"}'), (200, {}, b"ok")],
                    "throttle": [(429, {"Retry-After": "1"}, b"slow down"), (200, {}, b"ok")],
                    "down": [(503, {}, b"down")]})
    problems = []
    try:
        t0 = time.monotonic()
        e = _raised(lambda: flash._get(f"{srv.url}/seq/limit", lambda _m: None, attempts=4, timeout=5))
        took = time.monotonic() - t0
        if srv.count("/seq/limit") != 1 or took > 1.0:
            problems.append(f"a spent hourly limit: {srv.count('/seq/limit')} request(s) in {took:.1f} s, expected one at once")
        if not isinstance(e.__cause__, urllib.error.HTTPError) or e.__cause__.code != 403:
            problems.append(f"the FlashError's cause is {type(e.__cause__).__name__}, not the HTTPError 403")
        elif b"rate limit" not in (getattr(e.__cause__, "ix_body", b"") or b""):
            problems.append("the 403's body was not kept for the rate-limit check")
        data = flash._get(f"{srv.url}/seq/throttle", lambda _m: None, attempts=4, timeout=5)
        if data != b"ok" or srv.count("/seq/throttle") != 2:
            problems.append(f"a 429 with Retry-After was not retried ({srv.count('/seq/throttle')} requests)")
        e = _raised(lambda: flash._get(f"{srv.url}/seq/down", lambda _m: None, attempts=1, timeout=5))
        if not isinstance(e.__cause__, urllib.error.HTTPError) or e.__cause__.code != 503 or "/seq/down" not in str(e):
            problems.append(f"a 503: cause {type(e.__cause__).__name__}, message {str(e)[:80]!r}")
        e = _raised(lambda: flash._get(_dead_url(), lambda _m: None, attempts=1, timeout=5))
        if not isinstance(e.__cause__, OSError):
            problems.append(f"a refused connection's cause is {type(e.__cause__).__name__}")
    finally:
        srv.close()
    check(not problems, "; ".join(problems))


# ------------------------------------------------------------------ group "callers"
def _http_error(url, code, headers=None, body=b""):
    import email.message
    import io
    msg = email.message.Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError(url, code, "HIL", msg, io.BytesIO(body))


class _GitHub:
    """flash._get / wcb_flash._get stand-ins raising what the real one raises: FlashError from the urllib error."""

    def __init__(self, fail):
        self.fail, self.urls = fail, []

    def get(self, url, _log=None, **_k):
        import flash
        self.urls.append(url)
        cause = self.fail(url)
        raise flash.FlashError(f"cannot reach GitHub: {url} -- {cause}") from cause


def _swap(gh, reachable=True):
    import flash
    import wcb_flash
    saved = (flash.reachable, flash._get, wcb_flash.reachable, wcb_flash._get)
    flash.reachable = wcb_flash.reachable = (lambda *a, **k: reachable)
    flash._get = wcb_flash._get = gh.get
    return lambda: (setattr(flash, "reachable", saved[0]), setattr(flash, "_get", saved[1]),
                    setattr(wcb_flash, "reachable", saved[2]), setattr(wcb_flash, "_get", saved[3]))


@case("the native flashers, nothing cached: NaviCore's fetch_images and the WCB's raise GitHub's explanation and that "
      "Intellex has no saved copy, the technical detail (the URL and HTTP code) in the flash log only; offline mode says "
      "so; a 404 keeps its own message; a cached listing and image are used without asking anything", group="callers")
def flashers(ctx):
    import flash
    import ghstatus
    import wcb_flash
    srv = _Server()
    srv.status["pages"] = components(Pages="major_outage")
    problems = []
    branch = "hil-ghstat-nocache"
    want = (f"GitHub is having problems right now (Pages: major outage). {TROUBLE_TAIL} {ghstatus.NO_COPY}")
    try:
        for label, call in (("NaviCore", lambda log: flash.fetch_images(branch, log)),
                            ("WCB ESP32", lambda log: wcb_flash.fetch_images("ESP32", None, branch, log))):
            log = []
            restore = _swap(_GitHub(lambda url: _http_error(url, 503)))
            try:
                with _Online(srv, "pages"):
                    e = _raised(lambda: call(log.append))
            finally:
                restore()
            if not isinstance(e, flash.FlashError) or str(e) != want:
                problems.append(f"{label}, a 503 while Pages is down: {type(e).__name__} {str(e)[:160]!r}")
            tech = [m for m in log if m.startswith("GitHub fetch failed: ")]
            if not tech or "503" not in tech[0] or "api.github.com" not in tech[0]:
                problems.append(f"{label}: the technical detail is not in the log ({log[-3:]})")
            if "503" in str(e) or "api.github.com" in str(e):
                problems.append(f"{label}: the technical detail leaked into the message the user sees")
            # A 404: the branch has no such image - ours, the message as it was.
            restore = _swap(_GitHub(lambda url: _http_error(url, 404)))
            try:
                with _Online(srv, "pages"):
                    e = _raised(lambda: call(lambda _m: None))
            finally:
                restore()
            if "HTTP Error 404" not in str(e) or "GitHub is having problems" in str(e):
                problems.append(f"{label}, a 404: {str(e)[:120]!r}, expected the original 'HTTP Error 404' message")
            # Offline mode: said, the status page not asked.
            n0 = srv.count("/status/")
            restore = _swap(_GitHub(lambda url: _http_error(url, 503)), reachable=False)
            try:
                with _Online(srv, "pages", offline=True):
                    e = _raised(lambda: call(lambda _m: None))
            finally:
                restore()
            if str(e) != f"{ghstatus.OFFLINE_MODE} {ghstatus.NO_COPY}":
                problems.append(f"{label}, offline mode: {str(e)[:160]!r}")
            if srv.count("/status/") != n0:
                problems.append(f"{label}, offline mode: the status page was fetched")

        # Cached: the listing and the image come from the cache, GitHub's failure is never explained.
        name = "WCB_6.2.1_250646RSEP2026_hilghstat_ESP32.bin"
        _ixpy.seed_cache(ctx, "wcb", branch, {name: b"\xE9IMAGE", "WCB_6.2.1_250646RSEP2026_hilghstat_ESP32_part.bin": b"P",
                                              "WCB_6.2.1_250646RSEP2026_hilghstat_ESP32_boot.bin": b"B"})
        n0 = srv.count("/status/")
        restore = _swap(_GitHub(lambda url: _http_error(url, 503)))
        try:
            with _Online(srv, "pages"):
                fw = wcb_flash.fetch_images("ESP32", None, branch, lambda _m: None)
        finally:
            restore()
        if fw["app"]["data"] != b"\xE9IMAGE":
            problems.append("a cached WCB set was not used when GitHub failed")
        if srv.count("/status/") != n0:
            problems.append("a flash served from the cache asked the status page")
    finally:
        srv.close()
    check(not problems, "; ".join(problems))


@case("the host's /_api/gh routes, nothing cached: 502 with `message` the technical detail and `explain` GitHub's "
      "sentence plus the no-saved-copy note (contents and raw); a GitHub 404 is a 404 with no `explain`; a refusal of "
      "ours (another repo) explains itself; offline mode says so", group="callers")
def proxy_routes(ctx):
    import ghstatus
    from bridge_units import _server
    s = _server()
    srv = _Server()
    srv.status["ok"] = components()
    owner, repo, path = "greghulette", "Wireless_Communication_Board-WCB", "Code/bin"
    branch = "hil-ghstat-proxy"
    contents = "/_api/gh/contents?" + urllib.parse.urlencode({"owner": owner, "repo": repo, "path": path, "ref": branch})
    raw = "/_api/gh/raw?" + urllib.parse.urlencode({"owner": owner, "repo": repo, "ref": branch,
                                                     "name": "WCB_x_ESP32.bin"})
    problems = []

    def get(p):
        code, hdr, body = s.http("GET", p)
        try:
            j = json.loads(body.decode("utf-8"))
        except ValueError:
            j = {}
        return code, j

    reset = int(time.time()) + 900
    rate = (f"{RATE_HEAD} It resets at {time.strftime('%H:%M', time.localtime(reset))}. {RATE_TAIL} "
            f"{ghstatus.NO_COPY}")
    try:
        for p, what in ((contents, "contents"), (raw, "raw")):
            restore = _swap(_GitHub(lambda url: _http_error(url, 503)))
            try:
                with _Online(srv, "ok"):
                    code, j = get(p)
            finally:
                restore()
            if code != 502 or j.get("explain") != f"GitHub's servers answered with an error (503). {SERVER_TAIL} {ghstatus.NO_COPY}":
                problems.append(f"{what}, a 503: {code} explain {str(j.get('explain'))[:120]!r}")
            if "503" not in j.get("message", ""):
                problems.append(f"{what}, a 503: the technical detail is not in `message` ({j.get('message', '')[:120]!r})")
            restore = _swap(_GitHub(lambda url: _http_error(url, 403, {"x-ratelimit-remaining": "0",
                                                                         "x-ratelimit-reset": str(reset)})))
            try:
                with _Online(srv, "ok"):
                    code, j = get(p)
            finally:
                restore()
            if code != 502 or j.get("explain") != rate:
                problems.append(f"{what}, rate limited: {code} explain {str(j.get('explain'))[:140]!r}")
            restore = _swap(_GitHub(lambda url: _http_error(url, 404)))
            try:
                with _Online(srv, "ok"):
                    code, j = get(p)
            finally:
                restore()
            if code != 404 or "explain" in j:
                problems.append(f"{what}, a GitHub 404: {code} {j}, expected a 404 with no explain")
            restore = _swap(_GitHub(lambda url: _http_error(url, 503)), reachable=False)
            try:
                with _Online(srv, "ok", offline=True):
                    code, j = get(p)
            finally:
                restore()
            if code != 502 or j.get("explain") != f"{ghstatus.OFFLINE_MODE} {ghstatus.NO_COPY}":
                problems.append(f"{what}, offline mode: {code} explain {str(j.get('explain'))[:120]!r}")
        code, j = get("/_api/gh/contents?" + urllib.parse.urlencode({"owner": "someone", "repo": "elsewhere",
                                                                       "path": path, "ref": "main"}))
        if code != 502 or not j.get("explain") or j.get("explain") != j.get("message"):
            problems.append(f"a refusal (another repo): {code} {j}, expected 502 explaining itself")
    finally:
        srv.close()
    check(not problems, "; ".join(problems))


if __name__ == "__main__":
    sys.exit(_ixpy.main())
