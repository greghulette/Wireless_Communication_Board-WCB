"""Where Intellex keeps things, under its venv, in a scratch directory inside the stage (intellex.paths_logs_unit,
IX-WP3). Nothing outside the stage is read or written: INTELLEX_DATA_DIR and LOCALAPPDATA point into the stage
(run_intellex_py), and each case re-points the data directory at its own subdirectory - INTELLEX_DATA_DIR for the frozen
rules, and for the NaviLink move (which INTELLEX_DATA_DIR bypasses) the platform's own base with INTELLEX_DATA_DIR
lifted: LOCALAPPDATA on Windows, HOME on macOS and Linux (XDG_DATA_HOME lifted too). So every case runs on every
platform (the IX re-scan's paths.data_dir_rules: they used to skip off Windows).

- paths.py (Intellex src/paths.py:56-152): a frozen build reads the user copy only when it is non-empty (data_dir), the
  wikis decide per subdirectory (data_subdir), updates are written to the user directory (write_dir); with no
  INTELLEX_DATA_DIR the directory is the platform's own (~/Library/Application Support/Intellex on macOS), and the
  pre-rename NaviLink directory there is moved once and never merged into an existing Intellex one (_migrate_legacy).
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
    """paths as a frozen build sees it: FROZEN set, BUNDLE_DIR at `bundle`, and the user data directory at `user`
    (INTELLEX_DATA_DIR); put back after."""

    def __init__(self, bundle, user):
        import paths
        self.paths, self.bundle, self.user = paths, bundle, user

    def __enter__(self):
        import pathlib
        p = self.paths
        self.saved = (p.FROZEN, p.BUNDLE_DIR, p._migrated, os.environ.get("INTELLEX_DATA_DIR"))
        p.FROZEN, p.BUNDLE_DIR, p._migrated = True, pathlib.Path(self.bundle), False
        os.environ["INTELLEX_DATA_DIR"] = self.user
        return p

    def __exit__(self, *exc):
        p = self.paths
        p.FROZEN, p.BUNDLE_DIR, p._migrated, d = self.saved
        if d is None:
            os.environ.pop("INTELLEX_DATA_DIR", None)
        else:
            os.environ["INTELLEX_DATA_DIR"] = d


class _PlatformHome:
    """paths.user_data_dir() with no INTELLEX_DATA_DIR, the platform's base re-pointed into `root`: LOCALAPPDATA on
    Windows, HOME on macOS and Linux (XDG_DATA_HOME lifted) -> (paths, the base the Intellex directory sits in)."""
    KEYS = ("INTELLEX_DATA_DIR", "LOCALAPPDATA", "HOME", "XDG_DATA_HOME")

    def __init__(self, root):
        import paths
        self.paths, self.root = paths, root

    def __enter__(self):
        p = self.paths
        self.saved = (p._migrated, {k: os.environ.get(k) for k in self.KEYS})
        p._migrated = False
        os.environ.pop("INTELLEX_DATA_DIR", None)
        os.environ.pop("XDG_DATA_HOME", None)
        if sys.platform == "win32":
            os.environ["LOCALAPPDATA"] = self.root
            base = self.root
        else:
            os.environ["HOME"] = self.root
            base = (os.path.join(self.root, "Library", "Application Support") if sys.platform == "darwin"
                    else os.path.join(self.root, ".local", "share"))
        return p, base

    def __exit__(self, *exc):
        self.paths._migrated, env = self.saved
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@case("a frozen build reads a user copy only when it is non-empty; wikis decide per subdirectory; updates go to the "
      "user directory", group="main")
def frozen_paths(ctx):
    root = _scratch(ctx, "frozen")
    bundle, user = os.path.join(root, "bundle"), os.path.join(root, "user")
    _touch(os.path.join(bundle, "webui", "index.html"))
    _touch(os.path.join(bundle, "wiki", "wcb", "Home.md"))
    _touch(os.path.join(bundle, "wiki", "navicore", "Home.md"))
    with _Frozen(bundle, user) as p:
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
        os.makedirs(os.path.join(user, "wiki", "navicore"), exist_ok=True)
        check(str(p.data_subdir("wiki", "navicore")) == os.path.join(bundle, "wiki", "navicore"),
              "an EMPTY downloaded wiki directory shadowed the bundled one")
        check(str(p.write_dir("firmware")) == os.path.join(user, "firmware"), "a frozen update would write the bundle")
    with _Frozen(bundle, user) as p:
        p.FROZEN = False
        check(str(p.data_dir("webui")) == os.path.join(bundle, "webui"), "from source, a user copy was read")
        check(str(p.write_dir("firmware")) == os.path.join(bundle, "firmware"), "from source, updates stay in src/")


@case("with no INTELLEX_DATA_DIR the data directory is the platform's own; the NaviLink one there is moved to "
      "Intellex once, and never merged into an existing one", group="main")
def legacy_move(ctx):
    root = _scratch(ctx, "legacy")
    with _PlatformHome(root) as (p, base):
        _touch(os.path.join(base, "NaviLink", "settings.json"), '{"branch": {"wcb": "old"}}')
        d = p.user_data_dir()
        check(str(d) == os.path.join(base, "Intellex"), f"user_data_dir {d}, expected {os.path.join(base, 'Intellex')} "
                                                        f"(the platform's own place for {sys.platform})")
        check(os.path.isfile(os.path.join(d, "settings.json")), "the NaviLink directory was not moved to Intellex")
        check(not os.path.exists(os.path.join(base, "NaviLink")), "NaviLink is still there after the move")
    _touch(os.path.join(base, "NaviLink", "other.json"))
    with _PlatformHome(root) as (p, base):
        p.user_data_dir()
        check(os.path.isfile(os.path.join(base, "NaviLink", "other.json")),
              "a second NaviLink directory was touched although Intellex already existed")
        check(not os.path.exists(os.path.join(base, "Intellex", "other.json")),
              "a NaviLink directory was merged into an existing Intellex one")
    with _PlatformHome(root) as (p, base):
        os.environ["INTELLEX_DATA_DIR"] = os.path.join(root, "override")
        try:
            check(str(p.user_data_dir()) == os.path.join(root, "override"), "INTELLEX_DATA_DIR did not win")
            check(os.path.isfile(os.path.join(base, "NaviLink", "other.json")),
                  "with INTELLEX_DATA_DIR set the legacy directory was still moved")
        finally:
            os.environ.pop("INTELLEX_DATA_DIR", None)


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


# ------------------------------------------------------------------ group "certs" (intellex.certs_context_unit)
@case("certs.context() can verify on this venv; with an empty platform store it loads certifi's bundle, keeps "
      "verification on, and is built once; a populated store is left alone; with no certifi it fails honestly",
      group="certs")
def certs_context(ctx):
    import certifi
    import certs
    problems = []
    certs.context.cache_clear()
    try:
        c = certs.context()
        n = c.cert_store_stats()["x509_ca"]
        if n <= 0 or c.verify_mode != ssl.CERT_REQUIRED or not c.check_hostname:
            problems.append(f"this venv's context: {n} CA(s), verify_mode {c.verify_mode}, check_hostname "
                            f"{c.check_hostname} - every HTTPS fetch would fail or go unverified")
        platform = ssl.create_default_context().cert_store_stats()["x509_ca"]
        ctx.note(f"certs_context: the platform store holds {platform} CA(s); the context Intellex uses holds {n}")

        ref = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ref.load_verify_locations(cafile=certifi.where())
        want = ref.cert_store_stats()["x509_ca"]
        real = ssl.create_default_context
        made = []

        def empty(*a, **k):                     # a stock python.org macOS build: nothing in the store
            e = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            made.append(e)
            return e
        certs.context.cache_clear()
        ssl.create_default_context = empty
        try:
            got = certs.context()
            again = certs.context()
        finally:
            ssl.create_default_context = real
        if got is not made[0] or got.cert_store_stats()["x509_ca"] != want:
            problems.append(f"an empty store: the context holds {got.cert_store_stats()['x509_ca']} CA(s), expected "
                            f"certifi's {want}")
        if got.verify_mode != ssl.CERT_REQUIRED or not got.check_hostname:
            problems.append("an empty store: verification was turned off instead of CAs found")
        if again is not got or len(made) != 1:
            problems.append(f"the context was built {len(made)} times for two calls (it is cached)")

        with open(certifi.where(), encoding="ascii", errors="replace") as f:
            pem = f.read()
        one = pem[pem.index("-----BEGIN CERTIFICATE-----"):pem.index("-----END CERTIFICATE-----") + 25]

        def populated(*a, **k):                 # a store with something in it - one CA, so certifi's would show
            e = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            e.load_verify_locations(cadata=one)
            made.append(e)
            return e
        certs.context.cache_clear()
        ssl.create_default_context = populated
        try:
            got = certs.context()
        finally:
            ssl.create_default_context = real
        if got.cert_store_stats()["x509_ca"] != 1:
            problems.append(f"a populated store (1 CA) now holds {got.cert_store_stats()['x509_ca']}: it should be "
                            f"left alone")

        saved = sys.modules.get("certifi")
        sys.modules["certifi"] = None
        certs.context.cache_clear()
        ssl.create_default_context = empty
        try:
            got = certs.context()
        finally:
            ssl.create_default_context = real
            sys.modules["certifi"] = saved
        if got.cert_store_stats()["x509_ca"] != 0 or got.verify_mode != ssl.CERT_REQUIRED or not got.check_hostname:
            problems.append("no certifi and an empty store: expected the empty context with verification still on")
    finally:
        certs.context.cache_clear()
    check(not problems, "; ".join(problems))



# ------------------------------------------------------------------ group "winsize_mac" (intellex.winsize_mac_unit)
class _Rect:
    def __init__(self, x, y, w, h):
        import types
        self.origin = types.SimpleNamespace(x=float(x), y=float(y))
        self.size = types.SimpleNamespace(width=float(w), height=float(h))


class _FakeScreen:
    def __init__(self, full, vis):
        self._full, self._vis = full, vis

    def frame(self):
        return self._full

    def visibleFrame(self):
        return self._vis


def _fake_appkit(main):
    """An AppKit module whose NSScreen.mainScreen() answers `main` (a _FakeScreen, None, or an exception to raise)."""
    import types
    mod = types.ModuleType("AppKit")

    class NSScreen:
        @staticmethod
        def mainScreen():
            if isinstance(main, Exception):
                raise main
            return main
    mod.NSScreen = NSScreen
    return mod


@case("winsize._mac_visible_frame: the main screen less the menu bar and Dock, top measured down from the screen's own "
      "top (AppKit counts y up from the bottom) and relative to that screen, not the desktop; no main screen is None; "
      "on this Mac the real call gives four ints inside the main screen, and fit() centres the window in it",
      group="winsize_mac")
def mac_visible_frame(ctx):
    import winsize
    problems = []
    saved = sys.modules.get("AppKit")
    try:
        # A 2560x1440 screen to the right of and below the primary (global origin 1792,-300): a 25-point menu bar at
        # its top, a 60-point Dock at its bottom. AppKit's y grows upwards, so the visible part starts 60 above the
        # frame's bottom edge.
        full, vis = _Rect(1792, -300, 2560, 1440), _Rect(1792, -240, 2560, 1355)
        sys.modules["AppKit"] = _fake_appkit(_FakeScreen(full, vis))
        got = winsize._mac_visible_frame()
        if got != (0, 25, 2560, 1355):
            problems.append(f"a secondary screen with a menu bar and a bottom Dock: {got}, expected (0, 25, 2560, 1355) "
                            f"- left and top relative to that screen, top below the menu bar")
        if not all(isinstance(v, int) for v in (got or ())):
            problems.append(f"not ints: {got}")
        # A Dock on the left: the visible part starts 80 points in.
        sys.modules["AppKit"] = _fake_appkit(_FakeScreen(_Rect(0, 0, 1792, 1120), _Rect(80, 0, 1712, 1095)))
        got = winsize._mac_visible_frame()
        if got != (80, 25, 1712, 1095):
            problems.append(f"a left Dock on the primary screen: {got}, expected (80, 25, 1712, 1095)")
        if sys.platform == "darwin":
            want = {"width": 1280, "height": 880, "x": 80 + (1712 - 1280) // 2, "y": 25 + (1095 - 880) // 2,
                    "min_size": (900, 600)}
            if winsize.fit(1280, 880, (900, 600)) != want:
                problems.append(f"fit() on that screen: {winsize.fit(1280, 880, (900, 600))}, expected {want}")
            sys.modules["AppKit"] = _fake_appkit(RuntimeError("no window server"))
            if winsize.fit(1280, 880, (900, 600)) != {"width": 1280, "height": 880, "min_size": (900, 600)}:
                problems.append("fit() with AppKit failing did not fall back to the requested size")
        sys.modules["AppKit"] = _fake_appkit(None)
        if winsize._mac_visible_frame() is not None:
            problems.append("no main screen: not None")
    finally:
        if saved is None:
            sys.modules.pop("AppKit", None)
        else:
            sys.modules["AppKit"] = saved
    if sys.platform == "darwin":
        real = winsize._mac_visible_frame()
        from AppKit import NSScreen
        f = NSScreen.mainScreen().frame()
        if not (isinstance(real, tuple) and len(real) == 4 and all(isinstance(v, int) for v in real)):
            problems.append(f"the real call: {real!r}, not four ints")
        else:
            left, top, w, h = real
            if not (w > 0 and h > 0 and left >= 0 and top >= 0 and left + w <= f.size.width
                    and top + h <= f.size.height):
                problems.append(f"the real call: {real} does not lie inside the main screen's "
                                f"{f.size.width:.0f}x{f.size.height:.0f}")
        ctx.note(f"winsize_mac_unit: this Mac's main screen {f.size.width:.0f}x{f.size.height:.0f}, visible frame {real}")
    else:
        ctx.note("winsize_mac_unit: not a Mac - the real AppKit call is not made here")
    check(not problems, "; ".join(problems))


# ------------------------------------------------------------------ group "applog" (half of intellex.applog_unit)
@case("applog.start() with a logs directory that cannot be made returns None, path() stays None and stdout and stderr "
      "are left as they were; the tee writes and flushes each line to the file, and a sink or console that fails "
      "never breaks a print", group="applog")
def applog_fallback(ctx):
    import contextlib
    import io
    import applog
    import paths
    problems = []
    d = _scratch(ctx, "applog_unwritable")
    _touch(os.path.join(d, "logs"), "a file where the logs directory goes")
    saved = os.environ.get("INTELLEX_DATA_DIR")
    out0, err0 = sys.stdout, sys.stderr
    try:
        os.environ["INTELLEX_DATA_DIR"] = d
        check(str(paths.user_data_dir()) == d, f"INTELLEX_DATA_DIR not honoured: {paths.user_data_dir()}")
        got = applog.start()
        if got is not None or applog.path() is not None:
            problems.append(f"with no logs directory possible start() returned {got!r}, path() {applog.path()!r}")
        if sys.stdout is not out0 or sys.stderr is not err0:
            problems.append("start() replaced stdout/stderr although no log opened")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            print("HIL-after-failed-log")
        if buf.getvalue() != "HIL-after-failed-log\n":
            problems.append(f"print after the failed start: {buf.getvalue()!r}")
    finally:
        sys.stdout, sys.stderr = out0, err0
        if saved is None:
            os.environ.pop("INTELLEX_DATA_DIR", None)
        else:
            os.environ["INTELLEX_DATA_DIR"] = saved

    class Sink:
        def __init__(self, fail=False):
            self.fail, self.events = fail, []

        def write(self, t):
            if self.fail:
                raise OSError("HIL: disk full")
            self.events.append(("write", t))

        def flush(self):
            if self.fail:
                raise OSError("HIL: disk full")
            self.events.append(("flush",))
    sink, console = Sink(), io.StringIO()
    tee = applog._Tee(console, sink)
    n = tee.write("HIL line\n")
    if n != len("HIL line\n") or console.getvalue() != "HIL line\n" or sink.events != [("write", "HIL line\n"),
                                                                                      ("flush",)]:
        problems.append(f"a tee'd line: returned {n}, console {console.getvalue()!r}, sink {sink.events} - expected "
                        f"both written and the file flushed at once")
    bad, console2 = Sink(fail=True), io.StringIO()
    tee2 = applog._Tee(console2, bad)
    try:
        n = tee2.write("HIL survives\n")
        tee2.flush()
        if console2.getvalue() != "HIL survives\n" or n != len("HIL survives\n"):
            problems.append(f"with a failing log file the console got {console2.getvalue()!r}")
    except Exception as e:  # noqa: BLE001
        problems.append(f"a failing log file broke the print: {type(e).__name__}: {e}")
    sink3 = Sink()
    tee3 = applog._Tee(None, sink3)                    # pythonw / a windowed build: no console at all
    try:
        tee3.write("HIL no console\n")
        tee3.flush()
        if ("write", "HIL no console\n") not in sink3.events or tee3.isatty():
            problems.append(f"with no console the file got {sink3.events}")
    except Exception as e:  # noqa: BLE001
        problems.append(f"no console broke the tee: {type(e).__name__}: {e}")
    check(not problems, "; ".join(problems))


if __name__ == "__main__":
    sys.exit(_ixpy.main())
