"""The run checkpoint: the one record of a run, so a run can be paused and resumed (docs/HIL_TESTING.md §9).

Every run folder holds checkpoint.json, written atomically when each test starts and ends and at every state change.
report.md is rebuilt from it after every result and swapped in whole - never appended to, never merged by hand. A
plain single-segment run that finished renders exactly the report layout the harness always had; the status line,
Segments, Re-run and Not-run sections appear only when they apply.

run.lock marks a run as live in some process. It is an OS byte lock, not data: the OS drops it when the process dies,
so a checkpoint still saying "running" with its lock free is a run that was interrupted (power-off, crash, window
closed) and can be resumed. Pure data - nothing here opens a serial port.

The saved-config reference (config_ref) never holds the mesh password or WiFi credentials: those tokens are stored as
'<prefix><redacted:sha256[:12]>' (redact_token), and every comparison and diff uses that form.
"""
import copy
import glob
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
from datetime import datetime

FORMAT = 1
CHECKPOINT = "checkpoint.json"
REPORT = "report.md"
LOCK = "run.lock"
PAUSE = "PAUSE"

# reason code -> the text the banner, the status line and the report show
REASONS = {
    "user": "Pause pressed",
    "ctrl_c": "Ctrl+C pressed",
    "pause_file": "a PAUSE file asked for it",
    "host_outage": "the host lost the bench (sleep or USB) and it did not come back cleanly",
    "aborted": "aborted mid-test (a second Ctrl+C)",
    "harness_error": "a harness error",
    "closing": "paused to close the window",
    "interrupted": "stopped unexpectedly (power-off, crash or closed window)",
    "stopped": "Stop pressed",
}


class CheckpointError(Exception):
    """No readable checkpoint where one was expected."""


class RunBusy(Exception):
    """The run's lock is held by a live process."""


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def fmt_duration(sec):
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def active_clock():
    """Seconds the host was AWAKE (QueryUnbiasedInterruptTime excludes sleep and hibernate), or time.monotonic() where
    that is not available. A segment's active time is the difference of two readings, so paused time, time between
    processes and time asleep never count."""
    if os.name == "nt":
        try:
            import ctypes
            t = ctypes.c_ulonglong()
            ctypes.windll.kernel32.QueryUnbiasedInterruptTime(ctypes.byref(t))
            return t.value / 1e7
        except Exception:  # noqa: BLE001 - fall back to the monotonic clock
            pass
    return time.monotonic()


# ---------------------------------------------------------------------------- atomic files
_warned = set()


def _stderr(text):
    try:
        sys.stderr.write(text + "\n")
    except Exception:  # noqa: BLE001
        pass


def atomic_write_text(path, text, retries=10, log=None):
    """Write `path` so a power-off leaves either the old file or the new one, never half of each: write path.tmp,
    fsync, then os.replace. On Windows a reader holding the file without FILE_SHARE_DELETE (Python's own open(), an
    editor, OneDrive, antivirus) makes the replace fail with PermissionError, so it is retried every 50 ms. If it still
    fails the update is skipped - logged once per file - and False returned: a failed write never fails a test.
    Text mode, so line endings are the platform's, exactly as the harness's reports always were."""
    tmp = path + ".tmp"
    last = None
    for _ in range(retries + 1):
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            _warned.discard(path)
            return True
        except PermissionError as e:
            last = e
            time.sleep(0.05)
        except OSError as e:          # disk full, folder gone: retrying will not help
            last = e
            break
    if path not in _warned:
        _warned.add(path)
        (log or _stderr)(f"could not write {path}: {last} - this update was skipped; {os.path.basename(tmp)} keeps "
                         f"it, and the next write replaces both")
    return False


def atomic_write_json(path, obj, log=None):
    return atomic_write_text(path, json.dumps(obj, indent=2, ensure_ascii=False) + "\n", log=log)


# ---------------------------------------------------------------------------- the run lock
class RunLock:
    """An OS byte lock on <run>/run.lock. The byte sits far past any content, so the file stays readable. Windows byte
    locks are per handle, and flock() is per open file description, so held() sees a lock taken by another handle in
    the same process too. The OS drops the lock when the process dies - that is how an interrupted run is recognised."""
    OFFSET = 1 << 30

    def __init__(self, out_dir):
        self.path = os.path.join(out_dir, LOCK)
        self.fd = None

    @classmethod
    def _lock(cls, fd):
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, cls.OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    @classmethod
    def _unlock(cls, fd):
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, cls.OFFSET, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass

    @property
    def locked(self):
        return self.fd is not None

    def acquire(self, wait_s=1.0):
        """Take the lock, retrying for `wait_s`; RunBusy if another handle keeps it."""
        if self.fd is not None:
            return
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        deadline = time.monotonic() + wait_s
        while True:
            try:
                self._lock(fd)
                self.fd = fd
                return
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise RunBusy(f"{os.path.basename(os.path.dirname(self.path))} is being run or resumed by "
                                  f"another process")
                time.sleep(0.1)

    def release(self):
        if self.fd is None:
            return
        fd, self.fd = self.fd, None
        self._unlock(fd)
        try:
            os.close(fd)
        except OSError:
            pass

    @classmethod
    def held(cls, out_dir):
        """Is the run live in some process? Tries the lock and lets it go at once."""
        path = os.path.join(out_dir, LOCK)
        if not os.path.exists(path):
            return False
        try:
            fd = os.open(path, os.O_RDWR)
        except OSError:
            return False
        try:
            cls._lock(fd)
        except OSError:
            os.close(fd)
            return True
        cls._unlock(fd)
        os.close(fd)
        return False


# ---------------------------------------------------------------------------- fingerprints
def sha(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


def bench_canon(cfg):
    """bench.json minus what is expected to change between sessions: COM ports, notes and top-level _comment keys."""
    c = copy.deepcopy(cfg)
    for k in [k for k in c if k.startswith("_")]:
        del c[k]
    for d in c.get("devices", {}).values():
        if isinstance(d, dict):
            d.pop("port", None)
            d.pop("note", None)
    return c


def links_canon(mgr):
    """(canon, verified keys) of a LinkManager. `verified` is kept out of the canon, because verify() rewrites it."""
    rows = []
    for link in mgr.all():
        j = link.to_json()
        rows.append({k: j[k] for k in ("wcb", "port", "probe", "header", "swap", "tap")})
    rows.sort(key=lambda r: (r["wcb"], r["port"]))
    return rows, sorted(l.key for l in mgr.all() if l.verified)


def _fmt_val(v):
    return json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v


def diff_canon(a, b):
    """Key-path differences between two bench canons, one level into devices and dicts: 'devices.probe3 added',
    'opt_in +seq_wipe', 'taps changed'."""
    out = []
    a, b = a or {}, b or {}
    for k in sorted(set(a) | set(b)):
        if k not in a:
            out.append(f"{k} added")
        elif k not in b:
            out.append(f"{k} removed")
        elif a[k] != b[k]:
            x, y = a[k], b[k]
            if isinstance(x, dict) and isinstance(y, dict):
                for sub in sorted(set(x) | set(y)):
                    if sub not in x:
                        out.append(f"{k}.{sub} added")
                    elif sub not in y:
                        out.append(f"{k}.{sub} removed")
                    elif x[sub] != y[sub]:
                        if isinstance(x[sub], dict) and isinstance(y[sub], dict):
                            fields = [f"{f} {_fmt_val(x[sub].get(f))} -> {_fmt_val(y[sub].get(f))}"
                                      for f in sorted(set(x[sub]) | set(y[sub])) if x[sub].get(f) != y[sub].get(f)]
                            out.append(f"{k}.{sub}: " + ", ".join(fields))
                        else:
                            out.append(f"{k}.{sub} {_fmt_val(x[sub])} -> {_fmt_val(y[sub])}")
            elif (isinstance(x, list) and isinstance(y, list)
                  and all(not isinstance(v, (dict, list)) for v in x + y)):
                added = [v for v in y if v not in x]
                removed = [v for v in x if v not in y]
                out.append(f"{k} " + " ".join([f"+{v}" for v in added] + [f"-{v}" for v in removed]))
            else:
                out.append(f"{k} changed")
    return out


def diff_links(a, b):
    """'W1S3 was probe1 S2, now probe1 S4' lines between two links canons."""
    def keyed(rows):
        return {f"W{r['wcb']}{r['port']}": r for r in rows or []}

    def desc(r):
        return f"{r['probe']} {r['header']}" + (" SWAP" if r.get("swap") else "") + (" (tap)" if r.get("tap") else "")
    ka, kb = keyed(a), keyed(b)
    out = []
    for key in sorted(set(ka) | set(kb)):
        if key not in ka:
            out.append(f"{key} added: {desc(kb[key])}")
        elif key not in kb:
            out.append(f"{key} removed (was {desc(ka[key])})")
        elif ka[key] != kb[key]:
            out.append(f"{key} was {desc(ka[key])}, now {desc(kb[key])}")
    return out


# ---------------------------------------------------------------------------- the harness itself
_FROZEN = None     # (root, files) captured right after the suites were imported
_INFO = None


def _harness_files(root):
    """sha1[:12] of every harness source: hil/*.py, suites/*.py, run.py, gui.py and the Wizard specs."""
    out = {}
    pats = [("hil", "*.py"), ("suites", "*.py"), ("", "run.py"), ("", "gui.py")]
    for sub, pat in pats:
        for p in sorted(glob.glob(os.path.join(root, sub, pat))):
            rel = os.path.relpath(p, root).replace("\\", "/")
            out[rel] = _file_sha(p)
    specs = os.path.normpath(os.path.join(root, "..", "wizard", "specs"))
    for p in sorted(glob.glob(os.path.join(specs, "*"))):
        if os.path.isfile(p):
            out["tests/wizard/specs/" + os.path.basename(p)] = _file_sha(p)
    return out


def _file_sha(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()[:12]
    except OSError:
        return None


def freeze_harness(root):
    """Record the harness sources as this process imported them. gui.py and run.py call it right after importing the
    suites, so a GUI that stays open and resumes is shown as running the same code - which it is."""
    global _FROZEN
    _FROZEN = (_norm(root), _harness_files(root))


def _norm(path):
    return os.path.normcase(os.path.abspath(path))


def harness_files(root):
    if _FROZEN and _FROZEN[0] == _norm(root):
        return dict(_FROZEN[1])
    return _harness_files(root)


def _git(root, *args):
    try:
        p = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=5)
        return p.stdout if p.returncode == 0 else None
    except Exception:  # noqa: BLE001 - no git, or it hung
        return None


def harness_info(root):
    """git HEAD and dirty count, Python and pyserial versions, and the source hashes. Cached per process."""
    global _INFO
    if _INFO is None or _INFO[0] != root:
        head = _git(root, "rev-parse", "--short", "HEAD")
        status = _git(root, "status", "--porcelain", "--", ":/tests/hil", ":/tests/wizard")
        try:
            import serial
            pyserial = getattr(serial, "__version__", None)
        except ImportError:
            pyserial = None
        _INFO = (root, {"git": head.strip() if head else None,
                        "dirty": len([l for l in status.splitlines() if l.strip()]) if status is not None else None,
                        "python": platform.python_version(), "pyserial": pyserial})
    return dict(_INFO[1], files=harness_files(root))


# ---------------------------------------------------------------------------- secrets
# The backup chain carries the ESP-NOW password (EPASS,<pass>, WCB.ino collectConfigCommands) and the WiFi network and
# passphrase (WIFI,AP,<ssid>,<pass> / WIFI,JOIN,<ssid>,<pass>). The first character is the function identifier, which
# printBackupConfig can flip mid-chain, so it is matched as any character.
_SECRET = re.compile(r"^(.EPASS,?|.WIFI,(?:AP|JOIN),)(.*)$", re.I | re.S)


def redact_token(t):
    m = _SECRET.match(t)
    if not m or m.group(2).startswith("<redacted:"):
        return t
    return m.group(1) + "<redacted:" + hashlib.sha256(m.group(2).encode("utf-8")).hexdigest()[:12] + ">"


def redact_tokens(tokens):
    return [redact_token(t) for t in tokens]


def _hash_repl(prefix_group, value_group):
    def repl(m):
        return m.group(prefix_group) + "<redacted:" + hashlib.sha256(
            m.group(value_group).encode("utf-8")).hexdigest()[:12] + ">"
    return repl


# The same secrets inside free text: a test's failure detail (an ExpectTimeout's last-lines tail around ?backup,
# ?config or a boot banner), a config diff, a resume block. The token payload runs to '^', the end of the line, or a
# quote closing a Python list repr. ?config and the boot banner print "Password: <pw>" / "ESP-NOW Password: <pw>"
# (WCB.ino printConfigInfo, setup); the SBUS controller's cfg JSON carries its WiFi networks in "wifiNets".
_SECRET_TEXT = (
    (re.compile(r"((?<![A-Za-z0-9])(?:EPASS,|EPASS(?!,)|WIFI,(?:AP|JOIN),))"
                r"((?!<redacted:)(?:(?!['\"](?:[,\])}]|[\r\n]|$))[^\^\r\n])+)", re.I), _hash_repl(1, 2)),
    (re.compile(r"((?<![A-Za-z])(?:ESP-NOW )?Password:[ \t]*)((?!<redacted:)\S+)", re.I), _hash_repl(1, 2)),
    (re.compile(r"(\"wifiNets\"\s*:\s*)(\[[^\]]*\])"), _hash_repl(1, 2)),
    # The probe's MESH JOIN quotes it as PASS=<password> (Probe._cmd's error names the whole command). The value ends at
    # whitespace, or at a quote followed by whitespace or the end - the close of "'<cmd>' -> ERR ...". The usage text
    # PASS=<pw> is left alone.
    (re.compile(r"((?<![A-Za-z0-9])PASS=)((?!<redacted:|<pw>)(?:(?!['\"](?:\s|$))\S)+)"), _hash_repl(1, 2)),
)


def redact_text(s):
    """`s` with every mesh password and WiFi credential it quotes replaced by <redacted:sha256[:12]>. Applied to
    every free-text field the checkpoint stores (result and outage detail, the pause reason, the last resume error)
    and to the log lines the pause/resume code adds; session.log's own serial traffic is left as it always was."""
    if not s:
        return s
    for rx, repl in _SECRET_TEXT:
        s = rx.sub(repl, s)
    return s


# ---------------------------------------------------------------------------- the checkpoint
def iso_ts(iso):
    """Epoch seconds of an ISO timestamp, 0 when it does not parse."""
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return 0.0


class Checkpoint:
    def __init__(self, out_dir, data):
        self.out_dir = out_dir
        self.data = data
        self._mu = threading.RLock()
        self._lock = RunLock(out_dir)
        self._frozen = False
        self._seg_clock0 = None    # active_clock() when this process began the current segment
        self._pause_stale = {}     # PAUSE path -> (mtime_ns, inode) of a stale file the start-of-segment sweep
        #                            could not delete
        self.last_boundary = None  # (monotonic, awake) at the last test boundary - memory only
        self.sync_log = None       # callable: fsync session.log (set by the runner)
        self.log = None            # callable(text): a note in session.log
        self.registry_ids = None   # every test id this process knows, for the report's Not-run section
        self.start_firmware = {}

    # ------------------------------------------------------------ create / load
    @classmethod
    def new(cls, out_dir, tests, label, selectors, bench, discover=False):
        now = now_iso()
        canon = bench_canon(bench.cfg)
        lcanon, verified = links_canon(bench.links)
        data = {
            "format": FORMAT, "run": os.path.basename(out_dir), "label": label,
            "selectors": list(selectors) if selectors is not None else None,
            "state": "running", "reason": None, "reason_text": None, "in_flight": None,
            "last_resume_error": None, "discover_pending": bool(discover),
            "created": now, "updated": now,
            "tests": [t["id"] for t in tests],
            "results": [], "outages": [], "dropped": [], "segments": [], "devices": {},
            "bench": {"sha": sha(canon), "canon": canon},
            "links": {"sha": sha(lcanon), "canon": lcanon, "verified": verified},
            "harness": {}, "config_ref": None,
        }
        return cls(out_dir, data)

    @classmethod
    def load(cls, out_dir):
        """Read <out_dir>/checkpoint.json.tmp when it parses, else checkpoint.json. CheckpointError when neither reads.

        The .tmp first: a successful os.replace consumes it, so one that survives holds the newest save - a power-off
        before the replace, or a replace another handle blocked (atomic_write_text gives up after ~0.5 s). The last
        saves of a run (the final result, pause(), finish()) have no later save to retry them, so reading the main file
        first would bring back a stale 'running' copy and re-run a test that already has a result. A .tmp torn by a
        crash mid-write does not parse, and the main file is used. Never compare 'updated': it has one-second
        resolution, and a tie would pick the stale file."""
        path = os.path.join(out_dir, CHECKPOINT)
        data, err = None, None
        for p in (path + ".tmp", path):
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    break
                data = None
            except (OSError, ValueError) as e:
                err = e
        if data is None:
            raise CheckpointError(f"no readable checkpoint in {out_dir} ({err})")
        for k, v in (("results", []), ("outages", []), ("dropped", []), ("segments", []), ("devices", {}),
                     ("tests", [])):
            data.setdefault(k, v)
        ck = cls(out_dir, data)
        # A second Ctrl+C inside record_result can leave a result AND in_flight for the same id: the test finished.
        fl = data.get("in_flight")
        if fl and fl.get("id") in {r["id"] for r in data["results"]}:
            data["in_flight"] = None
        return ck

    # ------------------------------------------------------------ properties
    @property
    def name(self):
        return os.path.basename(self.out_dir)

    @property
    def state(self):
        return self.data.get("state")

    @property
    def total(self):
        return len(self.data["tests"])

    @property
    def done_count(self):
        return len(self.data["results"])

    @property
    def in_flight(self):
        return (self.data.get("in_flight") or {}).get("id")

    @property
    def active_s(self):
        return sum(s.get("active_s") or 0.0 for s in self.data["segments"])

    @property
    def frozen(self):
        return self._frozen

    @property
    def locked(self):
        return self._lock.locked

    # ------------------------------------------------------------ locking
    def acquire(self):
        self._lock.acquire()

    def release(self):
        self._lock.release()

    def freeze(self):
        """The GUI is closing now: nothing is written from here on, so a test cut off by the exit can never be
        recorded as a result. The worker's saves become no-ops."""
        with self._mu:
            self._frozen = True

    # ------------------------------------------------------------ saving
    def _note(self, text):
        if self.log:
            try:
                self.log(text)
                return
            except Exception:  # noqa: BLE001
                pass
        _stderr(text)

    def save(self, report=True):
        with self._mu:
            if self._frozen:
                return False
            self.data["updated"] = now_iso()
            if self._seg_clock0 is not None and self.data["segments"]:
                self.data["segments"][-1]["active_s"] = round(active_clock() - self._seg_clock0, 1)
            ok = atomic_write_json(os.path.join(self.out_dir, CHECKPOINT), self.data, log=self._note)
            if report:
                atomic_write_text(os.path.join(self.out_dir, REPORT), render_report(self), log=self._note)
            return ok

    def _sync(self):
        if self.sync_log:
            try:
                self.sync_log()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ what is left
    def remaining(self, registry):
        """(tests still to run, in selection order, mapped through the registry; ids that no longer exist). A cut-off
        or NOT A RESULT test has no result and every test before it has one, so it comes first by construction."""
        done = {r["id"] for r in self.data["results"]}
        by_id = {t["id"]: t for t in registry}
        tests, dropped = [], []
        for tid in self.data["tests"]:
            if tid in done:
                continue
            if tid in by_id:
                tests.append(by_id[tid])
            else:
                dropped.append(tid)
        with self._mu:
            for tid in dropped:
                if tid not in self.data["dropped"]:
                    self.data["dropped"].append(tid)
        return tests, dropped

    # ------------------------------------------------------------ recording
    def record_start(self, t):
        with self._mu:
            self.data["in_flight"] = {"id": t["id"], "started": now_iso()}
            self.save(report=False)

    def record_result(self, t, status, detail, dur):
        with self._mu:
            if self._frozen:
                return
            row = {"id": t["id"], "title": t.get("title", ""), "status": status, "detail": redact_text(detail),
                   "dur": dur, "at": now_iso(), "seg": len(self.data["segments"])}
            # one statement: a KeyboardInterrupt cannot land between the two (load() cleans up the rest)
            self.data["results"], self.data["in_flight"] = self.data["results"] + [row], None
            self.save(report=True)
        self._sync()

    def record_outage(self, t, kind, detail):
        """A NOT A RESULT (or a test the pre-test gate kept from starting). Not a result: the test runs again."""
        with self._mu:
            self.data["outages"].append({"id": t["id"], "kind": kind, "detail": redact_text(detail),
                                         "at": now_iso(), "seg": len(self.data["segments"]), "then": None})
            self.save(report=True)
        self._sync()

    def set_outage_then(self, then):
        with self._mu:
            if self.data["outages"] and self.data["outages"][-1].get("then") is None:
                self.data["outages"][-1]["then"] = then
                self.save(report=True)

    # ------------------------------------------------------------ segments and states
    def begin_segment(self, usb, firmware, changes=(), host=None, bench=None):
        with self._mu:
            self.data["segments"].append({
                "n": len(self.data["segments"]) + 1, "start": now_iso(), "end": None, "active_s": 0.0,
                "host": host, "end_reason": None, "usb": usb, "firmware": firmware, "changes": list(changes)})
            self._seg_clock0 = active_clock()
            self.data.update(state="running", reason=None, reason_text=None, last_resume_error=None)
            devices = self.data.setdefault("devices", {})
            for name, u in (usb or {}).items():
                devices[name] = {k: u.get(k) for k in ("serial", "vid", "pid", "location")}
            if bench is not None:   # what was accepted at this resume is what the next one compares against
                canon = bench_canon(bench.cfg)
                lcanon, verified = links_canon(bench.links)
                self.data["bench"] = {"sha": sha(canon), "canon": canon}
                self.data["links"] = {"sha": sha(lcanon), "canon": lcanon, "verified": verified}
            self.save()

    def end_segment(self, reason):
        with self._mu:
            segs = self.data["segments"]
            if segs and segs[-1].get("end") is None:
                segs[-1]["end"] = now_iso()
                segs[-1]["end_reason"] = reason
                if self._seg_clock0 is not None:
                    segs[-1]["active_s"] = round(active_clock() - self._seg_clock0, 1)
            self._seg_clock0 = None

    def close_dangling(self):
        """An interrupted run's last segment never ended: close it at the last moment the checkpoint was written."""
        with self._mu:
            segs = self.data["segments"]
            if segs and segs[-1].get("end") is None:
                segs[-1]["end"] = self.data.get("updated")
                segs[-1]["end_reason"] = "interrupted"

    def pause(self, reason, error=None, text=None):
        with self._mu:
            if self._frozen:
                return
            self.data.update(state="paused", reason=reason,
                             reason_text=redact_text(text) or REASONS.get(reason, reason))
            if error is not None:
                self.data["last_resume_error"] = redact_text(str(error))
            self.end_segment(reason)
            self.save()
        self._sync()

    def finish(self, state):
        """done or stopped."""
        with self._mu:
            if self._frozen:
                return
            self.data["state"] = state
            self.data["reason"] = "stopped" if state == "stopped" else None
            self.data["reason_text"] = REASONS["stopped"] if state == "stopped" else None
            self.end_segment(state)
            self.save()
        self._sync()

    def block(self, error, aborted=False):
        """A resume that did not get past its checks: the run stays paused (an interrupted run becomes paused)."""
        with self._mu:
            if self.data["state"] == "running":
                fl = self.in_flight
                self.data.update(state="paused", reason="interrupted",
                                 reason_text=REASONS["interrupted"] + (f" during {fl}" if fl else ""))
            if not aborted and error is not None:
                self.data["last_resume_error"] = redact_text(str(error))
            self.save()

    def abandon(self):
        if RunLock.held(self.out_dir) and not self.locked:
            raise RunBusy(f"{self.name} is live in another process")
        with self._mu:
            self.data["state"] = "abandoned"
            self.end_segment("abandoned")
            self.save()

    # ------------------------------------------------------------ saved-config reference
    def set_config_ref(self, tokens, taken, after=None):
        """tokens: {wcb: [tokens] or None}. A board that could not be read keeps its previous reference."""
        with self._mu:
            old = (self.data.get("config_ref") or {}).get("tokens") or {}
            new = dict(old)
            for w, toks in (tokens or {}).items():
                if toks is not None:
                    new[str(w)] = redact_tokens(toks)
            self.data["config_ref"] = {"taken": taken, "at": now_iso(), "after_test": after,
                                       "active_s": round(self.active_s_now(), 1), "tokens": new}
            self.save(report=False)

    def active_s_now(self):
        """Active time including the running segment's time since its last save."""
        done = sum(s.get("active_s") or 0.0 for s in self.data["segments"][:-1])
        if self.data["segments"]:
            cur = self.data["segments"][-1]
            done += (active_clock() - self._seg_clock0) if self._seg_clock0 is not None else (cur.get("active_s") or 0)
        return done

    # ------------------------------------------------------------ PAUSE files
    def _pause_paths(self):
        return [os.path.join(self.out_dir, PAUSE), os.path.join(os.path.dirname(self.out_dir), PAUSE)]

    def clear_stale_pause_files(self, log=None):
        """At a segment's start: any PAUSE file already there is stale (it would pause the run on its first test), so
        it is deleted. One that cannot be deleted (read-only, held open) is remembered by (mtime, inode) and ignored
        until it changes - otherwise it would pause every segment at its first boundary."""
        self._pause_stale = {}
        for p in self._pause_paths():
            try:
                st = os.stat(p)
            except OSError:
                continue
            try:
                os.remove(p)
                (log or self._note)(f"deleted a stale PAUSE file {p} (it was there before this segment began)")
            except OSError as e:
                self._pause_stale[p] = (st.st_mtime_ns, st.st_ino)
                (log or self._note)(f"could not delete a stale PAUSE file {p} ({e}) - it is ignored until it changes")

    def pause_file_present(self):
        """'pause_file' when <run>/PAUSE or results/PAUSE asks for a pause; the file is consumed. Any file found after
        the segment-start sweep counts, whatever its mtime: a copied or moved-in file keeps its source's LastWriteTime.
        Only the undeletable stale file the sweep reported, unchanged, is skipped."""
        for p in self._pause_paths():
            try:
                st = os.stat(p)
            except OSError:
                continue
            sig = (st.st_mtime_ns, st.st_ino)
            if self._pause_stale.get(p) == sig:
                continue
            try:
                os.remove(p)
            except OSError:
                self._pause_stale[p] = sig     # honoured once; it pauses again only if it changes again
            return "pause_file"
        return None

    # ------------------------------------------------------------ summaries
    def summary(self):
        held = RunLock.held(self.out_dir)
        d = self.data
        return {"name": self.name, "path": self.out_dir, "label": d.get("label"), "done": self.done_count,
                "total": self.total, "state": d.get("state"), "reason": d.get("reason"),
                "reason_text": d.get("reason_text"), "in_flight": self.in_flight, "updated": d.get("updated"),
                "last_resume_error": d.get("last_resume_error"), "active_s": self.active_s, "live": held,
                "interrupted": d.get("state") == "running" and not held}

    def pause_message(self):
        return (f"Paused - safe to disconnect. {self.done_count} of {self.total} done. Resume with the Resume button "
                f"(or python tests/hil/run.py --resume {self.name})")


def summary_text(s):
    """One line for the GUI banner or run.py --paused."""
    when = (s.get("updated") or "")[11:16]
    head = f"{s['name']} ({s.get('label') or '-'}): {s['done']} of {s['total']} done"
    if s.get("interrupted"):
        during = f"during {s['in_flight']}" if s.get("in_flight") else "between tests"
        text = f"Interrupted run {head} - stopped unexpectedly {during} (power-off, crash or closed window)"
    elif s["state"] == "stopped":
        text = f"Stopped run {head}, stopped {when}"
    else:
        text = f"Paused run {head}, paused {when} - {s.get('reason_text') or s.get('reason') or ''}"
    if s.get("last_resume_error"):
        text += " · last resume blocked: " + s["last_resume_error"].splitlines()[0]
    return text


def find_resumable(results_root, include_stopped=False):
    """Summaries of the runs that can be resumed: paused, interrupted (running with its lock free) and, when asked,
    stopped. Newest first by the last checkpoint write. Unreadable checkpoints and live runs are left out."""
    out = []
    try:
        names = os.listdir(results_root)
    except OSError:
        return out
    for name in names:
        p = os.path.join(results_root, name)
        if not os.path.isdir(p) or not (os.path.exists(os.path.join(p, CHECKPOINT))
                                        or os.path.exists(os.path.join(p, CHECKPOINT + ".tmp"))):
            continue
        try:
            s = Checkpoint.load(p).summary()
        except (CheckpointError, KeyError, TypeError, AttributeError):
            continue
        if s["live"]:
            continue
        if s["state"] == "paused" or s["interrupted"] or (include_stopped and s["state"] == "stopped"):
            out.append(s)
    out.sort(key=lambda s: iso_ts(s.get("updated")), reverse=True)
    return out


# ---------------------------------------------------------------------------- the report
def _cell(text):
    return (text or "").replace("|", "\\|").replace("\n", " ")


def _when(iso):
    return (iso or "")[:19].replace("T", " ")


def _nvs_lines(nvs):
    """One line per board with its settings-storage use at the run's start and end (hil/nvs.py), or nothing."""
    if not nvs:
        return []
    lines = []
    for board in sorted(set(nvs.get("start") or {}) | set(nvs.get("end") or {})):
        parts = []
        for phase in ("start", "end"):
            s = (nvs.get(phase) or {}).get(board)
            if s:
                parts.append(f"{phase} {s['used']}/{s['total']} used ({s['pct']}%), {s['available']} available")
        if parts:
            lines.append(f"NVS {board}: " + " · ".join(parts) + "\n")
    return lines + ["\n"] if lines else []


def render_report(ck):
    """report.md from the checkpoint. A single-segment run that finished (done, or stopped) with no host outage and
    no dropped test renders exactly the layout write_report always produced; everything else adds a status line and
    the sections that apply."""
    d = ck.data
    results = d["results"]
    segs, outages, dropped = d["segments"], d["outages"], d["dropped"]
    state = d.get("state")
    plain = state in ("done", "stopped") and len(segs) <= 1 and not outages and not dropped
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    out = [f"# HIL run {ck.name}\n\n"]
    if not plain and state != "done":
        n, m = ck.done_count, ck.total
        if state == "running":
            line = f"**RUNNING** — {n} of {m} done (as of {(d.get('updated') or '')[11:19]})"
        elif state == "paused":
            line = (f"**PAUSED** after {n} of {m} — {d.get('reason_text') or d.get('reason') or ''}. Resume: GUI Resume, "
                    f"or python tests/hil/run.py --resume {ck.name}")
        else:
            line = f"**{(state or '?').upper()}** after {n} of {m}"
        if d.get("last_resume_error"):
            line += f"\n\nLast resume attempt blocked: {d['last_resume_error'].splitlines()[0]}"
        out.append(line + "\n\n")
    out.append(" · ".join(f"{k} {v}" for k, v in sorted(counts.items())) + f" · took {fmt_duration(ck.active_s)}\n\n")
    out += _nvs_lines(d.get("nvs"))
    out.append("| Result | Test | Title | Time | Detail |\n|---|---|---|---|---|\n")
    for r in results:
        first = r["detail"].splitlines()[0].replace("|", "\\|") if r["detail"] else ""
        out.append(f"| {r['status']} | {r['id']} | {r['title']} | {r['dur']:.1f}s | {first} |\n")
    if not plain:
        if len(segs) > 1:
            out.append("\n## Segments\n\n| # | Started | Ended | Active | Ended because | Changes at resume |\n"
                       "|---|---|---|---|---|---|\n")
            for s in segs:
                why = REASONS.get(s.get("end_reason"), s.get("end_reason") or ("running" if not s.get("end") else ""))
                out.append(f"| {s['n']} | {_when(s.get('start'))} | {_when(s.get('end'))} | "
                           f"{fmt_duration(s.get('active_s') or 0)} | {_cell(why)} | "
                           f"{_cell('; '.join(s.get('changes') or []))} |\n")
        if outages:
            out.append("\n## Re-run after a host outage\n\n| Test | When | What happened | Then |\n|---|---|---|---|\n")
            for o in outages:
                out.append(f"| {o['id']} | {_when(o.get('at'))} | {_cell((o.get('detail') or '').splitlines()[0])} | "
                           f"{o.get('then') or ('pending' if state == 'running' else '-')} |\n")
        notrun = [f"- `{t}` — no longer in the suite (renamed or removed)" for t in dropped]
        if state in ("stopped", "abandoned"):
            done = {r["id"] for r in results}
            notrun += [f"- `{t}` — not run: the run was {state}" for t in d["tests"] if t not in done and t not in dropped]
        sel = d.get("selectors")
        if ck.registry_ids is not None and sel is not None and len(segs) > 1:
            import fnmatch
            added = [t for t in ck.registry_ids if t not in d["tests"]
                     and (not sel or any(fnmatch.fnmatch(t, s) for s in sel))]
            notrun += [f"- `{t}` — added to the suite since this run started; run it separately" for t in added]
        if notrun:
            out.append("\n## Not run\n\n" + "\n".join(notrun) + "\n")
    fails = [r for r in results if r["status"] in ("FAIL", "ERROR")]
    if fails:
        out.append("\n## Failure detail\n")
        for r in fails:
            out.append(f"\n### {r['id']} — {r['title']}\n\n```\n{r['detail']}\n```\n")
    return "".join(out)
