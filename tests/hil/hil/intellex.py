"""Run Intellex under test: a staged copy of it per test, its host started on a free port with a leash on, and then
Playwright specs (tests/intellex) or plain HTTP/WebSocket checks against it.

Intellex (github.com/greghulette/Intellex) is the desktop companion for NaviCore. One aiohttp process (src/host.py)
serves the NaviCore config tool at / and the WCB Wizard at /wcb/Wizard/, and bridges each page's /_link WebSocket to
ONE transport: a COM port or a droid's ws://<ip>/ws. See docs/HIL_TESTING.md §10 and Intellex's CLAUDE.md "A test host
needs a leash".

Why a staged copy: a host writes settings, logs, tool bundles and a firmware cache. Pointed at Greg's checkout and his
real %LOCALAPPDATA%, a test would change what he runs. So each test gets <out>/intellex/<test id>/ holding a copy of
Intellex's src/ and tools/, bundles seeded from the NaviCore and WCB working trees (so a Wizard or config-tool change is
tested inside Intellex before it ships), and its own data directory, appdata/Intellex: INTELLEX_DATA_DIR on every
platform (Intellex bd4f37d) and LOCALAPPDATA's parent for an older checkout on Windows. On a Mac LOCALAPPDATA reached
nothing, and the staged hosts wrote ~/Library/Application Support/Intellex (run 20261005-221308).

Why the leash: left alone a host PINGs every Espressif COM port (the SBUS controller resets when its port opens; W1 or
NaviCore may be mid-test), probes 192.168.4.1 over the PC's second WiFi adapter, and waits on a GitHub probe. The env
hooks in Intellex (INTELLEX_OFFLINE, INTELLEX_SERIAL_ALLOW, INTELLEX_DISCOVER_HOSTS; INTELLEX_DATA_DIR above) hold it to
what the test wants; every host here starts offline, with no serial port and no discovery host unless the test names them.

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
from contextlib import contextmanager

from .bridge import Bridge
from .runner import Skip
from .wizard import OWN_GROUP, _kill_tree, _outcomes, _reacquire, _wait_node
from .wlan import scrub
from .ws import WsClient, frame

REPO = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def main_checkout(repo):
    """The main checkout of this repo: `repo` itself, or - from a git worktree under <main>/.claude/worktrees/<name>,
    where the week's agents write and run tests - <main>."""
    parts = os.path.normpath(repo).split(os.sep)
    for i in range(len(parts) - 1):
        if parts[i] == ".claude" and parts[i + 1] == "worktrees" and i:
            return os.sep.join(parts[:i])
    return os.path.normpath(repo)


def github_dir(repo):
    """The folder holding this repo and its siblings (Intellex, NaviCore): the repo's parent, or - from a git worktree
    under <repo>/.claude/worktrees/<name>, where the week's agents write and run tests - the MAIN checkout's parent.
    Beside .claude/worktrees there is no Intellex, and every Intellex test skipped 'no Intellex checkout' there."""
    return os.path.dirname(main_checkout(repo))


def builds_dir():
    """tests/hil/results/builds, where the harness keeps the bench images (results/builds/FLASHED.md): this checkout's,
    or - from a git worktree, which has no results/ (gitignored) - the main checkout's, which the bench boards were
    flashed from."""
    here = os.path.join(REPO, "tests", "hil", "results", "builds")
    if os.path.isdir(here):
        return here
    return os.path.join(main_checkout(REPO), "tests", "hil", "results", "builds")


GITHUB = github_dir(REPO)
INTELLEX_TESTS = os.path.join(REPO, "tests", "intellex")
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
                             **OWN_GROUP).stdout.strip()
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
    # The sibling NaviCore checkout, or NAVICORE_REPO as tests/wizard/lib/navicore/paths.js reads it, so one setting
    # points the config tool's specs and Intellex's staged copy of the tool at the same tree.
    nav = os.environ.get("NAVICORE_REPO") or os.path.join(GITHUB, "NaviCore")
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
                              **OWN_GROUP).stdout.strip()
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
    offline: INTELLEX_OFFLINE (on by default: no GitHub, a 2 s start).
    hide: network names (SSIDs) to take out of every line the host prints before it is kept or logged: a host attached
    over WiFi records the SSID it asks Windows for (host.py api_attach, ssid_for_host) and names both networks when the
    adapter moves ('network changed "<a>" -> "<b>"', _reidentify_if_moved). A name is never logged (hil/wlan.py)."""

    def __init__(self, bench, stage_dir, allow_ports=(), discover_hosts=(), offline=True, env=None, args=(), hide=()):
        self.bench, self.stage = bench, stage_dir
        self.allow_ports, self.discover_hosts, self.offline = list(allow_ports), list(discover_hosts), offline
        self.extra_env, self.args = dict(env or {}), list(args)
        self.hide = tuple(h for h in hide if h)
        self.proc = self.url = None
        self.port = 0
        self.lines = []
        self._reader = None

    def start(self, timeout=30.0):
        py = intellex_python(self.bench)
        self.port = _free_port()
        env = {k: v for k, v in os.environ.items() if not k.startswith("INTELLEX_")}
        env.update(LOCALAPPDATA=os.path.join(self.stage, "appdata"), PYTHONUTF8="1",
                   INTELLEX_DATA_DIR=os.path.join(self.stage, "appdata", "Intellex"),
                   INTELLEX_SERIAL_ALLOW=",".join(self.allow_ports),
                   INTELLEX_DISCOVER_HOSTS=",".join(self.discover_hosts))
        if self.offline:
            env["INTELLEX_OFFLINE"] = "1"
        env.update(self.extra_env)
        cmd = [py, os.path.join(self.stage, "src", "host.py"), "--port", str(self.port), "--no-auto-bounce"] + self.args
        self.proc = subprocess.Popen(cmd, cwd=self.stage, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace", **OWN_GROUP)
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
            line = scrub(line.rstrip(), *self.hide)
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


def scrub_logs(folder, hide):
    """Every file under `folder` with each name in `hide` replaced (hil/wlan.py scrub), in place: the host's own log
    files tee what it prints, network names included."""
    names = tuple(h for h in hide if h)
    if not names or not os.path.isdir(folder):
        return
    for root, _, files in os.walk(folder):
        for f in files:
            p = os.path.join(root, f)
            try:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
                clean = scrub(text, *names)
                if clean != text:
                    with open(p, "w", encoding="utf-8") as fh:
                        fh.write(clean)
            except OSError:
                pass


def copy_logs(bench, stage_dir, test_id, hide=()):
    """The staged host's own log files, next to the run's report (they are in the stage, which may be deleted). hide:
    network names taken out of them first, in the stage and in the copy (IntellexHost hide)."""
    logs = os.path.join(stage_dir, "appdata", "Intellex", "logs")
    scrub_logs(logs, hide)
    if bench.out_dir and os.path.isdir(logs):
        dst = os.path.join(bench.out_dir, "intellex-logs", test_id)
        shutil.copytree(logs, dst, dirs_exist_ok=True)


# ------------------------------------------------------------------ Playwright and venv scripts
def run_intellex_test(bench, test_id, attach=None, device=None, tools="worktree", settings=None, allow_ports=None,
                      discover_hosts=(), offline=True, env=None, args=None, timeout=300.0, wiki=False,
                      link_check=None, recover=None, seed=None, hide=(), hooks=None):
    """Run the Playwright test titled `<test_id> ...` in tests/intellex against a staged host. device: a bench device
    whose port the host is given (released from the harness first, taken back and checked after). attach: the
    /_api/attach body, or None to leave the host unattached. wiki: seed the crafted wiki (seed_wiki). seed: seed(stage)
    runs once the stage exists, before the host starts (seed_firmware: a firmware cache). hide: network names the host's
    lines and logs never carry (IntellexHost hide, copy_logs). Raises AssertionError / Skip like run_wizard_test.

    hooks: {name: fn(host, body) -> reply}, bench actions the spec asks for mid-run through the bridge's /hook route
    (tests/intellex/lib/board.js hil.hook): the harness test's own thread is parked while Playwright runs, so a step
    the spec must sequence - move the PC's WiFi adapter, mark and count a console, judge the host's flash log - is a
    hook. One runs at a time, under the bridge's lock; a Skip it raises reaches the spec as {skip}.

    link_check (with `attach`): a raw /_link client of the harness's own ('rawLink', a LinkTap) reads every byte the host
    fans out for the whole Playwright run, and link_check(bytes) -> [problem] judges them afterwards - boot_check: the
    board printed no boot line, so nothing reset it. Its problems fail the test beside the spec's own. The bytes are
    never logged: through NaviCore they hold GET_CONFIG, through a WCB its ?backup.
    recover: recover(bench, device) -> what it did, called when `device` does not answer once the host has let it go
    (reset_into_app: a WCB a test left in its ROM loader). The test then fails, naming it; without `recover` the
    reacquire's own failure is raised, as before."""
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
    if wiki:
        seed_wiki(sd)
    if seed is not None:
        seed(sd)
    out_dir = bench.out_dir or tempfile.mkdtemp(prefix="intellex-")
    report_path = os.path.join(out_dir, f"{test_id}.playwright.json")
    if device:
        bench.close_device(device)
    host = IntellexHost(bench, sd, allow_ports=allow_ports, discover_hosts=discover_hosts, offline=offline, env=env,
                        hide=hide)
    bound = {name: (lambda body, f=f: f(host, body)) for name, f in (hooks or {}).items()}
    aborted = None
    proc = None
    tail = []
    rawlink = None
    link_problems, lost = [], None
    try:
        host.start()
        if attach:
            host.attach(attach)
            if link_check is not None:
                rawlink = LinkTap(host.port, name="rawLink")
        context = {"device": device, "args": args or {}, "intellex": {"url": host.url, "stage": sd, "target": attach},
                   "hooks": sorted(bound)}
        with Bridge(bench, context, **({"hooks": bound} if bound else {})) as bridge:
            penv = dict(os.environ, PLAYWRIGHT_JSON_OUTPUT_NAME=report_path, FORCE_COLOR="0", INTELLEX_URL=host.url,
                        INTELLEX_STAGE=sd, HIL_BRIDGE=bridge.url)
            import re as _re
            cmd = [node, cli, "test", "--reporter=line,json", "--grep", r"(^|\s)" + _re.escape(test_id) + r"(\s|$)"]
            bench.note(f"intellex: {test_id} at {host.url}" + (f", attached to {attach}" if attach else ""))
            proc = subprocess.Popen(cmd, cwd=INTELLEX_TESTS, env=penv, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                    **OWN_GROUP)
            tail, killed = _wait_node(bench, proc, timeout)
            if killed:
                raise AssertionError(f"{test_id}: Playwright still running after {timeout:.0f}s - killed")
    except BaseException as e:
        if not isinstance(e, Exception):
            aborted = e
        raise
    finally:
        if rawlink is not None:
            data = rawlink.snapshot()
            rawlink.close()
            try:
                link_problems = list(link_check(data) or [])
            except Exception as e:  # noqa: BLE001 - a checker's own bug must not hide the test's result
                link_problems = [f"the /_link check itself failed: {type(e).__name__}: {e}"]
            for p in link_problems:
                bench.note(f"{test_id}: rawLink: {p}")
        host.stop()
        copy_logs(bench, sd, test_id, tuple(hide))
        if device:
            try:
                _reacquire(bench, device)
            except AssertionError as e:
                if aborted is not None:
                    bench.note(f"{device} not reacquired after the abort: {e}")
                elif recover is None:
                    raise
                else:
                    try:
                        what = recover(bench, device)
                    except AssertionError as r:
                        what = f"and the recovery failed too: {r}"
                    lost = f"{device} did not answer once Intellex let it go ({e}); {what}"
                    bench.note(f"{test_id}: {lost}")
    extra = link_problems + ([lost] if lost else [])
    try:
        with open(report_path, encoding="utf-8") as f:
            outcomes = _outcomes(json.load(f))
    except (OSError, ValueError):
        raise AssertionError("\n".join([f"{test_id}: Playwright exited {proc.returncode if proc else '?'} with no "
                                        f"report:\n    " + "\n    ".join(tail)] + extra))
    if not outcomes:
        raise AssertionError("\n".join([f"{test_id}: no Playwright test is titled '{test_id} ...' in "
                                        f"tests/intellex/specs"] + extra))
    failed = [(t, m) for t, s, m in outcomes if s not in ("passed", "skipped")]
    if failed or extra:
        raise AssertionError("\n".join([f"{t}: {m}" for t, m in failed] + extra))
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
                       errors="replace", timeout=timeout, env=e, **OWN_GROUP)
    out = (p.stdout or "") + (p.stderr or "")
    for line in out.splitlines():
        if line.strip():
            bench.log("intellex", "<", line.rstrip())
    return p.returncode, out


# ------------------------------------------------------------------ seeded data: a crafted wiki, a firmware cache
FIXTURES = os.path.join(INTELLEX_TESTS, "fixtures")


def seed_wiki(stage_dir):
    """Copy tests/intellex/fixtures/wiki/<product>/ into the stage's src/wiki/, where a non-frozen host reads its wikis
    (Intellex paths.data_subdir). A crafted wiki, not Greg's downloaded one: its pages carry the links, images and
    script the viewer must rewrite or keep inert, and none of it is real documentation -> the directory."""
    dst = os.path.join(stage_dir, "src", "wiki")
    shutil.copytree(os.path.join(FIXTURES, "wiki"), dst, dirs_exist_ok=True)
    return dst


def fw_key(branch):
    """The cache directory name Intellex gives a branch (fwcache._key): '/' and '\\' flattened to '__', so 'feature/x'
    is one directory and the summary walk sees it."""
    return (branch or "main").replace("/", "__").replace("\\", "__")


FW_REPOS = {"wcb": ("greghulette", "Wireless_Communication_Board-WCB", "Code/bin"),
            "navicore": ("greghulette", "NaviCore", "firmware")}      # Intellex wcb_flash.py / flash.py GITHUB_*


def seed_firmware(stage_dir, product, branch, files):
    """A firmware set for (product, branch) where a non-frozen host keeps its cache (src/firmware/<product>/<branch>/,
    fwcache._dirs), with the listing.json both native flashers and the GitHub proxy fall back to offline. files:
    {name: bytes}. The listing has GitHub's contents shape and GitHub download URLs, as a cached one does -> the
    directory."""
    owner, repo, path = FW_REPOS[product]
    d = os.path.join(stage_dir, "src", "firmware", product, fw_key(branch))
    os.makedirs(d, exist_ok=True)
    listing = []
    for name, data in files.items():
        with open(os.path.join(d, name), "wb") as f:
            f.write(data)
        listing.append({"name": name, "path": f"{path}/{name}", "type": "file", "size": len(data),
                        "download_url": f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}/{name}"})
    with open(os.path.join(d, "listing.json"), "w", encoding="utf-8") as f:
        json.dump(listing, f)
    return d


# ------------------------------------------------------------------ scripts under Intellex's venv (tests/intellex/py)
PY_DIR = os.path.join(INTELLEX_TESTS, "py")


def py_outcome(report):
    """(failed, skipped, passed) from a tests/intellex/py results file - {"cases": [{"name", "status", "message"}],
    "notes": [...]}, status passed | failed | skipped; anything else counts as failed. Each list holds (name, message)."""
    out = {"passed": [], "failed": [], "skipped": []}
    for c in (report or {}).get("cases") or []:
        st = c.get("status")
        out[st if st in out else "failed"].append((c.get("name", "?"), c.get("message", "")))
    return out["failed"], out["skipped"], out["passed"]


def judge_py(test_id, report, rc, tail):
    """Raise for a venv script's result: AssertionError naming each failed case (a crash, a missing results file, no case
    at all or a non-zero exit with nothing failed are failures too), Skip when every case skipped."""
    if report is None:
        raise AssertionError(f"{test_id}: the venv script exited {rc} with no results file:\n    " + "\n    ".join(tail))
    failed, skipped, passed = py_outcome(report)
    if failed:
        raise AssertionError("\n".join(f"{n}: {m}" for n, m in failed))
    if rc not in (0, None):
        raise AssertionError(f"{test_id}: the venv script exited {rc} with no failed case:\n    " + "\n    ".join(tail))
    if not passed and not skipped:
        raise AssertionError(f"{test_id}: the venv script ran no case")
    if skipped and not passed:
        raise Skip(skipped[0][1] or f"{skipped[0][0]} skipped")


def run_intellex_py(bench, test_id, script, args=None, device=None, tools="none", stage_dir=None, env=None,
                    timeout=600.0, allow_ports=None):
    """Run tests/intellex/py/<script> under Intellex's venv against a staged Intellex (the script puts its src/ and tools/
    first on the import path), with `args` as JSON, and judge its results file (judge_py) -> the report. device: a bench
    device whose COM port the script gets as args["port"]; the harness releases it first and takes it back after,
    checking the board answers (handed_over). The script runs leashed: offline, no discovery host, LOCALAPPDATA in the
    stage, and INTELLEX_SERIAL_ALLOW naming that port alone (nothing without a device)."""
    require(bench)
    args = dict(args or {})
    sd = stage_dir or stage(bench, test_id, tools=tools)
    out_dir = bench.out_dir or tempfile.mkdtemp(prefix="intellex-")
    out = os.path.join(out_dir, f"{test_id}.py.json")
    if os.path.exists(out):
        os.remove(out)
    port = bench.cfg["devices"][device]["port"] if device else None
    if port:
        args.setdefault("port", port)
    if allow_ports is None:
        allow_ports = [port] if port else []
    e = {"LOCALAPPDATA": os.path.join(sd, "appdata"), "INTELLEX_DATA_DIR": os.path.join(sd, "appdata", "Intellex"),
         "INTELLEX_OFFLINE": "1",
         "INTELLEX_SERIAL_ALLOW": ",".join(allow_ports), "INTELLEX_DISCOVER_HOSTS": ""}
    e.update(env or {})
    argv = [os.path.join(PY_DIR, script), "--stage", sd, "--args", json.dumps(args), "--out", out]

    def go():
        try:
            return run_venv(bench, argv, cwd=sd, timeout=timeout, env=e)
        except subprocess.TimeoutExpired:
            raise AssertionError(f"{test_id}: {script} still running after {timeout:.0f} s - killed") from None

    if device:
        others = running_intellex()
        if others:
            raise Skip(f"another Intellex is running (PID {others[0][0]}) and may hold {port} - close it first")
        with handed_over(bench, device):
            rc, text = go()
    else:
        rc, text = go()
    try:
        with open(out, encoding="utf-8") as f:
            report = json.load(f)
    except (OSError, ValueError):
        report = None
    for n in (report or {}).get("notes") or []:
        bench.note(f"{test_id}: {n}")
    if report is not None:
        for name, msg in py_outcome(report)[1]:          # a case skipped beside others that passed shows only here
            bench.note(f"{test_id}: case skipped: {name}: {msg}")
    judge_py(test_id, report, rc, [x for x in text.splitlines() if x.strip()][-12:])
    return report


# ------------------------------------------------------------------ bench boards behind a host
@contextmanager
def handed_over(bench, device):
    """Release `device`'s COM port for Intellex (a host or a venv script) -> its name, and take it back afterwards,
    proving the board still answers (hil/wizard.py _reacquire: ?VERSION for a WCB, PING for NaviCore). A board that does
    not come back fails the test after the block's own failure, both named; after an abort (Ctrl+C) it is only noted,
    so the abort stays an abort."""
    port = bench.cfg["devices"][device]["port"]
    bench.close_device(device)
    failure = None
    try:
        yield port
    except BaseException as e:
        failure = e
        raise
    finally:
        try:
            _reacquire(bench, device)
        except AssertionError as e:
            if failure is None:
                raise
            if not isinstance(failure, Exception):
                bench.note(f"{device} not reacquired after the abort: {e}")
            else:
                raise AssertionError(f"{failure}\n{device} did not come back afterwards: {e}") from None


@contextmanager
def attached_host(bench, test_id, device, tools="none", attach=True):
    """A leashed host holding `device`'s COM port -> the IntellexHost: stage Intellex, release the port from the harness,
    start the host allowed that port alone, and attach it with POST /_api/attach (attach=False leaves it unattached).
    On the way out: detach, kill the host's process tree (a host left running reopens its target every second, forever:
    the COM11 incident), copy its log, then take the port back and check the board answers (handed_over)."""
    require(bench)
    others = running_intellex()
    if others:
        raise Skip(f"another Intellex is running (PID {others[0][0]}); a test host would contend for its ports - close "
                   f"it first")
    sd = stage(bench, test_id, tools=tools)
    port = bench.cfg["devices"][device]["port"]
    host = IntellexHost(bench, sd, allow_ports=[port])
    with handed_over(bench, device):
        try:
            host.start()
            if attach:
                host.attach({"kind": "serial", "port": port})
            yield host
        finally:
            host.stop()
            copy_logs(bench, sd, test_id)


class LinkTap:
    """One raw /_link client, read on its own thread: every byte the host fans out to a page, in arrival order - the
    oracle for what a tool receives. BINARY frames carry the transport's bytes; the host's own TEXT line (the ERROR
    reply to a failed write) is kept too, as UTF-8. send() writes one TEXT frame, terminator included, as the shim does
    per line. Never log what it holds: through NaviCore that includes GET_CONFIG and its passwords."""

    def __init__(self, port, origin=None, name="tap"):
        self.name = name
        self.ws = WsClient("127.0.0.1", port, "/_link", origin=origin)
        self.data = bytearray()
        self.frames = 0
        self.closed = None              # why the socket ended, once it has
        self._lock = threading.Lock()
        self._wlock = threading.Lock()  # a pong from the reader and a send from the test are both frames
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"link-{name}", daemon=True)
        self._thread.start()

    def _run(self):
        ws = self.ws
        while not self._stop.is_set():
            try:
                ws.sock.settimeout(0.5)
                op, payload = ws._frame()
            except socket.timeout:
                continue
            except (ConnectionError, OSError) as e:
                if not self._stop.is_set():
                    self.closed = f"{type(e).__name__}: {e}"
                return
            if op in (0x1, 0x2):
                with self._lock:
                    self.data += payload
                    self.frames += 1
            elif op == 0x9:             # aiohttp's heartbeat (30 s): answer it, or the host drops the page
                try:
                    with self._wlock:
                        ws.sock.sendall(frame(payload, opcode=0xA))
                except OSError:
                    pass
            elif op == 0x8:
                self.closed = "the host closed the socket"
                return

    def send(self, text):
        with self._wlock:
            self.ws.send_text(text)

    def snapshot(self):
        with self._lock:
            return bytes(self.data)

    def wait_for(self, needle, timeout=5.0, start=0):
        """The offset just past the first `needle` (bytes) at or after `start`; AssertionError on a timeout or a closed
        socket. The message quotes the needle (a test's own marker) and a byte count, never the stream."""
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                i = self.data.find(needle, start)
                n = len(self.data)
            if i >= 0:
                return i + len(needle)
            if self.closed is not None:
                raise AssertionError(f"{self.name}: /_link ended ({self.closed}) before {needle[:48]!r} arrived")
            if time.monotonic() >= deadline:
                raise AssertionError(f"{self.name}: no {needle[:48]!r} on /_link within {timeout:g} s ({n} bytes "
                                     f"so far)")
            time.sleep(0.02)

    def close(self):
        self._stop.set()
        self._thread.join(2)
        try:
            self.ws.close()
        except OSError:
            pass


def line_after(data, needle, start=0):
    """The offset just past the line holding `needle` (its '\\n' included), or -1 when the needle or the line's end is
    not there yet."""
    i = data.find(needle, start)
    if i < 0:
        return -1
    j = data.find(b"\n", i + len(needle))
    return -1 if j < 0 else j + 1


def line_start(data, needle, start=0):
    """The offset where the line holding the first `needle` at or after `start` begins, or -1."""
    i = data.find(needle, start)
    if i < 0:
        return -1
    return max(start, data.rfind(b"\n", 0, i) + 1)


def window(data, first, last):
    """The bytes strictly between the line holding `first` and the next line holding `last` -> bytes, or None when either
    is missing: the part of two taps' streams that must be byte-identical, whatever each saw before it connected."""
    a = line_after(data, first)
    if a < 0:
        return None
    b = line_start(data, last, a)
    return None if b < 0 else data[a:b]


def line_spans(data, needle):
    """[(start, end)] of every whole line holding `needle`, in order; end is just past its '\\n'. For a sync line that
    is not unique - NaviCore's PONG is the same every time - the k-th span in two taps is the same line, provided both
    were connected before the first one was asked for."""
    out, i = [], data.find(needle)
    while i >= 0:
        j = data.find(b"\n", i)
        if j < 0:
            break
        out.append((data.rfind(b"\n", 0, i) + 1, j + 1))
        i = data.find(needle, j + 1)
    return out


# What a board prints as it starts, as it reaches a transport or a /_link: a classic ESP32 WCB's ROM reset line, then
# setup()'s first and last lines (hil/wcb.py BOOT_LINE, WCB.wait_boot); NaviCore's ROM line, banner and reset reason
# (suites/s46_navicore_boot.py BANNER_START, hil/navicore.py parse_boot). The ROM's 'ets <date>' banner is left out: its
# date differs between ESP32 silicon revisions.
WCB_BOOT_MARKERS = ("rst:0x", "Booting up the Wireless Communication Board", "Raw Serial Forwarding Task Created")
NAVICORE_BOOT_MARKERS = ("ESP-ROM:", "=== NaviCore ===", "Reset reason:")


def boot_markers_in(data, markers):
    """The boot markers present in `data` (bytes), in the order given."""
    return [m for m in markers if m.encode() in data]


def boot_check(markers, who, boots_of=()):
    """A run_intellex_test link_check -> check(bytes) -> [problem]: `who` printed one of `markers` through the link (it
    restarted), or - through a WCB's console - '[ETM] WCB<n> came ONLINE (boot)' for a board n in `boots_of`, the line a
    WCB prints for each boot announce it hears (suites/s33_intellex_bench.py _boot_edges): that board restarted. Only
    the markers are quoted, never the stream."""
    def check(data):
        out = []
        got = boot_markers_in(data, markers)
        if got:
            out.append(f"{who} printed {', '.join(repr(m) for m in got)} through the link: it restarted during the test")
        for n in boots_of:
            k = data.count(f"[ETM] WCB{n} came ONLINE (boot)".encode())
            if k:
                out.append(f"through {who}'s console, WCB{n} was heard booting {k} time(s) during the test")
        return out
    return check


# What the ESP32 ROM prints when a reset samples GPIO0 low (a download-mode boot), and what a WCB prints once setup() has
# finished: after a restart, the one tells a board left in its ROM loader from one back in its app.
ROM_LOADER_MARKERS = ("DOWNLOAD_BOOT", "waiting for download")
WCB_READY = "Raw Serial Forwarding Task Created"


def reset_into_app(bench, device, timeout=25.0):
    """Reset the WCB `device` into its app through its CP210x/CH9102 auto-reset circuit, for a board a test left in its
    ROM loader -> what was done. RTS asserted with DTR released holds EN low with GPIO0 high, and releasing RTS boots the
    app: the pulse hil/serialdev.py usb_jtag_reset gives an S3, on the same two lines (a CH9102 sends each write as it is
    made). The harness opens its port with both lines low, so nothing here holds GPIO0 down. AssertionError when the
    board still does not answer ?VERSION."""
    from .serialdev import usb_jtag_reset
    from .wcb import WCB
    dev = bench.dev(device)
    mark = dev.mark()
    usb_jtag_reset(dev)
    try:
        WCB(dev).wait_boot(mark, timeout=timeout)
    except AssertionError as e:
        raise AssertionError(f"{device} still does not answer after an EN reset with GPIO0 high: {e}") from None
    bench.note(f"{device}: reset into its app with an EN pulse (RTS, DTR low); it answers again")
    return "the harness reset it into its app with an EN pulse (RTS, DTR low), and it answers again"
