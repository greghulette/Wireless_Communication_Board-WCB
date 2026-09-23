"""Harness self-test: the run checkpoint and the runner loop, with a fake bench and fake tests. No hardware, no serial
port, no bench - it runs anywhere Python and pyserial are.

    python tests/hil/selftest.py

It covers pause and resume (docs/HIL_TESTING.md §9): a new run to done; the finished single-segment report byte for
byte against the layout write_report produced before checkpoints existed (GOLDEN); a PAUSE-file pause that releases
everything and a resume that continues in order with active time summed over segments; Stop; Pause and Stop together
(last press wins); a test cut off mid-way re-run first; the pre-test outage gate; an automatic retry after a host
outage; checkpoint load cleanup; dropped test ids; find_resumable; the run lock held by another process; atomic
writes against a file another handle holds; redaction of the mesh password and WiFi credentials; and the resume's
re-identification (a moved board found by its USB serial, a different board answering as WCB1 never adopted unasked,
a changed command character or LFI blocked at once), against faked USB enumeration and identify_port. Also, from the
adversarial review: identify_port against a scripted port (nothing unprefixed sent); a .tmp newer than a checkpoint.json
another handle held; a finished-but-unsaved run resumed to done without the checks; a held NaviCore port blocking;
a passing wire recorded as verified; the SBUS controller's held inputs released after a cut-off sbus.* test; a Ctrl+C
during a Wizard test landing at once and killing node's tree; run.py's questions on stdin=NUL and its SIGINT handler;
Ctrl+C during the resume checks; a PAUSE file with an old mtime; secrets in free text; tests added since a selection.
Outside pause/resume, it also checks that the probe's bundled EspSoftwareSerial is byte-identical to the WCB's; the
expected durations (hil/durations.py: checkpoint and report.md sources, the median of the newest five real results,
SKIP and NOT A RESULT rows left out, the cache reused and invalidated, corrupt files tolerated); the runner's up-front
opt-in skip with the exact message the tests' own checks used to raise; and run.py --list, in a subprocess that
imports the real suites (read-only: it writes no cache), for every gated test's declared opt-in and message.

The real suites are never imported: runner.REGISTRY holds fake tests while this runs, and the rest of the resume
checks (resume.check_bench, which talks to the boards) are replaced by a stub. What only the real bench can prove is
listed in docs/HIL_TESTING.md §9.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from hil import checkpoint, durations, optin, resume, runner  # noqa: E402
from hil.checkpoint import Checkpoint, CheckpointError, RunBusy, RunLock  # noqa: E402

# report.md exactly as runner.write_report wrote it for GOLDEN_RESULTS with elapsed=3725.4, captured from that function
# (tests/hil/hil/runner.py before this change) and compared here byte for byte, with the platform's line endings.
GOLDEN = ('# HIL run 20260923-081500\n\nERROR 1 \xb7 FAIL 1 \xb7 PASS 2 \xb7 SKIP 1 \xb7 took 1:02:05\n\n| Result | Test | '
          'Title | Time | Detail |\n|---|---|---|---|---|\n| PASS | probe.hello | Probe answers HELLO | 1.9s |  |\n| FAIL | '
          'wcb.version | WCB reports its version | 3.0s | wcb1: no line matching /x/ within 3s; last lines: |\n| SKIP | '
          'serial.s3 | S3 pipes → bytes | 0.0s | needs a probe wire on W1S3 |\n| ERROR | mesh.bcast | Broadcast | '
          'reaches W2 | 12.5s | Traceback (most recent call last): |\n| PASS | pwm.out | PWM out | 65.0s |  |\n\n## Failure '
          'detail\n\n### wcb.version — WCB reports its version\n\n```\nwcb1: no line matching /x/ within 3s; last '
          'lines:\n    a | b\n    c\n```\n\n### mesh.bcast — Broadcast | reaches W2\n\n```\nTraceback (most recent '
          'call last):\n  File "x", line 1\nValueError: boom\n```\n')
GOLDEN_RESULTS = [
    ("probe.hello", "Probe answers HELLO", "PASS", "", 1.94),
    ("wcb.version", "WCB reports its version", "FAIL", "wcb1: no line matching /x/ within 3s; last lines:\n    a | b\n    c",
     3.05),
    ("serial.s3", "S3 pipes → bytes", "SKIP", "needs a probe wire on W1S3", 0.0),
    ("mesh.bcast", "Broadcast | reaches W2", "ERROR",
     "Traceback (most recent call last):\n  File \"x\", line 1\nValueError: boom", 12.5),
    ("pwm.out", "PWM out", "PASS", "", 65.0),
]

SECRETS = ("hunter2", "sekrit99", "DomeNet")
CONFIG = ["?HW,24", "?WCB,1", "?WIFI,AP,DomeNet,sekrit99", "?EPASS,hunter2", "?CMDCHAR,;"]


# ---------------------------------------------------------------------------- fakes
class Clock:
    """Stands in for checkpoint.active_clock: moves only when a fake test says so, so active times are exact."""
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


CLOCK = Clock()


class FakeDev:
    """Duck-types what the runner reads of a SerialDevice: name, active, connected, last_error_at, close()."""
    def __init__(self, name, connected=True):
        self.name, self.connected, self.active, self.last_error_at = name, connected, True, None

    def close(self):
        self.active = False


class Stubs:
    def __init__(self):
        self.snapshots = 0
        self.firmware = 0
        self.checks = []          # (in_flight at the call, cut_off, ask)
        self.check_result = ([], {})
        self.check_raises = None  # an exception the next check_bench raises (once)

    def snapshot_configs(self, bench):
        self.snapshots += 1
        return {"1": list(CONFIG)}

    def record_firmware(self, bench):
        self.firmware += 1
        return {"wcb1": "6.2.1_TEST"}

    def check_bench(self, bench, ckpt, ask, log=None, should_abort=None, cut_off=False, on_bench_changed=None):
        self.checks.append((ckpt.in_flight, cut_off, ask))
        if self.check_raises is not None:
            e, self.check_raises = self.check_raises, None
            raise e
        return self.check_result


STUBS = Stubs()


def fake(tid, fn=None, title=None, ran=None):
    """A registry entry that needs nothing (no device, no wire, drives nothing)."""
    def body(bench):
        if ran is not None:
            ran.append(tid)
        if fn:
            fn(bench)
    return {"id": tid, "title": title or f"fake {tid}", "needs": [], "links": [], "drives": [], "_drives": set(),
            "fn": body}


def tick(seconds):
    def f(bench):
        CLOCK.t += seconds
    return f


class Tmp:
    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="hil-selftest-")
        self.results = os.path.join(self.root, "results")

    def bench(self, devices=None):
        cfg = {"_comment": "selftest", "devices": devices or {"wcb1": {"port": "COMFAKE1", "kind": "wcb", "wcb": 1}},
               "mesh_only_wcbs": []}
        path = os.path.join(self.root, "bench.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        return runner.Bench(path, self.results)

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


def read(path, mode="r"):
    with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as f:
        return f.read()


def new_run(bench, tests, label="selftest", control=None, **kw):
    control = control or runner.RunControl()
    runner.REGISTRY[:] = tests
    ckpt = runner.start_run(bench, tests, label)
    runner.continue_run(bench, ckpt, resuming=False, should_stop=control.should_stop,
                        should_pause=control.should_pause, **kw)
    return ckpt


def resume_run(bench, path, control=None, **kw):
    control = control or runner.RunControl()
    ckpt = Checkpoint.load(path)
    runner.continue_run(bench, ckpt, resuming=True, should_stop=control.should_stop,
                        should_pause=control.should_pause, **kw)
    return ckpt


def ids(ckpt):
    return [r["id"] for r in ckpt.data["results"]]


# ---------------------------------------------------------------------------- tests
def t_new_run_to_done(tmp):
    b = tmp.bench()
    ran = []
    tests = [fake(x, tick(5), ran=ran) for x in "abc"]
    before = STUBS.snapshots
    ck = new_run(b, tests)
    assert ck.state == "done", ck.state
    assert ran == ["a", "b", "c"] and ids(ck) == ["a", "b", "c"]
    assert STUBS.snapshots == before, "A2: a run under 5 tests must not take the start snapshot"
    assert ck.data["config_ref"] is None
    assert not RunLock.held(ck.out_dir), "lock must be released"
    on_disk = Checkpoint.load(ck.out_dir)
    assert on_disk.state == "done" and on_disk.active_s == 15.0, (on_disk.state, on_disk.active_s)
    rep = read(os.path.join(ck.out_dir, "report.md"))
    assert "RUNNING" not in rep and "## Segments" not in rep and "PASS 3 · took 0:15" in rep, rep
    # five or more tests: the baseline is taken at the start
    ck5 = new_run(b, [fake(x) for x in "vwxyz"])
    assert STUBS.snapshots == before + 1 and ck5.data["config_ref"]["taken"] == "start"


def t_golden_report(tmp):
    b = tmp.bench()
    out = os.path.join(tmp.root, "20260923-081500")
    os.makedirs(out)
    tests = [{"id": i, "title": title} for i, title, *_ in GOLDEN_RESULTS]
    ck = Checkpoint.new(out, tests, "golden", None, b)
    CLOCK.t = 1000.0
    ck.begin_segment(usb={}, firmware={})
    for (i, title, status, detail, dur), t in zip(GOLDEN_RESULTS, tests):
        ck.record_result(t, status, detail, dur)
    CLOCK.t = 1000.0 + 3725.4
    ck.finish("done")
    got = read(os.path.join(out, "report.md"), "rb")
    want = GOLDEN.replace("\n", os.linesep).encode("utf-8")
    assert got == want, f"report differs from the pre-checkpoint layout:\n{got!r}\n!=\n{want!r}"


def t_pause_file_and_resume(tmp):
    b = tmp.bench()
    ran = []
    seen = {}

    def b_fn(bench):
        CLOCK.t += 10
        seen["report_mid"] = read(os.path.join(bench.out_dir, "report.md"))
        open(os.path.join(bench.out_dir, "PAUSE"), "w").close()
    tests = [fake("a", tick(10), ran=ran), fake("b", b_fn, ran=ran)] + [fake(x, tick(10), ran=ran) for x in "cdef"]
    ck = new_run(b, tests)
    d = ck.out_dir
    assert "**RUNNING** — 1 of 6 done" in seen["report_mid"], seen["report_mid"]
    assert ck.state == "paused" and ck.data["reason"] == "pause_file", (ck.state, ck.data["reason"])
    assert ids(ck) == ["a", "b"] and ran == ["a", "b"]
    assert not os.path.exists(os.path.join(d, "PAUSE")), "the PAUSE file is consumed"
    assert not RunLock.held(d) and b._log is None and not b.devs, "lock, log and ports all released at the pause"
    assert ck.data["config_ref"]["taken"] == "pause" and ck.data["config_ref"]["after_test"] == "b"
    rep = read(os.path.join(d, "report.md"))
    assert "**PAUSED** after 2 of 6" in rep and f"--resume {ck.name}" in rep, rep
    summ = checkpoint.find_resumable(tmp.results)
    assert [s["name"] for s in summ] == [ck.name] and summ[0]["state"] == "paused"
    # resume in a "new process": a fresh Bench, the checkpoint read from disk
    b2 = tmp.bench()
    ck2 = resume_run(b2, d)
    assert ck2.state == "done", ck2.state
    assert ran == ["a", "b", "c", "d", "e", "f"], ran
    assert ids(ck2) == ["a", "b", "c", "d", "e", "f"]
    assert len(ck2.data["segments"]) == 2
    seg = [s["active_s"] for s in ck2.data["segments"]]
    assert seg == [20.0, 40.0] and ck2.active_s == 60.0, seg
    log = read(os.path.join(d, "session.log"))
    assert "===== SEGMENT 2" in log and "===== run PAUSED" in log
    rep = read(os.path.join(d, "report.md"))
    assert "## Segments" in rep and "PASS 6 · took 1:00" in rep and rep.count("| PASS | ") == 6, rep
    assert checkpoint.find_resumable(tmp.results) == []


def t_stop(tmp):
    b = tmp.bench()
    ctl = runner.RunControl()
    tests = [fake("a"), fake("b", lambda bench: ctl.request_stop()), fake("c"), fake("d")]
    ck = new_run(b, tests, control=ctl)
    assert ck.state == "stopped" and ids(ck) == ["a", "b"], (ck.state, ids(ck))
    rep = read(os.path.join(ck.out_dir, "report.md"))
    assert "STOPPED" not in rep and "## Not run" not in rep, "A6: a never-paused stopped run keeps the old layout"
    assert checkpoint.find_resumable(tmp.results) == []
    assert [s["name"] for s in checkpoint.find_resumable(tmp.results, include_stopped=True)] == [ck.name]


def t_last_press_wins(tmp):
    c = runner.RunControl()
    c.request_pause("user")
    c.request_stop()
    assert c.should_stop() and c.should_pause() is None
    c.request_pause("user")
    assert not c.should_stop() and c.should_pause() == "user"
    c.cancel_pause()
    assert not c.should_stop() and c.should_pause() is None
    b = tmp.bench()
    ctl = runner.RunControl()
    ck = new_run(b, [fake("a", lambda bench: (ctl.request_pause("user"), ctl.request_stop())), fake("b")], control=ctl)
    assert ck.state == "stopped", f"pause then stop must stop, got {ck.state}"
    ctl = runner.RunControl()
    ck = new_run(b, [fake("a", lambda bench: (ctl.request_stop(), ctl.request_pause("user"))), fake("b")], control=ctl)
    assert ck.state == "paused" and ck.data["reason"] == "user", f"stop then pause must pause, got {ck.state}"


def t_cut_off_reruns_first(tmp):
    b = tmp.bench()
    ran = []

    def interrupt(bench):
        raise KeyboardInterrupt
    tests = [fake("a", ran=ran), fake("b", interrupt, ran=ran), fake("c", ran=ran)]
    runner.REGISTRY[:] = tests
    ck = runner.start_run(b, tests, "cut")
    try:
        runner.continue_run(b, ck, resuming=False)
        raise AssertionError("KeyboardInterrupt should escape")
    except KeyboardInterrupt:
        pass
    disk = Checkpoint.load(ck.out_dir)
    assert disk.state == "paused" and disk.data["reason"] == "aborted", (disk.state, disk.data["reason"])
    assert disk.in_flight == "b" and ids(disk) == ["a"], (disk.in_flight, ids(disk))
    assert not RunLock.held(ck.out_dir)
    tests[1] = fake("b", ran=ran)               # the test no longer interrupts
    runner.REGISTRY[:] = tests
    n = len(STUBS.checks)
    ck2 = resume_run(tmp.bench(), ck.out_dir)
    assert STUBS.checks[n][0] == "b", "the resume checks must see the cut-off test in flight"
    assert ran == ["a", "b", "b", "c"] and ids(ck2) == ["a", "b", "c"] and ck2.in_flight is None, (ran, ids(ck2))
    # a harness error outside the test body (here on_start) also leaves the test in flight, no result
    b3 = tmp.bench()
    tests = [fake("x"), fake("y"), fake("z")]
    runner.REGISTRY[:] = tests
    ck3 = runner.start_run(b3, tests, "harness error")

    def on_start(t):
        if t["id"] == "y":
            raise RuntimeError("bug in the harness")
    try:
        runner.continue_run(b3, ck3, resuming=False, on_start=on_start)
        raise AssertionError("RuntimeError should escape")
    except RuntimeError:
        pass
    disk = Checkpoint.load(ck3.out_dir)
    assert disk.state == "paused" and disk.data["reason"] == "harness_error" and disk.in_flight == "y"
    assert ids(disk) == ["x"] and "bug in the harness" in disk.data["reason_text"]


def t_frozen_checkpoint_records_nothing(tmp):
    b = tmp.bench()
    box = {}
    tests = [fake("a"), fake("b", lambda bench: box["ck"].freeze()), fake("c")]
    runner.REGISTRY[:] = tests
    ck = runner.start_run(b, tests, "freeze")
    box["ck"] = ck
    runner.continue_run(b, ck, resuming=False)
    disk = Checkpoint.load(ck.out_dir)
    assert ids(disk) == ["a"] and disk.in_flight == "b" and disk.state == "running", (ids(disk), disk.in_flight)


def t_pretest_outage_gate(tmp):
    b = tmp.bench()
    ran = []
    tests = [fake("a", ran=ran), fake("b", ran=ran)]
    runner.REGISTRY[:] = tests
    ck = runner.start_run(b, tests, "gate")
    dev = FakeDev("wcb1", connected=False)      # the reader is waiting to reopen a vanished port
    b.devs["wcb1"] = dev
    old = runner.OUTAGE_GRACE_S
    runner.OUTAGE_GRACE_S = 1
    try:
        runner.continue_run(b, ck, resuming=False)
    finally:
        runner.OUTAGE_GRACE_S = old
    assert ran == [], "the test must never start"
    assert ck.state == "paused" and ck.data["reason"] == "host_outage", (ck.state, ck.data["reason"])
    assert ids(ck) == [] and ck.in_flight is None
    assert ck.data["outages"][0]["kind"] == "between_tests" and ck.data["outages"][0]["then"] == "paused"
    assert not dev.active and not b.devs and b._log is None and not RunLock.held(ck.out_dir)


def t_outage_auto_retry(tmp):
    b = tmp.bench()
    attempts = []
    requeued = []

    def flaky(bench):
        attempts.append(1)
        if len(attempts) == 1:        # every bench port dies at once mid-test: the host lost USB
            for n in ("wcb1", "probe1"):
                bench.devs[n].last_error_at = time.monotonic()
            raise AssertionError("wcb1: COMFAKE1 is gone")
    tests = [fake("a", flaky), fake("b")]
    runner.REGISTRY[:] = tests
    ck = runner.start_run(b, tests, "outage")
    b.devs["wcb1"], b.devs["probe1"] = FakeDev("wcb1"), FakeDev("probe1")
    n = len(STUBS.checks)
    runner.continue_run(b, ck, resuming=False, on_requeue=lambda t, d: requeued.append(t["id"]))
    assert ck.state == "done" and ids(ck) == ["a", "b"] and len(attempts) == 2, (ck.state, ids(ck), attempts)
    assert requeued == ["a"]
    assert STUBS.checks[n][1] is True and STUBS.checks[n][2] is None, "automatic checks: cut_off=True, ask=None"
    o = ck.data["outages"]
    assert len(o) == 1 and o[0]["kind"] == "usb_loss" and o[0]["then"] == "auto-resumed", o
    assert "## Re-run after a host outage" in read(os.path.join(ck.out_dir, "report.md"))
    # the same test losing the host twice pauses instead of looping
    b2 = tmp.bench()

    def always(bench):
        bench.devs["wcb1"], bench.devs["probe1"] = FakeDev("wcb1"), FakeDev("probe1")
        for d in bench.devs.values():
            d.last_error_at = time.monotonic()
        raise AssertionError("gone again")
    tests = [fake("a", always), fake("b")]
    runner.REGISTRY[:] = tests
    ck2 = runner.start_run(b2, tests, "outage twice")
    b2.devs["wcb1"], b2.devs["probe1"] = FakeDev("wcb1"), FakeDev("probe1")
    runner.continue_run(b2, ck2, resuming=False)
    assert ck2.state == "paused" and ck2.data["reason"] == "host_outage" and ck2.in_flight == "a", ck2.state
    assert ids(ck2) == [] and [x["then"] for x in ck2.data["outages"]] == ["auto-resumed", "paused"]


def t_load_cleanup_and_tmp_fallback(tmp):
    d = os.path.join(tmp.results, "m6")
    os.makedirs(d)
    data = {"format": 1, "run": "m6", "state": "paused", "tests": ["a", "b"], "in_flight": {"id": "a"},
            "results": [{"id": "a", "title": "", "status": "PASS", "detail": "", "dur": 1.0}], "segments": []}
    with open(os.path.join(d, "checkpoint.json"), "w", encoding="utf-8") as f:
        json.dump(data, f)
    assert Checkpoint.load(d).in_flight is None, "M6: a test with a result is not in flight"
    # only the .tmp survived a power-off mid-replace
    os.replace(os.path.join(d, "checkpoint.json"), os.path.join(d, "checkpoint.json.tmp"))
    assert Checkpoint.load(d).data["run"] == "m6"
    with open(os.path.join(d, "checkpoint.json.tmp"), "w") as f:
        f.write("{not json")
    try:
        Checkpoint.load(d)
        raise AssertionError("an unreadable checkpoint must raise CheckpointError")
    except CheckpointError:
        pass


def t_dropped_ids(tmp):
    b = tmp.bench()
    ran = []
    old = [fake("a", ran=ran), fake("gone", ran=ran), fake("b", ran=ran)]
    runner.REGISTRY[:] = old
    ck = runner.start_run(b, old, "dropped")
    ck.pause("user")
    ck.release()
    runner.REGISTRY[:] = [old[0], old[2]]           # 'gone' was renamed or removed while paused
    soft, hard, notes, restore = resume.offline_differences(tmp.bench(), Checkpoint.load(ck.out_dir))
    assert not hard and any("gone" in s and "no longer in the suite" in s for s in soft), soft
    n = len(STUBS.checks)
    ck2 = resume_run(tmp.bench(), ck.out_dir)
    assert len(STUBS.checks) == n + 1, "a paused run with tests left still goes through the checks"
    assert Checkpoint.load(ck.out_dir).data["dropped"] == ["gone"]
    assert ran == ["a", "b"] and ids(ck2) == ["a", "b"] and ck2.data["dropped"] == ["gone"], (ran, ck2.data["dropped"])
    # continue_run leaves 'dropped' alone until after step 1 (check_bench), so step 1 still sees the new drop
    ck3 = runner.start_run(tmp.bench(), old, "dropped2")
    ck3.pause("user")
    ck3.release()
    seen = {}
    orig = STUBS.check_bench

    def spy(bench, ckpt, *a, **kw):
        seen["dropped"] = list(ckpt.data["dropped"])
        return orig(bench, ckpt, *a, **kw)
    resume.check_bench = spy
    try:
        resume_run(tmp.bench(), ck3.out_dir)
    finally:
        resume.check_bench = orig
    assert seen["dropped"] == [], seen
    rep = read(os.path.join(ck.out_dir, "report.md"))
    assert "## Not run" in rep and "`gone` — no longer in the suite" in rep, rep


def _write_ck(root, name, state, updated, lock=False):
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "checkpoint.json"), "w", encoding="utf-8") as f:
        json.dump({"format": 1, "run": name, "state": state, "updated": updated, "tests": ["a"], "results": [],
                   "segments": []}, f)
    if lock:
        lk = RunLock(d)
        lk.acquire()
        return lk
    return None


def t_find_resumable(tmp):
    r = os.path.join(tmp.root, "fr")
    os.makedirs(r)
    _write_ck(r, "p_old", "paused", "2026-09-23T10:00:00-04:00")
    _write_ck(r, "p_new", "paused", "2026-09-23T11:00:00-04:00")
    _write_ck(r, "interrupted", "running", "2026-09-23T10:30:00-04:00")
    lk = _write_ck(r, "live", "running", "2026-09-23T12:30:00-04:00", lock=True)
    lk2 = _write_ck(r, "resuming", "paused", "2026-09-23T12:40:00-04:00", lock=True)
    _write_ck(r, "done", "done", "2026-09-23T13:00:00-04:00")
    _write_ck(r, "gone", "abandoned", "2026-09-23T13:00:00-04:00")
    _write_ck(r, "stopped", "stopped", "2026-09-23T12:00:00-04:00")
    os.makedirs(os.path.join(r, "bad"))
    with open(os.path.join(r, "bad", "checkpoint.json"), "w") as f:
        f.write("{truncated")
    os.makedirs(os.path.join(r, "no_checkpoint"))
    try:
        got = [s["name"] for s in checkpoint.find_resumable(r)]
        assert got == ["p_new", "interrupted", "p_old"], got
        got = [s["name"] for s in checkpoint.find_resumable(r, include_stopped=True)]
        assert got == ["stopped", "p_new", "interrupted", "p_old"], got
        s = {x["name"]: x for x in checkpoint.find_resumable(r)}
        assert s["interrupted"]["interrupted"] and not s["p_new"]["interrupted"]
    finally:
        lk.release()
        lk2.release()
    # a run lock taken by another handle in this process is seen as held (Windows byte locks are per handle)
    d = os.path.join(r, "p_old")
    a = RunLock(d)
    a.acquire()
    try:
        assert RunLock.held(d)
        try:
            RunLock(d).acquire(wait_s=0.2)
            raise AssertionError("a second acquire must raise RunBusy")
        except RunBusy:
            pass
    finally:
        a.release()
    assert not RunLock.held(d)


def t_lock_held_by_child_process(tmp):
    d = os.path.join(tmp.root, "child")
    os.makedirs(d)
    code = ("import sys, time; sys.path.insert(0, sys.argv[1]); from hil.checkpoint import RunLock; "
            "l = RunLock(sys.argv[2]); l.acquire(); print('locked', flush=True); time.sleep(60)")
    p = subprocess.Popen([sys.executable, "-c", code, HERE, d], stdout=subprocess.PIPE, text=True)
    try:
        line = p.stdout.readline().strip()
        assert line == "locked", f"child said {line!r}"
        assert RunLock.held(d), "a lock held by another process must be seen as held"
    finally:
        p.kill()
        p.wait(timeout=10)
    deadline = time.monotonic() + 5
    while RunLock.held(d) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not RunLock.held(d), "the OS frees the lock when the process dies"


def t_atomic_write_retry(tmp):
    path = os.path.join(tmp.root, "held.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("old")
    if os.name != "nt":
        assert checkpoint.atomic_write_text(path, "new") and read(path) == "new"
        return
    # Python's own open() on Windows shares read/write but not delete, so os.replace onto the file fails until it closes
    holder = open(path, encoding="utf-8")
    threading.Timer(0.2, holder.close).start()
    t0 = time.monotonic()
    assert checkpoint.atomic_write_text(path, "new"), "the write must succeed once the other handle closes"
    assert read(path) == "new" and time.monotonic() - t0 >= 0.1, "it must have retried"
    logged = []
    with open(path, encoding="utf-8"):
        ok = checkpoint.atomic_write_text(path, "newer", retries=3, log=logged.append)
        ok2 = checkpoint.atomic_write_text(path, "newer", retries=1, log=logged.append)
    assert not ok and not ok2 and read(path) == "new", "a write that cannot land is skipped, the old file intact"
    assert len(logged) == 1, f"logged once per file, got {logged}"
    assert checkpoint.atomic_write_text(path, "newest") and read(path) == "newest"


def t_redaction(tmp):
    r = checkpoint.redact_token
    e = r("?EPASS,hunter2")
    assert e.startswith("?EPASS,<redacted:") and "hunter2" not in e and r(e) == e
    w = r("?WIFI,AP,DomeNet,sekrit99")
    assert w.startswith("?WIFI,AP,<redacted:") and "DomeNet" not in w and "sekrit99" not in w
    assert r("?WIFI,JOIN,Net,pw").startswith("?WIFI,JOIN,<redacted:")
    assert r("?WIFI,OFF") == "?WIFI,OFF" and r("?HW,24") == "?HW,24"
    assert r("?EPASS,hunter2") == e and r("?EPASS,hunter3") != e
    # a paused run's checkpoint and report carry the redacted form only
    b = tmp.bench()
    ctl = runner.RunControl()
    ck = new_run(b, [fake(x) for x in "abcde"] + [fake("f", lambda bench: ctl.request_pause("user"))] + [fake("g")],
                 control=ctl)
    assert ck.state == "paused"
    for name in ("checkpoint.json", "report.md", "session.log"):
        text = read(os.path.join(ck.out_dir, name))
        leaked = [s for s in SECRETS if s in text]
        assert not leaked, f"{name} contains {leaked}"
    toks = ck.data["config_ref"]["tokens"]["1"]
    assert e in toks and w in toks
    # the resume's config comparison works on the redacted forms, and its diff shows only those
    old = resume.read_config
    try:
        resume.read_config = lambda bench, wcb: [t.replace("hunter2", "hunter3") for t in CONFIG]
        diffs, current = resume._config_diffs(b, Checkpoint.load(ck.out_dir))
        assert len(diffs) == 1 and "EPASS" in diffs[0] and not any(s in diffs[0] for s in SECRETS + ("hunter3",))
        resume.read_config = lambda bench, wcb: list(CONFIG)
        diffs, _ = resume._config_diffs(b, Checkpoint.load(ck.out_dir))
        assert diffs == [], diffs
        # A2: no reference -> the comparison is skipped (None)
        ck.data["config_ref"] = None
        assert resume._config_diffs(b, ck) == (None, {})
    finally:
        resume.read_config = old


def t_run_busy(tmp):
    b = tmp.bench()
    tests = [fake("a")]
    runner.REGISTRY[:] = tests
    ck = runner.start_run(b, tests, "busy")
    ck.pause("user")
    ck.release()
    other = RunLock(ck.out_dir)
    other.acquire()
    try:
        resume_run(tmp.bench(), ck.out_dir)
        raise AssertionError("a run held elsewhere must raise RunBusy")
    except RunBusy:
        pass
    finally:
        other.release()
    assert Checkpoint.load(ck.out_dir).state == "paused"


class _Port:
    def __init__(self, device, serial):
        self.device, self.serial_number, self.vid, self.pid, self.location = device, serial, 0x10C4, 0xEA60, None


def t_reidentify_never_guesses(tmp):
    """resume.reidentify against faked USB enumeration and identify_port (hil/identify.py): no port is opened."""
    from hil import identify
    devices = {"wcb1": {"port": "COM6", "kind": "wcb", "wcb": 1}, "wcb2": {"port": "COM15", "kind": "wcb", "wcb": 2},
               "probe1": {"port": "COM16", "kind": "probe", "mac": "AA:BB"},
               "navicore": {"port": "COM5", "kind": "navicore"}, "sbus": {"port": "COM4", "kind": "sbus"}}
    answers = {}
    asked = []
    opened = []

    def fake_identify(port, wcb_home=False):
        opened.append((port, wcb_home))
        return answers.get(port)
    old = (identify.usb_ports, identify.identify_port)
    identify.identify_port = fake_identify

    def setup(ports, ans, in_flight=None):
        b = tmp.bench(json.loads(json.dumps(devices)))
        d = os.path.join(tmp.root, "rid")
        os.makedirs(d, exist_ok=True)
        ck = Checkpoint.new(d, [], "rid", None, b)
        ck.data["devices"] = {"wcb1": {"serial": "S1"}, "wcb2": {"serial": "S2"}, "probe1": {"serial": "S3"},
                              "navicore": {"serial": "S5"}, "sbus": {"serial": "S4"}}
        if in_flight:
            ck.data["in_flight"] = {"id": in_flight}
        identify.usb_ports = lambda: {p: _Port(p, s) for p, s in ports.items()}
        answers.clear()
        answers.update(ans)
        opened.clear()
        return b, ck

    def ask(title, text, default=False):
        asked.append(text)
        return ask.answer
    ask.answer = True
    home = {"COM6": "S1", "COM15": "S2", "COM16": "S3", "COM5": "S5", "COM4": "S4"}
    good = {"COM6": {"kind": "wcb", "wcb": 1, "version": "v1"}, "COM15": {"kind": "wcb", "wcb": 2, "version": "v1"},
            "COM16": {"kind": "probe", "mac": "AA:BB", "version": "4"}}
    try:
        # everything where it was: no moves, NaviCore and SBUS never opened (their ping proves them later)
        b, ck = setup(home, good)
        moves, versions = resume.reidentify(b, ck, lambda s: None, wait_s=0)
        ports_opened = [p for p, _ in opened]
        assert moves == [] and versions["wcb2"] == "v1" and "COM5" not in ports_opened and "COM4" not in ports_opened
        # a WCB found by its recorded serial is identified with the fixed config pull (wcb_home), never ?VERSION first
        assert ("COM6", True) in opened and ("COM6", False) not in opened, opened
        # wcb2 moved: found by its USB serial, confirmed by board number, saved to bench.json
        ports = dict(home)
        del ports["COM15"]
        ports["COM9"] = "S2"
        b, ck = setup(ports, dict(good, COM9=good["COM15"]))
        moves, _ = resume.reidentify(b, ck, lambda s: None, wait_s=0)
        assert moves == ["wcb2 COM15 -> COM9"], moves
        assert json.loads(read(b.bench_path))["devices"]["wcb2"]["port"] == "COM9"
        # the bench WCB1 is gone and another board answers as WCB1: never adopted without asking
        ports = dict(home)
        del ports["COM6"]
        ports["COM12"] = "OTHER"
        ans = dict(good, COM12={"kind": "wcb", "wcb": 1, "version": "v9"})
        b, ck = setup(ports, ans)
        try:
            resume.reidentify(b, ck, lambda s: None, ask=None, wait_s=0)
            raise AssertionError("the outage path (ask=None) must block on a different board")
        except resume.ResumeBlocked as e:
            assert "not the board recorded" in str(e), e
        b, ck = setup(ports, ans)
        ask.answer = False
        try:
            resume.reidentify(b, ck, lambda s: None, ask=ask, wait_s=0)
            raise AssertionError("No must leave the run paused")
        except resume.ResumeDeclined:
            pass
        b, ck = setup(ports, ans)
        ask.answer = True
        moves, _ = resume.reidentify(b, ck, lambda s: None, ask=ask, wait_s=0)
        assert moves == ["wcb1 COM6 -> COM12"] and "COM12 answers as WCB1" in asked[-1], (moves, asked)
        # a probe on another port and another USB serial: its MAC is unique, so it is adopted without asking
        ports = dict(home)
        del ports["COM16"]
        ports["COM20"] = "Z"
        n = len(asked)
        b, ck = setup(ports, dict(good, COM20=good["COM16"]))
        moves, _ = resume.reidentify(b, ck, lambda s: None, ask=ask, wait_s=0)
        assert moves == ["probe1 COM16 -> COM20"] and len(asked) == n, (moves, asked[n:])
        # WCB1 on its own USB serial but silent after a cut-off chars.* test: named, with the likely cause
        b, ck = setup(home, dict(good, COM6=None), in_flight="chars.cmdchar_change_restore")
        try:
            resume.reidentify(b, ck, lambda s: None, ask=ask, wait_s=0)
            raise AssertionError("a silent board must block")
        except resume.ResumeBlocked as e:
            assert "same USB board" in str(e) and "command character, LFI or delimiter" in str(e), e
        # a port busy in another program, holding WCB1's serial
        b, ck = setup(home, dict(good, COM6={"kind": "error", "error": "Access is denied"}))
        try:
            resume.reidentify(b, ck, lambda s: None, ask=ask, wait_s=0)
            raise AssertionError("a held port must block")
        except resume.ResumeBlocked as e:
            assert "COM6 could not be opened" in str(e) and "Arduino IDE" in str(e), e
        # a probe with no mac in bench.json is a hard block, never identified by elimination
        b, ck = setup(home, good)
        del b.cfg["devices"]["probe1"]["mac"]
        try:
            resume.reidentify(b, ck, lambda s: None, ask=ask, wait_s=0)
            raise AssertionError("no mac must block")
        except resume.ResumeBlocked as e:
            assert "no mac" in str(e), e
        # a cut-off chars.* test left W1's command character at ':' (saved in NVS): blocked at once, naming the
        # restore command, with no boot-wait retry (each retry would put more harness text on the mesh)
        b, ck = setup(home, dict(good, COM6={"kind": "wcb", "wcb": 1, "version": None, "lfi": "?", "cmdchar": ":"}),
                      in_flight="chars.cmdchar_change_restore")
        t0 = time.monotonic()
        try:
            resume.reidentify(b, ck, lambda s: None, ask=ask, wait_s=20)
            raise AssertionError("a changed command character must block")
        except resume.ResumeBlocked as e:
            assert "?CMDCHAR,;" in str(e) and time.monotonic() - t0 < 1.0, (str(e), time.monotonic() - t0)
        b, ck = setup(home, dict(good, COM6={"kind": "wcb", "wcb": 1, "version": None, "lfi": "!", "cmdchar": ";"}))
        try:
            resume.reidentify(b, ck, lambda s: None, ask=ask, wait_s=20)
            raise AssertionError("a changed LFI must block")
        except resume.ResumeBlocked as e:
            assert "!FUNCCHAR,?" in str(e) and "LFI is '!'" in str(e), e
        b, ck = setup(home, good, in_flight="chars.funcchar_change_restore")
        assert "power cycle keeps" in resume.cutoff_hint(ck) and "Power-cycle it" not in resume.cutoff_hint(ck)
    finally:
        identify.usb_ports, identify.identify_port = old


class FakeSer:
    """Duck-types SerialDevice for identify_port: answers scripted lines per command, records what was sent."""
    script = {}
    sent = []

    def __init__(self, name, port, baud=115200, log=None):
        self.baud, self.lines = baud, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def mark(self):
        return len(self.lines)

    def since(self, mark):
        return self.lines[mark or 0:]

    def send(self, text, eol="\n"):
        FakeSer.sent.append(text)
        if self.baud == 115200:
            self.lines += FakeSer.script.get(text, [])

    def expect(self, pattern, timeout=3.0, since=None):
        import re
        for line in self.lines[since or 0:]:
            m = re.search(pattern, line)
            if m:
                return m
        raise resume.ExpectTimeout(f"scan: no line matching /{pattern}/")


def t_identify_port_changed_chars(tmp):
    """identify_port against a scripted port: nothing unprefixed is ever sent, and the characters are reported."""
    from hil import identify
    old = identify.SerialDevice
    identify.SerialDevice = FakeSer
    try:
        # LFI '!' on the bench's own WCB (wcb_home): only the fixed config pull is sent - '?VERSION' would be
        # unprefixed on this board and broadcast to the mesh
        FakeSer.sent = []
        FakeSer.script = {"WCB_WEBTOOL_CONFIG_PULL": ["!HW,24", "!WCB,1", "!WCBQ,2", "!FUNCCHAR,!", "!CMDCHAR,;"]}
        info = identify.identify_port("COMX", wcb_home=True)
        assert info == {"kind": "wcb", "version": None, "wcb": 1, "lfi": "!", "cmdchar": ";", "delim": "^"}, info
        assert FakeSer.sent == ["WCB_WEBTOOL_CONFIG_PULL"], FakeSer.sent
        # command character ':' on a port found by scanning: ?VERSION then ?config read to its own lines - no
        # ';S0,HILEND' sentinel, which a ':' board would broadcast
        FakeSer.sent = []
        FakeSer.script = {"?VERSION": ["Software Version: 6.2.1_TEST"],
                          "?config": ["", "Configuration: Wireless Communication Board 1 (W1)",
                                      "Software Version: 6.2.1_TEST", "Command Character:         :"]}
        info = identify.identify_port("COMX")
        assert info == {"kind": "wcb", "version": "6.2.1_TEST", "wcb": 1, "lfi": "?", "cmdchar": ":",
                        "delim": None}, info
        assert FakeSer.sent == ["?VERSION", "?config"], FakeSer.sent
        # default characters on the bench's own WCB: the pull, then ?VERSION and ?config for the version
        FakeSer.sent = []
        FakeSer.script["WCB_WEBTOOL_CONFIG_PULL"] = ["?HW,24", "?WCB,2", "?CMDCHAR,;"]
        FakeSer.script["?config"] = ["Configuration: Wireless Communication Board 2 (W2)", "Command Character: ;"]
        info = identify.identify_port("COMX", wcb_home=True)
        assert info["wcb"] == 2 and info["version"] == "6.2.1_TEST" and info["cmdchar"] == ";", info
        assert all(x == "WCB_WEBTOOL_CONFIG_PULL" or x.startswith("?") for x in FakeSer.sent), FakeSer.sent
        # a ',' delimiter left by a cut-off chars.delim_* test (R2-1): the ?DELIM line comes before CMDCHAR; only the
        # pull is sent - run()'s ';S0,HILEND' would split at the ',' into a broadcast
        FakeSer.sent = []
        FakeSer.script = {"WCB_WEBTOOL_CONFIG_PULL": ["?HW,24", "?WCB,1", "?DELIM,,", "?CMDCHAR,;"]}
        info = identify.identify_port("COMX", wcb_home=True)
        assert info.get("delim") == "," and FakeSer.sent == ["WCB_WEBTOOL_CONFIG_PULL"], (info, FakeSer.sent)
        assert resume._chars_changed(info)
        msg = str(resume._chars_block("wcb1", "COMX", dict(info, cmdchar=":")))
        assert "?D^" in msg and msg.index("?D^") < msg.index("CMDCHAR"), msg   # the delimiter is restored first
        # ?config's "Delimiter Character:" on a scanned port
        FakeSer.sent = []
        FakeSer.script = {"?VERSION": ["Software Version: 6.2.1_TEST"],
                          "?config": ["Configuration: Wireless Communication Board 1 (W1)",
                                      "Delimiter Character:      |", "Command Character:         ;"]}
        info = identify.identify_port("COMX")
        assert info["delim"] == "|" and resume._chars_changed(info), info
        # a silent home port: None (still booting), and 921600 is never tried
        FakeSer.sent = []
        FakeSer.script = {}
        assert identify.identify_port("COMX", wcb_home=True) is None and "HELLO" not in FakeSer.sent
    finally:
        identify.SerialDevice = old


def t_tmp_newer_than_main(tmp):
    """F1: the last saves of a run are skipped while another handle holds checkpoint.json; load() must read the .tmp."""
    b = tmp.bench()
    d = os.path.join(tmp.root, "held")
    os.makedirs(d)
    tests = [{"id": x, "title": x} for x in "abc"]
    ck = Checkpoint.new(d, tests, "held", None, b)
    notes = []
    ck.log = notes.append                 # the skipped saves are logged, not printed
    ck.begin_segment(usb={}, firmware={})
    ck.record_start(tests[0])
    ck.record_result(tests[0], "PASS", "", 1.0)
    ck.record_start(tests[1])
    if os.name == "nt":
        with open(os.path.join(d, "checkpoint.json"), encoding="utf-8"):   # no FILE_SHARE_DELETE: replace fails
            ck.record_result(tests[1], "FAIL", "boom", 1.0)
            ck.pause("user")
    else:  # simulate the skipped replace: the main file keeps record_start(b), the .tmp holds the newest save
        ck.record_result(tests[1], "FAIL", "boom", 1.0)
        ck.pause("user")
        stale = dict(ck.data, state="running", in_flight={"id": "b"}, results=ck.data["results"][:1])
        with open(os.path.join(d, "checkpoint.json"), "w", encoding="utf-8") as f:
            json.dump(stale, f)
        with open(os.path.join(d, "checkpoint.json.tmp"), "w", encoding="utf-8") as f:
            json.dump(ck.data, f)
    disk = Checkpoint.load(d)
    assert disk.state == "paused" and ids(disk) == ["a", "b"] and disk.in_flight is None, (disk.state, ids(disk))
    assert "Paused run" in checkpoint.summary_text(disk.summary())


def t_finished_run_resumes_to_done(tmp):
    """F2: every test has a result but finish('done') never ran - the resume marks it done without the checks."""
    b = tmp.bench()
    ran = []
    tests = [fake(x, ran=ran) for x in "abc"]

    def on_result(t, *_):
        if t["id"] == "c":
            raise KeyboardInterrupt      # e.g. the second Ctrl+C in the refresh after the last test
    runner.REGISTRY[:] = tests
    ck = runner.start_run(b, tests, "finished")
    try:
        runner.continue_run(b, ck, resuming=False, on_result=on_result)
        raise AssertionError("KeyboardInterrupt should escape")
    except KeyboardInterrupt:
        pass
    assert Checkpoint.load(ck.out_dir).state == "paused" and ids(Checkpoint.load(ck.out_dir)) == ["a", "b", "c"]
    n = len(STUBS.checks)
    ck2 = resume_run(tmp.bench(), ck.out_dir)
    assert ck2.state == "done" and len(STUBS.checks) == n and ran == ["a", "b", "c"], (ck2.state, STUBS.checks[n:])
    assert not RunLock.held(ck.out_dir)
    # an interrupted one (power-off: still 'running', lock free) too
    ck3 = Checkpoint.load(ck.out_dir)
    ck3.data["state"] = "running"
    checkpoint.atomic_write_json(os.path.join(ck.out_dir, "checkpoint.json"), ck3.data)
    assert Checkpoint.load(ck.out_dir).summary()["interrupted"]
    ck4 = resume_run(tmp.bench(), ck.out_dir)
    assert ck4.state == "done" and len(STUBS.checks) == n


def t_navicore_silent_blocks_on_navicore(tmp):
    """F3: a held NaviCore port blocks (never escapes as SerialException), naming NaviCore, not the mesh."""
    import serial
    b = tmp.bench({"wcb1": {"port": "COMFAKE1", "kind": "wcb", "wcb": 1},
                   "navicore": {"port": "COMNAV", "kind": "navicore"}})
    ck = Checkpoint.new(os.path.join(tmp.root, "nav"), [], "nav", None, b)
    ck.data["devices"] = {"navicore": {"serial": "S5"}}

    def dev(name):
        raise serial.SerialException("could not open port 'COMNAV': PermissionError(13, 'Access is denied.')")
    b.dev = dev
    try:
        resume.wait_mesh(b, ck, lambda s: None, timeout=0)
        raise AssertionError("a held NaviCore port must block")
    except resume.ResumeBlocked as e:
        assert all(x in str(e) for x in ("did not answer GET_WCB_STATUS", "Arduino IDE", "(same USB serial)")), e
    # NaviCore answers but misses a board: the mesh wording stays
    old = resume.NaviCore
    resume.NaviCore = lambda d: type("N", (), {"online_ids": lambda self: set()})()
    b.dev = lambda name: object()
    try:
        resume.wait_mesh(b, ck, lambda s: None, timeout=0)
        raise AssertionError("a missing board must block")
    except resume.ResumeBlocked as e:
        assert "NaviCore does not see WCB1" in str(e) and "same channel" in str(e), e
    finally:
        resume.NaviCore = old


def t_passing_wire_marked_verified(tmp):
    """F4: a wire restored from the checkpoint that passes its check is recorded as verified for the next resume."""
    b = tmp.bench({"wcb1": {"port": "COMFAKE1", "kind": "wcb", "wcb": 1},
                   "probe1": {"port": "COMP1", "kind": "probe", "mac": "AA:BB"}})
    canon = [{"wcb": 1, "port": "S2", "probe": "probe1", "header": "A", "swap": False, "tap": False}]
    b.links.restore(canon)
    assert not b.links.all()[0].verified
    os.makedirs(os.path.join(tmp.root, "wires"))
    ck = Checkpoint.new(os.path.join(tmp.root, "wires"), [], "wires", None, b)
    ck.data["links"]["verified"] = ["W1S2"]
    b.links.check = lambda link, swap=None: True
    b.port_baud = lambda w, p: 9600
    resume.check_wires(b, ck, lambda s: None)
    assert b.links.all()[0].verified
    assert json.loads(read(b.links.path))["links"][0]["verified"] is True, "links.json must be saved"
    ck.begin_segment(usb={}, firmware={}, bench=b)
    assert ck.data["links"]["verified"] == ["W1S2"], ck.data["links"]


class FakeJsonDev:
    """The SBUS controller's JSON port: pong to a ping, its cfg (WiFi passwords included) to getcfg."""
    CFG = {"e": "cfg", "fwver": "x", "btn": [{"ch": 7}, {"ch": 7}], "tr": [{"m": 1}, {"m": 0}, {"m": 1}],
           "wifiNets": [{"s": "DomeNet", "p": "sekrit99"}]}

    def __init__(self, logged):
        self.lines, self.sent, self.logged = [], [], logged
        self.log = lambda name, direction, text: logged.append(text)

    def mark(self):
        return len(self.lines)

    def _rx(self, text):
        self.lines.append(text)
        if self.log:
            self.log("sbus", "<", text)

    def send(self, text, eol="\n"):
        self.sent.append(json.loads(text))
        if '"ping"' in text:
            self._rx('{"t":"pong","fwver":"sbus-1.2"}')
        elif '"getcfg"' in text:
            self._rx(json.dumps(self.CFG, separators=(",", ":")))

    def expect(self, pattern, timeout=3.0, since=None):
        import re
        for line in self.lines[since or 0:]:
            m = re.search(pattern, line)
            if m:
                return m
        raise resume.ExpectTimeout(f"sbus: no line matching /{pattern}/")


def t_sbus_released_after_cutoff(tmp):
    """F5: a cut-off sbus.* test's held stick, buttons and button-mode trims are released; the cfg is never logged
    with its WiFi passwords."""
    b = tmp.bench({"sbus": {"port": "COMS", "kind": "sbus"}})
    logged = []
    d = FakeJsonDev(logged)
    b.dev = lambda name: d
    ck = Checkpoint.new(os.path.join(tmp.root, "sbus"), [], "sbus", None, b)
    ck.data["in_flight"] = {"id": "sbus.to_navicore"}
    out = resume.check_controllers(b, ck, lambda s: None)
    assert out == {"sbus": "sbus-1.2"}
    sent = d.sent[1:]
    assert sent[0] == {"t": "getcfg"} and sent[1] == {"t": "a", "lx": 0, "ly": 0, "rx": 0, "ry": 0}, sent
    assert sent[2:] == [{"t": "btn", "i": 0, "p": False}, {"t": "btn", "i": 1, "p": False},
                        {"t": "tr", "i": 0, "d": 1, "p": False}, {"t": "tr", "i": 2, "d": 1, "p": False}], sent
    assert not any("sekrit99" in x for x in logged) and any('"e":"cfg"' in x for x in logged), logged
    assert d.log is not None and d.log("sbus", "<", "sekrit99") is None and logged[-1] == "sekrit99", \
        "the device's own log is restored afterwards"
    # no cut-off sbus test: ping only
    d2 = FakeJsonDev([])
    b.dev = lambda name: d2
    ck.data["in_flight"] = {"id": "wcb.version"}
    resume.check_controllers(b, ck, lambda s: None)
    assert d2.sent == [{"t": "ping"}], d2.sent


def t_wizard_abort_kills_tree(tmp):
    """F7/F11: during a Wizard test a Ctrl+C lands at once, kills node's tree and is not replaced by _reacquire's
    failure; the unit-test node runs in its own process group."""
    import _thread
    from types import SimpleNamespace
    from hil import wizard
    b = tmp.bench()
    b.new_session()
    fake_root = os.path.join(tmp.root, "wizard")
    os.makedirs(os.path.join(fake_root, "node_modules", "@playwright", "test"))
    open(os.path.join(fake_root, "node_modules", "@playwright", "test", "cli.js"), "w").close()
    procs = []

    def popen(cmd, **kw):
        if cmd and cmd[0] == "taskkill":           # _kill_tree's own taskkill (R2-2): the real one
            return subprocess.Popen(cmd, **kw)
        p = subprocess.Popen([sys.executable, "-c", "import time; print('[1/1] started', flush=True); time.sleep(30)"],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             creationflags=kw.get("creationflags", 0))
        procs.append(p)
        return p

    class Bridge:
        url = "http://127.0.0.1:0"

        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

    def reacquire(bench, device, timeout=25.0):
        raise AssertionError(f"{device} did not come back after the Wizard test")
    runs = []

    def fake_run(cmd, **kw):
        runs.append(kw)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    saved = {n: getattr(wizard, n) for n in ("subprocess", "shutil", "WIZARD_TESTS", "usb_ids", "Bridge", "_reacquire")}
    wizard.subprocess = SimpleNamespace(Popen=popen, PIPE=subprocess.PIPE, STDOUT=subprocess.STDOUT,
                                        DEVNULL=subprocess.DEVNULL,
                                        TimeoutExpired=subprocess.TimeoutExpired,
                                        CompletedProcess=subprocess.CompletedProcess,
                                        run=lambda cmd, **kw: fake_run(cmd, **kw) if "--test" in cmd
                                        else subprocess.run(cmd, **kw),
                                        CREATE_NEW_PROCESS_GROUP=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    wizard.shutil = SimpleNamespace(which=lambda n: sys.executable)
    wizard.WIZARD_TESTS = fake_root
    wizard.usb_ids = lambda com: (0x10C4, 0xEA60)
    wizard.Bridge = Bridge
    wizard._reacquire = reacquire
    timer = threading.Timer(1.0, _thread.interrupt_main)
    t0 = time.monotonic()
    try:
        timer.start()
        try:
            wizard.run_wizard_test(b, "wizard.fake", device="wcb1", timeout=60)
            raise AssertionError("the Ctrl+C must escape")
        except KeyboardInterrupt:
            pass
        took = time.monotonic() - t0
        assert took < 5, f"the Ctrl+C took {took:.1f}s to land"
        deadline = time.monotonic() + 5
        while procs[0].poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert procs[0].poll() is not None, "node's tree must be killed on the abort"
        b.sync_log()
        log = read(os.path.join(b.out_dir, "session.log"))
        assert "[1/1] started" in log and "not reacquired after the abort" in log, log
        # F11: node --test gets its own process group too
        wizard.run_unit_tests(b)
        assert runs and runs[0].get("creationflags") == getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0), runs
        assert wizard._LIVE is None, "the finished test is no longer the live one"
        # R2-3: kill_live ends the test in flight (gui.py's close-now)
        live = popen(["node"])
        wizard._LIVE = live
        wizard.kill_live()
        live.wait(timeout=10)
        assert live.poll() is not None
        wizard._LIVE = None
        wizard.kill_live()                          # nothing in flight: a no-op
    finally:
        timer.cancel()
        for p in procs:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=10)
            p.stdout.close()
        for n, v in saved.items():
            setattr(wizard, n, v)
        b.close()


def _run_py_functions(*names):
    """make_ask / install_pause_handler from run.py without importing it (its import loads the real suites)."""
    import ast
    import signal
    src = read(os.path.join(HERE, "run.py"))
    tree = ast.parse(src)
    mod = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names],
                     type_ignores=[])
    ns = {"sys": sys, "os": os, "signal": signal}
    exec(compile(mod, "run.py", "exec"), ns)
    return [ns[n] for n in names]


class _TtyInput:
    """stdin that claims to be a terminal: Windows reports NUL as a character device, so isatty() is True."""
    def __init__(self, text):
        import io
        self.buf = io.StringIO(text)

    def isatty(self):
        return True

    def readline(self, *a):
        return self.buf.readline(*a)

    def read(self, *a):
        return self.buf.read(*a)


def t_cli_ask_and_handler(tmp):
    """F12/F13: a question on stdin=NUL takes its default and names --force; the SIGINT handler is reentrant-safe."""
    import contextlib
    import io
    import signal
    make_ask, install_pause_handler = _run_py_functions("make_ask", "install_pause_handler")
    old_in = sys.stdin
    try:
        sys.stdin = _TtyInput("")            # EOF at once, as NUL gives
        ask = make_ask(False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert ask("Reboot the WCBs first?", "text", True) is True, "the reboot's default is Yes"
            assert ask.unanswered
            assert ask("The bench changed", "text", False) is False
        assert "rerun with --force" in out.getvalue(), out.getvalue()
        sys.stdin = _TtyInput("y\n")
        ask = make_ask(False)
        with contextlib.redirect_stdout(io.StringIO()):
            assert ask("The bench changed", "text", False) is True and not ask.unanswered
    finally:
        sys.stdin = old_in
    # a nested request inside the held lock must not deadlock (the handler lands right after __enter__)
    rc = runner.RunControl()
    done = threading.Event()

    def nested():
        with rc._lock:
            rc.request_pause("ctrl_c")
            assert rc.pausing
        done.set()
    th = threading.Thread(target=nested, daemon=True)
    th.start()
    th.join(2)
    assert done.is_set(), "RunControl must be reentrant"
    # the handler itself: first call pauses, second raises KeyboardInterrupt
    old, old_err = signal.getsignal(signal.SIGINT), sys.stderr
    try:
        sys.stderr = io.StringIO()           # no fileno(): the handler's raw write is skipped, not printed
        rc = runner.RunControl()
        install_pause_handler(rc)
        handler = signal.getsignal(signal.SIGINT)
        with rc._lock:
            handler(signal.SIGINT, None)
        assert rc.should_pause() == "ctrl_c"
        try:
            handler(signal.SIGINT, None)
            raise AssertionError("the second Ctrl+C must raise KeyboardInterrupt")
        except KeyboardInterrupt:
            pass
    finally:
        signal.signal(signal.SIGINT, old)
        sys.stderr = old_err


def t_ctrl_c_during_checks_cancels(tmp):
    """F14: a Ctrl+C during the resume checks is a cancel (ResumeAborted); an interrupted run stays 'interrupted'."""
    b = tmp.bench()
    tests = [fake("a"), fake("b")]
    runner.REGISTRY[:] = tests
    ck = runner.start_run(b, tests, "ctrlc")
    ck.release()                              # still 'running' with the lock free: interrupted
    STUBS.check_raises = KeyboardInterrupt()
    try:
        resume_run(tmp.bench(), ck.out_dir)
        raise AssertionError("ResumeAborted expected")
    except resume.ResumeAborted as e:
        assert "Ctrl+C" in str(e)
    disk = Checkpoint.load(ck.out_dir)
    assert disk.state == "paused" and disk.data["reason"] == "interrupted", (disk.state, disk.data["reason"])
    assert not RunLock.held(ck.out_dir)


def t_pause_file_old_mtime(tmp):
    """F15: a PAUSE file copied in keeps its old mtime and must still pause; an undeletable stale one is ignored."""
    import stat
    b = tmp.bench()
    tests = [fake("a")]
    runner.REGISTRY[:] = tests
    ck = runner.start_run(b, tests, "pausefile")
    ck.clear_stale_pause_files()
    p = os.path.join(ck.out_dir, "PAUSE")
    open(p, "w").close()
    old = time.time() - 3 * 86400
    os.utime(p, (old, old))
    assert ck.pause_file_present() == "pause_file" and not os.path.exists(p)
    if os.name == "nt":
        open(p, "w").close()
        os.chmod(p, stat.S_IREAD)           # os.remove raises PermissionError on a read-only file on Windows
        notes = []
        try:
            ck.clear_stale_pause_files(notes.append)
            assert os.path.exists(p) and any("could not delete" in n for n in notes), notes
            assert ck.pause_file_present() is None and ck.pause_file_present() is None
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            with open(p, "w") as f:
                f.write("again")
            os.utime(p, (time.time() + 5, time.time() + 5))
            assert ck.pause_file_present() == "pause_file"
        finally:
            if os.path.exists(p):
                os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
    ck.release()


def t_redaction_free_text(tmp):
    """F16/A1: the password and WiFi credentials never reach checkpoint.json or report.md through free text."""
    from hil.wcb import split_checked_chain
    rt = checkpoint.redact_text
    chain = "?HW,24^?WCB,2^?WIFI,AP,DomeNet,sekrit99^?EPASS,hunter2^?CMDCHAR,;"
    red = rt(chain)
    assert not any(s in red for s in SECRETS) and red.endswith("^?CMDCHAR,;") and "?HW,24^?WCB,2^" in red, red
    assert rt(red) == red, "idempotent"
    assert rt("?EPASS,hunter2") == checkpoint.redact_token("?EPASS,hunter2"), "same hash as the config_ref form"
    for text in ("W1: missing ['?EPASS,hunter2'] / extra ['?EPASS,x']", "Password: hunter2",
                 "ESP-NOW Password: hunter2", 'got {"e":"cfg","wifiNets":[{"s":"DomeNet","p":"sekrit99"}]}',
                 "last lines:\n    ?epass,hunter2\n    !WIFI,JOIN,DomeNet,sekrit99",
                 "probe2: 'MESH JOIN ID=11 OCT2=AB OCT3=CD QTY=9 CHAN=1 CHK=1 TEMP=1 TYPE=HILProbe PASS=hunter2' -> "
                 "ERR already joined"):
        assert not any(s in rt(text) for s in SECRETS), rt(text)
    assert "extra ['?EPASS,<redacted:" in rt("W1: missing ['?EPASS,hunter2'] / extra ['?EPASS,x']")
    assert rt("AP password   : set") == "AP password   : set" and rt("?WIFI,OFF") == "?WIFI,OFF"
    assert rt("MESH JOIN ... PASS=<pw>") == "MESH JOIN ... PASS=<pw>", "the usage text is left alone"
    # a truncated ?MGMT,PULL chain: the snapshot note, the block, the checkpoint and the report stay clean
    b = tmp.bench()
    ctl = runner.RunControl()
    ck = new_run(b, [fake(x) for x in "abcde"] + [fake("f", lambda bench: ctl.request_pause("user"))] + [fake("g")],
                 control=ctl)

    def bad_read(bench, wcb):
        split_checked_chain(chain)          # raises 'not a checksummed chain ... <tail>'
    old = resume.read_config
    b2 = tmp.bench()
    b2.open_session(ck.out_dir, 2)
    try:
        resume.read_config = bad_read
        ORIG["snapshot_configs"](b2)
        try:
            resume._config_diffs(b2, Checkpoint.load(ck.out_dir))
            raise AssertionError("an unreadable config must block")
        except resume.ResumeBlocked as e:
            disk = Checkpoint.load(ck.out_dir)
            disk.block(e)
    finally:
        resume.read_config = old
        b2.close()
    # a test failing with an ExpectTimeout tail around ?backup / ?config, and a pause text quoting it
    disk.record_result({"id": "g", "title": "g"}, "FAIL",
                       "config could not be re-read (wcb1: no line matching /x/; last lines:\n    ?HW,24^?EPASS,hunter2"
                       "^?WIFI,JOIN,DomeNet,sekrit99\n    Password: hunter2)", 1.0)
    disk.record_outage({"id": "g"}, "usb_loss", "NOT A RESULT - ... ESP-NOW Password: hunter2")
    disk.pause("host_outage", text="the host lost the bench: ?EPASS,hunter2")
    for name in ("checkpoint.json", "report.md"):
        text = read(os.path.join(ck.out_dir, name))
        leaked = [s for s in SECRETS if s in text]
        assert not leaked, f"{name} contains {leaked}"
    assert "Last resume attempt blocked" in read(os.path.join(ck.out_dir, "report.md"))
    log = read(os.path.join(ck.out_dir, "session.log"))
    assert "config snapshot of W1 failed" in log and not any(s in log for s in SECRETS), "the added log line is clean"


def t_added_tests_listed(tmp):
    """F17: a run started with a selection (the GUI's Run everything passes []) lists tests added since, on resume."""
    b = tmp.bench()
    ctl = runner.RunControl()
    tests = [fake("a", lambda bench: ctl.request_pause("user")), fake("b")]
    runner.REGISTRY[:] = tests
    ck = runner.start_run(b, tests, "everything", selectors=[])
    runner.continue_run(b, ck, resuming=False, should_stop=ctl.should_stop, should_pause=ctl.should_pause)
    assert ck.state == "paused"
    runner.REGISTRY[:] = tests + [fake("new")]
    ck2 = resume_run(tmp.bench(), ck.out_dir)
    assert ck2.state == "done"
    rep = read(os.path.join(ck.out_dir, "report.md"))
    assert "`new` — added to the suite since this run started" in rep, rep


def t_finished_run_with_dropped(tmp):
    """R2-6: a paused run whose only unfinished tests were renamed or removed ends done - listing them as not run."""
    b = tmp.bench()
    ctl = runner.RunControl()
    tests = [fake("a"), fake("b", lambda bench: ctl.request_pause("user")), fake("c")]
    ck = new_run(b, tests, control=ctl)
    assert ck.state == "paused" and ids(ck) == ["a", "b"]
    runner.REGISTRY[:] = tests[:2]                   # 'c' renamed or removed while paused
    n = len(STUBS.checks)
    ck2 = resume_run(tmp.bench(), ck.out_dir)
    assert ck2.state == "done" and len(STUBS.checks) == n and ck2.data["dropped"] == ["c"], (ck2.state, ck2.data)
    rep_md = read(os.path.join(ck.out_dir, "report.md"))
    assert "`c` — no longer in the suite" in rep_md, rep_md


def t_start_closes_recording_ports(tmp):
    """R2-7: the start-of-run firmware record opens NaviCore and the probes; it closes what it opened, and leaves a
    port that was already open alone."""
    b = tmp.bench({"wcb1": {"port": "COMFAKE1", "kind": "wcb", "wcb": 1},
                   "navicore": {"port": "COMNAV", "kind": "navicore"}})
    before = FakeDev("wcb1")
    b.devs["wcb1"] = before
    opened = []

    def record(bench):
        for n in ("wcb1", "navicore"):
            if n not in bench.devs:
                bench.devs[n] = FakeDev(n)
                opened.append(bench.devs[n])
        return {"wcb1": "6.2.1_TEST"}
    old = resume.record_firmware
    resume.record_firmware = record
    try:
        tests = [fake(x) for x in "abcde"]         # >= SNAPSHOT_MIN_TESTS, so the recording runs
        runner.REGISTRY[:] = tests
        ck = runner.start_run(b, tests, "ports")
    finally:
        resume.record_firmware = old
    assert [d.name for d in opened] == ["navicore"] and not opened[0].active, opened
    assert "navicore" not in b.devs and b.devs.get("wcb1") is before and before.active, b.devs
    runner.continue_run(b, ck, resuming=False)
    assert ck.state == "done"


def t_vendored_softserial_in_lockstep(tmp):
    """The probe firmware bundles a copy of the WCB's patched EspSoftwareSerial (tracker #78) - Arduino copies a sketch
    into its build folder, so it cannot include the WCB's by a relative path. The two must stay byte-identical."""
    import filecmp
    wcb = os.path.normpath(os.path.join(HERE, "..", "..", "Code", "WCB", "src", "EspSoftwareSerial"))
    probe = os.path.join(HERE, "wcb_probe", "src", "EspSoftwareSerial")
    assert os.path.isdir(wcb) and os.path.isdir(probe), (wcb, probe)

    def files(root):
        return sorted(os.path.relpath(os.path.join(d, f), root) for d, _, fs in os.walk(root) for f in fs)
    a, b = files(wcb), files(probe)
    assert a == b, f"the copies hold different files: WCB {a} / probe {b}"
    differ = [f for f in a if not filecmp.cmp(os.path.join(wcb, f), os.path.join(probe, f), shallow=False)]
    assert not differ, f"tests/hil/wcb_probe/src/EspSoftwareSerial differs from Code/WCB/src/EspSoftwareSerial in {differ}"


# ---------------------------------------------------------------------------- probe restarts (tracker #78)
PROBE_BOOT = "BOOT wcb_probe 7 mac=AA:BB:CC:DD:EE:01"


class FakeProbeDev:
    """A wcb_probe on a fake port: answers the protocol lines the harness sends, and can print a panic or drop its
    port. What the runner reads of a SerialDevice (active, connected, last_error_at, close) is here too.

    Channels own pins the way probe_main.cpp does: BIND first unbinds its own channel, then answers 'ERR pin in use'
    when another channel still holds one of the header's pins (bindChannel); UNBIND, RESET and a restart release them,
    and TX on an unbound channel is refused. held is {channel: header}, to compare with what the host thinks."""
    def __init__(self, name="probe1"):
        self.name, self.port = name, "COMP1"
        self.lines, self.sent = [], []
        self.active, self.connected, self.last_error_at = True, True, None
        self.pins = {}          # channel -> set of (header, "TX"|"RX") it holds
        self.held = {}          # channel -> header
        self.reset_answer = "OK RESET"

    def _bind(self, tok):
        ch, header = tok[1], tok[2]
        self.pins.pop(ch, None)
        self.held.pop(ch, None)
        swap, rx_only = "SWAP" in tok, "RXONLY" in tok
        want = {(header, "TX" if swap else "RX")}
        if ch in "AB" or not rx_only:
            want.add((header, "RX" if swap else "TX"))
        if any(want & other for other in self.pins.values()):
            return "ERR pin in use"
        self.pins[ch], self.held[ch] = want, header
        return f"OK BIND {ch} {header}"

    def _restarted(self):
        self.pins.clear()
        self.held.clear()

    def _rx(self, text):
        self.lines.append((time.monotonic(), text))

    def mark(self):
        return len(self.lines)

    def since(self, mark):
        return [t for _, t in self.lines[mark:]]

    def close(self):
        self.active = False

    def send(self, text, eol="\n"):
        self.sent.append(text)
        verb = text.split()[0]
        if verb == "HELLO":
            self._rx(PROBE_BOOT.replace("BOOT", "HELLO") + " mesh=0")
        elif text == "MESH LEAVE":
            self._rx("OK MESH LEAVE rebooting")
            self._restarted()
            self._rx("\x00\x00\x00" + PROBE_BOOT)        # the ROM banner glued to the front, as on the bench
        elif verb == "BIND":
            self._rx(self._bind(text.split()))
        elif verb == "UNBIND":
            self.pins.pop(text.split()[1], None)
            self.held.pop(text.split()[1], None)
            self._rx("OK UNBIND")
        elif verb == "RESET":
            if self.reset_answer.startswith("OK"):
                self._restarted()
            self._rx(self.reset_answer)
        elif verb == "TX" and text.split()[1] not in self.held:
            self._rx("ERR channel not bound")
        else:
            self._rx(f"OK {verb}")

    def expect(self, pattern, timeout=3.0, since=None):
        import re
        for _, text in self.lines[since or 0:]:
            m = re.search(pattern, text)
            if m:
                return m
        raise resume.ExpectTimeout(f"{self.name}: no line matching /{pattern}/")

    def panic(self):
        """What wcb_probe 6 printed when a stale level arm fired: panics, then (ROM banner glued on) its boot line."""
        for _ in range(2):
            self._rx("Guru Meditation Error: Core  1 panic'ed (Interrupt wdt timeout on CPU1). ")
            self._rx("Rebooting...")
        self._restarted()
        self._rx("\x00\x00" + PROBE_BOOT)

    def reopen(self, reset):
        """The port dropped and serialdev reopened it: no BOOT line reaches the log either way (serialdev.py _reopen).
        reset=False is a re-enumeration with the chip still running, so it keeps every channel."""
        if reset:
            self._restarted()
        self._rx("<<serial error: ClearCommError failed>>")
        self._rx(f"<<reopened {self.port}>>")

    def binds(self, ch=None):
        return sum(1 for s in self.sent if s.startswith(f"BIND {ch} " if ch else "BIND "))


def _probe_bench(tmp):
    b = tmp.bench({"wcb1": {"port": "COMFAKE1", "kind": "wcb", "wcb": 1},
                   "probe1": {"port": "COMP1", "kind": "probe", "mac": "AA:BB:CC:DD:EE:01"}})
    d = FakeProbeDev()
    b.devs["probe1"] = d                  # Bench.dev() hands this out instead of opening a port
    b.port_baud = lambda w, p: 9600       # auto-baud links need no WCB config read
    return b, d


def t_probe_reboot_rebinds(tmp):
    """A probe restart (planned or not) forgets its channels, so the harness must too: bind() no longer trusts a
    cached binding the probe printed a boot or panic line after, a read across the restart fails with the reason
    instead of returning b'', and only an unplanned restart counts as one (MESH LEAVE's does not; lines from before
    the first RESET do not)."""
    b, d = _probe_bench(tmp)
    d._rx(PROBE_BOOT)                     # it booted before the harness took it over: not a test's business
    p = b.probe("probe1")
    assert p.unplanned_reboots() == [], p.unplanned_reboots()
    link = b.links.set_link(1, "S3", "probe1", "S3")
    link.listen()
    assert link.channel == "C" and d.binds() == 1, (link.channel, d.sent)
    link.listen()
    assert d.binds() == 1, "an unchanged binding is reused"
    # a planned restart: MESH LEAVE. Not unplanned - but the binding is gone all the same, so the next listen re-binds
    p.mesh_leave()
    assert p.unplanned_reboots() == [], p.unplanned_reboots()
    assert b.links.rebooted_since_bind(link) is not None
    link.listen()
    assert d.binds() == 2 and link.channel == "C", (d.binds(), link.channel)
    # an unplanned panic after a read mark: the read fails with the reason (not b''), and the link is bound again
    m = link.mark()
    d.panic()
    hits = p.unplanned_reboots()
    assert [h[2] for h in hits][:1] and "Guru Meditation" in hits[0][2], hits
    assert any("BOOT wcb_probe" in h[2] for h in hits), "the boot after a panic is not a planned one"
    try:
        link.received(m)
        raise AssertionError("a read across a probe panic must fail")
    except AssertionError as e:
        assert "W1S3: probe1 panicked at t=" in str(e) and "nothing it received" in str(e), e
    assert d.binds() == 3 and link.channel == "C", (d.binds(), link.channel)
    assert link.received(link.mark()) == b"", "reads after the re-bind work again"
    # a send after another panic re-binds silently first (no mark to protect)
    d.panic()
    link.send(b"x")
    assert d.binds() == 4 and d.sent[-1].startswith("TX C "), d.sent[-2:]


def t_runner_fails_test_on_probe_panic(tmp):
    """The runner scans every open probe after each test: an unplanned restart fails THAT test with 'probe1 panicked
    at t=...' (a result, not a harness crash) and forgets the probe, so the next test binds afresh and passes. A
    planned MESH LEAVE and the first-use RESET do not; the run still finishes 'done', with the checkpoint intact."""
    b, d = _probe_bench(tmp)
    d._rx(PROBE_BOOT)
    seen = {}

    def first_use_and_leave(bench):          # opens the probe (first-use RESET) and leaves the mesh: both planned
        bench.links.set_link(1, "S3", "probe1", "S3").listen()
        bench.probe("probe1").mesh_leave()

    def panics(bench):
        link = bench.links.get(1, "S3")
        link.listen()
        seen["bound_before_panic"] = link.channel
        d.panic()                            # the test itself notices nothing and passes

    def next_test(bench):
        link = bench.links.get(1, "S3")
        seen["channel_at_start"] = link.channel
        n = d.binds()
        link.listen()
        seen["rebound"] = d.binds() == n + 1

    tests = [fake("probe.a", first_use_and_leave), fake("probe.b", panics), fake("probe.c", next_test)]
    ck = new_run(b, tests)
    assert ck.state == "done", ck.state
    res = {r["id"]: r for r in ck.data["results"]}
    assert res["probe.a"]["status"] == "PASS", res["probe.a"]
    assert res["probe.b"]["status"] == "FAIL", res["probe.b"]
    detail = res["probe.b"]["detail"]
    assert detail.startswith("probe1 panicked at t=") and "2 panics in all" in detail, detail
    assert "The test itself passed" in detail, detail
    assert res["probe.c"]["status"] == "PASS", res["probe.c"]
    assert seen == {"bound_before_panic": "C", "channel_at_start": None, "rebound": True}, seen
    on_disk = Checkpoint.load(ck.out_dir)
    assert [r["status"] for r in on_disk.data["results"]] == ["PASS", "FAIL", "PASS"], on_disk.data["results"]


def _in_step(bench, d):
    """The host's channel table matches what the fake probe really holds."""
    host = {ch: l.header for ch, l in bench.links.owners.get(d.name, {}).items()}
    assert host == d.held, f"the host thinks {host}, the probe holds {d.held}"


def t_probe_restart_forgets_only_what_it_lost(tmp):
    """A restart forgets only the bindings it destroyed. A wire bound after it keeps its channel, the end-of-test RESET
    leaves probe and host agreeing on nothing bound, and when that RESET fails the fallback keeps the live bindings - so
    the next test binding the same wires in the other order never meets 'ERR pin in use' (the fake enforces pin
    ownership as probe_main.cpp bindChannel does). A wire forgotten by another wire's bind still reports the restart."""
    b, d = _probe_bench(tmp)
    d._rx(PROBE_BOOT)
    b.links.set_link(1, "S3", "probe1", "S3")
    b.links.set_link(1, "S4", "probe1", "S4")
    seen = {}

    def wires(bench):
        return bench.links.get(1, "S3"), bench.links.get(1, "S4")

    def panics_mid_read(bench):           # both bound; a read across the panic; the other re-bound in a finally
        a, w = wires(bench)
        a.listen()
        w.listen()
        m = a.mark()
        d.panic()
        try:
            a.received(m)
        finally:
            w.listen()
            _in_step(bench, d)

    def opposite_order(bench):            # the end-of-test RESET left nothing bound on either side
        _in_step(bench, d)
        seen["after_reset"] = dict(d.held)
        a, w = wires(bench)
        w.listen()
        a.listen()
        seen["opposite"] = (w.channel, a.channel)
        _in_step(bench, d)

    def bound_after_panic(bench):         # W bound after the panic keeps its channel when A notices the panic
        a, w = wires(bench)
        bench.links.release(w)
        m = a.mark()
        d.panic()
        w.listen()
        ch, n = w.channel, d.binds(w.channel)
        try:
            a.received(m)
            raise RuntimeError("a read across the panic must fail")
        except AssertionError as e:
            seen["msg"] = str(e)
        seen["kept"] = (w.channel == ch, d.binds(ch) == n)
        _in_step(bench, d)

    def reset_fails(bench):               # a stale A and a live W at the end, and the probe refuses RESET
        a, w = wires(bench)
        bench.links.release(w)
        a.listen()
        d.panic()
        w.listen()
        seen["live"] = w.channel
        d.reset_answer = "ERR busy"

    def after_fallback(bench):
        d.reset_answer = "OK RESET"
        _in_step(bench, d)
        a, w = wires(bench)
        seen["fallback"] = (a.channel, w.channel)
        a.listen()
        w.listen()
        _in_step(bench, d)

    def forgotten_by_other(bench):        # W's bind notices the panic first and forgets A too; A's read still says why
        a, w = wires(bench)
        a.listen()
        w.listen()
        m = a.mark()
        d.panic()
        w.listen()
        try:
            a.received(m)
            raise RuntimeError("a read across the panic must fail")
        except AssertionError as e:
            seen["forgotten_msg"] = str(e)
        _in_step(bench, d)

    tests = [fake("r.a", panics_mid_read), fake("r.b", opposite_order), fake("r.c", bound_after_panic),
             fake("r.d", reset_fails), fake("r.e", after_fallback), fake("r.f", forgotten_by_other)]
    ck = new_run(b, tests)
    assert ck.state == "done", ck.state
    res = {r["id"]: r for r in ck.data["results"]}
    status = {k: v["status"] for k, v in res.items()}
    assert status == {"r.a": "FAIL", "r.b": "PASS", "r.c": "FAIL", "r.d": "FAIL", "r.e": "PASS", "r.f": "FAIL"}, res
    for k in ("r.a", "r.c", "r.d", "r.f"):
        assert res[k]["detail"].startswith("probe1 panicked at t="), res[k]["detail"]
    assert "ERR pin in use" not in json.dumps(ck.data["results"]), ck.data["results"]
    assert seen["after_reset"] == {} and seen["opposite"] == ("C", "D"), seen
    assert "W1S3: probe1 panicked at t=" in seen["msg"], seen["msg"]
    assert seen["kept"] == (True, True), seen["kept"]
    assert seen["fallback"] == (None, seen["live"]), seen
    assert "W1S3: probe1 panicked at t=" in seen["forgotten_msg"] and "taken over" not in seen["forgotten_msg"], seen


def t_probe_port_reopen_counts_as_restart(tmp):
    """A probe whose USB port dropped and reopened prints no BOOT line the harness can see, so '<<reopened' counts as a
    restart: a read across it fails with the reason, the stale channels are UNBOUND on the probe too (a re-enumeration
    with no reset keeps them, and the next bind would meet 'ERR pin in use'), the runner fails the test and RESETs the
    probe - and a reopen before the first RESET is not a test's business."""
    b, d = _probe_bench(tmp)
    d._rx(PROBE_BOOT)
    d.reopen(reset=True)
    p = b.probe("probe1")
    assert p.unplanned_reboots() == [], p.unplanned_reboots()
    a = b.links.set_link(1, "S3", "probe1", "S3")
    w = b.links.set_link(1, "S4", "probe1", "S4")
    a.listen()
    w.listen()
    assert (a.channel, w.channel) == ("C", "D"), (a.channel, w.channel)
    m = a.mark()
    d.reopen(reset=False)                 # re-enumerated with the chip running: it still holds C on S3 and D on S4
    hit = b.links.rebooted_since_bind(a)
    assert hit is not None and hit[2].startswith("<<reopened"), hit
    w.listen()                            # notices first: UNBINDs C and D on the probe, then binds W on C
    assert w.channel == "C" and "UNBIND D" in d.sent, (w.channel, d.sent[-6:])
    _in_step(b, d)
    try:
        a.received(m)
        raise RuntimeError("a read across a port reopen must fail")
    except AssertionError as e:
        assert "W1S3: probe1 lost its USB port" in str(e) and "nothing it received" in str(e), e
    assert a.channel == "D", a.channel
    _in_step(b, d)

    def reopens(bench):
        bench.links.get(1, "S3").listen()
        d.reopen(reset=True)              # the test itself notices nothing and passes

    def next_test(bench):
        _in_step(bench, d)
        bench.links.get(1, "S4").listen()
        bench.links.get(1, "S3").listen()
        _in_step(bench, d)

    resets = d.sent.count("RESET")
    ck = new_run(b, [fake("u.a", reopens), fake("u.b", next_test)])
    res = {r["id"]: r for r in ck.data["results"]}
    assert res["u.a"]["status"] == "FAIL" and res["u.a"]["detail"].startswith("probe1 lost its USB port"), res["u.a"]
    assert res["u.b"]["status"] == "PASS", res["u.b"]
    assert d.sent.count("RESET") == resets + 1, d.sent


# ---------------------------------------------------------------------------- opt-ins and expected durations
# Every gated test and the Skip message it gave before the gates moved onto @test(opt_in=...), captured from the test
# bodies on 2026-09-23. The declared gates must reproduce each one exactly, so reports do not change.
_ERASE = "every accepted BEGIN erases at least 4 KB of the inactive app slot, which nothing can restore"
GATED = {
    "var.cap_persistent_full": ("nvs_wear", "about 100 whole-blob NVS writes"),
    "seq.clear_all_empty_hash": ("seq_wipe", "wipes W1's sequences and replays them"),
    **{f"ota.{n}": ("ota_erase", _ERASE) for n in (
        "local_begin_abort", "local_begin_supersede", "local_cursor_nak_incomplete", "local_truncated_verify_fail",
        "local_bad_magic", "local_overrun", "local_base64_errors", "local_idle_timeout_nak_no_refresh",
        "local_write_refreshes_timeout", "local_baud_bump_abort", "local_baud_invalid", "local_baud_timeout_restore",
        "local_baud_rejected_rebegin_restores", "relay_cursor_dup_gap_wrong_session_abort", "relay_teardown_frame_err",
        "relay_end_incomplete_and_verify_fail", "relay_timeout_keepalive", "cross_transport_remote_abort_kills_local")},
    "ota.local_sha_corrupt_full": ("ota_full", "erases the whole inactive app slot"),
    "ota.local_full_same_image_wcb1": ("ota_full", "erases and rewrites W1's inactive app slot and switches its boot "
                                                   "slot twice"),
    "ota.local_wrong_chip_image": ("ota_wrong_chip", "streams an S3 image to W1; only IDF verify stands between it "
                                                     "and the boot slot"),
    "ota.relay_full_same_image_wcb2": ("ota_full_wcb2", "erases and rewrites W2's inactive app slot and switches its "
                                                        "boot slot twice"),
    "navicore.rec_play_clip": ("navicore_clip", "replays a saved clip: servo motion and recorded actions"),
    "sbus.signal_loss_controller_reset": ("sbus_reset", "reboots the SBUS controller"),
    "softrx.erratum_pairs": ("softrx_erratum", "about 15 minutes of soft-port input into W1 S3-S5; needs wcb_probe 4"),
    "soak.w1s4_wire": ("w1s4_soak", "loads W1's S2/S4 fan-out for soak_minutes, default 20"),
}


def _report_md(folder, rows):
    os.makedirs(folder, exist_ok=True)
    body = "".join(f"| {st} | {tid} | {title} | {dur:.1f}s | {detail} |\n" for st, tid, title, dur, detail in rows)
    with open(os.path.join(folder, "report.md"), "w", encoding="utf-8") as f:
        f.write(f"# HIL run {os.path.basename(folder)}\n\nx\n\n| Result | Test | Title | Time | Detail |\n"
                f"|---|---|---|---|---|\n" + body)


def _checkpoint_json(folder, rows, raw=None):
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "checkpoint.json"), "w", encoding="utf-8") as f:
        f.write(raw if raw is not None else json.dumps(
            {"format": 1, "state": "done", "tests": [r[1] for r in rows],
             "results": [{"id": tid, "title": "t", "status": st, "detail": d, "dur": dur} for st, tid, dur, d in rows]}))


def t_durations(tmp):
    """hil/durations.py: both sources, the median of the newest SAMPLES real results, SKIP and NOT A RESULT left out,
    corrupt files tolerated, and the cache reused, invalidated per folder and never written with write_cache=False."""
    res = tmp.results
    # the oldest run has only report.md (from before checkpoints): a title with a pipe, a NOT A RESULT row, a SKIP row
    _report_md(os.path.join(res, "20260101-000001"), [
        ("PASS", "a", "A | with a pipe", 10.0, ""), ("FAIL", "b", "B", 20.0, "no line matching /x/ \\| 3.0s \\| y"),
        ("ERROR", "c", "C", 99.0, "NOT A RESULT - the host lost every USB serial port at once"),
        ("SKIP", "a", "A", 0.0, "needs a probe wire on W1S3")])
    # a checkpoint run: its report.md is ignored while the checkpoint reads
    _checkpoint_json(os.path.join(res, "20260102-000001"), [
        ("PASS", "a", 30.0, ""), ("SKIP", "b", 0.0, 'opt-in: add "x"'), ("ERROR", "c", 5.0, "Traceback"),
        ("PASS", "d", "not a number", "")])
    _report_md(os.path.join(res, "20260102-000001"), [("PASS", "a", "A", 777.0, "")])
    # a torn checkpoint: report.md is the fallback
    _checkpoint_json(os.path.join(res, "20260103-000001"), [], raw='{"format": 1, "results": [')
    _report_md(os.path.join(res, "20260103-000001"), [("PASS", "a", "A", 50.0, "")])
    # binary junk for a report, a folder with neither file, and a stray file
    os.makedirs(os.path.join(res, "20260104-000001"))
    with open(os.path.join(res, "20260104-000001", "report.md"), "wb") as f:
        f.write(b"\x00\xff\xfe| PASS | a | A | 1.0s\x00\n")
    os.makedirs(os.path.join(res, "builds"))
    with open(os.path.join(res, "links.json"), "w", encoding="utf-8") as f:
        f.write("{}")
    # 'e' in seven older runs: only the newest five count (400 300 200 100 3 -> 200)
    for n, dur in enumerate([1, 2, 3, 100, 200, 300, 400], start=1):
        _report_md(os.path.join(res, f"20250101-00000{n}"), [("PASS", "e", "E", float(dur), "")])
    calls = []
    real_scan = durations.scan_run

    def counting(folder):
        calls.append(os.path.basename(folder))
        return real_scan(folder)
    durations.scan_run = counting
    cache = os.path.join(res, durations.CACHE)
    try:
        h = durations.load(res, write_cache=False)
        assert not os.path.exists(cache), "write_cache=False wrote the cache"
        assert h["a"] == (30.0, 3), h["a"]      # 50 (report fallback), 30 (checkpoint), 10; the SKIP and 777 ignored
        assert h["b"] == (20.0, 1) and h["c"] == (5.0, 1), (h["b"], h["c"])
        assert "d" not in h and h["e"] == (200.0, 5), (h.get("d"), h["e"])
        calls.clear()
        assert durations.load(res) == h and os.path.exists(cache) and len(calls) == 11, calls
        calls.clear()
        assert durations.load(res) == h and calls == [], f"the cache was not reused: {calls}"
        # one folder changes: only it is read again
        _report_md(os.path.join(res, "20260101-000001"), [("PASS", "b", "B", 40.0, "")])
        calls.clear()
        h2 = durations.load(res)
        assert calls == ["20260101-000001"] and h2["b"] == (40.0, 1) and h2["a"] == (40.0, 2), (calls, h2)
        # a deleted folder leaves the cache
        shutil.rmtree(os.path.join(res, "20260103-000001"))
        h3 = durations.load(res)
        with open(cache, encoding="utf-8") as f:
            assert "20260103-000001" not in json.load(f)["runs"]
        assert h3["a"] == (30.0, 1), h3["a"]
        # a corrupt cache is rebuilt, not raised
        with open(cache, "w", encoding="utf-8") as f:
            f.write("{not json")
        calls.clear()
        assert durations.load(res) == h3 and len(calls) == 10, calls
        # valid JSON of the wrong shape (a result row with no id while in_flight is set makes Checkpoint.load raise
        # KeyError): that folder falls back to its report.md, and every other folder's history survives
        bad = os.path.join(res, "20260105-000001")
        _checkpoint_json(bad, [], raw='{"format": 1, "results": [{"status": "PASS", "dur": 1}], "in_flight": {"id": "x"}}')
        _report_md(bad, [("PASS", "f", "F", 7.0, "")])
        h4 = durations.load(res, write_cache=False)
        assert h4["f"] == (7.0, 1) and h4["a"] == h3["a"], h4
        # and a folder whose scan raises anything at all is skipped, never raised out of load()
        def boom(folder):
            if os.path.basename(folder) == "20260105-000001":
                raise RuntimeError("boom")
            return real_scan(folder)
        durations.scan_run = boom
        h5 = durations.load(res, write_cache=False)
        assert "f" not in h5 and h5["a"] == h3["a"], h5
        shutil.rmtree(bad)
        assert durations.load(os.path.join(res, "no-such-folder"), write_cache=False) == {}
    finally:
        durations.scan_run = real_scan
    # expected(): history first, then the opt-in estimate; a minutes_key opt-in always follows bench.json
    assert durations.expected({"id": "a"}, h3) == (30.0, "history")
    assert durations.expected({"id": "zz"}, h3) == (None, None)
    assert durations.expected({"id": "zz", "opt_in": "nvs_wear"}, h3) == (optin.OPT_INS["nvs_wear"]["estimate_s"],
                                                                        "estimate")
    soak = {"id": "a", "opt_in": "w1s4_soak"}
    assert durations.expected(soak, h3, {}) == (20 * 60 + 60, "estimate")
    assert durations.expected(soak, h3, {"soak_minutes": 5}) == (5 * 60 + 60, "estimate")
    assert [durations.fmt_expected(*x) for x in ((12.4, "history"), (900, "estimate"), (None, None))] == \
        ["0:12", "~15:00", "?"]
    assert [durations.fmt_span(x) for x in (20, 60, 725, 7500)] == ["<1 min", "1 min", "12 min", "2 h 05 min"]


def t_optin_gate_up_front(tmp):
    """A test whose opt-in is off is skipped before it starts - its body never runs, on_start is not called - with the
    exact message the in-body checks raised; opt_in_why replaces the default reason; a missing device still wins; an
    unknown key fails at the decorator; set_enabled keeps bench.json's other keys and order."""
    b = tmp.bench()
    ran, started = [], []
    t_on = fake("on", ran=ran)
    t_on["opt_in"] = "seq_wipe"
    t_off = fake("off", ran=ran)
    t_off["opt_in"] = "nvs_wear"
    t_why = fake("why", ran=ran)
    t_why.update(opt_in="ota_full", opt_in_why="erases and rewrites W1's inactive app slot and switches its boot slot "
                                               "twice")
    t_need = fake("need", ran=ran)
    t_need.update(opt_in="nvs_wear", needs=["navicore"])
    b.cfg["opt_in"] = ["seq_wipe"]
    ck = new_run(b, [t_on, t_off, t_why, t_need], on_start=lambda t: started.append(t["id"]))
    assert ran == ["on"] and started == ["on"], (ran, started)
    got = {r["id"]: (r["status"], r["detail"], r["dur"]) for r in ck.data["results"]}
    assert got["on"][0] == "PASS"
    assert got["off"] == ("SKIP", 'opt-in: add "nvs_wear" to bench.json "opt_in" (about 100 whole-blob NVS writes)',
                          0.0), got["off"]
    assert got["why"] == ("SKIP", 'opt-in: add "ota_full" to bench.json "opt_in" (erases and rewrites W1\'s inactive '
                                  'app slot and switches its boot slot twice)', 0.0), got["why"]
    assert got["need"] == ("SKIP", "needs device navicore", 0.0), got["need"]
    rep = read(os.path.join(ck.out_dir, "report.md"))
    assert ('| SKIP | off | fake off | 0.0s | opt-in: add "nvs_wear" to bench.json "opt_in" (about 100 whole-blob NVS '
            'writes) |') in rep, rep
    log = read(os.path.join(ck.out_dir, "session.log"))
    assert '===== off SKIP (0.0s) opt-in: add "nvs_wear"' in log and "===== off fake off" not in log, log
    try:
        runner.test("x.y", "t", opt_in="no_such_key")
        raise AssertionError("an unknown opt_in key was accepted")
    except ValueError as e:
        assert "no_such_key" in str(e) and "hil/optin.py" in str(e), e
    cfg = {"_comment": "c", "devices": {}, "opt_in": ["ota_full", "seq_wipe"], "soak_minutes": 5}
    optin.set_enabled(cfg, "nvs_wear", True)
    optin.set_enabled(cfg, "ota_full", False)
    optin.set_enabled(cfg, "nvs_wear", True)
    assert list(cfg) == ["_comment", "devices", "opt_in", "soak_minutes"] and cfg["opt_in"] == ["seq_wipe", "nvs_wear"]
    cfg2 = {"devices": {}}
    optin.set_enabled(cfg2, "seq_wipe", False)
    assert cfg2["opt_in"] == [] and optin.enabled({"opt_in": "nvs_wear"}) == []


def t_list_lines(tmp):
    """run.py --list: runner.list_lines' columns, and the real suites in a subprocess - every gated test carries its
    declared opt-in and gives exactly the message in GATED, and every other test is ungated."""
    a = fake("area.one", title="One")
    a["links"] = ["W1S3"]
    g = fake("area.gated", title="Gated")
    g["opt_in"] = "softrx_erratum"
    u = fake("area.unknown", title="Unknown")
    lines = runner.list_lines([a, g, u], {"opt_in": []}, {"area.one": (12.4, 3)})
    assert lines == [f"{'area.one':<26} {'W1S3':<16} {'0:12':>8}  One",
                     f"{'area.gated':<26} {'-':<16} {'~16:00':>8}  [opt-in softrx_erratum: off] Gated",
                     f"{'area.unknown':<26} {'-':<16} {'?':>8}  Unknown"], lines
    assert runner.list_lines([g], {"opt_in": ["softrx_erratum"]}, {})[0].endswith("[opt-in softrx_erratum] Gated")
    # the real suites, in their own process: this one keeps the fake registry
    probe = (
        "import importlib, json, pkgutil, sys\n"
        f"sys.path.insert(0, {HERE!r})\n"
        "from hil import optin, runner\n"
        "import suites\n"
        "for m in pkgutil.iter_modules(suites.__path__):\n"
        "    importlib.import_module('suites.' + m.name)\n"
        "print(json.dumps({t['id']: [t.get('opt_in'), optin.gate({}, t)] for t in runner.REGISTRY}))\n")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert out.returncode == 0, out.stderr
    reg = json.loads(out.stdout)
    gated = {tid: v for tid, v in reg.items() if v[0]}
    assert set(gated) == set(GATED), (sorted(set(gated) ^ set(GATED)))
    for tid, (key, why) in GATED.items():
        assert gated[tid] == [key, f'opt-in: add "{key}" to bench.json "opt_in" ({why})'], (tid, gated[tid])
    bench = os.path.join(tmp.root, "bench.json")
    with open(bench, "w", encoding="utf-8") as f:
        json.dump({"devices": {}, "opt_in": ["ota_full"]}, f)
    cache = os.path.join(HERE, "results", durations.CACHE)
    before = os.stat(cache).st_mtime_ns if os.path.exists(cache) else None
    out = subprocess.run([sys.executable, os.path.join(HERE, "run.py"), "--list", "--bench", bench],
                         capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert out.returncode == 0, out.stderr
    rows = out.stdout.splitlines()
    assert len(rows) == len(reg), (len(rows), len(reg))
    for tid, (key, _) in GATED.items():
        row = next(r for r in rows if r.split()[0] == tid)
        assert f"[opt-in {key}{'' if key == 'ota_full' else ': off'}] " in row, row
    after = os.stat(cache).st_mtime_ns if os.path.exists(cache) else None
    assert before == after, "run.py --list wrote results/durations.json"


TESTS = [t_new_run_to_done, t_golden_report, t_pause_file_and_resume, t_stop, t_last_press_wins,
         t_cut_off_reruns_first, t_frozen_checkpoint_records_nothing, t_pretest_outage_gate, t_outage_auto_retry,
         t_load_cleanup_and_tmp_fallback, t_dropped_ids, t_find_resumable, t_lock_held_by_child_process,
         t_atomic_write_retry, t_redaction, t_run_busy, t_reidentify_never_guesses, t_identify_port_changed_chars,
         t_tmp_newer_than_main, t_finished_run_resumes_to_done, t_navicore_silent_blocks_on_navicore,
         t_passing_wire_marked_verified, t_sbus_released_after_cutoff, t_wizard_abort_kills_tree, t_cli_ask_and_handler,
         t_ctrl_c_during_checks_cancels, t_pause_file_old_mtime, t_redaction_free_text, t_added_tests_listed,
         t_finished_run_with_dropped, t_start_closes_recording_ports, t_vendored_softserial_in_lockstep,
         t_probe_reboot_rebinds, t_runner_fails_test_on_probe_panic, t_probe_restart_forgets_only_what_it_lost,
         t_probe_port_reopen_counts_as_restart,
         t_durations, t_optin_gate_up_front, t_list_lines]
ORIG = {}   # the real functions main() patches, for a test that needs one


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    patches = [(checkpoint, "active_clock", CLOCK), (resume, "snapshot_configs", STUBS.snapshot_configs),
               (resume, "record_firmware", STUBS.record_firmware), (resume, "check_bench", STUBS.check_bench),
               (resume, "usb_map", lambda bench: {n: {"port": d.get("port"), "serial": f"SER-{n}", "vid": 0x10C4,
                                                      "pid": 0xEA60, "location": None}
                                                  for n, d in bench.cfg["devices"].items()}),
               (runner, "harness_info", lambda root: {"git": "selftest", "dirty": 0, "python": "x", "pyserial": "x",
                                                      "files": {}}),
               (runner, "OUTAGE_SETTLE_S", 0), (runner, "_keep_awake", lambda on: None),
               (runner, "_on_battery", lambda: False)]
    saved = [(m, n, getattr(m, n)) for m, n, _ in patches]
    ORIG.update({n: v for _, n, v in saved})
    for m, n, v in patches:
        setattr(m, n, v)
    registry = list(runner.REGISTRY)
    failed = 0
    try:
        for fn in TESTS:
            tmp = Tmp()
            t0 = time.monotonic()
            try:
                fn(tmp)
                print(f"PASS  {fn.__name__[2:]:<36} ({time.monotonic() - t0:.1f}s)")
            except Exception:  # noqa: BLE001
                failed += 1
                print(f"FAIL  {fn.__name__[2:]}\n      " + traceback.format_exc().replace("\n", "\n      "))
            finally:
                tmp.close()
    finally:
        for m, n, v in saved:
            setattr(m, n, v)
        runner.REGISTRY[:] = registry
    print(f"\n{len(TESTS) - failed} of {len(TESTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
