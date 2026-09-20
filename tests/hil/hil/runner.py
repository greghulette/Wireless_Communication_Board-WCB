"""Test registry, the Bench (devices + wires), and the runner — shared by run.py (CLI) and gui.py.

Tests register with @test(id, title, needs=[device names], links=[port keys]). A test passes by
returning, fails by raising AssertionError (ExpectTimeout included), and is skipped by raising
Skip — or automatically when a device or wire it needs is not on the bench. When `links` is not
given it is read from the test's source (`wire(bench, 1, "S3")` / `link(bench, 2, "S1")`, one
level into module helpers), so the GUI can say which wire unlocks which test without anyone
maintaining a list. Every serial line in and out is written to session.log.
"""
import fnmatch
import inspect
import json
import os
import re
import threading
import time
import traceback
from datetime import datetime

from .config import read_config
from .links import LinkManager
from .probe import PROBE_BAUD, Probe
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
    "other": ("Other device", "what it is"),
}


def describe_device(d):
    """'NaviCore port S1' from {"kind": "navicore", "detail": "S1"}."""
    label = DEVICE_KINDS.get(d.get("kind"), (d.get("kind") or "device", ""))[0]
    return f"{label} {d.get('detail', '')}".strip()


def fmt_duration(sec):
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def test(test_id, title, needs=(), links=None, drives=()):
    """drives: ports the test makes transmit without needing a probe there (e.g. a mesh broadcast of ;S3), on top of
    the literal port references found in its source — see drives_of()."""
    def deco(fn):
        REGISTRY.append({"id": test_id, "title": title, "needs": list(needs), "links": links, "drives": list(drives),
                         "fn": fn})
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


def links_of(t):
    """Port keys a test needs; 'W1S3|W1S4' means any one of them."""
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


class Bench:
    BAUD = {"wcb": 115200, "probe": PROBE_BAUD, "navicore": 115200, "sbus": 115200}

    def __init__(self, bench_path, results_root, log_sink=None):
        self.bench_path = bench_path
        self.results_root = results_root
        self.log_sink = log_sink
        self.reload_config()
        self.devs, self.probes, self.cache = {}, {}, {}
        self.out_dir = None
        self._log = None
        self._log_lock = threading.Lock()
        self._t0 = time.monotonic()
        os.makedirs(results_root, exist_ok=True)
        self.links = LinkManager(self, os.path.join(results_root, "links.json"))
        self.links.load()

    def reload_config(self):
        with open(self.bench_path, encoding="utf-8") as f:
            self.cfg = json.load(f)

    def save_config(self):
        with open(self.bench_path, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")

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

    def log(self, name, direction, text):
        line = f"{time.monotonic() - self._t0:9.3f} {name:>9} {direction} {text}"
        with self._log_lock:
            if self._log:
                self._log.write(line + "\n")
                self._log.flush()
        if self.log_sink:
            self.log_sink(line)

    def note(self, text):
        self.log("runner", "#", text)

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
        if name not in self.devs:
            d = self.cfg["devices"][name]
            self.devs[name] = SerialDevice(name, d["port"], self.BAUD[d["kind"]], log=self.log).open()
            time.sleep(0.2)
        return self.devs[name]

    def probe(self, name):
        if name not in self.probes:
            p = Probe(self.dev(name))
            p.reset()   # the probe keeps bindings across host runs; start clean
            self.links.forget_probe(name)
            self.probes[name] = p
        return self.probes[name]

    def close_device(self, name):
        if name in self.probes:
            try:
                for link in self.links.all():
                    if link.probe_name == name:
                        self.links.release(link)
            except Exception:
                pass
            self.links.forget_probe(name)
            self.probes.pop(name)
        dev = self.devs.pop(name, None)
        if dev:
            dev.close()

    def close(self):
        for name in list(self.devs):
            self.close_device(name)
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
        if not any(bench.links.get(*parse_key(a)) for a in alts):   # get() hides a device port with no port_stimulus
            held = [f"{a} has {describe_device(devices[a])} on it" +
                    ("" if bench.links.device_only(*parse_key(a)) else ", so only a listen-only tap can go there")
                    for a in alts if a in devices]
            miss.append(f"a probe wire on {key}" + (f" ({'; '.join(held)})" if held else ""))
    for key in drives_of(bench, t):
        if bench.links.device_only(*parse_key(key)):
            miss.append(f"{key} kept free of test traffic (it has {describe_device(devices[key])} on it)")
    return miss


def run_tests(bench, tests, on_start=None, on_result=None, should_stop=None):
    results = []
    for t in tests:
        if should_stop and should_stop():
            break
        miss = missing(bench, t)
        if miss:
            r = (t, "SKIP", "needs " + ", ".join(miss), 0.0)
            bench.note(f"===== {t['id']} SKIP (0.0s) needs {', '.join(miss)}")
        else:
            if on_start:
                on_start(t)
            bench.note(f"===== {t['id']} {t['title']}")
            start = time.monotonic()
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
            # A baud a test pinned with listen(<baud>) must not outlive it: config_guard's resync only re-binds
            # auto-baud wires, so one pinned W1S2 at 115200 once garbled every later injection into that 9600 port.
            for link in bench.links.all():
                if link.channel is not None and not link.auto_baud:
                    try:
                        bench.links.release(link)
                    except Exception as e:  # noqa: BLE001 — a probe that went away; the next bind reports it
                        bench.note(f"release of {link.key} after {t['id']} failed: {e}")
            bench.note(f"===== {t['id']} {status} ({dur:.1f}s) {detail.splitlines()[0] if detail else ''}")
            r = (t, status, detail, dur)
        results.append(r)
        if on_result:
            on_result(*r)
    return results


def run(bench_path, selectors, results_root, discover=False):
    """The CLI flow: open the bench, discover wires if asked (or never done), run, report."""
    bench = Bench(bench_path, results_root)
    out_dir = bench.new_session()
    try:
        first_time = not os.path.exists(bench.links.path)
        if discover or (first_time and bench.probe_names() and bench.has("wcb1")):
            print("Discovering wires...")
            for link in bench.links.discover():
                print(f"  {link}{'' if link.verified else '   <-- did not verify'}")

        def printer(t, status, detail, dur):
            print(f"{status:<5} {t['id']:<24} {t['title']}  ({dur:.1f}s)")
            if detail and status != "PASS":
                print("      " + detail.replace("\n", "\n      "))

        start = time.monotonic()
        results = run_tests(bench, select(selectors), on_result=printer)
        elapsed = time.monotonic() - start
        print(f"Took {fmt_duration(elapsed)}")
    finally:
        bench.close()
    write_report(out_dir, results, elapsed)
    return out_dir, results


def write_report(out_dir, results, elapsed=None):
    counts = {}
    for _, status, _, _ in results:
        counts[status] = counts.get(status, 0) + 1
    with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as f:
        f.write(f"# HIL run {os.path.basename(out_dir)}\n\n")
        took = f" · took {fmt_duration(elapsed)}" if elapsed is not None else ""
        f.write(" · ".join(f"{k} {v}" for k, v in sorted(counts.items())) + took + "\n\n")
        f.write("| Result | Test | Title | Time | Detail |\n|---|---|---|---|---|\n")
        for t, status, detail, dur in results:
            first = detail.splitlines()[0].replace("|", "\\|") if detail else ""
            f.write(f"| {status} | {t['id']} | {t['title']} | {dur:.1f}s | {first} |\n")
        fails = [(t, d) for t, s, d, _ in results if s in ("FAIL", "ERROR")]
        if fails:
            f.write("\n## Failure detail\n")
            for t, d in fails:
                f.write(f"\n### {t['id']} — {t['title']}\n\n```\n{d}\n```\n")
