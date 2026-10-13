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
import urllib.parse

from hil.intellex import (IntellexHost, require, run_intellex_py, run_intellex_test, run_venv, seed_firmware,
                          seed_wiki, stage)
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
      "script, and are otherwise the bundled file byte for byte, uncached; /wcb/Wizard redirects; /_intellex.js opens "
      "with the branch prelude; with no bundle each tool route answers 503 (IX-WP3)")
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
            # _inject_shim inserts the tag and a newline before the first <script> (Intellex host.py).
            if html.replace(SHIM_TAG + chr(10), "", 1) != on_disk:
                problems.append(f"{path}: the served page differs from {os.path.relpath(disk, sd)} beyond the shim tag")
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
      "port (400), refuses a port outside INTELLEX_SERIAL_ALLOW (502) while keeping the target until detach, drops an "
      "out-of-range relayId and an unknown role; open-window refuses a remote URL and has no window hook (IX-WP3)")
def attach_validation(bench):
    require(bench)
    sd = stage(bench, "intellex.attach_validation")
    problems = []
    with IntellexHost(bench, sd) as host:
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
        # A ws target on a port nothing listens on: refused fast, and the role/relayId rules are visible in /_api/status.
        for req, role, rid in (({"kind": "ws", "host": "127.0.0.1", "role": "evil", "relayId": 25}, "", None),
                               ({"kind": "ws", "host": "127.0.0.1", "role": "relay", "relayId": 19}, "relay", 19)):
            st, body = host.json("POST", "/_api/attach", req, timeout=20)
            st2, stat = host.json("GET", "/_api/status")
            if st == 200:
                problems.append(f"attach {req} succeeded on a closed port")
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


@test("intellex.ghget_retry_unit", "flash._get against a local HTTP server: 429, 403 and 503 are retried after the "
      "Retry-After wait, a 404 is final at once, a refused connection fails fast (IX-WP3)")
def ghget_retry_unit(bench):
    """tests/intellex/py/ghget_retry.py: Intellex src/flash.py:159-184 (no_network) and :223-263 (_get). No GitHub."""
    run_intellex_py(bench, "intellex.ghget_retry_unit", "ghget_retry.py")


@test("intellex.paths_logs_unit", "Where Intellex keeps things: a frozen build's user copy only when non-empty, wikis per "
      "subdirectory, updates in the user directory, the NaviLink move done once and never merged; the log prune across "
      "both prefixes by timestamp; winsize.fit clamping and centring; certs.is_cert_error on a buried failure (IX-WP3)")
def paths_logs_unit(bench):
    """tests/intellex/py/paths_logs.py, in a scratch directory inside the stage (LOCALAPPDATA re-pointed per case)."""
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
      "target is the one attached (in-process fakes; IX re-scan host.bridge.superseded_attach)")
def bridge_superseded_attach(bench):
    """tests/intellex/py/bridge_units.py group superseded: Intellex src/host.py Bridge.attach/detach (:214-251), whose
    generation count is what stops a slow open() from leaking a transport nothing can close (a held COM port with its
    reader still feeding bytes). A transport whose open() waits on an Event stands in for the slow port or handshake;
    the HTTP case runs Intellex's real build_app() in the venv process, its rebuild() handing out such transports."""
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
        with IntellexHost(bench, sd) as host:
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
        with IntellexHost(bench, sd) as host:
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
      "the fetcher's log; a failed tool update is 502 with what landed; firmware and docs report ok false with the "
      "error and what is cached; a frozen build re-invokes itself (stub fetchers; IX re-scan host.api.update_subprocesses)")
def update_routes(bench):
    """Intellex src/host.py api_update_webui (:912-965), api_update_firmware (:636-669), api_update_wiki (:828-854). The
    stage's tools/fetch_webui.py, fetch_firmware.py and fetch_wiki.py are replaced by _FETCH_STUB, which edits or writes
    stage files as a real fetch would (the bundled Wizard's UI_VERSION, the config tool's footer-dtg, a cached image, a
    wiki page) - so nothing reaches GitHub. The frozen re-invocation (--run-fetch-*) and app.py's handling of it are
    tests/intellex/py/host_units.py group update_frozen, run on the same stage. Pinned as it is today: a failed
    firmware or docs update still answers 200 (ok false), only the tool update answers 502."""
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
        if st != 502 or body.get("ok") is not False or "the Wizard fetch failed" not in body.get("error", "") \
                or body.get("version") != "HILNC-2" or body.get("wcbVersion") != "HILWIZ-1":
            problems.append(f"update-webui failing after the config tool landed: {st} {body}, expected 502 with the "
                            f"fetcher's words, version HILNC-2 (what landed) and wcbVersion HILWIZ-1")
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
                or nc.get("count") != 1:
            problems.append(f"update-firmware failing for WCB: {st} {body}, expected 200 ok false with the fetcher's "
                            f"words and the NaviCore set still reported cached")

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
        if st != 200 or body.get("ok") is not False or "not reachable" not in body.get("error", ""):
            problems.append(f"update-wiki failing: {st} {body}")
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
      "cooldown, and gives up after 3 failed bounces with one line; a move or a working bounce resets that; on-subnet "
      "routing, a serial link and --no-auto-bounce never bounce (in-process, discover stood in for; IX re-scan "
      "host.reconnect.auto_bounce)")
def reconnect_auto_bounce(bench):
    """tests/intellex/py/host_units.py group bounce: Intellex src/host.py reconnect_loop (:1810-1871) running in its real
    build_app() in the venv process, every reattach failing, with discover.local_ip_for, ssid_for_host, wifi_bounce and
    probe answered by the case - no adapter is touched and the host (192.0.2.1, TEST-NET-1) is never contacted. Every
    staged-host test runs with --no-auto-bounce; this is the path that flag turns off. BOUNCE_COOLDOWN_S is shortened
    to 2.5 s for the run."""
    run_intellex_py(bench, "intellex.reconnect_auto_bounce", "host_units.py", args={"group": "bounce"}, timeout=300)


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
            with IntellexHost(bench, sd, args=["--ws", "127.0.0.2", "--ssid", "HILCLI"]) as host:
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
