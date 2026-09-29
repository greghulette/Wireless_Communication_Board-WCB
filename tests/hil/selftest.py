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
The config-pull collector (hil/wcb.py, F13) runs against a scripted relay console: one line, parts, refusals, timeouts,
the one re-send after a NOPARTS, and the screening of a refusal's code and detail; so do navicore.pull_over_limit's
verdict on NaviCore's library and wcb.pull_error_oom_parts' re-arm of the one-shot fault (s21 and s03 helpers).
Outside pause/resume, it also checks that the probe's bundled EspSoftwareSerial is byte-identical to the WCB's; that
every Code/WCB/*.cpp and WCB.ino includes WCB_RemoteTerm.h first (CLAUDE.md rule 12), against planted violations too; the
expected durations (hil/durations.py: checkpoint and report.md sources, the median of the newest five real results,
SKIP and NOT A RESULT rows left out, the cache reused and invalidated, corrupt files tolerated); the runner's up-front
opt-in skip with the exact message the tests' own checks used to raise; and run.py --list, in a subprocess that
imports the real suites (read-only: it writes no cache), for every gated test's declared opt-in and message. And the
no-servos gate (hil/servos.py, run.py --no-servos): its skip, its flag across a pause and resume, and the real registry
through the runner loop with fake bodies, skipping exactly the listed ids. The NaviCore and SBUS drivers
(hil/navicore.py, hil/sbus.py; docs/hil_plan/NAVICORE.md INF1, INF2) against scripted consoles (FakeNaviDev): every
parser - PWM_UPDATE, [MAE:n], [CLIPITEM], MESH_STATS pages, the boot banner, #L09, #L13, ?WDP,DUMP - fed lines in the
firmware's own formats; FNV-1a against the published vectors; the SBUS codec on a real frame NaviCore dumped, and in
round trips; the paced write and the USB-Serial/JTAG reset pulse (hil/serialdev.py); and the protocols built on them
(ranged clip download and indexed upload, SET_CONFIG's saveId, the command library's size and hash, ?backup hashed).
And nc_guard (hil/nc_guard.py; NAVICORE.md INF3) against FakeNaviBoard, a NaviCore whose GET_CONFIG and SET_CONFIG
follow the firmware's sparse printing and absent-keys-left-alone merge: the restore ladder's three paths and its
failure, what it restores besides the config (command library, HIL clips, RAM toggles, the mode and learned peers
through a scripted W1), the snapshot file and checkpoint record a killed test leaves, the resume's NaviCore check
(restore, compare, accept, refuse a foreign snapshot), Bench.log's filter on NaviCore and SBUS lines, redacted_diff,
and s40's nccfg.guard_selftest run whole against the fake.
NaviCore's image tooling (hil/ncflash.py, INF4): the image check on synthetic ESP32-S3 images and each defect it names;
the sketchbook-against-repo library check; build()'s command line, BUILD.json and refusals against a scripted
arduino-cli, and a NaviCore git worktree compiled through a staged copy (build(source=...)); ?OTALOCAL,STATUS parsing;
flash() against a fake NaviCore that speaks ?OTALOCAL (ACK, a damaged line's NAK and the rewind, a lost ACK found by
STATUS, an ACK held until the host sends, the idle reaper, a chunk written short, END's verify, the restart into the
other slot, the old slot, no return); and the recovery ladder's decisions against a fake board and a scripted esptool,
which may only ever write 0x10000 and 0xe000.

The real suites are never run: runner.REGISTRY holds fake tests while this runs (t_pull_over_limit_policy imports s03
and s21 for their helpers and undoes their registrations), and the rest of the resume checks (resume.check_bench,
which talks to the boards) are replaced by a stub. What only the real bench can prove is listed in
docs/HIL_TESTING.md §9.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from hil import checkpoint, durations, optin, resume, runner, servos  # noqa: E402
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


class FakeStallDev(FakeJsonDev):
    """The controller's USB outbox latch: a getcfg reply is held until `stall` pings have come in after it, then sent
    ahead of that ping's pong (run 20260922-095341). stall=None never sends it. expect() waits its timeout out."""

    def __init__(self, logged, stall):
        super().__init__(logged)
        self.stall, self.held, self.pings_since = stall, None, 0

    def send(self, text, eol="\n"):
        if '"getcfg"' in text and self.stall != 0:
            self.sent.append(json.loads(text))
            self.held, self.pings_since = json.dumps(self.CFG, separators=(",", ":")), 0
            return
        if '"ping"' in text and self.held is not None:
            self.pings_since += 1
            if self.stall is not None and self.pings_since >= self.stall:
                self._rx(self.held)
                self.held = None
        super().send(text, eol)

    def expect(self, pattern, timeout=3.0, since=None):
        import re
        for attempt in (0, 1):
            for line in self.lines[since or 0:]:
                m = re.search(pattern, line)
                if m:
                    return m
            if attempt == 0:
                time.sleep(timeout)
        raise resume.ExpectTimeout(f"sbus: no line matching /{pattern}/ within {timeout}s; last lines:")


def t_sbus_reply_nudged_by_ping(tmp):
    """A controller reply stuck in its USB outbox is pinged loose (SbusCtl._reply); a prompt one sends no extra ping,
    and one that never comes fails with the whole wait in its message."""
    from hil.sbus import SbusCtl
    d = FakeStallDev([], stall=0)
    assert SbusCtl(d).cfg()["e"] == "cfg"
    assert d.sent == [{"t": "ping"}, {"t": "getcfg"}], d.sent
    d = FakeStallDev([], stall=1)
    ctl = SbusCtl(d)
    ctl.NUDGE_S = 0.02
    assert ctl.cfg()["e"] == "cfg"
    assert d.sent == [{"t": "ping"}, {"t": "getcfg"}, {"t": "ping"}], d.sent
    i = next(k for k, x in enumerate(d.lines) if x.startswith('{"e":"cfg"'))
    assert d.lines[i + 1].startswith('{"t":"pong"'), "the held reply goes out ahead of the nudge's pong"
    d = FakeStallDev([], stall=None)
    ctl = SbusCtl(d)
    ctl.NUDGE_S = 0.02
    try:
        ctl.cfg(timeout=0.1)
    except resume.ExpectTimeout as e:
        assert "within 0.1s, pinging every 0.02s" in str(e), str(e)
    else:
        raise AssertionError("a reply that never comes must time out")
    assert d.sent.count({"t": "ping"}) >= 3, d.sent


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


def t_nctool_pipe_bridge(tmp):
    """INF7 (docs/hil_plan/NAVICORE.md §5.2): the bridge's /serial routes serve the piped device only, outside the
    one-at-a-time lock; a line read comes back whole with the harness's synthetic lines left to the pipe; a write is one
    line, paced past 512 bytes, and logged through Bench.log's credential filter; setSignals is recorded and NEVER reaches
    the port; /sbus sends only the RAM-only verbs. run_wizard_test(pipe=True) keeps the port open, tells the page it is
    piped, and pings NaviCore afterwards; run_unit_tests reports an all-skipped run as SKIP and notes a failing todo."""
    import json as _json
    import urllib.error
    import urllib.request
    from types import SimpleNamespace
    from hil import wizard
    from hil.bridge import Bridge
    from hil.serialdev import SerialDevice
    b = tmp.bench({"navicore": {"port": "COMNC", "kind": "navicore"}, "sbus": {"port": "COMSB", "kind": "sbus"}})
    b.new_session()
    nc, sb = SerialDevice("navicore", "COMNC", log=b.log), SerialDevice("sbus", "COMSB", log=b.log)
    nc._ser, sb._ser = RecSer(), RecSer()
    b.devs.update(navicore=nc, sbus=sb)

    def post(url, path, body):
        req = urllib.request.Request(url + path, data=_json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, _json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, _json.loads(e.read())
    try:
        with Bridge(b, {"device": "navicore", "pipe": True}) as br:
            with br._lock:                                   # served outside the lock: this must not block
                code, got = post(br.url, "/serial/mark", {"device": "navicore"})
            assert code == 200 and got == {"mark": 0}, (code, got)
            nc._append('{"type":"PONG","version":"v0.2.0_X"}')
            nc._append("<<reopened COMNC>>")
            code, got = post(br.url, "/serial/read", {"device": "navicore", "since": 0})
            assert got == {"lines": ['{"type":"PONG","version":"v0.2.0_X"}', "<<reopened COMNC>>"], "next": 2}, got
            assert post(br.url, "/serial/read", {"device": "navicore", "since": 2})[1] == {"lines": [], "next": 2}
            post(br.url, "/serial/write", {"device": "navicore", "text": '{"sys":1,"type":"PING"}'})
            post(br.url, "/serial/write", {"device": "navicore", "text": '{"a":"' + "x" * 1300 + '","wifiPassword":"sekrit99"}'})
            assert nc._ser.writes[0] == b'{"sys":1,"type":"PING"}\n', nc._ser.writes
            assert [len(w) for w in nc._ser.writes[1:]] == [512, 512, 311], [len(w) for w in nc._ser.writes]
            code, got = post(br.url, "/serial/write", {"device": "navicore", "text": "a\nb"})
            assert code == 409 and "one line" in got["error"], (code, got)
            code, got = post(br.url, "/serial/signals", {"device": "navicore", "dtr": False, "rts": True})
            assert code == 200 and br.signals == [{"dtr": False, "rts": True}] and nc._ser.control == [], (br.signals, nc._ser.control)
            code, got = post(br.url, "/serial/mark", {"device": "sbus"})
            assert code == 409 and "no pipe to 'sbus'" in got["error"], (code, got)
            assert post(br.url, "/sbus", {"t": "a", "rx": 100})[0] == 200
            code, got = post(br.url, "/sbus", {"t": "mode", "sbus24": True})
            assert code == 409 and "refuses 'mode'" in got["error"], (code, got)
            assert sb._ser.writes == [b'{"t":"a","rx":100}\n'], sb._ser.writes
        with Bridge(b, {"device": "navicore", "pipe": False}) as br:
            code, got = post(br.url, "/serial/mark", {"device": "navicore"})
            assert code == 409 and "pipe=False" in got["error"], (code, got)
        b.sync_log()
        log = read(os.path.join(b.out_dir, "session.log"))
        assert "sekrit99" not in log and "<redacted:" in log, "a piped write must pass Bench.log's credential filter"
        assert "setSignals dtr=False rts=True (recorded, not applied)" in log, log[-400:]

        # run_wizard_test(pipe=True): the port is not handed over, the page is told, NaviCore is pinged afterwards.
        fake_root = os.path.join(tmp.root, "wizard")
        os.makedirs(os.path.join(fake_root, "node_modules", "@playwright", "test"))
        open(os.path.join(fake_root, "node_modules", "@playwright", "test", "cli.js"), "w").close()
        seen = {}

        class FakeBridge:
            url = "http://127.0.0.1:0"

            def __init__(self, bench, context):
                seen["context"] = context

            def __enter__(self):
                return self

            def __exit__(self, *e):
                return False

        def popen(cmd, env=None, **kw):
            report = {"suites": [{"specs": [{"title": "nctool.fake does a thing", "tests": [
                {"status": "expected", "results": [{"status": "passed", "errors": []}]}]}]}]}
            with open(env["PLAYWRIGHT_JSON_OUTPUT_NAME"], "w", encoding="utf-8") as f:
                _json.dump(report, f)
            return subprocess.Popen([sys.executable, "-c", "print('[1/1] ok')"], stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
        saved = {n: getattr(wizard, n) for n in ("subprocess", "shutil", "WIZARD_TESTS", "usb_ids", "Bridge", "_reacquire")}
        closed, pinged = [], []
        real_close = b.close_device
        b.close_device = lambda name, release=True: closed.append(name)
        wizard.subprocess = SimpleNamespace(Popen=popen, PIPE=subprocess.PIPE, STDOUT=subprocess.STDOUT,
                                            DEVNULL=subprocess.DEVNULL, TimeoutExpired=subprocess.TimeoutExpired,
                                            CREATE_NEW_PROCESS_GROUP=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                                            run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, runs.pop(0), ""),
                                            CompletedProcess=subprocess.CompletedProcess)
        wizard.shutil = SimpleNamespace(which=lambda n: sys.executable)
        wizard.WIZARD_TESTS = fake_root
        wizard.usb_ids = lambda com: (0x303A, 0x1001)
        wizard.Bridge = FakeBridge
        wizard._reacquire = lambda bench, device, timeout=25.0: pinged.append(device)
        try:
            wizard.run_wizard_test(b, "nctool.fake", device="navicore", pipe=True, timeout=60)
            assert closed == [] and pinged == ["navicore"], (closed, pinged)
            ctx = seen["context"]
            assert ctx["pipe"] is True and ctx["device"] == "navicore" and ctx["kind"] == "navicore", ctx
            wizard.run_wizard_test(b, "nctool.fake", device=None, timeout=60)
            assert closed == [] and seen["context"]["pipe"] is False and pinged == ["navicore"], (closed, seen, pinged)
            # node --test: every test skipped -> SKIP with the reason; a failing todo -> PASS and a note. Both of node's
            # reporters: spec (its default, even piped) and TAP.
            runs = ["﹣ nctool.static: x (0.1ms) # the NaviCore repo is not beside this one\nℹ tests 1\nℹ pass 0\nℹ skipped 1\n",
                    "TAP version 13\nok 1 - x # SKIP the NaviCore repo is not beside this one\n# pass 0\n# skipped 1\n",
                    "✔ a (1ms)\n⚠ b (2ms) # known tool defect\nℹ pass 1\nℹ todo 1\n✖ failing tests:\n⚠ b (2ms) # known tool defect\n",
                    "TAP version 13\nok 1 - a\nnot ok 2 - c # TODO known tool defect\n# pass 1\n# todo 1\n"]
            for _ in range(2):
                e = _raises(lambda: wizard.run_unit_tests(b, files=("unit/navicore/static.test.js",)), runner.Skip)
                assert "not beside this one" in str(e), e
            wizard.run_unit_tests(b, files=("unit/navicore/unit.test.js",))
            wizard.run_unit_tests(b, files=("unit/navicore/unit.test.js",))
            b.sync_log()
            log = read(os.path.join(b.out_dir, "session.log"))
            assert log.count("known defect (node todo, still failing): ⚠ b (2ms) # known tool defect") == 1, log[-600:]
            assert "known defect (node todo, still failing): not ok 2 - c # TODO" in log
        finally:
            for n, v in saved.items():
                setattr(wizard, n, v)
            b.close_device = real_close
    finally:
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
                 "last lines:\n    [SBUS] AP mode  SSID: SBUSCtrl  Pass: sekrit99",
                 "last lines:\n    ESP-NOW password updated to: hunter2", "ESP-NOW Password updated to: x hunter2",
                 "last lines:\n    Password: DomeNet sekrit99\n    ;S0marker",
                 "probe2: 'MESH JOIN ID=11 OCT2=AB OCT3=CD QTY=9 CHAN=1 CHK=1 TEMP=1 TYPE=HILProbe PASS=hunter2' -> "
                 "ERR already joined"):
        assert not any(s in rt(text) for s in SECRETS), rt(text)
    assert "extra ['?EPASS,<redacted:" in rt("W1: missing ['?EPASS,hunter2'] / extra ['?EPASS,x']")
    assert rt("AP password   : set") == "AP password   : set" and rt("?WIFI,OFF") == "?WIFI,OFF"
    assert rt("PASS: 3, FAIL: 0") == "PASS: 3, FAIL: 0", "a status count is not a password"
    for text in ("Password: a b", "ESP-NOW password updated to: x y", "[SBUS] AP mode  SSID: S  Pass: p q",
                 "NAVICORE CONFIG NOT RESTORED \u2014 wcbNetwork.password: hunter2"):
        assert rt(rt(text)) == rt(text), f"idempotent: {rt(text)!r} -> {rt(rt(text))!r}"
    assert rt("Password: a b\n;S0next").endswith("\n;S0next"), "the value stops at the end of its line"
    assert rt("MESH JOIN ... PASS=<pw>") == "MESH JOIN ... PASS=<pw>", "the usage text is left alone"
    # F13: a config part line in a tail - its secrets sit whole in part 1, where the token prefixes still find them
    part = "[MGMT:CFGPART,2]P1A2B,1,2:[VER:6.3.0]?HW,24^?WCB,2^?WIFI,AP,DomeNet,sekrit99^?EPASS,hunter2^?SEQ,SAVE,K,z~"
    assert not any(s in rt(part) for s in SECRETS) and rt(part).endswith("^?SEQ,SAVE,K,z~"), rt(part)
    assert not any(s in rt(part[:part.index("^?SEQ")] + "~") for s in SECRETS), "a part ending right after ?EPASS"
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


# ---------------------------------------------------------------------------- CLAUDE.md rule 12 (WCB-WP37)
# The two files rule 12 exempts, and why. The wrapper is exempt outright; the other only while it prints nothing.
RULE12_EXEMPT = {"WCB_RemoteTerm.cpp": "it is the wrapper the header redirects Serial to",
                 "wcb_pin_map.cpp": "every print in it is commented out"}
_INCLUDE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*[<"]([^>"\n]+)[>"]', re.M)


def _strip_c_comments(text):
    """C/C++ source with its // and /* */ comments blanked out, newlines kept so line numbers hold. String and character
    literals are copied through untouched, so the '//' in "ws://%s/ws" does not start a comment."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("".join(ch if ch == "\n" else " " for ch in text[i:j]))
            i = j
        elif c in "\"'":
            j = i + 1
            while j < n and text[j] not in (c, "\n"):
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def rule12_problems(code_dir):
    """CLAUDE.md rule 12: every Code/WCB/*.cpp, and WCB.ino too (WCB_RemoteTerm.h's own usage note), #includes
    WCB_RemoteTerm.h before any other header, so its '#define Serial WCBDebugSerial' (WCB_RemoteTerm.h:104) reaches the
    file and everything it includes, and the file prints to the port setup() begins. Otherwise its lines go to the core's
    raw Serial, which nothing ever begins: a handler that runs and says nothing, which cost a session on ?WIFI. The
    first #include outside a comment is what counts; the manual check in CLAUDE.md greps the first three lines. -> one
    line per file that breaks it, naming the file and line."""
    problems = []
    for name in sorted(f for f in os.listdir(code_dir) if f.endswith(".cpp") or f == "WCB.ino"):
        if name == "WCB_RemoteTerm.cpp":
            continue
        with open(os.path.join(code_dir, name), encoding="utf-8", errors="replace") as f:
            code = _strip_c_comments(f.read())
        if name in RULE12_EXEMPT:
            live = re.search(r"\bSerial\s*\.", code)
            if live:
                problems.append(f"{name}:{code.count(chr(10), 0, live.start()) + 1}: prints through Serial, so its "
                                f"rule-12 exemption ({RULE12_EXEMPT[name]}) no longer holds")
            continue
        first = _INCLUDE.search(code)
        if first is None:
            problems.append(f"{name}: no #include at all, so WCB_RemoteTerm.h is not first")
        elif first.group(1) != "WCB_RemoteTerm.h":
            problems.append(f"{name}:{code.count(chr(10), 0, first.start()) + 1}: the first #include is "
                            f"{first.group(1)}, not WCB_RemoteTerm.h")
    return problems


def t_rule12_remoteterm_first(tmp):
    """CLAUDE.md rule 12 as a check that needs no bench (docs/hil_plan/WCB.md WCB-WP37): the firmware tree passes, and
    each way to break the rule, planted in a copy of the tree, is caught with the file and line: a new subsystem file
    without the include, the include after another header or only in a comment, an existing file given another header
    first, and a print added to an exempt file. A comment header above the include, or '//' inside a string, is fine."""
    code = os.path.normpath(os.path.join(HERE, "..", "..", "Code", "WCB"))
    checked = sorted(f for f in os.listdir(code) if f.endswith(".cpp") or f == "WCB.ino")
    assert "WCB.ino" in checked and len([f for f in checked if f.startswith("WCB_")]) >= 10, checked
    found = rule12_problems(code)
    assert not found, "CLAUDE.md rule 12 is broken: " + "; ".join(found)
    copy = os.path.join(tmp.root, "WCB")
    os.makedirs(copy)
    for f in checked:
        shutil.copy2(os.path.join(code, f), os.path.join(copy, f))
    assert rule12_problems(copy) == []

    def plant(name, text):
        with open(os.path.join(copy, name), "w", encoding="utf-8") as f:
            f.write(text)

    new = "WCB_HilPlanted.cpp"
    for text, want in (
            ('#include "WCB_HilPlanted.h"\n#include <Arduino.h>\nvoid hilPlanted() { Serial.println("x"); }\n',
             [f"{new}:1: the first #include is WCB_HilPlanted.h, not WCB_RemoteTerm.h"]),
            ('#include <Arduino.h>\n#include "WCB_RemoteTerm.h"\n',
             [f"{new}:1: the first #include is Arduino.h, not WCB_RemoteTerm.h"]),
            ('// #include "WCB_RemoteTerm.h"\n/* #include "WCB_RemoteTerm.h" */\n#include "WCB_WiFi.h"\n',
             [f"{new}:3: the first #include is WCB_WiFi.h, not WCB_RemoteTerm.h"]),
            ("int hilPlanted;\n", [f"{new}: no #include at all, so WCB_RemoteTerm.h is not first"]),
            ('/* a header\n * over two lines */\n// and a line comment\n#include "WCB_RemoteTerm.h"\n'
             'static const char *u = "ws://%s/ws"; // #include <Arduino.h>\n#include <Arduino.h>\n', [])):
        plant(new, text)
        got = rule12_problems(copy)
        assert got == want, (text, got)
    os.remove(os.path.join(copy, new))
    with open(os.path.join(code, "WCB_WiFi.cpp"), encoding="utf-8", errors="replace") as f:
        wifi = f.read()
    plant("WCB_WiFi.cpp", "#include <WiFi.h>\n" + wifi)
    assert rule12_problems(copy) == ["WCB_WiFi.cpp:1: the first #include is WiFi.h, not WCB_RemoteTerm.h"], \
        rule12_problems(copy)
    shutil.copy2(os.path.join(code, "WCB_WiFi.cpp"), os.path.join(copy, "WCB_WiFi.cpp"))
    with open(os.path.join(code, "wcb_pin_map.cpp"), encoding="utf-8", errors="replace") as f:
        pins = f.read()
    plant("wcb_pin_map.cpp", pins + '\nvoid hilPlanted() { Serial.println("pins"); }\n')
    got = rule12_problems(copy)
    assert len(got) == 1 and got[0].startswith("wcb_pin_map.cpp:") and "exemption" in got[0], got
    shutil.copy2(os.path.join(code, "wcb_pin_map.cpp"), os.path.join(copy, "wcb_pin_map.cpp"))
    assert rule12_problems(copy) == []



# The one file that may call the driver's esp_now_send: the wrapper itself (CLAUDE.md rule 16).
RULE16_WRAPPER = "WCB_EspNow.cpp"
_RAW_SEND = re.compile(r"\besp_now_send\s*\(")
_C_STRING = re.compile(r'"(?:[^"\\\n]|\\.)*"')


def rule16_problems(code_dir):
    """CLAUDE.md rule 16: every ESP-NOW send in Code/WCB goes through wcbEspNowSend (WCB_EspNow.h), which bounds the
    frames in flight and makes a task wait for the radio. A raw esp_now_send anywhere else bypasses it and brings back
    tracker #102 (a mesh-send flood took the heap and aborted the board) and #107 (?RTERM lines lost to a full radio
    queue). Comments and string literals do not count. -> one line per call, naming the file and line, and one if the
    wrapper itself no longer calls the driver."""
    problems = []
    for name in sorted(f for f in os.listdir(code_dir) if f.endswith((".cpp", ".h", ".ino"))):
        with open(os.path.join(code_dir, name), encoding="utf-8", errors="replace") as f:
            code = _C_STRING.sub('""', _strip_c_comments(f.read()))
        hits = list(_RAW_SEND.finditer(code))
        if name == RULE16_WRAPPER:
            if not hits:
                problems.append(f"{name}: the wrapper no longer calls esp_now_send")
            continue
        problems += [f"{name}:{code.count(chr(10), 0, m.start()) + 1}: calls esp_now_send directly, not wcbEspNowSend"
                     for m in hits]
    return problems


def t_rule16_espnow_send_wrapped(tmp):
    """CLAUDE.md rule 16 as a check that needs no bench: the firmware tree passes, and each way to break the rule,
    planted in a copy of the tree, is caught with the file and line - a raw call in a new file, in WCB.ino, and one
    split over two lines, and a wrapper that stopped calling the driver. The driver's name in a comment or a string,
    and esp_now_send_status_t, do not count."""
    code = os.path.normpath(os.path.join(HERE, "..", "..", "Code", "WCB"))
    names = sorted(f for f in os.listdir(code) if f.endswith((".cpp", ".h", ".ino")))
    assert RULE16_WRAPPER in names and "WCB.ino" in names, names
    found = rule16_problems(code)
    assert not found, "CLAUDE.md rule 16 is broken: " + "; ".join(found)
    copy = os.path.join(tmp.root, "WCB16")
    os.makedirs(copy)
    for f in names:
        shutil.copy2(os.path.join(code, f), os.path.join(copy, f))
    assert rule16_problems(copy) == []

    def plant(name, text):
        with open(os.path.join(copy, name), "w", encoding="utf-8") as f:
            f.write(text)

    new = "WCB_HilPlanted.cpp"
    for text, want in (
            ('#include "WCB_RemoteTerm.h"\nvoid a(const uint8_t *m) {\n  esp_now_send(m, m, 6);\n}\n',
             [f"{new}:3: calls esp_now_send directly, not wcbEspNowSend"]),
            ('#include "WCB_RemoteTerm.h"\nvoid a(const uint8_t *m) {\n  esp_err_t r = esp_now_send\n    (m, m, 6);\n}\n',
             [f"{new}:3: calls esp_now_send directly, not wcbEspNowSend"]),
            ('#include "WCB_RemoteTerm.h"\n// esp_now_send(m, m, 6) used to be here\n/* esp_now_send(x) */\n'
             'void a(esp_now_send_status_t s) { Serial.println("esp_now_send( failed"); wcbEspNowSend(0, 0, 0); }\n', [])):
        plant(new, text)
        got = rule16_problems(copy)
        assert got == want, (text, got)
    os.remove(os.path.join(copy, new))
    with open(os.path.join(code, "WCB.ino"), encoding="utf-8", errors="replace") as f:
        ino = f.read()
    plant("WCB.ino", ino + "\nvoid hilPlanted(const uint8_t *m) { esp_now_send(m, m, 1); }\n")
    got = rule16_problems(copy)
    assert len(got) == 1 and got[0].startswith("WCB.ino:") and "directly" in got[0], got
    shutil.copy2(os.path.join(code, "WCB.ino"), os.path.join(copy, "WCB.ino"))
    with open(os.path.join(code, RULE16_WRAPPER), encoding="utf-8", errors="replace") as f:
        wrapper = f.read()
    plant(RULE16_WRAPPER, _RAW_SEND.sub("esp_now_sendX(", wrapper))
    assert rule16_problems(copy) == [f"{RULE16_WRAPPER}: the wrapper no longer calls esp_now_send"], rule16_problems(copy)
    shutil.copy2(os.path.join(code, RULE16_WRAPPER), os.path.join(copy, RULE16_WRAPPER))
    assert rule16_problems(copy) == []

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
    "wifi.off_and_back": ("wifi_modes", "changes W1's WiFi mode and reboots it four times"),
    "wifi.join_w2_ap": ("wifi_modes", "changes W1's WiFi mode and reboots it four times"),
    "wifi.ap_derived_ssid_boot": ("wifi_modes", "changes W1's access point name and reboots it twice"),
    "wifi.join_lost_and_rejoin": ("wifi_modes", "joins W1 to W2's access point, turns W2's access point off and back "
                                                "on, and reboots each board twice"),
    "wifi.join_absent_ssid_keeps_mesh": ("wifi_modes", "points W1's JOIN at a network nobody hosts for about 70 s and "
                                                       "reboots W1 twice"),
    "etm.seq_wrap": ("etm_seq_wrap", "floods the mesh with about 65,000 JSON broadcasts from W1 for several minutes "
                                     "and reboots W2"),
    **{t: ("wifi_pc", "a WiFi adapter on this PC leaves its network for about 30 s; run it with someone at the keyboard")
       for t in ("wifi.pc_joins_ap_ws", "ws.line_framing", "ws.backup_over_ws", "ws.client_slots",
                 "wifi.ap_dhcp_no_gateway", "ws.ota_chunk")},
    "ident.epass_live": ("mesh_password", "takes W1 off the mesh for a few seconds with a throwaway password"),
    "nvs.erase_defaults_restore": ("nvs_erase", "erases all of W1's settings and restores them from its chain"),
    "nvs.wcb_erase_alias": ("nvs_erase", "erases all of W1's settings and restores them from its chain"),
    "seq.nvs_full_consistency": ("nvs_fill", "fills W1's settings storage with throwaway sequences, then removes them"),
    "nvs.full_map_and_device_save": ("nvs_fill", "fills W1's settings storage with throwaway sequences, then removes them"),
    "ident.epass_window_gates": ("mesh_password", "takes W1 off the mesh for a few seconds with a throwaway password"),
    "nvs.erase_led_and_tails": ("nvs_erase", "erases all of W1's settings and restores them from its chain"),
    **{f"ota.{n}": ("ota_erase", _ERASE) for n in (
        "local_begin_abort", "local_begin_supersede", "local_cursor_nak_incomplete", "local_truncated_verify_fail",
        "local_bad_magic", "local_overrun", "local_base64_errors", "local_idle_timeout_nak_no_refresh",
        "local_write_refreshes_timeout", "local_baud_bump_abort", "local_baud_invalid", "local_baud_timeout_restore",
        "local_baud_rejected_rebegin_restores", "relay_cursor_dup_gap_wrong_session_abort", "relay_teardown_frame_err",
        "relay_end_incomplete_and_verify_fail", "relay_timeout_keepalive", "cross_transport_remote_abort_kills_local",
        "begin_abandons_config_pull")},
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
    **{f"nccfg.{n}": ("navicore_reboot", "restarts NaviCore: the mesh and SBUS OUT lose it for about 5 s")
       for n in ("persist_reboot", "reset_defaults_ram", "reset_defaults_keeps_identity")},
    **{f"nccfg.{n}": ("navicore_fault", "injects faults into NaviCore's config storage (a NAVICORE_HIL_HOOKS build "
                                        "only)")
       for n in ("hook_save_fail", "hook_get_config_overflow", "hook_config_unreadable")},
    # NC-WP2, NC-WP9 and NC-WP10 (suites/s46_navicore_boot.py, s47_navicore_ota.py), 2026-09-28
    **{t: ("navicore_reboot", "restarts NaviCore: the mesh and SBUS OUT lose it for about 5 s")
       for t in ("ncboot.banner_order", "ncboot.reboot_resets_ram_state", "ncboot.wcbs_see_reboot",
                 "ncboot.new_peer_after_boot", "ncboot.roll_call_missing_board", "ncboot.mesh_reboot",
                 "ncboot.boardtype2_mismatch", "sbus.boot_quiet", "ncota.recovery_hard_reset")},
    "ncboot.bad_device_id": ("navicore_identity", "takes NaviCore off the mesh with a saved invalid deviceId until it "
                                                  "is restored over USB; attended only"),
    "ncota.recovery_esptool": ("navicore_esptool", "resets NaviCore into ROM download mode and writes its app0 and "
                                                   "otadata with esptool; watched runs only"),
    "ncota.local_begin_abort_timeout": ("navicore_ota_erase", "every accepted BEGIN erases 4 KB of NaviCore's inactive "
                                                              "app slot, which nothing restores"),
    "ncota.local_full_same_image": ("navicore_ota_full", "rewrites NaviCore's inactive app slot and switches its boot "
                                                         "slot twice"),
    "ncota.relay_full_via_w1": ("navicore_ota_relay_full", "rewrites NaviCore's inactive app slot through W1's relay "
                                                           "and switches its boot slot twice (~25 min)"),
    "ncota.relay_full_to_w2": ("ota_full_wcb2", "erases and rewrites W2's inactive app slot and switches its boot slot "
                                                "twice"),
    # NC-WP6 (suites/s43_navicore_mesh.py), 2026-09-28
    "ncmesh.wdp_learn_forget": ("navicore_nvs", "writes NaviCore's learned-peer list to its NVS four times"),
    # NC-WP7 (suites/s44_navicore_devices.py), 2026-09-28
    **{f"ncdev.{n}": ("navicore_aux_tx", "sends HCR, MP3 Trigger, DFPlayer and WLED bytes out NaviCore's own serial "
                                         "ports, where nothing records what is attached")
       for n in ("hcr_local_payload", "local_device_bytes", "cli_hcr_test_codes", "hcr_local_volstep_cap",
                 "hcr_level_same_both_ways")},
    # IX-WP5 (suites/s33_intellex_bench.py), 2026-09-29
    "intellex.serial_device_loss_navicore": ("intellex_reboot", "restarts NaviCore through Intellex's transport: the "
                                                                "mesh and SBUS OUT lose it for about 5 s"),
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



def t_no_servos(tmp):
    """No moving servos (hil/servos.py; tonight's run.py --no-servos): every listed test is skipped before it starts -
    body never run, on_start never called - with exactly servos.SKIP_REASON, ahead of its opt-in's skip but after a
    missing device; the run's flag lives in its checkpoint and survives a pause and a resume from disk into a bench.json
    that lacks it; bench.json "no_servos" alone does the same and fails safe on a hand-typed value; every listed id is
    a registered test (a renamed test would silently move its servo again), and the whole real registry, fed through
    the runner loop with fake bodies, skips exactly the listed ids; run.py --list --no-servos tags exactly those rows."""
    for v in (True, "true", "yes", 1):
        assert servos.enabled({"no_servos": v}), v
    for v in (False, 0, None, "false", "no", "off", ""):
        assert not servos.enabled({"no_servos": v}), v
    assert not servos.enabled({}) and servos.enabled({}, {"no_servos": True}) and not servos.enabled(None, None)
    # 1. the run's flag: start_run(no_servos=True), a pause, then a resume from disk into a bench.json without the key
    b = tmp.bench()
    ran, started = [], []
    listed = fake("maestro.sub", ran=ran)
    gated = fake("navicore.rec_play_clip", ran=ran)
    gated["opt_in"] = "navicore_clip"                    # listed and opt-in off: the servo reason is the one reported
    needy = fake("navicore.set_mode", ran=ran)
    needy["needs"] = ["navicore"]                         # listed but its device is missing: "needs ..." still wins
    first = fake("area.first", lambda bench: open(os.path.join(bench.out_dir, "PAUSE"), "w").close(), ran=ran)
    tests = [fake("wcb.unknown", ran=ran), listed, gated, needy, first, fake("sbus.to_navicore", ran=ran),
             fake("area.last", ran=ran)]
    runner.REGISTRY[:] = tests
    ck = runner.start_run(b, tests, "selftest", no_servos=True)
    runner.continue_run(b, ck, resuming=False, on_start=lambda t: started.append(t["id"]))
    assert ck.state == "paused", ck.state
    got = {r["id"]: (r["status"], r["detail"], r["dur"]) for r in ck.data["results"]}
    assert got["maestro.sub"] == ("SKIP", servos.SKIP_REASON, 0.0), got["maestro.sub"]
    assert got["navicore.rec_play_clip"] == ("SKIP", servos.SKIP_REASON, 0.0), got["navicore.rec_play_clip"]
    assert got["navicore.set_mode"] == ("SKIP", "needs device navicore", 0.0), got["navicore.set_mode"]
    assert got["wcb.unknown"][0] == "PASS" and ran == started == ["wcb.unknown", "area.first"], (ran, started)
    assert Checkpoint.load(ck.out_dir).data.get("no_servos") is True, "the flag is not in the checkpoint on disk"
    log = read(os.path.join(ck.out_dir, "session.log"))
    assert f"===== maestro.sub SKIP (0.0s) {servos.SKIP_REASON}" in log and "===== maestro.sub fake" not in log, log
    b2 = tmp.bench()
    assert "no_servos" not in b2.cfg
    ck2 = resume_run(b2, ck.out_dir)
    assert ck2.state == "done", ck2.state
    got = {r["id"]: (r["status"], r["detail"]) for r in ck2.data["results"]}
    assert got["sbus.to_navicore"] == ("SKIP", servos.SKIP_REASON), got["sbus.to_navicore"]
    assert got["area.last"][0] == "PASS" and ran == ["wcb.unknown", "area.first", "area.last"], ran
    rep = read(os.path.join(ck2.out_dir, "report.md"))
    assert f"| SKIP | maestro.sub | fake maestro.sub | 0.0s | {servos.SKIP_REASON} |" in rep, rep
    # 2. bench.json "no_servos" alone, and a run with neither
    b3 = tmp.bench()
    b3.cfg["no_servos"] = True
    ran3 = []
    ck3 = new_run(b3, [fake("maestro.verbs", ran=ran3), fake("area.x", ran=ran3)])
    assert [(r["id"], r["status"]) for r in ck3.data["results"]] == [("maestro.verbs", "SKIP"), ("area.x", "PASS")]
    assert ran3 == ["area.x"] and "no_servos" not in ck3.data, (ran3, ck3.data.get("no_servos"))
    ran4 = []
    ck4 = new_run(tmp.bench(), [fake("maestro.verbs", ran=ran4)])
    assert ran4 == ["maestro.verbs"] and ck4.data["results"][0]["status"] == "PASS"
    # 3. list_lines' tags
    rows = runner.list_lines([fake("maestro.sub", title="S"), fake("area.one", title="One")], {}, {})
    assert rows[0].endswith("  [servo] S") and rows[1].endswith("  One"), rows
    assert runner.list_lines([fake("maestro.sub", title="S")], {}, {}, no_servos=True)[0].endswith("[servo: skipped] S")
    assert runner.list_lines([fake("maestro.sub", title="S")], {"no_servos": True}, {})[0].endswith("[servo: skipped] S")
    # 4. the real suites, listed in their own process (this one keeps the fake registry)
    probe = (
        "import importlib, json, pkgutil, sys\n"
        f"sys.path.insert(0, {HERE!r})\n"
        "from hil import runner\n"
        "import suites\n"
        "for m in pkgutil.iter_modules(suites.__path__):\n"
        "    importlib.import_module('suites.' + m.name)\n"
        "print(json.dumps([[t['id'], t['title'], t.get('opt_in')] for t in runner.REGISTRY]))\n")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert out.returncode == 0, out.stderr
    reg = json.loads(out.stdout)
    real = {tid for tid, _, _ in reg}
    stale = sorted(set(servos.SERVO_TESTS) - real)
    assert not stale, f"hil/servos.py lists ids no suite registers (renamed? their servos would move again): {stale}"
    # every real test, with a fake body, through the real start_run / checkpoint / runner loop; every opt-in on, so the
    # only skips left are the servo ones
    b5 = tmp.bench()
    b5.cfg["opt_in"] = list(optin.OPT_INS)
    ran5 = []
    fakes = []
    for tid, title, key in reg:
        f = fake(tid, title=title, ran=ran5)
        f["opt_in"] = key
        fakes.append(f)
    runner.REGISTRY[:] = fakes
    ck5 = runner.start_run(b5, fakes, "selftest", no_servos=True)
    runner.continue_run(b5, ck5, resuming=False)
    assert ck5.state == "done", ck5.state
    skipped = {r["id"] for r in ck5.data["results"] if r["status"] == "SKIP"}
    assert skipped == set(servos.SERVO_TESTS), sorted(skipped ^ set(servos.SERVO_TESTS))
    assert all(r["detail"] == servos.SKIP_REASON for r in ck5.data["results"] if r["status"] == "SKIP")
    assert not set(ran5) & set(servos.SERVO_TESTS) and len(ran5) == len(reg) - len(servos.SERVO_TESTS), len(ran5)
    # run.py --list --no-servos against the real suites: exactly the listed rows are tagged as skipped
    bench = os.path.join(tmp.root, "bench.json")
    with open(bench, "w", encoding="utf-8") as f:
        json.dump({"devices": {}}, f)
    out = subprocess.run([sys.executable, os.path.join(HERE, "run.py"), "--list", "--no-servos", "--bench", bench],
                         capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert out.returncode == 0, out.stderr
    rows = out.stdout.splitlines()
    assert len(rows) == len(reg), (len(rows), len(reg))
    tagged = {r.split()[0] for r in rows if "[servo: skipped] " in r}
    assert tagged == set(servos.SERVO_TESTS), sorted(tagged ^ set(servos.SERVO_TESTS))
    assert not [r for r in rows if "[servo] " in r], "--no-servos left a servo row untagged as skipped"


def t_config_guard_auto_restore(tmp):
    """config_guard puts back the AUTO_RESTORE token kinds a test leaked - and only those - before it reports the leak
    (docs/HIL_TEST_AUDIT.md A1): undo lines first, then the missing baseline tokens in backup order; a mesh-only board
    gets ;W<n> lines and keeps a ^-chained value; a leaked delimiter stops the auto-restore altogether."""
    import suites.common as common

    class FakeWCB:
        sent = []

        def __init__(self, dev):
            self.dev = dev

        def run(self, cmd, timeout=5.0):
            FakeWCB.sent.append((self.dev, cmd))
            return []

        def send(self, cmd):
            FakeWCB.sent.append((self.dev, cmd))

    class FakeLinks:
        def resync(self, n):
            pass

    class FakeBench:
        def __init__(self):
            self.cache, self.links, self.notes = {}, FakeLinks(), []

        def usb_wcbs(self):
            return {1: "wcb1"}

        def usb_wcb_number(self):
            return 1

        def dev(self, name):
            return name

        def note(self, s):
            self.notes.append(s)

    def guard(wcb, snaps):
        FakeWCB.sent.clear()
        queue = [list(s) for s in snaps]
        old = common.snapshot, common.WCB, common.time.sleep
        common.snapshot, common.WCB, common.time.sleep = (lambda bench, n: queue.pop(0)), FakeWCB, (lambda s: None)
        try:
            with common.config_guard(FakeBench(), wcb):
                pass
        except AssertionError as e:
            return str(e), list(FakeWCB.sent)
        finally:
            common.snapshot, common.WCB, common.time.sleep = old
        raise AssertionError("no leak reported")

    before = ["?HW,24", "?WCB,1", "?BAUD,S3,9600", "?BCAST,OUT,S3,ON", "?BCAST,IN,S3,ON", "?LABEL,S3,Old",
              "?SEQ,SAVE,K1,;S1a", "?ETM,ON", "?MAP,SERIAL,S2,S4"]
    leaked = ["?HW,24", "?WCB,1", "?BAUD,S3,19200", "?BCAST,OUT,S3,OFF", "?BCAST,IN,S3,ON", "?LABEL,S3,New",
              "?LABEL,S5,Extra", "?SEQ,SAVE,K1,;S1b", "?SEQ,SAVE,K9,;S1z", "?ETM,ON", "?MAP,SERIAL,S2,S5",
              "?MAP,SERIAL,S4,S5", "?VAR,SET,v1,3"]
    msg, sent = guard(1, [before, leaked, before])
    assert [c for _, c in sent] == ["?LABEL,CLEAR,S5", "?SEQ,CLEAR,K9", "?MAP,SERIAL,CLEAR,S4", "?VAR,CLEAR,v1",
                                    "?BAUD,S3,9600", "?BCAST,OUT,S3,ON", "?LABEL,S3,Old", "?SEQ,SAVE,K1,;S1a",
                                    "?MAP,SERIAL,S2,S4"], sent
    assert all(d == "wcb1" for d, _ in sent) and "put back by config_guard" in msg and "CONFIG NOT RESTORED" in msg, msg
    # a kind it must never touch stays with the test, and the failure names it
    leaked2 = [x if x != "?ETM,ON" else "?ETM,OFF" for x in before]
    msg, sent = guard(1, [before, leaked2, leaked2])
    assert not sent and "not put back: ['?ETM,OFF', '?ETM,ON']" in msg and "put back by" not in msg, msg
    # a mesh-only board: ;W<n> lines through wcb1, and a ^-chained value is left alone (the sender would split it)
    before3 = ["?WCB,2", "?LABEL,S4,Old", "?SEQ,SAVE,K2,;S2a^;S2b"]
    leaked3 = ["?WCB,2", "?LABEL,S4,New", "?LABEL,S5,Extra"]
    msg, sent = guard(2, [before3, leaked3, leaked3])
    assert sent == [("wcb1", ";W2,?LABEL,CLEAR,S5"), ("wcb1", ";W2,?LABEL,S4,Old")], sent
    assert "not put back: ['?SEQ,SAVE,K2,;S2a^;S2b']" in msg and "board still differs" in msg, msg
    # a leaked delimiter: nothing can be sent safely
    msg, sent = guard(1, [before, before[:-1] + ["?DELIM,|"], before])
    assert not sent and "not put back" in msg, (msg, sent)


def t_ws_frames(tmp):
    """hil.ws: a masked client frame round-trips through parse() in its three length forms, and WsClient completes a
    handshake, sends a command line and reads text frames from a stand-in server on a local socket."""
    import socket
    import threading
    from hil import ws
    for n in (5, 300, 70000):
        data = (bytes(range(256)) * (n // 256 + 1))[:n]
        f = ws.frame(data, mask=b"\x01\x02\x03\x04")
        got = ws.parse(f)
        assert got and got[0] == 0x1 and got[1] == data and got[2] == len(f), n
    assert ws.parse(b"\x81") is None
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    seen = {}

    def server():
        c, _ = srv.accept()
        req = b""
        while b"\r\n\r\n" not in req:
            req += c.recv(4096)
        seen["req"] = req
        c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
        buf = b""
        while True:
            got = ws.parse(buf)
            if got:
                break
            buf += c.recv(4096)
        seen["cmd"] = got[1]
        for line in (b"Software Version: 1\n", b"End of Version\n"):
            c.sendall(b"\x81" + bytes([len(line)]) + line)          # the board's frames are not masked
        c.close()

    th = threading.Thread(target=server, daemon=True)
    th.start()
    cl = ws.WsClient("127.0.0.1", port, timeout=3.0)
    cl.send_text("?VERSION\n")
    text = cl.read_until("End of Version", timeout=3.0)
    cl.close()
    th.join(2)
    srv.close()
    assert b"Upgrade: websocket" in seen["req"] and seen["cmd"] == b"?VERSION\n", seen
    assert "Software Version: 1" in text and "End of Version" in text, text


def t_nvs_parse(tmp):
    """hil/nvs.py reads ?NVS output: the usage line, the namespace lines after it, and nothing from a board without
    ?NVS; the report shows one line per board at start and end."""
    from hil import nvs, checkpoint as ck
    out = ["NVS: used=404 free=226 available=100 total=630 namespaces=32 (64% used)",
           "  maestro_cfg     45", "  phy             66", "End of NVS"]
    stats, spaces = nvs.parse(out)
    assert stats == dict(used=404, free=226, available=100, total=630, namespaces=32, pct=64), stats
    assert spaces == {"maestro_cfg": 45, "phy": 66}, spaces
    assert nvs.parse(["Unknown command: NVS"]) == (None, {})
    stats["spaces"] = spaces
    assert "used 404/630 (64%), available 100; most: phy 66, maestro_cfg 45" == nvs.summary(stats), nvs.summary(stats)
    lines = ck._nvs_lines({"start": {"W1": stats}, "end": {"W1": dict(stats, used=410, available=94, pct=65)}})
    assert lines[0] == "NVS W1: start 404/630 used (64%), 100 available · end 410/630 used (65%), 94 available\n", lines
    assert ck._nvs_lines(None) == [] and ck._nvs_lines({}) == []


class PullDev:
    """A relay's console for the config-pull collector (hil/wcb.py Pull). Each send() is answered by script(cmd, n) -
    n counts the sends from 1 - appended at once, plus `timed` lines appended later from a timer thread (for the
    deadline). Duck-types what Pull reads of a SerialDevice: name, mark(), since(), send(), and log (None, or a
    callable(name, direction, text) as SerialDevice's)."""

    def __init__(self, script=None, timed=()):
        self.name, self.lines, self.sent, self.sent_at = "fakerelay", [], [], []
        self.log = None
        self._script = script or (lambda cmd, n: [])
        self._timed = list(timed)
        self._lock = threading.Lock()

    def _append(self, *lines):
        with self._lock:
            self.lines.extend(lines)

    def mark(self):
        with self._lock:
            return len(self.lines)

    def since(self, m):
        with self._lock:
            return list(self.lines[m:])

    def send(self, text):
        self.sent.append(text)
        self.sent_at.append(time.monotonic())
        self._append(*self._script(text, len(self.sent)))
        for delay, line in self._timed:
            t = threading.Timer(delay, self._append, [line])
            t.daemon = True
            t.start()


def t_mgmt_pull_parts(tmp):
    """F13: hil/wcb.py's config-pull collector against a scripted relay console - the one-line reply; parts with noise
    lines between them, out of order and with a duplicate; a missing part (a timeout naming what arrived); a new id
    (restarts); CFGERR of each code, and read_config retrying only the passing ones; an empty legacy line; trailing
    spaces kept by the '~'; a '^' inside a ?SEQ,SAVE value spanning a part boundary; a CRC failure; the deadline
    restarting on each new part; the pull spacing stamped when the reply completes; and no secret in any message."""
    import types
    from hil import config as C, wcb as W
    chain = "?HW,24^?WCB,2^?WIFI,AP,DomeNet,sekrit99^?EPASS,hunter2^?SEQ,SAVE,HILK,;S1a^;S1b^?LABEL,S3,Dome  ^?CMDCHAR,;"
    reply = f"[VER:6.3.0_TEST]{chain}^?CHK{W.chain_crc(chain)}"
    seq = "?SEQ,SAVE,HILK,;S1a^;S1b"
    msgs = []

    def parts_of(text, cuts, pid="1A2B", wcb=2):
        b = [0] + list(cuts) + [len(text)]
        return [f"[MGMT:CFGPART,{wcb}]P{pid},{k},{len(b) - 1}:{text[b[k - 1]:b[k]]}~" for k in range(1, len(b))]

    def answer(*lines):
        return PullDev(lambda cmd, n: list(lines))

    def fails(fn, exc=AssertionError):
        try:
            fn()
        except exc as e:
            msgs.append(str(e))
            return e
        raise AssertionError(f"{fn} did not raise {exc.__name__}")

    old = W.PULL_SPACING_S, C.time
    W.PULL_SPACING_S = 0
    try:
        # the one-line reply, the ,P form, and mgmt_pull's tuple
        d = answer("[ETM] WCB3 came ONLINE", f"[MGMT:CONFIG,2]{reply}")
        r = W.pull_config(d, 2, timeout=1)
        assert d.sent == ["?MGMT,PULL,2,P"] and (r.kind, r.count, r.part_id) == ("legacy", 1, None) and r.text == reply
        assert r.version == "6.3.0_TEST" and r.provided == r.calc and seq in W.comparable(r.tokens), W.comparable(r.tokens)
        assert W.WCB(answer(f"[MGMT:CONFIG,2]{reply}")).mgmt_pull(2)[0] == "6.3.0_TEST"
        assert W.pull_config(answer(f"[MGMT:CONFIG,2]{reply}"), 2, parts=False, timeout=1).kind == "legacy"
        msgs.append(repr(r))
        # three parts with noise between them, out of order, one twice
        p = parts_of(reply, [45, 90])                   # the reply is 136 characters
        noise = ["[ETM] WCB3 came ONLINE", "[MGMT:CFGPART,3]P0001,1,1:x~", "[MGMT:CONFIG,3][VER:x]y", '{"sys":1}',
                 "[MGMT:CFGERR,3]NOMEM,x", "[MGMT:CFGPART,2]P1A2B,1,3:no tilde"]
        d = answer(p[1], *noise[:3], p[0], p[1], *noise[3:], p[2])
        r = W.pull_config(d, 2, timeout=1)
        assert (r.kind, r.count, r.part_id, r.text) == ("parts", 3, "1A2B", reply), repr(r)
        assert r.part_lengths == [45, 45, len(reply) - 90] and seq in W.comparable(r.tokens)
        msgs.append(repr(r))
        col = W.PullCollector(2)
        assert [col.feed(x) for x in (p[1], p[1], noise[1], noise[5], "plain text", p[2])] == \
            ["partial", "duplicate", "ignored", "malformed", "ignored", "partial"]
        assert col.feed(p[0]) == "complete" and col.reply[1] == reply
        # a missing part: a timeout that names what arrived, and nothing else
        e = fails(lambda: W.pull_config(answer(p[0], p[2]), 2, timeout=0.3))
        assert not isinstance(e, W.PullRefused) and "parts [1, 3] of 3 under id 1A2B" in str(e), e
        # a new id: the parts held are dropped, and the new build's parts join
        stale = chain.replace("?WCB,2", "?WCB,2^?PEERSLIVE,9")
        stale = f"[VER:6.3.0_TEST]{stale}^?CHK{W.chain_crc(stale)}"
        col = W.PullCollector(2)
        assert [col.feed(x) for x in parts_of(stale, [70], "AAAA")[:1] + parts_of(reply, [60], "BBBB")] == \
            ["partial", "partial", "complete"] and col.restarts == 1 and col.result().part_id == "BBBB"
        assert col.result().text == reply
        # CFGERR: the code, whether it passes, the mark it was sent at (a ,P NOPARTS is asked again once: see below)
        for code, retryable in (("NOMEM", True), ("CHANGED", True), ("NOPARTS", True), ("TOOBIG", False)):
            d = answer("noise", p[0], f"[MGMT:CFGERR,2]{code},3100 bytes, free 9000")
            e = fails(lambda: W.pull_config(d, 2, timeout=1), W.PullRefused)
            assert (e.code, e.retryable, e.mark, e.detail) == (code, retryable, 0, "3100 bytes, free 9000"), vars(e)
            assert len(d.sent) == (2 if code == "NOPARTS" else 1), (code, d.sent)
        # a detail that looks like config text (a firmware bug) is withheld, never quoted
        e = fails(lambda: W.pull_config(answer(f"[MGMT:CFGERR,2]NOPARTS,3100 {reply[:90]}"), 2, timeout=1), W.PullRefused)
        assert e.detail.startswith("<") and "withheld" in e.detail, e.detail
        # the bare legacy line: out of memory on a target, or through a relay with no CFGERR
        for line in ("[MGMT:CONFIG,2]", "[MGMT:CONFIG,2]   "):
            e = fails(lambda: W.pull_config(answer(line), 2, timeout=1), W.PullRefused)
            assert (e.code, e.retryable) == ("EMPTY", True), vars(e)
        # trailing spaces at the end of a part survive the join: the '~' frames them
        cut = reply.index("Dome  ") + len("Dome  ")
        p = parts_of(reply, [cut])
        assert p[0].endswith("  ~")
        r = W.pull_config(answer(*p), 2, timeout=1)
        assert r.text == reply and r.provided == r.calc and "?LABEL,S3,Dome  " in r.tokens
        # a '^' inside a ?SEQ,SAVE value, with the part boundary just before or just after it: joined as text first
        for off in (4, 5):
            r = W.pull_config(answer(*parts_of(reply, [reply.index(";S1a^") + off])), 2, timeout=1)
            assert seq in W.comparable(r.tokens), (off, W.comparable(r.tokens))
        # a CRC failure: raised with verify, returned without it
        bad = parts_of(reply, [60])
        bad[1] = bad[1].replace(";S1b", ";S1c")
        e = fails(lambda: W.pull_config(answer(*bad), 2, timeout=1))
        assert "fails its CRC" in str(e), e
        r = W.pull_config(answer(*bad), 2, timeout=1, verify=False)
        assert r.provided != r.calc
        # the deadline restarts on every new part: 0.6 s from the send would expire before part 2 at 0.9 s
        p = parts_of(reply, [70])
        t0 = time.monotonic()
        r = W.pull_config(PullDev(timed=[(0.4, p[0]), (0.9, p[1])]), 2, timeout=0.6)
        assert r.text == reply and time.monotonic() - t0 >= 0.85
        e = fails(lambda: W.pull_config(PullDev(timed=[(0.3, p[0])]), 2, timeout=0.4))
        assert "parts [1] of 2" in str(e), e
        # the spacing counts from the reply's completion, not the send
        W.PULL_SPACING_S = 0.3
        d = PullDev(lambda cmd, n: [] if n == 1 else [f"[MGMT:CONFIG,2]{reply}"], timed=[(0.2, f"[MGMT:CONFIG,2]{reply}")])
        W.pull_config(d, 2, timeout=1)
        W.pull_config(d, 2, timeout=1)
        assert d.sent_at[1] - d.sent_at[0] >= 0.45, d.sent_at
        W.PULL_SPACING_S = 0
        # read_config: a permanent refusal fails at once, a passing one is tried three times, then a success is taken

        class Bench:
            def __init__(self, dev):
                self.d = dev

            def usb_wcbs(self):
                return {}

            def usb_wcb_number(self):
                return 1

            def dev(self, name):
                return self.d

        C.time = types.SimpleNamespace(sleep=lambda s: None)
        # NOPARTS: three tries of a ,P pull that Pull itself sends twice
        for code, sends in (("TOOBIG", 1), ("NOPARTS", 6), ("NOMEM", 3)):
            d = PullDev(lambda cmd, n: [f"[MGMT:CFGERR,2]{code},x"])
            e = fails(lambda: C.read_config(Bench(d), 2), W.PullRefused)
            assert e.code == code and len(d.sent) == sends, (code, len(d.sent))
        d = PullDev(lambda cmd, n: ["[MGMT:CFGERR,2]NOMEM,x"] if n == 1 else parts_of(reply, [90]))
        assert seq in C.read_config(Bench(d), 2) and len(d.sent) == 2
    finally:
        W.PULL_SPACING_S, C.time = old
    leaked = [m for m in msgs if any(s in m for s in SECRETS)]
    assert not leaked, f"{len(leaked)} messages quote a secret"


def _raises(fn, exc=AssertionError):
    try:
        fn()
    except exc as e:
        return e
    raise AssertionError(f"{fn} did not raise {exc.__name__}")


def _drain(p):
    while not p.poll():
        time.sleep(0.01)
    return p


def t_mgmt_pull_noparts_and_codes(tmp):
    """F13 review: a ,P pull answered NOPARTS (every copy of its parts request lost) is sent once more, PULL_SPACING_S
    after the refusal, without blocking poll(), and noted in session.log; the parts that follow complete it, a second
    NOPARTS raises with the first send's mark, and a timeout after the re-send is a timeout. No re-send for a plain
    pull, with retry_noparts=False, or for any other code. A CFGERR code is kept only shaped like the firmware's (else
    MALFORMED, permanent), and its detail only beside a code the firmware sends - no secret in any message."""
    from hil import wcb as W
    chain = "?HW,24^?WCB,2^?WIFI,AP,DomeNet,sekrit99^?EPASS,hunter2^?CMDCHAR,;"
    reply = f"[VER:6.3.0_TEST]{chain}^?CHK{W.chain_crc(chain)}"
    parts = [f"[MGMT:CFGPART,2]P1A2B,1,2:{reply[:50]}~", f"[MGMT:CFGPART,2]P1A2B,2,2:{reply[50:]}~"]
    noparts = "[MGMT:CFGERR,2]NOPARTS,3100 chars, max 2912"
    msgs, notes = [], []
    old = W.PULL_SPACING_S
    W.PULL_SPACING_S = 0
    try:
        # one lost parts request: NOPARTS, then the parts - one re-send, one note, the mark of the first send
        d = PullDev(lambda cmd, n: ["[ETM] WCB3 came ONLINE", noparts] if n == 1 else parts)
        d.log = lambda name, direction, text: notes.append((name, direction, text))
        r = W.pull_config(d, 2, timeout=1)
        assert d.sent == ["?MGMT,PULL,2,P"] * 2, d.sent
        assert (r.kind, r.count, r.text == reply, r.resent, r.mark) == ("parts", 2, True, ["NOPARTS"], 0), repr(r)
        assert len(notes) == 1 and notes[0][:2] == ("fakerelay", "#") and "NOPARTS" in notes[0][2], notes
        msgs += [x[2] for x in notes]
        # the re-send waits PULL_SPACING_S from the refusal, and poll() returns meanwhile
        W.PULL_SPACING_S = 0.3
        d = PullDev(lambda cmd, n: [noparts] if n == 1 else parts)
        p = W.Pull(d, 2, timeout=1)
        t0 = time.monotonic()
        assert p.poll() is False and p.poll() is False and len(d.sent) == 1 and time.monotonic() - t0 < 0.1
        assert _drain(p).reply().resent == ["NOPARTS"] and d.sent_at[1] - d.sent_at[0] >= 0.3, d.sent_at
        W.PULL_SPACING_S = 0
        # a second NOPARTS raises, with the first send's mark
        d = PullDev(lambda cmd, n: ["noise", noparts])
        e = _raises(lambda: W.pull_config(d, 2, timeout=1), W.PullRefused)
        assert (e.code, e.mark, len(d.sent)) == ("NOPARTS", 0, 2), (e.code, e.mark, d.sent)
        msgs.append(str(e))
        # a re-send that gets only part of the reply times out as a timeout, naming what arrived
        d = PullDev(lambda cmd, n: [noparts] if n == 1 else parts[:1])
        e = _raises(lambda: W.pull_config(d, 2, timeout=0.3))
        assert not isinstance(e, W.PullRefused) and len(d.sent) == 2 and "parts [1] of 2" in str(e), str(e)
        # no re-send: a plain pull, retry_noparts=False, another code
        for kw, line in (({"parts": False}, noparts), ({"retry_noparts": False}, noparts),
                         ({}, "[MGMT:CFGERR,2]NOMEM,need 3100 free 9000")):
            d = PullDev(lambda cmd, n: [line])
            e = _raises(lambda: _drain(W.Pull(d, 2, timeout=1, **kw)), W.PullRefused)
            assert len(d.sent) == 1 and e.code == W.parse_error(line.split("]", 1)[1])[0], (kw, e.code, d.sent)
        # the code: 2-12 upper-case letters, else MALFORMED (permanent); the detail: only beside a firmware code
        malformed = "the line does not start with a refusal code"
        for body, code, detail in (
                ("NOMEM,3100 bytes, free 9000", "NOMEM", "3100 bytes, free 9000"),
                ("NOPARTS", "NOPARTS", ""),
                ("?EPASS,hunter2", "MALFORMED", f"<13 characters withheld: {malformed}>"),
                ("?WIFI,AP,DomeNet,sekrit99", "MALFORMED", f"<24 characters withheld: {malformed}>"),
                ("hunter2", "MALFORMED", f"<7 characters withheld: {malformed}>"),          # a bare secret
                ("nomem,x", "MALFORMED", f"<6 characters withheld: {malformed}>"),
                ("N,x", "MALFORMED", f"<2 characters withheld: {malformed}>"),
                ("NOMEMNOMEMNOM,x", "MALFORMED", f"<14 characters withheld: {malformed}>"),  # 13 letters
                ("", "MALFORMED", ""),
                ("SEKRIT,hunter2", "SEKRIT", "<7 characters withheld: SEKRIT is not a code the firmware sends>"),
                ("TOOBIG,3100 ^?EPASS,x", "TOOBIG", "<14 characters withheld: they look like config text>")):
            e = W.PullRefused(2, *W.parse_error(body))
            assert (e.code, e.detail) == (code, detail), (code, e.code, len(e.detail))
            assert e.retryable == (code in W.RETRYABLE) and (code != "MALFORMED" or not e.retryable)
            msgs.append(str(e))
        # through a pull: a malformed refusal is permanent, raised at once
        d = PullDev(lambda cmd, n: ["[MGMT:CFGERR,2]?EPASS,hunter2"])
        e = _raises(lambda: W.pull_config(d, 2, timeout=1), W.PullRefused)
        assert (e.code, e.retryable, len(d.sent)) == ("MALFORMED", False, 1), (e.code, d.sent)
        msgs.append(str(e))
    finally:
        W.PULL_SPACING_S = old
    leaked = [m for m in msgs if any(s in m for s in SECRETS)]
    assert not leaked, f"{len(leaked)} messages quote a secret"


def t_pull_over_limit_policy(tmp):
    """F13 review: navicore.pull_over_limit's verdict (s21 _navicore_library_problems) - silent for both pulls passes
    only while nothing shows the library is new; with F13 (a plain pull answered, or W2 accepting a parts request) the
    ,P pull must end in parts or a passing NOMEM/CHANGED, so NOPARTS twice running (a relay that never sends type 19)
    fails and one lost parts request does not; _navicore_pull_outcome against a scripted NaviCore relay; s03's _refused
    re-arming the one-shot PULLFAULT after a NOPARTS, and _pull_lines screening the code."""
    import types
    from hil import wcb as W
    saved = list(runner.REGISTRY)
    try:
        import suites.s03_wcb as S03
        import suites.s21_navicore_sbus as S21
    finally:
        runner.REGISTRY[:] = saved              # helpers only: the real tests never join a selftest run
    V, msgs = S21._navicore_library_problems, []
    for plain, asked, accepted in (("silent", "silent", False),                        # a library from before F13
                                   ("refused NOPARTS", "parts 2", False), ("refused NOPARTS", "parts 3", True),
                                   ("refused NOPARTS", "refused NOPARTS, then parts 2", True),   # a lost request
                                   ("refused NOPARTS", "refused NOMEM", False),
                                   ("refused NOPARTS", "refused NOPARTS, then refused CHANGED", True)):
        assert V(plain, asked, accepted) == [], (plain, asked, accepted, V(plain, asked, accepted))
    for plain, asked, accepted, why in (
            ("silent", "silent", True, "never reached NaviCore's console"),           # new, and drops type 18
            ("refused NOPARTS", "refused NOPARTS, then refused NOPARTS", False, "never reached W2"),   # ,P ignored
            ("refused NOPARTS", "silent", False, "nothing on packet type 18"),
            ("refused NOPARTS", "refused NOPARTS, then silent", True, "not refused NOPARTS, then silent"),
            ("refused NOPARTS", "refused TOOBIG", False, "not refused TOOBIG"),
            ("refused NOPARTS", "refused EMPTY", False, "not refused EMPTY"),
            ("refused NOPARTS", "refused MALFORMED", False, "not refused MALFORMED"),
            ("refused NOPARTS", "a config line", False, "not a config line"),
            ("silent", "parts 2", False, "silent for both"),
            ("silent", "parts 2", True, "yet the plain pull was silent")):
        got = "; ".join(V(plain, asked, accepted))
        assert why in got, (plain, asked, accepted, got)
        msgs.append(got)

    chain = "?HW,24^?WCB,2^?EPASS,hunter2^?SEQ,SAVE,HILP00," + "z" * 40
    want = f"[VER:6.3.0_TEST]{chain}^?CHK{W.chain_crc(chain)}"
    parts = [f"[MGMT:CFGPART,2]PABCD,1,2:{want[:40]}~", f"[MGMT:CFGPART,2]PABCD,2,2:{want[40:]}~"]
    noparts = "[MGMT:CFGERR,2]NOPARTS,3100 chars, max 2912"
    nomem = "[MGMT:CFGERR,2]NOMEM,need 3100 free 9000"

    def nc(script):
        return types.SimpleNamespace(dev=PullDev(script))

    old = W.PULL_SPACING_S, S21.time
    W.PULL_SPACING_S = 0
    S21.time = types.SimpleNamespace(sleep=lambda s: None, monotonic=time.monotonic)
    try:
        # a new library whose ,P is ignored (type 5 only): NOPARTS for both, the ,P pull asked twice - it fails
        c, problems = nc(lambda cmd, n: [noparts]), []
        plain = S21._navicore_pull_outcome(c, False, 3100, want, problems, timeout=0.5)
        asked = S21._navicore_pull_outcome(c, True, 3100, want, problems, timeout=0.5)
        assert (plain, asked, problems) == ("refused NOPARTS", "refused NOPARTS, then refused NOPARTS", []), \
            (plain, asked, problems)
        assert c.dev.sent == ["?MGMT,PULL,2", "?MGMT,PULL,2,P", "?MGMT,PULL,2,P"], c.dev.sent
        assert "never reached W2" in "; ".join(V(plain, asked, False))
        # one lost parts request: NOPARTS, then parts that join into W2's chain - it passes
        c, problems = nc(lambda cmd, n: [noparts] if n == 1 else parts), []
        asked = S21._navicore_pull_outcome(c, True, 3100, want, problems, timeout=0.5)
        assert (asked, problems) == ("refused NOPARTS, then parts 2", []), (asked, problems)
        assert V("refused NOPARTS", asked, True) == []
        # a library from before F13: nothing at all
        assert S21._navicore_pull_outcome(nc(lambda cmd, n: []), True, 3100, want, [], timeout=0.2) == "silent"
        # a plain pull refused but for NOPARTS, or a config line for an over-limit config: wrong for any library
        c, problems = nc(lambda cmd, n: [nomem]), []
        assert S21._navicore_pull_outcome(c, False, 3100, want, problems, timeout=0.5) == "refused NOMEM"
        assert len(problems) == 1 and "CFGERR NOMEM" in problems[0], problems
        c, problems = nc(lambda cmd, n: [f"[MGMT:CONFIG,2]{want}"]), []
        assert S21._navicore_pull_outcome(c, True, 3100, want, problems, timeout=0.5) == "a config line"
        assert len(problems) == 1 and "[MGMT:CONFIG,2] line" in problems[0], problems
        msgs += problems

        # s03 _refused: a NOPARTS spent the one-shot fault, so it is re-armed and the pull goes once more
        notes = []
        w1 = types.SimpleNamespace(dev=PullDev(lambda cmd, n: [noparts] if n == 1 else [nomem]))
        w1.dev.log = lambda name, direction, text: notes.append(text)
        rearmed, problems = [], []
        mark, e = S03._refused(w1, "NOMEM", problems, rearm=lambda: rearmed.append(len(w1.dev.sent)))
        assert (e.code, mark, problems, rearmed, len(w1.dev.sent)) == ("NOMEM", 1, [], [1], 2), \
            (e.code, mark, problems, rearmed, w1.dev.sent)
        assert len(notes) == 1 and "NOPARTS" in notes[0], notes
        # NOPARTS again: re-armed once, then a problem
        w1, rearmed, problems = types.SimpleNamespace(dev=PullDev(lambda cmd, n: [noparts])), [], []
        mark, e = S03._refused(w1, "NOMEM", problems, rearm=lambda: rearmed.append(1))
        assert e.code == "NOPARTS" and rearmed == [1] and len(w1.dev.sent) == 2, (e.code, rearmed, w1.dev.sent)
        assert len(problems) == 1 and "not NOMEM" in problems[0], problems
        msgs += problems + notes
        # no rearm (the one-line test, where NOPARTS cannot happen), or a plain pull expecting NOPARTS: one send
        for kw, code, bad in (({}, "NOMEM", True), ({"parts": False}, "NOPARTS", False)):
            w1, problems = types.SimpleNamespace(dev=PullDev(lambda cmd, n: [noparts])), []
            S03._refused(w1, code, problems, **kw)
            assert len(w1.dev.sent) == 1 and bool(problems) == bad, (kw, w1.dev.sent, problems)
        # s03 _pull_lines screens the code of a CFGERR line
        d = PullDev()
        d._append("[MGMT:CFGERR,2]?EPASS,hunter2", "[MGMT:CFGERR,2]NOMEM,x", "[MGMT:CFGERR,3]?WIFI,AP,x,sekrit99")
        codes = [code for code, _ in S03._pull_lines(d, 0, 2)["CFGERR"]]
        assert codes == ["MALFORMED", "NOMEM"], codes
    finally:
        W.PULL_SPACING_S, S21.time = old
    leaked = [m for m in msgs if any(s in m for s in SECRETS)]
    assert not leaked, f"{len(leaked)} messages quote a secret"


def t_backup_chain_parse(tmp):
    """s03's ?backup chain parser (wizard.remote_pull, run 20260924-234056): a line printed on the WiFi task between each
    header and its chain; a chain split by one, reported as failing its CRC; a configured chain that is missing, which
    must not pick up the factory one; a bare checksum and an empty chain line; and _factory_reply reading ?backup again
    while its factory chain is broken, then giving up."""
    from hil import wcb as W
    saved = list(runner.REGISTRY)
    try:
        import suites.s03_wcb as S03
    finally:
        runner.REGISTRY[:] = saved              # helpers only: the real tests never join a selftest run
    live, fact = "?HW,24^?WCB,2^?SEQ,SAVE,HILK,;S1a^;S1b^?LABEL,S3,Dome", "?HW,24^?WCB,2^?SEQ,SAVE,HILK,;S1a^;S1b"
    stray = "[ETM] WCB1 came ONLINE (boot) (src MAC: 00:11:22:33:44:55)"

    def chk(chain):
        return f"{chain}^?CHK{W.chain_crc(chain)}"

    def backup(configured, factory, noise=()):
        return (["", "*** ========================================", "*** WCB Configuration Backup", "?HW,24", "?WCB,2",
                 "", "*** === For Configured Boards (Current Delimiter: '^') ==="] + list(noise) + configured +
                [f"*** Checksum: ?CHK{W.chain_crc(live)}", "",
                 "*** === For Factory Reset/Fresh Boards (Uses Default '^' Delimiter) ==="] + list(noise) + factory +
                [f"*** Checksum: ?CHK{W.chain_crc(fact)}", "--------- End of Backup ---------", ""])

    good = backup([chk(live)], [chk(fact)])
    assert S03.backup_chain_lines(good) == {"configured": chk(live), "factory": chk(fact)}
    assert S03.backup_chain_problems(good) == []
    between = backup([chk(live)], [chk(fact)], noise=[stray])
    assert S03.backup_chain_lines(between) == {"configured": chk(live), "factory": chk(fact)}, "a stray line broke it"
    assert S03.backup_chain_problems(between) == []
    cut = chk(fact).index("^?SEQ") + 1
    split = backup([chk(live)], [chk(fact)[:cut], stray, chk(fact)[cut:]])
    probs = S03.backup_chain_problems(split)
    assert len(probs) == 1 and "factory chain" in probs[0] and "fails its CRC" in probs[0], probs
    missing = S03.backup_chain_lines(backup([], [chk(fact)]))
    assert missing == {"configured": "", "factory": chk(fact)}, missing
    assert "not a checksummed chain (0 chars)" in S03.backup_chain_problems(backup([], [chk(fact)]))[0]
    bare = S03.backup_chain_problems(backup([f"^?CHK{W.chain_crc('')}"], [chk(fact)]))
    assert len(bare) == 1 and "configured chain is not a checksummed chain" in bare[0], bare
    assert S03.backup_chain_lines(backup([""], [chk(fact)]))["configured"] == ""
    assert S03.backup_chain_lines(["no backup here"]) == {"configured": None, "factory": None}
    for text in [str(x) for x in (S03.backup_chain_problems(split), bare)]:
        assert "HILK" not in text and "Dome" not in text, "a problem line quoted the chain"

    class W2:
        def __init__(self, *replies):
            self.replies, self.calls = replies, 0

        def run(self, cmd, timeout=None):
            assert cmd == "?backup", cmd
            self.calls += 1
            return self.replies[min(self.calls, len(self.replies)) - 1]

    w2 = W2(split, between)
    assert S03._factory_reply(w2, "6.3.0_T") == f"[VER:6.3.0_T]{chk(fact)}" and w2.calls == 2
    w2 = W2(split)
    try:
        S03._factory_reply(w2, "6.3.0_T")
        raise RuntimeError("a factory chain that never passes its CRC was accepted")
    except AssertionError as e:
        assert w2.calls == 3 and "3 reads" in str(e) and "HILK" not in str(e), (w2.calls, str(e))


def t_run_glued_sentinel(tmp):
    """WCB.run (run 20260925-092255, etm.reboot_defer_cap): the ;S0 echo is two writes, so the WiFi task's 'came
    ONLINE' can land between the sentinel's text and its CRLF. A sentinel that starts its line ends the read, with
    the output before it intact; one that only appears mid-line (?DEBUG's 'Sent to USB: HILEND...') does not."""
    import re as _re
    from hil import wcb as W

    class Dev:
        def __init__(self, shape):
            self.lines, self.shape = [], shape

        def mark(self):
            return len(self.lines)

        def send(self, text, eol=None):
            if text.startswith(";S0,"):
                end = text[4:]
                self.lines += self.shape(end)
            else:
                self.lines += [(0.0, "out 1"), (0.0, "out 2")]

        def expect(self, pattern, timeout=3.0, since=None):
            rx = _re.compile(pattern)
            for _, x in self.lines[since or 0:]:
                if rx.search(x):
                    return rx.search(x)
            raise AssertionError(f"no line matching /{pattern}/")

        def since(self, mark):
            return [x for _, x in self.lines[mark:]]

    def lines(texts):
        return [(0.0, x) for x in texts]

    glued = Dev(lambda end: lines([f"Sent to USB: {end}", f"{end}[ETM] WCB2 came ONLINE (src MAC: 02:05:4B:00:00:02)", ""]))
    assert W.WCB(glued).run("?STATS") == ["out 1", "out 2", f"Sent to USB: {glued.lines[2][1][13:]}"]
    clean = Dev(lambda end: lines([end]))
    assert W.WCB(clean).run("?STATS") == ["out 1", "out 2"]
    midline = Dev(lambda end: lines([f"Sent to USB: {end}"]))
    try:
        W.WCB(midline).run("?STATS", timeout=0.1)
        raise RuntimeError("a mid-line sentinel ended the read")
    except AssertionError:
        pass
    longer = Dev(lambda end: lines([end + "0"]))       # another sentinel that merely starts with this one's text
    try:
        W.WCB(longer).run("?STATS", timeout=0.1)
        raise RuntimeError("a longer hex token ended the read")
    except AssertionError:
        pass


def t_intellex_stage_filter(tmp):
    """hil/intellex.py's stage filter drops the tool bundles, downloaded data, caches, the build stamp and zips from a
    staged src/, and KEEPS src/wiki.html and src/wikidocs.py - source the host imports, which a 'wiki*' glob dropped
    (the first staged host could not have started). include_data keeps the wikis and the firmware cache."""
    from hil import intellex as IX
    src = os.path.join(tmp.root, "src")
    for d in ("wiki", "webui", "webui_wcb", "firmware", "__pycache__"):
        os.makedirs(os.path.join(src, d))
    for f in ("wiki.html", "wikidocs.py", "host.py", "build_stamp.py", "design.zip"):
        open(os.path.join(src, f), "w").close()
    names = sorted(os.listdir(src))
    assert IX._stage_ignore(False)(src, names) == {"wiki", "webui", "webui_wcb", "firmware", "__pycache__",
                                                   "build_stamp.py", "design.zip"}
    assert IX._stage_ignore(True)(src, names) == {"webui", "webui_wcb", "__pycache__", "build_stamp.py", "design.zip"}


def t_intellex_py_judge(tmp):
    """hil/intellex.py judges a tests/intellex/py results file: py_outcome sorts the cases (an unknown status counts as
    failed); judge_py names every failed case, and fails a missing results file, a run with no case and a non-zero exit
    with nothing failed; every case skipped is a Skip with the first reason; passes and skips together pass."""
    from hil import intellex as IX
    from hil.runner import Skip

    def rep(*cs):
        return {"cases": [{"name": n, "status": s, "message": m} for n, s, m in cs]}
    assert IX.py_outcome(rep(("a", "passed", ""), ("b", "failed", "boom"), ("c", "skipped", "why"), ("d", "odd", "?"))) \
        == ([("b", "boom"), ("d", "?")], [("c", "why")], [("a", "")])
    for report, rc, want in ((None, 1, "no results file"), (rep(), 0, "ran no case"),
                             (rep(("a", "passed", "")), 3, "exited 3 with no failed case"),
                             (rep(("a", "failed", "x broke"), ("b", "failed", "y broke")), 1, "a: x broke\nb: y broke")):
        msg = None
        try:
            IX.judge_py("ix.t", report, rc, ["tail line"])
        except AssertionError as e:
            msg = str(e)
        assert msg is not None and want in msg, (report, rc, msg)
    got = None
    try:
        IX.judge_py("ix.t", rep(("a", "skipped", "no AP"), ("b", "skipped", "later")), 0, [])
    except Skip as e:
        got = str(e)
    assert got == "no AP", got
    IX.judge_py("ix.t", rep(("a", "passed", ""), ("b", "skipped", "n/a")), 0, [])


def t_intellex_stream_helpers(tmp):
    """hil/intellex.py's stream and seeding helpers: window() takes the bytes strictly between a start line and the next
    end line, whatever came before; line_spans() finds every whole line holding a repeated needle (NaviCore's PONG);
    boot_markers_in(); fw_key() flattens a branch as Intellex's fwcache._key does; seed_firmware() writes a set and a
    GitHub-shaped listing where a non-frozen host caches; seed_wiki() copies the crafted wiki."""
    from hil import intellex as IX
    data = b"old\r\nnoise X-SYNC tail\r\nline 1\r\nline 2\r\nX-END\r\nafter\n"
    assert IX.window(data, b"X-SYNC", b"X-END") == b"line 1\r\nline 2\r\n"
    assert IX.window(data, b"X-SYNC", b"NOPE") is None
    assert IX.window(b"X-SYNC and no newline yet", b"X-SYNC", b"X-END") is None
    assert IX.window(b"X-END\nX-SYNC\nmid\nX-END\n", b"X-SYNC", b"X-END") == b"mid\n"
    pong = b'{"type":"PONG","version":"v"}'
    d2 = b"a\n" + pong + b"\n" + b"m1\n" + pong + b"\r\n" + b"m2\nm3\n" + pong + b"\n" + pong
    spans = IX.line_spans(d2, b'"type":"PONG"')
    assert len(spans) == 3 and d2[spans[1][1]:spans[2][0]] == b"m2\nm3\n", spans
    assert IX.boot_markers_in(b"x rst:0x1 (POWERON_RESET)\nBooting up the Wireless Communication Board\n",
                              IX.WCB_BOOT_MARKERS) == ["rst:0x", "Booting up the Wireless Communication Board"]
    assert IX.boot_markers_in(b'{"type":"PONG"}\n', IX.NAVICORE_BOOT_MARKERS) == []
    assert (IX.fw_key("feature/x"), IX.fw_key(""), IX.fw_key("a\\b")) == ("feature__x", "main", "a__b")
    d = IX.seed_firmware(tmp.root, "wcb", "feature/x", {"WCB_1_ESP32.bin": b"\x01\x02"})
    assert d == os.path.join(tmp.root, "src", "firmware", "wcb", "feature__x"), d
    with open(os.path.join(d, "WCB_1_ESP32.bin"), "rb") as f:
        assert f.read() == b"\x01\x02"
    with open(os.path.join(d, "listing.json"), encoding="utf-8") as f:
        listing = json.load(f)
    assert listing == [{"name": "WCB_1_ESP32.bin", "path": "Code/bin/WCB_1_ESP32.bin", "type": "file", "size": 2,
                        "download_url": "https://raw.githubusercontent.com/greghulette/Wireless_Communication_Board-WCB/"
                                        "feature/x/Code/bin/WCB_1_ESP32.bin"}], listing
    w = IX.seed_wiki(tmp.root)
    for rel in (("wcb", "Home.md"), ("wcb", "_Sidebar.md"), ("wcb", "Images", "pic.png"), ("intellex", "Home.md")):
        assert os.path.isfile(os.path.join(w, *rel)), rel


def t_intellex_link_tap(tmp):
    """hil/intellex.py LinkTap against a stand-in /_link on a local socket: BINARY and TEXT frames are kept in order as
    bytes, the host's heartbeat ping gets a pong with the same payload, send() writes one masked TEXT frame, wait_for()
    finds a marker and on a timeout names the marker and a byte count - never the stream - and a close frame is recorded."""
    import socket
    import threading
    from hil import intellex as IX
    from hil import ws
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    seen, go_close = {}, threading.Event()

    def frames(c, n):
        buf, out = b"", []
        while len(out) < n:
            got = ws.parse(buf)
            if got:
                out.append(got[:2])
                buf = buf[got[2]:]
                continue
            buf += c.recv(4096)
        return out

    def server():
        c, _ = srv.accept()
        req = b""
        while b"\r\n\r\n" not in req:
            req += c.recv(4096)
        c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
        seen["sent"] = frames(c, 1)
        text = "d€ secret-looking\n".encode()
        c.sendall(b"\x82\x05abc\r\n" + b"\x81" + bytes([len(text)]) + text + b"\x89\x02hb")
        seen["pong"] = frames(c, 1)
        go_close.wait(5)
        c.sendall(b"\x88\x00")
        time.sleep(0.3)
        c.close()

    th = threading.Thread(target=server, daemon=True)
    th.start()
    tap = IX.LinkTap(port, name="T")
    try:
        tap.send("?VERSION\r")
        i = tap.wait_for("d€".encode(), timeout=3)
        data = tap.snapshot()
        assert data == b"abc\r\nd\xe2\x82\xac secret-looking\n" and i == data.find(b"d\xe2\x82\xac") + 4, data
        deadline = time.monotonic() + 3
        while "pong" not in seen and time.monotonic() < deadline:
            time.sleep(0.02)
        assert seen["sent"] == [(0x1, b"?VERSION\r")] and seen.get("pong") == [(0xA, b"hb")], seen
        msg = None
        try:
            tap.wait_for(b"HILNOPE", timeout=0.3)
        except AssertionError as e:
            msg = str(e)
        assert msg and "HILNOPE" in msg and "bytes so far" in msg and "secret" not in msg, msg
        go_close.set()
        deadline = time.monotonic() + 3
        while tap.closed is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert tap.closed == "the host closed the socket", tap.closed
        msg = None
        try:
            tap.wait_for(b"HILNOPE", timeout=2)
        except AssertionError as e:
            msg = str(e)
        assert msg and "ended" in msg, msg
    finally:
        tap.close()
        th.join(3)
        srv.close()


def t_intellex_github_dir(tmp):
    """hil/intellex.py finds the sibling repos (Intellex, NaviCore) beside the main checkout, also when it runs from a git
    worktree under <repo>/.claude/worktrees/<name>, where there are none beside .claude/worktrees."""
    from hil import intellex as IX
    j = os.path.join
    main = j("C:" + os.sep, "Users", "g", "GitHub", "Wireless_Communication_Board-WCB")
    assert IX.github_dir(main) == j("C:" + os.sep, "Users", "g", "GitHub"), IX.github_dir(main)
    wt = j(main, ".claude", "worktrees", "agent-abc123")
    assert IX.github_dir(wt) == j("C:" + os.sep, "Users", "g", "GitHub"), IX.github_dir(wt)
    assert IX.github_dir(j(main, "claude", "worktrees", "x")) == j(main, "claude", "worktrees")


def t_intellex_handed_over(tmp):
    """hil/intellex.py handed_over: the port is released first and taken back after, the board checked; a board that
    does not come back fails a passing block, is named after a failing block's own failure, and after an abort is only
    noted so the abort stays an abort."""
    from hil import intellex as IX

    class B:
        cfg = {"devices": {"wcb1": {"port": "COM6", "kind": "wcb"}}}

        def __init__(self):
            self.calls, self.notes = [], []

        def close_device(self, name):
            self.calls.append(("close", name))

        def note(self, text):
            self.notes.append(text)

    real = IX._reacquire
    try:
        b = B()
        IX._reacquire = lambda bench, dev: bench.calls.append(("back", dev))
        with IX.handed_over(b, "wcb1") as port:
            assert port == "COM6" and b.calls == [("close", "wcb1")]
        assert b.calls == [("close", "wcb1"), ("back", "wcb1")], b.calls

        def gone(bench, dev):
            raise AssertionError("wcb1 did not come back")
        IX._reacquire = gone
        for body, want in ((None, "wcb1 did not come back"), (AssertionError("the body failed"),
                                                              "the body failed\nwcb1 did not come back afterwards")):
            msg = None
            try:
                with IX.handed_over(B(), "wcb1"):
                    if body:
                        raise body
            except AssertionError as e:
                msg = str(e)
            assert msg is not None and msg.startswith(want), (want, msg)
        b = B()
        aborted = False
        try:
            with IX.handed_over(b, "wcb1"):
                raise KeyboardInterrupt
        except KeyboardInterrupt:
            aborted = True
        assert aborted and b.notes and "not reacquired after the abort" in b.notes[0], b.notes
    finally:
        IX._reacquire = real


# ---------------------------------------------------------------------------- NaviCore and SBUS drivers (INF1, INF2)
MODE_LINE = "Mode=1  matrixBtn=0  matrixVal=992"        # #L12's answer (NaviCore.ino:3627): the driver's flush


class FakeNaviDev:
    """NaviCore's (or the SBUS controller's) USB console for hil/navicore.py and hil/sbus.py: each send() is answered by
    script(text, n) - n counts the sends from 1 - with lines appended at once, and later(delay, *lines) appends lines
    from a timer. Duck-types what the drivers read of a SerialDevice: name, port, lines [(t, text)], mark(), since(),
    send(), expect() (it waits, as SerialDevice's does), log, and send_paced() (recorded in `paced`)."""

    def __init__(self, script=None, name="fakenavi"):
        self.name, self.port, self.log = name, "COMFAKE", None
        self.lines, self.sent, self.paced = [], [], []
        self._script = script or (lambda text, n: [])
        self._lock = threading.Lock()

    def _append(self, *texts):
        with self._lock:
            self.lines.extend((time.monotonic(), t) for t in texts)
        for t in texts:
            if self.log:
                self.log(self.name, "<", t)

    def later(self, delay, *texts):
        t = threading.Timer(delay, self._append, texts)
        t.daemon = True
        t.start()

    def mark(self):
        with self._lock:
            return len(self.lines)

    def since(self, m):
        with self._lock:
            return [t for _, t in self.lines[m:]]

    def send(self, text, eol="\n"):
        if self.log:
            self.log(self.name, ">", text)
        self.sent.append(text)
        self._append(*self._script(text, len(self.sent)))

    def send_paced(self, text, chunk=512, gap_s=0.004):
        self.paced.append((len(text), chunk, gap_s))
        self.send(text)

    def expect(self, pattern, timeout=3.0, since=None):
        import re as _re
        from hil.serialdev import ExpectTimeout
        rx, start = _re.compile(pattern), (self.mark() if since is None else since)
        deadline = time.monotonic() + timeout
        while True:
            for text in self.since(start):
                m = rx.search(text)
                if m:
                    return m
            if time.monotonic() >= deadline:
                raise ExpectTimeout(f"{self.name}: no line matching /{pattern}/ within {timeout}s")
            time.sleep(0.005)


class RecSer:
    """A pyserial handle that records its writes and its RTS/DTR changes, in order, and can fail a write."""

    def __init__(self, fail_after=None, exc=None):
        self.writes, self.control, self.fail_after, self.exc = [], [], fail_after, exc

    def write(self, data):
        if self.fail_after is not None and len(self.writes) >= self.fail_after:
            raise self.exc
        self.writes.append(bytes(data))
        return len(data)

    def __setattr__(self, key, value):
        if key in ("rts", "dtr"):
            self.control.append((key, value))
        object.__setattr__(self, key, value)


def t_nc_transport(tmp):
    """hil/serialdev.py's additions for the NaviCore driver: send_paced() writes a long line in 512-byte chunks (the
    config tool's USB_CHUNK) and a short one in one write, logging it once; a write that fails part-way raises at once
    naming how far it got, never resuming; usb_jtag_reset() sets RTS=1 then DTR=0, and releases with RTS=0 then a DTR
    write (usbser.sys sends the line state only on a DTR write), and refuses a closed port; NaviCore.hard_reset()
    returns the mark taken before the pulse, and NaviCore.send_paced() falls back to one send on a transport without
    paced writes."""
    import serial
    from hil import navicore as NC
    from hil.serialdev import ExpectTimeout, SerialDevice, usb_jtag_reset
    dev, logged = SerialDevice("fakenc", "COMX"), []
    dev.log = lambda name, direction, text: logged.append((direction, len(text)))
    dev._ser = RecSer()
    dev.send_paced("a" * 1300)
    assert [len(w) for w in dev._ser.writes] == [512, 512, 277] and b"".join(dev._ser.writes) == b"a" * 1300 + b"\n"
    assert logged == [(">", 1300)], logged
    dev._ser = RecSer()
    dev.send_paced("short")
    assert dev._ser.writes == [b"short\n"], dev._ser.writes
    for exc, why in ((serial.SerialException("gone"), "failed 512 bytes into a 1301-byte"),
                     (serial.SerialTimeoutException("slow"), "timed out 512 bytes into a 1301-byte")):
        dev._ser = RecSer(fail_after=1, exc=exc)
        e = _raises(lambda: dev.send_paced("a" * 1300), ExpectTimeout)
        assert why in str(e) and len(dev._ser.writes) == 1, (e, dev._ser.writes)
    assert any(x.startswith("<<write error: gone") for x in dev.since(0)), dev.since(0)
    dev._ser = RecSer()
    usb_jtag_reset(dev, hold_s=0.01)
    assert dev._ser.control == [("rts", True), ("dtr", False), ("rts", False), ("dtr", False)], dev._ser.control
    before = dev.mark()
    assert NC.NaviCore(dev).hard_reset(0.01) == before and len(dev._ser.control) == 8
    dev._ser = None
    _raises(lambda: usb_jtag_reset(dev, hold_s=0.01), ExpectTimeout)

    class Plain(FakeNaviDev):
        send_paced = None                          # a transport with no paced writes (a WebSocket, INF5)
    plain = Plain()
    NC.NaviCore(plain).send_paced("x" * 600)
    assert plain.sent == ["x" * 600] and plain.paced == [], plain.sent


def t_nc_fnv1a(tmp):
    """hil/navicore.py fnv1a32 is rcCmdlibHash (NaviCore rc_config.h:2159-2164): the published FNV-1a 32 test vectors, str
    and bytes alike. Then the command library against a scripted NaviCore: set_cmdlib() sends one paced line with
    "data" last and checks the ACK's size and hash against its own FNV-1a of the UTF-8 bytes (a non-ASCII library
    included); cmdlib() returns the exact bytes, and a reply whose bytes do not match its size and hash fails;
    cmdlib_meta(); a library that is not one line of one JSON value is refused before sending."""
    from hil import navicore as NC
    # The FNV-1a 32 reference vectors (Fowler/Noll/Vo, isthe.com/chongo/tech/comp/fnv): "" is the offset basis itself.
    assert (NC.fnv1a32(b""), NC.fnv1a32(b"a"), NC.fnv1a32("foobar")) == (0x811C9DC5, 0xE40C292C, 0xBF9CF968)
    assert NC.NaviCore.fnv1a32("é") == NC.fnv1a32("é".encode("utf-8")) != NC.fnv1a32("é".encode("latin-1"))
    lib = '{"boards":[{"id":"HILbrd","name":"Dôme","cmds":[";S1x"]}],"enums":{}}'
    state = {"lib": '{"boards":[],"enums":{}}', "corrupt": False}   # the empty library GET_CMDLIB sends (:3866)

    def script(text, n):
        if text == '{"type":"GET_CMDLIB"}':
            body = state["lib"]
            size = len(body.encode()) + (1 if state["corrupt"] else 0)
            return [f'{{"type":"CMDLIB","size":{size},"hash":{NC.fnv1a32(body)},"data":{body}}}']
        if text == '{"type":"GET_CMDLIB_META"}':
            return [f'{{"type":"CMDLIB_META","size":{len(state["lib"].encode())},"hash":{NC.fnv1a32(state["lib"])}}}']
        if text.startswith('{"type":"SET_CMDLIB","data":'):
            state["lib"] = text[len('{"type":"SET_CMDLIB","data":'):-1]         # "data" is last: the value, verbatim
            body = state["lib"].encode()
            return [f'{{"type":"ACK","of":"SET_CMDLIB","ok":true,"size":{len(body)},"hash":{NC.fnv1a32(body)}}}']
        return []
    d = FakeNaviDev(script)
    nc = NC.NaviCore(d)
    assert nc.cmdlib() == b'{"boards":[],"enums":{}}'
    ack = nc.set_cmdlib("  " + lib + " ")
    assert ack["ok"] and state["lib"] == lib and len(d.paced) == 1, (ack, d.paced)
    assert (ack["size"], ack["hash"]) == (len(lib.encode()), NC.fnv1a32(lib)) and len(lib.encode()) > len(lib)
    assert nc.cmdlib() == lib.encode() and nc.cmdlib_meta() == (len(lib.encode()), NC.fnv1a32(lib))
    state["corrupt"] = True
    assert "arrived; NaviCore sent" in str(_raises(nc.cmdlib))
    sent = len(d.sent)
    for bad in ('{"a":1}\n', "5", '{"a":'):
        _raises(lambda: nc.set_cmdlib(bad), ValueError)
    assert len(d.sent) == sent


def t_nc_pwm_update(tmp):
    """parse_pwm_update on a monitor frame built with NaviCore's own format string (sendPWMUpdate, NaviCore.ino:
    3142-3147), and monitor() against a scripted NaviCore: START_MONITOR and STOP_MONITOR each ACKed, the frames
    between them parsed in order, a frame cut short left out and noted in session.log, and lines that are not whole
    frames refused."""
    from hil import navicore as NC
    fmt = ('{"type":"PWM_UPDATE","matrixCh":%d,"modeCh":%d,"matrixVal":%d,"modeVal":%d,"btn":%d,"mode":%d,'
           '"sbus":{"ok":%s,"fps":%d,"frames":%lu,"ageMs":%lu,"lost":%s,"failsafe":%s,"chCount":%d,"frameLen":%d,'
           '"channels":[%s]}}').replace("%lu", "%d")
    chans = [992] * 7 + [173] * 6 + [992, 173, 173, 173] + [992] * 7

    def frame(k):
        return fmt % (7, 12, 992, 1811, 0, 1, "true", 111, 444673 + k, 4, "false", "false", 24, 36,
                      ",".join(map(str, chans)))
    f = NC.parse_pwm_update(frame(0))
    assert (f["matrixCh"], f["modeCh"], f["mode"], f["sbus"]["fps"], f["sbus"]["channels"]) == (7, 12, 1, 111, chans)
    for bad in ('{"type":"PWM_UPDATE","sbus":{"chCount":24,"channels":[1,2]}}', MODE_LINE, frame(0)[:-9]):
        _raises(lambda: NC.parse_pwm_update(bad), ValueError)
    notes = []

    def script(text, n):
        if text == '{"type":"START_MONITOR"}':
            return ['{"type":"ACK","ok":true}', frame(1), frame(2), frame(3)[:50], frame(4)]
        return ['{"type":"ACK","ok":true}'] if text == '{"type":"STOP_MONITOR"}' else []
    d = FakeNaviDev(script)
    d.log = lambda name, direction, text: notes.append(text) if direction == "#" else None
    frames = NC.NaviCore(d).monitor(0.05)
    assert [x["sbus"]["frames"] for x in frames] == [444674, 444675, 444677], frames
    assert d.sent == ['{"type":"START_MONITOR"}', '{"type":"STOP_MONITOR"}'], d.sent
    assert len(notes) == 1 and "1 PWM_UPDATE line(s)" in notes[0], notes


def t_nc_mae_markers(tmp):
    """parse_mae on every [MAE:n] shape maestroReportQuery prints (NaviCore.ino:741-756): values, error words, another
    slot or channel never answering; then the Maestro methods against a scripted NaviCore - each ?MAE line followed by
    the #L12 flush, answers as int or error word, a query with no marker failing with what was printed - and the
    helpers that pick a slot: local slots only, a channel a passthrough knob drives passed over, Skip when none
    answers."""
    from hil import navicore as NC
    text = "\n".join(['[MAE:1]{"q":"pos","ch":0,"val":6400}', '[MAE:9]{"q":"pos","ch":0,"err":"disabled"}',
                      '[MAE:1]{"q":"mov","val":1}', '[MAE:0]{"q":"mov","err":"disabled"}', '[MAE:1]{"q":"err","val":0}',
                      '[MAE:2]{"q":"pos","ch":5,"err":"timeout"}'])
    assert (NC.parse_mae(text, 1, "pos", 0), NC.parse_mae(text, 9, "pos", 0)) == (6400, "disabled")
    assert (NC.parse_mae(text, 1, "mov"), NC.parse_mae(text, 0, "mov"), NC.parse_mae(text, 1, "err")) == (1, "disabled", 0)
    assert NC.parse_mae(text, 2, "pos", 5) == "timeout"
    assert NC.parse_mae(text, 1, "pos", 1) is None and NC.parse_mae(text, 3, "mov") is None
    assert NC.parse_mae('[MAE:11]{"q":"mov","val":0}', 1, "mov") is None      # slot 11 is not slot 1
    answers = {"?MAE,GET,1,0": '[MAE:1]{"q":"pos","ch":0,"val":6000}', "?MAE,MOVING,1": '[MAE:1]{"q":"mov","val":0}',
               "?MAE,ERR,1": '[MAE:1]{"q":"err","val":0}', "?MAE,GET,2,0": '[MAE:2]{"q":"pos","ch":0,"val":4000}',
               "?MAE,GET,2,1": '[MAE:2]{"q":"pos","ch":1,"val":5000}',
               "?MAE,GET,3,0": '[MAE:3]{"q":"pos","ch":0,"err":"timeout"}'}
    d = FakeNaviDev(lambda text, n: [MODE_LINE] if text == "#L12" else ([answers[text]] if text in answers else []))
    nc = NC.NaviCore(d)
    assert (nc.mae_get(1, 0), nc.mae_moving(1), nc.mae_err(1)) == (6000, 0, 0)
    nc.mae_set(1, 0, 6000)
    nc.mae_free(1, 0)
    assert d.sent == ["?MAE,GET,1,0", "#L12", "?MAE,MOVING,1", "#L12", "?MAE,ERR,1", "#L12", "?MAE,1,0,6000", "#L12",
                      "?MAE,FREE,1,0", "#L12"], d.sent
    e = _raises(lambda: nc.mae_get(4, 0))
    assert "?MAE,GET,4,0 printed" in str(e) and MODE_LINE in str(e), e
    cfg = {"maestros": [{"type": 2, "device": 2}, {"type": 1, "device": 1}],
           "knobs": {"0": {"function": 1, "outputs": [{"target": 2, "maestroCh": 0}]}}}
    assert nc.local_slots(cfg) == [(2, 1)]
    assert nc.usable_slot(cfg) == (2, 1, 4000) and nc.undriven_channel(cfg) == (2, 1, 1, 5000)
    _raises(lambda: nc.usable_slot({"maestros": [{}, {}, {"type": 1, "device": 3}]}), runner.Skip)


def t_nc_clip_items(tmp):
    """?REC,LS's [CLIPITEM] lines (navicore_record.h listClips :960-982) through parse_clip_items and rec_ls(); ?REC,INFO
    through rec_info(); rec_rm() deleted and delete failed, and a clip name the board would rewrite (_clipPath keeps
    only [A-Za-z0-9_-], 32 of them) refused before anything is sent."""
    from hil import navicore as NC
    ls = ['[CLIPFS]{"total":12582912,"used":811008}', "[REC] clips:", "[CLIPLIST:BEGIN]",
          '[CLIPITEM]{"name":"HILa","bytes":26756,"dur":1767,"n":191}',
          '[CLIPITEM]{"name":"HIL_b-2","bytes":140,"dur":0,"n":1}', "[CLIPLIST:END]"]
    assert NC.parse_clip_items(ls) == [{"name": "HILa", "bytes": 26756, "dur": 1767, "n": 191},
                                       {"name": "HIL_b-2", "bytes": 140, "dur": 0, "n": 1}]
    replies = {"?REC,LS": ls, "?REC,INFO": ["[REC] state=idle  events=0/24000  dur=0ms  drops=0  buf=ok"],
               "?REC,RM,HILa": ["[REC] deleted"], "?REC,RM,HILz": ["[REC] delete failed"], "#L12": [MODE_LINE]}
    d = FakeNaviDev(lambda text, n: replies.get(text, []))
    nc = NC.NaviCore(d)
    assert nc.rec_ls() == [("HILa", 26756, 1767, 191), ("HIL_b-2", 140, 0, 1)]
    assert nc.rec_info() == ("idle", "0", "24000", "0", "0", "ok")
    assert nc.rec_rm("HILa") is True and "delete failed" in str(_raises(lambda: nc.rec_rm("HILz")))
    sent = len(d.sent)
    for bad in ("HIL x", "", "HIL/../x", "H" * 33, "HILé"):
        _raises(lambda: nc.rec_rm(bad), ValueError)
    assert len(d.sent) == sent, "a refused name was sent"


def t_nc_recorder_transfer(tmp):
    """rec_download() and rec_upload() against a scripted editStream (navicore_record.h :731-950): the (0,0) probe, then
    ranges with batched keyframes ([CLIPDL:EVB]) and an action ([CLIPDL:EV]) rebuilt into the per-event shape, a stale
    END from another clip ignored, a range with a cut line asked again; refused - a truncated clip (fc != count), a
    buffer that changed between ranges, a missing clip, firmware without ranged download. The upload writes each event
    at its index, retries a NAK, and on a refused EDITEND or EDITBEGIN fails, sending EDITCANCEL only once editing."""
    import re as _re
    from hil import navicore as NC
    clip = [{"t": 0, "k": 1, "slot": 3, "ch": 0, "pos": 6000}, {"t": 20, "k": 1, "slot": 3, "ch": 1, "pos": 5000},
            {"t": 40, "k": 0, "type": "wcb_unicast", "target": "1", "cmd": ";S2HILx"},
            {"t": 60, "k": 2, "chan": 1, "vol": 40}, {"t": 80, "k": 1, "slot": 3, "ch": 0, "pos": 6400},
            {"t": 90, "k": 1, "slot": 3, "ch": 1, "pos": 5200}, {"t": 100, "k": 1, "slot": 4, "ch": 2, "pos": 7000}]
    j = lambda o: json.dumps(o, separators=(",", ":"))      # noqa: E731

    def stream(name, first, want, st):
        n = min(want, len(clip) - first)
        fp = st["fp"].pop(0) if isinstance(st["fp"], list) else st["fp"]
        head = {"count": len(clip), "durationMs": 100, "mode": 1, "from": first, "n": n, "fp": fp,
                "fc": st.get("fc", len(clip)), "nm": name}
        if st.get("legacy"):
            head = {"count": len(clip), "durationMs": 100, "mode": 1}
        out, rows, rfirst = ["[CLIPDL:BEGIN]" + j(head)], [], None
        for i in range(first, first + n):
            ev = clip[i]
            if ev["k"]:
                rfirst = i if not rows else rfirst
                rows.append([ev["t"], 1, ev["slot"], ev["ch"], ev["pos"]] if ev["k"] == 1 else
                            [ev["t"], 2, ev["chan"], ev["vol"], 0])
                continue
            if rows:
                out.append(f"[CLIPDL:EVB,{rfirst}]" + j({"e": rows}))
                rows = []
            out.append(f"[CLIPDL:EV,{i}]" + j(ev))
        if rows:
            out.append(f"[CLIPDL:EVB,{rfirst}]" + j({"e": rows}))
        if st.get("legacy"):
            return out + ["[CLIPDL:END]"]                              # firmware from before the ranged form
        return out + ["[CLIPDL:END]" + j({"from": first, "n": n, "fp": fp, "nm": name})]

    def downloader(st):
        def script(text, n):
            if text == "#L12":
                return [MODE_LINE]
            m = _re.match(r"^\?REC,EDITLOAD,(\w+),(\d+),(\d+),B$", text)
            if not m:
                return []
            name, first, want = m.group(1), int(m.group(2)), int(m.group(3))
            if name != "HILclip":
                return [f"[REC] clip '{name}' not found"]
            st["asked"].append((first, want))
            lines = stream(name, first, want, st)
            if (first, want) == (0, 4) and st["asked"].count((0, 4)) == 1:
                lines = ['[CLIPDL:END]{"from":0,"n":4,"fp":"00000000","nm":"HILold"}'] + lines   # a stale range
                lines[2] = lines[2][:25]                                  # the first EVB line cut short
            return lines
        return script
    st = {"fp": "1A2B3C4D", "asked": []}
    d = FakeNaviDev(downloader(st))
    nc = NC.NaviCore(d)
    assert nc.rec_download("HILclip", chunk=4) == clip
    assert st["asked"] == [(0, 0), (0, 4), (0, 4), (4, 3)], st["asked"]
    assert d.sent[1::2] == ["#L12"] * 4 and all(x.startswith("?REC,EDITLOAD,HILclip,") for x in d.sent[::2]), d.sent
    for st, why in (({"fp": "1A2B3C4D", "asked": [], "fc": 9}, "truncated on the board: its file holds 9 events"),
                    ({"fp": ["AAAA0000", "BBBB1111"], "asked": []}, "changed on the board mid-download"),
                    ({"fp": "1A2B3C4D", "asked": [], "legacy": True}, "no ranged download")):
        assert why in str(_raises(lambda: NC.NaviCore(FakeNaviDev(downloader(st))).rec_download("HILclip", chunk=4)))
    assert "not on NaviCore's clips partition" in str(_raises(lambda: nc.rec_download("HILnone")))

    def uploader(st):
        def script(text, n):
            if text == "#L12":
                return [MODE_LINE]
            if text == "?REC,EDITBEGIN":
                return [st.get("begin", "[CLIPUL:BEGIN,OK]")]
            m = _re.match(r"^\?REC,EDITEV,(\d+),(.*)$", text)
            if m:
                i = int(m.group(1))
                if i == 2 and not st.get("nakked"):
                    st["nakked"] = True
                    return ["[CLIPUL:NAK,bad event / bad index / not editing]"]
                st["got"][i] = json.loads(m.group(2))
                return [f"[CLIPUL:ACK,{i}]"]
            if text.startswith("?REC,EDITEND,"):
                return [st.get("end", "[CLIPUL:END,OK]")]
            return ["[CLIPUL:CANCEL,OK]"] if text == "?REC,EDITCANCEL" else []
        return script
    st = {"got": {}}
    d = FakeNaviDev(uploader(st))
    assert NC.NaviCore(d).rec_upload("HILup", clip) == len(clip)
    assert [st["got"][i] for i in range(len(clip))] == clip
    assert sum(x.startswith("?REC,EDITEV,2,") for x in d.sent) == 2 and "?REC,EDITCANCEL" not in d.sent
    assert d.sent[-2:] == ["?REC,EDITEND,HILup", "#L12"], d.sent[-2:]
    st = {"got": {}, "end": "[CLIPUL:END,ERR,save-failed]"}
    d = FakeNaviDev(uploader(st))
    assert "save-failed" in str(_raises(lambda: NC.NaviCore(d).rec_upload("HILup", clip)))
    assert d.sent[-1] == "?REC,EDITCANCEL", d.sent[-1]
    st = {"got": {}, "begin": "[CLIPUL:BEGIN,ERR,busy]"}
    d = FakeNaviDev(uploader(st))
    assert "EDITBEGIN refused" in str(_raises(lambda: NC.NaviCore(d).rec_upload("HILup", clip)))
    assert not any(x.startswith(("?REC,EDITEV", "?REC,EDITCANCEL")) for x in d.sent), d.sent
    _raises(lambda: NC.NaviCore(d).rec_upload("HILup", []), ValueError)


def t_nc_mesh_stats(tmp):
    """merge_mesh_stats on the shapes buildMeshStatsPage writes (rc_telemetry.h:1361-1432): one USB page; bridged pages
    (page 0 with the aggregate and no rows, page 1 with the rows and "last":1) merged by board id; a set with no last
    page incomplete; and mesh_stats() sending the line navicore.mesh_stats_counts always sent."""
    from hil import navicore as NC
    usb = ('{"type":"MESH_STATS","pg":0,"self":20,"upMs":100955926,"agg":{"sent":5,"ackd":5,"rty":0,"fail":0,"ung":0,'
           '"bcast":6,"recv":2},"peers":[[1,4,4,0,0,0,2],[2,1,1,0,0,0,0]],"last":1}')
    s = NC.merge_mesh_stats([json.loads(usb)])
    assert (s["self"], s["agg"]["bcast"], sorted(s["peers"]), s["peers"][1], s["complete"]) == \
        (20, 6, [1, 2], [1, 4, 4, 0, 0, 0, 2], True), s
    p0 = ('{"sys":1,"type":"MESH_STATS","pg":0,"self":20,"upMs":1,"agg":{"sent":9,"ackd":8,"rty":1,"fail":1,"ung":0,'
          '"bcast":3,"recv":7},"peers":[]}')
    p1 = '{"sys":1,"type":"MESH_STATS","pg":1,"self":20,"peers":[[1,5,4,1,1,0,6],[2,4,4,0,0,0,1]],"last":1}'
    s = NC.merge_mesh_stats([json.loads(p0), json.loads(p1)])
    assert s["agg"]["recv"] == 7 and s["peers"][2] == [2, 4, 4, 0, 0, 0, 1] and s["complete"], s
    assert not NC.merge_mesh_stats([json.loads(p0)])["complete"]
    assert NC.merge_mesh_stats([]) == {"self": None, "upMs": None, "agg": {}, "peers": {}, "complete": False, "pages": []}
    d = FakeNaviDev(lambda text, n: [usb] if text == '{"type":"GET_MESH_STATS"}' else [])
    assert NC.NaviCore(d).mesh_stats()["peers"][2][1] == 1 and d.sent == ['{"type":"GET_MESH_STATS"}'], d.sent


def t_nc_boot_banner(tmp):
    """parse_boot on a banner in setup()'s formats (NaviCore.ino printBootTelemetry :4367-4415, :4899-4918; a reset
    reason whose name holds parentheses) and on the ROM's download-mode lines; then wait_boot() against a scripted
    NaviCore - the banner ends it; with the banner lost to the USB re-enumeration, a PONG after '<<reopened' ends it
    (no PING before the reopen); 'waiting for download' fails at once; silence fails at the timeout, saying the port
    never reopened - and reboot() by REBOOT (ACKed) and by #L02."""
    from hil import navicore as NC
    banner = ["", "=== NaviCore ===",
              "Reset reason: 3 - Software restart (incl. boot-guard retry)  (RTC codes core0=3 [SW system] core1=3 "
              "[SW system])", "Boot attempts since power applied: 2   <-- board retried/reset before this boot",
              "[WCB] Joined network as device ID 20 (quantity=1)", "[NaviCore] Firmware v9.9.9_TEST — setup complete.",
              "  Connect config_tool/index.html via Web Serial for configuration."]
    assert NC.parse_boot(banner) == {"complete": True, "version": "v9.9.9_TEST", "reset_code": 3,
                                     "reset": "Software restart (incl. boot-guard retry)", "rtc": (3, 3),
                                     "attempts": 2, "device_id": 20, "quantity": 1, "download_mode": False}
    rom = NC.parse_boot(["ESP-ROM:esp32s3-20210327", "rst:0x1 (POWERON),boot:0x0 (DOWNLOAD(USB/UART0))",
                         "waiting for download"])
    assert rom["download_mode"] and not rom["complete"] and rom["version"] is None, rom
    pong, ping = '{"type":"PONG","version":"v9.9.9_TEST"}', '{"type":"PING"}'

    def board(st):
        def script(text, n):
            if text == ping:
                return [pong] if st.get("up") else []
            if text == '{"type":"REBOOT"}':
                d.later(0.1, *banner)
                return ['{"type":"ACK","ok":true,"msg":"rebooting"}']
            if text == "#L02":
                d.later(0.1, *banner)
            return []
        return script
    d = FakeNaviDev(board({"up": True}))
    m = d.mark()
    d.later(0.1, *banner)
    assert banner[5] in NC.NaviCore(d).wait_boot(since=m, timeout=3) and d.sent == [ping], d.sent
    st = {}
    d = FakeNaviDev(board(st))
    m = d.mark()
    booted = threading.Timer(0.2, st.update, [{"up": True}])      # up before the port reopens: no PING goes unanswered
    booted.daemon = True
    booted.start()
    d.later(0.3, "<<serial error: device gone>>", "<<reopened COMFAKE>>")
    lines = NC.NaviCore(d).wait_boot(since=m, timeout=3)
    assert "<<reopened COMFAKE>>" in lines and d.sent == [ping, ping], d.sent
    d = FakeNaviDev(board({"up": True}))
    d.later(0.05, "waiting for download")
    t0 = time.monotonic()
    assert "ROM download mode" in str(_raises(lambda: NC.NaviCore(d).wait_boot(since=0, timeout=5)))
    assert time.monotonic() - t0 < 1.5
    e = _raises(lambda: NC.NaviCore(FakeNaviDev(board({}))).wait_boot(timeout=0.4))
    assert "did not come back within 0.4 s" in str(e) and "never reopened" in str(e), e
    for how, first in (("json", '{"type":"REBOOT"}'), ("l02", "#L02")):
        d = FakeNaviDev(board({"up": True}))
        lines = NC.NaviCore(d).reboot(how, timeout=3)
        assert NC.parse_boot(lines)["complete"] and d.sent[0] == first, (how, d.sent)
    _raises(lambda: NC.NaviCore(d).reboot("power"), ValueError)


def t_nc_wdp_views(tmp):
    """?WDP,DUMP through parse_wdp (WCB_Mgmt.h printWdpDump :221-262): the SELF row, a neighbour, a port label, the
    WDPCFG summary and the END count; wdp_dump() rows (SELF included, as before), self_row(), wdpcfg(); and
    version_surfaces() over a scripted NaviCore and a scripted W1 (hil.wcb.WCB), a silent surface reading None."""
    from hil import navicore as NC
    from hil import wcb as W
    dump = ["[WDP:N=20,CLIENT=0,ALIAS=NaviCore,HW=32,HWREV=,FW=v9.9.9_TEST,CAP=0000,CTRL=0,CAPTAGS=,MAESTRO=-,AGE=0,"
            "SEEN=1,PEER=3]",
            "[WDP:N=1,CLIENT=0,ALIAS=Body,HW=24,HWREV=,FW=6.2.1_TEST,CAP=0001,CTRL=20,CAPTAGS=maestro,MAESTRO=1,AGE=12,"
            "SEEN=1,PEER=1]", "[WDPIF:N=1,S=1,DEV=Maestro 1]", "[WDPCFG:EN=1,AUTOJOIN=1,PEERS=2]", "[WDP:END,count=1]"]
    v = NC.parse_wdp(dump)
    assert [r["N"] for r in v["rows"]] == ["20", "1"] and v["rows"][1]["MAESTRO"] == "1" and v["count"] == 1, v
    assert v["ifaces"] == [{"N": "1", "S": "1", "DEV": "Maestro 1"}], v["ifaces"]
    assert v["cfg"] == {"EN": "1", "AUTOJOIN": "1", "PEERS": "2"}, v["cfg"]
    status = ('{"type":"WCB_STATUS","quantity":1,"self":20,"online":[1,1],"known":[1,1],"clients":[0,0],'
              '"temporary":[0,0],"aliases":["Body",""],"portLabels":[["","","","",""],["","","","",""]],'
              '"seqHash":[1,2]}')
    replies = {'{"type":"PING"}': ['{"type":"PONG","version":"v9.9.9_TEST"}'], "?WDP,DUMP": dump,
               '{"type":"GET_WCB_STATUS"}': [status], "?version": ["Software Version: v9.9.9_TEST", "End of Version"],
               "?OTALOCAL,STATUS": ["---------- OTA Status ----------", "Chip:        ESP32-S3 (family 1)",
                                    "Firmware:    v9.9.9_TEST", "Session:     idle"], "#L12": [MODE_LINE]}
    d = FakeNaviDev(lambda text, n: replies.get(text, []))
    nc = NC.NaviCore(d)
    assert [r["N"] for r in nc.wdp_dump()] == ["20", "1"] and nc.self_row()["FW"] == "v9.9.9_TEST"
    assert nc.wdpcfg() == {"EN": "1", "AUTOJOIN": "1", "PEERS": "2"}
    w1_replies = {';W20,{"type":"PING"}': ['{"sys":1,"type":"PONG","id":20,"version":"v9.9.9_TEST","model":0,"mode":1}'],
                  "?WDP,DUMP": ["[WDP:N=20,CLIENT=1,ALIAS=NaviCore,HW=0,HWREV=NaviCore v2,FW=v9.9.9_TEST,CAP=0000,"
                                "CTRL=0,CAPTAGS=rc sbus,MAESTRO=1,AGE=4,SEEN=1,PEER=0]", "[WDP:END,count=1]"]}
    w1dev = FakeNaviDev(lambda text, n: [text[4:]] if text.startswith(";S0,") else w1_replies.get(text, []), "wcb1")
    got = nc.version_surfaces(W.WCB(w1dev), timeout=0.3)
    want = {k: "v9.9.9_TEST" for k in ("pong", "cli", "ota", "self_row", "mesh_pong", "w1_row")}
    assert got == dict(want, rc_hb=None), got                   # no rc_hb in the window: None, not an error


def t_nc_config_protocol(tmp):
    """The config and JSON methods against a scripted NaviCore whose config holds a synthetic mesh password: config()
    and config(raw=True), the exact text, a cut line failing without quoting it; set_config() paced, its data exact
    (a dict or verbatim text), a stale ACK of another saveId skipped, ok:false raised unless check=False, a multi-line
    value refused; reset_defaults(); ack() by 'of' past an unrelated ACK, ack_line() byte-exact; trigger() with its
    rc_trig and without; rc_events(); backup() with ?EPASS hashed, and a cut backup failing without quoting it;
    seq()/seqval(), their ok:false raised; cli(flush=False) needing `until`. No message quotes a secret."""
    from hil import checkpoint as C
    from hil import navicore as NC
    cfg_text = '{"boardType":0,"wcbNetwork":{"deviceId":20,"password":"hunter2"},"wifiPassword":"sekrit99"}'
    st = {"cut": False, "ok": True, "backup_end": True}
    backup = ["", "*** ========================================", "*** WCB Configuration Backup", "", "?HW,32",
              "?MAC,2,00", "?MAC,3,14", "?WCB,20", "?ALIAS,NaviCore", "?WCBQ,1", "?EPASS,hunter2", "?CMDCHAR,;"]

    def script(text, n):
        if text == '{"type":"GET_CONFIG"}':
            line = '{"type":"CONFIG","data":' + cfg_text + "}"
            return [line[:-12] if st["cut"] else line]
        if text.startswith('{"type":"SET_CONFIG"'):
            obj = json.loads(text)
            st["set"], st["set_text"] = obj, text
            ack = {"type": "ACK", "of": "SET_CONFIG", "ok": st["ok"]}
            if not st["ok"]:
                ack["msg"] = "config apply failed"
            ack["saveId"] = obj["saveId"]
            return ['{"type":"ACK","of":"SET_CONFIG","ok":true,"saveId":1}',          # a late ACK of an earlier save
                    json.dumps(ack, separators=(",", ":"))]
        if text == '{"type":"RESET_DEFAULTS"}':
            return ['{"type":"ACK","ok":true}']
        if text.startswith('{"type":"TEST_ACTION"'):
            return ['{"type":"ACK","ok":true}', '{"type":"ACK","of":"TEST_ACTION","ok":false}']
        if text == '{"type":"TRIGGER","mode":1,"btn":36,"tap":1}':
            return ["[TRIGGER] mode=1 btn=36 tap=1", '{"sys":1,"type":"rc_trig","id":20,"mode":1,"btn":36,"tap":1}',
                    '{"type":"ACK","ok":true}']
        if text == '{"type":"TRIGGER","mode":1,"btn":37,"tap":1}':
            return ['{"type":"ACK","ok":false,"msg":"bad mode/btn/tap"}']
        if text == '{"type":"WCB_SEND","target":0,"cmd":";S3HILq"}':
            return ['{"sys":1,"type":"rc_mode","id":20,"mode":2}', '{"type":"ACK","ok":true}']
        if text == "?backup":
            return backup + (["--------- End of Backup ---------", ""] if st["backup_end"] else [])
        if text == '{"type":"GET_WCB_SEQ","wcb":2}':
            return ['{"sys":1,"type":"WCB_SEQ","ok":true,"wcb":2,"hash":123,"names":["HILA","HILB"]}']
        if text == '{"type":"GET_WCB_SEQ","wcb":25}':
            return ['{"sys":1,"type":"WCB_SEQ","ok":false,"wcb":25,"msg":"wcb out of range"}']
        if text == '{"type":"GET_WCB_SEQVAL","wcb":2,"key":"HILA"}':
            return ['{"sys":1,"type":"WCB_SEQVAL","ok":true,"wcb":2,"key":"HILA","status":0,"value":";S1a^;S2b"}']
        return [MODE_LINE] if text == "#L12" else []
    d = FakeNaviDev(script)
    nc = NC.NaviCore(d)
    msgs = []
    assert nc.config()["wcbNetwork"]["deviceId"] == 20 and nc.config(raw=True) == cfg_text
    st["cut"] = True
    msgs.append(str(_raises(lambda: nc.config(raw=True))))
    st["cut"] = False
    ack = nc.set_config({"boardType": 0, "hil": [1, 2]}, save_id=77)
    assert ack == {"type": "ACK", "of": "SET_CONFIG", "ok": True, "saveId": 77} and len(d.paced) == 1, (ack, d.paced)
    assert st["set"]["data"] == {"boardType": 0, "hil": [1, 2]}
    nc.set_config(cfg_text)
    assert st["set_text"].endswith('"data":' + cfg_text + "}") and 0 < st["set"]["saveId"] < 2 ** 31, st["set_text"]
    st["ok"] = False
    msgs.append(str(_raises(lambda: nc.set_config(cfg_text))))
    assert "config apply failed" in msgs[-1] and nc.set_config(cfg_text, check=False)["ok"] is False
    _raises(lambda: nc.set_config('{"a":\n1}'), ValueError)
    assert nc.reset_defaults() == {"type": "ACK", "ok": True}
    action = {"type": "wcb_unicast", "target": "25", "cmd": ";S2HILz"}
    assert nc.test_action(action) == {"type": "ACK", "of": "TEST_ACTION", "ok": False}
    assert nc.ack_line({"type": "TEST_ACTION", "action": action}) == '{"type":"ACK","ok":true}'
    ack, trig = nc.trigger(1, 36, 1)
    assert ack == {"type": "ACK", "ok": True} and trig == {"sys": 1, "type": "rc_trig", "id": 20, "mode": 1, "btn": 36,
                                                           "tap": 1}, (ack, trig)
    assert nc.trigger(1, 37, 1) == ({"type": "ACK", "ok": False, "msg": "bad mode/btn/tap"}, None)
    m = d.mark()
    assert nc.wcb_send(0, ";S3HILq")["ok"] and [o["mode"] for _, o in nc.rc_events(m, "rc_mode")] == [2]
    tokens = nc.backup()
    assert tokens == [C.redact_token(t) for t in backup if t.startswith("?")] and "?CMDCHAR,;" in tokens, tokens
    assert not any("hunter2" in t for t in tokens) and any(t.startswith("?EPASS,<redacted:") for t in tokens), tokens
    st["backup_end"] = False
    msgs.append(str(_raises(lambda: nc.backup(timeout=0.2))))
    assert "End of Backup" in msgs[-1], msgs[-1]
    assert nc.seq(2) == {"hash": 123, "names": ["HILA", "HILB"]}
    assert "wcb out of range" in str(_raises(lambda: nc.seq(25)))
    assert nc.seqval(2, "HILA") == {"key": "HILA", "status": 0, "value": ";S1a^;S2b"}
    _raises(lambda: nc.cli("#L11", flush=False), ValueError)
    leaked = [x for x in msgs if "hunter2" in x or "sekrit99" in x]
    assert not leaked, f"{len(leaked)} messages quote a secret"


def t_sbus_codec(tmp):
    """hil/sbus.py's SBUS codec. A known frame: the 36 bytes NaviCore dumped with #L13 in run 20260922-120804 (78.954 s;
    sensor values only), read through parse_sbus_raw, decode to exactly the 24 channels its #L09 printed around it
    (parse_sbus_dump), and encode() rebuilds them byte for byte - so the controller's packer (SBUSController.ino
    buildSbusFrame) and NaviCore's unpacker (sbus_reader.h decodeFrame), both transliterated here, agree with the wire.
    Then round trips for SBUS-16 and SBUS-24 with every flag bit and the 11-bit extremes, each data block checked
    against the same packing said another way (one little-endian integer), and the refusals."""
    import random
    from hil import navicore as NC
    from hil import sbus as SB
    raw_dump = ["---- SBUS RAW ---- (36 bytes, SBUS-24)", "  [ 0] 0F E0 03 1F F8 C0 07 3E ",
                "  [ 8] F0 81 AF 15 AD 68 45 2B ", "  [16] 5A D1 0A F0 B5 A2 15 AD ", "  [24] 00 1F F8 C0 07 3E F0 81 ",
                "  [32] 0F 7C 00 00 ", "  byte 0       = header (expect 0F)", "  bytes 1-22   = CH1-16 data",
                "  bytes 23-33  = CH17-24 data  ← check these", "  byte 34      = flags", "  byte 35      = footer (expect 00)"]
    rows = [[992] * 7 + [173], [173] * 5 + [992, 173, 173], [173] + [992] * 7]
    state = ["---- SBUS STATE ----", "  variant=SBUS-24 (24 ch, 36-byte frame)",
             "  frames=453100  fps=106  ageMs=1  lost=no  failsafe=no"]
    state += ["  CH%d-%d: " % (8 * r + 1, 8 * r + 8) + "".join("%4d " % v for v in vals) for r, vals in enumerate(rows)]
    dump = NC.parse_sbus_dump(state + [MODE_LINE])
    assert (dump["fps"], dump["variant"], dump["frames"], dump["age"], dump["lost"], dump["failsafe"]) == \
        (106, "SBUS-24", "453100", 1, "no", "no"), dump
    raw = NC.parse_sbus_raw(["#L13 below"] + raw_dump)
    assert SB.decode(raw) == {"n": 24, "channels": dump["channels"], "flags": 0, "lost": False, "failsafe": False}
    assert SB.encode(dump["channels"]) == raw and len(dump["channels"]) == 24
    assert NC.parse_sbus_raw(["---- SBUS RAW ---- (no frame parsed yet)"]) == b""
    _raises(lambda: NC.parse_sbus_raw(raw_dump[:4]))                      # rows short of the 36 bytes
    rnd = random.Random(20260927)
    for n in (16, 24):
        for flags in (0, SB.FLAG_CH17, SB.FLAG_CH18, SB.FLAG_LOST, SB.FLAG_FAILSAFE, 0x0F):
            ch = [rnd.randrange(0, 2048) for _ in range(n)]
            ch[0], ch[-1] = 0x7FF, 0
            frame = SB.encode(ch, flags, n)
            assert len(frame) == SB.FRAME_LEN[n] and (frame[0], frame[-2], frame[-1]) == (0x0F, flags, 0x00)
            assert frame[1:-2] == sum(c << (11 * i) for i, c in enumerate(ch)).to_bytes(n * 11 // 8, "little")
            assert SB.decode(frame) == {"n": n, "channels": ch, "flags": flags, "lost": bool(flags & 0x04),
                                        "failsafe": bool(flags & 0x08)}
    for bad in (raw[:-1], b"\x0e" + raw[1:], raw[:-1] + b"\x01", b"\x0f" * 30):
        _raises(lambda: SB.decode(bad))
    for args in (([0] * 15, 0, 16), ([2048] + [0] * 23, 0, 24), ([0] * 24, 256, 24), ([0] * 16, 0, 12)):
        _raises(lambda: SB.encode(*args), ValueError)


def t_sbus_ctl(tmp):
    """hil/sbus.py SbusCtl against a scripted controller: every verb sends exactly the JSON line the suites sent before the
    move (s11, s21, hil/resume.py); ping() returns fwver; cfg() pings first unless told not to, hashes the WiFi
    credentials in the device log while it reads and puts the log back; bootlog(); center_all() releases the sticks,
    every button and the button-mode trims only; reset_rts() pulses RTS with a DTR write after each change. And
    safe_channels, band and matrix_button against a NaviCore config: bound channels are unsafe, a 0/0 band is inert,
    the first unmapped slot is chosen, and a resting value that decodes skips."""
    import types
    from hil import sbus as SB
    j = lambda o: json.dumps(o, separators=(",", ":"))      # noqa: E731 - how the suites built each line before
    cfg = {"e": "cfg", "sbus24": True, "lx": 3, "btn": [{"c": 7, "v": 350}, {"c": 7, "v": 550}],
           "tr": [{"c": 7, "m": 1, "vR": 350, "vL": 550}, {"c": 9, "m": 0, "s": 10}],
           "wifiNets": [{"s": "DomeNet", "p": "sekrit99"}]}

    def script(text, n):
        if text == '{"t":"ping"}':
            return ['{"t":"pong","ver":3,"fwver":"sbus-9.9"}']
        if text == '{"t":"getcfg"}':
            return [j(cfg)]
        return ['{"e":"bootlog","rst":3,"rstn":"SW","rtc0":3,"rtcn":"SW system","n":4,"prev":0,"up":1234,"log":[]}'] \
            if text == '{"t":"bootlog"}' else []
    logged = []
    d = FakeNaviDev(script, "sbus")
    d.log = lambda name, direction, text: logged.append(text)
    own_log = d.log
    ctl = SB.SbusCtl(d)
    assert ctl.ping() == "sbus-9.9" and ctl.ping_once() == "sbus-9.9"
    got = ctl.cfg()
    assert got["wifiNets"][0]["p"] == "sekrit99" and d.sent[-2:] == ['{"t":"ping"}', '{"t":"getcfg"}'], d.sent
    assert not any("sekrit99" in x for x in logged) and any('"e":"cfg"' in x for x in logged) and d.log is own_log
    ctl.cfg(ping=False)
    assert d.sent[-3:] == ['{"t":"ping"}', '{"t":"getcfg"}', '{"t":"getcfg"}'], d.sent
    assert ctl.bootlog()["n"] == 4
    d.sent.clear()
    ctl.axes(1, 0, 0, 0)
    ctl.axes(0, 0, 0, 0)
    ctl.switch(2, 1)
    ctl.slider(0, 75)
    ctl.trim(3, 1)
    ctl.trim(3, -1, True)
    ctl.trim(3, 1, False)
    ctl.button(4, True)
    ctl.button(4, False)
    ctl.lua(1, True)
    assert d.sent == [j({"t": "a", "lx": 1, "ly": 0, "rx": 0, "ry": 0}), j({"t": "a", "lx": 0, "ly": 0, "rx": 0, "ry": 0}),
                      j({"t": "sw", "i": 2, "p": 1}), j({"t": "sl", "i": 0, "v": 75}), j({"t": "tr", "i": 3, "d": 1}),
                      j({"t": "tr", "i": 3, "d": -1, "p": True}), j({"t": "tr", "i": 3, "d": 1, "p": False}),
                      j({"t": "btn", "i": 4, "p": True}), j({"t": "btn", "i": 4, "p": False}),
                      j({"t": "lua", "i": 1, "p": True})], d.sent
    assert d.sent[5] == '{"t":"tr","i":3,"d":-1,"p":true}'
    d.sent.clear()
    ctl.center_all(cfg)
    assert d.sent == ['{"t":"a","lx":0,"ly":0,"rx":0,"ry":0}', '{"t":"btn","i":0,"p":false}',
                      '{"t":"btn","i":1,"p":false}', '{"t":"tr","i":0,"d":1,"p":false}'], d.sent
    d._ser = RecSer()
    ctl.reset_rts(hold_s=0.01)
    assert d._ser.control == [("rts", True), ("dtr", False), ("rts", False), ("dtr", False)], d._ser.control
    ncfg = {"matrixChannel": 7, "switches": {"0": {"channel": 12}}, "knobs": [{"channel": 24}, {"c": 4}],
            "thresholds": [{"minPwm": 0, "maxPwm": 0}, {"minPwm": 300, "maxPwm": 400}, [500, 600]],
            "mappings": {"102": {"t1": []}}}
    assert SB.safe_channels(ncfg) == set(range(1, 25)) - {4, 7, 12, 24}
    assert (SB.band(ncfg, 350), SB.band(ncfg, 550), SB.band(ncfg, 0), SB.band(ncfg, 992)) == (2, 3, None, None)
    nc = types.SimpleNamespace(sbus_dump=lambda: {"channels": [992] * 24})
    assert SB.matrix_button(nc, cfg, ncfg, 1) == (1, {"c": 7, "v": 550}, 3)
    assert SB.matrix_button(nc, cfg, ncfg, 2) == (0, {"c": 7, "v": 350}, 2)
    rest = types.SimpleNamespace(sbus_dump=lambda: {"channels": [350] * 24})
    _raises(lambda: SB.matrix_button(rest, cfg, ncfg, 1), runner.Skip)


# ---------------------------------------------------------------------------- nc_guard (NAVICORE.md INF3)
class FakeNaviBoard:
    """A NaviCore for hil/nc_guard.py, answering in the firmware's own line formats. GET_CONFIG prints in
    rcConfigToJSON's key order and leaves out what is empty (an empty mapping, a slot's empty channels, empty
    serialLabels: rc_config.h:1262, :1340-1341, :1419-1426). SET_CONFIG merges as rcConfigFromJSON does: only the
    mapping keys it names, a slot's channels only when "channels" is present, serialLabels whole when present
    (:1580-1603, :1705-1721, :1847-1864). RESET_DEFAULTS is RAM only and reloads the compile-time mesh password
    (:801-972, NaviCore.ino:3989-3992). Also the command library by size and FNV-1a hash, ?REC,LS/RM, ?WDP,DUMP with
    its PEER flags and WDPCFG count, #L12, and the RAM toggles. w1_script is W1's console: ?WDP,POLL makes every WCB
    NaviCore hears advertise once (it learns a peer on the second advert, WCB_Client.cpp:2166-2168), and a relayed
    SET_MODE sets the mode. Quirks model a firmware the ladder must survive: 'ignore_clears' skips an empty mapping
    object, 'stuck_password' keeps the compile-time mesh password whatever arrives."""
    FACTORY_PW = "DomeNet"                  # the fake's compile-time mesh password (a fake secret, like the others)
    LABEL_KEYS = ("S3", "S4", "S5", "maestro")

    def __init__(self):
        self.c = self.factory()
        self.c.update(wifiEnabled=True, wifiPassword="sekrit99")
        self.c["wcbNetwork"].update(password="hunter2", quantity=1)
        self.c["wcbProfiles"] = [{"name": "Dev", "macOct2": 0, "macOct3": 20, "password": "hunter2", "quantity": 1,
                                  "deviceId": 20, "channel": 1}]
        self.c["mappings"] = {"101": self._mapping({"t1": [{"type": "wcb_unicast", "target": "1", "cmd": "?HILA"}],
                                                    "t1note": "Dome"}),
                              "207": self._mapping({"exclusive": True, "t2": [{"type": "maestro", "target": "1",
                                                                               "cmd": ";M11"}]})}
        self.c["maestros"][0].update(type=1, channels={0: {"name": "Pie 1", "min": 3968, "max": 8000},
                                                       2: {"name": "Pie 2", "min": 4000, "max": 7600}})
        for s in self.c["maestros"][1:]:
            s["type"] = 2
        self.c["auxBaud"]["S3"] = 115200
        self.cmdlib = '{"boards":[{"id":"HILboard"}],"enums":{}}'
        self.clips = ["intro", "HILkeep"]
        self.mode, self.quantity = 1, 1
        self.heard, self.learned, self.adverts = {1, 2}, {2}, {1: 9, 2: 9}
        self.debug, self.monitor, self.calib = 0, False, False
        self.quirks, self.received, self.w1_received = set(), [], []

    @classmethod
    def factory(cls):
        bands = [{"id": i + 1, "label": f"B{i + 1}", "minPwm": 0 if i >= 20 else 1799 - 41 * i,
                  "maxPwm": 0 if i >= 20 else 1823 - 41 * i} for i in range(36)]
        return {"txModel": 0, "wifiEnabled": False, "wifiSsid": "", "wifiPassword": "", "boardType": 0,
                "tapWindowMs": 500, "holdMs": 750, "matrixChannel": 7, "thresholds": bands, "mappings": {},
                "maestros": [{"type": 0, "device": i + 1, "channels": {}} for i in range(8)],
                "wcbNetwork": {"macOct2": 0, "macOct3": 20, "password": cls.FACTORY_PW, "quantity": 4, "deviceId": 20,
                               "channel": 1},
                "wcbProfiles": [], "auxBaud": {"S3": 9600, "S4": 9600, "S5": 9600, "maestro": 57600},
                "serialLabels": {k: "" for k in cls.LABEL_KEYS}}

    @staticmethod
    def _mapping(m):
        out = {"exclusive": bool(m.get("exclusive", False))}           # memset, then what the object names
        for t in range(1, 5):
            if isinstance(m.get(f"t{t}"), list) and m[f"t{t}"]:
                out[f"t{t}"] = m[f"t{t}"][:5]
            if m.get(f"t{t}note"):
                out[f"t{t}note"] = m[f"t{t}note"][:19]
        return out

    def text(self):
        """GET_CONFIG's data, as rcConfigToJSON prints it."""
        c, maps, maes = self.c, {}, []
        for k in sorted(c["mappings"], key=int):
            m = c["mappings"][k]
            if m["exclusive"] or len(m) > 1:
                maps[k] = m
        for s in c["maestros"]:
            o = {"type": s["type"], "device": s["device"]}
            chs = [dict(ch=n, **v) for n, v in sorted(s["channels"].items()) if v["name"] or v["min"] or v["max"]]
            if chs:
                o["channels"] = chs
            maes.append(o)
        d = {k: c[k] for k in ("txModel", "wifiEnabled", "wifiSsid", "wifiPassword", "boardType", "tapWindowMs",
                               "holdMs", "matrixChannel", "thresholds")}
        d.update(mappings=maps, maestros=maes, wcbNetwork=dict(c["wcbNetwork"]),
                 wcbProfiles=[dict(p) for p in c["wcbProfiles"]], auxBaud=dict(c["auxBaud"]))
        labels = {k: v for k, v in c["serialLabels"].items() if v}
        if labels:
            d["serialLabels"] = labels
        return json.dumps(d, separators=(",", ":"), ensure_ascii=False)

    def merge(self, d):
        """SET_CONFIG's data, as rcConfigFromJSON applies it."""
        c = self.c
        for k in ("txModel", "wifiEnabled", "wifiSsid", "wifiPassword", "boardType", "tapWindowMs", "holdMs",
                  "matrixChannel"):
            if k in d:
                c[k] = d[k]
        for i, t in enumerate((d.get("thresholds") or [])[:36]):
            c["thresholds"][i] = {"id": t.get("id", i + 1), "label": t.get("label", ""), "minPwm": t.get("minPwm", 0),
                                  "maxPwm": t.get("maxPwm", 0)}
        for k, m in (d.get("mappings") or {}).items():
            mode, btn = int(k) // 100, int(k) % 100
            if not (1 <= mode <= 3 and 1 <= btn <= 36) or (m == {} and "ignore_clears" in self.quirks):
                continue
            c["mappings"][k] = self._mapping(m)
        for i, s in enumerate((d.get("maestros") or [])[:8]):
            slot = c["maestros"][i]
            slot["type"], slot["device"] = s.get("type", 0), s.get("device", i + 1)
            if "channels" in s:
                slot["channels"] = {x["ch"]: {"name": x.get("name", ""), "min": x.get("min", 0), "max": x.get("max", 0)}
                                    for x in s["channels"] if 0 <= x.get("ch", -1) < 32}
        for k, v in (d.get("wcbNetwork") or {}).items():
            if k in c["wcbNetwork"]:
                c["wcbNetwork"][k] = v
        if "wcbProfiles" in d:
            c["wcbProfiles"] = [dict(p) for p in d["wcbProfiles"]]
        c["auxBaud"].update({k: v for k, v in (d.get("auxBaud") or {}).items() if k in c["auxBaud"]})
        if "serialLabels" in d:
            c["serialLabels"] = {k: "" for k in self.LABEL_KEYS}
            c["serialLabels"].update({k: v[:24] for k, v in d["serialLabels"].items() if k in self.LABEL_KEYS})
        if "stuck_password" in self.quirks:
            c["wcbNetwork"]["password"] = self.FACTORY_PW

    def forget(self, n):
        """FORGET_PEER: the peer must be heard twice again before it re-joins (WCB_Client.cpp forgetPeer)."""
        self.learned.discard(n)
        self.adverts[n] = 0

    def script(self, text, n):
        from hil.navicore import fnv1a32
        self.received.append(text)
        ack = '{"type":"ACK","ok":true}'
        if text == '{"type":"GET_CONFIG"}':
            return ['{"type":"CONFIG","data":' + self.text() + "}"]
        if text.startswith('{"type":"SET_CONFIG"'):
            obj = json.loads(text)
            self.merge(obj["data"])
            return [f'{{"type":"ACK","of":"SET_CONFIG","ok":true,"saveId":{obj["saveId"]}}}']
        if text == '{"type":"RESET_DEFAULTS"}':
            self.c = self.factory()
            return [ack]
        if text == '{"type":"GET_CMDLIB_META"}':
            lib = self.cmdlib or ""
            return [f'{{"type":"CMDLIB_META","size":{len(lib.encode())},"hash":{fnv1a32(lib) if lib else 0}}}']
        if text == '{"type":"GET_CMDLIB"}':
            lib = self.cmdlib or '{"boards":[],"enums":{}}'
            return [f'{{"type":"CMDLIB","size":{len(lib.encode())},"hash":{fnv1a32(lib)},"data":{lib}}}']
        if text.startswith('{"type":"SET_CMDLIB","data":'):
            self.cmdlib = text[len('{"type":"SET_CMDLIB","data":'):-1].strip()
            return [f'{{"type":"ACK","of":"SET_CMDLIB","ok":true,"size":{len(self.cmdlib.encode())},'
                    f'"hash":{fnv1a32(self.cmdlib)}}}']
        if text == "?REC,LS":
            return (['[CLIPFS]{"total":12000000,"used":4096}', "[REC] clips:", "[CLIPLIST:BEGIN]"]
                    + [f'[CLIPITEM]{{"name":"{c}","bytes":296,"dur":1000,"n":2}}' for c in self.clips]
                    + ["[CLIPLIST:END]"])
        if text.startswith("?REC,RM,"):
            name = text[len("?REC,RM,"):]
            if name in self.clips:
                self.clips.remove(name)
                return ["[REC] deleted"]
            return ["[REC] delete failed"]
        if text == "#L12":
            return [f"Mode={self.mode}  matrixBtn=0  matrixVal=992"]
        if text == "?WDP,DUMP":
            rows = ["[WDP:N=20,CLIENT=0,ALIAS=NaviCore,HW=0,HWREV=,FW=v0.2.0_TEST,CAP=0000,CTRL=0,CAPTAGS=,MAESTRO=-,"
                    "AGE=0,SEEN=1,PEER=3]"]
            for w in sorted(self.heard):
                flag = 2 if w in self.learned else (1 if w <= self.quantity else 0)
                rows.append(f"[WDP:N={w},CLIENT=0,ALIAS=W{w},HW=24,HWREV=,FW=6.2.1_TEST,CAP=0001,CTRL=20,CAPTAGS=,"
                            f"MAESTRO=-,AGE=4,SEEN=1,PEER={flag}]")
            peers = self.quantity + len([w for w in self.learned if w > self.quantity])
            return rows + [f"[WDPCFG:EN=1,AUTOJOIN=1,PEERS={peers}]", f"[WDP:END,count={len(self.heard)}]"]
        if text.startswith('{"type":"SET_DEBUG_FLAGS"'):
            self.debug = json.loads(text)["flags"]
            return [ack]
        if text == '{"type":"STOP_MONITOR"}':
            self.monitor = self.calib = False
            return [ack]
        if text == '{"type":"PING"}':
            self.calib = False
            return ['{"type":"PONG","version":"v0.2.0_TEST"}']
        return []

    def w1_script(self, text, n):
        import re as _re
        self.w1_received.append(text)
        if text.startswith(";S0,"):
            return [text[4:]]
        if text == "?WDP,POLL":
            for w in sorted(self.heard):
                self.adverts[w] = self.adverts.get(w, 0) + 1
                if self.adverts[w] >= 2 and w > self.quantity:
                    self.learned.add(w)
            return ["[WDP] polled: advertised + solicited the mesh"]
        m = _re.match(r';W20,\{"type":"SET_MODE","mode":(\d)\}$', text)
        if m:
            self.mode = int(m.group(1))
        return []


def _nc_bench(tmp, board):
    """A Bench whose navicore and wcb1 are `board`'s scripted consoles, logging through Bench.log, so its filter runs
    on every line as it would on the bench."""
    b = tmp.bench({"wcb1": {"port": "COMW1", "kind": "wcb", "wcb": 1},
                   "navicore": {"port": "COMNAV", "kind": "navicore"}, "sbus": {"port": "COMS", "kind": "sbus"}})
    nav, w1 = FakeNaviDev(board.script, "navicore"), FakeNaviDev(board.w1_script, "wcb1")
    nav.log = w1.log = b.log
    b.dev = lambda name: {"navicore": nav, "wcb1": w1}[name]
    return b


def _fast_guard():
    """nc_guard's waits for the mesh cut to nothing: the fake answers at once. -> the saved values, for _slow_guard."""
    from hil import nc_guard as G
    saved = (G.POLL_GAP_S, G.RELEARN_WAIT_S, G.MODE_WAIT_S)
    G.POLL_GAP_S, G.RELEARN_WAIT_S, G.MODE_WAIT_S = 0.0, 0.3, 0.3
    return saved


def _slow_guard(saved):
    from hil import nc_guard as G
    G.POLL_GAP_S, G.RELEARN_WAIT_S, G.MODE_WAIT_S = saved


def _trap_change(orig_text, label="HILlabel"):
    """The three changes a re-sent snapshot cannot undo: mapping 136, channels on slot 3 (which had none), a label."""
    maes = json.loads(orig_text)["maestros"]
    maes[2]["channels"] = [{"ch": 0, "name": "HILch", "min": 4000, "max": 8000}]
    return {"mappings": {"136": {"t1": [{"type": "wcb_unicast", "target": "1", "cmd": "?HILNOOP"}], "t1note": "HILn"}},
            "maestros": maes, "serialLabels": {"S5": label}}


def t_nc_guard_ladder(tmp):
    """nc_guard's restore ladder against FakeNaviBoard: nothing written when nothing changed; the trap (the snapshot
    re-sent alone leaves a new mapping, a slot's new channels and a new label) undone by the snapshot plus exactly the
    three clears, with no RESET_DEFAULTS; RESET_DEFAULTS then the snapshot's exact text only after the clears failed
    (a firmware that skips an empty mapping); a config that will not come back failing the test with the body's own
    failure first and the credential that differs only as a hash; guards that do not nest; an abort that skips the
    restore."""
    from hil import nc_guard as G
    board = FakeNaviBoard()
    orig = board.text()
    b = _nc_bench(tmp, board)
    b.new_session()
    with G.nc_guard(b) as g:
        assert g.before_text == orig and g.before["wcbNetwork"]["deviceId"] == 20 and g.snap["learned"] == [2]
    assert g.path == "unchanged" and not [x for x in board.received if x.startswith('{"type":"SET_CONFIG"')], g.path
    change = _trap_change(orig)
    with G.nc_guard(b) as g:
        g.nc.set_config(change)
        g.nc.set_config(g.before_text)                     # the snapshot alone: the trap
        left = G.config_diff(orig, board.text())
        assert left == ["mappings.136 added", "maestros[2].channels added", "serialLabels added"], left
        board.received.clear()
    assert g.path == "diff" and board.text() == orig, (g.path, G.config_diff(orig, board.text()))
    assert '{"type":"RESET_DEFAULTS"}' not in board.received, "the diff path never sends RESET_DEFAULTS"
    sets = [json.loads(x)["data"] for x in board.received if x.startswith('{"type":"SET_CONFIG"')]
    assert len(sets) == 1 and sets[0]["mappings"]["136"] == {} and sets[0]["serialLabels"] == {}, sets
    want = [(True, json.loads(orig)["maestros"][0]["channels"])] + [(True, [])] * 7
    assert [("channels" in s, s.get("channels")) for s in sets[0]["maestros"]] == want, \
        "channels: [] for every slot with none"
    assert sets[0]["wcbNetwork"]["password"] == "hunter2", "the password goes back unchanged"
    target = json.loads(orig)
    assert G.with_clears(target, target)["serialLabels"] == {} and "serialLabels" not in target and \
        "channels" not in target["maestros"][2], "with_clears leaves its target alone"
    board.quirks.add("ignore_clears")                      # a firmware whose empty mapping object does nothing
    with G.nc_guard(b) as g:
        g.nc.set_config(change)
        board.received.clear()
    assert g.path == "reset" and board.text() == orig, g.path
    reset = board.received.index('{"type":"RESET_DEFAULTS"}')
    sets = [i for i, x in enumerate(board.received) if x.startswith('{"type":"SET_CONFIG"')]
    assert len(sets) == 2 and sets[0] < reset < sets[1], "RESET_DEFAULTS only after the snapshot plus clears failed"
    assert board.received[sets[1]].endswith('"data":' + orig + "}"), "then the snapshot's exact text"
    board.quirks.discard("ignore_clears")

    def stuck():
        with G.nc_guard(b) as g2:
            g2.nc.set_config(change)
            board.quirks.add("stuck_password")
            raise ValueError("the body failed too")
    msg = str(_raises(stuck))
    assert msg.startswith("the body failed too\nNAVICORE CONFIG NOT RESTORED — wcbNetwork.password: <redacted:"), msg
    assert not any(s in msg for s in SECRETS) and f"{G.SNAPSHOT_FILE} in this run's folder" in msg, msg
    assert G.load_snapshot(b.out_dir)["state"] == "not_restored"
    log = read(os.path.join(b.out_dir, "session.log"))
    assert "NAVICORE LEAK NAVICORE CONFIG NOT RESTORED" in log and not any(s in log for s in SECRETS), \
        "session.log: the guard's own notes and NaviCore's lines, all without a credential"
    board.quirks.discard("stuck_password")
    board.c = FakeNaviBoard().c

    def nested():
        with G.nc_guard(b):
            with G.nc_guard(b):
                pass
    _raises(nested, RuntimeError)
    board.received.clear()
    try:
        with G.nc_guard(b) as g:
            g.nc.set_config(change)
            raise KeyboardInterrupt                         # a second Ctrl+C: out at once, no restore
    except KeyboardInterrupt:
        pass
    assert board.text() != orig and G.load_snapshot(b.out_dir)["state"] == "guarding", "left for the resume"
    with G.nc_guard(b) as g:                                # no checkpoint here: the file alone says it is pending
        pass
    assert g.path == "unchanged" and g.before_text == orig and board.text() == orig, "restored first, then snapshotted"
    assert "restoring its snapshot before this test's own" in read(os.path.join(b.out_dir, "session.log"))
    b.close()


def t_nc_guard_state(tmp):
    """nc_guard's restore of what is not config: the command library put back by its bytes; new HIL* clips removed and
    other clips kept; debug flags, the monitor and CALIB cleared; the mode set back by a mesh SET_MODE from W1; a lost
    learned peer re-learned by two ?WDP,POLL from W1; and reported, failing the test as NAVICORE STATE NOT RESTORED: a
    library written where none was stored, a peer learned during the test, a lost peer that is not on the air."""
    from hil import nc_guard as G
    saved = _fast_guard()
    try:
        board = FakeNaviBoard()
        orig, lib = board.text(), board.cmdlib
        b = _nc_bench(tmp, board)
        b.new_session()
        with G.nc_guard(b) as g:
            g.nc.set_cmdlib('{"boards":[],"enums":{"HIL":1}}')
            board.clips += ["HILtemp", "userclip"]
            board.mode, board.debug, board.monitor, board.calib = 3, 0x7F, True, True
            board.forget(2)
        assert g.path == "unchanged" and not g.problems, g.problems
        assert board.cmdlib == lib and board.clips == ["intro", "HILkeep", "userclip"], (board.cmdlib, board.clips)
        assert (board.mode, board.debug, board.monitor, board.calib) == (1, 0, False, False)
        assert 2 in board.learned and board.w1_received.count("?WDP,POLL") == 2, board.w1_received
        assert ';W20,{"type":"SET_MODE","mode":1}' in board.w1_received and board.text() == orig
        board.cmdlib = None

        def new_lib():
            with G.nc_guard(b) as g2:
                g2.nc.set_cmdlib('{"boards":[]}')
        msg = str(_raises(new_lib))
        assert msg.startswith("NAVICORE STATE NOT RESTORED — a command library") and "D-NC13" in msg, msg
        board.cmdlib = lib

        def learns():
            with G.nc_guard(b):
                board.heard.add(9)
                board.learned.add(9)
        assert "NaviCore learned WCB 9 during the test" in str(_raises(learns))
        board.heard.discard(9)
        board.learned.discard(9)

        def gone():
            with G.nc_guard(b):
                board.forget(2)
                board.heard.discard(2)
        msg = str(_raises(gone))
        assert "learned peer(s) [2] not re-learned after two ?WDP,POLL" in msg, msg
        assert G.load_snapshot(b.out_dir)["state"] == "restored", "the config itself came back"

        # A learned peer that is silent when the snapshot is taken (no PEER=2 row, only the count) and lost during the
        # test is polled for too, and comes back once it is on the air (run 20260928-154700: W2 between its adverts).
        board.learned.add(2)
        polls = board.w1_received.count("?WDP,POLL")
        with G.nc_guard(b) as g2:
            board.forget(2)
            board.heard.add(2)
        assert not g2.problems, g2.problems
        assert 2 in board.learned and board.w1_received.count("?WDP,POLL") == polls + 2, board.w1_received
        b.close()
    finally:
        _slow_guard(saved)


def t_nc_guard_persist_resume(tmp):
    """D-NC3: a guarded test killed mid-way (a second Ctrl+C) leaves the exact snapshot in navicore_snapshot.json
    and a 'guarding' record in the checkpoint that holds only the redacted hash; the week-start copy is written once.
    resume.check_navicore restores NaviCore from it without asking on the automatic resume, and the record reads
    'restored'; a clean NaviCore needs nothing; one changed while paused blocks the automatic resume with the key
    paths (credentials hashed) and a Yes makes it the reference; a snapshot the checkpoint did not record is never
    restored from; a newer file counts over a checkpoint record that was lost; and the next guarded test in a run
    whose record is still 'guarding' restores it first."""
    from hil import nc_guard as G
    saved = _fast_guard()
    try:
        board = FakeNaviBoard()
        orig = board.text()
        b = _nc_bench(tmp, board)
        change = _trap_change(orig)

        def killed(bench):
            with G.nc_guard(bench) as g:
                g.nc.set_config(change)
                raise KeyboardInterrupt
        runner.REGISTRY[:] = [fake("nccfg.killed", killed), fake("after")]
        ck = runner.start_run(b, runner.REGISTRY, "selftest")
        _raises(lambda: runner.continue_run(b, ck, resuming=False), KeyboardInterrupt)
        b.close()
        assert ck.state == "paused" and ck.in_flight == "nccfg.killed", (ck.state, ck.in_flight)
        ref = ck.data["navicore"]
        assert ref["state"] == "guarding" and ref["test"] == "nccfg.killed" and ref["sha"] == G.redacted_sha(orig), ref
        snap = G.load_snapshot(ck.out_dir)
        assert snap["config"] == orig and snap["state"] == "guarding" and snap["cmdlib"] == board.cmdlib
        assert (snap["clips"], snap["learned"], snap["peers"], snap["mode"]) == (["intro", "HILkeep"], [2], 2, 1), snap
        for name in ("checkpoint.json", "report.md", "session.log"):
            text = read(os.path.join(ck.out_dir, name))
            assert not any(s in text for s in SECRETS), f"{name} holds a credential"
        week = os.path.join(tmp.results, G.WEEK_START_FILE)
        assert json.loads(read(week))["config"] == orig and board.text() != orig
        b2 = _nc_bench(tmp, board)
        b2.open_session(ck.out_dir, 2)
        disk, logs = Checkpoint.load(ck.out_dir), []
        changes = resume.check_navicore(b2, disk, None, logs.append)
        assert board.text() == orig and changes == ["NaviCore restored after the cut-off nccfg.killed (diff)"], changes
        assert disk.data["navicore"]["state"] == "restored" and G.load_snapshot(ck.out_dir)["state"] == "restored"
        assert Checkpoint.load(ck.out_dir).data["navicore"]["state"] == "restored", "saved"
        assert resume.check_navicore(b2, disk, None, logs.append) == []
        assert logs[-1].endswith("matches the run's snapshot"), logs[-1]
        board.c["tapWindowMs"] = 600
        board.c["wcbNetwork"]["password"] = "sekrit99"
        e = str(_raises(lambda: resume.check_navicore(b2, disk, None, logs.append), resume.ResumeBlocked))
        assert "tapWindowMs: 500 -> 600" in e and "wcbNetwork.password: <redacted:" in e, e
        assert not any(s in e for s in SECRETS), e
        asked = []
        changes = resume.check_navicore(b2, disk, lambda title, text, dflt: asked.append((title, dflt)) or True,
                                        logs.append)
        assert asked == [("NaviCore's saved config differs", False)] and changes[0].startswith(
            "NaviCore config accepted with difference: tapWindowMs: 500 -> 600"), (asked, changes)
        assert disk.data["navicore"]["sha"] == G.redacted_sha(board.text()) != G.redacted_sha(orig)
        board.c = FakeNaviBoard().c                         # back to the bench's config for what follows
        other = dict(G.load_snapshot(ck.out_dir), config=orig.replace('"holdMs":750', '"holdMs":800'))
        other["sha"] = G.redacted_sha(other["config"])
        checkpoint.atomic_write_json(G.snapshot_path(ck.out_dir), other)
        disk.data["navicore"]["state"] = "guarding"
        e = str(_raises(lambda: resume.check_navicore(b2, disk, None, logs.append), resume.ResumeBlocked))
        assert "not the snapshot the checkpoint recorded" in e and board.text() == orig, e
        # the checkpoint's record lost while the test ran on (a GUI closed with No froze it): the newer file's
        # 'guarding' counts, and the resume restores from it
        board.merge(change)
        disk.data["navicore"]["state"] = "restored"
        checkpoint.atomic_write_json(G.snapshot_path(ck.out_dir),
                                     dict(snap, state="guarding", seq=disk.data["navicore"]["seq"] + 1))
        changes = resume.check_navicore(b2, disk, None, logs.append)
        assert board.text() == orig and changes == ["NaviCore restored after the cut-off nccfg.killed (diff)"], changes
        # an older file holding the config the checkpoint recorded is used; the next guarded test in a run whose
        # record still says 'guarding' puts NaviCore back before its own snapshot
        snap["state"] = "guarding"
        checkpoint.atomic_write_json(G.snapshot_path(ck.out_dir), snap)
        disk.data["navicore"].update(sha=snap["sha"], state="guarding")
        assert snap["seq"] < disk.data["navicore"]["seq"], "the file is the older of the two here"
        board.merge(change)
        b2.ckpt = disk
        with G.nc_guard(b2) as g:
            assert g.before_text == orig, "restored first, then snapshotted"
        assert "restoring its snapshot before this test's own" in read(os.path.join(ck.out_dir, "session.log"))
        b2.close()
    finally:
        _slow_guard(saved)


def t_nc_log_filter(tmp):
    """D-NC5: Bench.log hashes every credential on NaviCore's and the SBUS controller's lines, both directions and the
    GUI's sink alike (the JSON password fields, ?EPASS alone or in a chain, wifiNets, the Pass: banner), and leaves a
    WCB's lines as they were; redact_text stays idempotent, hashes a mesh password alike in JSON and in ?EPASS, leaves
    an empty password alone and covers the escaped form; redacted_diff names key paths and shows a credential only as
    its hash, at any depth."""
    rt, rd = checkpoint.redact_text, checkpoint.redacted_diff
    b = tmp.bench({"wcb1": {"port": "COMW1", "kind": "wcb", "wcb": 1},
                   "navicore": {"port": "COMNAV", "kind": "navicore"}, "sbus": {"port": "COMS", "kind": "sbus"}})
    sink = []
    b.log_sink = sink.append
    b.new_session()
    for name, direction, text in (
            ("navicore", "<", '{"type":"CONFIG","data":{"wifiPassword":"sekrit99","wcbNetwork":{"password":"hunter2"},'
                              '"wcbProfiles":[{"name":"Dev","password":"hunter2"}]}}'),
            ("navicore", ">", '{"type":"SET_CONFIG","saveId":5,"data":{"wcbNetwork":{"password":"hunter2"}}}'),
            ("navicore", "<", "?EPASS,hunter2"),
            ("navicore", "<", "?HW,32^?WCB,20^?EPASS,hunter2^?CMDCHAR,;"),
            ("navicore", "#", "a note quoting ?EPASS,hunter2"),
            ("sbus", "<", '{"e":"cfg","wifiNets":[{"s":"DomeNet","p":"sekrit99"}]}'),
            ("sbus", "<", "[SBUS] AP mode  SSID: SBUSCtrl  Pass: sekrit99"),
            ("wcb1", "<", "?EPASS,hunter2"),
            ("runner", "#", "a plain note")):
        b.log(name, direction, text)
    b.close()
    lines = read(os.path.join(b.out_dir, "session.log")).splitlines()
    ours = [x for x in lines if " navicore " in x or "     sbus " in x]
    assert len(ours) == 7 and all("<redacted:" in x for x in ours), ours
    assert not any(s in x for x in ours for s in SECRETS), "a NaviCore or SBUS line kept a credential"
    assert next(x for x in lines if " wcb1 " in x).endswith("?EPASS,hunter2"), "a WCB's lines are left as they were"
    assert sink == lines, "the GUI's log view gets the same filtered lines"
    j, e = rt('{"password":"hunter2"}'), rt("?EPASS,hunter2")
    assert j[len('{"password":"'):-2] == e[len("?EPASS,"):] and rt(j) == j and rt(e) == e, (j, e)
    assert rt('{"wifiPassword":""}') == '{"wifiPassword":""}', "an empty password is no secret"
    esc = rt('{"line":"{\\"type\\":\\"SET_CONFIG\\",\\"data\\":{\\"wifiPassword\\":\\"sekrit99\\"}}"}')
    assert "sekrit99" not in esc and rt(esc) == esc, esc
    assert rt('{"meshPassword":"hunter2","apPass":"x"}').startswith('{"meshPassword":"<redacted:'), "any *password key"
    a = {"tapWindowMs": 500, "mappings": {"101": {}}, "maestros": [{"type": 1}, {"type": 2}],
         "wcbNetwork": {"password": "hunter2", "deviceId": 20}, "wcbProfiles": [{"password": "hunter2"}]}
    z = json.loads(json.dumps(a))
    z.update(tapWindowMs=600, serialLabels={"S5": "HIL"})
    z["mappings"]["136"] = {"t1": [1]}
    z["maestros"][1]["channels"] = []
    z["wcbNetwork"]["password"] = "DomeNet"
    z["wcbProfiles"][0]["password"] = "sekrit99"
    got = rd(a, z)
    assert got[:3] == ["tapWindowMs: 500 -> 600", "mappings.136 added", "maestros[1].channels added"], got
    assert got[3].startswith("wcbNetwork.password: <redacted:") and got[4].startswith("wcbProfiles[0].password: "), got
    assert got[5] == "serialLabels added" and not any(s in x for x in got for s in SECRETS), got
    assert rd(a, a) == [] and len(rd({"k": list(range(50))}, {"k": list(range(1, 51))}, limit=5)) == 6
    assert rd({"wifiNets": [{"p": "sekrit99"}]}, {"wifiNets": []})[0].startswith("wifiNets: <redacted:")


def t_nc_guard_bench_test(tmp):
    """suites/s40_navicore_config.py nccfg.guard_selftest's own logic, run by the runner against FakeNaviBoard: it
    passes, NaviCore ends byte-identical, it notes that the snapshot alone left all three changes in place, and its
    session.log scan finds the config lines it needs (so a PASS on the bench means the scan saw them there too)."""
    saved = list(runner.REGISTRY)
    try:
        import suites.s40_navicore_config as S40
    finally:
        runner.REGISTRY[:] = saved              # the body only: the real test never joins a selftest run
    board = FakeNaviBoard()
    orig = board.text()
    b = _nc_bench(tmp, board)
    ck = new_run(b, [fake("nccfg.guard_selftest", S40.guard_selftest)])
    r = ck.data["results"][0]
    assert r["status"] == "PASS", r["detail"]
    assert board.text() == orig and ck.data["navicore"]["state"] == "restored" and ck.data["navicore"]["path"] == "diff"
    log = read(os.path.join(ck.out_dir, "session.log"))
    assert "the snapshot re-sent alone left ['mapping', 'channels', 'label'] in place" in log, "the trap, noted"
    assert "traps exercised: mapping, channels, label" in log and not any(s in log for s in SECRETS)
    board.c["mappings"]["136"] = board._mapping({"t1note": "taken"})   # 136 taken: the next inert key is used
    board.c["maestros"][2]["channels"] = {1: {"name": "x", "min": 0, "max": 0}}
    assert S40._inert_mapping_key(json.loads(board.text())) == "236"
    assert S40._slot_without_channels(json.loads(board.text())) == (2, True)
    b.close()


# ---------------------------------------------------------------------------- NaviCore over the mesh (INF6, hil/ncmesh.py)
# Payloads run through the config tool's real _fragChunks (NaviCore config_tool/index.html:5556-5594, extracted and run
# in node 24 on 2026-09-28) and the length, in code points, of every chunk it cut. The Python mirror must cut the same.
NCMESH_GOLDEN = [
    ('{"sys":1,"type":"PING"}', 7, [23]),
    ('{"sys":1,"type":"SET_CONFIG","saveId":4242,"data":{"mappings":{"136":{"t1":[{"type":"wcb_unicast","target":"1",'
     '"cmd":";S2HIL\\"quoted\\"\\\\back"}],"t1note":"Dôme 中 \U0001f642"}},"serialLabels":{"S5":"tab\\there'
     '\\nnl\\u0001ctl"}}}', 4242, [117, 97]),
    ("x" * 1000, 65535, [143, 143, 143, 143, 143, 143, 142]),
    ('"' * 300, 1, [71, 71, 71, 71, 16]),
    ("\\" * 150 + "é" * 100 + "中" * 90 + "\U0001f642" * 80, 300, [71, 71, 71, 60, 47, 40, 35, 25]),
    ("\u0001\u0002\u001f" * 70, 12, [23, 23, 23, 23, 23, 23, 23, 23, 23, 3]),
]


def t_ncmesh_fragments(tmp):
    """hil/ncmesh.py fragments(), the config tool's _fragChunks and sendJSON envelopes (NAVICORE.md INF6): the same cuts
    as the tool's own function on six payloads (quotes, backslashes, control characters, 2-, 3- and 4-byte UTF-8, an
    emoji the tool walks as one code point); every envelope {"f","of","sid","s"} in that key order, at most 187 UTF-8
    bytes, the slices joining back into the payload with no code point split; each slice but the last full (one more
    code point would pass the 143-byte escaped budget); the escaping JSON.stringify writes; dict payloads serialised as
    JSON.stringify does; the refusals (sid 0 or 65536, more than 192 parts unless lifted, an empty payload, a lone
    surrogate); and the pacing: 100 ms between envelopes, the link term only for a line long enough, nothing after the
    last, any order and repeats sent as asked."""
    from hil import ncmesh as M
    for payload, sid, cuts in NCMESH_GOLDEN:
        envs = M.fragments(payload, sid)
        objs = [json.loads(e) for e in envs]
        assert [len(o["s"]) for o in objs] == cuts, (payload[:30], [len(o["s"]) for o in objs], cuts)
        assert all(list(o) == ["f", "of", "sid", "s"] for o in objs), "key order f, of, sid, s"
        assert [o["f"] for o in objs] == list(range(1, len(cuts) + 1)) and {o["of"] for o in objs} == {len(cuts)}
        assert {o["sid"] for o in objs} == {sid} and "".join(o["s"] for o in objs) == payload
        assert all(len(e.encode("utf-8")) <= M.ENV_MAX_BYTES for e in envs), [len(e.encode()) for e in envs]
        for o, nxt in zip(objs, objs[1:]):
            esc = sum(M.esc_bytes(c) for c in o["s"])
            assert esc <= M.ENV_BUDGET < esc + M.esc_bytes(nxt["s"][0]), (esc, nxt["s"][0])
        assert all(e == M.compact(o) for e, o in zip(envs, objs)), "each envelope is its own compact JSON"
    assert M.fragments('{"sys":1,"type":"PING"}', 7) == ['{"f":1,"of":1,"sid":7,"s":"{\\"sys\\":1,\\"type\\":\\"PING\\"}"}']
    assert M.envelope(1, 1, 3, 'a"\\\n\t\x01é') == '{"f":1,"of":1,"sid":3,"s":"a\\"\\\\\\n\\t\\u0001é"}'
    assert [M.esc_bytes(c) for c in '"\\\b\t\n\f\r\x00\x1fa\x7fé中\U0001f642'] == [2, 2, 2, 2, 2, 2, 2, 6, 6, 1, 1,
                                                                                            2, 3, 4]
    obj = {"sys": 1, "type": "SET_CONFIG", "saveId": 5, "data": {"t": "é"}}
    assert M.fragments(obj, 9) == M.fragments('{"sys":1,"type":"SET_CONFIG","saveId":5,"data":{"t":"é"}}', 9)
    big = "y" * (M.ENV_BUDGET * M.MAX_PARTS + 1)            # 193 full slices
    for bad in (lambda: M.fragments("x", 0), lambda: M.fragments("x", 65536), lambda: M.fragments("", 1),
                lambda: M.fragments("a\ud800b", 1), lambda: M.fragments(big, 1)):
        _raises(bad, ValueError)
    assert len(M.fragments(big, 1, max_parts=None)) == M.MAX_PARTS + 1
    import math
    env = M.envelope(999, 999, 99999, "x" * M.ENV_BUDGET)    # the worst case the chunker plans for
    assert len(env.encode()) == M.ENV_TARGET_BYTES and M.pace_s(env) == 0.1, "a 180-byte envelope: 17 ms, the floor wins"
    assert M.pace_s(env, prefix="p" * 2000) == math.ceil((2000 + 180 + 1) / 11.52) * 2 / 1000, "a long line: the link"
    sent, slept = [], []
    w1 = FakeNaviDev(lambda text, n: sent.append(text) or [], name="wcb1")
    from hil.wcb import WCB
    envs = M.fragments("z" * 400, 77)
    M.send_fragments(WCB(w1), envs, order=[2, 0, 0, 1], sleep=slept.append)
    assert sent == [";W20," + envs[i] for i in (2, 0, 0, 1)], sent
    assert slept == [M.pace_s(envs[i], ";W20,") for i in (2, 0, 0)] == [0.1, 0.1, 0.1], slept
    sent.clear()
    M.send_fragments(WCB(w1), envs, gap_s=0.25, sleep=slept.append)
    assert len(sent) == 3 and slept[-2:] == [0.25, 0.25]
    _raises(lambda: M.send_fragments(WCB(w1), ['{"s":"' + "q" * 190 + '"}'], sleep=slept.append), ValueError)


def t_ncmesh_bridged_reassemble(tmp):
    """bridged(): ';W20,<json>' typed on W1 exactly (a dict compacted with its key order kept), the {"sys":1 lines
    parsed and W1's other lines kept, the first line matching the pattern returned, None - not a raise - when nothing
    matches in time, and JSON refused that one mesh packet cannot hold (over 187 bytes, or two lines). reassemble(): the
    tool's receive side - two interleaved sessions with a duplicate part, joined in completion order; envelopes with f 0,
    f > of, of 0 or a negative sid ignored; a sid reused with another "of" starting over; a session that never completes
    left out."""
    from hil import ncmesh as M
    from hil.wcb import WCB
    pong = '{"sys":1,"type":"PONG","id":20,"version":"v0.2.0_TEST","model":0,"mode":1}'

    def w1_script(text, n):
        if text == ';W20,{"type":"PING"}':
            return ['{"sys":1,"type":"rc_hb","id":20,"fw":"v0.2.0_TEST"}', "[ETM] WCB2 came ONLINE", pong, '{"sys":1,x']
        return []
    w1 = FakeNaviDev(w1_script, name="wcb1")
    r = M.bridged(WCB(w1), {"type": "PING"}, r'"type":"PONG"', timeout=1.0)
    assert w1.sent == [';W20,{"type":"PING"}'] and r.match and r.match.string == pong, (w1.sent, r)
    assert [o["type"] for o in r.sys] == ["rc_hb", "PONG"] and "[ETM] WCB2 came ONLINE" in r.lines, r
    t0 = time.monotonic()
    r = M.bridged(WCB(w1), '{"type":"TRIGGER","mode":1,"btn":36,"tap":1}', r'"type":"ACK"', timeout=0.2)
    assert r.match is None and r.lines == [] and time.monotonic() - t0 >= 0.2, r
    r = M.bridged(WCB(w1), {"type": "PING"}, timeout=0.1)
    assert r.match is None and len(r.sys) == 2
    for bad in ({"type": "X", "pad": "p" * 180}, '{"type":"X"}\n{"type":"Y"}', {"type": "\ud800"}):
        _raises(lambda: M.bridged(WCB(w1), bad, timeout=0.1), ValueError)
    a, b = M.fragments("A" * 300 + '"', 5), M.fragments("B" * 150, 6)
    lines = ["noise", a[0], b[0], a[1], a[1], M.envelope(0, 3, 5, "bad"), M.envelope(4, 3, 5, "bad"),
             M.envelope(1, 0, 8, "bad"), M.envelope(1, 1, -1, "bad"), b[1], a[2], '{"f":1,"of":2}',
             M.envelope(1, 2, 9, "old"), M.envelope(1, 3, 9, "N1"), M.envelope(3, 3, 9, "N3"), M.envelope(2, 3, 9, "N2"),
             M.envelope(1, 4, 10, "never")]
    assert len(a) == 3 and len(b) == 2, (len(a), len(b))
    assert M.reassemble(lines) == [(6, "B" * 150), (5, "A" * 300 + '"'), (9, "N1N2N3")], M.reassemble(lines)


def _fake_nc_mesh(board):
    """A NaviCore console for burn_window: SET_DEBUG_FLAGS ACKed and remembered, #L12 answered; `board['rx']` is where
    the fake probe delivers."""
    def script(text, n):
        if text.startswith('{"type":"SET_DEBUG_FLAGS"'):
            board["flags"] = json.loads(text)["flags"]
            return ['{"type":"ACK","ok":true}']
        if text == "#L12":
            return ["Mode=1  matrixBtn=0  matrixVal=992"]
        if text == '{"type":"GET_WCB_STATUS"}':
            return [board.get("status", '{"type":"WCB_STATUS","quantity":1,"self":20,"online":[1,1],"known":[1,1]}')]
        if text == '{"type":"GET_CONFIG"}':
            out = board.get("bcast_out", ())
            bc = {p: {"out": p in out, "in": False} for p in ("S3", "S4", "S5")}
            return ['{"type":"CONFIG","data":' + json.dumps({"chRateHz": 5, "serialBcast": bc}, separators=(",", ":"))
                    + "}"]
        return []
    return FakeNaviDev(script, name="navicore")


class FakeMeshProbe:
    """A joined probe whose commands to NaviCore pass WCB_Client's duplicate window (hil.ncmesh.cmd_seq_dup, a port of
    WCB_Client.cpp:879-898) with the numbers a fresh join uses, 1, 2, 3...; an accepted one appears on NaviCore's console
    as onWCBCommand prints it under DBG_MAESTRO. `lost` drops a send on the air."""

    def __init__(self, nav, board, device_id=16, window=None, lost=lambda k: False):
        self.nav, self.board, self.id, self.seq, self.lost = nav, board, device_id, 0, lost
        self.window = window if window is not None else {}
        self.accepted = []

    def mesh_send(self, target, text, ensured=True):
        from hil import ncmesh as M
        self.seq += 1
        if self.lost(self.seq) or M.cmd_seq_dup(self.window, self.seq):
            return True
        self.accepted.append(self.seq)
        if self.board.get("flags", 0) & 1:
            self.nav._append(f"[WCB RX] from WCB{self.id}: {text}")
        return True


def _old_window(high, seen):
    """NaviCore's window after an earlier session under the same id that it heard send `seen` numbers (the last one
    `high`)."""
    from hil import ncmesh as M
    w = {}
    for s in sorted(set(seen) | {high}):
        M.cmd_seq_dup(w, s)
    return w


def t_ncmesh_burn_window(tmp):
    """burn_window() and cmd_seq_dup(): the port of WCB_Client's duplicate window (the high always a duplicate, a seen
    number up to 32 below it one, an unseen one there taken and marked, 33 below or more taken without a trace, a newer
    one moving the window, the int16 wrap). Then the burn against it: with no earlier session, 66 sends and nothing
    dropped; for every earlier session whose last number NaviCore heard was at most 66 - a dense one and a sparse one
    (only that last number) - the burn sees the drop and goes 34 past it, after which the next 40 commands are all
    taken; drops that go on (a lossy air) stop at the cap with an AssertionError, and so does a probe NaviCore never
    hears. DBG_MAESTRO is set for the burn and cleared after."""
    from hil import ncmesh as M
    from hil.navicore import NaviCore
    w = {}
    assert [M.cmd_seq_dup(w, s) for s in (10, 10, 9, 9, 12, 11, 12, 45, 13, 13, 12)] == \
        [False, True, False, True, False, False, True, False, False, True, False], w
    w = {}
    assert [M.cmd_seq_dup(w, s) for s in (65535, 1, 65535, 2)] == [False, False, True, False], "the int16 wrap"
    for high in range(1, 67):
        for seen in (range(1, high + 1), [high]):
            board = {}
            nav = _fake_nc_mesh(board)
            probe = FakeMeshProbe(nav, board, window=_old_window(high, seen))
            res = M.burn_window(NaviCore(nav), probe, 16, gap_s=0, settle_s=0)
            assert high in res["dropped"] and res["sent"] >= max(66, high + 34), (high, res)
            before = len(probe.accepted)
            for _ in range(40):
                probe.mesh_send(20, "HILtest")
            assert len(probe.accepted) == before + 40, (high, len(seen), "a test command was dropped")
            assert board["flags"] == 0, "the debug flags go back to 0"
    board = {}
    nav = _fake_nc_mesh(board)
    res = M.burn_window(NaviCore(nav), FakeMeshProbe(nav, board), 16, gap_s=0, settle_s=0)
    assert res["sent"] == 66 and res["dropped"] == [] and res["tag"].startswith("HILB"), res
    board = {}
    nav = _fake_nc_mesh(board)
    lossy = FakeMeshProbe(nav, board, lost=lambda k: k % 20 == 0)
    e = _raises(lambda: M.burn_window(NaviCore(nav), lossy, 16, gap_s=0, settle_s=0))
    assert "still drops the burn" in str(e) and board["flags"] == 0, e
    board = {}
    nav = _fake_nc_mesh(board)
    deafp = FakeMeshProbe(nav, board, lost=lambda k: True)
    assert "printed none of the 66" in str(_raises(lambda: M.burn_window(NaviCore(nav), deafp, 16, gap_s=0,
                                                                          settle_s=0)))


def t_ncmesh_deaf_and_probe_peer(tmp):
    """deaf(): the octet from the board's own chain, flipped (XOR 1) on the board's OWN console and flipped back however
    the block ends - normally, on a failure (which still propagates), on an abort; a flip the board refuses fails the
    test with the octet still sent back; a board that will not confirm the octet back fails it as STILL DEAF with the
    command to type; a board with no console of its own is refused (Skip) before anything is sent. probe_peer():
    refuses an id NaviCore knows, and a NaviCore that writes mesh text out an aux port (serialBcast out), before any
    join; joins with quantity 20 unless told otherwise (so the probe has NaviCore as an ESP-NOW peer from begin()), and
    burns the window before the body runs."""
    from contextlib import contextmanager as _cm
    from hil import ncmesh as M
    from hil.runner import Skip
    state = {"oct": "1A", "refuse": set(), "silent": False}

    def w_script(text, n):
        if text.startswith(";S0,"):
            return [text[4:]]
        if text.startswith("?MAC,3,"):
            val = text[len("?MAC,3,"):]
            if state["silent"] or val in state["refuse"]:
                return ["Invalid hex value for 3rd MAC octet. Use two hex digits (00-FF)."]
            state["oct"] = val
            return [f"Updated 3rd MAC octet to 0x{val}"]
        return []
    b = tmp.bench({"wcb1": {"port": "COMW1", "kind": "wcb", "wcb": 1}, "wcb2": {"port": "COMW2", "kind": "wcb", "wcb": 2},
                   "navicore": {"port": "COMNAV", "kind": "navicore"}})
    w2 = FakeNaviDev(w_script, name="wcb2")
    b.dev = lambda name: {"wcb2": w2}[name]
    b.config_tokens = lambda n, refresh=False: ["?HW,24", "?MAC,2,00", "?MAC,3,1A", "?WCB,2"]
    with M.deaf(b, 2) as w:
        assert state["oct"] == "1B" and w.dev is w2
    assert state["oct"] == "1A" and [x for x in w2.sent if x.startswith("?MAC")] == ["?MAC,3,1B", "?MAC,3,1A"]

    def failing():
        with M.deaf(b, 2):
            raise ValueError("the body failed")
    assert "the body failed" in str(_raises(failing, ValueError)) and state["oct"] == "1A"
    try:
        with M.deaf(b, 2):
            raise KeyboardInterrupt
    except KeyboardInterrupt:
        pass
    assert state["oct"] == "1A", "an abort still puts the octet back"
    state["refuse"] = {"1B"}
    w2.sent.clear()
    assert "did not take ?MAC,3,1B" in str(_raises(failing))
    assert [x for x in w2.sent if x.startswith("?MAC")] == ["?MAC,3,1B", "?MAC,3,1A"], w2.sent
    state["refuse"] = set()

    def stays_deaf():
        with M.deaf(b, 2):
            state["silent"] = True
    msg = str(_raises(stays_deaf))
    assert "W2 IS STILL DEAF" in msg and "type ?MAC,3,1A on W2's own console" in msg, msg
    state.update(silent=False, oct="1A")
    b.cfg["devices"].pop("wcb2")
    w2.sent.clear()
    _raises(lambda: M.deaf(b, 2).__enter__(), Skip)
    assert w2.sent == [], "nothing sent to a board with no console of its own"

    import suites.common as C
    board, joined = {}, []
    nav = _fake_nc_mesh(board)

    @_cm
    def fake_join(bench, probe_name, device_id, forget=True, **overrides):
        joined.append((probe_name, device_id, overrides))
        yield FakeMeshProbe(nav, board, device_id=device_id)
    b2 = tmp.bench({"wcb1": {"port": "COMW1", "kind": "wcb", "wcb": 1}, "navicore": {"port": "COMNAV", "kind": "navicore"}})
    b2.new_session()
    b2.dev = lambda name: {"navicore": nav}[name]
    saved = (C.probe_in_mesh, M.burn_window)
    C.probe_in_mesh = fake_join
    M.burn_window = lambda nc, probe, device_id, target=20: {"sent": 66, "dropped": [], "tag": "HILBTEST"}
    try:
        with M.probe_peer(b2, 16) as probe:
            assert probe.burn["sent"] == 66
        assert joined == [("probe1", 16, {"quantity": 20})], joined
        with M.probe_peer(b2, 15, probe_name="probe2", quantity=5, checksum=False):
            pass
        assert joined[-1] == ("probe2", 15, {"quantity": 5, "checksum": False}), joined
        board["status"] = '{"type":"WCB_STATUS","quantity":1,"self":20,"online":[1],"known":[1,0,0,0,0,0,0,0,0,0,0,0,1]}'
        _raises(lambda: M.probe_peer(b2, 13).__enter__(), Skip)
        assert len(joined) == 2, "a known id is refused before any join"
        board.pop("status")
        board["bcast_out"] = ("S5",)
        assert "out S5 (serialBcast out)" in str(_raises(lambda: M.probe_peer(b2, 14).__enter__(), Skip))
        assert len(joined) == 2, "serialBcast out is refused before any join"
    finally:
        C.probe_in_mesh, M.burn_window = saved
    b2.close()


# ---------------------------------------------------------------------------- NaviCore's config surface (NC-WP1, s40)
def _nm_cut(s, size):
    """strlcpy(dst, s, size) as the harness then reads it: at most size-1 BYTES of the UTF-8, a cut character decoded
    as U+FFFD (SerialDevice decodes errors='replace')."""
    return (s if isinstance(s, str) else "").encode("utf-8")[:size - 1].decode("utf-8", errors="replace")


def _nm_pick(obj, key, default):
    """ArduinoJson 7's `obj[key] | default`: the value when it has the default's type (int for int, bool for bool,
    str for str), else the default. A null object reads every key as the default."""
    v = obj.get(key) if isinstance(obj, dict) else None
    if isinstance(default, bool):
        return v if isinstance(v, bool) else default
    if isinstance(default, int):
        return v if isinstance(v, int) and not isinstance(v, bool) else default
    return v if isinstance(v, str) else default


def _nm_as_int(v):
    """`variant.as<int>()`: an int as it is, anything else 0."""
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def _nm_wrap(v, bits, signed=False):
    v &= (1 << bits) - 1
    return v - (1 << bits) if signed and v >= 1 << (bits - 1) else v


def _nm_toint(key):
    """Arduino String::toInt(): the leading integer, 0 without one."""
    m = re.match(r"\s*([-+]?\d+)", key)
    return int(m.group(1)) if m else 0


NM_HCR_OK = {2: "emo", 3: "emo", 4: "emo", 5: None, 6: None, 8: None, 9: None, 11: None, 20: None, 21: None, 7: "gap",
             10: "ovr", 13: "muse", 14: "wav", 16: "stop", 17: "vol", 18: "step", 19: "step"}


def _nm_hcr_ok(fn, chan, track):
    """HcrCodec::normalize (WcbCmd WcbHcr.cpp:7-43)."""
    k = NM_HCR_OK.get(fn, "bad")
    return {"bad": False, None: True, "emo": 0 <= chan <= 3 and 0 <= track <= 99, "gap": 0 <= chan <= 99 and 0 <= track <= 99,
            "ovr": 0 <= chan <= 1, "muse": 0 <= track <= 1, "wav": 0 <= chan <= 2 and 0 <= track <= 9999,
            "stop": 0 <= chan <= 2, "vol": 0 <= chan <= 3 and 0 <= track <= 100, "step": 0 <= track <= 99}[k]


class NaviModel:
    """NaviCore's USB console and config handling for the s40 suite, ported from the firmware this suite was written
    against (NaviCore.ino processInputLine :3773-4257, execCliLine :3359-3693, applySerialBauds/applySbusOut/
    applyConfigSideEffects :3229-3345; rc_config.h rcConfigLoadDefaults :801-968, actionToJson/actionFromJson
    :970-1155, rcConfigToJSON :1211-1482, rcConfigFromJSON :1502-1905). It keeps RAM and flash apart (SET_CONFIG saves,
    RESET_DEFAULTS does not, REBOOT reloads the flash copy) and a boot-time copy of the mesh identity, so the RTERM
    split (D-NC17) shows. W1's console (w1_script) relays ;W20 JSON and CLI lines and lists NaviCore's advertised port
    labels in its ?WDP,DUMP. What it does not model it answers with silence, and a forbidden command (#L2, #L20, #L21,
    ?FORGET,ALL, ?REC,START...) fails the selftest outright."""
    FW = "v0.2.0_102105QSEP26"
    FACTORY_PW = "DomeNet"
    BAND = [b for b in [("B1", 1799, 1823), ("B2", 1758, 1782), ("B3", 1718, 1742), ("B4", 1676, 1700),
                        ("B5", 1634, 1658), ("B6", 1594, 1618), ("T4 Left", 1553, 1577), ("T4 Right", 1512, 1536),
                        ("T5 Left", 1471, 1495), ("T5 Right", 1430, 1454), ("T3 Up", 1389, 1413),
                        ("T3 Down", 1348, 1372), ("T2 Up", 1308, 1332), ("T2 Down", 1266, 1290),
                        ("T6 Left", 1225, 1249), ("T6 Right", 1184, 1208), ("T1 Left", 1143, 1167),
                        ("T1 Right", 1103, 1127), ("L-Stick Click", 1062, 1086), ("R-Stick Click", 1021, 1045),
                        ("Unassigned", 0, 0)]] + [(f"Logical {i}", 0, 0) for i in range(1, 16)]
    SW = ("SA", "SB", "SC", "SD", "SE", "SF", "SG", "SH", "SI", "SJ")
    SW_CH, SW_POS = (8, 9, 10, 11, 12, 13, 14, 15, 0, 0), (3, 3, 3, 3, 3, 2, 3, 2, 2, 2)
    KN = ("S1", "S2", "LS", "RS", "S3", "J1", "J2", "J3", "J4", "J5", "J6")
    KN_CH = (5, 6, 0, 0, 0, 1, 2, 3, 4, 0, 0)
    LBL = ("S3", "S4", "S5", "maestro")
    FORBIDDEN = (re.compile(r"^#[Ll]0?2$"), re.compile(r"^#[Ll]2[01]"), re.compile(r"^\?FORGET,ALL", re.I),
                 re.compile(r"^\?REC,(START|SAVE|RM|RENAME|EDIT|CLEAR|LOAD|PLAY|STOP)", re.I),
                 re.compile(r'"type":"(REBOOT|FORGET_PEER)".*"all":true'))

    def __init__(self):
        self.c = self.defaults()
        bench = {"sbusOutEnabled": True, "wifiEnabled": True, "wifiSsid": "HILap", "wifiPassword": "sekrit99",
                 "wcbNetwork": {"password": "hunter2", "quantity": 1},
                 "wcbProfiles": [{"name": "Dev", "macOct2": 0, "macOct3": 0, "password": "hunter2", "quantity": 1,
                                  "deviceId": 20, "channel": 1},
                                 {"name": "Droid", "macOct2": 0, "macOct3": 1, "password": "sekrit99", "quantity": 4,
                                  "deviceId": 20, "channel": 1}],
                 "hcrDest": {"transport": "wcb", "target": "2", "wcbPort": 1},
                 "wledSlots": [{"id": 1, "port": 0, "wcb": 2, "configured": True}],
                 "auxBaud": {"S3": 115200, "S4": 9600, "S5": 9600, "maestro": 57600},
                 "maestros": [{"type": 1, "device": 1, "channels": [{"ch": 0, "name": "Pie 1", "min": 3968, "max": 8000},
                                                                   {"ch": 2, "name": "Pie 2", "min": 4000, "max": 7600}]}]
                             + [{"type": 2, "device": i} for i in range(2, 9)],
                 "mappings": {"101": {"t1": [{"type": "wcb_unicast", "target": "1", "cmd": ";S2HILA"}], "t1note": "Dome"},
                              "207": {"exclusive": True, "t2": [{"type": "maestro", "target": "1", "cmd": "goHome"}]}},
                 "switches": {"SC": {"channel": 10, "positions": 3, "p1": [{"type": "maestro", "target": "1",
                                                                             "cmd": "setEasing,p1"}]},
                              "SI": {"channel": 16, "positions": 2, "p0": [{"type": "wcb_broadcast", "cmd": ";W1;S3x"}]},
                              "SJ": {"channel": 17, "positions": 2, "p1": [{"type": "wcb_broadcast", "cmd": ";W2;S2y"}]}},
                 "knobs": {"J2": {"channel": 24, "function": 1, "modeAware": True,
                                  "outputs": [{"target": 1, "maestroCh": 0, "posMin": 4000, "posMax": 8000,
                                               "releaseIdleMs": 1500}]},
                           "J4": {"channel": 4, "function": 1, "smoothProfile": 1,
                                  "outputs": [{"target": t, "maestroCh": 0, "posMin": 4000, "posMax": 8000}
                                              for t in range(2, 8)]},
                           "S1": {"channel": 5, "function": 2, "outputs": [{"target": 1, "maestroCh": 0, "posMin": 0,
                                                                            "posMax": 99}]}},
                 "smoothProfiles": [{"name": "Default", "entries": [{"mid": 1, "ch": 0, "spd": 20, "acc": 3}]},
                                    {"name": "Snappy", "entries": [{"mid": 2, "ch": 0, "spd": 60, "acc": 0}]}]
                                   + [{"name": ""}] * 4,
                 "modeReport": {"enabled": True, "wcb": 2, "template": ";V,MODE,{mode}"},
                 "statsReport": {"enabled": True, "wcb": 1}}
        self.merge(bench)
        self.flash = self.text()
        self.cmdlib = '{"boards":[{"id":"HILboard","cmds":[";S1x"]}],"enums":{}}'
        self.clips = ["intro"]
        self.mode, self.flags, self.monitor, self.calib = 1, 0, False, False
        self.channels = [992] * 24
        self.channels[6], self.channels[11] = 992, 172
        self.boot()
        self.nav = self.w1 = None
        self.received = []
        self.seq_busy = False

    # ------------------------------------------------------------ the config model
    @classmethod
    def defaults(cls):
        return {"txModel": 0, "threeAxisGimbals": False, "sbusOutEnabled": False, "wifiEnabled": False, "wifiSsid": "",
                "wifiPassword": "", "maeGateMs": 250, "boardType": 0, "tapWindowMs": 500, "holdMs": 750,
                "switchSettleMs": 80, "chRateHz": 5, "matrixChannel": 7, "matrixDebounceFrames": 1, "modeSwitch": 4,
                "peerAlert": True, "peerActions": [],
                "thresholds": [{"id": i + 1, "label": b[0], "minPwm": b[1], "maxPwm": b[2]} for i, b in enumerate(cls.BAND)],
                "mappings": {},
                "switches": [{"channel": cls.SW_CH[i], "positions": cls.SW_POS[i], "t": [([], "")] * 3} for i in range(10)],
                "knobs": [{"channel": cls.KN_CH[i], "function": 0, "reverse": False, "modeAware": False,
                           "modeSwitchOverride": -1, "smoothProfile": -1, "easeSwitchOverride": False, "outputs": [],
                           "outputs2": [], "outputs3": []} for i in range(11)],
                "hcrDest": {"transport": 2, "target": "S3", "wcbPort": 1},
                "maestros": [{"type": 0, "device": i + 1, "channels": {}} for i in range(8)],
                "wcbNetwork": {"macOct2": 0, "macOct3": 0, "password": cls.FACTORY_PW, "quantity": 4, "deviceId": 20,
                               "channel": 1},
                "wcbProfiles": [], "mp3Dest": {"transport": 2, "target": "2"}, "dfpDest": {"transport": 2, "target": "S3"},
                "wledSlots": [{"wledID": 0, "serialPort": 0, "remoteWCB": 0, "configured": False} for _ in range(4)],
                "auxBaud": [9600, 9600, 9600], "maestroBaud": 115200, "serialLabels": ["", "", "", ""],
                "bcastOut": [False] * 3, "bcastIn": [False] * 3,
                "modeReport": {"enabled": False, "wcb": 0, "tmpl": "", "cmds": ["", "", ""]},
                "statsReport": {"enabled": False, "wcb": 0},
                "smooth": [{"name": "Default" if p == 0 else "", "entries": {}} for p in range(6)]}

    @staticmethod
    def action_from(o):
        """actionFromJson (rc_config.h:1063-1155) -> the internal action, or None."""
        o = o if isinstance(o, dict) else {}
        t = _nm_pick(o, "type", "")
        a = {"type": None, "target": "", "cmd": "", "delay": _nm_wrap(_nm_pick(o, "delay", 0), 16), "skipRunning": False,
             "note": "", "fn": 0, "chan": 0, "track": 0}
        if t in ("wcb_unicast", "maestro_remote", "maestro"):
            a.update(type="maestro" if t != "wcb_unicast" else t, target=_nm_cut(_nm_pick(o, "target", ""), 6),
                     cmd=_nm_cut(_nm_pick(o, "cmd", ""), 96), skipRunning=_nm_pick(o, "skipRunning", False))
        elif t == "wcb_broadcast":
            a.update(type=t, cmd=_nm_cut(_nm_pick(o, "cmd", ""), 96), skipRunning=_nm_pick(o, "skipRunning", False))
        elif t in ("maestro_local", "wled", "record"):
            a.update(type=t, cmd=_nm_cut(_nm_pick(o, "cmd", ""), 96))
        elif t == "serial":
            a.update(type=t, target=_nm_cut(_nm_pick(o, "port", ""), 6), cmd=_nm_cut(_nm_pick(o, "cmd", ""), 96))
        elif t in ("hcr", "dfplayer", "mp3"):
            a.update(type=t, fn=_nm_wrap(_nm_pick(o, "fn", 0), 8), track=_nm_wrap(_nm_pick(o, "track", 0), 16, True),
                     chan=0 if t == "mp3" else _nm_wrap(_nm_pick(o, "chan", 0), 8, True))
        elif t == "play":
            a.update(type=t, cmd=_nm_cut(_nm_pick(o, "cmd", ""), 96), fn=_nm_wrap(_nm_pick(o, "fn", 0), 8))
        elif t == "stop":
            a.update(type=t)
        else:
            return None
        a["note"] = _nm_cut(_nm_pick(o, "note", ""), 20)
        return a

    @staticmethod
    def action_to(a):
        """actionToJson (rc_config.h:970-1061)."""
        o = {"type": a["type"]}
        t = a["type"]
        if t in ("wcb_unicast", "maestro"):
            o.update(target=a["target"], cmd=a["cmd"])
        elif t in ("wcb_broadcast", "maestro_local", "wled", "record"):
            o["cmd"] = a["cmd"]
        elif t == "serial":
            o.update(port=a["target"], cmd=a["cmd"])
        elif t in ("hcr", "dfplayer"):
            o.update(fn=a["fn"], chan=a["chan"], track=a["track"])
        elif t == "mp3":
            o.update(fn=a["fn"], track=a["track"])
        elif t == "play":
            o.update(cmd=a["cmd"], fn=a["fn"])
        if a["delay"]:
            o["delay"] = a["delay"]
        if t in ("wcb_unicast", "wcb_broadcast", "maestro") and a["skipRunning"]:
            o["skipRunning"] = True
        if a["note"]:
            o["note"] = a["note"]
        return o

    def tier_from(self, obj, key):
        out = []
        if isinstance(obj, dict) and key in obj:
            for o in obj[key] if isinstance(obj[key], list) else []:
                if len(out) >= 5:
                    break
                a = self.action_from(o)
                if a:
                    out.append(a)
        return out

    @staticmethod
    def read_outs(arr):
        out = []
        for o in arr if isinstance(arr, list) else []:
            if len(out) >= 10:
                break
            o = o if isinstance(o, dict) else {}
            out.append({"target": _nm_wrap(_nm_pick(o, "target", 0), 8) if "target" in o else (_nm_pick(o, "slot", 0) or 1),
                        "maestroCh": _nm_wrap(_nm_pick(o, "maestroCh", 0), 8),
                        "posMin": _nm_wrap(_nm_pick(o, "posMin", 4000), 16), "posMax": _nm_wrap(_nm_pick(o, "posMax", 8000), 16),
                        "midClosed": _nm_pick(o, "midClosed", False),
                        "releaseIdleMs": _nm_wrap(_nm_pick(o, "releaseIdleMs", 0), 16)})
        return out

    def merge(self, d):
        """rcConfigFromJSON (rc_config.h:1502-1905): each branch only when its key is present. Always true."""
        c = d if isinstance(d, dict) else {}
        r = self.c
        g = lambda k, dflt: _nm_pick(c, k, dflt)          # noqa: E731
        if "txModel" in c:
            r["txModel"] = _nm_wrap(g("txModel", 0), 8)
        for k in ("threeAxisGimbals", "sbusOutEnabled", "wifiEnabled"):
            if k in c:
                r[k] = g(k, False)
        if "wifiSsid" in c:
            r["wifiSsid"] = _nm_cut(g("wifiSsid", r["wifiSsid"]), 33)
        if "wifiPassword" in c:
            r["wifiPassword"] = _nm_cut(g("wifiPassword", r["wifiPassword"]), 64)
        if "maeGateMs" in c:
            r["maeGateMs"] = _nm_wrap(g("maeGateMs", 250), 16)
        if "boardType" in c:
            r["boardType"] = _nm_wrap(g("boardType", 0), 8)
        if "tapWindowMs" in c:
            r["tapWindowMs"] = _nm_as_int(c["tapWindowMs"])
        if r["tapWindowMs"] < 100:
            r["tapWindowMs"] = 500
        if "holdMs" in c:
            r["holdMs"] = _nm_as_int(c["holdMs"])
        if r["holdMs"] < r["tapWindowMs"] + 100:
            r["holdMs"] = r["tapWindowMs"] + 250
        if r["holdMs"] > 5000:
            r["holdMs"] = 5000
        if "switchSettleMs" in c:
            r["switchSettleMs"] = max(0, min(1000, g("switchSettleMs", 80)))
        if "chRateHz" in c:
            hz = g("chRateHz", 5)
            r["chRateHz"] = 5 if hz < 1 else min(hz, 20)
        if "matrixChannel" in c:
            r["matrixChannel"] = _nm_as_int(c["matrixChannel"])
        if "matrixDebounceFrames" in c:
            r["matrixDebounceFrames"] = max(1, min(4, g("matrixDebounceFrames", 1)))
        if "funcBindings" in c:
            r["modeSwitch"] = _nm_wrap(_nm_pick(c["funcBindings"], "mode", r["modeSwitch"]), 8, True)
        if "peerEvent" in c:
            pe = c["peerEvent"] if isinstance(c["peerEvent"], dict) else {}
            r["peerAlert"] = _nm_pick(pe, "alert", r["peerAlert"])
            if "actions" in pe:
                r["peerActions"] = self.tier_from(pe, "actions")
        if "thresholds" in c:
            for i, th in enumerate((c["thresholds"] if isinstance(c["thresholds"], list) else [])[:36]):
                th = th if isinstance(th, dict) else {}
                r["thresholds"][i] = {"id": _nm_pick(th, "id", i + 1), "label": _nm_cut(_nm_pick(th, "label", ""), 24),
                                      "minPwm": _nm_pick(th, "minPwm", 0), "maxPwm": _nm_pick(th, "maxPwm", 0)}
        if "mappings" in c:
            for k, v in (c["mappings"] if isinstance(c["mappings"], dict) else {}).items():
                bid = _nm_toint(k)
                mode, btn = int(bid / 100), bid - int(bid / 100) * 100
                if not (1 <= mode <= 3 and 1 <= btn <= 36):
                    continue
                v = v if isinstance(v, dict) else {}
                r["mappings"][f"{mode * 100 + btn}"] = {
                    "exclusive": _nm_pick(v, "exclusive", False),
                    "t": [(self.tier_from(v, f"t{t}"), _nm_cut(_nm_pick(v, f"t{t}note", ""), 20)) for t in range(1, 5)]}
        if "switches" in c:
            sw = c["switches"] if isinstance(c["switches"], dict) else {}
            for i, label in enumerate(self.SW):
                if label not in sw:
                    continue
                s = sw[label] if isinstance(sw[label], dict) else {}
                r["switches"][i] = {"channel": _nm_pick(s, "channel", self.SW_CH[i]),
                                    "positions": _nm_wrap(_nm_pick(s, "positions", self.SW_POS[i]), 8),
                                    "t": [(self.tier_from(s, f"p{p}"), _nm_cut(_nm_pick(s, f"p{p}note", ""), 20))
                                          for p in range(3)]}
        if "knobs" in c:
            kn = c["knobs"] if isinstance(c["knobs"], dict) else {}
            for i, label in enumerate(self.KN):
                if label not in kn:
                    continue
                k = kn[label] if isinstance(kn[label], dict) else {}
                n = {"channel": _nm_pick(k, "channel", self.KN_CH[i]), "function": _nm_wrap(_nm_pick(k, "function", 0), 8),
                     "reverse": _nm_pick(k, "reverse", False), "modeAware": _nm_pick(k, "modeAware", False),
                     "modeSwitchOverride": _nm_wrap(_nm_as_int(k["modeSwitchOverride"]), 8, True)
                     if "modeSwitchOverride" in k else -1,
                     "smoothProfile": _nm_wrap(_nm_as_int(k["smoothProfile"]), 8, True) if "smoothProfile" in k else -1,
                     "easeSwitchOverride": _nm_pick(k, "easeSwitchOverride", False),
                     "outputs": self.read_outs(k.get("outputs")) if "outputs" in k else []}
                n["outputs2"] = self.read_outs(k.get("outputs2")) if n["modeAware"] and "outputs2" in k else \
                    (list(n["outputs"]) if n["modeAware"] else [])
                n["outputs3"] = self.read_outs(k.get("outputs3")) if n["modeAware"] and "outputs3" in k else \
                    (list(n["outputs"]) if n["modeAware"] else [])
                r["knobs"][i] = n
        if "smoothProfiles" in c:
            r["smooth"] = [{"name": "", "entries": {}} for _ in range(6)]
            for p, po in enumerate((c["smoothProfiles"] if isinstance(c["smoothProfiles"], list) else [])[:6]):
                po = po if isinstance(po, dict) else {}
                r["smooth"][p]["name"] = _nm_cut(_nm_pick(po, "name", ""), 24)
                for e in po.get("entries") or [] if isinstance(po.get("entries"), list) else []:
                    mid, ch = _nm_pick(e, "mid", 1), _nm_pick(e, "ch", 0)
                    if 1 <= mid <= 8 and 0 <= ch < 32:
                        r["smooth"][p]["entries"][(mid, ch)] = (_nm_wrap(_nm_pick(e, "spd", 0), 16),
                                                                 _nm_wrap(_nm_pick(e, "acc", 0), 8))
        if "maestros" in c:
            for i, mo in enumerate((c["maestros"] if isinstance(c["maestros"], list) else [])[:8]):
                mo = mo if isinstance(mo, dict) else {}
                slot = r["maestros"][i]
                slot["type"] = _nm_wrap(_nm_pick(mo, "type", 0), 8)
                slot["device"] = _nm_wrap(_nm_as_int(mo["device"]), 8) if "device" in mo else i + 1
                if "channels" in mo:
                    slot["channels"] = {}
                    for co in mo["channels"] if isinstance(mo["channels"], list) else []:
                        if not isinstance(co, dict) or "ch" not in co or not 0 <= _nm_as_int(co["ch"]) < 32:
                            continue
                        slot["channels"][_nm_as_int(co["ch"])] = {"name": _nm_cut(_nm_pick(co, "name", ""), 24),
                                                                  "min": _nm_wrap(_nm_pick(co, "min", 0), 16),
                                                                  "max": _nm_wrap(_nm_pick(co, "max", 0), 16)}
        for key, tdef, portdef in (("hcrDest", "serial", "S3"), ("mp3Dest", "wcb", "S3"), ("dfpDest", "serial", "S3")):
            if key in c:
                o = c[key] if isinstance(c[key], dict) else {}
                tp = _nm_pick(o, "transport", tdef)
                t = 2 if tp == "off" else 1 if tp == "wcb" else 0
                r[key] = {"transport": t,
                          "target": _nm_cut(_nm_pick(o, "target", "2"), 6) if t == 1 else _nm_cut(_nm_pick(o, "port", portdef), 6),
                          "wcbPort": _nm_wrap(_nm_pick(o, "wcbPort", 1), 8) if t == 1 else 0}
        if "wcbNetwork" in c:
            o = c["wcbNetwork"] if isinstance(c["wcbNetwork"], dict) else {}
            net = r["wcbNetwork"]
            for k in ("macOct2", "macOct3"):
                if k in o:
                    net[k] = _nm_as_int(o[k]) & 0xFF
            net["password"] = _nm_cut(_nm_pick(o, "password", net["password"]), 40)
            net["quantity"] = _nm_wrap(_nm_pick(o, "quantity", net["quantity"]), 8)
            net["deviceId"] = _nm_wrap(_nm_pick(o, "deviceId", net["deviceId"]), 8)
            ch = _nm_pick(o, "channel", net["channel"])
            net["channel"] = 1 if ch < 1 or ch > 11 else ch
        if "wcbProfiles" in c:
            r["wcbProfiles"] = []
            for o in (c["wcbProfiles"] if isinstance(c["wcbProfiles"], list) else [])[:6]:
                o = o if isinstance(o, dict) else {}
                ch = _nm_pick(o, "channel", 1)
                r["wcbProfiles"].append({"name": _nm_cut(_nm_pick(o, "name", ""), 24),
                                         "macOct2": _nm_as_int(o.get("macOct2")) & 0xFF,
                                         "macOct3": _nm_as_int(o.get("macOct3")) & 0xFF,
                                         "password": _nm_cut(_nm_pick(o, "password", ""), 40),
                                         "quantity": _nm_wrap(_nm_pick(o, "quantity", 4), 8),
                                         "deviceId": _nm_wrap(_nm_pick(o, "deviceId", 20), 8),
                                         "channel": 1 if ch < 1 or ch > 11 else ch})
        if "wledSlots" in c:
            arr = (c["wledSlots"] if isinstance(c["wledSlots"], list) else [])[:4]
            for i in range(4):
                o = arr[i] if i < len(arr) and isinstance(arr[i], dict) else None
                r["wledSlots"][i] = {"wledID": 0, "serialPort": 0, "remoteWCB": 0, "configured": False} if o is None and \
                    i >= len(arr) else {"wledID": _nm_as_int((o or {}).get("id")) & 0xFF,
                                        "serialPort": _nm_as_int((o or {}).get("port")) & 0xFF,
                                        "remoteWCB": _nm_as_int((o or {}).get("wcb")) & 0xFF,
                                        "configured": _nm_pick(o or {}, "configured", False)}
        if "auxBaud" in c:
            o = c["auxBaud"] if isinstance(c["auxBaud"], dict) else {}
            san = lambda b, d: b if 1200 <= b <= 115200 else d          # noqa: E731

            def u32(k, cur):
                v = o.get(k)
                return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 0xFFFFFFFF else cur
            r["auxBaud"] = [san(u32(p, r["auxBaud"][i]), 9600) for i, p in enumerate(("S3", "S4", "S5"))]
            r["maestroBaud"] = san(u32("maestro", r["maestroBaud"]), 115200)
        if "serialLabels" in c:
            r["serialLabels"] = ["", "", "", ""]
            for k, v in (c["serialLabels"] if isinstance(c["serialLabels"], dict) else {}).items():
                if k in self.LBL:
                    r["serialLabels"][self.LBL.index(k)] = _nm_cut(v if isinstance(v, str) else "", 25)
        if "serialBcast" in c:
            o = c["serialBcast"] if isinstance(c["serialBcast"], dict) else {}
            for i, p in enumerate(("S3", "S4", "S5")):
                if p in o:
                    po = o[p] if isinstance(o[p], dict) else {}
                    r["bcastOut"][i] = _nm_pick(po, "out", r["bcastOut"][i])
                    r["bcastIn"][i] = _nm_pick(po, "in", r["bcastIn"][i])
        if "modeReport" in c:
            o = c["modeReport"] if isinstance(c["modeReport"], dict) else {}
            mr = r["modeReport"]
            mr["enabled"] = _nm_pick(o, "enabled", mr["enabled"])
            mr["wcb"] = _nm_wrap(_nm_pick(o, "wcb", mr["wcb"]), 8)
            if "template" in o:
                mr["tmpl"] = _nm_cut(_nm_pick(o, "template", ""), 48)
            if "cmds" in o:
                arr = o["cmds"] if isinstance(o["cmds"], list) else []
                mr["cmds"] = [_nm_cut(arr[i] if i < len(arr) and isinstance(arr[i], str) else "", 48) for i in range(3)]
        if "statsReport" in c:
            o = c["statsReport"] if isinstance(c["statsReport"], dict) else {}
            r["statsReport"] = {"enabled": _nm_pick(o, "enabled", r["statsReport"]["enabled"]),
                                "wcb": _nm_wrap(_nm_pick(o, "wcb", r["statsReport"]["wcb"]), 8)}
        return True

    def to_json(self):
        """rcConfigToJSON (rc_config.h:1211-1482), key for key."""
        r = self.c
        d = {k: r[k] for k in ("txModel", "threeAxisGimbals", "sbusOutEnabled", "wifiEnabled", "wifiSsid",
                               "wifiPassword", "maeGateMs", "boardType", "tapWindowMs", "holdMs", "switchSettleMs",
                               "chRateHz", "matrixChannel", "matrixDebounceFrames")}
        d["funcBindings"] = {"mode": r["modeSwitch"]}
        d["peerEvent"] = {"alert": r["peerAlert"], "actions": [self.action_to(a) for a in r["peerActions"]]}
        d["thresholds"] = [dict(t) for t in r["thresholds"]]
        maps = {}
        for mode in (1, 2, 3):
            for btn in range(1, 37):
                m = r["mappings"].get(f"{mode * 100 + btn}")
                if not m or (not any(acts or note for acts, note in m["t"]) and not m["exclusive"]):
                    continue
                o = {"exclusive": m["exclusive"]}
                for t, (acts, note) in enumerate(m["t"], 1):
                    if acts:
                        o[f"t{t}"] = [self.action_to(a) for a in acts]
                    if note:
                        o[f"t{t}note"] = note
                maps[f"{mode * 100 + btn}"] = o
        d["mappings"] = maps
        d["switches"] = {}
        for i, s in enumerate(r["switches"]):
            o = {"channel": s["channel"], "positions": s["positions"]}
            for p, (acts, note) in enumerate(s["t"]):
                if acts:
                    o[f"p{p}"] = [self.action_to(a) for a in acts]
                if note:
                    o[f"p{p}note"] = note
            d["switches"][self.SW[i]] = o
        d["knobs"] = {}
        for i, k in enumerate(r["knobs"]):
            o = {x: k[x] for x in ("channel", "function", "reverse", "modeAware", "modeSwitchOverride")}
            if k["smoothProfile"] != -1:
                o["smoothProfile"] = k["smoothProfile"]
            if k["easeSwitchOverride"]:
                o["easeSwitchOverride"] = True

            def outs(lst):
                res = []
                for x in lst:
                    y = {z: x[z] for z in ("target", "maestroCh", "posMin", "posMax")}
                    if x["midClosed"]:
                        y["midClosed"] = True
                    if x["releaseIdleMs"]:
                        y["releaseIdleMs"] = x["releaseIdleMs"]
                    res.append(y)
                return res
            o["outputs"] = outs(k["outputs"])
            if k["modeAware"]:
                o["outputs2"], o["outputs3"] = outs(k["outputs2"]), outs(k["outputs3"])
            d["knobs"][self.KN[i]] = o
        names = {0: "serial", 1: "wcb", 2: "off"}
        h = r["hcrDest"]
        d["hcrDest"] = {"transport": names[h["transport"]], "target": h["target"], "wcbPort": h["wcbPort"]} \
            if h["transport"] == 1 else {"transport": names[h["transport"]], "port": h["target"]}
        d["maestros"] = []
        for s in r["maestros"]:
            o = {"type": s["type"], "device": s["device"]}
            chs = [dict(ch=n, name=v["name"], min=v["min"], max=v["max"]) for n, v in sorted(s["channels"].items())
                   if v["name"] or v["min"] or v["max"]]
            if chs:
                o["channels"] = chs
            d["maestros"].append(o)
        d["wcbNetwork"] = dict(r["wcbNetwork"])
        d["wcbProfiles"] = [dict(p) for p in r["wcbProfiles"]]
        for key in ("mp3Dest", "dfpDest"):
            x = r[key]
            d[key] = {"transport": names[x["transport"]], "target": x["target"]} if x["transport"] == 1 else \
                {"transport": names[x["transport"]], "port": x["target"]}
        d["wledSlots"] = [{"id": w["wledID"], "port": w["serialPort"], "wcb": w["remoteWCB"], "configured": w["configured"]}
                          for w in r["wledSlots"]]
        d["auxBaud"] = {"S3": r["auxBaud"][0], "S4": r["auxBaud"][1], "S5": r["auxBaud"][2], "maestro": r["maestroBaud"]}
        labels = {self.LBL[i]: v for i, v in enumerate(r["serialLabels"]) if v}
        if labels:
            d["serialLabels"] = labels
        d["serialBcast"] = {p: {"out": r["bcastOut"][i], "in": r["bcastIn"][i]} for i, p in enumerate(("S3", "S4", "S5"))}
        mr = r["modeReport"]
        d["modeReport"] = {"enabled": mr["enabled"], "wcb": mr["wcb"], "template": mr["tmpl"], "cmds": list(mr["cmds"])}
        d["statsReport"] = dict(r["statsReport"])
        d["smoothProfiles"] = [{"name": p["name"], "entries": [{"mid": m, "ch": ch, "spd": v[0], "acc": v[1]}
                                                               for (m, ch), v in sorted(p["entries"].items()) if v[0] or v[1]]}
                               for p in r["smooth"]]
        return d

    def text(self):
        return json.dumps(self.to_json(), separators=(",", ":"), ensure_ascii=False)

    def load_flash(self):
        self.c = self.defaults()
        self.merge(json.loads(self.flash))

    # ------------------------------------------------------------ boot, identity, side effects
    def boot(self):
        """setup(): the pin profile and bauds applied, WCB_Client given the mesh identity (its own copy)."""
        net = self.c["wcbNetwork"]
        self.applied = {"board": self.c["boardType"], "aux": list(self.c["auxBaud"]), "mae": self.c["maestroBaud"],
                        "sbusOut": self.c["sbusOutEnabled"]}
        self.ident = {"oct2": net["macOct2"], "oct3": net["macOct3"], "id": net["deviceId"], "q": net["quantity"],
                      "pw": net["password"]}

    def labels(self):
        """rcSerialLabel for WDP ports 1-4 (rc_config.h:1940-1958)."""
        out = {}
        for i, key in enumerate(("S3", "S4", "S5")):
            v = self.c["serialLabels"][i]
            if not v:
                for dk, name in (("hcrDest", "HCR"), ("mp3Dest", "MP3"), ("dfpDest", "DFPlayer")):
                    x = self.c[dk]
                    if x["transport"] == 0 and x["target"] == key:
                        v = name
                        break
            if v:
                out[i + 1] = v
        out[4] = self.c["serialLabels"][3] or "Maestro"
        return out

    def side_effects(self):
        """applyConfigSideEffects (NaviCore.ino:3321-3345) -> the lines it prints, or None when boardType changed."""
        if self.c["boardType"] != self.applied["board"]:
            return None
        out = []
        if self.c["maestroBaud"] != self.applied["mae"]:
            self.applied["mae"] = self.c["maestroBaud"]
            out.append(f"[Serial2] Local Maestro re-open @ {self.c['maestroBaud']} baud  TX=GPIO6")
        for i, p in enumerate(("S3", "S4", "S5")):
            if self.c["auxBaud"][i] != self.applied["aux"][i]:
                self.applied["aux"][i] = self.c["auxBaud"][i]
                out.append(f"[AUX] {p} re-open @ {self.c['auxBaud'][i]} baud" + (" (hw UART0)" if p == "S3" else ""))
        if self.c["sbusOutEnabled"] != self.applied["sbusOut"]:
            self.applied["sbusOut"] = self.c["sbusOutEnabled"]
            out.append("[SBUS] OUT enabled — re-emit on GPIO5 (100k 8E2 inverted)" if self.c["sbusOutEnabled"]
                       else "[SBUS] OUT disabled (passthrough off — no CPU cost)")
        return out

    def save(self):
        self.flash = self.text()
        return f"RC config saved to LittleFS ({len(self.flash.encode('utf-8'))} bytes)."

    # ------------------------------------------------------------ parsing, as ArduinoJson would
    @staticmethod
    def parse_header(line):
        """(object or None, error code or None) for the filtered header parse (NaviCore.ino:3809-3832): a value the
        filter skips is not escape-checked (skipQuotedString), so an invalid escape passes here and fails the full
        parse; an unterminated line is IncompleteInput, anything else malformed InvalidInput."""
        try:
            return json.loads(line), None
        except json.JSONDecodeError as e:
            lenient = re.sub(r'\\([^"\\/bfnrtu])', r"\1", line)
            try:
                return json.loads(lenient), None
            except json.JSONDecodeError as e2:
                incomplete = e2.pos >= len(lenient) or "Unterminated string" in e2.msg
                return None, "IncompleteInput" if incomplete else "InvalidInput"

    # ------------------------------------------------------------ NaviCore's console
    def out(self, *lines):
        return list(lines)

    def pong(self):
        return f'{{"type":"PONG","version":"{self.FW}"}}'

    def rc_trig(self, mode, btn, tap):
        return f'{{"sys":1,"type":"rc_trig","id":{self.c["wcbNetwork"]["deviceId"]},"mode":{mode},"btn":{btn},"tap":{tap}}}'

    def dlog(self, bit, text):
        return [text] if self.flags & bit else []

    def dispatch(self, a):
        """rcExecuteActionNow (NaviCore.ino:2035-2112) -> its console lines; W1 gets a unicast to board 1."""
        t, out = a["type"], []
        if t == "wcb_unicast":
            b = int(a["target"]) if a["target"].isdigit() else 0
            if 1 <= b <= 20:
                out += self.dlog(0x02, f"[DISPATCH] WCB→{b}  {a['cmd']}")
                if b == 1 and self.w1 is not None and a["cmd"].startswith(";S0,"):
                    self.w1._append(a["cmd"][4:])
        elif t == "wcb_broadcast":
            out += self.dlog(0x02, f"[DISPATCH] WCB broadcast  {a['cmd']}")
        elif t == "maestro":
            i = int(a["target"]) if a["target"].isdigit() else 0
            out += [f"WARN: Maestro action with invalid ID {i} (target='{a['target']}')"] if not 1 <= i <= 8 else \
                self.dlog(0x01, f"[DISPATCH] Maestro {i}  {a['cmd']}")
        elif t == "serial":
            lab = {"S3": "Serial 1", "S4": "Serial 2", "S5": "Serial 3"}.get(a["target"], a["target"])
            out += self.dlog(0x20, f"[DISPATCH] Serial TX [{lab}]  {a['cmd']}")
        elif t == "hcr":
            d = self.c["hcrDest"]
            if d["transport"] == 2:
                out += self.dlog(0x08, "[DISPATCH] HCR is disabled in config — action skipped")
            elif not _nm_hcr_ok(a["fn"], a["chan"], a["track"]):
                out += self.dlog(0x08, f"[DISPATCH] HCR-{'WCB' if d['transport'] == 1 else 'Serial'}: bad/unsupported "
                                       f"fn={a['fn']} chan={a['chan']} track={a['track']} — skipped")
        elif t in ("mp3", "dfplayer"):
            d = self.c["mp3Dest" if t == "mp3" else "dfpDest"]
            name, top = ("MP3", 8) if t == "mp3" else ("DFP", 18)
            if d["transport"] == 2:
                out += self.dlog(0x10 if t == "mp3" else 0x40, "[DISPATCH] MP3 Trigger is disabled in config — action "
                                 "skipped" if t == "mp3" else "[DISPATCH] DFPlayer is disabled in config — action skipped")
            elif not 1 <= a["fn"] <= top:
                out += self.dlog(0x10 if t == "mp3" else 0x40, f"[DISPATCH] {name}: bad fn={a['fn']} — skipped")
        elif t == "wled":
            s = a["cmd"].lstrip(" \t")
            s = s[1:] if s.startswith(";") else s
            if not s[:1] in ("L", "l"):
                out += self.dlog(0x04, f"[DISPATCH] WLED: '{a['cmd']}' is not a ;L command — skipped")
        return out

    def script(self, text, n):
        text = text[:98304]                        # handleSerialInput's cap: the rest of the line is dropped (:4291)
        self.received.append(text)
        for rx in self.FORBIDDEN:
            if rx.search(text):
                raise AssertionError(f"a test sent NaviCore a forbidden command: {text[:40]}")
        if text == "WCB_WEBTOOL_CONFIG_PULL":
            return self.backup()
        if text.startswith("?"):
            return self.cli(text)
        if text.startswith("#"):
            return self.hash_cmd(text)
        if text.startswith("{"):
            return self.json_line(text)
        return []

    def backup(self):
        i = self.ident
        return ["", "*** WCB Configuration Backup", "", "?HW,32", f"?MAC,2,{i['oct2']:02X}", f"?MAC,3,{i['oct3']:02X}",
                f"?WCB,{i['id']}", "?RELAY,1", "?ALIAS,NaviCore", f"?WCBQ,{i['q']}",
                f"?EPASS,{self.c['wcbNetwork']['password']}", "?CMDCHAR,;", "--------- End of Backup ---------", ""]

    def version_lines(self):
        return [f"Software Version: {self.FW}", "End of Version"]

    def wdp_dump(self):
        return [f"[WDP:N=20,CLIENT=0,ALIAS=NaviCore,HW=32,HWREV=,FW={self.FW},CAP=0000,CTRL=0,CAPTAGS=,MAESTRO=-,AGE=0,"
                f"SEEN=1,PEER=3]",
                "[WDP:N=1,CLIENT=0,ALIAS=W1,HW=24,HWREV=,FW=6.2.1_TEST,CAP=0001,CTRL=20,CAPTAGS=,MAESTRO=-,AGE=3,SEEN=1,PEER=1]",
                "[WDP:N=2,CLIENT=0,ALIAS=W2,HW=24,HWREV=,FW=6.2.1_TEST,CAP=0001,CTRL=20,CAPTAGS=,MAESTRO=-,AGE=5,SEEN=1,PEER=2]",
                "[WDPCFG:EN=1,AUTOJOIN=1,PEERS=2]", "[WDP:END,count=2]"]

    def cli(self, text):
        if text.startswith("?OTALOCAL,"):
            return [f"Chip:     ESP32-S3", f"Firmware: {self.FW}", "Running:  app0", "Next:     app1", "Session:  idle"]
        low = text[1:].lower()
        if low == "backup":
            return self.backup()
        if low == "version":
            return self.version_lines()
        if low.startswith("wdp,"):
            return self.wdp_dump() if low == "wdp,dump" else [f"Unknown command: {text}"]
        if low.startswith("mgmt,"):
            n = len(text) - 6
            return [f"[mgmt] line too long ({n} B) - dropped"] if n >= 400 else []
        if low.startswith("forget"):
            arg = text[8:].strip() if len(text) > 8 else ""
            n = int(arg) if arg.isdigit() else 0
            return [f"[WCB] WCB {n} is not a learned peer (nothing to forget)"] if 1 <= n <= 20 else \
                ["[WCB] usage: ?FORGET,<id 1-20>  or  ?FORGET,ALL"]
        if low.startswith("rec"):
            if low == "rec,ls":
                return ['[CLIPFS]{"total":12000000,"used":4096}', "[REC] clips:", "[CLIPLIST:BEGIN]"] + \
                    [f'[CLIPITEM]{{"name":"{c}","bytes":296,"dur":1000,"n":2}}' for c in self.clips] + ["[CLIPLIST:END]"]
            return ["[REC] state=idle  events=0/1000  dur=0ms  drops=0  buf=ok"]
        return [f"Unknown command: {text}"]

    def hash_cmd(self, text):
        if len(text) < 3 or text[1] not in "Ll":
            return []
        fn = (ord(text[2]) - 48) * 10 + (ord(text[3]) - 48) if len(text) >= 4 else ord(text[2]) - 48
        if fn == 12:
            return [f"Mode={self.mode}  matrixBtn=0  matrixVal={self.channels[self.c['matrixChannel'] - 1]}"]
        if fn == 9:
            rows = [" ".join(f"{v:4d}" for v in self.channels[i:i + 8]) for i in range(0, 24, 8)]
            return ["---- SBUS STATE ----", "  variant=SBUS-24 (24 ch, 36-byte frame)",
                    "  frames=1000  fps=111  ageMs=4  lost=no  failsafe=no"] + \
                [f"  CH{i * 8 + 1}-{i * 8 + 8}:   {r}" for i, r in enumerate(rows)]
        if fn == 1:
            return ["NaviCore — NaviCore v2"]
        return [f"Unknown #L code {fn}. Valid: 1,2,9,10,11,12,13,20,21"]

    def monitor_loop(self):
        while self.monitor:
            chans = ",".join(str(v) for v in self.channels)
            ms = self.c["modeSwitch"]
            mode_ch = self.c["switches"][ms]["channel"] if 0 <= ms < 10 else 0
            self.nav._append(f'{{"type":"PWM_UPDATE","matrixCh":{self.c["matrixChannel"]},"modeCh":{mode_ch},'
                             f'"matrixVal":992,"modeVal":172,"btn":0,"mode":{self.mode},"sbus":{{"ok":true,"fps":111,'
                             f'"frames":1000,"ageMs":4,"lost":false,"failsafe":false,"chCount":24,"frameLen":36,'
                             f'"channels":[{chans}]}}}}')
            time.sleep(0.05)

    def json_line(self, line):
        obj, err = self.parse_header(line)
        if err:
            return [f'{{"type":"ERROR","msg":"JSON parse failed ({err})","rxLen":{len(line.encode("utf-8"))}}}']
        t = obj.get("type") if isinstance(obj, dict) and isinstance(obj.get("type"), str) else ""
        ack = '{"type":"ACK","ok":true}'
        if t in ("PING", "ping"):
            self.calib = False
            return [self.pong()]
        if t == "GET_CONFIG":
            return ['{"type":"CONFIG","data":' + self.text() + "}"]
        if t == "GET_CMDLIB":
            lib = self.cmdlib or '{"boards":[],"enums":{}}'
            return [f'{{"type":"CMDLIB","size":{len(lib.encode())},"hash":{self._fnv(lib)},"data":{lib}}}']
        if t == "GET_CMDLIB_META":
            lib = self.cmdlib or ""
            return [f'{{"type":"CMDLIB_META","size":{len(lib.encode())},"hash":{self._fnv(lib) if lib else 0}}}']
        if t == "SET_CMDLIB":
            k = line.find('"data":')
            lib = None
            if k >= 0:
                s = k + 7
                while s < len(line) and line[s].isspace():
                    s += 1
                if s < len(line) and line[s] in "{[":
                    depth, in_str, esc = 0, False, False
                    for i in range(s, len(line)):
                        ch = line[i]
                        if esc:
                            esc = False
                        elif in_str:
                            esc, in_str = ch == "\\", in_str and ch != '"'
                        elif ch == '"':
                            in_str = True
                        elif ch == line[s]:
                            depth += 1
                        elif ch == ("}" if line[s] == "{" else "]"):
                            depth -= 1
                            if depth == 0:
                                lib = line[s:i + 1].strip()
                                break
            if lib:
                self.cmdlib = lib
                return [f'{{"type":"ACK","of":"SET_CMDLIB","ok":true,"size":{len(lib.encode())},"hash":{self._fnv(lib)}}}']
            return ['{"type":"ACK","of":"SET_CMDLIB","ok":false,"size":0,"hash":0}']
        if t == "SET_CONFIG":
            try:
                full = json.loads(line)
            except ValueError:
                return ['{"type":"ACK","of":"SET_CONFIG","ok":false,"msg":"parse failed"}']
            sid = full["saveId"] if isinstance(full.get("saveId"), int) else 0
            if "data" not in full:
                return [f'{{"type":"ACK","of":"SET_CONFIG","ok":false,"msg":"missing data","saveId":{sid}}}']
            self.merge(full["data"] if isinstance(full["data"], dict) else None)
            out = [self.save()]
            fx = self.side_effects()
            out += [INFO_LINE] if fx is None else fx
            return out + [f'{{"type":"ACK","of":"SET_CONFIG","ok":true,"saveId":{sid}}}']
        if t == "START_MONITOR":
            if not self.monitor:
                self.monitor = True
                threading.Thread(target=self.monitor_loop, daemon=True).start()
            return [ack]
        if t == "STOP_MONITOR":
            self.monitor = self.calib = False
            time.sleep(0.06)                       # let the loop see the flag before the ACK is written
            return [ack]
        if t == "CALIB":
            self.calib = _nm_pick(obj, "on", False)
            return [f"[CALIB] action dispatch {'SUPPRESSED (calibrating)' if self.calib else 'resumed'}", ack]
        if t == "RESET_DEFAULTS":
            self.c = self.defaults()
            return [ack]
        if t == "TEST_ACTION":
            a = self.action_from(obj.get("action")) if isinstance(obj.get("action"), dict) else None
            if a is None or (a["type"] == "wcb_unicast" and not (a["target"].isdigit() and 1 <= int(a["target"]) <= 20)):
                return ['{"type":"ACK","of":"TEST_ACTION","ok":false}']
            return self.dispatch(a) + ['{"type":"ACK","of":"TEST_ACTION","ok":true}']
        if t == "REBOOT":
            self.monitor = self.calib = False
            self.flags = 0
            self.load_flash()
            self.boot()
            self.nav.later(0.2, "<<reopened COMFAKE>>",
                           "Reset reason: 3 - Software restart (incl. boot-guard retry)  (RTC codes core0=3 [SW system] "
                           "core1=3 [SW CPU])", f"[NaviCore] Firmware {self.FW} — setup complete.")
            return ['{"type":"ACK","ok":true,"msg":"rebooting"}']
        if t == "TRIGGER":
            mode, btn, tap = _nm_pick(obj, "mode", 1), _nm_pick(obj, "btn", 0), _nm_pick(obj, "tap", 1)
            if not (1 <= btn <= 36 and 1 <= mode <= 3 and 1 <= tap <= 4):
                return ['{"type":"ACK","ok":false,"msg":"bad mode/btn/tap"}']
            m = self.c["mappings"].get(f"{mode * 100 + btn}")
            if m and any(acts for acts, _ in m["t"]):
                raise AssertionError(f"a test TRIGGERed mapped slot {mode * 100 + btn}")
            return [f"[TRIGGER] mode={mode} btn={btn} tap={tap}", self.rc_trig(mode, btn, tap), ack]
        if t == "WCB_SEND":
            tgt, cmd = _nm_pick(obj, "target", 0), _nm_pick(obj, "cmd", "")
            if tgt == 0:
                if len(cmd) > 187:
                    return [f"[WCB_Client] broadcast: command too long ({len(cmd)} > 187 chars) — fragmentation is "
                            f"unicast-only. send() it to each board instead.",
                            '{"type":"ACK","ok":false,"msg":"broadcast refused by WCB_Client"}']
                raise AssertionError("a test broadcast a command over NaviCore's WCB_SEND")
            if 1 <= tgt <= 20:
                raise AssertionError("a test unicast over NaviCore's WCB_SEND")
            return [f'{{"type":"ACK","ok":false,"msg":"target {tgt} out of range (0=broadcast, 1-20=unicast)"}}']
        if t == "FORGET_PEER":
            i = _nm_pick(obj, "id", 0)
            if i != self.c["wcbNetwork"]["deviceId"]:
                raise AssertionError(f"a test sent FORGET_PEER {i}")
            return [f"[WCB] WCB {i} is not a learned peer (nothing to forget)",
                    f'{{"type":"ACK","of":"FORGET_PEER","ok":true,"id":{i}}}']
        if t == "SET_DEBUG_FLAGS":
            self.flags = _nm_pick(obj, "flags", 0)
            return [f"[DBG] flags=0x{self.flags:02X}", ack]
        if t in ("GET_WCB_SEQ", "GET_WCB_SEQVAL"):
            kind = t[4:]
            b = _nm_pick(obj, "wcb", 0)
            if not 1 <= b <= 20:
                return [f'{{"sys":1,"type":"{kind}","ok":false,"wcb":0,"msg":"wcb out of range"}}']
            if kind == "WCB_SEQVAL" and not _nm_pick(obj, "key", ""):
                return [f'{{"sys":1,"type":"{kind}","ok":false,"wcb":{b},"msg":"key required"}}']
            reply = f'{{"sys":1,"type":"WCB_SEQ","ok":true,"wcb":{b},"hash":123,"names":["intro"]}}' if kind == "WCB_SEQ" \
                else f'{{"sys":1,"type":"WCB_SEQVAL","ok":true,"wcb":{b},"key":"{obj["key"]}","status":1,"value":""}}'
            self.nav.later(0.2, reply)
            return []
        if t == "GET_WCB_STATUS":
            return ['{"type":"WCB_STATUS","quantity":1,"self":20,"online":[1,1],"known":[1,1],"clients":[0,0],'
                    '"temporary":[0,0],"aliases":["W1","W2"],"portLabels":[["","","","",""],["","","","",""]],'
                    '"seqHash":[1,2]}']
        return ['{"type":"ERROR","msg":"unknown type"}']

    @staticmethod
    def _fnv(s):
        from hil.navicore import fnv1a32
        return fnv1a32(s)

    # ------------------------------------------------------------ W1's console
    def w1_script(self, text, n):
        if text.startswith(";S0,"):
            return [text[4:]]
        if text == "?WDP,DUMP":
            rows = [f"[WDP:N=20,CLIENT=1,ALIAS=NaviCore,HW=32,HWREV=NaviCore v2,FW={self.FW},CAP=0000,CTRL=0,CAPTAGS=,"
                    f"MAESTRO=1,AGE=4,SEEN=1,PEER=0]"]
            rows += [f"[WDPIF:N=20,S={p},DEV={v}]" for p, v in sorted(self.labels().items())]
            return rows + ["[WDP:END,count=2]"]
        m = re.match(r";W20,(.*)$", text)
        if not m:
            return []
        body = m.group(1)
        if body.startswith("{"):
            obj, err = self.parse_header(body)
            t = obj.get("type") if isinstance(obj, dict) else None
            if t == "PING":
                self.w1.later(0.3, f'{{"sys":1,"type":"rc_hb","id":20,"fw":"{self.FW}","up":1000,"mode":{self.mode},'
                                   f'"model":{self.c["txModel"]},"sbusFps":111,"sbusAge":4,"sbusLost":0,"sbusFail":0}}')
                return [f'{{"sys":1,"type":"PONG","id":20,"version":"{self.FW}","model":{self.c["txModel"]},"mode":{self.mode}}}']
            if t == "SET_CONFIG":
                self.nav._append("[RC] SET_CONFIG → deferred to main loop")
                if not isinstance(obj.get("data"), dict):
                    self.nav._append("[RC] reassembled SET_CONFIG missing 'data' object")
                else:
                    raise AssertionError("a test bridged a real SET_CONFIG")
            return []
        if body.startswith("?"):
            lines = self.cli(body)
            for x in lines:
                self.nav._append(x)
            if self.c["wcbNetwork"]["password"] == self.ident["pw"]:
                return [f"[TERM:20]{x}" for x in lines if x]
            return []
        return []


INFO_LINE = '{"type":"INFO","msg":"boardType changed — reboot to apply the new pin profile"}'
NCCFG_SHOULD = {"nccfg.string_truncation_utf8", "nccfg.hold_exceeds_tap_window", "nccfg.dest_null_hazard",
                "nccfg.mesh_creds_live_split", "nccfg.reset_defaults_keeps_identity"}


def t_nccfg_suite_against_model(tmp):
    """Every nccfg test in s40 run whole, through the runner, against NaviModel - a port of the config handling of the
    NaviCore firmware the suite was written against - and a W1 console that relays to it: each normal test passes, each
    (should) test fails on today's behaviour, the hook tests skip (no hook build), the model ends every test with the
    config it started with (the guard restores it), and session.log carries no credential. Then
    nccfg.mesh_creds_live_split, the RESET_DEFAULTS test restored without a restart, alone against two models whose SBUS
    input the defaults would act on: CH7 inside a default band, and a mode switch other than SE on CH12. It skips
    without sending RESET_DEFAULTS."""
    saved = list(runner.REGISTRY)
    try:
        # t_nc_guard_bench_test imported the suite already, and an earlier case left its own entries here: a
        # fresh import into an empty registry collects exactly the suite's tests, whatever ran before
        runner.REGISTRY[:] = []
        sys.modules.pop("suites.s40_navicore_config", None)
        import suites.s40_navicore_config  # noqa: F401
        mine = [dict(t) for t in runner.REGISTRY if t["id"].startswith("nccfg.")]
    finally:
        runner.REGISTRY[:] = saved
    model = NaviModel()
    orig = model.flash
    b = tmp.bench({"wcb1": {"port": "COMW1", "kind": "wcb", "wcb": 1},
                   "navicore": {"port": "COMNAV", "kind": "navicore"}, "sbus": {"port": "COMS", "kind": "sbus"}})
    b.cfg["opt_in"] = ["navicore_reboot", "navicore_fault"]
    nav, w1 = FakeNaviDev(model.script, "navicore"), FakeNaviDev(model.w1_script, "wcb1")
    model.nav, model.w1 = nav, w1
    nav.log = w1.log = b.log
    b.dev = lambda name: {"navicore": nav, "wcb1": w1}[name]
    tests = []
    for t in mine:
        t["needs"], t["links"], t["drives"], t["_drives"] = [], [], [], set()
        tests.append(t)
    saved_g = _fast_guard()
    try:
        ck = new_run(b, tests)
    finally:
        _slow_guard(saved_g)
    res = {r["id"]: r for r in ck.data["results"]}
    bad = []
    for tid, r in res.items():
        want = "SKIP" if "hook_" in tid else "FAIL" if tid in NCCFG_SHOULD else "PASS"
        if r["status"] != want:
            bad.append(f"{tid}: {r['status']} (expected {want}) {r['detail'][:300]}")
    assert len(res) == len(mine) >= 35, (len(res), len(mine))
    assert not bad, "\n".join(bad)
    assert model.flash == orig and model.text() == orig, "the model's config was not left as found"
    log = read(os.path.join(ck.out_dir, "session.log"))
    assert not any(s in log for s in SECRETS), "a credential reached session.log"
    b.close()
    split = next(t for t in tests if t["id"] == "nccfg.mesh_creds_live_split")
    for want, prep in (("CH7 reads 1811", lambda m: m.channels.__setitem__(6, 1811)),
                       ("move the mode off 2", lambda m: (m.c.update(modeSwitch=0), setattr(m, "mode", 2)))):
        m2 = NaviModel()
        prep(m2)
        b2 = tmp.bench({"wcb1": {"port": "COMW1", "kind": "wcb", "wcb": 1},
                        "navicore": {"port": "COMNAV", "kind": "navicore"}})
        nav2, w12 = FakeNaviDev(m2.script, "navicore"), FakeNaviDev(m2.w1_script, "wcb1")
        m2.nav, m2.w1 = nav2, w12
        nav2.log = w12.log = b2.log
        b2.dev = lambda name, d={"navicore": nav2, "wcb1": w12}: d[name]
        saved_g = _fast_guard()
        try:
            ck2 = new_run(b2, [split])
        finally:
            _slow_guard(saved_g)
        r = ck2.data["results"][0]
        assert r["status"] == "SKIP" and want in r["detail"], (want, r["status"], r["detail"][:200])
        assert not any('"RESET_DEFAULTS"' in x for x in nav2.sent), "RESET_DEFAULTS sent despite the SBUS check"
        b2.close()


# ---------------------------------------------------------------------------- NaviCore images (INF4, hil/ncflash.py)
NC_VERSION = "v9.9.9_111111ZSEP26"
NC_PARTITIONS = ("# Name, Type, SubType, Offset, Size, Flags\n"                   # NaviCore partitions.csv:14-21
                 "nvs, data, nvs, 0x9000, 0x5000,\notadata, data, ota, 0xe000, 0x2000,\n"
                 "app0, app, ota_0, 0x10000, 0x1e0000,\napp1, app, ota_1, 0x1f0000, 0x1e0000,\n"
                 "spiffs, data, spiffs, 0x3d0000, 0x20000,\ncoredump, data, coredump, 0x3f0000, 0x10000,\n"
                 "clips, data, spiffs, 0x400000, 0xc00000,\n")
NC_FQBN_DOC = ("arduino-cli compile --fqbn \"esp32:esp32:esp32s3:USBMode=hwcdc,CDCOnBoot=cdc,PartitionScheme=custom,"
               "FlashSize=16M,PSRAM=opi\" NaviCore.ino\n")


def _nc_image(version=NC_VERSION, elf=b"\x7fELF selftest", chip_id=9, body=6000, zero_sha=False,
              desc_magic=0xABCD5432):
    """A small ESP32-S3 app image in esptool's layout -> (image, elf): the 24-byte header (chip id at 12,
    hash_appended at 23); two segments, the first holding esp_app_desc_t at file offset 0x20 with the ELF's SHA-256 at
    0xB0 and a FW_VERSION string after it; the checksum byte ending a 16-byte block; the SHA-256 of all that."""
    import hashlib
    import struct
    desc = bytearray(0x100)
    struct.pack_into("<I", desc, 0, desc_magic)
    if not zero_sha:
        desc[0x90:0xB0] = hashlib.sha256(elf).digest()
    seg0 = bytes(desc) + b"FW:" + version.encode() + b"\0"
    seg0 += b"\0" * (-len(seg0) % 4)
    seg1 = bytes((i * 7 + 3) & 0xFF for i in range(body))
    seg1 += b"\0" * (-len(seg1) % 4)
    img = bytearray(struct.pack("<BBBBIB3sHBHH4sB", 0xE9, 2, 2, 0x4F, 0x40376260, 0xEE, b"\0\0\0", chip_id, 0, 0,
                                0xFFFF, b"\0" * 4, 1))
    csum = 0xEF
    for addr, data in ((0x3C0E0020, seg0), (0x42000020, seg1)):
        img += struct.pack("<II", addr, len(data)) + data
        for b in data:
            csum ^= b
    img += b"\0" * (15 - len(img) % 16) + bytes([csum])
    img += hashlib.sha256(bytes(img)).digest()
    return bytes(img), elf


def _nc_folder(root, name, image, elf, csv=NC_PARTITIONS):
    """A build folder as arduino-cli leaves one: the image, its ELF and the partitions.csv copy."""
    folder = os.path.join(root, name)
    os.makedirs(folder, exist_ok=True)
    for fn, data in (("NaviCore.ino.bin", image), ("NaviCore.ino.elf", elf), ("partitions.csv", csv.encode())):
        if data is not None:
            with open(os.path.join(folder, fn), "wb") as f:
                f.write(data)
    return folder


def _nc_otadata():
    """The esp32 core's boot_app0.bin, byte for byte: sequence 1 and its CRC in the first sector, sequence 0 (whose CRC
    0xFFFFFFFF happens to be right) in the second, everything else erased."""
    import struct
    import zlib
    seq = struct.pack("<I", 1)
    first = seq + b"\xFF" * 24 + struct.pack("<I", zlib.crc32(seq, 0xFFFFFFFF))
    return first + b"\xFF" * (0x1000 - len(first)) + b"\0\0\0\0" + b"\xFF" * (0x1000 - 4)


def t_ncflash_image_check(tmp):
    """check_image (hil/ncflash.py, INF4) on synthetic ESP32-S3 images: a good one gives its version, its ELF SHA-256
    (the ELF beside it hashing to the bytes at 0xB0) and the slot from its folder's partitions.csv; every defect is
    named - the magic, the chip id, a corrupt segment (checksum), the appended SHA-256, trailing bytes, an ELF of
    another build, a zero ELF SHA, no app descriptor, no or two version strings, too big for the slot (given or from
    the csv), a BUILD.json describing another image. Also the tables the recovery trusts: partitions.csv (NaviCore's
    rows), check_layout, and check_otadata on boot_app0.bin's bytes."""
    import hashlib
    import struct
    import zlib
    from hil import ncflash as F
    img, elf = _nc_image()
    good = _nc_folder(tmp.root, "good", img, elf)
    info = F.check_image(good)
    assert info["version"] == NC_VERSION and info["elf_sha"] == hashlib.sha256(elf).hexdigest(), info
    assert info["elf_checked"] and info["slot"] == 0x1E0000 and info["size"] == len(img) and info["data"] == img, info
    assert F.check_image(os.path.join(good, "NaviCore.ino.bin"))["folder"] == os.path.normpath(good)

    def bad(name, data, want, elf_bytes=elf, csv=NC_PARTITIONS, slot=None):
        folder = _nc_folder(tmp.root, name, data, elf_bytes, csv)
        e = _raises(lambda: F.check_image(folder, slot=slot), F.ImageError)
        assert want in str(e), (name, str(e))
    magic = bytearray(img)
    magic[0] = 0xE8
    bad("magic", bytes(magic), "magic byte 0xE8")
    bad("chip", _nc_image(chip_id=0)[0], "chip id 0, not 9")
    seg = bytearray(img)
    seg[0x200] ^= 0x01
    bad("segment", bytes(seg), "checksum byte")
    tail = bytearray(img)
    tail[-1] ^= 0x01
    bad("sha", bytes(tail), "appended SHA-256 does not match")
    bad("trailing", img + b"\0" * 16, "+16 bytes after the image's end")
    bad("elf", img, "are not one build", elf_bytes=b"another build")
    bad("zero", _nc_image(zero_sha=True)[0], "all zero")
    bad("desc", _nc_image(desc_magic=0)[0], "no app descriptor")
    bad("noversion", _nc_image(version="none")[0], "no FW_VERSION string")
    bad("two", _nc_image(version=NC_VERSION + "\0v1.0.0_010101ZJAN26")[0], "2 version strings")
    bad("slot", img, f"{len(img)} B does not fit the 4096 B OTA slot", slot=4096)
    bad("slotcsv", img, "does not fit the 4096 B OTA slot", csv=NC_PARTITIONS.replace("0x1e0000", "0x1000"))
    folder = _nc_folder(tmp.root, "manifest", img, elf)
    with open(os.path.join(folder, "BUILD.json"), "w", encoding="utf-8") as f:
        json.dump({"image": {"elf_sha": "ab" * 32}}, f)
    assert "records ELF abababababababab" in str(_raises(lambda: F.check_image(folder), F.ImageError))
    parts = F.partitions(NC_PARTITIONS)
    assert F.ota_slot(parts) == 0x1E0000 and parts["app0"]["offset"] == 0x10000, parts
    assert (parts["otadata"]["offset"], parts["otadata"]["size"], parts["clips"]["size"]) == (0xE000, 0x2000, 0xC00000)
    F.check_layout(NC_PARTITIONS)
    _raises(lambda: F.check_layout(NC_PARTITIONS.replace("0xe000", "0xd000")), ValueError)
    ota = _nc_otadata()
    F.check_otadata(ota)
    app1 = bytearray(ota)                                    # sequence 2 in the first sector would boot app1
    app1[0:4], app1[28:32] = b"\x02\0\0\0", struct.pack("<I", zlib.crc32(b"\x02\0\0\0", 0xFFFFFFFF))
    for broken in (ota[:4096], bytes(app1), b"\x09" + ota[1:], b"\xFF" * 0x2000):
        _raises(lambda: F.check_otadata(broken), ValueError)


def t_ncflash_libs(tmp):
    """The library pre-check: compare_tree reads only src/ and library.properties, lists a CRLF-only difference as
    eol_only (still the same), and names a real difference and a file on one side only; check_libs pairs the
    sketchbook's WCB_Client and WcbCmd with the WCBClient and WcbCmd repos and never writes either; libs_line says so."""
    from hil import ncflash as F
    gh, sb = os.path.join(tmp.root, "gh"), os.path.join(tmp.root, "sb")

    def lib(root, files):
        for rel, text in files.items():
            p = os.path.join(root, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(text)
    base = {"src/a.h": b"int a;\n", "src/sub/b.cpp": b"int b;\n", "library.properties": b"name=X\nversion=1.2.3\n",
            "examples/e.ino": b"one\n"}
    lib(os.path.join(gh, "WCBClient"), base)
    lib(os.path.join(sb, "libraries", "WCB_Client"), dict(base, **{"examples/e.ino": b"two\n"}))
    lib(os.path.join(gh, "WcbCmd"), base)
    lib(os.path.join(sb, "libraries", "WcbCmd"), dict(base, **{"src/a.h": b"int a;\r\n"}))
    libs = F.check_libs(sketchbook=sb, github=gh)
    assert libs["WCB_Client"]["same"] and not libs["WCB_Client"]["eol_only"], libs["WCB_Client"]   # examples ignored
    assert libs["WcbCmd"]["same"] and libs["WcbCmd"]["eol_only"] == ["src/a.h"], libs["WcbCmd"]
    assert libs["WCB_Client"]["version"] == "1.2.3" and libs["WCB_Client"]["repo_state"]["head"] is None
    line = F.libs_line(libs)
    assert line.startswith("WCB_Client = WCBClient 1.2.3 (not recorded)") and "1 files in other line endings" in line, line
    lib(os.path.join(sb, "libraries", "WcbCmd"), {"src/sub/b.cpp": b"int c;\n", "src/extra.h": b"x\n"})
    os.remove(os.path.join(gh, "WCBClient", "library.properties"))
    libs = F.check_libs(sketchbook=sb, github=gh)
    assert not libs["WcbCmd"]["same"] and libs["WcbCmd"]["differ"] == ["src/sub/b.cpp"], libs["WcbCmd"]
    assert libs["WcbCmd"]["only_sketchbook"] == ["src/extra.h"] and libs["WCB_Client"]["only_sketchbook"] == \
        ["library.properties"], libs
    line = F.libs_line(libs)
    assert "WcbCmd DIFFERS from WcbCmd 1.2.3: src/sub/b.cpp, src/extra.h (sketchbook only)" in line, line
    shutil.rmtree(os.path.join(gh, "WcbCmd"))
    assert F.check_libs(sketchbook=sb, github=gh)["WcbCmd"]["missing"] == [os.path.join(gh, "WcbCmd")]
    assert F.tree_line({"head": "a" * 40, "short": "aaaaaaa", "dirty": True, "files": ["x", "y"], "diff": "0123"}) == \
        "`aaaaaaa` + 2 dirty files (diff 0123)"


def t_ncflash_build(tmp):
    """build() against a scripted arduino-cli (ncflash._run_cli) and fixed tree states: the command line (NaviCore's
    FQBN, --build-path the folder, --jobs, the hooks property only with hooks=True, never an upload or a port), the
    BUILD.json it writes (commit and dirty flag, libraries, the image check), and every refusal before or after the
    compile - an FQBN NaviCore/CLAUDE.md no longer names, library drift (allowed and recorded with allow_drift), a
    folder that already holds a build (rebuild=True recompiles it), a tree that changed during the compile, a failed
    compile (only its error lines quoted), a hooks property the build did not apply, and a library taken from
    somewhere else."""
    from hil import ncflash as F
    gh, sb, builds = (os.path.join(tmp.root, n) for n in ("gh", "sb", "builds"))
    nav = os.path.join(gh, "NaviCore")
    os.makedirs(nav)
    for fn, text in (("NaviCore.ino", "void setup(){}\n"), ("CLAUDE.md", NC_FQBN_DOC), ("partitions.csv", NC_PARTITIONS),
                     ("fw_version.h", '#define FW_VERSION_BASE  "v9.9.9"\n#define FW_VERSION_DTG   "111111ZSEP26"\n')):
        with open(os.path.join(nav, fn), "w", encoding="utf-8") as f:
            f.write(text)
    for lib, repo in (("WCB_Client", "WCBClient"), ("WcbCmd", "WcbCmd")):
        for root in (os.path.join(gh, repo), os.path.join(sb, "libraries", lib)):
            os.makedirs(os.path.join(root, "src"))
            with open(os.path.join(root, "src", "x.h"), "w") as f:
                f.write("int x;\n")
    img, elf = _nc_image()
    calls = []
    script = {"rc": 0, "used_dir": None, "props": None, "err": ""}

    def fake_cli(argv, low_priority=True, timeout_s=3600):
        calls.append((list(argv), low_priority))
        out = argv[argv.index("--build-path") + 1]
        _nc_folder(os.path.dirname(out), os.path.basename(out), img, elf)
        props = script["props"] if script["props"] is not None else \
            (["compiler.cpp.extra_flags=-DNAVICORE_HIL_HOOKS=1"] if "--build-property" in argv else
             ["compiler.cpp.extra_flags="])
        used = [{"name": lib, "version": "1", "install_dir": script["used_dir"] or os.path.join(sb, "libraries", lib)}
                for lib in ("WCB_Client", "WcbCmd")]
        doc = {"compiler_out": "Sketch uses 1 bytes", "compiler_err": script["err"], "success": script["rc"] == 0,
               "builder_result": {"build_properties": props, "used_libraries": used,
                                  "build_platform": {"id": "esp32:esp32", "version": "3.3.4"}}}
        return script["rc"], json.dumps(doc), "", 1.0
    states = {"n": 0, "change": False}

    def fake_tree(repo):
        states["n"] += 1
        diff = "0123456789ab" if not (states["change"] and states["n"] % 2 == 0) else "ffffffffffff"
        return {"repo": repo, "head": "c" * 40, "short": "ccccccc", "subject": "s", "dirty": True, "files": ["NaviCore.ino"],
                "diff": diff}
    saved = (F._run_cli, F.tree_state, F._core_version, F.cli_path)
    F._run_cli, F.tree_state, F._core_version, F.cli_path = fake_cli, fake_tree, lambda cli: "3.3.4", lambda: "arduino-cli"
    try:
        kw = dict(builds_root=builds, github=gh, sketchbook=sb)
        man = F.build("t1", **kw)
        argv, low = calls[-1]
        assert argv[:4] == ["arduino-cli", "compile", "--json", "--fqbn"] and argv[4] == F.FQBN and low, argv
        assert argv[argv.index("--build-path") + 1] == os.path.join(builds, "navicore-t1") and argv[-1] == nav, argv
        assert "--jobs" in argv and "--build-property" not in argv, argv
        assert not {"-u", "--upload", "-p", "--port"} & set(argv), argv
        with open(os.path.join(builds, "navicore-t1", "BUILD.json"), encoding="utf-8") as f:
            disk = json.load(f)
        assert disk == man and man["navicore"]["short"] == "ccccccc" and man["navicore"]["dirty"], man["navicore"]
        assert man["image"]["version"] == NC_VERSION and man["image"]["slot"] == 0x1E0000 and not man["hooks"], man
        assert man["libraries"]["WcbCmd"]["same"] and not man["drift_allowed"], man["libraries"]
        assert F.build_line(man).startswith("NaviCore `ccccccc` + 1 dirty files (diff 0123456789ab); WCB_Client = "), \
            F.build_line(man)
        assert "BUILD.json" in str(_raises(lambda: F.build("t1", **kw), F.BuildError))
        man = F.build("t1", hooks=True, rebuild=True, **kw)
        assert F.HOOKS_PROPERTY in calls[-1][0] and man["hooks"] and not man["hooks_in_source"] and man["warnings"], man
        script["props"] = ["compiler.cpp.extra_flags="]
        assert "lack" in str(_raises(lambda: F.build("t2", hooks=True, **kw), F.BuildError))
        script["props"] = None
        script["used_dir"] = os.path.join(tmp.root, "elsewhere")
        assert "not the sketchbook copy" in str(_raises(lambda: F.build("t3", **kw), F.BuildError))
        script["used_dir"] = None
        states["change"] = True
        assert "changed during the compile" in str(_raises(lambda: F.build("t4", **kw), F.BuildError))
        states["change"] = False
        script.update(rc=1, err="NaviCore.ino:9:1: error: 'x' was not declared\n    const char* pw = \"hunter2\";\n")
        e = str(_raises(lambda: F.build("t5", **kw), F.BuildError))
        assert "error: 'x' was not declared" in e and "hunter2" not in e, e
        assert os.path.isfile(os.path.join(builds, "navicore-t5", "compile.log"))
        script.update(rc=0, err="")
        with open(os.path.join(sb, "libraries", "WcbCmd", "src", "x.h"), "w") as f:
            f.write("int y;\n")
        n = len(calls)
        e = str(_raises(lambda: F.build("t6", **kw), F.BuildError))
        assert "WcbCmd DIFFERS from WcbCmd: src/x.h" in e and len(calls) == n, e
        man = F.build("t6", allow_drift=True, **kw)
        assert man["drift_allowed"] and "built with library drift" in F.build_line(man), man
        with open(os.path.join(nav, "CLAUDE.md"), "w", encoding="utf-8") as f:
            f.write(NC_FQBN_DOC.replace(",PSRAM=opi", ""))
        assert "does not name" in str(_raises(lambda: F.build("t7", allow_drift=True, **kw), F.BuildError))
        _raises(lambda: F.build("../x", **kw), ValueError)
    finally:
        F._run_cli, F.tree_state, F._core_version, F.cli_path = saved


def t_ncflash_build_source(tmp):
    """build(source=...) (hil/ncflash.py): a NaviCore git worktree, whose folder is not named NaviCore, is compiled from
    a copy named NaviCore holding its sketch files and nothing else (arduino-cli refuses a sketch folder not named after
    its main .ino), and the copy is gone afterwards, even when the build then fails; CLAUDE.md's FQBN, fw_version.h, the
    hooks scan and the tree state come from the source itself, and FLASHED.md's line names its branch. A source already
    named NaviCore compiles in place, no source is still <github>/NaviCore, and a folder without NaviCore.ino is refused
    before anything runs. Then tree_state on a real git repository: the branch, and None once HEAD is detached."""
    from hil import ncflash as F
    gh, sb, builds = (os.path.join(tmp.root, n) for n in ("gh", "sb", "builds"))
    wt = os.path.join(tmp.root, "worktrees", "hil-week")
    named = os.path.join(tmp.root, "elsewhere", "NaviCore")
    version = '#define FW_VERSION_BASE  "v9.9.9"\n#define FW_VERSION_DTG   "%s"\n'
    sketch = {"NaviCore.ino": "void setup(){}\n", "partitions.csv": NC_PARTITIONS,
              "fw_version.h": version % "111111ZSEP26", "navicore_hil.h": "#ifdef NAVICORE_HIL_HOOKS\n#endif\n",
              "src/sub/a.cpp": "int a;\n"}
    rest = {"CLAUDE.md": NC_FQBN_DOC, "README.md": "# x\n", "index.html": "<html></html>\n", "docs/y.md": "y\n",
            "firmware/x.bin": "bin\n"}

    def write(root, files):
        for rel, text in files.items():
            p = os.path.join(root, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8", newline="") as f:
                f.write(text)
    for root in (wt, named, os.path.join(gh, "NaviCore")):
        write(root, {**sketch, **rest})
    for lib, repo in (("WCB_Client", "WCBClient"), ("WcbCmd", "WcbCmd")):
        for root in (os.path.join(gh, repo), os.path.join(sb, "libraries", lib)):
            write(root, {"src/x.h": "int x;\n"})
    img, elf = _nc_image()
    seen = []

    def fake_cli(argv, low_priority=True, timeout_s=3600):
        src, files = argv[-1], {}
        for d, _, names in os.walk(src):
            for n in names:
                with open(os.path.join(d, n), "rb") as f:
                    files[os.path.relpath(os.path.join(d, n), src).replace(os.sep, "/")] = f.read()
        seen.append((src, files))
        out = argv[argv.index("--build-path") + 1]
        _nc_folder(os.path.dirname(out), os.path.basename(out), img, elf)
        props = [F.HOOKS_PROPERTY] if "--build-property" in argv else ["compiler.cpp.extra_flags="]
        used = [{"name": lib, "version": "1", "install_dir": os.path.join(sb, "libraries", lib)}
                for lib in ("WCB_Client", "WcbCmd")]
        return 0, json.dumps({"compiler_out": "", "compiler_err": "", "success": True,
                              "builder_result": {"build_properties": props, "used_libraries": used,
                                                 "build_platform": {"id": "esp32:esp32", "version": "3.3.4"}}}), "", 1.0

    def fake_tree(repo):
        return {"repo": repo, "head": "a" * 40, "short": "aaaaaaa", "subject": "s", "dirty": False, "files": [],
                "diff": None, "branch": "hil-week" if "hil-week" in repo else "main"}
    saved = (F._run_cli, F.tree_state, F._core_version, F.cli_path)
    F._run_cli, F.tree_state = fake_cli, fake_tree
    F._core_version, F.cli_path = lambda cli: "3.3.4", lambda: "arduino-cli"
    try:
        kw = dict(builds_root=builds, github=gh, sketchbook=sb)
        man = F.build("w1", hooks=True, source=wt, **kw)
        src, files = seen[-1]
        assert os.path.basename(src) == "NaviCore" and os.path.normpath(src) != os.path.normpath(wt), src
        assert sorted(files) == sorted(sketch), sorted(files)
        assert all(files[k] == v.encode() for k, v in sketch.items()), "the staged copy differs from the source"
        assert not os.path.exists(src), f"the staged copy {src} was left behind"
        assert man["staged"] and man["navicore"]["repo"] == os.path.normpath(os.path.abspath(wt)), man["navicore"]
        assert man["hooks"] and man["hooks_in_source"] and not man["warnings"], man
        assert F.build_line(man).startswith("NaviCore `aaaaaaa` on hil-week clean; WCB_Client = "), F.build_line(man)
        man = F.build("d1", **kw)
        assert seen[-1][0] == os.path.join(gh, "NaviCore") and not man["staged"], (seen[-1][0], man["staged"])
        assert F.build_line(man).startswith("NaviCore `aaaaaaa` clean; "), F.build_line(man)
        man = F.build("n1", source=named, **kw)
        assert seen[-1][0] == os.path.normpath(os.path.abspath(named)) and not man["staged"], seen[-1][0]
        n = len(seen)
        empty = os.path.join(tmp.root, "empty")
        os.makedirs(empty)
        assert "no NaviCore.ino" in str(_raises(lambda: F.build("x1", source=empty, **kw), F.BuildError))
        write(wt, {"fw_version.h": version % "222222ZSEP26"})
        e = str(_raises(lambda: F.build("w2", source=wt, **kw), F.BuildError))
        assert "fw_version.h says v9.9.9_222222ZSEP26" in e and not os.path.exists(seen[-1][0]), (e, seen[-1][0])
        write(wt, {"CLAUDE.md": NC_FQBN_DOC.replace(",PSRAM=opi", "")})
        assert "does not name" in str(_raises(lambda: F.build("w3", source=wt, **kw), F.BuildError))
        assert len(seen) == n + 1, "a refused source reached the compiler"
    finally:
        F._run_cli, F.tree_state, F._core_version, F.cli_path = saved
    repo = os.path.join(tmp.root, "repo")
    os.makedirs(repo)

    def git(*args):
        subprocess.run(["git", "-C", repo, "-c", "user.name=selftest", "-c", "user.email=selftest@invalid",
                        "-c", "commit.gpgsign=false", *args], check=True, capture_output=True, timeout=60)
    git("init", "-q", "-b", "hil-week")
    write(repo, {"a.h": "int a;\n"})
    git("add", "a.h")
    git("commit", "-q", "-m", "a")
    st = F.tree_state(repo)
    assert st["branch"] == "hil-week" and not st["dirty"], st
    assert F.tree_line(st) == f"`{st['short']}` on hil-week clean", F.tree_line(st)
    git("checkout", "-q", "--detach")
    st = F.tree_state(repo)
    assert st["branch"] is None and F.tree_line(st) == f"`{st['short']}` clean", st


class FakeOtaNavi(FakeNaviDev):
    """NaviCore's ?OTALOCAL (navicore_ota.h:244-310) behind FakeNaviDev: PING, #L12, STATUS, one session with a write
    cursor, the idle reaper (run when a line arrives, as checkOtaTimeout runs in loop()), END verifying the received
    bytes' own appended SHA-256 as esp_ota_end does, and a restart that swaps the slots. `faults` by offset, each used
    once: 'nak' (the line arrived damaged: rejected, NAK at the cursor), 'drop' (written, its ACK lost), 'stall'
    (written, no ACK, and the host waits past the 30 s idle reaper, which fires on the next line), 'hold' (the ACK
    held until the host sends again: the HWCDC stall), 'short' (base64 lost in transit: 3 bytes fewer written),
    'flip' (a byte corrupted on the way). `boot`: 'banner' (setup()'s lines arrive), 'reopen' (the banner is lost to
    the USB re-enumeration), 'old_slot' (the bootloader refused the new image), 'never'. `max_wait` caps how long a
    wait on this fake lasts, so a board that stays silent costs the self-test a fraction of a second."""

    def __init__(self, version=NC_VERSION, faults=None, boot="banner", report_sha=False, max_wait=0.4):
        super().__init__(self._on, name="navicore")
        self.version, self.faults, self.boot, self.report_sha = version, dict(faults or {}), boot, report_sha
        self.slots, self.run_i, self.next_size = [("app0", 0x10000), ("app1", 0x1F0000)], 0, 0x1E0000
        self.session, self.up, self.held, self.aborts, self.idle_s = None, True, [], 0, 30.0
        self.connected, self.closed, self.app_sha, self.flashed, self.max_wait = True, False, None, None, max_wait

    def expect(self, pattern, timeout=3.0, since=None):
        return super().expect(pattern, min(timeout, self.max_wait), since)

    def close(self):
        self.closed, self.connected = True, False

    def open(self):
        self.closed, self.connected = False, True
        return self

    def status(self):
        run, nxt = self.slots[self.run_i], self.slots[self.run_i ^ 1]
        out = ["---------- OTA Status ----------", "Chip:        ESP32-S3 (family 1)", f"Firmware:    {self.version}",
               f"Running:     '{run[0]}' @0x{run[1]:06x} (1966080 B)",
               f"Next (OTA):  '{nxt[0]}' @0x{nxt[1]:06x} ({self.next_size} B)"]
        if self.app_sha:
            out.insert(3, f"App SHA256:  {self.app_sha}")
        s = self.session
        out.append(f"Session:     ACTIVE id=1  {s['written']} / {s['size']} B" if s else "Session:     idle")
        return out + ["--------------------------------"]

    def restart(self):
        self.up = False

        def back():
            if self.boot == "never":
                self._append("<<serial error: device gone>>", "<<reopened COMFAKE>>")
                return
            if self.boot != "old_slot":
                self.run_i ^= 1
                self.app_sha = self.flashed[0xB0:0xB8].hex() if self.report_sha is True else self.report_sha or None
            self.up = True
            if self.boot == "reopen":
                self._append("<<serial error: device gone>>", "<<reopened COMFAKE>>")
            else:
                self._append("", "Reset reason: 3 - Software restart (incl. boot-guard retry)  (RTC codes core0=3 [SW "
                             "system] core1=3 [SW system])", f"[NaviCore] Firmware {self.version} — setup complete.")
        t = threading.Timer(0.1, back)
        t.daemon = True
        t.start()

    def _on(self, text, n):
        import base64
        import hashlib
        if not self.up:
            return []
        out, self.held = self.held, []
        s = self.session
        if s and time.monotonic() - s["t"] > self.idle_s:
            self.session = s = None
            out.append("[OTA] aborted: session timed out (current app intact)")
        if text == "":
            return out
        if text == '{"type":"PING"}':
            return out + [f'{{"type":"PONG","version":"{self.version}"}}']
        if text == "#L12":
            return out + [MODE_LINE]
        if text == "?OTALOCAL,STATUS":
            return out + self.status()
        if text == "?OTALOCAL,ABORT":
            self.aborts += 1
            self.session = None
            return out + (["[OTA] aborted: local abort command (current app intact)"] if s else [])
        if text.startswith("?OTALOCAL,BEGIN,"):
            size, fam = (int(v) for v in text.split(",")[2:4])
            out += ["", "[OTA:BEGIN,START]"]
            if fam != 1:
                return out + [f"[OTA] BEGIN rejected: image chip family {fam} != this board 1 (brick guard)",
                              "[OTA:BEGIN,ERR,0]"]
            nxt = self.slots[self.run_i ^ 1]
            self.session = {"size": size, "written": 0, "buf": bytearray(), "t": time.monotonic()}
            return out + [f"[OTA] BEGIN ok: session 1, {size} B -> partition '{nxt[0]}' @0x{nxt[1]:06x} "
                          f"({self.next_size} B)", "[OTA:BEGIN,OK,0]"]
        if text.startswith("?OTALOCAL,DATA,"):
            _, _, off, b64 = text.split(",", 3)
            off, raw = int(off), base64.b64decode(b64)
            fault = self.faults.pop(off, None) if s and off == s["written"] else None
            if fault == "nak":
                return out + [f"[OTA] DATA rejected at offset {off + 1} (write cursor at {off})", f"[OTA:NAK,{off}]"]
            if not s or off != s["written"]:
                cur = s["written"] if s else 0
                return out + [f"[OTA] DATA rejected at offset {off} (write cursor at {cur})", f"[OTA:NAK,{cur}]"]
            raw = raw[:-3] if fault == "short" else bytes([raw[0] ^ 0xFF]) + raw[1:] if fault == "flip" else raw
            s["buf"] += raw
            s["written"] += len(raw)
            s["t"] = time.monotonic()
            ack = f"[OTA:ACK,{s['written']}]"
            if fault == "stall":
                s["t"] -= self.idle_s + 1      # the host waits past the idle reaper: it fires on the next line
                return out
            if fault == "drop":
                return out
            if fault == "hold":
                self.held.append(ack)
                return out
            return out + [ack]
        if text == "?OTALOCAL,END":
            if not s:
                return out + ["[OTA] END: no matching active session", "[OTA:END,ERR]"]
            self.session = None
            buf = bytes(s["buf"])
            if s["written"] != s["size"]:
                return out + [f"[OTA] END rejected: incomplete {s['written']} / {s['size']} B", "[OTA:END,ERR]"]
            if buf[:1] != b"\xE9" or hashlib.sha256(buf[:-32]).digest() != buf[-32:]:
                return out + ["[OTA] END verify FAILED: ESP_ERR_OTA_VALIDATE_FAILED (image rejected, current app "
                              "intact)", "[OTA:END,ERR]"]
            self.flashed = buf
            self.restart()
            return out + [f"[OTA] END ok: verified {len(buf)} B -> next boot '{self.slots[self.run_i ^ 1][0]}'",
                          "[OTA:END,OK]", "[OTA] rebooting into new firmware in 2s..."]
        return out


NC_FAST = dict(chunk_timeout=0.3, begin_timeout=1.0, end_timeout=1.0, boot_timeout=2.0, nudge_s=0.05)


def t_ncflash_flash(tmp):
    """flash() against FakeOtaNavi over a 13-chunk image: every chunk lands although one arrives damaged (NAK at the
    cursor: sent again), one ACK is lost (?OTALOCAL,STATUS shows it written: go on), and one is held until the host
    sends (a newline releases it); END verifies, the board restarts into the other slot, and STATUS, PING and the App
    SHA256 line all match the image. The board receives exactly the image; session.log gets each DATA line as its
    offset and length, never the base64; FLASHED.md gains a row under its own heading, the hand-kept text above it
    untouched. Then the same with the banner lost to the USB re-enumeration (the reopened port and a PONG end the
    wait), and the refusals that send nothing: an image that fails its check, a PONG that is not NaviCore's, an image
    bigger than the board's Next slot."""
    import re
    from hil import ncflash as F
    from hil import navicore as NC
    img, elf = _nc_image(body=12000)
    folder = _nc_folder(os.path.join(tmp.root, "builds"), "navicore-t", img, elf)
    builds = os.path.join(tmp.root, "builds")
    with open(os.path.join(builds, "FLASHED.md"), "w", encoding="utf-8") as f:
        f.write("# Bench images\n\n| Folder | Board |\n|---|---|\n| `navicore/` | NaviCore |\n\nHand-kept notes.\n")
    board = FakeOtaNavi(faults={2048: "nak", 4096: "drop", 6144: "hold"}, report_sha=True)
    logged = []
    board.log = lambda name, direction, text: logged.append((direction, text))
    res = F.flash(NC.NaviCore(board), folder, "selftest image", builds_root=builds, **NC_FAST)
    assert board.flashed == img and board.run_i == 1, "the board did not receive exactly the image"
    st, sends = res["stats"], (len(img) + 1023) // 1024 + 1        # every chunk once, the damaged one twice
    assert (st["chunks"], st["naks"], st["resyncs"], st["end"]) == (sends, 1, 1, "OK") and st["nudged"] >= 1, st
    assert res["after"]["running"]["label"] == "app1" and res["before"]["running"]["label"] == "app0", res["after"]
    assert res["version"] == NC_VERSION and res["app_sha"] == img[0xB0:0xB8].hex(), res
    data_lines = [t for d, t in logged if d == ">" and t.startswith("?OTALOCAL,DATA,")]
    assert len(data_lines) == sends and all(re.fullmatch(r"\?OTALOCAL,DATA,\d+,<\d+ base64 chars>", t)
                                            for t in data_lines), data_lines[:3]
    notes = [t for d, t in logged if d == "#"]
    assert f"ncflash: {len(img)} / {len(img)} B (100%)" in notes and notes[-1].startswith("ncflash: OK: 'app0'"), notes
    assert ("<", "(empty line: output nudge)") not in logged and any(t == "(empty line: output nudge)"
                                                                   for d, t in logged if d == ">"), logged[-5:]
    assert all(len(t) < 300 for d, t in logged if d == ">"), "base64 reached the log"
    with open(os.path.join(builds, "FLASHED.md"), encoding="utf-8") as f:
        text = f.read()
    assert text.startswith("# Bench images\n\n| Folder | Board |\n|---|---|\n| `navicore/` | NaviCore |\n\nHand-kept notes.\n")
    assert F.FLASH_LOG_HEADING in text and "| `navicore-t/` |" in text and f"`{res['elf_sha'][:16]}`" in text, text
    assert "| ?OTALOCAL | OK: 'app0' @0x010000 -> 'app1' @0x1f0000, PONG " + NC_VERSION in text, text
    board2 = FakeOtaNavi(boot="reopen")
    board2.slots.reverse()                                    # running app1, so this one comes back on app0
    res2 = F.flash(NC.NaviCore(board2), folder, builds_root=builds, **NC_FAST)
    assert board2.flashed == img and res2["after"]["running"]["label"] == "app0" and res2["app_sha"] is None, res2
    with open(os.path.join(builds, "FLASHED.md"), encoding="utf-8") as f:
        rows = [x for x in f.read().splitlines() if x.startswith("| ") and "?OTALOCAL" in x]
    assert len(rows) == 2 and "no App SHA256 line on the board" in rows[1], rows
    corrupt = _nc_folder(tmp.root, "corrupt", img[:-1] + bytes([img[-1] ^ 1]), elf)
    for dev_kw, image, exc, want in (({}, corrupt, F.ImageError, "appended SHA-256"),
                                     ({"version": "1.0.3"}, folder, F.FlashError, "not a NaviCore version"),
                                     ({}, folder, F.FlashError, "does not fit the board's")):
        board = FakeOtaNavi(**dev_kw)
        if want.startswith("does not fit"):
            board.next_size = 4096
        e = _raises(lambda: F.flash(NC.NaviCore(board), image, builds_root=builds, **NC_FAST), exc)
        assert want in str(e) and not any(x.startswith("?OTALOCAL,BEGIN") for x in board.sent), (want, str(e))


def t_ncflash_flash_failures(tmp):
    """flash() where it must stop, and say where, with the running app intact: the board's idle reaper ends the
    session while an ACK is lost (the STATUS resync finds it idle; an ABORT still goes out); a damaged chunk written
    short (the cursor lands inside the chunk: aborted, since that image can no longer be completed); a byte corrupted
    in transit (END's verify refuses it); BEGIN refused for the wrong chip family (ota_stream). After END,OK: the
    board back on its old slot (the bootloader refused the image), the wrong App SHA256, and no board at all (NotBack,
    the esptool rung named). Each writes a FAILED / VERIFY FAILED / NOT BACK row to FLASHED.md."""
    from hil import ncflash as F
    from hil import navicore as NC
    img, elf = _nc_image(body=12000)
    builds = os.path.join(tmp.root, "builds")
    folder = _nc_folder(builds, "navicore-f", img, elf)

    def run(board, exc=F.FlashError):
        return _raises(lambda: F.flash(NC.NaviCore(board), folder, builds_root=builds, **NC_FAST), exc)
    board = FakeOtaNavi(faults={4096: "stall"})
    e = run(board)
    assert e.stage == "data" and e.offset == 4096 and e.intact is True, (e.stage, e.offset, e.intact, str(e))
    assert "the board ended the session: session timed out (current app intact)" in str(e) and board.aborts == 1, str(e)
    assert "the running app is intact: STATUS shows 'app0' @0x010000 running, session idle" in str(e), str(e)
    board = FakeOtaNavi(faults={5120: "short"})
    e = run(board)
    assert e.stage == "data" and e.offset == 6141 and e.intact is True and board.session is None, (e.offset, str(e))
    assert [x for x in board.sent if x.startswith("?OTALOCAL,DATA,")][-1].startswith("?OTALOCAL,DATA,5120,"), \
        "a chunk went out after the one the board wrote short"
    assert "a damaged line was written, so this image cannot be completed" in str(e) and board.aborts == 1, str(e)
    board = FakeOtaNavi(faults={1024: "flip"})
    e = run(board)
    assert e.stage == "end" and e.intact and "END refused: END verify FAILED: ESP_ERR_OTA_VALIDATE_FAILED" in str(e), str(e)
    assert board.run_i == 0 and board.flashed is None
    board = FakeOtaNavi()
    e = _raises(lambda: F.ota_stream(NC.NaviCore(board), img, family=0, **{k: v for k, v in NC_FAST.items()
                                                                              if k != "boot_timeout"}), F.FlashError)
    assert e.stage == "begin" and "image chip family 0 != this board 1 (brick guard)" in str(e), str(e)
    board = FakeOtaNavi(boot="old_slot")
    e = run(board)
    assert e.stage == "verify" and e.intact is True and "came back on the old slot 'app0' @0x010000: the bootloader " \
        "refused the new image" in str(e), str(e)
    board = FakeOtaNavi(report_sha="deadbeefdeadbeef")
    e = run(board)
    assert e.stage == "verify" and "App SHA256 deadbeefdeadbeef is not the image's" in str(e), str(e)
    board = FakeOtaNavi(boot="never")
    e = run(board, F.NotBack)
    assert e.stage == "boot" and e.intact is False and "allow_esptool=True" in str(e) and "app1" in str(e), str(e)
    with open(os.path.join(builds, "FLASHED.md"), encoding="utf-8") as f:
        rows = [x for x in f.read().splitlines() if x.startswith("| ") and "`navicore-f/`" in x]
    kinds = [r.split(" | ")[5].split(":")[0].split(" at ")[0] for r in rows]
    assert kinds == ["FAILED", "FAILED", "FAILED", "VERIFY FAILED after the restart",
                     "VERIFY FAILED after the restart", "NOT BACK after END"], kinds


class _PulseSer:
    """The pyserial handle NaviCore.hard_reset pulses: RTS released after being set is the chip's reset."""

    def __init__(self, board):
        object.__setattr__(self, "board", board)
        object.__setattr__(self, "rts", False)
        object.__setattr__(self, "pulses", 0)

    def __setattr__(self, key, value):
        if key == "rts" and self.rts and not value:
            object.__setattr__(self, "pulses", self.pulses + 1)
            self.board.on_reset()
        object.__setattr__(self, key, value)


class FakeLadderNavi(FakeOtaNavi):
    """A NaviCore for the recovery ladder: `up` says whether its app answers, `after_reset` what a USB-Serial/JTAG
    reset does ('boots', 'download' - the ROM banner, no app -, or 'dead')."""

    def __init__(self, up, after_reset):
        super().__init__(max_wait=0.2)
        self.up, self.after_reset = up, after_reset
        self._ser = _PulseSer(self)

    def on_reset(self):
        if self.after_reset == "boots":
            self.restart_quick()
        elif self.after_reset == "download":
            self.later(0.05, "<<serial error: device gone>>", "<<reopened COMFAKE>>", "ESP-ROM:esp32s3-20210327",
                       "waiting for download")

    def restart_quick(self):
        def back():
            self.up = True
            self._append("<<serial error: device gone>>", "<<reopened COMFAKE>>")
        t = threading.Timer(0.05, back)
        t.daemon = True
        t.start()


def t_ncflash_recover(tmp):
    """recover()'s ladder against FakeLadderNavi and a scripted esptool: a PONG ends it at rung 1 with no reset; a board
    that boots on the USB-Serial/JTAG reset ends it at rung 2; with neither, and esptool not allowed, RecoveryFailed
    names both esptool commands and nothing runs; allowed, a chip in ROM download mode is left by the RTC watchdog
    with nothing written (rung 3), and a hung app gets the known-good image in app0 and boot_app0.bin in otadata in one
    connection (rung 4, a FLASHED.md row), with the port released while esptool holds it. Never an address other than
    0x10000 and 0xe000; a known-good image that fails its check is not written; guard_writes refuses 0x0, 0x8000 and
    any erase."""
    from hil import ncflash as F
    from hil import navicore as NC
    img, elf = _nc_image()
    builds = os.path.join(tmp.root, "builds")
    good = _nc_folder(builds, "navicore", img, elf)
    a15 = os.path.join(tmp.root, "a15")
    parts = os.path.join(a15, "packages", "esp32", "hardware", "esp32", "3.3.4", "tools", "partitions")
    os.makedirs(parts)
    with open(os.path.join(parts, "boot_app0.bin"), "wb") as f:
        f.write(_nc_otadata())
    gh = os.path.join(tmp.root, "gh")
    os.makedirs(os.path.join(gh, "NaviCore"))
    with open(os.path.join(gh, "NaviCore", "partitions.csv"), "w", encoding="utf-8") as f:
        f.write(NC_PARTITIONS)
    env = {k: os.environ.get(k) for k in ("HIL_ARDUINO15", "HIL_GITHUB_ROOT")}
    os.environ.update(HIL_ARDUINO15=a15, HIL_GITHUB_ROOT=gh)
    calls = []

    def esptool_for(board, kick_rc, write_rc=0):
        def run(argv):
            calls.append((list(argv), board.closed))
            if "chip-id" in argv:
                if kick_rc == 0:
                    board.up = True
                return kick_rc, "Chip is ESP32-S3" if kick_rc == 0 else "A fatal error occurred: Failed to connect"
            if write_rc == 0:
                board.up, board.run_i = True, 0
            return write_rc, "Hash of data verified."
        return run
    fast = dict(builds_root=builds, boot_timeout=0.6, ping_tries=1)
    try:
        board = FakeLadderNavi(up=True, after_reset="dead")
        r = F.recover(NC.NaviCore(board), **fast)
        assert r["rung"] == "ping" and r["version"] == NC_VERSION and board._ser.pulses == 0, r
        board = FakeLadderNavi(up=False, after_reset="boots")
        r = F.recover(NC.NaviCore(board), **fast)
        assert r["rung"] == "hard_reset" and board._ser.pulses == 1, r
        board = FakeLadderNavi(up=False, after_reset="dead")
        e = str(_raises(lambda: F.recover(NC.NaviCore(board), esptool=esptool_for(board, 0), **fast), F.RecoveryFailed))
        assert "allow_esptool=True" in e and "--before no-reset --after watchdog-reset --connect-attempts 2 chip-id" in e \
            and "write-flash" in e and "0x10000" in e and "0xe000" in e and not calls, e
        board = FakeLadderNavi(up=False, after_reset="download")
        r = F.recover(NC.NaviCore(board), allow_esptool=True, esptool=esptool_for(board, 0), **fast)
        assert r["rung"] == "kick" and [c[0][-1] for c in calls] == ["chip-id"] and calls[0][1], (r, calls)
        assert not board.closed and "ROM download mode" in r["tried"][-1], r
        calls.clear()
        board = FakeLadderNavi(up=False, after_reset="dead")
        board.run_i = 1
        r = F.recover(NC.NaviCore(board), allow_esptool=True, esptool=esptool_for(board, 2), **fast)
        assert r["rung"] == "esptool" and r["status"]["running"]["label"] == "app0" and len(calls) == 2, (r, calls)
        write, released = calls[1]
        assert released and not board.closed and F.guard_writes(write) is write, calls
        addrs = [write[i] for i in range(len(write)) if write[i].startswith("0x")]
        assert addrs == ["0x10000", "0xe000"] and write[write.index("0x10000") + 1] == os.path.join(good,
                                                                                                    "NaviCore.ino.bin")
        assert write[write.index("0xe000") + 1] == os.path.join(parts, "boot_app0.bin") and "--after" in write and \
            write[write.index("--after") + 1] == "watchdog-reset", write
        with open(os.path.join(builds, "FLASHED.md"), encoding="utf-8") as f:
            assert "| esptool app0 + otadata | written (recovery) |" in f.read()
        calls.clear()
        with open(os.path.join(good, "NaviCore.ino.bin"), "r+b") as f:
            f.write(b"\xE8")
        board = FakeLadderNavi(up=False, after_reset="dead")
        e = str(_raises(lambda: F.recover(NC.NaviCore(board), allow_esptool=True, esptool=esptool_for(board, 2), **fast),
                        F.RecoveryFailed))
        assert "does not pass check_image" in e and not calls, e
        port = "COM5"
        for argv in (["--chip", "esp32s3", "-p", port, "write-flash", "0x0", "boot.bin"],
                     ["-p", port, "write-flash", "-z", "0x8000", "part.bin"],
                     ["-p", port, "write-flash", "0x10000", "a.bin", "0x9000", "nvs.bin"],
                     ["-p", port, "erase-flash"], ["-p", port, "erase-region", "0xe000", "0x2000"]):
            _raises(lambda: F.guard_writes(argv), ValueError)
    finally:
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def t_ncflash_status_parse(tmp):
    """parse_ota_status on otaPrintStatus's format (navicore_ota.h:226-238): idle, an ACTIVE session with its cursor, no
    spare slot, an App SHA256 line (INF9 a), and a stray line before the block; ota_status retries a block cut short."""
    from hil import ncflash as F
    from hil import navicore as NC
    block = ["[DISPATCH] x", "---------- OTA Status ----------", "Chip:        ESP32-S3 (family 1)",
             "Firmware:    v0.2.0_102105QSEP26", "Running:     'app0' @0x010000 (1966080 B)",
             "Next (OTA):  'app1' @0x1f0000 (1966080 B)", "Session:     idle", "--------------------------------"]
    st = F.parse_ota_status(block)
    assert st == {"chip": "ESP32-S3", "family": 1, "firmware": "v0.2.0_102105QSEP26",
                  "running": {"label": "app0", "addr": 0x10000, "size": 1966080},
                  "next": {"label": "app1", "addr": 0x1F0000, "size": 1966080}, "active": False, "session": None,
                  "app_sha": None}, st
    active = block[:6] + ["App SHA256:  5c31d8b4678db243", "Session:     ACTIVE id=1  4096 / 1170096 B", block[-1]]
    st = F.parse_ota_status(block + active)
    assert st["active"] and st["session"] == {"id": 1, "written": 4096, "size": 1170096} and \
        st["app_sha"] == "5c31d8b4678db243", st
    assert F.parse_ota_status(block[:5] + ["Next (OTA):  none — partition table has no spare OTA slot!"] + block[6:])[
        "next"] is None
    cut = {"n": 0}

    def script(text, n):
        if text == "?OTALOCAL,STATUS":
            cut["n"] += 1
            return block[1:4] if cut["n"] == 1 else block[1:]
        return [MODE_LINE] if text == "#L12" else []
    assert F.ota_status(NC.NaviCore(FakeNaviDev(script)))["running"]["label"] == "app0" and cut["n"] == 2


# ---------------------------------------------------------------------------- NaviCore boot and OTA (s46, s47)
def t_ncflash_identity(tmp):
    """hil/ncflash.py's image-identity helpers: image_sha16 reads the 16 hex digits the board prints as App SHA256;
    builds_with_sha finds the build folders carrying a board's SHA (any case, 8-64 digits; nothing for a short or
    non-hex one); flash_rows reads record_flash's table under its heading and nothing of the hand-kept text above it or
    of a heading after it; last_written passes over FAILED, VERIFY FAILED and NOT BACK rows to the newest OK or esptool
    write; put_back names the three commands with the bench image's SHA."""
    from hil import ncflash as F
    builds = os.path.join(tmp.root, "builds")
    img_a, elf_a = _nc_image(elf=b"\x7fELF a")
    img_b, elf_b = _nc_image(elf=b"\x7fELF b")
    a, b = _nc_folder(builds, "navicore-a", img_a, elf_a), _nc_folder(builds, "navicore-b", img_b, elf_b)
    _nc_folder(builds, "navicore-a-copy", img_a, elf_a)
    os.makedirs(os.path.join(builds, "ncflash-logs"))
    sha_a = img_a[0xB0:0xB8].hex()
    assert F.image_sha16(a) == sha_a and F.image_sha16(os.path.join(b, "NaviCore.ino.bin")) == img_b[0xB0:0xB8].hex()
    assert F.image_sha16(os.path.join(builds, "nothing")) is None
    assert [os.path.basename(x) for x in F.builds_with_sha(sha_a.upper(), builds)] == ["navicore-a", "navicore-a-copy"]
    assert [os.path.basename(x) for x in F.builds_with_sha(img_b[0xB0:0xC0].hex(), builds)] == ["navicore-b"]
    assert F.builds_with_sha(sha_a[:7], builds) == [] and F.builds_with_sha("zz" * 8, builds) == [] and \
        F.builds_with_sha(None, builds) == []
    assert F.flash_rows(builds) == [] and F.last_written(builds) is None
    with open(os.path.join(builds, "FLASHED.md"), "w", encoding="utf-8") as f:
        f.write("# Bench images\n\n| Folder | Board |\n|---|---|\n| `navicore/` | NaviCore |\n")
    kw = dict(elf_sha="0" * 64, tree="`abc1234` clean", how="?OTALOCAL", what="x | y")
    F.record_flash(builds, folder=a, result="OK: 'app0' -> 'app1'", **dict(kw, elf_sha=img_a[0xB0:0xD0].hex()))
    F.record_flash(builds, folder=b, result="FAILED at DATA 4096/6144 B: x; running app intact", **kw)
    F.record_flash(builds, folder=b, result="VERIFY FAILED after the restart: it came back on the old slot", **kw)
    rows = F.flash_rows(builds)
    assert [(r["folder"], r["result"].split(":")[0].split(" at ")[0]) for r in rows] == \
        [("navicore-a", "OK"), ("navicore-b", "FAILED"), ("navicore-b", "VERIFY FAILED after the restart")], rows
    assert rows[0]["sha"] == sha_a and rows[0]["what"] == "x / y" and rows[0]["how"] == "?OTALOCAL", rows[0]
    assert F.last_written(builds)["folder"] == "navicore-a"
    F.record_flash(builds, folder=b, result="written (recovery)", **dict(kw, how="esptool app0 + otadata"))
    F.record_flash(builds, folder=a, result="NOT BACK after END: x", **kw)
    with open(os.path.join(builds, "FLASHED.md"), "a", encoding="utf-8") as f:
        f.write("\n## Later notes\n\n| not | a | flash | row | at | all | here |\n")
    assert len(F.flash_rows(builds)) == 5 and F.last_written(builds)["folder"] == "navicore-b"
    saved = (F.BUILDS, F.BENCH_IMAGE)
    try:
        F.BUILDS, F.BENCH_IMAGE = builds, "navicore-a"
        text = F.put_back()
        assert sha_a in text and "hil.ncflash status" in text and "flash results/builds/navicore-a" in text and \
            "recover --allow-esptool --known-good results/builds/navicore-a" in text, text
        assert "results/builds/navicore-b" in F.put_back("navicore-b")
    finally:
        F.BUILDS, F.BENCH_IMAGE = saved


class BootDev(FakeNaviDev):
    """FakeNaviDev plus what a restart and the recovery ladder touch: a pyserial handle whose RTS pulse resets the board
    (_PulseSer; NaviCore.hard_reset), close() and open() (recover() hands the port to esptool), connected/active."""

    def __init__(self, script, name, board):
        super().__init__(script, name)
        self._ser = _PulseSer(board)
        self.connected = self.active = True
        self.closed = False

    def close(self):
        self.closed, self.connected = True, False

    def open(self):
        self.closed, self.connected = False, True
        return self


class FakeLink:
    """A probe wire as the suites use one (hil.links.Link): mark() and received() over a bytearray the model writes."""

    def __init__(self, key, buf):
        self.key, self.buf, self.tap = key, buf, False

    def mark(self):
        return len(self.buf)

    def received(self, since):
        return bytes(self.buf[since:])


class _OtaCore:
    """One board's OTA session (navicore_ota.h otaBegin/otaWrite/otaEnd/otaAbortSession :122-224; a WCB's WCB_OTA.cpp has
    the same core): superseding BEGIN, the brick guard, the size guard, the in-order cursor, the overrun abort,
    esp_ota_write's 0xE9 check on the first chunk, the incomplete-END refusal and esp_ota_end's verify (the image's own
    appended SHA-256, as FakeOtaNavi checks it). `say` prints one console line. Every accepted BEGIN is recorded in
    `erased`, so the selftest can hold each erase to its opt-in."""

    def __init__(self, family, say, run_i, slot_size=0x1E0000):
        self.family, self.say, self.run_i, self.size = family, say, run_i, slot_size
        self.slots = [("app0", 0x10000), ("app1", 0x1F0000)]
        self.s, self.erased, self.pending, self.images = None, [], None, {}

    @property
    def next(self):
        return self.slots[self.run_i ^ 1]

    def written(self):
        return self.s["written"] if self.s else 0

    def abort(self, reason):
        if self.s:
            self.say(f"[OTA] aborted: {reason} (current app intact)")
        self.s = None

    def begin(self, sid, size, fam):
        self.s = None
        if fam != self.family:
            self.say(f"[OTA] BEGIN rejected: image chip family {fam} != this board {self.family} (brick guard)")
            return False
        if size == 0 or size > self.size:
            self.say(f"[OTA] BEGIN rejected: image {size} B exceeds partition '{self.next[0]}' ({self.size} B)")
            return False
        self.erased.append((self.next[0], size))
        self.s = {"sid": sid, "size": size, "written": 0, "buf": bytearray(), "t": time.monotonic(), "shown": 0}
        self.say(f"[OTA] BEGIN ok: session {sid}, {size} B -> partition '{self.next[0]}' @0x{self.next[1]:06x} "
                 f"({self.size} B)")
        return True

    def write(self, sid, off, raw):
        s = self.s
        if not s or sid != s["sid"] or off != s["written"]:
            return False
        if s["written"] + len(raw) > s["size"]:
            self.say(f"[OTA] write overruns image ({s['written']} + {len(raw)} > {s['size']}) — aborting")
            self.s = None
            return False
        if s["written"] == 0 and raw[:1] != b"\xe9":
            self.say(f"[OTA] esp_ota_write failed @{off}: ESP_ERR_OTA_VALIDATE_FAILED — aborting")
            self.s = None
            return False
        s["buf"] += raw
        s["written"] += len(raw)
        s["t"] = time.monotonic()
        if s["written"] - s["shown"] >= 65536 or s["written"] == s["size"]:
            s["shown"] = s["written"]
            self.say(f"[OTA] {s['written']} / {s['size']} B ({s['written'] * 100 // s['size']}%)")
        return True

    def end(self, sid):
        import hashlib
        s = self.s
        if not s or sid != s["sid"]:
            self.say("[OTA] END: no matching active session")
            return False
        self.s = None
        if s["written"] != s["size"]:
            self.say(f"[OTA] END rejected: incomplete {s['written']} / {s['size']} B")
            return False
        buf = bytes(s["buf"])
        if buf[:1] != b"\xe9" or hashlib.sha256(buf[:-32]).digest() != buf[-32:]:
            self.say("[OTA] END verify FAILED: ESP_ERR_OTA_VALIDATE_FAILED (image rejected, current app intact)")
            return False
        self.images[self.run_i ^ 1], self.pending = buf, self.run_i ^ 1
        self.say(f"[OTA] END ok: verified {len(buf)} B -> next boot '{self.next[0]}'")
        return True

    def reap(self, idle_s):
        if self.s and time.monotonic() - self.s["t"] > idle_s:
            self.abort("session timed out")


def _nm_b64(text, cap):
    """mbedtls_base64_decode into a `cap`-byte buffer -> (bytes, 0), or (None, -44) for a character outside the alphabet
    or a bad length, or (None, -42) when it decodes to more than `cap` (the size is checked after the characters)."""
    import base64
    import binascii
    if not re.fullmatch(r"[A-Za-z0-9+/]*={0,2}", text) or len(text) % 4 == 1:
        return None, -44
    try:
        raw = base64.b64decode(text + "=" * (-len(text) % 4), validate=True)
    except binascii.Error:
        return None, -44
    return (None, -42) if len(raw) > cap else (raw, 0)


class NaviBootModel(NaviModel):
    """NaviModel plus what s46 and s47 drive, from NaviCore hil-week 6925773 (the bench image, D45): restarts - REBOOT,
    #L02, the USB-Serial/JTAG reset, a relayed REBOOT, an OTA END, an esptool write - that go quiet at once, come back
    `boot_s` later with setup()'s banner (NaviCore.ino:4486-4953), keep the saved config, command library, clips and
    learned peers, and lose RAM state (debug flags, the monitor, CALIB, an OTA session); ?OTALOCAL and the ?OTA relay
    parser (navicore_ota.h:250-554) on one _OtaCore, with the idle reaper run as loop() runs it; the target side of W1's
    relay (:372-448), its ACK printed on W1; GET_MESH_STATS' uptime (rc_telemetry.h:1371-1378); the new-peer grace
    (:5293-5317) and the boot roll call (:5343-5362); a relayed REBOOT (rc_telemetry.h:2404-2410: no ACK); #L02, #L90 of
    a hook build and #L01; W1's view of a restart (the boot announce, the WDP row's AGE and HWREV); W2 as a relay target
    with its own _OtaCore and console. The waits (grace, roll call, reaper, boot) are short so the suites run in
    seconds; the tests' own constants are patched to match."""
    FORBIDDEN = tuple(rx for rx in NaviModel.FORBIDDEN if rx.pattern != r"^#[Ll]0?2$")
    WCB2_FW = "6.2.1_TEST"
    HOOK = ("[HIL] NAVICORE_HIL_HOOKS build: #L90-#L93 fault verbs and DBG_WIRE (debug bit 7) are live - a test image, "
            "never a release")

    def __init__(self, app_sha, grace_s=0.5, roll_call_s=1.0, idle_s=1.5, boot_s=0.8, mut=()):
        self.mut = set(mut)            # t_ncboot_mutations: a break, or a D-NC fix, by name (see where each is read)
        super().__init__()
        self.app_sha, self.grace_s, self.roll_call_s, self.idle_s, self.boot_s = app_sha, grace_s, roll_call_s, idle_s, \
            boot_s
        self.FW = NaviModel.FW
        self.hooks = self.up = self.wcb_ready = self.alive = True
        self.boot_n, self.up_t = 3, time.monotonic() - 3600
        self.join_t, self.advert_t = time.monotonic() - 3600, time.monotonic() - 20
        self.seen, self.on_air = {1, 2}, {1, 2}
        self.restarts, self.w2, self.w1s2 = [], None, bytearray()
        self.ota = _OtaCore(1, self._say, 1)
        self.w2ota = _OtaCore(0, self._say_w2, 0, slot_size=1966080)
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def _say(self, line):
        self.nav._append(line)

    def _say_w2(self, line):
        if self.w2 is not None:
            self.w2._append(line)

    def _loop(self):
        while self.alive:
            if self.up:
                self.ota.reap(self.idle_s)
            self.w2ota.reap(30.0)
            time.sleep(0.05)

    def merge(self, d):
        super().merge(d)
        if "boardtype_clamped" in getattr(self, "mut", ()) and self.c["boardType"] > 1:
            self.c["boardType"] = 0                  # the D-NC19 fix: clamp to 0-1 on input

    # ------------------------------------------------------------ restarts
    def on_reset(self):
        if "no_reset" not in self.mut:               # break: a USB-Serial/JTAG reset that does nothing
            self.restart(11, 21)

    def restart(self, code, rtc, delay=0.0):
        """The board goes quiet now and comes back `delay` + `boot_s` later, as setup() prints it."""
        self.up, self.monitor = False, False

        def back():
            self.calib = False
            if "flags_survive" not in self.mut:      # break: the debug flags outlive a restart
                self.flags = 0
            self.ota.s = None
            if "old_slot" in self.mut:               # break: the bootloader refuses the new image
                self.ota.pending = None
            if self.ota.pending is not None:
                self.ota.run_i, self.ota.pending = self.ota.pending, None
                img = self.ota.images[self.ota.run_i]
                self.app_sha = img[0xB0:0xB8].hex()
                v = re.search(rb"v\d+\.\d+\.\d+[A-Za-z0-9.+-]*_\d{6}[A-Z]{4}\d{2}", img)
                self.FW = v.group(0).decode() if v else self.FW
            self.load_flash()
            self.boot()
            self.boot_n += 1
            self.up_t = time.monotonic()
            self.wcb_ready = 1 <= self.ident["id"] <= 20
            self.seen = set()
            self.restarts.append(code)
            lines = self.banner(code, rtc)
            self.up = True
            self.nav._append(*lines)
            if self.wcb_ready:
                self.join_t = self.advert_t = time.monotonic()
                self.w1._append(f"[ETM] WCB{self.ident['id']} came ONLINE (boot) (src MAC: 02:00:00:00:00:14)")
                roll = threading.Timer(self.roll_call_s, self._roll_call, (self.boot_n,))
                roll.daemon = True
                roll.start()
        t = threading.Timer(delay + self.boot_s, back)
        t.daemon = True
        t.start()

    def banner(self, code, rtc):
        """setup()'s lines (NaviCore.ino:4486-4953) for the config just loaded; WCB_Client's own lines as it prints them."""
        c, net = self.c, self.c["wcbNetwork"]
        reasons = {3: "Software restart (incl. boot-guard retry)", 7: "RTC watchdog (short-WDT bootloader fired)",
                   11: "USB peripheral (host toggled DTR/RTS — e.g. a tool opening the port)"}
        rtcs = {12: "see rom/rtc.h", 16: "RTC-WDT (short-WDT bootloader fired — auto-retry)", 21: "USB UART chip reset"}
        out = ["", "", "=== NaviCore ===", f"App SHA256: {self.app_sha}"] + ([self.HOOK] if self.hooks else [])
        out += ["Bootloader: stock (IDF v5.5.1-710-g8410210c9a, built Nov 12 2025 10:32:28)",
                f"Reset reason: {code} - {reasons[code]}  (RTC codes core0={rtc} [{rtcs[rtc]}] core1={rtc} [{rtcs[rtc]}])",
                f"Boot attempts since power applied: {self.boot_n}   <-- board retried/reset before this boot",
                "[MEM] rcConfig (335892 bytes) allocated in PSRAM, free PSRAM now 8033516",
                "RC config loaded from LittleFS.", "[CLIPS] mounted: 11496 KB free of 12288 KB"]
        v32 = c["boardType"] == 1
        rx, tx = (5, 4) if v32 else (4, 5)

        def san(b, d):
            return b if 1200 <= b <= 115200 else d
        out += [f"[BOARD] {'WCB HW 3.2' if v32 else 'NaviCore v2'} pin profile",
                f"[SBUS] IN+OUT share Serial1/UART1 — RX GPIO{rx} / TX GPIO{tx}, 100k 8E2 inverted. UART0 = hardware S3.",
                f"[Serial2] Local Maestro open @ {san(c['maestroBaud'], 115200)} baud  TX=GPIO6",
                f"[AUX] S3 open @ {san(c['auxBaud'][0], 9600)} baud (hw UART0)",
                f"[AUX] S4 open @ {19200 if 'banner_s4' in self.mut else san(c['auxBaud'][1], 9600)} baud",
                f"[AUX] S5 open @ {san(c['auxBaud'][2], 9600)} baud",
                f"[SBUS] OUT enabled — re-emit on GPIO{tx} (100k 8E2 inverted)" if c["sbusOutEnabled"] else
                "[SBUS] OUT disabled (passthrough off — no CPU cost)"]
        ap = False
        if c["wifiEnabled"]:
            pw = c["wifiPassword"].encode("utf-8")
            if 0 < len(pw) < 8:
                out += [f"[WIFI] REFUSED: password is {len(pw)} character(s); WPA2 requires 8.",
                        "[WIFI] Not starting an open AP — set a longer password and reboot."]
            elif not pw:
                out += ["[WIFI] REFUSED: no AP password set. Refusing to start an OPEN access point —",
                        "[WIFI] this board accepts REBOOT / RESET_DEFAULTS / SET_CONFIG with no credential."]
            else:
                ap = True
                out += [f'[WIFI] SoftAP "{c["wifiSsid"] or "NaviCore-%d" % net["deviceId"]}" up on channel '
                        f'{net["channel"]} — 192.168.4.1', "[WIFI] ESP-NOW will share this channel (WIFI_AP_STA).",
                        "[WIFI] DHCP offers no default gateway — clients keep their own route.",
                        "[WS] command endpoint ready — ws://192.168.4.1/ws"]
        if not 1 <= net["deviceId"] <= 20:
            out += [f"[WCB_Client] ERROR: device_id {net['deviceId']} is out of range (1–20)",
                    "[WCB] ERROR: wcb->begin() failed — check WCB Network settings in GUI"]
        else:
            out += (["[WCB_Client] SoftAP detected — running WIFI_AP_STA, ESP-NOW sharing the AP's radio channel"]
                    if ap else []) + [
                "[WCB_Client] MAC set to 02:00:00:00:00:14", "[WCB_Client] restored 1 learned peer(s)",
                f"[WCB_Client] Joined WCB network as device ID {net['deviceId']} (quantity={net['quantity']}, oct2=0x00, "
                f"oct3=0x00)", f'[WCB_Client] WDP identity set: type="NaviCore" fw="{self.FW}"',
                f"[WCB] Joined network as device ID {net['deviceId']} (quantity={net['quantity']})"]
        return out + [f"[NaviCore] Firmware {self.FW} — setup complete.",
                      "  Connect config_tool/index.html via Web Serial for configuration."]

    def _roll_call(self, n):
        if n != self.boot_n or not self.wcb_ready:
            return
        floor = [i for i in range(1, min(self.ident["q"], 20) + 1) if i != self.ident["id"]]
        missing = [i for i in floor if i not in self.on_air]
        self.nav._append(*[f"[WCB] roll call: WCB{i} never heard from" for i in missing],
                         f"[WCB] roll call: {len(floor) - len(missing)}/{len(floor)} board(s) online "
                         f"{max(1, int(self.roll_call_s))}s after join")

    def advert(self, n):
        """drainPeerEvents (NaviCore.ino:5293-5317) for WCB n's WDP advert: silent inside the grace, else the alert and the
        configured actions (a ;S2 unicast to W1 lands on its S2 wire)."""
        if not self.wcb_ready or n in self.seen:
            return
        self.seen.add(n)
        if time.monotonic() < self.join_t + self.grace_s:
            return
        if "grace_fixed" in self.mut and n in self.on_air:
            return                                   # the D-NC25 fix: a board online when the grace ended is not new
        out = [f"[PEER] New WCB {n} W{n} detected"] if self.c["peerAlert"] else []
        for a in self.c["peerActions"]:
            out += self.dispatch(a)
            if a["type"] == "wcb_unicast" and a["target"] == "1" and a["cmd"].startswith(";S2"):
                self.w1s2 += a["cmd"][3:].encode() + b"\r"
        if out:
            self.nav._append(*out)

    # ------------------------------------------------------------ NaviCore's console
    def script(self, text, n):
        if not self.up:
            self.received.append(text)
            return []
        return super().script(text, n)

    def hash_cmd(self, text):
        if len(text) >= 3 and text[1] in "Ll":
            fn = (ord(text[2]) - 48) * 10 + (ord(text[3]) - 48) if len(text) >= 4 else ord(text[2]) - 48
            if fn == 2:
                self.restart(3, 12)
                return []
            if fn == 90 and self.hooks:
                ms = _nm_toint(text[5:]) if len(text) > 5 else 0
                return [f"[HIL] #L90: loop() stalls {ms} ms at its next pass", "[HIL] #L90: loop() resumed after 0 ms"]
            if fn == 1:
                return [f"NaviCore — {'WCB HW 3.2' if self.applied['board'] == 1 else 'NaviCore v2'}"]
        return super().hash_cmd(text)

    def cli(self, text):
        if text.startswith("?OTALOCAL,"):
            return self.ota_local(text[10:])
        if text.startswith("?OTA,"):
            return self.relay_out(text[5:])
        return super().cli(text)

    def ota_status(self):
        run, nxt, s = self.ota.slots[self.ota.run_i], self.ota.next, self.ota.s
        return ["---------- OTA Status ----------", "Chip:        ESP32-S3 (family 1)", f"Firmware:    {self.FW}",
                f"App SHA256:  {self.app_sha}", f"Running:     '{run[0]}' @0x{run[1]:06x} ({self.ota.size} B)",
                f"Next (OTA):  '{nxt[0]}' @0x{nxt[1]:06x} ({self.ota.size} B)",
                f"Session:     ACTIVE id={s['sid']}  {s['written']} / {s['size']} B" if s else "Session:     idle",
                "--------------------------------"]

    def ota_local(self, args):
        """processOtaLocalCommand (navicore_ota.h:269-335)."""
        c1 = args.find(",")
        sub, rest = (args if c1 < 0 else args[:c1]).strip().upper(), ("" if c1 < 0 else args[c1 + 1:])
        if sub in ("STATUS", ""):
            return self.ota_status()
        if sub == "BEGIN":
            p = rest.find(",")
            if p < 0:
                return ["[OTA] BEGIN usage: ?OTALOCAL,BEGIN,<imageSize>,<family 0|1>"]
            m = self.nav.mark()
            ok = self.ota.begin(1, _nm_toint(rest[:p]), _nm_toint(rest[p + 1:]) & 0xFF)
            said = self.nav.since(m)
            del self.nav.lines[m:]                 # printed after START, in this order (:288-291)
            return ["", "[OTA:BEGIN,START]"] + said + [f"[OTA:BEGIN,{'OK' if ok else 'ERR'},{self.ota.written()}]"]
        if sub == "DATA":
            p = rest.find(",")
            if p < 0:
                return ["[OTA] DATA usage: ?OTALOCAL,DATA,<offset>,<base64>"]
            off = _nm_toint(rest[:p])
            raw, rc = _nm_b64(rest[p + 1:].strip(), 1024)
            if rc:
                return [f"[OTA] DATA base64 error {rc} (chunk too big? max 1024 B decoded)"]
            if not self.ota.write(1, off, raw):
                if "reaper_refresh" in self.mut and self.ota.s:
                    self.ota.s["t"] = time.monotonic()   # break: a rejected chunk keeps the local session alive
                return [f"[OTA] DATA rejected at offset {off} (write cursor at {self.ota.written()})",
                        f"[OTA:NAK,{self.ota.written()}]"]
            return [f"[OTA:ACK,{self.ota.written()}]"]
        if sub == "END":
            if self.ota.end(1):
                self.restart(3, 12, delay=0.3)
                return ["[OTA:END,OK]", "[OTA] rebooting into new firmware in 2s..."]
            return ["[OTA:END,ERR]"]
        if sub == "ABORT":
            self.ota.abort("local abort command")
            return []
        return [f"[OTA] unknown subcommand '{sub}' (use STATUS|BEGIN|DATA|END|ABORT)"]

    def relay_out(self, args):
        """processOtaRelayCommand (navicore_ota.h:475-554): NaviCore relaying to WCB <target>. Only W2 answers here; its
        ACK is printed on NaviCore's console from loop() (:463-469)."""
        import zlib
        c1 = args.find(",")
        sub, rest = (args if c1 < 0 else args[:c1]).strip().upper(), ("" if c1 < 0 else args[c1 + 1:])
        c2 = rest.find(",")
        target, r2 = _nm_toint(rest if c2 < 0 else rest[:c2]) & 0xFF, ("" if c2 < 0 else rest[c2 + 1:])
        c3 = r2.find(",")
        sid, r3 = _nm_toint(r2 if c3 < 0 else r2[:c3]) & 0xFFFF, ("" if c3 < 0 else r2[c3 + 1:])
        if not self.wcb_ready:
            return ["[OTA] relay: WCB not ready"]
        if not 1 <= target <= 20:
            return [f"[OTA] relay: invalid target {target}"]
        if sub == "BEGIN":
            p = r3.find(",")
            return self.w2_frame(target, "BEGIN", sid, size=_nm_toint(r3 if p < 0 else r3[:p]),
                                 fam=0 if p < 0 else _nm_toint(r3[p + 1:]) & 0xFF)
        if sub == "DATA":
            p = r3.find(",")
            if p < 0:
                return ["[OTA] relay DATA: ?OTA,DATA,<t>,<s>,<offset>[:<crc32>],<b64>"]
            off_field, b64 = r3[:p], r3[p + 1:].strip()
            off_field, _, crc = off_field.partition(":")
            if crc:
                want = int(re.match(r"[0-9A-Fa-f]*", crc).group(0) or "0", 16)
                have = zlib.crc32(f"{off_field},{b64}".encode()) & 0xFFFFFFFF
                if want != have:
                    return [f"[OTA] relay DATA @{_nm_toint(off_field)} DROPPED: crc {have:08X} != {want:08X} "
                            f"(b64 {len(b64)} chars)"]
            raw, rc = _nm_b64(b64, 192)
            if rc:
                return [f"[OTA] relay DATA base64 error {rc}"]
            return self.w2_frame(target, "DATA", sid, off=_nm_toint(off_field), raw=raw)
        if sub in ("END", "ABORT"):
            return self.w2_frame(target, sub, sid)
        return [f"[OTA] relay: unknown subcommand '{sub}'"]

    def w2_frame(self, target, sub, sid, size=0, fam=0, off=0, raw=b""):
        """W2's target side (WCB_OTA.cpp handleOta*Packet, the twin of navicore_ota.h:372-448) for a frame NaviCore relayed:
        its console lines, then its ACK back on NaviCore's console."""
        if target != 2 or self.w2 is None:
            return []
        core = self.w2ota
        if sub == "BEGIN":
            ok = core.begin(sid, size, fam)
            ack = (core.written(), 0 if ok else 1)
        elif sub == "DATA":
            if core.s and core.s["sid"] == sid:
                core.s["t"] = time.monotonic()
            core.write(sid, off, raw)
            ack = (core.written(), 0 if core.s and core.s["sid"] == sid else 1)
        elif sub == "END":
            ok = core.end(sid)
            ack = (0, 0 if ok else 1)
            if ok:
                def back():
                    core.run_i, core.pending = core.pending, None
                    self._say_w2("Reset reason: 3 - Software Reset")
                    self.w1._append("[ETM] WCB2 came ONLINE (boot) (src MAC: 02:00:00:00:00:02)")
                t = threading.Timer(0.5, back)
                t.daemon = True
                t.start()
        else:
            core.abort("remote abort")
            ack = (0, 0)
        self.nav.later(0.03, f"[OTA:ACK,2,{sid},{ack[0]},{ack[1]}]")
        return []

    def json_line(self, line):
        obj, err = self.parse_header(line)
        t = obj.get("type") if isinstance(obj, dict) else None
        if t == "REBOOT":
            self.restart(3, 12, delay=0.25)
            return ['{"type":"ACK","ok":true,"msg":"rebooting"}']
        if t == "GET_MESH_STATS":
            up = int((time.monotonic() - self.up_t) * 1000)
            return [f'{{"type":"MESH_STATS","pg":0,"self":{self.ident["id"]},"upMs":{up},"agg":{{"sent":0,"ackd":0,'
                    f'"rty":0,"fail":0,"ung":0,"bcast":0,"recv":0}},"peers":[],"last":1}}']
        if t == "WCB_SEND" and _nm_pick(obj, "target", 0) == 1:
            cmd = _nm_pick(obj, "cmd", "")
            if not self.wcb_ready:
                return ['{"type":"ACK","ok":false,"msg":"WCB not ready (init failed)"}']
            if cmd.startswith(";S0,"):
                self.w1._append(cmd[4:])
            elif cmd.startswith(";S2"):
                self.w1s2 += cmd[3:].encode() + b"\r"
            return ['{"type":"ACK","ok":true}']
        return super().json_line(line)

    # ------------------------------------------------------------ W1 and W2
    def relay_in(self, sub, sid, rest):
        """NaviCore's target side (navicore_ota.h:372-448) for a frame W1's relay sent it -> the ACK lines W1 prints."""
        import base64
        if not self.wcb_ready or not self.up:
            return []
        if sub == "BEGIN":
            size, _, fam = rest.partition(",")
            ok = self.ota.begin(sid, _nm_toint(size), _nm_toint(fam or "0") & 0xFF)
            return [f"[OTA:ACK,20,{sid},{self.ota.written()},{0 if ok else 1}]"]
        if sub == "DATA":
            off_field, _, b64 = rest.partition(",")
            off_s = off_field.partition(":")[0]
            if self.ota.s and self.ota.s["sid"] == sid:
                self.ota.s["t"] = time.monotonic()
            self.ota.write(sid, _nm_toint(off_s), base64.b64decode(b64))
            live = bool(self.ota.s) and self.ota.s["sid"] == sid or "ack_ok_no_session" in self.mut   # break: #70 twin
            return [f"[OTA:ACK,20,{sid},{self.ota.written()},{0 if live else 1}]"]
        if sub == "END":
            ok = self.ota.end(sid)
            if ok:
                self._say("[OTA] remote update verified — rebooting into new firmware...")
                self.restart(3, 12, delay=0.35)
            return [f"[OTA:ACK,20,{sid},0,{0 if ok else 1}]"] * (4 if ok else 1)
        if sub == "ABORT":
            self.ota.abort("remote abort")
            return [f"[OTA:ACK,20,{sid},0,0]"]
        return []

    def w1_script(self, text, n):
        if text == "?WDP,POLL":
            for b in sorted(self.on_air):
                self.advert(b)
            return ["[WDP] POLL: advertised and solicited"]
        if text == "?WDP,DUMP":
            hw = "NaviCore v2" if self.applied["board"] == 0 else "WCB 3.2"
            return [f"[WDP:N=20,CLIENT=1,ALIAS=NaviCore,HW=32,HWREV={hw},FW={self.FW},CAP=0000,CTRL=0,CAPTAGS=,"
                    f"MAESTRO=1,AGE={int(time.monotonic() - self.advert_t)},SEEN=1,PEER=0]"] + \
                [f"[WDPIF:N=20,S={p},DEV={v}]" for p, v in sorted(self.labels().items())] + ["[WDP:END,count=2]"]
        m = re.match(r"\?OTA,(\w+),20,(\d+)(?:,(.*))?$", text)
        if m:
            return self.relay_in(m.group(1).upper(), int(m.group(2)), m.group(3) or "")
        if text == ';W20,{"type":"REBOOT"}':
            if self.wcb_ready and self.up:
                self.nav._append("[RC] Remote REBOOT requested via WCB")
                self.restart(3, 12, delay=0.1 if "reboot_acked" not in self.mut else 0.5)
                if "reboot_acked" in self.mut:       # the D-NC29 fix: ACK, then a deferred restart
                    return ['{"sys":1,"type":"ACK","of":"REBOOT","ok":true}']
            return []
        if text.startswith(";W20,?") and not (self.wcb_ready and self.up):
            return []
        return super().w1_script(text, n)

    def w2_script(self, text, n):
        if text.startswith(";S0,"):
            return [text[4:]]
        if text == "?OTALOCAL,STATUS":
            core = self.w2ota
            run, nxt = core.slots[core.run_i], core.next
            return ["---------- OTA Status ----------", "Chip:        ESP32-D0WD-V3 (family 0)",
                    f"Firmware:    {self.WCB2_FW}", f"Running:     '{run[0]}' @0x{run[1]:06x} (1966080 B)",
                    f"Next (OTA):  '{nxt[0]}' @0x{nxt[1]:06x} (1966080 B)",
                    "Session:     idle" if not core.s else f"Session:     ACTIVE id={core.s['sid']}",
                    "--------------------------------"]
        return []

    def esptool(self, argv):
        """esptool as recover() runs it (hil/ncflash.py kick_argv, write_argv): chip-id finds no ROM bootloader (the app
        runs); write-flash puts the given app into app0, selects it, and the RTC watchdog boots it."""
        if "chip-id" in argv:
            return 2, "A fatal error occurred: Failed to connect to ESP32-S3: No serial data received."
        with open(argv[argv.index("0x10000") + 1], "rb") as f:
            self.ota.images[0] = f.read()
        self.ota.pending = 0
        self.restart(7, 16)
        return 0, "Hash of data verified.\nLeaving...\nHard resetting via RTC WDT..."


NCBOOT_SHOULD = {"ncboot.new_peer_after_boot", "ncboot.mesh_reboot", "ncboot.boardtype2_mismatch"}


def t_ncboot_helpers(tmp):
    """The pure parts of s46 and s47. The banner anchors against NaviBootModel's banner (setup()'s lines for the bench
    config): whole, nothing to report and the facts read; then a wrong baud, a SoftAP on another SSID or channel, two
    lines swapped, a failure line and a lost start line are each named, and no message quotes the SSID. The setTarget
    scan finds only 0x04 frames among Pololu traffic; the stick and switch pickers take J4 (remote-only) and a switch
    tier's marker on a wired port, and pass over the mode switch, an easing switch, a knob with a local output and an
    unwired port. The BEGIN builders refuse anything NaviCore's or W2's guards would let through."""
    import types
    import suites.s46_navicore_boot as S46
    import suites.s47_navicore_ota as S47
    m = NaviBootModel(app_sha="529503cd35f1e5e5")
    m.alive = False
    cfg = json.loads(m.text())
    lines = ["ESP-ROM:esp32s3-20210327", "rst:0xc (RTC_SW_CPU_RST),boot:0x2b (SPI_FAST_FLASH_BOOT)"] + m.banner(3, 12)
    anchors = S46.banner_anchors(cfg, m.FW, m.app_sha, True)
    banner = S46.banner_lines(lines)
    assert banner[0] == "=== NaviCore ===" and banner[-1].endswith("setup complete."), banner[:2]
    problems, facts = S46.banner_problems(banner, anchors)
    assert not problems and facts["bootloader"] == "stock" and facts["reset"] == 3 and facts["rtc"] == (12, 12), \
        (problems, facts)

    def check(mutate, cfg_change=None, want=None):
        c2 = json.loads(json.dumps(cfg))
        (cfg_change or (lambda c: None))(c2)
        got, _ = S46.banner_problems(mutate(list(banner)), S46.banner_anchors(c2, m.FW, m.app_sha, True))
        assert got and all(want in p for p in got[:1]) and not any("HILap" in p for p in got), (want, got)
        return got
    check(lambda b: b, lambda c: c["auxBaud"].update(S4=19200), "S4: missing (expected '[AUX] S4 open @ 19200 baud')")
    check(lambda b: b, lambda c: c.update(wifiSsid="OTHERap"), "the SoftAP on the mesh channel: missing")
    check(lambda b: b, lambda c: c["wcbNetwork"].update(channel=6), "the SoftAP on the mesh channel: missing")
    i, j = banner.index("[AUX] S3 open @ 115200 baud (hw UART0)"), banner.index("[BOARD] NaviCore v2 pin profile")
    check(lambda b: b[:j] + [b[i]] + b[j:i] + b[i + 1:], None,
          "S3: out of order (expected '[AUX] S3 open @ 115200 baud (hw UART0)')")
    got = check(lambda b: b[:-1] + ['[WIFI] SoftAP "HILap" FAILED to start on channel 1.', b[-1]], None,
                "a [WIFI] failure line (not quoted: it names the AP)")
    assert S46.banner_lines(lines[:2]) == [] and S46.banner_anchors(cfg, m.FW, None, False)[1][0] != "App SHA256"
    # the SoftAP line names the AP: redact_text (and so Bench.log on NaviCore's lines, and every failure detail) hashes
    # it, once, as hil/ncflash.py's own logs do
    from hil.checkpoint import redact_text
    ap = next(x for x in banner if x.startswith("[WIFI] SoftAP"))
    hashed = redact_text(ap)
    assert "HILap" not in hashed and hashed.startswith('[WIFI] SoftAP "<redacted:') and redact_text(hashed) == hashed and \
        hashed.endswith('" up on channel 1 — 192.168.4.1'), hashed
    # the setTarget scan: setSpeed 0x07 and setAcceleration 0x09 frames (the easing re-applied at boot) are no motion
    frames = bytes([0xAA, 2, 0x07, 0, 20, 0, 0xAA, 4, 0x04, 5, 0x70, 0x2E, 0xAA, 3, 0x09, 0, 3, 0, 0xAA, 2, 0x04, 0, 0, 0])
    assert S46._set_targets(frames) == [(4, 5, 6000), (2, 0, 0)] and S46._set_targets(b"HILSIA\r") == []
    ctl = {"lx": 3, "ly": 4, "rx": 1, "ry": 2,
           "sw": [{"l": "SA", "c": 12, "t": 0, "pos": 0, "v": [172, 992, 1811]},
                  {"l": "SC", "c": 10, "t": 0, "pos": 1, "v": [172, 992, 1811]},
                  {"l": "SI", "c": 16, "t": 1, "pos": 0, "v": [172, 1811, 1811]}]}
    ncfg = {"funcBindings": {"mode": 4}, "maestros": [{"type": 1, "device": 1}] + [{"type": 2, "device": d} for d in
                                                                                    range(2, 9)],
            "knobs": {"J3": {"channel": 3, "function": 1, "outputs": [{"target": 1}, {"target": 2}]},
                      "J4": {"channel": 4, "function": 1, "outputs": [{"target": t} for t in range(2, 8)]}},
            "switches": {"SA": {"channel": 8}, "SB": {"channel": 9}, "SC": {"channel": 10, "positions": 3,
                                                                           "p0": [{"type": "maestro", "target": "1",
                                                                                   "cmd": "setEasing,p0"}]},
                         "SD": {"channel": 11}, "SE": {"channel": 12, "positions": 3,
                                                       "p2": [{"type": "wcb_broadcast", "cmd": ";W1;S3HILMODE"}]},
                         "SF": {"channel": 13}, "SG": {"channel": 14}, "SH": {"channel": 15},
                         "SI": {"channel": 16, "positions": 2, "p0": [{"type": "wcb_broadcast", "cmd": ";W1;S3HILSIA"}],
                                "p2": [{"type": "wcb_broadcast", "cmd": ";W2;S2HILSIB"}]}}}
    assert S46._knob_stick(ctl, ncfg) == ("ly", "J4", [2, 3, 4, 5, 6, 7])
    assert S46._knob_stick(dict(ctl, ly=9), ncfg) is None                 # J3 drives a local slot: never picked
    wired = {(2, "S2"): FakeLink("W2S2", bytearray())}
    bench = types.SimpleNamespace(links=types.SimpleNamespace(get=lambda w, p: wired.get((w, p))))
    sw = S46._switch_marker(bench, ctl, ncfg)
    assert (sw["label"], sw["index"], sw["back"], sw["go"], sw["text"], sw["link"].key) == \
        ("SI", 2, 0, 2, "HILSIB", "W2S2"), sw                             # a 2-way switch's position 1 reads as 0
    wired.clear()
    assert S46._switch_marker(bench, ctl, ncfg) is None                   # W2S2 unwired; SE is the mode switch
    nxt = {"label": "app0", "addr": 0x10000, "size": 1966080}
    for size, fam in ((4096, 1), (1, 1), (1966080, 1), (4096, 257)):
        _raises(lambda: S47._refused_begin(size, fam, nxt), ValueError)
    assert [S47._refused_begin(s, f, nxt) for s, f in ((4096, 0), (0, 1), (1966081, 1))] == \
        ["?OTALOCAL,BEGIN,4096,0", "?OTALOCAL,BEGIN,0,1", "?OTALOCAL,BEGIN,1966081,1"]
    _raises(lambda: S47._relay_begin(2, 7, 4096, 2), ValueError)
    assert S47._relay_begin(2, 7, 4096, 1) == "?OTA,BEGIN,2,7,4096,1"


def _run_boot_suite(tmp, ids=None, mut=(), tag="all"):
    """A fake bench for s46 and s47 under <tmp>/<tag>: a bench image and a rollback image in a builds folder with their
    FLASHED.md rows, W2's bench image, the core's boot_app0.bin and NaviCore's partitions.csv for the esptool rung, a
    fresh NaviBootModel(mut) behind NaviCore, W1 and W2, every opt-in on. Runs the tests named in `ids` (default: all
    but sbus.boot_quiet, which needs the SBUS controller) through the runner, with the suites' waits and paths patched
    to the model's, and puts every patch back -> (results by id, the model, facts: builds, bench_img, w2_img, log,
    count)."""
    import contextlib
    import hashlib
    from hil import ncflash as F
    root = os.path.join(tmp.root, tag)
    builds = os.path.join(root, "builds")
    bench_img, bench_elf = _nc_image(version=NaviModel.FW, elf=b"\x7fELF bench")
    old_img, old_elf = _nc_image(version=NaviModel.FW, elf=b"\x7fELF rollback")
    bench_dir = _nc_folder(builds, "navicore-bench", bench_img, bench_elf)
    old_dir = _nc_folder(builds, "navicore", old_img, old_elf)
    for folder, img in ((old_dir, old_img), (bench_dir, bench_img)):
        F.record_flash(builds, folder=folder, elf_sha=img[0xB0:0xD0].hex(), tree="not recorded", how="?OTALOCAL",
                       result="OK: 'app0' @0x010000 -> 'app1' @0x1f0000", what="selftest")
    w2_img = b"\xe9" + bytes(700) + NaviBootModel.WCB2_FW.encode() + bytes(900)
    w2_img += hashlib.sha256(w2_img).digest()
    w2_path = os.path.join(root, "WCB.ino.bin")
    with open(w2_path, "wb") as f:
        f.write(w2_img)
    a15 = os.path.join(root, "a15")
    parts = os.path.join(a15, "packages", "esp32", "hardware", "esp32", "3.3.4", "tools", "partitions")
    os.makedirs(parts)
    with open(os.path.join(parts, "boot_app0.bin"), "wb") as f:
        f.write(_nc_otadata())
    gh = os.path.join(root, "gh")
    os.makedirs(os.path.join(gh, "NaviCore"))
    with open(os.path.join(gh, "NaviCore", "partitions.csv"), "w", encoding="utf-8") as f:
        f.write(NC_PARTITIONS)
    model = NaviBootModel(app_sha=bench_img[0xB0:0xB8].hex(), mut=mut)
    model.ota.images[1] = bench_img
    b = tmp.bench({"wcb1": {"port": "COMW1", "kind": "wcb", "wcb": 1}, "wcb2": {"port": "COMW2", "kind": "wcb", "wcb": 2},
                   "navicore": {"port": "COMNAV", "kind": "navicore"}, "sbus": {"port": "COMS", "kind": "sbus"}})
    b.cfg["opt_in"] = list(optin.OPT_INS)
    nav = BootDev(model.script, "navicore", model)
    w1, w2 = FakeNaviDev(model.w1_script, "wcb1"), FakeNaviDev(model.w2_script, "wcb2")
    model.nav, model.w1, model.w2 = nav, w1, w2
    nav.log = w1.log = w2.log = b.log
    b.dev = lambda name: {"navicore": nav, "wcb1": w1, "wcb2": w2}[name]
    saved_reg = list(runner.REGISTRY)
    try:
        runner.REGISTRY[:] = []
        for name in ("suites.s46_navicore_boot", "suites.s47_navicore_ota"):
            sys.modules.pop(name, None)
        import suites.s46_navicore_boot as S46
        import suites.s47_navicore_ota as S47
        mine = [dict(t) for t in runner.REGISTRY if t["id"].startswith(("ncboot.", "ncota.")) and
                (ids is None or t["id"] in ids)]
    finally:
        runner.REGISTRY[:] = saved_reg
    for t in mine:
        t["needs"], t["links"], t["drives"], t["_drives"] = [], [], [], set()
    patches = [(F, "BUILDS", builds), (F, "BENCH_IMAGE", "navicore-bench"), (F, "run_esptool", model.esptool),
               (S47, "WCB_BENCH_IMAGE", w2_path), (S47, "REAPER_S", model.idle_s),
               (S47, "config_guard", lambda bench, *w: contextlib.nullcontext()),
               (S46, "ROLL_CALL_S", model.roll_call_s), (S46, "PEER_GRACE_S", model.grace_s), (S46, "ADVERT_WAIT_S", 1.0),
               (S46, "link", lambda bench, w, p: FakeLink(f"W{w}{p}", model.w1s2))]
    saved = [(mod, name, getattr(mod, name)) for mod, name, _ in patches]
    env = {k: os.environ.get(k) for k in ("HIL_ARDUINO15", "HIL_GITHUB_ROOT")}
    os.environ.update(HIL_ARDUINO15=a15, HIL_GITHUB_ROOT=gh)
    for mod, name, value in patches:
        setattr(mod, name, value)
    saved_g = _fast_guard()
    try:
        ck = new_run(b, mine)
    finally:
        _slow_guard(saved_g)
        for mod, name, value in saved:
            setattr(mod, name, value)
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        model.alive = False
    log = read(os.path.join(ck.out_dir, "session.log"))
    b.close()
    return ({r["id"]: r for r in ck.data["results"]}, model,
            dict(builds=builds, bench_img=bench_img, w2_img=w2_img, log=log, count=len(mine)))


def t_ncboot_ncota_against_model(tmp):
    """Every test of s46 and s47 but sbus.boot_quiet (it needs the SBUS controller; t_ncboot_helpers covers its parts)
    run whole, through the runner, against NaviBootModel, with every opt-in on and the waits shrunk to the model's: each
    normal and opt-in test passes and each (should) test fails, as on today's NaviCore. Then the bench is as it was: the
    model's saved config, command library and clips unchanged; NaviCore on the bench image (the esptool rung moved it to
    app0, and the flashes come back to where each began); every accepted BEGIN belongs to an erasing or flashing test
    (4 KB twice, then the bench image four times; W2's image twice), so no read-only test ever erased a slot; FLASHED.md
    gained a row for each flash; no result detail and no runner note names the AP's SSID, every boot banner's SoftAP
    line reached session.log hashed, and session.log carries no credential."""
    from hil import ncflash as F
    probe = NaviModel()
    orig = (probe.flash, probe.cmdlib, list(probe.clips))
    res, model, x = _run_boot_suite(tmp)
    bad = [f"{tid}: {r['status']} (expected {'FAIL' if tid in NCBOOT_SHOULD else 'PASS'}) {r['detail'][:400]}"
           for tid, r in res.items() if r["status"] != ("FAIL" if tid in NCBOOT_SHOULD else "PASS")]
    assert len(res) == x["count"] == 19, (len(res), x["count"])
    assert not bad, "\n".join(bad)
    for tid in NCBOOT_SHOULD:
        assert "(should, D-NC" in res[tid]["detail"], (tid, res[tid]["detail"][:200])
    assert (model.flash, model.cmdlib, model.clips) == orig, "the model's saved config, library or clips changed"
    img = x["bench_img"]
    assert model.app_sha == img[0xB0:0xB8].hex() and model.FW == NaviModel.FW, (model.app_sha, model.FW)
    assert model.ota.slots[model.ota.run_i][0] == "app0" and model.ota.s is None, model.ota.run_i
    n = len(img)
    assert [size for _, size in model.ota.erased] == [4096, 4096, n, n, n, n], model.ota.erased
    assert [size for _, size in model.w2ota.erased] == [len(x["w2_img"])] * 2 and model.w2ota.run_i == 0, \
        model.w2ota.erased
    rows = F.flash_rows(x["builds"])
    assert [(r["folder"], r["how"], r["result"].split(":")[0]) for r in rows[2:]] == \
        [("navicore-bench", "esptool app0 + otadata", "written (recovery)")] + \
        [("navicore-bench", "?OTALOCAL", "OK")] * 2 + [("navicore-bench", "?OTA relay via W1", "OK")] * 2, rows[2:]
    assert model.restarts.count(11) == 1 and model.restarts.count(7) == 1, model.restarts
    log = x["log"]
    notes = [line for line in log.splitlines() if " runner # " in line]
    assert not any("HILap" in r["detail"] for r in res.values()), "a result detail names the AP"
    assert not any("HILap" in line for line in notes), "a runner note names the AP"
    assert 'SoftAP "HILap"' not in log and log.count('SoftAP "<redacted:') >= 15, "a banner's SoftAP line kept its name"
    assert not any(s in log for s in SECRETS), "a credential reached session.log"


# (test, model mutation, the status it must then get): a break of NaviCore's behaviour each test exists to catch, and
# each D-NC fix its (should) test asks for.
NCBOOT_MUTATIONS = (
    ("ncboot.reboot_resets_ram_state", "flags_survive", "FAIL", "a debug flag survived the restart"),
    ("ncboot.banner_order", "banner_s4", "FAIL", "S4: missing"),
    ("ncota.relay_target_nosession", "ack_ok_no_session", "FAIL", "expected {'DATA': (0, 1)"),
    # recover() itself calls this rung a success: its fallback PING answers, since the app never went down. Only the
    # uptime tells.
    ("ncota.recovery_hard_reset", "no_reset", "FAIL", "the chip did not restart"),
    ("ncota.local_full_same_image", "old_slot", "FAIL", "came back on the old slot"),
    ("ncota.local_begin_abort_timeout", "reaper_refresh", "FAIL", "rejected chunks refreshed the window"),
    ("ncboot.new_peer_after_boot", "grace_fixed", "PASS", ""),
    ("ncboot.mesh_reboot", "reboot_acked", "PASS", ""),
    ("ncboot.boardtype2_mismatch", "boardtype_clamped", "PASS", ""),
)


def t_ncboot_mutations(tmp):
    """The s46/s47 tests catch what they exist to catch: against a NaviBootModel broken in one way each - debug flags that
    outlive a restart, a banner with the wrong S4 baud, a relayed DATA ACKed OK with no session (the #70 twin), a
    USB-Serial/JTAG reset that does nothing, a bootloader that refuses the new image, rejected chunks that keep a local
    session alive - the test fails and says why; and with each D-NC fix in the model (the new-peer grace, an ACKed and
    deferred mesh REBOOT, boardType clamped on input) its (should) test passes. Each runs alone on a fresh model."""
    for tid, mut, want, why in NCBOOT_MUTATIONS:
        res, model, _ = _run_boot_suite(tmp, ids={tid}, mut={mut}, tag=mut)
        r = res[tid]
        assert r["status"] == want and why in r["detail"], (tid, mut, r["status"], r["detail"][:300])
        if mut == "boardtype_clamped":
            assert model.restarts == [], f"a clamped boardType still restarted NaviCore: {model.restarts}"


# ---------------------------------------------------------------------------- NC-WP6: NaviCore on the mesh (s43)
def t_ncmesh_protocol_helpers(tmp):
    """hil/ncmesh.py's ports of what NaviCore prints and advertises, against cases worked from the firmware: wdp_scrub
    (WCB_Mgmt.h:169-174) and json_strip (rc_telemetry.h:340-346); rterm_pieces against CaptureSink's byte-wise wrap
    (navicore_rterm.h:48-62) - 159, 160, 161 and 320 bytes, an empty line, a two-byte character across the cut;
    port_labels against NaviModel.labels (NC-WP1's port of rcSerialLabel) on the bench config and on one with a user
    label, a serial HCR and DFPlayer and a Maestro label, then the WLED rule and the 24-character cap; local_maestro_ids'
    dedup; status_rows on a USB, a bridged positional and a sparse reply; stats_rows leaving out 'Reported by Other
    Nodes'; bulk_frames' sizes, key order, base64 round trip, hash override and refusals."""
    import base64 as b64
    from hil import ncmesh as M
    from hil.navicore import fnv1a32
    assert M.wdp_scrub("a,b]c\x01d") == "a_b_c_d"
    assert M.json_strip('a"b\\c\x02d') == "abcd"
    for n, want in ((159, [159]), (160, [160]), (161, [160, 1]), (320, [160, 160]), (0, [])):
        got = M.rterm_pieces(["x" * n])
        assert [len(p.encode()) for p in got] == want, (n, [len(p) for p in got])
    assert M.rterm_pieces(["a", "", "b\r\n"]) == ["a", "b"]
    cut = M.rterm_pieces(["x" * 159 + "é" + "y"])      # the 2-byte e-acute straddles byte 160
    assert len(cut) == 2 and cut[0].startswith("x" * 159) and cut[0].endswith("�") and cut[1] == "�y", cut
    m = NaviModel()
    cfg = json.loads(m.text())
    assert M.port_labels(cfg) == m.labels() == {4: "Maestro"}, (M.port_labels(cfg), m.labels())
    m.merge({"serialLabels": {"S4": "Dome lights", "maestro": "Dome"}, "hcrDest": {"transport": "serial", "port": "S3"},
             "dfpDest": {"transport": "serial", "port": "S5"}})
    cfg = json.loads(m.text())
    assert M.port_labels(cfg) == m.labels() == {1: "HCR", 2: "Dome lights", 3: "DFPlayer", 4: "Dome"}, \
        (M.port_labels(cfg), m.labels())
    cfg["dfpDest"] = {"transport": "off", "port": "S5"}
    cfg["wledSlots"] = [{"id": 2, "port": 5, "wcb": 0, "configured": True}]
    cfg["serialLabels"] = {"S4": "L" * 30}
    assert M.port_labels(cfg) == {1: "HCR", 2: "L" * 24, 3: "WLED", 4: "Maestro"}, M.port_labels(cfg)
    assert M.local_maestro_ids({"maestros": [{"type": 1, "device": 1}, {"type": 2, "device": 2}, {"type": 1, "device": 1},
                                             {"type": 1, "device": 5}]}) == [1, 5]
    assert M.local_maestro_ids({}) == []
    usb = {"online": [1, 0, 1], "known": [1, 1, 1], "clients": [0, 0, 1], "temporary": [0, 0, 1], "aliases": ["A", "", ""],
           "portLabels": [["x", "", "", "", ""], ["", "", "", "", ""], ["", "", "", "", ""]], "seqHash": [5, 0, 0]}
    r = M.status_rows(usb)
    assert r[1] == {"online": 1, "known": 1, "client": 0, "temporary": 0, "alias": "A",
                    "labels": ["x", "", "", "", ""], "seq": 5} and r[3]["temporary"] == 1, r
    r = M.status_rows({"sys": 1, "type": "WCB_STATUS", "relay": 1, "online": [1, 1], "known": [1, 1], "clients": [0, 0]})
    assert r[2] == {"online": 1, "known": 1, "client": 0, "temporary": None, "alias": None, "labels": None,
                    "seq": None}, r
    r = M.status_rows({"rows": [[1, 1, 0, 0], [16, 1, 1, 1]]})
    assert set(r) == {1, 16} and (r[16]["client"], r[16]["temporary"], r[16]["alias"]) == (1, 1, None), r
    text = ["--- WCB2 ESP-NOW Statistics (Since Last Reboot) ---",
            "--------------- ETM Per-Board Statistics ---------------",
            "WCB1: Sent: 5, ACKd: 5, Retries: 0, Failed: 0, Online (last seen 2s ago)",
            "WCB20 (special): Sent: 1, ACKd: 1, Retries: 0, Failed: 0, OFFLINE",
            "------------- Reported by Other Nodes -------------",
            "WCB20: Sent: 9, ACKd: 9, Retries: 0, Failed: 0, Unguaranteed: 0, Bcast: 3, Recv: 4  (7s ago)"]
    assert M.stats_rows(text) == {1: "Online", 20: "OFFLINE"}, M.stats_rows(text)
    data = bytes(range(256)) * 2 + b"tail"                  # 516 bytes: 6 chunks, the last 36
    begin, parts, done = M.bulk_frames(data, 4242)
    assert begin.startswith('{"bb":4242,') and json.loads(begin) == {"bb": 4242, "n": 6, "t": 516, "h": fnv1a32(data),
                                                                      "g": "cmdlib"}, begin
    assert len(parts) == 6 and all(p.startswith('{"bc":4242,') for p in parts), parts[:1]
    assert b"".join(b64.b64decode(json.loads(p)["s"]) for p in parts) == data
    assert [json.loads(p)["q"] for p in parts] == list(range(6)) and max(len(p) for p in parts) <= M.ENV_MAX_BYTES - 12
    assert done(2) == '{"bd":4242,"r":2}'
    assert json.loads(M.bulk_frames("x" * 96, 1, hash_=7)[0]) == {"bb": 1, "n": 1, "t": 96, "h": 7, "g": "cmdlib"}
    for bad in ((b"", 1), (b"x", 0), (b"x", 65536), (b"x" * (96 * 512 + 1), 1)):
        try:
            M.bulk_frames(*bad)
        except ValueError:
            continue
        raise AssertionError(f"bulk_frames accepted {len(bad[0])} bytes / sid {bad[1]}")


def _nmm_strip(text):
    return "".join(c for c in text if c not in '"\\' and ord(c) >= 0x20)


def _nmm_bracket_value(text):
    """The JSON object or array after '"data":' up to its matching bracket, as NaviCore's USB SET_CMDLIB takes it
    (NaviCore.ino:3902-3935), or ""."""
    k = text.find('"data":')
    s = k + 7
    while 0 <= k and s < len(text) and text[s].isspace():
        s += 1
    if k < 0 or s >= len(text) or text[s] not in "{[":
        return ""
    close, depth, in_str, esc = "}" if text[s] == "{" else "]", 0, False, False
    for i in range(s, len(text)):
        ch = text[i]
        if esc:
            esc = False
        elif in_str:
            esc, in_str = ch == "\\", ch != '"'
        elif ch == '"':
            in_str = True
        elif ch == text[s]:
            depth += 1
        elif ch == close:
            depth -= 1
            if depth == 0:
                return text[s:i + 1].strip()
    return ""


class NaviMeshModel(NaviModel):
    """NaviCore's mesh side for the s43 suite, on NaviModel's USB console: the JSON bridge and its fragment layer
    (rc_telemetry.h handle() :2032-2460, the pool :160-227, _applyReassembled :1097-1237, the parked RESET_DEFAULTS and
    WCB_SEND :1512-1546), the String- and file-backed fragment senders paced 150 ms (:557-733, :946-970), GET_WCB_META
    (:1986-2030), the bulk sink (WCB_Client.cpp:959-1172, rc_telemetry.h:775-860), the remote terminal (onWCBCommand
    NaviCore.ino:3025-3126, drainRemoteCli :5010-5024, CaptureSink navicore_rterm.h:48-86 with its byte-wise 160 wrap), and
    W1's side of it: the 20 s relay window any ';W20,{' opens (WCB.ino:8019-8021), the relay it gates (:5513-5520), the
    [TERM:20] lines it prints and drops when empty (WCB_RemoteTerm.cpp:178-207). W1's S2 is a byte buffer that ';S2'
    writes land in; W2 is a console that stores and reads back sequences. As today's firmware: a bridged WCB_SEND answers
    ok:true whatever the send did and a fragmented one is dropped (D-NC27), the strip leaves wifiEnabled (D-NC18),
    RESET_DEFAULTS resets the identity too (D-NC16) and the RTERM reply uses the RAM password (D-NC17), and a sequence
    value loses its quotes (D-NC46). `mut` fixes each finding, or breaks one behaviour a test exists to catch."""
    ALIASES = ("Body", "Dome")
    PORT_LABELS = (["", "Maestro S2", "", "", "Kyber"], ["HCR", "", "WLED 1", "", ""])
    SEQHASH = (0x1234ABCD, 0x0BADF00D)

    def __init__(self, mut=()):
        super().__init__()
        self.mut = set(mut)
        self.w1s2 = bytearray()
        self.window_until = 0.0
        self.pool = []
        self.next_sid = 1
        self.w2_seqs = {}
        self.rx = {}
        self.bulk = None
        self.w2 = None
        self.frames_t0 = time.monotonic()
        self.send_spans = []

    # ------------------------------------------------------------ plumbing
    def sbus_frames(self):
        """#L09's frame counter: about 111 frames a second, as on the bench; with 'sbus_starved', 40 a second while a
        fragment send runs (a sender that held loop() off the SBUS reader)."""
        now = time.monotonic()
        frames = 111 * (now - self.frames_t0)
        if "sbus_starved" in self.mut:
            frames -= sum(71 * max(0.0, min(b, now) - a) for a, b in self.send_spans if now > a)
        return 1000 + int(frames)

    def hash_cmd(self, text):
        return [re.sub(r"frames=\d+", f"frames={self.sbus_frames()}", x) for x in super().hash_cmd(text)]

    def _later(self, delay, fn, *args):
        t = threading.Timer(delay, fn, args)
        t.daemon = True
        t.start()

    def relay(self, line):
        """W1 prints a JSON line from the mesh only inside its relay window."""
        if time.monotonic() < self.window_until:
            self.w1._append(line)

    def to_sender(self, sender, obj):
        text = obj if isinstance(obj, str) else json.dumps(obj, separators=(",", ":"))
        if sender == 1:
            self.relay(text)

    def w1_command(self, cmd):
        """What W1 does with a command NaviCore sent it: only ';S2<text>' is modelled (its S2 probe wire)."""
        if cmd.upper().startswith(";S2"):
            self.w1s2 += (cmd[3:] + "\r").encode()

    def dispatch(self, a):
        out = super().dispatch(a)
        if a["type"] == "wcb_unicast" and a["target"] == "1":
            self.w1_command(a["cmd"])
        return out

    # ------------------------------------------------------------ W1 and W2
    def w1_script(self, text, n):
        if text.startswith(";S0,"):
            return [text[4:]]
        if text == "?WDP,DUMP":
            return NaviModel.w1_script(self, text, n)
        m = re.match(r"^;W20,(.*)$", text, re.S)
        if not m:
            return []
        body = m.group(1)
        if body.startswith("{"):
            self.window_until = time.monotonic() + 20.0
        self.mesh(1, body)
        return []

    def w2_script(self, text, n):
        if text.startswith(";S0,"):
            return [text[4:]]
        m = re.match(r"^\?SEQ,SAVE,([^,]+),(.*)$", text)
        if m:
            self.w2_seqs[m.group(1)] = m.group(2)
            return [f"Stored: Key='{m.group(1)}' ({len(m.group(2))} chars)"]
        m = re.match(r"^\?SEQ,GET,(.+)$", text)
        if m:
            k = m.group(1)
            return [f"[MGMT:SEQVAL,2]{k},OK,{self.w2_seqs[k]}" if k in self.w2_seqs else f"[MGMT:SEQVAL,2]{k},NOTFOUND,"]
        return []

    # ------------------------------------------------------------ NaviCore's USB, where the mesh model needs more
    def roster(self):
        return {"quantity": 1, "self": 20, "online": [1, 1], "known": [1, 1], "clients": [0, 0], "temporary": [0, 0],
                "aliases": list(self.ALIASES), "portLabels": [list(x) for x in self.PORT_LABELS],
                "seqHash": list(self.SEQHASH)}

    def wdp_dump(self):
        rows = super().wdp_dump()
        wide = ("[WDP:N=14,CLIENT=1,ALIAS=HILProbe,HW=0,HWREV=wcb_probe-2,FW=wcb_probe-2,CAP=0000,CTRL=0,CAPTAGS="
                + "hil " * 20 + ",MAESTRO=-,AGE=7,SEEN=1,PEER=0]")
        return rows[:-2] + [wide, rows[-2], "[WDP:END,count=3]"]

    def json_line(self, line):
        obj, err = self.parse_header(line)
        t = obj.get("type") if isinstance(obj, dict) else None
        if t == "GET_WCB_STATUS":
            return [json.dumps(dict({"type": "WCB_STATUS"}, **self.roster()), separators=(",", ":"))]
        if t == "WCB_SEND":
            tgt, cmd = _nm_pick(obj, "target", 0), _nm_pick(obj, "cmd", "")
            if 1 <= tgt <= 20:
                if tgt in (1, 2):
                    if tgt == 1:
                        self.w1_command(cmd)
                    return ['{"type":"ACK","ok":true}']
                return ['{"type":"ACK","ok":false,"msg":"send refused by WCB_Client"}']
        if t == "GET_WCB_SEQVAL" and _nm_pick(obj, "wcb", 0) == 2 and _nm_pick(obj, "key", ""):
            key = obj["key"]
            status, value = (0, self.w2_seqs[key]) if key in self.w2_seqs else (1, "")
            if "seq_escape" not in self.mut:
                value = _nmm_strip(value)
            self.nav.later(0.2, json.dumps({"sys": 1, "type": "WCB_SEQVAL", "ok": True, "wcb": 2, "key": key,
                                            "status": status, "value": value}, separators=(",", ":")))
            return []
        return super().json_line(line)

    # ------------------------------------------------------------ onWCBCommand and the bridge
    def mesh(self, sender, text):
        if text.startswith(('{"bb":', '{"bc":', '{"bd":')):
            self.bulk_in(sender, text)
            return
        self.rx[sender] = self.rx.get(sender, 0) + 1
        if self.handle(sender, text):
            return
        if text[:1] in ("?", "#"):
            self.remote_cli(sender, text[:199])
            return
        if self.flags & 0x01:
            self.nav._append(f"[WCB RX] from WCB{sender}: {text}")

    def remote_cli(self, sender, text):
        lines = self.hash_cmd(text) if text.startswith("#") else self.cli(text)
        self.nav._append(*lines)
        if self.c["wcbNetwork"]["password"] != self.ident["pw"]:
            return                                    # the RTERM reply carries the RAM password: W1 drops it (D-NC17)
        for x in lines:
            data = x.encode("utf-8")
            if "rterm_no_wrap" in self.mut:
                chunks = [data] if data else []
            else:
                chunks = [data[i:i + 160] for i in range(0, len(data), 160)]
            for c in chunks:
                if c:
                    self.w1._append(f"[TERM:20]{c.decode('utf-8', errors='replace')}")

    def handle(self, sender, text):
        if not text.startswith("{"):
            return False
        try:
            doc = json.loads(text)
        except ValueError:
            return False
        if not isinstance(doc, dict):
            return False
        if "f" in doc and "of" in doc and "sid" in doc:
            self.fragment(sender, doc)
            return True
        t = doc.get("type") if isinstance(doc.get("type"), str) else ""
        if not t:
            return False
        if t.startswith("rc_"):
            return True
        if t == "PING":
            self.to_sender(sender, {"sys": 1, "type": "PONG", "id": 20, "version": self.FW, "model": 0, "mode": self.mode})
            return True
        if t in ("START_MONITOR", "STOP_MONITOR", "SET_DEBUG_FLAGS", "CALIB"):
            return True
        if t in ("SET_CONFIG", "SET_CMDLIB"):
            self.nav._append(f"[RC] {t} → deferred to main loop")
            self.apply(sender, text)
            return True
        if t == "TEST_ACTION":
            self.test_action(sender, text)
            return True
        if t == "GET_CMDLIB_META":
            lib = self.cmdlib or ""
            self.to_sender(sender, {"sys": 1, "type": "CMDLIB_META", "size": len(lib.encode()),
                                    "hash": self._fnv(lib) if lib else 0})
            return True
        if t == "GET_CMDLIB":
            lib = self.cmdlib or '{"boards":[],"enums":{}}'
            self.frag_send(sender, f'{{"type":"CMDLIB","size":{len(lib.encode())},"hash":{self._fnv(lib)},"data":{lib}}}',
                           "CMDLIB", file_backed=True)
            return True
        if t == "GET_WCB_META":
            r = self.roster()
            self.frag_send(sender, json.dumps({"sys": 1, "type": "WCB_META", "aliases": r["aliases"],
                                               "portLabels": r["portLabels"], "seqHash": r["seqHash"]},
                                              separators=(",", ":")), "WCB_META")
            return True
        if t == "WCB_SEND":
            self.wcb_send(sender, doc)
            return True
        if t == "RESET_DEFAULTS":
            import copy
            old = copy.deepcopy(self.c)
            self.c = self.defaults()
            if "keep_identity" in self.mut:
                for k in ("wcbNetwork", "wcbProfiles", "boardType", "wifiEnabled", "wifiSsid", "wifiPassword"):
                    self.c[k] = old[k]
            fx = [] if "reset_no_effects" in self.mut else (self.side_effects() or [])
            self.nav._append(*fx, f"[RC] RESET_DEFAULTS from W{sender} → live config reset to factory defaults "
                                  f"(not persisted)")
            self.to_sender(sender, {"sys": 1, "type": "ACK", "of": "RESET_DEFAULTS", "ok": True})
            return True
        if t in ("REBOOT", "FORGET_PEER", "SET_MODE", "TRIGGER"):
            raise AssertionError(f"a test sent NaviCore {t} over the mesh")
        self.nav._append(f"[RC] Unknown inbound type '{t}' from WCB{sender}")
        return False

    def wcb_send(self, sender, doc):
        tgt = doc.get("target") if isinstance(doc.get("target"), int) else -1
        cmd = doc.get("cmd") if isinstance(doc.get("cmd"), str) else ""
        ok = 0 <= tgt <= 20 and bool(cmd)
        sent = ok and tgt in (1, 2)                  # the model's peers; anything else esp_now refuses
        if sent and tgt == 1:
            self.w1_command(cmd)
        if "wcb_send_fixed" in self.mut:
            ok = sent
        self.to_sender(sender, {"sys": 1, "type": "ACK", "of": "WCB_SEND", "ok": ok})

    def fragment(self, sender, doc):
        from hil import ncmesh as M
        f, of, sid = doc.get("f"), doc.get("of"), doc.get("sid")
        if not all(isinstance(v, int) for v in (f, of, sid)):
            return
        if f < 1 or of < 1 or of > 192 or f > of or sid == 0:
            return
        now = time.monotonic()
        self.pool = [p for p in self.pool if p["expire"] > now]
        sess = next((p for p in self.pool if p["sid"] == sid and p["sender"] == sender), None)
        if sess is None:
            if len(self.pool) >= (4 if "pool_4" in self.mut else 3):
                self.nav._append("[RC] Fragment pool exhausted — dropping")
                return
            sess = {"sid": sid, "sender": sender, "total": of, "parts": {}, "expire": now + M.FRAG_TIMEOUT_S}
            self.pool.append(sess)
        if of != sess["total"]:
            return
        sess["parts"].setdefault(f, doc.get("s") or "")
        if "no_refresh" not in self.mut:
            sess["expire"] = now + M.FRAG_TIMEOUT_S
        if len(sess["parts"]) >= sess["total"]:
            full = "".join(sess["parts"][k] for k in range(1, sess["total"] + 1))
            self.pool.remove(sess)
            self.nav._append(f"[RC] frag sid={sid} COMPLETE, deferring {len(full.encode())} bytes to main loop")
            self.apply(sender, full)

    def apply(self, sender, text):
        """_applyReassembled (rc_telemetry.h:1097-1237)."""
        try:
            doc = json.loads(text)
        except ValueError:
            self.nav._append("[RC] reassembled JSON parse failed")
            return
        t = doc.get("type") if isinstance(doc, dict) else None
        if t == "SET_CONFIG":
            data = doc.get("data")
            if not isinstance(data, dict):
                self.nav._append("[RC] reassembled SET_CONFIG missing 'data' object")
                return
            wnet = data.get("wcbNetwork")
            if isinstance(wnet, dict):
                if "deviceId" in wnet and "no_devid_line" not in self.mut:
                    self.nav._append(f"[RC] SET_CONFIG: ignoring incoming wcbNetwork.deviceId={wnet['deviceId']} "
                                     f"(WCB-transport saves can't change our own slot)")
                for k in ("deviceId", "macOct2", "macOct3", "password", "quantity") + \
                        (("channel",) if "strip_radio" in self.mut else ()):
                    wnet.pop(k, None)
                if not wnet:
                    data.pop("wcbNetwork")
            if "strip_radio" in self.mut:
                for k in ("wifiEnabled", "wifiSsid", "wifiPassword"):
                    data.pop(k, None)
            self.merge(data)
            fx = self.side_effects()
            self.nav._append(self.save(), "[RC] SET_CONFIG → applied + saved to LittleFS", *(fx or []))
            self.to_sender(sender, {"sys": 1, "type": "ACK", "of": "SET_CONFIG", "id": 20, "ok": True,
                                    "saveId": doc.get("saveId", 0)})
        elif t == "SET_CMDLIB":
            k, end = text.find('"data":'), text.rfind("}")
            lib = text[k + 7:end].strip() if k >= 0 and end > k + 7 else ""
            if "cmdlib_bracket" in self.mut:
                lib = _nmm_bracket_value(text)
            if lib:
                self.cmdlib = lib
            self.nav._append(f"[RC] SET_CMDLIB → {'saved to LittleFS' if lib else 'SAVE FAILED / empty'}")
            self.to_sender(sender, {"sys": 1, "type": "ACK", "of": "SET_CMDLIB", "ok": bool(lib),
                                    "size": len(lib.encode()), "hash": self._fnv(lib) if lib else 0})
        elif t == "TEST_ACTION":
            self.test_action(sender, text)
        elif t == "WCB_SEND" and "wcb_send_fixed" in self.mut:
            self.wcb_send(sender, doc)
        else:
            self.nav._append(f"[RC] reassembled payload had unexpected type '{t}' — dropping")

    def test_action(self, sender, text):
        try:
            doc = json.loads(text)
        except ValueError:
            doc = {}
        a = self.action_from(doc.get("action")) if isinstance(doc.get("action"), dict) else None
        ok = a is not None and not (a["type"] == "wcb_unicast" and not (a["target"].isdigit()
                                                                        and 1 <= int(a["target"]) <= 20))
        if ok:
            self.nav._append(*self.dispatch(a))
        # The ACK reaches W1 about 0.1 s after the action's marker reaches W1 S2 (run 20260929-025701).
        self._later(0.1, self.to_sender, sender, {"sys": 1, "type": "ACK", "of": "TEST_ACTION", "ok": ok})

    def frag_send(self, sender, payload, what, file_backed=False):
        """_startFragSend / _startFragSendFile and the pump: code-point-safe slices (143 escaped bytes and 160 raw, or 80
        raw from a file), one envelope per 150 ms, START and COMPLETE lines."""
        data = payload.encode("utf-8")
        cuts, i = [0], 0
        while i < len(data):
            take, esc = 0, 0
            while i + take < len(data):
                c = data[i + take]
                cp = 1 if c < 0x80 else 2 if c >> 5 == 6 else 3 if c >> 4 == 0xE else 4 if c >> 3 == 0x1E else 1
                if file_backed:
                    if take + cp > 80:
                        break
                else:
                    cost = sum(2 if b in (0x22, 0x5C, 8, 9, 10, 12, 13) else 6 if b < 0x20 else 1
                               for b in data[i + take:i + take + cp])
                    if esc + cost > 143 or take + cp > 160:
                        break
                    esc += cost
                take += cp
            i += take or 1
            cuts.append(i)
        sid, n = self.next_sid, len(cuts) - 1
        self.next_sid += 1
        self.send_spans.append((time.monotonic(), time.monotonic() + 0.15 * (n - 1) + 0.02))
        self.nav._append(f"[RC] {what} {'file-send' if file_backed else 'send'} START: {len(data)} bytes → {n} "
                         f"fragments to W{sender} (sid={sid})")
        for k in range(n):
            env = json.dumps({"f": k + 1, "of": n, "sid": sid, "s": data[cuts[k]:cuts[k + 1]].decode("utf-8")},
                             separators=(",", ":"), ensure_ascii=False)
            self._later(0.15 * k, self.to_sender, sender, env)
        self._later(0.15 * (n - 1) + 0.02, self.nav._append, f"[RC] send COMPLETE: {n} fragments (sid={sid})")

    def bulk_in(self, sender, text):
        """WCB_Client's bulk receiver and rc_telemetry.h's sink, all chunks in order or not."""
        import base64
        from hil.navicore import fnv1a32
        o = json.loads(text)
        if "bb" in o:
            n, size, h = o.get("n", 0), o.get("t", 0), o.get("h", 0)
            if not (1 <= n <= 512 and (n - 1) * 96 < size <= n * 96) or o.get("g") != "cmdlib":
                self.to_sender(sender, {"bs": o["bb"], "done": 1, "ok": 0, "hash": 0, "r": 0})
                return
            self.bulk = {"sid": o["bb"], "n": n, "t": size, "h": h, "got": {}}
            self.nav._append(f"[RC] bulk cmdlib begin: sid={o['bb']} {size} bytes / {n} chunks")
            return
        b = self.bulk
        sid = o.get("bc", o.get("bd"))
        if not b or b["sid"] != sid:
            self.to_sender(sender, {"bs": sid, "nb": 1})
            return
        if "bd" in o:
            miss = [q for q in range(b["n"]) if q not in b["got"]][:20]
            self.to_sender(sender, {"bs": sid, "got": len(b["got"]), "r": o.get("r", 0), "miss": miss})
            return
        b["got"].setdefault(o["q"], base64.b64decode(o["s"]))
        if len(b["got"]) < b["n"]:
            return
        blob = b"".join(b["got"][q] for q in range(b["n"]))[:b["t"]]
        ok = fnv1a32(blob) == b["h"] or "bulk_no_verify" in self.mut
        if ok:
            self.cmdlib = blob.decode("utf-8")
            self.nav._append(f"[RC] bulk cmdlib sid={sid} published ({len(blob)} bytes, hash {b['h']})")
        else:
            self.nav._append(f"[RC] bulk cmdlib sid={sid} hash mismatch (got {fnv1a32(blob)} want {b['h']}, "
                             f"{len(blob)} B) — discarded")
        self.bulk = None
        self.to_sender(sender, {"bs": sid, "done": 1, "ok": 1 if ok else 0, "hash": b["h"], "r": 0})


MESH_MODEL_IDS = ("ncmesh.bridged_wcb_meta", "ncmesh.bridged_set_config", "ncmesh.bridged_set_config_strip",
                  "ncmesh.bridged_reset_defaults", "ncmesh.bridged_reset_keeps_identity", "ncmesh.bridged_cmdlib",
                  "ncmesh.bridged_cmdlib_keys_after_data", "ncmesh.fragment_reassembly_edges",
                  "ncmesh.bridged_wcb_send_findings", "ncmesh.bridged_usb_only_types",
                  "ncmesh.remote_cli_order_and_drop", "ncmesh.wdp_port_labels", "ncmesh.seqval_verbatim")
MESH_SHOULD = {"ncmesh.bridged_set_config_strip", "ncmesh.bridged_reset_keeps_identity",
               "ncmesh.bridged_cmdlib_keys_after_data", "ncmesh.bridged_wcb_send_findings", "ncmesh.seqval_verbatim"}


def _run_mesh_suite(tmp, ids=MESH_MODEL_IDS, mut=(), tag="all"):
    """The s43 tests named in `ids`, whole, through the runner, against a fresh NaviMeshModel(mut) behind NaviCore, W1
    and W2 (NaviCore's fragment timeout cut to 1 s, the suite's W1 S2 wire the model's buffer, config_guard a no-op) ->
    (results by id, the model, session.log)."""
    import contextlib
    from hil import ncmesh as M
    saved_reg = list(runner.REGISTRY)
    try:
        runner.REGISTRY[:] = []
        sys.modules.pop("suites.s43_navicore_mesh", None)
        import suites.s43_navicore_mesh as S43
        mine = [dict(t) for t in runner.REGISTRY if t["id"] in ids]
    finally:
        runner.REGISTRY[:] = saved_reg
    for t in mine:
        t["needs"], t["links"], t["drives"], t["_drives"] = [], [], [], set()
    model = NaviMeshModel(mut=mut)
    b = tmp.bench({"wcb1": {"port": "COMW1", "kind": "wcb", "wcb": 1}, "wcb2": {"port": "COMW2", "kind": "wcb", "wcb": 2},
                   "navicore": {"port": "COMNAV", "kind": "navicore"}})
    nav, w1, w2 = (FakeNaviDev(model.script, "navicore"), FakeNaviDev(model.w1_script, "wcb1"),
                   FakeNaviDev(model.w2_script, "wcb2"))
    model.nav, model.w1, model.w2 = nav, w1, w2
    nav.log = w1.log = w2.log = b.log
    b.dev = lambda name: {"navicore": nav, "wcb1": w1, "wcb2": w2}[name]
    patches = [(M, "FRAG_TIMEOUT_S", 1.0), (S43, "link", lambda bench, w, p: FakeLink(f"W{w}{p}", model.w1s2)),
               (S43, "config_guard", lambda bench, *w: contextlib.nullcontext({n: [] for n in w}))]
    saved = [(mod, name, getattr(mod, name)) for mod, name, _ in patches]
    for mod, name, value in patches:
        setattr(mod, name, value)
    saved_g = _fast_guard()
    try:
        ck = new_run(b, mine)
    finally:
        _slow_guard(saved_g)
        for mod, name, value in saved:
            setattr(mod, name, value)
        model.monitor = False
    log = read(os.path.join(ck.out_dir, "session.log"))
    b.close()
    return {r["id"]: r for r in ck.data["results"]}, model, log


def t_ncmesh_suite_against_model(tmp):
    """The bridge half of s43 - thirteen tests - run whole, through the runner, against NaviMeshModel: each normal test
    passes and each (should) test fails, naming its D-NC, as on today's NaviCore; the model ends with the config and
    command library it started with (nc_guard put them back after RESET_DEFAULTS, a bridged save and the library
    writes, bulk transfers among them); session.log carries no credential. The rest of s43 needs timing the model does
    not keep (a 50 s offline window, W1 reboots, a 60 s heartbeat) or the probe; t_ncmesh_protocol_helpers covers the
    helpers they share."""
    probe = NaviMeshModel()
    orig = (probe.flash, probe.cmdlib)
    res, model, log = _run_mesh_suite(tmp)
    bad = [f"{tid}: {r['status']} (expected {'FAIL' if tid in MESH_SHOULD else 'PASS'}) {r['detail'][:400]}"
           for tid, r in res.items() if r["status"] != ("FAIL" if tid in MESH_SHOULD else "PASS")]
    assert len(res) == len(MESH_MODEL_IDS), sorted(res)
    assert not bad, "\n".join(bad)
    for tid in MESH_SHOULD:
        assert "(should, D-NC" in res[tid]["detail"], (tid, res[tid]["detail"][:200])
    assert (model.flash, model.cmdlib) == orig, "the model's saved config or command library changed"
    assert model.text() == orig[0], "the model's live config is not the saved one"
    assert not any(s in log for s in SECRETS), "a credential reached session.log"


# (test, model mutation, the status it must then get, what its detail must say): each D-NC fix its (should) test asks
# for, and a break of a behaviour each of five normal tests exists to catch.
NCMESH_MUTATIONS = (
    ("ncmesh.bridged_set_config_strip", "strip_radio", "PASS", ""),
    ("ncmesh.bridged_reset_keeps_identity", "keep_identity", "PASS", ""),
    ("ncmesh.bridged_wcb_send_findings", "wcb_send_fixed", "PASS", ""),
    ("ncmesh.bridged_cmdlib_keys_after_data", "cmdlib_bracket", "PASS", ""),
    ("ncmesh.seqval_verbatim", "seq_escape", "PASS", ""),
    ("ncmesh.fragment_reassembly_edges", "no_refresh", "FAIL", "s apart: the action ran 0 time(s)"),
    ("ncmesh.fragment_reassembly_edges", "pool_4", "FAIL", "no 'Fragment pool exhausted' line"),
    ("ncmesh.bridged_set_config", "no_devid_line", "FAIL", "no 'deviceId ignored' line"),
    ("ncmesh.remote_cli_order_and_drop", "rterm_no_wrap", "FAIL", "came back as pieces of"),
    ("ncmesh.bridged_cmdlib", "bulk_no_verify", "FAIL", "with a wrong hash"),
    ("ncmesh.bridged_cmdlib", "sbus_starved", "FAIL", "SBUS frames a second across the transfer"),
    ("ncmesh.bridged_reset_defaults", "reset_no_effects", "FAIL", "the live side effects did not run"),
)


def t_ncmesh_mutations(tmp):
    """The s43 tests the model runs catch what they exist to catch: with each D-NC fix in the model (radio fields
    stripped, the identity kept through RESET_DEFAULTS, WCB_SEND reporting the send and handling fragments, a bridged
    library taken by bracket matching, sequence values escaped) its (should) test passes; and broken one way each - a
    fragment session whose deadline is not renewed, a pool of four, no deviceId line, RTERM without its 160-byte wrap, a
    bulk sink that publishes whatever the hash, a fragment send that starves the SBUS reader, a mesh RESET_DEFAULTS
    without its side effects - the test fails and says why. Each runs alone on a fresh model."""
    for tid, mut, want, why in NCMESH_MUTATIONS:
        res, _, _ = _run_mesh_suite(tmp, ids=(tid,), mut={mut}, tag=mut)
        r = res[tid]
        assert r["status"] == want and why in r["detail"], (tid, mut, r["status"], r["detail"][:300])


# ---------------------------------------------------------------------------- NaviCore device transports (s44)
class DevLink:
    """A probe wire as s44 reads one (hil.links.Link): the bytes the model writes, mark() offsets, bursts stamped with
    the host clock in ms (s41 _probe_ms reads them as the probe's), expect() that waits and raises AssertionError."""

    def __init__(self, key, tap=False):
        self.key, self.tap = key, tap
        self.buf, self.chunks = bytearray(), []
        self._lock = threading.Lock()

    def write(self, data):
        if data:
            with self._lock:
                self.chunks.append((len(self.buf), int(time.monotonic() * 1000), bytes(data)))
                self.buf += data

    def mark(self):
        with self._lock:
            return len(self.buf)

    def received(self, since):
        with self._lock:
            return bytes(self.buf[since or 0:])

    def bursts(self, since):
        with self._lock:
            return [(ms, c[max(0, since - off):]) for off, ms, c in self.chunks if off + len(c) > since]

    def expect(self, data, timeout=3.0, since=None):
        deadline = time.monotonic() + timeout
        while data not in self.received(since):
            if time.monotonic() >= deadline:
                raise AssertionError(f"{self.key}: {data!r} never arrived")
            time.sleep(0.01)


class HcrPy:
    """WcbCmd's HcrCodec (WcbHcr.cpp:7-122), as NaviCore's local HCR and W2 both run it: normalize, then format, with the
    V/A/B volume shadow that SetVolume and the all-channel steps (0-100) keep. fmt() -> the bytes as text, "" when
    refused."""
    E, A = "HSMC", "VAB"

    def __init__(self):
        self.vol = [50, 50, 50]

    def fmt(self, fn, chan, track):
        if not _nm_hcr_ok(fn, chan, track):
            return ""
        E, A = self.E, self.A
        if fn in (2, 3, 4):
            return f"<O{E[chan]}{track},QE{E[chan]}>\n" if fn == 2 else f"<S{E[chan]}{track},QE{E[chan]},QT>\n"
        if fn == 7:
            return f"<MN{chan},MX{track}>\n"
        if fn == 10:
            return f"<O{chan},QO>\n"
        if fn == 13:
            return f"<M{track},QM>\n"
        if fn == 14:
            return f"<C{A[chan]}{track:04d},QP{A[chan]}>\n"
        if fn == 16:
            return f"<PS{A[chan]},QP{A[chan]}>\n"
        if fn == 17:
            for c in (range(3) if chan == 3 else [chan]):
                self.vol[c] = track
            return "".join(f"<PV{A[c]}{track}>\n" for c in (range(3) if chan == 3 else [chan]))
        if fn in (18, 19):
            step = (track or 5) * (-1 if fn == 19 else 1)
            self.vol = [max(0, min(100, v + step)) for v in self.vol]
            return "".join(f"<PV{A[c]}{self.vol[c]}>\n" for c in range(3))
        return {5: "<SE,QT>\n", 6: "<MM>\n", 8: "<PSV,QT>\n<PSV,QPV>\n<PSA,QPA>\n<PSB,QPB>\n", 9: "<PSV,QT>\n",
                11: "<OR,QE>\n", 20: "<PSG>\n", 21: "<PSG>\n<PSA,QPA>\n<PSB,QPB>\n"}[fn]


def _nm_hcr_local(codec, fn, chan, track, cap=99):
    """What executeHcrAction's local branch sends for a non-fade action (NaviCore.ino:1652-1725): a per-channel
    VOLUP/VOLDN (chan 1-3 = V/A/B) is a SetVolume made from the codec's shadow and clamped 0-`cap` (99 today, :1712,
    D-NC57); the rest is HcrCodec's format."""
    if fn in (18, 19) and 1 <= chan <= 3 and _nm_hcr_ok(fn, chan, track):
        c = chan - 1
        return codec.fmt(17, c, max(0, min(cap, codec.vol[c] + (track or 5) * (-1 if fn == 19 else 1))))
    return codec.fmt(fn, chan, track)


def _nm_fade_now(codec, fn, ch, base):
    """HcrFade::start with 0 s on channel ch (1 = A, 2 = B) (WcbCmd WcbHcrFade.cpp): a FadeIn is one SetVolume to `base`;
    a FadeOut is SetVolume 0, StopWAV, then `base` back."""
    if fn == 12:
        return codec.fmt(17, ch, base)
    return codec.fmt(17, ch, 0) + codec.fmt(16, ch, 0) + codec.fmt(17, ch, base)


def _nm_hcr_wcb(fn, chan, track):
    """The ;H command executeHcrAction's WCB branch sends (NaviCore.ino:1610-1650): ;H,FADEIN/FADEOUT on A or B for a
    fade (:1628-1635), else HcrCodec::normalize and hcrFormatWcbCommand (:1499-1555); "" when refused."""
    if fn in (12, 15):
        return f";H,{'FADEIN' if fn == 12 else 'FADEOUT'},{'A' if chan == 1 else 'B'},{track}" if chan in (1, 2) else ""
    if not _nm_hcr_ok(fn, chan, track):
        return ""
    emo = "HSMC"[chan] if 0 <= chan <= 3 else "?"
    vab = "VAB"[chan] if 0 <= chan <= 2 else "V"
    lv = "STRONG" if track >= 1 else "MOD"
    if fn in (18, 19):
        return (";H,VOLUP" if fn == 18 else ";H,VOLDN") + (f",{'VAB'[chan - 1]}" if 1 <= chan <= 3 else "") + \
            (f",{track}" if track > 0 else "")
    return {2: f";H,SETEMOTION,{emo},{track}", 3: f";H,TRIGGER,{emo},{lv}", 4: f";H,STIM,{emo},{lv}", 5: ";H,OVERLOAD",
            6: ";H,MUSE", 7: f";H,MUSE,GAP,{chan},{track}", 8: ";H,STOP", 9: ";H,STOPEMOTE", 10: f";H,OVERRIDE,{chan}",
            11: ";H,RESETEMOTIONS", 13: f";H,MUSE,{track}",
            14: f";H,FN,14,0,{track}" if chan == 0 else f";H,PLAY,{vab},{track}",
            16: f";H,FN,16,0,{track}" if chan == 0 else f";H,STOPWAV,{vab}",
            17: f";H,VOL,{track}" if chan == 3 else f";H,VOL,{vab},{track}"}.get(fn, f";H,FN,{fn},{chan},{track}")


class W2HcrPy:
    """W2's ;H handler (WCB_HCR.cpp processHCRRuntimeCommand :428-650) for the commands NaviCore sends, over its own
    HcrPy: what W2 writes to its HCR port (s15's hcr.verbs_* and hcr.fn_codec). run() takes what follows ';H,'."""
    CH = {"V": 0, "A": 1, "B": 2}

    def __init__(self, link):
        self.codec, self.link = HcrPy(), link

    def run(self, body):
        f = body.split(",")
        v, c, CH = f[0].upper(), self.codec, self.CH
        arg = lambda i: f[i] if len(f) > i else ""          # noqa: E731
        out = ""
        if v == "FN":
            out = c.fmt(_nm_toint(arg(1)), _nm_toint(arg(2)), _nm_toint(arg(3)))
        elif v in ("STIM", "TRIGGER"):
            out = f"<S{arg(1)}{1 if arg(2) == 'STRONG' else 0},QE{arg(1)},QT>\n"
        elif v == "SETEMOTION" and arg(1) in ("H", "S", "M", "C"):
            out = c.fmt(2, "HSMC".index(arg(1)), _nm_toint(arg(2)))       # over 99: dropped, as the library does
        elif v in ("OVERLOAD", "RESETEMOTIONS", "STOP", "STOPEMOTE"):
            out = c.fmt({"OVERLOAD": 5, "RESETEMOTIONS": 11, "STOP": 8, "STOPEMOTE": 9}[v], 0, 0)
        elif v == "OVERRIDE":
            out = c.fmt(10, 1 if arg(1) in ("1", "ON") else 0, 0)
        elif v == "MUSE":
            out = c.fmt(6, 0, 0) if len(f) == 1 else c.fmt(7, _nm_toint(arg(2)), _nm_toint(arg(3))) \
                if arg(1) == "GAP" else c.fmt(13, 0, 1 if arg(1) in ("1", "ON") else 0)
        elif v in ("PLAY", "STOPWAV") and arg(1) in ("A", "B"):
            out = c.fmt(14, CH[arg(1)], _nm_toint(arg(2))) if v == "PLAY" else c.fmt(16, CH[arg(1)], 0)
        elif v == "VOL":
            out = c.fmt(17, CH[arg(1)], _nm_toint(arg(2))) if arg(1) in CH else c.fmt(17, 3, _nm_toint(arg(1)))
        elif v in ("VOLUP", "VOLDN"):
            one = arg(1) in CH
            step = (_nm_toint(arg(2) if one else arg(1)) or 5) * (1 if v == "VOLUP" else -1)
            out = "".join(c.fmt(17, ch, max(0, min(100, c.vol[ch] + step))) for ch in ([CH[arg(1)]] if one else range(3)))
        elif v in ("FADEIN", "FADEOUT") and arg(1) in ("A", "B") and _nm_toint(arg(2)) <= 0:
            out = _nm_fade_now(c, 12 if v == "FADEIN" else 15, CH[arg(1)], c.vol[CH[arg(1)]])
        self.link.write(out.encode())


def _nm_mp3_verb(fn, track):
    """mp3FormatCommand (NaviCore.ino:1734-1760), the verb after ';A,' -> "" when refused."""
    return {1: f"PLAY,{track}" if 1 <= track <= 255 else "", 2: f"PLAYFS,{track}" if 0 <= track <= 255 else "",
            3: "STOP", 4: "NEXT", 5: "PREV", 6: f"VOL,{track}" if 0 <= track <= 64 else "", 7: "VOLUP",
            8: "VOLDN"}.get(fn, "")


def _nm_dfp_verb(fn, chan, track):
    """dfpFormatCommand (NaviCore.ino:1838-1880), the verb after ';D,' -> "" when refused."""
    return {1: f"PLAY,{track}" if 1 <= track <= 2999 else "",
            2: f"FOLDER,{chan},{track}" if 1 <= chan <= 99 and 1 <= track <= 255 else "",
            3: f"MP3FOLDER,{track}" if 1 <= track <= 9999 else "", 4: "STOP", 5: "NEXT", 6: "PREV", 7: "PAUSE",
            8: "RESUME", 9: f"VOL,{track}" if 0 <= track <= 30 else "", 10: "VOLUP", 11: "VOLDN",
            12: f"LOOP,{track}" if 1 <= track <= 2999 else "", 13: f"LOOPALL,{chan}" if 0 <= chan <= 1 else "",
            14: f"LOOPFOLDER,{chan}" if 1 <= chan <= 99 else "", 15: "RANDOM", 16: f"EQ,{chan}" if 0 <= chan <= 5 else "",
            17: f"DEVICE,{chan}" if 1 <= chan <= 5 else "", 18: "RESET"}.get(fn, "")


class Mp3Py:
    """WcbCmd's Mp3Codec::handle (WcbMp3.cpp:32-95) for a verb (what follows ';A,') -> the MP3 Trigger bytes, or None:
    'v' <volume> before every play, VOLUP/VOLDN 5 at a time (a lower number is louder)."""

    def __init__(self, vol=20):
        self.vol = vol

    def handle(self, verb):
        U = verb.upper()
        if U.startswith(("PLAY,", "PLAYFS,")):
            fs = U.startswith("PLAYFS,")
            n = _nm_toint(verb[7 if fs else 5:])
            return bytes([0x76, self.vol, 0x70 if fs else 0x74, n]) if (0 if fs else 1) <= n <= 255 else None
        if U in ("STOP", "NEXT", "PREV"):
            return {"STOP": b"O", "NEXT": b"F", "PREV": b"R"}[U]
        if U.startswith("VOL,"):
            if not 0 <= _nm_toint(verb[4:]) <= 64:
                return None
            self.vol = _nm_toint(verb[4:])
        elif U in ("VOLUP", "VOLDN"):
            self.vol = max(0, self.vol - 5) if U == "VOLUP" else min(64, self.vol + 5)
        else:
            return None
        return bytes([0x76, self.vol])


class DfpPy:
    """WcbCmd's DfPlayerCodec::handle (WcbDfPlayer.cpp:51-171) for a verb (what follows ';D,') -> the 10-byte frame, or
    None: VOLUP/VOLDN are absolute SetVolume frames 2 apart, clamped 0-30."""
    SIMPLE = {"STOP": 0x16, "NEXT": 0x01, "PREV": 0x02, "PAUSE": 0x0E, "RESUME": 0x0D, "RANDOM": 0x18, "RESET": 0x0C}
    ONE = (("LOOPALL,", 0x11, 0, 1), ("LOOPFOLDER,", 0x17, 1, 99), ("LOOP,", 0x08, 1, 2999), ("EQ,", 0x07, 0, 5),
           ("DEVICE,", 0x09, 1, 5), ("PLAY,", 0x03, 1, 2999), ("MP3FOLDER,", 0x12, 1, 9999))

    def __init__(self, vol=20):
        self.vol = vol

    @staticmethod
    def frame(cmd, param=0):
        body = bytes([0xFF, 0x06, cmd, 0x00, (param >> 8) & 0xFF, param & 0xFF])
        ck = -sum(body) & 0xFFFF
        return b"\x7e" + body + bytes([ck >> 8, ck & 0xFF, 0xEF])

    def handle(self, verb):
        U = verb.upper()
        if U in self.SIMPLE:
            return self.frame(self.SIMPLE[U])
        if U.startswith("FOLDER,"):
            fo, _, tr = verb[7:].partition(",")
            ok = tr and 1 <= _nm_toint(fo) <= 99 and 1 <= _nm_toint(tr) <= 255
            return self.frame(0x0F, _nm_toint(fo) << 8 | _nm_toint(tr)) if ok else None
        if U.startswith("VOL,") or U in ("VOLUP", "VOLDN"):
            if U.startswith("VOL,"):
                if not 0 <= _nm_toint(verb[4:]) <= 30:
                    return None
                self.vol = _nm_toint(verb[4:])
            else:
                self.vol = min(30, self.vol + 2) if U == "VOLUP" else max(0, self.vol - 2)
            return self.frame(0x06, self.vol)
        for pre, cmd, lo, hi in self.ONE:
            if U.startswith(pre):
                n = _nm_toint(verb[len(pre):])
                return self.frame(cmd, n) if lo <= n <= hi else None
        return None


def _nm_wled(body):
    """WcbWled::build (WcbCmd WcbWled.cpp) for the verbs s09's table uses -> the JSON, "" for an unknown verb."""
    f = [x.strip() for x in body.split(",")]
    v = f[0].upper()
    num = lambda i: _nm_toint(f[i]) if len(f) > i else 0      # noqa: E731
    if v in ("ON", "OFF", "TOGGLE"):
        return {"ON": '{"on":true}', "OFF": '{"on":false}', "TOGGLE": '{"on":"t"}'}[v]
    if v == "BRI" and len(f) > 1:
        return '{"bri":%d}' % max(0, min(255, num(1)))
    if v in ("PS", "PAL") and len(f) > 1:
        return '{"ps":%d}' % num(1) if v == "PS" else '{"seg":[{"pal":%d}]}' % num(1)
    if v == "COL" and len(f) > 1 and len(f[1].lstrip("#")) in (6, 8):
        h = f[1].lstrip("#")
        return '{"seg":[{"col":[[%s]]}]}' % ",".join(str(int(h[i:i + 2], 16)) for i in range(0, len(h), 2))
    if v == "FX" and len(f) > 1:
        extra = "".join(',"%s":%d' % (k, max(0, min(255, num(i)))) for i, k in ((2, "sx"), (3, "ix")) if len(f) > i)
        return '{"seg":[{"fx":%d%s}]}' % (num(1), extra)
    if v == "JSON" and "," in body:
        return body.split(",", 1)[1]
    return ""


class FakeMaestro:
    """NaviCore's Maestro 1 on Serial2: per channel a target and a speed (S x 100 quarter-us a second, 0 = none); a
    channel that was off (target 0) jumps to its first target; getPosition, getMovingState and getErrors as ?MAE reads
    them (reading the errors clears them)."""

    def __init__(self):
        self.ch, self.err = {}, 0

    def _s(self, c):
        return self.ch.setdefault(c, {"from": 0, "to": 0, "t0": 0.0, "speed": 0})

    def pos(self, c):
        s = self._s(c)
        if not (s["to"] and s["speed"] and s["from"]):
            return s["to"]
        d = s["speed"] * 100 * (time.monotonic() - s["t0"])
        if d >= abs(s["to"] - s["from"]):
            return s["to"]
        return int(s["from"] + (d if s["to"] > s["from"] else -d))

    def moving(self):
        return int(any(self.pos(c) != s["to"] for c, s in list(self.ch.items())))

    def read_err(self):
        e, self.err = self.err, 0
        return e

    def frame(self, f):
        if any(b >= 0x80 for b in f[3:]):
            self.err |= 0x10                     # a command-range byte inside a frame: a serial protocol error
        elif f[2] in (0x04, 0x07):
            s = self._s(f[3])
            s.update({"from": self.pos(f[3]), "t0": time.monotonic()})
            s["to" if f[2] == 0x04 else "speed"] = f[4] | f[5] << 7


class NaviDevModel(NaviModel):
    """NaviModel plus the device paths s44 drives, from NaviCore hil-week 6925773 and the WcbCmd 0.9.1 it compiles:
    executeMaestroCmd with its 35-character copy, casts and clamps (NaviCore.ino:1307-1390); maestroWrite to Serial2
    (a FakeMaestro) or into the broadcast WCBStream (177 bytes, sent before a frame that would not fit and at the end
    of the command), whose packets both Maestro_Remote WCBs write to their S1 (the W1 S1 probe and the W2 S1 tap); the
    easing rules (setEasing's re-apply and two repeats 500 ms apart that re-send only positive limits, restartScript's
    own/switch/override/Off, the re-apply and repeats after every config save, :1106-1250); ?MAE on a local slot and on
    a remote one (;M<dev>,<verb> read by W2's Maestro 2, the marker printed later and mirrored to W1 when the read came
    over the bridge); the HCR, MP3 Trigger, DFPlayer and WLED on either transport (ports of HcrCodec, Mp3Codec,
    DfPlayerCodec and WcbWled, and of W2's handlers; a local fade ticked from a thread that loop() stops once hcrDest is
    not local); a serial action whose ACK waits out a bit-banged write; the 4-deep mesh-to-serial queue and the
    broadcast fan-out; #L90, #L20/#L21 and DBG_WIRE of a hook image. `mut` breaks the model in one way, or applies a
    D-NC fix, by name (NCDEV_MUTATIONS; see where each is read)."""
    FORBIDDEN = tuple(rx for rx in NaviModel.FORBIDDEN if rx.pattern != r"^#[Ll]2[01]")
    CAP = 177
    LINKS = ("W1S1", "W2S1", "W2S2", "W2S3", "W2S4", "W2S5")

    def __init__(self, mut=()):
        self.mut = set(mut)
        self.lock = threading.RLock()
        super().__init__()
        self.links = {k: DevLink(k, tap=k == "W2S1") for k in self.LINKS}
        self.w2 = self.w2hcr = self.w2audio = None
        self.stream, self.spd, self.acc, self.sw_ease = bytearray(), {}, {}, [-1] * 8
        self.mae1, self.m2, self.w2vars = FakeMaestro(), {"pos": 6000, "mov": 0, "err": 0}, {}
        self.hcr, self.mp3, self.dfp, self.fades = HcrPy(), Mp3Py(20), DfpPy(20), {}
        self.fwd, self.stall_until, self._relay, self.block_s, self.alive = [], 0.0, False, 0.0, True
        threading.Thread(target=self._fade_loop, daemon=True).start()

    def label(self, port):
        """auxPortLabel (NaviCore.ino:274-279)."""
        if port not in ("S3", "S4", "S5"):
            return port
        i = int(port[1]) - 3
        return f"Serial {i + 1 if self.c['boardType'] == 0 else i + 3}"

    # ------------------------------------------------------------ Maestro frames
    def wire(self, port, data):
        """navihil::wire: '[WIRE] <port> <off>/<len>: <hex>', one line per 48 bytes of a block, under DBG_WIRE."""
        if not self.flags & 0x80 or not data:
            return []
        return [f"[WIRE] {port} {o}/{len(data)}: {data[o:o + 48].hex(' ').upper()}" for o in range(0, len(data), 48)]

    def flush(self):
        """WCBStream _flushBuffer: one Kyber broadcast, which W1 and W2 write to their S1."""
        if not self.stream:
            return []
        pkt = bytes(self.stream)
        self.stream.clear()
        for k in ("W1S1", "W2S1"):
            self.links[k].write(pkt)
        return [f"[WCBStream] Broadcast (Kyber) {len(pkt)} bytes — OK"]

    def mwrite(self, i, cmd, payload=b""):
        """maestroWrite (NaviCore.ino:647-701) -> (written, console lines)."""
        if not 1 <= i <= 8 or self.c["maestros"][i - 1]["type"] == 0:
            return False, []
        slot = self.c["maestros"][i - 1]
        frame = bytes([0xAA, slot["device"], cmd & 0x7F]) + bytes(payload)
        blocks = [frame] if cmd == 0xA7 or not payload else [frame[:3], bytes(payload)]
        if slot["type"] == 1:
            self.mae1.frame(frame)
            return True, [y for b in blocks for y in self.wire("Serial2", b)]
        out = []
        if self.CAP - len(self.stream) < len(frame) and "cut_mid_frame" not in self.mut:   # break: fill to the brim
            out += self.flush()
        for b in blocks:
            out += self.wire("WCBStream", b)
            for byte in b:
                if len(self.stream) >= self.CAP:
                    out += self.flush()
                self.stream.append(byte)
        return True, out

    def u(self, s, bits):
        """(uint8_t) / (uint16_t)atoi, as executeMaestroCmd casts (D-NC56); the 'no_alias' fix keeps the number."""
        v = _nm_toint(s)
        return v if "no_alias" in self.mut else v & ((1 << bits) - 1)

    def refuse(self, i, ch):
        return self.dlog(1, f"[DISPATCH] Maestro {i}: channel {ch} out of range (0-31) — skipped")

    def set_target(self, i, ch, pos):
        if ch > 31:
            return self.refuse(i, ch)
        pos = min(pos, 16383)
        return self.mwrite(i, 0x84, bytes([ch, pos & 0x7F, pos >> 7 & 0x7F]))[1]

    def set_speed(self, i, ch, spd):
        if ch > 31:
            return self.refuse(i, ch)
        spd = min(spd, 16383)
        ok, out = self.mwrite(i, 0x87, bytes([ch, spd & 0x7F, spd >> 7 & 0x7F]))
        if ok:
            self.spd[(i, ch)] = spd
        return out + (self.dlog(1, f"[DISPATCH] Maestro {i} ch {ch}  SetSpeed {spd}") if ok else [])

    def set_accel(self, i, ch, acc):
        if ch > 31:
            return self.refuse(i, ch)
        if acc > 255:                                    # reached only with the 'no_alias' fix
            return self.dlog(1, f"[DISPATCH] Maestro {i}: accel {acc} out of range (0-255) — skipped")
        ok, out = self.mwrite(i, 0x89, bytes([ch, acc & 0x7F, acc >> 7 & 0x7F]))
        if ok:
            self.acc[(i, ch)] = acc
        return out + (self.dlog(1, f"[DISPATCH] Maestro {i} ch {ch}  SetAccel {acc}") if ok else [])

    def invalidate(self, i):
        """maeSmoothInvalidateSlot (:1022-1025) on a script start or stop."""
        for k in [k for k in self.spd if k[0] == i]:
            self.spd[k] = None
        for k in [k for k in self.acc if k[0] == i]:
            self.acc[k] = None

    def entry(self, p, i, ch):
        return self.c["smooth"][p]["entries"].get((i, ch), (0, 0)) if 0 <= p < 6 else (0, 0)

    def knob_outs(self, i):
        """(knob, channel) for every passthrough output on Maestro slot i, each output set of a mode-aware knob."""
        for kn in self.c["knobs"]:
            if kn["function"] != 1:
                continue
            for outs in [kn["outputs"]] + ([kn["outputs2"], kn["outputs3"]] if kn["modeAware"] else []):
                for o in outs:
                    if o["target"] == i and o["maestroCh"] < 32:
                        yield kn, o["maestroCh"]

    def resolve(self, kn, i):
        """resolveKnobEasing (:1010-1015)."""
        own, sw = kn["smoothProfile"], self.sw_ease[i - 1]
        return (sw if kn["easeSwitchOverride"] and sw != -1 else own) if own >= 0 else sw

    def reapply(self, i):
        """reapplyMaestroEasing (:1147-1170): the effective easing, 0 when none, cache-gated."""
        out = []
        for kn, ch in self.knob_outs(i):
            eff = self.resolve(kn, i)
            s, a = self.entry(eff, i, ch) if eff >= 0 else (0, 0)
            if self.spd.get((i, ch), 0) != min(s, 16383):
                out += self.set_speed(i, ch, s)
            if self.acc.get((i, ch), 0) != a:
                out += self.set_accel(i, ch, a)
        return out

    def reassert(self, i):
        """reassertMaestroEasing (:1212-1233): only an effective profile's non-zero entries, unconditionally."""
        out = []
        for kn, ch in self.knob_outs(i):
            eff = self.resolve(kn, i)
            if eff < 0:
                if "repeat_zero" in self.mut:                  # break: a repeat that drives 0 like the re-apply
                    out += self.set_speed(i, ch, 0) + self.set_accel(i, ch, 0)
                continue
            if self.entry(eff, i, ch) != (0, 0):
                out += self.set_speed(i, ch, self.entry(eff, i, ch)[0]) + self.set_accel(i, ch, self.entry(eff, i, ch)[1])
        return out

    def schedule(self, i):
        """scheduleEasingRepeat (:1185-1194): EASE_REPEATS (2), EASE_REPEAT_MS (500) apart."""
        for k in (1, 2):
            t = threading.Timer(0.5 * k, self._repeat, (i,))
            t.daemon = True
            t.start()

    def _repeat(self, i):
        with self.lock:
            if self.alive:
                lines = self.reassert(i) + self.flush()
                if lines:
                    self.nav._append(*lines)

    def script_easing(self, i, use):
        """applyScriptEasing (:1106-1138): a profile's entries, or with Off every channel any profile manages zeroed."""
        out = []
        for ch in range(32):
            if 0 <= use < 6 and self.entry(use, i, ch) != (0, 0):
                out += self.set_speed(i, ch, self.entry(use, i, ch)[0]) + self.set_accel(i, ch, self.entry(use, i, ch)[1])
            elif use == -2 and any(self.entry(p, i, ch) != (0, 0) for p in range(6)):
                out += self.set_speed(i, ch, 0) + self.set_accel(i, ch, 0)
        return out

    def sub_ok(self, i, sub):
        """The D-NC23 fix ('msb_fixed') refuses a subroutine over 127; the D-NC56 fix one over 255."""
        if sub > 255 or (sub > 127 and "msb_fixed" in self.mut):
            return self.dlog(1, f"[DISPATCH] Maestro {i}: subroutine {sub} out of range — skipped")
        return None

    def mae_cmd(self, i, cmd):
        """executeMaestroCmd (:1307-1390): char buf[36], strtok on ','."""
        toks = [t for t in cmd[:35].split(",") if t]
        if not toks:
            return []
        tok, a = toks[0], toks[1:]
        if tok in ("goHome", "stopScript"):
            out = self.mwrite(i, 0xA2 if tok == "goHome" else 0xA4)[1]
            self.invalidate(i)
            return out
        if tok == "setTarget" and len(a) >= 2:
            return self.set_target(i, self.u(a[0], 8), self.u(a[1], 16))
        if tok == "setSpeed" and len(a) >= 2:
            return self.set_speed(i, self.u(a[0], 8), self.u(a[1], 16))
        if tok == "setAccel" and len(a) >= 2:
            return self.set_accel(i, self.u(a[0], 8), self.u(a[1], 8))
        if tok == "setSpeedAccel" and len(a) >= 3:
            ch = self.u(a[0], 8)
            return self.set_speed(i, ch, self.u(a[1], 16)) + self.set_accel(i, ch, self.u(a[2], 8))
        if tok == "setEasing":
            s = a[0] if a else ""
            self.sw_ease[i - 1] = -2 if s[:1] in ("o", "O") else \
                _nm_toint(s[1:]) if s[:1] in ("p", "P") and 0 <= _nm_toint(s[1:]) < 6 else -1
            out = self.reapply(i)
            self.schedule(i)
            return out
        if tok == "restartScript":
            sub = self.u(a[0], 8) if a else 0
            spec, ovr = (a[1] if len(a) > 1 else ""), (a[2] if len(a) > 2 else "")
            own = _nm_toint(spec[1:]) if spec[:1] in ("p", "P") and 0 <= _nm_toint(spec[1:]) < 6 else -1
            sw = self.sw_ease[i - 1]
            use = (sw if ovr[:1] in ("o", "O") and sw != -1 else own) if own >= 0 else sw
            out = self.script_easing(i, use)
            refused = self.sub_ok(i, sub)
            if refused is not None:
                return out + refused
            out += self.mwrite(i, 0xA7, bytes([sub]))[1]
            self.invalidate(i)
            return out
        if tok == "subParam" and len(a) >= 2:
            sub, par = self.u(a[0], 8), min(self.u(a[1], 16), 16383)
            refused = self.sub_ok(i, sub)
            if refused is not None:
                return refused
            out = self.mwrite(i, 0xA8, bytes([sub, par & 0x7F, par >> 7 & 0x7F]))[1]
            self.invalidate(i)
            return out
        return []

    # ------------------------------------------------------------ the devices
    def device_on(self, port):
        """auxPortHasDevice (:2946-2968)."""
        return any(self.c[k]["transport"] == 0 and self.c[k]["target"] == port for k in ("hcrDest", "mp3Dest", "dfpDest")) \
            or any(w["configured"] and w["remoteWCB"] == 0 and f"S{w['serialPort']}" == port for w in self.c["wledSlots"])

    def fade_start(self, port, fn, ch, sec):
        """HcrFade::start on local channel ch (1 = A, 2 = B): a FadeIn anchors at 0 and ramps to the level, a FadeOut
        ramps from the level to 0 and then sends StopWAV and the level back; 0 s is instant (_nm_fade_now)."""
        old = self.fades.pop(ch, None)
        base = old["restore"] if old else self.hcr.vol[ch]
        if sec <= 0:
            return self.wire(port, _nm_fade_now(self.hcr, fn, ch, base).encode())
        frm, to = (0, base) if fn == 12 else (self.hcr.vol[ch], 0)
        out = self.hcr.fmt(17, ch, frm) if frm != self.hcr.vol[ch] else ""
        self.fades[ch] = {"from": frm, "to": to, "t0": time.monotonic(), "dur": float(sec), "next": 0.0, "last": frm,
                          "stop": fn == 15, "restore": base, "port": port}
        return self.wire(port, out.encode())

    def _fade_loop(self):
        """loop()'s HcrFade tick (:5537-5552), 150 ms steps; a fade is cancelled in the pass that sees hcrDest is no
        longer local, so nothing follows the save that moved it. Lines are appended under the lock, in order."""
        while self.alive:
            time.sleep(0.05)
            with self.lock:
                if self.c["hcrDest"]["transport"] != 0 and "fade_not_cancelled" not in self.mut:   # break: keep going
                    self.fades.clear()
                lines, now = [], time.monotonic()
                for ch, f in list(self.fades.items()):
                    el = now - f["t0"]
                    if el >= f["dur"]:
                        out = self.hcr.fmt(17, ch, f["to"])
                        if f["stop"]:
                            out += self.hcr.fmt(16, ch, 0) + self.hcr.fmt(17, ch, f["restore"])
                        lines += self.wire(f["port"], out.encode())
                        del self.fades[ch]
                    elif now >= f["next"]:
                        f["next"] = now + 0.15
                        v = f["from"] + int((f["to"] - f["from"]) * el / f["dur"])
                        if v != f["last"]:
                            f["last"] = v
                            lines += self.wire(f["port"], self.hcr.fmt(17, ch, v).encode())
                if lines and self.nav is not None:
                    self.nav._append(*lines)

    def hcr_action(self, a):
        """executeHcrAction (:1600-1725): the WCB branch sends the ;H command (to W2HcrPy while s15's fixture holds it);
        the local branch writes the port through the tap and shows its payload in the trace."""
        d, fn, ch, tr = self.c["hcrDest"], a["fn"], a["chan"], a["track"]
        if d["transport"] == 2:
            return self.dlog(8, "[DISPATCH] HCR is disabled in config — action skipped")
        kind = "WCB" if d["transport"] == 1 else "Serial"
        if fn in (12, 15) and ch not in (1, 2):
            return self.dlog(8, f"[DISPATCH] HCR-{kind}: fade chan must be A(1)/B(2), got {ch} — skipped")
        if d["transport"] == 1:
            cmd = _nm_hcr_wcb(fn, ch, tr)
            if not cmd:
                return self.dlog(8, f"[DISPATCH] HCR-WCB: bad/unsupported fn={fn} chan={ch} track={tr} — skipped")
            if self.w2hcr is not None:
                self.w2hcr.run(cmd[3:])
            return self.dlog(8, f"[DISPATCH] HCR→WCB{d['target']}  {cmd}  OK")
        port = d["target"]
        if fn in (12, 15):
            return self.fade_start(port, fn, ch, tr) + \
                self.dlog(8, f"[DISPATCH] HCR→{port}  Fade{'In' if fn == 12 else 'Out'} ch={ch} {tr}s")
        level = min(tr, 1) if fn in (3, 4) and "level_same" in self.mut else tr          # the D-NC59 fix
        payload = _nm_hcr_local(self.hcr, fn, ch, level, cap=100 if "volstep_100" in self.mut else 99)  # D-NC57 fix
        if not payload:
            return self.dlog(8, f"[DISPATCH] HCR-Serial: bad/unsupported fn={fn} chan={ch} track={tr} — skipped")
        shown = [y for x in self.dlog(8, f"[DISPATCH] HCR→{port}  fn={fn} chan={ch} track={tr}  {payload}")
                 for y in x.rstrip("\n").split("\n")]
        return shown + self.wire(port, payload.encode())

    def audio_action(self, a, mp3):
        """executeMp3Action / executeDfpAction (:1776-1835, :1901-1958): one verb for both transports; local through the
        codec on the port's tap, remote as ;A / ;D to W2 (whose codec, while s15's fixture holds it, writes its S5)."""
        d = self.c["mp3Dest" if mp3 else "dfpDest"]
        bit, tag, fn, ch, tr = (0x10 if mp3 else 0x40), ("MP3" if mp3 else "DFP"), a["fn"], a["chan"], a["track"]
        if d["transport"] == 2:
            return self.dlog(bit, "[DISPATCH] MP3 Trigger is disabled in config — action skipped" if mp3 else
                             "[DISPATCH] DFPlayer is disabled in config — action skipped")
        verb = _nm_mp3_verb(fn, tr) if mp3 else _nm_dfp_verb(fn, ch, tr)
        if d["transport"] == 0:
            port, codec = d["target"], (self.mp3 if mp3 else self.dfp)
            if not verb:
                return self.dlog(bit, f"[DISPATCH] MP3-local: bad/out-of-range fn={fn} arg={tr} — skipped" if mp3 else
                                 f"[DISPATCH] DFP-local: bad/out-of-range fn={fn} chan={ch} track={tr} — skipped")
            out = codec.handle(verb) or b""
            ok = "OK" if out else "FAIL"
            return self.dlog(bit, f"[DISPATCH] MP3→{port}  fn={fn} arg={tr} vol={codec.vol}  {ok}" if mp3 else
                             f"[DISPATCH] DFP→{port}  fn={fn} chan={ch} track={tr} vol={codec.vol}  {ok}") + \
                self.wire(port, out)
        if not verb:
            return self.dlog(bit, f"[DISPATCH] {tag}: bad fn={fn} — skipped")
        if self.w2audio is not None and self.w2audio[0] == tag:
            self.links["W2S5"].write(self.w2audio[1].handle(verb) or b"")
        return self.dlog(bit, f"[DISPATCH] {tag}→WCB{d['target']}  ;{'A' if mp3 else 'D'},{verb}  OK")

    def w2_command(self, cmd):
        """W2 running a unicast from NaviCore: ;L1 is its WLED on S2; a command without its ';' is plain broadcast text
        (WCB.ino:6010-6020), out its S3-S5."""
        m = re.match(r"^;L(\d*),?(.*)$", cmd)
        if m:
            js = _nm_wled(m.group(2)) if m.group(1) == "1" else ""
            if js:
                self.links["W2S2"].write(js.encode() + b"\n")
        elif not cmd.startswith((";", "?")):
            for p in ("W2S3", "W2S4", "W2S5"):
                self.links[p].write(cmd.encode() + b"\r")

    def wled_action(self, a):
        """executeWledAction (:1960-2020)."""
        s = a["cmd"].lstrip(" \t")
        s = s[1:] if s.startswith(";") else s
        if s[:1] not in ("L", "l"):
            return self.dlog(4, f"[DISPATCH] WLED: '{a['cmd']}' is not a ;L command — skipped")
        m = re.match(r"^(\d*)(.*)$", s[1:])
        wid = _nm_toint(m.group(1)) if m.group(1) else 0
        if wid > 9:
            return self.dlog(4, f"[DISPATCH] WLED: id {wid} out of range (1-9) — skipped")
        body = m.group(2)[1:] if m.group(2).startswith(",") else m.group(2)
        slots = [w for w in self.c["wledSlots"] if w["configured"]]
        if wid == 0:
            local = [w for w in slots if w["remoteWCB"] == 0 and 3 <= w["serialPort"] <= 5]
            if not local:
                return self.dlog(4, "[DISPATCH] WLED: bare ;L but no LOCAL WLED configured — skipped")
            w = min(local, key=lambda x: x["wledID"])
        else:
            w = next((x for x in slots if x["wledID"] == wid), None)
            if w is None:
                return self.dlog(4, f"[DISPATCH] WLED {wid} not configured — skipped")
        if w["remoteWCB"] == 0:
            js, port = _nm_wled(body), f"S{w['serialPort']}"
            wires = self.wire(port, js.encode()) + self.wire(port, b"\n") if js else []
            return self.dlog(4, f"[DISPATCH] WLED {w['wledID']}→{port}  {body}  {'OK' if js else 'no-op'}") + wires
        cmd = f";L{wid},{body}" if "wled_semicolon" in self.mut else a["cmd"]         # the D-NC60 fix
        self.w2_command(cmd)
        return self.dlog(4, f"[DISPATCH] WLED {w['wledID']}→WCB{w['remoteWCB']}  {cmd}  OK")

    def serial_action(self, a):
        """RA_SERIAL (:2093-2100): the text and a CR, two blocks through the tap; writeS4/S5 block loop(), and so the USB
        ACK, for the line (D-NC58; the 'serial_paced' fix queues it instead)."""
        port, cmd = a["target"], a["cmd"]
        out = self.dlog(0x20, f"[DISPATCH] Serial TX [{self.label(port)}]  {cmd}")
        if port in ("S3", "S4", "S5"):
            out += self.wire(port, cmd.encode()) + self.wire(port, b"\r")
            if port != "S3" and "serial_paced" not in self.mut:
                self.block_s += (len(cmd.encode()) + 1) * 10 / self.c["auxBaud"][int(port[1]) - 3]
        return out

    def dispatch(self, a):
        t = a["type"]
        if t == "maestro":
            i = int(a["target"]) if a["target"].isdigit() else 0
            if not 1 <= i <= 8:
                return [f"WARN: Maestro action with invalid ID {i} (target='{a['target']}')"]
            return self.dlog(1, f"[DISPATCH] Maestro {i}  {a['cmd']}") + self.mae_cmd(i, a["cmd"])
        if t == "maestro_local":
            return self.dlog(1, f"[DISPATCH] Maestro (legacy local → ID 1)  {a['cmd']}") + self.mae_cmd(1, a["cmd"])
        if t == "hcr":
            return self.hcr_action(a)
        if t in ("mp3", "dfplayer"):
            return self.audio_action(a, t == "mp3")
        if t == "wled":
            return self.wled_action(a)
        if t == "serial":
            return self.serial_action(a)
        return super().dispatch(a)

    # ------------------------------------------------------------ NaviCore's console
    def script(self, text, n):
        with self.lock:
            return super().script(text, n)

    def json_line(self, line):
        obj, err = self.parse_header(line)
        t = obj.get("type") if isinstance(obj, dict) else None
        if t == "TEST_ACTION":
            a = self.action_from(obj.get("action")) if isinstance(obj.get("action"), dict) else None
            if a is None or (a["type"] == "wcb_unicast" and not (a["target"].isdigit() and 1 <= int(a["target"]) <= 20)):
                return ['{"type":"ACK","of":"TEST_ACTION","ok":false}']
            self.block_s = 0.0
            lines, ack = self.dispatch(a) + self.flush(), '{"type":"ACK","of":"TEST_ACTION","ok":true}'
            if self.block_s:
                self.nav.later(self.block_s, ack)             # the ACK after the action has run (:4017-4027)
                return lines
            return lines + [ack]
        out = super().json_line(line)
        if t == "SET_CONFIG" and out and '"ok":true' in out[-1]:
            extra = [y for i in range(1, 9) for y in self.reapply(i)] + self.flush()   # processSwitches :2466-2478
            for i in range(1, 9):
                self.schedule(i)
            out = out[:-1] + extra + out[-1:]
        return out

    def hash_cmd(self, text):
        m = re.match(r"^#[Ll](\d+)(?:,(.*))?$", text)
        fn = int(m.group(1)) if m else -1
        if fn == 90:
            ms = _nm_toint(m.group(2) or "0")
            if ms <= 0:
                return ["[HIL] #L90: loop() stalls 0 ms at its next pass", "[HIL] #L90: loop() resumed after 0 ms"]
            self.stall_until = time.monotonic() + ms / 1000
            t = threading.Timer(ms / 1000, self._resume, (ms,))
            t.daemon = True
            t.start()
            return [f"[HIL] #L90: loop() stalls {ms} ms at its next pass"]
        if fn in (20, 21):
            port = "S3" if fn == 20 else "S4"
            return [f"[HCR TEST] -> {port} : SetEmotion(HAPPY,80) via hcrFormatCommand + raw frame"] + \
                self.wire(port, HcrPy().fmt(2, 0, 80).encode()) + self.wire(port, b"<OH80,QEH>\n") + \
                ["[HCR TEST] sent — watch the HCR; check TX wiring to HCR RX, common ground"]
        return super().hash_cmd(text)

    def _resume(self, ms):
        with self.lock:
            self.stall_until = 0.0
            queued, self.fwd = self.fwd, []
            self.nav._append(f"[HIL] #L90: loop() resumed after {ms} ms",
                             *[y for fw, text in queued for y in self.write_fwd(fw, text)])

    def marker(self, slot, q, ch, val=None, err=None):
        body = f'"val":{val}' if err is None else f'"err":"{err}"'
        return f'[MAE:{slot}]{{"q":"pos","ch":{ch},{body}}}' if q == "pos" else f'[MAE:{slot}]{{"q":"{q}",{body}}}'

    def remote_read(self, slot, q, ch):
        """maestroBroadcastReadVerb (:703-745): W2 hosts Maestro 2, reads it (the request frame on its S1 tap), keeps the
        value in m2pos<ch>/m2moving/m2err and answers :MQR; the marker lands later (maePumpRemoteEmits :829-850), and
        on W1 too as [TERM:20] when the read came over the bridge (maeLatchRemoteRelay :802-804)."""
        dev = self.c["maestros"][slot - 1]["device"]
        if dev == 2:
            self.links["W2S1"].write(bytes([0xAA, 2, {"pos": 0x10, "mov": 0x13, "err": 0x21}[q]]) +
                                     (bytes([ch]) if q == "pos" else b""))
            val = self.m2[q]
            if q == "err":
                self.m2["err"] = 0
            self.w2vars[{"pos": f"m{dev}pos{ch}", "mov": f"m{dev}moving", "err": f"m{dev}err"}[q]] = val
            relay, mk = self._relay, self.marker(slot, q, ch, val)

            def land():
                self.nav._append(mk)
                if relay and "no_relay_marker" not in self.mut:                  # break: never mirrored to W1
                    self.w1._append(f"[TERM:20]{mk}")
            t = threading.Timer(0.05, land)
            t.daemon = True
            t.start()
        return self.dlog(1, f"[DISPATCH] Maestro {slot} remote read sent — awaiting :MQR")

    def cli(self, text):
        if text[:5].upper() != "?MAE,":
            return super().cli(text)
        p = [x.strip() for x in text[5:].split(",")]
        verb = p[0].upper()
        if verb == "FREE" and len(p) >= 3:
            slot, ch = _nm_toint(p[1]) & 0xFF, _nm_toint(p[2]) & 0xFF
            return self.set_speed(slot, ch, 0) + self.set_accel(slot, ch, 0) + self.flush()
        if verb in ("GET", "MOVING", "ERR") and len(p) >= 2:
            slot, q = _nm_toint(p[1]) & 0xFF, {"GET": "pos", "MOVING": "mov", "ERR": "err"}[verb]
            ch = _nm_toint(p[2]) & 0xFF if verb == "GET" and len(p) > 2 else 0
            kind = self.c["maestros"][slot - 1]["type"] if 1 <= slot <= 8 else 0
            if kind == 0:
                return [self.marker(slot, q, ch, err="disabled")]
            if kind == 2:
                return self.remote_read(slot, q, ch)
            val = self.mae1.pos(ch) if q == "pos" else self.mae1.moving() if q == "mov" else self.mae1.read_err()
            return [self.marker(slot, q, ch, val)]
        if len(p) >= 3 and p[0].isdigit():
            return self.set_target(_nm_toint(p[0]) & 0xFF, _nm_toint(p[1]) & 0xFF, _nm_toint(p[2]) & 0xFFFF) + self.flush()
        return []

    def wdp_dump(self):
        """W2's row lists the Maestro it hosts (MAESTRO=2)."""
        return [r.replace("MAESTRO=-,AGE=5", "MAESTRO=2,AGE=5") for r in super().wdp_dump()]

    # ------------------------------------------------------------ the mesh: W1, W2 and the serial bridge
    def write_fwd(self, fw, text):
        """auxTxPump's finished write (:5062-5105)."""
        port = f"S{fw}"
        return self.wire(port, text.encode() + b"\r") + \
            self.dlog(0x20, f"[DISPATCH] Serial TX [{self.label(port)}]  {text}")

    def forward(self, fw, text):
        """queueSerialFwd (:2937-2943, 4 deep, a full queue drops) while loop() is stalled; written at once otherwise."""
        if time.monotonic() < self.stall_until:
            if len(self.fwd) < (5 if "queue_5" in self.mut else 4):              # break: a 5-deep queue
                self.fwd.append((fw, text))
            return
        self.nav._append(*self.write_fwd(fw, text))

    def fan_out(self, text):
        """queueSerialBroadcastOut (:3100-3117): every port with serialBcast out that no device owns; never JSON."""
        if text.startswith("{") and "json_fanned" not in self.mut:                # break: JSON fanned out too
            return
        for i, p in enumerate(("S3", "S4", "S5")):
            if self.c["bcastOut"][i] and not self.device_on(p):
                self.forward(i + 3, text)

    def w1_script(self, text, n):
        with self.lock:
            m = re.match(r"^;W20,(.*)$", text)
            if m:
                body = m.group(1)
                if re.match(r"^;[sS][1-3]", body):
                    self.forward(int(body[2]) + 2, body[3:])
                    return []
                if body.startswith("?"):
                    self._relay = True
                    try:
                        return super().w1_script(text, n)
                    finally:
                        self._relay = False
                if body.startswith("{"):
                    self.fan_out(body)
                    return super().w1_script(text, n)
                if not body.startswith((";", "#")):
                    self.fan_out(body)                      # a unicast with no ';': handed to the broadcast fan-out
                return []
            if text and not text.startswith((";", "?", "#", "{")):
                self.fan_out(text)                          # plain text typed on W1 is a mesh broadcast
                return []
            return super().w1_script(text, n)

    def w2_script(self, text, n):
        m = re.match(r"^\?VAR,(GET|CLEAR),(\w+)$", text)
        if m and m.group(1) == "GET":
            v = self.w2vars.get(m.group(2))
            return [f"[VAR] {m.group(2)} = {v}" if v is not None else f"[VAR] {m.group(2)} not found"]
        if m:
            self.w2vars.pop(m.group(2), None)
            return [f"[VAR] Cleared {m.group(2)}"]
        return [text[4:]] if text.startswith(";S0,") else []


def t_ncdev_helpers(tmp):
    """The pure parts of s44: wire_log joins one port's [WIRE] blocks and names each block a lost line left short (a
    missing head, a missing middle, a missing tail) while other ports are ignored; kyber_packets reads the WCBStream's
    flush lines; stream_packets cuts a burst between frames only (32 six-byte frames and a subroutine frame: 174 + 22);
    pololu, frame_lengths and dev_stream build, measure and filter Pololu frames; hcr_payload reassembles a local HCR
    dispatch line whose payload spans lines; aux_label, free_profiles, profile_without and managed_channels read a
    config. And the tables agree with this file's ports of the WcbCmd codecs and of NaviCore's formatters - HcrCodec and
    W2's ;H handler, hcrFormatWcbCommand, Mp3Codec, DfPlayerCodec, WcbWled - so a typo in a table or a port shows here,
    with the DEVICE 2 regression frame equal to s15's _dfp; every mae_cases frame string is whole frames."""
    import suites.s44_navicore_devices as S
    from suites.s09_wled import CASES
    from suites.s15_hcr_mp3_dfp import _dfp
    a48, b12 = " ".join(["41"] * 48), " ".join(["42"] * 12)
    lines = [f"[WIRE] S4 0/60: {a48}", "noise", "[WIRE] S3 0/2: 00 01", f"[WIRE] S4 48/60: {b12}", "[WIRE] S4 0/1: 0D"]
    assert S.wire_log(lines, "S4") == (b"A" * 48 + b"B" * 12 + b"\r", []), S.wire_log(lines, "S4")
    assert S.wire_log(lines, "S3") == (b"\x00\x01", []) and S.wire_log(lines, "S5") == (b"", [])
    assert S.wire_log([lines[3], lines[4]], "S4") == (b"B" * 12 + b"\r",
                                                       ["S4: bytes 0-47 of a 60-byte block were never logged"])
    assert S.wire_log([lines[0], lines[4]], "S4")[1] == ["S4: a 60-byte block stopped at byte 48"]
    assert S.wire_log([lines[0]], "S4")[1] == ["S4: a 60-byte block stopped at byte 48"]
    assert S.wire_log(["[WIRE] WCBStream 0/3: AA 04 22"], "WCBStream") == (b"\xaa\x04\x22", [])
    assert S.kyber_packets(["[WCBStream] Broadcast (Kyber) 174 bytes — OK", "x",
                            "[WCBStream] Broadcast (Kyber) 22 bytes — FAIL"]) == [(174, "OK"), (22, "FAIL")]
    assert S.stream_packets([6] * 32 + [4]) == [174, 22] and S.stream_packets([6] * 29) == [174]
    assert S.stream_packets([6] * 30) == [174, 6] and S.stream_packets([3, 4]) == [7] and S.stream_packets([]) == []
    assert S.pololu(4, 0x84, 5, 6000) == bytes.fromhex("AA 04 04 05 70 2E")
    assert S.pololu(4, 0x27, 200) == b"\xaa\x04\x27\xc8" and S.pololu(2, 0x22) == b"\xaa\x02\x22"
    burst = S.pololu(4, 0x07, 5, 105) + S.pololu(2, 0x22) + S.pololu(4, 0x27, 0)
    assert S.frame_lengths(burst) == [6, 3, 4], S.frame_lengths(burst)
    assert S.dev_stream(burst, 4) == S.pololu(4, 0x07, 5, 105) + S.pololu(4, 0x27, 0)
    _raises(lambda: S.frame_lengths(b"\x01\x02\x03"), ValueError)
    trace = ["[DISPATCH] HCR→S4  fn=17 chan=3 track=40  <PVV40>", "<PVA40>", "<PVB40>", "[WIRE] S4 0/24: 3C",
             "[DISPATCH] HCR→S4  fn=5 chan=0 track=0  <SE,QT>"]
    assert S.hcr_payload(trace, "S4", 17, 3, 40) == "<PVV40>\n<PVA40>\n<PVB40>\n"
    assert S.hcr_payload(trace, "S4", 5, 0, 0) == "<SE,QT>\n" and S.hcr_payload(trace, "S3", 5, 0, 0) is None
    assert S.aux_label({"boardType": 0}, "S4") == "Serial 2" and S.aux_label({"boardType": 1}, "S5") == "Serial 5"
    cfg = {"smoothProfiles": [{"entries": [{"mid": 1, "ch": 0, "spd": 20, "acc": 3}]},
                              {"entries": [{"mid": 4, "ch": 0, "spd": 60, "acc": 0}]}, {"entries": []}, {"entries": []},
                              {"entries": [{"mid": 4, "ch": 7, "spd": 0, "acc": 9}]}, {"entries": []}],
           "knobs": {"J4": {"smoothProfile": 3}},
           "mappings": {"101": {"t1": [{"type": "maestro", "target": "4", "cmd": "restartScript,1,p5"}]}}}
    assert S.free_profiles(cfg) == [2], S.free_profiles(cfg)
    assert S.profile_without(cfg, 4) == 0 and S.profile_without(cfg, 1) == 1
    assert S.managed_channels(cfg, 4) == [0, 7] and S.managed_channels(cfg, 2) == []
    assert S.DFP_DEVICE_2 == _dfp(0x09, 2) == DfpPy.frame(0x09, 2)
    local, w2 = HcrPy(), W2HcrPy(DevLink("W2S4"))
    for fn, chan, track, verb, want in S.HCR_CASES:
        assert _nm_hcr_wcb(fn, chan, track) == verb, (fn, chan, track, _nm_hcr_wcb(fn, chan, track), verb)
        m = w2.link.mark()
        w2.run(verb[3:])
        assert w2.link.received(m) == want, (verb, w2.link.received(m), want)
        got = _nm_fade_now(local, fn, chan, local.vol[chan]) if fn in (12, 15) else _nm_hcr_local(local, fn, chan, track)
        assert got.encode() == want, ("local", fn, chan, track, got, want)
    for fn, chan, track, kind in S.HCR_REFUSED:
        assert not _nm_hcr_wcb(fn, chan, track), (fn, chan, track)
        assert (kind == "fade") == (fn in (12, 15)) and (kind == "fade" or not HcrPy().fmt(fn, chan, track))
    mp3 = Mp3Py(20)
    for fn, track, verb, hexb, vol in S.MP3_CASES:
        assert f";A,{_nm_mp3_verb(fn, track)}" == verb, (fn, track, verb)
        assert mp3.handle(verb[3:]) == bytes.fromhex(hexb) and mp3.vol == vol, (verb, mp3.vol)
    assert not any(_nm_mp3_verb(fn, track) for fn, track in S.MP3_REFUSED)
    dfp = DfpPy(20)
    assert len(S.DFP_VOLS) == len(S.DFP_CASES)
    for (fn, chan, track, verb, want), vol in zip(S.DFP_CASES, S.DFP_VOLS):
        assert f";D,{_nm_dfp_verb(fn, chan, track)}" == verb, (fn, chan, track, verb)
        assert dfp.handle(verb[3:]) == want and dfp.vol == vol, (verb, dfp.vol)
    assert not any(_nm_dfp_verb(fn, chan, track) for fn, chan, track in S.DFP_REFUSED)
    for verb, want in CASES:
        assert _nm_wled(verb).encode() + b"\n" == want, (verb, _nm_wled(verb))
    assert _nm_wled("BOGUS") == ""
    cases = S.mae_cases(4, 0)
    for cmd, want in cases:
        assert sum(S.frame_lengths(want)) == len(want) and all(d == 4 for d, *_ in S.pololu_frames(want)), cmd
    assert [c for c, _ in cases if len(c) > S.MAE_CMD_KEEP] == ["setTarget,5," + "0" * 20 + "6000"]


def _run_dev_suite(tmp, ids=None, mut=()):
    """A fake bench for s44: NaviDevModel(mut) behind NaviCore, W1 and W2, its DevLinks as the probe wires, s15's W2 device
    fixtures replaced by the model's own (W2HcrPy, Mp3Py, DfpPy on W2's S4/S5), config_guard by nothing (the model's
    W2 has no config to put back), W2's WLED 1 on S2 as bench config has it, every opt-in on. Runs the ncdev tests
    named in `ids` (default: all but ncdev.knob_local_readback, which needs the SBUS controller) through the runner and
    puts every patch back -> (results by id, the model, facts: the config text before, session.log, the count)."""
    import contextlib
    model = NaviDevModel(mut=mut)
    orig = model.text()
    b = tmp.bench({"wcb1": {"port": "COMW1", "kind": "wcb", "wcb": 1}, "wcb2": {"port": "COMW2", "kind": "wcb", "wcb": 2},
                   "navicore": {"port": "COMNAV", "kind": "navicore"}, "sbus": {"port": "COMS", "kind": "sbus"}})
    b.cfg["opt_in"] = list(optin.OPT_INS)
    nav, w1, w2 = (FakeNaviDev(model.script, "navicore"), FakeNaviDev(model.w1_script, "wcb1"),
                   FakeNaviDev(model.w2_script, "wcb2"))
    model.nav, model.w1, model.w2 = nav, w1, w2
    nav.log = w1.log = w2.log = b.log
    b.dev = lambda name: {"navicore": nav, "wcb1": w1, "wcb2": w2}[name]
    saved_reg = list(runner.REGISTRY)
    try:
        runner.REGISTRY[:] = []
        sys.modules.pop("suites.s44_navicore_devices", None)
        import suites.s44_navicore_devices as S44
        mine = [dict(t) for t in runner.REGISTRY if t["id"].startswith("ncdev.") and t["id"] != "ncdev.knob_local_readback"
                and (ids is None or t["id"] in ids)]
    finally:
        runner.REGISTRY[:] = saved_reg
    for t in mine:
        t["needs"], t["links"], t["drives"], t["_drives"] = [], [], [], set()

    @contextlib.contextmanager
    def hcr_w2(bench, port="S4", poll="OFF", debug=False):
        model.w2hcr = W2HcrPy(model.links["W2S4"])
        try:
            yield None
        finally:
            model.w2hcr = None

    @contextlib.contextmanager
    def w2_audio(bench, kind, cfg, port):
        model.w2audio = (kind, Mp3Py(20) if kind == "MP3" else DfpPy(20))
        try:
            yield None
        finally:
            model.w2audio = None
    patches = [(S44, "link", lambda bench, w, p: model.links[f"W{w}{p}"]), (S44, "_hcr_w2", hcr_w2),
               (S44, "_w2_audio", w2_audio), (S44, "config_guard", lambda bench, *w: contextlib.nullcontext()),
               (S44, "snapshot", lambda bench, n: ["?WLED,1:W2S2:115200"]), (S44, "_settle_maestro2", lambda w: None)]
    saved = [(mod, name, getattr(mod, name)) for mod, name, _ in patches]
    for mod, name, value in patches:
        setattr(mod, name, value)
    saved_g = _fast_guard()
    try:
        ck = new_run(b, mine)
    finally:
        _slow_guard(saved_g)
        for mod, name, value in saved:
            setattr(mod, name, value)
        model.alive = False
    log = read(os.path.join(ck.out_dir, "session.log"))
    b.close()
    return {r["id"]: r for r in ck.data["results"]}, model, dict(orig=orig, log=log, count=len(mine))


NCDEV_SHOULD = {"ncdev.mae_verb_no_alias": "D-NC56", "ncdev.mae_subroutine_msb": "D-NC23",
                "ncdev.hcr_local_volstep_cap": "D-NC57", "ncdev.hcr_level_same_both_ways": "D-NC59",
                "ncdev.serial_action_paced": "D-NC58", "ncdev.wled_forward_normalised": "D-NC60"}


def t_ncdev_suite_against_model(tmp):
    """Every ncdev test but ncdev.knob_local_readback (it needs the SBUS controller) run whole, through the runner,
    against NaviDevModel with every opt-in on: each normal and opt-in test passes, and each (should) test fails, as on
    today's NaviCore, naming its D-NC; NaviCore's config ends as it began, in RAM and saved (every nc_guard restored
    it); the local Maestro channel the servo tests moved is off again with no speed limit; no credential reached
    session.log."""
    res, model, x = _run_dev_suite(tmp)
    bad = [f"{tid}: {r['status']} (expected {'FAIL' if tid in NCDEV_SHOULD else 'PASS'}) {r['detail'][:400]}"
           for tid, r in res.items() if r["status"] != ("FAIL" if tid in NCDEV_SHOULD else "PASS")]
    assert len(res) == x["count"] == 22, (len(res), x["count"])
    assert not bad, "\n".join(bad)
    for tid, dnc in NCDEV_SHOULD.items():
        assert f"(should, {dnc})" in res[tid]["detail"], (tid, res[tid]["detail"][:200])
    assert model.text() == x["orig"] and model.flash == x["orig"], "the model's config was not left as found"
    assert model.mae1.pos(1) == 0 and model.mae1.ch[1]["speed"] == 0, model.mae1.ch.get(1)
    assert not any(s in x["log"] for s in SECRETS), "a credential reached session.log"


# (test, model mutation, the status it must then get, a piece of its detail): a break of the behaviour each test exists
# to catch, and each D-NC fix its (should) test asks for. The byte tables need no break of their own here: every row is
# compared byte-exact, t_ncdev_helpers checks each against its codec port, and the whole-suite run passes on them.
NCDEV_MUTATIONS = (
    ("ncdev.mae_remote_stream_exact", "cut_mid_frame", "FAIL", "expected [174, 22]"),
    ("ncdev.easing_repeat_frames", "repeat_zero", "FAIL", "setEasing,off"),
    ("ncdev.mae_remote_read", "no_relay_marker", "FAIL", "[TERM:20]"),
    ("ncdev.mesh_forward_burst", "queue_5", "FAIL", "expected the first 4"),
    ("ncdev.bcast_out_opt_in", "json_fanned", "FAIL", "declined JSON"),
    ("ncdev.hcr_local_payload", "fade_not_cancelled", "FAIL", "the fade kept writing S4"),
    ("ncdev.mae_verb_no_alias", "no_alias", "PASS", ""),
    ("ncdev.mae_subroutine_msb", "msb_fixed", "PASS", ""),
    ("ncdev.hcr_local_volstep_cap", "volstep_100", "PASS", ""),
    ("ncdev.hcr_level_same_both_ways", "level_same", "PASS", ""),
    ("ncdev.serial_action_paced", "serial_paced", "PASS", ""),
    ("ncdev.wled_forward_normalised", "wled_semicolon", "PASS", ""),
)


def t_ncdev_mutations(tmp):
    """The s44 tests catch what they exist to catch: against a NaviDevModel broken in one way each - a burst cut inside a
    frame, an easing repeat that drives 0, a remote read never mirrored to the bridge, a 5-deep forward queue, declined
    JSON fanned out, a local fade that outlives the save that moved the HCR - the test fails and says why; and with
    each D-NC fix in the model (D-NC56, D-NC23, D-NC57, D-NC59, D-NC58, D-NC60) its (should) test passes. Each runs
    alone on a fresh model."""
    for tid, mut, want, why in NCDEV_MUTATIONS:
        res, _, _ = _run_dev_suite(tmp, ids={tid}, mut={mut})
        r = res[tid]
        assert r["status"] == want and why in r["detail"], (tid, mut, r["status"], r["detail"][:300])


TESTS = [t_new_run_to_done, t_golden_report, t_pause_file_and_resume, t_stop, t_last_press_wins,
         t_cut_off_reruns_first, t_frozen_checkpoint_records_nothing, t_pretest_outage_gate, t_outage_auto_retry,
         t_load_cleanup_and_tmp_fallback, t_dropped_ids, t_find_resumable, t_lock_held_by_child_process,
         t_atomic_write_retry, t_redaction, t_run_busy, t_reidentify_never_guesses, t_identify_port_changed_chars,
         t_tmp_newer_than_main, t_finished_run_resumes_to_done, t_navicore_silent_blocks_on_navicore,
         t_passing_wire_marked_verified, t_sbus_released_after_cutoff, t_sbus_reply_nudged_by_ping,
         t_wizard_abort_kills_tree, t_nctool_pipe_bridge, t_cli_ask_and_handler,
         t_ctrl_c_during_checks_cancels, t_pause_file_old_mtime, t_redaction_free_text, t_added_tests_listed,
         t_finished_run_with_dropped, t_start_closes_recording_ports, t_vendored_softserial_in_lockstep,
         t_rule12_remoteterm_first, t_rule16_espnow_send_wrapped, t_probe_reboot_rebinds, t_runner_fails_test_on_probe_panic, t_probe_restart_forgets_only_what_it_lost,
         t_probe_port_reopen_counts_as_restart,
         t_durations, t_optin_gate_up_front, t_list_lines, t_no_servos, t_config_guard_auto_restore, t_ws_frames,
         t_nvs_parse, t_mgmt_pull_parts, t_mgmt_pull_noparts_and_codes, t_pull_over_limit_policy,
         t_backup_chain_parse, t_run_glued_sentinel, t_intellex_stage_filter, t_intellex_py_judge,
         t_intellex_stream_helpers, t_intellex_link_tap, t_intellex_handed_over, t_intellex_github_dir,
         t_nc_transport, t_nc_fnv1a, t_nc_pwm_update, t_nc_mae_markers, t_nc_clip_items, t_nc_recorder_transfer,
         t_nc_mesh_stats, t_nc_boot_banner, t_nc_wdp_views, t_nc_config_protocol, t_sbus_codec, t_sbus_ctl,
         t_nc_log_filter, t_nc_guard_ladder, t_nc_guard_state, t_nc_guard_persist_resume, t_nc_guard_bench_test,
         t_ncmesh_fragments, t_ncmesh_bridged_reassemble, t_ncmesh_burn_window, t_ncmesh_deaf_and_probe_peer,
         t_ncmesh_protocol_helpers, t_ncmesh_suite_against_model, t_ncmesh_mutations,
         t_nccfg_suite_against_model,
         t_ncflash_image_check, t_ncflash_libs, t_ncflash_build, t_ncflash_build_source, t_ncflash_status_parse,
         t_ncflash_flash,
         t_ncflash_flash_failures, t_ncflash_recover, t_ncflash_identity, t_ncboot_helpers, t_ncboot_ncota_against_model,
         t_ncboot_mutations, t_ncdev_helpers, t_ncdev_suite_against_model, t_ncdev_mutations]


def t_wizard_spec_ids(tmp):
    """The Wizard's harness tests and its Playwright specs name each other exactly: every wizard.* test registered in
    suites/s30_wizard.py (but wizard.parser, which runs node tests) has one spec titled with its id in tests/wizard/specs,
    every wizard.* spec title is registered once, and a spec that expects to fail (test.fail: a (should) spec) is
    registered with a title that says '(should)'. run_wizard_test finds its spec by --grep on the id, so a typo on either
    side otherwise shows only on the bench, as 'no Playwright test is titled ...'. No node, no browser: the spec files
    are read as text, the registry is the real suites imported in their own process."""
    specs_dir = os.path.normpath(os.path.join(HERE, "..", "wizard", "specs"))
    titles, fails = [], set()
    for name in sorted(os.listdir(specs_dir)):
        if not name.endswith(".spec.js"):
            continue                                  # the specs/navicore folder holds nctool.* ids, s49's
        src = read(os.path.join(specs_dir, name))
        for chunk in re.split(r"\n(?=test\()", src):
            m = re.match(r"test\(\s*['\"`](wizard\.[a-z0-9_]+)[\s'\"`]", chunk)
            if m:
                titles.append(m.group(1))
                if re.search(r"^\s+test\.fail\(\s*true", chunk, re.M):
                    fails.add(m.group(1))
    probe = (
        "import importlib, json, pkgutil, sys\n"
        f"sys.path.insert(0, {HERE!r})\n"
        "from hil import runner\n"
        "import suites\n"
        "for m in pkgutil.iter_modules(suites.__path__):\n"
        "    importlib.import_module('suites.' + m.name)\n"
        "print(json.dumps([[t['id'], t['title']] for t in runner.REGISTRY if t['id'].startswith('wizard.')]))\n")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert out.returncode == 0, out.stderr
    reg = json.loads(out.stdout)
    ids = [tid for tid, _ in reg]
    dup_ids = sorted({t for t in ids if ids.count(t) > 1})
    dup_specs = sorted({t for t in titles if titles.count(t) > 1})
    assert not dup_ids and not dup_specs, f"registered twice: {dup_ids}; titled twice: {dup_specs}"
    no_spec = sorted(set(ids) - set(titles) - {"wizard.parser"})
    no_test = sorted(set(titles) - set(ids))
    assert not no_spec, f"s30 registers wizard tests no spec is titled with: {no_spec}"
    assert not no_test, f"specs titled with ids no suite registers (the harness never runs them): {no_test}"
    unmarked = sorted(t for t, title in reg if t in fails and not title.startswith("(should)"))
    assert not unmarked, f"test.fail specs registered without '(should)' in their title: {unmarked}"
    assert len(titles) >= 60, f"only {len(titles)} wizard spec titles read: the title pattern no longer matches the specs"


TESTS.append(t_wizard_spec_ids)
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
