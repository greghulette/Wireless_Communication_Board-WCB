"""Intellex under test (IX-WP2..IX-WP4, docs/HIL_TESTING.md §10): a staged copy of Intellex per test, its host started
on a free port with the leash on (offline, no serial port, no discovery host unless a test names them), then HTTP,
WebSocket and Playwright checks against it. Nothing here needs a board: these run while a bench run holds every COM
port, because the leash keeps the host off all of them. The tests that do use boards are in s33_intellex_bench.py.

Intellex serves the NaviCore config tool at / and the WCB Wizard at /wcb/Wizard/, and bridges each page's /_link
WebSocket to one transport. Its own no-hardware smoke tools run here too, one harness test each, against the staged
copy - so one report covers them and a regression in either repo shows up in the same place. So do checks that import
Intellex's modules (tests/intellex/py, run under its venv by run_intellex_py): the flash rules, the flash pipeline with a
fake esptool and a fake port, the GitHub retry rules, paths and logs. Seeded data comes from tests/intellex/fixtures (a
crafted wiki, recorded-shape esptool output) or is written into the stage (a firmware cache for a test branch).
"""
import glob
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.parse

from hil.intellex import (FW_REPOS, STAGE_DIR, IntellexHost, intellex_dir, require, run_intellex_py,
                          run_intellex_test, run_venv, seed_firmware, seed_wiki, stage)
from hil.runner import Skip, test
from hil.ws import WsClient, WsEndpoint

SHIM_TAG = '<script src="/_intellex.js"></script>'   # Intellex host.py SHIM_TAG
STATUS_KEYS = ("attached", "target", "lastError", "reconnecting", "wantsLink", "kind", "role", "relayId")
# Every POST route in Intellex's build_app EXCEPT /_api/wifi-bounce: were the Origin guard ever broken, that request
# would run netsh against this PC's WiFi adapter, the one the week's unattended session reaches the internet through.
GUARDED_POSTS = ("/_api/attach", "/_api/detach", "/_api/identify", "/_api/signals", "/_api/branch", "/_api/flash",
                 "/_api/flash-wcb", "/_api/update-webui", "/_api/update-firmware", "/_api/update-wiki",
                 "/_api/open-window")
EVIL = "http://evil.example"


def _snapshot(d):
    """{relative path: (size, mtime)} of every file under d (empty when d does not exist)."""
    out = {}
    for root, _, files in os.walk(d):
        for f in files:
            p = os.path.join(root, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            out[os.path.relpath(p, d)] = (st.st_size, st.st_mtime)
    return out


# ------------------------------------------------------------------ IX-WP2: the plumbing itself
def _real_data_dir():
    """Intellex's own per-user directory on this computer - Intellex src/paths.py user_data_dir() without its
    INTELLEX_DATA_DIR override: %LOCALAPPDATA%\\Intellex, ~/Library/Application Support/Intellex, or the XDG one."""
    if sys.platform == "win32":
        return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local"), "Intellex")
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "Intellex")
    return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "Intellex")


@test("intellex.stage_selftest", "A staged Intellex host starts offline and leashed: /_api/status answers, no serial port "
      "is listed, its log lands in its own stage, and nothing is written to the real per-user Intellex directory (IX-WP2)")
def stage_selftest(bench):
    require(bench)
    real = _real_data_dir()
    before = _snapshot(real)
    sd = stage(bench, "intellex.stage_selftest")
    with IntellexHost(bench, sd) as host:
        st, body = host.json("GET", "/_api/status")
        assert st == 200 and isinstance(body, dict), f"/_api/status: {st} {body}"
        missing = [k for k in STATUS_KEYS if k not in body]
        assert not missing, f"/_api/status lacks {missing}: {sorted(body)}"
        assert body["attached"] is False and body["wantsLink"] is False, f"a fresh host is attached: {body}"
        st, ports = host.json("GET", "/_api/ports")
        listed = ports if isinstance(ports, list) else ports.get("ports", ports) if isinstance(ports, dict) else ports
        assert st == 200 and listed == [], f"the leash lets the host list serial ports: {st} {ports}"
    after = _snapshot(real)
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    assert not changed, f"the staged host wrote under the real {real}: {changed[:5]}"
    logs = glob.glob(os.path.join(sd, "appdata", "Intellex", "logs", "*.log"))
    assert logs, "the staged host left no log in its own appdata"


# ------------------------------------------------------------------ IX-WP3: Intellex's own smoke tools, on the stage
_PY_SMOKES = ("smoke_fanout", "smoke_probe_identity", "smoke_reconnect_identity", "smoke_ghproxy", "smoke_wiki",
              "smoke_test_hooks")
_NODE_SMOKES = ("smoke_mesh_route", "smoke_doorway_via_wcb", "smoke_ota_label", "smoke_wizard_reconnect")


def _register_py_smoke(name):
    tid = "intellex.smoke_repo_" + name[len("smoke_"):]

    @test(tid, f"Intellex tools/{name}.py passes against the staged copy (no hardware; IX-WP3)")
    def run(bench):
        require(bench)
        sd = stage(bench, tid, tools="shipped", include_data=True)
        rc, out = run_venv(bench, [os.path.join("tools", name + ".py")], cwd=sd, timeout=600)
        tail = [l for l in out.splitlines() if l.strip()][-8:]
        assert rc == 0, f"{name}.py exited {rc}:\n    " + "\n    ".join(tail)
        if any(l.strip().startswith("SKIP") for l in tail):
            bench.note(f"{name}: " + "; ".join(l.strip() for l in tail if l.strip().startswith("SKIP")))
    return run


def _register_node_smoke(name):
    tid = "intellex.smoke_repo_" + name[len("smoke_"):]

    @test(tid, f"Intellex tools/{name}.js passes against the staged copy's shim (no hardware; IX-WP3)")
    def run(bench):
        require(bench)
        node = shutil.which("node")
        if not node:
            raise Skip("Node.js is not on PATH")
        sd = stage(bench, tid, tools="shipped")
        p = subprocess.run([node, os.path.join("tools", name + ".js")], cwd=sd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300,
                           creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        out = (p.stdout or "") + (p.stderr or "")
        for line in out.splitlines():
            if line.strip():
                bench.log("intellex", "<", line.rstrip())
        tail = [l for l in out.splitlines() if l.strip()][-8:]
        assert p.returncode == 0, f"{name}.js exited {p.returncode}:\n    " + "\n    ".join(tail)
    return run


for _n in _PY_SMOKES:
    _register_py_smoke(_n)
for _n in _NODE_SMOKES:
    _register_node_smoke(_n)


@test("intellex.smoke_repo_compile", "Intellex's no-build checks on the staged copy: py_compile of src/ and tools/, "
      "node --check of the shim, jscheck of shell.html and launcher.html (Intellex CLAUDE.md Verifying; IX-WP3)")
def smoke_repo_compile(bench):
    require(bench)
    sd = stage(bench, "intellex.smoke_repo_compile", tools="none")
    files = sorted(glob.glob(os.path.join(sd, "src", "*.py")) + glob.glob(os.path.join(sd, "tools", "*.py")))
    rc, out = run_venv(bench, ["-m", "py_compile"] + files, cwd=sd)
    assert rc == 0, f"py_compile failed:\n{out[-800:]}"
    node = shutil.which("node")
    if not node:
        raise Skip("Node.js is not on PATH (py_compile passed)")
    nf = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    p = subprocess.run([node, "--check", os.path.join("src", "intellex_shim.js")], cwd=sd, capture_output=True,
                       text=True, timeout=60, creationflags=nf)
    assert p.returncode == 0, f"node --check intellex_shim.js:\n{(p.stdout + p.stderr)[-800:]}"
    jscheck = r"C:\Users\ghulette\tools\jscheck.js"
    if not os.path.isfile(jscheck):
        bench.note("jscheck.js not found - shell.html and launcher.html not checked")
        return
    for page in ("shell.html", "launcher.html"):
        p = subprocess.run([node, jscheck, os.path.join("src", page)], cwd=sd, capture_output=True, text=True,
                           timeout=60, creationflags=nf)
        assert p.returncode == 0, f"jscheck {page}:\n{(p.stdout + p.stderr)[-800:]}"


# ------------------------------------------------------------------ IX-WP3: the host's API and its guards
@test("intellex.routes", "Intellex serves both tools unforked: / and /wcb/Wizard/ carry the shim once, as their first "
      "script, and are otherwise the bundled file byte for byte but for a ?v=<mtime>.<size> stamp on each of the page's "
      "own scripts and stylesheet, uncached; /wcb/Wizard redirects; /_intellex.js opens with the branch prelude; with "
      "no bundle each tool route answers 503 (IX-WP3)")
def routes(bench):
    require(bench)
    sd = stage(bench, "intellex.routes")
    problems = []
    with IntellexHost(bench, sd) as host:
        pages = (("/", os.path.join(sd, "src", "webui", "index.html")),
                 ("/wcb/Wizard/", os.path.join(sd, "src", "webui_wcb", "Wizard", "index.html")),
                 ("/wcb/Wizard/index.html", os.path.join(sd, "src", "webui_wcb", "Wizard", "index.html")))
        for path, disk in pages:
            st, hdr, body = host.http("GET", path)
            html = body.decode("utf-8", "replace")
            ctype = {k.lower(): v for k, v in hdr.items()}.get("content-type", "")
            if st != 200 or "text/html" not in ctype:
                problems.append(f"{path}: {st} {ctype}")
                continue
            if html.count(SHIM_TAG) != 1:
                problems.append(f"{path}: the shim tag appears {html.count(SHIM_TAG)} times")
            elif html.find(SHIM_TAG) != html.lower().find("<script"):
                problems.append(f"{path}: the shim is not the first script")
            with open(disk, encoding="utf-8", errors="replace") as f:
                on_disk = f.read()
            # _inject_shim inserts the tag and a newline before the first <script>, and _versioned stamps the page's own
            # files ?v=<mtime>.<size> (Intellex host.py, 2026-10-15: a webview kept running a cached app.js).
            if re.sub(r'\?v=[0-9a-f]+\.[0-9a-f]+"', '"', html).replace(SHIM_TAG + chr(10), "", 1) != on_disk:
                problems.append(f"{path}: the served page differs from {os.path.relpath(disk, sd)} beyond the shim tag "
                                f"and the ?v= stamps")
            if not re.search(r'<script src="[^"/:?]+\?v=[0-9a-f]+\.[0-9a-f]+"', html):
                problems.append(f"{path}: none of the page's own scripts carries a ?v= stamp")
            cache = {k.lower(): v for k, v in hdr.items()}.get("cache-control", "")
            if "no-store" not in cache:
                problems.append(f"{path}: Cache-Control is {cache!r}, not no-store")
        st, hdr, _ = host.http("GET", "/wcb/Wizard")
        loc = {k.lower(): v for k, v in hdr.items()}.get("location", "")
        if st not in (301, 308) or not loc.endswith("/wcb/Wizard/"):
            problems.append(f"/wcb/Wizard: {st} Location {loc!r}, not a redirect to /wcb/Wizard/")
        st, _, body = host.http("GET", "/_intellex.js")
        if st != 200 or not body.decode("utf-8", "replace").lstrip().startswith("window.__intellexBranches"):
            problems.append(f"/_intellex.js: {st}, does not open with window.__intellexBranches")
    sd2 = stage(bench, "intellex.routes_nobundle", tools="none")
    with IntellexHost(bench, sd2) as host:
        for path in ("/", "/wcb/Wizard/"):
            st, _, body = host.http("GET", path)
            if st != 503 or len(body) < 20:
                problems.append(f"{path} with no bundle: {st} ({len(body)} bytes), not a 503 explanation")
    assert not problems, "; ".join(problems)


@test("intellex.origin_guard", "Every POST route but wifi-bounce refuses a foreign Origin with 403 before its handler "
      "runs, and the /_link upgrade refuses a foreign Origin while accepting 127.0.0.1, localhost and none (IX-WP3)")
def origin_guard(bench):
    require(bench)
    sd = stage(bench, "intellex.origin_guard")
    problems = []
    with IntellexHost(bench, sd) as host:
        for path in GUARDED_POSTS:
            st, _, _ = host.http("POST", path, {}, headers={"Origin": EVIL})
            if st != 403:
                problems.append(f"POST {path} from {EVIL}: {st}")
        st, body = host.json("GET", "/_api/status")
        if body.get("wantsLink") or body.get("attached"):
            problems.append(f"a refused attach still changed the host: {body}")
        try:
            WsClient("127.0.0.1", host.port, "/_link", origin=EVIL).close()
            problems.append(f"/_link accepted Origin {EVIL}")
        except ConnectionError as e:
            if "403" not in str(e):
                problems.append(f"/_link from {EVIL}: {e}")
        for origin in (f"http://127.0.0.1:{host.port}", f"http://localhost:{host.port}", None):
            try:
                WsClient("127.0.0.1", host.port, "/_link", origin=origin).close()
            except (ConnectionError, OSError) as e:
                problems.append(f"/_link refused Origin {origin}: {e}")
    assert not problems, "; ".join(problems)


@test("intellex.link_error_frame", "A /_link write with nothing attached comes back as one TEXT line "
      "{\"type\":\"ERROR\",...} naming the failure (IX-WP3)")
def link_error_frame(bench):
    require(bench)
    sd = stage(bench, "intellex.link_error_frame")
    with IntellexHost(bench, sd) as host:
        ws = WsClient("127.0.0.1", host.port, "/_link")
        try:
            ws.send_text("?VERSION\n")
            text = ws.read_until('"ERROR"', timeout=5)
        finally:
            ws.close()
    line = next((l for l in text.splitlines() if '"ERROR"' in l), "")
    assert line, f"no ERROR line came back: {text[:200]!r}"
    msg = json.loads(line)
    assert msg.get("type") == "ERROR" and "link write failed" in msg.get("msg", ""), f"the ERROR line: {line}"


@test("intellex.attach_validation", "POST /_api/attach refuses bad JSON, an unknown kind and a serial target with no "
      "port (400), refuses a port outside INTELLEX_SERIAL_ALLOW and a WiFi host outside INTELLEX_WS_ALLOW - the "
      "default 192.168.4.1 included - (502, nothing contacted) while keeping the target until detach, drops an "
      "out-of-range relayId and an unknown role; open-window refuses a remote URL and has no window hook (IX-WP3)")
def attach_validation(bench):
    """Intellex src/host.py api_attach. The WiFi leash (INTELLEX_WS_ALLOW, Intellex 2026-10-13; transport.ws_allowed,
    checked in Bridge.rebuild before a transport exists) is given 127.0.0.1 alone here: a host outside it is refused
    with the variable named, before anything connects (192.0.2.1 is TEST-NET-1 and 192.168.4.1 is every board's access
    point - the default a ws attach with no host takes). Bridge.rebuild's own fallback with no target at all is
    Intellex's tools/smoke_test_hooks.py H5 (intellex.smoke_repo_test_hooks)."""
    require(bench)
    sd = stage(bench, "intellex.attach_validation")
    problems = []
    with IntellexHost(bench, sd, ws_hosts=["127.0.0.1"]) as host:
        for body, want in ((b"not json", 400), ({"kind": "bogus"}, 400), ({"kind": "serial"}, 400)):
            st, _, _ = host.http("POST", "/_api/attach", body, headers={"Content-Type": "application/json"})
            if st != want:
                problems.append(f"attach {body!r}: {st}, not {want}")
        # COM250 exists nowhere; the leash must refuse it before pyserial is asked (a real bench port could be in use).
        st, body = host.json("POST", "/_api/attach", {"kind": "serial", "port": "COM250"})
        if st != 502 or "INTELLEX_SERIAL_ALLOW" not in str(body):
            problems.append(f"attach COM250 outside the allow list: {st} {body}")
        st, stat = host.json("GET", "/_api/status")
        if not stat.get("wantsLink") or stat.get("attached"):
            problems.append(f"after a refused attach the target should be kept, unattached, until detach: {stat}")
        host.detach()
        st, stat = host.json("GET", "/_api/status")
        if stat.get("wantsLink"):
            problems.append(f"detach did not forget the target: {stat}")
        # A ws host outside INTELLEX_WS_ALLOW: refused before any connect, the variable named, the target kept.
        for req, want in (({"kind": "ws", "host": "192.0.2.1", "role": "navicore"}, "192.0.2.1"),
                          ({"kind": "ws", "role": "navicore"}, "192.168.4.1")):
            st, body = host.json("POST", "/_api/attach", req, timeout=20)
            st2, stat = host.json("GET", "/_api/status")
            if st != 502 or f"{want} is not in INTELLEX_WS_ALLOW" not in str(body):
                problems.append(f"attach {req} outside the WiFi allow list: {st} {body}, expected 502 '{want} is not "
                                f"in INTELLEX_WS_ALLOW'")
            if not stat.get("wantsLink") or stat.get("attached") or \
                    f"{want} is not in INTELLEX_WS_ALLOW" not in str(stat.get("lastError")):
                problems.append(f"after attach {req} was refused by the leash: {stat}, expected the target kept, "
                                f"unattached, with the refusal as lastError")
            host.detach()
        # A ws target on a port nothing listens on: refused fast, and the role/relayId rules are visible in /_api/status.
        for req, role, rid in (({"kind": "ws", "host": "127.0.0.1", "role": "evil", "relayId": 25}, "", None),
                               ({"kind": "ws", "host": "127.0.0.1", "role": "relay", "relayId": 19}, "relay", 19)):
            st, body = host.json("POST", "/_api/attach", req, timeout=20)
            st2, stat = host.json("GET", "/_api/status")
            if st == 200:
                problems.append(f"attach {req} succeeded on a closed port")
            elif "INTELLEX_WS_ALLOW" in str(body):
                problems.append(f"attach {req}: refused by the leash ({body}) although 127.0.0.1 is allowed")
            if (stat.get("role") or "") != role or (stat.get("relayId") or None) != rid:
                problems.append(f"attach {req}: status role {stat.get('role')!r} relayId {stat.get('relayId')!r}, "
                                f"expected {role!r} {rid!r}")
            host.detach()
        st, _ = host.json("POST", "/_api/open-window", {"url": "https://example.com/"})
        if st != 400:
            problems.append(f"open-window with a remote URL: {st}, not 400")
        st, _ = host.json("POST", "/_api/open-window", {"url": "/_shell"})
        if st != 501:
            problems.append(f"open-window with no window hook: {st}, not 501")
    assert not problems, "; ".join(problems)


@test("intellex.settings_branch", "POST /_api/branch stores a valid branch per product (in the stage's settings.json "
      "only) and refuses spaces, '?', 101 characters and an unknown product; the choice reaches /_api/branches and "
      "the shim prelude (IX-WP3)")
def settings_branch(bench):
    require(bench)
    sd = stage(bench, "intellex.settings_branch")
    problems = []
    with IntellexHost(bench, sd) as host:
        for product, branch in (("wcb", "WIFI"), ("navicore", "feature/x")):
            st, body = host.json("POST", "/_api/branch", {"product": product, "branch": branch})
            if st != 200 or not body.get("ok"):
                problems.append(f"branch {product}={branch}: {st} {body}")
        st, br = host.json("GET", "/_api/branches")
        if br != {"navicore": "feature/x", "wcb": "WIFI"}:
            problems.append(f"/_api/branches: {br}")
        st, _, js = host.http("GET", "/_intellex.js")
        head = js.decode("utf-8", "replace")[:300]
        if '"WIFI"' not in head or '"feature/x"' not in head:
            problems.append(f"the shim prelude does not carry the branches: {head[:120]!r}")
        for product, branch in (("wcb", "a b"), ("wcb", "x?y"), ("wcb", "x" * 101), ("bogus", "main")):
            st, body = host.json("POST", "/_api/branch", {"product": product, "branch": branch})
            if st != 400:
                problems.append(f"branch {product}={branch[:20]!r}: {st}, not refused")
    saved = os.path.join(sd, "appdata", "Intellex", "settings.json")
    if not os.path.isfile(saved):
        problems.append("settings.json was not written inside the stage")
    assert not problems, "; ".join(problems)


@test("intellex.offline_con_floor", "Offline (INTELLEX_OFFLINE), every network feature fails fast and says so: the "
      "tool-version check reports offline, the branch list still offers main and the current branch, and the "
      "tool/wiki/firmware updates refuse without touching the bundles (IX-WP3)")
def offline_con_floor(bench):
    require(bench)
    sd = stage(bench, "intellex.offline_con_floor")
    problems = []
    index = os.path.join(sd, "src", "webui", "index.html")
    before = os.stat(index).st_mtime
    with IntellexHost(bench, sd) as host:
        st, ver = host.json("GET", "/_api/webui-version", timeout=30)
        if st != 200 or "offline" not in json.dumps(ver).lower():
            problems.append(f"/_api/webui-version offline: {st} {str(ver)[:200]}")
        st, bl = host.json("GET", "/_api/branch-list", timeout=30)
        if st != 200 or not isinstance(bl, dict):
            problems.append(f"/_api/branch-list: {st} {bl}")
        elif "main" not in json.dumps(bl):
            problems.append(f"/_api/branch-list offline does not offer main: {str(bl)[:200]}")
        for path in ("/_api/update-webui?tool=navicore", "/_api/update-wiki", "/_api/update-firmware"):
            st, body = host.json("POST", path, {}, timeout=120)
            if st == 200 and isinstance(body, dict) and body.get("ok"):
                problems.append(f"POST {path} succeeded offline: {str(body)[:200]}")
    if os.stat(index).st_mtime != before:
        problems.append("an offline update rewrote the NaviCore bundle")
    assert not problems, "; ".join(problems)


def _hdr(headers, name):
    return {k.lower(): v for k, v in headers.items()}.get(name.lower(), "")


# Every GET route of Intellex's build_app (src/host.py:1765-1842) the leash lets answer offline: (path, status, the
# Content-Type it starts with, the JSON keys it must carry or None).
API_GETS = (
    ("/_api/status", 200, "application/json", STATUS_KEYS),
    ("/_api/ports", 200, "application/json", ("ports",)),
    ("/_api/discover", 200, "application/json", ("candidates",)),
    ("/_api/webui-version", 200, "application/json", ("bundled", "published", "stale", "checked", "reason", "wcb")),
    ("/_api/firmware", 200, "application/json", ("navicore", "wcb")),
    ("/_api/branches", 200, "application/json", ("navicore", "wcb")),
    ("/_api/branch-list", 200, "application/json", ("navicore", "wcb")),
    ("/_api/log", 200, "application/json", ("path", "dir", "tail")),
    ("/_api/version", 200, "application/json", ("version", "commit", "built")),
    ("/_api/flash-status", 200, "application/json", ("running", "ok", "error", "version", "percent", "log")),
    ("/_api/wiki", 200, "application/json", ("wikis",)),
    ("/_launcher", 200, "text/html", None),
    ("/_shell", 200, "text/html", None),
    ("/_intellex.js", 200, "application/javascript", None),
    ("/wiki/", 200, "text/html", None),
    ("/_assets/intellex-mark-32.png", 200, "image/png", None),
    ("/wcb/Images/r2logo.png", 200, "image/png", None),
    ("/Images/r2logo.png", 200, "image/png", None),
    ("/flasher.js", 200, "", None),
    ("/wcb/Wizard/app.js", 200, "", None),
)


@test("intellex.routes_api", "Every GET route of Intellex answers offline with its status, content type and JSON keys: "
      "no port listed, no discovery host, a log inside the stage, GitHub proxy calls for other repos refused (502), "
      "an unknown path 404, a POST to a GET route 405, /_link without an upgrade refused (IX-WP3)")
def routes_api(bench):
    """The rest of the plan's intellex.routes row (INTELLEX.md §3.3: 'every route answers with the status and content
    type in §1.3'); intellex.routes covers the tool pages. /_api/version is not compared with Intellex's HEAD: from a
    stage inside this repo, version.py's git fallback (Intellex src/version.py:43-67) finds THIS repo's commit, since
    the stage has no build stamp and no .git of its own; stage() records Intellex's real HEAD in session.log."""
    require(bench)
    sd = stage(bench, "intellex.routes_api")
    problems = []
    with IntellexHost(bench, sd) as host:
        for path, want, ctype, keys in API_GETS:
            st, hdr, body = host.http("GET", path, timeout=30)
            got_type = _hdr(hdr, "content-type")
            if st != want or not got_type.startswith(ctype):
                problems.append(f"GET {path}: {st} {got_type!r}, expected {want} {ctype!r}")
                continue
            if keys:
                try:
                    obj = json.loads(body.decode("utf-8"))
                except ValueError:
                    problems.append(f"GET {path}: not JSON")
                    continue
                missing = [k for k in keys if k not in obj]
                if missing:
                    problems.append(f"GET {path}: lacks {missing}")
                elif path == "/_api/ports" and obj["ports"] != []:
                    problems.append(f"the leash let /_api/ports list {len(obj['ports'])} port(s)")
                elif path == "/_api/discover" and obj["candidates"] != []:
                    problems.append(f"the leash let /_api/discover probe {len(obj['candidates'])} host(s)")
                elif path == "/_api/log" and not os.path.normpath(obj["path"]).startswith(os.path.normpath(sd)):
                    problems.append(f"the host logs outside its stage: {obj['path']}")
                elif path == "/_api/flash-status" and obj["running"] is not False:
                    problems.append(f"a fresh host says a flash is running: {obj['running']}")
                elif path == "/_api/webui-version" and (obj["checked"] or obj["wcb"].get("checked")):
                    problems.append("offline, /_api/webui-version says it compared against Pages")
            if path in ("/_launcher", "/_shell", "/_intellex.js") and "no-store" not in _hdr(hdr, "cache-control"):
                problems.append(f"GET {path}: Cache-Control {_hdr(hdr, 'cache-control')!r}, not no-store")
            if path == "/wiki/" and "script-src 'nonce-" not in _hdr(hdr, "content-security-policy"):
                problems.append("GET /wiki/ carries no nonce CSP")
        st, hdr, _ = host.http("GET", "/wiki")
        if st != 301 or not _hdr(hdr, "location").endswith("/wiki/"):
            problems.append(f"GET /wiki: {st} -> {_hdr(hdr, 'location')!r}, not a 301 to /wiki/")
        for path in ("/_api/gh/contents?owner=someone&repo=elsewhere&path=Code/bin&ref=main",
                     "/_api/gh/raw?owner=someone&repo=elsewhere&ref=main&name=x.bin"):
            st, hdr, body = host.http("GET", path, timeout=30)
            if st != 502 or "message" not in json.loads(body.decode("utf-8") or "{}"):
                problems.append(f"GET {path.split('?')[0]} for another repo: {st}, not a 502 with a message")
        st, _, _ = host.http("GET", "/no-such-file-HIL.txt")
        if st != 404:
            problems.append(f"an unknown path: {st}, not 404")
        st, _, _ = host.http("POST", "/_api/status", {})
        if st != 405:
            problems.append(f"POST to the GET-only /_api/status: {st}, not 405")
        st, _, _ = host.http("GET", "/_link")
        if st in (200, 500) or st < 400:
            problems.append(f"GET /_link with no WebSocket upgrade: {st}")
    assert not problems, "; ".join(problems)


@test("intellex.wiki_security", "The offline docs share the control API's origin, so they stay inert: a name in the URL "
      "is escaped, every page carries a fresh script nonce, a wiki's own files are served sandboxed, '..' cannot leave "
      "the wiki, an unknown wiki is a 404 and a missing page says so (a crafted wiki; IX-WP3)")
def wiki_security(bench):
    """Intellex src/host.py:668-772 (_WIKI_CSP, _WIKI_ASSET_CSP, _wiki_shell escaping the title, wiki_page's traversal
    check) and src/wikidocs.py:71-85 (_page_file refuses '/', '\\' and '..'). The wiki is tests/intellex/fixtures/wiki,
    seeded into the stage (seed_wiki). That no script in a page runs is proved in a browser by intellex.ui_wiki."""
    require(bench)
    sd = stage(bench, "intellex.wiki_security")
    seed_wiki(sd)
    problems = []
    with open(os.path.join(sd, "src", "host.py"), "rb") as f:
        host_head = f.read(160)          # the bytes a traversal that reached src/host.py would serve
    with IntellexHost(bench, sd) as host:
        evil = "</title><script>window.__pwned=1</script>"
        st, hdr, body = host.http("GET", "/wiki/wcb/" + urllib.parse.quote(evil, safe=""))
        html = body.decode("utf-8", "replace")
        if st != 200 or evil in html or "&lt;/title&gt;&lt;script&gt;" not in html:
            problems.append(f"a script in the URL: {st}, reflected unescaped={evil in html}")
        nonces = []
        for _ in range(2):
            st, hdr, body = host.http("GET", "/wiki/wcb/Home")
            csp = _hdr(hdr, "content-security-policy")
            m = re.search(r"script-src 'nonce-([^']+)'", csp)
            page = body.decode("utf-8", "replace")
            if st != 200 or not m:
                problems.append(f"/wiki/wcb/Home: {st}, CSP {csp[:80]!r}")
                continue
            nonces.append(m.group(1))
            if f'<script nonce="{m.group(1)}">' not in page:
                problems.append("the page's own script does not carry the CSP's nonce")
            if "'unsafe-inline'" in csp.split("script-src", 1)[1].split(";", 1)[0]:
                problems.append("script-src allows 'unsafe-inline'")
        if len(nonces) == 2 and nonces[0] == nonces[1]:
            problems.append("two responses carried the same script nonce")
        for asset in ("Images/pic.png", "Images/evil.svg"):
            st, hdr, _ = host.http("GET", f"/wiki/wcb/{asset}")
            if st != 200 or not _hdr(hdr, "content-security-policy").startswith("sandbox"):
                problems.append(f"/wiki/wcb/{asset}: {st}, CSP {_hdr(hdr, 'content-security-policy')[:60]!r}, not "
                                f"sandboxed")
        for trick in ("..%2F..%2Fhost.py", "..%5C..%5Chost.py", "%2E%2E%2F%2E%2E%2Fhost.py", "Images%2F..%2F..%2F.."
                      "%2Fhost.py"):
            st, hdr, body = host.http("GET", f"/wiki/wcb/{trick}")
            if host_head in body:
                problems.append(f"/wiki/wcb/{trick} served host.py from outside the wiki")
        st, _, _ = host.http("GET", "/wiki/nosuchwiki/Home")
        if st != 404:
            problems.append(f"an unknown wiki: {st}, not 404")
        st, _, body = host.http("GET", "/wiki/wcb/No-Such-Page-HIL")
        if st != 200 or b"not in the downloaded copy" not in body:
            problems.append(f"a missing page: {st}, without the 'not in the downloaded copy' explanation")
    assert not problems, "; ".join(problems)


@test("intellex.wiki_code_verbatim", "(should) A [[wiki link]] written inside a fenced code block or inline code is shown "
      "as written, not rewritten into a markdown link (a crafted wiki; IX-WP3)")
def wiki_code_verbatim(bench):
    """Found writing IX-WP4 (INTELLEX.md finding 15). Intellex src/wikidocs.py rewrites [[Target]] and [[Text|Target]]
    with a regex over the whole markdown SOURCE before parsing (_wikilinks_to_md, :88-101, called by render_page :215
    and render_sidebar :227), so a [[...]] in a fenced block or in backticks comes out as '[Target](Target)': a changed
    example. The module's own header says its link rewriting is done in markdown-it's renderer rules precisely so that
    code and examples are left alone (:9-16). Nothing becomes a live link (intellex.ui_wiki checks that); the shown text
    is wrong. The wiki is tests/intellex/fixtures/wiki (Home.md, 'Code stays code')."""
    require(bench)
    sd = stage(bench, "intellex.wiki_code_verbatim", tools="none")
    seed_wiki(sd)
    with IntellexHost(bench, sd) as host:
        st, _, body = host.http("GET", "/wiki/wcb/Home")
    html = body.decode("utf-8", "replace")
    assert st == 200, f"/wiki/wcb/Home: {st}"
    shown = {"[[Not A Link]]": "in a fenced block", "[[Inline Not A Link]]": "in inline code"}
    lost = [f"{lit} {where}" for lit, where in shown.items() if lit not in html]
    assert not lost, (f"rewritten inside code, not shown as written: {'; '.join(lost)} (wikidocs.py:88-101 rewrites "
                      f"[[...]] before markdown-it parses the page)")


def _fw_files():
    """A firmware set per product for the test branch, with distinct bytes per file."""
    wcb_tag, nc_tag = "6.2.1_250646RSEP2026_hilcache", "0.2.0_280000RSEP2026"
    wcb = {f"WCB_{wcb_tag}_{k}.bin": f"{k}-{wcb_tag}".encode() * 40 for k in ("ESP32", "ESP32_part", "ESP32_boot")}
    nc = {f"NaviCore_{nc_tag}_ESP32S3.bin": f"nc-{nc_tag}".encode() * 50}
    return {"wcb": wcb, "navicore": nc}


@test("intellex.offline_gh_proxy", "Offline, the tools' GitHub firmware calls are answered from the cache: the listing "
      "with X-Intellex-Source cache and every download_url pointing back at the host, the bytes identical to the cached "
      "files; another repo, path, filename or an uncached branch is refused with 502; /_api/firmware reports the "
      "configured branch's set (IX-WP3)")
def offline_gh_proxy(bench):
    """The GitHub-proxy half of the plan's intellex.offline_con_floor row (INTELLEX.md §3.3), and its settings_branch
    row's '/_api/firmware' check. Intellex src/ghproxy.py:57-150 and host.py:775-805: only the two firmware repos and
    their bin directories, a bare filename, cached bytes served as they are. The cache is seeded into the stage for the
    branch hil-cache (seed_firmware) and settings.json names it for both products."""
    require(bench)
    branch = "hil-cache"
    sd = stage(bench, "intellex.offline_gh_proxy", settings={"branch": {"wcb": branch, "navicore": branch}})
    files = _fw_files()
    for product, fs in files.items():
        seed_firmware(sd, product, branch, fs)
    problems = []
    repos = {"wcb": ("greghulette", "Wireless_Communication_Board-WCB", "Code/bin"),
             "navicore": ("greghulette", "NaviCore", "firmware")}
    with IntellexHost(bench, sd) as host:
        st, summary = host.json("GET", "/_api/firmware")
        for product, fs in files.items():
            s = summary.get(product) or {}
            if s.get("branch") != branch or s.get("count") != len(fs):
                problems.append(f"/_api/firmware {product}: {s}, expected branch {branch} with {len(fs)} files")
        for product, (owner, repo, path) in repos.items():
            q = urllib.parse.urlencode({"owner": owner, "repo": repo, "path": path, "ref": branch})
            st, hdr, body = host.http("GET", f"/_api/gh/contents?{q}", timeout=30)
            if st != 200 or _hdr(hdr, "x-intellex-source") != "cache":
                problems.append(f"{product} listing: {st}, X-Intellex-Source {_hdr(hdr, 'x-intellex-source')!r}")
                continue
            listing = json.loads(body.decode("utf-8"))
            names = sorted(f.get("name") for f in listing)
            if names != sorted(files[product]):
                problems.append(f"{product} listing names {names}, expected {sorted(files[product])}")
            for f in listing:
                url = f.get("download_url") or ""
                if not url.startswith("/_api/gh/raw?"):
                    problems.append(f"{f.get('name')}: download_url {url[:60]!r} does not point back at the host")
                    continue
                st, hdr, data = host.http("GET", url, timeout=30)
                want = files[product].get(f["name"])
                if st != 200 or data != want or _hdr(hdr, "x-intellex-source") != "cache":
                    problems.append(f"{f['name']}: {st}, {len(data)} bytes (cached {len(want or b'')}), "
                                    f"sha {hashlib.sha256(data).hexdigest()[:12]}")
        owner, repo, path = repos["wcb"]
        refused = ((owner, "NaviCore-fork", path, branch), (owner, repo, "Code/WCB", branch),
                   (owner, repo, path, "no-such-branch-hil"))
        for o, r, p, ref in refused:
            q = urllib.parse.urlencode({"owner": o, "repo": r, "path": p, "ref": ref})
            st, _, _ = host.http("GET", f"/_api/gh/contents?{q}", timeout=30)
            if st != 502:
                problems.append(f"contents {o}/{r}/{p}@{ref}: {st}, not 502")
        name = next(iter(files["wcb"]))
        for bad in ("../" + name, "sub/" + name, "sub\\" + name, "WCB_not_cached_ESP32.bin"):
            q = urllib.parse.urlencode({"owner": owner, "repo": repo, "ref": branch, "name": bad})
            st, _, _ = host.http("GET", f"/_api/gh/raw?{q}", timeout=30)
            if st != 502:
                problems.append(f"raw {bad!r}: {st}, not 502")
    assert not problems, "; ".join(problems)


@test("intellex.settings_branch_dotdot", "(should) POST /_api/branch refuses a branch name with a '..' path segment: "
      "settings.py says its whitelist keeps '/../' out, and tool_base() puts the branch in a URL path (IX-WP3)")
def settings_branch_dotdot(bench):
    """Found writing IX-WP3 (INTELLEX.md finding 12). Intellex src/settings.py:33-37 says the whitelist exists so the
    branch 'must not be able to smuggle in URL operators (?, &, #, =, /../)', but [A-Za-z0-9._/-]+ accepts '../x'
    (valid_branch, :61-62), and tool_base() puts the branch in a URL PATH (:104-110), so fetch_webui would fetch a
    tool from https://greghulette.github.io/<product>/dev/../x/... - another of Greg's Pages sites. Low impact: only the
    launcher (same origin) can set it. The plan expected '../x' refused (INTELLEX.md §3.3 intellex.settings_branch); the
    code accepts it, so this is a (should) test, not a line of settings_branch."""
    require(bench)
    sd = stage(bench, "intellex.settings_branch_dotdot", tools="none")
    accepted = []
    with IntellexHost(bench, sd) as host:
        for branch in ("../x", "a/../b", "..", "feature/../../NaviCore"):
            st, body = host.json("POST", "/_api/branch", {"product": "wcb", "branch": branch})
            if st != 400:
                accepted.append(branch)
        host.json("POST", "/_api/branch", {"product": "wcb", "branch": "main"})
    assert not accepted, (f"POST /_api/branch accepted {accepted}: a '..' path segment reaches "
                          f"settings.tool_base()'s URL path (settings.py:104-110)")


@test("intellex.gh_proxy_ref_dotdot", "The GitHub proxy refuses a ref that is not a branch name: offline a ref of "
      "'..' or '.' serves no listing or image from outside the branch's own cache directory (files planted at the cache "
      "root and the product directory), and online no such ref is fetched or written; a cached branch is still served "
      "(plan finding 20; IX re-scan host.api.gh_proxy_ref_unchecked)")
def gh_proxy_ref_dotdot(bench):
    """INTELLEX.md finding 20. Intellex src/ghproxy.py contents() and raw() handed the page's ref to fwcache, whose _key
    (fwcache.py:81-89) flattens only '/' and '\\': a ref of '..' made the cache directory src/firmware/wcb/.., the cache
    root, and '.' the product directory. Offline the proxy served a listing.json or an image from there; online it
    wrote them there, and raw() put the ref in the raw.githubusercontent.com URL path ('../../<owner>/<repo>/<branch>'
    left the firmware repository). Here the HTTP half runs against a staged host with files planted where '..' and
    '.' reach, beside a real cached branch; tests/intellex/py/ghproxy_units.py group ref then runs the online half on
    the same stage, GitHub stood in for (flash.reachable, flash._get)."""
    require(bench)
    owner, repo, path = "greghulette", "Wireless_Communication_Board-WCB", "Code/bin"
    branch = "hil-cache"
    sd = stage(bench, "intellex.gh_proxy_ref_dotdot", tools="none")
    seed_firmware(sd, "wcb", branch, _fw_files()["wcb"])
    planted_name = "WCB_9.9.9_010000RJAN2026_planted_ESP32.bin"
    planted = b"PLANTED-OUTSIDE-ANY-BRANCH;" * 8
    fw = os.path.join(sd, "src", "firmware")
    for d in (fw, os.path.join(fw, "wcb")):              # where '..' and '.' land: the cache root, the product dir
        with open(os.path.join(d, planted_name), "wb") as f:
            f.write(planted)
        with open(os.path.join(d, "listing.json"), "w", encoding="utf-8") as f:
            json.dump([{"name": planted_name, "path": f"{path}/{planted_name}", "type": "file", "size": len(planted),
                        "download_url": f"https://raw.githubusercontent.com/{owner}/{repo}/x/{path}/{planted_name}"}], f)
    before = _snapshot(fw)
    problems = []
    with IntellexHost(bench, sd) as host:
        q = urllib.parse.urlencode({"owner": owner, "repo": repo, "path": path, "ref": branch})
        st, hdr, _ = host.http("GET", f"/_api/gh/contents?{q}", timeout=30)
        if st != 200 or _hdr(hdr, "x-intellex-source") != "cache":
            problems.append(f"control: the cached branch {branch}: {st}")
        for ref in ("..", "."):
            for what, query in (("contents", {"owner": owner, "repo": repo, "path": path, "ref": ref}),
                                ("raw", {"owner": owner, "repo": repo, "ref": ref, "name": planted_name})):
                st, _, body = host.http("GET", f"/_api/gh/{what}?{urllib.parse.urlencode(query)}", timeout=30)
                try:
                    msg = json.loads(body.decode("utf-8")).get("message", "") if st != 200 else ""
                except (ValueError, AttributeError):
                    msg = ""
                if st == 200 or planted in body or planted_name.encode() in body:
                    problems.append(f"{what} ref {ref!r}: {st}, served what was planted "
                                    f"{'at the cache root' if ref == '..' else 'in the product directory'}")
                elif st != 502 or "not a branch name" not in msg:
                    problems.append(f"{what} ref {ref!r}: {st} {msg[:120]!r}, not refused as a ref that is no branch")
    changed = sorted(k for k in set(before) | set(_snapshot(fw)) if before.get(k) != _snapshot(fw).get(k))
    if changed:
        problems.append(f"offline requests changed the firmware cache: {changed[:4]}")
    try:
        run_intellex_py(bench, "intellex.gh_proxy_ref_dotdot", "ghproxy_units.py", args={"group": "ref"}, stage_dir=sd)
    except AssertionError as e:
        problems.append(f"online (ghproxy_units.py): {e}")
    assert not problems, "; ".join(problems)


# ------------------------------------------------------------------ IX-WP3: Intellex's modules under its venv
@test("intellex.flash_rules_unit", "Intellex's flash rules, as pure functions under its venv: one anchored app image or a "
      "refusal, the size-matched ESP32-S3 bootloader, no full flash without a bootloader and table, sorted write lists "
      "with otadata always and NVS only on a reset, NaviCore's both-or-neither pair, and detect() on esptool 5.3.1's "
      "flash-id output (IX-WP3)")
def flash_rules_unit(bench):
    """tests/intellex/py/flash_units.py group rules: no board, no network, no esptool process (detect() reads recorded
    output through a patched proc.run)."""
    run_intellex_py(bench, "intellex.flash_rules_unit", "flash_units.py", args={"group": "rules"})


@test("intellex.flash_update_partition_escalates", "(should) Intellex's Update FW onto a board whose partition table "
      "differs from the build's escalates once to bootloader + table + app with NVS untouched, as the Wizard's flasher "
      "does; a matching table stays app-only (plan finding 5; IX-WP3)")
def flash_update_partition_escalates(bench):
    """INTELLEX.md finding 5: wcb_flash.write_list(app_only=True) writes the app alone (Intellex src/wcb_flash.py:302-307)
    while Wizard/flasher.js reads the table at 0x8000 and escalates on a mismatch (flasher.js:245-260, :465-511). The
    bench boards are min_spiffs already, so only this unit shows it (tests/intellex/py/flash_units.py
    group partition_should)."""
    run_intellex_py(bench, "intellex.flash_update_partition_escalates", "flash_units.py",
                    args={"group": "partition_should"})


@test("intellex.flash_pipeline_fake", "Intellex's /_api/flash-wcb and /_api/flash end to end in-process: one flash at a "
      "time (409), the port released and always handed back, esptool's argv (chip, sorted addresses, keep/keep/keep, "
      "--compress, 921600), app-only, full and factory write lists from the cache, esptool's words on a failure, and "
      "the USB-only and allow-list refusals - with a fake esptool and a fake port (IX-WP3)")
def flash_pipeline_fake(bench):
    """tests/intellex/py/flash_pipeline.py group pipeline: Intellex's real build_app() in the venv process, a FakeTransport
    for the port (named COMFAKE, the only name INTELLEX_SERIAL_ALLOW holds), and the fake esptool in
    tests/intellex/fixtures/fake_esptool first on PYTHONPATH. The script refuses to call a flash route unless
    `python -m esptool` answers with the fake's signature."""
    run_intellex_py(bench, "intellex.flash_pipeline_fake", "flash_pipeline.py", args={"group": "pipeline"},
                    timeout=300)


@test("intellex.flash_esptool5_output", "(should) With esptool 5.3.1, the esptool Intellex installs, /_api/flash-status "
      "follows the write's progress, and the NaviCore flash uses esptool 5's spellings so no 'Deprecated:' warnings land "
      "in the user's flash log (IX-WP3)")
def flash_esptool5_output(bench):
    """Found writing IX-WP3 (INTELLEX.md findings 13 and 14). _PCT_RE (Intellex src/flash.py:414, used by wcb_flash.py:38-39,
    :390) matches esptool 4's '(25 %)'; esptool 5.3.1 prints '[=====>   ]  25.0%' (esptool logger.py:223-248), so the
    progress bar sits at 0 % through the whole write and jumps to 100. flash.py:434-438 still spells the NaviCore write
    esptool 4's way (write_flash, --flash_mode, default_reset), which esptool 5 answers with 'Deprecated:' warnings
    (esptool cli_util.py:35-44, :350-383), the thing wcb_flash.py:352-354 says it avoids. The fake esptool prints what
    5.3.1 prints (tests/intellex/py/flash_pipeline.py group esptool5_should)."""
    run_intellex_py(bench, "intellex.flash_esptool5_output", "flash_pipeline.py", args={"group": "esptool5_should"},
                    timeout=300)


@test("intellex.gh_explain_unit", "Why a GitHub firmware download failed, as the host words it (src/ghstatus.py): offline "
      "mode said and nothing asked; no internet when neither GitHub nor githubstatus.com answers; the spent hourly rate "
      "limit with its reset time; GitHub reporting Git Operations / API Requests / Pages down (and only those); a 5xx, "
      "timeout or no connection while all is well; a 404 left alone; the status page asked once a minute within ~4 s; "
      "flash._get keeps the cause and does not wait out a spent limit (local stand-ins, no GitHub)")
def gh_explain_unit(bench):
    """tests/intellex/py/ghstatus_units.py group rules: Intellex src/ghstatus.py explain/failure/status_trouble and
    src/flash.py _get, against a local HTTP server standing in for GitHub and githubstatus.com."""
    run_intellex_py(bench, "intellex.gh_explain_unit", "ghstatus_units.py", args={"group": "rules"})


@test("intellex.gh_explain_callers_unit", "With nothing cached, both native flashers raise GitHub's explanation plus "
      "'no saved copy' with the HTTP code in the flash log only, a 404 keeps its message, a cached set asks nothing; the "
      "host's /_api/gh routes answer 502 with `message` (the detail) and `explain` (the sentence), a GitHub 404 as a 404 "
      "without one, a refusal explaining itself (in-process host, GitHub stood in for)")
def gh_explain_callers_unit(bench):
    """tests/intellex/py/ghstatus_units.py group callers: Intellex src/flash.py / src/wcb_flash.py fetch_images
    (list_firmware, download -> flash.explained) and src/ghproxy.py contents/raw through host.py's real build_app()."""
    run_intellex_py(bench, "intellex.gh_explain_callers_unit", "ghstatus_units.py", args={"group": "callers"})


@test("intellex.ui_gh_explain_navicore", "The config tool under Intellex shows the host's explanation for a firmware "
      "download it could not serve - the Latest check's status line and fetchFirmwareImages' error (both OTA buttons) - "
      "asking the host once and githubstatus.com never (Playwright, offline host, no cache)")
def ui_gh_explain_navicore(bench):
    run_intellex_test(bench, "intellex.ui_gh_explain_navicore")


@test("intellex.ui_gh_explain_wizard", "The Wizard under Intellex shows the host's explanation for a firmware download "
      "it could not serve (getBinaryData: both OTA paths), the 502 in its log only, never asking githubstatus.com "
      "(Playwright, offline host, no cache)")
def ui_gh_explain_wizard(bench):
    run_intellex_test(bench, "intellex.ui_gh_explain_wizard")


@test("intellex.ghget_retry_unit", "flash._get against a local HTTP server: 429, 403 and 503 are retried after the "
      "Retry-After wait, a 404 is final at once, a refused connection fails fast (IX-WP3)")
def ghget_retry_unit(bench):
    """tests/intellex/py/ghget_retry.py: Intellex src/flash.py:159-184 (no_network) and :223-263 (_get). No GitHub."""
    run_intellex_py(bench, "intellex.ghget_retry_unit", "ghget_retry.py")


@test("intellex.paths_logs_unit", "Where Intellex keeps things: a frozen build's user copy only when non-empty, wikis per "
      "subdirectory, updates in the user directory, the platform's own data directory with no INTELLEX_DATA_DIR and "
      "the NaviLink move there done once and never merged - on every platform; the log prune across both prefixes by "
      "timestamp; winsize.fit clamping and centring; certs.is_cert_error on a buried failure (IX-WP3; IX re-scan "
      "paths.data_dir_rules)")
def paths_logs_unit(bench):
    """tests/intellex/py/paths_logs.py, in a scratch directory inside the stage. The frozen rules re-point
    INTELLEX_DATA_DIR per case; the NaviLink move, which INTELLEX_DATA_DIR bypasses, re-points the platform's base
    (LOCALAPPDATA on Windows, HOME on macOS and Linux) - so neither case skips off Windows any more, as both did until
    the IX re-scan's paths.data_dir_rules row."""
    run_intellex_py(bench, "intellex.paths_logs_unit", "paths_logs.py")


@test("intellex.identify_direct_pong", "(should) Only a direct PONG identifies a serial port as a NaviCore: one relayed "
      "over the mesh (it carries sys and id) does not; a busy port reads as busy and one outside INTELLEX_SERIAL_ALLOW is "
      "never opened (plan finding 3)")
def identify_direct_pong(bench):
    """INTELLEX.md finding 3 (DX11: fixed test-first): discover.identify_serial accepts any PONG (Intellex
    src/discover.py:221-222) though its docstring says only a direct one counts (:154-158). A fake pyserial Serial
    answers the PING, so no port is opened (tests/intellex/py/discover_units.py)."""
    run_intellex_py(bench, "intellex.identify_direct_pong", "discover_units.py", args={"group": "direct_pong_should"})


@test("intellex.flash_update_partition_edges", "Update FW's partition check where the board's table cannot be read or "
      "the build lacks a piece: app-only while the app fits the legacy 0x140000 slot and refused ('Use Flash FW "
      "instead') one byte past it; no table in the build, no read and app-only; a differing table with no bootloader in "
      "the build refused; every refusal writes nothing and restarts the board (fake esptool; IX re-scan "
      "flash.wcb.update_escalates)")
def flash_update_partition_edges(bench):
    """tests/intellex/py/flash_units.py group partition_edges: Intellex src/wcb_flash.py:372-393 (update_needs_full) and
    :523-535 (flash), the branches intellex.flash_update_partition_escalates leaves out. esptool is patched in-process
    (proc.run, proc.popen): a 4 MB ESP32, a read of 0x8000 that answers the case's table or exits 2."""
    run_intellex_py(bench, "intellex.flash_update_partition_edges", "flash_units.py",
                    args={"group": "partition_edges"})


@test("intellex.bridge_superseded_attach", "An attach still inside open() when a newer attach or a detach lands closes "
      "what it opened and raises 'superseded' instead of publishing over the newer link, and the bridge's lock is free "
      "while open() blocks; through POST /_api/attach the first of two racing attaches answers 502 and the second's "
      "target is the one attached, and lastError stays empty beside it - the loser's 'superseded' is not recorded "
      "(in-process fakes; IX re-scan host.bridge.superseded_attach, host.api.superseded_last_error)")
def bridge_superseded_attach(bench):
    """tests/intellex/py/bridge_units.py group superseded: Intellex src/host.py Bridge.attach/detach (:214-251), whose
    generation count is what stops a slow open() from leaking a transport nothing can close (a held COM port with its
    reader still feeding bytes). A transport whose open() waits on an Event stands in for the slow port or handshake;
    the HTTP case runs Intellex's real build_app() in the venv process, its rebuild() handing out such transports.
    The re-scan's host.api.superseded_last_error: api_attach wrote the loser's 'superseded by a newer attach' into
    lastError after the winner cleared it, so /_api/status showed an error beside a healthy newer link. Since Intellex
    2026-10-13 a failure is recorded only while that attach's target is still the current one (Bridge.note_failure)."""
    run_intellex_py(bench, "intellex.bridge_superseded_attach", "bridge_units.py", args={"group": "superseded"})


@test("intellex.attach_identify_role", "A serial attach asks the port what it is before it answers: a NaviCore gets "
      "the PING alone and is role navicore; a WCB - also one mirroring NaviCore's mesh PONG - gets ?RELAY,WIFI and "
      "?WDP,DUMP and is role wcb with its WDP number as relayId; a relay is role relay, relayId 19; a silent port stays "
      "''; an unidentified port is asked again when the loop reattaches it, an identified one is not (in-process fakes; "
      "IX re-scan host.api.attach_serial_identify_role)")
def attach_identify_role(bench):
    """tests/intellex/py/bridge_units.py group identify: Intellex src/host.py _identify_attached_serial (:1152-1231),
    api_attach (:1304) and reconnect_loop (:1797), on its real build_app() in the venv process with the reconnect loop
    running. A scripted fake COM port answers as each kind of board would (discover.classify reads the answers). The
    row's bench half - W1 and NaviCore's own ports - is not here: intellex.nc_via_usb_doorway covers its effect on the
    NaviCore tool through W1."""
    run_intellex_py(bench, "intellex.attach_identify_role", "bridge_units.py", args={"group": "identify"},
                    timeout=300)


# ------------------------------------------------------------------ found on the bench in IX-WP9: a link whose far end vanished
DROP_GAP_S = 2.5            # the longest /_api/status may go unanswered, polled every 0.25 s, while the host drops a link
DROP_SHOW_S = 3.0           # from the host's 'link is dead' line to /_api/status showing the link lost
DROP_WATCH_S = 40.0         # the probe's verdict comes ~8 s after the attach; its stall on Intellex e9f95f2 lasts 10 s


def drop_stall_problems(samples, verdict_at):
    """The /_api/status polls a test took while a host dropped a WebSocket link -> ([problem], facts). samples: [(sent,
    done, attached)] in seconds from the far end's silence, attached None for a poll that got no answer (done is when it
    gave up); verdict_at: when the host's 'link is dead' line arrived, or None. Every problem is a (should) of INTELLEX.md
    finding 19 but the first, which says the probe never fired at all."""
    answered = [(s, d, a) for s, d, a in samples if a is not None]
    gaps = [(b[1] - a[1], a[1]) for a, b in zip(answered, answered[1:])]
    gap, gap_from = max(gaps) if gaps else (0.0, None)
    lost = next((d for _, d, a in answered if a is False), None)
    facts = {"verdict_s": None if verdict_at is None else round(verdict_at, 1),
             "lost_s": None if lost is None else round(lost, 1), "longest_unanswered_s": round(gap, 1),
             "unanswered_polls": sum(1 for _, _, a in samples if a is None)}
    if verdict_at is None:
        return ["the host never printed its 'link is dead' verdict: its liveness probe did not fire on a silent far end "
                "(host.py reconnect_loop)"], facts
    problems = []
    if gap > DROP_GAP_S:
        problems.append(f"(should) /_api/status went unanswered for {gap:.1f} s from {gap_from:.1f} s after the far end "
                        f"fell silent (the verdict came at {verdict_at:.1f} s): host.py:1596 drops the link on the event "
                        f"loop, and closing the WebSocket waits websockets' close_timeout (10 s) for a close frame that "
                        f"never comes (INTELLEX.md finding 19)")
    if lost is None:
        problems.append("(should) /_api/status never showed the link lost (INTELLEX.md finding 19)")
    elif lost - verdict_at > DROP_SHOW_S:
        problems.append(f"(should) /_api/status showed the link lost {lost - verdict_at:.1f} s after the host's 'link is "
                        f"dead' line, over {DROP_SHOW_S:.0f} s (INTELLEX.md finding 19)")
    return problems, facts


@test("intellex.link_drop_no_stall", "(should) A WebSocket link whose far end vanishes - as an access point does when "
      "its board restarts: no close frame, no FIN, new connects refused - is dropped without stalling the host: "
      "/_api/status never goes unanswered for over 2.5 s and shows the link lost within 3 s of the host's own 'link is "
      "dead' line; a stand-in endpoint on 127.0.0.2:80, no board and no WiFi (INTELLEX.md finding 19)")
def link_drop_no_stall(bench):
    """Intellex src/host.py reconnect_loop: after PROBE_IDLE_S (6 s) without a byte from the droid, three failed TCP
    probes of its port 80 print '<host> unreachable after <n>s idle - link is dead' and call bridge._drop() on the event
    loop itself (:1583-1596). _drop() closes the transport (:249-256), and WebSocketTransport.close() (ws_transport.py
    :89-99) runs websockets' closing handshake, which waits close_timeout - 10 s by default (websockets 17.1,
    sync/connection.py:1024-1040) - for a close frame a vanished far end never sends; every request the host would
    serve, every page, waits with it. The loop's own comment on open() (host.py:1604-1609) names this failure for the
    open. Found on the bench in intellex.wifi_link_loss (run 20260929-202852: the verdict 11.8 s after NaviCore's REBOOT,
    the status answering again at 21.9 s). Here the far end is hil/ws.py WsEndpoint on 127.0.0.2:80 (a loopback
    address nothing else here uses: attach_validation counts on 127.0.0.1:80 being closed), silenced one second after
    the attach: its listener closes, so the probe's connect is refused, and the open socket stays open with nothing
    read or sent. A poll of /_api/status every 0.25 s, 1 s each, from the silence until the status has shown the link
    lost for 2 s."""
    import threading
    import time
    require(bench)
    try:
        ep = WsEndpoint("127.0.0.2", 80)
    except OSError as e:
        raise Skip(f"nothing can listen on 127.0.0.2:80 here ({e}): the stand-in needs port 80, the one Intellex's "
                   f"WebSocket transport and liveness probe use (ws_transport.py DEFAULT_PORT, host.py reconnect_loop)")
    sd = stage(bench, "intellex.link_drop_no_stall", tools="none")
    samples, seen = [], {}
    try:
        with IntellexHost(bench, sd, ws_hosts=["127.0.0.2"]) as host:          # the stand-in alone (INTELLEX_WS_ALLOW)
            host.attach({"kind": "ws", "host": "127.0.0.2", "role": "navicore"}, timeout=20)
            st, body = host.json("GET", "/_api/status", timeout=5)
            if not (isinstance(body, dict) and body.get("attached")):
                raise AssertionError(f"the host did not attach to the stand-in endpoint: {st} {body}")
            time.sleep(1.0)
            stop = threading.Event()

            def watch():                          # when the verdict line arrives, to 50 ms
                while not stop.is_set():
                    if "verdict" not in seen and any("link is dead" in x for x in list(host.lines)):
                        seen["verdict"] = time.monotonic() - t0
                    time.sleep(0.05)
            t0 = time.monotonic()
            ep.go_silent()
            watcher = threading.Thread(target=watch, daemon=True)
            watcher.start()
            lost_at = None
            try:
                while time.monotonic() - t0 < DROP_WATCH_S:
                    sent = time.monotonic() - t0
                    try:
                        st, body = host.json("GET", "/_api/status", timeout=1.0)
                        att = bool(body.get("attached")) if st == 200 and isinstance(body, dict) else None
                    except OSError:
                        att = None
                    done = time.monotonic() - t0
                    samples.append((round(sent, 2), round(done, 2), att))
                    if att is False and lost_at is None:
                        lost_at = done
                    if lost_at is not None and done - lost_at >= 2.0:
                        break
                    time.sleep(0.25)
            finally:
                stop.set()
                watcher.join(timeout=1.0)
    finally:
        ep.close()
    problems, facts = drop_stall_problems(samples, seen.get("verdict"))
    facts["accepted"] = ep.accepted
    bench.note(f"intellex.link_drop_no_stall: {facts}")
    assert not problems, "; ".join(problems)


IDLE_TRICKLE_S = 2.0          # a byte from the far end every 2 s: never PROBE_IDLE_S (6 s) of silence
IDLE_HOLD_S = 40.0            # the row: still attached after 40 s of bytes over a port TCP cannot reach
IDLE_VERDICT_MIN_S = 8.0      # PROBE_IDLE_S, then three failed probes one loop pass (1 s) apart: ~8-9 s, never at once
IDLE_VERDICT_MAX_S = 14.0     # and well before the keepalive (5 s ping, 10 s to answer) could have dropped it instead


@test("intellex.reconnect_idle_gate", "A WebSocket link whose far end TCP cannot reach but which still sends a byte every "
      "2 s - an access point stalled by an OTA erase - stays attached for 40 s with no 'link is dead' verdict; once the "
      "bytes stop, the verdict comes 8 to 14 s after the last one and /_api/status shows the link until then; a "
      "stand-in endpoint on 127.0.0.2:80 (IX re-scan host.reconnect.idle_probe)")
def reconnect_idle_gate(bench):
    """Intellex src/host.py reconnect_loop (:1707-1754): on a ws link the loop TCP-probes the far end's port 80 only after
    PROBE_IDLE_S (6 s) without a byte (bridge.last_rx, set by every chunk the transport hands up), and drops the link
    after PROBE_FAILS_NEEDED (3) failures in a row. Traffic is its proof of life, because an ESP32 writing flash can
    stall both cores and fail a probe on a healthy board. intellex.link_drop_no_stall proves the verdict and that it
    does not stall the host; this proves the gate. The far end is hil/ws.py WsEndpoint on 127.0.0.2:80: after the attach
    its listener closes, so a probe would fail at once, while the open connection is still served (pings answered) and
    gets a one-byte TEXT frame every 2 s. Then the frames stop - the pings are still answered, so only the probe can
    end the link - and the verdict must wait for the idle gate. The host's own idle figure in its line is checked too."""
    import threading
    import time
    require(bench)
    try:
        ep = WsEndpoint("127.0.0.2", 80)
    except OSError as e:
        raise Skip(f"nothing can listen on 127.0.0.2:80 here ({e}): the stand-in needs port 80, the one Intellex's "
                   f"liveness probe uses (host.py reconnect_loop)")
    sd = stage(bench, "intellex.reconnect_idle_gate", tools="none")
    problems, facts = [], {}
    try:
        with IntellexHost(bench, sd, ws_hosts=["127.0.0.2"]) as host:          # the stand-in alone (INTELLEX_WS_ALLOW)
            host.attach({"kind": "ws", "host": "127.0.0.2", "role": "navicore"}, timeout=20)
            st, body = host.json("GET", "/_api/status", timeout=5)
            if not (isinstance(body, dict) and body.get("attached")):
                raise AssertionError(f"the host did not attach to the stand-in endpoint: {st} {body}")
            ep.close_listener()
            ep.trickle(IDLE_TRICKLE_S)
            t0 = time.monotonic()
            down, polls = [], 0
            while time.monotonic() - t0 < IDLE_HOLD_S:
                try:
                    st, body = host.json("GET", "/_api/status", timeout=2.0)
                    att = st == 200 and isinstance(body, dict) and body.get("attached") is True
                except OSError:
                    att = False
                polls += 1
                if not att:
                    down.append(round(time.monotonic() - t0, 1))
                time.sleep(0.5)
            dead = [x for x in host.lines if "link is dead" in x]
            facts.update(hold_polls=polls, trickled=ep.trickled, upgraded=ep.upgraded)
            if down:
                problems.append(f"with a byte every {IDLE_TRICKLE_S:g} s the link read as lost at {down[:5]} s: the idle "
                                f"gate let the probe drop a link that was still sending")
            if dead:
                problems.append(f"the host gave a 'link is dead' verdict while bytes still flowed: {dead[0]!r}")
            if ep.upgraded != 1:
                problems.append(f"the endpoint saw {ep.upgraded} WebSocket upgrades: the link was rebuilt meanwhile")
            if ep.trickled < IDLE_HOLD_S / IDLE_TRICKLE_S - 2:
                problems.append(f"the endpoint sent only {ep.trickled} frames in {IDLE_HOLD_S:g} s")
            seen = {}
            stop = threading.Event()
            last = ep.stop_trickle()

            def watch():                              # when the verdict line arrives, to 50 ms
                while not stop.is_set():
                    line = next((x for x in list(host.lines) if "link is dead" in x), None)
                    if line and "verdict" not in seen:
                        seen["verdict"], seen["line"] = time.monotonic() - last, line
                    time.sleep(0.05)
            watcher = threading.Thread(target=watch, daemon=True)
            watcher.start()
            early, lost = [], None
            try:
                while time.monotonic() - last < IDLE_VERDICT_MAX_S + 6:
                    now = time.monotonic() - last
                    try:
                        st, body = host.json("GET", "/_api/status", timeout=1.0)
                        att = bool(body.get("attached")) if st == 200 and isinstance(body, dict) else None
                    except OSError:
                        att = None
                    if att is False and lost is None:
                        lost = time.monotonic() - last
                    if att is not True and now < IDLE_VERDICT_MIN_S - 0.5:
                        early.append(round(now, 1))
                    if lost is not None and "verdict" in seen:
                        break
                    time.sleep(0.25)
            finally:
                stop.set()
                watcher.join(1.0)
            verdict = seen.get("verdict")
            m = re.search(r"unreachable after (\d+)s idle", seen.get("line", ""))
            facts.update(verdict_s=None if verdict is None else round(verdict, 1),
                         lost_s=None if lost is None else round(lost, 1), idle_reported=m and int(m.group(1)))
            if verdict is None:
                problems.append(f"no 'link is dead' verdict within {IDLE_VERDICT_MAX_S + 6:g} s of the last byte: the "
                                f"probe never fired on a far end that went quiet")
            elif not IDLE_VERDICT_MIN_S <= verdict <= IDLE_VERDICT_MAX_S:
                problems.append(f"the verdict came {verdict:.1f} s after the last byte, outside {IDLE_VERDICT_MIN_S:g}-"
                                f"{IDLE_VERDICT_MAX_S:g} s (PROBE_IDLE_S 6 s, then 3 failed probes 1 s apart)")
            if m and int(m.group(1)) < IDLE_VERDICT_MIN_S:
                problems.append(f"the host judged the link dead after {m.group(1)} s idle, under "
                                f"{IDLE_VERDICT_MIN_S:g} s: {seen['line']!r}")
            if early:
                problems.append(f"/_api/status stopped showing the link {early[:3]} s after the last byte, before the "
                                f"idle gate could have opened")
            if verdict is not None and lost is None:
                problems.append("the verdict came but /_api/status never showed the link lost")
    finally:
        ep.close()
    bench.note(f"intellex.reconnect_idle_gate: {facts}")
    assert not problems, "; ".join(problems)


# ------------------------------------------------------------------ IX-WP4: the pages, in a browser
@test("intellex.ui_tools_load", "Both tools load through Intellex with nothing attached: no page error, no failed "
      "request, the shim backs navigator.serial and says there is no transport (Playwright; IX-WP4)")
def ui_tools_load(bench):
    run_intellex_test(bench, "intellex.ui_tools_load")


def _bundle_versions(bench):
    """{tool: (shipped, worktree)}: Intellex's bundled Wizard UI_VERSION and config-tool footer-dtg against this repo's
    Wizard and the NaviCore checkout - a note only (DX3: a stale shipped bundle is reported, not failed)."""
    from hil.intellex import GITHUB, REPO, intellex_dir

    def grab(path, rx):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                m = re.search(rx, f.read(400_000))
            return m.group(1).strip() if m else "?"
        except OSError:
            return "absent"
    idir = intellex_dir(bench)
    ver = r"""\bUI_VERSION\s*=\s*['"]([^'"]+)['"]"""
    dtg = r'id="footer-dtg"[^>]*>([^<]+)<'
    return {"Wizard": (grab(os.path.join(idir, "src", "webui_wcb", "Wizard", "app.js"), ver),
                       grab(os.path.join(REPO, "Wizard", "app.js"), ver)),
            "config tool": (grab(os.path.join(idir, "src", "webui", "index.html"), dtg),
                            grab(os.path.join(GITHUB, "NaviCore", "config_tool", "index.html"), dtg))}


@test("intellex.ui_tools_load_shipped", "Both tools as Intellex ships them load with nothing attached: no page error, no "
      "failed request, the shim backs navigator.serial (Playwright on Intellex's own bundles; IX-WP4, DX3)")
def ui_tools_load_shipped(bench):
    """intellex.ui_tools_load's spec against `shipped` bundles: what a user of the current Intellex runs. How far they lag
    this repo's Wizard and the NaviCore checkout goes in session.log as a note, not a failure (INTELLEX.md DX3)."""
    for tool, (shipped, now) in _bundle_versions(bench).items():
        bench.note(f"intellex: shipped {tool} {shipped}; working tree {now}{'' if shipped == now else ' (differs)'}")
    run_intellex_test(bench, "intellex.ui_tools_load_shipped", tools="shipped")


@test("intellex.ui_tools_glue", "The shim's glue in both tools, with nothing attached: the Wizard's port sharing off, its "
      "flash on the host, its splash dismissed, its RC-tool link local, the branch key set only off main, the chip "
      "text; the config tool's flash buttons enabled with the native titles and its OTA label left as USB (IX-WP4)")
def ui_tools_glue(bench):
    """The rest of the plan's intellex.ui_tools_load row (INTELLEX.md §3.3): Intellex src/intellex_shim.js:336-366,
    :574-607, :650-666, :784-877, :1318-1395, :1502-1532."""
    run_intellex_test(bench, "intellex.ui_tools_glue")


def _fw_repo_constants(bench):
    """{"navicore": [owner, repo, path], "wcb": [...]} as Intellex's flashers and GitHub proxy know them (GITHUB_OWNER,
    GITHUB_REPO, GITHUB_BIN_PATH in src/flash.py and src/wcb_flash.py, read with ast): the pairs ghproxy.REPOS serves."""
    import ast
    from hil.intellex import intellex_dir
    out = {}
    for product, fname in (("navicore", "flash.py"), ("wcb", "wcb_flash.py")):
        with open(os.path.join(intellex_dir(bench), "src", fname), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        vals = {n.targets[0].id: ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                and n.targets[0].id in ("GITHUB_OWNER", "GITHUB_REPO", "GITHUB_BIN_PATH")}
        out[product] = [vals.get("GITHUB_OWNER"), vals.get("GITHUB_REPO"), vals.get("GITHUB_BIN_PATH")]
    return out


def _register_contract(tool, tools):
    tid = f"intellex.ui_contract_{tool}" + ("_shipped" if tools == "shipped" else "")
    what = "the Wizard" if tool == "wizard" else "the NaviCore config tool"
    src = "this repo's Wizard/ and NaviCore config_tool/" if tools == "worktree" else "Intellex's shipped bundles"

    @test(tid, f"Every name the shim reaches into {what} for is there, of the right kind, in {src}: functions, state, "
               f"DOM ids and the strings it matches (INTELLEX.md §1.5; Playwright; IX-WP4)")
    def run(bench):
        require(bench)
        if tools == "shipped":
            for t, (shipped, now) in _bundle_versions(bench).items():
                bench.note(f"intellex: shipped {t} {shipped}; working tree {now}")
        run_intellex_test(bench, tid, tools=tools, args={"repos": _fw_repo_constants(bench)})
    return run


for _tool in ("wizard", "navicore"):
    for _tools in ("worktree", "shipped"):
        _register_contract(_tool, _tools)


@test("intellex.ui_wizard_setup_images", "(should) Every ../Images file the Wizard references is bundled by Intellex, and "
      "the guided setup's hardware-version and Maestro steps show their pictures (plan finding 2; Playwright; IX-WP4)")
def ui_wizard_setup_images(bench):
    """INTELLEX.md finding 2: Wizard/app.js:10691 (LabelOnly.jpg, the identity step) and :10952 (PololuLogo.png, the
    Maestro step) are missing from Intellex's WCB_IMAGES (tools/fetch_webui.py:150), which this harness's stage reads
    too (hil/intellex.py seed_bundles), so the stage has the same gap as an Intellex install."""
    run_intellex_test(bench, "intellex.ui_wizard_setup_images")


@test("intellex.ui_latest_fw_version", "(should) After the host flashes a WCB for the Wizard, the Wizard's own "
      "latestFirmwareVersion names the build written, so boardGo labels the card with it (plan finding 1; Playwright; "
      "IX-WP4)")
def ui_latest_fw_version(bench):
    """INTELLEX.md finding 1: the shim assigns window.latestFirmwareVersion (Intellex src/intellex_shim.js:1390), but the
    Wizard's is a top-level let (Wizard/app.js:128) that a property on window cannot reach, as the shim's own comment says
    (:1176-1181); boardGo labels the card from the let (app.js:7586-7587). The flash routes are fulfilled in the page,
    so nothing is flashed."""
    run_intellex_test(bench, "intellex.ui_latest_fw_version")


@test("intellex.ui_launcher", "The chooser, with its routes answered from fixtures: chip labels and skips, 'asking...' "
      "upgraded in place by identify, droid rows and relayId in the attach body, the filter counts, auto-select once, "
      "theme and layout remembered, the maintenance lines, the branch picker, the log, Connect and 'Open the tool anyway' "
      "(Playwright; IX-WP4)")
def ui_launcher(bench):
    """Intellex src/launcher.html. Nothing reaches a port, a droid or GitHub: /_api/ports, identify, discover and attach
    are fulfilled in the page (tests/intellex/specs/ui_launcher.spec.js), and the host is leashed besides."""
    run_intellex_test(bench, "intellex.ui_launcher")


@test("intellex.ui_shell", "The two-tool window: tabs and ?view=, both panes preloaded, the split zoom arithmetic, a "
      "divider drag remembered, New window moving a tool out, and the chooser overlay opening and closing without "
      "reloading the tools (Playwright; IX-WP4)")
def ui_shell(bench):
    """Intellex src/shell.html:130-444."""
    run_intellex_test(bench, "intellex.ui_shell")


@test("intellex.ui_wiki", "The offline docs in a browser, on a crafted wiki: tabs, sidebar and filter, links and images "
      "rewritten, a missing image turned into a link, and no script from a page or a URL runs (Playwright; IX-WP4)")
def ui_wiki(bench):
    """Intellex src/wiki.html, src/wikidocs.py, host.py _WIKI_CSP; the wiki is tests/intellex/fixtures/wiki."""
    run_intellex_test(bench, "intellex.ui_wiki", wiki=True)


@test("intellex.shim_line_framing", "The shim's fake port puts ONE /_link message per line: CR, LF and CRLF (once) "
      "lines out of one chunk, a tail held until its terminator, a 1500-byte line written in 512-byte chunks that split "
      "an 'é' arriving whole as one frame, and an unterminated tail flushed by close() (Playwright, nothing attached; "
      "IX re-scan shim.line_framing)")
def shim_line_framing(bench):
    """Intellex src/intellex_shim.js:152-221, through navigator.serial in the NaviCore config tool on /; the frames are
    read off Playwright's websocket events (tests/intellex/specs/ui_shim.spec.js). bridge_terminators_w1 checks the
    host's side of the same contract without the shim."""
    run_intellex_test(bench, "intellex.shim_line_framing")


@test("intellex.shim_native_flash", "The config tool's flash buttons through the shim: a cancelled Full Wipe confirm "
      "sends nothing; an accepted one POSTs eraseNvs true, locks the flash, OTA and connect buttons while it runs, "
      "appends each log line once and ends 'failed: E'; Update Firmware asks nothing and ends 'complete ✓ - board is "
      "running <v>'; a 409 shows the host's words, a 404 or 405 the stale-host message (Playwright, /_api/flash "
      "fulfilled; IX re-scan shim.start_native_flash)")
def shim_native_flash(bench):
    """Intellex src/intellex_shim.js:670-738 startNativeFlash, reached through the shim's capture-phase click handler on
    the tool's own buttons (wireNativeFlash :574-607); /_api/flash and /_api/flash-status are fulfilled in the page, so
    nothing is flashed (tests/intellex/specs/ui_shim.spec.js). intellex.flash_navicore_app runs the Update path on the
    bench."""
    run_intellex_test(bench, "intellex.shim_native_flash")


# ------------------------------------------------------------------ the IX re-scan's medium-risk rows (2026-10-12)
def _own_addresses():
    """This computer's own IPv4 addresses other than loopback, link-local and the droids' 192.168.4.0/24 (where NaviCore's
    access point lives: a connect to the PC's own address there would not leave the PC, but the test has no need to go
    near that network) -> sorted list. From the routing table's pick for a TEST-NET address (a UDP connect sends
    nothing) and the interface list (ifconfig / ipconfig)."""
    found = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            found.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        if os.name == "nt":
            out = subprocess.run(["ipconfig"], capture_output=True, text=True, timeout=20).stdout
            found.update(re.findall(r"IPv4[^:\r\n]*:\s*(\d+\.\d+\.\d+\.\d+)", out))
        else:
            out = subprocess.run(["ifconfig", "-a"], capture_output=True, text=True, timeout=20).stdout
            found.update(re.findall(r"\binet (?:addr:)?(\d+\.\d+\.\d+\.\d+)", out))
    except (OSError, subprocess.SubprocessError):
        pass
    return sorted(a for a in found if not a.startswith(("127.", "0.", "169.254.", "192.168.4.")))


@test("intellex.loopback_only", "The host listens on 127.0.0.1 alone: a TCP connect to each of this computer's own "
      "non-loopback addresses on the host's port is refused, while 127.0.0.1 answers /_api/status (IX re-scan "
      "host.serve.loopback_only)")
def loopback_only(bench):
    """Intellex src/host.py BIND_HOST (:118-121) and web.run_app(host=BIND_HOST) (:2069): the control API and /_link are
    an unauthenticated command channel to the droid, so nothing off this computer may reach them. The droids' own
    192.168.4.0/24 is left out of the addresses tried (_own_addresses)."""
    require(bench)
    addrs = _own_addresses()
    if not addrs:
        raise Skip("this computer has no IPv4 address but loopback to try")
    sd = stage(bench, "intellex.loopback_only", tools="none")
    problems = []
    with IntellexHost(bench, sd) as host:
        st, body = host.json("GET", "/_api/status")
        if st != 200 or not isinstance(body, dict) or "attached" not in body:
            problems.append(f"127.0.0.1:{host.port} /_api/status: {st} {body}")
        for a in addrs:
            try:
                with socket.create_connection((a, host.port), timeout=3):
                    problems.append(f"{a}:{host.port} accepted a connection: the host listens beyond loopback")
            except ConnectionRefusedError:
                pass
            except OSError as e:
                problems.append(f"{a}:{host.port}: {type(e).__name__} {e}, not a refusal")
    bench.note(f"intellex.loopback_only: refused on {len(addrs)} non-loopback address(es)")
    assert not problems, "; ".join(problems)


@test("intellex.attach_ws_ssid", "A ws attach records the network the OS names for the host, the body's ssid only when "
      "the OS cannot say (macOS), stripped, and no ssid at all for a blank or non-string one (in-process, "
      "discover.ssid_for_host stood in for; IX re-scan host.api.attach_ws_ssid)")
def attach_ws_ssid(bench):
    """tests/intellex/py/host_units.py group ws_ssid: Intellex src/host.py api_attach (:1269-1285) on its real build_app()
    in the venv process, the transport a fake. /_api/status does not show the ssid; the bounce and the move check use
    it (reconnect_loop, _reidentify_if_moved), so the case reads the bridge's spec."""
    run_intellex_py(bench, "intellex.attach_ws_ssid", "host_units.py", args={"group": "ws_ssid"})


@test("intellex.identify_route", "POST /_api/identify: no port (or not a string) is 400; the port the host holds "
      "answers {attached:true} without opening it; any other port is probed - a direct PONG, a busy port, a silent "
      "one, one outside INTELLEX_SERIAL_ALLOW never opened (in-process, a fake pyserial; IX re-scan host.api.identify)")
def identify_route(bench):
    """tests/intellex/py/host_units.py group identify_route: Intellex src/host.py api_identify (:1309-1333) on its real
    build_app() in the venv process. The row's bench half - the PONG of NaviCore's own port through the route - is
    played by a fake pyserial Serial answering the PING as NaviCore does, so no COM port is opened."""
    run_intellex_py(bench, "intellex.identify_route", "host_units.py", args={"group": "identify_route"})


# A stand-in for Intellex's tools/fetch_webui.py, fetch_firmware.py and fetch_wiki.py in a stage: it records its argv,
# edits or writes the files tools/hil_stub.json names for it (paths relative to the stage), prints a line and exits
# with the code asked for. The host runs it exactly as it runs the real fetcher (host.py api_update_*).
_FETCH_STUB = r'''import json, os, re, sys


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    stage = os.path.dirname(here)
    name = os.path.splitext(os.path.basename(__file__))[0]
    with open(os.path.join(here, "hil_stub_calls.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"tool": name, "argv": sys.argv[1:]}) + "\n")
    with open(os.path.join(here, "hil_stub.json"), encoding="utf-8") as f:
        ctl = json.load(f).get(name, {})
    for rel, (pattern, repl) in ctl.get("edit", {}).items():
        p = os.path.join(stage, rel)
        with open(p, encoding="utf-8") as f:
            text = f.read()
        with open(p, "w", encoding="utf-8") as f:
            f.write(re.sub(pattern, repl, text, count=1))
    for rel, content in ctl.get("write", {}).items():
        p = os.path.join(stage, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
    print(ctl.get("print", "HIL stub " + name))
    return ctl.get("rc", 0)


if __name__ == "__main__":         # imported (src/wikidocs.py tries fetch_wiki for its TITLES), it does nothing
    sys.exit(main())
'''
_DTG_RX = r'(id="footer-dtg"[^>]*>)[^<]+'
_UIVER_RX = r"""(\bUI_VERSION\s*=\s*['"])[^'"]+"""


@test("intellex.update_routes", "POST /_api/update-webui, -firmware and -wiki run their fetcher and report it: a bad "
      "?tool= is 400 and runs nothing; ?tool= reaches the fetcher (all by default); success carries the new versions and "
      "the fetcher's log; a failed update answers 200 ok false on all three, with the error, the log and what landed "
      "or is cached; a frozen build re-invokes itself (stub fetchers; IX re-scan host.api.update_subprocesses)")
def update_routes(bench):
    """Intellex src/host.py api_update_webui (:912-965), api_update_firmware (:636-669), api_update_wiki (:828-854). The
    stage's tools/fetch_webui.py, fetch_firmware.py and fetch_wiki.py are replaced by _FETCH_STUB, which edits or writes
    stage files as a real fetch would (the bundled Wizard's UI_VERSION, the config tool's footer-dtg, a cached image, a
    wiki page) - so nothing reaches GitHub. The frozen re-invocation (--run-fetch-*) and app.py's handling of it are
    tests/intellex/py/host_units.py group update_frozen, run on the same stage. A failed update answers 200 with ok
    false on all three routes: the tool update answered 502 alone until Intellex made it match the other two
    (2026-10-13; the launcher's api() reads only the body, so 200 {ok:false} is the form it handles for all three)."""
    require(bench)
    tid = "intellex.update_routes"
    sd = stage(bench, tid)
    tools = os.path.join(sd, "tools")
    for name in ("fetch_webui", "fetch_firmware", "fetch_wiki"):
        with open(os.path.join(tools, name + ".py"), "w", encoding="utf-8") as f:
            f.write(_FETCH_STUB)
    calls_file = os.path.join(tools, "hil_stub_calls.jsonl")

    def control(**ctl):
        with open(os.path.join(tools, "hil_stub.json"), "w", encoding="utf-8") as f:
            json.dump(ctl, f)
        if os.path.exists(calls_file):
            os.remove(calls_file)

    def calls():
        try:
            with open(calls_file, encoding="utf-8") as f:
                return [json.loads(x) for x in f if x.strip()]
        except OSError:
            return []
    index = os.path.join("src", "webui", "index.html")
    app_js = os.path.join("src", "webui_wcb", "Wizard", "app.js")
    with open(os.path.join(sd, index), encoding="utf-8", errors="replace") as f:
        m = re.search(r'id="footer-dtg"[^>]*>([^<]+)<', f.read())
    dtg0 = m.group(1).strip() if m else ""
    problems = []
    with IntellexHost(bench, sd) as host:
        control()
        st, body = host.json("POST", "/_api/update-webui?tool=bogus", {}, timeout=60)
        if st != 400 or body != {"ok": False, "error": "tool must be navicore|wcb|all"} or calls():
            problems.append(f"update-webui?tool=bogus: {st} {body}, fetcher ran {calls()}")

        control(fetch_webui={"rc": 0, "print": "HIL stub: Wizard updated",
                             "edit": {app_js: [_UIVER_RX, r"\g<1>HILWIZ-1"]}})
        st, body = host.json("POST", "/_api/update-webui?tool=wcb", {}, timeout=120)
        if st != 200 or body.get("ok") is not True or body.get("wcbVersion") != "HILWIZ-1" \
                or body.get("version") != dtg0 or "HIL stub: Wizard updated" not in body.get("log", ""):
            problems.append(f"update-webui?tool=wcb succeeding: {st} {body}, expected ok, wcbVersion HILWIZ-1, version "
                            f"{dtg0!r} and the fetcher's line in the log")
        if [c["argv"] for c in calls()] != [["--tool", "wcb"]]:
            problems.append(f"update-webui?tool=wcb ran the fetcher with {calls()}")

        control(fetch_webui={"rc": 0})
        host.json("POST", "/_api/update-webui", {}, timeout=120)
        if [c["argv"] for c in calls()] != [["--tool", "all"]]:
            problems.append(f"update-webui with no ?tool= ran the fetcher with {calls()}, expected --tool all")

        control(fetch_webui={"rc": 1, "print": "HIL stub: the Wizard fetch failed (rate limited)",
                             "edit": {index: [_DTG_RX, r"\g<1>HILNC-2"]}})
        st, body = host.json("POST", "/_api/update-webui?tool=all", {}, timeout=120)
        if st != 200 or body.get("ok") is not False or "the Wizard fetch failed" not in body.get("error", "") \
                or "the Wizard fetch failed" not in body.get("log", "") \
                or body.get("version") != "HILNC-2" or body.get("wcbVersion") != "HILWIZ-1":
            problems.append(f"update-webui failing after the config tool landed: {st} {body}, expected 200 ok false "
                            f"with the fetcher's words in error and log, version HILNC-2 (what landed) and wcbVersion "
                            f"HILWIZ-1")
        st, ver = host.json("GET", "/_api/webui-version", timeout=30)
        if (ver or {}).get("bundled") != "HILNC-2" or ((ver or {}).get("wcb") or {}).get("bundled") != "HILWIZ-1":
            problems.append(f"/_api/webui-version after the updates: bundled {(ver or {}).get('bundled')!r}, Wizard "
                            f"{((ver or {}).get('wcb') or {}).get('bundled')!r}")

        img = "NaviCore_9.9.9_010000RJAN2026_hilstub_ESP32S3.bin"
        control(fetch_firmware={"rc": 0, "print": "HIL stub: NaviCore cached",
                                "write": {os.path.join("src", "firmware", "navicore", "main", img): "x" * 64}})
        st, body = host.json("POST", "/_api/update-firmware", {}, timeout=120)
        nc = ((body or {}).get("cached") or {}).get("navicore") or {}
        if st != 200 or body.get("ok") is not True or nc.get("count") != 1 or nc.get("newest") != img \
                or "HIL stub: NaviCore cached" not in body.get("log", ""):
            problems.append(f"update-firmware succeeding: {st} {body}")
        if [c["argv"] for c in calls()] != [[]]:
            problems.append(f"update-firmware ran the fetcher with {calls()}")
        control(fetch_firmware={"rc": 1, "print": "WCB ESP32  FAILED: HIL stub rate limit"})
        st, body = host.json("POST", "/_api/update-firmware", {}, timeout=120)
        nc = ((body or {}).get("cached") or {}).get("navicore") or {}
        if st != 200 or body.get("ok") is not False or "HIL stub rate limit" not in body.get("error", "") \
                or "HIL stub rate limit" not in body.get("log", "") or nc.get("count") != 1:
            problems.append(f"update-firmware failing for WCB: {st} {body}, expected 200 ok false with the fetcher's "
                            f"words in error and log and the NaviCore set still reported cached")

        control(fetch_wiki={"rc": 0, "print": "[wcb] 1 page(s), 0 asset(s)",
                            "write": {os.path.join("src", "wiki", "wcb", "Home.md"): "# HIL stub page\n"}})
        st, body = host.json("POST", "/_api/update-wiki", {}, timeout=120)
        have = {w.get("product"): w.get("have") for w in (body or {}).get("wikis") or []}
        if st != 200 or body.get("ok") is not True or have.get("wcb") is not True or "[wcb] 1 page(s)" not in \
                body.get("log", ""):
            problems.append(f"update-wiki succeeding: {st} {body}")
        if [c["argv"] for c in calls()] != [["--wiki", "all"]]:
            problems.append(f"update-wiki ran the fetcher with {calls()}")
        control(fetch_wiki={"rc": 1, "print": "github.com is not reachable - the bundled docs are unchanged."})
        st, body = host.json("POST", "/_api/update-wiki", {}, timeout=120)
        if st != 200 or body.get("ok") is not False or "not reachable" not in body.get("error", "") \
                or "not reachable" not in body.get("log", ""):
            problems.append(f"update-wiki failing: {st} {body}, expected 200 ok false with the fetcher's words in "
                            f"error and log")
    try:
        run_intellex_py(bench, tid, "host_units.py", args={"group": "update_frozen"}, stage_dir=sd)
    except AssertionError as e:
        problems.append(f"frozen (host_units.py): {e}")
    assert not problems, "; ".join(problems)


@test("intellex.gh_proxy_online", "Online, the GitHub proxy's routes answer from GitHub (X-Intellex-Source github) with "
      "download_url pointed back at the host, store the listing as GitHub sent it and each image on the way past, "
      "answer 502 to a reply that is not a listing without replacing the stored one, and serve the stored copy offline "
      "(in-process, GitHub stood in for; IX re-scan host.api.gh_proxy_online)")
def gh_proxy_online(bench):
    """tests/intellex/py/ghproxy_units.py group online: Intellex src/ghproxy.py contents/raw (:88-147) through host.py
    api_gh_contents/api_gh_raw on its real build_app() in the venv process, flash.reachable and flash._get stood in for
    so nothing reaches GitHub; the cache is the stage's src/firmware."""
    run_intellex_py(bench, "intellex.gh_proxy_online", "ghproxy_units.py", args={"group": "online"})


@test("intellex.bridge_attach_detach_window", "A detach that lands while an attach is between publishing its transport "
      "and recording its target is not undone: the bridge stays detached and unwanted, what was published is closed, "
      "and the reconnect loop does not reattach (in-process fakes; IX re-scan host.bridge.attach_detach_window)")
def bridge_attach_detach_window(bench):
    """tests/intellex/py/bridge_units.py group window: Intellex src/host.py Bridge.attach (:214-251). Found 2026-10-12
    while building intellex.bridge_superseded_attach: attach() published the transport under the lock, let it go, and
    took it again to record the target and set _want - so a detach landing between the two cleared the target, then
    had it written back, and the reconnect loop reattached the link the user stopped. The bridge's lock is wrapped to
    park the attaching thread at exactly that point; once one locked section does both, there is no such point and the
    detach lands after the attach. Fixed on Intellex main."""
    run_intellex_py(bench, "intellex.bridge_attach_detach_window", "bridge_units.py", args={"group": "window"})


@test("intellex.reconnect_auto_bounce", "The reconnect loop's self-heal: a ws link whose reattach fails twice while the "
      "OS routes to the host off its subnet re-associates the adapter, asking the OS for the network first, once per "
      "cooldown, and gives up after 3 failed bounces with one line; a move or a working bounce resets that; a new "
      "attach and a detach zero both the failure count and the give-up count; on-subnet routing, a serial link and "
      "--no-auto-bounce never bounce (in-process, discover stood in for; IX re-scan host.reconnect.auto_bounce)")
def reconnect_auto_bounce(bench):
    """tests/intellex/py/host_units.py group bounce: Intellex src/host.py reconnect_loop running in its real build_app()
    in the venv process, every reattach failing, with discover.local_ip_for, ssid_for_host, wifi_bounce and probe
    answered by the case - no adapter is touched and the host (192.0.2.1, TEST-NET-1) is never contacted; it is the one
    ws host the leash allows (INTELLEX_WS_ALLOW), so the loop's move check may ask the stood-in probe about it. Every
    staged-host test runs with --no-auto-bounce; this is the path that flag turns off. BOUNCE_COOLDOWN_S is shortened
    to 2.5 s for the run. The two counts are Bridge.retry_fails and Bridge.bounce_fails since Intellex 2026-10-13: as
    reconnect_loop locals they outlived a detach and the next attach (a product call from the re-scan, now changed)."""
    run_intellex_py(bench, "intellex.reconnect_auto_bounce", "host_units.py", args={"group": "bounce"}, timeout=360,
                    ws_hosts=["192.0.2.1"])


@test("intellex.host_cli", "host.py's command line: --serial with --ws exits 2 before serving; a --serial target that "
      "cannot open is recorded first and retried ('will keep retrying'); a --ws target is attached before the first "
      "/_api/status answers; --ssid reaches the spec, --no-auto-bounce clears the self-heal, and the startup lines say "
      "OUT OF DATE when Pages is newer (a stand-in endpoint on 127.0.0.2:80; IX re-scan host.main.cli)")
def host_cli(bench):
    """Intellex src/host.py main() (:2015-2074). As processes, on a staged host: the exit 2, a --serial port outside
    INTELLEX_SERIAL_ALLOW (refused before it is opened - no port is touched) kept as the target and retried by the
    reconnect loop, and --ws 127.0.0.2 against hil/ws.py WsEndpoint (skipped with a note where nothing can listen on
    127.0.0.2:80). In-process (tests/intellex/py/host_units.py group cli): --ssid and its NaviCore default in the
    spec, --no-auto-bounce, a --serial target identified before serving, and the startup lines, Pages stood in for.
    The row's W1 half (a real port at startup) is left to that fake: these tests stay off the boards."""
    import time
    require(bench)
    tid = "intellex.host_cli"
    sd = stage(bench, tid, tools="none")
    problems = []
    rc, out = run_venv(bench, [os.path.join(sd, "src", "host.py"), "--port", "1", "--serial", "COMHILCLI", "--ws",
                               "127.0.0.2"], cwd=sd, timeout=60,
                       env={"INTELLEX_OFFLINE": "1", "INTELLEX_SERIAL_ALLOW": "", "INTELLEX_DISCOVER_HOSTS": "",
                            "INTELLEX_DATA_DIR": os.path.join(sd, "appdata", "Intellex")})
    if rc != 2 or "mutually exclusive" not in out or "serving" in out:
        problems.append(f"--serial with --ws: exited {rc}, expected 2 with 'mutually exclusive' and nothing served")

    with IntellexHost(bench, sd, args=["--serial", "COMHILCLI"]) as host:
        st, body = host.json("GET", "/_api/status")
        if not (body.get("wantsLink") and not body.get("attached") and body.get("kind") == "serial"):
            problems.append(f"--serial COMHILCLI (refused): the first /_api/status {body}, expected the target kept, "
                            f"unattached")
        text = "\n".join(host.lines)
        if "attach failed" not in text or "will keep retrying every second" not in text:
            problems.append("--serial COMHILCLI (refused): no 'attach failed' / 'will keep retrying' lines at startup")
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and not str(body.get("lastError", "")).startswith("reconnect failed"):
            time.sleep(0.25)
            st, body = host.json("GET", "/_api/status")
        if not str(body.get("lastError", "")).startswith("reconnect failed"):
            problems.append(f"--serial COMHILCLI: the reconnect loop never retried it (lastError "
                            f"{body.get('lastError')!r})")

    try:
        ep = WsEndpoint("127.0.0.2", 80)
    except OSError as e:
        ep = None
        bench.note(f"{tid}: --ws at startup not checked: nothing can listen on 127.0.0.2:80 here ({e})")
    if ep is not None:
        try:
            with IntellexHost(bench, sd, args=["--ws", "127.0.0.2", "--ssid", "HILCLI"], ws_hosts=["127.0.0.2"]) as host:
                st, body = host.json("GET", "/_api/status")
                lines = list(host.lines)
                if not (body.get("attached") and body.get("kind") == "ws" and body.get("target") == "ws://127.0.0.2/ws"):
                    problems.append(f"--ws 127.0.0.2: the first /_api/status {body}, expected attached to "
                                    f"ws://127.0.0.2/ws before serving")
                at = next((i for i, x in enumerate(lines) if x.startswith("attached  ws://127.0.0.2/ws")), None)
                serving = next((i for i, x in enumerate(lines) if x.startswith("serving")), None)
                if at is None or serving is None or at > serving:
                    problems.append(f"--ws 127.0.0.2: the 'attached' line (line {at}) does not come before 'serving' "
                                    f"(line {serving})")
        finally:
            ep.close()
    try:
        run_intellex_py(bench, tid, "host_units.py", args={"group": "cli"}, stage_dir=sd)
    except AssertionError as e:
        problems.append(f"in-process (host_units.py cli): {e}")
    assert not problems, "; ".join(problems)


@test("intellex.wifi_bounce_macos_unit", "Intellex's macOS bounce, every command answered by a script: the routing "
      "table names the carrying interface (never the default route, and only one holding an address on the droid's "
      "/24), a built-in Wi-Fi port is power-cycled off then on, an adapter macOS does not count as Wi-Fi is declined "
      "with nothing touched, and the SSID scan matches by name (IX re-scan discover.wifi_bounce_macos)")
def wifi_bounce_macos_unit(bench):
    """tests/intellex/py/discover_units.py group bounce_macos: Intellex src/discover.py _wifi_bounce_macos (:641-771) with
    proc.run patched - no route, ifconfig or networksetup runs, no adapter is touched. intellex.wifi_bounce_scoped
    is the attended bench half (Windows)."""
    run_intellex_py(bench, "intellex.wifi_bounce_macos_unit", "discover_units.py", args={"group": "bounce_macos"})


@test("intellex.reachable_unit", "flash.reachable: every address DNS returns gets its own attempt (a dead IPv6 does not "
      "spend the budget), the budget outlasts a slow lookup and bounds a hung one, answers are kept per host for 20 s, "
      "and INTELLEX_OFFLINE answers False at once (socket stood in for; IX re-scan flash.nc.reachable)")
def reachable_unit(bench):
    """tests/intellex/py/flash_units.py group reachable: Intellex src/flash.py reachable (:50-156) with socket.getaddrinfo
    and socket.socket replaced in the venv process - nothing is resolved or connected (TEST-NET and documentation
    addresses only)."""
    run_intellex_py(bench, "intellex.reachable_unit", "flash_units.py", args={"group": "reachable"})


@test("intellex.nc_fetch_store_unit", "NaviCore's fetch_images online keeps what it downloads: the listing as GitHub sent "
      "it and each image under firmware/navicore/<branch> ('/' flattened) with the manifest, never another product's "
      "image or an empty one, and gives the same set back offline (GitHub stood in for; IX re-scan "
      "flash.nc.fetch_images)")
def nc_fetch_store_unit(bench):
    """tests/intellex/py/flash_units.py group nc_fetch_store: Intellex src/flash.py list_firmware/fetch_images
    (:266-371) with flash.reachable and flash._get answered in the venv process; the cache is the stage's src/firmware.
    intellex.flash_rules_unit covers the offline pairing rules."""
    run_intellex_py(bench, "intellex.nc_fetch_store_unit", "flash_units.py", args={"group": "nc_fetch_store"})


# settings.json as a hand edit, a crash or another version could leave it -> (label, bytes, or None for a directory).
_BAD_SETTINGS = (("not JSON", b"not json"), ("binary garbage", b"\xff\xfe\x00{"),
                 ("invalid stored names", b'{"branch": {"wcb": "a b", "navicore": "../x"}}'),
                 ("a JSON list", b"[]"), ("a JSON string", b'"main"'), ("JSON null", b"null"),
                 ("a branch that is not a map", b'{"branch": "wcb"}'), ("a branch list", b'{"branch": ["main"]}'),
                 ("names that are not strings", b'{"branch": {"wcb": 5, "navicore": null}}'),
                 ("a directory where the file goes", None))


@test("intellex.settings_branch_fallback", "A corrupt, unreadable or wrongly shaped settings.json never stops the host: "
      "it starts, /_api/branches says main for both products, the shim and /_api/firmware are served, and POST "
      "/_api/branch replaces the bad file with a good one (IX re-scan settings.branch_fallback)")
def settings_branch_fallback(bench):
    """Intellex src/settings.py branch() and branches() (:46-54, :82-90): 'falls back to main on anything unset or
    malformed rather than raising: a corrupt settings file must not make the app unable to flash'. branch() runs for
    every page (the shim prelude, host.py shim_js), /_api/branches, /_api/firmware and both flashers. One host per
    shape of bad file, each in its own stage's appdata. A directory in the file's place cannot be written either, so
    Set answers 400 there ('could not write the settings file')."""
    require(bench)
    problems = []
    for label, data in _BAD_SETTINGS + (("control: wcb on WIFI", b'{"branch": {"wcb": "WIFI"}}'),):
        sd = stage(bench, "intellex.settings_branch_fallback", tools="none")
        f = os.path.join(sd, "appdata", "Intellex", "settings.json")
        if data is None:
            os.makedirs(f)
        else:
            with open(f, "wb") as fh:
                fh.write(data)
        want = {"navicore": "main", "wcb": "WIFI" if label.startswith("control") else "main"}
        try:
            host = IntellexHost(bench, sd).start()
        except AssertionError as e:
            problems.append(f"{label}: the host did not start ({str(e).splitlines()[0]})")
            continue
        try:
            st, br = host.json("GET", "/_api/branches")
            if st != 200 or br != want:
                problems.append(f"{label}: /_api/branches {st} {br}, expected {want}")
            st, _, js = host.http("GET", "/_intellex.js")
            if st != 200 or not js.startswith(("window.__intellexBranches = " + json.dumps(
                    want, separators=(",", ":"))).encode()):
                problems.append(f"{label}: /_intellex.js {st} {js[:60]!r} - every page loses its shim")
            st, fw = host.json("GET", "/_api/firmware")
            if st != 200 or (fw.get("wcb") or {}).get("branch") != want["wcb"]:
                problems.append(f"{label}: /_api/firmware {st} {str(fw)[:80]}")
            st, body = host.json("POST", "/_api/branch", {"product": "wcb", "branch": "hil-fixed"})
            st2, br = host.json("GET", "/_api/branches")
            if data is None:
                if st != 400 or "could not write" not in str(body.get("error")) or br != want:
                    problems.append(f"{label}: Set answered {st} {body}, branches {br}; expected 400 'could not write "
                                    f"the settings file' and nothing changed")
            elif st != 200 or br != {"navicore": "main", "wcb": "hil-fixed"}:
                problems.append(f"{label}: Set wcb=hil-fixed answered {st} {body}, then /_api/branches {br} - the bad "
                                f"file was not replaced")
        finally:
            host.stop()
    assert not problems, "; ".join(problems)


@test("intellex.fwcache_branch_slash", "The firmware cache for a branch with a slash: feature/x, cached under its "
      "percent-encoded name feature%2Fx, is reported by /_api/firmware and served by the GitHub proxy offline, while "
      "feature/y and the branch feature__x are not (they shared one directory while '/' folded to '__'); a frozen "
      "build reads each set from the user directory first, per set, and writes there (IX re-scan "
      "fwcache.key_and_lookup)")
def fwcache_branch_slash(bench):
    """Intellex src/fwcache.py _key, load/load_listing and _dirs. On a staged host whose settings.json names feature/x
    for both products, with a cache seeded for feature/x (seed_firmware encodes it as fwcache._key does). The key was
    '/' folded to '__' until Intellex 2026-10-13, so feature/x and a branch named feature__x shared a directory (the
    re-scan's product call); it is percent-encoded now, and a set cached under the old name is fetched again. The frozen
    two-location precedence and the old-name miss are tests/intellex/py/fwcache_units.py group frozen_lookup, on the
    same stage."""
    require(bench)
    tid = "intellex.fwcache_branch_slash"
    branch = "feature/x"
    sd = stage(bench, tid, tools="none", settings={"branch": {"wcb": branch, "navicore": branch}})
    files = _fw_files()
    for product, fs in files.items():
        d = seed_firmware(sd, product, branch, fs)
        if os.path.basename(d) != "feature%2Fx":
            raise AssertionError(f"seed_firmware filed {branch} under {os.path.basename(d)}, not feature%2Fx")
    problems = []
    with IntellexHost(bench, sd) as host:
        st, summary = host.json("GET", "/_api/firmware")
        for product, fs in files.items():
            s = (summary or {}).get(product) or {}
            if s.get("branch") != branch or s.get("count") != len(fs):
                problems.append(f"/_api/firmware {product}: {s}, expected branch {branch} with {len(fs)} files")
        st, br = host.json("GET", "/_api/branches")
        if br != {"navicore": branch, "wcb": branch}:
            problems.append(f"/_api/branches: {br}")
        for product, (owner, repo, path) in FW_REPOS.items():
            q = urllib.parse.urlencode({"owner": owner, "repo": repo, "path": path, "ref": branch})
            st, hdr, body = host.http("GET", f"/_api/gh/contents?{q}", timeout=30)
            if st != 200 or _hdr(hdr, "x-intellex-source") != "cache":
                problems.append(f"{product} listing for {branch}: {st}, X-Intellex-Source "
                                f"{_hdr(hdr, 'x-intellex-source')!r}")
                continue
            for f in json.loads(body.decode("utf-8")):
                st, hdr, data = host.http("GET", f["download_url"], timeout=30)
                if st != 200 or data != files[product].get(f["name"]):
                    problems.append(f"{product} {f['name']} for {branch}: {st}, {len(data)} bytes")
            for other in ("feature/y", "feature__x"):
                q = urllib.parse.urlencode({"owner": owner, "repo": repo, "path": path, "ref": other})
                st, hdr, _ = host.http("GET", f"/_api/gh/contents?{q}", timeout=30)
                if st != 502:
                    problems.append(f"{product} listing for the uncached {other}: {st} (X-Intellex-Source "
                                    f"{_hdr(hdr, 'x-intellex-source')!r}), not 502" +
                                    (" - feature/x's set, served under the folded name it shares" if
                                     other == "feature__x" else ""))
    try:
        run_intellex_py(bench, tid, "fwcache_units.py", args={"group": "frozen_lookup"}, stage_dir=sd)
    except AssertionError as e:
        problems.append(f"frozen (fwcache_units.py): {e}")
    assert not problems, "; ".join(problems)


@test("intellex.fwcache_atomic_unit", "fwcache.store writes through a .part file and renames it, records the image in "
      "the manifest with the time it arrived, never stores an empty payload, and a failing rename leaves nothing under "
      "the build's name and raises nothing (IX re-scan fwcache.atomic_store)")
def fwcache_atomic_unit(bench):
    """tests/intellex/py/fwcache_units.py group atomic: Intellex src/fwcache.py store/store_listing (:108-140,
    :183-193) in a scratch directory inside the stage, pathlib.Path.replace made to fail for the last part."""
    run_intellex_py(bench, "intellex.fwcache_atomic_unit", "fwcache_units.py", args={"group": "atomic"})


@test("intellex.ui_launcher_connect_error", "The chooser's Connect when the host refuses the attach: the page stays on "
      "/_launcher with the host's words in #status in the error style ('could not connect' when it gives none), nothing "
      "opens in either layout, and the next Connect still works (Playwright, attach fulfilled; IX re-scan "
      "launcher.connect)")
def ui_launcher_connect_error(bench):
    """Intellex src/launcher.html connect() (:647-657), openTools() (:628-645); tests/intellex/specs/ui_launcher.spec.js.
    The attach is fulfilled in the page, so nothing is attached."""
    run_intellex_test(bench, "intellex.ui_launcher_connect_error")


@test("intellex.ui_launcher_tools_stale", "A stale bundled tool shows in the chooser's folded summary ('config tool + "
      "Wizard update available', each alone when only one is) and in the attention count, the panel line says 'update "
      "available (<published>)', and the summary's Update runs the panel's Update tools for both tools and clears the "
      "line, after which the summary offers no Update (Playwright, webui-version and update-webui fulfilled; IX re-scan "
      "launcher.maintenance_summary)")
def ui_launcher_tools_stale(bench):
    """Intellex src/launcher.html paintMaint (:343-394), checkUi (:868-901), Update tools (:1176-1191);
    tests/intellex/specs/ui_launcher.spec.js. Nothing reaches GitHub: both routes are fulfilled in the page."""
    run_intellex_test(bench, "intellex.ui_launcher_tools_stale")


@test("intellex.shim_stale_host_buttons", "On a host too old to answer /_api/flash-status (404) the config tool's "
      "Update Firmware and Full Wipe stay disabled with the stale-host message through the shim's 3 s re-asserts, are "
      "wired with their native titles once the host answers, and stay so after one later miss (Playwright; IX re-scan "
      "shim.native_flash_buttons)")
def shim_stale_host_buttons(bench):
    """Intellex src/intellex_shim.js hostSupportsFlash / wireNativeFlash (:514-607); tests/intellex/specs/ui_shim.spec.js.
    /_api/flash-status is fulfilled in the page; nothing is flashed."""
    run_intellex_test(bench, "intellex.shim_stale_host_buttons")


@test("intellex.shim_wizard_no_share", "The Wizard's establishConnection is wrapped by DOMContentLoaded, once, and the "
      "original always receives allowShare false as its 4th argument (the others and its result passed through) "
      "(Playwright; IX re-scan shim.wizard_port_sharing_off)")
def shim_wizard_no_share(bench):
    """Intellex src/intellex_shim.js disableWcbPortSharing (:784-794) and its DOMContentLoaded hook (:1625-1641); a
    recorder takes the place of the Wizard's establishConnection before the shim wraps it
    (tests/intellex/specs/ui_shim.spec.js)."""
    run_intellex_test(bench, "intellex.shim_wizard_no_share")


@test("intellex.shim_wizard_flash_refusals", "The Wizard's flashFirmware through the shim: a stale host and a WiFi link "
      "are refused before anything is POSTed or reported; a host failure is THROWN with its error after feeding the log, "
      "status and progress once each; a 409 gives the host's words, a 404/405 the stale-host message; boardGo turns a "
      "failure into 'Flash failed' and pushes nothing (Playwright, flash routes fulfilled; IX re-scan "
      "shim.wizard_flash_replacement)")
def shim_wizard_flash_refusals(bench):
    """Intellex src/intellex_shim.js installWcbFlash (:1327-1417) and the Wizard's boardGo (Wizard/app.js), with a fake
    board connection in slot 1 and /_api/flash-wcb, /_api/flash-status and /_api/status fulfilled in the page
    (tests/intellex/specs/ui_shim.spec.js). Pinned as it is today: boardGo closes the board's connection before it
    calls flashFirmware, so the shim's WiFi refusal - meant to come 'BEFORE boardGo() tears the board's connection
    down' (its comment) - comes after that teardown; boardGo's failure path then reconnects."""
    run_intellex_test(bench, "intellex.shim_wizard_flash_refusals")


@test("intellex.shim_close_intent", "The shim tells a Disconnect from a teardown: the user's Disconnect on a healthy link "
      "is honoured (nothing reconnects in 8 s), while a close within 60 s of a loss - the tool's own failed reconnect - "
      "is teardown, and the shim gets the page back (Playwright, /_link routed to a stand-in NaviCore; IX re-scan "
      "shim.close_intent)")
def shim_close_intent(bench):
    """Intellex src/intellex_shim.js IntellexPort.close (:224-246), LOSS_GRACE_MS (:48-54) and watchLink (:1595-1605),
    with the NaviCore config tool's own Disconnect, handleLinkLost and tryAutoReconnect. The page's /_link is answered
    by Playwright (page.routeWebSocket): a stand-in that answers ?REC and PING and can drop; /_api/status says a serial
    link is attached. The row's bench half (NaviCore's port) is played by that stand-in, so no board is touched
    (tests/intellex/specs/ui_shim.spec.js)."""
    run_intellex_test(bench, "intellex.shim_close_intent")


@test("intellex.app_browser_mode", "The desktop app (app.py) as a process: --browser opens the default browser on the "
      "chooser ('/' with a --ws target) and serves only once its own /_api/status answers; a second app.py on the same "
      "port exits 1 at once with 'already running' on stderr and opens nothing; in-process, a failed start releases its "
      "early attach and the window, storage and browser fallbacks (IX re-scan app.window_mode)")
def app_browser_mode(bench):
    """Intellex src/app.py _bind (:47-71), main (:141-387). BROWSER names a stand-in that records the URL it was asked to
    open, so no browser starts; INTELLEX_DATA_DIR keeps everything in the stage. The second app.py binds the same port
    with no SO_REUSEADDR and must refuse before attaching anything. The --ws half runs against hil/ws.py WsEndpoint on
    127.0.0.2:80 (skipped with a note where nothing can listen there). The window itself is attended; its pywebview
    calls, the failed-start release and the fallbacks are tests/intellex/py/host_units.py group app."""
    import time
    require(bench)
    tid = "intellex.app_browser_mode"
    sd = stage(bench, tid, tools="none")
    opened = os.path.join(sd, "browser_opened.txt")
    if os.name == "nt":
        browser = os.path.join(sd, "hil_browser.bat")
        with open(browser, "w", encoding="utf-8") as f:
            f.write(f'@echo %~1>> "{opened}"\r\n')
    else:
        browser = os.path.join(sd, "hil_browser.sh")
        with open(browser, "w", encoding="utf-8") as f:
            f.write(f'#!/bin/sh\nprintf "%s\\n" "$1" >> "{opened}"\n')
        os.chmod(browser, 0o755)

    def urls():
        try:
            with open(opened, encoding="utf-8") as f:
                return [x.strip() for x in f if x.strip()]
        except OSError:
            return []

    def wait_urls(n, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and len(urls()) < n:
            time.sleep(0.2)
        return urls()
    problems = []
    env = {"BROWSER": browser, "PYTHONUNBUFFERED": "1"}     # app.py, unlike host.py, leaves a piped stdout buffered
    with IntellexHost(bench, sd, script="app.py", args=["--browser"], env=env) as app:
        got = wait_urls(1)
        if got != [f"{app.url}/_launcher"]:
            problems.append(f"--browser opened {got}, expected [{app.url}/_launcher]")
        if not any(x.startswith(f"serving {app.url}/_launcher") for x in app.lines):
            problems.append("no 'serving <url>' line from app.py --browser")
        st, body = app.json("GET", "/_api/status")
        if st != 200 or body.get("attached") is not False:
            problems.append(f"app.py's own /_api/status: {st} {body}")
        py = require(bench)[1]
        e = {k: v for k, v in os.environ.items() if not k.startswith("INTELLEX_")}
        e.update(PYTHONUTF8="1", BROWSER=browser, INTELLEX_OFFLINE="1", INTELLEX_SERIAL_ALLOW="",
                 INTELLEX_DISCOVER_HOSTS="", INTELLEX_DATA_DIR=os.path.join(sd, "appdata", "Intellex"))
        t0 = time.monotonic()
        try:
            p = subprocess.run([py, os.path.join(sd, "src", "app.py"), "--port", str(app.port), "--browser",
                                "--no-auto-bounce"], cwd=sd, env=e, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=30)
            took = time.monotonic() - t0
            if p.returncode != 1 or f"Intellex is already running on 127.0.0.1:{app.port}" not in (p.stderr or ""):
                problems.append(f"a second app.py on port {app.port}: exited {p.returncode} after {took:.1f} s, stderr "
                                f"{(p.stderr or '').strip()[:160]!r}; expected 1 with 'already running'")
            elif took > 10:
                problems.append(f"the second app.py took {took:.1f} s to refuse")
        except subprocess.TimeoutExpired:
            problems.append(f"a second app.py on port {app.port} was still running after 30 s: it did not refuse the "
                            f"port (and was killed)")
        time.sleep(1.0)
        if urls() != [f"{app.url}/_launcher"]:
            problems.append(f"the refused second app.py opened the browser: {urls()}")
        st, body = app.json("GET", "/_api/status")
        if st != 200:
            problems.append(f"the first app.py stopped answering after the second was refused: {st}")
    try:
        ep = WsEndpoint("127.0.0.2", 80)
    except OSError as e:
        ep = None
        bench.note(f"{tid}: --ws at launch not checked: nothing can listen on 127.0.0.2:80 here ({e})")
    if ep is not None:
        try:
            os.remove(opened)
            with IntellexHost(bench, sd, script="app.py", args=["--browser", "--ws", "127.0.0.2"], env=env,
                              ws_hosts=["127.0.0.2"]) as app:
                got = wait_urls(1)
                st, body = app.json("GET", "/_api/status")
                if got != [f"{app.url}/"]:
                    problems.append(f"--browser --ws 127.0.0.2 opened {got}, expected [{app.url}/] (straight to the "
                                    f"tool)")
                if not body.get("attached") or body.get("target") != "ws://127.0.0.2/ws":
                    problems.append(f"--browser --ws 127.0.0.2: /_api/status {body}, expected attached at launch")
        finally:
            ep.close()
    try:
        run_intellex_py(bench, tid, "host_units.py", args={"group": "app"}, stage_dir=sd)
    except AssertionError as e:
        problems.append(f"in-process (host_units.py app): {e}")
    assert not problems, "; ".join(problems)


@test("intellex.certs_context_unit", "certs.context() verifies on this venv; with an empty platform store (a stock "
      "python.org macOS build) it loads certifi's bundle with verification still on, built once; a populated store is "
      "left alone; with no certifi it fails honestly rather than turning verification off (IX re-scan certs.context)")
def certs_context_unit(bench):
    """tests/intellex/py/paths_logs.py group certs: Intellex src/certs.py context() (:33-48), every HTTPS fetch's
    context (flash._get, the tool and wiki fetchers, the version check), with ssl.create_default_context stood in for
    to make the store empty or populated."""
    run_intellex_py(bench, "intellex.certs_context_unit", "paths_logs.py", args={"group": "certs"})


@test("intellex.fetch_firmware_unit", "tools/fetch_firmware.py caches NaviCore's set and the WCB sets for ESP32 and both "
      "ESP32-S3 flash sizes and exits 0; a product whose listing 404s fails alone (exit 1, the other cached); a build "
      "with no 8 MB S3 bootloader is a note (GitHub stood in for; IX re-scan tools.fetch_firmware)")
def fetch_firmware_unit(bench):
    """tests/intellex/py/fetch_units.py group fetch_firmware: Intellex tools/fetch_firmware.py main() (:36-98) in the venv
    process with flash/wcb_flash reachable() and _get() answered there; the cache is the stage's src/firmware.
    intellex.update_routes runs the route that starts it."""
    run_intellex_py(bench, "intellex.fetch_firmware_unit", "fetch_units.py", args={"group": "fetch_firmware"})


@test("intellex.fetch_wiki_unit", "tools/fetch_wiki.py: both link dialects and image tags read into pages and assets, a "
      "dotted page name not taken for a file; with no page index the crawl from Home and _Sidebar, a 404 page skipped, "
      "a 2 MB image fetched whole (no size ceiling), an asset path out of the wiki refused (a leading ../ logged and "
      "skipped, no longer stripped into a path inside the wiki); the swap recovering a "
      "half-swap and putting the old copy back; one empty wiki kept on disk while the others update; offline, nothing "
      "touched (GitHub stood in for; IX re-scan tools.fetch_wiki)")
def fetch_wiki_unit(bench):
    """tests/intellex/py/fetch_units.py group fetch_wiki: Intellex tools/fetch_wiki.py _targets, _is_asset, fetch_one,
    _swap_in and main() (:132-381), with page_index, _get and _size_of answered in the venv process and the wikis in a
    scratch directory inside the stage."""
    run_intellex_py(bench, "intellex.fetch_wiki_unit", "fetch_units.py", args={"group": "fetch_wiki"})


# ------------------------------------------------------------------ the 2026-10-12 re-scan's low-risk rows
@test("intellex.wifi_bounce_route", "POST /_api/wifi-bounce runs discover.wifi_bounce in a thread with the body's ssid, "
      "'NaviCore' with no body, a body that is not JSON or one without ssid, and answers {ok, message} as the bounce "
      "returned them; a slow bounce leaves /_api/status answering (in-process, the bounce stood in for; IX re-scan "
      "host.api.wifi_bounce_route)")
def wifi_bounce_route(bench):
    """tests/intellex/py/host_units.py group wifi_bounce_route: Intellex src/host.py api_wifi_bounce (:425-434) on its
    real build_app() in the venv process, discover.wifi_bounce replaced before the app starts (and the macOS bounce made
    to raise), so no adapter is touched. No page calls this route; intellex.origin_guard leaves it out on purpose."""
    run_intellex_py(bench, "intellex.wifi_bounce_route", "host_units.py", args={"group": "wifi_bounce_route"})


@test("intellex.signals_route", "POST /_api/signals: 409 with nothing attached, 200 and nothing done on a WebSocket "
      "link (a stand-in endpoint on 127.0.0.2:80), 409 again after the detach; on a serial link DTR and RTS reach "
      "set_signals as given (None when absent), and a driver error is 502 (IX re-scan host.api.signals)")
def signals_route(bench):
    """Intellex src/host.py api_signals (:1348-1372): no transport is 409; a WebSocketTransport inherits Transport's
    set_signals, a no-op (transport.py:68-77, ws_transport.py:136), so a page's setSignals over WiFi answers ok. On a
    staged host against hil/ws.py WsEndpoint; the serial half in-process (tests/intellex/py/host_units.py group
    signals), a fake transport recording what set_signals was handed."""
    require(bench)
    tid = "intellex.signals_route"
    problems = []
    sd = stage(bench, tid, tools="none")
    try:
        ep = WsEndpoint("127.0.0.2", 80)
    except OSError as e:
        ep = None
        bench.note(f"{tid}: the WebSocket half not checked: nothing can listen on 127.0.0.2:80 here ({e})")
    try:
        with IntellexHost(bench, sd, ws_hosts=["127.0.0.2"]) as host:
            body = {"dataTerminalReady": False, "requestToSend": False}
            st, reply = host.json("POST", "/_api/signals", body)
            if st != 409 or reply != {"ok": False, "error": "nothing attached"}:
                problems.append(f"signals on a fresh host: {st} {reply}, expected 409 'nothing attached'")
            if ep is not None:
                host.attach({"kind": "ws", "host": "127.0.0.2", "role": "navicore"}, timeout=20)
                for b in (body, {"dataTerminalReady": True, "requestToSend": True}):
                    st, reply = host.json("POST", "/_api/signals", b)
                    if st != 200 or reply != {"ok": True}:
                        problems.append(f"signals {b} on a ws link: {st} {reply}, expected 200 {{ok: true}}")
                st, s = host.json("GET", "/_api/status")
                if not s.get("attached") or ep.upgraded != 1:
                    problems.append(f"after the signals the ws link: attached {s.get('attached')}, {ep.upgraded} "
                                    f"upgrade(s) at the endpoint - expected the one link untouched")
                host.detach()
                st, reply = host.json("POST", "/_api/signals", body)
                if st != 409:
                    problems.append(f"signals after the detach: {st} {reply}, expected 409")
    finally:
        if ep is not None:
            ep.close()
    try:
        run_intellex_py(bench, tid, "host_units.py", args={"group": "signals"}, stage_dir=sd)
    except AssertionError as e:
        problems.append(f"in-process (host_units.py signals): {e}")
    assert not problems, "; ".join(problems)


@test("intellex.reconnect_retry_reason", "While a reattach keeps failing the reconnect loop keeps lastError at "
      "'reconnect failed: <why>' and prints that line once per new reason, again when the reason changes back; a "
      "reattach that works prints 'reconnected', clears lastError and forgets the reason (in-process fakes; IX "
      "re-scan host.reconnect.retry_loop)")
def reconnect_retry_reason(bench):
    """tests/intellex/py/host_units.py group retry_loop: Intellex src/host.py reconnect_loop's except TransportError
    (:1789-1805, failed_why) on its real build_app() in the venv process, a fake serial transport whose open() fails
    with the reason the case sets. intellex.bridge_failed_attach_w1 reads the same on W1's real, held port."""
    run_intellex_py(bench, "intellex.reconnect_retry_reason", "host_units.py", args={"group": "retry_loop"},
                    timeout=300)


@test("intellex.version_unit", "version.info(): git's short SHA from a checkout, '+dirty' with an uncommitted change, "
      "built 'from source'; a build stamp wins over git even on a dirty tree; with neither, no commit; describe() and "
      "GET /_api/version agree (a scratch git repo; IX re-scan host.api.version)")
def version_unit(bench):
    """tests/intellex/py/host_units.py group version: Intellex src/version.py info, _from_stamp, _from_git and describe
    (:34-77) and host.py api_version (:559-565). version.__file__ is pointed into a scratch git repo inside the stage
    (and, for 'no git', a directory git is kept from searching above with GIT_CEILING_DIRECTORIES); the stamp is a
    build_stamp module put in sys.modules, as a frozen build imports its generated one."""
    run_intellex_py(bench, "intellex.version_unit", "host_units.py", args={"group": "version"})


@test("intellex.tool_base_unit", "settings.tool_base: main (or no branch) is the site's own /Wizard or /config_tool, "
      "any other branch /dev/<branch>/<leaf>, the configured branch when none is given; fetch_webui.base_for and the "
      "launcher's version check ask the same URLs (GitHub stood in for; IX re-scan settings.tool_base)")
def tool_base_unit(bench):
    """tests/intellex/py/host_units.py group tool_base: Intellex src/settings.py tool_base (:126-140), tools/
    fetch_webui.py base_for (:71-78) and host.py _published_dtg / _wcb_published_ver (:461-519), whose urlopen is
    answered in the venv process with flash.reachable True - nothing reaches the network."""
    run_intellex_py(bench, "intellex.tool_base_unit", "host_units.py", args={"group": "tool_base"})


@test("intellex.flash_progress_unit", "flash.write_progress weights each region by size: a 2 KB bootloader written to "
      "100 % is about 0.1 % of a 2 MB write, not 99; a region's last line naming the next one's start stays with it; "
      "/_api/flash-status's percent is monotonic and held at 99; its log keeps the last 400 lines (IX re-scan "
      "host.flash.status_progress)")
def flash_progress_unit(bench):
    """tests/intellex/py/flash_units.py group progress: Intellex src/flash.py write_progress (:423-459) fed esptool 5's
    progress lines ('Writing at 0x... [===] 50.0% ...'), and host.py _flash_progress / _flash_log (:985-996) - the state
    /_api/flash-status serves."""
    run_intellex_py(bench, "intellex.flash_progress_unit", "flash_units.py", args={"group": "progress"})


@test("intellex.discover_scan_unit", "discover.scan() rows: via, routable, reachable (TCP asked only with a route, the "
      "probe only with TCP), kind and its flags, version, relayId, alias, peers and the hint for a NaviCore, a relay, a "
      "WCB doorway, a routed host with no TCP and one with no route; INTELLEX_DISCOVER_HOSTS and an explicit list; "
      "_hint's shapes (the OS answered here; IX re-scan discover.scan_rows)")
def discover_scan_unit(bench):
    """tests/intellex/py/discover_units.py group scan_rows: Intellex src/discover.py scan (:456-484), _default_candidates
    (:446-453) and _hint (:774-802), with local_ip_for, tcp_open and probe answered in the venv process - nothing is
    probed."""
    run_intellex_py(bench, "intellex.discover_scan_unit", "discover_units.py", args={"group": "scan_rows"})


@test("intellex.winsize_mac_unit", "winsize._mac_visible_frame: the main screen less menu bar and Dock, top counted down "
      "from the screen's own top and relative to that screen; None without one; fit() centres in it and falls back "
      "when AppKit fails; on a Mac the real call is four ints inside the main screen (IX re-scan winsize.fit)")
def winsize_mac_unit(bench):
    """tests/intellex/py/paths_logs.py group winsize_mac: Intellex src/winsize.py _mac_visible_frame (:144-166) with
    AppKit's NSScreen stood in for (a secondary screen at a negative global y, a left Dock), and the real call on this
    Mac (skipped elsewhere: the fakes still run)."""
    run_intellex_py(bench, "intellex.winsize_mac_unit", "paths_logs.py", args={"group": "winsize_mac"})


def _version_of(sd):
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', open(os.path.join(sd, "src", "version.py"), encoding="utf-8").read(),
                  re.M)
    return m.group(1) if m else "?"


@test("intellex.applog_unit", "Each run's log opens with its header - 'Intellex v<VERSION> (...)', started, python, "
      "frozen, bundle, data and argv as the host was started - and the host's lines are in it at once; a data "
      "directory where no logs directory can be made leaves the host serving, /_api/log naming no file; applog's "
      "fallback and tee in the venv (IX re-scan applog.tee_prune)")
def applog_unit(bench):
    """Intellex src/applog.py start (:110-142) as a staged host runs it (host.py main), _Tee (:38-87). The header
    lines are read from the staged host's own log while it runs; the second host's appdata/Intellex/logs is a FILE.
    tests/intellex/py/paths_logs.py group applog: start() returning None with stdout untouched, the tee flushing each
    line and surviving a sink or console that fails."""
    import glob as _glob
    require(bench)
    tid = "intellex.applog_unit"
    problems = []
    sd = stage(bench, tid, tools="none")
    ver = _version_of(sd)
    with IntellexHost(bench, sd) as host:
        logs = _glob.glob(os.path.join(sd, "appdata", "Intellex", "logs", "intellex-*.log"))
        st, info = host.json("GET", "/_api/log")
        if len(logs) != 1:
            problems.append(f"the staged host left {len(logs)} intellex-*.log files, expected one")
        else:
            with open(logs[0], encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
            head = lines[:8]
            want = [("Intellex v" + ver, lambda x: x.startswith(f"Intellex v{ver}")),
                    ("  started  ", lambda x: x.startswith("  started  20")),
                    ("  python   3.", lambda x: x.startswith("  python   3.")),
                    ("  frozen   False", lambda x: x == "  frozen   False"),
                    ("  bundle   <stage>/src", lambda x: os.path.realpath(x[11:]) == os.path.realpath(
                        os.path.join(sd, "src")) if x.startswith("  bundle   ") else False),
                    ("  data     <stage>/appdata/Intellex", lambda x: os.path.realpath(x[11:]) == os.path.realpath(
                        os.path.join(sd, "appdata", "Intellex")) if x.startswith("  data     ") else False),
                    (f"  argv     --port {host.port} --no-auto-bounce",
                     lambda x: x == f"  argv     --port {host.port} --no-auto-bounce"),
                    ("(a blank line)", lambda x: x == "")]
            for i, (what, ok) in enumerate(want):
                if i >= len(head) or not ok(head[i]):
                    problems.append(f"log line {i + 1} is {head[i] if i < len(head) else None!r}, expected {what!r}")
            if not any(x.startswith(f"serving   http://127.0.0.1:{host.port}/") for x in lines):
                problems.append("the host's 'serving' line is not in its log while it runs: the log is buffered")
            if os.path.realpath(info.get("path", "")) != os.path.realpath(logs[0]):
                problems.append(f"/_api/log names {info.get('path')!r}, not the log the host is writing")
    sd2 = stage(bench, tid + "_nolog", tools="none")
    logs_dir = os.path.join(sd2, "appdata", "Intellex", "logs")
    with open(logs_dir, "w", encoding="utf-8") as f:
        f.write("a file where the logs directory goes\n")
    with IntellexHost(bench, sd2) as host:
        st, info = host.json("GET", "/_api/log")
        if st != 200 or info.get("path") != "" or info.get("tail") != "" or \
                os.path.realpath(info.get("dir", "")) != os.path.realpath(logs_dir):
            problems.append(f"with no logs directory possible /_api/log: {st} {info}, expected no path, no tail and "
                            f"dir {logs_dir}")
        if host.json("GET", "/_api/status")[0] != 200:
            problems.append("with no logs directory possible the host does not answer /_api/status")
    try:
        run_intellex_py(bench, tid, "paths_logs.py", args={"group": "applog"}, stage_dir=sd)
    except AssertionError as e:
        problems.append(f"in the venv (paths_logs.py applog): {e}")
    assert not problems, "; ".join(problems)


@test("intellex.log_tail_cap", "GET /_api/log: the path of this run's log inside the stage, the logs directory, and "
      "exactly its last 400 lines after 600 more are written to it; a log it cannot read gives '(could not read the "
      "log: ...)' instead (IX re-scan host.api.log)")
def log_tail_cap(bench):
    """Intellex src/host.py api_log (:568-590). The 600 numbered lines are appended to the staged host's own log file;
    the tail is compared with the file's last 400 lines read right after. The unreadable case takes the file's read
    permission away for one request and puts it back."""
    import stat as _stat
    require(bench)
    sd = stage(bench, "intellex.log_tail_cap", tools="none")
    problems = []
    with IntellexHost(bench, sd) as host:
        st, info = host.json("GET", "/_api/log")
        p = info.get("path", "")
        if os.path.realpath(info.get("dir", "")) != os.path.realpath(os.path.join(sd, "appdata", "Intellex", "logs")) \
                or not os.path.realpath(p).startswith(os.path.realpath(info.get("dir", "")) + os.sep):
            problems.append(f"/_api/log: path {p!r}, dir {info.get('dir')!r}, expected this run's log in the stage's "
                            f"appdata/Intellex/logs")
        with open(p, "a", encoding="utf-8") as f:
            for i in range(600):
                f.write(f"HILTAIL-{i:03d}\n")
        st, info = host.json("GET", "/_api/log")
        with open(p, encoding="utf-8", errors="replace") as f:
            want = "".join(f.readlines()[-400:])
        tail = info.get("tail", "")
        if tail != want or tail.count("\n") != 400:
            problems.append(f"the tail after 600 more lines: {tail.count(chr(10))} lines (first {tail[:14]!r}), "
                            f"expected the file's last 400")
        elif "HILTAIL-199" in tail or "HILTAIL-200" not in tail or not tail.rstrip().endswith("HILTAIL-599"):
            problems.append("the tail is not HILTAIL-200 .. HILTAIL-599")
        mode = os.stat(p).st_mode
        os.chmod(p, 0)
        try:
            st, info = host.json("GET", "/_api/log")
        finally:
            os.chmod(p, _stat.S_IMODE(mode))
        if st != 200 or not str(info.get("tail", "")).startswith("(could not read the log: "):
            problems.append(f"an unreadable log: {st}, tail {str(info.get('tail'))[:60]!r}, expected '(could not read "
                            f"the log: ...)'")
    assert not problems, "; ".join(problems)


@test("intellex.branch_list_cached", "GET /_api/branch-list offline serves a seeded branches_cache.json (cached, its "
      "names, the set branch and main put in, 'missing' for a set branch it lacks, the offline reason); online with "
      "GitHub stood in for, gh-pages is dropped, the first call of a run fetches inside the TTL, later ones do not "
      "until it expires or ?refresh=1, and a total failure does not end the once-per-run fetch (IX re-scan "
      "host.api.branch_list)")
def branch_list_cached(bench):
    """Intellex src/host.py api_branch_list (:598-615) and src/branchlist.py branches (:95-160). Offline on a staged host
    with appdata/Intellex/branches_cache.json seeded and the WCB branch set to one the cache does not list. The cache
    here holds gh-pages, which a fetched list never does (branches() drops it before writing the cache, since its first
    commit): offline it is served as cached - pinned, a product call. The online half in the venv
    (tests/intellex/py/host_units.py group branch_list), branchlist._fetch and flash.reachable answered there."""
    import time
    require(bench)
    tid = "intellex.branch_list_cached"
    sd = stage(bench, tid, tools="none", settings={"branch": {"wcb": "hil-other", "navicore": "main"}})
    with open(os.path.join(sd, "appdata", "Intellex", "branches_cache.json"), "w", encoding="utf-8") as f:
        json.dump({"navicore": ["main", "hil-nc"], "wcb": ["main", "gh-pages", "hil-feat"], "at": time.time()}, f)
    problems = []
    with IntellexHost(bench, sd) as host:
        st, bl = host.json("GET", "/_api/branch-list", timeout=30)
        want = {"navicore": ["main", "hil-nc"], "wcb": ["hil-other", "main", "gh-pages", "hil-feat"], "cached": True,
                "missing": ["wcb"]}
        got = {k: bl.get(k) for k in want} if isinstance(bl, dict) else bl
        if st != 200 or got != want:
            problems.append(f"offline with a seeded cache: {st} {got}, expected {want}")
        if not str((bl or {}).get("error", "")).startswith("offline ("):
            problems.append(f"offline the error says {str((bl or {}).get('error'))[:80]!r}, not why ('offline (...)')")
        st, bl2 = host.json("GET", "/_api/branch-list?refresh=1", timeout=30)
        if st != 200 or {k: bl2.get(k) for k in want} != want:
            problems.append(f"?refresh=1 offline: {st} {bl2}, expected the cached lists again")
    try:
        run_intellex_py(bench, tid, "host_units.py", args={"group": "branch_list"}, stage_dir=sd)
    except AssertionError as e:
        problems.append(f"online, GitHub stood in for (host_units.py branch_list): {e}")
    assert not problems, "; ".join(problems)


@test("intellex.firmware_summary_newest", "GET /_api/firmware reports the configured branch's cache per product: the "
      "count of .bin files and as newest an app image, never its _part, _boot or bootloader companions; another branch's "
      "files are not counted, and moving the branch moves the summary (a seeded cache; IX re-scan "
      "host.api.firmware_summary)")
def firmware_summary_newest(bench):
    """Intellex src/host.py api_firmware (:636-638) and src/fwcache.py summary (:143-168). The cache is seeded where a
    non-frozen host keeps it (seed_firmware); a leftover .part from an interrupted store and the listing are not
    images."""
    require(bench)
    tid = "intellex.firmware_summary_newest"
    sd = stage(bench, tid, tools="none")
    t = "6.2.1_120000ROCT2026_hilsum"
    wcb = {f"WCB_{t}_{k}.bin": b"x" * 64 for k in ("ESP32", "ESP32_part", "ESP32_boot", "ESP32S3", "ESP32S3_part",
                                                     "ESP32S3_boot_8MB", "ESP32S3_boot_16MB")}
    d = seed_firmware(sd, "wcb", "main", wcb)
    with open(os.path.join(d, f"WCB_{t}_ESP32.bin.part"), "wb") as f:
        f.write(b"partial")
    nc = {f"NaviCore_{t}_ESP32S3.bin": b"n" * 64, f"NaviCore_{t}_ESP32S3_part.bin": b"p" * 64,
          "WCB_S3_custom_bootloader_16MB_wdt3s.bin": b"b" * 64}
    seed_firmware(sd, "navicore", "main", nc)
    seed_firmware(sd, "wcb", "hil-sum", {"WCB_9.9.9_zzz_ESP32S3.bin": b"z" * 64})
    problems = []
    with IntellexHost(bench, sd) as host:
        st, fw = host.json("GET", "/_api/firmware")
        want = {"wcb": {"count": 7, "newest": f"WCB_{t}_ESP32S3.bin", "branch": "main"},
                "navicore": {"count": 3, "newest": f"NaviCore_{t}_ESP32S3.bin", "branch": "main"}}
        if st != 200 or fw != want:
            problems.append(f"/_api/firmware on main: {st} {fw}, expected {want}")
        st, _ = host.json("POST", "/_api/branch", {"product": "wcb", "branch": "hil-sum"})
        st, fw = host.json("GET", "/_api/firmware")
        if (fw or {}).get("wcb") != {"count": 1, "newest": "WCB_9.9.9_zzz_ESP32S3.bin", "branch": "hil-sum"}:
            problems.append(f"with WCB on hil-sum: {fw}, expected that branch's one image")
        host.json("POST", "/_api/branch", {"product": "wcb", "branch": "hil-nothing"})
        st, fw = host.json("GET", "/_api/firmware")
        if (fw or {}).get("wcb") != {"count": 0, "newest": "", "branch": "hil-nothing"}:
            problems.append(f"with WCB on a branch with no cache: {fw}, expected count 0")
        host.json("POST", "/_api/branch", {"product": "wcb", "branch": "main"})
    assert not problems, "; ".join(problems)


@test("intellex.wiki_inventory", "GET /_api/wiki lists each wiki with its title, its pages without the _Sidebar, _Footer "
      "and _Header furniture, the count of those and have; a product with no wiki is have false, count 0 (a crafted "
      "wiki; IX re-scan host.api.wiki_inventory)")
def wiki_inventory(bench):
    """Intellex src/host.py api_wiki (:828-830) and src/wikidocs.py available (:57-70), FURNITURE (:47). The crafted
    wiki (tests/intellex/fixtures/wiki) has a _Sidebar; a _Footer and a _Header are added in the stage."""
    require(bench)
    sd = stage(bench, "intellex.wiki_inventory", tools="none")
    root = seed_wiki(sd)
    for name in ("_Footer", "_Header"):
        with open(os.path.join(root, "wcb", name + ".md"), "w", encoding="utf-8") as f:
            f.write(f"HIL {name}\n")
    on_disk = sorted(os.path.splitext(n)[0] for n in os.listdir(os.path.join(root, "wcb")) if n.endswith(".md"))
    pages = [p for p in on_disk if p not in ("_Sidebar", "_Footer", "_Header")]
    problems = []
    with IntellexHost(bench, sd) as host:
        st, body = host.json("GET", "/_api/wiki")
        wikis = {w.get("product"): w for w in (body or {}).get("wikis", [])}
        if st != 200 or sorted(wikis) != ["intellex", "navicore", "wcb"]:
            problems.append(f"/_api/wiki: {st}, products {sorted(wikis)}")
        w = wikis.get("wcb", {})
        if w.get("pages") != pages or w.get("count") != len(pages) or w.get("have") is not True or \
                w.get("title") != "WCB":
            problems.append(f"wcb: {w}, expected title WCB, pages {pages} (of {on_disk}), count {len(pages)}, have")
        if any(f in (w.get("pages") or []) for f in ("_Sidebar", "_Footer", "_Header")):
            problems.append(f"furniture listed as pages: {w.get('pages')}")
        n = wikis.get("navicore", {})
        if n.get("have") is not False or n.get("count") != 0 or n.get("pages") != []:
            problems.append(f"navicore (no wiki seeded): {n}, expected have false, count 0")
        i = wikis.get("intellex", {})
        if i.get("count") != 2 or i.get("have") is not True:
            problems.append(f"intellex: {i}, expected its two pages")
    assert not problems, "; ".join(problems)


@test("intellex.wiki_missing_text", "The docs with nothing downloaded: /wiki/ and a wiki's page say 'No offline docs "
      "yet' and how to get them (Download docs in the launcher); with one wiki stored, /wiki/ goes to it and a missing "
      "product says 'Not downloaded' naming it (IX re-scan wiki.nothing_downloaded)")
def wiki_missing_text(bench):
    """Intellex src/host.py _wiki_missing (:735-747), wiki_index (:750-755) and wiki_page (:785-786): a page of a
    product whose wiki is not on disk. Stages with no wiki, then with only the crafted WCB one."""
    require(bench)
    problems = []
    sd = stage(bench, "intellex.wiki_missing_text", tools="none")
    with IntellexHost(bench, sd) as host:
        for path in ("/wiki/", "/wiki/wcb/", "/wiki/navicore/Home"):
            st, hdr, body = host.http("GET", path)
            html = body.decode("utf-8", "replace")
            if st != 200 or "No offline docs yet" not in html or "Download docs" not in html or \
                    "Not downloaded" in html:
                problems.append(f"no wiki at all, GET {path}: {st}, 'No offline docs yet' {'No offline docs yet' in html}"
                                f", 'Download docs' {'Download docs' in html}")
    sd2 = stage(bench, "intellex.wiki_missing_text_one", tools="none")
    root = seed_wiki(sd2)
    shutil.rmtree(os.path.join(root, "intellex"))
    with IntellexHost(bench, sd2) as host:
        st, hdr, _ = host.http("GET", "/wiki/")
        if st != 302 or not _hdr(hdr, "location").endswith("/wiki/wcb/"):
            problems.append(f"with only the WCB wiki, GET /wiki/: {st} -> {_hdr(hdr, 'location')!r}, expected a 302 "
                            f"to /wiki/wcb/")
        for path, product in (("/wiki/navicore/", "navicore"), ("/wiki/intellex/Home", "intellex")):
            st, hdr, body = host.http("GET", path)
            html = body.decode("utf-8", "replace")
            if st != 200 or "Not downloaded" not in html or f"<b>{product}</b> wiki is not on disk" not in html or \
                    "No offline docs yet" in html:
                problems.append(f"with only the WCB wiki, GET {path}: {st}, 'Not downloaded' {'Not downloaded' in html}"
                                f", names {product}: {f'<b>{product}</b>' in html}")
            if 'href="/wiki/wcb/"' not in html:
                problems.append(f"GET {path}: the page does not offer the WCB wiki's tab")
    assert not problems, "; ".join(problems)


@test("intellex.wiki_dotted_page", "A wiki page whose name looks like it has an extension (version3.2_build) is served "
      "as a rendered page because it exists, not as a missing asset; a real image beside it is still an asset and a "
      "dotted name with no page says the page is not there (a crafted wiki; IX re-scan wiki.page_named_with_dot)")
def wiki_dotted_page(bench):
    """Intellex src/host.py wiki_page (:758-792): render_page first, by what is on disk (wikidocs._page_file), then a
    file under the wiki, then the 'not in the downloaded copy' page. The page is written into the stage's crafted
    WCB wiki, with a link to it from a page."""
    require(bench)
    sd = stage(bench, "intellex.wiki_dotted_page", tools="none")
    root = seed_wiki(sd)
    with open(os.path.join(root, "wcb", "version3.2_build.md"), "w", encoding="utf-8") as f:
        f.write("# Version 3.2 build\n\nHILDOTTED page body.\n")
    with open(os.path.join(root, "wcb", "Links-To-Dotted.md"), "w", encoding="utf-8") as f:
        f.write("# Links\n\n[[version3.2_build]]\n")
    problems = []
    with IntellexHost(bench, sd) as host:
        st, hdr, body = host.http("GET", "/wiki/wcb/version3.2_build")
        html = body.decode("utf-8", "replace")
        if st != 200 or not _hdr(hdr, "content-type").startswith("text/html") or "HILDOTTED" not in html or \
                "<title>Version 3.2 build \u00b7 Intellex Docs</title>" not in html:
            problems.append(f"GET /wiki/wcb/version3.2_build: {st} {_hdr(hdr, 'content-type')!r}, rendered "
                            f"{'HILDOTTED' in html}")
        if _hdr(hdr, "content-security-policy").startswith("sandbox"):
            problems.append("the dotted page was served with the asset CSP")
        st, hdr, body = host.http("GET", "/wiki/wcb/Links-To-Dotted")
        if 'href="/wiki/wcb/version3.2_build"' not in body.decode("utf-8", "replace"):
            problems.append("a [[version3.2_build]] link does not point at /wiki/wcb/version3.2_build")
        st, hdr, _ = host.http("GET", "/wiki/wcb/Images/pic.png")
        if st != 200 or not _hdr(hdr, "content-security-policy").startswith("sandbox"):
            problems.append(f"the image beside it: {st}, CSP {_hdr(hdr, 'content-security-policy')[:40]!r}")
        st, _, body = host.http("GET", "/wiki/wcb/version9.9_none")
        if st != 200 or b"not in the downloaded copy" not in body:
            problems.append(f"a dotted name with no page: {st}, without 'not in the downloaded copy'")
    assert not problems, "; ".join(problems)


# ------------------------------------------------------------------ the chooser (launcher.html), re-scan low-risk rows
@test("intellex.ui_launcher_identify_states", "The chooser's serial rows: identify answering {attached:true} is "
      "'connected right now' and lit, and a failed or non-JSON identify leaves the bare chip label - each upgraded in "
      "place with the selected row's panel following; FTDI, a description and a bare port labelled; macOS debug, "
      "Bluetooth and wlan nodes skipped (Playwright; IX re-scan launcher.serial_list)")
def ui_launcher_identify_states(bench):
    """Intellex src/launcher.html scan (:771-866): chipLabel, worthAsking (303A only), skip, the identify .then/.catch.
    /_api/ports and identify are fulfilled in the page (tests/intellex/specs/ui_launcher.spec.js). Found writing it
    (GUI run 20261013-000028): the attached answer returned before the panel repaint and the .catch had none, so a
    selected port kept '... — asking…' in the panel - fixed in Intellex d486fa9."""
    run_intellex_test(bench, "intellex.ui_launcher_identify_states")


@test("intellex.ui_launcher_wifi_watch", "The chooser's WiFi list with no droid: one hint from the most informative "
      "candidate with ' — still watching…' while the 2-minute watch runs, a rescan every 4 s, and after 2 minutes a "
      "last pass without the suffix, no more rescans, the header reading 'scan complete' and 'No droid found' offered "
      "again (Playwright with a fake clock; IX re-scan launcher.wifi_list)")
def ui_launcher_wifi_watch(bench):
    """Intellex src/launcher.html scanWifi and watchForDroid, WATCH_EVERY_MS 4 s, WATCH_FOR_MS 2 min, under Playwright's
    page.clock. The re-scan's product call, changed in Intellex 2026-10-13: '#scanstate' kept reading 'watching…' after
    the watch had ended (the last pass still saw a routable host not answering), and the end of the watch never offered
    'No droid found'; now the header says 'scan complete' and the offer is made when the watch runs out."""
    run_intellex_test(bench, "intellex.ui_launcher_wifi_watch")


@test("intellex.ui_launcher_theme_first_run", "The chooser on a first run follows the OS theme (prefers-color-scheme "
      "light and dark), the mark swaps to its light build with the theme, a chosen theme is remembered over the OS, "
      "and the layout picker is built from VIEWS with WCB the default (Playwright; IX re-scan launcher.theme_views)")
def ui_launcher_theme_first_run(bench):
    """Intellex src/launcher.html currentTheme / applyTheme (:521-543) and buildViews / currentView (:545-606), in a
    fresh browser context per colour scheme."""
    run_intellex_test(bench, "intellex.ui_launcher_theme_first_run")


@test("intellex.ui_launcher_maintenance_buttons", "The chooser's Update tools, Download firmware and Download docs POST "
      "their route with the button disabled meanwhile, report success, partial and failure ('updated — config tool A, "
      "Wizard B', 'E (now: config tool A)', 'firmware cached — safe to go offline now', 'docs updated', ...) and "
      "re-check; Rescan asks for the ports, discovery and every check again (Playwright; IX re-scan "
      "launcher.maintenance_actions)")
def ui_launcher_maintenance_buttons(bench):
    """Intellex src/launcher.html $('update').onclick (:1175-1191), $('updatefw').onclick (:929-941),
    $('updatewiki').onclick (:1128-1141) and $('rescan').onclick; the three update routes are fulfilled in the page,
    so nothing reaches GitHub."""
    run_intellex_test(bench, "intellex.ui_launcher_maintenance_buttons")


@test("intellex.ui_launcher_branch_set_fetches_tool", "The chooser's Set, when it changes a product's branch, fetches "
      "that tool at once (?force=1&tool=wcb|navicore|all) and says which copy landed (main's for a branch that "
      "publishes none) or why none did; every open page of that tool - a Wizard and a config tool beside the chooser - "
      "offers Reload/Later and reloads only on Reload; an unchanged Set fetches nothing; Update tools tells only a "
      "tool it swapped in (Playwright, offline; 2026-10-15 'it knew to change the binary, but not the gui')")
def ui_launcher_branch_set_fetches_tool(bench):
    """Intellex src/launcher.html $('br-save').onclick, fetchBranchTools, announceTools and $('update').onclick, and
    src/intellex_shim.js updateNotice (the 'intellex' BroadcastChannel); /_api/update-webui is fulfilled in the page,
    the branches are written to the stage's settings.json and put back to main."""
    run_intellex_test(bench, "intellex.ui_launcher_branch_set_fetches_tool")


@test("intellex.ui_launcher_app_update", "The launcher's Intellex line and Update Intellex: up to date says so; a release "
      "available is called out in the summary, asks in the page before installing (Cancel installs nothing), shows the "
      "install's step and percent and then 'restarting'; a refusal shows the host's reason; a git checkout offers its "
      "new commits; the first start after an update says so once; Check asks again (Playwright, /_api/app-update "
      "fulfilled)")
def ui_launcher_app_update(bench):
    """Intellex src/launcher.html checkApp / paintApp / $('appupd*') against src/selfupdate.py status() shapes."""
    run_intellex_test(bench, "intellex.ui_launcher_app_update")


def _seed_branch_cache(sd):
    import time
    with open(os.path.join(sd, "appdata", "Intellex", "branches_cache.json"), "w", encoding="utf-8") as f:
        json.dump({"navicore": ["main"], "wcb": ["main", "hil-feat"], "at": time.time()}, f)


@test("intellex.ui_launcher_branch_refresh", "The chooser's branch picker with a seeded branch-list cache that lacks the "
      "set WCB branch: the note says it is no longer in the list, Refresh requests ?refresh=1 and puts the list's error "
      "in the status line, a clean refresh says 'branch lists updated' and the note goes, a failed request is 'could "
      "not ask the host' (Playwright, offline; IX re-scan launcher.branch_picker)")
def ui_launcher_branch_refresh(bench):
    """Intellex src/launcher.html checkBranches / fillBranchOptions / $('br-refresh').onclick (:1007-1090) against the
    host's real /_api/branch-list offline (branchlist.py), the cache seeded into the stage's appdata and the WCB branch
    set to hil-gone in its settings.json."""
    run_intellex_test(bench, "intellex.ui_launcher_branch_refresh",
                      settings={"branch": {"wcb": "hil-gone", "navicore": "main"}}, seed=_seed_branch_cache)


@test("intellex.ui_launcher_docs_version", "With docs stored the chooser's Docs asks the host for a second window on "
      "/wiki/ and opens a popup itself when there is no window backend, nothing when the host made one; the header "
      "shows the build as /_api/version says it (Playwright, a crafted wiki; IX re-scan launcher.log_docs_version)")
def ui_launcher_docs_version(bench):
    """Intellex src/launcher.html $('opendocs').onclick (:1116-1127) and the #appver filler (:1146-1155);
    /_api/open-window is fulfilled in the page (501 as host.py answers with no window backend, then ok)."""
    run_intellex_test(bench, "intellex.ui_launcher_docs_version", wiki=True)


@test("intellex.ui_launcher_no_droid", "With no serial port and no droid the chooser's status says 'No droid found. "
      "You can still open the tool on its own...' once both lists have answered, not after the serial half alone "
      "(Playwright; IX re-scan launcher.back_offline_reload)")
def ui_launcher_no_droid(bench):
    """Intellex src/launcher.html offerOfflineIfEmpty (:1214-1222), called at the end of scan() after scanWifi; the
    discovery answer is held to show the order."""
    run_intellex_test(bench, "intellex.ui_launcher_no_droid")


# ------------------------------------------------------------------ the shell and the shim, re-scan low-risk rows
@test("intellex.ui_shell_moved_out", "The shell's New window hands the Wizard to its own window before the 4 s preload: "
      "the preload does not recreate its frame, and clicking the WCB tab brings it back (Playwright; IX re-scan "
      "shell.new_window)")
def ui_shell_moved_out(bench):
    """Intellex src/shell.html $('t-win').onclick (:348-375: movedOut, made[other] = false) and ensure / preloadOthers
    (:152-188: the preload skips a moved-out tool, an explicit ensure() clears it)."""
    run_intellex_test(bench, "intellex.ui_shell_moved_out")


@test("intellex.ui_shell_link_labels", "The shell's bar names the link from /_api/status every 2 s: 'WiFi <ip>', "
      "'Relay <ip> → mesh', 'WCB <ip> → mesh', 'USB <port>', 'reconnecting…' while wanted and down, 'not attached', "
      "and 'not attached' when the status request fails (Playwright; IX re-scan shell.link_label)")
def ui_shell_link_labels(bench):
    """Intellex src/shell.html labelFor and refresh (:377-396), the target strings host.py _label_for makes
    (:1885-1898). /_api/status is fulfilled for the shell document only."""
    run_intellex_test(bench, "intellex.ui_shell_link_labels")


@test("intellex.ui_shell_foreign_message", "The shell ignores open-chooser and close-chooser from another origin - a "
      "dispatched message from http://evil.example and one posted by an opaque-origin (data:) frame - and acts on the "
      "same messages from its own origin (Playwright; IX re-scan shell.chooser_overlay)")
def ui_shell_foreign_message(bench):
    """Intellex src/shell.html's message listener (:439-444: ev.origin !== location.origin is dropped)."""
    run_intellex_test(bench, "intellex.ui_shell_foreign_message")


@test("intellex.ui_rc_tool_url_kept", "In the Wizard through Intellex, rc_config_tool_url set to a user's own URL is "
      "left as it is, while one still on greghulette.github.io, or none, becomes '/' (Playwright; IX re-scan "
      "shim.rc_tool_url)")
def ui_rc_tool_url_kept(bench):
    """Intellex src/intellex_shim.js:360-366, which runs before the Wizard's app.js reads the key."""
    run_intellex_test(bench, "intellex.ui_rc_tool_url_kept")


@test("intellex.ui_chip_opens_chooser", "The Wizard's chip and the config tool's transport label open the chooser: on "
      "their own a navigation to /_launcher, inside the shell the overlay (the Wizard has no chip there), the tool's "
      "document kept (Playwright; IX re-scan shim.transport_label_chip)")
def ui_chip_opens_chooser(bench):
    """Intellex src/intellex_shim.js openChooser (:1488-1497), labelEl (:1436-1455), wcbChip (:1499-1520)."""
    run_intellex_test(bench, "intellex.ui_chip_opens_chooser")


@test("intellex.shim_crlf_split", "Characterisation: a CRLF whose CR ends one write and whose LF starts the next goes "
      "out as two /_link frames, the line with its CR then a bare LF (Playwright, nothing attached; IX re-scan "
      "shim.crlf_split_across_chunks)")
def shim_crlf_split(bench):
    """Intellex src/intellex_shim.js:202-216 (the per-line framing: a CR at the end of the buffer is sent at once,
    since the LF is not there yet). intellex.shim_line_framing covers a CRLF inside one chunk."""
    run_intellex_test(bench, "intellex.shim_crlf_split")


@test("intellex.shim_still_trying_banner", "While the host wants a link and has none the shim shows no banner for "
      "the first 20 s, then 'Still trying to reach the droid — <n>s' with a link to the chooser, removed once the host "
      "is attached (Playwright, /_api/status fulfilled; IX re-scan shim.watchlink_reconnect_banner)")
def shim_still_trying_banner(bench):
    """Intellex src/intellex_shim.js watchLink and banner (:1563-1618, QUIET_S 20 s), in the NaviCore config tool; once
    attached, /_link is a Playwright stand-in answering ?REC and PING (tests/intellex/specs/ui_shim.spec.js)."""
    run_intellex_test(bench, "intellex.shim_still_trying_banner")


# ------------------------------------------------------------------ the tools' Pages half, and the launchers
@test("intellex.webui_version_online", "GET /_api/webui-version online, GitHub Pages stood in for on 127.0.0.1: a "
      "bundle older than its published copy is stale with checked true and no reason, an equal one is not stale, each "
      "tool asks its configured branch's copy (settings.tool_base: <site>/config_tool, <site>/dev/<branch>/Wizard), "
      "and a failure says why - certs.ADVICE for a certificate the venv cannot verify, the HTTP error for a 404 - "
      "without hiding the other tool's answer (in-process; IX re-scan host.api.webui_version)")
def webui_version_online(bench):
    """Intellex src/host.py api_webui_version, _published_dtg and _wcb_published_ver on its real build_app() in the venv
    (tests/intellex/py/webui_units.py group webui_version). urlopen answers https://greghulette.github.io/<path> from
    a local HTTP stand-in (and, for the certificate case, an HTTPS one with a self-signed certificate the venv's own
    verification refuses); flash.reachable answers True; anything else is refused, so nothing reaches the network.
    intellex.offline_con_floor and intellex.routes_api cover the offline half."""
    require(bench)
    run_intellex_py(bench, "intellex.webui_version_online", "webui_units.py", args={"group": "webui_version"})


@test("intellex.fetch_webui_unit", "tools/fetch_webui.py against a local stand-in for GitHub Pages: _safe_relname; "
      "the cmdlib manifest walk; each tool's update one atomic swap, byte-identical to what was served, the old bundle "
      "kept whole as <dir>.prev; up to date, --check and --force; one missing file changes nothing and leaves no "
      "staging directory; the branch's /dev/<branch>/ copy, else main's with a note; --tool all as two independent "
      "swaps (in-process; IX re-scan tools.fetch_webui)")
def fetch_webui_unit(bench):
    """Intellex tools/fetch_webui.py (_safe_relname, cmdlib_names, _swap_in, fetch_wcb, _fetch_navicore, resolve_base,
    main) in the venv (tests/intellex/py/webui_units.py group fetch_webui), its main() run per argv against a
    127.0.0.1 server laid out like gh-pages (--base for the row's own case, and urlopen answering greghulette.github.io
    from it for the branch cases); fetch_webui.reachable answers True. The bundles are the stage's src/webui and
    src/webui_wcb, the staging directories go to a scratch directory in the stage. intellex.offline_con_floor covers
    the offline refusal."""
    require(bench)
    run_intellex_py(bench, "intellex.fetch_webui_unit", "webui_units.py", args={"group": "fetch_webui"})


@test("intellex.selfupdate_unit", "Intellex updating itself (src/selfupdate.py) against a stand-in for GitHub Releases: "
      "the check (none, up to date, newer with this computer's build and checksums, without either, offline, cached); "
      "Windows swaps the running .exe by rename and restarts, refusing a download that fails SHA256SUMS.txt; macOS "
      "swaps the .app (symlinks kept, the old one kept); a git checkout fast-forwards and restarts, refusing over "
      "uncommitted changes or its own commits; install is 409 with nothing to install or a flash running; "
      "version.info() ignores a stale build stamp from source (in-process)")
def selfupdate_unit(bench):
    """Intellex src/selfupdate.py and its host routes in the venv (tests/intellex/py/selfupdate_units.py group
    selfupdate): sys.executable and selfupdate.mode/root point at stand-ins under the stage, restart() is recorded,
    and every request goes to a 127.0.0.1 server - nothing reaches GitHub and nothing of Greg's is replaced."""
    require(bench)
    run_intellex_py(bench, "intellex.selfupdate_unit", "selfupdate_units.py", args={"group": "selfupdate"})


@test("intellex.tool_serving_unit", "A tool always shows the copy on disk: every file of both tools goes out no-cache, "
      "each served page asks for its own scripts and stylesheet at ?v=<mtime>.<size> (a changed file, a new URL), a "
      "frozen app serves an update's user copy with no restart, and /_api/update-webui says which tools it swapped in "
      "and passes --force for ?force=1 (in-process; 2026-10-15 'it knew to change the binary, but not the gui')")
def tool_serving_unit(bench):
    """Intellex src/host.py _tool_files, _versioned, webui_dir/wcb_dir and api_update_webui (changed, notes, --force)
    on its real build_app() in the venv (tests/intellex/py/webui_units.py group tool_serving): bundles written into
    the stage's src, paths.FROZEN set for the frozen case, the fetcher replaced by a stand-in that swaps the Wizard."""
    require(bench)
    run_intellex_py(bench, "intellex.tool_serving_unit", "webui_units.py", args={"group": "tool_serving"})


# A stand-in Python for the launcher scripts. Each name a launcher looks for on PATH (python3.11, python3, ...) and the
# venv's python3 it makes is a two-line sh wrapper running this file under the harness's own interpreter as the version
# it stands for. It records every call, answers -V, runs a -c snippet with sys.version_info set to that version (so the
# launcher's own version tests decide), makes a venv of wrappers for -m venv, and never installs, fetches or starts
# anything: -m pip, tools/fetch_webui.py and src/app.py are only recorded (the fetch writes src/webui/index.html when
# told to succeed). hil_launch_ctl.json beside the log says how each step goes.
_FAKE_PY = r'''import json, os, shlex, sys


def wrapper(path, ver, name, log, ctl, flags=()):
    argv = [sys.executable, "-I", os.path.abspath(__file__), "--as", ver, "--name", name, "--log", log, "--ctl", ctl]
    with open(path, "w") as f:
        f.write("#!/bin/sh\nexec " + " ".join(shlex.quote(x) for x in argv + list(flags) + ["--"]) + ' "$@"\n')
    os.chmod(path, 0o755)


def main():
    a = sys.argv[1:]
    cut = a.index("--")
    opts, args = a[:cut], a[cut + 1:]
    ver, name, log, ctl_path = (opts[opts.index(k) + 1] for k in ("--as", "--name", "--log", "--ctl"))
    try:
        with open(ctl_path) as f:
            ctl = json.load(f)
    except (OSError, ValueError):
        ctl = {}
    rec = {"exe": name, "as": ver, "argv": args, "cwd": os.getcwd()}
    if "-r" in args:
        rec["requirements_found"] = os.path.isfile(args[args.index("-r") + 1])
    with open(log, "a") as f:
        f.write(json.dumps(rec) + "\n")
    if "--broken" in opts:
        return 1
    if args[:1] in (["-V"], ["--version"]):
        print("Python " + ver)
        return 0
    if args[:1] == ["-c"]:
        class VersionInfo(tuple):
            major = property(lambda s: s[0])
            minor = property(lambda s: s[1])
            micro = property(lambda s: s[2])
        sys.version_info = VersionInfo(tuple(int(x) for x in ver.split(".")) + ("final", 0))
        exec(compile(args[1], "<-c>", "exec"), {"__name__": "__main__"})
        return 0
    if args[:2] == ["-m", "venv"]:
        how = ctl.get("venv", "ok")
        if how == "fail":
            print("Error: the HIL stand-in venv failed")
            return 1
        if how == "ok":
            d = os.path.join(args[2], "bin")
            os.makedirs(d, exist_ok=True)
            for exe in ("python3", "python"):
                wrapper(os.path.join(d, exe), ver, ".venv/bin/" + exe, log, ctl_path, ["--venv"])
            with open(os.path.join(args[2], "pyvenv.cfg"), "w") as f:
                f.write("version = " + ver + "\n")
        return 0                                    # "silent": exits 0 having made nothing
    if args[:2] == ["-m", "pip"]:
        return int(ctl.get("pip_rc", 0))
    script = (args[0] if args else "").replace("\\", "/")
    if script == "tools/fetch_webui.py":
        if ctl.get("fetch") == "ok":
            os.makedirs(os.path.join("src", "webui"), exist_ok=True)
            with open(os.path.join("src", "webui", "index.html"), "w") as f:
                f.write("<!-- HIL stand-in bundle -->\n")
            print("HIL stand-in fetch_webui: fetched")
            return 0
        print("HIL stand-in fetch_webui: github.io is not reachable")
        return 1
    if script == "src/app.py":
        print("HIL stand-in app.py: started")
        return int(ctl.get("app_rc", 0))
    print("HIL stand-in python: a call it does not know")
    return 97


if __name__ == "__main__":
    sys.exit(main())
'''

# What each macOS launcher prints at its decisions (None: it has no such line).
_LAUNCHER_WORDS = {
    "Intellex.command": {"first": "First run: setting up. This takes a minute.",
                         "no_python": "No usable python3 was found (3.9 or newer is required).",
                         "no_venv": "Could not create a virtual environment in .venv",
                         "gave_up": "Intellex could not start (exit 1)."},
    "scripts/run-macos.command": {"first": "First run: creating the virtual environment...",
                                  "no_python": "No usable python3 found (3.9+).",
                                  "no_venv": None, "gave_up": None},
}
_LAUNCH_FILES = ("Intellex.command", "Intellex.bat", "README.md", "requirements.txt", "scripts/run-macos.command",
                 "scripts/run-windows.bat", "src/app.py", "tools/fetch_webui.py")
_APP_ARGS = ["--browser", "--port", "18999", "HIL two words"]
_C = "<-c>"
_VENV_PY = ".venv/bin/python3"


def _launch_tree(idir, dst, webui=None, venv=None, fake=None, log=None, ctl=None):
    """A scratch copy of what the launchers touch in Intellex at `idir` (modes kept, so a .command runs by its path):
    the launchers, README, requirements.txt, src/app.py and tools/fetch_webui.py - the stand-in never runs the last
    two, so the rest of src/ is not consulted. No .venv and no src/webui unless asked: webui 'empty' makes src/webui/
    with a subdirectory and no index.html, 'bundled' adds the index.html; venv '<x.y.z>' makes a stand-in venv of that
    version."""
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    for rel in _LAUNCH_FILES:
        p = os.path.join(dst, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        shutil.copy2(os.path.join(idir, *rel.split("/")), p)
    if webui:
        os.makedirs(os.path.join(dst, "src", "webui", "cmdlib"))
        if webui == "bundled":
            with open(os.path.join(dst, "src", "webui", "index.html"), "w", encoding="utf-8") as f:
                f.write("<!-- HIL bundle -->\n")
    if venv:
        d = os.path.join(dst, ".venv", "bin")
        os.makedirs(d)
        for exe in ("python3", "python"):
            _fake_exe(os.path.join(d, exe), fake, venv, ".venv/bin/" + exe, log, ctl, ["--venv"])
    return dst


def _fake_exe(path, fake, ver, name, log, ctl, flags=()):
    """A sh wrapper at `path` running the stand-in as Python `ver` (the same form _FAKE_PY's wrapper() writes)."""
    import shlex
    argv = [sys.executable, "-I", fake, "--as", ver, "--name", name, "--log", log, "--ctl", ctl] + list(flags) + ["--"]
    with open(path, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\nexec " + " ".join(shlex.quote(x) for x in argv) + ' "$@"\n')
    os.chmod(path, 0o755)


def _run_launcher(work, tree, launcher, pythons, ctl, fake):
    """Run tree/<launcher> by its path from `work` (not its own directory), stdin not a terminal, PATH holding only the
    stand-in interpreters in `pythons` ({name: version, or 'broken'}) and dirname -> (exit code, output, calls), each
    call (exe, argv with any -c snippet as '<-c>', cwd, the record)."""
    bindir = os.path.join(work, "bin")
    if os.path.isdir(bindir):
        shutil.rmtree(bindir)
    os.makedirs(bindir)
    log = os.path.join(work, "calls.jsonl")
    ctl_path = os.path.join(work, "hil_launch_ctl.json")
    with open(ctl_path, "w", encoding="utf-8") as f:
        json.dump(ctl, f)
    if os.path.exists(log):
        os.remove(log)
    for name, ver in pythons.items():
        _fake_exe(os.path.join(bindir, name), fake, "0.0.0" if ver == "broken" else ver, name, log, ctl_path,
                  ["--broken"] if ver == "broken" else [])
    os.symlink(shutil.which("dirname") or "/usr/bin/dirname", os.path.join(bindir, "dirname"))
    env = {"PATH": bindir, "HOME": work, "TMPDIR": work, "LC_ALL": "C", "INTELLEX_OFFLINE": "1",
           "INTELLEX_DATA_DIR": os.path.join(work, "appdata")}
    try:
        p = subprocess.run([os.path.join(tree, *launcher.split("/"))] + _APP_ARGS, cwd=work, env=env,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
        rc, out = p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        rc, out = None, "(still running after 120 s - killed)"
    calls = []
    if os.path.exists(log):
        with open(log, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                argv = list(r["argv"])
                if argv[:1] == ["-c"]:
                    argv[1:2] = [_C]
                calls.append((r["exe"], tuple(argv), r["cwd"], r))
    return rc, out, calls


def _launch_problems(idir, root, note=lambda s: None):
    """Every launcher check against Intellex at `idir`, in scratch directories under `root` -> [problem]. On Windows
    only the static .bat checks run (note() says so)."""
    problems = []
    fake = os.path.join(root, "hil_fake_python.py")
    os.makedirs(root, exist_ok=True)
    with open(fake, "w", encoding="utf-8") as f:
        f.write(_FAKE_PY)
    problems += _bat_problems(idir, fake, root)
    readme = open(os.path.join(idir, "README.md"), encoding="utf-8").read()
    if "xattr -d com.apple.quarantine Intellex.command" not in readme:
        problems.append("README.md no longer explains a quarantined Intellex.command (xattr -d com.apple.quarantine)")
    try:
        modes = subprocess.run(["git", "-C", idir, "ls-files", "-s", "--", "*.command"], capture_output=True,
                               text=True, timeout=30).stdout.split("\n")
        bad = [m.split()[-1] for m in modes if m.strip() and not m.startswith("100755")]
        if bad:
            problems.append(f"committed without the exec bit (git index mode not 100755): {bad}")
    except (OSError, subprocess.SubprocessError) as e:
        note(f"the .command files' git modes not checked: {e}")
    if os.name == "nt" or not os.path.exists("/bin/bash"):
        note("the .command launchers are not run here (no /bin/bash); the .bat checks are static")
        return problems
    for launcher, words in _LAUNCHER_WORDS.items():
        problems += [f"{launcher}: {p}" for p in _command_problems(idir, root, launcher, words, fake)]
    return problems


def _command_problems(idir, root, launcher, words, fake):
    """One macOS launcher through first run, every run, the 3.11 note, the fetch-on-empty path and its refusals."""
    problems = []
    base = os.path.join(root, launcher.replace("/", "_").replace(".", "_"))
    app = (_VENV_PY, ("src/app.py",) + tuple(_APP_ARGS))
    pip = [(_VENV_PY, ("-m", "pip", "install", "--quiet", "--upgrade", "pip")),
           (_VENV_PY, ("-m", "pip", "install", "--quiet", "-r", "requirements.txt"))]

    def run(tag, pythons, ctl, **tree_kw):
        work = os.path.join(base, tag)
        tree = os.path.join(work, "Intellex")
        if tree_kw.get("reuse"):
            tree_kw.pop("reuse")
        else:
            os.makedirs(work, exist_ok=True)
            _launch_tree(idir, tree, fake=fake, log=os.path.join(work, "calls.jsonl"),
                         ctl=os.path.join(work, "hil_launch_ctl.json"), **tree_kw)
        rc, out, calls = _run_launcher(work, tree, launcher, pythons, ctl, fake)
        return tree, rc, out, calls

    def expect(label, tree, rc, out, calls, want_rc, want_calls, has=(), lacks=()):
        got = [(c[0], c[1]) for c in calls]
        if rc != want_rc:
            problems.append(f"{label}: exited {rc}, expected {want_rc}; it printed {out.strip()[-300:]!r}")
        if got != want_calls:
            problems.append(f"{label}: the calls were {got}, expected {want_calls}")
        wrong_cwd = sorted({c[2] for c in calls if os.path.realpath(c[2]) != os.path.realpath(tree)})
        if wrong_cwd:
            problems.append(f"{label}: a call ran in {wrong_cwd}, not the launcher's own tree")
        for s in has:
            if s and s not in out:
                problems.append(f"{label}: {s!r} not printed (it printed {out.strip()[-300:]!r})")
        for s in lacks:
            if s and s in out:
                problems.append(f"{label}: {s!r} printed")
        reqs = [c[3].get("requirements_found") for c in calls if "-r" in c[1]]
        if reqs and reqs != [True]:
            problems.append(f"{label}: pip -r requirements.txt ran where there is no requirements.txt")

    note_310 = ("Note: this venv is Python 3.10. Intellex is developed on 3.14 and only", "3.11+ is exercised")
    fetching = "Fetching the config tool -- it is not in the repo, see README."
    soft = "Could not fetch it. Intellex will start and explain."
    started = "HIL stand-in app.py: started"

    # First run: the newest python3 that answers (python3.12 is broken, python3 older), a venv, its dependencies, no
    # note at 3.11, the config tool fetched because src/webui is absent, then app.py with every argument.
    py = {"python3.12": "broken", "python3.11": "3.11.9", "python3": "3.9.6"}
    tree, rc, out, calls = run("first", py, {"fetch": "ok"})
    bindir = os.path.join(base, "first", "bin")
    expect("first run", tree, rc, out, calls, 0,
           [("python3.12", ("-c", _C)), ("python3.11", ("-c", _C)), ("python3.11", ("-V",)),
            ("python3.11", ("-m", "venv", ".venv"))] + pip
           + [(_VENV_PY, ("-c", _C)), (_VENV_PY, ("tools/fetch_webui.py",)), app],
           has=(words["first"], f"Using {os.path.join(bindir, 'python3.11')} (Python 3.11.9)", fetching, started),
           lacks=("Note: this venv is Python", soft))
    if not os.path.isfile(os.path.join(tree, "src", "webui", "index.html")):
        problems.append("first run: no src/webui/index.html after the fetch")

    # Every later run: no setup and no fetch, only the version note's check and app.py.
    tree, rc, out, calls = run("first", py, {"fetch": "ok"}, reuse=True)
    expect("second run", tree, rc, out, calls, 0, [(_VENV_PY, ("-c", _C)), app], has=(started,),
           lacks=(words["first"], fetching, "Note: this venv is Python"))

    # A venv older than 3.11: the note, at setup and again on a later run; the app still starts.
    py10 = {"python3.10": "3.10.14", "python3": "3.9.6"}
    tree, rc, out, calls = run("py310", py10, {"fetch": "ok"})
    expect("first run on 3.10", tree, rc, out, calls, 0,
           [("python3.10", ("-c", _C)), ("python3.10", ("-V",)), ("python3.10", ("-m", "venv", ".venv"))] + pip
           + [(_VENV_PY, ("-c", _C)), (_VENV_PY, ("tools/fetch_webui.py",)), app], has=note_310 + (started,))
    tree, rc, out, calls = run("py310", py10, {"fetch": "ok"}, reuse=True)
    expect("a later run on 3.10", tree, rc, out, calls, 0, [(_VENV_PY, ("-c", _C)), app], has=note_310 + (started,),
           lacks=(words["first"],))

    # src/webui present but without index.html, and the fetch failing: never fatal.
    tree, rc, out, calls = run("emptyui", {"python3.11": "3.11.9"}, {"fetch": "fail"}, webui="empty", venv="3.11.9")
    expect("an empty src/webui with the fetch failing", tree, rc, out, calls, 0,
           [(_VENV_PY, ("-c", _C)), (_VENV_PY, ("tools/fetch_webui.py",)), app], has=(fetching, soft, started),
           lacks=(words["first"],))

    # No usable python3: nothing made, nothing started.
    tree, rc, out, calls = run("nopython", {"python3": "3.8.18"}, {})
    expect("only a Python 3.8", tree, rc, out, calls, 1, [("python3", ("-c", _C))],
           has=(words["first"], words["no_python"], words["gave_up"]), lacks=(started,))
    if os.path.exists(os.path.join(tree, ".venv")):
        problems.append("only a Python 3.8: a .venv was made")

    # venv creation failing, or exiting 0 without making an interpreter; pip failing. The app never starts.
    make = [("python3.11", ("-c", _C)), ("python3.11", ("-V",)), ("python3.11", ("-m", "venv", ".venv"))]
    tree, rc, out, calls = run("venvfail", {"python3.11": "3.11.9"}, {"venv": "fail"})
    expect("python -m venv failing", tree, rc, out, calls, 1, make, has=(words["gave_up"],), lacks=(started,))
    tree, rc, out, calls = run("venvsilent", {"python3.11": "3.11.9"}, {"venv": "silent"})
    if words["no_venv"]:
        expect("a venv with no interpreter in it", tree, rc, out, calls, 1, make,
               has=(words["no_venv"], words["gave_up"]), lacks=(started,))
    elif rc in (0, None) or any(c[1][:1] == ("src/app.py",) for c in calls):
        problems.append(f"a venv with no interpreter in it: exited {rc} with calls {[(c[0], c[1]) for c in calls]}; "
                        f"expected a failure before app.py")
    tree, rc, out, calls = run("pipfail", {"python3.11": "3.11.9"}, {"pip_rc": 1})
    expect("pip failing", tree, rc, out, calls, 1, make + pip[:1], has=(words["gave_up"],), lacks=(started,))
    return problems


# The Windows launchers' decisions, in order, as their commands read with REM lines dropped and spaces collapsed:
# (what, the command's pattern). Each is looked for after the previous one's line.
_BAT_STEPS = (
    ("the venv's interpreter is .venv\\Scripts\\python.exe", r'^set PY=\.venv\\Scripts\\python\.exe$'),
    ("first run is decided by that interpreter being absent", r'^if not exist "%PY%" \($'),
    ("the py launcher is looked for", r'^where py >nul 2>&1$'),
    ("the venv is made with py -3 (the newest 3.x), else python",
     r'^if %ERRORLEVEL%==0 \( py -3 -m venv \.venv \) else \( python -m venv \.venv \)$'),
    ("a venv with no interpreter is checked for", r'^if not exist "%PY%" \($'),
    ("... and refused", r'^exit /b 1$'),
    ("pip is upgraded in the venv", r'^"%PY%" -m pip install --quiet --upgrade pip$'),
    ("requirements.txt is installed in the venv", r'^"%PY%" -m pip install --quiet -r requirements\.txt$'),
    ("a failed install is checked for", r'^if errorlevel 1 \($'),
    ("... and refused", r'^exit /b 1$'),
    ("the venv's version is asked on every run", r'^"%PY%" -c "(?P<code>[^"]+)" >nul 2>&1$'),
    ("... and a failing answer", r'^if errorlevel 1 \($'),
    ("... notes it is older than 3.11", r'^echo Note: this venv is older than Python 3\.11\. '),
    ("the config tool is fetched when src\\webui\\index.html is absent", r'^if not exist "src\\webui\\index\.html" \($'),
    ("... with tools\\fetch_webui.py and no arguments", r'^"%PY%" tools\\fetch_webui\.py$'),
    ("... a failed fetch only noted", r'^if errorlevel 1 echo Could not fetch it\. Intellex will start and explain\.$'),
    ("app.py is started with every argument", r'src\\app\.py %\*$'),
)


def _bat_problems(idir, fake, root):
    """Intellex.bat and scripts/run-windows.bat, read: the decisions _BAT_STEPS lists, in that order; no exit inside
    the fetch block (a failed fetch is never fatal); and the version check's own -c snippet run by the stand-in Python
    at 3.10.14 and 3.11.0, which must fail and pass - the note shows below 3.11 exactly, as in the .command launchers."""
    problems = []
    for rel in ("Intellex.bat", "scripts/run-windows.bat"):
        with open(os.path.join(idir, *rel.split("/")), encoding="utf-8", errors="replace") as f:
            cmds = [(i, " ".join(line.split())) for i, line in enumerate(f.read().splitlines(), 1)]
        cmds = [(i, c) for i, c in cmds if c and not re.match(r"(?i)^(rem\b|::)", c)]
        at, code, fetch_at = 0, None, None
        for what, rx in _BAT_STEPS:
            hit = next(((k, m) for k, (i, c) in enumerate(cmds) if k >= at for m in [re.search(rx, c, re.I)] if m),
                       None)
            if hit is None:
                problems.append(f"{rel}: no command after line {cmds[at - 1][0] if at else 0} where {what} "
                                f"(/{rx}/)")
                continue
            k, m = hit
            at = k + 1
            if "code" in m.groupdict():
                code = m.group("code")
            if what.startswith("the config tool is fetched"):
                fetch_at = k
        if fetch_at is not None:
            for i, c in cmds[fetch_at + 1:]:
                if c == ")":
                    break
                if re.search(r"(?i)(^|[\s(])(exit|goto)\b", c):
                    problems.append(f"{rel}:{i}: '{c}' inside the fetch block - a failed fetch must not stop the app")
        if code is not None:
            for ver, want in (("3.10.14", 1), ("3.11.0", 0)):
                p = subprocess.run([sys.executable, "-I", fake, "--as", ver, "--name", "python.exe", "--log",
                                    os.path.join(root, "bat_calls.jsonl"), "--ctl", os.path.join(root, "none.json"),
                                    "--", "-c", code], capture_output=True, text=True, timeout=30)
                if p.returncode != want:
                    problems.append(f"{rel}: its version check {code!r} exits {p.returncode} on Python {ver}, expected "
                                    f"{want} (the note shows below 3.11 exactly)")
    return problems


@test("intellex.launch_scripts", "Intellex.command and scripts/run-macos.command on a scratch copy with stand-in "
      "Pythons: first run makes the venv from the newest python3 that answers and installs requirements.txt in it, "
      "notes a venv older than 3.11 on every run, fetches the config tool when src/webui has no index.html (a failed "
      "fetch never fatal), and execs app.py with every argument; no usable python3, a failed venv or pip stops before "
      "the app. Intellex.bat and run-windows.bat read for the same decisions in order, their 3.11 check run "
      "(IX re-scan launch.scripts)")
def launch_scripts(bench):
    """Intellex Intellex.command (:25-120), scripts/run-macos.command (:11-80), Intellex.bat (:17-79),
    scripts/run-windows.bat (:14-68). Each .command runs by its path, from another directory, on a fresh scratch copy
    per case (_launch_tree), with PATH holding only stand-in interpreters (_FAKE_PY) and dirname: nothing is installed
    from PyPI, nothing is fetched, app.py never runs, and Greg's .venv and data directory are not touched. The calls
    each stand-in records are the launcher's decisions: which interpreter, when the venv is made, pip, the version
    check, the fetch and its arguments, app.py's arguments. A .bat cannot run here: _bat_problems reads both for the
    same decisions in the same order (_BAT_STEPS) and runs their version check's own snippet; `py -3` choosing the
    newest Python is the py launcher's documented behaviour, not checked. The README's quarantine note and the
    .command files' exec bit in git are checked too."""
    require(bench)
    idir = intellex_dir(bench)
    root = os.path.join(bench.out_dir or tempfile.mkdtemp(prefix="intellex-"), STAGE_DIR, "launch_scripts")
    if os.path.isdir(root):
        shutil.rmtree(root)
    problems = _launch_problems(idir, root, note=lambda s: bench.note(f"intellex.launch_scripts: {s}"))
    assert not problems, "; ".join(problems)
