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
import subprocess
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
@test("intellex.stage_selftest", "A staged Intellex host starts offline and leashed: /_api/status answers, no serial port "
      "is listed, its log lands in its own stage, and nothing is written to the real %LOCALAPPDATA%\\Intellex (IX-WP2)")
def stage_selftest(bench):
    require(bench)
    real = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Intellex")
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
