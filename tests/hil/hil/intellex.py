"""Run Intellex under test: a staged copy of it per test, its host started on a free port with a leash on, and then
Playwright specs (tests/intellex) or plain HTTP/WebSocket checks against it.

Intellex (github.com/greghulette/Intellex) is the desktop companion for NaviCore. One aiohttp process (src/host.py)
serves the NaviCore config tool at / and the WCB Wizard at /wcb/Wizard/, and bridges each page's /_link WebSocket to
ONE transport: a COM port or a droid's ws://<ip>/ws. See docs/HIL_TESTING.md §10 and Intellex's CLAUDE.md "A test host
needs a leash".

Why a staged copy: a host writes settings, logs, tool bundles and a firmware cache. Pointed at Greg's checkout and his
real %LOCALAPPDATA%, a test would change what he runs. So each test gets <out>/intellex/<test id>/ holding a copy of
Intellex's src/ and tools/, bundles seeded from the NaviCore and WCB working trees (so a Wizard or config-tool change is
tested inside Intellex before it ships), and its own LOCALAPPDATA.

Why the leash: left alone a host PINGs every Espressif COM port (the SBUS controller resets when its port opens; W1 or
NaviCore may be mid-test), probes 192.168.4.1 over the PC's second WiFi adapter, and waits on a GitHub probe. The three
env hooks in Intellex (INTELLEX_OFFLINE, INTELLEX_SERIAL_ALLOW, INTELLEX_DISCOVER_HOSTS) hold it to what the test
wants; every host here starts offline, with no serial port and no discovery host unless the test names them.

The harness itself stays standard library plus pyserial: HTTP through urllib, WebSocket through hil/ws.py. Checks that
must import Intellex's own modules run as scripts under Intellex's venv (run_intellex_py).
"""
import ast
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

from .bridge import Bridge
from .runner import Skip
from .wizard import _kill_tree, _outcomes, _reacquire, _wait_node

REPO = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
GITHUB = os.path.dirname(REPO)
INTELLEX_TESTS = os.path.join(REPO, "tests", "intellex")
_NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
STAGE_DIR = "ixstage"      # <out>/ixstage/<test id>: a staged Intellex (running_intellex() tells ours apart by it)


def intellex_dir(bench):
    """The Intellex checkout: bench.json "intellex_dir", else a checkout beside this repo."""
    return os.path.normpath(bench.cfg.get("intellex_dir") or os.path.join(GITHUB, "Intellex"))


def intellex_python(bench):
    """Intellex's venv interpreter (aiohttp, websockets, esptool, pyserial...). Always `python.exe -m`, never the venv's
    .exe shims: the venv was renamed with the project and its shims point at the old path (Intellex plan finding 6)."""
    if bench.cfg.get("intellex_python"):
        return bench.cfg["intellex_python"]
    d = intellex_dir(bench)
    win = os.path.join(d, ".venv", "Scripts", "python.exe")
    return win if os.name == "nt" else os.path.join(d, ".venv", "bin", "python")


def require(bench):
    """Skip unless the Intellex checkout and its venv are there."""
    d, py = intellex_dir(bench), intellex_python(bench)
    if not os.path.isfile(os.path.join(d, "src", "host.py")):
        raise Skip(f"no Intellex checkout at {d} (bench.json \"intellex_dir\")")
    if not os.path.isfile(py):
        raise Skip(f"no Intellex venv interpreter at {py} (bench.json \"intellex_python\")")
    return d, py


def running_intellex():
    """[(pid, command line)] of Intellex instances NOT started by this harness: Greg's own app, or a host from a
    source checkout. One attached to NaviCore's AP made NaviCore re-arm W1's terminal once a second and held W1's
    reboots off (tracker #93); one attached to a COM port holds it. Staged hosts (a path under .../ixstage/<id>/src/)
    are ours. Windows only; elsewhere the check is skipped."""
    if os.name != "nt":
        return []
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'Intellex.exe' -or "
          "($_.CommandLine -match 'src[\\\\/](app|host)\\.py') } | Select-Object ProcessId,CommandLine | ConvertTo-Json")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=30,
                             creationflags=_NEW_GROUP).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return []
    if not out:
        return []
    rows = json.loads(out)
    rows = rows if isinstance(rows, list) else [rows]
    mine = os.sep + STAGE_DIR + os.sep
    return [(r["ProcessId"], r.get("CommandLine") or "") for r in rows
            if mine not in (r.get("CommandLine") or "").replace("/", os.sep)]


# ------------------------------------------------------------------ staging
def _bundle_lists(idir):
    """The file lists Intellex's own tools/fetch_webui.py publishes, read from its source so they cannot drift:
    ROOT_FILES, IMAGES, WCB_SUBDIR, WCB_FILES, WCB_VENDOR, WCB_IMAGES, CMDLIB_VENDORS, CMDLIB_EXTRA."""
    want = {"ROOT_FILES", "IMAGES", "WCB_SUBDIR", "WCB_FILES", "WCB_VENDOR", "WCB_IMAGES", "CMDLIB_VENDORS",
            "CMDLIB_EXTRA"}
    tree = ast.parse(open(os.path.join(idir, "tools", "fetch_webui.py"), encoding="utf-8").read())
    got = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in want:
                got[name] = ast.literal_eval(node.value)
    missing = want - set(got)
    if missing:
        raise AssertionError(f"Intellex tools/fetch_webui.py no longer defines {sorted(missing)}")
    return got


def _copy(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)


def seed_bundles(bench, src_dir, tools="worktree"):
    """Put the two tools where a non-frozen Intellex serves them from (src/webui, src/webui_wcb).
    worktree: NaviCore/config_tool and this repo's Wizard/, as fetch_webui.py would publish them (the default: tests
    the tools as they are now). shipped: Intellex's own bundles (what users of the current Intellex get). none: no
    bundle at all (the 503 pages)."""
    idir = intellex_dir(bench)
    if tools == "none":
        return
    if tools == "shipped":
        for name in ("webui", "webui_wcb"):
            if os.path.isdir(os.path.join(idir, "src", name)):
                shutil.copytree(os.path.join(idir, "src", name), os.path.join(src_dir, name))
        return
    if tools != "worktree":
        raise ValueError(f"tools must be worktree, shipped or none, not {tools!r}")
    lists = _bundle_lists(idir)
    nav = os.path.join(GITHUB, "NaviCore")
    web = os.path.join(src_dir, "webui")
    for f in lists["ROOT_FILES"]:
        _copy(os.path.join(nav, "config_tool", f), os.path.join(web, f))
    for vendor in lists["CMDLIB_VENDORS"]:
        shutil.copytree(os.path.join(nav, "config_tool", "cmdlib", vendor), os.path.join(web, "cmdlib", vendor))
    for f in lists["CMDLIB_EXTRA"]:
        p = os.path.join(nav, "config_tool", "cmdlib", f)
        if os.path.isfile(p):
            _copy(p, os.path.join(web, "cmdlib", f))
    for f in lists["IMAGES"]:
        _copy(os.path.join(nav, "Images", f), os.path.join(web, "Images", f))
    wcb = os.path.join(src_dir, "webui_wcb")
    for f in list(lists["WCB_FILES"]) + list(lists["WCB_VENDOR"]):
        _copy(os.path.join(REPO, "Wizard", f), os.path.join(wcb, lists["WCB_SUBDIR"], f))
    for f in lists["WCB_IMAGES"]:
        _copy(os.path.join(REPO, "Images", f), os.path.join(wcb, "Images", f))


def _stage_ignore(include_data):
    """What a stage leaves out of src/: the tool bundles (seeded separately), downloaded data (the wikis, the firmware
    cache), caches, the build stamp and stray zips. DIRECTORIES only for the data: src/wiki.html and src/wikidocs.py
    are source the host imports, and a 'wiki*' glob would drop them."""
    data = () if include_data else ("firmware",)

    def ignore(dirpath, names):
        out = set()
        for n in names:
            if os.path.isdir(os.path.join(dirpath, n)):
                wiki_data = n.startswith("wiki") and not include_data
                if n.startswith("webui") or n == "__pycache__" or n in data or wiki_data:
                    out.add(n)
            elif n == "build_stamp.py" or n.endswith(".zip"):
                out.add(n)
        return out
    return ignore


def stage(bench, test_id, tools="worktree", settings=None, include_data=False):
    """A fresh <out>/intellex/<test_id>/ holding src/ (bundles seeded per `tools`), tools/ and appdata/ -> its path.
    settings: a dict written as appdata/Intellex/settings.json (the branch per product, for example). include_data:
    copy Intellex's downloaded wikis and firmware cache too (read-only data some of its smoke tools render)."""
    idir = intellex_dir(bench)
    base = bench.out_dir or tempfile.mkdtemp(prefix="intellex-")
    # Short on purpose: <out>/ixstage/<id>/src/webui_wcb/Wizard/vendor/esptool-js/esptool-js-0.4.7.bundle.js must stay
    # under Windows' 260-character path limit, which a long results path plus "intellex.<id>" went past.
    d = os.path.join(base, STAGE_DIR, test_id.replace("intellex.", "", 1))
    if os.path.isdir(d):
        shutil.rmtree(d)
    shutil.copytree(os.path.join(idir, "src"), os.path.join(d, "src"), ignore=_stage_ignore(include_data))
    shutil.copytree(os.path.join(idir, "tools"), os.path.join(d, "tools"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    seed_bundles(bench, os.path.join(d, "src"), tools)
    appdata = os.path.join(d, "appdata")
    os.makedirs(os.path.join(appdata, "Intellex"), exist_ok=True)
    if settings is not None:
        with open(os.path.join(appdata, "Intellex", "settings.json"), "w", encoding="utf-8") as f:
            json.dump(settings, f)
    head = _git(idir, "rev-parse", "--short", "HEAD") or "?"
    dirty = "+dirty" if _git(idir, "status", "--porcelain", "--", "src", "tools") else ""
    bench.note(f"intellex: staged {head}{dirty} with {tools} tools at {d}")
    return d


def _git(repo, *args):
    try:
        return subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True, timeout=30,
                              creationflags=_NEW_GROUP).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------ the host
class IntellexHost:
    """One staged host process. start() waits for /_api/status; stop() detaches and kills its process tree - a host
    left running reopens its wanted target every second, forever (the COM11 incident).

    allow_ports: the COM ports it may touch (INTELLEX_SERIAL_ALLOW; none by default).
    discover_hosts: the hosts discovery may probe (INTELLEX_DISCOVER_HOSTS; none by default).
    offline: INTELLEX_OFFLINE (on by default: no GitHub, a 2 s start)."""

    def __init__(self, bench, stage_dir, allow_ports=(), discover_hosts=(), offline=True, env=None, args=()):
        self.bench, self.stage = bench, stage_dir
        self.allow_ports, self.discover_hosts, self.offline = list(allow_ports), list(discover_hosts), offline
        self.extra_env, self.args = dict(env or {}), list(args)
        self.proc = self.url = None
        self.port = 0
        self.lines = []
        self._reader = None

    def start(self, timeout=30.0):
        py = intellex_python(self.bench)
        self.port = _free_port()
        env = {k: v for k, v in os.environ.items() if not k.startswith("INTELLEX_")}
        env.update(LOCALAPPDATA=os.path.join(self.stage, "appdata"), PYTHONUTF8="1",
                   INTELLEX_SERIAL_ALLOW=",".join(self.allow_ports),
                   INTELLEX_DISCOVER_HOSTS=",".join(self.discover_hosts))
        if self.offline:
            env["INTELLEX_OFFLINE"] = "1"
        env.update(self.extra_env)
        cmd = [py, os.path.join(self.stage, "src", "host.py"), "--port", str(self.port), "--no-auto-bounce"] + self.args
        self.proc = subprocess.Popen(cmd, cwd=self.stage, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace", creationflags=_NEW_GROUP)
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()
        self.url = f"http://127.0.0.1:{self.port}"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError(f"the Intellex host exited {self.proc.returncode} while starting:\n    "
                                     + "\n    ".join(self.lines[-15:]))
            try:
                if self.http("GET", "/_api/status", timeout=2)[0] == 200:
                    return self
            except OSError:
                pass
            time.sleep(0.25)
        self.stop()
        raise AssertionError(f"the Intellex host did not answer /_api/status within {timeout:.0f} s")

    def _drain(self):
        for line in self.proc.stdout:
            line = line.rstrip()
            if line:
                self.lines.append(line)
                del self.lines[:-400]
                self.bench.log("intellex", "<", line)

    def http(self, method, path, body=None, headers=None, timeout=10.0):
        """(status, headers, body bytes); a 4xx/5xx is returned, not raised. body: bytes, or a dict sent as JSON."""
        data = None
        hdrs = dict(headers or {})
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        elif body is not None:
            data = body if isinstance(body, bytes) else str(body).encode()
        req = urllib.request.Request(self.url + path, data=data, headers=hdrs, method=method)
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(req, timeout=timeout) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers or {}), e.read()

    def json(self, method, path, body=None, headers=None, timeout=10.0):
        status, _, raw = self.http(method, path, body, headers, timeout)
        try:
            return status, json.loads(raw.decode("utf-8") or "null")
        except ValueError:
            return status, {"_raw": raw[:200].decode("utf-8", "replace")}

    def attach(self, spec, timeout=15.0):
        """POST /_api/attach; on failure detach (attach records the target first, and would keep retrying it)."""
        status, body = self.json("POST", "/_api/attach", spec, timeout=timeout)
        if status != 200 or not (isinstance(body, dict) and body.get("ok")):
            self.detach()
            raise AssertionError(f"Intellex could not attach {spec}: {status} {body}")
        return body

    def detach(self):
        try:
            self.json("POST", "/_api/detach", {}, timeout=5)
        except OSError:
            pass

    def stop(self):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.detach()
            _kill_tree(self.proc)
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                pass
        if self._reader:
            self._reader.join(3)
        self.proc = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Report a redirect as it is (a test checks /wcb/Wizard gets a 301), rather than following it."""
    def redirect_request(self, *a, **k):
        return None


def copy_logs(bench, stage_dir, test_id):
    """The staged host's own log files, next to the run's report (they are in the stage, which may be deleted)."""
    logs = os.path.join(stage_dir, "appdata", "Intellex", "logs")
    if bench.out_dir and os.path.isdir(logs):
        dst = os.path.join(bench.out_dir, "intellex-logs", test_id)
        shutil.copytree(logs, dst, dirs_exist_ok=True)


# ------------------------------------------------------------------ Playwright and venv scripts
def run_intellex_test(bench, test_id, attach=None, device=None, tools="worktree", settings=None, allow_ports=None,
                      discover_hosts=(), offline=True, env=None, args=None, timeout=300.0):
    """Run the Playwright test titled `<test_id> ...` in tests/intellex against a staged host. device: a bench device
    whose port the host is given (released from the harness first, taken back and checked after). attach: the
    /_api/attach body, or None to leave the host unattached. Raises AssertionError / Skip like run_wizard_test."""
    import shutil as _sh
    node = _sh.which("node")
    if not node:
        raise Skip("Node.js is not on PATH")
    cli = os.path.join(INTELLEX_TESTS, "node_modules", "@playwright", "test", "cli.js")
    if not os.path.isfile(cli):
        raise Skip(f"tests/intellex is not installed - in {INTELLEX_TESTS} run: npm install")
    require(bench)
    others = running_intellex()
    if others:
        raise Skip(f"another Intellex is running (PID {others[0][0]}); a test host would contend for its ports and "
                   f"the droid's AP - close it first")
    if allow_ports is None:
        allow_ports = [bench.cfg["devices"][device]["port"]] if device else []
    sd = stage(bench, test_id, tools=tools, settings=settings)
    out_dir = bench.out_dir or tempfile.mkdtemp(prefix="intellex-")
    report_path = os.path.join(out_dir, f"{test_id}.playwright.json")
    if device:
        bench.close_device(device)
    host = IntellexHost(bench, sd, allow_ports=allow_ports, discover_hosts=discover_hosts, offline=offline, env=env)
    aborted = None
    proc = None
    tail = []
    try:
        host.start()
        if attach:
            host.attach(attach)
        context = {"device": device, "args": args or {}, "intellex": {"url": host.url, "stage": sd, "target": attach}}
        with Bridge(bench, context) as bridge:
            penv = dict(os.environ, PLAYWRIGHT_JSON_OUTPUT_NAME=report_path, FORCE_COLOR="0", INTELLEX_URL=host.url,
                        INTELLEX_STAGE=sd, HIL_BRIDGE=bridge.url)
            import re as _re
            cmd = [node, cli, "test", "--reporter=line,json", "--grep", r"(^|\s)" + _re.escape(test_id) + r"(\s|$)"]
            bench.note(f"intellex: {test_id} at {host.url}" + (f", attached to {attach}" if attach else ""))
            proc = subprocess.Popen(cmd, cwd=INTELLEX_TESTS, env=penv, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                    creationflags=_NEW_GROUP)
            tail, killed = _wait_node(bench, proc, timeout)
            if killed:
                raise AssertionError(f"{test_id}: Playwright still running after {timeout:.0f}s - killed")
    except BaseException as e:
        if not isinstance(e, Exception):
            aborted = e
        raise
    finally:
        host.stop()
        copy_logs(bench, sd, test_id)
        if device:
            try:
                _reacquire(bench, device)
            except AssertionError as e:
                if aborted is None:
                    raise
                bench.note(f"{device} not reacquired after the abort: {e}")
    try:
        with open(report_path, encoding="utf-8") as f:
            outcomes = _outcomes(json.load(f))
    except (OSError, ValueError):
        raise AssertionError(f"{test_id}: Playwright exited {proc.returncode if proc else '?'} with no report:\n    "
                             + "\n    ".join(tail))
    if not outcomes:
        raise AssertionError(f"{test_id}: no Playwright test is titled '{test_id} ...' in tests/intellex/specs")
    failed = [(t, m) for t, s, m in outcomes if s not in ("passed", "skipped")]
    if failed:
        raise AssertionError("\n".join(f"{t}: {m}" for t, m in failed))
    skipped = [m for _, s, m in outcomes if s == "skipped"]
    if skipped and len(skipped) == len(outcomes):
        raise Skip(skipped[0] or "skipped by the Playwright test")


def run_venv(bench, argv, cwd, timeout=600.0, env=None):
    """Run a command under Intellex's venv interpreter, logging its output -> (returncode, output). Its own process
    group, so run.py's first Ctrl+C (pause after this test) does not reach it."""
    py = intellex_python(bench)
    e = {k: v for k, v in os.environ.items() if not k.startswith("INTELLEX_")}
    e.update(PYTHONUTF8="1")
    e.update(env or {})
    p = subprocess.run([py] + list(argv), cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, env=e, creationflags=_NEW_GROUP)
    out = (p.stdout or "") + (p.stderr or "")
    for line in out.splitlines():
        if line.strip():
            bench.log("intellex", "<", line.rstrip())
    return p.returncode, out
