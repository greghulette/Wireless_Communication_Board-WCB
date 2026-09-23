"""Run one Wizard (Playwright) test from tests/wizard against a bench board, with the harness in charge.

The harness hands the board's COM port to Chrome, serves a Bridge for the browser test to reach the probes and the
other boards through, runs Playwright for the one test id, and takes the port back. Web Serial never
shows a COM number, so the browser test finds its board by the USB VID/PID the harness reads for that COM port —
and each device gets its own Chrome profile (tests/wizard/.profiles/<device>), holding only that board's grant,
because the bench's CP210x boards (wcb2, both probes) all report the same VID/PID.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time

from .bridge import Bridge
from .runner import Skip
from .serialdev import ExpectTimeout
from .wcb import WCB

WIZARD_TESTS = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "wizard"))


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
    ignore that Ctrl+C, so taskkill is the only thing that ends them. A KeyboardInterrupt is re-raised once it is done."""
    if os.name != "nt":
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


def _reacquire(bench, device, timeout=25.0):
    """Take the port back from Chrome and wait for the board to answer. Opening or closing the port from Chrome can
    toggle DTR/RTS and reset the board, and Chrome may hold the handle a moment after it exits."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            dev = bench.dev(device)
        except Exception as e:  # noqa: BLE001 — still held by the exiting browser
            last = e
            time.sleep(0.5)
            continue
        if bench.cfg["devices"][device]["kind"] != "wcb":
            return
        try:
            WCB(dev).version()
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


def run_unit_tests(bench, timeout=120.0):
    """Wizard/parser.js under `node --test` (tests/wizard/unit). No browser and no board — it is here so one command
    runs every Wizard check, and so a parser regression shows up in the same report as the bench tests."""
    node = shutil.which("node")
    if not node:
        raise Skip("Node.js is not on PATH")
    # Its own process group, like the Playwright Popen below: run.py's first Ctrl+C means "pause after this test", and
    # without this node shares the console, gets CTRL_C_EVENT, exits 1 and the test is recorded as FAIL. A second
    # Ctrl+C still ends it: subprocess.run kills the child when KeyboardInterrupt escapes.
    proc = subprocess.run([node, "--test", "unit/*.test.js"], cwd=WIZARD_TESTS, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    for line in (proc.stdout + proc.stderr).splitlines():
        if line.strip():
            bench.log("wizard", "<", line.rstrip())
    if proc.returncode != 0:
        fails = [l.strip() for l in proc.stdout.splitlines() if l.strip().startswith(("not ok", "✖"))]
        raise AssertionError("\n".join(fails) or f"node --test exited {proc.returncode}")


def run_wizard_test(bench, test_id, device="wcb1", args=None, timeout=300.0):
    """Run the Playwright test titled `<test_id> ...` with `device`'s port handed to Chrome. device=None runs a
    test that needs no board and leaves every port where it is. Returns nothing; raises AssertionError / Skip."""
    node = shutil.which("node")
    if not node:
        raise Skip("Node.js is not on PATH")
    cli = os.path.join(WIZARD_TESTS, "node_modules", "@playwright", "test", "cli.js")
    if not os.path.isfile(cli):
        raise Skip(f"tests/wizard is not installed — in {WIZARD_TESTS} run: npm install && npx playwright install chromium")

    context = {"device": device, "args": args or {}}
    if device:
        d = bench.cfg["devices"][device]
        vid, pid = usb_ids(d["port"])
        if vid is None:
            raise AssertionError(f"{device}: {d['port']} is not a USB serial port on this PC")
        context.update(com=d["port"], kind=d["kind"], wcb=d.get("wcb"), vid=vid, pid=pid)

    out_dir = bench.out_dir or tempfile.mkdtemp(prefix="wizard-")
    report_path = os.path.join(out_dir, f"{test_id}.playwright.json")
    env = dict(os.environ, PLAYWRIGHT_JSON_OUTPUT_NAME=report_path, FORCE_COLOR="0")
    # --grep sees "<project> <file> <describe> <title>", so the id is delimited by spaces, not anchored with ^.
    # node on the CLI directly, never the npx .cmd shim: cmd.exe would read the regex's | as a pipe.
    cmd = [node, cli, "test", "--reporter=line,json", "--grep", r"(^|\s)" + re.escape(test_id) + r"(\s|$)"]

    global _LIVE
    if device:
        bench.close_device(device)
    aborted = None
    try:
        with Bridge(bench, context) as bridge:
            env["HIL_BRIDGE"] = bridge.url
            bench.note(f"wizard: {test_id} on {context.get('com', 'no board')} (bridge {bridge.url})")
            # Its own process group: run.py's first Ctrl+C means "pause after this test", and without this it would
            # also reach node and Chrome in the same console and kill the Wizard test in flight. _kill_tree uses
            # taskkill, so the watchdog still ends the whole tree.
            proc = subprocess.Popen(cmd, cwd=WIZARD_TESTS, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace",
                                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
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
