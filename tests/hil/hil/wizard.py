"""Run one Wizard (Playwright) test from tests/wizard against a bench board, with the harness in charge.

The harness hands the board's COM port to Chrome, serves a Bridge for the browser test to reach the probes and the
other boards through, runs Playwright for the one test id, and takes the port back. Web Serial never
shows a COM number, so the browser test finds its board by the USB VID/PID the harness reads for that COM port —
and each device gets its own Chrome profile (tests/wizard/.profiles/<device>), holding only that board's grant,
because the bench's CP210x boards (wcb2, both probes) all report the same VID/PID.

The NaviCore config tool's specs (tests/wizard/specs/navicore, ids nctool.*) never use real Web Serial unattended: the
page gets a fake port. With no device it talks to an in-Node emulator; with pipe=True the harness keeps the device's
port and the fake port's lines go through the bridge's /serial routes (docs/hil_plan/NAVICORE.md §5.2). Their L3 specs
(nctool.webserial_*, attended) hand NaviCore's port to Chrome like any Wizard test.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time

from .bridge import Bridge
from .checkpoint import redact_text
from .runner import Skip
from .serialdev import ExpectTimeout
from .wcb import WCB

# A bridged fragment envelope anywhere in a line: NaviCore's (rc_telemetry.h:513, {"f":N,"of":M,"sid":S,"s":"..."}) as
# a WCB prints it on USB, or the config tool's own after ;w<n>, (sendJSON, NaviCore config_tool/index.html:5686).
FRAG_ENVELOPE = re.compile(r'\{"f":\d+,"of":\d+,"sid":\d+,"s":')


class PipeLog:
    """session.log for a piped device while its pipe is up: every line through redact_text, and the slice of every
    fragment envelope replaced by its length. Through a WCB the config tool pulls NaviCore's whole CONFIG - the mesh
    password, the AP password and the AP's name (NaviCore rc_config.h:1226-1236, :1365) - as ~100 envelopes that the WCB
    prints on its USB, and Bench.log redacts only NaviCore's and the controller's own lines (runner.REDACT_KINDS): a
    secret cut across two envelopes passes any line-by-line filter (s43 never pulls a bridged GET_CONFIG for this
    reason; the config tool cannot connect without one). The page still gets every line whole: only the log copy is cut.
    settle() then waits out a transfer the spec left running, so no envelope reaches the log after the filter is off."""

    def __init__(self, dev):
        self.dev, self.old, self.last = dev, dev.log, None

    def filt(self, text):
        m = FRAG_ENVELOPE.search(text)
        if m:
            self.last = time.monotonic()
            text = f"{text[:m.end()]}<{len(text) - m.end()} characters not logged>"
        return redact_text(text)

    def __enter__(self):
        old = self.old
        if old:
            self.dev.log = lambda name, direction, text: old(name, direction, self.filt(text))
        return self

    def settle(self, quiet_s=2.0, max_s=30.0):
        """Wait until no envelope has arrived for quiet_s, counted from now at the earliest; at most max_s. NaviCore
        sends one envelope per 150 ms (FRAG_PACING_MS, rc_telemetry.h:529), so a CONFIG the page no longer wants still
        arrives for up to ~15 s after a failed spec or an early disconnect."""
        start = time.monotonic()
        while True:
            now = time.monotonic()
            if now - max(self.last or start, start) >= quiet_s or now - start >= max_s:
                return
            time.sleep(0.2)

    def __exit__(self, *exc):
        self.dev.log = self.old

WIZARD_TESTS = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "wizard"))
# Popen/run keywords that start a child in a process group of its own: Windows' CREATE_NEW_PROCESS_GROUP, or a new
# session elsewhere. Either way run.py's first Ctrl+C ("pause after this test") does not reach node and Chrome, and
# _kill_tree can end the whole tree.
OWN_GROUP = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
             else {"start_new_session": True})


def usb_ids(com):
    """(vid, pid) of a COM port, or (None, None) — pyserial reads them from the USB descriptor."""
    from serial.tools import list_ports
    for p in list_ports.comports():
        if p.device.upper() == com.upper():
            return p.vid, p.pid
    return None, None


# The Playwright node process in flight, so a GUI closing mid-test can end it (kill_live): Windows does not end a
# child when its parent exits, and node is in its own process group, so it would keep Chrome, the COM port and the
# test running after the window had gone.
_LIVE = None


def _kill_tree(proc):
    """kill() ends only node, and would leave its Chrome holding the COM port.

    taskkill runs in its own process group and is waited out, never run(): a Ctrl+C mashed during an abort reached a
    taskkill sharing run.py's console and ended it before it killed anything (0xC000013A, measured), and run()'s bare
    except then kills the child when a KeyboardInterrupt lands in communicate(). node and Chrome, in their own group,
    ignore that Ctrl+C, so taskkill is the only thing that ends them. A KeyboardInterrupt is re-raised once it is done.
    Elsewhere the child leads its own session (OWN_GROUP), so its process group is it and everything it started: kill
    the group. proc.kill() alone ended only node and left Chrome holding the board's port."""
    if os.name != "nt":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:                  # not a group leader (started without OWN_GROUP), or already gone
            proc.kill()
        return
    tk = subprocess.Popen(["taskkill", "/T", "/F", "/PID", str(proc.pid)], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    pending = None
    while True:
        try:
            tk.wait()
            break
        except KeyboardInterrupt as e:   # the tree must still die first
            pending = e
    if pending is not None:
        raise pending


def kill_live():
    """End the Playwright test in flight, if any (gui.py's close-now, after the checkpoint is frozen)."""
    p = _LIVE
    if p is not None and p.poll() is None:
        _kill_tree(p)


def _outcomes(report):
    """[(title, status, message)] from a Playwright JSON report (suites nest)."""
    out = []

    def walk(suite):
        for spec in suite.get("specs", []):
            for t in spec.get("tests", []):
                results = t.get("results") or [{}]
                last = results[-1]
                status = last.get("status") or t.get("status", "skipped")
                msg = "\n".join(e.get("message", "") for e in last.get("errors", []))
                if status == "skipped":
                    msg = next((a.get("description", "") for a in t.get("annotations", []) if a.get("type") == "skip"), "")
                out.append((spec.get("title", ""), status, msg))
        for s in suite.get("suites", []):
            walk(s)

    for s in report.get("suites", []):
        walk(s)
    return out


PORT_SETTLE_S = 1.0      # after the last holder of a port lets go, before the harness opens it (_port_free)


def _port_free(path, timeout=10.0):
    """Off Windows: wait until no process holds `path` (lsof), then PORT_SETTLE_S more -> True, or False at the timeout.
    On 2026-10-08 the harness's open of WCB1's CH343 port right after wizard.relay_terminal hung in the kernel twice
    (WCH's CH34xVCPDriver), and the chip itself then answered nothing until it was replugged. Chrome may still be
    closing the port when node exits, and an open racing a close is the likeliest way into that; the wait costs about
    a second a Wizard test. Windows' exclusive open already fails fast while Chrome holds the port."""
    if os.name == "nt" or not path:
        return True
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            held = subprocess.run(["lsof", "-t", path], capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:  # noqa: BLE001 - no lsof answer: treat as free and rely on the settle
            held = ""
        if not held:
            time.sleep(PORT_SETTLE_S)
            return True
        time.sleep(0.25)
    return False


def _reacquire(bench, device, timeout=25.0):
    """Take the port back from Chrome and wait for the board to answer. Opening or closing the port from Chrome can
    toggle DTR/RTS and reset the board, and Chrome may hold the handle a moment after it exits. A NaviCore must answer
    a PING (its native-USB S3 resets on a DTR/RTS edge and needs a few seconds to boot); other non-WCB devices only
    need the port back. After a pipe run the port was never released, so this only proves the board still answers.
    Off Windows nothing opens the port until Chrome has let go of it (_port_free)."""
    deadline = time.monotonic() + timeout
    last = None
    kind = bench.cfg["devices"][device]["kind"]
    if not _port_free(bench.cfg["devices"][device].get("port")):
        bench.note(f"{device}: {bench.cfg['devices'][device].get('port')} is still held 10 s after the Wizard test; "
                   f"opening it anyway")
    while time.monotonic() < deadline:
        try:
            dev = bench.dev(device)
        except Exception as e:  # noqa: BLE001 — still held by the exiting browser
            last = e
            time.sleep(0.5)
            continue
        if kind not in ("wcb", "navicore"):
            return
        try:
            if kind == "wcb":
                WCB(dev).version()
            else:
                from .navicore import NaviCore
                NaviCore(dev).ping()
            return
        except ExpectTimeout as e:
            last = e
            time.sleep(1.0)
    raise AssertionError(f"{device} did not come back after the Wizard test: {last}")


def _wait_node(bench, proc, timeout):
    """Log proc's output into session.log until it exits -> (last 25 lines, killed by the watchdog).

    The pipe is read on a helper thread and the main thread waits in 0.5 s steps. A pipe read on Windows ignores
    Ctrl+C, so with the read on the main thread run.py's SIGINT handler would wait for node's next line - up to the
    3-minute port-picker timeout - and two presses in that silence merged into one pause request. node and Chrome are
    in their own process group, so Ctrl+C no longer reaches them: on an abort (KeyboardInterrupt) the whole tree is
    killed here, which frees the COM port and the Chrome profile. The watchdog is a timer, not a check in a loop that
    reads: a hung browser prints nothing."""
    killed = threading.Event()
    watchdog = threading.Timer(timeout, lambda: (killed.set(), _kill_tree(proc)))
    watchdog.start()
    tail = []

    def drain():
        for line in proc.stdout:          # the browser test's own progress, into session.log
            line = line.rstrip()
            if line:
                bench.log("wizard", "<", line)   # bench.log holds its own lock, so this is thread-safe
                tail.append(line)
                del tail[:-25]
    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        while True:
            try:
                proc.wait(timeout=0.5)   # back in Python code twice a second, so the SIGINT handler runs promptly
                break
            except subprocess.TimeoutExpired:
                pass
        reader.join(5)
    except BaseException:
        _kill_tree(proc)
        raise
    finally:
        watchdog.cancel()
    return list(tail), killed.is_set()


def _tap_count(out, key):
    """A count from node --test's summary, 0 when absent: the spec reporter's 'ℹ pass 9' (node's default, also when its
    output is piped) or TAP's '# pass 9'. key: pass, skipped, todo."""
    m = re.search(rf"^(?:ℹ|#) {key} (\d+)\s*$", out, re.M)
    return int(m.group(1)) if m else 0


# The config tool's specs that meet a real NaviCore (L2 and L3, NAVICORE.md §5.4).
BOARD_TOOL_TESTS = ("nctool.board_", "nctool.webserial_")


def navicore_repo_env(bench, test_id, env):
    """NAVICORE_REPO for a config-tool test that meets the board: bench.json "navicore_repo", the NaviCore tree the
    bench's NaviCore image was built from, so the tool under test and the firmware on the board are one pair
    (docs/HIL_WEEK_DECISIONS.md D71). Every other spec keeps D-NC10 - the tool as it is on disk in the sibling NaviCore
    checkout, found by tests/wizard/lib/navicore/paths.js - since the no-board checks compare that tool with its own
    firmware source (nctool.static) and with Intellex's copy. Unset, nothing changes. bench.json is shared by the bench's
    computers and its path names one of them (the Windows PC's hil-week worktree): where it does not exist, the NaviCore
    checkout beside this repo serves, the tree this computer builds its bench image from (D77), and the test says so."""
    repo = bench.cfg.get("navicore_repo")
    if repo and test_id.startswith(BOARD_TOOL_TESTS):
        if not os.path.isfile(os.path.join(repo, "config_tool", "index.html")):
            sibling = os.path.normpath(os.path.join(WIZARD_TESTS, "..", "..", "..", "NaviCore"))
            if os.path.isfile(os.path.join(sibling, "config_tool", "index.html")):
                bench.note(f"{test_id}: bench.json navicore_repo ({repo}) is not on this computer - serving the "
                           f"NaviCore checkout beside this repo ({sibling})")
                repo = sibling
        env["NAVICORE_REPO"] = repo
    return env


def run_unit_tests(bench, timeout=120.0, files=("unit/*.test.js",)):
    """Node tests under `node --test` (tests/wizard/unit): Wizard/parser.js by default, or the given files (the NaviCore
    config tool's L0 tests, unit/navicore/*.test.js, nctool.static and nctool.unit). No browser and no board — it is
    here so one command runs every check, and so a regression shows up in the same report as the bench tests.
    Every test skipped (the NaviCore repo is not beside this one) is a SKIP, not a pass; a failing `todo` test (a
    known, pinned tool defect) does not fail the run and is noted by name."""
    node = shutil.which("node")
    if not node:
        raise Skip("Node.js is not on PATH")
    # Its own process group, like the Playwright Popen below: run.py's first Ctrl+C means "pause after this test", and
    # without this node shares the console, gets CTRL_C_EVENT, exits 1 and the test is recorded as FAIL. A second
    # Ctrl+C still ends it: subprocess.run kills the child when KeyboardInterrupt escapes.
    proc = subprocess.run([node, "--test", *files], cwd=WIZARD_TESTS, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout, **OWN_GROUP)
    for line in (proc.stdout + proc.stderr).splitlines():
        if line.strip():
            bench.log("wizard", "<", line.rstrip())
    if proc.returncode != 0:
        fails = [l.strip() for l in proc.stdout.splitlines()
                 if l.strip().startswith(("not ok", "✖")) and "# TODO" not in l]
        raise AssertionError("\n".join(fails) or f"node --test exited {proc.returncode}")
    # A todo test that still fails: spec reporter '⚠ <name> (<ms>) # <reason>' (listed twice), TAP 'not ok N - <name> # TODO'.
    todo = dict.fromkeys(l.strip() for l in proc.stdout.splitlines()
                         if l.strip().startswith("⚠") or (l.strip().startswith("not ok") and "# TODO" in l))
    for t in todo:
        bench.note(f"known defect (node todo, still failing): {t}")
    if _tap_count(proc.stdout, "pass") == 0 and _tap_count(proc.stdout, "skipped") > 0:
        why = (re.search(r"# SKIP (.+)$", proc.stdout, re.M)                 # TAP
               or re.search(r"^\s*﹣ .*? # (.+)$", proc.stdout, re.M))       # spec reporter
        raise Skip(why.group(1).strip() if why else "every node test skipped")


def run_wizard_test(bench, test_id, device="wcb1", args=None, timeout=300.0, pipe=False):
    """Run the Playwright test titled `<test_id> ...` with `device`'s port handed to Chrome. device=None runs a
    test that needs no board and leaves every port where it is. pipe=True keeps the device's port open in the harness
    instead and the page gets a fake Web Serial port piped to it through the bridge's /serial routes (the NaviCore
    config tool's L2 specs, docs/hil_plan/NAVICORE.md §5.2): no reset on open, and every line is logged and redacted.
    Returns nothing; raises AssertionError / Skip."""
    node = shutil.which("node")
    if not node:
        raise Skip("Node.js is not on PATH")
    cli = os.path.join(WIZARD_TESTS, "node_modules", "@playwright", "test", "cli.js")
    if not os.path.isfile(cli):
        raise Skip(f"tests/wizard is not installed — in {WIZARD_TESTS} run: npm install && npx playwright install chromium")
    if pipe and not device:
        raise ValueError("pipe=True needs a device to pipe")

    context = {"device": device, "pipe": bool(pipe), "args": args or {}}
    if device:
        d = bench.cfg["devices"][device]
        vid, pid = usb_ids(d["port"])
        if vid is None:
            raise AssertionError(f"{device}: {d['port']} is not a USB serial port on this PC")
        context.update(com=d["port"], kind=d["kind"], wcb=d.get("wcb"), vid=vid, pid=pid)

    out_dir = bench.out_dir or tempfile.mkdtemp(prefix="wizard-")
    report_path = os.path.join(out_dir, f"{test_id}.playwright.json")
    env = navicore_repo_env(bench, test_id, dict(os.environ, PLAYWRIGHT_JSON_OUTPUT_NAME=report_path, FORCE_COLOR="0"))
    if test_id.startswith("nctool."):
        # A failed spec's test-results/<test>/error-context.md would carry an ARIA snapshot of the page, and on a real
        # NaviCore the config tool's page holds the CONFIG echo and the credential fields (NAVICORE.md D-NC5). This
        # variable is Playwright's own switch for that snapshot (playwright lib/index.js _takePageSnapshot); the error
        # messages the harness reports come from the JSON report and stay.
        env["PLAYWRIGHT_NO_COPY_PROMPT"] = "1"
    # --grep sees "<project> <file> <describe> <title>", so the id is delimited by spaces, not anchored with ^.
    # node on the CLI directly, never the npx .cmd shim: cmd.exe would read the regex's | as a pipe.
    cmd = [node, cli, "test", "--reporter=line,json", "--grep", r"(^|\s)" + re.escape(test_id) + r"(\s|$)"]

    global _LIVE
    plog = None
    if device and pipe:
        # Opened (or kept open) here: the page reaches it only through the bridge. Its session.log copy goes through
        # PipeLog while the page runs (a bridged CONFIG crosses a WCB's USB in fragment envelopes).
        plog = PipeLog(bench.dev(device)).__enter__()
    elif device:
        bench.close_device(device)
    aborted = None
    try:
        with Bridge(bench, context) as bridge:
            env["HIL_BRIDGE"] = bridge.url
            bench.note(f"wizard: {test_id} on {context.get('com', 'no board')}{' (piped)' if pipe else ''} "
                       f"(bridge {bridge.url})")
            # Its own process group: run.py's first Ctrl+C means "pause after this test", and without this it would
            # also reach node and Chrome in the same console and kill the Wizard test in flight. _kill_tree uses
            # taskkill, so the watchdog still ends the whole tree.
            proc = subprocess.Popen(cmd, cwd=WIZARD_TESTS, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace", **OWN_GROUP)
            _LIVE = proc
            tail, killed = _wait_node(bench, proc, timeout)
            if killed:
                raise AssertionError(f"{test_id}: Playwright still running after {timeout:.0f}s — killed")
    except BaseException as e:
        if not isinstance(e, Exception):     # KeyboardInterrupt: an ordinary test failure keeps today's handling
            aborted = e
        raise
    finally:
        _LIVE = None
        if plog is not None and aborted is None:
            # Not on an abort: then the filter stays on (it only ever hides and redacts), so nothing slips out after it.
            if context.get("kind") != "navicore" or plog.last is not None:
                plog.settle()
            plog.__exit__()
        if device:
            try:
                _reacquire(bench, device)
            except AssertionError as e:
                if aborted is None:
                    raise
                # never let this replace the abort already on its way out: a second Ctrl+C must still abort
                bench.note(f"{device} not reacquired after the abort: {e}")

    try:
        with open(report_path, encoding="utf-8") as f:
            outcomes = _outcomes(json.load(f))
    except (OSError, ValueError):
        raise AssertionError(f"{test_id}: Playwright exited {proc.returncode} with no report:\n    " + "\n    ".join(tail))
    if not outcomes:
        raise AssertionError(f"{test_id}: no Playwright test is titled '{test_id} ...' in tests/wizard/specs")
    failed = [(t, m) for t, s, m in outcomes if s not in ("passed", "skipped")]
    if failed:
        raise AssertionError("\n".join(f"{t}: {m}" for t, m in failed))
    skipped = [m for _, s, m in outcomes if s == "skipped"]
    if skipped and len(skipped) == len(outcomes):
        raise Skip(skipped[0] or "skipped by the Playwright test")
