"""Where Intellex keeps things, under its venv, in a scratch directory inside the stage (intellex.paths_logs_unit,
IX-WP3). Nothing outside the stage is read or written: LOCALAPPDATA points into the stage (run_intellex_py), and each case
re-points it at its own subdirectory.

- paths.py (Intellex src/paths.py:81-144): a frozen build reads the user copy only when it is non-empty (data_dir), the
  wikis decide per subdirectory (data_subdir), updates are written to the user directory (write_dir), and the pre-rename
  NaviLink directory is moved once and never merged into an existing Intellex one (_migrate_legacy, :56-78).
- applog._prune (src/applog.py:91-106): keeps the newest KEEP_RUNS (20) runs across both name prefixes, ordered by the
  timestamp in the name, not by the name.
- winsize.fit (src/winsize.py:49-84): clamps to the work area less MARGIN, centres, never shrinks below 320x240, keeps
  min_size inside the window, and falls back to the requested size when the screen cannot be measured.
- certs.is_cert_error (src/certs.py:51-66): finds an SSLCertVerificationError however deep urllib buried it.
"""
import os
import ssl
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ixpy  # noqa: E402
from _ixpy import case, check  # noqa: E402


def _scratch(ctx, name):
    d = os.path.join(ctx.stage, "scratch", name)
    os.makedirs(d, exist_ok=True)
    return d


def _touch(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class _Frozen:
    """paths as a frozen build sees it: FROZEN set, BUNDLE_DIR at `bundle`, LOCALAPPDATA at `appdata`; put back after."""

    def __init__(self, bundle, appdata):
        import paths
        self.paths, self.bundle, self.appdata = paths, bundle, appdata

    def __enter__(self):
        p = self.paths
        self.saved = (p.FROZEN, p.BUNDLE_DIR, p._migrated, os.environ.get("LOCALAPPDATA"))
        import pathlib
        p.FROZEN, p.BUNDLE_DIR, p._migrated = True, pathlib.Path(self.bundle), False
        os.environ["LOCALAPPDATA"] = self.appdata
        return p

    def __exit__(self, *exc):
        p = self.paths
        p.FROZEN, p.BUNDLE_DIR, p._migrated, local = self.saved
        if local is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = local


@case("a frozen build reads a user copy only when it is non-empty; wikis decide per subdirectory; updates go to the "
      "user directory", group="main")
def frozen_paths(ctx):
    if sys.platform != "win32":
        raise _ixpy.SkipCase("paths.user_data_dir() reads LOCALAPPDATA on Windows only")
    root = _scratch(ctx, "frozen")
    bundle, appdata = os.path.join(root, "bundle"), os.path.join(root, "appdata")
    _touch(os.path.join(bundle, "webui", "index.html"))
    _touch(os.path.join(bundle, "wiki", "wcb", "Home.md"))
    _touch(os.path.join(bundle, "wiki", "navicore", "Home.md"))
    with _Frozen(bundle, appdata) as p:
        user = os.path.join(appdata, "Intellex")
        check(str(p.user_data_dir()) == user, f"user_data_dir {p.user_data_dir()}, expected {user}")
        check(str(p.data_dir("webui")) == os.path.join(bundle, "webui"), "no user copy: data_dir left the bundle")
        os.makedirs(os.path.join(user, "webui"), exist_ok=True)
        check(str(p.data_dir("webui")) == os.path.join(bundle, "webui"),
              "an EMPTY user copy shadowed the bundle (an interrupted update would blank the tool)")
        _touch(os.path.join(user, "webui", "index.html"))
        check(str(p.data_dir("webui")) == os.path.join(user, "webui"), "a non-empty user copy was not preferred")
        _touch(os.path.join(user, "wiki", "intellex", "Home.md"))
        check(str(p.data_subdir("wiki", "intellex")) == os.path.join(user, "wiki", "intellex"),
              "the downloaded intellex wiki was not used")
        check(str(p.data_subdir("wiki", "wcb")) == os.path.join(bundle, "wiki", "wcb"),
              "one downloaded wiki hid a sibling that exists only in the bundle")
        check(str(p.write_dir("firmware")) == os.path.join(user, "firmware"), "a frozen update would write the bundle")
    with _Frozen(bundle, appdata) as p:
        p.FROZEN = False
        check(str(p.write_dir("firmware")) == os.path.join(bundle, "firmware"), "from source, updates stay in src/")


@case("the NaviLink data directory is moved to Intellex once, and never merged into an existing one", group="main")
def legacy_move(ctx):
    if sys.platform != "win32":
        raise _ixpy.SkipCase("paths.user_data_dir() reads LOCALAPPDATA on Windows only")
    root = _scratch(ctx, "legacy")
    appdata, bundle = os.path.join(root, "appdata"), os.path.join(root, "bundle")
    _touch(os.path.join(appdata, "NaviLink", "settings.json"), '{"branch": {"wcb": "old"}}')
    with _Frozen(bundle, appdata) as p:
        d = p.user_data_dir()
        check(os.path.isfile(os.path.join(d, "settings.json")), "the NaviLink directory was not moved to Intellex")
        check(not os.path.exists(os.path.join(appdata, "NaviLink")), "NaviLink is still there after the move")
    _touch(os.path.join(appdata, "NaviLink", "other.json"))
    with _Frozen(bundle, appdata) as p:
        p.user_data_dir()
        check(os.path.isfile(os.path.join(appdata, "NaviLink", "other.json")),
              "a second NaviLink directory was touched although Intellex already existed")
        check(not os.path.exists(os.path.join(appdata, "Intellex", "other.json")),
              "a NaviLink directory was merged into an existing Intellex one")


@case("applog keeps the newest 20 runs across both prefixes, by the timestamp in the name", group="main")
def log_prune(ctx):
    import pathlib
    import applog
    d = pathlib.Path(_scratch(ctx, "logs"))
    # Six pre-rename runs, all OLDER than nineteen new ones: by timestamp the five oldest navilink runs go. Sorted by
    # name instead, every intellex-* sorts ahead of every navilink-*, and five of the NEW runs would go.
    old = [f"navilink-202501{day:02d}-120000.log" for day in range(1, 7)]
    new = [f"intellex-202609{day:02d}-120000.log" for day in range(1, 20)]
    for n in old + new + ["other-20270101-000000.log"]:
        (d / n).write_text("x", encoding="utf-8")
    check(applog.KEEP_RUNS == 20, f"KEEP_RUNS is {applog.KEEP_RUNS}; this case is written for 20")
    applog._prune(d)
    left = sorted(p.name for p in d.iterdir())
    want = sorted([old[-1]] + new + ["other-20270101-000000.log"])
    check(left == want, f"after the prune: {len(left)} files; kept {sorted(set(left) - set(want))[:3]} it should have "
                        f"deleted, deleted {sorted(set(want) - set(left))[:3]} it should have kept")


@case("winsize.fit clamps to the work area, centres, and falls back when the screen cannot be measured", group="main")
def window_fit(ctx):
    import winsize
    real_win, real_mac = winsize._win_work_area, winsize._mac_visible_frame
    try:
        for area, want in (((0, 0, 1536, 816), {"width": 1280, "height": 792, "x": 128, "y": 12,
                                                  "min_size": (900, 600)}),
                           ((100, 40, 800, 500), {"width": 776, "height": 476, "x": 112, "y": 52,
                                                    "min_size": (776, 476)}),
                           ((0, 0, 300, 200), {"width": 320, "height": 240, "x": -10, "y": -20,
                                                 "min_size": (320, 240)})):
            winsize._win_work_area = winsize._mac_visible_frame = (lambda a=area: a)
            got = winsize.fit(1280, 880, (900, 600))
            check(got == want, f"work area {area}: {got}, expected {want}")
        winsize._win_work_area = winsize._mac_visible_frame = lambda: None
        check(winsize.fit(1280, 880, (900, 600)) == {"width": 1280, "height": 880, "min_size": (900, 600)},
              "an unmeasurable screen did not give back the requested size")

        def boom():
            raise OSError("no display")
        winsize._win_work_area = winsize._mac_visible_frame = boom
        check(winsize.fit(1280, 880, (900, 600)).get("width") == 1280, "a failing measurement broke fit()")
    finally:
        winsize._win_work_area, winsize._mac_visible_frame = real_win, real_mac


@case("certs.is_cert_error finds a certificate failure however deep it is buried, and nothing else", group="main")
def cert_errors(ctx):
    import certs
    import flash
    verify = ssl.SSLCertVerificationError(1, "certificate verify failed: unable to get local issuer certificate")
    wrapped = urllib.error.URLError(verify)
    try:
        try:
            raise wrapped
        except urllib.error.URLError as e:
            raise flash.FlashError("cannot reach GitHub") from e
    except flash.FlashError as e:
        chained = e
    check(certs.is_cert_error(verify), "a bare SSLCertVerificationError")
    check(certs.is_cert_error(wrapped), "a URLError whose reason is the verification failure")
    check(certs.is_cert_error(chained), "a FlashError raised from that URLError (__cause__)")
    check(not certs.is_cert_error(urllib.error.URLError(ConnectionRefusedError(10061, "refused"))), "a refused connect")
    check(not certs.is_cert_error(urllib.error.URLError("a string reason")), "a URLError with a string reason")
    check(not certs.is_cert_error(None), "None")
    loop = OSError("self-referential")
    loop.reason = loop
    check(not certs.is_cert_error(loop), "an error whose reason is itself (the walk must be bounded)")


if __name__ == "__main__":
    sys.exit(_ixpy.main())
