"""Settings-storage (NVS) use on each USB-attached WCB, read with the firmware's read-only ?NVS and recorded at the
start and end of every run long enough to take a config baseline (docs/HIL_TEST_AUDIT.md F10). Each reading goes
into the session log, the run's checkpoint (so report.md shows it) and results/nvs_history.csv, one row per board and
phase, so storage creep shows across runs. A board whose firmware predates ?NVS records nothing; nothing here can fail
a run."""
import csv
import os
import re
from datetime import datetime

from .wcb import WCB

LINE = re.compile(r"^NVS: used=(\d+) free=(\d+) available=(\d+) total=(\d+) namespaces=(\d+) \((\d+)% used\)$")
SPACE = re.compile(r"^  (\S+)\s+(\d+)$")
FIELDS = ("used", "free", "available", "total", "namespaces", "pct")
HISTORY = "nvs_history.csv"


def parse(lines):
    """(stats, {namespace: entries}) from ?NVS output; (None, {}) when it has no usage line."""
    stats, spaces = None, {}
    for x in lines:
        x = x.rstrip()
        m = LINE.match(x)
        if m:
            stats = dict(zip(FIELDS, (int(v) for v in m.groups())))
            continue
        m = SPACE.match(x)
        if stats is not None and m:
            spaces[m.group(1)] = int(m.group(2))
    return stats, spaces


def summary(stats, top=5):
    """One line for a note or a report: used/total, available, and the biggest namespaces."""
    most = ", ".join(f"{k} {v}" for k, v in sorted(stats.get("spaces", {}).items(), key=lambda kv: -kv[1])[:top])
    return (f"used {stats['used']}/{stats['total']} ({stats['pct']}%), available {stats['available']}"
            + (f"; most: {most}" if most else ""))


def record(bench, phase, run_name=None):
    """{"W<n>": stats with its namespaces under "spaces"} for every WCB with its own USB cable."""
    out = {}
    for num, dev in sorted(bench.usb_wcbs().items()):
        board = f"W{num}"
        try:
            stats, spaces = parse(WCB(bench.dev(dev)).run("?NVS", timeout=6))
        except Exception as e:  # noqa: BLE001 - a reading never fails the run
            bench.note(f"NVS {board} at {phase}: not read ({str(e).splitlines()[0] if str(e) else type(e).__name__})")
            continue
        if not stats:
            bench.note(f"NVS {board} at {phase}: no ?NVS in this firmware")
            continue
        stats["spaces"] = spaces
        out[board] = stats
        bench.note(f"NVS {board} at {phase}: {summary(stats)}")
        _append(bench.results_root, run_name, board, phase, stats)
    return out


def _append(results_root, run_name, board, phase, stats):
    path = os.path.join(results_root, HISTORY)
    new = not os.path.exists(path)
    try:
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["when", "run", "board", "phase", *FIELDS[:-1], "pct_used", "namespaces_json"])
            w.writerow([datetime.now().isoformat(timespec="seconds"), run_name or "", board, phase,
                        *(stats[k] for k in FIELDS), repr(stats.get("spaces", {}))])
    except OSError:
        pass
