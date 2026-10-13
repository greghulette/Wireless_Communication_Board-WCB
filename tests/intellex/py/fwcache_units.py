"""Intellex's offline firmware cache (src/fwcache.py) under its venv, in scratch directories inside the stage. Nothing
outside the stage is read or written: INTELLEX_DATA_DIR points into it (run_intellex_py), and the frozen case re-points
paths.BUNDLE_DIR and INTELLEX_DATA_DIR at its own subdirectories.

group "atomic" (intellex.fwcache_atomic_unit): store() and store_listing() (:108-140, :183-193) write <name>.part and
rename it, so an interrupted write never leaves a short image under a name that means a build; an empty payload is not
stored; the manifest records each image under "<product>/<branch>" with the time it arrived. A rename that fails leaves
the final name absent (the .part is what remains) and is swallowed: a cache never breaks a flash.

group "frozen_lookup" (half of intellex.fwcache_branch_slash): a frozen build reads each (product, branch) set from the
user directory first and the bundled one second, deciding per set (:65-105) - a user directory holding main's images
must not hide a WIFI set that shipped in the app - and a branch miss is a miss, never another branch's image; updates
are written to the user directory, never into the app.
"""
import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ixpy  # noqa: E402
from _ixpy import case, check  # noqa: E402


def _scratch(ctx, name):
    d = os.path.join(ctx.stage, "scratch", name)
    os.makedirs(d, exist_ok=True)
    return d


def _files(root):
    out = {}
    for d, _, fs in os.walk(root):
        for f in fs:
            p = os.path.join(d, f)
            out[os.path.relpath(p, root).replace(os.sep, "/")] = open(p, "rb").read()
    return out


class _Paths:
    """paths as a build sees it: FROZEN, BUNDLE_DIR and INTELLEX_DATA_DIR set for the block; put back after."""

    def __init__(self, frozen, bundle, user):
        import paths
        self.p, self.frozen, self.bundle, self.user = paths, frozen, bundle, user

    def __enter__(self):
        self.saved = (self.p.FROZEN, self.p.BUNDLE_DIR, os.environ.get("INTELLEX_DATA_DIR"))
        self.p.FROZEN, self.p.BUNDLE_DIR = self.frozen, pathlib.Path(self.bundle)
        os.environ["INTELLEX_DATA_DIR"] = self.user
        return self.p

    def __exit__(self, *exc):
        self.p.FROZEN, self.p.BUNDLE_DIR, d = self.saved
        if d is None:
            os.environ.pop("INTELLEX_DATA_DIR", None)
        else:
            os.environ["INTELLEX_DATA_DIR"] = d


# ------------------------------------------------------------------ group "atomic"
@case("store() writes through a .part file and renames it, records the image in the manifest, skips an empty "
      "payload, and a failing rename leaves no file under the build's name", group="atomic")
def atomic_store(ctx):
    import time
    import fwcache
    root = _scratch(ctx, "atomic")
    bundle, user = os.path.join(root, "bundle"), os.path.join(root, "user")
    name = "WCB_6.2.1_250646RSEP2026_hilatomic_ESP32.bin"
    problems = []
    with _Paths(False, bundle, user):                      # from source: the cache is BUNDLE_DIR/firmware
        fw = os.path.join(bundle, "firmware")
        t0 = int(time.time())
        fwcache.store("wcb", "feature/hil", name, b"IMAGE" * 100)
        got = _files(fw)
        if got.get(f"wcb/feature__hil/{name}") != b"IMAGE" * 100:
            problems.append(f"store() did not leave the image at wcb/feature__hil/{name}: {sorted(got)}")
        if any(k.endswith(".part") for k in got):
            problems.append(f"store() left a .part behind: {sorted(got)}")
        try:
            m = json.loads(got.get("manifest.json", b"{}"))
        except ValueError:
            m = {}
        stamp = (m.get("wcb/feature__hil") or {}).get(name)
        if not isinstance(stamp, int) or not t0 - 2 <= stamp <= t0 + 5:
            problems.append(f"the manifest's wcb/feature__hil entry for the image is {stamp!r}, expected the time it "
                            f"arrived ({t0}): {m}")
        fwcache.store_listing("wcb", "feature/hil", b'[{"name": "x"}]')
        got = _files(fw)
        if got.get("wcb/feature__hil/listing.json") != b'[{"name": "x"}]' or any(k.endswith(".part") for k in got):
            problems.append(f"store_listing() left {sorted(got)}")

        before = _files(fw)
        fwcache.store("wcb", "feature/hil", "WCB_empty_ESP32.bin", b"")
        fwcache.store_listing("wcb", "hil-empty", b"")
        if _files(fw) != before:
            problems.append(f"an empty payload was stored: {sorted(set(_files(fw)) - set(before))}")

        # The rename fails (a full disk, a file held open): the build's name must not appear, and nothing raises.
        real = pathlib.Path.replace

        def boom(self, target):
            raise OSError(28, "HIL: No space left on device")
        pathlib.Path.replace = boom
        try:
            fwcache.store("wcb", "hil-full", name, b"IMAGE" * 100)
            fwcache.store_listing("wcb", "hil-full", b"[]")
            raised = None
        except Exception as e:                              # noqa: BLE001 - the case reports it
            raised = e
        finally:
            pathlib.Path.replace = real
        got = _files(fw)
        if raised is not None:
            problems.append(f"a failing rename raised {type(raised).__name__}: {raised} - a cache must never break the "
                            f"flash that is storing into it")
        if f"wcb/hil-full/{name}" in got or "wcb/hil-full/listing.json" in got:
            problems.append(f"a failing rename still left the final name: {sorted(k for k in got if 'hil-full' in k)}")
        if got.get(f"wcb/hil-full/{name}.part") != b"IMAGE" * 100:
            problems.append(f"after a failing rename the .part is {sorted(k for k in got if 'hil-full' in k)}: expected "
                            f"the bytes under {name}.part and nothing under the build's name")
        if name in ((json.loads(got.get("manifest.json", b"{}")) or {}).get("wcb/hil-full") or {}):
            problems.append("the manifest records an image whose rename failed")
        if fwcache.load("wcb", "hil-full", name) is not None:
            problems.append("load() returned an image whose rename failed")
    check(not problems, "; ".join(problems))


# ------------------------------------------------------------------ group "frozen_lookup"
@case("frozen: each (product, branch) set is read from the user directory first and the bundled one second, per set; "
      "a branch miss is a miss; updates go to the user directory", group="frozen_lookup")
def frozen_lookup(ctx):
    import fwcache
    root = _scratch(ctx, "frozen")
    bundle, user = os.path.join(root, "bundle"), os.path.join(root, "user")

    def put(base, rel, data):
        p = os.path.join(base, "firmware", *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
    a, b = "WCB_6.2.1_hil_ESP32.bin", "WCB_6.2.1_hilwifi_ESP32.bin"
    put(bundle, f"wcb/main/{a}", b"bundled-main")
    put(user, f"wcb/main/{a}", b"user-main")
    put(bundle, f"wcb/WIFI/{b}", b"bundled-wifi")
    put(bundle, "wcb/WIFI/listing.json", b'["bundled-wifi"]')
    put(user, "wcb/main/listing.json", b'["user-main"]')
    put(bundle, f"wcb/feature__x/{b}", b"bundled-feature")
    os.makedirs(os.path.join(user, "firmware", "wcb", "WIFI", b), exist_ok=True)   # a directory, not a file: unreadable
    problems = []
    with _Paths(True, bundle, user):
        for branch, nm, want in (("main", a, b"user-main"), ("WIFI", b, b"bundled-wifi"), ("WIFI", a, None),
                                 ("feature/x", b, b"bundled-feature"), ("feature/y", b, None), ("main", b, None)):
            got = fwcache.load("wcb", branch, nm)
            if got != want:
                problems.append(f"frozen load(wcb, {branch}, {nm}): {got!r}, expected {want!r}")
        for branch, want in (("WIFI", b'["bundled-wifi"]'), ("main", b'["user-main"]'), ("feature/x", None)):
            if fwcache.load_listing("wcb", branch) != want:
                problems.append(f"frozen load_listing(wcb, {branch}): {fwcache.load_listing('wcb', branch)!r}, expected "
                                f"{want!r}")
        s = fwcache.summary({"wcb": "WIFI", "navicore": "main"})
        if (s.get("wcb") or {}).get("count") != 1 or (s.get("wcb") or {}).get("branch") != "WIFI":
            problems.append(f"frozen summary for WIFI: {s.get('wcb')} - the user directory's main set hid the bundled "
                            f"WIFI set")
        bundle_before = _files(os.path.join(bundle, "firmware"))
        fwcache.store("wcb", "WIFI", "WCB_new_ESP32.bin", b"downloaded")
        if _files(os.path.join(bundle, "firmware")) != bundle_before:
            problems.append("a frozen build wrote into the app's bundled firmware directory")
        if not os.path.isfile(os.path.join(user, "firmware", "wcb", "WIFI", "WCB_new_ESP32.bin")):
            problems.append("a frozen build did not write the download to the user directory")
        if fwcache.load("wcb", "WIFI", "WCB_new_ESP32.bin") != b"downloaded":
            problems.append("a frozen build cannot read back what it just stored")
    check(not problems, "; ".join(problems))


if __name__ == "__main__":
    sys.exit(_ixpy.main())
