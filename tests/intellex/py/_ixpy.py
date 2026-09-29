"""The runner every script in tests/intellex/py shares. The scripts import Intellex's own modules, so they run under
Intellex's venv (aiohttp, websockets, esptool, pyserial), started by the WCB harness (tests/hil/hil/intellex.py
run_intellex_py) as

    <Intellex>/.venv/Scripts/python.exe tests/intellex/py/<script>.py --stage <staged Intellex> --args <json> --out <file>

with the leash in the environment (INTELLEX_OFFLINE=1, INTELLEX_SERIAL_ALLOW naming the one port the test was given or
none, INTELLEX_DISCOVER_HOSTS empty, LOCALAPPDATA inside the stage). The staged src/ and tools/ go first on sys.path, so
`import wcb_flash` is the staged copy, never Greg's checkout.

A script registers cases with @case(name, group=...); args["group"] (a name or a list) picks which run, "main" by
default. A case passes by returning, skips by raising SkipCase, and fails by raising anything else. The results file is
{"cases": [{"name", "status", "message"}], "notes": [...]}, judged by hil/intellex.py judge_py.

Everything a case prints or returns lands in the run's session.log: never a config value, a password or an SSID - only
lengths, counts, key names and a test's own markers.
"""
import argparse
import json
import os
import sys
import time
import traceback

CASES = []
FIXTURES = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fixtures"))


class SkipCase(Exception):
    """This case cannot run here (the bench lacks what it needs); says why."""


class Ctx:
    """What a case gets: the stage, the harness's args, and note() for a line in the harness's session.log."""

    def __init__(self, stage, args):
        self.stage, self.args = stage, args
        self.src, self.tools = os.path.join(stage, "src"), os.path.join(stage, "tools")
        self.notes = []

    def note(self, text):
        self.notes.append(str(text))
        print(f"  note  {text}", flush=True)


def case(name, group="main"):
    def deco(fn):
        CASES.append((group, name, fn))
        return fn
    return deco


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def fw_key(branch):
    """Intellex fwcache._key's directory name for a branch ('/' flattened to '__')."""
    return (branch or "main").replace("/", "__").replace("\\", "__")


REPOS = {"wcb": ("greghulette", "Wireless_Communication_Board-WCB", "Code/bin"),
         "navicore": ("greghulette", "NaviCore", "firmware")}


def seed_cache(ctx, product, branch, files):
    """A firmware set for (product, branch) in the staged src/firmware (where a non-frozen Intellex caches, fwcache.py),
    with the listing.json the flashers fall back to offline. files: {name: bytes} -> the directory. The same layout as
    hil/intellex.py seed_firmware, which the harness-side tests use."""
    owner, repo, path = REPOS[product]
    d = os.path.join(ctx.src, "firmware", product, fw_key(branch))
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True)
    ap.add_argument("--args", default="{}")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    ctx = Ctx(os.path.abspath(a.stage), json.loads(a.args or "{}"))
    sys.path[:0] = [ctx.src, ctx.tools]
    groups = ctx.args.get("group", "main")
    groups = [groups] if isinstance(groups, str) else list(groups)
    results = []
    for group, name, fn in CASES:
        if group not in groups:
            continue
        t0 = time.monotonic()
        try:
            fn(ctx)
            status, msg = "passed", ""
        except SkipCase as e:
            status, msg = "skipped", str(e)
        except AssertionError as e:
            status, msg = "failed", str(e) or "assertion failed"
        except Exception:   # noqa: BLE001 - a crash is this case's failure, not the run's
            status, msg = "failed", traceback.format_exc(limit=8).strip()
        first = msg.splitlines()[0] if msg else ""
        print(f"{status.upper():7} {name} ({time.monotonic() - t0:.1f}s){': ' + first if first else ''}", flush=True)
        results.append({"name": name, "status": status, "message": msg})
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump({"cases": results, "notes": ctx.notes}, f, indent=1)
    return 1 if any(r["status"] == "failed" for r in results) else 0
