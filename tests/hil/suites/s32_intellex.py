"""Intellex under test (IX-WP2..IX-WP4, docs/HIL_TESTING.md §10): a staged copy of Intellex per test, its host started
on a free port with the leash on (offline, no serial port, no discovery host unless a test names them), then HTTP,
WebSocket and Playwright checks against it. Nothing in this first set needs a board: they run while a bench run holds
every COM port, because the leash keeps the host off all of them.

Intellex serves the NaviCore config tool at / and the WCB Wizard at /wcb/Wizard/, and bridges each page's /_link
WebSocket to one transport. Its own no-hardware smoke tools run here too, one harness test each, against the staged
copy - so one report covers them and a regression in either repo shows up in the same place.
"""
import glob
import json
import os
import shutil
import subprocess

from hil.intellex import (IntellexHost, require, run_intellex_test, run_venv, stage)
from hil.runner import Skip, test
from hil.ws import WsClient

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


# ------------------------------------------------------------------ IX-WP4: the pages, in a browser
@test("intellex.ui_tools_load", "Both tools load through Intellex with nothing attached: no page error, no failed "
      "request, the shim backs navigator.serial and says there is no transport (Playwright; IX-WP4)")
def ui_tools_load(bench):
    run_intellex_test(bench, "intellex.ui_tools_load")
