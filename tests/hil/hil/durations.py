"""Expected test durations from past runs: the GUI's Expected column and time-left estimate, and run.py --list.

A test's expected time is the median of its most recent real results - PASS, FAIL or ERROR, never SKIP and never a
NOT A RESULT row - newest run folder first, up to SAMPLES of them. A run folder is read from checkpoint.json (its
results[].dur; .tmp first, as Checkpoint.load does) and, for runs older than the checkpoint, from report.md's table.
A test with no real result falls back to its opt-in's estimate (hil/optin.py), or is unknown.

Parsing ~150 folders takes a few seconds, so each folder's durations are cached in results/durations.json keyed by the
folder name and the size and mtime of the files it was read from; only a new or changed folder is read again. Corrupt,
partial and unreadable files are skipped, never raised. Nothing here touches a serial port. The GUI calls load() on a
background thread; run.py --list reads the cache but never writes it.
"""
import json
import os
import re
import statistics

from . import optin
from .checkpoint import CHECKPOINT, REPORT, Checkpoint, CheckpointError, atomic_write_json, fmt_duration

CACHE = "durations.json"
CACHE_FORMAT = 1
SAMPLES = 5
REAL = ("PASS", "FAIL", "ERROR")

# | PASS | probe.hello | Title (may hold ' | ') | 0.6s | detail, its pipes escaped |
_ROW = re.compile(r"^\| (PASS|FAIL|ERROR|SKIP) \| (\S+) \| .* \| (\d+(?:\.\d+)?)s \| (.*) \|\s*$")


def _real(status, detail):
    return status in REAL and not (detail or "").lstrip().startswith("NOT A RESULT")


def _sig(folder):
    """What the folder's durations were read from: [[file, size, mtime_ns], ...]. [] when it has neither file."""
    out = []
    for name in (CHECKPOINT, CHECKPOINT + ".tmp", REPORT):
        try:
            st = os.stat(os.path.join(folder, name))
        except OSError:
            continue
        out.append([name, st.st_size, st.st_mtime_ns])
    return out


def _from_checkpoint(folder):
    """{id: dur} of the real results in a checkpoint, or None when no checkpoint reads."""
    try:
        results = Checkpoint.load(folder).data.get("results") or []
    except (CheckpointError, AttributeError, KeyError, TypeError, ValueError):   # KeyError: a result row with no id
        return None
    out = {}
    for r in results:
        try:
            if _real(r.get("status"), r.get("detail")):
                out[str(r["id"])] = float(r["dur"])
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    return out


def _from_report(folder):
    """{id: dur} of the real rows of report.md's result table ({} when it does not read)."""
    out = {}
    try:
        with open(os.path.join(folder, REPORT), encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _ROW.match(line)
                if m and _real(m.group(1), m.group(4)):
                    out[m.group(2)] = float(m.group(3))
    except OSError:
        pass
    return out


def scan_run(folder):
    """{id: dur} of one run folder: the checkpoint when one reads, else report.md."""
    got = None
    if os.path.exists(os.path.join(folder, CHECKPOINT)) or os.path.exists(os.path.join(folder, CHECKPOINT + ".tmp")):
        got = _from_checkpoint(folder)
    return got if got is not None else _from_report(folder)


def _read_cache(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("format") == CACHE_FORMAT and isinstance(data.get("runs"), dict):
            return data["runs"]
    except (OSError, ValueError):
        pass
    return {}


def load(results_root, samples=SAMPLES, cache_path=None, write_cache=True):
    """{test id: (median seconds, samples used)} from every run folder under results_root. cache_path defaults to
    results/durations.json; write_cache=False reads it but leaves it alone."""
    cache_path = cache_path or os.path.join(results_root, CACHE)
    cached = _read_cache(cache_path)
    runs, changed = {}, False
    try:
        names = sorted(os.listdir(results_root), reverse=True)     # run folders are timestamps: newest first
    except OSError:
        names = []
    per_run = []
    for name in names:
        folder = os.path.join(results_root, name)
        if not os.path.isdir(folder):
            continue
        sig = _sig(folder)
        if not sig:
            continue
        hit = cached.get(name)
        if isinstance(hit, dict) and hit.get("sig") == sig and isinstance(hit.get("durs"), dict):
            durs = hit["durs"]
        else:
            try:
                durs = scan_run(folder)
            except Exception:  # noqa: BLE001 - one bad folder must never take out every other run's history
                durs = {}
            changed = True
        runs[name] = {"sig": sig, "durs": durs}
        per_run.append(durs)
    if set(cached) - set(runs):
        changed = True                                              # a folder was deleted
    if write_cache and changed:
        atomic_write_json(cache_path, {"format": CACHE_FORMAT, "runs": runs}, log=lambda s: None)
    picked = {}
    for durs in per_run:
        for tid, dur in durs.items():
            got = picked.setdefault(tid, [])
            if len(got) < samples and isinstance(dur, (int, float)):
                got.append(float(dur))
    return {tid: (statistics.median(v), len(v)) for tid, v in picked.items() if v}


def expected(t, history, cfg=None):
    """(seconds, 'history' | 'estimate') for registry entry t, or (None, None) when nothing is known. A test whose
    opt-in follows a bench.json key (w1s4_soak) always takes the estimate."""
    key = t.get("opt_in")
    if key and optin.OPT_INS[key].get("minutes_key"):
        return optin.estimate_s(key, cfg), "estimate"
    h = (history or {}).get(t["id"])
    if h:
        return h[0], "history"
    if key:
        est = optin.estimate_s(key, cfg)
        if est is not None:
            return est, "estimate"
    return None, None


def fmt_expected(sec, kind):
    """'0:12' from history, '~15:00' for an estimate, '?' when unknown."""
    if sec is None:
        return "?"
    return ("~" if kind == "estimate" else "") + fmt_duration(round(sec))


def fmt_span(sec):
    """A human total: '<1 min', '12 min', '2 h 05 min'."""
    sec = max(0, int(round(sec)))
    if sec < 60:
        return "<1 min"
    m = (sec + 30) // 60
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"
