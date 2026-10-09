"""Test registry, the Bench (devices + wires), and the runner — shared by run.py (CLI) and gui.py.

Tests register with @test(id, title, needs=[device names], links=[port keys], opt_in=<key>); a port key is a WCB
port ('W1S3') or one of NaviCore's own pins ('N20S3', 'N20MAE', 'N20SBO': hil/links.py NC_PORTS). A test passes by
returning, fails by raising AssertionError (ExpectTimeout included), and is skipped by raising
Skip — or automatically when a device or wire it needs is not on the bench, when it moves a servo in a no-servos run
(hil/servos.py), or when its opt-in (hil/optin.py) is not in bench.json "opt_in". When `links` is not
given it is read from the test's source (`wire(bench, 1, "S3")` / `link(bench, 2, "S1")`, one
level into module helpers), so the GUI can say which wire unlocks which test without anyone
maintaining a list. Every serial line in and out is written to session.log; NaviCore's and the SBUS controller's with
their credentials hashed (Bench.log, REDACT_KINDS).

Every run has a checkpoint (hil/checkpoint.py) and can be paused between tests and resumed later, in the same folder,
after the bench was unplugged and moved (docs/HIL_TESTING.md §9). start_run() makes a new run; continue_run() drives a
new or resumed one; run() and resume() are the CLI's flows around them.
"""
import fnmatch
import inspect
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime

from .checkpoint import (PAUSE, Checkpoint, CheckpointError, atomic_write_json, find_resumable,
                         fmt_duration, harness_info, redact_text)
from . import nvs as nvsrec
from . import optin, servos
from .config import read_config
from .links import LinkManager
from .probe import PROBE_BAUD, Probe, restart_what
from .serialdev import SerialDevice

REGISTRY = []


class Skip(Exception):
    pass


# Real devices a WCB port can be wired to instead of a probe (bench.json "port_devices"):
# kind -> (label, what the detail field holds).
DEVICE_KINDS = {
    "maestro": ("Pololu Maestro", "device id, e.g. 2"),
    "navicore": ("NaviCore port", "NaviCore port, e.g. S1"),
    "hcr": ("HCR vocalizer", ""),
    "mp3": ("MP3 Trigger", ""),
    "dfp": ("DFPlayer", ""),
    "wled": ("WLED controller", "WLED id"),
    "wcb": ("Another WCB's port", "e.g. W2S3"),
    "kyber": ("Kyber", "Maestro or MarcDuino port"),
    "other": ("Other device", "what it is"),
}


def describe_device(d):
    """'NaviCore port S1' from {"kind": "navicore", "detail": "S1"}."""
    label = DEVICE_KINDS.get(d.get("kind"), (d.get("kind") or "device", ""))[0]
    return f"{label} {d.get('detail', '')}".strip()


HIL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A NOT A RESULT test gets this much AWAKE time for every bench port to come back before the run pauses. 0 makes every
# host outage pause at once.
OUTAGE_GRACE_S = 120
OUTAGE_SETTLE_S = 3          # boards reset by the re-enumeration finish booting before the checks
SNAPSHOT_MIN_TESTS = 5       # a run this short takes no config baseline or firmware record at its start (~5 s)
CONFIG_REF_EVERY_S = 15 * 60  # refresh the saved-config reference after this much active time
# Device kinds whose serial lines Bench.log passes through checkpoint.redact_text, in both directions (docs/hil_plan/
# NAVICORE.md D-NC5): NaviCore's GET_CONFIG carries the mesh and AP passwords, the harness sends them back in every
# SET_CONFIG a restore makes (hil/nc_guard.py), its ?backup prints ?EPASS, and the SBUS controller's getcfg carries its
# WiFi networks. A probe joining the mesh sends it the password as MESH JOIN ... PASS=<password> (D57). A WCB's lines
# are left as they are (its ?backup and ?MGMT,PULL chains keep ?EPASS raw, as they always have).
REDACT_KINDS = ("navicore", "sbus", "probe")


def test(test_id, title, needs=(), links=None, drives=(), opt_in=None, opt_in_why=None):
    """drives: ports the test makes transmit without needing a probe there (e.g. a mesh broadcast of ;S3), on top of
    the literal port references found in its source — see drives_of().

    opt_in: a key of hil/optin.py OPT_INS. The runner skips the test before it starts unless bench.json "opt_in" names
    it, with 'opt-in: add "<key>" to bench.json "opt_in" (<why>)'; opt_in_why replaces the entry's default <why>. An
    unknown key fails here, at import, so a typo cannot quietly leave a test ungated."""
    if opt_in is not None and opt_in not in optin.OPT_INS:
        raise ValueError(f"@test {test_id!r}: unknown opt_in {opt_in!r} - add it to hil/optin.py OPT_INS "
                         f"(known: {', '.join(optin.OPT_INS)})")

    def deco(fn):
        REGISTRY.append({"id": test_id, "title": title, "needs": list(needs), "links": links, "drives": list(drives),
                         "opt_in": opt_in, "opt_in_why": opt_in_why, "fn": fn})
        return fn
    return deco


_LINK_CALL = re.compile(r'\b(?:wire|link)\(\s*bench\s*,\s*(\d+)\s*,\s*"(S[1-5])"')


def _sources(fn):
    """The test's source plus the module helpers it calls (one level)."""
    try:
        src = inspect.getsource(fn)
        module = inspect.getmodule(fn)
    except (OSError, TypeError):
        return []
    out = [src]
    for name, obj in vars(module).items():
        if (inspect.isfunction(obj) and obj is not fn and obj.__module__ == module.__name__
                and re.search(rf"\b{re.escape(name)}\(", src)):
            try:
                out.append(inspect.getsource(obj))
            except OSError:
                pass
    return out


def _infer_links(fn):
    return sorted({f"W{w}{p}" for src in _sources(fn) for w, p in _LINK_CALL.findall(src)})


_DRIVE_REMOTE = re.compile(r"(?<![A-Za-z0-9]);W(\d+),?;S([1-5])")
_DRIVE_LOCAL = re.compile(r"(?<![A-Za-z0-9]);S([1-5])")
_DRIVE_PORT = re.compile(r"\bW(\d+)S([1-5])\b")


def drives_of(bench, t):
    """Ports a test may make transmit without a probe there: its declared drives plus every literal ;S<n> (a port of
    the console WCB), ;W<n>;S<p> and W<n>S<p> in its source. Over-inclusive on purpose: it only matters for a port
    with a real device and no port_stimulus, and there skipping is the safe answer."""
    if "_drives" not in t:
        found = set()
        for src in _sources(t["fn"]):
            found |= {(int(w), p) for w, p in _DRIVE_REMOTE.findall(src)}
            found |= {(None, p) for p in _DRIVE_LOCAL.findall(_DRIVE_REMOTE.sub("", src))}
            found |= {(int(w), p) for w, p in _DRIVE_PORT.findall(src)}
        t["_drives"] = found
    local = bench.usb_wcb_number()
    keys = set(t.get("drives", []))
    keys |= {f"W{local if w is None else w}S{p}" for w, p in t["_drives"] if w is None or 1 <= w <= 20}
    return sorted(k for k in keys if re.match(r"^W\d+S[1-5]$", k))


def list_lines(tests, cfg=None, history=None, no_servos=False):
    """run.py --list: id, wires, expected duration (hil/durations.py: '0:12' from past runs, '~5:00' estimated, '?'),
    a servo tag ('[servo]', or '[servo: skipped]' under --no-servos or bench.json "no_servos"; hil/servos.py), an
    opt-in tag ('[opt-in ota_full]', or '[opt-in ota_full: off]' when bench.json does not name it), and the title."""
    from . import durations
    on = optin.enabled(cfg)
    skip_servos = servos.enabled(cfg, {"no_servos": no_servos})
    out = []
    for t in tests:
        wires = ",".join(links_of(t)) or "-"
        exp = durations.fmt_expected(*durations.expected(t, history, cfg))
        key = t.get("opt_in")
        tag = f"[servo{': skipped' if skip_servos else ''}] " if servos.moves_servo(t) else ""
        tag += f"[opt-in {key}{'' if key in on else ': off'}] " if key else ""
        out.append(f"{t['id']:<26} {wires:<16} {exp:>8}  {tag}{t['title']}")
    return out


def links_of(t):
    """Port keys a test needs; 'W1S3|W1S4' means any one of them. NaviCore's pins ('N20S3') are never inferred from the
    source: a test that needs one declares it in links=[...]."""
    if t["links"] is None:
        t["links"] = _infer_links(t["fn"])
    return t["links"]


def parse_key(key):
    m = re.match(r"^W(\d+)(S[1-5])$", key)
    if not m:
        raise ValueError(f"bad port key {key}")
    return int(m.group(1)), m.group(2)


def area_of(t):
    return t["id"].split(".")[0]


def select(selectors):
    return [t for t in REGISTRY if not selectors or any(fnmatch.fnmatch(t["id"], s) for s in selectors)]


def unmatched(selectors):
    """The selectors no registered test matches. select() drops them in silence, so a typo, or the '\r' a Windows list
    file leaves on every id, runs fewer tests than asked and still reads '0 failed' (2026-10-05: 103 of 104 ids)."""
    return [s for s in selectors or [] if not any(fnmatch.fnmatch(t["id"], s) for t in REGISTRY)]


class Bench:
    BAUD = {"wcb": 115200, "probe": PROBE_BAUD, "navicore": 115200, "sbus": 115200}

    def __init__(self, bench_path, results_root, log_sink=None):
        self.bench_path = bench_path
        self.results_root = results_root
        self.log_sink = log_sink
        self.reload_config()
        self.devs, self.probes, self.cache = {}, {}, {}
        self._parked = {}          # name -> a SerialDevice close_device() closed, reopened by dev() (see there)
        self.out_dir = None
        self.ckpt = None           # the running run's Checkpoint while continue_run() drives it (hil/nc_guard.py)
        self._log = None
        self._log_lock = threading.Lock()
        self._t0 = time.monotonic()
        os.makedirs(results_root, exist_ok=True)
        self.links = LinkManager(self, os.path.join(results_root, "links.json"))
        self.links.load()

    # The bench is driven from more than one computer, and each names its ports its own way (COM6 on Windows,
    # /dev/cu.wchusbserial52D20606051 on a Mac). This computer's live in results/ports.json, untracked, over
    # bench.json's, so Find devices on one never rewrites the other's ports in git.
    @property
    def ports_path(self):
        return os.path.join(self.results_root, "ports.json")

    def _read_json(self, path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def reload_config(self):
        with open(self.bench_path, encoding="utf-8") as f:
            self.cfg = json.load(f)
        for name, port in self._read_json(self.ports_path).items():
            d = self.cfg.get("devices", {}).get(name)
            if isinstance(d, dict) and port:
                d["port"] = port

    def save_config(self):
        """This computer's ports to results/ports.json; everything else to bench.json, where each device keeps the port
        the file already had. A device new to bench.json is written with this computer's port, as a first guess for
        the next one. Atomic: bench.json is tracked in git, and a power-off mid-write would leave it truncated."""
        devices = self.cfg.get("devices", {})
        atomic_write_json(self.ports_path, {n: d["port"] for n, d in devices.items()
                                            if isinstance(d, dict) and d.get("port")})
        tracked = (self._read_json(self.bench_path).get("devices") or {})
        out = json.loads(json.dumps(self.cfg))
        for name, d in out.get("devices", {}).items():
            was = (tracked.get(name) or {}).get("port") if isinstance(tracked.get(name), dict) else None
            if isinstance(d, dict) and was:
                d["port"] = was
        atomic_write_json(self.bench_path, out)

    # ------------------------------------------------------------ logging
    def new_session(self):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = os.path.join(self.results_root, stamp)
        n = 1
        while os.path.exists(out_dir):
            n += 1
            out_dir = os.path.join(self.results_root, f"{stamp}-{n}")
        os.makedirs(out_dir)
        with self._log_lock:
            if self._log:
                self._log.close()
            self._log = open(os.path.join(out_dir, "session.log"), "w", encoding="utf-8")
        self.out_dir = out_dir
        return out_dir

    def open_session(self, out_dir, segment):
        """Continue a paused run's session.log in its own folder (append), after a segment marker. Log times count
        from this process's start, so they restart from 0 in a new process."""
        with self._log_lock:
            if self._log:
                self._log.close()
            self._log = open(os.path.join(out_dir, "session.log"), "a", encoding="utf-8")
        self.out_dir = out_dir
        self.note(f"===== SEGMENT {segment} {datetime.now().astimezone().isoformat(timespec='seconds')} "
                  f"host={socket.gethostname()} (log times count from this process's start)")

    def sync_log(self):
        """fsync session.log, so after a power-off its tail matches the checkpoint."""
        with self._log_lock:
            if self._log:
                try:
                    self._log.flush()
                    os.fsync(self._log.fileno())
                except (OSError, ValueError):
                    pass

    def log(self, name, direction, text):
        """One session.log line (and the GUI's log view). A line of a device whose kind is in REDACT_KINDS has every
        credential it carries replaced by <redacted:sha256[:12]> first, whichever way it went."""
        dev = self.cfg.get("devices", {}).get(name)
        if isinstance(dev, dict) and dev.get("kind") in REDACT_KINDS:
            text = redact_text(text)
        line = f"{time.monotonic() - self._t0:9.3f} {name:>9} {direction} {text}"
        with self._log_lock:
            if self._log:
                self._log.write(line + "\n")
                self._log.flush()
        if self.log_sink:
            self.log_sink(line)

    def note(self, text):
        self.log("runner", "#", text)

    def log_time(self, t):
        """A monotonic time on the session log's scale (the t= column)."""
        return t - self._t0

    # ------------------------------------------------------------ devices
    def has(self, name):
        return name in self.cfg["devices"]

    def probe_names(self):
        return [n for n, d in self.cfg["devices"].items() if d["kind"] == "probe"]

    def usb_wcb_number(self):
        """The primary console: the WCB named wcb1, which sends every mesh command."""
        return self.cfg["devices"]["wcb1"]["wcb"]

    def usb_wcbs(self):
        """{board number: device name} for every WCB with its own USB connection."""
        return {d["wcb"]: n for n, d in self.cfg["devices"].items() if d["kind"] == "wcb" and "wcb" in d}

    def wcb_numbers(self):
        usb = self.usb_wcbs()
        primary = self.usb_wcb_number()
        others = sorted(w for w in usb if w != primary)
        mesh = [w for w in self.cfg.get("mesh_only_wcbs", []) if w not in usb]
        return [primary] + others + mesh

    def dev(self, name):
        """The open SerialDevice for `name`. One closed by close_device() - a Wizard test handing its port to Chrome,
        a pause - comes back as the SAME object, reopened, so a wrapper a test took before the hand-off
        (WCB(bench.dev("wcb1")), usb_wcb) still works after it, and marks taken on it stay valid: its log carries on.
        A fresh object left the old one closed, and a test's finally raised 'COM6 is gone' through it, hiding the
        result and skipping the ?RTERM,STOP after it (wizard.mapping_bidir_relay, run 20260929-101257). A device
        whose port or baud changed meanwhile is opened fresh."""
        if name not in self.devs:
            d = self.cfg["devices"][name]
            baud = self.BAUD[d["kind"]]
            parked = self._parked.pop(name, None)
            if parked is not None and parked.port == d["port"] and parked.baud == baud:
                try:
                    self.devs[name] = parked.open()
                except Exception:
                    self._parked[name] = parked        # still held (Chrome exiting): the caller retries
                    raise
            else:
                self.devs[name] = SerialDevice(name, d["port"], baud, log=self.log).open()
                # NaviCore's native-USB port answers every command and an open never resets it, so one gone silent
                # is reopened (hil/serialdev.py); the Kyber resets on every open, and the rest has never needed it.
                self.devs[name].revive_silent = d["kind"] == "navicore"
            time.sleep(0.2)
        return self.devs[name]

    def probe(self, name):
        if name not in self.probes:
            p = Probe(self.dev(name))
            p.reset()   # the probe keeps bindings across host runs; start clean
            self.links.forget_probe(name)
            self.probes[name] = p
        return self.probes[name]

    def close_device(self, name, release=True):
        """release=False skips unbinding the probe's channels: on a dead port each unbind waits SEND_WAIT_S (5 s) per
        wire, and a pause or an outage resets the probes on the way back anyway."""
        if name in self.probes:
            if release:
                try:
                    for link in self.links.all() + self.links.nc_all():
                        if link.probe_name == name:
                            self.links.release(link)
                except Exception:
                    pass
            self.links.forget_probe(name)
            self.probes.pop(name)
        dev = self.devs.pop(name, None)
        if dev:
            dev.close()
            self._parked[name] = dev

    def close_ports(self, release=True):
        """Close every device, and no reader thread is left reopening a COM name that may belong to another board
        after a move."""
        for name in list(self.devs):
            self.close_device(name, release=release)

    def close(self):
        self.close_ports()
        self.sync_log()
        with self._log_lock:
            if self._log:
                self._log.close()
                self._log = None

    # ------------------------------------------------------------ WCB config cache
    def config_tokens(self, wcb, refresh=False):
        if refresh or wcb not in self.cache:
            self.cache[wcb] = read_config(self, wcb)
        return self.cache[wcb]

    def invalidate(self, wcb=None):
        if wcb is None:
            self.cache.clear()
        else:
            self.cache.pop(wcb, None)

    def port_devices(self):
        """{"W1S5": {"kind": "navicore", "detail": "S1"}, ...}: real devices wired to WCB ports instead of a probe."""
        return self.cfg.get("port_devices", {})

    def port_baud(self, wcb, port):
        for t in self.config_tokens(wcb):
            if t.upper().startswith(f"?BAUD,{port},"):
                return int(t.split(",")[2])
        return 9600


def missing(bench, t):
    miss = [f"device {n}" for n in t["needs"] if not bench.has(n)]
    devices = bench.port_devices()
    for key in links_of(t):
        alts = key.split("|")
        if not any(bench.links.get_key(a) for a in alts):   # get() hides a device port with no port_stimulus
            held = [f"{a} has {describe_device(devices[a])} on it" +
                    ("" if bench.links.device_only(*parse_key(a)) else
                     ", and its wire only hears what the device sends in" if bench.links.device_sends(*parse_key(a)) else
                     ", so only a listen-only tap can go there")
                    for a in alts if a in devices]
            nav = [a for a in alts if a.startswith("N")]
            if nav and not held:
                held = ["NaviCore's own pin: probe 3, wired as docs/hil_plan/NAVICORE.md D-NC37 has it, then --discover"]
            miss.append(f"a probe wire on {key}" + (f" ({'; '.join(held)})" if held else ""))
    for key in drives_of(bench, t):
        if bench.links.device_only(*parse_key(key)):
            miss.append(f"{key} kept free of test traffic (it has {describe_device(devices[key])} on it)")
    return miss


_CAFFEINATE = None   # macOS: the `caffeinate -i` process holding idle sleep off while tests run


def _keep_awake(on):
    """Stop Windows' idle sleep while tests run: on 2026-09-22 the host slept 44 min into a full run and every USB serial
    port vanished at once, which failed 12 tests. SetThreadExecutionState is per thread, so this is called on the thread
    that runs the tests (the GUI's worker, or the CLI's main thread). It cannot veto a lid close, the power button or a
    critical-battery hibernate - _awake_s() and host_usb_loss() catch those after the fact. On macOS a `caffeinate -i`
    does the same; `-w` ends it with this process, so a killed run never leaves the Mac unable to sleep."""
    global _CAFFEINATE
    if sys.platform == "darwin":
        try:
            if on and (_CAFFEINATE is None or _CAFFEINATE.poll() is not None):
                _CAFFEINATE = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())],
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif not on and _CAFFEINATE is not None:
                _CAFFEINATE.terminate()
                _CAFFEINATE = None
        except OSError:
            _CAFFEINATE = None
        return
    if os.name != "nt":
        return
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
    except Exception:  # noqa: BLE001 - best effort; a run without it is no worse than before
        pass


def _awake_s():
    """Seconds of time the host was AWAKE (Windows QueryUnbiasedInterruptTime excludes sleep and hibernate), or None
    off Windows. time.monotonic() counts a hibernate (QueryPerformanceCounter on this Python), so the difference across
    a test is how long the host slept inside it. host_usb_loss() alone missed the 2026-09-22 19:58 critical-battery
    hibernate: after resume only W1's CH9102 port errored, and the rest survived. macOS: CLOCK_UPTIME_RAW, which stops
    in sleep, against _host_s()'s CLOCK_MONOTONIC, which does not."""
    if sys.platform == "darwin":
        return _mac_clock(8)    # CLOCK_UPTIME_RAW
    if os.name != "nt":
        return None
    try:
        import ctypes
        t = ctypes.c_ulonglong()
        ctypes.windll.kernel32.QueryUnbiasedInterruptTime(ctypes.byref(t))
        return t.value / 1e7
    except Exception:  # noqa: BLE001 - no detector is no worse than before
        return None


def _mac_clock(clock_id):
    """Seconds on a macOS clock_gettime clock, or None. Through libSystem: the python.org 3.9 build has no
    time.clock_gettime."""
    try:
        import ctypes
        f = ctypes.CDLL("/usr/lib/libSystem.B.dylib").clock_gettime_nsec_np
        f.restype, f.argtypes = ctypes.c_uint64, [ctypes.c_int]
        return f(clock_id) / 1e9
    except Exception:  # noqa: BLE001 - no detector is no worse than before
        return None


def _host_s():
    """Seconds on a clock that keeps counting while the host sleeps, the other half of _awake_s(): time.monotonic() on
    Windows (QueryPerformanceCounter). On macOS time.monotonic() is mach_absolute_time, which stops in sleep, so the
    two would always agree: CLOCK_MONOTONIC instead."""
    if sys.platform == "darwin":
        t = _mac_clock(6)       # CLOCK_MONOTONIC
        if t is not None:
            return t
    return time.monotonic()


def _on_battery():
    """True when the host runs on battery: Windows GetSystemPowerStatus ACLineStatus == 0, macOS `pmset -g batt`."""
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
            return "'Battery Power'" in out
        except (OSError, subprocess.SubprocessError):
            return False
    if os.name != "nt":
        return False
    try:
        import ctypes

        class _SPS(ctypes.Structure):
            _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                        ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                        ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
        s = _SPS()
        return bool(ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s))) and s.ACLineStatus == 0
    except Exception:  # noqa: BLE001
        return False


def host_usb_loss(bench, since):
    """The monotonic time every active bench port failed at once after `since`, or None. All of them within 2 s of each
    other is the HOST losing USB (sleep, a hub reset, a cable pulled) - no firmware can take out seven boards' ports in
    the same moment (2026-09-22: seven ports within 12 ms, the instant Windows logged 'The system is entering sleep')."""
    devs = [d for d in bench.devs.values() if d.active]
    lost = [d.last_error_at for d in devs if d.last_error_at is not None and d.last_error_at >= since]
    if len(devs) >= 2 and len(lost) == len(devs) and max(lost) - min(lost) < 2.0:
        return min(lost)
    return None


def hung_ports(bench):
    """Names of the bench devices with a port open that never returned (serialdev PortHung): a USB-serial chip or driver
    that only a replug brings back. Parked ports count too: the open that hangs is usually the one that takes a port
    back after a Wizard test (WCB1's CH343, 2026-10-08)."""
    devs = list(bench.devs.values()) + list(getattr(bench, "_parked", {}).values())
    return sorted({getattr(d, "name", "?") for d in devs if callable(getattr(d, "hung", None)) and d.hung()})


def _hung_text(hung):
    return (f"{', '.join(hung)}: its port's open never returned - the USB-serial chip or its driver is hung; unplug and "
            f"replug it, then resume")


def _probe_marks(bench):
    """{probe name: (Probe object, log mark)} for every probe open as a test starts."""
    return {name: (p, p.dev.mark()) for name, p in list(bench.probes.items())}


def _probe_incidents(bench, marks):
    """One line per probe that restarted on its own during the test (a panic, or a boot nothing planned), or "".

    Planned restarts are left out (Probe.unplanned_reboots): MESH LEAVE, and anything before a Probe object's first
    RESET - the first use, and the resume/outage path, which reopens a probe as a fresh object. A port that dropped and
    reopened counts as a restart (probe.py REOPEN_MARKER). Why: wcb_probe 6 panic-looped when a wired WCB rebooted, the
    harness kept reading the channels it had lost, and two tests blamed the firmware for bytes the probe never captured
    (run 20260923-154611, tracker #78).

    Each probe that did restart gets a RESET and is then forgotten wholesale, so probe and host agree that nothing is
    bound and the next test binds afresh. Not the selective forget_rebooted_probe after a RESET: RESET releases the
    wires bound after the restart too, and the host would go on reading them as bound. The selective forget is only the
    fallback when the RESET fails (a probe still panic-looping) - it keeps the post-restart bindings the probe may still
    hold, so the next bind cannot pick a letter whose header is taken ('ERR pin in use')."""
    out = []
    for name, p in list(bench.probes.items()):
        prior = marks.get(name)
        since = prior[1] if prior is not None and prior[0] is p else 0   # a probe reopened mid-test: its new log
        try:
            hits = p.unplanned_reboots(since)
        except Exception as e:  # noqa: BLE001 - a scan never breaks the run
            bench.note(f"reboot scan of {name} failed: {e}")
            continue
        if not hits:
            continue
        panics = [h for h in hits if "Guru Meditation" in h[2]]
        first = (panics or hits)[0]
        what = restart_what(first[2])
        count = f", {len(panics)} panics in all" if len(panics) > 1 else ""
        out.append(f"{name} {what} at t={bench.log_time(first[1]):.3f} with no reboot planned "
                   f"({first[2].strip(chr(0)).strip()[:90]}{count}) - it lost every channel binding, so what this "
                   f"test read through it is not a result")
        try:
            p.reset()
            bench.links.forget_probe(name)
        except Exception as e:  # noqa: BLE001 - still restarting or panic-looping: keep what the probe may still hold
            bench.note(f"RESET of {name} after its restart failed ({e}) - forgetting only the channels it lost")
            bench.links.forget_rebooted_probe(name)
    return "; ".join(out)


MESH_STALL = "[MESH] ESP-NOW transmit stalled"     # WCB_EspNow.cpp watchStall, every 30 s while a stall lasts


def _wcb_marks(bench):
    """{device name: (SerialDevice, log mark)} for every WCB console open as a test starts; one that cannot be marked
    is left out, as a scan never breaks the run."""
    kinds = {n: (d or {}).get("kind") for n, d in (bench.cfg.get("devices") or {}).items()}
    out = {}
    for name, dev in list(getattr(bench, "devs", {}).items()):
        if kinds.get(name) != "wcb":
            continue
        try:
            out[name] = (dev, dev.mark())
        except Exception:  # noqa: BLE001
            pass
    return out


def _wcb_stalls(bench, marks):
    """One line per WCB that reported its ESP-NOW transmit stalled during the test (MESH_STALL: every frame refused
    for 5 s or more, none completing), or "". Such a board sends nothing to the mesh, heartbeats included, so what the
    test concluded about its mesh traffic is not a result: full run 20261006-122850 had one board stalled for 2 min,
    seen only because its peers called it offline. The line carries the board's heap figures, quoted here."""
    out = []
    for name, (dev, since) in marks.items():
        try:
            hits = [x.strip() for x in dev.since(since) if x.startswith(MESH_STALL)]
        except Exception as e:  # noqa: BLE001 - a scan never breaks the run
            bench.note(f"stall scan of {name} failed: {e}")
            continue
        if hits:
            out.append(f"{name} reported its ESP-NOW transmit stalled ({len(hits)} report(s), the last {hits[-1][:200]!r})")
    return "; ".join(out)


BOARD_PANIC = ("Guru Meditation Error", "abort() was called")    # an ESP32 panic on a board's console


def _console_marks(bench):
    """{device name: (SerialDevice, log mark)} for every WCB and NaviCore console open as a test starts; one that
    cannot be marked is left out, as a scan never breaks the run."""
    kinds = {n: (d or {}).get("kind") for n, d in (bench.cfg.get("devices") or {}).items()}
    out = {}
    for name, dev in list(getattr(bench, "devs", {}).items()):
        if kinds.get(name) not in ("wcb", "navicore"):
            continue
        try:
            out[name] = (dev, dev.mark())
        except Exception:  # noqa: BLE001
            pass
    return out


def _console_panics(bench, marks):
    """One line per WCB or NaviCore that panicked during the test, or "". No test expects one, and a board that
    panicked restarted, so whatever the test concluded across that moment is not a result. NaviCore panicked on 8 of
    45 boots in full runs 20261006-122850 and -235930 - in its IPC task while installing the GPIO ISR service - and
    every test that saw it passed, because each looked only at its own lines; only ncboot.reboot_resets_ram_state,
    which checks the reset reason, caught it."""
    out = []
    for name, (dev, since) in marks.items():
        try:
            hits = [x.strip() for x in dev.since(since) if any(p in x for p in BOARD_PANIC)]
        except Exception as e:  # noqa: BLE001 - a scan never breaks the run
            bench.note(f"panic scan of {name} failed: {e}")
            continue
        if hits:
            more = f", {len(hits)} panic lines in all" if len(hits) > 1 else ""
            out.append(f"{name} panicked during the test ({hits[0][:160]!r}{more})")
    return "; ".join(out)


def _ports_back(bench, awake_s=120, abort=None):
    """Wait for every active port to reopen. Counted in loop turns, not by the clock: if the host sleeps again during
    the wait, the clock jump must not use up the budget before USB has even re-enumerated. abort() (Pause or Stop
    pressed) ends the wait early."""
    for _ in range(int(awake_s / 0.25)):
        if all(d.connected for d in bench.devs.values() if d.active):
            return True
        if abort and abort():
            return False
        time.sleep(0.25)
    return False


def _boundary():
    return _host_s(), _awake_s()


def _bench_unhealthy(bench, boundary):
    """Why the next test must not start, or None. Catches what the in-test classification cannot: the ports died, or
    the host slept, AFTER one test ended and before the next began - otherwise the next test (and every one after it)
    fails on dead ports and is recorded as a genuine FAIL."""
    dead = [d.name for d in bench.devs.values() if d.active and not d.connected]
    if dead:
        return f"{', '.join(dead)} lost its USB port (the reader is waiting to reopen it)"
    if boundary:
        mono0, awake0 = boundary
        awake1 = _awake_s()
        if awake0 is not None and awake1 is not None:
            slept = (_host_s() - mono0) - (awake1 - awake0)
            if slept > 5:
                return f"the host was asleep for {slept:.0f}s since the last test ended"
    return None


class RunControl:
    """One pending request - None, 'pause' or 'stop' - under a lock, so the last press wins atomically. Shared by the
    CLI (Ctrl+C) and the GUI (Pause / Stop / closing)."""

    def __init__(self):
        # An RLock: run.py's SIGINT handler runs on the main thread and can fire while that thread holds this lock
        # inside should_pause()/should_stop() - CPython checks for pending signals right after __enter__ returns. The
        # handler's nested pausing/request_pause then re-enter harmlessly instead of deadlocking the run, and a
        # second press's KeyboardInterrupt landing there cannot leave a plain Lock held for good.
        self._lock = threading.RLock()
        self._request = None
        self._reason = None

    def request_pause(self, reason="user"):
        with self._lock:
            self._request, self._reason = "pause", reason

    def request_stop(self):
        with self._lock:
            self._request, self._reason = "stop", None

    def cancel_pause(self):
        with self._lock:
            if self._request == "pause":
                self._request, self._reason = None, None

    def clear(self):
        with self._lock:
            self._request, self._reason = None, None

    def should_pause(self):
        """The pause reason, or None."""
        with self._lock:
            return self._reason if self._request == "pause" else None

    def should_stop(self):
        with self._lock:
            return self._request == "stop"

    @property
    def pausing(self):
        return self.should_pause() is not None


def run_tests(bench, tests, on_start=None, on_result=None, should_stop=None, *, ckpt, should_pause=None,
              on_requeue=None):
    """Run `tests` in order under `ckpt`. Returns this segment's results; the run's state is ckpt.state."""
    if _on_battery():
        # 2026-09-22: the host ran on battery for 4 h, slept once and later hibernated at critical battery mid-run.
        bench.note("WARNING: the host is running on BATTERY - a long run can end in a critical-battery hibernate. "
                   "Plug it in.")
    # Read every test's source NOW, while the files match the loaded code. links_of and drives_of read it with
    # inspect.getsource, which re-reads the FILE: a suite edited mid-run hands back shifted lines, and run
    # 20260927-174702 paused at 448/499 on a TokenError from reading the middle of a string.
    for t in tests:
        links_of(t)
        drives_of(bench, t)
    _keep_awake(True)
    try:
        return _run_tests(bench, tests, on_start, on_result, should_stop, ckpt, should_pause, on_requeue)
    finally:
        _keep_awake(False)


def _run_tests(bench, tests, on_start, on_result, should_stop, ckpt, should_pause=None, on_requeue=None):
    results = []
    retried = set()        # one automatic retry per test after a host outage; a second one pauses the run
    i = 0
    ckpt.last_boundary = _boundary()
    while i < len(tests):
        t = tests[i]
        if should_stop and should_stop():
            ckpt.finish("stopped")
            break
        why = (should_pause() if should_pause else None) or ckpt.pause_file_present()
        if why:
            _pause_here(bench, ckpt, why)
            break
        hung = hung_ports(bench)
        if hung:      # no test can use the board, and nothing but a replug brings it back: pause, not a run of ERRORs
            bench.note(f"===== {t['id']} NOT STARTED - {_hung_text(hung)}")
            ckpt.record_outage(t, "port_hung", f"not started - {_hung_text(hung)}")
            _pause_outage(bench, ckpt, _hung_text(hung))
            break
        bad = _bench_unhealthy(bench, ckpt.last_boundary)
        if bad:
            bench.note(f"===== {t['id']} NOT STARTED - {bad}")
            ckpt.record_outage(t, "between_tests", f"not started - {bad}")
            key = ("gate", t["id"])
            if key in retried:
                _pause_outage(bench, ckpt, f"the bench was lost twice before {t['id']} could start")
                break
            if not _recover(bench, ckpt, should_stop, should_pause, cut_off=False):
                break
            retried.add(key)
            ckpt.last_boundary = _boundary()
            continue
        miss = missing(bench, t)
        # After the missing check, so a gated test that also lacks a wire keeps the "needs ..." skip it always had. The
        # servo gate goes before the opt-in one: a no-servos run's report has to say which tests it kept still, and
        # its flag lives in the checkpoint as well as bench.json, so a --resume keeps it (hil/servos.py).
        off = None if miss else (servos.gate(bench.cfg, ckpt.data, t) or optin.gate(bench.cfg, t))
        if miss:
            r = (t, "SKIP", "needs " + ", ".join(miss), 0.0)
            bench.note(f"===== {t['id']} SKIP (0.0s) needs {', '.join(miss)}")
        elif off:
            r = (t, "SKIP", off, 0.0)
            bench.note(f"===== {t['id']} SKIP (0.0s) {off}")
        else:
            ckpt.record_start(t)
            if on_start:
                on_start(t)
            bench.note(f"===== {t['id']} {t['title']}")
            probe_marks = _probe_marks(bench)
            wcb_marks = _wcb_marks(bench)
            console_marks = _console_marks(bench)
            start = time.monotonic()
            awake0 = _awake_s()
            try:
                t["fn"](bench)
                status, detail = "PASS", ""
            except Skip as e:
                status, detail = "SKIP", str(e)
            except AssertionError as e:
                status, detail = "FAIL", str(e)
            except Exception:
                status, detail = "ERROR", traceback.format_exc()
            dur = time.monotonic() - start
            awake1 = _awake_s()
            slept = dur - (awake1 - awake0) if awake0 is not None and awake1 is not None else 0.0
            usb_lost, hung = None, []
            if status in ("FAIL", "ERROR"):
                time.sleep(0.5)       # a failed write can return a few ms before the reader threads log their errors
                usb_lost = host_usb_loss(bench, start)
                hung = hung_ports(bench)
            if status in ("FAIL", "ERROR") and slept > 5:
                status = "ERROR"
                detail = (f"NOT A RESULT - the host was asleep for {slept:.0f}s during this test (sleep or hibernate, "
                          f"e.g. on critical battery); rerun it. The test said: "
                          f"{detail.splitlines()[0] if detail else '-'}")
            elif usb_lost is not None:
                status = "ERROR"
                detail = (f"NOT A RESULT - the host lost every USB serial port at once {usb_lost - start:.1f}s into "
                          f"this test (the PC slept, or a hub reset / cable pull); rerun it. The test said: "
                          f"{detail.splitlines()[0] if detail else '-'}")
            elif hung:
                status = "ERROR"
                detail = (f"NOT A RESULT - {_hung_text(hung)}. The test said: "
                          f"{redact_text(detail.strip().splitlines()[-1]) if detail.strip() else '-'}")
            else:
                # A probe that restarted on its own during the test lost every channel binding; whatever the test
                # concluded from those channels is not a result about the WCB. Fail THIS test with the reason, and
                # make the next bind start clean. Runs before the pinned-wire release below, which would otherwise
                # try to unbind channels the probe no longer has.
                incident = _probe_incidents(bench, probe_marks)
                if incident:
                    own = {"PASS": "The test itself passed, but read through that probe."}.get(
                        status, f"The test itself said ({status}): {detail}")
                    status, detail = "FAIL", f"{incident}\n{own}"
                panic = _console_panics(bench, console_marks)
                if panic:
                    own = {"PASS": "The test itself passed, but that board restarted meanwhile."}.get(
                        status, f"The test itself said ({status}): {detail}")
                    status, detail = "FAIL", f"{panic}\n{own}"
                stall = _wcb_stalls(bench, wcb_marks)
                if stall:
                    own = {"PASS": "The test itself passed, but that board sent nothing to the mesh meanwhile."}.get(
                        status, f"The test itself said ({status}): {detail}")
                    status, detail = "FAIL", f"{stall}\n{own}"
            # A baud a test pinned with listen(<baud>) must not outlive it: config_guard's resync only re-binds
            # auto-baud wires, so one pinned W1S2 at 115200 once garbled every later injection into that 9600 port.
            # A wire on NaviCore's own pins is released whatever its baud: nothing re-binds it between tests, and a
            # channel left on SBUS OUT would log ~111 probe lines a second for the rest of the run.
            for link in bench.links.all() + bench.links.nc_all():
                if link.channel is not None and (not link.auto_baud or link.navicore):
                    try:
                        bench.links.release(link)
                    except Exception as e:  # noqa: BLE001 — a probe that went away; the next bind reports it
                        bench.note(f"release of {link.key} after {t['id']} failed: {e}")
            bench.note(f"===== {t['id']} {status} ({dur:.1f}s) {detail.splitlines()[0] if detail else ''}")
            r = (t, status, detail, dur)
        if r[1] == "ERROR" and r[2].startswith("NOT A RESULT"):
            # Not a result: checkpointed as an outage, and the test runs again (it has no result, so it is also first
            # on a resume). It stays in_flight: its own cleanup ran against dead ports.
            kind = "slept" if "was asleep" in r[2] else "port_hung" if "USB-serial chip" in r[2] else "usb_loss"
            ckpt.record_outage(t, kind, r[2])
            if on_requeue:
                on_requeue(t, r[2])
            if kind == "port_hung":     # no automatic retry: the chip stays hung until someone replugs it
                _pause_outage(bench, ckpt, _hung_text(hung_ports(bench)) if hung_ports(bench) else r[2])
                break
            if t["id"] in retried:
                _pause_outage(bench, ckpt, f"{t['id']} lost the host a second time")
                break
            if not _recover(bench, ckpt, should_stop, should_pause, cut_off=True):
                break
            retried.add(t["id"])
            ckpt.last_boundary = _boundary()
            continue
        ckpt.record_result(*r)
        results.append(r)
        if on_result:
            on_result(*r)
        if "CONFIG NOT RESTORED" in r[2]:
            _refresh_config_ref(bench, ckpt, "after_leak", t["id"])
        elif (len(ckpt.data["tests"]) >= SNAPSHOT_MIN_TESTS and ckpt.data.get("config_ref")
              and ckpt.active_s_now() - (ckpt.data["config_ref"].get("active_s") or 0) >= CONFIG_REF_EVERY_S):
            _refresh_config_ref(bench, ckpt, "periodic", t["id"])
        i += 1
        ckpt.last_boundary = _boundary()
    else:
        if ckpt.data.get("nvs"):
            ckpt.data["nvs"]["end"] = nvsrec.record(bench, "end", ckpt.name)
        ckpt.finish("done")
    return results


def _refresh_config_ref(bench, ckpt, taken, after):
    from . import resume
    try:
        ckpt.set_config_ref(resume.snapshot_configs(bench), taken, after=after)
    except Exception as e:  # noqa: BLE001 - a snapshot never fails the run
        bench.note(f"saved-config reference not refreshed ({redact_text(str(e))})")


def _pause_here(bench, ckpt, why):
    """A pause at a test boundary, in the order that makes 'safe to disconnect' true when it is shown: the finished
    test's pinned wires are already released; take the saved-config reference; checkpoint and report; log; close every
    port and the log. Only then does the caller tell the user."""
    from . import resume
    last = ckpt.data["results"][-1]["id"] if ckpt.data["results"] else None
    try:
        ckpt.set_config_ref(resume.snapshot_configs(bench), "pause", after=last)
    except Exception as e:  # noqa: BLE001
        bench.note(f"config snapshot at the pause failed ({redact_text(str(e))}) - the resume compares against the "
                   f"last reference")
    ckpt.pause(why)
    bench.note(f"===== run PAUSED ({ckpt.data['reason_text']}) - {ckpt.done_count} of {ckpt.total} done; resume: GUI "
               f"Resume / python tests/hil/run.py --resume {ckpt.name}")
    bench.close_ports(release=False)
    bench.close()


def _pause_outage(bench, ckpt, detail):
    from .checkpoint import REASONS
    ckpt.set_outage_then("paused")
    ckpt.pause("host_outage", text=f"{REASONS['host_outage']}: {detail}")
    bench.note(f"===== run PAUSED ({redact_text(detail)}) - {ckpt.done_count} of {ckpt.total} done; resume: "
               f"GUI Resume / "
               f"python tests/hil/run.py --resume {ckpt.name}")
    bench.close_ports(release=False)
    bench.close()


def _recover(bench, ckpt, should_stop, should_pause, cut_off):
    """After a host outage: wait up to OUTAGE_GRACE_S of awake time for the ports, then re-check the bench with no
    questions asked (ask=None: any difference pauses instead). True = carry on; False = the run is now paused or
    stopped, with its ports closed."""
    from . import resume

    def asked():
        return bool((should_stop and should_stop()) or (should_pause and should_pause()))

    def stop_or_pause(detail):
        if should_stop and should_stop():
            bench.close_ports(release=False)
            ckpt.finish("stopped")
        else:
            _pause_outage(bench, ckpt, detail)
        return False

    if asked() or OUTAGE_GRACE_S <= 0:
        return stop_or_pause("a pause was requested" if OUTAGE_GRACE_S > 0 else "OUTAGE_GRACE_S is 0")
    bench.note("waiting for every bench port to come back after the host lost USB")
    if not _ports_back(bench, OUTAGE_GRACE_S, asked):
        return stop_or_pause("a pause was requested" if asked() else
                             f"the bench ports did not come back within {OUTAGE_GRACE_S} s of awake time")
    bench.note("every bench port is back - re-checking the bench before continuing")
    time.sleep(OUTAGE_SETTLE_S)
    bench.close_ports(release=False)
    try:
        resume.check_bench(bench, ckpt, ask=None, log=bench.note, should_abort=asked, cut_off=cut_off)
    except resume.ResumeAborted:
        return stop_or_pause("a pause was requested during the checks")
    except resume.ResumeBlocked as e:
        bench.note("the automatic resume was blocked:\n" + redact_text(str(e)))
        return stop_or_pause(str(e).splitlines()[0])
    ckpt.set_outage_then("auto-resumed")
    bench.note("the bench checked out - re-running the test that was cut off" if cut_off
               else "the bench checked out - continuing")
    return True


def _discover(bench, log):
    log("Discovering wires...")
    for link in bench.links.discover(log=log):
        log(f"  {link}{'' if link.verified else '   <-- did not verify'}")


def start_run(bench, tests, label, selectors=None, discover=False, no_servos=False):
    """A new run: its folder and session.log, a checkpoint holding the lock, and - for a run of SNAPSHOT_MIN_TESTS or
    more - the firmware versions and the saved-config baseline (about 5 s). A shorter run skips both, so a
    double-clicked single test does not pay for them. Nothing here ever blocks the run. no_servos (run.py --no-servos)
    is recorded in the checkpoint, not bench.json: the run's own flag, kept by a resume in any process."""
    from . import resume
    out_dir = bench.new_session()
    stale = os.path.join(bench.results_root, PAUSE)
    if os.path.exists(stale):
        try:
            os.remove(stale)
            bench.note(f"deleted a stale {stale} left from an earlier run")
        except OSError:
            pass
    ckpt = Checkpoint.new(out_dir, tests, label, selectors, bench, discover=discover)
    if no_servos:
        ckpt.data["no_servos"] = True
    ckpt.log, ckpt.sync_log = bench.note, bench.sync_log
    ckpt.acquire()        # before the first save, so a scan never sees this run as interrupted
    try:
        ckpt.data["harness"] = harness_info(HIL_ROOT)
        ckpt.data["devices"] = {n: {k: u.get(k) for k in ("serial", "vid", "pid", "location")}
                                for n, u in resume.usb_map(bench).items()}
        ckpt.save()
        if len(tests) >= SNAPSHOT_MIN_TESTS:
            pre = set(bench.devs)
            try:
                ckpt.start_firmware = resume.record_firmware(bench)
                ckpt.set_config_ref(resume.snapshot_configs(bench), "start")
                # Settings-storage use per board (hil/nvs.py; HIL_TEST_AUDIT.md F10): a full NVS fails tests that
                # have nothing wrong with them, and only a reading at both ends of every run shows it creeping.
                ckpt.data["nvs"] = {"start": nvsrec.record(bench, "start", ckpt.name)}
            finally:
                # record_firmware opens NaviCore and every probe. A run whose tests never use them must not hold their
                # ports all run (an IDE upload or a NaviCore tab gets "port busy"), and the pre-test gate must not
                # pause it over a device it never needed. Tests reopen what they use, lazily, as before.
                for n in set(bench.devs) - pre:
                    bench.close_device(n, release=False)
        else:
            bench.note(f"no config baseline or firmware record at the start: {len(tests)} test(s), under "
                       f"{SNAPSHOT_MIN_TESTS}")
        ckpt.save()
    except BaseException as e:
        kb = isinstance(e, KeyboardInterrupt)
        ckpt.pause("aborted" if kb else "harness_error", text=None if kb else f"a harness error at the start: {e}")
        ckpt.release()
        raise
    return ckpt


def continue_run(bench, ckpt, *, resuming, ask=None, log=None, on_start=None, on_result=None, on_requeue=None,
                 should_stop=None, should_pause=None, on_checks_done=None, on_bench_changed=None, discover_log=None):
    """Drive a new run (from start_run) or resume a paused / interrupted / stopped one, in its own folder.

    Resuming: session.log is appended to after a segment marker, and resume.check_bench() re-establishes the bench -
    ResumeBlocked or ResumeAborted leaves the run paused, with every port closed, and is re-raised. Then pending wire
    discovery, a new segment, and the remaining tests. Anything else escaping (a second Ctrl+C, a harness bug) leaves
    a still-running run paused - 'aborted' or 'harness_error' - and is re-raised. The lock is always released."""
    from . import resume as rs
    log = log or bench.note
    ckpt.acquire()                       # RunBusy when another process has it; a no-op after start_run
    ckpt.log, ckpt.sync_log = bench.note, bench.sync_log
    ckpt.registry_ids = [t["id"] for t in REGISTRY]
    bench.ckpt = ckpt                    # nc_guard records NaviCore's snapshot state in it (checkpoint 'navicore')

    def aborting():
        return bool((should_stop and should_stop()) or (should_pause and should_pause()))

    try:
        if resuming:
            bench.open_session(ckpt.out_dir, len(ckpt.data["segments"]) + 1)
            ckpt.close_dangling()
            # Not ckpt.remaining(): it records dropped ids, and step 1 must still see new ones as a difference.
            done, known = {r["id"] for r in ckpt.data["results"]}, {t["id"] for t in REGISTRY}
            left = [tid for tid in ckpt.data["tests"] if tid not in done and tid in known]
            if not left and not ckpt.in_flight:
                # Nothing that still exists is left to run: the process died after the last result and before
                # finish('done') (e.g. in the config refresh after the last test), or every unfinished test was renamed
                # or removed. The bench checks would only cost minutes, or block (the bench already moved) a run that
                # is complete. Step 1 never runs on this path, so the gone ids are recorded here, or they would vanish
                # from the report instead of being listed under "Not run".
                _, gone = ckpt.remaining(REGISTRY)
                log(f"every test in {ckpt.name} that still exists already has a result - marking it done without the "
                    f"bench checks" + (f"; no longer in the suite, so not run: {', '.join(gone)}" if gone else ""))
                ckpt.finish("done")
                return []
        ckpt.clear_stale_pause_files(log)
        _keep_awake(True)
        changes, firmware = [], dict(ckpt.start_firmware or {})
        if resuming:
            try:
                changes, firmware = rs.check_bench(bench, ckpt, ask, log, aborting, cut_off=False,
                                                   on_bench_changed=on_bench_changed)
            except (rs.ResumeAborted, KeyboardInterrupt) as e:
                # No pause handler during the checks (M12): a Ctrl+C outside a question arrives as the default
                # KeyboardInterrupt - a cancel, like Stop/Pause, not the mid-test abort. An interrupted run keeps
                # reason 'interrupted' (block), and run.py exits 3 with "Resume not done".
                err = e if isinstance(e, rs.ResumeAborted) else rs.ResumeAborted("Ctrl+C")
                ckpt.block(err, aborted=True)
                log(f"resume cancelled ({err}) - the run is still paused")
                bench.close_ports(release=False)
                bench.close()
                if err is e:
                    raise
                raise err from None
            except rs.ResumeBlocked as e:
                ckpt.block(e)
                log("RESUME BLOCKED - the run is still paused:\n" + redact_text(str(e)))
                bench.close_ports(release=False)
                bench.close()
                raise
            ckpt.data["harness"] = harness_info(HIL_ROOT)
        if ckpt.data.get("discover_pending"):
            if resuming:
                log("wire discovery was asked for and never finished - running it now")
            _discover(bench, discover_log or log)
            ckpt.data["discover_pending"] = False
        ckpt.begin_segment(usb=rs.usb_map(bench), firmware=firmware, changes=changes, host=socket.gethostname(),
                           bench=bench)
        if on_checks_done:
            on_checks_done()
        tests, _ = ckpt.remaining(REGISTRY)
        return run_tests(bench, tests, on_start, on_result, should_stop, ckpt=ckpt, should_pause=should_pause,
                         on_requeue=on_requeue)
    except (rs.ResumeAborted, rs.ResumeBlocked):
        raise
    except BaseException as e:
        if ckpt.state == "running" and not ckpt.frozen:
            kb = isinstance(e, KeyboardInterrupt)
            text = None if kb else f"a harness error: {type(e).__name__}: {(str(e).splitlines() or [''])[0]}"
            try:
                ckpt.pause("aborted" if kb else "harness_error", text=text)
                bench.note(f"===== run PAUSED ({ckpt.data['reason_text']}) - {ckpt.done_count} of {ckpt.total} done"
                           + (f", {ckpt.in_flight} cut off" if ckpt.in_flight else "")
                           + f"; resume: GUI Resume / python tests/hil/run.py --resume {ckpt.name}")
                bench.sync_log()
            except Exception:  # noqa: BLE001 - never mask the original
                pass
        raise
    finally:
        _keep_awake(False)
        ckpt.release()
        bench.ckpt = None


def _cli_printer(t, status, detail, dur):
    print(f"{status:<5} {t['id']:<24} {t['title']}  ({dur:.1f}s)")
    if detail and status != "PASS":
        print("      " + detail.replace("\n", "\n      "))


def _cli_requeue(t, detail):
    print(f"RETRY {t['id']:<24} {detail.splitlines()[0]} - it will run again")


def run(bench_path, selectors, results_root, discover=False, control=None, on_checks_done=None, no_servos=False):
    """The CLI flow: open the bench, discover wires if asked (or never done), run, report. -> (out_dir, ckpt)."""
    control = control or RunControl()
    bench = Bench(bench_path, results_root)
    try:
        first_time = not os.path.exists(bench.links.path)
        disc = bool(discover or (first_time and bench.probe_names() and bench.has("wcb1")))
        tests = select(selectors)
        for s in unmatched(selectors):
            print(f"WARNING: {s!r} matches no test")
        ckpt = start_run(bench, tests, " ".join(selectors) or "everything", selectors=list(selectors or []),
                         discover=disc, no_servos=no_servos)
        print(f"Run {ckpt.name} · {len(tests)} tests · pause: Ctrl+C, or create results/{ckpt.name}/{PAUSE}")
        _print_no_servos(bench, ckpt, tests)
        continue_run(bench, ckpt, resuming=False, on_result=_cli_printer, on_requeue=_cli_requeue,
                     should_stop=control.should_stop, should_pause=control.should_pause,
                     on_checks_done=on_checks_done, discover_log=print)
        print(f"Took {fmt_duration(ckpt.active_s)}")
    finally:
        bench.close()
    return ckpt.out_dir, ckpt


def resolve_run(results_root, which="latest"):
    """A run folder from 'latest' (the newest paused or interrupted run), a run name, or a path."""
    if which in (None, "latest"):
        runs = find_resumable(results_root)
        if not runs:
            raise CheckpointError("no paused or interrupted run in tests/hil/results (run.py --paused lists them)")
        return runs[0]["path"]
    path = which if os.path.isdir(which) else os.path.join(results_root, which)
    if not (os.path.exists(os.path.join(path, "checkpoint.json"))
            or os.path.exists(os.path.join(path, "checkpoint.json.tmp"))):
        raise CheckpointError(f"no checkpoint in results/{os.path.basename(os.path.normpath(path))}")
    return path


def _print_no_servos(bench, ckpt, tests):
    if servos.enabled(bench.cfg, ckpt.data):
        n = sum(1 for t in tests if servos.moves_servo(t))
        print(f"No moving servos: {n} test{'' if n == 1 else 's'} that move a servo will SKIP (hil/servos.py)")


def resume(bench_path, results_root, which="latest", control=None, ask=None, on_checks_done=None, no_servos=False):
    """The CLI's --resume: continue a paused, interrupted or (named) stopped run. -> (out_dir, ckpt). no_servos turns
    the run's no-servos flag on for the rest of it (and it stays on); it never turns off a flag the run started with."""
    control = control or RunControl()
    path = resolve_run(results_root, which)
    ckpt = Checkpoint.load(path)
    if ckpt.state in ("done", "abandoned"):
        raise CheckpointError(f"{ckpt.name} is {ckpt.state} - there is nothing to resume")
    bench = Bench(bench_path, results_root)
    try:
        print(f"Resuming {ckpt.name} ({ckpt.data.get('label')}): {ckpt.done_count} of {ckpt.total} done")
        if no_servos:
            ckpt.data["no_servos"] = True    # saved with the checkpoint's next write, before any test runs
        done = {r["id"] for r in ckpt.data["results"]}
        _print_no_servos(bench, ckpt, [t for t in REGISTRY if t["id"] in ckpt.data["tests"] and t["id"] not in done])

        def log(s):
            bench.note(s)
            print(s)
        continue_run(bench, ckpt, resuming=True, ask=ask, log=log, on_result=_cli_printer,
                     on_requeue=_cli_requeue, should_stop=control.should_stop, should_pause=control.should_pause,
                     on_checks_done=on_checks_done, discover_log=print)
        print(f"Took {fmt_duration(ckpt.active_s)} (active, every segment)")
    finally:
        bench.close()
    return path, ckpt
